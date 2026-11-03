# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_memory.py — Memory system: identity, operators, machines, session log,
                memory tiers (permanent / recent / archive), memory index.
"""
from s32_config import (
    CONFIG, REFERENCE_DIR, PERSON_DIR, PEOPLE_DIR, _person_slug,
    SAVE_STATES_ROOT as _SSR, TRAINING_ROOT, GAMEPLAY_ROOT, person_subdir, _first_person, SESSION_LOG_FILE, MEMORY_FILE, MEMORY_RECENT,
    MEMORY_ARCHIVE, MEMORY_INDEX, OPERATORS_DIR, MACHINES_DIR, IDENTITY_FILE,
    WORKFLOW_DIR,
    _machine_name, _current_operator, _session_start, _hw, _script_dir,
    state, log, http_post,
    os, json, datetime, threading, time,
)

# ── Personality (per-iteration identity: name + gender) ───────────────────────
# These belong to an individual saved personality, NOT the global platform, so
# they live here (bundled into snapshots) rather than in config.json. A global
# config reset can't touch them; loading a snapshot restores that personality's
# name + gender. Colors stay global in config.json (they have their own reset).
PERSONALITY_FILE = os.path.join(REFERENCE_DIR, "personality.json")
_PERSONALITY_DEFAULTS = {"assistant_name": "System 32", "gender": "Neutral"}

def _load_personality():
    """Return {'assistant_name':..., 'gender':...}, falling back to defaults."""
    p = dict(_PERSONALITY_DEFAULTS)
    try:
        if os.path.exists(PERSONALITY_FILE):
            with open(PERSONALITY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if data.get("assistant_name"):
                    p["assistant_name"] = str(data["assistant_name"]).strip() or "System 32"
                g = str(data.get("gender", "Neutral")).strip().capitalize()
                p["gender"] = g if g in ("Male", "Female", "Neutral") else "Neutral"
    except Exception:
        pass
    return p

def _save_personality(assistant_name=None, gender=None):
    """Update name and/or gender in personality.json (per-iteration store)."""
    p = _load_personality()
    if assistant_name is not None:
        p["assistant_name"] = str(assistant_name).strip() or "System 32"
    if gender is not None:
        g = str(gender).strip().capitalize()
        p["gender"] = g if g in ("Male", "Female", "Neutral") else "Neutral"
    try:
        os.makedirs(REFERENCE_DIR, exist_ok=True)
        with open(PERSONALITY_FILE, "w", encoding="utf-8") as f:
            json.dump(p, f, indent=2)
    except Exception:
        pass
    return p

def _ensure_memory_structure():
    """Create all memory folders and files if they don't exist."""
    if state.get("resetting"):
        return            # factory reset in progress — never recreate personal folders
    try:
        os.makedirs(REFERENCE_DIR,   exist_ok=True)
        os.makedirs(MEMORY_ARCHIVE,  exist_ok=True)
        os.makedirs(OPERATORS_DIR,   exist_ok=True)
        os.makedirs(MACHINES_DIR,    exist_ok=True)
        # Create empty files if missing
        for path in [MEMORY_FILE, MEMORY_RECENT, SESSION_LOG_FILE]:
            if not os.path.exists(path):
                open(path, "w", encoding="utf-8").close()
    except Exception as e:
        log(f"[S32] Memory structure init failed: {e}")

def _ensure_reference_dir():
    try:
        os.makedirs(REFERENCE_DIR, exist_ok=True)
    except Exception:
        pass

