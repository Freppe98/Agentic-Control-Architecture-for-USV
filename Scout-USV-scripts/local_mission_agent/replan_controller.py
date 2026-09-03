"""
The replanning controller: the one component that owns an agent-initiated
mission-replan transaction end to end.

It deliberately concentrates every piece of transactional state and every
vehicle-write side effect that the read-only decision_engine must NOT have:

  * the active transition id and the replanning FSM state;
  * the decision snapshot id the transaction is bound to;
  * the mission revision number and the revision record;
  * bounded retry counters and a post-terminal cooldown;
  * one-action-at-a-time protection (a non-blocking lock -- a duplicate trigger
    while a transaction is in flight is suppressed, never run twice);
  * a fresh control-authority check (autonomy_gate) immediately before EVERY
    vehicle write, so authority moving away from LOCAL_AGENT stops writes
    within the transaction rather than being raced;
  * the failure/fallback policy;
  * a restart-safe persisted status (an interrupted transaction is failed
    closed at next startup, never resumed).

All vehicle operations go through an injected gateway (replan_gateway.py) that
calls the existing verified Flask endpoints -- there is no direct-MAVLink path
here. Planning/validation is delegated to safe_return_planner.py.

FSM
---
    MONITORING ── replan required & permitted ──> HOLD_REQUESTED
    HOLD_REQUESTED ── verified LOITER ──> HOLD_CONFIRMED
    HOLD_CONFIRMED ──> PLANNING ──> VALIDATING ──> UPLOAD_REQUESTED
                   ──> VERIFYING_REVISION ──> RESUME_REQUESTED
                   ── verified AUTO & progression ──> MONITORING_REVISED  (success)

From HOLD_CONFIRMED onward, ANY failure leaves the vehicle in LOITER:
    plan/validate/upload/readback/resume failure ──(bounded retries)──> SAFE_HOLD
    retries exhausted & RTL fallback permitted ──> FALLBACK_RTL (verified RTL)
    authority lost mid-transaction ──> SUSPENDED (writes stop, stay in LOITER)
LOITER never confirmed at all ──> FAILED (vehicle mode left unchanged).

INVARIANT: SAFE_HOLD means the hold has been POSITIVELY PROVEN -- verified
LOITER mode AND fresh groundspeed at/below threshold AND the required
persistence (_acquire_hold_settle()) -- never merely requested or
mode-confirmed. A HOLD-only action request (decision_policy.
ACTION_REQUEST_HOLD) is bound by this SAME proof:
    HOLD_CONFIRMED ──> _acquire_hold_settle() proves settled ──> SAFE_HOLD
                                                                  (_direct_safe_hold)
    HOLD_CONFIRMED ──> _acquire_hold_settle() times out, never settles,
                        OR the final defensive re-assert fails to verify
                                                              ──> SUSPENDED
                                                                  (_hold_not_proven,
                                                                   never SAFE_HOLD)
Either way a HOLD-only request NEVER attempts PLANNING/VALIDATING/UPLOAD/RTL.

Obstacle replanning is groundwork only: obstacle_execution_enabled is always
False here and no REPLAN_OBSTACLE path is wired -- see obstacle_model.py /
detour_planner.py (dry-run).
"""
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

import autonomy_gate
import decision_policy
import energy_policy
import geo
import mission_feasibility
import mission_progression
import mission_revision
import planning_package
import replan_config
import route_hash
import safe_return_planner

# ── FSM states ──────────────────────────────────────────────────────────────
MONITORING = "MONITORING"
HOLD_REQUESTED = "HOLD_REQUESTED"
HOLD_CONFIRMED = "HOLD_CONFIRMED"
PLANNING = "PLANNING"
VALIDATING = "VALIDATING"
UPLOAD_REQUESTED = "UPLOAD_REQUESTED"
VERIFYING_REVISION = "VERIFYING_REVISION"
RESUME_REQUESTED = "RESUME_REQUESTED"
MONITORING_REVISED = "MONITORING_REVISED"
SAFE_HOLD = "SAFE_HOLD"
SUSPENDED = "SUSPENDED"
FALLBACK_RTL = "FALLBACK_RTL"
FAILED = "FAILED"

# Terminal states an observe() may start a new transaction from (after cooldown).
_IDLE_STATES = (MONITORING, MONITORING_REVISED, SAFE_HOLD, SUSPENDED, FALLBACK_RTL, FAILED)
# States that mean a MAVLink/transaction step was in flight -- interrupted if
# found persisted at startup.
_INTERRUPTIBLE_STATES = (
    HOLD_REQUESTED, HOLD_CONFIRMED, PLANNING, VALIDATING,
    UPLOAD_REQUESTED, VERIFYING_REVISION, RESUME_REQUESTED,
)

# Outcome sentinels from an internal step attempt.
_SUCCESS = "SUCCESS"
_SUSPEND = "SUSPEND"
_FAIL_STEP = "FAIL_STEP"

BLOCKED_BY_AUTHORITY = "REPLAN_REQUIRED_BUT_BLOCKED_BY_AUTHORITY"

# Fail-closed codes for the fresh pre-replan ORIGINAL-mission proof (CRITICAL
# ISSUE 2). Any of these means the transaction did NOT touch the vehicle: no
# LOITER, no upload, no AUTO -- the mission is never replaced without a fresh
# proof of what is physically on the Pixhawk.
ORIGINAL_MISSION_PROOF_UNAVAILABLE = "ORIGINAL_MISSION_PROOF_UNAVAILABLE"
ORIGINAL_MISSION_HASH_MISMATCH = "ORIGINAL_MISSION_HASH_MISMATCH"
ORIGINAL_MISSION_COUNT_MISMATCH = "ORIGINAL_MISSION_COUNT_MISMATCH"
ORIGINAL_MISSION_ID_MISMATCH = "ORIGINAL_MISSION_ID_MISMATCH"


