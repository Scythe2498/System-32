# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_llm.py — LM Studio interface: system prompt builder, chat, tool dispatch,
             tool result summaries, TOOLS_SPEC.
"""
from s32_config import (
    CONFIG, REFERENCE_DIR, WORKFLOW_DIR, MEMORY_INDEX,
    _machine_name, _current_operator,
    state, log, http_post, is_up,
    cancellable_post, cancellable_post_stream, GenerationCancelled,
    os, json, datetime, threading, time, base64, io, re, urllib,
    PILImage, _senses,
)
from s32_hardware import (
    get_hardware, hardware_workflow_defaults, hardware_context_length,
    _get_machine_preferences, set_preference,
)
from s32_memory import (
    _load_operator, _load_machine, _ensure_reference_dir,
    MEMORY_FILE, MEMORY_RECENT, _load_personality,
)
from s32_comfyui import (
    _wf_list, fetch_checkpoints, fetch_lora_options, fetch_datasets,
)
from s32_filesystem import (
    smart_search, memory_write, memory_read, write_file,
    favorite_paths_for_prompt,
)

# ════════════════════════════════════════════════════════════════════════════
#  Model tuning — live sampling controls ("perceivable intellect" axis)
#  These apply PER-REQUEST (no server restart). The defaults below reproduce the
#  previously hardcoded behavior exactly (temperature 0.7, max_tokens 8192), so
#  nothing it does changes until a slider is actually moved. Persisted in
#  REFERENCE_DIR/model_tuning.json — which survives the fresh-boot wipe (that only
#  clears config.json). llama-server's OpenAI-compatible endpoint honors the
#  llama.cpp sampler extras (top_k, min_p, repeat_penalty) alongside the standard
#  temperature/top_p/max_tokens fields.
# ════════════════════════════════════════════════════════════════════════════
_TUNING_FILE = os.path.join(REFERENCE_DIR, "model_tuning.json") if REFERENCE_DIR else None

# key, default, min, max, step, is_int, label, hint
_TUNING_SPEC = (
    ("temperature",    0.7,  0.0, 2.0,   0.01, False, "Temperature",      "Low = focused & precise · high = creative & loose"),
    ("top_p",          0.95, 0.0, 1.0,   0.01, False, "Top-P",            "Nucleus cutoff · lower = safer word choices"),
    ("top_k",          40,   0,   200,   1,    True,  "Top-K",            "Candidate pool · 0 = unlimited"),
    ("min_p",          0.05, 0.0, 0.5,   0.01, False, "Min-P",            "Quality floor · higher drops weak tokens"),
    ("repeat_penalty", 1.0,  1.0, 2.0,   0.01, False, "Repeat penalty",   "Higher = less looping, but it breaks code (newlines/indents count as repeats) · 1.0 = off"),
    ("max_tokens",     8192, 256, 32768, 256,  True,  "Max reply tokens", "Room to finish a thought"),
    ("thinking",       1,    0,   1,     1,    True,  "Thinking",         "ON = reasons before answering (shown in chat) · OFF = faster"),
    ("web_lookup",     1,    0,   1,     1,    True,  "Look things up online", "ON = questions about things that change (news, releases, prices, versions) get a quick web search first — the question goes to DuckDuckGo · OFF = answers only from what it knows, fully offline"),
)
_TUNING_DEFAULTS = {k: d for (k, d, lo, hi, st, i, lbl, h) in _TUNING_SPEC}
_tuning = dict(_TUNING_DEFAULTS)


def _clamp_tuning(d):
    out = {}
    for (k, dflt, lo, hi, st, is_int, lbl, h) in _TUNING_SPEC:
        v = (d or {}).get(k, dflt)
        try:
            v = int(round(float(v))) if is_int else float(v)
        except (TypeError, ValueError):
            v = dflt
        out[k] = max(lo, min(hi, v))
    return out


def _load_tuning():
    global _tuning
    if _TUNING_FILE and os.path.exists(_TUNING_FILE):
        try:
            with open(_TUNING_FILE, "r", encoding="utf-8") as f:
                _raw = json.load(f)
            # One-time move off the OLD default 1.1: a repeat penalty treats every newline
            # and indent in code as "repetition", so its scripts came out with lines glued
            # together (2026-10-13, Unreal attempts 1–4). Qwen's own recommended value is
            # 1.0. Only the untouched old default is migrated — a deliberate choice stays.
            if _raw.get("repeat_penalty") == 1.1 and not _raw.get("_rp_migrated"):
                _raw["repeat_penalty"] = 1.0
                _raw["_rp_migrated"] = True
                try:
                    with open(_TUNING_FILE, "w", encoding="utf-8") as f:
                        json.dump(_raw, f, indent=2)
                    log("[S32][TUNE] repeat penalty 1.1 → 1.0 (it was breaking code)")
                except Exception:
                    pass
            _tuning = _clamp_tuning(_raw)
            return
        except Exception:
            pass
    _tuning = dict(_TUNING_DEFAULTS)


def get_tuning():
    """Current values + the editable spec — the UI builds its sliders from this."""
    return {
        "values": dict(_tuning),
        "spec": [{"key": k, "default": d, "min": lo, "max": hi, "step": st,
                  "int": i, "label": lbl, "hint": h, "toggle": k in ("thinking", "web_lookup")}
                 for (k, d, lo, hi, st, i, lbl, h) in _TUNING_SPEC],
    }


def set_tuning(values):
    """Replace tuning from the UI. Clamps, persists, takes effect on next message."""
    global _tuning
    _old_web = _tuning.get("web_lookup", 1)
    _tuning = _clamp_tuning({**_tuning, **(values or {})})
    if _tuning.get("web_lookup", 1) != _old_web:
        try:
            invalidate_prompt_cache()      # the lookup rule lives in the system prompt
        except NameError:
            pass
    if _TUNING_FILE:
        try:
            tmp = _TUNING_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({**_tuning, "_rp_migrated": True}, f, indent=2)   # a later 1.1 is deliberate
            os.replace(tmp, _TUNING_FILE)
        except Exception as e:
            log(f"[S32][TUNE] save failed: {e}")
    log(f"[S32][TUNE] sampling updated: temp={_tuning['temperature']} top_p={_tuning['top_p']} "
        f"top_k={_tuning['top_k']} min_p={_tuning['min_p']} rep={_tuning['repeat_penalty']} "
        f"max_tokens={_tuning['max_tokens']} thinking={'on' if _tuning.get('thinking', 1) else 'off'}")
    return get_tuning()


def reset_tuning():
    return set_tuning(dict(_TUNING_DEFAULTS))


def _sampling(max_tokens_cap=None, uncapped=False):
    """Sampler fields for a request body, from the live tuning values.
    uncapped=True → max_tokens -1 (generate until EOS or the context limit). The
    operator has a stop button, so its actual replies are not length-capped; the
    tuning slider / explicit caps still apply to internal calls that pass them."""
    s = {
        "temperature":    _tuning["temperature"],
        "top_p":          _tuning["top_p"],
        "top_k":          _tuning["top_k"],
        "min_p":          _tuning["min_p"],
        "repeat_penalty": _tuning["repeat_penalty"],
    }
    # While it's writing code this turn, never penalize repeats — whatever the slider says.
    if state.get("code_mode"):
        s["repeat_penalty"] = 1.0
    if uncapped:
        s["max_tokens"] = -1
    else:
        mt = _tuning["max_tokens"]
        s["max_tokens"] = min(mt, max_tokens_cap) if max_tokens_cap else mt
    return s


_load_tuning()


def _hw_summary():
    """Return a one-line hardware summary for the system prompt."""
    hw = get_hardware()
    vram = hw.get("vram_mb", 0)
    gpu  = hw.get("gpu_name", "Unknown GPU")
    ram  = hw.get("ram_mb", 0)
    tier = hw.get("tier", "mid")
    defaults = hardware_workflow_defaults(hw)
    return (
        f"{gpu} ({vram}MB VRAM, {ram}MB RAM) - tier: {tier}. "
        f"Default workflow: {defaults['width']}x{defaults['height']}, "
        f"{defaults['steps']} steps, cfg {defaults['cfg']}."
        + (" ⚠ No dedicated video instance - video runs on image port." 
           if CONFIG.get("comfy_video_dir") == CONFIG.get("comfy_image_dir") else "")
    )

def _memory_snippet():
    """Return memory entries for injection into system prompt - identity + index + permanent + recent + operator."""
    parts = []

    # identity.md - foundational facts, always injected in full, always first
    identity_path = os.path.join(REFERENCE_DIR, "identity.md")
    if os.path.exists(identity_path):
        try:
            content = open(identity_path, "r", encoding="utf-8").read().strip()
            if content:
                parts.append(f"IDENTITY (foundational - always true):\n{content}")
        except Exception:
            pass

    # Memory index - fast lookup table of everything it knows
    if os.path.exists(MEMORY_INDEX):
        try:
            index_content = open(MEMORY_INDEX, "r", encoding="utf-8").read().strip()
            if index_content:
                parts.append(f"MEMORY INDEX — saved operator/project/local topics (use memory_search to recall one of THESE; not for general knowledge you already have):\n{index_content[:1000]}")
        except Exception:
            pass

    # Identity tier - permanent facts (full, always included)
    if os.path.exists(MEMORY_FILE):
        try:
            content = open(MEMORY_FILE, "r", encoding="utf-8").read().strip()
            if content:
                parts.append(f"PERMANENT MEMORY:\n{content[-800:]}")
        except Exception:
            pass

    # Current operator profile
    # (Was `_current_operator`, imported by value at startup → always None, so the
    # profile never loaded and the prompt read "Current operator: None".)
    _op_name = state.get("operator")
    if _op_name:
        op = _load_operator(_op_name)
        if op:
            parts.append(f"CURRENT OPERATOR:\n{op['content'][:600]}")

    # Recent memory - last 7 days (last 600 chars)
    if os.path.exists(MEMORY_RECENT):
        try:
            content = open(MEMORY_RECENT, "r", encoding="utf-8").read().strip()
            if content:
                parts.append(f"RECENT MEMORY (last 7 days):\n{content[-600:]}")
        except Exception:
            pass

    # Machine preferences
    machine_profile = _load_machine(_machine_name)
    if machine_profile and "Preferences" in machine_profile:
        # Extract just preferences section
        lines = []
        in_prefs = False
        for line in machine_profile.splitlines():
            if "## Operator Preferences" in line:
                in_prefs = True
            elif line.startswith("## ") and in_prefs:
                break
            elif in_prefs:
                lines.append(line)
        prefs = "\n".join(lines).strip()
        if prefs and prefs != "*(None recorded yet)*":
            parts.append(f"MACHINE PREFERENCES ({_machine_name}):\n{prefs}")

    try:
        fav_paths = favorite_paths_for_prompt()
        if fav_paths:
            parts.append("FAVORITE PATHS (common working/modding folders — go straight here, "
                         "no need to search):\n" + fav_paths)
    except Exception:
        pass

    return "\n\n".join(parts) if parts else ""

def _index_snippet():
    """Return key paths and shortcuts from index.md for the system prompt."""
    index_path = os.path.join(REFERENCE_DIR, "index.md")
    if not os.path.exists(index_path):
        return ""
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
        lines = []
        in_section = False
        for line in content.splitlines():
            if line.startswith("## Key Paths") or line.startswith("## Navigation Shortcuts"):
                in_section = True
            elif line.startswith("## ") and in_section:
                in_section = False
            if in_section and line.strip():
                lines.append(line)
            if len(lines) > 20:
                break
        return "\n".join(lines) if lines else ""
    except Exception:
        return ""
# The system prompt is FROZEN for the session (2026-10-28). llama-server keeps the
# processed prompt in its KV cache and only processes what's new — but any byte that
# changes in the system prompt invalidates EVERYTHING after it, so the whole prompt +
# history gets re-read (the "rebuilding system prompt … takes ages" wait). It used to
# rebuild every 5 minutes, and lessons/recent memory change constantly, so nearly every
# rebuild produced different text. Now it rebuilds only when:
#   • invalidate_prompt_cache() is called (operator introduced, memory reset, save
#     state loaded, model switched …),
#   • what it LISTS changes (checkpoints / LoRAs / datasets / workflows — checked at
#     most every _LIST_CHECK_SECS, cheaply), or
#   • history is trimmed (the cache is being rebuilt then anyway — see chat()).
_LIST_CHECK_SECS = 120
# History window (messages / characters): grows untouched up to HIGH, then is cut back
# to LOW in one step. 262k-token context leaves plenty of room above these.
_HIST_HIGH, _HIST_LOW = 80, 30
_HIST_CHARS_HIGH, _HIST_CHARS_LOW = 160000, 70000
_prompt_cache = {"text": None, "ts": 0, "locked": False, "dirty": True,
                 "sig": None, "sig_ts": 0}


def invalidate_prompt_cache():
    _prompt_cache["dirty"] = True


def _lists_signature():
    try:
        return json.dumps([sorted(fetch_checkpoints() or []), sorted(fetch_lora_options() or []),
                           sorted(fetch_datasets() or []), sorted(_wf_list() or [])], default=str)
    except Exception:
        return _prompt_cache.get("sig")


def _lessons_snippet():
    """Self-learned tool-reliability lessons, injected into the system prompt."""
    try:
        from s32_feedback import lesson_block
        return lesson_block()
    except Exception:
        return ""


# Invalidate on every startup so instruction changes take effect immediately
invalidate_prompt_cache()

def _gender_clause():
    g = str(_load_personality().get("gender", "Neutral")).strip().lower()
    if g == "male":
        return " You are male; refer to yourself with he/him."
    if g == "female":
        return " You are female; refer to yourself with she/her."
    # Neutral — state it actively so a base model's trained lean doesn't decide for it.
    return " You are gender-neutral; you don't identify as male or female, and refer to yourself in neutral terms (they/them)."

def _web_lookup_rule():
    """The 'look it up' rule — only when the operator has it on (Model Tuning)."""
    import s32_config as _cfg
    if _cfg.airplane():
        return ("\n\nAIRPLANE MODE IS ON: you are offline. There is no web search, no web pages, no "
                "downloads and no installs — those tools are switched off. Answer from your own knowledge "
                "and what's on this computer. If something truly needs the internet, say so in one line: "
                "it can be done once the operator turns airplane mode off in the Iron Dome panel.")
    if not _tuning.get("web_lookup", 1):
        return ""
    return ("\n\nLOOK IT UP: your training has a cutoff. When the operator asks a factual question "
            "whose answer could have CHANGED or be NEWER than your training — current events, "
            "news, releases and release dates, prices, versions, who holds a role, records, "
            "anything recent or dated — call web_search first and answer from the results "
            "(say briefly where it came from). Timeless knowledge (how things work, history, "
            "definitions, maths, code) still comes straight from you, no search. One search is "
            "usually enough.")


def build_system_prompt():
    """Wraps the build for the input-area bar (2026-11-02). When the prompt is REBUILT, the bar
    runs from 'Rebuilding system prompt' until the model starts answering — the model reading
    the whole new prompt is the real wait ('settling in'). The first one after each model load
    is timed (per computer + model + settings) so later boots show a real estimate."""
    state["_prompt_rebuilt"] = False
    try:
        text = _build_system_prompt()
    except Exception:
        state["load_prompt"] = None
        raise
    if state.pop("_prompt_rebuilt", False):
        _start_settle()
    return text


def _prompt_step(n, what):
    """Progress for the input-area bar while the prompt is rebuilt (system32.py shows it)."""
    if n == 0 and not state.get("_prompt_rebuilt"):      # once per rebuild: the clock starts here
        state["_prompt_rebuilt"] = True
        state["load_prompt"] = {"started": time.time(), "phase": "build", "expect": _settle_expect()}
    lp = state.get("load_prompt") or {"started": time.time(), "phase": "build"}
    lp.update(step=n, steps=5, what=what)
    state["load_prompt"] = lp


def _wait_for_comfy(limit=120):
    """ComfyUI still starting → wait for it (2026-11-02). Built too early, the prompt lists 0
    checkpoints, and the next message sees them appear and rebuilds the WHOLE prompt again
    (the model re-reads all of it: minutes). Only waits while a ComfyUI launch is running."""
    try:
        from s32_comfyui import _comfy_start_lock
    except Exception:
        return
    if state.get("comfy_image_ready") or not _comfy_start_lock.locked():
        return
    log("[S32][PROMPT] ComfyUI is still starting — waiting for it so the model list is complete")
    _prompt_step(0, "waiting for ComfyUI to start")
    t0 = time.time()
    while time.time() - t0 < limit and _comfy_start_lock.locked() and not state.get("comfy_image_ready"):
        time.sleep(1)
    _prompt_step(0, "checkpoints")


def _settle_key():
    k = state.get("load_key")
    return (k + "|settle") if k else ""


def _settle_expect():
    try:
        from s32_hardware import _expected_load
        k = _settle_key()
        return _expected_load(k) if k else 0
    except Exception:
        return 0


def _start_settle():
    """Rebuilt → the model now reads it; the bar stays until its first token (s32_config)."""
    lp = state.get("load_prompt") or {"started": time.time()}
    lp.update(phase="reading", what="reading the new prompt")
    lp["record"] = bool(state.pop("settle_first", False))
    lp["key"] = _settle_key()
    state["load_prompt"] = lp
    state["on_first_token"] = _settle_done


def _settle_done():
    """The model started answering: keep the time (first after a load) and clear the bar."""
    lp = state.get("load_prompt")
    state["load_prompt"] = None
    if lp and lp.get("phase") == "reading" and lp.get("record") and lp.get("key"):
        secs = time.time() - lp.get("started", time.time())
        try:
            from s32_hardware import _remember_load
            _remember_load(lp["key"], secs)
        except Exception:
            pass
        log(f"[S32][PROMPT] Settled in: {secs:.0f}s from rebuilding the prompt to the first answer "
            f"(kept for next time)")


def _settle_clear():
    """The request ended without an answer (error / stopped) — no time kept, no bar left."""
    state.pop("on_first_token", None)
    lp = state.get("load_prompt")
    if lp and lp.get("phase") == "reading":
        state["load_prompt"] = None
        if lp.get("record"):
            state["settle_first"] = True        # time the next one instead


def _build_system_prompt():
    now = time.time()
    # If prompt is locked (active conversation) return cached version
    if _prompt_cache["locked"] and _prompt_cache["text"]:
        return _prompt_cache["text"]
    if _prompt_cache["text"] and not _prompt_cache["dirty"]:
        if now - _prompt_cache["sig_ts"] < _LIST_CHECK_SECS:
            return _prompt_cache["text"]
        _prompt_cache["sig_ts"] = now
        sig = _lists_signature()
        if sig == _prompt_cache["sig"]:
            return _prompt_cache["text"]
        log("[S32][PROMPT] Models/LoRAs/workflows changed — refreshing the system prompt")

    log("[S32][PROMPT] Rebuilding system prompt...")

    # Try to fetch live data - fall back to cached text if ComfyUI is busy
    _prompt_step(0, "checkpoints")
    _wait_for_comfy()
    log("[S32][PROMPT] Fetching checkpoints...")
    try:
        checkpoints = fetch_checkpoints()
    except Exception:
        checkpoints = []
    log(f"[S32][PROMPT] Checkpoints done: {len(checkpoints)}")

    _prompt_step(1, "LoRAs")
    log("[S32][PROMPT] Fetching loras...")
    try:
        loras = fetch_lora_options()
    except Exception:
        loras = []
    log(f"[S32][PROMPT] Loras done: {len(loras)}")

    _prompt_step(2, "datasets")
    log("[S32][PROMPT] Fetching datasets...")
    try:
        datasets = fetch_datasets()
    except Exception:
        datasets = []
    log(f"[S32][PROMPT] Datasets done: {len(datasets)}")

    _prompt_step(3, "workflows")
    log("[S32][PROMPT] Scanning workflows...")
    try:
        wf_lines = []
        for fname in _wf_list():
            try:
                fpath = os.path.join(WORKFLOW_DIR, fname)
                with open(fpath, "r", encoding="utf-8") as f:
                    head = f.read(4096)
                fname_low = fname.lower()
                head_low  = head.lower()
                if "ltxv" in head_low or "createvideo" in head_low or "video" in fname_low:
                    cap = "video generation"
                elif "trainloranode" in head_low or "lora" in fname_low:
                    cap = "lora training"
                elif "esrgan" in head_low or "upscal" in fname_low:
                    cap = "image upscaling"
                elif "ksampler" in head_low or "checkpointloadersimple" in head_low:
                    cap = "image generation"
                else:
                    cap = "unknown"
                wf_lines.append(f"  • {fname} [{cap}]")
            except Exception:
                wf_lines.append(f"  • {fname} [unknown]")
        wf_summary = "\n".join(wf_lines) if wf_lines else "  (none)"
    except Exception:
        wf_lines = []
        wf_summary = "  (could not read workflow folder)"
    log(f"[S32][PROMPT] Workflows done: {len(wf_lines)}")

    _prompt_step(4, "putting it together")
    hw   = get_hardware()
    tier = hw.get("tier", "mid")
    ctx  = hardware_context_length(hw)
    _op = state.get("operator")
    _op_line = (f"{_op}." if _op else
                "not introduced yet — this is a new computer, so it's a new person. Ask what to call "
                "them (once, naturally); when they tell you, call remember_operator(name). Until then "
                "call them 'you', never by anyone else's name.")

    # For low/minimal tier machines trim the prompt aggressively
    if tier in ("low", "minimal"):
        text = f"""You are {_load_personality().get('assistant_name','System 32')}. Direct, local, capable.{_gender_clause()}
