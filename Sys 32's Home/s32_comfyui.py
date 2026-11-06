# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_comfyui.py — ComfyUI launch (image + video), workflow engine,
                 image/video/lora generation runners.
"""
from s32_config import (
    CONFIG, WORKFLOW_DIR, _script_dir,
    state, log, http_get, http_post, is_up,
    _push_result, _push_done, _result_lock,
    os, json, copy, io, base64, uuid, random, time, threading, subprocess, urllib,
    shutil,
    PILImage,
)
from s32_hardware import (
    get_hardware, hardware_workflow_defaults, _scan_workflow_error,
)

# ── Session-image memory cap ──────────────────────────────────────────────────
# Every generated image used to keep its full raw bytes in state["session_images"]
# forever, so RAM grew without bound across a long session of generations — a
# real driver of slowdown after an hour of heavy use. We now keep raw bytes only
# for the most recent few (instant reuse); older entries keep just path+prompt and
# are re-read from disk on demand by get_output_image.
_SESSION_IMG_KEEP_BYTES = 12     # entries that retain raw bytes in RAM
_SESSION_IMG_MAX        = 200    # hard cap on entry count (files stay on disk)


def _cap_session_images():
    imgs = state.get("session_images")
    if not imgs:
        return
    if len(imgs) > _SESSION_IMG_KEEP_BYTES:
        for e in imgs[:-_SESSION_IMG_KEEP_BYTES]:
            if isinstance(e, dict) and "bytes" in e:
                e.pop("bytes", None)
    if len(imgs) > _SESSION_IMG_MAX:
        del imgs[:len(imgs) - _SESSION_IMG_MAX]

# Title keywords → (field_name, cast) for title-based patching
_TITLE_MAP = {
    # easy nodes use their own field names
    "positive":   ("positive", str),
    "negative":   ("negative", str),
    # primitives with value field
    "prompt":     ("value", str),
    "width":      ("value", int),
    "height":     ("value", int),
    "fps":        ("value", int),
    "frame rate": ("value", int),
    "duration":   ("value", int),
    "steps":      ("value", int),
    "cfg":        ("value", float),
    "seed":       ("value", int),
    "strength":   ("value", float),
    # checkpoint / lora
    "load checkpoint": ("ckpt_name", str),
    "checkpoint":      ("ckpt_name", str),
    "load lora":       ("lora_name", str),
    # clip text encode
    "clip text encode (prompt)": ("text", str),
}

def _wf_list():
    """Return list of available workflow JSON filenames in WORKFLOW_DIR."""
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    try:
        return sorted(
            f for f in os.listdir(WORKFLOW_DIR)
            if f.lower().endswith(".json")
        )
    except Exception as e:
        return []

def _wf_load(name):
    """Load a workflow JSON by filename. Returns dict or raises."""
    if not name.lower().endswith(".json"):
        name += ".json"
    # Try exact match first
    path = os.path.join(WORKFLOW_DIR, os.path.basename(name))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # Fuzzy match - normalize spaces, underscores, hyphens and compare
    def _norm(s):
        return s.lower().replace(" ", "").replace("_", "").replace("-", "").replace(".json","")
    name_norm = _norm(name)
    available = [f for f in os.listdir(WORKFLOW_DIR) if f.lower().endswith(".json")]
    # Exact normalized match
    for fname in available:
        if _norm(fname) == name_norm:
            path = os.path.join(WORKFLOW_DIR, fname)
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    # Partial match - find workflows that contain all words from the query
    name_words = set(name_norm.replace(".", "").split())
    best = None
    best_score = 0
    for fname in available:
        fname_norm = _norm(fname)
        score = sum(1 for w in name_words if w in fname_norm)
        if score > best_score:
            best_score = score
            best = fname
    # Build helpful error with available workflows
    available_names = [f[:-5] for f in available]  # strip .json
    hint = f" Did you mean '{best[:-5]}'?" if best and best_score > 0 else ""
    raise FileNotFoundError(
        f"Workflow not found: {name}.{hint} "
        f"Available: {available_names}"
    )

# Class-type → {override_key: field_name}
_CLASS_MAP = {
    # Prompt nodes
    "easy positive":             {"positive": "positive"},
    "easy negative":             {"negative": "negative"},
    "PrimitiveStringMultiline":  {"positive": "value", "prompt": "value", "negative": "value"},
    # CLIPTextEncode intentionally excluded - matched via easy positive/negative nodes above it

    # Sampler nodes
    "KSampler": {
        "steps": "steps", "cfg": "cfg", "seed": "seed",
        "sampler": "sampler_name", "scheduler": "scheduler",
    },
    "KSamplerAdvanced": {
        "steps": "steps", "cfg": "cfg", "seed": "noise_seed",
        "sampler": "sampler_name", "scheduler": "scheduler",
    },
    "KSamplerSelect":   {"sampler": "sampler_name"},
    "RandomNoise":      {"seed": "noise_seed"},
    "CFGGuider":        {"cfg": "cfg"},

    # Latent/image size nodes
    "EmptyLatentImage": {
        "width": "width", "height": "height", "batch": "batch_size",
    },
    "EmptyLTXVLatentVideo": {"batch": "batch_size"},

    # Image scaling nodes - width/height intentionally excluded so hardware defaults
    # don't override upscale output size. Scale factor logic handles sizing separately.
    "ImageScale": {
        "upscale_method": "upscale_method", "crop": "crop",
    },
    "ImageScaleBy":     {"scale": "scale_by"},
    "ResizeAndPadImage": {
        "width": "target_width", "height": "target_height",
    },
    "ResizeImagesByLongerEdge": {"longer_edge": "longer_edge"},

    # Checkpoint / model loaders
    "CheckpointLoaderSimple":    {"checkpoint": "ckpt_name"},
    "ImageOnlyCheckpointLoader": {"checkpoint": "ckpt_name"},
    "LTXAVTextEncoderLoader":    {"checkpoint": "ckpt_name"},
    "LTXVAudioVAELoader":        {"checkpoint": "ckpt_name"},

    # LoRA loaders
    "LoraLoader":          {"lora": "lora_name", "lora_strength": "strength_model"},
    "LoraLoaderModelOnly": {"lora": "lora_name", "lora_strength": "strength_model"},

    # Image input nodes
    "LoadImage":          {"image": "image"},
    "LoadImageMask":      {"image": "image"},
    "LoadImageFromPath":  {"image": "image_path"},

    # Dataset / training nodes
    "LoadImageTextDataSetFromFolder": {"dataset": "folder"},
    "TrainLoraNode": {
        "steps": "steps", "learning_rate": "learning_rate",
        "rank": "rank", "seed": "seed", "batch": "batch_size",
        "existing_lora": "existing_lora",
    },
    "SaveLoRA": {"lora_prefix": "prefix"},

    # Video primitive nodes - matched by title in _wf_describe, but class_map for direct override
    "PrimitiveInt": {},  # handled via title matching in _wf_describe
    "PrimitiveBoolean": {},

    # LTX video conditioning
    "LTXVConditioning": {},
    "LTXVImgToVideoInplace": {"strength": "strength"},
    "LTXVPreprocess": {"img_compression": "img_compression"},

}

def _wf_patch(wf, overrides):
    """
    Apply overrides to a workflow. Tries class-type matching first, then title matching.
    overrides: dict of {param_name: value}
    Returns list of (node_id, field, value) changes made.
    """
    changes = []
    for node_id, node in wf.items():
        title      = (node.get("_meta", {}).get("title") or "").lower().strip()
        class_type = node.get("class_type", "")
        inputs     = node.get("inputs", {})

        for param, value in overrides.items():
            key = param.lower().strip()
            patched = False

            # 1. Class-type matching (most reliable)
            class_fields = _CLASS_MAP.get(class_type, {})
            if key in class_fields:
                field = class_fields[key]
                if field in inputs or class_type in ("easy positive", "easy negative",
                                                      "PrimitiveStringMultiline", "LoadImage"):
                    try:
                        if field in ("steps", "noise_seed", "seed", "width", "height", "batch_size"):
                            inputs[field] = int(value)
                        elif field in ("cfg", "strength_model", "value") and isinstance(value, (int, float)):
                            inputs[field] = float(value)
                        else:
                            inputs[field] = value
                        changes.append((node_id, field, value))
                        patched = True
                    except Exception:
                        pass

            if patched:
                continue

            # 2. Title-based matching (fallback)
            if key in _TITLE_MAP:
                field, cast = _TITLE_MAP[key]
                if key in title or title == key:
                    try:
                        inputs[field] = cast(value)
                        changes.append((node_id, field, value))
                    except Exception:
                        pass

    return changes

def _wf_describe(wf):
    """Return a compact description of a workflow's patchable nodes."""
    lines = []
    for node_id, node in wf.items():
        title      = (node.get("_meta", {}).get("title") or node.get("class_type", "")).strip()
        class_type = node.get("class_type", "")
        inputs     = node.get("inputs", {})

        def scalar(k): return inputs.get(k) if not isinstance(inputs.get(k), list) else None

        if class_type == "easy positive":
            lines.append(f"  [{node_id}] {title} → positive = {repr(scalar('positive') or '')}")
        elif class_type == "easy negative":
            lines.append(f"  [{node_id}] {title} → negative = {repr(scalar('negative') or '')}")
        elif class_type == "PrimitiveStringMultiline":
            lines.append(f"  [{node_id}] {title} → positive/prompt = {repr(scalar('value') or '')}")
        elif class_type == "CLIPTextEncode":
            pass  # wired via easy positive/negative - skip
        elif class_type == "LoadImage":
            lines.append(f"  [{node_id}] {title} → image = {repr(scalar('image') or '')} (auto-injected)")
        elif class_type in ("LoadImageMask", "LoadImageFromPath"):
            lines.append(f"  [{node_id}] {title} → image (auto-injected)")
        elif class_type == "CheckpointLoaderSimple":
            lines.append(f"  [{node_id}] {title} → checkpoint = {repr(scalar('ckpt_name') or '')}")
        elif class_type in ("LoraLoader", "LoraLoaderModelOnly"):
            lines.append(f"  [{node_id}] {title} → lora = {repr(scalar('lora_name') or '')}, strength = {scalar('strength_model')}")
        elif class_type in ("KSampler", "KSamplerAdvanced"):
            sf = "noise_seed" if class_type == "KSamplerAdvanced" else "seed"
            lines.append(f"  [{node_id}] {title} → steps={scalar('steps')}, cfg={scalar('cfg')}, seed={scalar(sf)}, sampler={scalar('sampler_name')}")
        elif class_type == "EmptyLatentImage":
            lines.append(f"  [{node_id}] {title} → width={scalar('width')}, height={scalar('height')}, batch={scalar('batch_size')}")
        elif class_type == "ImageScale":
            lines.append(f"  [{node_id}] {title} → width={scalar('width')}, height={scalar('height')} (use scale=N for Nx upscale)")
        elif class_type == "ResizeAndPadImage":
            lines.append(f"  [{node_id}] {title} → width={scalar('target_width')}, height={scalar('target_height')}")
        elif class_type == "ResizeImagesByLongerEdge":
            lines.append(f"  [{node_id}] {title} → longer_edge={scalar('longer_edge')}")
        elif class_type == "PrimitiveInt":
            t = title.lower()
            for key in ("width","height","fps","frame rate","duration","steps","cfg","seed"):
                if key in t:
                    lines.append(f"  [{node_id}] {title} → {key} = {scalar('value')}")
                    break
        elif class_type == "LTXVImgToVideoInplace":
            lines.append(f"  [{node_id}] {title} → strength = {scalar('strength')}")
        elif class_type == "TrainLoraNode":
            lines.append(f"  [{node_id}] {title} → steps={scalar('steps')}, lr={scalar('learning_rate')}, rank={scalar('rank')}, existing_lora={repr(scalar('existing_lora') or '')}")
        elif class_type == "LoadImageTextDataSetFromFolder":
            lines.append(f"  [{node_id}] {title} → dataset = {repr(scalar('folder') or '')}")
        elif class_type == "easy imageRemBg":
            lines.append(f"  [{node_id}] {title} → rem_mode={scalar('rem_mode')}, add_background={scalar('add_background')}")

    return "\n".join(lines) if lines else "  (no patchable fields detected)"