def _load_operator(name):
    """Load an operator profile. Returns dict or None."""
    if not name:
        return None
    path = os.path.join(OPERATORS_DIR, f"{name}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {"name": name, "path": path, "content": f.read()}
    except Exception:
        return None

def _save_operator(name, content):
    """Save or update an operator profile."""
    _ensure_memory_structure()
    path = os.path.join(OPERATORS_DIR, f"{name}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"[S32] Operator profile saved: {name}")
    except Exception as e:
        log(f"[S32] Operator save failed: {e}")

def _load_machine(machine_name):
    """Load a machine profile. Returns content string or None."""
    path = os.path.join(MACHINES_DIR, f"{machine_name}.md")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None

def _save_machine(machine_name, content):
    """Save machine preferences."""
    _ensure_memory_structure()
    path = os.path.join(MACHINES_DIR, f"{machine_name}.md")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        log(f"[S32] Machine profile saved: {machine_name}")
    except Exception as e:
        log(f"[S32] Machine save failed: {e}")

def _reset_machine_preferences(machine_name=None):
    """Clear the saved Operator Preferences for a machine profile, keeping the
    profile shell (machine name, operator, notes) intact. Used by Memory Reset."""
    machine_name = machine_name or _machine_name
    profile = _load_machine(machine_name)
    if not profile:
        return {"ok": True, "note": "no machine profile to reset"}
    out = []
    in_prefs = False
    for line in profile.splitlines():
        if "## Operator Preferences" in line:
            out.append(line)
            out.append("*(None recorded yet)*")
            in_prefs = True
            continue
        if in_prefs:
            if line.startswith("## "):   # reached the next section — resume copying
                in_prefs = False
                out.append(line)
            continue                      # otherwise drop the old preference lines
        out.append(line)
    _save_machine(machine_name, "\n".join(out))
    log(f"[S32] Machine preferences reset: {machine_name}")
    return {"ok": True, "machine": machine_name}

# ── State snapshots (save / load its persistent brain) ──────────────────────
# Saved under System 32's Output folder, in a "save states" subfolder beside the
# generated-images/videos folders. Derived from the runtime output path so it
# honors any config.json override, with a sensible fallback.
_out_images = CONFIG.get("out_images", "")
_out_base = (os.path.dirname(_out_images) if _out_images
             else os.path.join(os.path.dirname(_script_dir), "System 32's Output"))
SAVE_STATES_ROOT = _SSR or os.path.join(_out_base, "save states")
# Save states are per person (2026-10-28): a save state holds that person's memories,
# so it's listed and loadable only on their computer.
SNAPSHOTS_DIR = os.path.join(SAVE_STATES_ROOT, _person_slug(_machine_name))


def _migrate_legacy_snapshots():
    """Zips saved before per-person save states sat loose in 'save states\\'. They hold
    the first person's memories, so they go to the first person (people\\.first) — the
    same owner as the memories themselves, whichever computer happens to boot."""
    try:
        if not os.path.isdir(SAVE_STATES_ROOT):
            return
        loose = [f for f in os.listdir(SAVE_STATES_ROOT) if f.lower().endswith(".zip")]
        if not loose:
            return
        owner = _first_person() or _person_slug(_machine_name)
        dest = os.path.join(SAVE_STATES_ROOT, owner)
        os.makedirs(dest, exist_ok=True)
        import shutil
        for f in loose:
            shutil.move(os.path.join(SAVE_STATES_ROOT, f), os.path.join(dest, f))
        log(f"[S32] Moved {len(loose)} earlier save state(s) into {owner}'s folder")
    except Exception as e:
        log(f"[S32] Save-state migration skipped: {e}")


_migrate_legacy_snapshots()

# Which snapshot is "currently running" — set on save/load, shown in the UI.
ACTIVE_STATE_FILE = os.path.join(REFERENCE_DIR, "active_state.json")


def get_active_snapshot():
    try:
        with open(ACTIVE_STATE_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get("active", "") or ""
    except Exception:
        return ""


def set_active_snapshot(name):
    try:
        _ensure_reference_dir()
        tmp = ACTIVE_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"active": name or ""}, f)
        os.replace(tmp, ACTIVE_STATE_FILE)
    except Exception:
        pass

# ── Window layout (settings panel + game-library geometry) ───────────────────
# Position/size of the draggable popups, so they reopen where you left them.
# Lives in reference/ (survives every reboot) and is also bundled into snapshots.
UI_LAYOUT_FILE = os.path.join(REFERENCE_DIR, "ui_layout.json")


def load_ui_layout():
    try:
        with open(UI_LAYOUT_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_ui_layout(key, geo):
    """Merge-save geometry for one panel (e.g. 'settings', 'library',
    'cat_model'…) without clobbering the others. `geo` is {left,top,width,height}."""
    try:
        if (not isinstance(key, str) or not key or len(key) > 40
                or not all(c.isalnum() or c == "_" for c in key)
                or not isinstance(geo, dict)):
            return {"error": "bad layout payload"}
        layout = load_ui_layout()
        clean = {}
        for k in ("left", "top", "width", "height"):
            v = geo.get(k)
            if isinstance(v, (int, float)):
                clean[k] = round(float(v))
        layout[key] = clean
        _ensure_reference_dir()
        tmp = UI_LAYOUT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(layout, f, indent=2)
        os.replace(tmp, UI_LAYOUT_FILE)
        return {"ok": True, "key": key, "geo": clean}
    except Exception as e:
        return {"error": str(e)}


def _snapshot_targets():
    """(files, dirs) a save state carries — {arcname: path on THIS computer}."""
    # SHARED (who it is + what it's learned) and PERSON (everything about this
    # person) — v2 manifest. Person data is restored into whichever person folder is
    # loading it, so the zip never carries absolute paths for it.
    files = {
        "identity.md":           IDENTITY_FILE,
        "personality.json":      PERSONALITY_FILE,
        "voice_preference.json": os.path.join(REFERENCE_DIR, "voice_preference.json"),
        "lessons.json":          os.path.join(REFERENCE_DIR, "lessons.json"),
        "tool_stats.json":       os.path.join(REFERENCE_DIR, "tool_stats.json"),
        "model_tuning.json":     os.path.join(REFERENCE_DIR, "model_tuning.json"),
        "ui_layout.json":        UI_LAYOUT_FILE,
        "steam_ui.json":         os.path.join(REFERENCE_DIR, "steam_ui.json"),
        "machine.md":            os.path.join(MACHINES_DIR, f"{_machine_name}.md"),
    }
    # Only THIS person's parts of the per-person Output roots — never the shared
    # roots themselves (that would carry other people's data into this zip).
    dirs = {"person": PERSON_DIR,                          # memories, history, boards, favorites…
            "training": person_subdir(TRAINING_ROOT),      # their exports + code/UE corpora
            "gameplay_dataset": person_subdir(GAMEPLAY_ROOT),
            "workflows": WORKFLOW_DIR}                     # shared: restored by MERGE, never swapped
    td = dirs.get("training") or ""
    # Never let a training folder that IS the reference root (or holds people\)
    # sweep everyone's data into one person's save state.
    if td and (os.path.normcase(os.path.abspath(td)) == os.path.normcase(os.path.abspath(REFERENCE_DIR))
               or os.path.isdir(os.path.join(td, "people"))):
        dirs["training"] = ""
    return files, dirs


def save_state_snapshot(name=None):
    """Bundle its persistent state — memory tiers, identity, machine/operator
    profiles, image favorites, voice/training/visual data — into a zip on the
    drive (reference/snapshots/). If `name` is given it's used as the filename
    (sanitized), overwriting any existing snapshot of that name; otherwise a
    timestamped name is generated. Returns the snapshot filename/path."""
    import re as _re
    import zipfile
    _ensure_memory_structure()
    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = (name or "").strip()
    if name:
        # Sanitize to a safe bare filename. Path separators are removed (no
        # traversal possible), but dots are allowed so names like "v1.5" survive.
        safe = _re.sub(r'[^A-Za-z0-9 _.\-]', "", name).strip().replace(" ", "_")[:80]
        safe = safe.lstrip(".")
        if not safe or safe in (".", ".."):
            safe = f"snapshot_{ts}"
        fname = safe if safe.lower().endswith(".zip") else safe + ".zip"
    else:
        fname = f"snapshot_{ts}.zip"
    fpath = os.path.join(SNAPSHOTS_DIR, fname)
    overwritten = os.path.exists(fpath)

    files, dirs = _snapshot_targets()
    manifest = {"version": 2, "files": {}, "dirs": {}, "created": ts, "machine": _machine_name}
    try:
        with zipfile.ZipFile(fpath, "w", zipfile.ZIP_DEFLATED) as z:
            for arc, p in files.items():
                if p and os.path.isfile(p):
                    z.write(p, arcname="files/" + arc)
                    manifest["files"][arc] = p
            for dname, d in dirs.items():
                if d and os.path.isdir(d):
                    manifest["dirs"][dname] = d
                    for root, _, fnames in os.walk(d):
                        for fn in fnames:
                            full = os.path.join(root, fn)
                            rel = os.path.relpath(full, d).replace("\\", "/")
                            z.write(full, arcname=f"dirs/{dname}/{rel}")
            z.writestr("manifest.json", json.dumps(manifest, indent=2))
        log(f"[S32] State snapshot saved: {fname}")
        set_active_snapshot(fname)
        return {"ok": True, "filename": fname, "path": fpath, "overwritten": overwritten,
                "files": len(manifest["files"]), "dirs": len(manifest["dirs"])}
    except Exception as e:
        log(f"[S32] Snapshot save failed: {e}")
        return {"error": str(e)}


def list_state_snapshots():
    """Return available snapshot filenames, newest first."""
    if not os.path.isdir(SNAPSHOTS_DIR):
        return []
    return sorted([f for f in os.listdir(SNAPSHOTS_DIR) if f.endswith(".zip")], reverse=True)


def _safe_rel(rel):
    """Strip any path traversal from a zip member's relative path — including drive
    letters ('C:'), which os.path.join would otherwise treat as a new root."""
    rel = rel.replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".", "..") and ":" not in p]
    return os.path.join(*parts) if parts else ""


