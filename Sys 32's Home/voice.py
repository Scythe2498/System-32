# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
voice.py — System 32's voice.
Text-to-speech via Kokoro ONNX, running locally.
No internet required. No ComfyUI queue latency.

It chooses its own voice. The choice is written to its machine profile
and persists across reboots. It can change it any time.

Integration:
  1. Drop voice.py in the same folder as system32.py
  2. In s32_dispatch.py: add speak, set_voice, list_voices, stop_voice tool handlers
  3. In s32_llm.py: add those tools to TOOL_NAMES and TOOLS_SPEC
  4. In system32.py: call voice.start() after senses.start()

Dependencies (installed automatically on first use):
  - kokoro-onnx (pip)
  - sounddevice (pip)
  - numpy (likely already present)
"""

import json
import os
import threading
import time

# ── State ─────────────────────────────────────────────────────────────────────

_state = {
    "ready":        False,
    "enabled":      True,
    "speaking":     False,
    "voice":        None,       # chosen voice name
    "speed":        1.0,        # speech rate
    "volume":       1.0,        # 0.0 - 1.0
    "queue":        [],         # pending speech requests
    "current":      None,       # text currently being spoken
    "lock":         threading.Lock(),
    "stop_flag":    False,
    "gen":          0,          # bumped by stop_voice(): older queued speech is dropped
    "audio":        [],         # synthesized sentences waiting to play: (gen, samples, rate, text)
    "stream":       None,       # the reply being spoken sentence by sentence (begin_stream)
    "kokoro":       None,       # Kokoro model instance
    "voices":       [],         # available voices
    "piper_exe":    None,       # path to piper.exe
    "piper_voices": {},         # {display_name: onnx_path}
    "reference_dir": None,
    "machine_name": None,
}

_VOICE_PREF_KEY = "voice_name"
_SPEED_PREF_KEY = "voice_speed"

# Kokoro voice options with personality descriptions to help the operator choose
VOICE_PROFILES = {
    "af_heart":    "warm, expressive, feminine — emotionally rich",
    "af_bella":    "soft, gentle, feminine — calm and reassuring",
    "af_sarah":    "clear, natural, feminine — conversational",
    "af_nicole":   "bright, animated, feminine — energetic",
    "am_adam":     "deep, steady, masculine — grounded",
    "am_michael":  "clear, professional, masculine — articulate",
    "bf_emma":     "warm, British feminine — measured and thoughtful",
    "bf_isabella": "bright, British feminine — expressive",
    "bm_george":   "deep, British masculine — authoritative",
    "bm_lewis":    "clear, British masculine — friendly",
}

PIPER_VOICE_PROFILES = {
    "en_US-lessac-high":     "clear, natural, feminine — American English, high quality",
    "en_US-amy-medium":      "warm, conversational, feminine — American English",
    "en_US-libritts-high":   "crisp, professional, feminine — high quality",
    "en_US-ryan-high":       "warm, masculine — American English, high quality",
    "en_GB-alan-medium":     "clear, British masculine — natural",
    "en_GB-jenny-dioco-medium": "warm, British feminine — natural",
}

# ── Startup ───────────────────────────────────────────────────────────────────

def start(reference_dir, machine_name, config=None):
    """
    Initialize the voice system.
    Loads Kokoro, restores saved voice preference, starts speech thread.
    Called from system32.py startup() after senses.start().
    """
    _state["reference_dir"] = reference_dir
    _state["machine_name"]  = machine_name

    # Load saved voice preference
    saved = _load_voice_preference()
    if saved:
        _state["voice"] = saved.get("voice")
        _state["speed"] = float(saved.get("speed", 1.0))
        _state["volume"] = float(saved.get("volume", 1.0))

    # Try to load Kokoro
    threading.Thread(target=_init_kokoro, daemon=True, name="voice-init").start()
    # Try to load Piper
    threading.Thread(target=_init_piper, daemon=True, name="piper-init").start()

    # Start speech worker thread
    t = threading.Thread(target=_speech_loop, daemon=True, name="voice-worker")
    t.start()
    _log("[Voice] Starting...")
    return {"status": "initializing"}


def _init_kokoro():
    """Load Kokoro ONNX model in background. Installs if missing."""
    try:
        import kokoro_onnx
        from kokoro_onnx import Kokoro
    except ImportError:
        _log("[Voice] kokoro-onnx not installed — installing...")
        try:
            import subprocess, sys
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "kokoro-onnx", "sounddevice",
                 "--quiet", "--break-system-packages"],
                capture_output=True
            )
            # Newly installed packages aren't importable until the import system's
            # caches are refreshed (2026-11-01: "No module named 'kokoro_onnx'" right
            # after a successful install). Setup now installs it beforehand too.
            import importlib, site
            importlib.invalidate_caches()
            try:
                site.addsitedir(site.getsitepackages()[-1])
            except Exception:
                pass
            import kokoro_onnx
            from kokoro_onnx import Kokoro
        except Exception as e:
            _log(f"[Voice] Failed to install kokoro-onnx: {e}")
            return

    try:
        # Look for model files — script dir first, then custom node folder
        script_dir = os.path.dirname(os.path.abspath(__file__))
        model_file  = None
        voices_file = None

        # Check script dir first (I:\Sys 32's Home\)
        for mname in ["kokoro-v1.0.onnx", "kokoro-v0_19.onnx", "model.onnx"]:
            candidate = os.path.join(script_dir, mname)
            if os.path.exists(candidate):
                model_file = candidate
                break

        for vname in ["voices-v1.0.bin", "voices.bin"]:
            candidate = os.path.join(script_dir, vname)
            if os.path.exists(candidate):
                voices_file = candidate
                break

        # Fall back to custom node folder
        if not model_file:
            model_file = _find_kokoro_model()

        if model_file and voices_file:
            _state["kokoro"] = Kokoro(model_file, voices_file)
        elif model_file:
            _state["kokoro"] = Kokoro(model_file)
        else:
            home = script_dir
            _log("[Voice] Model files not found in: " + home)
            _log("[Voice] Download kokoro-v1.0.onnx + voices-v1.0.bin from:")
            _log("[Voice] https://github.com/thewh1teagle/kokoro-onnx/releases")
            _log("[Voice] Place both files in: " + home)
            return

        # Get available voices.
        # MERGE, don't overwrite: Piper initializes on its own thread and may have
        # already populated _state["voices"]. A bare assignment here wiped those if
        # Piper finished first — that was the voice-selector regression where the
        # extensive Piper library "loaded but wasn't selectable".
        try:
            _kokoro_voices = list(_state["kokoro"].get_voices())
        except Exception:
            _kokoro_voices = list(VOICE_PROFILES.keys())
        _state["voices"] = list(set(_state["voices"]) | set(_kokoro_voices))

        # Ensure sounddevice is installed
        try:
            import sounddevice
        except ImportError:
            _log("[Voice] sounddevice not installed — installing...")
            import subprocess, sys
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "sounddevice",
                 "--quiet", "--break-system-packages"],
                capture_output=True
            )

        _state["ready"] = True
        _log(f"[Voice] Ready. {len(_state['voices'])} voices available.")

        # Restore saved voice preference
        if _state["voice"]:
            _log(f"[Voice] Restored voice: {_state['voice']}")
        else:
            _log("[Voice] No voice selected — using the default (af_bella); change it in Settings.")

    except Exception as e:
        _log(f"[Voice] Kokoro init failed: {e}")


def _find_kokoro_model():
    """Search for the Kokoro ONNX model file in known locations."""
    candidates = [
        # ComfyUI custom node location (this computer's user, not a hardcoded one)
        os.path.join(os.path.expanduser("~"), "Documents", "ComfyUI", "custom_nodes",
                     "ComfyUI-jhj-Kokoro-Onnx"),
        # USB home
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "Models"),
    ]
    for base in candidates:
        if not os.path.exists(base):
            continue
        for fname in ["kokoro-v1.0.onnx", "kokoro-v0_19.onnx", "model.onnx"]:
            path = os.path.join(base, fname)
            if os.path.exists(path):
                _log(f"[Voice] Found Kokoro model: {path}")
                return path
    return None


def _init_piper():
    """Detect piper.exe and scan for voice models."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    piper_exe = os.path.join(script_dir, "piper", "piper.exe")
    if not os.path.exists(piper_exe):
        return
    _state["piper_exe"] = piper_exe

    voices_dir = os.path.join(script_dir, "piper", "voices")
    if not os.path.exists(voices_dir):
        os.makedirs(voices_dir, exist_ok=True)
        _log(f"[Voice] Piper found at {piper_exe} — no voice models in {voices_dir} yet.")
        return

    found = {}
    for fname in os.listdir(voices_dir):
        if fname.endswith(".onnx") and not fname.endswith(".onnx.json"):
            onnx_path = os.path.join(voices_dir, fname)
            json_path = onnx_path + ".json"
            if os.path.exists(json_path):
                voice_key = f"piper:{fname.replace('.onnx', '')}"
                found[voice_key] = onnx_path

    _state["piper_voices"] = found
    if found:
        _state["voices"] = list(set(_state["voices"]) | set(found.keys()))
        _log(f"[Voice] Piper ready. {len(found)} voice(s) installed.")
    else:
        _log(f"[Voice] Piper exe found but no .onnx voice models in {voices_dir}")