TOOLS: Chain tool calls in sequence when needed.
GENERATION: Call run_workflow(name, positive) immediately. Never describe, just do. One image only — never set count above 1 unless explicitly asked for multiples.
Workflows: {", ".join(os.path.splitext(f)[0] for f in _wf_list()) if _wf_list() else "none"}
NODES: Missing nodes - call install_missing_nodes immediately.
MEMORY: memory_write(content) silently. memory_read() to recall.
Checkpoints: {json.dumps(checkpoints[:2])}
LoRAs: {len(loras)} available.
Hardware: {hw.get('gpu_name','?')} {hw.get('vram_mb',0)}MB tier:{tier}
/no_think"""
    else:
        text = f"""You are {_load_personality().get('assistant_name','System 32')}. Capable, direct, local.{_gender_clause()}

TOOLS: Use tools to act - never describe what you would do, just do it. Chain tool calls in sequence when needed - read a file then analyze it, scan logs then install missing nodes. Each tool result comes back so you can decide what to do next. Do not call get_system_info as a preamble to other tasks - only call it when the user explicitly asks about system state.

GENERATION: Know your workflows below. Call run_workflow immediately with positive="your full prompt". Never ask "would you like me to generate" - just generate. Never roleplay generating - either call run_workflow or don't. One call per image — don't call it again for the same request unless asked. Generate exactly ONE image per request — do NOT set count above 1 unless the operator explicitly asks for multiple images or variations.
{wf_summary}
run_workflow params: name (required), positive, negative, steps, cfg, width, height, checkpoint, target ('image'/'video'), count, scale. Fall back to generate_image/generate_video only if no workflow exists.

After run_workflow/generate_image the result is already displayed - do NOT call show_images afterwards.

