"""
CONTINUOUS RISK MODEL -- deterministic, transparent, decomposable, evidence-
aware, side-effect-free, OBSERVATIONAL/ADVISORY risk assessment for the Local
Agent.

This module answers a DIFFERENT question than mission_feasibility.py:

    mission_feasibility.py -- "is the mission / return physically or
                                evidentially ADMISSIBLE?" (hard, boolean,
                                fail-closed)
    risk_model.py           -- "how UNDESIRABLE / UNCERTAIN is the current
                                operational situation, right now, on a
                                continuous scale?" (graded, advisory)

Risk NEVER recomputes feasibility. It consumes mission_feasibility.py's
RESULT (mission_feasible / rtl_return_feasible / mission_margin_percent /
rtl_return_margin_percent / battery provenance) as one input among several --
see evaluate_energy below. A hard feasibility failure is never "averaged
away" by the other components: see evaluate_risk's hard-constraint handling.

Architecture (task: DESIRED DECISION ARCHITECTURE)
-----------------------------------------------------
    stabilized vehicle evidence
              |
        DecisionSnapshot
         /            \\
        v              v
 HARD FEASIBILITY   CONTINUOUS RISK  <-- this module
 (mission_feasibility.py)  (risk_model.py)
        \\            /
         v          v
        decision evidence
              |
      recommendation only (THIS task)
              |
        Experiment Recorder

No ARM/mode-change/LOITER/RTL/mission-upload/replan/authority write of any
kind happens anywhere in this module. Every function here is pure: it takes
already-fetched evidence (dicts/values the caller read this iteration) and
returns a result. No HTTP call, no MAVLink write, no disk I/O, no controller
mutation. See test_risk_model_no_side_effects.py.

Evidence sources (see the task's own investigation write-up / the final
report's evidence inventory for the full audit)
-----------------------------------------------------
  * ENERGY        -- mission_feasibility.MissionFeasibilityResult (mission_
                      margin_percent / rtl_return_margin_percent / mission_
                      feasible / rtl_return_feasible / battery provenance).
                      NEVER the legacy ambiguous estimated_safe_return_
                      distance_m (see mission_feasibility.py's own module
                      docstring for why that figure is unsafe to reuse).
  * COMMUNICATION -- decision_snapshot.communication_state (CONNECTED /
                      PARTITIONED / DISCONNECTED, from communication.
                      get_comm_state()), contextualized once by
                      control_authority + mission-execution state (task
                      section 10) -- never a second independent read of
                      telemetry_age_s/heartbeat loss, which communication_
                      state already summarizes (avoids double-counting, task
                      section 9).
  * NAVIGATION    -- vehicle_state["evidence"]["gps"/"ekf"/"position"], the
                      FRESHNESS-STABILIZED last-valid-observation layer
                      (services/evidence_freshness.py via GET /agent/state),
                      not a raw single MAVLink poll (task section 11).
  * HEALTH        -- vehicle_state["failsafe"]["status"] (OK/ACTIVE/UNKNOWN,
                      MAV_STATE-derived) as the primary signal, vehicle_
                      state["imu"]["imu_health"] as a secondary/supporting
                      one. Deliberately does NOT re-read battery/power (that
                      is ENERGY's evidence -- task section 12's "avoid
                      counting battery twice").
  * MISSION       -- mission-execution controller state + binding, and the
                      replanning FSM state -- narrow, only the states that
                      genuinely indicate the agent's own mission/replan
                      machinery is stuck and needs attention (task section
                      13); a state the agent reaches deliberately while
                      mitigating risk (COMPLETED_HOLD, RETURNING_HOME,
                      PAUSED) is NOT itself extra risk.

Equations (task section 30 -- reproducible from the thesis)
-----------------------------------------------------------
Every component score R_x is in [0.0, 1.0] (0.0 = nominal/negligible, 1.0 =
maximum modeled risk for that dimension) or None ("EVIDENCE_UNAVAILABLE" --
never silently coerced to 0.0/nominal). See each evaluate_* function's own
docstring for its exact piecewise mapping; risk_config.py documents every
calibration constant used.

    R_total = (sum of weight_x * R_x over components with R_x available)
              / (sum of weight_x over components with R_x available)

i.e. a weighted mean RENORMALIZED over available evidence, not a sum diluted
by a missing component (a missing HEALTH reading must not silently pull the
total toward LOW just because its weight vanished from the numerator only).

Hard-feasibility override (task section 5 / acceptance criterion 4):

    hard_constraint_violated = (mission_feasible is False) or
                                (rtl_return_feasible is False)
    if hard_constraint_violated:
        score = 1.0                      # never the weighted mean
        level = CRITICAL                 # never anything else
        recommendation in (RETURN_HOME, HOLD)

UNKNOWN-evidence floor (task section 14 / acceptance criterion 5): ENERGY and
NAVIGATION are the two components this task's investigation found to be
"critical" in the task's own sense (its own worked examples name exactly
these two: "battery unavailable... navigation unavailable... must not
produce LOW"). If either is entirely unavailable (R_x is None) AND there is
no hard violation, the aggregate `level` can never read LOW or ELEVATED --
it is forced to UNKNOWN unless the AVAILABLE evidence already independently
computes to HIGH/CRITICAL (missing evidence must never make things look
BETTER, but it must also never manufacture alarm beyond what present
evidence supports).

Non-compensatory severity floors (aggregate-semantics correction task)
------------------------------------------------------------------------
The weighted mean above is a CONTINUOUS, COMPENSATORY aggregate by
construction: a perfect energy/navigation/health score can mathematically
dilute a single bad component's contribution down to a small fraction of
[0, 1] (e.g. DISCONNECTED communication alone -- score 0.95, weight 0.25 --
weighted mean 0.2375, comfortably inside the LOW bucket). That is correct
and useful for the *score* (it is a real, reproducible measure of combined
graded risk across components, exercised e.g. by the E3 communication-
degradation family), but it is WRONG for the *level*: an operator or an
autonomy policy reading "LOW" must
never be looking at a situation that contains a known DISCONNECTED link, an
unhealthy EKF, a stale position fix, an active vehicle failsafe, or a stuck
mission/replan state machine.

So the *level* is no longer read off the weighted score alone. Each
component defines a small, deterministic minimum-severity rule for its own
KNOWN-bad states (see _COMMUNICATION_FLOOR_BY_REASON /
_NAVIGATION_FLOOR_BY_REASON / _HEALTH_FLOOR_BY_REASON /
_mission_component_floor / _energy_component_floor below). These floors only
fire on AFFIRMATIVELY BAD evidence (a component's score IS available, and it
read as one of these specific reasons/ranges) -- never on
EVIDENCE_UNAVAILABLE, which is the separate UNKNOWN-evidence rule above.

ENERGY's floor (final calibration task) deserves its own note because ENERGY
carries TWO distinct risk concepts, not one:

    R_E(m)  -- the CONTINUOUS, COMPENSATORY score evaluate_energy computes
               (the piecewise-linear mapping in _margin_to_score), still
               capable of being diluted by the weighted mean like any other
               component's score.
    F_E(m)  -- the NON-COMPENSATORY minimum severity LEVEL
               (_energy_component_floor) that the SAME worst-of-(mission,
               RTL) margin m demands as it approaches the hard-feasibility
               boundary from the positive side -- immediately BELOW
               energy_elevated_margin_percent/energy_high_margin_percent
               (risk_config.py), strictly ABOVE m = 0. At and below m = 0,
               ENERGY's severity is no longer F_E's concern at all -- it is
               already the hard-feasibility override below, which is
               checked first and outranks every floor, energy's included.

    m = min(mission_margin_percent, rtl_return_margin_percent) [whichever
        are available; the more restrictive one governs]
    R_E(m) = 0                                    m >= energy_margin_safe_percent (30)
             (safe - m) / (safe - critical)        0 < m < 30 (critical = 0)
             1                                      m <= 0
    F_E(m) = NONE      m >= energy_elevated_margin_percent (15)
             ELEVATED   energy_high_margin_percent (5) <= m < 15
             HIGH       0 < m < 5
             (m <= 0 -- hard-feasibility override, not F_E)

Before this task, a margin of +1% (deep inside "tightening", R_E = 0.97,
weighted contribution ~0.29 of the 0.30 energy weight) could still be diluted
by four healthy components down to a merely ELEVATED *level*, then jump
straight to CRITICAL the instant m crossed 0 -- no progressive HIGH step in
between. F_E(m) closes that gap the same non-compensatory way communication/
navigation/health/mission's floors already do: a +1% margin now floors the
level to HIGH regardless of how good everything else looks, and a margin
between 5% and 15% floors it to at least ELEVATED.

    R_w  = weighted mean (unchanged formula above)            -- continuous
    L_w  = level_from_score(R_w)                               -- "weighted_level"
    F_i  = severity floor demanded by component i's OWN known state (or none)
    L_floor = max_severity_i(F_i)                               -- "component_floor_level"
    L_known = max_severity(L_w, L_floor)
    L_final =
        CRITICAL                                    if hard_constraint_violated
        UNKNOWN                                      elif L_known is undefined (no
                                                       evidence at all)
        UNKNOWN                                      elif energy/navigation entirely
                                                       missing AND L_known in {LOW, ELEVATED}
        L_known                                      otherwise

`max_severity` orders LOW < ELEVATED < HIGH < CRITICAL explicitly (never
lexicographic string comparison -- see _SEVERITY_INDEX/_max_severity). Ties
between two components independently demanding the same maximum floor are
broken by a fixed, documented component priority order (_COMPONENT_ORDER),
not dict-iteration accident.

`score` keeps its EXISTING contract exactly (Operator compatibility): the
weighted mean, forced to 1.0 under a hard violation, never artificially
nudged to "match" a floor. `weighted_score` is the same underlying number,
always exposed under its own name, always the true (unforced) weighted mean
even when a hard violation forces `score` to 1.0 -- so the DISCONNECTED
example above legitimately reads:

    score: 0.2375, weighted_score: 0.2375, weighted_level: LOW,
    component_floor_level: HIGH, component_floor_reason: "COMMUNICATION_
    DISCONNECTED_NO_AUTONOMOUS_EXECUTION", level: HIGH

i.e. the *score* still tells you the true combined graded magnitude; the
*level* tells you the governing operational severity after the
non-compensatory safety rule fires. Neither is "wrong" -- they answer
different questions (see module docstring's opening paragraph).
"""
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, List, Optional, Tuple

