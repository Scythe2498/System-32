# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_feedback.py — Automatic, self-tracked feedback loop (tool reliability first).

It watches its own tool use with no input from the operator:
  • every tool call's outcome (ok / error),
  • repeat + loop misfires flagged by the chain orchestrator,
  • unknown-tool slips (a name that isn't a real tool).

From recurring patterns it derives weighted "lessons". The top lessons are
injected into its system prompt each session, so a frozen model makes fewer of
the same mistakes over time — the loop closes immediately, no retrain needed.
Records are structured so they double as fine-tune data when that phase comes.

Stores (in REFERENCE_DIR):
  tool_stats.json   per-tool counters + recent errors
  lessons.json      weighted lessons, reinforced on repeat, decayed on success
"""

import os
import re
import json
import time
import threading
import difflib

from s32_config import REFERENCE_DIR, log

_LOCK         = threading.Lock()
_STATS_FILE   = os.path.join(REFERENCE_DIR, "tool_stats.json")
_LESSONS_FILE = os.path.join(REFERENCE_DIR, "lessons.json")

_MAX_WEIGHT            = 10
_MAX_LESSONS_IN_PROMPT = 8

_stats   = None       # lazy-loaded
_lessons = None


# ── persistence ──────────────────────────────────────────────────────────────
def _read(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _load():
    global _stats, _lessons
    if _stats is None:
        _stats = _read(_STATS_FILE, {})
    if _lessons is None:
        _lessons = _read(_LESSONS_FILE, {"lessons": []})


def _save():
    try:
        with open(_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(_stats, f, indent=2)
        with open(_LESSONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_lessons, f, indent=2)
    except Exception as e:
        log(f"[S32][FB] save failed: {e}")


# ── helpers ──────────────────────────────────────────────────────────────────
def _stat(name):
    return _stats.setdefault(name, {
        "calls": 0, "ok": 0, "err": 0, "repeat": 0, "loop": 0,
        "unknown": 0, "last_errs": [],
    })


def _norm_err(e):
    return re.sub(r"[0-9]+", "#", (e or "").lower())[:80]


def _result_is_error(results):
    """results = list of result dicts yielded by dispatch_tool."""
    if not results:
        return False, ""
    for r in results:
        if isinstance(r, dict) and r.get("error"):
            return True, str(r["error"])[:200]
    return False, ""


def _bump_lesson(scope, kind, text, delta=1):
    """Add or reinforce a lesson (deduped by scope+kind), capping weight."""
    for l in _lessons["lessons"]:
        if l["scope"] == scope and l["kind"] == kind:
            l["weight"] = min(_MAX_WEIGHT, l["weight"] + delta)
            l["hits"]   = l.get("hits", 0) + 1
            l["ts"]     = time.time()
            if text:
                l["lesson"] = text
            return
    _lessons["lessons"].append({
        "scope": scope, "kind": kind, "lesson": text,
        "weight": max(1, delta), "hits": 1, "ts": time.time(),
    })


def _decay_lesson(scope, kind, delta=1):
    """Weaken (and eventually prune) a lesson when the tool behaves again."""
    keep = []
    for l in _lessons["lessons"]:
        if l["scope"] == scope and l["kind"] == kind:
            l["weight"] -= delta
            if l["weight"] <= 0:
                continue
        keep.append(l)
    _lessons["lessons"] = keep


# ── public recording API (called from the tool path) ─────────────────────────
def record_tool_outcome(name, params, results):
    """Every tool call lands here via the dispatch wrapper."""
    try:
        with _LOCK:
            _load()
            st = _stat(name)
            st["calls"] += 1
            is_err, emsg = _result_is_error(results)
            if is_err:
                st["err"] += 1
                st["last_errs"] = ([emsg] + st.get("last_errs", []))[:5]
                # Same error recurring for this tool → promote a caution lesson.
                norm = _norm_err(emsg)
                same = sum(1 for e in st["last_errs"] if _norm_err(e) == norm)
                if same >= 2:
                    _bump_lesson(
                        f"tool:{name}", "error",
                        f"{name} has failed repeatedly with: \"{emsg[:120]}\". "
                        f"Check inputs/preconditions before calling it again.", 2)
            else:
                st["ok"] += 1
                _decay_lesson(f"tool:{name}", "error", 1)   # behaving again
            _save()
    except Exception as e:
        log(f"[S32][FB] record_tool_outcome error: {e}")


def record_pattern(name, kind):
    """kind ∈ {'repeat','loop'} — flagged by the chain orchestrator."""
    try:
        with _LOCK:
            _load()
            st = _stat(name)
            st[kind] = st.get(kind, 0) + 1
            if kind == "repeat":
                _bump_lesson(
                    f"tool:{name}", "repeat",
                    f"{name}: calling it again with the same input returns the same result — "
                    f"answer from the first result instead of repeating the call.", 2)
            elif kind == "loop":
                _bump_lesson(
                    f"tool:{name}", "loop",
                    f"{name}: don't call it many times in a row — gather what you need in one "
                    f"or two calls, then act on it.", 2)
            _save()
    except Exception as e:
        log(f"[S32][FB] record_pattern error: {e}")


def record_unknown(name):
    """The model emitted a tool name that isn't real."""
    try:
        with _LOCK:
            _load()
            st = _stat(name)
            st["unknown"] = st.get("unknown", 0) + 1
            try:
                from s32_llm import TOOL_NAMES
                close = difflib.get_close_matches(name, list(TOOL_NAMES), n=2, cutoff=0.5)
            except Exception:
                close = []
            hint = f" Closest real tools: {', '.join(close)}." if close else ""
            _bump_lesson(
                f"badtool:{name}", "unknown",
                f"There is no tool named '{name}'.{hint} Use an exact name from the tool list.", 2)
            _save()
    except Exception as e:
        log(f"[S32][FB] record_unknown error: {e}")


# ── consumption ──────────────────────────────────────────────────────────────
def lesson_block(max_n=_MAX_LESSONS_IN_PROMPT):
    """Top-weighted lessons formatted for the system prompt (or '' if none)."""
    try:
        with _LOCK:
            _load()
            ls = sorted(_lessons["lessons"],
                        key=lambda l: (-l["weight"], -l.get("ts", 0)))[:max_n]
        if not ls:
            return ""
        lines = ["LESSONS LEARNED (from your own past tool use — follow these to avoid "
                 "repeating mistakes):"]
        lines += [f"  • {l['lesson']}" for l in ls]
        return "\n".join(lines)
    except Exception:
        return ""


def reset():
    """Wipe accumulated training data (lessons + tool stats). Hooked to the
    memory-erase button so bad/clogged feedback can be cleared and regenerated
    fresh — e.g. after baking new rules into identity.md."""
    global _stats, _lessons
    try:
        with _LOCK:
            _stats = {}
            _lessons = {"lessons": []}
            _save()
        log("[S32][FB] training data cleared (lessons + tool stats).")
        return True
    except Exception as e:
        log(f"[S32][FB] reset failed: {e}")
        return False


def reload():
    """Drop the in-memory cache so the next access re-reads from disk — used
    after a state snapshot restores lessons.json / tool_stats.json."""
    global _stats, _lessons
    with _LOCK:
        _stats = None
        _lessons = None
        _load()


def review_lessons():
    """Tool result: current lessons + a small per-tool reliability summary."""
    try:
        with _LOCK:
            _load()
            ls = sorted(_lessons["lessons"], key=lambda l: -l["weight"])
            rel = []
            for n, s in _stats.items():
                c = s.get("calls", 0)
                if c:
                    rel.append({"tool": n, "calls": c, "ok": s.get("ok", 0),
                                "err": s.get("err", 0), "repeat": s.get("repeat", 0),
                                "loop": s.get("loop", 0)})
        return {
            "lessons": [{"lesson": l["lesson"], "weight": l["weight"],
                         "hits": l.get("hits", 1)} for l in ls],
            "reliability": sorted(rel, key=lambda x: -x["err"])[:12],
            "count": len(ls),
        }
    except Exception as e:
        return {"error": str(e), "lessons": [], "reliability": [], "count": 0}
