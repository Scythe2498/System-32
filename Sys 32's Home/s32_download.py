# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_download.py — Confined web downloads for System 32.

Fetches models, voices, and dataset images through the same web chokepoint the
rest of its web access uses (proxy-aware, logged, so the network guardian sees
it), behind the same propose→confirm gate as filesystem writes.

Flow:  propose_download(url, kind) probes the URL (size, type, filename), routes
it to the right folder by kind, and raises an in-chat Confirm card. Nothing
touches disk until confirm_download(token), which streams to a <name>.part file
with throttled progress notes and atomic-renames on completion.

Routing (kind → destination):
  checkpoint/lora/vae/controlnet/diffusion/embedding/upscale/clip/text_encoder
        → <ComfyUI>/models/<subdir>/   (ComfyUI always searches its own models/)
  voice → <SCRIPT_DIR>/piper/voices/
  image/dataset → <Output>/dataset/[subfolder]/   (auto-indexed for visual_search)
  web_image → <Output>/downloads/images/  (the ⬇ on an image-search result)
  video → <Output>/downloads/videos/      (the ⬇ on a video-search result; video PAGES
          like YouTube are saved with yt-dlp, installed on first use)
  generic → <Output>/downloads/

Safety: executables (.exe/.bat/.dll/.msi/…) are flagged hard and NEVER run —
only saved to disk for the operator to review.
"""

import os
import re
import time
import threading
import urllib.parse

from s32_config import CONFIG, REFERENCE_DIR, _script_dir, state, log
from s32_config import _result_lock, _job_id, _push_result, _push_done   # noqa: F401

_dl_lock = threading.Lock()
_active = {}   # filename → {"got": bytes, "total": bytes} for downloads running right now


def active_downloads_note():
    """One line for its live turn while downloads run/queue, so it KNOWS they're in
    progress (it re-downloaded a half-finished 6.9 GB file because nothing told it)."""
    parts = []
    with _dl_lock:
        for fn, p in _active.items():
            pct = f"{int(p['got'] * 100 / p['total'])}% " if p.get("total") else ""
            parts.append(f"{fn} — {pct}({_mb(p['got'])}"
                         f"{' / ' + _mb(p['total']) if p.get('total') else ''})")
        queued = [q["filename"] for q in _auto_queue]
    if not parts and not queued:
        return ""
    s = "[Background: downloading " + "; ".join(parts) if parts else "[Background:"
    if queued:
        s += ("; " if parts else " ") + "queued: " + ", ".join(queued)
    return s + ". These are NOT finished — do not start them again; they appear in chat when done.]"


def downloads_status():
    """For the UI's input-bar loading bar: what's downloading, what's queued, and
    whether ComfyUI is mid-restart (the step that follows a model download)."""
    with _dl_lock:
        active = [{"name": fn, "got": p.get("got", 0), "total": p.get("total", 0)}
                  for fn, p in _active.items()]
        queued = [q["filename"] for q in _auto_queue]
    restarting = False
    try:
        import s32_comfyui
        restarting = s32_comfyui._restart_lock.locked()
    except Exception:
        pass
    # Its background ComfyUI work that outlives the turn (batches, videos) —
    # set by s32_dispatch._bg_generate. Shares the bar so one ✕ covers both.
    return {"active": active, "queued": queued, "restarting": restarting,
            "batch": state.get("bg_gen")}


def cancel_all_downloads():
    """The input bar's ✕: stop the running download AND drop everything queued
    behind it (cancel_download alone only stopped the current file — the queue
    would just start the next one)."""
    with _dl_lock:
        dropped = [q["filename"] for q in _auto_queue]
        for q in _auto_queue:
            _inflight.discard(os.path.normcase(q["dest"]))
        _auto_queue.clear()
        running = list(_active)
    state["pending_download"] = None
    if running:
        state["download_cancel"] = True      # the worker posts "✗ Download cancelled." and removes the .part
    if dropped:
        _push_note("✗ Cancelled queued download(s): " + ", ".join(dropped))
    log(f"[S32][DL] cancel all: running={running} dropped={dropped}")
    return {"cancelled": True, "running": running, "dropped": dropped}


def _already_downloading(name):
    n = (name or "").lower()
    with _dl_lock:
        return n in {k.lower() for k in _active} or n in {q["filename"].lower() for q in _auto_queue}

_MODEL_SUBDIR = {
    "checkpoint": "checkpoints", "ckpt": "checkpoints", "model": "checkpoints",
    "lora": "loras", "locon": "loras", "lycoris": "loras",
    "vae": "vae",
    "controlnet": "controlnet",
    "diffusion": "diffusion_models", "unet": "diffusion_models",
    "embedding": "embeddings", "textualinversion": "embeddings",
    "upscale": "upscale_models", "esrgan": "upscale_models",
    "clip": "clip", "text_encoder": "text_encoders", "textencoder": "text_encoders",
}
_EXE_EXTS = {".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".ps1", ".vbs",
             ".vbe", ".js", ".jse", ".dll", ".sys", ".jar", ".lnk"}
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}
_VIDEO_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".avi"}
_MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".sft"}
_KNOWN_HOSTS = ("huggingface.co", "civitai.com", "github.com",
                "raw.githubusercontent.com", "cdn-lfs.huggingface.co")


def _out_base():
    out = CONFIG.get("out_images", "")
    return os.path.dirname(out) if out else REFERENCE_DIR


def _comfy_models_root():
    return (CONFIG.get("models_dir")
            or os.path.join(CONFIG.get("comfy_image_dir", ""), "models"))


def _safe_seg(name):
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name)
    return name or "download"


def _dest_dir(kind, subfolder=None):
    kind = (kind or "").lower()
    if kind in _MODEL_SUBDIR:
        d = os.path.join(_comfy_models_root(), _MODEL_SUBDIR[kind])
    elif kind == "voice":
        d = os.path.join(_script_dir, "piper", "voices")
    elif kind in ("image", "dataset"):
        d = CONFIG.get("out_dataset") or os.path.join(_out_base(), "dataset")
        if subfolder:
            d = os.path.join(d, _safe_seg(subfolder))
    elif kind == "web_image":
        d = os.path.join(_out_base(), "downloads", "images")
    elif kind == "video":
        d = os.path.join(_out_base(), "downloads", "videos")
    else:
        d = os.path.join(_out_base(), "downloads")
    os.makedirs(d, exist_ok=True)
    return d


def _infer_kind(url, filename, explicit=None):
    if explicit:
        e = explicit.lower()
        return e if (e in _MODEL_SUBDIR or e in ("voice", "image", "dataset", "generic",
                                                  "web_image", "video")) else "generic"
    u = (url or "").lower()
    f = (filename or "").lower()
    ext = os.path.splitext(f)[1]
    m = re.search(r"[?&]type=([a-z]+)", u)            # civitai ?type=Lora
    if m and m.group(1) in _MODEL_SUBDIR:
        return m.group(1)
    if ext in _IMG_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext == ".onnx" and ("piper" in u or "voice" in u or "voice" in f):
        return "voice"
    for key in ("lora", "vae", "controlnet", "upscale", "embedding", "clip"):
        if key in u or key in f:
            return key
    if ext in _MODEL_EXTS:
        return "checkpoint"
    if ext == ".onnx":
        return "voice"
    return "generic"


def _is_executable(fn):
    return os.path.splitext((fn or "").lower())[1] in _EXE_EXTS


def _filename_from(url, content_disposition="", override=None):
    if override:
        return _safe_seg(override)
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition or "")
    if m:
        return _safe_seg(urllib.parse.unquote(m.group(1)))
    path = urllib.parse.urlparse(url).path
    return _safe_seg(urllib.parse.unquote(os.path.basename(path) or "download"))


_CT_EXT = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/pjpeg": ".jpg",
    "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
    "image/bmp": ".bmp", "image/tiff": ".tiff", "image/avif": ".avif",
    "image/svg+xml": ".svg",
}


def _ensure_ext(filename, ctype):
    """If a downloaded file has no extension (e.g. a Flickr-style numeric id), add
    one from the Content-Type so it's recognised as an image instead of 'File'."""
    if os.path.splitext(filename)[1]:
        return filename
    base = (ctype or "").split(";")[0].strip().lower()
    ext = _CT_EXT.get(base, "")
    return filename + ext if ext else filename


