"""
Shared, context-agnostic mission-progression verification.

ONE verifier answers every "did AUTO actually start the mission" question in the
Local Agent -- Start, Resume, and the safe-return revised-mission AUTO. It
replaces the old one-shot `mode==AUTO and mission_active` check (which failed a
genuinely-running mission on a single inactive/UNKNOWN sample -- the Start bug
that was fixed and that must not be re-created on the replan path) with a
pre-AUTO baseline plus a bounded poll that proves progression POSITIVELY and only
fails on a definitive condition or after the FULL configured deadline.

It knows nothing about Start/Resume/Replan specifically. The caller supplies a
ProgressionContext -- how to read a fresh snapshot, the expected mission
identity, how to resolve the current target, the timing params, and the
clock/sleep hooks -- and calls capture_baseline() then watch(baseline,
timeout_s). Every success path still requires armed==true, mode==AUTO,
LOCAL_AGENT authority, and an unchanged expected mission identity; RUNNING is
then proven by one of:

    A. explicit MAVLink-derived mission-active evidence (ACTIVE_TRUE)
    B. mission sequence advancing beyond the captured baseline
    C. fresh meaningful movement (preferring decreasing distance to the target)

Definitive immediate-failure conditions (disarm, mode leaving AUTO after it was
verified, authority loss, mission-identity change) end the watch before the
deadline; a transient unavailable/stale sample or a bare ACTIVE_UNKNOWN /
ACTIVE_FALSE_EXPLICIT is retried, never treated as an immediate failure.
"""
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import autonomy_gate
import geo
import planning_package

# ── Three-valued MAVLink-derived running evidence (task's semantics) ───────────
# Absent evidence is ACTIVE_UNKNOWN, never collapsed to false.
ACTIVE_TRUE = "ACTIVE_TRUE"
ACTIVE_FALSE_EXPLICIT = "ACTIVE_FALSE_EXPLICIT"
ACTIVE_UNKNOWN = "ACTIVE_UNKNOWN"


@dataclass
class ProgressionContext:
    """Everything the verifier needs, with no knowledge of which lifecycle is
    calling it. `read_snapshot` returns a fresh decision snapshot (or None when a
    read fails -- treated as UNKNOWN, never as a value). `target_for_sequence`
    maps a Pixhawk sequence to the (lat, lon) of the currently-selected target
    for movement proof C (or None when it cannot be resolved).
    `expected_mission_id` is the Operator/package mission identity that must not
    change under us (None disables the identity gate -- e.g. the bench, where the
    MAVLink mission carries no Operator id)."""
    read_snapshot: Callable[[], Any]
    target_for_sequence: Callable[[Optional[int]], Optional[Tuple[float, float]]]
    expected_mission_id: Optional[str]
    poll_interval_s: float
    min_displacement_m: float
    max_position_age_s: float
    clock: Callable[[], float]
    sleep: Callable[[float], None]


def route_target_lookup(route: List[dict]) -> Callable[[Optional[int]], Optional[Tuple[float, float]]]:
    """Build a target_for_sequence over an explicit route list. Pixhawk item 0 is
    Home and route execution starts at item 1, so a Pixhawk sequence maps to
    route index (seq - 1). Used by the replan path (revised route) and mirrors
    mission_execution_controller._current_target for the original mission."""
    route = route or []

    def lookup(seq: Optional[int]) -> Optional[Tuple[float, float]]:
        if not isinstance(seq, int) or seq < 1:
            return None
        idx = seq - 1
        if 0 <= idx < len(route):
            wp = route[idx]
            lat, lon = wp.get("latitude"), wp.get("longitude")
            if lat is not None and lon is not None:
                return (lat, lon)
        return None

    return lookup


def _position_fresh(ctx: ProgressionContext, snap) -> bool:
    if snap is None or snap.latitude is None or snap.longitude is None:
        return False
    if snap.latitude == 0.0 and snap.longitude == 0.0:
        return False
    age = snap.position_age_s
    return age is not None and age <= ctx.max_position_age_s