def _wf_capability(wf, fname):
    """
    Infer what a workflow does from its node class types and filename.
    Returns a short capability string like 'image generation', 'video generation', etc.
    """
    class_types = set()
    for node in wf.values():
        ct = node.get("class_type", "")
        if ct:
            class_types.add(ct.lower())

    fname_low = fname.lower()

    # Check for well-known indicators
    is_video    = any(k in class_types for k in {"ltxvconditioning", "ltxvimgtovideo", "ltxvimgtovideoinplace", "createvideo", "videocombine", "vhs_videocombine"}) or "video" in fname_low
    is_lora     = any(k in class_types for k in {"trainloranode", "lora"}) or "lora" in fname_low
    is_upscale  = any(k in class_types for k in {"esrgan", "ultimatesdupscale", "imageupscalewithmodel", "upscalemodelloader"}) or "upscal" in fname_low
    is_3d       = any(k in class_types for k in {"saveglb", "voxeltomesh", "hunyuan3dv2conditioning", "vaedecodehunyuan3d", "emptylatenthunyuan3dv2", "saveobj", "saveply"}) or "3d" in fname_low
    is_bgremove = any(k in class_types for k in {"easy imageremBg", "easy imageremBg".lower(), "imageremovebg"}) or "bg" in fname_low or "remov" in fname_low
    is_img2img  = any(k in class_types for k in {"vaencode", "setlatentnoisemask"}) or "img2img" in fname_low or "inpaint" in fname_low
    is_image    = any(k in class_types for k in {"ksampler", "ksampleradvanced", "samplercustomadvanced", "checkpointloadersimple"})

    if is_lora:     return "lora training"
    if is_video:    return "video generation"
    if is_3d:       return "3d generation"
    if is_upscale:  return "image upscaling"
    if is_bgremove: return "background removal"
    if is_img2img:  return "image-to-image"
    if is_image:    return "image generation"
    return "unknown"

def wf_list_tool():
    files = _wf_list()
    if not files:
        return {"error": f"No JSON files found in {WORKFLOW_DIR}"}
    workflows = {}
    for fname in files:
        try:
            wf = _wf_load(fname)
            capability = _wf_capability(wf, fname)
            patchable  = _wf_describe(wf)
            workflows[fname] = {
                "capability": capability,
                "patchable":  patchable,
            }
        except Exception as e:
            workflows[fname] = {"capability": "error", "patchable": f"[error reading: {e}]"}
    return {"workflow_dir": WORKFLOW_DIR, "workflows": workflows}