class ReplanController:
    def __init__(
        self,
        cfg: Optional[replan_config.ReplanConfig] = None,
        gateway: Any = None,
        status_store: Optional["StatusStore"] = None,
        event_callback: Optional[Callable[[str, str, str], None]] = None,
        original_mission_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
        clock: Callable[[], float] = time.time,
        recorder: Any = None,
        feasibility_fn: Optional[Callable[[], Optional[Dict[str, Any]]]] = None,
    ):
        self.cfg = cfg or replan_config.DEFAULT
        self.gateway = gateway
        self._store = status_store
        self._event_cb = event_callback   # (event_type, message, severity) -> None
        # Returns the mission-execution controller's bound ORIGINAL mission
        # identity {mission_id, original_route_hash, original_route_count} (or
        # None). Used to prove, before any vehicle write, that the package we are
        # about to replan from is the SAME mission that controller is running --
        # never vehicle_state.mission.current_mission_id (null on this path).
        self._original_mission_fn = original_mission_fn
        # Returns the LATEST feasibility evidence dict (mission_feasible/
        # rtl_return_feasible/status, decision_policy.ActionRequest.
        # feasibility_evidence's shape) or None (E2 water-trial integration
        # task: RTL fallback proof). Lazily resolved every call, mirroring
        # original_mission_fn above -- the caller (local_agent.py) wires this
        # to decision_policy_instance.latest_feasibility_evidence, which the
        # main loop keeps continuously refreshed every iteration (including
        # while a replan transaction runs on its own thread), so this is
        # "current" to within one main-loop iteration, never a value bound at
        # transaction start. Fails closed to None (-> treated as UNKNOWN, not
        # feasible) if unset or raising.
        self._feasibility_fn = feasibility_fn
        self._clock = clock
        # Thesis Experiment Recorder -- OBSERVATIONAL only, duck-typed/injected
        # exactly like event_callback above (task section 0: never influences
        # replanning; every call site wraps this in try/except).
        self._recorder = recorder
        # Sleep hook for the shared progression watch -- real time.sleep in
        # production (the watch runs on the transaction daemon thread, never the
        # main loop), an injectable clock-advance in tests so the full deadline
        # is exercised deterministically.
        self._sleep = time.sleep

        self._action_lock = threading.Lock()   # one-action-at-a-time
        self._state_lock = threading.Lock()     # guards the fields below

        self._state = MONITORING
        self._active_transition_id: Optional[str] = None
        self._snapshot_id: Optional[str] = None
        self._revision_number = 0
        self._retry_count = 0
        self._last_terminal_at: Optional[float] = None
        # ── Terminal trigger-generation latch (task section 1) ─────────────────
        # The live bench showed cooldown-based auto-retry of an UNCHANGED terminal
        # trigger is undesirable: after a SAFE_HOLD the same still-active
        # force_safe_return re-fired once cooldown expired, against an already
        # SUSPENDED/unbound mission. The contract is now: each time the risk/
        # injection condition transitions inactive->active it is assigned a new
        # generation; AT MOST ONE bounded transaction runs per generation; once a
        # transaction reaches a terminal (SUCCESS or FAILED/SAFE_HOLD/SUSPENDED/
        # FALLBACK_RTL) that generation is CONSUMED/latched and cooldown expiry
        # alone never re-triggers it. A new transaction requires a genuinely new
        # generation: the trigger goes false then true again, the injection is
        # cleared+reapplied, the controller is reset, or a new original mission
        # execution begins. Runtime-only (never persisted -- a fresh process
        # starts a fresh trigger context).
        self._trigger_active = False              # was the risk condition active last observe?
        self._trigger_generation = 0              # increments on each inactive->active edge
        self._active_generation: Optional[int] = None      # generation the running transaction is bound to
        self._consumed_generation: Optional[int] = None    # last generation a transaction consumed
        self._terminal_reason: Optional[str] = None        # terminal state/outcome of the consumed generation
        # ── Authoritative decision-policy action request (E2 water-trial task) ──
        # The most recently observed ActionRequest (decision_policy.py), for
        # status()/recorder display -- purely observational. `_generation_hold_only`
        # is captured ONLY at the same inactive->active edge that assigns
        # _trigger_generation above, so it reflects "was THIS generation born as a
        # HOLD-only request" and is immune to later observe() calls changing the
        # in-flight generation's classification (same edge-scoping the generation
        # counter itself already relies on). `_active_hold_only` is the value
        # _run() actually binds to and acts on, captured atomically alongside
        # _active_generation at transaction start.
        self._last_action_request: Optional[Dict[str, Any]] = None
        self._generation_hold_only = False
        self._active_hold_only = False
        self._last_error: Optional[Dict[str, Any]] = None
        self._last_authority: Optional[str] = None
        self._authority_blocked = False
        self._last_decision = energy_policy.DECISION_MONITOR
        self._last_reason_codes: List[str] = []
        self._last_energy_inputs: Dict[str, Any] = {}
        self._last_energy_simulated = False
        self._current_revision: Optional[mission_revision.MissionRevision] = None
        self._last_revision_dict: Optional[Dict[str, Any]] = None
        self._history: List[Dict[str, Any]] = []
        self._simulated_run = False
        # Planning-package/mission consistency (updated every observe) and the
        # last transaction's geometry-validation report.
        self._consistency_state = planning_package.CONSISTENCY_MISSING
        self._consistency_detail: Dict[str, Any] = {}
        self._last_geometry_validation: Optional[Dict[str, Any]] = None
        # Fresh pre-replan ORIGINAL-mission proof + revised-mission progression
        # evidence for status/trace (CRITICAL ISSUE 2 / 1). The proven mission id
        # is what the shared progression verifier holds as the expected identity.
        self._proven_mission_id: Optional[str] = None
        self._last_original_proof: Optional[Dict[str, Any]] = None
        self._last_revised_progression: Optional[Dict[str, Any]] = None
        # Battery diagnostics (task section 5): the RAW and normalized battery of
        # the CURRENT observed snapshot -- never a cached old percentage. Lets an
        # intermittent BATTERY_UNAVAILABLE (ArduPilot battery_remaining == -1 or an
        # absent sample) be diagnosed without changing any safety semantics: an
        # unavailable/invalid battery stays unavailable, never faked to a value.
        self._last_battery: Optional[Dict[str, Any]] = None
        # Bounded HOLD-SETTLE proof-acquisition diagnostics (E2 replan
        # armed-LOITER upload race fix): the most recent settle wait's
        # attempts/elapsed_s/samples + whether it confirmed (True), timed out
        # (False), or is still in progress (None). None before any transaction
        # has ever reached HOLD_CONFIRMED. Purely observational -- see
        # _acquire_hold_settle.
        self._last_hold_settle: Optional[Dict[str, Any]] = None

        if self._store is not None:
            self._store.load_into(self)

    # ── Restart safety ────────────────────────────────────────────────────────
    def recover_after_restart(self) -> None:
        """If the persisted FSM state is an in-flight one, a transaction was
        interrupted. Fail it closed -- the vehicle-side outcome is unknowable, so
        do not resume; the operator must re-trigger from a known state."""
        with self._state_lock:
            if self._state in _INTERRUPTIBLE_STATES:
                interrupted = self._state
                self._state = FAILED
                self._last_error = {
                    "code": "UNKNOWN_AFTER_RESTART",
                    "message": (
                        f"Local Agent restarted while a replan was in {interrupted}; "
                        "the transaction was interrupted at an unknown point and was "
                        "NOT resumed. Re-trigger from a known state."
                    ),
                    "interrupted_state": interrupted,
                }
                self._last_terminal_at = self._clock()
                self._persist_locked()
                print(f"[REPLAN] recovered interrupted transaction in {interrupted} -> FAILED "
                      "UNKNOWN_AFTER_RESTART (not resumed)")

    # ── Observation / start decision ──────────────────────────────────────────
    def observe(self, snapshot, energy_result: energy_policy.EnergyResult,
                action_request: Optional[decision_policy.ActionRequest] = None,
                now: Optional[float] = None) -> Dict[str, Any]:
        """Called every loop iteration with the current snapshot + energy result
        + the authoritative decision policy's ActionRequest (decision_policy.py,
        E2 water-trial integration task). Updates reported status and returns
        {"start": bool, "reason": str}. Never itself runs a transaction -- the
        caller decides threading and calls run_transaction() when start is True.

        `action_request` is now the SOLE authoritative trigger into this FSM
        (one controller, one entry point, one trigger-generation latch -- never
        a second independent trigger). `energy_result` is retained ONLY as
        evidence/debounce/diagnostics: its own persistence-debounced decision
        (`EnergyResult.decision`), reason codes, and inputs are still recorded
        and surfaced in status()/the recorder for observability and for
        decision_policy.py's own upstream reasoning, but `energy_result.decision
        == DECISION_REPLAN_SAFE_RETURN` no longer independently sets `want` --
        it must flow through risk_model.py's recommendation and
        decision_policy.py's ActionRequest to ever start a transaction. A HOLD
        action request takes the hold-only path (straight to a verified
        LOITER/SAFE_HOLD, no return route attempted)."""
        now = self._clock() if now is None else now
        # Planning-package/mission consistency is evaluated EVERY iteration (not
        # just when replanning) so status always reflects whether the stored
        # package matches the mission the vehicle is actually running.
        consistency, detail = planning_package.check_consistency(
            planning_package.load(), snapshot.mission_id)
        action_wants_return = (action_request is not None
                               and action_request.action == decision_policy.ACTION_REQUEST_RETURN_HOME)
        action_wants_hold = (action_request is not None
                             and action_request.action == decision_policy.ACTION_REQUEST_HOLD)
        want = action_wants_return or action_wants_hold
        with self._state_lock:
            self._last_decision = energy_result.decision
            self._last_reason_codes = list(energy_result.reason_codes)
            self._last_energy_inputs = dict(energy_result.inputs)
            self._last_energy_simulated = energy_result.simulated
            self._last_action_request = action_request.to_dict() if action_request is not None else None
            self._snapshot_id = snapshot.snapshot_id if self._active_transition_id is None else self._snapshot_id
            self._authority_blocked = False
            self._consistency_state = consistency
            self._consistency_detail = detail
            # Battery diagnostics from the CURRENT snapshot (task section 5) --
            # raw + normalized + validity + freshness, reflecting THIS sample only
            # (never a cached old percentage). battery_valid False with a raw of -1
            # / None explains a BATTERY_UNAVAILABLE without any safety change.
            self._last_battery = {
                "battery_percent": getattr(snapshot, "battery_percent", None),
                "battery_valid": getattr(snapshot, "battery_valid", None),
                "battery_raw": getattr(snapshot, "battery_raw", None),
                "battery_observed_at": getattr(snapshot, "created_at", None),
                "telemetry_age_s": getattr(snapshot, "telemetry_age_s", None),
            }
            # Trigger-generation edge detection (task section 1). A new generation
            # is assigned on each inactive->active transition of the RAW risk/
            # injection condition -- independent of authority/enable/consistency,
            # so a trigger that first appears under OPERATOR authority still owns a
            # stable generation when authority is later handed to the Local Agent.
            if want and not self._trigger_active:
                self._trigger_generation += 1
                self._generation_hold_only = action_wants_hold
            self._trigger_active = want
            generation = self._trigger_generation
            consumed = self._consumed_generation

        if self._action_lock.locked():
            return {"start": False, "reason": "transaction already in progress"}

        if not want:
            return {"start": False, "reason": "no replan required"}
        if not self.cfg.autonomous_execution_enabled:
            return {"start": False, "reason": "autonomous execution disabled"}

        authority = snapshot.control_authority
        if authority == "OPERATOR":
            with self._state_lock:
                self._authority_blocked = True
            self._emit("replan_blocked_by_authority",
                       "Replan required but control authority is OPERATOR -- recommending, not executing.",
                       "warning")
            return {"start": False, "reason": BLOCKED_BY_AUTHORITY}
        if authority != "LOCAL_AGENT":
            return {"start": False, "reason": f"authority not LOCAL_AGENT ({authority})"}

        # Terminal latch (task section 1): once a transaction has consumed THIS
        # generation, an unchanged still-active condition must NOT start another --
        # this dominates cooldown, so cooldown expiry alone never re-triggers. Only
        # a genuinely new generation (trigger false->true again, injection cleared+
        # reapplied, reset(), or a new original mission) clears the latch.
        if consumed is not None and generation == consumed:
            return {"start": False,
                    "reason": "trigger generation already consumed (latched) -- clear+reapply the "
                              "condition, reset the controller, or start a new mission to re-trigger"}

        # Fail closed BEFORE putting the vehicle into LOITER if the stored
        # package does not belong to the mission the vehicle is running.
        if consistency != planning_package.CONSISTENCY_OK:
            self._emit("replan_package_inconsistent",
                       f"Replan required but planning package is {consistency} -- not executing.",
                       "warning")
            return {"start": False, "reason": consistency}

        if self._in_cooldown(now):
            return {"start": False, "reason": "cooldown"}

        return {"start": True, "reason": "replan required and permitted"}

    def _in_cooldown(self, now: float) -> bool:
        with self._state_lock:
            last = self._last_terminal_at
        return last is not None and (now - last) < self.cfg.cooldown_s

    def is_running(self) -> bool:
        """True while a transaction holds the one-action lock."""
        return self._action_lock.locked()

    def reset(self, clear_cooldown: bool = True) -> Dict[str, Any]:
        """
        Rearm the controller from a TERMINAL state back to MONITORING. Refused
        during an active transaction. Purely Local-Agent state: it issues NO
        mode command and does NOT clear the Pixhawk mission -- it only clears the
        transaction's error/transition id and (optionally) the cooldown so a
        fresh decision can start immediately instead of waiting out cooldown.
        The last revision record and revision number are preserved for audit.
        """
        if self.is_running():
            return {"reset": False, "reason": "a replan transaction is in progress"}
        with self._state_lock:
            if self._state not in _IDLE_STATES:
                return {"reset": False, "reason": f"state {self._state} is not terminal"}
            previous = self._state
            self._state = MONITORING
            self._active_transition_id = None
            self._last_error = None
            if clear_cooldown:
                self._last_terminal_at = None
            # An explicit reset is one of the sanctioned new-generation conditions
            # (task section 1): clear the terminal latch so a still-active trigger
            # can run again.
            self._rearm_trigger_locked("reset")
            self._persist_locked()
        self._emit("replan_reset", f"Controller reset from {previous} to MONITORING.", "info")
        print(f"[REPLAN] controller reset {previous} -> MONITORING (clear_cooldown={clear_cooldown})")
        return {"reset": True, "from": previous, "to": MONITORING}

    def note_new_mission(self, reason: str = "new mission execution") -> None:
        """Sanctioned new-generation condition (task sections 1 & 2): a new
        original mission execution has begun (or a new verified original package
        replaced the previous one). Clear the terminal trigger latch so the replan
        controller treats the next active risk condition as a fresh generation
        rather than staying latched on a generation that belonged to the PREVIOUS
        mission. Refused while a transaction is running (never disturb a live
        transaction's generation binding)."""
        if self.is_running():
            return
        with self._state_lock:
            self._rearm_trigger_locked(reason)

    # ── Transaction ───────────────────────────────────────────────────────────
    def run_transaction(self, snapshot) -> Dict[str, Any]:
        """Execute the full FSM to a terminal state. Acquires the one-action lock
        non-blocking; a concurrent/duplicate call is suppressed (returns started
        False) rather than running a second transaction."""
        if not self._action_lock.acquire(blocking=False):
            return {"started": False, "reason": "duplicate suppressed -- transaction in progress"}
        try:
            return self._run(snapshot)
        finally:
            self._action_lock.release()

    def _run(self, snapshot) -> Dict[str, Any]:
        transition_id = uuid.uuid4().hex
        package = planning_package.load()
        self._simulated_run = bool(self.cfg.dry_run)

        with self._state_lock:
            self._active_transition_id = transition_id
            # Bind this transaction to the current trigger generation so its
            # terminal outcome consumes exactly that generation (task section 1).
            self._active_generation = self._trigger_generation
            # Bind hold-only-ness atomically with the generation above, captured
            # from the SAME inactive->active edge (E2 water-trial task) -- immune
            # to a later observe() call on the main thread reclassifying an
            # already-active generation while this transaction is in flight.
            self._active_hold_only = self._generation_hold_only
            self._snapshot_id = snapshot.snapshot_id
            self._revision_number += 1
            self._retry_count = 0
            self._last_error = None
            self._proven_mission_id = None
            self._last_original_proof = None
            self._last_revised_progression = None
            revision = mission_revision.MissionRevision(
                mission_id=snapshot.mission_id,
                parent_revision=snapshot.mission_revision,
                new_revision=self._revision_number,
                decision_snapshot_id=snapshot.snapshot_id,
                transition_id=transition_id,
                reason_codes=list(self._last_reason_codes),
                original_route_hash=(package or {}).get("original_route_hash"),
            )
            self._current_revision = revision

        # (1) Authority before ANYTHING -- fail closed with the vehicle untouched.
        if not self._authorized():
            return self._suspend("Control authority is not LOCAL_AGENT before commanding LOITER.")

        # (2) Fresh ORIGINAL-mission proof BEFORE the first vehicle-changing write
        # (CRITICAL ISSUE 2). If the stored package is not provably the mission
        # physically on the Pixhawk RIGHT NOW, do NOT touch the vehicle: no LOITER,
        # no upload, no AUTO. dry-run simulates the whole lifecycle, so the
        # physical-Pixhawk proof is not meaningful there and is skipped.
        if not self.cfg.dry_run:
            proof = self._prove_original_mission(package, revision)
            if not proof["ok"]:
                return self._fail(proof["code"], proof["message"], detail=proof.get("detail"))
        else:
            with self._state_lock:
                self._proven_mission_id = snapshot.mission_id or (package or {}).get("mission_id")

        # (3) LOITER, verified.
        self._transition(HOLD_REQUESTED,
                         "Original mission freshly proven -- requesting LOITER hold.")
        loiter = self._do_loiter()
        if not self._verified(loiter):
            return self._fail("LOITER_NOT_VERIFIED",
                              "Could not confirm LOITER; vehicle mode left unchanged.",
                              detail=loiter)
        self._transition(HOLD_CONFIRMED, "LOITER confirmed -- vehicle is holding position.")

        with self._state_lock:
            hold_only = self._active_hold_only

        # (3a) HOLD-SETTLE proof (E2 replan armed-LOITER upload race fix; P0
        # thesis-freeze fix: hold-only SAFE_HOLD authority-scoping). HOLD_CONFIRMED
        # above only proves MODE HOLD CONFIRMED (mode == LOITER, server-verified
        # over a stability window) -- it does NOT prove PHYSICAL HOLD SETTLED
        # (armed AND fresh groundspeed at/below the SAME threshold the upload
        # endpoint's own armed-LOITER exception requires). A bench/water-trial run
        # showed the first upload attempted immediately after HOLD_CONFIRMED
        # racing a still-decelerating vehicle (observed 0.63 m/s against the
        # endpoint's 0.5 m/s bound), burning all max_transaction_retries in
        # ~70ms -- long before the boat could possibly have stopped. This is a
        # ONE-TIME bounded wait, run here for EVERY HOLD_CONFIRMED (both the
        # HOLD-only request below and the plan/validate/upload path further
        # down), so no caller can ever reach a SAFE_HOLD/terminal outcome on
        # mode confirmation alone -- a transition to SAFE_HOLD must mean a
        # commanded LOITER has been PROVEN, not merely requested. It never
        # itself consumes a transaction retry. See _acquire_hold_settle's own
        # docstring.
        settle = self._acquire_hold_settle()
        if not settle["ok"] and settle.get("suspend"):
            return self._suspend(settle["message"])

        # (3b) A HOLD-only action request (decision_policy.py: safe return could
        # not be established) never attempts PLANNING/VALIDATING/UPLOAD at all --
        # the authoritative decision policy already determined a return route
        # cannot be justified, so the transaction ends here. INVARIANT (P0
        # thesis-freeze fix): SAFE_HOLD means the hold has been POSITIVELY
        # PROVEN -- verified LOITER mode AND fresh groundspeed at/below
        # threshold AND the required persistence -- never merely requested or
        # mode-confirmed. If HOLD-SETTLE never proved within its bound, this is
        # NOT a SAFE_HOLD: fail closed to SUSPENDED instead (vehicle stays in
        # verified-mode LOITER either way; a plan/validate/upload attempt was
        # never even spent, and -- same as the proven case just below -- a
        # HOLD-only request NEVER attempts the RTL-fallback hierarchy, timeout
        # or not).
        if hold_only:
            if not settle["ok"]:
                return self._hold_not_proven(
                    revision, "HOLD_SETTLE_TIMEOUT",
                    "Authoritative decision policy requested HOLD (safe return not "
                    f"established); {settle['message']}")
            return self._direct_safe_hold(
                revision,
                "Authoritative decision policy requested HOLD (safe return not "
                "established) -- holding in verified LOITER; no return route "
                "attempted.")

        if not settle["ok"]:
            # Never settled within the bound: fail closed through the EXISTING
            # fallback hierarchy (RTL if currently proven feasible and enabled,
            # else SAFE_HOLD) -- the vehicle is already safely holding in
            # verified LOITER either way; a plan/validate/upload attempt was
            # never even spent chasing a physically-unsettled vehicle.
            return self._fallback(snapshot, revision, settle["message"])

        # (3) plan -> validate -> upload -> verify -> resume, with bounded retries.
        last_failure = None
        max_retries = max(0, self.cfg.max_transaction_retries)
        for attempt in range(max_retries + 1):
            with self._state_lock:
                self._retry_count = attempt
            outcome, info = self._attempt_return(snapshot, package, revision)
            if outcome == _SUCCESS:
                return self._succeed(revision)
            if outcome == _SUSPEND:
                return self._suspend(info)
            last_failure = info
            self._emit("replan_attempt_failed",
                       f"Replan attempt {attempt + 1}/{max_retries + 1} failed: {info}",
                       "warning")

        # (4) retries exhausted -> fallback / safe hold.
        return self._fallback(snapshot, revision, last_failure)

    def _attempt_return(self, snapshot, package, revision) -> "tuple[str, str]":
        # PLANNING
        if not self._authorized():
            return _SUSPEND, "Authority lost before PLANNING."
        # Defense in depth: re-check package/mission consistency at plan time in
        # case the package changed between observe() and here. Fail closed --
        # never replan from a package belonging to a different mission.
        consistency, _detail = planning_package.check_consistency(package, snapshot.mission_id)
        if consistency != planning_package.CONSISTENCY_OK:
            self._set_error(consistency, f"planning package inconsistent: {consistency}", consistency)
            return _FAIL_STEP, f"planning package inconsistent ({consistency})"
        self._transition(PLANNING, "Building safe-return route from approved geometry.")
        build = safe_return_planner.build_safe_return_route(snapshot, package, self.cfg)
        if not build["ok"]:
            self._set_error("PLANNING_FAILED", build["reason"], build["reason_code"])
            return _FAIL_STEP, f"planning failed ({build['reason_code']})"
        revision.preserved_waypoint_count = build["preserved_waypoint_count"]
        revision.removed_waypoint_count = build["removed_waypoint_count"]
        revision.inserted_waypoint_count = build["inserted_waypoint_count"]
        revision.revised_waypoint_count = len(build["route"])
        revision.revised_route_count = len(build["route"])
        revision.revised_route_hash = route_hash.route_content_hash(build["route"])
        # Shortest-safe-return planner evidence (which strategy actually won --
        # never silently label a retrace fallback "shortest").
        revision.planner_strategy = build.get("method")
        revision.planner_route_distance_m = round(
            geo.path_length_m([(wp["latitude"], wp["longitude"]) for wp in build["route"]]), 1)
        revision.planner_direct_path_valid = build.get("direct_path_valid")
        revision.planner_candidate_node_count = build.get("candidate_node_count")
        revision.planner_fallback_used = build.get("fallback_used")
        revision.planner_runtime_s = build.get("planner_runtime_s")

        # VALIDATING
        self._transition(VALIDATING, "Validating revised route (contract + no-go).")
        validation = safe_return_planner.validate_route(build["route"], package, snapshot, self.cfg)
        revision.validation_result = validation
        with self._state_lock:
            self._last_geometry_validation = validation.get("geometry_validation")
        if not validation["valid"]:
            self._set_error("VALIDATION_FAILED", validation["reason"], validation["reason_code"])
            return _FAIL_STEP, f"validation failed ({validation['reason_code']})"

        # ACTUAL REVISED-ROUTE ENERGY RECHECK -- AFTER geometry validation
        # succeeds, BEFORE upload. The energy trigger that started this
        # transaction only proves an INITIAL/direct return looked viable
        # (mission_feasibility's straight-line rtl_return_feasible, exposed via
        # feasibility_fn); RETRACE_APPROVED retraces already-approved waypoints
        # and may be substantially longer than that direct estimate. A
        # constrained safe-return route IS a RETURN-HOME action, so it is
        # re-checked with the SAME energy model (mission_feasibility.
        # evaluate_route_return_energy, reusing _dimension_capacity -- no new
        # formula) against the SAME emergency-return reserve
        # (rtl_reserve_fraction) the RTL dimension already uses -- never the
        # ongoing-mission reserve. FEASIBLE continues to upload; INFEASIBLE or
        # UNKNOWN never uploads and falls into the existing retry/fallback
        # path below (constrained retrace -> native RTL if currently proven
        # feasible -> SAFE_HOLD), exactly like any other _FAIL_STEP.
        revised_route_distance_m = round(
            geo.path_length_m([(wp["latitude"], wp["longitude"]) for wp in build["route"]]), 1)
        injected_battery_percent = (
            (getattr(snapshot, "active_experiment_overrides", None) or {}).get("battery_percent"))
        energy_check = mission_feasibility.evaluate_route_return_energy(
            distance_m=revised_route_distance_m,
            physical_battery_percent=getattr(snapshot, "battery_percent", None),
            injected_battery_percent=injected_battery_percent,
            nominal_capacity_Ah=self.cfg.nominal_capacity_Ah,
            conservative_current_A=self.cfg.conservative_current_A,
            design_speed_mps=self.cfg.design_speed_mps,
            usable_capacity_factor=self.cfg.usable_capacity_factor,
            reserve_fraction=self.cfg.rtl_reserve_fraction,
        )
        # Evidence: distinguishes the INITIAL/direct return estimate (the
        # trigger's own current rtl_return_feasible, via feasibility_fn) from
        # the ACTUAL constrained revised-route feasibility computed just above
        # -- both recorded on the SAME validation_result the recorder already
        # persists (MissionRevision.validation_result), no schema redesign.
        validation.setdefault("checks", {}).update({
            "initial_return_rtl_feasible": self._current_rtl_feasible(),
            "revised_route_distance_m": energy_check.distance_m,
            "revised_route_energy_status": energy_check.status,
            "revised_route_required_ah": energy_check.required_capacity_Ah,
            "revised_route_available_ah": energy_check.available_capacity_Ah,
            "revised_route_reserve_ah": energy_check.reserve_capacity_Ah,
            "revised_route_margin_ah": energy_check.margin_Ah,
            "revised_route_margin_percent": energy_check.margin_percent,
        })
        if energy_check.feasible is not True:
            self._set_error("REVISED_ROUTE_ENERGY_NOT_PROVEN_FEASIBLE", energy_check.message,
                            energy_check.reason, detail=energy_check.to_dict())
            return _FAIL_STEP, f"actual revised-route energy not feasible ({energy_check.status})"

        # UPLOAD
        if not self._authorized():
            return _SUSPEND, "Authority lost before UPLOAD."
        self._transition(UPLOAD_REQUESTED, "Uploading revised safe-return mission.")
        upload = self._do_upload(build["route"])
        revision.upload_operation_result = _summarize_upload(upload)
        if not (upload.get("accepted") and upload.get("uploaded")):
            self._set_error("UPLOAD_FAILED",
                            (upload.get("error") or {}).get("message", "upload not accepted/uploaded")
                            if isinstance(upload.get("error"), dict) else "upload not accepted/uploaded",
                            "UPLOAD_FAILED")
            return _FAIL_STEP, "upload failed"

        # VERIFYING (the upload service already did the fresh readback). Record the
        # REVISED mission identity so later monitoring compares against the REVISED
        # hash/count -- never the original, which the revised route no longer has.
        self._transition(VERIFYING_REVISION, "Verifying revised mission via fresh readback.")
        revision.readback_verification_result = _summarize_verification(upload)
        if not upload.get("verified"):
            self._set_error("READBACK_MISMATCH",
                            "Upload acked but fresh readback did not verify.", "READBACK_MISMATCH")
            return _FAIL_STEP, "readback mismatch"
        revision.revised_route_count = (upload.get("observed_route_waypoint_count")
                                        or revision.revised_route_count)
        revision.revised_proof = {
            "verified": upload.get("verified"),
            "revised_route_hash": (upload.get("observed_route_content_hash")
                                   or revision.revised_route_hash),
            "expected_route_hash": upload.get("expected_route_content_hash"),
            "revised_route_count": upload.get("observed_route_waypoint_count"),
            "proof_timestamp": round(self._clock(), 3),
        }

        # RESUME (AUTO requires verified Home)
        if not self._authorized():
            return _SUSPEND, "Authority lost before RESUME."
        if not self._home_verified():
            self._set_error("HOME_UNVERIFIED",
                            "AUTO resume requires a verified Home; none is verified.", "HOME_UNVERIFIED")
            self._ensure_loiter()
            return _FAIL_STEP, "home unverified"
        self._transition(RESUME_REQUESTED, "Resuming AUTO on the revised mission.")

        # Capture the pre-AUTO progression baseline immediately before AUTO, so the
        # shared verifier measures sequence/movement against a fixed reference.
        baseline = self._capture_baseline()
        auto = self._do_auto()
        if not self._verified(auto):
            self._set_error("RESUME_FAILED", "Could not confirm AUTO; restoring LOITER.", "RESUME_FAILED")
            self._ensure_loiter()
            return _FAIL_STEP, "AUTO not verified"

        # Confirm progression via the SHARED verifier over the REVISED route
        # (CRITICAL ISSUE 1): poll to the full configured deadline, proving
        # progression positively (ACTIVE_TRUE / sequence advance / movement toward
        # target) -- never fail on a single inactive/UNKNOWN sample. RETURNING_HOME
        # is only reported after this returns proven.
        watch = self._watch_progression(baseline, build["route"],
                                        self.cfg.revised_progression_timeout_s)
        summary = _summarize_progression(watch)
        revision.revised_progression = summary
        with self._state_lock:
            self._last_revised_progression = summary
        if not watch.get("proven"):
            code = watch.get("failure_code") or "PROGRESSION_UNCONFIRMED"
            self._set_error(code,
                            watch.get("failure_message")
                            or "revised mission progression not confirmed; restoring LOITER",
                            code, detail=summary)
            self._ensure_loiter()
            return _FAIL_STEP, "progression unconfirmed"
        return _SUCCESS, "revised mission running"

    # ── Terminal transitions ──────────────────────────────────────────────────
    def _succeed(self, revision) -> Dict[str, Any]:
        self._finalize_revision(revision)
        self._transition(MONITORING_REVISED,
                         "Revised safe-return mission is running under AUTO.", terminal=True)
        self._emit("replan_completed", "Safe-return replan completed; revised mission running.", "info")
        return {"started": True, "outcome": MONITORING_REVISED, "revision": self._last_revision_dict}

    def _suspend(self, reason: str) -> Dict[str, Any]:
        self._set_error("AUTHORITY_LOST", reason, "AUTHORITY_LOST")
        self._finalize_revision(self._current_revision)
        self._transition(SUSPENDED, reason, terminal=True)
        self._emit("replan_suspended", f"Replan suspended: {reason}", "warning")
        return {"started": True, "outcome": SUSPENDED, "reason": reason}

    def _fail(self, code: str, reason: str, detail=None) -> Dict[str, Any]:
        self._set_error(code, reason, code, detail=detail)
        self._finalize_revision(self._current_revision)
        self._transition(FAILED, reason, terminal=True)
        self._emit("replan_failed", f"Replan failed: {reason}", "warning")
        return {"started": True, "outcome": FAILED, "reason": reason}

    def _hold_not_proven(self, revision, code: str, reason: str) -> Dict[str, Any]:
        """Terminal path for a HOLD-only transaction that could NOT positively
        prove its physical hold (P0 thesis-freeze invariant fix: a transition
        to SAFE_HOLD must mean a commanded LOITER has been PROVEN -- verified
        mode AND fresh groundspeed at/below threshold AND the required
        persistence -- never merely requested or mode-confirmed). Used both
        when _acquire_hold_settle() times out without ever proving settled,
        and when the final defensive _ensure_loiter() re-assertion in
        _direct_safe_hold() itself fails to verify.

        Fails closed to SUSPENDED -- the same "no further writes, vehicle left
        exactly where it already safely was, never auto-resumed" contract
        _suspend() already uses for authority loss -- but with its OWN
        explicit code/reason (HOLD_SETTLE_TIMEOUT / LOITER_REASSERT_NOT_VERIFIED),
        never the AUTHORITY_LOST code _suspend() hardcodes, which would
        misrepresent why this transaction actually stopped. NEVER attempts
        PLANNING/VALIDATING/UPLOAD/AUTO/RTL -- identical in that respect to
        _direct_safe_hold(), just without the positive SAFE_HOLD claim."""
        self._set_error(code, reason, code)
        self._finalize_revision(revision)
        self._transition(SUSPENDED, reason, terminal=True)
        self._emit("replan_suspended", f"Replan suspended: {reason}", "warning")
        return {"started": True, "outcome": SUSPENDED, "reason": reason}

    def _current_rtl_feasible(self) -> Optional[bool]:
        """Fresh (this-call) rtl_return_feasible, via feasibility_fn (E2
        water-trial integration task section 15: "no blind RTL" -- proven
        current feasibility, not merely a verified Home, is required before
        ever commanding RTL). Fails closed to None (treated as UNKNOWN, never
        as True) on any missing callback, malformed value, or exception."""
        if self._feasibility_fn is None:
            return None
        try:
            evidence = self._feasibility_fn()
        except Exception:
            return None
        if not isinstance(evidence, dict):
            return None
        value = evidence.get("rtl_return_feasible")
        return value if isinstance(value, bool) else None

    def _fallback(self, snapshot, revision, last_failure) -> Dict[str, Any]:
        rtl_feasible = self._current_rtl_feasible()
        rtl_ok = (
            self.cfg.rtl_fallback_enabled
            and self._authorized()
            and self._home_verified()
            # Verified Home alone is NOT sufficient -- a verified Home only
            # proves WHERE RTL would go, not that the vehicle can currently
            # get there. rtl_feasible is False (proven infeasible) or None
            # (unknown/unproven) must both fail closed to SAFE_HOLD, never a
            # blind RTL (task section 6/15).
            and rtl_feasible is True
        )
        if rtl_ok:
            self._transition(FALLBACK_RTL, "Retries exhausted -- commanding verified RTL fallback.")
            rtl = self._do_rtl()
            self._finalize_revision(revision)
            if self._verified(rtl):
                self._mark_terminal(FALLBACK_RTL)
                self._emit("replan_fallback_rtl",
                           "Safe-return retries exhausted; verified RTL fallback engaged.", "warning")
                return {"started": True, "outcome": FALLBACK_RTL, "reason": last_failure}
            # RTL itself failed -> restore LOITER and hold.
            self._ensure_loiter()
            self._set_error("RTL_FALLBACK_FAILED", "RTL fallback not verified; holding in LOITER.",
                            "RTL_FALLBACK_FAILED")
            self._transition(SAFE_HOLD, "RTL fallback failed -- holding in LOITER.", terminal=True)
            self._emit("replan_safe_hold", "RTL fallback failed; holding in LOITER.", "warning")
            return {"started": True, "outcome": SAFE_HOLD, "reason": last_failure}

        # No fallback -> guarantee a safe hold in LOITER. Record WHY (task
        # section 6: "expose urgent operator-visible reason") without
        # changing the terminal outcome/reason contract other callers key on.
        if self.cfg.rtl_fallback_enabled and rtl_feasible is not True:
            self._set_error(
                "RTL_FALLBACK_INFEASIBLE",
                f"RTL fallback not attempted -- current rtl_return_feasible is "
                f"{rtl_feasible!r}, not True; holding in LOITER instead of a blind RTL.",
                "RTL_FALLBACK_INFEASIBLE",
            )
        self._ensure_loiter()
        self._finalize_revision(revision)
        self._transition(SAFE_HOLD, "Retries exhausted -- holding in LOITER (no RTL fallback).",
                         terminal=True)
        self._emit("replan_safe_hold",
                   f"Safe-return retries exhausted; holding in LOITER. Last failure: {last_failure}",
                   "warning")
        return {"started": True, "outcome": SAFE_HOLD, "reason": last_failure}

    def _direct_safe_hold(self, revision, reason: str) -> Dict[str, Any]:
        """Terminal path for a HOLD-only action request whose physical
        HOLD-SETTLE proof already succeeded (E2 water-trial task; P0
        thesis-freeze fix: hold-only SAFE_HOLD authority-scoping). ONLY called
        once the caller has confirmed mode LOITER AND run it through the SAME
        _acquire_hold_settle() physical-settle proof the energy-return path
        uses (settle["ok"] is True) -- a timed-out settle never reaches here,
        see _hold_not_proven(). Re-asserts LOITER defensively immediately
        before certifying the hold: SAFE_HOLD means POSITIVELY PROVEN, so if
        even that last, already-expected-to-succeed re-assertion fails to
        verify, this must NOT claim SAFE_HOLD either -- it fails closed
        through the SAME _hold_not_proven() path instead. No
        PLANNING/VALIDATING/UPLOAD/RTL is ever attempted for a HOLD-only
        request, proven or not."""
        if not self._ensure_loiter():
            return self._hold_not_proven(
                revision, "LOITER_REASSERT_NOT_VERIFIED",
                "HOLD-SETTLE was proven, but the final defensive LOITER "
                "re-assertion could not be verified immediately before "
                "certifying the hold; failing closed instead of certifying "
                "an unproven hold.")
        self._finalize_revision(revision)
        self._transition(SAFE_HOLD, reason, terminal=True)
        self._emit("replan_safe_hold", reason, "warning")
        return {"started": True, "outcome": SAFE_HOLD, "reason": reason}

    # ── Vehicle-op wrappers (dry-run substitutes a flagged simulated success) ──
    def _authorized(self) -> bool:
        """Fresh authority read + autonomy_gate check, immediately before a write.
        Fails closed on any read error. Recorded for status."""
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

    def _do_loiter(self):
        if self.cfg.dry_run:
            return _simulated_mode_result(5, "LOITER")
        return self.gateway.command_loiter()

    def _do_auto(self):
        if self.cfg.dry_run:
            return _simulated_mode_result(10, "AUTO")
        return self.gateway.command_auto()

    def _do_rtl(self):
        if self.cfg.dry_run:
            return _simulated_mode_result(11, "RTL")
        return self.gateway.command_rtl()

    def _do_upload(self, route):
        if self.cfg.dry_run:
            h = route_hash.route_content_hash(route)
            return {"accepted": True, "uploaded": True, "verified": True,
                    "expected_route_content_hash": h, "observed_route_content_hash": h,
                    "observed_route_waypoint_count": len(route), "simulated": True, "dry_run": True}
        return self.gateway.upload_mission(route, self._active_transition_id, "AGENT_REPLAN")

    def _home_verified(self) -> bool:
        if self.cfg.dry_run:
            return True
        return self.gateway.home_verified()

    # ── HOLD-SETTLE proof (E2 replan armed-LOITER upload race fix) ────────────
    def _acquire_hold_settle(self) -> Dict[str, Any]:
        """Bounded proof that the armed vehicle has PHYSICALLY settled in the
        just-confirmed LOITER -- not merely that the Pixhawk reports mode ==
        LOITER (HOLD_CONFIRMED, above, already proves that much and no more).

        Polls gateway.upload_preconditions() -- the upload endpoint's OWN
        read-only dry-run precondition check (services/mission_upload_service.
        check_upload_preconditions/_evaluate_preconditions) -- for up to
        cfg.replan_hold_settle_timeout_s. This is deliberately the SAME check
        the real upload will make, not a second local copy of the groundspeed
        threshold, the freshness bounds, or the armed/mode logic: "settled"
        here means exactly, and only, "the real upload would be ALLOWED right
        now". Unknown/stale/unavailable groundspeed is never treated as
        stopped -- that check already fails closed
        (ARMED_LOITER_GROUNDSPEED_UNAVAILABLE / ARMED_LOITER_STALE_GROUNDSPEED),
        so "allowed" here already carries that same fail-closed guarantee.

        Requires the check to report ALLOWED for a continuous
        cfg.replan_hold_settle_persistence_s (any single not-allowed sample
        resets that window) before declaring HOLD-SETTLE confirmed -- a lone
        flickering sample right at the threshold must never alone authorize
        the upload. Authority is re-checked every poll, exactly like every
        other write-adjacent step; an authority loss here suspends the
        transaction rather than continuing to poll. A no-op (always ok) in
        dry_run, which never performs the underlying HTTP check.

        Returns {"ok": True} once settled, or {"ok": False, "message": ...,
        "suspend": bool} on authority loss (suspend=True) or on timing out
        without ever proving settled (suspend=False -- the caller falls back
        through the existing RTL/SAFE_HOLD hierarchy; the vehicle is simply
        left exactly where it already safely was, in verified LOITER)."""
        if self.cfg.dry_run:
            return {"ok": True}
        start = self._clock()
        deadline = start + self.cfg.replan_hold_settle_timeout_s
        settled_since: Optional[float] = None
        samples: List[Dict[str, Any]] = []
        attempts = 0
        self._record_hold_settle_event("HOLD_SETTLE_WAIT_STARTED", {
            "timeout_s": self.cfg.replan_hold_settle_timeout_s,
            "poll_interval_s": self.cfg.replan_hold_settle_poll_interval_s,
            "persistence_s": self.cfg.replan_hold_settle_persistence_s,
        })
        while True:
            if not self._authorized():
                return {"ok": False, "suspend": True,
                        "message": "Authority lost during HOLD-SETTLE wait (before revised-mission upload)."}
            attempts += 1
            now = self._clock()
            try:
                check = self.gateway.upload_preconditions("AGENT_REPLAN")
            except Exception as e:
                check = {"allowed": False, "error_code": "PRECONDITION_CHECK_UNAVAILABLE",
                         "error_message": str(e), "preconditions": {}}
            allowed = bool(isinstance(check, dict) and check.get("allowed"))
            pre = (check.get("preconditions") if isinstance(check, dict) else None) or {}
            sample = {
                "elapsed_s": round(now - start, 3),
                "mode": pre.get("verified_mode"),
                "armed": pre.get("armed"),
                "groundspeed": pre.get("groundspeed_m_s"),
                "groundspeed_age_s": pre.get("groundspeed_age_s"),
                "threshold_mps": check.get("armed_loiter_max_groundspeed_m_s") if isinstance(check, dict) else None,
                "allowed": allowed,
                "error_code": check.get("error_code") if isinstance(check, dict) else None,
            }
            samples.append(sample)
            with self._state_lock:
                self._last_hold_settle = {
                    "confirmed": None, "attempts": attempts, "elapsed_s": sample["elapsed_s"],
                    "samples": samples[-20:],
                }
            if allowed:
                if settled_since is None:
                    settled_since = now
                if now - settled_since >= self.cfg.replan_hold_settle_persistence_s:
                    with self._state_lock:
                        self._last_hold_settle["confirmed"] = True
                    self._record_hold_settle_event("HOLD_SETTLE_CONFIRMED", {
                        "attempts": attempts, "elapsed_s": sample["elapsed_s"], "samples": samples[-20:],
                    })
                    return {"ok": True}
            else:
                settled_since = None
            if now >= deadline:
                with self._state_lock:
                    self._last_hold_settle["confirmed"] = False
                self._record_hold_settle_event("HOLD_SETTLE_TIMEOUT", {
                    "attempts": attempts, "elapsed_s": sample["elapsed_s"], "samples": samples[-20:],
                    "last_error_code": sample["error_code"],
                }, priority="high")
                return {"ok": False, "suspend": False,
                        "message": (
                            f"HOLD-SETTLE not proven within {self.cfg.replan_hold_settle_timeout_s}s "
                            f"(last observed groundspeed {sample['groundspeed']} m/s vs threshold "
                            f"{sample['threshold_mps']} m/s, last reason {sample['error_code']}); "
                            "vehicle remains in verified LOITER."
                        )}
            self._sleep(self.cfg.replan_hold_settle_poll_interval_s)

    def _record_hold_settle_event(self, event_type: str, data: Dict[str, Any],
                                  priority: str = "normal") -> None:
        """Best-effort recorder evidence for the HOLD-SETTLE wait (thesis
        evidence: proves the safety hold was commanded, physical motion
        decayed, and only THEN was the autonomous mission rewrite attempted).
        Never raises; a missing/disabled recorder is the normal case outside
        an experiment run."""
        if self._recorder is None:
            return
        try:
            self._recorder.record_event(event_type, source="replan_controller", data=data, priority=priority)
        except Exception:
            pass

    # ── Fresh pre-replan ORIGINAL-mission proof (CRITICAL ISSUE 2) ────────────
    def _bound_original_mission(self) -> Optional[Dict[str, Any]]:
        """The mission-execution controller's bound ORIGINAL mission identity, or
        None. Fails closed (None) if the callback is missing or raises."""
        if self._original_mission_fn is None:
            return None
        try:
            return self._original_mission_fn()
        except Exception:
            return None

    def _prove_pixhawk_readback(self):
        """A FRESH, proof-grade Pixhawk mission readback. Prefers the gateway's
        prove_pixhawk_mission_readback() (requests a refresh and waits for the
        coordinator's refresh generation to advance) so a cache-first read cannot
        let a stale readback satisfy the proof; falls back to
        pixhawk_mission_readback() for gateways that do not implement it (its
        result is still subject to the freshness gate)."""
        prove = getattr(self.gateway, "prove_pixhawk_mission_readback", None)
        return prove() if callable(prove) else self.gateway.pixhawk_mission_readback()

    def _prove_original_mission(self, package, revision) -> Dict[str, Any]:
        """Prove, FRESH and before any vehicle write, that the stored planning
        package is the SAME mission physically on the Pixhawk right now. Requires:
          - a usable package with a mission_id and an original route hash;
          - the mission-execution controller's BOUND original mission_id (never
            vehicle_state.current_mission_id, which is null on this path) equal to
            the package mission_id (and, when present, its route hash);
          - a fresh, reachable, complete, non-stale Pixhawk readback whose route
            content hash AND waypoint count match the package's.
        Returns {ok: True} (with the proof recorded on `revision` and the proven
        identity bound), or {ok: False, code, message, detail} with a specific
        ORIGINAL_MISSION_* fail-closed code. On failure NOTHING is written."""
        # (1) Package identity present + usable.
        if not planning_package.is_usable(package):
            return self._proof_fail(ORIGINAL_MISSION_PROOF_UNAVAILABLE,
                                    "no usable stored planning package to prove the original mission")
        pkg_mid = package.get("mission_id")
        pkg_hash = package.get("original_route_hash") or package.get("route_hash")
        pkg_count = len(package.get("route") or [])
        if not pkg_mid:
            return self._proof_fail(ORIGINAL_MISSION_ID_MISMATCH,
                                    "stored planning package has no mission_id")
        if not pkg_hash:
            return self._proof_fail(ORIGINAL_MISSION_HASH_MISMATCH,
                                    "stored planning package has no original route hash")

        # (2) BOUND original mission identity from the mission-execution controller.
        bound = self._bound_original_mission()
        if not bound or not bound.get("mission_id"):
            return self._proof_fail(
                ORIGINAL_MISSION_ID_MISMATCH,
                "no bound original mission identity from mission execution; cannot prove "
                "what is on the vehicle")
        if bound["mission_id"] != pkg_mid:
            return self._proof_fail(
                ORIGINAL_MISSION_ID_MISMATCH,
                f"planning package mission_id {pkg_mid!r} != bound original mission_id "
                f"{bound['mission_id']!r}",
                detail={"package_mission_id": pkg_mid, "bound_mission_id": bound["mission_id"]})
        if bound.get("original_route_hash") and bound["original_route_hash"] != pkg_hash:
            return self._proof_fail(
                ORIGINAL_MISSION_HASH_MISMATCH,
                "planning package original route hash != bound original route hash",
                detail={"package_route_hash": pkg_hash,
                        "bound_route_hash": bound["original_route_hash"]})

        # (3) Fresh live Pixhawk readback -- reachable, complete, fresh, matching.
        try:
            readback = self._prove_pixhawk_readback()
        except Exception as e:
            return self._proof_fail(ORIGINAL_MISSION_PROOF_UNAVAILABLE,
                                    f"could not read Pixhawk mission: {e}")
        if not isinstance(readback, dict) or readback.get("reachable") is False:
            return self._proof_fail(
                ORIGINAL_MISSION_PROOF_UNAVAILABLE,
                f"Pixhawk mission readback unreachable: {(readback or {}).get('error')}")
        if readback.get("partial"):
            return self._proof_fail(ORIGINAL_MISSION_PROOF_UNAVAILABLE,
                                    "Pixhawk mission readback is partial")
        fresh, reason = planning_package.readback_is_fresh(readback)
        if not fresh:
            return self._proof_fail(
                ORIGINAL_MISSION_PROOF_UNAVAILABLE,
                f"Pixhawk mission readback is not fresh enough to prove the original "
                f"mission: {reason}", detail={"freshness": reason})
        rb_hash = readback.get("route_content_hash")
        rb_count = readback.get("route_waypoint_count")
        if not rb_count or rb_count <= 0:
            return self._proof_fail(ORIGINAL_MISSION_COUNT_MISMATCH,
                                    "Pixhawk route waypoint count is zero")
        if pkg_hash != rb_hash:
            return self._proof_fail(
                ORIGINAL_MISSION_HASH_MISMATCH,
                f"package original route hash {pkg_hash} != fresh Pixhawk route hash {rb_hash}",
                detail={"package_route_hash": pkg_hash, "pixhawk_route_content_hash": rb_hash})
        if pkg_count != rb_count:
            return self._proof_fail(
                ORIGINAL_MISSION_COUNT_MISMATCH,
                f"package original route count {pkg_count} != fresh Pixhawk route count {rb_count}",
                detail={"package_route_count": pkg_count, "pixhawk_route_count": rb_count})

        # Proven. Bind the identity for the shared progression verifier and record
        # the proof for audit / thesis evidence.
        proof_record = {
            "proven": True,
            "mission_id": pkg_mid,
            "original_route_hash": pkg_hash,
            "original_route_count": pkg_count,
            "pixhawk_route_content_hash": rb_hash,
            "pixhawk_route_count": rb_count,
            "proof_source": readback.get("proof_source"),
            "readback_age_s": readback.get("age_s"),
            "proof_timestamp": round(self._clock(), 3),
        }
        revision.original_route_hash = pkg_hash
        revision.original_route_count = pkg_count
        revision.original_proof = proof_record
        with self._state_lock:
            self._proven_mission_id = pkg_mid
            self._last_original_proof = proof_record
        return {"ok": True}

    def _proof_fail(self, code: str, message: str, detail=None) -> Dict[str, Any]:
        record = {"proven": False, "code": code, "message": message, "detail": detail,
                  "proof_timestamp": round(self._clock(), 3)}
        with self._state_lock:
            self._last_original_proof = record
        return {"ok": False, "code": code, "message": message, "detail": detail}

    # ── Shared progression verification (CRITICAL ISSUE 1) ────────────────────
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

    def _read_snapshot_safe(self):
        """Read fresh vehicle state and build a snapshot; None on any read error
        (the shared verifier treats an unreadable sample as UNKNOWN, retried
        within the deadline, never as a value)."""
        try:
            return self._build_snapshot(self.gateway.read_vehicle_state())
        except Exception:
            return None

    def _progression_context(self, route) -> mission_progression.ProgressionContext:
        return mission_progression.ProgressionContext(
            read_snapshot=self._read_snapshot_safe,
            target_for_sequence=mission_progression.route_target_lookup(route or []),
            expected_mission_id=self._proven_mission_id,
            poll_interval_s=self.cfg.progression_poll_interval_s,
            min_displacement_m=self.cfg.progression_min_displacement_m,
            max_position_age_s=self.cfg.max_position_age_s,
            clock=self._clock,
            sleep=self._sleep,
        )

    def _capture_baseline(self) -> Dict[str, Any]:
        if self.cfg.dry_run:
            return {"simulated": True, "baseline_sequence": None,
                    "baseline_position": {"latitude": None, "longitude": None}}
        return mission_progression.capture_baseline(self._progression_context(None))

    def _watch_progression(self, baseline, route, timeout_s: float) -> Dict[str, Any]:
        if self.cfg.dry_run:
            return {"proven": True, "proof": "SIMULATED", "simulated": True,
                    "configured_timeout_s": timeout_s, "actual_elapsed_s": 0.0,
                    "sample_count": 0, "baseline": baseline, "samples": [],
                    "final_mode": "AUTO", "final_armed": True}
        return mission_progression.watch(self._progression_context(route), baseline, timeout_s)

    def _ensure_loiter(self) -> bool:
        """Re-assert LOITER so a post-resume failure leaves a confirmed safe hold.
        Best-effort: a failure here is recorded but does not raise.

        SAFETY-EXEMPT LOITER (P0-2): this is the ONE vehicle write in this
        controller that does NOT call _authorized() first. It is deliberately
        narrow -- LOITER only, never AUTO/upload/RTL -- and is only reached
        from a small, explicit set of fail-closed/terminal paths (a failed
        RESUME/HOME_UNVERIFIED/progression-unconfirmed step, the RTL-fallback
        failure hold, the no-fallback safe hold, and the direct HOLD-only
        action request). It exists so a vehicle already committed to a
        return is left in a physically meaningful hold rather than an
        undefined mode merely because authority changed hands or an earlier
        step failed; it never commands AUTO/upload/RTL. Every non-LOITER
        write in this controller (upload/AUTO/RTL) calls _authorized()
        immediately before itself, with no exception -- see _authorized()'s
        callers. A _ensure_loiter() call can never lead to a subsequent
        AUTO/upload without a fresh authority check: every path that follows
        one is terminal (SAFE_HOLD/FAILED) or is a bounded retry that
        re-enters this same phase sequence from its own _authorized() gate."""
        try:
            result = self._do_loiter()
            return self._verified(result)
        except Exception as e:
            self._emit("replan_loiter_restore_failed",
                       f"Could not re-assert LOITER after a failure: {e}", "warning")
            return False

    @staticmethod
    def _verified(result) -> bool:
        return bool(isinstance(result, dict) and result.get("verified"))

    # ── State / status plumbing ───────────────────────────────────────────────
    def _transition(self, new_state: str, reason: str, terminal: bool = False):
        with self._state_lock:
            prev = self._state
            self._state = new_state
            entry = {"from": prev, "to": new_state, "reason": reason,
                     "at": round(self._clock(), 3),
                     "transition_id": self._active_transition_id}
            self._history.append(entry)
            del self._history[:-50]
            if terminal:
                self._last_terminal_at = self._clock()
                self._consume_generation_locked(new_state)
            self._persist_locked()
        # transition_log is the existing audit trail already carried in the
        # status payload -- record there rather than a new log.
        try:
            import transition_log
            transition_log.record_transition("replan", prev, new_state, reason)
        except Exception:
            pass

    def _mark_terminal(self, state: str):
        with self._state_lock:
            self._last_terminal_at = self._clock()
            self._state = state
            self._consume_generation_locked(state)
            self._persist_locked()

    def _consume_generation_locked(self, terminal_state: str) -> None:
        """Latch the running transaction's trigger generation as consumed (task
        section 1). MUST hold self._state_lock. After this, an unchanged active
        condition on the same generation can no longer start a new transaction --
        only a genuinely new generation can. No-op if no transaction generation is
        bound (e.g. a restart-forced terminal, which carries no live generation)."""
        if self._active_generation is not None:
            self._consumed_generation = self._active_generation
        self._terminal_reason = terminal_state

    def _rearm_trigger_locked(self, reason: str) -> None:
        """Clear the terminal latch so a genuinely new generation is permitted
        (task section 1). MUST hold self._state_lock. Used by reset() and
        note_new_mission(): drops the consumed marker and forces the next active
        observation to be treated as a fresh inactive->active edge, so a still-
        continuously-active condition re-arms into a NEW generation rather than
        staying latched on the consumed one."""
        self._consumed_generation = None
        self._active_generation = None
        self._trigger_active = False
        self._generation_hold_only = False
        self._terminal_reason = None

    def _set_error(self, code: str, message: str, reason_code: str, detail=None):
        with self._state_lock:
            self._last_error = {"code": code, "message": message, "reason_code": reason_code}
            if detail is not None:
                self._last_error["detail"] = detail

    def _finalize_revision(self, revision):
        if revision is None:
            return
        with self._state_lock:
            self._last_revision_dict = revision.to_dict()
        # Experiment-recorder evidence (task section 12/20): the complete
        # MissionRevision record, exactly once per revision number, written
        # as revised_mission_rN.json. Fires on EVERY transaction terminal
        # (success/suspend/fail/fallback) since _finalize_revision is the one
        # place all of them already call -- so a failed/invalid replan (e.g.
        # the E2 return-infeasible/SAFE_HOLD case) still gets its revision
        # evidence recorded, not just a successful one.
        if self._recorder is not None:
            try:
                self._recorder.record_revision(self._last_revision_dict)
            except Exception:
                pass

    def _emit(self, event_type: str, message: str, severity: str):
        if self._event_cb is not None:
            try:
                self._event_cb(event_type, message, severity)
            except Exception:
                pass

    def _persist_locked(self):
        if self._store is not None:
            self._store.save_from(self)

    # ── Status (section 10) ───────────────────────────────────────────────────
    def status(self) -> Dict[str, Any]:
        with self._state_lock:
            rev = self._last_revision_dict or {}
            return {
                "autonomous_execution_enabled": self.cfg.autonomous_execution_enabled,
                "dry_run": self.cfg.dry_run,
                "mode": "DRY_RUN" if self.cfg.dry_run else "EXECUTE",
                # energy_policy.py's OWN persistence-debounced decision (E2
                # water-trial integration task): evidence/diagnostics ONLY --
                # it no longer independently triggers a transaction, see
                # observe()'s docstring. action_request below is the sole
                # authoritative trigger.
                "current_decision": self._last_decision,
                "reason_codes": list(self._last_reason_codes),
                # Authoritative decision-policy action request (E2 water-trial
                # task section 12): lets the recorder/operator reconstruct all
                # three layers (risk / recommendation / action_request / fsm_state)
                # for any given decision snapshot -- None until decision_policy.py
                # is wired in by the caller.
                "action_request": self._last_action_request,
                # P0-3: True when the most recently STARTED transaction took the
                # HOLD-only path (_active_hold_only, bound in _run() -- the
                # authoritative decision policy requested a safety hold and no
                # PLANNING/VALIDATING/UPLOAD was ever attempted, see
                # _direct_safe_hold()). Lets a caller landing on a terminal
                # SAFE_HOLD distinguish "no replan was attempted" from "a
                # replan was attempted and failed" -- the two must not share
                # the same failure wording (see mission_execution_controller.
                # _apply_replan_handoff). False before any transaction has run.
                "hold_only": self._active_hold_only,
                "snapshot_id": self._snapshot_id,
                "fsm_state": self._state,
                "current_step": self._state,
                "active_transition_id": self._active_transition_id,
                "revision_number": self._revision_number,
                "strategy": mission_revision.STRATEGY_SAFE_RETURN_HOME,
                "original_mission_hash": rev.get("original_route_hash"),
                "original_mission_revision": rev.get("parent_revision"),
                "revised_mission_hash": rev.get("revised_route_hash"),
                "revised_mission_revision": rev.get("new_revision"),
                # Identity / proof evidence (CRITICAL ISSUE 2 / 1, thesis trace).
                "replan_operation_id": self._active_transition_id,
                # The PROVEN Operator/package mission id (never the vehicle's null
                # current_mission_id that rev.mission_id was captured from).
                "original_mission_id": ((self._last_original_proof or {}).get("mission_id")
                                        or self._proven_mission_id or rev.get("mission_id")),
                "original_route_count": rev.get("original_route_count"),
                "revised_route_count": rev.get("revised_route_count"),
                "original_mission_proof": self._last_original_proof,
                "revised_mission_proof": rev.get("revised_proof"),
                "revised_progression": self._last_revised_progression,
                # Bounded HOLD-SETTLE proof-acquisition diagnostics (E2 replan
                # armed-LOITER upload race fix) -- see _acquire_hold_settle.
                # None before any transaction has reached HOLD_CONFIRMED;
                # confirmed True/False/None (still polling) thereafter.
                "hold_settle": self._last_hold_settle,
                "energy": dict(self._last_energy_inputs),
                # Battery diagnostics for the current snapshot (task section 5).
                "battery_diagnostics": self._last_battery,
                "authority_status": self._last_authority,
                "authority_age_s": None,
                "authority_blocked": self._authority_blocked,
                "blocked_recommendation": BLOCKED_BY_AUTHORITY if self._authority_blocked else None,
                "retry_count": self._retry_count,
                "max_retries": self.cfg.max_transaction_retries,
                # ── Terminal trigger-generation latch (task section 1) ──────────
                # The current risk/injection condition stays visible (current_
                # decision / reason_codes / energy) even when its generation has
                # been consumed; these fields say whether it can still act.
                "trigger_active": self._trigger_active,
                "trigger_generation": self._trigger_generation,
                "consumed_trigger_generation": self._consumed_generation,
                "trigger_consumed": (self._consumed_generation is not None
                                     and self._trigger_generation == self._consumed_generation),
                "terminal_reason": self._terminal_reason,
                "last_error": self._last_error,
                "validation_outcome": rev.get("validation_result"),
                "upload_outcome": rev.get("upload_operation_result"),
                "readback_outcome": rev.get("readback_verification_result"),
                "fallback_enabled": self.cfg.rtl_fallback_enabled,
                "fallback_state": (FALLBACK_RTL if self._state == FALLBACK_RTL else None),
                "simulated": self._simulated_run or self._last_energy_simulated,
                "in_cooldown": self._in_cooldown_nolock(self._clock()),
                "running": self._action_lock.locked(),
                "planning_package": planning_package.summary(planning_package.load()),
                "planning_package_consistency": {
                    "state": self._consistency_state,
                    "detail": self._consistency_detail,
                },
                "geometry_validation": self._last_geometry_validation,
                "config": _config_block(self.cfg),
                "obstacle_execution_enabled": False,
                "obstacle_note": "Obstacle replanning is groundwork only; not enabled.",
                "history": list(self._history[-10:]),
                "last_revision": self._last_revision_dict,
            }

    def _in_cooldown_nolock(self, now: float) -> bool:
        last = self._last_terminal_at
        return last is not None and (now - last) < self.cfg.cooldown_s

    # snapshot of internal fields for the status store to persist.
    def _persistable(self) -> Dict[str, Any]:
        return {
            "state": self._state,
            "active_transition_id": self._active_transition_id,
            "revision_number": self._revision_number,
            "last_error": self._last_error,
            "last_terminal_at": self._last_terminal_at,
            "last_revision": self._last_revision_dict,
        }

    def _restore(self, data: Dict[str, Any]) -> None:
        self._state = data.get("state", MONITORING)
        self._active_transition_id = data.get("active_transition_id")
        self._revision_number = data.get("revision_number", 0) or 0
        self._last_error = data.get("last_error")
        self._last_terminal_at = data.get("last_terminal_at")
        self._last_revision_dict = data.get("last_revision")