def _collision_free(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def _mb(n):
    try:
        n = float(n)
    except Exception:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def _push_note(text, header="⬇ Download", key=None):
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        # group="activity": folded with neighbouring download/ComfyUI notes into one card.
        _cfg._push_result({"content": text, "path": header, "group": "activity",
                           "label": "download", "key": key}, jid)
        _cfg._push_done(jid)
    except Exception:
        pass


def _push_download_card(pend):
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result({
            "type":     "dl_confirm",
            "token":    pend["token"],
            "url":      pend["url"],
            "filename": pend["filename"],
            "dest":     pend["dest"],
            "kind":     pend["kind"],
            "size":     pend["size"],
            "size_h":   _mb(pend["size"]) if pend["size"] else "unknown",
            "host":     pend["host"],
            "exe":      pend["exe"],
            "trusted":  pend["trusted"],
        }, jid)
        _cfg._push_done(jid)
    except Exception as e:
        log(f"[S32][DL] card push failed: {e}")


def _dl_headers(url):
    """Browser-like headers so hotlink-protected hosts (which 404/403 bare requests)
    serve the file. Referer = the file's own origin, which defeats naive hotlink
    checks; Accept covers images and binary downloads alike."""
    from s32_web import _UA
    h = {"User-Agent": _UA,
         "Accept": "image/avif,image/webp,image/apng,image/*,application/octet-stream,*/*;q=0.8"}
    p = urllib.parse.urlparse(url)
    if p.scheme and p.netloc:
        h["Referer"] = f"{p.scheme}://{p.netloc}/"
    return h


def _probe(url):
    try:
        import requests
        from s32_web import _proxies
        r = requests.get(url, headers=_dl_headers(url), proxies=_proxies(),
                         stream=True, timeout=(15, 60), allow_redirects=True)
        size = int(r.headers.get("Content-Length") or 0)
        ctype = r.headers.get("Content-Type", "")
        cd = r.headers.get("Content-Disposition", "")
        status = r.status_code
        r.close()
        if status >= 400:
            return {"error": f"HTTP {status}"}
        return {"size": size, "ctype": ctype,
                "filename": _filename_from(url, cd), "status": status}
    except Exception as e:
        return {"error": str(e)[:120]}


def propose_download(url, kind=None, filename=None, subfolder=None):
    """Probe a URL and raise a Confirm card. Nothing downloads until confirm."""
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        _push_note("⛔ I can only download from http(s) URLs.")
        return {"refused": True, "reason": "Only http(s) URLs are allowed."}
    # Videos (a page like YouTube, or a .mp4) go through the video path — always confirmed.
    if (kind or "").lower() == "video" or (not kind and _is_video_page(url)):
        return propose_video_download(url, filename)
    _want = _safe_seg(filename) if filename else _filename_from(url)
    if _already_downloading(_want):
        return {"error": f"{_want} is ALREADY downloading in the background — don't start it "
                         f"again. It shows a ✓ note in chat when it's done."}
    info = _probe(url)
    if info.get("error"):
        err = info["error"]
        if err in ("HTTP 404", "HTTP 403", "HTTP 410"):
            _push_note(f"⛔ That URL returned {err} — it's likely wrong, expired, or a "
                       f"guess. For dataset images, search first (web_image_search) and "
                       f"download a real result URL.")
        else:
            _push_note(f"⛔ Couldn't reach that URL ({err}).")
        return {"error": err, "url": url}
    fn = _safe_seg(filename) if filename else info["filename"]
    kind = _infer_kind(url, fn, kind)
    ctype = (info.get("ctype", "") or "").lower()
    # A page URL (HTML) downloaded as an "image" is the classic mistake — the model
    # grabbed the source/page link instead of the direct image. Catch it here.
    if kind in ("image", "dataset", "web_image") and ("text/html" in ctype or ctype.startswith("text/")):
        _push_note("⛔ That link is a webpage, not an image file. Use the direct image URL "
                   "(the 'image:' link from web_image_search), not the page link.")
        return {"error": "That URL is a webpage, not an image.", "url": url}
    fn = _ensure_ext(fn, ctype)                       # extensionless → add .jpg/.png/etc.
    dest = _collision_free(os.path.join(_dest_dir(kind, subfolder), fn))
    host = urllib.parse.urlparse(url).netloc
    pend = {
        "token":    f"dl-{int(time.time() * 1000)}",
        "url":      url,
        "dest":     dest,
        "filename": os.path.basename(dest),
        "kind":     kind,
        "size":     info.get("size", 0),
        "ctype":    info.get("ctype", ""),
        "host":     host,
        "exe":      _is_executable(fn),
        "trusted":  any(host == h or host.endswith("." + h) for h in _KNOWN_HOSTS),
        "ts":       time.time(),
    }
    state["pending_download"] = pend

    # Trusted dataset/image downloads flow WITHOUT the confirm gate, so a
    # "find an image and turn it into a video" chain completes in one pass instead
    # of stalling on a Confirm card mid-chain (the image must exist on disk before
    # the next step can use it). Scope is deliberately narrow — only small,
    # non-executable image/dataset files auto-proceed; models, voices, generic, large,
    # or executable downloads still raise the gate (that's what the gate is FOR).
    _AUTO_IMG_MAX = 50 * 1024 * 1024  # 50MB ceiling; a real photo is well under this
    _auto_ok = (
        kind in ("image", "dataset", "web_image")
        and not pend["exe"]
        and 0 <= pend.get("size", 0) <= _AUTO_IMG_MAX
    )
    if _auto_ok:
        state["pending_download"] = None
        if state.get("download_running"):
            # Something else is mid-download; fall back to the card to avoid clashing.
            state["pending_download"] = pend
            _push_download_card(pend)
            return {"proposed": True, "kind": kind, "dest": dest,
                    "size": pend["size"], "executable": pend["exe"], "host": host}
        saved = _download_worker(pend)   # synchronous — file exists on return
        if saved:
            return {"downloaded": True, "kind": kind, "dest": saved,
                    "path": saved, "size": pend["size"], "host": host}
        return {"error": "The image download failed.", "url": url}

    _push_download_card(pend)
    return {"proposed": True, "kind": kind, "dest": dest,
            "size": pend["size"], "executable": pend["exe"], "host": host}


def confirm_download(token=None):
    import s32_config as _cfg
    if _cfg.airplane():
        state["pending_download"] = None
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    pend = state.get("pending_download")
    if not pend or (token and pend.get("token") != token):
        return {"error": "No matching download to confirm."}
    if state.get("download_running"):
        return {"error": "A download is already running."}
    state["pending_download"] = None
    worker = _ytdlp_worker if pend.get("via") == "yt-dlp" else _download_worker
    threading.Thread(target=worker, args=(pend,), daemon=True).start()
    return {"started": True, "filename": pend["filename"]}


def cancel_download(token=None):
    state["download_cancel"] = True
    if state.get("pending_download"):
        state["pending_download"] = None
        _push_note("✗ Download cancelled.")
    return {"cancelled": True}


def _download_worker(pend):
    url, dest = pend["url"], pend["dest"]
    part = dest + ".part"
    total = pend.get("size", 0)
    state["download_cancel"] = False
    state["download_running"] = True
    # Progress is shown by the input-bar loading bar (system32.py polls
    # downloads_status()) — no per-5% chat notes. Register first so the bar
    # appears the moment the download starts, not after the HTTP handshake.
    with _dl_lock:
        _active[pend["filename"]] = {"got": 0, "total": total}
    try:
        import requests
        from s32_web import _proxies
        r = requests.get(url, headers=_dl_headers(url), proxies=_proxies(),
                         stream=True, timeout=(15, 300), allow_redirects=True)
        if r.status_code >= 400:
            _push_note(f"⛔ Download failed: HTTP {r.status_code}")
            return
        if not total:
            total = int(r.headers.get("Content-Length") or 0)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        got = 0
        with _dl_lock:
            _active[pend["filename"]] = {"got": 0, "total": total}
        with open(part, "wb") as f:
            for chunk in r.iter_content(256 * 1024):
                if state.get("download_cancel"):
                    _push_note("✗ Download cancelled.")
                    f.close()
                    _safe_remove(part)
                    return
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                _active.get(pend["filename"], {})["got"] = got
        os.replace(part, dest)
        if pend.get("kind") in ("image", "dataset", "web_image"):
            try:
                from s32_filesystem import index_image
                index_image(dest)
            except Exception:
                pass
            # Make this the current image so a follow-up workflow (image→video,
            # img2img) injects it instead of skipping with last_image_path=None.
            try:
                state["last_image_path"] = dest
            except Exception:
                pass
        tail = ("  ⚠ This is an executable — review it before running. I will not run it."
                if pend.get("exe") else "")
        _push_note(f"✓ Downloaded {os.path.basename(dest)} ({_mb(got)}) → {dest}{tail}",
                   key="dl:" + os.path.basename(dest))
        log(f"[S32][DL] saved {dest} ({got} bytes)")
        return dest
    except Exception as e:
        _safe_remove(part)
        _push_note(f"⛔ Download error: {str(e)[:120]}")
        log(f"[S32][DL] failed {url}: {str(e)[:120]}")
        return None
    finally:
        state["download_running"] = False
        with _dl_lock:
            _active.pop(pend["filename"], None)


# ── Model auto-fetch (used by s32_models.preflight) ─────────────────────────
#  Downloads a model to the EXACT relative path a workflow references, inside the
#  folder ComfyUI actually searches. Trusted hosts download automatically (operator's
#  choice, 2026-10-06); anything else raises the normal Confirm card.
_inflight = set()            # normalized dest paths queued or downloading
_auto_queue = []
_auto_thread = {"t": None}


def _comfy_folder(folder):
    """The folder ComfyUI really loads `folder` models from (asks ComfyUI first)."""
    try:
        from s32_config import http_get
        host = CONFIG.get("comfy_image_host", "http://127.0.0.1:8188")
        fp = http_get(f"{host}/internal/folder_paths", timeout=5)
        entry = fp.get(folder) if isinstance(fp, dict) else None
        paths = entry[0] if (isinstance(entry, list) and entry and isinstance(entry[0], list)) else entry
        if isinstance(paths, list):
            for p in paths:
                if isinstance(p, str) and p:
                    return p
    except Exception:
        pass
    return os.path.join(_comfy_models_root(), folder)


def _safe_rel(rel):
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".", "..")]
    parts = [re.sub(r'[<>:"|?*\x00-\x1f]', "_", p) for p in parts]
    return os.path.join(*parts) if parts else "download.safetensors"


