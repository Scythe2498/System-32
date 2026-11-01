# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_steam.py — Steam games launcher + GPU/CPU resource swap.

Scans every Steam library (fresh, so added/removed games show up), reads each
installed game from its appmanifest, resolves local library art for icons, and
groups by genre (cached; fetched once from Steam's public store API). Launches
games two ways:

  • "user"  — YOU play: ComfyUI is closed first, then the model, freeing the
              GPU/CPU for the game. A watcher waits for the game to exit, then
              boots it back up (model first, then ComfyUI).
  • "s32"   — IT plays: its brain (the model) stays up because it needs it to
              play; ComfyUI is closed to free VRAM. Actually controlling the game
              (screen-vision + input) is the game-control ecosystem — this just
              launches it and marks its in-game.

Nothing here renders gameplay behind the chat; that overlay is a future piece.
"""

import os
import re
import json
import time
import threading
import subprocess

try:
    from s32_config import CONFIG, REFERENCE_DIR, state, log, _machine_name
except Exception:
    CONFIG, REFERENCE_DIR, state, log = {}, None, {}, (lambda *a, **k: None)
    _machine_name = "unknown"

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Non-game library entries to hide from the grid
_SKIP_NAMES = ("steamworks common redistributables", "steam linux runtime",
               "proton", "steamvr", "steam controller")
_SKIP_APPIDS = {228980}   # Steamworks Common Redistributables


# ── VDF parsing (libraryfolders.vdf + appmanifest_*.acf) ─────────────────────
def _vdf_tokens(s):
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in " \t\r\n":
            i += 1; continue
        if c in "{}":
            yield c; i += 1; continue
        if c == '"':
            j = i + 1; buf = []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j + 1]); j += 2
                else:
                    buf.append(s[j]); j += 1
            yield "".join(buf); i = j + 1; continue
        j = i
        while j < n and s[j] not in " \t\r\n{}\"":
            j += 1
        yield s[i:j]; i = j


def _parse_vdf(text):
    toks = list(_vdf_tokens(text))
    pos = 0

    def obj():
        nonlocal pos
        d = {}
        while pos < len(toks):
            t = toks[pos]
            if t == "}":
                pos += 1; break
            key = t; pos += 1
            if pos < len(toks) and toks[pos] == "{":
                pos += 1; d[key] = obj()
            else:
                d[key] = toks[pos] if pos < len(toks) else ""; pos += 1
        return d

    root = {}
    while pos < len(toks):
        key = toks[pos]; pos += 1
        if pos < len(toks) and toks[pos] == "{":
            pos += 1; root[key] = obj()
        else:
            root[key] = toks[pos] if pos < len(toks) else ""; pos += 1
    return root


# ── Steam discovery ──────────────────────────────────────────────────────────
def find_steam():
    if CONFIG.get("steam_path") and os.path.isdir(CONFIG["steam_path"]):
        return CONFIG["steam_path"]
    try:
        import winreg
        probes = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
        for hk, path, val in probes:
            try:
                with winreg.OpenKey(hk, path) as k:
                    p, _ = winreg.QueryValueEx(k, val)
                    if p and os.path.isdir(p):
                        return p
            except Exception:
                continue
    except Exception:
        pass
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        if os.path.isdir(p):
            return p
    return None


def _library_paths(steam):
    libs = []
    if steam and os.path.isdir(steam):
        libs.append(steam)
    lf = os.path.join(steam or "", "steamapps", "libraryfolders.vdf")
    if os.path.exists(lf):
        try:
            with open(lf, encoding="utf-8", errors="ignore") as f:
                data = _parse_vdf(f.read())
            folders = data.get("libraryfolders") or data.get("LibraryFolders") or {}
            for _k, v in folders.items():
                path = v.get("path") if isinstance(v, dict) else v
                if path and os.path.isdir(path) and path not in libs:
                    libs.append(path)
        except Exception as e:
            log(f"[S32][STEAM] libraryfolders parse failed: {e}")
    return libs


def _read_manifest(acf_path, library):
    try:
        with open(acf_path, encoding="utf-8", errors="ignore") as f:
            d = _parse_vdf(f.read())
        app = d.get("AppState", {})
        appid = app.get("appid"); name = app.get("name")
        installdir = app.get("installdir", "")
        if not (appid and name):
            return None
        appid = int(appid)
        if appid in _SKIP_APPIDS or name.strip().lower() in _SKIP_NAMES:
            return None
        if any(s in name.lower() for s in _SKIP_NAMES):
            return None
        full_install = os.path.join(library, "steamapps", "common", installdir) if installdir else ""
        return {"appid": appid, "name": name, "installdir": full_install, "library": library}
    except Exception:
        return None


def scan_libraries():
    """Fresh scan of every library → list of installed games."""
    steam = find_steam()
    if not steam:
        return []
    games, seen = [], set()
    for lib in _library_paths(steam):
        sa = os.path.join(lib, "steamapps")
        if not os.path.isdir(sa):
            continue
        try:
            for fn in os.listdir(sa):
                if fn.startswith("appmanifest_") and fn.endswith(".acf"):
                    g = _read_manifest(os.path.join(sa, fn), lib)
                    if g and g["appid"] not in seen:
                        seen.add(g["appid"]); games.append(g)
        except Exception as e:
            log(f"[S32][STEAM] scan {sa} failed: {e}")
    games.sort(key=lambda g: g["name"].lower())
    return games


# ── Icons (local Steam library art) ──────────────────────────────────────────
def icon_path(appid):
    steam = find_steam()
    if not steam:
        return None
    cache = os.path.join(steam, "appcache", "librarycache")
    candidates = [
        os.path.join(cache, f"{appid}_library_600x900.jpg"),
        os.path.join(cache, f"{appid}_header.jpg"),
        os.path.join(cache, f"{appid}_capsule_231x87.jpg"),
        os.path.join(cache, f"{appid}_icon.jpg"),
        os.path.join(cache, str(appid), "library_600x900.jpg"),
        os.path.join(cache, str(appid), "header.jpg"),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def cdn_icon(appid):
    return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg"


# ════════════════════════════════════════════════════════════════════════════
#  Multi-store game library — Steam + Epic + GOG + Ubisoft + EA + Xbox.
#  Every game gets a string uid "<platform>:<id>" so non-Steam titles can be
#  categorized / hidden / ordered right alongside Steam ones.
# ════════════════════════════════════════════════════════════════════════════
_STORE_META = {
    "steam":     {"label": "Steam Games", "shop": "https://store.steampowered.com"},
    "epic":      {"label": "Epic Games",  "shop": "https://store.epicgames.com"},
    "gog":       {"label": "GOG",         "shop": "https://www.gog.com"},
    "ea":        {"label": "EA Games",    "shop": "https://www.ea.com/games"},
    "ubisoft":   {"label": "Ubisoft",     "shop": "https://store.ubisoft.com"},
    "xbox":      {"label": "Xbox",        "shop": "https://www.xbox.com/play/games"},
    "battlenet": {"label": "Battle.net",  "shop": "https://shop.battle.net"},
}
_PLAT_ORDER = ("steam", "epic", "ea", "ubisoft", "gog", "xbox", "battlenet")


def _regval(key, name):
    try:
        import winreg
        v, _ = winreg.QueryValueEx(key, name)
        return v
    except Exception:
        return None


def _first_ico(folder):
    try:
        for fn in os.listdir(folder):
            if fn.lower().endswith(".ico"):
                return os.path.join(folder, fn)
    except Exception:
        pass
    return None


def _scan_epic():
    """Epic — install manifests are clean JSON (.item files)."""
    out = []
    d = os.path.expandvars(r"%PROGRAMDATA%\Epic\EpicGamesLauncher\Data\Manifests")
    if not os.path.isdir(d):
        return out
    for fn in os.listdir(d):
        if not fn.lower().endswith(".item"):
            continue
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                m = json.load(f)
        except Exception:
            continue
        name = m.get("DisplayName") or m.get("AppName")
        appn = m.get("AppName")
        inst = m.get("InstallLocation") or ""
        exe = m.get("LaunchExecutable") or ""
        if not name or not appn or not inst or not exe or exe.lower().endswith(".msi"):
            continue
        full_exe = os.path.join(inst, exe)
        out.append({"uid": f"epic:{appn}", "name": name, "platform": "epic",
                    "installdir": inst,
                    "launch": {"kind": "uri",
                               "uri": f"com.epicgames.launcher://apps/{appn}?action=launch&silent=true",
                               "exe": full_exe},
                    "icon": _first_ico(inst)})
    return out


def _scan_gog():
    out = []
    try:
        import winreg
    except Exception:
        return out
    for sub in (r"SOFTWARE\WOW6432Node\GOG.com\Games", r"SOFTWARE\GOG.com\Games"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub)
        except Exception:
            continue
        i = 0
        while True:
            try:
                gid = winreg.EnumKey(root, i); i += 1
            except OSError:
                break
            try:
                gk = winreg.OpenKey(root, gid)
                name = _regval(gk, "gameName")
                path = _regval(gk, "path")
                exe = _regval(gk, "exe") or _regval(gk, "exeFile")
                if name and path:
                    full = exe if (exe and os.path.isabs(exe)) else (os.path.join(path, exe) if exe else path)
                    out.append({"uid": f"gog:{gid}", "name": name, "platform": "gog",
                                "installdir": path, "launch": {"kind": "exe", "exe": full},
                                "icon": _first_ico(path)})
            except Exception:
                continue
    return out


def _scan_ubisoft():
    out = []
    try:
        import winreg
    except Exception:
        return out
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs")
    except Exception:
        return out
    i = 0
    while True:
        try:
            gid = winreg.EnumKey(root, i); i += 1
        except OSError:
            break
        try:
            gk = winreg.OpenKey(root, gid)
            inst = _regval(gk, "InstallDir")
            if inst:
                inst = inst.replace("/", "\\").rstrip("\\")
                name = os.path.basename(inst) or f"Ubisoft {gid}"
                out.append({"uid": f"ubisoft:{gid}", "name": name, "platform": "ubisoft",
                            "installdir": inst,
                            "launch": {"kind": "uri", "uri": f"uplay://launch/{gid}/0"},
                            "icon": _first_ico(inst)})
        except Exception:
            continue
    return out


def _scan_ea():
    out = []
    try:
        import winreg
    except Exception:
        return out
    for sub in (r"SOFTWARE\WOW6432Node\EA Games", r"SOFTWARE\WOW6432Node\Origin Games",
                r"SOFTWARE\EA Games", r"SOFTWARE\Origin Games"):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub)
        except Exception:
            continue
        i = 0
        while True:
            try:
                gid = winreg.EnumKey(root, i); i += 1
            except OSError:
                break
            try:
                gk = winreg.OpenKey(root, gid)
                inst = _regval(gk, "Install Dir") or _regval(gk, "InstallDir") or _regval(gk, "InstallLocation")
                disp = _regval(gk, "DisplayName")
                if inst:
                    inst = inst.replace("/", "\\").rstrip("\\")
                    name = disp or os.path.basename(inst) or f"EA {gid}"
                    out.append({"uid": f"ea:{gid}", "name": name, "platform": "ea",
                                "installdir": inst,
                                "launch": {"kind": "uri", "uri": f"origin://launchgame/{gid}"},
                                "icon": _first_ico(inst)})
            except Exception:
                continue
    return out


def _xbox_name(content):
    cfg = os.path.join(content, "MicrosoftGame.config")
    if not os.path.isfile(cfg):
        return None
    try:
        import xml.etree.ElementTree as ET
        root = ET.parse(cfg).getroot()
        el = root.find(".//ShellVisibleName")
        if el is not None and el.text and el.text.strip():
            return el.text.strip()
    except Exception:
        pass
    return None


def _xbox_logo(content):
    try:
        for cand in ("StoreLogo.png", "Square150x150Logo.png", "Logo.png", "Square44x44Logo.png"):
            p = os.path.join(content, cand)
            if os.path.isfile(p):
                return p
        img = os.path.join(content, "Images")
        if os.path.isdir(img):
            for fn in os.listdir(img):
                if "logo" in fn.lower() and fn.lower().endswith(".png"):
                    return os.path.join(img, fn)
    except Exception:
        pass
    return None


def _scan_xbox():
    """Xbox / Game Pass PC games install to <drive>:\\XboxGames\\<Title>\\Content\\
    with Microsoft's own 'gamelaunchhelper.exe' stub — running it launches the game
    through the proper gaming runtime (no AUMID needed)."""
    out = []
    import string
    for drive in string.ascii_uppercase:
        root = f"{drive}:\\XboxGames"
        if not os.path.isdir(root):
            continue
        try:
            titles = os.listdir(root)
        except Exception:
            continue
        for title in titles:
            content = os.path.join(root, title, "Content")
            helper = os.path.join(content, "gamelaunchhelper.exe")
            if not os.path.isfile(helper):
                continue
            out.append({"uid": f"xbox:{title}", "name": _xbox_name(content) or title,
                        "platform": "xbox", "installdir": content,
                        "launch": {"kind": "exe", "exe": helper},
                        "icon": _xbox_logo(content)})
    return out


_PROVIDERS = (_scan_epic, _scan_gog, _scan_ubisoft, _scan_ea, _scan_xbox)
_icon_cache = {}


def scan_all():
    """Every installed game across every detected store, unified."""
    games = []
    for g in scan_libraries():                    # Steam (existing logic)
        games.append({"uid": f"steam:{g['appid']}", "appid": g["appid"], "name": g["name"],
                      "platform": "steam", "installdir": g.get("installdir", ""),
                      "launch": {"kind": "steam", "appid": g["appid"]},
                      "icon": icon_path(g["appid"])})
    for prov in _PROVIDERS:
        try:
            games.extend(prov())
        except Exception as e:
            log(f"[S32][LIB] {getattr(prov, '__name__', '?')} failed: {e}")
    _icon_cache.clear()
    for g in games:
        if g.get("icon"):
            _icon_cache[g["uid"]] = g["icon"]
    return games


# ── Custom game-tile icons (operator-set; override native/CDN art) ───────────
#  Stores an operator-chosen image (a screenshot, or any PNG/JPG/…) as a game's
#  tile icon — the fix for store games that ship no usable art. Images live in
#  REFERENCE_DIR/game_icons/ (persistent on the USB), mapped uid→filename in
#  custom_icons.json. icon_for_uid() and _art_url() consult these first.
_ICON_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff",      ".jpg"),
    (b"GIF87a",            ".gif"),
    (b"GIF89a",            ".gif"),
    (b"BM",                ".bmp"),
    (b"RIFF",              ".webp"),   # RIFF....WEBP (verified below)
    (b"\x00\x00\x01\x00",  ".ico"),
]
_custom_icons = None     # {uid: filename}


def _custom_icon_dir():
    if not REFERENCE_DIR:
        return None
    d = os.path.join(REFERENCE_DIR, "game_icons")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _custom_icon_map_path():
    return os.path.join(REFERENCE_DIR, "custom_icons.json") if REFERENCE_DIR else None


def _load_custom_icons():
    global _custom_icons
    if _custom_icons is None:
        p = _custom_icon_map_path()
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    _custom_icons = json.load(f)
            except Exception:
                _custom_icons = {}
        else:
            _custom_icons = {}
    return _custom_icons


def _save_custom_icons():
    p = _custom_icon_map_path()
    if not p or _custom_icons is None:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_custom_icons, f, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        log(f"[S32][LIB] custom icon map save failed: {e}")


def _sniff_image_ext(data):
    for magic, ext in _ICON_MAGIC:
        if data[:len(magic)] == magic:
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


def _safe_uid_name(uid):
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(uid))[:80] or "icon"


def custom_icon_for(uid):
    """Path to the operator-set icon for this uid, or None."""
    fn = _load_custom_icons().get(str(uid))
    if not fn:
        return None
    d = _custom_icon_dir()
    p = os.path.join(d, fn) if d else None
    return p if (p and os.path.exists(p)) else None


def has_custom_icon(uid):
    return custom_icon_for(uid) is not None


def set_custom_icon(uid, src_path=None, b64=None):
    """Store an image as the tile icon for `uid`. Source is a file path (copied)
    or base64 bytes (e.g. a live screen frame). Returns {ok, path} or {error}."""
    uid = str(uid or "").strip()
    if not uid:
        return {"error": "No game specified."}
    d = _custom_icon_dir()
    if not d:
        return {"error": "No reference directory available for icon storage."}
    try:
        if b64:
            import base64 as _b64
            raw = _b64.b64decode(str(b64).split(",", 1)[-1])
        elif src_path and os.path.exists(src_path):
            with open(src_path, "rb") as f:
                raw = f.read()
        else:
            return {"error": "No image source provided."}
    except Exception as e:
        return {"error": f"Couldn't read image: {str(e)[:80]}"}
    if not raw or len(raw) < 16:
        return {"error": "Image is empty or unreadable."}
    if len(raw) > 12 * 1024 * 1024:
        return {"error": "Image too large (max 12 MB)."}
    ext = _sniff_image_ext(raw)
    if not ext:
        return {"error": "Not a recognized image (PNG / JPG / GIF / BMP / WEBP / ICO)."}
    m = _load_custom_icons()
    old = m.get(uid)                       # clear any prior file (extension may differ)
    if old:
        try:
            op = os.path.join(d, old)
            if os.path.exists(op):
                os.remove(op)
        except Exception:
            pass
    fn = _safe_uid_name(uid) + ext
    try:
        with open(os.path.join(d, fn), "wb") as f:
            f.write(raw)
    except Exception as e:
        return {"error": f"Couldn't save icon: {str(e)[:80]}"}
    m[uid] = fn
    _save_custom_icons()
    _icon_cache.pop(uid, None)             # force re-resolve on next render
    log(f"[S32][LIB] custom icon set for {uid} ({len(raw)} bytes)")
    return {"ok": True, "uid": uid, "path": os.path.join(d, fn)}


def remove_custom_icon(uid):
    uid = str(uid or "").strip()
    m = _load_custom_icons()
    fn = m.pop(uid, None)
    if fn:
        d = _custom_icon_dir()
        try:
            p = os.path.join(d, fn) if d else None
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        _save_custom_icons()
        _icon_cache.pop(uid, None)
        log(f"[S32][LIB] custom icon removed for {uid}")
        return {"ok": True, "uid": uid}
    return {"ok": True, "uid": uid, "noop": True}


def icon_for_uid(uid):
    uid = str(uid)
    c = custom_icon_for(uid)          # operator override always wins
    if c:
        return c
    if uid in _icon_cache:
        return _icon_cache[uid]
    if uid.startswith("steam:"):
        try:
            return icon_path(int(uid.split(":", 1)[1]))
        except Exception:
            return None
    return None


def _art_url(g):
    from urllib.parse import quote
    uid = g.get("uid", "")
    cp = custom_icon_for(uid)          # operator-set icon wins, even with no native art
    if cp:
        try:
            v = int(os.path.getmtime(cp) * 1000)     # ms mtime busts the browser cache on change
        except Exception:
            v = 0
        return f"/api/steam/icon?appid={quote(uid)}&v={v}"
    if g.get("icon"):
        return f"/api/steam/icon?appid={quote(g['uid'])}"
    if g["platform"] == "steam":
        return cdn_icon(g["appid"])
    return ""


# ── Genre (cached; one public store-API lookup per new game) ─────────────────
def _genre_cache_path():
    return os.path.join(REFERENCE_DIR, "steam_genres.json") if REFERENCE_DIR else None


_genre_cache = None


def _load_genres():
    global _genre_cache
    if _genre_cache is None:
        p = _genre_cache_path()
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    _genre_cache = json.load(f)
            except Exception:
                _genre_cache = {}
        else:
            _genre_cache = {}
    return _genre_cache


def _save_genres():
    p = _genre_cache_path()
    if not p or _genre_cache is None:
        return
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_genre_cache, f)
        os.replace(tmp, p)
    except Exception:
        pass


def _fetch_genre(appid):
    """One lightweight metadata GET from Steam's public store API. Disable with
    CONFIG['steam_fetch_genres']=False (then everything is 'Uncategorized')."""
    if not CONFIG.get("steam_fetch_genres", True):
        return None
    try:
        import urllib.request
        url = f"https://store.steampowered.com/api/appdetails?appids={appid}&filters=genres"
        req = urllib.request.Request(url, headers={"User-Agent": "System32/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.load(r)
        node = data.get(str(appid), {})
        if not node.get("success"):
            return ["Uncategorized"]
        genres = node.get("data", {}).get("genres", [])
        names = [g.get("description") for g in genres if g.get("description")]
        return names or ["Uncategorized"]
    except Exception:
        return None


def genres_for(appid, blocking=False):
    cache = _load_genres()
    key = str(appid)
    if key in cache:
        return cache[key]
    if blocking:
        g = _fetch_genre(appid)
        if g is not None:
            cache[key] = g; _save_genres()
            return g
    return None  # not yet known


def _resolve_genres_bg(appids):
    cache = _load_genres()
    changed = False
    for appid in appids:
        if str(appid) in cache:
            continue
        g = _fetch_genre(appid)
        if g is not None:
            cache[str(appid)] = g; changed = True
            time.sleep(0.4)   # be gentle on the store API
    if changed:
        _save_genres()


# ── Listing for the UI (grouped by genre) ────────────────────────────────────
def list_games(show_hidden=False):
    return _list_games(show_hidden=show_hidden)


# ── Hidden games + custom categories (UI prefs) ──────────────────────────────
def _ui_path():
    return os.path.join(REFERENCE_DIR, "steam_ui.json") if REFERENCE_DIR else None


_ui = None


def _mig_uid(a):
    a = str(a)
    if ":" in a:
        return a
    return f"steam:{a}" if a.isdigit() else a


def _ui_key():
    # Per-machine namespace so each PC keeps its OWN categories/hidden/order.
    # Plugging the drive into another computer can no longer clobber this one's
    # library view — which is what made categories vanish on the round trip.
    return _machine_name or "default"


def _parse_ui_bucket(bucket):
    """Normalize one machine's saved library-view prefs into the in-memory shape."""
    out = {"hidden": [], "categories": [], "order": []}
    if not isinstance(bucket, dict):
        return out
    if isinstance(bucket.get("hidden"), list):
        out["hidden"] = [_mig_uid(a) for a in bucket["hidden"]]
    if isinstance(bucket.get("order"), list):
        out["order"] = [_mig_uid(a) for a in bucket["order"]]
    if isinstance(bucket.get("categories"), list):
        out["categories"] = [
            {"name": str(c.get("name", "")).strip(),
             "appids": [_mig_uid(a) for a in c.get("appids", [])],
             "hidden": bool(c.get("hidden", False)),
             "color": str(c.get("color", "") or ""),
             "parent": str(c.get("parent", "") or ""),
             "collapsed": bool(c.get("collapsed", False))}
            for c in bucket["categories"] if c.get("name")]
    return out


def _load_ui():
    global _ui
    if _ui is None:
        _ui = {"hidden": [], "categories": [], "order": []}
        p = _ui_path()
        if p and os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if isinstance(d.get("machines"), dict):
                    # Machine-scoped format: read ONLY this machine's bucket.
                    _ui = _parse_ui_bucket(d["machines"].get(_ui_key(), {}))
                else:
                    # Legacy flat file (shared across machines). Adopt it as THIS
                    # machine's prefs one time; the next save rewrites it scoped, so
                    # other machines then begin with their own fresh library view.
                    _ui = _parse_ui_bucket(d)
            except Exception:
                pass
    return _ui


def _save_ui():
    p = _ui_path()
    if not p or _ui is None:
        return
    try:
        # Preserve every OTHER machine's bucket; update only this machine's. Read
        # the file fresh each save so writing here can never erase categories made
        # on a different PC (the bug that made them vanish on the round trip).
        doc = {"machines": {}}
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    existing = json.load(f)
                if isinstance(existing.get("machines"), dict):
                    doc["machines"] = existing["machines"]
            except Exception:
                pass
        doc["machines"][_ui_key()] = _ui
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
        os.replace(tmp, p)
    except Exception as e:
        log(f"[S32][STEAM] ui prefs save failed: {e}")


def set_hidden(appid, hidden=True):
    ui = _load_ui()
    a = str(appid)
    if not a:
        return {"ok": False, "error": "bad uid"}
    h = set(ui["hidden"])
    h.add(a) if hidden else h.discard(a)
    ui["hidden"] = sorted(h)
    _save_ui()
    return {"ok": True, "appid": a, "hidden": bool(hidden)}


def _cat_by_name(ui, name):
    for c in ui["categories"]:
        if c["name"].lower() == (name or "").lower():
            return c
    return None


def category_op(op, name=None, new_name=None, appid=None, category=None,
                index=None, hidden=None, parent=None, color=None, order=None,
                collapsed=None):
    ui = _load_ui()
    op = (op or "").lower()
    if op == "create":
        name = (name or "").strip()[:40]
        if not name:
            return {"ok": False, "error": "name required"}
        if _cat_by_name(ui, name):
            return {"ok": False, "error": "a category with that name exists"}
        par = (parent or "").strip()
        if par and not _cat_by_name(ui, par):
            return {"ok": False, "error": "parent category not found"}
        ui["categories"].append({"name": name, "appids": [], "hidden": False,
                                 "color": (color or ""), "parent": par, "collapsed": False})
        _save_ui()
        return {"ok": True, "name": name, "parent": par}
    if op == "rename":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": False, "error": "category not found"}
        nn = (new_name or "").strip()[:40]
        if not nn:
            return {"ok": False, "error": "new name required"}
        if _cat_by_name(ui, nn) and nn.lower() != c["name"].lower():
            return {"ok": False, "error": "name already used"}
        old = c["name"]
        c["name"] = nn
        for ch in ui["categories"]:                # keep children pointing at the new name
            if ch.get("parent", "").lower() == old.lower():
                ch["parent"] = nn
        _save_ui()
        return {"ok": True, "name": nn}
    if op == "delete":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": True}
        gone = c["name"].lower()
        grandparent = c.get("parent", "")
        for ch in ui["categories"]:                # orphan children up to the grandparent
            if ch.get("parent", "").lower() == gone:
                ch["parent"] = grandparent
        ui["categories"] = [x for x in ui["categories"] if x["name"].lower() != gone]
        _save_ui()
        return {"ok": True}
    if op == "set_parent":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": False, "error": "category not found"}
        par = (parent or "").strip()
        if par:
            p = _cat_by_name(ui, par)
            if not p:
                return {"ok": False, "error": "parent not found"}
            if par.lower() == c["name"].lower():
                return {"ok": False, "error": "can't nest a folder into itself"}
            if _is_descendant(ui, par, c["name"]):   # would create a cycle
                return {"ok": False, "error": "can't nest a folder into its own child"}
        c["parent"] = par
        _save_ui()
        return {"ok": True, "name": c["name"], "parent": par}
    if op == "set_color":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": False, "error": "category not found"}
        col = (color or "").strip()
        if col and not (col.startswith("#") and len(col) in (4, 7)):
            return {"ok": False, "error": "bad color"}
        c["color"] = col
        _save_ui()
        return {"ok": True, "name": c["name"], "color": col}
    if op == "assign":
        a = str(appid)
        if not a:
            return {"ok": False, "error": "bad uid"}
        for c in ui["categories"]:                 # remove from all first
            if a in c["appids"]:
                c["appids"].remove(a)
        if category:                                # empty/None = just uncategorize
            c = _cat_by_name(ui, category)
            if not c:
                return {"ok": False, "error": "category not found"}
            if a not in c["appids"]:
                idx = index
                if isinstance(idx, int) and 0 <= idx <= len(c["appids"]):
                    c["appids"].insert(idx, a)      # drop at a specific position (reorder)
                else:
                    c["appids"].append(a)
        _save_ui()
        return {"ok": True, "appid": a, "category": category or None}
    if op == "reorder_uncat":                       # arrange a game among the uncategorized
        a = str(appid)
        if not a:
            return {"ok": False, "error": "bad uid"}
        order_l = [x for x in ui.get("order", []) if x != a]
        try:
            idx = int(index)
        except Exception:
            idx = len(order_l)
        idx = max(0, min(idx, len(order_l)))
        order_l.insert(idx, a)
        ui["order"] = order_l
        _save_ui()
        return {"ok": True, "appid": a, "index": idx}
    if op == "set_order":                           # store the full uncategorized order
        if isinstance(order, list):
            ui["order"] = [str(x) for x in order if str(x)]
            _save_ui()
            return {"ok": True, "count": len(ui["order"])}
        return {"ok": False, "error": "order list required"}
    if op == "reorder_category":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": False, "error": "category not found"}
        ui["categories"].remove(c)
        target = (category or "").strip()
        if target and _cat_by_name(ui, target):
            ti = ui["categories"].index(_cat_by_name(ui, target))
            ui["categories"].insert(ti, c)          # drop before the target sibling
        else:
            try:
                idx = int(index)
            except Exception:
                idx = len(ui["categories"])
            ui["categories"].insert(max(0, min(idx, len(ui["categories"]))), c)
        _save_ui()
        return {"ok": True, "name": c["name"]}
    if op == "folder_hidden":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": False, "error": "category not found"}
        c["hidden"] = bool(hidden)
        _save_ui()
        return {"ok": True, "name": c["name"], "hidden": c["hidden"]}
    if op == "set_collapsed":
        c = _cat_by_name(ui, name)
        if not c:
            return {"ok": False, "error": "category not found"}
        c["collapsed"] = bool(collapsed)
        _save_ui()
        return {"ok": True, "name": c["name"], "collapsed": c["collapsed"]}
    return {"ok": False, "error": f"unknown op '{op}'"}


def _is_descendant(ui, name, ancestor):
    """True if category `name` is a descendant of `ancestor` (walks parent links)."""
    seen = set()
    cur = _cat_by_name(ui, name)
    while cur and cur.get("parent"):
        p = cur["parent"].lower()
        if p in seen:
            break
        seen.add(p)
        if p == ancestor.lower():
            return True
        cur = _cat_by_name(ui, cur["parent"])
    return False


def detect_stores(games=None):
    """A store chip appears only if at least one game from it was actually found."""
    if games is None:
        games = scan_all()
    present = {g["platform"] for g in games}
    out = []
    for p in _PLAT_ORDER:
        if p in present:
            meta = _STORE_META.get(p, {"label": p.title(), "shop": ""})
            out.append({"platform": p, "label": meta["label"], "shop_url": meta["shop"]})
    return out


def _list_games(show_hidden=False):
    games = scan_all()
    cache = _load_genres()
    ui = _load_ui()
    hidden = set(ui["hidden"])
    need = []
    by_appid = {}                                  # keyed by uid string (name kept for callers)
    for g in games:
        uid = g["uid"]; plat = g["platform"]
        if plat == "steam":
            gen = cache.get(str(g["appid"]))
            primary = (gen[0] if gen else "Uncategorized")
            if gen is None:
                need.append(g["appid"])
        else:
            primary = _STORE_META.get(plat, {}).get("label", plat.title())
        by_appid[uid] = {"uid": uid, "appid": uid, "name": g["name"], "platform": plat,
                         "genre": primary, "art": _art_url(g), "hidden": uid in hidden,
                         "custom": has_custom_icon(uid)}
    if need:
        threading.Thread(target=_resolve_genres_bg, args=(need,), daemon=True).start()

    def _visible(e):
        return show_hidden or not e["hidden"]

    # ── Custom categories as a tree (parent → children), saved order preserved ──
    in_cat = set()
    node_games = {}
    for c in ui["categories"]:
        cg = []
        for a in c["appids"]:
            e = by_appid.get(a)
            if e:
                in_cat.add(a)                     # claimed → excluded from genre groups
                if _visible(e):
                    cg.append(e)
        node_games[c["name"].lower()] = cg

    children_of = {}
    for c in ui["categories"]:
        children_of.setdefault(c.get("parent", "").lower(), []).append(c)
    name_set = {c["name"].lower() for c in ui["categories"]}

    def _build(c, seen):
        nm = c["name"].lower()
        if nm in seen:
            return None                           # cycle guard
        seen = seen | {nm}
        cg = node_games.get(nm, [])
        kids = []
        for ch in children_of.get(nm, []):
            node = _build(ch, seen)
            if node is not None:
                kids.append(node)
        has_content = (len(c["appids"]) > 0) or (len(children_of.get(nm, [])) > 0)
        visible_content = bool(cg) or bool(kids)
        auto_hidden = has_content and not visible_content
        is_hidden = bool(c.get("hidden")) or auto_hidden
        if is_hidden and not show_hidden:
            return None
        return {"name": c["name"], "games": cg, "children": kids,
                "hidden": bool(c.get("hidden")), "empty_hidden": auto_hidden,
                "color": c.get("color", ""), "parent": c.get("parent", ""),
                "collapsed": bool(c.get("collapsed"))}

    custom = []
    for c in ui["categories"]:
        par = c.get("parent", "")
        if not par or par.lower() not in name_set:   # root (orphans treated as roots)
            node = _build(c, set())
            if node is not None:
                custom.append(node)

    # ── Remaining games grouped by genre, sorted by the operator's manual order ──
    order_index = {a: i for i, a in enumerate(ui.get("order", []))}
    grouped = {}
    for a, e in by_appid.items():
        if a in in_cat or not _visible(e):
            continue
        grouped.setdefault(e["genre"], []).append(e)
    order = sorted([k for k in grouped if k != "Uncategorized"])
    if "Uncategorized" in grouped:
        order.append("Uncategorized")

    def _sk(e):
        return (order_index.get(e["appid"], 10 ** 9), e["name"].lower())
    groups = [{"genre": k, "games": sorted(grouped[k], key=_sk)} for k in order]

    return {
        "steam_found": bool(games),
        "stores": detect_stores(games),
        "count": len(games),
        "custom": custom,
        "groups": groups,
        "categories": [c["name"] for c in ui["categories"]],
        "hidden_count": len([a for a in hidden if a in by_appid]),
        "show_hidden": show_hidden,
        "resolving": len(need),
        "user_playing": state.get("user_playing"),
        "s32_playing": state.get("s32_playing"),
    }


# ── Resource swap (close GPU/CPU hogs, restore after) ────────────────────────
def _kill_by_port(port):
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           timeout=10, creationflags=_NOWIN)
        pids = set()
        for line in r.stdout.splitlines():
            if f":{port} " in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids.add(parts[-1])
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True,
                           timeout=10, creationflags=_NOWIN)
    except Exception:
        pass


