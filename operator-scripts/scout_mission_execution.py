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

# ── The Start read budget (the ONE route that needs its own) ───────────────────────────────
# Start is not a short supervisory write. It is ONE BOUNDED SCOUT TRANSACTION, and Scout's own
# bounds are what set this number:
#
#   start_proof_timeout_s        15 s   verified LOITER / launch-hold proof
#   mode verification                   verified AUTO
#   start_progression_timeout_s  10 s   progression confirmation before RUNNING
#   operation_timeout_s          60 s   the CEILING on the whole transaction
#
# A healthy Start therefore takes as long as the vehicle takes — a real run measured 12.0 s end
# to end, and Scout is entitled to 60. The shared WRITE_READ_TIMEOUT (12 s) sat *inside* that
# window, so the operator's HTTP client gave up while Scout was still working, the transport
# reported `unknown`, and the UI printed "Mission could not start: No response from Scout" about
# a mission that entered RUNNING and AUTO. The client must not be the first thing to quit.
#
# 65 s = Scout's operation_timeout_s (60) + a 5 s margin for the response itself. It is a BOUND,
# not an absence of one: past it, Scout has exceeded its own ceiling and `unknown` is the honest
# answer again. Nothing else on the Local Agent transport is affected — this budget is passed per
# call, so status reads stay on READ_TIMEOUT and pause/resume/stop/rearm stay on
# WRITE_READ_TIMEOUT.
START_READ_TIMEOUT = 65.0

# Scout's mission-execution states, verbatim. The operator never invents one and never orders
# them into a private FSM — this tuple exists so an UNRECOGNIZED state is displayed as-is and
# flagged, rather than silently bucketed into a state the operator would act on.
STATES = (
    "NOT_READY", "NOT_STARTED", "READY",
    # The Start transaction, in Scout's own order. ARMING / VERIFYING_ARMED / CONFIRMING_PROGRESSION
    # are steps Scout reports and this station did not recognize: a reconciling read that landed on
    # one of them fell through to "start outcome undetermined" — an UNDECIDED transaction reported
    # as an unanswered one — instead of "Scout is still processing".
    "START_REQUESTED", "ARMING", "VERIFYING_ARMED",
    "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED",
    "SETTING_HOME", "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
    "CONFIRMING_PROGRESSION",
    "RUNNING",
    "PAUSE_REQUESTED", "PAUSED", "RESUME_REQUESTED",
    "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
    "STOP_VERIFYING_MISSION", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
    "STOP_VERIFYING_REWIND", "STOP_RESETTING", "STOP_VERIFYING_RESET",
    "STOPPED", "CANCELLED",
    "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED", "COMPLETED_HOLD",
    "SUSPENDED", "FAILED",
)

# ── The STOP lifecycle operation (Scout owns the WHOLE transaction) ───────────────────────
# POST /agent/mission_execution/stop is a FIRST-CLASS Scout lifecycle operation. It is NOT a raw
# Pixhawk stop and it is NOT something this station assembles from parts. Scout performs, in one
# transaction:
#
#   active mission → verified LOITER → verify active mission identity → restore the immutable
#   original mission if a verified revised route is installed → rewind the original mission to
#   its start → verify the rewind → reset mission-execution / replan / test state → clear the
#   simulated experiment injection → invalidate the prior runtime Home → return supervisory
#   authority to OPERATOR → re-prove mission evidence
#
# The Operator forwards the intent, preserves Scout's evidence verbatim, and re-reads canonical
# status. It issues NO LOITER, NO mission upload, NO rewind, NO rearm, NO reset and NO authority
# write of its own — every one of those would be a second lifecycle competing with Scout's.
#
# A SUCCESSFUL Stop normally comes to rest at:
#
#   state = NOT_READY, start_eligible = true, authority_blocks_start = true, authority = OPERATOR
#
# which is NOT a failure and must never be displayed as one: authority is deliberately back with
# the OPERATOR, and the Operator's own Start transaction is what hands it to LOCAL_AGENT again.
STOP_SEQUENCE = (
    "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
    "STOP_VERIFYING_MISSION", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
    "STOP_VERIFYING_REWIND", "STOP_RESETTING", "STOP_VERIFYING_RESET",
)
STOP_IN_TRANSACTION_STATES = frozenset(STOP_SEQUENCE)
# Terminal "the run was deliberately ended" states Scout may REST in. CANCELLED is accepted as a
# synonym so a Scout that names it differently is not displayed as an unrecognized state. Under
# the current contract Scout normally settles in NOT_READY instead, with the outcome carried in
# the `stop` evidence block — which is why nothing downstream may require STOPPED to call a Stop
# successful (see stop_evidence / stop_view).
STOPPED_STATES = frozenset({"STOPPED", "CANCELLED"})

