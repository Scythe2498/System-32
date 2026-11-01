# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_reset.py — Factory reset (2026-10-28).

Privacy is the product: a factory reset leaves NOTHING behind about anyone who used
this drive — no memories, conversations, browsing history, screenshots, clipboard
snapshots, attachments, generations, save states, datasets, trained models, coding
projects, profiles, preferences or learned behaviour. What stays is only what System
32 needs to run: the program itself, the bundled Python, ComfyUI and the models in its
models folder, llama-server, the reference docs, the workflows, and the boot config.

After the wipe the process exits (so nothing still held in memory can be written back
out), and the next launch is a true first boot.

Rule for the reference folder: KEEP-list, not a delete-list. Everything there is
deleted except the shipped reference docs (*.md that aren't state files), so any new
state file a future feature adds is wiped too without anyone remembering to list it.
"""

import os
import shutil
import threading
import time

from s32_config import CONFIG, REFERENCE_DIR, state, log, _script_dir

CONFIRM_WORD = "RESET"

# .md files in reference material that ARE state (not docs) — deleted.
_STATE_MD = {"identity.md", "index.md", "memory.md", "memory_recent.md",
             "memory_index.md", "session_log.md"}
# The ComfyUI example image ships with ComfyUI.
_COMFY_KEEP = {"example.png"}
# config.json keys that point at a person's own things.
_PERSONAL_CONFIG_KEYS = ("unreal_project",)


def _out_base():
    out = CONFIG.get("out_images", "")
    return os.path.dirname(out) if out else ""


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _inside(child, parent):
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def _too_broad(p):
    """Never touch a drive root, a user's home folder, or anything that CONTAINS the
    program — whatever config.json says. A misconfigured path must fail safe."""
    try:
        n = _norm(p)
        drive, rest = os.path.splitdrive(n)
        if len([x for x in rest.replace("\\", "/").split("/") if x]) < 1:
            return True                                     # drive / filesystem root
        home = os.path.expanduser("~")
        if home and _inside(home, n):
            return True                                     # a user profile (or above)
        if _inside(_script_dir, n) or (REFERENCE_DIR and _inside(REFERENCE_DIR, n)
                                       and _norm(REFERENCE_DIR) != n):
            return True                                     # would contain System 32
    except Exception:
        return True
    return False


def _ours(p):
    """True for a folder that belongs to System 32 on its own drive (inside the program
    folder, or a 'Sys 32'/'System 32' folder on the same drive) — not a computer's own
    ComfyUI in Documents."""
    try:
        if _inside(p, _script_dir):
            return True
        same_drive = os.path.splitdrive(_norm(p))[0] == os.path.splitdrive(_norm(_script_dir))[0]
        low = _norm(p).lower()
        return same_drive and ("sys 32" in low or "system 32" in low) \
            and not _inside(p, os.path.expanduser("~"))
    except Exception:
        return False


def _comfy_dir():
    return CONFIG.get("comfy_image_dir", "")


def _targets():
    """[(label, path, mode)]. mode: 'all' deletes the path itself (file or folder);
    'contents' empties a folder but keeps it; 'contents_except' keeps _COMFY_KEEP;
    's32_only' removes only files System 32 itself put there (named s32_*)."""
    t = []
    ref = REFERENCE_DIR
    # Reference folder: only when it really IS System 32's reference folder.
    if ref and os.path.isdir(ref) and "reference" in os.path.basename(ref.rstrip("\\/")).lower():
        for name in sorted(os.listdir(ref)):
            p = os.path.join(ref, name)
            low = name.lower()
            if low in ("code_index.md", "net_owners"):
                continue                                   # code_index: generated; net_owners: public table, nobody's data
            if os.path.isfile(p) and low.endswith(".md") and low not in _STATE_MD:
                continue                                   # shipped reference doc — keep
            t.append(("Memories, profiles, history & learned data", p, "all"))
    # Output: System 32's own folders + files by name — never "everything in the parent".
    lab = "Generations, save states, datasets & trained models (Output)"
    ob = _out_base()
    # Only System 32's own Output folder, and only paths inside it — a config.json that
    # points outputs into someone's user folder must never turn "downloads" or "training"
    # into THEIR Downloads/training (2026-10-28 review).
    if ob and os.path.basename(ob.rstrip("\\/")).lower() == "system 32's output":
        for k in ("out_images", "out_videos", "out_3d", "out_checkpoints", "out_dataset",
                  "out_diffusion", "out_loras"):
            if CONFIG.get(k) and _inside(CONFIG[k], ob) and _norm(CONFIG[k]) != _norm(ob):
                t.append((lab, CONFIG[k], "contents"))
        for name in ("save states", "gameplay_dataset", "training", "downloads", "exports",
                     "inputs", "visual_index.json", "image_index.json"):
            t.append((lab, os.path.join(ob, name), "all"))
    else:
        # Outputs configured somewhere else: remove only System 32's generated files.
        for k in ("out_images", "out_videos", "out_3d"):
            if CONFIG.get(k):
                t.append((lab, CONFIG[k], "generated_only"))
    home = _script_dir
    for name in (".attachments", "workspace", "patches"):
        t.append(("Dropped files & old workspaces", os.path.join(home, name), "all"))
    t.append(("Chat customizations (incl. any background photo)",
              os.path.join(home, "ui_prefs.json"), "all"))
    for name in ("llama_server.log", "node_deps.log"):
        t.append(("Logs", os.path.join(home, name), "all"))
    # SearXNG's caches (incl. per-query tokens). Wiped on every start anyway; the reset
    # must not rely on that.
    t.append(("Search caches", os.path.join(home, "searxng", "tmp"), "contents"))
    cd = _comfy_dir()
    if cd and os.path.isdir(cd):
        # ComfyUI is launched with --input-directory = comfy_image_input (the drive's
        # "Sys 32's comfyui resources\\input", or a computer's own install), and stages
        # outputs in <install>\\output.
        inp = CONFIG.get("comfy_image_input") or os.path.join(cd, "input")
        if _ours(inp):
            t.append(("ComfyUI copies of your images", inp, "contents_except"))
        else:
            t.append(("ComfyUI copies of your images", inp, "s32_only"))
        if _inside(cd, home):
            # The ComfyUI that ships on the drive: everything in it is System 32's.
            outp = os.path.join(cd, "output")
            t.append(("ComfyUI copies of your images", outp, "contents"))
            t.append(("ComfyUI copies of your images", os.path.join(cd, "temp"), "contents"))
            t.append(("Workflows saved in the ComfyUI tab",
                      os.path.join(cd, "user", "default", "workflows"), "contents"))
        t.append(("Logs", os.path.join(cd, "system32_image.log"), "all"))
    return [x for x in t if os.path.exists(x[1]) and not _too_broad(x[1])]


_GEN_NAME = __import__("re").compile(r"^(image|video|model|comfyui|s32)_\d{8}_\d{6}", 2)


def _is_generated(name):
    """System 32's own output file names (image_YYYYMMDD_HHMMSS…)."""
    return bool(_GEN_NAME.match(name))


def _count(path, mode):
    if os.path.isfile(path):
        return 1
    if mode == "s32_only":
        return sum(1 for n in os.listdir(path) if n.lower().startswith("s32_"))
    if mode == "generated_only":
        return sum(1 for n in os.listdir(path) if _is_generated(n))
    n = 0
    for _r, _d, files in os.walk(path):
        n += len(files)
    return n


# Windows Firewall rules System 32 made: Iron Dome lockdown rules, outbound blocks, and
# blocks the network guardian made on the operator's Confirm. They aren't files, so the
# folder wipe never reached them — and they're a record of what this person blocked.
_FW_RULE_PATTERNS = ("S32_IronDome_*", "S32_Egress_*", "S32_Airplane_*", "System32 block *")


def _fw_rules():
    """Names of System 32's firewall rules (None if Windows Firewall can't be read)."""
    try:
        import s32_irondome as _d
        pats = ",".join(f"'{x}'" for x in _FW_RULE_PATTERNS)
        r = _d._run_hidden(["powershell", "-NoProfile", "-Command",
                            f"Get-NetFirewallRule -DisplayName {pats} -ErrorAction SilentlyContinue "
                            f"| ForEach-Object {{ $_.DisplayName }}"])
        if getattr(r, "returncode", 1) != 0:
            return None
        return sorted({ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()})
    except Exception:
        return None


def _remove_fw_rules():
    """→ list of rules still there afterwards (removing needs administrator rights)."""
    try:
        import s32_irondome as _d
        for pat in _FW_RULE_PATTERNS:
            _d._run_hidden(["powershell", "-NoProfile", "-Command",
                            f"Remove-NetFirewallRule -DisplayName '{pat}' -ErrorAction SilentlyContinue"])
    except Exception:
        pass
    return _fw_rules() or []


def preview():
    """What a factory reset would remove, grouped for the confirm dialog."""
    groups = {}
    for label, p, mode in _targets():
        groups[label] = groups.get(label, 0) + _count(p, mode)
    fw = _fw_rules() or []
    if fw:
        try:
            import s32_irondome as _d
            admin = _d._is_admin()
        except Exception:
            admin = False
        groups["Windows Firewall rules System 32 made" + ("" if admin else
               " (needs System 32 running as administrator to remove)")] = len(fw)
    return {"groups": [{"label": k, "files": v} for k, v in groups.items()],
            "keeps": "System 32 itself, Python, ComfyUI and its models, llama-server, the "
                     "reference docs, the workflows, and the boot settings.",
            "confirm_word": CONFIRM_WORD}


def _rm(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
    return not os.path.exists(path)


def _quiet_background():
    """Stop everything that could write personal data back while (or after) we wipe."""
    state["resetting"] = True       # writers check this (memory, session log, corpus, /chat, /shutdown)
    state["cancel"] = True          # stop a turn that's mid-generation
    try:
        from s32_config import cancel_generation
        cancel_generation()
    except Exception:
        pass
    steps = (
        ("comfyui", lambda: __import__("s32_steam")._stop_comfy()),        # unlocks its log,
        ("model", lambda: __import__("s32_hardware").shutdown_llama_server()),  # drops its history
        ("search", lambda: __import__("s32_searx").stop()),                # unlocks its caches
        ("senses", lambda: __import__("senses").stop_screen_capture()),
        ("mic", lambda: __import__("s32_stt").stop_listening()),
        ("voice", lambda: __import__("voice").stop_voice()),
        ("gameplay", lambda: __import__("s32_gameplay").end_session()),
    )
    for name, fn in steps:
        try:
            fn()
        except Exception:
            pass
    # In-memory copies that background loops would otherwise save back to disk.
    try:
        import s32_irondome as _d
        _d._baseline.clear(); _d._baseline.update({"samples": 0, "items": {}})
        _d._whitelist.clear(); _d._whitelist.update({"ports": [], "procs": []})
    except Exception:
        pass
    try:
        import s32_feedback as _fb
        _fb._stats, _fb._lessons = {}, {"lessons": []}
    except Exception:
        pass
    try:                            # the outbound watcher saves its record every minute
        import s32_egress as _eg
        with _eg._kb_lock:
            _eg._kb.clear(); _eg._kb.update({"watched_secs": 0.0, "mature": False, "programs": {}})
            _eg._dirty["v"] = False
        _eg._alerts.clear(); _eg._starts.clear()
    except Exception:
        pass
    state["messages"] = []
    state["hist_start"] = 0
    state["session_images"] = []


def factory_reset(confirm=""):
    if str(confirm or "").strip().upper() != CONFIRM_WORD:
        return {"error": f"Type {CONFIRM_WORD} to confirm."}
    log("[S32][RESET] FACTORY RESET — wiping all personal data")
    _quiet_background()
    removed, failed = 0, []
    for label, p, mode in _targets():
        if mode == "all":
            n = _count(p, mode)
            if _rm(p):
                removed += n
            else:
                failed.append(p)
        else:
            for name in os.listdir(p):
                if mode == "contents_except" and name.lower() in _COMFY_KEEP:
                    continue
                if mode == "s32_only" and not name.lower().startswith("s32_"):
                    continue
                if mode == "generated_only" and not _is_generated(name):
                    continue
                q = os.path.join(p, name)
                n = _count(q, "all")
                if _rm(q):
                    removed += n
                else:
                    failed.append(q)
    fw_left = _remove_fw_rules()
    if fw_left:
        log(f"[S32][RESET] {len(fw_left)} Windows Firewall rule(s) could not be removed — "
            f"System 32 isn't running as administrator: {', '.join(fw_left[:6])}")
    # Personal pointers in the boot config (the config itself is needed to boot).
    try:
        import json
        cf = os.path.join(_script_dir, "config.json")
        if os.path.exists(cf):
            with open(cf, encoding="utf-8") as f:
                cfg = json.load(f)
            if any(k in cfg for k in _PERSONAL_CONFIG_KEYS):
                for k in _PERSONAL_CONFIG_KEYS:
                    cfg.pop(k, None)
                with open(cf, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2)
    except Exception as e:
        log(f"[S32][RESET] config clean skipped: {e}")
    # Leave the empty output folders bootstrap expects.
    for k in ("out_images", "out_videos", "out_3d"):
        try:
            if CONFIG.get(k):
                os.makedirs(CONFIG[k], exist_ok=True)
        except Exception:
            pass
    log(f"[S32][RESET] removed {removed} file(s)"
        + (f"; {len(failed)} item(s) were in use and could not be removed" if failed else "")
        + " — System 32 will close now; the next start is a fresh first boot.")

    try:      # tells launch.py to boot again from scratch (see launch.py _boot_once)
        open(os.path.join(_script_dir, ".factory_reset_restart"), "w").close()
    except Exception:
        pass

    def _exit_soon():
        time.sleep(1.5)                 # let the HTTP reply reach the browser
        os._exit(0)                     # no shutdown hooks: nothing in memory gets saved
    threading.Thread(target=_exit_soon, daemon=True).start()
    return {"ok": True, "removed": removed, "locked": failed[:10], "firewall_rules_left": fw_left[:20]}
