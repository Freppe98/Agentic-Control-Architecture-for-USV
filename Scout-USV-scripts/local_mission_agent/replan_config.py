"""
Typed, overridable settings for the Local Agent replanning lifecycle.

Everything the replanning controller, energy policy, and safe-return planner
reason about lives here as one frozen dataclass rather than scattered module
constants, so the whole feature's behaviour can be read (and, for a bench
experiment, overridden) in one place. Safe defaults are conservative and, most
importantly, keep the feature INERT unless it is explicitly turned on:

  * autonomous_execution_enabled defaults to False -- the controller will
    reason, snapshot, and (in dry-run) plan, but never write to the vehicle.
  * dry_run defaults to True -- even once autonomous execution is enabled, the
    default is to simulate the transaction (no LOITER/upload/AUTO writes) until
    an operator deliberately clears dry_run.

Overrides come from environment variables (REPLAN_* prefix) so a systemd unit
or a one-off bench run can change behaviour without editing source -- the same
precedence idiom config.py already uses for OPERATOR_URLS. Nothing here is a
workstation-specific URL; deployment endpoints stay in config.py.
"""
import os
from dataclasses import dataclass, asdict


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ReplanConfig:
    # ── Master switches ────────────────────────────────────────────────────
    # autonomous_execution_enabled is the single gate on whether the controller
    # may EVER write to the vehicle. dry_run, when True, runs the full lifecycle
    # but substitutes every vehicle write with a clearly-flagged simulated
    # success, so the transaction can be demonstrated end-to-end without moving
    # the boat. Both default to the safe/inert value.
    autonomous_execution_enabled: bool = False
    dry_run: bool = True

    # ── Energy policy (energy_policy.py) ───────────────────────────────────
    # A deliberately simple, transparent, conservative model -- NOT a learned
    # battery model. See energy_policy.py for the exact calculation.
    #
    # critical_battery_percent: a hard floor -- at or below this, replan
    #   regardless of the return-feasibility estimate.
    # reserve_margin_percent: charge kept in hand on top of the estimated
    #   cost of returning, so the boat is never planned down to 0.
    # usable_range_m: conservative full-battery usable range, used only to turn
    #   a return DISTANCE into an estimated return COST in percent. Tunable per
    #   hull/payload; intentionally an underestimate of true range.
    # energy_persistence_count: debounce -- the trigger condition must hold for
    #   this many consecutive evaluations before a replan fires, so one noisy
    #   battery/GPS sample cannot trigger a return.
    critical_battery_percent: float = 15.0
    reserve_margin_percent: float = 10.0
    usable_range_m: float = 3000.0
    energy_persistence_count: int = 3

    # ── Mission-energy-feasibility capacity model (mission_feasibility.py) ──
    # A deliberately simple, transparent, conservative capacity/current/time
    # model -- NOT a high-fidelity electrochemical battery model. See
    # mission_feasibility.py's module docstring for the exact equations these
    # drive. These are PROTOTYPE CALIBRATION PARAMETERS for the thesis
    # implementation, not universal maritime thresholds; independent of (and
    # not shared with) energy_policy.py's own usable_range_m/reserve_margin_
    # percent/critical_battery_percent above, which remain that separate,
    # unmodified, existing model (task section 18).
    #
    # nominal_capacity_Ah: Scout's nominal battery capacity.
    # conservative_current_A / design_speed_mps: field-calibrated prototype
    #   parameters derived from Scout energy characterization run
    #   run-20260821-130456-usv-2-1b52892f (E-ENERGY-CAL trial 1,
    #   long_back_and_forth_fixed_speed, COMPLETED_HOLD). That run measured a
    #   moving time-weighted mean current of ~2.93 A and an observed moving
    #   speed of ~0.85 m/s over ~1.18 km / ~1.13 Ah integrated consumption
    #   (see analyze_energy_run.py's output for that run). conservative_current_A
    #   is set to 3.5 A (~19% margin above the measured moving current -- NEVER
    #   the live/idle SYS_STATUS current, task section 10) and design_speed_mps
    #   to the observed 0.85 m/s, which together keep the model ~19-20%
    #   conservative relative to that run's measured consumption density
    #   (predicted ~1.144 Ah/km vs. measured ~0.957 Ah/km). These are prototype
    #   calibration values from a single field run, not a universally-valid
    #   hydrodynamic/electrical model, and design_speed_mps remains an explicit,
    #   centrally-configured design parameter -- Scout carries no authoritative
    #   mission speed today, so this is never inferred from instantaneous
    #   groundspeed (task section 7).
    # usable_capacity_factor: derates nominal capacity to a conservative
    #   usable fraction (ageing/temperature/voltage-sag headroom).
    #
    # TWO DISTINCT RESERVES, not one shared value (task: RTL-reserve
    # semantics correction). Each is capacity held back on top of its OWN
    # dimension's estimated route cost, as a fraction of nominal capacity, so
    # neither trip is ever planned down to 0 -- the capacity-model analogue of
    # reserve_margin_percent above. They must stay distinct because the two
    # dimensions carry very different uncertainty:
    #
    # mission_reserve_fraction: the ONGOING-MISSION reserve. Buffers an open-
    #   ended set of unknowns -- further replanning cycles, extra legs,
    #   holding/loitering overhead, payload/sensor load, imprecise remaining-
    #   route geometry -- appropriate to a mission that is still in progress
    #   and may need to do more before it is over. Conservative by design.
    # rtl_reserve_fraction: the EMERGENCY-RETURN (RTL/safe-return) reserve.
    #   Buffers only the uncertainty of ONE well-defined, immediate maneuver:
    #   the straight-line RTL-distance estimate under-representing the real
    #   course (current/heading-keeping), final station-keeping/docking near
    #   Home, and battery-percent sensor noise -- plus a non-zero floor
    #   against deep discharge. Deliberately SMALLER than mission_reserve_
    #   fraction: reusing the mission reserve here made ANY RTL effectively
    #   infeasible below ~18.75% SOC (125 * 0.15) even when Home was metres
    #   away, which is the bug this split fixes. Default 0.05 (5%, one third
    #   of the mission reserve) puts that same zero-distance floor at ~6.25%
    #   SOC (125 * 0.05) -- see mission_feasibility.py's module docstring for
    #   the full derivation.
    nominal_capacity_Ah: float = 40.0
    conservative_current_A: float = 3.5
    design_speed_mps: float = 0.85
    usable_capacity_factor: float = 0.8
    mission_reserve_fraction: float = 0.15
    rtl_reserve_fraction: float = 0.05

    # ── Controller (replan_controller.py) ──────────────────────────────────
    # max_transaction_retries: bounded retries of the plan->upload->resume
    #   sequence AFTER LOITER is confirmed, before giving up to SAFE_HOLD /
    #   fallback. cooldown_s: minimum time after a terminal outcome before a
    #   new transaction may start, so a persistent condition cannot loop.
    max_transaction_retries: int = 2
    cooldown_s: float = 30.0

    # ── HOLD-SETTLE proof-acquisition (replan_controller.py) ────────────────
    # HOLD_CONFIRMED proves the Pixhawk reached and held verified LOITER MODE
    # -- it does NOT prove the armed vehicle has physically stopped
    # decelerating into that hold. The upload endpoint's OWN armed-LOITER
    # exception additionally requires fresh groundspeed at/below its
    # configured threshold (services/mission_upload_service.py's ARMED_LOITER_
    # MAX_GROUNDSPEED_M_S -- read from that endpoint's own response, never
    # duplicated here as a second magic number); a bench/water-trial run
    # showed all of max_transaction_retries burned in ~70ms immediately after
    # HOLD_CONFIRMED, purely because the boat was still slowing into LOITER,
    # not because the revised route was ever actually invalid.
    #
    # This is a bounded ACQUISITION window (the same "bounded proof-
    # acquisition around an existing read-only check" shape mission_execution_
    # config.py's start_proof_timeout_s/start_proof_poll_interval_s already
    # use for the analogous Start-readiness race), run ONCE per transaction --
    # BEFORE the plan/validate/upload retry loop, so it never itself consumes
    # a transaction retry.
    #
    # replan_hold_settle_timeout_s: the total bound on the wait. Mirrors
    #   revised_progression_timeout_s below (a few seconds is enough for a
    #   station-keeping controller to arrest way in LOITER; this is not an
    #   open-ended wait).
    # replan_hold_settle_poll_interval_s: how often to re-check.
    # replan_hold_settle_persistence_s: once the precondition check first
    #   reports ALLOWED, how long that must hold CONTINUOUSLY (any
    #   not-allowed sample resets it) before HOLD-SETTLE is declared confirmed
    #   -- mirrors services/mode_verification.py's LOITER stable_window_s
    #   (1.0s), so a single flickering fresh-but-still-just-crossing-threshold
    #   sample can never alone authorize the upload.
    replan_hold_settle_timeout_s: float = 10.0
    replan_hold_settle_poll_interval_s: float = 0.5
    replan_hold_settle_persistence_s: float = 1.0

    # ── Fallback (replan_controller.py) ────────────────────────────────────
    # rtl_fallback_enabled: whether a verified RTL is permitted as the LAST
    #   resort once retries are exhausted. Defaults OFF -- RTL does not respect
    #   operator no-go polygons, so it is opt-in only. Even when enabled it
    #   fires only with a verified Home, exhausted retries, and confirmed
    #   authority (see replan_controller._fallback).
    rtl_fallback_enabled: bool = False

    # ── Safe-return planner (safe_return_planner.py) ───────────────────────
    # connect_gap_max_m: the current position may be joined to the nearest
    #   already-approved waypoint by one short connecting segment no longer than
    #   this. Beyond it, the planner fails closed rather than drawing a long
    #   unverified line to rejoin the approved network.
    connect_gap_max_m: float = 150.0

    # ── Revised-mission progression watch (CRITICAL ISSUE 1) ───────────────
    # After the revised safe-return mission is uploaded, verified, and AUTO is
    # verified, the SAME shared progression verifier used by Start/Resume
    # (mission_progression.py) proves the revised mission is actually progressing
    # before RETURNING_HOME. These mirror the mission-execution progression
    # fields by NAME so the one algorithm reads the same knobs on every path;
    # only the total deadline may legitimately differ from Start's.
    #
    # revised_progression_timeout_s: the REAL total deadline of the revised-AUTO
    #   progression watch -- transient/UNKNOWN samples are re-checked until a
    #   positive proof appears or this whole window elapses. It is NOT an
    #   early-exit-on-first-inactive check (the old one-shot bug).
    # progression_poll_interval_s: how often the watch samples fresh state.
    # progression_min_displacement_m: minimum fresh displacement (metres) that
    #   counts as movement-based proof C (conservative so GPS jitter never proves).
    # max_position_age_s: telemetry older than this is stale and never proves.
    revised_progression_timeout_s: float = 10.0
    progression_poll_interval_s: float = 0.4
    progression_min_displacement_m: float = 1.5
    max_position_age_s: float = 5.0

    def to_dict(self) -> dict:
        return asdict(self)