import risk_config

# ── Risk levels (task section 7) ───────────────────────────────────────────
LEVEL_LOW = "LOW"
LEVEL_ELEVATED = "ELEVATED"
LEVEL_HIGH = "HIGH"
LEVEL_CRITICAL = "CRITICAL"
LEVEL_UNKNOWN = "UNKNOWN"
_LEVELS_BY_SEVERITY = (LEVEL_LOW, LEVEL_ELEVATED, LEVEL_HIGH, LEVEL_CRITICAL)
# Explicit severity ordering -- LEVEL_UNKNOWN is deliberately NOT a member: it
# is a "no comparable severity known" sentinel, not a point on the LOW..
# CRITICAL scale, so it is never lexicographically or numerically compared
# against the four real levels (aggregate-semantics correction task section 3).
_SEVERITY_INDEX = {level: i for i, level in enumerate(_LEVELS_BY_SEVERITY)}


def _max_severity(*levels: Optional[str]) -> Optional[str]:
    """Highest-severity level among the arguments, using _SEVERITY_INDEX
    (never string comparison). None/LEVEL_UNKNOWN entries do not participate
    -- they are "nothing known here", not "LOW". Returns None if nothing
    comparable was passed."""
    known = [lvl for lvl in levels if lvl in _SEVERITY_INDEX]
    if not known:
        return None
    return max(known, key=lambda lvl: _SEVERITY_INDEX[lvl])

# ── Evidence-quality confidence (task section 14) ──────────────────────────
CONFIDENCE_HIGH = "HIGH"
CONFIDENCE_MEDIUM = "MEDIUM"
CONFIDENCE_LOW = "LOW"
CONFIDENCE_UNKNOWN = "UNKNOWN"

# ── Advisory recommendation (task section 21/22 -- NEVER auto-executed) ────
# Mission-level outcome vocabulary, distinct from LEVEL_* (severity) and from
# the replan FSM's own procedural step names (task: three-layer semantic
# model). CRITICAL does NOT universally imply HOLD -- see _recommendation().
RECOMMEND_CONTINUE = "CONTINUE"
RECOMMEND_CONTINUE_WITH_CAUTION = "CONTINUE_WITH_CAUTION"
RECOMMEND_HOLD = "HOLD"
RECOMMEND_RETURN = "RETURN_HOME"

MAX_SCORE = 1.0
MIN_SCORE = 0.0

# The two components the task's own worked examples name as "critical" --
# entirely missing evidence here forces the aggregate level away from a
# reassuring LOW/ELEVATED (see module docstring). Communication/health/
# mission are real components but are not held to this floor: comm_state is
# effectively always resolvable (communication.get_comm_state() never
# returns None in production), and health/mission are narrower supporting
# signals, not primary safety-of-return evidence.
CRITICAL_COMPONENTS = ("energy", "navigation")

# Mission-execution states under which continuing autonomously without the
# operator link is a PROVEN-capable posture, not a guess (task section 10).
_AUTONOMY_CAPABLE_STATES = frozenset({
    "RUNNING", "PAUSED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "COMPLETED_HOLD",
})

# mission-execution / replan FSM states that mean the agent's own machinery
# is stuck needing attention (task section 13) -- NOT the deliberate
# protective states it reaches while mitigating risk.
_MISSION_EXECUTION_TROUBLE_STATES = frozenset({"SUSPENDED", "FAILED"})
_REPLAN_TROUBLE_STATES = frozenset({"SAFE_HOLD", "SUSPENDED", "FALLBACK_RTL", "FAILED"})

