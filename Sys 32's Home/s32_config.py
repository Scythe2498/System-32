# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_config.py — CONFIG, state dict, path constants, core utilities.
Everything else imports from here. No imports from other s32_* modules.
"""

import base64
import copy
import datetime
import io
import json
import os
import random
import re
import shutil
import signal
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import http.client
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

try:
    import senses as _senses
except ImportError:
    _senses = None

# ── Config ────────────────────────────────────────────────────────────────────

_user       = os.environ.get("USERNAME", "user")
_profile    = os.environ.get("USERPROFILE", f"C:\\Users\\{_user}")
_appdata    = os.environ.get("APPDATA",    f"{_profile}\\AppData\\Roaming")
_local      = os.environ.get("LOCALAPPDATA", f"{_profile}\\AppData\\Local")
_docs_comfy = os.path.join(_profile, "Documents", "ComfyUI")
_comfy_dir  = os.path.join(_local, "Programs", "ComfyUI", "resources", "ComfyUI")
_script_dir = os.path.dirname(os.path.abspath(__file__))
_out_base   = os.path.join(os.path.dirname(_script_dir), "System 32's Output")

CONFIG = {
    "lms_exe":            os.path.join(_profile, ".lmstudio", "bin", "lms.exe"),
    "lmstudio_model_key": "local-model",
    "lmstudio_model":     "local-model",
    "lmstudio_host":      "http://localhost:1234",
    "comfy_image_host":   "http://127.0.0.1:8188",
    "comfy_image_dir":    _comfy_dir,
    "comfy_image_python": os.path.join(_docs_comfy, ".venv", "Scripts", "python.exe"),
    "comfy_image_input":  os.path.join(_docs_comfy, "input"),
    "comfy_image_output": os.path.join(_docs_comfy, "output"),
    "user_dir":      _docs_comfy,
    "models_dir":    os.path.join(_docs_comfy, "models"),
    "loras_dir":     os.path.join(_docs_comfy, "models", "loras"),
    "datasets_dir":  os.path.join(_docs_comfy, "input", "Datasets"),
    "out_images":      os.path.join(_out_base, "generated images"),
    "out_videos":      os.path.join(_out_base, "generated videos"),
    "out_3d":          os.path.join(_out_base, "generated 3d"),
    "out_checkpoints": os.path.join(_out_base, "checkpoints"),
    "out_dataset":     os.path.join(_out_base, "dataset"),
    "out_diffusion":   os.path.join(_out_base, "diffusion_models"),
    "out_loras":       os.path.join(_out_base, "loras"),
    "web_port":      7432,
    "web_proxy":       "",
    "web_proxy_http":  "",
    "web_proxy_https": "",
}

_config_file = os.path.join(_script_dir, "config.json")
if os.path.exists(_config_file):
    try:
        with open(_config_file, "r") as _f:
            _loaded = json.load(_f)
        CONFIG.update(_loaded)
    except Exception as _e:
        print(f"[S32] Warning: could not load config.json: {_e}")

WORKFLOW_DIR = CONFIG.get("workflow_dir", os.path.join(_script_dir, "JsonFiles"))

_script_dir = os.path.dirname(os.path.abspath(__file__))
REFERENCE_DIR = CONFIG.get(
    "reference_dir",
    os.path.join(os.path.dirname(_script_dir), "System 32's reference material")
    if os.path.exists(os.path.join(os.path.dirname(_script_dir), "System 32's reference material"))
    else os.path.join(_script_dir, "reference material")
)

# ── People: new computer = new person (2026-10-28) ────────────────────────────
# Everything that is ABOUT a person — memories, chat history, session log, their
# favorites, their coding projects, their profile — lives in that person's own
# folder, keyed by the computer they use: reference\people\<COMPUTERNAME>\. The drive
# plugged into a new computer meets a new person and sees nothing of anyone else.
# What the assistant itself is (identity, name/gender, voice, learned tool lessons,
# workflows) stays shared. Privacy is the product: one person's data never reaches
# another person's session.
_machine_name     = os.environ.get("COMPUTERNAME", "unknown")
_current_operator = None   # legacy; the live value is state["operator"] (see s32_memory)


def _person_slug(name):
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("._")
    return s[:60] or "unknown"


PERSON_SLUG = _person_slug(_machine_name)
PEOPLE_DIR = os.path.join(REFERENCE_DIR, "people")
PERSON_DIR = os.path.join(PEOPLE_DIR, PERSON_SLUG)

# Output-side folders that are ALSO per person (2026-10-28 review): save states hold a
# person's memories, training/ holds their exported conversations + code corpora,
# gameplay_dataset/ holds screenshots of their games. Each is <root>\<COMPUTERNAME>\.
_cfg_out = CONFIG.get("out_images", "")
OUT_BASE = os.path.dirname(_cfg_out) if _cfg_out else ""
SAVE_STATES_ROOT = os.path.join(OUT_BASE, "save states") if OUT_BASE else ""
TRAINING_ROOT = os.path.join(OUT_BASE, "training") if OUT_BASE else ""
GAMEPLAY_ROOT = os.path.join(OUT_BASE, "gameplay_dataset") if OUT_BASE else ""


def person_subdir(root):
    """This person's folder inside a per-person Output root (created on demand)."""
    if not root:
        return ""
    d = os.path.join(root, PERSON_SLUG)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def private_roots():
    """[(root, this person's part)] — every area partitioned by person. The privacy
    wall (s32_dispatch) refuses anything inside a root that isn't this person's part."""
    out = [(PEOPLE_DIR, PERSON_DIR)]
    for r in (SAVE_STATES_ROOT, TRAINING_ROOT, GAMEPLAY_ROOT):
        if r:
            out.append((r, os.path.join(r, PERSON_SLUG)))
    return out