def _within(path, root):
    a = os.path.normcase(os.path.abspath(path))
    b = os.path.normcase(os.path.abspath(root)).rstrip("\\/") + os.sep
    return a.startswith(b)


def load_state_snapshot(filename):
    """Restore its persistent state from a snapshot zip created by save_state_snapshot."""
    import zipfile
    if not filename or os.path.basename(filename) != filename:
        return {"error": "Invalid snapshot name."}
    fpath = os.path.join(SNAPSHOTS_DIR, filename)
    if not os.path.isfile(fpath):
        return {"error": f"Snapshot not found: {filename}"}
    try:
        with zipfile.ZipFile(fpath, "r") as z:
            names = z.namelist()
            if "manifest.json" not in names:
                return {"error": "Snapshot missing manifest."}
            manifest = json.loads(z.read("manifest.json"))
            bad = z.testzip()
            if bad:
                # Check the whole zip BEFORE replacing anything: a corrupt member used
                # to surface only after the live person folder had been emptied.
                return {"error": f"Snapshot is damaged ({bad}) — nothing was changed."}
            restored = 0
            files_t, dirs_t = _snapshot_targets()
            # Where each saved item goes on THIS computer. v1 zips (before per-person
            # folders) stored absolute paths and kept memories at the reference root —
            # route those into this person's folder instead of the dead shared paths.
            _legacy_person_files = {"memory.md", "memory_recent.md", "memory_index.md",
                                    "favorites.json", "path_favorites.json",
                                    "conversation_log.jsonl", "session_log.md", "thoughts.log"}
            def _file_dest(arc):
                if arc in files_t:
                    return files_t[arc]
                if arc in _legacy_person_files:
                    return os.path.join(PERSON_DIR, arc)
                return None
            def _dir_dest(dname):
                if dname in dirs_t:
                    return dirs_t[dname]
                return {"archive": MEMORY_ARCHIVE, "operators": OPERATORS_DIR,
                        "machines": MACHINES_DIR}.get(dname)
            _same_machine = manifest.get("machine", _machine_name) == _machine_name
            for arc in manifest.get("files", {}):
                if arc == "machine.md" and not _same_machine:
                    continue          # another computer's profile never replaces this one's
                member = "files/" + arc
                dest = _file_dest(arc)
                if member in names and dest:
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    with open(dest, "wb") as f:
                        f.write(z.read(member))
                    restored += 1
            import shutil
            for dname in manifest.get("dirs", {}):
                ddest = _dir_dest(dname)
                if not ddest:
                    continue
                os.makedirs(ddest, exist_ok=True)
                if dname in ("gameplay_dataset", "person", "training"):
                    # Clean swap: the snapshot's set fully replaces the current one
                    # (a true trade-out, not a merge) — for a person, their whole folder.
                    for _e in os.listdir(ddest):
                        _p = os.path.join(ddest, _e)
                        try:
                            shutil.rmtree(_p) if os.path.isdir(_p) else os.remove(_p)
                        except Exception:
                            pass
                prefix = f"dirs/{dname}/"
                for n in names:
                    if n.startswith(prefix) and not n.endswith("/"):
                        rel = _safe_rel(n[len(prefix):])
                        if not rel:
                            continue
                        out = os.path.join(ddest, rel)
                        if not _within(out, ddest):
                            continue          # zip-slip: never write outside the destination
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        with open(out, "wb") as f:
                            f.write(z.read(n))
                        restored += 1
        log(f"[S32] State snapshot restored: {filename} ({restored} files)")
        # If a voice preference was in the snapshot, apply it live so the restored
        # voice takes effect immediately instead of waiting for the next reboot.
        try:
            if "voice_preference.json" in manifest.get("files", {}):
                import voice as _voice
                _prefs = _voice._load_voice_preference()
                if _prefs and _prefs.get("voice"):
                    _voice.set_voice(_prefs["voice"], _prefs.get("speed"), _prefs.get("volume"))
                    log(f"[S32] Voice restored from snapshot: {_prefs['voice']}")
        except Exception as _ve:
            log(f"[S32] Could not apply restored voice: {_ve}")
        # If training data was in the snapshot, drop the feedback cache so the
        # restored lessons/stats take effect immediately.
        try:
            if ("lessons.json" in manifest.get("files", {}) or
                    "tool_stats.json" in manifest.get("files", {})):
                from s32_feedback import reload as _fb_reload
                _fb_reload()
                log("[S32] Training data restored from snapshot.")
        except Exception as _fe:
            log(f"[S32] Could not reload restored training data: {_fe}")
        # If the game-library setup was restored, drop its in-memory cache so the
        # restored categories/colors/hidden show up without a reboot.
        try:
            if "steam_ui.json" in manifest.get("files", {}):
                import s32_steam
                s32_steam._ui = None
                log("[S32] Game library restored from snapshot.")
        except Exception as _se:
            log(f"[S32] Could not reload restored game library: {_se}")
        try:
            _detect_operator()
            from s32_llm import invalidate_prompt_cache as _ipc
            _ipc()
        except Exception:
            pass
        set_active_snapshot(filename)
        return {"ok": True, "restored": filename, "files": restored}
    except Exception as e:
        log(f"[S32] Snapshot load failed: {e}")
        return {"error": str(e)}