# ── Reason codes (stable, machine-readable) ────────────────────────────────
REASON_ENERGY_HARD_INFEASIBLE = "ENERGY_HARD_INFEASIBLE"
REASON_ENERGY_EVIDENCE_UNAVAILABLE = "ENERGY_EVIDENCE_UNAVAILABLE"
REASON_ENERGY_MARGIN_COMFORTABLE = "ENERGY_MARGIN_COMFORTABLE"
REASON_ENERGY_MARGIN_TIGHTENING = "ENERGY_MARGIN_TIGHTENING"
REASON_ENERGY_MARGIN_NEAR_CRITICAL = "ENERGY_MARGIN_NEAR_CRITICAL"
# Floor-only reasons (final calibration task) -- these are never evaluate_
# energy's OWN `reason` (that stays comfortable/tightening/near_critical,
# driven by the R_energy score thresholds); they are only ever
# component_floor_reason, produced by _energy_component_floor from the SAME
# worst_margin_percent evidence, one level below the hard-feasibility
# boundary.
REASON_ENERGY_MARGIN_TIGHT = "ENERGY_MARGIN_TIGHT"
REASON_ENERGY_MARGIN_NEAR_INFEASIBLE = "ENERGY_MARGIN_NEAR_INFEASIBLE"

REASON_COMMUNICATION_CONNECTED = "COMMUNICATION_CONNECTED"
REASON_COMMUNICATION_PARTITIONED = "COMMUNICATION_PARTITIONED"
REASON_COMMUNICATION_DISCONNECTED_AUTONOMOUS = "COMMUNICATION_DISCONNECTED_AUTONOMOUS_CONTINUATION"
REASON_COMMUNICATION_DISCONNECTED_NO_AUTONOMY = "COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION"
REASON_COMMUNICATION_EVIDENCE_UNAVAILABLE = "COMMUNICATION_EVIDENCE_UNAVAILABLE"

REASON_NAVIGATION_NOMINAL = "NAVIGATION_NOMINAL"
REASON_NAVIGATION_GPS_DEGRADED = "NAVIGATION_GPS_DEGRADED"
REASON_NAVIGATION_GPS_NO_FIX = "NAVIGATION_GPS_NO_FIX"
REASON_NAVIGATION_EKF_UNHEALTHY = "NAVIGATION_EKF_UNHEALTHY"
REASON_NAVIGATION_POSITION_AGING = "NAVIGATION_POSITION_AGING"
REASON_NAVIGATION_POSITION_STALE = "NAVIGATION_POSITION_STALE"
REASON_NAVIGATION_EVIDENCE_UNAVAILABLE = "NAVIGATION_EVIDENCE_UNAVAILABLE"

REASON_HEALTH_NOMINAL = "HEALTH_NOMINAL"
REASON_HEALTH_FAILSAFE_ACTIVE = "HEALTH_FAILSAFE_ACTIVE"
REASON_HEALTH_IMU_WARNING = "HEALTH_IMU_WARNING"
REASON_HEALTH_IMU_STALE = "HEALTH_IMU_STALE"
REASON_HEALTH_EVIDENCE_UNAVAILABLE = "HEALTH_EVIDENCE_UNAVAILABLE"

REASON_MISSION_NOMINAL = "MISSION_NOMINAL"
REASON_MISSION_EXECUTION_TROUBLE = "MISSION_EXECUTION_TROUBLE"
REASON_MISSION_REPLAN_TROUBLE = "MISSION_REPLAN_TROUBLE"
REASON_MISSION_STALE_BINDING = "MISSION_STALE_BINDING"
REASON_MISSION_EVIDENCE_UNAVAILABLE = "MISSION_EVIDENCE_UNAVAILABLE"

REASON_HARD_MISSION_INFEASIBLE = "MISSION_INFEASIBLE"
REASON_HARD_RTL_INFEASIBLE = "RTL_INFEASIBLE"
REASON_HARD_MISSION_AND_RTL_INFEASIBLE = "MISSION_AND_RTL_INFEASIBLE"

REASON_NO_EVIDENCE_AVAILABLE = "NO_EVIDENCE_AVAILABLE"
REASON_NOMINAL_AGGREGATE = "NOMINAL"


@dataclass(frozen=True)
class ComponentResult:
    name: str
    score: Optional[float]          # None == EVIDENCE_UNAVAILABLE, never 0.0
    weight: float
    weighted_score: Optional[float]
    reason: str
    evidence: Dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class RiskResult:
    # `score`/`level` are the EXISTING Operator contract (unchanged shape --
    # aggregate-semantics correction task sections 5/12/19/20): `score` is
    # forced to 1.0 under a hard violation, otherwise equals `weighted_score`
    # below (never independently adjusted to "match" a floor -- task section
    # 4). `level` is now the GOVERNING level -- weighted level, corrected by
    # non-compensatory severity floors and the hard-feasibility override; it
    # may read a higher severity than `weighted_level` even though `score`
    # stayed the same number (see module docstring's DISCONNECTED example).
    score: Optional[float]          # None only if EVERY component is unavailable
    level: str
    components: Dict[str, Dict[str, Any]]
    weights: Dict[str, float]
    dominant_component: Optional[str]
    dominant_reason: Optional[str]
    hard_constraint_violated: bool
    feasibility_status: Optional[str]
    confidence: str
    evaluated_at: float
    recommendation: str
    # ── New in the aggregate-semantics correction task (section 5) ──────────
    # The true weighted mean, ALWAYS unforced -- equal to `score` except
    # under a hard violation, where `score` reads 1.0 but this keeps showing
    # the real underlying combined magnitude (diagnostic/audit value; task
    # section 5's "do not force the numeric score to match the floor").
    weighted_score: Optional[float]
    # level_from_score(weighted_score) alone -- the level the weighted
    # aggregate WOULD produce with no non-compensatory correction applied.
    weighted_level: str
    # The highest minimum severity any individual component's KNOWN state
    # demands (None if no component currently floors anything).
    component_floor_level: Optional[str]
    # The floor-triggering component's OWN reason code (None if no floor).
    component_floor_reason: Optional[str]
    # Which component produced component_floor_level/_reason (None if none).
    component_floor_source: Optional[str]
    # CRITICAL if hard_constraint_violated, else None -- the same information
    # as hard_constraint_violated, spelled as a LEVEL so callers can feed it
    # straight into the same max_severity() calculation as the other two
    # (task section 3's formal L_final = max_severity(L_w, L_floor,
    # hard_override_level)).
    hard_override_level: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _clamp(x: float) -> float:
    return max(MIN_SCORE, min(MAX_SCORE, x))


# ── ENERGY (task section 8) ────────────────────────────────────────────────
def _margin_to_score(margin: float, cfg: risk_config.RiskConfig) -> float:
    """
    Piecewise-linear:  R_E = 0                                    margin >= safe
                        R_E = 1                                    margin <= critical
                        R_E = (safe - margin) / (safe - critical)  otherwise
    """
    safe = cfg.energy_margin_safe_percent
    critical = cfg.energy_margin_critical_percent
    if margin >= safe:
        return MIN_SCORE
    if margin <= critical:
        return MAX_SCORE
    return round(_clamp((safe - margin) / (safe - critical)), 4)


