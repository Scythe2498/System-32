# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
senses.py — System 32's ambient awareness layer.

Runs as a background thread. Polls system state on a quiet timer and writes
a structured snapshot to system_state.json in the reference directory.

It sees:
  - Foreground window / active application
  - Running processes (names only)
  - Recently modified files across watched drives
  - GPU utilization and VRAM
  - Disk space per drive
  - Clipboard (read-only)
  - Rolling activity log (what changed since last poll)

It cannot:
  - Write anything (except system_state.json, in the current person's own folder)
  - Access network traffic
  - Read keystrokes
  - Read file contents (it must ask, same as always)

Integration:
  1. Import this module in system32.py
  2. Call senses.start(reference_dir, config) after _ensure_memory_structure()
  3. Call senses.get_snapshot() from _memory_snippet() to inject into system prompt
  4. Register the get_system_state and watch_path tools in TOOLS_SPEC

Author: built for System 32 by Claude, reviewed by SoullessScythe
"""

import datetime
import json
import os
import subprocess
import threading
import time

# ── State ─────────────────────────────────────────────────────────────────────

_state = {
    "ready":          False,
    "last_poll":      None,
    "snapshot":       {},
    "activity_log":   [],   # rolling list of change events
    "watched_paths":  [],   # extra paths to monitor (added via watch_path tool)
    "reference_dir":  None,
    "config":         {},
    "poll_interval":  30,   # seconds between polls (quiet by default)
    "_prev_files":    {},   # path → mtime, for change detection
    "_lock":          threading.Lock(),
}

_STATE_FILE = None  # set at start()

# ── Startup ───────────────────────────────────────────────────────────────────

def start(reference_dir, config, poll_interval=30):
    """
    Start the ambient sensing loop.
    Call once from system32.py startup, after memory structure is ready.
    """
    global _STATE_FILE
    _state["reference_dir"] = reference_dir
    _state["config"]        = config
    _state["poll_interval"] = max(15, poll_interval)  # never faster than 15s
    # Per person (2026-10-28): the snapshot holds THIS person's clipboard, recently
    # opened files, activity and watched folders — private to them, not shared.
    try:
        from s32_config import PERSON_DIR as _pd
        os.makedirs(_pd, exist_ok=True)
        _STATE_FILE = os.path.join(_pd, "system_state.json")
    except Exception:
        _STATE_FILE = os.path.join(reference_dir, "system_state.json")

    # Load any persisted watched paths from previous session
    _load_watched_paths()

    # First poll immediately so it has data before first message
    threading.Thread(target=_poll, daemon=True, name="senses-init").start()

    # Background loop
    t = threading.Thread(target=_loop, daemon=True, name="senses-loop")
    t.start()
    _log("[Senses] Started.")

    # Start screen capture automatically, honoring any saved Screen Vision prefs
    _vp = _load_vision_prefs()
    _screen["capture_mode"] = "all" if _vp.get("mode") == "all" else "cursor"
    _vp_fps = _vp.get("fps", 2)
    try:
        _vp_fps = max(1, min(10, int(_vp_fps)))
    except Exception:
        _vp_fps = 2
    if _vp.get("enabled", True):
        start_screen_capture(fps=_vp_fps, buffer_size=10)
    else:
        _screen["fps"] = _vp_fps          # remember rate even while paused
        _log("[Senses] Screen vision is OFF (saved privacy preference).")


def _loop():
    """Main poll loop. Runs forever in background. Quiet and low-priority."""
    while True:
        time.sleep(_state["poll_interval"])
        try:
            _poll()
        except Exception as e:
            _log(f"[Senses] Poll error: {e}")


def _poll():
    """Collect a full system snapshot and persist it."""
    snap = {}

    snap["timestamp"]    = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snap["foreground"]   = _get_foreground_window()
    snap["processes"]    = _get_processes()
    snap["drives"]       = _get_drives()
    snap["gpu"]          = _get_gpu()
    snap["clipboard"]    = _get_clipboard()
    snap["recent_files"] = _get_recent_files()
    snap["watched"]      = _get_watched_paths()

    # Build activity log entry from changes
    changes = _detect_changes(snap)
    if changes:
        entry = {
            "time":    snap["timestamp"],
            "changes": changes,
        }
        with _state["_lock"]:
            _state["activity_log"].append(entry)
            # Keep last 50 change events
            if len(_state["activity_log"]) > 50:
                _state["activity_log"] = _state["activity_log"][-50:]

    with _state["_lock"]:
        _state["snapshot"] = snap
        _state["last_poll"] = snap["timestamp"]
        _state["ready"] = True

    # Persist to disk
    _save_snapshot(snap)


# ── Sensors ───────────────────────────────────────────────────────────────────

def _get_foreground_window():
    """Return the title and exe of the currently focused window."""
    try:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"title": "", "exe": ""}

        # Window title
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        # Process name
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _pid_to_name(pid.value)

        return {"title": title, "exe": exe}
    except Exception as e:
        return {"title": "", "exe": "", "error": str(e)}


def _pid_to_name(pid):
    """Get process name from PID using Windows API."""
    try:
        import ctypes
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        kernel32 = ctypes.windll.kernel32
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.psapi.GetModuleFileNameExW(h, None, buf, 260)
        kernel32.CloseHandle(h)
        return os.path.basename(buf.value) if buf.value else ""
    except Exception:
        return ""


def _get_processes():
    """Return list of running process names (deduplicated, sorted)."""
    try:
        result = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        names = set()
        for line in result.stdout.splitlines():
            parts = line.strip('"').split('","')
            if parts:
                name = parts[0].strip('"')
                if name:
                    names.add(name)
        return sorted(names)
    except Exception as e:
        return []


def _get_drives():
    """Return disk usage per available drive."""
    import shutil
    drives = []
    try:
        result = subprocess.run(
            ["wmic", "logicaldisk", "get", "deviceid,drivetype"],
            capture_output=True, text=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0].endswith(":"):
                drive = parts[0] + "\\"
                try:
                    total, used, free = shutil.disk_usage(drive)
                    drives.append({
                        "drive": drive,
                        "total_gb": round(total / 1e9, 1),
                        "free_gb":  round(free  / 1e9, 1),
                        "used_pct": round(used / total * 100, 1) if total else 0,
                    })
                except Exception:
                    pass
    except Exception:
        # Fallback: check known drive letters
        import string
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                try:
                    total, used, free = shutil.disk_usage(drive)
                    drives.append({
                        "drive": drive,
                        "total_gb": round(total / 1e9, 1),
                        "free_gb":  round(free  / 1e9, 1),
                        "used_pct": round(used / total * 100, 1) if total else 0,
                    })
                except Exception:
                    pass
    return drives


def _get_gpu():
    """Return GPU utilization and VRAM via nvidia-smi. Graceful fallback."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                "--format=csv,noheader,nounits"
            ],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            gpus = []
            for line in result.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 5:
                    gpus.append({
                        "name":       parts[4],
                        "util_pct":   int(parts[0]) if parts[0].isdigit() else 0,
                        "vram_used":  int(parts[1]) if parts[1].isdigit() else 0,
                        "vram_total": int(parts[2]) if parts[2].isdigit() else 0,
                        "temp_c":     int(parts[3]) if parts[3].isdigit() else 0,
                    })
            return gpus
    except Exception:
        pass

    # Try AMD via rocm-smi (for the RX 9060 XT)
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse", "--showmemuse", "--csv"],
            capture_output=True, text=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        if result.returncode == 0:
            return [{"source": "rocm-smi", "raw": result.stdout.strip()[:200]}]
    except Exception:
        pass

    return []


