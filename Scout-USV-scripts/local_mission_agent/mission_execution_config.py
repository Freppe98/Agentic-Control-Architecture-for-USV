"""
Typed, overridable settings for the Local Agent MISSION-EXECUTION lifecycle
(the original mission: Start / Pause / Resume / return completion).

Kept deliberately separate from replan_config.ReplanConfig: that one owns the
energy-replanning FSM's thresholds, this one owns the original-mission
lifecycle's thresholds. Both follow the same idiom -- one frozen dataclass, safe
conservative defaults, and REPLAN-style MISSION_EXEC_* environment overrides so a
systemd unit or a one-off bench run can retune without editing source.

Master enable
-------------
mission_execution_enabled gates whether the controller may EVER write to the
vehicle. It defaults to True because Start/Pause/Resume are explicit,
operator-initiated actions (unlike autonomous replanning, which defaults inert) --
but every vehicle write is STILL gated on LOCAL_AGENT control authority at the
moment of the write (see mission_execution_controller._authorized), so enabling
this flag alone never lets Scout move the boat without an operator having granted
authority first. Set it False to hard-disable the whole feature for a bench run.
"""
import os
from dataclasses import dataclass, asdict
from typing import Optional


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


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip()


def _env_float_opt(name: str, default: Optional[float]) -> Optional[float]:
    """Optional float: unset -> default; an empty/"none"/"off" value disables the
    gate (None); anything unparseable falls back to the default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    s = raw.strip().lower()
    if s in ("", "none", "off", "disabled"):
        return None
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class MissionExecutionConfig:
    # ── Master switch ──────────────────────────────────────────────────────
    mission_execution_enabled: bool = True

    # ── Home policy (section 3) ────────────────────────────────────────────
    # home_verification_tolerance_m: the maximum accepted distance between the
    #   requested launch position and the Pixhawk's read-back HOME_POSITION for
    #   the Set Home to count as verified. Passed through to the vehicle Flask
    #   Set Home service AND re-checked against its returned distance.
    # max_position_age_s: a position older than this is stale and is refused as
    #   a Start / Resume basis and as an arrival sample.
    home_verification_tolerance_m: float = 5.0
    max_position_age_s: float = 5.0

    # ── Return-to-Home completion monitor (section 7) ──────────────────────
    # home_arrival_radius_m / home_arrival_persistence_s: the vehicle must sit
    #   within the radius, continuously, for the persistence window before
    #   arrival is confirmed -- so one noisy inside-radius sample cannot end the
    #   mission. home_arrival_final_item_tolerance: how many mission items short
    #   of the last item still counts as "at or near the final item".
    home_arrival_radius_m: float = 7.5
    home_arrival_persistence_s: float = 4.0
    home_arrival_final_item_tolerance: int = 1

    # ── Normal ORIGINAL-mission completion monitor (task section 3) ─────────
    # The live-bench gap this closes: a mission that reached its final waypoint
    # (WP 14/14, remaining ~0, vehicle stopped) stayed RUNNING forever with no
    # finish transition. Completion is declared only on a defensible COMBINATION
    # of fresh evidence, never one fragile signal:
    #   * current sequence at/within the final-item tolerance of the last
    #     executable route item;
    #   * a complete, fresh Pixhawk mission readback (final item confirmed);
    #   * (optionally) vehicle position within
    #     mission_complete_position_radius_m of the final waypoint when both are
    #     available -- a None radius disables the position gate;
    #   * stable, with NO further progression, held continuously for
    #     mission_complete_persistence_s (so a sequence that momentarily equals
    #     the last index but then advances never completes the mission).
    # Only after that does the controller command + verify a final LOITER and
    # transition to COMPLETED_HOLD (it NEVER auto-disarms). mission_complete_
    # final_item_tolerance mirrors the return-home tolerance: how many items
    # short of the last still counts as "at the final item".
    mission_complete_final_item_tolerance: int = 0
    mission_complete_persistence_s: float = 6.0
    mission_complete_position_radius_m: Optional[float] = 15.0

    # ── Start proof acquisition (READINESS RETRY RACE fix) ─────────────────
    # A bounded window, INSIDE the Start transaction, for the Pixhawk mission
    # proof to go from transiently unavailable/busy/refreshing/stale to
    # genuinely fresh -- so a background coordinator refresh that is merely
    # still in flight when Start is pressed does not force the operator to
    # press Start again. Deliberately its OWN config, never the progression
    # deadline below: this bounds evidence ACQUISITION before any vehicle
    # write; progression bounds evidence of the mission actually RUNNING
    # after AUTO. A DEFINITIVE failure (hash mismatch, invalid package,
    # active replan, ...) is never retried here regardless of this timeout --
    # see mission_execution_controller._START_PROOF_TRANSIENT_CODES.
    start_proof_timeout_s: float = 15.0
    start_proof_poll_interval_s: float = 1.0

    # ── Progression confirmation (sections 2 / 5) ──────────────────────────
    # After a verified AUTO, how long to keep confirming the vehicle is actually
    # progressing the mission before declaring the Start/Resume confirmed. This
    # is the REAL total deadline of the progression watch: transient samples are
    # re-checked until a positive proof appears or this whole window elapses (it
    # is NOT an early-exit-on-first-negative check -- see
    # mission_execution_controller._watch_progression).
    start_progression_timeout_s: float = 10.0
    resume_progression_timeout_s: float = 10.0
    # How often the progression watch samples fresh vehicle state during the
    # window above. Short and bounded (250-500 ms) so it runs off the operation
    # worker without blocking the Local Agent main loop, and comfortably below
    # the timeout so many samples are taken before the deadline.
    progression_poll_interval_s: float = 0.4
    # The minimum fresh displacement (metres) from the pre-AUTO baseline position
    # that counts as movement-based progression proof (proof C). Conservative so
    # GPS jitter cannot prove progression; unit-tested. Reducing distance to the
    # current mission target is preferred over movement in an arbitrary direction.
    progression_min_displacement_m: float = 1.5

    # ── Automatic ARM phase (Start section 11) ─────────────────────────────
    # After requesting ARM (when the vehicle was disarmed), how long to keep
    # reading FRESH vehicle state for a verified armed=true before failing
    # closed. A command acknowledgement alone is never sufficient -- a fresh
    # telemetry armed=true is required. The Flask /nav/ArmOn route does its own
    # HEARTBEAT-verified arming; this is the controller's independent fresh-state
    # re-verification window on top of that.
    arm_verify_timeout_s: float = 6.0

    # ── Operation bounds (section 11) ──────────────────────────────────────
    # operation_timeout_s: an overall wall-clock cap on a single Start/Pause/
    #   Resume operation, after which it fails closed. max_operation_retries:
    #   bounded retries of a transient verified-mode/read step within one
    #   operation before it fails.
    operation_timeout_s: float = 60.0
    max_operation_retries: int = 1

    # ── Passive readiness polling (section 10 / READY-state correction) ─────
    # readiness_poll_interval_s: how often passive readiness evaluation refreshes
    #   the full read-only Start prerequisite proof (usable package, package/
    #   Pixhawk route-hash match, LOCAL_AGENT authority, fresh state). The proof
    #   includes a live Pixhawk mission readback (~2.5 s), so it is throttled and
    #   run off the main loop rather than on every 1 s iteration; an explicit
    #   Start always re-proves synchronously and is never throttled. 0 forces a
    #   refresh on every observe() (used by tests for determinism).
    #
    #   INVARIANT (see validate()): the poll interval must be COMFORTABLY shorter
    #   than the accepted-proof lifetime (planning_package.PROOF_MAX_CACHE_AGE_S,
    #   ~8 s). If it is not, the retained proof lapses between polls and passive
    #   readiness reports REPLANNING_PROOF_STALE (a "checking" window) purely
    #   because the poll outran the cache TTL. Default 5.0 s leaves ~3 s of
    #   headroom below the 8 s proof lifetime.
    readiness_poll_interval_s: float = 5.0

    # ── Stop Mission: safe abort + reset-to-start transaction (Stop lifecycle) ──
    # Stop is an operator-requested safe abort that ends the current execution,
    # restores the immutable original mission if a revised safe-return route is
    # installed, rewinds the Pixhawk mission to the start, resets execution/replan/
    # experiment test state, and hands authority back to the OPERATOR -- so a fresh
    # Start can begin the same approved original mission from the beginning. Every
    # step is verified from fresh evidence; a step that cannot be proven fails
    # closed in a safe non-running state without ever claiming a successful reset.
    #
    # mission_rewind_sequence: the Pixhawk mission item the rewind targets (seq 0
    #   is Home; route execution starts at item 1 -- see _current_target). Setting
    #   current to 0 makes the next Start begin the original route from the start.
    # mission_rewind_verify_max_sequence: the highest fresh current_seq still
    #   accepted as "rewound to the beginning" after the DO_SET_MISSION_CURRENT.
    #   Tolerates the Home-vs-first-executable-item distinction (0 or 1) and a
    #   near-instant 0->1 auto-advance, while still rejecting a mid-route sequence
    #   (proof the rewind did NOT actually take -- ACK alone is never trusted).
    # stop_rewind_verify_timeout_s: how long to keep reading FRESH vehicle state
    #   for the sequence to reach the start before failing the rewind closed.
    # stop_authority_after: the control authority Stop hands back once the vehicle
    #   is held safely and the reset is verified (task: return authority to the
    #   OPERATOR only after a safe hold).
    mission_rewind_sequence: int = 0
    mission_rewind_verify_max_sequence: int = 1
    stop_rewind_verify_timeout_s: float = 6.0
    stop_authority_after: str = "OPERATOR"

    # ── Post-restart recovery retry (RECOVERY_PENDING reconciliation) ────────
    # recovery_retry_interval_s: how often the controller re-attempts restart
    #   reconciliation while it is stuck in RECOVERY_PENDING because the initial
    #   attempt hit TEMPORARY/unavailable evidence (most commonly the vehicle
    #   Flask service on 127.0.0.1:8080 not yet up when the Local Agent starts
    #   after a reboot -- systemd ordering is not relied upon). Like the readiness
    #   poll, the reconciliation read includes a live Pixhawk readback (~2.5 s), so
    #   it is throttled and run off the main loop; the retry only fires while the
    #   state is RECOVERY_PENDING and stops the moment it reconciles or exits into
    #   a rearmable state. 0 forces a synchronous retry on every observe() (used by
    #   tests for determinism).
    recovery_retry_interval_s: float = 5.0

    def to_dict(self) -> dict:
        return asdict(self)


# field name -> (env var, caster)
_FIELD_ENV = {
    "mission_execution_enabled": ("MISSION_EXEC_ENABLED", _env_bool),
    "home_verification_tolerance_m": ("MISSION_EXEC_HOME_TOLERANCE_M", _env_float),
    "max_position_age_s": ("MISSION_EXEC_MAX_POSITION_AGE_S", _env_float),
    "home_arrival_radius_m": ("MISSION_EXEC_HOME_ARRIVAL_RADIUS_M", _env_float),
    "home_arrival_persistence_s": ("MISSION_EXEC_HOME_ARRIVAL_PERSISTENCE_S", _env_float),
    "home_arrival_final_item_tolerance": ("MISSION_EXEC_HOME_ARRIVAL_FINAL_ITEM_TOLERANCE", _env_int),
    "mission_complete_final_item_tolerance": ("MISSION_EXEC_MISSION_COMPLETE_FINAL_ITEM_TOLERANCE", _env_int),
    "mission_complete_persistence_s": ("MISSION_EXEC_MISSION_COMPLETE_PERSISTENCE_S", _env_float),
    "mission_complete_position_radius_m": ("MISSION_EXEC_MISSION_COMPLETE_POSITION_RADIUS_M", _env_float_opt),
    "start_proof_timeout_s": ("MISSION_EXEC_START_PROOF_TIMEOUT_S", _env_float),
    "start_proof_poll_interval_s": ("MISSION_EXEC_START_PROOF_POLL_INTERVAL_S", _env_float),
    "start_progression_timeout_s": ("MISSION_EXEC_START_PROGRESSION_TIMEOUT_S", _env_float),
    "resume_progression_timeout_s": ("MISSION_EXEC_RESUME_PROGRESSION_TIMEOUT_S", _env_float),
    "progression_poll_interval_s": ("MISSION_EXEC_PROGRESSION_POLL_INTERVAL_S", _env_float),
    "progression_min_displacement_m": ("MISSION_EXEC_PROGRESSION_MIN_DISPLACEMENT_M", _env_float),
    "arm_verify_timeout_s": ("MISSION_EXEC_ARM_VERIFY_TIMEOUT_S", _env_float),
    "operation_timeout_s": ("MISSION_EXEC_OPERATION_TIMEOUT_S", _env_float),
    "max_operation_retries": ("MISSION_EXEC_MAX_OPERATION_RETRIES", _env_int),
    "readiness_poll_interval_s": ("MISSION_EXEC_READINESS_POLL_INTERVAL_S", _env_float),
    "recovery_retry_interval_s": ("MISSION_EXEC_RECOVERY_RETRY_INTERVAL_S", _env_float),
    "mission_rewind_sequence": ("MISSION_EXEC_REWIND_SEQUENCE", _env_int),
    "mission_rewind_verify_max_sequence": ("MISSION_EXEC_REWIND_VERIFY_MAX_SEQUENCE", _env_int),
    "stop_rewind_verify_timeout_s": ("MISSION_EXEC_STOP_REWIND_VERIFY_TIMEOUT_S", _env_float),
    "stop_authority_after": ("MISSION_EXEC_STOP_AUTHORITY_AFTER", _env_str),
}


# The readiness poll must re-prove well WITHIN the accepted-proof lifetime, not
# merely before it -- a poll landing right at the TTL edge still leaves gaps once
# the ~2.5 s readback and jitter are accounted for. Require a comfortable
# fraction of the proof lifetime as the safe maximum.
READINESS_POLL_SAFETY_FRACTION = 0.75

# The progression poll interval must sample several times before the deadline, so
# require it to be no larger than this fraction of the total progression timeout.
PROGRESSION_POLL_SAFETY_FRACTION = 0.5


def proof_freshness_lifetime_s() -> float:
    """The accepted-proof lifetime the readiness poll must stay comfortably
    within (planning_package.PROOF_MAX_CACHE_AGE_S). Read indirectly so this
    module carries no hard import cycle and always reflects the live value."""
    try:
        import planning_package
        return float(planning_package.PROOF_MAX_CACHE_AGE_S)
    except Exception:
        return 8.0


def validate(cfg: "MissionExecutionConfig") -> "tuple":
    """(ok, issues). Enforce the timing invariant that passive readiness re-proof
    happens comfortably within the accepted-proof lifetime, so READY never
    oscillates to a proof-stale ("checking") window purely because the poll
    interval outran the cache TTL. A 0 interval (synchronous / every-observe)
    is always fine. Returns issue strings rather than raising so a caller can
    warn, clamp, or reject as it sees fit."""
    issues = []
    lifetime = proof_freshness_lifetime_s()
    safe_max = lifetime * READINESS_POLL_SAFETY_FRACTION
    poll = cfg.readiness_poll_interval_s
    if poll > 0 and poll > safe_max:
        issues.append(
            f"readiness_poll_interval_s={poll} exceeds the safe maximum "
            f"{safe_max:.1f}s (proof freshness lifetime {lifetime:.1f}s x "
            f"{READINESS_POLL_SAFETY_FRACTION}); readiness would go proof-stale "
            f"between polls for an unchanged mission")

    # ── Progression-watch / ARM invariants (Start section 8) ────────────────
    # A zero/negative deadline or poll interval would make the progression watch
    # meaningless (it either never samples or exits instantly -- the very bug
    # this rewrite fixes). The poll interval must also sit COMFORTABLY below the
    # deadline so several samples are taken before it elapses. Movement threshold
    # must be nonnegative, and the telemetry max age must be positive so a "fresh"
    # sample is a real requirement rather than always-true.
    if cfg.start_progression_timeout_s <= 0:
        issues.append(f"start_progression_timeout_s={cfg.start_progression_timeout_s} must be > 0")
    if cfg.start_proof_timeout_s <= 0:
        issues.append(f"start_proof_timeout_s={cfg.start_proof_timeout_s} must be > 0")
    if cfg.start_proof_poll_interval_s <= 0:
        issues.append(f"start_proof_poll_interval_s={cfg.start_proof_poll_interval_s} must be > 0")
    elif cfg.start_proof_poll_interval_s > cfg.start_proof_timeout_s:
        issues.append(
            f"start_proof_poll_interval_s={cfg.start_proof_poll_interval_s} must not exceed "
            f"start_proof_timeout_s={cfg.start_proof_timeout_s}")
    if cfg.resume_progression_timeout_s <= 0:
        issues.append(f"resume_progression_timeout_s={cfg.resume_progression_timeout_s} must be > 0")
    if cfg.progression_poll_interval_s <= 0:
        issues.append(f"progression_poll_interval_s={cfg.progression_poll_interval_s} must be > 0")
    elif cfg.progression_poll_interval_s > cfg.start_progression_timeout_s * PROGRESSION_POLL_SAFETY_FRACTION:
        issues.append(
            f"progression_poll_interval_s={cfg.progression_poll_interval_s} is not comfortably "
            f"below start_progression_timeout_s={cfg.start_progression_timeout_s} "
            f"(should be <= {cfg.start_progression_timeout_s * PROGRESSION_POLL_SAFETY_FRACTION:.2f}s so "
            "several samples are taken before the deadline)")
    if cfg.progression_min_displacement_m < 0:
        issues.append(f"progression_min_displacement_m={cfg.progression_min_displacement_m} must be >= 0")
    if cfg.arm_verify_timeout_s <= 0:
        issues.append(f"arm_verify_timeout_s={cfg.arm_verify_timeout_s} must be > 0")
    if cfg.max_position_age_s <= 0:
        issues.append(f"max_position_age_s={cfg.max_position_age_s} must be > 0 (fresh-telemetry gate)")
    return (not issues), issues


def load() -> MissionExecutionConfig:
    """Resolve a MissionExecutionConfig from MISSION_EXEC_* environment
    overrides, falling back to the safe defaults above. A poll interval that
    violates the freshness-lifetime invariant is warned about here (visibility);
    the retained-proof logic in the controller keeps it from oscillating even so."""
    defaults = MissionExecutionConfig()
    kwargs = {}
    for field, (env_name, caster) in _FIELD_ENV.items():
        kwargs[field] = caster(env_name, getattr(defaults, field))
    cfg = MissionExecutionConfig(**kwargs)
    ok, issues = validate(cfg)
    if not ok:
        for issue in issues:
            print(f"[MISSION_EXEC_CONFIG] WARNING: {issue}")
    return cfg


def resolve() -> "tuple":
    """Merge defaults < environment into one config plus a {field: source} map,
    mirroring replan_config.resolve() so status can show where each value came
    from. There is no runtime-override layer for mission-execution config."""
    defaults = MissionExecutionConfig()
    kwargs = {}
    sources = {}
    for field, (env_name, caster) in _FIELD_ENV.items():
        default_val = getattr(defaults, field)
        if os.environ.get(env_name) is not None:
            kwargs[field] = caster(env_name, default_val)
            sources[field] = "environment"
        else:
            kwargs[field] = default_val
            sources[field] = "default"
    return MissionExecutionConfig(**kwargs), sources


DEFAULT = load()