def evaluate_energy(feasibility: Dict[str, Any], cfg: risk_config.RiskConfig) -> ComponentResult:
    """
    R_energy = f(min(available margins)) -- consumes mission_feasibility.py's
    OWN mission_margin_percent / rtl_return_margin_percent, never a
    recomputed distance (task section 8/2). This is the CONTINUOUS,
    COMPENSATORY score only -- see _energy_component_floor / the module
    docstring's "R_energy vs F_energy" section for the separate
    NON-compensatory minimum-severity floor computed from this same
    worst-margin evidence (final calibration task).

    * mission_feasible is False OR rtl_return_feasible is False -> R_E = 1.0,
      REASON_ENERGY_HARD_INFEASIBLE. This is a DIAGNOSTIC score only --
      evaluate_risk's hard-constraint override is what actually forces the
      aggregate level to CRITICAL, never this component alone (task section 5).
    * both feasibility dimensions are UNKNOWN (mission_feasible is None AND
      rtl_return_feasible is None -- i.e. mission_feasibility.py's own status
      is UNKNOWN, battery invalid or position stale) -> R_E = None
      (EVIDENCE_UNAVAILABLE). This is deliberately NOT triggered when only
      ONE dimension is unavailable (e.g. no mission uploaded yet, but Home is
      verified and RTL margin is known) -- the other margin is real evidence
      and is used alone.
    * otherwise -> the piecewise-linear mapping above, on whichever
      margin(s) are available (the worse of the two when both are).
    """
    weight = cfg.energy_weight
    mission_feasible = feasibility.get("mission_feasible")
    rtl_feasible = feasibility.get("rtl_return_feasible")
    mission_margin = feasibility.get("mission_margin_percent")
    rtl_margin = feasibility.get("rtl_return_margin_percent")

    evidence = {
        "mission_feasible": mission_feasible,
        "rtl_return_feasible": rtl_feasible,
        "mission_margin_percent": mission_margin,
        "rtl_return_margin_percent": rtl_margin,
        "battery_percent": feasibility.get("battery_percent"),
        "battery_source": feasibility.get("battery_source"),
        "physical_battery_percent": feasibility.get("physical_battery_percent"),
        "injected_battery_percent": feasibility.get("injected_battery_percent"),
        "feasibility_status": feasibility.get("status"),
        "feasibility_reason": feasibility.get("reason"),
    }

    if mission_feasible is False or rtl_feasible is False:
        return ComponentResult("energy", MAX_SCORE, weight, round(MAX_SCORE * weight, 4),
                               REASON_ENERGY_HARD_INFEASIBLE, evidence)

    if mission_feasible is None and rtl_feasible is None:
        return ComponentResult("energy", None, weight, None,
                               REASON_ENERGY_EVIDENCE_UNAVAILABLE, evidence)

    margins = [m for m in (mission_margin, rtl_margin) if m is not None]
    worst_margin = min(margins)
    evidence["worst_margin_percent"] = worst_margin
    score = _margin_to_score(worst_margin, cfg)
    if score <= 0.0:
        reason = REASON_ENERGY_MARGIN_COMFORTABLE
    elif score >= 0.75:
        reason = REASON_ENERGY_MARGIN_NEAR_CRITICAL
    else:
        reason = REASON_ENERGY_MARGIN_TIGHTENING
    return ComponentResult("energy", score, weight, round(score * weight, 4), reason, evidence)


# ── COMMUNICATION (task section 9/10) ──────────────────────────────────────
def evaluate_communication(comm_state: Optional[str], control_authority: Optional[str],
                           mission_execution_state: Optional[str],
                           cfg: risk_config.RiskConfig) -> ComponentResult:
    """
    Categorical base score per the AUTHORITATIVE communication_state (never a
    second independent read of heartbeat age/loss -- comm_state already
    summarizes that, task section 9's double-counting warning). DISCONNECTED
    is the one state contextualized (task section 10): a link loss under
    proven LOCAL_AGENT authority with a mission-execution state that has
    already demonstrated it can continue autonomously is real risk, but
    lower than a link loss with no valid autonomous execution posture at all.
    """
    weight = cfg.communication_weight
    autonomy_capable = (control_authority == "LOCAL_AGENT"
                        and mission_execution_state in _AUTONOMY_CAPABLE_STATES)
    evidence = {
        "communication_state": comm_state,
        "control_authority": control_authority,
        "mission_execution_state": mission_execution_state,
        "autonomous_continuation_proven": autonomy_capable,
    }

    if comm_state == "CONNECTED":
        return ComponentResult("communication", MIN_SCORE, weight, round(MIN_SCORE * weight, 4),
                               REASON_COMMUNICATION_CONNECTED, evidence)
    if comm_state == "PARTITIONED":
        score = cfg.communication_partitioned_score
        return ComponentResult("communication", score, weight, round(score * weight, 4),
                               REASON_COMMUNICATION_PARTITIONED, evidence)
    if comm_state == "DISCONNECTED":
        if autonomy_capable:
            score = cfg.communication_disconnected_authority_healthy_score
            reason = REASON_COMMUNICATION_DISCONNECTED_AUTONOMOUS
        else:
            score = cfg.communication_disconnected_score
            reason = REASON_COMMUNICATION_DISCONNECTED_NO_AUTONOMY
        return ComponentResult("communication", score, weight, round(score * weight, 4), reason, evidence)

    # None, or any value this model does not recognize -- fail closed to
    # "unknown", never assume CONNECTED-good.
    return ComponentResult("communication", None, weight, None,
                           REASON_COMMUNICATION_EVIDENCE_UNAVAILABLE, evidence)


# ── NAVIGATION / LOCALIZATION (task section 11) ────────────────────────────
def _gps_fix_score(gps_fix_evidence: Dict[str, Any], cfg: risk_config.RiskConfig) -> Optional[float]:
    state = gps_fix_evidence.get("state")
    value = gps_fix_evidence.get("value")
    if state in (None, "NEVER_OBSERVED"):
        return None
    if state == "STALE":
        # Explicit degradation of the evidence channel itself -- never let a
        # held-over "last known good" fix type hide that GPS has stopped
        # reporting (task section 11).
        return MAX_SCORE
    if value is None:
        return None
    if value >= 3:
        return MIN_SCORE
    if value == 2:
        return cfg.navigation_gps_degraded_score
    return MAX_SCORE  # 0 (no fix) / 1 (no fix)


def _ekf_score(ekf_evidence: Dict[str, Any]) -> Optional[float]:
    state = ekf_evidence.get("state")
    value = ekf_evidence.get("value")
    if state in (None, "NEVER_OBSERVED"):
        return None
    if state == "STALE":
        return MAX_SCORE
    if value is True:
        return MIN_SCORE
    if value is False:
        return MAX_SCORE
    return None


def _position_score(position_evidence: Dict[str, Any], cfg: risk_config.RiskConfig) -> Optional[float]:
    state = position_evidence.get("state")
    if state in (None, "NEVER_OBSERVED"):
        return None
    if state == "STALE":
        return MAX_SCORE
    if state == "AGING":
        return cfg.navigation_position_aging_score
    return MIN_SCORE  # FRESH