def _auto_worker():
    _any_ok = False
    try:
        while True:
            with _dl_lock:
                if not _auto_queue:
                    break
                pend = _auto_queue.pop(0)
            while state.get("download_running"):
                time.sleep(2)
            try:
                if _download_worker(pend):
                    _any_ok = True          # the worker already posted "✓ Downloaded …"
            finally:
                with _dl_lock:
                    _inflight.discard(os.path.normcase(pend["dest"]))
        # ComfyUI only sees new model files after a restart — do it once, now that the
        # queue is empty (it waits for any running generation to finish first).
        if _any_ok:
            try:
                from s32_comfyui import restart_comfyui
                restart_comfyui("new model downloaded", wait=False)
            except Exception as e:
                log(f"[S32][DL] ComfyUI restart after download failed: {e}")
    finally:
        _auto_thread["t"] = None


def fetch_models(items):
    """items: [{url, folder, rel_path}]. Returns {"started": n, "confirm": bool,
    "skipped": [...], "errors": [...]}, or {"error": ...} if nothing could start."""
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": "airplane mode is on — nothing can be downloaded", "airplane": True}
    started, skipped, errors, confirm = 0, [], [], False
    present, inflight = [], []
    for it in items or []:
        url = (it.get("url") or "").strip()
        dest = os.path.join(_comfy_folder(it.get("folder") or "checkpoints"),
                            _safe_rel(it.get("rel_path")))
        key = os.path.normcase(dest)
        if key in _inflight:
            inflight.append(os.path.basename(dest)); skipped.append(os.path.basename(dest))
            continue
        if os.path.exists(dest):
            present.append(os.path.basename(dest)); skipped.append(os.path.basename(dest))
            continue
        info = _probe(url)
        if info.get("error"):
            err = info["error"]
            if err in ("HTTP 401", "HTTP 403"):
                err += " (this file needs a login/API token on that site — download it manually)"
            errors.append(f"{os.path.basename(dest)}: {err}")
            continue
        host = urllib.parse.urlparse(url).netloc
        pend = {"token": f"dl-{int(time.time() * 1000)}-{started}", "url": url, "dest": dest,
                "filename": os.path.basename(dest), "kind": "model",
                "size": info.get("size", 0), "ctype": info.get("ctype", ""), "host": host,
                "exe": _is_executable(dest),
                "trusted": any(host == h or host.endswith("." + h) for h in _KNOWN_HOSTS),
                "ts": time.time()}
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if pend["trusted"] and not pend["exe"]:
            with _dl_lock:
                _inflight.add(key)
                _auto_queue.append(pend)
                if not _auto_thread["t"]:
                    _auto_thread["t"] = threading.Thread(target=_auto_worker, daemon=True)
                    _auto_thread["t"].start()
            started += 1
            log(f"[S32][DL] auto-fetch queued {pend['filename']} ({_mb(pend['size'])}) from {host}")
        elif not state.get("pending_download"):
            state["pending_download"] = pend
            _push_download_card(pend)
            confirm = True
        else:
            errors.append(f"{pend['filename']}: another Download card is already waiting")
    if not started and not confirm and errors:
        return {"error": "; ".join(errors)[:300]}
    return {"started": started, "confirm": confirm, "skipped": skipped, "errors": errors,
            "present": present, "inflight": inflight}