def _mission_evidence_fresh(ctx: ProgressionContext, snap) -> bool:
    """Whether snap's mission_active_evidence is fresh enough to be used as
    POSITIVE proof A of NEW progression (root-cause fix: "an ancient cached
    ACTIVE_TRUE must never prove a new Start"). Reuses ctx.max_position_age_s
    as the freshness bound (the same "how stale is too stale for fresh
    evidence" figure already used for position/movement proof C -- one shared
    freshness policy, not a second invented threshold).

    An UNKNOWN age (the source never reported one -- every existing
    caller/fixture that predates this field, or any real Flask response older
    than this fix) is UNPROVABLE, and unprovable freshness must never be
    treated as proven fresh -- exactly the DecisionSnapshot.mission_active_
    evidence_age_s docstring's own rule ("None is 'freshness unprovable',
    never 'assume fresh' nor 'assume stale'"). Only a KNOWN age within the
    bound counts as proof A here.

    This is scoped to proof A alone: it does NOT collapse
    mission_active_evidence itself to false, does not touch ACTIVE_UNKNOWN's
    three-valued handling elsewhere in this module, and does not turn an
    unknown age into an immediate failure -- B (sequence) and C (displacement)
    remain fully available for the rest of the configured deadline exactly as
    if evidence A were simply absent."""
    age = getattr(snap, "mission_active_evidence_age_s", None)
    return age is not None and age <= ctx.max_position_age_s


def capture_baseline(ctx: ProgressionContext) -> Dict[str, Any]:
    """Capture the pre-AUTO progression baseline immediately before AUTO, so
    sequence/movement progress is measured against a fixed reference."""
    snap = ctx.read_snapshot()
    return {
        "baseline_sequence": None if snap is None else snap.current_sequence,
        "baseline_position": {
            "latitude": None if snap is None else snap.latitude,
            "longitude": None if snap is None else snap.longitude,
        },
        "baseline_position_timestamp": round(ctx.clock(), 3),
        "baseline_position_age_s": None if snap is None else snap.position_age_s,
        "baseline_groundspeed": None if snap is None else snap.groundspeed,
        "baseline_mission_active": None if snap is None else snap.mission_active,
        "baseline_mission_state": None if snap is None else snap.mission_active_evidence,
    }


def _immediate_failure(ctx: ProgressionContext, snap, authority, authority_local):
    """Definitive conditions that end the watch before the deadline. Returns
    (code, message) or None. A transient unavailable/stale sample or a bare
    mission_active=false is NOT one of these (handled by the caller)."""
    # Vehicle became disarmed.
    if snap.armed is False:
        return ("VEHICLE_DISARMED",
                "vehicle became disarmed during launch; restoring LOITER (not auto-disarming)")
    # Mode left AUTO after AUTO was verified (RC/failsafe/operator). mode_name
    # None is 'unavailable', not 'left AUTO'.
    if snap.mode_name is not None and snap.mode_name != "AUTO":
        intervention = "RC_OVERRIDE" if authority_local else "OPERATOR_ABORT"
        return ("MODE_LEFT_AUTO",
                f"mode left AUTO (now {snap.mode_name}) after AUTO was verified "
                f"[{intervention}]; restoring LOITER")
    # Authority lost (operator take control). None authority is 'unavailable'.
    if authority is not None and not authority_local:
        return ("AUTHORITY_LOST",
                f"LOCAL_AGENT authority lost (now {authority}) during launch "
                "[OPERATOR_ABORT]; restoring LOITER")
    # Mission identity changed under us. snap.mission_id is vehicle_state.
    # mission.current_mission_id -- Flask's legacy /start_mission operator
    # sensor-logging label when it is in THAT format (see planning_package.
    # is_legacy_operator_mission_label), a DIFFERENT identifier namespace
    # from the canonical msn-* mission identity this watch is proving and
    # therefore never valid evidence of a genuine identity change (mission
    # binding/reproof identity bug root cause). Any OTHER non-null value --
    # including a genuinely different canonical id -- still fails closed here
    # exactly as before this fix.
    if (snap.mission_id is not None and ctx.expected_mission_id is not None
            and snap.mission_id != ctx.expected_mission_id
            and not planning_package.is_legacy_operator_mission_label(snap.mission_id)):
        return ("MISSION_IDENTITY_CHANGED",
                f"vehicle mission identity changed to {snap.mission_id!r} "
                f"(expected {ctx.expected_mission_id!r}) during launch; restoring LOITER")
    return None


