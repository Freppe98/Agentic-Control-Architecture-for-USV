"""
Operation layer for the Local Agent's replanning HTTP surface.

agent_server.py stays a thin request parser; every operation's validation,
persistence, and result shaping lives here -- the same routes->services split
the vehicle Flask side uses. Every function returns (http_status, body_dict).

Canonical write path (see the task's section 7 and the final handoff): these
are DIRECT request/response operations against the Local Agent's own inbound
server, NOT the deliver-once operator command queue and NEVER Pixhawk command
handling. The replanning state (planning package, injection store, live
controller/config) all live in this process, so this is the only place that can
serve them with a durable, idempotent result. The Operator Backend proxies to
this surface exactly as it already proxies GET /agent/pixhawk_mission.

Idempotency / duplicate protection: the planning package and the injection are
both SINGLE-SLOT stores written atomically -- a duplicate PUT from a second
operator replaces with identical content (same route hash) rather than creating
a second entry, and DELETE is idempotent. No operation appends, so concurrent
duplicate requests cannot accumulate.
"""
from typing import Any, Optional, Tuple

import experiment_injection
import pixhawk_mission
import planning_package
import replan_runtime
from config import USV_ID

try:  # optional -- the mission-execution controller may not be wired in every build
    import mission_execution_runtime
except Exception:  # pragma: no cover - defensive
    mission_execution_runtime = None


def _error(code: str, message: str, http: int = 400) -> Tuple[int, dict]:
    return http, {"accepted": False, "error": {"code": code, "message": message}}


def _notify_new_original_package(package: dict, route_hash: Optional[str]) -> Optional[dict]:
    """Explicit new-original-mission notification (task section 2). After a new
    immutable package is accepted AND verified against a fresh Pixhawk readback,
    tell BOTH controllers so a replacement is handled deterministically at store
    time rather than being stumbled upon later at rearm:

      * the mission-execution controller invalidates the previous mission's
        execution-specific state (only when terminal/idle; an active mission is
        never silently replaced -- it reports a conflict instead);
      * the replan controller re-arms its terminal trigger latch (a new original
        mission is a sanctioned new-generation condition, task section 1).

    Fail-soft: never lets a notification error fail the acceptance response."""
    mission_id = package.get("mission_id")
    count = len(package.get("route") or [])
    result: dict = {}
    try:
        ctrl = mission_execution_runtime.get_controller() if mission_execution_runtime else None
        if ctrl is not None and hasattr(ctrl, "on_new_package_stored"):
            result = ctrl.on_new_package_stored(mission_id, route_hash, count) or {}
    except Exception as e:  # pragma: no cover - defensive
        result = {"adopted": None, "reason": f"notification error: {e}"}
    try:
        replan_ctrl = replan_runtime.get_controller()
        # Only rearm the replan latch when the new mission was actually adopted
        # (terminal/idle). During active execution the running mission's replan
        # context must not be disturbed.
        if replan_ctrl is not None and result.get("adopted") and hasattr(replan_ctrl, "note_new_mission"):
            replan_ctrl.note_new_mission(f"new original package {mission_id} stored")
    except Exception:  # pragma: no cover - defensive
        pass
    return result or None


# ── Planning package ──────────────────────────────────────────────────────────
# The canonical Local-Agent replan planning-package surface. Accepts the
# immutable original replan-planning-package-v1 the Operator authors, validates
# it (structurally AND against the live Pixhawk route), and stores it atomically.
# Both POST and PUT map here (agent_server.py); the resource path is the existing
# /agent/replan/planning_package prefix -- no route is duplicated.
def _pixhawk_readback(proof: bool = False) -> dict:
    """The current Pixhawk mission readback via the existing mission service
    (read-only). Never raises -- an unreachable vehicle Flask API degrades to a
    reachable=False payload the caller fails closed on.

    `proof=True` returns a FRESH, proof-grade readback (requests a coordinator
    refresh and waits for the refresh generation to advance) for the ACCEPTANCE
    path, where validating against a stale cached readback would be unsafe.
    `proof=False` is the cache-first DISPLAY read used by the readiness GET
    (whose staleness is surfaced honestly by build_readiness, not acted on)."""
    try:
        if proof:
            return pixhawk_mission.build_pixhawk_mission_proof()
        return pixhawk_mission.build_pixhawk_mission_status()
    except Exception as e:  # defensive: builders already wrap their own errors
        return {"reachable": False, "error": f"pixhawk readback failed: {e}",
                "route_content_hash": None, "route_waypoint_count": None,
                "partial": None, "mission_valid": None}


# HTTP status for a Pixhawk-consistency rejection: a genuine content conflict is
# 409, an availability problem is 503, anything else 400.
_PIXHAWK_HTTP = {
    "ROUTE_HASH_MISMATCH": 409, "ROUTE_COUNT_MISMATCH": 409, "ACTIVE_MISSION_CHANGED": 409,
    "PIXHAWK_UNAVAILABLE": 503, "PIXHAWK_READBACK_PARTIAL": 503,
    "ROUTE_HASH_UNAVAILABLE": 503, "PIXHAWK_MISSION_INVALID": 503,
    "PIXHAWK_READBACK_STALE": 503, "PIXHAWK_READBACK_UNVERIFIED": 503,
}


def _envelope_meta(env: Optional[dict]) -> Optional[dict]:
    """The store envelope's provenance/metadata, WITHOUT the (large) package
    bodies -- those are returned separately as `package`."""
    if not env:
        return None
    return {k: env.get(k) for k in (
        "store_version", "generation", "received_at", "validated_at",
        "pixhawk_hash_used", "validation", "active_package_revision",
        "revision_history", "package_identity", "idempotent")}


