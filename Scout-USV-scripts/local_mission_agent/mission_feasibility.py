"""
Mission ENERGY FEASIBILITY -- deterministic, side-effect-free evidence for two
distinct questions, evaluated fresh from whatever evidence is currently
available:

    mission_feasible     -- "can the REMAINING OPERATOR-PLANNED mission still
                             be completed, on the current effective battery,
                             with the configured reserve held back?"
    rtl_return_feasible  -- "if I abandon the mission RIGHT NOW, can I safely
                             return from where I am to the CURRENT VERIFIED
                             Pixhawk / RTL Home, on the same terms?"

These are answered SEPARATELY, from the current position, not chained --
finishing the mission and getting back to the *current* RTL Home afterward are
two different trips, to two different destinations, and there is no reason to
expect them to agree.

TWO HOMES, NEVER ONE GENERIC "HOME"
------------------------------------
This module exists because an earlier version conflated two genuinely
different concepts under one implicit "Home", which produced a live evidence
contradiction on the bench: a mission planned for a lake ~1.6 km from the
garage, evaluated while Scout sat AT its current verified RTL Home in the
garage, reported a ~3204 m "return distance" -- a lake-scale number -- for
what should have been a ~5 m trip back to the garage. See the module's git
history / task write-up ("mission-energy-feasibility semantics") for the full
root-cause trace; the short version:

  * PLANNED MISSION HOME (`planned_home`) -- the Operator-authored Home in the
    approved planning package (planning_package.py's `home` field). It
    describes where the PLANNED MISSION was designed around. It may be far
    from Scout's current physical position (the lake mission in the bench
    scenario is a legitimate, intentional example: ~1.6 km from the garage).
    It plays NO PART in the RTL question.

  * RTL / SAFETY HOME (`rtl_home`) -- the CURRENT, VERIFIED Pixhawk Home (the
    live "Set Home Here" position: `decision_snapshot.DecisionSnapshot.
    home_valid` / `.home_latitude` / `.home_longitude`, sourced from the
    vehicle's own `agent.home_status`). It is where a real RTL abort would
    actually return the vehicle to, right now. It plays NO PART in the
    mission-completion question.

The old bug was not that the wrong Home coordinate was used for either
question -- decision_snapshot.py already resolves the verified Pixhawk Home
correctly when it prefers `home_status` over the planning package. The bug was
that the RETURN-distance estimate this module was fed (decision_snapshot's
`estimated_safe_return_distance_m`, a conservative RETRACE built as
`[current position] + reversed(planning_package["route"][:current_sequence])
+ [Home]`) treats route-index progress as if it were physically-travelled
distance. Pre-Start (or in a bench rig where GPS position and mission
progress are deliberately decoupled), `current_sequence` is just "the next
targeted waypoint", not "already flown" -- so the retrace synthesises a
fictitious outbound leg through the PLANNED MISSION's own waypoints (the lake)
before returning to the (correctly resolved) RTL Home, inflating the distance
by roughly double the outbound leg to the mission. `estimated_safe_return_
distance_m` is NOT renamed or touched here (energy_policy.py's existing,
already-tested return-energy trigger keeps consuming it exactly as before --
see energy_policy.py and decision_snapshot.py's own docstrings, which now
cross-reference this one). This module simply never uses that figure: the RTL
dimension below computes its own distance directly from `current_position` ->
`rtl_home`, so it can never be contaminated by planned-mission route geometry
again.

Route / planned-Home semantics (no double counting)
------------------------------------------------------
planned_completion_distance_m is "current position -> the rest of the
approved planning-package route, in order, stopping at its last waypoint" --
nothing more. planning_package.py's own documented contract (see its module
docstring, "Semantic segments") explicitly does NOT assume the route's last
waypoint is Home; a route that ends with an explicit RETURN-segment connector
back to planned_home is already included once, simply as the tail of the
route -- this module never appends a second "then go to planned_home" leg on
top of it (`mission_geometry_source` is tagged accordingly when the remaining
route's last point already coincides with planned_home within a small
tolerance, purely as provenance -- it changes no distance). If the route does
NOT end at planned_home, mission completion is judged purely on finishing the
route as planned; nothing in the planning-package contract requires an
Operator-authored mission to loop back to its own planned Home, so nothing is
invented here either.

ROUTE-IDENTITY SAFETY INVARIANT (task: mission-route-identity safety)
-----------------------------------------------------------------------
A live bug demonstrated that this module could confidently evaluate a STALE
planning-package route -- one Scout's own readiness proof already knows does
NOT match the route currently on the Pixhawk -- and publish a confident
FEASIBLE/INFEASIBLE verdict (with a real-looking distance and margin) computed
entirely from geometry that no longer describes the vehicle's actual mission.

The mission dimension may therefore only be evaluated when the route's
IDENTITY is CURRENT and proven, not merely when a route happens to be present.
`route_identity_verified` (an input, defaulting to True for pure-function
callers that do not care about this axis -- see `evaluate_mission_feasibility`
below) must be explicitly resolved to True by a caller that has real evidence
of a match; `evaluate_from_snapshot` is the one production adapter, and it
NEVER defaults to True on its own -- it always resolves the real value from
`mission_binding` (mission_execution_controller.status()["binding"]), which is
itself sourced from mission_execution_controller's own existing readiness
proof against a live Pixhawk readback (planning_package.build_readiness) --
reusing that proof, never a second, parallel hash comparison.

When the route identity is NOT proven (mismatched, stale, or simply never
proven yet), the mission dimension becomes UNKNOWN (REASON_PLANNING_PACKAGE_
STALE / REASON_MISSION_ROUTE_UNVERIFIED) -- never FEASIBLE, never INFEASIBLE,
and `planned_completion_distance_m` is never emitted as if it were authoritative
route geometry. This is a distinct axis from route AVAILABILITY (a missing
route is REASON_MISSION_UNAVAILABLE, unaffected by this invariant) and, by
construction (the RTL dimension below never reads `mission_route` at all), it
never touches `rtl_return_feasible` -- a stale MISSION route must never make a
perfectly good RTL answer disappear.

Boundary rule (energy model, task: physically-grounded capacity model)
-----------------------------------------------------------------------
This module estimates energy cost from a conservative capacity/current/time
model, NOT a learned or coulomb-counting battery model and NOT (as an earlier
version did) an opaque distance-over-an-arbitrary-usable-range abstraction:

    route_time_h            = route_distance_m / design_speed_mps / 3600
    required_capacity_Ah    = conservative_current_A * route_time_h
    available_capacity_Ah   = nominal_capacity_Ah * usable_capacity_factor
                               * effective_battery_percent / 100
    reserve_capacity_Ah     = nominal_capacity_Ah * reserve_fraction
    capacity_margin_Ah      = available_capacity_Ah - required_capacity_Ah
                               - reserve_capacity_Ah
    margin_percent          = capacity_margin_Ah / nominal_capacity_Ah * 100

    feasible  iff margin_Ah >  0   (strictly positive)
    infeasible iff margin_Ah <= 0  (exactly zero counts as infeasible: zero
                                     slack is not a margin -- energy_policy.py's
                                     own CODE_MARGIN_NON_POSITIVE trigger uses
                                     the same "<= 0 is not fine" boundary for
                                     the identical reason).

This is deliberately NOT a high-fidelity electrochemical battery model. It is
deterministic, conservative, interpretable, and experimentally calibratable --
suitable for supervisory mission feasibility, not for precision endurance
prediction. `design_speed_mps` is an explicit, centrally-configured design
parameter (task section 7/9) because Scout carries no authoritative mission
speed today; it is NEVER inferred from instantaneous groundspeed. The same
model is applied independently to the remaining mission and to the RTL return
-- two distinct distances, the SAME available capacity (both draw from the
same battery, at the same instant), never chained.

`available_capacity_Ah` depends only on the effective battery percent and the
configured capacity parameters -- identical for both dimensions -- so it is
computed once and exposed once, not duplicated per dimension.
`estimated_mission_duration_h` / `estimated_mission_capacity_Ah` and
`estimated_rtl_duration_h` / `estimated_rtl_capacity_Ah` are the per-dimension
quantities that DO differ (different distances).

TWO DISTINCT RESERVES (task: RTL-reserve semantics correction)
-----------------------------------------------------------------------
`reserve_capacity_Ah`, unlike `available_capacity_Ah`, is NOT shared between
the two dimensions -- each has its OWN reserve, `mission_reserve_capacity_Ah`
(from `mission_reserve_fraction`) and `rtl_reserve_capacity_Ah` (from
`rtl_reserve_fraction`), because the two questions carry genuinely different
uncertainty:

  * The MISSION reserve buffers an open-ended set of unknowns proper to a
    mission that is still in progress and may need to do more before it is
    over: further replanning cycles, extra legs, holding/loitering overhead,
    payload/sensor load, imprecise remaining-route geometry. Conservative by
    design (default 0.15 / 15%, unchanged from before this correction).

  * The RTL reserve buffers only the uncertainty of ONE well-defined,
    immediate maneuver -- abandon the mission now and return to the current
    verified Home: the straight-line RTL-distance estimate under-representing
    the real course (current/heading-keeping), final station-keeping/docking
    near Home, and battery-percent sensor noise -- plus a non-zero floor
    against deep discharge. It does NOT need to buffer future mission
    unknowns, because by construction it is evaluated as if the mission were
    abandoned right now. Deliberately SMALLER than the mission reserve
    (default 0.05 / 5%, one third of the mission reserve).

Reusing the mission reserve for RTL (the pre-correction bug) made ANY RTL
effectively infeasible below `125 * mission_reserve_fraction` percent SOC
(18.75% at the 15% default) even when Home was metres away, because at zero
distance `margin_percent = usable_capacity_factor*100*battery/100 -
100*reserve_fraction` and a positive margin requires `battery > 100 *
reserve_fraction / usable_capacity_factor` -- with `usable_capacity_factor
=0.8` that is `battery > 125 * reserve_fraction`. Splitting the reserve moves
that same zero-distance floor to `125 * rtl_reserve_fraction` (~6.25% SOC at
the 5% default) for the RTL dimension specifically, while leaving the mission
dimension's own floor (still 18.75% at its unchanged 15% default) untouched --
each dimension now fails closed at a floor sized to its own uncertainty,
instead of a shared floor sized to the more conservative of the two.

critical_battery_percent (the existing hard floor override in energy_policy.py
that ignores margin and forces a return) is DELIBERATELY NOT reasoned about
here. That hard override is a separate, existing, already-tested mechanism
that continues to live in energy_policy.py unmodified, on its own distance/
usable-range-based model (see energy_policy.py's own docstring and this
module's section on that module, task section 18).

Three-valued status
--------------------
Missing or stale evidence is UNKNOWN, never collapsed into FEASIBLE. The
combined top-level `status`/`reason` describes BOTH dimensions, prioritising
the mission dimension (mission unknown/infeasible is reported before RTL
unknown/infeasible -- this is the exact order the Start gate uses: a Start
that cannot complete the planned mission is rejected for that reason even if
RTL happens to also be a problem; see mission_execution_controller._run_start
section 6/7). `mission_feasible` and `rtl_return_feasible` are independent
booleans, each always computed whenever ITS OWN inputs allow it, regardless of
the other dimension's outcome -- a mission that can no longer be completed may
still have a perfectly safe way back to the current RTL Home right now, and
that must remain visible (this is what a later risk/decision policy needs: a
"mission infeasible, RTL feasible" state calls for something very different
than "RTL itself is no longer feasible").

Contract (task section 2)
--------------------------
Pure / deterministic / side-effect-free: no Pixhawk command, no Operator call,
no mutation of any mission/authority/replan state, no disk I/O. Every input is
a value the caller already read this iteration; this module only computes.
"""
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import geo

