#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
System 32 — Single File Launcher
Double-click this. Everything else is automatic.

This file:
1. Finds or downloads Python
2. Installs required packages
3. Runs bootstrap.py to discover and configure the system
4. Launches system32.py

No batch file needed. No manual steps.
"""

import os
import sys
import subprocess
import urllib.request
import json
import time
import webbrowser
from pathlib import Path

SCRIPT_DIR  = Path(__file__).parent.absolute()
BOOTSTRAP   = SCRIPT_DIR / "bootstrap.py"
SYSTEM32    = SCRIPT_DIR / "system32.py"
CONFIG_FILE = SCRIPT_DIR / "config.json"
PYTHON_DIR  = SCRIPT_DIR / "python"

def _airplane_on():
    """Airplane mode, as System 32 saved it (reference material\\airplane_mode.json).
    Read here because this runs BEFORE System 32 — its pip checks/installs and downloads
    must not reach the internet either (2026-10-31)."""
    import json as _j
    here = os.path.dirname(os.path.abspath(__file__))
    refs = []
    try:
        with open(os.path.join(here, "config.json"), encoding="utf-8") as f:
            r = _j.load(f).get("reference_dir")
            if r:
                refs.append(r)
    except Exception:
        pass
    refs += [os.path.join(os.path.dirname(here), "System 32's reference material"),
             os.path.join(here, "reference material")]
    for ref in refs:
        try:
            with open(os.path.join(ref, "airplane_mode.json"), encoding="utf-8") as f:
                return bool(_j.load(f).get("on"))
        except Exception:
            continue
    return False


AIRPLANE = _airplane_on()
if AIRPLANE:
    # pip honours this everywhere: already-installed packages still check out, nothing is
    # fetched, and a missing one fails at once instead of waiting on timeouts.
    os.environ["PIP_NO_INDEX"] = "1"


BANNER = """
  ==========================================
        S Y S T E M   3 2
    AI-Powered ComfyUI Interface
  ==========================================
