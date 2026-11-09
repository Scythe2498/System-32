# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_similar.py — "Find images like this one" WITHOUT uploading it (2026-11-08, v2).

A real reverse image search (Google Lens, Bing, Yandex, TinEye) works by sending the
picture to that company. This does the same job on the operator's own computer:

  1. LOOK    — the model, which can already see the image, writes a few search phrases
               for it, including any text it can read in it (a watermark, a signature, a
               caption). A file's own name counts too when it's a real name.
  2. GATHER  — those WORDS go out through System 32's anonymous image search: every
               source at once, two pages each. The image itself never leaves.
               Unlike a normal image search, nothing is thrown away for having the "wrong"
               title — the original is often titled something we'd never guess; the
               pixels decide.
  3. SCORE   — every thumbnail is compared here: CLIP (a small image-understanding model,
               ~90 MB, on the drive) on the centred square AND on the whole picture, plus a
               difference hash for "the same picture, resized or recompressed".
  4. RETRY   — the titles of the closest matches are the words the rest of the web uses for
               this picture, so they become a second round of searches (still words only).
  5. CHECK   — the best few are downloaded at full size and compared again, plus a
               detail-by-detail match (OpenCV) that recognises the SAME picture even cropped,
               bordered or with text added.
  6. RETURN  — best first, labelled: same image / near-identical / very close / similar /
               related.
"""

import io
import os
import re
import threading
import time

from s32_config import CONFIG, state, log

_HOME = os.path.dirname(os.path.abspath(__file__))
_DIR = os.path.join(_HOME, "clip_models")
_PATH = os.path.join(_DIR, "clip-vit-base-patch32-vision-quantized.onnx")
# CLIP ViT-B/32 image encoder, ONNX (quantized, ~90 MB) — runs on onnxruntime, which the
# voice already installed. Downloaded once, anonymously, onto the drive.
CLIP_URL = ("https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
            "onnx/vision_model_quantized.onnx")
_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)
_SAME_HASH = 10            # difference-hash distance ≤ this → the same picture
_MAX_ROUND = 160           # thumbnails scored per search round
_RECHECK = 16              # the best this many are re-checked at full size
_SAME_FEATURES = 25        # OpenCV detail matches that agree on one placement → same picture
# Labels for CLIP's similarity. Two DIFFERENT photos of the same kind of thing land ~0.7–0.85;
# CLIP almost never gives unrelated pictures more than ~0.5 — a raw "70%" undersells a match.
_LABELS = ((0.95, "near-identical"), (0.88, "very close"), (0.78, "similar"), (0.0, "related"))
_sess = {"s": None, "in": None, "out": None}
_lock = threading.Lock()


# ── the CLIP image encoder ──────────────────────────────────────────────────
def _download_model():
    import s32_config as _cfg
    if _cfg.airplane():
        raise RuntimeError("the image-matching model isn't downloaded yet, and airplane mode is on")
    import requests
    from s32_web import _proxies, _UA, _log_request
    os.makedirs(_DIR, exist_ok=True)
    part = _PATH + ".part"
    log("[S32][SIMILAR] downloading the image-matching model (CLIP, one-time, ~90 MB) onto the drive…")
    with requests.get(CLIP_URL, headers={"User-Agent": _UA}, proxies=_proxies(), timeout=60,
                      stream=True, allow_redirects=True) as r:
        _log_request("GET", CLIP_URL, r.status_code, "CLIP model download")
        r.raise_for_status()
        got = 0
        with open(part, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                got += len(chunk)
                if got > 700 * 2**20:
                    raise RuntimeError("the model download is far bigger than expected")
                f.write(chunk)
    if got < 20 * 2**20:
        os.remove(part)
        raise RuntimeError(f"the model download was too small ({got} bytes)")
    os.replace(part, _PATH)
    log(f"[S32][SIMILAR] image-matching model ready ({got / 2**20:.0f} MB)")


def _session():
    """onnxruntime session for the CLIP image encoder (loaded once). Few threads, so the
    voice (also onnxruntime, on the CPU) doesn't stutter while it scores."""
    with _lock:
        if _sess["s"] is not None:
            return _sess
        if not os.path.exists(_PATH):
            _download_model()
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 4) // 4))
        s = ort.InferenceSession(_PATH, sess_options=so, providers=["CPUExecutionProvider"])
        ins = s.get_inputs()
        outs = [o.name for o in s.get_outputs()]
        # Read the names instead of assuming them: 'pixel_values' → 'image_embeds' on the
        # usual export; otherwise the first input / a pooled first output.
        _sess.update(s=s, inp=next((i.name for i in ins if "pixel" in i.name), ins[0].name),
                     out=("image_embeds" if "image_embeds" in outs else outs[0]))
        return _sess