def _safe_remove(p):
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass


# ── Videos (2026-10-29) ───────────────────────────────────────────────────────
#  A video-search result is usually a PAGE (YouTube, Vimeo, …), not a file. Those are
#  saved with yt-dlp (pip, open source; installed in the background the first time,
#  together with deno, the small JavaScript runtime yt-dlp needs for YouTube). A direct
#  .mp4/.webm link uses the normal downloader. Either way a video ALWAYS raises the
#  Confirm card first (title, size, destination) — nothing is saved until Confirm.
_VIDEO_HOSTS = ("youtube.com", "youtu.be", "vimeo.com", "dailymotion.com", "twitch.tv",
                "tiktok.com", "x.com", "twitter.com", "facebook.com", "instagram.com",
                "reddit.com", "bilibili.com", "rumble.com", "streamable.com")
_VIDEO_MAX_HEIGHT = 1080
_ytdlp_install = {"started": False, "done": False}
# Sites change their pages often and an old yt-dlp then fails ("Unexpected response",
# "Unable to extract …" — TikTok, 2026-10-31). It was installed once and never updated.
# Now: updated at most once per session — in the background when the installed copy is
# older than _YTDLP_MAX_AGE_DAYS, or right away (then one retry) when a site fails in the
# way yt-dlp says an update fixes. Never in airplane mode.
_YTDLP_MAX_AGE_DAYS = 30
# curl-cffi: lets yt-dlp look like a real browser ("impersonation"). Dailymotion refuses
# without it ("none of these impersonate targets are available: firefox", 2026-10-31);
# TikTok often wants it too.
_YTDLP_SPEC = "yt-dlp[default,curl-cffi]"
_ytdlp_update = {"tried": False, "restart": False}
# Windows won't let pip replace a compiled file System 32 already has open (cffi's
# _cffi_backend .pyd, 2026-10-31: "[WinError 5] Access is denied"). Then the update is left
# for the next start: this marker makes bootstrap.py run it BEFORE System 32 loads anything.
_YTDLP_PENDING = os.path.join(_script_dir, ".pending_ytdlp_update")
_ytdlp_update_lock = threading.Lock()


