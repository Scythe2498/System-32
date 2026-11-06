# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_models.py — find and fetch whatever a workflow needs, for ANY workflow.

Workflows are drag-and-drop, so nothing here knows about specific JSONs. Before a
workflow is queued, preflight() asks ComfyUI itself what is installed:

  • DETECT  — /object_info/<class_type> lists, per loader input, exactly which files
              ComfyUI can see. Any model filename the workflow references that isn't
              in that list is missing. A class_type ComfyUI doesn't know at all is a
              missing custom NODE.
  • SOURCE  — for each missing model: ComfyUI-Manager's public model list (filename →
              url + target folder; the same project the node installer already uses),
              then a Hugging Face search for the exact filename.
  • FETCH   — through s32_download.fetch_model(): trusted hosts (Hugging Face, Civitai,
              GitHub) download automatically, anything else raises the Confirm card.
              Missing nodes go to the existing install_missing_nodes path.

Everything degrades gracefully: if ComfyUI can't be asked, preflight reports nothing
and the workflow runs exactly as it did before.
"""

import os
import json
import threading
import time
import urllib.parse

from s32_config import CONFIG, state, log, http_get

MODEL_EXTS = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft", ".onnx")

# Loader class → ComfyUI models/<folder>. Used only when the source doesn't say where a
# file belongs. Unknown loaders fall back to the input name, then to "checkpoints".
_CLASS_FOLDER = {
    "CheckpointLoaderSimple": "checkpoints", "CheckpointLoader": "checkpoints",
    "ImageOnlyCheckpointLoader": "checkpoints", "unCLIPCheckpointLoader": "checkpoints",
    "LoraLoader": "loras", "LoraLoaderModelOnly": "loras",
    "VAELoader": "vae",
    "UNETLoader": "diffusion_models", "UnetLoaderGGUF": "unet",
    "CLIPLoader": "text_encoders", "DualCLIPLoader": "text_encoders",
    "TripleCLIPLoader": "text_encoders", "QuadrupleCLIPLoader": "text_encoders",
    "CLIPVisionLoader": "clip_vision",
    "UpscaleModelLoader": "upscale_models",
    "ControlNetLoader": "controlnet", "DiffControlNetLoader": "controlnet",
    "StyleModelLoader": "style_models", "GLIGENLoader": "gligen",
    "HypernetworkLoader": "hypernetworks",
}
_INPUT_FOLDER = {
    "ckpt_name": "checkpoints", "lora_name": "loras", "vae_name": "vae",
    "unet_name": "diffusion_models", "clip_name": "text_encoders",
    "clip_name1": "text_encoders", "clip_name2": "text_encoders", "clip_name3": "text_encoders",
    "control_net_name": "controlnet", "style_model_name": "style_models",
}
# Manager "save_path"/"type" values → models/<folder>
_MANAGER_TYPE_FOLDER = {
    "checkpoint": "checkpoints", "checkpoints": "checkpoints", "lora": "loras",
    "vae": "vae", "unet": "diffusion_models", "diffusion_model": "diffusion_models",
    "clip": "text_encoders", "text_encoder": "text_encoders", "t5": "text_encoders",
    "clip_vision": "clip_vision", "upscale": "upscale_models", "controlnet": "controlnet",
    "embeddings": "embeddings", "embedding": "embeddings",
}
_MANAGER_MODEL_LIST = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/model-list.json"

_info_cache = {}          # class_type → (timestamp, object_info entry ({} = unknown node))
_INFO_TTL = 20            # seconds — files added/removed mid-session are seen on the next run
_manager_cache = None     # list of Manager model entries
_lock = threading.Lock()


def _norm(p):
    return (p or "").replace("\\", "/").strip().lower()


def _combo_options(spec):
    """Allowed values of a COMBO input across ComfyUI spec formats:
    [[a, b, c], {...}]  or  ["COMBO", {"options": [a, b, c]}]."""
    if not isinstance(spec, (list, tuple)) or not spec:
        return None
    first = spec[0]
    if isinstance(first, list):
        return first
    if first == "COMBO" and len(spec) > 1 and isinstance(spec[1], dict):
        opts = spec[1].get("options")
        return opts if isinstance(opts, list) else None
    return None


def _object_info(host, class_type):
    with _lock:
        hit = _info_cache.get(class_type)
        if hit and time.time() - hit[0] < _INFO_TTL:
            return hit[1]
    try:
        data = http_get(f"{host}/object_info/{urllib.parse.quote(class_type)}", timeout=10)
        info = data.get(class_type, {}) if isinstance(data, dict) else {}
    except Exception:
        return None                      # ComfyUI unreachable → unknown, not "missing"
    with _lock:
        _info_cache[class_type] = (time.time(), info)
    return info


def clear_cache():
    """Forget what ComfyUI reported (call after models/nodes are installed)."""
    with _lock:
        _info_cache.clear()


def _manager_list():
    global _manager_cache
    import s32_config as _cfg
    if _cfg.airplane():
        return []                        # not cached — the real list loads once it's off
    if _manager_cache is not None:
        return _manager_cache
    try:
        from s32_web import _request
        body, r, err = _request("GET", _MANAGER_MODEL_LIST)
        data = json.loads(body) if (body and not err) else {}
        _manager_cache = data.get("models", []) if isinstance(data, dict) else []
        log(f"[S32][MODELS] Manager model list loaded: {len(_manager_cache)} entries")
    except Exception as e:
        log(f"[S32][MODELS] Manager model list unavailable: {str(e)[:80]}")
        _manager_cache = []
    return _manager_cache


def _hf_search(filename):
    """Find an exact filename on Hugging Face. Returns a resolve URL or None."""
    try:
        from s32_web import _request
    except Exception:
        return None
    base = os.path.basename(filename)
    stem = os.path.splitext(base)[0]
    queries = [stem]
    trimmed = stem.replace("_", "-").rsplit("-", 1)[0]
    if trimmed and trimmed != stem:
        queries.append(trimmed)
    checked = set()
    for q in queries:
        body, r, err = _request("GET", "https://huggingface.co/api/models",
                                params={"search": q, "limit": 8})
        if err or not body:
            continue
        try:
            repos = [m.get("id") for m in json.loads(body) if m.get("id")]
        except Exception:
            continue
        for rid in repos:
            if rid in checked:
                continue
            checked.add(rid)
            b2, r2, e2 = _request("GET", f"https://huggingface.co/api/models/{rid}")
            if e2 or not b2:
                continue
            try:
                sibs = [s.get("rfilename", "") for s in json.loads(b2).get("siblings", [])]
            except Exception:
                continue
            for rf in sibs:
                if os.path.basename(rf).lower() == base.lower():
                    return f"https://huggingface.co/{rid}/resolve/main/{urllib.parse.quote(rf)}"
    return None


def _type_dirs(host, folder):
    """Folders ComfyUI loads `folder`-type models from (e.g. every 'checkpoints' path,
    including extra model paths)."""
    try:
        fp = http_get(f"{host}/internal/folder_paths", timeout=5)
        e = fp.get(folder) if isinstance(fp, dict) else None
        paths = e[0] if (isinstance(e, list) and e and isinstance(e[0], list)) else e
        return [p for p in (paths or []) if isinstance(p, str) and os.path.isdir(p)]
    except Exception:
        return []


def on_disk(host, folder, rel):
    """Is this model file already sitting in a folder ComfyUI uses for its type? (The
    operator may have just dropped it in; ComfyUI only sees it after a restart.)"""
    if not folder:
        return None
    rel_n = (rel or "").replace("\\", "/").strip("/")
    base = os.path.basename(rel_n).lower()
    for d in _type_dirs(host, folder):
        cand = os.path.join(d, *rel_n.split("/"))
        if os.path.isfile(cand):
            return cand
        for root, _ds, files in os.walk(d):
            for fn in files:
                if fn.lower() == base:
                    return os.path.join(root, fn)
    return None


def find_source(filename, folder_hint=None):
    """Where to download a model file from. Returns {url, folder, via} or None."""
    base = os.path.basename(filename).lower()
    import s32_config as _cfg
    if _cfg.airplane():
        return None
    for m in _manager_list():
        if (m.get("filename") or "").lower() == base and m.get("url"):
            sp = (m.get("save_path") or "default").strip("/\\")
            folder = (folder_hint if sp in ("", "default")
                      else _MANAGER_TYPE_FOLDER.get(sp.lower(), sp))
            if not folder:
                folder = _MANAGER_TYPE_FOLDER.get((m.get("type") or "").lower(), "checkpoints")
            return {"url": m["url"], "folder": folder, "via": "ComfyUI-Manager model list"}
    url = _hf_search(filename)
    if url:
        return {"url": url, "folder": folder_hint or "checkpoints", "via": "Hugging Face search"}
    return None


def scan_workflow(host, wf):
    """Generic detection. Returns {"models": [...], "nodes": [...]} of what's missing,
    or None if ComfyUI couldn't be asked."""
    missing_models, missing_nodes, seen = [], [], set()
    for node_id, node in (wf or {}).items():
        if not isinstance(node, dict):
            continue
        ct = node.get("class_type")
        if not ct:
            continue
        info = _object_info(host, ct)
        if info is None:
            return None
        if info == {}:
            if ct not in missing_nodes:
                missing_nodes.append(ct)
            continue
        spec_inputs = {}
        for grp in ("required", "optional"):
            spec_inputs.update((info.get("input") or {}).get(grp) or {})
        for in_name, val in (node.get("inputs") or {}).items():
            if not isinstance(val, str) or not val.lower().endswith(MODEL_EXTS):
                continue
            opts = _combo_options(spec_inputs.get(in_name))
            if opts is None:
                continue                      # not a file picker ComfyUI validates
            if _norm(val) in {_norm(o) for o in opts if isinstance(o, str)}:
                continue
            key = (_norm(val), ct)
            if key in seen:
                continue
            seen.add(key)
            folder = _CLASS_FOLDER.get(ct) or _INPUT_FOLDER.get(in_name)
            missing_models.append({"file": val, "class_type": ct, "input": in_name,
                                   "node": node_id, "folder": folder})
    return {"models": missing_models, "nodes": missing_nodes}