# ── Three-valued overall status ────────────────────────────────────────────
STATUS_FEASIBLE = "FEASIBLE"
STATUS_INFEASIBLE = "INFEASIBLE"
STATUS_UNKNOWN = "UNKNOWN"

# ── Reason codes (stable, machine-readable) ────────────────────────────────
REASON_SUFFICIENT_ENERGY = "SUFFICIENT_ENERGY"
REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION = "INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION"
REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN = "INSUFFICIENT_ENERGY_FOR_RTL_RETURN"
REASON_BATTERY_INVALID = "BATTERY_INVALID"
REASON_POSITION_STALE = "POSITION_STALE"
REASON_MISSION_UNAVAILABLE = "MISSION_UNAVAILABLE"
# Covers BOTH "no Pixhawk home_status at all" and "home_status present but not
# yet verified" -- either way the RTL question has no proven current Home to
# answer against, so it fails closed the same way (task: "missing/unverified
# RTL Home -> RTL feasibility UNKNOWN").
REASON_RTL_HOME_UNAVAILABLE = "RTL_HOME_UNAVAILABLE"
# Route-identity safety (task: mission-route-identity safety) -- the mission
# dimension has a route to look at, but that route's IDENTITY is not proven
# current, so its geometry is not trustworthy enough to judge feasibility.
# PLANNING_PACKAGE_STALE: a proof exists and shows a genuine mismatch (mirrors
# planning_package.READY_PACKAGE_STALE's own value verbatim -- reused, not
# reinvented). MISSION_ROUTE_UNVERIFIED: no proof exists yet either way.
REASON_PLANNING_PACKAGE_STALE = "PLANNING_PACKAGE_STALE"
REASON_MISSION_ROUTE_UNVERIFIED = "MISSION_ROUTE_UNVERIFIED"

