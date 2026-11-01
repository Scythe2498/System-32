# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_gameplay.py — the self-training gameplay loop.

While it's in a game session, every successful game_input is paired with the
screenshot of the moment it acted (the frame its own see_screen just refreshed),
building a (state → action) dataset on disk. An "after" frame is grabbed a beat
later, so each step is (before, action, after) — the raw material for judging
whether a move helped. Sessions can be labeled good/bad (by the operator now, by
a self-eval or a better model later), and export() turns the labeled steps into
vision-action JSONL a fine-tune can consume.

The model isn't the point — the loop is. Prove it on the fast default, then swap
in any vision model and measure against the same accumulating dataset.

Layout:  <Output>/gameplay_dataset/<session>/
            session.json           {id, appid, name, started, label}
            steps.jsonl            one line per action (+ 'patch_after' lines)
            frames/00000_before.jpg, 00000_after.jpg, ...
"""

import os
import json
import time
import base64
import threading

from s32_config import CONFIG, REFERENCE_DIR, PERSON_DIR, GAMEPLAY_ROOT, person_subdir, state, log

_lock = threading.Lock()
_session = {"id": None, "dir": None, "appid": None, "name": None, "step": 0}


def _dataset_root():
    """This person's gameplay captures (screenshots of THEIR games) —
    Output\\gameplay_dataset\\<computer>\\, private to them."""
    return person_subdir(GAMEPLAY_ROOT) or os.path.join(PERSON_DIR, "gameplay_dataset")


def _exports_dir():
    d = os.path.join(_dataset_root(), "exports")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


# ── Frame source (reuse senses' ambient capture; no second screenshotter) ─────
def _latest_frame_b64():
    try:
        import senses
        return senses._screen.get("latest_b64")
    except Exception:
        return None


def _save_b64_jpg(b64, path):
    try:
        if not b64:
            return False
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64))
        return True
    except Exception:
        return False


# ── Session lifecycle ────────────────────────────────────────────────────────
def start_session(appid=None, name=None):
    with _lock:
        sid = time.strftime("%Y%m%d-%H%M%S")
        d = os.path.join(_dataset_root(), sid)
        try:
            os.makedirs(os.path.join(d, "frames"), exist_ok=True)
        except Exception as e:
            log(f"[S32][PLAY] couldn't make dataset dir: {e}")
            return None
        _session.update({"id": sid, "dir": d, "appid": appid, "name": name, "step": 0})
        try:
            with open(os.path.join(d, "session.json"), "w", encoding="utf-8") as f:
                json.dump({"id": sid, "appid": appid, "name": name,
                           "started": time.time(), "label": None}, f, indent=2)
        except Exception:
            pass
    log(f"[S32][PLAY] capture session {sid} started ({name or appid})")
    return sid


def end_session():
    with _lock:
        sid, steps = _session["id"], _session["step"]
        _session.update({"id": None, "dir": None, "appid": None, "name": None, "step": 0})
    if sid:
        log(f"[S32][PLAY] capture session {sid} ended ({steps} steps)")


def _append(rec):
    d = _session["dir"]
    if not d:
        return
    try:
        with _lock, open(os.path.join(d, "steps.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ── Capture one (state, action) step ─────────────────────────────────────────
def record_step(action, ok=True):
    if not CONFIG.get("gameplay_capture", True):
        return
    if not state.get("s32_playing"):
        return
    if _session["id"] is None:
        start_session(state.get("s32_playing"))
    if not _session["dir"]:
        return
    before = _latest_frame_b64()
    with _lock:
        step = _session["step"]; _session["step"] += 1
        d = _session["dir"]
    brel = f"frames/{step:05d}_before.jpg"
    saved = _save_b64_jpg(before, os.path.join(d, brel))
    _append({"step": step, "ts": round(time.time(), 3), "action": action, "ok": ok,
             "before": brel if saved else None})

    def _grab_after():
        time.sleep(0.7)                         # let the action's effect land
        ab = _latest_frame_b64()
        arel = f"frames/{step:05d}_after.jpg"
        if _save_b64_jpg(ab, os.path.join(d, arel)):
            _append({"patch_after": step, "after": arel})

    threading.Thread(target=_grab_after, daemon=True).start()


# ── Labeling + listing ───────────────────────────────────────────────────────
def label_session(label, session=None):
    sid = session or _session["id"]
    if not sid:
        return {"ok": False, "error": "no session to label"}
    lab = label if label in ("good", "bad", None) else None
    meta_path = os.path.join(_dataset_root(), sid, "session.json")
    try:
        meta = {}
        if os.path.exists(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        meta["label"] = lab
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        return {"ok": True, "session": sid, "label": lab}
    except Exception as e:
        return {"ok": False, "error": str(e)[:100]}


def list_sessions():
    root = _dataset_root()
    out = []
    if os.path.isdir(root):
        for sid in sorted(os.listdir(root), reverse=True):
            d = os.path.join(root, sid)
            mp = os.path.join(d, "session.json")
            if not os.path.isdir(d) or not os.path.exists(mp):
                continue
            try:
                with open(mp, encoding="utf-8") as f:
                    m = json.load(f)
                steps = 0
                sp = os.path.join(d, "steps.jsonl")
                if os.path.exists(sp):
                    with open(sp, encoding="utf-8") as f:
                        steps = sum(1 for ln in f if '"action"' in ln)
                out.append({"id": sid, "name": m.get("name"), "appid": m.get("appid"),
                            "label": m.get("label"), "steps": steps})
            except Exception:
                continue
    return {"sessions": out, "active": _session["id"]}


def stats():
    s = list_sessions()["sessions"]
    return {"sessions": len(s),
            "good": sum(1 for x in s if x["label"] == "good"),
            "bad": sum(1 for x in s if x["label"] == "bad"),
            "total_steps": sum(x["steps"] for x in s)}


def reset():
    """Wipe all captured gameplay training data — every session and every export.
    Used by the 'Reset Gaming Training Data' button and the full blank-slate reset."""
    import shutil
    try:
        end_session()          # stop any in-progress capture before deleting
    except Exception:
        pass
    root = _dataset_root()
    removed = 0
    try:
        if os.path.isdir(root):
            for entry in os.listdir(root):
                p = os.path.join(root, entry)
                try:
                    if os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                    else:
                        os.remove(p)
                    removed += 1
                except Exception:
                    pass
    except Exception as e:
        return {"error": str(e)}
    log(f"[S32][GAMEPLAY] training data reset ({removed} item(s) removed)")
    return {"ok": True, "removed": removed}


# ── Export vision-action JSONL ───────────────────────────────────────────────
def _load_session_steps(d):
    steps, afters = {}, {}
    sp = os.path.join(d, "steps.jsonl")
    if not os.path.exists(sp):
        return []
    with open(sp, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except Exception:
                continue
            if "patch_after" in r:
                afters[r["patch_after"]] = r.get("after")
            elif "step" in r:
                steps[r["step"]] = r
    rows = []
    for st in sorted(steps):
        r = steps[st]
        r["after"] = afters.get(st)
        rows.append(r)
    return rows


def export(scope="good", inline_images=False):
    """Export (state→action) steps as JSONL.
      scope: 'good' (only sessions labeled good) | 'all'
      inline_images=False → reference image file paths (compact, portable)
      inline_images=True  → embed base64 (self-contained, large)"""
    root = _dataset_root()
    rows = []
    sessions_used = 0
    if os.path.isdir(root):
        for sid in sorted(os.listdir(root)):
            d = os.path.join(root, sid)
            mp = os.path.join(d, "session.json")
            if not os.path.isdir(d) or not os.path.exists(mp):
                continue
            try:
                with open(mp, encoding="utf-8") as f:
                    meta = json.load(f)
            except Exception:
                continue
            if scope == "good" and meta.get("label") != "good":
                continue
            used_this = False
            game = meta.get("name") or "a game"
            instr = (f"You are playing {game}. Look at the screen and choose the single "
                     f"best next action (press/hold/move/move_to/click).")
            for r in _load_session_steps(d):
                if not r.get("before"):
                    continue
                bpath = os.path.join(d, r["before"])
                if not os.path.exists(bpath):
                    continue
                if inline_images:
                    try:
                        with open(bpath, "rb") as f:
                            img = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
                    except Exception:
                        continue
                else:
                    img = os.path.abspath(bpath)
                rows.append({
                    "image": img,
                    "instruction": instr,
                    "action": r["action"],
                    "after": os.path.abspath(os.path.join(d, r["after"])) if r.get("after") else None,
                    "label": meta.get("label"),
                    "game": game,
                    "session": sid,
                })
                used_this = True
            if used_this:
                sessions_used += 1

    if not rows:
        return {"ok": False, "error": f"no steps to export (scope={scope}). "
                f"Label a session 'good' first, or use scope='all'.", "count": 0}
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(_exports_dir(), f"gameplay_{scope}_{ts}.jsonl")
    try:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
    except Exception as e:
        return {"ok": False, "error": str(e)[:120], "count": 0}
    log(f"[S32][PLAY] exported {len(rows)} steps from {sessions_used} session(s) → {path}")
    return {"ok": True, "count": len(rows), "sessions": sessions_used, "path": path}
