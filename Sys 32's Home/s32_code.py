# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_code.py — Inline chat programming.

The operator asks for something that does a job; it plans it, writes it, posts the
code in chat (with a Download button that opens a Save As dialog), runs Python to
test it, fixes what breaks, and keeps going until it works.

THE STORYBOARD (what makes this work across a long, interrupted job): every coding
project has a board on disk — goal, plan steps (done / doing / todo), decisions,
files + versions, the last run, and what's next. The ACTIVE board is shown to it at
the start of every turn (user side, never its own role), so it always knows where
it left off — after a tangent, a restart, or days later. Starting a different
project parks the current one; naming a parked project picks it back up.

Layout:  <home>\\workspace\\code\\<project>\\board.json
                                         \\<files it wrote>
                                         \\.versions\\<file>.v<N>
         <home>\\workspace\\code\\_active.json  → which project is active
Its PRIVATE workspace — the working copies it needs to run code and to remember the
project. Nothing reaches the operator's folders unless he presses Download on a card.

Runs: Python only, System 32's own interpreter, working folder = the project folder,
timeout, output captured. Code that touches files outside, deletes, runs programs or
uses the network waits on the operator's Confirm card (same gate as Unreal code).
"""

import os
import re
import sys
import json
import time
import hashlib
import subprocess

from s32_config import state, log, _script_dir, PERSON_DIR

MAX_RUN_ATTEMPTS = 5          # per file, per operator request
_RUN_TIMEOUT = 60
_RUNNABLE = (".py", ".bat", ".cmd")
_STATUS_MARK = {"done": "✓", "doing": "▶", "todo": "·"}


# ── storage ──────────────────────────────────────────────────────────────────
def _root():
    # A person's projects are theirs alone. (The old patches\\code location is moved
    # to the FIRST person by s32_config's migration — never to whoever codes first.)
    d = os.path.join(PERSON_DIR, "code")
    os.makedirs(d, exist_ok=True)
    return d


def chat_file(filename, content, purpose=""):
    """A file shown ONLY in chat (code card with Download / Copy) — nothing written to
    disk. Used when it writes a file without a location: the operator decides where it
    goes, if anywhere."""
    fn = _safe_name(filename)
    content = content if isinstance(content, str) else str(content or "")
    log(f"[S32][CODE] shown in chat (not saved): {fn} ({len(content.splitlines())} lines)")
    return {"type": "code_file", "project": "", "slug": "", "filename": fn, "version": 1,
            "purpose": purpose or "", "code": content, "ran": False, "chat_only": True, "ok": True}


def _slug(s, n=40):
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return s[:n] or "project"


def _safe_name(fn):
    fn = os.path.basename(str(fn or "").replace("\\", "/"))
    fn = re.sub(r'[<>:"|?*\x00-\x1f]', "_", fn).strip(" .")
    return fn or "script.py"


def _pdir(slug):
    return os.path.join(_root(), slug)


def _load(slug):
    try:
        with open(os.path.join(_pdir(slug), "board.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save(b):
    b["updated"] = time.strftime("%Y-%m-%d %H:%M")
    d = _pdir(b["slug"])
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "board.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(b, f, indent=2, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, "board.json"))


def _get_active():
    try:
        with open(os.path.join(_root(), "_active.json"), encoding="utf-8") as f:
            return json.load(f).get("active")
    except Exception:
        return None


def _set_active(slug):
    with open(os.path.join(_root(), "_active.json"), "w", encoding="utf-8") as f:
        json.dump({"active": slug}, f)


def _all_boards():
    out = []
    for name in sorted(os.listdir(_root())):
        if name.startswith("_"):
            continue
        b = _load(name)
        if b:
            out.append(b)
    return out


def _find(project):
    """Match a project by slug or loose name."""
    if not project:
        return None
    want = _slug(project)
    b = _load(want)
    if b:
        return b
    for b in _all_boards():
        if b.get("closed"):
            continue          # a finished project is only reopened by its exact name
        if want in b["slug"] or b["slug"] in want or _slug(b.get("name")) == want:
            return b
    return None


def _progress(b):
    steps = b.get("steps") or []
    done = sum(1 for s in steps if s.get("status") == "done")
    return f"{done}/{len(steps)} steps" if steps else "no plan yet"


# ── the board it sees every turn ─────────────────────────────────────────────
def board_note():
    """User-side context for its live turn: the active storyboard + parked projects."""
    try:
        act = _get_active()
        b = _load(act) if act else None
        parked = [x for x in _all_boards() if x["slug"] != act and not x.get("closed")]
    except Exception:
        return ""
    finished = state.get("code_closed_now")
    if not b and not parked and not finished:
        return ""
    lines = []
    if finished and not b:
        # Closing the board used to leave it with NO board and only "[Still working on:
        # make me a calculator]" → it started writing it all over again (2026-10-27).
        lines.append(f"[Coding project '{finished}' is FINISHED — you closed it this turn. The work is "
                     f"done: don't write or run it again. Tell the operator in a sentence or two what it "
                     f"does and that they can Download it from the card.]")
    if b and not b.get("closed"):
        lines.append(f"[Coding project ACTIVE: '{b['name']}' — {b.get('goal', '')[:200]}")
        for i, s in enumerate(b.get("steps") or [], 1):
            lines.append(f"  {_STATUS_MARK.get(s.get('status'), '·')} {i}. {s.get('text', '')[:140]}")
        files = b.get("files") or {}
        if files:
            lines.append("  Files: " + ", ".join(f"{k} (v{v.get('version', 1)})" for k, v in files.items()))
        # WHERE the project lives + what was imported (2026-10-29: next session she knew the
        # project's name but not its folder — list_directory('FRIK pipboy gaze power') → Not found,
        # then she wandered around 'home' until the repeat breaker stopped her).
        lines.append(f"  Folder: {_pdir(b['slug'])}  (your files; read-only copies of imported code are in "
                     f"its original\\ subfolder)")
        _ims = b.get("imports") or ([{"source": b["source"], "folder": os.path.join(_pdir(b["slug"]), "original")}]
                                    if b.get("source") else [])      # boards from before 'imports' existed
        for im in _ims[-4:]:
            _f = im.get("folder", "")
            _rels = []
            try:                                   # the actual copies, so she can read_file them directly
                for d_, _s, fns in os.walk(_f):
                    _s[:] = [x for x in _s if os.path.normcase(os.path.join(d_, x)) not in
                             {os.path.normcase(o.get("folder", "")) for o in _ims if o is not im}]
                    for fn in fns:
                        _rels.append(os.path.relpath(os.path.join(d_, fn), _f))
                    if len(_rels) > 15:
                        break
            except Exception:
                pass
            lines.append(f"  Imported: {im.get('source', '')} → {_f}"
                         + (f"  (compiled, not readable: {', '.join(im['compiled'][:4])})" if im.get("compiled") else ""))
            if _rels:
                lines.append("    files: " + ", ".join(_rels[:15]) + (" …" if len(_rels) > 15 else ""))
        lr = b.get("last_run")
        if lr:
            lines.append(f"  Last run: {lr.get('file')} — {'ran clean' if lr.get('ok') else 'FAILED: ' + lr.get('summary', '')[:160]}")
        for n in (b.get("notes") or [])[-4:]:
            lines.append(f"  Decision: {n[:160]}")
        if b.get("verdict"):
            lines.append(f"  Operator verdict: {b['verdict']}")
        if b.get("next"):
            lines.append(f"  Next: {b['next'][:200]}")
        lines.append("  This board is your storyboard — keep it current with code_plan (mark steps "
                     "done, set next). If the operator asks for something unrelated, start a NEW "
                     "project with code_plan; this one is parked and resumes by name.]")
    if parked:
        lines.append("[Parked coding projects (resume by calling code_plan with the name): " +
                     "; ".join(f"'{x['name']}' ({_progress(x)})" for x in parked[:6]) + "]")
    return "\n".join(lines)


def _board_card(b, event=""):
    return {"type": "code_board", "name": b["name"], "goal": b.get("goal", ""),
            "steps": b.get("steps") or [], "next": b.get("next", ""), "event": event,
            "files": list((b.get("files") or {}).keys())}


def _push(payload):
    try:
        from s32_unreal import _push_card
        _push_card(payload)
    except Exception as e:
        log(f"[S32][CODE] card push failed: {e}")


# ── tool: code_plan ───────────────────────────────────────────────────────────
def code_plan(project, goal=None, steps=None, done=None, doing=None, note=None, next=None,
              close=False, _card=True):
    """Create, update, resume, or close a coding project's storyboard."""
    if not project:
        return {"error": "Give the project a short name (e.g. 'csv cleaner')."}
    b = _find(project)
    event = "updated"
    _before = json.dumps({k: (b or {}).get(k) for k in ("goal", "steps", "notes", "next", "closed")},
                         sort_keys=True) if b else None
    if b and b.get("closed") and goal and not close:
        # A NEW plan under a finished project's name ("batch calculator" again, 2026-10-27):
        # reopening it dragged in the old files/last run and it spent two calls untangling
        # "outdated" state. Move the old one aside and start clean.
        old_slug = b["slug"]
        new_slug = f"{old_slug}_{(b.get('created') or '').split(' ')[0].replace('-', '') or 'old'}"
        i = 2
        while os.path.exists(_pdir(new_slug)):
            new_slug = f"{old_slug}_old{i}"; i += 1
        try:
            os.replace(_pdir(old_slug), _pdir(new_slug))
            ob = _load(new_slug)
            if ob:
                ob["slug"], ob["name"] = new_slug, f"{ob['name']} ({(ob.get('created') or 'earlier').split(' ')[0]})"
                _save(ob)
            log(f"[S32][CODE] archived finished '{b['name']}' as '{new_slug}' — starting fresh")
            b, _before = None, None
        except Exception as e:
            log(f"[S32][CODE] couldn't archive '{old_slug}': {e}")
    if not b:
        slug = _slug(project)
        b = {"name": str(project)[:60], "slug": slug, "goal": "", "steps": [], "notes": [],
             "files": {}, "next": "", "created": time.strftime("%Y-%m-%d %H:%M")}
        event = "started"
    prev = _get_active()
    if prev and prev != b["slug"]:
        pb = _load(prev)
        if pb and not pb.get("closed"):
            event = event if event == "started" else "resumed"
            log(f"[S32][CODE] parked '{pb['name']}' ({_progress(pb)})")
    if goal:
        b["goal"] = str(goal)[:500]
    if steps:
        if isinstance(steps, str):
            steps = [s.strip(" -•") for s in steps.splitlines() if s.strip()]
        old = {s.get("text"): s.get("status") for s in b.get("steps") or []}
        b["steps"] = [{"text": str(t)[:200], "status": old.get(str(t)[:200], "todo")}
                      for t in steps if str(t).strip()][:20]
    for key, status in (("done", "done"), ("doing", "doing")):
        val = {"done": done, "doing": doing}[key]
        if val is None:
            continue
        nums = val if isinstance(val, list) else [val]
        for n in nums:
            try:
                i = int(n) - 1
                if 0 <= i < len(b["steps"]):
                    if status == "doing":
                        for s in b["steps"]:
                            if s["status"] == "doing":
                                s["status"] = "todo"
                    b["steps"][i]["status"] = status
            except (TypeError, ValueError):
                pass
    if note:
        b.setdefault("notes", []).append(str(note)[:300])
        b["notes"] = b["notes"][-12:]
    if next is not None:
        b["next"] = str(next)[:300]
    if close:
        b["closed"] = True
        event = "closed"
    else:
        b.pop("closed", None)
    # It re-planned the same board 4× in a row instead of writing code (2026-10-27).
    # An update that changes nothing — or a second plan call with no write_code since —
    # gets a firm nudge instead of another card.
    _after = json.dumps({k: b.get(k) for k in ("goal", "steps", "notes", "next", "closed")},
                        sort_keys=True)
    plans = state.setdefault("code_plan_streak", 0)
    if event == "updated" and b["slug"] == _get_active() and (_after == _before or plans >= 1):
        state["code_plan_streak"] = plans + 1
        log(f"[S32][CODE] code_plan repeated without new work ('{b['name']}') — nudging")
        if _after != _before:
            _save(b)
        return {"ok": True, "project": b["name"], "event": "unchanged", "progress": _progress(b),
                "next": b.get("next", ""), "quiet": True, "nudge": True}
    state["code_plan_streak"] = plans + 1
    _save(b)
    _set_active(None if close else b["slug"])
    state["code_project"] = None if close else b["slug"]
    state["code_closed_now"] = b["name"] if close else None
    state["code_closed_after"] = 0
    log(f"[S32][CODE] board {event}: '{b['name']}' ({_progress(b)})")
    if _card:
        _push(_board_card(b, event))
    return {"ok": True, "project": b["name"], "event": event, "progress": _progress(b),
            "next": b.get("next", ""), "quiet": True}