# battery_source values.
SOURCE_PHYSICAL = "PHYSICAL"
SOURCE_INJECTED = "INJECTED"

# planned_home / rtl_home provenance tags (task section 7).
HOME_SOURCE_PLANNING_PACKAGE = "PLANNING_PACKAGE"
HOME_SOURCE_PIXHAWK_VERIFIED_HOME = "PIXHAWK_VERIFIED_HOME"

# mission_geometry_source values.
MISSION_METHOD_REMAINING_ROUTE = "CURRENT_POSITION_TO_REMAINING_ROUTE"
# Same calculation; tag only, so a caller/UI/recorder can tell that the last
# remaining waypoint already happens to sit at planned_home (i.e. no separate
# "then go to planned_home" leg was needed) without re-deriving that geometry.
MISSION_METHOD_REMAINING_ROUTE_ENDS_AT_PLANNED_HOME = (
    "CURRENT_POSITION_TO_REMAINING_ROUTE_ENDING_AT_PLANNED_HOME"
)
# A route point within this of planned_home counts as "the route already ends
# there" -- mirrors mission_execution_config.MissionExecutionConfig.
# home_verification_tolerance_m's default (5.0 m); this module has no config
# object of its own for a pure labelling heuristic that changes no distance.
PLANNED_HOME_COINCIDENCE_TOLERANCE_M = 5.0

# rtl_return_geometry_source values.
# The only geometry this module computes itself: current position -> the
# verified Pixhawk Home, direct. Deliberately NOT a replan/retrace/no-go-aware
# path -- see module docstring section 4's reasoning for why safe_return_
# planner.py (whose Home is the PLANNING PACKAGE Home, not necessarily the
# current verified RTL Home pre-Start-sync) is not reused here.
RTL_METHOD_STRAIGHT_LINE = "RTL_STRAIGHT_LINE_ESTIMATE"
# A caller may supply its own (e.g. an already-validated safe-return path
# length) via rtl_return_distance_m/rtl_return_geometry_source; this is the
# fallback label if it forgot to also supply a source string.
RTL_METHOD_CALLER_SUPPLIED = "CALLER_SUPPLIED_RTL_PATH"


def _valid_point(point: Optional[Tuple[float, float]]) -> bool:
    """A usable (latitude, longitude) pair -- neither the tuple itself nor
    either coordinate is None. Anything else (None, or a partial (lat, None)
    pair) counts as "no Home", the same way `home is None` already would --
    a caller must never be able to pass a half-known point and have it
    silently participate in a distance calculation."""
    return point is not None and point[0] is not None and point[1] is not None


def _home_dict(point: Optional[Tuple[float, float]], source: str) -> Optional[dict]:
    if not _valid_point(point):
        return None
    lat, lon = point
    return {"latitude": lat, "longitude": lon, "source": source}


@dataclass(frozen=True)
class MissionFeasibilityResult:
    status: str
    reason: str
    message: str

    # Energy evidence.
    battery_percent: Optional[float]          # effective/policy battery used
    battery_source: Optional[str]             # SOURCE_PHYSICAL / SOURCE_INJECTED / None
    physical_battery_percent: Optional[float]  # preserved verbatim, never fabricated
    injected_battery_percent: Optional[float]  # preserved verbatim; None when no injection

    # Route / progress evidence.
    current_sequence: Optional[int]
    remaining_waypoint_count: Optional[int]

    # Two distinct Home identities (task section 7) -- never blended.
    planned_home: Optional[dict]   # {latitude, longitude, source: PLANNING_PACKAGE} or None
    rtl_home: Optional[dict]       # {latitude, longitude, source: PIXHAWK_VERIFIED_HOME} or None

    # Two distinct distances, to two distinct destinations.
    planned_completion_distance_m: Optional[float]
    rtl_return_distance_m: Optional[float]

    # Route-identity safety evidence (task: mission-route-identity safety) --
    # whether the route used above is currently proven to match the live
    # Pixhawk route, and why not when it is not. None/None means the caller
    # did not wire this evidence at all (pure-function default; see
    # evaluate_mission_feasibility's own docstring).
    route_identity_verified: Optional[bool]
    route_identity_reason: Optional[str]

    mission_margin_percent: Optional[float]
    rtl_return_margin_percent: Optional[float]

    mission_feasible: Optional[bool]        # True / False / None (unknown)
    rtl_return_feasible: Optional[bool]     # True / False / None (unknown)

    mission_geometry_source: str
    rtl_return_geometry_source: Optional[str]

    # ── Physical capacity/current/time energy model (task: physically-
    #    grounded battery model) -- see module docstring's "Boundary rule"
    #    section for the exact equations. Config echoed for auditability.
    nominal_capacity_Ah: float
    usable_capacity_factor: float
    conservative_current_A: float
    design_speed_mps: float
    # Two distinct reserves (task: RTL-reserve semantics correction) -- see
    # module docstring's "TWO DISTINCT RESERVES" section.
    mission_reserve_fraction: float
    rtl_reserve_fraction: float

    estimated_mission_duration_h: Optional[float]
    estimated_rtl_duration_h: Optional[float]
    estimated_mission_capacity_Ah: Optional[float]
    estimated_rtl_capacity_Ah: Optional[float]

    # available_capacity_Ah is battery-derived, identical for both dimensions
    # (same battery, same instant) -- computed once, not duplicated per
    # dimension. The two reserves are NOT shared (see module docstring).
    available_capacity_Ah: Optional[float]
    mission_reserve_capacity_Ah: Optional[float]
    rtl_reserve_capacity_Ah: Optional[float]

    # Observed-current-state validation evidence ONLY (task section 17) --
    # NEVER an input to the energy model above, which always uses the
    # configured conservative_current_A. Sourced from DecisionSnapshot's own
    # battery_voltage/battery_current when available via evaluate_from_
    # snapshot; None when the pure function is called directly without them.
    measured_voltage_V: Optional[float]
    measured_current_A: Optional[float]

    evaluated_at: float
    position_age_s: Optional[float]
    max_position_age_s: float

    def to_dict(self) -> dict:
        return asdict(self)


