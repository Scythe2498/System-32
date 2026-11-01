# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_firewall.py — Intelligent network guardian.

Tier 1 (autonomous, read-only): continuously watches listening ports and active
connections via psutil, building a baseline of what's normal on this machine.

Tier 2 (autonomous, NON-destructive): when something NEW and genuinely suspicious
appears, it raises an alert — a chat note, spoken aloud, and a visual banner —
explaining what it saw and why. It NEVER kills or blocks anything on its own.

It learns: anything the operator approves goes on a persistent allowlist and is
never flagged again. Over time it learns this machine's normal.

Tier 3 (operator-gated action): it can PROPOSE killing or blocking something,
but it can never execute it itself — the only code path that actually kills or
blocks is a human pressing Confirm. The PROTECTED set is enforced at both propose
and execute time, so its own stack and critical Windows processes can never be
terminated, no matter who asks (model, button, or operator).

A hard PROTECTED set — its own stack (llama-server, ComfyUI, the web UI) plus
critical Windows processes — is never flagged and never killable, even if asked.
"""
import os, json, time, threading, subprocess

try:
    from s32_config import (CONFIG, REFERENCE_DIR, state, log,
                            _result_lock, _push_result, _push_done)
except Exception:                                    # keep importable in isolation
    CONFIG, REFERENCE_DIR, state = {}, "", {}
    _result_lock = threading.Lock()
    def log(*a):  print(*a)
    def _push_result(*a, **k): pass
    def _push_done(*a, **k):   pass

_state = {
    "baseline": set(),       # connection signatures already seen (no alert)
    "seeded":   False,
    "alerts":   [],          # active, undismissed alerts
    "info_log": [],          # quiet record of low-level events
    "limited":  False,       # True if psutil visibility is privilege-limited
    "lock":     threading.Lock(),
    "poll_interval": 25,
}

# ── PROTECTED SET — never flagged, never (in any future tier) killed ──────────
_PROTECTED_PORTS = set()                              # its stack ports, filled at start()
_OWN_NAME_HINTS  = ("llama-server", "llama_server")   # its model server
_OWN_EXE_HINTS   = ("system 32", "sys 32", "comfyui", "\\comfyui\\", "system32.py")
# Critical Windows processes — killing these can bluescreen or destabilise Windows.
_CRITICAL_PROC = {
    "system", "registry", "memory compression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "svchost.exe",
    "fontdrvhost.exe", "dwm.exe", "spoolsv.exe", "taskhostw.exe", "ctfmon.exe",
    "audiodg.exe", "conhost.exe", "searchindexer.exe", "searchhost.exe",
    "runtimebroker.exe", "sihost.exe", "dllhost.exe", "wudfhost.exe",
}

# ── Suspicion heuristics ─────────────────────────────────────────────────────
_SUSPICIOUS_DIR_HINTS = ("\\temp\\", "\\tmp\\", "\\appdata\\local\\temp\\",
                         "\\downloads\\", "\\$recycle.bin", "\\windows\\temp\\",
                         "\\programdata\\temp\\", "\\public\\")
# Ports commonly used by remote-access tools, backdoors, and C2 frameworks.
_RISKY_PORTS = {1337, 1604, 2745, 3127, 4444, 4445, 5554, 6666, 6667, 6697,
                9999, 12345, 12346, 20034, 27374, 31337, 31338, 54321, 65000}


def _allowlist_path():
    return os.path.join(REFERENCE_DIR, "firewall_allowlist.json") if REFERENCE_DIR else None


def _load_allowlist():
    p = _allowlist_path()
    if p and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": []}


def _save_allowlist(data):
    p = _allowlist_path()
    if not p:
        return
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[S32][FW] allowlist save failed: {e}")


def _short(path, n=48):
    if not path:
        return ""
    return path if len(path) <= n else "…" + path[-(n - 1):]


def _is_private(ip):
    if not ip:
        return True
    if ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
        return True
    return (ip.startswith("10.") or ip.startswith("192.168.") or
            ip.startswith("169.254.") or ip.startswith("fe80:") or
            any(ip.startswith(f"172.{b}.") for b in range(16, 32)))


def _sig(c):
    return f"{(c.get('name') or '?').lower()}|{c.get('lport', 0)}|{c.get('status')}|{c.get('rip', '')}"


# ── Scanning (psutil) ────────────────────────────────────────────────────────
def _proc_name_exe(pid):
    name, exe = "?", ""
    if pid:
        try:
            import psutil as _ps
            pr = _ps.Process(pid)
            name = pr.name()
            try:
                exe = pr.exe() or ""
            except Exception:
                exe = ""
        except Exception:
            pass
    return name, exe


def _conn_dict(c, name, exe, pid=None):
    if pid is None:
        pid = getattr(c, "pid", None)        # sconn has .pid; pconn does not
    return {
        "pid":    pid,
        "name":   name,
        "exe":    exe,
        "lport":  c.laddr.port if c.laddr else 0,
        "laddr":  (f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else ""),
        "raddr":  (f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else ""),
        "rip":    (c.raddr.ip if c.raddr else ""),
        "rport":  (c.raddr.port if c.raddr else 0),
        "status": c.status,
    }


def _proc_conns(p):
    """psutil renamed Process.connections() -> net_connections(); support both."""
    fn = getattr(p, "net_connections", None) or getattr(p, "connections", None)
    if not fn:
        return []
    try:
        return fn(kind="inet")
    except Exception:
        return []


def _scan():
    """Return a list of connection dicts (listeners + established).

    psutil is OPTIONAL here — on some setups it isn't importable in the runtime
    at all (separate interpreter/venv). So we try psutil if present, but always
    fall back to `netstat -ano` (with `tasklist` for names) when it yields
    nothing. That path needs neither psutil nor admin."""
    out, seen = [], set()

    def _add(c, name, exe, pid=None):
        try:
            if c.status not in ("LISTEN", "ESTABLISHED"):
                return
            d = _conn_dict(c, name, exe, pid)
            key = (d["pid"], d["lport"], d["raddr"], d["status"])
            if key in seen:
                return
            seen.add(key)
            out.append(d)
        except Exception:
            pass

    psutil = None
    try:
        import psutil as _ps
        psutil = _ps
    except Exception:
        psutil = None

    glob_n = proc_n = 0
    if psutil is not None:
        # 1) Global table — fullest view, but may need admin.
        try:
            for c in psutil.net_connections(kind="inet"):
                glob_n += 1
                name, exe = _proc_name_exe(c.pid)
                _add(c, name, exe)
        except Exception:
            pass
        # 2) Per-process — the user's own processes, without admin.
        try:
            for p in psutil.process_iter(["pid", "name", "exe"]):
                pconns = _proc_conns(p)
                if not pconns:
                    continue
                name = p.info.get("name") or "?"
                exe  = p.info.get("exe") or ""
                ppid = p.info.get("pid")
                for c in pconns:
                    proc_n += 1
                    _add(c, name, exe, pid=ppid)
        except Exception as e:
            log(f"[S32][FW] per-process scan error: {e}")

    if glob_n == 0:
        _state["limited"] = True

    # 3) netstat fallback — runs whenever psutil gave nothing OR isn't available.
    netstat_n = 0
    if not out:
        net = _scan_netstat()
        netstat_n = len(net)
        for d in net:
            key = (d["pid"], d["lport"], d["raddr"], d["status"])
            if key not in seen:
                seen.add(key)
                out.append(d)
        if net:
            _state["limited"] = True

    if not _state.get("diag_logged"):
        _state["diag_logged"] = True
        log(f"[S32][FW] scan sources: psutil={'yes' if psutil else 'MISSING'} "
            f"global={glob_n} per-process={proc_n} netstat={netstat_n} usable={len(out)}")
    return out


def _split_addr(a):
    """Parse 'ip:port', '[::1]:port', '0.0.0.0:0', '*:*' -> (ip, port)."""
    try:
        a = a.strip()
        if a.startswith("["):                          # IPv6 [..]:port
            ip = a[1:a.index("]")]
            port = int(a[a.index("]") + 2:])
        else:
            ip, _sep, p = a.rpartition(":")
            port = int(p) if p.isdigit() else 0
        return ip, port
    except Exception:
        return "", 0


def _tasklist_names():
    """pid -> image name via `tasklist` (no psutil, no admin needed)."""
    m = {}
    try:
        import csv, io
        r = subprocess.run(["tasklist", "/fo", "csv", "/nh"], capture_output=True,
                           text=True, timeout=15, creationflags=_NO_WINDOW)
        for row in csv.reader(io.StringIO(r.stdout or "")):
            if len(row) >= 2:
                try:
                    m[int(row[1])] = row[0]
                except Exception:
                    continue
    except Exception:
        pass
    return m


def _scan_netstat():
    """Parse `netstat -ano` (no admin needed) into connection dicts."""
    out = []
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                           timeout=15, creationflags=_NO_WINDOW)
    except Exception as e:
        log(f"[S32][FW] netstat failed: {e}")
        return out

    try:
        import psutil as _ps
    except Exception:
        _ps = None
    tl = {} if _ps else _tasklist_names()      # name map when psutil is missing
    name_cache = {}

    def _name(pid):
        if pid in name_cache:
            return name_cache[pid]
        n, e = "?", ""
        if _ps is not None:
            try:
                pr = _ps.Process(pid)
                n = pr.name()
                try:
                    e = pr.exe() or ""
                except Exception:
                    e = ""
            except Exception:
                pass
        else:
            n = tl.get(pid, "?")
        name_cache[pid] = (n, e)
        return n, e

    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() not in ("TCP", "UDP"):
            continue
        proto = parts[0].upper()
        try:
            if proto == "TCP" and len(parts) >= 5:
                local, foreign, state, pid = parts[1], parts[2], parts[3], parts[4]
            elif proto == "UDP" and len(parts) >= 4:
                local, foreign, state, pid = parts[1], parts[2], "", parts[-1]
            else:
                continue
            pid = int(pid)
        except Exception:
            continue
        st = {"LISTENING": "LISTEN", "ESTABLISHED": "ESTABLISHED"}.get(state.upper())
        if not st:
            continue                                    # ignore TIME_WAIT, UDP, etc.
        lip, lport = _split_addr(local)
        fip, fport = _split_addr(foreign)
        name, exe = _name(pid)
        out.append({
            "pid": pid, "name": name, "exe": exe,
            "lport": lport, "laddr": local,
            "raddr": (foreign if st == "ESTABLISHED" else ""),
            "rip": fip, "rport": fport, "status": st,
        })
    return out


# ── Protection + allowlist matching ──────────────────────────────────────────
def _is_protected(c):
    name = (c.get("name") or "").lower()
    exe  = (c.get("exe") or "").lower()
    if c.get("lport") in _PROTECTED_PORTS:
        return True
    if name in _CRITICAL_PROC:
        return True
    if any(h in name for h in _OWN_NAME_HINTS):
        return True
    if any(h in exe for h in _OWN_EXE_HINTS):
        return True
    return False


def _allow_match(c, allow):
    name = (c.get("name") or "").lower()
    for e in allow.get("entries", []):
        if e.get("name") and e["name"] == name and e.get("port") in (None, c.get("lport")):
            return True
    return False


# ── Triage ───────────────────────────────────────────────────────────────────
def _triage(c):
    """Return (level, reason). level is 'high' (alert), 'info' (quiet), or None."""
    exe  = (c.get("exe") or "").lower()
    name = c.get("name") or "?"
    port = c.get("lport") or 0
    in_susp_dir = any(h in exe for h in _SUSPICIOUS_DIR_HINTS)
    risky_port  = port in _RISKY_PORTS

    if c.get("status") == "LISTEN":
        if in_susp_dir:
            return ("high", f"{name} is listening on port {port} and runs from a suspicious "
                            f"location ({_short(c.get('exe'))}).")
        if risky_port:
            return ("high", f"{name} is listening on port {port} — a port often used by "
                            f"remote-access or backdoor tools.")
        return ("info", f"{name} started listening on port {port}.")

    # ESTABLISHED — an active connection. The LOCAL port here is a random
    # ephemeral number (meaningless), so judge by where it's connecting TO:
    # the remote IP and remote port. Ignore private/LAN destinations entirely.
    rip   = c.get("rip", "")
    rport = c.get("rport", 0)
    if rip and not _is_private(rip):
        if in_susp_dir:
            return ("high", f"{name} (from {_short(c.get('exe'))}) is connecting out to {rip}.")
        if rport in _RISKY_PORTS:
            return ("high", f"{name} is connecting out to {rip} on port {rport} — a port "
                            f"associated with remote-access or backdoor tools.")
    return (None, None)


# ── Alerting ─────────────────────────────────────────────────────────────────
def _raise_alert(c, level, reason):
    alert = {
        "id":     f"{(c.get('name') or '?')}:{c.get('lport', 0)}:{int(time.time())}",
        "level":  level,
        "reason": reason,
        "name":   c.get("name"),
        "pid":    c.get("pid"),
        "exe":    c.get("exe"),
        "port":   c.get("lport"),
        "raddr":  c.get("raddr"),
        "status": c.get("status"),
        "ts":     time.strftime("%H:%M:%S"),
        "sig":    _sig(c),
    }
    with _state["lock"]:
        _state["alerts"].append(alert)
        if len(_state["alerts"]) > 20:
            _state["alerts"] = _state["alerts"][-20:]
    # 1) visual banner (surfaced via /status payload, polled every 2s)
    state["security_alert"] = {
        "level": level, "reason": reason, "name": c.get("name"),
        "port": c.get("lport"), "id": alert["id"],
    }
    # 2) chat note
    try:
        import s32_config as _cfg
        with _result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        path_line = f"\nPath: {c.get('exe')}" if c.get("exe") else ""
        _push_result({"content": f"⚠ Security — {reason}\n"
                                 f"Process: {c.get('name')} (pid {c.get('pid')})"
                                 f"{path_line}\n\n"
                                 f"I haven't touched it — I never act on my own. Tell me to "
                                 f"approve it (mark safe so I stop flagging it) or to look closer.",
                      "path": "⬡ Security"}, jid)
        _push_done(jid)
    except Exception as e:
        log(f"[S32][FW] alert push failed: {e}")
    # 3) speak it (priority, so it interrupts)
    try:
        import voice as _voice
        _voice.speak(f"Heads up. {reason}", priority=True)
    except Exception:
        pass
    log(f"[S32][FW] ALERT [{level}] {reason}")


def _record_info(c, reason):
    with _state["lock"]:
        _state["info_log"].append({"ts": time.strftime("%H:%M:%S"), "reason": reason,
                                   "name": c.get("name"), "port": c.get("lport")})
        if len(_state["info_log"]) > 50:
            _state["info_log"] = _state["info_log"][-50:]


# ── Public actions (operator-driven; non-destructive) ────────────────────────
def approve(name=None, port=None, alert_id=None):
    """Mark a process/port as trusted so the guardian stops flagging it."""
    data = _load_allowlist()
    entry = {"name": (name or "").lower() or None, "port": port}
    if entry["name"] and entry not in data["entries"]:
        data["entries"].append(entry)
        _save_allowlist(data)
    dismiss(alert_id=alert_id, name=name)
    log(f"[S32][FW] Approved (trusted): {entry}")
    return {"ok": True, "approved": entry}


def dismiss(alert_id=None, name=None):
    """Clear an active alert / the banner without trusting it permanently."""
    with _state["lock"]:
        if alert_id:
            _state["alerts"] = [a for a in _state["alerts"] if a["id"] != alert_id]
        elif name:
            _state["alerts"] = [a for a in _state["alerts"]
                                if (a.get("name") or "").lower() != name.lower()]
        else:
            _state["alerts"] = []
        state["security_alert"] = _state["alerts"][-1] if _state["alerts"] else None
    return {"ok": True}


def scan_network():
    """Read-only snapshot of listeners + active connections (for the LLM/UI)."""
    conns = _scan()
    allow = _load_allowlist()

    def _mark(c):
        d = dict(c)
        d["protected"] = _is_protected(c)
        d["trusted"]   = _allow_match(c, allow)
        lvl, why = _triage(c)
        d["flag"] = lvl
        d["why"]  = why
        return d

    listeners   = [_mark(c) for c in conns if c["status"] == "LISTEN"]
    established = [_mark(c) for c in conns if c["status"] == "ESTABLISHED"]
    flagged = [c for c in (listeners + established) if c["flag"] == "high"]
    return {
        "listening":   listeners,
        "connections": established,
        "flagged":     flagged,
        "counts": {"listening": len(listeners), "connections": len(established),
                   "flagged": len(flagged)},
        "limited": _state["limited"],
        "note": ("Visibility is limited without administrator rights — some process "
                 "names may be unknown." if _state["limited"] else ""),
    }


def get_alerts():
    with _state["lock"]:
        return {"alerts": list(_state["alerts"]), "info": list(_state["info_log"][-10:])}


# ── Tier 3: operator-gated actions (propose → human Confirm → execute) ────────
# Nothing in here executes on its own. propose_action() only STAGES; the single
# code path that kills/blocks is confirm_action(), reachable only by the Confirm
# button (a human). The PROTECTED set is checked at BOTH stages.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CRITICAL_PIDS = {0, 4}                               # System Idle Process, System


def _is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _protection_check(pid=None, name=None, exe=None, port=None):
    """THE SAFETY FLOOR. Returns (blocked, reason). Anything in the protected set
    is refused no matter who asks — model, button, or operator."""
    n = (name or "").lower()
    e = (exe or "").lower()
    try:
        pid_i = int(pid) if pid is not None else None
    except Exception:
        pid_i = None
    if pid_i in _CRITICAL_PIDS:
        return (True, f"PID {pid} is a core Windows process and can never be terminated.")
    if n in _CRITICAL_PROC:
        return (True, f"{name} is a critical Windows process — ending it could crash the machine. I won't.")
    if any(h in n for h in _OWN_NAME_HINTS) or any(h in e for h in _OWN_EXE_HINTS):
        return (True, "That's part of my own stack — killing it would take me offline. I won't.")
    if port and port in _PROTECTED_PORTS:
        return (True, f"Port {port} belongs to my own stack (model / ComfyUI / UI). I won't touch it.")
    return (False, "")


def _resolve_target(pid=None, name=None, port=None):
    """Resolve an action target to concrete {pid,name,exe,port}. Works without
    psutil by resolving against the scan (netstat-backed)."""
    psutil = None
    try:
        import psutil as _ps
        psutil = _ps
    except Exception:
        psutil = None
    if pid:
        if psutil is not None:
            try:
                pr = psutil.Process(int(pid))
                try:
                    exe = pr.exe() or ""
                except Exception:
                    exe = ""
                return {"found": True, "pid": int(pid), "name": pr.name(), "exe": exe, "port": port}
            except Exception:
                pass
        # No psutil (or lookup failed): find the pid in the scan.
        for c in _scan():
            if c["pid"] == int(pid):
                return {"found": True, "pid": int(pid), "name": c["name"], "exe": c["exe"], "port": c["lport"]}
        return {"found": False, "reason": f"No process is running with PID {pid}."}
    for c in _scan():
        if name and (c["name"] or "").lower() == str(name).lower():
            return {"found": True, "pid": c["pid"], "name": c["name"], "exe": c["exe"], "port": c["lport"]}
        if port and c["lport"] == port:
            return {"found": True, "pid": c["pid"], "name": c["name"], "exe": c["exe"], "port": c["lport"]}
    return {"found": False, "reason": "Couldn't find a matching process or connection."}
    return {"found": False, "reason": "Couldn't find a matching process or connection."}


def _push_note(text, header="⬡ Security"):
    try:
        import s32_config as _cfg
        with _result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _push_result({"content": text, "path": header}, jid)
        _push_done(jid)
    except Exception:
        pass


def _clear_pending():
    state["pending_action"] = None
    with _state["lock"]:
        _state["pending"] = None


def propose_action(action, pid=None, name=None, port=None, reason=""):
    """STAGE a destructive action for the operator to confirm. Never executes.
    Refuses outright if the target is protected."""
    if action not in ("kill", "block"):
        return {"error": f"Unknown action '{action}'. Use 'kill' or 'block'."}
    tgt = _resolve_target(pid, name, port)
    if not tgt.get("found"):
        return {"error": tgt.get("reason", "Target not found.")}
    blocked, why = _protection_check(tgt["pid"], tgt["name"], tgt["exe"], tgt.get("port"))
    if blocked:
        _push_note(f"⛔ I won't {action} {tgt['name']}. {why}")
        return {"refused": True, "reason": why, "target": tgt}
    needs_admin = (action == "block" and not _is_admin())
    pending = {
        "token":  f"{action}-{tgt['pid']}-{int(time.time())}",
        "action": action, "pid": tgt["pid"], "name": tgt["name"],
        "exe": tgt["exe"], "port": tgt.get("port"),
        "reason": reason, "needs_admin": needs_admin,
        "ts": time.strftime("%H:%M:%S"),
    }
    state["pending_action"] = pending
    with _state["lock"]:
        _state["pending"] = pending
    admin_note = (" (heads up: this needs admin rights, which I don't have right now — "
                  "it may not go through)") if needs_admin else ""
    _push_note(f"⚠ Ready to {action} {tgt['name']} (pid {tgt['pid']}){admin_note}. "
               f"Nothing happens until you press Confirm. Say 'cancel' or hit Cancel to call it off.")
    return {"ok": True, "staged": pending}


def confirm_action(token=None):
    """The ONLY path that executes. Reachable only by a human pressing Confirm."""
    pending = state.get("pending_action")
    if not pending:
        return {"error": "There's nothing staged to confirm."}
    if token and token != pending.get("token"):
        return {"error": "That confirmation no longer matches what's staged."}
    # Re-check the floor at execute time (belt and braces).
    blocked, why = _protection_check(pending["pid"], pending["name"], pending["exe"], pending.get("port"))
    if blocked:
        _clear_pending()
        _push_note(f"⛔ Stopped — {why}")
        return {"refused": True, "reason": why}
    action = pending["action"]
    res = _do_kill(pending["pid"], pending["name"]) if action == "kill" else _do_block(pending)
    _clear_pending()
    if res.get("ok"):
        _push_note(f"✓ {action.capitalize()} done: {pending['name']} (pid {pending['pid']}).")
    elif res.get("needs_admin"):
        _push_note(f"⚠ Couldn't {action} {pending['name']}: {res.get('reason')}")
    else:
        _push_note(f"⚠ Couldn't {action} {pending['name']}: {res.get('error', 'unknown error')}")
    return res


def cancel_action(token=None):
    _clear_pending()
    return {"ok": True, "cancelled": True}


def _do_kill(pid, name=""):
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            log(f"[S32][FW] Terminated {name} (pid {pid}) on operator confirmation.")
            return {"ok": True, "killed": name, "pid": pid}
        blob = ((r.stdout or "") + (r.stderr or "")).lower()
        if "access is denied" in blob or "denied" in blob:
            return {"needs_admin": True,
                    "reason": f"ending {name} needs administrator rights. Restart System 32 as admin."}
        return {"error": ((r.stdout or "") + (r.stderr or "")).strip()[:200] or "taskkill failed"}
    except Exception as e:
        return {"error": str(e)}


def _do_block(pending):
    if not _is_admin():
        return {"needs_admin": True,
                "reason": "blocking via the Windows Firewall needs administrator rights. Restart System 32 as admin."}
    name = pending.get("name") or "process"
    exe  = pending.get("exe") or ""
    port = pending.get("port")
    rule = f"System32 block {name} {int(time.time())}"
    try:
        if exe:
            args = ["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}",
                    "dir=out", "action=block", f"program={exe}", "enable=yes"]
        elif port:
            args = ["netsh", "advfirewall", "firewall", "add", "rule", f"name={rule}",
                    "dir=in", "action=block", "protocol=TCP", f"localport={port}"]
        else:
            return {"error": "Nothing to block — no program path or port."}
        r = subprocess.run(args, capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW)
        if r.returncode == 0:
            log(f"[S32][FW] Firewall rule added: {rule}")
            return {"ok": True, "blocked": name, "rule": rule}
        return {"error": ((r.stdout or "") + (r.stderr or "")).strip()[:200] or "netsh failed"}
    except Exception as e:
        return {"error": str(e)}


# ── Monitor loop ─────────────────────────────────────────────────────────────
def _loop():
    while True:
        time.sleep(_state["poll_interval"])
        try:
            conns = _scan()
            allow = _load_allowlist()
            for c in conns:
                sig = _sig(c)
                if sig in _state["baseline"]:
                    continue
                if not _state["seeded"]:
                    _state["baseline"].add(sig)
                    continue
                _state["baseline"].add(sig)         # remember so we don't re-alert
                if _is_protected(c) or _allow_match(c, allow):
                    continue
                level, reason = _triage(c)
                if level == "high":
                    _raise_alert(c, level, reason)
                elif level == "info":
                    _record_info(c, reason)
            if not _state["seeded"]:
                _state["seeded"] = True
                log(f"[S32] Network guardian baseline set "
                    f"({len(_state['baseline'])} connections){' [limited visibility]' if _state['limited'] else ''}.")
            if len(_state["baseline"]) > 4000:
                _state["baseline"] = set(list(_state["baseline"])[-2000:])
        except Exception as e:
            log(f"[S32][FW] loop error: {e}")


def _port_of(host):
    try:
        return int(str(host).split(":")[-1])
    except Exception:
        return None


def start():
    """Start the read-only network guardian (Tier 1 monitor + Tier 2 alerts)."""
    for key in ("lmstudio_host", "comfy_image_host", "comfy_video_host"):
        p = _port_of(CONFIG.get(key, ""))
        if p:
            _PROTECTED_PORTS.add(p)
    wp = CONFIG.get("web_port")
    if isinstance(wp, int):
        _PROTECTED_PORTS.add(wp)
    _PROTECTED_PORTS.update({1234, 7432, 8188})       # its stack, belt & braces
    threading.Thread(target=_loop, daemon=True, name="fw-loop").start()
    log("[S32] Network guardian started — watching ports & connections "
        "(read-only; alerts only, never acts on its own).")
