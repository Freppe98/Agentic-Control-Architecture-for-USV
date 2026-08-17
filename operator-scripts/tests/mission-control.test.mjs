// mission-control.test.mjs — the STATE → CONTROL mapping for the Map's Agent Mission card,
// plus the page-surface guards that keep normal mission operation on the Map.
//
// The bench-test defect this pins: normal mission controls were on the diagnostic page, and the
// operator had to manage internal control authority by hand. The product decision is that the
// Map is the operational surface and the Agent page is diagnostics. Two things then have to be
// provably true, forever:
//   1. the control set is derived from SCOUT'S STATE, never from the last click — including the
//      rule that a RUNNING mission's primary button is Pause and NEVER Resume;
//   2. one operation can never be submitted twice.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  lifecycleControls, stopAvailability, pauseAvailability, resumeAvailability, missionCardView,
  normalizeStatus, interpretTransaction, transactionSummary, outcomeLabel, startFailure,
  reconciledStart, stopOutcomeView, stopPhase, stopEvidenceDetail, startGate,
  OUTCOME, STATES, STOPPED_STATES, STOPPABLE_STATES, STOP_TRANSITION_LABELS,
} from "../operator/lib/mission-execution.js";
import { deploymentReadiness } from "../operator/lib/home.js";
import { handoffGate } from "../operator/lib/authority.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");

// The page sources the surface guards read. Declared here rather than beside section H because
// the button-mapping sections above also assert against the Map's wiring.
const mapSrc = read("../operator/pages/Map.js");
const agentSrc = read("../operator/pages/Agent.js");
const apiSrc = read("../operator/services/api.js");