def _stop_comfy():
    try:
        subprocess.run(["taskkill", "/F", "/FI", "WINDOWTITLE eq ComfyUI*"],
                       capture_output=True, timeout=10, creationflags=_NOWIN)
    except Exception:
        pass
    _kill_by_port(CONFIG.get("comfy_image_port", 8188))
    state["comfy_image_ready"] = False
    log("[S32][STEAM] ComfyUI stopped (freeing VRAM)")


def _stop_model():
    try:
        subprocess.run(["taskkill", "/F", "/IM", "llama-server.exe"],
                       capture_output=True, timeout=10, creationflags=_NOWIN)
    except Exception as e:
        log(f"[S32][STEAM] model stop note: {e}")
    _kill_by_port(1234)
    state["lmstudio_ready"] = False
    log("[S32][STEAM] model stopped (freeing GPU)")


def _restart_brain():
    log("[S32][STEAM] booting back up — model first…")
    try:
        from s32_hardware import start_llama_server
        start_llama_server()
    except Exception as e:
        log(f"[S32][STEAM] model restart failed: {e}")
        return
    waited = 0
    while not state.get("lmstudio_ready") and waited < 300:
        time.sleep(3); waited += 3
    if state.get("model_load_failed"):
        log("[S32][STEAM] model failed to reload — skipping ComfyUI")
        return
    try:
        from s32_comfyui import start_comfy_image
        log("[S32][STEAM] model ready — starting ComfyUI…")
        start_comfy_image()
    except Exception as e:
        log(f"[S32][STEAM] ComfyUI restart failed: {e}")