def _config_block(cfg) -> Dict[str, Any]:
    """Resolved config values + per-field source for status. Sourced from
    replan_config.resolve() (authoritative), which the running cfg is kept in
    sync with by replan_runtime.apply_config_patch()."""
    try:
        resolved, sources = replan_config.resolve()
        return {"values": resolved.to_dict(), "sources": sources}
    except Exception:
        return {"values": cfg.to_dict(), "sources": {}}


def _simulated_mode_result(custom_mode: int, name: str) -> Dict[str, Any]:
    return {"accepted": True, "verified": True, "observed_mode": custom_mode,
            "requested_mode": name, "simulated": True, "dry_run": True}


def _summarize_upload(upload: Dict[str, Any]) -> Dict[str, Any]:
    """Bounded summary of a mission-contract upload result for the revision
    record -- never the raw diagnostics/waypoint lists."""
    if not isinstance(upload, dict):
        return {"accepted": None, "uploaded": None, "verified": None}
    return {
        "accepted": upload.get("accepted"),
        "uploaded": upload.get("uploaded"),
        "verified": upload.get("verified"),
        "expected_route_content_hash": upload.get("expected_route_content_hash"),
        "observed_route_content_hash": upload.get("observed_route_content_hash"),
        "observed_route_waypoint_count": upload.get("observed_route_waypoint_count"),
        "error": upload.get("error"),
        "simulated": upload.get("simulated", False),
    }