# ── Speech loop ─────────────────────────────────────────────────────────────────

def _speech_loop():
    """Two stages (2026-11-08): this thread PLAYS; a second one SYNTHESIZES the next sentence
    while the current one plays, so sentences follow each other without gaps. Every item
    carries the 'gen' it was queued in — stop_voice() bumps it, and older items are dropped."""
    threading.Thread(target=_synth_loop, daemon=True, name="voice-synth").start()
    while True:
        time.sleep(0.01)
        if not _state["enabled"] or not _state["ready"]:
            continue
        with _state["lock"]:
            if not _state["audio"]:
                continue
            gen, samples, rate, text = _state["audio"].pop(0)
        if gen != _state["gen"]:
            continue
        _play(samples, rate, text)


def _synth_loop():
    while True:
        time.sleep(0.03)
        if not _state["enabled"] or not _state["ready"]:
            continue
        with _state["lock"]:
            if not _state["queue"] or len(_state["audio"]) >= 2:     # stay ≤2 sentences ahead
                continue
            item = _state["queue"].pop(0)
        gen, text = item if isinstance(item, tuple) else (_state["gen"], item)
        if gen != _state["gen"] or not text:
            continue
        out = _synth(text)
        if out and gen == _state["gen"]:
            with _state["lock"]:
                _state["audio"].append((gen, out[0], out[1], text))