# ── tool: write_code (+ optional run) ─────────────────────────────────────────
def _active_or(project, filename="", purpose=""):
    b = _find(project) if project else None
    if not b:
        act = _get_active()
        b = _load(act) if act else None
    if not b:
        # A quick one-off with no plan: name the project after the file (not "untitled")
        # and skip the board card — a one-file job doesn't need a storyboard on screen.
        name = project or os.path.splitext(_safe_name(filename))[0].replace("_", " ") or "script"
        code_plan(name, goal=purpose or f"write {_safe_name(filename)}", _card=False)
        b = _find(name)
    return b


def write_code(project, filename, code, purpose="", run=False, input=""):
    code = (code or "").rstrip() + "\n"
    if not code.strip():
        return {"error": "No code was given — pass the complete file contents as `code`."}
    if state.get("code_closed_now") and not project:
        # It closed the project and then started rewriting it (2026-10-27, twice in one
        # turn). Once is a reminder; a second time ends the turn.
        n = state["code_closed_after"] = state.get("code_closed_after", 0) + 1
        done = state["code_closed_now"]
        if n >= 2:
            return {"ok": False, "halt": True, "quiet": True,
                    "halt_msg": f"('{done}' is finished — it's on the card above, ready to Download.)",
                    "error": "Project already finished."}
        return {"ok": False, "quiet": True, "error":
                f"Nothing written — you already FINISHED and closed '{done}' this turn; it ran clean and "
                f"is on the card above. Reply to the operator now: what it does and that they can "
                f"Download it. Don't call any more tools."}
    b = _active_or(project, filename, purpose)
    _set_active(b["slug"])
    state["code_plan_streak"] = 0
    fn = _safe_name(filename)
    d = _pdir(b["slug"])
    path = os.path.join(d, fn)
    info = b.setdefault("files", {}).get(fn) or {"version": 0}
    _orig_rel = info.get("orig_rel")
    if os.path.isfile(path):
        os.makedirs(os.path.join(d, ".versions"), exist_ok=True)
        try:
            with open(path, encoding="utf-8") as f:
                prev = f.read()
            with open(os.path.join(d, ".versions", f"{fn}.v{info['version']}"), "w", encoding="utf-8") as f:
                f.write(prev)
        except Exception:
            pass
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(code)
    info = {"version": int(info.get("version", 0)) + 1, "purpose": str(purpose)[:200],
            "updated": time.strftime("%H:%M")}
    if _orig_rel:
        info["orig_rel"] = _orig_rel
    b["files"][fn] = info
    _save(b)
    state["code_mode"] = True
    log(f"[S32][CODE] wrote {b['slug']}/{fn} v{info['version']} ({len(code.splitlines())} lines)")
    card = {"type": "code_file", "project": b["name"], "slug": b["slug"], "filename": fn,
            "version": info["version"], "purpose": purpose, "code": code, "path": path}
    _raw = filename if ("/" in str(filename).replace("\\", "/") or not _orig_rel) else _orig_rel
    _vs = _diff_vs_original(b, _raw, fn, path, info["version"])
    if _vs:
        card["vs_original"] = _vs
        if _vs.get("original") and _vs.get("original") != _orig_rel:
            b = _load(b["slug"])                    # remember which original this file is a copy of
            b.setdefault("files", {}).setdefault(fn, {})["orig_rel"] = _vs["original"]
            _save(b)
    if run and fn.lower().endswith(_RUNNABLE):
        res = run_code(b["slug"], fn, input=input, _card=card)
        return res
    card["ran"] = False
    return {**card, "ok": True}