def _summarize_progression(watch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bounded summary of the shared progression watch for the revision record /
    status -- the proof used, timing, and a bounded tail of samples (never the
    unbounded sample stream)."""
    if not isinstance(watch, dict):
        return None
    return {
        "proven": watch.get("proven"),
        "proof": watch.get("proof"),
        "failure_code": watch.get("failure_code"),
        "failure_message": watch.get("failure_message"),
        "configured_timeout_s": watch.get("configured_timeout_s"),
        "actual_elapsed_s": watch.get("actual_elapsed_s"),
        "sample_count": watch.get("sample_count"),
        "baseline_sequence": (watch.get("baseline") or {}).get("baseline_sequence"),
        "final_sequence": watch.get("final_sequence"),
        "final_mode": watch.get("final_mode"),
        "final_armed": watch.get("final_armed"),
        "authority": watch.get("authority"),
        "max_groundspeed": watch.get("max_groundspeed"),
        "max_distance_moved_m": watch.get("max_distance_moved_m"),
        "mission_active_evidence_observed": watch.get("mission_active_evidence_observed"),
        "simulated": watch.get("simulated", False),
        "samples": (watch.get("samples") or [])[-20:],
    }


def _summarize_verification(upload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(upload, dict):
        return {"verified": None}
    return {
        "verified": upload.get("verified"),
        "verification_status": upload.get("verification_status"),
        "expected_route_content_hash": upload.get("expected_route_content_hash"),
        "observed_route_content_hash": upload.get("observed_route_content_hash"),
        "simulated": upload.get("simulated", False),
    }


# ── Restart-safe status store ─────────────────────────────────────────────────
class StatusStore:
    """Persists the controller's restart-relevant fields to a JSON file (atomic
    replace), so an interrupted transaction is detectable at next startup. Kept
    small and separate so the controller stays testable without touching disk
    (tests pass status_store=None)."""

    def __init__(self, path: Optional[str] = None):
        import config as _config
        self.path = path or getattr(_config, "REPLAN_STATUS_FILE", None)

    def load_into(self, controller: "ReplanController") -> None:
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

    def save_from(self, controller: "ReplanController") -> None:
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
            print(f"[REPLAN] could not persist controller status: {e}")