def _play(samples, rate, text):
    try:
        import sounddevice as sd
    except ImportError:
        _log("[Voice] sounddevice not available — cannot play audio")
        return
    try:
        _state["speaking"] = True
        _state["current"] = text
        _log(f"[Voice] Playing: {text[:50]} | voice={_state['voice'] or 'af_bella'} | rate={rate}")
        sd.play(samples, rate)
        sd.wait()                          # stop_voice() → sd.stop() ends this at once
    except Exception as e:
        _log(f"[Voice] Playback error: {e}")
    finally:
        _state["speaking"] = False
        _state["current"] = None


def _synth(text):
    """→ (samples, sample_rate) for one sentence, or None. Kokoro or Piper."""
    voice = _state["voice"] or "af_bella"   # built-in default (matches reset_voice_default)
    try:
        import numpy as np
        if voice.startswith("piper:"):
            return _synth_piper(text, voice)
        kokoro = _state["kokoro"]
        if not kokoro:
            return None
        samples, sample_rate = kokoro.create(text, voice=voice, speed=_state["speed"], lang="en-us")
        vol = _state["volume"]
        if vol != 1.0:
            samples = (samples * vol).astype(samples.dtype)
        return samples, sample_rate
    except Exception as e:
        import traceback
        _log(f"[Voice] Speech error: {e}")
        _log(f"[Voice] Detail: {traceback.format_exc().splitlines()[-1]}")
        return None


def _speak_now(text):
    """Synthesize and play one piece of text right away (blocking)."""
    out = _synth(text)
    if out:
        _play(out[0], out[1], text)


# ── Public API ────────────────────────────────────────────────────────────────