def load() -> ReplanConfig:
    """Resolve a ReplanConfig from REPLAN_* environment overrides, falling back
    to the safe defaults above. Called once at import time (DEFAULT below); a
    test or bench run can also call it after setting os.environ."""
    return ReplanConfig(
        autonomous_execution_enabled=_env_bool("REPLAN_AUTONOMOUS_EXECUTION", ReplanConfig.autonomous_execution_enabled),
        dry_run=_env_bool("REPLAN_DRY_RUN", ReplanConfig.dry_run),
        critical_battery_percent=_env_float("REPLAN_CRITICAL_BATTERY_PERCENT", ReplanConfig.critical_battery_percent),
        reserve_margin_percent=_env_float("REPLAN_RESERVE_MARGIN_PERCENT", ReplanConfig.reserve_margin_percent),
        usable_range_m=_env_float("REPLAN_USABLE_RANGE_M", ReplanConfig.usable_range_m),
        energy_persistence_count=_env_int("REPLAN_ENERGY_PERSISTENCE_COUNT", ReplanConfig.energy_persistence_count),
        nominal_capacity_Ah=_env_float("REPLAN_NOMINAL_CAPACITY_AH", ReplanConfig.nominal_capacity_Ah),
        conservative_current_A=_env_float("REPLAN_CONSERVATIVE_CURRENT_A", ReplanConfig.conservative_current_A),
        design_speed_mps=_env_float("REPLAN_DESIGN_SPEED_MPS", ReplanConfig.design_speed_mps),
        usable_capacity_factor=_env_float("REPLAN_USABLE_CAPACITY_FACTOR", ReplanConfig.usable_capacity_factor),
        mission_reserve_fraction=_env_float("REPLAN_MISSION_RESERVE_FRACTION", ReplanConfig.mission_reserve_fraction),
        rtl_reserve_fraction=_env_float("REPLAN_RTL_RESERVE_FRACTION", ReplanConfig.rtl_reserve_fraction),
        max_transaction_retries=_env_int("REPLAN_MAX_TRANSACTION_RETRIES", ReplanConfig.max_transaction_retries),
        cooldown_s=_env_float("REPLAN_COOLDOWN_S", ReplanConfig.cooldown_s),
        replan_hold_settle_timeout_s=_env_float("REPLAN_HOLD_SETTLE_TIMEOUT_S", ReplanConfig.replan_hold_settle_timeout_s),
        replan_hold_settle_poll_interval_s=_env_float("REPLAN_HOLD_SETTLE_POLL_INTERVAL_S", ReplanConfig.replan_hold_settle_poll_interval_s),
        replan_hold_settle_persistence_s=_env_float("REPLAN_HOLD_SETTLE_PERSISTENCE_S", ReplanConfig.replan_hold_settle_persistence_s),
        rtl_fallback_enabled=_env_bool("REPLAN_RTL_FALLBACK_ENABLED", ReplanConfig.rtl_fallback_enabled),
        connect_gap_max_m=_env_float("REPLAN_CONNECT_GAP_MAX_M", ReplanConfig.connect_gap_max_m),
        revised_progression_timeout_s=_env_float("REPLAN_REVISED_PROGRESSION_TIMEOUT_S", ReplanConfig.revised_progression_timeout_s),
        progression_poll_interval_s=_env_float("REPLAN_PROGRESSION_POLL_INTERVAL_S", ReplanConfig.progression_poll_interval_s),
        progression_min_displacement_m=_env_float("REPLAN_PROGRESSION_MIN_DISPLACEMENT_M", ReplanConfig.progression_min_displacement_m),
        max_position_age_s=_env_float("REPLAN_MAX_POSITION_AGE_S", ReplanConfig.max_position_age_s),
    )


