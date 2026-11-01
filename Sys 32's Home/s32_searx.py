# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_searx.py — System 32's own private search engine (SearXNG), 2026-10-29.

SearXNG is an open-source meta-search engine: it asks Google Images, Google Videos,
YouTube, Bing, DuckDuckGo, Dailymotion, Vimeo and dozens more at once — with no account,
no API key, and nothing in between. System 32 runs its OWN copy, on this computer only:

  • First boot: downloads SearXNG's source (pinned, tested version) into
    <home>\\searxng\\src and installs its packages into <home>\\searxng\\lib — a folder of
    their own, so they can never clash with System 32's packages. Nothing is installed on
    the computer itself; it all lives on the drive. (One-time; runs in the background.)
  • Every boot: starts it hidden, listening on 127.0.0.1 only (nothing else on the
    network can reach it). s32_web picks it up for image, video and text search.
  • It closes itself when System 32 closes — a watchdog inside the launcher exits as
    soon as System 32's process is gone, however it ended — so the USB can be ejected.

No Docker, nothing for a new person to learn. Turn it off with "searxng": false in
config.json. A SearXNG the operator runs himself ("searxng_url", or one already
listening on 8888) is used instead of starting ours.

License: SearXNG is AGPL-3.0. It is downloaded at first run, not shipped in System 32's
own files, and run unmodified.
"""

import os
import sys
import json
import time
import shutil
import socket
import secrets
import zipfile
import threading
import subprocess

from s32_config import CONFIG, _script_dir, log

SEARX_COMMIT = "c15647b5a5921858d5147d291c658fbda3cba12c"     # 2026-10-29, tested
_ZIP_URL = f"https://codeload.github.com/searxng/searxng/zip/{SEARX_COMMIT}"

ROOT = os.path.join(_script_dir, "searxng")
SRC = os.path.join(ROOT, "src")
LIB = os.path.join(ROOT, "lib")
SETTINGS = os.path.join(ROOT, "settings.yml")
LOGF = os.path.join(ROOT, "searxng.log")
STAMP = os.path.join(ROOT, ".installed.json")
# SearXNG keeps small cache databases in the system temp folder — some engines store
# per-search tokens there (hashed, but still a footprint on the HOST computer). Its temp
# folder is pointed here instead, on the drive, and emptied every start and stop.
TMP = os.path.join(ROOT, "tmp")
PORTS = (8888, 8889, 8890)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

status = {"state": "off", "url": None, "detail": ""}
_proc = {"p": None}
_lock = threading.Lock()

_SETTINGS_TEXT = """# SearXNG settings for System 32 — a private search engine running on THIS computer only.
# System 32 starts it on its own; you don't need to touch this file.
use_default_settings: true

server:
  secret_key: "{key}"     # random, made for your copy
  limiter: false          # only you use it, from this computer
  image_proxy: false

search:
  safe_search: 1          # moderate
  formats:                # System 32 reads JSON — without this SearXNG answers 403
    - html
    - json

ui:
  query_in_title: false
{engines}"""

# SearXNG ships Google Images switched off (Google blocks it now and then). It's one source
# among several here, so a block costs nothing — turn it on.
_ENGINES_TEXT = """
engines:
  - name: google images
    disabled: false
"""


def _healthy(port, timeout=1.5):
    """SearXNG's /healthz — answers 'OK' instantly (a real search can take seconds)."""
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{port}/healthz", timeout=timeout,
                         proxies={"http": None, "https": None})
        return r.status_code == 200 and r.text.strip().upper().startswith("OK")
    except Exception:
        return False


def _http_json(port, q="test", category="general", timeout=2.0):
    """GET a local SearXNG search → parsed JSON or None (never via any proxy)."""
    try:
        import requests
        r = requests.get(f"http://127.0.0.1:{port}/search",
                         params={"q": q, "format": "json", "categories": category},
                         timeout=timeout, proxies={"http": None, "https": None})
        if r.status_code == 200:
            d = r.json()
            return d if isinstance(d, dict) and "results" in d else None
    except Exception:
        pass
    return None