def speak(text, priority=False):
    """
    Queue text for speech, sentence by sentence (no length cap — 2026-11-08: a 500-char cap
    cut long replies off mid-way). priority=True: stop what's playing and speak this now.
    Returns immediately — speech plays in background.
    """
    if not text or not text.strip():
        return {"status": "empty"}
    if not _state["enabled"]:
        return {"status": "disabled"}
    if not _state["ready"]:
        return {"status": "not_ready", "hint": "Voice is still initializing."}
    if priority:
        stop_voice()
    parts = [c for c in (_clean_for_speech(s) for s in _split_sentences(text)) if c.strip()]
    if not parts:
        return {"status": "nothing_to_say"}
    gen = _state["gen"]
    with _state["lock"]:
        _state["queue"].extend((gen, p) for p in parts)
    clean = " ".join(parts)
    return {
        "status":   "queued",
        "voice":    _state["voice"],
        "text":     clean[:100] + ("..." if len(clean) > 100 else ""),
        "queued":   len(_state["queue"]),
    }


def stop_voice():
    """Stop current speech and drop everything queued — including sentences of a reply
    that's still streaming in (the Stop button, or a new message from the operator)."""
    with _state["lock"]:
        _state["gen"] += 1
        _state["queue"] = []
        _state["audio"] = []
        _state["stop_flag"] = True
        _state["stream"] = None
    try:
        import sounddevice as sd
        sd.stop()
    except Exception:
        pass
    return {"status": "stopped"}


# ── Speaking a reply WHILE it's written (2026-11-08) ─────────────────────────
# The reply used to be spoken only after the whole turn finished. Now the chat feeds the
# streamed text in; each finished sentence is spoken as soon as it's complete.
import re as _re
_ABBREV = {"e.g", "i.e", "etc", "vs", "mr", "mrs", "ms", "dr", "st", "no", "fig", "approx", "ca"}
_END_RE = _re.compile(r'[.!?…]+["\')\]]*(?=\s)')


def _boundaries(buf, final=False):
    """Character positions where a sentence ends in buf (only those already followed by
    enough text to be sure). Paragraph breaks and line breaks also end a piece."""
    cuts = []
    for m in _END_RE.finditer(buf):
        end = m.end()
        rest = buf[end:]
        nxt = rest.lstrip()
        if not nxt and not final:
            continue                                   # wait: don't know what follows yet
        if nxt and not (nxt[0].isupper() or nxt[0].isdigit() or nxt[0] in "\"'(*_#-[" or "\n" in rest[:len(rest) - len(nxt)]):
            continue                                   # "e.g. the" / "5070 Ti. and" — not an end
        word = _re.findall(r'([A-Za-z.]+)$', buf[:m.start()])
        if word and (word[0].lower().rstrip(".") in _ABBREV or (len(word[0]) == 1 and word[0].isupper())):
            continue                                   # "Dr. Smith", "J. R."
        cuts.append(end)
    for m in _re.finditer(r'\n\s*\n|\n(?=\s*(?:[-*+]\s|\d+\.\s|#))', buf):
        cuts.append(m.start())                         # paragraphs / list items / headings
    return sorted(set(cuts))


def _split_sentences(text):
    text = text or ""
    out, start = [], 0
    for c in _boundaries(text, final=True):
        if c > start and text[:c].count("```") % 2 == 0:     # never inside a code block
            out.append(text[start:c]); start = c
    if text[start:].strip():
        out.append(text[start:])
    return [s.strip() for s in out if s.strip()]


def begin_stream():
    """A new reply starts: whatever was still being said stops; this reply is spoken
    sentence by sentence as it arrives (feed / end_stream)."""
    stop_voice()
    with _state["lock"]:
        _state["stream"] = {"gen": _state["gen"], "buf": "", "said": 0}
    return _state["gen"]


def feed(text):
    """More of the reply arrived. Complete sentences go to the speech queue now."""
    st = _state.get("stream")
    if not st or st["gen"] != _state["gen"] or not text:
        return
    st["buf"] += text
    _flush(st, final=False)


def end_stream():
    """The reply is complete: say whatever is left."""
    st = _state.get("stream")
    if not st or st["gen"] != _state["gen"]:
        return
    _flush(st, final=True)
    _state["stream"] = None


