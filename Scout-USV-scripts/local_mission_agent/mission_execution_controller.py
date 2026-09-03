"""
The mission-execution controller: the one component that owns the ORIGINAL
mission lifecycle end to end -- preparation, start, running, pause, resume, and
return completion.

It is deliberately SEPARATE from the energy replanning controller
(replan_controller.py). That controller owns the safe-return transaction (energy
decision, replanning LOITER, revised route, upload/readback, AUTO resume). This
one owns the mission the operator prepared, and hands off cleanly to the
replanning controller when the energy policy triggers -- it never issues a
competing mode command while replanning owns the vehicle (task section 6).

Concept separation (task's architectural requirement):
  1. Pixhawk mode (AUTO/LOITER/RTL/...) -- an OBSERVED value, read from
     /agent/state; never conflated with a lifecycle state.
  2. Mission-execution lifecycle -- the FSM in this file.
  3. Replanning lifecycle -- the separate FSM in replan_controller.py.

FSM
---
    NOT_READY ── usable planning package present ──> READY
    READY ── Start Mission ──> START_REQUESTED
          ── (disarmed) ──> ARMING ──> verified armed=true ──> VERIFYING_ARMED
          ── ARM verified / already armed ──> START_HOLD_REQUESTED
          ── verified LOITER *while armed* ──> START_HOLD_CONFIRMED ──> SETTING_HOME
          ── verified Home read-back ──> VERIFYING_HOME ──> SYNCHRONIZING_PACKAGE
          ── package Home synced + consistent ──> STARTING_AUTO
          ── verified AUTO + progression ──> RUNNING

    Start write order for a Start beginning DISARMED is ARM -> LOITER -> SET_HOME
    -> AUTO; for a Start already ARMED it is LOITER -> SET_HOME -> AUTO (no ARM).
    ARM is the FIRST vehicle-changing write so the first LOITER we depend on as a
    safety hold occurs only after the vehicle is positively verified armed -- a
    disarmed USV can report LOITER mode but cannot physically hold station.
    RUNNING ── Pause ──> PAUSE_REQUESTED ── verified LOITER ──> PAUSED
    PAUSED  ── Resume ──> RESUME_REQUESTED ── verified AUTO ──> RUNNING
    RUNNING ── energy replan triggers ──> (replanning owns the vehicle;
              mission-execution exposes derived REPLANNING, issues no writes)
    replanning reaches MONITORING_REVISED ──> RETURNING_HOME
    RETURNING_HOME ── within radius, persisted, fresh ──> HOME_ARRIVAL_PENDING
                   ── arrival confirmed ──> FINAL_HOLD_REQUESTED
                   ── verified LOITER ──> COMPLETED_HOLD (success/terminal)
    Any operation: authority lost mid-write ──> SUSPENDED (writes stop, hold)
    replanning reaches SAFE_HOLD/SUSPENDED/FAILED ──> SUSPENDED (no auto-resume),
      EXCEPT a HOLD-only transaction (decision_policy requested a safety hold,
      no replan attempted) whose physical hold-settle was POSITIVELY PROVEN
      (fsm SAFE_HOLD) ──> PAUSED instead: a deliberate, successful controlled
      pause (mission/sequence retained, vehicle in verified LOITER), not an
      execution failure. Reconnection (comm CONNECTED) never auto-Resumes from
      PAUSED -- only an explicit operator Resume (the existing guarded
      transaction above) may. A HOLD-only transaction whose hold could NOT be
      positively proven (fsm SUSPENDED, HOLD_SETTLE_TIMEOUT /
      LOITER_REASSERT_NOT_VERIFIED) and any genuinely attempted-and-failed
      replan still fail closed to SUSPENDED -- see _apply_replan_handoff.

Every vehicle write goes through an injected gateway
(mission_execution_gateway.py) that calls the existing verified Flask endpoints
-- there is no direct-MAVLink path here, and there is no second copy of the
verified LOITER/AUTO/Set Home logic (task section 11). A fresh control-authority
check runs immediately before EVERY write; a shared write arbiter
(write_arbiter.py) guarantees this controller and the replanning controller can
never write at the same instant.
"""
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

import autonomy_gate
import experiment_injection
import geo
import mission_execution_config as me_config
import mission_feasibility
import mission_progression
import planning_package
import replan_config
import write_arbiter

# ── FSM states ──────────────────────────────────────────────────────────────
NOT_READY = "NOT_READY"
READY = "READY"
START_REQUESTED = "START_REQUESTED"
# ARM precedes the physical-hold LOITER (only reached when initially disarmed).
ARMING = "ARMING"
VERIFYING_ARMED = "VERIFYING_ARMED"
START_HOLD_REQUESTED = "START_HOLD_REQUESTED"
START_HOLD_CONFIRMED = "START_HOLD_CONFIRMED"
SETTING_HOME = "SETTING_HOME"
VERIFYING_HOME = "VERIFYING_HOME"
SYNCHRONIZING_PACKAGE = "SYNCHRONIZING_PACKAGE"
STARTING_AUTO = "STARTING_AUTO"
CONFIRMING_PROGRESSION = "CONFIRMING_PROGRESSION"
RUNNING = "RUNNING"
PAUSE_REQUESTED = "PAUSE_REQUESTED"
PAUSED = "PAUSED"
RESUME_REQUESTED = "RESUME_REQUESTED"
# ── Stop Mission phases (operator-requested safe abort + reset-to-start) ──────
# These are TRANSIENT operation phases (like the Start pipeline phases), not
# permanent rest states: a Stop transaction moves through them and settles into
# READY / NOT_READY (reset proven) or SUSPENDED (failed after the hold). Physical
# safety precedes logical reset: the verified LOITER hold is proven BEFORE any
# mission restore / rewind / state reset happens.
STOP_REQUESTED = "STOP_REQUESTED"
STOP_HOLD_REQUESTED = "STOP_HOLD_REQUESTED"
STOP_HOLD_CONFIRMED = "STOP_HOLD_CONFIRMED"
RESTORING_ORIGINAL = "RESTORING_ORIGINAL"
REWINDING_MISSION = "REWINDING_MISSION"
VERIFYING_RESET = "VERIFYING_RESET"
RETURNING_HOME = "RETURNING_HOME"
HOME_ARRIVAL_PENDING = "HOME_ARRIVAL_PENDING"
FINAL_HOLD_REQUESTED = "FINAL_HOLD_REQUESTED"
COMPLETED_HOLD = "COMPLETED_HOLD"
SUSPENDED = "SUSPENDED"
FAILED = "FAILED"
# Explicit post-restart reconciliation phase. A persisted STABLE autonomous state
# (RUNNING/PAUSED/...) is EVIDENCE of the prior run, never proof of the current
# vehicle state, so it is re-proved from fresh local evidence here before any
# authoritative live state is re-exposed. Non-authoritative, non-live: never
# reported as RUNNING, never drives the replanning handoff, never issues a write.
RECOVERY_PENDING = "RECOVERY_PENDING"

# States with a live original mission that the replanning handoff applies to.
_LIVE_STATES = (RUNNING, PAUSED, RETURNING_HOME, HOME_ARRIVAL_PENDING)
# Terminal / idle / recovery states an operator may rearm from.
_REARMABLE_STATES = (COMPLETED_HOLD, SUSPENDED, FAILED, RECOVERY_PENDING)
# States an operator may Stop from: any live original/revised execution, plus a
# SUSPENDED run (e.g. after a replan failure) whose revised mission still needs
# restoring/rewinding for a clean re-Start. NOT_READY/READY have nothing to abort;
# COMPLETED_HOLD/FAILED are terminal (rearm handles those).
_STOPPABLE_STATES = (RUNNING, PAUSED, RETURNING_HOME, HOME_ARRIVAL_PENDING, SUSPENDED)
# Operation-in-flight states -- if any is found persisted at startup, an
# operation was interrupted and is failed closed (never resumed).
_INTERRUPTIBLE_STATES = (
    START_REQUESTED, ARMING, VERIFYING_ARMED, START_HOLD_REQUESTED,
    START_HOLD_CONFIRMED, SETTING_HOME, VERIFYING_HOME, SYNCHRONIZING_PACKAGE,
    STARTING_AUTO, CONFIRMING_PROGRESSION,
    PAUSE_REQUESTED, RESUME_REQUESTED, FINAL_HOLD_REQUESTED,
    # A Stop interrupted mid-transaction (before it settled) is failed closed on
    # restart, exactly like any other interrupted operation -- never resumed.
    STOP_REQUESTED, STOP_HOLD_REQUESTED, STOP_HOLD_CONFIRMED,
    RESTORING_ORIGINAL, REWINDING_MISSION, VERIFYING_RESET,
)
# Persisted STABLE autonomous states that must be reconciled (not trusted) after a
# restart, mapped to the vehicle mode that state implies. A state absent from this
# map (RETURNING_HOME / HOME_ARRIVAL_PENDING) cannot be positively re-proven safe
# from a cold start and stays in RECOVERY_PENDING for the operator to re-issue.
_RECONCILABLE_STABLE_MODE = {RUNNING: "AUTO", PAUSED: "LOITER"}
# Vehicle modes that mean "under autonomous propulsion". If restart reconciliation
# hits a DEFINITIVE contradiction while fresh state shows the vehicle ARMED in one
# of these modes, recovery fails closed PHYSICALLY: it requests the existing
# safety-exempt verified LOITER hold (never ARM/AUTO, never auto-disarm) before
# declaring a non-running, operator-recovery-required state.
_AUTONOMOUS_EXECUTION_MODES = frozenset({"AUTO"})
# Readiness-proof reasons that mean "the ONLY thing keeping this mission from
# being started RIGHT NOW is that control authority is not LOCAL_AGENT". These
# are surfaced as authority_blocks_start so an OPERATOR-authority pre-Start state
# reads as "waiting to acquire Local Agent authority", not "un-startable mission"
# (task section 2). This never asserts the other evidence is proven -- authority
# is evaluated before the Pixhawk readback in the proof -- it only classifies the
# CURRENT block as an authority handoff rather than a mission/package defect.
_AUTHORITY_BLOCK_REASONS = frozenset({"AUTHORITY_NOT_LOCAL_AGENT", "AUTHORITY_UNKNOWN"})

# ── Three-valued mission-active evidence (task's semantics) ───────────────────
# An absent or stale mission-active field is UNKNOWN, never collapsed to false.
# Canonically defined in mission_progression (the shared verifier); re-exported
# here so existing references keep resolving against one source of truth.
ACTIVE_TRUE = mission_progression.ACTIVE_TRUE
ACTIVE_FALSE_EXPLICIT = mission_progression.ACTIVE_FALSE_EXPLICIT
ACTIVE_UNKNOWN = mission_progression.ACTIVE_UNKNOWN

# ── Operator-facing launch phases (task: neutral, phase-specific status) ──────
# One phase at a time; the Operator shows the label, the Agent trace keeps the
# detailed evidence. Not the same as the FSM state (which is the internal safety
# machine); this is the human-readable "what is happening right now".
PHASE_LABELS = {
    START_REQUESTED: "Checking mission readiness…",
    ARMING: "Arming vehicle…",
    VERIFYING_ARMED: "Arming vehicle…",
    START_HOLD_REQUESTED: "Taking agent control…",
    START_HOLD_CONFIRMED: "Holding position…",
    SETTING_HOME: "Setting and verifying Home…",
    VERIFYING_HOME: "Setting and verifying Home…",
    SYNCHRONIZING_PACKAGE: "Synchronizing mission package…",
    STARTING_AUTO: "Starting AUTO…",
    CONFIRMING_PROGRESSION: "Confirming mission progression…",
    RECOVERY_PENDING: "Reconciling mission after restart…",
    STOP_REQUESTED: "Stopping mission…",
    STOP_HOLD_REQUESTED: "Holding position (LOITER)…",
    STOP_HOLD_CONFIRMED: "Position hold confirmed…",
    RESTORING_ORIGINAL: "Restoring original mission…",
    REWINDING_MISSION: "Rewinding mission to start…",
    VERIFYING_RESET: "Resetting for a fresh start…",
}

# The replanning FSM states that mean a replan transaction is in flight. Kept as
# a local literal set (not imported) so this controller does not depend on
# replan_controller's module internals; the authoritative live signal is still
# the shared write arbiter's owner.
_ACTIVE_REPLAN_STATES = frozenset({
    "HOLD_REQUESTED", "HOLD_CONFIRMED", "PLANNING", "VALIDATING",
    "UPLOAD_REQUESTED", "VERIFYING_REVISION", "RESUME_REQUESTED",
})
_REPLAN_SUCCESS_STATE = "MONITORING_REVISED"
_REPLAN_FAILURE_STATES = frozenset({"SAFE_HOLD", "SUSPENDED", "FALLBACK_RTL", "FAILED"})
# replan_controller.py error codes that mean "the HOLD-only physical
# hold-settle proof (mode + fresh groundspeed + persistence) could not be
# established within its bound" (_hold_not_proven(), P0 SAFE_HOLD-invariant
# fix) -- a HOLD-only transaction that landed on SUSPENDED for exactly this
# reason, never a genuine planner/upload/authority failure. A local literal
# set (not imported), same reasoning as _ACTIVE_REPLAN_STATES above.
_HOLD_PROOF_FAILURE_CODES = frozenset({"HOLD_SETTLE_TIMEOUT", "LOITER_REASSERT_NOT_VERIFIED"})

# ── Start proof-acquisition classification (READINESS RETRY RACE fix) ────────
# The exact _resolve_start_prerequisites() failure codes that mean "the Pixhawk
# mission proof is not fresh/available YET" (a background coordinator refresh
# genuinely in flight, a busy/refreshing/stale cache, a transient read
# timeout) -- never a definitive package/mission/hash/geometry/authority/
# replan problem. Only these are retried by _acquire_start_proof(); every
# other failure (route hash mismatch, invalid/missing package, active replan,
# authority violation, geometry failure, ...) fails on the FIRST attempt with
# zero retry delay, exactly as before this fix.
_START_PROOF_TRANSIENT_CODES = frozenset({
    "PIXHAWK_UNAVAILABLE", "PIXHAWK_TIMEOUT", "PIXHAWK_READBACK_PARTIAL",
    "PIXHAWK_READBACK_STALE", "ROUTE_HASH_STALE", "ROUTE_HASH_UNAVAILABLE",
})


# Vehicle-side legacy operator mission-label namespace exclusion (mission
# binding/reproof identity bug root cause): see planning_package.
# is_legacy_operator_mission_label's module-level comment for the full
# rationale. Re-exported here under the controller's existing private-helper
# naming convention so every call site below reads the same as before.
_is_legacy_operator_mission_label = planning_package.is_legacy_operator_mission_label