def _square(img):
    """The WHOLE picture on a square (grey bars) — the centre crop cuts the sides off wide
    photos, and a copy cropped differently then looks less alike than it is."""
    from PIL import Image
    w, h = img.size
    s = max(w, h)
    out = Image.new("RGB", (s, s), (127, 127, 127))
    out.paste(img, ((s - w) // 2, (s - h) // 2))
    return out


def _prep(img):
    """PIL image → CLIP input (3×224×224, normalised): shortest side 224, centre crop."""
    import numpy as np
    from PIL import Image
    img = img.convert("RGB")
    w, h = img.size
    s = 224 / min(w, h)
    img = img.resize((max(224, round(w * s)), max(224, round(h * s))), Image.BICUBIC)
    w, h = img.size
    l, t = (w - 224) // 2, (h - 224) // 2
    img = img.crop((l, t, l + 224, t + 224))
    a = np.asarray(img, dtype="float32") / 255.0
    a = (a - np.array(_MEAN, dtype="float32")) / np.array(_STD, dtype="float32")
    return a.transpose(2, 0, 1)


def embed(images, whole=False):
    """[PIL images] → unit vectors (n × d). whole=True: the whole picture, not the centre."""
    import numpy as np
    S = _session()
    out = []
    for i in range(0, len(images), 16):
        batch = np.stack([_prep(_square(im) if whole else im) for im in images[i:i + 16]]).astype("float32")
        o = S["s"].run([S["out"]], {S["inp"]: batch})[0]
        if o.ndim == 3:                       # hidden states → the first (class) token
            o = o[:, 0, :]
        out.append(o.astype("float32"))
    out = np.concatenate(out)
    return out / (np.linalg.norm(out, axis=1, keepdims=True) + 1e-9)


class _Query:
    """The operator's picture, fingerprinted once."""
    def __init__(self, img):
        self.img = img
        self.vc = embed([img])[0]
        self.vw = embed([img], whole=True)[0]
        self.h = _dhash(img)
        self.hw = _dhash(_square(img))

    def score(self, ims):
        """→ [(similarity, same_by_hash)] for PIL images: the better of centre/whole views."""
        import numpy as np
        if not ims:
            return []
        c = embed(ims) @ self.vc
        w = embed(ims, whole=True) @ self.vw
        out = []
        for im, a, b in zip(ims, c, w):
            same = (_hamming(self.h, _dhash(im)) <= _SAME_HASH
                    or _hamming(self.hw, _dhash(_square(im))) <= _SAME_HASH)
            out.append((float(max(a, b)), same))
        return out


# ── difference hash (same as the visual indexer) ────────────────────────────
def _dhash(img, size=8):
    g = img.convert("L").resize((size + 1, size))
    px = list(g.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (1 if px[row * (size + 1) + col] > px[row * (size + 1) + col + 1] else 0)
    return bits


def _hamming(a, b):
    return bin(a ^ b).count("1")


# ── the search ──────────────────────────────────────────────────────────────
def _load_query(image_ref, img_bytes):
    """→ (PIL image, label) or raise. The attached image first, else a reference / path."""
    from PIL import Image
    if img_bytes:
        return Image.open(io.BytesIO(img_bytes)).convert("RGB"), "the attached image", None
    if image_ref:
        from s32_dispatch import resolve_image_ref
        path, how = resolve_image_ref(image_ref)
        if not path:
            raise ValueError(how)
        return Image.open(path).convert("RGB"), os.path.basename(path), path
    raise ValueError("Which image? Attach one, or name it (e.g. '#1', 'last', or a file name).")


def _phrases_from_vision(img):
    """For an image the model hasn't seen (a file on disk): its own vision writes 2–3
    search phrases. Local — the model runs on this computer."""
    try:
        import base64
        import s32_llm
        buf = io.BytesIO()
        im = img.copy()
        im.thumbnail((768, 768))
        im.save(buf, format="JPEG", quality=85)
        payload = {
            "model": CONFIG["lmstudio_model"],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": ("Write 3 short web image-search phrases (3-7 words each) that "
                                          "would find pictures like this one: the main subject, its "
                                          "setting, its style. One per line. No numbering, no quotes.")},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                                                    + base64.b64encode(buf.getvalue()).decode()}}]}],
            "tool_choice": "none", "max_tokens": 80, "temperature": 0.3, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        resp = s32_llm.http_post(f"{CONFIG['lmstudio_host']}/v1/chat/completions", payload, timeout=90)
        text = (resp["choices"][0]["message"].get("content") or "")
        text = re.sub(r"(?s)<think>.*?</think>", "", text)
        return [l.strip(" -•*\"'") for l in text.splitlines() if 2 < len(l.strip()) < 80][:3]
    except Exception as e:
        log(f"[S32][SIMILAR] couldn't describe the image: {e}")
        return []