# Per-person data that used to be shared. It belongs to the FIRST computer that boots
# with per-person folders (the operator boots his own desktop first). That computer is
# recorded in people\.first, and every boot retries anything a locked file left behind
# — so a half-finished move can never strand data where everyone can read it.
_LEGACY_PERSON_ITEMS = ("memory.md", "memory_recent.md", "memory_archive", "memory_index.md",
                        "session_log.md", "favorites.json", "path_favorites.json",
                        "thoughts.log", "conversation_log.jsonl", "operators",
                        "web_requests.log", "system_state.json")
_FIRST_MARK = os.path.join(PEOPLE_DIR, ".first")


def _first_person():
    try:
        with open(_FIRST_MARK, encoding="utf-8") as f:
            return f.read().strip() or None
    except Exception:
        return None


def _move_into(src, dst_dir, name=None):
    """Move src into dst_dir (merging folders). Returns True when src is gone."""
    dst = os.path.join(dst_dir, name or os.path.basename(src))
    try:
        os.makedirs(dst_dir, exist_ok=True)
        if os.path.isdir(src) and os.path.isdir(dst):
            for n in os.listdir(src):
                _move_into(os.path.join(src, n), dst)
            os.rmdir(src)
        elif os.path.exists(dst):
            os.replace(src, dst)        # the legacy copy is the newer, more complete one
        else:
            shutil.move(src, dst)
    except Exception as e:
        print(f"[S32] Warning: couldn't move {src} into the person folder yet ({e}); will retry next boot")
    return not os.path.exists(src)


def _migrate_legacy_person_data():
    try:
        first = _first_person()
        legacy = [os.path.join(REFERENCE_DIR, n) for n in _LEGACY_PERSON_ITEMS
                  if os.path.exists(os.path.join(REFERENCE_DIR, n))]
        code_ws = [(os.path.join(_script_dir, "workspace", sub), sub) for sub in ("code", "unreal")]
        code_ws += [(os.path.join(_script_dir, "patches", "code"), "code")]
        code_ws = [(p, sub) for p, sub in code_ws if os.path.isdir(p)]
        out_legacy = []
        for root in (TRAINING_ROOT, GAMEPLAY_ROOT):
            if root and os.path.isdir(root):
                for n in os.listdir(root):
                    p = os.path.join(root, n)
                    # Anything loose in the root predates per-person folders. A folder
                    # is a person's only if it's a known person (has a people\ folder).
                    if not os.path.isdir(os.path.join(PEOPLE_DIR, n)):
                        out_legacy.append((p, root))
        if not (legacy or code_ws or out_legacy):
            return
        if not first:
            if os.path.isdir(PEOPLE_DIR) and any(not n.startswith(".") for n in os.listdir(PEOPLE_DIR)):
                first = sorted(n for n in os.listdir(PEOPLE_DIR) if not n.startswith("."))[0]
            else:
                first = PERSON_SLUG
            os.makedirs(PEOPLE_DIR, exist_ok=True)
            with open(_FIRST_MARK, "w", encoding="utf-8") as f:
                f.write(first)
        owner = os.path.join(PEOPLE_DIR, first)
        moved = 0
        for p in legacy:
            moved += _move_into(p, owner)
        for p, sub in code_ws:
            moved += _move_into(p, owner, sub)
        for p, root in out_legacy:
            moved += _move_into(p, os.path.join(root, first))
        if moved:
            print(f"[S32] Moved {moved} item(s) of earlier data into {first}'s private folder")
    except Exception as _me:
        print(f"[S32] Warning: per-person migration incomplete: {_me}")


