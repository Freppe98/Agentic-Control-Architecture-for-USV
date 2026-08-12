// Unit tests for the replanning supervisory logic layer (operator/lib/replan.js) and the
// api.js wiring guards. Run: `node --test tests/` (or `npm test`).
//
// Two kinds, matching the rest of the suite: (1) pure-logic tests over lib/replan.js (no DOM,
// no fetch) — the FSM classification, the staged-execution mapping onto Scout's two flags, the
// real-execution safety interlock, status normalization, and injection payload building;
// (2) source guards (readFileSync) for the api.js wiring that has no DOM harness here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  STAGE, PACKAGE_CONSISTENT, FSM_TERMINAL, FSM_ACTIVE_ORDER,
  isTransactionActive, isTerminal, executionStage, stagePatch,
  realExecutionBlockers, canEnableRealExecution, normalizeTransition,
  normalizeReplanStatus, outcomeLabel, injectionPayload, injectionHasOverride,
  replanMapModel, actionRequestView,
} from "../operator/lib/replan.js";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

// ── FSM classification ────────────────────────────────────────────────────────────────
test("active FSM states are transaction-active; idle/terminal are not", () => {
  for (const s of FSM_ACTIVE_ORDER) assert.equal(isTransactionActive(s), true, s);
  assert.equal(isTransactionActive("MONITORING"), false);
  assert.equal(isTransactionActive("MONITORING_REVISED"), false);
  for (const s of FSM_TERMINAL) assert.equal(isTransactionActive(s), false, s);
});

test("terminal failure states are recognised as terminal", () => {
  for (const s of FSM_TERMINAL) assert.equal(isTerminal(s), true, s);
  assert.equal(isTerminal("MONITORING"), false);
  assert.equal(isTerminal("PLANNING"), false);
});

// ── Staged execution mapping onto Scout's two INDEPENDENT flags ─────────────────────────
test("executionStage reads the two flags without coupling them", () => {
  assert.equal(executionStage({ autonomous_execution_enabled: false, dry_run: true }), STAGE.DISABLED);
  assert.equal(executionStage({ autonomous_execution_enabled: false, dry_run: false }), STAGE.DISABLED);
  assert.equal(executionStage({ autonomous_execution_enabled: true, dry_run: true }), STAGE.DRY_RUN);
  assert.equal(executionStage({ autonomous_execution_enabled: true, dry_run: false }), STAGE.REAL);
});

test("stagePatch maps the ladder back onto the two flags, never touching RTL fallback", () => {
  assert.deepEqual(stagePatch(STAGE.DISABLED), { autonomous_execution_enabled: false });
  assert.deepEqual(stagePatch(STAGE.DRY_RUN), { autonomous_execution_enabled: true, dry_run: true });
  assert.deepEqual(stagePatch(STAGE.REAL), { autonomous_execution_enabled: true, dry_run: false });
  for (const p of [stagePatch(STAGE.DISABLED), stagePatch(STAGE.DRY_RUN), stagePatch(STAGE.REAL)])
    assert.equal("rtl_fallback_enabled" in p, false);
  assert.throws(() => stagePatch("NONSENSE"));
});

// ── Real-execution safety interlock (task Section 6) ────────────────────────────────────
test("real execution is allowed only when every precondition holds", () => {
  const ok = { injectionActive: false, transactionActive: false, packageConsistent: true, homeValid: true, authorityKnown: true };
  assert.deepEqual(realExecutionBlockers(ok), []);
  assert.equal(canEnableRealExecution(ok), true);
});

test("an active forced-return injection blocks real execution (the forbidden sequence)", () => {
  const blockers = realExecutionBlockers({ injectionActive: true, transactionActive: false, packageConsistent: true, homeValid: true, authorityKnown: true });
  assert.equal(blockers.length, 1);
  assert.match(blockers[0], /injection/i);
  assert.equal(canEnableRealExecution({ injectionActive: true, packageConsistent: true, homeValid: true }), false);
});