def wf_run_tool(name, overrides, target, img_bytes=None):
    """
    Load, patch, and queue a workflow from WORKFLOW_DIR.
    target: 'image' (port 8188)
    overrides: dict of parameter overrides to patch into the workflow
    img_bytes: optional explicit input image (the operator's attachment for THIS call).
        When given it is staged to the ComfyUI input folder and injected directly,
        outranking last_image_path — which can be stale or cleared by intervening state.
    """
    # Pick ComfyUI host - check live if state flag hasn't been set yet
    if state.get("comfy_hold"):
        _what = (state.get("installing") or {}).get("what") or state["comfy_hold"]
        return {"group": "activity", "label": "workflow",
                "content": f"ComfyUI is stopped for a moment while installing {_what} — run it again when the bar finishes.",
                "error": (f"ComfyUI is stopped while System 32 installs {_what} for a workflow (the bar "
                          f"below the chat shows it). Nothing was run. Tell the operator to wait for the "
                          f"'✓ Installed' note and then ask again — don't retry on your own.")}
    img_up = state["comfy_image_ready"] or is_up(CONFIG["comfy_image_host"])
    log(f"[S32][WF] target={target} | image_ready={state['comfy_image_ready']} img_up={img_up}")

    # All workflows run on image instance (port 8188)
    if not img_up:
        return {"error": "ComfyUI not ready."}
    host = CONFIG["comfy_image_host"]
    state["comfy_image_ready"] = True

    try:
        wf = _wf_load(name)
    except FileNotFoundError:
        return {"error": f"Workflow not found: {name}"}
    except Exception as e:
        return {"error": f"Failed to load workflow: {e}"}

    wf = copy.deepcopy(wf)

    # A specific earlier image it picked (resolved to a real file by
    # s32_dispatch.resolve_image_ref). Private key — never patched into a node.
    overrides = dict(overrides or {})
    _picked = overrides.pop("_input_image", None)

    # Fill in hardware-appropriate defaults for any key not explicitly provided
    hw_defaults = hardware_workflow_defaults(get_hardware())
    merged_overrides = dict(hw_defaults)
    merged_overrides.update(overrides or {})  # explicit overrides always win

    changes = _wf_patch(wf, merged_overrides)

    # Auto-inject the last known image into any unpatched image loader nodes
    _IMAGE_LOADER_CLASSES = {
        "LoadImage", "LoadImageMask", "LoadImageFromPath",
        "ImageLoader", "ETN_LoadImageBase64",
    }
    _VIDEO_LOADER_CLASSES = {
        "LoadVideo", "LoadVideoPath", "VHS_LoadVideo", "LoadVideoFFmpeg",
    }
    if "image" not in (overrides or {}):
        img_src = None
        # An explicitly-passed image (the operator's attachment for THIS call) wins over
        # last_image_path. This is what makes "attach an image → remove background / upscale /
        # image-to-video / image-to-3D" reliable: the attachment reaches the loader directly
        # instead of depending on last_image_path, which was coming up None at inject time.
        if img_bytes:
            try:
                _in = CONFIG.get("comfy_image_input", "")
                if _in and os.path.isdir(_in):
                    _ts = time.strftime("%Y%m%d_%H%M%S")
                    _p  = os.path.join(_in, f"s32_wfinput_{_ts}.png")
                    if PILImage:
                        PILImage.open(io.BytesIO(img_bytes)).convert("RGB").save(_p, "PNG")
                    else:
                        with open(_p, "wb") as _f:
                            _f.write(img_bytes)
                    img_src = _p
                    state["last_image_path"] = _p   # refresh so follow-ups see it too
                    log(f"[S32][WF] Using attached image for this workflow: {_p}")
            except Exception as e:
                log(f"[S32][WF] Failed to stage attached image: {e}")
        if not img_src and _picked and os.path.isfile(_picked):
            img_src = _picked
            state["last_image_path"] = _picked     # follow-up steps continue from what it picked
            log(f"[S32][WF] Image inject source (picked): {img_src}")
        if not img_src and state.get("last_image_path") and os.path.isfile(state["last_image_path"]):
            img_src = state["last_image_path"]
            log(f"[S32][WF] Image inject source: {img_src}")
        elif not img_src:
            log(f"[S32][WF] Image inject skipped: last_image_path={state.get('last_image_path')}")

        # Also check for video inject into video loader nodes
        vid_src = None
        if state.get("last_video_path") and os.path.isfile(state["last_video_path"]):
            vid_src = state["last_video_path"]
        # Check if this workflow has image loader nodes that need an image
        has_image_loader = any(
            node.get("class_type") in _IMAGE_LOADER_CLASSES
            for node in wf.values()
        )
        # GUARD: an image-input workflow (img2video, bg remover, upscaler, img2img…)
        # with NO available input image must not be submitted — the stale placeholder
        # filename baked into the JSON (e.g. maxresdefault.jpg) fails ComfyUI validation
        # with an opaque HTTP 400. Return a clear, recoverable error instead so it can
        # generate an image first or ask the operator for one.
        if has_image_loader and not img_src:
            return {"error": ("This workflow needs an input image, but there is no current "
                              "image (nothing generated or attached yet). Generate an image "
                              "first (e.g. with 'simple image generator.json'), or ask the "
                              "operator to attach one — then run this workflow again.")}
        if img_src and has_image_loader:
            input_dir = CONFIG.get("comfy_image_input", "")
            log(f"[S32][WF] Input dir: {input_dir} exists={os.path.exists(input_dir) if input_dir else False}")
            if input_dir and os.path.exists(input_dir):
                fname = "s32_input_" + os.path.basename(img_src)
                dest  = os.path.join(input_dir, fname)
                try:
                    shutil.copy2(img_src, dest)
                    # Read source dimensions for scale-factor support
                    src_w, src_h = None, None
                    if PILImage:
                        try:
                            with PILImage.open(img_src) as _im:
                                src_w, src_h = _im.size
                        except Exception:
                            pass
                    for node in wf.values():
                        if node.get("class_type") in _IMAGE_LOADER_CLASSES:
                            inp = node.get("inputs", {})
                            log(f"[S32][WF] Found image loader '{node.get('class_type')}' inputs: {list(inp.keys())}")
                            for field in ("image", "image_path", "path"):
                                if field in inp and not isinstance(inp[field], list):
                                    log(f"[S32][WF] Patching {field}: {inp[field]} -> {fname}")
                                    inp[field] = fname
                                    break
                    # Apply scale factor to ImageScale/EmptyLatentImage nodes if scale override given
                    scale = float((overrides or {}).get("scale", 0))
                    if scale > 0 and src_w and src_h:
                        new_w = int(src_w * scale)
                        new_h = int(src_h * scale)
                        for node in wf.values():
                            ct = node.get("class_type", "")
                            inp = node.get("inputs", {})
                            if ct in ("ImageScale", "ImageScaleBy", "EmptyLatentImage",
                                      "LatentUpscale", "ImageUpscale"):
                                if "width" in inp and not isinstance(inp["width"], list):
                                    inp["width"]  = new_w
                                    inp["height"] = new_h
                                    log(f"[S32][WF] Scale {scale}x: {src_w}x{src_h} -> {new_w}x{new_h}")
                    # A size the operator ASKED for ("upscale this to 512x512") — ImageScale is left
                    # out of the class map so hardware defaults can't shrink an upscale, which also
                    # dropped an explicit size (2026-10-30: asked 512, got the baked-in 2048). Only in
                    # a plain image-in → image-out workflow (no sampler / latent nodes), so image
                    # generators and video workflows are untouched.
                    _ow, _oh = (overrides or {}).get("width"), (overrides or {}).get("height")
                    _plain = not any(("sampler" in str(n.get("class_type", "")).lower()
                                      or "latent" in str(n.get("class_type", "")).lower())
                                     for n in wf.values())
                    if not scale > 0 and (_ow or _oh) and _plain:
                        try:
                            _w = int(_ow) if _ow else (round(src_w * int(_oh) / src_h) if src_w and src_h else None)
                            _h = int(_oh) if _oh else (round(src_h * int(_ow) / src_w) if src_w and src_h else None)
                        except (TypeError, ValueError, ZeroDivisionError):
                            _w = _h = None
                        if _w and _h:
                            for node in wf.values():
                                inp = node.get("inputs", {})
                                if node.get("class_type") == "ImageScale" and "width" in inp \
                                        and not isinstance(inp["width"], list):
                                    inp["width"], inp["height"] = _w, _h
                                    log(f"[S32][WF] Size asked for: {_w}x{_h}")
                                    changes.append(("ImageScale", "size", f"{_w}x{_h}"))
                    changes.append(("ImageLoader", "image", fname))
                    log(f"[S32][WF] Auto-injected image: {fname}")
                except Exception as e:
                    log(f"[S32][WF] Image inject failed: {e}")

        # Auto-inject last video into video loader nodes
        if vid_src:
            has_video_loader = any(
                node.get("class_type") in _VIDEO_LOADER_CLASSES
                for node in wf.values()
            )
            if has_video_loader:
                input_dir = CONFIG.get("comfy_image_input", "")
                if input_dir and os.path.exists(input_dir):
                    vfname = "s32_input_" + os.path.basename(vid_src)
                    vdest  = os.path.join(input_dir, vfname)
                    try:
                        shutil.copy2(vid_src, vdest)
                        for node in wf.values():
                            if node.get("class_type") in _VIDEO_LOADER_CLASSES:
                                inp = node.get("inputs", {})
                                # ComfyUI's own LoadVideo calls it "file" (2026-10-30: 'video to frames'
                                # 400'd on the workflow's stale placeholder because "file" wasn't tried).
                                for field in ("file", "video", "path", "video_path"):
                                    if field in inp and not isinstance(inp[field], list):
                                        inp[field] = vfname
                                        log(f"[S32][WF] Video inject: {field} -> {vfname}")
                                        break
                        changes.append(("VideoLoader", "video", vfname))
                    except Exception as e:
                        log(f"[S32][WF] Video inject failed: {e}")

    # Randomize seeds on every run unless the caller explicitly set one
    if "seed" not in (overrides or {}):
        _SEED_CLASSES = {
            "KSampler",
            "KSamplerAdvanced",
            "RandomNoise",
            "easy kSampler",
            "easy kSamplerTiled",
            "easy fullkSampler",
            "easy kSamplerSDTurbo",
            "easy kSamplerInpainting",
            "easy kSamplerDownscaleUnet",
            "easy preSamplingDynamicCFG",
            "easy preSampling",
            "easy preSamplingAdvanced",
        }
        seeds_set = []
        for node_id, node in wf.items():
            ct  = node.get("class_type", "")
            inp = node.get("inputs", {})
            if ct in _SEED_CLASSES:
                for sf in ("seed", "noise_seed"):
                    if sf in inp:
                        new_seed = random.randint(0, 2**32 - 1)
                        inp[sf] = new_seed
                        seeds_set.append(f"{node_id}:{ct}.{sf}={new_seed}")
            elif "seed" in ct.lower() or "sampler" in ct.lower() or "noise" in ct.lower():
                for sf in ("seed", "noise_seed"):
                    if sf in inp:
                        new_seed = random.randint(0, 2**32 - 1)
                        inp[sf] = new_seed
                        seeds_set.append(f"{node_id}:{ct}.{sf}={new_seed}")
        if seeds_set:
            log(f"[S32][SEED] Randomized: {seeds_set}")
        else:
            log(f"[S32][SEED] WARNING - no seed nodes found in workflow '{name}'. Node types: {list(set(n.get('class_type','') for n in wf.values()))}")

    log(f"[S32][WF] Running '{name}' on {host} with patches: {changes}")

    # Pre-flight: ask ComfyUI what this (drag-and-drop) workflow needs that isn't
    # installed — models AND custom nodes — and start fetching it (s32_models). Runs
    # BEFORE the auto-swaps below so a missing file gets the real thing, not a stand-in.
    # If ComfyUI can't be asked, preflight returns None and nothing changes.
    try:
        import s32_models
        _pf = s32_models.preflight(host, wf)
    except Exception as e:
        log(f"[S32][WF] preflight skipped: {e}")
        _pf = None
    if _pf and _pf.get("block"):
        # "error" is what IT is told; "content"+"group" make the operator's chat show
        # it as the first line of the same activity card the download/restart lines
        # fold into (one card for the whole story).
        return {"error": _pf["message"],
                "missing_models": [os.path.basename(m["file"]) for m in _pf.get("missing", [])],
                "missing_nodes": _pf.get("nodes", []),
                "group": "activity", "label": "workflow",
                "content": f"{os.path.splitext(name)[0]} can't run yet: "
                           f"{_pf.get('operator') or 'files are missing'}. Run it again when this finishes."}

    # Auto-fix checkpoint names - if the workflow has a checkpoint that ComfyUI
    # can't find, swap it with the best available one before queuing
    try:
        available_ckpts = fetch_checkpoints()
        if available_ckpts:
            _CKPT_CLASSES = {"CheckpointLoaderSimple", "ImageOnlyCheckpointLoader",
                             "LTXAVTextEncoderLoader", "LTXVAudioVAELoader"}
            for node_id, node in wf.items():
                if node.get("class_type") in _CKPT_CLASSES:
                    current = node.get("inputs", {}).get("ckpt_name", "")
                    if not current or current not in available_ckpts:
                        # A SPECIALIZED model (3D, video…) can't be substituted — swapping
                        # hunyuan_3d for an image checkpoint "ran" and silently produced
                        # nothing. Only a same-stem match counts; otherwise say what's missing.
                        _special = (node.get("class_type") in ("ImageOnlyCheckpointLoader",
                                    "LTXAVTextEncoderLoader", "LTXVAudioVAELoader")
                                    or __import__("re").search(r"3d|hunyuan|tripo|svd|video|ltx|\bwan|cosmos|mochi",
                                                 current or "", __import__("re").I))
                        if current and _special:
                            _stem = os.path.splitext(os.path.basename(current))[0].lower()
                            _same = [c for c in available_ckpts if _stem in c.lower()]
                            if not _same:
                                log(f"[S32][WF] Required checkpoint missing: '{current}' — not substituting.")
                                return {"error": (f"This workflow needs the model '{current}', which isn't "
                                                  f"installed in ComfyUI's checkpoints folder (installed: "
                                                  f"{len(available_ckpts)}). Another model can't stand in for "
                                                  f"it. Tell the operator it's missing so they can restore it.")}
                        replacement = _default_checkpoint(current if current else None)
                        if replacement:
                            node["inputs"]["ckpt_name"] = replacement
                            log(f"[S32][WF] Auto-set checkpoint: '{current or 'empty'}' → '{replacement}'")
    except Exception as e:
        log(f"[S32][WF] Checkpoint auto-swap failed: {e}")

    # Clear empty or unavailable LoRA names so ComfyUI doesn't reject the workflow
    try:
        available_loras = fetch_lora_options()
        _LORA_CLASSES = {"LoraLoader", "LoraLoaderModelOnly"}
        nodes_to_bypass = []
        for node_id, node in wf.items():
            if node.get("class_type") in _LORA_CLASSES:
                current = node.get("inputs", {}).get("lora_name", "")
                if not current or current not in available_loras:
                    if available_loras:
                        # Use first available LoRA
                        node["inputs"]["lora_name"] = available_loras[0]
                        log(f"[S32][WF] LoRA auto-set: '{current or 'empty'}' → '{available_loras[0]}'")
                    else:
                        # No LoRAs at all - bypass this node by rewiring dependents
                        # Find what this node outputs and what depends on it
                        model_input = node.get("inputs", {}).get("model")
                        nodes_to_bypass.append((node_id, model_input))
                        log(f"[S32][WF] No LoRAs available - bypassing node {node_id}")

        # Rewire nodes that depended on bypassed LoRA nodes
        for bypass_id, model_src in nodes_to_bypass:
            if not model_src:
                continue
            for other_id, other_node in wf.items():
                for input_key, input_val in other_node.get("inputs", {}).items():
                    if isinstance(input_val, list) and len(input_val) == 2:
                        if str(input_val[0]) == str(bypass_id):
                            other_node["inputs"][input_key] = model_src
                            log(f"[S32][WF] Rewired {other_id}.{input_key} from bypassed LoRA to {model_src}")
    except Exception as e:
        log(f"[S32][WF] LoRA auto-clear failed: {e}")

    # Force image_output to "Save" on any sampler that has it set to "Hide"
    # "Hide" sends to PreviewImage/websocket only — we need actual saved files
    _SAMPLER_CLASSES = {"easy fullkSampler", "easy kSampler", "KSampler",
                        "KSamplerAdvanced", "easy fullkSamplerIteration"}
    for node_id, node in wf.items():
        if node.get("class_type") in _SAMPLER_CLASSES:
            inp = node.get("inputs", {})
            if inp.get("image_output") in ("Hide", "Preview"):
                inp["image_output"] = "Save"
                log(f"[S32][WF] Set image_output=Save on node {node_id} ({node.get('class_type')})")

    # Clear stale missing node state before this attempt
    state["missing_nodes"] = {}
    try:
        prompt_id, _ = comfy_queue(host, wf)
    except urllib.error.HTTPError as e:
        body = getattr(e, "s32_body", "") or ""
        # Try to parse specific node error from ComfyUI response
        node_list = []
        try:
            err = json.loads(body)
            node_list = _scan_workflow_error(err)
        except Exception:
            pass
        if not node_list:
            # Fall back to state if parsing failed
            missing = state.get("missing_nodes", {})
            for names in missing.values():
                node_list.extend(names)
        if node_list:
            label = "image (8188)"
            state["missing_nodes"][label] = node_list
            return {
                "error": (
                    f"Workflow failed: missing node(s) — {', '.join(node_list)}. "
                    f"Call install_missing_nodes to fix this automatically."
                ),
                "missing_nodes": node_list,
                "auto_install": True,
            }
        return {"error": f"Failed to queue workflow: {e}"}
    except Exception as e:
        return {"error": f"Failed to queue workflow: {e}"}

    state["job_status"] = f"Running {name}..."
    result = comfy_wait(host, prompt_id)
    state["job_status"] = None
    if result.get("_cancelled"):
        return {"error": CANCELLED_MSG, "cancelled": True}

    # Nodes that exist only for ComfyUI UI preview - skip on first pass, use as fallback
    _PREVIEW_ONLY_CLASSES = {"PreviewImage", "SaveImageWebsocket", "PreviewVideo"}

    _3D_EXTS    = {".glb", ".obj", ".ply", ".stl", ".fbx", ".gltf", ".usdz"}
    _VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".gif"}
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

    def _classify(filename):
        ext = os.path.splitext(filename)[1].lower()
        if ext in _3D_EXTS:    return "3d"
        if ext in _VIDEO_EXTS: return "video"
        if ext in _IMAGE_EXTS: return "image"
        return "unknown"

    def _collect_outputs(result, wf, skip_preview):
        collected = []
        seen = set()
        for node_id, node_out in result.get("outputs", {}).items():
            wf_node = wf.get(node_id, {})
            if skip_preview and wf_node.get("class_type") in _PREVIEW_ONLY_CLASSES:
                continue
            # Collect every list value in the output dict - images, videos, meshes, files, gltf, etc.
            for key, items in node_out.items():
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, dict) or "filename" not in item:
                        continue
                    fname = item["filename"]
                    if fname in seen:
                        continue
                    seen.add(fname)
                    kind = _classify(fname)
                    if kind != "unknown":
                        collected.append((kind, item))
        return collected

    raw_outputs = _collect_outputs(result, wf, skip_preview=True)
    if not raw_outputs:
        raw_outputs = _collect_outputs(result, wf, skip_preview=False)

    # A video generation emits the final video PLUS its frames / a preview image.
    # The video is the only wanted output — drop the image frames so they don't land
    # in 'generated images' (which must hold standalone images only). Video + 3D kept.
    if any(k == "video" for k, _ in raw_outputs):
        raw_outputs = [(k, it) for (k, it) in raw_outputs if k != "image"]

    outputs = []
    ts = time.strftime("%Y%m%d_%H%M%S")
    img_idx = 0
    for kind, item in raw_outputs:
        if kind == "image":
            try:
                raw = comfy_image_bytes(host, item["filename"],
                                        item.get("subfolder",""), item.get("type","output"))
                out_dir = CONFIG["out_images"]
                os.makedirs(out_dir, exist_ok=True)
                suffix = f"_{img_idx}" if img_idx > 0 else ""
                save_path = os.path.join(out_dir, f"image_{ts}{suffix}.png")
                img_idx += 1
                with open(save_path, "wb") as f:
                    f.write(raw)
                log(f"[S32][WF] Saved image to {save_path}")
                state["last_image_bytes"] = raw
                state["last_image_path"]  = save_path
                state["session_images"].append({
                    "bytes":  raw,
                    "path":   save_path,
                    "prompt": (overrides or {}).get("positive", ""),
                })
                _cap_session_images()
                outputs.append({"type": "image",
                                 "url": f"/img?path={urllib.parse.quote(save_path)}",
                                 "filename": item["filename"],
                                 "saved": save_path,
                                 "source": "ai",
                                 # genid must be UNIQUE per saved image (the frontend dedups
                                 # on it). ComfyUI's own filename repeats across a batch —
                                 # its output dir is cleared between runs by staging cleanup,
                                 # so its counter resets to _00001_ every time — which made
                                 # images 2..N in a batch get skipped in chat. The saved path
                                 # is uniquely timestamped, so key off that.
                                 "genid": os.path.basename(save_path)})
            except Exception as e:
                outputs.append({"type": "image_error", "filename": item["filename"], "error": str(e)})
        elif kind == "video":
            try:
                raw = comfy_image_bytes(host, item["filename"],
                                        item.get("subfolder",""), item.get("type","output"))
                out_dir = CONFIG["out_videos"]
                os.makedirs(out_dir, exist_ok=True)
                ext = os.path.splitext(item["filename"])[1] or ".mp4"
                save_path = os.path.join(out_dir, f"video_{ts}{ext}")
                with open(save_path, "wb") as f:
                    f.write(raw)
                log(f"[S32][WF] Saved video to {save_path}")
                outputs.append({"type": "video",
                                 "url": f"/img?path={urllib.parse.quote(save_path)}",
                                 "ext": ext.lstrip("."),
                                 "filename": item["filename"],
                                 "saved": save_path})
            except Exception as e:
                outputs.append({"type": "error", "filename": item["filename"], "error": str(e)})
        elif kind == "3d":
            try:
                raw = comfy_image_bytes(host, item["filename"],
                                        item.get("subfolder",""), item.get("type","output"))
                out_dir = CONFIG.get("out_3d", os.path.join(_script_dir, "Output", "generated 3d"))
                os.makedirs(out_dir, exist_ok=True)
                ext = os.path.splitext(item["filename"])[1] or ".glb"
                save_path = os.path.join(out_dir, f"model_{ts}{ext}")
                with open(save_path, "wb") as f:
                    f.write(raw)
                log(f"[S32][WF] Saved 3D model to {save_path}")
                outputs.append({"type": "model_3d",
                                 "filename": item["filename"],
                                 "saved": save_path,
                                 "message": f"3D model saved: {os.path.basename(save_path)}"})
            except Exception as e:
                outputs.append({"type": "error", "filename": item["filename"], "error": str(e)})
    # Now that organized copies are saved to the correct 'System 32's Output' folders,
    # delete this generation's raw files from ComfyUI's staging output dir so staging
    # doesn't accumulate (frames, the video original, previews, etc.). HTTP-fetched bytes
    # are already written, so removing the staged files is safe. Only 'output'-type files.
    try:
        _stage = os.path.join(CONFIG.get("comfy_image_dir", "") or "", "output")
        if _stage and os.path.isdir(_stage):
            for _n, _no in (result.get("outputs", {}) or {}).items():
                if not isinstance(_no, dict):
                    continue
                for _k, _items in _no.items():
                    if not isinstance(_items, list):
                        continue
                    for _it in _items:
                        if not isinstance(_it, dict) or "filename" not in _it:
                            continue
                        if _it.get("type", "output") != "output":
                            continue
                        _p = os.path.join(_stage, _it.get("subfolder", "") or "", _it["filename"])
                        try:
                            if os.path.isfile(_p):
                                os.remove(_p)
                        except Exception:
                            pass
    except Exception:
        pass
    if not outputs:
        # ComfyUI records WHY a run failed in the history entry's status messages
        # (execution_error: failing node + exception). Surface it — "produced no
        # output" hid a missing-model crash on 2026-10-06.
        _why = ""
        try:
            _st = (result or {}).get("status") or {}
            for _msg in _st.get("messages") or []:
                if isinstance(_msg, (list, tuple)) and len(_msg) > 1 and _msg[0] == "execution_error":
                    _d = _msg[1] or {}
                    _why = (f"{_d.get('node_type', 'a node')} failed: "
                            f"{str(_d.get('exception_message', '')).strip()[:300]}")
                    break
            if not _why and _st.get("status_str") == "error":
                _why = "ComfyUI reported an error but gave no details (check the ComfyUI log)."
        except Exception:
            pass
        if _why:
            log(f"[S32][WF] ComfyUI execution error: {_why}")
            # A loader that can't find a file ComfyUI itself listed = its model list is
            # stale (file removed while System 32 was off). Hand it to the auto-download
            # path instead of just reporting the crash.
            _mm = __import__("re").search(
                r"Model in folder '([^']+)' with filename '([^']+)' not found", _why)
            if _mm:
                try:
                    import s32_models
                    _pf = s32_models.after_run_missing(host, _d.get("node_type", ""),
                                                       _mm.group(1), _mm.group(2))
                    if _pf and _pf.get("message"):
                        return {"group": "activity", "label": "workflow",
                                "content": f"{os.path.splitext(name)[0]} stopped — ComfyUI's model list "
                                           f"was out of date; {_pf.get('operator') or _mm.group(2) + ' is missing'}. "
                                           f"Run it again when this finishes.",
                                "error": "ComfyUI's model list was out of date — it listed "
                                         f"{_mm.group(2)} but the file isn't on disk. "
                                         + _pf["message"].replace("The workflow was NOT run.",
                                                                  "Nothing was saved.")}
                except Exception as e:
                    log(f"[S32][WF] missing-model handoff failed: {e}")
            # A node that needs a Python package it doesn't have (it imports it only when it
            # runs) → install it into ComfyUI's Python and restart ComfyUI (2026-11-05).
            _mod = __import__("re").search(r"No module named '([^']+)'", _why)
            if _mod:
                try:
                    from s32_hardware import install_python_module
                    _ip = install_python_module(_mod.group(1))
                except Exception as e:
                    _ip = {"error": str(e)[:150]}
                if _ip.get("started"):
                    _what = (f"it needs the Python package {_ip['started']} — installing it now, then "
                             f"ComfyUI restarts (the bar below shows it)")
                    return {"group": "activity", "label": "workflow",
                            "content": f"{os.path.splitext(name)[0]} stopped — {_what}. Run it again when this finishes.",
                            "error": (f"The workflow stopped inside ComfyUI: {_why} System 32 is installing "
                                      f"the package {_ip['started']} into ComfyUI and restarting it — nothing "
                                      f"was saved. Tell the operator it's being installed and to ask again "
                                      f"once it says it's done. Don't run it again yourself until then.")}
                return {"error": f"The workflow failed inside ComfyUI — {_why} The package couldn't be "
                                 f"installed automatically ({_ip.get('error')}). Nothing was saved. "
                                 f"Tell the operator plainly what failed."}
            return {"error": f"The workflow failed inside ComfyUI — {_why} Nothing was saved. "
                             f"Tell the operator plainly what failed."}
        return {"error": "Workflow completed but produced no image or video output."}
    if len(outputs) == 1:
        return outputs[0]
    return {"type": "multi", "outputs": outputs}