"""

def pr(msg):   print(f"  [..] {msg}")
def ok(msg):   print(f"  [OK] {msg}")
def warn(msg): print(f"  [??] {msg}")
def err(msg):  print(f"  [!!] {msg}")


# ── Step 1: Find Python ────────────────────────────────────────────────────────

def find_python():
    """Find the best available Python. Returns path or None."""
    user    = os.environ.get("USERNAME", "")
    profile = os.environ.get("USERPROFILE", f"C:\\Users\\{user}")
    appdata = os.environ.get("APPDATA", f"{profile}\\AppData\\Roaming")

    candidates = [
        # 1. Bundled portable Python on USB
        str(PYTHON_DIR / "python.exe"),
        # 2. ComfyUI Documents venv
        f"{profile}\\Documents\\ComfyUI\\.venv\\Scripts\\python.exe",
        # 3. ComfyUI AppData venv
        f"{profile}\\AppData\\Local\\Programs\\ComfyUI\\.venv\\Scripts\\python.exe",
    ]

    for path in candidates:
        if os.path.exists(path):
            return path

    # 5. uv-managed Python
    uv_base = os.path.join(appdata, "uv", "python")
    if os.path.exists(uv_base):
        for entry in sorted(os.listdir(uv_base), reverse=True):
            candidate = os.path.join(uv_base, entry, "python.exe")
            if os.path.exists(candidate):
                return candidate

    # 6. System Python (skip Microsoft Store stub)
    for cmd in ["python", "python3"]:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True)
            if r.returncode == 0:
                path = subprocess.run(
                    [cmd, "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True
                ).stdout.strip()
                if path and os.path.exists(path) and "WindowsApps" not in path:
                    return path
        except FileNotFoundError:
            continue

    return None


# ── Step 2: Download portable Python if nothing found ─────────────────────────

def download_portable_python():
    """Download embeddable Python 3.12 to the USB python folder."""
    if AIRPLANE:
        err("No Python found, and airplane mode is on — System 32 can't download one while offline.")
        err("Turn airplane mode off (edit the reference folder's airplane_mode.json to {\"on\": false}) or install Python.")
        return None
    pr("Getting System 32's own portable Python 3.12 (one-time)...")
    url = "https://www.python.org/ftp/python/3.12.7/python-3.12.7-embed-amd64.zip"
    zip_path = SCRIPT_DIR / "python_portable.zip"

    try:
        pr("Downloading Python (this may take a minute)...")
        urllib.request.urlretrieve(url, zip_path)
        ok("Downloaded.")

        pr("Extracting...")
        import zipfile
        PYTHON_DIR.mkdir(exist_ok=True)
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(PYTHON_DIR)
        zip_path.unlink()

        # Enable site-packages
        for fname in os.listdir(PYTHON_DIR):
            if fname.endswith("._pth"):
                pth = PYTHON_DIR / fname
                content = pth.read_text()
                if "#import site" in content:
                    pth.write_text(content.replace("#import site", "import site"))

        # Bootstrap pip
        pip_pyz = PYTHON_DIR / "pip.pyz"
        urllib.request.urlretrieve("https://bootstrap.pypa.io/pip/pip.pyz", pip_pyz)
        subprocess.run(
            [str(PYTHON_DIR / "python.exe"), str(pip_pyz), "install", "pip", "--quiet"],
            capture_output=True
        )
        pip_pyz.unlink(missing_ok=True)

        ok(f"Portable Python ready: {PYTHON_DIR / 'python.exe'}")
        return str(PYTHON_DIR / "python.exe")

    except Exception as e:
        err(f"Could not download Python: {e}")
        err("Install Python from https://python.org or install ComfyUI first.")
        return None


# ── Step 3: Ensure core packages ──────────────────────────────────────────────

def ensure_core_packages(python):
    """Install minimum packages needed to run bootstrap."""
    for pkg, module in [("requests", "requests"), ("pillow", "PIL")]:
        r = subprocess.run([python, "-c", f"import {module}"],
                           capture_output=True)
        if r.returncode != 0:
            pr(f"Installing {pkg}...")
            subprocess.run(
                [python, "-m", "pip", "install", pkg, "--quiet",
                 "--break-system-packages"],
                capture_output=True
            )


def clean_for_fresh_boot():
    """Delete config.json and USB yaml section before every launch for plug and play."""
    # Preserve user preferences before deleting config
    user_prefs = {}
    USER_PREF_KEYS = [
        "lmstudio_model_path", "lmstudio_model", "lmstudio_model_key",
        "inference_backend", "web_port", "reference_dir",
        "unreal_project", "unreal_pinned_version", "unreal_path", "unreal_version_label",
        "web_proxy", "web_proxy_http", "web_proxy_https", "update_check", "update_repo",
    ]
    if CONFIG_FILE.exists():
        try:
            import json as _json
            with open(CONFIG_FILE, "r") as f:
                old_cfg = _json.load(f)
            for k in USER_PREF_KEYS:
                if k in old_cfg:
                    user_prefs[k] = old_cfg[k]
        except Exception:
            pass

    # Delete config.json — bootstrap rewrites it fresh with correct paths
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
            pr("Cleaned config.json for fresh discovery...")
        except Exception:
            pass

    # Write preserved prefs back so bootstrap can read them
    if user_prefs:
        try:
            import json as _json
            with open(CONFIG_FILE, "w") as f:
                _json.dump(user_prefs, f, indent=2)
        except Exception:
            pass

    # Clean the system32_usb section from extra_models_config.yaml
    # so it gets rewritten with the correct drive letter
    import os as _os
    appdata = _os.environ.get("APPDATA", "")
    yaml_path = _os.path.join(appdata, "ComfyUI", "extra_models_config.yaml")
    if _os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Remove system32_usb section — bootstrap rewrites it
            if "system32_usb:" in content:
                lines = content.splitlines()
                new_lines = []
                in_usb = False
                for line in lines:
                    if line.strip().startswith("system32_usb:"):
                        in_usb = True
                        continue
                    if in_usb and line and not line[0].isspace():
                        in_usb = False
                    if not in_usb:
                        new_lines.append(line)
                with open(yaml_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_lines))
                pr("Cleaned yaml USB section for fresh discovery...")
        except Exception:
            pass

def run_bootstrap(python, force=False):
    """Run bootstrap.py to configure the system."""
    if not BOOTSTRAP.exists():
        err(f"bootstrap.py not found at {BOOTSTRAP}")
        err("Make sure all System 32 files are in the same folder as launch.py")
        return False

    cmd = [python, str(BOOTSTRAP)]
    if force:
        cmd.append("--force")

    result = subprocess.run(cmd, cwd=str(SCRIPT_DIR))
    return result.returncode == 0


# ── Step 5: Launch system32.py ────────────────────────────────────────────────

def launch_system32(python):
    """Launch the main system32.py application."""
    if not SYSTEM32.exists():
        err(f"system32.py not found at {SYSTEM32}")
        return False

    # Read web port from config
    port = 7432
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        port = cfg.get("web_port", 7432)
    except Exception:
        pass

    pr(f"Launching System 32 on http://localhost:{port}...")

    # system32.py opens the browser itself — and only when no System 32 tab is already
    # open. Opening one here too gave two windows on every start.

    # Run system32.py — this blocks until it stops
    subprocess.run([python, str(SYSTEM32)], cwd=str(SCRIPT_DIR))
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def heal_bytecode_cache():
    """USB-portability guard. An unclean pull, or a sleep/hibernate mid-write, can
    leave a __pycache__ directory corrupt and unreadable on the NEXT machine — which
    then throws 'X is corrupt and unreadable' the instant something imports through
    it (exactly what bit the pymatting/rembg cache moving desktop<->laptop). Bytecode
    caches are 100% regenerable, so before any package imports run we scan the bundled
    python's site-packages and delete any __pycache__ we cannot read; Python rebuilds
    it on next import. Silent when everything is healthy. Never deletes anything that
    is not a __pycache__, so real package code is always left alone."""
    import shutil
    root = PYTHON_DIR / "Lib" / "site-packages"
    if not root.is_dir():
        return
    bad = []
    def _onerr(e):
        p = getattr(e, "filename", None)
        if p:
            bad.append(p)
    for _dp, _dn, _fn in os.walk(root, onerror=_onerr):
        # numba's cache files (.nbi/.nbc) live in __pycache__ and are pickles: a USB
        # pull can leave one READABLE but full of junk, and numba then crashes the import
        # ("invalid load key", 2026-10-31: pymatting -> rembg). A pickle starts with 0x80.
        if os.path.basename(_dp) != "__pycache__" or _dp in bad:
            continue
        for n in _fn:
            if n.endswith((".nbi", ".nbc")):
                try:
                    with open(os.path.join(_dp, n), "rb") as f:
                        good = f.read(1) == b"\x80"
                except Exception:
                    good = False
                if not good:
                    bad.append(_dp)
                    break
    if not bad:
        return
    healed, stuck = 0, []
    for p in bad:
        sp = str(p)
        if "__pycache__" not in sp:                 # never remove real package code
            warn(f"Unreadable path left untouched (not a cache): {sp}")
            continue
        try:
            shutil.rmtree(p, ignore_errors=False)
            healed += 1
        except Exception as e:
            stuck.append((sp, str(e)[:80]))
    if healed:
        ok(f"Self-heal: cleared {healed} corrupt bytecode cache(s) - Python will rebuild them.")
    for sp, e in stuck:
        warn(f"Corrupt cache couldn't be auto-removed: {sp} ({e}) - run chkdsk on the drive.")


def main():
    print(BANNER)

    force = "--force" in sys.argv
    while True:
        why = _boot_once(force)
        if not why:
            break
        # A factory reset or an update just closed System 32 — boot again, without
        # waiting for Enter, so the open tab reloads into the new start.
        print("\n  [System 32] " + ("Update installed — starting again..." if why == "update"
                                     else "Factory reset complete — starting fresh...") + "\n")
        os.environ["S32_AUTOSTART"] = "1"
    print("\n  [System 32] Stopped.")
    input("  Press Enter to close.")


RESET_MARK = SCRIPT_DIR / ".factory_reset_restart"
UPDATE_MARK = SCRIPT_DIR / ".update_restart"          # s32_update: installed / rolled back


def _boot_once(force):
    """One full boot: clean config → bootstrap → (bootstrap runs system32.py).
    Returns "reset" / "update" if System 32 exited to be started again, else False."""
    for _mark in (RESET_MARK, UPDATE_MARK):
        try:
            _mark.unlink()
        except Exception:
            pass
    # Always clean config and yaml for plug and play
    clean_for_fresh_boot()
    if AIRPLANE:
        warn("Airplane mode is on — setup checks what's installed but downloads nothing.")

    # Find Python. System 32 runs on its OWN Python on the stick: a fresh copy (from the
    # repo) has none, so it's downloaded first — using the computer's Python instead filled
    # that Python with System 32's packages (2026-11-01 repo test). The computer's Python
    # is only a fallback when the download can't happen (offline / airplane mode).
    pr("Locating Python...")
    python = None
    if not (PYTHON_DIR / "python.exe").exists() and not AIRPLANE:
        python = download_portable_python()
    if not python:
        python = find_python()
    if not python:
        python = download_portable_python()
        if not python:
            input("\n  Press Enter to exit.")
            sys.exit(1)
    else:
        ok(f"Python: {python}")

    # USB-portability guard: clear any corrupt bytecode caches before imports run
    heal_bytecode_cache()

    # Ensure core packages
    ensure_core_packages(python)

    # Always run bootstrap — config was deleted for fresh discovery. bootstrap.py hands
    # off to system32.py itself and returns when System 32 exits. (This used to call
    # launch_system32() afterwards too — a SECOND System 32 + browser window every time
    # the first one closed.)
    pr("Running setup...")
    ran = run_bootstrap(python, force=force)
    if RESET_MARK.exists():
        return "reset"
    if UPDATE_MARK.exists():
        return "update"
    if not ran:
        err("System 32 stopped with an error. Check the output above for details.")
    return False


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Stopped.")
        sys.exit(0)