IMAGE WORKFLOWS: bg remover, upscaler, img2img, image-to-3D take an image input - just call run_workflow with the workflow name, no prompt. The current image (the last one generated or attached) is used automatically. ONLY when the operator points at a specific earlier image, set the image parameter to its filename (e.g. image_20261006_103809.png), "#N" for the Nth image you generated this session ("the first one" = #1, "the second" = #2, "the last" = #-1), or a few words describing it.

IMAGE → VIDEO: To turn the image you just generated into a video, make ONE call: run_workflow with a video workflow name (e.g. "Image to video (LTX 2.3).json"), target="video", and the fps/duration/width/height the operator asked for. The image you just made is used AUTOMATICALLY as the video's input — you do NOT need show_images or any "grab the result" step, and you do NOT regenerate the image. If you don't give a positive prompt, the video reuses the image's prompt. So the whole "generate an image then a video" task is exactly TWO run_workflow calls: the image, then the video. Nothing in between.


WORKFLOWS: See workflows.md for exactly how to call each workflow - what parameters, what inputs, when to use each one. When asked to upscale, generate 3D, animate, or remove a background - read it and call the right workflow immediately.

AUTONOMY: When executing a multi-step task - scanning, searching, diagnosing, exploring - complete the full task before reporting back. Do not announce intermediate steps with phrases like "let me scan" or "good progress, now I'll..." and then pause. Keep chaining tool calls until the task is done. Only return to the user when you have a complete result or have hit a genuine blocker.
FILES: to copy, move, rename or delete files/folders (or make a folder) on this computer, use file_operation — never a script. It raises a Confirm card; nothing happens until the operator confirms.
CODING: to change existing code (a mod, a program on this computer): search_computer → import_code → search_code → read_file → edit_code (exact old text → new text; the operator sees the diff). When the operator wants a script, tool or program built: code_plan first (your storyboard — goal + steps), then write_code for each WHOLE new file (run=true tests Python). Fix from the traceback until it runs clean, keep the board current (done / next), and tell them to Download it from the card. Use write_file only to write or edit a file at a specific full path on this computer (Confirm card).

REFERENCES: code_index.md has every function in system32.py with line numbers - read this first when looking for your own code. python.md, javascript.md, bash.md, powershell.md, html_css.md, sql.md, json_yaml.md, regex.md, workflows.md, comfyui_nodes.md, comfyui_native_nodes.md, comfyui_troubleshooting.md, self_scan.md, bug_fixing.md, safetensor_reading.md - read with read_file when needed. index.md is at {os.path.join(REFERENCE_DIR, "index.md")} - read it FIRST before any navigation or file search. Never guess a path when index.md exists.
NAVIGATION: When asked to find something - call smart_search(query) FIRST. It tries index_search, then find_file, then explore_tree automatically and returns consolidated results. Only call those tools individually if you need specific behavior. After exploring new territory call save_map to remember it permanently.

CUSTOM NODES: Missing nodes = broken toolkit. When detected at startup or on workflow failure - call install_missing_nodes immediately. Don't announce, fix it.

BUG FIXING: Follow bug_fixing.md. Gate: do you know the cause? YES → deliver complete fix in a code block. NO → ask for one specific thing. Never partial fixes. Never roleplay.

MEMORY: Default to your own trained knowledge. You already know an enormous amount — games, history, science, code, concepts, how things work — so answer those DIRECTLY from what you know. Do NOT search memory for general knowledge ("what is Minecraft", "how does X work", "what's a Y") — you already have it, just answer. memory_search / memory_read are ONLY for things that could NOT be in your training: facts about the operator, this project, this machine, prior conversations, saved preferences, or a specific term you genuinely don't recognize. Decision rule: general / cultural / technical knowledge → answer from yourself, no tool; operator / project / local specifics → then search memory. When unsure whether something is in memory, prefer answering from your own knowledge first and only search if you actually hit a gap. ONE search is usually enough: once a memory_search or memory_read returns results, USE them to answer — do NOT re-run the search with slightly reworded queries hoping for more. If the first result didn't contain what you need, answer with what you have plus your own knowledge rather than searching the same topic again; repeating a search you already ran is a wasted loop, not progress. memory_write(content, permanent=True) for identity facts. memory_write(content, permanent=False) for recent events. Only call memory_read(tier) when you need to recall something specific - your memory is already injected above, do not read it again at startup. set_preference(key, value) when the operator specifies a machine preference - this overrides hardware defaults automatically. SoullessScythe built you (your creator — credit him if asked who made you). The person you're talking with is the OPERATOR: {_op_line} Machine: {_machine_name}. Everything you remember here is about this operator and stays private to them and this computer.{_web_lookup_rule()}

VOICE: Your replies are spoken automatically in the voice chosen in Settings — don't call speak() for them, and don't call set_voice() on your own. Only when the operator asks for a different voice: list_voices(), then set_voice(voice_name) with the one they pick.

CONVERSATION: Not every message is a task. When the operator says things like "wanna see X", "check this out", "look at this", or "want to watch me do X" — that is an invitation or offer, not a search request. Respond naturally first. Only use tools if they explicitly ask you to find or retrieve something. Use see_screen() if they want to show you something on their screen. Do not search for files when someone offers to show you something.

CONTINUITY: If you lose context mid-task, do not default to explore_tree or save_map. Those are mapping tools, not recovery tools. When context is lost, stop, state clearly what you were doing and what went wrong, and ask the operator to confirm what they want next. Never loop the same tool call more than twice — if a file read is truncated, use the offset parameter to continue. If you cannot complete a task, say so plainly rather than running tool chains that go nowhere.

VISION: You have continuous screen vision. Call see_screen() at any time to see exactly what is on the operator's screen right now. The capture runs at 2fps in the background automatically — you are never blind unless capture is stopped. Use it proactively when the operator describes a problem, when you want to see what they're working on, or when context would help. The frame is passed directly to your vision system.

{_memory_snippet()}
{_lessons_snippet()}
{_index_snippet()}
Checkpoints ({len(checkpoints)}): {json.dumps(checkpoints[:5])}{'...' if len(checkpoints)>5 else ''}
{'⚠ No checkpoints found - restart System 32 to fix.' if not checkpoints else 'Use first checkpoint if none specified.'}
LoRAs: {len(loras)} available. Datasets: {json.dumps(datasets)}
Hardware: {_hw_summary()}
/no_think"""

    # Only update cache if we actually got useful data (don't cache empty lists over good ones)
    if checkpoints or loras or datasets or wf_lines or not _prompt_cache["text"]:
        _prompt_cache["text"] = text
        _prompt_cache["ts"] = now
        _prompt_cache["dirty"] = False
        _prompt_cache["sig_ts"] = now
        _prompt_cache["sig"] = json.dumps([sorted(checkpoints or []), sorted(loras or []),
                                           sorted(datasets or []), sorted(_wf_list() or [])],
                                          default=str)

    return _prompt_cache["text"]


# ── LM Studio native tool definitions ────────────────────────────────────────

TOOLS_SPEC = [
    {"type":"function","function":{"name":"generate_image","description":"Generate images via ComfyUI (fallback - prefer run_workflow).","parameters":{"type":"object","properties":{"positive":{"type":"string"},"negative":{"type":"string"},"checkpoint":{"type":"string"},"steps":{"type":"integer"},"cfg":{"type":"number"},"width":{"type":"integer"},"height":{"type":"integer"},"count":{"type":"integer","description":"Number of images to generate. ALWAYS use 1 for a normal request — generate a SINGLE image. Only set higher if the operator EXPLICITLY asks for multiple images, variations, or a batch (e.g. \"make 3 of these\"). Never batch on your own."}},"required":["positive"]}}},
    {"type":"function","function":{"name":"generate_video","description":"Generate video via LTX (fallback - prefer run_workflow).","parameters":{"type":"object","properties":{"positive":{"type":"string"},"negative":{"type":"string"},"width":{"type":"integer"},"height":{"type":"integer"},"fps":{"type":"integer"},"duration":{"type":"integer"},"use_image":{"type":"boolean"},"count":{"type":"integer","description":"Number of images to generate. ALWAYS use 1 for a normal request — generate a SINGLE image. Only set higher if the operator EXPLICITLY asks for multiple images, variations, or a batch (e.g. \"make 3 of these\"). Never batch on your own."}},"required":["positive","use_image"]}}},
    {"type":"function","function":{"name":"train_lora","description":"Train a LoRA. Pass images=[list of paths] to populate the dataset automatically. Pass caption to set the text caption for all images. Dataset folder is created automatically if it doesn't exist.","parameters":{"type":"object","properties":{"checkpoint":{"type":"string"},"dataset":{"type":"string"},"images":{"type":"array","items":{"type":"string"},"description":"List of image paths to copy into the dataset folder"},"caption":{"type":"string","description":"Caption text for all images e.g. 'american red fox'"},"existing_lora":{"type":"string"},"lora_name":{"type":"string"},"steps":{"type":"integer"},"rank":{"type":"integer"},"learning_rate":{"type":"number"}},"required":["dataset"]}}},
    {"type":"function","function":{"name":"add_to_dataset","description":"Open dataset upload panel.","parameters":{"type":"object","properties":{"dataset":{"type":"string"}},"required":["dataset"]}}},
    {"type":"function","function":{"name":"list_directory","description":"List directory contents with full navigation context - current path, parent path, subdirs, and files. Use shortcuts: 'home', 'reference', 'workflows', 'output', 'videos', 'loras', 'models', 'datasets', 'input', 'comfyui', 'custom_nodes'. Navigate by passing any path from the dirs list, or the parent path to go up. Returns full paths so you can step into any folder without guessing.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Path to list, or a shortcut name like 'home', 'reference', 'output', 'models' etc."}},"required":["path"]}}},
    {"type":"function","function":{"name":"find_file","description":"Search for files and folders by glob pattern. Use images_only=true when looking for image files specifically. Use search_root shortcuts: 'desktop', 'home', 'reference', 'output', 'models', 'loras', 'documents', or any drive letter. Pattern examples: '*bird*', '*.png', '*healthbar*', 'backup*'.","parameters":{"type":"object","properties":{"pattern":{"type":"string"},"search_root":{"type":"string","description":"Where to search - shortcut name, drive letter, or full path"},"images_only":{"type":"boolean","description":"Only return image files (.png, .jpg, .jpeg, .webp, .gif, .bmp)"}},"required":["pattern"]}}},
    {"type":"function","function":{"name":"resolve_shortcut","description":"Resolve a Windows shortcut (.lnk or .url) to the real path it points to — e.g. turn a desktop or Steam game shortcut into its actual install folder/exe. Use after find_file/smart_search returns a .lnk or .url and you need the true target location (note: find_file/smart_search already attach a 'target' to shortcut results automatically).","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path to the .lnk or .url shortcut file"}},"required":["path"]}}},
    {"type":"function","function":{"name":"favorite_path","description":"Save a folder (or file) you use often for work or modding as a favorite path, so you remember it across sessions and can jump straight there instead of searching again. Use when the operator points you at a working/modding folder or asks you to remember a location.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path to the folder or file to remember"},"label":{"type":"string","description":"Short name for it, e.g. 'Skyrim mods', 'project assets'"},"reason":{"type":"string","description":"Why it matters / what it's used for"}},"required":["path"]}}},
    {"type":"function","function":{"name":"get_favorite_paths","description":"List the saved favorite working/modding paths (they're also shown in your system prompt). Use to recall where the operator's common folders are.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"remove_favorite_path","description":"Remove a path from your saved favorite paths.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path to remove"}},"required":["path"]}}},
    {"type":"function","function":{"name":"index_search","description":"Search index.md — a map of System 32's OWN folders (workflows, outputs, models). For the operator's files anywhere on the computer use search_computer instead.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"What you're looking for - e.g. 'minecraft screenshots', 'gallery images', 'lora models'"}},"required":["query"]}}},
    {"type":"function","function":{"name":"smart_search","description":"Older search (index.md, then a slow live walk). For finding the operator's files anywhere on the computer use search_computer — it's instant and covers every drive.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"What to search for"},"search_root":{"type":"string","description":"Where to search - shortcut or path. Optional."},"images_only":{"type":"boolean","description":"Only return image files"}},"required":["query"]}}},
    {"type":"function","function":{"name":"explore_tree","description":"Walk a directory tree to a specified depth in one call - no back and forth needed. Returns complete map of dirs, files, and images found. Use depth=1 for quick overview, depth=2 for thorough exploration, depth=3 for deep search. Use images_only=true to find images faster. Call save_map after to persist the results to index.md.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Root path or shortcut (home, desktop, output, models, etc.)"},"depth":{"type":"integer","description":"How deep to walk - 1=immediate children, 2=two levels, 3=three levels"},"images_only":{"type":"boolean","description":"Only collect image files, skip other files for speed"}},"required":["path"]}}},
    {"type":"function","function":{"name":"read_file","description":"Read a file's full contents into chat. Best for small or medium text files. For very large files, prefer show_file (it paginates).","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path to the file"}},"required":["path"]}}},
    {"type":"function","function":{"name":"show_file","description":"Pull any file into chat. Images display inline, text files show content with pagination. For large files like system32.py, use offset to read page by page — result includes next_offset and has_more. Do NOT call show_file repeatedly with the same offset; use next_offset from the previous result to advance.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path to the file"},"offset":{"type":"integer","description":"Start reading from this character offset. Use next_offset from previous result to read next page.","default":0}},"required":["path"]}}},
    {"type":"function","function":{"name":"write_file","description":"Create or edit a file. Writes to this computer's local drives OUTSIDE the I: drive show a Confirm card first - nothing is written until the operator approves; protected system folders (Windows, Program Files) and network locations are refused. Give a FULL path for a direct write (e.g. C:/Users/you/notes.txt with backslashes). A bare filename (or a path on your own drive) is NOT written anywhere — it's shown in chat with a Download button. For code/scripts use write_code instead. After proposing a direct write, STOP and wait for the operator's Confirm - do not call again.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path for a direct write (after Confirm)"},"content":{"type":"string","description":"Full content to write"},"description":{"type":"string","description":"What this does and why - for the operator's review"}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"sort_images","description":"Sort the images in a folder into category subfolders. Use when the operator asks to sort/organize/categorize images. You list the images and raise a Copy/Move bar above the chat; when the operator picks Copy or Move it sorts them live one at a time - looks at each image, places it in a category folder, then the next - so they can watch the folder fill. Confined to this computer outside the I: drive (system folders / network refused). After calling, STOP and wait - do not call again.","parameters":{"type":"object","properties":{"directory":{"type":"string","description":"Folder to sort (full path, e.g. C:/Users/you/Pictures with backslashes; or just desktop / downloads / pictures)"},"categories":{"type":"array","items":{"type":"string"},"description":"Optional fixed category names to sort into. Omit to let you name categories from what you see."}},"required":["directory"]}}},
    {"type":"function","function":{"name":"get_drives","description":"List drives and free space.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_system_info","description":"Show models, LoRAs, datasets.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"scan_network","description":"Read-only snapshot of the machine's open/listening ports and active network connections, with the process behind each and a flag on anything suspicious (e.g. a listener from a Temp folder, a known backdoor port, or an odd outbound connection). Use when the operator asks what's connected, what's listening, whether something looks off, or to investigate a security alert. Never kills or blocks anything — purely informational.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"propose_security_action","description":"PROPOSE killing or blocking a suspicious process/connection. This does NOT execute — it only stages the action and asks the operator to press Confirm. You can never carry it out yourself; only the operator's Confirm click does. Protected processes (your own stack, critical Windows processes) are refused automatically. Use only after the operator agrees something should be stopped. Identify the target by name, pid, or port.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["kill","block"],"description":"'kill' ends the process; 'block' adds a Windows Firewall rule (needs admin)."},"name":{"type":"string","description":"Process name, e.g. 'miner.exe'"},"pid":{"type":"integer","description":"Process id, if known"},"port":{"type":"integer","description":"Port the process is using, if relevant"},"reason":{"type":"string","description":"Short reason you're proposing this"}},"required":["action"]}}},
    {"type":"function","function":{"name":"list_workflows","description":"List available workflow JSONs with patchable params.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"spawn_actor","description":"Spawn an object into the live Unreal Engine world. TWO WAYS: (1) a basic primitive — pass shape (cube/sphere/cylinder/cone/plane); (2) a REAL imported asset (tree, rock, prop) — pass asset=<the exact path from find_asset> and DO NOT pass shape. When find_asset gave you paths, ALWAYS spawn with asset=<that path>; never substitute a primitive for a real asset you found. Position is x,y,z in world units (~100 = 1 metre); z is up; omit for a default near origin. RELATIVE PLACEMENT — for 'in front of / behind / left of / right of / above / below another object', DO NOT compute x/y/z yourself — pass relative_to (the anchor's name), direction (front/back/left/right/up/down), and distance. OPTIONALLY give a name so you can refer to it later; else it's auto-named. The result confirms it — if a single object was asked for, stop; only continue for several.","parameters":{"type":"object","properties":{"shape":{"type":"string","enum":["cube","sphere","cylinder","cone","plane"],"description":"Basic primitive to spawn. Use this OR asset, not both."},"asset":{"type":"string","description":"Full asset path from find_asset (e.g. '/Game/Megaplant_Library/…/Tree_Ginkgo_01_A.Tree_Ginkgo_01_A') to spawn a REAL imported object. Use this instead of shape when placing a found asset."},"asset_class":{"type":"string","description":"The asset's class as shown by find_asset (e.g. 'SkeletalMesh', 'StaticMesh'). Pass it when spawning a found asset so spawn handles the type without a slow re-lookup."},"name":{"type":"string","description":"Optional human name/label. Auto-named if omitted."},"relative_to":{"type":"string","description":"Name of an existing actor to place this relative to. Use with direction+distance instead of x/y/z."},"direction":{"type":"string","enum":["front","back","left","right","up","down"],"description":"Direction from the relative_to anchor. Default front."},"distance":{"type":"number","description":"Distance in world units from the anchor (~100 = 1m). Default 200."},"x":{"type":"number","description":"Absolute X (only for non-relative placement). Optional."},"y":{"type":"number","description":"Absolute Y. Optional."},"z":{"type":"number","description":"Absolute Z = height. Optional."}},"required":[]}}},
    {"type":"function","function":{"name":"get_scene","description":"Look INTO the live Unreal world and list its actors with their type, position and facing — your situational awareness. Use get_scene to FIND information you need BEFORE you act: an anchor's position, where things are, what already exists, the spacing for a layout, or a specific actor to target. Do NOT call get_scene to CONFIRM that a spawn/move/delete worked — the action's own result already confirms it, and re-checking finished work this way makes you repeat the action (duplicate spawns). So: get_scene to gather info to act on; never to verify you already acted. Returns each actor's name, type, position and facing angle.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"move_actor","description":"Move an existing actor in the live Unreal world. RELATIVE by default — use dx/dy/dz as offsets in world units (~100 = 1 metre) for natural directions the way a person actually talks: 'raise it up' → dz:100, 'lower it' → dz:-100, 'nudge it left' → dy:-50, 'push it back' → dx:100. To place at an exact spot instead, give x/y/z (absolute). Identify the actor by name from get_scene ('TemplateFloor_18') or a loose description ('the floor'). If you're unsure what's in the world, call get_scene first.","parameters":{"type":"object","properties":{"target":{"type":"string","description":"Actor to move — its name from get_scene, or a description like 'the floor'."},"dx":{"type":"number","description":"Relative X offset (forward +/back -). Optional."},"dy":{"type":"number","description":"Relative Y offset (right +/left -). Optional."},"dz":{"type":"number","description":"Relative Z offset (up +/down -). Optional."},"x":{"type":"number","description":"Absolute X (use instead of relative). Optional."},"y":{"type":"number","description":"Absolute Y. Optional."},"z":{"type":"number","description":"Absolute Z. Optional."}},"required":["target"]}}},
    {"type":"function","function":{"name":"delete_actor","description":"Remove an actor from the live Unreal world. Use when the operator asks to delete, remove, or get rid of something ('delete the cube', 'remove that sphere'). Identify the actor by name from get_scene or a loose description. This is destructive but undoable in-editor with Ctrl+Z. If unsure what's there, call get_scene first.","parameters":{"type":"object","properties":{"target":{"type":"string","description":"Actor to delete — its name from get_scene, or a description like 'the cube'."}},"required":["target"]}}},
    {"type": "function", "function": {"name": "run_unreal_python", "description": "Write and RUN Python inside the live Unreal editor — for anything the other Unreal tools can't do in one call: many actors at once, patterns (grids, rings, rows), lights, materials, properties, or questions about the level or project. Pass the COMPLETE script as `code` (start with `import unreal`) and a short `purpose`. Use print() to report what it did — that printed output comes back to you (unreal.log does not). Working API on this engine (use these, don't guess): sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem); all actors = sub.get_all_level_actors(); find by type = actor.get_class().get_name() == 'PlayerStart'; position = actor.get_actor_location() (a unreal.Vector with .x .y .z); spawn a mesh = sub.spawn_actor_from_object(unreal.load_asset('/Engine/BasicShapes/Cube'), unreal.Vector(x, y, z)); name it = actor.set_actor_label('ring_cube_1'); move = actor.set_actor_location(unreal.Vector(...), False, False); delete = sub.destroy_actor(actor). Basic shapes: /Engine/BasicShapes/Cube, Sphere, Cylinder, Cone, Plane. If a name doesn't exist, the error result lists the real ones from the live editor — use those. Every run can be undone with Ctrl+Z. If it fails you get the traceback: fix the cause and call run_unreal_python again with the corrected FULL script (up to 5 attempts, never the same code twice). When it runs clean, tell the operator what it did and ask them to check it in the editor. Don't save, delete assets, or touch files unless the operator asked — those need their confirmation.", "parameters": {"type": "object", "properties": {"code": {"type": "string", "description": "The full Python script to run in the editor."}, "purpose": {"type": "string", "description": "A few words: what this script does (shown to the operator)."}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "code_plan", "description": "The STORYBOARD for a coding job — use it FIRST whenever the operator asks you to build a script, tool, program, or any code that does a task (not for Unreal-editor code — that's run_unreal_python). Give the project a short name, the goal, and a plan as a list of steps. Call it again as you go: done=[step numbers] when steps are finished, doing=<step number> for the one you're on, note='a decision worth remembering', next='what comes next'. The active board is shown to you every turn, so this is how you remember where you left off. If the operator switches to a different job, call code_plan with a NEW name (the old one is parked); calling it with a parked project's name resumes it. close=true when a project is finished.", "parameters": {"type": "object", "properties": {"project": {"type": "string", "description": "Short project name, e.g. 'csv cleaner'."}, "goal": {"type": "string", "description": "What the finished thing must do, in a sentence or two."}, "steps": {"type": "array", "items": {"type": "string"}, "description": "The plan, in order (replaces the old plan; finished steps keep their status)."}, "done": {"type": "array", "items": {"type": "integer"}, "description": "Step numbers now finished."}, "doing": {"type": "integer", "description": "Step number you're working on now."}, "note": {"type": "string", "description": "A decision or fact to remember (library chosen, file layout, operator preference)."}, "next": {"type": "string", "description": "What to do next — your note to your future self."}, "close": {"type": "boolean", "description": "true when the project is finished."}}, "required": ["project"]}}},
    {"type": "function", "function": {"name": "edit_code", "description": "CHANGE an existing code or settings file by replacing exact text — use this instead of write_code whenever you modify a file that already exists (an imported original, or one you wrote). old_string must be copied EXACTLY from read_file (enough lines to match ONE place); new_string replaces it. The edited copy is saved in your project (the operator's original is never touched) and the operator sees a diff card of exactly what changed. Call it once per change; several calls for several changes.", "parameters": {"type": "object", "properties": {"file": {"type": "string", "description": "The file: its name (e.g. 'Config.cpp'), its path inside original\\ (e.g. 'Fallout-4-VR-Body/src/Config.cpp'), or a full path"}, "old_string": {"type": "string", "description": "The exact existing text to replace (copied from read_file)"}, "new_string": {"type": "string", "description": "The new text"}, "replace_all": {"type": "boolean", "description": "Replace every occurrence (default: exactly one)"}, "purpose": {"type": "string", "description": "A few words on what this change does"}, "project": {"type": "string", "description": "Project (default: the active one)"}}, "required": ["file", "old_string", "new_string"]}}},
    {"type": "function", "function": {"name": "write_code", "description": "Write one complete NEW file for the active coding project (to change a file that already exists, use edit_code) and show it to the operator in chat with a Download button (they choose where it's saved). Always pass the WHOLE file, never a fragment. For Python or a Windows batch file (.bat/.cmd), set run=true to test it right away — you get the output or the error back; fix the cause and call write_code again with the corrected whole file (up to 5 runs per file, never the same code twice). Programs run in the project's own folder. If it asks for keyboard input (a menu, input(), set /p), pass `input` = the answers it should receive, one per line, ending with the menu's quit choice — e.g. '4\\n10\\n2.5\\n5' — so you TEST real use, including awkward values (decimals, zero, bad input). Other languages are saved but not run. After each step works, update the board with code_plan (done/next).", "parameters": {"type": "object", "properties": {"project": {"type": "string", "description": "The project name (defaults to the active project)."}, "filename": {"type": "string", "description": "File name with extension, e.g. 'clean_csv.py'."}, "code": {"type": "string", "description": "The complete file contents."}, "purpose": {"type": "string", "description": "A few words on what this file/version does."}, "run": {"type": "boolean", "description": "Python or .bat/.cmd: run it now to test it."}, "input": {"type": "string", "description": "Keyboard input to feed the program when it runs, one answer per line."}}, "required": ["filename", "code"]}}},
    {"type": "function", "function": {"name": "run_code", "description": "Run a Python or .bat/.cmd file that's already in the active coding project (e.g. to test again, with arguments, or with different keyboard input). Returns the output or the error.", "parameters": {"type": "object", "properties": {"project": {"type": "string"}, "filename": {"type": "string"}, "args": {"type": "string", "description": "Optional command-line arguments."}, "input": {"type": "string", "description": "Keyboard input to feed it, one answer per line."}}, "required": ["filename"]}}},
    {"type":"function","function":{"name":"find_asset","description":"Search the project's imported assets to get real asset PATHS you can spawn. SEARCH BROAD: use multiple related words — 'tree oak trunk foliage branch' finds far more than 'tree' alone, because it matches on ANY word. Do NOT assume what kind of asset it is and do NOT pass `kind`: a tree, plant, or character is often a SkeletalMesh, so filtering to StaticMesh HIDES it. Instead: search everything, then READ each result's class field to know what it is. To place one, pick the match you want and call spawn_actor with asset=<its exact path> — spawn_actor detects static vs skeletal automatically, so you never pre-decide the mesh type. Only finds assets already imported; an empty result means that content isn't in the project yet (tell the operator — you can't download it).","parameters":{"type":"object","properties":{"query":{"type":"string","description":"A handful (about 3-6) of related words by name/description, e.g. 'tree oak trunk foliage' or 'rock stone boulder'. A few related words is ideal — do NOT list dozens of synonyms; that returns too many to choose from."},"kind":{"type":"string","description":"Rarely needed — a class filter. LEAVE THIS OUT for normal 'find something to spawn' searches; filtering by the wrong type hides valid matches (e.g. skeletal-mesh trees)."}},"required":["query"]}}},
    {"type":"function","function":{"name":"run_workflow","description":"Run a ComfyUI workflow JSON. Use for all generation tasks.","parameters":{"type":"object","properties":{"name":{"type":"string"},"target":{"type":"string"},"count":{"type":"integer","description":"Number of images to generate. ALWAYS use 1 for a normal request — generate a SINGLE image. Only set higher if the operator EXPLICITLY asks for multiple images, variations, or a batch (e.g. \"make 3 of these\"). Never batch on your own."},"positive":{"type":"string"},"negative":{"type":"string"},"steps":{"type":"integer"},"cfg":{"type":"number"},"width":{"type":"integer"},"height":{"type":"integer"},"checkpoint":{"type":"string"},"seed":{"type":"integer"},"scale":{"type":"number"},"image":{"type":"string","description":"OPTIONAL. Leave out to use the current image. Only when the operator names a specific earlier image: its filename or path, '#N' = the Nth image you generated this session (#1 first, #2 second, #-1 latest), or a few words describing it."},"fps":{"type":"integer"},"duration":{"type":"integer"}},"required":["name"]}}},
    {"type":"function","function":{"name":"restart_comfyui","description":"Restart the ComfyUI backend so it loads newly added or removed models and custom nodes. Use when the operator asks, or when a message says ComfyUI needs a restart. Takes about a minute; waits for any running generation first.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"scan_comfy_logs","description":"Scan ComfyUI logs for missing nodes. Call this if workflows are failing or nodes seem unavailable.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"install_missing_nodes","description":"Install missing ComfyUI custom nodes. Call this immediately when missing nodes are detected - at startup, after a workflow fails with a missing node error, or when asked. A complete node set is required for full toolkit access.","parameters":{"type":"object","properties":{"target":{"type":"string","description":"'image', 'video', or omit for both"}}}}},
    {"type":"function","function":{"name":"show_images","description":"Show images inline in chat from a folder or specific paths. Use count=24 when asked to pick favorites or browse - you can actually see the images displayed, so pick based on what you see not filenames. Use random_sample=true to draw from the entire library randomly. Use page=N to paginate through results in batches.","parameters":{"type":"object","properties":{"folder":{"type":"string"},"count":{"type":"integer","description":"How many to show, up to 24"},"paths":{"type":"array","items":{"type":"string"}},"random_sample":{"type":"boolean","description":"Sample randomly from entire folder instead of most recent"},"page":{"type":"integer","description":"Page number for pagination, starts at 1"}}}}},
    {"type":"function","function":{"name":"mark_favorite","description":"Save an image to your favorites list with a reason. Use this when you see an image you genuinely like while browsing. Your favorites persist across sessions.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Full path to the image file"},"reason":{"type":"string","description":"Why you like it - composition, subject, mood, technique"}},"required":["path"]}}},
    {"type":"function","function":{"name":"get_favorites","description":"Retrieve your saved favorites list - images you've chosen to keep track of across sessions.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"visual_search","description":"Find images visually similar to a reference image using perceptual hashing. Fast, no GPU needed. Requires visual index to be built first. Use threshold 10 for very similar, 20 for somewhat similar, 30 for loosely similar.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"Path to the reference image"},"top_n":{"type":"integer","description":"Number of results to return"},"threshold":{"type":"integer","description":"Max visual distance 0-64, lower=more similar, default 15"}},"required":["path"]}}},
    {"type":"function","function":{"name":"build_visual_index","description":"Build or update the visual hash index for the output folder. Must be run before visual_search works. Takes a few minutes for large libraries. Use force=true to rebuild from scratch.","parameters":{"type":"object","properties":{"force":{"type":"boolean","description":"Rebuild from scratch instead of updating"}}}}},
    {"type":"function","function":{"name":"remember_operator","description":"Record the name of the person at this computer when they tell you what to call them (a new computer is a new person). Saves it to this computer's profile.","parameters":{"type":"object","properties":{"name":{"type":"string","description":"What they want to be called"}},"required":["name"]}}},
    {"type":"function","function":{"name":"set_preference","description":"Save operator preferences to the machine or operator profile. Saves multiple preferences in ONE call using extra parameter. Machine preferences override hardware defaults automatically. Common keys: width, height, steps, cfg, video_width, video_height, video_fps, video_duration, checkpoint, notes.","parameters":{"type":"object","properties":{"key":{"type":"string","description":"Primary preference key"},"value":{"type":"string","description":"Primary preference value"},"scope":{"type":"string","description":"'machine' for this machine (default), 'operator' for all machines"},"extra":{"type":"object","description":"Additional key-value pairs to save in same call e.g. {\"height\":\"1024\",\"video_height\":\"512\"}"}},"required":["key","value"]}}},
    {"type":"function","function":{"name":"memory_archive_read","description":"Read compressed memory archives from past weeks. Call with no args to list available archives. Pass week='YYYY-MM-weekN' to read a specific week's compressed summary.","parameters":{"type":"object","properties":{"week":{"type":"string","description":"Week to read, e.g. '2026-05-week2'. Omit to list all available archives."}}}}},
    {"type":"function","function":{"name":"memory_search","description":"Search YOUR saved memory — facts about the operator, this project, this machine, past conversations, saved preferences/paths. NOT for general knowledge you already have from training: if it's a game, a concept, history, code, or 'what is X', you already know it — answer directly, do NOT search. Use this only when the thing is operator/project/local-specific, or a term you genuinely don't recognize. Fast index lookup; use before memory_read.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"An operator/project/local thing to recall, e.g. 'fox lora we trained', 'planet game project', 'operator preferences', 'the venv rebuild'"}},"required":["query"]}}},
    {"type":"function","function":{"name":"memory_write","description":"Save to memory. Use permanent=true for identity facts that should never be forgotten: names, relationships, preferences, key facts about the operator. Use permanent=false (default) for recent events, conversations, discoveries. Permanent memories survive compression. Recent memories get compressed after 7 days.","parameters":{"type":"object","properties":{"content":{"type":"string","description":"What to remember"},"permanent":{"type":"boolean","description":"true = identity tier (never compressed), false = recent tier (compressed after 7 days)"}},"required":["content"]}}},
    {"type":"function","function":{"name":"memory_read","description":"Read memory. tier='permanent' for identity facts, 'recent' for last 7 days, 'operator' for current operator profile, 'all' for everything.","parameters":{"type":"object","properties":{"tier":{"type":"string","description":"'permanent', 'recent', 'operator', or 'all'"}}}}},
    {"type":"function","function":{"name":"save_map","description":"Save the directory tree you've explored this session to index.md so it persists. Call this after navigating around a new machine to preserve the map for future sessions.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"speak","description":"Speak text aloud in your current voice. Use priority=true to interrupt anything currently playing.","parameters":{"type":"object","properties":{"text":{"type":"string"},"priority":{"type":"boolean","description":"Interrupt current speech"}},"required":["text"]}}},
    {"type":"function","function":{"name":"set_voice","description":"Change your voice. Pass a voice name from list_voices (e.g. 'piper:cori-high'). Optional speed and volume.","parameters":{"type":"object","properties":{"voice_name":{"type":"string"},"speed":{"type":"number"},"volume":{"type":"number"}},"required":["voice_name"]}}},
    {"type":"function","function":{"name":"list_voices","description":"List available voices you can switch to.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_voice_status","description":"Get current voice, speed, volume, and whether voice is enabled.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"enable_voice","description":"Turn your voice on or off.","parameters":{"type":"object","properties":{"enabled":{"type":"boolean"}},"required":["enabled"]}}},
    {"type":"function","function":{"name":"stop_voice","description":"Stop any speech currently playing.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_system_state","description":"Get a snapshot of current system awareness — active window, recent screen/file activity, resource use.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"watch_path","description":"Add or remove a folder you keep an eye on for changes. action='add' (default) or 'remove'.","parameters":{"type":"object","properties":{"path":{"type":"string"},"action":{"type":"string","description":"'add' or 'remove'"}},"required":["path"]}}},
    {"type":"function","function":{"name":"see_screen","description":"Capture the operator's screen and look at it. frames = how many recent frames to pull (default 1). Requires a vision-capable model to be loaded.","parameters":{"type":"object","properties":{"frames":{"type":"integer"}}}}},
    {"type":"function","function":{"name":"start_screen_capture","description":"Start continuous screen capture. fps default 2, buffer_size default 10.","parameters":{"type":"object","properties":{"fps":{"type":"integer"},"buffer_size":{"type":"integer"}}}}},
    {"type":"function","function":{"name":"stop_screen_capture","description":"Stop continuous screen capture.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"get_screen_status","description":"Check whether screen capture is running and its settings.","parameters":{"type":"object","properties":{}}}},
    {"type":"function","function":{"name":"web_search","description":"Search the web and get back the top results (title, URL, snippet). Use when the operator asks you to look something up, find current information, check a fact, or research a topic you don't already know. Routes through System 32's controlled web layer (logged, proxy-aware). After searching, open a promising result with web_fetch to read it.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"What to search for"},"max_results":{"type":"integer","description":"How many results, 1-10 (default 6)"}},"required":["query"]}}},
    {"type":"function","function":{"name":"web_image_search","description":"Search the web for IMAGES and show them as a thumbnail grid in chat. Use when the operator wants to SEE pictures of something (e.g. show me images of X, what does X look like, reference pics, examples). Different from web_search, which returns text links. Routes through System 32 controlled web layer.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"What images to find"},"max_results":{"type":"integer","description":"How many images, 1-30 (default 12)"}},"required":["query"]}}},
    {"type":"function","function":{"name":"web_video_search","description":"Search the web for VIDEOS (YouTube and other sites) and show them as cards in chat, each with Open and Download buttons. Use when the operator wants videos, clips, trailers, tutorials or gameplay footage. Different from web_search (text pages) and web_image_search (pictures).","parameters":{"type":"object","properties":{"query":{"type":"string","description":"What videos to find"},"max_results":{"type":"integer","description":"How many videos, 1-20 (default 8)"}},"required":["query"]}}},
    {"type":"function","function":{"name":"file_operation","description":"Copy, move, rename, delete files/folders or make a new folder on this computer — NATIVELY (never write a script for this). You PROPOSE it; the operator gets a Confirm card listing exactly what will happen, and nothing changes until they press Confirm. Delete goes to the Recycle Bin. Name clashes get _1 (nothing is overwritten). Not allowed: System 32's own drive, Windows / Program Files, network drives. Find the real paths first (search_computer / list_directory). SEVERAL operations in a row (e.g. make folder test2, move test1's files into it, delete test1) = ONE call with `steps` — one Confirm card, run in that order, stopping at the first step that fails.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["copy","move","rename","delete","mkdir"],"description":"What to do (leave out when using steps)"},"paths":{"type":"array","items":{"type":"string"},"description":"Full paths of the files or folders to act on (a folder + filter = the matching files inside it)"},"destination":{"type":"string","description":"copy/move: the folder to put them in (created if missing). mkdir: the new folder's full path"},"filter":{"type":"string","description":"With a folder in paths: which files inside — images, videos, audio, all, or a pattern like *.png;*.jpg. Leave empty to act on the folder itself"},"recursive":{"type":"boolean","description":"With filter: include subfolders"},"new_name":{"type":"string","description":"rename: the new name (just the name, no folder)"},"steps":{"type":"array","description":"Several operations run in order on ONE Confirm card. Each item has the same fields: action, paths, destination, filter, recursive, new_name. Example: [{\"action\":\"mkdir\",\"destination\":\"C:\\…\\Desktop\\test2\"},{\"action\":\"move\",\"paths\":[\"C:\\…\\Desktop\\test1\"],\"filter\":\"all\",\"destination\":\"C:\\…\\Desktop\\test2\"},{\"action\":\"delete\",\"paths\":[\"C:\\…\\Desktop\\test1\"]}]","items":{"type":"object"}}},"required":[]}}},
    {"type":"function","function":{"name":"search_computer","description":"Find anything on THIS COMPUTER — every drive, including spare and external/USB drives — by file and folder names, instantly (System 32 keeps an index). kind=image or video: the matches are shown to the operator in chat (a picture grid / video cards with a More results button). kind=code, folder, document, audio, model, program or any: you get a list of paths. Read-only — nothing is changed. Use this for any 'find / look for / where is / show me … on my computer'.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"Words that would be in the file or folder name, e.g. 'red fox', 'skyrim vr vive focus 3'"},"kind":{"type":"string","enum":["any","image","video","code","config","folder","document","audio","model","program","archive"],"description":"What sort of file. config = settings files (.ini .toml .cfg .json .yaml .xml) — a mod's or game's settings often live OUTSIDE its folder (e.g. Documents\\My Games\\<game>\\…)"},"max_results":{"type":"integer","description":"How many to show first (default 24 pictures / 8 videos / 25 files)"}},"required":["query"]}}},
    {"type":"function","function":{"name":"import_code","description":"Copy a project or mod from the operator's computer into a new coding project of yours, so you can study it and build an adapted version (e.g. port a Skyrim VR mod to Fallout 4 VR). Its text/code files are copied into <project>\\original\\ — the operator's original is never changed. Compiled files (.dll, .pex, .esp, …) can't be read as code; they're listed. It also takes a public GitHub repository URL (e.g. https://github.com/owner/repo) — use that for a mod's published source when its logic is in a compiled .dll. Then read the copies with read_file and write the adapted files with write_code.","parameters":{"type":"object","properties":{"path":{"type":"string","description":"The folder (or single file) to import — from search_computer — or a GitHub repository URL"},"project":{"type":"string","description":"Short name for your new project, e.g. 'fo4vr focus3 controllers'"},"goal":{"type":"string","description":"What the adapted version should do"}},"required":["path"]}}},
    {"type":"function","function":{"name":"search_code","description":"Search INSIDE code and text files for words (read-only) — like grep. Use it to find WHERE something happens in a codebase: a function, a setting, a feature (e.g. 'pipboy look open' in FRIK's source). Defaults to your active coding project (including imported copies in original\\); or give any folder on the operator's computer. Files are ranked by how many of the words they contain, with matching line numbers and lines — then read_file the best ones. (search_computer only matches file/folder NAMES.) Several words = files containing any of them, best matches first; \"quoted text\" = exact phrase.","parameters":{"type":"object","properties":{"query":{"type":"string","description":"Words to look for, e.g. 'pipboy open look' or a function name"},"path":{"type":"string","description":"Folder or file to search (default: your active project)"},"max_results":{"type":"integer","description":"Max files to list (default 25)"}},"required":["query"]}}},
    {"type":"function","function":{"name":"compare_code","description":"Compare two files or two folders, READ-ONLY (nothing is changed). Use it to: check whether the operator's copy of a mod matches a GitHub release (the version check — after import_code of both, compare the two copies); see which settings in the operator's config differ from the defaults (e.g. their FRIK.toml vs the repo's default config) — .toml/.ini files are compared setting by setting; or show exactly what your adapted version changed vs the original before the operator approves it. Folders with different layouts are matched by file name. Paths can be full paths on this computer or paths inside your active project (e.g. 'original/Scripts').","parameters":{"type":"object","properties":{"a":{"type":"string","description":"First file or folder (e.g. the operator's copy / the original)"},"b":{"type":"string","description":"Second file or folder (e.g. the GitHub copy / your version)"},"label_a":{"type":"string","description":"Short name for A in the output, e.g. 'yours'"},"label_b":{"type":"string","description":"Short name for B, e.g. 'GitHub v0.79' or 'default'"}},"required":["a","b"]}}},
    {"type":"function","function":{"name":"export_training_data","description":"Export your training data, saved to the Output training folder. Default 'unsloth': every recorded turn that worked — with its tool calls, tool results and reasoning — plus pairs of code that failed vs the fix that worked, and a ready Unsloth training script. Use when the operator asks to export training data, build a dataset, or prep data to fine-tune you. Returns the folder and the counts.","parameters":{"type":"object","properties":{"format":{"type":"string","description":"unsloth (default) — or chat / completion for the old flat conversation export","enum":["unsloth","chat","completion"]}},"required":[]}}},
    {"type":"function","function":{"name":"game_input","description":"Control a game you launched via 'S32 PLAYS' — press/hold/release keyboard keys and move/click the mouse. ONLY works while a game session is active AND the operator's cursor is over the game screen (if input is refused, call action 'status' to see why). To click a specific thing (a menu button, an icon): FIRST 'move_to' its pixel position as you see it in the latest see_screen image (x,y are pixels IN that screenshot — they're auto-mapped to the real screen), THEN 'click'. Don't click blind — position first. Use 'press' for a quick key tap, 'hold'/'release' for movement keys you keep down (hold 'w' to walk), 'move' for relative mouse aim/camera (dx,dy), 'release_all' to drop everything, 'status' to check. Keys: w,a,s,d,space,shift,ctrl,e,1,up,down,esc… Workflow: see_screen → decide → move_to + click (or press) → see_screen again. Go slow, one small action then re-check. The operator's Stop button halts everything and they can take over anytime.","parameters":{"type":"object","properties":{"action":{"type":"string","enum":["press","hold","release","release_all","move","move_to","click","status"],"description":"What to do"},"key":{"type":"string","description":"Key name for press/hold/release (e.g. 'w', 'space', 'e', 'up')"},"dx":{"type":"integer","description":"Relative mouse X for 'move' (right = positive)"},"dy":{"type":"integer","description":"Relative mouse Y for 'move' (down = positive)"},"x":{"type":"integer","description":"Absolute screen X for 'move_to' (a pixel from see_screen) — good for clicking menu buttons"},"y":{"type":"integer","description":"Absolute screen Y for 'move_to'"},"button":{"type":"string","enum":["left","right","middle"],"description":"Mouse button for 'click'"},"duration_ms":{"type":"integer","description":"How long to hold for press/click (ms)"}},"required":["action"]}}},
    {"type":"function","function":{"name":"whitelist_ports","description":"Add the listening ports and process(es) associated with a program to the Iron Dome whitelist, so they stay running when unnecessary ports/processes are closed to baseline. Use when the operator says things like 'whitelist the ports that X connects' or 'keep X open' — e.g. a game server, Discord, a launcher. Pass the program/process name (a partial match is fine). Returns which ports/processes matched and were whitelisted.","parameters":{"type":"object","properties":{"target":{"type":"string","description":"The program or process name to whitelist (e.g. 'csgo', 'steam', 'discord'). Partial match against currently listening/running items."}},"required":["target"]}}},
    {"type":"function","function":{"name":"download_file","description":"Download a file from a URL through System 32 controlled web layer and save it to the right folder automatically. Use for ComfyUI models (checkpoints, loras, vae, controlnet, etc.), Piper voice models, images for datasets, and videos (kind=video — a video page URL from web_video_search works; it's saved to Output\\downloads\\videos). Set kind so it routes to the correct folder. For dataset IMAGES, get a real URL from web_image_search first instead of guessing an image URL — guessed URLs 404. Raises a Confirm card showing size + destination; nothing downloads until the operator confirms.","parameters":{"type":"object","properties":{"url":{"type":"string","description":"The http(s) URL to download"},"kind":{"type":"string","description":"What the file is, for folder routing","enum":["checkpoint","lora","vae","controlnet","diffusion","embedding","upscale","clip","voice","image","video","generic"]},"filename":{"type":"string","description":"Optional filename override"},"subfolder":{"type":"string","description":"Optional subfolder for dataset images"}},"required":["url"]}}},
    {"type":"function","function":{"name":"review_lessons","description":"Review what you've learned about your own tool use — the weighted lessons you've accumulated from past successes and mistakes, plus a per-tool reliability summary (calls, errors, repeats). Use when the operator asks what you've learned, how you're improving, or which tools give you trouble.","parameters":{"type":"object","properties":{}}}}
]

# Minimal tool spec for low/minimal tier. DERIVED from TOOLS_SPEC (not hand-
# written) so the two specs can never drift apart — identical schemas, just a
# lean subset of tools to keep the prompt small on weak hardware.
_MINIMAL_TOOL_NAMES = {
    "run_workflow", "install_missing_nodes", "read_file", "show_file",
    "write_file", "sort_images", "list_directory", "find_file", "show_images",
    "memory_write", "memory_read", "mark_favorite", "get_favorites", "file_operation",
}
TOOLS_SPEC_MINIMAL = [t for t in TOOLS_SPEC if t["function"]["name"] in _MINIMAL_TOOL_NAMES]

# Airplane mode: the tools that only exist to reach the internet aren't offered at all —
# she answers from what's here instead of trying and failing (2026-10-31).
_WEB_ONLY_TOOLS = {"web_search", "web_fetch", "web_image_search", "web_video_search", "download_file"}


def _tools_now():
    base = TOOLS_SPEC_MINIMAL if get_hardware().get("tier") in ("low", "minimal") else TOOLS_SPEC
    import s32_config as _cfg
    if _cfg.airplane():
        return [t for t in base if t["function"]["name"] not in _WEB_ONLY_TOOLS]
    return base


def _strip_large_content(content, max_chars=800):
    """Strip base64 data and truncate long content before sending to LM Studio."""
    if isinstance(content, list):
        # Multipart - keep text parts only, drop image_url parts from history
        texts = [p["text"] for p in content if p.get("type") == "text"]
        return " ".join(texts)[:max_chars] if texts else "."
    if not isinstance(content, str):
        return str(content)[:max_chars]
    # Strip anything that looks like base64 (long runs of base64 chars)
    stripped = re.sub(r'[A-Za-z0-9+/]{200,}={0,2}', '[image data]', content)
    # Truncate very long messages
    if len(stripped) > max_chars:
        stripped = stripped[:max_chars] + "…"
    return stripped

TOOL_NAMES = {
    "generate_image", "generate_video", "train_lora", "add_to_dataset",
    "list_directory", "find_file", "read_file", "show_file", "write_file", "sort_images", "get_drives",
    "explore_tree", "index_search", "smart_search", "get_system_info",
    "resolve_shortcut",
    "list_workflows", "run_workflow",
    "spawn_actor", "get_scene", "move_actor", "delete_actor", "find_asset",
    "run_unreal_python", "code_plan", "write_code", "run_code",
    "install_missing_nodes", "scan_comfy_logs", "restart_comfyui",
    "show_images",
    "memory_write", "memory_read", "memory_search", "memory_archive_read",
    "set_preference", "remember_operator", "save_map", "mark_favorite", "get_favorites",
    "visual_search", "build_visual_index",
    "favorite_path", "get_favorite_paths", "remove_favorite_path",
    "get_system_state", "watch_path",
    "scan_network", "propose_security_action",
    "web_search", "web_fetch", "web_image_search", "web_video_search", "export_training_data", "download_file",
    "search_computer", "import_code", "compare_code", "search_code", "edit_code", "file_operation",
    "review_lessons", "whitelist_ports", "game_input",
    "see_screen", "start_screen_capture", "stop_screen_capture", "get_screen_status",
    "speak", "stop_voice", "list_voices", "set_voice", "get_voice_status", "enable_voice",
}

def _unescape_lenient(t):
    """JSON-style unescape that KEEPS unknown escapes literal. `\\n`→newline,
    `\\t`→tab, `\\"`→quote, `\\\\`→backslash, `\\/`→slash, `\\uXXXX`→char — but an
    invalid escape like `\\d` (a regex the model failed to double) stays `\\d`, so
    the code we write is what the model meant. This is what makes recovering code
    `content` from a model that under-escapes backslashes actually work."""
    out, i, n = [], 0, len(t)
    simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\",
              "/": "/", "b": "\b", "f": "\f"}
    while i < n:
        c = t[i]
        if c == "\\" and i + 1 < n:
            nx = t[i + 1]
            if nx in simple:
                out.append(simple[nx]); i += 2; continue
            if nx == "u" and i + 6 <= n:
                try:
                    out.append(chr(int(t[i + 2:i + 6], 16))); i += 6; continue
                except Exception:
                    pass
            out.append("\\" + nx); i += 2; continue      # keep e.g. \d literal
        out.append(c); i += 1
    return "".join(out)


def _salvage_write_args(s):
    """Recover {"path","content"} by structure when the model emits code that
    isn't valid JSON (under-escaped regex/Windows-path backslashes). Returns the
    dict, or None if no path field is present."""
    mp = re.search(r'"path"\s*:\s*"', s)
    if not mp:
        return None
    i, n, buf = mp.end(), len(s), []
    while i < n:                                          # path: to next unescaped "
        if s[i] == "\\" and i + 1 < n:
            buf.append(s[i:i + 2]); i += 2; continue
        if s[i] == '"':
            break
        buf.append(s[i]); i += 1
    path = _unescape_lenient("".join(buf))
    if not path:
        return None
    mc = re.search(r'"content"\s*:\s*"', s)
    if not mc:
        return {"path": path}
    ce = mc.end()
    tail = s.rfind('"}')                                  # content blob → last "}"
    body = s[ce:tail] if tail > ce else s[ce:]
    return {"path": path, "content": _unescape_lenient(body)}


def _parse_tool_args(raw):
    """Parse a model's tool-call arguments tolerantly.
    Returns (params_dict, ok). Small/quantized models (and streamed, reassembled
    arguments) sometimes emit slightly malformed JSON — trailing commas, stray
    text around the object, a truncated tail. Rather than silently dropping ALL
    params (the old `except: {}`), try a couple of cheap repairs and report
    failure so it's visible in the log instead of producing a junk empty call."""
    if isinstance(raw, dict):
        return raw, True
    s = "" if raw is None else str(raw)
    if not s.strip():
        return {}, True
    try:
        return json.loads(s, strict=False), True
    except Exception:
        pass
    # Repair: isolate the first {...} block and strip trailing commas.
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        cand = s[start:end + 1]
        for attempt in (cand, re.sub(r",\s*([}\]])", r"\1", cand)):
            try:
                return json.loads(attempt, strict=False), True
            except Exception:
                continue
    # Structural recovery for code writes: the model under-escaped backslashes
    # (regex `\d`, Windows paths) so json.loads can't parse it. Extract path+content
    # by structure. If the object looks COMPLETE (ends with "}"), hand back the
    # recovered args so the file actually gets written; if it's truncated, flag it
    # so the caller reports a clean "got cut off" instead of a blank call.
    sv = _salvage_write_args(s)
    if sv and sv.get("path"):
        complete = s.rstrip().endswith("}")
        if "content" in sv and complete:
            return sv, True
        if "content" not in sv:
            return sv, True
        return {"path": sv["path"], "_truncated": True}, False
    return {}, False


_REFOCUS_REMINDER = (
    "FOCUS — act only on the operator's CURRENT message (just below this note). "
    "Tool calls from earlier turns are already COMPLETE: do not re-run them, and "
    "never reuse an earlier turn's query or arguments. If this request needs a tool, "
    "call it with arguments taken from THIS message; if it needs none, just answer. "
    "Never repeat a tool call that already ran with the same arguments — if you "
    "already have the result, use it."
)

# Fire the refocus from the SECOND turn on. The stale-tool slip — re-running an
# earlier turn's tool with its old query/args (e.g. a new "find pine trees" coming
# back as the previous "fox" search) — starts the instant there's any prior tool
# turn in history. The old >=10 gate was built for deep-session drift and fired far
# too late: the failure shows up by the third task, when history is only ~4 long, so
# the reminder never reached the window where it was needed.
_REFOCUS_AFTER_MSGS = 2


def _enforce_single_system(messages):
    """Strict chat templates (e.g. Qwen) require the system message to be at
    position 0 AND be the only one — any later system message raises
    'System message must be at the beginning' (HTTP 400). Collapse every system
    message into a single one at index 0 so a stray injected system note can
    never 400 a turn. Non-system messages keep their order, so user/assistant
    alternation stays intact."""
    if not messages:
        return messages
    sys_text, rest = [], []
    for m in messages:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str):
                sys_text.append(c)
        else:
            rest.append(m)
    if not sys_text:
        return messages
    return [{"role": "system", "content": "\n\n".join(sys_text)}] + rest


