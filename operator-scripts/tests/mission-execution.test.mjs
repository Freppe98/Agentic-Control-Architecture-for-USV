// Unit tests for the mission-execution lifecycle logic layer (operator/lib/mission-execution.js)
// and source guards for the Agent/Map/Vehicle wiring. Run: `node --test tests/` (or `npm test`).
//
// Two kinds, matching the rest of the suite:
//   (1) pure-logic tests over lib/mission-execution.js (no DOM, no fetch) — every Scout state, the
//       REPLANNING overlay, action derivation from status ALONE, transitional labels, Rearm
//       availability, the completion rule, continuation true/false/null, and the operation-result
//       interpretation (200-with-error, 409, unknown, older Scout);
//   (2) source guards (readFileSync) for the page wiring that has no DOM harness here — the button
//       must not flip on click, Pause must be called Pause, and no page may implement Start as its
//       own LOITER → Set Home → AUTO sequence.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  STATES, EFFECTIVE_REPLANNING, TRANSITIONAL_STATES, REARMABLE_STATES, TRANSITION_LABELS,
  ERROR_TEXT, OUTCOME, isTransitional, isRearmable, isUnknownState, stateLabel, errorText,
  normalizeStatus, isStatusBody, primaryAction, rearmAvailability, startBlockers, isComplete,
  continuationView, returnProgress, interpretOperation, outcomeLabel, operationSummary,
  START_HOME_NOTE,
} from "../operator/lib/mission-execution.js";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