def _get_clipboard():
    """Return clipboard text content (read-only, truncated)."""
    try:
        import ctypes

        CF_UNICODETEXT = 13
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        if not user32.OpenClipboard(0):
            return ""
        try:
            h = user32.GetClipboardData(CF_UNICODETEXT)
            if not h:
                return ""
            ptr = kernel32.GlobalLock(h)
            if not ptr:
                return ""
            try:
                text = ctypes.wstring_at(ptr)
                return text[:500].strip()  # cap at 500 chars
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def _get_recent_files(minutes=15):
    """
    Return files modified in the last N minutes across watched locations.
    Scans: Desktop, Documents, Downloads, and any extra watched_paths.
    Does NOT scan entire drives - too slow for a background loop.
    """
    user    = os.environ.get("USERPROFILE", "")
    cutoff  = time.time() - (minutes * 60)

    scan_roots = [
        os.path.join(user, "Desktop"),
        os.path.join(user, "Documents"),
        os.path.join(user, "Downloads"),
    ] + _state.get("watched_paths", [])

    # Also include the script dir and reference dir
    ref = _state.get("reference_dir", "")
    if ref:
        scan_roots.append(ref)

    found = []
    seen  = set()

    for root in scan_roots:
        if not root or not os.path.exists(root):
            continue
        try:
            for entry in os.scandir(root):
                if entry.path in seen:
                    continue
                seen.add(entry.path)
                try:
                    stat = entry.stat()
                    if stat.st_mtime >= cutoff and entry.is_file():
                        found.append({
                            "path":     entry.path,
                            "name":     entry.name,
                            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%H:%M:%S"),
                            "size_kb":  round(stat.st_size / 1024, 1),
                        })
                except Exception:
                    pass
        except Exception:
            pass

    # Sort newest first
    found.sort(key=lambda x: x["modified"], reverse=True)
    return found[:30]


