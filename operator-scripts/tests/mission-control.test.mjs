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
  lifecycleControls, stopAvailability, normalizeStatus, interpretTransaction,
  transactionSummary, outcomeLabel, OUTCOME, STATES, STOPPED_STATES,
} from "../operator/lib/mission-execution.js";
import { deploymentReadiness } from "../operator/lib/home.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");

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
  assert.equal(labels(ctl)[0], "Pause");
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

test("SUSPENDED shows the failure and offers Rearm plus Take Control", () => {
  const ctl = lifecycleControls(S({ state: "SUSPENDED", can_start: false,
    last_error: "replanning ended in SAFE_HOLD" }));
  assert.deepEqual(actions(ctl), ["rearm", "take-control"]);
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

// ── E. Stop: unsupported is EXPLICIT, never faked and never Rearm ───────────────────────
test("a Scout with no can_stop field reports Stop as UNSUPPORTED with a real reason", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_pause: true }));   // no can_stop key
  assert.equal(av.supported, false);
  assert.equal(av.enabled, false);
  assert.match(av.reason, /does not implement/i);
  assert.match(av.reason, /Rearm is not a substitute/);
  assert.match(av.reason, /Pause holds the mission without ending it/);
});

test("an unsupported Stop is still SHOWN — disabled with the reason, not hidden", () => {
  const ctl = lifecycleControls(S({ state: "RUNNING", can_start: false, can_pause: true }));
  const stop = byAction(ctl, "stop");
  assert.ok(stop, "Stop must remain visible so the operator can see why it is unavailable");
  assert.equal(stop.enabled, false);
  assert.match(stop.reason, /does not implement/i);
  assert.equal(ctl.stop.supported, false);
});

test("can_stop:false is 'not right now', which is a different message from 'no such endpoint'", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_pause: true, can_stop: false }));
  assert.equal(av.supported, true);
  assert.equal(av.enabled, false);
  assert.match(av.reason, /can_stop=false/);
  assert.doesNotMatch(av.reason, /does not implement/i);
});

test("can_stop:true enables Stop", () => {
  const av = stopAvailability(S({ state: "RUNNING", can_pause: true, can_stop: true }));
  assert.equal(av.supported, true);
  assert.equal(av.enabled, true);
  assert.equal(av.reason, null);
});

test("the STOP sequence states are modelled so none reads as an unknown state", () => {
  for (const s of ["STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED", "STOPPED"]) {
    assert.ok(STATES.includes(s), s);
  }
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
const mapSrc = read("../operator/pages/Map.js");
const agentSrc = read("../operator/pages/Agent.js");
const apiSrc = read("../operator/services/api.js");

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
  const card = mapSrc.slice(mapSrc.indexOf("function renderAgentMission"),
    mapSrc.indexOf("function renderAgentMission") + 4500);
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
