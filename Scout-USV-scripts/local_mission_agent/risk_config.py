"""
Typed, overridable settings for the CONTINUOUS RISK MODEL (risk_model.py).

Same idiom as replan_config.py / mission_execution_config.py /
experiment_record_config.py: one frozen dataclass, safe defaults,
RISK_* environment overrides so a systemd unit or a one-off bench run can
retune without editing source.

These are ENGINEERING CALIBRATION PARAMETERS for this thesis prototype, not
universal maritime/robotics thresholds. They exist so the model's behaviour is
auditable and reproducible from one place, not because the underlying numbers
carry any external standard's authority. Every constant here maps directly to
one term in a documented piecewise-linear equation in risk_model.py -- see
that module's docstring for the exact formulas.

Deliberately NOT exposed here (definitional, not calibration choices):
  * the maximum component score (1.0) and minimum (0.0) -- the scale itself.
  * which evidence fields feed which component -- that is architecture, not a
    tunable number; changing it is a code change to risk_model.py, not a
    config value.

Kept small on purpose (task: "not dozens of tunable parameters") -- five
weights, three level thresholds, and a couple of named calibration points per
component, each independently justified in risk_model.py's docstring.
"""
import os
from dataclasses import dataclass, asdict


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class RiskConfig:
    # ── Component weights (sum should be 1.0; validate() checks this) ───────
    # R_total = energy_weight*R_energy + communication_weight*R_communication
    #         + navigation_weight*R_navigation + health_weight*R_health
    #         + mission_weight*R_mission
    # Energy/communication/navigation carry the most weight because they have
    # the most directly reliable, continuously-updated current evidence
    # (mission/RTL feasibility margins, the authoritative comm_state, and the
    # freshness-stabilized GPS/EKF/position evidence layer). Health and
    # mission/autonomy are real but narrower signals (see risk_model.py's
    # module docstring for why each is scoped the way it is), so they carry
    # less weight.
    energy_weight: float = 0.30
    communication_weight: float = 0.25
    navigation_weight: float = 0.25
    health_weight: float = 0.10
    mission_weight: float = 0.10

    # ── Risk-level thresholds (task section 7) ───────────────────────────────
    # score < level_low_max              -> LOW
    # level_low_max <= score < level_elevated_max -> ELEVATED
    # level_elevated_max <= score < level_high_max -> HIGH
    # score >= level_high_max            -> CRITICAL
    # An even quartering of [0, 1]. Documented explicitly (risk_model.py /
    # final report) as a prototype calibration choice, not a validated
    # maritime-safety boundary.
    level_low_max: float = 0.25
    level_elevated_max: float = 0.50
    level_high_max: float = 0.75

    # ── Energy component (risk_model.evaluate_energy) ────────────────────────
    # Margin (mission_margin_percent / rtl_return_margin_percent, whichever are
    # available) mapped piecewise-linearly onto [0, 1]:
    #   margin >= energy_margin_safe_percent      -> R_energy = 0.0
    #   margin <= energy_margin_critical_percent  -> R_energy -> 1.0 (this
    #     boundary coincides with mission_feasibility's own margin<=0
    #     infeasibility boundary -- reachable here only as a limit, since a
    #     margin at/below 0 has already been surfaced as hard_constraint_
    #     violated by the time risk.py runs its component maths).
    #   between -- linear interpolation.
    energy_margin_safe_percent: float = 30.0
    energy_margin_critical_percent: float = 0.0

    # ── Energy severity floors (final calibration task) ──────────────────────
    # R_energy (above) is the CONTINUOUS, COMPENSATORY score -- it can still be
    # averaged down by the other four components right up to the hard-
    # feasibility boundary (margin <= 0, handled entirely by evaluate_risk's
    # hard_constraint_violated override, never by these two). These two add a
    # non-compensatory MINIMUM LEVEL as the SAME worst-of-(mission, RTL)
    # margin approaches that boundary from the positive side, using the same
    # worst_margin_percent evidence R_energy already computes -- never raw
    # battery percent (risk_model._energy_component_floor):
    #   worst_margin >= energy_elevated_margin_percent  -> no energy floor
    #   energy_high_margin_percent <= worst_margin
    #       < energy_elevated_margin_percent             -> floor >= ELEVATED
    #   0 < worst_margin < energy_high_margin_percent    -> floor >= HIGH
    #   worst_margin <= 0                                -> hard-feasibility
    #     override (mission_feasible/rtl_return_feasible False), not this
    #     floor's concern.
    # Required ordering (validate() checks this): energy_margin_safe_percent >
    # energy_elevated_margin_percent > energy_high_margin_percent >
    # energy_margin_critical_percent.
    energy_elevated_margin_percent: float = 15.0
    energy_high_margin_percent: float = 5.0

    # ── Communication component (risk_model.evaluate_communication) ─────────
    # Categorical base score per comm_state, contextualized once for
    # DISCONNECTED (task section 10): a DISCONNECTED link under proven
    # LOCAL_AGENT authority with a healthy autonomous mission-execution state
    # is real risk, but not the same as DISCONNECTED with no valid autonomous
    # execution state at all.
    communication_partitioned_score: float = 0.50
    communication_disconnected_authority_healthy_score: float = 0.70
    communication_disconnected_score: float = 0.95

    # ── Navigation component (risk_model.evaluate_navigation) ───────────────
    # Sub-signal scores combined by MAX (worst signal governs -- never
    # "averaged away"), each drawn from the freshness-stabilized evidence
    # layer (services/evidence_freshness.py via vehicle_state["evidence"]),
    # not raw single-poll MAVLink reads.
    navigation_gps_degraded_score: float = 0.50    # 2D fix (no 3D lock)
    navigation_position_aging_score: float = 0.30  # position evidence AGING

    # ── Health component (risk_model.evaluate_health) ───────────────────────
    # IMU evidence is secondary/supporting (failsafe ACTIVE is definitional
    # 1.0, not a calibration choice -- see risk_model.py).
    health_imu_warning_score: float = 0.40   # vibration over threshold
    health_imu_stale_score: float = 0.60     # ATTITUDE older than IMU staleness bound

    # ── Mission / autonomy component (risk_model.evaluate_mission) ──────────
    # A live signal that the agent's OWN mission/replan machinery has hit
    # trouble it cannot resolve alone (mission-execution SUSPENDED, or the
    # replanning FSM parked in SAFE_HOLD/SUSPENDED/FALLBACK_RTL/FAILED) --
    # deliberately NOT triggered by ordinary protective states the agent
    # reaches deliberately (COMPLETED_HOLD, RETURNING_HOME, PAUSED), which
    # score 0.0 here (task section 13: do not inflate risk merely because the
    # agent is actively mitigating risk).
    mission_trouble_score: float = 0.65
    mission_stale_binding_score: float = 0.30   # STALE_MISMATCH package binding

    def to_dict(self) -> dict:
        return asdict(self)