test("active transaction, inconsistent package, invalid Home each block real execution", () => {
  assert.equal(canEnableRealExecution({ transactionActive: true, packageConsistent: true, homeValid: true }), false);
  assert.equal(canEnableRealExecution({ packageConsistent: false, homeValid: true }), false);
  assert.equal(canEnableRealExecution({ packageConsistent: true, homeValid: false }), false);
  assert.equal(canEnableRealExecution({ packageConsistent: true, homeValid: true, authorityKnown: false }), false);
});

// ── Status normalization (Scout's word, verbatim, missing→null) ─────────────────────────
test("normalizeReplanStatus reads the canonical object and classifies the transaction", () => {
  const scout = {
    fsm_state: "PLANNING", current_step: "generate_route", transition_id: "tx-9",
    revision: 1, strategy: "SAFE_RETURN", retry_count: 0,
    decision: "SAFE_RETURN", reason_codes: ["ENERGY_MARGIN_LOW"],
    action_request: "REQUEST_RETURN_HOME",
    snapshot_id: "snap-1", energy_calculation: { margin_percent: -5 },
    original_mission_hash: "sha256:aaa", revised_mission_hash: "sha256:bbb",
    package_consistency: PACKAGE_CONSISTENT,
    geometry_validation: { boundary_available: true, connector_proven_safe: true },
    transition_history: [{ timestamp: "t0", from: "MONITORING", to: "HOLD_REQUESTED", reason: "ENERGY", transition_id: "tx-1" }],
    obstacle_execution_enabled: false,
  };
  const n = normalizeReplanStatus({ scout, supported: true, reachable: true });
  assert.equal(n.present, true);
  assert.equal(n.transaction.active, true);
  assert.equal(n.transaction.terminal, false);
  assert.equal(n.decision.decision, "SAFE_RETURN");
  assert.deepEqual(n.decision.reasonCodes, ["ENERGY_MARGIN_LOW"]);
  assert.equal(n.decision.actionRequest, "REQUEST_RETURN_HOME");
  assert.equal(n.package.consistent, true);
  assert.equal(n.missionRevision.originalHash, "sha256:aaa");
  assert.equal(n.transitions.length, 1);
  assert.equal(n.transitions[0].to, "HOLD_REQUESTED");
  assert.equal(n.execution.obstacleExecutionEnabled, false);
});

// ── CONTRACT: the FINAL Scout implementation added ActionRequest to
// replan_controller.status() — i.e. `action_request` is a top-level field of the SAME body
// as fsm_state / current_decision / reason_codes / current_step / strategy, all published
// together on `GET /agent/replan/status`. This pins that exact shape so a future Scout
// response that moves the field elsewhere fails this test instead of silently going unread.
test("contract: action_request is consumed from the same replan_controller.status() body as "
  + "fsm_state/current_decision/reason_codes/current_step/strategy", () => {
  const scout = {
    fsm_state: "HOLD_REQUESTED",
    current_step: "verify_hold",
    current_decision: "SAFE_RETURN",
    reason_codes: ["ENERGY_MARGIN_LOW", "COMMUNICATION_DEGRADED"],
    strategy: "SAFE_RETURN",
    action_request: "REQUEST_HOLD",
  };
  const n = normalizeReplanStatus({ scout, supported: true, reachable: true });
  assert.equal(n.transaction.fsmState, "HOLD_REQUESTED");
  assert.equal(n.transaction.currentStep, "verify_hold");
  assert.equal(n.transaction.strategy, "SAFE_RETURN");
  assert.equal(n.decision.decision, "SAFE_RETURN");
  assert.deepEqual(n.decision.reasonCodes, ["ENERGY_MARGIN_LOW", "COMMUNICATION_DEGRADED"]);
  assert.equal(n.decision.actionRequest, "REQUEST_HOLD");
  const act = actionRequestView(n);
  assert.equal(act.reported, true);
  assert.equal(act.code, "REQUEST_HOLD");
  assert.equal(act.text, "REQUEST HOLD");
});

