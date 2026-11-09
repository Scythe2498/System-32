# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_update.py — "A new version is available — Update?" (2026-11-01).

Checks System 32's GitHub repo for a newer RELEASE: one anonymous request (no machine
name, no usage data — the same outbound path and browser identity as web search), at
start and once a day. Never in airplane mode; off entirely with "update_check": false in
config.json. Nothing changes until the operator presses Update.

Update = only System 32's OWN files are replaced (everything the repo holds: Sys 32's Home
code + README/LICENSE). Memories, reference material, outputs, models, ComfyUI, Python,
settings — none of that is in the repo, so none of it is touched. System 32 then restarts
by itself (launch.py).

CHANGELOG / ROLL BACK (2026-11-07): no backups are kept on the drive any more (they filled
it up over time). The DATA · SAVE · RESET panel lists the last three releases straight from
GitHub, with their notes; installing an older one ("Roll back") downloads THAT release and
installs it the same way as an update. Only fetched when the panel is opened.

Publishing an update: on GitHub, Releases → "Draft a new release" → tag v0.9.1 (higher than
VERSION in s32_config.py) → describe what changed → Publish. The description is what the
operator sees under "What's new".
"""

import json
import os
import re
import shutil
import threading
import time
import zipfile

from s32_config import CONFIG, REFERENCE_DIR, VERSION, _script_dir, log

REPO = CONFIG.get("update_repo") or "Scythe2498/System-32"
_CHECK_EVERY = 24 * 3600
_FIRST_CHECK_DELAY = 120          # let the model + ComfyUI start first
_MAX_ZIP = 80 * 2**20
_BACKUP_DIR = os.path.join(_script_dir, ".update_backup")   # old backups — removed at start
_DONE_MARK = os.path.join(_script_dir, ".update_done")       # {"from", "to"} → one note after restart
_DISMISS_FILE = os.path.join(REFERENCE_DIR, "update_dismissed.json") if REFERENCE_DIR else ""
_RESTART_MARK = os.path.join(_script_dir, ".update_restart")
_HOME_NAME = "Sys 32's Home"
# Files an update may never write, whatever the zip holds (they're per-machine or personal).
_NEVER = {"config.json", "ui_prefs.json", "seen.json", "model_settings.json"}

_state = {"latest": None, "notes": "", "zip": "", "page": "", "checked": 0, "error": "",
          "busy": False, "done": "", "updated_from": ""}
_releases = {"list": [], "at": 0, "error": ""}
_LIST_TTL = 600
_lock = threading.Lock()


def _ver(v):
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(n) for n in nums[:4]) or (0,)


def _newer(a, b):
    """True when version a is newer than version b."""
    return _ver(a) > _ver(b)


def _dismissed():
    try:
        with open(_DISMISS_FILE, encoding="utf-8") as f:
            return str(json.load(f).get("version") or "")
    except Exception:
        return ""


def _enabled():
    import s32_config as _cfg
    return CONFIG.get("update_check", True) is not False and not _cfg.airplane()


def check():
    """Ask GitHub for the latest release. Quiet on every kind of 'nothing to say'."""
    if not _enabled():
        return status()
    try:
        from s32_web import _request
    except Exception as e:
        _state["error"] = f"web layer unavailable: {e}"
        return status()
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    body, resp, err = _request("GET", url, headers={"Accept": "application/vnd.github+json"})
    _state["checked"] = time.time()
    code = getattr(resp, "status_code", None)
    if err or code is None:
        _state["error"] = err or "no answer"
        return status()
    if code == 404:                       # no release yet, or the repo is still private
        _state.update(latest=None, error="")
        return status()
    if code != 200:
        _state["error"] = f"GitHub answered {code}"
        return status()
    try:
        d = json.loads(body or "{}")
    except ValueError:
        _state["error"] = "unreadable answer"
        return status()
    tag = str(d.get("tag_name") or "")
    if d.get("draft") or d.get("prerelease") or not tag:
        return status()
    _state.update(latest=tag.lstrip("vV"), notes=str(d.get("body") or "")[:3000],
                  zip=str(d.get("zipball_url") or ""), page=str(d.get("html_url") or ""), error="")
    if _newer(_state["latest"], VERSION):
        log(f"[S32][UPDATE] System 32 {_state['latest']} is available (this is {VERSION})")
    return status()


def status():
    avail = bool(_state["latest"] and _newer(_state["latest"], VERSION)
                 and _state["latest"] != _dismissed())
    return {"current": VERSION, "available": _state["latest"] if avail else "",
            "notes": _state["notes"] if avail else "", "page": _state["page"] if avail else "",
            "busy": _state["busy"], "done": _state["done"], "error": _state["error"],
            "rollback": "", "updated_from": "",            # no rollback banner any more
            "enabled": _enabled()}


def dismiss(version):
    """'Not now' — this version isn't offered again (a newer one will be)."""
    try:
        if _DISMISS_FILE:
            with open(_DISMISS_FILE, "w", encoding="utf-8") as f:
                json.dump({"version": str(version or "")}, f)
    except Exception as e:
        return {"error": str(e)}
    return {"ok": True}