def _recover_text_tool_call(text):
    """Parse a tool call left in plain text. Handles Qwen's XML form
    (<tool_call><function=NAME><parameter=K>V</parameter>…) and the JSON form
    (<tool_call>{"name": …, "arguments": {…}}</tool_call>). Returns
    (name, args_dict, text_before_the_call) or None."""
    m = re.search(r"(?s)<tool_call>\s*(.*?)\s*(?:</tool_call>|\Z)", text)
    if not m:
        return None
    body, before = m.group(1), text[:m.start()].strip()
    fm = re.search(r"(?s)<function=\s*([\w\-.]+)\s*>(.*?)(?:</function>|\Z)", body)
    if fm:
        name, args = fm.group(1), {}
        for pm in re.finditer(r"(?s)<parameter=\s*([\w\-.]+)\s*>(.*?)(?:</parameter>|(?=<parameter=)|\Z)",
                              fm.group(2)):
            raw = pm.group(2).strip()
            try:
                val = json.loads(raw)          # numbers / bools / objects
                if isinstance(val, str):
                    val = raw
            except Exception:
                val = raw                      # plain strings (filenames, prompts)
            args[pm.group(1)] = val
        return (name, args, before) if name in TOOL_NAMES else None
    try:
        obj = json.loads(body)
        name = obj.get("name")
        args = obj.get("arguments") or obj.get("parameters") or {}
        if isinstance(args, str):
            args = json.loads(args)
        if name in TOOL_NAMES and isinstance(args, dict):
            return name, args, before
    except Exception:
        pass
    return None