test("actionRequestView reads ONLY normalizeReplanStatus().decision.actionRequest — a body "
  + "with every other replan field but no action_request reads not-reported, never inferred "
  + "from decision/reason_codes/fsm_state", () => {
  const n = normalizeReplanStatus({
    scout: {
      fsm_state: "HOLD_REQUESTED", current_decision: "SAFE_RETURN",
      reason_codes: ["ENERGY_MARGIN_LOW"], current_step: "verify_hold", strategy: "SAFE_RETURN",
    },
    supported: true, reachable: true,
  });
  const act = actionRequestView(n);
  assert.equal(act.reported, false);
  assert.equal(act.code, null);
  assert.equal(act.text, "—");
});

test("actionRequestView on an unsupported/unreachable replan status reads not-reported, "
  + "never a fabricated NONE", () => {
  assert.equal(actionRequestView(normalizeReplanStatus(null)).reported, false);
  assert.equal(actionRequestView(normalizeReplanStatus({ supported: false })).reported, false);
});

test("an older Scout (supported:false) normalizes to not-present without throwing", () => {
  const n = normalizeReplanStatus({ supported: false, reachable: true, outcome: "unsupported" });
  assert.equal(n.supported, false);
  assert.equal(n.present, false);
  assert.equal(n.transaction.active, false);
  assert.deepEqual(n.transitions, []);
});

test("simulated decision input is flagged from the simulation state", () => {
  const n = normalizeReplanStatus({ scout: { simulation_state: { active: true }, decision: "SAFE_RETURN" } });
  assert.equal(n.decision.simulated, true);
});

test("normalizeTransition marks a SIMULATED source", () => {
  assert.equal(normalizeTransition({ from: "A", to: "B", source: "SIMULATED" }).simulated, true);
  assert.equal(normalizeTransition({ from: "A", to: "B" }).simulated, false);
  assert.equal(normalizeTransition(null), null);
});

// ── Outcome labels + injection payload building ─────────────────────────────────────────
test("unknown outcome is labelled reconcile, never failed", () => {
  assert.match(outcomeLabel("unknown"), /reconcile/i);
  assert.equal(outcomeLabel("accepted"), "Accepted");
  assert.equal(outcomeLabel("rejected"), "Rejected");
});

test("injectionPayload drops empty fields and requires at least one override", () => {
  assert.deepEqual(injectionPayload({ forceSafeReturn: true }), { force_safe_return: true });
  assert.deepEqual(injectionPayload({ batteryPercent: 10, durationS: 300 }),
    { battery_percent: 10, duration_s: 300 });
  assert.equal(injectionHasOverride(injectionPayload({})), false);
  assert.equal(injectionHasOverride(injectionPayload({ energyMarginPercent: -5 })), true);
});

// ── Map model (task Section 8) ──────────────────────────────────────────────────────────
test("map model draws original + active, never an obstacle layer", () => {
  const n = normalizeReplanStatus({ scout: { fsm_state: "MONITORING" } });
  const m = replanMapModel(n, {
    original: { route_hash: "sha256:aaa", mission_revision: 0, route_waypoints: [{ latitude: 56, longitude: 12 }] },
    active: { route_content_hash: "sha256:aaa", waypoints: [{ lat: 56, lng: 12 }] },
  });
  const kinds = m.layers.map((l) => l.kind);
  assert.deepEqual(kinds, ["original", "active"]);
  assert.equal(m.obstacleLayer, false);
  assert.equal(m.contradiction, false);
});

test("map model flags a contradiction when the live route disagrees with the current revision", () => {
  const n = normalizeReplanStatus({ scout: { fsm_state: "MONITORING_REVISED", revision: 1,
    revised_mission_hash: "sha256:revised", strategy: "SAFE_RETURN" } });
  const m = replanMapModel(n, {
    original: { route_hash: "sha256:orig", mission_revision: 0, route_waypoints: [{ latitude: 56, longitude: 12 }] },
    active: { route_content_hash: "sha256:orig", waypoints: [{ lat: 56, lng: 12 }] },  // Pixhawk not caught up
  });
  assert.equal(m.contradiction, true);
  assert.equal(m.authoritativeActiveHash, "sha256:revised");
});

