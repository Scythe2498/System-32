# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_gameinput.py — confined game input injection (its hands).

It can press/hold keys and move/click the mouse INSIDE a game it launched via
"S32 PLAYS" — and only then. Input is generated with SendInput using scan codes
(+ KEYEVENTF_SCANCODE) so DirectInput/RawInput games actually register it, unlike
virtual-key injection which many games ignore.

CONFINEMENT — every injection is gated on ALL of:
  1. a session is active        — state["s32_playing"] is set
  2. the GAME window is focused — the foreground window's process exe lives under
                                   the game's install dir. SendInput targets the
                                   foreground window, so this is what stops input
                                   leaking into the desktop or its own chat. Lose
                                   focus (alt-tab) and its hands go dead instantly.
  3. not halted                 — the off-switch hasn't been pulled

OFF-SWITCH — the Stop button in its chat hits halt(): it releases every held key
and mouse button (nothing stays stuck) and blocks further input until a new
"S32 PLAYS" launch re-arms the session. Releases ALWAYS go through, gate or not,
so keys can never get stranded down.

This module imports cleanly off-Windows (ctypes.windll is only touched inside the
send functions); only actual injection requires Windows.
"""

import os
import time
import ctypes
import threading

try:
    from s32_config import state, log
except Exception:
    state, log = {}, (lambda *a, **k: None)

_lock = threading.Lock()
_held_keys = set()       # scancode names currently down
_held_buttons = set()    # mouse buttons currently down


# ── Scan-code map (Set 1 make codes; True = extended 0xE0 key) ────────────────
_SCAN = {
    "esc": (0x01, False), "escape": (0x01, False),
    "1": (0x02, False), "2": (0x03, False), "3": (0x04, False), "4": (0x05, False),
    "5": (0x06, False), "6": (0x07, False), "7": (0x08, False), "8": (0x09, False),
    "9": (0x0A, False), "0": (0x0B, False), "-": (0x0C, False), "=": (0x0D, False),
    "backspace": (0x0E, False), "tab": (0x0F, False),
    "q": (0x10, False), "w": (0x11, False), "e": (0x12, False), "r": (0x13, False),
    "t": (0x14, False), "y": (0x15, False), "u": (0x16, False), "i": (0x17, False),
    "o": (0x18, False), "p": (0x19, False),
    "a": (0x1E, False), "s": (0x1F, False), "d": (0x20, False), "f": (0x21, False),
    "g": (0x22, False), "h": (0x23, False), "j": (0x24, False), "k": (0x25, False),
    "l": (0x26, False),
    "z": (0x2C, False), "x": (0x2D, False), "c": (0x2E, False), "v": (0x2F, False),
    "b": (0x30, False), "n": (0x31, False), "m": (0x32, False),
    "enter": (0x1C, False), "return": (0x1C, False),
    "lctrl": (0x1D, False), "ctrl": (0x1D, False), "control": (0x1D, False),
    "lshift": (0x2A, False), "shift": (0x2A, False), "rshift": (0x36, False),
    "lalt": (0x38, False), "alt": (0x38, False),
    "space": (0x39, False), "spacebar": (0x39, False),
    "capslock": (0x3A, False),
    "f1": (0x3B, False), "f2": (0x3C, False), "f3": (0x3D, False), "f4": (0x3E, False),
    "f5": (0x3F, False), "f6": (0x40, False), "f7": (0x41, False), "f8": (0x42, False),
    "f9": (0x43, False), "f10": (0x44, False), "f11": (0x57, False), "f12": (0x58, False),
    ",": (0x33, False), ".": (0x34, False), "/": (0x35, False), ";": (0x27, False),
    "'": (0x28, False), "[": (0x1A, False), "]": (0x1B, False), "\\": (0x2B, False),
    "`": (0x29, False),
    # extended keys (0xE0-prefixed)
    "up": (0x48, True), "down": (0x50, True), "left": (0x4B, True), "right": (0x4D, True),
    "rctrl": (0x1D, True), "ralt": (0x38, True),
    "ins": (0x52, True), "insert": (0x52, True), "del": (0x53, True), "delete": (0x53, True),
    "home": (0x47, True), "end": (0x4F, True), "pageup": (0x49, True), "pagedown": (0x51, True),
}


# ── SendInput plumbing (Windows) ─────────────────────────────────────────────
PUL = ctypes.POINTER(ctypes.c_ulong)


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("dwExtraInfo", PUL)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong), ("dwExtraInfo", PUL)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.c_ulong), ("u", _INPUTUNION)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


_INPUT_MOUSE, _INPUT_KEYBOARD = 0, 1
_KEYEVENTF_EXTENDEDKEY, _KEYEVENTF_KEYUP, _KEYEVENTF_SCANCODE = 0x0001, 0x0002, 0x0008
_MOUSEEVENTF = {"move": 0x0001,
                "left_down": 0x0002, "left_up": 0x0004,
                "right_down": 0x0008, "right_up": 0x0010,
                "middle_down": 0x0020, "middle_up": 0x0040}


def _send_key(scan, extended, down):
    flags = _KEYEVENTF_SCANCODE | (0 if down else _KEYEVENTF_KEYUP)
    if extended:
        flags |= _KEYEVENTF_EXTENDEDKEY
    extra = ctypes.c_ulong(0)
    ki = _KEYBDINPUT(0, scan, flags, 0, ctypes.cast(ctypes.byref(extra), PUL))
    inp = _INPUT(_INPUT_KEYBOARD, _INPUTUNION(ki=ki))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def _send_mouse(flags, dx=0, dy=0):
    extra = ctypes.c_ulong(0)
    mi = _MOUSEINPUT(dx, dy, 0, flags, 0, ctypes.cast(ctypes.byref(extra), PUL))
    inp = _INPUT(_INPUT_MOUSE, _INPUTUNION(mi=mi))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


# ── Confinement gate: it acts only while the cursor is over the game screen ──
_rect_cache = {"t": 0.0, "rects": []}


def _game_window_rects():
    """Visible top-level windows owned by the game's process(es) (exe under the
    install dir). For a fullscreen game this rect is the whole monitor."""
    try:
        installdir = state.get("s32_playing_installdir") or ""
        if not installdir:
            return []
        root = os.path.normcase(os.path.abspath(installdir))
        try:
            import psutil
            game_pids = set()
            for p in psutil.process_iter(["pid", "exe"]):
                try:
                    exe = p.info.get("exe") or ""
                    if exe and os.path.normcase(os.path.abspath(exe)).startswith(root):
                        game_pids.add(p.info["pid"])
                except Exception:
                    continue
        except Exception as e:
            log(f"[S32][GAME] can't enumerate processes ({str(e)[:60]})")
            return []
        if not game_pids:
            return []
        user32 = ctypes.windll.user32
        rects = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                pid = ctypes.c_ulong()
                user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
                if pid.value in game_pids:
                    r = _RECT()
                    user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
                    if (r.right - r.left) >= 200 and (r.bottom - r.top) >= 200:
                        rects.append((r.left, r.top, r.right, r.bottom))
            except Exception:
                pass
            return True

        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        return rects
    except Exception as e:
        log(f"[S32][GAME] window scan error: {str(e)[:80]}")
        return []


def _game_rects_cached():
    now = time.time()
    if now - _rect_cache["t"] > 1.5:          # game windows move rarely; cache briefly
        _rect_cache["rects"] = _game_window_rects()
        _rect_cache["t"] = now
    return _rect_cache["rects"]


def _cursor_over_game():
    """True when the mouse cursor sits inside the game's window region. This is the
    co-op gate: cursor on the game → it may act; cursor off → its hands go dead,
    so you always overpower just by moving the mouse away."""
    try:
        rects = _game_rects_cached()
        if not rects:
            return False, "game window not found yet (still loading?)"
        pt = _POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        for (l, t, r, b) in rects:
            if l <= pt.x <= r and t <= pt.y <= b:
                return True, ""
        return False, "cursor is off the game screen"
    except Exception as e:
        return False, f"cursor check error: {str(e)[:60]}"


def _can_inject():
    if state.get("game_input_halted"):
        return False, "halted (Stop was pressed) — relaunch with S32 PLAYS to re-arm"
    if not state.get("s32_playing"):
        return False, "no game session active"
    ok, why = _cursor_over_game()
    if not ok:
        return False, why
    return True, ""


# ── Public actions ───────────────────────────────────────────────────────────
def _resolve(key):
    return _SCAN.get((key or "").strip().lower())


def press_key(key, duration_ms=60):
    ok, why = _can_inject()
    if not ok:
        return {"ok": False, "reason": why}
    sc = _resolve(key)
    if not sc:
        return {"ok": False, "error": f"unknown key '{key}'"}
    try:
        _send_key(sc[0], sc[1], True)
        time.sleep(min(max(int(duration_ms), 10), 3000) / 1000.0)
        _send_key(sc[0], sc[1], False)
        return {"ok": True, "key": key}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def hold_key(key):
    ok, why = _can_inject()
    if not ok:
        return {"ok": False, "reason": why}
    sc = _resolve(key)
    if not sc:
        return {"ok": False, "error": f"unknown key '{key}'"}
    try:
        _send_key(sc[0], sc[1], True)
        with _lock:
            _held_keys.add(key.lower())
        return {"ok": True, "held": sorted(_held_keys)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def release_key(key=None):
    # releases ALWAYS work (no gate) so nothing gets stranded down
    try:
        if key is None:
            return release_all()
        sc = _resolve(key)
        if sc:
            _send_key(sc[0], sc[1], False)
        with _lock:
            _held_keys.discard(key.lower())
        return {"ok": True, "held": sorted(_held_keys)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def move_mouse(dx=0, dy=0):
    ok, why = _can_inject()
    if not ok:
        return {"ok": False, "reason": why}
    try:
        _send_mouse(_MOUSEEVENTF["move"], int(dx), int(dy))
        return {"ok": True, "dx": int(dx), "dy": int(dy)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def move_mouse_to(x, y):
    """Place the cursor at a point the model read off the see_screen image. x,y are
    PIXELS IN THAT IMAGE — they're mapped back to real screen coordinates using the
    frame's monitor rect + scale (multi-monitor + downscale aware), then SetCursorPos
    places the cursor exactly there (SetCursorPos spans the whole virtual desktop)."""
    ok, why = _can_inject()
    if not ok:
        return {"ok": False, "reason": why}
    try:
        sx, sy = int(x), int(y)
        mapped = False
        try:
            import senses
            cap = senses._screen.get("latest_cap_rect")   # (l,t,r,b) virtual coords
            isz = senses._screen.get("latest_img_size")    # (w,h) image px
            if cap and isz and isz[0] and isz[1]:
                l, t, r, b = cap
                iw, ih = isz
                sx = int(l + (float(x) / iw) * (r - l))
                sy = int(t + (float(y) / ih) * (b - t))
                mapped = True
        except Exception:
            pass
        ctypes.windll.user32.SetCursorPos(sx, sy)
        return {"ok": True, "x": sx, "y": sy, "from_image_px": [int(x), int(y)],
                "mapped": mapped}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def click_mouse(button="left", duration_ms=40):
    ok, why = _can_inject()
    if not ok:
        return {"ok": False, "reason": why}
    b = (button or "left").lower()
    if b not in ("left", "right", "middle"):
        return {"ok": False, "error": f"unknown button '{button}'"}
    try:
        _send_mouse(_MOUSEEVENTF[f"{b}_down"])
        with _lock:
            _held_buttons.add(b)
        time.sleep(min(max(int(duration_ms), 10), 2000) / 1000.0)
        _send_mouse(_MOUSEEVENTF[f"{b}_up"])
        with _lock:
            _held_buttons.discard(b)
        return {"ok": True, "button": b}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


def release_all():
    """Let go of every held key + mouse button. Always runs."""
    released = []
    with _lock:
        keys = list(_held_keys)
        btns = list(_held_buttons)
    for k in keys:
        sc = _resolve(k)
        if sc:
            try:
                _send_key(sc[0], sc[1], False); released.append(k)
            except Exception:
                pass
    for b in btns:
        try:
            _send_mouse(_MOUSEEVENTF[f"{b}_up"]); released.append(b)
        except Exception:
            pass
    with _lock:
        _held_keys.clear()
        _held_buttons.clear()
    return {"ok": True, "released": released}


def halt():
    """The off-switch (Stop button): release everything + block further input."""
    r = release_all()
    state["game_input_halted"] = True
    log(f"[S32][GAME] HALT — released {r.get('released', [])}, input blocked until re-arm")
    return {"ok": True, "halted": True, "released": r.get("released", [])}


def arm():
    """Called when a new S32-play session starts (clears halt)."""
    state["game_input_halted"] = False
    with _lock:
        _held_keys.clear()
        _held_buttons.clear()
    return {"ok": True}


def status():
    over, why = (_cursor_over_game() if state.get("s32_playing") else (False, "no session"))
    return {"playing": state.get("s32_playing"),
            "halted": bool(state.get("game_input_halted")),
            "cursor_over_game": over,
            "gate_reason": ("ready" if over else why),
            "game_windows": len(_game_rects_cached()) if state.get("s32_playing") else 0,
            "held_keys": sorted(_held_keys), "held_buttons": sorted(_held_buttons)}


# ── Single tool entry point ──────────────────────────────────────────────────
def game_input(action, key=None, dx=0, dy=0, x=None, y=None, button="left", duration_ms=60):
    a = (action or "").strip().lower()
    if a == "press":
        return press_key(key, duration_ms)
    if a == "hold":
        return hold_key(key)
    if a == "release":
        return release_key(key)
    if a == "release_all":
        return release_all()
    if a == "move":
        return move_mouse(dx, dy)
    if a == "move_to":
        return move_mouse_to(x or 0, y or 0)
    if a == "click":
        return click_mouse(button, duration_ms)
    if a == "status":
        return status()
    return {"ok": False, "error": f"unknown action '{action}' "
            "(press|hold|release|release_all|move|move_to|click|status)"}