def preflight(host, wf, auto_fetch=True):
    """Detect what a workflow needs before it's queued, and start fetching it.

    Returns None when nothing is missing (or ComfyUI couldn't be asked) — the caller
    runs the workflow normally. Otherwise returns {"block": bool, "message": str,
    "missing": [...], "fetching": [...], "no_source": [...], "nodes": [...]}.
    block=False means only substitutable files lacked a source, so the caller's
    existing fallback (checkpoint/LoRA auto-swap) may still run the workflow."""
    scan = scan_workflow(host, wf)
    if not scan or (not scan["models"] and not scan["nodes"]):
        return None
    return _handle(host, scan, auto_fetch)


def after_run_missing(host, node_type, folder, filename, auto_fetch=True):
    """A run crashed because a model file isn't on disk even though ComfyUI LISTED it
    (its model list was stale — e.g. the file was removed while System 32 was off, so
    neither preflight nor the hot-swap watcher could tell). Same handling as preflight:
    on disk → restart; else find a source and download (ComfyUI restarts after)."""
    clear_cache()
    scan = {"models": [{"file": filename, "class_type": node_type or "", "input": "",
                        "node": "", "folder": folder or _CLASS_FOLDER.get(node_type or "", "checkpoints")}],
            "nodes": []}
    return _handle(host, scan, auto_fetch)