def evaluate_navigation(evidence_block: Dict[str, Any], cfg: risk_config.RiskConfig) -> ComponentResult:
    """
    R_navigation = max(gps_fix_score, ekf_score, position_score) over
    whichever of the three are available (worst signal governs -- explicit
    negative evidence on ANY one axis is never averaged away by the other
    two being healthy). Every sub-score is read from the freshness-
    stabilized evidence layer (vehicle_state["evidence"]), not a raw single
    MAVLink poll -- GPS fix type, EKF health, and position freshness are
    kept together under one component rather than three independently-
    weighted ones because they are the same underlying "can I trust where I
    think I am" question (task section 4's double-counting guidance).
    R_navigation = None only when ALL THREE sub-signals have never been
    observed at all.
    """
    weight = cfg.navigation_weight
    gps_fix_evidence = (evidence_block.get("gps") or {}).get("fix_type") or {}
    ekf_evidence = evidence_block.get("ekf") or {}
    position_evidence = evidence_block.get("position") or {}

    gps_score = _gps_fix_score(gps_fix_evidence, cfg)
    ekf_score = _ekf_score(ekf_evidence)
    pos_score = _position_score(position_evidence, cfg)

    sub_evidence = {
        "gps_fix_type": {"value": gps_fix_evidence.get("value"), "state": gps_fix_evidence.get("state"),
                         "age_s": gps_fix_evidence.get("age_s"), "score": gps_score},
        "ekf": {"value": ekf_evidence.get("value"), "state": ekf_evidence.get("state"),
               "age_s": ekf_evidence.get("age_s"), "score": ekf_score},
        "position": {"state": position_evidence.get("state"), "age_s": position_evidence.get("age_s"),
                    "score": pos_score},
    }

    available = [(name, s) for name, s in
                (("gps_fix_type", gps_score), ("ekf", ekf_score), ("position", pos_score)) if s is not None]
    if not available:
        return ComponentResult("navigation", None, weight, None,
                               REASON_NAVIGATION_EVIDENCE_UNAVAILABLE, sub_evidence)

    dominant_signal, score = max(available, key=lambda item: item[1])
    if score <= 0.0:
        reason = REASON_NAVIGATION_NOMINAL
    elif dominant_signal == "ekf" and score >= MAX_SCORE:
        reason = REASON_NAVIGATION_EKF_UNHEALTHY
    elif dominant_signal == "position" and score >= MAX_SCORE:
        reason = REASON_NAVIGATION_POSITION_STALE
    elif dominant_signal == "position":
        reason = REASON_NAVIGATION_POSITION_AGING
    elif dominant_signal == "gps_fix_type" and score >= MAX_SCORE:
        reason = REASON_NAVIGATION_GPS_NO_FIX
    else:
        reason = REASON_NAVIGATION_GPS_DEGRADED
    return ComponentResult("navigation", score, weight, round(score * weight, 4), reason, sub_evidence)


# ── VEHICLE / HEALTH (task section 12) ─────────────────────────────────────
def evaluate_health(failsafe: Dict[str, Any], imu: Dict[str, Any],
                    cfg: risk_config.RiskConfig) -> ComponentResult:
    """
    R_health = max(failsafe_score, imu_score) over whichever is available.
    failsafe (HEARTBEAT.system_status CRITICAL/EMERGENCY) is the primary,
    definitionally-1.0-when-ACTIVE signal; IMU health is secondary/
    supporting (a vibration/staleness warning, not itself a failsafe).
    Deliberately does NOT re-read battery/power evidence -- that is ENERGY's
    evidence (task section 12's "avoid counting battery twice").
    """
    weight = cfg.health_weight
    failsafe = failsafe or {}
    imu = imu or {}
    failsafe_status = failsafe.get("status")
    imu_health = imu.get("imu_health")

    if failsafe_status == "ACTIVE":
        failsafe_score = MAX_SCORE
    elif failsafe_status == "OK":
        failsafe_score = MIN_SCORE
    else:
        failsafe_score = None  # UNKNOWN / missing HEARTBEAT

    if imu_health == "OK":
        imu_score = MIN_SCORE
    elif imu_health == "WARNING":
        imu_score = cfg.health_imu_warning_score
    elif imu_health == "STALE":
        imu_score = cfg.health_imu_stale_score
    else:
        imu_score = None  # UNKNOWN / never observed

    evidence = {
        "failsafe_status": failsafe_status, "failsafe_score": failsafe_score,
        "imu_health": imu_health, "imu_score": imu_score,
    }

    available = [(n, s) for n, s in (("failsafe", failsafe_score), ("imu", imu_score)) if s is not None]
    if not available:
        return ComponentResult("health", None, weight, None, REASON_HEALTH_EVIDENCE_UNAVAILABLE, evidence)

    dominant_signal, score = max(available, key=lambda item: item[1])
    if score <= 0.0:
        reason = REASON_HEALTH_NOMINAL
    elif dominant_signal == "failsafe":
        reason = REASON_HEALTH_FAILSAFE_ACTIVE
    elif imu_health == "STALE":
        reason = REASON_HEALTH_IMU_STALE
    else:
        reason = REASON_HEALTH_IMU_WARNING
    return ComponentResult("health", score, weight, round(score * weight, 4), reason, evidence)


# ── MISSION / AUTONOMY STATE (task section 13) ─────────────────────────────
def evaluate_mission(mission_execution_status: Optional[Dict[str, Any]],
                     replan_status: Optional[Dict[str, Any]],
                     cfg: risk_config.RiskConfig) -> ComponentResult:
    """
    A narrow signal: does the agent's OWN mission/replan machinery currently
    need operator attention it cannot resolve alone? Deliberately does NOT
    score a state the agent reaches deliberately while mitigating risk
    (RUNNING/PAUSED/RETURNING_HOME/COMPLETED_HOLD all score 0.0 here) -- see
    task section 13's LOITER-after-safe-hold example. Scored states:
      * mission-execution state SUSPENDED/FAILED -> mission_trouble_score
      * replanning FSM parked in SAFE_HOLD/SUSPENDED/FALLBACK_RTL/FAILED
        -> mission_trouble_score
      * mission binding STALE_MISMATCH (a new package uploaded under a live
        execution, not yet rebound) -> mission_stale_binding_score (milder --
        an operational bookkeeping mismatch, not a stuck-machinery signal)
    R_mission = max of whichever of the above apply (0.0 if none apply).
    None (EVIDENCE_UNAVAILABLE) only when the mission-execution controller
    itself is not initialised yet (status()["supported"] is False).
    """
    weight = cfg.mission_weight
    mission_execution_status = mission_execution_status or {}
    replan_status = replan_status or {}

    supported = mission_execution_status.get("supported")
    state = mission_execution_status.get("state")
    binding_state = (mission_execution_status.get("binding") or {}).get("binding_state")
    replan_fsm = replan_status.get("fsm_state")

    evidence = {
        "mission_execution_supported": supported,
        "mission_execution_state": state,
        "binding_state": binding_state,
        "replan_fsm_state": replan_fsm,
    }

    if supported is False:
        return ComponentResult("mission", None, weight, None, REASON_MISSION_EVIDENCE_UNAVAILABLE, evidence)

    candidates: List[Tuple[float, str]] = []
    if state in _MISSION_EXECUTION_TROUBLE_STATES:
        candidates.append((cfg.mission_trouble_score, REASON_MISSION_EXECUTION_TROUBLE))
    if replan_fsm in _REPLAN_TROUBLE_STATES:
        candidates.append((cfg.mission_trouble_score, REASON_MISSION_REPLAN_TROUBLE))
    if binding_state == "STALE_MISMATCH":
        candidates.append((cfg.mission_stale_binding_score, REASON_MISSION_STALE_BINDING))

    if not candidates:
        return ComponentResult("mission", MIN_SCORE, weight, round(MIN_SCORE * weight, 4),
                               REASON_MISSION_NOMINAL, evidence)
    score, reason = max(candidates, key=lambda item: item[0])
    return ComponentResult("mission", score, weight, round(score * weight, 4), reason, evidence)