def _name_hint(path):
    """A file's own name, when it's a real name ('kodiak-bear-katmai-2019.jpg' →
    'kodiak bear katmai 2019'), not a camera/download default ('IMG_2041', 'image (3)')."""
    if not path:
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    words = [w for w in re.split(r"[\W_]+", stem) if w]
    alpha = [w for w in words if re.fullmatch(r"[A-Za-z]{3,}", w)]
    generic = {"img", "image", "images", "photo", "pic", "picture", "screenshot", "download",
               "downloads", "untitled", "file", "copy", "dsc", "dcim", "pxl", "mvimg", "unnamed",
               "capture", "snip", "edit", "edited", "final", "new", "jpeg", "jpg", "png", "webp"}
    if len([w for w in alpha if w.lower() not in generic]) < 2:
        return None
    return " ".join(words)[:100]


_SITE_JUNK = re.compile(
    r"\s*(?:[-|–—:·•]\s*)?(?:stock (?:photo|image|picture)s?|royalty[- ]free.*|free (?:download|image)s?|"
    r"hd wallpapers?|wallpapers?|images? & pictures|on pinterest|pinterest|reddit|flickr|"
    r"wikimedia commons|wikipedia|shutterstock|getty ?images|istock|alamy|adobe stock|unsplash|"
    r"pexels|pixabay|dreamstime|123rf|deviantart|artstation|imgur|tumblr|facebook|instagram|"
    r"twitter|x\.com|youtube|amazon\.com|ebay|etsy)\b.*$", re.I)


def _title_phrase(title):
    """A result's title → a clean search phrase (site names and 'stock photo' stripped)."""
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    t = re.sub(r"\s*[|–—]\s*[^|–—]{0,40}$", "", t) if len(t) > 30 else t   # ' | SiteName'
    t = _SITE_JUNK.sub("", t).strip(" -|–—:·•,.")
    t = re.sub(r"\b(?:stock )?(?:photo|image|picture|vector|illustration) (?:of|showing)\b", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:90] if len(re.findall(r"[A-Za-z]{3,}", t)) >= 2 else ""


def _gather(queries, pages=(1, 2), seen=None):
    """Words → candidate pictures from EVERY source, no relevance filter (the pixels decide).
    → list of {image, thumb, title, source}."""
    from s32_web import _run_engines, _IMAGE_PRIMARY, _IMAGE_BACKUP, _resting
    seen = set() if seen is None else seen
    engines = [(n, f) for n, f in tuple(_IMAGE_PRIMARY) + tuple(_IMAGE_BACKUP)
               if _resting.get(n, 0) <= time.time()]
    out, per_engine, answered = [], {}, 0
    for q in queries:
        for page in pages:
            try:
                got = _run_engines(engines, q, 50, page)
            except Exception as e:
                log(f"[S32][SIMILAR] search {q!r} failed: {e}")
                continue
            for name, (res, _err) in got.items():
                answered += len(res or [])
                for r in res or []:
                    u = (r.get("image") or "").strip()
                    k = u.rstrip("/").lower()
                    if not u or k in seen:
                        continue
                    seen.add(k)
                    per_engine[name] = per_engine.get(name, 0) + 1
                    out.append({"image": u, "thumb": r.get("thumb") or u,
                                "title": r.get("title") or "", "source": r.get("source") or ""})
    log(f"[S32][SIMILAR]   {len(out)} new picture(s) for {queries} — "
        + (", ".join(f"{n} {c}" for n, c in sorted(per_engine.items(), key=lambda x: -x[1]))
           or ("all already seen" if answered else "no source answered")))
    return out


