# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
System 32 — Bootstrap / First-Run Setup
Runs before system32.py. On first run, discovers all dependencies and writes
config.json. On subsequent runs, validates config and passes through immediately.

Usage (called from System32.bat):
    python bootstrap.py [--force]   # --force re-runs setup even if config exists
"""

import json
import os
import subprocess
import sys
import time
import glob
import winreg

# ── Paths ─────────────────────────────────────────────────────────────────────

SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(SCRIPT_DIR, "config.json")
SYSTEM32_PY  = os.path.join(SCRIPT_DIR, "system32.py")

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


# Fixed paths that don't need discovery
_DEFAULT_PACKAGES = [
    "pillow", "requests", "comfy-aimdo",
    "comfyui-frontend-package", "psutil", "tqdm"
]

_MODULE_MAP = {
    "pillow":                   "PIL",
    "comfy-aimdo":              "comfy_aimdo",
    "comfyui-frontend-package": "comfyui_frontend_package",
    "PySocks":                  "socks",
    "opencv-python":            "cv2",
}

# Packages checked by "is it installed" (pip's own record) instead of "does it
# import". rembg imports onnxruntime, which only comes with rembg[cpu]/[gpu] -
# without it `import rembg` fails forever and the check re-ran pip every boot.
# A fresh machine still has no record -> still installs.
_DIST_CHECK = {"rembg"}

def _pkg_present(python_exe, pkg):
    if pkg in _DIST_CHECK:
        code = f"import importlib.metadata as m; m.version({pkg!r})"
    else:
        code = f"import {_MODULE_MAP.get(pkg, pkg.replace('-', '_').lower())}"
    return subprocess.run([python_exe, "-c", code], capture_output=True).returncode == 0

def _load_requirements():
    req_file = os.path.join(SCRIPT_DIR, "requirements.txt")
    if not os.path.exists(req_file):
        return _DEFAULT_PACKAGES
    packages = []
    with open(req_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                pkg = line.split("[")[0].split("==")[0].split(">=")[0].split("<=")[0].strip()
                if pkg:
                    packages.append(pkg)
    return packages or _DEFAULT_PACKAGES

def _default_output_base():
    """Output folder lives next to Sys 32's Home on the USB drive."""
    # Go up one level from Sys 32's Home to get the USB root
    usb_root = os.path.dirname(SCRIPT_DIR)
    return os.path.join(usb_root, "System 32's Output")

_out = _default_output_base()

FIXED = {
    "lmstudio_model_key": "local-model",
    "lmstudio_model":     "local-model",
    "inference_backend":  "llamacpp",
    "lmstudio_context_length": 131072,
    "lmstudio_gpu_layers": 20,  # safe default — use model switcher to tune per model
    "lmstudio_host":      "http://localhost:1234",
    "comfy_image_host":   "http://127.0.0.1:8188",
    "comfy_video_host":   "http://127.0.0.1:8000",
    "out_images":      os.path.join(_out, "generated images"),
    "out_videos":      os.path.join(_out, "generated videos"),
    "out_3d":          os.path.join(_out, "generated 3d"),
    "out_checkpoints": os.path.join(_out, "checkpoints"),
    "out_dataset":     os.path.join(_out, "dataset"),
    "out_diffusion":   os.path.join(_out, "diffusion_models"),
    "out_loras":       os.path.join(_out, "loras"),
    "web_port":           7432,
}

# ── Printing ───────────────────────────────────────────────────────────────────

def pr(msg, symbol=".."):
    print(f"  [{symbol}] {msg}")

def ok(msg):
    print(f"  [OK] {msg}")

def warn(msg):
    print(f"  [??] {msg}")

def err(msg):
    print(f"  [!!] {msg}")

def header(title):
    print(f"\n  {'─'*44}")
    print(f"  {title}")
    print(f"  {'─'*44}")

# ── Dependency install ─────────────────────────────────────────────────────────

REQUIRED_PACKAGES = ["pillow", "requests"]

def _pip_available(python_exe):
    r = subprocess.run([python_exe, "-m", "pip", "--version"],
                       capture_output=True)
    return r.returncode == 0