_FIELD_ENV = {
    "energy_weight": "RISK_ENERGY_WEIGHT",
    "communication_weight": "RISK_COMMUNICATION_WEIGHT",
    "navigation_weight": "RISK_NAVIGATION_WEIGHT",
    "health_weight": "RISK_HEALTH_WEIGHT",
    "mission_weight": "RISK_MISSION_WEIGHT",
    "level_low_max": "RISK_LEVEL_LOW_MAX",
    "level_elevated_max": "RISK_LEVEL_ELEVATED_MAX",
    "level_high_max": "RISK_LEVEL_HIGH_MAX",
    "energy_margin_safe_percent": "RISK_ENERGY_MARGIN_SAFE_PERCENT",
    "energy_margin_critical_percent": "RISK_ENERGY_MARGIN_CRITICAL_PERCENT",
    "energy_elevated_margin_percent": "RISK_ENERGY_ELEVATED_MARGIN_PERCENT",
    "energy_high_margin_percent": "RISK_ENERGY_HIGH_MARGIN_PERCENT",
    "communication_partitioned_score": "RISK_COMMUNICATION_PARTITIONED_SCORE",
    "communication_disconnected_authority_healthy_score": "RISK_COMMUNICATION_DISCONNECTED_AUTHORITY_HEALTHY_SCORE",
    "communication_disconnected_score": "RISK_COMMUNICATION_DISCONNECTED_SCORE",
    "navigation_gps_degraded_score": "RISK_NAVIGATION_GPS_DEGRADED_SCORE",
    "navigation_position_aging_score": "RISK_NAVIGATION_POSITION_AGING_SCORE",
    "health_imu_warning_score": "RISK_HEALTH_IMU_WARNING_SCORE",
    "health_imu_stale_score": "RISK_HEALTH_IMU_STALE_SCORE",
    "mission_trouble_score": "RISK_MISSION_TROUBLE_SCORE",
    "mission_stale_binding_score": "RISK_MISSION_STALE_BINDING_SCORE",
}


