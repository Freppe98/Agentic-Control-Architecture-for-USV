// mission-publish.test.mjs — the Plan page's publication view and the ONE readiness vocabulary
// Map and Agent both render. Pure module: no DOM, no fetch. Run with `npm test`.
//
// These tests exist because of one specific defect and one specific class of lie:
//
//   the defect  the Plan page showed a final green "Uploaded & verified" as soon as the flight
//               controller's read-back verified, while Scout still held the PREVIOUS mission's
//               planning package. Full success must be shown ONLY after Scout's package has been
//               read back and proven to match, and the middle case — flight controller verified,
//               package owed — must be its own visible, actionable outcome.
//
//   the lie     "the planning package is not consistent with the approved mission", shown for a
//               Scout that was merely re-deriving its readiness or could not be reached at all.
//               A refresh is not a mismatch and an unasked question is not a disagreement.

import test from "node:test";
import assert from "node:assert/strict";

import {
  publishView, phaseText, pixhawkVerified, readinessLabel, operatorIdentityProven,
  OUTCOME, PUBLISH_STATE, READINESS_STATE, READINESS_TEXT, SCOUT_REFRESHING, PHASES,
} from "../operator/lib/mission-publish.js";

const ok = (phase) => ({ phase, status: "ok", detail: null, code: null });
const failed = (phase, code) => ({ phase, status: "failed", detail: null, code });
const pending = (phase, code) => ({ phase, status: "pending", detail: null, code });

/** A READY envelope, as the backend emits it. */
function readyEnv(over = {}) {
  return {
    ok: true, operation: "publish", vehicle_id: "usv-2", mission_id: "msn-abc",
    phase: "READY", state: "READY", error: null, message: null, idempotent: false,
    expected_route_hash: "sha256:aaa", expected_route_count: 14,
    phases: PHASES.map(ok),
    final: { mission_id_match: true, hash_match: true, count_match: true, agent_ready: true },
    ...over,
  };
}

/** A publish that verified the flight controller but could not complete the package sync. */
function syncRequiredEnv(over = {}) {
  return {
    ok: false, operation: "publish", vehicle_id: "usv-2", mission_id: "msn-abc",
    phase: "SYNCING_SCOUT_PACKAGE", state: PUBLISH_STATE.PACKAGE_SYNC_REQUIRED,
    error: "SCOUT_PACKAGE_POST_FAILED", message: "Scout refused the planning package.",
    idempotent: false, expected_route_hash: "sha256:aaa", expected_route_count: 14,
    phases: [ok("VALIDATING_PLAN"), ok("UPLOADING_PIXHAWK"), ok("VERIFYING_PIXHAWK"),
             ok("PERSISTING_OPERATOR_MISSION"), ok("BUILDING_PLANNING_PACKAGE"),
             failed("SYNCING_SCOUT_PACKAGE", "SCOUT_PACKAGE_POST_FAILED")],
    final: { mission_id_match: null, hash_match: null, count_match: null, agent_ready: false },
    ...over,
  };
}

// ── The Plan page's three endings ──────────────────────────────────────────────────────────
test("full success is shown ONLY when the Agent package has been verified", () => {
  const view = publishView(readyEnv());
  assert.equal(view.kind, OUTCOME.OK);
  assert.equal(view.agentReady, true);
  assert.equal(view.canRetrySync, false);
  assert.equal(view.headline, "Mission uploaded and Agent package synchronized");
});

test("a verified Pixhawk upload alone is NOT full success", () => {
  // The exact regression: every phase up to and including the flight controller passed, and the
  // page must still not go green.
  const view = publishView(syncRequiredEnv());
  assert.notEqual(view.kind, OUTCOME.OK);
  assert.equal(view.agentReady, false);
});

test("Pixhawk verified + Scout sync failed shows the partial outcome and offers a retry", () => {
  const view = publishView(syncRequiredEnv());
  assert.equal(view.kind, OUTCOME.PARTIAL);
  assert.equal(view.canRetrySync, true);
  assert.match(view.headline, /Agent package synchronization required/);
  assert.match(view.headline, /uploaded to Pixhawk/);
});

test("an unreachable Scout after a verified upload is still the partial outcome", () => {
  const view = publishView(syncRequiredEnv({
    state: PUBLISH_STATE.SCOUT_UNREACHABLE, error: "SCOUT_UNREACHABLE" }));
  assert.equal(view.kind, OUTCOME.PARTIAL);
  assert.equal(view.canRetrySync, true);
});