# ── tool: run_code ───────────────────────────────────────────────────────────
def _diff_vs_original(b, raw_name, fn, path, version):
    """A changed copy of an imported file ('Pipboy.cpp' / 'src/pipboy/Pipboy.cpp') → a diff card
    against that original, so the operator sees exactly what she changed. None if it's new."""
    orig_root = os.path.join(_pdir(b["slug"]), "original")
    if not os.path.isdir(orig_root):
        return None
    want = str(raw_name or fn).replace("\\", "/").lower().lstrip("./")
    cands = []
    for d, dirs, fns in os.walk(orig_root):
        dirs[:] = [x for x in dirs if x.lower() not in _IMPORT_SKIP_DIRS]
        for f in fns:
            if f.lower() == fn.lower():
                rel = os.path.relpath(os.path.join(d, f), orig_root).replace("\\", "/")
                cands.append(rel)
        if len(cands) > 20:
            break
    if not cands:
        return None
    if len(cands) > 1:
        ends = []
        if "/" in want:
            ends = ([c for c in cands if c.lower() == want]
                    or [c for c in cands if c.lower().endswith("/" + want)]
                    or sorted((c for c in cands if want.endswith("/" + c.lower())), key=len, reverse=True)[:1])
        if len(ends) != 1:
            return {"ambiguous": cands[:6]}
        cands = ends
    rel = cands[0]
    try:
        r = compare_code(os.path.join(orig_root, *rel.split("/")), path, "original", f"yours v{version}")
    except Exception as e:
        log(f"[S32][CODE] diff vs original failed: {e}")
        return None
    if r.get("error"):
        return None
    _push(r)                                             # the operator sees the change, line by line
    d = r.get("diff", "") or ""
    head = d.splitlines()[0] if d else ("identical" if r.get("identical") else "")
    return {"original": rel, "stat": head, "identical": bool(r.get("identical"))}


def _attempts():
    return state.setdefault("code_attempts", {})


def new_request():
    """Called at the start of every operator turn: fresh attempt budget, and code-friendly
    sampling if a coding project is active."""
    state["code_attempts"] = {}
    state["code_approved"] = {}
    state["code_closed_now"] = None
    state["code_plan_streak"] = 0
    if _get_active():
        state["code_mode"] = True


def _hash(code):
    return hashlib.sha1(re.sub(r"\s+", " ", code or "").strip().encode("utf-8")).hexdigest()[:12]


_SET_RE = re.compile(r'^\s*@?set\s+(?:/a\s+|/p\s+)?"?([A-Za-z_][\w.]*)\s*[-+*/%&|^]?=', re.I)
_SETA_MULTI = re.compile(r',\s*([A-Za-z_][\w.]*)\s*[-+*/%&|^]?=')


def _bat_stale_vars(code):
    """cmd expands %var% for a whole ( … ) block when it READS the block, before any line in it
    runs. So a variable set inside a block and read as %var% in that same block always shows
    the value from BEFORE the block — the 15 / 3 = 42 bug (2026-10-27): the division result
    printed the previous multiply's answer. Returns [(var, line_no), …] for every such read."""
    found, depth, block = [], 0, None        # block: {"sets": set(), "reads": [(var, ln)]}
    for ln, raw in enumerate(code.splitlines(), 1):
        line = raw.strip()
        if not line or re.match(r'(?i)^(rem\b|::)', line):
            continue
        bare = re.sub(r'"[^"]*"', '""', line)
        opens, closes = bare.count("("), bare.count(")")
        if depth == 0 and opens > closes:
            block = {"sets": set(), "reads": []}
        if block is not None:
            # A line like `set /a result=%num1% / %num2%` — the reads on it are of other vars.
            for stmt in re.split(r'\s*(?:&&?|\|\|)\s*|\(\s*|\)\s*', line):
                m = _SET_RE.match(stmt)
                if m:
                    block["sets"].add(m.group(1).lower())
                    if re.match(r'(?i)^\s*@?set\s+/a', stmt):
                        block["sets"].update(v.lower() for v in _SETA_MULTI.findall(stmt))
            for v in re.findall(r'%([A-Za-z_][\w.]*)%', line):
                block["reads"].append((v, ln))
        depth = max(0, depth + opens - closes)
        if depth == 0 and block is not None:
            seen = set()
            for v, l in block["reads"]:
                if v.lower() in block["sets"] and (v.lower(), l) not in seen:
                    seen.add((v.lower(), l))
                    found.append((v, l))
            block = None
    return found


def _lint(fn, code):
    """Bugs we can prove from the source alone. Returns a message, or ''."""
    if not fn.lower().endswith((".bat", ".cmd")):
        return ""
    stale = _bat_stale_vars(code)
    if not stale:
        return ""
    where = ", ".join(f"%{v}% on line {l}" for v, l in stale[:6])
    names = sorted({v for v, _ in stale})
    return ("BUG (found by checking the code — the run itself didn't crash): " + where + " — "
            + ("that variable is" if len(names) == 1 else "those variables are")
            + " SET inside the same ( … ) block, and cmd fills in %var% for the whole block before "
              "it runs, so it prints the OLD value from before the block. Use "
            + ", ".join(f"!{v}!" for v in names)
            + " there instead (setlocal enabledelayedexpansion must be on), or move the work out of "
              "the block with goto labels.")