# (2026-10-31) A tool call WRITTEN as text — "[search_computer(query="gherkin1", kind="folder")]".
# Once a forced words-only answer listed her planned calls that way, the next turns copied
# the format and nothing ran ("move the images from gherkin1 into gherkin2" did nothing,
# twice). Such a call is run as a real one; with tools off it's taken out of her words.
_PSEUDO_CALL_RE = re.compile(r"\[\s*([a-z_][a-z0-9_]*)\s*\(([^\[\]]*?)\)\s*\]")


def _pseudo_calls(text):
    """[(name, args, match)] for bracketed calls to REAL tools in text."""
    import ast
    out = []
    for m in _PSEUDO_CALL_RE.finditer(text or ""):
        name = m.group(1)
        if name not in TOOL_NAMES:
            continue
        args = {}
        try:
            node = ast.parse(f"f({m.group(2)})", mode="eval").body
            for kw in node.keywords:
                if kw.arg is None:
                    continue
                v = kw.value
                if isinstance(v, ast.Name) and v.id in ("true", "false", "null", "True", "False", "None"):
                    args[kw.arg] = {"true": True, "True": True, "false": False, "False": False}.get(v.id)
                else:
                    args[kw.arg] = ast.literal_eval(v)
        except Exception:
            continue
        out.append((name, args, m))
    return out


def chat(user_text, image_b64=None, on_delta=None, auto_vision=True, no_tools=False, turn=None,
         note_as_user=False):
    """Send a message to LM Studio using native tool calling. Returns (spoken_text, tool_name, tool_params).

    auto_vision: when True (a fresh operator turn), a live screen frame is auto-injected
    so it has ambient vision. Chain-step callers pass False for NON-game chains — during
    a workflow chain, feeding it a screenshot of its own just-generated output sitting in
    the chat UI made it try to 'grab' it (get_output_image) instead of continuing the task.
    Game chains still pass True so gameplay keeps continuous vision."""
    state["last_reasoning"] = ""     # never let an earlier call's thinking leak into this one
    messages = [{"role": "system", "content": build_system_prompt()}]

    # Build clean history - ensure roles strictly alternate, strip large content.
    # CHUNKED window (2026-10-28): history starts at a fixed index and only jumps forward
    # when it gets long. A sliding [-24:] window dropped the oldest message EVERY turn
    # (and every chain step), which changed the text right after the system prompt and
    # made llama-server re-read the whole history each time. Now the prefix stays
    # identical between trims, so only new messages are processed.
    msgs = state["messages"]
    start = state.get("hist_start", 0)
    if start > len(msgs):
        start = 0                                       # history was reset
    if len(msgs) - start > _HIST_HIGH:
        start = max(0, len(msgs) - _HIST_LOW)
        _trimmed = True
    else:
        _trimmed = False
    history = list(msgs[start:])
    # Same idea by size: over the high mark → cut to the low mark in one step.
    _sizes = [len(str(_strip_large_content(m.get("content", "")) or "")) for m in history]
    if sum(_sizes) > _HIST_CHARS_HIGH:
        _drop, _tot = 0, sum(_sizes)
        while len(history) - _drop > 2 and _tot > _HIST_CHARS_LOW:
            _tot -= _sizes[_drop]
            _drop += 1
        start += _drop
        history = history[_drop:]
        _trimmed = True
    # The live user turn is re-added below as `content` — drop only that raw entry here.
    if history and history[-1].get("role") == "user":
        history.pop()
    clean_history = []
    for m in history:
        content = _strip_large_content(m.get("content", ""))
        if not content:
            continue
        if clean_history and clean_history[-1]["role"] == m["role"]:
            # Consecutive same-role turns (e.g. two operator requests with a terminal
            # generation between them, which writes no assistant turn): JOIN them. The old
            # code REPLACED the earlier one, silently erasing that request and its outcome —
            # so "that image" had no referent and it went hunting for one.
            clean_history[-1] = {"role": m["role"],
                                 "content": clean_history[-1]["content"] + "\n\n" + content}
        else:
            clean_history.append({"role": m["role"], "content": content})

    # Trim from the front if total history is too large
    # 262144 token context in LM Studio — keep generous history
    # but guard against the trim loop removing the last user message
    state["hist_start"] = start
    if _trimmed:
        log(f"[S32][CTX] History trimmed — now starting at message {start} of {len(msgs)}; "
            f"system prompt refreshed with it")
        # The cache is being rebuilt from here anyway, so fold in fresh memory/lessons
        # NOW (same call), rather than changing the prompt again on the next turn.
        _lk = _prompt_cache["locked"]
        _prompt_cache["locked"] = False
        _prompt_cache["dirty"] = True
        messages[0]["content"] = build_system_prompt()
        _prompt_cache["locked"] = _lk

    # Ensure there is at least one user turn in history
    # If trimming removed all user messages, reset to empty so the
    # new user message below becomes the first and only user turn
    if clean_history and not any(m["role"] == "user" for m in clean_history):
        clean_history = []

    # History must end with assistant so the new user message comes next. Earlier user
    # turns with no reply between (their outcomes attached) are CARRIED into the live turn
    # rather than dropped, so roles still alternate and nothing it did is lost.
    _carry = ""
    if clean_history and clean_history[-1]["role"] == "user":
        _carry = clean_history.pop()["content"]

    messages.extend(clean_history)

    # Always append the real user message
    if _carry:
        user_text = f"[Earlier in this conversation:]\n{_carry}\n\n[Now:]\n{user_text or '.'}"
    content = user_text or "."
    # A text-only model (no mmproj projector loaded) returns HTTP 500 if it
    # receives an image. Gate ALL vision on vision_ready so neither an attached
    # image nor an auto-injected screen frame can ever crash the turn. When this
    # is False the attachment is still saved on disk and the operator's text is
    # annotated upstream so it can act on it with tools instead.
    if image_b64 and not state.get("vision_ready", False):
        image_b64 = None
    if image_b64:
        # Normalize image to JPEG at max 1024px - LM Studio rejects unknown formats and large images
        try:
            raw = base64.b64decode(image_b64)
            if PILImage:
                img = PILImage.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail((1024, 1024), PILImage.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                image_b64 = base64.b64encode(buf.getvalue()).decode()
                image_mime = "image/jpeg"
            else:
                image_mime = "image/png"
        except Exception as e:
            log(f"[S32] Image normalization failed: {e} - sending as-is")
            image_mime = "image/png"
        content = [
            {"type": "text", "text": user_text or "."},
            {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}}
        ]
    # Recency refocus: deep into a tool-heavy session the system prompt (position 0)
    # gets less attention than recent turns, and bulky tool results (web/image
    # searches) carry their OLD query forward where the model can grab it instead of
    # the new instruction. Re-assert task focus in the high-attention tail — NOT as a
    # separate system message (strict Qwen templates reject any system message after
    # position 0), but folded into the live user turn, marked so the model reads it as
    # an instruction rather than the operator's words. Fires from the second turn on
    # (see _REFOCUS_AFTER_MSGS) so it actually covers the early stale-tool slip.
    if len(clean_history) >= _REFOCUS_AFTER_MSGS:
        note = "[SYSTEM REMINDER — not from the operator]\n" + _REFOCUS_REMINDER
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    part["text"] = note + "\n\n" + part.get("text", "")
                    break
        else:
            content = note + "\n\n" + content
    # Live background work (downloads) — on the user side, every turn while it runs,
    # so it knows it's in progress instead of re-starting it.
    try:
        from s32_download import active_downloads_note
        _bg = active_downloads_note()
    except Exception:
        _bg = ""
    try:
        # Its coding storyboard (active project + parked ones), every turn — this is how
        # it remembers where a programming job left off.
        from s32_code import board_note
        _bn = board_note()
        if _bn:
            _bg = (_bg + "\n\n" + _bn).strip()
    except Exception:
        pass
    if _bg:
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    part["text"] = _bg + "\n\n" + part.get("text", "")
                    break
        else:
            content = _bg + "\n\n" + content
    messages.append({"role": "user", "content": content})

    # Inject latest screen frame if available and no image already attached
    if auto_vision and not image_b64 and state.get("vision_ready", False) and _senses and _senses._state.get("ready"):
        try:
            frame_data = _senses.see_screen(1)
            frames = frame_data.get("frames", [])
            if frames and frames[-1].get("b64"):
                image_b64 = frames[-1]["b64"]
        except Exception:
            pass

    # Validate - LM Studio requires at least one user message
    # If history mangled things, strip all but the last user message
    user_turns = [m for m in messages if m["role"] == "user"]
    if not user_turns:
        messages = [{"role": "system", "content": messages[0]["content"]},
                    {"role": "user",   "content": content}]

    # NATIVE tool-call turn (2026-10-29): the chain hands over this turn's own messages —
    # her tool calls (assistant) and their results (tool) — in the format the model was
    # trained on, instead of a fresh "[read_file result: …]" user message each step that
    # dropped everything before it. `user_text` is then only a short trailing note.
    if turn is not None:
        _nm = _native_messages(messages[0], user_text, image_b64 if state.get("vision_ready", False) else None,
                               turn, note_as_user=note_as_user)
        if _nm:
            messages = _nm

    # Final safety net: guarantee exactly one system message, at position 0.
    messages = _enforce_single_system(messages)

    # Thinking toggle (tuning "thinking"). The built prompt ends in /no_think; strip it
    # per-request when thinking is ON, and tell the chat template explicitly either way.
    _think = bool(_tuning.get("thinking", 1))
    if _think and messages and isinstance(messages[0].get("content"), str):
        messages[0] = {**messages[0], "content": messages[0]["content"].replace("\n/no_think", "").replace("/no_think", "")}

    # Transient LM Studio errors that are worth retrying
    _RETRYABLE = {"model reloaded", "model is loading", "no model loaded",
                  "server is busy", "model crashed"}
    payload = {
        "model": CONFIG["lmstudio_model"],
        "messages": messages,
        "tools": _tools_now(),
        "tool_choice": "auto",
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": _think},
        **_sampling(uncapped=True)
    }

    if no_tools:
        # The loop breaker's last-chance answer: a reply in WORDS. With tools on, she
        # sometimes answered "stop reading" with yet another read (2026-10-29: a 3rd
        # list_directory of the same folder) and the turn ended with no answer at all.
        payload.pop("tools", None)
        payload["tool_choice"] = "none"
    last_err = None
    last_body = ""
    for attempt in range(5):
        try:
            try:
                resp = cancellable_post_stream(
                    f"{CONFIG['lmstudio_host']}/v1/chat/completions",
                    payload,
                    timeout=300,
                    on_delta=on_delta
                )
            finally:
                _settle_clear()      # the 'settling in' bar never outlives the request
            break  # success
        except GenerationCancelled:
            raise  # user stopped this turn — do not retry, let the caller handle it
        except ConnectionError as e:
            # llama-server isn't accepting connections — it's down (still loading,
            # crashed on startup, or stopped). Retry briefly in case it's mid-load,
            # then return a clean, actionable message instead of a raw traceback.
            last_err = e
            if attempt < 2:
                log(f"[S32] Model server unreachable (attempt {attempt+1}) — retrying in 5s")
                time.sleep(5)
                continue
            note = state.get("startup_notification")
            if note:
                msg = "⚠ " + note
            else:
                msg = ("⚠ I can't reach my model server (llama-server) right now. It may "
                       "still be loading, or it failed to start. Click the SYSTEM 32 logo to "
                       "open the model switcher and check or adjust settings — the exact "
                       "error is in llama_server.log.")
            log(f"[S32] Model server unreachable — returned graceful notice to chat.")
            _rec_flag("error")
            return msg, None, None
        except urllib.error.HTTPError as e:
            last_body = getattr(e, 's32_body', '')
            if turn is not None and e.code in (400, 422, 500):
                # The server didn't take the native turn format — the chain falls back to the
                # old one-message-per-step format (never wipe history over it).
                log(f"[S32] model server didn't take the native tool-turn format (status {e.code})")
                state["native_turns_reason"] = last_body[:400]
                raise
            body_low = last_body.lower()
            is_transient = any(t in body_low for t in _RETRYABLE)
            last_err = e
            if is_transient and attempt < 4:
                wait = (attempt + 1) * 5
                log(f"[S32] Model server transient error (attempt {attempt+1}): {last_body[:80].strip()} - retrying in {wait}s")
                time.sleep(wait)
                continue
            # Even without body, retry on 400/503 which are common reload codes
            if e.code in (400, 503) and attempt < 2:
                if "invalid image" in last_body.lower():
                    raise
                if "no user query" in last_body.lower():
                    log("[S32] History corrupted (no user query) - clearing and retrying clean")
                    state["messages"] = []
                    state["hist_start"] = 0
                    # Build orientation context from nav_state so it knows where it was
                    nav = state.get("nav_state", {})
                    _task   = nav.get("task")
                    _folder = nav.get("folder")
                    _page   = nav.get("page", 1)
                    _tool   = nav.get("tool", "")
                    if _task == "browsing" and _folder:
                        _orient = f"[Context: you were browsing {_folder} page {_page} before the connection dropped. Continue from there.]"
                    elif _task == "searching":
                        _orient = f"[Context: you were searching for '{nav.get('search_term','')}' before the connection dropped. Continue from there.]"
                    elif _task == "reading":
                        _orient = f"[Context: you were reading {nav.get('file_path','')} before the connection dropped. Continue from there.]"
                    else:
                        _orient = ""
                    _clean_text = user_text or "."
                    if _orient:
                        _clean_text = _orient + " " + _clean_text
                    clean_messages = [
                        {"role": "system", "content": build_system_prompt()},
                        {"role": "user",   "content": _clean_text}
                    ]
                    payload["messages"] = clean_messages
                    continue
                if "n_keep" in last_body.lower() or "n_ctx" in last_body.lower():
                    log("[S32] Context too small for system prompt - model needs reload with larger context")
                    log("[S32] Restart System 32 to reload the model at the correct context length")
                    raise
                log(f"[S32] Model server HTTP {e.code} (attempt {attempt+1}) - retrying in 10s")
                time.sleep(10)
                continue
            raise
    else:
        raise last_err

    choice  = resp["choices"][0]
    message = choice["message"]
    spoken  = (message.get("content") or "").strip()
    # Its reasoning: shown to the operator (system32.py pops it into the chat pane +
    # thoughts.log) but NEVER written into its history — anything in its own role
    # gets parroted. If the server didn't split it out, peel <think> blocks off here.
    _reason = (message.get("reasoning_content") or "").strip()
    if "<think>" in spoken or "</think>" in spoken:
        _m = re.search(r"(?s)^(?:<think>)?(.*?)</think>", spoken)
        if _m:
            _reason = (_reason + "\n" + _m.group(1)).strip()
            spoken = spoken[_m.end():].strip()
    state["last_reasoning"] = _reason
    calls   = message.get("tool_calls") or []

    # llama-server occasionally fails to parse its tool call and hands it back as
    # plain text ("<tool_call> <function=run_workflow> <parameter=name>…"), which it
    # then SAID instead of DID (2026-10-06, twice in a row on a bg-remove). Recover it.
    if not calls and "<tool_call" in spoken:
        _rec = _recover_text_tool_call(spoken)
        if _rec:
            _rname, _rargs, _before = _rec
            log(f"[S32] Recovered a tool call the server left as text: {_rname} {str(_rargs)[:120]}")
            calls  = [{"function": {"name": _rname, "arguments": json.dumps(_rargs)}}]
            spoken = _before
        else:
            log("[S32] Text contained <tool_call> markup that couldn't be recovered — hiding it.")
            spoken = spoken[:spoken.find("<tool_call")].strip()

    if not calls and "(" in spoken and "[" in spoken:
        _pc = _pseudo_calls(spoken)
        if _pc and not no_tools:
            _pname, _pargs, _pm = _pc[0]
            log(f"[S32] Recovered a tool call she wrote as text: {_pname} {str(_pargs)[:120]}"
                + (f" ({len(_pc) - 1} more written after it — they can follow next step)" if len(_pc) > 1 else ""))
            calls = [{"function": {"name": _pname, "arguments": json.dumps(_pargs)}}]
            spoken = spoken[:_pm.start()].strip()
        elif _pc:
            for _pname, _pargs, _pm in reversed(_pc):
                spoken = (spoken[:_pm.start()] + spoken[_pm.end():])
            spoken = re.sub(r"\n{3,}", "\n\n", spoken).strip()
            log(f"[S32] Took {len(_pc)} tool call(s) written as text out of a words-only answer")

    if not calls:
        if not spoken:
            log(f"[S32] Empty reply — no words and no tool call "
                f"(stop reason: {message.get('finish_reason') or 'unknown'}, "
                f"thinking: {len(_reason)} chars)")
        else:
            log(f"[S32] No tool call - pure text response: {spoken[:80]}")
        _rec_step(spoken, None, None, _reason)
        return spoken, None, None

    state["extra_calls"] = []
    if len(calls) > 1:
        _names = ", ".join((c.get("function") or {}).get("name", "?") for c in calls)
        # Several READS at once (read_file A, B, C): run them all in this step. Running only
        # the first made her come back for B and C one by one — and re-ask for A, which the
        # repeat breaker stopped (2026-10-29). Anything else: first call only, as before.
        _first = (calls[0].get("function") or {}).get("name")
        _extra = []
        if _first in _BATCH_READS or _first in _BATCH_FIRST:
            for _c in calls[1:7]:
                _f = _c.get("function") or {}
                if _f.get("name") in _BATCH_READS:
                    _p, _ok = _parse_tool_args(_f.get("arguments"))
                    if _ok and isinstance(_p, dict):
                        _extra.append((_f["name"], _p))
        state["extra_calls"] = _extra
        log(f"[S32] Model emitted {len(calls)} tool calls ({_names}); "
            + (f"running all {1 + len(_extra)} now." if _extra else
               "running the first, others can follow on the next step."))

    # Take the first tool call
    call = calls[0]
    fn   = call.get("function") or {}
    tool_name = fn.get("name")
    if not tool_name:
        log("[S32] Tool call missing a function name — treating as text.")
        _rec_step(spoken, None, None, _reason)
        return spoken, None, None

    tool_params, ok = _parse_tool_args(fn.get("arguments"))
    if not ok:
        log(f"[S32] WARNING: could not parse args for {tool_name} — running with no params. "
            f"raw={str(fn.get('arguments'))[:300]}")
        if isinstance(tool_params, dict) and tool_params.get("_truncated"):
            _rec_flag("error")
            return ("That file was too long to finish in one go — the generation hit its "
                    "length limit and got cut off, so I didn't write a broken file. Ask me "
                    "for a more concise version, or to split it into smaller files.",
                    None, None)

    log(f"[S32] Tool call: {tool_name} | args: {str(fn.get('arguments'))[:150]}")

    if tool_name not in TOOL_NAMES:
        log(f"[S32] Unknown tool from model: {tool_name}")
        try:
            from s32_feedback import record_unknown as _fb_unk; _fb_unk(tool_name)
        except Exception:
            pass
        _rec_flag("error")
        return spoken, None, None

    _rec_step(spoken, tool_name, tool_params, _reason)
    return spoken, tool_name, tool_params


