"""
The immutable, time-consistent decision snapshot every replanning decision is
evaluated against.

A snapshot is taken once per decision from the observations local_agent.py has
already gathered that iteration (the GET /agent/state response, the Local
Agent's own comm state and control-authority read, and the persisted approved
planning package). Once built it is frozen: the energy policy, the controller,
and the mission-revision record all read the SAME values, so a decision can
never be made against half-updated telemetry.

Design rules honoured here:
  * Unavailable battery is None, never 0 -- see decision_engine._normalize_
    battery_percent (reused verbatim, not re-implemented). A None battery is
    "unknown", and the energy policy excludes it from the feasibility estimate.
  * Every field is a real read or an explicit None -- nothing is invented.
  * The safe-return DISTANCE estimate is conservative (a retrace of the
    approved outbound path when the package is present, straight-line to Home
    only as a floor when it is not) -- it must never underestimate the cost of
    getting home.
"""
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

import geo
from decision_engine import _normalize_battery_percent


@dataclass(frozen=True)
class DecisionSnapshot:
    snapshot_id: str
    created_at: float
    vehicle_id: Optional[str]

    # Position / motion
    latitude: Optional[float]
    longitude: Optional[float]
    position_age_s: Optional[float]
    heading: Optional[float]
    groundspeed: Optional[float]

    # Vehicle mode / arm
    mode: Optional[int]
    mode_name: Optional[str]
    armed: Optional[bool]

    # Energy
    battery_percent: Optional[float]     # normalized: None when unavailable
    battery_valid: bool
    battery_raw: Any
    battery_voltage: Optional[float]
    battery_current: Optional[float]

    # Mission identity / progress
    mission_id: Optional[str]
    mission_hash: Optional[str]
    mission_revision: Optional[int]
    current_sequence: Optional[int]
    mission_count: Optional[int]
    mission_active: Optional[bool]
    # Three-valued MAVLink-derived running evidence: ACTIVE_TRUE /
    # ACTIVE_FALSE_EXPLICIT / ACTIVE_UNKNOWN. Absent evidence is ACTIVE_UNKNOWN,
    # never collapsed to false (task's three-valued mission-active semantics).
    # Distinct from `mission_active`, which is only the operator-lifecycle flag.
    # Its freshness/age is a separate, DEFAULTED field near the bottom of this
    # dataclass (mission_active_evidence_age_s -- dataclass field-ordering
    # requires every defaulted field to come after every non-defaulted one).
    mission_active_evidence: Optional[str]
    mission_progress: Optional[str]

    # Home
    home_latitude: Optional[float]
    home_longitude: Optional[float]
    home_valid: bool
    distance_to_home_m: Optional[float]

    # Distances (metres)
    estimated_remaining_survey_distance_m: Optional[float]
    estimated_safe_return_distance_m: Optional[float]

    # Link / comms
    communication_state: Optional[str]
    telemetry_age_s: Optional[float]

    # Authority
    control_authority: Optional[str]
    authority_age_s: Optional[float]

    # Obstacle groundwork (placeholder -- see obstacle_model.py; never drives a
    # decision in this phase, only carried so a future REPLAN_OBSTACLE path has
    # a home for its evidence).
    obstacle_summary: Optional[Dict[str, Any]] = None

    # Deterministic experiment overrides active when this snapshot was taken
    # (source-tagged SIMULATED). None when no injection is active.
    active_experiment_overrides: Optional[Dict[str, Any]] = None

    # The transition id of the transaction this snapshot is bound to, if any.
    active_transition_id: Optional[str] = None

    # Age (seconds) of the raw MISSION_CURRENT.mission_state observation
    # mission_active_evidence (above) was derived from (services/agent_state.py's
    # mission_active_evidence_age_s), or None when unavailable/unreported by the
    # Flask side (every caller that predates this field, and any DecisionSnapshot
    # built without wiring it through, gets this default -- never a manufactured
    # negative). None is "freshness unprovable", never "assume fresh" nor "assume
    # stale" -- see mission_progression.py's freshness gate, the one place this is
    # actually consulted, to decide whether an ACTIVE_TRUE sample may prove
    # progression (an ancient cached ACTIVE_TRUE must never prove a brand-new
    # Start).
    mission_active_evidence_age_s: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _retrace_points(package_route: List[dict], current_seq: Optional[int],
                    pos: Optional[Tuple[float, float]],
                    home: Optional[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """
    The conservative safe-return polyline used only for a DISTANCE estimate
    here: current position -> the already-approved outbound waypoints traversed
    so far, in reverse -> Home. This mirrors (but does not import) the
    safe_return_planner's primary strategy; it is intentionally a superset of
    the straight line, so the estimate is never optimistic. Returns [] when
    neither position nor home is known.

    KNOWN LIMITATION (kept as-is; energy_policy.py's existing, already-tested
    return-energy trigger consumes this figure unmodified -- do not repurpose
    it): `current_seq` is treated here as "how much of package_route has
    already been physically flown", but it is really just Pixhawk's currently-
    targeted item. Whenever position and route progress are NOT correlated --
    pre-Start, or a bench rig where the vehicle sits somewhere the planned
    route never visited -- `package_route[:current_seq]` pulls in PLANNED-
    MISSION waypoints that were never actually traversed, synthesising a
    fictitious outbound-and-back leg through them before reaching `home`. This
    is the exact root cause traced in mission_feasibility.py's module
    docstring (a ~3.2 km "safe return" reported while the vehicle sat ~5 m
    from its verified Home). `home` itself is resolved correctly by
    build_snapshot below (verified Pixhawk Home preferred); the distance this
    function produces is not. mission_feasibility.py's RTL dimension
    deliberately does NOT use estimated_safe_return_distance_m for this
    reason -- it computes its own straight-line current-position -> verified-
    Home distance instead (RTL_METHOD_STRAIGHT_LINE). Fixing this function
    properly (e.g. only trusting `current_seq` as "traversed" when telemetry
    shows the vehicle actually progressing along package_route) is future
    work, out of scope for the Home-semantics correction that added this note.
    """
    pts: List[Tuple[float, float]] = []
    if pos is not None:
        pts.append(pos)
    if package_route and isinstance(current_seq, int) and current_seq > 0:
        traversed = package_route[:current_seq]
        for wp in reversed(traversed):
            lat, lon = wp.get("latitude"), wp.get("longitude")
            if lat is not None and lon is not None:
                pts.append((lat, lon))
    if home is not None:
        pts.append(home)
    return pts


def build_snapshot(
    vehicle_state: dict,
    comm_state: Optional[str],
    control_authority: Optional[str],
    planning_package: Optional[dict] = None,
    experiment_overrides: Optional[dict] = None,
    active_transition_id: Optional[str] = None,
    authority_age_s: Optional[float] = None,
    now: Optional[float] = None,
) -> DecisionSnapshot:
    """
    Build an immutable snapshot from this iteration's observations. Never
    raises: any missing sub-structure degrades to None rather than failing the
    decision. `planning_package` is the persisted approved package (see
    planning_package.py) -- when present, its original route and Home refine the
    return-distance estimate; when absent, only the straight-line-to-Home floor
    is available.
    """
    now = time.time() if now is None else now
    telemetry = vehicle_state.get("telemetry", {}) or {}
    mavlink = vehicle_state.get("mavlink", {}) or {}
    vs_mission = vehicle_state.get("mission", {}) or {}
    agent = vehicle_state.get("agent", {}) or {}
    home_status = agent.get("home_status") or {}

    lat = telemetry.get("lat")
    lon = telemetry.get("lng")
    pos = (lat, lon) if lat is not None and lon is not None else None

    raw_battery = telemetry.get("battery")
    battery_percent = _normalize_battery_percent(raw_battery)

    # Home: prefer the verified Pixhawk Home reported in home_status; fall back
    # to the planning package's stored Home. home_valid is the runtime-verified
    # latch, never merely "coordinates look plausible".
    hp = home_status.get("home_position") or {}
    home_lat = hp.get("latitude")
    home_lon = hp.get("longitude")
    home_valid = bool(home_status.get("verified"))
    if (home_lat is None or home_lon is None) and planning_package:
        pkg_home = planning_package.get("home") or {}
        home_lat = pkg_home.get("latitude")
        home_lon = pkg_home.get("longitude")
    home = (home_lat, home_lon) if home_lat is not None and home_lon is not None else None

    distance_to_home = (
        geo.haversine_m(pos[0], pos[1], home[0], home[1])
        if pos is not None and home is not None else None
    )

    package_route = (planning_package or {}).get("route") or []
    current_seq = vs_mission.get("current_waypoint")
    count = vs_mission.get("mission_count")

    retrace = _retrace_points(package_route, current_seq, pos, home)
    safe_return_distance = geo.path_length_m(retrace) if len(retrace) >= 2 else distance_to_home

    remaining_survey_distance = None
    if package_route and isinstance(current_seq, int):
        remaining = [
            (wp.get("latitude"), wp.get("longitude"))
            for wp in package_route[current_seq:]
            if wp.get("latitude") is not None and wp.get("longitude") is not None
        ]
        if pos is not None and remaining:
            remaining = [pos] + remaining
        if len(remaining) >= 2:
            remaining_survey_distance = geo.path_length_m(remaining)

    progress = (
        f"{current_seq}/{count}" if current_seq is not None and count is not None else None
    )

    return DecisionSnapshot(
        snapshot_id=uuid.uuid4().hex,
        created_at=round(now, 3),
        vehicle_id=vehicle_state.get("usv_id") or (planning_package or {}).get("usv_id"),
        latitude=lat,
        longitude=lon,
        position_age_s=mavlink.get("last_message_age_s") or mavlink.get("mavlink_last_msg_age_s"),
        heading=telemetry.get("heading"),
        groundspeed=telemetry.get("groundspeed"),
        mode=telemetry.get("mode"),
        mode_name=telemetry.get("mode_name"),
        armed=telemetry.get("armed"),
        battery_percent=battery_percent,
        battery_valid=battery_percent is not None,
        battery_raw=raw_battery,
        battery_voltage=telemetry.get("battery_voltage"),
        battery_current=telemetry.get("battery_current"),
        mission_id=vs_mission.get("current_mission_id"),
        mission_hash=(planning_package or {}).get("original_route_hash"),
        mission_revision=(planning_package or {}).get("revision"),
        current_sequence=current_seq,
        mission_count=count,
        mission_active=vs_mission.get("mission_active"),
        # Prefer the explicit MAVLink-derived three-valued evidence when the
        # vehicle state carries it; otherwise UNKNOWN (never fabricated false).
        mission_active_evidence=vs_mission.get("mission_active_evidence") or "ACTIVE_UNKNOWN",
        mission_active_evidence_age_s=vs_mission.get("mission_active_evidence_age_s"),
        mission_progress=progress,
        home_latitude=home_lat,
        home_longitude=home_lon,
        home_valid=home_valid,
        distance_to_home_m=None if distance_to_home is None else round(distance_to_home, 1),
        estimated_remaining_survey_distance_m=(
            None if remaining_survey_distance is None else round(remaining_survey_distance, 1)
        ),
        estimated_safe_return_distance_m=(
            None if safe_return_distance is None else round(safe_return_distance, 1)
        ),
        communication_state=comm_state,
        telemetry_age_s=mavlink.get("heartbeat_age_s"),
        control_authority=control_authority,
        authority_age_s=authority_age_s,
        obstacle_summary=None,
        active_experiment_overrides=experiment_overrides or None,
        active_transition_id=active_transition_id,
    )