def _game_running(installdir):
    if not installdir:
        return False
    try:
        import psutil
        root = os.path.normcase(os.path.abspath(installdir))
        for p in psutil.process_iter(["exe"]):
            try:
                exe = p.info.get("exe") or ""
                if exe and os.path.normcase(os.path.abspath(exe)).startswith(root):
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _watch_and_restore(appid, installdir):
    # wait for the game to actually start (up to 120s)
    waited, appeared = 0, False
    while waited < 120 and not appeared:
        time.sleep(3); waited += 3
        if _game_running(installdir):
            appeared = True
    if not appeared:
        log(f"[S32][STEAM] game {appid} never detected (psutil/installdir?) — restoring brain")
        state["user_playing"] = None
        _restart_brain(); return
    log(f"[S32][STEAM] game {appid} running — brain stays down until it exits")
    while _game_running(installdir):
        time.sleep(5)
    log(f"[S32][STEAM] game {appid} closed — restoring")
    state["user_playing"] = None
    _restart_brain()


def _steam_run(appid):
    try:
        os.startfile(f"steam://rungameid/{appid}")   # Windows-native, robust
        return True
    except Exception:
        steam = find_steam()
        try:
            subprocess.Popen([os.path.join(steam or "", "steam.exe"),
                              "-applaunch", str(appid)], creationflags=_NOWIN)
            return True
        except Exception as e:
            log(f"[S32][STEAM] launch failed for {appid}: {e}")
            return False