if REFERENCE_DIR:
    _migrate_legacy_person_data()
    try:
        os.makedirs(PERSON_DIR, exist_ok=True)
    except Exception:
        pass

# ── Airplane mode (2026-10-31) ────────────────────────────────────────────────
# One switch that cuts System 32 off from the internet. Saved next to the reference docs
# (a factory reset turns it back off). Enforced at the LOWEST level here — every socket
# System 32's own process opens goes through the guard below — so a feature nobody
# remembered to check still can't reach out. Each web feature also checks airplane() and
# says so plainly. This computer, the local network (and Tailscale) keep working.
_AIRPLANE_FILE = os.path.join(REFERENCE_DIR, "airplane_mode.json") if REFERENCE_DIR else ""
AIRPLANE_MSG = ("Airplane mode is on — System 32 is offline, so nothing on the internet can be "
                "reached (search, web pages, downloads, installs). Turn it off in the Iron Dome panel.")


def _airplane_load():
    try:
        with open(_AIRPLANE_FILE, encoding="utf-8") as f:
            return bool(json.load(f).get("on"))
    except Exception:
        return False


_airplane = {"on": _airplane_load()}


def airplane():
    return _airplane["on"]


def set_airplane(on):
    _airplane["on"] = bool(on)
    try:
        if _AIRPLANE_FILE:
            with open(_AIRPLANE_FILE, "w", encoding="utf-8") as f:
                json.dump({"on": bool(on)}, f)
    except Exception as e:
        print(f"[S32] couldn't save airplane mode: {e}")
    return _airplane["on"]


class AirplaneMode(OSError):
    pass


def _local_ip(ip):
    """This computer, the local network, link-local, Tailscale (100.64/10)."""
    import ipaddress
    try:
        a = ipaddress.ip_address(str(ip).split("%")[0])
    except ValueError:
        return None                                          # not an address (a name)
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    if a.is_loopback or a.is_private or a.is_link_local or a.is_unspecified:
        return True
    return a.version == 4 and ipaddress.ip_address("100.64.0.0") <= a <= ipaddress.ip_address("100.127.255.255")


def _proxy_endpoints():
    """Every proxy this process could be sent through: System 32's own setting AND the
    system's (HTTPS_PROXY / Windows proxy settings) — a proxy on this computer is still
    the internet (2026-10-31: an environment proxy on 127.0.0.1 slipped past)."""
    out = set()
    vals = [(CONFIG.get(k) or "").strip() for k in ("web_proxy", "web_proxy_http", "web_proxy_https")]
    try:
        vals += [str(v).strip() for k, v in urllib.request.getproxies().items() if k.lower() != "no"]
    except Exception:
        pass
    for v in vals:
        if not v:
            continue
        try:
            u = urllib.parse.urlparse(v if "://" in v else "http://" + v)
            if u.hostname:
                out.add((u.hostname.lower(), u.port or (1080 if u.scheme.startswith("socks") else 8080)))
        except ValueError:
            continue
    return out


def _airplane_refuses(host, port=None):
    if not _airplane["on"] or host is None:
        return False
    h = str(host).strip("[]").lower()
    if port is not None and (h, int(port)) in _proxy_endpoints():
        return True                                          # a local proxy is still the internet
    if h in ("localhost", "") or h.endswith(".localhost"):
        return False
    loc = _local_ip(h)
    return True if loc is None else not loc                 # names (no DNS!) and public addresses