def _ytdlp_age_days():
    """Days since the installed yt-dlp was released (its version IS the date), or None."""
    try:
        import datetime
        from yt_dlp.version import __version__ as v
        y, m, d = (int(x) for x in v.split(".")[:3])
        return (datetime.date.today() - datetime.date(y, m, d)).days
    except Exception:
        return None


def _ytdlp_needs_update(msg):
    low = (msg or "").lower()
    return ("latest version" in low or "yt-dlp -u" in low or "unable to extract" in low
            or "unexpected response" in low or "impersonat" in low)


def _update_ytdlp(why):
    """pip-upgrade yt-dlp once per session; drop the loaded copy so the next import is the
    new one. True if it updated."""
    import s32_config as _cfg
    if _cfg.airplane():
        return False
    with _ytdlp_update_lock:
        if _ytdlp_update["tried"]:
            return False
        _ytdlp_update["tried"] = True
        import importlib
        import subprocess
        import sys
        old = None
        try:
            from yt_dlp.version import __version__ as old
        except Exception:
            pass
        log(f"[S32][DL] updating the video downloader (yt-dlp {old or '?'}) — {why}…")
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-U", _YTDLP_SPEC,
                                "--quiet", "--break-system-packages"], capture_output=True, text=True,
                               timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception as e:
            log(f"[S32][DL] yt-dlp update failed: {e}")
            return False
        if r.returncode != 0:
            err = r.stderr or ""
            line = next((ln for ln in err.splitlines() if "ERROR" in ln), err[-200:])
            log(f"[S32][DL] yt-dlp update failed: {line.strip()[:300]}")
            if "WinError 5" in err or "Access is denied" in err:
                try:
                    with open(_YTDLP_PENDING, "w", encoding="utf-8") as f:
                        f.write(_YTDLP_SPEC)
                    _ytdlp_update["restart"] = True
                    log("[S32][DL] a file it needs is in use — the update will run on the next start")
                except Exception:
                    pass
            return False
        for k in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.")]:
            del sys.modules[k]
        importlib.invalidate_caches()
        new = None
        try:
            from yt_dlp.version import __version__ as new
        except Exception:
            pass
        log(f"[S32][DL] yt-dlp {old or '?'} → {new or '?'}")
        return bool(new)              # retry even if the version is the same: an extra (curl-cffi) may be new