def _get_watched_paths():
    """Return status of all explicitly watched paths."""
    results = []
    for path in _state.get("watched_paths", []):
        if not path:
            continue
        exists = os.path.exists(path)
        info = {"path": path, "exists": exists}
        if exists and os.path.isdir(path):
            try:
                entries = os.listdir(path)
                info["file_count"] = len(entries)
                info["sample"] = entries[:10]
            except Exception:
                pass
        elif exists and os.path.isfile(path):
            try:
                stat = os.stat(path)
                info["size_kb"]  = round(stat.st_size / 1024, 1)
                info["modified"] = datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass
        results.append(info)
    return results


# ── Change detection ──────────────────────────────────────────────────────────

def _detect_changes(snap):
    """Compare current snapshot to previous. Returns list of change strings."""
    changes = []

    # Foreground window change
    prev_fg = _state["snapshot"].get("foreground", {})
    curr_fg = snap.get("foreground", {})
    if prev_fg.get("title") != curr_fg.get("title") and curr_fg.get("title"):
        changes.append(f"Focus changed → {curr_fg.get('exe','?')}: {curr_fg.get('title','')[:80]}")

    # New processes
    prev_procs = set(_state["snapshot"].get("processes", []))
    curr_procs = set(snap.get("processes", []))
    started = curr_procs - prev_procs
    stopped = prev_procs - curr_procs
    for p in list(started)[:5]:
        changes.append(f"Process started: {p}")
    for p in list(stopped)[:5]:
        changes.append(f"Process stopped: {p}")

    # New/modified files
    prev_files = {f["path"]: f["modified"] for f in _state["snapshot"].get("recent_files", [])}
    for f in snap.get("recent_files", []):
        if f["path"] not in prev_files:
            changes.append(f"File modified: {f['name']} ({f['size_kb']}KB)")

    # Clipboard changed
    prev_clip = _state["snapshot"].get("clipboard", "")
    curr_clip = snap.get("clipboard", "")
    if curr_clip and curr_clip != prev_clip:
        preview = curr_clip[:60].replace("\n", " ")
        changes.append(f"Clipboard updated: \"{preview}\"")

    return changes


# ── Public API ────────────────────────────────────────────────────────────────

