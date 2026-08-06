"""Operator-side client for Scout's Local Agent MISSION-EXECUTION API (`/agent/mission_execution/*`,
port 8090).

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
---------------------------------------------
Scout's Local Agent owns the mission-execution lifecycle OUTRIGHT. Start is ONE Scout-side
transaction — validate → verified LOITER → set Home to the current launch position → read Home
back and verify it → synchronize the planning-package Home → revalidate the package → verified
AUTO → progression confirmation → RUNNING. Pause and Resume are likewise single Scout
transactions that record and re-verify the mission sequence.

The Operator Station therefore does NOT:
  * run a second mission-execution FSM (no shadow state, no inferred transitions);
  * issue separate LOITER / SET_HOME / AUTO commands to implement "Start";
  * decide when Home is good, when the vehicle has arrived, or when the mission is complete;
  * queue any of this through the operator command queue (this is not a queued command path —
    it is a direct, synchronous, per-vehicle proxy to the selected Scout's Local Agent).

It only forwards an explicit operator intent, preserves Scout's body verbatim, and reconciles
an operation whose HTTP verdict was lost.

HTTP 200 IS NOT THE SAME AS SUCCESS
-----------------------------------
Scout answers 200 for "I processed your request" and 409 for "I refused it (precondition,
lifecycle state, replanning ownership, write arbitration)". A 200 body can still carry a
VEHICLE-level failure: `accepted:false`, or a structured `error` such as LOITER_NOT_VERIFIED /
SET_HOME_FAILED / AUTO_NOT_VERIFIED / PROGRESSION_UNCONFIRMED. Treating every 200 as success is
exactly the lie this station must not tell, so `interpret_operation()` re-reads the body and
narrows the transport outcome to an OPERATIONAL one:

  accepted  — 2xx and Scout's body says it worked (`accepted` not false, no `error`).
  failed    — 2xx but Scout reported a vehicle-level failure in the body. Scout's own error
              code and message are preserved and shown; the operator is never told "started".
  rejected  — a definite 4xx refusal (409 precondition / lifecycle / replanning / arbitration).
  unknown   — no verdict reached us (write timeout, dropped connection, ambiguous 5xx). The
              operation MAY have taken effect. We NEVER auto-resend it; `reconcile()` reads
              canonical status and resolves it from Scout's own state instead.
  unavailable / unsupported — a failed read, or an older Scout that 404s these routes. An older
              Scout is `supported:false`, never a fabricated READY / can_start / verified Home.

ONE MOCKING SURFACE: the HTTP itself is scout_replan.read/write (the shared Local Agent
transport), so a test swaps `scout_replan.requests` for both subsystems and the two can never
drift on what "unknown" means.
"""
from __future__ import annotations

import scout_replan

# Re-exported so callers reason in ONE outcome vocabulary across both Local Agent subsystems.
OUTCOME_ACCEPTED = scout_replan.OUTCOME_ACCEPTED
OUTCOME_REJECTED = scout_replan.OUTCOME_REJECTED
OUTCOME_UNKNOWN = scout_replan.OUTCOME_UNKNOWN
OUTCOME_UNAVAILABLE = scout_replan.OUTCOME_UNAVAILABLE
OUTCOME_UNSUPPORTED = scout_replan.OUTCOME_UNSUPPORTED
# The one outcome that only exists at the OPERATIONAL layer: Scout answered 200 and then told
# us, in the body, that the vehicle-level operation did not succeed.
OUTCOME_FAILED = "failed"

SUBSYSTEM = "mission-execution"
BASE_PATH = "/agent/mission_execution"

# Scout's mission-execution states, verbatim. The operator never invents one and never orders
# them into a private FSM — this tuple exists so an UNRECOGNIZED state is displayed as-is and
# flagged, rather than silently bucketed into a state the operator would act on.
STATES = (
    "NOT_READY", "NOT_STARTED", "READY",
    "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED",
    "SETTING_HOME", "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
    "RUNNING",
    "PAUSE_REQUESTED", "PAUSED", "RESUME_REQUESTED",
    "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED", "STOPPED", "CANCELLED",
    "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED", "COMPLETED_HOLD",
    "SUSPENDED", "FAILED",
)