def _port_in_use(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _write_settings():
    """Create settings.yml (random secret) — or, for an existing one (e.g. the copy the
    operator placed by hand), make sure JSON output and Google Images are on."""
    os.makedirs(ROOT, exist_ok=True)
    if not os.path.exists(SETTINGS):
        with open(SETTINGS, "w", encoding="utf-8") as f:
            f.write(_SETTINGS_TEXT.format(key=secrets.token_hex(32), engines=_ENGINES_TEXT))
        return
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            txt = f.read()
        changed = False
        if "- json" not in txt:
            log("[S32][SEARX] settings.yml has JSON output off — System 32 can't read it that way; "
                "add '- json' under search → formats (left as you wrote it).")
        if "\nengines:" not in txt and not txt.startswith("engines:"):
            txt = txt.rstrip("\n") + "\n" + _ENGINES_TEXT
            changed = True
        if changed:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                f.write(txt)
    except Exception as e:
        log(f"[S32][SEARX] couldn't check settings.yml: {e}")


def _installed():
    try:
        with open(STAMP, encoding="utf-8") as f:
            st = json.load(f)
        return (st.get("commit") == SEARX_COMMIT
                and st.get("python") == "%d.%d" % sys.version_info[:2]
                and os.path.isfile(os.path.join(SRC, "searx", "webapp.py"))
                and os.path.isdir(LIB))
    except Exception:
        return False


def _install():
    """Download the pinned source + install its packages into LIB. Returns True/False."""
    import requests
    try:
        from s32_web import _proxies
        proxies = _proxies()
    except Exception:
        proxies = None
    os.makedirs(ROOT, exist_ok=True)
    zpath = os.path.join(ROOT, "src.zip.part")
    status.update(state="installing", detail="downloading SearXNG")
    log("[S32][SEARX] setting up private search (SearXNG) — one-time download, in the background…")
    try:
        with requests.get(_ZIP_URL, stream=True, timeout=(15, 300), proxies=proxies) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"download HTTP {r.status_code}")
            with open(zpath, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    if chunk:
                        f.write(chunk)
        tmp = os.path.join(ROOT, "src.tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        inner = next((os.path.join(tmp, d) for d in os.listdir(tmp)
                      if os.path.isdir(os.path.join(tmp, d, "searx"))), None)
        if not inner:
            raise RuntimeError("the download didn't contain SearXNG")
        shutil.rmtree(SRC, ignore_errors=True)
        os.replace(inner, SRC)
        shutil.rmtree(tmp, ignore_errors=True)
    except Exception as e:
        status.update(state="failed", detail=f"download failed: {str(e)[:120]}")
        log(f"[S32][SEARX] download failed: {e}")
        return False
    finally:
        try:
            os.remove(zpath)
        except Exception:
            pass
    status.update(detail="installing its packages")
    log("[S32][SEARX] installing SearXNG's packages into its own folder…")
    shutil.rmtree(LIB, ignore_errors=True)
    try:
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "--target", LIB, "-r", os.path.join(SRC, "requirements.txt")],
                           capture_output=True, text=True, timeout=1800, creationflags=_NO_WINDOW)
    except Exception as e:
        status.update(state="failed", detail=f"package install failed: {str(e)[:120]}")
        log(f"[S32][SEARX] package install failed: {e}")
        return False
    if r.returncode != 0:
        status.update(state="failed", detail="package install failed")
        log(f"[S32][SEARX] package install failed: {(r.stderr or r.stdout or '')[-400:]}")
        return False
    with open(STAMP, "w", encoding="utf-8") as f:
        json.dump({"commit": SEARX_COMMIT, "python": "%d.%d" % sys.version_info[:2],
                   "installed": time.strftime("%Y-%m-%d %H:%M")}, f)
    log("[S32][SEARX] SearXNG installed.")
    return True


# The launcher: SearXNG's own folders first on the path (works with an embeddable Python,
# whose ._pth file ignores PYTHONPATH), plus a watchdog that exits the moment System 32's
# process is gone — whether it closed normally, crashed, or was killed.
_LAUNCHER = r'''
import os, sys, threading, time
sys.path[:0] = [{lib!r}, {src!r}]
PARENT = {ppid}

# Windows has no 'pwd' module or os.getuid (Unix only). SearXNG imports pwd for one error
# message about a database System 32 doesn't use (2026-10-29: "No module named 'pwd'" on
# the operator's first boot) — give it a harmless stand-in.
try:
    import pwd  # noqa: F401
except ImportError:
    import types
    class _PwEntry:
        pw_name = os.environ.get("USERNAME", "user")
        pw_uid = 0
    _pwd = types.ModuleType("pwd")
    _pwd.getpwuid = lambda uid=0: _PwEntry()
    _pwd.getpwnam = lambda name="": _PwEntry()
    sys.modules["pwd"] = _pwd
if not hasattr(os, "getuid"):
    os.getuid = lambda: 0

def _alive(pid):
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def _watch():
    while True:
        time.sleep(2)
        if not _alive(PARENT):
            os._exit(0)

threading.Thread(target=_watch, daemon=True).start()
# Privacy: the built-in web server logs every request line — i.e. every search query —
# which would leave a search history on the drive. Errors only.
import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)
import runpy
runpy.run_module("searx.webapp", run_name="__main__")
'''


