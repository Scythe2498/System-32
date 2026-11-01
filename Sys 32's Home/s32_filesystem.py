# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_filesystem.py — All filesystem tools: list, find, read, show images,
                    drives, visual search, index search, explore tree,
                    memory read/write/search, favorites, directory map.
"""
from s32_config import (
    CONFIG, REFERENCE_DIR, PERSON_DIR, PEOPLE_DIR, MEMORY_FILE, MEMORY_RECENT, MEMORY_ARCHIVE,
    MEMORY_INDEX, WORKFLOW_DIR, _script_dir, TEXT_EXTS,
    _machine_name, _current_operator, _session_start,
    state, log, http_get, is_up,
    _result_lock, _push_result, _push_done,
    os, json, datetime, threading, time, random, re, shutil, urllib, string,
    subprocess,
    PILImage,
)
from s32_memory import (
    _ensure_memory_structure, _ensure_reference_dir, _load_operator,
)
def _dhash(image, hash_size=8):
    """Perceptual difference hash - fast visual fingerprint, no GPU needed."""
    image = image.convert("L").resize((hash_size + 1, hash_size), resample=1)
    pixels = list(image.getdata())
    h = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left  = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            h = (h << 1) | (1 if left > right else 0)
    return h

def _hamming(h1, h2):
    xor = h1 ^ h2
    count = 0
    while xor:
        count += xor & 1
        xor >>= 1
    return count

def visual_search(query_path, top_n=12, threshold=15):
    """
    Find images visually similar to query_path using perceptual hashing.
    threshold: 0=identical, 64=completely different. 15 is a good default.
    """
    if PILImage is None:
        return {"error": "PIL not available - install pillow"}

    out_images = CONFIG.get("out_images", "")
    visual_index_path = os.path.join(os.path.dirname(out_images), "visual_index.json")

    # Resolve query path
    if not os.path.isabs(query_path):
        candidate = os.path.join(out_images, query_path)
        if os.path.isfile(candidate):
            query_path = candidate

    if not os.path.isfile(query_path):
        return {"error": f"Query image not found: {query_path}"}

    # Load visual index
    if not os.path.exists(visual_index_path):
        return {
            "error": "Visual index not built yet.",
            "fix": f"Run: python visual_index.py --build --dir \"{out_images}\""
        }

    try:
        with open(visual_index_path, encoding="utf-8") as f:
            index = json.load(f)
    except Exception as e:
        return {"error": f"Could not load visual index: {e}"}

    # Hash the query image
    try:
        with PILImage.open(query_path) as img:
            query_hash = _dhash(img)
    except Exception as e:
        return {"error": f"Could not read query image: {e}"}

    # Search
    results = []
    query_stem = os.path.splitext(os.path.basename(query_path))[0]
    for key, entry in index.items():
        if key == query_stem:
            continue  # skip self
        try:
            stored_hash = int(entry["hash"], 16)
            dist = _hamming(query_hash, stored_hash)
            if dist <= threshold:
                results.append({
                    "path":       entry["path"],
                    "filename":   os.path.basename(entry["path"]),
                    "similarity": round((1 - dist / 64) * 100, 1),
                    "distance":   dist,
                })
        except Exception:
            pass

    results.sort(key=lambda x: x["distance"])
    results = results[:top_n]

    if not results:
        return {
            "message": f"No visually similar images found within threshold {threshold}.",
            "tip": "Try increasing threshold (max 64) for looser matching."
        }

    return {
        "query":   query_path,
        "results": results,
        "count":   len(results),
        "paths":   [r["path"] for r in results],
    }
_visual_index_lock = threading.Lock()
_VIS_EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _visual_index_file():
    out_images = CONFIG.get("out_images", "")
    base = os.path.dirname(out_images) if out_images else REFERENCE_DIR
    return os.path.join(base, "visual_index.json")


def index_image(path):
    """Incrementally add ONE image to the visual index — idempotent, thread-safe,
    atomic write. Returns True if newly added/updated. Used so every image it
    shows in chat or writes to the output folders feeds visual_search over time
    without a manual rebuild."""
    if PILImage is None or not path:
        return False
    try:
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return False
        if os.path.splitext(path)[1].lower() not in _VIS_EXTS:
            return False
        key = os.path.splitext(os.path.basename(path))[0]
        with _visual_index_lock:
            vip = _visual_index_file()
            index = {}
            if os.path.exists(vip):
                try:
                    with open(vip, encoding="utf-8") as f:
                        index = json.load(f)
                except Exception:
                    index = {}
            ex = index.get(key)
            if ex and ex.get("path") == path:
                return False                      # already indexed, same file
            with PILImage.open(path) as img:
                h = _dhash(img)
                w, ht = img.size
            index[key] = {"path": path, "hash": format(h, "016x"), "width": w, "height": ht}
            tmp = vip + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2)
            os.replace(tmp, vip)                  # atomic — never corrupts the index
        return True
    except Exception as e:
        log(f"[S32] index_image failed: {str(e)[:80]}")
        return False


def _visual_index_watch_loop(interval=45):
    """Daemon: periodically index any new images in the output folder, so anything
    generated or saved there is searchable without a manual build_visual_index."""
    while True:
        try:
            out_images = CONFIG.get("out_images", "")
            if out_images and os.path.isdir(out_images):
                added = 0
                for e in os.scandir(out_images):
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in _VIS_EXTS:
                        if index_image(e.path):
                            added += 1
                if added:
                    log(f"[S32] Visual index: +{added} new output image(s) auto-indexed.")
        except Exception as ex:
            log(f"[S32] visual index watch error: {str(ex)[:80]}")
        time.sleep(interval)


def start_visual_indexer():
    threading.Thread(target=_visual_index_watch_loop, daemon=True).start()
    log("[S32] Visual indexer started — output images auto-index for visual_search.")


def wipe_visual_index():
    """Delete the visual (image) index. Used by the operator's full memory reset;
    the auto-indexer rebuilds it from new output images afterward."""
    try:
        with _visual_index_lock:
            vip = _visual_index_file()
            if os.path.exists(vip):
                os.remove(vip)
        log("[S32] Visual index wiped.")
        return True
    except Exception as e:
        log(f"[S32] visual index wipe failed: {str(e)[:80]}")
        return False


def build_visual_index(force=False):
    """Build or update the visual hash index for the output folder."""
    if PILImage is None:
        return {"error": "PIL not available - install pillow"}

    out_images = CONFIG.get("out_images", "")
    visual_index_path = os.path.join(os.path.dirname(out_images), "visual_index.json")

    if not os.path.exists(out_images):
        return {"error": f"Output folder not found: {out_images}"}

    # Load existing
    index = {}
    if not force and os.path.exists(visual_index_path):
        try:
            with open(visual_index_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            pass

    image_exts = {".png", ".jpg", ".jpeg", ".webp"}
    new_count = 0
    errors = 0

    try:
        files = [e for e in os.scandir(out_images)
                 if e.is_file() and os.path.splitext(e.name)[1].lower() in image_exts]
    except Exception as e:
        return {"error": str(e)}

    log(f"[S32] Building visual index: {len(files)} images to scan...")
    for entry in files:
        key = os.path.splitext(entry.name)[0]
        if key in index and not force:
            continue
        try:
            with PILImage.open(entry.path) as img:
                h = _dhash(img)
                w, ht = img.size
            index[key] = {
                "path":   entry.path,
                "hash":   format(h, '016x'),
                "width":  w,
                "height": ht,
            }
            new_count += 1
        except Exception:
            errors += 1

    try:
        with open(visual_index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2)
    except Exception as e:
        return {"error": f"Could not save index: {e}"}

    log(f"[S32] Visual index built: {new_count} new, {errors} errors, {len(index)} total")
    return {
        "status":    "done",
        "new":       new_count,
        "total":     len(index),
        "errors":    errors,
        "saved_to":  visual_index_path,
    }
    """
    Save an image to its favorites list with an optional reason.
    Persists to favorites.json in the reference folder.
    """
    _ensure_reference_dir()
    favorites_path = os.path.join(PERSON_DIR, "favorites.json")

    # Load existing favorites
    try:
        with open(favorites_path, "r", encoding="utf-8") as f:
            favorites = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        favorites = []

    # Resolve relative paths
    out_images = CONFIG.get("out_images", "")
    if not os.path.isabs(path):
        candidate = os.path.join(out_images, path)
        if os.path.isfile(candidate):
            path = candidate

    if not os.path.isfile(path):
        return {"error": f"File not found: {path}"}

    # Check if already favorited
    for entry in favorites:
        if entry.get("path") == path:
            entry["reason"] = reason or entry.get("reason", "")
            entry["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(favorites_path, "w", encoding="utf-8") as f:
                json.dump(favorites, f, indent=2)
            return {"status": "updated", "path": path}

    favorites.append({
        "path":    path,
        "filename": os.path.basename(path),
        "reason":  reason,
        "added":   datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })

    with open(favorites_path, "w", encoding="utf-8") as f:
        json.dump(favorites, f, indent=2)

    log(f"[S32] Favorite saved: {os.path.basename(path)}")
    return {"status": "saved", "path": path, "total_favorites": len(favorites)}


def smart_search(query, search_root=None, images_only=False):
    """
    Smart search dispatcher. If search_root is specified, searches there directly
    rather than trusting the index which may point elsewhere.
    """
    log(f"[S32] smart_search: '{query}' root={search_root}")
    results = {"query": query, "steps_taken": [], "found": [], "next_step": ""}

    # Shortcuts (.lnk/.url) used to be skipped as "not content" — but they now
    # resolve to real install paths (see _enrich_shortcuts), so a query-matching
    # shortcut is exactly what the user wants. Only .desktop (Linux) stays skipped.
    _SKIP_EXTS = {".desktop"}

    # If a specific root is given, walk it directly — do NOT consult the index
    # The index may have stale entries from other drives
    if search_root:
        # Resolve drive letter shorthand ("F" -> "F:\")
        resolved_root = search_root
        if len(search_root) == 1 and search_root.isalpha():
            resolved_root = search_root.upper() + ":\\"
        elif len(search_root) == 2 and search_root[1] == ":":
            resolved_root = search_root.upper() + "\\"

        # Walk the directory tree directly — bypasses the index entirely
        found_files = []
        q_lower = query.lower()
        try:
            for dirpath, dirnames, filenames in os.walk(resolved_root):
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext in _SKIP_EXTS:
                        continue
                    _IMG = {".png",".jpg",".jpeg",".webp",".gif",".bmp"}
                    _VID = {".mp4",".mov",".avi",".mkv",".webm"}
                    if images_only and ext not in _IMG and ext not in _VID:
                        continue
                    if q_lower in fname.lower():
                        found_files.append(os.path.join(dirpath, fname))
                    if len(found_files) >= 50:
                        break
                if len(found_files) >= 50:
                    break
        except PermissionError:
            pass
        except Exception as e:
            results["next_step"] = f"Error walking {resolved_root}: {e}"
            return results

        results["steps_taken"].append("walk_drive")
        if found_files:
            results["found"] = [{"path": p, "source": "walk"} for p in found_files[:20]]
            results["next_step"] = f"Found {len(found_files)} file(s) in {resolved_root}. Use show_file or show_images on results."
            log(f"[S32] smart_search: found {len(found_files)} in {resolved_root}")
            return results
        results["next_step"] = f"Nothing matching '{query}' found in {resolved_root}. The drive may use different folder names — try explore_tree to browse it."
        return results

    # No root specified - try index first
    idx = index_search(query)
    results["steps_taken"].append("index_search")
    if idx.get("best_match") and idx["best_match"].get("exists"):
        best_path = idx["best_match"]["path"]
        results["found"] = [{"path": best_path, "source": "index"}]
        results["next_step"] = f"Found in index: {best_path}. Call list_directory or show_images on this path."
        log(f"[S32] smart_search: found via index - {best_path}")
        return results

    if idx.get("fallback"):
        results["found"] = [{"path": r["path"], "source": "index_fallback"} for r in idx["fallback"]]
        results["next_step"] = f"Found {len(results['found'])} files via index fallback."
        return results

    # Try find_file broadly — filter out shortcuts
    pattern = f"*{query.replace(' ', '*')}*"
    ff = fs_find(pattern, "home", images_only=images_only)
    results["steps_taken"].append("find_file")
    ff_results = [r for r in ff.get("results", [])
                  if os.path.splitext(r["path"])[1].lower() not in _SKIP_EXTS]
    if ff_results:
        results["found"] = [{"path": r["path"], "source": "find_file"} for r in ff_results[:10]]
        results["next_step"] = f"Found {len(ff_results)} file(s) via find_file."
        return results

    # Last resort - explore output folder
    et = explore_tree("output", depth=2, images_only=images_only)
    results["steps_taken"].append("explore_tree")
    if et.get("image_paths"):
        results["found"] = [{"path": p, "source": "explore_tree"} for p in et["image_paths"][:10]]
        results["next_step"] = f"Found {len(et['image_paths'])} images in output folder."
        return results

    results["next_step"] = "Nothing found. Specify a search_root or try a different term."
    return results

def _levenshtein(s1, s2):
    """Calculate edit distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            current_row.append(min(
                previous_row[j + 1] + 1,
                current_row[j] + 1,
                previous_row[j] + (c1 != c2)
            ))
        previous_row = current_row
    return previous_row[-1]