def _is_video_page(url):
    host = urllib.parse.urlparse(url or "").netloc.lower().split(":")[0]
    return any(host == h or host.endswith("." + h) for h in _VIDEO_HOSTS)


def _ensure_ytdlp():
    """True when yt-dlp can be imported; otherwise starts a one-time background install."""
    import importlib
    if _ytdlp_install["done"]:
        importlib.invalidate_caches()
    try:
        import yt_dlp  # noqa: F401
        _age = _ytdlp_age_days()
        if _age is not None and _age > _YTDLP_MAX_AGE_DAYS and not _ytdlp_update["tried"]:
            threading.Thread(target=_update_ytdlp, args=(f"{_age} days old",), daemon=True).start()
        return True
    except ImportError:
        pass
    import s32_config as _cfg
    if not _ytdlp_install["started"] and not _cfg.airplane():
        _ytdlp_install["started"] = True

        def _bg():
            import subprocess
            import sys
            try:
                log("[S32][DL] installing the video downloader (yt-dlp + deno, one-time, in the background)…")
                r = subprocess.run([sys.executable, "-m", "pip", "install", _YTDLP_SPEC, "deno",
                                    "--quiet", "--break-system-packages"], capture_output=True, text=True,
                                   timeout=900, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if r.returncode == 0:
                    importlib.invalidate_caches()
                    _ytdlp_install["done"] = True
                    _push_note("✓ Video downloader installed — press ⬇ on a video again.")
                log("[S32][DL] yt-dlp " + ("installed" if r.returncode == 0
                                           else f"install failed: {(r.stderr or '')[-200:]}"))
                if r.returncode != 0:
                    _ytdlp_install["started"] = False        # let the next press try again
            except Exception as e:
                _ytdlp_install["started"] = False
                log(f"[S32][DL] yt-dlp install failed: {e}")
        threading.Thread(target=_bg, daemon=True).start()
    return False


def _ytdlp_opts(extra=None):
    h = _VIDEO_MAX_HEIGHT
    # One file with sound and picture (no ffmpeg needed to merge streams), mp4 preferred.
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "windowsfilenames": True,
         "format": f"b[ext=mp4][height<={h}]/b[height<={h}]/b"}
    try:
        from s32_web import _proxies
        proxy = (_proxies() or {}).get("https")
        if proxy:
            o["proxy"] = proxy
    except Exception:
        pass
    try:
        import deno
        dp = deno.find_deno_bin()
        if dp:
            o["js_runtimes"] = {"deno": {"path": dp}}
    except Exception:
        pass
    o.update(extra or {})
    return o