def _python_works(python_exe):
    """True only if this Python actually RUNS. A venv launcher file can exist while
    its base interpreter (the Python that built the venv) is gone — e.g. a USB venv
    created against a uv/Documents Python on a since-wiped C:. os.path.exists() is
    True, but every call through it dies with 'No Python at <base path>' and a
    non-zero exit. Probe it — same lesson as the STT transcribe-probe: don't trust
    that a thing is alive just because its file is present."""
    if not python_exe or not os.path.exists(python_exe):
        return False
    try:
        r = subprocess.run([python_exe, "-c", "import sys"],
                           capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False

def _bootstrap_pip(python_exe):
    """Install pip into a portable Python that doesn't have it yet."""
    pr("pip not found — bootstrapping pip...")
    pip_pyz = os.path.join(os.path.dirname(python_exe), "pip.pyz")
    if not os.path.exists(pip_pyz):
        # Download pip.pyz
        try:
            import urllib.request
            _download("https://bootstrap.pypa.io/pip/pip.pyz", pip_pyz)
        except Exception as e:
            err(f"Could not download pip: {e}")
            return False
    r = subprocess.run([python_exe, pip_pyz, "install", "pip", "--quiet"],
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok("pip installed")
        return True
    err(f"pip bootstrap failed: {r.stderr[:200]}")
    return False

def _patch_pth(python_exe):
    """
    Embeddable Python blocks site-packages via ._pth file.
    Uncomment 'import site' so pip-installed packages are importable.
    """
    python_dir = os.path.dirname(python_exe)
    for fname in os.listdir(python_dir):
        if fname.endswith("._pth"):
            path = os.path.join(python_dir, fname)
            try:
                with open(path, "r") as f:
                    content = f.read()
                if "#import site" in content:
                    with open(path, "w") as f:
                        f.write(content.replace("#import site", "import site"))
                    ok(f"Patched {fname} (enabled site-packages)")
            except Exception as e:
                warn(f"Could not patch {fname}: {e}")

def ensure_packages(python_exe):
    header("Checking Python dependencies")

    # Portable Python needs site-packages enabled and pip bootstrapped
    _patch_pth(python_exe)
    if not _pip_available(python_exe):
        if not _bootstrap_pip(python_exe):
            err("Cannot install packages without pip. Aborting.")
            return

    packages = _load_requirements()
    req_file = os.path.join(SCRIPT_DIR, "requirements.txt")
    pr(f"{'From requirements.txt' if os.path.exists(req_file) else 'Core packages'} ({len(packages)} packages)...")

    for pkg in packages:
        if _pkg_present(python_exe, pkg):
            ok(f"{pkg} ready")
        else:
            pr(f"Installing {pkg}...")
            r = subprocess.run(
                [python_exe, "-m", "pip", "install", pkg, "--quiet"],
                capture_output=True, text=True
            )
            if r.returncode == 0:
                ok(f"{pkg} installed")
            else:
                # Retry with --break-system-packages for managed environments
                r2 = subprocess.run(
                    [python_exe, "-m", "pip", "install", pkg,
                     "--quiet", "--break-system-packages"],
                    capture_output=True, text=True
                )
                if r2.returncode == 0:
                    ok(f"{pkg} installed")
                else:
                    warn(f"{pkg} install failed — some features may be unavailable")

    # Check for git (needed for custom node installs) — auto-install if missing.
    header("Checking git")
    git_exe = _ensure_git()
    if git_exe:
        ok(f"git ready: {git_exe}")
    else:
        warn("git unavailable — custom node auto-install won't work until git is installed.\n"
             "       Install git from https://git-scm.com/download/win")

def ensure_runtime_packages(python_exe):
    """Install System 32's OWN runtime deps into the BUNDLED python that
    Start.bat actually launches — separate from the ComfyUI venv.

    Without this, requirements.txt only lands in the venv, so `import psutil`
    (RAM stats, awareness) fails at runtime even though setup said "ready".
    Embeddable Python also ignores site-packages until its ._pth is patched."""
    header("Runtime deps (System 32's own python)")
    _patch_pth(python_exe)
    if not _pip_available(python_exe):
        if not _bootstrap_pip(python_exe):
            warn("Could not set up pip for the bundled python — psutil/RAM stats "
                 "may be unavailable. (The firewall's netstat fallback still works.)")
            return
    packages = _load_requirements() + ["PySocks"]   # PySocks: SOCKS/Tor proxy support
    pr(f"Installing {len(packages)} package(s) into the bundled python...")
    for pkg in packages:
        if _pkg_present(python_exe, pkg):
            ok(f"{pkg} ready")
            continue
        pr(f"Installing {pkg}...")
        r = subprocess.run([python_exe, "-m", "pip", "install", pkg, "--quiet"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            r = subprocess.run([python_exe, "-m", "pip", "install", pkg,
                                "--quiet", "--break-system-packages"],
                               capture_output=True, text=True)
        if r.returncode == 0:
            ok(f"{pkg} installed")
        else:
            warn(f"{pkg} install failed — some features may be unavailable")
    _pending_ytdlp_update(python_exe)


def _pending_ytdlp_update(python_exe):
    """Finish a video-downloader update System 32 couldn't do while running (a file it
    needed was in use — Windows locks loaded .pyd files). Runs here, before System 32
    starts, so nothing is locked. Left in place if it fails, so the next start retries."""
    mark = os.path.join(SCRIPT_DIR, ".pending_ytdlp_update")
    if not os.path.exists(mark) or AIRPLANE:
        return
    spec = "yt-dlp[default,curl-cffi]"        # fixed: the marker's contents are never trusted
    pr("Finishing the video downloader update...")
    r = subprocess.run([python_exe, "-m", "pip", "install", "-U", spec, "--quiet",
                        "--break-system-packages"], capture_output=True, text=True)
    if r.returncode == 0:
        ok("Video downloader updated")
        try:
            os.remove(mark)
        except Exception:
            pass
    else:
        warn("Video downloader update failed — it will try again next start")


# ── Python / venv discovery ────────────────────────────────────────────────────

VENV_SEARCH = [
    r"C:\Users\{user}\Documents\ComfyUI\.venv\Scripts\python.exe",
    r"C:\Users\{user}\AppData\Local\Programs\ComfyUI\.venv\Scripts\python.exe",
    r"C:\ComfyUI\.venv\Scripts\python.exe",
    r"D:\ComfyUI\.venv\Scripts\python.exe",
    r"E:\ComfyUI\.venv\Scripts\python.exe",
    r"H:\ComfyUI\.venv\Scripts\python.exe",
]

def find_python():
    header("Locating Python / venv")
    user = os.environ.get("USERPROFILE", "")

    # Search known venv locations
    for pattern in VENV_SEARCH:
        path = pattern.replace("{user}", user)
        if os.path.exists(path):
            ok(f"Found venv: {path}")
            return path

    # Search all drives for ComfyUI venvs
    pr("Scanning drives for ComfyUI venv...")

    # Check USB venv first — fastest path. Must actually RUN, not just exist: a USB
    # venv built against an off-drive base Python (uv/Documents on C:) is a dead
    # launcher on any other machine.
    usb_venv = os.path.join(SCRIPT_DIR, "ComfyUI", ".venv", "Scripts", "python.exe")
    if _python_works(usb_venv):
        ok(f"Found venv python: {usb_venv}")
        return usb_venv

    # Check known locations before doing a full drive scan
    profile = os.environ.get("USERPROFILE", "")
    known = [
        os.path.join(profile, "Documents", "ComfyUI", ".venv", "Scripts", "python.exe"),
        os.path.join(profile, "AppData", "Local", "Programs", "ComfyUI", ".venv", "Scripts", "python.exe"),
    ]
    for path in known:
        if os.path.exists(path):
            ok(f"Found venv python: {path}")
            return path

    # Full scan as last resort — skip known large/irrelevant directories
    SKIP_DIRS = {
        "windows", "winsxs", "$recycle.bin", "system volume information",
        "programdata", "recovery", ".unsloth", "node_modules", "__pycache__",
        "steam", "steamapps", "epic games", "program files", "program files (x86)",
    }
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        base = f"{letter}:\\"
        if not os.path.exists(base):
            continue
        for root, dirs, files in os.walk(base, topdown=True):
            dirs[:] = [d for d in dirs if d.lower() not in SKIP_DIRS]
            if "python.exe" in files and ".venv" in root.lower():
                path = os.path.join(root, "python.exe")
                if _python_works(path):
                    ok(f"Found venv python: {path}")
                    return path
            if len(root.split(os.sep)) > 8:
                dirs.clear()

    # System 32's own portable Python on the stick comes BEFORE the computer's Python:
    # installing into the host's Python leaves packages behind on someone else's PC
    # (2026-10-31 repo test: it used the laptop's Python 3.12 and filled it).
    bundled = os.path.join(SCRIPT_DIR, "python", "python.exe")
    if _python_works(bundled):
        ok(f"Using System 32's own Python: {bundled}")
        return bundled

    # Fall back to system Python
    for cmd in ["python", "python3"]:
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True)
            if r.returncode == 0:
                path = subprocess.run(
                    [cmd, "-c", "import sys; print(sys.executable)"],
                    capture_output=True, text=True
                ).stdout.strip()
                warn(f"No venv found, using system Python: {path}")
                return path
        except FileNotFoundError:
            continue

    # Last resort: the bundled portable Python always exists on the drive and always
    # runs. It can't host ComfyUI (embeddable — no venv module), but it keeps
    # ensure_packages and the rest of bootstrap from aborting on a bare machine where
    # no full/venv Python is present yet. ComfyUI is handled separately by
    # _ensure_usb_comfy_venv, which rebuilds its own venv when a full Python appears.
    bundled = os.path.join(SCRIPT_DIR, "python", "python.exe")
    if _python_works(bundled):
        warn(f"No working full/venv Python found — using bundled portable Python: {bundled}")
        return bundled

    err("Could not find Python. Please install Python 3.10+ or ComfyUI.")
    return None

# ── LM Studio discovery ────────────────────────────────────────────────────────

LMS_SEARCH = [
    os.path.join(os.environ.get("USERPROFILE", ""), ".lmstudio", "bin", "lms.exe"),
    os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "LM-Studio", "bin", "lms.exe"),
    os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "Local", "Programs", "LM-Studio", "bin", "lms.exe"),
    r"C:\Program Files\LM-Studio\bin\lms.exe",
    r"C:\Program Files (x86)\LM-Studio\bin\lms.exe",
]

def find_lmstudio():
    header("Locating LM Studio")
    profile = os.environ.get("USERPROFILE", "")

    for path in LMS_SEARCH:
        if os.path.exists(path):
            ok(f"Found LM Studio: {path}")
            return path

    warn("LM Studio not found. You can set the path manually in config.json.")
    return os.path.join(profile, ".lmstudio", "bin", "lms.exe")

# ── ComfyUI discovery ──────────────────────────────────────────────────────────

COMFY_SEARCH_ROOTS = [
    r"C:\Users\{user}\Documents\ComfyUI",
    r"C:\Users\{user}\AppData\Local\Programs\ComfyUI",
    r"C:\ComfyUI",
    r"D:\ComfyUI",
    r"H:\ComfyUI",
]

# Video model filename fingerprints — must appear in actual model filenames
# Kept specific to avoid false positives on folder names
VIDEO_FINGERPRINTS = [
    "ltx", "ltxv", "hunyuan_video", "mochi", "cosmos", "wan2",
    "video_generation", "animatediff", "videocrafter"
]

def _is_video_install(comfy_dir):
    """
    Heuristic — check if this ComfyUI has video-specific model files.
    Only looks at actual filenames, not folder names, to avoid false positives.
    """
    model_dirs = [
        os.path.join(comfy_dir, "models", "checkpoints"),
        os.path.join(comfy_dir, "models", "unet"),
        os.path.join(comfy_dir, "models", "diffusion_models"),
    ]
    for model_dir in model_dirs:
        if not os.path.exists(model_dir):
            continue
        try:
            for f in os.listdir(model_dir):
                fl = f.lower()
                # Only match if fingerprint is in the actual filename
                if any(fp in fl for fp in VIDEO_FINGERPRINTS):
                    return True
        except Exception:
            pass
    return False

def _find_comfy_main(root):
    """Find main.py under a root directory."""
    # Direct
    candidate = os.path.join(root, "main.py")
    if os.path.exists(candidate):
        return os.path.dirname(candidate), candidate

    # One level deep (portable installs)
    for sub in ["ComfyUI", "resources/ComfyUI", "resources\\ComfyUI"]:
        candidate = os.path.join(root, sub, "main.py")
        if os.path.exists(candidate):
            return os.path.dirname(candidate), candidate

    return None, None

def find_comfyui_installs():
    header("Locating ComfyUI installs")
    user = os.environ.get("USERPROFILE", "")
    found = []  # list of (comfy_dir, main_py, is_video)

    roots = [r.replace("{user}", user) for r in COMFY_SEARCH_ROOTS]

    # Also search all drives shallowly
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        base = f"{letter}\\"
        if not os.path.exists(base):
            continue
        try:
            for top in os.listdir(base):
                candidate_root = os.path.join(base, top)
                if os.path.isdir(candidate_root):
                    roots.append(candidate_root)
        except Exception:
            pass

    seen = set()
    for root in roots:
        if not os.path.exists(root):
            continue
        comfy_dir, main_py = _find_comfy_main(root)
        if comfy_dir and comfy_dir not in seen:
            seen.add(comfy_dir)
            is_vid = _is_video_install(comfy_dir)
            label = "video" if is_vid else "image"
            ok(f"Found ComfyUI ({label}): {comfy_dir}")
            found.append((comfy_dir, main_py, is_vid))

    return found

def _user_dir_for(comfy_dir):
    """Best-guess user directory for a ComfyUI install."""
    user = os.environ.get("USERPROFILE", "")
    # Desktop/portable installs often share a user dir
    candidate = os.path.join(user, "Documents", "ComfyUI")
    if os.path.exists(candidate):
        return candidate
    return comfy_dir  # fallback: same as install dir

# ── Config assembly ────────────────────────────────────────────────────────────

def build_config(python_exe, lms_exe, comfy_installs):
    header("Building configuration")
    user = os.environ.get("USERPROFILE", "")

    cfg = dict(FIXED)
    cfg["lms_exe"] = lms_exe

    # Model path logging happens after preserved values are applied in main()

    # Workflow JSON directory — USB comfyui resources takes priority
    usb_res_wf = _find_usb_comfyui_resources()
    usb_json   = os.path.join(usb_res_wf, "JsonFiles") if usb_res_wf else None
    if usb_json and os.path.exists(usb_json):
        cfg["workflow_dir"] = usb_json
    else:
        json_dir = os.path.join(SCRIPT_DIR, "JsonFiles")
        os.makedirs(json_dir, exist_ok=True)
        cfg["workflow_dir"] = json_dir

    # Use USB ComfyUI venv if it actually RUNS (not just exists — a venv whose base
    # Python is gone is a dead launcher), otherwise the working primary Python.
    usb_comfy_venv = os.path.join(SCRIPT_DIR, "ComfyUI", ".venv", "Scripts", "python.exe")
    comfy_python = usb_comfy_venv if _python_works(usb_comfy_venv) else python_exe
    cfg["comfy_image_python"] = comfy_python
    cfg["comfy_video_python"]  = comfy_python

    image_install = None
    video_install = None

    # USB ComfyUI always wins as image install if present
    usb_comfy_dir  = os.path.join(SCRIPT_DIR, "ComfyUI")
    usb_comfy_main = os.path.join(usb_comfy_dir, "main.py")
    if os.path.exists(usb_comfy_main):
        image_install = (usb_comfy_dir, usb_comfy_main)
        # Remaining installs are candidates for video
        remaining = [i for i in comfy_installs
                     if os.path.normpath(i[0]) != os.path.normpath(usb_comfy_dir)]
    else:
        remaining = comfy_installs

    if not image_install:
        # Sort: prefer Documents for image
        def _install_score_video(item):
            d = item[0].lower()
            return (0 if "appdata" in d or "programs" in d else 1)

        def _install_score_image(item):
            d = item[0].lower()
            return (0 if "documents" in d else 1)

        video_candidates = sorted([i for i in remaining if i[2]],     key=_install_score_video)
        image_candidates = sorted([i for i in remaining if not i[2]], key=_install_score_image)

        if not image_candidates and len(video_candidates) >= 2:
            video_candidates.sort(key=_install_score_video)
            image_candidates = [video_candidates.pop()]
        elif not video_candidates and len(image_candidates) >= 2:
            image_candidates.sort(key=_install_score_image)
            video_candidates = [image_candidates.pop(0)]

        if video_candidates:
            video_install = (video_candidates[0][0], video_candidates[0][1])
        if image_candidates:
            image_install = (image_candidates[0][0], image_candidates[0][1])
    else:
        # USB is image — find video from remaining
        def _install_score_video(item):
            d = item[0].lower()
            return (0 if "appdata" in d or "programs" in d else 1)
        video_candidates = sorted(remaining, key=_install_score_video)
        if video_candidates:
            video_install = (video_candidates[0][0], video_candidates[0][1])

    # Image ComfyUI
    if image_install:
        comfy_dir, main_py = image_install
        usb_res = _find_usb_comfyui_resources()
        is_usb  = os.path.normpath(comfy_dir) == os.path.normpath(os.path.join(SCRIPT_DIR, "ComfyUI"))
        if is_usb and usb_res:
            out_base = os.path.join(os.path.dirname(SCRIPT_DIR), "System 32's Output")
            cfg["comfy_image_dir"]    = comfy_dir
            cfg["comfy_image_input"]  = os.path.join(usb_res, "input")
            cfg["comfy_image_output"] = os.path.join(out_base, "generated images")
            cfg["user_dir"]           = usb_res
            cfg["models_dir"]         = os.path.join(usb_res, "models")
            cfg["loras_dir"]          = os.path.join(usb_res, "models", "loras")
            cfg["datasets_dir"]       = os.path.join(usb_res, "input", "Datasets")
            cfg["custom_nodes_dir"]   = os.path.join(usb_res, "custom_nodes")
            ok(f"Image ComfyUI: {comfy_dir} (USB resources: {usb_res})")
        else:
            udir = _user_dir_for(comfy_dir)
            cfg["comfy_image_dir"]    = comfy_dir
            cfg["comfy_image_input"]  = os.path.join(udir, "input")
            cfg["comfy_image_output"] = os.path.join(udir, "output")
            cfg["user_dir"]           = udir
            cfg["models_dir"]         = os.path.join(udir, "models")
            cfg["loras_dir"]          = os.path.join(udir, "models", "loras")
            cfg["datasets_dir"]       = os.path.join(udir, "input", "Datasets")
            ok(f"Image ComfyUI: {comfy_dir} (main.py: {main_py})")
    else:
        warn("No image ComfyUI found — using defaults. Edit config.json to fix.")
        udir = os.path.join(user, "Documents", "ComfyUI")
        cfg["comfy_image_dir"]    = udir
        cfg["comfy_image_input"]  = os.path.join(udir, "input")
        cfg["comfy_image_output"] = os.path.join(udir, "output")
        cfg["user_dir"]           = udir
        cfg["models_dir"]         = os.path.join(udir, "models")
        cfg["loras_dir"]          = os.path.join(udir, "models", "loras")
        cfg["datasets_dir"]       = os.path.join(udir, "input", "Datasets")

    # Video ComfyUI
    if video_install:
        comfy_dir, main_py = video_install
        cfg["comfy_video_dir"]  = comfy_dir
        cfg["comfy_video_main"] = main_py
        ok(f"Video ComfyUI:  {comfy_dir}")
    else:
        warn("No video ComfyUI found — using defaults. Edit config.json to fix.")
        cfg["comfy_video_dir"]  = cfg["comfy_image_dir"]
        cfg["comfy_video_main"] = os.path.join(cfg["comfy_image_dir"], "main.py")

    return cfg

# ── extra_models_config.yaml validation and repair ────────────────────────────

def ensure_extra_models_yaml(cfg):
    """
    Find the ComfyUI extra_models_config.yaml and make sure it has
    checkpoint paths defined. If not, patch it in place.
    """
    header("Checking ComfyUI model paths config")
    profile = os.environ.get("USERPROFILE", "")
    appdata = os.environ.get("APPDATA", "")
    udir = cfg.get("user_dir", os.path.join(profile, "Documents", "ComfyUI"))

    candidates = [
        os.path.join(appdata, "ComfyUI", "extra_models_config.yaml"),
        os.path.join(appdata, "ComfyUI", "extra_model_paths.yaml"),
        os.path.join(profile, "AppData", "Roaming", "ComfyUI", "extra_models_config.yaml"),
        os.path.join(profile, "Documents", "ComfyUI", "extra_model_paths.yaml"),
    ]

    yaml_path = next((p for p in candidates if os.path.exists(p)), None)

    if not yaml_path:
        # Create one from scratch
        yaml_path = os.path.join(appdata, "ComfyUI", "extra_models_config.yaml")
        os.makedirs(os.path.dirname(yaml_path), exist_ok=True)
        pr(f"No extra_models_config found — creating: {yaml_path}")
        _write_yaml(yaml_path, udir)
        ok("Created extra_models_config.yaml")
        return

    # Force rewrite if USB ComfyUI is the primary install
    # clears stale desktop paths that no longer exist
    usb_comfy_check = os.path.join(SCRIPT_DIR, "ComfyUI", "main.py")
    if os.path.exists(usb_comfy_check):
        _write_yaml(yaml_path, "")
        ok(f"extra_models_config OK: {yaml_path}")
        return

    # Read and check if checkpoint paths are defined
    try:
        content = open(yaml_path, encoding="utf-8").read()
    except Exception as e:
        warn(f"Could not read {yaml_path}: {e}")
        return

    if "checkpoints" not in content or "latent_upscale_models" not in content:
        # Checkpoint paths missing, or a non-standard folder type (e.g. latent_upscale_models
        # used by LTX video workflows) isn't registered — regenerate so ComfyUI sees it.
        pr(f"extra_models_config missing required model paths — patching: {yaml_path}")
        backup = yaml_path + ".bak"
        try:
            import shutil
            shutil.copy2(yaml_path, backup)
        except Exception:
            pass
        _write_yaml(yaml_path, udir)
        ok(f"Patched extra_models_config.yaml (backup: {backup})")
        return

    # Verify at least one checkpoint path actually contains model files
    import re as _re
    checkpoint_found = False
    for match in _re.finditer(r"base_path:\s*(.+)", content):
        base = match.group(1).strip()
        ckpt_dir = os.path.join(base, "models", "checkpoints")
        if os.path.exists(ckpt_dir):
            try:
                files = [f for f in os.listdir(ckpt_dir)
                         if f.endswith((".safetensors", ".ckpt", ".pt"))]
                if files:
                    checkpoint_found = True
                    break
            except Exception:
                pass
        # Also check USB resource paths
        usb = _find_usb_comfyui_resources()
        if usb:
            usb_ckpt = os.path.join(usb, "models", "checkpoints")
            if os.path.exists(usb_ckpt):
                try:
                    files = [f for f in os.listdir(usb_ckpt)
                             if f.endswith((".safetensors", ".ckpt", ".pt"))]
                    if files:
                        checkpoint_found = True
                        break
                except Exception:
                    pass

    if checkpoint_found:
        ok(f"extra_models_config OK: {yaml_path}")
    else:
        warn(f"extra_models_config found but no checkpoint files detected in any search path.")
        warn(f"Drop .safetensors files into a checkpoints folder or into D:\\Sys 32's comfyui resources\\models\\checkpoints\\")

def _find_usb_comfyui_resources():
    """Find the USB comfyui resources folder dynamically — no hardcoded names."""
    drive = os.path.splitdrive(SCRIPT_DIR)[0] + "\\"
    parent = os.path.dirname(SCRIPT_DIR)
    candidates = [
        os.path.join(drive, "Sys 32's comfyui resources"),
        os.path.join(drive, "comfyui resources"),
        os.path.join(drive, "ComfyUI Resources"),
        os.path.join(drive, "System 32 ComfyUI"),
        os.path.join(parent, "Sys 32's comfyui resources"),
        os.path.join(parent, "comfyui resources"),
        os.path.join(parent, "ComfyUI Resources"),
    ]
    for c in candidates:
        if os.path.exists(c) and os.path.isdir(c):
            return c
    return None

def _write_yaml(path, base_path):
    """Write a complete extra_models_config.yaml with all model paths."""
    user = os.environ.get("USERPROFILE", "")

    # USB Models folder (Sys 32's own model storage)
    usb_models = os.path.join(SCRIPT_DIR, "Models")
    # USB ComfyUI resources folder (detected dynamically)
    usb_comfyui = _find_usb_comfyui_resources()
    usb_section = ""
    if os.path.exists(usb_models):
        usb_section += f"""
system32_usb:
  base_path: {SCRIPT_DIR}
  checkpoints: Models\\checkpoints
  loras: Models\\loras
  vae: Models\\vae
  upscale_models: Models\\upscale_models
  embeddings: Models\\embeddings
  controlnet: Models\\controlnet
  clip: Models\\clip
  diffusion_models: Models\\diffusion_models
  text_encoders: Models\\text_encoders
"""
    if os.path.exists(usb_comfyui):
        usb_section += f"""
system32_resources:
  base_path: {usb_comfyui}
  checkpoints: models/checkpoints
  loras: models/loras
  vae: models/vae
  upscale_models: models/upscale_models
  latent_upscale_models: models/latent_upscale_models
  embeddings: models/embeddings
  controlnet: models/controlnet
  clip: models/clip
  clip_vision: models/clip_vision
  style_models: models/style_models
  gligen: models/gligen
  photomaker: models/photomaker
  unet: models/unet
  diffusion_models: models/diffusion_models
  text_encoders: models/text_encoders
  audio_encoders: models/audio_encoders
  custom_nodes: custom_nodes/
  input: input/
"""

    appdata = os.environ.get("APPDATA", "")
    appdata_nodes = os.path.join(
        os.environ.get("LOCALAPPDATA", appdata),
        "Programs", "ComfyUI", "resources", "ComfyUI", "custom_nodes"
    )

    # Only include desktop section if base_path exists
    desktop_section = ""
    if base_path and os.path.exists(base_path):
        desktop_cn = os.path.join(base_path, "custom_nodes")
        cn_line = "  custom_nodes: custom_nodes/\n" if os.path.exists(desktop_cn) else ""
        desktop_section = (
            f"comfyui_desktop:\n"
            f"  is_default: \"true\"\n"
            f"  base_path: {base_path}\n"
            f"  checkpoints: models/checkpoints\n"
            f"  loras: models/loras\n"
            f"  vae: models/vae\n"
            f"  upscale_models: models/upscale_models\n"
            f"  embeddings: models/embeddings\n"
            f"  controlnet: models/controlnet\n"
            f"  clip: models/clip\n"
            f"  diffusion_models: models/diffusion_models\n"
            f"  text_encoders: models/text_encoders\n"
            f"{cn_line}"
            f"  download_model_base: models\n"
        )

    # Only include appdata extensions if they exist
    ext_section = ""
    if os.path.exists(appdata_nodes):
        ext_section = f"desktop_extensions:\n  custom_nodes: {appdata_nodes}\n"

    yaml_content = (
        f"# ComfyUI extra_model_paths — written by System 32 bootstrap\n"
        f"{desktop_section}"
        f"{ext_section}"
        f"{usb_section}"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(yaml_content)



def validate_config(cfg):
    """Check that critical paths in an existing config still exist."""
    issues = []
    checks = [
        ("comfy_image_dir",    "Image ComfyUI dir"),
        ("comfy_video_dir",    "Video ComfyUI dir"),
        ("comfy_image_python", "Image Python venv"),
    ]
    for key, label in checks:
        path = cfg.get(key, "")
        if path and not os.path.exists(path):
            issues.append(f"{label} missing: {path}")

    # Check main.py specifically
    img_dir = cfg.get("comfy_image_dir", "")
    if img_dir and not os.path.exists(os.path.join(img_dir, "main.py")):
        issues.append(f"Image ComfyUI main.py not found in: {img_dir}")
    vid_main = cfg.get("comfy_video_main", "")
    if vid_main and not os.path.exists(vid_main):
        issues.append(f"Video ComfyUI main.py not found: {vid_main}")

    return issues

# ── Output folder creation ─────────────────────────────────────────────────────

def write_code_index(cfg):
    """
    Write code_index.md — maps all functions across all .py files in Sys 32's Home.
    Regenerated on every boot so new files are always included automatically.
    """
    ref_dir = os.path.join(os.path.dirname(SCRIPT_DIR), "System 32's reference material")
    if not os.path.exists(ref_dir):
        ref_dir = os.path.join(SCRIPT_DIR, "reference material")

    try:
        import ast as _ast
    except Exception:
        return

    role_map = {
        'detect_hardware': 'HARDWARE', 'get_hardware': 'HARDWARE',
        'hardware_context_length': 'HARDWARE', 'hardware_workflow_defaults': 'HARDWARE',
        '_hw_summary': 'HARDWARE', '_get_machine_preferences': 'HARDWARE',
        'start_comfy_image': 'COMFYUI', 'start_comfy_video': 'COMFYUI',
        'is_up': 'COMFYUI', 'comfy_queue': 'COMFYUI', 'comfy_wait': 'COMFYUI',
        'check_missing_nodes': 'COMFYUI',
        '_wf_load': 'WORKFLOW', '_wf_list': 'WORKFLOW', '_wf_patch': 'WORKFLOW',
        'wf_run_tool': 'WORKFLOW', 'run_image': 'WORKFLOW', 'run_video': 'WORKFLOW',
        'run_lora': 'WORKFLOW', 'fetch_checkpoints': 'WORKFLOW',
        'fetch_lora_options': 'WORKFLOW',
        'index_search': 'NAVIGATION', 'explore_tree': 'NAVIGATION',
        'fs_list': 'NAVIGATION', 'fs_find': 'NAVIGATION', 'fs_drives': 'NAVIGATION',
        'fs_read': 'NAVIGATION', 'show_file': 'NAVIGATION', 'write_file': 'NAVIGATION',
        '_get_desktop_path': 'NAVIGATION',
        'memory_write': 'MEMORY', 'memory_read': 'MEMORY', 'memory_search': 'MEMORY',
        'save_directory_map': 'MEMORY', '_write_session_log': 'MEMORY',
        '_compress_old_memories': 'MEMORY', '_rebuild_memory_index': 'MEMORY',
        '_memory_snippet': 'MEMORY', '_index_snippet': 'MEMORY',
        '_load_identity': 'MEMORY', '_update_identity': 'MEMORY',
        '_detect_operator': 'MEMORY', '_load_operator': 'MEMORY',
        'set_preference': 'MEMORY',
        'mark_favorite': 'IMAGES', 'get_favorites': 'IMAGES',
        'fs_show_images': 'IMAGES', 'visual_search': 'IMAGES',
        'build_visual_index': 'IMAGES',
        'build_system_prompt': 'CORE', 'chat': 'CORE',
        'dispatch_tool': 'CORE', '_tool_result_summary': 'CORE',
        'startup': 'CORE', 'main': 'CORE',
    }

    lines = [
        "# System 32 — Code Index",
        "*Auto-generated by bootstrap on every boot.*",
        "*All .py files in Sys 32's Home are indexed — new files appear automatically.*",
        "",
        "Use read_file(path) with a line range to read any function directly.",
        "",
    ]

    total_fns = 0
    py_files = sorted([
        f for f in os.listdir(SCRIPT_DIR)
        if f.endswith('.py') and not f.startswith('__')
    ])

    for py_file in py_files:
        script_path = os.path.join(SCRIPT_DIR, py_file)
        try:
            content = open(script_path, "r", encoding="utf-8").read()
            # A file with a stray '\S'-style escape made ast print a bare
            # '<unknown>:1: SyntaxWarning' every boot. Parse quietly, and name the file.
            import warnings as _w
            with _w.catch_warnings(record=True) as _caught:
                _w.simplefilter("always")
                tree = _ast.parse(content, filename=py_file)
            for _cw in _caught:
                if issubclass(_cw.category, SyntaxWarning):
                    warn(f"Code index: {_cw.message} in {os.path.basename(str(_cw.filename))} line {_cw.lineno} (harmless; worth a raw string)")
                    break
        except Exception:
            continue

        fns_in_file = [
            (node.lineno, node.name, (_ast.get_docstring(node) or "").split('.')[0][:80])
            for node in _ast.walk(tree)
            if isinstance(node, _ast.FunctionDef) and node.col_offset == 0
        ]
        if not fns_in_file:
            continue

        total_fns += len(fns_in_file)
        lines.append(f"## {py_file}")
        lines.append(f"*{script_path}*")
        lines.append("")

        if py_file == "system32.py":
            # Group by role for the main file
            groups = {}
            for lineno, name, doc in fns_in_file:
                group = role_map.get(name, 'OTHER')
                groups.setdefault(group, []).append((lineno, name, doc))
            order = ['CORE','HARDWARE','COMFYUI','WORKFLOW',
                     'NAVIGATION','MEMORY','IMAGES','OTHER']
            for group in order:
                fns = sorted(groups.get(group, []))
                if not fns:
                    continue
                lines.append(f"### {group}")
                for lineno, name, doc in fns:
                    prefix = "→" if not name.startswith('_') else " "
                    desc = f" — {doc}" if doc else ""
                    lines.append(f"{prefix} `{name}` L{lineno}{desc}")
                lines.append("")
        else:
            # Flat list for other files
            for lineno, name, doc in sorted(fns_in_file):
                prefix = "→" if not name.startswith('_') else " "
                desc = f" — {doc}" if doc else ""
                lines.append(f"{prefix} `{name}` L{lineno}{desc}")
            lines.append("")

    lines.insert(3, f"*{total_fns} total functions across {len(py_files)} files.*")

    try:
        index_path = os.path.join(ref_dir, "code_index.md")
        with open(index_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        ok(f"Code index written: {index_path} ({total_fns} functions, {len(py_files)} files)")
    except Exception as e:
        warn(f"Could not write code index: {e}")

def write_index(cfg):
    """
    Write index.md to the reference material folder.
    Machine-aware — gets rewritten on every bootstrap so paths are always current.
    It reads this to orient itself without hunting for things.
    """
    ref_dir = os.path.join(os.path.dirname(SCRIPT_DIR), "System 32's reference material")
    if not os.path.exists(ref_dir):
        ref_dir = os.path.join(SCRIPT_DIR, "reference material")
        os.makedirs(ref_dir, exist_ok=True)

    index_path = os.path.join(ref_dir, "index.md")
    user = os.environ.get("USERPROFILE", "")

    # Collect custom node list
    custom_nodes = []
    nodes_dir = os.path.join(cfg.get("comfy_image_dir", ""), "custom_nodes")
    if os.path.exists(nodes_dir):
        for n in sorted(os.listdir(nodes_dir)):
            if os.path.isdir(os.path.join(nodes_dir, n)) and not n.startswith("."):
                custom_nodes.append(n)

    content = f"""# System 32 — Index
*Auto-generated by bootstrap. Rewritten each session. Add your own notes below the divider.*

## This Machine
- **User:** {user}
- **Computer:** {os.environ.get('COMPUTERNAME', 'unknown')}
- **Script home:** {SCRIPT_DIR}

## Key Paths
| What | Path |
|---|---|
| Workflows | {cfg.get('workflow_dir', '')} |
| Reference material | {ref_dir} |
| Generated images | {cfg.get('out_images', '')} |
| Generated videos | {cfg.get('out_videos', '')} |
| Trained LoRAs | {cfg.get('out_loras', '')} |
| Generated 3D | {cfg.get('out_3d', '')} |
| Models folder | {cfg.get('models_dir', '')} |
| LoRAs folder | {cfg.get('loras_dir', '')} |
| Datasets | {cfg.get('datasets_dir', '')} |
| ComfyUI (image) | {cfg.get('comfy_image_dir', '')} |
| ComfyUI (video) | {cfg.get('comfy_video_dir', '')} |
| ComfyUI input | {cfg.get('comfy_image_input', '')} |
| ComfyUI output | {cfg.get('comfy_image_output', '')} |
| Custom nodes | {nodes_dir} |
| LM Studio | {cfg.get('lms_exe', '')} |
| Documents venv | {os.path.join(os.environ.get('USERPROFILE', ''), 'Documents', 'ComfyUI', '.venv', 'Scripts', 'python.exe')} |

## Navigation Shortcuts (use with list_directory)
- `home` → {SCRIPT_DIR}
- `reference` → {ref_dir}
- `workflows` → {cfg.get('workflow_dir', '')}
- `output` → {cfg.get('out_images', '')}
- `videos` → {cfg.get('out_videos', '')}
- `loras` → {cfg.get('loras_dir', '')}
- `models` → {cfg.get('models_dir', '')}
- `datasets` → {cfg.get('datasets_dir', '')}
- `comfyui` → {cfg.get('comfy_image_dir', '')}
- `custom_nodes` → {nodes_dir}

## Installed Custom Nodes ({len(custom_nodes)})
{chr(10).join(f'- {n}' for n in custom_nodes) if custom_nodes else '- None found'}

## Reference Handbooks
- README.md — index of all handbooks
- python.md, javascript.md, bash.md, powershell.md, html_css.md, sql.md, json_yaml.md, regex.md
- image_selection.md — how to browse and pick images
- workflows.md — how to call each workflow
- comfyui_nodes.md — custom node installation
- comfyui_native_nodes.md — built-in ComfyUI nodes
- comfyui_troubleshooting.md — diagnostic rulebook
- self_scan.md — how to look inward for errors
- bug_fixing.md — how to structure a fix
- safetensor_reading.md — reading model files

---
*Your notes below this line are preserved across reboots:*

"""
    # Preserve any existing notes below the divider
    if os.path.exists(index_path):
        try:
            existing = open(index_path, encoding="utf-8").read()
            divider = "*Your notes below this line are preserved across reboots:*"
            if divider in existing:
                notes = existing.split(divider, 1)[1].strip()
                if notes:
                    content += notes + "\n"
        except Exception:
            pass

    try:
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(content)
        ok(f"Index written: {index_path}")
    except Exception as e:
        warn(f"Could not write index: {e}")
def create_output_folders(cfg):
    """Create all output and working folders."""
    header("Creating output folders")
    for key in ("out_images", "out_videos", "out_3d",
                "out_checkpoints", "out_dataset", "out_diffusion", "out_loras"):
        path = cfg.get(key, "")
        if path:
            os.makedirs(path, exist_ok=True)
            ok(f"{path}")
    for key in ("comfy_image_input", "comfy_image_output", "datasets_dir"):
        path = cfg.get(key, "")
        if path:
            os.makedirs(path, exist_ok=True)

    # Models live in Sys 32's comfyui resources — no separate USB Models folder needed

# ── Main ───────────────────────────────────────────────────────────────────────

def _ensure_llm_imported(lms_exe):
    """
    Scan I:/LLM Storage for model folders and import any that
    aren't already in LM Studio's catalog. Runs silently on every boot.
    Publisher/Model folder structure is preserved as the lms key.
    """
    llm_storage = "I:\\LLM Storage"
    if not os.path.exists(llm_storage):
        return

    # Get currently cataloged models from lms ls
    cataloged = set()
    try:
        result = subprocess.run(
            [lms_exe, "ls"],
            capture_output=True, text=True, timeout=15
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split()
            if parts and "/" in parts[0]:
                cataloged.add(parts[0].lower())
    except Exception:
        pass

    # Walk model storage — supports both Publisher/Model and flat Model structures
    def _find_gguf(folder):
        try:
            for fname in os.listdir(folder):
                if fname.lower().endswith(".gguf") and "mmproj" not in fname.lower():
                    return os.path.join(folder, fname)
        except Exception:
            pass
        return None

    def _already_cataloged(name):
        name_lower = name.lower()
        for suffix in ("-gguf", "_gguf"):
            if name_lower.endswith(suffix):
                name_lower = name_lower[:-len(suffix)]
                break
        for c in cataloged:
            c_clean = c.lower()
            for suffix in ("-gguf", "_gguf"):
                if c_clean.endswith(suffix):
                    c_clean = c_clean[:-len(suffix)]
                    break
            if name_lower in c_clean or c_clean in name_lower:
                return True
            # Word overlap — 3+ shared words means same model
            wa = set(name_lower.replace("-", " ").replace("_", " ").split())
            wb = set(c_clean.replace("-", " ").replace("_", " ").split())
            if len(wa & wb) >= 3:
                return True
        return False

    def _import_model(gguf_path, publisher, model_name):
        pr(f"Importing model: {publisher}/{model_name}...")
        try:
            result = subprocess.run(
                [lms_exe, "import", gguf_path, "--yes"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                ok(f"Imported: {publisher}/{model_name}")
            else:
                warn(f"Import failed: {result.stderr.strip()[:100]}")
        except Exception as e:
            warn(f"Import error: {e}")

    try:
        entries = [d for d in os.listdir(llm_storage)
                   if os.path.isdir(os.path.join(llm_storage, d))]
    except Exception:
        return

    for entry in entries:
        entry_path = os.path.join(llm_storage, entry)

        # Check if this is a Publisher folder (contains subfolders with GGUFs)
        # or a flat Model folder (contains GGUFs directly)
        try:
            subentries = [d for d in os.listdir(entry_path)
                          if os.path.isdir(os.path.join(entry_path, d))]
        except Exception:
            subentries = []

        gguf_direct = _find_gguf(entry_path)

        if gguf_direct and not subentries:
            # Flat structure — entry IS the model folder, use "local" as publisher
            model_name = entry
            publisher  = "local"
            if _already_cataloged(model_name):
                continue
            _import_model(gguf_direct, publisher, model_name)

        elif subentries:
            # Publisher/Model structure
            publisher = entry
            for model_name in subentries:
                model_path = os.path.join(entry_path, model_name)
                if _already_cataloged(model_name):
                    continue
                gguf = _find_gguf(model_path)
                if not gguf:
                    continue
                _import_model(gguf, publisher, model_name)


# Pinned llama.cpp build — do NOT track "latest".
# Builds from ~b7400 (Dec 2025) onward ship "router mode", which changed
# launch/template/flash-attn behavior and breaks model loading for our setup
# (server comes up routing cached presets instead of loading our model →
# empty responses). b9500 is the build System 32 was validated against.
# To move forward deliberately, bump these three together after testing.
LLAMA_BUILD = "b9500"
LLAMA_ASSET = f"llama-{LLAMA_BUILD}-bin-win-cuda-12.4-x64.zip"
LLAMA_URL   = (f"https://github.com/ggml-org/llama.cpp/releases/download/"
               f"{LLAMA_BUILD}/{LLAMA_ASSET}")


def _ensure_llama_server(script_dir):
    """
    Ensure the PINNED llama-server build (LLAMA_BUILD) is present in Sys 32's Home.
    A build-marker file records which build is installed; if it's missing or
    doesn't match the pin (e.g. a drifted "latest" got pulled on a fresh drive),
    the pinned build is re-downloaded so the binary can't silently wander.
    """
    dest   = os.path.join(script_dir, "llama-server.exe")
    marker = os.path.join(script_dir, "llama-server.build")

    if os.path.exists(dest):
        cur = ""
        try:
            with open(marker, "r") as _m:
                cur = _m.read().strip()
        except Exception:
            pass
        if cur == LLAMA_BUILD:
            ok(f"llama-server.exe present (pinned {LLAMA_BUILD}).")
            return dest
        warn(f"llama-server.exe build is '{cur or 'unknown'}', want {LLAMA_BUILD} "
             f"— replacing with the pinned build.")

    pr(f"Downloading pinned llama-server {LLAMA_BUILD} from GitHub...")
    import urllib.request
    import zipfile

    try:
        asset_name = LLAMA_ASSET
        asset_url  = LLAMA_URL

        pr(f"Downloading {asset_name}...")
        zip_path = os.path.join(script_dir, "llama_temp.zip")
        _download(asset_url, zip_path)

        pr("Extracting llama.cpp binaries...")
        found = False
        with zipfile.ZipFile(zip_path, "r") as z:
            for name in z.namelist():
                base = name.replace("\\", "/").split("/")[-1]
                if not base:
                    continue
                # Extract llama-server.exe and all supporting DLLs
                if (base.lower() == "llama-server.exe"
                        or base.lower() == "llama-server-impl.dll"
                        or base.lower().endswith(".dll")):
                    data = z.read(name)
                    out_path = os.path.join(script_dir, base)
                    with open(out_path, "wb") as fout:
                        fout.write(data)
                    if base.lower() == "llama-server.exe":
                        found = True

        try:
            os.remove(zip_path)
        except Exception:
            pass

        if found:
            try:
                with open(marker, "w") as _m:
                    _m.write(LLAMA_BUILD)
            except Exception:
                pass
            ok(f"llama-server.exe ready (pinned {LLAMA_BUILD}): {dest}")
            return dest
        else:
            exes = []
            warn(f"llama-server.exe not found in archive. Check release format.")
            return None

    except Exception as e:
        warn(f"Could not download pinned llama-server {LLAMA_BUILD}: {e}")
        warn(f"Download manually: {LLAMA_URL}")
        return None


# The CUDA runtime llama-server's graphics-card backend needs (2026-11-02). llama.cpp ships it
# as a SEPARATE zip next to the build: without these three DLLs ggml-cuda.dll can't load and
# llama-server quietly runs the whole model on the CPU (found: a 27B model reading at 9 tokens/s
# on a 5070 Ti; its log listed only the CPU). Same release, same CUDA 12.4 as LLAMA_ASSET.
CUDART_ASSET = "cudart-llama-bin-win-cuda-12.4-x64.zip"
CUDART_URL   = (f"https://github.com/ggml-org/llama.cpp/releases/download/"
                f"{LLAMA_BUILD}/{CUDART_ASSET}")
CUDART_DLLS  = ("cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll")


def _ensure_cuda_runtime(script_dir):
    """Put the CUDA runtime DLLs next to llama-server.exe when this computer has an NVIDIA
    card. Checked every start (cheap); downloaded once (~390 MB zip, only the 3 DLLs are
    kept). Never on a computer without an NVIDIA card — the drive fetches them the first
    time it's plugged into one."""
    import shutil
    missing = [d for d in CUDART_DLLS if not os.path.exists(os.path.join(script_dir, d))]
    if not missing:
        ok("Graphics-card support for the model (CUDA runtime) present.")
        return True
    if not os.path.exists(os.path.join(script_dir, "ggml-cuda.dll")):
        return False                      # not a CUDA build of llama-server — nothing to add
    try:
        cuda, _cap, name = _nvidia_info()
    except Exception:
        cuda, name = None, ""
    if not name:
        pr("No NVIDIA graphics card found — the model runs on the CPU on this computer.")
        return False
    if cuda and tuple(cuda) < (12, 4):
        warn(f"{name}: the NVIDIA driver is too old for llama-server's CUDA 12.4 — update the "
             f"driver (nvidia.com) or the model runs on the CPU.")
        return False
    if AIRPLANE:
        warn("The model's graphics-card support isn't installed, and airplane mode is on — "
             "the model runs on the CPU until it's off.")
        return False
    pr(f"Downloading graphics-card support for the model (CUDA runtime, one-time, ~390 MB) for {name}...")
    zip_path = os.path.join(script_dir, "cudart_temp.zip")
    try:
        _download(CUDART_URL, zip_path)
        import zipfile
        got = []
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                base = n.replace("\\", "/").split("/")[-1]
                if base in CUDART_DLLS:
                    part = os.path.join(script_dir, base + ".part")
                    with z.open(n) as src, open(part, "wb") as out:
                        shutil.copyfileobj(src, out)
                    os.replace(part, os.path.join(script_dir, base))
                    got.append(base)
        still = [d for d in CUDART_DLLS if not os.path.exists(os.path.join(script_dir, d))]
        if still:
            warn(f"The CUDA runtime download was missing {', '.join(still)} — the model may run on the CPU.")
            return False
        ok(f"Graphics-card support for the model installed ({', '.join(got)}).")
        return True
    except Exception as e:
        warn(f"Couldn't download the model's graphics-card support: {e}")
        warn(f"Download manually: {CUDART_URL} — put its 3 .dll files next to llama-server.exe.")
        return False
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass


# Vulkan add-on for llama-server (2026-11-02): a second graphics card that ISN'T NVIDIA (e.g. an
# AMD RX 9060 XT next to a 5070 Ti, or an AMD-only computer) is reached through llama.cpp's
# Vulkan backend. It's one file, ggml-vulkan.dll, from the SAME b9500 release (its core DLLs are
# byte-identical to the CUDA build's). System 32 then names the cards it uses (s32_hardware).
VULKAN_ASSET = f"llama-{LLAMA_BUILD}-bin-win-vulkan-x64.zip"
VULKAN_URL   = (f"https://github.com/ggml-org/llama.cpp/releases/download/"
                f"{LLAMA_BUILD}/{VULKAN_ASSET}")
_BUILTIN_GPU = ("intel", "microsoft", "(tm) graphics", "basic display", "parsec", "virtual",
                "remote")


def _other_gpus():
    """Graphics cards Windows lists that aren't NVIDIA and aren't built-in graphics."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }"],
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return []
    out = []
    for name in (r.stdout or "").splitlines():
        n = name.strip()
        low = n.lower()
        if not n or "nvidia" in low or any(b in low for b in _BUILTIN_GPU) or low == "amd radeon graphics":
            continue
        out.append(n)
    return out


def _ensure_vulkan_backend(script_dir):
    """ggml-vulkan.dll next to llama-server.exe when this computer has a non-NVIDIA card.
    Kept once it's there (the drive may meet such a computer again)."""
    dest = os.path.join(script_dir, "ggml-vulkan.dll")
    if os.path.exists(dest):
        ok("Vulkan graphics support for the model present.")
        return True
    if not os.path.exists(os.path.join(script_dir, "llama-server.exe")):
        return False
    others = _other_gpus()
    if not others:
        return False                      # NVIDIA only (or built-in graphics) — nothing to add
    if AIRPLANE:
        warn(f"{others[0]} can't be used for the model yet — airplane mode is on.")
        return False
    pr(f"Downloading Vulkan graphics support for the model (one-time, ~34 MB) for {others[0]}...")
    zip_path = os.path.join(script_dir, "vulkan_temp.zip")
    try:
        import zipfile, shutil
        _download(VULKAN_URL, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            src = [n for n in z.namelist() if n.replace("\\", "/").split("/")[-1] == "ggml-vulkan.dll"]
            if not src:
                warn("The Vulkan download didn't contain ggml-vulkan.dll.")
                return False
            with z.open(src[0]) as s, open(dest + ".part", "wb") as o:
                shutil.copyfileobj(s, o)
            os.replace(dest + ".part", dest)
        ok(f"Vulkan graphics support installed — {others[0]} can now run the model "
           f"(choose GRAPHICS CARDS in the model settings).")
        return True
    except Exception as e:
        warn(f"Couldn't download Vulkan graphics support: {e}")
        return False
    finally:
        try:
            os.remove(zip_path)
        except OSError:
            pass


def _ssl_context():
    """HTTPS trust for downloads. The bundled/relocated Python's compiled-in default
    cert path doesn't resolve on a fresh host, so urlopen falls back to an EMPTY trust
    store and every download dies with CERTIFICATE_VERIFY_FAILED (this is what broke
    the virgin-drive git/Python auto-install). Point it at the certifi CA bundle that
    ships ON THE STICK — host-independent, works on any machine. Fall back to the
    Windows cert store only if certifi isn't importable."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
        try:
            ctx.load_default_certs()
        except Exception:
            pass
        return ctx


def _download(url, dest, timeout=300):
    """Download url → dest using the stick's own CA trust (see _ssl_context). Replaces
    urllib.urlretrieve, which uses the broken default SSL context on a fresh host."""
    import urllib.request, shutil
    if AIRPLANE:
        raise RuntimeError("airplane mode is on — nothing is downloaded")
    req = urllib.request.Request(url, headers={"User-Agent": "System32-bootstrap"})
    with urllib.request.urlopen(req, context=_ssl_context(), timeout=timeout) as r, \
         open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


# Pinned installer fallbacks for a virgin machine that has NEITHER tool. Used only as
# a LAST resort (always probe first); pinned to known-good versions to match the
# b9500 / 3.12 pin-everything doctrine — never "latest", which can drift or break the
# chain on a machine we can't babysit.
GIT_INSTALLER_URL   = "https://github.com/git-for-windows/git/releases/download/v2.54.0.windows.1/Git-2.54.0-64-bit.exe"
PY312_INSTALLER_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"


def _ensure_git():
    """Return a usable git ('git' if on PATH, else a full path), auto-installing Git
    for Windows SILENTLY if the host has none. Probe first; download only as a last
    resort. After a silent install Windows does NOT refresh PATH for already-running
    processes, so we prepend git's dir to THIS process's PATH — children spawned later
    (the runtime node installer) then inherit it without a reboot."""
    def _use(p):
        d = os.path.dirname(p)
        if d and d.lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return p

    try:
        if subprocess.run(["git", "--version"], capture_output=True,
                          text=True).returncode == 0:
            return "git"
    except Exception:
        pass

    known = [
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
        os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Git", "cmd", "git.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
    ]
    for p in known:
        if os.path.isfile(p):
            return _use(p)

    pr("git not found — downloading Git for Windows (one-time, ~65MB)...")
    try:
        import urllib.request
        inst = os.path.join(os.environ.get("TEMP", SCRIPT_DIR), "git_installer.exe")
        _download(GIT_INSTALLER_URL, inst)
        pr("Installing git silently...")
        # Inno Setup silent flags; /o:PathOption=Cmd puts git on PATH for future shells.
        subprocess.run([inst, "/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES",
                        "/NOCANCEL", "/SP-", "/o:PathOption=Cmd"], timeout=600)
        try:
            os.remove(inst)
        except Exception:
            pass
    except Exception as e:
        warn(f"git auto-install failed: {str(e)[:120]}")
        warn("Install git manually from https://git-scm.com/download/win")
        return None

    for p in known:
        if os.path.isfile(p):
            ok(f"git installed: {p}")
            return _use(p)
    warn("git installed but not located at the expected path — a reboot may be needed.")
    return None


def _ensure_build_python():
    """Return a working Python 3.12 to build the ComfyUI venv, auto-installing the
    official 3.12 SILENTLY if the host has none. Probe via _find_build_python first;
    download only as a last resort. After install, prepend its dir to THIS process's
    PATH so the immediate venv build can use it without a reboot."""
    py = _find_build_python()
    if py:
        return py

    pr("No Python 3.12 found — downloading the official 3.12 installer (one-time, ~25MB)...")
    try:
        import urllib.request
        inst = os.path.join(os.environ.get("TEMP", SCRIPT_DIR), "python312_installer.exe")
        _download(PY312_INSTALLER_URL, inst)
        pr("Installing Python 3.12 silently (PrependPath + pip)...")
        subprocess.run([inst, "/quiet", "InstallAllUsers=0",
                        "PrependPath=1", "Include_pip=1", "Include_test=0"], timeout=900)
        try:
            os.remove(inst)
        except Exception:
            pass
    except Exception as e:
        warn(f"Python 3.12 auto-install failed: {str(e)[:120]}")
        warn("Install Python 3.12 from python.org (tick 'Add python.exe to PATH').")
        return None

    cand = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "Programs", "Python", "Python312", "python.exe")
    if os.path.isfile(cand):
        os.environ["PATH"] = os.path.dirname(cand) + os.pathsep + os.environ.get("PATH", "")
    py = _find_build_python()
    if py:
        ok(f"Python 3.12 installed: {py}")
    else:
        warn("Python 3.12 installed but not yet visible — reboot and re-run if the venv build is skipped.")
    return py


def _find_build_python():
    """Return a working Python **3.12** interpreter for building the ComfyUI venv,
    or None. Hard-pinned to 3.12: that's the version torch+CUDA wheels currently
    support, so a 3.13/3.14 would build a venv that then dies at the torch install.
    The embeddable portable Python can't be used (no venv module), so we look
    elsewhere — the Windows 'py' launcher, PATH, and known python.org / ComfyUI / uv
    locations — verifying each really IS 3.12 and actually runs before trusting it."""
    def _is_py312(exe):
        if not _python_works(exe):
            return False
        try:
            # Must be 3.12 AND have a usable `venv` module. The bundled embeddable
            # Python reports 3.12 and runs, but ships WITHOUT venv/ensurepip — so it
            # passes a naive version check, then fails `python -m venv` (this is what
            # made the virgin-drive venv build fail). Importing venv here rejects it.
            r = subprocess.run(
                [exe, "-c", "import sys, venv; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=20)
            return r.returncode == 0 and r.stdout.strip() == "3.12"
        except Exception:
            return False

    candidates = []
    # Ask the Windows 'py' launcher directly for 3.12, then PATH 'python' — both
    # return a full path with no guessing. (PATH python is only kept if it's 3.12.)
    for probe in (["py", "-3.12", "-c", "import sys;print(sys.executable)"],
                  ["python", "-c", "import sys;print(sys.executable)"]):
        try:
            r = subprocess.run(probe, capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip():
                candidates.append(r.stdout.strip())
        except Exception:
            pass

    user     = os.environ.get("USERPROFILE", "")
    localapp = os.environ.get("LOCALAPPDATA", "")
    appdata  = os.environ.get("APPDATA", "")
    candidates += [
        os.path.join(localapp, "Programs", "Python", "Python312", "python.exe"),
        r"C:\Python312\python.exe",
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                     "Python312", "python.exe"),
        # ComfyUI Desktop venvs (version-checked below — only used if they're 3.12).
        os.path.join(user, "Documents", "ComfyUI", ".venv", "Scripts", "python.exe"),
        os.path.join(localapp, "Programs", "ComfyUI", ".venv", "Scripts", "python.exe"),
    ]
    # uv-managed Pythons (scan newest first).
    uv_base = os.path.join(appdata, "uv", "python")
    if os.path.isdir(uv_base):
        for entry in sorted(os.listdir(uv_base), reverse=True):
            candidates.append(os.path.join(uv_base, entry, "python.exe"))

    seen = set()
    for exe in candidates:
        if exe and exe not in seen:
            seen.add(exe)
            if _is_py312(exe):
                return exe
    return None


# PyTorch for the ComfyUI venv (2026-10-31): a matched set, and the CUDA build that fits THIS
# machine's NVIDIA driver and card. PyTorch 2.11's builds: cu130 + cu128 = Turing (RTX 20)
# through Blackwell (RTX 50); cu126 = Maxwell/Pascal/Volta (GTX 900/10, Titan V) through
# Hopper, no Blackwell. A build needs a driver that supports its CUDA version.
_TORCH_SET = ["torch==2.11.0", "torchvision==0.26.0", "torchaudio==2.11.0"]


def _nvidia_info():
    """→ (driver's CUDA version as (major, minor) or None, compute capability float or None,
    GPU name). Read with nvidia-smi; (None, None, "") when there's no NVIDIA card/driver."""
    import re
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    cands = ["nvidia-smi",
             os.path.join(sysroot, "System32", "nvidia-smi.exe"),
             os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                          "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe")]
    # Newer drivers can leave it only in the driver's own store folder (2026-10-31: a laptop
    # on driver 610.62 came out as "no NVIDIA driver found").
    cands += sorted(glob.glob(os.path.join(sysroot, "System32", "DriverStore", "FileRepository",
                                           "nv*", "nvidia-smi.exe")), reverse=True)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    for exe in cands:
        try:
            r = subprocess.run([exe], capture_output=True, text=True, timeout=20, creationflags=flags)
        except Exception:
            continue
        if r.returncode != 0:
            continue
        # 'CUDA Version: 13.0' — driver 610 prints 'CUDA UMD Version: 13.3' instead (2026-10-31,
        # laptop: the old pattern missed it → "no NVIDIA driver found"). Failing both, the
        # driver version itself ('Driver Version' / 'KMD Version') says what CUDA it supports.
        m = re.search(r"CUDA[A-Za-z ]*Version:\s*(\d+)\.(\d+)", r.stdout or "")
        cuda = (int(m.group(1)), int(m.group(2))) if m else None
        if cuda is None:
            dm = re.search(r"(?:Driver|KMD) Version:\s*(\d+)\.(\d+)", r.stdout or "")
            if dm:
                cuda = _cuda_for_driver(int(dm.group(1)) + int(dm.group(2)) / 100.0)
        cap, name = None, ""
        try:
            q = subprocess.run([exe, "--query-gpu=compute_cap,name", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=20, creationflags=flags)
            first = (q.stdout or "").strip().splitlines()[0] if q.returncode == 0 and q.stdout.strip() else ""
            if first:
                c, _, name = first.partition(",")
                cap, name = float(c.strip()), name.strip()
        except Exception:
            pass
        return cuda, cap, name
    return _nvidia_from_windows()


def _cuda_for_driver(drv):
    """NVIDIA driver version (e.g. 610.62) → the newest CUDA it supports."""
    return ((13, 0) if drv >= 580 else (12, 8) if drv >= 570 else (12, 6) if drv >= 560
            else (12, 4) if drv >= 550 else (12, 0))


def _nvidia_from_windows():
    """No nvidia-smi: ask Windows which display adapters it has. An NVIDIA one's driver
    version '32.0.16.1062' = 610.62 → the CUDA it supports; the card's age from its name."""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name + '|' + $_.DriverVersion }"],
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return None, None, ""
    import re
    for line in (r.stdout or "").splitlines():
        name, _, ver = line.strip().partition("|")
        if "nvidia" not in name.lower():
            continue
        digits = ver.replace(".", "")
        try:
            drv = int(digits[-5:]) / 100.0                    # 32.0.16.1062 → 610.62
        except ValueError:
            continue
        cuda = _cuda_for_driver(drv)
        low = name.lower()
        cap = (7.5 if re.search(r"gtx\s*16", low) else
               6.0 if re.search(r"\bgtx\b|quadro [kmp]\d|titan x|titan v|tesla [kmpv]", low) else None)
        return cuda, cap, name.strip()
    return None, None, ""


def _pick_torch_build():
    """→ (index tag like 'cu130', one line saying why)."""
    cuda, cap, name = _nvidia_info()
    card = name or "NVIDIA card"
    if cuda is None:
        return "cu128", "no NVIDIA driver found — the standard build (runs on the CPU until one is installed)"
    if cap is not None and cap < 7.5:
        return "cu126", f"{card} (compute {cap}) is older than the RTX 20 series — CUDA 12.6 is the build that still supports it"
    if cuda >= (13, 0):
        return "cu130", f"{card}, driver supports CUDA {cuda[0]}.{cuda[1]} — the fastest build"
    if cuda >= (12, 8):
        return "cu128", (f"{card}, driver supports CUDA {cuda[0]}.{cuda[1]} — updating the NVIDIA driver "
                         "(580 or newer) would allow the faster CUDA 13 build")
    if cap is not None and cap >= 10:
        return "cu128", (f"{card} needs a newer NVIDIA driver (this one supports only CUDA {cuda[0]}.{cuda[1]}) "
                         "— update it, or image generation won't run")
    return "cu126", (f"{card}, driver supports only CUDA {cuda[0]}.{cuda[1]} — updating the NVIDIA driver "
                     "would allow a faster build")


def _py312_with_venv(exe):
    if not _python_works(exe):
        return False
    try:
        r = subprocess.run([exe, "-c", "import sys, venv; print('%d.%d' % sys.version_info[:2])"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and r.stdout.strip() == "3.12"
    except Exception:
        return False


def _repair_venv(venv_dir):
    """Re-point a broken venv at a working Python 3.12 (`python -m venv --upgrade`): rewrites
    its launcher + pyvenv.cfg and KEEPS every installed package. True if it works afterwards
    (its Python runs AND imports torch) — otherwise the caller rebuilds it."""
    if not os.path.isdir(os.path.join(venv_dir, "Lib", "site-packages", "torch")):
        return False                                   # nothing worth keeping
    base = ""
    try:
        with open(os.path.join(venv_dir, "pyvenv.cfg"), encoding="utf-8") as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip().lower() == "home":
                    base = os.path.join(v.strip(), "python.exe")
    except Exception:
        pass
    if not (base and _py312_with_venv(base)):
        base = _ensure_build_python()
    if not base:
        return False
    pr(f"Repairing the ComfyUI venv with {base} (keeps its packages)...")
    try:
        subprocess.run([base, "-m", "venv", "--upgrade", venv_dir], capture_output=True, timeout=300)
    except Exception as e:
        warn(f"venv repair failed: {e}")
        return False
    vpy = os.path.join(venv_dir, "Scripts", "python.exe")
    try:
        r = subprocess.run([vpy, "-c", "import torch"], capture_output=True, text=True, timeout=180)
        if r.returncode == 0:
            ok("USB ComfyUI venv repaired — packages kept, nothing re-downloaded.")
            return True
        warn("Repaired venv can't load PyTorch: " + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:160])
    except Exception as e:
        warn(f"Repaired venv didn't start: {e}")
    return False


# ── Self-install: the pieces that aren't in the repo (2026-11-01) ───────────────
# The repo holds only System 32's own code. Everything else is fetched from its official
# source on the first start (never in airplane mode): ComfyUI (pinned, like llama-server
# and SearXNG), the Kokoro voice files, and — when nobody has picked one — the language
# model is found in "LLM Storage".
COMFY_TAG = "v0.24.0"                 # PINNED — the ComfyUI version System 32 is tested with
COMFY_URL = f"https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/{COMFY_TAG}.zip"
KOKORO_FILES = {
    "kokoro-v1.0.onnx": ["https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.onnx",
                         "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx"],
    "voices-v1.0.bin":  ["https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin",
                         "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin"],
}
# Piper: the second voice engine — only the PROGRAM is installed; voices are the user's to add
# (each Piper voice has its own license). rhasspy/piper's last Windows release (MIT).
PIPER_URL = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip"
# The 3D viewer for the chat page (static\\): Google's model-viewer + three.js's Draco and
# Basis decoders — Apache-2.0 / MIT, not System 32's own, so they're downloaded (npm) at
# setup instead of shipping in the repo. Pinned versions, every file checked by SHA-256.
STATIC_PKGS = {
    "https://registry.npmjs.org/@google/model-viewer/-/model-viewer-3.5.0.tgz": {
        "package/dist/model-viewer.min.js": ("model-viewer.min.js", "8923739c8c1b4a02dd9c8cf66da5c2a448235cb5e49e439dd7bbba944ba4fbe1"),
        "package/LICENSE": ("LICENSE-model-viewer.txt", "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"),
    },
    "https://registry.npmjs.org/three/-/three-0.163.0.tgz": {
        "package/examples/jsm/libs/draco/gltf/draco_decoder.js": ("draco/draco_decoder.js", "8625489da79a805f4f2a7d511c3e52d8b4085608a9d2a4d5f4f9de5db0aea04f"),
        "package/examples/jsm/libs/draco/gltf/draco_decoder.wasm": ("draco/draco_decoder.wasm", "a680d927bed9cb864ddbd63521868891af2bfbe755092761b4837487618df8ac"),
        "package/examples/jsm/libs/draco/gltf/draco_wasm_wrapper.js": ("draco/draco_wasm_wrapper.js", "8bb2952d2ba7d67e1414f8df819410cb0434a666be53f671fff75f68843d76f6"),
        "package/examples/jsm/libs/basis/basis_transcoder.js": ("basis/basis_transcoder.js", "48a0ef319a28bf0224ee88ded34f74eaf97c175bba9eb18b47fb9720510ad6c4"),
        "package/examples/jsm/libs/basis/basis_transcoder.wasm": ("basis/basis_transcoder.wasm", "79ae97d781e10a566659c689b7bb1de91726453f55f9f5e3bcc07a4e3904070f"),
        "package/LICENSE": ("LICENSE-three.txt", "4c40a1ef62450b857c3b2aaf294936304cd552d965fbcd9d32d4c5bcf4ba4454"),
    },
}
_LOTTIE_STUB = ("// Lottie (animated textures) is not used by System 32 — this stub keeps the 3D viewer "
                "from fetching it online.\nexport class LottieLoader {}\n")
_RESOURCE_DIRS = ("models/checkpoints", "models/loras", "models/vae", "models/upscale_models",
                  "models/diffusion_models", "models/text_encoders", "custom_nodes", "input")


def _ensure_comfyui(script_dir):
    """Download ComfyUI (pinned COMFY_TAG) into <home>\\ComfyUI when it isn't there, and make
    the drive's 'Sys 32's comfyui resources' folders. Existing installs are never touched."""
    import zipfile
    import shutil
    comfy = os.path.join(script_dir, "ComfyUI")
    try:
        os.makedirs(os.path.join(script_dir, "JsonFiles"), exist_ok=True)   # where workflows go
    except Exception:
        pass
    if not os.path.exists(os.path.join(_find_usb_comfyui_resources() or "", "models")):
        res = os.path.join(os.path.dirname(script_dir), "Sys 32's comfyui resources")
        try:
            for d in _RESOURCE_DIRS:
                os.makedirs(os.path.join(res, *d.split("/")), exist_ok=True)
            ok(f"Made the ComfyUI resources folders: {res}")
        except Exception as e:
            warn(f"Couldn't make {res}: {e}")
    if os.path.exists(os.path.join(comfy, "main.py")):
        return True
    if AIRPLANE:
        warn("ComfyUI isn't installed, and airplane mode is on — image generation stays off.")
        return False
    header("Installing ComfyUI")
    pr(f"Downloading ComfyUI {COMFY_TAG} (one-time)...")
    zip_path = os.path.join(script_dir, "comfyui_temp.zip")
    tmp = os.path.join(script_dir, "comfyui_temp")
    try:
        _download(COMFY_URL, zip_path, timeout=900)
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        inner = next((os.path.join(tmp, d) for d in os.listdir(tmp)
                      if os.path.isfile(os.path.join(tmp, d, "main.py"))), None)
        if not inner:
            raise RuntimeError("the download didn't contain ComfyUI's main.py")
        os.makedirs(comfy, exist_ok=True)
        for n in os.listdir(inner):
            dst = os.path.join(comfy, n)
            if not os.path.exists(dst):                  # never overwrite (e.g. a kept .venv)
                shutil.move(os.path.join(inner, n), dst)
        ok(f"ComfyUI {COMFY_TAG} installed: {comfy}")
        return True
    except Exception as e:
        warn(f"Couldn't install ComfyUI: {e} — image generation stays off until it's there.")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(zip_path)
        except OSError:
            pass


def _ensure_voice_packages(script_dir):
    """kokoro-onnx + sounddevice into System 32's own Python, here in setup. voice.py's own
    first-use install ran inside the running System 32 and the import still failed right
    after (2026-11-01 repo test: "Failed to install kokoro-onnx: No module named
    'kokoro_onnx'"), so voice stayed off on a fresh drive."""
    py = os.path.join(script_dir, "python", "python.exe")
    if not os.path.exists(py) or AIRPLANE:
        return
    # faster-whisper = speech INPUT (s32_stt). Installed here too (2026-11-08): installing it
    # from inside the running System 32 never became importable on a fresh drive.
    need = [pkg for pkg, mod in (("kokoro-onnx", "kokoro_onnx"), ("sounddevice", "sounddevice"),
                                 ("faster-whisper", "faster_whisper"))
            if subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode != 0]
    for pkg in need:
        pr(f"Installing {pkg}{' (speech input, one-time, ~150 MB)' if pkg == 'faster-whisper' else ''}...")
        r = subprocess.run([py, "-m", "pip", "install", pkg, "--quiet"],
                           capture_output=True, text=True, timeout=1800)
        if r.returncode == 0:
            ok(f"{pkg} installed")
        else:
            what = "speech input (the mic) may not work" if pkg == "faster-whisper" else "voice may stay off"
            warn(f"{pkg} install failed — {what}: "
                 + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:160])


def _ensure_piper(script_dir):
    """Install the Piper program into <home>\\piper (no voices) and make piper\\voices for the
    user's own .onnx + .onnx.json voice files. An existing Piper is never touched."""
    import zipfile
    import shutil
    pdir = os.path.join(script_dir, "piper")
    vdir = os.path.join(pdir, "voices")
    if os.path.exists(os.path.join(pdir, "piper.exe")):
        os.makedirs(vdir, exist_ok=True)
        return True
    if AIRPLANE:
        return False
    pr("Downloading Piper (optional second voice engine — voices not included)...")
    zip_path = os.path.join(script_dir, "piper_temp.zip")
    tmp = os.path.join(script_dir, "piper_temp")
    try:
        _download(PIPER_URL, zip_path, timeout=600)
        shutil.rmtree(tmp, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        inner = os.path.join(tmp, "piper")
        if not os.path.isfile(os.path.join(inner, "piper.exe")):
            raise RuntimeError("the download didn't contain piper.exe")
        os.makedirs(pdir, exist_ok=True)
        for n in os.listdir(inner):
            dst = os.path.join(pdir, n)
            if not os.path.exists(dst):
                shutil.move(os.path.join(inner, n), dst)
        os.makedirs(vdir, exist_ok=True)
        ok(f"Piper installed. To use Piper voices, drop each voice's .onnx + .onnx.json into: {vdir}")
        return True
    except Exception as e:
        warn(f"Couldn't install Piper: {str(e)[:120]} — the Kokoro voices still work.")
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            os.remove(zip_path)
        except OSError:
            pass


def _ensure_static(script_dir):
    """The chat page's 3D viewer files (static\\). Downloaded once, every file checked against
    its pinned SHA-256 before it's used; an existing static folder is never touched."""
    import tarfile
    import hashlib
    sdir = os.path.join(script_dir, "static")
    if os.path.isfile(os.path.join(sdir, "model-viewer.min.js")):
        return True
    if AIRPLANE:
        warn("The 3D viewer isn't installed, and airplane mode is on — 3D previews stay off.")
        return False
    pr("Downloading the 3D viewer for the chat page (model-viewer + three.js decoders)...")
    tmp = os.path.join(script_dir, "static_temp.tgz")
    got = {}
    try:
        for url, files in STATIC_PKGS.items():
            _download(url, tmp, timeout=600)
            with tarfile.open(tmp, "r:gz") as t:
                for member, (dest, sha) in files.items():
                    f = t.extractfile(member)
                    if f is None:
                        raise RuntimeError(f"{member} is missing from {url.rsplit('/', 1)[1]}")
                    data = f.read()
                    if hashlib.sha256(data).hexdigest() != sha:
                        raise RuntimeError(f"{dest} didn't match its checksum — not used")
                    got[dest] = data
        for dest, data in got.items():
            out = os.path.join(sdir, *dest.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
        with open(os.path.join(sdir, "none.js"), "w", encoding="utf-8") as f:
            f.write(_LOTTIE_STUB)
        ok(f"3D viewer installed: {sdir}")
        return True
    except Exception as e:
        warn(f"Couldn't install the 3D viewer: {str(e)[:140]} — 3D previews stay off.")
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def _ensure_voice_files(script_dir):
    """Download Kokoro's voice model files into <home> when they're missing."""
    missing = [n for n in KOKORO_FILES if not os.path.exists(os.path.join(script_dir, n))]
    if not missing:
        _ensure_voice_packages(script_dir)
        _ensure_piper(script_dir)
        return True
    if AIRPLANE:
        warn("Voice files aren't installed, and airplane mode is on — voice stays off.")
        return False
    header("Installing voice")
    for name in missing:
        dest = os.path.join(script_dir, name)
        part = dest + ".part"
        for url in KOKORO_FILES[name]:
            pr(f"Downloading {name}...")
            try:
                _download(url, part, timeout=900)
                os.replace(part, dest)
                ok(f"{name} installed")
                break
            except Exception as e:
                warn(f"{name}: {str(e)[:120]}")
                try:
                    os.remove(part)
                except OSError:
                    pass
        else:
            warn(f"Couldn't download {name} — voice stays off until it's there.")
            _ensure_piper(script_dir)
            return False
    _ensure_voice_packages(script_dir)
    _ensure_piper(script_dir)
    return True


def _llm_storage_dirs():
    """'LLM Storage' folders: next to Sys 32's Home first (wherever the stick's folder is),
    then the top of every drive."""
    out = [os.path.join(os.path.dirname(SCRIPT_DIR), "LLM Storage")]
    out += [f"{d}:\\LLM Storage" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ"]
    seen, res = set(), []
    for p in out:
        k = os.path.normcase(os.path.abspath(p))
        if k not in seen and os.path.isdir(p):
            seen.add(k)
            res.append(p)
    return res


_LLM_README = """Put your language models here.

Give each model its own folder, for example:

    LLM Storage\\<publisher>\\<model name>\\<model>.gguf

System 32 runs GGUF models with llama.cpp. For vision (looking at pictures and your
screen), put the model's matching mmproj-....gguf file in the same folder.

Then start System 32 again - it finds the model by itself. With several models it
starts with the newest; switch any time with the model switcher (click the SYSTEM 32
logo).
"""


def _ensure_llm_storage():
    """No model anywhere: make 'LLM Storage' next to Sys 32's Home (with a note inside
    saying what goes there) and say so — instead of only "no model selected"."""
    base = os.path.join(os.path.dirname(SCRIPT_DIR), "LLM Storage")
    try:
        os.makedirs(base, exist_ok=True)
        note = os.path.join(base, "Put your models here.txt")
        if not os.path.exists(note):
            with open(note, "w", encoding="utf-8") as f:
                f.write(_LLM_README)
    except Exception as e:
        warn(f"Couldn't make {base}: {e}")
        return
    warn(f"No language model yet — put a .gguf model in its own folder inside: {base}")
    warn("Then start System 32 again; it picks the model up by itself.")


def _find_llm_model():
    """The newest .gguf language model in any LLM Storage folder (not an mmproj vision
    projector). → (path, how many models there are in all) or (None, 0)."""
    found = []
    for base in _llm_storage_dirs():
        for root, _dirs, files in os.walk(base):
            for f in files:
                lf = f.lower()
                if lf.endswith(".gguf") and "mmproj" not in lf:
                    p = os.path.join(root, f)
                    try:
                        found.append((os.path.getmtime(p), p))
                    except OSError:
                        pass
    if not found:
        return None, 0
    found.sort(reverse=True)
    return found[0][1], len(found)


def _ensure_usb_comfy_venv(script_dir):
    """
    If ComfyUI is installed on the USB drive, ensure it has a working venv.
    Creates and installs requirements if the venv doesn't exist.
    Uses the best available full Python — not the embeddable portable Python
    which lacks the venv module.
    """
    usb_comfy = os.path.join(script_dir, "ComfyUI")
    if not os.path.exists(os.path.join(usb_comfy, "main.py")):
        return  # No USB ComfyUI installed

    venv_python = os.path.join(usb_comfy, ".venv", "Scripts", "python.exe")
    if os.path.exists(venv_python):
        if _python_works(venv_python):
            ok("USB ComfyUI venv ready.")
            return
        # The launcher file exists but its base interpreter is gone (the venv was
        # built against a uv/Documents Python on a now-wiped or different C:), or the
        # launcher itself is damaged (0 bytes — 'Access is denied'). Try a REPAIR first:
        # it keeps the multi-GB packages (2026-10-31: a laptop's launcher went 0 bytes,
        # and every computer switch re-downloaded ~3 GB of PyTorch).
        if _repair_venv(os.path.join(usb_comfy, ".venv")):
            return
        # A dead venv silently breaks ComfyUI — image gen hangs forever with 0
        # checkpoints — so tear it down and rebuild against a live Python below.
        warn("USB ComfyUI venv is broken (its base Python is missing) — rebuilding.")
        import shutil
        shutil.rmtree(os.path.join(usb_comfy, ".venv"), ignore_errors=True)

    # Find a Python 3.12 to build the venv with — auto-installs the official 3.12 if
    # the host has none (see _ensure_build_python / _find_build_python; hard-pinned to
    # 3.12 because torch+CUDA wheels don't exist for 3.13/3.14, so a newer Python would
    # build a venv that dies at the torch install).
    full_python = _ensure_build_python()
    if not full_python:
        warn("USB ComfyUI venv: no Python 3.12 found to build it. Image generation stays off.")
        warn("Fix: install Python 3.12 from python.org (tick 'Add python.exe to PATH'), then reboot.")
        return
    ok(f"Building ComfyUI venv with Python 3.12: {full_python}")

    pr("Setting up USB ComfyUI venv (first run — this may take a few minutes)...")
    try:
        # Create venv using full Python
        subprocess.run(
            [full_python, "-m", "venv", os.path.join(usb_comfy, ".venv")],
            check=True, capture_output=True
        )
        ok("USB ComfyUI venv created.")

        # Stage pip's downloads/unpacks on the STICK, not the host's C: drive. pip
        # unpacks multi-GB torch into %TEMP% (which lives on C:) before moving it into
        # the venv; on a fresh Windows with a small or nearly-full C: that fills the
        # disk and pip WEDGES — which looks exactly like a timeout (it stalls on writes
        # that never complete, then the subprocess timeout fires early). The stick has
        # the room (e.g. 157GB vs 900MB free on C:), so point pip's TEMP + cache at it.
        # Now the host's C: size stops mattering — we carry our own scratch space.
        pip_tmp   = os.path.join(usb_comfy, ".venv", "_pip_tmp")
        pip_cache = os.path.join(usb_comfy, ".venv", "_pip_cache")
        os.makedirs(pip_tmp, exist_ok=True)
        os.makedirs(pip_cache, exist_ok=True)
        build_env = dict(os.environ)
        build_env["TMP"] = pip_tmp
        build_env["TEMP"] = pip_tmp
        build_env["PIP_CACHE_DIR"] = pip_cache

        # Install torch with CUDA support — the build that fits this driver and card
        # (_pick_torch_build). Was always cu128: right for RTX 20-50, but it left out
        # ComfyUI's faster CUDA 13 path on new drivers and can't run on GTX 10-series.
        tag, why = _pick_torch_build()
        pr(f"Installing PyTorch {_TORCH_SET[0].split('==')[1]} ({tag}): {why}...")
        tried = []
        # One fallback: cu130 → cu128 (same cards). cu126 has none — it's chosen for cards/drivers
        # the others can't run on.
        for t in [tag] + (["cu128"] if tag == "cu130" else []):
            tried.append(t)
            r = subprocess.run(
                [venv_python, "-m", "pip", "install", *_TORCH_SET,
                 "--index-url", f"https://download.pytorch.org/whl/{t}", "--quiet"],
                capture_output=True, text=True, timeout=1800, env=build_env
            )
            if r.returncode == 0:
                ok(f"PyTorch installed ({t}).")
                break
            warn(f"PyTorch {t} install failed: " + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:200])
        else:
            warn("PyTorch couldn't be installed (" + ", ".join(tried) + ") — image generation won't run until it is.")

        # Install ComfyUI requirements
        req_file = os.path.join(usb_comfy, "requirements.txt")
        if os.path.exists(req_file):
            pr("Installing ComfyUI requirements...")
            subprocess.run(
                [venv_python, "-m", "pip", "install", "-r", req_file, "--quiet"],
                capture_output=True, timeout=1200, env=build_env
            )

        # Common custom-node dependencies that AREN'T in ComfyUI's base requirements
        # but that bundled nodes reliably need — otherwise they're only noticed at
        # runtime by the missing-node scanner, triggering an install→restart→rescan
        # loop on every fresh boot. Pre-installing them here keeps a fresh machine
        # quiet. Extend this list as recurring offenders surface.
        #   onnx — needed by the Kokoro-Onnx node; a day-one thorn until pinned here.
        _COMMON_NODE_DEPS = ["onnx"]
        pr("Installing common node dependencies (onnx, …)...")
        subprocess.run(
            [venv_python, "-m", "pip", "install", *_COMMON_NODE_DEPS, "--quiet"],
            capture_output=True, timeout=600, env=build_env
        )
        ok("USB ComfyUI venv setup complete.")
    except Exception as e:
        warn(f"USB ComfyUI venv setup failed: {e}")
        warn("ComfyUI will fall back to desktop install if available.")


def _bg_install_node_deps(python_exe, jobs):
    """Install custom-node requirements in a DETACHED background process so a slow
    node (e.g. comfyui-impact-pack, whose heavy deps download silently under
    --quiet and look frozen) never blocks or crashes boot. The helper installs
    each job sequentially — pip is not safe to run concurrently against one env —
    logging progress to node_deps.log. System 32 launches immediately; anything
    not ready in time is picked up later by install_missing_nodes. Returns True if
    the background process was spawned, False (→ caller does a bounded blocking
    fallback) otherwise. jobs: list of (node_folder, requirements_path) tuples."""
    log_path = os.path.join(SCRIPT_DIR, "node_deps.log")
    helper = os.path.join(SCRIPT_DIR, "_node_deps_bg.py")
    # repr() embeds Windows backslash paths as valid Python string literals.
    script = (
        "import subprocess, time\n"
        f"PY = {python_exe!r}\n"
        f"JOBS = {jobs!r}\n"
        f"LOG = {log_path!r}\n"
        "def w(m):\n"
        "    try:\n"
        "        open(LOG, 'a', encoding='utf-8').write(time.strftime('%H:%M:%S ') + m + '\\n')\n"
        "    except Exception:\n"
        "        pass\n"
        "w('=== node deps install started (%d jobs) ===' % len(JOBS))\n"
        "for name, req in JOBS:\n"
        "    w('installing ' + name)\n"
        "    try:\n"
        "        r = subprocess.run([PY, '-m', 'pip', 'install', '-r', req,\n"
        "                            '--quiet', '--no-input', '--prefer-binary'],\n"
        "                           capture_output=True, text=True, timeout=1800)\n"
        "        w(('OK ' if r.returncode == 0 else 'PARTIAL ') + name)\n"
        "    except Exception as e:\n"
        "        w('ERR ' + name + ': ' + str(e)[:160])\n"
        "w('=== done ===')\n"
    )
    try:
        with open(helper, "w", encoding="utf-8") as f:
            f.write(script)
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        subprocess.Popen(
            [python_exe, helper], creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception as e:
        warn(f"Background node-deps launch failed: {str(e)[:120]}")
        return False


def main():
    print("""
  ==========================================
        S Y S T E M   3 2
      First-Run Bootstrap
  ==========================================
""")

    force = "--force" in sys.argv
    if AIRPLANE:
        warn("Airplane mode is on — checking what's installed, downloading nothing.")

    # ── Always run fresh discovery — plug and play ─────────────────────────────
    # Preserve user-set output paths before deleting config
    preserved = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                old_cfg = json.load(f)
            # Keep only user preferences — not machine-discovered paths
            for key in ("web_port", "reference_dir",
                        "lmstudio_model", "lmstudio_model_key",
                        "lmstudio_model_path",
                        "lmstudio_context_length", "lmstudio_gpu_offload",
                        "lmstudio_gpu_layers", "inference_backend",
                        "web_proxy", "web_proxy_http", "web_proxy_https",
                        "update_check", "update_repo"):
                if key in old_cfg:
                    preserved[key] = old_cfg[key]
        except Exception:
            pass
        # Debug: show what we're preserving
        if "lmstudio_model_path" in preserved:
            print(f"  [..] Preserving model path: {preserved['lmstudio_model_path'][:60]}")
        else:
            print(f"  [??] lmstudio_model_path not found in config to preserve")
        try:
            os.remove(CONFIG_FILE)
        except Exception:
            pass

    # ── Fresh discovery ────────────────────────────────────────────────────────
    python_exe     = find_python()
    if not python_exe:
        input("\n  Press Enter to exit.")
        sys.exit(1)

    ensure_packages(python_exe)

    # System 32 itself runs on the BUNDLED python (see Start.bat's goto), not the
    # ComfyUI venv — so its runtime deps (psutil for RAM/awareness, pillow,
    # requests, ...) must be installed there too, or `import psutil` fails at
    # runtime even though the venv check above said "ready".
    bundled_py = os.path.join(SCRIPT_DIR, "python", "python.exe")
    if os.path.exists(bundled_py) and \
       os.path.abspath(bundled_py) != os.path.abspath(python_exe):
        ensure_runtime_packages(bundled_py)

    lms_exe        = find_lmstudio()
    # Skip lms import — models already cataloged, import hangs on --yes flag
    # _ensure_llm_imported(lms_exe)

    # Download llama-server.exe if not present
    script_dir = os.path.dirname(os.path.abspath(__file__))
    _ensure_llama_server(script_dir)
    _ensure_cuda_runtime(script_dir)      # without it the model silently runs on the CPU
    _ensure_vulkan_backend(script_dir)    # a non-NVIDIA card (AMD) for the model

    # ComfyUI (downloaded if missing), its venv, and the voice files
    _ensure_comfyui(script_dir)
    _ensure_usb_comfy_venv(script_dir)
    _ensure_voice_files(script_dir)
    _ensure_static(script_dir)

    comfy_installs = find_comfyui_installs()

    if os.path.exists(os.path.join(script_dir, "ComfyUI", "main.py")):
        ok(f"Using the ComfyUI on this drive: {os.path.join(script_dir, 'ComfyUI')}")
    elif not comfy_installs:
        warn("No ComfyUI installs found. Config will use defaults — edit config.json after setup.")

    cfg = build_config(python_exe, lms_exe, comfy_installs)
    # Restore user-set paths that survived the fresh discovery
    # Do this BEFORE the llamacpp path check so preserved model path is visible
    if preserved:
        cfg.update(preserved)
    print(f"  [..] After preserve: lmstudio_model_path = '{cfg.get('lmstudio_model_path','')[:60]}'")

    # Log model path status after preservation
    if cfg.get("inference_backend") == "llamacpp":
        saved_path = cfg.get("lmstudio_model_path", "")
        # Auto-recover from model_settings.json if path is missing
        if not saved_path or not os.path.exists(saved_path):
            settings_file = os.path.join(SCRIPT_DIR, "model_settings.json")
            if os.path.exists(settings_file):
                try:
                    with open(settings_file, "r") as _sf:
                        model_settings = json.load(_sf)
                    for filename in model_settings.keys():
                        for llm_base in _llm_storage_dirs():
                            for root, dirs, files in os.walk(llm_base):
                                for fname in files:
                                    if fname.lower() == filename.lower():
                                        found = os.path.join(root, fname)
                                        cfg["lmstudio_model_path"] = found
                                        saved_path = found
                                        ok(f"Auto-recovered model path: {found}")
                                        break
                                if saved_path and os.path.exists(saved_path):
                                    break
                            if saved_path and os.path.exists(saved_path):
                                break
                except Exception as _e:
                    pass

        if not saved_path or not os.path.exists(saved_path):
            # Nothing picked yet (a fresh drive): use the model in LLM Storage.
            _m, _n = _find_llm_model()
            if _m and not saved_path:
                cfg["lmstudio_model_path"] = _m
                cfg["lmstudio_model"] = cfg["lmstudio_model_key"] = \
                    os.path.splitext(os.path.basename(_m))[0].lower()
                saved_path = _m
                ok(f"Found a language model in LLM Storage: {os.path.basename(_m)}"
                   + (f" (newest of {_n} — switch any time with the model switcher)" if _n > 1 else ""))
        if saved_path and os.path.exists(saved_path):
            ok(f"Model path: {saved_path}")
        elif saved_path:
            warn(f"Last model not found: {saved_path}")
            warn("Use the model switcher to select a model.")
        else:
            _ensure_llm_storage()
    # A fresh drive gets its reference folder NEXT TO Sys 32's Home ("System 32's reference
    # material", like the Output folder). Only when neither place exists yet — an existing
    # one is never moved (2026-11-01: a fresh copy put it inside Sys 32's Home instead).
    _ref_top = os.path.join(os.path.dirname(SCRIPT_DIR), "System 32's reference material")
    _ref_in = os.path.join(SCRIPT_DIR, "reference material")
    if not os.path.isdir(_ref_top) and not os.path.isdir(_ref_in) and not preserved.get("reference_dir"):
        try:
            os.makedirs(_ref_top)
            ok(f"Made the reference folder: {_ref_top}")
        except Exception as _re_err:
            warn(f"Couldn't make {_ref_top}: {_re_err}")
    ensure_extra_models_yaml(cfg)
    create_output_folders(cfg)
    write_index(cfg)
    write_code_index(cfg)

    # ── Install custom node requirements into the Documents venv ───────────────
    # ComfyUI Desktop loads custom node Python code via the Documents venv.
    # Dependencies must be installed there — NOT into the uv Python.
    docs_venv = os.path.join(
        os.environ.get("USERPROFILE", ""),
        "Documents", "ComfyUI", ".venv", "Scripts", "python.exe"
    )
    if not os.path.exists(docs_venv):
        docs_venv = cfg.get("comfy_image_python", "")

    # Only into a real ComfyUI venv — with no ComfyUI yet this fell back to whatever Python
    # setup ran on and filled the HOST's Python with ComfyUI packages (2026-11-01).
    _is_venv = os.path.basename(os.path.dirname(os.path.dirname(docs_venv or ""))).lower() in (".venv", "venv")
    if docs_venv and os.path.exists(docs_venv) and _is_venv:
        header("Installing custom node dependencies")
        pr(f"Target Python: {docs_venv}")

        # Core packages that nodes commonly need
        core_node_packages = [
            "opencv-python",   # cv2 — required by Easy-Use and many others
            "compel",          # required by ComfyUI-J
            "accelerate",      # required by WanVideoWrapper and others
            "diffusers",       # required by Easy-Use
        ]
        for pkg in core_node_packages:
            try:
                r = subprocess.run(
                    [docs_venv, "-m", "pip", "install", pkg, "--quiet"],
                    capture_output=True, text=True, timeout=420
                )
                if r.returncode == 0:
                    ok(f"{pkg} installed into Documents venv")
                else:
                    warn(f"{pkg} install failed — some nodes may not load")
            except subprocess.TimeoutExpired:
                warn(f"{pkg} install timed out — skipping (retry later with install_missing_nodes)")
            except Exception as e:
                warn(f"{pkg} install error: {str(e)[:120]} — skipping")

        # Scan all custom_nodes directories and install their requirements.txt
        custom_node_dirs = []
        comfy_image_dir = cfg.get("comfy_image_dir", "")
        if comfy_image_dir:
            custom_node_dirs.append(os.path.join(comfy_image_dir, "custom_nodes"))

        # Detect USB comfyui resources folder dynamically
        # Check relative to script dir first, then scan drive root
        usb_resource_candidates = [
            # Same drive as the USB, common naming patterns
            os.path.join(os.path.splitdrive(SCRIPT_DIR)[0] + "\\", "Sys 32's comfyui resources"),
            os.path.join(os.path.splitdrive(SCRIPT_DIR)[0] + "\\", "comfyui resources"),
            os.path.join(os.path.splitdrive(SCRIPT_DIR)[0] + "\\", "ComfyUI Resources"),
            os.path.join(os.path.splitdrive(SCRIPT_DIR)[0] + "\\", "System 32 ComfyUI"),
            # Relative to script
            os.path.join(os.path.dirname(SCRIPT_DIR), "comfyui resources"),
            os.path.join(os.path.dirname(SCRIPT_DIR), "Sys 32's comfyui resources"),
        ]
        for candidate in usb_resource_candidates:
            nodes_candidate = os.path.join(candidate, "custom_nodes")
            if os.path.exists(nodes_candidate):
                custom_node_dirs.append(nodes_candidate)
                ok(f"Found USB comfyui resources: {candidate}")
                break

        node_req_jobs = []
        for nodes_dir in custom_node_dirs:
            if not os.path.exists(nodes_dir):
                continue
            for node_folder in os.listdir(nodes_dir):
                node_req = os.path.join(nodes_dir, node_folder, "requirements.txt")
                if os.path.exists(node_req):
                    node_req_jobs.append((node_folder, node_req))

        if node_req_jobs:
            if _bg_install_node_deps(docs_venv, node_req_jobs):
                pr(f"Installing {len(node_req_jobs)} node dep set(s) in the background — "
                   f"System 32 starts now; progress in node_deps.log. Missing ones are "
                   f"picked up later by install_missing_nodes.")
            else:
                # Fallback: bounded, non-fatal blocking install (never crashes boot)
                for node_folder, node_req in node_req_jobs:
                    pr(f"Installing {node_folder} requirements...")
                    try:
                        r = subprocess.run(
                            [docs_venv, "-m", "pip", "install", "-r", node_req,
                             "--quiet", "--no-input", "--prefer-binary"],
                            capture_output=True, text=True, timeout=300
                        )
                        if r.returncode == 0:
                            ok(f"{node_folder} dependencies installed")
                        else:
                            warn(f"{node_folder} some dependencies failed")
                    except Exception as e:
                        warn(f"{node_folder} deps skipped: {str(e)[:120]}")

    # ── Write config ───────────────────────────────────────────────────────────
    header("Writing config.json")
    # Model key is already preserved via cfg.update(preserved) above
    # No need to re-read config here — it was deleted at bootstrap start
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)
    ok(f"Saved: {CONFIG_FILE}")

    # ── Summary ────────────────────────────────────────────────────────────────
    print("""
  ==========================================
   Setup complete. Review config.json if
   anything looks wrong, then relaunch.
  ==========================================

  Key values discovered:""")
    for k in ("comfy_image_dir", "comfy_video_dir", "comfy_image_python", "lms_exe"):
        print(f"    {k}: {cfg.get(k,'')}")

    print()
    if os.environ.get("S32_AUTOSTART") == "1":
        print("  Starting fresh after the factory reset...")     # no one to press Enter
    else:
        input("  Press Enter to launch System 32...")
    return cfg


if __name__ == "__main__":
    cfg = main()

    # ── Hand off to system32.py ────────────────────────────────────────────────
    if not os.path.exists(SYSTEM32_PY):
        print(f"  [!!] system32.py not found at: {SYSTEM32_PY}")
        input("  Press Enter to exit.")
        sys.exit(1)

    # Always use the same Python that ran bootstrap to run system32.py
    # Using comfy_image_python here causes module not found errors
    # System 32 gets a clean environment: airplane mode is ITS switch at runtime (turning
    # it off must bring installs back without a restart), so the boot-time pip block
    # isn't handed down.
    _env = dict(os.environ)
    _env.pop("PIP_NO_INDEX", None)
    result = subprocess.run(
        [sys.executable, SYSTEM32_PY],
        cwd=SCRIPT_DIR, env=_env
    )
    sys.exit(result.returncode)