# ── Component severity floors (aggregate-semantics correction, task section 2)
# Deterministic minimum LEVEL a specific KNOWN component state demands,
# independent of how small that component's weighted contribution is. Keyed
# by the component's OWN reason code -- reusing evaluate_*'s existing reason
# vocabulary rather than inventing a parallel one, so `component_floor_reason`
# in the aggregate result is always exactly the reason the triggering
# component itself already reports (task section 5's own worked example does
# this too: "COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION" is REASON_
# COMMUNICATION_DISCONNECTED_NO_AUTONOMY verbatim). Only AFFIRMATIVELY BAD
# reasons appear here -- *_EVIDENCE_UNAVAILABLE and *_NOMINAL reasons are
# intentionally absent, so a missing/nominal component never "floors"
# anything (that stays the separate UNKNOWN-evidence rule).
#
# ENERGY has no REASON-keyed table here (unlike the four below): its floor is
# a function of the same worst_margin_percent NUMBER R_energy is computed
# from, not a small enum of categorical states, so it is computed directly by
# _energy_component_floor below rather than a reason->level lookup. Energy's
# one HARD-severity state (mission/RTL infeasibility, margin <= 0) is already
# the hard-feasibility override, checked and applied ahead of -- and
# outranking -- every floor (task section 2's "already handled by hard
# feasibility override"); _energy_component_floor only ever fires strictly
# ABOVE that boundary (worst_margin > 0), for the progressive ELEVATED/HIGH
# minimum severities immediately below the safe margin (final calibration
# task).
_COMMUNICATION_FLOOR_BY_REASON = {
    REASON_COMMUNICATION_PARTITIONED: LEVEL_ELEVATED,
    REASON_COMMUNICATION_DISCONNECTED_AUTONOMOUS: LEVEL_HIGH,
    REASON_COMMUNICATION_DISCONNECTED_NO_AUTONOMY: LEVEL_HIGH,
}

_NAVIGATION_FLOOR_BY_REASON = {
    REASON_NAVIGATION_GPS_DEGRADED: LEVEL_ELEVATED,
    REASON_NAVIGATION_GPS_NO_FIX: LEVEL_HIGH,
    REASON_NAVIGATION_EKF_UNHEALTHY: LEVEL_HIGH,
    REASON_NAVIGATION_POSITION_STALE: LEVEL_HIGH,
    # POSITION_AGING deliberately has no floor -- it is an early/soft warning
    # (cfg.navigation_position_aging_score, 0.30 by default), not yet the
    # "evidence channel has stopped reporting" signal STALE is.
}

_HEALTH_FLOOR_BY_REASON = {
    REASON_HEALTH_FAILSAFE_ACTIVE: LEVEL_CRITICAL,
    REASON_HEALTH_IMU_WARNING: LEVEL_ELEVATED,
    # IMU_STALE means the ATTITUDE/vibration CHANNEL itself has stopped
    # reporting (not just a vibration warning) -- the same "stale evidence
    # channel is worse than a bad-but-live reading" reasoning navigation's
    # POSITION_STALE floor uses, so it gets the same HIGH floor, one step
    # above the live WARNING case.
    REASON_HEALTH_IMU_STALE: LEVEL_HIGH,
}

# Mission is evaluated from its own evidence dict rather than a single
# reason->floor table: evaluate_mission collapses several different replan
# FSM states into ONE reason code (REASON_MISSION_REPLAN_TROUBLE), but this
# task requires FALLBACK_RTL to be treated differently from SAFE_HOLD/
# SUSPENDED/FAILED (task section 2: FALLBACK_RTL "may indicate mitigation is
# active rather than worsening risk... do not automatically make it
# CRITICAL" -- interpreted here, conservatively, as "impose no extra floor at
# all", the same treatment RETURNING_HOME/PAUSED/COMPLETED_HOLD already get
# in evaluate_mission's own scoring). SUSPENDED/FAILED/SAFE_HOLD are the
# agent's own machinery genuinely stuck needing attention, so they floor to
# HIGH; STALE_MISMATCH is a milder bookkeeping mismatch, floored to ELEVATED.
_MISSION_EXECUTION_FLOOR_STATES = frozenset({"SUSPENDED", "FAILED"})
_REPLAN_FLOOR_STATES = frozenset({"SAFE_HOLD", "SUSPENDED", "FAILED"})  # FALLBACK_RTL excluded, see above


def _energy_component_floor(energy: "ComponentResult",
                            cfg: risk_config.RiskConfig) -> Tuple[Optional[str], Optional[str]]:
    """Non-compensatory minimum severity F_E(m) as the worst-of-(mission,
    RTL) margin m approaches the hard-feasibility boundary from the positive
    side (final calibration task). Reads the SAME worst_margin_percent
    evidence R_energy itself was computed from (evaluate_energy's
    evidence["worst_margin_percent"]) -- never raw battery percent, and never
    re-derives margin from anything else.

    Only consulted when energy's score IS available and worst_margin_percent
    IS present in its evidence -- both are simultaneously absent exactly when
    evaluate_energy took its EVIDENCE_UNAVAILABLE branch (both margins
    unknown) OR its hard-infeasible branch (evaluate_energy returns before
    ever computing worst_margin_percent there) -- so this function naturally
    contributes no floor in either of those cases: EVIDENCE_UNAVAILABLE stays
    the separate UNKNOWN-evidence rule's concern (task section 3 -- a missing
    floor must never read as reassuring), and hard infeasibility stays
    evaluate_risk's hard_constraint_violated override's concern, which is
    checked ahead of -- and outranks -- every floor including this one (task
    section 2).

        F_E(m) = NONE     if m >= energy_elevated_margin_percent
                 ELEVATED  if energy_high_margin_percent <= m < energy_elevated_margin_percent
                 HIGH      if 0 < m < energy_high_margin_percent
                 (m <= 0 is the hard-feasibility override's concern, not this
                  function's -- see above)
    """
    if energy.score is None:
        return None, None
    worst_margin = energy.evidence.get("worst_margin_percent")
    if worst_margin is None:
        return None, None
    if worst_margin >= cfg.energy_elevated_margin_percent:
        return None, None
    if worst_margin >= cfg.energy_high_margin_percent:
        return LEVEL_ELEVATED, REASON_ENERGY_MARGIN_TIGHT
    if worst_margin > 0:
        return LEVEL_HIGH, REASON_ENERGY_MARGIN_NEAR_INFEASIBLE
    return None, None  # margin <= 0 -- hard-feasibility override's concern