def propose_video_download(url, title=None):
    """Probe a video (file or page) and raise the Confirm card. Nothing saves until Confirm."""
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        _push_note("⛔ I can only download from http(s) URLs.")
        return {"refused": True, "reason": "Only http(s) URLs are allowed."}
    host = urllib.parse.urlparse(url).netloc
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower()
    direct = ext in _VIDEO_EXTS and not _is_video_page(url)
    info = {}
    if direct:
        info = _probe(url)
        if info.get("error"):
            _push_note(f"⛔ Couldn't reach that video ({info['error']}).")
            return {"error": info["error"], "url": url}
        # A link that ENDS in .webm/.mp4 but serves a web page (Wikimedia Commons'
        # "File:…webm" pages, 2026-10-29) — let yt-dlp find the real video in it.
        if "html" in (info.get("ctype") or "").lower():
            direct = False
    if direct:
        fn = _safe_seg(info.get("filename") or f"video{ext}")
        dest = _collision_free(os.path.join(_dest_dir("video"), fn))
        pend = {"token": f"dl-{int(time.time() * 1000)}", "url": url, "dest": dest,
                "filename": os.path.basename(dest), "kind": "video", "size": info.get("size", 0),
                "ctype": info.get("ctype", ""), "host": host, "exe": False, "trusted": False,
                "ts": time.time()}
    else:
        if not _ensure_ytdlp():
            _push_note("⏳ Setting up the video downloader (one-time install) — press ⬇ again in a minute.")
            return {"error": "The video downloader is still installing — try again in a minute.",
                    "installing": True}
        def _read():
            with _ytdlp_update_lock:      # never read while an update swaps the files
                pass
            import yt_dlp
            with yt_dlp.YoutubeDL(_ytdlp_opts()) as y:
                return y.extract_info(url, download=False)
        try:
            info = _read()
        except Exception as e:
            msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:200]
            log(f"[S32][DL] yt-dlp probe failed for {url}: {msg}")
            info = None
            if _ytdlp_needs_update(msg):
                _push_note("⏳ That site changed — updating the video downloader and trying again…")
                if _update_ytdlp("a site changed its page"):
                    try:
                        info = _read()
                        log(f"[S32][DL] retry after update worked for {url}")
                    except Exception as e2:
                        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e2)).replace("ERROR: ", "")[:200]
                        log(f"[S32][DL] yt-dlp probe failed again after update: {msg}")
            if info is None:
                if _ytdlp_update["restart"]:
                    _push_note("⏳ The video downloader's update needs System 32 restarted (one of its "
                               "files is in use right now). Close and reopen System 32, then try again "
                               "— the update finishes during startup.")
                    return {"error": "update pending: restart System 32, then try again", "url": url}
                _push_note(f"⛔ Couldn't read that video: {msg}")
                return {"error": msg, "url": url}
        if (info or {}).get("_type") == "playlist":
            _push_note("⛔ That's a playlist, not a single video.")
            return {"error": "playlist", "url": url}
        if info.get("is_live"):
            _push_note("⛔ That's a live stream — it can't be saved as a file.")
            return {"error": "live stream", "url": url}
        size = info.get("filesize") or info.get("filesize_approx") or 0
        if not size and info.get("requested_formats"):
            size = sum((f.get("filesize") or f.get("filesize_approx") or 0)
                       for f in info["requested_formats"])
        name = (info.get("title") or title or "video")[:80]
        fn = _safe_seg(f"{name}.{info.get('ext') or 'mp4'}")
        dest = _collision_free(os.path.join(_dest_dir("video"), fn))
        pend = {"token": f"dl-{int(time.time() * 1000)}", "url": url, "dest": dest,
                "filename": os.path.basename(dest), "kind": "video", "size": int(size or 0),
                "ctype": "video", "host": host, "exe": False, "trusted": False,
                "ts": time.time(), "via": "yt-dlp", "duration": info.get("duration")}
    if _already_downloading(pend["filename"]):
        return {"error": f"{pend['filename']} is already downloading."}
    state["pending_download"] = pend
    _push_download_card(pend)
    log(f"[S32][DL] video proposed: {pend['filename']} ({_mb(pend['size']) if pend['size'] else 'size unknown'}) from {host}")
    return {"proposed": True, "kind": "video", "dest": pend["dest"], "size": pend["size"],
            "host": host, "filename": pend["filename"]}


