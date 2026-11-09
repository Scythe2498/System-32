# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_dispatch.py — Tool dispatcher and tool result summarizer.
"""
import re
from s32_config import (
    CONFIG, MEMORY_ARCHIVE, PEOPLE_DIR, PERSON_DIR, MACHINES_DIR, private_roots, _machine_name, state, log,
    _result_lock, _job_id, _push_result, _push_done,
    os, json, datetime, base64, time, threading,
    PILImage, _senses,
)
from s32_hardware import (
    get_hardware, tool_install_missing_nodes, check_missing_nodes,
    set_preference, _get_machine_preferences,
)
from s32_comfyui import (
    run_image, run_video, run_lora, wf_run_tool, wf_list_tool, _wf_load,
    start_comfy_image,
)
from s32_filesystem import (
    fs_list, fs_find, fs_read, show_file, write_file, propose_write, sort_images,
    fs_drives, fs_sysinfo, fs_get_output_image, fs_show_images,
    smart_search, index_search, explore_tree,
    memory_write, memory_read, memory_search,
    save_directory_map, mark_favorite, get_favorites,
    visual_search, build_visual_index,
    _resolve_shortcut, _enrich_shortcuts,
    favorite_path, get_favorite_paths, remove_favorite_path,
)
try:
    import voice as _voice
except ImportError:
    _voice = None
try:
    from s32_firewall import scan_network as _scan_network, propose_action as _propose_action
except Exception:
    _scan_network = None
    _propose_action = None
try:
    from s32_web import (web_search as _web_search, web_fetch as _web_fetch,
                          web_image_search as _web_image_search,
                          web_video_search as _web_video_search)
except Exception:
    _web_search = None
    _web_fetch = None
    _web_image_search = None
    _web_video_search = None

try:
    from s32_training import export_training_data as _export_training, training_stats as _training_stats
except Exception:
    _export_training = None
    _training_stats = None

try:
    from s32_download import propose_download as _propose_download
except Exception:
    _propose_download = None

import re as _re

# Batch-intent detection. The model tends to set count>1 on its own, which shows
# up as duplicate/triplicate images the operator never asked for. We honor count>1
# ONLY when the OPERATOR's own latest message actually asked for several — otherwise
# we clamp to 1. This keeps deliberate batches (datasets, variations) working while
# stopping unsolicited multi-output.
_MULTI_WORDS = ("multiple", "several", "variation", "variations", "version", "versions",
                "batch", "a few", "options", "grid", "couple", "bunch", "set of", "many")
# A count, as digits OR words ("run five more" was clamped to 1 on 2026-10-06 —
# only digits were recognised).
_NUMW = (r'(?:[2-9]|[1-9]\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|'
         r'thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|a\s+dozen)')
_MULTI_NUM   = _re.compile(
    r'\b(?:make|generate|create|draw|render|gimme|give\s+me|do|produce|want|need|run|queue|'
    r'roll|try|spin\s+up)\s+(?:me\s+)?(?:a\s+)?(?:another\s+)?' + _NUMW + r'\b'
    r'|\banother\s+' + _NUMW + r'\b'
    r'|\b' + _NUMW + r'\s*(?:more|images?|pics?|pictures?|renders?|variations?|versions?|copies|'
    r'generations?|gens|runs?|times(?!\s+(?:bigger|larger|smaller|as|more|less|faster|slower|the))|'
    r'takes|of\s+(?:these|them|it|those))\b')


# ── Picking a specific earlier image ─────────────────────────────────────────
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
             "fourth": 4, "4th": 4, "fifth": 5, "5th": 5, "sixth": 6, "6th": 6,
             "seventh": 7, "7th": 7, "eighth": 8, "8th": 8, "ninth": 9, "9th": 9,
             "tenth": 10, "10th": 10}   # ordinals only — "a fox with two tails" isn't image #2
_STOP = {"the", "a", "an", "image", "picture", "pic", "photo", "one", "that", "this", "of",
         "with", "you", "i", "made", "generated", "created", "earlier", "from", "session",
         "in", "on", "it", "my", "your", "png", "jpg", "and", "to"}


def session_generations():
    """Images IT produced this session, oldest first (its ComfyUI-tab watcher
    entries are the operator's own work and don't count toward 'the Nth image')."""
    out, seen = [], set()
    for e in state.get("session_images") or []:
        p = e.get("path") if isinstance(e, dict) else None
        if not p or e.get("prompt") == "(ComfyUI tab)":
            continue
        k = os.path.normcase(os.path.abspath(p))
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


def _session_listing(gens):
    if not gens:
        return "No images have been generated this session yet."
    rows = [f"#{i} {os.path.basename(e['path'])}" + (f" ({e['prompt'][:40]})" if e.get("prompt") else "")
            for i, e in enumerate(gens, 1)]
    return "Images this session: " + "; ".join(rows[-12:])


def resolve_image_ref(ref):
    """'#2' / '2' / 'second' / 'last' / 'second to last' / a path / a filename / a few
    words → (path, how) or (None, error message for it)."""
    import re as _re
    r = str(ref).strip().strip('"\'').strip()
    low = r.lower()
    gens = session_generations()

    # 1) A real path.
    cand = os.path.normpath(r.replace("/", os.sep).replace("\\", os.sep))
    if os.path.isfile(cand):
        return cand, "path"

    # 2) Ordinal: "#2", "2", "#-1", "second", "image 3", "last", "second to last".
    n = None
    m = _re.fullmatch(r"(?:#|no\.?\s*|number\s*|image\s*|img\s*)?(-?\d+)", low)
    if m:
        n = int(m.group(1))
    elif _re.search(r"\b(second|2nd)[\s-]+(to|from)[\s-]+(the[\s-]+)?last\b", low) or \
            _re.search(r"\b(previous|one before)\b", low):
        n = -2
    elif _re.search(r"\b(last|latest|newest|most recent|current)\b", low):
        n = -1
    else:
        for w in _re.findall(r"[a-z0-9]+", low):
            if w in _ORDINALS:
                n = _ORDINALS[w]
                break
    if n is not None:
        if not gens:
            return None, "No images have been generated this session yet, so there's no image #%s." % n
        idx = n - 1 if n > 0 else n
        if n == 0 or not (-len(gens) <= idx < len(gens)):
            return None, f"There's no image #{n} — only {len(gens)} this session. " + _session_listing(gens)
        return gens[idx]["path"], f"session image #{idx + 1 if idx >= 0 else len(gens) + idx + 1}"

    # 3) A filename (with or without extension) — session first, then output/input folders.
    base = os.path.basename(cand).lower()
    stem = os.path.splitext(base)[0]
    if stem and _re.search(r"[._\d]", stem):
        for e in reversed(gens):
            b = os.path.basename(e["path"]).lower()
            if b == base or os.path.splitext(b)[0] == stem:
                return e["path"], "session filename"
        dirs = [CONFIG.get(k) for k in ("out_images", "comfy_image_input")]
        dirs.append(os.path.join(os.path.dirname(CONFIG.get("out_images", "") or ""), "dataset"))
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
            for root, _ds, files in os.walk(d):
                for f in files:
                    fl = f.lower()
                    if os.path.splitext(fl)[1] not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                        continue
                    if fl == base or os.path.splitext(fl)[0] == stem:
                        return os.path.join(root, f), "found on disk"

    # 4) A few describing words → best match against this session's prompts/filenames.
    words = [w for w in _re.findall(r"[a-z0-9]+", low) if w not in _STOP and len(w) > 2]
    if words and gens:
        best, best_score = None, 0
        for e in gens:                              # later wins ties → most recent
            hay = (e.get("prompt", "") + " " + os.path.basename(e["path"])).lower()
            sc = sum(1 for w in words if w in hay)
            if sc >= best_score and sc > 0:
                best, best_score = e, sc
        if best:
            return best["path"], f"best match for {' '.join(words)!r}"
    return None, (f"Couldn't find the image {r!r}. Pass a filename, '#N' for the Nth image "
                  f"generated this session (#-1 = latest), or leave image out for the current "
                  f"image. " + _session_listing(gens))


def _last_user_text(_state):
    for m in reversed(_state.get("messages", []) or []):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                c = " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            return (c or "")
    return ""


def _operator_wants_multiple():
    """True only if the operator's latest message genuinely asked for several
    images (an explicit count or a 'multiple/variations/batch' word)."""
    t = _last_user_text(state).lower()
    if not t:
        return False
    if any(w in t for w in _MULTI_WORDS):
        return True
    return bool(_MULTI_NUM.search(t))


def _clamp_count(count, name):
    """Allow count>1 only when the operator actually asked; else force 1."""
    if count > 1 and not _operator_wants_multiple():
        log(f"[S32] {name}: model requested count={count} but operator didn't ask for "
            f"multiples — clamping to 1.")
        return 1
    return count

# ── Privacy wall (2026-10-28): new computer = new person ─────────────────────
# Each person's memories/history live in reference\people\<computer>\. No tool may
# read, list, search or show ANOTHER person's folder — not by path, not in results.
def _canon(p):
    r"""Absolute, case-folded, real path — so \\?\ prefixes, 8.3 short names (SYSTEM~1),
    '..' and slash direction can't sneak past the comparison."""
    p = os.path.expandvars(os.path.expanduser(str(p).strip().strip('"')))
    if p.startswith("\\\\?\\UNC\\"):
        p = "\\\\" + p[8:]
    elif p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        p = p[4:]
    try:
        p = os.path.realpath(p)
    except Exception:
        p = os.path.abspath(p)
    return os.path.normcase(p)