def index_search(query):
    """
    Search index.md with fuzzy matching and fallback to direct file search.
    1. Exact word match (fast path)
    2. Fuzzy match with synonym expansion
    3. Direct find_file fallback if index returns nothing
    """
    index_path = os.path.join(REFERENCE_DIR, "index.md")
    if not os.path.exists(index_path):
        return {"error": "index.md not found - run bootstrap first"}

    try:
        with open(index_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"error": str(e)}

    # Synonym expansion for common queries
    _SYNONYMS = {
        "screenshot": ["screenshots", "screen capture", "capture", "snap"],
        "minecraft": ["mc", "mine craft"],
        "gallery": ["images", "pictures", "output", "generated"],
        "video": ["videos", "mp4", "animation", "clip"],
        "lora": ["loras", "model", "training"],
        "checkpoint": ["checkpoints", "model", "ckpt"],
        "dataset": ["datasets", "training data", "images"],
        "output": ["outputs", "generated", "results"],
        "reference": ["ref", "material", "docs"],
    }

    query_words = query.lower().split()

    # Expand query with synonyms
    expanded_words = set(query_words)
    for word in query_words:
        for key, variants in _SYNONYMS.items():
            if word == key or word in variants:
                expanded_words.update([key] + variants)

    matches = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("*"):
            continue

        line_lower = line.lower()

        # Score by exact word matches
        exact_score = sum(1 for w in expanded_words if w in line_lower)

        # Score by fuzzy match on individual words
        fuzzy_score = 0
        for qw in query_words:
            for lw in line_lower.split():
                if len(qw) > 3 and _levenshtein(qw, lw) <= 2:
                    fuzzy_score += 0.5

        score = exact_score + fuzzy_score
        if score > 0:
            # Extract path from line
            best_path = ""
            if "|" in line:
                parts = [p.strip() for p in line.split("|") if p.strip()]
                for p in parts:
                    if "\\" in p or "/" in p:
                        best_path = p.strip("`").strip()
                        break
            elif "→" in line:
                parts = line.split("→")
                if len(parts) > 1:
                    best_path = parts[1].strip().strip("`").strip()
            elif "\\" in line or "/" in line:
                best_path = line.strip("`").strip()

            matches.append({
                "score":   score,
                "line":    line,
                "path":    best_path,
                "exists":  os.path.exists(best_path) if best_path else False,
                "fuzzy":   fuzzy_score > 0 and exact_score == 0,
            })

    matches.sort(key=lambda x: (-x["score"], -int(x["exists"])))
    top = matches[:5]

    if not top:
        # Fallback - direct find_file search
        log(f"[S32] index_search: no index match for '{query}', trying find_file fallback")
        fallback = fs_find(f"*{query.replace(' ', '*')}*", max_results=10)
        fallback_results = fallback.get("results", [])
        if fallback_results:
            return {
                "query":      query,
                "matches":    [],
                "fallback":   fallback_results[:5],
                "next_step":  f"Index had no match. Found {len(fallback_results)} files via direct search - use these paths.",
            }
        return {
            "query":     query,
            "matches":   [],
            "next_step": "Nothing in index or filesystem. Call explore_tree on the most likely drive or folder."
        }

    best = top[0]
    if best["path"] and best["exists"]:
        next_step = f"Call list_directory('{best['path']}') or explore_tree('{best['path']}', depth=2) to go there now."
    elif best["path"]:
        next_step = f"Best match path '{best['path']}' not accessible. Call explore_tree on the parent drive."
    else:
        next_step = "Match found but no path extracted. Call explore_tree to search manually."

    return {
        "query":      query,
        "best_match": best,
        "matches":    top,
        "next_step":  next_step,
    }
    """
    Walk a directory tree to a specified depth in one call.
    Returns a structured map of everything found.
    Updates dir_map and visited_dirs in state.
    """
    # Resolve shortcuts
    _shortcuts = {
        "home": _script_dir, "reference": REFERENCE_DIR,
        "workflows": WORKFLOW_DIR, "output": CONFIG.get("out_images",""),
        "videos": CONFIG.get("out_videos",""), "loras": CONFIG.get("loras_dir",""),
        "models": CONFIG.get("models_dir",""), "datasets": CONFIG.get("datasets_dir",""),
        "comfyui": CONFIG.get("comfy_image_dir",""),
        "desktop": _get_desktop_path(),
        "documents": os.path.join(os.environ.get("USERPROFILE",""), "Documents"),
        "downloads": os.path.join(os.environ.get("USERPROFILE",""), "Downloads"),
        "user": os.environ.get("USERPROFILE",""),
    }
    path_lower = str(path).strip().lower().rstrip("\\/")
    if path_lower in _shortcuts:
        path = _shortcuts[path_lower]

    path = os.path.expandvars(os.path.expanduser(str(path)))
    if len(path) == 1 and path.isalpha():
        path = path.upper() + ":\\"
    elif len(path) == 2 and path[1] == ":":
        path = path + "\\"

    if not os.path.exists(path):
        return {"error": f"Not found: {path}"}

    IMAGE_EXTS = {".png",".jpg",".jpeg",".webp",".gif",".bmp"}
    tree = {}
    image_index = []
    total_files = 0
    total_dirs  = 0
    MAX_FILES = 500  # cap to avoid massive results

    def _walk(current, current_depth):
        nonlocal total_files, total_dirs
        if current_depth > depth:
            return {}
        if total_files > MAX_FILES:
            return {"truncated": True}

        node = {"path": current, "dirs": {}, "files": [], "images": []}
        try:
            with os.scandir(current) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            total_dirs += 1
                            if not images_only or current_depth < depth:
                                node["dirs"][e.name] = _walk(e.path, current_depth + 1)
                            state.setdefault("visited_dirs", set()).add(e.path)
                        elif e.is_file():
                            ext = os.path.splitext(e.name)[1].lower()
                            total_files += 1
                            if ext in IMAGE_EXTS:
                                node["images"].append(e.path)
                                image_index.append(e.path)
                            elif not images_only:
                                node["files"].append(e.name)
                    except Exception:
                        pass
                    if total_files > MAX_FILES:
                        node["truncated"] = True
                        break
        except PermissionError:
            node["error"] = "permission denied"
        except Exception as e:
            node["error"] = str(e)

        # Update dir_map
        state.setdefault("dir_map", {})[current] = {
            "dirs":  list(node["dirs"].keys()),
            "files": node["files"][:20],
            "images": len(node["images"]),
            "visited": time.strftime("%Y-%m-%d %H:%M"),
        }
        return node

    log(f"[S32][EXPLORE] Walking '{path}' depth={depth}...")
    tree = _walk(path, 0)
    log(f"[S32][EXPLORE] Done: {total_dirs} dirs, {total_files} files, {len(image_index)} images")

    return {
        "root":         path,
        "depth":        depth,
        "tree":         tree,
        "total_dirs":   total_dirs,
        "total_files":  total_files,
        "image_count":  len(image_index),
        "image_paths":  image_index[:50],
        "truncated":    total_files > MAX_FILES,
        "next_step":    "Call save_map() NOW to persist this map permanently" if tree else "Nothing found",
        "show_now":     f"Call show_images(paths=image_paths) to display {len(image_index)} images" if image_index else "No images found here",
    }

def explore_tree(path, depth=2, images_only=False):
    """
    Walk a directory tree to a specified depth in one call.
    Returns a structured map of everything found.
    Updates dir_map and visited_dirs in state.
    """
    _shortcuts = {
        "home": _script_dir, "reference": REFERENCE_DIR,
        "workflows": WORKFLOW_DIR, "output": CONFIG.get("out_images",""),
        "videos": CONFIG.get("out_videos",""), "loras": CONFIG.get("loras_dir",""),
        "models": CONFIG.get("models_dir",""), "datasets": CONFIG.get("datasets_dir",""),
        "comfyui": CONFIG.get("comfy_image_dir",""),
        "desktop": _get_desktop_path(),
        "documents": os.path.join(os.environ.get("USERPROFILE",""), "Documents"),
        "downloads": os.path.join(os.environ.get("USERPROFILE",""), "Downloads"),
        "user": os.environ.get("USERPROFILE",""),
    }
    path_lower = str(path).strip().lower().rstrip("\\/")
    if path_lower in _shortcuts:
        path = _shortcuts[path_lower]

    path = os.path.expandvars(os.path.expanduser(str(path)))
    if len(path) == 1 and path.isalpha():
        path = path.upper() + ":\\"
    elif len(path) == 2 and path[1] == ":":
        path = path + "\\"

    if not os.path.exists(path):
        return {"error": f"Not found: {path}"}

    IMAGE_EXTS = {".png",".jpg",".jpeg",".webp",".gif",".bmp"}
    tree = {}
    image_index = []
    total_files = 0
    total_dirs  = 0
    MAX_FILES   = 500

    def _walk(current, current_depth):
        nonlocal total_files, total_dirs
        if current_depth > depth:
            return {}
        if total_files > MAX_FILES:
            return {"truncated": True}
        node = {"path": current, "dirs": {}, "files": [], "images": []}
        try:
            with os.scandir(current) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            total_dirs += 1
                            node["dirs"][e.name] = _walk(e.path, current_depth + 1)
                            state.setdefault("visited_dirs", set()).add(e.path)
                        elif e.is_file():
                            ext = os.path.splitext(e.name)[1].lower()
                            total_files += 1
                            if ext in IMAGE_EXTS:
                                node["images"].append(e.path)
                                image_index.append(e.path)
                            elif not images_only:
                                node["files"].append(e.name)
                    except Exception:
                        pass
                    if total_files > MAX_FILES:
                        node["truncated"] = True
                        break
        except PermissionError:
            node["error"] = "permission denied"
        except Exception as e:
            node["error"] = str(e)
        state.setdefault("dir_map", {})[current] = {
            "dirs":    list(node["dirs"].keys()),
            "files":   node["files"][:20],
            "images":  len(node["images"]),
            "visited": time.strftime("%Y-%m-%d %H:%M"),
        }
        return node

    log(f"[S32][EXPLORE] Walking '{path}' depth={depth}...")
    tree = _walk(path, 0)
    log(f"[S32][EXPLORE] Done: {total_dirs} dirs, {total_files} files, {len(image_index)} images")

    return {
        "root":        path,
        "depth":       depth,
        "tree":        tree,
        "total_dirs":  total_dirs,
        "total_files": total_files,
        "image_count": len(image_index),
        "image_paths": image_index[:50],
        "truncated":   total_files > MAX_FILES,
        "next_step":   "Call save_map() NOW to persist this map permanently" if tree else "Nothing found",
        "show_now":    f"Call show_images(paths=image_paths) to display {len(image_index)} images" if image_index else "No images found here",
    }

def _memory_index_update(content, tier="recent", timestamp=None):
    """Add a new entry to the memory index when something is written."""
    if not timestamp:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d")
    try:
        # Extract a short label from the content (first line or first 60 chars)
        label = content.strip().splitlines()[0][:80] if content.strip() else content[:80]
        entry = f"- {label} [{tier}, {timestamp}]\n"
        # Append under the right section
        index_content = ""
        if os.path.exists(MEMORY_INDEX):
            with open(MEMORY_INDEX, "r", encoding="utf-8") as f:
                index_content = f.read()
        section = "## Permanent Facts" if tier == "permanent" else "## Recent"
        if section not in index_content:
            index_content += f"\n{section}\n"
        # Insert after section header
        index_content = index_content.replace(
            f"{section}\n",
            f"{section}\n{entry}"
        )
        with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
            f.write(index_content)
    except Exception as e:
        log(f"[S32] Memory index update failed: {e}")

def _memory_index_search(query):
    """Search the memory index for relevant entries. Returns matching lines."""
    if not os.path.exists(MEMORY_INDEX):
        return []
    try:
        with open(MEMORY_INDEX, "r", encoding="utf-8") as f:
            lines = f.readlines()
        query_words = query.lower().split()
        matches = []
        for line in lines:
            score = sum(1 for w in query_words if w in line.lower())
            if score > 0:
                matches.append((score, line.strip()))
        matches.sort(reverse=True)
        return [m[1] for m in matches[:10]]
    except Exception:
        return []

def _rebuild_memory_index():
    """
    Rebuild the memory index from scratch by scanning all memory files.
    Called on first boot if index doesn't exist, or when asked.
    """
    log("[S32] Rebuilding memory index...")
    sections = {
        "## Identity": [],
        "## Permanent Facts": [],
        "## Recent": [],
        "## Archives": [],
    }

    import re

    # Scan permanent memory
    if os.path.exists(MEMORY_FILE):
        try:
            content = open(MEMORY_FILE, "r", encoding="utf-8").read()
            for entry in content.split("---"):
                entry = entry.strip()
                if not entry:
                    continue
                m = re.search(r'\*(\d{4}-\d{2}-\d{2})', entry)
                date = m.group(1) if m else "unknown"
                label = entry.strip().splitlines()[-1][:80] if entry.strip() else ""
                if label:
                    sections["## Permanent Facts"].append(f"- {label} [permanent, {date}]")
        except Exception:
            pass

    # Scan recent memory
    if os.path.exists(MEMORY_RECENT):
        try:
            content = open(MEMORY_RECENT, "r", encoding="utf-8").read()
            for entry in content.split("---"):
                entry = entry.strip()
                if not entry:
                    continue
                m = re.search(r'\*(\d{4}-\d{2}-\d{2})', entry)
                date = m.group(1) if m else "unknown"
                label = entry.strip().splitlines()[-1][:80] if entry.strip() else ""
                if label:
                    sections["## Recent"].append(f"- {label} [recent, {date}]")
        except Exception:
            pass

    # Scan archives
    if os.path.exists(MEMORY_ARCHIVE):
        try:
            for fname in sorted(os.listdir(MEMORY_ARCHIVE)):
                if fname.endswith(".md"):
                    week = fname[:-3]
                    sections["## Archives"].append(f"- {week}: compressed summary available")
        except Exception:
            pass

    # Write the index
    try:
        _ensure_memory_structure()
        lines = ["# Memory Index\n",
                 f"*Rebuilt: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}*\n",
                 "*Use memory_search(query) to find specific memories.*\n\n"]
        for section, entries in sections.items():
            if entries:
                lines.append(f"{section}\n")
                lines.extend([f"{e}\n" for e in entries])
                lines.append("\n")
        with open(MEMORY_INDEX, "w", encoding="utf-8") as f:
            f.writelines(lines)
        log(f"[S32] Memory index rebuilt: {sum(len(v) for v in sections.values())} entries")
    except Exception as e:
        log(f"[S32] Memory index rebuild failed: {e}")

def memory_search(query):
    """Search the memory index for relevant entries matching a query."""
    matches = _memory_index_search(query)
    if not matches:
        return {
            "query": query,
            "matches": [],
            "tip": "Nothing found in index. Try memory_read('all') to scan full memory."
        }
    return {
        "query":   query,
        "matches": matches,
        "count":   len(matches),
        "tip":     "Use memory_read(tier) to read the full content of a matching tier."
    }

def memory_write(content, permanent=False):
    """
    Write to memory.
    permanent=True → identity tier (memory.md) - never compressed, never rotated
    permanent=False → recent tier (memory_recent.md) - compressed after 7 days
    """
    if state.get("resetting"):
        return {"error": "System 32 is factory resetting."}
    _ensure_memory_structure()
    target = MEMORY_FILE if permanent else MEMORY_RECENT
    if not os.path.exists(REFERENCE_DIR):
        log("[S32] Memory write skipped - reference directory not accessible (USB removed?)")
        return {"error": "Reference directory not accessible - is the USB connected?"}
    try:
        import datetime as _dt
        now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n---\n*{now}*\n\n{content.strip()}\n"
        with open(target, "a", encoding="utf-8") as f:
            f.write(entry)
        tier = "permanent" if permanent else "recent"
        _memory_index_update(content, tier, now[:10])
        log(f"[S32] Memory written ({tier}): {content[:80]}")
        return {"status": "saved", "tier": tier, "message": f"Saved to {tier} memory."}
    except Exception as e:
        log(f"[S32] Memory write failed: {e}")
        return {"error": str(e)}