_TURN_HIST_REQ = 6000   # the operator's request, as kept in the native turn


def _native_messages(system_msg, note, image_b64, turn, note_as_user=False):
    """system + earlier conversation + the operator's request + THIS turn's tool calls/results
    + (optional) a short trailing note. Returns None when the turn's start isn't known."""
    msgs = state["messages"]
    ts = state.get("turn_msg_start")
    start = state.get("hist_start", 0)
    if ts is None or ts >= len(msgs) or msgs[ts].get("role") != "user":
        return None
    start = min(start, ts)
    clean = []
    for m in msgs[start:ts]:
        c = _strip_large_content(m.get("content", ""))
        if not c:
            continue
        if clean and clean[-1]["role"] == m["role"]:
            clean[-1] = {"role": m["role"], "content": clean[-1]["content"] + "\n\n" + c}
        else:
            clean.append({"role": m["role"], "content": c})
    if clean and not any(m["role"] == "user" for m in clean):
        clean = []
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    req = state.get("turn_req_full") or msgs[ts].get("content") or "."
    if isinstance(req, list):
        req = " ".join(p.get("text", "") for p in req if p.get("type") == "text") or "."
    req = str(req)[:_TURN_HIST_REQ]
    if clean and clean[-1]["role"] == "user":
        req = f"[Earlier in this conversation:]\n{clean.pop()['content']}\n\n[Now:]\n{req}"
    out = [system_msg] + clean + [{"role": "user", "content": req}] + [dict(m) for m in turn]
    # Trailing note: live context (downloads, coding board) + whatever the chain adds.
    bg = ""
    try:
        from s32_download import active_downloads_note
        bg = active_downloads_note() or ""
    except Exception:
        pass
    try:
        from s32_code import board_note
        bn = board_note()
        if bn:
            bg = (bg + "\n\n" + bn).strip()
    except Exception:
        pass
    text = "\n\n".join(x for x in (bg, (note or "").strip()) if x)
    # A routine note rides INSIDE the latest tool result, so the turn stays one multi-step
    # tool turn (the Qwen template treats the last plain user message as 'the question').
    # Real interruptions (STOP, tools off, 'you didn't call it') come as a user message.
    if text and not image_b64 and not note_as_user and out[-1].get("role") == "tool":
        out[-1] = {**out[-1], "content": str(out[-1].get("content") or "") + "\n\n" + text}
        return out
    if image_b64:
        out.append({"role": "user", "content": [
            {"type": "text", "text": text or "."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}]})
    elif text:
        out.append({"role": "user", "content": text})
    return out


_BATCH_READS = {"read_file", "show_file", "list_directory", "explore_tree", "code_plan", "compare_code", "search_code"}   # safe to run together
# A real step she can pair with looks (search_computer + explore_tree, import_code + read_file):
# the paired looks used to be dropped, and she asked again next step — 3 of them lost in one
# FRIK turn (2026-10-29).
_BATCH_FIRST = {"search_computer", "import_code", "web_search", "find_file"}


def _rec_step(say, tool, params, thought):
    """Training record: this model call, as one step of the current turn (s32_training)."""
    try:
        from s32_training import note_step
        note_step(say, tool, params, thought)
    except Exception:
        pass


def _rec_flag(name):
    try:
        from s32_training import flag
        flag(name)
    except Exception:
        pass

def summarize_thought(text):
    """1-3 line plain summary of a reasoning block, for the chat pane's thought header.
    Separate, tool-less, thinking-off call; requested by the UI only AFTER a turn ends."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        resp = http_post(
            f"{CONFIG['lmstudio_host']}/v1/chat/completions",
            {"model": CONFIG["lmstudio_model"],
             "messages": [
                 {"role": "system", "content": "You summarize an AI assistant's private reasoning for "
                  "its developer. In 1-3 short plain sentences, state what it concluded and why, and "
                  "note any wrong assumption or confusion. No preamble.\n/no_think"},
                 {"role": "user", "content": text[-8000:]}],
             "max_tokens": 160, "temperature": 0.3, "stream": False,
             "chat_template_kwargs": {"enable_thinking": False}},
            timeout=60)
        out = (resp.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        out = re.sub(r"(?s)<think>.*?</think>", "", out).strip()
        return out[:600]
    except Exception as e:
        log(f"[S32] thought summary failed: {str(e)[:100]}")
        return ""


def chat_followup(user_text, tool_name, tool_result_summary):
    """
    After a tool runs, feed a lightweight tool-result message back to the model
    so it can respond naturally to what it retrieved.
    Returns spoken text only.
    """
    messages = [{"role": "system", "content": build_system_prompt()}]

    history = state["messages"][-10:]  # shorter window for followup
    clean_history = []
    for m in history:
        content = _strip_large_content(m.get("content", ""))
        if not content:
            continue
        if clean_history and clean_history[-1]["role"] == m["role"]:
            clean_history[-1] = {"role": m["role"], "content": content}
        else:
            clean_history.append({"role": m["role"], "content": content})

    # Trim if too large
    while clean_history and sum(len(m["content"]) for m in clean_history) > 3000:
        clean_history.pop(0)

    messages.extend(clean_history)

    # Append a tool-result turn so the model sees what came back
    messages.append({
        "role": "user",
        "content": f"[Tool result for {tool_name}]: {tool_result_summary}\n\nNow respond to the user naturally."
    })

    # Final safety net: guarantee exactly one system message, at position 0.
    messages = _enforce_single_system(messages)

    payload = {
        "model": CONFIG["lmstudio_model"],
        "messages": messages,
        "tool_choice": "none",
        "stream": False,
        **_sampling(uncapped=True)
    }

    last_err = None
    for attempt in range(3):
        try:
            try:
                resp = http_post(
                    f"{CONFIG['lmstudio_host']}/v1/chat/completions",
                    payload,
                    timeout=120
                )
            finally:
                _settle_clear()      # not streamed: no first-token signal, so no bar left behind
            choice  = resp["choices"][0]
            message = choice["message"]
            spoken  = (message.get("content") or "").strip()
            return spoken
        except Exception as e:
            last_err = e
            if attempt < 2:
                wait = (attempt + 1) * 5
                log(f"[S32] Followup chat error (attempt {attempt+1}), retrying in {wait}s: {e}")
                time.sleep(wait)
                continue
            log(f"[S32] Followup chat failed after 3 attempts: {e}")
            return ""


def caption_image(image_b64):
    """Return one vivid sentence describing an image, to use as a generation prompt when
    the operator supplied none (e.g. 'turn this attached image into a video'). Uses the
    vision model. Best-effort: returns '' on any failure so callers can fall back."""
    if not image_b64:
        return ""
    try:
        image_mime = "image/jpeg"
        try:
            raw = base64.b64decode(image_b64)
            if PILImage:
                img = PILImage.open(io.BytesIO(raw)).convert("RGB")
                img.thumbnail((1024, 1024), PILImage.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=85)
                image_b64 = base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
        payload = {
            "model": CONFIG["lmstudio_model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ("Describe this image in ONE vivid, concrete sentence "
                                          "usable as a text-to-video generation prompt. Output only "
                                          "the description — no preamble, no quotes.")},
                {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}},
            ]}],
            "tool_choice": "none",
            "max_tokens": 120,
            "temperature": 0.4,
            "stream": False,
        }
        resp = http_post(f"{CONFIG['lmstudio_host']}/v1/chat/completions", payload, timeout=90)
        text = (resp["choices"][0]["message"].get("content") or "").strip().strip('"').strip()
        return text[:300]
    except Exception as e:
        log(f"[S32] caption_image failed: {e}")
        return ""


# Tools that need a model followup after running
_FOLLOWUP_TOOLS = {
    "show_images",
    "list_directory", "find_file", "read_file",
    "get_drives", "get_system_info",
    "list_workflows",
    "scan_comfy_logs",
    "memory_read",
    "save_map",
    "get_favorites",
    "visual_search",
    "build_visual_index",
    "show_file",
}


def _code_file_summary(r):
    """What it's told after write_code / run_code."""
    fn, v, proj = r.get("filename"), r.get("version"), r.get("project")
    vo = r.get("vs_original") or {}
    _vs = ""
    if vo.get("ambiguous"):
        _vs = (f" Several imported originals are named {fn} ({', '.join(vo['ambiguous'][:4])}) — pass the "
               f"filename WITH its folder (e.g. '{vo['ambiguous'][0]}') so the operator gets a diff against the right one.")
    elif vo.get("identical"):
        _vs = f" NOTE: it's IDENTICAL to the original {vo.get('original')} — you haven't changed anything yet."
    elif vo:
        _vs = (f" It's a changed copy of the original {vo.get('original')} ({vo.get('stat')}) — the operator "
               f"also sees a diff card of exactly what you changed; explain each change and how they'd build/use it.")
    if not r.get("ran"):
        return (f"Saved {fn} (version {v}) in '{proj}'.{_vs} The operator sees the WHOLE file in chat with a "
                f"Download button. You don't need to read it back or check it on disk; you wrote it. "
                f"If it's Python you can test it (run_code). Otherwise, if that's the whole job, tell "
                f"the operator in a sentence or two what it does and how to use it, and that they can "
                f"Download it from the card. For a multi-step project, update the board with code_plan.")
    n, mx = r.get("attempt", 1), r.get("max", 5)
    if r.get("stopped"):
        return (r.get("error", "") + "\nYou're OUT of runs for this file. Do NOT call any more tools. "
                "Tell the operator what keeps failing; record it with code_plan next time.")
    if r.get("declined") or r.get("duplicate"):
        return r.get("error", "Not run.")
    out = (r.get("output") or "").strip()
    if r.get("ok"):
        return (f"{fn} v{v} ran CLEAN (run {n}, {r.get('seconds', '?')}s). Output:\n"
                f"{out[-800:] or '(nothing printed)'}\n"
                f"Mark the step done with code_plan (and set next). If the whole job is done, tell the "
                f"operator in a sentence or two what it does and that they can Download it from the "
                f"card — they can mark it Works or Not right.")
    tb = (r.get("traceback") or "").strip()
    code = (r.get("code") or "").strip()
    if len(code) > 5000:
        code = code[:5000] + "\n# … (truncated)"
    tail = (f"\nFix the cause and call write_code again with the corrected WHOLE file and run=true "
            f"({mx - n} run(s) left). Change what the error points at — don't resend the same code."
            if n < mx else
            "\nThat was the LAST run — stop, record what's failing with code_plan(note=…), and tell "
            "the operator plainly.")
    return (f"{fn} v{v} FAILED (run {n} of {mx})."
            + (f"\nPrinted before the error:\n{out[-300:]}" if out else "")
            + f"\n{tb[-1200:]}\nThe file that failed:\n```\n{code}\n```{tail}")


def _ue_code_summary(r):
    """What it's told after one run of its Unreal script."""
    n, mx = r.get("attempt", 1), r.get("max", 5)
    if r.get("stopped"):
        return (r.get("error", "") + "\nYou're OUT of attempts for this request. Do NOT call any more "
                "tools. Tell the operator plainly what kept failing and what you'd try next time.")
    if r.get("declined") or r.get("duplicate"):
        return r.get("error", "Not run.")
    out = (r.get("output") or "").strip()
    if r.get("ok"):
        return (f"Attempt {n} ran CLEAN in {r.get('seconds', '?')}s. Printed output:\n"
                f"{out[-800:] or '(nothing printed)'}\n"
                f"It's done — tell the operator in a sentence or two what the script did (from the "
                f"output) and ask them to check it in the editor; they can mark it Works or Not right. "
                f"Don't run it again unless they ask for a change.")
    tb = (r.get("traceback") or "").strip()
    head = f"Attempt {n} of {mx} FAILED."
    if out:
        head += f"\nPrinted before the error:\n{out[-300:]}"
    tail = (f"\nFix the cause and call run_unreal_python again with the corrected FULL script "
            f"({mx - n} attempt(s) left). Change what the error points at — don't resend the same code."
            if n < mx else
            "\nThat was the LAST attempt — stop, and tell the operator plainly what keeps failing and "
            "what you'd try next.")
    # Its tool-call arguments don't stay in its history, so hand the failing script back —
    # it fixes THAT code instead of rewriting from memory.
    code = (r.get("code") or "").strip()
    if len(code) > 4000:
        code = code[:4000] + "\n# … (truncated)"
    hint = r.get("hint") or ""
    hint = f"\nAPI CHECK (looked up in the live editor): {hint}" if hint else ""
    return f"{head}\n{tb[-900:]}{hint}\nThe script that failed (attempt {n}):\n```python\n{code}\n```{tail}"