# ── Progression-watch invariants (CRITICAL ISSUE 1) ───────────────────────────
# The poll interval must sample several times before the deadline, so require it
# to be no larger than this fraction of the total progression timeout -- the same
# rule mission_execution_config enforces for Start/Resume.
PROGRESSION_POLL_SAFETY_FRACTION = 0.5


def validate(cfg: "ReplanConfig") -> "tuple":
    """(ok, issues). Fail-closed validation of the revised-mission progression
    knobs so a zero/negative deadline or poll interval (which would make the
    watch meaningless -- the very one-shot bug this fixes) is caught. Returns
    issue strings rather than raising so load() can warn."""
    issues = []
    if cfg.revised_progression_timeout_s <= 0:
        issues.append(f"revised_progression_timeout_s={cfg.revised_progression_timeout_s} must be > 0")
    if cfg.progression_poll_interval_s <= 0:
        issues.append(f"progression_poll_interval_s={cfg.progression_poll_interval_s} must be > 0")
    elif cfg.progression_poll_interval_s > cfg.revised_progression_timeout_s * PROGRESSION_POLL_SAFETY_FRACTION:
        issues.append(
            f"progression_poll_interval_s={cfg.progression_poll_interval_s} is not comfortably "
            f"below revised_progression_timeout_s={cfg.revised_progression_timeout_s} "
            f"(should be <= {cfg.revised_progression_timeout_s * PROGRESSION_POLL_SAFETY_FRACTION:.2f}s so "
            "several samples are taken before the deadline)")
    if cfg.progression_min_displacement_m < 0:
        issues.append(f"progression_min_displacement_m={cfg.progression_min_displacement_m} must be >= 0")
    if cfg.max_position_age_s <= 0:
        issues.append(f"max_position_age_s={cfg.max_position_age_s} must be > 0 (fresh-telemetry gate)")
    if cfg.replan_hold_settle_timeout_s <= 0:
        issues.append(f"replan_hold_settle_timeout_s={cfg.replan_hold_settle_timeout_s} must be > 0")
    if cfg.replan_hold_settle_poll_interval_s <= 0:
        issues.append(f"replan_hold_settle_poll_interval_s={cfg.replan_hold_settle_poll_interval_s} must be > 0")
    elif cfg.replan_hold_settle_poll_interval_s > cfg.replan_hold_settle_timeout_s:
        issues.append(
            f"replan_hold_settle_poll_interval_s={cfg.replan_hold_settle_poll_interval_s} must not exceed "
            f"replan_hold_settle_timeout_s={cfg.replan_hold_settle_timeout_s}")
    if cfg.replan_hold_settle_persistence_s < 0:
        issues.append(f"replan_hold_settle_persistence_s={cfg.replan_hold_settle_persistence_s} must be >= 0")
    elif cfg.replan_hold_settle_persistence_s > cfg.replan_hold_settle_timeout_s:
        issues.append(
            f"replan_hold_settle_persistence_s={cfg.replan_hold_settle_persistence_s} must not exceed "
            f"replan_hold_settle_timeout_s={cfg.replan_hold_settle_timeout_s}")
    return (not issues), issues