def _mission_component_floor(component: "ComponentResult") -> Tuple[Optional[str], Optional[str]]:
    evidence = component.evidence
    candidates: List[Tuple[str, str]] = []
    if evidence.get("mission_execution_state") in _MISSION_EXECUTION_FLOOR_STATES:
        candidates.append((LEVEL_HIGH, REASON_MISSION_EXECUTION_TROUBLE))
    if evidence.get("replan_fsm_state") in _REPLAN_FLOOR_STATES:
        candidates.append((LEVEL_HIGH, REASON_MISSION_REPLAN_TROUBLE))
    if evidence.get("binding_state") == "STALE_MISMATCH":
        candidates.append((LEVEL_ELEVATED, REASON_MISSION_STALE_BINDING))
    if not candidates:
        return None, None
    return max(candidates, key=lambda c: _SEVERITY_INDEX[c[0]])


# Fixed tie-break order when two+ components independently demand the SAME
# maximum floor severity (task section 2/6: "deterministic tie-breaking is
# fine, but document it"). Mirrors the components' own weight-priority order
# (energy/communication/navigation/health/mission) used throughout this
# module -- not significant beyond determinism, since the SEVERITY is already
# identical for every tied candidate.
_COMPONENT_ORDER = ("energy", "communication", "navigation", "health", "mission")