# ── applying ────────────────────────────────────────────────────────────────
def _download(url, dest):
    import s32_config as _cfg
    if _cfg.airplane():
        raise RuntimeError(_cfg.AIRPLANE_MSG)
    import requests
    from s32_web import _proxies, _UA, _log_request
    with requests.get(url, headers={"User-Agent": _UA}, proxies=_proxies(), timeout=60,
                      stream=True, allow_redirects=True) as r:
        _log_request("GET", url, r.status_code, "update download")
        r.raise_for_status()
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                got += len(chunk)
                if got > _MAX_ZIP:
                    raise RuntimeError("the download is far bigger than an update should be")
                f.write(chunk)


def _plan(zpath):
    """[(member, destination path)] — ONLY the repo's own files: Sys 32's Home\\... and the
    top-level README / LICENSE. Anything else in the zip is ignored."""
    home_parent = os.path.dirname(_script_dir)
    out = []
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        root = names[0].split("/", 1)[0] + "/" if names else ""
        for n in names:
            rel = n[len(root):] if n.startswith(root) else n
            parts = [p for p in rel.split("/") if p]
            if not parts or any(p in ("..", ".") or ":" in p for p in parts):
                continue
            if parts[0] == _HOME_NAME and len(parts) > 1:
                if parts[1].startswith(".") or parts[-1].lower() in _NEVER:
                    continue
                dest = os.path.join(_script_dir, *parts[1:])
            elif len(parts) == 1 and (parts[0].lower() == "readme.md" or parts[0].lower().startswith("license")):
                dest = os.path.join(home_parent, parts[0])
            else:
                continue
            out.append((n, dest))
    if not any(os.path.basename(d) == "system32.py" for _, d in out):
        raise RuntimeError("that download doesn't look like System 32 (no system32.py)")
    return out


def _restart_soon():
    """Close System 32 and let launch.py start it again (it watches for the marker)."""
    try:
        open(_RESTART_MARK, "w").close()
    except Exception:
        pass

    def _go():
        time.sleep(2.0)                   # let the HTTP reply reach the page
        os._exit(0)
    threading.Thread(target=_go, daemon=True).start()


def releases(refresh=False):
    """The last three published releases, newest first, from GitHub:
    [{version, tag, date, notes, page, zip, installed, newer}] — or {"error"}."""
    import s32_config as _cfg
    if CONFIG.get("update_check", True) is False:
        return {"error": "Update checks are off (\"update_check\": false in config.json), so the "
                         "changelog isn't fetched."}
    if _cfg.airplane():
        return {"error": "Airplane mode is on — the changelog comes from GitHub, so it needs the internet."}
    if not refresh and _releases["list"] and time.time() - _releases["at"] < _LIST_TTL:
        return {"releases": _mark(_releases["list"]), "current": VERSION}
    try:
        from s32_web import _request
    except Exception as e:
        return {"error": f"web layer unavailable: {e}"}
    body, resp, err = _request("GET", f"https://api.github.com/repos/{REPO}/releases?per_page=15",
                               headers={"Accept": "application/vnd.github+json"})
    code = getattr(resp, "status_code", None)
    if err or code != 200:
        return {"error": f"Couldn't read the releases from GitHub ({err or code})."}
    try:
        data = json.loads(body or "[]")
    except ValueError:
        return {"error": "GitHub's answer couldn't be read."}
    out = []
    for d in data if isinstance(data, list) else []:
        tag = str(d.get("tag_name") or "")
        if d.get("draft") or d.get("prerelease") or not tag:
            continue
        out.append({"version": tag.lstrip("vV"), "tag": tag,
                    "date": str(d.get("published_at") or "")[:10],
                    "notes": str(d.get("body") or "").strip()[:4000],
                    "page": str(d.get("html_url") or ""), "zip": str(d.get("zipball_url") or "")})
    out.sort(key=lambda r: _ver(r["version"]), reverse=True)
    _releases.update(list=out[:3], at=time.time(), error="")
    return {"releases": _mark(_releases["list"]), "current": VERSION}