_NOT_INTRODUCED = "(not introduced yet)"


def current_operator():
    """The person at THIS computer, or None if they haven't said their name yet."""
    return state.get("operator") or None


def _detect_operator():
    """
    New computer = new person. The person is whoever this computer's profile names;
    a computer it has never seen gets a fresh profile and an introduction, and it
    asks their name (remember_operator records it). No names are hardcoded.
    Returns (operator_name_or_None, is_new_machine, creator_known) — the third value
    is kept for the startup log's shape (always True: the creator credit is built in).
    """
    global _current_operator
    import re as _re
    _ensure_memory_structure()
    machine_profile = _load_machine(_machine_name)
    is_new_machine = machine_profile is None
    operator = None
    if is_new_machine:
        log(f"[S32] New computer: {_machine_name} — new person; will introduce and ask their name")
        from s32_hardware import get_hardware
        hw = get_hardware()
        _save_machine(_machine_name, f"""# Machine: {_machine_name}
*First seen: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*

## Hardware
- GPU: {hw.get('gpu_name', 'unknown')}
- VRAM: {hw.get('vram_mb', 0)}MB
- RAM: {hw.get('ram_mb', 0)}MB
- Tier: {hw.get('tier', 'unknown')}

## Primary Operator
{_NOT_INTRODUCED}

## Operator Preferences
*(None recorded yet)*

## Notes
*(Auto-created on first boot)*
""")
    else:
        log(f"[S32] Known machine: {_machine_name}")
        m = _re.search(r'## Primary Operator\s*\n([^\n]+)', machine_profile)
        name = m.group(1).strip() if m else ""
        if name and name != _NOT_INTRODUCED and name != _machine_name:
            operator = name
        elif not m:
            _save_machine(_machine_name, machine_profile.replace(
                "## Operator Preferences",
                f"## Primary Operator\n{_NOT_INTRODUCED}\n\n## Operator Preferences"))
    state["operator"] = operator
    _current_operator = operator
    return operator, is_new_machine, True