const envelope = (over = {}) => ({
  ok: true, supported: true, reachable: true,
  scout: {
    supported: true,
    state: "READY", effective_state: "READY", active_operation_id: null,
    mission_id: "msn-329c2faff137", mode: "LOITER",
    sequence: { current: 3, count: 22 },
    replanning: { active: false, fsm_state: "MONITORING" },
    return_completion: { final_loiter_verified: false },
    authority_status: "LOCAL_AGENT",
    can_start: true, can_pause: false, can_resume: false,
    mission_execution_enabled: true, last_error: null,
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));
const actions = (ctl) => ctl.buttons.map((b) => b.action);
const labels = (ctl) => ctl.buttons.map((b) => b.label);
const byAction = (ctl, a) => ctl.buttons.find((b) => b.action === a);

// ── A. The state → control mapping, exactly as the product decision specifies ───────────
test("READY offers Start Mission and nothing else", () => {
  const ctl = lifecycleControls(S({ state: "READY", can_start: true }));
  assert.deepEqual(actions(ctl), ["start"]);
  assert.deepEqual(labels(ctl), ["Start Mission"]);
  assert.equal(byAction(ctl, "start").enabled, true);
});

test("NOT_STARTED offers Start Mission", () => {
  const ctl = lifecycleControls(S({ state: "NOT_STARTED", can_start: true }));
  assert.deepEqual(actions(ctl), ["start"]);
});

test("RUNNING offers Pause and Stop — and NEVER labels the primary button Resume", () => {
  const ctl = lifecycleControls(S({ state: "RUNNING", can_start: false, can_pause: true,
    can_stop: true }));
  assert.deepEqual(actions(ctl), ["pause", "stop"]);
  assert.equal(labels(ctl)[0], "Pause Mission");
  assert.equal(byAction(ctl, "pause").enabled, true);
  // The load-bearing assertion: Resume is only meaningful AFTER a Pause. A running mission
  // whose button says "Resume" invites an operator to press it believing it is stopped.
  assert.equal(labels(ctl).some((l) => /resume/i.test(l)), false, labels(ctl));
  assert.equal(actions(ctl).includes("resume"), false);
});

test("RETURNING_HOME is treated as running: Pause and Stop, never Resume", () => {
  const ctl = lifecycleControls(S({ state: "RETURNING_HOME", can_start: false,
    can_pause: true, can_stop: true }));
  assert.deepEqual(actions(ctl), ["pause", "stop"]);
  assert.equal(labels(ctl).some((l) => /resume/i.test(l)), false);
});

test("PAUSED offers Resume Mission and Stop Mission", () => {
  const ctl = lifecycleControls(S({ state: "PAUSED", can_start: false, can_resume: true,
    can_stop: true }));
  assert.deepEqual(actions(ctl), ["resume", "stop"]);
  assert.deepEqual(labels(ctl), ["Resume Mission", "Stop Mission"]);
});

test("STOPPED and CANCELLED offer a fresh Start Mission", () => {
  for (const state of STOPPED_STATES) {
    const ctl = lifecycleControls(S({ state, can_start: true, can_stop: false }));
    assert.deepEqual(actions(ctl), ["start"], state);
    assert.deepEqual(labels(ctl), ["Start Mission"], state);
  }
});

test("FAILED shows the failure and offers Rearm plus Take Control", () => {
  const ctl = lifecycleControls(S({ state: "FAILED", can_start: false,
    last_error: { code: "AUTO_NOT_VERIFIED", message: "mode never read back as AUTO" } }));
  assert.deepEqual(actions(ctl), ["rearm", "take-control"]);
  assert.match(ctl.failure, /AUTO_NOT_VERIFIED/);
  assert.match(ctl.failure, /mode never read back as AUTO/);
  assert.doesNotMatch(ctl.failure, /\[object Object\]/);
  assert.equal(ctl.tone, "warn");
});

test("SUSPENDED shows the failure and offers Rearm, Stop and Take Control", () => {
  const ctl = lifecycleControls(S({ state: "SUSPENDED", can_start: false,
    last_error: "replanning ended in SAFE_HOLD" }));
  assert.deepEqual(actions(ctl), ["rearm", "stop", "take-control"]);
  assert.match(ctl.failure, /SAFE_HOLD/);
});

test("Take Control is withheld when the operator already holds authority", () => {
  const ctl = lifecycleControls(S({ state: "FAILED", can_start: false,
    authority_status: "OPERATOR" }));
  assert.equal(byAction(ctl, "take-control").enabled, false);
  assert.match(byAction(ctl, "take-control").reason, /already holds control/i);
});

test("COMPLETED_HOLD shows completion + final LOITER, and gates a new Start behind Rearm", () => {
  const ctl = lifecycleControls(S({ state: "COMPLETED_HOLD", can_start: false,
    return_completion: { final_loiter_verified: true } }));
  assert.deepEqual(actions(ctl), ["rearm", "start"]);
  assert.equal(ctl.complete, true);
  assert.match(ctl.notice, /COMPLETED_HOLD/);
  assert.match(ctl.notice, /verified final LOITER/);
  // A new Start is DELIBERATE: it stays disabled until the controller is prepared for a fresh
  // execution, with the reason spelled out rather than the button silently doing nothing.
  const start = byAction(ctl, "start");
  assert.equal(start.enabled, false);
  assert.match(start.reason, /Rearm the mission controller first/);
});

test("COMPLETED_HOLD without a verified final LOITER says so instead of showing a green tick", () => {
  const ctl = lifecycleControls(S({ state: "COMPLETED_HOLD", can_start: false,
    return_completion: { final_loiter_verified: false } }));
  assert.equal(ctl.complete, false);
  assert.match(ctl.notice, /NOT verified/);
});

// ── A2. The hold controls: an ABSENT can_* is silence, not a refusal ────────────────────
//
// THE DEFECT THIS SECTION PINS. `can_pause` / `can_resume` were read with a strict `=== true`,
// so a Scout whose status carries no such key — which is every Scout that reports its lifecycle
// state without the optional capability flags — collapsed "said nothing" into "said no". On the
// Map that rendered a RUNNING mission with a Pause button disabled by the *fabricated* reason
// "Scout reports can_pause=false", beside an unsupported Stop: two dead buttons and no way to
// hold the vehicle. The state is the authority for WHICH control exists; the flag only gates it
// when Scout actually sends one.
test("RUNNING with NO can_pause key still offers an ENABLED Pause Mission", () => {
  const ctl = lifecycleControls(normalizeStatus({
    ok: true, supported: true, reachable: true,
    scout: { state: "RUNNING", mode: "AUTO", mission_id: "msn-329c2faff137",
      mission_execution_enabled: true, active_operation_id: null },
  }));
  const pause = byAction(ctl, "pause");
  assert.ok(pause, "a running mission must always carry a Pause control");
  assert.equal(pause.label, "Pause Mission");
  assert.equal(pause.enabled, true);
  assert.equal(pause.reason, null, "Scout said nothing — no reason may be invented for it");
});

test("PAUSED with NO can_resume key still offers an ENABLED Resume Mission", () => {
  const ctl = lifecycleControls(normalizeStatus({
    ok: true, supported: true, reachable: true,
    scout: { state: "PAUSED", mode: "LOITER", mission_execution_enabled: true },
  }));
  const resume = byAction(ctl, "resume");
  assert.equal(resume.label, "Resume Mission");
  assert.equal(resume.enabled, true);
  assert.equal(resume.reason, null);
});

test("an EXPLICIT can_pause:false is still honoured, with Scout's own answer as the reason", () => {
  // The other half of the tri-state: Scout HAS refused, so the station does not talk it into a
  // button that would come back 409. It is shown, disabled, saying exactly what Scout said.
  const ctl = lifecycleControls(S({ state: "RUNNING", can_start: false, can_pause: false }));
  const pause = byAction(ctl, "pause");
  assert.equal(pause.enabled, false);
  assert.match(pause.reason, /can_pause=false/);
  assert.match(pause.reason, /RUNNING/);
});

test("can_pause tri-state: true / false / absent are three distinct answers", () => {
  assert.equal(normalizeStatus({ scout: { state: "RUNNING", can_pause: true } }).canPause, true);
  assert.equal(normalizeStatus({ scout: { state: "RUNNING", can_pause: false } }).canPause, false);
  assert.equal(normalizeStatus({ scout: { state: "RUNNING" } }).canPause, null);
  assert.equal(normalizeStatus({ scout: { state: "RUNNING" } }).pauseReported, false);
  assert.equal(normalizeStatus({ scout: { state: "PAUSED" } }).canResume, null);
});

test("Pause is offered only where a run exists, Resume only after a pause", () => {
  for (const state of ["RUNNING", "RETURNING_HOME"]) {
    assert.equal(pauseAvailability(S({ state })).available, true, state);
    assert.equal(resumeAvailability(S({ state })).available, false, state);
  }
  assert.equal(resumeAvailability(S({ state: "PAUSED" })).available, true);
  assert.equal(pauseAvailability(S({ state: "PAUSED" })).available, false);
  for (const state of ["READY", "NOT_STARTED", "COMPLETED_HOLD", "FAILED", "STOPPED"]) {
    assert.equal(pauseAvailability(S({ state })).available, false, state);
    assert.equal(resumeAvailability(S({ state })).available, false, state);
  }
});

test("an unreadable status offers no hold control and claims nothing about the run", () => {
  for (const av of [pauseAvailability(normalizeStatus({ supported: false })),
    resumeAvailability(normalizeStatus({ reachable: false, scout: {} }))]) {
    assert.equal(av.available, false);
    assert.equal(av.enabled, false);
    assert.match(av.reason, /unavailable/i);
  }
});

// ── A3. The card only moves when the AUTHORITATIVE status moves ─────────────────────────
//
// The one rule the whole card rests on: an accepted Pause does not make the button say Resume.
// Scout's next status does. Optimism here would tell an operator the vehicle is holding at a
// moment when the LOITER may not have been verified at all.
test("an ACCEPTED pause does not turn the button into Resume — only a PAUSED status does", () => {
  const accepted = interpretTransaction({ status: 200, data: {
    outcome: "accepted", operation: "pause",
    phases: [{ phase: "verify", status: "ok", verified: true, observed_state: "PAUSED",
      observed_mode: "LOITER" }],
    authority: { before: "LOCAL_AGENT", after: "LOCAL_AGENT", required: "LOCAL_AGENT" },
  } });
  assert.equal(accepted.outcome, OUTCOME.ACCEPTED);

  // Status still says RUNNING (the next poll has not landed): the control is STILL Pause.
  const stillRunning = lifecycleControls(S({ state: "RUNNING", can_start: false,
    can_pause: true }));
  assert.equal(byAction(stillRunning, "pause").label, "Pause Mission");
  assert.equal(actions(stillRunning).includes("resume"), false);

  // Only once Scout's canonical status reports PAUSED does Resume appear.
  const paused = lifecycleControls(S({ state: "PAUSED", can_start: false, can_resume: true }));
  assert.equal(byAction(paused, "resume").label, "Resume Mission");
  assert.equal(actions(paused).includes("pause"), false);
});

test("an ACCEPTED resume does not turn the button back into Pause — only a RUNNING status does", () => {
  const paused = lifecycleControls(S({ state: "PAUSED", can_start: false, can_resume: true }));
  assert.deepEqual(actions(paused), ["resume", "stop"]);
  const running = lifecycleControls(S({ state: "RUNNING", can_start: false, can_pause: true }));
  assert.deepEqual(actions(running), ["pause", "stop"]);
});

test("a pause that Scout ACCEPTED but did not verify is not reported as a clean hold", () => {
  // mission_lifecycle._verify_state re-reads canonical status and answers `withheld` when the
  // vehicle is not where the operation says it should be. The operator has to SEE that:
  // "accepted" is Scout taking the request, not proof that a LOITER was reached.
  const view = interpretTransaction({ status: 200, data: {
    outcome: "accepted", operation: "pause",
    phases: [{ phase: "verification", status: "withheld", verified: false,
      observed_state: "RUNNING", observed_mode: "AUTO",
      detail: "Scout accepted the pause but reports RUNNING in mode AUTO, not PAUSED in LOITER" }],
    authority: {},
  } });
  assert.equal(view.verified, false);
  assert.match(transactionSummary(view), /not PAUSED in LOITER/);
  assert.notEqual(transactionSummary(view), "Accepted");
});

test("a VERIFIED pause reports the hold plainly, with no false alarm attached", () => {
  const view = interpretTransaction({ status: 200, data: {
    outcome: "accepted", operation: "pause",
    phases: [{ phase: "verification", status: "ok", verified: true, observed_state: "PAUSED",
      observed_mode: "LOITER", detail: "Scout reports PAUSED in mode LOITER — verified" }],
    authority: {} } });
  assert.equal(view.verified, true);
  assert.doesNotMatch(transactionSummary(view), /NOT be verified/i);
});

test("the Map's result line calls an unverified acceptance what it is", () => {
  assert.match(mapSrc, /res\.view\.outcome === "accepted" && res\.view\.verified === false/);
  assert.match(mapSrc, /accepted — resulting state NOT verified/);
});

test("a FAILED pause/resume leaves the operator a usable error, never a silent no-op", () => {
  const rejected = interpretTransaction({ status: 409, data: {
    outcome: "rejected", operation: "pause", error_code: "REPLANNING_ACTIVE",
    error: "The replanning controller owns the vehicle", phases: [], authority: {} } });
  assert.equal(rejected.outcome, OUTCOME.REJECTED);
  assert.match(transactionSummary(rejected), /REPLANNING_ACTIVE|replanning controller/i);

  const failed = interpretTransaction({ status: 200, data: {
    outcome: "failed", operation: "resume",
    error: { code: "AUTO_NOT_VERIFIED", message: "mode never read back as AUTO" },
    phases: [], authority: {} } });
  assert.equal(failed.outcome, OUTCOME.FAILED);
  assert.match(transactionSummary(failed), /AUTO_NOT_VERIFIED/);
  assert.match(transactionSummary(failed), /mode never read back as AUTO/);
  assert.doesNotMatch(transactionSummary(failed), /\[object Object\]/);

  const unavailable = interpretTransaction({ status: 503, data: {
    outcome: "unavailable", operation: "pause", error: "Scout Local Agent unreachable" } });
  assert.equal(unavailable.outcome, OUTCOME.UNAVAILABLE);
  assert.match(outcomeLabel(unavailable.outcome), /unavailable/i);
  // A failed operation is never dressed up as a start failure banner on another operation.
  assert.equal(startFailure(null), null);
});

// ── A3b. A Start whose HTTP verdict was LOST is reported from the reconciling read ───────
//
// The live defect: Scout completed a Start in 12.0 s and entered RUNNING / AUTO, the operator's
// own HTTP client gave up first, and the card said "Mission could not start: No response from
// Scout" about a mission that was running. The transport budget is fixed in the backend; these
// pin the presentation rule, which has to hold whatever the budget is — a lost verdict is
// answered by the backend's ONE reconciling read of Scout's canonical status, never by the fact
// that the local client stopped waiting.
const lostVerdict = (reconciliation) => interpretTransaction({ status: 202, data: {
  outcome: "unknown", operation: "start", mission_id: "msn-329c2faff137",
  error: "No response from Scout — outcome unknown, reconcile with a read: ReadTimeout",
  reconciliation, phases: [], authority: { required: "LOCAL_AGENT", verified: true } } });

test("a lost Start verdict that reconciles to RUNNING is a started mission, not a failure", () => {
  const view = lostVerdict({ attempted: true, operation: "start", resolved: "running",
    state: "RUNNING", mode: "AUTO", mission_id: "msn-329c2faff137", mission_id_match: true,
    detail: "Scout reports RUNNING in mode AUTO" });
  assert.equal(startFailure(view), null, "no failure banner for a mission that is running");
  const rec = reconciledStart(view);
  assert.equal(rec.started, true);
  assert.equal(rec.inProgress, false);
  assert.equal(rec.tone, "ok");
  assert.match(rec.text, /started/i);
  assert.doesNotMatch(rec.text, /could not start|no response/i);
  // The evidence stays available in full — the tooltip still says where the answer came from.
  assert.match(transactionSummary(view), /reconciled: running/);
});

test("a lost Start verdict mid-transaction is IN PROGRESS, not a failed Start", () => {
  // Scout's own Start steps, including the three this build previously did not recognize.
  for (const state of ["ARMING", "VERIFYING_ARMED", "START_HOLD_CONFIRMED", "STARTING_AUTO",
    "CONFIRMING_PROGRESSION"]) {
    const view = lostVerdict({ attempted: true, operation: "start", resolved: "in_progress",
      state, detail: `Scout is still processing (${state})` });
    assert.equal(startFailure(view), null, state);
    const rec = reconciledStart(view);
    assert.equal(rec.inProgress, true, state);
    assert.equal(rec.started, false, state);
    assert.match(rec.text, /in progress/i, state);
    assert.doesNotMatch(rec.text, /could not start/i, state);
    // …and every one of them is a state this build knows and shows as a step.
    assert.equal(STATES.includes(state), true, state);
  }
});

test("a lost Start verdict whose reconciling READ also failed is a genuine no-response", () => {
  // Scout is unreachable: the write got no verdict AND the status endpoint answered nothing.
  // This is the one case that must still read as a connection failure, unchanged.
  const view = lostVerdict({ attempted: true, operation: "start", resolved: "unknown",
    status_outcome: "unavailable", supported: true, reachable: false, state: null,
    detail: "Mission-execution status could not be read — the operation's outcome stays UNKNOWN "
      + "and must not be resent blindly" });
  const fail = startFailure(view);
  assert.notEqual(fail, null);
  assert.equal(fail.title, "Mission could not start");
  assert.match(fail.text, /No response from Scout/);
  assert.equal(reconciledStart(view).started, false);
  assert.equal(reconciledStart(view).text, null);
});

test("a lost Start verdict that reconciles to a NEGATIVE answer stays a failure", () => {
  // Scout is resting where it started, or running something else. Both are definite answers, and
  // neither may be softened by the fact that reconciliation ran.
  for (const [resolved, detail] of [
    ["ready", "Scout is still READY — the start did not take effect"],
    ["not_started", "Scout reports STOPPED — the operation did not take effect"],
    ["mission_mismatch", "Scout reports mission msn-OTHER, not the expected msn-329c2faff137"],
    ["failed", "AUTO_NOT_VERIFIED"],
  ]) {
    const view = lostVerdict({ attempted: true, operation: "start", resolved, detail });
    assert.notEqual(startFailure(view), null, resolved);
    assert.equal(reconciledStart(view).text, null, resolved);
  }
});

test("a DEFINITE Scout Start failure is still shown as a failure", () => {
  // Untouched by any of the above: Scout answered, and its answer was that the vehicle-level
  // start failed. Reconciliation never runs here, and there is nothing to reinterpret.
  const failed = interpretTransaction({ status: 200, data: {
    outcome: "failed", operation: "start",
    error: { code: "AUTO_NOT_VERIFIED", message: "mode never read back as AUTO" },
    phases: [], authority: {} } });
  const fail = startFailure(failed);
  assert.equal(fail.title, "Mission could not start");
  assert.match(fail.detail, /AUTO_NOT_VERIFIED/);
  assert.equal(reconciledStart(failed), null, "an answered operation is never 'reconciled'");
  const rejected = interpretTransaction({ status: 409, data: {
    outcome: "blocked", operation: "start", error_code: "START_PRECONDITIONS_NOT_MET",
    blockers: ["Mission record VERIFIED: No active mission record"], phases: [], authority: {} } });
  assert.equal(startFailure(rejected).blocked, true);
});

test("the Map renders the reconciled Start verdict instead of the bare unknown", () => {
  assert.match(mapSrc, /mx\.reconciledStart\(res\.view\)/);
  // It is reached only for a Start, and only when there is no failure to show.
  assert.match(mapSrc, /res\.action === "start" && !startFail/);
  assert.match(mapSrc, /startReconciled && startReconciled\.text/);
});

// ── A4. Take Control stays reachable for the whole of a LOCAL_AGENT run ──────────────────
test("Take Control remains available while the Local Agent owns the mission", () => {
  for (const phase of [undefined, "confirmed"]) {
    const gate = handoffGate({ available: true, reachable: true, value: "LOCAL_AGENT",
      hasControl: false, phase }, { stale: false });
    assert.equal(gate.canTake, true, "the operator must always be able to take the wheel back");
    assert.equal(gate.hasControl, false, "…without that enabling the Pixhawk command buttons");
  }
  // And the vehicle command buttons stay LOCKED throughout — taking control is the only way in.
  const operator = handoffGate({ available: true, reachable: true, value: "OPERATOR",
    hasControl: true }, { stale: false });
  assert.equal(operator.hasControl, true);
  assert.equal(operator.canTake, false, "nothing to take — the operator already holds it");
});

test("the Map renders Take Control outside the Agent Mission card, so a run cannot hide it", () => {
  // It sits in Vehicle Commands, which is rendered before the lifecycle card and is not gated on
  // any mission-execution state — a RUNNING agent mission can never remove the manual override.
  assert.match(mapSrc, /\$\{takeControl\(av, stale, canTake\)\}/);
  assert.ok(mapSrc.indexOf("${takeControl(av, stale, canTake)}")
    < mapSrc.indexOf('<span class="lbl">Agent Mission</span>'),
    "Take Control must not live inside the Agent Mission card");
});

// ── B. Precedence: nothing is offered while Scout owns the moment ───────────────────────
test("a mid-transaction Scout offers no control at all, only progress", () => {
  for (const state of ["SETTING_HOME", "STARTING_AUTO", "PAUSE_REQUESTED",
    "STOP_HOLD_REQUESTED"]) {
    const ctl = lifecycleControls(S({ state, can_start: false }));
    assert.deepEqual(actions(ctl), [], state);
    assert.equal(ctl.tone, "caution", state);
  }
});

test("an active operation id suppresses every control even in a resting state", () => {
  const ctl = lifecycleControls(S({ state: "READY", can_start: true,
    active_operation_id: "op-77" }));
  assert.deepEqual(actions(ctl), []);
  assert.match(ctl.notice, /op-77/);
});

test("replanning ownership suppresses every control and names the FSM state", () => {
  const ctl = lifecycleControls(S({ state: "RUNNING", can_pause: true,
    effective_state: "REPLANNING", replanning: { active: true, fsm_state: "PLANNING" } }));
  assert.deepEqual(actions(ctl), []);
  assert.match(ctl.notice, /PLANNING/);
});

test("an unsupported or unreachable Scout offers nothing and claims nothing", () => {
  const unsupported = lifecycleControls(normalizeStatus({ supported: false }));
  assert.deepEqual(actions(unsupported), []);
  assert.match(unsupported.notice, /not supported/i);
  const unreachable = lifecycleControls(normalizeStatus({ reachable: false, scout: {} }));
  assert.deepEqual(actions(unreachable), []);
  assert.match(unreachable.notice, /unavailable/i);
});

// ── C. No double submission ─────────────────────────────────────────────────────────────
test("every button is disabled while an operation is in flight", () => {
  for (const state of ["READY", "RUNNING", "PAUSED", "FAILED", "COMPLETED_HOLD"]) {
    const ctl = lifecycleControls(S({ state, can_start: true, can_pause: true,
      can_resume: true, can_stop: true }), { busy: true });
    for (const b of ctl.buttons) {
      assert.equal(b.enabled, false, `${state}/${b.action} must be disabled while busy`);
      assert.match(b.reason, /already in progress/i);
    }
  }
});

test("busy disables the controls without changing the labels", () => {
  const idle = lifecycleControls(S({ state: "RUNNING", can_pause: true, can_stop: true }));
  const busy = lifecycleControls(S({ state: "RUNNING", can_pause: true, can_stop: true }),
    { busy: true });
  // The label follows STATUS, not the click: pressing Pause must not turn the button into
  // "Resume" before Scout's next authoritative status says so.
  assert.deepEqual(labels(busy), labels(idle));
});

// ── D. Start gating comes from the BACKEND preflight, not a second local derivation ─────
test("a blocked preflight disables Start and shows the backend's own reason", () => {
  const ctl = lifecycleControls(S({ state: "READY", can_start: true }), {
    startBlocked: true,
    startBlockedReason: "Planning package stored, usable and consistent: not stored",
  });
  const start = byAction(ctl, "start");
  assert.equal(start.enabled, false);
  assert.match(start.reason, /not stored/);
});

test("a structured preflight reason is formatted, never object-coerced", () => {
  const ctl = lifecycleControls(S({ state: "READY", can_start: true }), {
    startBlocked: true, startBlockedReason: { code: "NO_ACTIVE_MISSION", message: "none" },
  });
  assert.doesNotMatch(byAction(ctl, "start").reason, /\[object Object\]/);
  assert.match(byAction(ctl, "start").reason, /NO_ACTIVE_MISSION/);
});

// ── E. Stop availability: SCOUT'S LIFECYCLE EVIDENCE, never the Pixhawk mode ────────────
test("Stop is offered in every lifecycle state Scout supports it from", () => {
  for (const state of STOPPABLE_STATES) {
    const av = stopAvailability(S({ state, can_start: false }));
    assert.equal(av.available, true, state);
    assert.equal(av.enabled, true, `${state} — an absent can_stop is silence, not a refusal`);
  }
});

test("an absent can_stop is silence, not a refusal — the state is the authority", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_pause: true }));   // no can_stop key
  assert.equal(av.available, true);
  assert.equal(av.enabled, true);
  assert.equal(av.reported, false, "Scout said nothing about can_stop");
  assert.equal(av.reason, null);
});