def _predicate_breakdown(ctx: ProgressionContext, snap, authority_local, bl_seq,
                         dist_moved, dist_to_target, bl_dist_to_target) -> Dict[str, bool]:
    """The individual named predicates behind the A/B/C proof gate (task:
    instrument progression samples with the final individual predicates) --
    the ONE place this logic lives; _positive_proof below and watch()'s
    per-sample diagnostics both call this, so the accept/reject DECISION and
    what gets logged about it can never drift apart."""
    gates_ok = bool(snap.armed is True and snap.mode_name == "AUTO" and authority_local
                    and (snap.mission_id is None or snap.mission_id == ctx.expected_mission_id
                         or ctx.expected_mission_id is None
                         or planning_package.is_legacy_operator_mission_label(snap.mission_id)))
    mission_active_proven = bool(
        gates_ok and snap.mission_active_evidence == ACTIVE_TRUE
        and _mission_evidence_fresh(ctx, snap))
    sequence_advanced = bool(
        gates_ok and isinstance(snap.current_sequence, int) and isinstance(bl_seq, int)
        and snap.current_sequence > bl_seq)
    displacement_proven = bool(
        gates_ok and _position_fresh(ctx, snap) and dist_moved is not None
        and dist_moved >= ctx.min_displacement_m)
    target_progress_proven = bool(
        displacement_proven and dist_to_target is not None and bl_dist_to_target is not None
        and dist_to_target < bl_dist_to_target)
    return {
        "mode_auto_proven": gates_ok,
        "mission_active_proven": mission_active_proven,
        "sequence_advanced": sequence_advanced,
        "displacement_proven": displacement_proven,
        "target_progress_proven": target_progress_proven,
    }


def _positive_proof(ctx: ProgressionContext, snap, authority_local, bl_seq,
                    dist_moved, dist_to_target, bl_dist_to_target):
    """The proof gate: RUNNING is accepted only while armed==true, mode==AUTO,
    authority==LOCAL_AGENT and mission identity is unchanged, PLUS at least one
    defensible positive signal (A mission_active true AND fresh / B sequence
    advanced / C fresh meaningful movement toward the target). Returns
    'A'/'B'/'C' or None. See _predicate_breakdown for the individual gates."""
    pred = _predicate_breakdown(ctx, snap, authority_local, bl_seq,
                                dist_moved, dist_to_target, bl_dist_to_target)
    if not pred["mode_auto_proven"]:
        return None
    # A: explicit MAVLink-derived mission-active evidence became true (never the
    #    operator-lifecycle flag, which is UNKNOWN for the agent path) AND is
    #    itself fresh (root-cause fix: an ancient cached ACTIVE_TRUE must never
    #    prove a NEW Start -- see _mission_evidence_fresh).
    if pred["mission_active_proven"]:
        return "A"
    # B: current sequence advanced beyond the pre-AUTO baseline. Merely seeing
    #    sequence 1 when 1 was already selected is NOT progression.
    if pred["sequence_advanced"]:
        return "B"
    # C: fresh meaningful movement, preferring reduced distance to target.
    if pred["displacement_proven"]:
        if dist_to_target is None or bl_dist_to_target is None:
            return "C"  # no target resolvable -- conservative displacement only
        # Require movement TOWARD the target, not arbitrary drift.
        if dist_to_target < bl_dist_to_target:
            return "C"
    return None


