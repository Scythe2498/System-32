# ─────────────────────────────────────────────────────────────────────
#  System 32
#  Authored by SoullessScythe
#  Built over many late nights, one iteration at a time.
# ─────────────────────────────────────────────────────────────────────

"""
s32_unreal.py — Unreal Engine detection + launch.

Sibling of s32_comfyui.py: a heavy external tool System 32 does NOT bundle. Unreal
lives wherever the user already installed it (their fast internal SSD, where a 40GB+
editor belongs); System 32 stays portable and just DETECTS it and reaches out — the
same reflex it uses for ComfyUI. If Unreal isn't present, the capability simply
doesn't exist on this machine (the launch button never shows). No error, no clutter.

Two jobs here (the launch layer; the PILOTING layer — Remote Control tools it calls
to build worlds — is a later module):
  • detect_unreal()  — is a usable Unreal install present, which version, does it
                        match our pinned target.
  • launch_unreal()  — start the editor (optionally the world-building project) in
                        PC (windowed) or VR (OpenXR headset) mode.

── PINNED VERSION ────────────────────────────────────────────────────────────────
We target ONE known-good UE version this integration is validated against — the
b9500-style pin discipline, applied by DETECTION rather than bundling (we can't force
the user's install, but we can detect it and be honest about mismatches). Currently
the **5.8** line (the operator runs 5.8.2; Epic tracks the install as "5.8" and the
.x is an in-place hotfix, so "5.8" is the right, detectable pin and it naturally
covers 5.8.2). 5.8 is also the FINAL UE5 major release — feature-frozen, "most stable
to date" — so it's an unusually solid thing to pin against; nothing shifts under us.

TO CHANGE THE PIN LATER: edit `_DEFAULT_PINNED` below, OR set "unreal_pinned_version"
in config.json (config wins). One spot. That's the whole knob.
"""

import os
import json
import subprocess
import urllib.request
import urllib.error

from s32_config import CONFIG, log

# The single easy-to-change knob. config.json "unreal_pinned_version" overrides it.
_DEFAULT_PINNED = "5.8"


def _pinned_version():
    return str(CONFIG.get("unreal_pinned_version", _DEFAULT_PINNED)).strip()


def _editor_exe(engine_root):
    """Return the path to the editor executable under an engine install root, or None."""
    if not engine_root:
        return None
    for rel in (os.path.join("Engine", "Binaries", "Win64", "UnrealEditor.exe"),
                os.path.join("Engine", "Binaries", "Win64", "UE4Editor.exe")):
        p = os.path.join(engine_root, rel)
        if os.path.isfile(p):
            return p
    return None


def _launcher_records():
    """Read the Epic Games Launcher install manifest — the authoritative list of
    installed engine versions and their paths. Returns [{version, path}]. Empty on
    any failure (non-Windows, no Epic, unreadable)."""
    out = []
    pdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    candidates = [
        os.path.join(pdata, "Epic", "UnrealEngineLauncher", "LauncherInstalled.dat"),
        r"C:\ProgramData\Epic\UnrealEngineLauncher\LauncherInstalled.dat",
    ]
    for p in candidates:
        try:
            if not os.path.isfile(p):
                continue
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            for item in data.get("InstallationList", []) or []:
                app = str(item.get("AppName", ""))
                loc = str(item.get("InstallLocation", ""))
                # Engine installs are named like "UE_5.8"; skip plugins/marketplace items.
                if app.startswith("UE_") and loc:
                    out.append({"version": app[3:], "path": loc})
            if out:
                break
        except Exception:
            continue
    return out


def _scan_standard_paths():
    """Fallback: scan Epic's default install dir for UE_<version> folders."""
    out = []
    seen = set()
    for base in (r"C:\Program Files\Epic Games",
                 os.path.join(os.environ.get("PROGRAMFILES", r"C:\Program Files"), "Epic Games")):
        if base in seen or not os.path.isdir(base):
            continue
        seen.add(base)
        try:
            for name in os.listdir(base):
                if name.startswith("UE_") and _editor_exe(os.path.join(base, name)):
                    out.append({"version": name[3:], "path": os.path.join(base, name)})
        except Exception:
            pass
    return out


def _matches_pin(version, pinned):
    """True if an installed version satisfies the pin. '5.8' matches '5.8' and '5.8.2'."""
    if not version:
        return False
    return version == pinned or version.startswith(pinned + ".")


def detect_unreal():
    """Detect a usable Unreal install on this machine. Never raises.

    Returns:
      installed     bool  — a launchable editor was found
      version       str   — the version we'll launch (prefers the pinned line)
      editor_path   str   — full path to the editor exe
      matches_pin   bool  — is `version` on our pinned line
      pinned        str   — the pinned target (e.g. '5.8')
      all_versions  list  — every installed engine version found
      project       str   — configured world-building project (may be empty pre-Phase-0)

    Detection order: config override → Epic launcher manifest → default-path scan.
    """
    pinned = _pinned_version()
    res = {"installed": False, "version": None, "editor_path": None,
           "matches_pin": False, "pinned": pinned, "all_versions": [],
           "project": CONFIG.get("unreal_project", "")}
    try:
        found = {}  # version -> engine_root (dedup)

        # 1. Explicit operator override (escape hatch, like model paths).
        override = CONFIG.get("unreal_path", "")
        if override and _editor_exe(override):
            found[str(CONFIG.get("unreal_version_label", "custom"))] = override
        # 2. Epic launcher manifest (authoritative).
        for rec in _launcher_records():
            if _editor_exe(rec["path"]):
                found.setdefault(rec["version"], rec["path"])
        # 3. Default-path scan (fallback).
        for rec in _scan_standard_paths():
            found.setdefault(rec["version"], rec["path"])

        if not found:
            return res

        res["all_versions"] = sorted(found.keys())
        # Prefer the pinned line if installed; otherwise the highest version present
        # (so the button still works — we just flag the mismatch to the operator).
        chosen = next((v for v in found if _matches_pin(v, pinned)), None)
        if chosen is None:
            chosen = sorted(found.keys())[-1]

        res["installed"]   = True
        res["version"]     = chosen
        res["editor_path"] = _editor_exe(found[chosen])
        res["matches_pin"] = _matches_pin(chosen, pinned)
        return res
    except Exception as e:
        log(f"[S32][UE] detect failed: {str(e)[:100]}")
        return res


def launch_unreal(mode="pc"):
    """Start the Unreal editor (optionally the configured world-building project) in
    PC (windowed) or VR (OpenXR headset) mode. Non-blocking. Never raises.

    Windowed vs VR is a launch flag on ONE project, not two builds — same world, same
    assets; only the camera/input surface differs.
    """
    d = detect_unreal()
    if not d["installed"] or not d["editor_path"]:
        return {"ok": False, "msg": "Unreal Engine isn't detected on this machine."}

    cmd = [d["editor_path"]]
    project = CONFIG.get("unreal_project", "")
    setup_note = ""
    try:
        # No project configured → use the one the user opened last, and remember it.
        # Then switch on everything it needs BEFORE the editor starts (plugins and
        # Remote Control settings only load at startup) — the user never has to know.
        found, _how = find_project() if not (project and os.path.isfile(project)) else (project, "configured")
        if found:
            project = found
            if check_setup(found):
                changed = apply_setup(found)
                if changed:
                    setup_note = " (switched on what System 32 needs: " + "; ".join(changed) + ")"
            remember_project(found)
    except Exception as e:
        log(f"[S32][UE] pre-launch setup skipped: {e}")
    if project and os.path.isfile(project):
        cmd.append(project)              # open the world-building project directly
    if str(mode).lower() == "vr":
        cmd.append("-vr")                # OpenXR headset mode

    try:
        subprocess.Popen(cmd, cwd=os.path.dirname(d["editor_path"]))
        log(f"[S32][UE] launched {d['version']} mode={mode} "
            f"project={os.path.basename(project) if project else '(editor, no project)'}")
        return {"ok": True,
                "msg": f"Launching Unreal {d['version']} ({'VR' if str(mode).lower() == 'vr' else 'PC'})"
                       f"{' — ' + os.path.splitext(os.path.basename(project))[0] if project else ''}…{setup_note}"}
    except Exception as e:
        return {"ok": False, "msg": f"Launch failed: {str(e)[:120]}"}


# ── Remote Control client ─────────────────────────────────────────────────────
# The Remote Control API (enabled in-editor) exposes an HTTP server — its reach INTO
# a running Unreal instance, the ComfyUI-8188 equivalent. Default port 30010. Every
# world change is "call a UFUNCTION on a UObject": PUT /remote/object/call with
# {objectPath, functionName, parameters}. Verified against 5.8's own /remote/info.

_RC_DEFAULT = "http://127.0.0.1:30010"


def _rc_host():
    return str(CONFIG.get("unreal_rc_host", _RC_DEFAULT)).rstrip("/")


def rc_ping():
    """Is the Remote Control API answering? (GET /remote/info). Cheap liveness check —
    if this fails, the editor isn't running or the plugin isn't enabled, and nothing
    downstream can work. Returns {ok, error?}."""
    try:
        req = urllib.request.Request(_rc_host() + "/remote/info", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"ok": 200 <= resp.status < 300, "status": resp.status}
    except Exception as e:
        return {"ok": False, "error": str(e)[:160]}