def load() -> RiskConfig:
    defaults = RiskConfig()
    kwargs = {}
    for field, env_name in _FIELD_ENV.items():
        kwargs[field] = _env_float(env_name, getattr(defaults, field))
    return RiskConfig(**kwargs)


def resolve() -> "tuple":
    """(RiskConfig, {field: 'default'|'environment'}), mirroring the other
    *_config.py modules' resolve()."""
    defaults = RiskConfig()
    kwargs = {}
    sources = {}
    for field, env_name in _FIELD_ENV.items():
        default_val = getattr(defaults, field)
        if os.environ.get(env_name) is not None:
            kwargs[field] = _env_float(env_name, default_val)
            sources[field] = "environment"
        else:
            kwargs[field] = default_val
            sources[field] = "default"
    return RiskConfig(**kwargs), sources


def validate(cfg: "RiskConfig") -> "tuple":
    """(ok, issues). Fail-visible (never raises) validation: weights must be
    non-negative with a positive sum (evaluate_risk normalizes rather than
    requiring an exact sum of 1.0, so a small drift is not fatal, but an
    all-zero or negative weight set makes the model meaningless and is
    flagged), and the three level thresholds must be strictly increasing
    within (0, 1)."""
    issues = []
    weights = {
        "energy_weight": cfg.energy_weight,
        "communication_weight": cfg.communication_weight,
        "navigation_weight": cfg.navigation_weight,
        "health_weight": cfg.health_weight,
        "mission_weight": cfg.mission_weight,
    }
    for name, value in weights.items():
        if value < 0:
            issues.append(f"{name}={value} must be >= 0")
    total = sum(weights.values())
    if total <= 0:
        issues.append(f"sum of component weights is {total}, must be > 0")
    elif abs(total - 1.0) > 1e-6:
        issues.append(f"sum of component weights is {total}, expected 1.0 (evaluate_risk "
                      "normalizes by the actual sum, so this is a warning, not a hard failure)")
    if not (0.0 < cfg.level_low_max < cfg.level_elevated_max < cfg.level_high_max < 1.0):
        issues.append(
            f"level thresholds must satisfy 0 < level_low_max ({cfg.level_low_max}) < "
            f"level_elevated_max ({cfg.level_elevated_max}) < level_high_max "
            f"({cfg.level_high_max}) < 1")
    if cfg.energy_margin_safe_percent <= cfg.energy_margin_critical_percent:
        issues.append(
            f"energy_margin_safe_percent ({cfg.energy_margin_safe_percent}) must be > "
            f"energy_margin_critical_percent ({cfg.energy_margin_critical_percent})")
    if not (cfg.energy_margin_safe_percent > cfg.energy_elevated_margin_percent
            > cfg.energy_high_margin_percent > cfg.energy_margin_critical_percent):
        issues.append(
            f"energy floor thresholds must satisfy energy_margin_safe_percent "
            f"({cfg.energy_margin_safe_percent}) > energy_elevated_margin_percent "
            f"({cfg.energy_elevated_margin_percent}) > energy_high_margin_percent "
            f"({cfg.energy_high_margin_percent}) > energy_margin_critical_percent "
            f"({cfg.energy_margin_critical_percent})")
    return (not issues), issues


def _load_validated() -> RiskConfig:
    cfg = load()
    ok, issues = validate(cfg)
    if not ok:
        for issue in issues:
            print(f"[RISK_CONFIG] WARNING: {issue}")
    return cfg


DEFAULT = _load_validated()