def watch(ctx: ProgressionContext, baseline: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    """Poll fresh progression evidence until a positive proof appears, a
    definitive immediate-failure condition occurs, or the FULL configured
    deadline elapses. Never exits early on a single inactive/unavailable sample.
    Runs on an operation worker (never a main loop); uses ctx.clock/ctx.sleep so
    tests exercise the whole deadline deterministically. Returns a rich evidence
    dict either way."""
    start = ctx.clock()
    deadline = start + timeout_s
    poll = ctx.poll_interval_s
    bl_seq = baseline.get("baseline_sequence")
    bl_pos = baseline.get("baseline_position") or {}
    bl_lat, bl_lon = bl_pos.get("latitude"), bl_pos.get("longitude")

    samples: List[Dict[str, Any]] = []
    evidence_seen: List[str] = []
    max_groundspeed = 0.0
    max_distance = 0.0
    pred: Optional[Dict[str, bool]] = None
    final = {"mode_name": None, "armed": None, "authority": None,
             "sequence": None, "position": {"latitude": None, "longitude": None}}

    def evidence(proven, proof, code=None, message=None, final_predicates=None):
        return {
            "proven": proven, "proof": proof,
            "failure_code": code, "failure_message": message,
            "configured_timeout_s": timeout_s,
            "actual_elapsed_s": round(ctx.clock() - start, 3),
            "sample_count": len(samples),
            "baseline": baseline,
            "final_sequence": final["sequence"],
            "final_position": final["position"],
            "final_mode": final["mode_name"],
            "final_armed": final["armed"],
            "authority": final["authority"],
            "max_groundspeed": round(max_groundspeed, 3),
            "max_distance_moved_m": round(max_distance, 3),
            "mission_active_evidence_observed": sorted(set(evidence_seen)),
            # The individual predicate breakdown (task: "final individual
            # predicates") behind THIS outcome -- the same dict the deciding
            # (or, on PROGRESSION_UNCONFIRMED, the very last) sample carried.
            "final_predicates": final_predicates,
            "samples": samples[-60:],
            # For legacy callers/tests that still read the old shape.
            "mode_name": final["mode_name"],
            "mission_active": None,
            "current_waypoint": final["sequence"],
        }

    while True:
        now = ctx.clock()
        elapsed = round(now - start, 3)
        snap = ctx.read_snapshot()
        read_ok = snap is not None

        authority = None if snap is None else snap.control_authority
        authority_local = False
        if authority is not None:
            authority_local, _ = autonomy_gate.check_autonomous_write_authority(authority)

        dist_moved = None
        dist_to_target = None
        bl_dist_to_target = None
        if snap is not None and snap.latitude is not None and snap.longitude is not None:
            if bl_lat is not None and bl_lon is not None:
                dist_moved = geo.haversine_m(bl_lat, bl_lon, snap.latitude, snap.longitude)
                max_distance = max(max_distance, dist_moved)
            target = ctx.target_for_sequence(snap.current_sequence)
            if target is not None:
                dist_to_target = geo.haversine_m(snap.latitude, snap.longitude, target[0], target[1])
                if bl_lat is not None and bl_lon is not None:
                    bl_dist_to_target = geo.haversine_m(bl_lat, bl_lon, target[0], target[1])
        if snap is not None and isinstance(snap.groundspeed, (int, float)):
            max_groundspeed = max(max_groundspeed, float(snap.groundspeed))
        if snap is not None and snap.mission_active_evidence:
            evidence_seen.append(snap.mission_active_evidence)

        # Individual named predicates (task: instrument progression samples
        # with mode/mission-evidence freshness and the final individual
        # predicates, for E2 evidence) -- computed unconditionally (even on a
        # failed read, all False) so every sample -- not just the accepted one
        # -- carries the SAME breakdown _positive_proof itself decides from.
        pred = (_predicate_breakdown(ctx, snap, authority_local, bl_seq,
                                     dist_moved, dist_to_target, bl_dist_to_target)
               if snap is not None else {
                   "mode_auto_proven": False, "mission_active_proven": False,
                   "sequence_advanced": False, "displacement_proven": False,
                   "target_progress_proven": False,
               })
        mission_evidence_age_s = None if snap is None else getattr(
            snap, "mission_active_evidence_age_s", None)
        sample = {
            "elapsed_s": elapsed,
            "read_ok": read_ok,
            "armed": None if snap is None else snap.armed,
            "armed_source": "vehicle_state.telemetry",
            "mode_name": None if snap is None else snap.mode_name,
            "mode_source": "vehicle_state.telemetry",
            # Mode/heartbeat freshness (task: "mode freshness") -- the SAME
            # telemetry-age figure ARM/position freshness already use
            # (snap.telemetry_age_s), never a second, differently-sourced age.
            "mode_age_s": None if snap is None else getattr(snap, "telemetry_age_s", None),
            "mission_active_raw": None if snap is None else snap.mission_active,
            "mission_active_source": "vehicle_state.mission",
            "mission_active_evidence": None if snap is None else snap.mission_active_evidence,
            # Freshness/age of the raw MISSION_CURRENT.mission_state observation
            # mission_active_evidence came from (root-cause fix -- see
            # _mission_evidence_fresh) -- None when the source never reported one.
            "mission_active_evidence_age_s": mission_evidence_age_s,
            "mission_state": None if snap is None else snap.mission_active_evidence,
            "current_sequence": None if snap is None else snap.current_sequence,
            "baseline_sequence": bl_seq,
            "mission_count": None if snap is None else snap.mission_count,
            "groundspeed": None if snap is None else snap.groundspeed,
            "latitude": None if snap is None else snap.latitude,
            "longitude": None if snap is None else snap.longitude,
            "position_age_s": None if snap is None else snap.position_age_s,
            "distance_moved_m": None if dist_moved is None else round(dist_moved, 3),
            "distance_to_target_m": None if dist_to_target is None else round(dist_to_target, 3),
            "authority": authority,
            "mission_id": None if snap is None else snap.mission_id,
            # Final individual predicates (task: instrument progression
            # samples) -- the exact breakdown _positive_proof itself decided
            # from for THIS sample, not just the eventual accept/reject.
            "mode_auto_proven": pred["mode_auto_proven"],
            "mission_active_proven": pred["mission_active_proven"],
            "sequence_advanced": pred["sequence_advanced"],
            "displacement_proven": pred["displacement_proven"],
            "target_progress_proven": pred["target_progress_proven"],
        }
        samples.append(sample)
        if read_ok:
            final = {"mode_name": snap.mode_name, "armed": snap.armed, "authority": authority,
                     "sequence": snap.current_sequence,
                     "position": {"latitude": snap.latitude, "longitude": snap.longitude}}

        # ── Definitive immediate-failure conditions (only on a fresh, readable
        #    sample -- a transient unavailable sample is never a failure by
        #    itself). ──
        if read_ok:
            imm = _immediate_failure(ctx, snap, authority, authority_local)
            if imm is not None:
                code, message = imm
                return evidence(False, None, code, message, final_predicates=pred)

            # ── Positive progression proof: ALL gates + one of A/B/C. ──
            proof = _positive_proof(ctx, snap, authority_local, bl_seq,
                                    dist_moved, dist_to_target, bl_dist_to_target)
            if proof is not None:
                return evidence(True, proof, final_predicates=pred)

        if ctx.clock() >= deadline:
            break
        ctx.sleep(poll)

    return evidence(False, None, "PROGRESSION_UNCONFIRMED",
                    f"mission progression not confirmed within the full "
                    f"{timeout_s:.1f}s deadline; restoring LOITER",
                    final_predicates=pred)