def _run_game(g):
    """Launch a unified game record by its platform's mechanism."""
    lk = g.get("launch", {})
    kind = lk.get("kind")
    try:
        if kind == "steam":
            return _steam_run(lk["appid"])
        if kind == "uri":
            os.startfile(lk["uri"])
            return True
        if kind == "exe":
            exe = lk.get("exe")
            if exe and os.path.exists(exe):
                os.startfile(exe)
                return True
            log(f"[S32][LIB] exe missing for {g.get('uid')}: {exe}")
            return False
    except Exception as e:
        log(f"[S32][LIB] launch failed for {g.get('uid')}: {e}")
    return False


def launch_game(appid, who="user"):
    uid = str(appid)
    g = next((x for x in scan_all() if x["uid"] == uid), None)
    name = g["name"] if g else uid
    installdir = g.get("installdir", "") if g else ""
    if not g:
        return {"ok": False, "error": "game not found"}

    if who == "user":
        state["user_playing"] = uid
        _stop_comfy()
        time.sleep(1.0)
        _stop_model()
        if not _run_game(g):
            state["user_playing"] = None
            _restart_brain()
            return {"ok": False, "error": f"Couldn't launch {name}."}
        threading.Thread(target=_watch_and_restore, args=(uid, installdir),
                         daemon=True).start()
        return {"ok": True, "mode": "user", "name": name,
                "note": "ComfyUI and the model are down to free the GPU/CPU. "
                        "System 32 boots back up when you close the game."}

    # who == "s32": keep its brain, free VRAM by closing ComfyUI
    state["s32_playing"] = uid
    state["s32_playing_installdir"] = installdir
    try:
        import s32_gameinput
        s32_gameinput.arm()          # clear any prior halt, ready its hands
    except Exception:
        pass
    try:
        import s32_gameplay
        s32_gameplay.start_session(uid, name)   # begin recording (state→action)
    except Exception:
        pass
    _stop_comfy()
    if not _run_game(g):
        state["s32_playing"] = None
        state["s32_playing_installdir"] = ""
        return {"ok": False, "error": f"Couldn't launch {name}."}
    threading.Thread(target=_watch_s32_then_idle, args=(uid, installdir),
                     daemon=True).start()
    return {"ok": True, "mode": "s32", "name": name,
            "note": "The model stays up so it can play. It can use game_input to "
                    "press keys / move the mouse — but only while the game is focused. "
                    "Your Stop button is the kill switch."}


