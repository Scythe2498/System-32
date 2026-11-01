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
settings — none of that is in the repo, so none of it is touched. Before anything is
replaced, the current files are copied to Sys 32's Home\\.update_backup\\<version>-<time>\\,
and "Roll back" puts them back. System 32 then restarts by itself (launch.py).

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
_BACKUP_DIR = os.path.join(_script_dir, ".update_backup")
_DISMISS_FILE = os.path.join(REFERENCE_DIR, "update_dismissed.json") if REFERENCE_DIR else ""
_RESTART_MARK = os.path.join(_script_dir, ".update_restart")
_HOME_NAME = "Sys 32's Home"
# Files an update may never write, whatever the zip holds (they're per-machine or personal).
_NEVER = {"config.json", "ui_prefs.json", "seen.json", "model_settings.json"}

_state = {"latest": None, "notes": "", "zip": "", "page": "", "checked": 0, "error": "",
          "busy": False, "done": "", "updated_from": ""}
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
            "rollback": _last_backup_label(), "updated_from": _state["updated_from"],
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


def _last_backup():
    try:
        dirs = sorted((d for d in os.listdir(_BACKUP_DIR) if os.path.isdir(os.path.join(_BACKUP_DIR, d))),
                      reverse=True)
        return os.path.join(_BACKUP_DIR, dirs[0]) if dirs else ""
    except Exception:
        return ""


def _last_backup_label():
    b = _last_backup()
    if not b:
        return ""
    try:
        with open(os.path.join(b, "manifest.json"), encoding="utf-8") as f:
            return str(json.load(f).get("version") or os.path.basename(b))
    except Exception:
        return os.path.basename(b)


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


def apply(version):
    """The operator pressed Update. Download → back up what will change → replace → restart."""
    with _lock:
        if _state["busy"]:
            return {"error": "An update is already running."}
        if not _state["latest"] or str(version) != _state["latest"] or not _newer(version, VERSION):
            return {"error": "That update isn't available (check again later)."}
        _state.update(busy=True, done="", error="")
    tmp = os.path.join(_script_dir, ".update_download.zip")
    try:
        log(f"[S32][UPDATE] downloading System 32 {version}…")
        _download(_state["zip"], tmp)
        plan = _plan(tmp)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(_BACKUP_DIR, f"{VERSION}-{stamp}")
        os.makedirs(bdir, exist_ok=True)
        home_parent = os.path.dirname(_script_dir)
        saved, added = [], []
        for _m, dest in plan:
            rel = os.path.relpath(dest, home_parent)
            if os.path.isfile(dest):
                b = os.path.join(bdir, "files", rel)
                os.makedirs(os.path.dirname(b), exist_ok=True)
                shutil.copy2(dest, b)
                saved.append(rel)
            else:
                added.append(rel)
        with open(os.path.join(bdir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({"version": VERSION, "to": version, "saved": saved, "added": added,
                       "when": stamp}, f, indent=2)
        with zipfile.ZipFile(tmp) as z:
            for m, dest in plan:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                part = dest + ".update_part"
                with z.open(m) as src, open(part, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(part, dest)
        log(f"[S32][UPDATE] updated {VERSION} → {version} ({len(plan)} files; backup: {bdir}) — restarting")
        _state.update(done=version, busy=False)
        _restart_soon()
        return {"ok": True, "version": version, "files": len(plan), "restarting": True}
    except Exception as e:
        _state.update(busy=False, error=str(e)[:200])
        log(f"[S32][UPDATE] update failed — nothing was replaced: {e}")
        return {"error": f"The update didn't install: {str(e)[:200]}"}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def rollback():
    """Put back the files the last update replaced (and remove the ones it added)."""
    b = _last_backup()
    if not b:
        return {"error": "There's no earlier version to roll back to."}
    try:
        with open(os.path.join(b, "manifest.json"), encoding="utf-8") as f:
            man = json.load(f)
        home_parent = os.path.dirname(_script_dir)
        for rel in man.get("saved", []):
            src = os.path.join(b, "files", rel)
            dest = os.path.join(home_parent, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(src, dest)
        for rel in man.get("added", []):
            try:
                os.remove(os.path.join(home_parent, rel))
            except OSError:
                pass
        shutil.rmtree(b, ignore_errors=True)
        log(f"[S32][UPDATE] rolled back to {man.get('version')} — restarting")
        _restart_soon()
        return {"ok": True, "version": man.get("version"), "restarting": True}
    except Exception as e:
        return {"error": f"Roll back failed: {e}"}


def start():
    # Just updated? (the last backup was made by an update TO this version) — say so once,
    # with the way back.
    b = _last_backup()
    if b:
        try:
            with open(os.path.join(b, "manifest.json"), encoding="utf-8") as f:
                man = json.load(f)
            if str(man.get("to")) == VERSION:
                _state["updated_from"] = str(man.get("version") or "")
                log(f"[S32][UPDATE] running {VERSION} (updated from {_state['updated_from']})")
        except Exception:
            pass
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