def resolve_effective_battery(
    physical_battery_percent: Optional[float],
    injected_battery_percent: Optional[float],
) -> Tuple[Optional[float], Optional[str]]:
    """(effective_percent, source). An injected override always wins (tagged
    INJECTED) -- same precedence energy_policy.py already uses. An invalid
    physical reading is never coerced to 0; if there is no injection either,
    the effective value is None (unknown), not a fabricated number."""
    if injected_battery_percent is not None:
        return injected_battery_percent, SOURCE_INJECTED
    if physical_battery_percent is not None:
        return physical_battery_percent, SOURCE_PHYSICAL
    return None, None


def _remaining_route(
    mission_route: Optional[List[dict]], current_sequence: Optional[int],
) -> Tuple[List[dict], int]:
    """(remaining_waypoints, start_index). Pixhawk item 0 is Home and route
    execution starts at item 1, so sequence seq maps to route index seq-1 --
    the CURRENTLY TARGETED waypoint, included (mirrors
    mission_execution_controller._current_target /
    mission_progression.route_target_lookup). A sequence of None/0/negative
    (mission not yet started / at Home) means the WHOLE route is still ahead.
    A sequence at or beyond the route length means the mission is complete --
    no remaining waypoints."""
    route = mission_route or []
    if not isinstance(current_sequence, int) or current_sequence < 1:
        idx = 0
    else:
        idx = current_sequence - 1
    idx = max(0, min(idx, len(route)))
    return route[idx:], idx


def _route_points(remaining: List[dict]) -> List[Tuple[float, float]]:
    pts = []
    for wp in remaining:
        lat, lon = wp.get("latitude"), wp.get("longitude")
        if lat is not None and lon is not None:
            pts.append((lat, lon))
    return pts


def _path_distance_from(current_position: Tuple[float, float],
                        remaining_points: List[Tuple[float, float]]) -> float:
    if not remaining_points:
        return 0.0
    pts = [current_position] + remaining_points
    return geo.path_length_m(pts)


def _route_ends_at_planned_home(remaining_points: List[Tuple[float, float]],
                                planned_home: Optional[Tuple[float, float]]) -> bool:
    if not remaining_points or not _valid_point(planned_home):
        return False
    lat, lon = remaining_points[-1]
    plat, plon = planned_home
    return geo.haversine_m(lat, lon, plat, plon) <= PLANNED_HOME_COINCIDENCE_TOLERANCE_M


def _available_capacity_Ah(
    effective_battery_percent: float,
    nominal_capacity_Ah: float,
    usable_capacity_factor: float,
) -> float:
    """Battery-derived, the SAME for both the mission and RTL dimensions
    (see module docstring) -- computed once, not duplicated per dimension."""
    available = nominal_capacity_Ah * usable_capacity_factor * effective_battery_percent / 100.0
    return round(available, 4)


def _reserve_capacity_Ah(nominal_capacity_Ah: float, reserve_fraction: float) -> float:
    """A dimension's OWN reserve (see module docstring's "TWO DISTINCT
    RESERVES" section) -- callers pass mission_reserve_fraction or
    rtl_reserve_fraction, never one value shared between both."""
    return round(nominal_capacity_Ah * reserve_fraction, 4)