def get_snapshot():
    """
    Return the current system state snapshot as a formatted string
    suitable for injection into the system prompt.
    """
    with _state["_lock"]:
        snap = dict(_state["snapshot"])
        activity = list(_state["activity_log"][-10:])  # last 10 events

    if not snap:
        return ""

    lines = [f"SYSTEM STATE (as of {snap.get('timestamp', 'unknown')}):"]

    # Active window
    fg = snap.get("foreground", {})
    if fg.get("title") or fg.get("exe"):
        lines.append(f"  Active window: [{fg.get('exe', '?')}] {fg.get('title', '')[:100]}")

    # GPU
    gpus = snap.get("gpu", [])
    for g in gpus:
        if isinstance(g, dict) and "util_pct" in g:
            lines.append(
                f"  GPU: {g.get('name','?')} — "
                f"{g.get('util_pct',0)}% util, "
                f"{g.get('vram_used',0)}/{g.get('vram_total',0)}MB VRAM, "
                f"{g.get('temp_c',0)}°C"
            )

    # Drives
    drives = snap.get("drives", [])
    if drives:
        drive_strs = [f"{d['drive']} {d['free_gb']}GB free" for d in drives]
        lines.append(f"  Drives: {' | '.join(drive_strs)}")

    # Recent file activity
    recent = snap.get("recent_files", [])
    if recent:
        lines.append(f"  Recently modified ({len(recent)} files):")
        for f in recent[:8]:
            lines.append(f"    {f['modified']} {f['name']} ({f['size_kb']}KB) — {os.path.dirname(f['path'])}")

    # Clipboard
    clip = snap.get("clipboard", "")
    if clip:
        preview = clip[:120].replace("\n", " ")
        lines.append(f"  Clipboard: \"{preview}\"{'...' if len(clip) > 120 else ''}")

    # Activity log
    if activity:
        lines.append(f"  Recent activity:")
        for event in activity[-5:]:
            for change in event.get("changes", [])[:3]:
                lines.append(f"    [{event['time'][11:16]}] {change}")

    return "\n".join(lines)


def get_snapshot_dict():
    """Return the raw snapshot dict for tool responses."""
    with _state["_lock"]:
        return {
            "snapshot":      dict(_state["snapshot"]),
            "activity_log":  list(_state["activity_log"]),
            "watched_paths": list(_state["watched_paths"]),
            "last_poll":     _state["last_poll"],
        }


def add_watched_path(path):
    """
    Add a path to the persistent watch list.
    It'll monitor it every poll cycle and it survives reboots.
    """
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.exists(path):
        return {"error": f"Path does not exist: {path}"}

    with _state["_lock"]:
        if path not in _state["watched_paths"]:
            _state["watched_paths"].append(path)

    _save_watched_paths()
    return {"status": "watching", "path": path, "total_watched": len(_state["watched_paths"])}


def remove_watched_path(path):
    """Remove a path from the watch list."""
    with _state["_lock"]:
        if path in _state["watched_paths"]:
            _state["watched_paths"].remove(path)
            _save_watched_paths()
            return {"status": "removed", "path": path}
    return {"status": "not_found", "path": path}


# ── Persistence ───────────────────────────────────────────────────────────────

def _save_snapshot(snap):
    """Write snapshot to system_state.json. Silent on failure (USB may be slow)."""
    if not _STATE_FILE:
        return
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "snapshot":      snap,
                "activity_log":  _state["activity_log"][-50:],
                "watched_paths": _state["watched_paths"],
            }, f, indent=2)
    except Exception:
        pass