def _load_validated() -> ReplanConfig:
    cfg = load()
    ok, issues = validate(cfg)
    if not ok:
        for issue in issues:
            print(f"[REPLAN_CONFIG] WARNING: {issue}")
    return cfg


DEFAULT = _load_validated()


# ── Runtime override layer ────────────────────────────────────────────────────
# The Operator Station can change a small supported subset of settings at
# runtime (PATCH /agent/replan/config) WITHOUT restarting Scout and WITHOUT
# rewriting the deployment environment. Overrides live in-memory only (this
# dict), so they are ephemeral: a restart falls back to environment/defaults,
# which keeps the durable deployment config authoritative. resolve() merges the
# three layers and reports the source of each field.
import threading as _threading

# field name -> (env var, caster) -- the single map load()/resolve() share.
_FIELD_ENV = {
    "autonomous_execution_enabled": ("REPLAN_AUTONOMOUS_EXECUTION", _env_bool),
    "dry_run": ("REPLAN_DRY_RUN", _env_bool),
    "critical_battery_percent": ("REPLAN_CRITICAL_BATTERY_PERCENT", _env_float),
    "reserve_margin_percent": ("REPLAN_RESERVE_MARGIN_PERCENT", _env_float),
    "usable_range_m": ("REPLAN_USABLE_RANGE_M", _env_float),
    "energy_persistence_count": ("REPLAN_ENERGY_PERSISTENCE_COUNT", _env_int),
    "nominal_capacity_Ah": ("REPLAN_NOMINAL_CAPACITY_AH", _env_float),
    "conservative_current_A": ("REPLAN_CONSERVATIVE_CURRENT_A", _env_float),
    "design_speed_mps": ("REPLAN_DESIGN_SPEED_MPS", _env_float),
    "usable_capacity_factor": ("REPLAN_USABLE_CAPACITY_FACTOR", _env_float),
    "mission_reserve_fraction": ("REPLAN_MISSION_RESERVE_FRACTION", _env_float),
    "rtl_reserve_fraction": ("REPLAN_RTL_RESERVE_FRACTION", _env_float),
    "max_transaction_retries": ("REPLAN_MAX_TRANSACTION_RETRIES", _env_int),
    "cooldown_s": ("REPLAN_COOLDOWN_S", _env_float),
    "replan_hold_settle_timeout_s": ("REPLAN_HOLD_SETTLE_TIMEOUT_S", _env_float),
    "replan_hold_settle_poll_interval_s": ("REPLAN_HOLD_SETTLE_POLL_INTERVAL_S", _env_float),
    "replan_hold_settle_persistence_s": ("REPLAN_HOLD_SETTLE_PERSISTENCE_S", _env_float),
    "rtl_fallback_enabled": ("REPLAN_RTL_FALLBACK_ENABLED", _env_bool),
    "connect_gap_max_m": ("REPLAN_CONNECT_GAP_MAX_M", _env_float),
    "revised_progression_timeout_s": ("REPLAN_REVISED_PROGRESSION_TIMEOUT_S", _env_float),
    "progression_poll_interval_s": ("REPLAN_PROGRESSION_POLL_INTERVAL_S", _env_float),
    "progression_min_displacement_m": ("REPLAN_PROGRESSION_MIN_DISPLACEMENT_M", _env_float),
    "max_position_age_s": ("REPLAN_MAX_POSITION_AGE_S", _env_float),
}