def _fetch(cands, key="thumb"):
    """Download candidate pictures in parallel (through the web layer) → [(cand, PIL)]."""
    from concurrent.futures import ThreadPoolExecutor
    from PIL import Image
    from s32_web import fetch_image

    def one(c):
        body, _ct, err = fetch_image(c.get(key) or c.get("image"))
        if err or not body:
            return None
        try:
            im = Image.open(io.BytesIO(body))
            im.load()
            return c, im.convert("RGB")
        except Exception:
            return None
    with ThreadPoolExecutor(max_workers=12) as ex:
        return [r for r in ex.map(one, cands) if r]


def _same_by_features(a, b):
    """Detail-by-detail match (OpenCV ORB + RANSAC): how many matching details agree on
    one placement. The same picture cropped, bordered, resized or with text added scores
    high; two different photos of the same thing don't. 0 when OpenCV isn't there."""
    try:
        import cv2
        import numpy as np
    except Exception:
        return 0

    def gray(im):
        im = im.copy()
        im.thumbnail((800, 800))
        return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
    try:
        orb = cv2.ORB_create(1500)
        ka, da = orb.detectAndCompute(gray(a), None)
        kb, db = orb.detectAndCompute(gray(b), None)
        if da is None or db is None or len(ka) < 10 or len(kb) < 10:
            return 0
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(da, db, k=2)
        good = [m for m_n in pairs if len(m_n) == 2
                for m, n in [m_n] if m.distance < 0.75 * n.distance]
        if len(good) < 10:
            return 0
        src = np.float32([ka[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kb[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        _H, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        return int(mask.sum()) if mask is not None else 0
    except Exception:
        return 0


def _label(sim, same):
    if same:
        return "same image"
    return next(name for floor, name in _LABELS if sim >= floor)


def find_similar_images(image=None, queries=None, max_results=12, img_bytes=None):
    """The tool. image: '#1' / 'last' / a file name / a path (or the attached image);
    queries: the search phrases (the model writes them from what it sees)."""
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    t0 = time.time()
    try:
        q_img, label, q_path = _load_query(image, img_bytes)
    except Exception as e:
        return {"error": str(e)}
    if isinstance(queries, str):
        queries = [q for q in re.split(r"[\n;|]+", queries) if q.strip()]
    queries = [str(q).strip()[:100] for q in (queries or []) if str(q).strip()][:4]
    if not queries:
        queries = _phrases_from_vision(q_img)
    hint = _name_hint(q_path)
    if hint and hint.lower() not in (q.lower() for q in queries):
        queries.append(hint)
    if not queries:
        return {"error": "I couldn't put the image into search words — say in a few words what's in "
                         "it and I'll search with those."}
    try:
        max_results = max(1, min(int(max_results or 12), 30))
    except Exception:
        max_results = 12
    log(f"[S32][SIMILAR] finding images like {label} — round 1: {' | '.join(queries)}")
    try:
        Q = _Query(q_img)
    except Exception as e:
        log(f"[S32][SIMILAR] image matching unavailable: {e}")
        return {"error": f"The image-matching model couldn't run: {str(e)[:160]}"}

    seen, scored, used = set(), {}, list(queries)

    def score_round(cands):
        got = _fetch(cands[:_MAX_ROUND])
        for (c, _im), (sim, same) in zip(got, Q.score([im for _c, im in got])):
            scored[c["image"]] = {"c": c, "sim": sim, "same": same, "feat": 0}
        return len(got)

    # 1. the model's words (+ the file's name)
    checked = score_round(_gather(queries, seen=seen))

    # 2. the words the web uses for the closest matches
    best = sorted(scored.values(), key=lambda x: (x["same"], x["sim"]), reverse=True)[:8]
    more = []
    for x in best:
        if x["sim"] < 0.75 and not x["same"]:
            break
        ph = _title_phrase(x["c"].get("title"))
        if ph and ph.lower() not in (u.lower() for u in used + more):
            more.append(ph)
        if len(more) >= 3:
            break
    if more:
        log(f"[S32][SIMILAR] round 2 (from the closest matches' titles): {' | '.join(more)}")
        used += more
        checked += score_round(_gather(more, pages=(1,), seen=seen))
    if not scored:
        return {"error": f"Image search came back empty for {used} — the search sources found "
                         f"nothing that could be downloaded right now. Tell the operator."}

    # 3. the best few at full size: CLIP again + the detail-by-detail check
    top = sorted(scored.values(), key=lambda x: (x["same"], x["sim"]), reverse=True)[:_RECHECK]
    full = _fetch([x["c"] for x in top], key="image")
    if full:
        by = {c["image"]: im for c, im in full}
        ims = [by[x["c"]["image"]] for x in top if x["c"]["image"] in by]
        xs = [x for x in top if x["c"]["image"] in by]
        for x, im, (sim, same) in zip(xs, ims, Q.score(ims)):
            x["sim"] = max(x["sim"], sim)
            x["feat"] = _same_by_features(q_img, im)
            x["same"] = x["same"] or same or x["feat"] >= _SAME_FEATURES
    ranked = sorted(scored.values(), key=lambda x: (x["same"], x["sim"]), reverse=True)[:max_results]

    # 4. return
    from s32_web import _push_image_grid
    shown = [dict(x["c"], title=f"{_label(x['sim'], x['same'])} · {x['c'].get('title') or ''}".strip(" ·"))
             for x in ranked]
    _push_image_grid(f"images like {label}", shown)
    n_same = sum(1 for x in ranked if x["same"])
    log(f"[S32][SIMILAR] {checked} web pictures compared in {time.time() - t0:.0f}s — "
        + (f"{n_same} copy/copies of the same image" if n_same else
           f"no exact copy; best {_label(ranked[0]['sim'], False)} ({ranked[0]['sim']:.2f})" if ranked else "nothing"))
    return {"type": "similar_images_result", "image": label, "queries": used,
            "checked": checked, "count": len(ranked), "exact": n_same,
            "results": [{"title": x["c"].get("title", ""), "image": x["c"].get("image", ""),
                         "page": x["c"].get("source", ""), "score": round(x["sim"], 3),
                         "label": _label(x["sim"], x["same"]), "same_image": bool(x["same"])}
                        for x in ranked]}


# ═════════════════════════════════════════════════════════════════════════════
#  ON THIS COMPUTER (2026-11-08) — the same matching, turned inward: which of the
#  operator's own pictures look like this one, or which are duplicates of each other.
#  Nothing goes online (except the one-time model download). Each picture is
#  fingerprinted once; the fingerprints stay on the drive (System 32's private folder,
#  never indexed or uploaded), so the next search is instant.
# ═════════════════════════════════════════════════════════════════════════════
_FP_VERSION = 1
_LOCAL_EXT = {".png", ".jpg", ".jpeg", ".jfif", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
_MIN_BYTES = 12_000          # smaller = icons, emoji, UI bits
_MIN_SIDE = 96
# Computer-wide only: folders full of program / game art, not the operator's pictures.
_JUNK_DIR = re.compile(
    r"[\\/](?:appdata|program files(?: \(x86\))?|programdata|windows|steamapps|steamlibrary|"
    r"epic games|node_modules|site-packages|\.git|\$recycle\.bin|system volume information|"
    r"cache|caches|temp|tmp|thumbnails|icons?|\.venv|venv|__pycache__)[\\/]", re.I)
_DUP_SIM = 0.96              # duplicates mode: this alike (or the same by hash) = one group
_CLOSENESS = {"same": 2.0, "near-identical": 0.95, "very close": 0.88, "similar": 0.78, "related": 0.65}
_scan = {"cancel": False}
_fp_lock = threading.Lock()


def _fp_path():
    from s32_config import PERSON_DIR
    return os.path.join(PERSON_DIR, "image_fingerprints.db")


def _fp_db():
    import sqlite3
    p = _fp_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=30, check_same_thread=False)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS fp(path TEXT PRIMARY KEY, size INTEGER, mtime REAL,
                 w INTEGER, h INTEGER, dh TEXT, dhw TEXT, vc BLOB, vw BLOB, v INTEGER)""")
    return c


def _open_local(path):
    """A picture from disk, decoded small (JPEG draft mode — a 24 MP photo decodes in a
    fraction of the time). → (PIL RGB, (w, h) of the original) or None."""
    from PIL import Image
    try:
        im = Image.open(path)
        size = im.size
        if min(size) < _MIN_SIDE:
            return None
        if getattr(im, "format", "") == "JPEG":
            im.draft("RGB", (448, 448))
        im.seek(0)
        im = im.convert("RGB")
        im.thumbnail((640, 640))
        return im, size
    except Exception:
        return None


def _local_candidates(folders, recursive=True):
    """→ [(path, size, mtime)] of pictures to compare. folders=[] → every drive (the file
    index), skipping program/game/system folders."""
    out = []
    if folders:
        for root in folders:
            root = os.path.expandvars(os.path.expanduser(str(root).strip().strip('"')))
            if os.path.isfile(root):
                try:
                    st = os.stat(root)
                    out.append((root, st.st_size, st.st_mtime))
                except OSError:
                    pass
                continue
            if not os.path.isdir(root):
                continue
            stack = [root]
            while stack:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        for e in it:
                            try:
                                if e.is_dir(follow_symlinks=False):
                                    if recursive:
                                        stack.append(e.path)
                                elif os.path.splitext(e.name)[1].lower() in _LOCAL_EXT:
                                    st = e.stat()
                                    if st.st_size >= _MIN_BYTES:
                                        out.append((e.path, st.st_size, st.st_mtime))
                            except OSError:
                                pass
                except OSError:
                    pass
        return out
    import s32_findex
    try:
        c = s32_findex._connect()
        rows = c.execute("SELECT path, size, mtime FROM files WHERE kind='image'").fetchall()
        c.close()
    except Exception as e:
        log(f"[S32][SIMILAR] file index unreadable: {e}")
        rows = []
    if not rows:             # index not built yet → the usual folders
        return _local_candidates([r for r in s32_findex._usual_roots() if r and os.path.isdir(r)], True)
    return [(p, s or 0, m or 0) for p, s, m in rows
            if os.path.splitext(p)[1].lower() in _LOCAL_EXT and (s or 0) >= _MIN_BYTES
            and not _JUNK_DIR.search(p)]


def _fingerprints(cands):
    """[(path, size, mtime)] → {path: (vc, vw, dh, dhw, (w, h))}, fingerprinting only what's
    new or changed. Shows progress on the input bar; the ✕ stops it (what's done is kept)."""
    import numpy as np
    from concurrent.futures import ThreadPoolExecutor
    out, todo = {}, []
    with _fp_lock:
        c = _fp_db()
        have = {}
        for i in range(0, len(cands), 900):
            chunk = cands[i:i + 900]
            q = "SELECT path,size,mtime,w,h,dh,dhw,vc,vw,v FROM fp WHERE path IN (%s)" % ",".join("?" * len(chunk))
            for row in c.execute(q, [p for p, _s, _m in chunk]):
                have[row[0]] = row
        for p, s, m in cands:
            r = have.get(p)
            if r and r[1] == s and abs((r[2] or 0) - m) < 2 and r[9] == _FP_VERSION:
                out[p] = (np.frombuffer(r[7], dtype="float16").astype("float32"),
                          np.frombuffer(r[8], dtype="float16").astype("float32"),
                          int(r[5], 16), int(r[6], 16), (r[3], r[4]))
            else:
                todo.append((p, s, m))
        c.close()
    if not todo:
        return out
    _session()                       # model download (once) before the bar starts counting
    _scan["cancel"] = False
    t0 = time.time()
    state["scanning"] = {"done": 0, "total": len(todo), "started": t0}
    log(f"[S32][SIMILAR] fingerprinting {len(todo):,} picture(s) on this computer "
        f"({len(out):,} already done) — once; next time it's instant")
    done = 0
    try:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for i in range(0, len(todo), 32):
                if _scan["cancel"]:
                    log(f"[S32][SIMILAR] stopped by the operator after {done:,} picture(s) — they're kept")
                    break
                part = todo[i:i + 32]
                opened = [(t, r) for t, r in zip(part, ex.map(lambda t: _open_local(t[0]), part)) if r]
                rows = []
                if opened:
                    ims = [r[0] for _t, r in opened]
                    vc, vw = embed(ims), embed(ims, whole=True)
                    for (t, (im, size)), a, b in zip(opened, vc, vw):
                        dh, dhw = _dhash(im), _dhash(_square(im))
                        out[t[0]] = (a, b, dh, dhw, size)
                        rows.append((t[0], t[1], t[2], size[0], size[1], f"{dh:016x}", f"{dhw:016x}",
                                     a.astype("float16").tobytes(), b.astype("float16").tobytes(), _FP_VERSION))
                if rows:
                    with _fp_lock:
                        c = _fp_db()
                        c.executemany("INSERT OR REPLACE INTO fp VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
                        c.commit()
                        c.close()
                done += len(part)
                state["scanning"] = {"done": done, "total": len(todo), "started": t0}
    finally:
        state.pop("scanning", None)
    log(f"[S32][SIMILAR] fingerprinted {done:,} in {time.time() - t0:.0f}s")
    return out


def cancel_scan():
    _scan["cancel"] = True
    return {"ok": True, "cancelled": "scan"}


def scan_bar():
    """For the input-area bar (system32._loading_bar)."""
    s = state.get("scanning")
    if not s:
        return None
    n, of = s.get("done", 0), max(1, s.get("total", 1))
    el = time.time() - s.get("started", time.time())
    tail = ""
    if n >= 64:
        left = el / n * (of - n)
        tail = f"  · about {round(left / 60)} min left" if left >= 90 else f"  · about {round(left)}s left"
    return {"kind": "scan", "pct": int(n * 100 / of),
            "label": f"🔍 Looking through your pictures — {n:,} of {of:,}{tail}  · first time only"}


def _local_card(path, title):
    import s32_findex
    return dict(s32_findex.img_card({"path": path, "name": os.path.basename(path),
                                     "dir": os.path.dirname(path)}), title=title)


def find_similar_on_computer(image=None, folders=None, mode="similar", closeness="similar",
                             recursive=True, max_results=40, img_bytes=None):
    """The tool. image (attachment / '#1' / a path) + folders (none = every drive) →
    the operator's own pictures that look like it. mode='duplicates' (no image needed):
    groups of copies / near-copies inside those folders."""
    t0 = time.time()
    if isinstance(folders, str):
        folders = [f for f in re.split(r"[\n;|]+", folders) if f.strip()]
    folders = [str(f).strip() for f in (folders or []) if str(f).strip()]
    mode = "duplicates" if str(mode or "").lower().startswith("dup") else "similar"
    try:
        max_results = max(1, min(int(max_results or 40), 200))
    except Exception:
        max_results = 40
    q_img = q_path = None
    if mode == "similar":
        try:
            q_img, label, q_path = _load_query(image, img_bytes)
        except Exception as e:
            return {"error": str(e)}
    else:
        label = "duplicates"
    cands = _local_candidates(folders, recursive)
    if q_path:
        qn = os.path.normcase(os.path.abspath(q_path))
        cands = [c for c in cands if os.path.normcase(os.path.abspath(c[0])) != qn]
    where = ", ".join(folders) if folders else "every drive"
    if not cands:
        return {"error": f"No pictures found in {where}" + (" (folders that don't exist are skipped)" if folders else "")}
    if mode == "duplicates" and len(cands) > 40000:
        return {"error": f"{len(cands):,} pictures is too many to cross-check at once — pick a folder "
                         f"(or a few) to look for duplicates in."}
    log(f"[S32][SIMILAR] on this computer: {mode} — {len(cands):,} picture(s) in {where}")
    try:
        fps = _fingerprints(cands)
        Q = _Query(q_img) if q_img is not None else None
    except Exception as e:
        log(f"[S32][SIMILAR] image matching unavailable: {e}")
        return {"error": f"The image-matching model couldn't run: {str(e)[:160]}"}
    if not fps:
        return {"error": f"None of the {len(cands):,} pictures in {where} could be opened."}
    import numpy as np
    paths = list(fps)
    VC = np.stack([fps[p][0] for p in paths])
    VW = np.stack([fps[p][1] for p in paths])
    stopped = bool(_scan.get("cancel"))
    results = []

    if Q is not None:
        sims = np.maximum(VC @ Q.vc, VW @ Q.vw)
        floor = _CLOSENESS.get(str(closeness or "similar").lower(), 0.78)
        for i in np.argsort(-sims):
            p, s = paths[i], float(sims[i])
            same = (_hamming(Q.h, fps[p][2]) <= _SAME_HASH or _hamming(Q.hw, fps[p][3]) <= _SAME_HASH)
            if not same and s < min(floor, 1.0):
                if s < 0.65:
                    break
                continue
            results.append({"path": p, "score": round(s, 3), "same_image": same, "label": _label(s, same)})
        results.sort(key=lambda r: (r["same_image"], r["score"]), reverse=True)
        if floor > 1.0:                      # closeness='same' → copies only
            results = [r for r in results if r["same_image"]]
        total = len(results)
        results = results[:max_results]
        title = f"💻 pictures like {label}"
    else:
        # groups: union-find over pairs that are the same picture / nearly the same shot
        par = list(range(len(paths)))

        def find(a):
            while par[a] != a:
                par[a] = par[par[a]]
                a = par[a]
            return a
        for i in range(0, len(paths), 1024):
            S = VC[i:i + 1024] @ VC.T
            for a, b in zip(*np.nonzero(S >= _DUP_SIM)):
                a += i
                if a < b:
                    par[find(a)] = find(b)
        # Same hash and still clearly alike (a recolour / filter CLIP marks down). Hash
        # alone isn't enough: every flat or blank picture hashes alike.
        buckets = {}
        for i, p in enumerate(paths):
            buckets.setdefault(fps[p][2], []).append(i)
        for ids in buckets.values():
            for j in ids[1:]:
                if float(VW[j] @ VW[ids[0]]) >= 0.90:
                    par[find(j)] = find(ids[0])
        groups = {}
        for i in range(len(paths)):
            groups.setdefault(find(i), []).append(i)
        groups = [g for g in groups.values() if len(g) > 1]

        def px(i):
            w, h = fps[paths[i]][4]
            return (w or 0) * (h or 0)
        mt = {p: m for p, _s, m in cands}
        groups.sort(key=len, reverse=True)
        total = sum(len(g) for g in groups)
        for n, g in enumerate(groups, 1):
            # keep: the most pixels, then the oldest (the original, usually)
            g.sort(key=lambda i: (-px(i), mt.get(paths[i], 0)))
            k = g[0]
            for i in g:
                same = i == k or _hamming(fps[paths[i]][2], fps[paths[k]][2]) <= _SAME_HASH
                results.append({"path": paths[i], "group": n, "keep": i == k,
                                "label": "keep" if i == k else ("same image" if same else "near-identical"),
                                "size": fps[paths[i]][4], "same_image": bool(same)})
        results = results[:max(max_results, 2)]
        title = f"💻 duplicates in {where}"

    state["last_similar_local"] = [r["path"] for r in results]
    if results:
        import s32_web
        cards = []
        for r in results:
            name = os.path.basename(r["path"])
            tag = (f"group {r['group']} · {'✓ keep' if r['keep'] else r['label']}" if "group" in r
                   else r["label"])
            cards.append(_local_card(r["path"], f"{tag} · {name}"))
        s32_web._push({"type": "image_grid", "query": title, "images": cards, "sid": "", "more": False})
    log(f"[S32][SIMILAR] on this computer: {len(fps):,} compared in {time.time() - t0:.0f}s — "
        f"{total} match(es)" + (" (scan stopped early)" if stopped else ""))
    return {"type": "similar_local_result", "mode": mode, "image": label, "where": where,
            "checked": len(fps), "total": total, "count": len(results), "stopped": stopped,
            "groups": len({r.get("group") for r in results if "group" in r}),
            "results": results}