# Scout's structured Stop failure codes. Every one of these is raised AFTER the vehicle is safely
# holding: Scout reaches a verified LOITER first and only then restores/rewinds/resets, so a Stop
# that fails here leaves the vehicle held and the reset INCOMPLETE. The operator is shown Scout's
# exact code; the station never follows one with an automatic Rearm, Resume, AUTO or second Stop.
STOP_ERROR_CODES = frozenset({
    "STOP_ACTIVE_MISSION_UNKNOWN", "STOP_RESTORE_UPLOAD_FAILED", "STOP_RESTORE_HASH_MISMATCH",
    "STOP_REWIND_NOT_VERIFIED", "STOP_HOLD_NOT_VERIFIED", "STOP_MISSION_ID_MISMATCH",
})

# The fields of Scout's `stop` evidence block, in the order the operator reads them. Kept as one
# tuple so the backend summary, the write trace and the UI diagnostics cannot drift.
STOP_EVIDENCE_FIELDS = (
    "hold_verified", "original_restored", "active_hash_before", "original_hash", "revised_hash",
    "rewind_verified", "sequence_after", "replan_reset", "experiment_cleared", "authority_after",
    "ready_for_start", "outcome",
)

# Scout may report this as `effective_state` while its STORED state is still RUNNING/PAUSED:
# the replanning controller owns the vehicle for the duration. It is an overlay, not a state.
EFFECTIVE_REPLANNING = "REPLANNING"

