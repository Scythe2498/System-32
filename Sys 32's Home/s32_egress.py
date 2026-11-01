# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_egress.py — where each program on this computer connects OUT to (2026-10-31).

The Iron Dome closes ports and programs. This catches what hides INSIDE a program you
trust — code injected into a browser or a game, phoning home over a normal port.

  • WATCH — every ~5 s, the established outgoing connections (program → destination).
    Destinations are grouped by NETWORK OWNER (Google, Cloudflare, Valve…) using an
    offline, public-domain table (iptoasn.com, PDDL) kept on the drive — no lookup ever
    leaves the computer. Until the table is there, the first two parts of the address.
  • LEARN — each program's normal owners and ports, over ~48 hours of use (or the
    operator's "baseline now"). Programs that reach many owners (browsers) are WIDE; a
    new site is normal for them. The rest are NARROW.
  • FLAG — a narrow program at a new owner · beaconing (the same unfamiliar destination
    contacted again and again at a steady rhythm — the signature of something phoning
    home, and the way to catch a passenger inside a wide program) · a new odd port · a
    program that never used the network before.
  • ACT — an alert (banner + chat note, Mark safe / Dismiss). Never acts on its own —
    EXCEPT while the Iron Dome is armed (lockdown): then that destination is blocked for
    that program at once, as a dome rule, removed again by Reopen like every dome rule.