# ── Fallback workflows (used when no matching JSON exists in WORKFLOW_DIR) ────────
# These are minimal in-memory templates - prefer JSON workflows via run_workflow

IMAGE_WORKFLOW = {
    "13": {"inputs": {"positive": ""}, "class_type": "easy positive"},
    "14": {"inputs": {"negative": ""}, "class_type": "easy negative"},
    "20": {"inputs": {"images": ["31:36", 0]}, "class_type": "PreviewImage"},
    "31:25": {"inputs": {"ckpt_name": ""}, "class_type": "CheckpointLoaderSimple"},
    "31:26": {"inputs": {"text": ["13", 0], "clip": ["31:25", 1]}, "class_type": "CLIPTextEncode"},
    "31:27": {"inputs": {"text": ["14", 0], "clip": ["31:25", 1]}, "class_type": "CLIPTextEncode"},
    "31:35": {"inputs": {"width": 1024, "height": 1024, "batch_size": 1}, "class_type": "EmptyLatentImage"},
    "31:28": {"inputs": {"add_noise": "enable", "noise_seed": 0, "steps": 64, "cfg": 8, "sampler_name": "euler", "scheduler": "simple", "start_at_step": 0, "end_at_step": 10000, "return_with_leftover_noise": "disable", "model": ["31:25", 0], "positive": ["31:26", 0], "negative": ["31:27", 0], "latent_image": ["31:35", 0]}, "class_type": "KSamplerAdvanced"},
    "31:29": {"inputs": {"samples": ["31:28", 0], "vae": ["31:25", 2]}, "class_type": "VAEDecode"},
    "31:36": {"inputs": {"upscale_method": "nearest-exact", "width": 2048, "height": 2048, "crop": "disabled", "image": ["31:29", 0]}, "class_type": "ImageScale"},
}

LORA_WORKFLOW = {
    "10": {"inputs": {"batch_size": 1, "grad_accumulation_steps": 8, "steps": 512, "learning_rate": 0.0005, "rank": 64, "optimizer": "AdamW", "loss_function": "MSE", "seed": 0, "training_dtype": "bf16", "lora_dtype": "bf16", "algorithm": "LoRA", "gradient_checkpointing": True, "existing_lora": "[None]", "bucket_mode": False, "bypass_mode": False, "model": ["13", 0], "latents": ["18", 0], "positive": ["14", 0]}, "class_type": "TrainLoraNode"},
    "11": {"inputs": {"prefix": "loras/MyLoRA", "steps": ["10", 2], "lora": ["10", 0]}, "class_type": "SaveLoRA"},
    "12": {"inputs": {"filename_prefix": "loss_graph", "loss": ["10", 1]}, "class_type": "LossGraphNode"},
    "13": {"inputs": {"ckpt_name": ""}, "class_type": "CheckpointLoaderSimple"},
    "14": {"inputs": {"text": ["15", 1], "clip": ["13", 1]}, "class_type": "CLIPTextEncode"},
    "15": {"inputs": {"folder": ""}, "class_type": "LoadImageTextDataSetFromFolder"},
    "18": {"inputs": {"pixels": ["22", 0], "vae": ["13", 2]}, "class_type": "VAEEncode"},
    "19": {"inputs": {"images": ["22", 0]}, "class_type": "PreviewImage"},
    "22": {"inputs": {"target_width": 1024, "target_height": 1024, "padding_color": "black", "interpolation": "area", "image": ["15", 0]}, "class_type": "ResizeAndPadImage"},
}

VIDEO_WORKFLOW = {
    "75":      {"inputs": {"filename_prefix": "video/S32", "format": "auto", "codec": "auto", "video": ["320:310", 0]}, "class_type": "SaveVideo"},
    "269":     {"inputs": {"image": ""}, "class_type": "LoadImage"},
    "320:276": {"inputs": {"noise_seed": 42}, "class_type": "RandomNoise"},
    "320:277": {"inputs": {"noise_seed": 0}, "class_type": "RandomNoise"},
    "320:278": {"inputs": {"video_latent": ["320:288", 0], "audio_latent": ["320:307", 1]}, "class_type": "LTXVConcatAVLatent"},
    "320:279": {"inputs": {"ckpt_name": r"Video generation checkpoints\LTX 2.3 NSFW Checkpoint.safetensors"}, "class_type": "LTXVAudioVAELoader"},
    "320:280": {"inputs": {"sampler_name": "euler_cfg_pp"}, "class_type": "KSamplerSelect"},
    "320:281": {"inputs": {"sigmas": "0.85, 0.7250, 0.4219, 0.0"}, "class_type": "ManualSigmas"},
    "320:282": {"inputs": {"cfg": 1, "model": ["320:285", 0], "positive": ["320:284", 0], "negative": ["320:284", 1]}, "class_type": "CFGGuider"},
    "320:283": {"inputs": {"noise": ["320:277", 0], "guider": ["320:314", 0], "sampler": ["320:291", 0], "sigmas": ["320:306", 0], "latent_image": ["320:318", 0]}, "class_type": "SamplerCustomAdvanced"},
    "320:284": {"inputs": {"positive": ["320:304", 0], "negative": ["320:304", 1], "latent": ["320:307", 0]}, "class_type": "LTXVCropGuides"},
    "320:285": {"inputs": {"lora_name": "ltx 2.3 loras\\ltx-2.3-22b-distilled-lora-384.safetensors", "strength_model": 0.5, "model": ["320:316", 0]}, "class_type": "LoraLoaderModelOnly"},
    "320:286": {"inputs": {"longer_edge": 1536, "images": ["320:290", 0]}, "class_type": "ResizeImagesByLongerEdge"},
    "320:287": {"inputs": {"samples": ["320:307", 0], "upscale_model": ["320:311", 0], "vae": ["320:316", 2]}, "class_type": "LTXVLatentUpsampler"},
    "320:288": {"inputs": {"strength": 1, "bypass": ["320:302", 0], "vae": ["320:316", 2], "image": ["320:289", 0], "latent": ["320:287", 0]}, "class_type": "LTXVImgToVideoInplace"},
    "320:289": {"inputs": {"img_compression": 18, "image": ["320:286", 0]}, "class_type": "LTXVPreprocess"},
    "320:290": {"inputs": {"resize_type": "scale dimensions", "resize_type.width": 512, "resize_type.height": 512, "resize_type.crop": "center", "scale_method": "lanczos", "input": ["269", 0]}, "class_type": "ResizeImageMaskNode"},
    "320:291": {"inputs": {"sampler_name": "euler_ancestral_cfg_pp"}, "class_type": "KSamplerSelect"},
    "320:295": {"inputs": {"width": 256, "height": 256, "length": 526, "batch_size": 1}, "class_type": "EmptyLTXVLatentVideo"},
    "320:296": {"inputs": {"strength": 0.7, "bypass": ["320:302", 0], "vae": ["320:316", 2], "image": ["320:289", 0], "latent": ["320:295", 0]}, "class_type": "LTXVImgToVideoInplace"},
    "320:297": {"inputs": {"samples": ["320:309", 1], "audio_vae": ["320:279", 0]}, "class_type": "LTXVAudioVAEDecode"},
    "320:299": {"inputs": {"value": 512}, "class_type": "PrimitiveInt"},
    "320:300": {"inputs": {"value": 25}, "class_type": "PrimitiveInt"},
    "320:301": {"inputs": {"value": 21}, "class_type": "PrimitiveInt"},
    "320:302": {"inputs": {"value": False}, "class_type": "PrimitiveBoolean"},
    "320:303": {"inputs": {"text": "", "clip": ["320:317", 0]}, "class_type": "CLIPTextEncode"},
    "320:304": {"inputs": {"frame_rate": 25, "positive": ["320:303", 0], "negative": ["320:313", 0]}, "class_type": "LTXVConditioning"},
    "320:305": {"inputs": {"frames_number": 526, "frame_rate": 25, "batch_size": 1, "audio_vae": ["320:279", 0]}, "class_type": "LTXVEmptyLatentAudio"},
    "320:306": {"inputs": {"sigmas": "1.0, 0.99375, 0.9875, 0.98125, 0.975, 0.909375, 0.725, 0.421875, 0.0"}, "class_type": "ManualSigmas"},
    "320:307": {"inputs": {"av_latent": ["320:283", 0]}, "class_type": "LTXVSeparateAVLatent"},
    "320:308": {"inputs": {"noise": ["320:276", 0], "guider": ["320:282", 0], "sampler": ["320:280", 0], "sigmas": ["320:281", 0], "latent_image": ["320:278", 0]}, "class_type": "SamplerCustomAdvanced"},
    "320:309": {"inputs": {"av_latent": ["320:308", 0]}, "class_type": "LTXVSeparateAVLatent"},
    "320:310": {"inputs": {"fps": 25, "images": ["320:315", 0], "audio": ["320:297", 0]}, "class_type": "CreateVideo"},
    "320:311": {"inputs": {"model_name": ""}, "class_type": "LatentUpscaleModelLoader"},
    "320:312": {"inputs": {"value": 512}, "class_type": "PrimitiveInt"},
    "320:313": {"inputs": {"text": "pc game, console game, video game, cartoon, childish, ugly", "clip": ["320:317", 0]}, "class_type": "CLIPTextEncode"},
    "320:314": {"inputs": {"cfg": 1, "model": ["320:285", 0], "positive": ["320:304", 0], "negative": ["320:304", 1]}, "class_type": "CFGGuider"},
    "320:315": {"inputs": {"tile_size": 768, "overlap": 64, "temporal_size": 4096, "temporal_overlap": 4, "samples": ["320:309", 0], "vae": ["320:316", 2]}, "class_type": "VAEDecodeTiled"},
    "320:316": {"inputs": {"ckpt_name": r"Video generation checkpoints\LTX 2.3 NSFW Checkpoint.safetensors"}, "class_type": "CheckpointLoaderSimple"},
    "320:317": {"inputs": {"text_encoder": "gemma_3_12B_it_fp4_mixed.safetensors", "ckpt_name": r"Video generation checkpoints\LTX 2.3 NSFW Checkpoint.safetensors", "device": "default"}, "class_type": "LTXAVTextEncoderLoader"},
    "320:318": {"inputs": {"video_latent": ["320:296", 0], "audio_latent": ["320:305", 0]}, "class_type": "LTXVConcatAVLatent"},
    "320:319": {"inputs": {"value": ""}, "class_type": "PrimitiveStringMultiline"},
}

