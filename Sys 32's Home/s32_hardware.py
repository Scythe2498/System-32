# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_hardware.py — Hardware detection, llama.cpp server management,
                  node installation, workflow defaults, preferences.
"""
from s32_config import (
    CONFIG, REFERENCE_DIR, WORKFLOW_DIR,
    _machine_name, _current_operator, _hw,
    state, log, http_get, http_post, is_up,
    os, json, datetime, threading, time, subprocess, re, urllib,
)
from s32_memory import (
    _load_machine, _save_machine, _load_operator, _save_operator,
    _ensure_memory_structure,
)
_MISSING_PATTERNS = [
    r"Cannot import .+?custom_nodes.+? module for custom nodes: (.+)",
    r"Cannot import module '([^']+)'",
    r"Failed to import ([^\s,\n]+)",
    r"No module named '([^']+)'",
    r"ModuleNotFoundError.*'([^']+)'",
    r"import_failed_extensions.*?\[([^\]]+)\]",
    r"Failed to load custom node.*?([A-Za-z0-9_\-]+)",
]

_LOG_IGNORE = {
    "triton", "triton.runtime", "triton.language",
    "flash_attn", "xformers", "bitsandbytes", "deepspeed",
    "flet", "kokoro_onnx", "sageattention", "onnxruntime",
    "accelerate", "compel", "cv2", "opencv",
    "sklearn", "scipy", "matplotlib", "imageio",
    "av", "insightface", "mmcv", "mmdet", "mmpose",
}

_PIP_NODES = {
    "comfy_aimdo":              "comfy-aimdo",
    "comfy_aimdo.vram_buffer":  "comfy-aimdo",
    "comfyui_frontend_package": "comfyui-frontend-package",
}

def _scan_log_for_missing(log_path):
    """Read a ComfyUI log and return list of missing node/module names — but ONLY from
    the MOST RECENT ComfyUI run. The log file is appended across boots, so a module
    that was missing on an earlier boot (and has since been installed) still has its
    'No module named X' line sitting in the file forever; scanning the whole file
    resurrects those as false 'currently missing' reports (this is what kept onnx
    flagging after it was installed). Anchor to the last startup marker so only the
    current run is considered."""
    if not log_path or not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return []
    # Trim to the most recent run. ComfyUI prints one of these near the start of every
    # launch; take the latest occurrence of any of them as the start of "this run".
    _RUN_MARKERS = ("Total VRAM", "ComfyUI startup time", "Set vram state",
                    "Starting server", "** Platform:")
    cut = max((text.rfind(mk) for mk in _RUN_MARKERS), default=-1)
    if cut >= 0:
        text = text[cut:]
    else:
        # No marker found — bound staleness by scanning only the tail rather than all history.
        text = "\n".join(text.splitlines()[-400:])
    found = set()
    for pattern in _MISSING_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            name = m.group(1).strip().strip("'\"")
            top = name.split(".")[0].lower()
            if name and len(name) > 2 and top not in _LOG_IGNORE:
                found.add(name)
    return sorted(found)

def _scan_workflow_error(error_dict):
    """
    Extract missing node names from a ComfyUI /prompt 400 error response.
    Captures what log scanning misses - nodes that fail at execution time.
    """
    missing = set()
    if not isinstance(error_dict, dict):
        return []
    # Top-level error
    err_type = error_dict.get("error", {}).get("type", "")
    if err_type == "missing_node_type":
        class_type = error_dict.get("error", {}).get("extra_info", {}).get("class_type", "")
        if class_type:
            missing.add(class_type)
    # Node-level errors
    for node_id, node_err in (error_dict.get("node_errors") or {}).items():
        for e in (node_err.get("errors") or []):
            if e.get("type") == "missing_node_type":
                ct = node_err.get("class_type", "")
                if ct:
                    missing.add(ct)
    return sorted(missing)

_registry_cache = None   # module-level cache for the ComfyUI node registry; populated
                         # on first successful _load_registry() call. Must exist at
                         # module scope so the `if _registry_cache is not None` check on
                         # the very first call reads a defined name instead of crashing.

def _load_registry():
    """Fetch the ComfyUI Manager node registry. Cached after first load."""
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    try:
        from s32_filesystem import _REGISTRY_URL
        req = urllib.request.Request(
            _REGISTRY_URL,
            headers={"User-Agent": "System32/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        _registry_cache = data.get("custom_nodes", data) if isinstance(data, dict) else data
        log(f"[S32] Node registry loaded: {len(_registry_cache)} entries")
        return _registry_cache
    except Exception as e:
        log(f"[S32] Could not load node registry: {e}")
        return []

def _registry_lookup(missing_names):
    """
    Match missing node/class names against the ComfyUI Manager registry.
    Uses strict matching - title must contain the name or vice versa,
    not just share some characters.
    Returns list of dicts: {name, title, url}
    """
    registry = _load_registry()
    results = []
    matched_urls = set()

    for name in missing_names:
        name_low = name.lower().replace("-", "").replace("_", "").replace(" ", "")
        best = None
        best_score = 0

        for entry in registry:
            title = (entry.get("title") or "").lower().replace("-", "").replace("_", "").replace(" ", "")
            ref   = (entry.get("reference") or "").lower()
            nodes = [n.lower().replace("-","").replace("_","") for n in (entry.get("nodename_pattern") or [])]

            score = 0
            # Exact match on title
            if name_low == title:
                score = 100
            # name is substring of title or vice versa (but both must be >3 chars)
            elif len(name_low) > 3 and len(title) > 3:
                if name_low in title:
                    score = 80
                elif title in name_low:
                    score = 70
            # Match in node name patterns
            if any(name_low in n or n in name_low for n in nodes if len(n) > 3):
                score = max(score, 60)
            # Match in repo URL
            ref_clean = ref.replace("-","").replace("_","").replace("/","").replace(".","")
            if name_low in ref_clean and len(name_low) > 4:
                score = max(score, 50)

            if score > best_score:
                url = entry.get("reference") or (entry.get("files") or [""])[0]
                if url:
                    best_score = score
                    best = {"name": name, "title": entry.get("title", name), "url": url}

        if best and best_score >= 50 and best["url"] not in matched_urls:
            matched_urls.add(best["url"])
            results.append(best)

    return results

def _install_node(url, custom_nodes_dir, python_exe):
    """
    Clone a node repo into custom_nodes_dir and install its dependencies.
    Handles empty existing directories by removing and re-cloning.
    Returns (success, message)
    """
    folder_name = url.rstrip("/").split("/")[-1]
    if folder_name.endswith(".git"):
        folder_name = folder_name[:-4]
    dest = os.path.join(custom_nodes_dir, folder_name)

    # If folder exists but is empty or has no __init__.py, remove and re-clone
    if os.path.exists(dest):
        files = os.listdir(dest)
        if not files or ("__init__.py" not in files and not any(f.endswith(".py") for f in files)):
            log(f"[S32] {folder_name} exists but appears empty/broken - re-cloning")
            import shutil
            shutil.rmtree(dest, ignore_errors=True)
        else:
            # Already installed and looks valid - update it
            try:
                r = subprocess.run(
                    ["git", "pull"], capture_output=True, text=True,
                    timeout=60, cwd=dest
                )
                if r.returncode == 0:
                    return True, f"Updated: {folder_name}"
            except Exception:
                pass
            return True, f"Already installed: {folder_name}"

    # git clone
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", url, dest],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return False, f"git clone failed: {result.stderr[:300]}"
    except FileNotFoundError:
        return False, "git not found - install Git from https://git-scm.com/download/win"
    except subprocess.TimeoutExpired:
        return False, "git clone timed out"

    msgs = [f"Cloned: {folder_name}"]

    # pip install requirements.txt if present
    req_file = os.path.join(dest, "requirements.txt")
    if os.path.exists(req_file):
        try:
            r2 = subprocess.run(
                [python_exe, "-m", "pip", "install", "-r", req_file,
                 "--quiet", "--break-system-packages"],
                capture_output=True, text=True, timeout=300
            )
            if r2.returncode == 0:
                msgs.append("Dependencies installed.")
            else:
                msgs.append(f"pip warnings: {r2.stderr[:300]}")
        except subprocess.TimeoutExpired:
            msgs.append("pip install timed out.")

    # run install.py if present
    install_script = os.path.join(dest, "install.py")
    if os.path.exists(install_script):
        try:
            r3 = subprocess.run(
                [python_exe, install_script],
                capture_output=True, text=True, timeout=120, cwd=dest
            )
            if r3.returncode == 0:
                msgs.append("install.py ran successfully.")
            else:
                msgs.append(f"install.py warnings: {r3.stderr[:200]}")
        except subprocess.TimeoutExpired:
            msgs.append("install.py timed out.")

    return True, "\n".join(msgs)

def _find_comfyui_python(comfy_dir=None):
    """
    Return the Documents venv Python - the one that loads custom node code.
    This is always the correct target for custom node dependency installs.
    """
    docs_venv = os.path.join(
        os.environ.get("USERPROFILE", ""),
        "Documents", "ComfyUI", ".venv", "Scripts", "python.exe"
    )
    if os.path.exists(docs_venv):
        return docs_venv
    return CONFIG.get("comfy_image_python", "")

def _pip_install_node(package_name, python_exe):
    """Install a pip package node. Returns (success, message)."""
    cmd = [python_exe, "-m", "pip", "install", package_name,
           "--quiet", "--force-reinstall", "--break-system-packages"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return True, f"pip installed: {package_name}"
        return False, f"pip failed: {r.stderr[:200]}"
    except subprocess.TimeoutExpired:
        return False, f"pip install timed out: {package_name}"

def check_missing_nodes():
    """
    Scan both ComfyUI logs for missing nodes.
    Injects findings directly into the chat pipeline so it sees and acts on them.
    """
    image_log = os.path.join(CONFIG["comfy_image_dir"], "system32_image.log")

    results = {}
    for label, log_path in [("image (8188)", image_log)]:
        missing = _scan_log_for_missing(log_path)
        if missing:
            results[label] = missing

    if not results:
        return

    # Build a short actionable message - skip long paths and known-ignorable items
    actionable = {}
    for label, names in results.items():
        clean = []
        for n in names:
            # Skip full path entries (they contain backslashes or start with drive letters)
            if "\\" in n or (len(n) > 2 and n[1] == ":"):
                continue
            # Skip anything that looks like a path fragment
            if "Cannot import" in n or "No module" in n:
                continue
            clean.append(n)
        if clean:
            actionable[label] = clean

    if not actionable:
        return

    # Keep the message short - max 5 nodes per instance
    lines = ["[SYSTEM] Missing custom nodes detected at startup:"]
    for label, names in actionable.items():
        lines.append(f"[{label}]")
        for n in names[:5]:
            lines.append(f"  • {n}")
        if len(names) > 5:
            lines.append(f"  • ...and {len(names)-5} more")
    lines.append("Call install_missing_nodes now to restore full toolkit access.")
    msg = "\n".join(lines)

    log(f"[S32] {msg}")
    state["missing_nodes"] = results

    # Inject as a user message so it sees it and responds with a tool call
    # Wait until LM Studio is ready before injecting
    def _inject_and_respond():
        for _ in range(60):
            if state.get("lmstudio_ready"):
                break
            time.sleep(5)
        if not state.get("lmstudio_ready"):
            # LM Studio never came up - fall back to UI notification only
            state["startup_notification"] = "\n".join(lines[1:-1])
            return
        try:
            state["messages"].append({"role": "user", "content": msg})
            from s32_llm import chat as _chat
            from s32_dispatch import dispatch_tool as _dispatch
            spoken, tool_name, tool_params = _chat(msg)
            if spoken:
                state["messages"].append({"role": "assistant", "content": spoken})
            if tool_name:
                log(f"[S32] Startup auto tool call: {tool_name}")
                for result in _dispatch(tool_name, tool_params or {}):
                    log(f"[S32] Startup tool result: {str(result)[:200]}")
        except Exception as e:
            log(f"[S32] Startup node check inject failed: {e}")
            state["startup_notification"] = "\n".join(lines[1:-1])

    threading.Thread(target=_inject_and_respond, daemon=True).start()

def tool_install_missing_nodes(target=None):
    """
    Auto-install missing nodes for one or both ComfyUI instances.
    target: 'image', 'video', or None (both)
    """
    import s32_config as _cfg
    if _cfg.airplane():
        return {"status": "airplane", "message": _cfg.AIRPLANE_MSG,
                "report": "Airplane mode is on — custom nodes can't be downloaded or installed."}
    missing = state.get("missing_nodes", {})
    if not missing:
        image_log = os.path.join(CONFIG["comfy_image_dir"], "system32_image.log")
        for label, log_path in [("image (8188)", image_log)]:
            m = _scan_log_for_missing(log_path)
            if m:
                missing[label] = m
        if not missing:
            return {"status": "clean", "message": "No missing nodes found. Full toolkit available."}

    # Custom node dependencies must go into the Documents venv -
    # that's what ComfyUI uses to load custom node Python code
    user = os.environ.get("USERNAME", "")
    docs_venv = os.path.join(
        os.environ.get("USERPROFILE", f"C:\\Users\\{user}"),
        "Documents", "ComfyUI", ".venv", "Scripts", "python.exe"
    )
    if not os.path.exists(docs_venv):
        docs_venv = _find_comfyui_python(CONFIG["comfy_image_dir"])
    log(f"[S32] Node install using Python: {docs_venv}")

    instance_map = {
        "image (8188)": {
            "custom_nodes": os.path.join(CONFIG["comfy_image_dir"], "custom_nodes"),
            "python": docs_venv
        }
    }

    report = []
    installed_any = False
    already_done = set()
    installed_titles = []  # track all installed node titles for verification

    for label, names in missing.items():
        if target and target not in label:
            continue
        info = instance_map.get(label)
        if not info:
            continue

        for name in names:
            # Check pip-installable nodes first
            top = name.split(".")[0].lower().replace("-","").replace("_","")
            pip_pkg = None
            for key, pkg in _PIP_NODES.items():
                if top in key.lower().replace("-","").replace("_",""):
                    pip_pkg = pkg
                    break

            if pip_pkg and pip_pkg not in already_done:
                already_done.add(pip_pkg)
                log(f"[S32] pip installing {pip_pkg} for {label}...")
                ok_r, msg = _pip_install_node(pip_pkg, info["python"])
                status = "✓" if ok_r else "✗"
                report.append(f"{status} [{label}] {pip_pkg} (pip)\n   {msg}")
                if ok_r:
                    installed_any = True
                    installed_titles.append(pip_pkg)
                continue

        # Registry lookup for git-cloned nodes
        matches = _registry_lookup(names)
        matched_names = {m["name"] for m in matches}
        unmatched = [n for n in names if n not in matched_names]

        for match in matches:
            if match["url"] in already_done:
                continue
            already_done.add(match["url"])
            log(f"[S32] Installing {match['title']} into {label}...")
            ok_r, msg = _install_node(match["url"], info["custom_nodes"], info["python"])
            status = "✓" if ok_r else "✗"
            report.append(f"{status} [{label}] {match['title']}\n   {msg}")
            if ok_r:
                installed_any = True
                installed_titles.append(match["title"])

        for name in unmatched:
            report.append(f"? [{label}] '{name}' - not in registry. Try ComfyUI Manager manually.")

    if installed_any:
        report.append("\n✓ Nodes installed. Restarting ComfyUI to load them...")
        state["missing_nodes"] = {}

        # Autonomously restart ComfyUI and verify nodes loaded
        def _restart_and_verify():
            import socket
            from s32_comfyui import start_comfy_image
            time.sleep(2)

            for port, start_fn, ready_key, log_name in [
                (8188, start_comfy_image, "comfy_image_ready", "system32_image.log"),

            ]:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.settimeout(1)
                        if s.connect_ex(("127.0.0.1", port)) != 0:
                            continue
                    # Kill by PID
                    r = subprocess.run(
                        ["netstat", "-ano"],
                        capture_output=True, text=True, timeout=10
                    )
                    for line in r.stdout.splitlines():
                        if f":{port}" in line and "LISTENING" in line:
                            pid = line.strip().split()[-1]
                            try:
                                subprocess.run(
                                    ["taskkill", "/F", "/PID", pid],
                                    capture_output=True, timeout=10
                                )
                                log(f"[S32] Killed ComfyUI on port {port} (PID {pid})")
                            except Exception:
                                pass
                            break
                    time.sleep(3)
                    state[ready_key] = False
                    threading.Thread(target=start_fn, daemon=True).start()
                    log(f"[S32] Relaunching ComfyUI on port {port}...")

                    # Wait for ComfyUI to come back up
                    host = f"http://127.0.0.1:{port}"
                    for _ in range(60):
                        time.sleep(3)
                        if is_up(host):
                            break

                    # Read log and verify nodes loaded
                    log_path = os.path.join(CONFIG["comfy_image_dir"], log_name)
                    time.sleep(5)  # give it a moment to finish writing
                    verified = []
                    failed = []
                    if os.path.exists(log_path):
                        try:
                            content = open(log_path, "r", encoding="utf-8", errors="ignore").read()
                            for title in installed_titles:
                                title_lower = title.lower().replace(" ", "").replace("-", "")
                                if "import failed" in content.lower():
                                    # Check if this specific node failed
                                    for line in content.splitlines():
                                        if "IMPORT FAILED" in line:
                                            line_clean = line.lower().replace(" ", "").replace("-", "")
                                            if title_lower in line_clean:
                                                failed.append(title)
                                                break
                                    else:
                                        verified.append(title)
                                else:
                                    verified.append(title)
                        except Exception:
                            pass

                    if verified:
                        log(f"[S32] ✓ Verified loaded: {', '.join(verified)}")
                        state["startup_notification"] = f"✓ Nodes installed and verified: {', '.join(verified)}"
                        state["install_failures"] = {}  # reset on success
                    if failed:
                        log(f"[S32] ✗ Still failing after install: {', '.join(failed)}")
                        # Track failures - stop retrying after 2 attempts per session
                        for f in failed:
                            state.setdefault("install_failures", {})[f] = \
                                state.get("install_failures", {}).get(f, 0) + 1
                        persistent = [f for f in failed
                                      if state.get("install_failures", {}).get(f, 0) >= 2]
                        if persistent:
                            log(f"[S32] Giving up on persistent failures: {persistent} - manual install required")
                            state["startup_notification"] = (state.get("startup_notification") or "") + \
                                f"\n✗ Persistent failures (giving up): {', '.join(persistent)} - manual pip install required"
                        else:
                            state["startup_notification"] = (state.get("startup_notification") or "") + \
                                f"\n✗ Still failing: {', '.join(failed)} - dependencies may need manual install"

                except Exception as e:
                    log(f"[S32] Restart/verify error on port {port}: {e}")

        threading.Thread(target=_restart_and_verify, daemon=True).start()
        report.append("ComfyUI is restarting - verification will complete in about 30 seconds.")
    elif not report:
        report.append("Nothing new to install.")

    return {
        "status": "done",
        "report": "\n".join(report)
    }



# Title keywords → (field_name, cast) for title-based patching
_TITLE_MAP = {
    # easy nodes use their own field names
    "positive":   ("positive", str),
    "negative":   ("negative", str),
    # primitives with value field
    "prompt":     ("value", str),
    "width":      ("value", int),
    "height":     ("value", int),
    "fps":        ("value", int),
    "frame rate": ("value", int),
    "duration":   ("value", int),
    "steps":      ("value", int),
    "cfg":        ("value", float),
    "seed":       ("value", int),
    "strength":   ("value", float),
    # checkpoint / lora
    "load checkpoint": ("ckpt_name", str),
    "checkpoint":      ("ckpt_name", str),
    "load lora":       ("lora_name", str),
    # clip text encode
    "clip text encode (prompt)": ("text", str),
}

# ── LM Studio launcher/manager — REMOVED (LM Studio retired; llama.cpp is the
#    sole backend). Server lifecycle now lives in start_llama_server() /
#    shutdown_llama_server() below. ──────────────────────────────────────


def _find_llama_server():
    """Find llama-server.exe in Sys 32's Home directory."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, "llama-server.exe"),
        os.path.join(script_dir, "llama.cpp", "llama-server.exe"),
        os.path.join(script_dir, "llamacpp", "llama-server.exe"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def read_gguf_caps(path):
    """Read a GGUF model file's headers and return what the model is actually
    capable of: {arch, max_ctx, n_layers}. Best-effort and safe — only the
    metadata header is read (a few KB), large arrays (e.g. the tokenizer) are
    seeked past rather than loaded, and any failure returns {} so the caller can
    fall back to static caps. Pure stdlib (struct)."""
    import struct
    # GGUF metadata value type enum
    T_UINT8, T_INT8, T_UINT16, T_INT16, T_UINT32, T_INT32, T_FLOAT32, \
        T_BOOL, T_STRING, T_ARRAY, T_UINT64, T_INT64, T_FLOAT64 = range(13)
    _FIXED = {T_UINT8: 1, T_INT8: 1, T_BOOL: 1, T_UINT16: 2, T_INT16: 2,
              T_UINT32: 4, T_INT32: 4, T_FLOAT32: 4, T_UINT64: 8, T_INT64: 8,
              T_FLOAT64: 8}
    _FMT = {T_UINT8: "<B", T_INT8: "<b", T_BOOL: "<B", T_UINT16: "<H",
            T_INT16: "<h", T_UINT32: "<I", T_INT32: "<i", T_FLOAT32: "<f",
            T_UINT64: "<Q", T_INT64: "<q", T_FLOAT64: "<d"}
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"GGUF":
                return {}
            struct.unpack("<I", f.read(4))[0]                 # version
            f.read(8)                                          # tensor_count
            kv_count = struct.unpack("<Q", f.read(8))[0]

            def rd_str():
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", "replace")

            def rd_scalar(vt):
                return struct.unpack(_FMT[vt], f.read(_FIXED[vt]))[0]

            def skip_value(vt):
                if vt == T_STRING:
                    n = struct.unpack("<Q", f.read(8))[0]; f.seek(n, 1)
                elif vt == T_ARRAY:
                    et = struct.unpack("<I", f.read(4))[0]
                    ln = struct.unpack("<Q", f.read(8))[0]
                    if et == T_STRING:
                        for _ in range(ln):
                            n = struct.unpack("<Q", f.read(8))[0]; f.seek(n, 1)
                    elif et in _FIXED:
                        f.seek(_FIXED[et] * ln, 1)
                    else:
                        raise ValueError("bad array elem type")
                elif vt in _FIXED:
                    f.seek(_FIXED[vt], 1)
                else:
                    raise ValueError("bad value type")

            meta = {}
            for _ in range(kv_count):
                key = rd_str()
                vt = struct.unpack("<I", f.read(4))[0]
                if vt == T_STRING:
                    meta[key] = rd_str()
                elif vt in _FIXED:
                    meta[key] = rd_scalar(vt)
                else:
                    skip_value(vt)            # arrays etc. — not needed, seek past

        arch = meta.get("general.architecture")
        out = {"arch": arch}
        if arch:
            mc = meta.get(f"{arch}.context_length")
            nl = meta.get(f"{arch}.block_count")
            if isinstance(mc, int) and mc > 0:
                out["max_ctx"] = mc
            if isinstance(nl, int) and nl > 0:
                out["n_layers"] = nl
        return out
    except Exception as e:
        log(f"[S32] GGUF caps read failed ({os.path.basename(path or '?')}): {str(e)[:80]}")
        return {}


def _test_image_b64():
    """A small, definitely-decodable PNG for the vision probe. A 1x1 image makes
    some llama.cpp builds throw 'failed to load image', so use a real 64x64."""
    try:
        from s32_config import PILImage, io as _io, base64 as _b64
        if PILImage is not None:
            img = PILImage.new("RGB", (64, 64), (128, 128, 128))
            buf = _io.BytesIO()
            img.save(buf, format="PNG")
            return _b64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    # Verified-valid 8x8 gray PNG fallback.
    return ("iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGNs"
            "aGhgwAaYsIoOWgkAMBcBkGHHl6YAAAAASUVORK5CYII=")


def _probe_vision(host, max_wait=180):
    """Determine whether the LIVE model accepts image input. Returns 'vision',
    'no_vision', or 'unknown'. Retries while the model is still loading. The key
    distinction: a text-only model answers 'image input is not supported', while
    a vision model accepts the image (success, or at worst a decode error) — both
    of which mean the projector is loaded."""
    img_b64 = _test_image_b64()
    payload = {
        "model": CONFIG.get("lmstudio_model", "local"),
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "."},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
        ]}],
        "max_tokens": 1, "temperature": 0, "stream": False,
    }
    deadline = time.time() + max_wait
    while True:
        try:
            http_post(f"{host}/v1/chat/completions", payload, timeout=30)
            return "vision"
        except Exception as e:
            body = (getattr(e, "s32_body", "") or "").lower()
            if "image input is not supported" in body or "provide the mmproj" in body:
                return "no_vision"               # text-only: no projector loaded
            if "failed to load" in body:
                return "vision"                  # image WAS accepted → projector is loaded
            if time.time() >= deadline:
                return "unknown"
            time.sleep(5)                        # model still loading / transient — wait & retry