def _watch_s32_then_idle(appid, installdir):
    """When IT plays: wait (patiently — Steam launch popups can be slow) for the
    game to appear, then idle the session only once it actually EXITS. If it never
    gets detected, leave its session active rather than yanking its hands."""
    waited, appeared = 0, False
    while waited < 300 and not appeared:          # up to 5 min for the game to come up
        time.sleep(3); waited += 3
        if _game_running(installdir):
            appeared = True
    if not appeared:
        log(f"[S32][STEAM] couldn't detect game {appid}'s process under its install dir — "
            f"leaving the play session active (cursor-gated). End it with Stop, or reopen "
            f"Steam Games → restore.")
        return                                    # do NOT halt/clear — keep its hands
    log(f"[S32][STEAM] game {appid} detected — session active until it exits")
    while _game_running(installdir):
        time.sleep(5)
    try:
        import s32_gameinput
        s32_gameinput.halt()
    except Exception:
        pass
    try:
        import s32_gameplay
        s32_gameplay.end_session()
    except Exception:
        pass
    state["s32_playing"] = None
    state["s32_playing_installdir"] = ""
    log(f"[S32][STEAM] game {appid} ended — hands down; ComfyUI will revive")


def _comfy_up():
    if state.get("comfy_image_ready"):
        return True
    try:
        import socket
        host = str(CONFIG.get("comfy_image_host", "127.0.0.1:8188"))
        h, _, p = host.replace("http://", "").replace("https://", "").partition(":")
        with socket.create_connection((h or "127.0.0.1", int(p or 8188)), timeout=1):
            return True
    except Exception:
        return False