import socket as _socket
_real_getaddrinfo = _socket.getaddrinfo
_real_connect = _socket.socket.connect
_real_connect_ex = _socket.socket.connect_ex
_real_sendto = _socket.socket.sendto


def _g_getaddrinfo(host, *a, **k):
    if _airplane_refuses(host):
        raise _socket.gaierror(_socket.EAI_NONAME, "Airplane mode is on — no internet name lookups")
    return _real_getaddrinfo(host, *a, **k)


def _addr_refused(address):
    return isinstance(address, tuple) and len(address) >= 2 and _airplane_refuses(address[0], address[1])


def _g_connect(self, address):
    if _addr_refused(address):
        raise AirplaneMode(f"Airplane mode is on — connection to {address[0]} refused")
    return _real_connect(self, address)


def _g_connect_ex(self, address):
    if _addr_refused(address):
        return 10051 if os.name == "nt" else 101             # 'network unreachable'
    return _real_connect_ex(self, address)


def _g_sendto(self, data, *rest):
    address = rest[-1] if rest else None
    if _addr_refused(address):
        raise AirplaneMode(f"Airplane mode is on — packet to {address[0]} refused")
    return _real_sendto(self, data, *rest)


if not getattr(_socket, "_s32_airplane_guard", False):
    _socket.getaddrinfo = _g_getaddrinfo
    _socket.socket.connect = _g_connect
    _socket.socket.connect_ex = _g_connect_ex
    _socket.socket.sendto = _g_sendto
    _socket._s32_airplane_guard = True

# ── Child processes end with System 32 (2026-10-31) ───────────────────────────
# llama-server, ComfyUI and SearXNG used to outlive System 32 whenever it closed any way
# other than the browser tab (the cmd window's X, a crash, Task Manager): the model kept
# 15 GB of VRAM, a stray python.exe locked the torch upgrade. Now each one is started
# SUSPENDED, put in a Windows Job Object that kills everything in it when its last handle
# closes — the handle lives in THIS process, so however System 32 ends, Windows ends
# them — and only then resumed (ComfyUI's venv python.exe is a launcher that starts the
# real Python at once; starting suspended means nothing escapes before it's in the job).
# Things the operator launches THROUGH System 32 (games, Unreal, the browser) are never
# put in it.
CREATE_SUSPENDED = 0x00000004
_job = {"h": None, "failed": False}


def _job_handle():
    if os.name != "nt" or _job["failed"]:
        return None
    if _job["h"]:
        return _job["h"]
    import ctypes
    from ctypes import wintypes

    class _BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                    "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IO),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        h = k32.CreateJobObjectW(None, None)
        if not h:
            raise OSError(ctypes.get_last_error())
        info = _EXT()
        info.BasicLimitInformation.LimitFlags = 0x00002000          # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error())
        _job["h"] = h
        return h
    except Exception as e:
        _job["failed"] = True
        print(f"[S32] Couldn't create the process job ({e}) — child processes may outlive System 32")
        return None


def tie_to_s32(proc, name=""):
    """Put a process that was started with CREATE_SUSPENDED into System 32's job, then let
    it run. Always resumes it, even if the job can't be used. → True if it's tied."""
    tied = False
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    try:
        h = _job_handle()
        hp = int(getattr(proc, "_handle", 0) or 0)
        if h and hp:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            tied = bool(k32.AssignProcessToJobObject(h, hp))
            if tied:
                log(f"[S32] {name or 'child process'} tied to System 32 — it closes when System 32 closes")
            else:
                log(f"[S32] ⚠ {name or 'a child process'} couldn't be tied to System 32 "
                    f"(error {ctypes.get_last_error()}) — it may outlive System 32")
    except Exception as e:
        log(f"[S32] ⚠ tie_to_s32 failed for {name}: {e}")
    finally:
        try:
            nt = ctypes.WinDLL("ntdll")
            nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
            nt.NtResumeProcess(int(getattr(proc, "_handle", 0) or 0))
        except Exception as e:
            print(f"[S32] couldn't resume {name}: {e}")
    return tied