test("map model does not fabricate a revised route it has no geometry for", () => {
  const n = normalizeReplanStatus({ scout: { revision: 1, revised_mission_hash: "sha256:revised" } });
  const m = replanMapModel(n, { original: { route_hash: "sha256:orig", route_waypoints: [{ latitude: 56, longitude: 12 }] } });
  assert.equal(m.revisedAvailable, false);
  assert.equal(m.layers.some((l) => l.kind === "revised"), false);
});

test("map model surfaces geometry status without inventing geometry", () => {
  const n = normalizeReplanStatus({ scout: { geometry_validation: {
    boundary_available: true, boundary_checked: true, connector_proven_safe: false,
    shoreline_clearance_available: false } } });
  const m = replanMapModel(n, {});
  assert.equal(m.geometry.boundaryChecked, true);
  assert.equal(m.geometry.connectorProvenSafe, false);
  assert.equal(m.geometry.shorelineClearanceScalarOnly, true);
});

// ── api.js wiring guards ────────────────────────────────────────────────────────────────
test("api.js exposes per-vehicle replan methods on the /api/vehicles/{id}/replan/* surface", () => {
  const src = read("../operator/services/api.js");
  for (const fn of ["getReplanStatus", "getReplanReadiness", "getReplanConfig", "patchReplanConfig",
    "getReplanPackage", "putReplanPackage", "deleteReplanPackage", "getReplanExperiment",
    "putReplanExperiment", "deleteReplanExperiment", "resetReplanController", "getReplanOperations"]) {
    assert.match(src, new RegExp(`export function ${fn}\\b`), `missing ${fn}`);
  }
  assert.match(src, /\/api\/vehicles\/\$\{id\}\/replan\/status/);
  assert.match(src, /\/api\/vehicles\/\$\{id\}\/replan\/planning-package/);
});

// ── replan-planning-package-v1: the manual sync must stay MANUAL ────────────────────────
// The whole safety property of the sync is that it is operator-initiated. A poll that writes
// would resend the approved package on every page refresh and on every reconnect, so these
// guards pin the wiring at the source level (there is no DOM harness for Agent.js here).
test("api.js exposes syncReplanPackage as a POST to the explicit sync route", () => {
  const src = read("../operator/services/api.js");
  assert.match(src, /export function syncReplanPackage\b/);
  assert.match(src, /syncReplanPackage\(id, body = \{\}\) \{ return postJSON\(`\/api\/vehicles\/\$\{id\}\/replan\/planning-package\/sync`/);
});

test("no automatic sync on an ordinary page refresh: the poll path never calls it", () => {
  const src = read("../operator/pages/Agent.js");
  // The replan poller and its interval exist …
  assert.match(src, /function loadReplan\(id\)/);
  assert.match(src, /setInterval\(\(\) => \{ if \(!replanBusy\) loadReplan\(selId\); \}/);
  // … and loadReplan issues reads only — no sync, no package write, of any spelling.
  const body = src.slice(src.indexOf("function loadReplan(id)"),
    src.indexOf("function loadMissionExecution(id)"));
  for (const forbidden of ["syncReplanPackage", "putReplanPackage", "deleteReplanPackage",
    "putReplanExperiment", "patchReplanConfig", "resetReplanController"]) {
    assert.equal(body.includes(forbidden), false, `loadReplan must not call ${forbidden}`);
  }
  // No interval anywhere in the page drives a sync either.
  for (const m of src.matchAll(/setInterval\(([\s\S]{0,160}?)\d+\s*\)/g)) {
    assert.equal(m[1].includes("syncReplanPackage"), false, "an interval must never sync");
  }
});