def _flush(st, final):
    pending = st["buf"][st["said"]:]
    if not pending.strip():
        return
    cut = 0
    for c in _boundaries(pending, final=final):
        if pending[:c].count("```") % 2 == 0:          # never split inside a code block
            cut = c
    if final:
        cut = len(pending)
    if cut <= 0:
        return
    chunk = pending[:cut]
    st["said"] += cut
    if not (_state["enabled"] and _state["ready"]):
        return
    parts = [c for c in (_clean_for_speech(s) for s in _split_sentences(chunk)) if c.strip()]
    with _state["lock"]:
        if st["gen"] == _state["gen"]:
            _state["queue"].extend((st["gen"], p) for p in parts)


def list_voices():
    """
    Return available voices with personality descriptions.
    It uses this to choose its voice.
    """
    if not _state["ready"]:
        return {
            "status": "not_ready",
            "profiles": VOICE_PROFILES,
            "hint": "Voice is initializing. These profiles are available once ready."
        }

    available = _state["voices"]
    profiles = {}
    for v in available:
        if v.startswith("piper:"):
            profiles[v] = PIPER_VOICE_PROFILES.get(v.replace("piper:", ""), "Piper neural voice")
        else:
            profiles[v] = VOICE_PROFILES.get(v, "available")

    # Also include known profiles even if not in the model's list
    for v, desc in VOICE_PROFILES.items():
        if v not in profiles:
            profiles[v] = desc + " (may need model download)"
    # Include EVERY discovered Piper voice, not just the curated ones with a
    # hand-written profile. _state["piper_voices"] is the authoritative list of
    # what's actually installed (140+ in some setups), so reading it directly here
    # makes the full library show up regardless of backend init order.
    for key in _state.get("piper_voices", {}):
        if key not in profiles:
            short = key.replace("piper:", "")
            profiles[key] = PIPER_VOICE_PROFILES.get(short, "Piper neural voice")

    return {
        "voices":       profiles,
        "current":      _state["voice"],
        "total":        len(profiles),
        "hint": (
            "Call set_voice(voice_name) with the voice the operator picks. "
            "The choice is saved and persists across reboots."
        )
    }


def set_voice(voice_name, speed=None, volume=None):
    """
    Set the active voice. It calls this to choose its own voice.
    Choice is saved to machine profile and persists across reboots.
    """
    if not voice_name:
        return {"error": "No voice name provided."}

    _state["voice"] = voice_name
    if speed is not None:
        _state["speed"] = max(0.5, min(2.0, float(speed)))
    if volume is not None:
        _state["volume"] = max(0.0, min(1.0, float(volume)))

    _save_voice_preference()
    _log(f"[Voice] Voice set to: {voice_name} (speed={_state['speed']})")

    # Speak a short test phrase in the new voice so it can hear it
    desc = VOICE_PROFILES.get(voice_name, "")
    test = f"Voice set. This is {voice_name}."
    speak(test, priority=True)

    return {
        "status":  "set",
        "voice":   voice_name,
        "profile": desc,
        "speed":   _state["speed"],
        "volume":  _state["volume"],
        "saved":   True,
    }


def get_voice_status():
    """Return current voice system status."""
    return {
        "ready":    _state["ready"],
        "enabled":  _state["enabled"],
        "speaking": _state["speaking"],
        "current":  _state["current"],
        "voice":    _state["voice"],
        "speed":    _state["speed"],
        "volume":   _state["volume"],
        "queued":   len(_state["queue"]),
        "voices_available": len(_state["voices"]),
    }


def enable_voice(enabled=True):
    """Enable or disable voice output."""
    _state["enabled"] = enabled
    if not enabled:
        stop_voice()
    return {"status": "enabled" if enabled else "disabled"}