# Folders ComfyUI's own list is checked against the disk at boot, via the loader whose
# picker shows that list (that picker is what validation trusts — and what went stale).
_DRIFT_PROBE = {
    "checkpoints":      ("CheckpointLoaderSimple", "ckpt_name"),
    "loras":            ("LoraLoaderModelOnly", "lora_name"),
    "vae":              ("VAELoader", "vae_name"),
    "diffusion_models": ("UNETLoader", "unet_name"),
    "upscale_models":   ("UpscaleModelLoader", "model_name"),
}
_COMFY_EXTS = (".safetensors", ".ckpt", ".pt", ".pt2", ".pth", ".bin", ".pkl", ".sft")


def list_drift(host):
    """Compare what ComfyUI lists with what's on disk. Returns (gone, new) — files it
    lists that no longer exist, and files on disk it doesn't list — or None if ComfyUI
    couldn't be asked."""
    clear_cache()
    gone, new, asked = [], [], False
    for folder, (cls, inp) in _DRIFT_PROBE.items():
        info = _object_info(host, cls)
        if not info:
            continue
        spec = {}
        for grp in ("required", "optional"):
            spec.update((info.get("input") or {}).get(grp) or {})
        opts = _combo_options(spec.get(inp))
        dirs = _type_dirs(host, folder)
        if opts is None or not dirs:
            continue
        asked = True
        listed = {_norm(o) for o in opts if isinstance(o, str) and o.lower().endswith(_COMFY_EXTS)}
        disk = set()
        for d in dirs:
            for root, _ds, files in os.walk(d):
                for fn in files:
                    if fn.lower().endswith(_COMFY_EXTS):
                        disk.add(_norm(os.path.relpath(os.path.join(root, fn), d)))
        gone += [o for o in sorted(listed - disk)]
        new  += [f for f in sorted(disk - listed)]
    return (gone, new) if asked else None


