# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_stt.py — Speech-to-text voice input for System 32.

INCREMENT 1 (this file): the microphone pipeline + live input-level meter.
  • list_microphones()      — enumerate input devices for the dropdown
  • set_microphone(index)   — choose which mic to use
  • toggle(on)              — turn listening on/off (mic is OFF until toggled,
                              so nothing captures audio unless the operator asks)
  • get_level()             — current input level (0–100) for the dB meter

It opens a sounddevice InputStream (same library the voice OUTPUT already uses,
so no new dependency) and computes a dBFS-based level in the audio callback. The
captured audio is NOT yet transcribed — that's increment 2 (faster-whisper + VAD
→ inject text into chat). This increment proves the audio path works first: pick
a mic, toggle on, watch the meter move when you speak.

Privacy: the stream only runs while toggled on; toggling off stops and closes it.
"""

import threading
import os
import json

from s32_config import state, log, REFERENCE_DIR

_lock   = threading.Lock()
_stream = None
_PREF   = os.path.join(REFERENCE_DIR, "stt_preference.json")

# Shared state (visible to the API + future transcription loop)
_stt = state.setdefault("stt", {
    "enabled":      False,
    "device":       None,    # input device index, or None = system default
    "mode":         "continuous",  # "continuous" (VAD) or "ptt" (push-to-talk)
    "samplerate":   16000,
    "level":        0,        # 0–100 for the meter
    "error":        None,
    "transcribing": False,    # increment 2
})


def _load_pref():
    """Restore saved mic + mode (does NOT auto-start listening — mic stays off)."""
    try:
        with open(_PREF, encoding="utf-8") as f:
            p = json.load(f)
        if isinstance(p, dict):
            if "device" in p:
                _stt["device"] = p["device"]
            if p.get("mode") in ("continuous", "ptt"):
                _stt["mode"] = p["mode"]
    except Exception:
        pass


def _save_pref():
    try:
        with open(_PREF, "w", encoding="utf-8") as f:
            json.dump({"device": _stt.get("device"),
                       "mode": _stt.get("mode", "continuous")}, f)
    except Exception:
        pass


_load_pref()


def set_mode(mode):
    """Switch between continuous (VAD) and push-to-talk. Persists the choice."""
    m = "ptt" if str(mode).lower() in ("ptt", "push", "push-to-talk", "pushtotalk") \
        else "continuous"
    _stt["mode"] = m
    _save_pref()
    log(f"[S32][STT] mode = {m}")
    return {"ok": True, "mode": m}


def _sd():
    import sounddevice as sd
    return sd


def list_microphones():
    """Input devices for the dropdown: [{index, name, default}]."""
    try:
        sd = _sd()
        devices = sd.query_devices()
        try:
            default_in = sd.default.device[0]
        except Exception:
            default_in = -1
        mics = []
        for i, d in enumerate(devices):
            if d.get("max_input_channels", 0) > 0:
                mics.append({
                    "index":   i,
                    "name":    d.get("name", f"Device {i}"),
                    "default": (i == default_in),
                })
        return {"devices": mics, "selected": _stt.get("device"),
                "enabled": _stt.get("enabled", False), "mode": _stt.get("mode", "continuous"),
                "error": _stt.get("error")}
    except Exception as e:
        return {"devices": [], "selected": None, "enabled": False,
                "error": f"Audio device list unavailable: {str(e)[:120]}"}


def set_microphone(index):
    """Select the input device. Restarts the stream if currently listening."""
    try:
        idx = int(index) if index not in (None, "", "default") else None
    except (TypeError, ValueError):
        idx = None
    with _lock:
        _stt["device"] = idx
    _save_pref()
    if _stt.get("enabled"):
        stop_listening()
        start_listening()
    return {"ok": True, "device": _stt["device"]}


def _device_samplerate(sd, dev):
    try:
        info = sd.query_devices(dev if dev is not None else sd.default.device[0], "input")
        sr = int(info.get("default_samplerate") or 16000)
        return sr if sr > 0 else 16000
    except Exception:
        return 16000


def _level_callback(indata, frames, time_info, status):
    """Compute a dBFS-based 0–100 level from each audio block for the meter."""
    try:
        import numpy as np
        x = np.asarray(indata, dtype=np.float64).reshape(-1)
        if x.size == 0:
            return
        rms = float(np.sqrt(np.mean(np.square(x))))
        dbfs = 20.0 * np.log10(rms + 1e-9)          # ~-60 (quiet) .. 0 (max)
        level = int(max(0.0, min(100.0, (dbfs + 60.0) / 60.0 * 100.0)))
        # Light smoothing so the bar isn't jittery
        prev = _stt.get("level", 0)
        _stt["level"] = int(prev * 0.4 + level * 0.6)
        # Hand the raw block to the transcription feeder (PTT buffer / VAD)
        _feed(x.astype("float32"), x.size)
    except Exception:
        pass


def start_listening():
    global _stream
    with _lock:
        if _stream is not None:
            return {"ok": True, "enabled": True, "already": True}
        try:
            sd = _sd()
            dev = _stt.get("device")
            # Whisper wants 16 kHz; try it directly (PortAudio resamples on many
            # devices), else fall back to the device's native rate + resample later.
            sr = 16000
            try:
                sd.check_input_settings(device=dev, samplerate=16000, channels=1, dtype="float32")
            except Exception:
                sr = _device_samplerate(sd, dev)
            _stt["samplerate"] = sr
            _stream = sd.InputStream(
                device=dev, channels=1, samplerate=sr,
                blocksize=max(256, int(sr / 10)),    # ~100 ms blocks
                dtype="float32", callback=_level_callback,
            )
            _stream.start()
            _stt["enabled"] = True
            _stt["error"]   = None
            log(f"[S32][STT] listening (device={dev}, {sr} Hz)")
            _ensure_model()                          # warm up faster-whisper in the background
            return {"ok": True, "enabled": True}
        except Exception as e:
            _stream = None
            _stt["enabled"] = False
            _stt["error"]   = str(e)[:140]
            log(f"[S32][STT] start failed: {e}")
            return {"ok": False, "enabled": False, "error": str(e)[:140]}


def stop_listening():
    global _stream
    with _lock:
        s = _stream
        _stream = None
        _stt["enabled"] = False
        _stt["level"]   = 0
    if s is not None:
        try:
            s.stop()
            s.close()
        except Exception:
            pass
    log("[S32][STT] stopped")
    return {"ok": True, "enabled": False}


def toggle(on=None):
    """Flip listening, or set explicitly with on=True/False."""
    want = (not _stt.get("enabled")) if on is None else bool(on)
    return start_listening() if want else stop_listening()


def get_level():
    """Current meter level + status (polled by the UI ~10x/sec while on)."""
    return {
        "level":        _stt.get("level", 0),
        "enabled":      _stt.get("enabled", False),
        "mode":         _stt.get("mode", "continuous"),
        "transcribing": _stt.get("transcribing", False),
        "model_status": _stt.get("model_status", "idle"),
        "model_msg":    _stt.get("model_msg", ""),
        "error":        _stt.get("error"),
    }


# ════════════════════════════════════════════════════════════════════════════
#  Transcription engine (faster-whisper) — buffering, PTT + VAD triggers, inject
# ════════════════════════════════════════════════════════════════════════════
from s32_config import CONFIG

_MODEL     = None
_buf       = []                 # captured float32 samples (16 kHz mono)
_buf_lock  = threading.Lock()
_capturing = False              # PTT: True while the talk key is held
_vad       = {"speaking": False, "silence_ms": 0}

# VAD thresholds (0–100 level scale) — tunable via config; defaults are sane starts
_VAD_ON       = lambda: int(CONFIG.get("stt_vad_on", 30))    # rise above → speech
_VAD_OFF      = lambda: int(CONFIG.get("stt_vad_off", 18))   # fall below → maybe silence
_VAD_HANG_MS  = lambda: int(CONFIG.get("stt_vad_hang_ms", 800))   # silence to end utterance
_MIN_UTT_MS   = 350             # ignore sub-blips (noise)

_stt.setdefault("model_status", "idle")
_stt.setdefault("model_msg", "")


def _block_ms(frames):
    sr = _stt.get("samplerate", 16000) or 16000
    return int(frames * 1000 / sr)


def _append(samples):
    with _buf_lock:
        _buf.append(samples)


def _drain():
    with _buf_lock:
        if not _buf:
            return None
        import numpy as np
        seg = np.concatenate(_buf) if len(_buf) > 1 else _buf[0]
        _buf.clear()
        return seg


def _resample_16k(x):
    sr = _stt.get("samplerate", 16000) or 16000
    if sr == 16000:
        return x
    import numpy as np
    n = max(1, int(len(x) * 16000 / sr))
    return np.interp(np.linspace(0, 1, n, endpoint=False),
                     np.linspace(0, 1, len(x), endpoint=False), x).astype("float32")


def _pip_install_whisper():
    import sys
    import subprocess
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "faster-whisper",
                        "--break-system-packages", "--quiet"],
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                       timeout=900)
    except Exception as e:
        log(f"[S32][STT] engine install failed: {str(e)[:120]}")


def _probe_transcribe(model):
    """Force one short silent clip through transcribe so the real CUDA/cuBLAS code
    path runs at LOAD time. faster-whisper's transcribe() is lazy — the encoder
    (and its cuBLAS GEMMs) only execute as the returned generator is CONSUMED — so
    we must iterate it. Raises if the device can't actually transcribe (e.g. a
    missing cublas64_12.dll on the portable drive), letting _load_model fall to CPU.
    vad_filter is OFF on purpose: with it on, pure silence can skip the model
    entirely and the probe would pass without ever exercising cuBLAS."""
    import numpy as np
    silent = np.zeros(16000, dtype="float32")        # 1 s @ 16 kHz; encoder still runs
    segments, _info = model.transcribe(silent, language="en",
                                       beam_size=1, vad_filter=False)
    for _seg in segments:                            # consume → actually executes the GEMMs
        pass


def _load_model():
    global _MODEL
    if _MODEL is not None:
        return
    _stt["model_status"] = "loading"
    try:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            _stt["model_msg"] = "installing speech engine (one-time, a couple min)…"
            log("[S32][STT] faster-whisper not present — installing into bundled python")
            _pip_install_whisper()
        from faster_whisper import WhisperModel
        size = CONFIG.get("stt_model", "small")
        _stt["model_msg"] = f"loading {size} model…"
        for dev, ct in (("cuda", "int8_float16"), ("cpu", "int8")):
            try:
                m = WhisperModel(size, device=dev, compute_type=ct)
                # End-to-end probe: WhisperModel() can SUCCEED on cuda even when a
                # runtime CUDA DLL it only needs at transcribe time is missing
                # (cublas64_12.dll on the portable drive) — so STT would load "on
                # cuda" and then silently never return a transcript. Force one tiny
                # encode/decode here so a device that can't actually transcribe is
                # rejected now and we fall through to CPU cleanly.
                _probe_transcribe(m)
                _MODEL = m
                _stt["model_status"] = "ready"
                note = "" if dev == "cuda" else "  (CUDA unavailable — using CPU)"
                _stt["model_msg"] = f"{size} · {dev}{note}"
                log(f"[S32][STT] whisper {size} ready on {dev}{note}")
                return
            except Exception as e:
                log(f"[S32][STT] whisper {dev} unusable: {str(e)[:140]}")
                try:
                    del m                            # drop the half-built cuda model before CPU
                except Exception:
                    pass
        _stt["model_status"] = "error"
        _stt["model_msg"] = "model load failed (see log)"
    except ImportError:
        _stt["model_status"] = "error"
        _stt["model_msg"] = "speech engine install failed"
    except Exception as e:
        _stt["model_status"] = "error"
        _stt["model_msg"] = str(e)[:120]


def _ensure_model():
    if _stt.get("model_status") in ("loading", "ready") or _MODEL is not None:
        return
    threading.Thread(target=_load_model, daemon=True).start()


def _push_transcript(text):
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result({"type": "stt_transcript", "text": text}, jid)
        _cfg._push_done(jid)
        log(f"[S32][STT] transcript → chat: {text[:80]}")
    except Exception as e:
        log(f"[S32][STT] push transcript failed: {e}")


def _transcribe_and_inject(seg):
    if _MODEL is None or seg is None:
        return
    try:
        _stt["transcribing"] = True
        import numpy as np
        audio = _resample_16k(np.asarray(seg, dtype="float32").reshape(-1))
        if audio.size < int(16000 * _MIN_UTT_MS / 1000):
            return
        segments, _info = _MODEL.transcribe(
            audio, language=CONFIG.get("stt_language", "en"),
            vad_filter=True, beam_size=1)
        text = " ".join(s.text.strip() for s in segments).strip()
        if len(text) >= 2:
            _push_transcript(text)
    except Exception as e:
        log(f"[S32][STT] transcribe failed: {str(e)[:120]}")
    finally:
        _stt["transcribing"] = False


# ── PTT (push-to-talk): browser holds "V" → start/stop buffering ──────────────
def ptt_start():
    global _capturing
    if not _stt.get("enabled"):
        return {"ok": False, "error": "Mic is off."}
    _ensure_model()
    _drain()                 # clear any stale audio
    _capturing = True
    return {"ok": True, "capturing": True}


def ptt_stop():
    global _capturing
    if not _capturing:
        return {"ok": True, "capturing": False}
    _capturing = False
    seg = _drain()
    if seg is not None:
        threading.Thread(target=_transcribe_and_inject, args=(seg,), daemon=True).start()
    return {"ok": True, "capturing": False, "queued": seg is not None}


def _feed(samples, frames):
    """Called from the audio callback: buffer per the active mode."""
    mode = _stt.get("mode", "continuous")
    if mode == "ptt":
        if _capturing:
            _append(samples)
        return
    # continuous: simple energy VAD using the level we already computed
    if _stt.get("model_status") != "ready":
        return
    level = _stt.get("level", 0)
    if level >= _VAD_ON():
        _vad["speaking"] = True
        _vad["silence_ms"] = 0
        _append(samples)
    elif _vad["speaking"]:
        _append(samples)                       # keep trailing audio through the pause
        if level < _VAD_OFF():
            _vad["silence_ms"] += _block_ms(frames)
            if _vad["silence_ms"] >= _VAD_HANG_MS():
                _vad["speaking"] = False
                _vad["silence_ms"] = 0
                seg = _drain()
                if seg is not None:
                    threading.Thread(target=_transcribe_and_inject,
                                     args=(seg,), daemon=True).start()
        else:
            _vad["silence_ms"] = 0