def mark_favorite(path, reason=""):
    """Save an image to favorites.json with an optional reason."""
    _ensure_reference_dir()
    favorites_path = os.path.join(PERSON_DIR, "favorites.json")
    try:
        with open(favorites_path, "r", encoding="utf-8") as f:
            favorites = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        favorites = []

    out_images = CONFIG.get("out_images", "")
    if not os.path.isabs(path):
        candidate = os.path.join(out_images, path)
        if os.path.isfile(candidate):
            path = candidate

    if not os.path.isfile(path):
        return {"error": f"File not found: {path}"}

    for entry in favorites:
        if entry.get("path") == path:
            entry["reason"] = reason or entry.get("reason", "")
            entry["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            with open(favorites_path, "w", encoding="utf-8") as f:
                json.dump(favorites, f, indent=2)
            return {"status": "updated", "path": path}

    favorites.append({
        "path":     path,
        "filename": os.path.basename(path),
        "reason":   reason,
        "added":    datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    with open(favorites_path, "w", encoding="utf-8") as f:
        json.dump(favorites, f, indent=2)
    log(f"[S32] Favorite saved: {os.path.basename(path)}")
    return {"status": "saved", "path": path, "total_favorites": len(favorites)}

def get_favorites():
    """Return its favorites list."""
    _ensure_reference_dir()
    favorites_path = os.path.join(PERSON_DIR, "favorites.json")
    try:
        with open(favorites_path, "r", encoding="utf-8") as f:
            favorites = json.load(f)
        return {"favorites": favorites, "count": len(favorites)}
    except FileNotFoundError:
        return {"favorites": [], "count": 0}
    except json.JSONDecodeError:
        return {"error": "favorites.json is corrupted"}


# ── Favorite working/modding paths ──────────────────────────────────────────
# Folders (or files) the operator uses often for work or modding. Saved so it
# remembers them across sessions and can jump straight there instead of
# re-searching. These are surfaced in the system prompt so it always has them.
PATH_FAVORITES_FILE = os.path.join(PERSON_DIR, "path_favorites.json")


def _load_path_favorites():
    try:
        with open(PATH_FAVORITES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_path_favorites(favs):
    _ensure_reference_dir()
    with open(PATH_FAVORITES_FILE, "w", encoding="utf-8") as f:
        json.dump(favs, f, indent=2)


def favorite_path(path, label="", reason=""):
    """Save a frequently-used working/modding folder (or file) as a favorite path."""
    _ensure_reference_dir()
    p = os.path.expandvars(os.path.expanduser(str(path)))
    if not p:
        return {"error": "No path provided."}
    if not os.path.exists(p):
        return {"error": f"Path not found: {p}"}
    favs = _load_path_favorites()
    norm = os.path.normpath(p).lower()
    for entry in favs:
        if os.path.normpath(entry.get("path", "")).lower() == norm:
            if label:  entry["label"]  = label
            if reason: entry["reason"] = reason
            entry["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            _save_path_favorites(favs)
            return {"status": "updated", "path": p, "label": entry.get("label", "")}
    favs.append({
        "path":   p,
        "label":  label or os.path.basename(p.rstrip("\\/")) or p,
        "reason": reason,
        "is_dir": os.path.isdir(p),
        "added":  datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    _save_path_favorites(favs)
    return {"status": "saved", "path": p, "label": favs[-1]["label"], "total": len(favs)}


def get_favorite_paths():
    """Return saved favorite working/modding paths, newest first."""
    favs = _load_path_favorites()
    return {"favorite_paths": list(reversed(favs)), "count": len(favs)}


def remove_favorite_path(path):
    """Remove a path from favorites."""
    favs = _load_path_favorites()
    norm = os.path.normpath(os.path.expandvars(os.path.expanduser(str(path)))).lower()
    kept = [e for e in favs if os.path.normpath(e.get("path", "")).lower() != norm]
    _save_path_favorites(kept)
    return {"removed": len(favs) - len(kept), "remaining": len(kept)}


def favorite_paths_for_prompt(limit=15):
    """Compact list of favorite paths for injection into the system prompt."""
    favs = _load_path_favorites()
    if not favs:
        return ""
    lines = []
    for e in list(reversed(favs))[:limit]:
        lbl = e.get("label", "") or os.path.basename(e.get("path", ""))
        rsn = f" — {e['reason']}" if e.get("reason") else ""
        lines.append(f"- {lbl}: {e.get('path','')}{rsn}")
    return "\n".join(lines)
    """
    Write something to permanent memory. Called by it when it decides
    something is worth keeping forever.
    """
    _ensure_reference_dir()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n---\n*{now}*\n\n{content.strip()}\n"
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        log(f"[S32] Memory written: {content[:80]}")
        return {"status": "saved", "message": "Saved to permanent memory."}
    except Exception as e:
        log(f"[S32] Memory write failed: {e}")
        return {"error": str(e)}

def save_directory_map():
    """
    Save the explored directory tree to index.md.
    Appends the map below the notes divider so it persists across sessions.
    Called by it when it wants to preserve what it's mapped.
    """
    _ensure_reference_dir()
    dir_map = state.get("dir_map", {})
    if not dir_map:
        return {"status": "empty", "message": "No directories explored yet."}

    # Build tree representation
    lines = [f"\n## Directory Map - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]
    lines.append(f"*{len(dir_map)} directories explored this session.*\n")
    for path in sorted(dir_map.keys()):
        entry = dir_map[path]
        depth = path.replace("\\", "/").count("/") - 1
        indent = "  " * min(depth, 6)
        lines.append(f"{indent}**{path}/**")
        for d in entry.get("dirs", [])[:10]:
            lines.append(f"{indent}  📁 {d}/")
        file_list = entry.get("files", [])
        if file_list:
            shown = file_list[:8]
            lines.append(f"{indent}  📄 {', '.join(shown)}" +
                         (f" +{len(file_list)-8} more" if len(file_list) > 8 else ""))
        lines.append("")

    map_text = "\n".join(lines)

    # Append to index.md below the divider
    index_path = os.path.join(REFERENCE_DIR, "index.md")
    try:
        existing = open(index_path, encoding="utf-8").read() if os.path.exists(index_path) else ""
        # Remove old map section if present
        if "## Directory Map" in existing:
            existing = existing[:existing.index("## Directory Map")].rstrip()
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(existing + "\n" + map_text)
        log(f"[S32] Directory map saved: {len(dir_map)} paths")
        return {"status": "saved", "paths_mapped": len(dir_map), "saved_to": index_path}
    except Exception as e:
        return {"error": str(e)}
    """
    Write something to permanent memory. Called by it when it decides
    something is worth keeping forever.
    """
    _ensure_reference_dir()
    try:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n---\n*{now}*\n\n{content.strip()}\n"
        with open(MEMORY_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
        log(f"[S32] Memory written: {content[:80]}")
        return {"status": "saved", "message": "Saved to permanent memory."}
    except Exception as e:
        log(f"[S32] Memory write failed: {e}")
        return {"error": str(e)}

def memory_read(tier="all"):
    """Read memory. tier='permanent', 'recent', 'all', or 'operator'."""
    _ensure_memory_structure()
    result = {}
    if tier in ("permanent", "all"):
        try:
            result["permanent"] = open(MEMORY_FILE, "r", encoding="utf-8").read() if os.path.exists(MEMORY_FILE) else ""
        except Exception as e:
            result["permanent_error"] = str(e)
    if tier in ("recent", "all"):
        try:
            result["recent"] = open(MEMORY_RECENT, "r", encoding="utf-8").read() if os.path.exists(MEMORY_RECENT) else ""
        except Exception as e:
            result["recent_error"] = str(e)
    _op = state.get("operator")
    if tier in ("operator", "all") and _op:
        op = _load_operator(_op)
        result["operator"] = op["content"] if op else "No operator profile found"
    result["machine"] = _machine_name
    result["current_operator"] = _op or "(not introduced yet)"
    return result



# Registry URL - the public ComfyUI Manager node list
_REGISTRY_URL = "https://raw.githubusercontent.com/ltdrdata/ComfyUI-Manager/main/custom-node-list.json"
_registry_cache = None  # loaded on first use

# Log error patterns that indicate a missing custom node
_MISSING_PATTERNS = [
    r"Cannot import .+?custom_nodes.+? module for custom nodes: (.+)",
    r"Cannot import module '([^']+)'",
    r"Failed to import ([^\s,\n]+)",
    r"No module named '([^']+)'",
    r"ModuleNotFoundError.*'([^']+)'",
    r"import_failed_extensions.*?\[([^\]]+)\]",
    r"Failed to load custom node.*?([A-Za-z0-9_\-]+)",
]

# Packages that are optional/known-missing on most systems - don't treat as errors
_LOG_IGNORE = {
    "triton", "triton.runtime", "triton.language",
    "flash_attn",
    "xformers",
    "bitsandbytes",
    "deepspeed",
    "flet",
    "kokoro_onnx",
    "sageattention",
    "onnxruntime",
    "accelerate",
    "compel",
    "cv2",
    "opencv",
    "sklearn",
    "scipy",
    "matplotlib",
    "imageio",
    "av",
    "insightface",
    "mmcv",
    "mmdet",
    "mmpose",
}

# Known node packages that should be pip-installed, not git-cloned
_PIP_NODES = {
    "comfy_aimdo":              "comfy-aimdo",
    "comfy_aimdo.vram_buffer":  "comfy-aimdo",
    "comfyui_frontend_package": "comfyui-frontend-package",
}

def _get_desktop_path():
    """Get actual Desktop path - handles OneDrive redirection via registry."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        )
        path, _ = winreg.QueryValueEx(key, "Desktop")
        winreg.CloseKey(key)
        if path and os.path.exists(path):
            return path
    except Exception:
        pass
    # Fallback
    return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")

def fs_list(path, max_items=200):
    """
    List directory contents with full navigation context.
    Returns current path, parent path, subdirs, and files separately
    so it can navigate the tree without guessing paths.
    """
    # Resolve named shortcuts to real paths
    _shortcuts = {
        "home":            _script_dir,
        "reference":       REFERENCE_DIR,
        "workflows":       WORKFLOW_DIR,
        "output":          CONFIG.get("out_images", ""),
        "videos":          CONFIG.get("out_videos", ""),
        "loras":           CONFIG.get("loras_dir", ""),
        "models":          CONFIG.get("models_dir", ""),
        "datasets":        CONFIG.get("datasets_dir", ""),
        "input":           CONFIG.get("comfy_image_input", ""),
        "comfyui":         CONFIG.get("comfy_image_dir", ""),
        "custom_nodes":    os.path.join(CONFIG.get("comfy_image_dir", ""), "custom_nodes"),
        "desktop":         _get_desktop_path(),
        "documents":       os.path.join(os.environ.get("USERPROFILE", ""), "Documents"),
        "downloads":       os.path.join(os.environ.get("USERPROFILE", ""), "Downloads"),
        "user":            os.environ.get("USERPROFILE", ""),
    }

    path_lower = path.strip().lower().rstrip("\\/")
    if path_lower in _shortcuts:
        path = _shortcuts[path_lower]

    path = os.path.expandvars(os.path.expanduser(path))

    # Normalize drive letters - "G" → "G:\", "G:" → "G:\"
    if len(path) == 1 and path.isalpha():
        path = path.upper() + ":\\"
    elif len(path) == 2 and path[1] == ":":
        path = path + "\\"

    log(f"[S32][LS] Resolved path: '{path}' exists={os.path.exists(path)} isdir={os.path.isdir(path) if os.path.exists(path) else 'N/A'}")
    if not os.path.exists(path) or not os.path.isdir(path):
        if re.fullmatch(r"[A-Za-z]:\\?", path):          # a bare drive letter only — never free text into cmd
            try:
                r = subprocess.run(
                    ["cmd", "/c", f"dir {path} /b"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                if r.returncode == 0:
                    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
                    dirs  = []
                    files = []
                    for l in lines:
                        full = os.path.join(path, l)
                        try:
                            if os.path.isdir(full):
                                dirs.append({"name": l, "path": full, "type": "dir"})
                            else:
                                dirs.append({"name": l, "path": full, "type": "dir"})
                        except Exception:
                            dirs.append({"name": l, "path": full, "type": "unknown"})
                    return {
                        "current":   path,
                        "parent":    None,
                        "shortcuts": list(_shortcuts.keys()),
                        "dirs":      dirs,
                        "files":     files,
                        "dir_count": len(dirs),
                        "file_count": 0,
                        "note": "Listed via shell fallback",
                    }
            except Exception:
                pass
        suggestions = {k: v for k, v in _shortcuts.items() if v and os.path.exists(v)}
        return {"error": f"Not found: {path}", "shortcuts": suggestions}

    if not os.path.isdir(path):
        return {"error": f"Not a directory: {path}"}

    abs_path = os.path.abspath(path)
    parent = os.path.dirname(abs_path)
    if parent == abs_path:
        parent = None  # at drive root

    dirs  = []
    files = []
    try:
        with os.scandir(abs_path) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        dirs.append({
                            "name": e.name,
                            "path": e.path,
                            "type": "dir"
                        })
                    else:
                        s = e.stat()
                        files.append({
                            "name":     e.name,
                            "path":     e.path,
                            "type":     "file",
                            "size_mb":  round(s.st_size / 1048576, 2),
                            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(s.st_mtime))
                        })
                except Exception:
                    pass
                if len(dirs) + len(files) >= max_items:
                    break
    except PermissionError:
        return {"error": f"Permission denied: {abs_path}"}

    # Track visited folders this session to avoid re-entering
    if "visited_dirs" not in state:
        state["visited_dirs"] = set()
    already_visited = abs_path in state["visited_dirs"]
    state["visited_dirs"].add(abs_path)

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())

    # Count images and hint at show_images
    image_files = [f for f in files if os.path.splitext(f["name"])[1].lower()
                   in {".png",".jpg",".jpeg",".webp",".gif",".bmp"}]

    # Filter dirs to exclude already visited ones
    unvisited_dirs = [d for d in dirs if d["path"] not in state["visited_dirs"]]

    # Update the running directory map in state
    if "dir_map" not in state:
        state["dir_map"] = {}
    state["dir_map"][abs_path] = {
        "dirs":    [d["name"] for d in dirs],
        "files":   [f["name"] for f in files],
        "visited": time.strftime("%Y-%m-%d %H:%M"),
    }

    result = {
        "current":         abs_path,
        "parent":          parent,
        "shortcuts":       list(_shortcuts.keys()),
        "dirs":            dirs,
        "unvisited_dirs":  unvisited_dirs,
        "files":           files,
        "dir_count":       len(dirs),
        "file_count":      len(files),
        "truncated":       len(dirs) + len(files) >= max_items,
        "already_visited": already_visited,
        "map_size":        len(state.get("dir_map", {})),
        "navigate":        {
            "up":   f'list_directory("{parent}")' if parent else None,
            "into": "list_directory(path) - use any path from dirs above",
        }
    }

    # A small tree's files a few levels down, so one look is enough to find code
    # (2026-10-29: original\ → Scripts\ → Source\ → back up — the .psc four levels
    # down were never seen, and she decided the import was 'empty').
    if not image_files and dirs and len(dirs) <= 12:
        deep, seen = [], 0
        base_depth = abs_path.rstrip("\\/").count(os.sep)
        try:
            for d_, sub, fns in os.walk(abs_path):
                seen += len(sub) + len(fns)
                if seen > 600 or len(deep) >= 40:
                    break
                if d_.rstrip("\\/").count(os.sep) - base_depth >= 5:
                    sub[:] = []
                sub[:] = sorted(x for x in sub if not x.startswith(".") and x.lower() not in
                                ("node_modules", "__pycache__", ".git"))
                if d_ == abs_path:
                    continue                                     # top-level files are listed already
                for fn in sorted(fns):
                    deep.append(os.path.relpath(os.path.join(d_, fn), abs_path))
                    if len(deep) >= 40:
                        break
        except Exception:
            deep = []
        if deep:
            result["deep_files"] = deep
    if image_files:
        result["images_found"] = len(image_files)
        result["next_step"] = f"Call show_images(paths=image_paths) NOW to display {len(image_files)} images. Do not call list_directory again."
        result["image_paths"] = [f["path"] for f in image_files[:24]]
    elif not dirs and not files:
        result["dead_end"] = True
        result["next_step"] = f"Empty. Call list_directory('{parent}') to go back up."
    elif not image_files:
        if unvisited_dirs:
            result["no_images_here"] = True
            result["next_step"] = ("No images here. Files in this folder and its subfolders are listed above — "
                                   "read one with read_file(<full path>). For images, check an unvisited "
                                   f"subdir, or call list_directory('{parent}') to go back up.")
        else:
            result["dead_end"] = True
            result["next_step"] = f"No images and no unvisited subdirs. Call list_directory('{parent}') to go back up. Do NOT re-enter this folder."

    return result

def fs_find(pattern, root=None, max_results=50, images_only=False):
    import fnmatch
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff", ".svg"}
    results = []
    # Resolve shortcut names in root
    if root:
        root_lower = root.strip().lower().rstrip("\\/")
        _root_shortcuts = {
            "home": _script_dir,
            "desktop": _get_desktop_path(),
            "reference": REFERENCE_DIR,
            "output": CONFIG.get("out_images", ""),
            "models": CONFIG.get("models_dir", ""),
            "loras": CONFIG.get("loras_dir", ""),
            "documents": os.path.join(os.environ.get("USERPROFILE", ""), "Documents"),
        }
        if root_lower in _root_shortcuts:
            root = _root_shortcuts[root_lower]
        # Normalize bare drive letter
        elif len(root) == 1 and root.isalpha():
            root = root.upper() + ":\\"
        elif len(root) == 2 and root[1] == ":" and not root.endswith("\\"):
            root = root + "\\"
        # Also fix if someone passes the old non-OneDrive desktop path
        elif "desktop" in root_lower and not os.path.exists(root):
            root = _get_desktop_path()

    skip = {"windows", "winsxs", "$recycle.bin", "system volume information"}

    # Always check desktop and common locations first
    desktop = _get_desktop_path()

    def _shell_folder(name):
        """Read a known folder path from the Windows registry."""
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
            )
            path, _ = winreg.QueryValueEx(key, name)
            winreg.CloseKey(key)
            return path if path and os.path.exists(path) else ""
        except Exception:
            return ""

    known_dirs = [
        desktop,
        _shell_folder("My Pictures"),
        _shell_folder("My Video"),
        WORKFLOW_DIR,
        CONFIG.get("comfy_image_input", ""),
        CONFIG.get("out_images", ""),
        REFERENCE_DIR,
        _script_dir,
    ]
    known_dirs = [d for d in known_dirs if d and os.path.exists(d)]
    for d in known_dirs:
        if not d or not os.path.exists(d):
            continue
        try:
            for entry in os.scandir(d):
                if fnmatch.fnmatch(entry.name.lower(), pattern.lower()):
                    if images_only and entry.is_file():
                        if os.path.splitext(entry.name)[1].lower() not in IMAGE_EXTS:
                            continue
                    info = {
                        "path": entry.path,
                        "type": "dir" if entry.is_dir() else "file",
                    }
                    try:
                        s = entry.stat()
                        info["modified"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(s.st_mtime))
                        if entry.is_file():
                            info["size_mb"] = round(s.st_size / 1048576, 2)
                    except Exception:
                        pass
                    results.append(info)
        except Exception:
            pass

    if results:
        # Filter out already-visited directories from results to prevent re-entry loops
        visited = state.get("visited_dirs", set())
        for r in results:
            if r.get("type") == "dir" and r.get("path") in visited:
                r["already_visited"] = True
                r["note"] = "Already checked - no images found here"
        return {"results": results, "truncated": False}

    # Fall back to walking the specified root or C:\
    scan_root = os.path.expandvars(root or "C:\\")
    try:
        for dirpath, dirs, files in os.walk(scan_root, topdown=True):
            dirs[:] = [d for d in dirs if d.lower() not in skip]
            # Check directories too
            for d in dirs:
                if fnmatch.fnmatch(d.lower(), pattern.lower()):
                    full = os.path.join(dirpath, d)
                    results.append({"path": full, "type": "dir"})
                    if len(results) >= max_results:
                        return {"results": results, "truncated": True}
            for f in files:
                if fnmatch.fnmatch(f.lower(), pattern.lower()):
                    if images_only:
                        if os.path.splitext(f)[1].lower() not in IMAGE_EXTS:
                            continue
                    full = os.path.join(dirpath, f)
                    try:
                        s = os.stat(full)
                        results.append({
                            "path": full,
                            "type": "file",
                            "size_mb": round(s.st_size / 1048576, 2),
                            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(s.st_mtime))
                        })
                        if len(results) >= max_results:
                            return {"results": results, "truncated": True}
                    except Exception:
                        pass
    except Exception as e:
        return {"error": str(e)}
    return {"results": results, "truncated": False}


def _resolve_shortcut(path):
    """Resolve a Windows shortcut to the real path it points at.
    .url  → parsed directly (Steam .url IconFile usually lives in the install dir).
    .lnk  → resolved via PowerShell's WScript.Shell COM (no extra dependencies).
    Returns {"shortcut","target",...} or {"error":...}."""
    try:
        path = os.path.expandvars(os.path.expanduser(str(path)))
        if not os.path.isfile(path):
            return {"error": f"Not found: {path}"}
        ext = os.path.splitext(path)[1].lower()

        if ext == ".url":
            url = icon = ""
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    s = line.strip()
                    low = s.lower()
                    if low.startswith("url="):
                        url = s[4:].strip()
                    elif low.startswith("iconfile="):
                        icon = s[9:].strip()
            target = icon or url
            if not target:
                return {"error": f"No target in {path}"}
            out = {"shortcut": path, "target": target, "url": url}
            if icon and ("\\" in icon or "/" in icon):
                out["install_dir"] = os.path.dirname(icon)
            return out

        if ext == ".lnk":
            safe = path.replace("'", "''")
            ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
                  "Write-Output ($s.TargetPath + '|~|' + $s.Arguments + '|~|' + $s.WorkingDirectory)") % safe
            r = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            parts = (r.stdout or "").strip().split("|~|")
            target = parts[0].strip() if parts else ""
            if not target:
                return {"error": f"Could not resolve {path}"}
            out = {"shortcut": path, "target": target}
            if len(parts) > 1 and parts[1].strip():
                out["arguments"] = parts[1].strip()
            if len(parts) > 2 and parts[2].strip():
                out["working_dir"] = parts[2].strip()
            if os.path.isfile(target):
                out["install_dir"] = os.path.dirname(target)
            return out

        return {"error": f"Not a shortcut (.lnk/.url): {path}"}
    except Exception as e:
        log(f"[S32] shortcut resolve failed for {path}: {e}")
        return {"error": str(e)}


def _enrich_shortcuts(result, cap=8):
    """Add a 'target' (resolved path) to any .lnk/.url entries in a search
    result, so the real install location travels inline with the find — no
    extra tool call needed. Capped so a result full of .lnk files can't spawn
    a flood of PowerShell calls."""
    if not isinstance(result, dict):
        return result
    n = 0
    for key in ("results", "found"):
        lst = result.get(key)
        if not isinstance(lst, list):
            continue
        for entry in lst:
            if n >= cap:
                return result
            if not isinstance(entry, dict):
                continue
            p = entry.get("path", "")
            if isinstance(p, str) and p.lower().endswith((".lnk", ".url")):
                res = _resolve_shortcut(p)
                if res and res.get("target"):
                    entry["target"] = res["target"]
                    if res.get("install_dir"):
                        entry["install_dir"] = res["install_dir"]
                    n += 1
    return result

def show_file(path, offset=0, chunk=5000):
    """
    Pull any file into chat - images display inline, videos and other files
    show as a link with metadata. Works on any path on the filesystem.
    offset: character offset for reading large files in pages (default 0)
    chunk: characters to read per call (default 5000)
    """
    path = os.path.expandvars(os.path.expanduser(str(path)))

    # Normalize drive letter
    if len(path) == 1 and path.isalpha():
        path = path.upper() + ":\\"
    elif len(path) == 2 and path[1] == ":":
        path = path + "\\"

    if not os.path.exists(path):
        return {"error": f"Not found: {path}"}
    if not os.path.isfile(path):
        return {"error": f"Not a file: {path} - use list_directory to browse"}

    ext = os.path.splitext(path)[1].lower()
    size = os.path.getsize(path)
    size_mb = round(size / 1048576, 2)

    IMAGE_EXTS  = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tiff"}
    VIDEO_EXTS  = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".gif"}
    TEXT_EXTS_S = {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".py",
                   ".js", ".html", ".css", ".log", ".ini", ".cfg", ".bat"}

    if ext in IMAGE_EXTS:
        return {
            "type":     "image",
            "url":      f"/img?path={urllib.parse.quote(path)}",
            "filename": os.path.basename(path),
            "saved":    path,
            "size_mb":  size_mb,
        }
    elif ext in VIDEO_EXTS:
        return {
            "type":     "video",
            "url":      f"/img?path={urllib.parse.quote(path)}",   # /img streams videos (Range)
            "filename": os.path.basename(path),
            "path":     path,
            "size_mb":  size_mb,
        }
    elif ext in TEXT_EXTS_S or _looks_text(path):
        # Read text files with offset pagination (any extension, if the file is text)
        try:
            full_size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(offset)
                text = f.read(chunk)
            has_more = (offset + len(text.encode("utf-8"))) < full_size
            total_pages = max(1, -(-full_size // (chunk or 5000)))  # ceiling div
            current_page = (offset // (chunk or 5000)) + 1
            return {
                "type":         "text",
                "filename":     os.path.basename(path),
                "path":         path,
                "content":      text,
                "size_mb":      size_mb,
                "offset":       offset,
                "next_offset":  offset + len(text.encode("utf-8")) if has_more else None,
                "has_more":     has_more,
                "page":         current_page,
                "total_pages":  total_pages,
                "hint": f"Page {current_page}/{total_pages}. Call show_file with offset={offset + len(text.encode('utf-8'))} to read the next page." if has_more else "End of file.",
            }
        except Exception as e:
            return {"error": str(e)}
    else:
        return {
            "type":     "file",
            "filename": os.path.basename(path),
            "path":     path,
            "ext":      ext,
            "size_mb":  size_mb,
            "note":     "Binary file - cannot display contents directly",
        }

def _scan_patch_risk(content, filename):
    """Phase 8 safeguard. Flag a staged patch that could modify System 32's own
    files or execute code IF it were run. This does NOT block — patches are
    proposals the operator reviews and applies by hand — but it surfaces risk so
    that review is informed. Inert file types (.md/.txt/.json) are never flagged."""
    flags = []
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".py", ".pyw", ".bat", ".cmd", ".ps1", ".sh"):
        return flags
    own_files = ("system32.py", "s32_llm.py", "s32_dispatch.py", "s32_filesystem.py",
                 "s32_memory.py", "s32_hardware.py", "s32_comfyui.py", "s32_config.py",
                 "senses.py", "voice.py", "bootstrap.py", "launch.py", "s32_docs.py",
                 "identity.md", "config.json")
    low = content.lower()
    writes  = bool(re.search(r"open\s*\([^)]*['\"][wa]", content)) or ".write(" in content
    deletes = any(p in content for p in ("os.remove", "os.unlink", "shutil.rmtree",
                                          "os.replace", "os.rename"))
    execs   = any(p in content for p in ("exec(", "eval(", "__import__", "compile(",
                                          "subprocess", "os.system", "popen"))
    if (writes or deletes) and any(f in low for f in own_files):
        flags.append("references System 32's own files in a write/delete context")
    if deletes:
        flags.append("contains file-deletion calls (remove/unlink/rmtree/replace)")
    if execs:
        flags.append("contains code execution (exec/eval/subprocess/os.system)")
    if re.search(r"open\s*\([^)]*\.\.[\\/]", content):
        flags.append("opens a path that climbs out of its folder ('..')")
    return flags


def write_file(path, content, description=""):
    """Legacy entry point. There is NO patches staging any more (operator, 2026-10-27:
    "it stays in chat unless I download it" is safer) — a file without a real
    destination is shown in chat with a Download button and nothing touches disk."""
    from s32_code import chat_file
    return chat_file(os.path.basename(str(path or "").replace("/", "\\")) or "file.txt",
                     content, description)


# write_file() above only shows a file in chat. The functions below let it
# write DIRECTLY to this computer, confined and gated:
#   • Free-write zone — any LOCAL drive that is NOT its own drive and NOT a
#     protected system folder: create or edit, after one in-chat Confirm.
#   • Its own drive (where System 32 runs): never written — shown in chat to download.
#   • System folders (Windows, Program Files[ (x86)], system-drive root/boot):
#     BLOCKED even with Confirm.
#   • Network / UNC / remote drives: refused outright.
# Containment is checked at BOTH propose and confirm (defence in depth). The only
# code path that touches disk is _do_write(), reached only via confirm_write().
import ntpath as _nt

_OWN_DRIVE = (_nt.splitdrive(_script_dir)[0] or "").upper()        # e.g. "I:"
_SYS_DRIVE = (os.environ.get("SystemDrive") or "C:").upper()


def _canon_write_path(path):
    """Expand + canonicalise to an absolute Windows path. realpath (resolving
    junctions/symlinks, so escapes can't dodge the rules) is applied only on the
    real system; off-Windows we stay lexical so the classifier stays testable."""
    p = os.path.expandvars(os.path.expanduser(path or ""))
    if os.name == "nt":
        try:
            p = os.path.realpath(p)
        except Exception:
            p = os.path.abspath(p)
    return _nt.normpath(p)


def _drive_type(path):
    """'remote' for UNC/network drives, else 'local'. Conservative: anything we
    can't classify on Windows is treated as local (still subject to every other
    containment rule)."""
    if path.startswith("\\\\") or path.startswith("//"):
        return "remote"
    if os.name == "nt":
        try:
            import ctypes
            drive = _nt.splitdrive(path)[0]
            if drive:
                t = ctypes.windll.kernel32.GetDriveTypeW(drive + "\\")
                return "remote" if t == 4 else "local"      # 4 = DRIVE_REMOTE
        except Exception:
            return "local"
    return "local"


def _system_blocked(cpath):
    """True if cpath is inside a protected system area on the system drive."""
    drive, rest = _nt.splitdrive(cpath)
    if drive.upper() != _SYS_DRIVE:
        return False
    low = cpath.lower()
    for name in ("windows", "program files", "program files (x86)"):
        root = _nt.join(_SYS_DRIVE + "\\", name).lower()
        if low == root or low.startswith(root + "\\"):
            return True
    # Directly at the system-drive root (e.g. C:\bootmgr) — protect boot files.
    parts = [seg for seg in rest.split("\\") if seg]
    if len(parts) <= 1:
        return True
    return False


def _classify_write(path):
    raw = (path or "").strip()
    if not raw:
        return {"zone": "invalid", "path": "",
                "reason": "No file path was given."}
    # No directory component at all (e.g. "steam_launcher.py") → shown in chat only, with
    # a Download button. Nothing lands on disk unless the operator downloads it
    # (operator's call, 2026-10-27: safer than the old patches staging).
    if raw and not _nt.split(raw)[0] and "/" not in raw and ":" not in raw:
        return {"zone": "chat", "path": raw,
                "reason": "No folder was given, so it's in chat to download."}
    cpath = _canon_write_path(path)
    drive = _nt.splitdrive(cpath)[0].upper()
    if not drive:
        return {"zone": "invalid", "path": cpath,
                "reason": "I need a full path with a drive letter "
                          "(e.g. C:\\Users\\you\\notes.txt)."}
    if _drive_type(cpath) == "remote":
        return {"zone": "remote", "path": cpath,
                "reason": "That's a network / remote location — I only write to this computer."}
    if drive == _OWN_DRIVE:
        return {"zone": "chat", "path": cpath,
                "reason": "That's my own drive — I don't write there; it's in chat for you to download."}
    if _system_blocked(cpath):
        return {"zone": "blocked", "path": cpath,
                "reason": "That's a protected system folder — I won't write there even on confirm."}
    return {"zone": "free", "path": cpath}


def _push_note(text, header="⬡ Files"):
    try:
        import s32_config as _cfg
        with _result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _push_result({"content": text, "path": header}, jid)
        _push_done(jid)
    except Exception:
        pass


def _push_write_card(pending):
    try:
        import s32_config as _cfg
        content = pending["content"]
        preview = content[:500]
        with _result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _push_result({
            "type":              "fs_confirm",
            "token":             pending["token"],
            "path":              pending["path"],
            "mode":              pending["mode"],
            "size":              pending["size"],
            "lines":             pending["lines"],
            "preview":           preview,
            "preview_truncated": len(content) > len(preview),
            "description":       pending.get("description", ""),
        }, jid)
        _push_done(jid)
    except Exception as e:
        log(f"[S32] write card push failed: {e}")


def _clear_pending_write():
    try:
        state["pending_write"] = None
    except Exception:
        pass


def propose_write(path, content, description=""):
    """Propose a create/edit. Free-zone writes stage into state['pending_write']
    and raise an in-chat Confirm card; nothing touches disk until confirm_write().
    No-folder and it-own-drive writes become a chat-only card (Download / Copy) —
    nothing on disk. System/remote are refused."""
    if content is None:
        content = ""
    if not isinstance(content, str):
        content = str(content)
    cls = _classify_write(path)
    zone, cpath = cls["zone"], cls["path"]

    if zone in ("invalid", "remote", "blocked"):
        _push_note(f"⛔ {cls['reason']}\n    {cpath}")
        return {"refused": True, "zone": zone, "reason": cls["reason"], "path": cpath}

    if zone == "chat":
        from s32_code import chat_file
        return chat_file(os.path.basename(cpath.replace("/", "\\")) or cpath, content, description)

    # FREE zone — confined direct write, gated by Confirm
    exists = os.path.exists(cpath)
    mode = "edit" if exists else "create"
    pending = {
        "token":       f"write-{int(time.time() * 1000)}",
        "path":        cpath,
        "content":     content,
        "mode":        mode,
        "size":        len(content),
        "lines":       content.count("\n") + 1,
        "description": description or "",
        "ts":          time.strftime("%H:%M:%S"),
    }
    try:
        state["pending_write"] = pending
    except Exception:
        pass
    _push_write_card(pending)
    verb = "overwrite" if mode == "edit" else "create"
    return {"ok": True, "staged": True, "mode": mode, "path": cpath,
            "note": (f"Proposed — I'll {verb} this only after you press Confirm. "
                     f"Nothing is written yet. Do NOT call write_file again; wait for the operator.")}


def _do_write(pending):
    cpath = pending["path"]
    try:
        parent = _nt.dirname(cpath)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(cpath, "w", encoding="utf-8") as f:
            f.write(pending["content"])
        log(f"[S32] Wrote {cpath} ({pending['lines']} lines) on operator confirmation.")
        return {"ok": True}
    except PermissionError:
        return {"ok": False, "error": f"Permission denied: {cpath}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def confirm_write(token=None):
    """The ONLY path that writes to disk in the free zone. Reached only when a
    human presses Confirm. Re-classifies before writing (defence in depth)."""
    pending = state.get("pending_write")
    if not pending:
        return {"error": "There's nothing staged to write."}
    if not token or token != pending.get("token"):        # a token is REQUIRED (no blind confirms)
        return {"error": "That confirmation no longer matches what's staged."}
    cls = _classify_write(pending["path"])
    if cls["zone"] != "free":
        _clear_pending_write()
        _push_note(f"⛔ Stopped — {cls.get('reason', 'that location is no longer writable')}.")
        return {"refused": True, "reason": cls.get("reason")}
    res = _do_write(pending)
    _clear_pending_write()
    if res.get("ok"):
        _push_note(f"✓ {pending['mode'].capitalize()} done: {pending['path']} "
                   f"({pending['lines']} lines).")
        return {"ok": True, "path": pending["path"], "mode": pending["mode"]}
    _push_note(f"⚠ Couldn't write {pending['path']}: {res.get('error', 'unknown error')}")
    return res


def cancel_write(token=None):
    pending = state.get("pending_write")
    _clear_pending_write()
    if pending:
        _push_note(f"✗ Cancelled — {pending.get('path', 'the write')} was not written.")
    return {"ok": True, "cancelled": True}


# ── Native file operations: copy / move / rename / new folder / delete ──────────
# 2026-10-30: asked to copy pictures from test1 to test2, she wrote a Python script (the
# operator declined it). Now she PROPOSES the operation; a Confirm card lists exactly what
# will happen; nothing touches disk until the operator presses Confirm. Same zones as
# writes: never its own drive, Windows / Program Files, or network drives; never System
# 32's private data. Delete goes to the Recycle Bin. Name clashes get _1, _2 … (no overwrite).
_FILEOP_MAX = 5000
_FILEOP_KINDS = {
    "images": {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".tga", ".dds"},
    "videos": {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".flv", ".mpg", ".mpeg"},
    "audio":  {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".wma"},
}
_FILEOP_KINDS.update({"pictures": _FILEOP_KINDS["images"], "photos": _FILEOP_KINDS["images"],
                      "image": _FILEOP_KINDS["images"], "video": _FILEOP_KINDS["videos"]})
_FILEOP_VERBS = {"copy": "Copy", "move": "Move", "delete": "Delete", "rename": "Rename", "mkdir": "New folder"}


def _op_zone(path):
    """'free' if System 32 may change things at this path (else the reason)."""
    cls = _classify_write(path)
    return "free" if cls["zone"] == "free" else (cls.get("reason") or "that location is off-limits")


def _op_private(path):
    try:
        import s32_findex
        n = os.path.normcase(os.path.abspath(path)).rstrip("\\/") + os.sep
        return any(n.startswith(p) for p in s32_findex._private_dirs())
    except Exception:
        return False


def _op_expand(paths, filt, recursive):
    """→ (items [abs paths], error). A folder + filter → the matching files in it."""
    import fnmatch
    out = []
    want = None
    f = str(filt or "").strip().lower()
    if f and f not in ("all", "*", "everything", "files"):
        want = _FILEOP_KINDS.get(f)
        pats = None if want else [x.strip() for x in re.split(r"[;,\s]+", f) if x.strip()]
    for raw in paths:
        p = os.path.abspath(os.path.expandvars(os.path.expanduser(str(raw or "").strip().strip('"'))))
        if not raw or not os.path.exists(p):
            return None, f"Not found: {raw}"
        if _op_private(p):
            return None, "That's System 32's private data — it can't be copied, moved or deleted."
        if os.path.isdir(p) and f:
            walker = os.walk(p) if recursive else [(p, [], [x for x in os.listdir(p) if os.path.isfile(os.path.join(p, x))])]
            for d, _dirs, fns in walker:
                for fn in sorted(fns):
                    ext = os.path.splitext(fn)[1].lower()
                    if want is not None:
                        ok = ext in want
                    elif f in ("all", "*", "everything", "files"):
                        ok = True
                    else:
                        ok = any(fnmatch.fnmatch(fn.lower(), pt) for pt in pats)
                    if ok:
                        out.append(os.path.join(d, fn))
                        if len(out) > _FILEOP_MAX:
                            return None, f"That's more than {_FILEOP_MAX} files — narrow it down."
        else:
            out.append(p)
    return out, None


def _free_name(dst):
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(dst)
    if os.path.isdir(dst):
        base, ext = dst, ""
    n = 1
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def _size_of(p):
    try:
        if os.path.isfile(p):
            return os.path.getsize(p)
        return sum(os.path.getsize(os.path.join(d, f)) for d, _x, fs in os.walk(p) for f in fs)
    except OSError:
        return 0


def _fmt_bytes(n):
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.1f} MB" if n >= 2**20 else f"{max(1, n // 1024)} KB"


def _shell_folder_path(reg_name, fallback):
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders")
        val, _ = winreg.QueryValueEx(key, reg_name)
        winreg.CloseKey(key)
        if val and os.path.isdir(val):
            return val
    except Exception:
        pass
    return os.path.join(os.environ.get("USERPROFILE", ""), fallback)


def _op_path(raw):
    """A path as she writes it → the real one. 'desktop\\folder1' meant the operator's desktop
    (it resolved inside System 32's own folder and was refused), and C:\\Users\\<you>\\Desktop\\…
    isn't the desktop when OneDrive moved it (2026-10-30: test2 landed on the hidden one)."""
    p = os.path.expandvars(os.path.expanduser(str(raw or "").strip().strip('"')))
    if not p:
        return p
    parts = re.split(r"[\\/]+", p, maxsplit=1)
    homes = {"desktop": lambda: _get_desktop_path(),
             "documents": lambda: _shell_folder_path("Personal", "Documents"),
             "downloads": lambda: _shell_folder_path("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),
             "pictures": lambda: _shell_folder_path("My Pictures", "Pictures"),
             "videos": lambda: _shell_folder_path("My Video", "Videos"),
             "music": lambda: _shell_folder_path("My Music", "Music")}
    if not os.path.isabs(p) and parts[0].lower() in homes:
        base = homes[parts[0].lower()]()
        return os.path.join(base, parts[1]) if len(parts) > 1 and parts[1] else base
    prof = os.environ.get("USERPROFILE", "")
    if prof and not os.path.exists(p):
        for name in homes:
            plain = os.path.join(prof, name.capitalize())
            real = homes[name]()
            n, pl = os.path.normcase(os.path.abspath(p)), os.path.normcase(plain)
            if os.path.normcase(real) != pl and (n == pl or n.startswith(pl + os.sep)):
                return real + os.path.abspath(p)[len(plain):]
    return p


def _op_near(folder, ref):
    """A folder she named: absolute / desktop\\… as written, a plain name = next to `folder`."""
    p = _op_path(ref)
    return os.path.abspath(p if os.path.isabs(p) else os.path.join(folder, p))


def _rename_is_move(src, new_name):
    """(2026-10-31) A rename whose new_name points at a FOLDER ('test2', 'test2\\pic.png',
    'D:\\x\\') means 'move it there' — it used to be cut to the last part (or refused as
    'already exists'). → a move plan, an error, or None (a plain rename)."""
    raw = str(new_name).replace("\\", "/").strip().strip('"')
    parent = os.path.dirname(src.rstrip("\\/"))
    if raw.endswith("/"):
        into, nn = raw.rstrip("/"), ""
    elif "/" in raw or (len(raw) > 1 and raw[1] == ":"):
        into, nn = raw.rsplit("/", 1) if "/" in raw else (raw, "")
    else:
        cand = os.path.join(parent, raw)
        if not (os.path.isdir(cand) and os.path.normcase(cand) != os.path.normcase(src.rstrip("\\/"))):
            return None
        into, nn = raw, ""
    if not into:
        return None
    dest = _op_near(parent, into)
    z = _op_zone(dest)
    if z != "free":
        return {"error": f"Can't move {src} into {dest} — {z}."}
    if os.path.exists(dest) and not os.path.isdir(dest):
        return {"error": f"{dest} is a file, not a folder."}
    nd = os.path.normcase(dest) + os.sep
    if os.path.isdir(src) and nd.startswith(os.path.normcase(src.rstrip("\\/")) + os.sep):
        return {"error": f"Can't move {src} into itself."}
    warn = [f"new_name pointed at a folder — this MOVES it into {dest}."]
    if not os.path.exists(dest):
        warn.append(f"The folder {dest} will be created.")
    dst = os.path.join(dest, nn or os.path.basename(src.rstrip("\\/")))
    cand = _free_name(dst)
    if cand != dst:
        warn.append(f"{os.path.basename(dst)} already exists there — this one becomes {os.path.basename(cand)}.")
    if os.path.normcase(os.path.dirname(cand)) == os.path.normcase(parent) and not nn:
        return {"error": f"{os.path.basename(src)} is already in {dest}."}
    return {"action": "move", "plan": [(src, cand)], "dest": dest, "warn": warn, "total": _size_of(src)}


def _plan_file_op(action, paths=None, destination="", filter="", recursive=False, new_name=""):
    """Work out exactly what one copy / move / rename / mkdir / delete would do. Touches nothing.
    → {"action", "plan", "dest", "warn", "total"} or {"error"}."""
    if isinstance(paths, str):
        paths = [paths]
    paths = [_op_path(x) for x in (paths or []) if str(x or "").strip()]
    destination = _op_path(destination) if destination else destination
    action = str(action or "").strip().lower()
    action = {"cp": "copy", "duplicate": "copy", "mv": "move", "cut": "move", "remove": "delete",
              "del": "delete", "trash": "delete", "recycle": "delete", "ren": "rename",
              "make_folder": "mkdir", "new_folder": "mkdir", "create_folder": "mkdir"}.get(action, action)
    if action not in _FILEOP_VERBS:
        return {"error": "action must be copy, move, rename, mkdir or delete."}
    if isinstance(paths, str):
        paths = [paths]
    paths = [x for x in (paths or []) if str(x or "").strip()]
    dest = os.path.abspath(os.path.expandvars(os.path.expanduser(str(destination).strip().strip('"')))) if destination else ""
    plan, warn = [], []
    if action == "mkdir":
        target = dest or (paths[0] if paths else "")
        if not target:
            return {"error": "Give the new folder's full path as destination."}
        target = os.path.abspath(target)
        z = _op_zone(target)
        if z != "free":
            return {"error": f"Can't create a folder there — {z}."}
        if os.path.exists(target):
            return {"error": f"{target} already exists."}
        plan = [(None, target)]
    else:
        if not paths:
            return {"error": "Say which files or folders (paths) — find them with search_computer / list_directory first."}
        items, err = _op_expand(paths, filter, recursive)
        if err:
            return {"error": err}
        if not items:
            return {"error": f"Nothing in {', '.join(map(str, paths))} matches '{filter}'."}
        if action in ("move", "delete", "rename"):
            for it in items:
                z = _op_zone(it)
                if z != "free":
                    return {"error": f"Can't {action} {it} — {z}."}
        if action in ("copy", "move") and not dest and str(new_name or "").strip():
            # (2026-10-31) "move these into test2" with test2 written as new_name, not
            # destination. A plain name = a folder beside what she named (an existing one wins).
            _c = [_op_near(os.path.dirname(str(b).rstrip("\\/")), new_name) for b in (paths[0], items[0])]
            dest = next((c for c in _c if os.path.isdir(c)), _c[0])
            warn.append(f"Took new_name as the destination folder: {dest}")
        if action in ("copy", "move"):
            if not dest:
                return {"error": f"Give a destination folder to {action} into."}
            z = _op_zone(dest)
            if z != "free":
                return {"error": f"Can't {action} into {dest} — {z}."}
            if os.path.exists(dest) and not os.path.isdir(dest):
                return {"error": f"{dest} is a file, not a folder."}
            if not os.path.exists(dest):
                warn.append(f"The folder {dest} will be created.")
            taken = set()
            for it in items:
                nd = os.path.normcase(dest) + os.sep
                if os.path.isdir(it) and (os.path.normcase(it) + os.sep == nd or nd.startswith(os.path.normcase(it) + os.sep)):
                    return {"error": f"Can't {action} {it} into itself."}
                if action == "move" and os.path.normcase(os.path.dirname(it)) == os.path.normcase(dest):
                    continue                                          # already there
                dst = os.path.join(dest, os.path.basename(it.rstrip("\\/")))
                cand = _free_name(dst)
                while os.path.normcase(cand) in taken:                 # two sources with one name
                    b2, e2 = os.path.splitext(dst)
                    k = 1
                    while os.path.exists(f"{b2}_{k}{e2}") or os.path.normcase(f"{b2}_{k}{e2}") in taken:
                        k += 1
                    cand = f"{b2}_{k}{e2}"
                taken.add(os.path.normcase(cand))
                if cand != dst:
                    warn.append(f"{os.path.basename(dst)} already exists there — this one becomes {os.path.basename(cand)}.")
                plan.append((it, cand))
            if not plan:
                return {"error": f"Everything is already in {dest}."}
        elif action == "rename":
            if len(items) != 1 or not new_name:
                return {"error": "rename takes one path and new_name (just the new name, no folder)."}
            mv = _rename_is_move(items[0], new_name)
            if mv:
                return mv
            nn = os.path.basename(str(new_name).replace("\\", "/").strip())
            if not nn or nn in (".", ".."):
                return {"error": "new_name must be a plain file/folder name."}
            dst = os.path.join(os.path.dirname(items[0]), nn)
            if os.path.exists(dst):
                return {"error": f"{nn} already exists in that folder."}
            plan = [(items[0], dst)]
        else:   # delete
            plan = [(it, None) for it in items]
            warn.append("Deleted items go to the Recycle Bin (you can restore them).")
    total = sum(_size_of(src) for src, _d in plan if src)
    return {"action": action, "plan": plan, "dest": dest, "warn": warn, "total": total}


def propose_file_op(action, paths=None, destination="", filter="", recursive=False, new_name="", steps=None):
    """Plan a copy / move / rename / mkdir / delete and raise a Confirm card. Touches nothing.
    With `steps`, several of them become ONE card that runs them in order."""
    if steps:
        return _propose_steps(steps)
    pl = _plan_file_op(action, paths, destination, filter, recursive, new_name)
    if pl.get("error"):
        return pl
    action, plan, dest, warn, total = pl["action"], pl["plan"], pl["dest"], pl["warn"], pl["total"]
    pending = {"token": f"fop-{int(time.time() * 1000)}-{random.randint(1000, 9999)}", "action": action,
               "plan": plan, "dest": dest, "total": total, "ts": time.time()}
    _stage_fileop(pending)
    verb = _FILEOP_VERBS[action]
    shown = []
    for src, dst in plan[:12]:
        if action == "mkdir":
            shown.append(dst)
        elif action == "delete":
            shown.append(src)
        elif action == "rename":
            shown.append(f"{os.path.basename(src)} → {os.path.basename(dst)}")
        else:
            shown.append(os.path.basename(src) + ("" if os.path.basename(src) == os.path.basename(dst) else f" → {os.path.basename(dst)}"))
    card = {"type": "fileop_confirm", "token": pending["token"], "action": action, "verb": verb,
            "count": len(plan), "size": _fmt_bytes(total) if total else "", "dest": dest,
            "items": shown, "more": max(0, len(plan) - len(shown)), "warnings": warn[:6],
            "source": (os.path.dirname(plan[0][0]) if plan and plan[0][0] else "")}
    try:
        import s32_config as _cfg
        with _result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _push_result(card, jid)
        _push_done(jid)
    except Exception as e:
        log(f"[S32] file-op card push failed: {e}")
    log(f"[S32][FILES] proposed {action}: {len(plan)} item(s)" + (f" → {dest}" if dest else ""))
    what = f"{len(plan)} item(s)" + (f" ({_fmt_bytes(total)})" if total else "")
    return {"ok": True, "staged": True, "quiet": True, "action": action, "count": len(plan), "dest": dest,
            "note": (f"Proposed: {verb.lower()} {what}" + (f" into {dest}" if dest else "") +
                     ". A Confirm card is in chat — NOTHING happens until the operator presses Confirm. "
                     "Don't call file_operation again for this; tell them what you've set up and that "
                     "they need to confirm it." + (" Notes: " + " ".join(warn[:3]) if warn else ""))}


def _recycle(path):
    """Send to the Recycle Bin (undoable). Windows shell API; elsewhere send2trash if present."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class _SHF(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                        ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort),
                        ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", wintypes.LPCWSTR)]
        op = _SHF()
        op.wFunc = 3                                   # FO_DELETE
        op.pFrom = os.path.abspath(path) + "\0"        # double-NUL terminated list
        op.fFlags = 0x40 | 0x10 | 0x04 | 0x400         # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0 or op.fAnyOperationsAborted:
            raise OSError(f"the Recycle Bin refused it (code {rc})")
        return
    try:
        from send2trash import send2trash
        send2trash(path)
    except ImportError:
        raise OSError("no Recycle Bin available here — not deleted")


# (2026-10-31) Several cards can wait at once — each by its own token. It used to be ONE
# slot, so a second card silently voided the first (its Confirm then said "doesn't match").
_FILEOP_MAX_WAITING = 8


def _stage_fileop(pending):
    q = state.setdefault("pending_fileops", {})
    q[pending["token"]] = pending
    while len(q) > _FILEOP_MAX_WAITING:
        q.pop(next(iter(q)))                    # the oldest waiting card expires
    state["pending_fileop"] = pending           # the latest, as before


def _take_fileop(token):
    """The staged operation for this token, removed (single use) — or None."""
    if not token:
        return None
    p = (state.get("pending_fileops") or {}).pop(token, None)
    if p is not None and state.get("pending_fileop") is p:
        state["pending_fileop"] = None
    return p


def confirm_file_op(token=None):
    """The ONLY path that changes files for file_operation — reached only by the operator's
    Confirm. Re-checks every zone before acting (defence in depth)."""
    pending = _take_fileop(token)
    if not pending:
        return {"error": "That confirmation doesn't match anything staged."}
    if pending.get("action") == "steps":
        return _confirm_steps(pending)
    action = pending["action"]
    done, failed, msg = _run_plan(action, pending["plan"], pending.get("dest"))
    log(f"[S32][FILES] {action} confirmed by operator: {msg}")
    # So her NEXT turn knows what actually happened (the card was confirmed after her reply).
    try:
        state.setdefault("messages", []).append({"role": "user", "content": f"[File operation result: {msg}]"})
    except Exception:
        pass
    return {"ok": not failed or done > 0, "done": done, "failed": failed, "message": msg}


def _run_plan(action, plan, dest="", landed=None):
    """Carry out one planned operation. → (done, failed, message). `landed` (a list) collects
    (action, src, where it really ended up, src size) for each copy/move that finished."""
    done, failed = 0, []
    for src, dst in plan:
        try:
            for pth in [x for x in (src, dst) if x]:
                if pth == src and action == "copy":
                    if _op_private(src) or not os.path.exists(src):
                        raise OSError("source is gone or private")
                    continue
                z = _op_zone(pth if (pth != dst or action != "rename") else os.path.dirname(pth))
                if z != "free":
                    raise OSError(z)
            if action == "copy":
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                dst = _free_name(dst)
                _sz = _size_of(src) if landed is not None else 0
                if os.path.isdir(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
                if landed is not None:
                    landed.append(("copy", src, dst, _sz))
            elif action == "move":
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                _sz = _size_of(src) if landed is not None else 0
                _to = _free_name(dst)
                shutil.move(src, _to)
                if landed is not None:
                    landed.append(("move", src, _to, _sz))
            elif action == "rename":
                if os.path.exists(dst):
                    raise OSError("that name is taken now")
                os.rename(src, dst)
            elif action == "mkdir":
                if not os.path.isdir(dst):          # made meanwhile (another card) = done
                    os.makedirs(dst, exist_ok=False)
            elif action == "delete":
                _recycle(src)
            done += 1
        except Exception as e:
            failed.append(f"{os.path.basename(str(src or dst))}: {str(e)[:120]}")
    verb = {"copy": "Copied", "move": "Moved", "rename": "Renamed", "mkdir": "Created", "delete": "Sent to the Recycle Bin"}[action]
    msg = f"{verb} {done} of {len(plan)}" + (f" → {dest}" if dest else "")
    if failed:
        msg += f". {len(failed)} didn't: " + "; ".join(failed[:5]) + ("…" if len(failed) > 5 else "")
    return done, failed, msg


# ── Chained file operations (steps) ───────────────────────────────────────────
# "Make test2, move test1's files into it, then delete test1" = ONE file_operation with
# steps → ONE Confirm card listing every step → on Confirm they run IN ORDER and stop at the
# first step that doesn't fully work (so test1 is never deleted after a failed move).
def _step_fields(st):
    if isinstance(st, str):
        try:
            st = json.loads(st)
        except Exception:
            return None
    if not isinstance(st, dict):
        return None
    pths = st.get("paths")
    if pths is None:
        pths = st.get("path") or st.get("source") or st.get("sources") or []
    return {"action": st.get("action", ""), "paths": pths,
            "destination": st.get("destination") or st.get("dest") or "",
            "filter": st.get("filter", ""), "recursive": bool(st.get("recursive")),
            "new_name": st.get("new_name", "")}


def _made_by_earlier(path, made):
    n = os.path.normcase(os.path.abspath(_op_path(path))) if path else ""
    return bool(n) and any(n == m or n.startswith(m + os.sep) for m in made)


def _propose_steps(steps):
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except Exception:
            return {"error": "steps must be a list of operations, e.g. [{action: mkdir, destination: …}, {action: move, …}]."}
    if not isinstance(steps, list) or not steps:
        return {"error": "steps must be a list of operations."}
    if len(steps) > 10:
        return {"error": "That's more than 10 steps — split it up."}
    planned, made, shown, warn, total, count = [], [], [], [], 0, 0
    for i, raw in enumerate(steps, 1):
        f = _step_fields(raw)
        if not f:
            return {"error": f"Step {i} isn't an operation (needs action, paths / destination)."}
        pl = _plan_file_op(**f)
        if pl.get("error"):
            # A folder an EARLIER step creates/fills doesn't exist yet — plan it when it runs.
            srcs = f["paths"] if isinstance(f["paths"], list) else [f["paths"]]
            if pl["error"].startswith("Not found") and any(_made_by_earlier(x, made) for x in srcs):
                planned.append({"later": f})
                shown.append(f"{i}. {_FILEOP_VERBS.get(str(f['action']).lower(), f['action'])} "
                              f"{', '.join(map(str, srcs))}" + (f" → {f['destination']}" if f['destination'] else ""))
                continue
            # A folder that's already there needs no making — skip that step, keep going.
            if str(f["action"]).lower() in ("mkdir", "make_folder", "new_folder", "create_folder") \
                    and pl["error"].endswith("already exists."):
                warn.append(f"Step {i}: {pl['error'][:-1]} — skipped.")
                shown.append(f"{i}. (already there) {f['destination'] or f['paths']}")
                planned.append(None)
                continue
            return {"error": f"Step {i}: {pl['error']}"}
        planned.append(pl)
        a = pl["action"]
        if a == "mkdir":
            made.append(os.path.normcase(pl["plan"][0][1]))
        elif a in ("copy", "move") and pl["dest"]:
            made.append(os.path.normcase(pl["dest"]))
        count += len(pl["plan"])
        total += pl["total"]
        head = f"{i}. {_FILEOP_VERBS[a]} {len(pl['plan'])} item(s)" + (f" → {pl['dest']}" if pl["dest"] and a != "mkdir" else "")
        shown.append(head)
        for src, dst in pl["plan"][:3]:
            shown.append("     " + (dst if a == "mkdir" else src if a == "delete" else
                                    f"{os.path.basename(src)} → {os.path.basename(dst)}" if a == "rename" else os.path.basename(src)))
        if len(pl["plan"]) > 3:
            shown.append(f"     … and {len(pl['plan']) - 3} more")
        warn.extend(w for w in pl["warn"] if w not in warn)
    warn.append("Runs in this order, and stops at the first step that doesn't fully work.")
    n = len(steps)
    pending = {"token": f"fop-{int(time.time() * 1000)}-{random.randint(1000, 9999)}", "action": "steps",
               "steps": planned, "raw": steps, "total": total, "ts": time.time()}
    _stage_fileop(pending)
    card = {"type": "fileop_confirm", "token": pending["token"], "action": "steps", "verb": f"{n} steps",
            "count": count, "size": _fmt_bytes(total) if total else "", "dest": "",
            "items": shown, "more": 0, "warnings": warn[:8], "source": ""}
    try:
        import s32_config as _cfg
        with _result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _push_result(card, jid)
        _push_done(jid)
    except Exception as e:
        log(f"[S32] file-op card push failed: {e}")
    log(f"[S32][FILES] proposed {n} steps: " + "; ".join(x for x in shown if not x.startswith("     ")))
    return {"ok": True, "staged": True, "quiet": True, "action": "steps", "count": count,
            "note": (f"Proposed {n} steps in ONE Confirm card: " + "; ".join(x for x in shown if not x.startswith("     ")) +
                     ". NOTHING happens until the operator presses Confirm; then they run in this order. "
                     "Don't call file_operation again for this; tell them what you've set up and that they "
                     "need to confirm it." + (" Notes: " + " ".join(warn[:3]) if warn else ""))}


def _landed_problem(landed):
    """Before a chain DELETES anything: is every copy/move an earlier step made really on
    disk — same size, and (for a move) gone from where it was? → the first problem, or None."""
    for act, src, dst, sz in landed:
        if not os.path.exists(dst):
            return f"{os.path.basename(dst)} isn't at {os.path.dirname(dst)}"
        if sz and _size_of(dst) != sz:
            return f"{os.path.basename(dst)} at {os.path.dirname(dst)} isn't the full size"
        if act == "move" and os.path.exists(src):
            return f"{os.path.basename(src)} is still at {os.path.dirname(src)} after the move"
    return None


def _confirm_steps(pending):
    lines, all_failed, done_total = [], [], 0
    landed = []          # every copy/move this chain finished — checked before any delete
    steps = pending.get("steps") or []
    for i, pl in enumerate(steps, 1):
        if pl is None:
            lines.append(f"{i}. already there — skipped")
            continue
        if pl.get("later"):
            pl = _plan_file_op(**pl["later"])
            if pl.get("error"):
                lines.append(f"{i}. {pl['error']}")
                all_failed.append(pl["error"])
                if i < len(steps):
                    lines.append(f"Stopped before step {i + 1} — nothing after it was done.")
                break
        if pl["action"] == "delete" and landed:
            prob = _landed_problem(landed)
            if prob:
                why = f"{i}. Didn't delete anything — checked first and {prob}."
                lines.append(why)
                all_failed.append(why)
                log(f"[S32][FILES] chain delete held back: {prob}")
                if i < len(steps):
                    lines.append(f"Stopped before step {i + 1} — nothing after it was done.")
                break
        done, failed, msg = _run_plan(pl["action"], pl["plan"], pl.get("dest"), landed)
        done_total += done
        lines.append(f"{i}. {msg}")
        if failed:
            all_failed.extend(failed)
            if i < len(steps):
                lines.append(f"Stopped before step {i + 1} — nothing after it was done.")
            break
    msg = "  ".join(lines)
    log(f"[S32][FILES] {len(steps)} steps confirmed by operator: {msg}")
    try:
        state.setdefault("messages", []).append({"role": "user", "content": f"[File operation result: {msg}]"})
    except Exception:
        pass
    return {"ok": not all_failed or done_total > 0, "done": done_total, "failed": all_failed, "message": msg}


def cancel_file_op(token=None):
    pending = _take_fileop(token)
    if pending:
        try:
            state.setdefault("messages", []).append(
                {"role": "user", "content": (f"[File operation cancelled by the operator: {len(pending.get('steps') or [])} steps — nothing was changed]"
                                             if pending.get("action") == "steps" else
                                             f"[File operation cancelled by the operator: {pending['action']} of {len(pending['plan'])} item(s) — nothing was changed]")})
        except Exception:
            pass
        return {"ok": True, "cancelled": True}
    return {"ok": True, "cancelled": False}


# ── Image sorting by category (vision-classify → plan → operator Copy/Move) ────
# It scans each image with the local vision model, bins it into a named category
# (operator-supplied, or invented with a running list for consistency), creates a
# folder per category inside the SAME directory, and — only after the operator
# picks Copy or Move from the bar above the chat — copies/moves files in. Confined
# to the free-write zone (same rules as writes); move never deletes on failure;
# filename collisions are auto-suffixed; "unsorted" catches anything uncertain.
_SORT_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_SORT_CAP      = 5000


def _sanitize_category(label):
    if not label:
        return "unsorted"
    label = label.strip().splitlines()[0].strip().lower()
    label = re.sub(r"^(answer|category|label)\s*[:=]?\s*", "", label)   # drop sentinel prefix
    label = re.sub(r"^(a|an|the)\s+", "", label)                        # drop leading article
    label = re.sub(r"[^a-z0-9 _-]", "", label).strip()
    label = re.sub(r"\s+", "_", label)[:40].strip("_-")
    # 3+ words means we grabbed a sentence fragment, not a category → unsorted.
    if label.count("_") >= 2:
        return "unsorted"
    if label in ("", "unsorted", "unknown", "unclear", "none", "n_a", "na", "other", "image", "photo"):
        return "unsorted"
    return label


def _resolve_dir_shortcut(directory):
    """Map bare 'desktop'/'downloads'/'pictures'/'documents' to the user's folder."""
    d = (directory or "").strip()
    key = d.lower().strip("\\/ ")
    prof = os.environ.get("USERPROFILE", "")
    known = {"desktop": "Desktop", "downloads": "Downloads",
             "pictures": "Pictures", "documents": "Documents"}
    if key in known and prof:
        return os.path.join(prof, known[key])
    return d


def _classify_image(path, categories=None, known=None, timeout=120):
    """Vision-classify one image → a folder-safe label. None ⇒ vision unavailable;
    '' ⇒ failed (caller treats as 'unsorted'). Handles Qwen <think> reasoning."""
    import base64 as _b64, io as _io
    import urllib.request as _ureq
    if not state.get("vision_ready", False):
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if PILImage:
            img = PILImage.open(_io.BytesIO(raw)).convert("RGB")
            img.thumbnail((512, 512), PILImage.LANCZOS)
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=75)
            b64 = _b64.b64encode(buf.getvalue()).decode()
            mime = "image/jpeg"
        else:
            b64 = _b64.b64encode(raw).decode()
            mime = "image/png"
        if categories:
            instr = ("Choose the single best category for this image from this list: "
                     + ", ".join(categories) + ". If none truly fit, use 'unsorted'.")
        else:
            hint = (" Prefer reusing one of these if it fits: " + ", ".join(known)) if known else ""
            instr = ("Pick ONE short category (one or two words, lowercase) for the main subject "
                     "of this image — e.g. animals, food, people, screenshots, cars, nature, anime, "
                     "documents." + hint)
        # Force a parseable sentinel on the FINAL line so we can recover the answer
        # whether it lands in content or in a separate reasoning field.
        sys_p = ("You label an image with one short category. Keep it brief, then finish your "
                 "reply with a line in exactly this form:\nANSWER: <category>\n"
                 "where <category> is one or two lowercase words. /no_think")
        payload = {
            "model": CONFIG.get("lmstudio_model", ""),
            "messages": [
                {"role": "system", "content": sys_p},
                {"role": "user", "content": [
                    {"type": "text", "text": instr + "\nEnd your reply with:  ANSWER: <category>  /no_think"},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ]},
            ],
            "max_tokens": 512, "temperature": 0.1, "stream": False,
        }
        host = CONFIG.get("lmstudio_host", "http://localhost:1234")
        req = _ureq.Request(f"{host}/v1/chat/completions",
                            data=json.dumps(payload).encode(),
                            headers={"Content-Type": "application/json"})
        with _ureq.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        msg = resp.get("choices", [{}])[0].get("message", {}) or {}
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        blob = content + "\n" + reasoning
        # Trust ONLY an explicit sentinel (first two words of it) or short clean content.
        # Never fall back to raw reasoning text — that produces sentence-fragment folders.
        mm = re.search(r"answer\s*[:=]\s*([^\n]+)", blob, re.I)
        if mm:
            parts = mm.group(1).split()
            if len(parts) >= 2 and parts[1].lower().strip(",./&") in ("or", "and", "/", "&"):
                parts = parts[:1]
            candidate = " ".join(parts[:2])
        else:
            first = content.strip().splitlines()[0].strip() if content.strip() else ""
            candidate = first if (first and len(first.split()) <= 2) else ""
        log(f"[S32] classify {os.path.basename(path)}: content={content[:24]!r} "
            f"reason_len={len(reasoning)} -> {candidate[:30]!r}")
        return _sanitize_category(candidate)
    except Exception as e:
        log(f"[S32] classify_image failed for {os.path.basename(path)}: {str(e)[:100]}")
        return ""


def sort_images(directory, categories=None, max_images=_SORT_CAP):
    """Enumerate top-level images in `directory` and stage them in state['pending_sort']
    WITHOUT classifying yet. The operator picks Copy or Move from the bar above the chat,
    which calls run_sort() — that classifies and moves each image one at a time, live."""
    directory = _resolve_dir_shortcut(directory)
    cls = _classify_write(directory)
    cdir, zone = cls["path"], cls["zone"]
    if zone in ("invalid", "remote", "blocked"):
        _push_note(f"⛔ Can't sort there: {cls['reason']}\n    {cdir}", header="⬡ Sort")
        return {"refused": True, "reason": cls["reason"], "path": cdir}
    if zone == "staging":
        _push_note("⛔ That's my own drive — I don't reorganise files there.", header="⬡ Sort")
        return {"refused": True, "reason": "own drive (I:)", "path": cdir}
    if not os.path.isdir(cdir):
        return {"error": f"Not a folder: {cdir}"}
    if not state.get("vision_ready", False):
        return {"error": "I can't see images right now — the vision model isn't loaded."}
    if state.get("sort_running"):
        return {"error": "A sort is already running — let it finish first."}
    try:
        names = sorted(n for n in os.listdir(cdir)
                       if os.path.isfile(os.path.join(cdir, n))
                       and os.path.splitext(n)[1].lower() in _SORT_IMG_EXTS)
    except Exception as e:
        return {"error": str(e)}
    if not names:
        return {"error": f"No images found in {cdir}"}

    try:
        max_images = max(1, min(int(max_images or _SORT_CAP), _SORT_CAP))
    except Exception:
        max_images = _SORT_CAP
    capped = len(names) > max_images
    names = names[:max_images]

    cats = None
    if categories:
        if isinstance(categories, str):
            cats = [c for c in re.split(r"[,/]| and ", categories) if c.strip()]
        elif isinstance(categories, (list, tuple)):
            cats = [str(c) for c in categories if str(c).strip()]
        cats = [_sanitize_category(c) for c in (cats or [])] or None

    pending = {
        "token":      f"sort-{int(time.time() * 1000)}",
        "dir":        cdir,
        "files":      names,
        "categories": cats,
        "total":      len(names),
        "capped":     capped,
        "ts":         time.strftime("%H:%M:%S"),
    }
    state["pending_sort"] = pending
    cap_note = f" (first {max_images}; folder has more)" if capped else ""
    cat_note = (f" into: {', '.join(cats)}" if cats else " — naming categories as I go")
    _push_note(f"⬡ Ready to sort {len(names)} image(s) in {cdir}{cat_note}.{cap_note}\n"
               f"    Pick Copy or Move above the chat — I sort them live, one at a time.",
               header="⬡ Sort")
    return {"ok": True, "staged": True, "dir": cdir, "total": len(names),
            "note": ("Images found and the Copy/Move bar is up. The operator picks Copy or Move "
                     "to start the live sort. Do NOT call sort_images again — wait for them.")}


def _collision_free(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def run_sort(mode="move"):
    """Start the live sort in a background thread so the folder fills in real time and
    the chat stays responsive. mode ∈ {'move','copy'}."""
    pending = state.get("pending_sort")
    if not pending:
        return {"error": "No sort is staged."}
    if state.get("sort_running"):
        return {"error": "A sort is already running."}
    mode = "copy" if str(mode).lower().startswith("c") else "move"
    if _classify_write(pending["dir"])["zone"] != "free":
        state["pending_sort"] = None
        _push_note("⛔ Stopped — that folder is no longer sortable.", header="⬡ Sort")
        return {"refused": True}
    state["sort_running"] = True
    state["sort_cancel"] = False
    state["pending_sort"] = None          # hide the bar; the worker has its own copy
    threading.Thread(target=_sort_worker, args=(pending, mode), daemon=True).start()
    return {"ok": True, "started": True, "mode": mode, "total": pending["total"]}


def _sort_worker(pending, mode):
    """Live per-image loop: classify one → make/place into its folder → move/copy it
    now → next, so the folder fills in real time. Confined per folder; collision-safe;
    move never deletes on failure."""
    cdir, files = pending["dir"], pending["files"]
    cats = pending.get("categories")
    known = list(cats) if cats else []
    made, counts = set(), {}
    done = failed = 0
    verb = "Copying" if mode == "copy" else "Moving"
    # Small sort → a chat line per image (you watch it work). Large sort → periodic
    # progress only (~40 updates) so thousands of bubbles don't flood chat; the
    # folder itself is the real-time view either way.
    live = len(files) <= 60
    step = max(1, len(files) // 40)
    extra = "" if live else " — progress posts periodically; watch the folder live"
    _push_note(f"⬡ {verb} {len(files)} image(s) live in {cdir}{extra}…", header="⬡ Sort")
    try:
        for fn in files:
            if state.get("sort_cancel"):
                _push_note(f"✗ Sort stopped early — {done} done so far.", header="⬡ Sort")
                break
            src = os.path.join(cdir, fn)
            if not os.path.isfile(src):
                continue
            label = _classify_image(src, categories=cats, known=(None if cats else known))
            if label is None:
                _push_note("⛔ Vision became unavailable — stopping sort.", header="⬡ Sort")
                break
            if not label:
                label = "unsorted"
            if not cats and label != "unsorted" and label not in known:
                known.append(label)
            dest_dir = os.path.join(cdir, label)
            if _classify_write(dest_dir)["zone"] != "free":
                failed += 1
                continue
            try:
                if not os.path.isdir(dest_dir):
                    os.makedirs(dest_dir, exist_ok=True)
                    if label not in made:
                        made.add(label)
                        _push_note(f"📁 new folder: {label}", header="⬡ Sort")
                dst = _collision_free(os.path.join(dest_dir, fn))
                if mode == "copy":
                    shutil.copy2(src, dst)
                else:
                    shutil.move(src, dst)
                done += 1
                counts[label] = counts.get(label, 0) + 1
                log(f"[S32] sort {fn} -> {label}")
                if live:
                    _push_note(f"  {fn}  →  {label}", header="⬡ Sort")
                elif done % step == 0:
                    top = " · ".join(f"{k} ({v})" for k, v in
                                     sorted(counts.items(), key=lambda x: -x[1])[:5])
                    _push_note(f"  … {done}/{len(files)} sorted — {top}", header="⬡ Sort")
            except Exception as e:
                failed += 1
                log(f"[S32] sort {mode} failed {fn}: {str(e)[:60]}")
        summary = " · ".join(f"{k} ({v})" for k, v in sorted(counts.items(), key=lambda x: -x[1]))
        verb2 = "Copied" if mode == "copy" else "Moved"
        msg = f"✓ {verb2} {done} image(s) into {len(counts)} folder(s): {summary or '—'}"
        if failed:
            msg += f"  ⚠ {failed} couldn't be done."
        _push_note(msg, header="⬡ Sort")
        log(f"[S32] sort done ({mode}): {done} ok, {failed} failed in {cdir}")
    finally:
        state["sort_running"] = False
        state["sort_cancel"] = False


def cancel_sort():
    """Cancel a staged sort, or ask a running live sort to stop after the current image."""
    p = state.get("pending_sort")
    state["pending_sort"] = None
    if state.get("sort_running"):
        state["sort_cancel"] = True
        return {"ok": True, "cancelling": True}
    if p:
        _push_note("✗ Sort cancelled — nothing moved.", header="⬡ Sort")
    return {"ok": True, "cancelled": True}


def _looks_text(path):
    """A file is readable as text if it IS text — whatever its extension (.cpp, .h, .psc,
    .lua, .xml…). 2026-10-29: read_file refused a mod's .cpp because .cpp wasn't on a list."""
    try:
        with open(path, "rb") as f:            # read-only
            head = f.read(8192)
    except OSError:
        return False
    if not head:
        return True
    if b"\x00" in head:
        return False
    for enc in ("utf-8", "cp1252"):
        try:
            head.decode(enc)
            return True
        except UnicodeDecodeError:
            continue
    return False


def fs_read(path, max_chars=12000):
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(path):
        candidates = [
            os.path.join(WORKFLOW_DIR, path),
            os.path.join(CONFIG.get("comfy_image_input", ""), path),
            path,
        ]
        for c in candidates:
            if os.path.isfile(c):
                path = c
                break
    ext = os.path.splitext(path)[1].lower()
    if ext not in TEXT_EXTS and ext and not (os.path.isfile(path) and _looks_text(path)):
        return {"error": f"Cannot read binary file type: {ext}"}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        size = len(content)
        # Compact JSON to save tokens
        if ext == ".json":
            try:
                content = json.dumps(json.loads(content), separators=(",", ":"))
            except Exception:
                pass
        # Truncate if still too large
        if len(content) > max_chars:
            content = (content[:max_chars] + f"\n... [first {max_chars:,} of {size:,} characters — read the rest "
                       f"with show_file(path, offset={max_chars})]")
        return {"path": path, "content": content, "truncated": size > max_chars,
                "size_mb": round(size / 1048576, 4)}
    except PermissionError:
        return {"error": f"Permission denied: {path}"}
    except Exception as e:
        return {"error": str(e)}

def fs_drives():
    log("[S32][DRIVES] Scanning drives via wmic...")
    drives = []

    # Use shell to get all drives - more reliable than os.path.exists for
    # network drives, virtual drives, and removable media
    try:
        r = subprocess.run(
            ["wmic", "logicaldisk", "get",
             "Caption,Size,FreeSpace,VolumeName,DriveType",
             "/format:csv"],
            capture_output=True, text=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW
        )
        log(f"[S32][DRIVES] wmic returncode={r.returncode} lines={len(r.stdout.splitlines())}")
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) < 5 or not parts[1] or parts[1] == "Caption":
                    continue
                try:
                    caption    = parts[1].strip()   # e.g. "C:"
                    drive_type = parts[2].strip()   # 2=removable,3=fixed,4=network,5=optical
                    free       = int(parts[3].strip() or 0)
                    size       = int(parts[4].strip() or 0)
                    label      = parts[5].strip() if len(parts) > 5 else ""
                    path       = caption + "\\"
                    type_names = {"2":"removable","3":"fixed","4":"network",
                                  "5":"optical","6":"ram disk"}
                    drives.append({
                        "drive":    caption,
                        "label":    label,
                        "path":     path,
                        "type":     type_names.get(drive_type, drive_type),
                        "total_gb": round(size / 1073741824, 1) if size else 0,
                        "free_gb":  round(free / 1073741824, 1) if free else 0,
                        "used_gb":  round((size - free) / 1073741824, 1) if size else 0,
                    })
                    log(f"[S32][DRIVES] Found: {caption} label='{label}' type={drive_type} size={size}")
                except Exception:
                    continue
    except Exception:
        pass

    # Fallback to Python if wmic failed
    if not drives:
        import ctypes
        for letter in string.ascii_uppercase:
            d = f"{letter}:\\"
            try:
                dtype = ctypes.windll.kernel32.GetDriveTypeW(d)
                if dtype < 2:
                    continue
                total, used, free = shutil.disk_usage(d)
                label = ""
                try:
                    vol_buf = ctypes.create_unicode_buffer(261)
                    ctypes.windll.kernel32.GetVolumeInformationW(
                        d, vol_buf, 261, None, None, None, None, 0)
                    label = vol_buf.value
                except Exception:
                    pass
                drives.append({
                    "drive":    f"{letter}:",
                    "label":    label,
                    "path":     d,
                    "total_gb": round(total / 1073741824, 1),
                    "used_gb":  round(used  / 1073741824, 1),
                    "free_gb":  round(free  / 1073741824, 1),
                })
            except Exception:
                pass

    return {
        "drives":    drives,
        "next_step": "Call index_search(query) to find a specific location, or explore_tree('DRIVE:\\\\', depth=1) to map a drive. Do NOT call get_drives again."
    }

def fs_sysinfo():
    info = {}
    paths = {
        "checkpoints": os.path.join(CONFIG["models_dir"], "checkpoints"),
        "loras":       CONFIG["loras_dir"],
        "datasets":    CONFIG["datasets_dir"],
        "out_images":  CONFIG["out_images"],
        "out_videos":  CONFIG["out_videos"],
    }
    for k, p in paths.items():
        if os.path.exists(p):
            try:
                items = os.listdir(p)
                info[k] = {"path": p, "count": len(items), "sample": items[:20]}
            except Exception as e:
                info[k] = {"path": p, "error": str(e)}
        else:
            info[k] = {"path": p, "exists": False}
    return {"info": info}

def fs_get_output_image(position=1, folder="images", source="output"):
    """
    Recall a previously generated output by position.
    source='output'  - from the output folder, newest first (position 1 = most recent)
    source='session' - from this session's image generation order (position 1 = first generated)
    folder (source='output' only): one of
        'images' | 'videos' | '3d' | 'loras' | 'checkpoints' | 'dataset'
    Recalling sets the matching last_*_path so the item can be reused for
    context / chat injection on the next turn. Images and videos render inline;
    other types return a file reference (path + filename).
    """
    if source == "session":
        imgs = state["session_images"]
        if not imgs:
            return {"error": "No images generated in this session yet."}
        position = max(1, int(position))
        if position > len(imgs):
            return {"error": f"Only {len(imgs)} image(s) generated this session, can't get #{position}"}
        entry = imgs[position - 1]
        path = entry["path"]
        raw  = entry.get("bytes")
        if raw is None:                       # bytes were trimmed to bound memory — reload from disk
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except Exception:
                raw = None
        if raw is not None:
            state["last_image_bytes"] = raw
        state["last_image_path"]  = path
        return {
            "type":     "image",
            "url":      f"/img?path={urllib.parse.quote(path)}",
            "filename": os.path.basename(path),
            "position": position,
            "total":    len(imgs),
            "prompt":   entry.get("prompt", ""),
            "message":  f"Loaded session image #{position} of {len(imgs)}: {os.path.basename(path)}"
        }

    # source == 'output' - recall the most-recent output of a given type, newest first.
    # `folder` selects which output stream to recall from. Each type maps to its own
    # output dir, the file extensions it actually produces, a UI type label, and the
    # state slot that remembers "last X" so it can be reused for chat injection.
    OUTPUT_KINDS = {
        "images":      ("out_images",      {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"},       "image",      "last_image_path"),
        "videos":      ("out_videos",      {".mp4", ".webm", ".mov", ".avi", ".mkv"},                "video",      "last_video_path"),
        "3d":          ("out_3d",          {".glb", ".obj", ".ply", ".stl", ".fbx", ".gltf", ".usdz"}, "model",    "last_3d_path"),
        "loras":       ("out_loras",       {".safetensors", ".ckpt", ".pt", ".pth"},                 "lora",       "last_lora_path"),
        "checkpoints": ("out_checkpoints", {".safetensors", ".ckpt", ".pt", ".pth", ".gguf"},        "checkpoint", "last_checkpoint_path"),
        "dataset":     ("out_dataset",     None,                                                     "dataset",    "last_dataset_path"),
    }
    # normalize friendly aliases so the model doesn't have to be precise
    folder = (folder or "images").lower()
    folder = {"image": "images", "video": "videos", "lora": "loras",
              "checkpoint": "checkpoints", "datasets": "dataset",
              "3d_model": "3d", "3d_models": "3d", "model": "3d"}.get(folder, folder)
    if folder not in OUTPUT_KINDS:
        return {"error": f"Unknown output type '{folder}'. "
                         f"Use one of: {', '.join(OUTPUT_KINDS)}"}

    cfg_key, exts, type_label, state_key = OUTPUT_KINDS[folder]
    out_dir = CONFIG.get(cfg_key, "")
    if not out_dir or not os.path.exists(out_dir):
        return {"error": f"Output folder not found for '{folder}': {out_dir}"}

    try:
        files = []
        for f in os.scandir(out_dir):
            # datasets may be folders; every other type must be a file with a matching ext
            if exts is None:
                files.append((f.stat().st_mtime, f.path, f.name))
            elif f.is_file() and os.path.splitext(f.name)[1].lower() in exts:
                files.append((f.stat().st_mtime, f.path, f.name))
        files.sort(reverse=True)  # newest first; position 1 == most recent
    except Exception as e:
        return {"error": str(e)}

    if not files:
        return {"error": f"No {folder} found in {out_dir}"}

    position = max(1, int(position))
    if position > len(files):
        return {"error": f"Only {len(files)} {folder} item(s) in output folder, can't get position {position}"}

    mtime, path, name = files[position - 1]
    # Remember the most-recent path for this type so the next turn can reuse it
    # for context / chat injection. This is the whole point of recall.
    state[state_key] = path
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))

    # Images and videos render inline through the existing /img route; other
    # artifact types (3d / loras / checkpoints / datasets) return a file reference.
    if type_label in ("image", "video"):
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception as e:
            return {"error": str(e)}
        if type_label == "image":
            state["last_image_bytes"] = raw  # keep for animation use
        return {
            "type":     type_label,
            "url":      f"/img?path={urllib.parse.quote(path)}",
            "filename": name,
            "position": position,
            "total":    len(files),
            "modified": when,
            "message":  f"Loaded {type_label} #{position} of {len(files)}: {name}",
        }

    return {
        "type":     type_label,
        "path":     path,
        "filename": name,
        "position": position,
        "total":    len(files),
        "modified": when,
        "message":  f"Recalled {type_label} #{position} of {len(files)}: {name} "
                    f"(set as last {type_label} for reuse)",
    }

# ── Image browser ────────────────────────────────────────────────────────────

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}

# Named folders the browser can see - maps display name → config key or literal path
def _browser_folders():
    return {
        "Generated Images":  CONFIG.get("out_images",  ""),
        "Generated Videos":  CONFIG.get("out_videos",  ""),
        "Generated 3D":      CONFIG.get("out_3d",      ""),
        "ComfyUI Input":     CONFIG.get("comfy_image_input",  ""),
        "ComfyUI Output":    CONFIG.get("comfy_image_output", ""),
        "Datasets":          CONFIG.get("datasets_dir", ""),
        "LoRAs":             CONFIG.get("loras_dir",    ""),
    }

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

def fs_show_images(paths=None, folder=None, count=12, random_sample=False, page=1):
    """
    Display images inline in chat.
    random_sample=True pulls from the whole folder randomly instead of most recent.
    """
    outputs = []
    out_images = CONFIG.get("out_images", "")

    if not paths and folder:
        # Try exact path first, then browser folder shortcut, never fall back to out_images
        if os.path.isdir(folder):
            folder_path = folder
        else:
            folder_path = _browser_folders().get(folder, "")
        if not folder_path or not os.path.exists(folder_path):
            return {"error": f"Folder not found: {folder}. Use an absolute path or a shortcut (output, desktop, reference, etc.)"}
        try:
            files = []
            with os.scandir(folder_path) as it:
                for e in it:
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                        try:
                            files.append((e.stat().st_mtime, e.path))
                        except Exception:
                            pass
            total = len(files)
            cap = max(1, min(int(count), 24))
            if random_sample and total > cap:
                # Random sample from entire library
                sampled = random.sample(files, cap)
                paths = [p for _, p in sampled]
            else:
                files.sort(reverse=True)
                page = max(1, int(page))
                start = (page - 1) * cap
                end   = start + cap
                paths = [p for _, p in files[start:end]]
                total_pages = max(1, (total + cap - 1) // cap)
                if not paths:
                    return {"error": f"No images on page {page}. Total pages: {total_pages}"}
            # Remember where we browsed so it can return next turn
            state["last_browsed_folder"] = folder_path
        except Exception as e:
            return {"error": str(e)}

    if not paths:
        return {"error": "No paths or folder specified. Use find_file to locate images first, then pass the paths here. Or specify a folder with an absolute path."}

    for path in paths[:24]:
        path = os.path.expandvars(os.path.expanduser(str(path)))

        # Normalize drive letters
        if len(path) == 1 and path.isalpha():
            path = path.upper() + ":\\"
        elif len(path) == 2 and path[1] == ":":
            path = path + "\\"

        # Only resolve relative paths - absolute paths go directly
        if not os.path.isabs(path):
            resolved = False
            for base in [out_images, CONFIG.get("comfy_image_output", ""), os.getcwd()]:
                if base:
                    candidate = os.path.join(base, path)
                    if os.path.isfile(candidate):
                        path = candidate
                        resolved = True
                        break
            if not resolved:
                outputs.append({"type": "image_error", "path": path, "error": "File not found (relative path could not be resolved)"})
                continue

        # Check file exists - shell fallback for OneDrive/network files
        # (No `cmd /c if exist "<path>"` fallback — a path is never pasted into a shell
        # command; a crafted one could run commands. 2026-10-29.)
        file_ok = os.path.isfile(path)

        if not file_ok:
            outputs.append({"type": "image_error", "path": path, "error": "File not found"})
            continue
        ext = os.path.splitext(path)[1].lower()
        if ext not in IMAGE_EXTS:
            outputs.append({"type": "image_error", "path": path, "error": f"Unsupported type: {ext}"})
            continue
        outputs.append({
            "type":     "image",
            "url":      f"/img?path={urllib.parse.quote(path)}",
            "filename": os.path.basename(path),
            "saved":    path,
        })
        index_image(path)          # feed the visual index with everything it shows

    if not outputs:
        return {"error": "No images could be loaded."}
    if len(outputs) == 1:
        return outputs[0]
    return {
        "type":    "multi",
        "outputs": outputs,
        "folder":  state.get("last_browsed_folder", ""),
        "note":    f"Showing {len(outputs)} images. Use same folder path to return here next turn.",
    }

# ── Generation tools ──────────────────────────────────────────────────────────



# ════════════════════════════════════════════════════════════════════════════
#  File Manager — a confined, drag-droppable filesystem sandbox on the USB.
#  Every operation is restricted to a small set of allowed ROOTS. _fm_resolve is
#  the safety spine: it realpath-resolves (root, relpath) and verifies the result
#  stays INSIDE the root, so no ".." traversal, symlink, or absolute path can ever
#  reach a live file or the OS. (F1: roots + list + mkdir. Store/delete/offload
#  land in later grains.)
# ════════════════════════════════════════════════════════════════════════════

def _fm_out_base():
    out = CONFIG.get("out_images", "")
    return os.path.dirname(out) if out else (REFERENCE_DIR or _script_dir)


def fm_roots():
    """The allowed top-level areas the manager may touch (created on demand)."""
    base = _fm_out_base()
    roots = {
        "inputs":  {"label": "Inputs · drop zone", "path": os.path.join(base, "inputs")},
        "outputs": {"label": "Outputs",            "path": base},
    }
    for r in roots.values():
        try:
            os.makedirs(r["path"], exist_ok=True)
        except Exception:
            pass
    return roots


def _fm_resolve(root, relpath=""):
    """Resolve (root, relpath) to an absolute path GUARANTEED to stay inside the
    root. Returns (abspath, root_abspath) or raises ValueError on any escape."""
    roots = fm_roots()
    if root not in roots:
        raise ValueError("unknown root")
    root_abs = os.path.realpath(roots[root]["path"])
    rel = (relpath or "").replace("\\", "/").strip("/")
    target = os.path.realpath(os.path.join(root_abs, rel))
    if target != root_abs and not target.startswith(root_abs + os.sep):
        raise ValueError("path escapes the sandbox")
    return target, root_abs


def fm_root_list():
    """The roots, for the manager's sidebar."""
    out = []
    for key, r in fm_roots().items():
        out.append({"root": key, "label": r["label"], "path": r["path"]})
    return {"roots": out}


def fm_list(root, relpath=""):
    """List one directory inside a root: folders first, then files (name/size/mtime)."""
    try:
        target, root_abs = _fm_resolve(root, relpath)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(target):
        return {"error": "Not a directory."}
    dirs, files = [], []
    try:
        for e in os.scandir(target):
            try:
                if e.is_dir():
                    dirs.append({"name": e.name, "kind": "dir"})
                else:
                    st = e.stat()
                    files.append({"name": e.name, "kind": "file",
                                  "size": st.st_size, "mtime": int(st.st_mtime)})
            except Exception:
                continue
    except Exception as ex:
        return {"error": str(ex)[:120]}
    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    rel = os.path.relpath(target, root_abs)
    rel = "" if rel == "." else rel.replace("\\", "/")
    return {"root": root, "relpath": rel, "label": fm_roots()[root]["label"],
            "dirs": dirs, "files": files, "count": len(dirs) + len(files)}


def fm_mkdir(root, relpath, name):
    """Create a folder inside the sandbox (confined)."""
    name = os.path.basename((name or "").strip().replace("\\", "/"))
    if not name or name in (".", ".."):
        return {"error": "Invalid folder name."}
    sub = (relpath + "/" + name) if relpath else name
    try:
        target, _ = _fm_resolve(root, sub)
    except ValueError as e:
        return {"error": str(e)}
    try:
        os.makedirs(target, exist_ok=True)
        log(f"[S32][FM] mkdir {root}:/{sub}")
        return {"ok": True, "name": name}
    except Exception as e:
        return {"error": str(e)[:120]}