def _installed_stand_in(m):
    """The installed checkpoint / LoRA the run will use in place of a missing one, or ''."""
    try:
        from s32_comfyui import _default_checkpoint, fetch_lora_options
        if (m.get("folder") or "") == "checkpoints":
            alt = _default_checkpoint(m["file"])
            return alt if alt and alt != m["file"] else ""
        if (m.get("folder") or "") == "loras":
            loras = [x for x in (fetch_lora_options() or []) if x and x != "[None]"]
            return loras[0] if loras else ""
    except Exception:
        pass
    return ""


def _handle(host, scan, auto_fetch=True):
    lines, fetching, no_source = [], [], []
    op = []          # the same story in plain words for the OPERATOR's card

    import s32_config as _cfg
    if scan["nodes"] and _cfg.airplane():
        names = ", ".join(scan["nodes"])
        lines.append(f"Missing custom node(s): {names}. Airplane mode is on, so they can't be installed.")
        op.append(f"missing node(s) {names} — airplane mode is on")
    elif scan["nodes"]:
        names = ", ".join(scan["nodes"])
        log(f"[S32][MODELS] workflow needs missing node(s): {names}")
        state.setdefault("missing_nodes", {})["image (8188)"] = list(scan["nodes"])
        if auto_fetch and not state.get("node_install_running") and not state.get("node_install_worker"):
            state["node_install_running"] = True
            def _install():
                try:
                    from s32_hardware import tool_install_missing_nodes
                    rep = tool_install_missing_nodes("image")
                    log(f"[S32][MODELS] node install: {str(rep.get('report', rep))[:300]}")
                except Exception as e:
                    log(f"[S32][MODELS] node install failed: {e}")
                finally:
                    state["node_install_running"] = False     # the worker owns the bar now
                    clear_cache()
            threading.Thread(target=_install, daemon=True).start()
        lines.append(f"Missing custom node(s): {names} — installing them now (ComfyUI "
                     f"stops while they install, then restarts — the bar below the chat shows it).")
        op.append(f"missing node(s) {names} — installing")

    on_disk_now = []
    for m in scan["models"]:
        hit = on_disk(host, m["folder"], m["file"])
        if hit:
            m["on_disk"] = hit
            on_disk_now.append(m)
            continue
        src = find_source(m["file"], m["folder"])
        if not src:
            no_source.append(m)
            continue
        m["source"] = src
        fetching.append(m)

    if fetching and auto_fetch:
        try:
            from s32_download import fetch_models
            started = fetch_models([{"url": m["source"]["url"], "folder": m["source"]["folder"],
                                     "rel_path": m["file"]} for m in fetching])
        except Exception as e:
            started = {"error": str(e)[:120]}
        names = ", ".join(os.path.basename(m["file"]) for m in fetching)
        if isinstance(started, dict) and started.get("error"):
            lines.append(f"Missing model(s): {names}. Found a source but the download "
                         f"couldn't start: {started['error']}")
            op.append(f"missing {names} — the download couldn't start ({started['error']})")
        elif isinstance(started, dict) and started.get("confirm"):
            lines.append(f"Missing model(s): {names}. A Download card is up for the operator "
                         f"to confirm (not a trusted host); downloads continue after that.")
            op.append(f"missing {names} — confirm the download card to fetch it")
        else:
            _st = started if isinstance(started, dict) else {}
            if _st.get("started"):
                # 'started' is a COUNT (fetch_models) — joining it crashed the whole preflight,
                # so 'downloading now' never reached the operator (2026-11-05).
                _skip = set(_st.get("skipped") or [])
                _new = [os.path.basename(m["file"]) for m in fetching
                        if os.path.basename(m["file"]) not in _skip] or [names]
                lines.append(f"Missing model(s): {', '.join(_new)}. Downloading now from a trusted source "
                             f"— the download bar shows its progress.")
                op.append(f"missing {', '.join(_new)} — downloading (see the bar below)")
            if _st.get("inflight"):
                lines.append(f"Still downloading: {', '.join(_st['inflight'])} — wait for the ✓ "
                             f"note in chat, then run it again.")
                op.append(f"{', '.join(_st['inflight'])} is still downloading")
            if _st.get("present"):
                try:
                    from s32_comfyui import restart_comfyui
                    restart_comfyui("model on disk but not loaded", wait=False)
                    _rs = "Restarting ComfyUI now so it loads it (about a minute)"
                except Exception:
                    _rs = "ComfyUI needs a restart to see it"
                lines.append(f"Already on disk but ComfyUI hasn't loaded it yet: "
                             f"{', '.join(_st['present'])}. {_rs} — run the workflow again "
                             f"after the '✓ ComfyUI restarted' note.")
                op.append(f"{', '.join(_st['present'])} is on disk — ComfyUI is restarting to load it")

    if on_disk_now:
        names = ", ".join(os.path.basename(m["file"]) for m in on_disk_now)
        log(f"[S32][MODELS] on disk but not loaded by ComfyUI: {names}")
        try:
            from s32_comfyui import restart_comfyui
            restart_comfyui("model on disk but not loaded", wait=False)
            _rs = "Restarting ComfyUI now so it loads them (about a minute)"
        except Exception:
            _rs = "ComfyUI needs a restart to see them"
        lines.append(f"Already on disk but not loaded by ComfyUI yet: {names}. {_rs} — run the "
                     f"workflow again after the '✓ ComfyUI restarted' note.")
        op.append(f"{names} is on disk — ComfyUI is restarting to load it")

    # Only an ORDINARY image checkpoint/LoRA can be stood in for by the auto-swap.
    # Specialized models (3D, video…) can't — same rule as the swap guard in s32_comfyui.
    import re as _re
    _SWAPPABLE = {"checkpoints", "loras"}
    def _special(m):
        return (m["class_type"] in ("ImageOnlyCheckpointLoader",)
                or bool(_re.search(r"3d|hunyuan|tripo|svd|video|ltx|\bwan|cosmos|mochi",
                                   m["file"], _re.I)))
    hard_missing = [m for m in no_source
                    if (m["folder"] or "") not in _SWAPPABLE or _special(m)]
    import s32_config as _cfg
    if no_source and _cfg.airplane():
        names = ", ".join(os.path.basename(m["file"]) for m in no_source)
        lines.append(f"Missing: {names}. Airplane mode is on, so it can't be downloaded — tell the "
                     f"operator; it can be fetched once airplane mode is off.")
        op.append(f"missing {names} — airplane mode is on, so it can't be downloaded")
    elif no_source:
        # An ordinary checkpoint / LoRA with no known download is stood in for by one that IS
        # installed (the auto-swap in s32_comfyui) — say THAT, not "no source found", which
        # reads like a failure to a beginner (2026-11-05).
        swap_lines, still = [], []
        for m in no_source:
            if m in hard_missing:
                still.append(m)
                continue
            alt = _installed_stand_in(m)
            fn = os.path.basename(m["file"])
            if alt:
                swap_lines.append(f"{fn} isn't installed and has no known download, so the "
                                  f"installed {alt} is used instead.")
                op.append(f"{fn} isn't installed — using {alt} instead")
            else:
                still.append(m)
        lines += swap_lines
        if still:
            names = ", ".join(os.path.basename(m["file"]) for m in still)
            lines.append(f"No download source found automatically for: {names}. Search the web for "
                         f"the exact filename and use download_file, or ask the operator.")
            op.append(f"no download source found for {names}")

    block = bool(scan["nodes"] or fetching or hard_missing or on_disk_now)
    if block:
        lines.append("The workflow was NOT run. Tell the operator what's happening, and run it "
                     "again once the downloads/installs finish.")
    msg = "This workflow needs files that aren't installed. " + " ".join(lines)
    log(f"[S32][MODELS] preflight: {msg[:300]}")
    return {"block": block, "message": msg, "missing": scan["models"],
            "fetching": fetching, "no_source": no_source, "nodes": scan["nodes"],
            "operator": "; ".join(op)}
