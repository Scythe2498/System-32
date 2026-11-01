# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_web.py — Controlled web access for System 32.

Every outbound request flows through ONE chokepoint (`_request`) so it can be:
  • routed through a configurable proxy (HTTP or SOCKS) read from config.json,
  • logged to web_requests.log (so the guardian / awareness sees its reach out),
  • size-capped + timed-out, and given a real User-Agent.

Proxy is OFF by default (direct). To turn it on later, set in config.json:
    "web_proxy": "http://127.0.0.1:8080"      # applies to http+https
  or a SOCKS/Tor endpoint (needs PySocks, installed by bootstrap):
    "web_proxy": "socks5://127.0.0.1:9050"
  or split:
    "web_proxy_http":  "...", "web_proxy_https": "..."

Tools exposed to the model:
    web_search(query, max_results) -> keyless DuckDuckGo results (title/url/snippet)
    web_fetch(url, max_chars)      -> fetch a page, strip to readable text, capped
    web_image_search(query, n)     -> images: SearXNG (if running) + Bing + DuckDuckGo at once,
                                      merged; Bing/DDG pages + Openverse as backup. Grid in chat.
    web_video_search(query, n)     -> videos: SearXNG + DuckDuckGo + YouTube + Dailymotion at
                                      once, merged. Cards in chat.
"""

import os
import re
import html
import time
import json
from urllib.parse import urlparse, parse_qs, unquote, quote, urljoin

try:
    import requests
except Exception:                       # pragma: no cover
    requests = None

from s32_config import CONFIG, REFERENCE_DIR, PERSON_DIR, log

_UA          = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) System32/1.0 (+local-assistant)"
# Image hosts and search pages often refuse a non-browser User-Agent (thumbnails 403'd
# and the grid cells vanished). Used for image/video search pages and image fetches.
_BROWSER_UA  = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
_TIMEOUT     = 15
_MAX_BYTES   = 2_000_000                 # 2 MB cap on any fetched body
_IMG_MAX     = 1_500_000                 # 1.5 MB cap on images served to the panel
_REQUEST_LOG = os.path.join(PERSON_DIR, "web_requests.log")   # browsing history is the person's own


# ── Chat-panel cards (clickable links + images), routed through the chokepoint ─
def _domain(url):
    try:
        d = urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def _favicon(domain):
    """Favicon via DuckDuckGo's keyless icon service, proxied through /webimg so
    it respects the proxy and never leaks directly from the browser."""
    if not domain:
        return ""
    return "/webimg?u=" + quote(f"https://icons.duckduckgo.com/ip3/{domain}.ico", safe="")


def _push(result):
    """Push a payload to the chat panel as its own bubble (job_id pattern)."""
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result(result, jid)
        _cfg._push_done(jid)
    except Exception as e:
        log(f"[S32][WEB] panel push failed: {e}")


def _push_web_cards(query, results, read=None):
    if not results:
        return
    cards = []
    for r in results:
        dom = _domain(r.get("url", ""))
        cards.append({
            "title":   r.get("title", "") or r.get("url", ""),
            "url":     r.get("url", ""),
            "snippet": r.get("snippet", ""),
            "domain":  dom,
            "favicon": _favicon(dom),
        })
    # The chat shows these as a compact row of source icons at the end of her answer
    # (2026-10-28) — `read` marks the pages she actually read.
    _push({"type": "web_results", "query": query, "cards": cards, "read": list(read or [])})


def fetch_image(url):
    """Fetch a remote image through the chokepoint. Returns (bytes, ctype, error).
    Only image content-types are served; size-capped; honours the proxy."""
    if requests is None:
        return None, None, "requests unavailable"
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.I):
        return None, None, "bad url"
    proxies = _proxies()
    try:
        _o = urlparse(url)
        r = requests.get(url, headers={"User-Agent": _BROWSER_UA,
                                       "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                                       "Referer": f"{_o.scheme}://{_o.netloc}/"},
                         proxies=proxies, timeout=10, stream=True, allow_redirects=True)
        ctype = (r.headers.get("Content-Type", "") or "").lower()
        if r.status_code >= 400 or "image" not in ctype:
            r.close()
            _log_request("GET", url, r.status_code, f"img refused ({ctype[:30] or 'no type'})")
            return None, None, f"HTTP {r.status_code}" if r.status_code >= 400 else "not an image"
        body = b""
        for chunk in r.iter_content(8192):
            if not chunk:
                continue
            body += chunk
            if len(body) > _IMG_MAX:
                break
        r.close()
        _log_request("GET", url, r.status_code, f"img proxy={'yes' if proxies else 'direct'}")
        return body, ctype, None
    except Exception as e:
        _log_request("GET", url, "ERR", str(e)[:120])
        return None, None, str(e)


def _og_image(htmltext, base_url):
    """Pull a page's hero image (og:image / twitter:image) if present."""
    pats = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for p in pats:
        m = re.search(p, htmltext, re.I)
        if m:
            img = m.group(1).strip()
            if img.startswith("//"):
                img = "https:" + img
            elif img.startswith("/"):
                img = urljoin(base_url, img)
            if img.startswith("http"):
                return img
    return ""


# ── Proxy + chokepoint ───────────────────────────────────────────────────────
def _proxies():
    """Build a requests proxies dict from config, or None for direct.

    `web_proxy` applies to both schemes; `web_proxy_http` / `web_proxy_https`
    override per-scheme. Empty / absent => direct connection."""
    single = (CONFIG.get("web_proxy") or "").strip()
    p_http = (CONFIG.get("web_proxy_http") or "").strip()
    p_https = (CONFIG.get("web_proxy_https") or "").strip()
    if single:
        return {"http": single, "https": single}
    d = {}
    if p_http:
        d["http"] = p_http
    if p_https:
        d["https"] = p_https
    return d or None