def _readback_diag(readback: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Compact freshness/provenance diagnostics off a Pixhawk mission readback
    dict -- proof_source/refresh_generation/age_s plus the cached/stale/
    refreshing/busy flags -- so a Start proof-acquisition attempt (successful
    or not) always reports WHERE its evidence came from, never just whether it
    passed. Never raises; an absent/malformed readback yields all-None."""
    rb = readback if isinstance(readback, dict) else {}
    return {
        "proof_source": rb.get("proof_source"),
        "refresh_generation": rb.get("refresh_generation"),
        "age_s": rb.get("age_s"),
        "observed_at": rb.get("observed_at"),
        "cached": rb.get("cached"),
        "stale": rb.get("stale"),
        "refreshing": rb.get("refreshing"),
        "busy": rb.get("busy"),
    }



def _is_timeout(exc: Exception) -> bool:
    """Whether an exception from a read is a transient timeout (eligible for one
    bounded retry) rather than a hard failure. Recognises requests' Timeout and
    the stdlib TimeoutError, and falls back to the type name / message so a
    duck-typed fake gateway can raise a timeout without importing requests."""
    try:
        import requests
        if isinstance(exc, requests.exceptions.Timeout):
            return True
    except Exception:
        pass
    if isinstance(exc, TimeoutError):
        return True
    return "timeout" in type(exc).__name__.lower() or "timed out" in str(exc).lower()


class MissionExecutionController:
    def __init__(
        self,
        cfg: Optional[me_config.MissionExecutionConfig] = None,
        gateway: Any = None,
        status_store: Optional["StatusStore"] = None,
        event_callback: Optional[Callable[[str, str, str], None]] = None,
        replan_status_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        replan_reset_fn: Optional[Callable[[], Dict[str, Any]]] = None,
        experiment_reset_fn: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = None,
        recorder: Any = None,
    ):
        import time
        self.cfg = cfg or me_config.DEFAULT
        self.gateway = gateway
        self._store = status_store
        self._event_cb = event_callback
        self._replan_status_fn = replan_status_fn
        # Bounded internal reset hooks Stop calls to reset state that lives OUTSIDE
        # this controller's own architecture rather than duplicating that logic
        # here (task): reset the replanning transaction / trigger latch, and clear
        # an active simulated experiment injection so the next test starts clean.
        # Injected (duck-typed, no import) so tests exercise Stop with no real
        # replan controller / injection store; wired to replan.reset /
        # experiment_injection.clear in local_agent.py.
        self._replan_reset_fn = replan_reset_fn
        self._experiment_reset_fn = experiment_reset_fn
        self._clock = clock or time.time
        # Thesis Experiment Recorder (experiment_recorder.py) -- OBSERVATIONAL
        # only, duck-typed/injected exactly like the reset hooks above so tests
        # never need a real recorder. Every call site wraps this in try/except
        # and never inspects/depends on its return value (task section 0: a
        # recorder failure must never influence mission execution).
        self._recorder = recorder

        self._action_lock = threading.Lock()   # one operation at a time
        self._state_lock = threading.Lock()     # guards the fields below

        self._state = NOT_READY
        self._active_operation_id: Optional[str] = None
        self._mission_id: Optional[str] = None
        self._original_route_hash: Optional[str] = None
        self._active_route_hash: Optional[str] = None
        self._verified_home: Optional[Dict[str, float]] = None
        self._home_verification_distance_m: Optional[float] = None
        self._last_mode: Optional[str] = None
        self._last_sequence: Optional[int] = None
        self._last_count: Optional[int] = None
        self._sequence_before_pause: Optional[int] = None
        self._sequence_at_resume: Optional[int] = None
        self._first_sequence_after_resume: Optional[int] = None
        self._continuation_verified: Optional[bool] = None
        self._start_ts: Optional[float] = None
        self._pause_ts: Optional[float] = None
        self._resume_ts: Optional[float] = None
        self._last_error: Optional[Dict[str, Any]] = None
        self._last_authority: Optional[str] = None
        self._history: List[Dict[str, Any]] = []

        # Sleep hook for the progression watch -- real time.sleep in production
        # (the watch runs on the operation worker, never the main loop), an
        # injectable no-op/clock-advance in tests so the full deadline can be
        # exercised deterministically without wall-clock waiting.
        self._sleep = time.sleep

        # Operator-facing launch phase + last progression evidence + the
        # immutable Start operation snapshot (task safety/reliability item 1).
        self._phase: Optional[str] = None
        self._progression_evidence: Optional[Dict[str, Any]] = None
        self._start_snapshot: Optional[Dict[str, Any]] = None
        # Machine-readable evidence of the most recent Stop transaction (hold /
        # restore / rewind / reset / authority / readiness). Runtime-only, like the
        # Start snapshot: it describes THIS process's Stop and is never persisted or
        # resurrected after a restart. Surfaced in status() as `stop`.
        self._stop_evidence: Optional[Dict[str, Any]] = None
        # Post-restart reconciliation record: the prior persisted state treated as
        # evidence, and the fresh-evidence outcome. Populated only by
        # recover_after_restart(); surfaced in status() so an operator can see that
        # a displayed state was re-proved (or could not be) after a restart.
        self._recovery: Optional[Dict[str, Any]] = None
        # Bounded, single-flight retry of restart reconciliation while stuck in
        # RECOVERY_PENDING (see _maybe_retry_recovery). Runtime-only bookkeeping:
        # never persisted -- a fresh process always starts a fresh retry cadence.
        self._last_recovery_retry_ts: Optional[float] = None
        self._recovery_retry_inflight = False

        # Passive readiness proof (READY-state correction). The full read-only
        # Start prerequisite proof is expensive (it includes a live Pixhawk
        # readback), so it is evaluated on a throttled background refresh and its
        # result cached here for status(). READY/can_start is asserted only when
        # `_readiness_ready` is True; otherwise `_readiness_reason` says why.
        self._readiness_ready = False
        # Evidence axis (task section 3): all read-only mission/package/Pixhawk/
        # position evidence proven, regardless of the control-authority axis. Drives
        # `start_eligible` so an OPERATOR-authority pre-Start state (the Operator
        # hands off LOCAL_AGENT before invoking Scout Start) reads as "ready, waiting
        # for authority", not "un-startable". `_readiness_ready` requires BOTH axes.
        self._readiness_evidence_ready = False
        # A transient freshness/read gap while already proven READY reports
        # CHECKING (proof refreshing) WITHOUT tearing down readiness, so READY
        # does not oscillate for an unchanged mission (task section 7 / 10).
        self._readiness_checking = False
        self._readiness_reason: Optional[str] = None
        self._readiness_detail: Optional[Dict[str, Any]] = None
        self._readiness_mission_id: Optional[str] = None
        self._readiness_original_hash: Optional[str] = None
        self._readiness_active_hash: Optional[str] = None
        self._last_readiness_eval_ts: Optional[float] = None
        # Edge-triggered stale-terminal-replan reset for a FRESH mission's
        # readiness proof (task: pre-Start OPERATOR-authority replan lifecycle).
        # Tracks the (mission_id, original_route_hash) generation for which
        # _reset_replan() has already been fired by _apply_readiness_proof_locked,
        # so neither a 5-second poll loop nor a LATER full-READY proof for the
        # SAME generation re-fires it. Runtime-only (never persisted -- a fresh
        # process always re-proves and may reset once more on its first proof,
        # exactly the startup case this exists for); cleared whenever execution
        # state is invalidated (rearm / Stop / a new package replaces the
        # previous one) so a genuinely NEW generation can fire again.
        self._replan_reset_evidence_generation: Optional[tuple] = None
        # One-shot suppression for the readiness re-proof Stop always triggers
        # right after its OWN unconditional _reset_replan() call (task:
        # existing Stop reset semantics, section 9 -- "Existing rearm and Stop
        # reset semantics remain valid"). Stop hands authority back to OPERATOR
        # BEFORE that re-proof, so it would otherwise land squarely on the new
        # evidence-proven/authority-pending edge and fire a REDUNDANT second
        # reset for a status Stop already just reset. Set immediately after
        # Stop's own reset call (see _run_stop), consumed (cleared) by the very
        # next fresh-evidence-reset judgement regardless of outcome -- so it
        # only ever suppresses that ONE specific re-proof, never a later,
        # independent one for a genuinely new mission/generation.
        self._skip_next_fresh_evidence_reset = False
        self._readiness_refresh_inflight = False

        # Continuously-updated, ADVISORY mission-energy-feasibility evidence
        # (mission_feasibility.py), pushed in by the caller every iteration via
        # update_energy_feasibility() -- see that method's docstring. None
        # ("not yet evaluated") never blocks Start on its own; the AUTHORITATIVE
        # check that can reject a Start always re-evaluates fresh inline in
        # _run_start (see _evaluate_feasibility).
        self._feasibility: Optional[Dict[str, Any]] = None

        # Continuously-updated, OBSERVATIONAL/ADVISORY continuous risk
        # assessment (risk_model.py), pushed in by the caller every iteration
        # via update_risk_assessment() -- same cached-display idiom as
        # _feasibility above. This controller performs NO risk computation to
        # produce it, gates NOTHING on it (this task is explicitly advisory
        # only -- no CONTINUE/WARN/HOLD/REPLAN/RETURN policy wiring yet), and
        # makes no vehicle read/write here. None means "not yet evaluated".
        self._risk: Optional[Dict[str, Any]] = None

        # Replanning handoff / arrival-monitor bookkeeping.
        self._replanning_active = False
        self._last_replan_fsm: Optional[str] = None
        self._arrival_since: Optional[float] = None
        self._distance_to_home_m: Optional[float] = None
        self._arrival_confirmed = False
        self._final_loiter_verified: Optional[bool] = None
        self._final_hold_started = False
        # Normal ORIGINAL-mission completion monitor (task section 3): the
        # persistence timer / confirmation for reaching the final executable route
        # item under AUTO and holding, distinct from the return-to-Home arrival
        # monitor above. Runtime-only; never persisted (a fresh process re-derives
        # completion from fresh evidence, never resurrects a prior "completing").
        self._completion_since: Optional[float] = None
        self._completion_confirmed = False
        self._completion_evidence: Optional[Dict[str, Any]] = None
        # Set by on_new_package_stored() when a newly verified original package
        # arrives while execution is active and is therefore NOT adopted (task
        # section 2). Runtime-only diagnostic; never persisted.
        self._package_conflict: Optional[Dict[str, Any]] = None

        if self._store is not None:
            self._store.load_into(self)

    # ── Restart safety ────────────────────────────────────────────────────────
    def recover_after_restart(self) -> None:
        """Reconcile any persisted execution state against FRESH local vehicle
        evidence. Persisted state is evidence of the prior run, never proof of the
        current vehicle state, so it is never trusted verbatim.

        Two classes of persisted state need recovery:

        * A transitional operation-in-flight state (``_INTERRUPTIBLE_STATES``): an
          operation was interrupted at an unknowable point -> fail closed to
          ``FAILED`` (``UNKNOWN_AFTER_RESTART``); never resumed.
        * A STABLE autonomous state (``_LIVE_STATES``: RUNNING / PAUSED /
          RETURNING_HOME / HOME_ARRIVAL_PENDING): the Pixhawk may legitimately
          remain ARMED/AUTO while only the Raspberry Pi / Local Agent process
          restarted. Enter an explicit ``RECOVERY_PENDING`` reconciliation phase
          and re-prove from fresh local evidence BEFORE ever re-exposing an
          authoritative live state. This phase issues NO vehicle write -- it never
          resends ARM/AUTO merely because persisted state said RUNNING.

        In BOTH cases the persisted ``active_operation_id`` is cleared: a restart
        from a stable state must never resurrect an old operation id as active."""
        with self._state_lock:
            prior = self._state
            prior_op_id = self._active_operation_id
            # A restart never leaves an operation "currently executing": clear the
            # active id here (the terminal/history evidence keeps it separately).
            self._active_operation_id = None

            if prior in _INTERRUPTIBLE_STATES:
                self._state = FAILED
                self._last_error = {
                    "code": "UNKNOWN_AFTER_RESTART",
                    "message": (
                        f"Local Agent restarted while a mission-execution operation was in "
                        f"{prior}; it was interrupted at an unknown point and was NOT "
                        "resumed. Re-issue from a known state."
                    ),
                    "interrupted_state": prior,
                }
                self._persist_locked()
                print(f"[MISSION_EXEC] recovered interrupted operation in {prior} -> FAILED "
                      "UNKNOWN_AFTER_RESTART (not resumed)")
                return

            if prior not in _LIVE_STATES:
                # Idle / terminal / already-RECOVERY_PENDING: nothing to reconcile,
                # but a cleared op id (if any) must still be persisted.
                if prior_op_id is not None:
                    self._persist_locked()
                return

            # Stable autonomous state: mark execution UNVERIFIED and enter the
            # explicit recovery phase before any evidence is read. The persisted
            # identity/hashes are retained as the EXPECTED mission to reconcile
            # against; they are not yet re-asserted as authoritative.
            self._recovery = {
                "prior_state": prior,
                "prior_operation_id": prior_op_id,
                "prior_mission_id": self._mission_id,
                "prior_original_route_hash": self._original_route_hash,
                "expected_mode": _RECONCILABLE_STABLE_MODE.get(prior),
                "reconciled": None,
            }
            self._state = RECOVERY_PENDING
            self._phase = RECOVERY_PENDING
            self._continuation_verified = None
            self._persist_locked()
            prior_mission_id = self._mission_id
            prior_hash = self._original_route_hash
        print(f"[MISSION_EXEC] persisted stable state {prior} -> RECOVERY_PENDING; "
              "reconciling against fresh vehicle evidence (no vehicle write issued)")
        self._reconcile_stable_state(prior, prior_mission_id, prior_hash)

    def _reconcile_stable_state(self, prior_state: str, prior_mission_id: Optional[str],
                                prior_hash: Optional[str]) -> None:
        """Re-prove a persisted stable autonomous state from FRESH, read-only local
        vehicle evidence. Restores the authoritative prior state ONLY when current
        evidence proves the expected mission is still loaded and autonomous
        continuation is consistent; otherwise fails closed into RECOVERY_PENDING.
        Issues NO vehicle write (never ARM/AUTO)."""
        expected_mode = _RECONCILABLE_STABLE_MODE.get(prior_state)
        if expected_mode is None:
            # RETURNING_HOME / HOME_ARRIVAL_PENDING: an in-progress return cannot be
            # positively re-proven safe after a cold start. This is a DEFINITIVE
            # inability to establish safe continuation (not a temporary read gap),
            # so if the vehicle is still armed/AUTO it is physically safe-held.
            self._fail_recovery(
                "RECOVERY_NOT_RECONCILABLE",
                f"persisted {prior_state} cannot be auto-reconciled after restart; "
                "the operator must re-issue from a known state", definitive=True)
            return

        # Read-only identity/consistency proof: usable stored package, package
        # mission_id == expected, package route hash == FRESH Pixhawk readback hash,
        # Pixhawk reachable/complete/fresh, LOCAL_AGENT authority, fresh position +
        # mode. Performs NO vehicle write, so it is safe to run at startup. Its
        # `transient` flag distinguishes a TEMPORARY/unavailable read gap (Pixhawk
        # unreachable, stale readback, authority unknown -> no vehicle write) from a
        # DEFINITIVE contradiction (mission/hash/package mismatch -> physical hold).
        proof = self._resolve_start_prerequisites(prior_mission_id)
        if not proof["ok"]:
            self._fail_recovery(
                proof["code"],
                f"restart reconciliation could not re-prove the mission ({proof['message']})",
                detail=proof.get("detail"), authority=proof.get("authority"),
                definitive=not proof.get("transient", False))
            return
        binding = proof["binding"]
        snap = proof.get("snapshot")

        # The mission identity / original route hash must still match what was
        # bound before the restart (package mismatch / hash mismatch -> a definitive
        # contradiction -> fail closed, physically holding if still armed/AUTO).
        if prior_mission_id and binding["mission_id"] != prior_mission_id:
            self._fail_recovery(
                "RECOVERY_MISSION_ID_MISMATCH",
                f"reconciled mission id {binding['mission_id']!r} != persisted "
                f"{prior_mission_id!r}", authority=proof.get("authority"), definitive=True)
            return
        if prior_hash and binding["original_route_hash"] != prior_hash:
            self._fail_recovery(
                "RECOVERY_ROUTE_HASH_MISMATCH",
                f"reconciled route hash {binding['original_route_hash']} != persisted "
                f"{prior_hash}", authority=proof.get("authority"), definitive=True)
            return

        # Fresh armed evidence -- never fabricated; a stale heartbeat reads as
        # unavailable (None), not armed. The two non-armed cases are distinct:
        #
        #  * armed is None  -> UNKNOWN/stale read gap. A TEMPORARY unavailability,
        #    not a contradiction: never write, stay RECOVERY_PENDING so the bounded
        #    retry can reconcile once fresh evidence arrives.
        #  * armed is False -> FRESH, definite DISARMED. The prior autonomous run is
        #    NOT continuing -- a DEFINITIVE contradiction. Because the vehicle is
        #    already disarmed there is nothing under autonomous propulsion to hold,
        #    so NO LOITER is sent; recovery exits RECOVERY_PENDING into the rearmable
        #    SUSPENDED state (see _fail_recovery) for the operator to re-issue.
        armed = self._fresh_armed(snap)
        if armed is None:
            self._fail_recovery(
                "RECOVERY_ARMED_UNCONFIRMED",
                "fresh armed state is unavailable or stale; not restoring an autonomous state",
                authority=proof.get("authority"), definitive=False)
            return
        if armed is False:
            self._fail_recovery(
                "RECOVERY_DISARMED",
                f"persisted {prior_state} but fresh evidence shows the vehicle DISARMED; "
                "the prior autonomous run is not continuing",
                authority=proof.get("authority"), definitive=True)
            return

        # The vehicle must actually be in the mode the persisted stable state
        # implies (RUNNING->AUTO, PAUSED->LOITER). A vehicle now in MANUAL/HOLD/etc.
        # is NOT a healthy continuation -- a definitive contradiction (the physical
        # hold only fires if fresh state is still armed/AUTO, so a vehicle already
        # in a non-AUTO mode is not redundantly re-LOITERed).
        if snap is None or snap.mode_name != expected_mode:
            self._fail_recovery(
                "RECOVERY_MODE_MISMATCH",
                f"vehicle mode is {getattr(snap, 'mode_name', None)!r}, not the expected "
                f"{expected_mode!r} for a persisted {prior_state}",
                authority=proof.get("authority"), definitive=True)
            return

        # An autonomous state requires a verified Home; armed/AUTO without a verified
        # Home is a definitive contradiction -> physically safe-held.
        if not self._home_ready():
            self._fail_recovery(
                "RECOVERY_HOME_UNVERIFIED",
                "restart reconciliation requires a verified Home; none is verified",
                authority=proof.get("authority"), definitive=True)
            return

        # All fresh evidence agrees: restore the authoritative stable state WITHOUT
        # issuing any vehicle write (no ARM/AUTO -- the vehicle already holds it).
        with self._state_lock:
            self._state = prior_state
            self._phase = prior_state if prior_state in PHASE_LABELS else None
            self._mission_id = binding["mission_id"]
            self._original_route_hash = binding["original_route_hash"]
            self._active_route_hash = binding["active_route_hash"]
            self._last_authority = proof.get("authority")
            self._continuation_verified = True
            self._last_error = None
            if isinstance(self._recovery, dict):
                self._recovery["reconciled"] = True
                self._recovery["reconciled_to"] = prior_state
                self._recovery["reconciled_at"] = round(self._clock(), 3)
            entry = {"from": RECOVERY_PENDING, "to": prior_state,
                     "reason": "Restart reconciliation confirmed a healthy autonomous state "
                               "from fresh evidence (no ARM/AUTO re-issued).",
                     "at": round(self._clock(), 3), "operation_id": None}
            self._history.append(entry)
            del self._history[:-50]
            self._persist_locked()
        self._emit("mission_execution_recovered",
                   f"Reconciled persisted {prior_state} to authoritative {prior_state} from fresh "
                   "vehicle evidence (no ARM/AUTO re-issued).", "info")
        print(f"[MISSION_EXEC] recovery reconciled {prior_state}: fresh evidence proves the "
              "expected mission continues -- authoritative state restored, no vehicle write issued")

    def _fail_recovery(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None,
                       authority: Optional[str] = None, definitive: bool = False) -> None:
        """Fail-closed outcome of restart reconciliation.

        A TEMPORARY / unavailable read gap (``definitive=False`` -- Pixhawk
        unreachable, stale readback, authority/armed unknown) never touches the
        vehicle: it stays in the explicit non-authoritative RECOVERY_PENDING state
        so the next attempt (once evidence is fresh) can reconcile.

        A DEFINITIVE contradiction (``definitive=True`` -- mission/hash/package
        mismatch, wrong mode, Home unverified, disarmed, unreconcilable return) fails
        closed and, crucially, EXITS RECOVERY_PENDING -- it is proven, not merely
        unavailable, so retrying cannot change the verdict:

          * Vehicle still ARMED and under autonomous propulsion (AUTO): request the
            existing safety-exempt verified LOITER hold. Once LOITER is verified ->
            SUSPENDED (operator rearm required). If LOITER cannot be verified the
            vehicle may still be under propulsion, so it stays RECOVERY_PENDING
            (fail closed) and the retry re-attempts the hold.
          * Vehicle NOT armed+AUTO (already disarmed / MANUAL / a non-AUTO mode):
            there is nothing under autonomous propulsion to hold, so NO LOITER is
            sent -> SUSPENDED directly (rearmable). This is the reproduced case:
            persisted RUNNING, fresh MANUAL + disarmed must NOT LOITER and must NOT
            stay pending forever.

        It NEVER sends ARM or AUTO, NEVER auto-disarms, and does not LOITER merely
        because persisted state said RUNNING -- the hold is driven strictly by fresh
        armed+AUTO evidence."""
        safe_hold_verified = None      # None = not attempted; True/False = LOITER result
        held_from_mode = None
        if definitive:
            # Fresh read to decide the PHYSICAL response. A read is always allowed;
            # a write is attempted only on confirmed armed + autonomous mode.
            snap = self._read_snapshot_safe()
            armed = self._fresh_armed(snap)
            mode = getattr(snap, "mode_name", None)
            if armed is True and mode in _AUTONOMOUS_EXECUTION_MODES:
                held_from_mode = mode
                # Serialize with the shared write arbiter, then issue the
                # safety-exempt verified LOITER (authority-exempt by design -- we
                # never weaken authority globally, only reuse LOITER's existing
                # high-priority-hold semantics). Never ARM/AUTO/disarm.
                token = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
                try:
                    safe_hold_verified = self._ensure_loiter() if token is not None else False
                finally:
                    if token is not None:
                        write_arbiter.release(token)

        held = safe_hold_verified is True
        with self._state_lock:
            if authority is not None:
                self._last_authority = authority
            self._continuation_verified = None
            prior_state = self._recovery.get("prior_state") if isinstance(self._recovery, dict) else None
            if not definitive:
                # TEMPORARY / unavailable evidence: no vehicle write was issued and
                # nothing is proven either way. Stay in the non-authoritative
                # RECOVERY_PENDING state so the bounded periodic retry re-attempts
                # reconciliation once fresh evidence becomes available.
                final_state = RECOVERY_PENDING
                err_code = code
                err_msg = message
            elif safe_hold_verified is False:
                # DEFINITIVE contradiction, vehicle still ARMED+AUTO, but the LOITER
                # safe-hold could NOT be verified -- it may still be under autonomous
                # propulsion. Stay pending (fail closed) so the retry re-attempts the
                # hold; never declare a rearmable state while a hold is unconfirmed.
                final_state = RECOVERY_PENDING
                err_code = "RECOVERY_SAFE_HOLD_UNVERIFIED"
                err_msg = (f"{message} -- vehicle appeared ARMED/AUTO but the LOITER safe-hold could "
                           "NOT be verified; the vehicle may still be under autonomous propulsion. "
                           "Operator intervention required.")
            elif held:
                # DEFINITIVE contradiction, held in verified LOITER -> rearmable.
                final_state = SUSPENDED
                err_code = code
                err_msg = (f"{message} -- vehicle was still ARMED in {held_from_mode}; placed in "
                           "verified LOITER safe-hold. Operator rearm/re-issue required.")
            else:
                # DEFINITIVE contradiction with NOTHING to physically hold (vehicle
                # not ARMED+AUTO -- already disarmed / MANUAL / a non-AUTO mode). No
                # LOITER is sent; the prior autonomous run is definitively not
                # continuing, so exit RECOVERY_PENDING into the rearmable SUSPENDED
                # state for the operator to prepare a fresh run.
                final_state = SUSPENDED
                err_code = code
                err_msg = (f"{message} -- vehicle is not under autonomous propulsion; no safe-hold "
                           "needed. Operator rearm/re-issue required.")
            self._last_error = {"code": err_code, "message": err_msg, "prior_state": prior_state}
            if detail is not None:
                self._last_error["detail"] = detail
            if isinstance(self._recovery, dict):
                self._recovery["reconciled"] = False
                self._recovery["reason"] = err_code
                self._recovery["safe_hold"] = ("VERIFIED_LOITER" if held
                                               else "LOITER_UNVERIFIED" if safe_hold_verified is False
                                               else "NONE")
        # Record the outcome transition (history + persist) via the normal machine;
        # SUSPENDED is terminal-rearmable, RECOVERY_PENDING stays non-authoritative.
        self._transition(final_state, err_msg, terminal=(final_state == SUSPENDED))
        if held:
            self._emit("mission_execution_recovery_safe_hold",
                       f"Restart reconciliation failed closed: {err_msg}", "warning")
            print(f"[MISSION_EXEC] recovery definitive contradiction ({code}) with vehicle ARMED/"
                  f"{held_from_mode}: issued verified LOITER safe-hold -> SUSPENDED (no ARM/AUTO/disarm)")
        elif final_state == SUSPENDED:
            self._emit("mission_execution_recovery_contradiction",
                       f"Restart reconciliation found a definitive contradiction: {err_msg}", "warning")
            print(f"[MISSION_EXEC] recovery definitive contradiction ({err_code}): vehicle not "
                  "ARMED/AUTO, no safe-hold needed -> SUSPENDED (rearmable, no ARM/AUTO/disarm)")
        else:
            self._emit("mission_execution_recovery_unverified",
                       f"Restart reconciliation did not confirm a healthy autonomous state: {err_msg}",
                       "warning")
            print(f"[MISSION_EXEC] recovery unverified ({err_code}): {err_msg} -- staying "
                  f"{final_state}, will retry on the next due tick, no ARM/AUTO issued")

    def _maybe_retry_recovery(self, now: float) -> None:
        """Bounded, single-flight retry of restart reconciliation while the
        controller is stuck in RECOVERY_PENDING.

        Reconciliation at startup can fail on TEMPORARY/unavailable evidence -- most
        commonly the vehicle Flask service on 127.0.0.1:8080 not being up yet when
        the Local Agent starts after a reboot (systemd ordering is deliberately not
        relied upon). Without this, that one-shot failure would leave the controller
        permanently in RECOVERY_PENDING even after 8080 later becomes healthy. This
        re-runs the SAME read-only reconciliation on a bounded cadence until the
        state is either restored (fresh proof) or exits into a rearmable state
        (definitive contradiction).

        Driven from the main-loop tick (observe()), NOT a free-running background
        thread: it is throttled to ``recovery_retry_interval_s``, guarded so only one
        attempt runs at a time (``_recovery_retry_inflight``), fires only while the
        state is RECOVERY_PENDING with a RECONCILABLE prior state, and stops the
        moment reconciliation succeeds or transitions out of pending. The
        reconciliation read (a ~2.5 s Pixhawk readback) is issued WITHOUT holding the
        state lock, and is run off the tick on a daemon thread -- exactly the
        readiness-refresh pattern -- so it never stalls telemetry nor blocks the HTTP
        /status endpoint (status() only briefly takes the state lock). A 0 interval
        runs synchronously for deterministic tests."""
        with self._state_lock:
            if self._state != RECOVERY_PENDING:
                return
            rec = self._recovery
            if not isinstance(rec, dict):
                return
            prior_state = rec.get("prior_state")
            # Only a reconcilable stable state (RUNNING/PAUSED) can make progress by
            # retrying. A non-reconcilable pending (e.g. an interrupted RETURNING_HOME
            # whose safe-hold could not be verified) will never reconcile from fresh
            # reads, so it is left for the operator rather than retried forever.
            if prior_state not in _RECONCILABLE_STABLE_MODE:
                return
            if self._recovery_retry_inflight:
                return
            interval = self.cfg.recovery_retry_interval_s
            due = (self._last_recovery_retry_ts is None
                   or (now - self._last_recovery_retry_ts) >= interval)
            if not due:
                return
            self._recovery_retry_inflight = True
            prior_mission_id = rec.get("prior_mission_id")
            prior_hash = rec.get("prior_original_route_hash")
        if self.cfg.recovery_retry_interval_s <= 0:
            # Synchronous mode (tests): reconcile now, in-line and deterministic.
            self._retry_recovery(prior_state, prior_mission_id, prior_hash, now)
            return
        threading.Thread(target=self._retry_recovery,
                         args=(prior_state, prior_mission_id, prior_hash, now),
                         daemon=True).start()

    def _retry_recovery(self, prior_state: str, prior_mission_id: Optional[str],
                        prior_hash: Optional[str], now: float) -> None:
        """Body of one recovery-retry attempt (see _maybe_retry_recovery). Re-runs
        the read-only reconciliation and always clears the in-flight guard when
        done, so the next due tick can retry if the controller is still pending."""
        try:
            with self._state_lock:
                # Re-check under the lock: an operator action, or the initial
                # startup reconciliation, may have moved us out of RECOVERY_PENDING
                # after this attempt was scheduled -- never reconcile a non-pending
                # state (that could clobber a live/terminal state).
                if self._state != RECOVERY_PENDING:
                    return
                self._last_recovery_retry_ts = now
            self._reconcile_stable_state(prior_state, prior_mission_id, prior_hash)
        finally:
            with self._state_lock:
                self._recovery_retry_inflight = False

    # ── Passive per-iteration observation + replanning handoff ────────────────
    def observe(self, snapshot, replan_status: Optional[Dict[str, Any]] = None,
                now: Optional[float] = None) -> Dict[str, Any]:
        """Called every main-loop iteration with the current immutable snapshot
        and the replanning controller's status. Updates mode/sequence
        observations, applies the replanning handoff, and advances the
        return-to-Home arrival monitor. Never itself performs a vehicle write --
        it returns {"final_hold": bool}; the caller launches run_final_hold() on
        a thread when that is True, so a multi-second LOITER never blocks the
        main loop."""
        now = self._clock() if now is None else now
        with self._state_lock:
            self._last_mode = snapshot.mode_name
            self._last_sequence = snapshot.current_sequence
            self._last_count = snapshot.mission_count
            self._distance_to_home_m = snapshot.distance_to_home_m

        self._maybe_refresh_readiness(now)
        # Retry restart reconciliation if we are stuck in RECOVERY_PENDING because
        # the initial attempt hit temporary/unavailable evidence (e.g. Flask 8080
        # not up yet at boot). Bounded, single-flight, only while pending -- see
        # _maybe_retry_recovery. A no-op in every other state.
        self._maybe_retry_recovery(now)
        self._apply_replan_handoff(replan_status)

        # The arrival / completion monitors only run when no operation currently
        # holds the action lock (a write is in flight) -- observe() itself never
        # writes; it only SIGNALS the caller to launch run_final_hold() on a
        # thread. The return-to-Home arrival monitor and the normal-completion
        # monitor are mutually exclusive by state (RETURNING_HOME/HOME_ARRIVAL_
        # PENDING vs RUNNING), so at most one can signal.
        if self._action_lock.locked():
            return {"final_hold": False}
        arrival = self._advance_arrival_monitor(snapshot, now)
        if arrival.get("final_hold"):
            return arrival
        return self._advance_completion_monitor(snapshot, now)

    # ── Passive readiness proof (READY-state correction, task section 10) ─────
    def _maybe_refresh_readiness(self, now: float) -> None:
        """Trigger a throttled, background readiness re-proof. The proof is the
        full read-only Start prerequisite resolution (usable package, package/
        Pixhawk route-hash match, LOCAL_AGENT authority, fresh state) INCLUDING a
        live Pixhawk readback, so it is expensive; it is refreshed at most every
        `readiness_poll_interval_s` and off the main loop so a ~2.5 s readback
        never stalls telemetry, the replan handoff, or the arrival monitor. Only
        the idle NOT_READY/READY states are ever re-proved -- a live or terminal
        state is never disturbed by passive polling."""
        with self._state_lock:
            if self._state not in (NOT_READY, READY):
                return
            if self._readiness_refresh_inflight:
                return
            due = (self._last_readiness_eval_ts is None
                   or (now - self._last_readiness_eval_ts) >= self.cfg.readiness_poll_interval_s)
            if not due:
                return
            self._readiness_refresh_inflight = True
        if self.cfg.readiness_poll_interval_s <= 0:
            # Synchronous mode (tests): prove now, in-line and deterministic.
            self._refresh_readiness(now)
            return
        threading.Thread(target=self._refresh_readiness, args=(now,), daemon=True).start()

    def refresh_readiness(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Prove readiness synchronously and return the resulting status. Exposed
        for the operator/bench pre-flight and for deterministic testing; also the
        body the background refresh runs. Never enters FAILED -- a transient read
        failure while idle demotes to NOT_READY with a precise reason, not a
        terminal failure (task: passive polling must not FAIL)."""
        now = self._clock() if now is None else now
        with self._state_lock:
            self._readiness_refresh_inflight = True
        self._refresh_readiness(now)
        return self.status()

    def _refresh_readiness(self, now: float) -> None:
        try:
            with self._state_lock:
                state = self._state
            if state not in (NOT_READY, READY):
                return
            proof = self._resolve_start_prerequisites(None)
            reset_stale_replan = self._apply_readiness_proof_locked(proof, now)
            if reset_stale_replan:
                self._reset_replan()
        finally:
            with self._state_lock:
                self._readiness_refresh_inflight = False

    def _apply_readiness_proof_locked(self, proof: Dict[str, Any], now: float) -> bool:
        """Apply a completed _resolve_start_prerequisites() proof to the cached
        readiness fields. Factored out of _refresh_readiness so the on-demand
        reproof entry point (reprove_binding, below) mutates readiness evidence
        through the EXACT same logic as the passive background refresh -- never
        a second copy of this state machine. Callers own the
        `_readiness_refresh_inflight` flag around this call.

        Returns True when a stale HISTORICAL terminal replan status (left over
        from a PREVIOUS mission/attempt) should be reset -- see
        _maybe_mark_fresh_evidence_reset_locked for the exact edge/generation
        rule. This fires from EITHER of two distinct proof outcomes for the
        CURRENT (mission_id, original_route_hash) generation:

          * the full READY proof (evidence AND LOCAL_AGENT authority) -- the
            original pre-E2 replan-lifecycle edge; or
          * evidence alone freshly proven (mission/package/Pixhawk identity all
            agree) while the ONLY remaining Start blocker is the expected
            pre-Start OPERATOR-authority handoff (task section 3) -- a fresh
            mission must be able to clear a stale terminal replan latch without
            first requiring LOCAL_AGENT authority merely to rearm replan state;
            mission execution correctly stays NOT_READY in this case (authority
            is a genuine block on Start), only the stale replan latch clears.

        Never fires while execution is actually bound/running (this method only
        ever mutates state while self._state is NOT_READY/READY -- see the guard
        below), and never repeats for an unchanged (mission_id, hash)
        generation."""
        with self._state_lock:
            # Guard: an operation may have moved us out of the idle states
            # while the (potentially multi-second) proof was running -- never
            # clobber a live/terminal state from the background refresh.
            if self._state not in (NOT_READY, READY):
                return False
            self._last_readiness_eval_ts = now
            if proof.get("authority") is not None:
                self._last_authority = proof["authority"]
            # Consume Stop's one-shot post-reset suppression on THIS proof
            # judgement UNCONDITIONALLY -- regardless of which branch below
            # ultimately applies -- so a transient/failure outcome can never
            # strand it set and wrongly suppress a later, unrelated proof.
            suppress_fresh_evidence_reset = self._skip_next_fresh_evidence_reset
            self._skip_next_fresh_evidence_reset = False
            reset_stale_replan = False
            if proof["ok"]:
                b = proof["binding"]
                self._readiness_ready = True
                self._readiness_evidence_ready = True
                self._readiness_checking = False
                self._readiness_reason = None
                self._readiness_detail = None
                self._readiness_mission_id = b["mission_id"]
                self._readiness_original_hash = b["original_route_hash"]
                self._readiness_active_hash = b["active_route_hash"]
                if self._state == NOT_READY:
                    self._state = READY
                reset_stale_replan = self._maybe_mark_fresh_evidence_reset_locked(
                    b, suppress=suppress_fresh_evidence_reset)
            elif proof.get("evidence_ok") and proof.get("binding") is not None:
                # Evidence proven; the ONLY block is the authority axis (task
                # section 3) -- the Operator handoff has not happened yet.
                # execution_ready stays false (can't run now), but retain the
                # proven identity so start_eligible can report a ready mission
                # waiting only for LOCAL_AGENT authority. Never promote to READY.
                b = proof["binding"]
                self._readiness_ready = False
                self._readiness_evidence_ready = True
                self._readiness_checking = False
                self._readiness_reason = proof["code"]
                self._readiness_detail = proof.get("detail")
                self._readiness_mission_id = b["mission_id"]
                self._readiness_original_hash = b["original_route_hash"]
                self._readiness_active_hash = b["active_route_hash"]
                if self._state == READY:
                    self._state = NOT_READY
                reset_stale_replan = self._maybe_mark_fresh_evidence_reset_locked(
                    b, suppress=suppress_fresh_evidence_reset)
            elif proof.get("transient") and self._readiness_ready and self._state == READY:
                # A TEMPORARY freshness/read gap (an expired/refreshing cache,
                # a one-off read timeout) must NOT tear down a mission that was
                # already proven ready. Retain the last successful proof and
                # its bound identity, report CHECKING, and leave the state at
                # READY so it does not oscillate for an unchanged mission. An
                # explicit Start still re-proves synchronously and fail-closed,
                # so keeping can_start True here never bypasses the fresh proof.
                self._readiness_checking = True
                self._readiness_reason = proof["code"]
                self._readiness_detail = proof.get("detail")
            else:
                # A GENUINE (non-transient) EVIDENCE failure, or a transient one
                # before any proof was ever established -- demote and clear
                # identity (evidence is not proven, so not start-eligible).
                self._readiness_ready = False
                self._readiness_evidence_ready = False
                self._readiness_checking = False
                self._readiness_reason = proof["code"]
                self._readiness_detail = proof.get("detail")
                self._readiness_mission_id = None
                self._readiness_original_hash = None
                self._readiness_active_hash = None
                if self._state == READY:
                    self._state = NOT_READY
            self._persist_locked()
        return reset_stale_replan

    def _maybe_mark_fresh_evidence_reset_locked(self, binding: Dict[str, Any],
                                                suppress: bool = False) -> bool:
        """Edge-trigger for resetting a stale HISTORICAL terminal replan status
        left over from a PREVIOUS mission/attempt. Called from BOTH readiness-
        proof outcomes in _apply_readiness_proof_locked that mean "fresh mission
        evidence is proven, execution is idle and UNBOUND" (the caller only ever
        invokes this while self._state is NOT_READY/READY, which is itself proof
        binding is UNBOUND -- see _binding_block).

        Fires at MOST once per (mission_id, original_route_hash) generation --
        neither a throttled poll loop re-proving the SAME mission, nor a LATER
        proof that reaches full READY for the SAME generation after this already
        fired on the evidence-only edge, re-fires it. A genuinely NEW generation
        (different mission/hash, or the tracked generation cleared by rearm/Stop/
        a replacement package -- see _invalidate_execution_state_locked) can fire
        again.

        `suppress`, when True, means Stop's OWN unconditional _reset_replan()
        call (see _run_stop) already just reset this exact generation
        immediately before this re-proof -- the generation is marked as
        already-handled WITHOUT firing a second, redundant reset (Stop's
        existing reset semantics are the ONE reset for that transaction; see
        task section 9). The caller consumes the underlying one-shot flag
        exactly once per proof judgement regardless of outcome, so this never
        strands a later, independent generation suppressed.

        Never fires while the replan controller shows an ACTIVE transaction --
        a best-effort FIRST safety barrier from this controller's own last-
        observed replan FSM state; ReplanController.reset() itself refuses an
        active transaction as the authoritative SECOND barrier (never weakened
        here). MUST be called with self._state_lock held."""
        mission_id = binding.get("mission_id")
        route_hash = binding.get("original_route_hash")
        if not mission_id or not route_hash:
            return False
        generation = (mission_id, route_hash)
        if suppress:
            self._replan_reset_evidence_generation = generation
            return False
        if self._replan_reset_evidence_generation == generation:
            return False
        if self._last_replan_fsm in _ACTIVE_REPLAN_STATES:
            return False
        self._replan_reset_evidence_generation = generation
        return True

    # ── Read-only binding reproof (Operator "Full Refresh") ────────────────────
    # Package-identity evidence failures that MISSION_ROUTE_UNVERIFIED-style gaps
    # actually mean "no usable original-mission identity is stored/known" rather
    # than "the stored package disagrees with the vehicle".
    _REPROOF_NO_MISSION_CODES = frozenset({"PACKAGE_MISSION_ID_MISSING"})
    _REPROOF_NO_PACKAGE_CODES = frozenset({"NO_PLANNING_PACKAGE", "PACKAGE_ROUTE_HASH_MISSING"})

    def reprove_binding(self, expected_mission_id: Optional[str] = None) -> Dict[str, Any]:
        """Read-only, on-demand re-proof of mission-execution binding evidence
        against CURRENT evidence -- the Scout side of the Operator "Full
        Refresh" operation. Restores exactly the readiness evidence
        (`verified_route_hash`, resolved `mission_id`, `start_eligible`,
        `start_block_reason`) that the passive background readiness proof
        (_refresh_readiness, driven by observe()) already restores on its own
        throttled cadence -- this exposes that SAME proof synchronously and on
        demand, through the SAME _resolve_start_prerequisites() identity rule
        Start itself uses, and reports a precise, honest outcome from every
        execution state instead of silently no-op'ing outside NOT_READY/READY.

        NEVER uploads, clears, or writes a mission; NEVER Sets Home; NEVER ARMs,
        DISARMs, or changes mode; NEVER touches replanning; NEVER rewrites the
        stored planning package. Performs at most ONE proof-grade Pixhawk
        mission readback (the same bounded, single-retry readback
        _resolve_start_prerequisites always uses for Start) plus a package-file
        read -- no other I/O, and no vehicle write of any kind.

        `expected_mission_id`, when supplied, is an OPERATOR-SIDE CONSTRAINT
        only -- NEVER proof (task section 13). It never feeds into the
        evidence proof itself, which always resolves identity fresh from the
        CURRENT package + Pixhawk exactly as a real Start would; it is only
        compared against the resolved identity AFTER the proof completes, so a
        caller's mistaken expectation can never corrupt or clear already-good
        internal readiness evidence for a DIFFERENT, still-genuinely-current
        mission.

        `binding_state` in the result is the SAME literal FSM concept
        status()["binding"] already reports: BOUND means a LIVE execution has
        this mission_id bound, so it legitimately continues to read UNBOUND
        for a not-yet-started mission whether or not this reproof succeeded --
        reproving before a Start can never fabricate a live-execution claim
        this controller was never asked to make. The fields that actually
        change on a successful reproof are `verified_route_hash`, `mission_id`,
        `start_eligible`, `execution_ready`, `can_start`, and
        `start_block_reason` -- exactly the evidence a real Start (and
        therefore a real BOUND) depends on."""
        def _result(outcome: str, accepted: bool, reason: str, **kw) -> Dict[str, Any]:
            return self._reproof_result(outcome, accepted=accepted, reason=reason,
                                        expected_mission_id=expected_mission_id, **kw)

        if self._action_lock.locked():
            return _result("BUSY", accepted=False,
                           reason="a mission-execution operation is in progress")
        with self._state_lock:
            state = self._state
            inflight = self._readiness_refresh_inflight
        if inflight:
            return _result("BUSY", accepted=False,
                           reason="a readiness re-proof is already in progress")

        if state in _LIVE_STATES:
            # A live original mission is already bound (or, in the
            # STALE_MISMATCH edge case, was superseded by a later upload while
            # running). Reprove NEVER re-runs the identity proof against a live
            # execution -- it only reports the existing, already-authoritative
            # live binding read-only (task sections 10 / 25 / 26).
            binding = self._binding_block()
            if binding["binding_state"] == "BOUND":
                if expected_mission_id and expected_mission_id != binding["bound_original_mission_id"]:
                    return _result(
                        "MISSION_ID_MISMATCH", accepted=True, binding=binding,
                        reason=(f"expected mission_id {expected_mission_id!r} does not match the "
                                f"live bound mission {binding['bound_original_mission_id']!r}"))
                return _result(
                    "ALREADY_PROVEN", accepted=True, binding=binding,
                    reason="a live original mission is already bound; no re-proof performed "
                           "while running")
            return _result(
                "LIFECYCLE_NOT_REPROVABLE", accepted=True, binding=binding,
                reason=f"execution state {state} binding is {binding['binding_state']}; reprove "
                       "never mutates a live execution's binding")

        if state not in (NOT_READY, READY):
            # SUSPENDED / FAILED / COMPLETED_HOLD / RECOVERY_PENDING / any
            # transient in-flight operation phase: an explicit rearm() (or the
            # in-flight operation settling) is required first. Rearm is the
            # existing, deliberate UNBIND step (task section 1); reprove never
            # performs it implicitly -- see rearm()'s own docstring.
            return _result(
                "LIFECYCLE_NOT_REPROVABLE", accepted=True,
                reason=f"execution state {state} is not reprovable; rearm (or let the in-flight "
                       "operation settle) before requesting a binding reproof")

        # NOT_READY / READY: the reprovable idle case. Snapshot "before" so an
        # already-current mission reports ALREADY_PROVEN, not REPROVED (task
        # section 14 idempotence).
        with self._state_lock:
            was_proven = bool(self._readiness_ready or self._readiness_evidence_ready)
            prior_mission_id = self._readiness_mission_id
            prior_hash = self._readiness_original_hash
            self._readiness_refresh_inflight = True
        now = self._clock()
        try:
            proof = self._resolve_start_prerequisites(None)
        except Exception as e:
            with self._state_lock:
                self._readiness_refresh_inflight = False
            return _result("INTERNAL_ERROR", accepted=True, reason=f"reprove failed: {e}")
        try:
            reset_stale_replan = self._apply_readiness_proof_locked(proof, now)
            if reset_stale_replan:
                self._reset_replan()
        finally:
            with self._state_lock:
                self._readiness_refresh_inflight = False

        binding = self._binding_block()
        resolved_mission_id = (proof.get("binding") or {}).get("mission_id")
        detail = proof.get("detail") or {}
        pixhawk_hash = detail.get("pixhawk_route_content_hash")

        if expected_mission_id and resolved_mission_id and expected_mission_id != resolved_mission_id:
            return _result(
                "MISSION_ID_MISMATCH", accepted=True, binding=binding, pixhawk_route_hash=pixhawk_hash,
                reason=(f"expected mission_id {expected_mission_id!r} does not match the current "
                        f"proven identity {resolved_mission_id!r}"))

        if proof["ok"] or (proof.get("evidence_ok") and proof.get("binding") is not None):
            # Evidence proven (Start may still be blocked separately by the
            # authority axis -- start_block_reason in the result reflects that
            # honestly; reproving binding evidence never grants authority).
            unchanged = (was_proven and prior_mission_id == resolved_mission_id
                        and prior_hash == binding.get("verified_route_hash"))
            pixhawk_hash = pixhawk_hash or binding.get("verified_route_hash")
            return _result(
                "ALREADY_PROVEN" if unchanged else "REPROVED", accepted=True,
                binding=binding, pixhawk_route_hash=pixhawk_hash,
                reason="current planning package and fresh Pixhawk route evidence agree")

        if proof.get("transient"):
            return _result(
                "EVIDENCE_UNAVAILABLE", accepted=True, binding=binding, pixhawk_route_hash=pixhawk_hash,
                reason=proof.get("message") or proof["code"], reason_code=proof["code"])

        code = proof["code"]
        if code in self._REPROOF_NO_MISSION_CODES:
            outcome = "NO_CURRENT_MISSION"
        elif code in self._REPROOF_NO_PACKAGE_CODES:
            outcome = "NO_CURRENT_PACKAGE"
        elif detail.get("readiness_state") == planning_package.READY_PACKAGE_STALE:
            # A COMPLETED, FRESH proof shows the STORED PACKAGE itself no longer
            # matches the mission actually on the vehicle -- the Operator's
            # separate explicit package-sync remedy is required (task section
            # 7 / 36); reprove never rewrites the package to fix this itself.
            outcome = "PACKAGE_MISMATCH"
        else:
            # Package looks internally usable, but the FRESH live Pixhawk
            # readback right now simply does not match it (or another
            # Pixhawk-side evidence gate -- route count, mission-verified,
            # vehicle mission-id conflict -- failed).
            outcome = "PIXHAWK_MISMATCH"
        return _result(
            outcome, accepted=True, binding=binding, pixhawk_route_hash=pixhawk_hash,
            reason=proof.get("message") or code, reason_code=code)

    def _reproof_result(self, outcome: str, accepted: bool, reason: str,
                        binding: Optional[Dict[str, Any]] = None,
                        pixhawk_route_hash: Optional[str] = None,
                        reason_code: Optional[str] = None,
                        expected_mission_id: Optional[str] = None) -> Dict[str, Any]:
        """Build the reprove_binding() response. Re-reads status() fresh so
        start_eligible/execution_ready/can_start/start_block_reason reflect
        readiness AFTER this reproof was applied, via the SAME derivation
        status() always uses -- never a duplicated formula (task section 17)."""
        st = self.status()
        b = binding if binding is not None else st["binding"]
        return {
            "accepted": accepted,
            "outcome": outcome,
            "ok": outcome in ("REPROVED", "ALREADY_PROVEN"),
            "read_only": True,
            "execution_state": st["state"],
            "mission_id": st["mission_id"],
            "expected_mission_id": expected_mission_id,
            "package_mission_id": b.get("package_mission_id"),
            "package_route_hash": b.get("package_route_hash"),
            "pixhawk_route_hash": pixhawk_route_hash,
            "verified_route_hash": b.get("verified_route_hash"),
            "binding_state": b.get("binding_state"),
            "bound_original_mission_id": b.get("bound_original_mission_id"),
            "start_eligible": st["start_eligible"],
            "execution_ready": st["execution_ready"],
            "can_start": st["can_start"],
            "start_block_reason": st["start_block_reason"],
            "reason": reason,
            "reason_code": reason_code,
            "evaluated_at": self._clock(),
        }

    # ── Read-only Start identity proof (task section 2 root-cause fix) ────────
    def _resolve_start_prerequisites(self, requested_mission_id: Optional[str]) -> Dict[str, Any]:
        """Resolve and PROVE the active original mission identity from the right
        authorities -- never from vehicle_state.mission.current_mission_id, which
        the Pixhawk/MAVLink mission does not carry and which is therefore null on
        the bench. Read-only: performs NO vehicle write, so it is safe both as the
        Start gate and as passive readiness evaluation.

        Identity is resolved in order from: (1) the requested mission_id, (2) the
        stored usable planning package mission_id, (3) the stored package route
        hash, (4) a fresh live Pixhawk route readback hash. The proof requires:
            requested_mission_id (when supplied) == package.mission_id
            package.route_hash               == fresh Pixhawk readback route hash
        plus a stored+usable package, package readiness mission_verified &
        route_hash_match, a reachable/complete/non-empty Pixhawk readback,
        LOCAL_AGENT authority, and fresh position+mode evidence.

        vehicle_state.mission.current_mission_id is only SUPPORTING evidence: null
        never fails the proof; a non-null value that disagrees with the resolved
        identity fails closed with MISSION_ID_CONFLICT -- UNLESS that value is
        Flask's legacy `/start_mission` operator-typed sensor-logging label
        (see _is_legacy_operator_mission_label), which lives in a wholly
        separate identifier namespace from the canonical msn-* mission
        identity and is therefore never comparable to it either way.

        Returns a dict: {ok, code, message, detail, transient, to_state, binding,
        authority, snapshot}. `transient` marks a temporary read failure (the
        caller must not enter FAILED on it during passive polling); `to_state` is
        the safe outcome an explicit Start should transition to on failure."""
        # (1)-(3) Package identity: stored, usable, mission id, route hash.
        package = planning_package.load()
        if not planning_package.is_usable(package):
            return self._prereq_fail("NO_PLANNING_PACKAGE",
                                     "no usable stored planning package", FAILED)
        pkg_mid = package.get("mission_id")
        pkg_hash = package.get("original_route_hash") or package.get("route_hash")
        if not pkg_mid:
            return self._prereq_fail("PACKAGE_MISSION_ID_MISSING",
                                     "stored planning package has no mission_id", FAILED)
        if not pkg_hash:
            return self._prereq_fail("PACKAGE_ROUTE_HASH_MISSING",
                                     "stored planning package has no route hash", FAILED)
        if requested_mission_id and requested_mission_id != pkg_mid:
            return self._prereq_fail(
                "MISSION_ID_MISMATCH",
                f"requested mission_id {requested_mission_id!r} != package mission_id {pkg_mid!r}",
                FAILED, detail={"requested_mission_id": requested_mission_id,
                                "package_mission_id": pkg_mid})
        resolved_mid = requested_mission_id or pkg_mid

        # Fresh vehicle state (one bounded retry on a transient read timeout).
        vehicle_state, state_err = self._read_state_with_retry()
        if state_err is not None:
            code, message = state_err
            return self._prereq_fail(code, message, FAILED, transient=True)
        snapshot = self._build_snapshot(vehicle_state)

        # Control authority (fresh, narrow read) -- evaluated as a SEPARATE axis
        # from the mission/package/Pixhawk EVIDENCE below (task section 3). The
        # Operator Start transaction performs the LOCAL_AGENT authority handoff
        # BEFORE invoking the Scout Start lifecycle, so OPERATOR authority by
        # itself must not make a mission look fundamentally un-startable: we still
        # PROVE all the read-only evidence (it is a read, never a write), and only
        # at the end fail the full proof if authority is not LOCAL_AGENT. This
        # keeps exactly ONE current_authority() read in the proof and the same
        # AUTHORITY_NOT_LOCAL_AGENT / AUTHORITY_UNKNOWN codes and SUSPENDED
        # to_state Start already returned -- but lets `start_eligible` report that
        # the evidence is proven and only the authority handoff is pending.
        authority = None
        authority_fail = None  # (code, message, transient) recorded, decided at the end
        try:
            authority = self.gateway.current_authority()
            allowed, _ = autonomy_gate.check_autonomous_write_authority(authority)
            if not allowed:
                authority_fail = ("AUTHORITY_NOT_LOCAL_AGENT",
                                  f"control authority is {authority!r}, not LOCAL_AGENT", False)
        except Exception as e:
            authority_fail = ("AUTHORITY_UNKNOWN", f"could not read control authority: {e}", True)

        # (4) Fresh live Pixhawk mission readback (one bounded retry on timeout).
        readback, rb_err = self._read_pixhawk_with_retry()
        if rb_err is not None:
            code, message = rb_err
            return self._prereq_fail(code, message, FAILED, transient=True, authority=authority)
        if not isinstance(readback, dict) or readback.get("reachable") is False:
            return self._prereq_fail(
                "PIXHAWK_UNAVAILABLE",
                f"Pixhawk mission readback unreachable: {(readback or {}).get('error')}",
                FAILED, transient=True, authority=authority,
                readback_diagnostics=_readback_diag(readback))
        if readback.get("partial"):
            return self._prereq_fail("PIXHAWK_READBACK_PARTIAL",
                                     "Pixhawk mission readback is partial", FAILED,
                                     transient=True, authority=authority,
                                     readback_diagnostics=_readback_diag(readback))
        # Freshness gate: a cache-first GET /agent/pixhawk_mission may return a
        # stale or still-refreshing readback. Such evidence must never satisfy
        # the Start identity proof / READY -- fail closed, transiently (the next
        # poll, after the requested refresh completes, will carry fresh proof).
        fresh, fresh_reason = planning_package.readback_is_fresh(readback)
        if not fresh:
            return self._prereq_fail("PIXHAWK_READBACK_STALE",
                                     f"Pixhawk mission readback is not fresh enough to prove "
                                     f"Start readiness: {fresh_reason}", FAILED,
                                     transient=True, authority=authority,
                                     detail={"freshness": fresh_reason},
                                     readback_diagnostics=_readback_diag(readback))
        rb_count = readback.get("route_waypoint_count")
        if not rb_count or rb_count <= 0:
            return self._prereq_fail("ROUTE_COUNT_ZERO",
                                     "Pixhawk route waypoint count is zero", FAILED,
                                     authority=authority)

        # Package readiness proof against this live readback: the authoritative
        # route-hash / mission-verified gate (never weakened here).
        readiness = planning_package.build_readiness(readback)
        rstate = readiness.get("state")
        if rstate in (planning_package.READY_MISSING, planning_package.READY_INVALID):
            return self._prereq_fail("PACKAGE_NOT_READY",
                                     f"planning package not ready for start: {rstate}",
                                     FAILED, detail={"readiness_state": rstate},
                                     authority=authority)
        if not readiness.get("route_hash_match") or pkg_hash != readback.get("route_content_hash"):
            # CODE/TRANSIENT-CLASSIFICATION correction: planning_package.py's
            # own build_readiness() docstring reserves READY_PACKAGE_STALE
            # EXCLUSIVELY for "a COMPLETED, FRESH proof [that] shows a genuine
            # mismatch" -- i.e. definitive bad evidence, never a refresh/
            # staleness race. It must therefore map to the SAME definitive
            # "ROUTE_HASH_MISMATCH" code every other/unrecognized rstate
            # already falls through to below (the dict's own default) --
            # never to "ROUTE_HASH_STALE", which _START_PROOF_TRANSIENT_CODES
            # treats as retryable. Only the two genuinely freshness-axis
            # states (no fresh evidence YET, no proven mismatch either way)
            # get that code. `transient` below was already correctly False
            # for READY_PACKAGE_STALE, so this is a code-string/naming
            # correction only -- no retry-eligibility behavior changes.
            code = {planning_package.READY_HASH_UNAVAILABLE: "ROUTE_HASH_UNAVAILABLE",
                    planning_package.READY_PROOF_STALE: "ROUTE_HASH_STALE",
                    planning_package.READY_REFRESHING: "ROUTE_HASH_STALE"}.get(rstate, "ROUTE_HASH_MISMATCH")
            # Freshness-axis states (no fresh evidence yet) are transient; a
            # genuine PLANNING_PACKAGE_STALE mismatch is not. (In the Start path
            # the freshness gate above already fails closed before this, so a
            # non-fresh readback rarely reaches here -- this stays correct if it
            # ever does.)
            transient = rstate in (planning_package.READY_HASH_UNAVAILABLE,
                                   planning_package.READY_PIXHAWK_UNAVAILABLE,
                                   planning_package.READY_REFRESHING,
                                   planning_package.READY_PROOF_STALE)
            return self._prereq_fail(
                code,
                f"stored package route hash {pkg_hash} != fresh Pixhawk route hash "
                f"{readback.get('route_content_hash')}",
                FAILED, transient=transient, authority=authority,
                detail={"package_route_hash": pkg_hash,
                        "pixhawk_route_content_hash": readback.get("route_content_hash"),
                        "readiness_state": rstate},
                readback_diagnostics=_readback_diag(readback))
        if not readiness.get("mission_verified"):
            return self._prereq_fail("MISSION_NOT_VERIFIED",
                                     "package readiness does not confirm the mission is verified "
                                     "on the vehicle", FAILED, authority=authority,
                                     detail={"readiness_state": rstate,
                                             "pixhawk_route_content_hash":
                                                 readback.get("route_content_hash")},
                                     readback_diagnostics=_readback_diag(readback))

        # Fresh position + mode evidence.
        if not self._position_fresh(snapshot):
            return self._prereq_fail("POSITION_STALE_OR_INVALID",
                                     "current vehicle position is stale, missing, or invalid "
                                     "(null island)", FAILED, authority=authority,
                                     detail={"pixhawk_route_content_hash":
                                                 readback.get("route_content_hash")},
                                     readback_diagnostics=_readback_diag(readback))
        if snapshot.mode_name is None:
            return self._prereq_fail("PIXHAWK_STATE_UNAVAILABLE",
                                     "fresh Pixhawk mode/state is unavailable", FAILED,
                                     transient=True, authority=authority,
                                     detail={"pixhawk_route_content_hash":
                                                 readback.get("route_content_hash")},
                                     readback_diagnostics=_readback_diag(readback))

        # Supporting evidence only: vehicle_state.mission.current_mission_id --
        # and ONLY when it is not Flask's legacy operator sensor-logging label,
        # which is a different identifier namespace entirely and therefore
        # never valid evidence of a route-binding conflict (see
        # _is_legacy_operator_mission_label / the module-level comment above
        # it -- this is the mission binding/reproof identity bug's root
        # cause). A genuine disagreement in the COMPARABLE namespace (a bare
        # token, or a stale/previous canonical msn-* id) still fails closed
        # exactly as before.
        vehicle_mid = snapshot.mission_id
        if (vehicle_mid is not None and vehicle_mid != resolved_mid
                and not _is_legacy_operator_mission_label(vehicle_mid)):
            return self._prereq_fail(
                "MISSION_ID_CONFLICT",
                f"vehicle current_mission_id {vehicle_mid!r} conflicts with the resolved "
                f"mission identity {resolved_mid!r}",
                FAILED, authority=authority,
                detail={"vehicle_current_mission_id": vehicle_mid,
                        "resolved_mission_id": resolved_mid,
                        "pixhawk_route_content_hash": readback.get("route_content_hash")},
                readback_diagnostics=_readback_diag(readback))

        # All read-only EVIDENCE is proven at this point. The binding the evidence
        # would produce is fixed; whether Start can run RIGHT NOW depends only on
        # the authority axis.
        binding = {"mission_id": resolved_mid, "original_route_hash": pkg_hash,
                   "active_route_hash": pkg_hash}
        if authority_fail is not None:
            # Evidence proven but authority not LOCAL_AGENT (or unknown): the full
            # proof fails closed exactly as before -- Start still requires the
            # handoff -- but evidence_ok=True so start_eligible can be reported.
            code, message, transient = authority_fail
            return {"ok": False, "code": code, "message": message,
                    "detail": {"authority": authority}, "transient": transient,
                    "to_state": SUSPENDED, "binding": binding, "evidence_ok": True,
                    "authority": authority, "snapshot": snapshot}
        return {
            "ok": True, "code": None, "message": None, "detail": None,
            "transient": False, "to_state": None,
            "binding": binding,
            "evidence_ok": True,
            "authority": authority,
            "snapshot": snapshot,
            "readback_diagnostics": _readback_diag(readback),
        }

    def _acquire_start_proof(self, requested_mission_id: Optional[str]) -> Dict[str, Any]:
        """Bounded fresh-proof ACQUISITION phase around _resolve_start_
        prerequisites() (task: "READINESS RETRY RACE" fix) -- the one and only
        change from a one-shot proof read. Re-runs the full, read-only
        prerequisite proof (package, fresh vehicle state, authority, fresh
        Pixhawk mission readback, package/readback consistency) until it
        either succeeds, fails on a DEFINITIVE reason, or
        `cfg.start_proof_timeout_s` elapses -- but ONLY continues retrying
        while the failure is one of the exact transient "not fresh/available
        YET" codes in _START_PROOF_TRANSIENT_CODES (a background coordinator
        refresh genuinely in flight, a busy/refreshing/stale cache, or a
        transient read timeout). Every other failure -- route hash mismatch,
        missing/invalid package, MISSION_NOT_VERIFIED, authority violation,
        POSITION_STALE_OR_INVALID, MISSION_ID_CONFLICT/MISMATCH, ... -- returns
        on the FIRST attempt with zero retry delay, unchanged from before this
        fix. This is deliberately a loop around the EXISTING, already-proven
        prerequisite resolution (every underlying check is unchanged and
        unweakened) rather than a new parallel readiness implementation.

        Never blocks a gunicorn/API request thread in production: _run_start
        always executes on the controller's own action-lock-guarded operation
        call, exactly like the rest of the Start transaction.

        The returned proof dict carries the SAME shape _resolve_start_
        prerequisites returns, plus a "proof_acquisition" diagnostics block:
        attempts, elapsed_s, the last transient (code, message) seen (None if
        the very first attempt already resolved), and the FINAL readback's
        proof_source/refresh_generation/age_s (via _readback_diag, already
        present as proof["readback_diagnostics"])."""
        start = self._clock()
        deadline = start + self.cfg.start_proof_timeout_s
        attempts = 0
        last_transient: Optional[Dict[str, Any]] = None
        while True:
            attempts += 1
            proof = self._resolve_start_prerequisites(requested_mission_id)
            retryable = (not proof["ok"] and proof.get("transient")
                        and proof.get("code") in _START_PROOF_TRANSIENT_CODES)
            elapsed = round(self._clock() - start, 3)
            if not retryable:
                proof["proof_acquisition"] = {
                    "attempts": attempts, "elapsed_s": elapsed,
                    "last_transient_reason": last_transient,
                    "readback_diagnostics": proof.get("readback_diagnostics"),
                }
                if self._recorder is not None:
                    try:
                        self._recorder.record_event(
                            "START_PROOF_ACQUISITION", source="mission_execution_controller",
                            data={"requested_mission_id": requested_mission_id,
                                  "ok": proof["ok"], "code": proof.get("code"),
                                  **proof["proof_acquisition"]},
                            priority="high" if not proof["ok"] else "normal",
                        )
                    except Exception:
                        pass
                return proof
            last_transient = {"code": proof.get("code"), "message": proof.get("message")}
            if self._clock() >= deadline:
                proof["proof_acquisition"] = {
                    "attempts": attempts, "elapsed_s": elapsed,
                    "last_transient_reason": last_transient,
                    "readback_diagnostics": proof.get("readback_diagnostics"),
                    "timed_out": True,
                }
                if self._recorder is not None:
                    try:
                        self._recorder.record_event(
                            "START_PROOF_ACQUISITION", source="mission_execution_controller",
                            data={"requested_mission_id": requested_mission_id,
                                  "ok": False, "code": proof.get("code"),
                                  **proof["proof_acquisition"]},
                            priority="high",
                        )
                    except Exception:
                        pass
                return proof
            self._sleep(self.cfg.start_proof_poll_interval_s)

    @staticmethod
    def _prereq_fail(code: str, message: str, to_state: str, transient: bool = False,
                     detail: Optional[Dict[str, Any]] = None,
                     authority: Optional[str] = None,
                     readback_diagnostics: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # evidence_ok=False: an EVIDENCE-axis failure (package/Pixhawk/hash/
        # position/mode). Authority-axis failures are returned separately with
        # evidence_ok=True (task section 3). readback_diagnostics (READINESS
        # RETRY RACE fix): freshness/provenance evidence off whichever Pixhawk
        # readback this failure was evaluated against, when one was obtained --
        # None for a failure before any readback read (e.g. package errors).
        return {"ok": False, "code": code, "message": message, "detail": detail,
                "transient": transient, "to_state": to_state, "evidence_ok": False,
                "binding": None, "authority": authority, "snapshot": None,
                "readback_diagnostics": readback_diagnostics}

    def _read_state_with_retry(self):
        """Read /agent/state, allowing exactly ONE bounded retry on a transient
        read timeout (never on any other error, and never a write). Returns
        (vehicle_state, None) or (None, (code, message)) with STATE_TIMEOUT vs
        STATE_UNAVAILABLE distinguished from mission-identity errors."""
        try:
            return self.gateway.read_vehicle_state(), None
        except Exception as first:
            if not _is_timeout(first):
                return None, ("STATE_UNAVAILABLE", f"could not read vehicle state: {first}")
        try:
            return self.gateway.read_vehicle_state(), None
        except Exception as second:
            code = "STATE_TIMEOUT" if _is_timeout(second) else "STATE_UNAVAILABLE"
            return None, (code, f"could not read vehicle state after one retry: {second}")

    def _prove_pixhawk_readback(self):
        """Obtain a PROOF-GRADE Pixhawk readback for the identity/READY gate.

        Prefers the gateway's prove_pixhawk_mission_readback() -- which requests
        a coordinator refresh and waits (on this Local Agent thread, never a
        gunicorn thread) for the refresh generation to advance before returning
        -- so a cache-first GET /agent/pixhawk_mission cannot let a stale
        readback satisfy the proof. Falls back to pixhawk_mission_readback() for
        gateways that do not implement the proof method (its result is still
        subject to the freshness gate below)."""
        prove = getattr(self.gateway, "prove_pixhawk_mission_readback", None)
        return prove() if callable(prove) else self.gateway.pixhawk_mission_readback()

    def _read_pixhawk_with_retry(self):
        """Read a proof-grade Pixhawk mission readback, allowing one bounded
        retry on a transient read timeout. Returns (readback, None) or (None,
        (code, message))."""
        try:
            return self._prove_pixhawk_readback(), None
        except Exception as first:
            if not _is_timeout(first):
                return None, ("PIXHAWK_UNAVAILABLE", f"could not read Pixhawk mission: {first}")
        try:
            return self._prove_pixhawk_readback(), None
        except Exception as second:
            code = "PIXHAWK_TIMEOUT" if _is_timeout(second) else "PIXHAWK_UNAVAILABLE"
            return None, (code, f"could not read Pixhawk mission after one retry: {second}")

    def _apply_replan_handoff(self, replan_status: Optional[Dict[str, Any]]) -> None:
        if not replan_status:
            return
        fsm = replan_status.get("fsm_state")
        running = bool(replan_status.get("running"))
        active = running or fsm in _ACTIVE_REPLAN_STATES
        with self._state_lock:
            prev_active = self._replanning_active
            self._replanning_active = active and self._state in _LIVE_STATES
            edge_fsm = self._last_replan_fsm
            self._last_replan_fsm = fsm

        # React only on the replanning FSM's *edge* into a terminal state while
        # this controller holds a live mission -- never on a steady MONITORING.
        if fsm == edge_fsm:
            return
        if self._state not in _LIVE_STATES and not prev_active:
            return
        if fsm == _REPLAN_SUCCESS_STATE:
            with self._state_lock:
                if self._state in (RUNNING, PAUSED):
                    self._active_route_hash = replan_status.get("revised_mission_hash") or self._active_route_hash
                    self._replanning_active = False
                    self._final_hold_started = False
                    self._arrival_since = None
                    self._arrival_confirmed = False
                    self._final_loiter_verified = None
            self._transition(RETURNING_HOME,
                             "Replanning reached MONITORING_REVISED -- revised safe-return AUTO is running; "
                             "monitoring for arrival at Home.")
        elif fsm in _REPLAN_FAILURE_STATES:
            # P0-3: SAFE_HOLD is reached two structurally different ways --
            # (a) decision_policy.py authoritatively requested a HOLD-only
            # safety hold (e.g. a HIGH-risk communication-loss floor) and
            # replan_controller.py's _direct_safe_hold() never attempted
            # PLANNING/VALIDATING/UPLOAD at all, or (b) a real plan/validate/
            # upload/resume attempt ran and exhausted its retries. Only (b)
            # is a planner failure. replan_status["hold_only"] (set from
            # replan_controller's own _active_hold_only, bound at the exact
            # transaction that produced this terminal fsm) is the
            # authoritative signal for which one this was -- never inferred
            # from the fsm state name alone, which is identical either way.
            #
            # P0 SAFE_HOLD-invariant follow-up: a HOLD-only transaction can now
            # ALSO terminate on SUSPENDED instead of SAFE_HOLD -- when
            # replan_controller._acquire_hold_settle() could never positively
            # prove the physical hold within its bound, or its own final
            # defensive LOITER re-assertion failed to verify
            # (replan_controller._hold_not_proven(); codes
            # HOLD_SETTLE_TIMEOUT / LOITER_REASSERT_NOT_VERIFIED). That is
            # STILL "no replan attempted" -- no route was ever built/validated/
            # uploaded -- so it must not be reported as REPLANNING_NOT_
            # SUCCESSFUL either.
            #
            # "No replan attempted" is determined from hold_only ALONE:
            # replan_controller.py's own FSM structurally guarantees an
            # ACTION_REQUEST_HOLD transaction (_active_hold_only bound at
            # transaction start) can NEVER enter PLANNING/VALIDATING/UPLOAD --
            # see _run()'s hold_only branch, taken immediately after
            # HOLD_CONFIRMED, before the plan/validate/upload retry loop even
            # exists. retry_count is NOT usable evidence here: it counts
            # RETRIES within that loop, so retry_count == 0 is equally true of
            # a genuine first plan/validate/upload attempt that simply hasn't
            # retried yet -- it says nothing about whether an attempt
            # happened. The specific SAFE_HOLD vs.
            # SUSPENDED/HOLD_SETTLE_TIMEOUT/LOITER_REASSERT_NOT_VERIFIED code
            # only selects which precise, accurate wording to expose for the
            # same hold_only-authoritative "no replan attempted" fact.
            #
            # PAUSED-on-proven-hold fix (E3 field-run follow-up): fsm ==
            # "SAFE_HOLD" is, by replan_controller's own SAFE_HOLD invariant
            # (see that module's header comment), reached ONLY when the
            # physical hold has been POSITIVELY PROVEN (_acquire_hold_settle()
            # settled, or _direct_safe_hold()'s own defensive LOITER
            # re-assertion verified) -- never merely requested. A hold_only
            # transaction that landed there is therefore a successful,
            # deliberate, fully-proven controlled pause, not an execution
            # failure -- the SAME outcome an operator-issued Pause() reaches
            # (verified LOITER, mission/sequence retained), just triggered by
            # the decision policy instead of an explicit Pause call. It must
            # settle into PAUSED, reusing the EXACT SAME state Resume()/
            # Stop() already understand, rather than the fail-closed
            # SUSPENDED terminal. A hold_only transaction whose physical hold
            # could NOT be positively proven within its bound (fsm ==
            # "SUSPENDED", HOLD_SETTLE_TIMEOUT/LOITER_REASSERT_NOT_VERIFIED)
            # is UNPROVEN, not successful -- it still fails closed to
            # SUSPENDED exactly as before this fix, as does any genuinely
            # attempted-and-failed replan (hold_only False).
            hold_only = bool((replan_status or {}).get("hold_only"))
            replan_last_error = (replan_status or {}).get("last_error") or {}
            replan_error_code = replan_last_error.get("code")

            hold_only_proven = fsm == "SAFE_HOLD" and hold_only
            hold_proof_failed = (
                fsm == "SUSPENDED" and hold_only
                and replan_error_code in _HOLD_PROOF_FAILURE_CODES
            )

            # Best-effort FRESH vehicle-state read taken at the exact moment
            # this outcome is applied -- the SAME pattern _end_operation's
            # ensure_loiter branch already uses for its own terminal_evidence
            # (recorder/summary.json correctness fix, task section "RECORDER
            # / SUMMARY CHECK"). The `replan_status`-carrying snapshot passed
            # into observe() this cycle can predate the physical hold settling
            # (replan's own hold-settle wait runs on its OWN async transaction
            # thread, off this observe() call entirely -- see local_agent.py's
            # replan.observe()/mission_exec.observe() ordering), so reusing it
            # would risk exactly the stale-mode bug this read exists to avoid.
            # Never fabricated: a failed read leaves every derived field
            # explicitly None, same as _end_operation's own fallback.
            final_snap = None
            try:
                final_snap = self._build_snapshot(self.gateway.read_vehicle_state())
            except Exception:
                final_snap = None

            def _hold_evidence(to_state: str) -> Dict[str, Any]:
                seq_block = self._sequence_block()
                current_waypoint = final_snap.current_sequence if final_snap is not None else None
                mission_count = final_snap.mission_count if final_snap is not None else None
                return {
                    "mission_execution_state": to_state, "mission_execution_phase": None,
                    "final_mode": final_snap.mode_name if final_snap is not None else None,
                    "final_armed": final_snap.armed if final_snap is not None else None,
                    "final_authority": final_snap.control_authority if final_snap is not None else None,
                    "current_waypoint": current_waypoint if current_waypoint is not None else seq_block.get("current"),
                    "mission_count": mission_count if mission_count is not None else seq_block.get("count"),
                    "route_hash": self._active_route_hash, "mission_id": self._mission_id,
                }

            with self._state_lock:
                self._replanning_active = False
                if self._state in _LIVE_STATES:
                    if hold_only_proven:
                        self._last_error = None
                        # Retain the current sequence/progression the SAME way
                        # _run_pause() does for an operator-issued Pause, so
                        # Resume's continuation check has the same evidence to
                        # compare against regardless of which path paused it.
                        if final_snap is not None:
                            self._sequence_before_pause = final_snap.current_sequence
                            self._last_sequence = final_snap.current_sequence
                            self._last_count = final_snap.mission_count
                        self._pause_ts = self._clock()
                    elif hold_proof_failed:
                        self._last_error = {
                            "code": replan_error_code,
                            "message": (
                                "The decision policy requested a safety hold, but the physical "
                                "hold could not be positively proven within its bound "
                                f"({replan_last_error.get('message') or replan_error_code}); no "
                                "replanned return route was attempted (this is not a planner "
                                "failure). Mission execution is SUSPENDED and the original "
                                "mission is NOT automatically resumed."
                            ),
                            "replan_state": fsm,
                            "reason_codes": list((replan_status or {}).get("reason_codes") or []),
                        }
                    else:
                        self._last_error = {
                            "code": "REPLANNING_NOT_SUCCESSFUL",
                            "message": (f"Replanning ended in {fsm}; mission execution is SUSPENDED and the "
                                        "original mission is NOT automatically resumed."),
                            "replan_state": fsm,
                        }
            if self._state in _LIVE_STATES:
                if hold_only_proven:
                    reason = ("Decision policy requested a safety hold; the physical hold was "
                              "positively proven (verified LOITER + settle) and no replan was "
                              "attempted. Mission execution is PAUSED -- the original mission and "
                              "its current sequence are retained; the vehicle remains in verified "
                              "LOITER; communication recovering does NOT auto-resume it. An "
                              "explicit operator Resume or Stop is required.")
                    # terminal_evidence is not passed here: PAUSED is `settle`,
                    # not `terminal`, so _transition never consumes it (see its
                    # own docstring) -- Pause/Resume deliberately stay the same
                    # recorder run. The fresh evidence gathered above is still
                    # useful here for the retained sequence/progression fields
                    # (set under the lock above) and for _hold_evidence(SUSPENDED)
                    # in the fail-closed branch below.
                    self._transition(PAUSED, reason, settle=True)
                    self._emit("mission_execution_paused",
                               "Mission paused by a positively-proven communication-loss safety "
                               "hold (SAFE_HOLD, no replan attempted); vehicle remains in verified "
                               "LOITER pending an explicit operator Resume or Stop.", "info")
                else:
                    reason = ("Decision policy requested a safety hold, but the physical hold could "
                              "not be positively proven; suspending mission execution without "
                              "auto-resume."
                              if hold_proof_failed else
                              f"Replanning ended in {fsm}; suspending mission execution without auto-resume.")
                    self._transition(SUSPENDED, reason, terminal=True, terminal_evidence=_hold_evidence(SUSPENDED))

    def _advance_arrival_monitor(self, snapshot, now: float) -> Dict[str, Any]:
        with self._state_lock:
            state = self._state
        if state not in (RETURNING_HOME, HOME_ARRIVAL_PENDING):
            return {"final_hold": False}

        distance = snapshot.distance_to_home_m
        fresh = self._position_fresh(snapshot)
        within = distance is not None and distance <= self.cfg.home_arrival_radius_m
        near_final = self._near_final(snapshot)
        mode_ok = snapshot.mode_name in ("AUTO", "LOITER", "HOLD", None)
        condition = bool(within and fresh and near_final and mode_ok)

        with self._state_lock:
            if condition:
                if self._arrival_since is None:
                    self._arrival_since = now
                progress = now - self._arrival_since
                if state == RETURNING_HOME:
                    self._state = HOME_ARRIVAL_PENDING
                    self._persist_locked()
                if progress >= self.cfg.home_arrival_persistence_s and not self._final_hold_started:
                    self._arrival_confirmed = True
                    self._final_hold_started = True
                    signal = True
                else:
                    signal = False
            else:
                # A break in the condition (drifted outside, went stale) resets
                # the persistence timer -- one noisy inside-radius sample never
                # completes the mission.
                self._arrival_since = None
                self._arrival_confirmed = False
                if state == HOME_ARRIVAL_PENDING:
                    self._state = RETURNING_HOME
                    self._persist_locked()
                signal = False
        if signal:
            self._emit("mission_execution_arrival_confirmed",
                       f"Arrival at Home confirmed (within {self.cfg.home_arrival_radius_m} m for "
                       f"{self.cfg.home_arrival_persistence_s} s) -- requesting final LOITER hold.",
                       "info")
        return {"final_hold": signal}

    def _position_fresh(self, snapshot) -> bool:
        if snapshot.latitude is None or snapshot.longitude is None:
            return False
        if snapshot.latitude == 0.0 and snapshot.longitude == 0.0:
            return False
        age = snapshot.position_age_s
        return age is not None and age <= self.cfg.max_position_age_s

    def _near_final(self, snapshot) -> bool:
        """Whether the (revised) mission is at or near its final item. Missing
        sequence/count is treated as non-blocking -- distance/persistence remain
        the primary arrival evidence."""
        seq, count = snapshot.current_sequence, snapshot.mission_count
        if seq is None or count is None or count <= 0:
            return True
        return seq >= count - 1 - self.cfg.home_arrival_final_item_tolerance

    def _final_route_point(self) -> Optional[tuple]:
        """(lat, lon) of the last executable route waypoint in the stored package,
        or None when unavailable. Used only as an optional corroborating gate for
        normal completion; a missing point disables the position check, never
        forces a false completion."""
        route = (planning_package.load() or {}).get("route") or []
        for wp in reversed(route):
            lat, lon = wp.get("latitude"), wp.get("longitude")
            if lat is not None and lon is not None:
                return (lat, lon)
        return None

    def _telemetry_fresh(self, snapshot) -> bool:
        age = snapshot.telemetry_age_s
        return age is not None and age <= self.cfg.max_position_age_s

    def _advance_completion_monitor(self, snapshot, now: float) -> Dict[str, Any]:
        """Normal ORIGINAL-mission completion detector (task section 3).

        Closes the live-bench gap where a mission that reached its final waypoint
        (WP 14/14, remaining ~0, vehicle stopped) stayed RUNNING forever. Runs
        ONLY in the plain RUNNING state (never PAUSED, never while a replan
        transaction owns the vehicle, never while returning) and declares
        completion only on a DEFENSIBLE COMBINATION of fresh evidence held
        continuously for the persistence window -- never one fragile signal, and
        never merely because current_seq momentarily equals the last index:

          * sequence at/within mission_complete_final_item_tolerance of the last
            executable route item (a fresh, complete readback: count > 0);
          * fresh telemetry (sequence/mode evidence is not stale);
          * mode is an autonomous/hold mode (AUTO/LOITER/HOLD) -- an operator who
            took MANUAL control is NOT a completed autonomous mission;
          * optionally, position within mission_complete_position_radius_m of the
            final waypoint (when a radius is configured and both positions are
            available; if the radius is configured but position cannot be proven,
            completion is withheld -- fail closed, never guess);
          * the whole condition stable, continuously, for
            mission_complete_persistence_s.

        Returns {"final_hold": True} exactly once, when confirmed, so the caller
        launches run_final_hold() on a thread -> verified final LOITER ->
        COMPLETED_HOLD. It NEVER writes and NEVER auto-disarms. A broken sample
        (drifted, went stale, mode changed, seq not yet final) resets the timer."""
        with self._state_lock:
            state = self._state
        if state != RUNNING or self._is_replanning_active():
            if self._completion_since is not None or self._completion_confirmed:
                with self._state_lock:
                    self._completion_since = None
                    self._completion_confirmed = False
            return {"final_hold": False}

        seq, count = snapshot.current_sequence, snapshot.mission_count
        readback_ok = count is not None and count > 0 and seq is not None
        at_final = bool(readback_ok
                        and seq >= count - 1 - self.cfg.mission_complete_final_item_tolerance)
        telem_fresh = self._telemetry_fresh(snapshot)
        mode_ok = snapshot.mode_name in ("AUTO", "LOITER", "HOLD")

        # Optional position-near-final gate.
        radius = self.cfg.mission_complete_position_radius_m
        final_wp = self._final_route_point() if radius is not None else None
        position_distance_m = None
        if radius is None:
            position_ok = True
        elif final_wp is None:
            # No final waypoint geometry available -> cannot use the position gate;
            # do not let its absence block completion (the sequence/mode/readback/
            # persistence evidence still governs).
            position_ok = True
        elif self._position_fresh(snapshot):
            position_distance_m = geo.haversine_m(
                snapshot.latitude, snapshot.longitude, final_wp[0], final_wp[1])
            position_ok = position_distance_m <= radius
        else:
            position_ok = False  # radius required but position unprovable -> withhold

        condition = bool(at_final and readback_ok and telem_fresh and mode_ok and position_ok)
        evidence = {
            "at_final_item": at_final, "sequence": seq, "count": count,
            "telemetry_fresh": telem_fresh, "mode": snapshot.mode_name,
            "mode_ok": mode_ok, "position_ok": position_ok,
            "position_distance_m": (round(position_distance_m, 2)
                                    if position_distance_m is not None else None),
            "position_radius_m": radius,
        }
        signal = False
        with self._state_lock:
            if state != RUNNING:
                self._completion_since = None
                self._completion_confirmed = False
                self._completion_evidence = evidence
                return {"final_hold": False}
            self._completion_evidence = evidence
            if condition:
                if self._completion_since is None:
                    self._completion_since = now
                progress = now - self._completion_since
                if progress >= self.cfg.mission_complete_persistence_s and not self._final_hold_started:
                    self._completion_confirmed = True
                    self._final_hold_started = True
                    signal = True
            else:
                # A break in the condition resets the persistence timer -- one
                # transient final-sequence observation never completes the mission.
                self._completion_since = None
                self._completion_confirmed = False
        if signal:
            self._emit("mission_execution_completion_candidate",
                       f"Original mission reached its final item and held for "
                       f"{self.cfg.mission_complete_persistence_s} s -- requesting final LOITER "
                       "hold before declaring COMPLETED_HOLD.", "info")
        return {"final_hold": signal}

    # ── Start Mission (task section 2) ────────────────────────────────────────
    def start(self, mission_id: Optional[str] = None) -> Dict[str, Any]:
        if not self._action_lock.acquire(blocking=False):
            return self._busy_result("start")
        token = None
        try:
            with self._state_lock:
                state = self._state
            if state == RUNNING:
                return self._idempotent_result(
                    "start", "ALREADY_RUNNING",
                    "mission already running; not starting twice")
            if self._is_replanning_active():
                return self._rejected_result(
                    "start", "REPLANNING_ACTIVE",
                    "a replanning transaction is active; mission execution is not permitted")
            if not self.cfg.mission_execution_enabled:
                return self._rejected_result(
                    "start", "MISSION_EXECUTION_DISABLED",
                    "mission execution is disabled by configuration")
            if state not in (READY, NOT_READY):
                return self._rejected_result(
                    "start", "NOT_STARTABLE", f"cannot start from state {state}")
            token = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
            if token is None:
                return self._arbitration_busy_result("start")
            return self._run_start(mission_id)
        finally:
            if token is not None:
                write_arbiter.release(token)
            self._action_lock.release()

    def _run_start(self, requested_mission_id: Optional[str]) -> Dict[str, Any]:
        op_id = uuid.uuid4().hex
        previous = self._state
        with self._state_lock:
            self._active_operation_id = op_id
            self._last_error = None
        self._transition(START_REQUESTED, "Start Mission requested via Local Agent.")

        # ── Read-only identity proof (root-cause fix) ───────────────────────
        # Resolve the active original mission from the requested id + stored
        # package + package route hash + a FRESH Pixhawk readback hash -- NOT from
        # vehicle_state.mission.current_mission_id, which the MAVLink mission does
        # not carry (it is null on the bench). No vehicle write has occurred yet,
        # so any failure here fails closed with the vehicle untouched.
        #
        # Bounded fresh-proof ACQUISITION (task: "READINESS RETRY RACE" fix):
        # _acquire_start_proof re-runs this read-only proof, inside this SAME
        # Start transaction, for up to cfg.start_proof_timeout_s while (and
        # ONLY while) the failure is one of the exact transient "not fresh
        # yet" codes (a background Pixhawk-readback refresh genuinely in
        # flight / busy / stale cache) -- a definitive failure (hash mismatch,
        # invalid package, MISSION_NOT_VERIFIED, authority, ...) still returns
        # on the very first attempt with zero retry delay. See
        # _START_PROOF_TRANSIENT_CODES / _acquire_start_proof's own docstring.
        proof = self._acquire_start_proof(requested_mission_id)
        if not proof["ok"]:
            detail = proof.get("detail")
            acquisition = proof.get("proof_acquisition")
            if acquisition is not None:
                detail = dict(detail) if isinstance(detail, dict) else {}
                detail["proof_acquisition"] = acquisition
            return self._end_operation("start", op_id, previous, proof["to_state"],
                                       proof["code"], proof["message"], detail=detail)

        # Bind the execution-controller context BEFORE any mode/Home write. The
        # bound mission_id is the Operator/package identity; the route hashes come
        # from the immutable package (never invented, never written into MAVLink).
        binding = proof["binding"]
        proof_snap = proof.get("snapshot")
        with self._state_lock:
            self._mission_id = binding["mission_id"]
            self._original_route_hash = binding["original_route_hash"]
            self._active_route_hash = binding["active_route_hash"]
            self._last_authority = proof.get("authority")
            # (Task safety item 1) Immutable Start operation snapshot -- compact,
            # append-only evidence for the thesis: identity, expected hash, the
            # armed/position state we started from, authority before, and (filled
            # as phases complete) verified Home, baseline, authority after, and a
            # timestamp for every phase.
            self._progression_evidence = None
            # A fresh Start clears any prior Stop's evidence so status never shows a
            # stale `stop` block against a new run.
            self._stop_evidence = None
            self._start_snapshot = {
                "operation_id": op_id,
                "mission_id": binding["mission_id"],
                "expected_route_hash": binding["original_route_hash"],
                "initial_armed": getattr(proof_snap, "armed", None),
                "launch_position": {"latitude": getattr(proof_snap, "latitude", None),
                                    "longitude": getattr(proof_snap, "longitude", None)},
                "authority_before": proof.get("authority"),
                "verified_home": None,
                "authority_after": None,
                "baseline": None,
                "phase_timestamps": {START_REQUESTED: round(self._clock(), 3)},
            }

        # Experiment-recorder session lifecycle (task section 5): a recording
        # session begins automatically here -- the first point in the Start
        # pipeline where the ORIGINAL mission identity is freshly PROVEN, and
        # strictly before the first vehicle-changing write (ARM below). Fires
        # exactly once per genuine Start attempt that passed prerequisites
        # (never on a rejected/busy/idempotent-already-running response).
        # Best-effort/never blocks -- see experiment_recorder.py.
        if self._recorder is not None:
            try:
                self._recorder.start_run(
                    mission_id=binding["mission_id"],
                    original_route_hash=binding["original_route_hash"],
                    original_mission=self._original_mission_evidence(binding, proof_snap),
                    planning_package=planning_package.load(),
                )
                # Explicit, authoritative "this run represents the accepted
                # Start transaction" evidence (task section 7). The
                # transition INTO START_REQUESTED above (line ~1388) fired
                # before the recorder session existed yet -- recorder
                # initialisation deliberately stays HERE, after the ORIGINAL
                # mission identity is freshly proven and before the first
                # vehicle-changing write, so this event is recorded at
                # elapsed ~=0 instead of moving recorder init earlier.
                self._recorder.record_event(
                    "MISSION_START_REQUESTED", source="mission_execution_controller",
                    data={"operation_id": op_id, "from": previous,
                          "mission_id": binding["mission_id"],
                          "original_route_hash": binding["original_route_hash"]},
                    priority="high",
                )
            except Exception:
                pass

        # ── Mission-energy-feasibility gate, PHASE 1 (task: mission-energy-
        #    feasibility Home-semantics correction, sections 5/6/7; task: RTL
        #    Home / Start-readiness semantics correction) -- BEFORE ANY
        #    vehicle-changing write (ARM is next). Revalidates feasibility with
        #    FRESH evidence (proof_snap, just read by _resolve_start_
        #    prerequisites above) rather than trusting whatever a cached
        #    continuous-loop result said; a cached FEASIBLE followed by fresh
        #    evidence that is no longer feasible still rejects Start here.
        #    Read-only: no ARM, no AUTO, no Home write, no mission upload, no
        #    sequence change happens because of this check either way -- it
        #    only decides whether the write sequence below is allowed to begin
        #    at all.
        #
        #    TWO INDEPENDENT dimensions (task section 5), but NOT symmetric at
        #    THIS pre-Home point:
        #      * mission_feasible -- never depends on Home. Must be POSITIVELY
        #        True here (False OR unknown fails closed) -- an obviously
        #        infeasible mission is refused before ever touching the
        #        vehicle, exactly as before.
        #      * rtl_return_feasible -- depends on the CURRENT VERIFIED
        #        Pixhawk Home (mission_feasibility.py's `rtl_home`), which
        #        Start itself is about to establish (Set Home, below) when
        #        none is verified yet. Proven False (a Home IS already
        #        verified -- e.g. left over from a prior run -- and the RTL
        #        leg from here is provably unaffordable) still refuses Start
        #        here, with zero vehicle writes, exactly as before. Merely
        #        UNKNOWN (REASON_RTL_HOME_UNAVAILABLE because no Home is
        #        verified yet) is NOT a Start blocker at this point -- that is
        #        the expected pre-Start state the Start transaction exists to
        #        resolve, never "Start blocked: RTL Home unavailable". PHASE 2
        #        below re-proves BOTH dimensions fresh, positively, once Home
        #        is actually verified, strictly before AUTO -- the hard
        #        pre-AUTO gate is unchanged: AUTO never runs on an unknown or
        #        infeasible RTL return.
        feasibility = self._evaluate_feasibility(proof_snap, usv_id=getattr(proof_snap, "vehicle_id", None))
        if self._recorder is not None:
            try:
                self._recorder.record_event(
                    "MISSION_FEASIBILITY_CHECK", source="mission_execution_controller",
                    data={"operation_id": op_id, "mission_id": binding["mission_id"],
                          **feasibility.to_dict()},
                    priority="high",
                )
            except Exception:
                pass
        if feasibility.mission_feasible is not True or feasibility.rtl_return_feasible is False:
            if self._recorder is not None:
                try:
                    self._recorder.record_event(
                        "MISSION_START_REJECTED", source="mission_execution_controller",
                        data={"operation_id": op_id, "mission_id": binding["mission_id"],
                              "reason": feasibility.reason, **feasibility.to_dict()},
                        priority="high",
                    )
                except Exception:
                    pass
            return self._end_operation(
                "start", op_id, previous, FAILED, feasibility.reason,
                f"mission energy feasibility check failed: {feasibility.message}",
                detail=feasibility.to_dict())

        # (1) Ensure ARMED FIRST -- BEFORE any physical-hold LOITER (USV ordering
        # fix). All read-only preconditions that can be proven before arming are
        # already complete above; ARM is the FIRST vehicle-changing write so the
        # first LOITER we depend on as a safety hold occurs only after armed=true
        # is positively verified (a disarmed Pixhawk can report LOITER mode but
        # cannot use thrusters to hold station). Idempotent when already armed
        # (no duplicate ARM). On any ARM failure the vehicle is NOT sent LOITER as
        # a fake hold, Home is not set, AUTO is not entered, and it is never
        # auto-disarmed (unsafe on water).
        arm_fail = self._ensure_armed(op_id, previous)
        if arm_fail is not None:
            return arm_fail

        # (2) LOITER, verified WHILE ARMED -- the first launch safety hold. The
        # vehicle is armed at this point (already-armed or just-armed above), so
        # this is a physically meaningful station hold, not a disarmed mode-only
        # LOITER. Authority is re-checked immediately before the write.
        if not self._authorized():
            return self._end_operation("start", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "control authority is not LOCAL_AGENT before commanding LOITER")
        self._transition(START_HOLD_REQUESTED, "Requesting LOITER (armed) as the launch safety hold.")
        loiter = self._safe_write(self.gateway.command_loiter)
        hold_ok, hold_code, hold_msg = self._loiter_hold_verified(loiter)
        if not hold_ok:
            # A plain LOITER-not-verified after successful ARM re-asserts LOITER
            # (remain ARMED + LOITER, task's post-ARM safety policy). A vehicle
            # that DISARMED between ARM verification and here fails closed WITHOUT
            # re-commanding a hold it cannot physically honour -- never continue
            # to Home/AUTO, and never auto-disarm.
            return self._end_operation("start", op_id, previous, FAILED, hold_code, hold_msg,
                                       detail=loiter,
                                       ensure_loiter=(hold_code != "DISARMED_BEFORE_LOITER"))
        self._transition(START_HOLD_CONFIRMED,
                         "LOITER confirmed while armed -- vehicle is holding at launch position.")

        # (3) Set Home to current position, verify read-back.
        if not self._authorized():
            return self._end_operation("start", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "authority lost before Set Home", ensure_loiter=True)
        self._transition(SETTING_HOME, "Setting launch Home to the current verified vehicle position.")
        home = self._safe_write(lambda: self.gateway.set_home(
            command_id=op_id,
            tolerance_m=self.cfg.home_verification_tolerance_m,
            freshness_s=self.cfg.max_position_age_s))
        self._transition(VERIFYING_HOME, "Verifying Home read-back within tolerance.")
        if not self._home_set_verified(home):
            return self._end_operation("start", op_id, previous, FAILED, "SET_HOME_FAILED",
                                       (home.get("error") or {}).get("message", "Set Home not verified")
                                       if isinstance(home, dict) else "Set Home not verified",
                                       detail=_summarize_home(home), ensure_loiter=True)
        verified_home = home.get("home_position") or {}
        with self._state_lock:
            self._verified_home = {"latitude": verified_home.get("latitude"),
                                   "longitude": verified_home.get("longitude")}
            self._home_verification_distance_m = home.get("verification_distance_m")
            if self._start_snapshot is not None:
                self._start_snapshot["verified_home"] = dict(self._verified_home)

        # (4) Synchronize the planning package Home to the verified launch Home.
        self._transition(SYNCHRONIZING_PACKAGE,
                         "Synchronizing planning package Home to the verified launch Home.")
        synced = planning_package.update_home(self._verified_home)
        if synced is None:
            return self._end_operation("start", op_id, previous, FAILED, "PACKAGE_SYNC_FAILED",
                                       "could not synchronize planning package Home to the verified launch Home",
                                       ensure_loiter=True)
        # Re-check against the BOUND mission identity (not the vehicle's null
        # current_mission_id) so the post-sync package still describes the mission
        # we proved and bound above.
        consistency2, detail2 = planning_package.check_consistency(synced, self._mission_id)
        if consistency2 != planning_package.CONSISTENCY_OK or not planning_package.is_usable(synced):
            return self._end_operation("start", op_id, previous, FAILED, "PACKAGE_INCONSISTENT_AFTER_SYNC",
                                       f"planning package not usable/consistent after Home sync: {consistency2}",
                                       detail=detail2, ensure_loiter=True)

        # (4.5) Pre-flight safe-return geometry gate -- fail closed BEFORE AUTO.
        # safe_return_planner's emergency replan fails closed if the verified
        # launch Home cannot be proven inside the approved navigable_boundary or
        # an approved home_corridor (CODE_HOME_OUTSIDE_BOUNDARY / a mid-route
        # NAVIGABLE_BOUNDARY_VIOLATION). Discovering that only during a live
        # low-battery replan forces an unconstrained native-RTL fallback and a
        # suspended mission (REPLANNING_NOT_SUCCESSFUL). Checking it here, right
        # after Home is verified and synced, means Scout never launches into a
        # mission it cannot prove a safe return from.
        nav_boundary = synced.get("navigable_boundary") or []
        home_corridor = synced.get("home_corridor") or []
        if nav_boundary:
            home_latlon = (self._verified_home.get("latitude"), self._verified_home.get("longitude"))
            home_in_boundary = geo.point_in_polygon(home_latlon, nav_boundary)
            home_in_corridor = bool(home_corridor) and geo.point_in_polygon(home_latlon, home_corridor)
            if not home_in_boundary and not home_in_corridor:
                return self._end_operation(
                    "start", op_id, previous, FAILED, "HOME_OUTSIDE_APPROVED_GEOMETRY",
                    "verified launch Home lies outside the approved navigable_boundary and no "
                    "home_corridor contains it; a safe-return replan could not be proven, so "
                    "Start is refused before AUTO",
                    detail={"verified_home": dict(self._verified_home),
                            "home_in_navigable_boundary": home_in_boundary,
                            "home_in_home_corridor": home_in_corridor},
                    ensure_loiter=True)

        # ── Mission-energy-feasibility gate, PHASE 2 (task: RTL Home / Start-
        #    readiness semantics correction) -- the hard pre-AUTO Home gate.
        #    PHASE 1 above deliberately let an UNKNOWN rtl_return_feasible
        #    (no Home verified yet) through so Start could reach this point;
        #    that concession ends here. Home is now verified (Set Home,
        #    above) and proven inside the approved geometry -- re-prove BOTH
        #    feasibility dimensions from FRESH evidence (a fresh vehicle-state
        #    read, not proof_snap/the PHASE 1 result, which predate Set Home
        #    and may still carry no/stale Home evidence) and require BOTH
        #    positively True, the same fail-closed policy as PHASE 1 (False OR
        #    unknown on EITHER axis refuses Start) -- never proceed to AUTO on
        #    "we don't know". The vehicle is already ARMED and holding LOITER
        #    at this point, so a rejection here still fails closed to a
        #    confirmed safe hold (ensure_loiter=True), never AUTO and never an
        #    unsafe RTL.
        if not self._authorized():
            return self._end_operation("start", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "authority lost before the post-Home feasibility re-check",
                                       ensure_loiter=True)
        try:
            post_home_state = self.gateway.read_vehicle_state()
        except Exception as e:
            return self._end_operation(
                "start", op_id, previous, FAILED, "STATE_UNAVAILABLE",
                f"could not read fresh vehicle state to re-check mission energy feasibility "
                f"after Set Home: {e}", ensure_loiter=True)
        post_home_snapshot = self._build_snapshot(post_home_state)
        post_home_feasibility = self._evaluate_feasibility(
            post_home_snapshot, usv_id=getattr(post_home_snapshot, "vehicle_id", None))
        if self._recorder is not None:
            try:
                self._recorder.record_event(
                    "MISSION_FEASIBILITY_CHECK_POST_HOME", source="mission_execution_controller",
                    data={"operation_id": op_id, "mission_id": binding["mission_id"],
                          **post_home_feasibility.to_dict()},
                    priority="high",
                )
            except Exception:
                pass
        if post_home_feasibility.mission_feasible is not True or post_home_feasibility.rtl_return_feasible is not True:
            if self._recorder is not None:
                try:
                    self._recorder.record_event(
                        "MISSION_START_REJECTED", source="mission_execution_controller",
                        data={"operation_id": op_id, "mission_id": binding["mission_id"],
                              "reason": post_home_feasibility.reason, **post_home_feasibility.to_dict()},
                        priority="high",
                    )
                except Exception:
                    pass
            return self._end_operation(
                "start", op_id, previous, FAILED, post_home_feasibility.reason,
                f"mission energy feasibility check failed after Home verification: "
                f"{post_home_feasibility.message}",
                detail=post_home_feasibility.to_dict(), ensure_loiter=True)

        # (5) Capture the pre-AUTO progression baseline immediately before AUTO.
        # (Arming already happened first, above -- no AUTO may occur unless
        # armed=true was freshly verified before the launch LOITER.)
        if not self._authorized():
            return self._end_operation("start", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "authority lost before AUTO", ensure_loiter=True)
        baseline = self._capture_baseline()
        with self._state_lock:
            if self._start_snapshot is not None:
                self._start_snapshot["baseline"] = dict(baseline)
                self._start_snapshot["authority_after"] = self._last_authority

        # (6) AUTO, verified.
        self._transition(STARTING_AUTO, "Requesting AUTO on the original mission.")
        auto = self._safe_write(self.gateway.command_auto)
        if not self._verified(auto):
            return self._end_operation("start", op_id, previous, FAILED, "AUTO_NOT_VERIFIED",
                                       "could not confirm AUTO; restoring LOITER", detail=auto,
                                       ensure_loiter=True)

        # (7) Poll progression evidence until success or the FULL configured
        # deadline (root-cause fix: no early exit on a single inactive sample).
        self._transition(CONFIRMING_PROGRESSION,
                         "AUTO verified -- confirming the mission is actually progressing.")
        watch = self._watch_progression(baseline, self.cfg.start_progression_timeout_s)
        with self._state_lock:
            self._progression_evidence = watch
        if not watch.get("proven"):
            return self._end_operation(
                "start", op_id, previous, FAILED, watch.get("failure_code", "PROGRESSION_UNCONFIRMED"),
                watch.get("failure_message", "mission progression not confirmed; restoring LOITER"),
                detail=watch, ensure_loiter=True)

        with self._state_lock:
            self._start_ts = self._clock()
            self._last_error = None
        self._transition(RUNNING, "Original mission is running under AUTO -- progression proven.",
                         settle=True)
        self._emit("mission_execution_started",
                   f"Original mission started and progressing under AUTO ({watch.get('proof')}).", "info")
        return self._success_result("start", op_id, previous, RUNNING, verified_mode="AUTO",
                                     home_result=_summarize_home(home), prog=watch)

    # ── Pause Mission (task section 4) ────────────────────────────────────────
    def pause(self) -> Dict[str, Any]:
        if not self._action_lock.acquire(blocking=False):
            return self._busy_result("pause")
        token = None
        try:
            with self._state_lock:
                state = self._state
            if state == PAUSED:
                return self._idempotent_result("pause", "ALREADY_PAUSED",
                                                "mission already paused", verified_mode="LOITER")
            if self._is_replanning_active():
                return self._rejected_result("pause", "REPLANNING_ACTIVE",
                                              "a replanning transaction is active; pause is not permitted")
            if state != RUNNING:
                return self._rejected_result("pause", "NOT_PAUSABLE",
                                              f"cannot pause from state {state}")
            token = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
            if token is None:
                return self._arbitration_busy_result("pause")
            return self._run_pause()
        finally:
            if token is not None:
                write_arbiter.release(token)
            self._action_lock.release()

    def _run_pause(self) -> Dict[str, Any]:
        op_id = uuid.uuid4().hex
        previous = self._state
        with self._state_lock:
            self._active_operation_id = op_id
            self._last_error = None
        # Record the sequence/identity BEFORE requesting LOITER, so the retained
        # progress is captured even if the read after LOITER is noisy.
        try:
            pre_state = self.gateway.read_vehicle_state()
        except Exception as e:
            return self._end_operation("pause", op_id, previous, SUSPENDED, "STATE_UNAVAILABLE",
                                       f"could not read vehicle state before pause: {e}")
        pre_snap = self._build_snapshot(pre_state)
        with self._state_lock:
            self._sequence_before_pause = pre_snap.current_sequence

        if not self._authorized():
            return self._end_operation("pause", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "control authority is not LOCAL_AGENT before commanding LOITER")
        self._transition(PAUSE_REQUESTED, "Pause requested -- requesting LOITER while retaining mission.")
        loiter = self._safe_write(self.gateway.command_loiter)
        if not self._verified(loiter):
            return self._end_operation("pause", op_id, previous, FAILED, "LOITER_NOT_VERIFIED",
                                       "could not confirm LOITER for pause", detail=loiter)

        # Confirm the mission is still loaded and record the sequence during pause.
        try:
            post_state = self.gateway.read_vehicle_state()
        except Exception as e:
            return self._end_operation("pause", op_id, previous, SUSPENDED, "STATE_UNAVAILABLE",
                                       f"could not confirm mission loaded after LOITER: {e}")
        post_snap = self._build_snapshot(post_state)
        if not post_snap.mission_count or post_snap.mission_count <= 0:
            return self._end_operation("pause", op_id, previous, FAILED, "MISSION_NOT_LOADED",
                                       "mission is no longer loaded after requesting LOITER")
        with self._state_lock:
            self._pause_ts = self._clock()
            self._last_sequence = post_snap.current_sequence
            self._last_count = post_snap.mission_count
            self._last_error = None
        self._transition(PAUSED, "Mission paused in verified LOITER; mission and sequence retained.",
                         settle=True)
        self._emit("mission_execution_paused",
                   f"Mission paused in LOITER (sequence {post_snap.current_sequence}/{post_snap.mission_count} "
                   "retained).", "info")
        return self._success_result("pause", op_id, previous, PAUSED, verified_mode="LOITER",
                                     prog={"mode_name": post_snap.mode_name,
                                           "current_waypoint": post_snap.current_sequence,
                                           "mission_count": post_snap.mission_count})

    # ── Resume Mission (task section 5) ───────────────────────────────────────
    def resume(self) -> Dict[str, Any]:
        if not self._action_lock.acquire(blocking=False):
            return self._busy_result("resume")
        token = None
        try:
            with self._state_lock:
                state = self._state
            if state == RUNNING:
                return self._idempotent_result("resume", "ALREADY_RUNNING",
                                                "mission already running", verified_mode="AUTO")
            if self._is_replanning_active():
                return self._rejected_result("resume", "REPLANNING_ACTIVE",
                                              "a replanning transaction is active; resume is not permitted")
            if state != PAUSED:
                return self._rejected_result("resume", "NOT_RESUMABLE",
                                              f"cannot resume from state {state}")
            token = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
            if token is None:
                return self._arbitration_busy_result("resume")
            return self._run_resume()
        finally:
            if token is not None:
                write_arbiter.release(token)
            self._action_lock.release()

    def _run_resume(self) -> Dict[str, Any]:
        op_id = uuid.uuid4().hex
        previous = self._state
        with self._state_lock:
            self._active_operation_id = op_id
            self._last_error = None
            self._first_sequence_after_resume = None
            self._continuation_verified = None

        try:
            pre_state = self.gateway.read_vehicle_state()
        except Exception as e:
            return self._end_operation("resume", op_id, previous, SUSPENDED, "STATE_UNAVAILABLE",
                                       f"could not read vehicle state before resume: {e}")
        pre_snap = self._build_snapshot(pre_state)
        # Verify the same expected mission is still loaded.
        if self._mission_id is not None and pre_snap.mission_id is not None \
                and pre_snap.mission_id != self._mission_id:
            return self._end_operation("resume", op_id, previous, FAILED, "WRONG_MISSION_LOADED",
                                       f"a different mission is loaded ({pre_snap.mission_id!r}); "
                                       f"expected {self._mission_id!r}")
        if not pre_snap.mission_count or pre_snap.mission_count <= 0:
            return self._end_operation("resume", op_id, previous, FAILED, "MISSION_NOT_LOADED",
                                       "no mission is loaded to resume")
        with self._state_lock:
            self._sequence_at_resume = pre_snap.current_sequence

        # Require valid Home and fresh state before AUTO.
        if not self._home_ready():
            return self._end_operation("resume", op_id, previous, FAILED, "HOME_UNVERIFIED",
                                       "AUTO resume requires a verified Home; none is verified")
        if not self._position_fresh(pre_snap):
            return self._end_operation("resume", op_id, previous, FAILED, "POSITION_STALE_OR_INVALID",
                                       "current vehicle position is stale or invalid for resume")
        if not self._authorized():
            return self._end_operation("resume", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "control authority is not LOCAL_AGENT before commanding AUTO")
        self._transition(RESUME_REQUESTED, "Resume requested -- requesting AUTO to continue the mission.")
        auto = self._safe_write(self.gateway.command_auto)
        if not self._verified(auto):
            return self._end_operation("resume", op_id, previous, FAILED, "AUTO_NOT_VERIFIED",
                                       "could not confirm AUTO for resume; vehicle left in LOITER",
                                       detail=auto)

        # Confirm progression continues from the retained sequence.
        prog = self._confirm_progression()
        first_after = prog.get("current_waypoint")
        at_resume = self._sequence_at_resume
        continuation = self._assess_continuation(at_resume, first_after, prog)
        with self._state_lock:
            self._resume_ts = self._clock()
            self._first_sequence_after_resume = first_after
            self._continuation_verified = continuation
            self._last_error = None if continuation else {
                "code": "MISSION_SEQUENCE_RESTART_DETECTED",
                "message": (f"AUTO verified, but the mission sequence appears to have reset to {first_after} "
                            f"from a paused sequence of {at_resume}; continuation could NOT be verified. "
                            "Bench-verify Pixhawk resume-from-sequence behaviour."),
            }
        self._transition(RUNNING,
                         "Mission resumed under AUTO."
                         + ("" if continuation else " WARNING: sequence continuation not verified."),
                         settle=True)
        self._emit("mission_execution_resumed",
                   f"Mission resumed under AUTO (before_pause={self._sequence_before_pause}, "
                   f"at_resume={at_resume}, first_after_resume={first_after}, "
                   f"continuation_verified={continuation}).",
                   "info" if continuation else "warning")
        return self._success_result("resume", op_id, previous, RUNNING, verified_mode="AUTO", prog=prog)

    def _assess_continuation(self, at_resume, first_after, prog) -> bool:
        """Continuation is verified when the mission is active under AUTO and the
        first observed sequence has not clearly reset to the start when a nonzero
        paused sequence was expected. Normal Pixhawk advancement (first_after >=
        at_resume) is allowed."""
        if prog.get("mode_name") != "AUTO" or not prog.get("mission_active"):
            return False
        if at_resume is None or first_after is None:
            # No sequence evidence -- cannot positively verify continuation.
            return False
        if at_resume > 0 and first_after == 0:
            return False  # clear, unexpected reset to the beginning
        return first_after >= at_resume

    # ── Final completion hold (task section 7) ────────────────────────────────
    def run_final_hold(self) -> Dict[str, Any]:
        """Command and verify the final LOITER that ends the mission. Launched on
        a thread by the caller when observe() signals final_hold. Acquires the
        one-operation lock (non-blocking) and the shared write arbiter; if either
        is unavailable it declines rather than racing another write."""
        if not self._action_lock.acquire(blocking=False):
            return self._busy_result("final_hold")
        token = None
        try:
            with self._state_lock:
                state = self._state
            # Two entries reach the final verified LOITER: the return-to-Home
            # arrival monitor (RETURNING_HOME/HOME_ARRIVAL_PENDING) and the normal
            # ORIGINAL-mission completion monitor (RUNNING, task section 3). Both
            # end in the same COMPLETED_HOLD via _run_final_hold.
            if state not in (RETURNING_HOME, HOME_ARRIVAL_PENDING, RUNNING):
                return self._rejected_result("final_hold", "NOT_COMPLETABLE",
                                              f"cannot run final hold from state {state}")
            token = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
            if token is None:
                return self._arbitration_busy_result("final_hold")
            return self._run_final_hold()
        finally:
            if token is not None:
                write_arbiter.release(token)
            self._action_lock.release()

    def _run_final_hold(self) -> Dict[str, Any]:
        op_id = uuid.uuid4().hex
        previous = self._state
        # Where to fall back to if the final LOITER cannot be verified: the normal
        # completion path was RUNNING (re-arm the completion monitor); the
        # return-to-Home path falls back to RETURNING_HOME. Never mark complete.
        fallback_state = RUNNING if previous == RUNNING else RETURNING_HOME
        with self._state_lock:
            self._active_operation_id = op_id
        if not self._authorized():
            with self._state_lock:
                self._final_hold_started = False  # allow a retry once authority returns
                self._completion_since = None
            return self._end_operation("final_hold", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "control authority is not LOCAL_AGENT before final LOITER")
        self._transition(FINAL_HOLD_REQUESTED, "Arrival confirmed -- requesting final LOITER hold.")
        loiter = self._safe_write(self.gateway.command_loiter)
        # Record final evidence.
        final_snap = None
        try:
            final_state = self.gateway.read_vehicle_state()
            final_snap = self._build_snapshot(final_state)
            final_seq, final_mode = final_snap.current_sequence, final_snap.mode_name
        except Exception:
            final_seq, final_mode = None, (loiter.get("requested_mode") if isinstance(loiter, dict) else None)
        verified = self._verified(loiter)
        with self._state_lock:
            self._final_loiter_verified = verified
            self._last_sequence = final_seq if final_seq is not None else self._last_sequence
        if not verified:
            # Do NOT mark complete: fall back to the pre-hold live state (RUNNING
            # for normal completion, RETURNING_HOME for the return path), preserve
            # best-known safe state, report an explicit failure. The final hold is
            # not re-armed automatically here; the next in-radius arrival window or
            # the next persisted final-item window can signal it again, or an
            # operator can intervene.
            with self._state_lock:
                self._final_hold_started = False
                self._completion_since = None
                self._completion_confirmed = False
            return self._end_operation("final_hold", op_id, previous, fallback_state,
                                       "FINAL_LOITER_NOT_VERIFIED",
                                       "final LOITER could not be verified; mission NOT marked complete",
                                       detail=loiter)
        with self._state_lock:
            self._last_error = None
        # Terminal-evidence event (E2 water-trial recorder-aggregation fix):
        # the SAME proven facts as STOP_COMPLETE's payload, for a normal
        # arrival/COMPLETED_HOLD completion -- final_seq/final_mode above are
        # already this exact snapshot's proven values; final_snap additionally
        # carries armed/control_authority freshly read moments ago.
        seq_block = self._sequence_block()
        terminal_evidence = {
            "operation_id": op_id, "from": previous,
            "mission_execution_state": COMPLETED_HOLD,
            "mission_execution_phase": None,
            "final_mode": final_mode,
            "final_armed": final_snap.armed if final_snap is not None else None,
            "final_authority": final_snap.control_authority if final_snap is not None else None,
            "current_waypoint": final_seq if final_seq is not None else seq_block.get("current"),
            "mission_count": seq_block.get("count"),
            "route_hash": self._active_route_hash,
            "mission_id": self._mission_id,
        }
        self._transition(COMPLETED_HOLD, "Arrival at Home confirmed and final LOITER verified.",
                         terminal=True, terminal_evidence=terminal_evidence)
        self._emit("mission_execution_completed",
                   "Mission COMPLETED_HOLD -- arrived at Home and holding in verified LOITER.", "info")
        return self._success_result("final_hold", op_id, previous, COMPLETED_HOLD, verified_mode="LOITER",
                                     prog={"current_waypoint": final_seq, "mode_name": final_mode})

    # ── Shared execution-state invalidation (task sections 1, 2 & 7) ──────────
    def _invalidate_execution_state_locked(self) -> None:
        """Drop the controller to NOT_READY and clear ALL execution-specific state
        left over from a previous mission -- the bound original identity, active-
        route/revision hashes, the Start snapshot + progression evidence, prior
        verified Home, pause/resume sequence evidence, completion + arrival
        candidates/persistence, replan-active flag, stale recovery context, the
        cached readiness proof, and any package-conflict marker. MUST hold
        self._state_lock. Used by rearm() (operator re-arm) and on_new_package_
        stored() (a newly verified original mission replaces the previous one) so
        BOTH paths guarantee no stale execution evidence is inherited. It does NOT
        persist or re-prove readiness -- the caller decides that (and whether to do
        the re-proof off the request thread)."""
        self._state = NOT_READY
        self._active_operation_id = None
        self._last_error = None
        # NOT_READY is never a launch-pipeline phase -- clear it explicitly
        # (mirrors _transition()'s own PHASE_LABELS-membership rule) so a
        # reported `phase` never keeps showing a stale in-pipeline value
        # (e.g. "VERIFYING_RESET") after execution state has been
        # invalidated. Purely a status-accuracy fix -- does not touch
        # `_state`, authority, or any vehicle write.
        self._phase = None
        # Bound original mission identity + route hashes.
        self._mission_id = None
        self._original_route_hash = None
        self._active_route_hash = None
        # Start operation + progression evidence.
        self._start_snapshot = None
        self._progression_evidence = None
        # Stale recovery / retry context.
        self._recovery = None
        self._last_recovery_retry_ts = None
        # Prior verified runtime Home.
        self._verified_home = None
        self._home_verification_distance_m = None
        # Pause/resume sequence continuation evidence.
        self._sequence_before_pause = None
        self._sequence_at_resume = None
        self._first_sequence_after_resume = None
        self._continuation_verified = None
        # Return-to-Home arrival + normal-completion candidates/persistence.
        self._arrival_since = None
        self._arrival_confirmed = False
        self._final_loiter_verified = None
        self._final_hold_started = False
        self._completion_since = None
        self._completion_confirmed = False
        self._completion_evidence = None
        # Replan-active flag + package-conflict marker.
        self._replanning_active = False
        self._package_conflict = None
        # Cached readiness proof (re-proved fresh by the caller).
        self._readiness_ready = False
        self._readiness_evidence_ready = False
        self._readiness_checking = False
        self._readiness_reason = None
        self._readiness_detail = None
        self._readiness_mission_id = None
        self._readiness_original_hash = None
        self._readiness_active_hash = None
        self._last_readiness_eval_ts = None
        # A rearm/Stop/replacement package starts a genuinely NEW generation --
        # let the next fresh-evidence proof reset a stale replan latch again.
        self._replan_reset_evidence_generation = None

    def on_new_package_stored(self, mission_id: Optional[str] = None,
                              route_hash: Optional[str] = None,
                              route_count: Optional[int] = None) -> Dict[str, Any]:
        """Explicit new-original-mission notification (task section 2). Invoked by
        the acceptance path AFTER a new immutable planning package has been
        accepted AND a fresh Pixhawk readback verified it as the mission now on the
        vehicle -- so discovering a replacement no longer has to wait to be
        stumbled upon at the next rearm.

        Behaviour by mission-execution state:

          * ACTIVE (RUNNING/PAUSED/RETURNING_HOME/HOME_ARRIVAL_PENDING, an active
            REPLANNING, or any operation in flight): the running mission is NOT
            silently replaced/adopted. The conservative upload policy stands; a
            STALE_PACKAGE_DURING_ACTIVE_EXECUTION conflict is recorded and surfaced
            in status (adopted=False). No state is invalidated.

          * TERMINAL/IDLE (NOT_READY/READY/SUSPENDED/FAILED/COMPLETED_HOLD/
            RECOVERY_PENDING): the previous mission's execution-specific state is
            invalidated (see _invalidate_execution_state_locked) and the controller
            drops to NOT_READY. The new mission is NOT auto-started; after the
            normal readiness proof it becomes the clearly prepared next mission.

        Bounded + non-blocking: holds no lock across I/O; the readiness re-proof
        runs off the request thread in production (inline in synchronous tests)."""
        if self._action_lock.locked():
            with self._state_lock:
                self._package_conflict = {
                    "code": "OPERATION_IN_PROGRESS",
                    "package_mission_id": mission_id,
                    "bound_original_mission_id": self._mission_id,
                    "execution_state": self._state,
                }
            return {"adopted": False, "reason": "a mission-execution operation is in progress",
                    "conflict": "OPERATION_IN_PROGRESS", "execution_state": self._state}

        adopted = False
        with self._state_lock:
            state = self._state
            replanning = self._replanning_active or (
                write_arbiter.current_owner() == write_arbiter.OWNER_REPLANNING)
            if state in _LIVE_STATES or replanning:
                self._package_conflict = {
                    "code": "STALE_PACKAGE_DURING_ACTIVE_EXECUTION",
                    "package_mission_id": mission_id,
                    "bound_original_mission_id": self._mission_id,
                    "execution_state": state,
                }
                reason = (f"active execution ({'REPLANNING' if replanning else state}); the running "
                          "mission is not replaced. Stored package flagged as a conflict.")
            else:
                self._invalidate_execution_state_locked()
                self._persist_locked()
                adopted = True
                reason = ("previous mission execution state invalidated; the newly verified original "
                          "mission is prepared as the next mission (not auto-started)")
        if adopted:
            self._emit("mission_execution_new_package_bound",
                       f"New original mission {mission_id} stored + verified; prior execution state "
                       "invalidated, preparing as next mission (not auto-started).", "info")
            if self.cfg.readiness_poll_interval_s <= 0:
                self.refresh_readiness()
            else:
                threading.Thread(target=self.refresh_readiness, daemon=True).start()
        else:
            self._emit("mission_execution_package_conflict",
                       f"New package {mission_id} stored while execution is active; not adopted "
                       "(conflict surfaced in status).", "warning")
        with self._state_lock:
            current = self._state
        return {"adopted": adopted, "reason": reason, "execution_state": current,
                "conflict": None if adopted else "STALE_PACKAGE_DURING_ACTIVE_EXECUTION"}

    # ── Rearm ─────────────────────────────────────────────────────────────────
    def rearm(self) -> Dict[str, Any]:
        """Rearm from a terminal state (COMPLETED_HOLD/SUSPENDED/FAILED/
        RECOVERY_PENDING) back to NOT_READY/READY. Purely Local-Agent state:
        issues NO mode command and does NOT touch the Pixhawk mission -- it only
        UNBINDS the finished mission execution and clears the operation error so a
        fresh Start can prepare/bind the CURRENT stored planning package as the
        next original execution. Refused during an active operation.

        Rearm is *unbinding* (task section 1): it explicitly drops the obsolete
        bound original mission identity (mission_id + route hashes + verified
        Home + the Start snapshot/progression evidence). It never establishes a
        NEW identity by itself -- the readiness re-proof and the next Start are
        what bind the current package, and only after full route-content/hash +
        package-identity verification (never by mission_id or route count alone).
        Without this, a rearm after a re-upload left the controller advertising
        the PREVIOUS mission_id even though a new immutable package had replaced
        it (the reproduced MISSION_ID_MISMATCH -> rearm -> stale-identity bug).

        Bounded execution (task section 6): rearm performs NO blocking vehicle
        read/write and holds NO lock across I/O. It clears state under the brief
        state lock, releases it AND the action lock, and only then re-proves
        readiness -- inline when readiness is synchronous (tests), else on a
        daemon thread -- so the request thread returns immediately and a
        concurrent GET /status is never blocked behind a Pixhawk readback."""
        if not self._action_lock.acquire(blocking=False):
            return self._rejected_result("rearm", "OPERATION_IN_PROGRESS",
                                          "a mission-execution operation is in progress")
        released = False
        try:
            # Decide + (if eligible) mutate under the lock, then act on the
            # decision AFTER releasing it: _rejected_result() below re-acquires
            # self._state_lock, so calling it while still holding the lock would
            # deadlock -- a pre-existing bug uncovered while wiring the pre-E2
            # replan-reset lifecycle (no prior test ever called rearm() from a
            # non-terminal state, so this rejection path was never exercised).
            with self._state_lock:
                previous = self._state
                rearmable = previous in _REARMABLE_STATES
                if rearmable:
                    # Unbind the obsolete original mission identity + all
                    # execution-specific evidence, and drop to NOT_READY (task
                    # sections 1 & 2).
                    self._invalidate_execution_state_locked()
                    self._persist_locked()
            if not rearmable:
                return self._rejected_result("rearm", "NOT_REARMABLE",
                                             f"state {previous} is not terminal")
            self._emit("mission_execution_rearmed",
                       f"Mission execution rearmed from {previous} (unbound; will re-prove readiness).",
                       "info")
            # A terminal FAILED/SUSPENDED mission execution may leave the replan
            # controller latched in its own terminal state (SAFE_HOLD/SUSPENDED/
            # FAILED/FALLBACK_RTL) from the execution that just ended (task:
            # pre-E2 replan lifecycle). Do NOT eagerly reset it here: rearm only
            # drops to NOT_READY, and mission execution is not actually ready for
            # a next mission until the readiness re-proof below (inline or on the
            # daemon thread) proves it -- which already rearms a stale replan
            # status on its own NOT_READY->READY edge (see
            # _apply_readiness_proof_locked), exactly like a verified Stop
            # already does via _reset_replan() in _run_stop above. This avoids a
            # premature/redundant reset if the re-proof never actually succeeds.
            # Release BOTH locks before the (potentially slow, Pixhawk-readback)
            # readiness re-proof so rearm's own execution is bounded and holds no
            # lock across I/O -- a concurrent /status stays responsive.
            self._action_lock.release()
            released = True
            if self.cfg.readiness_poll_interval_s <= 0:
                # Synchronous readiness (tests): re-prove inline & deterministically.
                self.refresh_readiness()
            else:
                # Production: re-prove off the request thread so rearm returns now.
                threading.Thread(target=self.refresh_readiness, daemon=True).start()
            with self._state_lock:
                current = self._state
            print(f"[MISSION_EXEC] rearmed {previous} -> {current} (unbound prior identity)")
            return {"accepted": True, "operation": "rearm", "from": previous, "to": current,
                    "current_state": current, "final": True}
        finally:
            if not released:
                self._action_lock.release()

    # ── Stop Mission (operator-requested safe abort + reset-to-start) ─────────
    def stop(self) -> Dict[str, Any]:
        """Operator-requested Stop: a safe abort + reset-to-start transaction.

        NOT a raw Pixhawk STOP. It ends the current execution, restores the
        immutable original mission if a revised safe-return route is installed,
        rewinds the Pixhawk mission to the start, resets execution/replan/
        experiment state, hands authority back to the OPERATOR, and prepares the
        SAME approved original mission for a fresh Start -- so repeated testing and
        deployment recovery are simple.

        Physical safety precedes logical reset: a fresh verified LOITER hold is
        proven FIRST (never AUTO, never an auto-disarm); only then is any mission
        restore / rewind / state reset performed. Any step that cannot be proven
        fails closed in a safe non-running state (SUSPENDED) WITHOUT claiming a
        successful reset. Same single-operation arbitration as Start/Pause/Resume/
        Rearm: a concurrent mission-execution operation returns BUSY/CONFLICT
        rather than blocking, and no controller lock is held across a vehicle wait
        so GET /status stays responsive throughout."""
        if not self._action_lock.acquire(blocking=False):
            return self._busy_result("stop")
        token = None
        try:
            with self._state_lock:
                state = self._state
            if self._is_replanning_active():
                return self._rejected_result(
                    "stop", "REPLANNING_ACTIVE",
                    "a replanning transaction is active; stop is not permitted until it settles")
            if not self.cfg.mission_execution_enabled:
                return self._rejected_result(
                    "stop", "MISSION_EXECUTION_DISABLED",
                    "mission execution is disabled by configuration")
            if state not in _STOPPABLE_STATES:
                return self._rejected_result(
                    "stop", "NOT_STOPPABLE", f"cannot stop from state {state}")
            token = write_arbiter.acquire(write_arbiter.OWNER_MISSION_EXECUTION)
            if token is None:
                return self._arbitration_busy_result("stop")
            return self._run_stop()
        finally:
            if token is not None:
                write_arbiter.release(token)
            self._action_lock.release()

    def _run_stop(self) -> Dict[str, Any]:
        op_id = uuid.uuid4().hex
        previous = self._state
        with self._state_lock:
            self._active_operation_id = op_id
            self._last_error = None
            original_hash = self._original_route_hash or self._readiness_original_hash
            active_hash_bound = self._active_route_hash
        # Machine-readable evidence, filled in as each phase is proven.
        ev: Dict[str, Any] = {
            "operation_id": op_id, "from": previous,
            "hold_verified": None, "original_restored": None,
            "active_hash_before": None, "original_hash": original_hash,
            "revised_hash": None, "rewind_verified": None, "sequence_after": None,
            "replan_reset": None, "experiment_cleared": None,
            "authority_after": None, "ready_for_start": None, "outcome": None,
        }
        self._transition(STOP_REQUESTED,
                         "Stop Mission requested via Local Agent -- safe abort + reset to start.")

        # (1) PHYSICAL SAFETY FIRST: fresh verified LOITER hold. Never AUTO, never
        # auto-disarm. Fail closed WITHOUT any logical reset if it cannot be proven.
        if not self._authorized():
            return self._end_stop(op_id, previous, ev, SUSPENDED, "AUTHORITY_LOST",
                                   "control authority is not LOCAL_AGENT before commanding the "
                                   "stop LOITER hold", ensure_loiter=False)
        self._transition(STOP_HOLD_REQUESTED, "Requesting verified LOITER as the safe-abort hold.")
        loiter = self._safe_write(self.gateway.command_loiter)
        hold_ok, hold_detail = self._stop_loiter_verified(loiter)
        ev["hold_verified"] = hold_ok
        if not hold_ok:
            return self._end_stop(op_id, previous, ev, SUSPENDED, "STOP_HOLD_NOT_VERIFIED",
                                   "could not verify a fresh LOITER hold; failing closed WITHOUT "
                                   "resetting -- the vehicle may still be under autonomous propulsion",
                                   detail=hold_detail, ensure_loiter=True)
        self._transition(STOP_HOLD_CONFIRMED,
                         "Fresh LOITER hold confirmed -- determining the installed mission.")

        # (2) Determine the mission currently installed on the Pixhawk (fresh proof).
        readback, rb_err = self._read_pixhawk_with_retry()
        if rb_err is not None or not isinstance(readback, dict) or readback.get("reachable") is False:
            msg = rb_err[1] if rb_err else (readback or {}).get("error")
            return self._end_stop(op_id, previous, ev, SUSPENDED, "STOP_READBACK_UNAVAILABLE",
                                   f"could not prove the installed Pixhawk mission for stop: {msg}",
                                   ensure_loiter=True)
        fresh, fresh_reason = planning_package.readback_is_fresh(readback)
        if readback.get("partial") or not fresh:
            return self._end_stop(op_id, previous, ev, SUSPENDED, "STOP_READBACK_STALE",
                                   f"installed-mission readback is partial/not fresh enough to act "
                                   f"on: {fresh_reason}", ensure_loiter=True)
        active_hash_before = readback.get("route_content_hash")
        ev["active_hash_before"] = active_hash_before

        if not original_hash:
            return self._end_stop(op_id, previous, ev, SUSPENDED, "STOP_ORIGINAL_HASH_UNKNOWN",
                                   "no proven original route hash is bound; cannot safely restore/"
                                   "rewind the original mission", ensure_loiter=True)

        # Known, PROVEN revised safe-return hash(es): the bound active hash (set by
        # the verified replan handoff) and the live replan status's revised hash. A
        # restore is NEVER driven by mission id/count alone (task).
        revised_hashes = set()
        if active_hash_bound and active_hash_bound != original_hash:
            revised_hashes.add(active_hash_bound)
        replan_revised = self._replan_revised_hash()
        if replan_revised:
            revised_hashes.add(replan_revised)

        if active_hash_before == original_hash:
            # Original still installed -> keep the mission, only rewind.
            ev["original_restored"] = "NOT_NEEDED"
        elif active_hash_before in revised_hashes:
            # A verified revised safe-return route is installed -> restore the
            # immutable original mission and re-prove it with a fresh readback.
            ev["revised_hash"] = active_hash_before
            restored = self._restore_original_mission(op_id, original_hash)
            ev["original_restored"] = restored["ok"]
            if restored.get("summary") is not None:
                ev["restore"] = restored["summary"]
            if not restored["ok"]:
                return self._end_stop(op_id, previous, ev, SUSPENDED, restored["code"],
                                       restored["message"], detail=restored.get("detail"),
                                       ensure_loiter=True)
        else:
            # Unknown / unproven installed mission -> fail closed in LOITER.
            ev["original_restored"] = False
            return self._end_stop(op_id, previous, ev, SUSPENDED, "STOP_ACTIVE_MISSION_UNKNOWN",
                                   f"the installed Pixhawk mission hash {active_hash_before!r} is "
                                   "neither the proven original nor a verified revised safe-return "
                                   "hash; failing closed without restore/rewind", ensure_loiter=True)

        # (3) Rewind the mission to the start, VERIFIED from fresh sequence evidence.
        rewind = self._rewind_mission()
        ev["rewind_verified"] = rewind["ok"]
        ev["sequence_after"] = rewind.get("sequence_after")
        if not rewind["ok"]:
            return self._end_stop(op_id, previous, ev, SUSPENDED, rewind["code"], rewind["message"],
                                   detail=rewind.get("detail"), ensure_loiter=True)

        # (4) Reset execution / replanning / experiment state -- physical hold,
        # mission restore, and rewind are ALL proven at this point.
        self._transition(VERIFYING_RESET,
                         "Hold + restore + rewind verified -- resetting execution/replan/experiment state.")
        ev["replan_reset"] = self._reset_replan()
        ev["experiment_cleared"] = self._reset_experiment()
        with self._state_lock:
            self._invalidate_execution_state_locked()
            # This Stop just fired the ONE sanctioned reset for this
            # generation above; suppress the fresh-evidence-reset edge on the
            # readiness re-proof this transaction triggers below (step 6) so
            # it marks the generation handled WITHOUT a redundant second
            # _reset_replan() call (see _maybe_mark_fresh_evidence_reset_locked).
            self._skip_next_fresh_evidence_reset = True
            self._persist_locked()

        # (5) Return supervisory authority to the OPERATOR -- ONLY now, after a
        # verified safe hold (LOITER remains authority-exempt as a safety command).
        ev["authority_after"] = self._return_authority()

        # (6) Re-prove readiness so the next Start can begin the ORIGINAL mission
        # from the beginning. After handing authority back to the OPERATOR, the
        # honest landing is NOT_READY with the evidence proven and only the
        # LOCAL_AGENT authority handoff pending (start_eligible) -- which the
        # Operator Start transaction performs itself before the next Start.
        self.refresh_readiness()
        with self._state_lock:
            final_state = self._state
            ready_for_start = bool(self._readiness_evidence_ready)
            ev["ready_for_start"] = ready_for_start
            ev["outcome"] = final_state
            next_mission_id = self._readiness_mission_id
            next_route_hash = self._readiness_original_hash
            self._stop_evidence = ev
            self._persist_locked()
        # A successful Stop lands in READY/NOT_READY directly (see
        # _invalidate_execution_state_locked above), never through
        # _transition(terminal=True), so it finalizes the recording session
        # explicitly here -- task section 5's "successful explicit Stop
        # Mission" finalization condition / STOP_COMPLETE event.
        #
        # ── Stop finalization ordering fix (task sections 1/2/3) ────────────
        # ROOT CAUSE of "terminal_reason: authority -> OPERATOR" vs
        # "final_authority: LOCAL_AGENT": ev["authority_after"] above (5) is
        # the REAL, already-proven post-Stop authority -- but the OLD code
        # went straight to finalize_async() without ever telling the
        # recorder that value. The recorder's summary derived final_authority
        # from whichever periodic telemetry sample the (fully decoupled,
        # slower) main loop had queued last, which could easily still be
        # LOCAL_AGENT because that sample was taken before this Stop
        # transaction wrote OPERATOR back moments ago.
        #
        # Fix: emit one explicit terminal-evidence event carrying the SAME
        # proven facts the terminal_reason string below quotes (authority_
        # after, final mode/armed from the fresh LOITER-hold snapshot
        # already read in step (1), final sequence/mission id/route hash),
        # enqueued on the recorder's high-priority FIFO queue BEFORE
        # finalize_async's own FINALIZE marker. The writer thread drains
        # that queue strictly in order, so this event is always applied to
        # rs.terminal_snapshot before summary.json is built -- no extra
        # vehicle action/authority write, no waiting for queue drain, no
        # change to Stop's real physical/authority semantics; this only
        # tells the recorder what Stop already proved.
        if self._recorder is not None:
            try:
                seq_block = self._sequence_block()
                with self._state_lock:
                    final_phase = self._phase
                self._recorder.record_event(
                    "STOP_COMPLETE", source="mission_execution_controller",
                    data={
                        "operation_id": op_id, "from": previous,
                        "mission_execution_state": final_state,
                        "mission_execution_phase": final_phase,
                        "final_mode": hold_detail.get("observed_mode"),
                        "final_armed": hold_detail.get("observed_armed"),
                        "final_authority": ev.get("authority_after"),
                        "current_waypoint": seq_block.get("current"),
                        "mission_count": seq_block.get("count"),
                        "route_hash": next_route_hash,
                        "mission_id": next_mission_id,
                        "hold_verified": ev.get("hold_verified"),
                        "original_restored": ev.get("original_restored"),
                        "rewind_verified": ev.get("rewind_verified"),
                        "sequence_after": ev.get("sequence_after"),
                        "replan_reset": ev.get("replan_reset"),
                        "experiment_cleared": ev.get("experiment_cleared"),
                        "authority_after": ev.get("authority_after"),
                        "ready_for_start": ready_for_start,
                    },
                    priority="high",
                )
            except Exception:
                pass
        if self._recorder is not None:
            try:
                self._recorder.finalize_async(
                    "STOP_COMPLETE",
                    f"Stop Mission completed from {previous}; authority -> {ev['authority_after']}.",
                )
            except Exception:
                pass
        self._emit("mission_execution_stopped",
                   f"Stop complete from {previous}: verified LOITER hold, "
                   f"{'restored original + ' if ev['original_restored'] is True else ''}"
                   f"rewound to start, execution/replan/experiment reset, authority -> "
                   f"{ev['authority_after']}; state {final_state} (ready_for_start={ready_for_start}).",
                   "info")
        print(f"[MISSION_EXEC] stop complete {previous} -> {final_state} "
              f"(hold_verified={ev['hold_verified']}, original_restored={ev['original_restored']}, "
              f"rewind_verified={ev['rewind_verified']}, authority_after={ev['authority_after']})")
        return {
            "accepted": True, "operation": "stop", "outcome": final_state,
            "operation_id": op_id, "execution_state": final_state,
            "mission_id": next_mission_id, "route_hash": next_route_hash,
            "previous_state": previous, "current_state": final_state,
            "verified_mode": "LOITER", "home_result": None,
            "sequence": self._sequence_block(),
            "stop": ev, "error": None, "final": True,
        }

    def _end_stop(self, op_id: str, previous: str, ev: Dict[str, Any], to_state: str,
                  code: str, message: str, detail=None, ensure_loiter: bool = False) -> Dict[str, Any]:
        """Fail-closed exit for a Stop: optionally RE-ASSERT LOITER (so the vehicle
        remains in a confirmed safe hold), record the error and the partial Stop
        evidence, transition to a safe non-running state, and return a structured
        result. A Stop that fails NEVER claims a successful reset -- ready_for_start
        is False and the reset/authority handoff below step 4 did not run."""
        fallback = self._ensure_loiter() if ensure_loiter else None
        ev["outcome"] = to_state
        ev["ready_for_start"] = False
        with self._state_lock:
            self._last_error = {"code": code, "message": message}
            if detail is not None:
                self._last_error["detail"] = detail
            if ensure_loiter:
                self._last_error["fallback_loiter_verified"] = fallback
            self._stop_evidence = ev
        self._transition(to_state, message, terminal=(to_state in (FAILED, SUSPENDED, COMPLETED_HOLD)))
        self._emit("mission_execution_stop_failed", f"stop failed: {message}", "warning")
        print(f"[MISSION_EXEC] stop failed {previous} -> {to_state}: {code} -- {message}")
        error = {"code": code, "message": message}
        if detail is not None:
            error["detail"] = detail
        if ensure_loiter:
            error["fallback_loiter_verified"] = fallback
        return {
            "accepted": True, "operation": "stop", "outcome": to_state,
            "operation_id": op_id, "execution_state": to_state,
            "mission_id": self._mission_id, "route_hash": self._active_route_hash,
            "previous_state": previous, "current_state": to_state,
            "verified_mode": None, "home_result": None,
            "sequence": self._sequence_block(),
            "stop": ev, "error": error, "final": True,
        }

    def _stop_loiter_verified(self, loiter_result):
        """Accept the stop hold only when the LOITER command verified AND a FRESH
        vehicle read confirms mode==LOITER. Returns (ok, detail).

        `detail` also carries the SAME fresh snapshot's armed/waypoint fields
        (already read here, no extra vehicle read) so the Stop terminal-
        evidence event can report a real final_armed value rather than
        fabricating one -- see _run_stop's terminal STOP_COMPLETE event."""
        if not self._verified(loiter_result):
            return False, {"reason": "LOITER command did not verify", "loiter": loiter_result}
        snap = self._read_snapshot_safe()
        mode = getattr(snap, "mode_name", None)
        if snap is None or mode != "LOITER":
            return False, {"reason": "fresh vehicle state does not confirm LOITER",
                           "observed_mode": mode}
        return True, {"observed_mode": "LOITER", "observed_armed": getattr(snap, "armed", None)}

    def _replan_revised_hash(self) -> Optional[str]:
        """The revised safe-return route hash from the live replan status, or None.
        Used only as a POSITIVE match source for recognising an installed revised
        mission -- never to drive a restore by id/count."""
        if self._replan_status_fn is None:
            return None
        try:
            st = self._replan_status_fn()
        except Exception:
            return None
        return (st or {}).get("revised_mission_hash")

    def _restore_original_mission(self, op_id: str, original_hash: str) -> Dict[str, Any]:
        """Restore the immutable original mission (uploaded from the stored planning
        package) when a revised safe-return route is installed, then INDEPENDENTLY
        re-prove it with a fresh readback (hash + count). Never trusts the upload
        ack alone. Returns {ok, code?, message?, detail?, summary?}."""
        self._transition(RESTORING_ORIGINAL,
                         "A revised safe-return route is installed -- restoring the immutable "
                         "original mission from the approved planning package.")
        original = planning_package.load_original()
        route = (original or {}).get("route") or []
        if not planning_package.is_usable(original) or not route:
            return {"ok": False, "code": "STOP_ORIGINAL_UNAVAILABLE",
                    "message": "the immutable original mission/planning package is not usable to restore"}
        if not self._authorized():
            return {"ok": False, "code": "AUTHORITY_LOST",
                    "message": "control authority is not LOCAL_AGENT before restoring the original mission"}
        upload = self._safe_write(lambda: self.gateway.upload_mission(
            route=route, command_id=op_id, upload_context="AGENT_STOP_RESTORE"))
        summary = {
            "verified": bool(isinstance(upload, dict) and upload.get("verified")),
            "observed_route_content_hash": (upload or {}).get("observed_route_content_hash"),
            "observed_route_waypoint_count": (upload or {}).get("observed_route_waypoint_count"),
            "error": (upload or {}).get("error") if isinstance(upload, dict) else None,
        }
        if not summary["verified"]:
            return {"ok": False, "code": "STOP_RESTORE_UPLOAD_FAILED",
                    "message": "could not upload/verify the original mission during stop; remaining in LOITER",
                    "detail": summary, "summary": summary}
        # Re-prove the restored route with a FRESH readback -- the installed hash +
        # count must now match the immutable original (never trust the upload ack).
        readback, rb_err = self._read_pixhawk_with_retry()
        if rb_err is not None or not isinstance(readback, dict) or readback.get("reachable") is False:
            return {"ok": False, "code": "STOP_RESTORE_READBACK_UNAVAILABLE",
                    "message": "restored the original mission but could not re-prove it with a fresh readback",
                    "summary": summary}
        fresh, fresh_reason = planning_package.readback_is_fresh(readback)
        if readback.get("partial") or not fresh:
            return {"ok": False, "code": "STOP_RESTORE_READBACK_STALE",
                    "message": f"restored-mission readback is partial/not fresh: {fresh_reason}",
                    "summary": summary}
        rb_hash = readback.get("route_content_hash")
        rb_count = readback.get("route_waypoint_count")
        summary["reproved_route_content_hash"] = rb_hash
        summary["reproved_route_waypoint_count"] = rb_count
        if rb_hash != original_hash:
            return {"ok": False, "code": "STOP_RESTORE_HASH_MISMATCH",
                    "message": f"restored route hash {rb_hash} != original {original_hash}",
                    "detail": summary, "summary": summary}
        if rb_count is not None and rb_count != len(route):
            return {"ok": False, "code": "STOP_RESTORE_COUNT_MISMATCH",
                    "message": f"restored route count {rb_count} != original {len(route)}",
                    "detail": summary, "summary": summary}
        return {"ok": True, "summary": summary}

    def _rewind_mission(self) -> Dict[str, Any]:
        """Reset the Pixhawk mission progression to the start (mission_rewind_
        sequence) and VERIFY it from fresh sequence evidence -- an ACK alone is
        never trusted. Returns {ok, code?, message?, detail?, sequence_after}."""
        self._transition(REWINDING_MISSION,
                         f"Rewinding the Pixhawk mission to sequence {self.cfg.mission_rewind_sequence}.")
        if not self._authorized():
            return {"ok": False, "code": "AUTHORITY_LOST", "sequence_after": None,
                    "message": "control authority is not LOCAL_AGENT before rewinding the mission"}
        ack = self._safe_write(lambda: self.gateway.set_mission_current(self.cfg.mission_rewind_sequence))
        seq_after, ok = self._verify_rewind_fresh(self.cfg.stop_rewind_verify_timeout_s)
        if not ok:
            return {"ok": False, "code": "STOP_REWIND_NOT_VERIFIED", "sequence_after": seq_after,
                    "message": (f"rewind acknowledged but the fresh mission sequence ({seq_after}) never "
                                f"reached the start (<= {self.cfg.mission_rewind_verify_max_sequence}); "
                                "the mission was NOT actually rewound"),
                    "detail": {"ack": ack, "sequence_after": seq_after}}
        with self._state_lock:
            if seq_after is not None:
                self._last_sequence = seq_after
        return {"ok": True, "sequence_after": seq_after}

    def _verify_rewind_fresh(self, timeout_s: float):
        """Poll FRESH vehicle state until the mission sequence is proven back at the
        start (<= mission_rewind_verify_max_sequence, tolerating the Home-vs-first-
        item distinction and a quick 0->1 auto-advance) or the timeout elapses.
        Returns (last_seen_sequence, ok)."""
        limit = self.cfg.mission_rewind_verify_max_sequence
        deadline = self._clock() + timeout_s
        poll = self.cfg.progression_poll_interval_s
        last_seq = None
        while True:
            snap = self._read_snapshot_safe()
            seq = snap.current_sequence if snap is not None else None
            if isinstance(seq, int):
                last_seq = seq
                if seq <= limit:
                    return seq, True
            if self._clock() >= deadline:
                return last_seq, False
            self._sleep(poll)

    def _reset_replan(self):
        """Reset the replanning transaction / trigger latch via the injected hook
        (bounded internal reset, not a duplicate of replan logic). Returns the
        hook's result dict, or None when no hook is wired."""
        if self._replan_reset_fn is None:
            return None
        try:
            result = self._replan_reset_fn()
        except Exception as e:
            return {"reset": False, "reason": f"replan reset error: {e}"}
        return result if isinstance(result, dict) else {"reset": bool(result)}

    def _reset_experiment(self):
        """Clear an active simulated experiment injection via the injected hook so
        the next test starts clean. Returns True/False (whether something was
        cleared), or None when no hook is wired. Never touches real telemetry."""
        if self._experiment_reset_fn is None:
            return None
        try:
            return bool(self._experiment_reset_fn())
        except Exception:
            return None

    def _return_authority(self) -> Optional[str]:
        """Hand supervisory authority back to the configured post-stop authority
        (OPERATOR) once the vehicle is held safely. Best-effort: a failure to write
        authority does not un-reset the mission, but is surfaced as a warning and
        leaves authority_after unproven (None)."""
        setter = getattr(self.gateway, "set_control_authority", None)
        target = self.cfg.stop_authority_after
        if not callable(setter):
            return None
        try:
            result = setter(target)
        except Exception as e:
            self._emit("mission_execution_stop_authority_failed",
                       f"Stop could not hand authority back to {target}: {e}", "warning")
            return None
        if isinstance(result, dict):
            return result.get("authority", target)
        return result or target

    # ── Vehicle-op helpers ────────────────────────────────────────────────────
    def _authorized(self) -> bool:
        """Fresh authority read + autonomy_gate check, immediately before a write.
        Fails closed on any read error."""
        try:
            authority = self.gateway.current_authority()
        except Exception:
            with self._state_lock:
                self._last_authority = None
            return False
        with self._state_lock:
            self._last_authority = authority
        allowed, _ = autonomy_gate.check_autonomous_write_authority(authority)
        return allowed

    def _safe_write(self, fn) -> Dict[str, Any]:
        """Run a gateway write; a transport error is turned into a non-verified
        result rather than raising, so the FSM handles it as a normal failure."""
        try:
            result = fn()
            return result if isinstance(result, dict) else {"verified": False}
        except Exception as e:
            return {"verified": False, "error": {"code": "GATEWAY_ERROR", "message": str(e)}}

    def _confirm_progression(self) -> Dict[str, Any]:
        try:
            state = self.gateway.read_vehicle_state()
        except Exception:
            return {"mode_name": None, "mission_active": None, "current_waypoint": None, "mission_count": None}
        snap = self._build_snapshot(state)
        return {"mode_name": snap.mode_name, "mission_active": snap.mission_active,
                "current_waypoint": snap.current_sequence, "mission_count": snap.mission_count}

    # ── Automatic ARM phase (Start section 11 / ARM lifecycle) ────────────────
    def _read_snapshot_safe(self):
        """Read fresh vehicle state and build a snapshot; None on any read error
        (the caller treats an unreadable sample as UNKNOWN, never as a value)."""
        try:
            return self._build_snapshot(self.gateway.read_vehicle_state())
        except Exception:
            return None

    def _fresh_armed(self, snap) -> Optional[bool]:
        """Fresh armed state from a snapshot, or None when it is unavailable OR
        stale. A cached/stale value is treated as UNAVAILABLE, never trusted --
        ARM/AUTO/progression all require FRESH telemetry (task section 5)."""
        if snap is None or not isinstance(snap.armed, bool):
            return None
        age = snap.telemetry_age_s
        if age is not None and age > self.cfg.max_position_age_s:
            return None  # stale heartbeat -> armed evidence is not fresh
        return snap.armed

    def _ensure_armed(self, op_id: str, previous: str) -> Optional[Dict[str, Any]]:
        """Guarantee a FRESH, verified armed=true as the FIRST vehicle-changing
        Start write, before any physical-hold LOITER. Idempotent when already
        armed (no duplicate ARM command). Returns None on success, or an
        _end_operation failure result.

        ARM is the first write, so on ANY ARM failure the vehicle is NOT sent
        LOITER as a fake hold (a disarmed LOITER cannot physically hold station),
        Home is not set, AUTO is not entered, and it is never auto-disarmed. Every
        ARM-failure exit uses ensure_loiter=False -- unlike a failure AFTER a
        successful ARM, where LOITER is (re-)asserted to remain ARMED + LOITER."""
        snap = self._read_snapshot_safe()
        armed = self._fresh_armed(snap)

        if armed is True:
            # Already armed and fresh -- idempotent, do NOT resend ARM.
            self._transition(VERIFYING_ARMED,
                             "Vehicle already armed (fresh) -- verified, not re-arming.")
            return None
        if armed is None:
            # Unknown/stale armed state -> fail closed BEFORE any write (no ARM,
            # no LOITER-as-hold, no AUTO). Do not guess.
            return self._end_operation("start", op_id, previous, FAILED, "ARM_STATE_UNAVAILABLE",
                                       "armed state is unavailable or stale; cannot safely arm -- "
                                       "failing closed before any vehicle write", ensure_loiter=False)

        # Disarmed -> exactly one bounded ARM intent, authority-gated.
        if not self._authorized():
            return self._end_operation("start", op_id, previous, SUSPENDED, "AUTHORITY_LOST",
                                       "authority lost before ARM", ensure_loiter=False)
        self._transition(ARMING, "Vehicle disarmed -- requesting ARM (first Start write) before LOITER.")
        arm = self._safe_write(self.gateway.command_arm)
        err = (arm.get("error") if isinstance(arm, dict) else None) or {}
        # An explicit rejection ACK (or a transport error) is definitive -- do not
        # wait out the verify window for a state change that isn't coming.
        if err.get("code") in ("ACK_REJECTED", "GATEWAY_ERROR"):
            return self._end_operation("start", op_id, previous, FAILED, "ARM_FAILED",
                                       f"Pixhawk rejected ARM: {err.get('message') or arm.get('reason')}; "
                                       "not sending LOITER/Home/AUTO",
                                       detail=arm, ensure_loiter=False)

        # A command acknowledgement alone is NOT sufficient -- independently poll
        # FRESH vehicle state for armed=true up to arm_verify_timeout_s.
        self._transition(VERIFYING_ARMED, "Verifying fresh armed=true after the ARM request.")
        result = self._verify_armed_fresh(self.cfg.arm_verify_timeout_s)
        if result is None:
            return self._end_operation("start", op_id, previous, FAILED, "ARM_STATE_UNAVAILABLE",
                                       "armed state remained unavailable/stale after the ARM request; "
                                       "not continuing to LOITER/Home/AUTO", detail=arm, ensure_loiter=False)
        if result is not True:
            return self._end_operation("start", op_id, previous, FAILED, "ARM_NOT_VERIFIED",
                                       "ARM requested but fresh telemetry never confirmed armed=true; "
                                       "not sending LOITER/Home/AUTO", detail=arm, ensure_loiter=False)
        with self._state_lock:
            if self._start_snapshot is not None:
                self._start_snapshot["auto_armed"] = True
        return None

    def _loiter_hold_verified(self, loiter_result):
        """Accept LOITER as the launch safety hold only when the command verified
        AND a FRESH vehicle read still proves armed==true and mode==LOITER. At
        this gate the vehicle was positively armed just above; if it unexpectedly
        disarmed (or left LOITER) between ARM verification and here, a LOITER mode
        cannot physically hold station -- fail closed and do not continue to
        Home/AUTO. Returns (ok, failure_code, failure_message)."""
        if not self._verified(loiter_result):
            return (False, "LOITER_NOT_VERIFIED",
                    "could not confirm LOITER; vehicle mode left unchanged")
        snap = self._read_snapshot_safe()
        if self._fresh_armed(snap) is not True:
            return (False, "DISARMED_BEFORE_LOITER",
                    "vehicle is not freshly armed at LOITER verification; a LOITER mode cannot "
                    "physically hold station while disarmed -- not continuing to Home/AUTO")
        if snap is None or snap.mode_name != "LOITER":
            return (False, "LOITER_NOT_VERIFIED",
                    "fresh vehicle state does not confirm LOITER at the launch safety-hold gate")
        return (True, None, None)

    def _verify_armed_fresh(self, timeout_s: float) -> Optional[bool]:
        """Poll FRESH vehicle state until armed=true or timeout. Returns True
        (verified), False (a fresh armed=false was seen but it never reached
        true) or None (armed was never available/fresh at all -> unavailable)."""
        start = self._clock()
        deadline = start + timeout_s
        poll = self.cfg.progression_poll_interval_s
        saw_bool = False
        while True:
            snap = self._read_snapshot_safe()
            armed = self._fresh_armed(snap)
            if armed is True:
                return True
            if armed is False:
                saw_bool = True
            if self._clock() >= deadline:
                break
            self._sleep(poll)
        return False if saw_bool else None

    # ── Pre-AUTO baseline + progression watch (sections 2 / 5) ─────────────────
    def _progression_context(self) -> mission_progression.ProgressionContext:
        """Build the shared-verifier context for the ORIGINAL mission: read fresh
        snapshots via this controller's gateway, resolve targets from the stored
        planning-package route, hold the bound mission identity, and share the
        controller's timing config + clock/sleep hooks (so tests drive the whole
        deadline deterministically). Start and Resume use the SAME verifier as the
        replan path -- there is one progression algorithm, not two."""
        return mission_progression.ProgressionContext(
            read_snapshot=self._read_snapshot_safe,
            target_for_sequence=self._current_target,
            expected_mission_id=self._mission_id,
            poll_interval_s=self.cfg.progression_poll_interval_s,
            min_displacement_m=self.cfg.progression_min_displacement_m,
            max_position_age_s=self.cfg.max_position_age_s,
            clock=self._clock,
            sleep=self._sleep,
        )

    def _capture_baseline(self) -> Dict[str, Any]:
        """Capture the pre-AUTO progression baseline immediately before AUTO, so
        sequence/movement progress is measured against a fixed reference."""
        return mission_progression.capture_baseline(self._progression_context())

    def _current_target(self, seq: Optional[int]) -> Optional[tuple]:
        """(lat, lon) of the currently-selected mission target, or None. Pixhawk
        item 0 is Home and route execution starts at item 1, so a Pixhawk
        sequence maps to package route index (seq - 1)."""
        if not isinstance(seq, int) or seq < 1:
            return None
        route = (planning_package.load() or {}).get("route") or []
        idx = seq - 1
        if 0 <= idx < len(route):
            wp = route[idx]
            lat, lon = wp.get("latitude"), wp.get("longitude")
            if lat is not None and lon is not None:
                return (lat, lon)
        return None

    def _watch_progression(self, baseline: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
        """Poll fresh progression evidence until a positive proof appears, a
        definitive immediate-failure condition occurs, or the FULL configured
        deadline elapses -- via the shared mission_progression verifier (the SAME
        one the replan path uses). Never exits early on a single inactive/
        unavailable sample. Returns a rich evidence dict either way."""
        return mission_progression.watch(self._progression_context(), baseline, timeout_s)

    def _home_ready(self) -> bool:
        try:
            status = self.gateway.home_status()
        except Exception:
            return False
        return bool(status.get("verified")) and bool(status.get("ready_for_auto"))

    def _home_set_verified(self, result) -> bool:
        if not isinstance(result, dict) or not result.get("verified"):
            return False
        dist = result.get("verification_distance_m")
        if dist is None:
            return True  # service verified; no distance to re-check
        return dist <= self.cfg.home_verification_tolerance_m

    def _ensure_loiter(self) -> bool:
        """Re-assert LOITER so a post-hold failure leaves a confirmed safe hold.
        Best-effort: a failure here is recorded but does not raise.

        SAFETY-EXEMPT LOITER (P0-2): this is the ONE vehicle write in this
        controller that does NOT call _authorized() first. It is deliberately
        narrow -- LOITER only, never ARM/AUTO/upload/set_home/set_mission_
        current -- and is only reached from a small, explicit set of fail-
        closed/terminal paths (_end_operation(ensure_loiter=True), _end_stop's
        pre-restore hold, the restart-reconciliation safety hold in
        _fail_recovery -- see that method's own comment for the fullest
        statement of the rationale). It exists so a vehicle already under
        autonomous propulsion is not left drifting/holding no mode merely
        because supervisory authority changed hands at the exact moment of a
        failure; it never re-arms, never re-enters AUTO, and never implies
        any other write may proceed without a fresh _authorized() check.
        Every non-LOITER write in this controller (ARM/LOITER-as-launch-hold/
        Set Home/AUTO/upload/set_mission_current) calls _authorized()
        immediately before itself, with no exception -- see _authorized()'s
        callers. Once authority is OPERATOR, a call here still commands
        LOITER (the intended safety behaviour) but can never lead to a
        subsequent AUTO/upload: every path that follows a _ensure_loiter()
        call is terminal (FAILED/SUSPENDED) or itself re-checks authority
        before its own next write."""
        try:
            return self._verified(self._safe_write(self.gateway.command_loiter))
        except Exception as e:
            self._emit("mission_execution_loiter_restore_failed",
                       f"Could not re-assert LOITER after a failure: {e}", "warning")
            return False

    def _build_snapshot(self, vehicle_state: Dict[str, Any]):
        import decision_snapshot
        authority = None
        try:
            authority = (vehicle_state.get("agent") or {}).get("control_authority")
        except Exception:
            authority = None
        return decision_snapshot.build_snapshot(
            vehicle_state, comm_state=None, control_authority=authority,
            planning_package=planning_package.load())

    def _evaluate_feasibility(self, snapshot, usv_id: Optional[str] = None) -> "mission_feasibility.MissionFeasibilityResult":
        """AUTHORITATIVE mission-energy-feasibility evaluation (task: mission
        energy feasibility, section 6): read-only, in-memory only -- no
        vehicle command, no Operator call, no mission/authority/replan
        mutation (mission_feasibility.py's own contract). Called from the
        Start gate with a FRESH snapshot (the same one `_resolve_start_
        prerequisites` just proved), so a Start-time evaluation always uses
        current evidence, never a cached continuous-loop result (see
        update_energy_feasibility for that separate, advisory, cached path).

        Reuses the exact same planning-package route, active experiment
        injection, and replan_config (capacity/current/design-speed model
        parameters, max_position_age_s) the continuous evaluation and the
        existing return-energy policy (energy_policy.py) already use -- one
        shared evidence source, not a second copy.

        Deliberately does NOT pass `mission_binding` (task: mission-route-
        identity safety): by the time this runs, `_resolve_start_
        prerequisites` (called earlier in `_run_start`, strictly before this)
        has ALREADY freshly and authoritatively proven the stored package's
        route hash matches a live Pixhawk readback -- a stronger, inline
        guarantee than the continuous loop's periodically-refreshed binding
        cache. Route identity is proven true by construction at this call
        site; `evaluate_from_snapshot`'s default (route_identity_verified=
        True when no binding is supplied) correctly reflects that, not a
        bypass of the invariant."""
        package = planning_package.load()
        try:
            injection = experiment_injection.active(usv_id or getattr(snapshot, "vehicle_id", None))
        except Exception:
            injection = None
        cfg, _ = replan_config.resolve()
        return mission_feasibility.evaluate_from_snapshot(snapshot, package, injection, cfg)

    def update_energy_feasibility(self, result: Optional[Dict[str, Any]]) -> None:
        """Continuously-updated, ADVISORY mission-energy-feasibility evidence
        (mission_feasibility.py's MissionFeasibilityResult.to_dict()),
        supplied by the caller's own per-iteration evaluation (local_agent.py's
        main loop, from the same snapshot energy_policy.py already evaluates
        that iteration). This controller performs NO energy computation to
        produce it and makes no vehicle read/write here -- purely a cached
        value for status()/can_start display and soft gating.

        The AUTHORITATIVE check that can actually REJECT a Start always runs
        fresh, inline, in _run_start (see the feasibility gate there, which
        calls _evaluate_feasibility itself) -- this cached value is never
        substituted for that fresh re-check. None (the default, and what every
        caller that does not wire this up -- including most of this module's
        own test suite -- leaves it at) means "not yet evaluated" and never
        blocks Start on its own, so existing callers see unchanged behaviour."""
        with self._state_lock:
            self._feasibility = dict(result) if isinstance(result, dict) else None

    def update_risk_assessment(self, result: Optional[Dict[str, Any]]) -> None:
        """Continuously-updated, OBSERVATIONAL/ADVISORY continuous risk
        assessment (risk_model.py's RiskResult.to_dict()), supplied by the
        caller's own per-iteration evaluation (local_agent.py's main loop,
        from the same snapshot/feasibility/vehicle_state that iteration
        already produced). Purely a cached value for status() display --
        this task does NOT gate can_start/can_pause/can_resume or any other
        control-flow decision on risk (that is explicitly deferred to a
        later decision-policy task; see risk_model.py's module docstring).
        None (the default) means "not yet evaluated"."""
        with self._state_lock:
            self._risk = dict(result) if isinstance(result, dict) else None

    def _is_replanning_active(self) -> bool:
        if write_arbiter.current_owner() == write_arbiter.OWNER_REPLANNING:
            return True
        if self._replan_status_fn is not None:
            try:
                st = self._replan_status_fn()
            except Exception:
                st = None
            if st and (st.get("running") or st.get("fsm_state") in _ACTIVE_REPLAN_STATES):
                return True
        with self._state_lock:
            return self._replanning_active

    @staticmethod
    def _verified(result) -> bool:
        return bool(isinstance(result, dict) and result.get("verified"))

    # ── State / status plumbing ───────────────────────────────────────────────
    def _transition(self, new_state: str, reason: str, terminal: bool = False,
                    settle: bool = False, terminal_evidence: Optional[Dict[str, Any]] = None):
        """Move to ``new_state``, record history, and persist. ``terminal`` (a
        failed/completed rest state) and ``settle`` (a successful STABLE rest state
        -- RUNNING/PAUSED) both mean the operation has reached its final resting
        state, so ``active_operation_id`` is cleared: it denotes an operation
        CURRENTLY executing, and a settled RUNNING/PAUSED is no longer executing.
        The history entry for this transition still carries the id for diagnostics;
        only the live "currently active" field is cleared.

        ``terminal_evidence`` (E2 water-trial integration task: recorder
        aggregation fix): an optional dict of the SAME proven, already-computed
        facts the Stop path's STOP_COMPLETE event carries (mission_execution_
        state/phase, final_mode/armed/authority, current_waypoint/mission_count,
        route_hash/mission_id) -- emitted as a high-priority MISSION_EXECUTION_
        TERMINAL_EVIDENCE event BEFORE finalize_async below, so the recorder's
        rs.terminal_snapshot is populated for EVERY terminal outcome reached
        through this generic path (COMPLETED_HOLD via _run_final_hold, FAILED/
        SUSPENDED via _end_operation), not only the Stop path's own explicit
        event. No new vehicle action; purely reports what the caller already
        proved. Never fabricated: absent/unknown fields stay absent, exactly
        like STOP_COMPLETE's own payload."""
        with self._state_lock:
            prev = self._state
            self._state = new_state
            # Operator-facing phase: the human-readable "what's happening"; None
            # once we leave the launch pipeline (RUNNING / a terminal state).
            self._phase = new_state if new_state in PHASE_LABELS else None
            # Record the phase timestamp in the immutable Start operation snapshot.
            if self._start_snapshot is not None and new_state in PHASE_LABELS:
                self._start_snapshot.setdefault("phase_timestamps", {})[new_state] = round(self._clock(), 3)
            entry = {"from": prev, "to": new_state, "reason": reason,
                     "at": round(self._clock(), 3),
                     "operation_id": self._active_operation_id}
            self._history.append(entry)
            del self._history[:-50]
            if terminal or settle:
                self._active_operation_id = None
            self._persist_locked()
        try:
            import transition_log
            transition_log.record_transition("mission_execution", prev, new_state, reason)
        except Exception:
            pass
        # Experiment-recorder finalization (task section 5): COMPLETED_HOLD /
        # FAILED / SUSPENDED are terminal outcomes for a recording session.
        # PAUSED is `settle`, not `terminal`, so it deliberately does NOT
        # finalize here -- Pause/Resume stay the same run (task section 5).
        # A successful Stop lands in READY/NOT_READY via
        # _invalidate_execution_state_locked() rather than through here, so it
        # finalizes explicitly at its own call site (see _run_stop).
        if terminal and new_state in (COMPLETED_HOLD, FAILED, SUSPENDED) and self._recorder is not None:
            if terminal_evidence is not None:
                try:
                    self._recorder.record_event(
                        "MISSION_EXECUTION_TERMINAL_EVIDENCE",
                        source="mission_execution_controller",
                        data=terminal_evidence, priority="high",
                    )
                except Exception:
                    pass
            try:
                self._recorder.finalize_async(new_state, reason)
            except Exception:
                pass

    def _end_operation(self, operation: str, op_id: str, previous: str, to_state: str,
                       code: str, message: str, detail=None, ensure_loiter: bool = False) -> Dict[str, Any]:
        """Common failure exit: optionally re-assert LOITER, record the error,
        transition to the terminal/holding state, emit, and return a structured
        result. `to_state` reflects the safe outcome (FAILED / SUSPENDED /
        RETURNING_HOME).

        POST-FAILURE LOITER/RECORDER fix: when `ensure_loiter` performs a
        terminal safety restoration, this now ALSO takes one fresh vehicle-
        state read (same pattern _run_final_hold already uses after its own
        LOITER) and passes it to _transition as `terminal_evidence` -- the
        SAME mechanism _run_final_hold's COMPLETED_HOLD success path already
        uses, so the experiment recorder's terminal_snapshot (and therefore
        summary.json's vehicle.final_mode/final_armed/final_authority) is
        never left to fall back to stale periodic telemetry for a FAILED/
        SUSPENDED outcome. Best-effort: a failed fresh read leaves final_snap
        None and every derived field explicitly None (never fabricated) --
        fallback_loiter_verified (already computed above, independent of this
        read) still reports whether LOITER itself was verified."""
        fallback_loiter_verified = None
        final_snap = None
        if ensure_loiter:
            fallback_loiter_verified = self._ensure_loiter()
            try:
                final_snap = self._build_snapshot(self.gateway.read_vehicle_state())
            except Exception:
                final_snap = None
        with self._state_lock:
            self._last_error = {"code": code, "message": message}
            if detail is not None:
                self._last_error["detail"] = detail
            if ensure_loiter:
                # The failure response records the fallback LOITER command and
                # its verification result (task timeout-behavior contract).
                self._last_error["fallback_loiter_verified"] = fallback_loiter_verified
        terminal = to_state in (FAILED, SUSPENDED, COMPLETED_HOLD)
        terminal_evidence = None
        if ensure_loiter:
            seq_block = self._sequence_block()
            current_waypoint = final_snap.current_sequence if final_snap is not None else None
            mission_count = final_snap.mission_count if final_snap is not None else None
            terminal_evidence = {
                "operation_id": op_id, "from": previous,
                "mission_execution_state": to_state,
                "mission_execution_phase": None,
                "final_mode": final_snap.mode_name if final_snap is not None else None,
                "final_armed": final_snap.armed if final_snap is not None else None,
                "final_authority": final_snap.control_authority if final_snap is not None else None,
                "current_waypoint": current_waypoint if current_waypoint is not None else seq_block.get("current"),
                "mission_count": mission_count if mission_count is not None else seq_block.get("count"),
                "route_hash": self._active_route_hash,
                "mission_id": self._mission_id,
                # Never claims LOITER when it wasn't verified -- the same
                # boolean already carried on the API error response above,
                # mirrored here so the recorder gets the identical fact.
                "fallback_loiter_verified": fallback_loiter_verified,
            }
        self._transition(to_state, message, terminal=terminal, terminal_evidence=terminal_evidence)
        self._emit(f"mission_execution_{operation}_failed",
                   f"{operation} failed: {message}", "warning")
        error = {"code": code, "message": message}
        if detail is not None:
            error["detail"] = detail
        if ensure_loiter:
            error["fallback_loiter_verified"] = fallback_loiter_verified
        return {
            "accepted": True, "operation": operation, "outcome": to_state,
            "operation_id": op_id, "execution_state": to_state,
            "mission_id": self._mission_id, "route_hash": self._active_route_hash,
            "previous_state": previous, "current_state": to_state,
            "verified_mode": None, "home_result": None,
            "sequence": self._sequence_block(),
            "error": error,
            "final": True,
        }

    def _success_result(self, operation: str, op_id: str, previous: str, to_state: str,
                        verified_mode: Optional[str], home_result=None, prog=None) -> Dict[str, Any]:
        return {
            "accepted": True, "operation": operation, "outcome": to_state,
            "operation_id": op_id, "execution_state": to_state,
            "mission_id": self._mission_id, "route_hash": self._active_route_hash,
            "previous_state": previous, "current_state": to_state,
            "verified_mode": verified_mode, "home_result": home_result,
            "sequence": self._sequence_block(),
            "progression": prog,
            "error": None, "final": True,
        }

    def _idempotent_result(self, operation: str, code: str, message: str,
                           verified_mode: Optional[str] = None) -> Dict[str, Any]:
        with self._state_lock:
            state = self._state
        return {
            "accepted": True, "operation": operation, "outcome": code,
            "operation_id": self._active_operation_id, "execution_state": state,
            "mission_id": self._mission_id, "route_hash": self._active_route_hash,
            "previous_state": state, "current_state": state,
            "verified_mode": verified_mode, "home_result": None,
            "sequence": self._sequence_block(),
            "error": None, "idempotent": True, "final": True,
            "message": message,
        }

    def _rejected_result(self, operation: str, code: str, message: str) -> Dict[str, Any]:
        with self._state_lock:
            state = self._state
        return {
            "accepted": False, "operation": operation, "outcome": "REJECTED",
            "operation_id": None, "execution_state": state,
            "mission_id": self._mission_id, "route_hash": self._active_route_hash,
            "previous_state": state, "current_state": state,
            "verified_mode": None, "home_result": None,
            "sequence": self._sequence_block(),
            "error": {"code": code, "message": message}, "final": True,
        }

    def _busy_result(self, operation: str) -> Dict[str, Any]:
        return self._rejected_result(
            operation, "OPERATION_IN_PROGRESS",
            "a mission-execution operation is already in progress (only one at a time)")

    def _arbitration_busy_result(self, operation: str) -> Dict[str, Any]:
        owner = write_arbiter.current_owner()
        return self._rejected_result(
            operation, "ARBITRATION_BUSY",
            f"the vehicle write arbiter is held by {owner}; try again once it is released")

    def _sequence_block(self) -> Dict[str, Any]:
        with self._state_lock:
            return {
                "current": self._last_sequence,
                "count": self._last_count,
                "before_pause": self._sequence_before_pause,
                "at_resume": self._sequence_at_resume,
                "first_after_resume": self._first_sequence_after_resume,
                "continuation_verified": self._continuation_verified,
            }

    def _emit(self, event_type: str, message: str, severity: str):
        if self._event_cb is not None:
            try:
                self._event_cb(event_type, message, severity)
            except Exception:
                pass

    def _persist_locked(self):
        if self._store is not None:
            self._store.save_from(self)

    def is_running(self) -> bool:
        return self._action_lock.locked()

    def bound_original_mission(self) -> Optional[Dict[str, Any]]:
        """The ORIGINAL mission identity this controller currently owns, for the
        replan controller's fresh pre-replan original-mission proof (task section
        2 / CRITICAL ISSUE 2). Returns the Operator/package mission identity that
        was PROVEN and bound at Start -- never the vehicle's null
        current_mission_id -- plus the immutable original route hash and the
        original route waypoint count from the stored package.

        None unless a live ORIGINAL mission is bound (RUNNING/PAUSED/
        RETURNING_HOME/HOME_ARRIVAL_PENDING) with a proven mission_id and original
        route hash. After a safe-return handoff, _original_route_hash still holds
        the ORIGINAL hash (the revised hash is tracked separately as
        _active_route_hash), so this stays the original-mission proof even while
        the revised mission runs."""
        with self._state_lock:
            if self._state not in _LIVE_STATES:
                return None
            mission_id = self._mission_id
            original_hash = self._original_route_hash
            state = self._state
        if not mission_id or not original_hash:
            return None
        route = (planning_package.load() or {}).get("route") or []
        return {
            "mission_id": mission_id,
            "original_route_hash": original_hash,
            "original_route_count": len(route),
            "execution_state": state,
        }

    def _original_mission_evidence(self, binding: Dict[str, Any], proof_snap: Any) -> Dict[str, Any]:
        """The proven-original-mission evidence bundle for the experiment
        recorder's original_mission.json (task section 10) -- the SAME proven
        identity/hash/route this Start just bound, plus the stored package's
        route/Home, never a separately-invented copy. Read-only; never mutates
        anything. Best-effort -- called from a path that already tolerates a
        missing/partial package (Start would have failed closed before this
        point if the package itself were unusable)."""
        pkg = planning_package.load() or {}
        return {
            "mission_id": binding.get("mission_id"),
            "route_hash": binding.get("original_route_hash"),
            "route": pkg.get("route"),
            "route_count": len(pkg.get("route") or []),
            "home": pkg.get("home"),
            "planning_home": pkg.get("planning_home") or pkg.get("home"),
            "verified_home": None,  # filled by a later run.update if/when Home is verified
            "mission_revision": pkg.get("mission_revision"),
            "start_evidence": {
                "initial_armed": getattr(proof_snap, "armed", None),
                "launch_latitude": getattr(proof_snap, "latitude", None),
                "launch_longitude": getattr(proof_snap, "longitude", None),
                "authority_before": self._last_authority,
            },
        }

    def _binding_block(self) -> Dict[str, Any]:
        """Explicit mission-binding evidence for status (task section 1 / 11).
        MUST be called with self._state_lock held. Reads the stored package's
        identity (a fast file read, no network) and compares it against the
        controller's live bound original mission_id so a re-upload that mints a
        new immutable package is visible as a distinct, unbound package rather
        than silently replacing the old bound identity.

        binding_state:
          UNBOUND        -- no original mission is currently bound to execution
                            (idle/terminal/rearmed). The stored package, if any,
                            is what a Start would prepare/bind next.
          BOUND          -- a live/at-start binding whose mission_id matches the
                            stored package's mission_id.
          STALE_MISMATCH -- a live binding remains but the stored package now
                            carries a DIFFERENT mission_id (a new package was
                            uploaded under the running/paused original). Never
                            auto-adopted: Start/rearm must intentionally rebind.
        """
        bound_id = self._mission_id if self._state in _LIVE_STATES else None
        try:
            pkg = planning_package.load()
        except Exception:
            pkg = None
        pkg_mid = pkg.get("mission_id") if isinstance(pkg, dict) else None
        pkg_hash = None
        if isinstance(pkg, dict):
            pkg_hash = pkg.get("original_route_hash") or pkg.get("route_hash")
        if bound_id is None:
            binding_state = "UNBOUND"
        elif pkg_mid is not None and bound_id != pkg_mid:
            binding_state = "STALE_MISMATCH"
        else:
            binding_state = "BOUND"
        return {
            "bound_original_mission_id": bound_id,
            "bound_original_route_hash": self._original_route_hash if bound_id is not None else None,
            "package_mission_id": pkg_mid,
            "package_route_hash": pkg_hash,
            # The route hash the last completed readiness proof matched against the
            # live Pixhawk readback -- the content/hash evidence the bind requires.
            "verified_route_hash": self._readiness_original_hash,
            "binding_state": binding_state,
        }

    # ── Canonical status (task section 10) ────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            replan_active = self._replanning_active or (
                write_arbiter.current_owner() == write_arbiter.OWNER_REPLANNING)
            state = self._state
            effective = "REPLANNING" if (replan_active and state in _LIVE_STATES) else state
            # Continuously-updated ADVISORY mission-energy-feasibility evidence
            # (task: mission energy feasibility, section 9; task: RTL Home /
            # Start-readiness semantics correction). `feasibility_ok` is True
            # whenever no feasibility evidence has been pushed in yet (None --
            # preserves every existing caller/test that never wires
            # update_energy_feasibility, so their can_start/start_eligible are
            # unchanged), True whenever mission_feasible is POSITIVELY True and
            # rtl_return_feasible is NOT explicitly False, and False otherwise.
            # This mirrors _run_start's own PHASE 1 pre-Home gate exactly (see
            # its docstring): mission_feasible must be positively proven (False
            # OR unknown fails closed -- it never depends on Home, so there is
            # no reason to ever wait on it) but rtl_return_feasible is only
            # required to not be PROVEN infeasible here -- merely UNKNOWN
            # (REASON_RTL_HOME_UNAVAILABLE, the expected state before Home is
            # verified) must never make an otherwise-startable mission look
            # un-startable, exactly the live contradiction this task fixes
            # ("RTL Home unavailable" / "Start Mission disabled" before Home is
            # ever set, when Start itself is what sets it). This is the
            # LIVE/CACHED display+soft-gate axis (drives can_start/
            # start_eligible below); the AUTHORITATIVE checks that can actually
            # reject an operation always run fresh inline in _run_start (PHASE
            # 1 before ARM, PHASE 2 -- requiring BOTH dimensions positively
            # True -- after Home is verified and strictly before AUTO), never
            # from this cached value.
            feasibility = self._feasibility
            feasibility_ok = feasibility is None or (
                feasibility.get("mission_feasible") is True
                and feasibility.get("rtl_return_feasible") is not False)
            # READY / can_start is asserted only when the full read-only Start
            # identity proof currently holds (usable package, package/Pixhawk
            # route-hash match, LOCAL_AGENT authority, fresh state) -- not merely
            # because a package is stored. `_readiness_ready` is the cached result
            # of that proof; the controller only enters READY when it is True.
            can_start = (state == READY and self._readiness_ready and not replan_active
                         and self.cfg.mission_execution_enabled and not self._action_lock.locked()
                         and feasibility_ok)
            can_pause = (state == RUNNING and not replan_active and not self._action_lock.locked())
            can_resume = (state == PAUSED and not replan_active and not self._action_lock.locked())
            # While idle, surface the PROVEN identity/hashes from the readiness
            # proof so an operator sees exactly what a Start would bind; once an
            # operation binds them, the live values take precedence.
            mission_id = self._mission_id if self._mission_id is not None else self._readiness_mission_id
            original_hash = (self._original_route_hash if self._original_route_hash is not None
                             else self._readiness_original_hash)
            active_hash = (self._active_route_hash if self._active_route_hash is not None
                           else self._readiness_active_hash)
            # ── Mission-binding evidence (task section 1 / 11) ──────────────────
            # Explicitly distinguish the ORIGINAL mission this controller has
            # BOUND as its live execution identity from the mission_id of the
            # currently stored planning package, so a re-upload that mints a new
            # immutable package is never silently conflated with the old bound
            # execution. Never asserts a binding from mission_id alone: a live
            # binding only exists once Start proved package identity + fresh route
            # content/hash and bound it.
            binding = self._binding_block()
            # ── Start eligibility vs execution readiness (task sections 2 & 3) ──
            # Two distinct axes, so the Operator can tell "the mission is ready,
            # just hand off authority" from "the mission itself is not ready":
            #   * execution_ready -- the FULL read-only Start proof holds RIGHT NOW
            #     (evidence AND LOCAL_AGENT authority): can run without any handoff.
            #   * start_eligible  -- all mission/package/Pixhawk/position EVIDENCE
            #     is proven; the only thing possibly missing is the LOCAL_AGENT
            #     authority handoff, which the Operator Start transaction performs
            #     itself before invoking the Scout Start lifecycle. So an OPERATOR-
            #     authority pre-Start state is start_eligible=true, execution_ready
            #     =false -- honestly reflecting the pending handoff, NOT an
            #     un-startable mission.
            #   * authority_blocks_start -- start-eligible but blocked ONLY by the
            #     authority axis right now.
            #   * start_block_reason -- the single machine-readable current block.
            execution_ready = bool(self._readiness_ready)
            start_eligible = bool(self._readiness_evidence_ready and not replan_active
                                  and self.cfg.mission_execution_enabled
                                  and not self._action_lock.locked()
                                  and self._state in (NOT_READY, READY)
                                  and feasibility_ok)
            authority_blocks_start = bool(start_eligible and not can_start and feasibility_ok
                                          and self._readiness_reason in _AUTHORITY_BLOCK_REASONS)
            if can_start:
                start_block_reason = None
            elif not self.cfg.mission_execution_enabled:
                start_block_reason = "MISSION_EXECUTION_DISABLED"
            elif replan_active:
                start_block_reason = "REPLANNING_ACTIVE"
            elif self._action_lock.locked():
                start_block_reason = "OPERATION_IN_PROGRESS"
            elif state not in (READY, NOT_READY):
                start_block_reason = "NOT_STARTABLE"
            elif not feasibility_ok:
                start_block_reason = (feasibility or {}).get("reason") or "ENERGY_FEASIBILITY_UNKNOWN"
            else:
                start_block_reason = self._readiness_reason or "NOT_READY"
            return {
                "supported": True,
                "state": state,
                "effective_state": effective,
                # Operator-facing neutral phase + human label (task section 10).
                "phase": self._phase,
                "phase_label": PHASE_LABELS.get(self._phase),
                # Immutable Start operation snapshot + last progression evidence
                # (task safety item 1 / progression evidence contract).
                "start_snapshot": self._start_snapshot,
                "progression_evidence": self._progression_evidence,
                # Machine-readable evidence of the most recent Stop transaction
                # (hold / restore / rewind / reset / authority / readiness). Present
                # only after a Stop has run this process; runtime-only (not persisted).
                "stop": self._stop_evidence,
                # Post-restart reconciliation evidence: the prior persisted state
                # and whether fresh evidence re-proved it. Present only after a
                # restart from a stable autonomous state.
                "recovery": self._recovery,
                "active_operation_id": self._active_operation_id,
                "mission_id": mission_id,
                "original_route_hash": original_hash,
                "active_route_hash": active_hash,
                "verified_home": self._verified_home,
                "home_verification_distance_m": self._home_verification_distance_m,
                "mode": self._last_mode,
                "sequence": {
                    "current": self._last_sequence,
                    "count": self._last_count,
                    "before_pause": self._sequence_before_pause,
                    "at_resume": self._sequence_at_resume,
                    "first_after_resume": self._first_sequence_after_resume,
                    "continuation_verified": self._continuation_verified,
                },
                "timestamps": {
                    "start": self._start_ts, "pause": self._pause_ts, "resume": self._resume_ts,
                },
                "replanning": {
                    "active": bool(replan_active),
                    "fsm_state": self._last_replan_fsm,
                },
                "return_completion": {
                    "distance_to_home_m": self._distance_to_home_m,
                    "arrival_radius_m": self.cfg.home_arrival_radius_m,
                    "persistence_s": self.cfg.home_arrival_persistence_s,
                    "persistence_progress_s": (
                        None if self._arrival_since is None
                        else round(self._clock() - self._arrival_since, 2)),
                    "arrival_confirmed": self._arrival_confirmed,
                    "final_loiter_verified": bool(self._final_loiter_verified) if self._final_loiter_verified is not None else False,
                },
                # Normal ORIGINAL-mission completion evidence (task section 3):
                # the final-item persistence timer + the multi-signal evidence
                # snapshot that drives RUNNING -> COMPLETED_HOLD, distinct from
                # return_completion above (which is the return-to-Home arrival).
                "completion": {
                    "candidate": self._completion_since is not None,
                    "confirmed": bool(self._completion_confirmed),
                    "final_item_tolerance": self.cfg.mission_complete_final_item_tolerance,
                    "persistence_s": self.cfg.mission_complete_persistence_s,
                    "persistence_progress_s": (
                        None if self._completion_since is None
                        else round(self._clock() - self._completion_since, 2)),
                    "evidence": self._completion_evidence,
                    "final_loiter_verified": (bool(self._final_loiter_verified)
                                              if self._final_loiter_verified is not None else False),
                },
                "binding": binding,
                # Set when a newly verified original package arrived during active
                # execution and was not adopted (task section 2); None otherwise.
                "package_conflict": self._package_conflict,
                "execution_ready": execution_ready,
                "start_eligible": start_eligible,
                "start_block_reason": start_block_reason,
                "authority_blocks_start": bool(authority_blocks_start),
                "authority_status": self._last_authority,
                "readiness": {
                    "ready": bool(self._readiness_ready),
                    # CHECKING: a temporary freshness/read gap while still proven
                    # ready -- surface it as "checking / proof refreshing", never
                    # as a package inconsistency (task section 7).
                    "checking": bool(self._readiness_checking),
                    "reason": self._readiness_reason,
                    "detail": self._readiness_detail,
                    "last_evaluated_at": self._last_readiness_eval_ts,
                    # The last COMPLETED, FRESH consistency proof, retained across
                    # refresh/proof-stale windows (never erased by a transient gap).
                    "last_verified": planning_package.last_verified_proof(),
                },
                # Mission-energy-feasibility evidence (task: mission energy
                # feasibility, sections 9/12) -- the LIVE/CACHED advisory value
                # pushed in by update_energy_feasibility() each iteration, in
                # the Operator-facing shape (Agent Mission card: "Energy:
                # FEASIBLE +24%"). "NOT_YET_EVALUATED" (distinct from the
                # module's own UNKNOWN reasons) means no caller has wired the
                # continuous evaluation in this process yet -- honestly
                # different from "evaluated and evidence is missing".
                "energy_feasibility": (
                    dict(feasibility) if feasibility is not None else {
                        "status": mission_feasibility.STATUS_UNKNOWN,
                        "reason": "NOT_YET_EVALUATED",
                        "message": "continuous feasibility evaluation has not run yet",
                        "mission_feasible": None, "rtl_return_feasible": None,
                        "battery_percent": None, "battery_source": None,
                        "planned_home": None, "rtl_home": None,
                        "planned_completion_distance_m": None, "rtl_return_distance_m": None,
                        "mission_margin_percent": None, "rtl_return_margin_percent": None,
                        "mission_geometry_source": None, "rtl_return_geometry_source": None,
                        "evaluated_at": None,
                    }
                ),
                # Continuous risk assessment (risk_model.py) -- the LIVE/CACHED
                # OBSERVATIONAL/ADVISORY value pushed in by
                # update_risk_assessment() each iteration. Gates nothing here
                # (can_start/can_pause/can_resume above are entirely
                # independent of this block) -- see risk_model.py's module
                # docstring. "NOT_YET_EVALUATED" (distinct from risk_model's
                # own UNKNOWN level) means no caller has wired the continuous
                # evaluation in this process yet.
                "risk": (
                    dict(self._risk) if self._risk is not None else {
                        "score": None, "level": "UNKNOWN",
                        "components": {}, "weights": {},
                        "dominant_component": None, "dominant_reason": "NOT_YET_EVALUATED",
                        "hard_constraint_violated": False, "feasibility_status": None,
                        "confidence": "UNKNOWN", "evaluated_at": None,
                        "recommendation": "CONTINUE_WITH_CAUTION",
                        # Aggregate-semantics correction task section 5/19 --
                        # same shape risk_model.RiskResult.to_dict() now
                        # produces, so a caller reading status() before the
                        # first evaluation sees the full contract, not a
                        # partial one.
                        "weighted_score": None, "weighted_level": "UNKNOWN",
                        "component_floor_level": None, "component_floor_reason": None,
                        "component_floor_source": None, "hard_override_level": None,
                    }
                ),
                "can_start": bool(can_start),
                "can_pause": bool(can_pause),
                "can_resume": bool(can_resume),
                "mission_execution_enabled": self.cfg.mission_execution_enabled,
                "config": _config_block(self.cfg),
                "last_error": self._last_error,
                "history": list(self._history[-10:]),
            }

    # ── Persistence hooks (used by StatusStore) ───────────────────────────────
    #
    # Persisted fields are the minimal EXPECTED-state evidence needed to detect and
    # reconcile a prior run at next startup: the state itself, the bound mission
    # identity/hashes, the verified Home, sequence bookkeeping, the last error, and
    # the reconciliation record.
    #
    # Deliberately NOT persisted (treated as CURRENT-RUN evidence, so absent after a
    # restart -- never resurrected as if this process produced them):
    #   * start_snapshot / progression_evidence -- describe THIS process's Start
    #     operation; a restored copy would carry a stale operation_id and imply a
    #     progression proof the current process never performed.
    #   * timestamps (_start_ts/_pause_ts/_resume_ts) and history -- wall-clock and
    #     transition log of the prior process, rebuilt fresh (recovery appends its
    #     own history entry describing what actually happened this run).
    # recover_after_restart() re-proves the live picture from fresh vehicle evidence
    # rather than trusting any of this; persisted state is evidence, not proof.
    def _persistable(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "active_operation_id": self._active_operation_id,
            "mission_id": self._mission_id,
            "original_route_hash": self._original_route_hash,
            "active_route_hash": self._active_route_hash,
            "verified_home": self._verified_home,
            "home_verification_distance_m": self._home_verification_distance_m,
            "sequence_before_pause": self._sequence_before_pause,
            "sequence_at_resume": self._sequence_at_resume,
            "first_sequence_after_resume": self._first_sequence_after_resume,
            "continuation_verified": self._continuation_verified,
            "last_error": self._last_error,
            "recovery": self._recovery,
        }

    def _restore(self, data: Dict[str, Any]) -> None:
        self._state = data.get("state", NOT_READY)
        self._active_operation_id = data.get("active_operation_id")
        self._mission_id = data.get("mission_id")
        self._original_route_hash = data.get("original_route_hash")
        self._active_route_hash = data.get("active_route_hash")
        self._verified_home = data.get("verified_home")
        self._home_verification_distance_m = data.get("home_verification_distance_m")
        self._sequence_before_pause = data.get("sequence_before_pause")
        self._sequence_at_resume = data.get("sequence_at_resume")
        self._first_sequence_after_resume = data.get("first_sequence_after_resume")
        self._continuation_verified = data.get("continuation_verified")
        self._last_error = data.get("last_error")
        self._recovery = data.get("recovery")


def _config_block(cfg) -> Dict[str, Any]:
    try:
        resolved, sources = me_config.resolve()
        return {"values": resolved.to_dict(), "sources": sources}
    except Exception:
        return {"values": cfg.to_dict(), "sources": {}}


def _summarize_home(result) -> Optional[Dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    return {
        "accepted": result.get("accepted"),
        "verified": result.get("verified"),
        "requested_position": result.get("requested_position"),
        "home_position": result.get("home_position"),
        "verification_distance_m": result.get("verification_distance_m"),
        "error": result.get("error"),
    }


# ── Restart-safe status store ─────────────────────────────────────────────────
class StatusStore:
    """Persists the controller's restart-relevant fields to a JSON file (atomic
    replace), so an interrupted operation is detectable at next startup. Kept
    small and separate so the controller stays testable without touching disk
    (tests pass status_store=None)."""

    def __init__(self, path: Optional[str] = None):
        import config as _config
        self.path = path or getattr(_config, "MISSION_EXECUTION_STATUS_FILE", None)

    def load_into(self, controller: "MissionExecutionController") -> None:
        if not self.path:
            return
        import json
        try:
            with open(self.path, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            controller._restore(data)

    def save_from(self, controller: "MissionExecutionController") -> None:
        if not self.path:
            return
        import json
        import os
        tmp = f"{self.path}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(controller._persistable(), f)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[MISSION_EXEC] could not persist controller status: {e}")