def _walls():
    """[(root, mine)] canonical, plus the machine-profile rule."""
    out = []
    for root, mine in private_roots():
        if root:
            out.append((_canon(root).rstrip("\\/") + os.sep, _canon(mine).rstrip("\\/") + os.sep))
    return out


_WALLS = _walls()
_MACHINES = _canon(MACHINES_DIR).rstrip("\\/") + os.sep
_MY_MACHINE = os.path.normcase(f"{_machine_name}.md")


def _someone_elses(p):
    try:
        c = _canon(p)
    except Exception:
        return False
    cd = c.rstrip("\\/") + os.sep
    for root, mine in _WALLS:
        if cd.startswith(root) and not cd.startswith(mine):
            return True
    if c.startswith(_MACHINES) and os.path.basename(c) not in ("", _MY_MACHINE) \
            and c.rstrip("\\/") + os.sep != _MACHINES:
        return True
    return False


def _mentions_other_person(text):
    """A result string that names a path inside someone else's private area."""
    t = os.path.normcase(str(text))
    for root, mine in _WALLS + [(_MACHINES, None)]:
        i = t.find(root)
        while i != -1:
            tail = t[i + len(root):]
            if mine is None:
                if not tail.startswith(_MY_MACHINE) and re.match(r"[^\\/\s\"']+", tail):
                    return True
            elif not t.startswith(mine, i) and re.match(r"[^\\/\s\"']", tail):
                return True
            i = t.find(root, i + 1)
    return False