def _load_watched_paths():
    """Restore watched paths from previous session."""
    if not _STATE_FILE or not os.path.exists(_STATE_FILE):
        return
    try:
        with open(_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        paths = data.get("watched_paths", [])
        # Validate — only keep paths that still exist
        valid = [p for p in paths if p and os.path.exists(p)]
        with _state["_lock"]:
            _state["watched_paths"] = valid
    except Exception:
        pass


def _save_watched_paths():
    """Persist current watched paths without a full snapshot write."""
    if not _STATE_FILE:
        return
    try:
        data = {}
        if os.path.exists(_STATE_FILE):
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["watched_paths"] = _state["watched_paths"]
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _log(msg):
    try:
        from s32_config import _emit as _e
        _e(msg)
    except Exception:
        print(msg)

# ── Screen capture ────────────────────────────────────────────────────────────

_screen = {
    "running":       False,
    "fps":           2,          # frames per second (default: 2)
    "capture_mode":  "cursor",   # "cursor" = monitor the mouse is on; "all" = every monitor
    "buffer":        [],         # rolling list of {"time", "path", "b64", "hash"}
    "buffer_size":   10,         # how many frames to keep in memory
    "frames_dir":    None,       # where frames are saved on disk
    "last_hash":     None,       # perceptual hash of last frame for change detection
    "capture_lock":  threading.Lock(),
    "latest_b64":    None,       # most recent frame as base64 string
    "latest_time":   None,       # timestamp of most recent frame
    "change_count":  0,          # how many distinct screen changes detected
}


def start_screen_capture(fps=2, buffer_size=10):
    """
    Start the continuous screen capture thread.
    Called automatically by start() — can also be called manually to change fps.
    fps: frames per second. 1-10 recommended. Default 2.
    buffer_size: how many frames to keep in rolling memory buffer.
    """
    global _screen
    if _screen["running"]:
        _screen["fps"]         = max(1, min(10, fps))
        _screen["buffer_size"] = buffer_size
        return {"status": "updated", "fps": fps}

    # Create frames directory in reference dir
    ref = _state.get("reference_dir")
    if not ref:
        return {"error": "Senses not started yet — call start() first"}

    frames_dir = os.path.join(ref, "screen_frames")
    os.makedirs(frames_dir, exist_ok=True)
    _screen["frames_dir"]  = frames_dir
    _screen["fps"]         = max(1, min(10, fps))
    _screen["buffer_size"] = buffer_size
    _screen["running"]     = True

    t = threading.Thread(target=_capture_loop, daemon=True, name="senses-screen")
    t.start()
    _log(f"[Senses] Screen capture started at {fps}fps")
    return {"status": "started", "fps": fps, "frames_dir": frames_dir}


def stop_screen_capture():
    """Stop the screen capture thread."""
    _screen["running"] = False
    _log("[Senses] Screen capture stopped.")
    return {"status": "stopped"}


# ── Screen Vision controls (privacy on/off, capture mode, fps) ────────────────
def _vision_prefs_path():
    ref = _state.get("reference_dir")
    return os.path.join(ref, "vision_prefs.json") if ref else None


def _save_vision_prefs():
    p = _vision_prefs_path()
    if not p:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": bool(_screen["running"]),
                       "mode": _screen.get("capture_mode", "cursor"),
                       "fps": int(_screen.get("fps", 2))}, f)
        os.replace(tmp, p)
    except Exception:
        pass


def _load_vision_prefs():
    p = _vision_prefs_path()
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def set_screen_vision(enabled):
    """Privacy switch — turn its screen capture on or off."""
    enabled = bool(enabled)
    if enabled and not _screen["running"]:
        start_screen_capture(fps=_screen.get("fps", 2), buffer_size=_screen.get("buffer_size", 10))
    elif not enabled and _screen["running"]:
        stop_screen_capture()
    _save_vision_prefs()
    return {"ok": True, "enabled": enabled}


def set_capture_mode(mode):
    """'cursor' = follow the monitor the mouse is on; 'all' = capture every monitor."""
    mode = "all" if str(mode).lower() == "all" else "cursor"
    _screen["capture_mode"] = mode
    _save_vision_prefs()
    return {"ok": True, "mode": mode}


def set_fps(fps):
    """Set the capture frame rate (1–10)."""
    try:
        f = max(1, min(10, int(fps)))
    except Exception:
        return {"error": "fps must be a number"}
    _screen["fps"] = f
    _save_vision_prefs()
    return {"ok": True, "fps": f}


def get_vision_settings():
    return {"enabled": bool(_screen["running"]),
            "mode": _screen.get("capture_mode", "cursor"),
            "fps": int(_screen.get("fps", 2))}