def run_code(project, filename, args="", input="", _card=None):
    b = _find(project) if project else None
    if not b:
        act = _get_active()
        b = _load(act) if act else None
    if not b:
        return {"error": "No coding project is active — write the file with write_code first."}
    state["code_plan_streak"] = 0
    fn = _safe_name(filename)
    path = os.path.join(_pdir(b["slug"]), fn)
    if not os.path.isfile(path):
        return {"error": f"{fn} isn't in project '{b['name']}' — files: "
                         f"{', '.join((b.get('files') or {}).keys()) or 'none yet'}."}
    if not fn.lower().endswith(_RUNNABLE):
        return {"error": f"I can only run Python (.py) and batch (.bat/.cmd) files — {fn} is saved and ready "
                         f"to download."}
    with open(path, encoding="utf-8") as f:
        code = f.read()
    card = dict(_card or {"type": "code_file", "project": b["name"], "slug": b["slug"],
                          "filename": fn, "version": (b.get("files") or {}).get(fn, {}).get("version", 1),
                          "purpose": "", "code": code, "path": path})
    key = f"{b['slug']}/{fn}"
    hist = _attempts().setdefault(key, [])
    n = len(hist) + 1
    if n > MAX_RUN_ATTEMPTS:
        if any(h.get("stop_told") for h in hist):
            return {"ok": False, "halt": True, "quiet": True,
                    "error": "Out of run attempts for this file — stop and report to the operator."}
        hist[-1]["stop_told"] = True
        return {**card, "ok": False, "ran": True, "stopped": True, "attempt": n - 1,
                "max": MAX_RUN_ATTEMPTS,
                "error": f"Stopped after {MAX_RUN_ATTEMPTS} runs. Last error: {hist[-1].get('error', '')[-300:]}"}
    h = _hash(code)
    for a in hist:
        if a["hash"] == h and not a["ok"]:
            return {**card, "ok": False, "ran": True, "duplicate": True, "attempt": a["n"],
                    "max": MAX_RUN_ATTEMPTS,
                    "error": f"This is the SAME code as run {a['n']}, which failed with: "
                             f"{a.get('error', '')[-300:]} — change the code before running again."}

    # Risk gate (shared with Unreal). Approval sticks for this file this request unless the
    # new version does something riskier than what was approved.
    try:
        from s32_unreal import risky_reasons, _ask_operator
        reasons = risky_reasons(code)
    except Exception:
        reasons, _ask_operator = [], None
    approved = state.setdefault("code_approved", {}).get(key, set())
    if reasons and not set(reasons) <= approved:
        if not (_ask_operator and _ask_operator(code, reasons, card.get("purpose") or fn, n, where="Code")):
            hist.append({"n": n, "hash": h, "ok": False, "error": "Operator declined to run it.", "code": code})
            return {**card, "ok": False, "ran": True, "declined": True, "attempt": n,
                    "max": MAX_RUN_ATTEMPTS, "reasons": reasons,
                    "error": "The operator declined to run this (it " + "; ".join(reasons) +
                             "). Don't run it as-is — ask what they'd like instead, or leave it for "
                             "them to download and run themselves." +
                             (" To copy, move, rename or delete files, use the file_operation tool instead "
                              "(native, with a Confirm card) — not a script."
                              if any("files on disk" in r for r in reasons) else "")}
        state["code_approved"][key] = approved | set(reasons)

    t0 = time.time()
    extra = [a for a in str(args).split()] if args else []
    if fn.lower().endswith((".bat", ".cmd")):
        if os.name != "nt":
            return {"error": "Batch files only run on Windows."}
        cmd = ["cmd", "/d", "/c", fn] + extra
    else:
        cmd = [sys.executable, "-X", "utf8", fn] + extra
    feed = str(input or "")
    if feed and not feed.endswith("\n"):
        feed += "\n"
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
        # Scripted keyboard input: an interactive program (menus, set /p, input()) gets
        # its answers from `input`; past the end it reads EOF. Interactive runs get a short
        # timeout — a menu that loops forever on empty input shouldn't hold the turn 60s.
        # 10s, not 20: a hung menu cost 20s per failed run (3 in one turn, 2026-10-27).
        tmo = 10 if feed or fn.lower().endswith((".bat", ".cmd")) else _RUN_TIMEOUT
        # Feed the input from a FILE, not a pipe: cmd's `set /p` misreads piped input
        # (every prompt came back empty and the menu looped until the timeout, 2026-10-27);
        # a file is read line by line, exactly like `calc.bat < answers.txt`.
        import tempfile
        with tempfile.TemporaryFile("w+b") as fin:
            _nl = "\r\n" if fn.lower().endswith((".bat", ".cmd")) else "\n"   # cmd wants CRLF lines
            fin.write(feed.replace("\r\n", "\n").replace("\n", _nl).encode("utf-8"))
            fin.seek(0)
            p = subprocess.run(cmd, cwd=_pdir(b["slug"]), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=tmo,
                               stdin=fin, **kw)
        out, err, rc = p.stdout or "", p.stderr or "", p.returncode
        timed_out = False
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (f"Stopped — the program didn't finish in time. Look at the output: if the same menu "
               f"repeats with empty answers, the program isn't reading the scripted input the way "
               f"you expect (check which prompts consume a line — every set /p, input() and pause "
               f"takes one); otherwise it's an endless loop. The input must end with the quit choice.")
        rc, timed_out = -1, True
    secs = round(time.time() - t0, 1)
    ok = (rc == 0) and not timed_out
    tb = err.strip() if not ok else ""
    lint = _lint(fn, code)
    if lint:
        # A program that runs but is provably wrong isn't "clean" — count it as a failed run
        # so the card shows it and it fixes it instead of closing.
        ok = False
        tb = (tb + "\n" if tb else "") + lint
    hist.append({"n": n, "hash": h, "ok": ok, "error": tb, "code": code})
    b["last_run"] = {"file": fn, "ok": ok, "summary": (tb.splitlines() or ["ran clean"])[-1][:200]}
    _save(b)
    log(f"[S32][CODE] run {key} attempt {n} {'OK' if ok else 'FAILED'} in {secs}s"
        + ("" if ok else f" — {(tb.splitlines() or [''])[-1][:120]}"))

    if ok and len(hist) > 1:
        try:
            from s32_unreal import _corpus
            for a in hist[:-1]:
                if not a["ok"] and a.get("error") != "Operator declined to run it.":
                    _corpus("code_repairs.jsonl",
                            {"project": b["name"], "goal": b.get("goal", ""), "file": fn,
                             "failed_code": a["code"], "error": a["error"], "fixed_code": code,
                             "attempts_to_fix": n - a["n"]})
        except Exception:
            pass
    shown = out.strip() + ("\n" + err.strip() if ok and err.strip() else "")
    if timed_out and len(shown) > 1500:
        shown = shown[:700] + "\n… (output repeated until the time limit) …\n" + shown[-600:]
    return {**card, "ok": ok, "ran": True, "attempt": n, "max": MAX_RUN_ATTEMPTS,
            "output": shown[-4000:],
            "traceback": tb[-4000:], "seconds": secs}


def record_verdict(slug, filename, works, note=""):
    b = _load(_slug(slug))
    if not b:
        return {"error": "Unknown project."}
    fn = _safe_name(filename)
    try:
        with open(os.path.join(_pdir(b["slug"]), fn), encoding="utf-8") as f:
            code = f.read()
    except Exception:
        code = ""
    try:
        from s32_unreal import _corpus
        _corpus("code_verdicts.jsonl", {"project": b["name"], "goal": b.get("goal", ""), "file": fn,
                                        "verdict": "works" if works else "not_right",
                                        "note": (note or "").strip()[:1000], "code": code})
    except Exception:
        pass
    try:
        from s32_training import label_link
        label_link(f"code:{b['slug']}/{fn}", works, "code_verdict", note)
    except Exception:
        pass
    b["verdict"] = ("✓ works" if works else "✗ not right" + (f" — {note.strip()[:200]}" if note.strip() else "")) + f" ({fn})"
    _save(b)
    msg = f"[Operator verdict on {fn} in '{b['name']}': {b['verdict']}]"
    for m in reversed(state.get("messages") or []):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] += "\n" + msg
            break
    log(f"[S32][CODE] verdict {b['slug']}/{fn}: {b['verdict']}")
    return {"ok": True}


def read_project_file(slug, filename):
    """For the Download button when the card is old/collapsed: the current file on disk."""
    b = _load(_slug(slug))
    if not b:
        return None
    p = os.path.join(_pdir(b["slug"]), _safe_name(filename))
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


# ── tool: import_code (2026-10-29) ────────────────────────────────────────────
# "I have a VR Skyrim mod for Vive Focus 3 controllers — grab that code and adapt it for
# Fallout 4 VR." The operator's original is only ever READ (opened 'rb'); its text/code
# files are copied into a new project of hers (<project>\original\…), she studies the
# copy, and writes the adapted version as new files with write_code. Compiled/binary files
# (.dll, .pex, .esp, images…) aren't copied — they're listed so she knows what's there.
_IMPORT_MAX_FILES = 1500
_IMPORT_MAX_FILE = 2 * 1024 * 1024        # 2 MB per file
_IMPORT_MAX_TOTAL = 60 * 1024 * 1024      # 60 MB per import
_IMPORT_SKIP_DIRS = {".git", ".svn", ".hg", "node_modules", "__pycache__", ".vs", ".idea",
                     "bin", "obj", "build", "dist", ".venv", "venv", "site-packages", "x64", "x86"}
_BINARY_EXTS = {".dll", ".exe", ".pex", ".esp", ".esm", ".esl", ".bsa", ".ba2", ".pdb", ".lib", ".obj",
                ".o", ".a", ".so", ".dylib", ".png", ".jpg", ".jpeg", ".dds", ".tga", ".bmp", ".gif",
                ".nif", ".hkx", ".wav", ".mp3", ".ogg", ".xwm", ".fuz", ".zip", ".7z", ".rar", ".pak",
                ".uasset", ".umap", ".fbx", ".glb", ".blend", ".mp4", ".mkv", ".ttf", ".otf", ".swf"}