# States in which Scout is MID-TRANSACTION — it is still deciding the outcome of a start, pause
# or resume. A reconciling read that lands here answers "not yet decided", not success or failure.
IN_TRANSACTION_STATES = frozenset({
    "START_REQUESTED", "ARMING", "VERIFYING_ARMED",
    "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED", "SETTING_HOME",
    "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO", "CONFIRMING_PROGRESSION",
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
    # Scout's explicit Start-eligibility contract. A Scout that reports these but happens to
    # omit `state` is still answering with a mission-execution status.
    "start_eligible", "execution_ready", "authority_blocks_start",
)

# ── Scout's explicit Start-eligibility contract ───────────────────────────────────────────
# `can_start` alone was never sufficient and is no longer the operator's input. Scout reports
# eligibility and the authority question SEPARATELY, because they are separate facts:
#
#   start_eligible          the mission, package, Home and lifecycle state permit a Start
#   authority_blocks_start  ... but authority is not LOCAL_AGENT yet
#   execution_ready         Scout is ready to run RIGHT NOW, under LOCAL_AGENT
#   start_block_reason      when start_eligible is false, Scout's own words for why
#
# Scout does NOT seize LOCAL_AGENT authority by itself, so `start_eligible:true` with
# `authority_blocks_start:true` is the NORMAL pre-Start condition — the Operator's Start
# transaction acquires and verifies authority as its first phase. Presenting that as a broken or
# unprepared mission (the old AUTHORITY_NOT_LOCAL_AGENT reading) told the operator to go and fix
# something that the very button they were looking at was going to do for them.

# ── The mission/package binding Scout reports ─────────────────────────────────────────────
# Whether the package Scout holds is BOUND to the original mission it is executing.
BINDING_UNBOUND = "UNBOUND"
BINDING_BOUND = "BOUND"
BINDING_STALE_MISMATCH = "STALE_MISMATCH"
BINDING_STATES = frozenset({BINDING_UNBOUND, BINDING_BOUND, BINDING_STALE_MISMATCH})

# Conflict codes Scout raises when a NEW package arrives against a run it cannot replace.
CONFLICT_STALE_PACKAGE_DURING_ACTIVE_EXECUTION = "STALE_PACKAGE_DURING_ACTIVE_EXECUTION"
CONFLICT_OPERATION_IN_PROGRESS = "OPERATION_IN_PROGRESS"

# Binding/conflict evidence that means a newly uploaded mission must NOT be presented as ready:
# the previous run still owns the vehicle and only Scout can end it.
ACTIVE_CONFLICT_CODES = frozenset({
    CONFLICT_STALE_PACKAGE_DURING_ACTIVE_EXECUTION, CONFLICT_OPERATION_IN_PROGRESS})


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


def stop_evidence(body):
    """Scout's `stop` evidence block, normalized but NEVER invented.

    Returns {reported, hold_verified, original_restored, active_hash_before, original_hash,
    revised_hash, rewind_verified, sequence_after, replan_reset, experiment_cleared,
    authority_after, ready_for_start, outcome} — every value Scout's own or None. `reported` is
    false when Scout carries no `stop` block at all, which is what keeps a station that has
    never seen a Stop from displaying a fabricated one.

    Booleans stay TRI-STATE (true / false / None): "Scout did not verify the rewind" and "Scout
    said nothing about the rewind" are different facts, and rounding the second into the first
    would report a failure Scout never claimed."""
    blk = body.get("stop") if isinstance(body, dict) else None
    if not isinstance(blk, dict) or not blk:
        return {"reported": False, **{f: None for f in STOP_EVIDENCE_FIELDS}}
    out = {"reported": True}
    for f in STOP_EVIDENCE_FIELDS:
        out[f] = blk.get(f)
    # The hashes, the outcome and the authority are IDENTIFIERS, so they are normalized to text;
    # the flags and the sequence keep Scout's own type.
    for f in ("active_hash_before", "original_hash", "revised_hash", "authority_after", "outcome"):
        out[f] = _str_or_none(out[f])
    return out


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
def _op(operation, base, json_body=None, *, read_timeout=None):
    return scout_replan.write(
        f"mission_execution.{operation}", base, f"{BASE_PATH}/{operation}", "POST", json_body,
        subsystem=SUBSYSTEM,
        conflict_error=("Scout refused the mission-execution operation: precondition, lifecycle "
                        "state, replanning ownership or write arbitration"),
        read_timeout=read_timeout)


def post_start(base, mission_id=None):
    """Start the mission. Body carries the mission id the OPERATOR believes is active, so Scout
    can fail closed with MISSION_ID_MISMATCH rather than starting the wrong route; Scout treats
    the body as optional, so an absent id is sent as an empty body rather than a guess.

    The ONLY route with its own read budget (START_READ_TIMEOUT): Scout's Start is a bounded
    multi-phase transaction and the operator must not give up inside Scout's own bound. Every
    other operation keeps the shared short write budget."""
    body = {"mission_id": mission_id} if mission_id else {}
    return _op("start", base, body, read_timeout=START_READ_TIMEOUT)


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
    """SAFE ABORT of the mission run — Scout's own first-class lifecycle transaction.

    ONE POST. Scout holds the vehicle in a verified LOITER, verifies the active mission
    identity, restores the immutable original mission if a verified revised route is installed,
    rewinds the original to its start, verifies the rewind, resets mission-execution / replan /
    test state, clears the simulated experiment injection, invalidates the prior runtime Home,
    returns supervisory authority to OPERATOR and re-proves the mission evidence.

    The Operator reimplements NONE of that. It does not send LOITER, does not upload the
    original mission, does not rewind the sequence, does not reset the replan controller, does
    not clear the experiment and does not write authority — the whole transaction is Scout's,
    and the station's job is to forward the intent and preserve the evidence.

    The mission id the OPERATOR believes is active is sent so Scout can fail closed on a
    mismatch rather than aborting a run the operator did not mean.

    This is NOT the legacy raw Pixhawk /nav/stop, and Stop is NOT Rearm: Rearm prepares the
    controller for another run, issues no vehicle command and verifies no hold."""
    body = {"mission_id": mission_id} if mission_id else {}
    return _op("stop", base, body)


def post_rearm(base):
    """Rearm the Local Agent's mission-execution controller from a terminal state. Issues NO
    vehicle command, does NOT change vehicle mode, does NOT clear the Pixhawk mission and does
    NOT re-upload the original mission — it only prepares the controller for another explicitly
    prepared run."""
    return _op("rearm", base, {})


# ── Read-only binding reproof (Scout's FINAL contract) ─────────────────────────────────────
# POST /agent/mission_execution/reprove_binding closes the one gap a pure GET refresh cannot:
# Scout's `binding_state`/`verified_route_hash` are Scout's own internal accounting (see
# `summarize_status` / `binding_view` in mission_lifecycle.py) and nothing in this station writes
# them — only a mission-publish package POST, or a LIVE mission execution binding its identity,
# has ever given Scout a reason to move them.
#
# THE SEMANTIC CORRECTION THIS CONTRACT MADE PRECISE — read this before touching binding_state
# anywhere in this station: `binding_state == BOUND` means a LIVE mission execution currently
# owns/binds the original mission identity. It is NOT "the route is proven" and it is NOT a
# precondition for Start. A completely healthy, verified, READY mission BEFORE Start correctly
# reports `binding_state == UNBOUND` while `verified_route_hash`, `state == READY`,
# `start_eligible` and `can_start` all say the route is proven and the mission may run. Binding
# only becomes meaningful — and BOUND — once a run is actually RUNNING and owns that identity.
# A station that reads idle UNBOUND as a failure, or requires BOUND for Start, is reproducing
# exactly the bug this contract exists to correct.
#
# Scout re-evaluates its OWN binding from ITS current package plus a fresh Pixhawk proof and,
# only if that proof is conclusive, restores `verified_route_hash` from it — issuing NO vehicle
# command of any kind (no LOITER, no mission write, no mode change). It reports one of the
# outcomes in REPROVE_OUTCOMES below, verbatim, in the response body's `outcome` field; see
# `interpret_reprove_binding` for how this station narrows that into REPROVE_SUCCESS_OUTCOMES /
# REPROVE_INCONCLUSIVE_OUTCOMES and mission_full_refresh.py for the full contract this station
# expects of it (current package usable + package route hash valid + current Pixhawk route hash
# proven + expected identities match + lifecycle state allows binding).
#
# An older/unmodified Scout 404s this route, which the shared transport already reports as
# `outcome="unsupported"` — the SAME honest "an older Scout" handling every other route in this
# module gets, never a fabricated success. Full Refresh treats that as "no read-only reproof is
# available" and reports Scout's CURRENT binding_state exactly as
# GET /agent/mission_execution/status shows it — it never sets binding_state=BOUND itself.

# Scout's own reprove-outcome vocabulary, verbatim (task Section 3). BUSY is carried on the
# HTTP transport as a definite 409 refusal; every other outcome arrives in the response body.
REPROVE_REPROVED = "REPROVED"
REPROVE_ALREADY_PROVEN = "ALREADY_PROVEN"
REPROVE_EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
REPROVE_PACKAGE_MISMATCH = "PACKAGE_MISMATCH"
REPROVE_PIXHAWK_MISMATCH = "PIXHAWK_MISMATCH"
REPROVE_MISSION_ID_MISMATCH = "MISSION_ID_MISMATCH"
REPROVE_LIFECYCLE_NOT_REPROVABLE = "LIFECYCLE_NOT_REPROVABLE"
REPROVE_NO_CURRENT_PACKAGE = "NO_CURRENT_PACKAGE"
REPROVE_NO_CURRENT_MISSION = "NO_CURRENT_MISSION"
REPROVE_BUSY = "BUSY"
REPROVE_INTERNAL_ERROR = "INTERNAL_ERROR"
REPROVE_OUTCOMES = frozenset({
    REPROVE_REPROVED, REPROVE_ALREADY_PROVEN, REPROVE_EVIDENCE_UNAVAILABLE,
    REPROVE_PACKAGE_MISMATCH, REPROVE_PIXHAWK_MISMATCH, REPROVE_MISSION_ID_MISMATCH,
    REPROVE_LIFECYCLE_NOT_REPROVABLE, REPROVE_NO_CURRENT_PACKAGE, REPROVE_NO_CURRENT_MISSION,
    REPROVE_BUSY, REPROVE_INTERNAL_ERROR,
})
# REPROVED and ALREADY_PROVEN are the two outcomes Full Refresh continues to final readiness
# evaluation from (task Section 10) — a genuinely re-proved route, or an idempotent no-op
# re-affirmation of proof that was already current. Neither one implies or requires BOUND.
REPROVE_SUCCESS_OUTCOMES = frozenset({REPROVE_REPROVED, REPROVE_ALREADY_PROVEN})
# Outcomes that mean Scout could not reach a conclusive verdict AT ALL this round — missing
# evidence, contention, or an internal fault — as distinct from a verdict Scout DID reach and
# reported (a mismatch, or a lifecycle state that cannot be re-proved). A conclusive mismatch is
# never re-labelled EVIDENCE_UNAVAILABLE, and an inconclusive round is never re-labelled a
# mismatch — see task Section 10.
REPROVE_INCONCLUSIVE_OUTCOMES = frozenset({
    REPROVE_EVIDENCE_UNAVAILABLE, REPROVE_NO_CURRENT_PACKAGE, REPROVE_NO_CURRENT_MISSION,
    REPROVE_BUSY, REPROVE_INTERNAL_ERROR,
})
# Outcomes that are a DEFINITE mismatch Scout itself proved — fail-closed, and reported
# explicitly (never silently repaired, never weakened). PACKAGE_MISMATCH is additionally
# reclassified by the Operator's own three-way reconciliation before it reaches the operator
# (task Section 9) — Scout only sees package vs Pixhawk, not the approved/Pixhawk/package triple.
REPROVE_DEFINITE_MISMATCH_OUTCOMES = frozenset({
    REPROVE_PACKAGE_MISMATCH, REPROVE_PIXHAWK_MISMATCH, REPROVE_MISSION_ID_MISMATCH,
})


def post_reprove_binding(base, mission_id=None):
    """Ask Scout to RE-EVALUATE (never fabricate) mission-execution binding from its current
    package and a fresh Pixhawk proof, with NO vehicle command of any kind. `mission_id` is the
    operator's active persisted mission, sent as an EXPECTED identity constraint only — Scout
    independently proves current state and does not treat this id as proof.

    Returns the normal scout_replan.write() three-outcome transport result (`scout` carries
    Scout's body verbatim). `unsupported` (404) means this Scout has not implemented the route
    yet; the caller must treat that exactly like any other older-Scout gap and fall back to
    observing binding_state as Scout currently reports it, never inventing BOUND locally. Pass
    the raw result to `interpret_reprove_binding` to narrow it into Scout's own outcome word."""
    body = {"mission_id": mission_id} if mission_id else {}
    return _op("reprove_binding", base, body)


def interpret_reprove_binding(result):
    """Narrow a transport result from `post_reprove_binding` into Scout's own reprove-outcome
    vocabulary (REPROVE_OUTCOMES) — never fabricated, never rounded up. Adds to the transport
    result:

      reprove_outcome     one of REPROVE_OUTCOMES, Scout's own word from the response body, or
                          None when Scout never told us one (unsupported / unreachable / a
                          transport failure whose verdict never arrived).
      reprove_supported   False only for a definite 404 (an older Scout) — every other gap is
                          reported through reprove_outcome / the transport outcome instead, never
                          conflated with "this Scout does not implement the route".
      reprove_success     True for REPROVED / ALREADY_PROVEN (REPROVE_SUCCESS_OUTCOMES).
      reprove_inconclusive True when Scout could not reach a verdict this round at all
                          (REPROVE_INCONCLUSIVE_OUTCOMES) — missing evidence, contention, or an
                          internal fault, distinct from a mismatch Scout DID prove.
      reprove_fail_closed True for a definite mismatch Scout itself proved
                          (REPROVE_DEFINITE_MISMATCH_OUTCOMES).
    """
    out = dict(result or {})
    body = _body(out)
    transport = out.get("outcome")
    raw = _str_or_none(body.get("outcome"))
    reprove_outcome = raw.upper() if raw and raw.upper() in REPROVE_OUTCOMES else None
    # BUSY is a definite 409 refusal on the transport (task Section 3): the HTTP status is the
    # authoritative signal for it, whether or not Scout also echoes it in the body.
    if out.get("http_status") == 409:
        reprove_outcome = REPROVE_BUSY
    out.update({
        "reprove_outcome": reprove_outcome,
        "reprove_supported": transport != scout_replan.OUTCOME_UNSUPPORTED,
        "reprove_success": reprove_outcome in REPROVE_SUCCESS_OUTCOMES,
        "reprove_inconclusive": reprove_outcome in REPROVE_INCONCLUSIVE_OUTCOMES,
        "reprove_fail_closed": reprove_outcome in REPROVE_DEFINITE_MISMATCH_OUTCOMES,
    })
    return out


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
        # Scout's Stop evidence, when the operation body carries it. Preserved as its own field
        # so the write trace and the UI read the SAME structure whether it arrived on the
        # operation response or on the next canonical status.
        "stop": stop_evidence(body),
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
    binding = body.get("binding") if isinstance(body.get("binding"), dict) else {}
    conflict = (body.get("package_conflict")
                if isinstance(body.get("package_conflict"), dict) else {})
    batt = (body.get("battery_diagnostics")
            if isinstance(body.get("battery_diagnostics"), dict) else {})
    nrg = (body.get("energy_feasibility")
           if isinstance(body.get("energy_feasibility"), dict) else {})
    rsk = body.get("risk") if isinstance(body.get("risk"), dict) else {}
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
        # Scout's Stop evidence block, verbatim (see stop_evidence). It is what makes a
        # successful Stop provable — hold verified, original restored, rewind verified, state
        # reset, authority back with the OPERATOR — instead of inferred from a resting state.
        "stop": stop_evidence(body),
        "stop_reported": isinstance(body.get("stop"), dict) and bool(body.get("stop")),
        "stop_outcome": stop_evidence(body)["outcome"],
        # Scout's explicit eligibility contract. PRESENCE is what makes it authoritative: a Scout
        # that predates the contract omits the keys entirely, and `None` here is what makes the
        # eligibility rule fall back to the older `can_start` reading rather than treat a missing
        # field as `false` and refuse every Start.
        "start_eligible": body.get("start_eligible"),
        "execution_ready": body.get("execution_ready"),
        "authority_blocks_start": body.get("authority_blocks_start"),
        "start_block_reason": _str_or_none(body.get("start_block_reason")),
        "eligibility_reported": "start_eligible" in body,
        # The mission/package binding, verbatim. `binding_state` is Scout's word; the operator
        # compares it, never recomputes it.
        "binding": dict(binding) if binding else None,
        "binding_state": _str_or_none(binding.get("binding_state")) if binding else None,
        "bound_original_mission_id": _str_or_none(binding.get("bound_original_mission_id"))
                                     if binding else None,
        "package_mission_id": _str_or_none(binding.get("package_mission_id")) if binding else None,
        "package_route_hash": _str_or_none(binding.get("package_route_hash")) if binding else None,
        "verified_route_hash": _str_or_none(binding.get("verified_route_hash")) if binding else None,
        "package_conflict": dict(conflict) if conflict else None,
        "package_conflict_code": _str_or_none(conflict.get("code")) if conflict else None,
        # Battery, as Scout DIAGNOSES it. `battery_valid:false` (or a -1 raw) is "unknown", and
        # the operator must never render it as 0% — see main._battery_view / the UI note.
        "battery_diagnostics": dict(batt) if batt else None,
        "battery_percent": batt.get("battery_percent") if batt else None,
        "battery_valid": batt.get("battery_valid") if batt else None,
        # ── Scout's two AUTHORITATIVE assessment blocks, carried VERBATIM ──────────────────
        # Both are Scout's alone: it owns the battery/range/reserve model behind
        # `energy_feasibility` and the weighting, severity floors and hard overrides behind
        # `risk`. This summary re-derives NEITHER — it carries the dicts through and lifts only
        # the two GOVERNING verdicts by name, so the operator's logs and traces can say what
        # Scout said without a second policy representation growing here.
        #
        # `risk_level` is Scout's `risk.level` and ONLY that. It is NOT `weighted_level` and it
        # is NOT derived from `score`: Scout's governing level is the weighted level raised by
        # any non-compensatory component floor and then by any hard-feasibility override, so a
        # weighted LOW under a HIGH component floor governs as HIGH. Reading the score here
        # would report LOW for a vehicle Scout has assessed as HIGH.
        "energy_feasibility": dict(nrg) if nrg else None,
        "energy_mission_feasible": nrg.get("mission_feasible") if nrg else None,
        "energy_rtl_return_feasible": nrg.get("rtl_return_feasible") if nrg else None,
        "risk": dict(rsk) if rsk else None,
        "risk_level": _str_or_none(rsk.get("level")) if rsk else None,
        "risk_recommendation": _str_or_none(rsk.get("recommendation")) if rsk else None,
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
            "can_resume", "can_stop", "stop_supported", "stop", "stop_outcome",
            "start_eligible", "authority_blocks_start", "authority_status", "last_error")},
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
        # A SUCCESSFUL Stop normally comes to rest in NOT_READY (authority is deliberately back
        # with the OPERATOR), NOT in STOPPED — so a reconciling read must resolve from Scout's
        # own `stop` evidence rather than from the resting state alone. STOPPED / CANCELLED are
        # handled above and still resolve to "stopped"; SUSPENDED is the documented FAILURE
        # landing and is resolved by the terminal branch above with Scout's own last_error.
        ev = summary.get("stop") or {}
        if ev.get("reported") and (ev.get("ready_for_start") is True
                                   or _is_success_outcome(ev.get("outcome"))):
            out["resolved"] = "stopped"
            out["detail"] = (f"Scout reports {state} with stop evidence "
                             f"{ev.get('outcome') or 'ready for a new start'}"
                             + (f" in mode {summary['mode']}" if summary["mode"] else ""))
        elif state in ("RUNNING", "RETURNING_HOME", "HOME_ARRIVAL_PENDING"):
            out["resolved"] = "running"
            out["detail"] = f"Scout is still {state} — the stop did not take effect"
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


def _is_success_outcome(outcome):
    """Whether Scout's `stop.outcome` word names a COMPLETED stop. Scout's exact vocabulary is
    its own, so this matches the words it is documented to use and treats anything else as
    undecided — never as a failure, and never as a success."""
    o = (outcome or "").strip().upper()
    return o in {"STOPPED", "COMPLETE", "COMPLETED", "SUCCESS", "SUCCEEDED", "OK"}


def _seq_note(summary):
    seq = summary.get("sequence") or {}
    cur, count = seq.get("current"), seq.get("count")
    if cur is None and count is None:
        return ""
    return f" at sequence {cur if cur is not None else '?'}/{count if count is not None else '?'}"
