# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_findex.py — Find anything on this computer (2026-10-29).

A searchable index of every drive — internal, spare, and external/USB — built quietly in
the background and kept up to date, so "find my red fox pictures", "where's that Skyrim VR
controller mod", "show me my gameplay videos" answer in a moment instead of walking folders
for minutes.

READ-ONLY: it only lists folder contents and file names/sizes/dates. It never opens a file
for writing, moves, renames or deletes anything.

PRIVATE: the index is a map of this person's files, so it lives in their own folder
(reference\\people\\<computer>\\file_index.db) — another person on another computer can't
search it, and a factory reset wipes it. System 32's private folders (memories, other
people's folders, save states) are never indexed.

What's indexed: every folder, plus pictures, videos, audio, code/text, documents, archives,
3D models, game plugins and programs. Skipped: Windows' own system folders, caches/temp,
recycle bins, and package/library folders (node_modules, site-packages, .git, …) that would
bury real results under thousands of library files.
"""

import os
import re
import sys
import time
import json
import sqlite3
import threading
from collections import OrderedDict

from s32_config import CONFIG, PERSON_DIR, REFERENCE_DIR, _script_dir, state, log

DB_PATH = os.path.join(PERSON_DIR, "file_index.db")
_RESCAN_HOURS = 12            # a drive is rescanned when its index is older than this
_WATCH_SECS = 60              # how often to look for newly plugged-in drives
_START_DELAY = 45             # let the model + ComfyUI start first

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"}
CODE_EXTS = {".py", ".pyw", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".c", ".cc", ".cpp", ".cxx", ".h",
             ".hpp", ".hxx", ".cs", ".java", ".kt", ".go", ".rs", ".lua", ".rb", ".php", ".swift",
             ".psc", ".pas", ".vb", ".bat", ".cmd", ".ps1", ".sh", ".ini", ".cfg", ".conf", ".toml",
             ".yaml", ".yml", ".json", ".xml", ".html", ".htm", ".css", ".scss", ".glsl", ".hlsl",
             ".fx", ".shader", ".usf", ".ush", ".cmake", ".sln", ".vcxproj", ".csproj", ".gradle",
             ".sql", ".r", ".m", ".ipynb", ".uproject", ".uplugin", ".lsl", ".ahk", ".nut", ".gd"}
DOC_EXTS = {".txt", ".md", ".rst", ".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx",
            ".csv", ".ppt", ".pptx", ".epub"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}
MODEL_EXTS = {".glb", ".gltf", ".obj", ".fbx", ".stl", ".ply", ".blend", ".uasset", ".safetensors",
              ".ckpt", ".gguf", ".nif"}
GAME_EXTS = {".esp", ".esm", ".esl", ".bsa", ".ba2", ".pex", ".dll", ".exe", ".pak", ".vpk"}
_KIND_OF = {}
for _k, _es in (("image", IMAGE_EXTS), ("video", VIDEO_EXTS), ("audio", AUDIO_EXTS),
                ("code", CODE_EXTS), ("document", DOC_EXTS), ("archive", ARCHIVE_EXTS),
                ("model", MODEL_EXTS), ("program", GAME_EXTS)):
    for _e in _es:
        _KIND_OF[_e] = _k

# Folder names skipped anywhere (lower-case).
_SKIP_NAMES = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws",
               "$winreagent", "windowsapps", "winsxs", "node_modules", "site-packages",
               "dist-packages", "__pycache__", ".git", ".svn", ".hg", ".vs", ".idea", ".cache",
               ".venv", "venv", "msocache", "recovery", "perflogs", "config.msi", "$sysreset",
               "temporary internet files", "inetcache", "crashdumps", "shadercache",
               "gpucache", "code cache", "cache_data", "dxcache", "glcache"}
# Path pieces skipped (lower-case, matched inside the full path).
_SKIP_PARTS = ("\\windows\\", "\\appdata\\local\\temp\\", "\\appdata\\local\\microsoft\\",
               "\\appdata\\local\\packages\\", "\\appdata\\locallow\\microsoft\\",
               "\\programdata\\microsoft\\", "\\program files\\windowsapps\\",
               "\\program files\\common files\\", "\\program files (x86)\\common files\\",
               "\\nvidia corporation\\", "\\steamapps\\shadercache\\", "\\steamapps\\downloading\\",
               "\\steamapps\\temp\\")
# (\windows\ is only skipped at a drive root — see _skip_dir.)

_STOP = set("""a an the of in on at to for from with and or my me our your this that these those
any all some find show look search get pull grab open locate where is are was be on computer pc
drive drives disk folder folders file files image images picture pictures photo photos pic pics
video videos clip clips movie movies footage code script scripts source project projects mod mods
please can you could would i it""".split())

_INDEX_VERSION = 2          # bump when _words changes → existing indexes rebuild once
_lock = threading.RLock()
_status = {"state": "off", "drives": {}, "files": 0, "detail": ""}
_fts = {"ok": None}


# ── storage ──────────────────────────────────────────────────────────────────
def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def _init(c):
    c.execute("""CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, dir TEXT, ext TEXT, kind TEXT,
        size INTEGER, mtime REAL, vol TEXT, gen INTEGER, words TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS files_vol ON files(vol, gen)")
    c.execute("CREATE INDEX IF NOT EXISTS files_kind ON files(kind)")
    c.execute("""CREATE TABLE IF NOT EXISTS apps (
        appid TEXT PRIMARY KEY, name TEXT, folder TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS vols (
        vol TEXT PRIMARY KEY, root TEXT, label TEXT, last_scan REAL, files INTEGER, gen INTEGER)""")
    c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = c.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not row or int(row[0]) < _INDEX_VERSION:
        c.execute("UPDATE vols SET last_scan=0")          # words changed → rescan (background)
        c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('version',?)", (str(_INDEX_VERSION),))
    if _fts["ok"] is None:
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(words, content='files', "
                      "content_rowid='id', tokenize='unicode61')")
            _fts["ok"] = True
        except sqlite3.OperationalError:
            _fts["ok"] = False                   # no FTS5 in this sqlite — LIKE search instead
            log("[S32][FIND] this Python's sqlite has no full-text search — using slower matching")
    c.commit()