# The subset the Operator Station may PATCH, with conservative typed bounds.
# bool fields carry no numeric bound. Anything not here is not runtime-mutable.
_PATCHABLE = {
    "autonomous_execution_enabled": ("bool", None, None),
    "dry_run": ("bool", None, None),
    "rtl_fallback_enabled": ("bool", None, None),
    "critical_battery_percent": ("float", 0.0, 60.0),
    "reserve_margin_percent": ("float", 0.0, 60.0),
    "usable_range_m": ("float", 100.0, 100000.0),
    "energy_persistence_count": ("int", 1, 20),
    "nominal_capacity_Ah": ("float", 1.0, 500.0),
    "conservative_current_A": ("float", 0.1, 100.0),
    "design_speed_mps": ("float", 0.05, 10.0),
    "usable_capacity_factor": ("float", 0.1, 1.0),
    "mission_reserve_fraction": ("float", 0.0, 0.9),
    "rtl_reserve_fraction": ("float", 0.0, 0.9),
    "max_transaction_retries": ("int", 0, 5),
    "cooldown_s": ("float", 0.0, 3600.0),
    "replan_hold_settle_timeout_s": ("float", 1.0, 60.0),
    "replan_hold_settle_poll_interval_s": ("float", 0.05, 10.0),
    "replan_hold_settle_persistence_s": ("float", 0.0, 30.0),
    "connect_gap_max_m": ("float", 0.0, 5000.0),
    "revised_progression_timeout_s": ("float", 1.0, 120.0),
    "progression_poll_interval_s": ("float", 0.05, 10.0),
    "progression_min_displacement_m": ("float", 0.0, 100.0),
    "max_position_age_s": ("float", 0.5, 60.0),
}