test("can_stop:false is Scout REFUSING right now — shown, disabled, with its own answer", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_pause: true, can_stop: false }));
  assert.equal(av.available, true, "never hidden — the operator must see why");
  assert.equal(av.enabled, false);
  assert.equal(av.reported, true);
  assert.match(av.reason, /can_stop=false/);
});

test("can_stop:true enables Stop", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_pause: true, can_stop: true }));
  assert.equal(av.available, true);
  assert.equal(av.enabled, true);
  assert.equal(av.reason, null);
});

test("can_stop:true in a state this build does not list is still honoured", () => {
  // "…and any other states explicitly supported by Scout". Scout is the authority; this build's
  // STOPPABLE_STATES list is a floor, not a ceiling.
  const av = stopAvailability(S({ state: "COMPLETED_HOLD", can_start: false, can_stop: true }));
  assert.equal(av.available, true);
  assert.equal(av.enabled, true);
});

test("Stop is NOT offered from a state Scout gives no stop evidence for", () => {
  const av = stopAvailability(S({ state: "READY", can_start: true }));
  assert.equal(av.available, false, "a mission that is not running has nothing to abort");
});

test("Stop is never derived from the Pixhawk mode alone", () => {
  // AUTO on the flight controller with the lifecycle resting in READY is NOT a stoppable run.
  const av = stopAvailability(S({ state: "READY", mode: "AUTO", can_start: true }));
  assert.equal(av.available, false);
  // …and a RUNNING lifecycle in MANUAL still is one: the lifecycle decides, not the mode.
  assert.equal(stopAvailability(S({ state: "RUNNING", mode: "MANUAL" })).available, true);
});