# ── State ─────────────────────────────────────────────────────────────────────
# (A module-level `state = {...}` used to be defined HERE, shadowing the shared state
#  imported from s32_config. Everything in this module silently used a PRIVATE copy:
#  collector writes to last_image_path/session_images never reached the rest of the app
#  (the stale "saved:" path; get_output_image "no images this session"), and gen_lock /
#  comfy_image_ready here were not the real ones. Removed 2026-10-06 — this module now
#  uses the one shared `state` from s32_config like everything else.)

_result_id   = 0
_job_id      = 0
_result_lock = threading.Lock()

_comfy_start_lock = threading.Lock()


def start_comfy_image():
    """Launch ComfyUI (blocks until ready). Only one launch at a time — the revive
    monitor, node installer and restart_comfyui can all call this; a second caller
    while a launch is in progress just returns instead of starting a duplicate."""
    if state.get("comfy_hold"):
        # Stopped on purpose (an install is replacing its files) — the installer starts it.
        log(f"[S32] ComfyUI stays stopped for now — installing {state['comfy_hold']}")
        return
    if not _comfy_start_lock.acquire(blocking=False):
        log("[S32] ComfyUI launch already in progress — not starting a second one.")
        return
    try:
        return _start_comfy_image_impl()
    finally:
        _comfy_start_lock.release()