def _component_severity_floor(
    components: Dict[str, ComponentResult],
    cfg: risk_config.RiskConfig,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """(floor_level, floor_reason, floor_source_component) -- the highest
    minimum severity any individual component's KNOWN state demands, or
    (None, None, None) if no component currently floors anything. Never
    triggered by EVIDENCE_UNAVAILABLE (that component's reason simply will
    not be a key in any *_FLOOR_BY_REASON table / will not match
    _mission_component_floor's / _energy_component_floor's evidence checks)."""
    candidates: List[Tuple[str, str, str]] = []

    energy = components.get("energy")
    if energy is not None:
        level, reason = _energy_component_floor(energy, cfg)
        if level is not None:
            candidates.append(("energy", level, reason))

    comm = components.get("communication")
    if comm is not None and comm.reason in _COMMUNICATION_FLOOR_BY_REASON:
        candidates.append(("communication", _COMMUNICATION_FLOOR_BY_REASON[comm.reason], comm.reason))

    nav = components.get("navigation")
    if nav is not None and nav.reason in _NAVIGATION_FLOOR_BY_REASON:
        candidates.append(("navigation", _NAVIGATION_FLOOR_BY_REASON[nav.reason], nav.reason))

    health = components.get("health")
    if health is not None and health.reason in _HEALTH_FLOOR_BY_REASON:
        candidates.append(("health", _HEALTH_FLOOR_BY_REASON[health.reason], health.reason))

    mission = components.get("mission")
    if mission is not None:
        level, reason = _mission_component_floor(mission)
        if level is not None:
            candidates.append(("mission", level, reason))

    if not candidates:
        return None, None, None

    top_severity = max(_SEVERITY_INDEX[c[1]] for c in candidates)
    tied = [c for c in candidates if _SEVERITY_INDEX[c[1]] == top_severity]
    tied.sort(key=lambda c: _COMPONENT_ORDER.index(c[0]))
    winning_component, winning_level, winning_reason = tied[0]
    return winning_level, winning_reason, winning_component


# ── Aggregation (task section 5/6/14) ──────────────────────────────────────
def _level_for_score(score: float, cfg: risk_config.RiskConfig) -> str:
    if score < cfg.level_low_max:
        return LEVEL_LOW
    if score < cfg.level_elevated_max:
        return LEVEL_ELEVATED
    if score < cfg.level_high_max:
        return LEVEL_HIGH
    return LEVEL_CRITICAL


def _confidence(components: Dict[str, ComponentResult]) -> str:
    missing = [name for name, c in components.items() if c.score is None]
    if not missing:
        return CONFIDENCE_HIGH
    if len(missing) == len(components):
        return CONFIDENCE_UNKNOWN
    if any(name in CRITICAL_COMPONENTS for name in missing):
        return CONFIDENCE_LOW
    return CONFIDENCE_MEDIUM


def _hard_violation_reason(feasibility: Dict[str, Any]) -> str:
    mission_feasible = feasibility.get("mission_feasible")
    rtl_feasible = feasibility.get("rtl_return_feasible")
    if mission_feasible is False and rtl_feasible is False:
        return REASON_HARD_MISSION_AND_RTL_INFEASIBLE
    if mission_feasible is False:
        return REASON_HARD_MISSION_INFEASIBLE
    return REASON_HARD_RTL_INFEASIBLE


def _recommendation(level: str, hard_violated: bool, feasibility: Dict[str, Any],
                     dominant_component: Optional[str] = None) -> str:
    """Advisory only -- see module docstring; nothing in this module directly
    acts on this value (see decision_policy.py for the action-request layer
    that consumes it). Uses the GOVERNING level (aggregate-semantics
    correction task section 8) -- a severity-floor-driven HIGH/CRITICAL is
    evaluated exactly as conservatively as a weighted-score-driven one.

    CRITICAL/HIGH does NOT universally imply HOLD (three-layer semantic
    model task): whenever the mission should no longer continue as planned
    -- either a hard mission/RTL-feasibility violation, or a HIGH/CRITICAL
    severity floor -- the RETURN-vs-HOLD choice is answered by
    mission_feasibility's own rtl_return_feasible, but ONLY when the
    governing problem is itself energy-shaped (a genuine hard feasibility
    violation, which is inherently a mission_feasibility/energy computation,
    or an ENERGY severity floor). A non-energy severity floor -- navigation
    uncertainty, a vehicle HEALTH_FAILSAFE_ACTIVE floor, a stuck mission/
    replan state -- says nothing about whether commanding a return right
    now is safe (a return route is only as trustworthy as the position/
    control authority it would be planned and executed from), so those
    default to the conservative HOLD regardless of rtl_return_feasible."""
    if hard_violated:
        mission_feasible = feasibility.get("mission_feasible")
        rtl_feasible = feasibility.get("rtl_return_feasible")
        if mission_feasible is False and rtl_feasible is True:
            return RECOMMEND_RETURN
        return RECOMMEND_HOLD
    if level in (LEVEL_HIGH, LEVEL_CRITICAL):
        if dominant_component == "energy" and feasibility.get("rtl_return_feasible") is True:
            return RECOMMEND_RETURN
        return RECOMMEND_HOLD
    if level in (LEVEL_ELEVATED, LEVEL_UNKNOWN):
        # UNKNOWN must never confidently say CONTINUE (task section 22).
        return RECOMMEND_CONTINUE_WITH_CAUTION
    return RECOMMEND_CONTINUE


def evaluate_risk(
    *,
    feasibility: Dict[str, Any],
    comm_state: Optional[str],
    control_authority: Optional[str],
    mission_execution_status: Optional[Dict[str, Any]],
    replan_status: Optional[Dict[str, Any]],
    navigation_evidence: Optional[Dict[str, Any]],
    failsafe: Optional[Dict[str, Any]] = None,
    imu: Optional[Dict[str, Any]] = None,
    cfg: Optional[risk_config.RiskConfig] = None,
    now: Optional[float] = None,
) -> RiskResult:
    """
    Pure, deterministic, side-effect-free (task section 17): every argument
    is evidence the caller already holds this iteration; this function only
    computes and returns a result. No vehicle command, no Operator call, no
    mission/replan/authority mutation, no disk I/O.

    `feasibility` is mission_feasibility.MissionFeasibilityResult.to_dict()
    (or an equivalent dict) -- the AUTHORITATIVE hard-constraint evidence.
    `mission_execution_status`/`replan_status` are mission_execution_
    controller.status() / replan_controller.status() (or None). `navigation_
    evidence` is vehicle_state["evidence"] from GET /agent/state (or None).
    """
    cfg = cfg or risk_config.DEFAULT
    now = time.time() if now is None else now
    feasibility = feasibility or {}
    navigation_evidence = navigation_evidence or {}

    mission_execution_state = (mission_execution_status or {}).get("state")

    energy = evaluate_energy(feasibility, cfg)
    communication = evaluate_communication(comm_state, control_authority, mission_execution_state, cfg)
    navigation = evaluate_navigation(navigation_evidence, cfg)
    health = evaluate_health(failsafe, imu, cfg)
    mission = evaluate_mission(mission_execution_status, replan_status, cfg)

    components: Dict[str, ComponentResult] = {
        "energy": energy, "communication": communication, "navigation": navigation,
        "health": health, "mission": mission,
    }

    present = {name: c for name, c in components.items() if c.score is not None}
    weight_sum = sum(c.weight for c in present.values())
    weighted_score = (
        round(sum(c.score * c.weight for c in present.values()) / weight_sum, 4)
        if weight_sum > 0 else None
    )

    mission_feasible = feasibility.get("mission_feasible")
    rtl_feasible = feasibility.get("rtl_return_feasible")
    hard_violated = (mission_feasible is False) or (rtl_feasible is False)
    hard_override_level = LEVEL_CRITICAL if hard_violated else None

    critical_missing = any(components[name].score is None for name in CRITICAL_COMPONENTS)
    # The level the weighted aggregate ALONE would produce -- always computed
    # (even under a hard violation / missing evidence) so it stays available
    # as its own diagnostic field, independent of the corrections below
    # (aggregate-semantics correction task sections 3/4).
    weighted_level = _level_for_score(weighted_score, cfg) if weighted_score is not None else LEVEL_UNKNOWN

    # Non-compensatory severity floors (task section 2/3): a known-bad
    # component state imposes a minimum LEVEL no matter how small its
    # weighted contribution is. Computed unconditionally for auditability
    # even under a hard violation (where it cannot change the outcome, since
    # hard_override_level already outranks it).
    component_floor_level, component_floor_reason, component_floor_source = _component_severity_floor(
        components, cfg)

    if hard_violated:
        # Hard-feasibility override -- outranks every floor and the weighted
        # score (task sections 1/14/17: keep this behaviour EXACTLY as it
        # was). `score` keeps its existing 1.0 contract; `weighted_score`
        # above stays the true unforced number for diagnostics.
        score = MAX_SCORE
        level = LEVEL_CRITICAL
        dominant_component = "energy"
        dominant_reason = _hard_violation_reason(feasibility)
    else:
        score = weighted_score
        # L_known = max_severity(L_w, L_floor) -- task section 3/16's formal
        # L_final (short of the hard-override/UNKNOWN corrections below).
        known_severity = _max_severity(weighted_level, component_floor_level)
        if known_severity is None:
            # No usable evidence anywhere AND no floor triggered -- nothing
            # comparable is known (only possible when weighted_score is None,
            # i.e. every component is EVIDENCE_UNAVAILABLE; a triggered floor
            # always implies at least one component IS present).
            level = LEVEL_UNKNOWN
        elif critical_missing and known_severity in (LEVEL_LOW, LEVEL_ELEVATED):
            # ENERGY/NAVIGATION entirely missing: a merely LOW/ELEVATED
            # picture built on incomplete critical evidence is not
            # trustworthy enough to call reassuring -- but a floor or
            # weighted score that ALREADY reads HIGH/CRITICAL from the
            # evidence that IS present must never be hidden behind UNKNOWN
            # (task section 7's own worked examples).
            level = LEVEL_UNKNOWN
        else:
            level = known_severity
        if present:
            nonzero_present = {name: c for name, c in present.items() if c.score > 0.0}
            if not nonzero_present:
                # Every available component reads exactly 0.0 -- there is no
                # "worst offender" to name (task section 6: a nominal
                # dominant_component=energy/ENERGY_MARGIN_COMFORTABLE reads
                # as if energy were somehow notable when it is simply
                # perfectly fine, same as everything else).
                dominant_component, dominant_reason = None, REASON_NOMINAL_AGGREGATE
            else:
                dominant_component, dominant_component_result = max(
                    nonzero_present.items(), key=lambda item: (item[1].score, item[1].weight))
                dominant_reason = dominant_component_result.reason
        else:
            dominant_component, dominant_reason = None, REASON_NO_EVIDENCE_AVAILABLE

    confidence = _confidence(components)
    recommendation = _recommendation(level, hard_violated, feasibility, dominant_component)

    return RiskResult(
        score=score,
        level=level,
        components={name: c.to_dict() for name, c in components.items()},
        weights={name: c.weight for name, c in components.items()},
        dominant_component=dominant_component,
        dominant_reason=dominant_reason,
        hard_constraint_violated=bool(hard_violated),
        feasibility_status=feasibility.get("status"),
        confidence=confidence,
        evaluated_at=round(now, 3),
        recommendation=recommendation,
        weighted_score=weighted_score,
        weighted_level=weighted_level,
        component_floor_level=component_floor_level,
        component_floor_reason=component_floor_reason,
        component_floor_source=component_floor_source,
        hard_override_level=hard_override_level,
    )


def evaluate_from_agent_state(
    *,
    feasibility: Dict[str, Any],
    comm_state: Optional[str],
    control_authority: Optional[str],
    vehicle_state: Optional[Dict[str, Any]],
    mission_execution_status: Optional[Dict[str, Any]],
    replan_status: Optional[Dict[str, Any]],
    cfg: Optional[risk_config.RiskConfig] = None,
    now: Optional[float] = None,
) -> RiskResult:
    """
    Convenience adapter mirroring mission_feasibility.evaluate_from_snapshot
    -- pulls navigation/health evidence out of the SAME vehicle_state dict
    local_agent.py already fetched this iteration (GET /agent/state), so
    every caller reads it the same way instead of two slightly different
    copies (task section 2).
    """
    vehicle_state = vehicle_state or {}
    return evaluate_risk(
        feasibility=feasibility,
        comm_state=comm_state,
        control_authority=control_authority,
        mission_execution_status=mission_execution_status,
        replan_status=replan_status,
        navigation_evidence=vehicle_state.get("evidence"),
        failsafe=vehicle_state.get("failsafe"),
        imu=vehicle_state.get("imu"),
        cfg=cfg,
        now=now,
    )
