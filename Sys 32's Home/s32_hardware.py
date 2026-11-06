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

# ── Node name → package (2026-11-05) ─────────────────────────────────────────
# A workflow names NODES ("easy positive"); the registry above lists PACKAGES by title
# ("ComfyUI Easy Use"), so matching names against titles found nothing. ComfyUI-Manager
# also publishes extension-node-map.json: every package's URL → the exact node names it
# provides. That's what ComfyUI-Manager itself uses to install missing nodes.
_NODE_MAP_URL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/extension-node-map.json"
_STATS_URL    = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/github-stats.json"
_CORE_URL     = "https://github.com/comfyanonymous/ComfyUI"
_node_map_cache = None
_stars_cache = None


def _load_stars():
    """{package URL: GitHub stars} from ComfyUI-Manager's stats — {} if it can't be had."""
    global _stars_cache
    if _stars_cache is not None:
        return _stars_cache
    try:
        req = urllib.request.Request(_STATS_URL, headers={"User-Agent": "System32/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        _stars_cache = {u.rstrip("/").lower(): int((v or {}).get("stars") or 0) for u, v in data.items()}
    except Exception as e:
        log(f"[S32] Could not load package star counts ({e}) — choosing without them")
        _stars_cache = {}
    return _stars_cache


def _load_node_map():
    """{node name: [package URLs]} + [(regex, URL)] — fetched once per session."""
    global _node_map_cache
    if _node_map_cache is not None:
        return _node_map_cache
    try:
        import s32_config as _cfg
        if _cfg.airplane():
            return {}, []
        req = urllib.request.Request(_NODE_MAP_URL, headers={"User-Agent": "System32/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
        names, patterns, sets = {}, [], {}
        for url, v in data.items():
            if not (isinstance(v, list) and v):
                continue
            sets[url] = {str(n) for n in (v[0] or [])}
            for n in (v[0] or []):
                names.setdefault(str(n), []).append(url)
            meta = v[1] if len(v) > 1 and isinstance(v[1], dict) else {}
            if meta.get("nodename_pattern"):
                try:
                    patterns.append((re.compile(meta["nodename_pattern"]), url))
                except re.error:
                    pass
        _node_map_cache = (names, patterns, sets)
        log(f"[S32] Node name map loaded: {len(names)} node names in {len(data)} packages")
        return _node_map_cache
    except Exception as e:
        log(f"[S32] Could not load the node name map: {e}")
        return {}, [], {}


def _node_map_lookup(missing_names):
    """Exact node names → [{name, title, url}] (one entry per package). Several packages
    can claim the same name — bundles and copies include another pack's nodes wholesale
    (VideoHelperSuite's nodes are also inside a 603-node bundle; ComfyUI_essentials'
    'MaskBlur+' inside a 4-star toolkit). The ORIGINAL is chosen by GitHub stars
    (ComfyUI-Manager's own stats: VideoHelperSuite 1908 vs the bundle's 120), then the
    curated list, then the pack covering more of this workflow's missing nodes.
    A node ComfyUI ITSELF provides is never installed from a copy: it's missing because
    this ComfyUI is older than the workflow → {"core": True} so the caller says so."""
    names, patterns, sets = _load_node_map()
    if not names and not patterns:
        return []
    curated = {}
    for e in _load_registry() or []:
        u = (e.get("reference") or "").rstrip("/").lower()
        if u:
            curated[u] = e.get("title") or u.split("/")[-1]
    out, seen = [], {}
    for name in missing_names:
        urls = list(names.get(name, []))
        if not urls:
            urls = [u for rx, u in patterns if rx.search(name)]
        if any(u.rstrip("/").lower() == _CORE_URL.lower() for u in urls):
            out.append({"name": name, "core": True, "names": [name], "url": "", "title": "ComfyUI"})
            continue
        urls = [u for u in urls if u.lower().startswith("https://github.com/")]
        if not urls:
            continue
        if len(urls) > 1:
            stars = _load_stars()
            def _rank(u):
                mine = sets.get(u, set()) or {name}
                return (-stars.get(u.rstrip("/").lower(), 0), u.rstrip("/").lower() not in curated,
                        -len(mine & set(missing_names)), len(mine))
            urls.sort(key=_rank)
        url = urls[0]
        if url in seen:
            continue
        seen[url] = True
        out.append({"name": name, "url": url,
                    "title": curated.get(url.rstrip("/").lower(), url.rstrip("/").split("/")[-1]),
                    "names": [n for n in missing_names if n in sets.get(url, ())] or [name]})
    return out


def _torch_guard_env(python_exe, custom_nodes_dir):
    """A node pack's requirements (or its install.py) must never swap ComfyUI's PyTorch —
    a plain 'torch' from PyPI is the CPU build and the graphics card stops being used
    (2026-11-05). pip honours PIP_CONSTRAINT for every install it runs, so the torch set
    that's installed now is pinned for the whole node install. A pack that truly needs
    another torch then fails its pip step (reported) instead of breaking ComfyUI."""
    env = os.environ.copy()
    try:
        r = subprocess.run(
            [python_exe, "-c",
             "import importlib.metadata as m\n"
             "for p in ('torch','torchvision','torchaudio'):\n"
             "    try: print(p + '==' + m.version(p))\n"
             "    except Exception: pass"],
            capture_output=True, text=True, timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pins = [l.strip() for l in (r.stdout or "").splitlines() if "==" in l]
        if pins:
            cfile = os.path.join(os.path.dirname(os.path.abspath(custom_nodes_dir)), ".s32_torch_pins.txt")
            with open(cfile, "w", encoding="utf-8") as f:
                f.write("\n".join(pins) + "\n")
            # As a file:// URL: pip splits PIP_CONSTRAINT on spaces, so a plain path like
            # "I:\Sys 32's Home\..." became "I:\Sys" → 'Could not open requirements file'
            # and the pack's dependencies were never installed (2026-11-05). The URL has none.
            import pathlib
            env["PIP_CONSTRAINT"] = pathlib.Path(cfile).resolve().as_uri()
            log(f"[S32] Node install: PyTorch kept as is ({', '.join(pins)})")
    except Exception as e:
        log(f"[S32] Node install: couldn't read the PyTorch version ({e}) — installing without the pin")
    return env


# A node that imports a Python package only when it RUNS (Easy-Use's background remover →
# transparent_background) fails mid-workflow with "No module named 'x'" (2026-11-05).
# The import name usually is the pip name; these are the common exceptions.
_MODULE_PIP = {
    "cv2": "opencv-python", "PIL": "pillow", "skimage": "scikit-image", "sklearn": "scikit-learn",
    "yaml": "pyyaml", "bs4": "beautifulsoup4", "Crypto": "pycryptodome", "dateutil": "python-dateutil",
    "fitz": "pymupdf", "attr": "attrs", "git": "GitPython", "google": "protobuf",
    "segment_anything": "segment-anything", "mmcv": "mmcv", "lap": "lap", "sam2": "sam2",
    "pkg_resources": "setuptools", "OpenGL": "PyOpenGL", "win32api": "pywin32", "win32con": "pywin32",
    "dotenv": "python-dotenv", "magic": "python-magic", "jwt": "PyJWT", "serial": "pyserial",
    "zmq": "pyzmq", "usb": "pyusb", "docx": "python-docx", "pptx": "python-pptx",
}
_NEVER_PIP = {"torch", "torchvision", "torchaudio", "comfy", "folder_paths", "nodes", "server",
              "execution", "comfy_extras", "custom_nodes", "xformers"}


def install_python_module(module):
    """A node couldn't import a Python package while running → install it in the background
    (tool_install_missing_nodes' worker: ComfyUI stopped meanwhile, then restarted).
    Returns {"started": pip_name} | {"error": ...}."""
    import s32_config as _cfg
    top = str(module or "").split(".")[0].strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,60}", top):
        return {"error": f"'{module}' doesn't look like a Python package name"}
    if top in _NEVER_PIP:
        return {"error": f"'{top}' is part of ComfyUI / PyTorch itself — it can't be pip-installed "
                         f"on its own"}
    if _cfg.airplane():
        return {"error": "airplane mode is on, so it can't be downloaded"}
    pkg = _MODULE_PIP.get(top, top.replace("_", "-"))
    if state.get("node_install_worker"):
        return {"started": pkg, "busy": True}
    r = tool_install_missing_nodes("image", modules=[top])
    if r.get("status") == "installing":
        return {"started": pkg}
    return {"error": r.get("report") or r.get("message") or "nothing to install"}


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
    if os.path.exists(dest):
        # Already there: NOT updated (a newer pack can need a newer ComfyUI than the pinned
        # one), but its dependencies ARE checked — a pack whose pip step failed before
        # stayed broken forever because this path returned early (2026-11-05).
        msgs = [f"Already installed: {folder_name}"]
    else:
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
    _penv = _torch_guard_env(python_exe, custom_nodes_dir)

    # pip install requirements.txt if present
    req_file = os.path.join(dest, "requirements.txt")
    if os.path.exists(req_file):
        try:
            r2 = subprocess.run(
                [python_exe, "-m", "pip", "install", "-r", req_file,
                 "--quiet", "--break-system-packages"],
                capture_output=True, text=True, timeout=300, env=_penv
            )
            if r2.returncode == 0:
                msgs.append("Dependencies installed.")
            else:
                _err = "\n".join(l for l in (r2.stderr or "").splitlines()
                                 if l.strip() and not l.startswith("[notice]"))
                msgs.append(f"pip warnings: {_err[:300]}")
        except subprocess.TimeoutExpired:
            msgs.append("pip install timed out.")

    # run install.py if present
    install_script = os.path.join(dest, "install.py")
    if os.path.exists(install_script):
        try:
            r3 = subprocess.run(
                [python_exe, install_script],
                capture_output=True, text=True, timeout=120, cwd=dest, env=_penv
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

_MODULE_ERR_RE = re.compile(r"(?:No module named|ModuleNotFoundError[^']*)\s*'([A-Za-z_][A-Za-z0-9_.]*)'")


def _scan_log_for_modules(log_path):
    """Python packages ComfyUI's latest run couldn't import ('No module named x') — these
    go to pip, never to a node pack with a similar title (2026-11-05: 'transparent_background'
    pulled in an unrelated 'Transparent-background-comfyUI' pack)."""
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception:
        return []
    cut = max((text.rfind(mk) for mk in ("Total VRAM", "ComfyUI startup time", "Set vram state",
                                          "Starting server", "** Platform:")), default=-1)
    text = text[cut:] if cut >= 0 else "\n".join(text.splitlines()[-400:])
    return sorted({m.group(1).split(".")[0] for m in _MODULE_ERR_RE.finditer(text)
                   if m.group(1).split(".")[0] not in _NEVER_PIP})


def _pip_into_comfy(pkgs, python_exe, cn):
    """pip-install packages into ComfyUI's venv, PyTorch pinned. → (ok_list, fail_list)."""
    done, failed = [], []
    for pkg in pkgs:
        state["installing"] = {"what": f"the Python package {pkg}", "started": time.time()}
        log(f"[S32][MODELS] installing the Python package {pkg} into ComfyUI's Python")
        try:
            r = subprocess.run([python_exe, "-m", "pip", "install", pkg, "--quiet"],
                               capture_output=True, text=True, timeout=900,
                               env=_torch_guard_env(python_exe, cn),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                done.append(pkg)
                log(f"[S32][MODELS] ✓ installed {pkg}")
            else:
                err = "\n".join(l for l in (r.stderr or "").splitlines()
                                if l.strip() and not l.startswith("[notice]"))[-300:]
                failed.append(f"{pkg}: {err[:200]}")
                log(f"[S32][MODELS] pip install {pkg} failed: {err}")
        except Exception as e:
            failed.append(f"{pkg}: {e}")
    return done, failed


def tool_install_missing_nodes(target=None, modules=None):
    """
    Install what a workflow is missing: node packs (by exact node name) and Python packages
    a node couldn't import. Planned here; the work runs in the background (2026-11-05):
    ComfyUI is STOPPED while installing (on Windows it locks its packages' files — pip got
    '[WinError 5] Access is denied: …cv2.pyd'), started again, and the new nodes checked.
    The input-area bar shows each step. target: 'image' or None.
    """
    import s32_config as _cfg
    if _cfg.airplane():
        return {"status": "airplane", "message": _cfg.AIRPLANE_MSG,
                "report": "Airplane mode is on — custom nodes can't be downloaded or installed."}
    if state.get("node_install_worker"):
        return {"status": "busy", "report": "Already installing — the bar below the chat shows it."}
    image_log = os.path.join(CONFIG["comfy_image_dir"], "system32_image.log")
    if modules:                                    # just these Python packages
        missing, modules = {}, {m for m in modules if m not in _NEVER_PIP}
    else:
        missing = {k: list(v) for k, v in (state.get("missing_nodes") or {}).items()}
        modules = set()
    if not missing and not modules:
        modules = set(_scan_log_for_modules(image_log))
        m = [n for n in _scan_log_for_missing(image_log) if n.split(".")[0] not in modules]
        if m:
            missing["image (8188)"] = m
    if not missing and not modules:
        return {"status": "clean", "message": "No missing nodes found. Full toolkit available."}

    docs_venv = _find_comfyui_python(CONFIG["comfy_image_dir"])
    cn = os.path.join(CONFIG["comfy_image_dir"], "custom_nodes")
    log(f"[S32] Node install using Python: {docs_venv}")

    report, plan_nodes, plan_pip = [], [], []
    seen = set()
    for label, names in missing.items():
        if target and target not in label:
            continue
        for name in names:                         # the few pip-only 'nodes'
            top = name.split(".")[0].lower().replace("-", "").replace("_", "")
            for key, pkg in _PIP_NODES.items():
                if top in key.lower().replace("-", "").replace("_", "") and pkg not in seen:
                    seen.add(pkg); plan_pip.append(pkg)
                    break
        matches = _node_map_lookup(names)
        core = [m["name"] for m in matches if m.get("core")]
        matches = [m for m in matches if not m.get("core")]
        for n in core:
            report.append(f"? [{label}] '{n}' is one of ComfyUI's own nodes, and this ComfyUI "
                          f"version doesn't have it — the workflow was made with a newer ComfyUI.")
        covered = {n for m in matches for n in m.get("names", [m["name"]])} | set(core)
        rest = [n for n in names if n not in covered]
        if rest:                                   # old title matching: only for node-like names
            matches += [m for m in _registry_lookup(rest)
                        if m["url"] not in {x["url"] for x in matches}]
            covered |= {m["name"] for m in matches}
        for m in matches:
            if m["url"] not in seen:
                seen.add(m["url"]); plan_nodes.append(m)
        for name in names:
            if name not in covered:
                report.append(f"? [{label}] '{name}' - no package found that provides this node. "
                              f"Search its name on github.com, or install it with ComfyUI Manager.")
    for mod in sorted(modules):
        pkg = _MODULE_PIP.get(mod, mod.replace("_", "-"))
        if pkg not in seen:
            seen.add(pkg); plan_pip.append(pkg)

    if not plan_nodes and not plan_pip:
        state["installing"] = None
        if report:
            state["startup_notification"] = "⚠ Couldn't install the workflow's missing nodes:\n" + \
                "\n".join(r.split("] ", 1)[-1] for r in report if r.startswith("?"))
        else:
            report.append("Nothing new to install.")
        return {"status": "done", "report": "\n".join(report)}

    what = ", ".join([m["title"] for m in plan_nodes] + plan_pip)

    def _work():
        from s32_comfyui import comfy_stopped
        state["node_install_worker"] = True
        installed, failed_msgs = [], []
        try:
            with comfy_stopped(what):
                for m in plan_nodes:
                    _for = ", ".join(m.get("names") or [m["name"]])
                    log(f"[S32] Installing {m['title']} (for: {_for})...")
                    state["installing"] = {"what": f"{m['title']} (nodes)", "started": time.time()}
                    ok_r, msg = _install_node(m["url"], cn, docs_venv)
                    log(f"[S32][MODELS] {'✓' if ok_r else '✗'} {m['title']}: {msg[:300]}")
                    if ok_r and "pip warnings" not in msg:
                        installed.append(m["title"])
                    elif ok_r:
                        installed.append(m["title"])
                        failed_msgs.append(f"{m['title']} — some of its dependencies didn't install: "
                                           f"{msg.split('pip warnings:', 1)[-1].strip()[:180]}")
                    else:
                        failed_msgs.append(f"{m['title']} — {msg[:180]}")
                ok_p, bad_p = _pip_into_comfy(plan_pip, docs_venv, cn)
                installed += ok_p
                failed_msgs += bad_p
                state["installing"] = {"what": "ComfyUI (loading what was installed)",
                                       "started": time.time(), "restart": True}
            # ComfyUI is started again by comfy_stopped. Check the node packs loaded.
            verified, broken = [], []
            try:
                content = open(image_log, "r", encoding="utf-8", errors="ignore").read()
                cut = max((content.rfind(mk) for mk in ("Total VRAM", "Starting server", "** Platform:")), default=-1)
                content = content[cut:] if cut >= 0 else content
            except Exception:
                content = ""
            for title in [m["title"] for m in plan_nodes if m["title"] in installed]:
                key = title.lower().replace(" ", "").replace("-", "").replace("_", "")
                bad = any("import failed" in ln.lower() and key in ln.lower().replace(" ", "").replace("-", "").replace("_", "")
                          for ln in content.splitlines())
                (broken if bad else verified).append(title)
            verified += [p for p in plan_pip if p in installed]
            state["missing_nodes"] = {}
            if verified and not broken and not failed_msgs:
                log(f"[S32] ✓ Verified loaded: {', '.join(verified)}")
                state["startup_notification"] = (f"✓ Installed {', '.join(verified)} and restarted ComfyUI "
                                                 f"— the workflow can run now. Ask again to run it.")
            else:
                lines = []
                if verified:
                    lines.append(f"✓ Installed {', '.join(verified)}.")
                if broken:
                    log(f"[S32] ✗ Still failing after install: {', '.join(broken)}")
                    lines.append(f"✗ {', '.join(broken)} didn't load (see system32_image.log).")
                lines += [f"✗ {f}" for f in failed_msgs]
                state["startup_notification"] = "\n".join(lines) + "\nComfyUI has restarted."
        except Exception as e:
            log(f"[S32] Node install failed: {e}")
            state["startup_notification"] = f"⚠ Installing {what} failed: {e}"
        finally:
            state["installing"] = None
            state["node_install_worker"] = False

    threading.Thread(target=_work, daemon=True, name="s32-node-install").start()
    report.insert(0, f"Installing {what} — ComfyUI stops while it installs, then restarts "
                     f"(the bar below the chat shows it).")
    return {"status": "installing", "report": "\n".join(report)}



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


# ── Model loading bar (2026-11-02) ───────────────────────────────────────────
# llama-server gives no percentage while it loads, so System 32 times each load and
# remembers it (per computer + model + settings — the same drive loads at different
# speeds on different machines). The input-area bar fills against that time; the very
# first load of a model on a computer has nothing to go on, so its bar just moves.
# Kept in reference material: local to this drive, never in the repo.
def _load_times_path():
    return os.path.join(REFERENCE_DIR or os.path.dirname(os.path.abspath(__file__)),
                        "model_load_times.json")


def _load_key(model_path, ctx, gpu_layers, kv_cache, mmproj):
    return "|".join([str(_machine_name or "?"), os.path.basename(model_path).lower(),
                     str(ctx), str(gpu_layers), str(kv_cache or "f16"), "vision" if mmproj else ""])


def _expected_load(key):
    try:
        with open(_load_times_path(), encoding="utf-8") as f:
            times = json.load(f).get(key) or []
        return sum(times) / len(times) if times else 0
    except Exception:
        return 0


def _remember_load(key, secs):
    try:
        p = _load_times_path()
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
        d[key] = ((d.get(key) or []) + [round(secs, 1)])[-3:]     # the last 3 loads
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except Exception:
        pass


# ── Graphics cards (2026-11-02) ──────────────────────────────────────────────
# llama-server's graphics backends are add-on DLLs next to it: ggml-cuda.dll (NVIDIA) and
# ggml-vulkan.dll (Vulkan — AMD, and NVIDIA again). With the Vulkan add-on present, an
# NVIDIA card is listed TWICE (CUDA0 and VulkanN), so System 32 always names the cards.
# Per model, the operator switches each card on or off (model panel → GRAPHICS CARDS); the
# setting is the NAMES of the cards that are on ("gpus_on"), so it travels with the drive:
#   no setting yet / none of the names on this computer → the single best card (NVIDIA via
#                      CUDA, else a discrete Vulkan card)
#   one or more on   → those cards, the model split across them by free memory
#   all off ([])     → the CPU only (--device none)
# Built-in graphics (Intel, AMD 'Radeon(TM) Graphics' APUs, software renderers) are never used.
# A CUDA-only build with no setting adds nothing to the command — exactly the old behaviour.
_DEV_RE = re.compile(r"^\s*((?:CUDA|Vulkan|ROCm|SYCL)\d+):\s*(.+?)\s*\((\d+)\s*MiB,\s*(\d+)\s*MiB free\)")
_BUILTIN_RE = re.compile(r"intel|microsoft|llvmpipe|swiftshader|\(tm\) graphics|^amd radeon graphics$", re.I)
_devices_cache = {}
_devices_lock = threading.Lock()          # one listing at a time; the next waits for its result


def _list_devices(server):
    """[{id, name, total, free}] from `llama-server --list-devices` — or None if the listing
    itself failed. Only a successful listing is cached. The FIRST listing on a fresh drive can
    take well over 30 s (≈1 GB of CUDA/Vulkan DLLs read from the USB for the first time, plus
    Windows scanning new files) — it timed out, 'no cards' was assumed, and the model ran on the
    CPU (2026-11-05). Now: 180 s, and a failure is not 'no cards'."""
    with _devices_lock:
        return _list_devices_locked(server)


def _list_devices_locked(server):
    if server in _devices_cache:
        return _devices_cache[server]
    out = []
    try:
        r = subprocess.run([server, "--list-devices"], capture_output=True, text=True, timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for line in (r.stdout or "").splitlines() + (r.stderr or "").splitlines():
            m = _DEV_RE.match(line)
            if m and not any(d["id"] == m.group(1) for d in out):
                out.append({"id": m.group(1), "name": m.group(2), "total": int(m.group(3)),
                            "free": int(m.group(4))})
        if not out and "Available devices" not in ((r.stdout or "") + (r.stderr or "")):
            log("[S32] Couldn't list graphics cards (llama-server gave no device list)")
            return None
    except Exception as e:
        log(f"[S32] Couldn't list graphics cards: {e}")
        return None
    _devices_cache[server] = out
    return out


def warm_gpu_list():
    """Read the card list in the background at start, so the first model load and the
    model panel don't wait on it (it's slow the first time on a fresh drive)."""
    def _go():
        s = _find_llama_server()
        if s:
            cards = usable_gpus(s)
            if cards:
                log("[S32] Graphics cards found: " + ", ".join(f"{c['name']} ({c['id']})" for c in cards))
    threading.Thread(target=_go, daemon=True, name="gpu-list").start()


def usable_gpus(server=None):
    """Each discrete card once, best first: NVIDIA via CUDA, then the rest via Vulkan."""
    server = server or _find_llama_server()
    if not server:
        return []
    devs = _list_devices(server)
    if devs is None:
        return None                                      # couldn't be listed (≠ no cards)
    cuda = [d for d in devs if d["id"].startswith("CUDA")]
    names = {d["name"].lower() for d in cuda}
    rest = [d for d in devs if not d["id"].startswith("CUDA")
            and d["name"].lower() not in names and not _BUILTIN_RE.search(d["name"])]
    return cuda + rest


def _pick_cards(cards, gpus):
    """→ the cards to use, given the model's setting (see the block comment above)."""
    if gpus == "all":                                    # the earlier One/All setting
        return cards
    if isinstance(gpus, list):
        names = {str(n).lower() for n in gpus}
        if not names:
            return []                                    # all switched off → CPU
        on = [c for c in cards if c["name"].lower() in names]
        if on:
            return on
    return cards[:1]                                     # default / another computer's cards


def _device_args(server, gpus):
    vulkan = os.path.exists(os.path.join(os.path.dirname(os.path.abspath(server)), "ggml-vulkan.dll"))
    if not vulkan and not isinstance(gpus, list):
        return []                                        # CUDA-only build, no setting — as before
    cards = usable_gpus(server)
    if cards is None:
        # Listing failed — NOT the same as 'no cards': let llama-server pick, as before.
        log("[S32] Couldn't read the graphics card list — letting llama-server choose the card.")
        return []
    pick = _pick_cards(cards, gpus)
    if not pick:
        log("[S32] Graphics cards all switched off (or none found) — running the model on the CPU.")
        return ["--device", "none"]
    if len(pick) > 1:
        free = [max(1, c["free"]) for c in pick]
        split = ",".join(str(round(f * 100 / sum(free))) for f in free)
        log("[S32] Graphics cards: " + " + ".join(f"{c['name']} ({c['id']})" for c in pick)
            + f" — model split {split.replace(',', '/')}")
        return ["--device", ",".join(c["id"] for c in pick), "--tensor-split", split]
    log(f"[S32] Graphics card: {pick[0]['name']} ({pick[0]['id']})")
    return ["--device", pick[0]["id"]]


def cancel_llama_load():
    """The ✕ on the model loading bar (2026-11-02): stop a load that's taking too long or has
    hung. The wait loop sees the cancel and stops quietly; the model panel can load again."""
    lm = state.get("load_model")
    if not lm:
        return {"error": "No model is loading."}
    gen = lm.get("gen")
    state["llama_cancel"] = gen
    state["load_model"] = None
    proc = _llama_proc.get("proc")
    try:
        if proc is not None and proc.poll() is None:
            proc.kill()
    except Exception:
        pass
    try:
        host = CONFIG.get("lmstudio_host", "http://localhost:1234")
        _kill_llama_on_port(int(host.split(":")[-1]) if ":" in host else 1234)
    except Exception:
        pass
    state["model_switching"] = False
    state["model_load_failed"] = True      # nothing loaded: chat says so, ComfyUI waits
    state["lmstudio_ready"] = True         # the UI stays usable so another load can start
    log(f"[S32] Model loading cancelled ({lm.get('name', 'model')}).")
    return {"ok": True, "msg": f"Loading {lm.get('name', 'the model')} was cancelled. Open model "
                               f"switching to change its settings or pick another model, then press Load."}


_llama_proc = {}


def _check_gpu_used(log_path, gpu_layers):
    """After a load (2026-11-02): an NVIDIA computer whose llama-server log never mentions
    CUDA is running the model on the CPU — llama-server doesn't say so itself. Say it
    plainly, once, with the fix."""
    try:
        if not gpu_layers or int(gpu_layers) <= 0:
            return                                        # CPU on purpose
        if "nvidia" not in str(get_hardware().get("gpu_name", "")).lower():
            return
        with open(log_path, encoding="utf-8", errors="replace") as f:
            head = f.read(200000)
        # A loaded CUDA backend lists its card ("- CUDA0 : NVIDIA ...") / "found 1 CUDA devices".
        if re.search(r"\bCUDA\d+\b|found [1-9]\d* CUDA device", head):
            return
        log("[S32] WARNING: the model is running on the CPU, not the graphics card — "
            "llama-server couldn't load its CUDA backend (llama_server.log lists only the CPU).")
        state["startup_notification"] = (
            "⚠ The model is running on the CPU, not your graphics card, so replies will be very "
            "slow. llama-server's graphics-card files (cudart64_12.dll, cublas64_12.dll, "
            "cublasLt64_12.dll) are missing from Sys 32's Home — restart System 32 and setup "
            "downloads them (needs the internet once).")
    except Exception:
        pass


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
    gpus        = None                                      # graphics cards (see _device_args)

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
                    if isinstance(_s.get("gpus_on"), list):
                        gpus = [str(n) for n in _s["gpus_on"]]
                    elif _s.get("gpus") == "all":
                        gpus = "all"
                    log(f"[S32] Applied model settings: ctx={ctx} gpu_layers={gpu_layers} "
                        f"threads={threads} batch={batch_size} kv={kv_cache} fa={flash_attn}"
                        + (f" cards={len(gpus) if isinstance(gpus, list) else gpus}" if gpus else ""))
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
        _store = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "LLM Storage")
        if not any(sep in model_path for sep in ("\\", "/")) or model_path == "local-model":
            # Nothing chosen yet (a fresh drive): not an error — say what to do (2026-11-05).
            log(f"[S32] No language model yet — put a .gguf model in its own folder inside {_store}, "
                f"then pick it in model switching (no restart needed).")
            state["startup_notification"] = (
                f"👋 Welcome! System 32 needs a language model to talk. Put a .gguf model file in its own "
                f"folder inside {_store}, then open the ☰ menu → MODEL SWITCHING, browse to it and press Load. "
                f"No restart needed — image generation starts with it.")
        else:
            log(f"[S32] llama-server: the model file isn't there any more: {model_path}")
            state["startup_notification"] = (
                f"⚠ The model file isn't where it was ({model_path}). Open the ☰ menu → MODEL SWITCHING and pick "
                f"it again, or another model — no restart needed.")
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
    _args += _device_args(server, gpus)
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
    _lkey = _load_key(model_path, ctx, gpu_layers, kv_cache,
                      mmproj_path if (mmproj_path and os.path.exists(mmproj_path)) else "") + \
        ("|cards:" + "+".join(sorted(str(n).lower() for n in gpus)) if isinstance(gpus, list)
         else "|all-gpus" if gpus == "all" else "")
    _t0 = time.time()
    state["load_model"] = {"gen": my_gen, "started": _t0, "expect": _expected_load(_lkey),
                           "name": os.path.splitext(os.path.basename(model_path))[0]}
    try:
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
        _llama_proc["proc"] = proc            # for the loading bar's ✕

        def _log_tail(path, n=1400):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    return f.read()[-n:]
            except Exception:
                return ""

        # Wait for server to become ready
        for i in range(60):
            time.sleep(3)
            if state.get("llama_cancel") == my_gen:          # the ✕ on the loading bar
                return
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
                state["model_load_failed"] = False     # a load that failed earlier (or a first
                                                       # boot with no model) no longer counts —
                                                       # ComfyUI's autostart and the chat read it
                _remember_load(_lkey, time.time() - _t0)
                state["load_key"] = _lkey          # the 'settling in' time is kept per load key
                state["settle_first"] = True       # the next prompt rebuild is this load's first
                _check_gpu_used(_llama_log, gpu_layers)
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
    finally:
        # The bar goes away however this ends (ready, crashed, timed out) — unless a
        # newer load has already put up its own.
        if (state.get("load_model") or {}).get("gen") == my_gen:
            state["load_model"] = None


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

