# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_docs.py — turn dropped-in files into a compact, parse-friendly prompt block.

Why this exists:
  Injecting whole files into the prompt worked for one small file but blew up when
  many/large files were dropped at once — the prompt got so large that llama-server's
  prompt-processing ran past the socket read timeout (the cancellable_post
  TimeoutError). This module keeps the prompt small while keeping it useful:

  Pipeline:
    1. structure-aware chunking  — Python via `ast` (functions / classes / methods),
       other text by line-windows.
    2. skeleton (structure map)  — one compact line per symbol with its line range,
       so the model always sees the whole shape of every file.
    3. BM25 ranking              — score chunks against the user's request and inject
       only the most relevant ones, in full, under a char budget.
    4. save full copies          — so anything not shown can still be read on demand
       with show_file('<saved path>').

  Stdlib only, no side effects on import. Safe to import; degrades gracefully if a
  file can't be parsed (falls back to text chunking for that file).
"""

import ast
import math
import os
import re
import time

# Inject everything verbatim below this size — small batches don't need the
# skeleton machinery and reading the whole thing is strictly better.
# Full injection is only worth it when the file is small enough that reading it
# whole is also CHEAP. A big single file dumped verbatim costs more prefill than
# the ranked digest of many files (that was the "one file slower than fifteen"
# regression). Above these sizes, ranked mode (structure map + relevant chunks)
# is both faster and sufficient — and show_file now paginates correctly for the
# rest. ~40-50K chars ≈ ~12K tokens of prefill, in line with the ranked path.
SMALL_TOTAL   = 40000    # chars: any batch up to this total → inject everything
FULL_SINGLE   = 50000    # chars: a single attached file up to this size → inject in full
CHUNK_BUDGET  = 60000    # chars of full chunk bodies to inject when large/ranked
PER_CHUNK_CAP = 16000    # one giant function can't eat the whole budget
MAX_CHUNKS    = 50       # cap injected chunk count (header overhead + focus)
MAX_SKEL_FILE = 120      # cap structure-map lines per file
MAX_SKEL_ALL  = 700      # cap structure-map lines overall
ATTACH_TTL    = 86400    # seconds: prune saved attachments older than this

_TOK = re.compile(r"[a-z0-9_]+")


def _sanitize(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name) or "file"


def _tokens(s):
    return _TOK.findall(s.lower())


# ── Chunking ──────────────────────────────────────────────────────────────────

def _py_chunks(text):
    """[(qualname, start_line, end_line, source)] for top-level funcs, classes,
    and methods. Returns None if the text doesn't parse as Python."""
    try:
        tree = ast.parse(text)
    except Exception:
        return None
    lines = text.splitlines()
    DEF = (ast.FunctionDef, ast.AsyncFunctionDef)

    def span(node):
        start = node.lineno
        if getattr(node, "decorator_list", None):
            start = min(start, min(d.lineno for d in node.decorator_list))
        end = getattr(node, "end_lineno", node.lineno)
        return start, end

    def src(s, e):
        return "\n".join(lines[s - 1:e])

    chunks = []
    for node in tree.body:
        if isinstance(node, DEF):
            s, e = span(node)
            chunks.append((node.name, s, e, src(s, e)))
        elif isinstance(node, ast.ClassDef):
            cs, ce = span(node)
            methods = [n for n in node.body if isinstance(n, DEF)]
            first_m = min((span(m)[0] for m in methods), default=ce + 1)
            header_end = max(cs, first_m - 1)
            chunks.append((node.name + " (class header)", cs, header_end, src(cs, header_end)))
            for m in methods:
                ms, me = span(m)
                chunks.append(("%s.%s" % (node.name, m.name), ms, me, src(ms, me)))
    return chunks


def _generic_chunks(text, window=40):
    """Line-window chunks for non-Python text."""
    lines = text.splitlines()
    out = []
    for i in range(0, len(lines), window):
        seg = lines[i:i + window]
        if not any(s.strip() for s in seg):
            continue
        out.append(("lines %d-%d" % (i + 1, i + len(seg)), i + 1, i + len(seg), "\n".join(seg)))
    return out


# ── Ranking ───────────────────────────────────────────────────────────────────

