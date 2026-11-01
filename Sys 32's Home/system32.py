# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
system32.py — HTTP handler, HTML UI, startup, main entry point.
              Launch with: python system32.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from s32_config import (
    CONFIG, state, log, is_up, http_get, cancel_generation, GenerationCancelled,
    _machine_name, _current_operator, _session_start,
    REFERENCE_DIR, PERSON_DIR, PEOPLE_DIR, MEMORY_INDEX, MEMORY_FILE, MEMORY_RECENT, MEMORY_ARCHIVE, SESSION_LOG_FILE, IDENTITY_FILE, WORKFLOW_DIR,
    _push_result, _push_done, _result_lock,
    PILImage, _senses,
    os, json, sys, io, base64, time, threading, signal,
    webbrowser, urllib, subprocess, datetime,
    BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer,
)
from s32_memory import (
    _ensure_memory_structure, _detect_operator, _create_soullessscythe_profile,
    _get_introduction_message, _load_identity, _update_identity,
    _compress_old_memories, _start_memory_compressor,
    _write_session_log, _start_session_log_writer,
    _reset_machine_preferences,
    save_state_snapshot, load_state_snapshot, list_state_snapshots,
    load_ui_layout, save_ui_layout, get_active_snapshot, set_active_snapshot,
    _reset_identity, _load_personality, _save_personality,
)
from s32_hardware import (
    get_hardware, detect_hardware,
    start_llama_server, shutdown_llama_server, _find_llama_server,
    check_missing_nodes,
)
from s32_comfyui import start_comfy_image, start_comfy_output_watcher, start_comfy_fs_watcher
from s32_llm import chat, build_system_prompt, _prompt_cache, _tool_result_summary, TOOLS_SPEC, _FOLLOWUP_TOOLS, invalidate_prompt_cache
from s32_dispatch import dispatch_tool
from s32_filesystem import _rebuild_memory_index

try:
    import voice as _voice
except ImportError:
    _voice = None

try:
    import s32_docs as _s32_docs   # structure-aware attachment chunking (Phase 1+)
except Exception:
    _s32_docs = None

# File-type sets shared across do_POST branches (/load_image, /browse_folder).
# Defined at module scope so referencing them in one branch never makes Python
# treat them as locals of the whole method (that was the IMAGE_EXTS UnboundLocalError).
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

def _auto_speak(text):
    """Speak a response automatically if voice is ready. Silent on failure."""
    if not _voice or not text:
        return
    try:
        if _voice._state.get("ready") and _voice._state.get("voice"):
            _voice.speak(text)
    except Exception:
        pass


def _trace_label(name, params):
    """Compact one-line label for a tool call, shown inline in chat as a trace."""
    p = params or {}
    key = (p.get("query") or p.get("url") or p.get("path") or p.get("name")
           or p.get("folder") or p.get("action") or p.get("filename") or p.get("project")
           or p.get("purpose") or "")
    key = str(key).replace("\n", " ")
    if len(key) > 64:
        key = key[:64] + "…"
    return f"{name}({key})" if key else f"{name}()"


# Tools whose RESULT the operator wants rendered in chat — the filesystem-browse
# tools, so it can watch WHERE it navigates and confirm it's the right place.
# Every tool still emits its green trace line; this only controls whether the result
# also gets a content box. The model receives every result regardless (via
# _tool_result_summary fed back into the chain), so hiding a box never starves it
# reasoning — it just keeps file-content reads and chatter out of the chat panel.
_SHOW_RESULT_TOOLS = {
    "list_directory", "explore_tree", "find_file",
    "smart_search", "index_search", "get_drives",
}


def _set_airplane(on):
    """Airplane mode on/off (Iron Dome panel). s32_config's guard takes effect at once;
    SearXNG stops/starts; with admin, the firewall backstop follows; her prompt + tools
    change (2026-10-31)."""
    import s32_config as _c
    _c.set_airplane(on)
    res = {"ok": True, "airplane": on}
    try:
        import s32_searx
        if on:
            s32_searx.stop()
        else:
            threading.Thread(target=s32_searx.start, daemon=True).start()
    except Exception as e:
        log(f"[S32][AIRPLANE] private search {'stop' if on else 'start'} failed: {e}")
    try:
        import s32_irondome
        fw = s32_irondome.airplane_firewall(on)
        res["firewall"] = fw
    except Exception as e:
        res["firewall"] = {"ok": False, "error": str(e)}
    try:
        invalidate_prompt_cache()
    except Exception:
        pass
    fw = res.get("firewall") or {}
    log(f"[S32][AIRPLANE] {'ON — System 32 is offline' if on else 'OFF — the web is back'}"
        + ("" if fw.get("ok") else (" (firewall backstop needs administrator)" if fw.get("needs_admin") else "")))
    return res


def _fop_key(tool, params):
    """Repeat-breaker identity for file_operation: WHAT it acts on. It has no path/query/folder/
    name, so every call looked identical and a corrected retry (2026-10-30: desktop\\… → C:\\…)
    was stopped as a repeat. Every other tool: "" (unchanged)."""
    if tool != "file_operation" or not isinstance(params, dict):
        return ""
    return json.dumps([params.get(k) for k in ("action", "paths", "destination", "filter", "new_name", "steps")],
                      default=str, sort_keys=True)[:400]


import re as _step_re

# Distinct generation STEPS an operator can ask for in one message. Used to decide how many
# generations a turn is allowed to chain: "generate a fox" = 1 (terminal, no unwanted
# follow-ups); "generate a fox, then remove its background, then make it 3D" = 3.
_STEP_PATTERNS = (
    r"\b(generat\w*|creat\w*|draw\w*|make|render|paint)\b.{0,40}?\b(image|picture|pic|photo|portrait|art)\b",
    r"background|\bbg\b",
    r"\b3\s?-?d\b|three[- ]d|\bmesh\b|\.glb\b",
    r"upscal",
    r"video|animat|\.mp4",
)
_SEQUENCE_CUE = r"\bthen\b|\band\b|,|after that|afterwards|in one go|chain|->|→|\bnext\b"


def _planned_gen_steps(text):
    """How many generation steps the operator's message asks for. >1 only when several
    distinct steps are named AND sequenced ('then', 'and', a comma, 'in one go', …), so
    'an image of a 3D-looking fox' stays a single step."""
    t = (text or "").lower()
    n = sum(1 for p in _STEP_PATTERNS if _step_re.search(p, t))
    if n > 1 and not _step_re.search(_SEQUENCE_CUE, t):
        return 1
    return max(1, n)


def _record_outcome(tool_name, tool_params, results):
    """Attach what a generation produced to the operator's request in history — on the
    USER side. Anything synthetic in its own (assistant) role gets parroted verbatim
    (proven with the ack filler and a '[Ran … — done.]' note); chat() joins consecutive
    user turns, so the request and its outcome stay visible next turn."""
    try:
        _r0 = results[0] if results else {}
        _what = tool_name
        _wf = (tool_params or {}).get("name")
        if _wf:
            _what += f" ({_wf})"
        if isinstance(_r0, dict) and _r0.get("error"):
            _how = f"failed: {str(_r0['error'])[:160]}"
        elif isinstance(_r0, dict) and _r0.get("type") == "queued":
            _how = "queued and rendering in the background"
        elif isinstance(_r0, dict) and _r0.get("saved"):
            _how = f"done — saved {os.path.basename(str(_r0['saved']))}"
        else:
            _how = "done"
        for _m in reversed(state["messages"]):
            if _m.get("role") == "user":
                if isinstance(_m.get("content"), str):
                    _m["content"] += f"\n[Outcome: {_what} — {_how}.]"
                break
    except Exception as _te:
        log(f"[S32] outcome note skipped: {_te}")


_PAGE_SEEN = threading.Event()   # set when an open tab polls /status
_THOUGHTS_LOG = os.path.join(PERSON_DIR, "thoughts.log")   # its reasoning quotes the person — theirs alone


def _pop_thought(context=""):
    """Take the reasoning from the last chat() call (s32_llm stores it in
    state['last_reasoning']), append it to thoughts.log for debugging, and return it for
    the chat pane. It is NEVER written into its history."""
    t = (state.pop("last_reasoning", "") or "").strip()
    if not t:
        return ""
    try:
        if os.path.exists(_THOUGHTS_LOG) and os.path.getsize(_THOUGHTS_LOG) > 5 * 1024 * 1024:
            os.replace(_THOUGHTS_LOG, _THOUGHTS_LOG + ".old")
        with open(_THOUGHTS_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    f"{' · ' + context[:120] if context else ''} =====\n{t}\n")
    except Exception:
        pass
    return t


def _should_show_result(tool_name, result):
    """True if this tool result should render as a chat box (else green-trace-only)."""
    if tool_name in _SHOW_RESULT_TOOLS:
        return True                          # browse/nav: show so navigation is visible
    if isinstance(result, dict):
        if result.get("quiet"):
            return False                     # meant for it only (e.g. "Python's still blocked")
        t = result.get("type")
        # file/text content reads (show_file/fs_read of a text or binary file) —
        # the green trace already says it read it; don't echo the body.
        if t in ("text", "file"):
            return False
        # images, video, 3D, web cards, grids, confirm cards, errors → render.
        if t or result.get("error"):
            return True
    return False                             # untyped, non-browse → green-trace-only


_MEDIA_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif", ".bmp": "image/bmp",
               ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
               ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
               ".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".obj": "text/plain",
               ".ply": "text/plain", ".stl": "model/stl"}


_ANNOUNCE_RE = _step_re.compile(
    r"\b(let me|let's|i'll|i will|i'm going to|i am going to|now i'll|next,? i'll|going to)\s+"
    r"(?!know\b|be\b|wait\b|hear\b)(\w+)", _step_re.I)


def _announces_action(text):
    """A reply whose LAST sentence promises a step ('Now I have the full picture. Let me read the
    two key functions…') with no tool call — the turn used to end there (2026-10-29, FRIK). A
    question, or 'let me know', is a real ending."""
    t = _step_re.sub(r"`[^`]*`", "x", str(text or "")).strip()
    if not t or t.endswith("?"):
        return False
    sents = [x for x in _step_re.split(r"(?<=[.!])\s+|\n+", t) if x.strip()]
    last = sents[-1] if sents else t
    return bool(_ANNOUNCE_RE.search(last)) and len(last) < 400


# (2026-10-31) She said a Confirm card was up for a file operation without calling the tool —
# no card existed. Caught when her words claim a card, no Confirm card was raised this turn,
# and none is still waiting from before (then she may mean that one).
_CARD_CLAIM_RE = _step_re.compile(
    r"(?i)(\bconfirm(?:ation)?\s+card\b"
    r"|\bcard\b[^.\n]{0,40}\b(?:is\s+up|up\s+(?:above|now)|above|below|ready|waiting|in\s+(?:the\s+)?chat|showing)\b"
    r"|\b(?:press|hit|click|tap)\w*\s+(?:the\s+)?[\"'“]?confirm\b"
    r"|\b(?:once|after|when)\s+you\s+confirm\b|\bwaiting\s+(?:for|on)\s+your\s+confirm)")


def _claims_missing_card(text, cards_at_start):
    if not text or not _CARD_CLAIM_RE.search(str(text)):
        return False
    if state.get("confirm_cards", 0) != cards_at_start:
        return False                                   # a card WAS raised this turn
    if state.get("pending_fileops") or state.get("pending_write") or state.get("pending_download") \
            or state.get("pending_sort"):
        return False                                   # one is still waiting from before
    return True


_CARD_NUDGE = ("[You said a Confirm card is up, but you did NOT call a tool — there is NO card. "
               "Call file_operation now (for several steps: one call with steps) — no text.]")


_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_DROP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".attachments", "dropped")
_DROP_MAX = 8 * 2**30            # 8 GB per file
_DROP_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".tga", ".dds", ".exr", ".hdr", ".psd"},
    "video": {".mp4", ".webm", ".mov", ".avi", ".mkv", ".m4v", ".wmv"},
    "audio": {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus"},
    "3d":    {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".usdz", ".usd", ".blend", ".nif", ".dae", ".3ds", ".uasset"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"},
    "model": {".safetensors", ".ckpt", ".gguf", ".pt", ".pth", ".onnx", ".bin"},
}


def _drop_kind(path):
    ext = os.path.splitext(path)[1].lower()
    for k, exts in _DROP_KINDS.items():
        if ext in exts:
            return k
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
        if head and b"\x00" not in head:
            head.decode("utf-8")
            return "text"
    except Exception:
        pass
    return "file"


def _unzip_drop(zpath):
    """A dropped .zip (a mod download, a project) is unpacked next to itself so she can
    import_code / read it. Zip-slip guarded; size-capped."""
    import zipfile
    out = os.path.splitext(zpath)[0]
    try:
        with zipfile.ZipFile(zpath) as z:
            infos = z.infolist()
            total = sum(i.file_size for i in infos)
            if total > 4 * 2**30 or len(infos) > 50000:
                return {"unpacked": None, "unpack_note": "too big to unpack automatically"}
            base = os.path.realpath(out)
            written = 0
            for i in infos:
                dest = os.path.realpath(os.path.join(out, i.filename))
                if not (dest == base or dest.startswith(base + os.sep)):
                    continue
                if i.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(i) as fi, open(dest, "wb") as fo:
                    while True:
                        b = fi.read(1 << 20)
                        if not b:
                            break
                        fo.write(b)
                written += 1
        return {"unpacked": out, "files_in_zip": written}
    except Exception as e:
        return {"unpacked": None, "unpack_note": f"couldn't unpack ({str(e)[:80]})"}


def _drop_note(dropped):
    """The turn note for files the operator dragged in: where each one is and what she can do."""
    real = os.path.realpath(_DROP_DIR)
    ok = []
    for d in dropped or []:
        p = str((d or {}).get("path") or "")
        if not p or not os.path.realpath(p).startswith(real + os.sep) or not os.path.isfile(p):
            continue                              # only files that really came through the drop
        ok.append({**d, "path": p, "kind": _drop_kind(p)})
    if not ok:
        return "", ok
    lines = [f"[The operator dropped {len(ok)} file(s) into the chat. They're saved on this computer — use "
             f"these exact paths with your tools:"]
    for d in ok:
        size = d.get("size") or os.path.getsize(d["path"])
        sz = f"{size / 2**20:.1f} MB" if size >= 2**20 else f"{max(1, size // 1024)} KB"
        line = f"- {d.get('name')} ({d['kind']}, {sz}): {d['path']}"
        if d.get("unpacked"):
            line += f"  — unpacked ({d.get('files_in_zip', '?')} files) to {d['unpacked']} (import_code that folder to work on it)"
        lines.append(line)
    lines.append("What to do with them depends on the request: text/code → read_file / search_code / edit_code; "
                 "a mod or project (folder or unpacked zip) → import_code; images → run_workflow / show_images; "
                 "3D files (.glb .fbx .obj .nif …) and textures → use them in the project they asked about. "
                 "If they didn't say, tell them what the file is and ask what to do with it.]")
    return "\n\n" + "\n".join(lines), ok


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _serve_local_media(self, path):
        """Stream a local picture/video/3D file — media types only, privacy wall, Range."""
        path = (path or "").strip()
        ext = os.path.splitext(path)[1].lower()
        mime = _MEDIA_MIME.get(ext)
        if not path or not mime or not os.path.isabs(path) or not os.path.isfile(path):
            self.send_response(404); self.end_headers(); return
        try:
            from s32_dispatch import _someone_elses
            if _someone_elses(path):
                self.send_response(403); self.end_headers(); return
        except Exception:
            pass
        try:
            size = os.path.getsize(path)
            start, end = 0, size - 1
            rng = self.headers.get("Range") or ""
            m = _step_re.match(r"bytes=(\d*)-(\d*)", rng)
            if m and size:
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                elif m.group(2):                       # suffix: last N bytes
                    start = max(0, size - int(m.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers(); return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(end - start + 1 if size else 0))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1 if size else 0
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log(f"[S32] media serve failed for {os.path.basename(path)}: {e}")

    def _serve_static(self, rel):
        """Bundled front-end files (<home>\\static): the 3D viewer + its decoders. Nothing outside."""
        rel = urllib.parse.unquote(rel)
        full = os.path.realpath(os.path.join(_STATIC_DIR, *[x for x in rel.replace("\\", "/").split("/") if x not in ("", ".", "..")]))
        ctype = {".js": "text/javascript", ".mjs": "text/javascript", ".wasm": "application/wasm",
                 ".json": "application/json"}.get(os.path.splitext(full)[1].lower())
        if not ctype or not full.startswith(os.path.realpath(_STATIC_DIR) + os.sep) or not os.path.isfile(full):
            self.send_response(404); self.send_header("Content-Length", "0"); self.end_headers()
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(data))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _receive_drop(self):
        """A file dragged (or attached) into chat — ANY type, streamed straight to disk in
        <home>\\.attachments\\dropped (private: never indexed). Only System 32's own page can
        post here: it must send X-S32-Drop, which a foreign web page can't without a CORS
        preflight this server never grants."""
        import urllib.parse as _up
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        origin = (self.headers.get("Origin") or "").lower()
        if self.headers.get("X-S32-Drop") != "1" or (origin and not _step_re.match(
                r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", origin)):
            log("[S32][DROP] refused an upload that didn't come from System 32's own page")
            self.send_json({"error": "refused"}, 403)
            return
        name = _up.unquote(_up.parse_qs(_up.urlparse(self.path).query).get("name", ["file"])[0])
        name = _step_re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(name.replace("\\", "/"))).strip(" .") or "file"
        if length <= 0:
            self.send_json({"error": "empty file"}, 400)
            return
        if length > _DROP_MAX:
            self.rfile.read(min(length, 1 << 20))
            self.send_json({"error": f"{name} is {length / 2**30:.1f} GB — the limit is {_DROP_MAX // 2**30} GB."}, 413)
            return
        try:
            import shutil as _sh
            free = _sh.disk_usage(_DROP_DIR if os.path.isdir(_DROP_DIR) else os.path.dirname(_DROP_DIR)).free
        except Exception:
            free = None
        if free is not None and free < length + 200 * 2**20:
            self.send_json({"error": f"Not enough space on the drive for {name} ({length / 2**20:.0f} MB)."}, 507)
            return
        os.makedirs(_DROP_DIR, exist_ok=True)
        dest = os.path.join(_DROP_DIR, time.strftime("%Y%m%d-%H%M%S_") + name)
        n = 1
        while os.path.exists(dest):
            base, ext = os.path.splitext(name)
            dest = os.path.join(_DROP_DIR, time.strftime("%Y%m%d-%H%M%S_") + f"{base}_{n}{ext}"); n += 1
        left, part = length, dest + ".part"
        try:
            with open(part, "wb") as f:
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 20))
                    if not chunk:
                        break
                    f.write(chunk)
                    left -= len(chunk)
            if left:
                raise IOError("upload cut off")
            os.replace(part, dest)
        except Exception as e:
            try:
                os.remove(part)
            except OSError:
                pass
            log(f"[S32][DROP] {name}: not saved ({e})")
            self.send_json({"error": f"{name} didn't finish uploading — try again."}, 500)
            return
        kind = _drop_kind(dest)
        info = {"name": name, "path": dest, "size": length, "kind": kind}
        if kind == "archive" and dest.lower().endswith(".zip"):
            info.update(_unzip_drop(dest))
        log(f"[S32][DROP] received {name} ({length / 2**20:.1f} MB, {kind}) → {dest}")
        self.send_json(info)

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass   # the page went away mid-request (tab closed / reloading) — nothing to do

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = get_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(html)
        elif self.path.startswith("/static/"):
            self._serve_static(self.path[len("/static/"):].split("?", 1)[0])
        elif self.path.startswith("/img?"):
            # Local pictures / videos / 3D for the chat pane (2026-10-29 security fix): this
            # used to fall back to `cmd /c if exist "<path>"` with the path from the URL —
            # any web page open in the browser could run commands on the computer through
            # it — and it served ANY file. Now: no shell, media types only, the privacy
            # wall, and streaming with Range (videos seek; big files aren't read into memory).
            qs = urllib.parse.parse_qs(self.path[5:])
            self._serve_local_media(qs.get("path", [""])[0])
        elif self.path.startswith("/thumb?"):
            # Small JPEG preview of a local picture or video (search grids).
            qs = urllib.parse.parse_qs(self.path[7:])
            try:
                import s32_findex
                data = s32_findex.thumbnail(qs.get("path", [""])[0])
            except Exception:
                data = None
            if not data:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/webimg?"):
            qs = urllib.parse.parse_qs(self.path[8:])
            u = qs.get("u", [""])[0]
            data = ctype = None
            try:
                import s32_web
                data, ctype, _err = s32_web.fetch_image(u)
            except Exception:
                data = None
            if not data:
                self.send_response(404); self.end_headers(); return
            self.send_response(200)
            self.send_header("Content-Type", ctype or "image/png")
            self.send_header("Content-Length", len(data))
            self.send_header("Cache-Control", "max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/shutdown" and state.get("resetting"):
            self.send_json({"status": "factory reset in progress"})   # the reset exits on its own
        elif self.path == "/shutdown":
            self.send_json({"status": "shutting down"})
            def _do_shutdown():
                time.sleep(0.3)
                log("[S32] Browser closed - running shutdown sequence...")
                _write_session_log(final=True)
                _update_identity()
                log("[S32] Running memory compression...")
                _compress_old_memories()
                log("[S32] Shutdown complete.")
                shutdown_llama_server()
                try:
                    import s32_searx
                    s32_searx.stop()
                except Exception:
                    pass
                time.sleep(1)
                os._exit(0)
            threading.Thread(target=_do_shutdown, daemon=False).start()
            return
        elif self.path == "/results":
            # Return and clear any pending background generation results
            with _result_lock:
                pending = list(state["pending_results"])
                state["pending_results"].clear()
            self.send_json({"results": pending})
        elif self.path == "/status":
            _PAGE_SEEN.set()            # a System 32 tab is open (see open_browser)
            workflow_dots = []
            try:
                for fname in sorted(os.listdir(WORKFLOW_DIR)):
                    if not fname.lower().endswith(".json"):
                        continue
                    label = fname[:-5]
                    ready = False
                    try:
                        with open(os.path.join(WORKFLOW_DIR, fname), "r", encoding="utf-8") as f:
                            wf_text = f.read(512)
                        ready = state["comfy_image_ready"] or is_up(CONFIG["comfy_image_host"])
                    except Exception:
                        pass
                    workflow_dots.append({"name": label, "ready": ready})
            except Exception:
                pass
            # Read actual ctx/gpu_layers from model_settings.json
            _ctx = CONFIG.get("lmstudio_context_length", 131072)
            _gpu = CONFIG.get("lmstudio_gpu_layers", 20)
            try:
                import json as _j
                _ms_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_settings.json")
                if os.path.exists(_ms_path):
                    with open(_ms_path, "r") as _msf:
                        _ms = _j.load(_msf)
                    _mpath = CONFIG.get("lmstudio_model_path", "")
                    _mkey = os.path.basename(_mpath).lower() if _mpath else ""
                    if _mkey and _mkey in _ms:
                        _ctx = _ms[_mkey].get("ctx_size", _ctx)
                        _gpu = _ms[_mkey].get("gpu_layers", _gpu)
            except Exception:
                pass
            self.send_json({
                "lm":              state["lmstudio_ready"],
                "model_failed":    state.get("model_load_failed", False),
                "job":             state["job_status"],
                "log":             state["startup_log"][-10:],
                "workflows":       workflow_dots,
                "active_workflow": state["active_workflow"],
                "notification":    state.get("startup_notification"),
                "security_alert":  state.get("security_alert"),
                "airplane":        __import__("s32_config").airplane(),
                "pending_action":  state.get("pending_action"),
                "pending_sort":    ((lambda ps: {"token": ps["token"], "dir": ps["dir"],
                                                  "total": ps["total"]})(state["pending_sort"])
                                     if state.get("pending_sort") else None),
                "model_path":      CONFIG.get("lmstudio_model_path", ""),
                "ctx_size":        _ctx,
                "gpu_layers":      _gpu,
            })
        elif self.path == "/api/identity":
            try:
                with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
                    identity_content = f.read()
            except Exception:
                identity_content = ""
            _p = _load_personality()
            self.send_json({"content": identity_content,
                            "name": _p.get("assistant_name", "System 32"),
                            "gender": _p.get("gender", "Neutral")})
        elif self.path == "/api/voices":
            try:
                from voice import list_voices as _list_voices
                vdata = _list_voices()
                voices = list(vdata.get("voices", {}).keys())
                current = vdata.get("current", "")
            except Exception as _ve:
                log(f"[S32] Voice API error: {_ve}")
                voices = []
                current = ""
            try:
                from voice import get_volume as _get_volume
                vol = _get_volume()
            except Exception:
                vol = 1.0
            self.send_json({"voices": voices, "current": current, "volume": vol})
        elif self.path == "/api/stt/devices":
            try:
                import s32_stt
                self.send_json(s32_stt.list_microphones())
            except Exception as e:
                self.send_json({"devices": [], "error": str(e)})
        elif self.path == "/api/stt/level":
            try:
                import s32_stt
                self.send_json(s32_stt.get_level())
            except Exception as e:
                self.send_json({"level": 0, "enabled": False, "error": str(e)})
        elif self.path == "/api/dome/status":
            try:
                import s32_irondome
                self.send_json(s32_irondome.iron_dome_status())
            except Exception as e:
                self.send_json({"error": str(e), "ports": [], "whitelist": {"ports": [], "procs": []}})
        elif self.path == "/api/update/status":
            try:
                import s32_update
                self.send_json(s32_update.status())
            except Exception as e:
                self.send_json({"error": str(e), "available": ""})
        elif self.path == "/api/egress/status":
            try:
                import s32_egress
                self.send_json(s32_egress.status())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/egress/blocks":
            try:
                import s32_egress
                self.send_json(s32_egress.blocks())
            except Exception as e:
                self.send_json({"error": str(e), "blocks": []})
        elif self.path == "/api/dome/plan":
            try:
                import s32_irondome
                self.send_json(s32_irondome.compute_close_plan())
            except Exception as e:
                self.send_json({"error": str(e), "close_ports": [], "close_procs": []})
        elif self.path == "/api/downloads":
            try:
                import s32_download
                self.send_json(s32_download.downloads_status())
            except Exception as e:
                self.send_json({"active": [], "queued": [], "restarting": False, "error": str(e)})
        elif self.path == "/api/tuning":
            try:
                import s32_llm
                self.send_json(s32_llm.get_tuning())
            except Exception as e:
                self.send_json({"error": str(e), "values": {}, "spec": []})
        elif self.path == "/api/unreal/status":
            try:
                import s32_unreal
                self.send_json(s32_unreal.detect_unreal())
            except Exception as e:
                self.send_json({"installed": False, "error": str(e)})
        elif self.path.startswith("/api/steam/games"):
            try:
                from urllib.parse import urlparse, parse_qs
                import s32_steam
                show = (parse_qs(urlparse(self.path).query).get("show_hidden") or ["0"])[0] in ("1", "true")
                self.send_json(s32_steam.list_games(show_hidden=show))
            except Exception as e:
                self.send_json({"error": str(e), "steam_found": False, "count": 0, "groups": []})
        elif self.path == "/api/game/status":
            try:
                import s32_gameinput
                self.send_json(s32_gameinput.status())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/gameplay/sessions":
            try:
                import s32_gameplay
                d = s32_gameplay.list_sessions()
                d["stats"] = s32_gameplay.stats()
                self.send_json(d)
            except Exception as e:
                self.send_json({"error": str(e), "sessions": []})
        elif self.path.startswith("/api/steam/icon"):
            try:
                from urllib.parse import urlparse, parse_qs
                import s32_steam
                appid = (parse_qs(urlparse(self.path).query).get("appid") or [""])[0]
                p = s32_steam.icon_for_uid(appid) if appid else None
                if p and os.path.exists(p):
                    with open(p, "rb") as f:
                        data = f.read()
                    pl = p.lower()
                    ct = ("image/x-icon" if pl.endswith(".ico")
                          else "image/png" if pl.endswith(".png")
                          else "image/jpeg")
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Cache-Control", "max-age=86400")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_response(302)
                    self.send_header("Location", s32_steam.cdn_icon(appid))
                    self.end_headers()
            except Exception:
                self.send_response(404); self.end_headers()
        elif self.path == "/api/memory/files":
            files = []
            for fpath in [MEMORY_FILE, MEMORY_RECENT, MEMORY_INDEX, SESSION_LOG_FILE]:
                if os.path.exists(fpath):
                    files.append(fpath)
            if os.path.exists(MEMORY_ARCHIVE):
                for fname in os.listdir(MEMORY_ARCHIVE):
                    files.append(os.path.join(MEMORY_ARCHIVE, fname))
            self.send_json({"files": files})
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        # Cross-site guard (2026-10-30): any web page open in the browser could POST here —
        # e.g. press Confirm on a staged write / file operation. Browsers always send Origin
        # on a cross-site POST; only System 32's own page (localhost) is accepted.
        _orig = (self.headers.get("Origin") or "").lower()
        if _orig and not _step_re.match(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$", _orig):
            log(f"[S32] refused a request from another web page ({_orig[:60]}) to {self.path.split('?')[0]}")
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n:
                    self.rfile.read(min(n, 1 << 20))
            except Exception:
                pass
            self.send_json({"error": "refused"}, 403)
            return
        if self.path.startswith("/api/drop"):
            return self._receive_drop()          # streamed — never read into memory as JSON
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        body   = json.loads(raw) if raw.strip() else {}

        if self.path == "/api/identity":
            new_content = body.get("content", "")
            new_name    = body.get("name", None)
            new_gender  = body.get("gender", None)
            try:
                with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
                    f.write(new_content)
                from s32_memory import _load_identity, _update_identity
                _load_identity(); _update_identity()
                # Persist name + gender to the per-iteration personality store (travels with
                # snapshots; NOT global config). "System 32" stays the platform name in code/logs.
                if new_name is not None or new_gender is not None:
                    _p = _save_personality(assistant_name=new_name, gender=new_gender)
                    log(f"[S32] Identity saved — name: {_p.get('assistant_name')}, gender: {_p.get('gender')}")
                _p = _load_personality()
                self.send_json({"ok": True,
                                "name": _p.get("assistant_name", "System 32"),
                                "gender": _p.get("gender", "Neutral")})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/unreal/launch":
            mode = str(body.get("mode", "pc")).lower()
            try:
                import s32_unreal
                self.send_json(s32_unreal.launch_unreal(mode))
            except Exception as e:
                self.send_json({"ok": False, "msg": str(e)})
        elif self.path == "/api/unreal/set-project":
            path = str(body.get("project", "")).strip().strip('"')
            try:
                import s32_config, json as _json
                s32_config.CONFIG["unreal_project"] = path
                # Persist to config.json (survives the fresh-boot wipe via launch.py's
                # preserve whitelist), so launch opens straight into the project's world
                # instead of the project browser.
                _cf = s32_config._config_file
                _cfg = {}
                try:
                    if os.path.isfile(_cf):
                        with open(_cf, encoding="utf-8") as _f:
                            _cfg = _json.load(_f)
                except Exception:
                    _cfg = {}
                _cfg["unreal_project"] = path
                with open(_cf, "w", encoding="utf-8") as _f:
                    _json.dump(_cfg, _f, indent=2)
                exists = bool(path) and os.path.isfile(path)
                self.send_json({"ok": True, "project": path, "exists": exists})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
        elif self.path == "/api/unreal/spawn-test":
            try:
                import s32_unreal
                self.send_json(s32_unreal.spawn_cube())
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
        elif self.path == "/api/voice/set":
            voice = body.get("voice", "")
            try:
                from voice import set_voice as _set_voice
                _set_voice(voice)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/voice/preview":
            voice = body.get("voice", "")
            try:
                from voice import speak as _speak, set_voice as _set_voice
                _set_voice(voice)
                _speak(f"Hello, I am {_load_personality().get('assistant_name','System 32')}.")
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/voice/volume":
            try:
                from voice import set_volume as _set_volume
                self.send_json(_set_volume(body.get("volume", 1.0)))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/stt/toggle":
            try:
                import s32_stt
                self.send_json(s32_stt.toggle(body.get("on")))
            except Exception as e:
                self.send_json({"error": str(e), "enabled": False})
        elif self.path == "/api/stt/select":
            try:
                import s32_stt
                self.send_json(s32_stt.set_microphone(body.get("device")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/stt/mode":
            try:
                import s32_stt
                self.send_json(s32_stt.set_mode(body.get("mode", "continuous")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/stt/ptt/start":
            try:
                import s32_stt
                self.send_json(s32_stt.ptt_start())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/stt/ptt/stop":
            try:
                import s32_stt
                self.send_json(s32_stt.ptt_stop())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/dome/whitelist":
            try:
                import s32_irondome
                self.send_json(s32_irondome.set_whitelist(text=body.get("text", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/dome/whitelist-target":
            try:
                import s32_irondome
                self.send_json(s32_irondome.whitelist_target(body.get("target", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/dome/arm":
            try:
                import s32_irondome
                self.send_json(s32_irondome.arm(mode=body.get("mode", "block")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/dome/disarm":
            try:
                import s32_irondome
                self.send_json(s32_irondome.disarm())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/thought_summary":
            try:
                import s32_llm
                self.send_json({"summary": s32_llm.summarize_thought(body.get("text", ""))})
            except Exception as e:
                self.send_json({"summary": "", "error": str(e)})
        elif self.path == "/api/tuning":
            try:
                import s32_llm
                if body.get("reset"):
                    self.send_json(s32_llm.reset_tuning())
                else:
                    self.send_json(s32_llm.set_tuning(body.get("values", {})))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/steam/launch":
            try:
                import s32_steam
                self.send_json(s32_steam.launch_game(body.get("appid"), body.get("who", "user")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/steam/restore":
            try:
                import s32_steam
                self.send_json(s32_steam.restore_brain_now())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/steam/hide":
            try:
                import s32_steam
                self.send_json(s32_steam.set_hidden(body.get("appid"), bool(body.get("hidden", True))))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/steam/icon/set":
            try:
                import s32_steam
                uid = body.get("uid", "")
                if body.get("source") == "screen":
                    frame = None
                    try:
                        import senses
                        frame = senses._screen.get("latest_b64")
                    except Exception:
                        frame = None
                    if not frame:
                        self.send_json({"error": "No screen frame available — turn Screen Vision on first."})
                    else:
                        self.send_json(s32_steam.set_custom_icon(uid, b64=frame))
                else:
                    self.send_json(s32_steam.set_custom_icon(uid, src_path=body.get("path", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/steam/icon/clear":
            try:
                import s32_steam
                self.send_json(s32_steam.remove_custom_icon(body.get("uid", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/steam/category":
            try:
                import s32_steam
                self.send_json(s32_steam.category_op(
                    body.get("op"), name=body.get("name"), new_name=body.get("new_name"),
                    appid=body.get("appid"), category=body.get("category"),
                    index=body.get("index"), hidden=body.get("hidden"),
                    parent=body.get("parent"), color=body.get("color"),
                    order=body.get("order"), collapsed=body.get("collapsed")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/game/halt":
            try:
                import s32_gameinput
                self.send_json(s32_gameinput.halt())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/gameplay/label":
            try:
                import s32_gameplay
                self.send_json(s32_gameplay.label_session(body.get("label"), body.get("session")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/gameplay/export":
            try:
                import s32_gameplay
                self.send_json(s32_gameplay.export(scope=body.get("scope", "good"),
                                                   inline_images=bool(body.get("inline"))))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/gameplay/reset":
            try:
                import s32_gameplay
                self.send_json(s32_gameplay.reset())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/vision":
            try:
                import senses
                if body.get("get"):
                    self.send_json(senses.get_vision_settings())
                else:
                    out = {}
                    if "enabled" in body: out.update(senses.set_screen_vision(body.get("enabled")))
                    if "mode" in body:    out.update(senses.set_capture_mode(body.get("mode")))
                    if "fps" in body:     out.update(senses.set_fps(body.get("fps")))
                    out["settings"] = senses.get_vision_settings()
                    self.send_json(out)
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/factory_reset":
            # Privacy-first full wipe (s32_reset): preview, or run after typed confirm.
            try:
                import s32_reset
                if body.get("preview"):
                    self.send_json(s32_reset.preview())
                else:
                    self.send_json(s32_reset.factory_reset(body.get("confirm", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/memory/reset":
            try:
                for fpath in [MEMORY_FILE, MEMORY_RECENT, MEMORY_INDEX, SESSION_LOG_FILE]:
                    if os.path.exists(fpath):
                        with open(fpath, "w", encoding="utf-8") as f:
                            f.write("")
                if os.path.exists(MEMORY_ARCHIVE):
                    for fname in os.listdir(MEMORY_ARCHIVE):
                        os.remove(os.path.join(MEMORY_ARCHIVE, fname))
                # Also reset this machine's saved operator preferences
                try:
                    _reset_machine_preferences()
                except Exception as e:
                    log(f"[S32] Machine preference reset failed: {e}")
                # Reset identity to a neutral base so it still has a working persona
                try:
                    _reset_identity()
                except Exception as e:
                    log(f"[S32] Identity reset failed: {e}")
                # Also wipe accumulated training data (lessons + tool stats) so
                # bad/clogged feedback is cleared and regenerates fresh.
                try:
                    from s32_feedback import reset as _fb_reset
                    _fb_reset()
                except Exception as e:
                    log(f"[S32] Training-data reset failed: {e}")
                # Full clean slate: wipe the conversation training corpus and the
                # visual (image) index too (the indexer rebuilds from new outputs).
                try:
                    from s32_training import wipe_corpus as _wipe_corpus
                    _wipe_corpus()
                except Exception as e:
                    log(f"[S32] Corpus wipe failed: {e}")
                try:
                    from s32_filesystem import wipe_visual_index as _wipe_vix
                    _wipe_vix()
                except Exception as e:
                    log(f"[S32] Visual index wipe failed: {e}")
                # Wipe captured gameplay training data (sessions + exports) too, so a
                # blank slate carries no leftover (state→action) capture.
                try:
                    import s32_gameplay
                    s32_gameplay.reset()
                except Exception as e:
                    log(f"[S32] Gameplay-data reset failed: {e}")
                # No snapshot is "current" after a blank-slate reset.
                try:
                    set_active_snapshot("")
                except Exception as e:
                    log(f"[S32] Active-snapshot clear failed: {e}")
                # Reset voice to the built-in default (af_bella) so reset never
                # leaves its pointing at a downloaded voice that may be absent.
                try:
                    import voice as _v
                    _v.reset_voice_default()
                except Exception as e:
                    log(f"[S32] Voice reset failed: {e}")
                invalidate_prompt_cache()
                log("[S32] Memory + machine preferences + identity + training data + "
                    "image index + voice reset by operator.")
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/state/save":
            try:
                self.send_json(save_state_snapshot(name=(body.get("name") or "").strip()))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/ui/layout":
            try:
                if body.get("get"):
                    self.send_json({"layout": load_ui_layout()})
                else:
                    self.send_json(save_ui_layout(body.get("key"), body.get("geo") or {}))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/training/export":
            try:
                from s32_training import export_training_data as _exp
                self.send_json(_exp(fmt=(body.get("format") or "unsloth")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/training/rate":
            try:
                from s32_training import rate_turn as _rate
                self.send_json(_rate(body.get("id"), body.get("label"), body.get("note") or ""))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/training/stats":
            try:
                from s32_training import training_stats as _tstats
                self.send_json(_tstats())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/state/list":
            try:
                self.send_json({"snapshots": list_state_snapshots(),
                                "active": get_active_snapshot()})
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/state/load":
            try:
                fname = (body.get("filename") or "").strip()
                if not fname:
                    self.send_json({"error": "No snapshot filename provided."})
                else:
                    result = load_state_snapshot(fname)
                    invalidate_prompt_cache()
                    self.send_json(result)
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path in ("/api/update/check", "/api/update/apply", "/api/update/dismiss",
                           "/api/update/rollback"):
            try:
                import s32_update
                act = self.path.rsplit("/", 1)[1]
                if act == "check":
                    self.send_json(s32_update.check())
                elif act == "apply":
                    self.send_json(s32_update.apply(body.get("version", "")))
                elif act == "dismiss":
                    self.send_json(s32_update.dismiss(body.get("version", "")))
                else:
                    self.send_json(s32_update.rollback())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/egress/baseline":
            try:
                import s32_egress
                self.send_json(s32_egress.baseline_now())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/airplane":
            try:
                self.send_json(_set_airplane(bool(body.get("on"))))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/egress/block":
            try:
                import s32_egress
                self.send_json(s32_egress.block(body.get("id", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/egress/unblock":
            try:
                import s32_egress
                self.send_json(s32_egress.unblock(body.get("rule", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path in ("/api/security/approve", "/api/security/dismiss") \
                and str(body.get("id") or "").startswith("eg:"):
            try:                                   # an OUTBOUND alert (s32_egress)
                import s32_egress
                self.send_json(s32_egress.trust(body["id"]) if self.path.endswith("approve")
                               else s32_egress.dismiss(body["id"]))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/security/approve":
            try:
                import s32_firewall
                self.send_json(s32_firewall.approve(
                    name=body.get("name"), port=body.get("port"),
                    alert_id=body.get("id")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/security/dismiss":
            try:
                import s32_firewall
                self.send_json(s32_firewall.dismiss(
                    alert_id=body.get("id"), name=body.get("name")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/security/propose":
            try:
                import s32_firewall
                self.send_json(s32_firewall.propose_action(
                    body.get("action", "kill"), pid=body.get("pid"),
                    name=body.get("name"), port=body.get("port")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/security/confirm":
            try:
                import s32_firewall
                self.send_json(s32_firewall.confirm_action(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/security/cancel":
            try:
                import s32_firewall
                self.send_json(s32_firewall.cancel_action(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/fs/confirm":
            try:
                import s32_filesystem
                self.send_json(s32_filesystem.confirm_write(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/fileop/confirm":
            try:
                import s32_filesystem
                self.send_json(s32_filesystem.confirm_file_op(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/fileop/cancel":
            try:
                import s32_filesystem
                self.send_json(s32_filesystem.cancel_file_op(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/fs/cancel":
            try:
                import s32_filesystem
                self.send_json(s32_filesystem.cancel_write(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/web/more":
            # "More results" under an image grid / video cards: the next batch of that search.
            try:
                import s32_web
                self.send_json(s32_web.web_more(body.get("sid")))
            except Exception as e:
                self.send_json({"error": str(e)[:200], "items": [], "more": False})
        elif self.path == "/api/web/save":
            # The ⬇ on an image-search picture or a video-search card (2026-10-29). Images
            # save straight away to Output\downloads\images; a video raises the usual
            # Confirm card (title, size, destination) and saves to Output\downloads\videos.
            if state.get("resetting"):
                self.send_json({"error": "factory reset in progress"}); return
            try:
                import s32_download
                u = str(body.get("url") or "").strip()
                if body.get("kind") == "video":
                    r = s32_download.propose_video_download(u, body.get("title") or None)
                else:
                    r = s32_download.propose_download(u, kind="web_image")
                self.send_json(r if isinstance(r, dict) else {"error": "no result"})
            except Exception as e:
                self.send_json({"error": str(e)[:200]})
        elif self.path == "/api/dl/confirm":
            try:
                import s32_download
                self.send_json(s32_download.confirm_download(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/ue/confirm":
            try:
                import s32_unreal
                self.send_json(s32_unreal.answer_confirm(body.get("token", ""), bool(body.get("run"))))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/ue/setup":
            try:
                import s32_unreal
                self.send_json(s32_unreal.run_setup(body.get("project", ""), bool(body.get("launch"))))
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)})
        elif self.path == "/api/code/verdict":
            try:
                import s32_code
                self.send_json(s32_code.record_verdict(body.get("slug", ""), body.get("filename", ""),
                                                       bool(body.get("works")), body.get("note", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/ue/verdict":
            try:
                import s32_unreal
                self.send_json(s32_unreal.record_verdict(body.get("task", ""), bool(body.get("works")),
                                                         body.get("note", "")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/activity/cancel":
            # The input bar's ✕ — cancels whatever the bar is showing.
            what = body.get("what", "")
            out = {}
            try:
                if what == "downloads":
                    import s32_download
                    out = s32_download.cancel_all_downloads()
                elif what == "batch":
                    import s32_comfyui
                    out = s32_comfyui.cancel_comfy_jobs("input bar")
            except Exception as e:
                out = {"error": str(e)}
            self.send_json(out)
        elif self.path == "/api/dl/cancel":
            try:
                import s32_download
                self.send_json(s32_download.cancel_download(token=body.get("token")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/sort/run":
            try:
                import s32_filesystem
                self.send_json(s32_filesystem.run_sort(mode=body.get("mode", "move")))
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/api/sort/cancel":
            try:
                import s32_filesystem
                self.send_json(s32_filesystem.cancel_sort())
            except Exception as e:
                self.send_json({"error": str(e)})
        elif self.path == "/chat" and state.get("resetting"):
            self.send_json({"error": "System 32 is factory resetting and will close in a moment."})
        elif self.path == "/chat":
            user_text = body.get("message","").strip()
            image_b64 = body.get("image")
            _docs = body.get("docs") or []
            _drop_txt, _dropped = _drop_note(body.get("dropped"))
            if not user_text and not _docs and not image_b64 and not _dropped:
                self.send_json({"error": "empty"}); return
            if not user_text:
                user_text = "Please read the attached file(s) and tell me what they are."

            state["cancel"] = False  # fresh turn — clear any prior stop request
            state["gen_cancel"] = False  # …and any prior ComfyUI cancel (⏹ or the bar's ✕)
            try:
                import s32_unreal as _ue
                _ue.new_code_task(user_text)   # fresh Unreal-code task + attempt budget per request
                import s32_code as _cd
                _cd.new_request()              # fresh run budget; code-friendly sampling if a project is active
                try:
                    import s32_web as _wb
                    _wb.new_request()          # fresh web-search budget for this question
                except Exception:
                    pass
                try:
                    import s32_findex as _fx
                    _fx.new_request()          # fresh search-this-computer budget
                except Exception:
                    pass
            except Exception:
                pass

            # If a file was attached, save it to ComfyUI input folder for workflow use
            if image_b64:
                try:
                    input_dir = CONFIG.get("comfy_image_input", "")
                    if input_dir and os.path.exists(input_dir):
                        raw = base64.b64decode(image_b64)
                        # Detect type from mime or filename
                        attached_mime = body.get("mime", "")
                        attached_name = body.get("filename", "attachment")
                        ext = os.path.splitext(attached_name)[1].lower() or ".bin"
                        ts  = time.strftime('%Y%m%d_%H%M%S')
                        fname = f"s32_attached_{ts}{ext}"
                        dest  = os.path.join(input_dir, fname)

                        _VID_EXTS = {".mp4",".webm",".mov",".avi",".mkv"}
                        _3D_EXTS_SET = {".glb",".gltf",".obj",".ply",".stl",".fbx",".usdz"}

                        if ext in _3D_EXTS_SET:
                            # Save 3D file directly
                            with open(dest, "wb") as f:
                                f.write(raw)
                            state["last_3d_path"] = dest
                            log(f"[S32] Attached 3D file saved to: {dest}")
                        elif ext in _VID_EXTS or (attached_mime and attached_mime.startswith("video/")):
                            # Save video directly
                            with open(dest, "wb") as f:
                                f.write(raw)
                            state["last_video_path"] = dest
                            state["last_image_path"] = None  # clear image for inject purposes
                            log(f"[S32] Attached video saved to: {dest}")
                        else:
                            # Image - normalize via PIL
                            if PILImage:
                                img = PILImage.open(io.BytesIO(raw)).convert("RGB")
                                fname = f"s32_attached_{ts}.png"
                                dest  = os.path.join(input_dir, fname)
                                img.save(dest, format="PNG")
                            else:
                                with open(dest, "wb") as f:
                                    f.write(raw)
                            state["last_image_path"]  = dest
                            state["last_image_bytes"] = raw
                            log(f"[S32] Attached image saved to: {dest}")
                except Exception as e:
                    log(f"[S32] Failed to save attached file: {e}")

            # A video / 3D model picked in the 🗂 browser arrives as a PATH, not bytes — it was never
            # made current, so 'turn this video into frames' ran on the workflow's stale placeholder
            # (2026-10-30). Stage it exactly like an uploaded one. Only a real file of those types,
            # and never one inside another person's private area.
            _picked = (body.get("path") or "") if not image_b64 else ""
            if _picked:
                try:
                    import shutil as _shp
                    from s32_dispatch import _someone_elses as _priv
                    _pext = os.path.splitext(_picked)[1].lower()
                    _pin = CONFIG.get("comfy_image_input", "")
                    _pkind = ("video" if _pext in {".mp4", ".webm", ".mov", ".avi", ".mkv"} else
                              "3d" if _pext in {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".usdz"} else "")
                    if _pkind and os.path.isfile(_picked) and _pin and os.path.isdir(_pin) and not _priv(_picked):
                        _pdst = os.path.join(_pin, f"s32_attached_{time.strftime('%Y%m%d_%H%M%S')}{_pext}")
                        _shp.copyfile(_picked, _pdst)
                        if _pkind == "video":
                            state["last_video_path"] = _pdst
                            state["last_image_path"] = None
                        else:
                            state["last_3d_path"] = _pdst
                        log(f"[S32] Attached {_pkind} (picked) staged: {_picked} -> {_pdst}")
                except Exception as e:
                    log(f"[S32] Failed to stage picked file: {e}")

            # Decide how the attachment reaches it. A text-only model 500s on
            # any image, and video/3D bytes are never valid image input — so we
            # only hand the model an image when a vision projector is loaded.
            # Otherwise the file is already saved + set as the current image/
            # video/3D, and we annotate its turn so it operates on it with tools.
            att_note = ""
            _llm_image = None
            if image_b64:
                _aext = os.path.splitext(body.get("filename", "") or "")[1].lower()
                _amime = body.get("mime", "") or ""
                if _aext in {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".usdz"}:
                    _akind, _apath = "3d", state.get("last_3d_path")
                elif _aext in {".mp4", ".webm", ".mov", ".avi", ".mkv"} or _amime.startswith("video/"):
                    _akind, _apath = "video", state.get("last_video_path")
                else:
                    _akind, _apath = "image", state.get("last_image_path")
                _vision_ok = state.get("vision_ready", False)
                _where = _apath or CONFIG.get("comfy_image_input", "the ComfyUI input folder")
                if _akind == "image" and _vision_ok:
                    _llm_image = image_b64        # it can actually see it
                    att_note = (f"\n\n[The user attached an IMAGE (you can see it), saved to: {_where} "
                                f"and it is ALREADY the current image. To act on it (remove background, "
                                f"upscale, image-to-video, image-to-3D, any workflow), call run_workflow "
                                f"directly — it uses this image automatically. Do NOT call "
                                f"show_images to 'grab' it, and do not pass an image path.]")
                elif _akind == "video":
                    att_note = (f"\n\n[The user attached a VIDEO, saved to: {_where} and set as the "
                                f"current video. You can't watch it, but you can run video workflows "
                                f"on it (e.g. upscale, video-to-video, extract frames) — call the tool "
                                f"that matches their request.]")
                elif _akind == "3d":
                    att_note = (f"\n\n[The user attached a 3D model, saved to: {_where} and set as the "
                                f"current 3D file. Operate on it with 3D tools if their request calls "
                                f"for it.]")
                else:
                    att_note = (f"\n\n[The user attached an IMAGE, saved to: {_where} and set as the "
                                f"current image. You can't see its contents (no vision model loaded) "
                                f"and you CANNOT read it with read_file (it's binary pixels, not text). "
                                f"But you CAN operate on it: remove background, upscale, image-to-video, "
                                f"image-to-3D, or feed it to a workflow — call the tool matching their "
                                f"request. If they only want it described, say a vision model must be "
                                f"loaded for that.]")

            # Files dragged in through /api/drop (any type). The first video / 3D model (and, with no
            # other image attached, the first picture) also becomes the CURRENT one, copied into
            # ComfyUI's input folder exactly like an attachment, so workflows pick it up.
            if _dropped:
                try:
                    import shutil as _sh
                    _in = CONFIG.get("comfy_image_input", "")
                    _ts = time.strftime('%Y%m%d_%H%M%S')
                    _done = set()
                    for _d in _dropped:
                        _k, _p = _d["kind"], _d["path"]
                        _ext = os.path.splitext(_p)[1].lower()
                        if _k in _done or not (_in and os.path.isdir(_in)):
                            continue
                        if _k == "video":
                            _dst = os.path.join(_in, f"s32_attached_{_ts}{_ext}")
                            _sh.copyfile(_p, _dst)
                            state["last_video_path"] = _dst
                            state["last_image_path"] = None
                        elif _k == "3d" and _ext in {".glb", ".gltf", ".obj", ".ply", ".stl", ".fbx", ".usdz"}:
                            _dst = os.path.join(_in, f"s32_attached_{_ts}{_ext}")
                            _sh.copyfile(_p, _dst)
                            state["last_3d_path"] = _dst
                        elif _k == "image" and not image_b64 and PILImage and _ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".tga"}:
                            _dst = os.path.join(_in, f"s32_attached_{_ts}.png")
                            PILImage.open(_p).convert("RGB").save(_dst, format="PNG")
                            state["last_image_path"] = _dst
                        else:
                            continue
                        _done.add(_k)
                        log(f"[S32][DROP] {_d.get('name')} is now the current {_k} ({_dst})")
                except Exception as _de:
                    log(f"[S32][DROP] couldn't set the dropped file as current: {_de}")
                att_note += _drop_txt

            # Phase 1: read dropped-in document/code files into THIS turn so it
            # can review or revise them. Contents are injected directly (not via
            # read_file tool calls) so it works regardless of tool-call reliability.
            # For large drops, s32_docs builds a compact structure map + only the
            # chunks most relevant to the request, so the prompt stays small (the
            # whole-blob version caused the cancellable_post socket timeout).
            doc_prefix = ""
            if _docs:
                if _s32_docs is not None:
                    try:
                        _att_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".attachments")
                        doc_prefix, _manifest = _s32_docs.process_attachments(_docs, user_text, _att_dir)
                        state["attachments"] = _manifest
                    except Exception as _de:
                        log(f"[S32] s32_docs failed ({_de}); using simple inline injection")
                        doc_prefix = ""
                if not doc_prefix:
                    # Fallback: simple capped inline injection (module missing or errored)
                    PER_FILE_CAP = 60000
                    TOTAL_CAP    = 160000
                    parts = []
                    used  = 0
                    for i, d in enumerate(_docs, 1):
                        name    = d.get("name", f"file{i}")
                        content = d.get("content", "") or ""
                        remaining = TOTAL_CAP - used
                        if remaining <= 0:
                            parts.append(f"===== FILE {i}/{len(_docs)}: {name} =====\n[skipped — attachment size limit reached]")
                            continue
                        cap       = min(PER_FILE_CAP, remaining)
                        truncated = len(content) > cap
                        snippet   = content[:cap]
                        used     += len(snippet)
                        note      = "\n[...truncated — ask to show more of this file if needed]" if truncated else ""
                        parts.append(f"===== FILE {i}/{len(_docs)}: {name} ({len(content)} chars) =====\n{snippet}{note}")
                    doc_prefix = (
                        f"[The user attached {len(_docs)} file(s). Read them in order; their full "
                        f"contents follow. If asked to revise or add to a file, write the WHOLE revised "
                        f"file with write_code — it appears in chat with a Download button. Never claim "
                        f"you edited a file on disk.]\n\n"
                        + "\n\n".join(parts)
                        + "\n\n[End of attached files.]\n\n"
                    )

            # Compact history entry (not the full dumps), with a note of filenames.
            _hist_text = user_text
            if _docs:
                _names = ", ".join(d.get("name", "?") for d in _docs)
                _hist_text = f"{user_text}\n[attached {len(_docs)} file(s): {_names}]"
            if _dropped:      # paths too, so later turns can still use them
                _hist_text += "\n[dropped in: " + "; ".join(f"{d.get('name')} → {d['path']}" for d in _dropped) + "]"
            state["turn_msg_start"] = len(state["messages"])     # native tool turns build from here
            state["turn_req_full"] = None
            state["messages"].append({"role": "user", "content": _hist_text})

            # Training record of this whole turn (s32_training): every step, tool result and
            # the final answer, graded at the end — the "what worked" data.
            _traj = {"cancel": False}
            try:
                import s32_training as _trn
                _traj["id"] = _trn.turn_begin((doc_prefix + user_text) if doc_prefix else user_text,
                                              user_image=bool(image_b64))
            except Exception:
                _trn = None
                _traj["id"] = ""

            def _tr(fn, *a):
                try:
                    if _trn:
                        getattr(_trn, fn)(*a)
                except Exception:
                    pass

            def _run_tool(_name, _params, _img):
                _res = list(dispatch_tool(_name, _params, _img))
                _tr("note_result", _name, _res)
                return _res

            try:
                # Stream the whole turn so long generations show progress live in
                # the chat bubble instead of looking frozen. Headers go out before
                # the (potentially slow) generation; tokens are forwarded as they
                # arrive via the on_delta callback.
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                streaming_started = True

                _client_gone = {"v": False}
                def chunk(obj):
                    # After the client disconnects (Stop / closed tab), further writes
                    # raise ConnectionAborted and were crashing the turn mid-chain,
                    # leaving half-finished state to bleed into the next request.
                    # Go silent instead: work already dispatched finishes server-side,
                    # results still reach the UI via the background results feed.
                    if _client_gone["v"]:
                        return
                    try:
                        data = json.dumps(obj).encode()
                        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
                        self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                        _client_gone["v"] = True
                        log("[S32] Client disconnected mid-stream — going silent for this turn.")

                _got_delta = {"v": False}
                def _on_delta(t):
                    if not t:
                        return
                    if not _got_delta["v"]:
                        _got_delta["v"] = True
                        chunk({"phase": "start"})   # open a fresh live bubble
                    chunk({"phase": "delta", "text": t})

                # When files are attached they're injected into the prompt (not read via
                # read_file), so no tool traces fire — leaving only the spinner during a
                # long summarize/analyze pass, with no sign of what it's working from.
                # Drop a green breadcrumb naming what it ingested, in attachment order,
                # so the turn shows activity the instant it starts.
                if _traj.get("id"):
                    chunk({"phase": "turn", "id": _traj["id"]})   # for the 👍 / 👎 under the turn
                if _docs:
                    _rd = ", ".join(d.get("name", "?") for d in _docs)
                    chunk({"phase": "trace", "text": f"read {len(_docs)} file(s): {_rd}"})

                state["turn_req_full"] = (doc_prefix + user_text if doc_prefix else user_text) + att_note
                _cards0 = state.get("confirm_cards", 0)
                spoken, tool_name, tool_params = chat(
                    (doc_prefix + user_text if doc_prefix else user_text) + att_note,
                    _llm_image, on_delta=_on_delta)
                _prompt_cache["locked"] = True  # lock prompt for duration of this turn
                _th = _pop_thought(user_text)
                if _th:
                    chunk({"phase": "thought", "text": _th})

                # It thought, then stopped without a word or a tool call (2026-10-29: "how
                # best to make money in oblivion" → a thought, then silence). The chain has
                # a fallback for this after a tool step; the FIRST reply had none, so the
                # turn just ended. Ask once for the reply itself; the empty step is dropped
                # from the training record, so only the real answer is learned.
                if not tool_name and not (spoken or "").strip() and not state.get("cancel"):
                    log("[S32] First reply was empty — asking once for the reply")
                    _tr("drop_last_step", "thought, then wrote no reply")
                    try:
                        spoken, tool_name, tool_params = chat(
                            "[Your reply came out empty — you finished thinking but wrote nothing. "
                            "Reply to the operator now.]", None, on_delta=_on_delta, auto_vision=False)
                    except GenerationCancelled:
                        _traj["cancel"] = True
                        spoken, tool_name, tool_params = "", None, None
                    except Exception as _ee:
                        log(f"[S32] empty-reply retry failed: {_ee}")
                        spoken, tool_name, tool_params = "", None, None
                    _th = _pop_thought("empty-reply retry")
                    if _th:
                        chunk({"phase": "thought", "text": _th})
                    if not tool_name and not (spoken or "").strip() and not state.get("cancel"):
                        _tr("flag", "fallback_reply")
                        spoken = ("(I thought it over but my reply came out empty — "
                                  "please ask me again.)")

                # Said a Confirm card is up without calling the tool — ask once for the call.
                if not tool_name and not state.get("cancel") and _claims_missing_card(spoken, _cards0):
                    log(f"[S32] Said a Confirm card is up but called no tool ({spoken[:60]!r}) — asking once")
                    chunk({"phase": "trace", "text": "⚠ said a card is up without raising it — asking once"})
                    try:
                        _sp2, _t2, _p2 = chat(_CARD_NUDGE, None, on_delta=_on_delta, auto_vision=False)
                        if _t2:
                            if spoken:
                                state["messages"].append({"role": "assistant", "content": spoken})
                            spoken, tool_name, tool_params = _sp2, _t2, _p2
                        else:
                            log("[S32] card nudge: still no tool call")
                    except GenerationCancelled:
                        _traj["cancel"] = True
                    except Exception as _ce:
                        log(f"[S32] card nudge failed: {_ce}")
                    _th = _pop_thought("card nudge")
                    if _th:
                        chunk({"phase": "thought", "text": _th})

                # Has a tool call - if model gave no spoken text, generate a fallback
                _ack_filler = False
                if tool_name and not spoken:
                    _ack_filler = True
                    _ack = {
                        "generate_image": "Got it, generating that image now...",
                        "generate_video": "Got it, generating that video now...",
                        "train_lora":     "Starting LoRA training now...",
                        "add_to_dataset": "Opening the dataset panel...",
                        "list_directory": "Looking that up...",
                        "find_file":      "Searching for that file...",
                        "read_file":      "Reading that file...",
                        "get_drives":     "Checking your drives...",
                        "get_system_info":"Pulling up your system info...",
                        "list_workflows": "Checking available workflows...",
                        "run_workflow":   "Running that workflow now...",
                        "scan_comfy_logs":        "Scanning ComfyUI logs...",
                        "install_missing_nodes":  "Installing missing nodes, this may take a minute...",
                        "show_images":              "Let me pull those up...",
                        "memory_read":              "Reading my memories...",
                    }
                    spoken = _ack.get(tool_name, "") if tool_name != "memory_write" else ""

                # Store only the model's REAL preamble in history — NEVER the hardcoded ack
                # filler. Writing the filler ("Running that workflow now...") into history
                # taught the model, in-context, to PARROT it as its response instead of
                # emitting the tool call — the more tool calls in a session, the worse it got
                # (identical on any model: it's context poisoning, not model quality). The
                # filler is still spoken + shown below; it just must not enter its context.
                if spoken and not _ack_filler:
                    state["messages"].append({"role": "assistant", "content": spoken})

                # If tokens streamed, the text is already on screen. Otherwise (tool
                # ack, or a tool-call with no preamble) push the text as a bubble now.
                if spoken and not _got_delta["v"]:
                    chunk({"phase": "text", "response": spoken, "tool": tool_name})
                if spoken:
                    _auto_speak(spoken)

                if not tool_name:
                    # Pure conversation — close the stream.
                    try:
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                        pass
                    _prompt_cache["locked"] = False
                    return

                img_bytes = base64.b64decode(image_b64) if image_b64 else None
                chunk({"phase": "trace", "text": _trace_label(tool_name, tool_params)})
                all_results = _run_tool(tool_name, tool_params, img_bytes)
                for result in all_results:
                    if _should_show_result(tool_name, result):
                        chunk({"phase": "result", "result": result})

                # For generation tools, remove ack from history
                _GENERATION_TOOLS = {"run_workflow", "generate_image", "generate_video", "train_lora"}
                # Silent tools - run without followup
                _SILENT_TOOLS = {"memory_write", "install_missing_nodes"}


                try:
                    _first_count = int(tool_params.get("count", 1) or 1)
                except Exception:
                    _first_count = 1
                _first_is_video = (tool_params.get("target") == "video") or (tool_name == "generate_video")
                _planned = _planned_gen_steps(user_text)
                _first_wants_video = _planned > 1   # kept name: "request has a FURTHER step"

                if tool_name in _SILENT_TOOLS:
                    pass   # silent first tool (memory_write etc.) — no chain

                elif (tool_name in _GENERATION_TOOLS) and (_first_is_video or _first_count > 1 or not _first_wants_video):
                    # A first-tool generation is TERMINAL unless it's an image gen AND the
                    # request explicitly asks for a video next (the image→video case). Video,
                    # batch, and plain single-step image work (remove background, upscale, make
                    # a 3D, generate one image) all end here. Entering the chain for these is
                    # what made it spawn unwanted follow-up workflows (bg-remove → img-to-3d)
                    # and clobber last_image_path between turns — which also broke attached-
                    # image injection.
                    if state["messages"] and state["messages"][-1]["role"] == "assistant":
                        state["messages"].pop()
                    _record_outcome(tool_name, tool_params, all_results)
                    log(f"[S32] First-tool generation terminal (video={_first_is_video} "
                        f"batch={_first_count > 1} planned_steps={_planned}) — no chain.")

                else:
                    # Both generation tools AND retrieval/action tools chain from here.
                    # A first-tool generation ("generate an image, THEN a video") used to
                    # stop at this point WITHOUT entering the loop below, so the second
                    # generation never ran — succession was impossible. Now generation
                    # tools also enter the chain (bounded by _MAX_GENS inside the loop), so
                    # multi-step creative tasks complete. A single generation still ends
                    # cleanly: after the one image it has nothing left to call, narrates a
                    # closing line, and the no-tool break ends the turn.
                    if tool_name in _GENERATION_TOOLS:
                        if state["messages"] and state["messages"][-1]["role"] == "assistant":
                            state["messages"].pop()
                        _record_outcome(tool_name, tool_params, all_results)
                    # Multi-step autonomous tool chain
                    # It can chain tool calls in sequence - read a file, analyze it,
                    # call another tool, etc. Each step gets the previous result as context.
                    _tier = get_hardware().get("tier", "mid")
                    MAX_CHAIN = 20 if _tier == "high" else 12 if _tier == "mid" else 8
                    chain_step = 0
                    _recent_tools = []  # for repetition detection
                    _recent_sigs  = []  # exact tool+target signatures, for tight loop detection
                    # The turn's FIRST call counts too — list_directory(home) ran twice back to back
                    # before the breaker noticed (2026-10-29).
                    if tool_name and isinstance(tool_params, dict) and tool_name not in _GENERATION_TOOLS:
                        _recent_sigs.append("%s|%s|%s|%s" % (
                            tool_name, (tool_params.get("path") or tool_params.get("query")
                                        or tool_params.get("folder") or tool_params.get("name")
                                        or _fop_key(tool_name, tool_params)),
                            (tool_params.get("offset", "") or tool_params.get("target", "")),
                            (tool_params.get("positive", "") or "")[:40]))
                    # First generation (if any) already ran, so count it against the cap.
                    _gen_count = 1 if tool_name in _GENERATION_TOOLS else 0
                    # Real-time interaction tools are legitimately repetitive (playing a
                    # game = look, act, look, act…), so they're exempt from the repeat/loop
                    # chain-breakers. MAX_CHAIN still caps the turn.
                    # run_unreal_python is iterative by design (fix → rerun); its own
                    # controller caps attempts at 5 and refuses identical code.
                    _REALTIME_TOOLS = {"game_input", "see_screen", "run_unreal_python",
                                       "code_plan", "write_code", "run_code", "edit_code"}
                    current_tool = tool_name
                    current_params = tool_params or {}
                    current_results = all_results

                    def _step_label(_t, _p):
                        _n = (_p or {}).get("name")
                        return f"{_t} ({_n})" if _n else _t
                    # Generation steps finished so far THIS request, in order. Without this,
                    # every image result read identically ("the image is finished…"), so after
                    # bg-remove it couldn't tell that step was done and re-ran it.
                    _done_gens = [_step_label(tool_name, tool_params)] if tool_name in _GENERATION_TOOLS else []
                    _retried_step = -1   # chain step that already got its one "you didn't call it" retry

                    _forced_next = {"call": None}
                    # NATIVE TOOL TURN: this turn's tool calls (assistant) and results (tool), sent
                    # back to the model in its trained format so each step SEES every earlier step
                    # (and llama-server reuses its cache). Falls back to the old one-message-per-
                    # step format if the server refuses it. Not for gameplay (screenshots each step).
                    _turn = []
                    _native = {"on": not state.get("native_turns_off") and not state.get("s32_playing"),
                               "cur": 0, "logged": -1, "say": spoken or "", "n": 0}
                    _TURN_BUDGET = 100000

                    def _tc(_t, _p):
                        _native["n"] += 1
                        return {"id": f"call_{_native['n']}", "type": "function",
                                "function": {"name": _t, "arguments": json.dumps(_p or {}, ensure_ascii=False)}}

                    def _turn_add(_say, _calls):
                        """_calls: [(tool, params, result_text)] — one assistant message + a tool
                        message per call. Oldest results shrink once the turn gets very long."""
                        _tcs = [_tc(_t, _p) for _t, _p, _x in _calls]
                        _turn.append({"role": "assistant", "content": _say or "", "tool_calls": _tcs})
                        for _c, (_t, _p, _x) in zip(_tcs, _calls):
                            _turn.append({"role": "tool", "tool_call_id": _c["id"], "name": _t,
                                          "content": str(_x or "(done — no output)")})
                        _tot = sum(len(m.get("content") or "") for m in _turn if m["role"] == "tool")
                        for m in _turn:
                            if _tot <= _TURN_BUDGET:
                                break
                            if m["role"] == "tool" and len(m["content"]) > 600:
                                _tot -= len(m["content"]) - 400
                                m["content"] = (m["content"][:400] + " …(older result shortened to save "
                                                "room — read it again only if you really need the rest)")

                    def _ask(_note, _img=None, _legacy=None, _interrupt=False, **kw):
                        """One model call for the chain: native turn if on, else the old message."""
                        if _native["on"]:
                            try:
                                return chat(_note, _img, auto_vision=False, turn=_turn,
                                            note_as_user=_interrupt, **kw)
                            except GenerationCancelled:
                                raise
                            except Exception as _ne:
                                log(f"[S32] switching to the one-message-per-step format for this session "
                                    f"(native tool turns not taken — status {getattr(_ne, 'code', '?')})")
                                _native["on"] = False
                                state["native_turns_off"] = True
                        return chat(_legacy if _legacy is not None else _note, _img,
                                    auto_vision=bool(state.get("s32_playing")), **kw)
                    # Results from EARLIER steps of this turn. The chain used to send her only the
                    # latest result — chain results never go into history — so a file she read was
                    # gone one step later (2026-10-29: read Pipboy.cpp → searched → re-read
                    # Pipboy.cpp → repeat break; the words-only answer then said 'I'll start by
                    # looking at the files…'). Newest first, bounded.
                    _earlier = []                       # [(key, label, text)]
                    _earlier_txt = {"t": ""}
                    _EARLIER_BUDGET, _EARLIER_ITEM = 40000, 12000
                    _NO_CARRY = {"see_screen", "game_input", "start_screen_capture", "stop_screen_capture"}

                    def _lbl(_t, _p):
                        _p = _p or {}
                        def _sh(x, n=120):
                            x = str(x or "")
                            return x if len(x) <= n else "…" + x[-(n - 1):]   # keep the END — the file name
                        _k = _sh(_p.get("path") or _p.get("query") or _p.get("folder") or _p.get("name")
                                 or _p.get("url") or _p.get("project") or "")
                        if _p.get("query") and _p.get("path"):        # two searches in one folder ≠ the same
                            _k = f"{str(_p['query'])[:80]} in {_sh(_p['path'], 90)}"
                        if _p.get("a") and _p.get("b"):
                            _k = f"{_sh(_p['a'], 80)} vs {_sh(_p['b'], 80)}"
                        _o = _p.get("offset")
                        return f"{_t}({_k}{', offset ' + str(_o) if _o else ''})"

                    def _earlier_block(_skip):
                        _out, _left = [], _EARLIER_BUDGET
                        for _k, _l, _x in reversed(_earlier):
                            if _k in _skip:
                                continue
                            if _left > 400:
                                _piece = _x[:min(_EARLIER_ITEM, _left)]
                                _cut = " …(cut)" if len(_piece) < len(_x) else ""
                            else:
                                _piece, _cut = _x[:300], " …(older — shortened; read it again only if you need the rest)"
                            _left -= len(_piece)
                            _out.append(f"[{_l} result: {_piece}{_cut}]")
                        if not _out:
                            return ""
                        return ("[EARLIER RESULTS THIS TURN — still valid, newest first. Use them; don't "
                                "fetch these again:]\n" + "\n".join(_out) + "\n[END EARLIER RESULTS]\n\n")

                    def _answer_from_what_you_have(_tool):
                        """A loop/repeat breaker used to END the turn with a canned line and no
                        answer (2026-10-28: 3 good web searches, then '(You already have that
                        content above…)'). Give her ONE last call to answer from what she
                        gathered; any tool call in it is ignored. True if she answered."""
                        if _tool in _GENERATION_TOOLS or state.get("cancel"):
                            return False
                        try:
                            _stop = ("[STOP — no more tool calls. You already have what you need above. "
                                     "Answer the operator now from it; if it doesn't fully cover the "
                                     "question, say what's missing.]")
                            _sp, _t, _p = _ask(_stop, None, _legacy=_chain_msg + "\n" + _stop, _interrupt=True)
                        except Exception as _fe:
                            log(f"[S32] final-answer call failed: {_fe}")
                            return False
                        _tr("flag", "forced_answer")
                        _th2 = _pop_thought("final answer")
                        if _th2:
                            chunk({"phase": "thought", "text": _th2})
                        if _t and (_t == _tool or _t in _GENERATION_TOOLS):
                            # Words WITH the same tool again are narration ("Let me check what's in
                            # the folder…"), not an answer — the turn used to end on that line
                            # (2026-10-29). Drop it; the words-only ask below gets the real answer.
                            log(f"[S32] final-answer call narrated + repeated {_t} — not an answer")
                            _sp, _t, _p = "", None, None
                        if _sp and _sp.strip():
                            state["messages"].append({"role": "assistant", "content": _sp})
                            chunk({"phase": "followup", "response": _sp})
                        # Asked to stop LOOPING on one tool, she moved on to a different one
                        # (2026-10-29: stuck re-reading FRIK.psc, then called write_code — which
                        # used to be thrown away, so no code was ever written). Do that step.
                        if _t and _t != _tool and _t not in _GENERATION_TOOLS and not state.get("cancel"):
                            _forced_next["call"] = ("", _t, _p)     # its words (if any) are shown above
                            log(f"[S32] final-answer call moved on to {_t} — continuing with it")
                            return True
                        if not (_sp and _sp.strip()) and not state.get("cancel"):
                            # She answered "stop and answer" with the SAME tool again (2026-10-29:
                            # a 3rd list_directory) — the turn ended on a canned line and the
                            # operator got nothing. One more ask, with tools switched off.
                            log(f"[S32] final-answer call repeated {_t or 'nothing'} — asking for words only")
                            try:
                                _wo = ("[Tools are OFF for this reply. In plain words, tell the "
                                       "operator: what you found so far, what's blocking the "
                                       "task, and the exact tool call you'll make next (or what "
                                       "you need from them). Only promise what your tools can "
                                       "really do — you can't compile C++, build mods or test "
                                       "in-game.]")
                                _sp, _t, _p = _ask(_wo, None, _legacy=_earlier_txt["t"] + _wo, no_tools=True,
                                                   _interrupt=True)
                            except Exception as _fe:
                                log(f"[S32] words-only answer failed: {_fe}")
                                return False
                            _th2 = _pop_thought("final answer (words only)")
                            if _th2:
                                chunk({"phase": "thought", "text": _th2})
                            if _sp and _sp.strip():
                                state["messages"].append({"role": "assistant", "content": _sp})
                                chunk({"phase": "followup", "response": _sp})
                        return bool(_sp and _sp.strip())

                    while chain_step < MAX_CHAIN:
                        chain_step += 1
                        if state.get("cancel"):
                            _traj["cancel"] = True
                            chunk({"phase": "followup", "response": "⏹ Stopped."})
                            break
                        summary = _tool_result_summary(current_tool, current_results[0] if current_results else {})

                        # NOTE: do NOT pre-inject result into state["messages"] here.
                        # chat() appends the user message itself. Double-injecting
                        # creates consecutive user turns which breaks Qwen's jinja template.
                        # Re-anchor the original request every step. On a long
                        # chain the first user turn scrolls out of the 24-message
                        # history window, and once it can't see what it was asked
                        # it stops calling tools. Restating it keeps it on task.
                        _task_reminder = f"\n[Still working on: {user_text[:200]}]" if user_text else ""
                        _progress = ""
                        if _done_gens:
                            _progress = (f"\n[Steps already DONE for this request, in order: "
                                         f"{' → '.join(_done_gens)}. Do NOT run any of these again. "
                                         f"Call the NEXT step that isn't done yet — or, if every step "
                                         f"is done, tell the operator it's finished.]")
                        # Its code results carry the traceback AND the failing script —
                        # they need far more room than a one-line tool result.
                        # Web results carry page text she needs to answer from — at 600 chars she
                        # saw barely two results, no pages, and kept searching (2026-10-28).
                        _cap = (7000 if current_tool in ("run_unreal_python", "write_code", "run_code", "edit_code")
                                else 9000 if current_tool in ("web_search", "web_fetch")
                                else 3000 if current_tool in ("web_image_search", "web_video_search")
                                else 4000 if current_tool == "search_computer"
                                else 7000 if current_tool == "import_code"
                                else 12000 if current_tool in ("read_file", "show_file", "compare_code", "search_code") else 8000 if current_tool == "explore_tree" else 600)
                        # The other reads she asked for in the same breath (s32_llm stores them).
                        _xtra = ""
                        _cur = [(_lbl(current_tool, current_params), summary[:_cap])] \
                            if current_tool not in _NO_CARRY else []
                        _calls_now = [(current_tool, current_params, summary[:_cap])]
                        for _xn, _xp in (state.pop("extra_calls", None) or [])[:6]:
                            chunk({"phase": "trace", "text": f"step {chain_step}: " + _trace_label(_xn, _xp)})
                            _xr = _run_tool(_xn, _xp, None)
                            _xs = _tool_result_summary(_xn, _xr[0] if _xr else {})
                            _xtra += f"\n[{_lbl(_xn, _xp)} result: {_xs[:12000]}]"
                            _cur.append((_lbl(_xn, _xp), _xs[:12000]))
                            _calls_now.append((_xn, _xp, _xs[:12000]))
                            _recent_sigs.append("%s|%s|%s|%s" % (            # same shape as the loop's _sig
                                _xn, (_xp.get("path") or _xp.get("query") or _xp.get("folder") or _xp.get("name") or _fop_key(_xn, _xp)),
                                (_xp.get("offset", "") or _xp.get("target", "")), (_xp.get("positive", "") or "")[:40]))
                        if _native["logged"] != _native["cur"]:
                            _native["logged"] = _native["cur"]
                            _turn_add(_native["say"], _calls_now)
                        _keys = {k for k, _ in _cur}
                        _eb = _earlier_block(_keys)
                        _chain_msg = (f"{_eb}[{_lbl(current_tool, current_params)} result: "
                                      f"{summary[:_cap]}]{_xtra}{_task_reminder}{_progress}")
                        _earlier[:] = [e for e in _earlier if e[0] not in _keys] + [(k, k, x) for k, x in _cur]
                        _earlier_txt["t"] = _earlier_block(set())

                        # Ask it what to do next - respond, call another tool, or both
                        # If it just looked at the screen, pass that frame as vision input
                        _chain_img = None
                        if current_tool == "see_screen" and state.get("last_image_bytes"):
                            _chain_img = state["last_image_bytes"]
                        # Ambient screen vision only during gameplay. In a normal tool chain
                        # (e.g. image→video) auto-injecting a screenshot of its own displayed
                        # output made it try to 'grab' it instead of continuing — so suppress it.
                        _game_active = bool(state.get("s32_playing"))
                        try:
                            next_spoken, next_tool, next_params = _ask(   # the request is in the turn already
                                _progress.strip(), _chain_img, _legacy=_chain_msg)
                        except GenerationCancelled:
                            _traj["cancel"] = True
                            chunk({"phase": "followup", "response": "⏹ Stopped."})
                            break
                        except Exception as chain_err:
                            _tr("flag", "reconnected")
                            log(f"[S32] Chain disconnected on step {chain_step}: {chain_err}")
                            # Build context-aware recovery from nav_state
                            nav = state.get("nav_state", {})
                            _task   = nav.get("task")
                            _folder = nav.get("folder")
                            _page   = nav.get("page", 1)
                            _search = nav.get("search_term")
                            _file   = nav.get("file_path")
                            _tool   = nav.get("tool") or current_tool
                            if _task == "browsing" and _folder:
                                _ctx = f"browsing folder: {_folder} (page {_page})"
                            elif _task == "searching" and _search:
                                _ctx = f"searching for: '{_search}'"
                            elif _task == "reading" and _file:
                                _ctx = f"reading file: {_file}"
                            elif _task == "generating":
                                _ctx = "running a generation workflow"
                            else:
                                _ctx = f"running tool: {_tool}"
                            # Pull last few session log lines for orientation
                            _recent_log = ""
                            try:
                                if os.path.exists(SESSION_LOG_FILE):
                                    with open(SESSION_LOG_FILE, "r", encoding="utf-8") as _lf:
                                        _log_lines = _lf.readlines()
                                    _recent_log = "".join(_log_lines[-8:]).strip()[:400]
                            except Exception:
                                pass
                            _recovery_msg = (
                                f"[RECONNECT] Connection dropped on chain step {chain_step}. "
                                f"You were {_ctx}. "
                                f"Last tool result: [{_tool} result: {summary[:300]}]. "
                                + (f"Recent session context: {_recent_log}. " if _recent_log else "")
                                + f"Resume from where you left off — do not restart from scratch."
                            )
                            log(f"[S32] Disconnect recovery context: {_ctx}")
                            # Clear the corrupted history before retry
                            state["messages"] = []
                            state["hist_start"] = 0
                            try:
                                time.sleep(3)
                                next_spoken, next_tool, next_params = chat(_recovery_msg)
                            except Exception:
                                chunk({"phase": "followup", "response": f"Lost connection while {_ctx}. Please resend your last message to continue."})
                                break

                        _th = _pop_thought(f"chain step {chain_step}")
                        if _th:
                            chunk({"phase": "thought", "text": _th})

                        # Described-but-didn't-call guard. Its reasoning (thoughts.log) showed
                        # it DECIDES the right next step, then emits prose ("Background removed —
                        # now turning it into 3D:") with no tool call — a false completion that
                        # ends the chain. Scoped tightly (a regression came from a broad narration
                        # nudge before): ONLY in a generation chain with planned steps still left,
                        # ONCE per step; its prose is discarded (never shown, never in history).
                        # Same failure outside generation chains (2026-10-12: "Player Start's at
                        # (-100, 0, 150). Spawning all 12:" — then nothing): a reply that ENDS in a
                        # colon is announcing an action it didn't take. Once per step.
                        _card_claim = (not next_tool) and _claims_missing_card(next_spoken, _cards0)
                        _promised = bool(next_spoken) and (next_spoken.rstrip().endswith(":")
                                                           or _announces_action(next_spoken)
                                                           or _card_claim)
                        if (not next_tool) and next_spoken and (1 <= _gen_count < _planned or _promised) \
                                and _retried_step != chain_step and not state.get("cancel"):
                            _retried_step = chain_step
                            log(f"[S32] Chain step {chain_step}: described the next step without "
                                f"calling it ({next_spoken[:60]!r}) — asking once more.")
                            chunk({"phase": "trace", "text": "⚠ described the next step without calling it — retrying once"})
                            _tr("drop_last_step", "described the next step without calling it")
                            try:
                                _nudge = (_CARD_NUDGE if _card_claim else
                                          "[You replied with text but did NOT call a tool. "
                                          "The next step is NOT done yet. Call " +
                                          ("run_workflow" if 1 <= _gen_count < _planned else "the tool") +
                                          " for it now — no text.]")
                                next_spoken, next_tool, next_params = _ask(
                                    _nudge, _chain_img, _legacy=_chain_msg + "\n" + _nudge, _interrupt=True)
                            except GenerationCancelled:
                                _traj["cancel"] = True
                                chunk({"phase": "followup", "response": "⏹ Stopped."})
                                break
                            except Exception as _re_err:
                                log(f"[S32] retry failed: {_re_err}")
                                next_spoken, next_tool, next_params = "", None, None
                            _th = _pop_thought(f"chain step {chain_step} retry")
                            if _th:
                                chunk({"phase": "thought", "text": _th})

                        # Send spoken response if any. Append to history always, but only
                        # SHOW it here when it's terminal (no tool) or paired with a silent
                        # tool — the generation/other tool paths below emit the same text as
                        # a "text" bubble tied to the tool, so showing it here too double-prints.
                        if next_spoken:
                            state["messages"].append({"role": "assistant", "content": next_spoken})
                            if (not next_tool) or (next_tool in _SILENT_TOOLS):
                                chunk({"phase": "followup", "response": next_spoken})

                        # If no next tool call, the chain is complete. (No narration
                        # 'nudging' — that mechanism, added while working on the game
                        # sequence, normalized mid-game narration and added an extra
                        # round-trip per narrated turn, slowing gameplay. It should just
                        # act; when it stops emitting tools, the turn ends.)
                        if not next_tool:
                            if not (next_spoken or "").strip() and not state.get("cancel"):
                                # It thought until the token limit (or said nothing) after a
                                # tool step — the turn used to end in silence (2026-10-27, a
                                # calculator that ran clean and then… nothing). Say where
                                # things stand instead of leaving the operator waiting.
                                _r0 = current_results[0] if current_results and isinstance(current_results[0], dict) else {}
                                if current_tool in ("write_code", "run_code") and _r0.get("filename"):
                                    _fb = (f"({_r0['filename']} " + ("ran clean" if _r0.get("ok") else "is saved")
                                           + " — it's on the card above, ready to Download.)")
                                else:
                                    _fb = "(Done — the results are above.)"
                                log(f"[S32] Chain step {chain_step}: empty reply after {current_tool} — showing fallback")
                                _tr("flag", "fallback_reply")
                                chunk({"phase": "followup", "response": _fb})
                                state["messages"].append({"role": "assistant", "content": _fb})
                            break

                        # Repetition detection — break if same tool called 4+ times in last 6 steps.
                        # Skipped for real-time tools (gameplay/screen-watching repeat by design).
                        if next_tool not in _REALTIME_TOOLS:
                            _recent_tools.append(next_tool)
                            if len(_recent_tools) > 6:
                                _recent_tools.pop(0)
                            # Reading / searching DIFFERENT files 4× in a row is research, not a loop
                            # (2026-10-29: 4 FRIK sources tripped it). An exact re-read is still caught
                            # by the signature check below.
                            if len(_recent_tools) >= 4 and len(set(_recent_tools[-4:])) == 1 \
                                    and next_tool not in ("read_file", "show_file", "search_code", "compare_code"):
                                log(f"[S32] Chain loop detected ({next_tool} x4) — breaking chain")
                                _tr("flag", "loop_broken")
                                chunk({"phase": "trace", "text": f"⚠ loop broken — {next_tool} called 4× in a row"})
                                try:
                                    from s32_feedback import record_pattern as _fb_pat; _fb_pat(next_tool, "loop")
                                except Exception: pass
                                if not _answer_from_what_you_have(next_tool):
                                    chunk({"phase": "followup", "response": f"I seem to be stuck checking {next_tool}. Stopping the chain — please clarify what you'd like me to do next."})
                                if _forced_next["call"]:
                                    next_spoken, next_tool, next_params = _forced_next["call"]
                                    _forced_next["call"] = None
                                    _recent_tools.clear()
                                else:
                                    break

                            # Tighter guard: the EXACT same tool+target (+offset) twice
                            # means it's re-reading content it already has — the
                            # show_file-on-the-same-file loop. Stop and answer from it.
                            # For run_workflow, the distinguishing fields are name/target/
                            # positive (NOT path/offset) — without them, an image-gen and a
                            # video-gen produced the identical signature "run_workflow||" and
                            # the legitimate image→video step was killed as a false repeat.
                            _sig = "%s|%s|%s|%s" % (
                                next_tool,
                                (next_params.get("path") or next_params.get("query")
                                 or next_params.get("folder") or next_params.get("name")
                                 or _fop_key(next_tool, next_params)),
                                (next_params.get("offset", "") or next_params.get("target", "")),
                                (next_params.get("positive", "") or "")[:40],
                            )
                            if _sig in _recent_sigs:
                                log(f"[S32] Repeat call detected ({next_tool} same target) — breaking chain")
                                _tr("flag", "repeat_broken")
                                chunk({"phase": "trace", "text": f"⚠ repeat broken — {next_tool} same input twice"})
                                try:
                                    from s32_feedback import record_pattern as _fb_pat; _fb_pat(next_tool, "repeat")
                                except Exception: pass
                                if not _answer_from_what_you_have(next_tool):
                                    chunk({"phase": "followup", "response": f"I kept going back to {next_tool} without getting further, so I stopped there. Tell me where to look next, or what to change, and I'll pick it up."})
                                if _forced_next["call"]:
                                    next_spoken, next_tool, next_params = _forced_next["call"]
                                    _forced_next["call"] = None
                                    _recent_tools.clear()
                                else:
                                    break
                            _recent_sigs.append(_sig)

                        # Generation tools (image/video/lora). These used to hard-stop the
                        # chain, which broke multi-step creative tasks like "generate an image,
                        # THEN turn it into a video" — the second generation never ran. Now we
                        # let the chain continue so a follow-up generation can fire (the image
                        # is saved synchronously and becomes last_image_path, so the video step
                        # picks it up). Bounded by _MAX_GENS so a confused model can't trigger
                        # an endless run of heavy generations.
                        _MAX_GENS = 4
                        if next_tool in _GENERATION_TOOLS:
                            if next_spoken:
                                if state["messages"] and state["messages"][-1]["role"] == "assistant":
                                    state["messages"].pop()
                            chunk({"phase": "text", "response": next_spoken or "", "tool": next_tool})
                            chunk({"phase": "trace", "text": f"step {chain_step}: " + _trace_label(next_tool, next_params)})
                            # The attachment was consumed by the FIRST step (and set as the
                            # current image). Re-injecting it here made every later step run on
                            # the original attachment instead of the previous step's output.
                            # Each chain step works on the PREVIOUS step's output — never an
                            # image it names mid-chain (it'd tend to re-pick the original).
                            # Picking a specific earlier image is for the first call of a turn.
                            if isinstance(next_params, dict):
                                for _ik in ("image", "image_path", "input_image"):
                                    if next_params.pop(_ik, None) is not None:
                                        log(f"[S32] Chain step {chain_step}: dropped '{_ik}' — chains continue from the last output")
                            next_results = _run_tool(next_tool, next_params, None)
                            for r in next_results:
                                if _should_show_result(next_tool, r):
                                    chunk({"phase": "result", "result": r})
                            _record_outcome(next_tool, next_params, next_results)
                            _done_gens.append(_step_label(next_tool, next_params))
                            _gen_count += 1
                            # A VIDEO generation is terminal — it's the last step of a
                            # creative chain and renders async in the background. A BATCH
                            # (count>1) is also terminal: it renders async, nothing chains
                            # off "generate N images", and letting the chain continue is
                            # what caused it to keep spawning more generations after a
                            # batch. Image generations (count==1) DO continue, so they can
                            # feed a follow-up (image→video).
                            _is_video_gen = (next_params.get("target") == "video") or (next_tool == "generate_video")
                            _is_batch_gen = False
                            try:
                                _is_batch_gen = int(next_params.get("count", 1) or 1) > 1
                            except Exception:
                                _is_batch_gen = False
                            # An image generation only CONTINUES the chain when the original
                            # request explicitly asks for a video next (the image→video case).
                            # Otherwise it's terminal. A plain "remove background", "upscale",
                            # "make a 3D", or "generate N images" is ONE step — continuing was
                            # making it chain into unwanted workflows (bg-remove → img-to-3d,
                            # batch → img-to-3d, …) and clobber last_image_path between turns,
                            # which is what broke attached-image injection too.
                            # Continue only while the operator's request still has steps left
                            # (generate → bg-remove → 3D = 3). A one-step request stays terminal,
                            # which is what stops unwanted follow-up workflows.
                            _steps_left = _gen_count < _planned
                            if _is_video_gen or _is_batch_gen or (not _steps_left) or _gen_count >= _MAX_GENS:
                                log(f"[S32] Generation terminal (video={_is_video_gen} batch={_is_batch_gen} "
                                    f"planned={_planned} gens={_gen_count}) — ending chain.")
                                break
                            # Image gen + the request wants a video → continue so the video
                            # step can fire on the image we just made.
                            current_tool = next_tool
                            current_params = next_params or {}
                            current_results = next_results
                            _native["cur"] += 1
                            _native["say"] = next_spoken or ""
                            continue

                        # Silent tools - run silently and continue chain
                        if next_tool in _SILENT_TOOLS:
                            log(f"[S32] Chain step {chain_step}: {next_tool} (silent)")
                            chunk({"phase": "trace", "text": f"step {chain_step}: " + _trace_label(next_tool, next_params)})
                            _sr = _run_tool(next_tool, next_params, None)
                            if _native["on"]:
                                _turn_add(next_spoken or "", [(next_tool, next_params or {},
                                          _tool_result_summary(next_tool, _sr[0] if _sr else {})[:2000])])
                            continue

                        # Another retrieval/action tool - run it and loop
                        log(f"[S32] Chain step {chain_step}: {next_tool}")
                        chunk({"phase": "text", "response": next_spoken or "", "tool": next_tool})
                        chunk({"phase": "trace", "text": f"step {chain_step}: " + _trace_label(next_tool, next_params)})
                        next_img_bytes = base64.b64decode(image_b64) if image_b64 else None
                        current_results = _run_tool(next_tool, next_params, next_img_bytes)
                        current_tool = next_tool
                        current_params = next_params or {}
                        _native["cur"] += 1
                        _native["say"] = next_spoken or ""
                        for r in current_results:
                            if _should_show_result(next_tool, r):
                                chunk({"phase": "result", "result": r})
                        # A tool can end the turn outright (e.g. Unreal code out of attempts and
                        # it kept calling it anyway).
                        if any(isinstance(r, dict) and r.get("halt") for r in current_results):
                            log(f"[S32] Chain halted by {next_tool}: {current_results[0].get('error', '')[:80]}")
                            _tr("flag", "halted")
                            _hm = next((r.get("halt_msg") for r in current_results
                                        if isinstance(r, dict) and r.get("halt_msg")), None)
                            chunk({"phase": "followup", "response": _hm or
                                   "(I've used all my attempts on this one — stopping here. Ask again "
                                   "and I'll start fresh with what I learned.)"})
                            break

                    # Loop exited. If we hit the step cap (rather than finishing
                    # naturally), say so instead of going quiet — and invite a resume.
                    if chain_step >= MAX_CHAIN:
                        log(f"[S32] Chain hit step limit ({MAX_CHAIN}).")
                        _tr("flag", "step_cap")
                        chunk({"phase": "followup", "response":
                               f"(I've run {MAX_CHAIN} tool steps on this — pausing here so I don't "
                               f"loop forever. Say 'continue' and I'll pick up where I left off.)"})

                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                    pass
                _prompt_cache["locked"] = False  # unlock prompt after turn completes
                # (Training record: the whole turn is banked by turn_end below.)
                # NOTE: do NOT auto-speak here. On the tool path the only `spoken`
                # text is the acknowledgement, and it was already spoken at the
                # start of the turn (when the tool fired). Speaking it again here
                # is what caused the double playback on workflow/generation calls.
                # (`spoken` is never reassigned in this branch — the chain uses
                # `next_spoken` — so this could only ever replay the same ack.)

            except GenerationCancelled:
                _traj["cancel"] = True
                _prompt_cache["locked"] = False
                # Stopped before (or between) any streamed output. Close the stream
                # cleanly if it had started, otherwise send a plain stopped reply.
                try:
                    if streaming_started:
                        chunk({"phase": "followup", "response": "⏹ Stopped."})
                        try:
                            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
                        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                            pass
                    else:
                        self.send_json({"response": "⏹ Stopped.", "tool": None, "result": None})
                except Exception:
                    pass

            except Exception as e:
                import traceback
                err = traceback.format_exc()
                log(f"[S32] Chat error: {err}")
                _tr("flag", "error")
                _prompt_cache["locked"] = False  # unlock on error
                try:
                    msg = str(e)
                    if "400" in msg or "No models loaded" in msg:
                        friendly = "No model is loaded in LM Studio yet. Open LM Studio and load a model, then try again."
                    else:
                        friendly = f"Something went wrong: {e}"
                    # Mid-stream the headers are already sent, so send_json can't reach the
                    # chat — a crash after a tool step used to just end the turn in silence
                    # (2026-10-29: video search, then nothing). Say it in the stream instead.
                    try:
                        _streamed = streaming_started
                    except NameError:
                        _streamed = False
                    if _streamed:
                        chunk({"phase": "followup", "response":
                               f"⚠ {friendly} — that's a bug on my side; the details are in the console."})
                        try:
                            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
                        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
                            pass
                    else:
                        self.send_json({"response": friendly, "tool": None, "result": None})
                except Exception:
                    pass
            finally:
                _tr("turn_end", _traj["cancel"] or bool(state.get("cancel")))

        elif self.path == "/cancel":
            # Phase 3 stop button: abort any in-flight generation and cut speech.
            # Works because the server is threaded, so this is served even while
            # /chat is blocked waiting on llama-server.
            try:
                cancel_generation()
            except Exception:
                pass
            try:
                # ⏹ also stops ITS ComfyUI run(s) — a sync step mid-chain or a batch.
                # Only if something of its is running, so a plain "stop talking"
                # doesn't leave a cancel flag waiting.
                if state.get("active_workflow") or state.get("bg_gen"):
                    import s32_comfyui
                    s32_comfyui.cancel_comfy_jobs("stop button")
            except Exception:
                pass
            try:
                if _voice:
                    _voice.stop_voice()
            except Exception:
                pass
            self.send_json({"status": "cancelled"})

        elif self.path == "/load_image":
            # Read an image file from disk, return base64 + filename for the browser panel
            file_path = body.get("path", "")
            if not file_path or not os.path.isfile(file_path):
                self.send_json({"error": f"File not found: {file_path}"}); return
            ext = os.path.splitext(file_path)[1].lower()
            if ext not in IMAGE_EXTS:
                self.send_json({"error": f"Not a supported image: {ext}"}); return
            try:
                with open(file_path, "rb") as f:
                    raw = f.read()
                mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".webp": "image/webp",
                        ".bmp": "image/bmp", ".gif": "image/gif"}.get(ext, "image/png")
                self.send_json({
                    "b64":      base64.b64encode(raw).decode(),
                    "mime":     mime,
                    "filename": os.path.basename(file_path),
                    "path":     file_path,
                })
            except Exception as e:
                self.send_json({"error": str(e)})

        elif self.path == "/browse_folder":
            # Return file list for a named folder (used by the browser panel JS)
            folder_key = body.get("folder")
            # Base folders available in the panel
            # (2026-10-31) LoRAs left the panel; downloaded images/videos joined it (the
            # downloader saves to <Output>\downloads\images|videos).
            _ob = os.path.dirname(CONFIG.get("out_images", "")) if CONFIG.get("out_images") else ""
            base_folders = {
                "images":  CONFIG.get("out_images", ""),
                "videos":  CONFIG.get("out_videos", ""),
                "3d":      CONFIG.get("out_3d", ""),
                "downloaded images": os.path.join(_ob, "downloads", "images") if _ob else "",
                "downloaded videos": os.path.join(_ob, "downloads", "videos") if _ob else "",
            }
            if not folder_key:
                # Return folder list with counts
                folders = []
                for name, path in base_folders.items():
                    if path and os.path.exists(path):
                        try:
                            files = [f for f in os.listdir(path)
                                     if os.path.isfile(os.path.join(path, f))]
                        except Exception:
                            files = []
                        if files:      # an empty folder gets no button
                            folders.append({"name": name, "path": path, "count": len(files)})
                self.send_json({"folders": folders})
            else:
                # Return files in the requested folder
                folder_path = base_folders.get(folder_key, folder_key)
                if not folder_path or not os.path.exists(folder_path):
                    self.send_json({"error": f"Folder not found: {folder_key}", "files": []})
                else:
                    try:
                        entries = sorted(os.listdir(folder_path), reverse=True)
                        files = []
                        for fname in entries[:200]:
                            fpath = os.path.join(folder_path, fname)
                            if not os.path.isfile(fpath):
                                continue
                            ext = os.path.splitext(fname)[1].lower()
                            ftype = "image" if ext in IMAGE_EXTS else "video" if ext in VIDEO_EXTS else "file"
                            files.append({
                                "name":  fname,
                                "path":  fpath,
                                "url":   f"/img?path={urllib.parse.quote(fpath)}",
                                "type":  ftype,
                                "size":  round(os.path.getsize(fpath) / 1024, 1),
                            })
                        self.send_json({"files": files, "folder": folder_key, "path": folder_path})
                    except Exception as e:
                        self.send_json({"error": str(e), "files": []})

        elif self.path == "/set_model":
            model_id = body.get("model_id", "").strip()
            if not model_id:
                self.send_json({"error": "No model_id provided"}); return
            log(f"[S32] set_model received: {model_id[:80]}")
            # Derive the lms catalog key from the path or use as-is
            if os.path.sep in model_id or (len(model_id) > 2 and model_id[1] == ":"):
                # Full file path — derive key from folder structure
                model_folder = os.path.basename(os.path.dirname(model_id))
                # Strip common suffixes that appear in folder names but not catalog keys
                lms_identifier = model_folder.lower()
                for suffix in ("-gguf", "_gguf", "-gguf-q4", "-gguf-q8"):
                    if lms_identifier.endswith(suffix):
                        lms_identifier = lms_identifier[:-len(suffix)]
                        break
            else:
                lms_identifier = model_id.lower()
            # Update config in memory and on disk
            CONFIG["lmstudio_model"]     = lms_identifier
            CONFIG["lmstudio_model_key"] = lms_identifier
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            try:
                existing = {}
                if os.path.exists(config_path):
                    with open(config_path, "r", encoding="utf-8") as f:
                        existing = json.load(f)
                existing["lmstudio_model"]          = lms_identifier
                existing["lmstudio_model_key"]      = lms_identifier
                existing["lmstudio_context_length"] = body.get("context_length", 131072)
                existing["lmstudio_gpu_offload"]    = body.get("gpu_offload", "max")
                existing["lmstudio_gpu_layers"]     = int(body.get("gpu_layers", 99))
                # For llamacpp backend, always save the full file path
                # model_id may be the full path or a catalog key
                # Check both model_id and the original body field
                raw_path = ""
                if os.path.isabs(model_id):
                    raw_path = model_id
                elif os.path.isabs(body.get("model_id", "")):
                    raw_path = body.get("model_id", "")
                # Also check if path ends with .gguf regardless of os.path.isabs result
                elif model_id.lower().endswith(".gguf") and ":" in model_id:
                    raw_path = model_id
                if CONFIG.get("inference_backend") == "llamacpp" and raw_path:
                    existing["lmstudio_model_path"] = raw_path
                    CONFIG["lmstudio_model_path"]   = raw_path
                    log(f"[S32] Model path saved: {os.path.basename(raw_path)}")
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(existing, f, indent=2)
                log(f"[S32] Model switched to: {lms_identifier}")
                # Reset ready state so UI shows offline during switch
                state["lmstudio_ready"] = False
                # Unload current model then load new one
                lms = CONFIG.get("lms_exe", "")
                host = CONFIG["lmstudio_host"]
                ctx_len    = body.get("context_length", 131072)
                gpu_off    = body.get("gpu_offload", "max")
                gpu_layers = int(body.get("gpu_layers", 20))
                threads    = int(body.get("threads", 14))
                batch_size = int(body.get("batch_size", 512))
                CONFIG["lmstudio_gpu_layers"]     = gpu_layers
                CONFIG["lmstudio_context_length"] = ctx_len
                try:
                    if CONFIG.get("inference_backend") == "llamacpp":
                        # llama.cpp — restart server with new model
                        shutdown_llama_server()
                        import time as _time
                        _time.sleep(2)
                        # Update config so start_llama_server picks up new model
                        CONFIG["lmstudio_model"]          = lms_identifier
                        CONFIG["lmstudio_context_length"] = ctx_len
                        CONFIG["lmstudio_gpu_layers"]     = gpu_layers
                        def _do_llama():
                            start_llama_server()
                            state["model_switching"] = False
                            # Persist the path that actually loaded
                            if state.get("lmstudio_ready") and CONFIG.get("lmstudio_model_path"):
                                try:
                                    config_path = os.path.join(
                                        os.path.dirname(os.path.abspath(__file__)), "config.json")
                                    if os.path.exists(config_path):
                                        with open(config_path,"r",encoding="utf-8") as _f:
                                            _cfg = json.load(_f)
                                        _cfg["lmstudio_model_path"] = CONFIG["lmstudio_model_path"]
                                        with open(config_path,"w",encoding="utf-8") as _f:
                                            json.dump(_cfg, _f, indent=2)
                                except Exception:
                                    pass
                        threading.Thread(target=_do_llama, daemon=True).start()
                    self.send_json({"status": "loading", "model": model_id,
                        "hint": "Loading — ready shortly."})
                except Exception as e:
                    state["model_switching"] = False
                    log(f"[S32] Model load error: {e}")
                    self.send_json({"status": "saved", "model": model_id,
                        "hint": "Config saved. Reload to apply."})
            except Exception as e:
                state["model_switching"] = False
                self.send_json({"error": str(e)})
            return

        elif self.path == "/browse_file":
            # Open Windows file picker dialog and return selected path
            import subprocess
            filter_type = body.get("filter", "GGUF files (*.gguf)|*.gguf|All files (*.*)|*.*")
            initial_dir = body.get("initial_dir", "I:\\")
            ps_script = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName Microsoft.VisualBasic; "
                "$f = New-Object System.Windows.Forms.OpenFileDialog; "
                f"$f.Filter = '{filter_type}'; "
                f"$f.InitialDirectory = '{initial_dir}'; "
                "$f.Multiselect = $false; "
                "$owner = New-Object System.Windows.Forms.Form; "
                "$owner.TopMost = $true; $owner.Visible = $false; "
                "if ($f.ShowDialog($owner) -eq 'OK') { Write-Output $f.FileName } else { Write-Output '' }"
            )
            try:
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                _si.wShowWindow = 0  # SW_HIDE — hides console, dialog still appears
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps_script],
                    capture_output=True, text=True, timeout=120,
                    creationflags=subprocess.CREATE_NEW_CONSOLE,
                    startupinfo=_si
                )
                path = result.stdout.strip()
                # Strip any BOM or whitespace
                path = path.lstrip("\ufeff").strip()
                if path and os.path.exists(path):
                    fname = os.path.basename(path)
                    size_gb = round(os.path.getsize(path) / 1e9, 1)
                    try:
                        from s32_hardware import read_gguf_caps
                        caps = read_gguf_caps(path)
                    except Exception:
                        caps = {}
                    self.send_json({"path": path, "name": fname, "size_gb": size_gb,
                                    "caps": caps, "cpu_threads": (os.cpu_count() or 0)})
                elif result.returncode != 0:
                    self.send_json({"error": result.stderr.strip() or "PowerShell error", "path": ""})
                else:
                    self.send_json({"path": "", "cancelled": True})
            except subprocess.TimeoutExpired:
                self.send_json({"error": "File picker timed out — please select a file within 2 minutes", "path": ""})
            except Exception as e:
                self.send_json({"error": str(e), "path": ""})
            return

        elif self.path == "/get_model_settings":
            model_path = body.get("model_path", CONFIG.get("lmstudio_model_path", ""))
            key = os.path.basename(model_path).lower()
            settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_settings.json")
            defaults = {"ctx_size":131072,"gpu_layers":20,"threads":14,"batch_size":512,"ubatch_size":2048,"kv_cache":"f16","flash_attn":False}
            try:
                if os.path.exists(settings_file):
                    with open(settings_file,"r",encoding="utf-8") as _f:
                        all_s = json.load(_f)
                    if key in all_s:
                        defaults.update(all_s[key])
            except Exception:
                pass
            self.send_json(defaults)
            return

        elif self.path == "/save_model_settings":
            model_path = body.get("model_path", CONFIG.get("lmstudio_model_path", ""))
            settings   = body.get("settings", {})
            key = os.path.basename(model_path).lower()
            settings_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_settings.json")
            try:
                all_s = {}
                if os.path.exists(settings_file):
                    with open(settings_file,"r",encoding="utf-8") as _f:
                        all_s = json.load(_f)
                all_s[key] = settings
                with open(settings_file,"w",encoding="utf-8") as _f:
                    json.dump(all_s, _f, indent=2)
                log(f"[S32] Model settings saved for {key}")
                self.send_json({"status":"saved","model":key})
            except Exception as e:
                self.send_json({"error":str(e)})
            return

        elif self.path == "/get_ui_prefs":
            prefs_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_prefs.json")
            try:
                if os.path.exists(prefs_file):
                    with open(prefs_file,"r",encoding="utf-8") as _f:
                        self.send_json(json.load(_f))
                else:
                    self.send_json({})
            except Exception as e:
                self.send_json({"error":str(e)})
            return

        elif self.path == "/save_ui_prefs":
            prefs = body.get("prefs", None)
            prefs_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_prefs.json")
            try:
                if prefs is None:
                    if os.path.exists(prefs_file):
                        os.remove(prefs_file)
                    log("[S32] UI prefs reset to default")
                    self.send_json({"status":"reset"})
                else:
                    with open(prefs_file,"w",encoding="utf-8") as _f:
                        json.dump(prefs, _f, indent=2)
                    log("[S32] UI prefs saved")
                    self.send_json({"status":"saved"})
            except Exception as e:
                self.send_json({"error":str(e)})
            return

        elif self.path == "/get_models":
            # Scan LM Studio models directory for available model files
            lms_models_dir = os.path.join(
                os.environ.get("USERPROFILE", os.path.expanduser("~")),
                ".lmstudio", "models"
            )
            models = []
            try:
                if os.path.exists(lms_models_dir):
                    for root, dirs, files in os.walk(lms_models_dir):
                        for fname in files:
                            if fname.lower().endswith((".gguf", ".bin", ".safetensors")):
                                # Use relative path from models dir as the model ID
                                rel = os.path.relpath(os.path.join(root, fname), lms_models_dir)
                                parts = rel.replace(os.sep, "/")
                                models.append({
                                    "id":   parts,
                                    "name": fname,
                                    "path": os.path.join(root, fname),
                                    "size_gb": round(os.path.getsize(os.path.join(root, fname)) / 1e9, 1),
                                })
                # Sort by name
                models.sort(key=lambda x: x["name"].lower())
                # Also check if LM Studio API has a loaded model
                host = CONFIG["lmstudio_host"]
                try:
                    r = http_get(f"{host}/v1/models", timeout=3)
                    loaded = [m.get("id","") for m in r.get("data",[]) if m.get("id")]
                except Exception:
                    loaded = []
                self.send_json({
                    "models":  models,
                    "loaded":  loaded,
                    "current": CONFIG.get("lmstudio_model", ""),
                    "models_dir": lms_models_dir,
                })
            except Exception as e:
                self.send_json({"error": str(e), "models": [], "current": CONFIG.get("lmstudio_model", "")})
            return

        elif self.path == "/upload_dataset":
            dataset = body.get("dataset","")
            images  = body.get("images",[])
            dest    = os.path.join(CONFIG["datasets_dir"], dataset)
            os.makedirs(dest, exist_ok=True)
            saved = []
            for img in images:
                try:
                    raw  = base64.b64decode(img["data"])
                    path = os.path.join(dest, img["name"])
                    with open(path, "wb") as f:
                        f.write(raw)
                    saved.append(img["name"])
                except Exception:
                    pass
            self.send_json({"saved": saved, "dataset": dataset})

        else:
            self.send_response(404); self.end_headers()

# ── HTML UI ───────────────────────────────────────────────────────────────────

def get_html():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>System 32</title>
  <!-- 3D viewer served from this drive (it used to load from Google's CDN on EVERY page open —
       Google saw each launch). Its decoders for compressed meshes/textures are local too. -->
  <script>self.ModelViewerElement={dracoDecoderLocation:'/static/draco/',ktx2TranscoderLocation:'/static/basis/',lottieLoaderLocation:'/static/none.js'};</script>
  <script type="module" src="/static/model-viewer.min.js"></script>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#040608;--surface:#080c10;--panel:#0c1018;--border:#1a2535;
  --accent:#00d4ff;--accent2:#ff4060;--accent3:#39ff14;
  --text:#c8d8e8;--sub:#4a6070;--warn:#ffaa00;
  --mono:'Consolas','Courier New',monospace;
  --sans:'Segoe UI',system-ui,sans-serif;
}
html,body{height:100%;background:var(--chat-bg,var(--bg));color:var(--text);font-family:var(--sans);overflow:hidden}
  body{background-image:var(--chat-bg-image,none);background-size:cover;background-position:center center;background-attachment:fixed;background-repeat:no-repeat}
body::before{content:'';position:fixed;inset:0;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,212,255,.012) 2px,rgba(0,212,255,.012) 4px);pointer-events:none;z-index:9999}
body::after{content:'';position:fixed;inset:0;background-image:linear-gradient(rgba(0,212,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(0,212,255,.025) 1px,transparent 1px);background-size:40px 40px;pointer-events:none;z-index:0}

#app{display:flex;flex-direction:column;height:100vh;position:relative;z-index:1}

/* Security banner */
#upd-banner{display:none;flex-wrap:wrap;align-items:center;gap:10px;padding:8px 14px;background:linear-gradient(90deg,rgba(0,212,255,.14),rgba(57,255,20,.06));border-bottom:1px solid var(--accent);font-family:var(--mono);font-size:13px;z-index:49}
#upd-banner #upd-icon{color:var(--accent);font-size:15px}
#upd-banner button{background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:4px;padding:3px 10px;font-family:var(--mono);font-size:12px;cursor:pointer}
#upd-banner button:hover{border-color:var(--accent);color:var(--accent)}
#upd-banner #upd-go{border-color:var(--accent);color:#000;background:var(--accent);font-weight:700}
#upd-banner #upd-notes{display:none;flex-basis:100%;margin:4px 0 2px;padding:8px 10px;max-height:220px;overflow:auto;white-space:pre-wrap;background:rgba(0,0,0,.35);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:12px}
#sec-banner{display:none;align-items:center;gap:10px;padding:8px 14px;background:linear-gradient(90deg,rgba(255,64,96,.18),rgba(255,170,0,.10));border-bottom:1px solid var(--accent2);font-family:var(--mono);font-size:13px;z-index:50;box-shadow:0 0 18px rgba(255,64,96,.25);animation:secpulse 2.4s ease-in-out infinite}
@keyframes secpulse{0%,100%{box-shadow:0 0 14px rgba(255,64,96,.18)}50%{box-shadow:0 0 26px rgba(255,64,96,.42)}}
#sec-banner #sec-icon{font-size:16px;color:var(--accent2)}
#sec-banner #sec-text{flex:1;color:var(--text)}
#sec-banner button{font-family:var(--mono);font-size:12px;border:1px solid var(--border);background:var(--panel);color:var(--text);padding:4px 11px;border-radius:4px;cursor:pointer;transition:all .15s}
#sec-banner #sec-approve:hover{border-color:var(--accent3);color:var(--accent3);box-shadow:0 0 10px rgba(57,255,20,.3)}
#sec-banner #sec-dismiss:hover{border-color:var(--accent2);color:var(--accent2)}
#sec-banner #sec-kill:hover{border-color:var(--accent2);color:var(--accent2);box-shadow:0 0 10px rgba(255,64,96,.3)}
#sec-banner #sec-confirm{border-color:var(--accent2);color:#fff;background:rgba(255,64,96,.25);font-weight:700}
#sec-banner #sec-confirm:hover{background:var(--accent2);box-shadow:0 0 14px rgba(255,64,96,.5)}
#sec-banner #sec-cancel:hover{border-color:var(--accent);color:var(--accent)}

/* Header */
#header{display:flex;align-items:center;justify-content:space-between;padding:0 24px;height:52px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0}
#header>div:first-child{display:flex;align-items:center;gap:6px}
#settings-btn{font-size:18px;padding:0 6px;opacity:0.7;cursor:pointer;color:var(--muted);transition:opacity .2s}#settings-btn:hover{opacity:1}
#logo{font-family:var(--mono);font-size:18px;color:var(--accent);letter-spacing:4px;text-shadow:0 0 20px rgba(0,212,255,.5)}
#logo span{color:var(--accent2)}
#dots{display:flex;gap:16px;align-items:center}
.sdot{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--sub)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--sub);transition:all .3s}
.dot.on{background:var(--accent3);box-shadow:0 0 8px var(--accent3)}
.dot.busy{background:var(--warn);box-shadow:0 0 8px var(--warn);animation:pulse 1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
#job-dot{display:none}

/* Progress */
#prog{height:2px;background:var(--border);flex-shrink:0;display:none}
#prog.on{display:block}
#prog-bar{height:100%;width:30%;background:linear-gradient(90deg,var(--accent),var(--accent2));animation:slide 1.2s ease-in-out infinite}
@keyframes slide{0%{margin-left:-30%}100%{margin-left:100%}}

/* Main */
#main{display:flex;flex:1;overflow:hidden}
#chat{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* Messages */
#msgs{flex:1;overflow-y:auto;padding:24px;display:flex;flex-direction:column;gap:18px;scrollbar-width:thin;scrollbar-color:var(--border) transparent}
.msg{display:flex;gap:12px;max-width:88%;animation:fadein .25s ease}
@keyframes fadein{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.msg.user{align-self:flex-end;flex-direction:row-reverse}
.msg.sys{align-self:center;max-width:100%}
.av{min-width:34px;height:34px;padding:0 4px;border-radius:4px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-family:var(--mono);font-size:11px;font-weight:700;text-align:center;line-height:1.1;word-break:break-word;overflow:hidden;box-sizing:border-box}
.av.ai{background:var(--ai-bub,#001a2e);border:1px solid var(--ai-accent,var(--accent));color:var(--ai-accent,var(--accent))}
.av.user{background:var(--user-bub,#1a0010);border:1px solid var(--user-accent,var(--accent2));color:var(--user-accent,var(--accent2))}
.bub{padding:11px 15px;border-radius:4px;font-size:14px;line-height:1.65}
.bub.ai{background:var(--ai-bub,var(--panel));border:1px solid var(--border);border-left:2px solid var(--ai-accent,var(--accent))}
.bub.user{background:var(--user-bub,rgba(255,64,96,.07));border:1px solid rgba(255,64,96,.22);border-right:2px solid var(--user-accent,var(--accent2))}
.msg.ai.turn .turnbody{flex:1;min-width:0;display:flex;flex-direction:column;background:var(--ai-bub,var(--panel));border:1px solid var(--border);border-left:2px solid var(--ai-accent,var(--accent));border-radius:4px;padding:5px 0}
.turnbody>.msg.seg{max-width:100%;animation:none;display:block}
.turnbody>.msg.seg>.bub.ai{background:transparent;border:none;padding:6px 15px}
.turnbody>.thought,.turnbody>.trace{margin-left:15px;margin-right:12px}
.bub.sys{background:rgba(57,255,20,.04);border:1px solid rgba(57,255,20,.18);color:var(--accent3);font-family:var(--mono);font-size:11px;padding:7px 13px;width:100%}

/* Media */
.media{margin-top:10px}
.media img{max-width:100%;border-radius:4px;border:1px solid var(--border);cursor:zoom-in;transition:border-color .2s}
.media img:hover{border-color:var(--accent)}
.media video{max-width:100%;border-radius:4px;border:1px solid var(--border)}
.saved{font-family:var(--mono);font-size:10px;color:var(--accent3);margin-top:5px;opacity:.7}

/* Inline tool-call / chain-step trace (bare neon-green debug text) */
.thought{margin:4px 0 4px 8px;border-left:2px solid var(--accent3);padding:2px 0 2px 8px;font-family:var(--mono);font-size:10.5px;color:var(--muted)}
.thought>summary{cursor:pointer;color:var(--accent3);opacity:.85;list-style:none;white-space:pre-wrap;word-break:break-word}
.thought>summary::-webkit-details-marker{display:none}
.thought .tsum{color:var(--fg);opacity:.75}
.thought .tsteps{color:var(--accent3);opacity:.7}
.thought pre .tsep{display:block;margin:8px 0 6px;color:var(--accent3);opacity:.6}
body.hide-thoughts .thought{display:none}
.thought pre{white-space:pre-wrap;word-break:break-word;max-height:340px;overflow:auto;margin:5px 0 2px;font-family:var(--mono);font-size:10px;opacity:.75}
.trace{font-family:var(--mono);font-size:10.5px;color:var(--accent3);opacity:.6;margin:2px 0 2px 8px;letter-spacing:.3px;white-space:pre-wrap;word-break:break-word;text-shadow:0 0 6px rgba(57,255,20,.25)}

/* Web search result cards */
.webcards{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.srcrow{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 15px 4px}
.srcrow .srclbl{font-family:var(--mono);font-size:9.5px;letter-spacing:1px;color:var(--muted);margin-right:2px}
.srcchip{width:26px;height:26px;border-radius:50%;background:var(--bg3);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;text-decoration:none;color:var(--accent);font-family:var(--mono);font-size:11px;font-weight:700;transition:border-color .15s,transform .15s;overflow:hidden}
.srcchip:hover{border-color:var(--ai-accent,var(--accent));transform:translateY(-1px)}
.srcchip img{width:16px;height:16px}
.srcrow .rate{margin-left:auto;display:flex;gap:4px;opacity:.35;transition:opacity .15s}
.msg.turn:hover .srcrow .rate,.srcrow .rate.set{opacity:1}
.rate button{background:transparent;border:1px solid transparent;border-radius:5px;padding:1px 5px;font-size:13px;line-height:18px;cursor:pointer;filter:grayscale(1);color:var(--muted)}
.rate button:hover{border-color:var(--border);filter:none}
.rate button.on{filter:none;border-color:var(--ai-accent,var(--accent));background:var(--bg3)}
.webcard{display:flex;gap:10px;align-items:flex-start;padding:8px 11px;background:rgba(0,212,255,.04);border:1px solid var(--border);border-radius:6px;text-decoration:none;transition:background .15s,border-color .15s,box-shadow .15s}
.webcard:hover{background:rgba(0,212,255,.08);border-color:var(--accent);box-shadow:0 0 12px rgba(0,212,255,.15)}
.webcards .webfav{width:18px;height:18px;min-width:18px;border-radius:4px;margin-top:2px;border:none;cursor:inherit;background:var(--bg);flex:0 0 auto}
.webcards .webfav:hover{border:none}
.imgridq{font-family:var(--mono);font-size:10px;color:var(--sub);margin:8px 0 6px;letter-spacing:.02em}
.imgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:6px;margin-top:4px}
.imgcell{position:relative;border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--bg)}
.imgcell img{width:100%;height:120px;object-fit:cover;display:block;cursor:zoom-in;transition:transform .15s,filter .15s}
.imgcell:hover{border-color:var(--accent);box-shadow:0 0 12px rgba(0,212,255,.18)}
.imgcell:hover img{transform:scale(1.04);filter:brightness(1.05)}
.imgsrc{position:absolute;left:0;right:0;bottom:0;font-family:var(--mono);font-size:8.5px;color:var(--sub);background:linear-gradient(transparent,rgba(0,0,0,.78));padding:10px 5px 3px;text-decoration:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;opacity:0;transition:opacity .15s}
.imgcell:hover .imgsrc{opacity:1}
.imgsrc:hover{color:var(--accent)}
.imgdl{position:absolute;top:4px;right:4px;width:24px;height:24px;border-radius:50%;border:1px solid var(--border);background:rgba(0,0,0,.62);color:var(--text);font-size:12px;line-height:22px;text-align:center;cursor:pointer;opacity:0;transition:opacity .15s,border-color .15s;padding:0}
.imgcell:hover .imgdl,.imgdl.busy,.imgdl.done,.imgdl.err{opacity:1}
.imgdl:hover{border-color:var(--accent);color:var(--accent)}
.imgdl.done{color:#39ff14;border-color:#39ff14}
.imgdl.err{color:var(--accent2);border-color:var(--accent2)}
@media (hover:none){.imgdl{opacity:1}}
.vidgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:8px;margin-top:4px}
.vidcard{border:1px solid var(--border);border-radius:6px;overflow:hidden;background:var(--bg);display:flex;flex-direction:column;min-width:0}
.vidcard:hover{border-color:var(--accent);box-shadow:0 0 12px rgba(0,212,255,.18)}
.vidthumb{position:relative;display:block;aspect-ratio:16/9;background:#000}
.vidthumb img{width:100%;height:100%;object-fit:cover;display:block}
.viddur{position:absolute;right:5px;bottom:5px;font-family:var(--mono);font-size:9.5px;background:rgba(0,0,0,.8);color:#fff;padding:1px 5px;border-radius:3px}
.vidbody{padding:6px 7px 7px;display:flex;flex-direction:column;gap:4px;flex:1}
.vidtitle{font-size:12px;line-height:1.3;color:var(--text);text-decoration:none;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.vidtitle:hover{color:var(--accent)}
.vidmeta{font-family:var(--mono);font-size:9px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vidbtns{display:flex;gap:6px;margin-top:auto}
.vidbtns a,.vidbtns button{flex:1;font-family:var(--mono);font-size:10px;padding:4px 0;border:1px solid var(--border);border-radius:4px;background:transparent;color:var(--text);text-align:center;text-decoration:none;cursor:pointer}
.vidbtns a:hover,.vidbtns button:hover{border-color:var(--accent);color:var(--accent)}
.vidbtns button:disabled{opacity:.7;cursor:default}
.vidnote{font-family:var(--mono);font-size:9px;color:var(--sub)}
.morewrap{display:flex;justify-content:center;margin:8px 0 2px}
.flist{display:flex;flex-direction:column;border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-top:4px}
.frow{display:flex;align-items:center;gap:8px;padding:5px 8px;border-top:1px solid var(--border);min-width:0}
.frow:first-child{border-top:none}
.frow:hover{background:rgba(0,212,255,.05)}
.fico{font-size:14px;width:18px;text-align:center;flex:none}
.fmain{min-width:0;flex:1}
.fname{font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fsub{font-family:var(--mono);font-size:9px;color:var(--sub);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fcopy{flex:none;border:1px solid var(--border);background:transparent;color:var(--sub);border-radius:4px;font-size:11px;padding:2px 7px;cursor:pointer}
.fcopy:hover{color:var(--accent);border-color:var(--accent)}
.morebtn{font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;padding:6px 16px;border:1px solid var(--border);border-radius:14px;background:transparent;color:var(--text);cursor:pointer}
.morebtn:hover:not(:disabled){border-color:var(--accent);color:var(--accent);box-shadow:0 0 10px rgba(0,212,255,.15)}
.morebtn:disabled{opacity:.6;cursor:default}
.morebtn.done{border-style:dashed}
.fsconfirm{margin-top:8px;padding:10px 12px;background:rgba(0,212,255,.04);border:1px solid var(--accent);border-radius:7px;box-shadow:0 0 14px rgba(0,212,255,.10)}
.fshead{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.fsbadge{font-family:var(--mono);font-size:9px;font-weight:700;letter-spacing:.05em;padding:2px 6px;border-radius:3px}
.fsbadge.fscreate{background:rgba(57,255,20,.12);color:var(--accent3);border:1px solid rgba(57,255,20,.4)}
.fsbadge.fsedit{background:rgba(255,64,96,.10);color:var(--accent2);border:1px solid rgba(255,64,96,.4)}
.fsbadge.fsdel{background:rgba(255,64,96,.22);color:#fff;border:1px solid var(--accent2)}
.fspath{font-family:var(--mono);font-size:11px;color:var(--text);word-break:break-all}
.fsmeta{font-family:var(--mono);font-size:10px;color:var(--sub);margin-top:4px}
.fspreview{margin:7px 0 0;padding:8px;background:var(--bg);border:1px solid var(--border);border-radius:5px;font-family:var(--mono);font-size:10.5px;line-height:1.45;color:var(--sub);max-height:160px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.fsbtns{display:flex;gap:8px;margin-top:9px}
.fsyes,.fsno{font-family:var(--mono);font-size:11px;font-weight:700;padding:6px 14px;border-radius:4px;cursor:pointer;border:1px solid}
.fsyes{background:rgba(0,212,255,.12);color:var(--accent);border-color:var(--accent)}
.fsyes:hover:not(:disabled){background:rgba(0,212,255,.22)}
.fsno{background:transparent;color:var(--sub);border-color:var(--border)}
.fsno:hover:not(:disabled){border-color:var(--accent2);color:var(--accent2)}
.fsyes:disabled,.fsno:disabled{opacity:.4;cursor:default}
.fsstatus{font-family:var(--mono);font-size:10.5px;color:var(--sub);margin-top:8px}
#sort-bar{display:none;align-items:center;gap:8px;margin:0 0 7px;padding:7px 11px;background:rgba(0,212,255,.06);border:1px solid var(--accent);border-radius:7px;box-shadow:0 0 12px rgba(0,212,255,.12)}
#sort-bar #sort-text{font-family:var(--mono);font-size:11px;color:var(--text);line-height:1.4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:60%}
#sort-bar button{font-family:var(--mono);font-size:11px;font-weight:700;padding:5px 13px;border-radius:4px;cursor:pointer;border:1px solid;transition:all .15s}
#sort-bar #sort-copy{background:rgba(57,255,20,.10);color:var(--accent3);border-color:rgba(57,255,20,.5)}
#sort-bar #sort-copy:hover:not(:disabled){background:rgba(57,255,20,.2);box-shadow:0 0 10px rgba(57,255,20,.3)}
#sort-bar #sort-move{background:rgba(0,212,255,.12);color:var(--accent);border-color:var(--accent)}
#sort-bar #sort-move:hover:not(:disabled){background:rgba(0,212,255,.24);box-shadow:0 0 10px rgba(0,212,255,.3)}
#sort-bar #sort-cancel{background:transparent;color:var(--sub);border-color:var(--border);padding:5px 9px}
#sort-bar #sort-cancel:hover{border-color:var(--accent2);color:var(--accent2)}
#sort-bar button:disabled{opacity:.4;cursor:default}
.webtxt{min-width:0;flex:1}
.webtitle{color:var(--accent);font-size:13px;font-weight:600;line-height:1.3;word-break:break-word}
.webdom{color:var(--sub);font-family:var(--mono);font-size:10px;margin:2px 0 3px}
.websnip{color:var(--text);font-size:11px;line-height:1.4;opacity:.8;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}

/* FS result box */
.cmpcard{margin-top:6px;border:1px solid var(--border);border-radius:6px;overflow:hidden;font-family:var(--mono);font-size:11px}
.cmphead{padding:7px 10px;background:rgba(0,212,255,.06)}
.cmphead b{color:var(--accent)}
.cmpcard details{border-top:1px solid var(--border)}
.cmpcard summary{padding:5px 10px;cursor:pointer;color:var(--sub)}
.cmpcard pre{margin:0;padding:6px 10px;max-height:320px;overflow:auto;white-space:pre;background:var(--bg)}
.cmpcard .dadd{color:var(--accent3)} .cmpcard .ddel{color:var(--accent2)} .cmpcard .dhunk{color:var(--accent)}
.fsbox{margin-top:10px;padding:10px 12px;background:var(--bg);border:1px solid var(--border);border-radius:4px;font-family:var(--mono);font-size:11px;color:var(--sub);max-height:280px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}

/* Thinking */
.thinking{display:flex;align-items:center;gap:8px;font-family:var(--mono);font-size:12px;color:var(--accent);opacity:.8}
.tdots span{display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--accent);animation:blink 1.2s infinite}
.tdots span:nth-child(2){animation-delay:.2s}.tdots span:nth-child(3){animation-delay:.4s}
@keyframes blink{0%,80%,100%{opacity:.2}40%{opacity:1}}

/* Input */
#input-area{padding:14px 24px;background:var(--surface);border-top:1px solid var(--border);flex-shrink:0}
#img-preview{display:none;align-items:center;gap:10px;margin-bottom:10px;padding:7px 12px;background:var(--panel);border:1px solid var(--border);border-radius:4px}
#drop-tray{display:flex;flex-wrap:wrap;gap:6px;flex:1 1 100%;min-width:0}
#drop-tray:empty{display:none}
.dchip{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:3px 8px;border:1px solid var(--border);border-radius:12px;font-family:var(--mono);font-size:11px;color:var(--accent);background:var(--bg);min-width:0}
.dchip .dn{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px}
.dchip .dp{color:var(--sub)}
.dchip.err{color:var(--accent2);border-color:var(--accent2)}
.dchip button{background:none;border:none;color:var(--accent2);cursor:pointer;font-size:12px;padding:0 2px}
.dchip .dthumb{width:34px;height:34px;object-fit:cover;border-radius:4px;flex:0 0 auto}
#img-preview .dchip{flex:0 0 auto}
#img-preview .dchip span{flex:0 1 auto;color:inherit}
#img-preview .dchip .dp{color:var(--sub)}
#img-preview .dchip .dthumb{height:34px;width:34px}
#prev-name:empty,#doc-summary:empty{display:none}
#img-preview #drop-tray{order:1;flex:1 1 0}
#img-preview #clear-img{order:2;margin-left:auto}
.bub.filebub{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.bub.filebub .sentpic{max-height:110px;max-width:160px;border-radius:4px;cursor:zoom-in;object-fit:cover}
#drop-overlay{position:fixed;inset:0;z-index:9000;pointer-events:none;display:none;align-items:center;justify-content:center;
  border:2px dashed var(--accent);background:rgba(0,212,255,.06);font-family:var(--mono);font-size:15px;color:var(--accent);letter-spacing:.08em}
#drop-overlay.on{display:flex}
#img-preview img{height:46px;border-radius:3px}
#img-preview span{font-family:var(--mono);font-size:11px;color:var(--sub);flex:1}
#clear-img{background:none;border:none;color:var(--accent2);cursor:pointer;font-size:16px;padding:2px 6px}
#input-row{display:flex;gap:10px;align-items:flex-end}
.ibtn{width:42px;height:42px;flex-shrink:0;background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--sub);cursor:pointer;font-size:17px;display:flex;align-items:center;justify-content:center;transition:all .2s}
.ibtn:hover{border-color:var(--accent);color:var(--accent)}
#msg-in{flex:1;background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--sans);font-size:14px;padding:10px 14px;resize:none;outline:none;min-height:42px;max-height:120px;transition:border-color .2s;scrollbar-width:thin}
#msg-in:focus{border-color:var(--accent)}
#msg-in::placeholder{color:var(--sub)}
#send{width:42px;height:42px;flex-shrink:0;background:var(--accent);border:none;border-radius:4px;color:var(--bg);cursor:pointer;font-size:18px;font-weight:700;transition:all .2s}
#send:hover{background:#33ddff;box-shadow:0 0 14px rgba(0,212,255,.4)}
#send:disabled{background:var(--sub);cursor:not-allowed;box-shadow:none}
.ue-card{margin-top:10px;border:1px solid var(--border);border-left:3px solid var(--sub);border-radius:4px;background:var(--bg);font-family:var(--mono);font-size:11px;overflow:hidden}
.ue-card.ok{border-left-color:var(--accent3)}.ue-card.fail{border-left-color:var(--accent2)}.ue-card.ask{border-left-color:var(--warn)}
.ue-head{padding:8px 12px;color:var(--text);display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.ue-head .st{font-weight:700}.ue-card.ok .st{color:var(--accent3)}.ue-card.fail .st{color:var(--accent2)}.ue-card.ask .st{color:var(--warn)}
.ue-head .pp{color:var(--sub)}
.ue-card details{border-top:1px solid var(--border)}
.ue-card summary{padding:6px 12px;color:var(--accent);cursor:pointer;user-select:none}
.ue-card pre{margin:0;padding:8px 12px;white-space:pre-wrap;word-break:break-word;max-height:360px;overflow:auto}
.ue-code{color:var(--text);background:var(--surface)}
.ue-out{color:var(--sub);border-top:1px solid var(--border)}
.ue-err{color:var(--accent2);border-top:1px solid var(--border)}
.ue-hist{padding:4px 12px;color:var(--sub);font-size:10px;border-top:1px solid var(--border)}
.ue-why{padding:6px 12px;color:var(--warn);border-top:1px solid var(--border)}
.ue-btns{display:flex;gap:8px;padding:8px 12px;border-top:1px solid var(--border);align-items:center}
.ue-btns button{background:transparent;border:1px solid var(--border);color:var(--text);font-family:var(--mono);font-size:11px;padding:5px 12px;border-radius:4px;cursor:pointer}
.ue-btns button.yes:hover{border-color:var(--accent3);color:var(--accent3)}
.ue-btns button.no:hover{border-color:var(--accent2);color:var(--accent2)}
.ue-btns .done{color:var(--sub)}
.act-head{color:var(--accent);padding-bottom:4px;margin-bottom:6px;border-bottom:1px solid var(--border)}
.act-line{white-space:pre-wrap;padding:2px 0}
#dl-bar{display:none;flex:1;position:relative;height:42px;background:var(--panel);border:1px solid var(--accent);border-radius:4px;overflow:hidden}
#dl-fill{position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,rgba(0,212,255,.18),rgba(0,212,255,.38));transition:width .8s ease}
#dl-fill.indet{width:30%!important;animation:dlslide 1.4s ease-in-out infinite}
@keyframes dlslide{0%{left:-30%}100%{left:100%}}
#dl-x{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:30px;height:30px;background:transparent;border:1px solid var(--border);border-radius:4px;color:var(--sub);cursor:pointer;font-size:14px;z-index:2;transition:all .2s}
#dl-x:hover{border-color:#e05555;color:#fff;background:#e05555}
#dl-x:disabled{opacity:.4;cursor:default;background:transparent;color:var(--sub);border-color:var(--border)}
#dl-text{position:absolute;inset:0 44px 0 0;display:flex;align-items:center;padding:0 14px;font-family:var(--mono);font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* Dataset panel */
#ds-panel{display:none;width:310px;flex-shrink:0;background:var(--surface);border-left:1px solid var(--border);flex-direction:column;padding:18px;gap:12px}
#ds-panel.open{display:flex}
.ptitle{font-family:var(--mono);font-size:10px;color:var(--accent);letter-spacing:2px}
#dropzone{border:2px dashed var(--border);border-radius:4px;padding:28px 14px;text-align:center;cursor:pointer;transition:all .2s;font-family:var(--mono);font-size:11px;color:var(--sub)}
#dropzone.over{border-color:var(--accent);color:var(--accent);background:rgba(0,212,255,.04)}
#thumbs{display:flex;flex-wrap:wrap;gap:5px}
.thumb{width:58px;height:58px;object-fit:cover;border-radius:3px;border:1px solid var(--border)}
#dsname{background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--mono);font-size:12px;padding:7px 11px;outline:none;width:100%}
#dsname:focus{border-color:var(--accent)}
.abtn{background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--text);font-size:13px;font-weight:600;padding:9px;cursor:pointer;width:100%;transition:all .2s;text-align:center}
.abtn:hover{border-color:var(--accent);color:var(--accent)}
.abtn.pri{background:var(--accent);color:var(--bg);border-color:var(--accent)}
.abtn.pri:hover{background:#33ddff}

/* Image browser panel */
#ib-panel{display:none;width:340px;flex-shrink:0;background:var(--surface);border-left:1px solid var(--border);flex-direction:column;padding:18px;gap:10px}
#ib-panel.open{display:flex}
#ib-folders{display:flex;flex-direction:column;gap:5px}
.ib-folder-btn{background:var(--panel);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:var(--mono);font-size:11px;padding:7px 11px;cursor:pointer;text-align:left;transition:all .2s}
.ib-folder-btn:hover{border-color:var(--accent);color:var(--accent)}
.ib-folder-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(0,212,255,.06)}
#ib-breadcrumb{font-family:var(--mono);font-size:10px;color:var(--sub);word-break:break-all;min-height:14px}
#ib-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;overflow-y:auto;flex:1;min-height:0}
.ib-thumb{width:100%;aspect-ratio:1;object-fit:cover;border-radius:3px;border:2px solid var(--border);cursor:pointer;transition:all .18s}
.ib-thumb:hover{border-color:var(--accent);box-shadow:0 0 8px rgba(0,212,255,.3)}
.ib-thumb.selected{border-color:var(--accent3);box-shadow:0 0 10px rgba(57,255,20,.4)}
#ib-status{font-family:var(--mono);font-size:10px;color:var(--sub);text-align:center;min-height:14px}
#ib-attach-btn{background:var(--accent);color:var(--bg);border:none;border-radius:4px;font-size:13px;font-weight:600;padding:9px;cursor:pointer;width:100%;transition:all .2s;display:none}
#ib-attach-btn:hover{background:#33ddff}
#ib-attach-btn.show{display:block}

/* Lightbox */
#lb{display:none;position:fixed;inset:0;background:rgba(0,0,0,.92);z-index:10000;align-items:center;justify-content:center;cursor:zoom-out}
#lb.open{display:flex}
#lb img{max-width:90vw;max-height:90vh;border-radius:4px}

::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
/* Game-library drag affordances: cyan = reorder/place · green = nest/into-folder */
#steam-body.dnd .steam-cat{ outline:1px dashed rgba(120,135,160,.35); outline-offset:2px; }
#steam-body.dnd .cat-header{ box-shadow:inset 0 0 0 1px rgba(120,135,160,.22); border-radius:4px; }
.steam-cat.over-body{ box-shadow:0 0 14px rgba(0,212,255,.55)!important; border-color:var(--accent)!important; background:rgba(0,212,255,.06); }
.cat-header.over-head{ box-shadow:inset 0 0 0 2px var(--accent3)!important; background:rgba(57,255,20,.16)!important; border-radius:4px; }
.steam-tile.over-tile{ box-shadow:0 0 12px rgba(0,212,255,.75)!important; outline:2px solid var(--accent); }
/* Sliders & switches (2026-10-31): thick filled track + big glowing knob; checkboxes are
   switches. --k = the control's colour (cyan default), --p = fill % (set by the script below). */
input[type=range]{--k:var(--accent);--p:50%;-webkit-appearance:none;appearance:none;height:8px!important;
  margin:6px 0;border-radius:999px;cursor:pointer;outline:none;border:none;
  background:linear-gradient(90deg,var(--k) 0,var(--k) var(--p),rgba(255,255,255,.09) var(--p),rgba(255,255,255,.09) 100%);
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.05),0 0 8px color-mix(in srgb,var(--k) 22%,transparent)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;appearance:none;width:20px;height:20px;border-radius:50%;
  background:var(--k);border:3px solid var(--panel);box-shadow:0 0 0 1px var(--k),0 0 10px var(--k);transition:transform .12s,box-shadow .12s}
input[type=range]::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:var(--k);border:3px solid var(--panel);
  box-shadow:0 0 0 1px var(--k),0 0 10px var(--k);transition:transform .12s,box-shadow .12s}
input[type=range]::-moz-range-track{background:transparent;border:none}
input[type=range]:hover::-webkit-slider-thumb,input[type=range]:active::-webkit-slider-thumb{transform:scale(1.15);box-shadow:0 0 0 1px var(--k),0 0 16px var(--k)}
input[type=range]:hover::-moz-range-thumb,input[type=range]:active::-moz-range-thumb{transform:scale(1.15);box-shadow:0 0 0 1px var(--k),0 0 16px var(--k)}
input[type=range]:focus-visible{box-shadow:0 0 0 2px color-mix(in srgb,var(--k) 55%,transparent)}
/* on/off settings that are 0-1 sliders under the hood (class tg) look and click like switches */
input[type=range].tg{width:36px!important;height:20px!important;flex:none;margin:4px 0;
  background:color-mix(in srgb,var(--k) calc(var(--p) * .3),rgba(255,255,255,.08));
  box-shadow:inset 0 0 0 1px color-mix(in srgb,var(--k) var(--p),var(--border))}
input[type=range].tg::-webkit-slider-thumb{width:20px;height:20px;border:4px solid transparent;background-clip:content-box;
  background-color:var(--sub);box-shadow:none}
input[type=range].tg[data-on="1"]::-webkit-slider-thumb{background-color:var(--k);filter:drop-shadow(0 0 4px var(--k))}
input[type=range].tg:hover::-webkit-slider-thumb{transform:none}
input[type=checkbox]{--k:var(--accent);-webkit-appearance:none;appearance:none;position:relative;flex:none;
  width:36px!important;height:20px!important;margin:0;border-radius:999px;cursor:pointer;vertical-align:middle;
  background:rgba(255,255,255,.08);box-shadow:inset 0 0 0 1px var(--border);transition:background .18s,box-shadow .18s}
input[type=checkbox]::before{content:'';position:absolute;top:4px;left:4px;width:12px;height:12px;border-radius:50%;
  background:var(--sub);transition:transform .18s,background .18s,box-shadow .18s}
input[type=checkbox]:checked{background:color-mix(in srgb,var(--k) 30%,transparent);
  box-shadow:inset 0 0 0 1px var(--k),0 0 10px color-mix(in srgb,var(--k) 35%,transparent)}
input[type=checkbox]:checked::before{transform:translateX(16px);background:var(--k);box-shadow:0 0 8px var(--k)}
input[type=checkbox]:hover::before{box-shadow:0 0 0 3px color-mix(in srgb,var(--k) 25%,transparent)}
input[type=checkbox]:checked:hover::before{box-shadow:0 0 8px var(--k),0 0 0 3px color-mix(in srgb,var(--k) 25%,transparent)}
input[type=checkbox]:focus-visible{outline:1px solid var(--k);outline-offset:2px}
</style>
<script>
/* Keeps each slider's fill (--p) in step with its value — on drag, when code sets .value,
   and for sliders added later. Switch-style sliders (class tg) flip on click. */
(function(){
  function paint(r){
    if(!r || r.type!=='range') return;
    var mn=r.min===''?0:+r.min, mx=r.max===''?100:+r.max, v=+r.value;
    var p=mx>mn?Math.max(0,Math.min(100,(v-mn)/(mx-mn)*100)):0;
    r.style.setProperty('--p',p+'%');
    if(r.classList.contains('tg')) r.dataset.on=p>=50?'1':'0';
  }
  var d=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value');
  Object.defineProperty(HTMLInputElement.prototype,'value',{configurable:true,enumerable:d.enumerable,
    get:function(){return d.get.call(this);},
    set:function(x){d.set.call(this,x); if(this.type==='range') paint(this);}});
  document.addEventListener('input',function(e){paint(e.target);},true);
  document.addEventListener('mousedown',function(e){
    var r=e.target;
    if(!r || r.type!=='range' || !r.classList.contains('tg')) return;
    e.preventDefault();
    var mn=r.min===''?0:+r.min, mx=r.max===''?100:+r.max;
    r.value=(+r.value>=(mn+mx)/2)?mn:mx;
    r.dispatchEvent(new Event('input',{bubbles:true}));
    r.dispatchEvent(new Event('change',{bubbles:true}));
  },true);
  function scan(n){
    if(!n || n.nodeType!==1) return;
    if(n.matches('input[type=range]')) paint(n);
    n.querySelectorAll('input[type=range]').forEach(paint);
  }
  new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(scan);});})
    .observe(document.documentElement,{childList:true,subtree:true});
  document.addEventListener('DOMContentLoaded',function(){scan(document.body);});
})();
</script>
</head>
<body>
<div id="drop-overlay">⬡ DROP FILES FOR SYSTEM 32</div>
<div id="app">
  <div id="header" style="position:relative">
    <div style="display:flex;align-items:center;gap:6px">
      <div id="settings-btn" onclick="toggleModelPicker()" title="Settings" style="cursor:pointer;padding:0 6px;opacity:0.7;user-select:none;color:var(--muted);display:flex;flex-direction:column;gap:3px;justify-content:center;height:28px">
        <div style="width:18px;height:2px;background:currentColor;border-radius:1px"></div>
        <div style="width:18px;height:2px;background:currentColor;border-radius:1px"></div>
        <div style="width:18px;height:2px;background:currentColor;border-radius:1px"></div>
      </div>
      <div id="logo">SYS<span>TEM</span> 32</div>
      <span id="airplane-ind" title="Airplane mode is on — System 32 is offline" style="display:none;margin-left:10px;font-size:13px;color:#e0a040;letter-spacing:1px;font-weight:700">&#9992; OFFLINE</span>
    </div>
    <style>
      .cat-popup{display:none;position:fixed;width:380px;min-width:300px;max-width:94vw;height:60vh;min-height:240px;background:rgba(18,20,26,0.92);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);border:1px solid var(--border);border-radius:8px;z-index:1000;box-shadow:0 8px 40px rgba(0,0,0,0.7);flex-direction:column;overflow:hidden;resize:both}
      .cat-popup.open{display:flex}
      .cat-head{cursor:move;display:flex;align-items:center;justify-content:space-between;padding:9px 13px;background:rgba(30,34,44,0.82);border-bottom:1px solid var(--border);user-select:none;flex-shrink:0}
      .cat-head .ttl{font-size:11px;letter-spacing:2px;font-weight:700}
      .cat-head .x{cursor:pointer;color:var(--muted);font-size:18px;line-height:1;padding:0 4px}
      .cat-body{overflow-y:auto;overflow-x:hidden;padding:16px;flex:1;min-height:0}
      .cat-btn{display:block;width:100%;text-align:left;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:5px;padding:11px 13px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px;font-family:var(--mono);transition:border-color .15s,color .15s}
      .cat-btn:hover{border-color:var(--accent);color:var(--accent)}
    </style>

    <!-- SETTINGS HUB — launcher of category windows -->
    <div id="model-picker" style="display:none;position:absolute;top:44px;left:0;background:rgba(18,20,26,0.92);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);border:1px solid var(--border);border-radius:6px;z-index:999;width:300px;min-width:240px;min-height:200px;max-height:80vh;box-shadow:0 4px 24px rgba(0,0,0,0.6);flex-direction:column;resize:both;overflow:hidden">
      <div id="picker-head" onmousedown="startPickerDrag(event)" style="cursor:move;display:flex;align-items:center;justify-content:space-between;padding:8px 12px;background:rgba(30,34,44,0.82);border-bottom:1px solid var(--border);border-radius:6px 6px 0 0;user-select:none;flex-shrink:0">
        <span style="font-size:11px;letter-spacing:2px;color:var(--muted)">&#9881; SETTINGS</span>
        <span onclick="toggleModelPicker()" title="Close" style="cursor:pointer;color:var(--muted);font-size:18px;line-height:1;padding:0 4px">&times;</span>
      </div>
      <div style="overflow-y:auto;overflow-x:hidden;padding:14px;flex:1;min-height:0;display:flex;flex-direction:column;gap:9px">
        <button class="cat-btn" onclick="openCat('model')">&#129504; MODEL SWITCHING</button>
        <button class="cat-btn" onclick="openCat('identity')">&#128100; IDENTITY</button>
        <button class="cat-btn" onclick="openCat('voices')">&#128266; INPUT &middot; OUTPUT</button>
        <button class="cat-btn" onclick="openCat('data')">&#128190; DATA &middot; SAVE &middot; RESET</button>
        <button class="cat-btn" onclick="openCat('dome')">&#9960; IRON DOME</button>
        <button class="cat-btn" onclick="openUICustom()">&#127912; CHAT CUSTOMIZATIONS</button>
        <button class="cat-btn" onclick="openCat('ext')">&#129513; EXTENSIONS</button>
      </div>
    </div>

    <!-- MODEL -->
    <div id="cat-model" class="cat-popup" style="top:78px;left:88px">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-model')" style="border-bottom-color:var(--accent)">
        <span class="ttl" style="color:var(--accent)">&#129504; MODEL SWITCHING</span>
        <span class="x" onclick="closeCat('cat-model')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px;letter-spacing:1px">SWITCH MODEL</div>
      <div id="model-current" style="font-size:11px;color:var(--fg);margin-bottom:10px;padding:5px 8px;background:var(--bg3);border-radius:4px;border:1px solid var(--border)">&mdash;</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
        <div>
          <div id="model-ctx-lbl" data-base="CONTEXT" style="font-size:10px;color:var(--muted);margin-bottom:3px">CONTEXT</div>
          <input id="model-ctx" type="number" value="131072" min="4096" max="262144" step="4096"
            style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
        </div>
        <div>
          <div id="model-gpu-lbl" data-base="GPU LAYERS" style="font-size:10px;color:var(--muted);margin-bottom:3px">GPU LAYERS</div>
          <input id="model-gpu-layers" type="number" value="20" min="0" max="200" step="1"
            style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
        </div>
        <div>
          <div id="model-threads-lbl" data-base="THREADS" style="font-size:10px;color:var(--muted);margin-bottom:3px">THREADS</div>
          <input id="model-threads" type="number" value="14" min="1" max="64" step="1"
            style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);margin-bottom:3px">BATCH SIZE</div>
          <input id="model-batch" type="number" value="512" min="64" max="4096" step="64"
            style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);margin-bottom:3px">KV CACHE</div>
          <select id="model-kv" onchange="onKvChange()"
            style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
            <option value="f16">f16 (full)</option>
            <option value="q8_0">q8_0 (&frac12; VRAM)</option>
            <option value="q4_0">q4_0 (&frac14; VRAM)</option>
          </select>
        </div>
        <div>
          <div style="font-size:10px;color:var(--muted);margin-bottom:3px">FLASH ATTN</div>
          <select id="model-flash"
            style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box">
            <option value="0">Off</option>
            <option value="1">On</option>
          </select>
        </div>
      </div>
      <button onclick="openCat('tuning')" style="width:100%;margin-bottom:8px;background:var(--bg3);color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:7px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">&#127899; MODEL TUNING</button>
      <div style="display:flex;gap:8px">
        <button onclick="browseModelFile()" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:12px;cursor:pointer;font-weight:600">&#128193; Browse</button>
        <button id="model-load-btn" onclick="loadSelectedModel()" style="flex:1;background:var(--accent);color:#000;border:none;border-radius:4px;padding:6px 10px;font-size:12px;cursor:pointer;opacity:1;font-weight:700" disabled>Load</button>
      </div>
      <div id="model-status" style="font-size:11px;color:var(--muted);margin-top:6px"></div>
      </div>
    </div>

    <!-- CHAT CUSTOMIZATIONS -->
    <div id="ui-custom-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:1400;align-items:center;justify-content:center" onclick="if(event.target===this)closeUICustom()">
      <div style="width:340px;max-height:86vh;overflow-y:auto;background:rgba(18,20,26,0.97);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid var(--border);border-radius:8px;box-shadow:0 6px 32px rgba(0,0,0,0.7);padding:18px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
          <span style="font-family:var(--mono);font-size:13px;letter-spacing:1px;color:var(--accent2);font-weight:700">&#127912; CHAT CUSTOMIZATIONS</span>
          <span onclick="closeUICustom()" title="Close" style="cursor:pointer;color:var(--muted);font-size:18px;line-height:1;padding:0 4px">&times;</span>
        </div>

        <div style="margin-bottom:14px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:.5px">SYSTEM 32 BUBBLE</div>
          <input id="uic-ai" type="color" value="#0c1018" oninput="uicLive()" style="width:100%;height:34px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;cursor:pointer;padding:2px">
        </div>

        <div style="margin-bottom:14px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:.5px">SYSTEM 32 BORDER &amp; TAG</div>
          <input id="uic-ai-accent" type="color" value="#00d4ff" oninput="uicLive()" style="width:100%;height:34px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;cursor:pointer;padding:2px">
        </div>

        <div style="margin-bottom:14px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:.5px">USER BUBBLE</div>
          <input id="uic-user" type="color" value="#3a1620" oninput="uicLive()" style="width:100%;height:34px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;cursor:pointer;padding:2px">
        </div>

        <div style="margin-bottom:14px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:.5px">USER BORDER &amp; TAG</div>
          <input id="uic-user-accent" type="color" value="#ff4060" oninput="uicLive()" style="width:100%;height:34px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;cursor:pointer;padding:2px">
        </div>

        <div style="margin-bottom:14px">
          <label style="display:flex;align-items:center;justify-content:space-between;gap:8px;cursor:pointer">
            <span>
              <span style="font-size:10px;color:var(--muted);letter-spacing:.5px;display:block">SHOW THOUGHTS</span>
              <span style="font-size:9px;color:var(--muted);opacity:.7">the 💭 reasoning lines in chat &mdash; off hides them (still saved)</span>
            </span>
            <input id="uic-thoughts" type="checkbox" checked onchange="uicLive()" style="width:18px;height:18px;cursor:pointer;accent-color:var(--accent)">
          </label>
        </div>

        <div style="margin-bottom:14px">
          <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:.5px">BACKGROUND</div>
          <div style="display:flex;gap:8px;margin-bottom:8px">
            <button id="uic-bgmode-color" onclick="uicBgMode('color')" style="flex:1;background:var(--accent);color:#000;border:none;border-radius:4px;padding:6px;font-size:11px;cursor:pointer;font-weight:700">Color</button>
            <button id="uic-bgmode-image" onclick="uicBgMode('image')" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px;font-size:11px;cursor:pointer;font-weight:600">Image</button>
          </div>
          <div id="uic-bg-color-row">
            <input id="uic-bg" type="color" value="#040608" oninput="uicLive()" style="width:100%;height:34px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;cursor:pointer;padding:2px">
          </div>
          <div id="uic-bg-image-row" style="display:none">
            <input id="uic-bg-file" type="file" accept="image/*" onchange="uicBgFile(event)" style="width:100%;font-size:11px;color:var(--sub)">
            <div style="font-size:10px;color:var(--muted);margin-top:5px">Tip: keep images reasonably sized — they travel with the drive.</div>
          </div>
        </div>

        <div style="display:flex;gap:8px;margin-top:18px">
          <button onclick="resetUI()" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:12px;cursor:pointer;font-weight:600">Reset to Default</button>
          <button onclick="saveUI()" style="flex:1;background:var(--accent2);color:#000;border:none;border-radius:4px;padding:8px;font-size:12px;cursor:pointer;font-weight:700">Save UI</button>
        </div>
        <div id="uic-status" style="font-size:11px;color:var(--muted);margin-top:8px;text-align:center;min-height:14px"></div>
      </div>
    </div>

    <!-- IDENTITY -->
    <div id="cat-identity" class="cat-popup" style="top:104px;left:120px">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-identity')">
        <span class="ttl" style="color:var(--fg)">&#128100; IDENTITY</span>
        <span class="x" onclick="closeCat('cat-identity')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:1px">NAME (what the assistant is called)</div>
        <input id="identity-name" type="text" placeholder="System 32" style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:12px;font-family:var(--mono);box-sizing:border-box;margin-bottom:12px">
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:1px">GENDER</div>
        <select id="identity-gender" style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:12px;font-family:var(--mono);box-sizing:border-box;margin-bottom:12px;cursor:pointer">
          <option value="Neutral">Neutral</option>
          <option value="Female">Female</option>
          <option value="Male">Male</option>
        </select>
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:1px">IDENTITY</div>
        <textarea id="identity-editor" rows="10" style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:11px;font-family:var(--mono);resize:vertical;box-sizing:border-box;line-height:1.5"></textarea>
        <button onclick="saveIdentity(this)" style="margin-top:5px;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 12px;font-size:11px;cursor:pointer;font-weight:600">Save</button>
        <div style="font-size:9px;color:var(--muted);margin-top:6px;line-height:1.4">Name updates the interface now and takes full effect on next restart. "System 32" stays the platform.</div>
      </div>
    </div>

    <!-- VOICES -->
    <div id="cat-voices" class="cat-popup" style="top:130px;left:152px">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-voices')" style="border-bottom-color:var(--accent)">
        <span class="ttl" style="color:var(--accent)">&#128266; INPUT &middot; OUTPUT</span>
        <span class="x" onclick="closeCat('cat-voices')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
        <div style="font-size:10px;color:var(--muted);margin-bottom:5px;letter-spacing:1px">VOICE</div>
        <div style="font-size:10px;color:var(--muted);margin-bottom:4px">CURRENT: <span id="current-voice-label" style="color:var(--fg)">&mdash;</span></div>
        <select id="voice-select" style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px 8px;font-size:12px;box-sizing:border-box"></select>
        <div style="display:flex;gap:6px;margin-top:5px">
          <button onclick="previewVoice()" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:5px;font-size:11px;cursor:pointer">&#9654; Preview</button>
          <button onclick="setVoice()" style="flex:1;background:var(--accent);color:#000;border:none;border-radius:4px;padding:5px;font-size:11px;cursor:pointer;font-weight:700">Set Voice</button>
        </div>
        <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
          <span style="font-size:10px;color:var(--muted);letter-spacing:1px">VOLUME</span>
          <input type="range" id="voice-volume" min="0" max="100" value="100"
                 oninput="onVolumeInput(this.value)" onchange="commitVolume(this.value)"
                 style="flex:1;accent-color:var(--accent);cursor:pointer">
          <span id="voice-volume-val" style="font-size:10px;color:var(--fg);width:30px;text-align:right">100%</span>
        </div>
        <div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
          <div style="font-size:10px;color:var(--muted);letter-spacing:2px;margin-bottom:8px">VOICE INPUT</div>
          <div style="display:flex;align-items:center;gap:6px">
            <button id="stt-toggle" onclick="toggleSTT()"
                    style="background:var(--bg3);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:11px;font-weight:700;letter-spacing:1px;cursor:pointer;white-space:nowrap">&#127908; OFF</button>
            <button id="stt-mode" onclick="toggleSTTMode()" title="Continuous: talk and it sends on a pause. Push-to-talk: only sends what you say while holding the talk button."
                    style="background:var(--bg3);color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:6px 8px;font-size:10px;font-weight:700;letter-spacing:1px;cursor:pointer;white-space:nowrap">CONT</button>
            <select id="stt-mic" onchange="selectMic(this.value)"
                    style="flex:1;min-width:0;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:11px;box-sizing:border-box"></select>
          </div>
          <div style="margin-top:8px;height:8px;background:var(--bg3);border:1px solid var(--border);border-radius:4px;overflow:hidden">
            <div id="stt-level" style="height:100%;width:0%;background:var(--accent3);transition:width .08s linear"></div>
          </div>
          <div id="stt-status" style="font-size:9px;color:var(--muted);margin-top:4px;min-height:11px"></div>
        </div>
        <div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <span style="font-size:10px;color:var(--muted);letter-spacing:2px">&#128065; SCREEN VISION</span>
            <button id="vision-toggle" onclick="toggleVision()" style="background:var(--bg3);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:5px 12px;font-size:11px;font-weight:700;letter-spacing:1px;cursor:pointer;white-space:nowrap">&#9679; OFF</button>
          </div>
          <div style="font-size:9px;color:var(--muted);opacity:.75;margin-bottom:9px">ambient screen awareness &mdash; turn off any time for privacy</div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:9px">
            <span style="font-size:10px;color:var(--muted);width:54px">CAPTURE</span>
            <button id="vision-mode" onclick="toggleVisionMode()" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:10px;font-weight:700;letter-spacing:1px;cursor:pointer">CURSOR MONITOR</button>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:10px;color:var(--muted);width:54px">FPS</span>
            <input type="range" id="vision-fps" min="1" max="10" step="1" value="2" oninput="onVisionFps(this.value)" onchange="commitVisionFps(this.value)" style="flex:1;height:3px;accent-color:var(--accent);cursor:pointer">
            <span id="vision-fps-val" style="font-size:11px;color:var(--fg);width:18px;text-align:right">2</span>
          </div>
          <div id="vision-status" style="font-size:9px;color:var(--muted);margin-top:5px;min-height:11px"></div>
        </div>
      </div>
    </div>

    <!-- DATA -->
    <div id="cat-data" class="cat-popup" style="top:96px;left:184px;height:66vh">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-data')" style="border-bottom-color:var(--accent)">
        <span class="ttl" style="color:var(--accent)">&#128190; DATA &middot; SAVE &middot; RESET</span>
        <span class="x" onclick="closeCat('cat-data')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
        <div style="display:flex;align-items:center;gap:7px;margin-bottom:12px;padding:7px 9px;background:var(--bg3);border:1px solid var(--accent);border-radius:5px">
          <span style="font-size:9px;color:var(--muted);letter-spacing:1px;white-space:nowrap">CURRENT STATE</span>
          <span id="current-state-name" style="font-size:11px;color:var(--accent);font-weight:700;font-family:var(--mono);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">&mdash;</span>
        </div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:2px;margin-bottom:4px">MEMORY FILES</div>
        <div style="font-size:9px;color:var(--muted);opacity:.7;margin-bottom:5px">the working files that get swapped in/out by Save &amp; Load States</div>
        <div id="memory-file-list" style="font-size:10px;color:var(--muted);margin-bottom:8px;line-height:1.6"></div>
        <button onclick="showMemoryReset()" style="width:100%;background:transparent;color:#ff7a1a;border:2px solid #ff7a1a;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700;letter-spacing:1px">RESET TO BLANK SLATE</button>
        <div style="display:flex;gap:8px;margin-top:8px">
          <button onclick="saveState(this)" style="flex:1;background:var(--bg3);color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:8px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">&#128190; SAVE STATE</button>
          <button onclick="showLoadState()" style="flex:1;background:var(--bg3);color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:8px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">&#128229; LOAD STATE</button>
        </div>
        <input id="savestate-name" type="text" placeholder="name (optional &mdash; reuse a name to overwrite)" style="width:100%;margin-top:6px;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:11px;box-sizing:border-box">
        <button onclick="exportTraining(this)" style="width:100%;margin-top:8px;background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:8px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">&#129504; EXPORT TRAINING DATA</button>
        <div id="state-status" style="font-size:10px;color:var(--muted);margin-top:6px;min-height:12px"></div>
        <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
          <div style="font-size:10px;color:var(--muted);letter-spacing:2px;margin-bottom:2px">&#127918; GAMEPLAY DATA</div>
          <div id="gameplay-stats" style="font-size:9px;color:var(--muted);margin-bottom:6px"></div>
          <div id="gameplay-list" style="display:flex;flex-direction:column;gap:4px;max-height:150px;overflow:auto"></div>
          <button onclick="resetGameplay(this)" style="width:100%;margin-top:8px;background:transparent;color:#ff7a1a;border:2px solid #ff7a1a;border-radius:4px;padding:6px;font-size:10px;cursor:pointer;font-weight:700;letter-spacing:1px">RESET GAMING TRAINING DATA</button>
          <div style="display:flex;gap:6px;margin-top:8px">
            <button onclick="exportGameplay('good',this)" style="flex:1;background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:6px;font-size:10px;cursor:pointer;font-weight:700;letter-spacing:1px">EXPORT GOOD</button>
            <button onclick="exportGameplay('all',this)" style="flex:1;background:var(--bg3);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:6px;font-size:10px;cursor:pointer;font-weight:700;letter-spacing:1px">EXPORT ALL</button>
          </div>
          <div id="gameplay-status" style="font-size:9px;color:var(--muted);margin-top:4px;min-height:11px"></div>
        </div>
        <div style="margin-top:14px;padding-top:10px;border-top:1px solid var(--border)">
          <div style="font-size:10px;color:#e05555;letter-spacing:2px;margin-bottom:4px">&#9888; FACTORY RESET</div>
          <div style="font-size:9px;color:var(--muted);margin-bottom:6px;line-height:1.4">Erase everything anyone has done with System 32 on this drive &mdash; every person's memories, chats, files, generations and save states. Only what's needed to run stays.</div>
          <button onclick="showFactoryReset()" style="width:100%;background:transparent;color:#e05555;border:2px solid #e05555;border-radius:4px;padding:8px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">FACTORY RESET</button>
        </div>
      </div>
    </div>

    <!-- IRON DOME -->
    <div id="cat-dome" class="cat-popup" style="top:122px;left:216px;height:66vh">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-dome')" style="border-bottom-color:#e05555">
        <span class="ttl" style="color:#e05555">&#9960; THE IRON DOME</span>
        <span class="x" onclick="closeCat('cat-dome')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
        <label id="airplane-row" style="display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;letter-spacing:1px;margin-bottom:10px;cursor:pointer">
          <input type="checkbox" id="airplane-toggle" onchange="setAirplane(this.checked)" style="cursor:pointer;--k:#e0a040">
          <span>&#9992; AIRPLANE MODE</span>
          <span id="airplane-note" style="font-weight:400;letter-spacing:0;color:var(--muted);font-size:9px">— System 32 never touches the internet</span>
        </label>
        <div id="dome-learn" style="font-size:9px;color:var(--muted);margin-bottom:8px">baselining&hellip;</div>
        <button id="dome-btn" onclick="domeButtonClick()"
                style="width:100%;background:transparent;color:#e05555;border:2px solid #e05555;border-radius:4px;padding:10px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">CLOSE UNNECESSARY PORTS &amp; PROCESSES</button>
        <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-top:12px;margin-bottom:4px">WHITELISTED PORTS / PROCESSES</div>
        <textarea id="dome-whitelist" rows="2" placeholder="e.g. 27015 steam.exe discord.exe &mdash; numbers are ports, words are process names"
                  style="width:100%;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:11px;box-sizing:border-box;font-family:var(--mono);resize:vertical"></textarea>
        <button onclick="saveWhitelist(this)"
                style="width:100%;margin-top:6px;background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:6px;font-size:11px;cursor:pointer;font-weight:700;letter-spacing:1px">SAVE WHITELIST</button>
        <div id="dome-wl-status" style="font-size:9px;color:var(--muted);margin-top:4px;min-height:11px"></div>
        <div style="font-size:10px;color:var(--muted);letter-spacing:1px;margin-top:12px;margin-bottom:4px">COMPLETE PORT LIST</div>
        <div id="dome-ports" style="font-size:10px;font-family:var(--mono);height:170px;min-height:60px;resize:vertical;overflow:auto;border:1px solid var(--border);border-radius:4px;padding:6px;line-height:1.7;color:var(--muted)">&hellip;</div>
      </div>
    </div>

    <!-- MODEL TUNING -->
    <div id="cat-tuning" class="cat-popup" style="top:134px;left:232px;height:auto;min-height:0;width:340px">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-tuning')" style="border-bottom-color:var(--accent)">
        <span class="ttl" style="color:var(--accent)">&#127899; MODEL TUNING</span>
        <span class="x" onclick="closeCat('cat-tuning')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
        <div style="font-size:9px;color:var(--muted);margin-bottom:8px;line-height:1.5">
          Perceived-intellect controls. These apply <b>live</b> &mdash; the next message obeys them, no reboot.
        </div>
        <div id="tuning-sliders" style="font-size:11px">&hellip;</div>
        <button id="tuning-reset" onclick="resetTuning()"
          style="width:100%;margin-top:12px;background:var(--bg3);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:7px;font-size:11px;cursor:pointer;font-weight:700;letter-spacing:1px">RESET TO DEFAULTS</button>
        <div id="tuning-status" style="font-size:9px;color:var(--muted);margin-top:6px;min-height:11px"></div>
      </div>
    </div>

    <!-- EXTENSIONS -->
    <div id="cat-ext" class="cat-popup" style="top:148px;left:248px;height:auto;min-height:0">
      <div class="cat-head" onmousedown="startCatDrag(event,'cat-ext')" style="border-bottom-color:var(--accent3)">
        <span class="ttl" style="color:var(--accent3)">&#129513; EXTENSIONS</span>
        <span class="x" onclick="closeCat('cat-ext')" title="Close">&times;</span>
      </div>
      <div class="cat-body">
        <button onclick="window.open('http://localhost:8188','_blank')" style="width:100%;background:var(--bg3);color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:10px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">OPEN COMFYUI</button>
        <button onclick="openSteamGames()" style="width:100%;margin-top:10px;background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:10px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">&#127918; OPEN GAME LIBRARY</button>
        <div id="unreal-ext" style="display:none;margin-top:12px;border-top:1px solid var(--border);padding-top:12px">
          <div id="unreal-ext-label" style="font-size:10px;color:var(--muted);margin-bottom:8px;letter-spacing:.5px;line-height:1.4"></div>
          <div style="display:flex;gap:6px;margin-bottom:8px">
            <input id="unreal-proj" type="text" placeholder="Path to your .uproject (blank = editor only)" style="flex:1;background:var(--bg2);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:7px 8px;font-size:11px;font-family:var(--mono)">
            <button onclick="saveUnrealProject()" style="background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:7px 11px;font-size:11px;cursor:pointer;font-weight:700;letter-spacing:.5px">SET</button>
          </div>
          <button onclick="launchUnreal('pc')" style="width:100%;background:var(--bg3);color:var(--accent2);border:1px solid var(--accent2);border-radius:4px;padding:10px;font-size:12px;cursor:pointer;font-weight:700;letter-spacing:1px">&#127760; LAUNCH UNREAL</button>
          <div id="unreal-spawn-result" style="font-size:10px;color:var(--muted);margin-top:6px;font-family:var(--mono);word-break:break-all;line-height:1.4"></div>
        </div>
      </div>
    </div>


    <!-- Iron Dome close-plan popup -->
    <div id="dome-plan-popup" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg2);border:1px solid #e05555;border-radius:8px;padding:20px;z-index:1100;min-width:380px;max-width:520px;max-height:75vh;overflow:auto;box-shadow:0 8px 32px rgba(0,0,0,0.7)">
      <div style="font-size:14px;font-weight:700;margin-bottom:6px;color:#e05555;text-align:center">⛨ Iron Dome — close plan</div>
      <div id="dome-plan-body" style="font-size:12px;color:var(--fg);line-height:1.6"></div>
      <div id="dome-arm-controls" style="margin-top:14px;display:none">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <span style="font-size:11px;color:var(--muted);white-space:nowrap">PROCESSES:</span>
          <select id="dome-arm-mode" style="flex:1;background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;font-size:12px">
            <option value="block">Block network only</option>
            <option value="kill">Kill</option>
            <option value="both">Block &amp; Kill</option>
          </select>
        </div>
        <div style="display:flex;gap:8px">
          <button id="dome-arm-btn" onclick="domeArm()" style="flex:1;background:#e05555;color:#fff;border:none;border-radius:4px;padding:9px;font-size:13px;cursor:pointer;font-weight:700;letter-spacing:1px">&#9960; ARM THE DOME</button>
          <button onclick="document.getElementById('dome-plan-popup').style.display='none'" style="flex:1;background:#3a3f4a;color:#fff;border:none;border-radius:4px;padding:9px;font-size:13px;cursor:pointer;font-weight:700">Cancel</button>
        </div>
      </div>
      <button id="dome-plan-close" onclick="document.getElementById('dome-plan-popup').style.display='none'" style="width:100%;margin-top:14px;background:#6e2a2a;color:#fff;border:none;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700">Close</button>
    </div>

    <!-- Steam games launcher (movable, transparent, scrollable) -->
    <div id="steam-popup" style="display:none;position:fixed;top:70px;left:50%;transform:translate(-50%,0);width:560px;min-width:340px;max-width:92vw;height:70vh;background:rgba(18,20,26,0.92);backdrop-filter:blur(6px);border:1px solid var(--accent3);border-radius:8px;z-index:1100;box-shadow:0 8px 40px rgba(0,0,0,0.7);display:none;flex-direction:column;overflow:hidden;resize:both">
      <div id="steam-header" onmousedown="startSteamDrag(event)" style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border);cursor:move;background:rgba(0,0,0,0.25)">
        <span style="font-size:12px;font-weight:700;letter-spacing:2px;color:var(--accent3);white-space:nowrap">GAME LIBRARY</span>
        <span id="lib-stores" style="display:flex;align-items:center;gap:10px;flex:1;min-width:0;overflow:hidden;flex-wrap:wrap"></span>
        <span id="steam-count" style="font-size:10px;color:var(--muted);white-space:nowrap"></span>
        <span onclick="document.getElementById('steam-popup').style.display='none'" title="Close" style="cursor:pointer;color:var(--muted);font-size:16px;line-height:1">✕</span>
      </div>
      <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-bottom:1px solid var(--border);background:rgba(0,0,0,0.15)">
        <button onclick="newSteamCategory()" style="background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:3px 8px;font-size:10px;cursor:pointer;font-weight:700">+ Category</button>
        <label style="display:flex;align-items:center;gap:4px;font-size:10px;color:var(--muted);cursor:pointer;margin-left:auto">
          <input type="checkbox" id="steam-show-hidden" onchange="loadSteamGames()" style="--k:var(--accent3);cursor:pointer"> show hidden <span id="steam-hidden-count"></span>
        </label>
      </div>
      <div id="steam-banner" style="display:none;padding:8px 12px;background:rgba(224,85,85,0.15);border-bottom:1px solid #e05555;font-size:11px;color:#ffb3b3;align-items:center;justify-content:space-between"></div>
      <div id="steam-body" style="flex:1;overflow:auto;padding:10px 12px">
        <div style="color:var(--muted);font-size:12px">Scanning Steam libraries…</div>
      </div>
    </div>

    <!-- Load state (snapshot picker) popup -->
    <div id="load-state-popup" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg2);border:1px solid var(--accent);border-radius:8px;padding:20px;z-index:1100;min-width:360px;max-height:70vh;overflow:auto;box-shadow:0 8px 32px rgba(0,0,0,0.7)">
      <div style="font-size:14px;font-weight:700;margin-bottom:10px;color:var(--fg);text-align:center">Restore a saved state</div>
      <div style="font-size:11px;color:var(--muted);text-align:center;margin-bottom:10px">This overwrites current memory, identity, profiles and favorites with the chosen snapshot.</div>
      <div id="snapshot-list" style="display:flex;flex-direction:column;gap:6px;margin-bottom:12px"></div>
      <button onclick="document.getElementById('load-state-popup').style.display='none'" style="width:100%;background:#6e2a2a;color:#fff;border:none;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700">Cancel</button>
    </div>

    <!-- Memory reset confirmation popup -->
    <div id="memory-reset-popup" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg2);border:1px solid #e0a055;border-radius:8px;padding:20px;z-index:1100;min-width:360px;max-width:460px;box-shadow:0 8px 32px rgba(0,0,0,0.7)">
      <div style="font-size:14px;font-weight:700;margin-bottom:8px;color:var(--fg);text-align:center">Reset to a blank slate?</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px;line-height:1.5">This clears the <b>current working state</b> back to a neutral base — memory, learned preferences, identity, accumulated lessons, and the in-progress training log. The files cleared:</div>
      <div id="reset-file-list" style="font-size:10px;color:var(--muted);background:var(--bg3);border-radius:4px;padding:8px;margin-bottom:10px;line-height:1.8;font-family:var(--mono)"></div>
      <div style="font-size:11px;color:var(--accent3);text-align:center;margin-bottom:12px;line-height:1.5">Your <b>saved states aren't touched</b> — load any snapshot to bring a character back. To keep this one, Save State (and Export Training Data) first.</div>
      <div style="display:flex;gap:8px">
        <button onclick="confirmMemoryReset()" style="flex:1;background:var(--bg3);color:#e0a055;border:1px solid #e0a055;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700">Reset to blank</button>
        <button onclick="document.getElementById('memory-reset-popup').style.display='none'" style="flex:1;background:var(--accent);color:#000;border:none;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700">Keep current</button>
      </div>
    </div>
    <!-- Factory reset confirmation popup -->
    <div id="factory-reset-popup" style="display:none;position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:var(--bg2);border:2px solid #e05555;border-radius:8px;padding:20px;z-index:1101;min-width:360px;max-width:480px;box-shadow:0 8px 32px rgba(0,0,0,0.7)">
      <div style="font-size:15px;font-weight:700;margin-bottom:8px;color:#e05555;text-align:center">Factory reset System 32?</div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:8px;line-height:1.5">This permanently erases <b>everything about everyone</b> who has used this drive, on every computer:</div>
      <div id="fr-list" style="font-size:10px;color:var(--fg);background:var(--bg3);border-radius:4px;padding:8px;margin-bottom:8px;line-height:1.8"></div>
      <div id="fr-keeps" style="font-size:10px;color:var(--accent3);margin-bottom:10px;line-height:1.5"></div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:6px">It can't be undone. System 32 closes afterwards &mdash; start it again for a fresh first boot. Type <b style="color:#e05555">RESET</b> to confirm:</div>
      <input id="fr-confirm" type="text" autocomplete="off" oninput="document.getElementById('fr-go').disabled=(this.value.trim().toUpperCase()!=='RESET')" style="width:100%;margin-bottom:10px;background:var(--bg3);color:var(--fg);border:1px solid #e05555;border-radius:4px;padding:7px 8px;font-size:13px;box-sizing:border-box;letter-spacing:2px">
      <div style="display:flex;gap:8px">
        <button id="fr-go" disabled onclick="confirmFactoryReset(this)" style="flex:1;background:#6e2a2a;color:#fff;border:none;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700">Erase everything</button>
        <button onclick="document.getElementById('factory-reset-popup').style.display='none'" style="flex:1;background:var(--accent);color:#000;border:none;border-radius:4px;padding:8px;font-size:13px;cursor:pointer;font-weight:700">Cancel</button>
      </div>
      <div id="fr-status" style="font-size:11px;color:var(--muted);margin-top:8px;text-align:center;min-height:14px"></div>
    </div>
    <div id="dots">
      <div class="sdot"><div class="dot" id="dlm"></div>LM</div>
      <div id="wf-dots" style="display:flex;gap:16px;align-items:center"></div>
      <div class="sdot" id="job-dot"><div class="dot busy"></div><span id="job-txt"></span></div>
    </div>
  </div>
  <div id="prog"><div id="prog-bar"></div></div>
  <div id="upd-banner">
    <span id="upd-icon">⬆</span>
    <span id="upd-text"></span>
    <span id="upd-btns">
      <button id="upd-notes-btn" onclick="updToggleNotes()">What's new</button>
      <button id="upd-go" onclick="updApply()">Update</button>
      <button id="upd-later" onclick="updLater()">Not now</button>
    </span>
    <span id="upd-roll" style="display:none">
      <button id="upd-roll-btn" onclick="updRollback()">Roll back</button>
      <button onclick="updHide()">OK</button>
    </span>
    <pre id="upd-notes"></pre>
  </div>
  <div id="sec-banner">
    <span id="sec-icon">⚠</span>
    <span id="sec-text"></span>
    <span id="sec-alert-btns">
      <button id="sec-approve" onclick="secApprove()">Mark safe</button>
      <button id="sec-kill" onclick="secKill()">Kill it</button>
      <button id="sec-dismiss" onclick="secDismiss()">Dismiss</button>
    </span>
    <span id="sec-confirm-btns" style="display:none">
      <button id="sec-confirm" onclick="secConfirm()">Confirm</button>
      <button id="sec-cancel" onclick="secCancel()">Cancel</button>
    </span>
  </div>
  <div id="main">
    <div id="chat">
      <div id="msgs">
        <div class="msg sys" id="status-msg"><div class="bub sys" id="status-txt">⬡ SYSTEM 32 - offline - initializing services...</div></div>
      </div>
      <div id="input-area">
        <div id="img-preview">
          <img id="prev-img" src="" alt="" style="display:none">
          <video id="prev-vid" style="display:none;max-height:60px;border-radius:4px" muted></video>
          <span id="prev-icon" style="display:none;font-size:24px"></span>
          <span id="prev-name">file</span>
          <span id="doc-summary" style="display:none;font-family:var(--mono);font-size:11px;color:var(--accent);margin-left:6px"></span>
          <button id="clear-img" onclick="clearImg()" title="Remove all">✕</button>
          <div id="drop-tray"></div>
        </div>
        <div id="sort-bar">
          <span id="sort-text"></span>
          <span style="flex:1"></span>
          <button id="sort-copy" onclick="runSort('copy')">Copy</button>
          <button id="sort-move" onclick="runSort('move')">Move</button>
          <button id="sort-cancel" onclick="cancelSort()">✕</button>
        </div>
        <div id="input-row">
          <button class="ibtn" title="Attach file" onclick="document.getElementById('file-in').click()">📎</button>
          <button class="ibtn" title="Browse files" onclick="openIb()">🗂</button>
          <input type="file" id="file-in" accept="*/*" multiple style="display:none" onchange="onFile(this)">
          <textarea id="msg-in" placeholder="Talk to System 32..." rows="1"
            onkeydown="onKey(event)" oninput="resize(this)"></textarea>
          <div id="dl-bar"><div id="dl-fill"></div><div id="dl-text"></div>
            <button id="dl-x" onclick="cancelActivity()" title="Cancel">✕</button></div>
          <button id="send" onclick="send()">➤</button>
          <button id="stop" onclick="cancelRun()" title="Stop (cancel this response)"
            style="display:none;width:42px;height:42px;flex-shrink:0;background:#e05555;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:18px;font-weight:700;transition:all .2s">⏹</button>
        </div>
      </div>
    </div>
    <div id="ib-panel">
      <div class="ptitle">▸ IMAGE BROWSER</div>
      <div id="ib-folders"></div>
      <div id="ib-breadcrumb"></div>
      <div id="ib-grid"></div>
      <div id="ib-status"></div>
      <button id="ib-attach-btn" onclick="ibAttach()">ATTACH SELECTED</button>
      <button class="abtn" onclick="closeIb()">CLOSE</button>
    </div>
    <div id="ds-panel">
      <div class="ptitle">▸ ADD TRAINING DATA</div>
      <div>
        <div style="font-size:10px;color:var(--sub);margin-bottom:5px;font-family:var(--mono)">DATASET NAME</div>
        <input id="dsname" placeholder="e.g. MyCharacter" type="text">
      </div>
      <div id="dropzone"
        onclick="document.getElementById('batch-in').click()"
        ondragover="event.preventDefault();this.classList.add('over')"
        ondragleave="this.classList.remove('over')"
        ondrop="onDrop(event)">
        <div style="font-size:22px;margin-bottom:6px">⬆</div>Drop images or click
        <input type="file" id="batch-in" accept="image/*" multiple style="display:none" onchange="onBatch(this)">
      </div>
      <div id="thumbs"></div>
      <div id="qcount" style="font-family:var(--mono);font-size:10px;color:var(--sub)">0 images queued</div>
      <button class="abtn pri" onclick="uploadDs()">UPLOAD</button>
      <button class="abtn" onclick="closeDs()">CLOSE</button>
    </div>
  </div>
</div>
<div id="lb" onclick="this.classList.remove('open')"><img id="lb-img" src="" alt=""></div>

<script>
let attached = null;
let docFiles = [];   // [{name, content}] readable text/code files dropped into chat
let droppedFiles = []; // [{name, path, size, kind, …}] ANY file uploaded through /api/drop
let _dropPending = new Set();
let batch = [];
let busy = false;
let _turnId = 0;          // bumps on every message you send — code/board cards condense within a turn
let chatAbort = null;   // AbortController for the in-flight /chat fetch

function cancelRun(){
  // Tell the server to abort generation + voice, and drop the client fetch too.
  fetch('/cancel',{method:'POST'}).catch(()=>{});
  // Off-switch: also halt any game input it's driving (releases held keys, blocks more).
  fetch('/api/game/halt',{method:'POST'}).catch(()=>{});
  if(chatAbort){ try{ chatAbort.abort(); }catch(e){} }
  const sb=document.getElementById('stop'); if(sb) sb.disabled=true;
}

// Status polling
let _lmWasReady = false;
let _notificationShown = false;
let _lastWfSig = '';
async function poll(){
  try{
    const s = await fetch('/status').then(r=>r.json());
    { const ai = document.getElementById('airplane-ind'); if(ai) ai.style.display = s.airplane ? 'inline' : 'none'; }
    document.getElementById('dlm').className = 'dot'+(s.lm?' on':'');

    // Track current model path from status
    if(s.model_path) window._currentModelPath = s.model_path;

    // Update status message when LM first comes online
    if(s.lm && !_lmWasReady){
      _lmWasReady = true;
      const el = document.getElementById('status-txt');
      if(el) el.textContent = '⬡ system 32 online';
    }
    document.getElementById('dlm').className = 'dot'+(s.lm?' on':'');

    // Show startup notification once (missing nodes etc)
    if(s.notification && !_notificationShown){      _notificationShown = true;
      addMsg('sys', s.notification);
    }

    // Security guardian banner — pending confirm takes priority over an alert
    const sb = document.getElementById('sec-banner');
    if(sb){
      const ab = document.getElementById('sec-alert-btns');
      const cb = document.getElementById('sec-confirm-btns');
      if(s.pending_action){
        window._secPending = s.pending_action;
        const p = s.pending_action;
        const warn = p.needs_admin ? ' — needs admin rights, may not go through' : '';
        document.getElementById('sec-text').textContent =
          `Confirm: ${p.action} ${p.name} (pid ${p.pid})?${warn}`;
        ab.style.display='none'; cb.style.display='inline';
        sb.style.display='flex';
      } else if(s.security_alert){
        window._secAlert = s.security_alert; window._secPending = null;
        document.getElementById('sec-text').textContent =
          (s.security_alert.reason || 'Suspicious network activity detected.');
        { const k = document.getElementById('sec-kill');
          if(k){ k.textContent = s.security_alert.egress ? 'Block it' : 'Kill it';
                 k.title = s.security_alert.egress ? 'Block this destination for this program (permanent; undo in the Iron Dome panel). Needs administrator.' : ''; } }
        ab.style.display='inline'; cb.style.display='none';
        sb.style.display='flex';
      } else {
        sb.style.display='none'; window._secAlert=null; window._secPending=null;
      }
    }

    // Image-sort bar (above the input; non-blocking — typing stays free)
    const sortBar = document.getElementById('sort-bar');
    if(sortBar){
      if(s.pending_sort){
        const p = s.pending_sort;
        window._sortToken = p.token;
        const dir = (p.dir||'').split(/[\\/]/).filter(Boolean).pop() || 'folder';
        document.getElementById('sort-text').textContent =
          `Sort ${p.total} image(s) in ${dir} — Copy or Move?`;
        sortBar.style.display='flex';
      } else {
        sortBar.style.display='none'; window._sortToken=null;
      }
    }

    // Rebuild workflow dots only when the list changes
    const wfs = s.workflows || [];
    const active = (s.active_workflow||'').replace(/\.json$/i,'').toUpperCase();
    const sig = wfs.map(w=>w.name+w.ready).join(',') + active;
    if(sig !== _lastWfSig){
      _lastWfSig = sig;
      const container = document.getElementById('wf-dots');
      container.innerHTML = '';
      wfs.forEach(w=>{
        const d = document.createElement('div');
        d.className = 'sdot';
        const isActive = active && w.name.toUpperCase() === active;
        const dotClass = isActive ? 'dot busy' : (w.ready ? 'dot on' : 'dot');
        d.innerHTML = `<div class="${dotClass}"></div>${esc(w.name.toUpperCase())}`;
        container.appendChild(d);
      });
    }

    const jd = document.getElementById('job-dot');
    if(s.job){
      jd.style.display='flex';
      document.getElementById('job-txt').textContent=s.job;
      document.getElementById('prog').classList.add('on');
    } else {
      jd.style.display='none';
      document.getElementById('prog').classList.remove('on');
    }
  } catch(e){}
}
setInterval(poll,2000); poll();

// ── Update notice (s32_update): "System 32 vX is available — Update?" ──
let _upd = null;
async function updPoll(){
  try{
    const s = await fetch('/api/update/status').then(r=>r.json());
    const b = document.getElementById('upd-banner');
    if(s.busy) return;
    if(s.available){
      _upd = s;
      document.getElementById('upd-text').textContent = `System 32 ${s.available} is available (you have ${s.current}).`;
      document.getElementById('upd-notes').textContent = s.notes || 'No notes for this release.';
      document.getElementById('upd-btns').style.display = 'inline-flex';
      document.getElementById('upd-roll').style.display = 'none';
      b.style.display = 'flex';
    } else if(s.updated_from && s.rollback && !window._updSeen){
      document.getElementById('upd-text').textContent = `Updated to ${s.current} (from ${s.updated_from}).`;
      document.getElementById('upd-btns').style.display = 'none';
      document.getElementById('upd-roll').style.display = 'inline-flex';
      b.style.display = 'flex';
    } else if(!window._updWorking){
      b.style.display = 'none';
    }
  }catch(e){}
}
function updToggleNotes(){
  const n = document.getElementById('upd-notes');
  n.style.display = (n.style.display === 'block') ? 'none' : 'block';
}
function updHide(){ window._updSeen = true; document.getElementById('upd-banner').style.display = 'none'; }
async function updLater(){
  if(!_upd) return;
  try{ await fetch('/api/update/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:_upd.available})}); }catch(e){}
  document.getElementById('upd-banner').style.display = 'none';
}
function _updWaitForRestart(msg){
  document.getElementById('upd-text').textContent = msg;
  document.getElementById('upd-btns').style.display = 'none';
  document.getElementById('upd-roll').style.display = 'none';
  setTimeout(function again(){
    fetch('/status').then(r=>{ if(r.ok) location.reload(); else setTimeout(again, 3000); })
                    .catch(()=>setTimeout(again, 3000));
  }, 8000);
}
async function updApply(){
  if(!_upd) return;
  if(!confirm(`Update System 32 to ${_upd.available}?\n\nOnly System 32's own program files are replaced — your memories, settings, outputs and models stay as they are. The current files are kept so you can roll back. System 32 restarts by itself.`)) return;
  window._updWorking = true;
  document.getElementById('upd-text').textContent = `Updating to ${_upd.available}…`;
  document.getElementById('upd-btns').style.display = 'none';
  try{
    const j = await fetch('/api/update/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:_upd.available})}).then(r=>r.json());
    if(j.ok) _updWaitForRestart(`Updated to ${j.version} — restarting…`);
    else { window._updWorking = false; document.getElementById('upd-text').textContent = j.error || 'The update did not install.'; document.getElementById('upd-btns').style.display = 'inline-flex'; }
  }catch(e){ window._updWorking = false; document.getElementById('upd-text').textContent = 'The update did not install: ' + e; }
}
async function updRollback(){
  if(!confirm('Roll back to the version you had before this update? System 32 restarts by itself.')) return;
  window._updWorking = true;
  try{
    const j = await fetch('/api/update/rollback',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json());
    if(j.ok) _updWaitForRestart(`Rolled back to ${j.version} — restarting…`);
    else { window._updWorking = false; alert(j.error || 'Roll back failed.'); }
  }catch(e){ window._updWorking = false; }
}
setInterval(updPoll, 300000); setTimeout(updPoll, 3000);

async function secApprove(){
  const a = window._secAlert; if(!a) return;
  try{ await fetch('/api/security/approve',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({name:a.name,port:a.port,id:a.id})}); }catch(e){}
  document.getElementById('sec-banner').style.display='none'; window._secAlert=null;
}
async function secDismiss(){
  const a = window._secAlert; if(!a) return;
  try{ await fetch('/api/security/dismiss',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({id:a.id,name:a.name})}); }catch(e){}
  document.getElementById('sec-banner').style.display='none'; window._secAlert=null;
}
async function secBlockEgress(a){
  try{
    const r = await fetch('/api/egress/block',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:a.id})});
    const j = await r.json();
    if(!j.ok){ alert(j.error || 'Not blocked.'); return; }
    document.getElementById('sec-banner').style.display='none'; window._secAlert=null;
    if(typeof loadDome === 'function') loadDome();
  }catch(e){ alert('Block failed: '+e.message); }
}
async function secKill(){
  if(window._secAlert && window._secAlert.egress) return secBlockEgress(window._secAlert);
  const a = window._secAlert; if(!a) return;
  try{ await fetch('/api/security/propose',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({action:'kill',name:a.name,port:a.port})}); }catch(e){}
  // On next poll the staged action surfaces and the banner switches to Confirm/Cancel.
}
async function secConfirm(){
  const p = window._secPending;
  try{ await fetch('/api/security/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({token: p?p.token:null})}); }catch(e){}
  window._secPending=null;
}
async function secCancel(){
  const p = window._secPending;
  try{ await fetch('/api/security/cancel',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({token: p?p.token:null})}); }catch(e){}
  window._secPending=null; document.getElementById('sec-banner').style.display='none';
}

async function runSort(mode){
  const bar = document.getElementById('sort-bar');
  document.getElementById('sort-copy').disabled = true;
  document.getElementById('sort-move').disabled = true;
  try{
    await fetch('/api/sort/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode: mode})});
  }catch(e){}
  if(bar) bar.style.display='none';
  window._sortToken=null;
  document.getElementById('sort-copy').disabled = false;
  document.getElementById('sort-move').disabled = false;
}
async function cancelSort(){
  const bar = document.getElementById('sort-bar');
  try{ await fetch('/api/sort/cancel',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({})}); }catch(e){}
  if(bar) bar.style.display='none';
  window._sortToken=null;
}

// Background result poller - picks up images as they complete in the background
const _jobBubbles = {};
async function pollResults(){
  try{
    const d = await fetch('/results').then(r=>r.json());
    if(!d.results||!d.results.length) return;
    d.results.forEach(r => {
      const jid = r.job_id;
      if(r.done){
        // Job complete - close this bubble
        delete _jobBubbles[jid];
        return;
      }
      // Voice transcript: don't render a bubble — feed it through the normal chat
      // send path so it replies (and speaks back) exactly as if you'd typed it.
      if(r.result && r.result.type==='stt_transcript'){
        injectTranscript(r.result.text);
        return;
      }
      // Status notes (downloads, ComfyUI restarts) fold into ONE activity card while
      // they arrive back to back; a repeated key rewrites its line in place.
      if(r.result && r.result.group){ renderActivity(r.result); return; }
      if(r.result && r.result.type==='ue_confirm_done'){ ueConfirmDone(r.result); return; }
      if(r.result && r.result.type==='code_board'){ renderBoard(r.result); return; }
      if(r.result && r.result.type==='web_results'){ _collectSources(r.result); return; }   // → sources row at turn end
      // Skip making an empty bubble for an image we've already shown. The actual
      // dedup (mark) happens in appendResult, the single render chokepoint.
      const gid = r.result && r.result.genid;
      if(gid && _shownGenIds.has(gid)) return;
      // Get or create bubble for this job. Origin decides the side:
      // user/ComfyUI-tab generations sit on the right (like your messages),
      // its own generations on the left (like its messages).
      if(!_jobBubbles[jid] || !document.body.contains(_jobBubbles[jid])){
        const side = (r.result && r.result.source === 'comfyui_user') ? 'user' : 'ai';
        _jobBubbles[jid] = addMsg(side,'', null);
      }
      const b = _jobBubbles[jid].querySelector('.bub');
      if(b && r.result){
        appendResult(b, r.result);
        // Nothing renderable in that result → don't leave an empty bubble behind.
        if(!b.children.length && !b.textContent.trim()){ _jobBubbles[jid].remove(); delete _jobBubbles[jid]; }
        scroll();
      }
    });
  } catch(e){}
}
let _shownGenIds = new Set();
setInterval(pollResults, 1000);

// ── Unreal code cards: one per attempt, code + output/traceback inline ──
function _el(tag, cls, text){ const e=document.createElement(tag); if(cls) e.className=cls; if(text!=null) e.textContent=text; return e; }
function ueCodeCard(r){
  const ok=!!r.ok, card=_el('div','ue-card '+(ok?'ok':'fail'));
  const head=_el('div','ue-head');
  const n=r.attempt||1, mx=r.max||5;
  let st = ok ? `✓ ran clean${r.seconds!=null?' ('+r.seconds+'s)':''}`
             : r.declined ? '✗ not run — you declined'
             : r.duplicate ? '✗ not run — same code as a failed attempt'
             : r.stopped ? `✗ stopped after ${mx} attempts` : '✗ failed';
  head.appendChild(_el('span','',`⬡ Unreal · attempt ${n}/${mx}`));
  head.appendChild(_el('span','st',st));
  if(r.purpose) head.appendChild(_el('span','pp',r.purpose));
  card.appendChild(head);
  if(r.code){
    const det=_el('details'); if(ok) det.open=true;
    const lines=(r.code.match(/\n/g)||[]).length+1;
    const fn=(r.file||'').split(/[\\/]/).pop();
    det.appendChild(_el('summary','',`script · ${lines} line${lines!==1?'s':''}${fn?' · '+fn:''}`));
    det.appendChild(_el('pre','ue-code',r.code));
    card.appendChild(det);
  }
  if(r.output) card.appendChild(_el('pre','ue-out',r.output));
  if(r.traceback) card.appendChild(_el('pre','ue-err',r.traceback));
  else if(!ok && r.error) card.appendChild(_el('pre','ue-err',r.error));
  if(ok && r.task){
    const b=_el('div','ue-btns');
    b.appendChild(_el('span','done','Does it do what you wanted?'));
    const yes=_el('button','yes','✓ Works'), no=_el('button','no','✗ Not right');
    const send=async (works)=>{
      let note='';
      if(!works){ note = prompt("What's not right? (optional — the assistant will see this)") || ''; }
      yes.disabled=no.disabled=true;
      try{ await fetch('/api/ue/verdict',{method:'POST',headers:{'Content-Type':'application/json'},
           body:JSON.stringify({task:r.task, works:works, note:note})}); }catch(e){}
      b.innerHTML=''; b.appendChild(_el('span','done', works ? '✓ Marked: works' : '✗ Marked: not right'+(note?' — '+note:'')));
    };
    yes.onclick=()=>send(true); no.onclick=()=>send(false);
    b.appendChild(yes); b.appendChild(no); card.appendChild(b);
  }
  return card;
}
function ueConfirmCard(r){
  const card=_el('div','ue-card ask'); card.dataset.token=r.token;
  const head=_el('div','ue-head');
  head.appendChild(_el('span','',`${r.where==='Code'?'⌨ Code':'⬡ Unreal'} · attempt ${r.attempt||1}`));
  head.appendChild(_el('span','st','⚠ confirm before running'));
  if(r.purpose) head.appendChild(_el('span','pp',r.purpose));
  card.appendChild(head);
  card.appendChild(_el('div','ue-why','This script '+(r.reasons||[]).join('; ')+'.'));
  const det=_el('details'); det.open=true;
  det.appendChild(_el('summary','','script'));
  det.appendChild(_el('pre','ue-code',r.code||''));
  card.appendChild(det);
  const b=_el('div','ue-btns');
  const yes=_el('button','yes','▶ Run it'), no=_el('button','no','✕ Don\'t run');
  const ans=async (run)=>{ yes.disabled=no.disabled=true;
    try{ await fetch('/api/ue/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
         body:JSON.stringify({token:r.token, run:run})}); }catch(e){} };
  yes.onclick=()=>ans(true); no.onclick=()=>ans(false);
  b.appendChild(yes); b.appendChild(no); card.appendChild(b);
  return card;
}
function ueSetupCard(r){
  const card=_el('div','ue-card ask');
  const head=_el('div','ue-head');
  head.appendChild(_el('span','','⬡ Unreal'));
  head.appendChild(_el('span','st','one-time setup needed'));
  head.appendChild(_el('span','pp',r.name||''));
  card.appendChild(head);
  card.appendChild(_el('div','ue-why',"To write and run code in Unreal, System 32 needs a few things switched on in this project:"));
  card.appendChild(_el('pre','ue-out',(r.missing||[]).map(x=>'• '+x).join('\n') + '\n\nYour original files are backed up (.s32bak) first.'));
  const b=_el('div','ue-btns');
  const go=_el('button','yes', '▶ Set it up & open Unreal');
  if(r.editor_open) card.appendChild(_el('div','ue-why','Unreal is open — save your work and close it first (it rewrites its settings on exit), then click.'));
  const no=_el('button','no','Not now');
  const note=_el('span','done','');
  go.onclick=async ()=>{
    go.disabled=no.disabled=true;
    let res={};
    try{ res=await fetch('/api/ue/setup',{method:'POST',headers:{'Content-Type':'application/json'},
         body:JSON.stringify({project:r.project, launch:true})}).then(x=>x.json()); }catch(e){ res={error:'Request failed.'}; }
    if(res.retry){   // Unreal still open — ask to close it, keep the button live
      note.textContent=res.error; go.textContent='▶ Unreal is closed — set it up & open it';
      go.disabled=no.disabled=false; return;
    }
    b.innerHTML='';
    if(res.ok){
      card.className='ue-card ok';
      head.querySelector('.st').textContent='✓ set up';
      b.appendChild(_el('span','done', (res.changed&&res.changed.length? res.changed.join('; ')+'. ' : '') + (res.next||'')));
    } else {
      card.className='ue-card fail';
      b.appendChild(_el('span','done','✗ '+(res.error||'Setup failed.')));
    }
  };
  no.onclick=()=>{ b.innerHTML=''; b.appendChild(_el('span','done','Skipped — Python stays off for now.')); };
  b.appendChild(go); b.appendChild(no); b.appendChild(note); card.appendChild(b);
  return card;
}
// ── Inline chat programming: a card per file version, and the project storyboard ──
async function saveCodeAs(filename, text){
  // A real Save As dialog where the browser supports it (Chrome/Edge); otherwise a
  // normal download to the Downloads folder.
  if(window.showSaveFilePicker){
    try{
      const h = await window.showSaveFilePicker({suggestedName: filename});
      const w = await h.createWritable(); await w.write(text); await w.close();
      return '✓ Saved';
    }catch(e){ if(e && e.name==='AbortError') return ''; }
  }
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([text],{type:'text/plain'}));
  a.download=filename; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(()=>URL.revokeObjectURL(a.href), 4000);
  return '✓ Downloaded';
}
function codeFileCard(r){
  const ran=!!r.ran, ok=!!r.ok;
  const card=_el('div','ue-card '+(ran ? (ok?'ok':'fail') : 'ok'));
  const head=_el('div','ue-head');
  head.appendChild(_el('span','',`⌨ ${r.filename} · v${r.version||1}`));
  let st='saved';
  if(ran){
    const n=r.attempt||1, mx=r.max||5;
    st = ok ? `✓ ran clean (run ${n}/${mx}${r.seconds!=null?', '+r.seconds+'s':''})`
       : r.declined ? '✗ not run — you declined'
       : r.duplicate ? '✗ not run — same code as a failed run'
       : r.stopped ? `✗ stopped after ${mx} runs` : `✗ failed (run ${n}/${mx})`;
  }
  head.appendChild(_el('span','st',st));
  head.appendChild(_el('span','pp',(r.project||'')+(r.purpose?' · '+r.purpose:'')));
  card.appendChild(head);
  if(r.code){
    const det=_el('details'); det.open = !ran || ok;
    const lines=(r.code.match(/\n/g)||[]).length;
    det.appendChild(_el('summary','',`code · ${lines} line${lines!==1?'s':''}`));
    det.appendChild(_el('pre','ue-code',r.code));
    card.appendChild(det);
  }
  if(r.output) card.appendChild(_el('pre','ue-out',r.output));
  if(r.traceback) card.appendChild(_el('pre','ue-err',r.traceback));
  else if(ran && !ok && r.error) card.appendChild(_el('pre','ue-err',r.error));
  const b=_el('div','ue-btns');
  const dl=_el('button','yes','⬇ Download'), cp=_el('button','','⧉ Copy'), note=_el('span','done','');
  dl.onclick=async ()=>{ const m=await saveCodeAs(r.filename, r.code||''); if(m) note.textContent=m; };
  cp.onclick=async ()=>{ try{ await navigator.clipboard.writeText(r.code||''); note.textContent='✓ Copied'; }catch(e){ note.textContent='Copy failed'; } };
  b.appendChild(dl); b.appendChild(cp);
  if(ran && ok){
    const yes=_el('button','yes','✓ Works'), no=_el('button','no','✗ Not right');
    const send=async (works)=>{
      let txt=''; if(!works){ txt = prompt("What's not right? (optional — the assistant will see this)") || ''; }
      yes.disabled=no.disabled=true;
      try{ await fetch('/api/code/verdict',{method:'POST',headers:{'Content-Type':'application/json'},
           body:JSON.stringify({slug:r.slug, filename:r.filename, works:works, note:txt})}); }catch(e){}
      yes.remove(); no.remove(); note.textContent = works ? '✓ Marked: works' : '✗ Marked: not right'+(txt?' — '+txt:'');
    };
    yes.onclick=()=>send(true); no.onclick=()=>send(false);
    b.appendChild(yes); b.appendChild(no);
  }
  b.appendChild(note); card.appendChild(b);
  return card;
}
// ONE board card per project per turn (updated in place), and ONE card per file per turn
// (each new version replaces the last, keeping a one-line history) — it re-planned and
// rewrote within a turn and the chat filled with near-identical cards (2026-10-27).
const _boards = {}, _codeCards = {};
function sweepEmptyBubbles(){
  // Its reply bubbles that ended up with nothing in them (a tool call with no words).
  document.querySelectorAll('#msgs .msg.ai').forEach(msg=>{
    if(msg.classList.contains('turn')) return;
    const bub = msg.querySelector('.bub');
    if(bub && !bub.children.length && !bub.textContent.trim()) msg.remove();
  });
  document.querySelectorAll('#msgs .msg.ai.turn').forEach(t=>{
    const body = t.querySelector('.turnbody');
    if(body && !body.children.length) t.remove();
  });
}
function _dropEmptyMsg(el){
  const msg = el && el.closest ? el.closest('.msg') : null;
  if(!msg || msg.classList.contains('turn')) return;
  const bub = msg.querySelector('.bub');
  if(bub && !bub.children.length && !bub.textContent.trim()){
    const body = msg.parentElement;
    msg.remove();
    if(body && body.classList.contains('turnbody') && !body.children.length) body.parentElement.remove();
  }
}
function renderBoard(r){
  const key = r.name || 'project', prev = _boards[key];
  const card = codeBoardCard(r);
  if(prev && prev.turn === _turnId && document.body.contains(prev.el)){
    prev.el.replaceWith(card);
  } else {
    const w = addMsg('ai','',null);
    const m = document.createElement('div'); m.className='media'; m.appendChild(card);
    w.querySelector('.bub').appendChild(m);
  }
  _boards[key] = {el: card, turn: _turnId};
  scroll();
}
function _statusLine(r){
  if(!r.ran) return `v${r.version||1} saved`;
  if(r.ok) return `v${r.version||1} ✓`;
  const last = ((r.traceback||r.error||'').trim().split('\n').pop()||'').slice(0,70);
  return `v${r.version||1} ✗ ${last}`;
}
function placeCodeCard(bubble, m, r){
  const key = (r.slug||'') + '/' + (r.filename||'');
  const prev = _codeCards[key];
  const card = codeFileCard(r);
  let hist = [];
  if(prev && prev.turn === _turnId && document.body.contains(prev.el)){
    hist = prev.hist.concat([prev.status]);
    const oldMedia = prev.el.parentElement;
    prev.el.remove();
    if(oldMedia && oldMedia.classList.contains('media') && !oldMedia.children.length) oldMedia.remove();
    if(prev.bubble !== bubble) _dropEmptyMsg(prev.bubble);   // its old reply bubble, if now empty
  }
  if(hist.length){
    const h = _el('div','ue-hist','earlier this turn: ' + hist.join('  ·  '));
    card.insertBefore(h, card.children[1] || null);
  }
  m.appendChild(card); bubble.appendChild(m);
  _codeCards[key] = {el: card, turn: _turnId, hist: hist, status: _statusLine(r), bubble: bubble};
}
function _diffPre(txt){
  const pre=document.createElement('pre');
  String(txt||'').split('\n').forEach(l=>{
    const sp=document.createElement('span');
    if(l.startsWith('@@')) sp.className='dhunk';
    else if(l.startsWith('+')&&!l.startsWith('+++')) sp.className='dadd';
    else if(l.startsWith('-')&&!l.startsWith('---')) sp.className='ddel';
    sp.textContent=l+'\n'; pre.appendChild(sp);
  });
  return pre;
}
function _cmpHead(A,B,txt){
  const h=document.createElement('div'); h.className='cmphead';
  const b1=document.createElement('b'), b2=document.createElement('b'), sp=document.createElement('span');
  b1.textContent=A; b2.textContent=B; sp.textContent=txt;
  h.append('⇄ ', b1, ' vs ', b2, ' — ', sp); return h;
}
function compareCard(r){
  // Read-only comparison (compare_code): a summary line + one fold-out per changed file.
  const box=document.createElement('div'); box.className='cmpcard';
  const A=r.label_a||'A', B=r.label_b||'B';
  if(r.diff!==undefined||r.summary!==undefined){
    box.appendChild(_cmpHead(A,B,r.identical?'identical':(r.summary||(/^settings:/.test(r.diff||'')?String(r.diff).split('\n')[0]:'different'))));
    if(r.diff){ const d=document.createElement('details'); d.open=true;
      const sm=document.createElement('summary'); sm.textContent=(r.a||'')+'  ⇄  '+(r.b||''); d.appendChild(sm);
      d.appendChild(_diffPre(r.diff)); box.appendChild(d); }
    return box;
  }
  const ch=(r.changed||[]), oa=(r.only_a||[]), ob=(r.only_b||[]);
  box.appendChild(_cmpHead(A,B,r.identical?'identical':
    `${(r.same||[]).length} same · ${ch.length} different · ${oa.length} only in ${A} · ${ob.length} only in ${B}`));
  ch.forEach(c=>{ const d=document.createElement('details');
    const sm=document.createElement('summary'); sm.textContent=c.a+(c.b&&c.b!==c.a?'  ⇄  '+c.b:'')+(c.note?'  — '+c.note:'');
    d.appendChild(sm); if(c.diff) d.appendChild(_diffPre(c.diff)); box.appendChild(d); });
  [[A,oa],[B,ob]].forEach(([lab,xs])=>{ if(!xs.length) return; const d=document.createElement('details');
    const sm=document.createElement('summary'); sm.textContent=`only in ${lab} (${xs.length})`; d.appendChild(sm);
    const pre=document.createElement('pre'); pre.textContent=xs.join('\n'); d.appendChild(pre); box.appendChild(d); });
  return box;
}
function codeBoardCard(r){
  const card=_el('div','ue-card ask');
  const head=_el('div','ue-head');
  head.appendChild(_el('span','','📋 '+(r.name||'project')));
  head.appendChild(_el('span','st', r.event||'updated'));
  card.appendChild(head);
  if(r.goal) card.appendChild(_el('div','ue-why',r.goal));
  const mark={done:'✓',doing:'▶',todo:'·'};
  const steps=(r.steps||[]).map((s,i)=>`${mark[s.status]||'·'} ${i+1}. ${s.text}`).join('\n');
  if(steps) card.appendChild(_el('pre','ue-out',steps));
  if(r.next && r.event!=='closed') card.appendChild(_el('div','ue-btns',null)).appendChild(_el('span','done','Next: '+r.next));
  return card;
}
function ueConfirmDone(r){
  const card=document.querySelector(`.ue-card.ask[data-token="${r.token}"]`);
  if(!card) return;
  const b=card.querySelector('.ue-btns'); if(b){ b.innerHTML=''; b.appendChild(_el('span','done', r.run?'▶ Running…  (result below)':'✕ Not run')); }
  const d=card.querySelector('details'); if(d) d.open=false;
}

// One card for a run of status notes. A new card starts when anything else has been
// added to the chat since (so each card tells one story); each line keeps its own
// icon so it's always clear whether it's the download or ComfyUI talking.
const _actCards = {};
const _ACT_ICON = {workflow:'⚠', download:'⬇', comfy:'⬡', unreal:'⬡'};
const _ACT_NAME = {workflow:'Workflow', download:'Download', comfy:'ComfyUI', unreal:'Unreal setup'};
function renderActivity(res){
  const msgs=document.getElementById('msgs');
  let c=_actCards[res.group];
  if(!c || !document.body.contains(c.wrap) || msgs.lastElementChild!==c.wrap){
    const wrap=addMsg('ai','',null);
    const bub=wrap.querySelector('.bub');
    const box=document.createElement('div'); box.className='fsbox act-card';
    const head=document.createElement('div'); head.className='act-head';
    const body=document.createElement('div'); body.className='act-body';
    box.appendChild(head); box.appendChild(body); bub.appendChild(box);
    c=_actCards[res.group]={wrap, head, body, lines:{}, labels:new Set()};
  }
  const lab=res.label||'comfy';
  c.labels.add(lab);
  c.head.textContent=[...c.labels].map(l=>`${_ACT_ICON[l]||'⬡'} ${_ACT_NAME[l]||l}`).join('  ·  ');
  let line = res.key ? c.lines[res.key] : null;
  if(!line){
    line=document.createElement('div'); line.className='act-line';
    c.body.appendChild(line);
    if(res.key) c.lines[res.key]=line;
  }
  line.textContent=`${_ACT_ICON[lab]||'⬡'}  ${res.content||''}`;
  scroll();
}

// Download bar: while it downloads (or ComfyUI restarts to load what it
// downloaded), the text box + send button become a loading bar so nothing new
// gets piled on its mid-download. Progress lives here instead of chat notes.
let _dlLock = false;
function _fmtB(n){
  if(!n) return '0 B';
  const u=['B','KB','MB','GB','TB']; let i=0;
  while(n>=1024 && i<u.length-1){ n/=1024; i++; }
  return (i<2 ? n.toFixed(0) : n.toFixed(1))+' '+u[i];
}
async function pollDownloads(){
  let d;
  try{ d = await fetch('/api/downloads').then(r=>r.json()); }catch(e){ return; }
  const act = d.active||[], q = d.queued||[], bt = d.batch||null;
  const on = act.length>0 || q.length>0 || !!d.restarting || !!bt;
  const xb = document.getElementById('dl-x');
  const bar=document.getElementById('dl-bar'), fill=document.getElementById('dl-fill'),
        txt=document.getElementById('dl-text'), inp=document.getElementById('msg-in'),
        sb=document.getElementById('send');
  if(!bar) return;
  if(on){
    let label, pct=null;
    if(act.length){
      const a=act[0];
      if(a.total){ pct=Math.min(100, Math.floor(a.got*100/a.total));
        label=`⬇ Downloading ${a.name} — ${pct}%  (${_fmtB(a.got)} / ${_fmtB(a.total)})`; }
      else label=`⬇ Downloading ${a.name} — ${_fmtB(a.got)}`;
      const more = (act.length-1) + q.length;
      if(more>0) label += `  · ${more} more queued`;
    } else if(q.length){
      label=`⬇ Next up: ${q[0]}` + (q.length>1 ? `  · ${q.length-1} more queued` : '');
    } else if(d.restarting){
      label='↻ Restarting ComfyUI to load new files…';
    } else {
      if(bt.total>1){
        pct=Math.floor(bt.done*100/bt.total);
        label=`⬡ Batch: ${bt.name} — ${bt.done} of ${bt.total} done`;
      } else label=`⬡ Generating ${bt.video?'video':''} — ${bt.name}`.replace('  ',' ');
    }
    // ✕ cancels what the bar shows: downloads (+ queue) first, else its batch.
    // No ✕ mid-restart — stopping ComfyUI halfway would leave it down.
    const what = (act.length||q.length) ? 'downloads' : (d.restarting ? '' : 'batch');
    if(xb){ xb.style.display = what ? '' : 'none';
            if(xb.dataset.what !== what){ xb.dataset.what = what; xb.disabled = false; } }
    txt.textContent=label; bar.title=label;
    if(pct===null){ fill.classList.add('indet'); fill.style.width=''; }
    else { fill.classList.remove('indet'); fill.style.width=pct+'%'; }
    if(!_dlLock){
      _dlLock=true;
      inp.style.display='none'; sb.style.display='none'; bar.style.display='block';
    }
  } else if(_dlLock){
    _dlLock=false;
    bar.style.display='none'; inp.style.display=''; sb.style.display='';
    fill.classList.remove('indet'); fill.style.width='0';
    if(xb){ xb.dataset.what=''; xb.disabled=false; }
  }
}
async function cancelActivity(){
  const xb=document.getElementById('dl-x'); if(!xb || !xb.dataset.what) return;
  xb.disabled=true;   // re-enabled if the bar moves on to something else
  try{ await fetch('/api/activity/cancel',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({what: xb.dataset.what})}); }catch(e){}
}
setInterval(pollDownloads, 1000); pollDownloads();

// Messages
// ── Sources row: Google-style source icons at the end of a turn (2026-10-28) ──────
const _srcs={};           // turnId → {items:[card…], read:Set(url)}
function _collectSources(result){
  const t=_srcs[_turnId]||(_srcs[_turnId]={items:[],read:new Set()});
  (result.read||[]).forEach(u=>t.read.add(u));
  (result.cards||result.results||[]).forEach(r=>{ if(r&&r.url&&!t.items.some(x=>x.url===r.url)) t.items.push(r); });
}
function _siteWords(r){
  // 'en.uesp.net' → ['uesp'], 'ludo.guide' → ['ludo'], 'thegamer.com' → ['thegamer']
  const d=(r.domain||'').toLowerCase().replace(/^www\./,'');
  const parts=d.split('.'); parts.pop();
  return parts.filter(p=>p.length>=4 || (parts.length===1 && p.length>=3));
}
function renderSources(){
  const t=_srcs[_turnId]; if(!t||!t.items.length) return;
  const body=_turnBody(false); if(!body) return;
  const said=[...body.querySelectorAll('.msg.seg .bub')].map(b=>b.textContent).join(' ').toLowerCase();
  // The sources it USED: pages it read + sites it names in its answer.
  let pick=t.items.filter(r=>t.read.has(r.url) || _siteWords(r).some(w=>said.includes(w)));
  if(!pick.length) pick=t.items.slice(0,4);
  const seen=new Set(), row=document.createElement('div'); row.className='srcrow';
  const lbl=document.createElement('span'); lbl.className='srclbl'; lbl.textContent='SOURCES'; row.appendChild(lbl);
  pick.forEach(r=>{
    const key=(r.domain||r.url); if(seen.has(key)) return; seen.add(key);
    const a=document.createElement('a'); a.className='srcchip'; a.href=r.url; a.target='_blank'; a.rel='noopener noreferrer';
    a.title=(r.title||r.url)+(r.domain?'  ·  '+r.domain:'');
    const img=document.createElement('img'); img.src=r.favicon||''; img.loading='lazy'; img.alt='';
    img.onerror=()=>{ img.remove(); a.textContent=((r.domain||'?').replace(/^www\./,'')[0]||'?').toUpperCase(); };
    a.appendChild(img); row.appendChild(a);
  });
  const old=body.querySelector(':scope > .srcrow'); if(old) old.remove();
  body.appendChild(row); scroll();
}
// ── 👍 / 👎 under a turn: the operator's word for the training data (2026-10-29) ──
const _trajIds={};        // turnId → the server's recorded-turn id
function renderRate(){
  const id=_trajIds[_turnId]; if(!id) return;
  const body=_turnBody(false); if(!body) return;
  let row=body.querySelector(':scope > .srcrow');
  if(!row){ row=document.createElement('div'); row.className='srcrow'; body.appendChild(row); }
  if(row.querySelector('.rate')) return;
  const r=document.createElement('span'); r.className='rate';
  [['good','👍','This worked — learn from it'],['bad','👎','This was wrong — don\'t learn from it']].forEach(([lab,ic,tip])=>{
    const b=document.createElement('button'); b.textContent=ic; b.title=tip; b.dataset.lab=lab;
    b.onclick=async()=>{
      const now=b.classList.contains('on') ? 'none' : lab;
      try{
        const d=await fetch('/api/training/rate',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:id,label:now})}).then(x=>x.json());
        if(d.error) return;
        r.querySelectorAll('button').forEach(x=>x.classList.toggle('on', x.dataset.lab===now));
        r.classList.toggle('set', now!=='none');
      }catch(_){}
    };
    r.appendChild(b);
  });
  row.appendChild(r);
}
let _turn=null;           // the assistant's turn block for the current _turnId
function _turnBody(create){
  if(_turn && _turn.id===_turnId && document.body.contains(_turn.msg)) return _turn.body;
  if(!create) return null;
  const wrap=document.getElementById('msgs');
  const d=document.createElement('div'); d.className='msg ai turn';
  const av=document.createElement('div'); av.className='av ai'; av.textContent=_aiTag();
  const body=document.createElement('div'); body.className='turnbody';
  d.appendChild(av); d.appendChild(body);
  const th=document.getElementById('thinking');      // keep the "Thinking…" dots last
  if(th && th.parentNode===wrap) wrap.insertBefore(d, th); else wrap.appendChild(d);
  _turn={id:_turnId, msg:d, body:body};
  return body;
}
function addMsg(role,text,result){
  const wrap=document.getElementById('msgs');
  if(role==='sys'){
    const d=document.createElement('div');
    d.className='msg sys';
    d.innerHTML=`<div class="bub sys">${esc(text)}</div>`;
    wrap.appendChild(d); scroll(); return d;
  }
  if(role!=='user'){
    // Everything the assistant produces between two of your messages lands in ONE turn
    // block (2026-10-28): each reply becomes a part ("segment") of it, alongside its
    // thoughts, tool steps and cards. A segment still looks like a message to the rest
    // of the code (.msg.ai with a .bub inside), so cards, streaming and cleanup work as
    // before.
    const body=_turnBody(true);
    const seg=document.createElement('div'); seg.className='msg ai seg';
    const sb=document.createElement('div'); sb.className='bub ai';
    sb.innerHTML=fmt(text);
    if(result) appendResult(sb,result);
    seg.appendChild(sb); body.appendChild(seg); scroll(); return seg;
  }
  const d=document.createElement('div');
  d.className=`msg ${role}`;
  const av=document.createElement('div');
  av.className=`av ${role==='user'?'user':'ai'}`;
  av.textContent=role==='user'?'YOU':_aiTag();
  const b=document.createElement('div');
  b.className=`bub ${role==='user'?'user':'ai'}`;
  b.innerHTML=fmt(text);
  if(result) appendResult(b,result);
  d.appendChild(av); d.appendChild(b);
  wrap.appendChild(d); scroll(); return d;
}

// Its reasoning (thinking toggle). Shown collapsed: a short summary on top (filled in
// AFTER the turn ends so it never slows a chain), the raw reasoning underneath.
let _pendingThoughts = [];
function addThought(text, aiEl){
  if(!text) return;
  const wrap=_turnBody(true);
  // Where this thought would go: above her streamed reply bubble if that bubble is the
  // newest thing on screen (this thinking produced it), else at the end.
  const before=(aiEl && aiEl.parentNode===wrap && wrap.lastElementChild===aiEl) ? aiEl : null;
  // Consecutive thoughts merge into ONE expandable thought (2026-10-28): look back from
  // the insertion point past step traces; if the nearest real item is a thought from
  // this same turn, append to it instead of stacking another collapsed line.
  // Within a turn block, ALL of the turn's thinking is one thought (at the top of the
  // block, steps in order) — the turn already keeps it next to what it explains.
  let prev = [...wrap.children].find(c=>c.tagName==='DETAILS' && c.classList.contains('thought') && c._entry && !c._entry.done) || null;
  if(!prev){
    prev = before ? before.previousElementSibling : wrap.lastElementChild;
    while(prev && prev.classList && prev.classList.contains('trace')) prev = prev.previousElementSibling;
  }
  if(prev && prev.tagName==='DETAILS' && prev.classList.contains('thought') && prev._entry && !prev._entry.done){
    const e=prev._entry;
    e.text += '\n\n' + text;
    e.count += 1;
    const sep=document.createElement('span'); sep.className='tsep'; sep.textContent='— step '+e.count+' —';
    e.pre.appendChild(sep); e.pre.appendChild(document.createTextNode(text));
    e.steps.textContent=' ('+e.count+' steps)';
    scroll(); return;
  }
  const d=document.createElement('details'); d.className='thought';
  const sm=document.createElement('summary');
  sm.innerHTML='💭 thinking<span class="tsteps"></span> · <span class="tsum">summary after this turn…</span>';
  const pre=document.createElement('pre'); pre.textContent=text;
  d.appendChild(sm); d.appendChild(pre);
  if(before) wrap.insertBefore(d, before); else wrap.appendChild(d);
  const entry={el:sm.querySelector('.tsum'), steps:sm.querySelector('.tsteps'), pre, text, count:1, done:false};
  d._entry=entry;
  _pendingThoughts.push(entry);
  scroll();
}
async function summarizeThoughts(){
  const todo=_pendingThoughts; _pendingThoughts=[];
  for(const t of todo) t.done=true;           // a new turn never merges into these
  if(document.body.classList.contains('hide-thoughts')){
    // Thoughts are switched off — don't spend model time summarizing what isn't shown.
    for(const t of todo) t.el.textContent='expand for the full reasoning';
    return;
  }
  for(const t of todo){                       // one at a time — the model server has one slot
    try{
      const r=await fetch('/api/thought_summary',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({text:t.text})}).then(x=>x.json());
      t.el.textContent=(r&&r.summary)?r.summary:'(no summary — expand for the full reasoning)';
    }catch(e){ t.el.textContent='(summary unavailable — expand for the full reasoning)'; }
  }
}

function addTrace(text){
  if(!text) return;
  const wrap=_turnBody(true);
  const d=document.createElement('div');
  d.className='trace';
  d.textContent='⬡ '+text;
  wrap.appendChild(d); scroll();
}

// One image tile / one video card — shared by the first batch and "More results".
function _imgCell(im){
      const cell=document.createElement('div'); cell.className='imgcell';
      const img=document.createElement('img');
      img.src=im.thumb; img.loading='lazy'; img.alt=im.title||'';
      img.onclick=()=>openLb(im.full);
      img.onerror=()=>{cell.style.display='none';};
      cell.appendChild(img);
      if(im.local){ const cp=document.createElement('button'); cp.className='imgdl'; cp.textContent='⧉';
        cp.title='Copy the path — '+im.local;
        cp.onclick=(ev)=>{ ev.stopPropagation(); _copyPath(cp, im.local); };
        cell.appendChild(cp);
        if(im.folder){ const f=document.createElement('span'); f.className='imgsrc'; f.textContent='📁 '+im.folder; f.title=im.local; cell.appendChild(f); } }
      if(im.src){ const dl=document.createElement('button'); dl.className='imgdl'; dl.textContent='⬇';
        dl.title='Save to Output\\downloads\\images';
        dl.onclick=(ev)=>{ ev.stopPropagation(); _webSave(dl, {url:im.src, kind:'image', title:im.title||''}); };
        cell.appendChild(dl); }
      if(im.source){ const a=document.createElement('a'); a.className='imgsrc';
        a.href=im.source; a.target='_blank'; a.rel='noopener noreferrer';
        try{a.textContent=new URL(im.source).hostname.replace(/^www\./,'');}catch(e){a.textContent='source';}
        cell.appendChild(a); }
  return cell;
}
function _vidCard(v){
      const card=document.createElement('div'); card.className='vidcard';
      const th=document.createElement('a'); th.className='vidthumb'; th.href=v.url; th.target='_blank'; th.rel='noopener noreferrer';
      if(v.thumb){ const img=document.createElement('img'); img.src=v.thumb; img.loading='lazy'; img.alt='';
        img.onerror=()=>{img.remove();}; th.appendChild(img); }
      if(v.duration){ const d=document.createElement('span'); d.className='viddur'; d.textContent=v.duration; th.appendChild(d); }
      card.appendChild(th);
      const body=document.createElement('div'); body.className='vidbody';
      const t=document.createElement('a'); t.className='vidtitle'; t.href=v.url; t.target='_blank'; t.rel='noopener noreferrer';
      t.textContent=v.title||v.url; t.title=v.title||''; body.appendChild(t);
      const views = (typeof v.views==='number' || /^\d+$/.test(String(v.views||'')))
        ? Number(v.views).toLocaleString('en-US')+' views' : v.views;
      const meta=[v.uploader||v.publisher, v.published, views].filter(Boolean).join(' · ');
      if(meta){ const mm=document.createElement('div'); mm.className='vidmeta'; mm.textContent=meta; mm.title=meta; body.appendChild(mm); }
      const btns=document.createElement('div'); btns.className='vidbtns';
      const op=document.createElement('a'); op.href=v.url; op.target='_blank'; op.rel='noopener noreferrer'; op.textContent='▶ Open';
      const dl=document.createElement('button');
      const note=document.createElement('div'); note.className='vidnote';
      if(v.local){ dl.textContent='⧉ Copy path'; dl.title=v.local; dl.onclick=()=>_copyPath(dl, v.local); op.textContent='▶ Play'; }
      else { dl.textContent='⬇ Download'; dl.title='Save to Output\\downloads\\videos (asks first)';
        dl.onclick=()=>_webSave(dl, {url:v.url, kind:'video', title:v.title||''}, note); }
      btns.appendChild(op); btns.appendChild(dl); body.appendChild(btns); body.appendChild(note);
  card.appendChild(body);
  return card;
}
function _copyPath(btn, p){
  const done=()=>{ const t=btn.textContent; btn.textContent='✓ Copied'; setTimeout(()=>{btn.textContent=t;},1500); };
  try{ navigator.clipboard.writeText(p).then(done, ()=>{ window.prompt('Path:', p); }); }
  catch(e){ window.prompt('Path:', p); }
}
const _KIND_ICON={folder:'📁',image:'🖼',video:'🎞',code:'📜',document:'📄',audio:'🎵',archive:'🗜',model:'🧊',program:'⚙'};
function _fileRow(r){
  const row=document.createElement('div'); row.className='frow';
  const ic=document.createElement('span'); ic.className='fico'; ic.textContent=_KIND_ICON[r.kind]||'📄';
  const main=document.createElement('div'); main.className='fmain';
  const nm=document.createElement('div'); nm.className='fname'; nm.title=r.path;
  nm.textContent=r.name+(r.count?('  —  '+r.count+' matching file'+(r.count>1?'s':'')):'');
  const sub=document.createElement('div'); sub.className='fsub';
  const sz = r.size ? (r.size>=1048576 ? (r.size/1048576).toFixed(1)+' MB' : Math.max(1,Math.round(r.size/1024))+' KB') : '';
  sub.textContent = r.count ? ((r.files||[]).join(', ')+(r.count>(r.files||[]).length?', …':'')+' · '+r.folder)
                            : [r.folder, sz, r.modified].filter(Boolean).join(' · ');
  sub.title=r.path;
  main.appendChild(nm); main.appendChild(sub);
  const cp=document.createElement('button'); cp.className='fcopy'; cp.textContent='⧉'; cp.title='Copy the path';
  cp.onclick=()=>_copyPath(cp, r.path);
  row.appendChild(ic); row.appendChild(main); row.appendChild(cp);
  return row;
}
// "More results" under an image grid / video cards (2026-10-29): the next batch of the SAME
// search — kept in memory by System 32 (no new search until that runs out).
function _moreBtn(sid, grid, kind){
  const wrap=document.createElement('div'); wrap.className='morewrap';
  const b=document.createElement('button'); b.className='morebtn'; b.textContent='More results';
  b.onclick=async()=>{
    if(b.disabled) return;
    b.disabled=true; b.textContent='Loading…';
    let j=null;
    try{ j=await fetch('/api/web/more',{method:'POST',headers:{'Content-Type':'application/json'},
           body:JSON.stringify({sid:sid})}).then(r=>r.json()); }catch(e){ j={error:String(e)}; }
    if(j && j.items && j.items.length){
      j.items.forEach(x=>grid.appendChild(kind==='video'?_vidCard(x):kind==='file'?_fileRow(x):_imgCell(x)));
    }
    if(j && j.error && !(j.items&&j.items.length)){
      b.textContent=/expired/.test(j.error)?'Results expired — search again':"Couldn't load more — try again";
      b.disabled=/expired/.test(j.error); return;
    }
    if(j && j.more){ b.disabled=false; b.textContent='More results'; }
    else { b.textContent='No more results'; b.classList.add('done'); }
  };
  wrap.appendChild(b); return wrap;
}
// ⬇ on a web image / video card (2026-10-29). Images save at once; a video asks first
// with the usual Download card in chat.
async function _webSave(btn, payload, note){
  if(btn.disabled) return;
  const label=btn.textContent; btn.disabled=true; btn.classList.add('busy');
  btn.textContent = payload.kind==='video' ? '⏳ Checking…' : '…';
  let j=null;
  try{ j=await fetch('/api/web/save',{method:'POST',headers:{'Content-Type':'application/json'},
         body:JSON.stringify(payload)}).then(r=>r.json()); }catch(e){ j={error:String(e)}; }
  btn.classList.remove('busy');
  const say=(t)=>{ if(note) note.textContent=t; else btn.title=t; };
  if(j && (j.downloaded || j.path)){
    btn.classList.add('done'); btn.textContent = payload.kind==='video' ? '✓ Saved' : '✓';
    say('Saved → '+(j.path||j.dest||''));
  } else if(j && j.proposed){
    btn.textContent = 'Confirm in chat ↓';
    say('Size and destination are on the Download card in chat — press Download there.');
    setTimeout(()=>{ btn.disabled=false; btn.textContent=label; }, 8000);
  } else {
    btn.classList.add('err'); btn.textContent = payload.kind==='video' ? '✗ Failed' : '✗';
    say((j && (j.error||j.reason)) || 'Download failed');
    setTimeout(()=>{ btn.disabled=false; btn.classList.remove('err'); btn.textContent=label; }, 6000);
  }
}
function appendResult(bubble,result){
  if(!result) return;
  // Global per-image dedup at the single render chokepoint, so the SAME underlying
  // generation never renders twice no matter which path surfaced it (poll buffer,
  // chat stream, or multi-output recursion). Distinct generations have distinct
  // ids, so genuine multi-image batches are unaffected.
  if(result.genid){
    if(_shownGenIds.has(result.genid)) return;
    _shownGenIds.add(result.genid);
    if(_shownGenIds.size>200){ _shownGenIds = new Set(Array.from(_shownGenIds).slice(-100)); }
  }
  const m=document.createElement('div'); m.className='media';
  // Multi-output (workflow with several images/videos)
  if(result.type==='multi' && result.outputs){
    result.outputs.forEach(r=>appendResult(bubble,r));
    return;
  }
  // A status note arriving inside its reply (not via the poller): same card look.
  if(result.group && result.content && !result.type){
    const box=document.createElement('div'); box.className='fsbox act-card';
    const head=document.createElement('div'); head.className='act-head';
    const lab=result.label||'comfy';
    head.textContent=`${_ACT_ICON[lab]||'⬡'} ${_ACT_NAME[lab]||lab}`;
    const line=document.createElement('div'); line.className='act-line'; line.textContent=result.content;
    box.appendChild(head); box.appendChild(line); m.appendChild(box); bubble.appendChild(m); return;
  }
  if(result.type==='ue_code'){ m.appendChild(ueCodeCard(result)); bubble.appendChild(m); return; }
  if(result.type==='ue_confirm'){ m.appendChild(ueConfirmCard(result)); bubble.appendChild(m); return; }
  if(result.type==='ue_setup'){ m.appendChild(ueSetupCard(result)); bubble.appendChild(m); return; }
  if(result.type==='code_file'){ placeCodeCard(bubble, m, result); return; }
  if(result.type==='code_board'){ m.appendChild(codeBoardCard(result)); bubble.appendChild(m); return; }
  if(result.type==='code_compare'){ m.appendChild(compareCard(result)); bubble.appendChild(m); return; }
  if(result.type==='queued'){
    const p=document.createElement('div');
    p.style.cssText='font-family:var(--mono);font-size:11px;color:var(--sub);padding:4px 0';
    p.textContent=`⬡ queued ${result.count} generation${result.count!==1?'s':''}${result.prompt?' - '+result.prompt:''}`;
    m.appendChild(p);
  } else if(result.type==='model_3d'){
    const p=document.createElement('div');
    p.style.cssText='font-family:var(--mono);font-size:11px;color:var(--accent);padding:4px 0';
    p.textContent='⬡ 3D model saved: '+(result.saved||result.filename||'');
    m.appendChild(p);
    if(result.saved){
      const mv = document.createElement('model-viewer');
      mv.setAttribute('src', `/img?path=${encodeURIComponent(result.saved)}`);
      mv.setAttribute('camera-controls','');
      mv.setAttribute('auto-rotate','');
      mv.setAttribute('shadow-intensity','1');
      mv.style.cssText='width:100%;height:380px;border-radius:6px;background:#111;margin-top:8px;display:block';
      m.appendChild(mv);
    }
  } else if(result.type==='web_results'){
    // No result list in chat (2026-10-28): results are collected and shown as a compact
    // row of source icons at the end of the turn — see renderSources().
    _collectSources(result);
  } else if(result.type==='image_grid'){
    if(result.query){ const h=document.createElement('div'); h.className='imgridq';
      h.textContent=(/^💻/.test(result.query)?'':'🔍 ')+result.query; m.appendChild(h); }
    const grid=document.createElement('div'); grid.className='imgrid';
    (result.images||[]).forEach(im=>grid.appendChild(_imgCell(im)));
    m.appendChild(grid);
    if(result.sid && result.more) m.appendChild(_moreBtn(result.sid, grid, 'image'));
  } else if(result.type==='file_list'){
    const h=document.createElement('div'); h.className='imgridq';
    h.textContent='💻 '+(result.query||'')+(result.total?(' · '+result.total+' found'):''); m.appendChild(h);
    const list=document.createElement('div'); list.className='flist';
    (result.rows||[]).forEach(r=>list.appendChild(_fileRow(r)));
    m.appendChild(list);
    if(result.sid && result.more) m.appendChild(_moreBtn(result.sid, list, 'file'));
  } else if(result.type==='video_grid'){
    if(result.query){ const h=document.createElement('div'); h.className='imgridq';
      h.textContent=(/^💻/.test(result.query)?'':'🎬 ')+result.query; m.appendChild(h); }
    const grid=document.createElement('div'); grid.className='vidgrid';
    (result.videos||[]).forEach(v=>grid.appendChild(_vidCard(v)));
    m.appendChild(grid);
    if(result.sid && result.more) m.appendChild(_moreBtn(result.sid, grid, 'video'));
  } else if(result.type==='fs_confirm'){
    const box=document.createElement('div'); box.className='fsconfirm';
    const head=document.createElement('div'); head.className='fshead';
    const badge=document.createElement('span');
    badge.className='fsbadge '+(result.mode==='edit'?'fsedit':'fscreate');
    badge.textContent=(result.mode==='edit'?'EDIT':'CREATE');
    const pth=document.createElement('span'); pth.className='fspath'; pth.textContent=result.path||'';
    head.appendChild(badge); head.appendChild(pth); box.appendChild(head);
    const meta=document.createElement('div'); meta.className='fsmeta';
    meta.textContent=(result.lines||0)+' lines · '+(result.size||0)+' chars'
      +(result.description?(' · '+result.description):'');
    box.appendChild(meta);
    if(result.preview){
      const pv=document.createElement('pre'); pv.className='fspreview';
      pv.textContent=result.preview+(result.preview_truncated?'\n…':'');
      box.appendChild(pv);
    }
    const tok=result.token||'';
    const btns=document.createElement('div'); btns.className='fsbtns';
    const yes=document.createElement('button'); yes.className='fsyes'; yes.textContent='Confirm write';
    const no=document.createElement('button'); no.className='fsno'; no.textContent='Cancel';
    const finish=(label)=>{ yes.disabled=true; no.disabled=true;
      const s=document.createElement('div'); s.className='fsstatus'; s.textContent=label; box.appendChild(s); };
    yes.onclick=async()=>{ yes.disabled=true; no.disabled=true;
      try{ const r=await fetch('/api/fs/confirm',{method:'POST',
             headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});
        const j=await r.json();
        finish(j&&j.ok?('✓ Written: '+(j.path||'')):('⚠ '+((j&&(j.reason||j.error))||'not written')));
      }catch(e){ finish('⚠ '+e); } };
    no.onclick=async()=>{ yes.disabled=true; no.disabled=true;
      try{ await fetch('/api/fs/cancel',{method:'POST',
             headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})}); }catch(e){}
      finish('✗ Cancelled'); };
    btns.appendChild(yes); btns.appendChild(no); box.appendChild(btns);
    m.appendChild(box);
  } else if(result.type==='fileop_confirm'){
    // Native copy / move / rename / mkdir / delete — nothing happens until Confirm.
    const box=document.createElement('div'); box.className='fsconfirm';
    const head=document.createElement('div'); head.className='fshead';
    const badge=document.createElement('span');
    badge.className='fsbadge '+(result.action==='delete'?'fsdel':(result.action==='move'?'fsedit':'fscreate'));
    badge.textContent=(result.verb||result.action||'').toUpperCase();
    const pth=document.createElement('span'); pth.className='fspath';
    pth.textContent=result.count+' item(s)'+(result.size?(' · '+result.size):'')+(result.dest?('  →  '+result.dest):(result.source?('  in  '+result.source):''));
    head.appendChild(badge); head.appendChild(pth); box.appendChild(head);
    const pv=document.createElement('pre'); pv.className='fspreview';
    pv.textContent=(result.items||[]).join('\n')+(result.more?('\n… and '+result.more+' more'):'');
    box.appendChild(pv);
    (result.warnings||[]).forEach(w=>{ const m2=document.createElement('div'); m2.className='fsmeta'; m2.textContent='• '+w; box.appendChild(m2); });
    const tok=result.token||'';
    const btns=document.createElement('div'); btns.className='fsbtns';
    const yes=document.createElement('button'); yes.className='fsyes'; yes.textContent='Confirm '+(result.verb||'').toLowerCase();
    const no=document.createElement('button'); no.className='fsno'; no.textContent='Cancel';
    const finish=(label)=>{ yes.disabled=true; no.disabled=true;
      const st=document.createElement('div'); st.className='fsstatus'; st.textContent=label; box.appendChild(st); };
    yes.onclick=async()=>{ yes.disabled=true; no.disabled=true; yes.textContent='Working…';
      try{ const r=await fetch('/api/fileop/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});
        const j=await r.json(); finish(j&&j.message?((j.failed&&j.failed.length?'⚠ ':'✓ ')+j.message):('⚠ '+((j&&j.error)||'not done')));
      }catch(e){ finish('⚠ '+e); } };
    no.onclick=async()=>{ yes.disabled=true; no.disabled=true;
      try{ await fetch('/api/fileop/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})}); }catch(e){}
      finish('✗ Cancelled — nothing was changed'); };
    btns.appendChild(yes); btns.appendChild(no); box.appendChild(btns);
    m.appendChild(box);
  } else if(result.type==='dl_confirm'){
    const box=document.createElement('div'); box.className='fsconfirm';
    const head=document.createElement('div'); head.className='fshead';
    const badge=document.createElement('span');
    badge.className='fsbadge '+(result.exe?'fsedit':'fscreate');
    badge.textContent='⬇ '+(result.kind||'file').toUpperCase();
    const pth=document.createElement('span'); pth.className='fspath'; pth.textContent=result.filename||'';
    head.appendChild(badge); head.appendChild(pth); box.appendChild(head);
    const meta=document.createElement('div'); meta.className='fsmeta';
    meta.textContent=(result.size_h||'unknown size')+' · '+(result.host||'')
      +(result.trusted?' · known source':' · ⚠ unverified source');
    box.appendChild(meta);
    const dst=document.createElement('div'); dst.className='fsmeta'; dst.style.opacity='.8';
    dst.textContent='→ '+(result.dest||''); box.appendChild(dst);
    if(result.exe){
      const w=document.createElement('div'); w.className='fsmeta'; w.style.color='var(--accent2)';
      w.textContent='⚠ Executable — it will be saved but never run. Review before opening.';
      box.appendChild(w);
    }
    const tok=result.token||'';
    const btns2=document.createElement('div'); btns2.className='fsbtns';
    const yes2=document.createElement('button'); yes2.className='fsyes'; yes2.textContent='Download';
    const no2=document.createElement('button'); no2.className='fsno'; no2.textContent='Cancel';
    const fin=(label)=>{ yes2.disabled=true; no2.disabled=true;
      const s=document.createElement('div'); s.className='fsstatus'; s.textContent=label; box.appendChild(s); };
    yes2.onclick=async()=>{ yes2.disabled=true; no2.disabled=true;
      try{ const r=await fetch('/api/dl/confirm',{method:'POST',
             headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})});
        const j=await r.json();
        fin(j&&j.started?('⬇ Downloading '+(j.filename||'')+'…'):('⚠ '+((j&&(j.reason||j.error))||'not started')));
      }catch(e){ fin('⚠ '+e); } };
    no2.onclick=async()=>{ yes2.disabled=true; no2.disabled=true;
      try{ await fetch('/api/dl/cancel',{method:'POST',
             headers:{'Content-Type':'application/json'},body:JSON.stringify({token:tok})}); }catch(e){}
      fin('✗ Cancelled'); };
    btns2.appendChild(yes2); btns2.appendChild(no2); box.appendChild(btns2);
    m.appendChild(box);
  } else if(result.type==='image'){
    const img=document.createElement('img');
    img.src = result.url || `data:image/png;base64,${result.data}`;
    img.onclick=()=>openLb(img.src); m.appendChild(img);
  } else if(result.type==='image_error'){
    const p=document.createElement('div');
    p.style.cssText='font-family:var(--mono);font-size:10px;color:var(--accent2);padding:4px 0';
    p.textContent='⚠ '+result.error+': '+(result.path||'');
    m.appendChild(p);
  } else if(result.type==='video'){
    const v=document.createElement('video');
    v.src = result.url || `data:video/${result.ext||'mp4'};base64,${result.data}`;
    v.controls=true; m.appendChild(v);
  } else if(result.type==='lora_done'&&result.data){
    const img=document.createElement('img');
    img.src=`data:image/png;base64,${result.data}`;
    img.onclick=()=>openLb(img.src); m.appendChild(img);
  }
  // Filesystem / text results
  if(!result.type && (result.drives||result.items!==undefined||result.results!==undefined||result.content!==undefined||result.info||result.workflows!==undefined||result.written!==undefined||result.ran!==undefined||result.need_images||result.error)){
    const box=document.createElement('div'); box.className='fsbox';
    box.textContent=fmtFs(result); m.appendChild(box);
  }
  if(result.saved){
    const p=document.createElement('div'); p.className='saved';
    p.textContent='⬡ saved: '+result.saved; m.appendChild(p);
  }
  if(m.children.length) bubble.appendChild(m);
}

function fmtFs(r){
  if(r.error) return 'Error: '+r.error;
  if(r.drives) return r.drives.map(d=>d.error?`${d.drive}  [error]`:`${d.drive}  ${d.used_gb}/${d.total_gb} GB  (${d.free_gb} GB free)`).join('\n');
  if(r.items!==undefined){
    let t=r.path+'\n'+'─'.repeat(38)+'\n';
    t+=r.items.map(i=>i.type==='dir'?`  [DIR]  ${i.name}`:`  ${String(i.size_mb+'MB').padStart(10)}  ${i.name}`).join('\n');
    if(r.truncated) t+='\n  ...';
    return t;
  }
  if(r.results!==undefined){
    if(!r.results.length) return 'No files found.';
    let t=r.results.map(x=>`${x.path}  (${x.size_mb}MB)`).join('\n');
    if(r.truncated) t+='\n...more results exist';
    return t;
  }
  if(r.content!==undefined){
    let t=r.path+'\n'+'─'.repeat(38)+'\n'+r.content;
    if(r.truncated) t+='\n\n...(truncated)';
    return t;
  }
  if(r.info){
    return Object.entries(r.info).map(([k,v])=>{
      let s=`${k.toUpperCase()}: ${v.path}`;
      if(v.sample) s+='\n  '+v.sample.slice(0,8).join(', ')+(v.count>8?` +${v.count-8} more`:'');
      if(v.error)  s+='\n  ['+v.error+']';
      return s;
    }).join('\n\n');
  }
  if(r.need_images) return `Ready to receive images for dataset: ${r.dataset}`;
  if(r.workflows!==undefined){
    const dir=r.workflow_dir||'JsonFiles';
    let t=dir+'\n'+'─'.repeat(38)+'\n';
    t+=Object.entries(r.workflows).map(([f,v])=>{
      const cap = typeof v === 'object' ? (v.capability||'') : '';
      const patch = typeof v === 'object' ? (v.patchable||'') : v;
      return `\n▸ ${f}${cap?' ['+cap+']':''}\n${patch}`;
    }).join('\n');
    return t;
  }
  if(r.written!==undefined) return `✓ Written: ${r.written}  (${r.bytes} bytes)`;
  if(r.ran!==undefined){
    let t=`▶ ${r.ran}  →  exit ${r.returncode}\n`;
    if(r.stdout) t+='─'.repeat(38)+'\n'+r.stdout;
    if(r.stderr) t+='\n[stderr]\n'+r.stderr;
    return t.trim();
  }
  return JSON.stringify(r,null,2);
}

function fmt(t){
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/\*\*(.*?)\*\*/g,'<strong>$1</strong>')
    .replace(/`(.*?)`/g,'<code style="font-family:var(--mono);background:var(--panel);padding:1px 5px;border-radius:3px">$1</code>')
    .replace(/\n/g,'<br>');
}
function esc(t){return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}
function scroll(){const w=document.getElementById('msgs');w.scrollTop=w.scrollHeight}

function addThinking(){
  const wrap=document.getElementById('msgs');
  const d=document.createElement('div'); d.className='msg'; d.id='thinking';
  d.innerHTML=`<div class="av ai">${_aiTag()}</div><div class="bub ai thinking">Thinking<div class="tdots"><span></span><span></span><span></span></div></div>`;
  wrap.appendChild(d); scroll(); return d;
}

// Send
async function send(){
  if(busy || _dlLock) return;   // input is a loading bar while downloads/restart run
  _turnId++;
  const inp=document.getElementById('msg-in');
  const txt=inp.value.trim();
  if(_dropPending.size){                       // still uploading — wait, with the button showing it
    const sbtn=document.getElementById('send'); sbtn.disabled=true;
    await Promise.all(Array.from(_dropPending)); sbtn.disabled=false;
  }
  if(!txt && !attached && docFiles.length===0 && droppedFiles.length===0) return;
  busy=true;
  document.getElementById('send').disabled=true;
  chatAbort=new AbortController();
  { const sb=document.getElementById('stop'); if(sb){ sb.style.display='block'; sb.disabled=false; } }
  inp.value=''; resize(inp);
  if(txt) addMsg('user',txt);            // files only → no empty text bubble
  // Everything being sent leaves the tray NOW (it used to sit there until her reply ended).
  const _sentAtt = attached, _sentDocs = docFiles.slice(), _sentDrops = droppedFiles.slice();
  attached = null; docFiles = [];
  droppedFiles = droppedFiles.filter(d => !_sentDrops.includes(d));
  document.querySelectorAll('#drop-tray .dchip').forEach(c => {
    if((c._drop && _sentDrops.includes(c._drop)) || (_sentAtt && _sentAtt._chip === c) || _sentDocs.some(d => d._chip === c)) c.remove();
  });
  ['prev-img','prev-vid','prev-icon'].forEach(id => { const e = document.getElementById(id); if(e){ e.style.display = 'none'; if(e.src !== undefined) e.removeAttribute('src'); } });
  { const pn = document.getElementById('prev-name'); if(pn) pn.textContent = ''; }
  _trayVisible();
  // ONE bubble for the files: every picture its own thumbnail, everything else a chip.
  if(_sentAtt || _sentDocs.length || _sentDrops.length){
    const wrap=document.getElementById('msgs');
    const d=document.createElement('div'); d.className='msg user';
    const av=document.createElement('div'); av.className='av user'; av.textContent='FILE';
    const bub=document.createElement('div'); bub.className='bub user filebub';
    const pic = (src, title) => { const i=document.createElement('img'); i.src=src; i.title=title; i.className='sentpic'; i.onclick=()=>openLb(src); bub.appendChild(i); };
    const chip = (icon, name, size, title) => { const c=document.createElement('span'); c.className='dchip';
      c.textContent = icon + ' ' + name + (size ? ' · ' + _fmtSize(size) : ''); c.title = title || name; bub.appendChild(c); };
    if(_sentAtt){
      if(_sentAtt.kind === 'image' && (_sentAtt._thumb || _sentAtt.b64)) pic(_sentAtt._thumb || `data:${_sentAtt.mime};base64,${_sentAtt.b64}`, _sentAtt.name);
      else chip(_sentAtt.kind === 'video' ? '🎞' : _sentAtt.kind === '3d' ? '⬡' : '📄', _sentAtt.name, _sentAtt.size);
    }
    _sentDrops.forEach(f => { if(f._thumb) pic(f._thumb, f.name + ' · ' + _fmtSize(f.size)); else chip(_dropIcon(f.kind), f.name, f.size, f.path); });
    _sentDocs.forEach(f => chip('📄', f.name, f.size));
    d.append(av, bub); wrap.appendChild(d); scroll();
  }
  const th=addThinking();
  document.getElementById('prog').classList.add('on');
  try{
    const body={message:txt};
    if(_sentDrops.length) body.dropped = _sentDrops.map(({_thumb, ...rest}) => rest);
    if(_sentAtt){
      if(_sentAtt.b64) body.image = _sentAtt.b64;
      body.mime     = _sentAtt.mime;
      body.filename = _sentAtt.name;
      if(_sentAtt.path) body.path = _sentAtt.path;
    }
    if(_sentDocs.length){
      body.docs = _sentDocs.map(d=>({name:d.name, content:d.content}));
    }
    const resp=await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),signal:chatAbort.signal});
    const te=resp.headers.get('Transfer-Encoding');
    let aiEl=null;
    if(te==='chunked'){
      const reader=resp.body.getReader(); const dec=new TextDecoder(); let buf='';
      while(true){
        const {done,value}=await reader.read(); if(done) break;
        buf+=dec.decode(value,{stream:true});
        // Parse HTTP chunked encoding: each chunk is hex-length\r\ndata\r\n
        // Keep consuming complete chunks from buf
        while(true){
          const crlf=buf.indexOf('\r\n');
          if(crlf===-1) break;
          const hexStr=buf.slice(0,crlf).trim();
          if(!hexStr) { buf=buf.slice(crlf+2); continue; }
          const chunkLen=parseInt(hexStr,16);
          if(isNaN(chunkLen)) { buf=buf.slice(crlf+2); continue; }
          if(chunkLen===0) { buf=''; break; } // terminal chunk
          const dataStart=crlf+2;
          if(buf.length < dataStart+chunkLen+2) break; // wait for more data
          const data=buf.slice(dataStart, dataStart+chunkLen);
          buf=buf.slice(dataStart+chunkLen+2);
          if(!data.trim()) continue;
          try{
            const c=JSON.parse(data);
            if(c.phase==='turn'){
              if(c.id) _trajIds[_turnId]=c.id;
            } else if(c.phase==='start'){
              try{ th.remove(); }catch(_){}
              document.getElementById('prog').classList.remove('on');
              aiEl=addMsg('ai','',null);
              aiEl._buf='';
            } else if(c.phase==='delta'){
              if(!aiEl){ try{ th.remove(); }catch(_){}; aiEl=addMsg('ai','',null); aiEl._buf=''; }
              aiEl._buf=(aiEl._buf||'')+(c.text||'');
              const b=aiEl.querySelector('.bub');
              if(b) b.innerHTML=fmt(aiEl._buf);
              scroll();
            } else if(c.phase==='text'){
              th.remove();
              document.getElementById('prog').classList.remove('on');
              // No words with this tool call → no empty bubble (results make one on demand).
              aiEl = (c.response||'').trim() ? addMsg('ai',c.response,null) : null;
              if(c.tool==='add_to_dataset') openDs('');
            } else if(c.phase==='result'){
              document.getElementById('prog').classList.remove('on');
              // A first-tool result can arrive before any reply text (thinking on, no words
              // yet) — make its bubble instead of dropping the result. (A write_code card
              // vanished this way, 2026-10-27.)
              if(c.result && !aiEl){ try{ th.remove(); }catch(_){} aiEl=addMsg('ai','',null); aiEl._buf=''; }
              if(c.result&&aiEl){
                // (The image browser used to pop open for ANY result with a 'folder' key —
                // e.g. running code. It opens only from its 🗂 button now. 2026-10-31)
                const b=aiEl.querySelector('.bub');
                if(b) appendResult(b,c.result);
              }
            } else if(c.phase==='followup'){
              if(c.response) addMsg('ai',c.response,null);
            } else if(c.phase==='trace'){
              addTrace(c.text||'');
            } else if(c.phase==='thought'){
              addThought(c.text||'', aiEl);
            }
          } catch(e){}
        }
      }
      try{ th.remove(); }catch(_){}
      document.getElementById('prog').classList.remove('on');
      sweepEmptyBubbles();
    } else {
      const data=await resp.json();
      th.remove();
      document.getElementById('prog').classList.remove('on');
      if(data.response) addMsg('ai',data.response,data.result);
      if(data.tool==='add_to_dataset') openDs('');
    }
  } catch(e){
    try{ th.remove(); }catch(_){}
    document.getElementById('prog').classList.remove('on');
    if(e.name==='AbortError'){
      addMsg('ai','⏹ Stopped.',null);
    } else {
      addMsg('ai','Connection error: '+e.message,null);
    }
  }
  busy=false;                      // the tray was cleared at send — files added since stay for the next message
  chatAbort=null;
  try{ renderSources(); }catch(_){}
  try{ renderRate(); }catch(_){}
  summarizeThoughts();
  { const sb=document.getElementById('stop'); if(sb){ sb.style.display='none'; sb.disabled=false; } }
  document.getElementById('send').disabled=false;
  document.getElementById('msg-in').focus();
}

function onKey(e){if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}}
function resize(el){el.style.height='auto';el.style.height=Math.min(el.scrollHeight,120)+'px'}

// Universal file attach
const _IMG_TYPES = new Set(['image/png','image/jpeg','image/webp','image/gif','image/bmp']);
const _VID_TYPES = new Set(['video/mp4','video/webm','video/quicktime','video/avi','video/x-matroska']);
const _3D_EXTS   = new Set(['.glb','.gltf','.obj','.ply','.stl','.fbx','.usdz']);
const _DOC_EXTS  = new Set(['.txt','.md','.py','.js','.ts','.jsx','.tsx','.vue','.json','.yaml','.yml',
  '.ini','.cfg','.conf','.log','.csv','.tsv','.html','.htm','.css','.xml','.toml','.bat','.sh','.ps1',
  '.java','.c','.cpp','.h','.hpp','.cs','.rs','.go','.rb','.php','.sql','.tex','.r','.lua','.env','.gitignore']);
const _DOC_MAX   = 200000; // client-side per-file char cap

function _fileKind(file){
  if(_IMG_TYPES.has(file.type)) return 'image';
  if(_VID_TYPES.has(file.type) || file.type.startsWith('video/')) return 'video';
  const ext = file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  if(_3D_EXTS.has(ext)) return '3d';
  if(_DOC_EXTS.has(ext) || (file.type && file.type.startsWith('text/'))) return 'doc';
  return 'file';
}

function _renderDocSummary(){
  const ds = document.getElementById('doc-summary');
  if(!ds) return;
  if(docFiles.length){
    ds.textContent = '📄 ' + docFiles.length + ' file(s): ' + docFiles.map(d=>d.name).join(', ');
    ds.style.display = 'inline';
    document.getElementById('img-preview').style.display = 'flex';
  } else {
    ds.textContent = ''; ds.style.display = 'none';
  }
}

const _IMG_INLINE_MAX = 25 * 1024 * 1024;   // a picture she SEES (vision) goes inline; bigger → upload
function _fmtSize(n){ return n >= 1048576 ? (n/1048576).toFixed(1)+' MB' : Math.max(1, Math.round(n/1024))+' KB'; }
function _dropIcon(k){ return {image:'🖼', video:'🎞', audio:'🔊', '3d':'⬡', archive:'🗜', model:'🧠', text:'📄', doc:'📄'}[k] || '📦'; }

// ANY file → streamed to System 32 (/api/drop), shown as a chip with progress. Nothing is
// read into the page, so big videos / meshes / archives don't freeze the browser.
// One chip per file in the tray above the input: a thumbnail for pictures, an icon otherwise.
function _isPic(file){ return /^image\/(png|jpe?g|webp|gif|bmp|avif)$/i.test(file.type||'') || /\.(png|jpe?g|webp|gif|bmp|avif)$/i.test(file.name||''); }
function _chipThumb(src, fallbackIcon){
  const t = document.createElement('img'); t.className = 'dthumb'; t.src = src; t.alt = '';
  t.onerror = () => { const s = document.createElement('span'); s.textContent = fallbackIcon; t.replaceWith(s); };
  return t;
}
function _trayVisible(){
  const t = document.getElementById('drop-tray');
  const ip = document.getElementById('img-preview');
  const pi = document.getElementById('prev-img'), pv = document.getElementById('prev-vid'), pc = document.getElementById('prev-icon');
  const legacy = [pi, pv, pc].some(e => e && e.style.display && e.style.display !== 'none');
  ip.style.display = ((t && t.children.length) || legacy) ? 'flex' : 'none';
}
function _simpleChip(icon, name, sizeTxt, onRemove, thumbSrc){
  const tray = document.getElementById('drop-tray');
  const chip = document.createElement('span'); chip.className = 'dchip';
  const ic = thumbSrc ? _chipThumb(thumbSrc, icon) : Object.assign(document.createElement('span'), {textContent: icon});
  const nm = document.createElement('span'); nm.className = 'dn'; nm.textContent = name; nm.title = name;
  const pr = document.createElement('span'); pr.className = 'dp'; pr.textContent = sizeTxt || '';
  const x = document.createElement('button'); x.textContent = '✕'; x.title = 'Remove';
  x.onclick = () => { onRemove && onRemove(); chip.remove(); _trayVisible(); };
  chip.append(ic, nm, pr, x); tray.appendChild(chip); _trayVisible();
  return chip;
}

function _uploadDrop(file){
  const tray = document.getElementById('drop-tray');
  const chip = document.createElement('span'); chip.className = 'dchip';
  const _thumb = _isPic(file) ? URL.createObjectURL(file) : null;
  const ic = _thumb ? _chipThumb(_thumb, '🖼') : document.createElement('span');
  if(!_thumb) ic.textContent = _dropIcon(_fileKind(file));
  const nm = document.createElement('span'); nm.className = 'dn'; nm.textContent = file.name; nm.title = file.name;
  const pr = document.createElement('span'); pr.className = 'dp'; pr.textContent = '0%';
  const x = document.createElement('button'); x.textContent = '✕'; x.title = 'Remove';
  chip.append(ic, nm, pr, x); tray.appendChild(chip);
  document.getElementById('img-preview').style.display = 'flex';
  const xhr = new XMLHttpRequest();
  const job = new Promise(res => {
    xhr.upload.onprogress = e => { if(e.lengthComputable) pr.textContent = Math.floor(e.loaded*100/e.total) + '%'; };
    xhr.onload = () => {
      let d = {}; try{ d = JSON.parse(xhr.responseText); }catch(_){}
      if(xhr.status === 200 && d.path){
        if(_thumb) d._thumb = _thumb;
        droppedFiles.push(d); chip._drop = d;
        if(!_thumb) ic.textContent = _dropIcon(d.kind);
        pr.textContent = _fmtSize(d.size) + (d.unpacked ? ' · unpacked' : '');
      } else { chip.classList.add('err'); pr.textContent = d.error || ('failed (' + xhr.status + ')'); }
      res();
    };
    xhr.onerror = xhr.onabort = () => { chip.classList.add('err'); pr.textContent = 'not uploaded'; res(); };
  });
  _dropPending.add(job); job.then(() => _dropPending.delete(job));
  x.onclick = () => {
    if(_dropPending.has(job)) xhr.abort();
    if(chip._drop) droppedFiles = droppedFiles.filter(d => d !== chip._drop);
    chip.remove(); _trayVisible();
  };
  xhr.open('POST', '/api/drop?name=' + encodeURIComponent(file.name));
  xhr.setRequestHeader('X-S32-Drop', '1');
  xhr.setRequestHeader('Content-Type', 'application/octet-stream');
  xhr.send(file);
}

function onFile(input){
  _takeFiles(Array.from(input.files || []));
  input.value='';
}

function _takeFiles(files){
  if(!files.length) return;
  let mediaTaken = !!attached;   // ONE picture goes inline (she can see it); everything else uploads
  files.forEach(file => {
    const kind = _fileKind(file);
    if(kind !== 'doc' && !(kind === 'image' && !mediaTaken && file.size <= _IMG_INLINE_MAX)){
      _uploadDrop(file);
      return;
    }
    if(kind === 'doc'){
      const r = new FileReader();
      const doc = {name: file.name, content: '', size: file.size};
      doc._chip = _simpleChip('📄', file.name, _fmtSize(file.size), () => { docFiles = docFiles.filter(d => d !== doc); doc._gone = true; });
      const job = new Promise(res => {
        r.onload = e => {
          let content = e.target.result || '';
          if(content.length > _DOC_MAX) content = content.slice(0, _DOC_MAX) + '\n[...truncated client-side]';
          doc.content = content;
          if(!doc._gone) docFiles.push(doc);
          res();
        };
        r.onerror = () => res();
      });
      _dropPending.add(job); job.then(() => _dropPending.delete(job));
      r.readAsText(file);
    } else if(!mediaTaken){
      mediaTaken = true;
      // Chip first (keeps the order you dropped them in), picture data when it's read.
      const a = {b64: null, name: file.name, mime: file.type || 'application/octet-stream', kind, size: file.size};
      const _url = URL.createObjectURL(file);
      a._thumb = _url;
      a._chip = _simpleChip('🖼', file.name, _fmtSize(file.size), () => { if(attached === a) attached = null; a._gone = true; }, _url);
      const r = new FileReader();
      const job = new Promise(res => {
        r.onload = e => {
          if(!a._gone){ a.b64 = e.target.result.split(',')[1]; a._thumb = e.target.result; attached = a; }
          res();
        };
        r.onerror = () => res();
      });
      _dropPending.add(job); job.then(() => _dropPending.delete(job));   // send waits for it
      r.readAsDataURL(file);
    }
  });
}

// Drag files anywhere onto the window. Panels with their own drop zones (dataset upload,
// Steam library) handle theirs first — those drops arrive here already 'defaultPrevented'.
(function(){
  let depth = 0;
  const ov = () => document.getElementById('drop-overlay');
  const hasFiles = e => e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files');
  window.addEventListener('dragenter', e => { if(!hasFiles(e)) return; depth++; ov() && ov().classList.add('on'); });
  window.addEventListener('dragleave', e => { if(!hasFiles(e)) return; depth = Math.max(0, depth-1); if(!depth && ov()) ov().classList.remove('on'); });
  window.addEventListener('dragover', e => { if(hasFiles(e)){ e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
  window.addEventListener('drop', e => {
    depth = 0; ov() && ov().classList.remove('on');
    if(!hasFiles(e) || e.defaultPrevented) return;
    e.preventDefault();
    _takeFiles(Array.from(e.dataTransfer.files || []));
    const inp = document.getElementById('msg-in'); inp && inp.focus();
  });
})();

function clearImg(keepDrops){
  attached = null;
  docFiles = [];
  if(!keepDrops){
    droppedFiles = [];
    const t = document.getElementById('drop-tray'); if(t) t.innerHTML = '';
  }
  const _t = document.getElementById('drop-tray');
  document.getElementById('img-preview').style.display = (_t && _t.children.length) ? 'flex' : 'none';
  document.getElementById('prev-img').removeAttribute('src'); document.getElementById('prev-img').style.display = 'none';
  document.getElementById('prev-vid').src = ''; document.getElementById('prev-vid').style.display = 'none';
  document.getElementById('prev-icon').style.display = 'none';
  document.getElementById('prev-name').textContent = '';
}

// Model picker — browse only, no dropdown
let _selectedModelPath = '';
let _modelSwitching = false;

// ── Window layout persistence (settings panel + game-library geometry) ────────
// Popups reopen where you left them; saved to ui_layout.json (survives reboots,
// and is bundled into snapshots). Off-screen saves are clamped back on-screen so
// the default is always somewhere reasonable.
let _uiLayout = {};
let _geoTimer = {};
(function loadUILayout(){
  fetch('/api/ui/layout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({get:1})})
    .then(r=>r.json()).then(d=>{ if(d && d.layout) _uiLayout = d.layout; }).catch(()=>{});
})();
function applyGeo(el, geo){
  if(!geo) return;
  el.style.position='fixed';
  if(geo.width)  el.style.width  = Math.max(260, Math.min(geo.width,  window.innerWidth  - 20)) + 'px';
  if(geo.height) el.style.height = Math.max(180, Math.min(geo.height, window.innerHeight - 20)) + 'px';
  const bw = el.offsetWidth || 300;
  if(geo.left!=null){
    el.style.right='auto'; el.style.transform='none';
    el.style.left = Math.max(0, Math.min(geo.left, window.innerWidth - Math.min(bw,80))) + 'px';
  }
  if(geo.top!=null) el.style.top = Math.max(0, Math.min(geo.top, window.innerHeight - 36)) + 'px';
}
function saveGeo(key, el){
  if(_geoTimer[key]) clearTimeout(_geoTimer[key]);
  _geoTimer[key] = setTimeout(function(){
    const r = el.getBoundingClientRect();
    const geo = {left:Math.round(r.left), top:Math.round(r.top), width:Math.round(r.width), height:Math.round(r.height)};
    _uiLayout[key] = geo;
    fetch('/api/ui/layout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key:key, geo:geo})}).catch(()=>{});
  }, 500);
}
function observeGeo(key, el){
  if(el._geoObserved) return;
  el._geoObserved = true;
  try{ new ResizeObserver(function(){ if(el.style.display!=='none') saveGeo(key, el); }).observe(el); }catch(e){}
}
function startPickerDrag(e){
  if(e.target && e.target.getAttribute && e.target.getAttribute('title') === 'Close') return;
  const p = document.getElementById('model-picker');
  const rect = p.getBoundingClientRect();
  // Re-anchor as fixed at its current spot so it can move anywhere on screen
  p.style.position = 'fixed';
  p.style.left = rect.left + 'px';
  p.style.top  = rect.top + 'px';
  p.style.right = 'auto';
  const offX = e.clientX - rect.left, offY = e.clientY - rect.top;
  function move(ev){
    let nx = Math.max(0, Math.min(window.innerWidth  - 60, ev.clientX - offX));
    let ny = Math.max(0, Math.min(window.innerHeight - 36, ev.clientY - offY));
    p.style.left = nx + 'px'; p.style.top = ny + 'px';
  }
  function up(){ document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); saveGeo('settings', p); }
  document.addEventListener('mousemove', move);
  document.addEventListener('mouseup', up);
  e.preventDefault();
}

async function toggleModelPicker(){
  const p = document.getElementById('model-picker');
  if(p.style.display !== 'none'){ closeModelPicker(); return; }
  p.style.display = 'flex';
  applyGeo(p, _uiLayout.settings); observeGeo('settings', p);
}
function closeModelPicker(){
  document.getElementById('model-picker').style.display = 'none';
}

// ── Category windows — each settings group is its own draggable popup ──────────
const _CATIDS = {model:'cat-model', identity:'cat-identity', voices:'cat-voices',
                 data:'cat-data', dome:'cat-dome', tuning:'cat-tuning', ext:'cat-ext'};
function openCat(key){
  const id = _CATIDS[key]; if(!id) return;
  const p = document.getElementById(id); if(!p) return;
  const gk = 'cat_' + key; p._gkey = gk;
  p.classList.add('open');
  applyGeo(p, _uiLayout[gk]); observeGeo(gk, p);
  try {
    if(key === 'model'){
      _selectedModelPath = '';
      const ms = document.getElementById('model-status'); if(ms) ms.textContent = 'Browse to select a model file.';
      const btn = document.getElementById('model-load-btn'); if(btn){ btn.disabled = true; btn.style.opacity = '0.4'; }
      loadCurrentModelSettings();
      refreshModelCurrent();
    } else if(key === 'identity'){ loadIdentityEditor(); }
    else if(key === 'voices'){ loadVoiceSelector(); loadMics(); loadVision(); }
    else if(key === 'data'){ loadGameplay(); loadMemoryFileList(); refreshCurrentState(); }
    else if(key === 'dome'){ loadDome(); }
    else if(key === 'tuning'){ loadTuning(); }
    else if(key === 'ext'){ checkUnreal(); }
  } catch(e){}
}
function closeCat(id){ const p = document.getElementById(id); if(p) p.classList.remove('open'); }
async function checkUnreal(){
  // Detect Unreal; show the launcher ONLY if a usable install is present. Absence is
  // silent — the capability just isn't part of this machine's System 32.
  const box = document.getElementById('unreal-ext');
  if(!box) return;
  try{
    const s = await fetch('/api/unreal/status').then(r=>r.json());
    if(s && s.installed){
      box.style.display = 'block';
      const lbl = document.getElementById('unreal-ext-label');
      if(lbl){
        let note = '&#9679; Unreal ' + (s.version || '?') + ' detected';
        if(!s.matches_pin) note += ' &middot; pinned target ' + (s.pinned || '?') + ' (yours may differ)';
        lbl.innerHTML = note;
      }
      const pf = document.getElementById('unreal-proj');
      if(pf && s.project) pf.value = s.project;
    } else {
      box.style.display = 'none';
    }
  }catch(e){ box.style.display = 'none'; }
}
async function launchUnreal(mode){
  const lbl = document.getElementById('unreal-ext-label');
  if(lbl) lbl.textContent = 'Launching ' + (mode === 'vr' ? 'VR' : 'PC') + '…';
  try{
    const r = await fetch('/api/unreal/launch', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:mode})}).then(r=>r.json());
    if(lbl && r) lbl.textContent = r.msg || (r.ok ? 'Launching…' : 'Launch failed.');
  }catch(e){ if(lbl) lbl.textContent = 'Launch failed.'; }
}
async function saveUnrealProject(){
  const pf = document.getElementById('unreal-proj');
  const lbl = document.getElementById('unreal-ext-label');
  if(!pf) return;
  try{
    const r = await fetch('/api/unreal/set-project', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({project:pf.value.trim()})}).then(r=>r.json());
    if(lbl){
      if(!r.ok) lbl.textContent = 'Save failed: ' + (r.error || '');
      else if(!r.project) lbl.textContent = 'Project cleared — launch opens the editor only.';
      else lbl.innerHTML = r.exists ? '&#10003; Project set — launch opens straight into it.'
                                    : '&#9888; Saved, but no .uproject found at that path.';
    }
  }catch(e){ if(lbl) lbl.textContent = 'Save failed.'; }
}
async function spawnTestCube(){
  const out = document.getElementById('unreal-spawn-result');
  if(out) out.textContent = 'Spawning… (editor must be running)';
  try{
    const r = await fetch('/api/unreal/spawn-test', {method:'POST'}).then(r=>r.json());
    if(out){
      if(r.ok) out.innerHTML = '&#10003; Cube spawned — look at your world.';
      else out.textContent = '\u2717 ' + (r.error || JSON.stringify(r.response||r)).slice(0,300);
    }
  }catch(e){ if(out) out.textContent = '\u2717 ' + e; }
}
async function refreshCurrentState(){
  const el = document.getElementById('current-state-name'); if(!el) return;
  try{
    const r = await fetch('/api/state/list',{method:'POST'});
    const d = await r.json();
    const a = (d.active || '').replace(/\.zip$/i,'');
    el.textContent = a || '— live working state (unsaved) —';
    el.style.color = a ? 'var(--accent)' : 'var(--muted)';
  }catch(e){ el.textContent = '—'; }
}
// ── Screen Vision (privacy / capture-mode / fps) ──────────────────────────────
function _applyVisionUI(s){
  if(!s) return;
  const t = document.getElementById('vision-toggle');
  if(t){
    if(s.enabled){ t.innerHTML = '&#9679; ON';  t.style.color = 'var(--accent3)'; t.style.borderColor = 'var(--accent3)'; }
    else         { t.innerHTML = '&#9679; OFF'; t.style.color = 'var(--muted)';   t.style.borderColor = 'var(--border)'; }
  }
  const m = document.getElementById('vision-mode');
  if(m) m.textContent = (s.mode === 'all') ? 'ALL MONITORS' : 'CURSOR MONITOR';
  const f = document.getElementById('vision-fps'), fv = document.getElementById('vision-fps-val');
  if(f) f.value = s.fps; if(fv) fv.textContent = s.fps;
}
async function loadVision(){
  try{
    const d = await fetch('/api/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({get:1})}).then(r=>r.json());
    _applyVisionUI(d);
  }catch(e){}
}
async function _visionSet(payload){
  const st = document.getElementById('vision-status');
  if(st) st.textContent = '';
  try{
    const d = await fetch('/api/vision',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
    if(d.settings) _applyVisionUI(d.settings);
    if(st && d.error) st.textContent = '✗ ' + d.error;
  }catch(e){ if(st) st.textContent = '✗ ' + e.message; }
}
function toggleVision(){
  const t = document.getElementById('vision-toggle');
  const on = t && t.innerHTML.indexOf('ON') >= 0;
  _visionSet({enabled: !on});
}
function toggleVisionMode(){
  const m = document.getElementById('vision-mode');
  const isAll = m && m.textContent.indexOf('ALL') >= 0;
  _visionSet({mode: isAll ? 'cursor' : 'all'});
}
function onVisionFps(v){ const fv = document.getElementById('vision-fps-val'); if(fv) fv.textContent = v; }
function commitVisionFps(v){ _visionSet({fps: parseInt(v,10)}); }
async function refreshModelCurrent(){
  const cur = document.getElementById('model-current'); if(!cur) return;
  try{
    const r = await fetch('/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const d = await r.json();
    const path = d.model_path || window._currentModelPath || '';
    cur.textContent = path ? path.split('\\').pop().split('/').pop() : '— none loaded —';
  } catch(e){
    const path = window._currentModelPath || '';
    cur.textContent = path ? path.split('\\').pop().split('/').pop() : '— none loaded —';
  }
}
function startCatDrag(e, id){
  const p = document.getElementById(id); if(!p) return;
  const r = p.getBoundingClientRect();
  p.style.transform = 'none'; p.style.left = r.left + 'px'; p.style.top = r.top + 'px'; p.style.right = 'auto';
  const ox = e.clientX - r.left, oy = e.clientY - r.top;
  function move(ev){
    p.style.left = Math.max(0, Math.min(ev.clientX - ox, window.innerWidth  - 60)) + 'px';
    p.style.top  = Math.max(0, Math.min(ev.clientY - oy, window.innerHeight - 30)) + 'px';
  }
  function up(){ document.removeEventListener('mousemove', move); document.removeEventListener('mouseup', up); if(p._gkey) saveGeo(p._gkey, p); }
  document.addEventListener('mousemove', move); document.addEventListener('mouseup', up);
  e.preventDefault();
}
// ── Settings panel functions ──────────────────────────────────────────────────
async function loadCurrentModelSettings(){
  try {
    const r = await fetch('/status');
    const d = await r.json();
    if(d.ctx_size)    document.getElementById('model-ctx').value = d.ctx_size;
    if(d.gpu_layers !== undefined) document.getElementById('model-gpu-layers').value = d.gpu_layers;
    if(d.model_path){
      const parts = d.model_path.replace(/\\/g,'/').split('/');
      document.getElementById('model-selected').value = parts[parts.length-1] || d.model_path;
    }
  } catch(e){}
}

async function loadIdentityEditor(){
  try {
    const r = await fetch('/api/identity');
    if(!r.ok){ console.error('Identity fetch failed:', r.status); return; }
    const d = await r.json();
    document.getElementById('identity-editor').value = d.content || '';
    const ni = document.getElementById('identity-name');
    if(ni) ni.value = (d.name && d.name !== 'System 32') ? d.name : '';
    const gi = document.getElementById('identity-gender');
    if(gi) gi.value = d.gender || 'Neutral';
  } catch(e){ console.error('Identity load error:', e); }
}

// Current user-facing assistant name (badge shows full name when renamed; 'S32' by default)
let _assistantName = 'System 32';
function _aiTag(){ return (_assistantName && _assistantName !== 'System 32') ? _assistantName : 'S32'; }

// Set the user-facing name on the header logo + input placeholder (platform "System 32" stays elsewhere)
function applyAssistantName(name){
  const nm = (name && name.trim()) ? name.trim() : 'System 32';
  _assistantName = nm;
  const logo = document.getElementById('logo');
  if(logo){
    if(nm === 'System 32'){ logo.innerHTML = 'SYS<span>TEM</span> 32'; }
    else { logo.textContent = nm; }
  }
  const mi = document.getElementById('msg-in');
  if(mi) mi.placeholder = 'Talk to ' + nm + '...';
  // refresh any AI nametag badges already on screen
  document.querySelectorAll('.av.ai').forEach(el=>{ el.textContent = _aiTag(); });
}

async function saveIdentity(btn){
  const text = document.getElementById('identity-editor').value;
  const ni   = document.getElementById('identity-name');
  const name = ni ? ni.value.trim() : '';
  const gi   = document.getElementById('identity-gender');
  const gender = gi ? gi.value : 'Neutral';
  try {
    const r = await fetch('/api/identity', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({content:text, name:name, gender:gender})});
    const d = await r.json();
    if(d.ok) applyAssistantName(d.name);
    if(btn){ btn.textContent = d.ok ? 'Saved \u2713' : 'Failed'; setTimeout(()=>{ btn.textContent = 'Save'; }, 2000); }
  } catch(e){ console.error('Save error:', e); }
}
// Apply the saved display name to header + placeholder on page load
(async function _loadAssistantName(){
  try{
    const r = await fetch('/api/identity');
    if(r.ok){ const d = await r.json(); applyAssistantName(d.name); }
  }catch(_){}
})();

async function loadVoiceSelector(){
  try {
    const r = await fetch('/api/voices');
    if(!r.ok){ console.error('Voices fetch failed:', r.status); return; }
    const d = await r.json();
    const sel = document.getElementById('voice-select');
    sel.innerHTML = '';
    (d.voices || []).forEach(v => {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = v;
      if(v === d.current) opt.selected = true;
      sel.appendChild(opt);
    });
    document.getElementById('current-voice-label').textContent = d.current || '—';
    if(typeof d.volume === 'number'){
      const pct = Math.round(d.volume * 100);
      const sl = document.getElementById('voice-volume');
      if(sl){ sl.value = pct; document.getElementById('voice-volume-val').textContent = pct + '%'; }
    }
  } catch(e){ console.error('Voice load error:', e); }
}

function onVolumeInput(val){
  document.getElementById('voice-volume-val').textContent = Math.round(val) + '%';
}
let _volTimer = null;
async function commitVolume(val){
  clearTimeout(_volTimer);
  _volTimer = setTimeout(async () => {
    try {
      await fetch('/api/voice/volume', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({volume: Math.max(0, Math.min(1, val/100))})});
    } catch(e){}
  }, 120);
}

// ── Voice Input (STT) ──────────────────────────────────────────────
let _sttLevelTimer = null;
let _sttEnabled = false, _sttMode = 'continuous';
async function loadMics(){
  try{
    const d = await fetch('/api/stt/devices').then(r=>r.json());
    const sel = document.getElementById('stt-mic');
    const st  = document.getElementById('stt-status');
    if(!sel) return;
    sel.innerHTML = '';
    const def = document.createElement('option');
    def.value = 'default'; def.textContent = 'System default';
    sel.appendChild(def);
    (d.devices||[]).forEach(m => {
      const o = document.createElement('option');
      o.value = m.index; o.textContent = m.name + (m.default ? ' (default)' : '');
      if(d.selected === m.index) o.selected = true;
      sel.appendChild(o);
    });
    if(st) st.textContent = d.error ? d.error : ((d.devices||[]).length + ' input device(s)');
    setSttToggle(d.enabled);
    setSttMode(d.mode || 'continuous');
  }catch(e){ const st=document.getElementById('stt-status'); if(st) st.textContent='Mic list failed: '+e.message; }
}
function setSttMode(mode){
  _sttMode = (mode === 'ptt') ? 'ptt' : 'continuous';
  const b = document.getElementById('stt-mode');
  if(!b) return;
  if(_sttMode === 'ptt'){
    b.textContent='PTT'; b.style.color='var(--accent2)'; b.style.borderColor='var(--accent2)';
    b.title='Push-to-talk: hold the V key (window focused, not typing in a box) to talk; release to send.';
  } else {
    b.textContent='CONT'; b.style.color='var(--accent)'; b.style.borderColor='var(--accent)';
    b.title='Continuous: just talk — it transcribes and sends after you pause.';
  }
  b.dataset.mode = _sttMode;
}
async function toggleSTTMode(){
  const b = document.getElementById('stt-mode');
  const next = (b && b.dataset.mode === 'ptt') ? 'continuous' : 'ptt';
  setSttMode(next);
  try{
    await fetch('/api/stt/mode', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode: next})});
  }catch(e){}
}
function setSttToggle(on){
  _sttEnabled = !!on;
  const b = document.getElementById('stt-toggle');
  if(!b) return;
  if(on){ b.textContent='🎤 ON'; b.style.background='var(--accent3)'; b.style.color='#000'; b.style.borderColor='var(--accent3)'; startSttMeter(); }
  else  { b.textContent='🎤 OFF'; b.style.background='var(--bg3)'; b.style.color='var(--muted)'; b.style.borderColor='var(--border)'; stopSttMeter(); }
}
function injectTranscript(text){
  if(!text) return;
  const inp = document.getElementById('msg-in');
  if(!inp) return;
  if(typeof busy !== 'undefined' && busy) return;   // it's mid-reply; drop to avoid overlap
  if(_dlLock) return;                               // downloading/restarting — same rule as typing
  inp.value = text;
  try{ resize(inp); }catch(e){}
  send();
}
function isTypingTarget(el){
  if(!el) return false;
  const t = (el.tagName||'').toLowerCase();
  return t==='input' || t==='textarea' || el.isContentEditable;
}
let _vDown = false;
document.addEventListener('keydown', (e)=>{
  if((e.key==='v'||e.key==='V') && !e.repeat && !e.ctrlKey && !e.metaKey && !e.altKey){
    if(!_sttEnabled || _sttMode!=='ptt' || isTypingTarget(e.target) || _vDown) return;
    _vDown = true;
    const b=document.getElementById('stt-toggle'); if(b) b.textContent='🎤 ●REC';
    fetch('/api/stt/ptt/start', {method:'POST'}).catch(()=>{});
  }
});
document.addEventListener('keyup', (e)=>{
  if(e.key==='v'||e.key==='V'){
    if(!_vDown) return;
    _vDown = false;
    const b=document.getElementById('stt-toggle'); if(b && _sttEnabled) b.textContent='🎤 ON';
    if(_sttMode==='ptt') fetch('/api/stt/ptt/stop', {method:'POST'}).catch(()=>{});
  }
});
async function toggleSTT(){
  try{
    const d = await fetch('/api/stt/toggle', {method:'POST'}).then(r=>r.json());
    setSttToggle(!!d.enabled);
    const st = document.getElementById('stt-status');
    if(st && d.error) st.textContent = d.error;
  }catch(e){ const st=document.getElementById('stt-status'); if(st) st.textContent='Toggle failed: '+e.message; }
}
async function selectMic(val){
  try{
    await fetch('/api/stt/select', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({device: val})});
  }catch(e){}
}
function startSttMeter(){
  stopSttMeter();
  _sttLevelTimer = setInterval(async () => {
    try{
      const d = await fetch('/api/stt/level').then(r=>r.json());
      const bar = document.getElementById('stt-level');
      if(bar) bar.style.width = Math.max(0, Math.min(100, d.level||0)) + '%';
      const st = document.getElementById('stt-status');
      if(st){
        if(d.model_msg && d.model_status!=='ready') st.textContent = '⏳ ' + d.model_msg;
        else if(d.transcribing) st.textContent = '… transcribing';
        else if(d.model_status==='ready') st.textContent = (d.mode==='ptt' ? 'Ready — hold V to talk' : 'Ready — listening');
        else if(d.model_status==='error') st.textContent = '⚠ ' + (d.model_msg||'speech engine error');
      }
      if(!d.enabled){ setSttToggle(false); }
    }catch(e){}
  }, 120);
}
function stopSttMeter(){
  if(_sttLevelTimer){ clearInterval(_sttLevelTimer); _sttLevelTimer = null; }
  const bar = document.getElementById('stt-level');
  if(bar) bar.style.width = '0%';
}

// ── Model tuning (live sampling sliders) ───────────────────────────
let _tuningSpec = [];
function _fmtTune(v, isInt){ return isInt ? String(v) : Number(v).toFixed(2); }
async function loadTuning(){
  const wrap = document.getElementById('tuning-sliders');
  if(!wrap) return;
  try{
    const d = await fetch('/api/tuning').then(r=>r.json());
    _tuningSpec = d.spec || [];
    const vals = d.values || {};
    wrap.innerHTML = _tuningSpec.map(s=>{
      const v = (s.key in vals) ? vals[s.key] : s.default;
      return `<div style="margin-bottom:13px">
        <div style="display:flex;justify-content:space-between;align-items:baseline">
          <span style="color:var(--fg);font-weight:600">${s.label}</span>
          <span id="tv-${s.key}" style="color:var(--accent);font-family:var(--mono)">${s.toggle?(+v?'ON':'OFF'):_fmtTune(v, s.int)}</span>
        </div>
        <input type="range" min="${s.min}" max="${s.max}" step="${s.step}" value="${v}" class="${s.toggle?'tg':''}"
          oninput="document.getElementById('tv-${s.key}').textContent=${s.toggle?"(+this.value?'ON':'OFF')":"_fmtTune(this.value, "+(s.int?1:0)+")"}"
          onchange="saveTuning()" data-tkey="${s.key}"
          style="width:100%;margin-top:4px;accent-color:var(--accent)">
        <div style="font-size:8.5px;color:var(--muted);margin-top:1px">${s.hint}</div>
      </div>`;
    }).join('');
  }catch(e){ wrap.textContent = 'tuning unavailable: ' + e.message; }
}
function _collectTuning(){
  const out = {};
  document.querySelectorAll('#tuning-sliders input[data-tkey]').forEach(inp=>{
    out[inp.getAttribute('data-tkey')] = parseFloat(inp.value);
  });
  return out;
}
async function saveTuning(){
  const st = document.getElementById('tuning-status');
  try{
    await fetch('/api/tuning',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({values:_collectTuning()})});
    if(st){ st.textContent = 'saved · applies on next message'; setTimeout(()=>{ if(st) st.textContent=''; }, 2000); }
  }catch(e){ if(st) st.textContent = 'save failed: ' + e.message; }
}
async function resetTuning(){
  try{
    await fetch('/api/tuning',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({reset:true})});
    loadTuning();
    const st = document.getElementById('tuning-status');
    if(st){ st.textContent = 'reset to defaults'; setTimeout(()=>{ if(st) st.textContent=''; }, 2000); }
  }catch(e){}
}

// ── The Iron Dome ──────────────────────────────────────────────────
async function loadDome(){
  try{
    const d = await fetch('/api/dome/status').then(r=>r.json());
    const learn = document.getElementById('dome-learn');
    if(learn){
      const c = d.counts || {};
      learn.textContent = d.ready
        ? `baselined · ${d.samples} samples · ${c.necessary||0} necessary, ${c.unnecessary||0} unnecessary, ${c.new||0} new`
        : `learning… ${d.samples||0}/${d.min_samples||5} samples before it can judge`;
    }
    const wl = document.getElementById('dome-whitelist');
    if(wl && document.activeElement !== wl){
      const w = d.whitelist || {ports:[],procs:[]};
      wl.value = [...(w.ports||[]), ...(w.procs||[])].join(' ');
    }
    const pl = document.getElementById('dome-ports');
    if(pl){
      const rows = (d.ports||[]).map(p => {
        let col = 'var(--muted)', tag = '';
        if(p.protected){ col='#7a8aa0'; tag=' ·protected'; }
        else if(p.whitelisted){ col='var(--accent)'; tag=' ·whitelisted'; }
        else if(p.necessary){ col='var(--accent3)'; }
        else { col='#e05555'; tag=' ·unnecessary'; }
        return `<div style="color:${col}">${p.port} <span style="opacity:.7">${p.proc||''}</span>${tag}</div>`;
      });
      pl.innerHTML = rows.length ? rows.join('') : '<span style="opacity:.6">no listening ports seen yet</span>';
    }
    _setDomeButton(!!d.armed, d.mode);
    { const at = document.getElementById('airplane-toggle'); if(at && typeof d.airplane === 'boolean') at.checked = d.airplane; }
    // Outbound watcher (s32_egress): learning progress + "baseline now"
    try{
      const e = await fetch('/api/egress/status').then(r=>r.json());
      let eg = document.getElementById('egress-line');
      if(!eg && learn){
        eg = document.createElement('div'); eg.id = 'egress-line';
        eg.style.cssText = 'font-size:9px;color:var(--muted);margin:-4px 0 8px;display:flex;gap:8px;align-items:center';
        learn.parentNode.insertBefore(eg, learn.nextSibling);
      }
      if(eg && !e.error){
        const tbl = e.owners_table === 'ready' ? '' : (e.owners_table === 'downloading' ? ' · owner table downloading' : ' · owner table missing (grouping by address)');
        eg.innerHTML = '';
        const t = document.createElement('span');
        t.textContent = (e.mature ? `outbound: watching ${e.programs} program(s) for drift` :
          `outbound: learning ${e.watched_hours} of ${e.learn_hours} h · ${e.programs} program(s)`) + tbl;
        eg.appendChild(t);
        if(!e.mature){
          const bn = document.createElement('button');
          bn.textContent = 'BASELINE NOW';
          bn.title = 'Only when this computer is clean: everything it does right now becomes normal, and it starts flagging drift.';
          bn.style.cssText = 'background:transparent;color:var(--accent3);border:1px solid var(--accent3);border-radius:3px;font-size:9px;padding:1px 6px;cursor:pointer';
          bn.onclick = async () => { if(!confirm('Mark what this computer does right now as normal? Only do this when you trust that it is clean.')) return;
            await fetch('/api/egress/baseline',{method:'POST'}); loadDome(); };
          eg.appendChild(bn);
        }
      }
      // Outbound blocks (permanent) — each with Unblock
      let bl = document.getElementById('egress-blocks');
      if(!bl && eg){ bl = document.createElement('div'); bl.id = 'egress-blocks';
        bl.style.cssText = 'font-size:10px;font-family:var(--mono);margin:0 0 8px;line-height:1.6';
        eg.parentNode.insertBefore(bl, eg.nextSibling); }
      if(bl){
        const lst = (await fetch('/api/egress/blocks').then(r=>r.json())).blocks || [];
        bl.innerHTML = '';
        if(lst.length){
          const h = document.createElement('div'); h.textContent = 'OUTBOUND BLOCKS'; h.style.cssText = 'font-size:9px;letter-spacing:1px;color:var(--muted)'; bl.appendChild(h);
        }
        lst.forEach(b => {
          const row = document.createElement('div'); row.style.cssText = 'display:flex;gap:6px;align-items:center;color:#e05555';
          const t = document.createElement('span'); t.style.flex = '1';
          t.textContent = `${b.name} ✕ ${b.what} · ${b.created}`; t.title = b.exe || '';
          const u = document.createElement('button'); u.textContent = 'Unblock';
          u.style.cssText = 'background:transparent;color:var(--accent3);border:1px solid var(--accent3);border-radius:3px;font-size:9px;padding:0 6px;cursor:pointer';
          u.onclick = async () => { const j = await fetch('/api/egress/unblock',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rule:b.rule})}).then(r=>r.json());
            if(!j.ok) alert(j.error || 'Not unblocked.'); loadDome(); };
          row.append(t, u); bl.appendChild(row);
        });
      }
    }catch(_){}
    // Without admin the firewall can't be changed — no button that would only PRETEND to
    // arm (2026-10-31). It keeps learning; the note says how to enable it.
    const btn = document.getElementById('dome-btn');
    let na = document.getElementById('dome-needs-admin');
    if(btn && !na){
      na = document.createElement('div'); na.id = 'dome-needs-admin';
      na.style.cssText = 'font-size:11px;line-height:1.5;color:#e0a040;border:1px solid #e0a040;border-radius:4px;padding:8px 10px;display:none';
      na.textContent = '⚠ The Iron Dome needs administrator rights to change the Windows Firewall. Close System 32 and start it with "Run as administrator" to arm it. Until then it keeps learning what\'s normal on this computer.';
      btn.parentNode.insertBefore(na, btn.nextSibling);
    }
    if(btn && na){
      const noAdmin = d.admin === false && !d.armed;
      btn.style.display = noAdmin ? 'none' : '';
      na.style.display = noAdmin ? 'block' : 'none';
    }
  }catch(e){ const l=document.getElementById('dome-learn'); if(l) l.textContent='status unavailable: '+e.message; }
}
async function setAirplane(on){
  const t = document.getElementById('airplane-toggle');
  try{
    const j = await fetch('/api/airplane',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({on})}).then(r=>r.json());
    const fw = j.firewall || {};
    const n = document.getElementById('airplane-note');
    if(n) n.textContent = on
      ? (fw.ok ? '— offline: System 32, ComfyUI and the model are cut off from the internet'
               : '— offline inside System 32 (run as administrator to also firewall ComfyUI and the model)')
      : (fw.left && fw.left.length ? '— online, but firewall rules remain: restart as administrator to clear them' : '— System 32 never touches the internet');
    const ai = document.getElementById('airplane-ind'); if(ai) ai.style.display = on ? 'inline' : 'none';
  }catch(e){ alert('Airplane mode: '+e.message); if(t) t.checked = !on; }
}
function _setDomeButton(armed, mode){
  const b = document.getElementById('dome-btn'); if(!b) return;
  if(armed){
    b.innerHTML = '&#9960; DOME ACTIVE &middot; REOPEN PORTS';
    b.style.color = 'var(--accent3)'; b.style.borderColor = 'var(--accent3)';
    b.style.background = 'rgba(57,255,20,0.07)';
  } else {
    b.innerHTML = 'CLOSE UNNECESSARY PORTS &amp; PROCESSES';
    b.style.color = '#e05555'; b.style.borderColor = '#e05555';
    b.style.background = 'transparent';
  }
}
function domeButtonClick(){
  const b = document.getElementById('dome-btn');
  const armed = b && b.innerHTML.indexOf('REOPEN') >= 0;
  if(armed) domeDisarm(); else domeShowPlan();
}
async function domeArm(){
  const mode = (document.getElementById('dome-arm-mode')||{}).value || 'block';
  const label = {block:'block the network of', kill:'kill', both:'block and kill'}[mode] || 'close';
  if(!confirm('Arm the Iron Dome? It will firewall-block the listed ports and '+label+' the listed processes. Reversible — REOPEN (or a reboot) restores everything.')) return;
  const btn = document.getElementById('dome-arm-btn');
  if(btn){ btn.disabled = true; btn.textContent = 'Arming…'; }
  try{
    const r = await fetch('/api/dome/arm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    let d = {}; try{ d = await r.json(); }catch(_){ d = {}; }
    document.getElementById('dome-plan-popup').style.display = 'none';
    loadDome();
    if(d.error) alert('Arm failed: '+d.error);
  }catch(e){ alert('Arm failed: '+e.message); }
  if(btn){ btn.disabled = false; btn.innerHTML = '&#9960; ARM THE DOME'; }
}
async function domeDisarm(){
  try{
    await fetch('/api/dome/disarm',{method:'POST'});
    loadDome();
  }catch(e){ alert('Disarm failed: '+e.message); }
}
async function saveWhitelist(btn){
  const wl = document.getElementById('dome-whitelist');
  const st = document.getElementById('dome-wl-status');
  if(btn) btn.disabled = true;
  try{
    const d = await fetch('/api/dome/whitelist', {method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text: wl ? wl.value : ''})}).then(r=>r.json());
    if(st) st.textContent = `saved · ${(d.ports||[]).length} ports, ${(d.procs||[]).length} processes whitelisted`;
    loadDome();
  }catch(e){ if(st) st.textContent='save failed: '+e.message; }
  finally{ if(btn) btn.disabled = false; }
}
async function domeShowPlan(){
  const body = document.getElementById('dome-plan-body');
  const pop = document.getElementById('dome-plan-popup');
  const ctl = document.getElementById('dome-arm-controls');
  const closeBtn = document.getElementById('dome-plan-close');
  if(ctl) ctl.style.display = 'none';
  if(closeBtn) closeBtn.style.display = 'block';
  if(body) body.innerHTML = '<div style="opacity:.7">computing plan…</div>';
  if(pop) pop.style.display = 'block';
  try{
    const d = await fetch('/api/dome/plan').then(r=>r.json());
    if(!d.ready){
      body.innerHTML = `<div style="color:var(--muted)">Still learning — only ${d.samples||0} samples so far. Let it baseline a while (across a few sessions) before it can tell necessary from unnecessary.</div>`;
      return;
    }
    const list = (arr, fn) => arr.length ? arr.map(fn).join('') : '<div style="opacity:.6;padding-left:8px">— none —</div>';
    const nClose = (d.close_ports||[]).length + (d.close_procs||[]).length;
    body.innerHTML = `
      <div style="margin-bottom:8px;color:#e05555;font-weight:700">Will close:</div>
      <div style="padding-left:8px;font-family:var(--mono);font-size:11px">
        ${list(d.close_ports, p=>`<div>port ${p.port} <span style="opacity:.7">${p.proc||''}</span></div>`)}
        ${list(d.close_procs, p=>`<div>process ${p.proc}</div>`)}
      </div>
      <div style="margin-top:12px;margin-bottom:6px;color:var(--accent);font-weight:700">Held back (whitelisted):</div>
      <div style="padding-left:8px;font-family:var(--mono);font-size:11px">${list(d.held_whitelisted, p=>`<div>${p.port?('port '+p.port):''} ${p.proc||''}</div>`)}</div>
      <div style="margin-top:12px;margin-bottom:6px;color:#7a8aa0;font-weight:700">Held back (protected — System 32's own stack + critical Windows):</div>
      <div style="padding-left:8px;font-family:var(--mono);font-size:11px">${list(d.held_protected, p=>`<div>${p.port?('port '+p.port):''} ${p.proc||''}</div>`)}</div>`;
    if(nClose > 0){
      if(ctl) ctl.style.display = 'block';
      if(closeBtn) closeBtn.style.display = 'none';
    } else {
      body.innerHTML += '<div style="margin-top:12px;padding:8px;background:var(--bg3);border-radius:4px;color:var(--accent3);font-size:11px">Nothing to close — everything currently up is necessary, whitelisted, or protected.</div>';
    }
  }catch(e){ body.innerHTML = '<div style="color:#e05555">plan failed: '+e.message+'</div>'; }
}

// ── Steam games launcher ───────────────────────────────────────────
function startSteamDrag(e){
  if(e.target && e.target.getAttribute && e.target.getAttribute('title')==='Close') return;
  const p=document.getElementById('steam-popup');
  const rect=p.getBoundingClientRect();
  p.style.transform='none'; p.style.left=rect.left+'px'; p.style.top=rect.top+'px';
  const offX=e.clientX-rect.left, offY=e.clientY-rect.top;
  function move(ev){
    p.style.left=Math.max(0,Math.min(window.innerWidth-60,ev.clientX-offX))+'px';
    p.style.top =Math.max(0,Math.min(window.innerHeight-36,ev.clientY-offY))+'px';
  }
  function up(){ document.removeEventListener('mousemove',move); document.removeEventListener('mouseup',up); saveGeo('library', p); }
  document.addEventListener('mousemove',move); document.addEventListener('mouseup',up);
  e.preventDefault();
}
function openSteamGames(){
  try{ closeModelPicker(); }catch(e){}
  const sp=document.getElementById('steam-popup');
  sp.style.display='flex';
  applyGeo(sp, _uiLayout.library); observeGeo('library', sp);
  loadSteamGames();
}
let _drag = null;                 // {kind:'tile',appid,fromCat} | {kind:'cat',name}
let _steamTimer = null, _steamSeq = 0, _catGames = {}, _uncatOrder = [], _iconBust = {};
let _collapsed = new Set(), _collapsedInit = false;
async function loadSteamGames(){
  if(_steamTimer){ clearTimeout(_steamTimer); _steamTimer=null; }
  const seq = ++_steamSeq;
  const body=document.getElementById('steam-body');
  const showH=document.getElementById('steam-show-hidden');
  const q=(showH && showH.checked) ? '?show_hidden=1' : '';
  try{
    const d=await fetch('/api/steam/games'+q).then(r=>r.json());
    if(seq!==_steamSeq) return;     // a newer load started — drop this stale result (no flicker)
    renderSteam(d);
    if(d.resolving>0) _steamTimer=setTimeout(loadSteamGames, 4000);
  }catch(e){ if(seq===_steamSeq && body) body.innerHTML='<div style="color:#e05555">Steam scan failed: '+e.message+'</div>'; }
}
function steamBanner(d){
  const b=document.getElementById('steam-banner');
  if(!b) return;
  if(d.user_playing){
    b.style.display='flex'; b.style.background='rgba(224,85,85,0.15)'; b.style.borderColor='#e05555'; b.style.color='#ffb3b3';
    b.innerHTML='<span>⏸ Paused so you can play — back when you close the game.</span>'+
      '<button onclick="restoreBrain()" style="background:#e05555;color:#fff;border:none;border-radius:4px;padding:4px 8px;font-size:10px;cursor:pointer;font-weight:700;white-space:nowrap">Boot back up</button>';
  } else if(d.s32_playing){
    b.style.display='flex'; b.style.background='rgba(57,255,20,0.12)'; b.style.borderColor='var(--accent3)'; b.style.color='#bff7b0';
    b.innerHTML='<span>🤖 Playing a game (ComfyUI closed to free VRAM).</span>'+
      '<button onclick="restoreBrain()" style="background:var(--accent3);color:#000;border:none;border-radius:4px;padding:4px 8px;font-size:10px;cursor:pointer;font-weight:700;white-space:nowrap">Restore ComfyUI</button>';
  } else { b.style.display='none'; }
}
function renderSteam(d){
  const body=document.getElementById('steam-body');
  const cnt=document.getElementById('steam-count');
  if(cnt) cnt.textContent = d.steam_found ? (d.count+' games'+(d.resolving?' · sorting…':'')) : 'no library';
  const hc=document.getElementById('steam-hidden-count'); if(hc) hc.textContent = d.hidden_count? '('+d.hidden_count+')':'';
  const ls=document.getElementById('lib-stores');
  if(ls) ls.innerHTML=(d.stores||[]).map(s=>steamStoreChip(s)).join('');
  steamBanner(d);
  if(!d.steam_found){ body.innerHTML='<div style="color:var(--muted);font-size:12px">No game library detected yet. Steam: if it\'s on an unusual drive, set <code>steam_path</code> in config. (Epic / EA / Ubisoft / GOG game-merging is coming.)</div>'; return; }
  if(!d.count){ body.innerHTML='<div style="color:var(--muted)">No installed games found.</div>'; return; }
  _catGames={};
  (function idx(nodes){ (nodes||[]).forEach(c=>{ _catGames[c.name]=c.games.map(g=>g.appid); idx(c.children); }); })(d.custom);
  if(!_collapsedInit){ (function ci(nodes){ (nodes||[]).forEach(c=>{ if(c.collapsed) _collapsed.add(c.name); ci(c.children); }); })(d.custom); _collapsedInit=true; }
  _uncatOrder=[]; (d.groups||[]).forEach(g=>g.games.forEach(x=>_uncatOrder.push(x.appid)));
  let html='';
  (d.custom||[]).forEach(c=>html+=catHTML(c,0));
  (d.groups||[]).forEach(g=>{
    html+=`<div ondragover="event.preventDefault()" ondrop="steamGenreDrop(event)">
      <div style="font-size:10px;letter-spacing:2px;color:var(--muted);margin:10px 0 6px">${esc(g.genre).toUpperCase()}</div>${gridOpen()}`;
    g.games.forEach(game=>html+=steamTileHTML(game,''));
    html+='</div></div>';
  });
  body.innerHTML=html;
}
function steamStoreChip(s){
  const url=(s.shop_url||'').replace(/'/g,'');
  return `<span onmousedown="event.stopPropagation()" onclick="event.stopPropagation();window.open('${url}','_blank')" title="Open ${esc(s.label)} shop" style="display:inline-flex;align-items:center;gap:5px;cursor:pointer;font-size:11px;font-weight:700;letter-spacing:1px;color:var(--muted)">${steamStoreIcon(s.platform)}<span>${esc(s.label).toUpperCase()}</span></span>`;
}
function steamStoreIcon(p){
  const c={steam:'#66c0f4',epic:'#e6e6e6',gog:'#a855f7',ea:'#ff5050',ubisoft:'#1f8fff',xbox:'#107c10',battlenet:'#00aeff'}[p]||'#8893a5';
  if(p==='steam') return `<svg width="15" height="15" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" fill="none" stroke="${c}" stroke-width="1.9"/><circle cx="15" cy="9" r="3" fill="${c}"/><circle cx="8.5" cy="15" r="2.1" fill="${c}"/><line x1="13.2" y1="10.2" x2="9.4" y2="14" stroke="${c}" stroke-width="1.5"/></svg>`;
  const ltr={epic:'E',gog:'G',ea:'EA',ubisoft:'U',xbox:'X',battlenet:'B'}[p]||'?';
  return `<span style="display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;background:${c};color:#0a0c10;font-size:8px;font-weight:800">${ltr}</span>`;
}
function catHTML(c, depth){
  const cn=esc(c.name).replace(/'/g,"\\'");
  const col=c.color || 'var(--accent3)';
  const dimF=c.hidden?'opacity:.55;':'';
  const ind=depth>0 ? `margin-left:${depth*14}px;` : '';
  const collapsed=_collapsed.has(c.name);
  const count=(c.games?c.games.length:0)+(c.children?c.children.length:0);
  let h=`<div class="steam-cat" data-cat="${esc(c.name)}"
      ondragover="steamCatOver(event)" ondragleave="steamCatOut(event)" ondrop="steamCatDrop(event,'${cn}')"
      style="border:1px dashed var(--border);border-radius:6px;padding:7px;margin:8px 0;${ind}${dimF}">
    <div class="cat-header" draggable="true" ondragstart="steamDragCat(event,'${cn}')" ondragend="steamDragEnd()"
         ondragover="steamHeadOver(event)" ondragleave="steamHeadOut(event)" ondrop="steamHeaderDrop(event,'${cn}')"
         oncontextmenu="steamFolderContext(event,'${cn}',${c.hidden?1:0},${c.parent?1:0});return false;"
         style="display:flex;align-items:center;gap:6px;margin-bottom:6px;cursor:grab;user-select:none" title="click name to collapse · drag to reorder · drop a folder on me to nest · right-click for more">
      <span class="cat-chev" onclick="event.stopPropagation();toggleCatCollapse(event,'${cn}')" title="Collapse / expand" style="cursor:pointer;color:var(--muted);font-size:11px;width:12px;text-align:center;flex:none">${collapsed?'▸':'▾'}</span>
      <span style="color:var(--muted);font-size:12px;line-height:1;flex:none">⠿</span>
      <span onclick="event.stopPropagation();toggleCatCollapse(event,'${cn}')" style="font-size:10px;letter-spacing:2px;color:${col};flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700;cursor:pointer">${esc(c.name).toUpperCase()}${collapsed&&count?` <span style="opacity:.6">(${count})</span>`:''}${c.hidden?' · hidden':''}</span>
      <span onclick="event.stopPropagation();steamColorPicker(event,'${cn}','${(c.color||'').replace(/'/g,'')}')" title="Name color" style="cursor:pointer;font-size:12px;flex:none">🎨</span>
      <span onclick="event.stopPropagation();newSteamCategory('${cn}')" title="Add subcategory" style="cursor:pointer;color:var(--muted);font-size:14px;line-height:1;flex:none">＋</span>
      <span onclick="event.stopPropagation();renameSteamCategory('${cn}')" title="Rename" style="cursor:pointer;color:var(--muted);font-size:11px;flex:none">✎</span>
      <span onclick="event.stopPropagation();deleteSteamCategory('${cn}')" title="Delete" style="cursor:pointer;color:var(--accent2);font-size:13px;line-height:1;flex:none">×</span>
    </div>
    <div class="cat-content" style="${collapsed?'display:none':''}">${gridOpen()}`;
  if(!c.games.length && !(c.children||[]).length) h+='<div style="grid-column:1/-1;font-size:9px;color:var(--muted);opacity:.6;padding:10px;text-align:center">drag games or a folder here</div>';
  c.games.forEach(g=>h+=steamTileHTML(g,c.name));
  h+='</div>';                                   // close grid
  (c.children||[]).forEach(ch=>h+=catHTML(ch, depth+1));
  h+='</div>';                                   // close cat-content
  h+='</div>';                                   // close steam-cat
  return h;
}
function toggleCatCollapse(ev, name){
  ev.stopPropagation();
  const cat=ev.target.closest('.steam-cat');
  if(!cat) return;
  const content=cat.querySelector(':scope > .cat-content');
  const chev=cat.querySelector(':scope > .cat-header > .cat-chev');
  if(!content) return;
  const isCollapsed = _collapsed.has(name);
  if(isCollapsed) _collapsed.delete(name); else _collapsed.add(name);
  content.style.display = isCollapsed ? '' : 'none';
  if(chev) chev.textContent = isCollapsed ? '▾' : '▸';
  fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({op:'set_collapsed', name:name, collapsed:!isCollapsed})}).catch(()=>{});
}
function gridOpen(){ return '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(92px,1fr));gap:8px;min-height:24px">'; }
function steamTileHTML(game, catName){
  const uid=(game.appid+'').replace(/'/g,"\\'");
  let art = game.art || '';
  if(art && _iconBust[game.appid]) art += (art.indexOf('?')>=0?'&':'?') + 'b=' + _iconBust[game.appid];
  const dim = game.hidden ? 'opacity:.45;' : '';
  const cn=(catName||'').replace(/'/g,"\\'");
  return `<div class="steam-tile" draggable="true"
            ondragstart="steamDragTile(event,'${uid}','${cn}')" ondragend="steamDragEnd()"
            ondragover="steamTileOver(event)" ondragleave="steamTileOut(event)" ondrop="steamTileDrop(event,'${uid}','${cn}')"
            oncontextmenu="steamContext(event,'${uid}',${game.hidden?1:0},${catName?1:0},${game.custom?1:0});return false;"
            onclick="steamPick(this)" title="${esc(game.name)} — drag into a folder · right-click for options"
            style="position:relative;cursor:grab;border-radius:6px;overflow:hidden;border:1px solid var(--border);aspect-ratio:2/3;background:#0a0c10;${dim}">
    ${art?`<img src="${art}" loading="lazy" draggable="false" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" style="width:100%;height:100%;object-fit:cover;display:block;pointer-events:none">`:''}
    <div style="display:${art?'none':'flex'};width:100%;height:100%;align-items:center;justify-content:center;text-align:center;font-size:10px;color:var(--fg);padding:6px;pointer-events:none">${esc(game.name)}</div>
    ${game.hidden?'<div style="position:absolute;top:3px;right:3px;background:rgba(0,0,0,.7);color:#fff;font-size:8px;padding:1px 4px;border-radius:3px;pointer-events:none">hidden</div>':''}
    <div class="steam-choice" style="display:none;position:absolute;inset:0;flex-direction:column;gap:5px;align-items:center;justify-content:center;background:rgba(0,0,0,0.80);padding:4px">
      <button onclick="event.stopPropagation();launchGame('${uid}','user')" style="background:var(--accent3);color:#000;border:none;border-radius:4px;padding:5px 8px;font-size:10px;font-weight:700;cursor:pointer;width:84%">▶ YOU PLAY</button>
      <button onclick="event.stopPropagation();launchGame('${uid}','s32')" style="background:var(--accent);color:#000;border:none;border-radius:4px;padding:5px 8px;font-size:10px;font-weight:700;cursor:pointer;width:84%">🤖 S32 PLAYS</button>
    </div>
  </div>`;
}
function steamPick(tile){
  document.querySelectorAll('.steam-tile .steam-choice').forEach(c=>{ if(c.parentElement!==tile) c.style.display='none'; });
  const ch=tile.querySelector('.steam-choice');
  ch.style.display = (ch.style.display==='flex') ? 'none' : 'flex';
}
function steamDragTile(ev, appid, fromCat){ _drag={kind:'tile',appid:appid,fromCat:fromCat||''}; const b=document.getElementById('steam-body'); if(b) b.classList.add('dnd'); try{ev.dataTransfer.effectAllowed='move';ev.dataTransfer.setData('text/plain','t');}catch(e){} }
function steamDragCat(ev, name){ _drag={kind:'cat',name:name}; const b=document.getElementById('steam-body'); if(b) b.classList.add('dnd','dnd-cat'); try{ev.dataTransfer.effectAllowed='move';ev.dataTransfer.setData('text/plain','c');}catch(e){} }
function steamDragEnd(){ const b=document.getElementById('steam-body'); if(b) b.classList.remove('dnd','dnd-cat'); document.querySelectorAll('.over-body,.over-head,.over-tile').forEach(e=>e.classList.remove('over-body','over-head','over-tile')); _drag=null; }
function steamCatOver(ev){ ev.preventDefault(); ev.stopPropagation(); ev.currentTarget.classList.add('over-body'); }
function steamCatOut(ev){ ev.stopPropagation(); if(ev.currentTarget) ev.currentTarget.classList.remove('over-body'); }
function steamHeadOver(ev){ ev.preventDefault(); ev.stopPropagation(); ev.currentTarget.classList.add('over-head'); const cat=ev.currentTarget.closest('.steam-cat'); if(cat) cat.classList.remove('over-body'); }
function steamHeadOut(ev){ if(ev.currentTarget) ev.currentTarget.classList.remove('over-head'); }
function steamTileOver(ev){ ev.preventDefault(); ev.stopPropagation(); ev.currentTarget.classList.add('over-tile'); }
function steamTileOut(ev){ if(ev.currentTarget) ev.currentTarget.classList.remove('over-tile'); }
async function steamCatDrop(ev, category){          // drop on a folder's BODY
  ev.preventDefault(); ev.stopPropagation();
  if(!_drag) return;
  if(_drag.kind==='cat'){ const dn=_drag.name; _drag=null; if(dn!==category) await steamCatOp({op:'reorder_category', name:dn, category:category}); return; }   // reorder before this folder
  if(_drag.kind==='tile'){ const a=_drag.appid; _drag=null; await steamAssign(a, category, null); }      // assign into folder
}
async function steamHeaderDrop(ev, category){       // drop ON a folder's HEADER
  ev.preventDefault(); ev.stopPropagation();
  if(!_drag) return;
  if(_drag.kind==='cat'){ const dn=_drag.name; _drag=null; if(dn!==category) await steamCatOp({op:'set_parent', name:dn, parent:category}); return; }   // NEST inside
  if(_drag.kind==='tile'){ const a=_drag.appid; _drag=null; await steamAssign(a, category, null); }
}
async function steamGenreDrop(ev){                   // drop into the genre/library area
  ev.preventDefault();
  if(!_drag || _drag.kind!=='tile'){ _drag=null; return; }
  const a=_drag.appid, from=_drag.fromCat; _drag=null;
  let arr=_uncatOrder.filter(x=>x!==a); arr.push(a);
  await steamUncat(a, from, arr);
}
async function steamTileDrop(ev, targetAppid, targetCat){
  ev.preventDefault(); ev.stopPropagation();
  if(!_drag || _drag.kind!=='tile'){ _drag=null; return; }
  const a=_drag.appid, from=_drag.fromCat; _drag=null;
  if(targetCat){
    let arr=(_catGames[targetCat]||[]).filter(x=>x!==a);
    let idx=arr.indexOf(targetAppid); if(idx<0) idx=arr.length;
    await steamAssign(a, targetCat, idx);            // move into / reorder within folder
  } else {
    let arr=_uncatOrder.filter(x=>x!==a);            // reorder among uncategorized
    let i=arr.indexOf(targetAppid); if(i<0) i=arr.length;
    arr.splice(i,0,a);
    await steamUncat(a, from, arr);
  }
}
async function steamUncat(appid, fromCat, newOrder){ // ensure uncategorized + apply order, one reload
  try{
    if(fromCat) await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'assign', appid:appid, category:null})});
    await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op:'set_order', order:newOrder})});
  }catch(e){}
  loadSteamGames();
}
async function steamAssign(appid, category, index){
  try{
    await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({op:'assign', appid:appid, category:category||null, index:index})});
    loadSteamGames();
  }catch(e){}
}
async function steamCatOp(payload){
  try{
    const d=await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(payload)}).then(r=>r.json());
    if(d && d.error) alert(d.error);
    loadSteamGames();
  }catch(e){}
}
async function newSteamCategory(parent){
  const label = parent ? ('New subcategory under "'+parent+'":') : 'Name the new category:';
  const name=(prompt(label)||'').trim(); if(!name) return;
  try{
    const d=await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({op:'create', name:name, parent:parent||''})}).then(r=>r.json());
    if(d && d.error){ alert(d.error); return; }
    loadSteamGames();
  }catch(e){}
}
async function renameSteamCategory(name){
  const nn=(prompt('Rename category:', name)||'').trim(); if(!nn || nn===name) return;
  try{
    const d=await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({op:'rename', name:name, new_name:nn})}).then(r=>r.json());
    if(d && d.error){ alert(d.error); return; }
    loadSteamGames();
  }catch(e){}
}
async function deleteSteamCategory(name){
  if(!confirm('Delete the category "'+name+'"? The games in it return to their genre — nothing is uninstalled.')) return;
  try{
    await fetch('/api/steam/category',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({op:'delete', name:name})});
    loadSteamGames();
  }catch(e){}
}
async function steamHide(appid, hidden){
  try{
    await fetch('/api/steam/hide',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({appid:appid, hidden:hidden})});
    loadSteamGames();
  }catch(e){}
}
function closeSteamCtx(){ const m=document.getElementById('steam-ctx'); if(m) m.remove(); }
function _steamMenu(ev, items){
  closeSteamCtx();
  const m=document.createElement('div'); m.id='steam-ctx';
  m.style.cssText='position:fixed;z-index:1200;background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:4px;font-size:11px;box-shadow:0 4px 16px rgba(0,0,0,.6);min-width:150px';
  m.style.left=Math.min(ev.clientX, window.innerWidth-170)+'px';
  m.style.top=Math.min(ev.clientY, window.innerHeight-20-items.length*30)+'px';
  items.forEach(it=>{ const b=document.createElement('div'); b.textContent=it[0];
    b.style.cssText='padding:6px 10px;cursor:pointer;border-radius:4px;color:'+(it[2]||'var(--fg)');
    b.onmouseover=()=>b.style.background='var(--bg3)'; b.onmouseout=()=>b.style.background='transparent';
    b.onclick=(e)=>{ e.stopPropagation(); closeSteamCtx(); it[1](); }; m.appendChild(b); });
  document.body.appendChild(m);
  setTimeout(()=>document.addEventListener('click', closeSteamCtx, {once:true}), 0);
}
function steamContext(ev, appid, hidden, inCat, hasCustom){
  ev.preventDefault();
  const items=[[hidden?'Unhide game':'Hide game', ()=>steamHide(appid, hidden?false:true)]];
  if(inCat) items.push(['Remove from category', ()=>steamAssign(appid,'',null)]);
  items.push(['Set icon from file…', ()=>setGameIconFromFile(appid)]);
  items.push(['Capture screen as icon', ()=>setGameIconFromScreen(appid)]);
  if(hasCustom) items.push(['Remove custom icon', ()=>clearGameIcon(appid), 'var(--accent2)']);
  _steamMenu(ev, items);
}
async function setGameIconFromFile(uid){
  closeSteamCtx();
  try{
    const r = await fetch('/browse_file',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filter:'Images (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp;*.ico)|*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp;*.ico|All files (*.*)|*.*', initial_dir:''})});
    const d = await r.json();
    if(!d.path){ return; }          // cancelled
    const s = await fetch('/api/steam/icon/set',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uid:uid, path:d.path})}).then(x=>x.json());
    if(s.error){ alert(s.error); return; }
    _iconBust[uid] = Date.now();
    loadSteamGames();
  }catch(e){ alert('Set icon failed: '+e.message); }
}
async function setGameIconFromScreen(uid){
  closeSteamCtx();
  try{
    const s = await fetch('/api/steam/icon/set',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uid:uid, source:'screen'})}).then(x=>x.json());
    if(s.error){ alert(s.error); return; }
    _iconBust[uid] = Date.now();
    loadSteamGames();
  }catch(e){ alert('Capture failed: '+e.message); }
}
async function clearGameIcon(uid){
  closeSteamCtx();
  try{
    await fetch('/api/steam/icon/clear',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({uid:uid})});
    _iconBust[uid] = Date.now();
    loadSteamGames();
  }catch(e){}
}
function steamFolderContext(ev, name, hidden, nested){
  ev.preventDefault();
  const items=[[hidden?'Unhide folder':'Hide folder', ()=>steamCatOp({op:'folder_hidden', name:name, hidden:hidden?false:true})]];
  if(nested) items.push(['Move to top level', ()=>steamCatOp({op:'set_parent', name:name, parent:''})]);
  items.push(['Add subcategory', ()=>newSteamCategory(name)]);
  items.push(['Name color…', ()=>steamColorPicker(ev, name, '')]);
  items.push(['Rename folder', ()=>renameSteamCategory(name)]);
  items.push(['Delete folder', ()=>deleteSteamCategory(name), 'var(--accent2)']);
  _steamMenu(ev, items);
}
function _hsvToRgb(h,s,v){
  const c=v*s, x=c*(1-Math.abs((h/60)%2-1)), mm=v-c; let r,g,b;
  if(h<60){r=c;g=x;b=0;}else if(h<120){r=x;g=c;b=0;}else if(h<180){r=0;g=c;b=x;}
  else if(h<240){r=0;g=x;b=c;}else if(h<300){r=x;g=0;b=c;}else{r=c;g=0;b=x;}
  return [Math.round((r+mm)*255),Math.round((g+mm)*255),Math.round((b+mm)*255)];
}
function _rgbToHex(r,g,b){ return '#'+[r,g,b].map(x=>x.toString(16).padStart(2,'0')).join(''); }
function _hexToHsv(hex){
  let r=parseInt(hex.substr(1,2),16)/255,g=parseInt(hex.substr(3,2),16)/255,b=parseInt(hex.substr(5,2),16)/255;
  const mx=Math.max(r,g,b),mn=Math.min(r,g,b),df=mx-mn; let h=0;
  if(df){ if(mx===r)h=((g-b)/df)%6; else if(mx===g)h=(b-r)/df+2; else h=(r-g)/df+4; h*=60; if(h<0)h+=360; }
  return {h:h, s:(mx?df/mx:0), v:mx};
}
function steamColorPicker(ev, name, current){
  closeSteamCtx();
  const has = current && current[0]==='#' && current.length===7;
  const init = has ? _hexToHsv(current) : {h:140,s:0.85,v:0.95};
  const m=document.createElement('div'); m.id='steam-ctx';
  m.onclick=(e)=>e.stopPropagation();                       // clicks inside never close the popup
  m.style.cssText='position:fixed;z-index:1200;background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:14px;box-shadow:0 6px 22px rgba(0,0,0,.7);width:200px;user-select:none';
  m.style.left=Math.min(ev.clientX, window.innerWidth-220)+'px';
  m.style.top=Math.min(ev.clientY, window.innerHeight-320)+'px';
  m.innerHTML=`<div style="font-size:10px;color:var(--muted);letter-spacing:2px;margin-bottom:10px">NAME COLOR</div>
    <canvas id="cc-wheel" width="170" height="170" style="cursor:crosshair;display:block;margin:0 auto"></canvas>
    <div style="display:flex;align-items:center;gap:6px;margin:10px 0 6px">
      <span style="font-size:9px;color:var(--muted)">DARK</span>
      <input type="range" id="cc-val" min="2" max="100" value="${Math.round(init.v*100)}" style="flex:1;--k:var(--accent3);cursor:pointer">
      <span style="font-size:9px;color:var(--muted)">LIGHT</span>
    </div>
    <div id="cc-prev" style="padding:7px;text-align:center;font-size:11px;letter-spacing:2px;font-weight:700;background:var(--bg3);border-radius:4px;margin-bottom:11px">${esc(name).toUpperCase()}</div>
    <div style="display:flex;gap:6px">
      <button id="cc-save" style="flex:1;background:var(--bg3);color:var(--accent3);border:1px solid var(--accent3);border-radius:4px;padding:7px;font-size:10px;cursor:pointer;font-weight:700">Save color</button>
      <button id="cc-def" style="flex:1;background:var(--bg3);color:var(--muted);border:1px solid var(--border);border-radius:4px;padding:7px;font-size:10px;cursor:pointer">Default</button>
    </div>`;
  document.body.appendChild(m);
  const cv=m.querySelector('#cc-wheel'), ctx=cv.getContext('2d'), prev=m.querySelector('#cc-prev'), val=m.querySelector('#cc-val');
  const R=84, cx=85, cy=85;
  let hue=init.h, sat=init.s;
  function curHex(){ const [r,g,b]=_hsvToRgb(hue,sat,val.value/100); return _rgbToHex(r,g,b); }
  function draw(){
    const vv=val.value/100, img=ctx.createImageData(cv.width,cv.height);
    for(let y=0;y<cv.height;y++){ for(let x=0;x<cv.width;x++){
      const dx=x-cx, dy=y-cy, dist=Math.sqrt(dx*dx+dy*dy), i=(y*cv.width+x)*4;
      if(dist<=R){ let h=(Math.atan2(dy,dx)*180/Math.PI+360)%360, s=dist/R;
        const c=_hsvToRgb(h,s,vv); img.data[i]=c[0]; img.data[i+1]=c[1]; img.data[i+2]=c[2]; img.data[i+3]=255; }
      else { img.data[i+3]=0; }
    }}
    ctx.putImageData(img,0,0);
    const mr=sat*R, mx=cx+Math.cos(hue*Math.PI/180)*mr, my=cy+Math.sin(hue*Math.PI/180)*mr;
    ctx.beginPath(); ctx.arc(mx,my,5,0,7); ctx.lineWidth=2; ctx.strokeStyle='#fff'; ctx.stroke();
    ctx.beginPath(); ctx.arc(mx,my,5,0,7); ctx.lineWidth=1; ctx.strokeStyle='#000'; ctx.stroke();
    prev.style.color=curHex();
  }
  function pick(e){
    const r=cv.getBoundingClientRect(), x=e.clientX-r.left, y=e.clientY-r.top, dx=x-cx, dy=y-cy;
    const dist=Math.min(Math.sqrt(dx*dx+dy*dy),R);
    hue=(Math.atan2(dy,dx)*180/Math.PI+360)%360; sat=dist/R; draw();
  }
  cv.onmousedown=(e)=>{ e.preventDefault(); pick(e);
    const mv=(ev2)=>pick(ev2), up=()=>{document.removeEventListener('mousemove',mv);document.removeEventListener('mouseup',up);};
    document.addEventListener('mousemove',mv); document.addEventListener('mouseup',up); };
  val.oninput=draw; draw();
  m.querySelector('#cc-save').onclick=async()=>{ const v=curHex(); closeSteamCtx(); await steamCatOp({op:'set_color', name:name, color:v}); };
  m.querySelector('#cc-def').onclick=async()=>{ closeSteamCtx(); await steamCatOp({op:'set_color', name:name, color:''}); };
  setTimeout(()=>document.addEventListener('click', closeSteamCtx, {once:true}), 0);
}
async function launchGame(appid, who){
  try{
    const d=await fetch('/api/steam/launch',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({appid:appid, who:who})}).then(r=>r.json());
    if(d.error){ alert(d.error); return; }
    // dissolve the launcher — the game is taking over
    const pop=document.getElementById('steam-popup'); if(pop) pop.style.display='none';
    // If IT's playing, kick off its perceive→act loop through the normal chat path
    if(who==='s32' && d.name){
      const inp=document.getElementById('msg-in');
      if(inp && !(typeof busy!=='undefined' && busy)){
        inp.value = `You've been launched into "${d.name}" to play it yourself. `+
          `Take a screenshot with see_screen first to see what's on screen (it may still be loading). `+
          `Then act with game_input — press/hold keys (w,a,s,d,space,e,esc…), move/move_to/click the mouse. `+
          `To click something specific: first move_to its pixel position from the screenshot, THEN click (don't click without positioning the cursor first). `+
          `Your input only works while my cursor is over the game; if game_input says the cursor is off the game `+
          `or the game window isn't found, just wait and look again with see_screen. `+
          `Go slow: look, make one small move, look again. Start by taking a screenshot now.`;
        try{ resize(inp); }catch(e){}
        send();
      }
    }
  }catch(e){ alert('Launch failed: '+e.message); }
}
async function restoreBrain(){
  try{ await fetch('/api/steam/restore',{method:'POST'}); loadSteamGames(); }catch(e){}
}

async function previewVoice(){
  const v = document.getElementById('voice-select').value;
  if(!v) return;
  await fetch('/api/voice/preview', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({voice:v})});
}

async function setVoice(){
  const v = document.getElementById('voice-select').value;
  if(!v) return;
  const r = await fetch('/api/voice/set', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({voice:v})});
  const d = await r.json();
  if(d.ok) document.getElementById('current-voice-label').textContent = v;
}

async function loadMemoryFileList(){
  try {
    const r = await fetch('/api/memory/files');
    if(!r.ok) return;
    const d = await r.json();
    const el = document.getElementById('memory-file-list');
    if(el) el.innerHTML = (d.files || []).map(f=>'📄 '+f).join('<br>');
  } catch(e){}
}

async function showFactoryReset(){
  const pop=document.getElementById('factory-reset-popup');
  const list=document.getElementById('fr-list');
  document.getElementById('fr-confirm').value='';
  document.getElementById('fr-go').disabled=true;
  document.getElementById('fr-status').textContent='';
  list.textContent='checking…';
  pop.style.display='block';
  try{
    const r=await fetch('/api/factory_reset',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({preview:true})}).then(x=>x.json());
    list.innerHTML=(r.groups||[]).map(g=>'• '+esc(g.label)+' <span style="color:var(--muted)">('+g.files+' file'+(g.files===1?'':'s')+')</span>').join('<br>')||'Nothing to erase — it\'s already clean.';
    document.getElementById('fr-keeps').textContent='Kept: '+(r.keeps||'');
  }catch(e){ list.textContent='(could not list — the reset still covers everything above)'; }
}
async function confirmFactoryReset(btn){
  const word=document.getElementById('fr-confirm').value.trim();
  btn.disabled=true;
  const st=document.getElementById('fr-status');
  st.textContent='Erasing…';
  try{
    const r=await fetch('/api/factory_reset',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({confirm:word})}).then(x=>x.json());
    if(r.error){ st.textContent=r.error; btn.disabled=false; return; }
    st.innerHTML='&#10003; Erased '+r.removed+' file(s). System 32 is restarting fresh &mdash; this page reloads when it\'s back.'
      +((r.locked&&r.locked.length)?'<br><span style="color:#e0a055">'+r.locked.length+' item(s) were in use; run Factory Reset again after restarting.</span>':'');
  }catch(e){ st.textContent='System 32 is restarting fresh — this page reloads when it\'s back.'; }
  _reloadWhenBack();
}
function _reloadWhenBack(){
  // Wait for the old process to go away, then reload as soon as the new one answers —
  // this tab becomes the fresh session instead of a second window opening.
  let wentDown=false;
  const tick=async()=>{
    try{ const r=await fetch('/status',{cache:'no-store'}); if(r.ok && wentDown){ location.reload(); return; } }
    catch(e){ wentDown=true; }
    setTimeout(tick, 2000);
  };
  setTimeout(tick, 2500);
}
async function showMemoryReset(){
  try {
    const r = await fetch('/api/memory/files');
    const d = await r.json();
    document.getElementById('reset-file-list').innerHTML = (d.files||[]).map(f=>'📄 '+f).join('<br>');
    document.getElementById('memory-reset-popup').style.display = 'block';
  } catch(e){}
}

async function confirmMemoryReset(){
  try {
    await fetch('/api/memory/reset', {method:'POST'});
    document.getElementById('memory-reset-popup').style.display = 'none';
    const el = document.getElementById('memory-file-list');
    if(el) el.innerHTML = '<span style="color:var(--accent3)">Reset to a blank slate. Your saved states are untouched — load one anytime to restore a character.</span>';
    try{ refreshCurrentState(); loadGameplay(); }catch(e){}
  } catch(e){ alert('Reset failed: ' + e.message); }
}

async function saveState(btn){
  const s = document.getElementById('state-status');
  if(btn) btn.disabled = true;
  if(s) s.textContent = 'Saving state...';
  try {
    const nameEl = document.getElementById('savestate-name');
    const name = nameEl ? nameEl.value.trim() : '';
    const r = await fetch('/api/state/save', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name})});
    const d = await r.json();
    if(s) s.textContent = d.error ? ('Save failed: ' + d.error)
      : ((d.overwritten ? 'Overwrote: ' : 'Saved: ') + d.filename);
    if(!d.error && nameEl) nameEl.value = '';
    if(!d.error) refreshCurrentState();
  } catch(e){ if(s) s.textContent = 'Save failed: ' + e.message; }
  if(btn) btn.disabled = false;
}

async function exportTraining(btn){
  const s = document.getElementById('state-status');
  if(btn) btn.disabled = true;
  if(s) s.textContent = 'Exporting training data...';
  try {
    const r = await fetch('/api/training/export', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({format:'unsloth'})});
    const d = await r.json();
    if(s) s.textContent = d.error ? ('Export failed: ' + d.error)
      : ('✓ ' + d.sft + ' training sample(s) + ' + d.dpo + ' worked-vs-failed pair(s) from '
         + d.turns + ' turn(s) → ' + (d.path||'') + '  (open README.txt there to train)');
  } catch(e){ if(s) s.textContent = 'Export failed: ' + e.message; }
  if(btn) btn.disabled = false;
}

async function loadGameplay(){
  const list = document.getElementById('gameplay-list');
  const st = document.getElementById('gameplay-stats');
  if(!list) return;
  try{
    const d = await fetch('/api/gameplay/sessions').then(r=>r.json());
    const s = d.stats || {};
    if(st) st.textContent = `${s.sessions||0} sessions · ${s.good||0} good · ${s.total_steps||0} steps captured`;
    if(!(d.sessions||[]).length){ list.innerHTML='<span style="font-size:9px;color:var(--muted);opacity:.7">No gameplay captured yet — launch a game with S32 PLAYS.</span>'; return; }
    list.innerHTML = d.sessions.map(x=>{
      const lab = x.label;
      const gcol = lab==='good' ? 'var(--accent3)' : 'var(--bg3)';
      const gtxt = lab==='good' ? '#000' : 'var(--muted)';
      const bcol = lab==='bad' ? 'var(--accent2)' : 'var(--bg3)';
      const btxt = lab==='bad' ? '#fff' : 'var(--muted)';
      return `<div style="display:flex;align-items:center;gap:6px;font-size:10px">
        <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--fg)" title="${esc(x.id)}">${esc(x.name||x.appid||x.id)} <span style="opacity:.6">· ${x.steps} steps</span></span>
        <button onclick="labelGameplay('${x.id}','good')" style="background:${gcol};color:${gtxt};border:1px solid var(--accent3);border-radius:3px;padding:2px 6px;font-size:9px;cursor:pointer;font-weight:700">good</button>
        <button onclick="labelGameplay('${x.id}','bad')" style="background:${bcol};color:${btxt};border:1px solid var(--accent2);border-radius:3px;padding:2px 6px;font-size:9px;cursor:pointer;font-weight:700">bad</button>
      </div>`;
    }).join('');
  }catch(e){ if(st) st.textContent='Gameplay list failed: '+e.message; }
}
async function labelGameplay(session, label){
  try{
    await fetch('/api/gameplay/label',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session:session, label:label})});
    loadGameplay();
  }catch(e){}
}
async function exportGameplay(scope, btn){
  const s = document.getElementById('gameplay-status');
  if(btn) btn.disabled=true;
  if(s) s.textContent = 'Exporting '+scope+' gameplay…';
  try{
    const d = await fetch('/api/gameplay/export',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({scope:scope})}).then(r=>r.json());
    if(s) s.textContent = d.error ? ('✗ '+d.error)
      : ('✓ '+d.count+' steps from '+d.sessions+' session(s) → '+(d.path||''));
  }catch(e){ if(s) s.textContent='Export failed: '+e.message; }
  if(btn) btn.disabled=false;
}
async function resetGameplay(btn){
  const s = document.getElementById('gameplay-status');
  if(!confirm('Delete ALL captured gameplay training data — every session, frame and export? This cannot be undone unless it was saved inside a snapshot.')) return;
  if(btn) btn.disabled=true;
  if(s) s.textContent = 'Resetting gameplay training data…';
  try{
    const d = await fetch('/api/gameplay/reset',{method:'POST'}).then(r=>r.json());
    if(s) s.textContent = d.error ? ('✗ '+d.error) : ('✓ cleared ('+(d.removed||0)+' item(s))');
    loadGameplay();
  }catch(e){ if(s) s.textContent='Reset failed: '+e.message; }
  if(btn) btn.disabled=false;
}

async function showLoadState(){
  const list = document.getElementById('snapshot-list');
  list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center">Loading...</div>';
  document.getElementById('load-state-popup').style.display = 'block';
  try {
    const r = await fetch('/api/state/list', {method:'POST'});
    const d = await r.json();
    const snaps = d.snapshots || [];
    if(!snaps.length){
      list.innerHTML = '<div style="color:var(--muted);font-size:11px;text-align:center">No saved snapshots yet.</div>';
      return;
    }
    list.innerHTML = '';
    snaps.forEach(name=>{
      const b = document.createElement('button');
      b.textContent = name;
      b.style.cssText = 'background:var(--bg3);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:8px;font-size:11px;cursor:pointer;text-align:left;font-family:var(--mono)';
      b.onclick = ()=>confirmLoadState(name);
      list.appendChild(b);
    });
  } catch(e){ list.innerHTML = '<div style="color:#e05555;font-size:11px">Failed to list: '+e.message+'</div>'; }
}

async function confirmLoadState(name){
  if(!confirm('Restore "'+name+'"? This swaps in the snapshot\'s memory, identity, profiles, favorites, game library and gameplay training data — replacing what\'s loaded now.')) return;
  const s = document.getElementById('state-status');
  try {
    const r = await fetch('/api/state/load', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filename:name})});
    const d = await r.json();
    document.getElementById('load-state-popup').style.display = 'none';
    if(s) s.textContent = d.error ? ('Load failed: ' + d.error) : ('Restored: ' + name + ' (' + (d.files||0) + ' files)');
    if(!d.error) refreshCurrentState();
  } catch(e){ if(s) s.textContent = 'Load failed: ' + e.message; }
}

let _browsingFile = false;
async function browseModelFile(){
  if(_modelSwitching) return;
  _browsingFile = true;
  document.getElementById('model-status').textContent = 'Opening file picker...';
  try{
    const ctrl = new AbortController();
    const tid = setTimeout(()=>ctrl.abort(), 115000);
    const r = await fetch('/browse_file',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({filter:'GGUF files (*.gguf)|*.gguf|All files (*.*)|*.*', initial_dir:'I:\\'}),
      signal: ctrl.signal});
    clearTimeout(tid);
    const d = await r.json();
    _browsingFile = false;
    if(d.cancelled || !d.path){ document.getElementById('model-status').textContent = 'Cancelled.'; return; }
    _selectedModelPath = d.path;
    document.getElementById('model-status').textContent = d.name + ' (' + d.size_gb + 'GB) — ready to load';
    const btn = document.getElementById('model-load-btn');
    if(btn){ btn.disabled = false; btn.style.opacity = '1'; }
    // Load saved settings for this model
    try{
      const sr = await fetch('/get_model_settings',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({model_path:d.path})});
      const sd = await sr.json();
      if(sd.ctx_size)    document.getElementById('model-ctx').value        = sd.ctx_size;
      if(sd.gpu_layers !== undefined) document.getElementById('model-gpu-layers').value = sd.gpu_layers;
      if(sd.threads)     document.getElementById('model-threads').value    = sd.threads;
      if(sd.batch_size)  document.getElementById('model-batch').value      = sd.batch_size;
      if(sd.kv_cache)    document.getElementById('model-kv').value          = sd.kv_cache;
      document.getElementById('model-flash').value = sd.flash_attn ? '1' : '0';
    } catch(e){}
    applyModelCaps(d);   // cap ctx/gpu/threads to what THIS model + CPU can do (clamps saved values too)
  } catch(e){
    _browsingFile = false;
    document.getElementById('model-status').textContent = 'Error: ' + e.message;
  }
}
function _clampInput(id, maxv){
  const el = document.getElementById(id);
  if(!el || !maxv || maxv <= 0) return;
  el.max = maxv;
  if(parseFloat(el.value) > maxv) el.value = maxv;
}
function _capLabel(id, base, maxv){
  const el = document.getElementById(id);
  if(el) el.textContent = base + ((maxv && maxv > 0) ? ` \u2264${maxv}` : '');
}
function onKvChange(){
  const kv = (document.getElementById('model-kv')||{}).value;
  const fa = document.getElementById('model-flash');
  if(kv && kv !== 'f16' && fa){ fa.value = '1'; }   // quantized KV requires flash attention
}
function applyModelCaps(d){
  const caps = (d && d.caps) || {};
  const cpu  = d && d.cpu_threads;
  // CONTEXT capped at the model's trained context length (GGUF context_length)
  _clampInput('model-ctx', caps.max_ctx);
  _capLabel('model-ctx-lbl', 'CONTEXT', caps.max_ctx);
  // GPU LAYERS capped at the model's actual layer count (GGUF block_count)
  _clampInput('model-gpu-layers', caps.n_layers);
  _capLabel('model-gpu-lbl', 'GPU LAYERS', caps.n_layers);
  // THREADS capped at the CPU's logical core count
  _clampInput('model-threads', cpu);
  _capLabel('model-threads-lbl', 'THREADS', cpu);
}
async function loadSelectedModel(){
  if(!_selectedModelPath || _modelSwitching) return;
  _modelSwitching = true;
  const btn = document.getElementById('model-load-btn');
  if(btn){ btn.disabled = true; btn.style.opacity = '0.4'; }
  const fname = _selectedModelPath.split('\\').pop().split('/').pop();
  document.getElementById('model-status').textContent = 'Switching to ' + fname + '...';
  _lmWasReady = false;
  const stxt = document.getElementById('status-txt');
  if(stxt) stxt.textContent = '⬡ system 32 — switching model...';
  try{
    const ctx       = parseInt(document.getElementById('model-ctx').value)        || 131072;
    const gpuLayers = parseInt(document.getElementById('model-gpu-layers').value) || 20;
    const threads   = parseInt(document.getElementById('model-threads').value)    || 14;
    const batch     = parseInt(document.getElementById('model-batch').value)      || 512;
    const kv        = (document.getElementById('model-kv')||{}).value    || 'f16';
    const flash     = ((document.getElementById('model-flash')||{}).value || '0') === '1';
    // Save settings for this model first, then switch
    const settings = {ctx_size:ctx, gpu_layers:gpuLayers, threads:threads, batch_size:batch, kv_cache:kv, flash_attn:flash};
    await fetch('/save_model_settings',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model_path:_selectedModelPath, settings:settings})}).catch(()=>{});
    const r = await fetch('/set_model',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({model_id:_selectedModelPath, context_length:ctx, gpu_layers:gpuLayers,
        threads:threads, batch_size:batch, kv_cache:kv, flash_attn:flash})});
    const d = await r.json();
    if(d.error){
      document.getElementById('model-status').textContent = 'Error: ' + d.error;
      _modelSwitching = false;
      return;
    }
    window._currentModelPath = _selectedModelPath;
    document.getElementById('model-status').textContent = 'Switching — ' + fname;
    setTimeout(()=>{ closeModelPicker(); _modelSwitching = false; }, 2000);
  } catch(e){
    document.getElementById('model-status').textContent = 'Error: ' + e.message;
    _modelSwitching = false;
  }
}
// Close picker if clicking outside
document.addEventListener('click', function(e){
  const p   = document.getElementById('model-picker');
  const btn = document.getElementById('settings-btn');
  if(_browsingFile) return;
  if(p && p.style.display !== 'none' && !p.contains(e.target) && btn && !btn.contains(e.target)){
    closeModelPicker();
  }
});

// ===== Chat Customizations =====
let _uicMode = 'color';
let _uicImg  = null;   // background image as a data URL (travels inside ui_prefs.json)
function _uicCollect(){
  return {
    ai_bub:   document.getElementById('uic-ai').value,
    user_bub: document.getElementById('uic-user').value,
    bg_mode:  _uicMode,
    bg_color: document.getElementById('uic-bg').value,
    bg_image: _uicImg,
    ai_accent:   document.getElementById('uic-ai-accent').value,
    user_accent: document.getElementById('uic-user-accent').value,
    show_thoughts: document.getElementById('uic-thoughts').checked
  };
}
function applyUIPrefs(p){
  const r = document.documentElement.style;
  if(!p) return;
  document.body.classList.toggle('hide-thoughts', p.show_thoughts === false);
  if(p.ai_bub)   r.setProperty('--ai-bub', p.ai_bub);     else r.removeProperty('--ai-bub');
  if(p.user_bub) r.setProperty('--user-bub', p.user_bub); else r.removeProperty('--user-bub');
  if(p.ai_accent)   r.setProperty('--ai-accent', p.ai_accent);     else r.removeProperty('--ai-accent');
  if(p.user_accent) r.setProperty('--user-accent', p.user_accent); else r.removeProperty('--user-accent');
  if(p.bg_mode === 'image' && p.bg_image){
    r.setProperty('--chat-bg-image', 'url(' + p.bg_image + ')');
    if(p.bg_color) r.setProperty('--chat-bg', p.bg_color);
  } else {
    r.removeProperty('--chat-bg-image');
    if(p.bg_color) r.setProperty('--chat-bg', p.bg_color); else r.removeProperty('--chat-bg');
  }
}
function uicLive(){ applyUIPrefs(_uicCollect()); }
function uicBgMode(m){
  _uicMode = m;
  document.getElementById('uic-bg-color-row').style.display = (m==='color')?'block':'none';
  document.getElementById('uic-bg-image-row').style.display = (m==='image')?'block':'none';
  const cb=document.getElementById('uic-bgmode-color'), ib=document.getElementById('uic-bgmode-image');
  const on  = el=>{el.style.background='var(--accent)';el.style.color='#000';el.style.border='none';el.style.fontWeight='700';};
  const off = el=>{el.style.background='var(--bg3)';el.style.color='var(--fg)';el.style.border='1px solid var(--border)';el.style.fontWeight='600';};
  if(m==='color'){ on(cb); off(ib); } else { on(ib); off(cb); }
  uicLive();
}
function uicBgFile(e){
  const f = e.target.files && e.target.files[0];
  if(!f) return;
  const rd = new FileReader();
  rd.onload = function(){ _uicImg = rd.result; uicLive(); };
  rd.readAsDataURL(f);
}
async function openUICustom(){
  try{ closeModelPicker(); }catch(_){}
  try{
    const r = await fetch('/get_ui_prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const p = await r.json();
    if(p && !p.error){
      if(p.ai_bub)   document.getElementById('uic-ai').value   = p.ai_bub;
      if(p.user_bub) document.getElementById('uic-user').value = p.user_bub;
      if(p.ai_accent)   document.getElementById('uic-ai-accent').value   = p.ai_accent;
      if(p.user_accent) document.getElementById('uic-user-accent').value = p.user_accent;
      if(p.bg_color) document.getElementById('uic-bg').value   = p.bg_color;
      document.getElementById('uic-thoughts').checked = (p.show_thoughts !== false);
      _uicImg = p.bg_image || null;
      uicBgMode(p.bg_mode || 'color');
    } else { uicBgMode('color'); }
  }catch(_){ uicBgMode('color'); }
  document.getElementById('uic-status').textContent = '';
  document.getElementById('ui-custom-overlay').style.display = 'flex';
}
function closeUICustom(){ document.getElementById('ui-custom-overlay').style.display = 'none'; }
async function saveUI(){
  const p = _uicCollect();
  const st = document.getElementById('uic-status'); st.textContent = 'Saving…';
  try{
    const r = await fetch('/save_ui_prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prefs:p})});
    const j = await r.json();
    st.textContent = j.error ? ('Error: '+j.error) : 'Saved \u2713';
    applyUIPrefs(p);
  }catch(e){ st.textContent = 'Error: '+e.message; }
}
async function resetUI(){
  const r = document.documentElement.style;
  ['--ai-bub','--user-bub','--ai-accent','--user-accent','--chat-bg','--chat-bg-image'].forEach(v=>r.removeProperty(v));
  _uicImg = null;
  document.getElementById('uic-ai').value   = '#0c1018';
  document.getElementById('uic-user').value = '#3a1620';
  document.getElementById('uic-ai-accent').value   = '#00d4ff';
  document.getElementById('uic-user-accent').value = '#ff4060';
  document.getElementById('uic-bg').value   = '#040608';
  document.getElementById('uic-thoughts').checked = true;
  document.body.classList.remove('hide-thoughts');
  const ff = document.getElementById('uic-bg-file'); if(ff) ff.value='';
  uicBgMode('color');
  const st = document.getElementById('uic-status'); st.textContent = 'Resetting…';
  try{
    const rr = await fetch('/save_ui_prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({prefs:null})});
    const j  = await rr.json();
    st.textContent = j.error ? ('Error: '+j.error) : 'Reset to default \u2713';
  }catch(e){ st.textContent = 'Error: '+e.message; }
}
// Apply any saved UI prefs on page load
(async function _loadUIPrefs(){
  try{
    const r = await fetch('/get_ui_prefs',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    const p = await r.json();
    if(p && !p.error) applyUIPrefs(p);
  }catch(_){}
})();

// Dataset panel
function openDs(name){
  if(name) document.getElementById('dsname').value=name;
  document.getElementById('ds-panel').classList.add('open');
}
function closeDs(){
  document.getElementById('ds-panel').classList.remove('open');
  batch=[]; document.getElementById('thumbs').innerHTML='';
  document.getElementById('qcount').textContent='0 images queued';
}
function onBatch(input){for(const f of input.files) addToQ(f); input.value=''}
function onDrop(e){e.preventDefault();document.getElementById('dropzone').classList.remove('over');for(const f of e.dataTransfer.files) if(f.type.startsWith('image/')) addToQ(f)}
function addToQ(file){
  const r=new FileReader();
  r.onload=e=>{
    batch.push({name:file.name,data:e.target.result.split(',')[1]});
    const img=document.createElement('img'); img.className='thumb'; img.src=e.target.result;
    document.getElementById('thumbs').appendChild(img);
    document.getElementById('qcount').textContent=batch.length+' images queued';
  };
  r.readAsDataURL(file);
}
async function uploadDs(){
  const name=document.getElementById('dsname').value.trim();
  if(!name){alert('Enter a dataset name.');return}
  if(!batch.length){alert('Add some images first.');return}
  try{
    const r=await fetch('/upload_dataset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({dataset:name,images:batch})});
    const d=await r.json();
    addMsg('sys',`⬡ uploaded ${d.saved.length} images to dataset: ${name}`);
    closeDs();
  } catch(e){addMsg('sys','⬡ upload error: '+e.message)}
}

// Image browser panel
let ibSelected = null; // {path, b64, mime, filename}

async function openIb(folderName){
  document.getElementById('ib-panel').classList.add('open');
  document.getElementById('ds-panel').classList.remove('open');
  ibSelected = null;
  document.getElementById('ib-attach-btn').classList.remove('show');
  document.getElementById('ib-grid').innerHTML = '';
  document.getElementById('ib-breadcrumb').textContent = '';
  document.getElementById('ib-status').textContent = 'Loading folders...';
  // Load folder list
  try{
    const r = await fetch('/browse_folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
    const d = await r.json();
    const folders = d.folders || [];
    const fl = document.getElementById('ib-folders');
    fl.innerHTML = '';
    folders.forEach(f=>{
      const b = document.createElement('button');
      b.className = 'ib-folder-btn' + (f.name===folderName?' active':'');
      b.textContent = '▸ ' + f.name + (f.count?' ('+f.count+')':'');
      b.onclick = ()=>ibLoadFolder(f.name, b);
      fl.appendChild(b);
    });
    document.getElementById('ib-status').textContent = folders.length ? 'Select a folder above.' : 'Nothing here yet — generations and downloads will show up here.';
    if(folderName) ibLoadFolder(folderName, document.querySelector('.ib-folder-btn.active'));
  } catch(e){
    document.getElementById('ib-status').textContent = 'Error loading folders: '+e.message;
  }
}

async function ibLoadFolder(folderName, btn){
  document.querySelectorAll('.ib-folder-btn').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  ibSelected = null;
  document.getElementById('ib-attach-btn').classList.remove('show');
  document.getElementById('ib-grid').innerHTML = '';
  document.getElementById('ib-status').textContent = 'Loading...';
  document.getElementById('ib-breadcrumb').textContent = '';
  try{
    const r = await fetch('/browse_folder',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({folder:folderName})});
    const d = await r.json();
    if(d.error){ document.getElementById('ib-status').textContent = d.error; return; }
    document.getElementById('ib-breadcrumb').textContent = d.path || '';
    const items = d.files || d.images || [];
    document.getElementById('ib-status').textContent = items.length ? `${items.length} file${items.length!==1?'s':''} - click to select` : 'No files found.';
    const grid = document.getElementById('ib-grid');
    grid.innerHTML = '';
    const _3dExts = new Set(['.glb','.gltf','.obj','.ply','.stl','.fbx','.usdz']);
    const _vidExts = new Set(['.mp4','.webm','.mov','.avi','.mkv']);
    const imgEls = [];
    items.forEach(item=>{
      const ext = item.name.slice(item.name.lastIndexOf('.')).toLowerCase();
      const title = item.name + (item.size?' ('+item.size+'KB)':'');
      if(_3dExts.has(ext)){
        // 3D - render a div with model-viewer
        const wrap = document.createElement('div');
        wrap.className = 'ib-thumb';
        wrap.title = title;
        wrap.dataset.path = item.path;
        wrap.style.cssText='overflow:hidden;position:relative';
        const mv = document.createElement('model-viewer');
        mv.setAttribute('src', `/img?path=${encodeURIComponent(item.path)}`);
        mv.setAttribute('auto-rotate','');
        mv.setAttribute('camera-controls','');
        mv.style.cssText='width:100%;height:100%;display:block';
        wrap.appendChild(mv);
        wrap.onclick = ()=>ibSelect(wrap, item.path, item.name);
        grid.appendChild(wrap);
      } else if(_vidExts.has(ext)){
        // Video - render a video element
        const v = document.createElement('video');
        v.className = 'ib-thumb';
        v.title = title;
        v.dataset.path = item.path;
        v.src = `/img?path=${encodeURIComponent(item.path)}`;
        v.muted = true;
        v.style.cssText='object-fit:cover';
        v.onclick = ()=>ibSelect(v, item.path, item.name);
        grid.appendChild(v);
      } else {
        // Image - lazy load thumbnail
        const el = document.createElement('img');
        el.className = 'ib-thumb';
        el.title = title;
        el.dataset.path = item.path;
        el.src = 'data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>';
        el.onclick = ()=>ibSelect(el, item.path, item.name);
        grid.appendChild(el);
        imgEls.push(el);
      }
    });
    ibLoadThumbs(imgEls, 0, 12);
  } catch(e){
    document.getElementById('ib-status').textContent = 'Error: '+e.message;
  }
}

async function ibLoadThumbs(els, start, batchSize){
  const end = Math.min(start+batchSize, els.length);
  for(let i=start;i<end;i++){
    const el = els[i];
    try{
      const r = await fetch('/load_image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:el.dataset.path})});
      const d = await r.json();
      if(d.b64) el.src = `data:${d.mime};base64,${d.b64}`;
    } catch(e){}
  }
  if(end < els.length) setTimeout(()=>ibLoadThumbs(els,end,batchSize), 0);
}

async function ibSelect(el, path, name){
  document.querySelectorAll('.ib-thumb').forEach(t=>t.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('ib-status').textContent = `Selected: ${name}`;
  const ext = path.slice(path.lastIndexOf('.')).toLowerCase();
  const _3dExts = new Set(['.glb','.gltf','.obj','.ply','.stl','.fbx','.usdz']);
  const _vidExts = new Set(['.mp4','.webm','.mov','.avi','.mkv']);
  if(_3dExts.has(ext)){
    ibSelected = {path, b64: null, mime: 'model/gltf-binary', filename: name, kind: '3d'};
    document.getElementById('ib-attach-btn').classList.add('show');
    return;
  }
  if(_vidExts.has(ext)){
    ibSelected = {path, b64: null, mime: 'video/mp4', filename: name, kind: 'video'};
    document.getElementById('ib-attach-btn').classList.add('show');
    return;
  }
  // Image - load b64
  try{
    const r = await fetch('/load_image',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});
    const d = await r.json();
    if(d.error){ document.getElementById('ib-status').textContent = 'Error loading: '+d.error; return; }
    ibSelected = {path, b64:d.b64, mime:d.mime, filename:d.filename, kind:'image'};
    document.getElementById('ib-attach-btn').classList.add('show');
  } catch(e){
    document.getElementById('ib-status').textContent = 'Load error: '+e.message;
  }
}

function ibAttach(){
  if(!ibSelected) return;
  if(ibSelected.kind === 'image'){
    attached = {b64: ibSelected.b64, name: ibSelected.filename, mime: ibSelected.mime, kind: 'image'};
    document.getElementById('prev-img').src = `data:${ibSelected.mime};base64,${ibSelected.b64}`;
    document.getElementById('prev-img').style.display = 'block';
    document.getElementById('prev-vid').style.display = 'none';
    document.getElementById('prev-icon').style.display = 'none';
  } else if(ibSelected.kind === 'video'){
    attached = {b64: null, name: ibSelected.filename, mime: ibSelected.mime, kind: 'video', path: ibSelected.path};
    document.getElementById('prev-vid').src = `/img?path=${encodeURIComponent(ibSelected.path)}`;
    document.getElementById('prev-vid').style.display = 'block';
    document.getElementById('prev-img').style.display = 'none';
    document.getElementById('prev-icon').style.display = 'none';
  } else {
    attached = {b64: null, name: ibSelected.filename, mime: ibSelected.mime, kind: ibSelected.kind, path: ibSelected.path};
    document.getElementById('prev-icon').textContent = '⬡';
    document.getElementById('prev-icon').style.display = 'inline';
    document.getElementById('prev-img').style.display = 'none';
    document.getElementById('prev-vid').style.display = 'none';
  }
  document.getElementById('prev-name').textContent = ibSelected.filename;
  document.getElementById('img-preview').style.display = 'flex';
  closeIb();
  document.getElementById('msg-in').focus();
}

function closeIb(){
  document.getElementById('ib-panel').classList.remove('open');
  ibSelected = null;
}

// Lightbox
function openLb(src){document.getElementById('lb-img').src=src;document.getElementById('lb').classList.add('open')}

// Clean shutdown when browser tab closes
window.addEventListener('beforeunload', function() {
  navigator.sendBeacon('/shutdown', '{}');
});
</script>
</body>
</html>"""

# ── Startup ───────────────────────────────────────────────────────────────────

def startup():
    log("[S32] Starting up...")
    # Detect hardware first - used for context length and workflow defaults
    get_hardware()
    # Initialize memory structure - create folders if they don't exist
    _ensure_memory_structure()
    # Privacy (2026-10-28): screen_frames\ is a rolling buffer of screenshots. Clear it
    # before capture starts, so a drive moved to another computer never carries the
    # last computer's screen along.
    try:
        _sf = os.path.join(REFERENCE_DIR, "screen_frames")
        if os.path.isdir(_sf):
            for _n in os.listdir(_sf):
                try:
                    os.remove(os.path.join(_sf, _n))
                except Exception:
                    pass
    except Exception:
        pass
    # Start ambient sensing loop — read-only system awareness + screen capture
    if _senses:
        try:
            _senses.start(REFERENCE_DIR, CONFIG, poll_interval=30)
            log("[S32] Senses started — ambient system awareness active.")
        except Exception as _se:
            log(f"[S32] Senses failed to start: {_se}")

    # Start voice system — it chooses its own voice
    if _voice:
        try:
            _voice.start(REFERENCE_DIR, _machine_name)
            log("[S32] Voice started.")
        except Exception as _ve:
            log(f"[S32] Voice failed to start: {_ve}")
    # Detect operator and machine (new computer = new person; no names hardcoded)
    operator, is_new_machine, creator_known = _detect_operator()
    log(f"[S32] Operator: {operator or '(not introduced yet)'} | New machine: {is_new_machine} | "
        f"Person folder: {PERSON_DIR}")
    if is_new_machine:
        state["startup_notification"] = _get_introduction_message()
    # Load and update identity on every boot
    _load_identity()
    _update_identity()
    # Start periodic session log writer - survives crashes
    _start_session_log_writer()
    # Start hourly memory compressor
    _start_memory_compressor()
    # Mirror the operator's own ComfyUI-tab generations into chat (Phase 5)
    start_comfy_output_watcher()
    # Catch training/model artifacts that bypass ComfyUI history (Phase 5b)
    start_comfy_fs_watcher()
    # Auto-grow the visual index from output images so visual_search stays current
    try:
        import s32_filesystem
        s32_filesystem.start_visual_indexer()
    except Exception as e:
        log(f"[S32] visual indexer start failed: {e}")
    # Network guardian — watch ports & connections, alert (read-only) (Phase 6)
    try:
        import s32_firewall
        s32_firewall.start()
    except Exception as _fwe:
        log(f"[S32] Network guardian failed to start: {_fwe}")
    # Iron Dome — baseline ports/processes over iterations (non-destructive learning)
    try:
        import s32_irondome
        s32_irondome.start()
    except Exception as _ide:
        log(f"[S32] Iron Dome baselining failed to start: {_ide}")
    # Airplane mode — make the firewall backstop match the saved switch (2026-10-31)
    try:
        import s32_config as _c, s32_irondome as _d
        _fw = _d.airplane_firewall(_c.airplane())
        if _c.airplane():
            log("[S32][AIRPLANE] airplane mode is ON — System 32 is offline"
                + ("" if _fw.get("ok") else " (firewall backstop needs administrator; System 32 itself is still offline)"))
        elif _fw.get("left"):
            state["startup_notification"] = (f"⚠ {len(_fw['left'])} airplane-mode firewall rule(s) are still active "
                                             f"(ComfyUI / the model can't reach the internet). Start System 32 as "
                                             f"administrator once to clear them.")
    except Exception as _ape:
        log(f"[S32] airplane mode check failed: {_ape}")
    # Update check — one anonymous look at the GitHub repo's newest release (2026-11-01)
    try:
        import s32_update
        s32_update.start()
    except Exception as _upe:
        log(f"[S32] Update check failed to start: {_upe}")
    # Outbound watcher — learns where each program connects; flags drift (2026-10-31)
    try:
        import s32_egress
        s32_egress.start()
    except Exception as _ege:
        log(f"[S32] Outbound watcher failed to start: {_ege}")
    # ComfyUI revive monitor — quietly bring ComfyUI back when no game runs but the model is up
    try:
        import s32_steam
        s32_steam.start_revive_monitor()
    except Exception as _rve:
        log(f"[S32] ComfyUI revive monitor failed to start: {_rve}")
    # Run once immediately to catch any entries already past 7 days
    threading.Thread(target=_compress_old_memories, daemon=True).start()
    # Rebuild memory index if it doesn't exist
    if not os.path.exists(MEMORY_INDEX):
        threading.Thread(target=_rebuild_memory_index, daemon=True).start()
    # Warn if video is using the image instance
    # Model loads FIRST — ComfyUI only starts after model is confirmed ready
    def _start_model_then_comfy():
        time.sleep(5)
        start_llama_server()

        # Wait for model to be ready before starting ComfyUI
        waited = 0
        while not state["lmstudio_ready"] and waited < 300:
            time.sleep(3)
            waited += 3

        if state.get("model_load_failed"):
            log("[S32] Model failed to load — skipping ComfyUI startup to conserve resources.")
            log("[S32] Use the model switcher to select a valid model, then restart.")
            return

        log("[S32] Model ready — starting ComfyUI...")
        start_comfy_image()

    threading.Thread(target=_start_model_then_comfy, daemon=True).start()

    # Index of this computer's files (all drives, read-only) for search_computer.
    try:
        import s32_findex
        s32_findex.start()
    except Exception as _fe:
        log(f"[S32][FIND] not available: {_fe}")

    # Private search engine (SearXNG) — installs itself on first boot, then just starts.
    try:
        import s32_searx
        threading.Thread(target=s32_searx.start, daemon=True).start()
    except Exception as _se:
        log(f"[S32][SEARX] not available: {_se}")

    # Monitor: shut down ComfyUI if model unloads
    def _comfy_model_monitor():
        import subprocess as _sp
        was_ready = False
        while True:
            time.sleep(15)
            if state["lmstudio_ready"] and not state.get("model_load_failed"):
                was_ready = True
            if was_ready and not state["lmstudio_ready"]:
                log("[S32] Model unloaded — shutting down ComfyUI to free resources.")
                try:
                    _sp.run(
                        ["taskkill", "/F", "/IM", "python.exe", "/FI",
                         f"WINDOWTITLE eq ComfyUI*"],
                        capture_output=True,
                        creationflags=_sp.CREATE_NO_WINDOW
                    )
                except Exception:
                    pass
                state["comfy_image_ready"] = False
                was_ready = False
    threading.Thread(target=_comfy_model_monitor, daemon=True).start()
    def open_browser():
        # Open a window only if no System 32 tab is already open. An open tab polls
        # /status every couple of seconds (and reloads itself after a factory reset or
        # restart), so a new window on top of it was the "two browser windows" bug.
        if not _PAGE_SEEN.wait(8):
            webbrowser.open(f"http://localhost:{CONFIG['web_port']}")
    threading.Thread(target=open_browser, daemon=True).start()
    def delayed_node_check():
        time.sleep(90)
        check_missing_nodes()
    threading.Thread(target=delayed_node_check, daemon=True).start()
    def health_monitor():
        """
        Relaunch ComfyUI if it goes down - but carefully.
        Requires 3 consecutive failed checks before acting.
        Won't relaunch during active generation or within 3 minutes of last relaunch.
        """
        time.sleep(60)  # give everything time to fully start
        img_failures = 0
        img_last_relaunch = 0
        RELAUNCH_COOLDOWN = 180
        FAILURE_THRESHOLD = 3

        while True:
            time.sleep(15)

            if state.get("job_status"):
                img_failures = 0
                continue

            now = time.time()

            if state["comfy_image_ready"]:
                if is_up(CONFIG["comfy_image_host"]):
                    img_failures = 0
                else:
                    img_failures += 1
                    if img_failures >= FAILURE_THRESHOLD:
                        if now - img_last_relaunch >= RELAUNCH_COOLDOWN:
                            log(f"[S32] ComfyUI (image) down for {img_failures} checks - relaunching...")
                            state["comfy_image_ready"] = False
                            img_failures = 0
                            img_last_relaunch = now
                            start_comfy_image()
                        else:
                            remaining = int(RELAUNCH_COOLDOWN - (now - img_last_relaunch))
                            log(f"[S32] ComfyUI (image) down but in cooldown - waiting {remaining}s")
    threading.Thread(target=health_monitor, daemon=True).start()
    log(f"[S32] Web UI at http://localhost:{CONFIG['web_port']}")
    if not os.path.isfile(os.path.join(_STATIC_DIR, "model-viewer.min.js")):
        log(f"[S32] ⚠ 3D previews are off: the 3D viewer ('static' folder) is missing in {os.path.dirname(_STATIC_DIR)} — setup downloads it on the next start")

def main():
    print("""
  ==========================================
        S Y S T E M   3 2
  ==========================================
""")
    def cleanup(signum=None, frame=None):
        print("\n[S32] Shutting down...")
        _write_session_log(final=True)
        _update_identity()
        _compress_old_memories()
        shutdown_llama_server()
        try:
            import s32_searx
            s32_searx.stop()
        except Exception:
            pass
        sys.exit(0)

    import atexit
    atexit.register(lambda: (_write_session_log(final=True), _update_identity()))
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT,  cleanup)

    threading.Thread(target=startup, daemon=True).start()
    # Threaded so a /cancel (or status poll) can be served while /chat is blocked
    # waiting on a generation. daemon_threads lets Ctrl+C exit without hanging.
    ThreadingHTTPServer.daemon_threads = True
    server = ThreadingHTTPServer(("localhost", CONFIG["web_port"]), Handler)
    print(f"[S32] Running at http://localhost:{CONFIG['web_port']}")
    print("[S32] Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        cleanup()

if __name__ == "__main__":
    main()
