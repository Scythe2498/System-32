# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_irondome.py — "The Iron Dome": port/process baselining + close-planning.

This is the LEARNING + CONTROL brain. It is deliberately NON-DESTRUCTIVE in this
increment: it samples the machine over many iterations (and across reboots),
learns which ports/processes are persistently present (necessary to run the
desktop) versus those that come and go (unnecessary / transient), maintains an
operator-editable whitelist, and can compute a "close plan" — exactly what an arm
action WOULD close — with protected and whitelisted items always held back.

Actually closing/opening (firewall block + guarded kill + keep-closed watcher +
the red↔green armed state) is the NEXT increment, wired only after the plan has
been seen to aim correctly on the real machine. Nothing here kills or blocks.

Persistence (so baselining accumulates "over iterations", even across reboots):
  • irondome_baseline.json   — per-signature presence stats
  • irondome_whitelist.json  — operator/​system whitelist (ports + process names)

Protected set (never in any close plan) is borrowed from s32_firewall: its own
stack (llama-server / ComfyUI / system32 / browser) + critical Windows processes.
"""

import os
import json
import time
import threading

try:
    from s32_config import CONFIG, REFERENCE_DIR, state, log
except Exception:                                    # keep importable in isolation
    CONFIG, REFERENCE_DIR, state, log = {}, None, {}, (lambda *a, **k: None)

# Borrow scanning + the protected set from the firewall (optional import).
try:
    import s32_firewall as _fw
except Exception:
    _fw = None


# ── Tunables (config-overridable) ────────────────────────────────────────────
def _MIN_SAMPLES():   return int(CONFIG.get("dome_min_samples", 5))     # before judging
def _NEC_RATIO():     return float(CONFIG.get("dome_necessary_ratio", 0.8))  # present≥80% ⇒ core
def _TRANS_RATIO():   return float(CONFIG.get("dome_transient_ratio", 0.5))  # present<50% ⇒ comes/goes
def _SAMPLE_SECS():   return int(CONFIG.get("dome_sample_secs", 90))    # background sample cadence


def _baseline_path():
    return os.path.join(REFERENCE_DIR, "irondome_baseline.json") if REFERENCE_DIR else None


def _whitelist_path():
    return os.path.join(REFERENCE_DIR, "irondome_whitelist.json") if REFERENCE_DIR else None


# ── Persistence ──────────────────────────────────────────────────────────────
def _load_json(path, default):
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path, data):
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        log(f"[S32][DOME] save failed ({os.path.basename(path or '?')}): {e}")


_lock     = threading.Lock()
_baseline = _load_json(_baseline_path(), {"samples": 0, "items": {}})
_whitelist = _load_json(_whitelist_path(), {"ports": [], "procs": []})


# ── Whitelist (operator-editable + system-updatable; whitelisted = always held) ─
def _norm_proc(name):
    return (name or "").strip().lower()


def get_whitelist():
    return {"ports": sorted(set(int(p) for p in _whitelist.get("ports", []) if str(p).strip().isdigit())),
            "procs": sorted(set(_norm_proc(p) for p in _whitelist.get("procs", []) if _norm_proc(p)))}


def set_whitelist(ports=None, procs=None, text=None):
    """Replace the whitelist. Accepts explicit lists, or free-form `text` from the
    editable box (comma/space/newline separated; numbers→ports, words→proc names)."""
    if text is not None:
        ports, procs = [], []
        for tok in text.replace(",", " ").replace(";", " ").split():
            tok = tok.strip()
            if not tok:
                continue
            (ports if tok.isdigit() else procs).append(tok)
    with _lock:
        if ports is not None:
            _whitelist["ports"] = sorted(set(int(p) for p in ports if str(p).strip().isdigit()))
        if procs is not None:
            _whitelist["procs"] = sorted(set(_norm_proc(p) for p in procs if _norm_proc(p)))
        _save_json(_whitelist_path(), _whitelist)
    log(f"[S32][DOME] whitelist set: {len(_whitelist['ports'])} ports, {len(_whitelist['procs'])} procs")
    return get_whitelist()


def add_to_whitelist(ports=None, procs=None):
    """Merge items into the whitelist (used by the natural-language tool)."""
    with _lock:
        wp = set(_whitelist.get("ports", []))
        wc = set(_whitelist.get("procs", []))
        for p in (ports or []):
            if str(p).strip().isdigit():
                wp.add(int(p))
        for c in (procs or []):
            if _norm_proc(c):
                wc.add(_norm_proc(c))
        _whitelist["ports"] = sorted(wp)
        _whitelist["procs"] = sorted(wc)
        _save_json(_whitelist_path(), _whitelist)
    log(f"[S32][DOME] whitelist += ports={ports or []} procs={procs or []}")
    return get_whitelist()


def _is_whitelisted_port(port):
    return int(port) in set(_whitelist.get("ports", []))


def _is_whitelisted_proc(name):
    return _norm_proc(name) in set(_whitelist.get("procs", []))


# ── Protected (never closable) — from firewall ───────────────────────────────
def _is_protected_proc(name, exe=""):
    n = _norm_proc(name)
    if _fw is not None:
        try:
            if n in _fw._CRITICAL_PROC:
                return True
            blob = (n + " " + (exe or "")).lower()
            if any(h in blob for h in _fw._OWN_NAME_HINTS) or \
               any(h in blob for h in _fw._OWN_EXE_HINTS):
                return True
        except Exception:
            pass
    # Last-resort safety net even if firewall import failed:
    return n in {"system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
                 "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "explorer.exe",
                 "llama-server.exe", "llama-server", "python.exe", "comfyui"}


def _wpath(p):
    """A Windows path, lower-cased with backslashes (no filesystem calls)."""
    return str(p or "").replace("/", "\\").rstrip("\\").lower()


def _windows_dir():
    return _wpath(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows")


def _is_windows_own(exe):
    """A program running from Windows' own folder (C:\\Windows\\…). The operator's call
    (2026-10-31): Windows' programs are left alone — never blocked, never killed. Arming
    once hit TrustedInstaller/TiWorker (Windows Update servicing — killing them mid-update
    can corrupt updates), SmartScreen and the search indexer. Writing into C:\\Windows needs
    admin, so something that can plant itself there is already past what the dome stops."""
    if not exe:
        return False
    return _wpath(exe).startswith(_windows_dir() + "\\")


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_protected_port(port):
    if _fw is not None:
        try:
            if int(port) in _fw._PROTECTED_PORTS:
                return True
        except Exception:
            pass
    # its web UI / model server / ComfyUI ports
    return int(port) in {1234, 7432, 8188}


# ── Sampling ─────────────────────────────────────────────────────────────────
def _list_processes():
    """Current process image names (psutil if present, else tasklist)."""
    names = {}
    try:
        import psutil
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                info = p.info
                names[info["pid"]] = (info.get("name") or "", info.get("exe") or "")
            except Exception:
                continue
        return names
    except Exception:
        pass
    if _fw is not None:
        try:
            for pid, nm in _fw._tasklist_names().items():
                names[pid] = (nm, "")
        except Exception:
            pass
    return names


_port_exe = {}          # port → program path of what listens on it (last sample)


def _udp_listeners():
    """UDP sockets bound to a port with no remote end = something listening on UDP. The
    TCP-only scan never saw these — the classic place to hide (2026-10-31: 'piggybacking on
    a normal port is exactly what I'd try')."""
    out = {}
    try:
        import psutil
        for c in psutil.net_connections(kind="udp"):
            try:
                if not c.laddr or c.raddr:
                    continue
                name, exe = (_fw._proc_name_exe(c.pid) if _fw is not None else ("?", ""))
                out[int(c.laddr.port)] = (name or "?", exe or "")
            except Exception:
                continue
    except Exception:
        pass
    return out


def sample_now():
    """One snapshot: listening ports {port: proc} (TCP and UDP) and processes {name: exe}."""
    ports, procs = {}, {}
    pexe = {}
    # ports from firewall connection scan (listening sockets)
    if _fw is not None:
        try:
            for c in _fw._scan():
                st = (c.get("status") or "").upper()
                lp = c.get("lport") or 0
                if lp and ("LISTEN" in st):
                    ports[int(lp)] = c.get("name") or "?"
                    pexe[int(lp)] = c.get("exe") or ""
        except Exception as e:
            log(f"[S32][DOME] port scan failed: {e}")
    for lp, (nm, ex) in _udp_listeners().items():
        if lp not in ports:
            ports[lp] = nm
            pexe[lp] = ex
    _port_exe.clear()
    _port_exe.update(pexe)
    # processes
    for pid, (nm, exe) in _list_processes().items():
        if nm:
            procs[_norm_proc(nm)] = exe
    return ports, procs


def record_sample():
    """Fold one sample into the persisted baseline stats."""
    ports, procs = sample_now()
    now = int(time.time())
    present = set()
    for port, proc in ports.items():
        present.add(f"port:{port}")
    for name in procs:
        present.add(f"proc:{name}")

    with _lock:
        _baseline["samples"] = _baseline.get("samples", 0) + 1
        items = _baseline.setdefault("items", {})
        # bump everyone's total; bump present count for those seen now
        for sig in set(list(items.keys())) | present:
            it = items.setdefault(sig, {"present": 0, "total": 0, "first": now, "last": 0,
                                        "label": _label_for(sig, ports, procs)})
            it["total"] += 1
            if sig in present:
                it["present"] += 1
                it["last"] = now
                # keep a fresh human label
                lbl = _label_for(sig, ports, procs)
                if lbl:
                    it["label"] = lbl
        _save_json(_baseline_path(), _baseline)
    return len(present)


def _label_for(sig, ports, procs):
    try:
        if sig.startswith("port:"):
            p = int(sig.split(":", 1)[1])
            return f"port {p}" + (f" · {ports[p]}" if p in ports else "")
        if sig.startswith("proc:"):
            return sig.split(":", 1)[1]
    except Exception:
        pass
    return sig


# ── Classification ───────────────────────────────────────────────────────────
def classify():
    """Bucket every known signature into necessary / unnecessary / new."""
    nec, unnec, new = [], [], []
    with _lock:
        total_samples = _baseline.get("samples", 0)
        for sig, it in _baseline.get("items", {}).items():
            tot = it.get("total", 0)
            pres = it.get("present", 0)
            ratio = (pres / tot) if tot else 0.0
            entry = {"sig": sig, "label": it.get("label", sig),
                     "ratio": round(ratio, 3), "present": pres, "total": tot,
                     "last": it.get("last", 0)}
            if tot < _MIN_SAMPLES():
                new.append(entry)
            elif ratio >= _NEC_RATIO():
                nec.append(entry)
            elif ratio < _TRANS_RATIO():
                unnec.append(entry)
            else:
                # middling presence — treat as necessary-ish (don't close on the fence)
                nec.append(entry)
    nec.sort(key=lambda e: -e["ratio"])
    unnec.sort(key=lambda e: e["ratio"])
    return {"samples": total_samples, "necessary": nec, "unnecessary": unnec, "new": new}


def compute_close_plan():
    """What an arm action WOULD close: currently-present items classified as
    unnecessary, MINUS protected, MINUS whitelisted. Nothing is closed here."""
    ports_now, procs_now = sample_now()
    cls = classify()
    unnec_sigs = {e["sig"] for e in cls["unnecessary"]} | {e["sig"] for e in cls["new"]}

    close_ports, close_procs = [], []
    held_protected, held_whitelisted = [], []

    for port, proc in ports_now.items():
        sig = f"port:{port}"
        if sig not in unnec_sigs:
            continue
        if _is_protected_port(port) or _is_protected_proc(proc) or _is_windows_own(_port_exe.get(port)):
            held_protected.append({"port": port, "proc": proc}); continue
        if _is_whitelisted_port(port) or _is_whitelisted_proc(proc):
            held_whitelisted.append({"port": port, "proc": proc}); continue
        close_ports.append({"port": port, "proc": proc})

    for name in procs_now:
        sig = f"proc:{name}"
        if sig not in unnec_sigs:
            continue
        if _is_protected_proc(name) or _is_windows_own(procs_now.get(name)):
            held_protected.append({"proc": name}); continue
        if _is_whitelisted_proc(name):
            held_whitelisted.append({"proc": name}); continue
        close_procs.append({"proc": name})

    return {
        "close_ports": sorted(close_ports, key=lambda x: x["port"]),
        "close_procs": sorted(close_procs, key=lambda x: x["proc"]),
        "held_protected": held_protected,
        "held_whitelisted": held_whitelisted,
        "samples": cls["samples"],
        "ready": cls["samples"] >= _MIN_SAMPLES(),
    }


# ── Natural-language whitelist: "whitelist the ports that X connects" ─────────
def whitelist_target(target):
    """Find the listening ports + process names associated with `target`
    (a process/app name, partial match) and add them to the whitelist."""
    target = (target or "").strip().lower()
    if not target:
        return {"ok": False, "error": "No target given."}
    ports_now, procs_now = sample_now()
    matched_ports, matched_procs = set(), set()
    for port, proc in ports_now.items():
        if target in _norm_proc(proc):
            matched_ports.add(port)
            matched_procs.add(_norm_proc(proc))
    for name in procs_now:
        if target in name:
            matched_procs.add(name)
    if not matched_ports and not matched_procs:
        return {"ok": False, "error": f"Nothing currently matching '{target}' is listening or running.",
                "matched_ports": [], "matched_procs": []}
    add_to_whitelist(ports=list(matched_ports), procs=list(matched_procs))
    return {"ok": True, "matched_ports": sorted(matched_ports),
            "matched_procs": sorted(matched_procs), "whitelist": get_whitelist()}


# ── ARM / DISARM — reversible firewall-based close, with a keep-closed watcher ─
#   Everything is a named Windows Firewall rule ("S32_IronDome_*"). Disarm — and
#   every reboot — removes them all (fail-open). Its own ports/procs can never be
#   touched: compute_close_plan already excludes them, and _safe_plan re-checks.
_DOME_PREFIX = "S32_IronDome_"
_NEVER_PORTS = {1234, 7432, 8188}          # llama-server / web UI / ComfyUI
_dome = {"armed": False, "mode": "block", "watcher": False}


def _run_hidden(args):
    import subprocess
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as e:
        log(f"[S32][DOME] command failed: {e}")
        class _R: returncode = 1
        return _R()


def _existing_dome_rules():
    """Names of the dome rules Windows Firewall holds right now (None if it can't be read)."""
    r = _run_hidden(["powershell", "-NoProfile", "-Command",
                     f"Get-NetFirewallRule -DisplayName '{_DOME_PREFIX}*' -ErrorAction SilentlyContinue "
                     f"| ForEach-Object {{ $_.DisplayName }}"])
    if getattr(r, "returncode", 1) != 0:
        return None
    return {ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()}


def _port_rule(port):
    return f"{_DOME_PREFIX}port_{port}"


def _proc_rule(exe):
    import hashlib
    import ntpath
    h = hashlib.sha1(_wpath(exe).encode("utf-8", "replace")).hexdigest()[:8]
    return f"{_DOME_PREFIX}proc_{ntpath.basename(exe)}_{h}"      # same name, other folder = other rule


def _fw_block_port(port):
    name = _port_rule(port)
    ok = True
    for direction in ("in", "out"):
        for proto in ("TCP", "UDP"):
            r = _run_hidden(["netsh", "advfirewall", "firewall", "add", "rule",
                             f"name={name}", f"dir={direction}", "action=block",
                             f"protocol={proto}", f"localport={port}"])
            if getattr(r, "returncode", 1) != 0:
                ok = False
    return ok


def _fw_block_program(exe):
    name = _proc_rule(exe)
    ok = True
    for direction in ("in", "out"):
        r = _run_hidden(["netsh", "advfirewall", "firewall", "add", "rule",
                         f"name={name}", f"dir={direction}", "action=block",
                         f"program={exe}", "enable=yes"])
        if getattr(r, "returncode", 1) != 0:
            ok = False
    return ok


def _fw_remove_all_dome_rules():
    # PowerShell wildcard delete removes every rule we ever added, in one shot.
    _run_hidden(["powershell", "-NoProfile", "-Command",
                 f"Remove-NetFirewallRule -DisplayName '{_DOME_PREFIX}*' -ErrorAction SilentlyContinue"])


def _resolve_exe(name):
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if _norm_proc(p.info.get("name") or "") == _norm_proc(name):
                    e = p.info.get("exe")
                    if e:
                        return e
            except Exception:
                continue
    except Exception:
        pass
    return None


def _resolve_exes(name):
    """EVERY program path running under this name — a copy with the same name in another
    folder (the classic disguise) gets its own block, not just the first one found."""
    out = []
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if _norm_proc(p.info.get("name") or "") == _norm_proc(name):
                    e = p.info.get("exe")
                    if e and e not in out:
                        out.append(e)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _kill_proc(name):
    killed = 0
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            try:
                if _norm_proc(p.info.get("name") or "") == _norm_proc(name):
                    if _is_protected_proc(p.info.get("name") or ""):
                        continue
                    try:
                        _exe = p.exe() or ""
                    except Exception:
                        _exe = ""
                    if not _exe or _is_windows_own(_exe):
                        continue          # can't tell where it runs from, or it's Windows' own
                    p.kill()
                    killed += 1
            except Exception:
                continue
    except Exception:
        pass
    return killed


_dyn = {"range": None}


def _dynamic_range():
    """Windows' dynamic (ephemeral) port range — numbers it hands out for seconds at a time
    (DNS lookups, browser/voice UDP). Read once from netsh; default 49152-65535."""
    if _dyn["range"] is None:
        lo, n = 49152, 16384
        try:
            r = _run_hidden(["netsh", "int", "ipv4", "show", "dynamicport", "udp"])
            import re as _r
            txt = getattr(r, "stdout", "") or ""
            a = _r.search(r"Start Port\s*:\s*(\d+)", txt)
            b = _r.search(r"Number of Ports\s*:\s*(\d+)", txt)
            if a and b:
                lo, n = int(a.group(1)), int(b.group(1))
        except Exception:
            pass
        _dyn["range"] = (lo, lo + n - 1)
    return _dyn["range"]


def _is_dynamic(port):
    lo, hi = _dynamic_range()
    return lo <= int(port) <= hi


def _safe_plan():
    """compute_close_plan, with a second hard exclusion of its ports/processes."""
    plan = compute_close_plan()
    plan["close_ports"] = [x for x in plan["close_ports"]
                           if int(x["port"]) not in _NEVER_PORTS
                           and not _is_protected_proc(x.get("proc", ""))
                           and not _is_protected_port(x["port"])]
    plan["close_procs"] = [x for x in plan["close_procs"]
                           if not _is_protected_proc(x["proc"])]
    # A port in the dynamic range is a throwaway number: blocking it does nothing to the
    # program (it gets another one next time) and later hits whatever innocent program
    # Windows hands that number to (2026-10-31: the rule list grew every minute). Close the
    # PROGRAM that opened it instead — that stops it on every port.
    fixed, dyn_owners = [], []
    for x in plan["close_ports"]:
        if _is_dynamic(x["port"]):
            nm = _norm_proc(x.get("proc"))
            if nm and nm != "?" and not any(c["proc"] == nm for c in plan["close_procs"]) \
                    and not _is_whitelisted_proc(nm) and not _is_windows_own(_port_exe.get(x["port"])):
                dyn_owners.append({"proc": nm, "via_port": x["port"]})
        else:
            fixed.append(x)
    plan["close_ports"] = fixed
    plan["close_procs"] = plan["close_procs"] + [d for i, d in enumerate(dyn_owners)
                                                 if d["proc"] not in {e["proc"] for e in dyn_owners[:i]}]
    return plan


def _apply_once():
    """Apply the current plan once. Truly idempotent: a rule Windows Firewall already holds
    is never added again (2026-10-31: smartscreen got a second pair every cycle — in block
    mode that's a new pair per program every 15 s). Returns (ports, procs, killed) for what
    was NEWLY blocked/killed this time."""
    mode = _dome.get("mode", "block")
    plan = _safe_plan()
    have = _existing_dome_rules()
    if have is None:
        have = set(_dome.get("applied") or ())
    bp, bpr, kl = [], [], []
    for x in plan["close_ports"]:
        rn = _port_rule(x["port"])
        if rn in have:
            continue
        if _fw_block_port(x["port"]):
            bp.append(f"{x['port']} ({x.get('proc') or '?'})")
            have.add(rn)
    for x in plan["close_procs"]:
        nm = x["proc"]
        if mode in ("block", "both"):
            for exe in _resolve_exes(nm):
                if _is_windows_own(exe):
                    continue
                rn = _proc_rule(exe)
                if rn not in have and _fw_block_program(exe):
                    bpr.append(nm if nm not in bpr else exe)
                    have.add(rn)
        if mode in ("kill", "both"):
            if _kill_proc(nm):
                kl.append(nm)
    _dome["applied"] = set(have)
    return bp, bpr, kl


_apply_lock = threading.Lock()      # one apply at a time; disarm waits for a running one


def _note(text):
    """A line in chat (the operator must SEE what a lockdown hit)."""
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result({"content": text, "path": "⛨ Iron Dome"}, jid)
        _cfg._push_done(jid)
    except Exception:
        pass


def arm(mode="block"):
    """Arm the dome. Returns immediately — the firewall work (many slow netsh
    calls) runs in the keep-closed watcher thread so it never blocks the request.
    mode: 'block' | 'kill' | 'both'. Ports are always firewall-blocked."""
    if not _is_admin():
        # Without admin every firewall rule silently fails — it used to say ARMED anyway
        # (2026-10-31: '0 port(s) blocked, 0 proc(s) net-blocked'). Never a false 'armed'.
        log("[S32][DOME] arm refused — System 32 isn't running as administrator")
        return {"ok": False, "armed": False, "needs_admin": True,
                "error": "The Iron Dome needs administrator rights to change the Windows Firewall. "
                         "Close System 32 and start it with 'Run as administrator'."}
    mode = mode if mode in ("block", "kill", "both") else "block"
    _dome["mode"] = mode
    _dome["armed"] = True
    state["dome_armed"] = True
    _start_dome_watcher()
    log(f"[S32][DOME] ARMING (mode={mode}) — applying firewall rules in background…")
    return {"ok": True, "armed": True, "mode": mode}


def disarm():
    _dome["armed"] = False
    state["dome_armed"] = False
    with _apply_lock:                  # a cycle mid-apply finishes first, then everything goes
        _fw_remove_all_dome_rules()
        _dome["applied"] = set()
        _dome["hit"] = {"ports": [], "procs": [], "killed": []}
    left = _existing_dome_rules()
    if left:
        log(f"[S32][DOME] DISARM: {len(left)} rule(s) still present — retrying")
        _fw_remove_all_dome_rules()
        left = _existing_dome_rules()
    if left:
        log(f"[S32][DOME] ⚠ DISARM INCOMPLETE — {len(left)} rule(s) remain: {', '.join(sorted(left))[:300]}")
        _note(f"⚠ Reopen incomplete — {len(left)} firewall rule(s) are still in place. Restart System 32 as administrator to clear them.")
        return {"ok": False, "armed": False, "left": sorted(left)}
    log("[S32][DOME] DISARMED — all dome firewall rules removed, ports reopened")
    _note("Reopened — every Iron Dome firewall rule removed.")
    return {"ok": True, "armed": False}


def _start_dome_watcher():
    if _dome.get("watcher"):
        return
    _dome["watcher"] = True
    threading.Thread(target=_dome_watcher_loop, daemon=True, name="dome-watcher").start()


def _dome_watcher_loop():
    """Applies the plan immediately, then keeps it applied (re-blocking stripped
    rules + catching newly-appearing unnecessary ports) every 15s while armed."""
    first = True
    _dome["hit"] = {"ports": [], "procs": [], "killed": []}
    while _dome.get("armed"):
        try:
            with _apply_lock:
                if not _dome.get("armed"):           # disarmed while waiting — add nothing
                    break
                bp, bpr, kl = _apply_once()
            hit = _dome.setdefault("hit", {"ports": [], "procs": [], "killed": []})
            for k, v in (("ports", bp), ("procs", bpr), ("killed", kl)):
                hit[k] = sorted(set(hit[k]) | set(v))
            _what = "; ".join(x for x in (
                ("blocked ports: " + ", ".join(bp)) if bp else "",
                ("net-blocked: " + ", ".join(bpr)) if bpr else "",
                ("killed: " + ", ".join(sorted(set(kl)))) if kl else "") if x)
            if first:
                log(f"[S32][DOME] ARMED (mode={_dome['mode']}): {len(bp)} port(s) blocked, "
                    f"{len(bpr)} proc(s) net-blocked, {len(kl)} killed"
                    + (f" — {_what}" if _what else ""))
                _note(f"Armed ({_dome['mode']}). " + (_what + "." if _what else
                      "Nothing to close right now — it keeps watching every 15 s."))
                _dome["last"] = {"ports": bp, "procs": bpr, "killed": kl}
                first = False
            elif _what:
                log(f"[S32][DOME] {_what}")
                _note(_what[0].upper() + _what[1:] + ".")
        except Exception as e:
            log(f"[S32][DOME] watcher error: {e}")
        time.sleep(15)
    _dome["watcher"] = False


def cleanup_dome_rules_on_boot():
    """Fail-open safety: wipe any leftover dome rules so every boot starts fully
    open. If the dome ever cuts something you need, a reboot always restores you."""
    try:
        _dome["armed"] = False
        state["dome_armed"] = False
        left = _existing_dome_rules()
        if left:
            if _is_admin():
                _fw_remove_all_dome_rules()
                left = _existing_dome_rules()
            if left:
                # Without admin the cleanup can't remove them — and firewall rules survive a
                # reboot, so they'd stay forever unnoticed. Say it, loudly.
                msg = (f"⚠ {len(left)} Iron Dome firewall rule(s) from an earlier session are still "
                       f"active. Start System 32 as administrator once to clear them.")
                log(f"[S32][DOME] {msg}")
                state["startup_notification"] = msg
            else:
                log("[S32][DOME] leftover dome rules from an earlier session cleared")
    except Exception:
        pass


# ── Airplane mode backstop (Windows Firewall) ────────────────────────────────
#  Airplane mode is enforced inside System 32 (s32_config's socket guard). ComfyUI and
#  llama-server are separate programs, so with admin rights they — and System 32's own
#  Python — are also blocked from the INTERNET at the firewall. Windows Firewall doesn't
#  filter connections inside this computer, so they still talk to each other.
#  A venv's python.exe is only a launcher: ComfyUI really runs in the base Python it
#  points to (pyvenv.cfg), so that one is blocked too — which, while airplane mode is on,
#  also cuts that Python for anything else using it.
_AIR_PREFIX = "S32_Airplane_"


def _venv_base(py):
    try:
        cfg = os.path.join(os.path.dirname(os.path.dirname(py)), "pyvenv.cfg")
        with open(cfg, encoding="utf-8") as f:
            for ln in f:
                k, _, v = ln.partition("=")
                if k.strip().lower() == "executable" and v.strip():
                    return v.strip()
                if k.strip().lower() == "home" and v.strip():
                    cand = os.path.join(v.strip(), "python.exe")
                    if os.path.exists(cand):
                        return cand
    except Exception:
        pass
    return None


def airplane_programs():
    import sys
    progs = [sys.executable]
    py = CONFIG.get("comfy_image_python") or ""
    if py:
        progs.append(py)
        b = _venv_base(py)
        if b:
            progs.append(b)
    try:
        from s32_hardware import _find_llama_server
        ls = _find_llama_server()
        if ls:
            progs.append(ls)
    except Exception:
        pass
    seen, out = set(), []
    for p in progs:
        k = _wpath(p)
        if p and k not in seen:
            seen.add(k); out.append(p)
    return out


def _air_rule(exe):
    import hashlib, ntpath
    return f"{_AIR_PREFIX}{ntpath.basename(exe)}_{hashlib.sha1(_wpath(exe).encode()).hexdigest()[:8]}"


def _existing_air_rules():
    r = _run_hidden(["powershell", "-NoProfile", "-Command",
                     f"Get-NetFirewallRule -DisplayName '{_AIR_PREFIX}*' -ErrorAction SilentlyContinue "
                     f"| ForEach-Object {{ $_.DisplayName }}"])
    if getattr(r, "returncode", 1) != 0:
        return None
    return {ln.strip() for ln in (getattr(r, "stdout", "") or "").splitlines() if ln.strip()}


def airplane_firewall(on):
    """Make the firewall backstop match airplane mode. → {ok, programs, needs_admin, left}."""
    have = _existing_air_rules() or set()
    if not _is_admin():
        stuck = sorted(have) if not on else []
        return {"ok": False, "needs_admin": True, "programs": [], "left": stuck}
    if on:
        done = []
        for exe in airplane_programs():
            rn = _air_rule(exe)
            if rn not in have:
                r = _run_hidden(["netsh", "advfirewall", "firewall", "add", "rule", f"name={rn}",
                                 "dir=out", "action=block", f"program={exe}", "enable=yes"])
                if getattr(r, "returncode", 1) != 0:
                    continue
            done.append(exe)
        log(f"[S32][AIRPLANE] firewall backstop on: {', '.join(os.path.basename(x.replace(chr(92), '/')) for x in done)}")
        return {"ok": True, "programs": done}
    _run_hidden(["powershell", "-NoProfile", "-Command",
                 f"Remove-NetFirewallRule -DisplayName '{_AIR_PREFIX}*' -ErrorAction SilentlyContinue"])
    left = sorted(_existing_air_rules() or [])
    if left:
        log(f"[S32][AIRPLANE] ⚠ {len(left)} airplane firewall rule(s) could not be removed")
    return {"ok": not left, "programs": [], "left": left}


# ── Status for the UI ────────────────────────────────────────────────────────
def iron_dome_status():
    cls = classify()
    ports_now, _procs_now = sample_now()
    port_list = sorted(
        [{"port": p, "proc": proc,
          "necessary": any(e["sig"] == f"port:{p}" for e in cls["necessary"]),
          "whitelisted": _is_whitelisted_port(p) or _is_whitelisted_proc(proc),
          "protected": _is_protected_port(p) or _is_protected_proc(proc)}
         for p, proc in ports_now.items()],
        key=lambda x: x["port"])
    return {
        "samples": cls["samples"],
        "ready": cls["samples"] >= _MIN_SAMPLES(),
        "min_samples": _MIN_SAMPLES(),
        "counts": {"necessary": len(cls["necessary"]),
                   "unnecessary": len(cls["unnecessary"]),
                   "new": len(cls["new"])},
        "whitelist": get_whitelist(),
        "ports": port_list,
        "armed": bool(state.get("dome_armed", False)),  # set by the arm action
        "mode": _dome.get("mode", "block"),
        "admin": _is_admin(),
        "airplane": bool(__import__("s32_config").airplane()),
        "hit": _dome.get("hit") or {"ports": [], "procs": [], "killed": []},
    }


# ── Background sampler (learns over iterations) ──────────────────────────────
def _sampler_loop():
    # one prompt sample so there's something immediately, then on cadence
    try:
        record_sample()
    except Exception as e:
        log(f"[S32][DOME] first sample failed: {e}")
    while True:
        time.sleep(max(20, _SAMPLE_SECS()))
        try:
            record_sample()
        except Exception as e:
            log(f"[S32][DOME] sample failed: {e}")


def start():
    cleanup_dome_rules_on_boot()          # fail-open: every boot starts fully open
    t = threading.Thread(target=_sampler_loop, daemon=True, name="iron-dome-sampler")
    t.start()
    log(f"[S32][DOME] baselining started (every {_SAMPLE_SECS()}s, "
        f"{_baseline.get('samples', 0)} samples carried over)")
    return t