def _virtual_screen_bbox():
    """(left, top, right, bottom) spanning ALL monitors — the full virtual desktop."""
    try:
        import ctypes
        u = ctypes.windll.user32
        SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
        SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
        x = u.GetSystemMetrics(SM_XVIRTUALSCREEN)
        y = u.GetSystemMetrics(SM_YVIRTUALSCREEN)
        w = u.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        h = u.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        if w and h:
            return (x, y, x + w, y + h)
    except Exception:
        pass
    return None


def _capture_loop():
    """Continuous screen capture loop. Runs in background thread."""
    try:
        from PIL import ImageGrab, Image
    except ImportError:
        _log("[Senses] PIL not available — screen capture disabled")
        _screen["running"] = False
        return

    while _screen["running"]:
        interval = 1.0 / max(1, _screen["fps"])
        try:
            _capture_frame(ImageGrab, Image)
        except Exception as e:
            _log(f"[Senses] Capture error: {e}")
        time.sleep(interval)


def _img_hash(img):
    """Simple perceptual hash for change detection — fast 8x8 average hash."""
    try:
        small = img.convert("L").resize((8, 8))
        pixels = list(small.getdata())
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if p > avg else "0" for p in pixels)
        return bits
    except Exception:
        return ""


def _hamming(h1, h2):
    """Hamming distance between two hash strings."""
    if not h1 or not h2 or len(h1) != len(h2):
        return 64
    return sum(c1 != c2 for c1, c2 in zip(h1, h2))