/** A canonical Scout status body, in the api.js envelope shape. */
const envelope = (over = {}) => ({
  supported: true, reachable: true, outcome: "accepted",
  scout: {
    supported: true,
    state: "READY", effective_state: "READY", active_operation_id: null,
    mission_id: "msn-0001",
    original_route_hash: "sha256:aaa", active_route_hash: "sha256:aaa",
    verified_home: { latitude: 56, longitude: 12 }, home_verification_distance_m: 0.4,
    mode: "LOITER",
    sequence: { current: 0, count: 10, before_pause: null, at_resume: null,
      first_after_resume: null, continuation_verified: null },
    timestamps: { start: null, pause: null, resume: null },
    replanning: { active: false, fsm_state: "MONITORING" },
    return_completion: { distance_to_home_m: null, arrival_radius_m: 7.5, persistence_s: 4,
      persistence_progress_s: 0, arrival_confirmed: false, final_loiter_verified: false },
    authority_status: "LOCAL_AGENT",
    can_start: true, can_pause: false, can_resume: false,
    mission_execution_enabled: true, config: {}, last_error: null, history: [],
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));

// ── A. Every Scout state is known, and the overlay is not one of them ───────────────────
test("every Scout mission-execution state, including the whole STOP sequence, is known", () => {
  // A state the build does not recognize is displayed raw and flagged. That is the right answer
  // for a state we have never heard of and the WRONG one for a step of Scout's own stop
  // transaction, which the operator must be able to read as progress.
  for (const s of ["NOT_READY", "NOT_STARTED", "READY", "START_REQUESTED",
    "ARMING", "VERIFYING_ARMED",
    "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED", "SETTING_HOME", "VERIFYING_HOME",
    "SYNCHRONIZING_PACKAGE", "STARTING_AUTO", "CONFIRMING_PROGRESSION",
    "RUNNING", "PAUSE_REQUESTED", "PAUSED",
    "RESUME_REQUESTED", "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
    "STOP_VERIFYING_MISSION", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
    "STOP_VERIFYING_REWIND", "STOP_RESETTING", "STOP_VERIFYING_RESET",
    "STOPPED", "CANCELLED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED",
    "COMPLETED_HOLD", "SUSPENDED", "FAILED"]) {
    assert.ok(STATES.includes(s), s);
    assert.equal(isUnknownState(s), false, s);
    assert.notEqual(stateLabel(s), "Unknown", s);
  }
  assert.equal(STATES.length, 34);
});

test("REPLANNING is an effective-state OVERLAY, never a stored state", () => {
  assert.equal(STATES.includes(EFFECTIVE_REPLANNING), false);
  const s = S({ state: "RUNNING", effective_state: "REPLANNING",
    replanning: { active: true, fsm_state: "PLANNING" } });
  assert.equal(s.state, "RUNNING");            // Scout's STORED state is untouched
  assert.equal(s.effectiveState, "REPLANNING");
  assert.equal(s.replanning.active, true);
});

test("a state this build does not know is flagged, not silently bucketed", () => {
  assert.equal(isUnknownState("SOME_FUTURE_STATE"), true);
  assert.equal(S({ state: "SOME_FUTURE_STATE" }).unknownState, true);
  assert.equal(stateLabel("SOME_FUTURE_STATE"), "SOME_FUTURE_STATE"); // shown as-is
});

// ── B. Transitional states and their labels ─────────────────────────────────────────────
test("every transitional state has the mandated progress label", () => {
  const expected = {
    START_REQUESTED: "Preparing start…",
    ARMING: "Arming…",
    VERIFYING_ARMED: "Verifying armed…",
    START_HOLD_REQUESTED: "Requesting launch hold…",
    START_HOLD_CONFIRMED: "Launch hold verified",
    SETTING_HOME: "Setting launch Home…",
    VERIFYING_HOME: "Verifying Home…",
    SYNCHRONIZING_PACKAGE: "Synchronizing mission package…",
    STARTING_AUTO: "Starting AUTO…",
    CONFIRMING_PROGRESSION: "Confirming mission progression…",
    PAUSE_REQUESTED: "Pausing mission…",
    RESUME_REQUESTED: "Resuming mission…",
    RETURNING_HOME: "Returning to Home",
    HOME_ARRIVAL_PENDING: "Confirming Home arrival…",
    FINAL_HOLD_REQUESTED: "Requesting final hold…",
  };
  for (const [state, label] of Object.entries(expected)) {
    assert.equal(TRANSITION_LABELS[state], label, state);
    assert.equal(stateLabel(state), label, state);
  }
});

test("the start/pause/resume/return steps are transitional; resting states are not", () => {
  for (const s of TRANSITIONAL_STATES) assert.equal(isTransitional(s), true, s);
  for (const s of ["READY", "RUNNING", "PAUSED", "COMPLETED_HOLD", "SUSPENDED", "FAILED",
    "NOT_READY"]) assert.equal(isTransitional(s), false, s);
  // RETURNING_HOME is a real, long-lived phase — it is transitional but NOT completion.
  assert.equal(isTransitional("RETURNING_HOME"), true);
});

// ── C. The primary action is derived from STATUS, never from the last click ─────────────
test("can_start alone yields an enabled Start Mission", () => {
  const a = primaryAction(S({ state: "READY", can_start: true }));
  assert.deepEqual([a.action, a.label, a.enabled], ["start", "Start Mission", true]);
});

test("can_pause yields Pause Mission — never 'Stop Mission'", () => {
  const a = primaryAction(S({ state: "RUNNING", can_start: false, can_pause: true }));
  assert.deepEqual([a.action, a.label, a.enabled], ["pause", "Pause Mission", true]);
  assert.doesNotMatch(a.label, /stop/i);
});

test("can_resume yields Resume Mission", () => {
  const a = primaryAction(S({ state: "PAUSED", can_start: false, can_resume: true }));
  assert.deepEqual([a.action, a.label, a.enabled], ["resume", "Resume Mission", true]);
});

test("an inconsistent Scout can never be offered Start on a pausable vehicle", () => {
  // can_start and can_pause are mutually exclusive on Scout; if both ever arrived, the safe
  // answer is the running one — Start must not appear for a mission Scout says is running.
  const a = primaryAction(S({ state: "RUNNING", can_start: true, can_pause: true }));
  assert.equal(a.action, "pause");
});

test("effective_state REPLANNING disables every action and names the owner", () => {
  const a = primaryAction(S({ state: "RUNNING", can_pause: true, effective_state: "REPLANNING",
    replanning: { active: true, fsm_state: "VALIDATING" } }));
  assert.equal(a.action, null);
  assert.equal(a.enabled, false);
  assert.equal(a.label, "Agent Replanning…");
  assert.match(a.reason, /replanning controller owns the vehicle/i);
  assert.match(a.reason, /VALIDATING/);
});

test("replanning.active alone (without the effective_state) still disables the control", () => {
  const a = primaryAction(S({ state: "RUNNING", can_pause: true,
    replanning: { active: true, fsm_state: "PLANNING" } }));
  assert.equal(a.enabled, false);
  assert.equal(a.label, "Agent Replanning…");
});

test("an active_operation_id disables the control even when a can_* flag is set", () => {
  const a = primaryAction(S({ state: "RUNNING", can_pause: true, active_operation_id: "op-9" }));
  assert.equal(a.enabled, false);
  assert.equal(a.action, null);
  assert.match(a.reason, /op-9/);
});

test("each transitional state disables the control and shows its own progress label", () => {
  for (const s of TRANSITIONAL_STATES) {
    const a = primaryAction(S({ state: s, can_start: false, can_pause: false, can_resume: false }));
    assert.equal(a.enabled, false, s);
    assert.equal(a.label, TRANSITION_LABELS[s], s);
  }
});

test("COMPLETED_HOLD is a disabled success state, not another action", () => {
  const a = primaryAction(S({ state: "COMPLETED_HOLD", can_start: false }));
  assert.equal(a.action, null);
  assert.equal(a.enabled, false);
  assert.match(a.reason, /rearm/i);
});

test("SUSPENDED and FAILED disable the control and surface Scout's last error", () => {
  for (const s of ["SUSPENDED", "FAILED"]) {
    const a = primaryAction(S({ state: s, can_start: false, last_error: "AUTO_NOT_VERIFIED" }));
    assert.equal(a.enabled, false, s);
    assert.equal(a.reason, "AUTO_NOT_VERIFIED", s);
  }
});

test("an older Scout offers no action and says so plainly", () => {
  const a = primaryAction(normalizeStatus({ supported: false, reachable: true }));
  assert.equal(a.action, null);
  assert.equal(a.enabled, false);
  assert.match(a.reason, /not supported by this Scout version/i);
});

test("an unreachable Scout offers no action and fabricates no state", () => {
  const a = primaryAction(normalizeStatus({ supported: true, reachable: false }));
  assert.equal(a.enabled, false);
  assert.match(a.reason, /unavailable/i);
  const s = normalizeStatus({ supported: true, reachable: false });
  assert.equal(s.state, null);
  assert.equal(s.canStart, false);
  assert.equal(s.returnCompletion.finalLoiterVerified, null);
});

test("the derivation reads ONLY status — the same status always gives the same button", () => {
  const st = S({ state: "RUNNING", can_start: false, can_pause: true });
  const a = primaryAction(st), b = primaryAction(st), c = primaryAction(st);
  assert.deepEqual(a, b);
  assert.deepEqual(b, c);
  // …and a status that still says READY yields Start, no matter what was clicked before.
  assert.equal(primaryAction(S({ state: "READY", can_start: true })).label, "Start Mission");
});

// ── D. Rearm availability ───────────────────────────────────────────────────────────────
test("Rearm is offered only from COMPLETED_HOLD / SUSPENDED / FAILED", () => {
  for (const s of REARMABLE_STATES) {
    assert.equal(isRearmable(s), true, s);
    assert.equal(rearmAvailability(S({ state: s, can_start: false })).available, true, s);
  }
  for (const s of ["READY", "RUNNING", "PAUSED", "RETURNING_HOME", "NOT_READY"]) {
    assert.equal(rearmAvailability(S({ state: s })).available, false, s);
  }
});

test("Rearm is shown but disabled while an operation or replanning is in flight", () => {
  const busy = rearmAvailability(S({ state: "FAILED", active_operation_id: "op-2" }));
  assert.deepEqual([busy.available, busy.enabled], [true, false]);
  const rp = rearmAvailability(S({ state: "SUSPENDED",
    replanning: { active: true, fsm_state: "PLANNING" } }));
  assert.deepEqual([rp.available, rp.enabled], [true, false]);
});

test("an older Scout never offers Rearm", () => {
  assert.equal(rearmAvailability(normalizeStatus({ supported: false })).available, false);
});

// ── E. Why Start is unavailable — Scout's words, not re-derived preconditions ───────────
test("start blockers report Scout's own status fields", () => {
  const b = startBlockers(S({ state: "NOT_READY", can_start: false,
    mission_execution_enabled: false, authority_status: "OPERATOR",
    last_error: "NO_PLANNING_PACKAGE" }));
  assert.ok(b.some((x) => /MISSION_EXECUTION_DISABLED/.test(x)));
  assert.ok(b.some((x) => /NOT_READY/.test(x)));
  assert.ok(b.some((x) => /NO_PLANNING_PACKAGE/.test(x)));
});

test("OPERATOR authority is NOT a start blocker — the Start transaction resolves it", () => {
  // Mission execution does need LOCAL_AGENT, but the Start transaction acquires and verifies it
  // as its first phase. Listing it as a blocker told the operator to go and press Release
  // Control by hand, which is exactly the manual authority management this station removed.
  const b = startBlockers(S({ state: "NOT_READY", can_start: false,
    authority_status: "OPERATOR" }));
  assert.equal(b.some((x) => /Release Control/i.test(x)), false, b);
  assert.equal(b.some((x) => /authority OPERATOR/.test(x)), false, b);
});

test("a STRUCTURED last_error renders as text, never as [object Object]", () => {
  const b = startBlockers(S({ state: "NOT_READY", can_start: false,
    last_error: { code: "NO_PLANNING_PACKAGE", message: "no package stored" } }));
  const line = b.find((x) => /Scout last error/.test(x));
  assert.ok(line, b);
  assert.doesNotMatch(line, /\[object Object\]/);
  assert.match(line, /NO_PLANNING_PACKAGE/);
  assert.match(line, /no package stored/);
});

test("replanning and an active operation are reported as start blockers", () => {
  const b = startBlockers(S({ state: "RUNNING", can_start: false, active_operation_id: "op-3",
    replanning: { active: true, fsm_state: "PLANNING" } }));
  assert.ok(b.some((x) => /Replanning is active/i.test(x)));
  assert.ok(b.some((x) => /op-3/.test(x)));
});

test("the error code from a failed attempt is shown verbatim with its explanation", () => {
  const b = startBlockers(S({ state: "READY", can_start: false }),
    { lastErrorCode: "PACKAGE_INCONSISTENT_AFTER_SYNC" });
  const line = b.find((x) => /PACKAGE_INCONSISTENT_AFTER_SYNC/.test(x));
  assert.ok(line, b);
  assert.match(line, /inconsistent after synchronization/i);
});

test("every documented Scout error code has readable text and keeps its code", () => {
  for (const code of ["NO_ACTIVE_MISSION", "NO_PLANNING_PACKAGE", "MISSION_ID_MISMATCH",
    "POSITION_STALE_OR_INVALID", "PIXHAWK_STATE_UNAVAILABLE", "AUTHORITY_LOST",
    "LOITER_NOT_VERIFIED", "SET_HOME_FAILED", "PACKAGE_SYNC_FAILED",
    "PACKAGE_INCONSISTENT_AFTER_SYNC", "AUTO_NOT_VERIFIED", "PROGRESSION_UNCONFIRMED",
    "MISSION_EXECUTION_DISABLED", "REPLANNING_ACTIVE", "ARBITRATION_BUSY"]) {
    assert.ok(ERROR_TEXT[code], code);
    assert.equal(typeof errorText(code), "string", code);
  }
  // An unrecognised code is returned unchanged, never dropped or replaced by a guess.
  assert.equal(errorText("SOME_NEW_CODE"), "SOME_NEW_CODE");
});

test("no start blockers are invented when Scout says it can start", () => {
  assert.deepEqual(startBlockers(S({ state: "READY", can_start: true })), []);
});

// ── F. Completion requires BOTH the state and a verified final LOITER ───────────────────
test("completion needs COMPLETED_HOLD *and* final_loiter_verified", () => {
  const rc = (o) => ({ distance_to_home_m: 1, arrival_radius_m: 7.5, persistence_s: 4,
    persistence_progress_s: 4, arrival_confirmed: true, final_loiter_verified: false, ...o });
  assert.equal(isComplete(S({ state: "COMPLETED_HOLD",
    return_completion: rc({ final_loiter_verified: true }) })), true);
  assert.equal(isComplete(S({ state: "COMPLETED_HOLD", return_completion: rc() })), false);
  assert.equal(isComplete(S({ state: "FINAL_HOLD_REQUESTED",
    return_completion: rc({ final_loiter_verified: true }) })), false);
});

test("arrival, a full persistence bar and RETURNING_HOME are NOT completion", () => {
  const s = S({ state: "RETURNING_HOME", return_completion: {
    distance_to_home_m: 0.5, arrival_radius_m: 7.5, persistence_s: 4,
    persistence_progress_s: 4, arrival_confirmed: true, final_loiter_verified: false } });
  assert.equal(isComplete(s), false);
  const p = returnProgress(s);
  assert.equal(p.fraction, 1);                 // the bar is full…
  assert.equal(p.finalLoiterVerified, false);  // …and the mission is still not complete
});

test("return progress is a display of Scout's counters, clamped, never an arrival decision", () => {
  assert.equal(returnProgress(S({})).fraction, 0);
  assert.equal(returnProgress(S({ return_completion: { persistence_s: 4,
    persistence_progress_s: 9 } })).fraction, 1);
  // Scout reporting no return_completion at all yields null, not a fabricated 0%.
  assert.equal(returnProgress(normalizeStatus({ supported: false })), null);
});

// ── G. Continuation: true / false / null ────────────────────────────────────────────────
test("continuation true is a positive status", () => {
  const c = continuationView(S({ sequence: { current: 5, count: 10, before_pause: 4,
    at_resume: 4, first_after_resume: 5, continuation_verified: true } }));
  assert.equal(c.state, "verified");
  assert.equal(c.warning, false);
});

test("continuation false is a prominent warning, even with verified AUTO", () => {
  const c = continuationView(S({ state: "RUNNING", mode: "AUTO",
    sequence: { current: 0, count: 10, before_pause: 4, at_resume: 4,
      first_after_resume: 0, continuation_verified: false } }));
  assert.equal(c.state, "not_verified");
  assert.equal(c.warning, true);
  assert.match(c.message, /AUTO resumed, but continuation from the paused waypoint was not verified/);
  assert.match(c.message, /waypoint 0/);
  // …and it is never phrased as an unqualified success.
  assert.doesNotMatch(c.message, /resumed successfully/i);
});

test("continuation null is 'not tested', not a pass and not a failure", () => {
  const c = continuationView(S({}));
  assert.equal(c.state, "unavailable");
  assert.equal(c.warning, false);
});

test("the sequence block preserves every field Scout reports", () => {
  const q = S({ sequence: { current: 5, count: 10, before_pause: 4, at_resume: 4,
    first_after_resume: 5, continuation_verified: true } }).sequence;
  assert.deepEqual(
    [q.current, q.count, q.beforePause, q.atResume, q.firstAfterResume, q.continuationVerified],
    [5, 10, 4, 4, 5, true]);
});

// ── H. Operation-result interpretation ──────────────────────────────────────────────────
test("a clean 200 is accepted", () => {
  const v = interpretOperation({ ok: true, status: 200, data: { scout: {
    accepted: true, operation_id: "op-1", current_state: "RUNNING", verified_mode: "AUTO",
    error: null, final: true } } });
  assert.equal(v.outcome, OUTCOME.ACCEPTED);
  assert.equal(v.resultingState, "RUNNING");
  assert.equal(v.verifiedMode, "AUTO");
});

test("HTTP 200 carrying body.error is a FAILURE, with Scout's exact code", () => {
  const v = interpretOperation({ ok: true, status: 200, data: {
    scout_error_code: "AUTO_NOT_VERIFIED",
    scout: { accepted: false, error: "AUTO_NOT_VERIFIED", current_state: "FAILED" } } });
  assert.equal(v.outcome, OUTCOME.FAILED);
  assert.equal(v.code, "AUTO_NOT_VERIFIED");
  assert.notEqual(v.outcome, OUTCOME.ACCEPTED);
  assert.match(operationSummary(v), /AUTO could not be verified/);
});

test("HTTP 200 with accepted:false and no code is still a failure", () => {
  const v = interpretOperation({ ok: true, status: 200, data: { scout: { accepted: false } } });
  assert.equal(v.outcome, OUTCOME.FAILED);
});

test("HTTP 409 is a rejection, not a network fault", () => {
  const v = interpretOperation({ ok: false, status: 409, data: {
    scout_error_code: "ARBITRATION_BUSY", scout: { accepted: false, error: "ARBITRATION_BUSY" } } });
  assert.equal(v.outcome, OUTCOME.REJECTED);
  assert.equal(v.code, "ARBITRATION_BUSY");
  assert.match(outcomeLabel(v.outcome), /Rejected/);
});

test("HTTP 202 is unknown and carries the backend's reconciliation verdict", () => {
  const v = interpretOperation({ ok: false, status: 202, data: {
    reconciliation: { resolved: "running", detail: "Scout reports RUNNING in mode AUTO" } } });
  assert.equal(v.outcome, OUTCOME.UNKNOWN);
  assert.match(outcomeLabel(v.outcome), /Unknown/);
  assert.doesNotMatch(outcomeLabel(v.outcome), /failed/i);   // unknown is NOT failure
  assert.match(operationSummary(v), /reconciled: running/);
});

test("a 5xx is unknown because the write may have landed", () => {
  assert.equal(interpretOperation({ status: 500, data: {} }).outcome, OUTCOME.UNKNOWN);
});

test("HTTP 503 is unavailable and 200+supported:false is an older Scout", () => {
  assert.equal(interpretOperation({ status: 503, data: {} }).outcome, OUTCOME.UNAVAILABLE);
  assert.equal(interpretOperation({ status: 200, data: { supported: false } }).outcome,
    OUTCOME.UNSUPPORTED);
  assert.equal(interpretOperation({ status: 404, data: {} }).outcome, OUTCOME.UNSUPPORTED);
});

test("outcome labels keep failure, rejection and unknown distinct", () => {
  const labels = ["accepted", "failed", "rejected", "unknown", "unavailable", "unsupported"]
    .map(outcomeLabel);
  assert.equal(new Set(labels).size, labels.length);
  assert.match(outcomeLabel("failed"), /Failed on the vehicle/);
});

test("an operation summary shows the code, the message and the resulting state", () => {
  const v = interpretOperation({ status: 200, data: {
    scout_error_code: "SET_HOME_FAILED", scout_error_message: "no ack from Pixhawk",
    scout: { accepted: false, error: "SET_HOME_FAILED", current_state: "FAILED" } } });
  const s = operationSummary(v);
  assert.match(s, /SET_HOME_FAILED/);
  assert.match(s, /no ack from Pixhawk/);
  assert.match(s, /state FAILED/);
});

// ── I. Older Scout compatibility: nothing is fabricated ─────────────────────────────────
test("an unsupported Scout fabricates no READY, can_start, Home, continuation or completion", () => {
  const s = normalizeStatus({ supported: false, reachable: true, scout: {} });
  assert.equal(s.supported, false);
  assert.equal(s.present, false);
  assert.equal(s.state, null);
  assert.equal(s.canStart, false);
  assert.equal(s.home.verified, null);
  assert.equal(s.home.verificationDistanceM, null);
  assert.equal(s.sequence.continuationVerified, null);
  assert.equal(s.returnCompletion.finalLoiterVerified, null);
  assert.equal(isComplete(s), false);
  assert.equal(continuationView(s).state, "unavailable");
});

test("a nested supported:false inside Scout's body is honoured too", () => {
  assert.equal(normalizeStatus({ supported: true, scout: { supported: false } }).supported, false);
});

test("a 200 carrying another endpoint's body is unsupported, not a blank lifecycle", () => {
  // OBSERVED on the deployed Scout: its Local Agent routes with path.startswith("/agent/mission"),
  // so GET /agent/mission_execution/status returns its legacy PIXHAWK MISSION READBACK with 200.
  const legacy = { available: true, mission_count: 15, mission_loaded: true, mission_valid: true,
    current_waypoint: 0, mission_hash: "5606802827", waypoints: [{ latitude: 56.66 }] };
  assert.equal(isStatusBody(legacy), false);
  const s = normalizeStatus({ supported: true, reachable: true, scout: legacy });
  assert.equal(s.supported, false);
  assert.equal(s.present, false);
  assert.equal(s.state, null);
  assert.equal(s.canStart, false);
  assert.equal(isComplete(s), false);
  const a = primaryAction(s);
  assert.equal(a.enabled, false);
  assert.match(a.reason, /not supported by this Scout version/i);
});

test("a genuine status body is identified by its lifecycle keys, null values included", () => {
  assert.equal(isStatusBody({ state: "READY" }), true);
  assert.equal(isStatusBody({ can_start: false }), true);
  assert.equal(isStatusBody({ mission_execution_enabled: true }), true);
  assert.equal(isStatusBody({}), false);
  assert.equal(isStatusBody(null), false);
});

test("the Home note states plainly that Start resets Home to the launch position", () => {
  assert.match(START_HOME_NOTE, /sets the current launch position as Home/);
  assert.match(START_HOME_NOTE, /verifies it/);
  assert.match(START_HOME_NOTE, /synchronizes the planning package/);
  assert.match(START_HOME_NOTE, /originally planned Home is not retained/);
});

// ── J. Source guards: the Agent page's wiring ───────────────────────────────────────────
const agentSrc = read("../operator/pages/Agent.js");
const mapSrc = read("../operator/pages/Map.js");
const vehicleSrc = read("../operator/pages/Vehicle.js");
const apiSrc = read("../operator/services/api.js");

test("the Agent page's fallback controls derive from status, never from the click", () => {
  // The Agent page keeps the lifecycle writes only as a collapsed DIAGNOSTIC fallback, and even
  // there the enablement comes from primaryAction(status) / stopAvailability(status). The click
  // handler dispatches and calls a write — it never assigns a label or a local state.
  assert.match(agentSrc, /mx\.primaryAction\(S\)/);
  assert.match(agentSrc, /mx\.stopAvailability\(S\)/);
  const handler = agentSrc.slice(agentSrc.indexOf("function wireMissionExecution"),
    agentSrc.indexOf("function wireMissionExecution") + 1800);
  assert.doesNotMatch(handler, /\.label\s*=/);
  assert.doesNotMatch(handler, /mxStatus\s*=/);
  assert.doesNotMatch(handler, /textContent\s*=/);
});

test("a lifecycle write never mutates the status it will be judged by", () => {
  const w = agentSrc.slice(agentSrc.indexOf("function mxWrite"),
    agentSrc.indexOf("function mxWrite") + 1200);
  assert.doesNotMatch(w, /mxStatus\s*=\s*[^n]/);        // only loadMissionExecution sets status
  assert.match(w, /loadMissionExecution\(id\)/);        // reconcile by re-reading Scout
});

test("Stop is a real, separate operation — never Pause wearing Stop's label", () => {
  // Pause holds the mission; Stop ends the run. Conflating them is how an operator comes to
  // believe a mission is over when the Local Agent still owns the vehicle.
  assert.match(agentSrc, /"Pause Mission"/);
  assert.match(mapSrc, /Stop Mission/);                 // the Map card offers a real Stop
  assert.match(apiSrc, /export function stopMissionExecution\(/);
  assert.doesNotMatch(vehicleSrc, /Stop Mission/i);     // never a vehicle-page mode command
  // Stop is never implemented as a LOITER command, and Rearm is never routed to it.
  const stopWiring = mapSrc.slice(mapSrc.indexOf("async function onMissionAction"),
    mapSrc.indexOf("async function onMissionAction") + 4200);
  assert.doesNotMatch(stopWiring, /SET_MODE_LOITER/);
  assert.match(stopWiring, /action === "stop"[\s\S]{0,1600}api\.stopMissionExecution\(id\)/);
  assert.match(stopWiring, /action === "rearm"[\s\S]{0,1600}api\.rearmMissionExecution\(id\)/);
});

test("the Agent page implements Start as ONE Scout call, not LOITER → Set Home → AUTO", () => {
  assert.match(agentSrc, /api\.startMissionExecution/);
  // No page-side mode/Home commands anywhere in the mission-execution wiring.
  assert.doesNotMatch(agentSrc, /api\.setHome\(/);
  assert.doesNotMatch(agentSrc, /api\.createCommand\(/);
  assert.doesNotMatch(agentSrc, /SET_MODE_LOITER/);
  assert.doesNotMatch(agentSrc, /SET_MODE_AUTO/);
  assert.doesNotMatch(agentSrc, /"SET_HOME"/);
});

test("the Agent card shows Home verification, sequence evidence and return persistence", () => {
  assert.match(agentSrc, /Requested launch Home/);
  assert.match(agentSrc, /Verified Home/);
  assert.match(agentSrc, /Verification distance/);
  assert.match(agentSrc, /Package Home synchronized/);
  assert.match(agentSrc, /Before pause/);
  assert.match(agentSrc, /First after resume/);
  assert.match(agentSrc, /Arrival persistence/);
  assert.match(agentSrc, /Arrival radius/);
  assert.match(agentSrc, /Final LOITER verified/);
});

test("the Agent card carries the continuation warning and the completion rule verbatim", () => {
  assert.match(agentSrc, /continuation from the paused waypoint was not verified/i);
  assert.match(agentSrc, /waypoint 0/);
  assert.match(agentSrc, /mx\.isComplete\(S\)/);
  assert.match(agentSrc, /final_loiter_verified = true/);
});

test("the Agent card explains that Rearm is not a vehicle reset", () => {
  assert.match(agentSrc, /Rearm Mission Controller/);
  assert.doesNotMatch(agentSrc, /Reset vehicle/i);
  assert.match(agentSrc, /does <b>not<\/b> clear the Pixhawk mission/);
});

test("mission-execution state is isolated per selected vehicle", () => {
  // A fetch is tagged with the vehicle it was for and discarded if the selection moved…
  assert.match(agentSrc, /if \(forId !== selId\) return;/);
  // …the section only renders state fetched for THIS vehicle…
  assert.match(agentSrc, /const forThis = mxForVid != null && v && mxForVid === v\.id;/);
  // …and switching vehicles clears the previous one's lifecycle state immediately.
  assert.match(agentSrc, /mxStatus = null; mxOps = \[\]; mxResult = null; mxForVid = null;/);
});

test("the mission-execution poll is read-only and skipped while a write is in flight", () => {
  assert.match(agentSrc, /if \(!mxBusy\) loadMissionExecution\(selId\)/);
  const loader = agentSrc.slice(agentSrc.indexOf("function loadMissionExecution"),
    agentSrc.indexOf("function loadMissionExecution") + 900);
  for (const w of ["startMissionExecution", "pauseMissionExecution", "resumeMissionExecution",
    "rearmMissionExecution"]) assert.doesNotMatch(loader, new RegExp(w));
});

// ── K. Source guards: exactly ONE lifecycle action path in the station ──────────────────
test("Map and Vehicle no longer render competing MISSION_PAUSE / MISSION_RESUME buttons", () => {
  assert.doesNotMatch(mapSrc, /\["MISSION_PAUSE"/);
  assert.doesNotMatch(mapSrc, /\["MISSION_RESUME"/);
  assert.doesNotMatch(vehicleSrc, /\["MISSION_PAUSE"/);
  assert.doesNotMatch(vehicleSrc, /\["MISSION_RESUME"/);
  assert.match(mapSrc, /const MAP_MISSION = \[\];/);
});

test("the MAP is the normal operational surface for the mission lifecycle", () => {
  // The product decision: normal Start / Pause / Resume / Stop belong on the Map, and the Agent
  // page is a diagnostic surface. The Vehicle page still defers rather than growing a third.
  assert.match(mapSrc, /Agent Mission/);
  // missionCardView is lifecycleControls in its compact operational form — the Map renders the
  // shared model, the Agent page keeps the diagnostic depth.
  assert.match(mapSrc, /missionCardView/);
  assert.match(vehicleSrc, /Mission lifecycle card/);
});

test("manual supervisory mode commands are deliberately KEPT on both pages", () => {
  for (const [name, src] of [["Map", mapSrc], ["Vehicle", vehicleSrc]]) {
    for (const t of ["SET_MODE_AUTO", "SET_MODE_LOITER", "SET_MODE_MANUAL", "RTL"]) {
      assert.match(src, new RegExp(`"${t}"`), `${name}: ${t}`);
    }
  }
});

// ── L. Source guards: the api.js surface ────────────────────────────────────────────────
test("api.js exposes exactly the five mission-execution operations plus the trace", () => {
  for (const fn of ["getMissionExecutionStatus", "startMissionExecution", "pauseMissionExecution",
    "resumeMissionExecution", "rearmMissionExecution", "getMissionExecutionOperations"]) {
    assert.match(apiSrc, new RegExp(`export function ${fn}\\(`), fn);
  }
});

test("every mission-execution call is per-vehicle and hits the operator backend only", () => {
  for (const path of ["/mission-execution/status", "/mission-execution/start",
    "/mission-execution/pause", "/mission-execution/resume", "/mission-execution/rearm"]) {
    assert.match(apiSrc, new RegExp(`/api/vehicles/\\$\\{id\\}${path}`), path);
  }
  // No direct browser-to-Scout access. Every request is a relative operator-backend path
  // (BASE is empty), so nothing in this module can address a Local Agent host or port 8090.
  assert.match(apiSrc, /^const BASE = "";$/m);
  const code = apiSrc.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(code, /https?:\/\//);
  assert.doesNotMatch(code, /8090/);
  assert.doesNotMatch(code, /agent\/mission_execution/);
});

test("the lifecycle does not go through the command queue", () => {
  // Scope to the mission-execution exports: the section's prose legitimately names the queue in
  // order to say the lifecycle does NOT use it, so strip comments before checking the code.
  const section = apiSrc.slice(apiSrc.indexOf("Mission-execution lifecycle"),
    apiSrc.indexOf("Small polling helper"));
  const code = section.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(code, /createCommand/);
  assert.match(code, /startMissionExecution/);        // the slice really does cover the exports
});