test("a Pixhawk verification failure is a failure, and offers NO package retry", () => {
  const view = publishView({
    state: PUBLISH_STATE.REAL_MISMATCH, phase: "VERIFYING_PIXHAWK",
    error: "PIXHAWK_HASH_MISMATCH", message: "The route on the flight controller does NOT match.",
    phases: [ok("VALIDATING_PLAN"), ok("UPLOADING_PIXHAWK"),
             failed("VERIFYING_PIXHAWK", "PIXHAWK_HASH_MISMATCH")],
    final: { agent_ready: false },
  });
  assert.equal(view.kind, OUTCOME.FAILED);
  assert.equal(view.canRetrySync, false);
  assert.equal(view.headline, "Mission upload could not be verified");
});

test("a queued Pixhawk upload reads as neutral progress, never as an error", () => {
  const view = publishView({
    state: PUBLISH_STATE.UPLOAD_IN_PROGRESS, phase: "UPLOADING_PIXHAWK",
    error: "PIXHAWK_UPLOAD_PENDING",
    phases: [ok("VALIDATING_PLAN"), pending("UPLOADING_PIXHAWK", "PIXHAWK_UPLOAD_PENDING")],
    final: { agent_ready: false },
  });
  assert.equal(view.kind, OUTCOME.PROGRESS);
  assert.equal(view.headline, "Uploading mission to Pixhawk…");
  assert.equal(view.canRetrySync, false);
});

test("every phase has neutral progress text, and an unknown phase does not invent one", () => {
  assert.equal(phaseText("VERIFYING_PIXHAWK"), "Verifying Pixhawk readback…");
  assert.equal(phaseText("PERSISTING_OPERATOR_MISSION"), "Saving active mission…");
  assert.equal(phaseText("SYNCING_SCOUT_PACKAGE"), "Synchronizing Agent planning package…");
  assert.equal(phaseText("VERIFYING_SCOUT_PACKAGE"), "Verifying Agent package…");
  assert.equal(phaseText("NOT_A_PHASE"), "Publishing mission…");
  // None of the in-flight lines is phrased as a warning.
  for (const p of PHASES) assert.doesNotMatch(phaseText(p), /fail|error|mismatch|not /i);
});

test("pixhawkVerified reads the phase list, so a later failure cannot retract the proof", () => {
  assert.equal(pixhawkVerified(syncRequiredEnv()), true);
  assert.equal(pixhawkVerified({ phases: [ok("VALIDATING_PLAN")] }), false);
  assert.equal(pixhawkVerified({ phases: [failed("VERIFYING_PIXHAWK", "X")] }), false);
  assert.equal(pixhawkVerified(null), false);
});

test("publishView carries the identity facts and the idempotency flag through", () => {
  const view = publishView(readyEnv({ idempotent: true }));
  assert.equal(view.missionId, "msn-abc");
  assert.equal(view.routeHash, "sha256:aaa");
  assert.equal(view.routeCount, 14);
  assert.equal(view.idempotent, true);
  assert.equal(publishView(null), null);
});

// ── The shared readiness vocabulary ────────────────────────────────────────────────────────
const pkg = (over = {}) => ({
  planning_package: {
    scout_reachable: true, stored: true, usable: true,
    mission_id: "msn-abc", mission_id_match: true, hash_match: true, hash_mismatch: false,
    scout_state: "REPLANNING_READY", ...over },
  vehicle_mission: { mission_id: "msn-abc", route_hash: "sha256:aaa", readback_hash_match: true },
});
const pub = (over = {}) => ({ mission_id: "msn-abc", route_hash: "sha256:aaa",
                              package_sync_state: "SYNCED", ...over });

test("all three identities proven equal reads READY", () => {
  const v = readinessLabel({ publish: pub(), readiness: pkg() });
  assert.equal(v.state, READINESS_STATE.READY);
  assert.equal(v.text, READINESS_TEXT.READY);
});

test("REPLANNING_READINESS_REFRESHING is neutral verification text, NOT a mismatch", () => {
  const v = readinessLabel({
    publish: pub(), readiness: pkg({ scout_state: SCOUT_REFRESHING, hash_match: false }) });
  assert.equal(v.state, READINESS_STATE.VERIFYING);
  assert.equal(v.text, "Verifying Agent readiness…");
  assert.doesNotMatch(v.text, /mismatch|not consistent/i);
});