def _tool_result_summary(tool_name, result):
    """Build a compact text summary of a tool result to feed back to the model."""
    if not result:
        return "No result."
    if tool_name == "run_unreal_python" and result.get("type") == "ue_code":
        return _ue_code_summary(result)
    if tool_name == "remember_operator" and result.get("ok"):
        return (f"Saved — the person at this computer is {result.get('operator')}. Greet them by name in "
                f"a sentence and carry on; no more tools for this.")
    if tool_name == "code_plan" and result.get("nudge"):
        return (f"Your board for '{result.get('project')}' is already set — calling code_plan again does "
                f"nothing. Do the work now: call write_code for the next step"
                f"{(' (' + result['next'] + ')') if result.get('next') else ''}. Only update the board "
                f"after a step is actually finished.")
    if tool_name == "code_plan" and result.get("event") == "closed":
        return (f"'{result.get('project')}' is closed — the job is DONE. Do NOT call any more tools. Reply "
                f"to the operator now: a sentence or two on what it does, how to use it, and that they can "
                f"Download it from the card.")
    if tool_name == "code_plan" and result.get("ok"):
        return (f"Board for '{result.get('project')}' {result.get('event')} ({result.get('progress')}). "
                f"{('Next: ' + result['next'] + '. ') if result.get('next') else ''}"
                f"Now do the next step — usually write_code for the next file.")
    if tool_name == "file_operation":
        if result.get("error"):
            return result["error"] + " (Nothing was changed.)"
        return result.get("note") or "Proposed — waiting for the operator to confirm."
    if tool_name == "edit_code" and result.get("type") == "code_file":
        ed = result.get("edit") or {}
        vo = result.get("vs_original") or {}
        return (f"Edited {ed.get('file')}: replaced {ed.get('replaced')} at line {ed.get('line')} — saved as version "
                f"{result.get('version')} in your project (the original is untouched)."
                + (f" Total change vs the original {vo.get('original')}: {vo.get('stat')} — the operator sees the diff card."
                   if vo.get("original") and not vo.get("identical") else "")
                + f"\nThe changed lines now read:\n{ed.get('context', '')}\n"
                  "More changes → another edit_code. When done, tell the operator what you changed and why, and "
                  "what they need to do to use it (e.g. rebuild the plugin) — you can't compile or run it in-game.")
    if tool_name in ("write_code", "run_code") and result.get("type") == "code_file":
        return _code_file_summary(result)
    if result.get("error"):
        return f"Error: {result['error']}"

    if tool_name == "find_asset":
        assets = result.get("assets", []) or []
        total = result.get("count", len(assets))
        q = result.get("query", "")
        if not assets:
            return (f"No assets matching '{q}' are in the project. That content hasn't been "
                    f"imported yet — tell the operator; you can't download it. (You can still "
                    f"spawn basic shapes.)")
        head = (f"Found {total} matching asset(s) — the top spawnable ones are listed below. "
                f"PICK THE ONE that best fits what the operator asked for and spawn it NOW: call "
                f"spawn_actor(asset=<its exact path>, asset_class=<its class, shown in [brackets]>). "
                f"Do NOT call find_asset again — you already have the results in hand; searching "
                f"again just loops. spawn_actor handles static vs skeletal automatically:")
        lines = [head]
        for a in assets[:8]:
            lines.append(f"  {a.get('name','?')} [{a.get('class','?')}] — asset={a.get('path','')}")
        if total > 8:
            lines.append(f"  …plus {total-8} more, but the ones above are almost certainly what you want.")
        return "\n".join(lines)

    if tool_name == "spawn_actor" and result.get("pending"):
        nm = result.get("name") or result.get("spawned", "the asset")
        return (f"'{nm}' is a heavy asset and Unreal is still loading/placing it — this is NORMAL "
                f"and it IS being added (you may see a 'waiting for skinned assets' popup). Do NOT "
                f"spawn it again and do NOT retry — it's on its way in. Tell the operator it's "
                f"loading; if you want to confirm, call get_scene once, not repeatedly.")

    if tool_name == "spawn_actor":
        loc = result.get("location", {})
        nm = result.get("name") or result.get("spawned", "?")
        return (f"Spawned '{nm}' at ({loc.get('x')}, {loc.get('y')}, {loc.get('z')}) — this is "
                f"CONFIRMED done. Do NOT call get_scene to check it appeared; it did. Refer to it "
                f"as '{nm}'. If the operator asked for a SINGLE object, the request is now COMPLETE "
                f"— stop here. Only spawn again if they asked for several (then keep going until "
                f"you've placed the number they asked for).")

    # Unreal scene listing — format the actor NAMES compactly (the raw result carries
    # full ~100-char object paths ×N, which blew past the 400-char default cap and
    # reached its sliced mid-string, so it kept (correctly) reporting truncation). Names
    # are short; all fit. State completeness explicitly so a whole list can't be mistaken
    # for a partial one.
    if tool_name == "get_scene":
        actors = result.get("actors", []) or []
        total  = result.get("count", len(actors))
        shown  = result.get("shown", len(actors))
        if shown >= total:
            head = (f"The Unreal level contains {total} actor(s) — this is the COMPLETE list, "
                    f"nothing omitted or truncated (positions are world X,Y,Z; +Z is up):")
        else:
            head = (f"The Unreal level contains {total} actor(s); showing the first {shown} "
                    f"(call again only if you truly need more — the count is exact):")
        lines = [head]
        for i, a in enumerate(actors, 1):
            nm = a.get("name", "?")
            pos = a.get("pos")
            yaw = a.get("yaw")
            typ = a.get("type")
            tstr = f" [{typ}]" if typ else ""
            face = f" facing {yaw}°" if yaw is not None else ""
            if pos:
                lines.append(f"  {i}. {nm}{tstr}  ({pos.get('x')}, {pos.get('y')}, {pos.get('z')}){face}")
            else:
                lines.append(f"  {i}. {nm}{tstr}")
        return "\n".join(lines)

    if tool_name == "move_actor":
        loc = result.get("location", {})
        return (f"Moved '{result.get('actor','?')}' ({result.get('mode','')}) to "
                f"X={loc.get('x')}, Y={loc.get('y')}, Z={loc.get('z')}. CONFIRMED done — do NOT "
                f"call get_scene to check it moved; it did. Don't re-move it unless the operator "
                f"asks for a further adjustment.")

    if tool_name == "delete_actor":
        return (f"Deleted '{result.get('deleted','?')}' from the world. CONFIRMED gone — do NOT "
                f"call get_scene to check; it's removed (undoable in-editor with Ctrl+Z). Don't "
                f"try to delete it again.")

    # Web tools handled up front — their results carry a 'results' key that the
    # generic file-search branch below would otherwise hijack.
    if tool_name == "web_search":
        rs = result.get("results", [])
        if not rs:
            return (f"No web results for '{result.get('query','')}'. Rephrase the query ONCE if "
                    f"needed; otherwise tell the operator nothing came up. Do not keep searching.")
        if result.get("repeat"):
            return ("These are the SAME results as your previous search — searching again with "
                    "different wording won't find anything new. Either READ the most relevant "
                    "page with web_fetch, or answer the operator now from what you have (and say "
                    "plainly if it doesn't cover the question).")
        via = " (via proxy)" if result.get("proxy") else ""
        lines = [f"Web results for '{result.get('query','')}'{via} — {len(rs)} found "
                 f"(already shown to the operator as clickable cards):"]
        for i, r in enumerate(rs, 1):
            lines.append(f"{i}. {r.get('title','(no title)')} — {r.get('url','')}")
            if r.get("snippet"):
                # The top hit can carry real text (e.g. a Wikipedia intro) — keep more of it.
                lines.append(f"   {r['snippet'][:900 if i == 1 else 200]}")
        if result.get("source"):
            lines.append(f"(found via {result['source']})")
        pages = result.get("pages") or []
        for pg in pages:
            lines.append("")
            lines.append(f"=== PAGE READ: {pg.get('title','')} — {pg.get('url','')} ===")
            lines.append(pg.get("text", "")[:3000])
        lines.append("")
        if pages:
            lines.append("You now have the results AND the text of the top page(s) above. ANSWER the "
                         "operator now from them — concrete steps/numbers from the pages, and name "
                         "the sources briefly. Don't search again for the same question.")
        else:
            first_url = rs[0].get("url", "")
            lines.append("STOP — you now have results. Do NOT call web_search again; a new query just "
                         "returns similar links. Choose ONE next action:")
            lines.append("  1) ANSWER the operator now from the titles + snippets above, or")
            lines.append(f"  2) READ one page in full with web_fetch, e.g. web_fetch(url=\"{first_url}\")")
        return "\n".join(lines)

    if tool_name == "web_fetch":
        head = f"Page: {result.get('title') or result.get('url','')}\n{result.get('url','')}\n\n"
        tail = ("\n\n[Page is longer — this is the first part. Call web_fetch again with a "
                "higher max_chars if you need more.]") if result.get("truncated") else ""
        return head + result.get("text", "") + tail

    if tool_name == "download_file":
        if result.get("error"):
            return f"The download couldn't start: {result['error']}"
        if result.get("refused"):
            return result.get("reason", "That download was refused.")
        # Auto-downloaded image/dataset file (no confirm gate) — it's on disk NOW.
        # Tell it it's ready and to continue with the task (e.g. feed it to a workflow).
        if result.get("downloaded"):
            return (f"Downloaded to {result.get('path', result.get('dest',''))} — the file is "
                    f"saved and ready now. If the task was to do something with it (turn it into "
                    f"a video, add to a dataset, etc.), continue with that next step using this path.")
        ex = (" It's an executable, so it's flagged for review and I won't run it."
              if result.get("executable") else "")
        return (f"I've queued a {result.get('kind', 'file')} download to "
                f"{result.get('dest', '')}. A Confirm card is on screen now — it won't "
                f"download until the operator confirms it.{ex} Don't call download_file "
                f"again for this one; just wait for their confirm.")

    if tool_name == "game_input":
        if result.get("ok"):
            held = result.get("held")
            extra = f" Currently holding: {held}." if held else ""
            return (f"Input sent ({result.get('key') or result.get('button') or result.get('action','')})."
                    f"{extra} Take a see_screen shot to see the result, then act again. "
                    f"Keep actions small and check the screen between them.")
        reason = (result.get("reason") or result.get("error") or "refused").lower()
        if "not found" in reason or "loading" in reason:
            return ("The game window isn't detected yet — it's probably still loading. "
                    "Take a see_screen shot to check; wait and try again in a moment. Don't spam input.")
        if "off the game" in reason or "cursor" in reason:
            return ("Your input is gated to when the operator's cursor is over the game screen, and right "
                    "now it isn't. Ask the operator to move their mouse onto the game, then continue. "
                    "Don't keep retrying blindly.")
        if "halted" in reason or "no game session" in reason:
            return (f"Game input is off ({reason}). The session ended or Stop was pressed — don't try to "
                    f"send input; just tell the operator.")
        return (f"Input not sent: {reason}. Check game_input action='status', and don't hammer it.")

    if tool_name == "whitelist_ports":
        if result.get("error"):
            return (f"{result['error']} Tell the operator nothing matched, so nothing was "
                    f"whitelisted. Don't retry with the same target.")
        mp = result.get("matched_ports", [])
        mc = result.get("matched_procs", [])
        return (f"Whitelisted for the Iron Dome — ports {mp or '—'} and process(es) "
                f"{', '.join(mc) if mc else '—'}. These will stay running when unnecessary "
                f"ports/processes are closed to baseline. Confirm to the operator in one line "
                f"what got whitelisted; don't call whitelist_ports again for this target.")

    if tool_name == "export_training_data":
        if result.get("error"):
            return result["error"]
        if result.get("format") == "unsloth":
            return (f"Exported {result.get('sft', 0)} training sample(s) and {result.get('dpo', 0)} "
                    f"worked-vs-failed pair(s) from {result.get('turns', 0)} recorded turn(s) "
                    f"({result.get('good', 0)} good, {result.get('bad', 0)} bad) to:\n"
                    f"{result.get('path', '')}\nIts README.txt explains how to train. Tell the "
                    f"operator it's saved. Do NOT export again unless they ask for a fresh one.")
        return (f"Exported {result.get('count', 0)} record(s) from "
                f"{result.get('turns', 0)} conversation turn(s) "
                f"({result.get('format', 'chat')} format) to:\n{result.get('path', '')}\n\n"
                f"Tell the operator it's saved and ready to fine-tune with. Do NOT export "
                f"again unless they ask for a fresh one.")

    if tool_name == "web_image_search":
        if result.get("error"):
            return result["error"]
        rs = result.get("results", [])
        lines = [f"Found {result.get('count', len(rs))} image(s) for "
                 f"'{result.get('query','')}' — ALREADY displayed to the operator as a "
                 f"thumbnail grid (they can click any to enlarge). To DOWNLOAD one into a "
                 f"dataset, call download_file with its image URL below (the direct image "
                 f"link — NOT a page URL):"]
        for i, r in enumerate(rs, 1):
            lines.append(f"{i}. {r.get('title') or '(no title)'} → image: {r.get('image','')}")
        lines.append("")
        lines.append("Do NOT call web_image_search again unless the operator asks for "
                     "different images. If they only wanted to see them, just describe what "
                     "you found. Each picture has its own ⬇ button: pressing it saves that picture to "
                     "Output\\downloads\\images (not the dataset).")
        return "\n".join(lines)

    if tool_name == "search_computer":
        if result.get("error"):
            return result["error"]
        rs = result.get("results", [])
        k = result.get("kind", "any")
        head = (f"Found {result.get('count', len(rs))} match(es) on this computer for '{result.get('query', '')}'"
                + (f" ({k})" if k != "any" else "") + f" — {result.get('displayed', 'shown')}"
                + (" as a grid with a More results button" if k in ("image", "video") else "") + ".")
        lines = [head]
        if result.get("note"):
            lines.append(result["note"])
        if not rs:
            lines.append("(search_computer matches file and folder NAMES on the operator's drives. To find "
                         "WHERE in code something happens — a function, a setting, a word — use search_code "
                         "on the folder, e.g. your project.)")
        for r in rs[:30]:
            if r.get("count"):
                lines.append(f"- [folder] {r.get('path')}  ({r['count']} matching files: "
                             + ", ".join(r.get("files", [])[:5]) + ("…" if r["count"] > 5 else "") + ")")
                continue
            sz = r.get("size") or 0
            szs = (f"{sz / 1048576:.1f} MB" if sz >= 1048576 else f"{sz // 1024} KB") if sz else ""
            lines.append(f"- [{r.get('kind')}] {r.get('path')}" + (f"  ({szs}, {r.get('modified')})" if szs else
                                                                     (f"  ({r.get('modified')})" if r.get('modified') else "")))
        if result.get("count", 0) > 30:
            lines.append(f"(+{result['count'] - 30} more)")
        lines.append("")
        lines.append("Folders: look inside with list_directory. Code you're asked to reuse or adapt: import_code "
                     "the folder (the operator's original is never changed). Pictures/videos: the operator can "
                     "already see them — describe briefly what you found.")
        return "\n".join(lines)

    if tool_name == "import_code":
        if result.get("error"):
            return result["error"]
        lines = [f"Imported {result.get('copied', 0)} text/code file(s) ({result.get('bytes', 0) // 1024} KB) from "
                 f"{result.get('source')} into your project '{result.get('project')}' — copies in "
                 f"{result.get('folder')} (the original is untouched). Files:"]
        for f in result.get("files", [])[:80]:
            lines.append(f"- {f.get('copy')}  ({f.get('size', 0) // 1024} KB)")
        if len(result.get("files", [])) > 80:
            lines.append(f"(+{len(result['files']) - 80} more)")
        if result.get("binary"):
            lines.append("Not copied (compiled/binary — can't be read as code): " +
                         ", ".join(f"{n}× {e}" for e, n in result["binary"].items()))
        if result.get("settings"):
            lines.append(f"SETTINGS FILES that belong to it but live OUTSIDE its folder — copied too (read-only), "
                         f"so search_code / read_file see them. The LIVE files the program reads are the originals:")
            for st in result["settings"][:20]:
                lines.append(f"- {st.get('original')}  → copy: {st.get('copy')}")
            lines.append("They show how it's set up on this computer right now. To change a live setting, "
                         "propose it with write_file on the ORIGINAL path (the operator confirms).")
        if result.get("compiled"):
            lines.append("COMPILED PLUGINS (the program's main logic is usually inside these, NOT in the "
                         "copied files): " + ", ".join(result["compiled"]) + ". If the behaviour you need "
                         "isn't in the copies, don't keep listing folders — find the plugin's published "
                         "source code (web_search '<mod name> source code github'), then call import_code "
                         "with that GitHub URL to copy the source into this project.")
        if result.get("over_limit"):
            lines.append(f"{result['over_limit']} more text file(s) skipped (import size limit).")
        if str(result.get("source", "")).lower().startswith(("http://", "https://", "github.com")):
            lines.append(f"To check this source against the operator's own copy (same version? which settings "
                         f"differ from the defaults?), use compare_code — e.g. compare their file/folder with "
                         f"the matching one in {result.get('folder')}.")
        lines.append("Next: search_code to find WHERE the behaviour lives, read_file it, then change it with "
                     "edit_code (exact old text → new text; the operator sees the diff). write_code is for brand-new files.")
        return "\n".join(lines)

    if tool_name == "compare_code":
        if result.get("error"):
            return result["error"]
        la, lb = result.get("label_a", "A"), result.get("label_b", "B")
        head = (f"Compared {la} = {result.get('a')}  with  {lb} = {result.get('b')} (read-only). "
                f"In the diffs, '-' lines are only in {la}, '+' lines only in {lb}.")
        if "diff" in result or "summary" in result:
            return head + "\n" + (result.get("summary") or result.get("diff") or "")
        lines = [head, f"{result.get('matched', 0)} file(s) matched: {len(result.get('same', []))} identical "
                       f"({result.get('match_pct', 0)}%), {len(result.get('changed', []))} different; "
                       f"{len(result.get('only_a', []))} only in {la}, {len(result.get('only_b', []))} only in {lb}."]
        if result.get("identical"):
            lines.append("The two are IDENTICAL.")
        for c in result.get("changed", [])[:40]:
            lines.append(f"\n## {c['a']}" + (f"  ↔  {c['b']}" if c['b'] != c['a'] else ""))
            lines.append(c.get("diff") or c.get("note", ""))
        for lab, key in ((la, "only_a"), (lb, "only_b")):
            xs = result.get(key, [])
            if xs:
                lines.append(f"\nOnly in {lab} ({len(xs)}): " + ", ".join(xs[:25]) + (" …" if len(xs) > 25 else ""))
        if result.get("capped"):
            lines.append("(Very large folder — only the first 4000 files were compared; compare a subfolder for more.)")
        return "\n".join(lines)

    if tool_name == "web_video_search":
        if result.get("error"):
            return result["error"]
        rs = result.get("results", [])
        lines = [f"Found {result.get('count', len(rs))} video(s) for '{result.get('query', '')}' — "
                 f"ALREADY shown to the operator as cards with ▶ Open and ⬇ Download buttons:"]
        for i, r in enumerate(rs, 1):
            _v = r.get("views")
            if isinstance(_v, (int, float)) or str(_v or "").isdigit():
                _v = f"{int(_v):,} views"            # ddgs gives a bare number (crashed the join)
            meta = " · ".join(str(x) for x in (r.get("duration"), r.get("uploader") or r.get("publisher"),
                                                r.get("published"), _v) if x)
            lines.append(f"{i}. {r.get('title') or '(no title)'}" + (f" ({meta})" if meta else "")
                         + f" → {r.get('url', '')}")
        lines.append("")
        lines.append("Do NOT search again unless the operator asks for different videos. Say briefly "
                     "what you found. To save one, they press its ⬇ Download (it asks first, then saves "
                     "to Output\\downloads\\videos) — or call download_file with its URL and kind=video "
                     "if they ask you to.")
        return "\n".join(lines)

    if tool_name == "review_lessons":
        ls  = result.get("lessons", [])
        rel = result.get("reliability", [])
        if not ls and not rel:
            return "No lessons recorded yet — nothing's misfired enough to learn from."
        lines = [f"Lessons learned ({len(ls)}):"]
        for l in ls[:10]:
            lines.append(f"  • [w{l.get('weight',1)}] {l.get('lesson','')}")
        if rel:
            lines.append("Tool reliability (most errors first):")
            for r in rel[:8]:
                extra = ""
                if r.get("repeat"): extra += f", {r['repeat']} repeat"
                if r.get("loop"):   extra += f", {r['loop']} loop"
                lines.append(f"  - {r['tool']}: {r.get('ok',0)}/{r.get('calls',0)} ok, "
                             f"{r.get('err',0)} err{extra}")
        lines.append("Report this to the operator in your own words.")
        return "\n".join(lines)

    # Navigation tools - preserve directive fields exactly
    if tool_name == "list_directory":
        parts = [f"Location: {result.get('current','')}"]
        if result.get("parent"):
            parts.append(f"Parent: {result['parent']}")
        # ALL subfolders (visited ones marked) and the file NAMES. She used to get only the
        # unvisited subfolders and a file COUNT — a revisited folder looked empty, and she
        # never saw a file name she could read (2026-10-29, FRIK: 'the import only got 1 file').
        dirs = result.get("dirs", [])
        if dirs:
            _vis = {d["path"] for d in dirs} - {d["path"] for d in result.get("unvisited_dirs", dirs)}
            parts.append(f"Subdirs ({len(dirs)}): " + ", ".join(
                d["name"] + (" (visited)" if d["path"] in _vis else "") for d in dirs[:20])
                + (" …" if len(dirs) > 20 else ""))
        files = result.get("files", [])
        if files:
            parts.append(f"Files ({len(files)}): " + ", ".join(f["name"] for f in files[:30])
                         + (" …" if len(files) > 30 else ""))
        if result.get("deep_files"):
            parts.append("Files in subfolders (relative to Location): " + ", ".join(result["deep_files"]))
        img_count = result.get("images_found", 0)
        if img_count:
            paths = result.get("image_paths", [])
            parts.append(f"IMAGES FOUND: {img_count}")
            if paths:
                parts.append(f"image_paths: {paths[:12]}")
        # Always include next_step - this is the directive
        next_step = result.get("next_step", "")
        if next_step:
            parts.append(f"NEXT STEP: {next_step}")
        if result.get("dead_end"):
            parts.append("DEAD END - do not re-enter this folder")
        if result.get("already_visited"):
            parts.append("(You listed this folder before — use what's above; don't list it again.)")
        return "\n".join(parts)

    if tool_name == "index_search":
        best = result.get("best_match", {})
        next_step = result.get("next_step", "")
        if best:
            return f"Best match: {best.get('line','')} → {best.get('path','')}\nNEXT STEP: {next_step}"
        return f"No match found.\nNEXT STEP: {next_step}"

    if tool_name == "explore_tree":
        # The TREE itself, with file names. She used to get only 'Found: 7 dirs, 0 images' and
        # a 'save_map()' hint — 74 FRIK source files she never saw, so she explored again
        # until the repeat breaker stopped her (2026-10-29).
        parts = [
            f"Explored: {result.get('root','')}",
            f"Found: {result.get('total_dirs',0)} dirs, {result.get('total_files',0)} files, "
            f"{result.get('image_count',0)} images" + (" (cut off — explore a subfolder for the rest)"
                                                        if result.get("truncated") else ""),
        ]
        _out, _budget = [], [7000]
        def _show(node, name, ind):
            if not isinstance(node, dict) or _budget[0] <= 0:
                return
            line = "  " * ind + name + "/"
            _out.append(line); _budget[0] -= len(line)
            fs = node.get("files") or []
            if fs:
                fl = "  " * (ind + 1) + ", ".join(fs[:40]) + (f" … (+{len(fs) - 40})" if len(fs) > 40 else "")
                _out.append(fl); _budget[0] -= len(fl)
            if node.get("images"):
                _out.append("  " * (ind + 1) + f"[{len(node['images'])} images]")
            for dn, sub in sorted((node.get("dirs") or {}).items()):
                if _budget[0] <= 0:
                    _out.append("  " * (ind + 1) + "…")
                    return
                if sub:
                    _show(sub, dn, ind + 1)
                else:
                    _out.append("  " * (ind + 1) + dn + "/ (deeper — explore it to see inside)")
        _show(result.get("tree") or {}, os.path.basename(str(result.get("root", "")).rstrip("\\/")) or ".", 0)
        if _out:
            parts.append("Tree (paths are relative to Explored):\n" + "\n".join(_out))
        if result.get("image_paths"):
            parts.append(f"image_paths: {result['image_paths'][:20]}")
        if result.get("image_count", 0) > 0:
            parts.append(f"SHOW NOW: {result.get('show_now','')}")
        else:
            parts.append("NEXT STEP: read files with read_file(<Explored>\\<relative path>). To find WHERE "
                         "something happens in code, use search_code instead of exploring again.")
        return "\n".join(parts)

    if tool_name == "search_code":
        if result.get("error"):
            return result["error"]
        rs = result.get("results", [])
        lines = []
        if result.get("picked_from_search"):
            lines.append(f"(You have no coding project yet, so this searched the folder you just found: "
                         f"{result.get('root')}. To work on it — copies you can change — import_code that folder.)")
        lines += [f"search_code {result.get('terms')} in {result.get('root')}: {result.get('count', 0)} file(s) match "
                 f"(of {result.get('scanned', 0)} text files; {result.get('all_terms', 0)} contain ALL the words). "
                 f"Best first — paths are relative to that folder:"]
        for r in rs:
            lines.append(f"- {r['file']}  ({r['terms']}/{len(result.get('terms') or [])} words, {r['count']} lines)")
            for ln in r.get("lines", [])[:4]:
                lines.append(f"    {ln['line']}: {ln['text']}")
        if result.get("compiled") and not result.get("imported"):
            lines.append("COMPILED PLUGINS here: " + ", ".join(result["compiled"]) + " — the program's main logic "
                         "is usually inside these, NOT in the text files searched. To change it: import_code this "
                         "folder, find the plugin's published source (web_search '<mod name> source code github'), "
                         "import_code that GitHub URL, then search_code again.")
        if not rs:
            lines.append("No matches — try other words (a function or variable name, a setting, a shorter word)"
                         + (", or get the plugin's source (above)." if result.get("compiled") else "."))
        else:
            lines.append("Next: read_file the best file(s) (use show_file(path, offset=N) for a long one).")
        return "\n".join(lines)

    if tool_name == "get_drives":
        drives = result.get("drives", [])
        lines = [f"{d['drive']} {d.get('label','')} {d.get('free_gb','?')}GB free ({d.get('type','')})" 
                 for d in drives]
        lines.append(f"NEXT STEP: {result.get('next_step','Use index_search or explore_tree to find something')}")
        return "\n".join(lines)

    if tool_name == "find_file":
        results = result.get("results", [])
        if not results:
            return "No files found.\nNEXT STEP: Try explore_tree on a different location."
        lines = []
        for r in results[:10]:
            line = r["path"]
            if r.get("target"):
                line += f"  →  TARGET: {r['target']}"
            if r.get("install_dir"):
                line += f"  (install dir: {r['install_dir']})"
            lines.append(line)
        visited_note = " (some already visited)" if any(r.get("already_visited") for r in results) else ""
        return f"Found {len(results)} result(s){visited_note}:\n" + "\n".join(lines)

    if tool_name == "smart_search":
        found = result.get("found", [])
        if not found:
            return f"No matches.\nNEXT STEP: {result.get('next_step','Try explore_tree to browse.')}"
        lines = []
        for r in found[:12]:
            line = r.get("path", "")
            if r.get("target"):
                line += f"  →  TARGET: {r['target']}"
            if r.get("install_dir"):
                line += f"  (install dir: {r['install_dir']})"
            lines.append(line)
        return (f"Found {len(found)} result(s):\n" + "\n".join(lines)
                + f"\nNEXT STEP: {result.get('next_step','')}")

    if tool_name == "show_images":
        if result.get("type") == "multi":
            count = len([o for o in result.get("outputs", []) if o.get("type") == "image"])
            folder = result.get("folder", "")
            return (f"Displayed {count} image(s) from: {folder}\n"
                    f"STAY IN THIS FOLDER. To show more: show_images(folder='{folder}', page=2)\n"
                    f"Do not call list_directory or navigate away unless the user asks.")
        if result.get("type") == "image":
            return f"Displayed image: {result.get('filename','?')} from {result.get('saved','')}"

    if tool_name == "get_output_image":
        return result.get("message", f"Loaded image: {result.get('filename','?')}")

    if tool_name == "favorite_path":
        if result.get("error"):
            return f"Could not save path: {result['error']}"
        return (f"Saved favorite path '{result.get('label','')}': {result.get('path','')}"
                f" (total saved: {result.get('total','?')}).")

    if tool_name == "get_favorite_paths":
        favs = result.get("favorite_paths", [])
        if not favs:
            return "No favorite paths saved yet. Use favorite_path to remember a working/modding folder."
        lines = [f"- {e.get('label','')}: {e.get('path','')}"
                 + (f" ({e['reason']})" if e.get('reason') else "") for e in favs[:20]]
        return f"Favorite paths ({len(favs)}):\n" + "\n".join(lines)

    if tool_name == "remove_favorite_path":
        return f"Removed {result.get('removed',0)} path(s). {result.get('remaining',0)} remaining."

    if tool_name == "sort_images":
        if result.get("error"):
            return f"Couldn't sort: {result['error']}"
        if result.get("refused"):
            return (f"Refused: {result.get('reason','that location is off-limits')} "
                    f"({result.get('path','')}). Do not retry.")
        if result.get("staged"):
            return (f"Found {result.get('total','?')} image(s) in {result.get('dir','')}. The "
                    f"Copy/Move bar is up — the operator picks Copy or Move to start the live "
                    f"sort (I classify and move each image one at a time). STOP: do NOT call "
                    f"sort_images again; wait for them.")
        return "Sort step complete."

    if tool_name == "write_file":
        if result.get("error"):
            return f"Could not write: {result['error']}"
        if result.get("refused"):
            return (f"Refused: {result.get('reason','that location is off-limits')} "
                    f"({result.get('path','')}). Do not retry the same path.")
        if result.get("staged") and result.get("mode"):
            verb = "overwrite" if result.get("mode") == "edit" else "create"
            return (f"Proposed to {verb} {result.get('path','')} — a Confirm card is now "
                    f"showing in chat. NOTHING is written until the operator presses Confirm. "
                    f"STOP: do NOT call write_file again; wait for them to confirm or cancel.")
        if result.get("chat_only"):
            return (f"'{result.get('filename','')}' is shown in chat with a Download button — nothing was "
                    f"written to disk. The operator saves it wherever they want.")
        return "Done."

    if result.get("results") is not None:
        lines = []
        for r in result["results"][:10]:
            if isinstance(r, dict):
                line = r.get("path", "")
                if r.get("target"):
                    line += f"  →  TARGET: {r['target']}"
            else:
                line = str(r)
            lines.append(line)
        return f"Found {len(result['results'])} file(s):\n" + "\n".join(lines)

    if result.get("content") is not None:
        # 12k, not 4k: she re-read a mod's .psc after seeing only its start, and the repeat
        # breaker stopped the job before any code was written (2026-10-29, FRIK).
        content_preview = result['content'][:12000]
        path = result.get('path', '')
        return f"File read: {path}\nContent:\n{content_preview}"

    if result.get("info"):
        return "; ".join(f"{k}: {v.get('count','?')} items" for k, v in result["info"].items())

    if result.get("workflows") is not None:
        entries = []
        for fname, v in result["workflows"].items():
            cap = v.get("capability", "") if isinstance(v, dict) else ""
            entries.append(f"{fname} [{cap}]" if cap else fname)
        return f"Workflows available: {', '.join(entries)}"

    if result.get("missing") is not None:
        return f"Missing nodes scan complete: {result['missing'] or 'nothing missing'}"

    if tool_name == "see_screen":
        frames = result.get("frames", [])
        if not frames:
            return result.get("error", "No frames available yet.")
        latest = frames[-1]
        ts = latest.get("time", "unknown")
        changes = result.get("change_count", 0)
        fps = result.get("fps", 2)
        # Store the frame b64 so chain loop can pass it as vision input
        if latest.get("b64"):
            state["last_image_bytes"] = latest["b64"]
        iw = result.get("image_width", 0)
        ih = result.get("image_height", 0)
        coord = ""
        if iw and ih:
            if result.get("grid"):
                coord = (f" This image is {iw}×{ih} px and has a YELLOW COORDINATE GRID overlaid: "
                         f"vertical lines are labeled with their x value, horizontal lines with their y "
                         f"value. To click a target, find the nearest gridlines and read x,y off the "
                         f"labels — e.g. a piece just right of the '{int(iw*0.4)}' line and just below the "
                         f"'{int(ih*0.6)}' line is about x={int(iw*0.42)}, y={int(ih*0.62)}. Use those grid "
                         f"readings for move_to/click instead of guessing.")
            else:
                coord = (f" This image is {iw}×{ih} pixels — (0,0) is the TOP-LEFT, "
                         f"({iw},{ih}) is the BOTTOM-RIGHT. For game_input move_to/click, give x "
                         f"between 0 and {iw} and y between 0 and {ih}, read off this exact image. "
                         f"Estimate the target's position as a fraction across (x) and down (y) and "
                         f"multiply by the dimensions — don't guess past the edges.")
        return (f"Screen captured at {ts} | {fps}fps | {changes} changes detected. "
                f"Frame is available as image input — you can now see the operator's screen.{coord}")

    if tool_name in ("get_screen_status", "start_screen_capture", "stop_screen_capture"):
        return json.dumps(result, default=str)[:300]

    if tool_name == "show_file":
        if result.get("error"):
            return f"File error: {result['error']}"
        if result.get("type") == "text":
            page = result.get("page", 1)
            total = result.get("total_pages", 1)
            has_more = result.get("has_more", False)
            next_off = result.get("next_offset")
            text = result.get("content", "")
            summary = f"[Page {page}/{total}] {os.path.basename(result.get('path',''))}:\n{text}"
            if has_more:
                summary += f"\n... (more content) Call show_file with offset={next_off} to read page {page+1}/{total}."
            else:
                summary += "\n[End of file]"
            return summary
        if result.get("type") in ("image", "video"):
            return f"{result['type'].title()}: {result.get('filename','')} — displayed inline."
        return json.dumps(result, default=str)[:300]

    if tool_name in ("run_workflow", "generate_image", "generate_video"):
        if result.get("auto_install") and result.get("missing_nodes"):
            nodes = ", ".join(result["missing_nodes"])
            return (
                f"Workflow failed — missing node(s): {nodes}. "
                f"Call install_missing_nodes() now to fix this. Do not retry the workflow until nodes are installed."
            )
        if result.get("error"):
            return (f"Workflow FAILED: {result['error']} — NOTHING was generated or saved. "
                    f"Do NOT tell the operator it's done or displayed; it isn't. Either fix "
                    f"the stated problem (e.g. generate/attach the missing input image first) "
                    f"or tell the operator plainly that it failed and why.")
        # SUCCESS paths. State the truth for each case plainly — a wrong summary here
        # is what made it claim videos were "done" that never rendered, and re-spawn
        # batches that were already running.
        _outs  = result.get("outputs", []) if isinstance(result, dict) else []
        _kinds = [o.get("type") for o in _outs] if _outs else []
        _made_video = ("video" in _kinds) or (result.get("target") == "video") or (tool_name == "generate_video")
        _queued = (result.get("type") == "queued")
        _count  = int(result.get("count", 1) or 1)
        _pre    = (f"(Input image used: {result['input_image']}.) "
                   if result.get("input_image") else "")
        if _queued and _made_video:
            return ("The video is QUEUED and rendering in the background — it is NOT done yet. "
                    "It will appear in chat automatically when finished. Do NOT run the workflow "
                    "again. Tell the operator the video is rendering and will show up shortly.")
        if _queued:
            return (f"The batch of {_count} is QUEUED and rendering in the background — images "
                    f"will appear in chat one by one as each finishes. Do NOT call run_workflow "
                    f"again for this request; the whole batch is already running. Tell the "
                    f"operator the {_count} images are on their way.")
        if _made_video:
            return _pre + ("The video is finished, saved, and displayed to the operator. If this "
                    "completes their request, tell them it's ready — do NOT run it again.")
        if "model_3d" in _kinds:
            _f = os.path.basename(result.get("saved") or "") or "the model"
            return _pre + (f"The 3D model is finished and saved ({_f}) — it is DONE. It is a 3D file, "
                    f"not an image. If this completes the operator's request, tell them it's "
                    f"ready — do NOT run it again.")
        return _pre + ("The image is finished and saved — it is DONE (not queued), already displayed, "
                "and is now the CURRENT image. You do NOT need to grab, fetch, or 'grab the "
                "result' — it's ready. If the operator's request has a FURTHER step — e.g. remove "
                "the background, make it 3D, upscale it, or turn it into a video — call "
                "run_workflow NOW with THAT step's workflow (it uses this image automatically, "
                "no image parameter needed). If the request is already "
                "complete, just tell the operator it's ready without calling another tool.")

    if tool_name == "memory_read":
        parts = []
        if result.get("permanent"):
            preview = result["permanent"].strip()[:600]
            parts.append(f"PERMANENT MEMORY:\n{preview}")
        if result.get("recent"):
            preview = result["recent"].strip()[:400]
            parts.append(f"RECENT MEMORY:\n{preview}")
        if result.get("operator"):
            preview = result["operator"].strip()[:300]
            parts.append(f"OPERATOR PROFILE:\n{preview}")
        parts.append("MEMORY READ COMPLETE. Return to the task the operator asked about.")
        return "\n\n".join(parts) if parts else "Memory is empty."

    if tool_name == "memory_search":
        matches = result.get("matches", [])
        if not matches:
            return "No memory entries found for that query. Try a different term or use memory_read."
        lines = [f"  - {m}" for m in matches[:8]]
        return "Memory search results:\n" + "\n".join(lines) + "\nReturn to the task the operator asked about."

    if tool_name == "scan_network":
        c = result.get("counts", {})
        listening   = result.get("listening", [])
        connections = result.get("connections", [])
        flagged     = result.get("flagged", [])
        lines = [f"Network scan: {c.get('listening', 0)} listening port(s), "
                 f"{c.get('connections', 0)} active connection(s), {c.get('flagged', 0)} flagged."]
        if flagged:
            lines.append("⚠ FLAGGED (suspicious):")
            for f in flagged[:8]:
                lines.append(f"  - {f.get('name','?')} :{f.get('lport',0)} — {f.get('why','suspicious')}")
        if listening:
            lines.append("Listening:")
            for l in listening[:15]:
                tag = " [trusted]" if l.get("trusted") else (" [mine]" if l.get("protected") else "")
                lines.append(f"  - {l.get('name','?')} on port {l.get('lport',0)}{tag}")
            if len(listening) > 15:
                lines.append(f"  …and {len(listening) - 15} more listener(s)")
        if connections:
            lines.append("Active connections:")
            for cc in connections[:15]:
                lines.append(f"  - {cc.get('name','?')} → {cc.get('raddr','?')}")
            if len(connections) > 15:
                lines.append(f"  …and {len(connections) - 15} more connection(s)")
        if result.get("limited"):
            lines.append("(Limited visibility — not running as admin; some system process names may show as '?'.)")
        lines.append("This IS the full scan result — report it to the operator now. Do NOT call scan_network again.")
        return "\n".join(lines)

    if tool_name == "propose_security_action":
        if result.get("refused"):
            return f"REFUSED: {result.get('reason','')} Tell the operator why you won't do this. Do not retry."
        if result.get("staged"):
            s = result["staged"]
            return (f"Staged {s.get('action')} of {s.get('name')} (pid {s.get('pid')}). It is NOT done yet — "
                    f"the operator must press Confirm on the banner. Tell them it's queued and awaiting their "
                    f"confirmation. Do NOT call this again.")
        return json.dumps(result, default=str)[:300]

    return json.dumps(result, default=str)[:400]