# ── The STOP contract (PENDING ON SCOUT — see SCOUT_STOP_API.md) ──────────────────────────
# Scout does not implement POST /agent/mission_execution/stop yet. The Operator model, proxy
# and UI are written against the contract below so that the day Scout ships it, nothing here
# changes; until then every Stop attempt answers `unsupported` (404) and the UI shows Stop as
# disabled with that exact reason. Nothing about Stop is ever synthesized from a low-level
# LOITER plus operator-side state — that would be a second, competing lifecycle.
#
#   POST /agent/mission_execution/stop  →  STOP_REQUESTED → STOP_HOLD_REQUESTED
#                                          → STOP_HOLD_CONFIRMED → STOPPED
#
# Stop ENDS the run and leaves the vehicle in a verified LOITER. It must NOT disarm, clear the
# Pixhawk mission, delete the planning package, invoke RTL, be reported as FAILED, or be
# emulated with Rearm.
STOP_SEQUENCE = ("STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED", "STOPPED")
STOP_IN_TRANSACTION_STATES = frozenset({
    "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
})
# Terminal "the run was deliberately ended" states. CANCELLED is accepted as a synonym so a
# Scout that names it differently is not displayed as an unrecognized state.
STOPPED_STATES = frozenset({"STOPPED", "CANCELLED"})

# Scout may report this as `effective_state` while its STORED state is still RUNNING/PAUSED:
# the replanning controller owns the vehicle for the duration. It is an overlay, not a state.
EFFECTIVE_REPLANNING = "REPLANNING"

# States in which Scout is MID-TRANSACTION — it is still deciding the outcome of a start, pause
# or resume. A reconciling read that lands here answers "not yet decided", not success or failure.
IN_TRANSACTION_STATES = frozenset({
    "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED", "SETTING_HOME",
    "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
    "PAUSE_REQUESTED", "RESUME_REQUESTED",
}) | STOP_IN_TRANSACTION_STATES

# The RETURN phase. Distinct from the set above: reaching it means the run progressed well past
# the operation being reconciled — it is NOT an undecided outcome. The mission is also NOT
# complete here (see COMPLETED_HOLD + final_loiter_verified).
RETURN_PHASE_STATES = frozenset({
    "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED",
})

# Everything that is not a RESTING state. The operator shows progress and disables the primary
# control throughout; it does NOT predict which state comes next.
TRANSITIONAL_STATES = IN_TRANSACTION_STATES | RETURN_PHASE_STATES

# States a Rearm is meaningful from (Scout still arbitrates; this only shapes the affordance).
REARMABLE_STATES = frozenset({"COMPLETED_HOLD", "SUSPENDED", "FAILED"})

# RESTING states from which a run has NOT begun — the only states from which the operator
# station may conclude that a failed Start left the vehicle untouched. Used by the Start
# transaction's authority-restore proof (see mission_lifecycle.py): outside this set, authority
# is NEVER taken back automatically, because Scout may be commanding the vehicle.
PRE_START_STATES = frozenset({"NOT_READY", "NOT_STARTED", "READY"}) | STOPPED_STATES

# Scout error codes raised BEFORE it issues any vehicle command for a Start — validation of the
# mission, the package, the position, the authority or the controller's own arbitration. Only
# these make a post-failure return of OPERATOR authority provable; everything else
# (LOITER_NOT_VERIFIED, SET_HOME_FAILED, PACKAGE_SYNC_FAILED, AUTO_NOT_VERIFIED,
# PROGRESSION_UNCONFIRMED) happens AFTER Scout began commanding the vehicle, so the vehicle
# state is uncertain and authority must stay where it is until the operator decides.
PRE_ACTION_ERROR_CODES = frozenset({
    "NO_ACTIVE_MISSION", "NO_PLANNING_PACKAGE", "MISSION_ID_MISMATCH",
    "POSITION_STALE_OR_INVALID", "PIXHAWK_STATE_UNAVAILABLE",
    "MISSION_EXECUTION_DISABLED", "REPLANNING_ACTIVE", "ARBITRATION_BUSY",
    "AUTHORITY_LOST", "NOT_READY", "START_NOT_ALLOWED",
})