def _start_comfy_image_impl():
    host   = CONFIG["comfy_image_host"]
    dirr   = CONFIG.get("comfy_image_dir", "")
    python = CONFIG.get("comfy_image_python", "")
    inp    = CONFIG.get("comfy_image_input", "")
    # ComfyUI must write its raw outputs to its OWN staging folder, NOT into the user's
    # 'generated images'. Previously this was 'out_images' (= generated images), so ComfyUI
    # dumped EVERY output type (images, video frames, 3D, etc.) straight into that folder,
    # and the collector's organized copies piled up on top. Now ComfyUI stages into
    # {install}/output; the collector fetches each output over HTTP, writes ONE organized
    # copy into the correct 'System 32's Output' folder by type, and deletes the staged
    # original — so each thing lands once, in its right folder, nothing left in staging.
    out    = os.path.join(dirr, "output") if dirr else CONFIG.get("comfy_image_output", "")

    if is_up(host):
        # Already running - but verify it can actually see models
        # If checkpoint list is empty, it was probably launched without our flags
        try:
            obj = http_get(f"{host}/object_info/CheckpointLoaderSimple")
            ckpts = obj.get("CheckpointLoaderSimple", {}).get("input", {}).get("required", {}).get("ckpt_name", [[]])[0]
            if ckpts:
                log("[S32] ComfyUI image already running.")
                state["comfy_image_ready"] = True
                __import__("s32_config").adopt_port_owner(int(host.rsplit(":", 1)[1]), "ComfyUI")
                return
            else:
                log("[S32] ComfyUI image running but has no checkpoints - relaunching with correct flags...")
                # Terminate the existing process so we can relaunch with our flags
                try:
                    import psutil
                    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                        try:
                            cmdline = " ".join(proc.info.get('cmdline') or [])
                            if "main.py" in cmdline and "8188" in cmdline:
                                proc.terminate()
                                proc.wait(timeout=5)
                                log(f"[S32] Terminated existing ComfyUI image process (pid {proc.pid})")
                        except Exception:
                            pass
                except ImportError:
                    # psutil not available - try subprocess kill
                    try:
                        subprocess.run(
                            ["taskkill", "/F", "/FI", "WINDOWTITLE eq ComfyUI*"],
                            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
                        )
                    except Exception:
                        pass
                time.sleep(3)
        except Exception:
            log("[S32] ComfyUI image already running.")
            state["comfy_image_ready"] = True
            try:
                __import__("s32_config").adopt_port_owner(int(host.rsplit(":", 1)[1]), "ComfyUI")
            except Exception:
                pass
            return

    if not dirr or not os.path.exists(dirr):
        log(f"[S32] ComfyUI (image) dir not found: {dirr} - skipping.")
        return
    if not python or not os.path.exists(python):
        log(f"[S32] ComfyUI (image) python not found: {python} - skipping.")
        return

    main_py = os.path.join(dirr, "main.py")
    if not os.path.exists(main_py):
        log(f"[S32] ComfyUI (image) main.py not found at: {main_py}")
        log(f"[S32] Update 'comfy_image_dir' in config.json to the folder containing main.py")
        return

    # Find extra_models_config - check all known locations
    user = os.environ.get("USERNAME", "")
    appdata = os.environ.get("APPDATA", "")
    _extra_candidates = [
        os.path.join(appdata, "ComfyUI", "extra_models_config.yaml"),
        os.path.join(appdata, "ComfyUI", "extra_model_paths.yaml"),
        os.path.join(dirr, "extra_models_config.yaml"),
        os.path.join(dirr, "extra_model_paths.yaml"),
        rf"C:\Users\{user}\AppData\Roaming\ComfyUI\extra_models_config.yaml",
        rf"C:\Users\{user}\AppData\Roaming\ComfyUI\extra_model_paths.yaml",
        rf"C:\Users\{user}\Documents\ComfyUI\extra_model_paths.yaml",
    ]
    extra_models = next((p for p in _extra_candidates if os.path.exists(p)), None)
    if extra_models:
        log(f"[S32] Found extra_models_config: {extra_models}")
    else:
        log(f"[S32] WARNING: No extra_models_config found - checkpoints may not be visible")
        log(f"[S32] Checked: {_extra_candidates[0]}, {_extra_candidates[2]}")

    log("[S32] Starting ComfyUI (image)...")
    logf = os.path.join(dirr, "system32_image.log")
    lf = open(logf, "w", encoding="utf-8", errors="replace")
    cmd = [python, "main.py",
           "--listen", "127.0.0.1", "--port", "8188",
           "--disable-auto-launch", "--windows-standalone-build",
           "--input-directory", inp, "--output-directory", out,
           "--lowvram"]
    if extra_models:
        cmd += ["--extra-model-paths-config", extra_models]
    custom_nodes = CONFIG.get("custom_nodes_dir", "")
    # custom nodes are loaded via extra_models_config.yaml, not a CLI flag

    log(f"[S32] ComfyUI image UI at {host}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    from s32_config import CREATE_SUSPENDED, tie_to_s32
    _cp = subprocess.Popen(
        cmd,
        cwd=dirr, stdout=lf, stderr=lf,
        creationflags=subprocess.CREATE_NO_WINDOW | (CREATE_SUSPENDED if os.name == "nt" else 0),
        env=env
    )
    tie_to_s32(_cp, "ComfyUI")            # ends with System 32, however it closes
    for i in range(160):  # up to ~8 min on slower machines
        time.sleep(3)
        if is_up(host):
            log("[S32] ComfyUI (image) ready.")
            state["comfy_image_ready"] = True
            return
        if i % 5 == 0:
            log(f"[S32] Waiting for ComfyUI image... ({i*3}s)")
    log("[S32] ERROR: ComfyUI (image) did not start in time.")
    try:
        with open(logf, "r") as f:
            tail = f.readlines()[-20:]
        log("[S32] Last lines from ComfyUI image log:")
        for line in tail:
            log(f"[S32]   {line.rstrip()}")
    except Exception:
        pass

# Video ComfyUI removed — all generation runs on image instance (port 8188)

# ── ComfyUI helpers ───────────────────────────────────────────────────────────

def comfy_queue(host, workflow):
    cid = str(uuid.uuid4())
    try:
        http_post(f"{host}/free",
                  {"unload_models": False, "free_memory": False}, timeout=5)
    except Exception:
        pass
    try:
        resp = http_post(f"{host}/prompt", {
            "prompt": workflow, "client_id": cid,
            "extra_data": {"extra_pnginfo": {"run_id": cid}}
        })
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        log(f"[S32][QUEUE] HTTP {e.code} from ComfyUI: {body[:600]}")
        # Parse missing node errors from the 400 response and add to state
        try:
            error_dict = json.loads(body)
            workflow_missing = _scan_workflow_error(error_dict)
            if workflow_missing:
                label = "image (8188)"
                existing = state.get("missing_nodes", {})
                existing.setdefault(label, [])
                for m in workflow_missing:
                    if m not in existing[label]:
                        existing[label].append(m)
                state["missing_nodes"] = existing
                log(f"[S32] Workflow missing nodes added: {workflow_missing}")
        except Exception:
            pass
        raise
    except Exception as e:
        log(f"[S32][QUEUE] Queue error: {e}")
        raise

    if "error" in resp:
        log(f"[S32][QUEUE] ComfyUI rejected prompt: {json.dumps(resp)[:400]}")
    if "node_errors" in resp and resp["node_errors"]:
        log(f"[S32][QUEUE] Node errors: {json.dumps(resp['node_errors'])[:400]}")

    pid = resp.get("prompt_id")
    if pid:
        # Record our own submissions so the output watcher can tell System 32's
        # generations apart from ones the operator runs in the ComfyUI tab.
        state.setdefault("own_prompt_ids", set()).add(pid)
    return pid, cid

_CANCELLED = {"outputs": {}, "status": {"status_str": "cancelled", "messages": []},
              "_cancelled": True}
CANCELLED_MSG = "Cancelled by the operator — this run was stopped."


def _comfy_post_raw(url, data, timeout=8):
    """POST that tolerates ComfyUI's empty-body replies (/interrupt, /queue)."""
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def cancel_comfy_jobs(reason="operator"):
    """Stop ITS ComfyUI work: flag the batch loop to stop, drop its prompts still
    waiting in ComfyUI's queue, and interrupt the running one if it's its.
    The operator's own ComfyUI-tab jobs are never touched (own_prompt_ids)."""
    state["gen_cancel"] = True
    host = CONFIG.get("comfy_image_host", "http://127.0.0.1:8188")
    own = state.get("own_prompt_ids", set())
    dropped, interrupted = [], False
    try:
        q = http_get(f"{host}/queue", timeout=5) or {}
        pending = [e[1] for e in q.get("queue_pending", []) if len(e) > 1 and e[1] in own]
        running = [e[1] for e in q.get("queue_running", []) if len(e) > 1 and e[1] in own]
        if pending:
            _comfy_post_raw(f"{host}/queue", {"delete": pending})
            dropped = pending
        if running:
            # prompt_id makes newer ComfyUI interrupt only that prompt; older builds
            # ignore it and interrupt whatever runs — which we just checked is its.
            _comfy_post_raw(f"{host}/interrupt", {"prompt_id": running[0]})
            interrupted = True
    except Exception as e:
        log(f"[S32][CANCEL] ComfyUI cancel request failed: {e}")
    log(f"[S32][CANCEL] ComfyUI ({reason}): interrupted={interrupted} dropped_pending={len(dropped)}")
    return {"interrupted": interrupted, "dropped": len(dropped)}


def comfy_wait(host, prompt_id):
    elapsed = 0
    while True:
        time.sleep(3)
        elapsed += 3
        if state.get("gen_cancel"):
            return dict(_CANCELLED)
        try:
            hist = http_get(f"{host}/history/{prompt_id}", timeout=10)
            if prompt_id in hist:
                return hist[prompt_id]
        except Exception:
            pass
        mins, secs = divmod(elapsed, 60)
        state["job_status"] = f"Working... {mins}m {secs}s" if mins else f"Working... {secs}s"

# ── Restart ComfyUI (it can now do this itself) ────────────────────────────
#  ComfyUI builds its model lists at startup and did NOT notice a model being removed
#  or added without a restart (seen 2026-10-06: a finished 6.9 GB download stayed
#  invisible). Used automatically after model downloads finish, when preflight finds a
#  file on disk ComfyUI can't see, and by the restart_comfyui tool.
_restart_lock = threading.Lock()


def _comfy_note(text, key=None):
    """A ComfyUI status line. group="activity" → the UI folds back-to-back notes
    (downloads, restarts) into ONE card; a repeated `key` updates its line in place
    (↻ Restarting… → ✓ restarted) instead of adding a box."""
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result({"content": text, "path": "⬡ ComfyUI", "group": "activity",
                           "label": "comfy", "key": key}, jid)
        _cfg._push_done(jid)
    except Exception:
        pass


def _kill_comfy_port(port):
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pids = {ln.split()[-1] for ln in r.stdout.splitlines()
                if f":{port} " in ln and "LISTENING" in ln.upper() and ln.split()[-1].isdigit()}
        for pid in pids:
            subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            log(f"[S32] Stopped ComfyUI (PID {pid}) for restart")
        return bool(pids)
    except Exception as e:
        log(f"[S32] Could not stop ComfyUI: {e}")
        return False


def comfy_stopped(reason=""):
    """Context manager (2026-11-05): ComfyUI STOPPED while the body runs, started again after.
    On Windows a running ComfyUI keeps its packages' files locked (cv2.pyd …), so pip can't
    replace them ('[WinError 5] Access is denied') and every node / package install that
    touches them failed. While stopped, `comfy_hold` makes start_comfy_image refuse, so the
    revive / health monitors can't bring it back mid-install. Waits for a running generation
    first. ComfyUI is started again however the body ends."""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        for _ in range(900):                       # up to ~30 min for a running job
            if not state["gen_lock"].locked():
                break
            time.sleep(2)
        state["comfy_hold"] = reason or "an update"          # WHAT is being installed
        state["comfy_image_ready"] = False
        port = int(str(CONFIG.get("comfy_image_host", "http://127.0.0.1:8188")).rsplit(":", 1)[-1])
        log(f"[S32] Stopping ComfyUI while installing {state['comfy_hold']} (its files are locked while it runs)")
        if _kill_comfy_port(port):
            time.sleep(4)                           # let Windows release the file locks
        try:
            yield
        finally:
            state["comfy_hold"] = None
            try:
                start_comfy_image()
            finally:
                try:
                    import s32_models
                    s32_models.clear_cache()
                except Exception:
                    pass
                if state.get("comfy_image_ready"):
                    state["comfy_last_restart"] = time.time()
    return _cm()


def restart_comfyui(reason="", wait=True):
    """Restart the ComfyUI backend so it re-scans models and nodes. Waits for any running
    generation to finish first. wait=False runs it in the background."""
    if not _restart_lock.acquire(blocking=False):
        return {"ok": True, "note": "ComfyUI is already restarting."}

    def _do():
        try:
            for _ in range(900):                       # up to ~30 min for a running job
                if not state["gen_lock"].locked():
                    break
                time.sleep(2)
            log(f"[S32] Restarting ComfyUI{(' — ' + reason) if reason else ''}...")
            _comfy_note("↻ Restarting ComfyUI so it picks up new files (about a minute)…", key="restart")
            port = int(str(CONFIG.get("comfy_image_host", "http://127.0.0.1:8188")).rsplit(":", 1)[-1])
            state["comfy_image_ready"] = False
            _kill_comfy_port(port)
            time.sleep(3)
            start_comfy_image()
            try:
                import s32_models
                s32_models.clear_cache()
            except Exception:
                pass
            if state.get("comfy_image_ready"):
                state["comfy_last_restart"] = time.time()
                _comfy_note("✓ ComfyUI restarted — new models and nodes are loaded.", key="restart")
            else:
                _comfy_note("⚠ ComfyUI didn't come back after the restart — check system32_image.log.", key="restart")
        finally:
            _restart_lock.release()

    if wait:
        _do()
        return {"ok": bool(state.get("comfy_image_ready")), "restarted": True}
    threading.Thread(target=_do, daemon=True, name="comfy-restart").start()
    return {"ok": True, "restarting": True}


# ── Phase 5: surface the operator's own ComfyUI-tab generations into chat ────
_WATCH_3D    = {".glb", ".obj", ".ply", ".stl", ".fbx", ".gltf", ".usdz"}
_WATCH_VIDEO = {".mp4", ".webm", ".mov", ".avi", ".mkv", ".gif"}
_WATCH_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _classify_ext(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in _WATCH_3D:    return "3d"
    if ext in _WATCH_VIDEO: return "video"
    if ext in _WATCH_IMAGE: return "image"
    return "other"


def _surface_user_generation(host, pid, entry):
    """Pull the outputs of a ComfyUI prompt the operator ran themselves (in the
    ComfyUI tab) into the chat pane and into its working context, so it can see
    it and use it in later tool calls. Returns how many outputs were surfaced."""
    outputs = (entry or {}).get("outputs", {})
    media, other = [], []
    for _node, node_out in outputs.items():
        if not isinstance(node_out, dict):
            continue
        for _key, items in node_out.items():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("filename"):
                    if _classify_ext(item["filename"]) != "other":
                        media.append(item)
                    else:
                        other.append(item["filename"])
    if not media and not other:
        return 0

    # Same rule as run_workflow: if this generation produced a video, its image
    # outputs are just frames — keep only the video so 'generated images' stays
    # images-only. (Applies to manual ComfyUI-tab video gens too.)
    if any(_classify_ext(m["filename"]) == "video" for m in media):
        media = [m for m in media if _classify_ext(m["filename"]) != "image"]

    import s32_config as _cfg
    with _result_lock:
        _cfg._job_id += 1
        jid = _cfg._job_id

    # Header note so the operator knows this came from the ComfyUI tab.
    _push_result({"content": "Generated by you in the ComfyUI tab — I can see this "
                             "and use it in tool calls (e.g. recall it, build on it, "
                             "or train from it).", "path": "⬡ From ComfyUI",
                  "source": "comfyui_user"}, jid)

    pushed, seen = 0, set()
    ts = time.strftime("%Y%m%d_%H%M%S")
    for item in media:
        fname = item["filename"]
        if fname in seen:
            continue
        seen.add(fname)
        kind = _classify_ext(fname)
        try:
            raw = comfy_image_bytes(host, fname, item.get("subfolder", ""), item.get("type", "output"))
            if kind == "image":
                out_dir = CONFIG["out_images"]; ext = ".png"
            elif kind == "video":
                out_dir = CONFIG["out_videos"]; ext = os.path.splitext(fname)[1] or ".mp4"
            else:
                out_dir = CONFIG.get("out_3d") or os.path.join(_script_dir, "Output", "generated 3d")
                ext = os.path.splitext(fname)[1] or ".glb"
            os.makedirs(out_dir, exist_ok=True)
            save_path = os.path.join(out_dir, f"comfyui_{ts}_{pushed}{ext}")
            with open(save_path, "wb") as f:
                f.write(raw)
            url = "/img?path=" + urllib.parse.quote(save_path)
            if kind == "image":
                state["last_image_bytes"] = raw
                state["last_image_path"]  = save_path
                state["session_images"].append({"bytes": raw, "path": save_path, "prompt": "(ComfyUI tab)"})
                _cap_session_images()
                res = {"type": "image", "url": url, "filename": fname, "saved": save_path, "source": "comfyui_user", "genid": fname}
            elif kind == "video":
                state["last_video_path"] = save_path
                res = {"type": "video", "url": url, "ext": ext.lstrip("."), "filename": fname,
                       "saved": save_path, "source": "comfyui_user"}
            else:
                state["last_3d_path"] = save_path
                res = {"type": "model_3d", "filename": fname, "saved": save_path,
                       "message": "3D model from ComfyUI tab", "source": "comfyui_user"}
            _push_result(res, jid)
            pushed += 1
        except Exception as e:
            log(f"[S32][COMFY-WATCH] surface failed for {fname}: {e}")

    # Non-image outputs (LoRAs, checkpoints, .safetensors, datasets...) — it
    # can't "view" weights, but it should know they appeared so it can use them.
    if other:
        names = ", ".join(sorted(set(other))[:8])
        extra = "" if len(set(other)) <= 8 else f" (+{len(set(other)) - 8} more)"
        _push_result({"content": f"Non-image output(s) from your ComfyUI run: {names}{extra}. "
                                 f"Likely LoRA / model / dataset files — ask me to find them, "
                                 f"inspect them, or use them in a generation.",
                      "path": "⬡ From ComfyUI", "source": "comfyui_user"}, jid)
        pushed += len(set(other))

    _push_done(jid)
    if pushed:
        log(f"[S32][COMFY-WATCH] surfaced {pushed} ComfyUI-tab output(s) from prompt {str(pid)[:8]}")
    return pushed


def _comfy_output_watch_loop():
    host = CONFIG["comfy_image_host"]
    seeded = False
    while True:
        try:
            if is_up(host):
                hist = http_get(f"{host}/history?max_items=64", timeout=8) or {}
                own      = state.setdefault("own_prompt_ids", set())
                surfaced = state.setdefault("surfaced_prompt_ids", set())
                for pid, entry in hist.items():
                    if pid in surfaced or pid in own:
                        continue
                    # Only surface completed prompts; leave unfinished ones for a later poll.
                    status = (entry or {}).get("status", {})
                    if isinstance(status, dict) and status.get("completed") is False:
                        continue
                    if not seeded:
                        surfaced.add(pid)   # first pass: mark existing history as already-seen
                        continue
                    _surface_user_generation(host, pid, entry)
                    surfaced.add(pid)
                seeded = True
                if len(surfaced) > 500:
                    state["surfaced_prompt_ids"] = set(list(surfaced)[-250:])
        except Exception as e:
            log(f"[S32][COMFY-WATCH] loop error: {e}")
        time.sleep(5)


def start_comfy_output_watcher():
    """Start the background watcher that mirrors ComfyUI-tab generations to chat."""
    threading.Thread(target=_comfy_output_watch_loop, daemon=True).start()
    log("[S32] ComfyUI output watcher started — your ComfyUI-tab generations will appear in chat.")


# ── Phase 5b: catch training/model artifacts that bypass ComfyUI /history ────
# LoRA / checkpoint trainers often write a .safetensors straight to disk without
# ever registering an output in /history, so the history watcher above can't see
# them. This filesystem watcher fills that gap: it notices NEW model files landing
# in the output folders and surfaces a note in chat so it knows they exist.
_MODEL_EXTS = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf"}


def _fs_watch_dirs():
    dirs = []
    for key in ("out_loras", "out_checkpoints", "out_diffusion"):
        d = CONFIG.get(key, "")
        if d and os.path.isdir(d):
            dirs.append(d)
    return dirs


def _scan_model_files(d):
    """Return {path: (mtime, size)} for model files under d (recursive)."""
    out = {}
    try:
        for root, _dirs, files in os.walk(d):
            for fn in files:
                if os.path.splitext(fn)[1].lower() in _MODEL_EXTS:
                    full = os.path.join(root, fn)
                    try:
                        st = os.stat(full)
                        out[full] = (st.st_mtime, st.st_size)
                    except Exception:
                        pass
    except Exception:
        pass
    return out


def _surface_disk_artifact(path):
    """Push a note about a newly-appeared model artifact into chat + context."""
    import s32_config as _cfg
    with _result_lock:
        _cfg._job_id += 1
        jid = _cfg._job_id
    name = os.path.basename(path)
    low = path.lower()
    kind = ("LoRA" if "lora" in low
            else "checkpoint" if ("checkpoint" in low or name.lower().endswith(".ckpt"))
            else "model")
    state["last_artifact_path"] = path
    _push_result({"content": f"A new {kind} just appeared on disk: {name}\n"
                             f"Path: {path}\n"
                             f"Looks like a training/model output. Ask me to find it, inspect "
                             f"it, move it, rename it, or load it into a generation.",
                  "path": "⬡ From ComfyUI"}, jid)
    _push_done(jid)
    log(f"[S32][COMFY-FS] surfaced new artifact: {name}")


def _comfy_fs_watch_loop():
    seen, pending = {}, {}     # seen: path->mtime (known); pending: path->size (settling)
    seeded = False
    while True:
        try:
            current = {}
            for d in _fs_watch_dirs():
                current.update(_scan_model_files(d))
            if not seeded:
                seen = {p: mt for p, (mt, _sz) in current.items()}   # seed backlog as known
                seeded = True
            else:
                for p, (mt, sz) in current.items():
                    if p in seen:
                        continue
                    if pending.get(p) == sz:        # size stable since last poll → write finished
                        _surface_disk_artifact(p)
                        seen[p] = mt
                        pending.pop(p, None)
                    else:
                        pending[p] = sz             # new or still growing — wait one more cycle
                # forget anything that was deleted/moved
                for p in list(pending):
                    if p not in current:
                        pending.pop(p, None)
                for p in list(seen):
                    if p not in current:
                        seen.pop(p, None)
        except Exception as e:
            log(f"[S32][COMFY-FS] loop error: {e}")
        time.sleep(8)


# ── Model hot-swap: drop a model in (or take one out) → ComfyUI reloads ───────
#  ComfyUI builds its model lists at startup and doesn't notice files being added or
#  removed (confirmed 2026-10-06), which made every model change a manual restart.
#  This watches EVERY folder ComfyUI loads models from (asked from ComfyUI, so extra
#  model paths are covered), waits until changes have settled (a copy has finished and
#  nothing else has changed for _HOT_QUIET seconds — five files dropped = one restart),
#  waits until ComfyUI is idle (its jobs AND the operator's ComfyUI-tab queue), then
#  restarts it. Its own downloads are skipped — they restart ComfyUI themselves.
#  Off switch: "model_hotswap": false in config.json.
_HOT_FOLDERS = ("checkpoints", "loras", "vae", "diffusion_models", "unet", "text_encoders",
                "clip", "clip_vision", "upscale_models", "controlnet", "embeddings",
                "style_models", "hypernetworks", "gligen")
_HOT_QUIET = 20
_HOT_POLL = 6
_hot_dirs = {"t": 0.0, "dirs": []}


def comfy_model_dirs(refresh=False):
    """Every folder ComfyUI loads model files from (ComfyUI is asked; cached 5 min)."""
    if not refresh and _hot_dirs["dirs"] and time.time() - _hot_dirs["t"] < 300:
        return _hot_dirs["dirs"]
    dirs = set()
    try:
        fp = http_get(f"{CONFIG['comfy_image_host']}/internal/folder_paths", timeout=5)
        for k in _HOT_FOLDERS:
            e = fp.get(k) if isinstance(fp, dict) else None
            paths = e[0] if (isinstance(e, list) and e and isinstance(e[0], list)) else e
            for p in (paths or []):
                if isinstance(p, str) and os.path.isdir(p):
                    dirs.add(os.path.normpath(p))
    except Exception:
        pass
    if not dirs and _hot_dirs["dirs"]:
        return _hot_dirs["dirs"]                 # ComfyUI down (e.g. mid-restart) — keep last list
    fallback = not dirs
    if fallback:
        root = CONFIG.get("models_dir") or os.path.join(CONFIG.get("comfy_image_dir", ""), "models")
        dirs = {os.path.normpath(os.path.join(root, k)) for k in _HOT_FOLDERS
                if os.path.isdir(os.path.join(root, k))}
    # A guessed list is only kept ~20s, so ComfyUI's real list replaces it promptly.
    _hot_dirs.update(t=time.time() - (280 if fallback else 0), dirs=sorted(dirs))
    return _hot_dirs["dirs"]


def _comfy_idle():
    """True when nothing is generating: its generation lock, its downloads/installs, and
    ComfyUI's own queue (catches jobs the operator runs in the ComfyUI tab)."""
    if state["gen_lock"].locked() or state.get("download_running") or state.get("node_install_running"):
        return False
    try:
        q = http_get(f"{CONFIG['comfy_image_host']}/queue", timeout=5)
        if q.get("queue_running") or q.get("queue_pending"):
            return False
    except Exception:
        return False                             # can't tell → don't risk interrupting
    return True


def _own_downloads_busy():
    try:
        import s32_download as _dl
        return bool(_dl._active or _dl._auto_queue or state.get("download_running"))
    except Exception:
        return bool(state.get("download_running"))


def _model_hotswap_loop():
    baseline = prev = None
    last_change = 0.0
    watched = None
    while True:
        time.sleep(_HOT_POLL)
        try:
            if not CONFIG.get("model_hotswap", True):
                continue
            dirs = comfy_model_dirs()
            if not dirs:
                continue
            if dirs != watched:
                # The SET of folders changed (at boot: a guessed list → ComfyUI's real one,
                # which adds the extra model folders). Files in newly watched folders aren't
                # "added" — re-baseline instead. (Every boot reported "added: hunyuan…" and
                # restarted ComfyUI for nothing, 2026-10-13.)
                if watched is not None:
                    log(f"[S32][HOTSWAP] watching {len(dirs)} model folder(s) — re-baselined")
                watched = list(dirs)
                baseline = prev = None
            cur = {}
            for d in dirs:
                cur.update(_scan_model_files(d))
            sig = {p: v[1] for p, v in cur.items()}          # path → size
            if baseline is None:
                baseline = prev = sig
                # Once per boot: ComfyUI may have been running BEFORE System 32 started
                # ("ComfyUI image already running"), so files added/removed while we were
                # off never registered with it — and the watcher's baseline can't see that.
                # Compare ComfyUI's own lists with the disk; restart if they disagree.
                try:
                    import s32_models
                    _dr = s32_models.list_drift(CONFIG["comfy_image_host"])
                    if _dr and (_dr[0] or _dr[1]):
                        _desc = "; ".join(x for x in (
                            ("missing on disk: " + ", ".join(os.path.basename(g) for g in _dr[0][:6])) if _dr[0] else "",
                            ("not loaded yet: " + ", ".join(os.path.basename(n) for n in _dr[1][:6])) if _dr[1] else "") if x)
                        log(f"[S32][HOTSWAP] ComfyUI's model list is out of date ({_desc})")
                        for _ in range(100):                     # wait (≤10 min) for idle
                            if _comfy_idle() and not _restart_lock.locked():
                                break
                            time.sleep(6)
                        if not _restart_lock.locked():
                            _comfy_note(f"ComfyUI's model list was out of date ({_desc}).")
                            restart_comfyui("model list out of date", wait=True)
                except Exception as e:
                    log(f"[S32][HOTSWAP] drift check failed: {e}")
                continue
            if sig != prev:                                   # still being copied / more coming
                prev, last_change = sig, time.time()
                continue
            added   = [p for p in sig if p not in baseline]
            removed = [p for p in baseline if p not in sig]
            changed = [p for p in sig if p in baseline and sig[p] != baseline[p]]
            if not (added or removed or changed):
                continue
            if time.time() - last_change < _HOT_QUIET:
                continue                                      # let a multi-file drop finish
            if state.get("comfy_last_restart", 0) >= last_change:
                baseline = sig                                # a restart already covered this
                continue
            if _restart_lock.locked():
                continue                                      # a restart is already underway — its timestamp will cover this
            if _own_downloads_busy():
                continue                                      # its download restarts ComfyUI itself
            if not _comfy_idle():
                continue                                      # wait for running jobs
            names = [os.path.basename(p) for p in (added + changed)]
            gone  = [os.path.basename(p) for p in removed]
            desc = "; ".join(x for x in (
                ("added: " + ", ".join(names[:6]) + (" …" if len(names) > 6 else "")) if names else "",
                ("removed: " + ", ".join(gone[:6]) + (" …" if len(gone) > 6 else "")) if gone else "") if x)
            log(f"[S32][HOTSWAP] model files changed ({desc}) — restarting ComfyUI")
            _comfy_note(f"Model files changed on disk ({desc}).")
            restart_comfyui("model files changed", wait=True)
            baseline = sig
        except Exception as e:
            log(f"[S32][HOTSWAP] loop error: {e}")


def start_comfy_fs_watcher():
    """Start the watcher for model/training artifacts written directly to disk, and the
    model hot-swap watcher for ComfyUI's model folders."""
    threading.Thread(target=_comfy_fs_watch_loop, daemon=True).start()
    log("[S32] ComfyUI artifact watcher started — new LoRAs/models on disk will appear in chat.")
    threading.Thread(target=_model_hotswap_loop, daemon=True, name="model-hotswap").start()
    log("[S32] Model hot-swap watcher started — dropping models in/out of ComfyUI's folders "
        "reloads ComfyUI automatically.")


def comfy_image_bytes(host, filename, subfolder, ftype):
    params = urllib.parse.urlencode(
        {"filename": filename, "subfolder": subfolder, "type": ftype})
    with urllib.request.urlopen(f"{host}/view?{params}", timeout=30) as r:
        return r.read()

def upload_image(host, img_bytes, filename):
    boundary = "----S32Boundary" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/png\r\n\r\n"
    ).encode() + img_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{host}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def fetch_checkpoints():
    try:
        d = http_get(f"{CONFIG['comfy_image_host']}/object_info/CheckpointLoaderSimple", timeout=8)
        return sorted(d["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0])
    except Exception:
        return []

def fetch_lora_options():
    try:
        d = http_get(f"{CONFIG['comfy_image_host']}/object_info/TrainLoraNode", timeout=8)
        field = d["TrainLoraNode"]["input"]["required"]["existing_lora"]
        if isinstance(field, list) and len(field) > 1 and isinstance(field[1], dict):
            return field[1].get("options", ["[None]"])
        return ["[None]"]
    except Exception:
        return ["[None]"]

def fetch_datasets():
    d = CONFIG["datasets_dir"]
    if not os.path.exists(d):
        return []
    return sorted(x for x in os.listdir(d) if os.path.isdir(os.path.join(d, x)))

# ── Filesystem tools (read-only) ──────────────────────────────────────────────

def _default_checkpoint(preferred=None):
    """
    Return the best available checkpoint name.
    Uses the preferred name if it's in the available list.
    Falls back to the first available checkpoint.
    Returns empty string if nothing is available.
    """
    try:
        available = fetch_checkpoints()
        if not available:
            return preferred or ""
        if preferred and preferred in available:
            return preferred
        # Try to find a close match (same stem)
        if preferred:
            stem = os.path.splitext(preferred)[0].lower()
            for c in available:
                if stem in c.lower():
                    return c
        return available[0]
    except Exception:
        return preferred or ""

def _preflight_block(host, wf, what):
    """generate_image / generate_video build their workflow in code and queued it blind —
    a missing node came back as a bare 'Queue failed: HTTP Error 400' (2026-11-05). Same
    pre-check as run_workflow: missing nodes / models are installed or downloaded, and the
    operator gets the same card. None = go ahead."""
    if state.get("comfy_hold"):
        _what = (state.get("installing") or {}).get("what") or state["comfy_hold"]
        return {"group": "activity", "label": "workflow",
                "content": f"ComfyUI is stopped for a moment while installing {_what} — ask again when the bar finishes.",
                "error": (f"ComfyUI is stopped while System 32 installs {_what}. Nothing was run. Tell "
                          f"the operator to wait for the '✓ Installed' note and ask again.")}
    try:
        import s32_models
        _pf = s32_models.preflight(host, wf)
    except Exception as e:
        log(f"[S32][WF] preflight skipped: {e}")
        return None
    if _pf and _pf.get("block"):
        return {"error": _pf["message"], "group": "activity", "label": "workflow",
                "missing_nodes": _pf.get("nodes", []),
                "content": f"{what} can't run yet: {_pf.get('operator') or 'files are missing'}. "
                           f"Run it again when this finishes."}
    return None


def run_image(params, img_bytes=None):
    host = CONFIG["comfy_image_host"]
    if not state["comfy_image_ready"]:
        if not is_up(host):
            return {"error": "ComfyUI (image) not ready yet."}
        state["comfy_image_ready"] = True
    wf = copy.deepcopy(IMAGE_WORKFLOW)
    wf["13"]["inputs"]["positive"]      = params.get("positive", "")
    wf["14"]["inputs"]["negative"]      = params.get("negative", "")
    wf["31:25"]["inputs"]["ckpt_name"]  = _default_checkpoint(params.get("checkpoint"))
    wf["31:28"]["inputs"]["steps"]      = int(params.get("steps", 64))
    wf["31:28"]["inputs"]["cfg"]        = float(params.get("cfg", 8))
    wf["31:28"]["inputs"]["noise_seed"] = random.randint(0, 2**32-1)
    w = int(params.get("width",  1024))
    h = int(params.get("height", 1024))
    wf["31:35"]["inputs"]["width"]  = w
    wf["31:35"]["inputs"]["height"] = h
    wf["31:36"]["inputs"]["width"]  = w * 2
    wf["31:36"]["inputs"]["height"] = h * 2

    if img_bytes:
        fname = f"s32_{uuid.uuid4().hex[:8]}.png"
        upload_image(host, img_bytes, fname)
        wf["37"] = {"inputs": {"image": fname}, "class_type": "LoadImage"}
    else:
        wf.pop("37", None)

    _blk = _preflight_block(host, wf, "The image generator")
    if _blk:
        return _blk
    try:
        prompt_id, _ = comfy_queue(host, wf)
    except Exception as e:
        log(f"[S32][IMAGE] Failed to queue prompt: {e}")
        return {"error": f"Queue failed: {e}"}

    log(f"[S32][IMAGE] Queued prompt_id={prompt_id}")
    state["job_status"] = "Generating image..."
    result = comfy_wait(host, prompt_id)
    state["job_status"] = None
    if result.get("_cancelled"):
        return {"error": CANCELLED_MSG, "cancelled": True}

    outputs = result.get("outputs", {})
    status  = result.get("status", {})
    log(f"[S32][IMAGE] Result status: {status.get('status_str','?')} | output nodes: {list(outputs.keys())}")

    if status.get("status_str") == "error":
        msgs = [m for m in status.get("messages",[]) if m[0]=="execution_error"]
        for m in msgs:
            log(f"[S32][IMAGE] Execution error: {m}")
        return {"error": f"ComfyUI error: {msgs}"}

    for node_id, node_out in outputs.items():
        for img in node_out.get("images", []):
            log(f"[S32][IMAGE] Found output image: {img}")
            try:
                raw = comfy_image_bytes(host, img["filename"],
                                        img.get("subfolder",""), img.get("type","output"))
                out = CONFIG["out_images"]
                os.makedirs(out, exist_ok=True)
                ts   = time.strftime("%Y%m%d_%H%M%S")
                path = os.path.join(out, f"image_{ts}.png")
                with open(path, "wb") as f:
                    f.write(raw)
                log(f"[S32][IMAGE] Saved to {path}")
                state["last_image_bytes"] = raw
                state["last_image_path"]  = path
                state["session_images"].append({
                    "bytes":  raw,
                    "path":   path,
                    "prompt": params.get("positive", ""),
                    "index":  len(state["session_images"]) + 1
                })
                _cap_session_images()
                return {"type": "image",
                        "url":  f"/img?path={urllib.parse.quote(path)}",
                        "saved": path,
                        "source": "ai",
                        "genid": img["filename"]}
            except Exception as e:
                log(f"[S32][IMAGE] Failed to retrieve image: {e}")
                return {"error": str(e)}

    log(f"[S32][IMAGE] No image output found. Full result: {json.dumps(result, default=str)[:500]}")
    return {"error": "No image output found."}

def run_video(params, img_bytes=None):
    host = CONFIG["comfy_image_host"]
    if not state["comfy_image_ready"]:
        if not is_up(host):
            return {"error": "ComfyUI not ready yet."}
            wf = copy.deepcopy(VIDEO_WORKFLOW)

    fps      = int(params.get("fps", 25))
    duration = int(params.get("duration", 21))
    width    = int(params.get("width", 512))
    height   = int(params.get("height", 512))
    frames   = duration * fps + 1
    use_img  = bool(params.get("use_image", False))

    wf["320:319"]["inputs"]["value"]    = params.get("positive", "")
    wf["320:313"]["inputs"]["text"]     = params.get("negative", "pc game, console game, video game, cartoon, childish, ugly")
    wf["320:302"]["inputs"]["value"]    = not use_img
    wf["320:277"]["inputs"]["noise_seed"] = random.randint(0, 2**32-1)
    wf["320:300"]["inputs"]["value"]    = fps
    wf["320:301"]["inputs"]["value"]    = duration
    wf["320:312"]["inputs"]["value"]    = width
    wf["320:299"]["inputs"]["value"]    = height
    wf["320:295"]["inputs"]["width"]    = width // 2
    wf["320:295"]["inputs"]["height"]   = height // 2
    wf["320:295"]["inputs"]["length"]   = frames
    wf["320:304"]["inputs"]["frame_rate"] = fps
    wf["320:305"]["inputs"]["frames_number"] = frames
    wf["320:305"]["inputs"]["frame_rate"]    = fps
    wf["320:310"]["inputs"]["fps"]      = fps
    wf["320:290"]["inputs"]["resize_type.width"]  = width
    wf["320:290"]["inputs"]["resize_type.height"] = height

    if use_img and img_bytes:
        fname = f"s32_vid_{uuid.uuid4().hex[:8]}.png"
        upload_image(host, img_bytes, fname)
        wf["269"]["inputs"]["image"] = fname
        log(f"[S32][VIDEO] Using attached image: {fname}")
    elif use_img and state["last_image_bytes"]:
        # Use the last generated image automatically
        fname = f"s32_vid_{uuid.uuid4().hex[:8]}.png"
        upload_image(host, state["last_image_bytes"], fname)
        wf["269"]["inputs"]["image"] = fname
        log(f"[S32][VIDEO] Using last generated image: {state['last_image_path']}")
    else:
        try:
            from PIL import Image as PILImage
            blank = PILImage.new("RGB", (width, height), (128, 128, 128))
            buf = io.BytesIO()
            blank.save(buf, format="PNG")
            fname = f"s32_blank_{uuid.uuid4().hex[:8]}.png"
            upload_image(host, buf.getvalue(), fname)
            wf["269"]["inputs"]["image"] = fname
        except Exception:
            pass

    _blk = _preflight_block(host, wf, "The video generator")
    if _blk:
        return _blk
    try:
        prompt_id, _ = comfy_queue(host, wf)
    except Exception as e:
        log(f"[S32][VIDEO] Failed to queue prompt: {e}")
        return {"error": f"Queue failed: {e}"}

    log(f"[S32][VIDEO] Queued prompt_id={prompt_id}")
    state["job_status"] = "Generating video..."
    result = comfy_wait(host, prompt_id)
    state["job_status"] = None
    if result.get("_cancelled"):
        return {"error": CANCELLED_MSG, "cancelled": True}

    outputs = result.get("outputs", {})
    status  = result.get("status", {})
    log(f"[S32][VIDEO] Result status: {status.get('status_str','?')} | output nodes: {list(outputs.keys())}")

    if status.get("status_str") == "error":
        msgs = [m for m in status.get("messages",[]) if m[0]=="execution_error"]
        for m in msgs:
            log(f"[S32][VIDEO] Execution error: {m}")
        return {"error": f"ComfyUI error: {msgs}"}

    for node_id, node_out in outputs.items():
        for key in ["gifs", "videos", "images", "Animated"]:
            for item in node_out.get(key, []):
                log(f"[S32][VIDEO] Found output item: {item}")
                try:
                    raw = comfy_image_bytes(host, item["filename"],
                                            item.get("subfolder",""), item.get("type","output"))
                    out = CONFIG["out_videos"]
                    os.makedirs(out, exist_ok=True)
                    ts  = time.strftime("%Y%m%d_%H%M%S")
                    ext = os.path.splitext(item["filename"])[1] or ".mp4"
                    path = os.path.join(out, f"video_{ts}{ext}")
                    with open(path, "wb") as f:
                        f.write(raw)
                    log(f"[S32][VIDEO] Saved to {path}")
                    return {"type": "video",
                            "url":  f"/img?path={urllib.parse.quote(path)}",
                            "ext": ext.lstrip("."), "saved": path}
                except Exception as e:
                    log(f"[S32][VIDEO] Failed to retrieve video: {e}")
                    return {"error": str(e)}

    log(f"[S32][VIDEO] No video output found. Full result: {json.dumps(result, default=str)[:500]}")
    return {"error": "No video output found."}

def run_lora(params):
    if not state["comfy_image_ready"]:
        return {"error": "ComfyUI (image) not ready yet."}
    host = CONFIG["comfy_image_host"]

    # Load from the actual workflow JSON so we always have correct node fields
    try:
        wf = _wf_load("Lora Training.json")
    except Exception as e:
        return {"error": f"Could not load Lora Training.json: {e}"}
    wf = copy.deepcopy(wf)

    dataset = params.get("dataset", "")
    dataset_name = dataset
    if not dataset.startswith("Datasets/"):
        dataset = f"Datasets/{dataset}"

    # Ensure dataset folder exists - create it if needed
    datasets_dir = CONFIG.get("datasets_dir", "")
    dataset_folder = os.path.join(datasets_dir, dataset_name)
    os.makedirs(dataset_folder, exist_ok=True)
    log(f"[S32][LORA] Dataset folder: {dataset_folder}")

    # If images were provided, copy them into the dataset folder with captions
    source_images = params.get("images", [])
    if source_images:
        import shutil as _shutil
        caption = params.get("caption", dataset_name)
        for i, img_path in enumerate(source_images):
            if os.path.isfile(img_path):
                ext = os.path.splitext(img_path)[1].lower()
                dest_name = f"{dataset_name}_{i:04d}{ext}"
                dest_path = os.path.join(dataset_folder, dest_name)
                txt_path  = os.path.join(dataset_folder, f"{dataset_name}_{i:04d}.txt")
                _shutil.copy2(img_path, dest_path)
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(caption)
                log(f"[S32][LORA] Copied: {os.path.basename(img_path)} → {dest_name}")

    # Check dataset has images - warn if empty
    img_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    dataset_images = [f for f in os.listdir(dataset_folder)
                      if os.path.splitext(f)[1].lower() in img_exts]
    if not dataset_images:
        return {
            "error": f"Dataset folder is empty: {dataset_folder}",
            "tip": "Pass images=[list of paths] to populate the dataset, or add images manually"
        }
    log(f"[S32][LORA] Dataset has {len(dataset_images)} images")

    # Patch known fields - walk nodes by class type for robustness
    for node_id, node in wf.items():
        ct = node.get("class_type", "")
        inp = node.get("inputs", {})

        if ct == "CheckpointLoaderSimple":
            inp["ckpt_name"] = _default_checkpoint(params.get("checkpoint"))

        elif ct == "TrainLoraNode":
            inp["steps"]                   = int(params.get("steps", 512))
            inp["rank"]                    = int(params.get("rank", 64))
            inp["learning_rate"]           = float(params.get("learning_rate", 0.0005))
            inp["seed"]                    = random.randint(0, 2**32-1)
            inp["existing_lora"]           = params.get("existing_lora", "[None]")
            if "grad_accumulation_steps" in inp:
                inp["grad_accumulation_steps"] = int(params.get("grad_accum", 8))

        elif ct == "SaveLoRA":
            inp["prefix"] = params.get("lora_name", "loras/MyLoRA")

        elif ct == "LoadImageTextDataSetFromFolder":
            inp["folder"] = dataset

    try:
        prompt_id, _ = comfy_queue(host, wf)
    except Exception as e:
        return {"error": f"Failed to queue LoRA training: {e}"}

    state["job_status"] = "Training LoRA..."
    result = comfy_wait(host, prompt_id)
    state["job_status"] = None
    if result.get("_cancelled"):
        return {"error": CANCELLED_MSG, "cancelled": True}

    for node_id, node_out in result.get("outputs", {}).items():
        for img in node_out.get("images", []):
            if "loss" in img.get("filename","").lower() or node_id == "12":
                try:
                    raw = comfy_image_bytes(host, img["filename"],
                                            img.get("subfolder",""), img.get("type","output"))
                    return {"type": "lora_done",
                            "data": base64.b64encode(raw).decode(),
                            "lora_name": params.get("lora_name", "loras/MyLoRA")}
                except Exception:
                    pass
    return {"type": "lora_done", "data": None,
            "lora_name": params.get("lora_name", "loras/MyLoRA")}

# ── Tool dispatcher ───────────────────────────────────────────────────────────
# Tools are invoked ONLY when called explicitly from the chat handler.
# The LLM never calls these directly - it returns structured JSON which
# the server parses and dispatches here.

TOOL_NAMES = {
    "generate_image", "generate_video", "train_lora", "add_to_dataset",
    "list_directory", "find_file", "read_file", "show_file", "write_file", "get_drives",
    "explore_tree", "index_search", "smart_search", "get_system_info",
    "list_workflows", "run_workflow",
    "install_missing_nodes", "scan_comfy_logs",
    "get_output_image", "show_images",
    "memory_write", "memory_read", "memory_search", "memory_archive_read", "set_preference", "save_map", "mark_favorite", "get_favorites",
    "visual_search", "build_visual_index",
}