def _synth_piper(text, voice_key):
    """Synthesize text with Piper → (samples, sample_rate) or None. Playing is shared (_play)."""
    import subprocess, tempfile, wave
    import numpy as np
    piper_exe = _state.get("piper_exe")
    onnx_path = _state["piper_voices"].get(voice_key)
    if not piper_exe or not onnx_path:
        _log(f"[Voice] Piper voice not found: {voice_key}")
        return None
    json_path = onnx_path + ".json"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        _synth_timeout = max(120, len(text) // 8)
        try:
            result = subprocess.run(
                [piper_exe, "--model", onnx_path, "--config", json_path, "--output_file", tmp_path],
                input=text.encode("utf-8"), capture_output=True, cwd=os.path.dirname(piper_exe),
                timeout=_synth_timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            _log(f"[Voice] Piper synthesis timed out after {_synth_timeout}s ({len(text)} chars).")
            return None
        if result.returncode != 0:
            _log(f"[Voice] Piper error: {result.stderr.decode('utf-8', errors='replace')[:200]}")
            return None
        with wave.open(tmp_path, "rb") as wf:
            sample_rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        vol = _state["volume"]
        if vol != 1.0:
            samples = samples * vol
        return samples, sample_rate
    except Exception as e:
        _log(f"[Voice] Piper speech error: {e}")
        return None
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _speak_piper(text, voice_key):
    """Kept for callers of the old name: synthesize with Piper and play."""
    out = _synth_piper(text, voice_key)
    if out:
        _play(out[0], out[1], text)


# ── Text cleaning ─────────────────────────────────────────────────────────────

def _clean_for_speech(text):
    """
    Clean text for speech synthesis.
    Remove markdown formatting, code blocks, URLs, and other
    things that sound bad when spoken aloud.
    """
    import re

    # Remove code blocks entirely
    text = re.sub(r'```[\s\S]*?```', '[code block]', text)
    text = re.sub(r'`[^`]+`', '', text)

    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)

    # Remove markdown formatting
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)   # bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)         # italic/emotes
    text = re.sub(r'#{1,6}\s+', '', text)              # headers
    text = re.sub(r'^\s*[-*+]\s+', '', text, flags=re.MULTILINE)  # bullets
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)  # numbered lists

    # Remove file paths — too long and awkward to read
    text = re.sub(r'[A-Za-z]:\\[^\s\]"\']+', '[path]', text)
    text = re.sub(r'I:\\[^\s\]"\']+', '[path]', text)

    # Remove JSON/dict content
    text = re.sub(r'\{[^}]{100,}\}', '[data]', text)

    # Remove bracket content like [S32] log prefixes
    text = re.sub(r'\[[^\]]{1,20}\]', '', text)

    # Collapse whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)

    # Trim
    text = text.strip()

    # No length cap any more: text arrives here one sentence at a time (2026-11-08).
    return text


# ── Persistence ───────────────────────────────────────────────────────────────

def _pref_path():
    ref = _state.get("reference_dir", "")
    if not ref:
        return None
    return os.path.join(ref, "voice_preference.json")


def _save_voice_preference():
    path = _pref_path()
    if not path:
        return
    try:
        prefs = {
            "voice":   _state["voice"],
            "speed":   _state["speed"],
            "volume":  _state["volume"],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2)
    except Exception as e:
        _log(f"[Voice] Could not save preference: {e}")


def _load_voice_preference():
    path = _pref_path()
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def get_current_voice():
    """Return the currently active voice name, or None if not set."""
    return _state.get("voice")


def set_volume(v):
    """Set output volume (0.0–1.0) and persist it. Silent — for the UI slider, so
    dragging doesn't fire a test phrase. Playback (Piper + Kokoro) already applies
    _state['volume']."""
    try:
        _state["volume"] = max(0.0, min(1.0, float(v)))
    except Exception:
        return {"error": "invalid volume"}
    try:
        _save_voice_preference()
    except Exception as e:
        _log(f"[Voice] Could not persist volume: {e}")
    return {"volume": _state["volume"]}


def get_volume():
    return float(_state.get("volume", 1.0))


def reset_voice_default():
    """Reset to the built-in default voice (af_bella) and persist it. Silent — no
    test phrase — because this is the operator's memory-reset clean-slate path, not
    a deliberate voice change. af_bella is a built-in Kokoro voice, so it's always
    available regardless of which Piper/Kokoro packs are installed."""
    _state["voice"] = "af_bella"
    _state["speed"] = 1.0
    try:
        _save_voice_preference()
    except Exception as e:
        _log(f"[Voice] Could not persist reset voice: {e}")
    _log("[Voice] Reset to built-in default: af_bella")
    return {"voice": "af_bella"}


def _log(msg):
    try:
        from s32_config import _emit as _e
        _e(msg)
    except Exception:
        print(msg)