test("Stop is disabled while another mission-execution operation is in progress", () => {
  const active = stopAvailability(S({ state: "RUNNING", can_stop: true,
    active_operation_id: "op-77" }));
  assert.equal(active.available, true);
  assert.equal(active.enabled, false);
  assert.match(active.reason, /op-77/);

  const mid = stopAvailability(S({ state: "PAUSE_REQUESTED", can_stop: true }));
  assert.equal(mid.enabled, false);
  assert.match(mid.reason, /mid-transaction/i);

  const busy = stopAvailability(S({ state: "RUNNING", can_stop: true }), { busy: true });
  assert.equal(busy.enabled, false);
  assert.match(busy.reason, /already in progress/i);
});

test("Stop is disabled while Scout reports a package/BUSY conflict on the active run", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_stop: true,
    package_conflict: { code: "OPERATION_IN_PROGRESS", execution_state: "RUNNING" } }));
  assert.equal(av.available, true);
  assert.equal(av.enabled, false);
  assert.match(av.reason, /OPERATION_IN_PROGRESS/);
});

test("Stop is disabled while the replanning controller owns the vehicle", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_stop: true,
    replanning: { active: true, fsm_state: "REPLANNING" } }));
  assert.equal(av.enabled, false);
  assert.match(av.reason, /replanning controller/i);
});

test("the whole STOP sequence is modelled so no phase reads as an unknown state", () => {
  for (const s of ["STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
    "STOP_VERIFYING_MISSION", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
    "STOP_VERIFYING_REWIND", "STOP_RESETTING", "STOP_VERIFYING_RESET", "STOPPED"]) {
    assert.ok(STATES.includes(s), s);
  }
});

// ── E2. The Stop control set: Stop sits BESIDE the current mission control ───────────────
test("RUNNING offers Pause AND Stop, in that order", () => {
  const ctl = lifecycleControls(S({ state: "RUNNING", can_start: false, can_pause: true,
    can_stop: true }));
  assert.deepEqual(actions(ctl), ["pause", "stop"]);
  assert.deepEqual(labels(ctl), ["Pause Mission", "Stop Mission"]);
  assert.equal(byAction(ctl, "stop").enabled, true);
});

test("PAUSED offers Resume AND Stop, in that order", () => {
  const ctl = lifecycleControls(S({ state: "PAUSED", can_start: false, can_resume: true,
    can_stop: true }));
  assert.deepEqual(actions(ctl), ["resume", "stop"]);
  assert.deepEqual(labels(ctl), ["Resume Mission", "Stop Mission"]);
});

test("SUSPENDED after a failed replan offers Rearm AND Stop", () => {
  const ctl = lifecycleControls(S({ state: "SUSPENDED", can_start: false, can_stop: true,
    last_error: { code: "REPLAN_FAILED", message: "safe return could not be planned" } }));
  assert.ok(actions(ctl).includes("rearm"), "Rearm prepares the controller for another run");
  assert.ok(actions(ctl).includes("stop"), "Stop is the safe abort — a different action");
  assert.ok(actions(ctl).indexOf("rearm") < actions(ctl).indexOf("stop"));
  assert.equal(byAction(ctl, "stop").enabled, true);
});

test("the return phase keeps Pause and Stop — the run is still under way there", () => {
  for (const state of ["RETURNING_HOME", "HOME_ARRIVAL_PENDING"]) {
    const ctl = lifecycleControls(S({ state, can_start: false, can_stop: true }));
    assert.deepEqual(actions(ctl), ["pause", "stop"], state);
  }
});

test("Stop is never offered while Scout is inside one of its own write transactions", () => {
  for (const state of ["STOP_REQUESTED", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
    "SETTING_HOME", "FINAL_HOLD_REQUESTED"]) {
    const ctl = lifecycleControls(S({ state, can_start: false, can_stop: true }));
    assert.deepEqual(actions(ctl), [], state);
  }
});

test("Stop is never labelled as a mission deletion anywhere the operator can read it", () => {
  const ctl = lifecycleControls(S({ state: "RUNNING", can_pause: true, can_stop: true }));
  assert.equal(byAction(ctl, "stop").label, "Stop Mission");
  const stopCopy = [mapSrc, agentSrc].join("\n");
  assert.doesNotMatch(stopCopy, /Delete Mission|Destroy Mission|Discard Mission/i);
});