def _words(name, folder):
    """Searchable words for a file: its name split on separators AND camelCase/digits
    ("RedFox_Snow01.jpg" → red fox snow 01), plus its folder names."""
    def split(s):
        s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
        # letters|digits boundaries both ways — "fallout4vr" → fallout 4 vr (the old one-pass
        # rule gave "fallout 4vr", so a Vortex 'fallout4vr' folder wasn't Fallout 4 VR)
        parts = re.findall(r"[a-z0-9]+", re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", s).lower())
        # keep the whole word too ("f4se", "fallout4vr") so typing it as one word still matches
        whole = [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in parts]
        return parts + whole
    stem = os.path.splitext(name)[0]
    parts = split(stem)
    dirs = []
    for seg in re.split(r"[\\/]", folder or "")[-6:]:      # nearest 6 folder names
        dirs += split(seg)
    return " ".join(parts) + " | " + " ".join(dirs)


# ── drives ───────────────────────────────────────────────────────────────────
def _drives():
    """[(root, vol_id, label, type)] for fixed + removable drives. Off Windows (tests), the
    config list "index_roots" is used."""
    roots = CONFIG.get("index_roots")
    if roots:
        return [(r, "root:" + os.path.normcase(os.path.abspath(r)), os.path.basename(r.rstrip("\\/")), "fixed")
                for r in roots if os.path.isdir(r)]
    out = []
    if os.name != "nt":
        return out
    import ctypes
    import string
    k = ctypes.windll.kernel32
    mask = k.GetLogicalDrives()
    for i, letter in enumerate(string.ascii_uppercase):
        if not mask & (1 << i):
            continue
        root = f"{letter}:\\"
        t = k.GetDriveTypeW(root)
        if t not in (2, 3):                     # 2 removable (USB sticks, SD), 3 fixed (incl. USB drives)
            continue
        vol = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_ulong(0)
        ok = k.GetVolumeInformationW(root, vol, 261, ctypes.byref(serial), None, None, None, 0)
        if not ok:
            continue                            # empty card reader etc.
        out.append((root, f"{serial.value:08X}", vol.value or letter, "removable" if t == 2 else "fixed"))
    return out


def _private_dirs():
    """Folders never indexed: System 32's private data (all people), the bundled runtimes."""
    out = []
    for p in (REFERENCE_DIR, os.path.join(_script_dir, "python"),
              os.path.join(_script_dir, "searxng"), os.path.join(_script_dir, "patches"),
              os.path.join(_script_dir, "workspace"), os.path.join(_script_dir, ".attachments")):
        if p:
            out.append(os.path.normcase(os.path.abspath(p)).rstrip("\\/") + os.sep)
    out_base = os.path.dirname(CONFIG.get("out_images", "") or "")
    if out_base:
        for sub in ("save states", "training", "gameplay_dataset"):
            out.append(os.path.normcase(os.path.join(out_base, sub)) + os.sep)
    return out


def _skip_dir(path, name, root, private):
    low = name.lower()
    if low in _SKIP_NAMES or (low.startswith("$") and len(path) < 8):
        return True
    norm = os.path.normcase(path) + os.sep
    if any(norm.startswith(p) for p in private):
        return True
    lp = (path.lower() + "\\").replace("/", "\\")
    if lp.startswith(root.lower().replace("/", "\\").rstrip("\\") + "\\windows\\"):
        return True                             # Windows' own folder, at a drive's root only
    return any(part in lp for part in _SKIP_PARTS if part != "\\windows\\")


# ── scanning ─────────────────────────────────────────────────────────────────
def _scan_drive(root, vol, label):
    t0 = time.time()
    private = _private_dirs()
    c = _connect()
    try:
        _init(c)
        row = c.execute("SELECT gen FROM vols WHERE vol=?", (vol,)).fetchone()
        gen = (row[0] if row else 0) + 1
        batch, n = [], 0
        stack = [root]

        def flush():
            if not batch:
                return
            with _lock:
                c.executemany("""INSERT INTO files(path,name,dir,ext,kind,size,mtime,vol,gen,words)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)
                                 ON CONFLICT(path) DO UPDATE SET name=excluded.name, dir=excluded.dir,
                                 ext=excluded.ext, kind=excluded.kind, size=excluded.size,
                                 mtime=excluded.mtime, vol=excluded.vol, gen=excluded.gen,
                                 words=excluded.words""", batch)
                c.commit()
            batch.clear()

        while stack:
            if state.get("resetting"):
                return
            d = stack.pop()
            try:
                it = os.scandir(d)
            except (PermissionError, FileNotFoundError, OSError):
                continue
            with it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if _skip_dir(e.path, e.name, root, private):
                                continue
                            stack.append(e.path)
                            st = e.stat(follow_symlinks=False)
                            batch.append((e.path, e.name, d, "", "folder", 0, st.st_mtime, vol, gen,
                                          _words(e.name, d)))
                        else:
                            ext = os.path.splitext(e.name)[1].lower()
                            kind = _KIND_OF.get(ext)
                            if not kind:
                                continue
                            st = e.stat(follow_symlinks=False)
                            batch.append((e.path, e.name, d, ext, kind, st.st_size, st.st_mtime, vol, gen,
                                          _words(e.name, d)))
                        n += 1
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
                    if len(batch) >= 4000:
                        flush()
                        _status["detail"] = f"indexing {label}: {n:,} items"
                        time.sleep(0.01)                # stay out of the way of everything else
        flush()
        with _lock:
            c.execute("DELETE FROM files WHERE vol=? AND gen<?", (vol, gen))
            c.execute("INSERT OR REPLACE INTO vols(vol,root,label,last_scan,files,gen) VALUES(?,?,?,?,?,?)",
                      (vol, root, label, time.time(), n, gen))
            if _fts["ok"]:
                c.execute("INSERT INTO fts(fts) VALUES('rebuild')")
            c.commit()
        _status["drives"][root] = {"label": label, "files": n, "scanned": time.strftime("%H:%M")}
        log(f"[S32][FIND] indexed {label} ({root}): {n:,} items in {time.time() - t0:.0f}s")
    except Exception as e:
        log(f"[S32][FIND] indexing {root} stopped: {e}")
    finally:
        c.close()


def _refresh_apps():
    """Steam keeps game names in small manifests (steamapps\appmanifest_611660.acf →
    "Fallout 4 VR"), while settings and bindings are filed by NUMBER
    ("steam.app.611660_vive_cosmos_controller.json"). Read the manifests (read-only) so a
    search for 'fallout 4 vr' also matches 611660. 2026-10-29."""
    try:
        c = _connect()
        try:
            _init(c)
            dirs = [r[0] for r in c.execute("SELECT path FROM files WHERE kind='folder' AND name LIKE 'steamapps'")]
            rows = []
            for d in dirs:
                try:
                    names = [n for n in os.listdir(d) if n.lower().startswith("appmanifest_") and n.lower().endswith(".acf")]
                except OSError:
                    continue
                for n in names:
                    try:
                        with open(os.path.join(d, n), encoding="utf-8", errors="replace") as f:
                            txt = f.read(20000)
                    except OSError:
                        continue
                    a = re.search(r'"appid"\s+"(\d+)"', txt)
                    nm = re.search(r'"name"\s+"([^"]+)"', txt)
                    inst = re.search(r'"installdir"\s+"([^"]+)"', txt)
                    if a and nm:
                        rows.append((a.group(1), nm.group(1), os.path.join(d, "common", inst.group(1)) if inst else ""))
            if rows:
                with _lock:
                    c.executemany("INSERT OR REPLACE INTO apps(appid,name,folder) VALUES(?,?,?)", rows)
                    c.commit()
            return len(rows)
        finally:
            c.close()
    except Exception as e:
        log(f"[S32][FIND] reading Steam game names: {e}")
        return 0


def _due(vol):
    try:
        c = _connect()
        try:
            _init(c)
            row = c.execute("SELECT last_scan FROM vols WHERE vol=?", (vol,)).fetchone()
        finally:
            c.close()
        return not row or (time.time() - (row[0] or 0)) > _RESCAN_HOURS * 3600
    except Exception:
        return True


def _loop():
    time.sleep(_START_DELAY)
    _status["state"] = "indexing"
    while not state.get("resetting"):
        try:
            for root, vol, label, _t in _drives():
                if state.get("resetting"):
                    return
                if _due(vol):
                    _status["detail"] = f"indexing {label}"
                    _scan_drive(root, vol, label)
            _status.update(state="ready", detail="")
            _status["files"] = count()
            n_apps = _refresh_apps()
            if n_apps and not _status.get("apps"):
                log(f"[S32][FIND] {n_apps} Steam game name(s) known (matches files filed by app number)")
            _status["apps"] = n_apps
        except Exception as e:
            log(f"[S32][FIND] indexer: {e}")
        time.sleep(_WATCH_SECS)


def start():
    if CONFIG.get("file_index") is False:
        _status.update(state="off", detail="turned off in config.json")
        return
    threading.Thread(target=_loop, daemon=True, name="s32-file-index").start()
    log("[S32][FIND] file index will build in the background (all drives, read-only)")


def count():
    try:
        c = _connect()
        try:
            _init(c)
            return c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        finally:
            c.close()
    except Exception:
        return 0


def status():
    return dict(_status, files=_status.get("files") or count())


# ── searching ────────────────────────────────────────────────────────────────
def _terms(query):
    ws = []
    for w in re.findall(r"[a-z0-9]+", (query or "").lower()):
        # "fallout4vr" → fallout 4 vr (like the index does), unless that leaves 1-letter
        # scraps — "f4se" stays one word.
        parts = re.findall(r"[a-z]+|\d+", w)
        ws += parts if len(parts) > 1 and all(len(x) > 1 or x.isdigit() for x in parts) else [w]
    return [w for w in dict.fromkeys(ws) if w not in _STOP and (len(w) > 1 or w.isdigit())]


def _term_in(t, words):
    """Word t is in `words`: exact, plural/singular, or a close form of a longer word
    (controller ~ controllers, remaster ~ remastered). Not loose prefixes: 'controller' no
    longer matches 'controls', so AMD's UI icons stop answering a controller search."""
    if t in words or (t + "s") in words or (t + "es") in words:
        return True
    if (t.endswith("es") and t[:-2] in words) or (t.endswith("s") and t[:-1] in words):
        return True
    if t.isdigit() or len(t) < 5:
        return False
    for w in words:
        if w.startswith(t) and len(w) - len(t) <= 3:
            return True
        if t.startswith(w) and len(w) >= 5 and len(t) - len(w) <= 3:
            return True
    return False


_KIND_ALIASES = {"images": "image", "picture": "image", "pictures": "image", "photo": "image",
                 "photos": "image", "videos": "video", "clip": "video", "movie": "video",
                 "scripts": "code", "source": "code", "folders": "folder", "directory": "folder",
                 "documents": "document", "doc": "document", "docs": "document", "file": "any",
                 "files": "any", "all": "any", "": "any", "music": "audio", "sound": "audio",
                 "3d": "model", "models": "model", "programs": "program", "app": "program",
                 "mod": "any", "mods": "any", "project": "any",
                 "settings": "config", "setting": "config", "ini": "config", "toml": "config",
                 "cfg": "config", "configuration": "config", "configs": "config"}
# Settings files (they're indexed as 'code'); kind='config' narrows to these.
CONFIG_EXTS = (".ini", ".toml", ".cfg", ".conf", ".json", ".yaml", ".yml", ".xml", ".properties")


# Words that make a game EDITION, not a different game: "fallout 4" covers Fallout 4 and
# Fallout 4 VR (2026-10-29: he said "fallout 4", the binding is filed under Fallout 4 VR's
# number 611660, and the old rule wanted every word incl. 'vr').
_EDITION_WORDS = {"vr", "edition", "remastered", "remaster", "special", "anniversary", "goty", "game",
                  "year", "definitive", "enhanced", "complete", "ultimate", "deluxe", "legendary",
                  "standard", "collection", "hd", "dx", "redux", "classic", "the"}


def _groups(c, terms):
    """Query words as match groups. A Steam game named in the query becomes ONE group that
    also accepts its app number(s): "fallout 4" → {fallout, 4} or 377160 or 611660;
    "fallout 4 vr" → {fallout, 4, vr} or 611660 only."""
    try:
        apps = c.execute("SELECT appid, name FROM apps").fetchall()
    except Exception:
        apps = []
    tset = set(terms)
    found = []                                   # (name, appid, core words, edition words named)
    for appid, name in apps:
        # The full name, and — for "Series: Title" names — the title alone, which is what
        # people say ("The Elder Scrolls V: Skyrim VR" → "skyrim vr"; "Fallout: New Vegas").
        for nm in dict.fromkeys([name] + ([name.split(":", 1)[1]] if ":" in name else [])):
            nt = _terms(nm)
            if not nt:
                continue
            core = [w for w in nt if w not in _EDITION_WORDS] or nt
            if set(core) <= tset:
                found.append((name, str(appid), core, [w for w in nt if w not in core and w in tset]))
                break
    # If the request names an edition ("vr"), only games of that edition count.
    named = [f for f in found if f[3]]
    if named:
        found = named
    groups, used, games = [], set(), []
    for name, appid, core, edw in sorted(found, key=lambda f: -len(f[2])):
        words = list(dict.fromkeys(core + edw))
        g = next((g for g in groups if set(core) <= set(g["words"])), None)
        if g is None:
            if set(core) & used:
                continue                         # overlaps a longer game name — skip
            g = {"words": words, "alts": []}
            groups.append(g)
            used |= set(words)
        g["alts"].append(appid)
        games.append((name, appid))
    for t in terms:
        if t not in used:
            groups.append({"words": [t], "alts": []})
    for g in groups:
        g["alt"] = g["alts"][0] if g["alts"] else None
    return groups, games


def _fts_expr(g):
    ws = " AND ".join((f'"{w}"' if (w.isdigit() or len(w) < 3) else f'"{w}"*') for w in g["words"])
    alts = " OR ".join(f'"{a}"' for a in g.get("alts") or [])
    return f"(({ws}) OR {alts})" if alts else f"({ws})"


def _path_weight(path):
    """Push programs' own install files down and the person's things up: a search for a mod
    was buried under VIVE Hub / SteamVR driver files and Unreal Engine content (2026-10-29)."""
    lp = path.lower().replace("/", "\\")
    w = 0.0
    if ("\\program files\\" in lp or "\\program files (x86)\\" in lp) and "\\steamapps\\" not in lp:
        w -= 2.0
    if any(x in lp for x in ("\\temp\\", "\\updater\\", "\\cache\\", "\\logs\\")):
        w -= 1.5
    if "\\epic games\\" in lp and "\\engine\\" in lp:
        w -= 1.0
    if any(x in lp for x in ("\\users\\", "\\mods\\", "vortex", "mod organizer", "\\documents\\",
                             "\\desktop\\", "\\downloads\\")):
        w += 1.0
    return w


def search(query, kind="any", limit=500, root=None, info=None):
    """→ [{path, name, dir, kind, size, mtime}] best first. Missing files (drive unplugged,
    deleted since the last scan) are left out. `info` (dict) gets {"games": [...]} back."""
    kind = _KIND_ALIASES.get(str(kind or "any").lower(), str(kind or "any").lower())
    terms = _terms(query)
    if not terms:
        return []
    c = _connect()
    try:
        _init(c)
        groups, games = _groups(c, terms)
        if info is not None:
            info["games"] = games
        where, args = [], []
        if kind == "code":
            where.append("f.kind IN ('code','folder')")
        elif kind == "config":
            where.append("(" + " OR ".join("lower(f.name) LIKE ?" for _ in CONFIG_EXTS) + ")")
            args.extend("%" + e for e in CONFIG_EXTS)
        elif kind != "any":
            where.append("f.kind=?")
            args.append(kind)
        if root:
            where.append("f.path LIKE ?")
            args.append(os.path.join(root, "") + "%")
        cond = (" AND " + " AND ".join(where)) if where else ""
        rows = {}
        with _lock:
            if _fts["ok"]:
                # Ranked INSIDE the database (bm25): the old "any word, first 20,000" cut the
                # best matches off when a word like '3' matched thousands of files.
                exprs = [_fts_expr(g) for g in groups]
                for m in (" AND ".join(exprs), " OR ".join(exprs)):
                    for r in c.execute(f"""SELECT f.path, f.name, f.dir, f.kind, f.size, f.mtime, f.words
                                           FROM fts JOIN files f ON f.id = fts.rowid
                                           WHERE fts MATCH ?{cond} ORDER BY rank LIMIT 5000""", [m] + args):
                        rows.setdefault(r[0], r)
            else:
                lk = " OR ".join("f.words LIKE ?" for _ in terms)
                for r in c.execute(f"""SELECT f.path, f.name, f.dir, f.kind, f.size, f.mtime, f.words FROM files f
                                       WHERE ({lk}){cond} LIMIT 20000""", [f"%{t}%" for t in terms] + args):
                    rows.setdefault(r[0], r)
    finally:
        c.close()
    G = len(groups)

    def hit(g, words):
        return any(a in words for a in g.get("alts") or []) or all(_term_in(w, words) for w in g["words"])

    game_groups = [g for g in groups if g.get("alts")]

    def score_rows(anchor):
        out = []
        for path, name, d, k, size, mtime, words in rows.values():
            nm, _, dr = (words or "").partition(" | ")
            nw = set(nm.split())
            aw = nw | set(dr.split())
            if anchor:
                # A game named in the request ANCHORS it: only things tied to that game count
                # (2026-10-29: "my fallout 4 vr mod vive focus 3 controller bindings" — VIVE's
                # own driver files matched 5 of 6 words but have nothing to do with Fallout,
                # while the real FO4VR binding, steam.app.611660_vive_cosmos_controller.json,
                # shares the game + 2 words and was dropped by the 2/3 rule).
                if not all(hit(g, aw) for g in game_groups):
                    continue
                rest = [g for g in groups if not g.get("alts")]
                r_any = sum(1 for g in rest if hit(g, aw))
                if rest and r_any < max(1, -(-len(rest) // 3)):      # a third of the other words
                    continue
                g_any = r_any + len(game_groups)
            else:
                g_any = sum(1 for g in groups if hit(g, aw))
                if g_any < max(1, -(-G * 2 // 3)):                    # 2/3 of the words, somewhere
                    continue
            g_name = sum(1 for g in groups if hit(g, nw))
            score = g_any * 2 + g_name * 1.5 + (1 if g_name == G else 0) + _path_weight(path)
            if k == "folder" and kind in ("code", "folder", "any") and g_name:
                score += 0.5
            out.append((score, mtime or 0, g_any == G, path, name, d, k, size))
        return out

    scored = score_rows(True) if game_groups else []
    if not scored:                         # nothing tied to the game → the normal rule
        scored = score_rows(False)
    # Plenty of results with EVERY word → leave out the partial matches.
    full = [x for x in scored if x[2]]
    if len(full) >= 5:
        scored = full
    scored.sort(key=lambda x: (-x[0], -x[1]))
    out = []
    for score, mtime, _f, path, name, d, k, size in scored:
        if not os.path.exists(path):
            continue
        out.append({"path": path, "name": name, "dir": d, "kind": k, "size": size or 0,
                    "mtime": mtime, "modified": time.strftime("%Y-%m-%d", time.localtime(mtime)) if mtime else ""})
        if len(out) >= limit:
            break
    return out


def _group_by_folder(results, min_files=3):
    """Many matches in one folder → ONE row for the folder ("12 matching files") instead of
    12 rows — 304 loose files was unreadable (2026-10-29)."""
    by_dir = OrderedDict()
    for r in results:
        if r["kind"] != "folder":
            by_dir.setdefault(r["dir"], []).append(r)
    grouped = {d for d, rs in by_dir.items() if len(rs) >= min_files}
    out, done = [], set()
    for r in results:
        if r["kind"] == "folder" and r["path"] in grouped:
            continue                                   # the folder row below says it all
        d = r["dir"]
        if r["kind"] != "folder" and d in grouped:
            if d in done:
                continue
            done.add(d)
            rs = by_dir[d]
            out.append({"path": d, "name": os.path.basename(d.rstrip("\\/")) or d, "dir": os.path.dirname(d),
                        "kind": "folder", "size": 0, "mtime": max(x["mtime"] or 0 for x in rs),
                        "modified": max(x["modified"] for x in rs), "count": len(rs),
                        "files": [x["name"] for x in rs[:5]]})
        else:
            out.append(r)
    return out


def live_search(query, kind="any", roots=None, budget_secs=8.0, limit=200):
    """Before the index is ready: a time-boxed walk of the usual places."""
    kind = _KIND_ALIASES.get(str(kind or "any").lower(), str(kind or "any").lower())
    terms = _terms(query)
    if not terms:
        return []
    home = os.path.expanduser("~")
    roots = roots or [os.path.join(home, x) for x in ("Pictures", "Videos", "Desktop", "Documents",
                                                       "Downloads", "OneDrive")]
    private = _private_dirs()
    t_end = time.time() + budget_secs
    out, stack = [], [r for r in roots if os.path.isdir(r)]
    while stack and time.time() < t_end and len(out) < limit:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not _skip_dir(e.path, e.name, os.path.splitdrive(e.path)[0] + "\\", private):
                                stack.append(e.path)
                            k = "folder"
                        else:
                            k = _KIND_OF.get(os.path.splitext(e.name)[1].lower())
                            if not k:
                                continue
                        if kind == "config":
                            if k == "folder" or not e.name.lower().endswith(CONFIG_EXTS):
                                continue
                        elif kind != "any" and k != kind and not (kind == "code" and k == "folder"):
                            continue
                        ws = _words(e.name, d)
                        wset = set(ws.replace("|", " ").split())
                        hit = sum(1 for t in terms if _term_in(t, wset))
                        if hit >= max(1, -(-len(terms) * 2 // 3)):
                            st = e.stat(follow_symlinks=False)
                            out.append({"path": e.path, "name": e.name, "dir": d, "kind": k,
                                        "size": st.st_size if k != "folder" else 0, "mtime": st.st_mtime,
                                        "modified": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))})
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def _usual_roots():
    """The person's own folders, most likely first — incl. the REAL Desktop/Documents when
    OneDrive moved them (C:\\Users\\<you>\\OneDrive\\Desktop)."""
    home = os.path.expanduser("~")
    first = []
    try:
        import s32_filesystem as _fs
        first = [_fs._get_desktop_path(), _fs._shell_folder_path("Personal", "Documents"),
                 _fs._shell_folder_path("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
                 _fs._shell_folder_path("My Pictures", "Pictures"), _fs._shell_folder_path("My Video", "Videos")]
    except Exception:
        pass
    plain = [os.path.join(home, x) for x in ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "OneDrive")]
    out, seen = [], set()
    for p in first + plain:
        k = os.path.normcase(os.path.abspath(p)) if p else ""
        if k and k not in seen and os.path.isdir(p):
            seen.add(k)
            out.append(p)
    return out


def ready():
    try:
        c = _connect()
        try:
            _init(c)
            return c.execute("SELECT COUNT(*) FROM vols").fetchone()[0] > 0
        finally:
            c.close()
    except Exception:
        return False


# ── thumbnails for the chat grid ────────────────────────────────────────────────
_thumbs = OrderedDict()
_THUMB_KEEP = 400


def thumbnail(path, size=320):
    """A small JPEG of a local picture (or a video's frame) → bytes, or None."""
    path = (path or "").strip()
    ext = os.path.splitext(path)[1].lower()
    if not path or not os.path.isabs(path) or not os.path.isfile(path) or \
            (ext not in IMAGE_EXTS and ext not in VIDEO_EXTS):
        return None
    try:
        from s32_dispatch import _someone_elses
        if _someone_elses(path):
            return None
    except Exception:
        pass
    try:
        key = (path, os.path.getmtime(path), size)
    except OSError:
        return None
    if key in _thumbs:
        _thumbs.move_to_end(key)
        return _thumbs[key]
    data = None
    try:
        import io
        from PIL import Image
        if ext in IMAGE_EXTS:
            im = Image.open(path)
            try:
                im.draft("RGB", (size, size))               # fast JPEG decode at reduced size
            except Exception:
                pass
        else:
            import cv2
            cap = cv2.VideoCapture(path)
            try:
                n = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                if n > 10:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, int(n * 0.1))
                ok, frame = cap.read()
            finally:
                cap.release()
            if not ok:
                return None
            im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        im = im.convert("RGB")
        im.thumbnail((size, size))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
        data = buf.getvalue()
    except Exception:
        return None
    _thumbs[key] = data
    while len(_thumbs) > _THUMB_KEEP:
        _thumbs.popitem(last=False)
    return data


# ── the search_computer tool ─────────────────────────────────────────────────
def _q(p):
    from urllib.parse import quote
    return quote(p, safe="")


def img_card(r):
    return {"thumb": "/thumb?path=" + _q(r["path"]), "full": "/img?path=" + _q(r["path"]),
            "src": "", "local": r["path"], "title": r["name"],
            "folder": os.path.basename(r["dir"]) or r["dir"], "source": ""}


def vid_card(r):
    return {"title": r["name"], "url": "/img?path=" + _q(r["path"]),
            "thumb": "/thumb?path=" + _q(r["path"]), "duration": "",
            "uploader": os.path.basename(r["dir"]) or r["dir"], "published": r.get("modified", ""),
            "views": "", "local": r["path"]}


def file_row(r):
    return {"name": r["name"], "path": r["path"], "kind": r["kind"], "size": r.get("size", 0),
            "modified": r.get("modified", ""), "folder": r["dir"], "count": r.get("count", 0),
            "files": r.get("files", [])}


_BATCH = {"image": 24, "video": 8}
# Searches per operator request (2026-10-29: she reworded the same search 4× until the loop
# breaker stopped her). After the 3rd she's told to answer with what she has and ASK.
_SEARCH_BUDGET = 3
_turn = {"n": 0}


def new_request():
    _turn["n"] = 0


def search_computer(query, kind="any", max_results=None):
    """Find files on this computer (every drive). Pictures/videos → shown in chat; everything
    else → a list. Read-only."""
    query = (query or "").strip()
    kind = _KIND_ALIASES.get(str(kind or "any").lower(), str(kind or "any").lower())
    if not query:
        return {"error": "Say what to look for, e.g. 'red fox' with kind='image'."}
    _turn["n"] += 1
    if _turn["n"] > _SEARCH_BUDGET:
        log(f"[S32][FIND] search budget used ({_SEARCH_BUDGET}) — not searching {query!r}")
        return {"type": "computer_search", "query": query, "kind": kind, "count": 0, "results": [],
                "quiet": True, "budget_used": True,
                "error": (f"You've already searched this computer {_SEARCH_BUDGET} times for this request — "
                          "rewording again won't find something new. Answer the operator NOW: say what you "
                          "found (and what it is), what you didn't, and ASK where it is or what it's called "
                          "(the folder, the mod manager, or the file's name).")}
    last = _turn["n"] == _SEARCH_BUDGET
    building = not ready()
    t0 = time.time()
    info = {}
    results = live_search(query, kind) if building else search(query, kind, info=info)
    fresh = False
    if not results and not building:
        # (2026-10-31) The index is rescanned every 12 hours, so a folder made or renamed
        # since then isn't in it ('gherkin1' on his OneDrive Desktop → 0 found, twice). Look
        # in the usual folders directly before saying there's nothing.
        results = live_search(query, kind, roots=list(reversed(_usual_roots())), budget_secs=4.0)
        fresh = bool(results)
    took = time.time() - t0
    # Remembered for search_code: 'search inside it' right after finding a mod, with no project yet.
    state["last_computer_search"] = {"query": query, "terms": _terms(query) if not building else query.lower().split(),
                                     "paths": [r.get("path") for r in results[:40] if r.get("path")]}
    log(f"[S32][FIND] search {query!r} kind={kind}: {len(results)} found in {took:.1f}s"
        + (" (index still building — searched the usual folders)" if building else "")
        + (" (not in the index yet — found in the usual folders)" if fresh else ""))
    note = ("The full index of this computer is still being built, so only the usual folders "
            "(Pictures, Videos, Desktop, Documents, Downloads) were searched — say so if nothing turns up."
            if building else "")
    if info.get("games"):
        note = (note + " " + "; ".join(f"{n} = Steam app {a} (its settings/bindings files are named by that "
                                       f"number, e.g. steam.app.{a}_…)" for n, a in info["games"])).strip()
    if last:
        note = (note + " This was your last search for this request: answer now with what you found, "
                "and if it isn't what the operator meant, ask where it is or what it's called.").strip()
    if not results:
        return {"type": "computer_search", "query": query, "kind": kind, "count": 0, "results": [],
                "building": building, "quiet": True,
                "error": (f"Nothing on this computer matches {query!r}" + (f" ({kind})" if kind != "any" else "")
                          + ". Files are found by their NAMES and folder names — try the words that "
                            "would be in the file or folder name. " + note).strip()}
    import s32_web
    if kind in ("image", "video"):
        batch = int(max_results or _BATCH[kind])
        sid = s32_web._more_store("local_" + kind, query, batch, results, set(), None)
        s32_web._more[sid]["page"] = s32_web._MORE_MAX_PAGES          # nothing more to fetch
        first = results[:batch]
        if kind == "image":
            s32_web._push({"type": "image_grid", "query": "💻 " + query,
                           "images": [img_card(r) for r in first], "sid": sid,
                           "more": len(results) > batch})
        else:
            s32_web._push({"type": "video_grid", "query": "💻 " + query,
                           "videos": [vid_card(r) for r in first], "sid": sid,
                           "more": len(results) > batch})
        shown = "ALREADY shown to the operator"
    else:
        batch = int(max_results or 25)
        results = _group_by_folder(results)
        sid = s32_web._more_store("local_file", query, batch, results, set(), None)
        s32_web._more[sid]["page"] = s32_web._MORE_MAX_PAGES
        first = results[:batch]
        s32_web._push({"type": "file_list", "query": query, "rows": [file_row(r) for r in first],
                       "sid": sid, "more": len(results) > batch, "total": len(results)})
        shown = "listed for the operator"
    return {"type": "computer_search", "query": query, "kind": kind, "count": len(results),
            "shown": len(first), "building": building, "note": note, "displayed": shown,
            "results": [{"path": r["path"], "kind": r["kind"], "size": r.get("size", 0),
                         "modified": r.get("modified", ""), "count": r.get("count", 0),
                         "files": r.get("files", [])} for r in results[:40]]}
