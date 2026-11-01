# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_training.py — Training data: what worked, ready for Unsloth (2026-10-29).

THE LOOP
  1. Every turn is recorded IN FULL as a trajectory: the request, each step (its
     thinking, what it said, the tool it called and with what, what that tool returned
     to it) and the final answer. (trajectories.jsonl, in this person's folder.)
  2. Each trajectory is graded by the harness — a turn that ended in a loop breaker, a
     halt, the step cap, a lost connection, a canned fallback or code that never ran
     clean is 'bad'; a turn that finished on its own is 'good'; a stopped turn or one
     built on an attached picture is left out.
  3. The operator's word overrides the grade: 👍 / 👎 on a turn, and Works / Not right
     on a code or Unreal card (train_labels.jsonl). 👍 counts double.
  4. EXPORT (Unsloth-ready) → <Output>\\training\\<computer>\\unsloth_<time>\\
       sft.jsonl   what worked — Qwen/OpenAI chat format WITH tool calls:
                   {"messages":[system, user, assistant{reasoning_content, tool_calls},
                    tool, …, assistant], "tools":[…schemas…]}
       dpo.jsonl   what worked vs what didn't — {"prompt":[…], "chosen":[…], "rejected":[…]}
                   from code that failed and the fix that ran clean (chat code + Unreal),
                   and from steps where it described a tool call instead of making it.
       train_unsloth.py, README.txt, manifest.json
  5. Train → load the new model → it does more things right → those become the next
     export. That is the positive-reinforcement loop.

The old flat conversation corpus (record_turn / export_training_data) is kept for the
legacy export but NOT used for the Unsloth set: it held final answers without the tool
calls behind them, which would teach answering from memory instead of looking it up.
"""

import os
import re
import json
import time
import random
import threading
import datetime
from collections import OrderedDict

from s32_config import CONFIG, REFERENCE_DIR, PERSON_DIR, TRAINING_ROOT, person_subdir, log

_LOCK       = threading.Lock()
SESSION_ID  = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
# Per person (2026-10-28): the corpus is their conversations — private to them.
_CORPUS     = os.path.join(PERSON_DIR, "conversation_log.jsonl")
_TRAJ       = os.path.join(PERSON_DIR, "trajectories.jsonl")
_LABELS     = os.path.join(PERSON_DIR, "train_labels.jsonl")

# A short, STABLE persona line for the system role. Deliberately not its giant
# dynamic runtime prompt — fine-tunes want a consistent, compact system message.
# Override with config "training_system_prompt", or pass system_prompt="" to omit.
_DEFAULT_PERSONA = (
    "You are {name}, a portable local AI assistant that lives on a USB drive and "
    "runs entirely on the user's own hardware. You are direct and capable, and you act "
    "with your tools — ComfyUI, the filesystem, the web, code, Unreal and games — "
    "instead of describing what you would do."
)


def _persona():
    p = CONFIG.get("training_system_prompt")
    if p is not None:
        return str(p)
    try:
        from s32_memory import _load_personality
        name = _load_personality().get("assistant_name") or "System 32"
    except Exception:
        name = "System 32"
    return _DEFAULT_PERSONA.format(name=name)


def _training_dir():
    """This person's training folder: their exported conversations and the code /
    Unreal repair corpora (Output\\training\\<computer>\\ — private to them)."""
    d = person_subdir(TRAINING_ROOT) or os.path.join(PERSON_DIR, "training")
    os.makedirs(d, exist_ok=True)
    return d


def _resetting():
    try:
        from s32_config import state as _st
        return bool(_st.get("resetting"))
    except Exception:
        return False


def _append(path, rec):
    with _LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _read_jsonl(path):
    rows = []
    if not os.path.exists(path):
        return rows
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
    except Exception as e:
        log(f"[S32][TRAIN] read {os.path.basename(path)} failed: {str(e)[:80]}")
    return rows


def _noise(text):
    """True for bracket-only system/command lines we don't want as training data."""
    t = (text or "").strip()
    return (not t) or (t.startswith("[") and t.endswith("]"))


# ═════════════════════════════════════════════════════════════════════════════
#  1. The trajectory recorder — one per operator turn
# ═════════════════════════════════════════════════════════════════════════════
_cur = None                 # the turn being recorded
_cur_thread = None          # only steps from the /chat thread that began it count
_recent = []                # (user, final, id) of this session's last turns — context
_seq = [0]
_CONTEXT_TURNS = 3
_USER_CAP = 30000
_GEN_TOOLS = {"run_workflow", "generate_image", "generate_video", "train_lora"}
_FALLBACKS = ("(Done — the results are above.)",
              "(You already have that content above",
              "without getting further, so I stopped there",
              "I seem to be stuck checking",
              "(I've used all my attempts",
              "(I've run ",
              "Lost connection while")


def _result_cap(tool):
    # Same room the chain gives it (system32 _cap): this is what it actually saw.
    if tool in ("run_unreal_python", "write_code", "run_code"):
        return 7000
    if tool in ("web_search", "web_fetch"):
        return 9000
    if tool in ("web_image_search", "web_video_search"):
        return 3000
    if tool == "search_computer":
        return 4000
    if tool == "import_code":
        return 7000
    if tool in ("compare_code", "search_code"):
        return 12000
    if tool == "edit_code":
        return 4000
    if tool in ("read_file", "show_file"):
        return 12000
    return 600


def turn_begin(user_content, user_image=False):
    """Start recording a turn. Returns its id (sent to the chat UI for 👍/👎)."""
    global _cur, _cur_thread
    if _resetting():
        _cur = None
        return ""
    _seq[0] += 1
    tid = "%s_%03d_%04x" % (SESSION_ID, _seq[0], random.getrandbits(16))
    _cur = {"id": tid, "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "session": SESSION_ID, "user": (user_content or "")[:_USER_CAP],
            "context": [list(x) for x in _recent[-_CONTEXT_TURNS:]],
            "steps": [], "dropped": [], "flags": [], "links": [], "code_runs": {}}
    if user_image:
        _cur["flags"].append("user_image")
    _cur_thread = threading.get_ident()
    return tid


def _mine():
    return _cur is not None and threading.get_ident() == _cur_thread


def note_step(say, tool=None, args=None, thought=""):
    """One model call inside the turn (called from s32_llm.chat)."""
    if not _mine():
        return
    try:
        st = {"say": (say or "").strip()}
        if (thought or "").strip():
            st["thought"] = thought.strip()
        if tool:
            st["tool"] = tool
            st["args"] = json.loads(json.dumps(args or {}, default=str))
        _cur["steps"].append(st)
    except Exception as e:
        log(f"[S32][TRAIN] note_step failed: {str(e)[:80]}")


def drop_last_step(reason):
    """The harness rejected the last step (e.g. it described a tool call instead of
    making it) and asks again — keep the rejected step as a 'what didn't work' sample."""
    if not _mine() or not _cur["steps"]:
        return
    st = _cur["steps"].pop()
    _cur["dropped"].append({"at": len(_cur["steps"]), "step": st, "reason": reason})
    flag("nudged")


def _is_err(r):
    return isinstance(r, dict) and (bool(r.get("error")) or r.get("ok") is False)


def note_result(tool, results):
    """What the tool handed back to it (the same summary the chain shows it)."""
    if not _mine():
        return
    try:
        r0 = results[0] if results else {}
        try:
            from s32_llm import _tool_result_summary
            summ = _tool_result_summary(tool, r0 if isinstance(r0, dict) else {})
        except Exception:
            summ = json.dumps(r0, default=str)[:600] if r0 else "(no result)"
        summ = (summ or "(done)")[:_result_cap(tool)]
        # attach to the most recent step that called this tool and has no result yet
        for st in reversed(_cur["steps"]):
            if st.get("tool") == tool and "result" not in st:
                st["result"] = summ
                st["error"] = _is_err(r0)
                # A script the harness refused because a native tool does the job (file ops →
                # file_operation): not part of the 'what worked' path even on a 👍 — kept as a
                # 'what didn't work' sample instead (2026-10-30: copy_images.py → file_operation).
                if isinstance(r0, dict) and r0.get("redirect"):
                    _cur["steps"].remove(st)
                    _cur["dropped"].append({"at": len(_cur["steps"]), "step": st,
                                            "reason": "wrote/ran a script for a job a native tool does"})
                    flag("nudged")
                break
        else:
            _cur["steps"].append({"say": "", "tool": tool, "args": {}, "result": summ,
                                  "error": _is_err(r0), "orphan": True})
        # links: verdicts arrive later and must find this turn
        for r in results or []:
            if not isinstance(r, dict):
                continue
            key = None
            if r.get("type") == "ue_code" and r.get("task"):
                key = "ue:" + str(r["task"])
            elif r.get("type") == "code_file" and r.get("slug") and r.get("filename"):
                key = "code:%s/%s" % (r["slug"], r["filename"])
            if key:
                if key not in _cur["links"]:
                    _cur["links"].append(key)
                if r.get("ran") or r.get("type") == "ue_code":
                    if not (r.get("duplicate") or r.get("declined")):
                        _cur["code_runs"][key] = bool(r.get("ok"))
    except Exception as e:
        log(f"[S32][TRAIN] note_result failed: {str(e)[:80]}")


def flag(name):
    if _mine() and name not in _cur["flags"]:
        _cur["flags"].append(name)


_BAD_FLAGS = ("loop_broken", "repeat_broken", "halted", "step_cap", "reconnected",
              "fallback_reply", "code_unfixed", "error")
_SKIP_FLAGS = ("cancelled", "user_image", "empty")
_NEVER_TEACH = {"loop_broken", "repeat_broken", "step_cap", "reconnected", "cancelled",
                "user_image", "empty"}


def _grade(t):
    fl = set(t.get("flags") or [])
    if fl & set(_SKIP_FLAGS):
        return "skip"
    if fl & set(_BAD_FLAGS):
        return "bad"
    return "good"


def turn_end(cancelled=False):
    """Finish the turn: grade it and append it to trajectories.jsonl. Never raises."""
    global _cur
    t, _cur = _cur, None
    if t is None or threading.get_ident() != _cur_thread:
        if t is not None and threading.get_ident() != _cur_thread:
            _cur = t           # someone else's turn — leave it alone
        return
    try:
        if _resetting():
            return
        if cancelled and "cancelled" not in t["flags"]:
            t["flags"].append("cancelled")
        steps = t["steps"]
        final = ""
        if steps and not steps[-1].get("tool"):
            final = steps[-1].get("say", "")
        if not steps or (not final and not any(s.get("tool") for s in steps)):
            t["flags"].append("empty")
        if final and any(final.startswith(f) for f in _FALLBACKS):
            t["flags"].append("fallback_reply")
        if any(ok is False for ok in t["code_runs"].values()):
            t["flags"].append("code_unfixed")
        t["final"] = final
        t["tools_used"] = sorted({s["tool"] for s in steps if s.get("tool")})
        t["auto"] = _grade(t)
        t.pop("code_runs", None)
        if len((t.get("user") or "").strip()) < 2 or _noise(t.get("user")):
            return
        _append(_TRAJ, t)
        if final:
            _recent.append((t["user"][:4000], final[:4000], t["id"]))
            del _recent[:-_CONTEXT_TURNS]
        log(f"[S32][TRAIN] turn recorded: {len(steps)} step(s), "
            f"tools={','.join(t['tools_used']) or '-'}, grade={t['auto']}"
            + (f" ({', '.join(t['flags'])})" if t["flags"] else ""))
    except Exception as e:
        log(f"[S32][TRAIN] turn_end failed: {str(e)[:100]}")


# ── the operator's word ──────────────────────────────────────────────────────
def rate_turn(turn_id, label, note=""):
    """👍 ('good') / 👎 ('bad') / clear ('none') on a recorded turn."""
    if _resetting():
        return {"error": "resetting"}
    label = {"up": "good", "down": "bad", "good": "good", "bad": "bad"}.get(str(label), "none")
    tid = str(turn_id or "").strip()
    if not re.fullmatch(r"[0-9A-Za-z_]{6,64}", tid):
        return {"error": "Unknown turn."}
    _append(_LABELS, {"id": tid, "label": label, "source": "operator",
                      "note": (note or "")[:1000],
                      "ts": datetime.datetime.now().isoformat(timespec="seconds")})
    log(f"[S32][TRAIN] operator rated {tid}: {label}")
    return {"ok": True, "label": label}


def label_link(key, works, source, note=""):
    """A Works / Not right verdict on a code or Unreal card → the turn that made it."""
    try:
        if _resetting():
            return
        target = None
        if _cur is not None and key in _cur.get("links", []):
            target = _cur["id"]          # verdict given while the same turn is running
        if not target:
            for t in reversed(_read_jsonl(_TRAJ)):
                if key in (t.get("links") or []):
                    target = t["id"]
                    break
        if not target:
            return
        _append(_LABELS, {"id": target, "label": "good" if works else "bad", "source": source,
                          "note": (note or "")[:1000],
                          "ts": datetime.datetime.now().isoformat(timespec="seconds")})
        log(f"[S32][TRAIN] {source} → turn {target}: {'good' if works else 'bad'}")
    except Exception as e:
        log(f"[S32][TRAIN] label_link failed: {str(e)[:80]}")


def _labels():
    """id → final label (the operator's own rating beats a card verdict; last wins)."""
    out = {}
    for r in _read_jsonl(_LABELS):
        i, lab, src = r.get("id"), r.get("label"), r.get("source", "")
        if not i:
            continue
        prev = out.get(i)
        if src == "operator":
            out[i] = (lab, src) if lab != "none" else None
        elif not (prev and prev[1] == "operator"):
            out[i] = (lab, src)
    return {k: v for k, v in out.items() if v}


# ═════════════════════════════════════════════════════════════════════════════
#  Legacy flat corpus (kept for the old export; no longer fed by the chat loop)
# ═════════════════════════════════════════════════════════════════════════════
def record_turn(user_text, assistant_text, tools=None):
    """Append one completed exchange to the persistent corpus. Never raises."""
    try:
        if _resetting():
            return False
        u = (user_text or "").strip()
        a = (assistant_text or "").strip()
        if len(u) < 2 or len(a) < 2 or _noise(u):
            return False
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               "session": SESSION_ID, "user": u, "assistant": a}
        if tools:
            rec["tools"] = list(tools)
        _append(_CORPUS, rec)
        return True
    except Exception as e:
        log(f"[S32][TRAIN] record_turn failed: {str(e)[:80]}")
        return False