OPERATIONS = ("start", "pause", "resume", "stop", "rearm")

# Fields that IDENTIFY a body as a mission-execution status. At least one must be present, and
# this guard is not theoretical: an older Local Agent routes with
# `self.path.startswith("/agent/mission")`, so GET /agent/mission_execution/status is swallowed by
# its legacy /agent/mission handler and answers HTTP 200 with a PIXHAWK MISSION READBACK
# (mission_count / waypoints / mission_hash). Without this check that body would be accepted as a
# lifecycle status whose every field happens to be absent — a card claiming the lifecycle is
# supported while showing blank state, blank Home and blank completion. That is exactly the
# fabrication Section 11 forbids, so an unrecognized body is `unsupported`, like a 404.
# Presence of the KEY is what counts: `active_operation_id: null` is a legitimate value.
STATUS_IDENTIFYING_FIELDS = (
    "state", "effective_state", "execution_state", "mission_execution_enabled",
    "can_start", "can_pause", "can_resume", "can_stop",
)


def _body(result):
    """Scout's parsed body from a transport result ({} when there is none)."""
    b = result.get("scout") if isinstance(result, dict) else None
    return b if isinstance(b, dict) else {}


def _first(body, *names):
    """First present (non-None) value among candidate field spellings, or None."""
    for n in names:
        v = body.get(n)
        if v is not None:
            return v
    return None