def _dimension_capacity(
    distance_m: Optional[float],
    design_speed_mps: float,
    conservative_current_A: float,
    available_capacity_Ah: float,
    reserve_capacity_Ah: float,
    nominal_capacity_Ah: float,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(duration_h, required_capacity_Ah, margin_percent) for ONE dimension
    (mission or RTL) -- see module docstring's "Boundary rule" section for the
    exact equations. None distance (no route / no verified RTL Home) or a
    non-positive design speed (misconfiguration) yields (None, None, None):
    this function is only ever called once the caller has already confirmed
    the distance is real evidence."""
    if distance_m is None or design_speed_mps is None or design_speed_mps <= 0:
        return None, None, None
    duration_h = distance_m / design_speed_mps / 3600.0
    required_Ah = conservative_current_A * duration_h
    margin_Ah = available_capacity_Ah - required_Ah - reserve_capacity_Ah
    margin_percent = (margin_Ah / nominal_capacity_Ah * 100.0) if nominal_capacity_Ah > 0 else None
    return (
        round(duration_h, 4),
        round(required_Ah, 4),
        round(margin_percent, 2) if margin_percent is not None else None,
    )


def _resolve_route_identity(
    package: Optional[dict], mission_binding: Optional[Dict[str, Any]],
) -> Tuple[Optional[bool], Optional[str]]:
    """(verified, reason) -- whether the CURRENT planning-package route is
    proven identical to the route mission_execution_controller's own
    readiness machinery last verified against a LIVE Pixhawk readback (task:
    mission-route-identity safety). Reuses that existing proof exactly --
    never a second, independent hash comparison.

    `mission_binding` is mission_execution_controller.status()["binding"] (or
    an equivalent shape): {package_route_hash, verified_route_hash, ...}.
    `verified_route_hash` is the route hash the controller's last COMPLETED
    readiness proof matched against a live Pixhawk readback; `package_route_
    hash` there is the CURRENT stored package's hash -- but this function
    re-derives the package hash itself from the SAME `package` dict this call
    is about to evaluate geometry from, so a package swapped out from under a
    slightly-stale binding is judged against what is actually being evaluated,
    not a second, potentially different, snapshot of the package.

    None `mission_binding` (the caller did not wire this evidence at all) ->
    (True, None): "not stated" defers to evaluate_mission_feasibility's own
    pure-function default, preserving legacy behaviour for callers that do
    not opt into this axis. Once wired, missing/mismatched verification ->
    NOT verified, never silently assumed fine."""
    if mission_binding is None:
        return True, None
    pkg_hash = (package or {}).get("route_hash") or (package or {}).get("original_route_hash")
    if pkg_hash is None:
        # No package route at all -- REASON_MISSION_UNAVAILABLE already covers
        # "no route"; route identity is moot.
        return True, None
    verified_hash = mission_binding.get("verified_route_hash")
    if verified_hash is not None and verified_hash == pkg_hash:
        return True, None
    if verified_hash is not None:
        return False, REASON_PLANNING_PACKAGE_STALE
    return False, REASON_MISSION_ROUTE_UNVERIFIED


def evaluate_mission_feasibility(
    *,
    current_position: Optional[Tuple[float, float]],
    position_age_s: Optional[float],
    mission_route: Optional[List[dict]],
    current_sequence: Optional[int],
    planned_home: Optional[Tuple[float, float]] = None,
    rtl_home: Optional[Tuple[float, float]] = None,
    physical_battery_percent: Optional[float],
    injected_battery_percent: Optional[float] = None,
    rtl_return_distance_m: Optional[float] = None,
    rtl_return_geometry_source: Optional[str] = None,
    nominal_capacity_Ah: float,
    conservative_current_A: float,
    design_speed_mps: float,
    usable_capacity_factor: float,
    mission_reserve_fraction: float,
    rtl_reserve_fraction: float,
    route_identity_verified: Optional[bool] = True,
    route_identity_reason: Optional[str] = None,
    measured_voltage_V: Optional[float] = None,
    measured_current_A: Optional[float] = None,
    max_position_age_s: float = 5.0,
    now: Optional[float] = None,
) -> MissionFeasibilityResult:
    """
    Pure, deterministic, side-effect-free (task section 2 / 17): no vehicle
    command, no Operator call, no mission/authority/replan mutation, no disk
    I/O. Every argument is evidence the caller already holds this iteration.

    `planned_home` is the Operator-planned mission Home (planning_package.py's
    `home`) -- used ONLY for provenance (`planned_home` in the result) and to
    label whether the remaining route already ends there. It never enters the
    RTL calculation.

    `rtl_home` is the CURRENT VERIFIED Pixhawk Home -- callers MUST only pass
    this when it is genuinely verified (see evaluate_from_snapshot, which
    gates it on `snapshot.home_valid`); passing an unverified or planning-
    package-fallback position here would silently reintroduce the exact
    conflation this module exists to prevent. When None, the RTL dimension is
    UNKNOWN (REASON_RTL_HOME_UNAVAILABLE), never assumed feasible.

    `rtl_return_distance_m` (optional) lets a caller supply a better-than-
    straight-line RTL path length (e.g. an already-validated safe-return
    route) -- pair it with `rtl_return_geometry_source` to label it. Left
    None (the normal case), this module computes the straight-line distance
    from `current_position` to `rtl_home` itself and labels it
    RTL_METHOD_STRAIGHT_LINE, so the RTL answer can never be contaminated by
    planned-mission route geometry.

    `route_identity_verified` (task: mission-route-identity safety) gates the
    MISSION dimension only -- see module docstring's "ROUTE-IDENTITY SAFETY
    INVARIANT" section. Defaults to True so pure-function callers that are not
    exercising this axis (most of this module's own unit tests) see unchanged
    behaviour; `evaluate_from_snapshot`, the one production adapter, NEVER
    relies on this default -- it always resolves the real value itself. Any
    value other than True (False, or None -- "cannot be proven either way")
    forces the mission dimension to UNKNOWN, with `route_identity_reason` (or
    REASON_MISSION_ROUTE_UNVERIFIED if none was supplied) as the reason; it
    never touches the RTL dimension, which does not read `mission_route`.

    `nominal_capacity_Ah` / `conservative_current_A` / `design_speed_mps` /
    `usable_capacity_factor` parameterise the capacity/current/time energy
    model -- see module docstring's "Boundary rule" section for the exact
    equations these drive. `mission_reserve_fraction` / `rtl_reserve_fraction`
    are the two DISTINCT reserves (module docstring's "TWO DISTINCT RESERVES"
    section) -- the mission dimension is judged against the former, the RTL
    dimension against the latter; never mixed.
    """
    now = time.time() if now is None else now
    effective_battery, battery_source = resolve_effective_battery(
        physical_battery_percent, injected_battery_percent)

    planned_home_dict = _home_dict(planned_home, HOME_SOURCE_PLANNING_PACKAGE)
    rtl_home_dict = _home_dict(rtl_home, HOME_SOURCE_PIXHAWK_VERIFIED_HOME)

    def _result(status, reason, message, *,
               remaining_wp_count=None, planned_dist=None,
               mission_duration=None, rtl_duration=None,
               mission_capacity=None, rtl_capacity=None,
               available_capacity=None,
               mission_reserve_capacity=None, rtl_reserve_capacity=None,
               mission_margin=None, rtl_margin=None,
               mission_feasible=None, rtl_feasible=None,
               rtl_dist=None, rtl_source=None,
               mission_source=MISSION_METHOD_REMAINING_ROUTE,
               route_verified=route_identity_verified, route_reason=None):
        return MissionFeasibilityResult(
            status=status, reason=reason, message=message,
            battery_percent=effective_battery, battery_source=battery_source,
            physical_battery_percent=physical_battery_percent,
            injected_battery_percent=injected_battery_percent,
            current_sequence=current_sequence,
            remaining_waypoint_count=remaining_wp_count,
            planned_home=planned_home_dict, rtl_home=rtl_home_dict,
            planned_completion_distance_m=planned_dist,
            rtl_return_distance_m=rtl_dist,
            route_identity_verified=route_verified,
            route_identity_reason=route_reason,
            mission_margin_percent=mission_margin,
            rtl_return_margin_percent=rtl_margin,
            mission_feasible=mission_feasible,
            rtl_return_feasible=rtl_feasible,
            mission_geometry_source=mission_source,
            rtl_return_geometry_source=rtl_source,
            nominal_capacity_Ah=nominal_capacity_Ah,
            usable_capacity_factor=usable_capacity_factor,
            conservative_current_A=conservative_current_A,
            design_speed_mps=design_speed_mps,
            mission_reserve_fraction=mission_reserve_fraction,
            rtl_reserve_fraction=rtl_reserve_fraction,
            estimated_mission_duration_h=mission_duration,
            estimated_rtl_duration_h=rtl_duration,
            estimated_mission_capacity_Ah=mission_capacity,
            estimated_rtl_capacity_Ah=rtl_capacity,
            available_capacity_Ah=available_capacity,
            mission_reserve_capacity_Ah=mission_reserve_capacity,
            rtl_reserve_capacity_Ah=rtl_reserve_capacity,
            measured_voltage_V=measured_voltage_V,
            measured_current_A=measured_current_A,
            evaluated_at=round(now, 3),
            position_age_s=position_age_s,
            max_position_age_s=max_position_age_s,
        )

    # ── Evidence gates, fail-closed, priority-ordered ──────────────────────
    if effective_battery is None:
        return _result(STATUS_UNKNOWN, REASON_BATTERY_INVALID,
                       "no valid battery reading (physical unavailable/out of range and no "
                       "simulated injection active) -- cannot assess energy feasibility.")

    position_fresh = (
        current_position is not None
        and position_age_s is not None
        and position_age_s <= max_position_age_s
    )
    if not position_fresh:
        return _result(STATUS_UNKNOWN, REASON_POSITION_STALE,
                       f"current position is missing or stale "
                       f"(age={position_age_s}, max={max_position_age_s}s) -- cannot compute "
                       "planned-completion or RTL-return distance from an unknown/stale start.")

    # Battery-derived available capacity -- identical for both dimensions
    # (same battery, same instant), computed once now that effective_battery
    # is known. The two reserves are NOT shared (see module docstring's "TWO
    # DISTINCT RESERVES" section) -- each dimension gets its own.
    available_capacity_Ah = _available_capacity_Ah(
        effective_battery, nominal_capacity_Ah, usable_capacity_factor)
    mission_reserve_capacity_Ah = _reserve_capacity_Ah(nominal_capacity_Ah, mission_reserve_fraction)
    rtl_reserve_capacity_Ah = _reserve_capacity_Ah(nominal_capacity_Ah, rtl_reserve_fraction)

    # ── Mission dimension: remaining planned-route distance from here. ─────
    if not mission_route:
        mission_feasible = None
        mission_duration = mission_capacity = mission_margin = None
        remaining_wp_count = None
        planned_dist = None
        mission_source = MISSION_METHOD_REMAINING_ROUTE
        mission_reason = REASON_MISSION_UNAVAILABLE
        route_verified_out, route_reason_out = route_identity_verified, route_identity_reason
    elif route_identity_verified is not True:
        # Route-identity safety invariant (task: mission-route-identity
        # safety): a route IS present, but its identity is not proven current
        # -- never evaluate its geometry, never emit its distance as if
        # authoritative. The RTL dimension below is entirely unaffected.
        mission_feasible = None
        mission_duration = mission_capacity = mission_margin = None
        remaining_wp_count = None
        planned_dist = None
        mission_source = MISSION_METHOD_REMAINING_ROUTE
        mission_reason = route_identity_reason or REASON_MISSION_ROUTE_UNVERIFIED
        route_verified_out, route_reason_out = False, mission_reason
    else:
        remaining, _idx = _remaining_route(mission_route, current_sequence)
        remaining_points = _route_points(remaining)
        planned_dist = round(_path_distance_from(current_position, remaining_points), 1)
        remaining_wp_count = len(remaining)
        mission_duration, mission_capacity, mission_margin = _dimension_capacity(
            planned_dist, design_speed_mps, conservative_current_A,
            available_capacity_Ah, mission_reserve_capacity_Ah, nominal_capacity_Ah)
        mission_feasible = None if mission_margin is None else (mission_margin > 0)
        mission_source = (
            MISSION_METHOD_REMAINING_ROUTE_ENDS_AT_PLANNED_HOME
            if _route_ends_at_planned_home(remaining_points, planned_home)
            else MISSION_METHOD_REMAINING_ROUTE
        )
        mission_reason = None
        route_verified_out, route_reason_out = True, None

    # ── RTL dimension: current position -> the CURRENT verified Pixhawk Home,
    #    computed HERE (never the planning-package Home, never a route-index-
    #    driven retrace) so it can never re-inherit planned-mission geometry.
    #    Entirely independent of the mission dimension above -- a stale/
    #    unverified MISSION route never affects this. ─────────────────────
    if not _valid_point(rtl_home):
        rtl_feasible = None
        rtl_duration = rtl_capacity = rtl_margin = None
        rtl_dist = None
        rtl_source = None
        rtl_reason = REASON_RTL_HOME_UNAVAILABLE
    else:
        if rtl_return_distance_m is not None:
            rtl_dist = round(rtl_return_distance_m, 1)
            rtl_source = rtl_return_geometry_source or RTL_METHOD_CALLER_SUPPLIED
        else:
            rtl_dist = round(
                geo.haversine_m(current_position[0], current_position[1],
                                rtl_home[0], rtl_home[1]), 1)
            rtl_source = RTL_METHOD_STRAIGHT_LINE
        rtl_duration, rtl_capacity, rtl_margin = _dimension_capacity(
            rtl_dist, design_speed_mps, conservative_current_A,
            available_capacity_Ah, rtl_reserve_capacity_Ah, nominal_capacity_Ah)
        rtl_feasible = None if rtl_margin is None else (rtl_margin > 0)
        rtl_reason = None

    # ── Combine: overall status/reason prioritises the MISSION dimension
    #    (what the Start gate acts on first -- task section 11 case D), then
    #    the RTL dimension. Both booleans remain independently available on
    #    the result regardless of which one drives the top-level reason. ──
    if mission_feasible is None:
        status, reason = STATUS_UNKNOWN, mission_reason
        message = "mission dimension could not be evaluated: " + mission_reason.lower().replace("_", " ")
    elif rtl_feasible is None:
        status, reason = STATUS_UNKNOWN, rtl_reason
        message = "RTL-return dimension could not be evaluated: " + rtl_reason.lower().replace("_", " ")
    elif not mission_feasible:
        status, reason = STATUS_INFEASIBLE, REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION
        message = (f"remaining planned mission needs an estimated {mission_capacity} Ah "
                   f"({planned_dist} m at {design_speed_mps} m/s design speed, ~{mission_duration} h) "
                   f"but only {available_capacity_Ah} Ah is available at effective battery "
                   f"{effective_battery}% ({battery_source}) after a {mission_reserve_capacity_Ah} Ah "
                   f"mission reserve; mission margin {mission_margin}% of nominal {nominal_capacity_Ah} Ah "
                   f"capacity.")
    elif not rtl_feasible:
        status, reason = STATUS_INFEASIBLE, REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN
        message = (f"planned mission remains completable (margin {mission_margin}%), but an RTL "
                   f"return from the current position to the current verified Pixhawk Home needs "
                   f"an estimated {rtl_capacity} Ah ({rtl_dist} m, {rtl_source}) against available "
                   f"capacity {available_capacity_Ah} Ah at effective battery {effective_battery}% "
                   f"({battery_source}) after a {rtl_reserve_capacity_Ah} Ah RTL reserve; RTL return "
                   f"margin {rtl_margin}% of nominal {nominal_capacity_Ah} Ah capacity.")
    else:
        status, reason = STATUS_FEASIBLE, REASON_SUFFICIENT_ENERGY
        message = (f"mission margin {mission_margin}%, RTL return margin {rtl_margin}% -- both "
                   f"positive at effective battery {effective_battery}% ({battery_source}), "
                   f"available capacity {available_capacity_Ah} Ah.")

    return _result(
        status, reason, message,
        remaining_wp_count=remaining_wp_count, planned_dist=planned_dist,
        mission_duration=mission_duration, rtl_duration=rtl_duration,
        mission_capacity=mission_capacity, rtl_capacity=rtl_capacity,
        available_capacity=available_capacity_Ah,
        mission_reserve_capacity=mission_reserve_capacity_Ah,
        rtl_reserve_capacity=rtl_reserve_capacity_Ah,
        mission_margin=mission_margin, rtl_margin=rtl_margin,
        mission_feasible=mission_feasible, rtl_feasible=rtl_feasible,
        rtl_dist=rtl_dist, rtl_source=rtl_source,
        mission_source=mission_source,
        route_verified=route_verified_out, route_reason=route_reason_out,
    )


def evaluate_from_snapshot(
    snapshot: Any,
    package: Optional[dict],
    injection: Optional[dict],
    cfg: Any,
    mission_binding: Optional[Dict[str, Any]] = None,
    now: Optional[float] = None,
) -> MissionFeasibilityResult:
    """
    Convenience adapter so every caller (local_agent.py's continuous per-
    iteration evaluation, mission_execution_controller's authoritative Start
    gate) pulls the SAME fields the SAME way from a
    decision_snapshot.DecisionSnapshot(-like) object, the persisted planning
    package, an active experiment_injection dict (or None), and a
    replan_config.ReplanConfig(-like) config -- one place reads this evidence,
    not two slightly-different copies.

    Home sourcing (the fix -- see module docstring):
      * planned_home comes from the planning package's own `home` field,
        independent of Pixhawk/verification state.
      * rtl_home comes from the snapshot's home_latitude/home_longitude ONLY
        when snapshot.home_valid is True (a genuinely verified Pixhawk Home).
        decision_snapshot.build_snapshot's `home_latitude`/`home_longitude`
        CAN silently fall back to the planning package's Home when
        `home_status` carries no coordinates at all -- that fallback is fine
        for decision_snapshot's own generic distance_to_home_m (an existing,
        unrelated consumer), but would be exactly the wrong-Home conflation
        for RTL feasibility here, so it is gated out via home_valid, which
        only ever reflects `home_status.verified` (never set by the
        planning-package fallback branch).

    Reuses no caller-supplied return distance: the RTL distance is computed by
    evaluate_mission_feasibility itself, straight-line from the snapshot's own
    position to rtl_home (see RTL_METHOD_STRAIGHT_LINE) -- deliberately NOT
    snapshot.estimated_safe_return_distance_m (see module docstring for why).

    `mission_binding` (task: mission-route-identity safety) is
    mission_execution_controller.status()["binding"] (or an equivalent
    shape) -- the existing readiness/binding proof this adapter reuses to
    resolve `route_identity_verified` (see `_resolve_route_identity`). Left
    None (a caller that has not wired the mission-execution controller in at
    all), the mission dimension is evaluated exactly as before this task --
    this is a deliberate default for callers that genuinely have no such
    evidence source, NOT a way to bypass the invariant: the one caller that
    matters in production (local_agent.py's continuous loop) always passes
    the real value.
    """
    lat, lon = getattr(snapshot, "latitude", None), getattr(snapshot, "longitude", None)
    current_position = (lat, lon) if lat is not None and lon is not None else None

    pkg_home = (package or {}).get("home") or {}
    plat, plon = pkg_home.get("latitude"), pkg_home.get("longitude")
    planned_home = (plat, plon) if plat is not None and plon is not None else None

    rtl_home = None
    if getattr(snapshot, "home_valid", False):
        hlat = getattr(snapshot, "home_latitude", None)
        hlon = getattr(snapshot, "home_longitude", None)
        if hlat is not None and hlon is not None:
            rtl_home = (hlat, hlon)

    route_identity_verified, route_identity_reason = _resolve_route_identity(package, mission_binding)

    return evaluate_mission_feasibility(
        current_position=current_position,
        position_age_s=getattr(snapshot, "position_age_s", None),
        mission_route=(package or {}).get("route"),
        current_sequence=getattr(snapshot, "current_sequence", None),
        planned_home=planned_home,
        rtl_home=rtl_home,
        physical_battery_percent=getattr(snapshot, "battery_percent", None),
        injected_battery_percent=(injection or {}).get("battery_percent"),
        nominal_capacity_Ah=cfg.nominal_capacity_Ah,
        conservative_current_A=cfg.conservative_current_A,
        design_speed_mps=cfg.design_speed_mps,
        usable_capacity_factor=cfg.usable_capacity_factor,
        mission_reserve_fraction=cfg.mission_reserve_fraction,
        rtl_reserve_fraction=cfg.rtl_reserve_fraction,
        route_identity_verified=route_identity_verified,
        route_identity_reason=route_identity_reason,
        measured_voltage_V=getattr(snapshot, "battery_voltage", None),
        measured_current_A=getattr(snapshot, "battery_current", None),
        max_position_age_s=getattr(cfg, "max_position_age_s", 5.0),
        now=now,
    )


# ── Actual constrained safe-return ROUTE energy check (task: revised-route
#    energy feasibility recheck) ────────────────────────────────────────────
# A distinct, LATER question from the RTL dimension above: once a caller
# (replan_controller.py) has already BUILT and geometrically VALIDATED an
# actual RETRACE_APPROVED route, is THAT constrained route -- not the RTL
# dimension's straight-line current->rtl_home estimate -- energy-feasible?
# RETRACE_APPROVED retraces already-approved waypoints rather than cutting a
# direct line, so it may be substantially longer than the initial return
# viability estimate; that upstream answer must be re-checked against the
# ACTUAL route length before the route is ever uploaded.
#
# Reuses the exact SAME battery-derived capacity/current/time model as the
# RTL dimension (_available_capacity_Ah / _dimension_capacity above -- see
# module docstring's "Boundary rule" section for the equations) and the SAME
# emergency-return reserve, rtl_reserve_fraction (module docstring's "TWO
# DISTINCT RESERVES" section: a constrained safe-return route IS a
# RETURN-HOME action, so it is judged against the RTL/emergency reserve,
# never mission_reserve_fraction). No new energy formula and no new reserve --
# purely a second DISTANCE fed through the identical, already-tested model.
REASON_ROUTE_DISTANCE_UNAVAILABLE = "ROUTE_DISTANCE_UNAVAILABLE"


@dataclass(frozen=True)
class RouteReturnEnergyResult:
    status: str                          # FEASIBLE / INFEASIBLE / UNKNOWN
    reason: str
    message: str
    feasible: Optional[bool]             # True / False / None (unknown) -- mirrors
                                          # rtl_return_feasible; UNKNOWN never becomes True.
    distance_m: Optional[float]
    duration_h: Optional[float]
    required_capacity_Ah: Optional[float]
    available_capacity_Ah: Optional[float]
    reserve_capacity_Ah: Optional[float]
    margin_Ah: Optional[float]
    margin_percent: Optional[float]
    battery_percent: Optional[float]
    battery_source: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_route_return_energy(
    *,
    distance_m: Optional[float],
    physical_battery_percent: Optional[float],
    injected_battery_percent: Optional[float] = None,
    nominal_capacity_Ah: float,
    conservative_current_A: float,
    design_speed_mps: float,
    usable_capacity_factor: float,
    reserve_fraction: float,
) -> RouteReturnEnergyResult:
    """
    Pure, deterministic, side-effect-free (same contract as
    evaluate_mission_feasibility above). `distance_m` is the caller's own
    already-computed ACTUAL route length (e.g. geo.path_length_m over a built
    RETRACE_APPROVED route's waypoints, waypoint 0 -> 1 -> ... -> Home) --
    this function never computes a distance itself and never substitutes a
    straight-line estimate; it is purely `distance_m -> required energy /
    margin / feasible`, the same shape as `_dimension_capacity` above, which
    it calls directly.

    `reserve_fraction` is the caller's choice -- callers checking a
    RETURN-HOME action must pass `rtl_reserve_fraction` (the emergency-return
    reserve), never `mission_reserve_fraction` (see module docstring).

    Three-valued, fail-closed exactly like the rest of this module: `feasible`
    is True only when a real margin is computed and strictly positive;
    missing/invalid battery or a missing distance yields UNKNOWN
    (feasible=None), never FEASIBLE.
    """
    effective_battery, battery_source = resolve_effective_battery(
        physical_battery_percent, injected_battery_percent)

    if effective_battery is None:
        return RouteReturnEnergyResult(
            status=STATUS_UNKNOWN, reason=REASON_BATTERY_INVALID,
            message="no valid battery reading (physical unavailable/out of range and no "
                    "simulated injection active) -- cannot assess actual revised-route "
                    "return energy feasibility.",
            feasible=None, distance_m=distance_m, duration_h=None,
            required_capacity_Ah=None, available_capacity_Ah=None,
            reserve_capacity_Ah=None, margin_Ah=None, margin_percent=None,
            battery_percent=None, battery_source=None,
        )

    if distance_m is None:
        return RouteReturnEnergyResult(
            status=STATUS_UNKNOWN, reason=REASON_ROUTE_DISTANCE_UNAVAILABLE,
            message="no actual revised-route distance available -- cannot assess energy "
                    "feasibility of a route that was not built.",
            feasible=None, distance_m=None, duration_h=None,
            required_capacity_Ah=None, available_capacity_Ah=None,
            reserve_capacity_Ah=None, margin_Ah=None, margin_percent=None,
            battery_percent=effective_battery, battery_source=battery_source,
        )

    available_capacity_Ah = _available_capacity_Ah(
        effective_battery, nominal_capacity_Ah, usable_capacity_factor)
    reserve_capacity_Ah = _reserve_capacity_Ah(nominal_capacity_Ah, reserve_fraction)
    duration_h, required_Ah, margin_percent = _dimension_capacity(
        distance_m, design_speed_mps, conservative_current_A,
        available_capacity_Ah, reserve_capacity_Ah, nominal_capacity_Ah)

    if margin_percent is None:
        return RouteReturnEnergyResult(
            status=STATUS_UNKNOWN, reason=REASON_ROUTE_DISTANCE_UNAVAILABLE,
            message="actual revised-route energy could not be computed (invalid design "
                    "speed configuration).",
            feasible=None, distance_m=distance_m, duration_h=None,
            required_capacity_Ah=None, available_capacity_Ah=available_capacity_Ah,
            reserve_capacity_Ah=reserve_capacity_Ah, margin_Ah=None, margin_percent=None,
            battery_percent=effective_battery, battery_source=battery_source,
        )

    margin_Ah = round(available_capacity_Ah - required_Ah - reserve_capacity_Ah, 4)
    feasible = margin_percent > 0
    if feasible:
        status, reason = STATUS_FEASIBLE, REASON_SUFFICIENT_ENERGY
        message = (f"actual revised-route return needs an estimated {required_Ah} Ah "
                   f"({distance_m} m at {design_speed_mps} m/s design speed, ~{duration_h} h) "
                   f"against available capacity {available_capacity_Ah} Ah at effective battery "
                   f"{effective_battery}% ({battery_source}) after a {reserve_capacity_Ah} Ah "
                   f"return reserve; margin {margin_percent}% of nominal {nominal_capacity_Ah} Ah "
                   f"capacity.")
    else:
        status, reason = STATUS_INFEASIBLE, REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN
        message = (f"actual revised-route return needs an estimated {required_Ah} Ah "
                   f"({distance_m} m at {design_speed_mps} m/s design speed, ~{duration_h} h) "
                   f"but only {available_capacity_Ah} Ah is available at effective battery "
                   f"{effective_battery}% ({battery_source}) after a {reserve_capacity_Ah} Ah "
                   f"return reserve; margin {margin_percent}% of nominal {nominal_capacity_Ah} Ah "
                   f"capacity -- not enough to safely complete the actual constrained route.")

    return RouteReturnEnergyResult(
        status=status, reason=reason, message=message, feasible=feasible,
        distance_m=distance_m, duration_h=duration_h,
        required_capacity_Ah=required_Ah, available_capacity_Ah=available_capacity_Ah,
        reserve_capacity_Ah=reserve_capacity_Ah, margin_Ah=margin_Ah,
        margin_percent=margin_percent,
        battery_percent=effective_battery, battery_source=battery_source,
    )