def _read_corpus():
    return _read_jsonl(_CORPUS)


def training_stats():
    trajs = _read_jsonl(_TRAJ)
    labs = _labels()
    good = bad = up = down = 0
    for t in trajs:
        lab = labs.get(t.get("id"))
        g = lab[0] if lab else t.get("auto")
        good += g == "good"
        bad += g == "bad"
        if lab and lab[1] == "operator":
            up += lab[0] == "good"
            down += lab[0] == "bad"
    rows = _read_corpus()
    return {"turns_recorded": len(trajs), "good": good, "bad": bad,
            "thumbs_up": up, "thumbs_down": down,
            "turns": len(rows), "sessions": len({r.get("session", "") for r in rows}),
            "corpus": _CORPUS}


def wipe_corpus():
    """Delete everything recorded for training (blank-slate reset)."""
    global _cur
    try:
        with _LOCK:
            for p in (_CORPUS, _TRAJ, _LABELS):
                if os.path.exists(p):
                    os.remove(p)
            _recent.clear()
            _cur = None
        log("[S32][TRAIN] training records wiped (conversations, turns, ratings).")
        return True
    except Exception as e:
        log(f"[S32][TRAIN] corpus wipe failed: {str(e)[:80]}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  2. Export — Unsloth-ready
# ═════════════════════════════════════════════════════════════════════════════
def _tool_specs():
    try:
        from s32_llm import TOOLS_SPEC
        return {t["function"]["name"]: t for t in TOOLS_SPEC}
    except Exception as e:
        log(f"[S32][TRAIN] tool specs unavailable: {e}")
        return {}


def _tools_for(used, specs, seed, scope, distractors=6):
    """The tool list a sample is trained with. 'all' = every tool, like at runtime (long:
    ~10k tokens per sample). 'relevant' (default) = the tools it used plus a few others
    picked at random, so it still learns to CHOOSE — and to choose none."""
    if scope == "all":
        return list(specs.values())
    names = [n for n in used if n in specs]
    rest = sorted(n for n in specs if n not in names)
    rnd = random.Random(seed)
    names += rnd.sample(rest, min(distractors, len(rest)))
    rnd.shuffle(names)
    return [specs[n] for n in names]


def _call(tool, args, i):
    return {"id": f"call_{i}", "type": "function",
            "function": {"name": tool, "arguments": args or {}}}


def _assistant(step, i, thinking):
    m = {"role": "assistant", "content": step.get("say", "") or ""}
    if thinking and step.get("thought"):
        m["reasoning_content"] = step["thought"]
    if step.get("tool"):
        m["tool_calls"] = [_call(step["tool"], step.get("args") or {}, i)]
    return m


def _head(t, sysmsg, rejected=()):
    msgs = [{"role": "system", "content": sysmsg}] if sysmsg else []
    for c in t.get("context") or []:
        u, a = c[0], c[1]
        if len(c) > 2 and c[2] in rejected:
            continue                     # never show a 👎 answer as its own past reply
        msgs.append({"role": "user", "content": u})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": t["user"]})
    return msgs


def _trajectory_messages(t, sysmsg, thinking, upto=None, rejected=()):
    """system + context + user + steps (assistant / tool pairs)."""
    msgs = _head(t, sysmsg, rejected)
    steps = t["steps"] if upto is None else t["steps"][:upto]
    for i, st in enumerate(steps):
        msgs.append(_assistant(st, i, thinking))
        if st.get("tool"):
            msgs.append({"role": "tool", "tool_call_id": f"call_{i}", "name": st["tool"],
                         "content": st.get("result") or "(done)"})
    if upto is None:
        while msgs and msgs[-1]["role"] == "tool":       # ends on its call (terminal gens)
            msgs.pop()
    return msgs


def _repair_pairs(sysmsg):
    """Code that failed vs the fix that ran clean → DPO pairs (chat code + Unreal)."""
    pairs = []
    tdir = _training_dir()
    for name, tool in (("code_repairs.jsonl", "write_code"),
                       ("unreal_code_repairs.jsonl", "run_unreal_python")):
        for r in _read_jsonl(os.path.join(tdir, name)):
            bad, good = r.get("failed_code") or "", r.get("fixed_code") or ""
            goal = (r.get("goal") or "").strip()
            if not bad or not good or bad == good or not goal:
                continue
            if tool == "write_code":
                ask = f"{goal}\n(project: {r.get('project', '')}, file: {r.get('file', '')})"
                mk = lambda code: {"project": r.get("project", ""), "filename": r.get("file", ""),
                                   "code": code, "run": True}
            else:
                ask = goal
                mk = lambda code: {"code": code, "purpose": goal[:120]}
            head = ([{"role": "system", "content": sysmsg}] if sysmsg else []) + \
                [{"role": "user", "content": ask}]
            pairs.append({"prompt": head,
                          "chosen": [{"role": "assistant", "content": "",
                                      "tool_calls": [_call(tool, mk(good), 0)]}],
                          "rejected": [{"role": "assistant", "content": "",
                                        "tool_calls": [_call(tool, mk(bad), 0)]}],
                          "tools_used": [tool], "source": name.replace(".jsonl", "")})
    return pairs


def _legacy_verdict_samples(sysmsg, since_ts):
    """Code the operator marked 'works' before turns were recorded in full → a one-step
    sample (request → the tool call with the working code)."""
    out = []
    tdir = _training_dir()
    for name, tool in (("code_verdicts.jsonl", "write_code"),
                       ("unreal_code_verdicts.jsonl", "run_unreal_python")):
        for r in _read_jsonl(os.path.join(tdir, name)):
            if r.get("verdict") != "works" or not r.get("code") or not r.get("goal"):
                continue
            if since_ts and (r.get("ts") or "") >= since_ts:
                continue                    # covered by a full trajectory
            if tool == "write_code":
                args = {"project": r.get("project", ""), "filename": r.get("file", ""),
                        "code": r["code"], "run": True}
            else:
                args = {"code": r["code"], "purpose": r["goal"][:120]}
            msgs = ([{"role": "system", "content": sysmsg}] if sysmsg else []) + [
                {"role": "user", "content": r["goal"]},
                {"role": "assistant", "content": "", "tool_calls": [_call(tool, args, 0)]}]
            out.append({"messages": msgs, "tools_used": [tool], "source": name.replace(".jsonl", "")})
    return out


def export_unsloth(tools_scope="relevant", thinking=True, system_prompt=None):
    """Write the Unsloth-ready set. Returns a summary dict or {"error": …}."""
    try:
        trajs = _read_jsonl(_TRAJ)
        labs = _labels()
        specs = _tool_specs()
        sysmsg = (_persona() if system_prompt is None else system_prompt or "").strip()

        sft, dpo = [], []
        rejected = {i for i, v in labs.items() if v[0] == "bad"} | \
            {t.get("id") for t in trajs if t.get("auto") == "bad" and t.get("id") not in labs}
        n = {"good": 0, "bad": 0, "skip": 0, "thumbs_up": 0, "thumbs_down": 0,
             "verdict_good": 0, "verdict_bad": 0, "unknown_tool": 0, "with_thinking": 0}
        for t in trajs:
            lab = labs.get(t.get("id"))
            grade = t.get("auto", "skip")
            if lab:
                k = ("thumbs_" + ("up" if lab[0] == "good" else "down")) if lab[1] == "operator" \
                    else ("verdict_" + lab[0])
                n[k] = n.get(k, 0) + 1
                # The operator's word wins — except a loop / the step cap / a stopped turn /
                # one built on a picture is never TAUGHT, even if the answer it got to was right.
                if lab[0] == "bad":
                    grade = "bad"
                elif not set(t.get("flags") or []) & _NEVER_TEACH:
                    grade = "good"
            n[grade] = n.get(grade, 0) + 1
            used = t.get("tools_used") or []
            if specs and any(u not in specs for u in used):
                n["unknown_tool"] += 1           # a tool that no longer exists
                continue
            if grade == "good":
                rec = {"messages": _trajectory_messages(t, sysmsg, thinking, rejected=rejected),
                       "tools": _tools_for(used, specs, t["id"], tools_scope),
                       "meta": {"id": t["id"], "label": (lab[0] + "/" + lab[1]) if lab else "auto",
                                "tools": used}}
                if any(m.get("reasoning_content") for m in rec["messages"]):
                    n["with_thinking"] += 1
                sft.append(rec)
                if lab and lab[0] == "good":
                    sft.append(rec)              # the operator's 👍 / Works counts double
                # nudges: a step that only TALKED vs the retried step that acted
                for d in t.get("dropped") or []:
                    at = d.get("at", 0)
                    if at < len(t["steps"]) and t["steps"][at].get("tool"):
                        dpo.append({"prompt": _trajectory_messages(t, sysmsg, thinking, upto=at,
                                                                   rejected=rejected),
                                    "chosen": [_assistant(t["steps"][at], at, thinking)],
                                    "rejected": [_assistant(d["step"], at, thinking)],
                                    "tools": _tools_for(used, specs, t["id"], tools_scope),
                                    "meta": {"id": t["id"], "source": "nudge"}})

        since = trajs[0].get("ts") if trajs else ""
        for s in _legacy_verdict_samples(sysmsg, since):
            s["tools"] = _tools_for(s.pop("tools_used"), specs, s["messages"][-2]["content"][:40],
                                    tools_scope)
            s["meta"] = {"source": s.pop("source"), "label": "works"}
            sft.append(s)
        for p in _repair_pairs(sysmsg):
            p["tools"] = _tools_for(p.pop("tools_used"), specs, p["prompt"][-1]["content"][:40],
                                    tools_scope)
            p["meta"] = {"source": p.pop("source")}
            dpo.append(p)

        if not sft and not dpo:
            return {"error": "Nothing to export yet — talk with it, use its tools, and rate "
                             "a few turns 👍 / 👎 first."}

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = os.path.join(_training_dir(), f"unsloth_{ts}")
        os.makedirs(out, exist_ok=True)

        def _w(name, rows):
            p = os.path.join(out, name)
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.replace(p + ".tmp", p)

        _w("sft.jsonl", sft)
        _w("dpo.jsonl", dpo)
        think_pct = round(100 * n["with_thinking"] / max(1, len(sft)))
        manifest = {"created": ts, "persona": sysmsg, "tools_scope": tools_scope,
                    "thinking": thinking, "sft_samples": len(sft), "dpo_pairs": len(dpo),
                    "turns": len(trajs), "counts": n, "thinking_pct": think_pct,
                    "base_model": CONFIG.get("training_base_model") or _DEFAULT_BASE}
        with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        with open(os.path.join(out, "train_unsloth.py"), "w", encoding="utf-8") as f:
            f.write(TRAIN_SCRIPT.replace("__BASE__", manifest["base_model"]))
        with open(os.path.join(out, "README.txt"), "w", encoding="utf-8") as f:
            f.write(README.format(sft=len(sft), dpo=len(dpo), turns=len(trajs),
                                  good=n["good"], bad=n["bad"], up=n["thumbs_up"],
                                  down=n["thumbs_down"], think=think_pct,
                                  base=manifest["base_model"]))
        log(f"[S32][TRAIN] Unsloth export: {len(sft)} SFT sample(s), {len(dpo)} DPO pair(s) "
            f"from {len(trajs)} turn(s) -> {out}")
        return {"ok": True, "path": out, "sft": len(sft), "dpo": len(dpo), "turns": len(trajs),
                "good": n["good"], "bad": n["bad"], "thinking_pct": think_pct,
                # legacy keys the old button reads
                "count": len(sft), "format": "unsloth"}
    except Exception as e:
        import traceback
        log(f"[S32][TRAIN] Unsloth export failed: {traceback.format_exc()[-400:]}")
        return {"error": f"Export failed: {str(e)[:120]}"}


def export_training_data(fmt="unsloth", group_by_session=True, system_prompt=None,
                         include_live=True, min_chars=2):
    """fmt='unsloth' (default) → the full set above. 'chat' / 'completion' → the old flat
    conversation export (final answers only, no tool calls)."""
    if fmt in ("unsloth", "", None):
        return export_unsloth(system_prompt=system_prompt)
    try:
        rows = _read_corpus()
        rows = [r for r in rows
                if len((r.get("user") or "").strip()) >= min_chars
                and len((r.get("assistant") or "").strip()) >= min_chars]
        if not rows:
            return {"error": "No conversation data recorded yet — have a conversation first, "
                             "then export."}
        sysmsg = (_persona() if system_prompt is None else system_prompt or "").strip()
        records = []
        if fmt == "completion":
            for r in rows:
                records.append({"prompt": r["user"], "completion": r["assistant"]})
        else:
            groups = OrderedDict()
            for r in rows:
                groups.setdefault(r.get("session", "_") if group_by_session else id(r), []).append(r)
            for grp in groups.values():
                msgs = [{"role": "system", "content": sysmsg}] if sysmsg else []
                for r in grp:
                    msgs.append({"role": "user", "content": r["user"]})
                    msgs.append({"role": "assistant", "content": r["assistant"]})
                records.append({"messages": msgs})
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(_training_dir(), f"sys32_dataset_{fmt}_{ts}.jsonl")
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(path + ".tmp", path)
        return {"path": path, "count": len(records), "turns": len(rows), "format": fmt}
    except Exception as e:
        return {"error": f"Export failed: {str(e)[:120]}"}


_DEFAULT_BASE = "Qwen/Qwen3.6-35B-A3B"

README = """System 32 — training set (Unsloth-ready)
==========================================

  sft.jsonl    {sft} samples — turns that WORKED, with every tool call, tool result and
               (where it thought) its reasoning. Rated 👍 / "Works" counts twice.
  dpo.jsonl    {dpo} pairs — what worked vs what didn't: code that failed vs the fix that
               ran clean, and steps where it only described a tool call vs the call.
  manifest.json, train_unsloth.py (this folder's trainer)

From {turns} recorded turns: {good} good, {bad} bad (👍 {up} · 👎 {down}).
{think}% of the samples carry its reasoning. Unsloth advises keeping at least 75%
reasoning samples so the model keeps thinking — if this is low, turn Thinking on in
Model Tuning and let more turns accumulate.

TRAIN
  pip install --upgrade unsloth unsloth_zoo
  python train_unsloth.py --dry-run          (renders everything, no GPU — check preview.txt)
  python train_unsloth.py                    (LoRA on the base below → lora/)
  python train_unsloth.py --dpo              (then a DPO pass on dpo.jsonl)
  python train_unsloth.py --gguf q8_0        (also write a GGUF for llama-server)

  Base model: {base}   (change with --base)

HARDWARE
  Unsloth's guide puts a 16-bit LoRA of the 35B-A3B MoE at ~74 GB of VRAM and does not
  recommend 4-bit (QLoRA) for these MoE models. A 16 GB card can't train it; it CAN train
  a smaller dense Qwen (Unsloth: ~10 GB for 4B, ~22 GB for 9B) with --base.

PRIVACY
  These files are this person's conversations, files and searches. Training on a rented
  cloud GPU means uploading them. Keep that in mind before you do.
"""

TRAIN_SCRIPT = r'''#!/usr/bin/env python3
"""
train_unsloth.py — fine-tune on a System 32 training export (sft.jsonl / dpo.jsonl).

  python train_unsloth.py --dry-run   render with the base model's own chat template, report
                                      lengths, write preview.txt — no GPU, no training
  python train_unsloth.py             SFT (LoRA) → ./lora
  python train_unsloth.py --dpo       SFT, then DPO on dpo.jsonl → ./lora
  python train_unsloth.py --gguf q8_0 also save a GGUF for llama-server (needs disk space)

Samples are rendered to text HERE with the model's own template (tools passed in, tool
calls as objects, reasoning kept), so the dataset never has to fit one Arrow schema.
"""
import argparse, inspect, json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name):
    p = os.path.join(HERE, name)
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def _args_as_strings(msgs):
    out = []
    for m in msgs:
        m = dict(m)
        if m.get("tool_calls"):
            m["tool_calls"] = [{**c, "function": {**c["function"],
                                "arguments": json.dumps(c["function"]["arguments"], ensure_ascii=False)}}
                               for c in m["tool_calls"]]
        out.append(m)
    return out


def _inline_thinking(msgs):
    out = []
    for m in msgs:
        m = dict(m)
        r = m.pop("reasoning_content", None)
        if r and m.get("role") == "assistant":
            m["content"] = "<think>\n" + r + "\n</think>\n\n" + (m.get("content") or "")
        out.append(m)
    return out


class Renderer:
    def __init__(self, tok):
        self.tok = tok
        self.args_str = False
        self.think = "field"

    def _raw(self, msgs, tools, gen=False):
        kw = dict(tokenize=False, add_generation_prompt=gen)
        if tools:
            kw["tools"] = tools
        m = _args_as_strings(msgs) if self.args_str else msgs
        if self.think == "inline":
            m = _inline_thinking(m)
        return self.tok.apply_chat_template(m, **kw)

    def __call__(self, msgs, tools, gen=False):
        try:
            return self._raw(msgs, tools, gen)
        except Exception:
            if self.args_str:
                raise
            self.args_str = True          # this template wants arguments as a JSON string
            return self._raw(msgs, tools, gen)

    def calibrate(self, rows):
        """Does the template keep reasoning_content? If not, fold it into the text."""
        for r in rows:
            msgs = r.get("messages") or (r.get("prompt", []) + r.get("chosen", []))
            th = next((m["reasoning_content"] for m in msgs if m.get("reasoning_content")), None)
            if th:
                probe = th.strip()[:60]
                if probe not in self(msgs, r.get("tools")):
                    self.think = "inline"
                    print("  template drops reasoning_content → putting <think> in the text")
                return


def common_prompt(a, b, marker):
    p = os.path.commonprefix([a, b])
    i = p.rfind(marker)
    return p[: i + len(marker)] if i >= 0 else p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="__BASE__")
    ap.add_argument("--max-len", type=int, default=16384)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--epochs", type=float, default=1)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--dpo", action="store_true")
    ap.add_argument("--gguf", default="")
    ap.add_argument("--load-4bit", action="store_true", help="QLoRA (not advised for MoE)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    sft, dpo = load("sft.jsonl"), load("dpo.jsonl")
    print(f"{len(sft)} SFT samples, {len(dpo)} DPO pairs · base {a.base}")

    if a.dry_run:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(a.base)
        model = None
    else:
        from unsloth import FastModel
        model, tok = FastModel.from_pretrained(model_name=a.base, max_seq_length=a.max_len,
                                               load_in_4bit=a.load_4bit,
                                               load_in_16bit=not a.load_4bit,
                                               full_finetuning=False)
    ttok = getattr(tok, "tokenizer", tok)                 # a processor wraps the tokenizer
    rtok = ttok if getattr(ttok, "chat_template", None) else tok
    R = Renderer(rtok)
    R.calibrate(sft + dpo)

    texts, too_long = [], 0
    for r in sft:
        t = R(r["messages"], r.get("tools"))
        if len(ttok(t, add_special_tokens=False)["input_ids"]) > a.max_len:
            too_long += 1
            continue
        texts.append(t)
    pairs = []
    for r in dpo:
        c = R(r["prompt"] + r["chosen"], r.get("tools"))
        j = R(r["prompt"] + r["rejected"], r.get("tools"))
        p = common_prompt(c, j, "<|im_start|>assistant\n")
        if c[len(p):] and j[len(p):] and c[len(p):] != j[len(p):]:
            pairs.append({"prompt": p, "chosen": c[len(p):], "rejected": j[len(p):]})
    print(f"rendered {len(texts)} SFT texts ({too_long} longer than --max-len skipped), "
          f"{len(pairs)} DPO pairs · reasoning: {R.think} · tool args: "
          f"{'string' if R.args_str else 'object'}")

    if a.dry_run:
        with open(os.path.join(HERE, "preview.txt"), "w", encoding="utf-8") as f:
            for t in texts[:3]:
                f.write(t + "\n\n" + "=" * 80 + "\n\n")
            for p in pairs[:2]:
                f.write("PROMPT …" + p["prompt"][-400:] + "\nCHOSEN " + p["chosen"][:1500]
                        + "\nREJECTED " + p["rejected"][:1500] + "\n\n" + "=" * 80 + "\n\n")
        print("wrote preview.txt — nothing trained (dry run)")
        return
    if not texts:
        sys.exit("nothing to train on")

    from datasets import Dataset
    from trl import SFTTrainer, SFTConfig
    from unsloth import FastModel as FM
    model = FM.get_peft_model(model, r=a.rank, lora_alpha=a.rank, lora_dropout=0, bias="none",
                              target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                                              "gate_proj", "up_proj", "down_proj"],
                              use_gradient_checkpointing="unsloth", random_state=3407)

    def cfg(cls, **kw):
        names = inspect.signature(cls.__init__).parameters
        if "max_seq_length" in kw and "max_seq_length" not in names:
            kw["max_length"] = kw.pop("max_seq_length")
        return cls(**{k: v for k, v in kw.items() if k in names})

    def trainer(cls, **kw):
        try:
            return cls(tokenizer=tok, **kw)
        except TypeError:
            return cls(processing_class=tok, **kw)

    out = os.path.join(HERE, "outputs")
    tr = trainer(SFTTrainer, model=model, train_dataset=Dataset.from_dict({"text": texts}),
                 args=cfg(SFTConfig, dataset_text_field="text", max_seq_length=a.max_len,
                          per_device_train_batch_size=1, gradient_accumulation_steps=4,
                          warmup_steps=5, num_train_epochs=a.epochs, learning_rate=a.lr,
                          logging_steps=1, optim="adamw_8bit", weight_decay=0.01,
                          lr_scheduler_type="linear", seed=3407, output_dir=out,
                          report_to="none", dataset_num_proc=1))
    if "<|im_start|>assistant\n" in texts[0]:
        from unsloth.chat_templates import train_on_responses_only
        tr = train_on_responses_only(tr, instruction_part="<|im_start|>user\n",
                                     response_part="<|im_start|>assistant\n")
    tr.train()

    if a.dpo and pairs:
        from unsloth import PatchDPOTrainer
        PatchDPOTrainer()
        from trl import DPOTrainer, DPOConfig
        dt = trainer(DPOTrainer, model=model, ref_model=None,
                     train_dataset=Dataset.from_list(pairs),
                     args=cfg(DPOConfig, beta=0.1, per_device_train_batch_size=1,
                              gradient_accumulation_steps=4, learning_rate=5e-6,
                              num_train_epochs=1, max_length=a.max_len,
                              max_prompt_length=max(512, a.max_len - 4096), logging_steps=1,
                              optim="adamw_8bit", seed=3407, output_dir=out + "_dpo",
                              report_to="none"))
        dt.train()

    model.save_pretrained(os.path.join(HERE, "lora"))
    tok.save_pretrained(os.path.join(HERE, "lora"))
    print("saved LoRA → lora/")
    if a.gguf:
        model.save_pretrained_gguf(os.path.join(HERE, "gguf"), tok, quantization_method=a.gguf)
        print("saved GGUF → gguf/  (point llama-server at it)")


if __name__ == "__main__":
    main()
'''