def _is_text(path, size):
    ext = os.path.splitext(path)[1].lower()
    if ext in _BINARY_EXTS or size > _IMPORT_MAX_FILE:
        return False
    try:
        with open(path, "rb") as f:                 # READ-ONLY
            head = f.read(4096)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        try:
            head.decode("cp1252")
            return True
        except UnicodeDecodeError:
            return False


_GH_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
                    r"(?:/(?:tree|blob)/([^/?#]+)(/[^?#]*)?)?/?(?:[?#].*)?$", re.I)
_GH_MAX_ZIP = 80 * 1024 * 1024           # a source download larger than this is refused
_COMPILED_EXTS = {".dll", ".exe", ".so", ".dylib"}


def _github_fetch(url, work):
    """Download a PUBLIC GitHub repo's source zip (read-only, nothing is run) and unpack it
    into `work`. Returns (folder_to_import, repo_name, error)."""
    m = _GH_RE.match(url.strip())
    if not m:
        return None, None, "Only public GitHub repository URLs can be imported (https://github.com/owner/repo)."
    owner, repo, ref, sub = m.group(1), m.group(2), m.group(3) or "HEAD", (m.group(4) or "").strip("/")
    import requests, zipfile, shutil
    try:
        from s32_web import _proxies
        proxies = _proxies()
    except Exception:
        proxies = None
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    zpath = os.path.join(work, "src.zip")
    zurl = f"https://codeload.github.com/{owner}/{repo}/zip/{ref}"
    log(f"[S32][CODE] downloading source of github.com/{owner}/{repo} ({ref}) — read-only")
    try:
        with requests.get(zurl, stream=True, timeout=(15, 120), proxies=proxies) as r:
            if r.status_code == 404:
                return None, repo, (f"github.com/{owner}/{repo} wasn't found (or it's private / the branch "
                                    f"'{ref}' doesn't exist). Check the URL with web_search.")
            if r.status_code >= 400:
                return None, repo, f"GitHub answered HTTP {r.status_code} for {owner}/{repo}."
            got = 0
            with open(zpath, "wb") as f:
                for part in r.iter_content(256 * 1024):
                    got += len(part)
                    if got > _GH_MAX_ZIP:
                        return None, repo, f"{owner}/{repo} is over {_GH_MAX_ZIP // 2**20} MB — too big to import."
                    f.write(part)
        out = os.path.join(work, "src")
        base_out = os.path.realpath(out)
        with zipfile.ZipFile(zpath) as z:
            for info in z.infolist():
                dest = os.path.realpath(os.path.join(out, info.filename))
                if not (dest == base_out or dest.startswith(base_out + os.sep)):
                    continue                                   # zip-slip: never write outside
                if info.is_dir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as fi, open(dest, "wb") as fo:
                    fo.write(fi.read())
        os.remove(zpath)
    except Exception as e:
        return None, repo, f"Couldn't download {owner}/{repo}: {str(e)[:160]}"
    tops = [d for d in os.listdir(out) if os.path.isdir(os.path.join(out, d))]
    root = os.path.join(out, tops[0]) if len(tops) == 1 else out       # codeload wraps in repo-ref/
    if sub:
        root = os.path.join(root, *sub.split("/"))
        if not os.path.exists(root):
            return None, repo, f"'{sub}' isn't in {owner}/{repo}."
    return root, repo, None


def import_code(path, project="", goal=""):
    raw = str(path or "").strip().strip('"')
    if re.match(r"^(?:https?://)?(?:www\.)?github\.com/", raw, re.I):
        return _import_github(raw, project, goal)
    return _import_local(raw, project, goal)


def _import_github(url, project, goal):
    import shutil
    m = _GH_RE.match(url)
    repo = m.group(2) if m else "repo"
    work = os.path.join(_root(), "_download_" + hashlib.md5(url.encode()).hexdigest()[:8])
    try:
        folder, repo, err = _github_fetch(url, work)
        if err:
            return {"error": err}
        r = _import_local(folder, project or repo, goal, sub=repo, shown_source=url)
        return r
    finally:
        shutil.rmtree(work, ignore_errors=True)                 # only the extracted copy is kept


_SETTINGS_MAX = 40
_SETTINGS_BYTES = 5 * 1024 * 1024
_NAME_GENERIC = {"mod", "mods", "the", "for", "and", "main", "files", "file", "data", "plugin", "plugins",
                 "version", "update", "fix", "patch", "src", "source", "scripts", "vr", "fo4", "f4se", "skse"}


def _mod_name_words(src):
    """'FRIK 79.0 53464 v0.79.0 2026-09-17T00-46Z gXceq17Q1' → ['frik']  (Nexus/Vortex folder
    names carry version + ids after the name)."""
    base = os.path.basename(str(src).rstrip("\\/"))
    words = []
    for w in re.split(r"[\s_\-.()\[\]]+", base):
        lw = w.lower()
        if re.fullmatch(r"v?\d[\w.]*", lw) or len(lw) < 3 or lw in _NAME_GENERIC:
            if words:
                break                          # the name ends where the version/ids start
            continue
        if re.search(r"\d", lw) and words:
            break
        words.append(lw)
    return words[:3]


def _related_settings(src, dest_root):
    """Settings files that belong to this mod but live OUTSIDE its folder (2026-10-29: FRIK's
    FRIK.toml is in Documents\\My Games\\Fallout4VR\\FRIK_Config — she found the folder and never
    opened it). Found with the file index; copied read-only into original\\_settings\\<drive>\\…"""
    words = _mod_name_words(src)
    if not words:
        return []
    try:
        import s32_findex
        if not s32_findex.ready():
            return []
        hits = s32_findex.search(" ".join(words), "config", limit=200)
    except Exception as e:
        log(f"[S32][CODE] settings lookup failed: {e}")
        return []
    srcn = os.path.normcase(os.path.abspath(src)).rstrip("\\/") + os.sep
    out, total = [], 0
    for h in hits:
        p = h.get("path") or ""
        pn = os.path.normcase(os.path.abspath(p))
        if pn.startswith(srcn) or not os.path.isfile(p):
            continue                                       # inside the mod folder → already copied
        low = pn.lower()
        if not all(w in low for w in words):
            continue                                       # the mod's NAME must be in the path
        try:
            size = os.path.getsize(p)
        except OSError:
            continue
        if size > _IMPORT_MAX_FILE or total + size > _SETTINGS_BYTES:
            continue
        drive, rest = os.path.splitdrive(os.path.abspath(p))
        parts = [drive.rstrip(":\\/") or "root"] + [x for x in re.split(r"[\\/]+", rest) if x]
        out_p = os.path.join(dest_root, "_settings", *parts)
        try:
            os.makedirs(os.path.dirname(out_p), exist_ok=True)
            with open(p, "rb") as fi, open(out_p, "wb") as fo:   # read the original, write OUR copy
                fo.write(fi.read())
        except OSError:
            continue
        total += size
        out.append({"original": p, "copy": out_p, "size": size})
        if len(out) >= _SETTINGS_MAX:
            break
    if out:
        log(f"[S32][CODE] settings files for {words}: {len(out)} copied from outside the mod folder")
    return out