def _mark(lst):
    return [dict(r, installed=(_ver(r["version"]) == _ver(VERSION)),
                 newer=_newer(r["version"], VERSION)) for r in lst]


def _install(version, zip_url, verb):
    """Download a release and put its files in place — the ONE install path for Update and
    Roll back. No backup is kept (2026-11-07); any listed release can be installed again."""
    with _lock:
        if _state["busy"]:
            return {"error": "An update is already running."}
        _state.update(busy=True, done="", error="")
    tmp = os.path.join(_script_dir, ".update_download.zip")
    try:
        log(f"[S32][UPDATE] {verb}: downloading System 32 {version}…")
        _download(zip_url, tmp)
        plan = _plan(tmp)
        with zipfile.ZipFile(tmp) as z:
            for m, dest in plan:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                part = dest + ".update_part"
                with z.open(m) as src, open(part, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(part, dest)
        try:
            with open(_DONE_MARK, "w", encoding="utf-8") as f:
                json.dump({"from": VERSION, "to": version, "verb": verb}, f)
        except Exception:
            pass
        log(f"[S32][UPDATE] {verb}: {VERSION} → {version} ({len(plan)} files) — restarting")
        _state.update(done=version, busy=False)
        _restart_soon()
        return {"ok": True, "version": version, "files": len(plan), "restarting": True}
    except Exception as e:
        _state.update(busy=False, error=str(e)[:200])
        log(f"[S32][UPDATE] {verb} failed — nothing was replaced: {e}")
        return {"error": f"System 32 {version} didn't install: {str(e)[:200]}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def apply(version):
    """The update banner's Update button."""
    if not _state["latest"] or str(version) != _state["latest"] or not _newer(version, VERSION):
        return {"error": "That update isn't available (check again later)."}
    return _install(version, _state["zip"], "update")


def install(version):
    """The changelog's Roll back / Install button — only a release GitHub listed."""
    r = releases()
    if r.get("error"):
        return r
    hit = next((x for x in r["releases"] if x["version"] == str(version)), None)
    if not hit:
        return {"error": f"{version} isn't one of the releases listed on GitHub."}
    if hit["installed"]:
        return {"error": f"{version} is the version you have."}
    return _install(hit["version"], hit["zip"], "roll back" if not hit["newer"] else "update")


def rollback(version=""):
    """Kept for the old route: installs the given listed release."""
    return install(version) if version else {"error": "Pick a version in the changelog."}


def start():
    # Just updated / rolled back? One note in the chat (no lingering banner — 2026-11-07).
    try:
        with open(_DONE_MARK, encoding="utf-8") as f:
            done = json.load(f)
        os.remove(_DONE_MARK)
        if _ver(done.get("to")) == _ver(VERSION):
            from s32_config import state
            what = "Rolled back" if done.get("verb") == "roll back" else "Updated"
            state["startup_notification"] = (f"✓ {what} to System 32 {VERSION} (from {done.get('from')}). "
                                             f"Earlier versions are in ☰ → DATA · SAVE · RESET → CHANGELOG.")
            log(f"[S32][UPDATE] running {VERSION} ({what.lower()} from {done.get('from')})")
    except FileNotFoundError:
        pass
    except Exception:
        pass
    # Backups from earlier versions aren't used any more — free the space.
    if os.path.isdir(_BACKUP_DIR):
        try:
            shutil.rmtree(_BACKUP_DIR)
            log("[S32][UPDATE] removed the old update backups (rolling back now downloads the version from GitHub)")
        except Exception as e:
            log(f"[S32][UPDATE] couldn't remove the old update backups: {e}")
    if CONFIG.get("update_check", True) is False:
        log("[S32][UPDATE] update check is off (config.json)")
        return

    def _loop():
        time.sleep(_FIRST_CHECK_DELAY)
        while True:
            try:
                check()
            except Exception as e:
                _state["error"] = str(e)[:200]
            time.sleep(_CHECK_EVERY)
    threading.Thread(target=_loop, daemon=True, name="s32-update").start()