// ── F. The transaction envelope: `blocked` is not `rejected` ────────────────────────────
test("a blocked transaction is reported as the OPERATOR's refusal, not Scout's", () => {
  const view = interpretTransaction({ status: 409, data: {
    outcome: "blocked", operation: "start", error_code: "MISSION_ID_MISMATCH",
    error: "The requested mission msn-x is not this vehicle's active mission msn-y.",
    phases: [{ phase: "mission-resolution", status: "failed", detail: "…" }],
    authority: { before: "OPERATOR", after: null, required: "LOCAL_AGENT", verified: null },
  } });
  assert.equal(view.outcome, OUTCOME.BLOCKED);
  assert.match(outcomeLabel(view.outcome), /Blocked by the Operator/);
  assert.match(outcomeLabel(view.outcome), /not sent/);
  assert.notEqual(view.outcome, OUTCOME.REJECTED);
});

test("the transaction summary reports what happened to authority", () => {
  const restored = interpretTransaction({ status: 200, data: {
    outcome: "accepted", operation: "stop",
    phases: [{ phase: "authority-restore", status: "ok", restored: true,
      detail: "Scout reports STOPPED with a verified LOITER — OPERATOR authority restored." }],
    authority: { before: "LOCAL_AGENT", after: "OPERATOR", verified: true },
  } });
  assert.equal(restored.authorityRestored, true);
  assert.match(transactionSummary(restored), /authority returned to OPERATOR/);

  const withheld = interpretTransaction({ status: 200, data: {
    outcome: "failed", operation: "start", scout_error_code: "LOITER_NOT_VERIFIED",
    phases: [{ phase: "authority-restore", status: "withheld", restored: false,
      detail: "Scout reported LOITER_NOT_VERIFIED after it began the start transaction" }],
    authority: { before: "OPERATOR", after: "LOCAL_AGENT", verified: true },
  } });
  assert.equal(withheld.authorityRestored, false);
  assert.match(transactionSummary(withheld), /after it began the start transaction/);
});

test("a transaction view never renders a structured error as [object Object]", () => {
  const view = interpretTransaction({ status: 200, data: {
    outcome: "failed", operation: "start",
    error: { code: "SET_HOME_FAILED", message: "no ack from Pixhawk" },
    phases: [], authority: {},
  } });
  assert.doesNotMatch(String(view.message), /\[object Object\]/);
  assert.doesNotMatch(transactionSummary(view), /\[object Object\]/);
  assert.match(transactionSummary(view), /no ack from Pixhawk/);
});

// ── G. Deployment readiness: three concepts, kept apart ─────────────────────────────────
const vehicleReady = { connected: true, gpsFresh: true, posValid: true, missionLoaded: true,
  homeVerified: true };

test("LOCAL_AGENT authority does NOT make the vehicle NOT READY", () => {
  // The bench-test inversion: the Map read READY while the operator held control and flipped to
  // NOT READY the moment authority moved to the agent — i.e. it called the vehicle unfit
  // precisely when it was correctly configured to fly the mission.
  const asAgent = deploymentReadiness({ ...vehicleReady, hasControl: false,
    authority: "LOCAL_AGENT" });
  assert.equal(asAgent.ready, true);
  const asOperator = deploymentReadiness({ ...vehicleReady, hasControl: true,
    authority: "OPERATOR" });
  assert.equal(asOperator.ready, true);
  assert.equal(asAgent.ready, asOperator.ready, "readiness must not depend on the control owner");
});

test("authority is no longer a scored readiness item", () => {
  const r = deploymentReadiness({ ...vehicleReady, hasControl: false, authority: "LOCAL_AGENT" });
  assert.equal(r.items.some((i) => i.key === "authority"), false, r.items);
  assert.deepEqual(r.items.map((i) => i.key), ["pixhawk", "gps", "mission", "home"]);
});

test("the control owner is reported as a separate, unscored fact", () => {
  assert.deepEqual(
    deploymentReadiness({ ...vehicleReady, authority: "LOCAL_AGENT" }).controlOwner,
    { value: "LOCAL_AGENT", label: "Local Agent", isOperator: false, isAgent: true });
  assert.equal(deploymentReadiness({ ...vehicleReady, authority: "RC" }).controlOwner.label,
    "RC override");
  assert.equal(deploymentReadiness({ ...vehicleReady }).controlOwner.label, "Unknown");
});

test("a genuine VEHICLE fault still makes it not ready, whoever holds authority", () => {
  for (const authority of ["OPERATOR", "LOCAL_AGENT"]) {
    assert.equal(deploymentReadiness({ ...vehicleReady, homeVerified: false, authority }).ready,
      false, authority);
    assert.equal(deploymentReadiness({ ...vehicleReady, connected: false, authority }).ready,
      false, authority);
  }
});

test("OPERATOR authority does not by itself imply the agent mission is startable", () => {
  // Vehicle readiness is silent about the mission; Start eligibility lives in the backend
  // preflight and Scout's own status, and the card gates Start on THAT.
  const r = deploymentReadiness({ ...vehicleReady, hasControl: true, authority: "OPERATOR" });
  assert.equal(r.ready, true);
  assert.equal("canStartMission" in r, false);
  const ctl = lifecycleControls(S({ state: "READY", can_start: true }),
    { startBlocked: true, startBlockedReason: "Scout replanning readiness: not ready" });
  assert.equal(byAction(ctl, "start").enabled, false);
});

test("the LOITER safety exemption is untouched by the readiness split", () => {
  const r = deploymentReadiness({ connected: true, hasControl: true, gpsFresh: true,
    posValid: true, missionLoaded: false, homeVerified: false });
  assert.equal(r.ready, false);
  assert.equal(r.loiterAvailable, true);
});