def _import_local(raw, project="", goal="", sub="", shown_source=""):
    src = os.path.expandvars(os.path.expanduser(raw))
    if not src or not os.path.exists(src):
        return {"error": f"Not found: {src or '(no path)'} — find it first with search_computer."}
    src = os.path.abspath(src)
    is_dir = os.path.isdir(src)
    name = project or (os.path.basename(src.rstrip("\\/")) if is_dir else os.path.splitext(os.path.basename(src))[0])
    name = f"{name}"[:50] or "imported code"
    # A new project (or the named one) — the board shows the goal.
    shown = shown_source or src
    _streak = state.get("code_plan_streak", 0)
    if _find(name):
        res = None                       # importing into her existing project — leave its board alone
    else:
        res = code_plan(name, goal=goal or f"Study and adapt the code from {shown}", _card=True)
    state["code_plan_streak"] = _streak  # an import isn't a re-plan (it tripped the nudge, 2026-10-29)
    b = _find(name)
    if not b:
        return {"error": "Couldn't create the project for the import."}
    dest_root = os.path.join(_pdir(b["slug"]), "original", *( [sub] if sub else [] ))
    files = []
    if is_dir:
        for d, dirs, fnames in os.walk(src):
            dirs[:] = [x for x in dirs if x.lower() not in _IMPORT_SKIP_DIRS]
            for fn in fnames:
                files.append(os.path.join(d, fn))
                if len(files) > _IMPORT_MAX_FILES * 4:
                    break
    else:
        files = [src]
    copied, skipped, too_many, total, compiled = [], {}, 0, 0, []
    base = src if is_dir else os.path.dirname(src)
    for fp in files:
        try:
            size = os.path.getsize(fp)
        except OSError:
            continue
        rel = os.path.relpath(fp, base)
        if not _is_text(fp, size):
            ext = os.path.splitext(fp)[1].lower() or "(no extension)"
            skipped[ext] = skipped.get(ext, 0) + 1
            if ext in _COMPILED_EXTS and len(compiled) < 10:
                compiled.append(rel.replace("\\", "/"))
            continue
        if len(copied) >= _IMPORT_MAX_FILES or total + size > _IMPORT_MAX_TOTAL:
            too_many += 1
            continue
        out = os.path.join(dest_root, rel)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(fp, "rb") as fi, open(out, "wb") as fo:        # read the original, write OUR copy
            fo.write(fi.read())
        copied.append({"file": rel.replace("\\", "/"), "size": size, "copy": out})
        total += size
    settings = [] if shown_source else _related_settings(src, dest_root)   # local mods only
    b = _load(b["slug"])
    _note = (f"Imported {len(copied)} text/code file(s) from {shown} (read-only) "
             f"into original\\. Write the adapted version as NEW files.")
    b["notes"] = [n for n in b.get("notes", []) if n != _note] + [_note]     # a re-import doesn't repeat it
    b["notes"] = b["notes"][-12:]
    b["source"] = shown
    b["imports"] = [x for x in (b.get("imports") or []) if x.get("folder") != dest_root][-5:] + [
        {"source": shown, "folder": dest_root, "copied": len(copied), "compiled": compiled[:6]}]
    _save(b)
    log(f"[S32][CODE] imported {len(copied)} file(s) ({total // 1024} KB) from {shown} into '{b['name']}'"
        + (f"; not copied: {sum(skipped.values())} binary" if skipped else ""))
    return {"type": "code_import", "project": b["name"], "slug": b["slug"], "source": shown,
            "compiled": compiled, "settings": settings,
            "copied": len(copied), "bytes": total, "files": copied[:200],
            "binary": dict(sorted(skipped.items(), key=lambda x: -x[1])[:15]),
            "over_limit": too_many, "folder": dest_root, "ok": True}


# ── compare_code: read-only diff of two files or folders ─────────────────────────────
# Uses: does the operator's copy match a GitHub release (version check)? Which settings
# differ from the defaults (FRIK.toml vs data/config/FRIK.toml)? What exactly did her
# adapted version change vs the original? Nothing is written anywhere.
_CMP_MAX_FILES = 4000
_CMP_MAX_CHARS = 12000
_CFG_EXTS = {".toml", ".ini", ".cfg", ".conf", ".properties"}


def _cmp_resolve(p):
    p = os.path.expandvars(os.path.expanduser(str(p or "").strip().strip('"')))
    if p and not os.path.isabs(p):
        slug = _get_active()
        if slug:
            for cand in (os.path.join(_pdir(slug), p), os.path.join(_pdir(slug), "original", p)):
                if os.path.exists(cand):
                    return os.path.abspath(cand)
    return os.path.abspath(p) if p else ""


def _cmp_read(path):
    try:
        with open(path, "rb") as f:                      # READ-ONLY
            data = f.read(_IMPORT_MAX_FILE + 1)
    except OSError:
        return None, None
    if len(data) > _IMPORT_MAX_FILE or b"\x00" in data[:8192]:
        return data, None
    for enc in ("utf-8", "cp1252"):
        try:
            return data, data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data, None


def _cmp_walk(root, skip=None):
    out = {}
    skip = os.path.normcase(skip.rstrip("\\/")) if skip else None
    for d, dirs, fnames in os.walk(root):
        dirs[:] = [x for x in dirs if x.lower() not in _IMPORT_SKIP_DIRS
                   and os.path.normcase(os.path.join(d, x)) != skip]
        for fn in fnames:
            fp = os.path.join(d, fn)
            out[os.path.relpath(fp, root).replace("\\", "/")] = fp
            if len(out) >= _CMP_MAX_FILES:
                return out
    return out


def _settings(text):
    """{'section.key': value} for toml/ini-style files (comments and blank lines ignored)."""
    out, sec = {}, ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in "#;":
            continue
        if s.startswith("[") and s.endswith("]"):
            sec = s.strip("[]").strip()
            continue
        m = re.match(r"^([^=:#;]+?)\s*[=:]\s*(.*)$", s)
        if m:
            val = re.split(r"\s+[#;]", m.group(2), 1)[0].strip()
            out[(sec + "." if sec else "") + m.group(1).strip()] = val
    return out


def _diff_text(ta, tb, la, lb, ext, budget):
    import difflib
    if ext in _CFG_EXTS:
        sa, sb = _settings(ta), _settings(tb)
        if sa or sb:
            lines = []
            for k in sorted(set(sa) | set(sb), key=str.lower):
                if k not in sa:
                    lines.append(f"  {k}: only in {lb} = {sb[k]}")
                elif k not in sb:
                    lines.append(f"  {k}: only in {la} = {sa[k]}")
                elif sa[k].strip('"\'').lower() != sb[k].strip('"\'').lower():
                    lines.append(f"  {k}: {sa[k]} ({la})  vs  {sb[k]} ({lb})")
            same = sum(1 for k in sa if k in sb and sa[k].strip('"\'').lower() == sb[k].strip('"\'').lower())
            head = f"settings: {same} the same, {len(lines)} different"
            return (head + ("\n" + "\n".join(lines) if lines else ""))[:budget]
    d = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(), la, lb, n=2, lineterm=""))
    adds = sum(1 for x in d if x.startswith("+") and not x.startswith("+++"))
    dels = sum(1 for x in d if x.startswith("-") and not x.startswith("---"))
    body = "\n".join(d)
    if len(body) > budget:
        body = body[:budget] + "\n… (diff cut here — compare the single file for the rest)"
    return f"+{adds} / -{dels} lines\n{body}"