def _log_request(method, url, status, note=""):
    try:
        with open(_REQUEST_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{method}\t{status}\t{url}\t{note}\n")
    except Exception:
        pass


def _request(method, url, **kw):
    """The single outbound chokepoint. Returns (body_text, response, error)."""
    if requests is None:
        return None, None, "The 'requests' library isn't available in this runtime."
    import s32_config as _cfg
    if _cfg.airplane():
        _log_request(method, url, "AIRPLANE", "refused — airplane mode")
        return None, None, _cfg.AIRPLANE_MSG
    proxies = _proxies()
    headers = kw.pop("headers", {}) or {}
    headers.setdefault("User-Agent", _UA)
    headers.setdefault("Accept-Language", "en-US,en;q=0.9")
    try:
        r = requests.request(method, url, headers=headers, proxies=proxies,
                             timeout=_TIMEOUT, stream=True, allow_redirects=True, **kw)
        body = b""
        for chunk in r.iter_content(8192):
            if not chunk:
                continue
            body += chunk
            if len(body) > _MAX_BYTES:
                break
        try:
            r.close()
        except Exception:
            pass
        _log_request(method, url, r.status_code,
                     f"proxy={'yes' if proxies else 'direct'} bytes={len(body)}")
        # Decode using the response's apparent encoding, falling back to utf-8.
        enc = r.encoding or "utf-8"
        try:
            text = body.decode(enc, "replace")
        except (LookupError, TypeError):
            text = body.decode("utf-8", "replace")
        return text, r, None
    except Exception as e:
        msg = str(e)
        if "SOCKS" in msg or "Missing dependencies" in msg:
            msg = ("SOCKS proxy needs the PySocks package. It should install on the "
                   "next setup run; until then use an http:// proxy or remove web_proxy.")
        _log_request(method, url, "ERR", msg[:140])
        return None, None, f"Request failed: {msg}"


# ── HTML helpers ─────────────────────────────────────────────────────────────
def _clean(s):
    """Strip tags, unescape entities, collapse whitespace."""
    s = re.sub(r"<[^>]+>", "", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def _ddg_unwrap(href):
    """DuckDuckGo wraps result links as //duckduckgo.com/l/?uddg=<encoded>."""
    if not href:
        return ""
    if "uddg=" in href:
        try:
            q = parse_qs(urlparse(href if "://" in href else "https:" + href).query)
            if "uddg" in q:
                return unquote(q["uddg"][0])
        except Exception:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def _extract_title(htmltext):
    m = re.search(r"<title[^>]*>(.*?)</title>", htmltext, re.S | re.I)
    return _clean(m.group(1)) if m else ""


def _extract_text(htmltext):
    """Best-effort readable-text extraction with no extra dependencies."""
    t = htmltext
    t = re.sub(r"<script.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<style.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<noscript.*?</noscript>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<(nav|header|footer|aside|form)\b.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|h[1-6]|tr|section|article)>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    t = re.sub(r"\n[ \t]+", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


# ── DuckDuckGo search (keyless) ──────────────────────────────────────────────
def _ddg_search(query, max_results):
    body, r, err = _request("POST", "https://html.duckduckgo.com/html/",
                            data={"q": query, "kl": "us-en"})
    if err:
        return None, err
    if _looks_blocked(body):
        return None, "blocked (bot check)"
    titles = re.findall(r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                        body, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', body, re.S)
    results = []
    for i, (href, title) in enumerate(titles):
        if len(results) >= max_results:
            break
        url = _ddg_unwrap(href)
        if not url or not url.startswith("http"):
            continue
        results.append({
            "title": _clean(title) or "(no title)",
            "url": url,
            "snippet": _clean(snippets[i]) if i < len(snippets) else "",
        })
    return results, None


# ── More search backends (2026-10-28) ────────────────────────────────────────
# DuckDuckGo's HTML endpoint regularly answers scripted requests with a bot-check page
# or changed markup → ZERO results with no error, and the model then filled the gap
# from memory ("Oblivion Remastered … announced at The Game Awards 2021" — invented).
# So search is a CHAIN: the first backend that returns results wins.
def _looks_blocked(body):
    b = (body or "").lower()
    return ("anomaly" in b and "duckduckgo" in b) or "captcha" in b or "are you a robot" in b


def _ddg_lite_search(query, max_results):
    body, r, err = _request("POST", "https://lite.duckduckgo.com/lite/",
                            data={"q": query, "kl": "us-en"})
    if err or not body or _looks_blocked(body):
        return None, err or "blocked"
    links = re.findall(r'<a[^>]+href="([^"]+)"[^>]*class=[\'"]result-link[\'"][^>]*>(.*?)</a>', body, re.S)
    links += re.findall(r'<a[^>]+class=[\'"]result-link[\'"][^>]*href="([^"]+)"[^>]*>(.*?)</a>', body, re.S)
    snips = re.findall(r'class=[\'"]result-snippet[\'"][^>]*>(.*?)</td>', body, re.S)
    out = []
    for i, (href, title) in enumerate(links):
        url = _ddg_unwrap(html.unescape(href))
        if not url.startswith("http") or any(o["url"] == url for o in out):
            continue
        out.append({"title": _clean(title) or url, "url": url,
                    "snippet": _clean(snips[i]) if i < len(snips) else ""})
        if len(out) >= max_results:
            break
    return out, None


def _bing_search(query, max_results):
    body, r, err = _request("GET", "https://www.bing.com/search",
                            params={"q": query, "setlang": "en", "cc": "US"})
    if err or not body or _looks_blocked(body):
        return None, err or "blocked"
    out = []
    for block in re.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', body, re.S):
        m = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not m:
            continue
        url = html.unescape(m.group(1))
        if not url.startswith("http"):
            continue
        sn = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
        out.append({"title": _clean(m.group(2)) or url, "url": url,
                    "snippet": _clean(sn.group(1)) if sn else ""})
        if len(out) >= max_results:
            break
    return out, None


def _wikipedia_search(query, max_results):
    """Wikipedia's official search API — keyless, reliable, great for 'what is X'. The
    top hit's intro is fetched too, so there's real text to answer from."""
    body, r, err = _request("GET", "https://en.wikipedia.org/w/api.php",
                            params={"action": "query", "list": "search", "srsearch": query,
                                    "format": "json", "srlimit": max(1, min(max_results, 5))})
    if err or not body:
        return None, err or "no response"
    try:
        hits = json.loads(body).get("query", {}).get("search", [])
    except Exception:
        return None, "bad response"
    out = []
    for h in hits:
        t = h.get("title", "")
        if not t:
            continue
        out.append({"title": t + " — Wikipedia",
                    "url": "https://en.wikipedia.org/wiki/" + quote(t.replace(" ", "_")),
                    "snippet": _clean(h.get("snippet", ""))})
    if out:
        try:
            b2, _r2, e2 = _request("GET", "https://en.wikipedia.org/w/api.php",
                                   params={"action": "query", "prop": "extracts", "exintro": 1,
                                           "explaintext": 1, "redirects": 1, "format": "json",
                                           "titles": out[0]["title"].replace(" — Wikipedia", "")})
            pages = (json.loads(b2 or "{}").get("query", {}) or {}).get("pages", {})
            for pg in pages.values():
                ex = (pg.get("extract") or "").strip()
                if ex:
                    out[0]["snippet"] = ex[:1200]
        except Exception:
            pass
    return out, None


_STOP = set("""a an the and or of to in on for with by from at as is are was were be been how
what when where which who why does do did can could should would will about into than then
this that these those it its your you my our me i vs versus best good guide early game""".split())
_MIN_RELEVANCE = 0.4
_last_search = {"sig": None}
# Per-question budget (2026-10-28): with good results she still re-searched with new
# wording 3× (each set differed, so the repeat check never fired). Each search now also
# READS the top pages, and a third search for the same question doesn't run.
_SEARCH_BUDGET = 2
_READ_TOP = 2
_READ_CHARS = 3000
_turn = {"searches": 0, "pages": []}


# Image/video searches had NO guard — only a line in her result telling her not to search again
# (2026-10-30: two image asks in a row → a 'spree' of grids, each query reworded a little, so
# nothing caught it). Per operator message: a rewording of a search she already ran is refused
# (the grid is already on screen; More results is the way to get more), and 3 is the most.
_MEDIA_BUDGET = 3
_MEDIA_WORDS = set("image images picture pictures photo photos pic pics video videos clip clips "
                   "footage wallpaper wallpapers hd 4k high quality real realistic".split())
_media_turn = {"queries": []}


def new_request():
    """Called at the start of every operator message: fresh search budget."""
    _turn["searches"] = 0
    _turn["pages"] = []
    _media_turn["queries"] = []


def _plural_norm(w):
    return w[:-2] if w.endswith("es") and len(w) > 4 else w[:-1] if w.endswith("s") and len(w) > 3 else w


def _media_guard(kind, query):
    """None = go ahead; else the refusal result for this image/video search."""
    terms = {_plural_norm(t) for t in _query_terms(query) if t not in _MEDIA_WORDS}
    for k, q, t in _media_turn["queries"]:
        if k != kind or not t or not terms:
            continue
        overlap = len(terms & t) / len(terms | t)
        if terms <= t or t <= terms or overlap >= 0.6:
            log(f"[S32][WEB] {kind} search {query[:50]!r} is a rewording of {q[:50]!r} — not searching again")
            return {"type": f"web_{kind}_result", "query": query, "count": 0, "results": [], "quiet": True,
                    "repeat_of": q,
                    "error": (f"You already searched {kind}s for '{q}' in this reply — those results are on "
                              f"the operator's screen, and a reworded search shows the same kind of "
                              f"{kind}s. Don't search again: tell the operator what you found (they can "
                              f"press 'More results' under the grid for more). Only search again for a "
                              f"DIFFERENT subject the operator asked for.")}
    if len(_media_turn["queries"]) >= _MEDIA_BUDGET:
        log(f"[S32][WEB] {kind} search budget used ({_MEDIA_BUDGET}) — not searching {query[:50]!r}")
        return {"type": f"web_{kind}_result", "query": query, "count": 0, "results": [], "quiet": True,
                "error": (f"You've already run {_MEDIA_BUDGET} image/video searches for this message — "
                          f"answer the operator now with what's on screen.")}
    _media_turn["queries"].append((kind, query, terms))
    return None


def _read_page(url, max_chars=_READ_CHARS):
    """Readable text of one page (no chat cards) — for search's built-in reading."""
    body, r, err = _request("GET", url)
    if err or not body:
        return ""
    ctype = (r.headers.get("Content-Type", "") if r is not None else "").lower()
    if ctype and "html" not in ctype and "text" not in ctype:
        return ""
    return _extract_text(body)[:max_chars]


def _query_terms(query):
    words = re.findall(r"[a-z0-9]+", (query or "").lower())
    # Numbers/years don't count — a guessed year ("2020") shouldn't sink good results.
    return [w for w in dict.fromkeys(words) if len(w) >= 3 and w not in _STOP and not w.isdigit()]


def _term_in(t, words):
    """Query word t appears in the set `words` — plurals both ways, long words by prefix."""
    return (t in words or (t + "s") in words or (t + "es") in words
            or (t.endswith("s") and t[:-1] in words) or (t.endswith("es") and t[:-2] in words)
            or (len(t) > 4 and any(x.startswith(t[:5]) for x in words)))


def _hay_words(r):
    hay = " ".join([r.get("title", ""), r.get("snippet", ""), r.get("url", "")]).lower()
    return set(re.sub(r"[^a-z0-9]+", " ", hay).split())


def _relevance(r, terms):
    """Weighted share of the query's real words found in the result's title/snippet/url.
    Earlier words weigh more — queries name their SUBJECT first ("Oblivion Remastered
    …"), so a page about exactly that subject passes even without every detail word,
    while a page matching only "Oblivion" (the 2013 film) doesn't."""
    if not terms:
        return 1.0
    hay = " ".join([r.get("title", ""), r.get("snippet", ""), r.get("url", "")]).lower()
    words = set(re.sub(r"[^a-z0-9]+", " ", hay).split())
    total = got = 0.0
    for i, t in enumerate(terms):
        w = 1.0 / (1.0 + 0.3 * i)
        total += w
        if _term_in(t, words):
            got += w                     # 'remastered' ~ 'remaster', 'foxes' ~ 'fox'
    return got / total


# ── ddgs: a maintained multi-engine search library (2026-10-28) ───────────────
# Hand-written scrapers break whenever an engine changes its page or blocks scripts
# (DDG bot-checks, Bing served the same off-topic list for every query). `ddgs`
# (pip, open source, successor of duckduckgo_search) queries several engines — Bing,
# Brave, DuckDuckGo, Mojeek, Yahoo, … — with browser impersonation, and its parsers
# are maintained upstream. Installed in the background on first use; until then (or
# if it fails) the hand-written chain below still runs.
_ddgs_install = {"started": False, "done": False}


def _ensure_ddgs():
    import s32_config as _cfg
    if _ddgs_install["started"] or _cfg.airplane():
        return
    _ddgs_install["started"] = True

    def _bg():
        import subprocess
        import sys
        try:
            log("[S32][WEB] installing the 'ddgs' search library (one-time, in the background)…")
            r = subprocess.run([sys.executable, "-m", "pip", "install", "ddgs", "--quiet",
                                "--break-system-packages"], capture_output=True, text=True,
                               timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0:
                # A package installed while we're running is invisible to `import` until
                # the import system's cached folder listings are refreshed (2026-10-28:
                # it installed, then every search still said "not installed yet").
                import importlib
                importlib.invalidate_caches()
                _ddgs_install["done"] = True
            log("[S32][WEB] ddgs " + ("installed — better web search from the next query on"
                                      if r.returncode == 0 else f"install failed: {(r.stderr or '')[-160:]}"))
        except Exception as e:
            log(f"[S32][WEB] ddgs install failed: {e}")
    import threading
    threading.Thread(target=_bg, daemon=True).start()


def _ddgs_client():
    """(DDGS instance, None) or (None, why not)."""
    import importlib
    if _ddgs_install.get("done"):
        importlib.invalidate_caches()
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS          # older name of the same library
        except ImportError:
            _ensure_ddgs()
            return None, ("ddgs installing…" if not _ddgs_install.get("done")
                          else "ddgs installed but not importable — restart System 32")
    proxy = (_proxies() or {}).get("https")
    try:
        try:
            return DDGS(proxy=proxy, timeout=15), None
        except TypeError:
            return DDGS(proxies=proxy, timeout=15), None
    except Exception as e:
        return None, str(e)[:120]


def _ddgs_search(query, max_results):
    d, why = _ddgs_client()
    if d is None:
        return None, why
    proxy = (_proxies() or {}).get("https")
    try:
        try:
            rows = d.text(query, region="us-en", safesearch="moderate",
                          max_results=max(5, max_results * 2))
        except TypeError:
            rows = d.text(keywords=query, region="us-en", max_results=max(5, max_results * 2))
        rows = list(rows or [])
    except Exception as e:
        _log_request("DDGS", query[:120], "ERR", str(e)[:120])
        return None, str(e)[:120]
    _log_request("DDGS", query[:120], "OK", f"{len(rows)} results proxy={'yes' if proxy else 'direct'}")
    out = []
    for r in rows:
        url = r.get("href") or r.get("url") or ""
        if not url.startswith("http") or any(o["url"] == url for o in out):
            continue
        out.append({"title": _clean(r.get("title", "")) or url, "url": url,
                    "snippet": _clean(r.get("body", "") or r.get("snippet", ""))})
    return out, None


_SEARCH_CHAIN = (("ddgs", _ddgs_search), ("DuckDuckGo", None),
                 ("DuckDuckGo Lite", _ddg_lite_search),
                 ("Bing", _bing_search), ("Wikipedia", _wikipedia_search))


# ── Public tools ─────────────────────────────────────────────────────────────
def web_search(query, max_results=6):
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    query = (query or "").strip()
    if not query:
        return {"error": "Empty search query."}
    try:
        max_results = max(1, min(int(max_results or 6), 10))
    except Exception:
        max_results = 6
    # Ask the engines in turn, keep only results that actually match the query, and
    # stop once there are enough. (2026-10-28: Bing answered every query with the same
    # off-topic list — the 2013 film, IMDb, a dictionary — and "first engine with any
    # results wins" accepted that junk 4 times in a row.)
    if _turn["searches"] >= _SEARCH_BUDGET:
        log(f"[S32][WEB] search budget used ({_SEARCH_BUDGET}) — not searching {query[:50]!r}")
        return {"type": "web_search_result", "query": query, "count": 0, "results": [],
                "quiet": True, "budget_used": True,
                "error": (f"You've already searched {_SEARCH_BUDGET} times for this question and "
                          "read the top pages — searching again won't add anything. Answer the "
                          "operator NOW from the results and page text above; if something is "
                          "still missing, say what, and offer to look into it.")}
    terms = _query_terms(query)
    pool, tried, err = [], [], None
    engines = list(_SEARCH_CHAIN)
    if _searx_base():
        engines.insert(0, ("SearXNG", lambda q, n: (_searxng_search(q, n), None)))
    for name, fn in engines:
        try:
            res, e = (_ddg_search(query, max_results) if fn is None else fn(query, max_results))
        except Exception as ex:
            res, e = None, str(ex)[:120]
        res = res or []
        good = 0
        for r in res:
            r["_score"] = _relevance(r, terms)
            r["_via"] = name
            if r["_score"] >= _MIN_RELEVANCE and not any(p_["url"] == r["url"] for p_ in pool):
                pool.append(r)
                good += 1
        tried.append(f"{name}: {good}/{len(res)} relevant" if res else f"{name}: {(e or 'no results')[:40]}")
        if not res:
            err = e or err
        if len(pool) >= max_results:
            break
    pool.sort(key=lambda r: -r["_score"])
    results = [{k: v for k, v in r.items() if not k.startswith("_")} for r in pool[:max_results]]
    source = ", ".join(sorted({r["_via"] for r in pool[:max_results]}))
    log(f"[S32][WEB] search {query[:60]!r}: " + (f"{len(results)} relevant result(s) via {source}"
        if results else "nothing relevant") + " — " + "; ".join(tried))
    if not results:
        # Several empty searches in a row → end the attempt for her (2026-10-28: told
        # "searching again rarely helps", she re-searched 4× anyway).
        now = time.time()
        if now - _last_search.get("empty_ts", 0) > 300:
            _last_search["empties"] = 0
        _last_search["empties"] = _last_search.get("empties", 0) + 1
        _last_search["empty_ts"] = now
        if _last_search["empties"] >= 3:
            _last_search["empties"] = 0
            return {"type": "web_search_result", "query": query, "count": 0, "results": [],
                    "halt": True, "quiet": True,
                    "halt_msg": ("(I couldn't find anything online for that — the search engines "
                                 "aren't giving me relevant results right now. Anything I've said "
                                 "about it is from memory and may be out of date.)"),
                    "error": "No relevant results again."}
        # Say it plainly so it isn't papered over with memory presented as fact.
        return {"type": "web_search_result", "query": query, "count": 0, "results": [],
                "error": ("No relevant search results — the search engines came back empty, "
                          f"blocked, or off-topic ({'; '.join(tried)}). Searching again with "
                          "another wording rarely helps. Tell the operator you couldn't find it "
                          "online right now. If you add anything from your own knowledge, say "
                          "clearly that it's from memory, may be out of date, and wasn't verified.")}
    # Same links as the previous search this turn → say so (she re-searched 4× in a loop).
    _last_search["empties"] = 0
    _turn["searches"] += 1
    # Read the top pages too, so the actual methods/facts are in hand — snippets only
    # tease ("we'll go over five of the best…"), which is what kept her searching.
    pages = []
    for r in results:
        if len(pages) >= _READ_TOP:
            break
        if r["url"] in _turn["pages"]:
            continue
        txt = _read_page(r["url"])
        if len(txt) > 300:
            pages.append({"title": r["title"], "url": r["url"], "text": txt})
            _turn["pages"].append(r["url"])
    if pages:
        log(f"[S32][WEB] read {len(pages)} page(s): " + ", ".join(_domain(p_['url']) for p_ in pages))
    sig = tuple(r["url"] for r in results)
    repeat = (sig == _last_search.get("sig") and time.time() - _last_search.get("ts", 0) < 300)
    _last_search.update({"sig": sig, "ts": time.time()})
    if not repeat:
        _push_web_cards(query, results, [p_["url"] for p_ in pages])   # → source icons
    return {"type": "web_search_result", "query": query, "count": len(results),
            "results": results, "source": source, "proxy": bool(_proxies()),
            "repeat": repeat, "pages": pages, "searches_left": _SEARCH_BUDGET - _turn["searches"]}


def web_fetch(url, max_chars=6000):
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    url = (url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    if not re.match(r"^https?://", url, re.I):
        if "." in url and " " not in url:
            url = "https://" + url
        else:
            return {"error": f"Not a valid URL: {url!r}"}
    try:
        max_chars = max(500, min(int(max_chars or 6000), 20000))
    except Exception:
        max_chars = 6000

    body, r, err = _request("GET", url)
    if err:
        return {"error": err, "url": url}

    ctype = (r.headers.get("Content-Type", "") if r is not None else "").lower()
    looks_html = ("html" in ctype or "text" in ctype or "<html" in body[:1000].lower()
                  or "<!doctype" in body[:1000].lower())
    if not looks_html:
        return {"url": url, "status": getattr(r, "status_code", 0),
                "content_type": ctype,
                "text": f"[Non-text content ({ctype or 'unknown type'}) — can't read as text.]",
                "truncated": False, "chars": 0}

    title = _extract_title(body)
    text = _extract_text(body)
    truncated = len(text) > max_chars
    hero = _og_image(body, url)
    if hero:
        _push({"type": "image", "url": "/webimg?u=" + quote(hero, safe="")})
    return {"url": url, "status": getattr(r, "status_code", 0), "title": title,
            "image": hero, "text": text[:max_chars], "truncated": truncated,
            "chars": len(text)}


# ── SearXNG text results (optional; configured or found running locally — see _searx_base) ─
def _searxng_search(query, max_results):
    """Text results from a configured SearXNG instance, or None."""
    rows, err = _searx_get("general", query)
    if not rows:
        return None
    try:
        out = []
        for it in rows[:max_results]:
            url = it.get("url", "")
            if not url:
                continue
            out.append({"title": _clean(it.get("title", "")) or url,
                        "url": url, "snippet": _clean(it.get("content", ""))})
        return out or None
    except Exception as e:
        log(f"[S32][WEB] searxng search failed: {str(e)[:100]}")
        return None


# ── Image search (2026-10-29 rewrite) ─────────────────────────────────────────
# Was: Openverse (CC-licensed photos only) → DDG i.js → SearXNG, first source with ANY
# results wins, and a source that failed returned [] without a word in the log. On the
# operator's machine "american red fox" got nothing from all three and nothing said why.
# Now: every source asked at once and merged (see _media_search below), every failure logged
# with its reason, results kept only if they match the query.
def _ddgs_image_search(query, max_results):
    d, why = _ddgs_client()
    if d is None:
        return None, why
    n = max(10, max_results * 2)
    try:
        try:
            rows = d.images(query, region="us-en", safesearch="moderate", max_results=n)
        except TypeError:
            rows = d.images(keywords=query, region="us-en", safesearch="moderate", max_results=n)
        rows = list(rows or [])
    except Exception as e:
        _log_request("DDGS-IMG", query[:120], "ERR", str(e)[:120])
        return None, str(e)[:120]
    _log_request("DDGS-IMG", query[:120], "OK", f"{len(rows)} results")
    out = []
    for r in rows:
        img = r.get("image") or ""
        if not img.startswith("http"):
            continue
        out.append({"image": img, "thumb": r.get("thumbnail") or img,
                    "title": _clean(r.get("title", "")), "source": r.get("url", ""),
                    "w": r.get("width") or "", "h": r.get("height") or ""})
    return out, None


def _bing_image_search(query, max_results, page=1):
    """Bing Images' result page — every tile carries a JSON 'm' attribute with the full
    image (murl), thumbnail (turl), title (t) and page (purl)."""
    body, r, err = _request("GET", "https://www.bing.com/images/async",
                            params={"q": query, "first": str((page - 1) * 35 + 1), "count": "35", "mmasync": "1",
                                    "setlang": "en", "cc": "US"},
                            headers={"User-Agent": _BROWSER_UA})
    if err or not body:
        return None, err or "no response"
    if _looks_blocked(body):
        return None, "blocked (bot check)"
    out = []
    for raw in re.findall(r'\bm="(\{[^"]+\})"', body):
        try:
            m = json.loads(html.unescape(raw))
        except Exception:
            continue
        img = m.get("murl") or ""
        if not img.startswith("http"):
            continue
        out.append({"image": img, "thumb": m.get("turl") or img,
                    "title": _clean(m.get("t", "")), "source": m.get("purl", "")})
    return out, (None if out else "no image tiles on the page")


def _ddg_image_search(query, max_results):
    """Keyless DDG image search: fetch a vqd token, then hit the i.js endpoint."""
    seed, r, err = _request("GET", "https://duckduckgo.com/",
                            params={"q": query, "iax": "images", "ia": "images"},
                            headers={"User-Agent": _BROWSER_UA})
    if err or not seed:
        return None, err or "no response"
    m = (re.search(r'vqd=["\']([\d-]+)["\']', seed)
         or re.search(r'vqd=([\d-]+)\&', seed)
         or re.search(r'"vqd":"([\d-]+)"', seed))
    if not m:
        return None, "no search token (blocked?)"
    body, r, err = _request("GET", "https://duckduckgo.com/i.js",
                            params={"l": "us-en", "o": "json", "q": query,
                                    "vqd": m.group(1), "f": ",,,", "p": "1"},
                            headers={"Referer": "https://duckduckgo.com/", "User-Agent": _BROWSER_UA})
    if err or not body:
        return None, err or "no response"
    try:
        data = json.loads(body)
    except Exception:
        return None, "bad response"
    out = []
    for it in data.get("results", []):
        img = it.get("image", "")
        if not img or not img.startswith("http"):
            continue
        out.append({"image": img, "thumb": it.get("thumbnail") or img,
                    "title": _clean(it.get("title", "")), "source": it.get("url", "")})
    return out, None


def _openverse_image_search(query, max_results, page=1):
    """Openverse — keyless, but only freely-licensed photos (Flickr, Wikimedia, …):
    good for 'a red fox', no use for a game, product or person. Last resort."""
    body, r, err = _request("GET", "https://api.openverse.org/v1/images/",
                            params={"q": query, "page_size": max(10, max_results), "page": page})
    if err or not body:
        return None, err or "no response"
    if r is not None and getattr(r, "status_code", 200) >= 400:
        return None, f"HTTP {r.status_code}"
    try:
        data = json.loads(body)
    except Exception:
        return None, "bad response"
    out = []
    for it in data.get("results", []):
        img = it.get("url") or it.get("thumbnail")
        if not img or not str(img).startswith("http"):
            continue
        out.append({"image": img, "thumb": it.get("thumbnail") or img,
                    "title": _clean(it.get("title", "")),
                    "source": it.get("foreign_landing_url") or it.get("url", "")})
    return out, None


_media_empties = {"image": [0, 0.0], "video": [0, 0.0]}     # [count, last time]


# ── SearXNG (optional): configured OR found running on this computer ─────────────
# SearXNG is a search engine the operator runs locally; it pulls from Google Images /
# Google Videos / YouTube and dozens more WITHOUT an account. If "searxng_url" isn't set,
# a SearXNG on the usual local ports is picked up automatically (checked every 5 min).
_SEARX_PORTS = (8888, 8889, 8890, 8080)     # 8888-8890: System 32's own (s32_searx)
_searx_probe = {"url": None, "ts": 0.0}


def _searx_base():
    cfg = (CONFIG.get("searxng_url") or "").strip().rstrip("/")
    if cfg:
        return cfg
    try:                                       # System 32's own copy (s32_searx) — ask it
        import s32_searx
        if s32_searx.status.get("state") == "on" and s32_searx.status.get("url"):
            return s32_searx.status["url"]
    except Exception:
        pass
    if time.time() - _searx_probe["ts"] < 300:
        return _searx_probe["url"]
    _searx_probe["ts"] = time.time()
    found = None
    if requests is not None:
        for port in _SEARX_PORTS:
            base = f"http://127.0.0.1:{port}"
            # /healthz answers instantly. (Was a real search with a 1.5 s limit — a real search
            # takes longer, so a running SearXNG was reported missing, 2026-10-29.)
            try:
                r = requests.get(base + "/healthz", timeout=1.5, proxies={"http": None, "https": None})
                if r.status_code == 200 and r.text.strip().upper().startswith("OK"):
                    found = base
                    break
            except Exception:
                continue
    if found and found != _searx_probe["url"]:
        log(f"[S32][WEB] found SearXNG running at {found} — adding it to image/video search")
    _searx_probe["url"] = found
    return found


def _searx_get(category, query, page=1):
    base = _searx_base()
    if not base:
        return None, "not running"
    # Local instance: never through the outside proxy (it does its own outbound requests).
    local = base.startswith(("http://127.", "http://localhost"))
    try:
        if local and requests is not None:
            r = requests.get(base + "/search", params={"q": query, "format": "json",
                                                       "categories": category, "pageno": page},
                             timeout=20, proxies={"http": None, "https": None})
            body, status = r.text, r.status_code
        else:
            body, r, err = _request("GET", base + "/search",
                                    params={"q": query, "format": "json", "categories": category,
                                            "pageno": page})
            if err:
                return None, err
            status = getattr(r, "status_code", 200)
    except Exception as e:
        return None, str(e)[:120]
    if status == 403:
        return None, "JSON output is switched off in SearXNG's settings.yml (search → formats)"
    if status >= 400:
        return None, f"HTTP {status}"
    try:
        return json.loads(body).get("results", []), None
    except Exception:
        return None, "bad response"


def _searxng_image_search(query, max_results, page=1):
    rows, err = _searx_get("images", query, page)
    if rows is None:
        return None, err
    out = []
    for it in rows:
        img = it.get("img_src") or it.get("thumbnail_src") or ""
        if img.startswith("//"):
            img = "https:" + img
        if not img.startswith("http"):
            continue
        thumb = it.get("thumbnail_src") or img
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        out.append({"image": img, "thumb": thumb, "title": _clean(it.get("title", "")),
                    "source": it.get("url", ""), "engine": ",".join(it.get("engines") or [])})
    return out, None


def _fmt_secs(v):
    try:
        v = int(float(v))
    except Exception:
        return str(v or "")
    h, m, s = v // 3600, (v % 3600) // 60, v % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _searxng_video_search(query, max_results, page=1):
    rows, err = _searx_get("videos", query, page)
    if rows is None:
        return None, err
    out = []
    for it in rows:
        url = it.get("url") or ""
        if not url.startswith("http"):
            continue
        thumb = it.get("thumbnail") or it.get("img_src") or ""
        if thumb.startswith("//"):
            thumb = "https:" + thumb
        ln = it.get("length") or ""
        out.append({"title": _clean(it.get("title", "")) or url, "url": url, "thumb": thumb,
                    "duration": _fmt_secs(ln) if str(ln).replace(".", "").isdigit() else str(ln),
                    "publisher": _domain(url), "uploader": it.get("author") or "",
                    "published": str(it.get("publishedDate") or "")[:10], "views": "",
                    "description": _clean(it.get("content", ""))[:300]})
    return out, None


def _ddgs_images_from(backend):
    def run(query, max_results, page=1):
        d, why = _ddgs_client()
        if d is None:
            return None, why
        try:
            rows = list(d.images(query, region="us-en", safesearch="moderate",
                                 max_results=max(10, max_results * 2), backend=backend,
                                 page=page) or [])
        except TypeError:          # an older ddgs without per-engine choice
            if backend != "bing":
                return None, "this ddgs can't pick an engine"
            return _ddgs_image_search(query, max_results)
        except Exception as e:
            _log_request("DDGS-IMG", f"{backend}: {query[:100]}", "ERR", str(e)[:120])
            return None, str(e)[:120]
        _log_request("DDGS-IMG", f"{backend}: {query[:100]}", "OK", f"{len(rows)} results")
        out = []
        for r in rows:
            img = r.get("image") or ""
            if img.startswith("http"):
                out.append({"image": img, "thumb": r.get("thumbnail") or img,
                            "title": _clean(r.get("title", "")), "source": r.get("url", "")})
        return out, None
    run.pages = True
    return run


def _dailymotion_search(query, max_results, page=1):
    """Dailymotion's public API — keyless."""
    body, r, err = _request("GET", "https://api.dailymotion.com/videos",
                            params={"search": query, "limit": max(10, max_results * 2),
                                    "sort": "relevance", "page": page,
                                    "fields": "id,title,url,thumbnail_480_url,duration,"
                                              "owner.screenname,created_time,views_total,description"})
    if err or not body:
        return None, err or "no response"
    try:
        rows = json.loads(body).get("list", [])
    except Exception:
        return None, "bad response"
    out = []
    for it in rows:
        url = it.get("url") or (f"https://www.dailymotion.com/video/{it['id']}" if it.get("id") else "")
        if not url:
            continue
        ct = it.get("created_time")
        out.append({"title": _clean(it.get("title", "")) or url, "url": url,
                    "thumb": it.get("thumbnail_480_url") or "",
                    "duration": _fmt_secs(it.get("duration")) if it.get("duration") else "",
                    "publisher": "Dailymotion", "uploader": it.get("owner.screenname") or "",
                    "published": time.strftime("%Y-%m-%d", time.gmtime(ct)) if isinstance(ct, (int, float)) else "",
                    "views": it.get("views_total") or "",
                    "description": _clean(it.get("description", ""))[:300]})
    return out, None


# Every primary source is asked AT ONCE and the results merged (2026-10-29, operator:
# "merge 1, 2 and 3"). The hand-written scrapers only run when the primaries come up short.
for _f in (_searxng_image_search, _searxng_video_search, _dailymotion_search,
           _bing_image_search, _openverse_image_search):
    _f.pages = True        # can fetch a page 2, 3, … (the "More results" button)

_IMAGE_PRIMARY = (("SearXNG", _searxng_image_search), ("Bing (ddgs)", _ddgs_images_from("bing")),
                  ("DuckDuckGo (ddgs)", _ddgs_images_from("duckduckgo")))
_IMAGE_BACKUP = (("Bing Images", _bing_image_search), ("DuckDuckGo Images", _ddg_image_search),
                 ("Openverse", _openverse_image_search))
_PER_SITE = 2          # at most this many results from one website / one uploader


def _site_key(kind, r):
    if kind == "image":
        return _domain(r.get("source") or r.get("image") or "")
    return (r.get("uploader") or "").strip().lower() or _domain(r.get("url", ""))


def _run_engines(engines, query, max_results, page=1):
    """Run engines in parallel → {name: (results, err)}. For page > 1 only the sources that
    can page are asked (the rest would just repeat their first page)."""
    from concurrent.futures import ThreadPoolExecutor
    out = {}
    if page > 1:
        engines = [(n, f) for n, f in engines if getattr(f, "pages", False)]
    if not engines:
        return out
    with ThreadPoolExecutor(max_workers=len(engines)) as ex:
        futs = {name: (ex.submit(fn, query, max_results, page) if page > 1 else ex.submit(fn, query, max_results))
                for name, fn in engines}
        for name, f in futs.items():
            try:
                out[name] = f.result(timeout=30)
            except Exception as e:
                out[name] = (None, str(e)[:120])
    return out


def _core_term(results, terms, hay_of):
    """The query word nearly every result agrees on — the real subject, wherever it sits in
    the query ("fox" in "american red fox", "oblivion" in "oblivion remastered money"). None
    when there's no clear one (one-word query, too few results, or no word stands out)."""
    if len(terms) < 2 or len(results) < 6:
        return None
    sets = [_hay_words(hay_of(r)) for r in results]
    df = {t: sum(1 for w in sets if _term_in(t, w)) for t in terms}
    # Most common word wins; a tie goes to the LATER word (in English the main noun comes
    # last: "american red FOX", "racing GAME"). Only when it's in most results.
    best = max(terms, key=lambda t: (df[t], terms.index(t)))
    return best if df[best] >= 0.6 * len(results) else None


# A source that's blocked/unreachable rests for a while instead of being retried on every
# search (2026-10-29: DuckDuckGo images refused every request on the operator's network).
_REST_SECS = 900
_resting = {}          # engine name → time it may be tried again


def _friendly(e):
    """Short, calm reason for the console summary line. Words like 'error'/'failed' turn the
    whole line red, which made healthy searches look broken — the full text still goes to
    web_requests.log."""
    low = str(e or "").lower()
    if not low or "no results" in low:
        return "no results"
    if any(x in low for x in ("403", "429", "blocked", "captcha", "ratelimit", "rate limit", "bot check")):
        return "blocked right now"
    if any(x in low for x in ("requesterror", "connect", "timed out", "timeout", "proxy", "ssl", "dns",
                              "resolve", "reset", "refused", "unreachable", "request failed")):
        return "unreachable right now"
    if "installing" in low:
        return "being installed"
    if "not running" in low:
        return "not running"
    return "didn't answer"


def _media_search(kind, query, max_results, primary, backup=(), page=1, seen=None, core_t=None):
    """Ask every source at once, keep what matches the query, merge, de-duplicate, and order
    so one website can't fill the grid (at most _PER_SITE each before any repeats).
    → (ALL ranked results, tried, source, subject). The caller shows the first batch; the
    rest are kept for the "More results" button. `seen`/`core_t` carry over between pages."""
    terms = _query_terms(query)
    key = "image" if kind == "image" else "url"
    pool, tried = [], []
    seen = set() if seen is None else seen
    core = {"t": core_t}

    def hay_of(r):
        return {"title": r.get("title", ""),
                "snippet": " ".join(str(r.get(k, "")) for k in ("description", "publisher", "uploader")),
                "url": " ".join(str(r.get(k, "")) for k in ("source", "image", "url"))}

    def take(name, res, e):
        res = res or []
        good = relevant = 0
        for r in res:
            hay = hay_of(r)
            r["_score"] = _relevance(hay, terms)
            # The SUBJECT word every source agrees on must be there (see _core_term):
            # 'American Red Cross' / 'American Dad! – FOX trailer' have 2 of 3 words of
            # "american red fox" but not 'fox' (2026-10-29, Dailymotion).
            if core["t"] and r["_score"] < 1.0 and not _term_in(core["t"], _hay_words(hay)):
                r["_score"] = min(r["_score"], _MIN_RELEVANCE - 0.01)
            r["_via"] = name
            # Keep the query string: every YouTube link is youtube.com/watch?v=… — stripping it
            # made them all "the same video" and threw away all but one (2026-10-29: 0/16).
            k = (r.get(key) or "").strip().rstrip("/").lower()
            # The same photo at two sizes (two engines): same page + same title = same picture.
            k2 = ((r.get("source") or "").strip().rstrip("/").lower() + "|" +
                  re.sub(r"\W+", " ", (r.get("title") or "").lower()).strip()) \
                if kind == "image" and r.get("source") and r.get("title") else ""
            if r["_score"] >= _MIN_RELEVANCE:
                relevant += 1
            if r["_score"] >= _MIN_RELEVANCE and k and k not in seen and (not k2 or k2 not in seen):
                seen.add(k)
                if k2:
                    seen.add(k2)
                r["_rank"] = len([p_ for p_ in pool if p_["_via"] == name])   # place in its own list
                pool.append(r)
                good += 1
        if res and relevant < len(res) // 4:
            rej = [r for r in res if r.get("_score", 1) < _MIN_RELEVANCE][:3]
            if rej:
                log(f"[S32][WEB]   {name}: only {relevant}/{len(res)} matched — e.g. rejected: "
                    + " | ".join(f"{(r.get('title') or '')[:50]!r} ({r['_score']:.2f})" for r in rej))
        if not res and e and _friendly(e) in ("blocked right now", "unreachable right now"):
            _resting[name] = time.time() + _REST_SECS
        if not (name == "SearXNG" and not res and e == "not running"):
            # relevant = matched the query; "already found" = another source had the same one
            dup = relevant - good
            tried.append((f"{name}: {relevant}/{len(res)} relevant" + (f" ({dup} already found)" if dup else ""))
                         if res else f"{name}: {_friendly(e)}")

    def runnable(engines):
        out = []
        for name, fn in engines:
            until = _resting.get(name, 0)
            if until > time.time():
                tried.append(f"{name}: resting ({int((until - time.time()) // 60) + 1} min) after being blocked")
            else:
                out.append((name, fn))
        return out

    got = _run_engines(runnable(primary), query, max_results, page)
    if page == 1:
        core["t"] = _core_term([r for res, _e in got.values() for r in (res or [])], terms, hay_of)
    for name, (res, e) in got.items():
        take(name, res, e)
    if len(pool) < max_results and backup:
        for name, (res, e) in _run_engines(runnable(backup), query, max_results, page).items():
            take(name, res, e)
    # Best match first; among equals, interleave the sources (each one's own top picks first).
    pool.sort(key=lambda r: (-round(r["_score"], 2), r["_rank"]))
    # Variety first: each batch of max_results takes at most _PER_SITE from one site; what's
    # held back follows in later batches (so "More results" isn't one site's leftovers).
    ordered, rest = [], list(pool)
    while rest:
        batch, per_site, later = [], {}, []
        for r in rest:
            s_ = _site_key(kind, r)
            if len(batch) < max_results and not (s_ and per_site.get(s_, 0) >= _PER_SITE):
                per_site[s_] = per_site.get(s_, 0) + 1
                batch.append(r)
            else:
                later.append(r)
        if len(batch) < max_results:           # not enough variety → allow repeats
            take_n = max_results - len(batch)
            batch, later = batch + later[:take_n], later[take_n:]
        ordered += batch
        rest = later
    first = ordered[:max_results]
    source = ", ".join(sorted({r["_via"] for r in first}))
    results = [{k: v for k, v in r.items() if not k.startswith("_")} for r in ordered]
    log(f"[S32][WEB] {kind} search {query[:60]!r}"
        + (f" (more results, page {page})" if page > 1 else "") + ": "
        + (f"{min(len(results), max_results)} result(s) via {source}"
           + (f" (+{len(results) - max_results} more held)" if len(results) > max_results else "")
           if results else "NOTHING found")
        + (f" [subject: {core['t']}]" if core["t"] else "")
        + " — " + "; ".join(tried))
    return results, tried, source, core["t"]


def _media_empty(kind, query, tried):
    """No results. A second empty search in a row (within 5 min) ends the turn: when
    every source fails, rewording the query doesn't help (2026-10-29: 'American red fox'
    → 'red fox' → 'American red fox' → loop breaker)."""
    c = _media_empties[kind]
    now = time.time()
    if now - c[1] > 300:
        c[0] = 0
    c[0] += 1
    c[1] = now
    why = "; ".join(tried)
    if c[0] >= 2:
        c[0] = 0
        return {"type": f"web_{kind}_result", "query": query, "count": 0, "results": [],
                "halt": True, "quiet": True,
                "halt_msg": (f"({kind.capitalize()} search isn't finding anything right now — "
                             f"every {kind} source came back empty or blocked (the console log "
                             f"says why). Try again in a bit, or I can do a normal web search instead.)"),
                "error": f"No {kind}s found again."}
    return {"type": f"web_{kind}_result", "query": query, "count": 0, "results": [], "quiet": True,
            "error": (f"No {kind}s found for {query!r} — every {kind} source came back empty, "
                      f"blocked or off-topic ({why}). This is the search sources failing, not "
                      f"the wording: do NOT search again with another wording. Tell the operator "
                      f"{kind} search came up empty right now.")}


def _img_card(r):
    return {"thumb":  "/webimg?u=" + quote(r["thumb"], safe=""),
            "full":   "/webimg?u=" + quote(r["image"], safe=""),
            "src":    r["image"],                 # the real file — for the ⬇ button
            "title":  r.get("title", ""),
            "source": r.get("source", "")}


def _vid_card(r):
    return {"title": r["title"], "url": r["url"],
            "thumb": ("/webimg?u=" + quote(r["thumb"], safe="")) if r.get("thumb") else "",
            "duration": r.get("duration", ""), "publisher": r.get("publisher", ""),
            "uploader": r.get("uploader", ""), "published": r.get("published", ""),
            "views": r.get("views", "")}


def _push_image_grid(query, results, sid=""):
    _push({"type": "image_grid", "query": query, "images": [_img_card(r) for r in results],
           "sid": sid, "more": bool(sid)})


def web_image_search(query, max_results=12):
    """Find images on the web and show them as a thumbnail grid in chat (each with a ⬇)."""
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    query = (query or "").strip()
    if not query:
        return {"error": "Empty image query."}
    _g = _media_guard("image", query)
    if _g:
        return _g
    try:
        max_results = max(1, min(int(max_results or 12), 30))
    except Exception:
        max_results = 12
    seen = set()
    allr, tried, source, core_t = _media_search("image", query, max_results, _IMAGE_PRIMARY, _IMAGE_BACKUP,
                                                seen=seen)
    if not allr:
        return _media_empty("image", query, tried)
    _media_empties["image"][0] = 0
    results = allr[:max_results]
    sid = _more_store("image", query, max_results, allr, seen, core_t)
    _push_image_grid(query, results, sid)
    return {"type": "web_image_result", "query": query, "count": len(results),
            "source": source,
            "results": [{"title": r["title"], "image": r["image"], "page": r["source"]}
                        for r in results]}


# ── Video search (2026-10-29) ─────────────────────────────────────────────────
def _ddgs_video_search(query, max_results, page=1):
    d, why = _ddgs_client()
    if d is None:
        return None, why
    n = max(8, max_results * 2)
    try:
        try:
            rows = d.videos(query, region="us-en", safesearch="moderate", max_results=n, page=page)
        except TypeError:
            rows = d.videos(keywords=query, region="us-en", safesearch="moderate", max_results=n)
        rows = list(rows or [])
    except Exception as e:
        _log_request("DDGS-VID", query[:120], "ERR", str(e)[:120])
        return None, str(e)[:120]
    _log_request("DDGS-VID", query[:120], "OK", f"{len(rows)} results")
    out = []
    for r in rows:
        url = r.get("content") or r.get("embed_url") or ""
        if not url.startswith("http"):
            continue
        imgs = r.get("images") or {}
        thumb = imgs.get("large") or imgs.get("medium") or imgs.get("small") or ""
        stats = r.get("statistics") or {}
        out.append({"title": _clean(r.get("title", "")) or url, "url": url, "thumb": thumb,
                    "duration": r.get("duration") or "",
                    "publisher": r.get("publisher") or r.get("provider") or _domain(url),
                    "uploader": r.get("uploader") or "",
                    "published": (r.get("published") or "")[:10],
                    "views": stats.get("viewCount") or "",
                    "description": _clean(r.get("description", ""))[:300]})
    return out, None


def _yt_text(node):
    if not isinstance(node, dict):
        return ""
    if node.get("simpleText"):
        return node["simpleText"]
    return "".join(x.get("text", "") for x in node.get("runs") or [] if isinstance(x, dict))


def _youtube_search(query, max_results):
    """YouTube's own results page (the ytInitialData JSON in it) — fallback."""
    body, r, err = _request("GET", "https://www.youtube.com/results",
                            params={"search_query": query, "hl": "en", "gl": "US"},
                            headers={"User-Agent": _BROWSER_UA})
    if err or not body:
        return None, err or "no response"
    m = re.search(r"(?:var ytInitialData|window\[\"ytInitialData\"\])\s*=\s*(\{.*?\});\s*</script>", body, re.S)
    if not m:
        return None, "no results data on the page (consent/bot page?)"
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None, "bad results data"
    out, stack = [], [data]
    while stack and len(out) < max_results * 2:
        node = stack.pop()
        if isinstance(node, dict):
            vr = node.get("videoRenderer")
            if isinstance(vr, dict) and vr.get("videoId"):
                vid = vr["videoId"]
                out.append({"title": _yt_text(vr.get("title")) or vid,
                            "url": f"https://www.youtube.com/watch?v={vid}",
                            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                            "duration": _yt_text(vr.get("lengthText")),
                            "publisher": "YouTube",
                            "uploader": _yt_text(vr.get("ownerText")),
                            "published": _yt_text(vr.get("publishedTimeText")),
                            "views": _yt_text(vr.get("viewCountText")),
                            "description": _yt_text((vr.get("detailedMetadataSnippets") or [{}])[0].get("snippetText"))[:300]})
                continue
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))
    return out, (None if out else "no videos on the page")


_ddgs_video_search.pages = True
_VIDEO_PRIMARY = (("SearXNG", _searxng_video_search), ("DuckDuckGo (ddgs)", _ddgs_video_search),
                  ("YouTube", _youtube_search), ("Dailymotion", _dailymotion_search))


def _push_video_grid(query, results, sid=""):
    _push({"type": "video_grid", "query": query, "videos": [_vid_card(r) for r in results],
           "sid": sid, "more": bool(sid)})


def web_video_search(query, max_results=8):
    """Find videos on the web and show them as cards in chat (▶ open, ⬇ download)."""
    import s32_config as _cfg
    if _cfg.airplane():
        return {"error": _cfg.AIRPLANE_MSG, "airplane": True}
    query = (query or "").strip()
    if not query:
        return {"error": "Empty video query."}
    _g = _media_guard("video", query)
    if _g:
        return _g
    try:
        max_results = max(1, min(int(max_results or 8), 20))
    except Exception:
        max_results = 8
    seen = set()
    allr, tried, source, core_t = _media_search("video", query, max_results, _VIDEO_PRIMARY, seen=seen)
    if not allr:
        return _media_empty("video", query, tried)
    _media_empties["video"][0] = 0
    results = allr[:max_results]
    sid = _more_store("video", query, max_results, allr, seen, core_t)
    _push_video_grid(query, results, sid)
    return {"type": "web_video_result", "query": query, "count": len(results), "source": source,
            "results": [{k: r.get(k, "") for k in ("title", "url", "duration", "uploader",
                                                     "publisher", "published", "views")}
                        for r in results]}


# ── "More results" (2026-10-29) ────────────────────────────────────────────────
# Each search gathers far more than it shows (e.g. 45 relevant images, 100+ videos). The
# rest is kept here — in MEMORY ONLY, for the last few searches, gone when System 32 closes
# or factory-resets — and the button under a grid shows the next batch. When the kept list
# runs out, the sources that can page are asked for their next page.
import secrets as _secrets
from collections import OrderedDict as _OD
_MORE_KEEP = 8
_MORE_MAX_PAGES = 5
_more = _OD()


def _more_store(kind, query, batch, allr, seen, core_t):
    sid = _secrets.token_hex(6)
    _more[sid] = {"kind": kind, "query": query, "batch": batch, "items": list(allr),
                  "shown": min(batch, len(allr)), "seen": seen, "core": core_t, "page": 1}
    while len(_more) > _MORE_KEEP:
        _more.popitem(last=False)
    return sid


def web_more(sid):
    """The next batch for the grid with this id → {"items": [...cards...], "more": bool}."""
    e = _more.get(str(sid or ""))
    if not e:
        return {"error": "Those results have expired — ask for the search again.", "items": [], "more": False}
    _more.move_to_end(sid)
    kind, batch = e["kind"], e["batch"]
    # Kept list short → fetch the sources' next page (paging-capable sources only).
    while len(e["items"]) - e["shown"] < batch and e["page"] < _MORE_MAX_PAGES and not kind.startswith("local_"):
        e["page"] += 1
        chain = _IMAGE_PRIMARY if kind == "image" else _VIDEO_PRIMARY
        new, _tried, _src, _c = _media_search(kind, e["query"], batch, chain, page=e["page"],
                                              seen=e["seen"], core_t=e["core"])
        if not new:
            e["page"] = _MORE_MAX_PAGES        # nothing new anywhere → stop asking
            break
        e["items"] += new
    nxt = e["items"][e["shown"]:e["shown"] + batch]
    e["shown"] += len(nxt)
    more = e["shown"] < len(e["items"]) or e["page"] < _MORE_MAX_PAGES
    if kind.startswith("local_"):                  # files on this computer (s32_findex)
        import s32_findex
        card = {"local_image": s32_findex.img_card, "local_video": s32_findex.vid_card}.get(
            kind, s32_findex.file_row)
    else:
        card = _img_card if kind == "image" else _vid_card
    log(f"[S32][WEB] more {kind}s for {e['query'][:50]!r}: +{len(nxt)} (shown {e['shown']})")
    return {"items": [card(r) for r in nxt], "more": bool(nxt) and more, "kind": kind}