def _str_or_none(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ── Reads ────────────────────────────────────────────────────────────────────────────────
def is_status_body(body):
    """True when a 2xx body actually IS a mission-execution status (see
    STATUS_IDENTIFYING_FIELDS). An empty body is not one; neither is another endpoint's body."""
    if not isinstance(body, dict) or not body:
        return False
    return any(f in body for f in STATUS_IDENTIFYING_FIELDS)


def get_status(base):
    """Scout's canonical mission-execution status object. Read-only, never fabricated: an
    unreachable Scout is `unavailable`; an older Scout that 404s the route — or that answers 200
    with SOME OTHER endpoint's body because it prefix-matches `/agent/mission` — is
    `supported:false`, so nothing downstream can read a blank card as a supported lifecycle."""
    out = scout_replan.read("mission_execution.status", base, f"{BASE_PATH}/status",
                            subsystem=SUBSYSTEM)
    if out.get("outcome") == OUTCOME_ACCEPTED and not is_status_body(out.get("scout")):
        out.update(
            ok=False, supported=False, outcome=OUTCOME_UNSUPPORTED,
            error=("This Scout answered /agent/mission_execution/status with a body that is not a "
                   "mission-execution status (an older Local Agent prefix-matches /agent/mission "
                   "and returns its Pixhawk mission readback) — the lifecycle is not supported"))
    return out


# ── Writes (one explicit operator intent each; never issued by a poll) ────────────────────
def _op(operation, base, json_body=None):
    return scout_replan.write(
        f"mission_execution.{operation}", base, f"{BASE_PATH}/{operation}", "POST", json_body,
        subsystem=SUBSYSTEM,
        conflict_error=("Scout refused the mission-execution operation: precondition, lifecycle "
                        "state, replanning ownership or write arbitration"))


def post_start(base, mission_id=None):
    """Start the mission. Body carries the mission id the OPERATOR believes is active, so Scout
    can fail closed with MISSION_ID_MISMATCH rather than starting the wrong route; Scout treats
    the body as optional, so an absent id is sent as an empty body rather than a guess."""
    body = {"mission_id": mission_id} if mission_id else {}
    return _op("start", base, body)


def post_pause(base):
    """Pause the mission (record sequence → verified LOITER → confirm mission still loaded →
    PAUSED). NOT a stop/cancel: it does not clear the mission, upload a replacement, or reset
    the sequence. A repeated Pause while already PAUSED is an idempotent success on Scout."""
    return _op("pause", base, {})


def post_resume(base):
    """Resume the mission (verify mission still loaded → verify Home/position → verified AUTO →
    observe sequence → RUNNING). Scout may report RUNNING with continuation_verified=false; that
    is a warning the caller MUST surface, not a success to round up."""
    return _op("resume", base, {})


def post_stop(base, mission_id=None):
    """END the mission run: Scout requests a hold, confirms a VERIFIED LOITER, and settles in
    STOPPED (STOP_REQUESTED -> STOP_HOLD_REQUESTED -> STOP_HOLD_CONFIRMED -> STOPPED).

    NOT IMPLEMENTED ON SCOUT YET. Until it is, this route 404s and the whole path answers
    `unsupported` — which is exactly what the operator is shown. Stop is deliberately NOT
    emulated from a low-level LOITER plus operator-side bookkeeping: that would be a second
    lifecycle competing with Scout's own, and the station has exactly one.

    Stop does NOT disarm, does NOT clear the Pixhawk mission, does NOT delete the planning
    package, does NOT invoke RTL, is NOT a FAILED outcome, and is NOT Rearm. See
    SCOUT_STOP_API.md for the contract this is written against."""
    body = {"mission_id": mission_id} if mission_id else {}
    return _op("stop", base, body)


def post_rearm(base):
    """Rearm the Local Agent's mission-execution controller from a terminal state. Issues NO
    vehicle command, does NOT change vehicle mode, does NOT clear the Pixhawk mission and does
    NOT re-upload the original mission — it only prepares the controller for another explicitly
    prepared run."""
    return _op("rearm", base, {})


# ── Operation-result interpretation (the HTTP-200-is-not-success rule) ────────────────────
def interpret_operation(result):
    """Narrow a transport result into the OPERATIONAL verdict plus the Scout fields the station
    reasons and reports with. Scout's body is preserved untouched under `scout`; everything here
    is a derived view of it, never a rewrite.

    The load-bearing rule: a 2xx whose body carries `error`, or `accepted:false`, is `failed` —
    a vehicle-level failure with Scout's own code and message — not a success."""
    out = dict(result or {})
    body = _body(out)

    accepted = body.get("accepted")
    scout_error = _first(body, "error")
    # Scout's structured error can arrive as a bare code string or as an object carrying one.
    error_code, error_message = None, None
    if isinstance(scout_error, dict):
        error_code = _str_or_none(_first(scout_error, "code", "error_code", "error"))
        error_message = _str_or_none(_first(scout_error, "message", "detail", "reason"))
    elif scout_error is not None:
        error_code = _str_or_none(scout_error)
    if error_code is None:
        error_code = _str_or_none(_first(body, "error_code"))
    if error_message is None:
        error_message = _str_or_none(_first(body, "message", "error_message"))

    transport = out.get("outcome")
    if transport == OUTCOME_ACCEPTED and (error_code or accepted is False):
        # Scout processed the request and told us the vehicle-level operation did not succeed.
        operational = OUTCOME_FAILED
    else:
        operational = transport

    out.update({
        "operational_outcome": operational,
        "accepted": accepted,
        "scout_error_code": error_code or out.get("scout_error_code"),
        "scout_error_message": error_message,
        "operation_outcome": _str_or_none(body.get("outcome")),
        "operation_id": _str_or_none(_first(body, "operation_id")),
        "execution_state": _str_or_none(_first(body, "execution_state")),
        "previous_state": _str_or_none(_first(body, "previous_state")),
        "current_state": _str_or_none(_first(body, "current_state", "execution_state")),
        "verified_mode": _str_or_none(_first(body, "verified_mode")),
        "mission_id": _str_or_none(_first(body, "mission_id")),
        "route_hash": _str_or_none(_first(body, "route_hash")),
        "final": body.get("final"),
        "idempotent": body.get("idempotent"),
        "home_result": body.get("home_result") if isinstance(body.get("home_result"), dict) else None,
        "sequence": body.get("sequence") if isinstance(body.get("sequence"), dict) else None,
    })
    out["ok"] = operational == OUTCOME_ACCEPTED
    return out


def summarize_status(result):
    """A derived, JSON-able summary of Scout's canonical status for logging/reconciliation.
    Every field is Scout's or None — `supported:false` and `reachable:false` stay honest and
    nothing (READY, can_start, verified Home, continuation, completion) is ever defaulted in."""
    body = _body(result)
    supported = bool(result.get("supported", True)) and body.get("supported") is not False
    reachable = bool(result.get("reachable", True))
    seq = body.get("sequence") if isinstance(body.get("sequence"), dict) else {}
    rc = body.get("return_completion") if isinstance(body.get("return_completion"), dict) else {}
    rp = body.get("replanning") if isinstance(body.get("replanning"), dict) else {}
    state = _str_or_none(body.get("state"))
    effective = _str_or_none(body.get("effective_state")) or state
    return {
        "supported": supported,
        "reachable": reachable,
        "present": bool(supported and reachable and body),
        "state": state,
        "effective_state": effective,
        "replanning_active": bool(rp.get("active")) or effective == EFFECTIVE_REPLANNING,
        "replanning_fsm_state": _str_or_none(rp.get("fsm_state")),
        "active_operation_id": _str_or_none(body.get("active_operation_id")),
        "mission_id": _str_or_none(body.get("mission_id")),
        "original_route_hash": _str_or_none(body.get("original_route_hash")),
        "active_route_hash": _str_or_none(body.get("active_route_hash")),
        "mode": _str_or_none(body.get("mode")),
        "authority_status": _str_or_none(body.get("authority_status")),
        "can_start": body.get("can_start"),
        "can_pause": body.get("can_pause"),
        "can_resume": body.get("can_resume"),
        # PRESENCE is the support signal for Stop, not the value: a Scout that has shipped the
        # Stop endpoint reports can_stop (true or false); one that has not omits the key
        # entirely, and `None` here is what makes the UI show Stop as UNSUPPORTED rather than
        # merely "not right now".
        "can_stop": body.get("can_stop"),
        "stop_supported": "can_stop" in body,
        "verified_mode": _str_or_none(body.get("verified_mode")),
        "mission_execution_enabled": body.get("mission_execution_enabled"),
        "sequence": dict(seq) if seq else None,
        "continuation_verified": seq.get("continuation_verified") if seq else None,
        "return_completion": dict(rc) if rc else None,
        "final_loiter_verified": rc.get("final_loiter_verified") if rc else None,
        "last_error": body.get("last_error"),
    }


# ── Reconciliation of an UNKNOWN write (never a resend) ───────────────────────────────────
# A write whose verdict was lost is resolved by READING Scout's canonical status and comparing
# it against what the operation was trying to achieve. This is the only correct move: resending
# a Start could re-run a whole Home/AUTO transaction the vehicle already performed.
def reconcile(base, operation, *, expected_mission_id=None):
    """Resolve an UNKNOWN `operation` by reading canonical status. Returns a dict whose
    `resolved` is one of:

      running / paused / completed / suspended / failed / ready  — Scout's state answers it;
      in_progress   — Scout is still mid-transaction (an active operation id / transitional
                      state); the outcome is not yet decided, so it stays undecided here;
      mission_mismatch — Scout is running a DIFFERENT mission than the one we asked to start;
      unknown       — the status read itself failed, or Scout's state does not answer the
                      question. Never upgraded to a success, never downgraded to a failure.
    """
    status = get_status(base)
    summary = summarize_status(status)
    out = {
        "attempted": True,
        "operation": operation,
        "status_outcome": status.get("outcome"),
        "resolved": OUTCOME_UNKNOWN,
        "detail": None,
        "expected_mission_id": expected_mission_id,
        "mission_id_match": None,
        **{k: summary[k] for k in (
            "supported", "reachable", "state", "effective_state", "active_operation_id",
            "mission_id", "mode", "verified_mode", "sequence", "continuation_verified",
            "return_completion", "final_loiter_verified", "can_start", "can_pause",
            "can_resume", "can_stop", "stop_supported", "last_error")},
    }

    if not summary["present"]:
        out["detail"] = ("Mission-execution status could not be read — the operation's outcome "
                         "stays UNKNOWN and must not be resent blindly")
        return out

    state = summary["state"]
    if expected_mission_id and summary["mission_id"]:
        out["mission_id_match"] = summary["mission_id"] == expected_mission_id

    # A different mission is running than the one we asked to start: a definite answer, and one
    # the operator has to see rather than a quiet "started".
    if operation == "start" and out["mission_id_match"] is False:
        out["resolved"] = "mission_mismatch"
        out["detail"] = (f"Scout reports mission {summary['mission_id']}, not the expected "
                         f"{expected_mission_id}")
        return out

    # Still mid-transaction — undecided, by design. The next poll answers it. The RETURN phase is
    # deliberately NOT included: reaching it means the run got well past the operation in question.
    if summary["active_operation_id"] or state in IN_TRANSACTION_STATES:
        out["resolved"] = "in_progress"
        out["detail"] = f"Scout is still processing ({state or 'transitional'})"
        return out

    if state in ("SUSPENDED", "FAILED"):
        out["resolved"] = state.lower()
        out["detail"] = summary["last_error"] or f"Scout reports {state}"
        return out
    if state == "COMPLETED_HOLD":
        out["resolved"] = "completed"
        out["detail"] = ("Scout reports COMPLETED_HOLD"
                         + ("" if summary["final_loiter_verified"] else
                            " but final LOITER is NOT verified"))
        return out

    # A deliberately ENDED run. Reported before the per-operation branches because it answers
    # every one of them: a Start that left Scout STOPPED did not take effect, and a Stop that
    # left it STOPPED did.
    if state in STOPPED_STATES:
        out["resolved"] = "stopped" if operation == "stop" else "not_started"
        out["detail"] = (f"Scout reports {state}"
                         + ("" if operation == "stop" else " — the operation did not take effect")
                         + (f" in mode {summary['mode']}" if summary["mode"] else ""))
        return out

    if operation == "start":
        if state in ("RUNNING", "RETURNING_HOME"):
            out["resolved"] = "running"
            out["detail"] = f"Scout reports {state} in mode {summary['mode'] or 'unknown'}"
        elif state == "READY":
            out["resolved"] = "ready"
            out["detail"] = "Scout is still READY — the start did not take effect"
        else:
            out["detail"] = f"Scout reports {state} — start outcome undetermined"
    elif operation == "pause":
        if state == "PAUSED":
            out["resolved"] = "paused"
            out["detail"] = (f"Scout reports PAUSED in mode {summary['mode'] or 'unknown'}"
                             f"{_seq_note(summary)}")
        elif state == "RUNNING":
            out["resolved"] = "running"
            out["detail"] = "Scout is still RUNNING — the pause did not take effect"
        else:
            out["detail"] = f"Scout reports {state} — pause outcome undetermined"
    elif operation == "resume":
        if state == "RUNNING":
            out["resolved"] = "running"
            cv = summary["continuation_verified"]
            out["detail"] = (f"Scout reports RUNNING in mode {summary['mode'] or 'unknown'}; "
                             + ("continuation verified" if cv is True
                                else "continuation NOT verified" if cv is False
                                else "continuation not reported"))
        elif state == "PAUSED":
            out["resolved"] = "paused"
            out["detail"] = "Scout is still PAUSED — the resume did not take effect"
        else:
            out["detail"] = f"Scout reports {state} — resume outcome undetermined"
    elif operation == "stop":
        # STOPPED itself is handled above. Anything else means the stop has not settled: the
        # run is still live, and authority stays exactly where it is.
        if state in ("RUNNING", "RETURNING_HOME"):
            out["resolved"] = "running"
            out["detail"] = "Scout is still RUNNING — the stop did not take effect"
        elif state == "PAUSED":
            out["resolved"] = "paused"
            out["detail"] = "Scout is still PAUSED — the stop did not take effect"
        else:
            out["detail"] = f"Scout reports {state} — stop outcome undetermined"
    elif operation == "rearm":
        if state in ("READY", "NOT_READY"):
            out["resolved"] = "ready" if state == "READY" else "not_ready"
            out["detail"] = f"Scout reports {state} — the controller was rearmed"
        else:
            out["detail"] = f"Scout reports {state} — rearm outcome undetermined"
    return out


def _seq_note(summary):
    seq = summary.get("sequence") or {}
    cur, count = seq.get("current"), seq.get("count")
    if cur is None and count is None:
        return ""
    return f" at sequence {cur if cur is not None else '?'}/{count if count is not None else '?'}"