def remember_operator(name):
    """Tool: the person at this computer gave their name. Saves it to this
    computer's profile and starts their own profile in their person folder."""
    import re as _re
    name = _re.sub(r"[\r\n#*]+", " ", str(name or "")).strip()[:40]
    if not name:
        return {"error": "Give the name the person wants to be called."}
    prof = _load_machine(_machine_name) or ""
    if "## Primary Operator" in prof:
        prof = _re.sub(r"(## Primary Operator\s*\n)[^\n]*", lambda m: m.group(1) + name, prof, count=1)
    else:
        prof = prof.rstrip() + f"\n\n## Primary Operator\n{name}\n"
    _save_machine(_machine_name, prof)
    if not _load_operator(name):
        _save_operator(name, f"""# Operator: {name}
*Profile started: {datetime.datetime.now().strftime('%Y-%m-%d')} on {_machine_name}*

## About them
*(Things they share about themselves go here.)*

## Preferences
*(None recorded yet)*
""")
    global _current_operator
    state["operator"] = name
    _current_operator = name
    # No system-prompt rebuild for this (2026-11-02): any change to the prompt makes the model
    # re-read ALL of it (minutes on a big model). The name is already in this conversation
    # (the remember_operator call + its result); the next start's prompt carries it.
    log(f"[S32] Operator for {_machine_name}: {name}")
    return {"ok": True, "quiet": True, "operator": name, "machine": _machine_name}