Privacy: a record of where programs connect is sensitive. Only owner numbers and port
numbers are stored (no addresses, no names of sites), in this person's own folder.
Windows' own programs (C:\\Windows\\…) are left alone, like in the dome.
"""

import os
import re
import json
import time
import gzip
import bisect
import socket
import struct
import hashlib
import threading
from array import array

try:
    from s32_config import CONFIG, REFERENCE_DIR, PERSON_DIR, state, log
except Exception:                                  # importable in isolation (tests)
    CONFIG, REFERENCE_DIR, PERSON_DIR, state = {}, "", "", {}
    def log(*a): print(*a)

_POLL = 5                                          # seconds between looks
_NORMAL_PORTS = {80, 443}
_WIDE_OWNERS = 20                                  # distinct owners → a 'wide' program
_BEACON_STARTS = 6                                 # new connections to one address …
_BEACON_SPAN = 180                                 # … over at least 3 minutes …
_BEACON_CV = 0.35                                  # … at a steady rhythm (spread/mean)
_DB_URL = "https://iptoasn.com/data/ip2asn-v4-u32.tsv.gz"
_DB_MAX_AGE = 30 * 86400


def _learn_secs():
    return float(CONFIG.get("egress_learn_hours", 48)) * 3600


def _db_path():
    return os.path.join(REFERENCE_DIR, "net_owners", "ip2asn-v4-u32.tsv.gz") if REFERENCE_DIR else ""


def _store_path():
    return os.path.join(PERSON_DIR, "egress_baseline.json") if PERSON_DIR else ""


# ── Network owners (offline table) ────────────────────────────────────────────
_db = {"starts": None, "ends": None, "asns": None, "names": {}, "rows": 0, "state": "none"}
_db_lock = threading.Lock()


def _load_db(path):
    starts, ends, asns, names = array("L"), array("L"), array("L"), {}
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as f:
        for ln in f:
            p = ln.rstrip("\n").split("\t")
            if len(p) < 5:
                continue
            try:
                a = int(p[2])
                if a == 0:
                    continue                       # "not routed"
                starts.append(int(p[0])); ends.append(int(p[1])); asns.append(a)
                if a not in names:
                    names[a] = p[4][:60]
            except ValueError:
                continue
    with _db_lock:
        _db.update(starts=starts, ends=ends, asns=asns, names=names, rows=len(starts), state="ready")
    log(f"[S32][EGRESS] network-owner table ready ({len(starts)} ranges, {len(names)} owners)")


def _fetch_db(path):
    """One anonymous download of the public table (no headers about this computer)."""
    try:
        import requests
        try:
            from s32_web import _proxies
            prox = _proxies()
        except Exception:
            prox = None
        os.makedirs(os.path.dirname(path), exist_ok=True)
        part = path + ".part"
        with requests.get(_DB_URL, stream=True, timeout=(15, 120), proxies=prox,
                          headers={"User-Agent": "System32"}) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}")
            with open(part, "wb") as f:
                for ch in r.iter_content(256 * 1024):
                    if ch:
                        f.write(ch)
        os.replace(part, path)
        log("[S32][EGRESS] network-owner table downloaded")
        return True
    except Exception as e:
        log(f"[S32][EGRESS] network-owner table not downloaded ({str(e)[:100]}) — grouping by address range for now")
        return False


def _ensure_db():
    path = _db_path()
    if not path:
        return
    have = os.path.isfile(path)
    if not have or time.time() - os.path.getmtime(path) > _DB_MAX_AGE:
        import s32_config as _cfg
        if _cfg.airplane():
            pass                                   # offline: use what's there
        else:
            _db["state"] = "downloading"
            _fetch_db(path)
    if os.path.isfile(path):
        try:
            _load_db(path)
        except Exception as e:
            _db["state"] = "failed"
            log(f"[S32][EGRESS] network-owner table unreadable: {e}")
    elif _db["state"] != "ready":
        _db["state"] = "missing"


def _private(ip):
    if not ip or ":" in ip and (ip.startswith("fe80") or ip.startswith("fc") or ip.startswith("fd") or ip == "::1"):
        return True
    if ":" in ip:
        return False
    return (ip.startswith(("10.", "127.", "192.168.", "169.254.", "0.")) or ip == "255.255.255.255"
            or any(ip.startswith(f"172.{b}.") for b in range(16, 32)) or ip.startswith(("224.", "239.")))


def owner_of(ip):
    """→ (key, label). key is stable for grouping ('AS15169'); label is for people."""
    if ":" in ip:
        parts = ip.split(":")
        k = "v6:" + ":".join(parts[:2])
        return k, k
    try:
        n = struct.unpack("!I", socket.inet_aton(ip))[0]
    except OSError:
        return "?", "?"
    with _db_lock:
        st = _db["starts"]
        if st is not None and len(st):
            i = bisect.bisect_right(st, n) - 1
            if i >= 0 and n <= _db["ends"][i]:
                a = _db["asns"][i]
                return f"AS{a}", f"{_db['names'].get(a, '')} (AS{a})".strip()
    k = ".".join(ip.split(".")[:2]) + ".*"
    return k, k


# ── Persistent knowledge (owners + ports per program; no addresses) ──────────
_kb = {"watched_secs": 0.0, "mature": False, "programs": {}}
_kb_lock = threading.Lock()
_dirty = {"v": False}


def _load_kb():
    p = _store_path()
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("programs"), dict):
                _kb.update(d)
        except Exception as e:
            log(f"[S32][EGRESS] couldn't read the outbound baseline: {e}")


def _save_kb():
    p = _store_path()
    if not p or not _dirty["v"] or state.get("resetting"):
        return
    try:
        with _kb_lock:
            data = json.dumps(_kb)
            _dirty["v"] = False
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, p)
    except Exception as e:
        log(f"[S32][EGRESS] save failed: {e}")


def _wpath(p):
    return str(p or "").replace("/", "\\").rstrip("\\").lower()


def _windows_own(exe):
    win = _wpath(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows")
    return bool(exe) and _wpath(exe).startswith(win + "\\")


def _own_stack(name, exe):
    try:
        import s32_irondome as _d
        return _d._is_protected_proc(name, exe)
    except Exception:
        return False


# ── Watching ──────────────────────────────────────────────────────────────────
_seen_conn = {}          # (pid, lport, rip, rport) → first seen (to count NEW connections)
_starts = {}             # (prog, rip) → [start times]   (memory only)
_alerted = set()         # alert keys raised this session
_alerts = {}             # id → alert (open ones)
_exe_cache = {}          # pid → (name, exe)


def _proc(pid):
    if pid in _exe_cache:
        return _exe_cache[pid]
    name, exe = "?", ""
    try:
        import psutil
        p = psutil.Process(pid)
        name = p.name()
        try:
            exe = p.exe() or ""
        except Exception:
            exe = ""
    except Exception:
        pass
    _exe_cache[pid] = (name, exe)
    return name, exe


def _outbound():
    """[(pid, name, exe, lport, rip, rport)] — established, to the internet."""
    out = []
    try:
        import psutil
        conns = psutil.net_connections(kind="inet")
    except Exception:
        return out
    for c in conns:
        try:
            if c.status != "ESTABLISHED" or not c.raddr or not c.pid:
                continue
            rip = c.raddr.ip
            if _private(rip):
                continue
            name, exe = _proc(c.pid)
            out.append((c.pid, name, exe, c.laddr.port if c.laddr else 0, rip, c.raddr.port))
        except Exception:
            continue
    return out


def _prog_key(name, exe):
    return _wpath(exe) or ("name:" + (name or "?").lower())


def _beacon(times):
    if len(times) < _BEACON_STARTS or times[-1] - times[0] < _BEACON_SPAN:
        return False
    gaps = [b - a for a, b in zip(times, times[1:])]
    m = sum(gaps) / len(gaps)
    if m <= 0:
        return False
    sd = (sum((g - m) ** 2 for g in gaps) / len(gaps)) ** 0.5
    return sd / m <= _BEACON_CV


def observe(conns, now=None):
    """Fold one look at the outbound connections into the baseline; return new alerts."""
    now = now or time.time()
    new_alerts = []
    live = set()
    with _kb_lock:
        mature = _kb.get("mature") or _kb.get("watched_secs", 0) >= _learn_secs()
        _kb["mature"] = bool(mature)
        for pid, name, exe, lport, rip, rport in conns:
            if _windows_own(exe):
                continue
            pk = _prog_key(name, exe)
            okey, olabel = owner_of(rip)
            ident = (pid, lport, rip, rport)
            live.add(ident)
            fresh = ident not in _seen_conn
            if fresh:
                _seen_conn[ident] = now
            prog = _kb["programs"].get(pk)
            first_time = prog is None
            if first_time:
                prog = {"name": name, "first": int(now), "owners": {}, "ports": {}, "learn_until": 0}
                _kb["programs"][pk] = prog
                if mature:
                    # a program that never used the network before: tell, then learn it quietly
                    prog["learn_until"] = int(now + 2 * 3600)
                    new_alerts.append(_mk_alert("first", pk, name, exe, okey, olabel, rip, rport,
                                                f"{name} is connecting to the internet for the first time "
                                                f"(to {olabel}, port {rport})."))
            learning = (not mature) or now < prog.get("learn_until", 0)
            wide = len(prog["owners"]) >= _WIDE_OWNERS
            known_owner = okey in prog["owners"]
            known_port = str(rport) in prog["ports"] or rport in _NORMAL_PORTS
            if fresh:
                ts = _starts.setdefault((pk, rip), [])
                ts.append(now)
                del ts[:-20]
            if not learning:
                if not known_owner and not wide:
                    new_alerts.append(_mk_alert("owner", pk, name, exe, okey, olabel, rip, rport,
                                                f"{name} is talking to a network it has never used: {olabel} "
                                                f"(port {rport})."))
                if not known_port:
                    new_alerts.append(_mk_alert("port", pk, name, exe, okey, olabel, rip, rport,
                                                f"{name} is connecting out on an unusual port: {rport} "
                                                f"(to {olabel})."))
                if not known_owner and fresh and _beacon(_starts.get((pk, rip), [])):
                    new_alerts.append(_mk_alert("beacon", pk, name, exe, okey, olabel, rip, rport,
                                                f"{name} keeps contacting {olabel} ({rip}) at a steady "
                                                f"rhythm — the pattern of something phoning home."))
            if learning:
                o = prog["owners"].setdefault(okey, 0)
                prog["owners"][okey] = o + 1
                prog["ports"][str(rport)] = prog["ports"].get(str(rport), 0) + 1
                _dirty["v"] = True
        for ident in [k for k in _seen_conn if k not in live]:
            _seen_conn.pop(ident, None)
    return [a for a in new_alerts if a]


def _mk_alert(kind, pk, name, exe, okey, olabel, rip, rport, reason):
    key = f"{kind}|{pk}|{okey if kind != 'port' else rport}"
    if key in _alerted:
        return None
    _alerted.add(key)
    aid = "eg:" + hashlib.sha1(key.encode()).hexdigest()[:12]
    a = {"id": aid, "egress": True, "kind": kind, "prog": pk, "name": name, "exe": exe,
         "owner": okey, "owner_label": olabel, "rip": rip, "port": rport, "reason": reason,
         "level": "high", "ts": time.strftime("%H:%M:%S")}
    _alerts[aid] = a
    return a


# ── Acting on an alert ────────────────────────────────────────────────────────
def _note(text):
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result({"content": text, "path": "⛨ Outbound"}, jid)
        _cfg._push_done(jid)
    except Exception:
        pass


def _block_now(a):
    """Dome armed → block this destination for this program, as a dome rule (Reopen and
    the boot cleanup remove it like every other dome rule)."""
    try:
        import s32_irondome as _d
    except Exception:
        return False
    if not (state.get("dome_armed") and _d._is_admin()):
        return False
    if not a.get("exe") or _windows_own(a["exe"]) or _own_stack(a.get("name"), a.get("exe")):
        return False
    h = hashlib.sha1(f"{_wpath(a['exe'])}|{a['rip']}".encode()).hexdigest()[:8]
    rn = f"{_d._DOME_PREFIX}egress_{os.path.basename(a['exe'].replace(chr(92), '/'))}_{h}"
    r = _d._run_hidden(["netsh", "advfirewall", "firewall", "add", "rule", f"name={rn}", "dir=out",
                        "action=block", f"program={a['exe']}", f"remoteip={a['rip']}", "enable=yes"])
    ok = getattr(r, "returncode", 1) == 0
    if ok:
        log(f"[S32][EGRESS] dome armed — blocked {a['name']} → {a['owner_label']} ({a['rip']})")
    return ok


def _raise(a):
    blocked = _block_now(a)
    tail = (" The Iron Dome is armed, so I blocked that destination for it — Reopen lifts it."
            if blocked else " I haven't touched it. Mark it safe if you expected this, Block it to cut "
                            "that program off from it, or arm the Iron Dome.")
    state["security_alert"] = {"level": "high", "reason": a["reason"] + (" (blocked)" if blocked else ""),
                               "name": a["name"], "port": a["port"], "id": a["id"], "egress": True}
    _note(f"⚠ {a['reason']}\nProgram: {a['exe'] or a['name']}" + tail)
    log(f"[S32][EGRESS] ALERT [{a['kind']}] {a['reason']}" + (" — BLOCKED (dome armed)" if blocked else ""))
    try:
        import voice as _voice
        _voice.speak(f"Heads up. {a['reason']}", priority=True)
    except Exception:
        pass


def trust(alert_id):
    """'Mark safe': this owner/port becomes normal for that program."""
    a = _alerts.pop(alert_id, None)
    if not a:
        return {"ok": False, "error": "That alert is no longer open."}
    with _kb_lock:
        prog = _kb["programs"].setdefault(a["prog"], {"name": a["name"], "first": int(time.time()),
                                                      "owners": {}, "ports": {}, "learn_until": 0})
        prog["owners"][a["owner"]] = prog["owners"].get(a["owner"], 0) + 1
        prog["ports"][str(a["port"])] = prog["ports"].get(str(a["port"]), 0) + 1
        _dirty["v"] = True
    _clear_banner(alert_id)
    _save_kb()
    log(f"[S32][EGRESS] marked safe: {a['name']} → {a['owner_label']} port {a['port']}")
    return {"ok": True, "trusted": {"program": a["name"], "owner": a["owner_label"], "port": a["port"]}}


def dismiss(alert_id):
    _alerts.pop(alert_id, None)
    _clear_banner(alert_id)
    return {"ok": True}


def _clear_banner(alert_id):
    sa = state.get("security_alert") or {}
    if sa.get("id") == alert_id:
        nxt = next(iter(_alerts.values()), None)
        state["security_alert"] = ({"level": "high", "reason": nxt["reason"], "name": nxt["name"],
                                    "port": nxt["port"], "id": nxt["id"], "egress": True} if nxt else None)


def baseline_now():
    """The operator says 'this machine is clean right now': stop learning, start watching."""
    with _kb_lock:
        _kb["mature"] = True
        for prog in _kb["programs"].values():
            prog["learn_until"] = 0
        _dirty["v"] = True
    _save_kb()
    log("[S32][EGRESS] baseline set by the operator — now watching for drift")
    return status()


def status():
    with _kb_lock:
        secs = _kb.get("watched_secs", 0.0)
        mature = bool(_kb.get("mature") or secs >= _learn_secs())
        progs = _kb["programs"]
        wide = sum(1 for p in progs.values() if len(p.get("owners", {})) >= _WIDE_OWNERS)
        n = len(progs)
    return {"mature": mature, "watched_hours": round(secs / 3600, 1),
            "learn_hours": round(_learn_secs() / 3600, 1), "programs": n, "wide": wide,
            "open_alerts": len(_alerts), "owners_table": _db["state"], "owners_rows": _db["rows"],
            "blocks": len(_load_blocks())}


# ── Manual blocks (drop 2): permanent, listed, undoable ──────────────────────
#  'Block it' on an outbound alert → a Windows Firewall rule named S32_Egress_… that the
#  dome's Reopen / boot cleanup does NOT remove (it isn't a lockdown — it's a decision).
#  Every start checks each one is still in the firewall and puts back any that went
#  missing; the Iron Dome panel lists them with Unblock. Factory reset removes them.
_EG_PREFIX = "S32_Egress_"
_OWNER_RANGES_MAX = 200          # a small owner is blocked whole; a giant one (Google…) by address
_blocks_lock = threading.Lock()


def _blocks_path():
    return os.path.join(PERSON_DIR, "egress_blocks.json") if PERSON_DIR else ""


def _load_blocks():
    p = _blocks_path()
    if p and os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except Exception:
            pass
    return []


def _save_blocks(lst):
    p = _blocks_path()
    if not p:
        return
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(lst, f, indent=1)
    os.replace(tmp, p)


def _owner_ranges(okey):
    """All address ranges of a network owner ('AS63949') as netsh 'a-b' strings, or None."""
    if not okey.startswith("AS"):
        return None
    try:
        a = int(okey[2:])
    except ValueError:
        return None
    out = []
    with _db_lock:
        st, en, asns = _db["starts"], _db["ends"], _db["asns"]
        if st is None:
            return None
        for i in range(len(asns)):
            if asns[i] == a:
                out.append(f"{socket.inet_ntoa(struct.pack('!I', st[i]))}-{socket.inet_ntoa(struct.pack('!I', en[i]))}")
                if len(out) > _OWNER_RANGES_MAX:
                    return None
    return out or None


def _fw():
    import s32_irondome as _d
    return _d


def _add_rule(rule, exe, remote):
    """remote: one address, or a list of 'a-b' ranges (netsh caps a rule's list — split)."""
    d = _fw()
    parts = [remote] if isinstance(remote, str) else [",".join(remote[i:i + 60]) for i in range(0, len(remote), 60)]
    ok = True
    for i, rem in enumerate(parts):
        r = d._run_hidden(["netsh", "advfirewall", "firewall", "add", "rule",
                           f"name={rule}" + (f"_{i}" if len(parts) > 1 else ""), "dir=out", "action=block",
                           f"program={exe}", f"remoteip={rem}", "enable=yes"])
        ok = ok and getattr(r, "returncode", 1) == 0
    return ok


def _existing_eg_rules():
    d = _fw()
    r = d._run_hidden(["powershell", "-NoProfile", "-Command",
                       f"Get-NetFirewallRule -DisplayName '{_EG_PREFIX}*' -ErrorAction SilentlyContinue "
                       f"| ForEach-Object {{ $_.DisplayName }}"])
    if getattr(r, "returncode", 1) != 0:
        return None
    return {ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()}


def block(alert_id):
    """'Block it' on an outbound alert: this destination, for this program only — permanently."""
    a = _alerts.get(alert_id)
    if not a:
        return {"ok": False, "error": "That alert is no longer open."}
    d = _fw()
    if not d._is_admin():
        return {"ok": False, "needs_admin": True,
                "error": "Blocking needs administrator rights. Start System 32 with 'Run as administrator', "
                         "or arm the Iron Dome next time — this one stays open until you decide."}
    if not a.get("exe") or _windows_own(a["exe"]) or _own_stack(a.get("name"), a.get("exe")):
        return {"ok": False, "error": "That program can't be blocked from here (Windows' own, or part of System 32)."}
    ranges = _owner_ranges(a["owner"])
    remote = ranges if ranges else a["rip"]
    what = (f"all of {a['owner_label']} ({len(ranges)} range(s))" if ranges else f"{a['rip']} ({a['owner_label']})")
    h = hashlib.sha1(f"{_wpath(a['exe'])}|{a['owner'] if ranges else a['rip']}".encode()).hexdigest()[:8]
    rule = f"{_EG_PREFIX}{os.path.basename(a['exe'].replace(chr(92), '/'))}_{h}"
    if not _add_rule(rule, a["exe"], remote):
        return {"ok": False, "error": "Windows Firewall refused the rule."}
    with _blocks_lock:
        lst = [b for b in _load_blocks() if b.get("rule") != rule]
        lst.append({"rule": rule, "name": a["name"], "exe": a["exe"], "what": what,
                    "remote": remote, "created": time.strftime("%Y-%m-%d %H:%M")})
        _save_blocks(lst)
    _alerts.pop(alert_id, None)
    _clear_banner(alert_id)
    log(f"[S32][EGRESS] BLOCKED (by the operator): {a['name']} → {what}")
    _note(f"Blocked {a['name']} from reaching {what}. It stays blocked across restarts — "
          f"undo it in the Iron Dome panel (Unblock).")
    return {"ok": True, "rule": rule, "what": what}


def blocks():
    return {"blocks": [{k: b.get(k) for k in ("rule", "name", "exe", "what", "created")} for b in _load_blocks()]}


def unblock(rule):
    d = _fw()
    if not d._is_admin():
        return {"ok": False, "needs_admin": True, "error": "Unblocking needs administrator rights."}
    if not str(rule or "").startswith(_EG_PREFIX) or any(c in str(rule) for c in "'\"\n\r`$;"):
        return {"ok": False, "error": "Not an outbound block."}
    d._run_hidden(["powershell", "-NoProfile", "-Command",
                   f"Remove-NetFirewallRule -DisplayName '{rule}*' -ErrorAction SilentlyContinue"])
    left = _existing_eg_rules()
    if left and any(x == rule or x.startswith(rule + "_") for x in left):
        return {"ok": False, "error": "Windows Firewall kept the rule — try again as administrator."}
    with _blocks_lock:
        _save_blocks([b for b in _load_blocks() if b.get("rule") != rule])
    log(f"[S32][EGRESS] unblocked: {rule}")
    return {"ok": True}


def remove_all_blocks():
    """Factory reset: every outbound block rule and the list (needs admin for the rules)."""
    try:
        _fw()._run_hidden(["powershell", "-NoProfile", "-Command",
                           f"Remove-NetFirewallRule -DisplayName '{_EG_PREFIX}*' -ErrorAction SilentlyContinue"])
    except Exception:
        pass
    p = _blocks_path()
    if p and os.path.isfile(p):
        os.remove(p)


def _verify_blocks():
    """Every start: each saved block must really be in Windows Firewall."""
    lst = _load_blocks()
    if not lst:
        return
    have = _existing_eg_rules()
    if have is None:
        return
    missing = [b for b in lst if not any(x == b["rule"] or x.startswith(b["rule"] + "_") for x in have)]
    if not missing:
        log(f"[S32][EGRESS] {len(lst)} outbound block(s) in place")
        return
    if _fw()._is_admin():
        for b in missing:
            _add_rule(b["rule"], b["exe"], b["remote"])
        log(f"[S32][EGRESS] {len(missing)} outbound block(s) had gone missing from Windows Firewall — put back")
    else:
        msg = (f"⚠ {len(missing)} of your outbound blocks are missing from Windows Firewall "
               f"({', '.join(b['name'] for b in missing[:4])}). Start System 32 as administrator once to put them back.")
        log(f"[S32][EGRESS] {msg}")
        state["startup_notification"] = msg


# ── Loop ──────────────────────────────────────────────────────────────────────
def _loop():
    last = time.time()
    last_save = last
    while True:
        time.sleep(_POLL)
        now = time.time()
        try:
            for a in observe(_outbound(), now):
                _raise(a)
            with _kb_lock:
                _kb["watched_secs"] = _kb.get("watched_secs", 0.0) + min(now - last, 60)
                _dirty["v"] = True
            if len(_exe_cache) > 4000:
                _exe_cache.clear()
        except Exception as e:
            log(f"[S32][EGRESS] watch error: {e}")
        last = now
        if now - last_save > 60:
            _save_kb()
            last_save = now


def start():
    _load_kb()
    try:
        _verify_blocks()
    except Exception as e:
        log(f"[S32][EGRESS] couldn't check the outbound blocks: {e}")
    threading.Thread(target=_ensure_db, daemon=True, name="egress-owners").start()
    threading.Thread(target=_loop, daemon=True, name="egress-watch").start()
    st = status()
    log(f"[S32][EGRESS] outbound watcher started — "
        + ("watching for drift" if st["mature"] else
           f"learning ({st['watched_hours']} of {st['learn_hours']} h)")
        + f", {st['programs']} program(s) known")