_override_lock = _threading.Lock()
_runtime_overrides: dict = {}


def resolve() -> "tuple":
    """Merge defaults < environment < runtime overrides into one ReplanConfig,
    plus a {field: source} map where source is 'default' | 'environment' |
    'runtime'. This is the authoritative resolved config the controller and
    energy policy should run against."""
    defaults = ReplanConfig()
    kwargs = {}
    sources = {}
    with _override_lock:
        overrides = dict(_runtime_overrides)
    for field, (env_name, caster) in _FIELD_ENV.items():
        default_val = getattr(defaults, field)
        if field in overrides:
            kwargs[field] = overrides[field]
            sources[field] = "runtime"
        elif os.environ.get(env_name) is not None:
            kwargs[field] = caster(env_name, default_val)
            sources[field] = "environment"
        else:
            kwargs[field] = default_val
            sources[field] = "default"
    return ReplanConfig(**kwargs), sources


def validate_patch(body: dict) -> "tuple":
    """Validate a PATCH body against _PATCHABLE. Returns (cleaned_overrides,
    error_code, error_message). Rejects unknown fields and out-of-bounds values
    -- nothing is applied on any error (all-or-nothing)."""
    if not isinstance(body, dict) or not body:
        return None, "INVALID_REQUEST", "request body must be a non-empty JSON object"
    cleaned = {}
    for key, value in body.items():
        spec = _PATCHABLE.get(key)
        if spec is None:
            return None, "UNSUPPORTED_SETTING", (
                f"setting {key!r} is not runtime-patchable "
                f"(patchable: {', '.join(sorted(_PATCHABLE))})"
            )
        kind, lo, hi = spec
        if kind == "bool":
            if not isinstance(value, bool):
                return None, "INVALID_VALUE", f"{key} must be a boolean"
            cleaned[key] = value
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None, "INVALID_VALUE", f"{key} must be a number"
            if value < lo or value > hi:
                return None, "OUT_OF_BOUNDS", f"{key} must be within [{lo}, {hi}] (got {value})"
            cleaned[key] = int(value) if kind == "int" else float(value)
    return cleaned, None, None


def apply_overrides(cleaned: dict) -> None:
    """Merge validated overrides into the runtime layer (in-memory only)."""
    with _override_lock:
        _runtime_overrides.update(cleaned)


def clear_overrides() -> None:
    with _override_lock:
        _runtime_overrides.clear()


def patchable_fields() -> dict:
    """The patchable subset + bounds, for the config GET response."""
    return {k: {"type": t, "min": lo, "max": hi} for k, (t, lo, hi) in _PATCHABLE.items()}