def start():
    """Background: install if needed, then run. Never raises."""
    try:
        _start()
    except Exception as e:
        status.update(state="failed", detail=str(e)[:160])
        log(f"[S32][SEARX] couldn't start private search: {e}")


def _start():
    import s32_config as _cfg
    if _cfg.airplane():
        status.update(state="off", detail="airplane mode")
        log("[S32][SEARX] airplane mode is on — private search not started")
        return
    if CONFIG.get("searxng") is False:
        status.update(state="off", detail="turned off in config.json")
        return
    if (CONFIG.get("searxng_url") or "").strip():
        status.update(state="external", url=CONFIG["searxng_url"], detail="using searxng_url")
        return
    for port in PORTS:                         # one already running (Docker, or a leftover)
        if _healthy(port):
            _announce(f"http://127.0.0.1:{port}", "already running")
            return
    with _lock:
        _write_settings()
        if not _installed() and not _install():
            return
        port = next((p for p in PORTS if not _port_in_use(p)), None)
        if port is None:
            status.update(state="failed", detail=f"ports {PORTS} are all taken")
            log(f"[S32][SEARX] ports {', '.join(map(str, PORTS))} are all in use — private search not started")
            return
        env = dict(os.environ)
        _wipe_tmp()
        env.update({"SEARXNG_SETTINGS_PATH": SETTINGS, "SEARXNG_PORT": str(port),
                    "SEARXNG_BIND_ADDRESS": "127.0.0.1", "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1", "TEMP": TMP, "TMP": TMP, "TMPDIR": TMP})
        code = _LAUNCHER.format(lib=LIB, src=SRC, ppid=os.getpid())
        status.update(state="starting", detail=f"starting on port {port}")
        logf = open(LOGF, "w", encoding="utf-8", errors="replace")
        from s32_config import CREATE_SUSPENDED, tie_to_s32
        _proc["p"] = subprocess.Popen([sys.executable, "-c", code], cwd=SRC, env=env,
                                      stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      creationflags=_NO_WINDOW | (CREATE_SUSPENDED if os.name == "nt" else 0))
        tie_to_s32(_proc["p"], "SearXNG")      # (its own watchdog stays as a second line)
        for _ in range(60):
            time.sleep(1)
            if _proc["p"].poll() is not None:
                break
            if _healthy(port):
                _announce(f"http://127.0.0.1:{port}", "started")
                return
        tail = ""
        try:
            with open(LOGF, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-500:]
        except Exception:
            pass
        stop()
        status.update(state="failed", detail="didn't start — see searxng\\searxng.log")
        log(f"[S32][SEARX] private search didn't start. Last lines of searxng.log:\n{tail}")


def _announce(url, how):
    status.update(state="on", url=url, detail=how)
    try:
        import s32_web
        s32_web._searx_probe.update({"url": url, "ts": time.time()})
    except Exception:
        pass
    try:                                       # its own port — never something to close
        import s32_irondome
        s32_irondome.add_to_whitelist(ports=[int(url.rsplit(":", 1)[1])])
    except Exception:
        pass
    log(f"[S32][SEARX] private search (SearXNG) {how} at {url} — Google Images/Videos, YouTube "
        f"and more now feed image, video and web search")


def _wipe_tmp():
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)


def stop():
    p = _proc.get("p")
    if p and p.poll() is None:
        try:
            p.terminate()
            p.wait(timeout=5)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
    _proc["p"] = None
    if p is not None:
        time.sleep(0.5)
        shutil.rmtree(TMP, ignore_errors=True)
    if status.get("state") in ("on", "starting"):
        status.update(state="off", detail="stopped")