def compare_code(a, b, label_a="", label_b=""):
    pa, pb = _cmp_resolve(a), _cmp_resolve(b)
    for p, raw in ((pa, a), (pb, b)):
        if not p or not os.path.exists(p):
            return {"error": f"Not found: {raw} — give a full path, or a path inside your active project."}
    la = label_a or os.path.basename(pa.rstrip("\\/")) or pa
    lb = label_b or os.path.basename(pb.rstrip("\\/")) or pb
    if la == lb:
        la, lb = la + " (A)", lb + " (B)"
    res = {"type": "code_compare", "a": pa, "b": pb, "label_a": la, "label_b": lb, "ok": True}
    if os.path.isfile(pa) and os.path.isfile(pb):
        da, ta = _cmp_read(pa)
        db, tb = _cmp_read(pb)
        if da == db or (ta is not None and tb is not None
                        and ta.replace("\r\n", "\n") == tb.replace("\r\n", "\n")):
            res.update(identical=True, summary="identical")
        elif ta is None or tb is None:
            res.update(identical=False, summary=f"binary files differ ({len(da or b'')} vs {len(db or b'')} bytes)")
        else:
            res.update(identical=False, diff=_diff_text(ta, tb, la, lb, os.path.splitext(pa)[1].lower(), _CMP_MAX_CHARS))
        log(f"[S32][CODE] compared {pa} ↔ {pb}: {'identical' if res['identical'] else 'different'}")
        return res
    if os.path.isfile(pa) != os.path.isfile(pb):
        return {"error": "Compare a file with a file, or a folder with a folder."}
    # Folders. If one sits inside the other (her project's original\ holds the GitHub copy
    # in original\<repo>\), the inner one is left out of the outer walk.
    fa = _cmp_walk(pa, skip=pb if os.path.normcase(pb).startswith(os.path.normcase(pa)) else None)
    fb = _cmp_walk(pb, skip=pa if os.path.normcase(pa).startswith(os.path.normcase(pb)) else None)
    pairs, used_b = [], set()
    lower_b = {k.lower(): k for k in fb}
    for ra in sorted(fa):
        rb = lower_b.get(ra.lower())
        if rb is None:
            # Different layouts (Scripts/Source/… vs data/mod/Scripts/Source/…): match by the
            # longest unique path ending, then by unique file name.
            ends = [k for k in fb if k not in used_b and (k.lower().endswith("/" + ra.lower()) or ra.lower().endswith("/" + k.lower()))]
            if len(ends) == 1:
                rb = ends[0]
            else:
                name = ra.rsplit("/", 1)[-1].lower()
                same = [k for k in fb if k not in used_b and k.rsplit("/", 1)[-1].lower() == name]
                if len(same) == 1:
                    rb = same[0]
        if rb is not None and rb not in used_b:
            used_b.add(rb)
            pairs.append((ra, rb))
    only_a = sorted(set(fa) - {x for x, _ in pairs})
    only_b = sorted(set(fb) - used_b)
    same, changed, budget = [], [], _CMP_MAX_CHARS
    for ra, rb in pairs:
        da, ta = _cmp_read(fa[ra])
        db, tb = _cmp_read(fb[rb])
        if da == db:
            same.append(ra)
            continue
        if ta is not None and tb is not None and ta.replace("\r\n", "\n") == tb.replace("\r\n", "\n"):
            same.append(ra)                                  # only line endings differ
            continue
        entry = {"a": ra, "b": rb}
        if ta is None or tb is None:
            entry["note"] = f"binary, differs ({len(da or b'')} vs {len(db or b'')} bytes)"
        elif budget > 400:
            entry["diff"] = _diff_text(ta, tb, f"{la}/{ra}", f"{lb}/{rb}", os.path.splitext(ra)[1].lower(),
                                       min(budget, 4000))
            budget -= len(entry["diff"])
        else:
            entry["note"] = "text differs (no room left to show it — compare this file on its own)"
        changed.append(entry)
    common = len(pairs)
    res.update(identical=not changed and not only_a and not only_b, same=same, changed=changed,
               only_a=only_a, only_b=only_b, matched=common,
               match_pct=round(100 * len(same) / common) if common else 0,
               capped=len(fa) >= _CMP_MAX_FILES or len(fb) >= _CMP_MAX_FILES)
    log(f"[S32][CODE] compared folders {pa} ↔ {pb}: {len(same)} same, {len(changed)} changed, "
        f"{len(only_a)} only in A, {len(only_b)} only in B")
    return res


def resolve_project_path(p):
    """A path she gives a file tool that doesn't exist as written, but names one of her
    projects ('FRIK pipboy gaze power') or a path inside the active project
    ('original/Scripts') → the real folder. Else None (the tool runs as before)."""
    raw = str(p or "").strip().strip('"')
    if not raw or os.path.isabs(raw) or re.match(r"^[A-Za-z]:", raw) or os.path.exists(raw):
        return None
    want = _slug(raw)
    for b in _all_boards():
        if b["slug"] == want or _slug(b.get("name")) == want:
            return _pdir(b["slug"])
    act = _get_active()
    if act:
        for cand in (os.path.join(_pdir(act), raw), os.path.join(_pdir(act), "original", raw)):
            if os.path.exists(cand):
                return os.path.abspath(cand)
    return None


# ── search_code: grep INSIDE files (read-only) ────────────────────────────────────────
# 2026-10-29: with FRIK's 352 source files imported she said 'I'll scan the files for
# PipBoy / Power' — and had no tool for it (search_computer matches NAMES, and her
# project folder isn't indexed). This is that scan.
_SC_MAX_FILES = 6000
_SC_MAX_BYTES = 80 * 1024 * 1024
_SC_CODE_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".inl", ".cs", ".py", ".js",
                 ".ts", ".lua", ".psc", ".java", ".go", ".rs", ".swift", ".kt", ".m", ".mm", ".gd",
                 ".shader", ".hlsl", ".glsl", ".usf", ".ush", ".ini", ".toml", ".json", ".yaml", ".yml",
                 ".xml", ".cfg", ".txt", ".md", ".cmake", ".bat", ".ps1", ".sh"}


def _sc_terms(query):
    q = str(query or "").strip()
    if len(q) >= 2 and q[0] == q[-1] == '"':
        return [q[1:-1].lower()]                            # "exact phrase"
    return [t.lower() for t in re.split(r"[\s,;|]+", q) if len(t) >= 2][:8]


def _best_found_folder():
    """The folder her last search_computer most likely meant: the SHALLOWEST found folder whose
    own name contains a search word (FRIK → F:\\Vortex Mods\\fallout4vr\\FRIK 79.0 …, not
    …\\Textures\\FRIK)."""
    last = state.get("last_computer_search") or {}
    _generic = {"mod", "mods", "my", "the", "file", "files", "folder", "game", "games", "program", "app",
                "code", "source", "script", "scripts", "plugin", "plugins", "for", "and", "of"}
    terms = [str(t).lower() for t in (last.get("terms") or []) if len(str(t)) >= 2]
    terms = [t for t in terms if t not in _generic] or terms
    best = None
    for p in last.get("paths") or []:
        try:
            if not os.path.isdir(p):
                continue
        except OSError:
            continue
        name = os.path.basename(p.rstrip("\\/")).lower()
        hits = sum(1 for t in terms if t in name)
        if terms and not hits:
            continue
        depth = p.replace("/", "\\").rstrip("\\").count("\\")
        key = (-hits, depth)
        if best is None or key < best[0]:
            best = (key, p)
    return best[1] if best else ""