def _get_introduction_message():
    """
    Build introduction message for new machines.
    It introduces itself and requests the operator's name.
    """
    nm = _load_personality().get("assistant_name") or "System 32"
    return f"""Hi - I'm {nm}. This is the first time I've run on this computer, so we haven't met.

I'm a local AI assistant built by SoullessScythe. I run locally on this drive, and what I remember about you stays here — private to you and this computer. I can generate images and video, navigate your files, write code, and remember things across sessions.

**What should I call you?**"""


def _create_soullessscythe_profile():
    """Retired (2026-10-28): the creator's personal profile is no longer created on
    every computer — new computer = new person. Kept so old imports don't break."""
    return


_BASE_IDENTITY = """# System 32 - Identity

**First boot:** {first}
**Boot count:** 1
**Created by:** SoullessScythe

## Base Identity
System 32 is a helpful, capable local AI assistant — friendly, direct, and practical. It helps with files, navigation, image/video/3D generation, and everyday tasks. It has no fixed persona beyond being a reliable, neutral assistant; its character can grow over time through the identity editor and its memories.

This file holds the core identity and tracks how long the assistant has been running. It is updated every session.
"""


def _load_identity():
    """Read identity.md - boot count, first boot date. On a first boot (fresh drive,
    or after a factory reset) create the neutral base identity — the old first-boot
    creator had ended up as unreachable code inside a retired function."""
    if not os.path.exists(IDENTITY_FILE):
        _ensure_reference_dir()
        now = datetime.datetime.now()
        try:
            with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
                f.write(_BASE_IDENTITY.format(first=now.strftime("%Y-%m-%d %H:%M")))
            log("[S32] First boot — base identity created.")
        except Exception:
            return {}
    try:
        with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
            return {"content": f.read()}
    except Exception:
        return {}

def _update_identity():
    """Increment boot count and update last seen timestamp."""
    _ensure_reference_dir()
    try:
        if os.path.exists(IDENTITY_FILE):
            with open(IDENTITY_FILE, "r", encoding="utf-8") as f:
                content = f.read()
            # Increment boot count
            import re as _re
            m = _re.search(r"\*\*Boot count:\*\* (\d+)", content)
            if m:
                new_count = int(m.group(1)) + 1
                content = _re.sub(
                    r"\*\*Boot count:\*\* \d+",
                    f"**Boot count:** {new_count}",
                    content
                )
            # Update last seen
            now_str = _session_start.strftime("%Y-%m-%d %H:%M")
            if "**Last seen:**" in content:
                content = _re.sub(
                    r"\*\*Last seen:\*\* .+",
                    f"**Last seen:** {now_str}",
                    content
                )
            else:
                content = content.rstrip() + f"\n**Last seen:** {now_str}\n"
            with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
                f.write(content)
    except Exception as e:
        log(f"[S32] Could not update identity: {e}")

def _reset_identity():
    """Reset identity.md to a neutral base assistant identity. Used by Memory
    Reset so a wiped System 32 still has a reasonable, working base persona
    rather than an empty or stale one. Custom persona accumulated via the
    identity editor is cleared back to this baseline."""
    _ensure_reference_dir()
    now = datetime.datetime.now()
    content = _BASE_IDENTITY.format(first=now.strftime("%Y-%m-%d %H:%M"))
    try:
        with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
            f.write(content)
        # Reset per-iteration personality (name + gender) back to platform defaults too
        _save_personality(assistant_name="System 32", gender="Neutral")
        log("[S32] Identity reset to neutral base.")
        return {"ok": True}
    except Exception as e:
        log(f"[S32] Identity reset failed: {e}")
        return {"error": str(e)}