def _has_other(obj, depth=0):
    # Walk the real strings (json.dumps would double Windows backslashes and miss them).
    if depth > 60:
        return True                     # absurdly deep → treat as suspect, scrub it
    if isinstance(obj, str):
        return _mentions_other_person(obj)
    if isinstance(obj, dict):
        return any(_has_other(v, depth + 1) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return any(_has_other(v, depth + 1) for v in obj)
    return False


def _scrub(obj, depth=0):
    """Remove what points into another person's area: list entries are dropped, and in
    text only the offending LINES are removed (reading a file that merely mentions such
    a path no longer blanks the whole thing)."""
    if depth > 60:
        return "[private]"
    if isinstance(obj, list):
        return [_scrub(x, depth + 1) for x in obj
                if not (isinstance(x, (str, dict)) and _has_other(x))]
    if isinstance(obj, dict):
        return {k: _scrub(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, str) and _mentions_other_person(obj):
        kept = [ln for ln in obj.splitlines() if not _mentions_other_person(ln)]
        return "\n".join(kept) if kept else "[private]"
    return obj


def _all_strings(obj, depth=0):
    if depth > 20:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _all_strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _all_strings(v, depth + 1)


def _path_params(params):
    """Every string anywhere in the params that could be a path (any param name, any
    nesting — train_lora image lists, workflow overrides, …)."""
    for v in _all_strings(params or {}):
        if len(v) < 1024 and ("\\" in v or "/" in v or ":" in v or v.startswith("~")):
            yield v


_CODE_TOOLS = ("write_code", "run_code", "run_unreal_python", "import_code")
_PRIVATE_NAMES = re.compile(r"(reference material|system 32's output|save states|"
                            r"\.\.[\\/][^\n]{0,80}people)", re.I)


_FILE_READERS = ("read_file", "show_file", "fs_read")


def dispatch_tool(name, params, img_bytes=None):
    """Records every tool outcome for the feedback loop, then delegates to the
    real dispatcher. Recording is best-effort and never breaks dispatch."""
    collected = []
    # A web address handed to a FILE reader (2026-10-28: read_file("https://…") →
    # "[Errno 22] Invalid argument") is a web page — fetch it instead of failing.
    if name in _FILE_READERS and isinstance(params, dict):
        _u = str(params.get("path") or params.get("file") or params.get("url") or "").strip()
        if re.match(r"(?i)^https?://", _u):
            log(f"[S32] {name} got a web address — reading it with web_fetch instead")
            name, params = "web_fetch", {"url": _u, "max_chars": 8000}
    if any(_someone_elses(p) for p in _path_params(params)):
        log(f"[S32] Privacy wall: {name} refused — path is inside another person's folder")
        yield {"error": "That folder belongs to another person who uses this drive on a different "
                        "computer — it's private to them, so it can't be opened from here."}
        return
    if name in _CODE_TOOLS and any(_PRIVATE_NAMES.search(v) for v in _all_strings(params or {})):
        # Code runs with full file access, so code that reaches into System 32's own
        # memory/private folders is refused outright rather than trusted to behave.
        log(f"[S32] Privacy wall: {name} refused — code touches System 32's private data folders")
        yield {"error": "That code reaches into System 32's private data folders (memories, save "
                        "states, training data). Code can't touch those — work on the operator's own "
                        "files instead."}
        return
    try:
        for r in _dispatch_impl(name, params, img_bytes):
            if isinstance(r, (dict, list)) and _has_other(r):
                r = _scrub(r)
            collected.append(r)
            yield r
    finally:
        try:
            from s32_feedback import record_tool_outcome
            record_tool_outcome(name, params, collected)
        except Exception:
            pass


def _dispatch_impl(name, params, img_bytes=None):
    """Generator - yields one result dict per run. Single-run tools yield once."""
    if name in ("list_directory", "explore_tree", "read_file", "show_file") and isinstance(params, dict) \
            and params.get("path"):
        try:
            import s32_code
            _rp = s32_code.resolve_project_path(params["path"])
            if _rp:
                log(f"[S32] {name}: '{params['path']}' is her project folder → {_rp}")
                params = {**params, "path": _rp}
        except Exception:
            pass
    log(f"[S32] Tool: {name} | params: {json.dumps(params, default=str)[:200]}")

    # Track what it's doing so we can recover cleanly after a disconnect
    _now = datetime.datetime.now().strftime("%H:%M:%S")
    if name in ("list_directory", "explore_tree", "show_images",
                "smart_search", "index_search", "get_drives"):
        _folder = (params.get("path") or params.get("folder") or
                   params.get("query") or params.get("search_root") or "")
        state["nav_state"].update({
            "task":        "browsing" if name in ("list_directory", "show_images",
                           "explore_tree", "get_drives") else "searching",
            "folder":      str(_folder)[:200],
            "page":        int(params.get("page", state["nav_state"].get("page", 1))),
            "search_term": params.get("query") or params.get("pattern") or None,
            "tool":        name,
            "updated":     _now,
        })
    elif name in ("read_file", "show_file", "find_file"):
        state["nav_state"].update({
            "task":      "reading",
            "file_path": str(params.get("path", ""))[:200],
            "tool":      name,
            "updated":   _now,
        })
    elif name in ("generate_image", "generate_video", "run_workflow", "train_lora"):
        state["nav_state"].update({
            "task":        "generating",
            "folder":      None,
            "search_term": None,
            "file_path":   None,
            "tool":        name,
            "updated":     _now,
        })

    if name in ("generate_image", "generate_video", "run_workflow"):
        # Build job parameters
        if name == "run_workflow":
            _TOOL_KEYS = {"name", "target", "overrides", "count"}
            overrides = dict(params.get("overrides") or {})
            for k, v in params.items():
                if k not in _TOOL_KEYS:
                    overrides[k] = v
            # A raw path it types must never reach ComfyUI (it 400s) — but it CAN point
            # at a specific earlier image: a filename/path, "#N" (Nth image it generated
            # this session), or a few describing words. We resolve it here to a real file
            # and wf_run_tool stages it like any input. The operator's attachment for this
            # turn still wins; an unresolvable reference is an error, never a silent guess.
            _ref = None
            for _ik in ("image", "image_path", "input_image"):
                if _ik in overrides:
                    _ref = _ref or overrides.pop(_ik, None)
            if _ref and img_bytes:
                log(f"[S32][WF] Ignoring image reference {str(_ref)[:80]!r} — the operator attached an image this turn.")
            elif _ref:
                _hit, _why = resolve_image_ref(_ref)
                if not _hit:
                    yield {"type": "generated", "target": params.get("target", "image"), "count": 1,
                           "error": _why}
                    return
                overrides["_input_image"] = _hit
                log(f"[S32][WF] Image reference {str(_ref)[:80]!r} → {_hit} ({_why})")
            # A size the OPERATOR typed ("upscale that to 1024x1024") that her call left out
            # (2026-10-30: the 2nd upscale came back 2048 — she passed only the image).
            if "width" not in overrides and "height" not in overrides:
                _sz = _re.search(r"\b(\d{2,5})\s*[x×]\s*(\d{2,5})\b", _last_user_text(state).lower())
                if _sz:
                    overrides["width"], overrides["height"] = int(_sz.group(1)), int(_sz.group(2))
                    log(f"[S32][WF] Size from the operator's message: {_sz.group(1)}x{_sz.group(2)}")
            wf_name   = params.get("name", "")
            wf_target = params.get("target", "image")
            count     = _clamp_count(max(1, min(int(params.get("count", 1)), 20)), name)
            # Only require positive if the workflow has a prompt node
            if not overrides.get("positive"):
                try:
                    wf = _wf_load(wf_name)
                    _PROMPT_CLASSES = {"easy positive", "PrimitiveStringMultiline", "CLIPTextEncode"}
                    # Only block if prompt nodes exist AND are empty
                    needs_prompt = False
                    for n in wf.values():
                        if n.get("class_type") in _PROMPT_CLASSES:
                            existing = n.get("inputs", {}).get("text", "") or \
                                       n.get("inputs", {}).get("positive", "") or \
                                       n.get("inputs", {}).get("string", "")
                            if not str(existing).strip():
                                needs_prompt = True
                                break
                    if needs_prompt:
                        # Before failing, inherit the prompt from the most recent
                        # generated image. This is what makes "generate an image, THEN a
                        # video" work: the video reuses the image's scene description
                        # instead of dying on "no prompt provided". The image is already
                        # injected as the visual input; the text prompt guides the motion.
                        _inherited = ""
                        try:
                            _imgs = state.get("session_images") or []
                            if _imgs:
                                _inherited = (_imgs[-1].get("prompt") or "").strip()
                        except Exception:
                            _inherited = ""
                        if _inherited:
                            overrides["positive"] = _inherited
                            log(f"[S32][WF] No positive given — inheriting last image's prompt: {_inherited[:60]}")
                        else:
                            # Nothing to inherit. If there's an input image (attachment or
                            # current), CAPTION it with the vision model and use that as the
                            # prompt — so "attach an image → turn it into a video" works with
                            # no typed prompt. Fall back to a generic motion prompt so a video
                            # never hard-errors on a missing prompt.
                            _cap = ""
                            try:
                                _cap_b64 = None
                                if img_bytes:
                                    _cap_b64 = base64.b64encode(img_bytes).decode()
                                else:
                                    _lp = state.get("last_image_path")
                                    if _lp and os.path.isfile(_lp):
                                        with open(_lp, "rb") as _f:
                                            _cap_b64 = base64.b64encode(_f.read()).decode()
                                if _cap_b64:
                                    from s32_llm import caption_image
                                    _cap = caption_image(_cap_b64)
                            except Exception:
                                _cap = ""
                            if _cap:
                                overrides["positive"] = _cap
                                log(f"[S32][WF] No prompt given — captioned the input image: {_cap[:60]}")
                            else:
                                overrides["positive"] = "smooth natural cinematic motion, subtle realistic movement"
                                log("[S32][WF] No prompt and no caption available — using generic motion prompt")
                    else:
                        log(f"[S32][WF] No positive override - using embedded prompt from workflow")
                except Exception:
                    pass
        else:
            count     = _clamp_count(max(1, min(int(params.get("count", 1)), 20)), name)
            overrides = params
            wf_name   = None
            wf_target = "video" if name == "generate_video" else "image"

        # Fire generation in background - return immediately so chat stays responsive
        _run_out = {}   # the ACTUAL result of this run (first run), read by the sync path below

        def _bg_generate(wf_name, overrides, wf_target, count, img_bytes, job_id):
            with state["gen_lock"]:
                state["active_workflow"] = wf_name or wf_target
                # Save the image path that existed before this batch started
                pre_batch_image = state.get("last_image_path")
                # Runs that outlive its turn (batches, videos) show in the input bar
                # with a ✕ (system32.py pollDownloads → /api/downloads "batch").
                _bg_visible = count > 1 or wf_target == "video"
                _label = (wf_name or ("video" if wf_target == "video" else "image")).replace(".json", "")
                _done = 0
                for i in range(count):
                    if state.get("gen_cancel"):
                        _push_result({"content": (f"✗ Batch cancelled — {_done} of {count} finished."
                                                  if count > 1 else "✗ Run cancelled."),
                                      "path": "⬡ ComfyUI"}, job_id)
                        log(f"[S32] Generation cancelled ({_done}/{count} finished)")
                        _run_out.setdefault("result", {"error": "Cancelled by the operator.",
                                                       "cancelled": True})
                        break
                    if _bg_visible:
                        state["bg_gen"] = {"name": _label, "done": _done, "total": count,
                                           "video": wf_target == "video"}
                    if count > 1:
                        log(f"[S32] Run {i+1}/{count}")
                        state["job_status"] = f"Run {i+1}/{count}..."
                        # Clear image inject between runs so each generates independently
                        # Only the first run uses the pre-existing last image (if any)
                        if i > 0:
                            state["last_image_path"] = None
                    try:
                        if wf_name:
                            # Pass the operator's attached image straight into the workflow
                            # (only on the first run of a batch). Named workflows previously
                            # relied ONLY on last_image_path, which can be cleared/stale — so an
                            # attached image silently didn't reach bg-remove/upscale/img2video/3D.
                            result = wf_run_tool(wf_name, overrides, wf_target,
                                                 img_bytes if i == 0 else None)
                        elif wf_target == "video":
                            result = run_video(overrides, img_bytes)
                        else:
                            result = run_image(overrides, img_bytes)
                        _run_out.setdefault("result", result)
                        if isinstance(result, dict) and result.get("cancelled"):
                            _push_result({"content": (f"✗ Batch cancelled — {_done} of {count} finished."
                                                      if count > 1 else "✗ Run cancelled."),
                                          "path": "⬡ ComfyUI"}, job_id)
                            log(f"[S32] Generation cancelled ({_done}/{count} finished)")
                            break                           # one note, not N errors
                        _push_result(result, job_id)
                        _done += 1
                    except Exception as e:
                        _run_out.setdefault("result", {"error": str(e)})
                        _push_result({"error": str(e)}, job_id)
                state["job_status"] = None
                state["active_workflow"] = None
                state["bg_gen"] = None
                _push_done(job_id)

        import s32_config as _cfg
        with _result_lock:
            _cfg._job_id += 1
            current_job_id = _cfg._job_id

        _gen_thread = threading.Thread(
            target=_bg_generate,
            args=(wf_name, overrides, wf_target, count, img_bytes, current_job_id),
            daemon=True
        )
        _gen_thread.start()

        total = count
        desc  = overrides.get("positive", "")[:60] if isinstance(overrides, dict) else ""

        # A SINGLE image generation is fast and usually feeds a follow-up step
        # (image→video, img2img). Wait for it to finish HERE so the chain has the
        # saved file + last_image_path BEFORE it advances — the same synchronous
        # handoff that makes web-image→video reliable. VIDEO generations stay async
        # (slow + terminal). BATCHES (count>1) also stay ASYNC: a 20-image batch
        # blows past any join timeout (the old join(180) silently expired mid-batch,
        # told it "done", and the chain kept spawning more) — and batches display
        # per-image via the background result push, which is the path that worked.
        # A batch is terminal: nothing chains off "generate N images".
        _is_video = (wf_target == "video") or (name == "generate_video")
        if not _is_video and count == 1:
            _gen_thread.join(timeout=600)
            # Report what THIS run actually produced — never the global last_image_path.
            # Reading the global made a failed run (bg-remove 400) report an old image as
            # "saved", and a 3D run report its INPUT image as the output; its summary then
            # told it "the image is finished" either way.
            _res = _run_out.get("result")
            if _res is None:
                yield {"type": "generated", "target": wf_target, "count": total, "prompt": desc,
                       "error": "Generation is still running (took over 10 minutes) — it will "
                                "appear in chat when done.", "job_id": current_job_id}
                return
            if isinstance(_res, dict) and _res.get("error"):
                # The error itself is already shown via the background feed; this copy is
                # typed so the UI doesn't print it twice, but its summary sees the failure.
                _fail = {"type": "generated", "target": wf_target, "count": total,
                         "prompt": desc, "error": _res["error"], "job_id": current_job_id}
                for _k in ("missing_nodes", "auto_install"):
                    if _k in _res:
                        _fail[_k] = _res[_k]
                yield _fail
                return
            _outs = (_res.get("outputs") if isinstance(_res, dict) and _res.get("type") == "multi"
                     else [_res]) or []
            _outs = [o for o in _outs if isinstance(o, dict)]
            _saved = next((o.get("saved") for o in _outs if o.get("saved")), None)
            _done_res = {"type": "generated", "target": wf_target, "count": total,
                         "prompt": desc, "saved": _saved, "path": _saved, "job_id": current_job_id,
                         "outputs": [{"type": o.get("type"), "saved": o.get("saved")} for o in _outs]}
            if isinstance(overrides, dict) and overrides.get("_input_image"):
                _done_res["input_image"] = os.path.basename(overrides["_input_image"])
            yield _done_res
            return

        state["messages"].append({
            "role": "assistant",
            "content": f"[Generation running in background: {count}x '{desc}']"
        })
        yield {"type": "queued", "target": wf_target, "count": total, "prompt": desc, "job_id": current_job_id}
        return
    # Single-result tools
    if name == "train_lora":      yield run_lora(params); return
    if name == "add_to_dataset":  yield {"need_images": True, "dataset": params.get("dataset","")}; return
    if name == "list_directory":  yield fs_list(params.get("path", "C:\\")); return
    if name == "find_file":
        pattern = params.get("pattern", "")
        # For specific filenames, try reading directly from known dirs first
        bare = pattern.replace("*","").strip()
        if bare and not any(c in bare for c in " ?["):
            result = fs_read(bare)
            if "content" in result:
                yield result
                return
        found = fs_find(pattern, params.get("search_root"), images_only=params.get("images_only", False))
        results = found.get("results", [])
        # If one result, or all results are the same filename - read the first one
        if results:
            names = set(os.path.basename(r["path"]) for r in results)
            if len(results) == 1 or len(names) == 1:
                yield fs_read(results[0]["path"])
                return
        yield _enrich_shortcuts(found)
        return
    if name == "resolve_shortcut":
        yield _resolve_shortcut(params.get("path", "")); return
    if name == "read_file":       yield fs_read(params.get("path","")); return
    if name == "show_file":       yield show_file(params.get("path",""), params.get("offset", 0), params.get("chunk", 12000)); return
    if name == "file_operation":
        import s32_filesystem as _fsx
        _paths = params.get("paths")
        if _paths is None:
            _paths = params.get("path") or params.get("source") or params.get("sources") or []
        yield _fsx.propose_file_op(params.get("action", ""), _paths, params.get("destination") or params.get("dest") or "",
                                   params.get("filter", ""), bool(params.get("recursive")), params.get("new_name", ""),
                                   steps=params.get("steps"))
        return
    if name == "write_file":
        yield propose_write(
            params.get("path", ""),
            params.get("content", ""),
            params.get("description", "")
        )
        return
    if name == "sort_images":
        yield sort_images(
            params.get("directory", "") or params.get("path", ""),
            params.get("categories")
        )
        return
    if name == "smart_search":
        yield _enrich_shortcuts(smart_search(
            params.get("query", ""),
            params.get("search_root"),
            params.get("images_only", False)
        ))
        return
    if name == "index_search":
        yield index_search(params.get("query", ""))
        return
    if name == "explore_tree":
        yield explore_tree(
            params.get("path","home"),
            depth=params.get("depth", 2),
            images_only=params.get("images_only", False)
        )
        return
    if name == "get_drives":      yield fs_drives(); return
    if name == "get_system_info": yield fs_sysinfo(); return
    if name == "scan_network":
        yield (_scan_network() if _scan_network else {"error": "Network guardian unavailable."}); return
    if name == "propose_security_action":
        if not _propose_action:
            yield {"error": "Network guardian unavailable."}; return
        yield _propose_action(params.get("action", "kill"), pid=params.get("pid"),
                              name=params.get("name"), port=params.get("port"),
                              reason=params.get("reason", "")); return
    if name == "web_search":
        yield (_web_search(params.get("query", ""), params.get("max_results", 6))
               if _web_search else {"error": "Web access unavailable."}); return
    if name == "web_fetch":
        yield (_web_fetch(params.get("url", ""), params.get("max_chars", 6000))
               if _web_fetch else {"error": "Web access unavailable."}); return
    if name == "web_image_search":
        yield (_web_image_search(params.get("query", ""), params.get("max_results", 12))
               if _web_image_search else {"error": "Web access unavailable."}); return
    if name == "find_similar_on_computer":
        # The same matching on the operator's own pictures — nothing goes online.
        try:
            from s32_similar import find_similar_on_computer as _fsc
        except Exception as e:
            yield {"error": f"Image matching unavailable: {e}"}; return
        yield _fsc(params.get("image"), params.get("folders"), params.get("mode", "similar"),
                   params.get("closeness", "similar"), params.get("recursive", True),
                   params.get("max_results", 40), img_bytes=img_bytes); return
    if name == "find_similar_images":
        # Private reverse image search: only WORDS go out; the picture is compared here.
        try:
            from s32_similar import find_similar_images as _fsi
        except Exception as e:
            yield {"error": f"Image matching unavailable: {e}"}; return
        yield _fsi(params.get("image"), params.get("queries"),
                   params.get("max_results", 12), img_bytes=img_bytes); return
    if name == "web_video_search":
        yield (_web_video_search(params.get("query", ""), params.get("max_results", 8))
               if _web_video_search else {"error": "Web access unavailable."}); return
    if name == "export_training_data":
        if not _export_training:
            yield {"error": "Training pipeline unavailable."}; return
        yield _export_training(fmt=params.get("format") or "unsloth"); return
    if name == "download_file":
        if not _propose_download:
            yield {"error": "Download engine unavailable."}; return
        yield _propose_download(params.get("url", ""), params.get("kind"),
                                params.get("filename"), params.get("subfolder")); return
    if name == "whitelist_ports":
        try:
            import s32_irondome
            yield s32_irondome.whitelist_target(params.get("target", ""))
        except Exception as e:
            yield {"error": f"Iron Dome unavailable: {e}"}
        return
    if name == "game_input":
        try:
            import s32_gameinput
            res = s32_gameinput.game_input(
                params.get("action", ""), key=params.get("key"),
                dx=params.get("dx", 0), dy=params.get("dy", 0),
                x=params.get("x"), y=params.get("y"),
                button=params.get("button", "left"),
                duration_ms=params.get("duration_ms", 60))
            if res.get("ok") and params.get("action") in (
                    "press", "hold", "move", "move_to", "click"):
                try:
                    import s32_gameplay
                    s32_gameplay.record_step({k: params.get(k) for k in
                        ("action", "key", "button", "dx", "dy", "x", "y")
                        if params.get(k) is not None}, True)
                except Exception:
                    pass
            yield res
        except Exception as e:
            yield {"error": f"Game input unavailable: {e}"}
        return
    if name == "review_lessons":
        try:
            from s32_feedback import review_lessons as _review
            yield _review()
        except Exception as e:
            yield {"error": f"Feedback unavailable: {e}"}
        return
    if name == "list_workflows":  yield wf_list_tool(); return
    if name == "spawn_actor":
        import s32_unreal
        yield s32_unreal.spawn_actor(
            params.get("shape", "cube"),
            params.get("x", 0.0), params.get("y", 0.0), params.get("z", 200.0),
            asset=params.get("asset"),
            asset_class=params.get("asset_class"),
            name=params.get("name"),
            relative_to=params.get("relative_to"),
            direction=params.get("direction", "front"),
            distance=params.get("distance", 200.0))
        return
    if name == "search_computer":
        import s32_findex
        yield s32_findex.search_computer(params.get("query", ""), params.get("kind", "any"),
                                         params.get("max_results"))
        return
    if name == "import_code":
        import s32_code
        yield s32_code.import_code(params.get("path", ""), params.get("project", ""), params.get("goal", ""))
        return
    if name == "search_code":
        import s32_code
        yield s32_code.search_code(params.get("query", ""), params.get("path", ""), params.get("max_results", 25))
        return
    if name == "compare_code":
        import s32_code
        yield s32_code.compare_code(params.get("a", ""), params.get("b", ""),
                                    params.get("label_a", ""), params.get("label_b", ""))
        return
    if name == "code_plan":
        import s32_code
        yield s32_code.code_plan(params.get("project", ""), params.get("goal"), params.get("steps"),
                                 params.get("done"), params.get("doing"), params.get("note"),
                                 params.get("next"), bool(params.get("close")))
        return
    if name == "edit_code":
        import s32_code
        yield s32_code.edit_code(params.get("project", ""), params.get("file") or params.get("filename") or params.get("path", ""),
                                 params.get("old_string", ""), params.get("new_string", ""),
                                 bool(params.get("replace_all")), params.get("purpose", ""))
        return
    if name == "write_code":
        import s32_code
        yield s32_code.write_code(params.get("project", ""), params.get("filename", ""),
                                  params.get("code", ""), params.get("purpose", ""),
                                  bool(params.get("run")), params.get("input", ""))
        return
    if name == "run_code":
        import s32_code
        yield s32_code.run_code(params.get("project", ""), params.get("filename", ""),
                                params.get("args", ""), params.get("input", ""))
        return
    if name == "run_unreal_python":
        import s32_unreal
        yield s32_unreal.run_unreal_python(params.get("code", ""), params.get("purpose", ""))
        return
    if name == "get_scene":
        import s32_unreal
        yield s32_unreal.get_scene()
        return
    if name == "move_actor":
        import s32_unreal
        yield s32_unreal.move_actor(
            params.get("target", ""),
            params.get("dx", 0.0), params.get("dy", 0.0), params.get("dz", 0.0),
            params.get("x"), params.get("y"), params.get("z"))
        return
    if name == "delete_actor":
        import s32_unreal
        yield s32_unreal.delete_actor(params.get("target", ""))
        return
    if name == "find_asset":
        import s32_unreal
        yield s32_unreal.find_asset(params.get("query", ""), params.get("kind"))
        return
    if name == "restart_comfyui":
        from s32_comfyui import restart_comfyui as _restart_comfy
        yield _restart_comfy("requested", wait=True)
        return
    if name == "scan_comfy_logs":
        check_missing_nodes()
        yield {"status": "scanned", "missing": state.get("missing_nodes", {})}
        return
    if name == "install_missing_nodes":
        yield tool_install_missing_nodes(params.get("target"))
        return
    if name == "get_output_image":
        # PROTECT USER ATTACHMENTS: when the operator attaches an image, it becomes
        # last_image_path (that attachment is what they want worked on). fs_get_output_image
        # sets last_image_path to whatever it fetches — which was silently REPLACING the
        # operator's attachment with an old generated image, so workflows ran against the
        # wrong picture. If the current image is a user attachment, put it back afterward:
        # an explicit attachment always outranks a browse of past outputs.
        _pre_attach = None
        try:
            _lp0 = state.get("last_image_path") or ""
            if os.path.basename(_lp0).startswith("s32_attached_") and os.path.isfile(_lp0):
                _pre_attach = _lp0
        except Exception:
            _pre_attach = None
        result = fs_get_output_image(
            params.get("position", 1),
            params.get("folder", "images"),
            params.get("source", "output")
        )
        if _pre_attach:
            try:
                state["last_image_path"] = _pre_attach
                log(f"[S32] get_output_image: restored operator attachment as current image ({os.path.basename(_pre_attach)})")
            except Exception:
                pass
        # Fallback: if the session lookup came back empty but a current image DOES
        # exist (it just generated one), hand that back instead of a confusing
        # "no images in this session" error — that error was derailing the
        # generate→use chain and sending it into a retry loop.
        if isinstance(result, dict) and result.get("error") and params.get("source") == "session":
            try:
                import os as _os
                _lp = state.get("last_image_path")
                if _lp and _os.path.isfile(_lp):
                    result = {"type": "image", "path": _lp, "note": "current generated image"}
            except Exception:
                pass
        yield result
        return
    if name == "show_images":
        yield fs_show_images(
            paths=params.get("paths"),
            folder=params.get("folder"),
            count=params.get("count", 12),
            random_sample=params.get("random_sample", False),
            page=params.get("page", 1)
        )
        return
    if name == "memory_write":
        yield memory_write(params.get("content", ""), permanent=params.get("permanent", False))
        return
    if name == "memory_search":
        yield memory_search(params.get("query", ""))
        return
    if name == "memory_read":
        # Prevent memory read loops - limit to 2 reads per session
        state["memory_read_count"] = state.get("memory_read_count", 0) + 1
        if state["memory_read_count"] > 2:
            yield {"note": "Memory already read this session. Your memory is injected in the system prompt - use it from there rather than reading again."}
            return
        yield memory_read(params.get("tier", "all"))
        return
    if name == "memory_archive_read":
        week = params.get("week", "")
        if week:
            path = os.path.join(MEMORY_ARCHIVE, f"{week}.md")
            if os.path.exists(path):
                yield {"content": open(path, "r", encoding="utf-8").read(), "week": week}
            else:
                # List available archives
                archives = [f[:-3] for f in os.listdir(MEMORY_ARCHIVE) if f.endswith(".md")]
                yield {"error": f"No archive for {week}", "available": sorted(archives)}
        else:
            archives = [f[:-3] for f in os.listdir(MEMORY_ARCHIVE) if f.endswith(".md")] if os.path.exists(MEMORY_ARCHIVE) else []
            yield {"available_archives": sorted(archives), "tip": "Pass week='YYYY-MM-weekN' to read a specific archive"}
        return
    if name == "remember_operator":
        from s32_memory import remember_operator as _rem_op
        yield _rem_op(params.get("name", ""))
        return
    if name == "set_preference":
        result = set_preference(
            params.get("key", ""),
            params.get("value", ""),
            params.get("scope", "machine"),
            params.get("extra")
        )
        # Do NOT invalidate prompt cache - would cause state break mid-conversation
        # Preferences apply to generation calls immediately via _get_machine_preferences()
        yield result
        return
    if name == "save_map":
        yield save_directory_map()
        return
    if name == "mark_favorite":
        yield mark_favorite(params.get("path",""), params.get("reason",""))
        return
    if name == "get_favorites":
        yield get_favorites()
        return
    if name == "favorite_path":
        yield favorite_path(params.get("path", ""), params.get("label", ""), params.get("reason", ""))
        return
    if name == "get_favorite_paths":
        yield get_favorite_paths()
        return
    if name == "remove_favorite_path":
        yield remove_favorite_path(params.get("path", ""))
        return
    if name == "visual_search":
        yield visual_search(
            params.get("path",""),
            top_n=params.get("top_n", 12),
            threshold=params.get("threshold", 15)
        )
        return
    if name == "build_visual_index":
        yield build_visual_index(force=params.get("force", False))
        return
    if name == "get_system_state":
        if _senses:
            yield _senses.get_snapshot_dict()
        else:
            yield {"error": "Senses module not loaded."}
        return
    if name == "watch_path":
        if _senses:
            action = params.get("action", "add")
            path   = params.get("path", "")
            if action == "remove":
                yield _senses.remove_watched_path(path)
            else:
                yield _senses.add_watched_path(path)
        else:
            yield {"error": "Senses module not loaded."}
        return
    if name == "see_screen":
        if _senses:
            yield _senses.see_screen(int(params.get("frames", 1)))
        else:
            yield {"error": "Senses module not loaded."}
        return
    if name == "start_screen_capture":
        if _senses:
            yield _senses.start_screen_capture(
                fps=int(params.get("fps", 2)),
                buffer_size=int(params.get("buffer_size", 10))
            )
        else:
            yield {"error": "Senses module not loaded."}
        return
    if name == "stop_screen_capture":
        if _senses:
            yield _senses.stop_screen_capture()
        else:
            yield {"error": "Senses module not loaded."}
        return
    if name == "get_screen_status":
        if _senses:
            yield _senses.get_screen_status()
        else:
            yield {"error": "Senses module not loaded."}
        return
    if name == "speak":
        if _voice:
            yield _voice.speak(params.get("text", ""), priority=params.get("priority", False))
        else:
            yield {"error": "Voice module not loaded."}
        return
    if name == "stop_voice":
        if _voice:
            yield _voice.stop_voice()
        else:
            yield {"error": "Voice module not loaded."}
        return
    if name == "list_voices":
        if _voice:
            yield _voice.list_voices()
        else:
            yield {"error": "Voice module not loaded."}
        return
    if name == "set_voice":
        if _voice:
            yield _voice.set_voice(
                params.get("voice_name") or params.get("voice", ""),
                speed=params.get("speed"),
                volume=params.get("volume"),
            )
        else:
            yield {"error": "Voice module not loaded."}
        return
    if name == "get_voice_status":
        if _voice:
            yield _voice.get_voice_status()
        else:
            yield {"error": "Voice module not loaded."}
        return
    if name == "enable_voice":
        if _voice:
            yield _voice.enable_voice(params.get("enabled", True))
        else:
            yield {"error": "Voice module not loaded."}
        return
    if name == "save_model_settings":
        from s32_hardware import save_model_settings
        yield save_model_settings(
            params.get("model_path", CONFIG.get("lmstudio_model_path", "")),
            params.get("settings", {})
        )
        return
    if name == "get_model_settings":
        from s32_hardware import get_model_settings
        yield get_model_settings(
            params.get("model_path", CONFIG.get("lmstudio_model_path", ""))
        )
        return
    yield {"error": f"Unknown tool: {name}"}

# ── LLM ───────────────────────────────────────────────────────────────────────

_prompt_cache = {"text": None, "ts": 0, "locked": False}