def _revive_loop():
    """Quietly bring ComfyUI back whenever no game is running but the model IS up
    and ComfyUI is down (e.g. after it finishes a game). Only acts AFTER ComfyUI
    has been up at least once, so it never races the initial boot startup."""
    was_ready = False
    down = 0
    while True:
        time.sleep(20)
        try:
            if _comfy_up():
                was_ready = True
            playing = state.get("s32_playing") or state.get("user_playing")
            model_up = state.get("lmstudio_ready") and not state.get("model_load_failed")
            if was_ready and (not playing) and model_up and (not _comfy_up()):
                down += 1
                if down >= 2:                       # ~40s down before reviving
                    log("[S32][STEAM] no game + model up + ComfyUI down — reviving ComfyUI")
                    try:
                        from s32_comfyui import start_comfy_image
                        start_comfy_image()
                    except Exception as e:
                        log(f"[S32][STEAM] ComfyUI revive failed: {e}")
                    down = 0
            else:
                down = 0
        except Exception:
            pass


def start_revive_monitor():
    threading.Thread(target=_revive_loop, daemon=True, name="comfy-revive").start()
    log("[S32][STEAM] ComfyUI revive monitor started")


def restore_brain_now():
    """Manual override: boot it back up regardless of game state."""
    try:
        import s32_gameinput
        s32_gameinput.halt()          # let go of everything before tearing down
    except Exception:
        pass
    state["user_playing"] = None
    state["s32_playing"] = None
    state["s32_playing_installdir"] = ""
    threading.Thread(target=_restart_brain, daemon=True).start()
    return {"ok": True}