def adopt_port_owner(port, name=""):
    """A llama-server / ComfyUI that was ALREADY running when System 32 started (left over
    from a session before this fix, or started by hand) is reused — but it was never in the
    job, so it outlived every close (2026-10-31: 'model already running' after closing with
    X). Put the process listening on `port` — plus its launcher parent of the same name and
    its children — into the job now, so it ends with THIS session. → number adopted."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes
    h = _job_handle()
    if not h:
        return 0
    try:
        import psutil
        pids = {c.pid for c in psutil.net_connections(kind="tcp")
                if c.pid and c.laddr and c.laddr.port == int(port) and c.status == "LISTEN"}
        procs = set()
        for pid in pids:
            try:
                p = psutil.Process(pid)
                procs.add(p.pid)
                procs.update(ch.pid for ch in p.children(recursive=True))
                par = p.parent()
                if par and par.name().lower() == p.name().lower():
                    procs.add(par.pid)                      # venv launcher → real python
            except Exception:
                continue
    except Exception:
        return 0
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    n = 0
    for pid in procs:
        hp = k32.OpenProcess(0x0100 | 0x0001, False, pid)        # SET_QUOTA | TERMINATE
        if not hp:
            continue
        try:
            if k32.AssignProcessToJobObject(h, hp):
                n += 1
        finally:
            k32.CloseHandle(hp)
    if n:
        log(f"[S32] {name or 'process'} was already running — adopted it ({n} process(es)); it now closes with System 32")
    elif procs:
        log(f"[S32] ⚠ {name or 'process'} was already running and couldn't be adopted — it may outlive System 32")
    return n


# ── Memory paths ──────────────────────────────────────────────────────────────
SESSION_LOG_FILE = os.path.join(PERSON_DIR, "session_log.md")
MEMORY_FILE      = os.path.join(PERSON_DIR, "memory.md")
MEMORY_RECENT    = os.path.join(PERSON_DIR, "memory_recent.md")
MEMORY_ARCHIVE   = os.path.join(PERSON_DIR, "memory_archive")
MEMORY_INDEX     = os.path.join(PERSON_DIR, "memory_index.md")
OPERATORS_DIR    = os.path.join(PERSON_DIR, "operators")
MACHINES_DIR     = os.path.join(REFERENCE_DIR, "machines")
IDENTITY_FILE    = os.path.join(REFERENCE_DIR, "identity.md")
_session_start    = datetime.datetime.now()
_hw = None  # populated by get_hardware() in s32_hardware

TEXT_EXTS = {'.txt','.json','.yaml','.yml','.md','.ini','.cfg','.log',
             '.csv','.py','.bat','.sh','.html','.css','.js','.toml','.xml'}

# ── Global state ──────────────────────────────────────────────────────────────

state = {
    "messages":          [],
    "job_status":        None,
    "lmstudio_ready":    False,
    "vision_ready":      False,   # True only when an mmproj projector is loaded
    "model_switching":   False,
    "comfy_image_ready": False,
    "startup_log":       [],
    "lms_process":       None,
    "model_load_failed": False,
    "last_image_bytes":  None,
    "last_image_path":   None,
    "last_video_path":   None,
    "last_3d_path":      None,
    "missing_nodes":        {},
    "install_failures":     {},
    "session_images":       [],
    "startup_notification": None,
    "dir_map":              {},
    "last_browsed_folder":  None,
    "visited_dirs":         set(),
    "pending_results":      [],
    "gen_lock":             threading.Lock(),
    "cancel":               False,   # set True to abort the current generation/turn
    "active_conn":          None,    # live llama-server HTTP connection (for mid-gen abort)
    "active_workflow":      None,
    "nav_state": {
        "task":        None,
        "folder":      None,
        "page":        1,
        "search_term": None,
        "file_path":   None,
        "tool":        None,
        "updated":     None,
    },
}

_result_id   = 0
_job_id      = 0
_result_lock = threading.Lock()

def _push_result(result, job_id):
    global _result_id
    with _result_lock:
        _result_id += 1
        state["pending_results"].append({"id": _result_id, "job_id": job_id, "result": result})
        # Counts Confirm cards (fileop/fs/dl_confirm) so a turn can tell whether the card she
        # talks about was really raised (2026-10-31).
        if isinstance(result, dict) and str(result.get("type", "")).endswith("_confirm"):
            state["confirm_cards"] = state.get("confirm_cards", 0) + 1

def _push_done(job_id):
    with _result_lock:
        state["pending_results"].append({"job_id": job_id, "done": True})

# ── Core utilities ────────────────────────────────────────────────────────────

_NEON  = "\033[38;2;57;255;20m"    # #39FF14 — neon green (its signature colour)
_AMBER = "\033[38;2;255;176;0m"    # warnings
_RED   = "\033[38;2;255;72;72m"    # errors
_RESET = "\033[0m"
_ANSI_ON = False


def _enable_ansi():
    """Turn on ANSI colour rendering in the Windows console (once)."""
    global _ANSI_ON
    if _ANSI_ON:
        return
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            # enable VIRTUAL_TERMINAL_PROCESSING (0x0004) on stdout
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
    except Exception:
        pass
    _ANSI_ON = True


_ERR_HINTS  = ("error", "failed", "fail:", "traceback", "exception",
               "timed out", "timeout", "http 4", "http 5", "crash",
               "could not", "couldn't", "cannot")
_WARN_HINTS = ("⚠", "warning", "warn", "retry", "retrying", "missing",
               "not found", "no mmproj", "skipped", "unavailable", "[??]")


def _colorize(msg):
    low = msg.lower()
    if "retry" in low or "retrying" in low:
        col = _AMBER                                # transient, recovering — warn, not error
    elif any(h in low for h in _ERR_HINTS):
        col = _RED
    elif any(h in low for h in _WARN_HINTS):
        col = _AMBER
    else:
        col = _NEON
    return f"{col}{msg}{_RESET}"


def _emit(msg):
    """Print a log line in its neon-green personality (red/amber for problems)."""
    _enable_ansi()
    try:
        print(_colorize(msg))
    except Exception:
        print(msg)


def log(msg):
    _emit(msg)
    state["startup_log"].append(msg)   # store raw (no ANSI) for the UI startup log
    if len(state["startup_log"]) > 200:
        state["startup_log"].pop(0)

def http_get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())

def http_post(url, data, timeout=30):
    payload = json.dumps(data).encode()
    req = urllib.request.Request(url, data=payload,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"[S32] HTTP {e.code} from {url}: {body[:400]}")
        e.s32_body = body
        raise

def is_up(url):
    try:
        urllib.request.urlopen(url, timeout=3)
        return True
    except Exception:
        return False


# ── Cancellation ──────────────────────────────────────────────────────────────
# Phase 3: a user-pressable "stop" needs two things working together:
#   1) the HTTP server must be threaded so /cancel can be received while /chat is
#      blocked in generation (system32.py uses ThreadingHTTPServer), and
#   2) the in-flight llama-server request must be abortable. llama.cpp stops a
#      slot's generation as soon as the client connection drops, so we issue the
#      generation over an http.client connection we keep a handle to and close
#      that handle on cancel.

_conn_lock = threading.Lock()


class GenerationCancelled(Exception):
    """Raised when an in-flight generation is aborted by the user."""
    pass


def cancel_generation():
    """Flag the current turn cancelled and drop the live llama-server socket.
    Safe to call from any thread (e.g. the /cancel request handler)."""
    state["cancel"] = True
    with _conn_lock:
        conn = state.get("active_conn")
    if conn is not None:
        try:
            conn.close()  # dropping the socket makes llama-server stop the slot
        except Exception:
            pass


def cancellable_post(url, data, timeout=300):
    """POST JSON like http_post, but over a connection cancel_generation() can
    close mid-flight. Raises GenerationCancelled if the turn was cancelled, and
    otherwise mirrors http_post's HTTPError behaviour (with .s32_body)."""
    parsed = urllib.parse.urlparse(url)
    body    = json.dumps(data).encode()
    headers = {"Content-Type": "application/json"}
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")

    with _conn_lock:
        state["active_conn"] = conn

    raw = None
    resp = None
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()          # blocks during generation; abortable by close()
        raw  = resp.read()
    except Exception:
        # If we were cancelled, the close() in cancel_generation() is what broke this.
        if state.get("cancel"):
            raise GenerationCancelled() from None
        raise
    finally:
        with _conn_lock:
            state["active_conn"] = None
        try:
            conn.close()
        except Exception:
            pass

    if state.get("cancel"):
        raise GenerationCancelled()

    if resp.status >= 400:
        err = urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(raw))
        body_text = raw.decode("utf-8", errors="replace")
        log(f"[S32] HTTP {resp.status} from {url}: {body_text[:400]}")
        err.s32_body = body_text
        raise err

    return json.loads(raw)