test("a passive refresh never overwrites known operator-level identity equality", () => {
  // The Operator can PROVE the record, its hash and the flight controller agree. A readiness
  // read that happens to be in flight must not turn that into a mismatch warning.
  const state = { publish: pub(), readiness: pkg() };
  assert.equal(operatorIdentityProven(state), true);
  const v = readinessLabel({ ...state, refreshing: true });
  assert.equal(v.state, READINESS_STATE.VERIFYING);
  assert.notEqual(v.state, READINESS_STATE.REAL_MISMATCH);
});

test("an unreachable Scout is SCOUT_UNREACHABLE, not a mismatch", () => {
  const v = readinessLabel({
    publish: pub(), readiness: pkg({ scout_reachable: false, mission_id: null,
                                     mission_id_match: false, hash_match: false }) });
  assert.equal(v.state, READINESS_STATE.SCOUT_UNREACHABLE);
});

test("an owed sync is PACKAGE_SYNC_REQUIRED, and carries the specific reason", () => {
  const v = readinessLabel({
    publish: pub({ package_sync_state: "REQUIRED", package_sync_error: "SCOUT_UNREACHABLE" }),
    readiness: pkg({ mission_id: null, mission_id_match: false, hash_match: false }) });
  assert.equal(v.state, READINESS_STATE.PACKAGE_SYNC_REQUIRED);
  assert.equal(v.detail, "SCOUT_UNREACHABLE");
});

test("a PROVEN id disagreement is a real mismatch", () => {
  const v = readinessLabel({
    publish: pub(), readiness: pkg({ mission_id: "msn-previous", mission_id_match: false }) });
  assert.equal(v.state, READINESS_STATE.REAL_MISMATCH);
  assert.match(v.detail, /different mission/);
});

test("a PROVEN hash disagreement is a real mismatch, and outranks an owed sync", () => {
  const v = readinessLabel({
    publish: pub({ package_sync_state: "REQUIRED" }),
    readiness: pkg({ hash_match: false, hash_mismatch: true }) });
  assert.equal(v.state, READINESS_STATE.REAL_MISMATCH);
});

test("mission_id_match:false with NO reported package id is unavailable, not a mismatch", () => {
  // Scout stored nothing, so it reported no id. The backend's `mission_id_match` is false only
  // because the comparison had no left-hand side — that is not evidence of disagreement.
  const v = readinessLabel({
    publish: pub({ package_sync_state: null }),
    readiness: pkg({ mission_id: null, mission_id_match: false, hash_match: false,
                     stored: false }) });
  assert.equal(v.state, READINESS_STATE.VERIFYING);
  assert.notEqual(v.state, READINESS_STATE.REAL_MISMATCH);
});

test("no active mission is NO_MISSION, not un-readiness", () => {
  const v = readinessLabel({ publish: { mission_id: null }, readiness: null });
  assert.equal(v.state, READINESS_STATE.NO_MISSION);
});

test("every readiness state has operator-facing text, and none of it says 'not consistent'", () => {
  for (const s of Object.values(READINESS_STATE)) {
    assert.equal(typeof READINESS_TEXT[s], "string");
    assert.ok(READINESS_TEXT[s].length > 0);
  }
  assert.doesNotMatch(READINESS_TEXT.VERIFYING, /not consistent|mismatch/i);
  assert.doesNotMatch(READINESS_TEXT.SCOUT_UNREACHABLE, /mismatch/i);
  assert.doesNotMatch(READINESS_TEXT.PACKAGE_SYNC_REQUIRED, /mismatch/i);
});

test("Map and Agent derive the SAME verdict from the same inputs", () => {
  // Both pages call this one function with the same two bodies, so the assertion that matters
  // is that the function is deterministic over them — there is no second derivation to drift.
  const inputs = { publish: pub({ package_sync_state: "REQUIRED" }), readiness: pkg() };
  assert.deepEqual(readinessLabel(inputs), readinessLabel(inputs));
  assert.equal(readinessLabel(inputs).state, READINESS_STATE.PACKAGE_SYNC_REQUIRED);
});

test("operatorIdentityProven is false unless the record, its hash and the FC all agree", () => {
  assert.equal(operatorIdentityProven({ publish: pub({ package_sync_state: "REQUIRED" }),
                                        readiness: pkg() }), false);
  assert.equal(operatorIdentityProven({
    publish: pub(),
    readiness: { vehicle_mission: { route_hash: "sha256:aaa", readback_hash_match: false } },
  }), false);
  assert.equal(operatorIdentityProven({}), false);
});