def _cursor_monitor_bbox():
    """Return (left, top, right, bottom) of the monitor the mouse cursor is on, so
    capture follows the active screen on multi-monitor setups. Returns None on
    non-Windows or any failure, in which case the caller falls back to the primary
    screen. Uses ctypes only — no extra dependency."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return None
        MONITOR_DEFAULTTONEAREST = 2
        hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)

        class _MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD),
                        ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT),
                        ("dwFlags", wintypes.DWORD)]

        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            bbox = (r.left, r.top, r.right, r.bottom)
            _screen["active_monitor"] = bbox
            return bbox
    except Exception:
        pass
    return None


def _grab_active_screen(ImageGrab):
    """Grab per capture mode: 'all' = the whole virtual desktop (every monitor);
    otherwise the monitor the cursor is on, falling back to the primary screen."""
    if _screen.get("capture_mode") == "all":
        try:
            return ImageGrab.grab(all_screens=True)
        except TypeError:
            pass
        except Exception:
            pass
        return ImageGrab.grab()
    bbox = _cursor_monitor_bbox()
    if bbox:
        try:
            return ImageGrab.grab(bbox=bbox, all_screens=True)
        except TypeError:
            try:
                return ImageGrab.grab(bbox=bbox)          # very old PIL: no all_screens
            except Exception:
                pass
        except Exception:
            pass
    return ImageGrab.grab()


def _capture_frame(ImageGrab, Image):
    """Capture one frame, check for changes, update buffer."""
    import base64
    import io

    # Grab the screen(s) per capture mode (multi-monitor aware)
    img = _grab_active_screen(ImageGrab)
    _cap_bbox = (_virtual_screen_bbox() if _screen.get("capture_mode") == "all"
                 else _cursor_monitor_bbox())   # virtual-screen rect of what we grabbed
    _orig_w, _orig_h = img.size                 # before any resize

    # Compute hash for change detection
    h = _img_hash(img)
    last_h = _screen.get("last_hash")

    # Skip if screen hasn't changed meaningfully (hamming < 3 = essentially same)
    if last_h and _hamming(h, last_h) < 3:
        return

    _screen["last_hash"]   = h
    _screen["change_count"] += 1

    # Resize so the STORED/encoded frame matches the size chat() actually sends the
    # model (it thumbnails to 1024). These MUST stay equal: the vision model reasons
    # in the pixel space of the image it SEES, and game_input maps those pixels back
    # to the screen via latest_img_size. If capture is 1280 but the model only sees
    # 1024, every coordinate it gives is off by the 1024/1280 ratio (~20% toward the
    # top-left) — which is the cursor-never-lands-right bug. Keep both at 1024.
    w, ht = img.size
    if w > 1024:
        ratio = 1024 / w
        img = img.resize((1024, int(ht * ratio)), Image.LANCZOS)

    # Encode to JPEG base64
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=70)
    b64 = base64.b64encode(buf.getvalue()).decode()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    ts_readable = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Save to disk (rolling — keep last buffer_size files on disk too)
    frame_path = None
    frames_dir = _screen.get("frames_dir")
    if frames_dir:
        frame_path = os.path.join(frames_dir, f"frame_{ts}.jpg")
        try:
            with open(frame_path, "wb") as f:
                f.write(buf.getvalue())
        except Exception:
            frame_path = None

        # Clean old frames — keep only last buffer_size on disk
        try:
            existing = sorted([
                os.path.join(frames_dir, fn)
                for fn in os.listdir(frames_dir)
                if fn.startswith("frame_") and fn.endswith(".jpg")
            ])
            while len(existing) > _screen["buffer_size"]:
                try:
                    os.remove(existing.pop(0))
                except Exception:
                    break
        except Exception:
            pass

    # Update in-memory buffer
    entry = {
        "time":  ts_readable,
        "path":  frame_path,
        "b64":   b64,
        "hash":  h,
    }

    with _screen["capture_lock"]:
        _screen["buffer"].append(entry)
        if len(_screen["buffer"]) > _screen["buffer_size"]:
            _screen["buffer"].pop(0)
        _screen["latest_b64"]  = b64
        _screen["latest_time"] = ts_readable
        _screen["latest_img_size"] = img.size   # (w,h) of the stored (resized) image
        _screen["latest_cap_rect"] = (
            (_cap_bbox[0], _cap_bbox[1], _cap_bbox[2], _cap_bbox[3])
            if _cap_bbox else (0, 0, _orig_w, _orig_h)
        )  # virtual-screen rect the image covers → lets input map image px → screen coords


def _grid_font(size):
    """Load a legible TrueType font at the requested px size, trying common system
    fonts, falling back to PIL's bitmap default. Bigger labels survive the vision
    encoder's downscaling — small labels are unreadable to the model."""
    try:
        from PIL import ImageFont
        for _name in ("arial.ttf", "Arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf",
                      "C:/Windows/Fonts/arial.ttf", "segoeui.ttf"):
            try:
                return ImageFont.truetype(_name, size)
            except Exception:
                continue
        return ImageFont.load_default()
    except Exception:
        return None