def cancellable_post_stream(url, data, timeout=300, on_delta=None):
    """Stream a chat completion from an OpenAI-compatible server (llama-server)
    over a cancellable connection, and reassemble it into the SAME shape the
    non-streaming endpoint returns:
        {"choices":[{"message":{"content": str, "tool_calls": [...]}}]}

    If on_delta is given, it's called with each text fragment as it arrives, so
    callers can forward tokens to the UI for live output.

    Why streaming: with stream=False the server sends nothing until the whole
    response is generated, so a slow/long generation blocks getresponse() past
    the socket read timeout (the TimeoutError on large drops). Streaming makes
    getresponse() return on the first token; the per-read timeout then only has
    to cover the gap *between* tokens, so long answers never time out. Cancel is
    checked between chunks, and closing the socket still stops the slot.
    """
    data = dict(data)
    data["stream"] = True
    parsed  = urllib.parse.urlparse(url)
    body    = json.dumps(data).encode()
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")

    with _conn_lock:
        state["active_conn"] = conn

    content_parts = []
    _streamed = [0]        # how much of the content has gone to on_delta
    reasoning_parts = []   # its thinking (llama-server streams it apart from the reply)
    tool_acc = {}   # index -> {"name": str, "args": str}
    _finish = [""]   # why it stopped: "stop" (ended on its own), "length" (token limit), …
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()            # returns on first bytes, not full response
        if resp.status >= 400:
            raw = resp.read()
            err = urllib.error.HTTPError(url, resp.status, resp.reason, resp.headers, io.BytesIO(raw))
            err.s32_body = raw.decode("utf-8", errors="replace")
            log(f"[S32] HTTP {resp.status} from {url} (stream): {err.s32_body[:400]}")
            raise err
        while True:
            if state.get("cancel"):
                raise GenerationCancelled()
            line = resp.readline()
            if not line:
                break
            line = line.decode("utf-8", errors="replace").strip()
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                evt = json.loads(payload)
                delta = evt["choices"][0].get("delta", {})
                if evt["choices"][0].get("finish_reason"):
                    _finish[0] = evt["choices"][0]["finish_reason"]
            except Exception:
                continue
            _rc = delta.get("reasoning_content") or delta.get("reasoning")
            if _rc:
                reasoning_parts.append(_rc)
            if delta.get("content"):
                content_parts.append(delta["content"])
                if on_delta:
                    # Never stream raw tool-call markup into the chat bubble. When
                    # llama-server fails to parse a call it arrives as plain content
                    # ("<tool_call> <function=…>"); s32_llm recovers it. Hold back any
                    # tail that could be the start of "<tool_call", stop at a real one.
                    _full = "".join(content_parts)
                    _cut = _full.find("<tool_call")
                    if _cut < 0:
                        _cut = len(_full)
                        for _k in range(min(len("<tool_call") - 1, len(_full)), 0, -1):
                            if "<tool_call".startswith(_full[-_k:]):
                                _cut = len(_full) - _k
                                break
                    if _cut > _streamed[0]:
                        try:
                            on_delta(_full[_streamed[0]:_cut])
                        except Exception:
                            pass
                        _streamed[0] = _cut
            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_acc.setdefault(idx, {"name": "", "args": ""})
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]
    except GenerationCancelled:
        raise
    except Exception:
        if state.get("cancel"):
            raise GenerationCancelled() from None
        raise
    finally:
        with _conn_lock:
            state["active_conn"] = None
        try:
            conn.close()
        except Exception:
            pass

    if state.get("cancel"):
        raise GenerationCancelled()

    # Flush a held-back tail that turned out NOT to be a tool call (e.g. ends in "<").
    if on_delta:
        _full = "".join(content_parts)
        if "<tool_call" not in _full and _streamed[0] < len(_full):
            try:
                on_delta(_full[_streamed[0]:])
            except Exception:
                pass

    tool_calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if slot["name"]:
            tool_calls.append({"function": {"name": slot["name"],
                                            "arguments": slot["args"] or "{}"}})
    message = {"content": "".join(content_parts), "finish_reason": _finish[0]}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message}]}