def search_code(query, path="", max_results=25):
    terms = _sc_terms(query)
    if not terms:
        return {"error": "Give search_code one or more words to look for, e.g. 'pipboy open look'."}
    root = _cmp_resolve(path) if path else ""
    picked = ""
    if not root:
        act = _get_active()
        root = _pdir(act) if act else ""
    if not root:
        # No project yet (fresh start, 2026-10-29: she found FRIK, then called search_code 4× with
        # no folder and got 'no active project' each time). Search what she just found instead.
        root = picked = _best_found_folder()
    if not root or not os.path.exists(root):
        return {"error": (f"Not found: {path}" if path else
                          "search_code needs a folder: you have no coding project yet and nothing found to "
                          "search. Find the program/mod with search_computer, then import_code its folder "
                          "(or pass path=<that folder>).")}
    try:
        import s32_findex
        private = s32_findex._private_dirs()
    except Exception:
        private = []
    own = os.path.normcase(os.path.abspath(_root())).rstrip("\\/") + os.sep

    def _blocked(p):
        n = os.path.normcase(os.path.abspath(p)).rstrip("\\/") + os.sep
        return not n.startswith(own) and any(n.startswith(x) for x in private)

    if _blocked(root):
        return {"error": "That folder is System 32's private data — search your project or the operator's own files."}
    files = [root] if os.path.isfile(root) else []
    compiled = []
    if not files:
        for d, dirs, fns in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x.lower() not in _IMPORT_SKIP_DIRS and not x.startswith(".")
                             and not _blocked(os.path.join(d, x)))
            for fn in sorted(fns):
                ext = os.path.splitext(fn)[1].lower()
                if ext in _SC_CODE_EXTS:
                    files.append(os.path.join(d, fn))
                elif ext in _COMPILED_EXTS and len(compiled) < 6:
                    compiled.append(os.path.relpath(os.path.join(d, fn), root).replace("\\", "/"))
            if len(files) >= _SC_MAX_FILES:
                break
    hits, read_bytes, scanned = [], 0, 0
    for fp in files[:_SC_MAX_FILES]:
        if read_bytes > _SC_MAX_BYTES:
            break
        data, text = _cmp_read(fp)
        if text is None:
            continue
        scanned += 1
        read_bytes += len(data)
        low = text.lower()
        if not any(t in low for t in terms):
            continue
        lines, seen_terms, count = [], set(), 0
        for no, line in enumerate(text.splitlines(), 1):
            ll = line.lower()
            got = [t for t in terms if t in ll]
            if got:
                count += 1
                seen_terms.update(got)
                lines.append((len(set(got)), no, line.strip()[:200]))
        lines.sort(key=lambda x: (-x[0], x[1]))
        name_bonus = sum(1 for t in terms if t in os.path.basename(fp).lower())
        hits.append({"file": os.path.relpath(fp, root if os.path.isdir(root) else os.path.dirname(root)),
                     "path": fp, "terms": len(seen_terms), "count": count,
                     "lines": [{"line": n, "text": t} for _, n, t in sorted(lines[:5], key=lambda x: x[1])],
                     "_score": (len(seen_terms) * 10 + name_bonus * 3 + min(count, 20) / 20)})
    hits.sort(key=lambda h: -h["_score"])
    for h in hits:
        h.pop("_score", None)
    n = int(max_results or 25)
    log(f"[S32][CODE] search_code {terms} in {root}: {len(hits)} file(s) of {scanned} scanned")
    return {"type": "code_search", "query": query, "terms": terms, "root": root, "scanned": scanned,
            "picked_from_search": bool(picked), "compiled": compiled,
            "imported": os.path.normcase(os.path.abspath(root)).startswith(own),
            "count": len(hits), "results": hits[:n], "all_terms": sum(1 for h in hits if h["terms"] == len(terms)),
            "quiet": True, "ok": True}


# ── edit_code: change exact lines, not the whole file ────────────────────────────────
# How Qwen Code / Claude Code edit (old_string → new_string, must match once). write_code
# made her re-type a whole 600-line Config.cpp to change one line — length limits, typos.
# The edited copy lives in her project (the operator's original is never touched); the
# diff card against the original shows the change inline in chat.
def _edit_source(b, file):
    """→ (path_to_read, error). Her working copy first, then the imported original,
    then a full path on the computer (read-only)."""
    raw = str(file or "").strip().strip('"')
    if not raw:
        return None, "Say which file: its name (e.g. 'Config.cpp') or path inside original\\."
    pdir = _pdir(b["slug"])
    fn = _safe_name(raw)
    mine = os.path.join(pdir, fn)
    if os.path.isfile(mine):
        return mine, None
    if os.path.isabs(raw) and os.path.isfile(raw):
        return raw, None
    rel = raw.replace("\\", "/").lstrip("./")
    orig_root = os.path.join(pdir, "original")
    for cand in (os.path.join(pdir, *rel.split("/")), os.path.join(orig_root, *rel.split("/"))):
        if os.path.isfile(cand):
            return cand, None
    cands = []
    for d, dirs, fns in os.walk(orig_root):
        dirs[:] = [x for x in dirs if x.lower() not in _IMPORT_SKIP_DIRS]
        for f in fns:
            if f.lower() == fn.lower():
                cands.append(os.path.join(d, f))
    if "/" in rel:
        ends = [c for c in cands if c.replace("\\", "/").lower().endswith("/" + rel.lower())]
        if len(ends) == 1:
            return ends[0], None
    if len(cands) == 1:
        return cands[0], None
    if cands:
        rels = [os.path.relpath(c, orig_root).replace("\\", "/") for c in cands[:6]]
        return None, f"Several files are named {fn}: {', '.join(rels)} — pass the one you mean with its folder."
    return None, f"No file named {raw} in your project or its original\\ copies — find it with search_code first."


def _find_block(text, old):
    """→ (start, end, how) of the ONE place `old` occurs; how='exact'|'loose'; or (count, None, err)."""
    n = text.count(old)
    if n == 1:
        i = text.index(old)
        return i, i + len(old), "exact"
    if n > 1:
        return n, None, "many"
    # Loose: same lines ignoring leading/trailing spaces (models often get indentation wrong).
    want = [l.strip() for l in old.strip("\n").split("\n")]
    if not any(want):
        return 0, None, "none"
    lines = text.split("\n")
    hits = []
    for i in range(len(lines) - len(want) + 1):
        if all(lines[i + k].strip() == want[k] for k in range(len(want))):
            hits.append(i)
    if len(hits) == 1:
        i = hits[0]
        start = sum(len(l) + 1 for l in lines[:i])
        end = start + sum(len(l) + 1 for l in lines[i:i + len(want)]) - 1
        return start, end, "loose"
    return len(hits), None, ("many" if hits else "none")


def edit_code(project, file, old_string, new_string, replace_all=False, purpose=""):
    if str(old_string or "") == str(new_string if new_string is not None else ""):
        return {"error": "old_string and new_string are the same — nothing would change."}
    b = _active_or(project, file, purpose)
    src, err = _edit_source(b, file)
    if err:
        return {"error": err}
    data, text = _cmp_read(src)
    if text is None:
        return {"error": f"{os.path.basename(src)} isn't a text file — it can't be edited as code."}
    text = text.replace("\r\n", "\n")
    old = str(old_string or "").replace("\r\n", "\n")
    new = str(new_string if new_string is not None else "").replace("\r\n", "\n")
    if not old:
        return {"error": "old_string is empty — copy the exact lines you want to change from read_file."}
    if old == new:
        return {"error": "old_string and new_string are the same — nothing would change."}
    if replace_all and text.count(old) > 1:
        n = text.count(old)
        new_text = text.replace(old, new)
        how = f"all {n} places"
    else:
        s, e, how = _find_block(text, old)
        if e is None:
            if how == "many":
                return {"error": f"old_string matches {s} places in {os.path.basename(src)} — include a few more "
                                 f"surrounding lines so it matches exactly one (or set replace_all=true)."}
            import difflib
            first = next((l.strip() for l in old.split("\n") if l.strip()), "")
            lines = text.split("\n")
            close = difflib.get_close_matches(first, [l.strip() for l in lines], n=3, cutoff=0.5)
            hint = ""
            if close:
                ln = [i + 1 for i, l in enumerate(lines) if l.strip() in close][:3]
                hint = " Closest lines: " + "; ".join(f"{i}: {lines[i - 1].strip()[:120]}" for i in ln)
            return {"error": f"old_string isn't in {os.path.basename(src)} — copy it EXACTLY from read_file "
                             f"(same words, same order).{hint}"}
        new_text = text[:s] + new + text[e:]
        how = "1 place" + (" (matched ignoring indentation)" if how == "loose" else "")
    fname = os.path.relpath(src, os.path.join(_pdir(b["slug"]), "original")).replace("\\", "/") \
        if os.path.normcase(src).startswith(os.path.normcase(os.path.join(_pdir(b["slug"]), "original"))) \
        else os.path.basename(src)
    r = write_code(b["name"], fname, new_text, purpose or f"edit {os.path.basename(src)}")
    if r.get("error"):
        return r
    # A few lines around the change, so she can check it without reading the file again.
    nl = new_text.split("\n")
    at = new_text.find(new) if new else -1
    line_no = new_text[:at].count("\n") + 1 if at >= 0 else 1
    lo, hi = max(1, line_no - 2), min(len(nl), line_no + new.count("\n") + 2)
    r["edit"] = {"file": os.path.basename(src), "from": src, "replaced": how, "line": line_no,
                 "context": "\n".join(f"{i}: {nl[i - 1]}" for i in range(lo, hi + 1))}
    log(f"[S32][CODE] edit_code {os.path.basename(src)}: replaced {how} at line {line_no}")
    return r