def _draw_coordinate_grid(b64_jpeg, major_frac=0.1, minor_frac=0.05):
    """Overlay a light, labeled coordinate grid on a frame. Vision models are poor
    at estimating raw pixel coordinates from a bare image; a labeled grid is a
    well-established, GAME-AGNOSTIC aid — it lets the model read any target's
    position off the nearest gridlines instead of guessing, in any game or app.
    Two tiers: faint MINOR lines for fine reference, bold LABELED MAJOR lines for
    the numbers. Used only during active game sessions (see see_screen). Returns a
    new b64 JPEG, or the original on any failure. Density is tunable (major_frac/
    minor_frac); labels are sized to the frame so they stay readable after encoding."""
    try:
        import base64
        import io
        from PIL import Image, ImageDraw
        raw = base64.b64decode(b64_jpeg)
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        draw = ImageDraw.Draw(img, "RGBA")
        major_line = (255, 235, 59, 140)   # soft yellow, bold-ish — the labeled lines
        minor_line = (255, 235, 59, 55)    # very faint — fine reference between labels
        label_bg   = (0, 0, 0, 170)
        label_fg   = (255, 235, 59, 255)
        fsize = max(14, int(h * 0.022))    # ~18px on an 800px-tall frame; scales with size
        font  = _grid_font(fsize)
        pad   = max(2, fsize // 5)
        lblw  = fsize * 2 + pad * 2        # rough label box width (up to 4 digits)
        lblh  = fsize + pad

        def _label(px, py, text):
            draw.rectangle([px, py, px + lblw, py + lblh], fill=label_bg)
            draw.text((px + pad, py + max(0, (lblh - fsize) // 2 - 1)), text, fill=label_fg, font=font)

        # Minor lines first (so majors draw on top)
        if minor_frac and minor_frac < major_frac:
            sx = max(1, int(w * minor_frac)); x = sx
            while x < w:
                draw.line([(x, 0), (x, h)], fill=minor_line, width=1); x += sx
            sy = max(1, int(h * minor_frac)); y = sy
            while y < h:
                draw.line([(0, y), (w, y)], fill=minor_line, width=1); y += sy
        # Major labeled lines — labels at top AND bottom (x) / left AND right (y)
        Mx = max(1, int(w * major_frac)); x = Mx
        while x < w:
            draw.line([(x, 0), (x, h)], fill=major_line, width=2)
            _label(x + 2, 0, str(x))
            _label(x + 2, h - lblh, str(x))
            x += Mx
        My = max(1, int(h * major_frac)); y = My
        while y < h:
            draw.line([(0, y), (w, y)], fill=major_line, width=2)
            _label(0, y + 1, str(y))
            _label(w - lblw, y + 1, str(y))
            y += My
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return b64_jpeg


def _game_session_active():
    """True while an 'S32 PLAYS' session is live — used to scope the coordinate
    grid to gameplay so normal screen vision stays clean."""
    try:
        from s32_config import state as _shared
        return bool(_shared.get("s32_playing"))
    except Exception:
        return False


def see_screen(frames=1):
    """
    Return the most recent screen frame(s) as base64 JPEG.
    frames=1: just the latest frame (default)
    frames=N: last N distinct frames from the rolling buffer
    Returns dict with "frames" list of {"time", "b64"} and "change_count".
    """
    if not _screen["running"]:
        return {
            "error": "Screen capture not running. Call start_screen_capture() first.",
            "hint":  "Call start_screen_capture() to turn screen vision on."
        }

    with _screen["capture_lock"]:
        buf = list(_screen["buffer"])
        change_count = _screen["change_count"]

    if not buf:
        return {"error": "No frames captured yet — try again in a moment."}

    # Return last N frames
    selected = buf[-max(1, min(frames, len(buf))):]
    _iw, _ih = _screen.get("latest_img_size", (0, 0)) or (0, 0)
    # During a game, overlay a labeled coordinate grid so it can read pixel
    # positions accurately for move_to/click (VLMs can't estimate raw coords well).
    _grid = _game_session_active()
    out_frames = []
    for f in selected:
        b64 = f["b64"]
        if _grid and b64:
            b64 = _draw_coordinate_grid(b64)
        out_frames.append({"time": f["time"], "b64": b64})
    # Debug aid: when gridding, drop the most recent gridded frame on disk so the
    # operator can open exactly what it saw (to judge grid density/readability).
    if _grid and out_frames:
        try:
            import base64 as _b64
            fdir = _screen.get("frames_dir")
            if fdir:
                with open(os.path.join(fdir, "latest_grid.jpg"), "wb") as _gf:
                    _gf.write(_b64.b64decode(out_frames[-1]["b64"]))
        except Exception:
            pass
    return {
        "frames": out_frames,
        "change_count": change_count,
        "fps":    _screen["fps"],
        "buffer": len(buf),
        "image_width":  _iw,
        "image_height": _ih,
        "grid": _grid,
    }


def get_screen_status():
    """Return current screen capture status."""
    return {
        "running":      _screen["running"],
        "fps":          _screen["fps"],
        "buffer_size":  _screen["buffer_size"],
        "frames_dir":   _screen["frames_dir"],
        "change_count": _screen["change_count"],
        "latest_time":  _screen["latest_time"],
        "buffer_count": len(_screen["buffer"]),
    }