// ── H. Page surfaces: the Map operates, the Agent page diagnoses ────────────────────────
test("the Map page carries the lifecycle controls", () => {
  assert.match(mapSrc, /Agent Mission/);
  assert.match(mapSrc, /function renderAgentMission/);
  // The card renders the shared model, never a page-local re-derivation. missionCardView is the
  // compact presentation of lifecycleControls (it calls it — see the lib), so asserting the
  // Map calls THAT still pins "the buttons come from Scout's status, not from the last click".
  assert.match(mapSrc, /mx\.missionCardView\(/);
  assert.match(read("../operator/lib/mission-execution.js"),
    /export function missionCardView[\s\S]{0,900}lifecycleControls\(S, \{ busy/);
  for (const call of ["startMissionExecution", "pauseMissionExecution",
    "resumeMissionExecution", "stopMissionExecution"]) {
    assert.match(mapSrc, new RegExp(`api\\.${call}\\(`), call);
  }
  assert.match(mapSrc, /data-mx=/);
});

test("the Map never sequences authority itself — one endpoint per intent", () => {
  // The browser must not do "release control, then start": a page that got the order wrong
  // could strand the vehicle between owners. The backend transaction owns both halves.
  const wiring = mapSrc.slice(mapSrc.indexOf("async function onMissionAction"),
    mapSrc.indexOf("async function onMissionAction") + 3000);
  assert.doesNotMatch(wiring, /setControlAuthority\([^)]*LOCAL_AGENT/);
  assert.match(mapSrc, /api\.startMissionExecution\(id, \{\}\)/);
});

test("the Map's mission lifecycle is never implemented with low-level vehicle commands", () => {
  // COMMENTS ARE STRIPPED FIRST. The guard is about what the card DOES, and the card's prose has
  // to be able to name the things it must not do — and, since Scout began reporting energy
  // feasibility, to name RTL at all: "RTL Home", "RTL return margin" and "RTL INSUFFICIENT" are
  // Scout's own vocabulary for whether the vehicle could get back, not a mode command. Matching
  // the bare word in prose would either forbid explaining the rule or forbid displaying Scout's
  // verdict; matching it in CODE still catches the only thing that was ever the danger.
  const card = mapSrc.slice(mapSrc.indexOf("function renderAgentMission"),
    mapSrc.indexOf("function renderAgentMission") + 6000)
    .replace(/^\s*\/\/.*$/gm, "");
  for (const t of ["SET_MODE_AUTO", "SET_MODE_LOITER", "RTL", "ARM", "DISARM",
    "createCommand"]) {
    assert.doesNotMatch(card, new RegExp(t), t);
  }
});

test("the Map guards against a double submission in the handler AND in the render", () => {
  assert.match(mapSrc, /if \(mission\.busy\) return;/);          // synchronous click guard
  assert.match(mapSrc, /busy: mission\.busy/);                   // buttons rendered disabled
  assert.match(mapSrc, /mission\.busy = true;/);                 // set BEFORE the await
});

test("normal Start / Pause / Resume are REMOVED from the Agent page's primary controls", () => {
  // They survive only inside the collapsed diagnostic fallback, which must say so.
  assert.doesNotMatch(agentSrc, /id="mx-primary"/);
  assert.match(agentSrc, /function mxFallbackControls/);
  assert.match(agentSrc, /Diagnostic fallback controls \(not the normal path\)/);
  assert.match(agentSrc, /<details class="mx-fallback">/);
  // Collapsed by default: no `open` attribute on the details element.
  assert.doesNotMatch(agentSrc, /<details class="mx-fallback" open/);
  // …and it points the operator at the Map for normal operation.
  assert.match(agentSrc, /Normal mission controls are on the Map page/);
});

test("the Agent page keeps its diagnostic depth", () => {
  for (const section of ["Mission lifecycle", "Home \\(set and verified by Scout\\)",
    "Pause / resume sequence evidence", "Replanning handoff & return completion",
    "Mission lifecycle operations", "Rearm Mission Controller"]) {
    assert.match(agentSrc, new RegExp(section), section);
  }
});

test("api.js exposes one endpoint per user intent, all per-vehicle", () => {
  for (const [fn, path] of [
    ["startMissionExecution", "/mission-execution/start"],
    ["pauseMissionExecution", "/mission-execution/pause"],
    ["resumeMissionExecution", "/mission-execution/resume"],
    ["stopMissionExecution", "/mission-execution/stop"],
    ["getMissionExecutionPreflight", "/mission-execution/preflight"],
  ]) {
    assert.match(apiSrc, new RegExp(`export function ${fn}\\(`), fn);
    assert.match(apiSrc, new RegExp(`/api/vehicles/\\$\\{id\\}${path}`), path);
  }
});

// ── H2. The Map CARD renders the same mapping, and nothing optimistic feeds it ──────────
//
// missionCardView is what the Map actually draws. These assert the operator-visible result of
// the mapping, not just the model behind it.
const cardActions = (card) => card.buttons.map((b) => b.action);

test("the Agent Mission card shows Start when READY and Pause Mission when RUNNING", () => {
  const ready = missionCardView(S({ state: "READY", can_start: true }),
    { readiness: { state: "READY", canStart: true } });
  assert.deepEqual(cardActions(ready), ["start"]);
  assert.equal(ready.buttons[0].label, "Start Mission");

  const running = missionCardView(S({ state: "RUNNING", can_start: false, can_pause: true,
    mode: "AUTO" }));
  assert.equal(cardActions(running).includes("pause"), true);
  assert.equal(running.buttons.find((b) => b.action === "pause").label, "Pause Mission");
  assert.equal(running.chip, "RUNNING");
  assert.equal(running.headline, "AUTO · WP 3 / 22");   // watching, not identifying
});

test("the Agent Mission card shows Resume Mission once Scout reports PAUSED", () => {
  const card = missionCardView(S({ state: "PAUSED", can_start: false, can_resume: true,
    mode: "LOITER" }));
  assert.equal(cardActions(card).includes("resume"), true);
  assert.equal(card.buttons.find((b) => b.action === "resume").label, "Resume Mission");
  assert.equal(cardActions(card).includes("pause"), false);
  assert.equal(card.headline, "LOITER · WP 3 / 22");
});

test("the card shows the PHASE and offers no conflicting action mid-transaction", () => {
  for (const [state, phrase] of [["STARTING_AUTO", /Starting AUTO/i],
    ["VERIFYING_HOME", /Verifying Home|Home/i], ["PAUSE_REQUESTED", /Pausing|Holding|hold/i],
    ["RESUME_REQUESTED", /Resuming|Starting AUTO|AUTO/i]]) {
    const card = missionCardView(S({ state, can_start: false }));
    assert.equal(card.working, true, state);
    assert.deepEqual(cardActions(card), [], `${state} must offer no competing action`);
    assert.match(card.headline, phrase, state);
  }
});

test("a lifecycle operation in flight from this station disables every card button", () => {
  const card = missionCardView(S({ state: "RUNNING", can_pause: true, can_stop: true }),
    { busy: true });
  assert.ok(card.buttons.length, "the controls stay visible so the operator sees the state");
  for (const b of card.buttons) {
    assert.equal(b.enabled, false, b.action);
    assert.match(b.reason, /already in progress/i);
  }
});

test("the card's FAILED presentation carries the recovery action and the reason", () => {
  const card = missionCardView(S({ state: "FAILED", can_start: false,
    last_error: { code: "PROGRESSION_UNCONFIRMED", message: "sequence never advanced" } }));
  assert.deepEqual(cardActions(card), ["rearm", "take-control"]);
  assert.match(card.blocker.title, /PROGRESSION_UNCONFIRMED/);
  assert.match(card.blocker.title, /sequence never advanced/);
});

test("the Map's card state comes ONLY from the polled status, never from an operation reply", () => {
  // The single most important wiring rule here: a transaction's own response must not be written
  // into `mission.status`. It is recorded as `mission.result` (the outcome line) and the card is
  // re-derived from a fresh authoritative read.
  assert.match(mapSrc, /mission\.result = \{ label, action, view: mx\.interpretTransaction\(r\)/);
  const assigns = [...mapSrc.matchAll(/mission\.status\s*=\s*([^;\n]+)/g)].map((m) => m[1].trim());
  for (const rhs of assigns) {
    assert.ok(/^st$|^null$|^mission\.preflight/.test(rhs),
      `mission.status may only be set from the status poll or cleared, not from: ${rhs}`);
  }
  // …and every transaction re-reads the authoritative status when it settles.
  const tx = mapSrc.slice(mapSrc.indexOf("function missionTransaction"),
    mapSrc.indexOf("function missionTransaction") + 1600);
  assert.match(tx, /\.finally\(\(\) => \{[\s\S]{0,400}loadMissionStatus\(id\)/);
});

test("Pause and Resume are the Local Agent's transactions — the Map never shortcuts them", () => {
  const wiring = mapSrc.slice(mapSrc.indexOf("async function onMissionAction"),
    mapSrc.indexOf("async function onMissionAction") + 3000);
  assert.match(wiring, /action === "pause"[\s\S]{0,300}api\.pauseMissionExecution\(id\)/);
  assert.match(wiring, /action === "resume"[\s\S]{0,300}api\.resumeMissionExecution\(id\)/);
  // Never a browser-side "just send AUTO" (or LOITER) standing in for the transaction Scout owns.
  for (const shortcut of ["SET_MODE_AUTO", "SET_MODE_LOITER", "createCommand"]) {
    assert.doesNotMatch(wiring, new RegExp(shortcut), shortcut);
  }
});

// ── I. No object coercion anywhere the lifecycle or policy renders ──────────────────────
test("no lifecycle or policy render path can produce [object Object]", () => {
  // A source guard, because the failure mode is invisible until it reaches an operator: the
  // pages must format Scout's structured values through asText/esc, never through String() or
  // a bare template interpolation of a value that may be an object.
  for (const [name, src] of [["Map", mapSrc], ["Agent", agentSrc]]) {
    assert.match(src, /from "\.\.\/lib\/format\.js"/, name);
  }
  // Agent.js's single display formatter must go through asText, not String().
  const valDecl = agentSrc.slice(agentSrc.indexOf("const val = "),
    agentSrc.indexOf("const val = ") + 220);
  assert.match(valDecl, /asText\(v\)/);
  assert.doesNotMatch(valDecl, /String\(v\)/);
  // The one place a lifecycle failure reason is produced must not coerce either.
  const lib = read("../operator/lib/mission-execution.js");
  assert.doesNotMatch(lib, /String\(S\.lastError\)/);
  assert.match(lib, /asText\(S\.lastError\)/);
});

test("a status full of structured values renders no [object Object] anywhere", () => {
  const status = S({
    state: "FAILED", can_start: false,
    last_error: { code: "PACKAGE_SYNC_FAILED", detail: { stage: "upload", attempts: 3 } },
  });
  const ctl = lifecycleControls(status, { startBlocked: true,
    startBlockedReason: { blockers: [{ label: "package" }] } });
  const rendered = [ctl.failure, ctl.notice, ...ctl.buttons.map((b) => `${b.label} ${b.reason}`)]
    .join(" ");
  assert.doesNotMatch(rendered, /\[object Object\]/, rendered);
  assert.match(ctl.failure, /PACKAGE_SYNC_FAILED/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// K. STOP — the safe abort, end to end
//
// Scout owns the whole transaction (verified LOITER → verify the active mission identity →
// restore the immutable original mission → rewind it to the start → verify the rewind → reset
// execution/replan/test state → clear the experiment injection → invalidate the runtime Home →
// return supervisory authority to OPERATOR → re-prove the mission evidence).
//
// The station's job is to offer it from the right states, show Scout's own phases while it runs,
// present the completed result honestly — and NEVER to perform any part of the sequence itself.
// ════════════════════════════════════════════════════════════════════════════════════════
const STOP_OK = {
  hold_verified: true, original_restored: true,
  active_hash_before: "sha256:revised00000000", original_hash: "sha256:original0000000",
  revised_hash: "sha256:revised00000000", rewind_verified: true, sequence_after: 0,
  replan_reset: true, experiment_cleared: true, authority_after: "OPERATOR",
  ready_for_start: true, outcome: "STOPPED",
};

// ── K1. In progress: Scout's phases, and no duplicate submission ────────────────────────
test("a Stop in flight shows Scout's own phases, never an optimistic success", () => {
  const expected = ["Stopping mission…", "Holding position…", "Restoring original mission…",
    "Rewinding mission…", "Verifying reset…"];
  const seen = ["STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_RESTORING_ORIGINAL",
    "STOP_REWINDING", "STOP_VERIFYING_RESET"].map((s) => STOP_TRANSITION_LABELS[s]);
  assert.deepEqual(seen, expected);

  for (const state of Object.keys(STOP_TRANSITION_LABELS)) {
    const card = missionCardView(S({ state, can_start: false }), {});
    assert.equal(card.working, true, state);
    assert.equal(card.headline, STOP_TRANSITION_LABELS[state], state);
    assert.equal(card.stopPhase, state, state);
    assert.equal(card.stopOutcome, null, `${state}: nothing is claimed before Scout finishes`);
    assert.deepEqual(card.buttons, [], `${state}: no control may be pressed mid-transaction`);
  }
});

test("before Scout has moved, the Stop line is the honest first step, not a placeholder", () => {
  const card = missionCardView(S({ state: "RUNNING", can_pause: true, can_stop: true }),
    { busy: true, stopping: true });
  assert.equal(card.working, true);
  assert.equal(card.headline, "Stopping mission…");
  assert.equal(card.stopPhase, "STOP_REQUESTED");
});

test("with no stop STATE published, the phase is derived from Scout's own evidence", () => {
  const after = (ev) => stopPhase("RUNNING",
    normalizeStatus({ scout: { state: "RUNNING", stop: ev } }).stop).text;
  assert.equal(after({ hold_verified: true }), "Restoring original mission…");
  assert.equal(after({ hold_verified: true, original_restored: true }), "Rewinding mission…");
  assert.equal(after({ hold_verified: true, rewind_verified: true }), "Verifying reset…");
  assert.equal(stopPhase("RUNNING", null).text, "Stopping mission…");
});

test("a Stop in progress disables every duplicate control", () => {
  const busy = lifecycleControls(S({ state: "RUNNING", can_pause: true, can_stop: true }),
    { busy: true });
  assert.equal(busy.buttons.length > 0, true);
  assert.equal(busy.buttons.every((b) => b.enabled === false), true);
  assert.equal(busy.buttons.every((b) => /already in progress/i.test(b.reason)), true);
  // …and the availability derivation agrees, so the Map and the Agent page cannot disagree.
  assert.equal(stopAvailability(S({ state: "RUNNING", can_stop: true }), { busy: true }).enabled,
    false);
});

// ── K2. Success: NOT_READY + start_eligible is EXPECTED, not a failure ──────────────────
test("a successful Stop reads as a stop, not as 'Not ready to start'", () => {
  const s = S({ state: "NOT_READY", can_start: false, mode: "LOITER", mission_id: "msn-1",
    start_eligible: true, authority_blocks_start: true, execution_ready: false,
    authority_status: "OPERATOR", stop: STOP_OK });
  const out = stopOutcomeView(s);
  assert.equal(out.ok, true);
  assert.equal(out.title, "Mission stopped");
  assert.deepEqual(out.lines, [
    "Vehicle held in LOITER",
    "Original mission restored",
    "Original mission reset to start",
    "Execution and replan test state cleared",
    "Operator authority restored",
    "Ready for a new Start",
  ]);
});

test("NOT_READY + start_eligible + authority_blocks_start after a Stop STILL offers Start", () => {
  // Scout's documented landing after a successful Stop. Authority is deliberately back with the
  // operator, and the Start transaction is what hands it to the Local Agent again — so this must
  // never read as a broken mission, and Start must stay available.
  const s = S({ state: "NOT_READY", can_start: false, mode: "LOITER", mission_id: "msn-1",
    start_eligible: true, authority_blocks_start: true, execution_ready: false,
    authority_status: "OPERATOR", stop: STOP_OK });
  const gate = startGate(s, { connected: true });
  assert.equal(gate.canStart, true, "the Start transaction performs the authority hand-off");
  assert.equal(gate.authorityWillBeAcquired, true);

  const ctl = lifecycleControls(s, {});
  assert.deepEqual(actions(ctl), ["start"]);
  assert.equal(byAction(ctl, "start").enabled, true);

  const card = missionCardView(s, { missionId: "msn-1" });
  assert.equal(card.stopOutcome.ok, true, "the abort is reported as a success");
  assert.equal(card.blocker, null, "a completed Stop is not a blocker");
  assert.notEqual(card.tone, "warn");
});

// ── K3. binding_state=UNBOUND before Start is HEALTHY, never a Start blocker ────────────
// The corrected Scout contract (Full Refresh alignment): binding_state=BOUND means a LIVE
// execution owns the mission identity — it is NOT a route-proof signal and is NEVER a Start
// precondition (that would be circular: BOUND only occurs AFTER Start). A completely healthy,
// verified, READY mission before Start correctly reports binding UNBOUND while
// verified_route_hash/state/start_eligible/can_start all say the route is proven.
test("a proven, READY, idle mission offers Start even though binding is UNBOUND", () => {
  const s = S({ state: "READY", can_start: true, mission_id: "msn-1",
    start_eligible: true, execution_ready: true, authority_blocks_start: false,
    binding: { binding_state: "UNBOUND", verified_route_hash: "sha256:aaaa",
      bound_original_mission_id: null } });
  assert.equal(s.binding.state, "UNBOUND");
  const gate = startGate(s, { connected: true });
  assert.equal(gate.canStart, true, "UNBOUND must never block a proven, eligible Start");

  const ctl = lifecycleControls(s, {});
  assert.deepEqual(actions(ctl), ["start"]);
  assert.equal(byAction(ctl, "start").enabled, true);
});

test("binding BOUND is expected only for a live execution, not a Start precondition", () => {
  // Start availability never REQUIRES binding_state === "BOUND" — the gate does not read the
  // binding STATE at all except to check for a blocking conflict (STALE_MISMATCH / an active
  // replacement conflict code), which UNBOUND is not.
  const unbound = startGate(S({ state: "READY", can_start: true, start_eligible: true,
    mission_id: "msn-1", binding: { binding_state: "UNBOUND" } }), { connected: true });
  const bound = startGate(S({ state: "READY", can_start: true, start_eligible: true,
    mission_id: "msn-1", binding: { binding_state: "BOUND" } }), { connected: true });
  assert.equal(unbound.canStart, true);
  assert.equal(bound.canStart, true);
});

test("a Stop with no revised route says so instead of claiming a restore that never happened", () => {
  const s = S({ state: "NOT_READY", mode: "LOITER", start_eligible: true,
    stop: { ...STOP_OK, original_restored: false, revised_hash: null } });
  const out = stopOutcomeView(s);
  assert.equal(out.ok, true);
  assert.equal(out.lines.includes("No revised route was installed"), true);
  assert.equal(out.lines.includes("Original mission restored"), false);
});

test("evidence Scout did not report is omitted, never guessed", () => {
  const s = S({ state: "NOT_READY", mode: "LOITER", stop: { hold_verified: true } });
  assert.deepEqual(stopOutcomeView(s).lines, ["Vehicle held in LOITER"]);
});

// ── K3. Failure: Scout's exact code, and the LOITER-safe statement ──────────────────────
for (const code of ["STOP_ACTIVE_MISSION_UNKNOWN", "STOP_RESTORE_UPLOAD_FAILED",
  "STOP_RESTORE_HASH_MISMATCH", "STOP_REWIND_NOT_VERIFIED"]) {
  test(`a Stop that failed with ${code} shows the exact code and the LOITER-safe state`, () => {
    const s = S({ state: "SUSPENDED", mode: "LOITER", can_start: false,
      last_error: { code, message: "scout detail" },
      stop: { hold_verified: true, original_restored: false, rewind_verified: false,
        outcome: code } });
    const view = interpretTransaction({ status: 200, data: {
      outcome: "failed", operation: "stop", scout_error_code: code,
      scout_error_message: "scout detail", phases: [], authority: {} } });
    const out = stopOutcomeView(s, view);
    assert.equal(out.ok, false);
    assert.equal(out.code, code);
    assert.equal(out.held, true);
    assert.match(out.text, /held in LOITER/);
    assert.match(out.text, /reset is incomplete/i);
    assert.doesNotMatch(out.text, /\[object Object\]/);
    // Scout's own code is preserved verbatim for the operator, alongside the readable text.
    assert.match(out.detail, new RegExp(code));
  });
}

test("a failed Stop states itself ONCE — the generic blocker does not repeat it", () => {
  const s = S({ state: "SUSPENDED", mode: "LOITER", can_start: false,
    last_error: { code: "STOP_REWIND_NOT_VERIFIED", message: "sequence read back as 4" },
    stop: { hold_verified: true, rewind_verified: false, outcome: "STOP_REWIND_NOT_VERIFIED" } });
  const card = missionCardView(s, {});
  assert.equal(card.stopOutcome.ok, false);
  assert.equal(card.blocker, null, "one failure, one place");
});

test("a failed Stop offers no automatic recovery — the operator decides", () => {
  const s = S({ state: "SUSPENDED", mode: "LOITER", can_start: false, can_stop: true,
    last_error: { code: "STOP_RESTORE_UPLOAD_FAILED" } });
  const ctl = lifecycleControls(s, {});
  // Rearm, Stop and Take Control are OFFERED as explicit operator choices. Nothing is auto-run:
  // the card model returns buttons, and pressing one is the operator's act.
  assert.deepEqual(actions(ctl), ["rearm", "stop", "take-control"]);
  assert.equal(ctl.buttons.every((b) => b.action !== "resume"), true, "never an auto-resume");
});

// ── K4. Evidence rendering: no [object Object], nothing dropped ─────────────────────────
test("Stop evidence renders as readable text, never [object Object]", () => {
  const s = S({ state: "NOT_READY", mode: "LOITER", stop: STOP_OK });
  const detail = stopEvidenceDetail(s.stop);
  assert.doesNotMatch(detail, /\[object Object\]/);
  for (const fragment of ["outcome STOPPED", "hold verified yes", "original restored yes",
    "rewind verified yes", "sequence after 0", "replan reset yes", "experiment cleared yes",
    "authority OPERATOR", "ready for start yes"]) {
    assert.equal(detail.includes(fragment), true, `${fragment} missing from: ${detail}`);
  }
  assert.equal(stopEvidenceDetail({ reported: false }), null);
});

test("a hostile stop block (nested objects) still renders readable text", () => {
  const s = S({ state: "NOT_READY", stop: { outcome: { code: "STOPPED", message: "done" },
    hold_verified: true, sequence_after: { current: 0 } } });
  assert.doesNotMatch(stopEvidenceDetail(s.stop), /\[object Object\]/);
  assert.doesNotMatch(JSON.stringify(stopOutcomeView(s)), /\[object Object\]/);
});

test("tri-state stop evidence keeps 'not reported' apart from 'false'", () => {
  const silent = S({ state: "NOT_READY", stop: { outcome: "STOPPED" } });
  assert.equal(silent.stop.rewindVerified, null, "Scout said nothing");
  const denied = S({ state: "NOT_READY", stop: { outcome: "STOPPED", rewind_verified: false } });
  assert.equal(denied.stop.rewindVerified, false, "Scout said no");
});

test("a Scout that reports no stop block produces no stop presentation at all", () => {
  const s = S({ state: "RUNNING", can_pause: true, can_stop: true });
  assert.equal(s.stop.reported, false);
  assert.equal(stopOutcomeView(s), null);
  assert.equal(missionCardView(s, {}).stopOutcome, null);
});

// ── K5. Source guards: the Operator performs NO part of Scout's stop sequence ───────────
test("Stop never invokes the raw Pixhawk stop anywhere in the app", () => {
  const libSrc = read("../operator/lib/mission-execution.js");
  // No CALL to it: the guard reads CODE, with comment lines stripped. A prose mention of what
  // the app deliberately does not do is fine — and is exactly what these modules carry.
  const code = (src) => src.split("\n")
    .filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join("\n");
  for (const [name, src] of [["Map", mapSrc], ["Agent", agentSrc], ["api", apiSrc],
    ["lib", libSrc]]) {
    assert.doesNotMatch(code(src), /nav\/stop/, `${name} must never call the raw stop`);
    assert.doesNotMatch(code(src), /navStop|rawStop/i, name);
  }
  assert.match(apiSrc, /mission-execution\/stop/, "the only Stop route is the lifecycle one");
});

test("Stop never independently calls LOITER, rearm, reset or upload from the Operator", () => {
  // The whole transaction is Scout's. The station sends ONE POST and re-reads status; a second
  // call sequenced around it would be a competing lifecycle.
  const start = mapSrc.indexOf('if (action === "stop")');
  const wiring = mapSrc.slice(start, mapSrc.indexOf('if (action === "rearm")', start));
  assert.equal(wiring.length > 0, true);
  assert.match(wiring, /api\.stopMissionExecution\(id\)/);
  for (const forbidden of [/SET_MODE_LOITER/, /api\.rearmMissionExecution/, /api\.setHome/,
    /api\.uploadMission/, /api\.createCommand/, /setControlAuthority/, /resetReplan/]) {
    assert.doesNotMatch(wiring, forbidden, String(forbidden));
  }
});

test("a restored original mission forces a fresh Pixhawk read-back, without recentring", () => {
  // Scout restores the immutable original and rewinds the sequence, so the overlay, the active
  // waypoint and the progress readout must come from ground truth rather than the cache.
  const at = mapSrc.indexOf("function stopChangedTheVehicleMission");
  assert.equal(at > 0, true);
  assert.match(mapSrc.slice(at, at + 700), /OUTCOME\.ACCEPTED/);
  const tx = mapSrc.slice(mapSrc.indexOf("function missionTransaction"), at);
  assert.match(tx, /refreshController\.refreshMission\(id, "stop"\)/);
  assert.match(tx, /loadMissionStatus\(id\)/);
  // The map is never recentred by a lifecycle transaction — centring is an explicit operator act.
  assert.doesNotMatch(tx, /centerMission\(|fitBounds/);
});

// ── K6. A stop block is EVIDENCE OF THE LAST STOP, not a permanent headline ─────────────
test("a stale stop block never narrates over a run that has since restarted", () => {
  // Scout's `stop` evidence persists in its status. Once the operator starts again, "Mission
  // stopped" beside a RUNNING mission would be the worst kind of stale claim.
  for (const state of ["RUNNING", "PAUSED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING",
    "COMPLETED_HOLD", "STARTING_AUTO"]) {
    const s = S({ state, can_start: false, stop: STOP_OK });
    assert.equal(stopOutcomeView(s), null, state);
    assert.equal(missionCardView(s, {}).stopOutcome, null, state);
  }
});

test("a SUSPENDED caused by a failed replan is not misread as a failed Stop", () => {
  // A leftover successful-stop block plus an unrelated SUSPENDED must not produce a stop
  // failure. Only a STOP_* code, or the stop transaction the operator just ran, does that.
  const s = S({ state: "SUSPENDED", can_start: false,
    last_error: { code: "REPLAN_FAILED", message: "safe return could not be planned" },
    stop: STOP_OK });
  assert.equal(stopOutcomeView(s), null);
  // …and the ordinary failure blocker is therefore still shown, from Scout's own last_error.
  const card = missionCardView(s, {});
  assert.equal(card.stopOutcome, null);
  assert.match(card.blocker.title, /REPLAN_FAILED/);
});

test("a SUSPENDED whose stop evidence carries a STOP_ code IS a failed Stop", () => {
  const s = S({ state: "SUSPENDED", mode: "LOITER", can_start: false,
    stop: { hold_verified: true, rewind_verified: false,
      outcome: "STOP_REWIND_NOT_VERIFIED" } });
  const out = stopOutcomeView(s);
  assert.equal(out.ok, false);
  assert.equal(out.code, "STOP_REWIND_NOT_VERIFIED");
});