def _ytdlp_worker(pend):
    import glob
    with _ytdlp_update_lock:              # never download while an update swaps the files
        pass
    import yt_dlp
    url, dest = pend["url"], pend["dest"]
    stem = os.path.splitext(dest)[0]
    state["download_cancel"] = False
    state["download_running"] = True
    with _dl_lock:
        _active[pend["filename"]] = {"got": 0, "total": pend.get("size", 0)}

    def _hook(d):
        if state.get("download_cancel"):
            raise yt_dlp.utils.DownloadCancelled("cancelled by the operator")
        if d.get("status") == "downloading":
            tot = d.get("total_bytes") or d.get("total_bytes_estimate") or pend.get("size", 0)
            with _dl_lock:
                if pend["filename"] in _active:
                    _active[pend["filename"]] = {"got": d.get("downloaded_bytes") or 0,
                                                 "total": int(tot or 0)}
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        # '%' in a title would be read as a template field — escape it.
        opts = _ytdlp_opts({"outtmpl": stem.replace("%", "%%") + ".%(ext)s",
                            "progress_hooks": [_hook], "overwrites": False})
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=True)
        path = ((info or {}).get("requested_downloads") or [{}])[0].get("filepath") or dest
        got = os.path.getsize(path) if os.path.exists(path) else 0
        _push_note(f"✓ Downloaded {os.path.basename(path)} ({_mb(got)}) → {path}",
                   key="dl:" + os.path.basename(path))
        log(f"[S32][DL] video saved {path} ({got} bytes)")
        return path
    except yt_dlp.utils.DownloadCancelled:
        _push_note("✗ Download cancelled.")
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e)).replace("ERROR: ", "")[:200]
        _push_note(f"⛔ Video download failed: {msg}")
        log(f"[S32][DL] yt-dlp failed {url}: {msg}")
    finally:
        state["download_running"] = False
        with _dl_lock:
            _active.pop(pend["filename"], None)
    for leftover in glob.glob(glob.escape(stem) + "*.part") + glob.glob(glob.escape(stem) + "*.ytdl"):
        _safe_remove(leftover)
    return None