def _bm25_rank(query, sources):
    """sources: list of chunk source strings. Returns chunk indices sorted by
    BM25 score (desc). Deterministic, stdlib-only."""
    docs = [_tokens(s) for s in sources]
    N = len(docs)
    if N == 0:
        return []
    avgdl = (sum(len(d) for d in docs) / N) or 1.0
    df = {}
    for d in docs:
        for t in set(d):
            df[t] = df.get(t, 0) + 1
    k1, b = 1.5, 0.75
    q = set(_tokens(query))
    scored = []
    for i, d in enumerate(docs):
        if not d or not q:
            scored.append((0.0, i)); continue
        tf = {}
        for t in d:
            tf[t] = tf.get(t, 0) + 1
        dl = len(d)
        s = 0.0
        for t in q:
            f = tf.get(t, 0)
            if not f:
                continue
            n = df.get(t, 0)
            idf = math.log(1 + (N - n + 0.5) / (n + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / avgdl))
        scored.append((s, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [i for _, i in scored]


# ── Saving / pruning ────────────────────────────────────────────────────────

def _save_all(docs, save_dir):
    saved = {}
    try:
        os.makedirs(save_dir, exist_ok=True)
        # prune stale attachments
        now = time.time()
        for fn in os.listdir(save_dir):
            fp = os.path.join(save_dir, fn)
            try:
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > ATTACH_TTL:
                    os.unlink(fp)
            except Exception:
                pass
    except Exception:
        pass
    for d in docs:
        name = d.get("name", "file")
        try:
            p = os.path.join(save_dir, _sanitize(name))
            with open(p, "w", encoding="utf-8", errors="replace") as f:
                f.write(d.get("content") or "")
            saved[name] = p
        except Exception:
            saved[name] = None
    return saved


# ── Entry point ───────────────────────────────────────────────────────────────

def process_attachments(docs, user_text, save_dir):
    """
    docs:      [{"name": str, "content": str}]
    user_text: the user's message (used to rank relevance)
    save_dir:  where full copies are written

    Returns (prompt_block: str, manifest: dict).
    """
    docs = [d for d in (docs or []) if isinstance(d, dict)]
    total = sum(len(d.get("content") or "") for d in docs)
    saved = _save_all(docs, save_dir)

    # Inject everything verbatim when it's a single file (up to FULL_SINGLE) or a
    # small batch (up to SMALL_TOTAL) — complete and avoids pagination round-trips.
    if (len(docs) == 1 and total <= FULL_SINGLE) or total <= SMALL_TOTAL:
        parts = []
        for i, d in enumerate(docs, 1):
            c = d.get("content") or ""
            parts.append("===== FILE %d/%d: %s (%d chars) =====\n%s"
                         % (i, len(docs), d.get("name", "file"), len(c), c))
        block = ("[The user attached %d file(s); full contents follow. If asked to "
                 "revise a file, output it with write_file(...) so it lands in "
                 "patches\\ for review.]\n\n" % len(docs)
                 + "\n\n".join(parts) + "\n\n[End of attached files.]\n\n")
        return block, {"mode": "full", "saved": saved}

    # Large batch → structure map + BM25-ranked relevant chunks.
    all_chunks = []        # (file, label, start, end, source)
    skel = []
    for d in docs:
        name = d.get("name", "file")
        content = d.get("content") or ""
        nlines = content.count("\n") + 1
        py = _py_chunks(content) if name.lower().endswith(".py") else None
        path = saved.get(name) or "(unsaved)"
        if py:
            skel.append("FILE: %s (%d lines, %d symbols) -> %s" % (name, nlines, len(py), path))
            shown = 0
            for (label, s, e, src) in py:
                if shown < MAX_SKEL_FILE:
                    skel.append("    %s  [L%d-%d]" % (label, s, e))
                    shown += 1
                all_chunks.append((name, label, s, e, src))
            if len(py) > MAX_SKEL_FILE:
                skel.append("    ... +%d more symbols (use show_file)" % (len(py) - MAX_SKEL_FILE))
        else:
            kind = "text" if py is None else "python(unparsed)"
            gc = _generic_chunks(content)
            skel.append("FILE: %s (%d lines, %s) -> %s" % (name, nlines, kind, path))
            for (label, s, e, src) in gc:
                all_chunks.append((name, label, s, e, src))

    # Rank on the source plus the symbol name repeated, so that naming a
    # function/method in the request strongly favours that chunk (users usually
    # say "fix cancellable_post", not paraphrase its body).
    ranked = _bm25_rank(user_text, [((c[1] + "\n") * 4 + c[4]) for c in all_chunks])

    injected, used = [], 0
    for idx in ranked:
        if len(injected) >= MAX_CHUNKS:
            break
        file, label, s, e, src = all_chunks[idx]
        body = src
        if len(body) > PER_CHUNK_CAP:
            body = body[:PER_CHUNK_CAP] + "\n[...chunk truncated — show_file for the full file]"
        if used + len(body) > CHUNK_BUDGET:
            continue
        used += len(body)
        injected.append("----- %s :: %s  [L%d-%d] -----\n%s" % (file, label, s, e, body))

    if len(skel) > MAX_SKEL_ALL:
        skel = skel[:MAX_SKEL_ALL] + ["... structure map truncated (%d more lines) — use show_file" % (len(skel) - MAX_SKEL_ALL)]

    block = (
        "[The user attached %d file(s), too large to include in full. Below is a "
        "STRUCTURE MAP of every file (each symbol with its line range and saved "
        "path), then the chunks most relevant to the request. To read any part not "
        "shown, call show_file('<saved path>') — it paginates. When revising, output "
        "the result with write_file(...) so it lands in patches\\ for review.]\n\n"
        "===== STRUCTURE MAP =====\n%s\n\n"
        "===== MOST RELEVANT CHUNKS =====\n%s\n\n"
        "[End of attached files.]\n\n"
    ) % (
        len(docs),
        "\n".join(skel),
        "\n\n".join(injected) if injected else
        "(nothing matched strongly — use the structure map and show_file to read what you need.)",
    )
    return block, {"mode": "ranked", "saved": saved, "chunks": len(all_chunks), "injected": len(injected)}