def _kill_llama_on_port(port):
    """Kill whatever process is listening on the model port (our llama-server)."""
    try:
        r = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in r.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                log(f"[S32] Stopped stale llama-server PID {pid} on port {port}")
                return True
    except Exception as e:
        log(f"[S32] Could not stop server on port {port}: {e}")
    return False


def _vision_marker_path():
    try:
        return os.path.join(REFERENCE_DIR, ".vision_probe.json")
    except Exception:
        return None


def _read_vision_marker():
    p = _vision_marker_path()
    if p and os.path.exists(p):
        try:
            with open(p, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _write_vision_marker(model_path, mmproj_path, result):
    p = _vision_marker_path()
    if not p:
        return
    try:
        with open(p, "w") as f:
            json.dump({"model": os.path.basename(model_path or ""),
                       "mmproj": os.path.basename(mmproj_path or ""),
                       "result": result}, f)
    except Exception:
        pass


_flash_syntax = None     # cached: True = value-form (--flash-attn on), False = bare flag


def _flash_attn_args(server):
    """Return the correct flash-attn CLI args for THIS llama-server build.
    Newer builds require a value (`--flash-attn on`); older builds take a bare
    flag (`--flash-attn`). On a value-form build a lone `--flash-attn` swallows the
    NEXT argument as its value and the server exits on startup — exactly the
    `unknown value for --flash-attn: '--mmproj'` crash. Probed once via --help and
    cached for the session."""
    global _flash_syntax
    if _flash_syntax is None:
        try:
            r = subprocess.run([server, "--help"], capture_output=True, text=True,
                               timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
            help_text = (r.stdout or "") + (r.stderr or "")
            _flash_syntax = ("on|off" in help_text) or ("[on|off|auto]" in help_text)
        except Exception:
            _flash_syntax = True      # default to the modern value-form (current builds)
        log(f"[S32] flash-attn flag style: {'--flash-attn on' if _flash_syntax else '--flash-attn'}")
    return ["--flash-attn", "on"] if _flash_syntax else ["--flash-attn"]


def start_llama_server():
    """
    Start llama-server.exe with the configured model.
    Accepts any GGUF by file path — no catalog required.
    """
    if state.get("lmstudio_ready"):
        return
    host        = CONFIG.get("lmstudio_host", "http://localhost:1234")
    # Use explicit GGUF path if set, otherwise try lmstudio_model
    model_path = CONFIG.get("lmstudio_model_path", "") or CONFIG.get("lmstudio_model", "")
    ctx         = CONFIG.get("lmstudio_context_length", 131072)
    gpu_layers  = CONFIG.get("lmstudio_gpu_layers", 20)
    threads     = CONFIG.get("lmstudio_threads", 0)          # 0 → let llama.cpp auto-pick
    batch_size  = CONFIG.get("lmstudio_batch_size", 512)
    kv_cache    = CONFIG.get("lmstudio_kv_cache", "f16")     # f16 | q8_0 | q4_0
    flash_attn  = bool(CONFIG.get("lmstudio_flash_attn", False))

    # Override with per-model settings if available
    if model_path:
        _model_key = os.path.basename(model_path).lower()
        _settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_settings.json")
        if os.path.exists(_settings_file):
            try:
                import json as _json
                with open(_settings_file, "r") as _sf:
                    _ms = _json.load(_sf)
                if _model_key in _ms:
                    _s = _ms[_model_key]
                    ctx        = _s.get("ctx_size",   ctx)
                    gpu_layers = _s.get("gpu_layers", gpu_layers)
                    threads    = _s.get("threads",    threads)
                    batch_size = _s.get("batch_size", batch_size)
                    kv_cache   = _s.get("kv_cache",   kv_cache)
                    flash_attn = bool(_s.get("flash_attn", flash_attn))
                    log(f"[S32] Applied model settings: ctx={ctx} gpu_layers={gpu_layers} "
                        f"threads={threads} batch={batch_size} kv={kv_cache} fa={flash_attn}")
            except Exception:
                pass

    # ── Vision projector: if an mmproj GGUF sits next to the model, load it so
    # it can actually SEE images (attachments, screen frames). Without it the
    # model is text-only and ANY image sent to it returns HTTP 500 — so we gate
    # all vision on this flag. Auto-detected; or set lmstudio_mmproj_path.
    mmproj_path = CONFIG.get("lmstudio_mmproj_path", "")
    if not mmproj_path and model_path and os.path.exists(model_path):
        try:
            _mdir = os.path.dirname(model_path)
            for _f in sorted(os.listdir(_mdir)):
                if _f.lower().endswith(".gguf") and "mmproj" in _f.lower():
                    mmproj_path = os.path.join(_mdir, _f)
                    break
        except Exception:
            pass
    _has_mmproj = bool(mmproj_path and os.path.exists(mmproj_path))
    if _has_mmproj:
        log(f"[S32] Vision projector available: {os.path.basename(mmproj_path)}")
    else:
        log("[S32] No mmproj projector found — model will run text-only.")
    # vision_ready is set definitively by probing the live server once it's up —
    # a projector existing on disk does NOT mean the running server loaded it.
    state["vision_ready"] = False
    _port = int(host.split(":")[-1]) if ":" in host else 1234

    # If the model key is a catalog name not a path, can't use llama.cpp directly
    if model_path and not os.path.exists(model_path):
        log(f"[S32] llama-server: model path not found: {model_path}")
        log(f"[S32] Set lmstudio_model to a full GGUF path in config.json")
        state["model_load_failed"] = True
        state["lmstudio_ready"] = True
        return

    server = _find_llama_server()
    if not server:
        log("[S32] llama-server.exe not found — run bootstrap to download it")
        log("[S32] Download from: https://github.com/ggml-org/llama.cpp/releases")
        log("[S32] Place llama-server.exe in the same folder as system32.py")
        # Mark ready anyway so the UI starts — it'll show connection errors
        state["lmstudio_ready"] = True
        return

    # If already running, verify it responds
    if is_up(f"{host}/v1/models"):
        try:
            r = http_get(f"{host}/v1/models", timeout=5)
            if r.get("data"):
                _vstate = _probe_vision(host)
                if _vstate == "vision":
                    state["vision_ready"] = True
                    log("[S32] llama-server already running — vision confirmed (mmproj loaded).")
                    state["lmstudio_ready"] = True
                    __import__("s32_config").adopt_port_owner(_port, "llama-server")
                    return
                if _vstate == "no_vision" and _has_mmproj:
                    _mk = _read_vision_marker()
                    _already_tried = (_mk.get("mmproj") == os.path.basename(mmproj_path)
                                      and _mk.get("model")  == os.path.basename(model_path)
                                      and _mk.get("result") != "vision")
                    if _already_tried:
                        # We've launched with this exact projector before and it
                        # still didn't give vision — don't reload every boot.
                        state["vision_ready"] = False
                        log("[S32] Projector present but a prior load didn't enable vision — "
                            "running text-only. Delete .vision_probe.json to retry.")
                        state["lmstudio_ready"] = True
                        __import__("s32_config").adopt_port_owner(_port, "llama-server")
                        return
                    # Stale server started before the projector was wired in. Restart
                    # it so the operator's image input actually works this session.
                    log("[S32] Server running WITHOUT vision but a projector is available — "
                        "restarting llama-server to load it (one-time model reload)...")
                    _kill_llama_on_port(_port)
                    time.sleep(2)
                    # fall through to a fresh launch below (now with --mmproj)
                else:
                    # Text-only model (no projector) or a probe we couldn't read —
                    # keep the running server; just don't promise vision.
                    state["vision_ready"] = (_vstate == "vision")
                    log(f"[S32] llama-server already running "
                        f"(vision {'on' if state['vision_ready'] else 'off'}).")
                    state["lmstudio_ready"] = True
                    __import__("s32_config").adopt_port_owner(_port, "llama-server")
                    return
        except Exception:
            pass
        # Kill stale server
        try:
            subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
        except Exception:
            pass

    if not model_path:
        log("[S32] No model path configured. Set lmstudio_model in config.json.")
        state["model_load_failed"] = True
        state["lmstudio_ready"] = True
        return

    port = int(host.split(":")[-1]) if ":" in host else 1234
    log(f"[S32] Starting llama-server: {os.path.basename(model_path)}")
    log(f"[S32] Context: {ctx} | GPU layers: {gpu_layers} | Port: {port}")

    _llama_log = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llama_server.log")
    _lf = open(_llama_log, "w", encoding="utf-8", errors="replace")
    _args = [server,
             "--model",        model_path,
             "--ctx-size",     str(ctx),
             "--n-gpu-layers",  str(gpu_layers),
             "--port",         str(port),
             "--host",         "127.0.0.1",
             "--parallel",     "1",
             "--batch-size",   str(batch_size),
             "--mmap",
            ]
    if threads and int(threads) > 0:
        _args += ["--threads", str(int(threads))]
    # KV-cache quantization — the biggest VRAM saver at long context. llama.cpp
    # requires flash attention for a quantized KV cache, so force it on when the
    # cache type isn't full-precision f16.
    if kv_cache and kv_cache != "f16":
        _args += ["--cache-type-k", str(kv_cache), "--cache-type-v", str(kv_cache)]
        flash_attn = True
    if flash_attn:
        _args += _flash_attn_args(server)
    if mmproj_path and os.path.exists(mmproj_path):
        _args += ["--mmproj", mmproj_path]
        log(f"[S32] Loading vision projector: {os.path.basename(mmproj_path)}")

    # ── Single-instance guard ────────────────────────────────────────────────
    # Stamp this start with a generation number and tear down ANY existing
    # llama-server on the port before launching, so we never stack two servers
    # fighting over the port + VRAM (the "two loads at once" bug). If a newer
    # start supersedes this one mid-wait, this wait loop exits quietly instead
    # of falsely reporting a crash.
    my_gen = state.get("llama_gen", 0) + 1
    state["llama_gen"] = my_gen
    _kill_llama_on_port(port)
    time.sleep(1)                       # let the port + VRAM free before relaunch

    from s32_config import CREATE_SUSPENDED, tie_to_s32
    proc = subprocess.Popen(
        _args,
        creationflags=subprocess.CREATE_NO_WINDOW | (CREATE_SUSPENDED if os.name == "nt" else 0),
        stdout=_lf,
        stderr=_lf,
    )
    tie_to_s32(proc, "llama-server")      # ends with System 32, however it closes

    def _log_tail(path, n=1400):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[-n:]
        except Exception:
            return ""

    # Wait for server to become ready
    for i in range(60):
        time.sleep(3)
        if state.get("llama_gen") != my_gen:            # a newer load took over
            log("[S32] llama-server load superseded by a newer request — abandoning this wait.")
            return
        if proc.poll() is not None:                     # process exited = it crashed
            tail = _log_tail(_llama_log)
            log(f"[S32] ERROR: llama-server exited (code {proc.returncode}) before becoming ready.")
            if tail:
                log("[S32] llama-server.log (tail):\n" + tail)
            state["model_switching"] = False
            state["startup_notification"] = (
                f"⚠ Model failed to load: {os.path.basename(model_path)}. "
                f"llama-server exited on startup — usually the context length or KV-cache "
                f"setting needs more VRAM than is free, or a launch flag isn't supported by "
                f"this build. Click the SYSTEM 32 logo to lower CONTEXT or change KV CACHE. "
                f"(Full error in llama_server.log.)"
            )
            state["lmstudio_ready"] = True              # let the UI come up so settings can be fixed
            return
        if is_up(f"{host}/v1/models"):
            time.sleep(2)  # let the model finish loading weights before probing
            _vs = _probe_vision(host)
            state["vision_ready"] = (_vs == "vision")
            _write_vision_marker(model_path, mmproj_path if _has_mmproj else "", _vs)
            log(f"[S32] llama-server ready (vision {'on' if state['vision_ready'] else 'off'}).")
            state["lmstudio_ready"] = True
            state["model_switching"] = False
            return
        if i % 5 == 0:
            log(f"[S32] Waiting for llama-server... ({i*3}s)")

    # Timeout — shut down and report clearly (unless superseded)
    if state.get("llama_gen") != my_gen:
        return
    log("[S32] ERROR: llama-server timed out. The model may require different settings.")
    try:
        shutdown_llama_server()
    except Exception:
        pass
    state["model_switching"] = False
    state["startup_notification"] = (
        f"⚠ Model failed to load: {os.path.basename(model_path)}. "
        f"Click the SYSTEM 32 logo to select a different model or adjust settings."
    )
    # Allow UI to start so user can use the switcher
    state["lmstudio_ready"] = True


def shutdown_llama_server():
    """Stop llama-server.exe."""
    host = CONFIG.get("lmstudio_host", "http://localhost:1234")
    port = host.split(":")[-1] if ":" in host else "1234"
    try:
        r = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in r.stdout.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.strip().split()[-1]
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                log(f"[S32] llama-server stopped (PID {pid})")
                break
    except Exception as e:
        log(f"[S32] Could not stop llama-server: {e}")


def detect_hardware():
    """
    Detect GPU VRAM, system RAM, and GPU name.
    Returns dict with vram_mb, ram_mb, gpu_name, tier.
    Tier: 'high' (16GB+ VRAM), 'mid' (8-16GB), 'low' (4-8GB), 'minimal' (<4GB)
    """
    hw = {"vram_mb": 0, "ram_mb": 0, "gpu_name": "Unknown", "tier": "mid"}

    # Detect system RAM
    try:
        import psutil
        hw["ram_mb"] = psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        pass

    # Try torch first (most accurate for CUDA)
    try:
        import subprocess as _sp
        r = _sp.run(
            [CONFIG.get("comfy_image_python", "python"), "-c",
             "import torch; d=torch.cuda.get_device_properties(0); "
             "print(d.name, d.total_memory//(1024*1024))"],
            capture_output=True, text=True, timeout=15
        )
        if r.returncode == 0:
            parts = r.stdout.strip().rsplit(" ", 1)
            if len(parts) == 2:
                hw["gpu_name"] = parts[0]
                hw["vram_mb"] = int(parts[1])
    except Exception:
        pass

    # Fallback - try nvidia-smi
    if hw["vram_mb"] == 0:
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    parts = line.split(", ")
                    if len(parts) == 2:
                        hw["gpu_name"] = parts[0].strip()
                        hw["vram_mb"] = int(parts[1].strip())
                        break
        except Exception:
            pass

    # Determine tier
    vram = hw["vram_mb"]
    if vram >= 16000:
        hw["tier"] = "high"
    elif vram >= 8000:
        hw["tier"] = "mid"
    elif vram >= 4000:
        hw["tier"] = "low"
    else:
        hw["tier"] = "minimal"

    log(f"[S32] Hardware: {hw['gpu_name']} | VRAM: {hw['vram_mb']}MB | RAM: {hw['ram_mb']}MB | Tier: {hw['tier']}")
    return hw

def hardware_context_length(hw):
    """Return the best context length for this machine's VRAM.
    Uses LM Studio's configured context if available, otherwise hardware tier defaults.
    """
    # Check if LM Studio has a higher context configured
    host = CONFIG.get("lmstudio_host", "http://localhost:1234")
    try:
        r = http_get(f"{host}/v1/models", timeout=3)
        for m in r.get("data", []):
            ctx = m.get("context_length", 0) or 0
            if ctx > 0:
                return ctx
    except Exception:
        pass
    # Fall back to hardware tier defaults
    tier = hw.get("tier", "mid")
    return {
        "high":    131072,
        "mid":     65536,
        "low":     16384,
        "minimal": 8192,
    }.get(tier, 32768)

def _get_machine_preferences():
    """Read operator preferences from the machine profile. Returns dict."""
    profile = _load_machine(_machine_name)
    if not profile:
        return {}
    prefs = {}
    import re
    in_prefs = False
    for line in profile.splitlines():
        if "## Operator Preferences" in line:
            in_prefs = True
            continue
        elif line.startswith("## ") and in_prefs:
            break
        if in_prefs and ":" in line:
            # Parse "- key: value" or "key: value"
            line = line.lstrip("- ").strip()
            if ":" in line:
                k, _, v = line.partition(":")
                prefs[k.strip().lower()] = v.strip()
    return prefs

def hardware_workflow_defaults(hw):
    """Return workflow defaults - hardware tier first, machine preferences override."""
    tier = hw.get("tier", "mid")
    base = {
        "high":    {"steps": 64, "width": 1024, "height": 1024, "cfg": 8},
        "mid":     {"steps": 40, "width": 1024, "height": 1024, "cfg": 7},
        "low":     {"steps": 25, "width": 768,  "height": 768,  "cfg": 7},
        "minimal": {"steps": 20, "width": 512,  "height": 512,  "cfg": 7},
    }.get(tier, {"steps": 30, "width": 768, "height": 768, "cfg": 7})

    # Apply machine preferences on top - operator always wins
    prefs = _get_machine_preferences()
    for key in ("steps", "width", "height", "cfg"):
        if key in prefs:
            try:
                base[key] = int(prefs[key])
            except ValueError:
                pass
    if "video_width" in prefs:
        try:
            base["video_width"] = int(prefs["video_width"])
        except ValueError:
            pass
    if "video_height" in prefs:
        try:
            base["video_height"] = int(prefs["video_height"])
        except ValueError:
            pass

    return base

def set_preference(key, value, scope="machine", extra=None):
    """
    Save operator preferences to the machine or operator profile.
    Pass extra={key:value,...} to save multiple preferences in one call.
    scope='machine' saves to machines/<this computer>.md
    scope='operator' saves to the current person's operator profile
    """
    _ensure_memory_structure()

    # Build full dict of all preferences to save
    all_prefs = {key: str(value)}
    if extra and isinstance(extra, dict):
        all_prefs.update({k: str(v) for k, v in extra.items()})

    if scope == "machine":
        profile = _load_machine(_machine_name) or f"""# Machine: {_machine_name}

## Primary Operator
{state.get("operator") or "(not introduced yet)"}

## Operator Preferences

## Notes
"""
        import re
        lines = profile.splitlines()
        new_lines = []
        in_prefs = False
        prefs_end_idx = None

        # First pass - update existing keys and find prefs section end
        updated_keys = set()
        for i, line in enumerate(lines):
            if "## Operator Preferences" in line:
                in_prefs = True
                new_lines.append(line)
                continue
            elif line.startswith("## ") and in_prefs:
                in_prefs = False
                prefs_end_idx = len(new_lines)
            if in_prefs:
                matched = False
                for k, v in all_prefs.items():
                    pattern = rf"^-?\s*{re.escape(k)}\s*:"
                    if re.match(pattern, line.strip()):
                        new_lines.append(f"- {k}: {v}")
                        updated_keys.add(k)
                        matched = True
                        break
                if not matched and "*(None recorded yet)*" not in line:
                    new_lines.append(line)
            else:
                new_lines.append(line)

        # Second pass - add any keys that weren't updated
        new_keys = {k: v for k, v in all_prefs.items() if k not in updated_keys}
        if new_keys:
            # Find the end of Operator Preferences section and insert there
            final = []
            in_prefs = False
            inserted = False
            for line in new_lines:
                if "## Operator Preferences" in line:
                    in_prefs = True
                    final.append(line)
                    continue
                if in_prefs and line.startswith("## ") and not inserted:
                    for k, v in new_keys.items():
                        final.append(f"- {k}: {v}")
                    inserted = True
                    in_prefs = False
                final.append(line)
            if not inserted:
                for k, v in new_keys.items():
                    final.append(f"- {k}: {v}")
            new_lines = final

        # Update notes if provided
        if "notes" in all_prefs:
            note_val = all_prefs.pop("notes")
            final = []
            in_notes = False
            notes_updated = False
            for line in new_lines:
                if "## Notes" in line:
                    in_notes = True
                    final.append(line)
                    final.append(note_val)
                    notes_updated = True
                    continue
                if in_notes and line.startswith("## "):
                    in_notes = False
                if in_notes and "*(Waiting" in line:
                    continue  # skip old placeholder
                if not in_notes:
                    final.append(line)
            new_lines = final

        _save_machine(_machine_name, "\n".join(new_lines))
        saved = list(all_prefs.keys())
        log(f"[S32] Machine preferences saved: {saved}")
        return {"status": "saved", "keys": saved, "scope": f"machine:{_machine_name}"}

    elif scope == "operator" and state.get("operator"):
        _opn = state["operator"]
        op = _load_operator(_opn)
        if op:
            content = op["content"]
            for k, v in all_prefs.items():
                content += f"\n- {k}: {v}\n"
            _save_operator(_opn, content)
            return {"status": "saved", "keys": list(all_prefs.keys()), "scope": f"operator:{_opn}"}

    return {"error": f"Unknown scope: {scope}"}

# ── Detected at startup, used throughout ──────────────────────────────────────
_hw = None

def get_hardware():
    global _hw
    if _hw is None:
        _hw = detect_hardware()
    return _hw

# ── ComfyUI ───────────────────────────────────────────────────────────────────