def _build_session_entry(final=False):
    """
    Build a structured session log entry - designed as a handoff document
    for the next bootstrap session, not just a conversation transcript.
    """
    now = datetime.datetime.now()
    duration = now - _session_start
    mins = int(duration.total_seconds() // 60)
    hw = _hw or {}

    lines = []
    lines.append(f"\n---\n")
    lines.append(f"## Session - {_session_start.strftime('%Y-%m-%d %H:%M')} ({mins} min){' [INCOMPLETE - crash or force-kill]' if not final else ''}\n")

    # Hardware context - critical for next session's bug fixing
    if hw:
        lines.append(f"**Hardware:** {hw.get('gpu_name','?')} | {hw.get('vram_mb',0)}MB VRAM | {hw.get('ram_mb',0)}MB RAM | Tier: {hw.get('tier','?')}")

    # Machine identity
    user = os.environ.get("USERNAME", "")
    lines.append(f"**Machine:** {os.environ.get('COMPUTERNAME', user)} ({user})")
    lines.append(f"**Script:** {_script_dir}")
    lines.append(f"**ComfyUI image:** {CONFIG.get('comfy_image_dir','?')}")
    lines.append(f"**Documents venv:** {os.path.join(os.environ.get('USERPROFILE',''), 'Documents', 'ComfyUI', '.venv', 'Scripts', 'python.exe')}")
    lines.append("")

    # What was generated
    session_imgs = state.get("session_images", [])
    if session_imgs:
        lines.append(f"**Generated:** {len(session_imgs)} image(s)")
        for img in session_imgs[:8]:
            prompt = img.get("prompt", "")
            if prompt:
                lines.append(f"  - {prompt[:100]}")
        lines.append("")

    # Errors and issues encountered this session - critical for next session
    startup_log = state.get("startup_log", [])
    errors = [l for l in startup_log if any(x in l for x in ["ERROR", "FAILED", "HTTP 400", "missing", "Cannot import", "Traceback"])]
    if errors:
        lines.append("**Errors this session:**")
        for e in errors[:8]:
            lines.append(f"  - {e.strip()[:150]}")
        lines.append("")

    # Missing nodes status
    missing = state.get("missing_nodes", {})
    if missing:
        lines.append("**Still missing nodes at session end:**")
        for label, names in missing.items():
            for n in names[:5]:
                lines.append(f"  - [{label}] {n}")
        lines.append("")

    # Conversation - full meaningful exchanges for context
    messages = state.get("messages", [])
    if messages:
        lines.append("**Conversation:**")
        exchange_count = 0
        for m in messages:
            if exchange_count >= 20:
                lines.append(f"  *...{len(messages) - exchange_count} more exchanges*")
                break
            role = m.get("role", "")
            content = (m.get("content") or "").strip()
            if not content or content.startswith("["):
                continue
            if role == "user" and len(content) > 3:
                lines.append(f"  **You:** {content[:300]}")
                exchange_count += 1
            elif role == "assistant" and len(content) > 10:
                lines.append(f"  **S32:** {content[:400]}")
                exchange_count += 1

    return "\n".join(lines)

def _compress_old_memories():
    """
    Compress memory_recent.md entries older than 7 days into weekly archive files.
    It summarizes them itself via LM Studio.
    Called on clean shutdown so it doesn't slow startup.
    """
    if not os.path.exists(MEMORY_RECENT):
        return
    if not os.path.exists(REFERENCE_DIR):
        return

    try:
        with open(MEMORY_RECENT, "r", encoding="utf-8") as f:
            content = f.read().strip()
    except Exception:
        return

    if not content:
        return

    # Parse entries by timestamp
    cutoff = datetime.datetime.now() - datetime.timedelta(days=7)
    entries = content.split("\n---\n")
    old_entries = []
    keep_entries = []

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        # Look for timestamp pattern *YYYY-MM-DD HH:MM*
        import re
        m = re.search(r'\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\*', entry)
        if m:
            try:
                ts = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
                if ts < cutoff:
                    old_entries.append(entry)
                else:
                    keep_entries.append(entry)
            except ValueError:
                keep_entries.append(entry)
        else:
            keep_entries.append(entry)

    if not old_entries:
        log("[S32] Memory compression: nothing old enough to compress")
        return

    log(f"[S32] Memory compression: {len(old_entries)} old entries to compress")

    # Group old entries by week
    weeks = {}
    for entry in old_entries:
        m = re.search(r'\*(\d{4}-\d{2}-\d{2})', entry)
        if m:
            dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d")
            # Week key: YYYY-MM-weekN
            week_num = (dt.day - 1) // 7 + 1
            week_key = f"{dt.strftime('%Y-%m')}-week{week_num}"
        else:
            week_key = "unknown"
        weeks.setdefault(week_key, []).append(entry)

    # Summarize each week via LM Studio
    for week_key, week_entries in weeks.items():
        archive_path = os.path.join(MEMORY_ARCHIVE, f"{week_key}.md")

        # Build summary via LM Studio
        raw_text = "\n\n".join(week_entries)
        try:
            summary_prompt = f"""Summarize these memory entries from System 32's logs for the week of {week_key}.
Write a concise summary (3-8 bullet points) capturing:
- Key events and decisions
- Things learned or built
- Important facts about the operator
- Any preferences or instructions given

Be specific and factual. This summary replaces the raw entries permanently.

ENTRIES:
{raw_text[:3000]}"""

            resp = http_post(
                f"{CONFIG['lmstudio_host']}/v1/chat/completions",
                {
                    "model":       CONFIG["lmstudio_model"],
                    "messages":    [{"role": "user", "content": summary_prompt}],
                    "max_tokens":  500,
                    "temperature": 0.3,
                },
                timeout=60
            )
            summary = resp.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        except Exception as e:
            log(f"[S32] Compression summarization failed: {e}")
            # Fall back to truncated raw text if LM Studio unavailable
            summary = f"[Raw entries - summarization failed]\n{raw_text[:1000]}"

        # Write or append to archive file
        archive_entry = f"""# Memory Archive - {week_key}
*Compressed: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*
*Original entries: {len(week_entries)}*

## Summary
{summary}

---
"""
        try:
            with open(archive_path, "a", encoding="utf-8") as f:
                f.write(archive_entry)
            log(f"[S32] Compressed {len(week_entries)} entries → {week_key}.md")
        except Exception as e:
            log(f"[S32] Archive write failed: {e}")

    # Rewrite memory_recent.md with only recent entries
    try:
        new_content = "\n---\n".join(keep_entries)
        with open(MEMORY_RECENT, "w", encoding="utf-8") as f:
            f.write(new_content)
        log(f"[S32] Memory compression complete. Kept {len(keep_entries)} recent entries.")
    except Exception as e:
        log(f"[S32] Failed to rewrite memory_recent: {e}")

def _start_memory_compressor():
    """Run memory compression on a background timer - every hour while running."""
    def _loop():
        while True:
            time.sleep(3600)  # every hour
            try:
                _compress_old_memories()
            except Exception as e:
                log(f"[S32] Memory compressor error: {e}")
    t = threading.Thread(target=_loop, daemon=True)
    t.name = "memory-compressor"
    t.start()
    log("[S32] Memory compressor started (runs hourly)")


def _write_session_log(final=False):
    """
    Write session log to disk. Keeps last 3 sessions.
    Safe against USB removal - fails silently if drive unavailable.
    """
    if state.get("resetting"):
        return
    _ensure_reference_dir()
    if not os.path.exists(REFERENCE_DIR):
        log("[S32] Session log skipped - reference directory not accessible (USB removed?)")
        return
    try:
        entry = _build_session_entry(final)
        path = SESSION_LOG_FILE
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                existing = f.read()
        # Keep last 3 sessions - split on ## Session
        sessions = [s for s in existing.split("## Session") if s.strip()]
        sessions = sessions[-2:]  # keep last 2, we're adding 1 more
        combined = "\n\n## Session".join([""] + sessions).strip()
        if combined:
            combined = "## Session" + combined + "\n\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(combined + entry)
        log(f"[S32] Session log updated.")
    except Exception as e:
        log(f"[S32] Session log write failed: {e}")

def _start_session_log_writer():
    """Background thread that writes session log every 5 minutes, only if something changed."""
    def _writer():
        last_message_count = 0
        while True:
            time.sleep(300)  # 5 minutes
            current_count = len(state.get("messages", []))
            if current_count != last_message_count:
                _write_session_log(final=False)
                last_message_count = current_count
    t = threading.Thread(target=_writer, daemon=True)
    t.start()