def get_planning_package() -> Tuple[int, dict]:
    pkg = planning_package.load()
    env = planning_package.load_envelope()
    readiness = planning_package.build_readiness(_pixhawk_readback())
    return 200, {
        "stored": pkg is not None,
        "usable": planning_package.is_usable(pkg),
        "package": pkg,
        "summary": planning_package.summary(pkg),
        "envelope": _envelope_meta(env),
        "readiness": readiness,
    }


def put_planning_package(body: Any) -> Tuple[int, dict]:
    # 1. Offline structural + semantic validation. Fail closed; store nothing.
    package, code, message = planning_package.validate_package_v1(body, USV_ID)
    if package is None:
        print(f"[REPLAN] planning_package rejected (structural): {code}: {message}")
        return _error(code, message, http=400)

    # 2. Independently confirm the package describes the mission actually on the
    #    Pixhawk. FRESH proof-grade readbacks (each requests a refresh and waits
    #    for the refresh generation to advance) -- acceptance must never validate
    #    against a stale cached readback. Two readbacks so a mission changing
    #    mid-validation is still caught.
    rb1 = _pixhawk_readback(proof=True)
    rb2 = _pixhawk_readback(proof=True)
    ok, pcode, pmsg, evidence = planning_package.verify_pixhawk_consistency(package, rb1, rb2)
    if not ok:
        print(f"[REPLAN] planning_package rejected (pixhawk consistency): {pcode}: {pmsg} evidence={evidence!r}")
        # A rejected replacement must NOT destroy the previously usable package:
        # store_accepted is never reached, so the prior envelope is untouched.
        http = _PIXHAWK_HTTP.get(pcode, 400)
        return http, {"accepted": False, "stored": False,
                      "error": {"code": pcode, "message": pmsg},
                      "pixhawk_consistency": evidence}

    # 3. Atomic store as the immutable original safety envelope.
    env = planning_package.store_accepted(package, rb1.get("route_content_hash"), evidence)
    # 4. Explicit new-original-mission notification to the execution + replan
    #    controllers (task section 2) -- deterministic replacement lifecycle at
    #    store time, not stumbled upon later at rearm. Fail-soft.
    execution_lifecycle = _notify_new_original_package(package, rb1.get("route_content_hash"))
    return 200, {
        "accepted": True,
        "stored": True,
        "idempotent": env.get("idempotent", False),
        "generation": env.get("generation"),
        "mission_id": package["mission_id"],
        "mission_revision": package["mission_revision"],
        "route_waypoint_count": len(package["route"]),
        "route_hash": package["route_hash"],
        "route_content_hash": package["route_hash"],
        "pixhawk_hash_used": env.get("pixhawk_hash_used"),
        "summary": planning_package.summary(package),
        "readiness": planning_package.build_readiness(rb1),
        "validation": {"valid": True},
        "execution_lifecycle": execution_lifecycle,
    }


def delete_planning_package() -> Tuple[int, dict]:
    # Explicit operator-initiated invalidation. Deletion is unconditional (the
    # operator asked for it); the safety comes from acceptance never destroying a
    # usable package on a REJECTED replacement -- see put_planning_package.
    removed = planning_package.clear()
    return 200, {"accepted": True, "cleared": removed}


# ── Experiment injection ──────────────────────────────────────────────────────
def get_experiment() -> Tuple[int, dict]:
    return 200, experiment_injection.status(USV_ID)


def put_experiment(body: Any) -> Tuple[int, dict]:
    kwargs, code, message = experiment_injection.validate(body, USV_ID)
    if kwargs is None:
        return _error(code, message, http=400)
    injected = experiment_injection.inject(**kwargs)
    return 200, {"accepted": True, "source": experiment_injection.SOURCE_SIMULATED,
                 "injection": injected}


def delete_experiment() -> Tuple[int, dict]:
    cleared = experiment_injection.clear()
    return 200, {"accepted": True, "cleared": cleared,
                 "source": experiment_injection.SOURCE_SIMULATED}


# ── Runtime config ────────────────────────────────────────────────────────────
def get_config() -> Tuple[int, dict]:
    return 200, replan_runtime.resolved_config()


def patch_config(body: Any) -> Tuple[int, dict]:
    result, code, message = replan_runtime.apply_config_patch(body if isinstance(body, dict) else {})
    if result is None:
        http = 409 if code == "TRANSACTION_ACTIVE" else 400
        return _error(code, message, http=http)
    return 200, {"accepted": True, **result}


# ── Status ────────────────────────────────────────────────────────────────────
def get_status() -> Tuple[int, dict]:
    controller = replan_runtime.get_controller()
    if controller is None:
        return 503, {"error": {"code": "CONTROLLER_NOT_READY",
                               "message": "replanning controller not initialised yet"}}
    return 200, controller.status()


# ── Reset ─────────────────────────────────────────────────────────────────────
def reset(body: Any = None) -> Tuple[int, dict]:
    controller = replan_runtime.get_controller()
    if controller is None:
        return 503, {"error": {"code": "CONTROLLER_NOT_READY",
                               "message": "replanning controller not initialised yet"}}
    result = controller.reset()
    if not result.get("reset"):
        return 409, {"accepted": False,
                     "error": {"code": "RESET_REJECTED", "message": result.get("reason")}}
    # Also clear the energy debounce so a fresh streak is required after a reset.
    energy = replan_runtime.get_energy()
    if energy is not None:
        energy.reset()
    return 200, {"accepted": True, **result}