def _rc_call(object_path, function_name, parameters=None, timeout=15):
    """PUT /remote/object/call — call a UFUNCTION on a UObject in the running editor.
    Returns (ok: bool, data|error_str). On HTTP errors, includes Unreal's own message
    body so we can SEE exactly what it rejected and iterate."""
    body = json.dumps({
        "objectPath": object_path,
        "functionName": function_name,
        "parameters": parameters or {},
        "generateTransaction": True,
    }).encode("utf-8")
    try:
        req = urllib.request.Request(
            _rc_host() + "/remote/object/call", data=body,
            headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return True, (json.loads(raw) if raw.strip() else {})
            except Exception:
                return True, raw
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        return False, f"HTTP {e.code}: {detail or e.reason}"
    except Exception as e:
        return False, str(e)[:200]


def spawn_cube(x=0.0, y=0.0, z=200.0):
    """PHASE 0 — spawn one cube in the running editor world.

    Deliberately minimal: hardcoded cube mesh, one call, a StaticMeshActor with the
    engine cube appears at (x,y,z). The ONLY thing this proves is 'can code reach the
    running world and make one thing appear' — the whole architecture in one gesture.
    Returns the FULL Remote Control response so we can see exactly what Unreal said and
    adjust in one iteration if the function signature differs on 5.8.

    First-attempt call: EditorLevelLibrary.SpawnActorFromObject with the engine cube.
    If 5.8 has retired that path, the returned error names the fix.
    """
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False, "stage": "ping",
                "error": "Remote Control isn't answering on " + _rc_host() +
                         " — is the editor running with the Remote Control API plugin enabled?"}
    ok, data = _rc_call(
        "/Script/EditorScriptingUtilities.Default__EditorLevelLibrary",
        "SpawnActorFromObject",
        {
            "ObjectToUse": "/Engine/BasicShapes/Cube.Cube",
            "Location": {"X": float(x), "Y": float(y), "Z": float(z)},
            "Rotation": {"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0},
        },
    )
    log(f"[S32][UE] spawn_cube ok={ok} at=({x},{y},{z}) resp={str(data)[:220]}")
    return {"ok": ok, "stage": "spawn", "response": data,
            "location": {"x": x, "y": y, "z": z}}


# ── Its voice-driven build tool ───────────────────────────────────────────────
# Basic engine shapes it can spawn by name. Arbitrary assets (Megascans, project
# meshes) come later via `asset=`; these primitives prove the parameterized loop.
_SHAPE_MESHES = {
    "cube":     "/Engine/BasicShapes/Cube.Cube",
    "sphere":   "/Engine/BasicShapes/Sphere.Sphere",
    "cylinder": "/Engine/BasicShapes/Cylinder.Cylinder",
    "cone":     "/Engine/BasicShapes/Cone.Cone",
    "plane":    "/Engine/BasicShapes/Plane.Plane",
}

# Per-type session counter for auto-naming spawned actors (cube_1, sphere_2, …), so every
# spawn has a human, unique identity instead of an anonymous engine name.
_spawn_counts = {}


def _asset_class(asset_path):
    """Describe an ASSET (not an actor) to get its class leaf, e.g. 'SkeletalMesh' /
    'StaticMesh'. Used to route spawning (skeletal meshes need special handling)."""
    try:
        body = json.dumps({"objectPath": asset_path}).encode("utf-8")
        req = urllib.request.Request(_rc_host() + "/remote/object/describe", data=body,
                                     headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        cls = str(data.get("Class") or "")
        return cls.rsplit(".", 1)[-1] if cls else None
    except Exception:
        return None


def _rc_set_property(obj_path, prop_name, value):
    """PUT /remote/object/property — set one property on an object. Best-effort bool."""
    try:
        body = json.dumps({"objectPath": obj_path, "access": "WRITE_TRANSACTION_ACCESS",
                           "propertyName": prop_name,
                           "propertyValue": {prop_name: value}}).encode("utf-8")
        req = urllib.request.Request(_rc_host() + "/remote/object/property", data=body,
                                     headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception:
        return False


def _spawn_skeletal(asset_path, x, y, z):
    """Spawn a SkeletalMesh properly: create a SkeletalMeshActor, then assign the mesh to its
    SkeletalMeshComponent (SpawnActorFromObject makes an empty/invisible actor for skeletal
    meshes). Best-effort — tries the component getter, then conventional component names, and
    several setter names — with a descriptive detail so a failure names its own fix.
    Returns (ok, actor_path, detail)."""
    ok, data = _rc_call(
        "/Script/EditorScriptingUtilities.Default__EditorLevelLibrary", "SpawnActorFromClass",
        {"ActorClass": "/Script/Engine.SkeletalMeshActor",
         "Location": {"X": float(x), "Y": float(y), "Z": float(z)},
         "Rotation": {"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0}})
    if not ok:
        return False, None, f"SpawnActorFromClass(SkeletalMeshActor) refused: {str(data)[:120]}"
    actor_path = None
    if isinstance(data, dict):
        rv = data.get("ReturnValue")
        actor_path = rv if isinstance(rv, str) else (rv.get("path") if isinstance(rv, dict) else None)
    if not actor_path:
        return False, None, "no actor path returned from SpawnActorFromClass"

    # Locate the skeletal mesh component.
    comp = None
    cok, cdata = _rc_call(actor_path, "GetComponentByClass",
                          {"ComponentClass": "/Script/Engine.SkeletalMeshComponent"})
    if cok and isinstance(cdata, dict):
        rv = cdata.get("ReturnValue")
        comp = rv if isinstance(rv, str) else (rv.get("path") if isinstance(rv, dict) else None)
    candidates = [c for c in (comp, actor_path + ".SkeletalMeshComponent0",
                              actor_path + ".SkeletalMeshComponent") if c]

    # Assign the mesh: try setter functions (new + old names), then a direct property set.
    for c in candidates:
        for fn in ("SetSkeletalMeshAsset", "SetSkeletalMesh"):
            sok, _ = _rc_call(c, fn, {"NewMesh": asset_path})
            if sok:
                return True, actor_path, f"mesh set via {fn}"
        if _rc_set_property(c, "SkeletalMeshAsset", asset_path):
            return True, actor_path, "mesh set via property"
    return True, actor_path, "actor spawned but mesh NOT assigned (component/setter needs iteration)"


def spawn_actor(shape="cube", x=0.0, y=0.0, z=200.0, asset=None, name=None,
                relative_to=None, direction="front", distance=200.0, asset_class=None):
    """Spawn one object into the live Unreal world — it first world-building tool. `shape`
    is a basic primitive (cube/sphere/cylinder/cone/plane); or pass `asset` = a full asset
    path. Position is (x,y,z) in Unreal units (~100 = 1 metre); z is up.

    RELATIVE PLACEMENT: to place it relative to another actor — 'a cube 200 units in front of
    PlayerStart' — pass `relative_to` (the anchor's name), `direction` (front/back/left/right/
    up/down), and `distance`. The spot is computed HERE from the anchor's real facing, so
    'front' is always correct — it must NOT compute x/y/z itself for these (that guessing is
    what made it spawn a wrong second object).

    `name` gives the object a human LABEL it and get_scene can refer to — so 'spawn a cube
    called anchor' → later 'move anchor' / 'delete anchor' resolve unambiguously. If omitted,
    a clean unique name is auto-assigned (cube_1, sphere_2, …) so every spawned thing has an
    identity instead of an anonymous StaticMeshActor_N. Returns that handle.
    """
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False, "error": "Unreal isn't reachable — launch the project and enable Remote Control."}

    # Relative placement computed server-side from the anchor's actual facing vector.
    if relative_to:
        apath, ainfo = _resolve_actor(relative_to)
        if not apath:
            opts = ", ".join(ainfo[:12]) if isinstance(ainfo, list) else str(ainfo)
            return {"ok": False, "error": f"Couldn't find anchor '{relative_to}' to place relative to. "
                                          f"Actors: {opts}"}
        rpos = _relative_position(apath, direction, distance)
        if rpos is None:
            return {"ok": False, "error": f"Couldn't read {relative_to}'s facing to compute '{direction}'."}
        x, y, z = rpos["X"], rpos["Y"], rpos["Z"]

    if asset:
        obj = asset
    else:
        shp = str(shape).lower().strip()
        if shp not in _SHAPE_MESHES:
            # Do NOT silently spawn a cube for an unknown shape (that turned 'spawn a tree'
            # into a cube). A non-primitive must come from find_asset → spawn with asset=<path>.
            return {"ok": False,
                    "error": f"'{shape}' isn't a basic shape (cube/sphere/cylinder/cone/plane). "
                             f"For anything else, call find_asset('{shape}') first to get its "
                             f"asset path, then spawn_actor with asset=<that path>. Nothing spawned."}
        obj = _SHAPE_MESHES[shp]

    # A SkeletalMesh asset needs a SkeletalMeshActor + assigned mesh; everything else
    # (primitives, static meshes) spawns via SpawnActorFromObject.
    # Prefer the class detected at FIND time (passed in) over a re-describe here — heavy
    # skeletal assets can make a spawn-time describe time out and return None.
    _acls = asset_class or (_asset_class(asset) if asset else None)
    _is_skeletal = bool(asset) and "skeletalmesh" in (_acls or "").lower()
    if asset:
        log(f"[S32][UE] spawn_actor asset='{asset.rsplit('/', 1)[-1]}' class={_acls} "
            f"is_skeletal={_is_skeletal}")

    def _pending(nm):
        # A timeout means our HTTP call gave up, NOT that Unreal failed — heavy assets keep
        # loading/placing after we time out. Report pending so it does NOT retry (which
        # spawns duplicates); it can verify with get_scene once it finishes streaming.
        log("[S32][UE] spawn_actor: timed out (heavy asset — Unreal is likely still placing it)")
        return {"ok": True, "pending": True, "spawned": nm, "name": nm,
                "location": {"x": float(x), "y": float(y), "z": float(z)}}

    if _is_skeletal:
        sk_ok, spawned_path, sk_detail = _spawn_skeletal(asset, x, y, z)
        log(f"[S32][UE] spawn_actor skeletal ok={sk_ok} ({sk_detail})")
        if not sk_ok:
            if "timed out" in sk_detail.lower():
                return _pending(name or asset.rsplit("/", 1)[-1])
            return {"ok": False, "error": f"Skeletal mesh spawn failed: {sk_detail}"}
    else:
        ok, data = _rc_call(
            "/Script/EditorScriptingUtilities.Default__EditorLevelLibrary",
            "SpawnActorFromObject",
            {
                "ObjectToUse": obj,
                "Location": {"X": float(x), "Y": float(y), "Z": float(z)},
                "Rotation": {"Pitch": 0.0, "Yaw": 0.0, "Roll": 0.0},
            },
            timeout=90,   # heavy imported assets (skeletal/streamed) take a while to load+place
        )
        if not ok:
            if "timed out" in str(data).lower():
                return _pending(name or (asset.rsplit("/", 1)[-1] if asset else obj))
            log(f"[S32][UE] spawn_actor failed: {str(data)[:120]}")
            return {"ok": False, "error": str(data)[:300]}
        # Grab the path of the actor we just spawned so we can name it and hand back a handle.
        spawned_path = None
        if isinstance(data, dict):
            rv = data.get("ReturnValue")
            if isinstance(rv, str):
                spawned_path = rv
            elif isinstance(rv, dict):
                spawned_path = rv.get("path") or rv.get("ObjectPath") or rv.get("Object")
    engine_name = spawned_path.rsplit(".", 1)[-1] if spawned_path else None

    # Label: operator-given wins; otherwise a clean per-type session name (cube_1, sphere_2).
    base = (os.path.basename(asset).split(".")[0] if asset else str(shape).lower().strip())
    if name:
        label = str(name).strip()
    else:
        _spawn_counts[base] = _spawn_counts.get(base, 0) + 1
        label = f"{base}_{_spawn_counts[base]}"

    named = False
    if spawned_path:
        lok, _ = _rc_call(spawned_path, "SetActorLabel",
                          {"NewActorLabel": label, "bMarkDirty": True})
        named = bool(lok)

    final = label if named else (engine_name or base)
    log(f"[S32][UE] spawn_actor '{final}' (named={named}) at=({x},{y},{z})")
    return {"ok": True, "spawned": final, "name": final, "named": named,
            "path": spawned_path, "location": {"x": float(x), "y": float(y), "z": float(z)}}


# ── Its eyes INTO the world ────────────────────────────────────────────────────
_EDITOR_LEVEL_LIB = "/Script/EditorScriptingUtilities.Default__EditorLevelLibrary"

# Ordered candidates for "list all actors", newest-first. On 5.8 the legacy
# EditorLevelLibrary.GetAllLevelActors is REFUSED remotely (deprecated), so we try the
# modern EditorActorSubsystem first and fall back to legacy for older engines. get_scene
# uses whichever the engine actually accepts and logs it, so we learn the real path for
# this install instead of guessing one.  (Spawn still rides EditorLevelLibrary — it works
# today; migrate it too only when the engine refuses it.)
_ACTOR_LIST_CANDIDATES = [
    ("/Script/UnrealEd.Default__EditorActorSubsystem", "GetAllLevelActors"),          # modern (UE5+)
    ("/Script/EditorScriptingUtilities.Default__EditorLevelLibrary", "GetAllLevelActors"),  # legacy
]


def get_scene(limit=80):
    """List the actors in the running Unreal level — its situational awareness, the SEE
    half of the build loop. Returns each actor's name + object path so it knows what
    exists and can target a specific actor to move/delete later.

    Tries the modern EditorActorSubsystem first, then the legacy library, and uses the
    first the engine accepts (5.8 refuses the legacy one). Returns a clear error naming
    the last response if none work, so a failure is diagnosable, not silent.
    """
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False,
                "error": "Unreal isn't reachable — launch the project and make sure the "
                         "Remote Control API plugin is enabled."}
    last = ""
    for obj_path, fn in _ACTOR_LIST_CANDIDATES:
        ok, data = _rc_call(obj_path, fn)
        if ok and isinstance(data, dict) and isinstance(data.get("ReturnValue"), list):
            paths = data["ReturnValue"]
            actors = []
            for p in paths[:limit]:
                label = _get_actor_label(p)                       # human name (what it refers to)
                nm = label or (str(p).rsplit(".", 1)[-1] if p else str(p))
                entry = {"name": nm, "path": p}
                atype = _describe_actor(p)     # WHAT it is (PlayerStart, DirectionalLight, …)
                if atype:
                    entry["type"] = atype
                loc = _get_actor_location(p)   # best-effort; same getter move_actor uses
                if loc:
                    entry["pos"] = {"x": round(loc.get("X", 0.0)),
                                    "y": round(loc.get("Y", 0.0)),
                                    "z": round(loc.get("Z", 0.0))}
                yaw = _get_actor_yaw(p)        # facing angle, for spatial/directional reasoning
                if yaw is not None:
                    entry["yaw"] = round(yaw)
                actors.append(entry)
            log(f"[S32][UE] get_scene {len(paths)} actor(s) via {obj_path.rsplit('.', 1)[-1]}")
            return {"ok": True, "count": len(paths), "shown": len(actors),
                    "actors": actors, "via": obj_path}
        last = str(data)[:220]
    log(f"[S32][UE] get_scene: no actor-list path accepted. last={last}")
    return {"ok": False,
            "error": "Couldn't list actors on this engine — none of the known methods were "
                     "accepted. Last response: " + last}


# ── Editing verbs: resolve a named actor, read its transform, move it ──────────
def _list_actors_raw():
    """Internal: the [{name, path}] list from the live level (reuses get_scene's fetch)."""
    r = get_scene()
    return r.get("actors", []) if r.get("ok") else []


def _resolve_actor(target):
    """Turn a name or loose description into an actor object path. FORGIVING: matches names
    ignoring case, spaces and underscores ('Player Start' == 'playerstart'), and understands
    TYPE words ('the light' → a lighting actor, 'the player spawn' → the PlayerStart, 'the
    camera' → a camera) so a near-miss label doesn't force a retry (which is what made it
    spawn duplicates). Returns (path, matched_name) on a unique hit, or (None, candidates).
    This resolution is the piece every per-actor verb (move/delete/…) reuses."""
    actors = _list_actors_raw()
    if not actors:
        return None, []
    tn = _norm(target)

    # 1. Exact normalized name.
    for a in actors:
        if _norm(a.get("name", "")) == tn:
            return a["path"], a["name"]
    # 2. Normalized substring on names (either direction).
    nmatch = [a for a in actors
              if tn and (tn in _norm(a.get("name", "")) or _norm(a.get("name", "")) in tn)]
    if len(nmatch) == 1:
        return nmatch[0]["path"], nmatch[0]["name"]
    # 3. Semantic TYPE match — does the target contain a word that maps to an actor type?
    hints = {sub for word, sub in _TYPE_HINTS.items() if word in tn}
    tmatch = [a for a in actors
              if hints and any(h in _norm(a.get("type", "")) for h in hints)]
    if len(tmatch) == 1:
        return tmatch[0]["path"], tmatch[0]["name"]
    # 4. Ambiguous — hand back the best candidate set for disambiguation.
    cands = nmatch or tmatch or actors
    return None, [a["name"] for a in cands]


# Operator words → substrings to look for in an actor's TYPE, so 'the light'/'player spawn'/
# 'camera' resolve by what the actor IS, not just its label.
_TYPE_HINTS = {
    "light": "light", "lighting": "light", "lamp": "light",
    "camera": "camera", "cam": "camera",
    "player": "playerstart", "playerstart": "playerstart",
    "playerspawn": "playerstart", "spawn": "playerstart",
    "fog": "fog",
}


def _get_actor_label(path):
    """Read an actor's editor LABEL (its human name), or None. The label is what the
    operator calls things ('cube_1', 'anchor') — distinct from the internal object name in
    the path — so get_scene reports labels and resolution matches on them."""
    ok, data = _rc_call(path, "GetActorLabel")
    if ok and isinstance(data, dict):
        lbl = data.get("ReturnValue")
        if isinstance(lbl, str) and lbl.strip():
            return lbl.strip()
    return None


def _describe_actor(path):
    """Read an actor's readable TYPE/class via /remote/object/describe, or None. Turns
    '/Script/Engine.PlayerStart' → 'PlayerStart', '/Script/Engine.DirectionalLight' →
    'DirectionalLight', so it understands WHAT each actor is (spawns the player, a light,
    a camera, a mesh) rather than just seeing a label."""
    try:
        body = json.dumps({"objectPath": path}).encode("utf-8")
        req = urllib.request.Request(_rc_host() + "/remote/object/describe", data=body,
                                     headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        cls = str(data.get("Class") or "")
        if cls:
            return cls.rsplit(".", 1)[-1].lstrip("_")   # readable leaf class name
    except Exception:
        pass
    return None


def _norm(s):
    """Normalize a name for forgiving matching: lowercase, keep only alphanumerics — so
    'Player Start', 'PlayerStart', 'player_start' all become 'playerstart'."""
    return "".join(c for c in str(s).lower() if c.isalnum())


def _get_actor_location(path):
    """Read an actor's current world location {X,Y,Z}, or None. Tries both known getter
    names (actor-level GetActorLocation, then the Blueprint-internal K2_ variant)."""
    for fn in ("GetActorLocation", "K2_GetActorLocation"):
        ok, data = _rc_call(path, fn)
        if ok and isinstance(data, dict):
            loc = data.get("ReturnValue")
            if isinstance(loc, dict) and "X" in loc:
                return loc
    return None


# Direction words → (the actor-facing vector getter, sign). front/back use the actor's
# forward vector, left/right its right vector; up/down use world Z (unambiguous). Computing
# direction from the actor's REAL facing vector server-side is what removes the axis-guessing
# that made it spawn a second object in the wrong place.
_DIRECTION_VECTORS = {
    "front":   ("GetActorForwardVector",  1), "forward": ("GetActorForwardVector",  1),
    "back":    ("GetActorForwardVector", -1), "behind":  ("GetActorForwardVector", -1),
    "right":   ("GetActorRightVector",    1), "left":    ("GetActorRightVector",   -1),
}


def _get_actor_vector(path, fn):
    """Read a unit direction vector (forward/right/up) for an actor, or None."""
    ok, data = _rc_call(path, fn)
    if ok and isinstance(data, dict):
        v = data.get("ReturnValue")
        if isinstance(v, dict) and "X" in v:
            return v
    return None


def _get_actor_yaw(path):
    """Read an actor's yaw (facing angle in degrees), or None — for rotational awareness."""
    for fn in ("GetActorRotation", "K2_GetActorRotation"):
        ok, data = _rc_call(path, fn)
        if ok and isinstance(data, dict):
            rot = data.get("ReturnValue")
            if isinstance(rot, dict) and "Yaw" in rot:
                return rot.get("Yaw")
    return None


def _relative_position(anchor_path, direction, distance):
    """World position `distance` units in `direction` from an anchor, using the anchor's
    OWN facing (so 'front' means where it actually points, computed correctly here rather
    than guessed). up/down use world Z. Returns {X,Y,Z} or None."""
    loc = _get_actor_location(anchor_path)
    if not loc:
        return None
    d = str(direction or "front").lower().strip()
    dist = float(distance)
    if d in ("up", "down"):
        return {"X": loc["X"], "Y": loc["Y"], "Z": loc["Z"] + (dist if d == "up" else -dist)}
    fn, sign = _DIRECTION_VECTORS.get(d, ("GetActorForwardVector", 1))
    vec = _get_actor_vector(anchor_path, fn)
    if not vec:
        return None
    return {"X": loc["X"] + sign * vec["X"] * dist,
            "Y": loc["Y"] + sign * vec["Y"] * dist,
            "Z": loc["Z"] + sign * vec["Z"] * dist}


def move_actor(target, dx=0.0, dy=0.0, dz=0.0, x=None, y=None, z=None):
    """Move an existing actor. RELATIVE by default (dx,dy,dz offsets — how an untrained
    builder speaks: 'raise it up' = +dz, 'nudge left' = -dy). If x/y/z are given, places
    at those absolute coordinates instead. World units ~100 = 1 metre; +Z is up.

    Relative moves read the current position first (that's why the getter matters), then
    apply the offset. Returns a compact factual result.
    """
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False, "error": "Unreal isn't reachable — launch the project and enable Remote Control."}
    path, info = _resolve_actor(target)
    if not path:
        opts = ", ".join(info[:14]) if isinstance(info, list) else str(info)
        return {"ok": False, "error": f"Couldn't find one actor matching '{target}'. "
                                      f"Actors in the level: {opts}"}
    name = info if isinstance(info, str) else str(target)

    absolute = (x is not None) or (y is not None) or (z is not None)
    cur = _get_actor_location(path)
    if absolute:
        base = cur or {"X": 0.0, "Y": 0.0, "Z": 0.0}
        newloc = {"X": float(x) if x is not None else base["X"],
                  "Y": float(y) if y is not None else base["Y"],
                  "Z": float(z) if z is not None else base["Z"]}
    else:
        if cur is None:
            return {"ok": False,
                    "error": f"Couldn't read {name}'s current position, so a relative move "
                             f"isn't possible. (The location-getter wasn't accepted remotely.)"}
        newloc = {"X": cur["X"] + float(dx), "Y": cur["Y"] + float(dy), "Z": cur["Z"] + float(dz)}

    ok, data = _rc_call(path, "SetActorLocation",
                        {"NewLocation": newloc, "bSweep": False, "bTeleport": True})
    if not ok:
        return {"ok": False, "error": str(data)[:250]}
    log(f"[S32][UE] move_actor {name} -> {newloc}")
    return {"ok": True, "actor": name, "mode": "absolute" if absolute else "relative",
            "location": {"x": newloc["X"], "y": newloc["Y"], "z": newloc["Z"]}}


# ── Editing verb: delete ──────────────────────────────────────────────────────
def delete_actor(target):
    """Remove an actor from the live level. Resolves the target by name/description (same
    rail as move_actor), then tries the editor-aware subsystem destroy first and falls
    back to actor-level self-destroy — using whichever the engine accepts. Returns a
    compact factual result; verify with get_scene if you want to confirm it's gone."""
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False, "error": "Unreal isn't reachable — launch the project and enable Remote Control."}
    path, info = _resolve_actor(target)
    if not path:
        opts = ", ".join(info[:14]) if isinstance(info, list) else str(info)
        return {"ok": False, "error": f"Couldn't find one actor matching '{target}'. "
                                      f"Actors in the level: {opts}"}
    name = info if isinstance(info, str) else str(target)

    # Ordered destroy candidates: editor-aware subsystem first (notifies the editor
    # cleanly), then actor-level self-destroy fallbacks. First accepted wins.
    candidates = [
        ("/Script/UnrealEd.Default__EditorActorSubsystem", "DestroyActor", {"ActorToDestroy": path}),
        (path, "K2_DestroyActor", {}),
        (path, "Destroy", {}),
    ]
    last = ""
    for obj_path, fn, params in candidates:
        ok, data = _rc_call(obj_path, fn, params)
        if ok:
            log(f"[S32][UE] delete_actor {name} via {fn}")
            return {"ok": True, "deleted": name, "via": fn}
        last = str(data)[:200]
    log(f"[S32][UE] delete_actor {name}: no destroy method accepted. last={last}")
    return {"ok": False, "error": f"Couldn't delete '{name}' — no destroy method was accepted. "
                                  f"Last response: {last}"}


# ── Asset discovery: find what's in the project to spawn ──────────────────────
_ASSET_REG_HELPERS = "/Script/AssetRegistry.Default__AssetRegistryHelpers"


def _asset_registry_path():
    """Get the AssetRegistry object's path via AssetRegistryHelpers.GetAssetRegistry (a
    static helper). Returns the object path string, or None."""
    ok, data = _rc_call(_ASSET_REG_HELPERS, "GetAssetRegistry")
    if ok and isinstance(data, dict):
        rv = data.get("ReturnValue")
        if isinstance(rv, str) and rv:
            return rv
        if isinstance(rv, dict):
            return rv.get("path") or rv.get("ObjectPath") or rv.get("Object")
    return None


def _reg_scan(reg_path, package_path):
    """Force a synchronous scan of a content path so freshly-imported / plugin-backed assets
    (Megascans/Megaplant) are gathered into the registry before we query. Best-effort."""
    try:
        _rc_call(reg_path, "ScanPathsSynchronous",
                 {"InPaths": [package_path], "bForceRescan": False,
                  "bIgnoreDenyListScanFilters": False}, timeout=30)
    except Exception:
        pass


def _reg_assets_by_path(reg_path, package_path, recursive=True):
    """List all AssetData under a content path via the registry directly. Returns a list of
    raw AssetData dicts, or None."""
    ok, data = _rc_call(reg_path, "GetAssetsByPath",
                        {"PackagePath": package_path, "bRecursive": recursive,
                         "bIncludeOnlyOnDiskAssets": False}, timeout=30)
    if ok and isinstance(data, dict):
        rv = data.get("ReturnValue")
        if isinstance(rv, list):
            return rv
    return None


def _parse_asset_data(a):
    """Normalize a raw AssetData dict (field names vary across UE versions) into
    {name, class, path}. Spawnable path is PackageName + '.' + AssetName."""
    if not isinstance(a, dict):
        return None
    name = a.get("AssetName") or a.get("Name")
    pkg = a.get("PackageName")
    cls = a.get("AssetClass")
    acp = a.get("AssetClassPath")
    if not cls and isinstance(acp, dict):
        cls = acp.get("AssetName") or acp.get("Name")
    path = None
    if pkg and name:
        path = f"{pkg}.{name}"
    elif a.get("ObjectPath"):
        path = a.get("ObjectPath")
    if not name or not path:
        return None
    return {"name": str(name), "class": str(cls) if cls else "?", "path": str(path)}


def _search_endpoint(query, class_names):
    """Fallback: the /remote/search/assets endpoint (what we had). Returns list of
    {name, class, path} or None on failure."""
    body = json.dumps({
        "Query": str(query),
        "Filter": {"PackageNames": [], "ClassNames": class_names, "PackagePaths": ["/Game"],
                   "RecursiveClassesExclusionSet": [], "RecursivePaths": True, "RecursiveClasses": True},
    }).encode("utf-8")
    try:
        req = urllib.request.Request(_rc_host() + "/remote/search/assets", data=body,
                                     headers={"Content-Type": "application/json"}, method="PUT")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        found = data.get("Assets", []) if isinstance(data, dict) else []
        return [{"name": a.get("Name"), "class": a.get("Class"), "path": a.get("Path")}
                for a in found if a.get("Path")]
    except Exception as e:
        log(f"[S32][UE] search-endpoint fallback failed: {str(e)[:100]}")
        return None


def find_asset(query, kind=None, limit=12):
    """Find imported project assets whose name matches `query` ('tree', 'rock', 'chair') and
    return name, class, and PATH — the path is what spawn_actor's asset= takes.

    PRIMARY: query the Asset Registry directly (scan the content tree, then list assets),
    because the registry is async/lazy and the plain search endpoint misses freshly-imported
    or plugin-backed content (Megascans/Megaplant) that hasn't been gathered yet. FALLS BACK
    to the search endpoint if the registry route isn't available, so it never does worse than
    before. Empty result = that content genuinely isn't imported yet (tell the operator; you
    can't download it)."""
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False, "error": "Unreal isn't reachable — launch the project and enable Remote Control."}
    q = str(query).lower().strip()
    words = [w for w in q.replace("_", " ").replace("-", " ").split() if len(w) > 1]
    kinds = ([kind.lower()] if isinstance(kind, str) else [k.lower() for k in kind]) if kind else []
    class_names = ([kind] if isinstance(kind, str) else list(kind)) if kind else []

    results, via = [], None

    def _kind_ok(p):
        return (not kinds) or any(k in p["class"].lower() for k in kinds)

    # PRIMARY: direct Asset Registry query (scan then list) — robust to async/lazy gathering.
    reg = _asset_registry_path()
    if reg:
        _reg_scan(reg, "/Game")
        raw = _reg_assets_by_path(reg, "/Game", True)
        if raw is not None:
            via = "registry"
            for a in raw:
                p = _parse_asset_data(a)
                if not p:
                    continue
                nlow = p["name"].lower()
                # Match if ANY query word appears in the name — so 'dead branch' finds
                # 'Dead_Branch', and 'branch' finds it too (word/substring, not whole-phrase).
                if words and not any(w in nlow for w in words):
                    continue
                if not _kind_ok(p):
                    continue
                results.append(p)

    # FALLBACK: the search endpoint, queried PER WORD and merged — the endpoint matches the
    # query narrowly, so 'dead branch' as one string misses; 'dead' + 'branch' separately
    # catches assets that contain either word. Dedup by path.
    if not results:
        seen = set()
        for w in (words or [q]):
            fb = _search_endpoint(w, class_names)
            if fb:
                via = via or "search-endpoint"
                for r in fb:
                    pth = r.get("path")
                    if pth and pth not in seen:
                        seen.add(pth)
                        results.append(r)

    # Put spawnable mesh types first — those are what it can actually place — so even a
    # very broad search surfaces the useful candidates at the top instead of a firehose.
    _MESH_FIRST = ("staticmesh", "skeletalmesh")
    results.sort(key=lambda r: 0 if any(m in str(r.get("class", "")).lower() for m in _MESH_FIRST) else 1)
    total = len(results)
    shown = results[:limit]
    log(f"[S32][UE] find_asset '{query}' via={via} -> {total} match(es)")
    return {"ok": True, "query": str(query), "count": total, "shown": len(shown),
            "assets": shown, "via": via}


# ═════════════════════════════════════════════════════════════════════════════
#  Programming in Unreal — the write → run → read the error → fix → rerun loop
# ═════════════════════════════════════════════════════════════════════════════
#  It writes editor Python; it runs INSIDE the live editor through Remote Control
#  (PythonScriptLibrary.ExecutePythonCommandEx). Every attempt is saved to
#  workspace\unreal\<task>\attempt_N.py (durable code state — survives chat compaction),
#  wrapped in an editor transaction (Ctrl+Z undoes a run), and its output/traceback
#  comes back so it can fix and rerun. Runs that touch files, saving, deleting,
#  processes or the network wait on the operator's Confirm card first. A clean run
#  parks for the operator's verdict (Works / Not right) — both the inner repairs
#  (failed code + error + the fix) and the verdict go into the training corpus.
#
#  Engine-side setup (one time, per project) — Config/DefaultRemoteControl.ini:
#     [/Script/RemoteControlCommon.RemoteControlSettings]
#     bEnableRemotePythonExecution=True
#     +CustomAllowedRemoteFunctionCalls=(ClassPath="/Script/PythonScriptPlugin.PythonScriptLibrary")
#  plus the Python Editor Script Plugin enabled.

import re
import time
import uuid
import hashlib
import threading

from s32_config import state, _script_dir, PERSON_DIR

_PY_LIB = "/Script/PythonScriptPlugin.Default__PythonScriptLibrary"
MAX_CODE_ATTEMPTS = 5
_CONFIRM_TIMEOUT = 300          # seconds the operator has to answer a Confirm card
_RUN_TIMEOUT = 120              # seconds a single script may run before we stop waiting

_SETUP_HELP = (
    "Unreal won't run Python remotely yet — the project's Remote Control settings block it. "
    "One-time setup in Unreal:\n"
    "  1. Edit → Plugins: enable 'Python Editor Script Plugin'.\n"
    "  2. Edit → Project Settings → Plugins → Remote Control: tick 'Enable Remote Python "
    "Execution', and under the allowed remote function calls add the class "
    "/Script/PythonScriptPlugin.PythonScriptLibrary.\n"
    "     (Or put DefaultRemoteControl.ini into <your project>\\Config\\ — the folder next to "
    "the .uproject file.)\n"
    "  3. Restart the editor.")
_SETUP_NEEDED = (
    "Python can't run in this Unreal project yet (its Remote Control settings block it). The "
    "operator now has a one-click setup card in chat; after they click it and restart Unreal, "
    "Python will work. Tell them that in one sentence. Do NOT call run_unreal_python again for this "
    "request. Either finish the task with spawn_actor / move_actor / delete_actor (spawn_actor "
    "takes absolute x/y/z, so you can place each item yourself), or tell the operator it needs "
    "the setup step.")

# Code that can reach beyond the level (disk, saving, deleting assets, processes,
# network, the editor itself) → the operator confirms first. Ordinary level edits run
# straight away and can be undone with Ctrl+Z.
_RISKY = [
    (r"\bos\.(remove|unlink|rmdir|removedirs|rename|replace)\b", "deletes or moves files on disk"),
    (r"\bshutil\.", "copies, moves or deletes files on disk"),
    (r"\bsend2trash\b", "deletes files"),
    (r"\.(unlink|rmdir|write_text|write_bytes)\s*\(", "changes files on disk"),
    (r"\bopen\s*\([^)]*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+]", "writes a file on disk"),
    (r"\bsubprocess\b|\bos\.(system|popen|startfile|exec\w*|spawn\w*)\b", "runs other programs"),
    (r"\bsocket\b|\burllib\b|\brequests\b|\bhttp\.client\b|\bhttpx\b", "uses the network"),
    (r"\bctypes\b|\b__import__\b|(?<![\w.])(?:eval|exec|compile)\s*\(", "runs dynamically built code"),
    (r"delete_(asset|directory|loaded_asset|loaded_assets)\b|\bdelete_assets?\b", "deletes project assets"),
    (r"\bsave_(asset|directory|loaded_asset|loaded_assets|packages|dirty_packages|current_level|map)\b|"
     r"\bsave_packages\b|EditorLoadingAndSavingUtils", "saves to disk (Ctrl+Z can't undo a save)"),
    (r"\brename_(asset|directory|loaded_asset)\b|\bconsolidate_assets\b", "renames or merges project assets"),
    (r"\bquit_editor\b|\bexecute_console_command\b|\bnew_level\b|\bload_level\b|\bload_map\b",
     "closes, switches or commands the editor"),
]


def _strip_comments(code):
    """Drop # comments (a commented-out save line shouldn't raise a Confirm card)."""
    try:
        import io, tokenize
        toks = [t for t in tokenize.generate_tokens(io.StringIO(code).readline)
                if t.type != tokenize.COMMENT]
        return tokenize.untokenize(toks)
    except Exception:
        return re.sub(r"(?m)^\s*#.*$", "", code)


def risky_reasons(code):
    out = []
    code = _strip_comments(code or "")
    for pat, why in _RISKY:
        if re.search(pat, code) and why not in out:
            out.append(why)
    return out


def _code_root():
    d = os.path.join(PERSON_DIR, "unreal")
    os.makedirs(d, exist_ok=True)
    return d


def _slug(s, n=40):
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return (s[:n] or "task")


def new_code_task(goal=""):
    """Called at the start of every operator turn: a fresh task, fresh attempt budget —
    and one fresh try at Python, in case the operator just did the setup."""
    state["ue_task"] = None
    state["ue_task_goal"] = goal or ""
    state["ue_python_blocked"] = False
    state["code_mode"] = False


def _task(purpose):
    t = state.get("ue_task")
    if not t:
        goal = state.get("ue_task_goal") or purpose or "unreal script"
        tid = time.strftime("%Y%m%d_%H%M%S") + "_" + _slug(goal, 30)
        t = {"id": tid, "goal": goal, "dir": os.path.join(_code_root(), tid), "attempts": []}
        os.makedirs(t["dir"], exist_ok=True)
        state["ue_task"] = t
        state.setdefault("ue_task_history", {})[tid] = t      # verdicts can arrive later
    return t


# ── the wrapper that runs inside the editor ──────────────────────────────────
_WRAPPER = r'''
import sys as _s32_sys, io as _s32_io, json as _s32_json, traceback as _s32_tb
_s32_buf = _s32_io.StringIO()
_s32_old = (_s32_sys.stdout, _s32_sys.stderr)
_s32_ok, _s32_err = True, ""
_s32_tx = None
try:
    import unreal as _s32_unreal
    try:
        _s32_tx = _s32_unreal.SystemLibrary.begin_transaction("System32", %(desc)r, None)
    except Exception:
        _s32_tx = None
except Exception:
    _s32_unreal = None
_s32_sys.stdout = _s32_sys.stderr = _s32_buf
try:
    import linecache as _s32_lc
    _s32_src = %(code)r
    _s32_lc.cache[%(fname)r] = (len(_s32_src), None, _s32_src.splitlines(True), %(fname)r)
    _s32_ns = {"__name__": "__s32__"}
    exec(compile(_s32_src, %(fname)r, "exec"), _s32_ns)
except SystemExit as _s32_x:
    if _s32_x.code not in (None, 0):
        _s32_ok, _s32_err = False, "SystemExit: %%s" %% (_s32_x.code,)
except BaseException:
    _s32_ok = False
    _t, _v, _b = _s32_sys.exc_info()
    _frames = [f for f in _s32_tb.extract_tb(_b) if f.filename == %(fname)r]
    _s32_err = "Traceback (most recent call last):\n" + "".join(_s32_tb.format_list(_frames)) \
               + "".join(_s32_tb.format_exception_only(_t, _v))
finally:
    _s32_sys.stdout, _s32_sys.stderr = _s32_old
    if _s32_tx is not None:
        try:
            _s32_unreal.SystemLibrary.end_transaction()
        except Exception:
            pass
    with open(%(result)r, "w", encoding="utf-8") as _f:
        _s32_json.dump({"ok": _s32_ok, "output": _s32_buf.getvalue()[-20000:],
                        "error": _s32_err[-8000:]}, _f)
'''


def _execute_in_editor(code, fname, desc):
    """Run code in the editor; return {ok, output, error} or {infra_error}."""
    run_dir = os.path.join(_code_root(), "_runs")
    os.makedirs(run_dir, exist_ok=True)
    result_path = os.path.join(run_dir, uuid.uuid4().hex + ".json")
    wrapper = _WRAPPER % {"code": code, "fname": fname, "desc": desc, "result": result_path}

    ok, data = _rc_call(_PY_LIB, "ExecutePythonCommandEx",
                        {"PythonCommand": wrapper, "ExecutionMode": "ExecuteFile",
                         "FileExecutionScope": "Private"}, timeout=_RUN_TIMEOUT)
    if not ok:
        low = str(data).lower()
        if "timed out" in low:
            return {"infra_error": f"The script is still running in Unreal after {_RUN_TIMEOUT}s — "
                                   f"it may be stuck in a loop or doing heavy work. Check the editor."}
        if ("cannot be accessed remotely" in low or "not allowed" in low or "is not exposed" in low
                or "unknown object" in low or "does not exist" in low or "http 4" in low):
            log(f"[S32][UE] python refused: {str(data)[:200]}")
            return {"infra_error": _SETUP_HELP, "blocked": True, "detail": str(data)[:300]}
        return {"infra_error": f"Remote Control call failed: {str(data)[:300]}"}

    # Our wrapper writes the real outcome to a file (same machine) — read it.
    for _ in range(20):
        if os.path.isfile(result_path):
            break
        time.sleep(0.1)
    try:
        with open(result_path, encoding="utf-8") as f:
            res = json.load(f)
        os.remove(result_path)
        return res
    except Exception:
        pass
    # Fallback: Unreal's own captured log (if the wrapper itself couldn't run).
    logs = data.get("LogOutput") if isinstance(data, dict) else None
    out, err = [], []
    for e in logs or []:
        if isinstance(e, dict):
            (err if "error" in str(e.get("Type", "")).lower() else out).append(str(e.get("Output", "")))
    return {"ok": not err and bool(isinstance(data, dict) and data.get("ReturnValue", True)),
            "output": "\n".join(out), "error": "\n".join(err)}


# ── Confirm card for risky code (the run waits for the operator) ─────────────
_confirms = {}      # token → {"event", "answer"}


def _push_card(payload):
    try:
        import s32_config as _cfg
        with _cfg._result_lock:
            _cfg._job_id += 1
            jid = _cfg._job_id
        _cfg._push_result(payload, jid)
        _cfg._push_done(jid)
    except Exception as e:
        log(f"[S32][UE] card push failed: {e}")


def answer_confirm(token, run):
    c = _confirms.get(token)
    if not c:
        return {"error": "That request already expired."}
    c["answer"] = bool(run)
    c["event"].set()
    return {"ok": True, "run": bool(run)}


def _ask_operator(code, reasons, purpose, attempt, where="Unreal"):
    token = uuid.uuid4().hex[:12]
    ev = threading.Event()
    _confirms[token] = {"event": ev, "answer": None}
    _push_card({"type": "ue_confirm", "token": token, "purpose": purpose or "", "where": where,
                "attempt": attempt, "reasons": reasons, "code": code})
    log(f"[S32][UE] waiting for operator to confirm attempt {attempt} ({'; '.join(reasons)})")
    t0 = time.time()
    while not ev.wait(1):
        if state.get("cancel") or time.time() - t0 > _CONFIRM_TIMEOUT:
            break
    ans = _confirms.pop(token, {}).get("answer")
    _push_card({"type": "ue_confirm_done", "token": token, "run": bool(ans)})
    return bool(ans)


# ── corpus ───────────────────────────────────────────────────────────────────
def _corpus(name, rec):
    try:
        from s32_training import _training_dir
        rec["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with open(os.path.join(_training_dir(), name), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"[S32][UE] corpus write failed: {e}")


def _hash(code):
    return hashlib.sha1(re.sub(r"\s+", " ", code or "").strip().encode("utf-8")).hexdigest()[:12]


# ── API lookup between attempts ──────────────────────────────────────────────
# Most of its failures are guessed API names ("get_all_actors", "iter_all_level_actors",
# "ActorFactoryCube.spawn_cube" — 2026-10-13, five attempts burned). When a run fails
# with "X has no attribute y", ask the LIVE editor what X really has and hand it the
# closest real names, the way a person would check the docs before the next try.
_ATTR_RX = re.compile(r"AttributeError: (?:'(\w+)' object|type object '(\w+)'|module '(unreal)') "
                      r"has no attribute '(\w+)'")

_LOOKUP = r'''
import unreal, difflib, json
_owner = %(owner)r
_obj = unreal if _owner == "unreal" else getattr(unreal, _owner, None)
_names = sorted(n for n in dir(_obj) if not n.startswith("_")) if _obj is not None else []
_want = %(attr)r
_words = [w for w in _want.lower().split("_") if len(w) > 2]
print(json.dumps({"found": _obj is not None,
                  "close": difflib.get_close_matches(_want, _names, 6, 0.45),
                  "related": [n for n in _names if any(w in n.lower() for w in _words)][:30]}))
'''


def _api_hint(traceback_text):
    m = _ATTR_RX.search(traceback_text or "")
    if not m:
        return ""
    owner = m.group(1) or m.group(2) or m.group(3)
    attr = m.group(4)
    try:
        res = _execute_in_editor(_LOOKUP % {"owner": owner, "attr": attr}, "lookup.py", "System 32: API lookup")
        info = json.loads((res.get("output") or "").strip().splitlines()[-1])
    except Exception as e:
        log(f"[S32][UE] API lookup failed: {e}")
        return ""
    if not info.get("found"):
        return f"(unreal.{owner} isn't a class in this engine's Python API.)"
    where = "the unreal module" if owner == "unreal" else f"unreal.{owner}"
    parts = []
    if info.get("close"):
        parts.append("closest real names: " + ", ".join(info["close"]))
    rel = [n for n in info.get("related", []) if n not in info.get("close", [])]
    if rel:
        parts.append("other names with those words: " + ", ".join(rel[:20]))
    if not parts:
        return f"{where} has no '{attr}' and nothing similar."
    hint = f"{where} has no '{attr}' — " + "; ".join(parts) + "."
    log(f"[S32][UE] API lookup: {hint[:160]}")
    return hint


# ── the tool ─────────────────────────────────────────────────────────────────
def run_unreal_python(code, purpose=""):
    """Run one attempt of its editor-Python script. Returns a 'ue_code' result."""
    code = (code or "").strip("\n")
    if not code.strip():
        return {"ok": False, "error": "No code was given — pass the full Python script as `code`."}
    state["code_mode"] = True          # rest of this turn: sampling tuned for code (s32_llm)
    if state.get("ue_python_blocked"):
        # Already refused this request — answer instantly, and don't show the card again.
        return {"ok": False, "error": _SETUP_NEEDED, "quiet": True}
    ping = rc_ping()
    if not ping.get("ok"):
        return {"ok": False, "error": "Unreal isn't reachable — launch the project and make sure "
                                      "the Remote Control API plugin is enabled."}
    t = _task(purpose)
    n = len(t["attempts"]) + 1
    if n > MAX_CODE_ATTEMPTS:
        last = t["attempts"][-1]
        if t.get("stop_told"):
            # It was already told to stop and called again anyway (2026-10-13: six more
            # calls after the cap). Tell the chain to end the turn.
            return {"ok": False, "halt": True, "quiet": True,
                    "error": "Out of attempts for this request — stop and report to the operator."}
        t["stop_told"] = True
        return {"ok": False, "type": "ue_code", "stopped": True, "task": t["id"], "attempt": n - 1,
                "purpose": purpose, "code": code, "file": last.get("file"),
                "error": f"Stopped after {MAX_CODE_ATTEMPTS} attempts. Last error: "
                         f"{(last.get('error') or '')[-300:]}"}
    h = _hash(code)
    for a in t["attempts"]:
        if a["hash"] == h and not a["ok"]:
            return {"ok": False, "type": "ue_code", "task": t["id"], "attempt": a["n"],
                    "purpose": purpose, "code": code, "duplicate": True,
                    "error": f"This is the SAME code as attempt {a['n']}, which already failed with: "
                             f"{(a.get('error') or '')[-300:]} — change the code before running again."}

    fname = f"attempt_{n}.py"
    fpath = os.path.join(t["dir"], fname)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(f"# System 32 — {t['goal'][:120]}\n# attempt {n}: {purpose[:120]}\n\n{code}\n")
    except Exception as e:
        log(f"[S32][UE] couldn't save attempt: {e}")

    reasons = risky_reasons(code)
    if reasons and not _ask_operator(code, reasons, purpose, n):
        t["attempts"].append({"n": n, "hash": h, "ok": False, "file": fpath,
                              "error": "Operator declined to run it.", "code": code})
        return {"ok": False, "type": "ue_code", "task": t["id"], "attempt": n, "purpose": purpose,
                "code": code, "file": fpath, "declined": True, "reasons": reasons,
                "error": "The operator declined to run this script (it " + "; ".join(reasons) +
                         "). Don't retry it as-is — ask what they'd like instead."}

    desc = f"System 32: {(purpose or t['goal'])[:80]}"
    t0 = time.time()
    res = _execute_in_editor(code, fname, desc)
    secs = round(time.time() - t0, 1)
    if res.get("infra_error"):
        log(f"[S32][UE] attempt {n} couldn't run: {res['infra_error'][:120]}")
        if res.get("blocked"):
            state["ue_python_blocked"] = True
            # Operator gets the ONE-CLICK setup card (manual steps only if no project can
            # be found); IT gets a clear "stop, finish another way" — without it it
            # retried Python and looped on get_scene.
            try:
                carded = offer_setup_card()
            except Exception:
                carded = False
            if carded:
                return {"ok": False, "error": _SETUP_NEEDED, "quiet": True}
            return {"ok": False, "error": _SETUP_NEEDED,
                    "group": "activity", "label": "unreal", "content": _SETUP_HELP}
        return {"ok": False, "error": res["infra_error"]}

    ok = bool(res.get("ok"))
    out = (res.get("output") or "").strip()
    err = (res.get("error") or "").strip()
    t["attempts"].append({"n": n, "hash": h, "ok": ok, "file": fpath, "error": err, "code": code})
    log(f"[S32][UE] attempt {n} {'OK' if ok else 'FAILED'} in {secs}s — {(err.splitlines() or [''])[-1][:120]}")

    # Inner repair triples: every earlier failure + the code that finally ran clean.
    if ok and len(t["attempts"]) > 1:
        prev = [a for a in t["attempts"][:-1] if not a["ok"] and a.get("error") != "Operator declined to run it."]
        for a in prev:
            _corpus("unreal_code_repairs.jsonl",
                    {"task": t["id"], "goal": t["goal"], "failed_code": a["code"],
                     "error": a["error"], "fixed_code": code, "attempts_to_fix": n - a["n"]})
    if ok:
        t["clean"] = {"attempt": n, "code": code, "file": fpath}
    hint = "" if ok else _api_hint(err)
    return {"ok": ok, "type": "ue_code", "hint": hint, "task": t["id"], "attempt": n, "max": MAX_CODE_ATTEMPTS,
            "purpose": purpose, "code": code, "file": fpath, "output": out[-4000:],
            "traceback": err[-4000:], "seconds": secs}


def record_verdict(task_id, works, note=""):
    """The operator's ruling on a clean run — the outer label for the corpus."""
    t = (state.get("ue_task_history") or {}).get(task_id)
    if not t:
        return {"error": "Unknown task."}
    clean = t.get("clean") or {}
    _corpus("unreal_code_verdicts.jsonl",
            {"task": task_id, "goal": t["goal"], "verdict": "works" if works else "not_right",
             "note": (note or "").strip()[:1000], "code": clean.get("code", ""),
             "attempts": len(t["attempts"]),
             "failures": [a.get("error", "")[-500:] for a in t["attempts"] if not a["ok"]]})
    t["verdict"] = "works" if works else "not_right"
    try:
        from s32_training import label_link
        label_link("ue:" + str(task_id), works, "unreal_verdict", note)
    except Exception:
        pass
    log(f"[S32][UE] verdict for {task_id}: {t['verdict']} {('— ' + note[:80]) if note else ''}")
    # Put the verdict where IT will see it next turn (user side — never its own role).
    msg = (f"[Operator verdict on your Unreal script ({t['goal'][:60]}): "
           + ("✓ it works as wanted." if works else
              "✗ not right" + (f" — {note.strip()[:300]}" if note.strip() else "") + ".") + "]")
    for m in reversed(state.get("messages") or []):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            m["content"] += "\n" + msg
            break
    return {"ok": True, "verdict": t["verdict"]}


# ═════════════════════════════════════════════════════════════════════════════
#  Automatic project setup — everything it needs, switched on for the user
# ═════════════════════════════════════════════════════════════════════════════
#  A fresh user can't be expected to know about Remote Control settings. Everything
#  it needs lives in two plain files in the project folder, so System 32 edits them:
#    • <Project>.uproject      → enable PythonScriptPlugin + RemoteControl
#                                 (engine plugins — no download)
#    • Config\DefaultRemoteControl.ini → remote Python on + PythonScriptLibrary allowed
#  Originals are backed up once as *.s32bak. Plugins/settings load at editor start, so
#  a running editor needs ONE restart (we never close it for the user — unsaved work).

import glob

_REQUIRED_PLUGINS = [
    ("PythonScriptPlugin", "Python Editor Script Plugin"),
    ("RemoteControl", "Remote Control API"),
    # (EditorScriptingUtilities is on by default and already works on 5.8 — listing a
    #  plugin the engine later drops would make Unreal complain at startup, so we don't.)
]
_RC_SECTION = "[/Script/RemoteControlCommon.RemoteControlSettings]"
# Boolean switches that must be True. ORDER MATTERS for the human labels only.
# Remote Python is only honoured with "Restrict Server Access" on — in Project Settings
# the Python checkbox is greyed out until it's ticked (operator saw exactly that,
# 2026-10-12; documented by unreal-mcp-agent). Restricting server access limits who may
# connect; this machine (127.0.0.1) is still allowed.
_RC_BOOLS = [
    ("bRestrictServerAccess", "Restrict Server Access"),
    ("bEnableRemotePythonExecution", "Enable Remote Python Execution"),
]
_RC_ALLOW_LINE = '+CustomAllowedRemoteFunctionCalls=(ClassPath="/Script/PythonScriptPlugin.PythonScriptLibrary")'
_RC_LINES = [(k, f"{k}=True") for k, _ in _RC_BOOLS] + [("PythonScriptLibrary", _RC_ALLOW_LINE)]


def _as_uproject(p):
    """Accept a .uproject path or its folder; return the .uproject path or None."""
    if not p:
        return None
    p = os.path.normpath(str(p).strip().strip('"'))
    if p.lower().endswith(".uproject") and os.path.isfile(p):
        return p
    if os.path.isdir(p):
        hits = glob.glob(os.path.join(p, "*.uproject"))
        if hits:
            return hits[0]
    return None


def _running_project():
    """Ask the open editor which project it has loaded."""
    ok, data = _rc_call("/Script/Engine.Default__KismetSystemLibrary", "GetProjectDirectory", timeout=5)
    if ok and isinstance(data, dict) and isinstance(data.get("ReturnValue"), str):
        return _as_uproject(data["ReturnValue"])
    return None


def _recent_projects():
    """Unreal's own recently-opened list (newest first)."""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "UnrealEngine")
    found = []
    for ini in glob.glob(os.path.join(base, "*", "Saved", "Config", "Windows*", "EditorSettings.ini")):
        try:
            txt = open(ini, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in re.finditer(r'ProjectName="([^"]+\.uproject)"(?:,LastOpenTime=([0-9.:\-]+))?', txt):
            found.append((m.group(2) or "", m.group(1)))
        for m in re.finditer(r'RecentlyOpenedProjectFiles=([^\r\n(]+\.uproject)', txt):
            found.append(("", m.group(1)))
    out = []
    for _, p in sorted(found, key=lambda x: x[0], reverse=True):
        up = _as_uproject(p)
        if up and up not in out:
            out.append(up)
    return out


def remember_project(path):
    """Save the project in config.json so launch opens straight into it."""
    try:
        import s32_config as _cfgmod
        CONFIG["unreal_project"] = path
        cf = _cfgmod._config_file
        data = {}
        if os.path.isfile(cf):
            with open(cf, encoding="utf-8") as f:
                data = json.load(f)
        if data.get("unreal_project") != path:
            data["unreal_project"] = path
            with open(cf, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[S32][UE] couldn't remember project: {e}")


def find_project():
    """The project to set up / launch: the one open in the editor, else the configured
    one, else Unreal's most recent. Returns (path or None, how)."""
    if rc_ping().get("ok"):
        p = _running_project()
        if p:
            return p, "open in the editor"
    p = _as_uproject(CONFIG.get("unreal_project", ""))
    if p:
        return p, "configured"
    rec = _recent_projects()
    if rec:
        return rec[0], "most recently opened"
    return None, "none found"


def _read_ini(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def check_setup(project):
    """What's missing for it in this project. Returns a list of human-readable items."""
    missing = []
    try:
        with open(project, encoding="utf-8-sig") as f:
            up = json.load(f)
    except Exception as e:
        return [f"couldn't read {os.path.basename(project)} ({e})"]
    enabled = {str(p.get("Name", "")).lower(): bool(p.get("Enabled"))
               for p in (up.get("Plugins") or []) if isinstance(p, dict)}
    for name, label in _REQUIRED_PLUGINS:
        if not enabled.get(name.lower()):
            missing.append(f"plugin: {label}")
    for path, is_default in _rc_ini_files(project):
        for prob in _rc_ini_problems(_read_ini(path), is_default):
            item = f"setting: {prob}"
            if item not in missing:
                missing.append(item)
    return missing


def _rc_ini_files(project):
    r"""Every RemoteControl ini Unreal layers for this project: the project default, plus
    the per-user SAVED copies (Saved\Config\Windows*\RemoteControl.ini). The saved layer
    loads on top — an old 'bEnableRemotePythonExecution=False' there silently overrides
    the default (suspected on the 2026-10-12 boot: setup applied, still refused)."""
    root = os.path.dirname(project)
    files = [(os.path.join(root, "Config", "DefaultRemoteControl.ini"), True)]
    for p in glob.glob(os.path.join(root, "Saved", "Config", "Windows*", "RemoteControl.ini")):
        files.append((p, False))          # only touched if it already has our section
    return files


def _rc_ini_problems(text, must_exist):
    sect = _ini_section(text, _RC_SECTION)
    has_sect = _RC_SECTION.lower() in text.lower()
    out = []
    if must_exist or has_sect:
        for key, label in _RC_BOOLS:
            if must_exist and not re.search(r"(?im)^\s*" + key + r"\s*=\s*true\s*$", sect):
                out.append(label)
            elif not must_exist and re.search(r"(?im)^\s*" + key + r"\s*=\s*false\s*$", sect):
                out.append(f"{label} (switched off in the saved settings)")
        if must_exist and "pythonscriptlibrary" not in sect.lower():
            out.append("allow PythonScriptLibrary calls")
        if not must_exist and re.search(r"(?im)^\s*!?CustomAllowedRemoteFunctionCalls\s*=", sect) \
                and "pythonscriptlibrary" not in sect.lower():
            out.append("allow PythonScriptLibrary calls (cleared in the saved settings)")
    return out


def _fix_rc_ini(path, create):
    """Make one RemoteControl ini allow remote Python. Returns True if it changed."""
    text = _read_ini(path)
    if not create and _RC_SECTION.lower() not in text.lower():
        return False
    sect = _ini_section(text, _RC_SECTION)
    add = []
    for key, _label in _RC_BOOLS:
        if re.search(r"(?im)^\s*" + key + r"\s*=\s*true\s*$", sect):
            continue
        new_sect = re.sub(r"(?im)^[ \t]*" + key + r"[ \t]*=.*$\n?", "", sect)   # drop a False line
        if new_sect != sect:
            text = text.replace(sect, new_sect, 1)
            sect = new_sect
        add.append(f"{key}=True")
    if "pythonscriptlibrary" not in sect.lower():
        add.append(_RC_ALLOW_LINE)
    if not add:
        return False
    _backup(path)
    if _RC_SECTION.lower() in text.lower():
        idx = text.lower().index(_RC_SECTION.lower()) + len(_RC_SECTION)
        rest = text[idx:]
        text = text[:idx] + "\n" + "\n".join(add) + ("" if rest.startswith(("\n", "\r")) else "\n") + rest
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + _RC_SECTION + "\n" + "\n".join(add) + "\n"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return True


def _ini_section(text, header):
    m = re.search(r"(?ims)^" + re.escape(header) + r"\s*$(.*?)(?=^\[|\Z)", text)
    return m.group(1) if m else ""


def _backup(path):
    b = path + ".s32bak"
    if os.path.isfile(path) and not os.path.exists(b):
        import shutil
        shutil.copy2(path, b)


def apply_setup(project):
    """Switch on everything it needs. Backs up the originals once. Returns a summary."""
    changed = []
    # 1) plugins in the .uproject
    with open(project, encoding="utf-8-sig") as f:
        up = json.load(f)
    plugins = up.setdefault("Plugins", [])
    by_name = {str(p.get("Name", "")).lower(): p for p in plugins if isinstance(p, dict)}
    for name, label in _REQUIRED_PLUGINS:
        p = by_name.get(name.lower())
        if p is None:
            plugins.append({"Name": name, "Enabled": True})
            changed.append(f"enabled {label}")
        elif not p.get("Enabled"):
            p["Enabled"] = True
            changed.append(f"enabled {label}")
    if any(c.startswith("enabled") for c in changed):
        _backup(project)
        with open(project, "w", encoding="utf-8") as f:
            json.dump(up, f, indent="\t")             # Unreal's own .uproject style
            f.write("\n")
    # 2) RemoteControl settings — the project default AND any saved per-user copy that
    #    would override it.
    for path, is_default in _rc_ini_files(project):
        if _fix_rc_ini(path, create=is_default):
            rel = os.path.relpath(path, os.path.dirname(project))
            changed.append(f"turned on remote Python in {rel}")
    remember_project(project)
    log(f"[S32][UE] setup applied to {os.path.basename(project)}: {changed or 'nothing needed'}")
    return changed


def setup_status():
    """For the UI/card: which project, what's missing, whether the editor is open."""
    proj, how = find_project()
    if not proj:
        return {"ok": False, "project": None,
                "error": "No Unreal project found — open your project in Unreal once (or set it in "
                         "Unreal settings) and System 32 will pick it up."}
    return {"ok": True, "project": proj, "name": os.path.splitext(os.path.basename(proj))[0],
            "how": how, "missing": check_setup(proj), "editor_open": bool(rc_ping().get("ok"))}


def offer_setup_card():
    """Show the one-click setup card in chat (used when Python is refused)."""
    st = setup_status()
    if not st.get("ok"):
        return False
    if not st["missing"]:
        # The files already allow it, yet Unreal refused: it's running on settings it
        # loaded before the change (restart), or something inside Unreal overrides them.
        _push_card({"group": "activity", "label": "unreal", "content":
                    f"{st['name']} already has Python switched on in its files, but Unreal is still "
                    f"refusing it. If Unreal was open before System 32 changed them, save your work "
                    f"and restart Unreal. If it was started after (e.g. from System 32's launch "
                    f"button), open Edit → Project Settings → Plugins → Remote Control and make sure "
                    f"'Enable Remote Python Execution' is ticked."})
        return True
    _push_card({"type": "ue_setup", "project": st["project"], "name": st["name"],
                "missing": st["missing"], "editor_open": st["editor_open"]})
    return True


def run_setup(project, launch_after=False):
    """The card's button. Apply, then tell the operator the one thing left to do."""
    proj = _as_uproject(project)
    if not proj:
        return {"ok": False, "error": "That project file isn't there anymore."}
    if rc_ping().get("ok"):
        # Unreal writes its in-memory settings back to Saved\Config when it closes — doing
        # this while it's open would be undone on exit. Ask for a close first.
        return {"ok": False, "retry": True,
                "error": "Save your work and close Unreal first — it rewrites its settings when "
                         "it closes and would undo this. Then click again and I'll open it back up."}
    try:
        changed = apply_setup(proj)
    except PermissionError:
        return {"ok": False, "error": "Windows wouldn't let me edit the project files (read-only "
                                      "or open elsewhere). Close other tools using them and retry."}
    except Exception as e:
        return {"ok": False, "error": f"Setup failed: {str(e)[:160]}"}
    open_now = bool(rc_ping().get("ok"))
    if open_now:
        nxt = "Save your work and restart Unreal — the plugins and settings load when it starts."
    elif launch_after:
        launch_unreal("pc")
        nxt = "Launching Unreal with everything switched on."
    else:
        nxt = "Everything's set — it takes effect the next time Unreal opens this project."
    return {"ok": True, "changed": changed, "next": nxt, "editor_open": open_now}
