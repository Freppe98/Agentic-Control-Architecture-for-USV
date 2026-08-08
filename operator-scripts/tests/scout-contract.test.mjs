// Frontend tests for the CURRENT Scout contract: explicit Start eligibility, the mission/package
// binding and its replacement conflicts, COMPLETED_HOLD, battery diagnostics, the replan trigger
// latch, and the revised-mission signal that wakes the Map overlay.
//
// Scout is the authority on all of it. These tests pin that the operator CONSUMES those fields
// rather than re-deriving them, and — just as importantly — that it stops making the two readings
// that were wrong on the bench:
//
//   1. treating "authority is not LOCAL_AGENT yet" as a broken/unprepared mission, when it is the
//      normal pre-Start state of a well-prepared one;
//   2. implying another automatic replan attempt is coming when Scout has already consumed that
//      trigger generation.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeStatus, startEligibility, startGate, primaryAction, lifecycleControls,
  missionCardView, startBlockers, bindingView, batteryView, isComplete,
  BINDING, PACKAGE_CONFLICT, MISSION_REPLACEMENT_BLOCKED_TEXT, START_ACQUIRES_AUTHORITY_NOTE,
} from "../operator/lib/mission-execution.js";
import { START_BLOCK } from "../operator/lib/mission-readiness.js";
import {
  normalizeReplanStatus, triggerLatch, cooldownView, missionRevisionSignal,
} from "../operator/lib/replan.js";
import { nextPublishAttempt, PUBLISH_RETRY } from "../operator/lib/mission-publish.js";

/** A Scout mission-execution status body, wrapped as api.js delivers it. */
const S = (body) => normalizeStatus({ supported: true, reachable: true, scout: body });

// ════════════════════════════════════════════════════════════════════════════════════════
// A. Start eligibility — Scout's explicit contract, and the authority deferral
// ════════════════════════════════════════════════════════════════════════════════════════
test("start_eligible=true with authority_blocks_start=true is ELIGIBLE, deferred on authority", () => {
  // THE case that used to read as a broken mission. Scout does not seize LOCAL_AGENT authority
  // itself, so this is the NORMAL pre-Start condition — the Start transaction takes control.
  const s = S({ state: "NOT_READY", can_start: false, authority_status: "OPERATOR",
    start_eligible: true, authority_blocks_start: true, execution_ready: false });
  const e = startEligibility(s);
  assert.equal(e.eligible, true);
  assert.equal(e.deferredOnAuthority, true);
  assert.equal(e.executionReady, false);
  assert.equal(e.source, "scout");
});

test("Start is OFFERED while authority is OPERATOR, and the card says control will be taken", () => {
  const s = S({ state: "NOT_READY", can_start: false, authority_status: "OPERATOR",
    start_eligible: true, authority_blocks_start: true, mission_id: "msn-1" });
  const gate = startGate(s, { connected: true });
  assert.equal(gate.canStart, true, gate.reason || "");
  assert.equal(gate.authorityWillBeAcquired, true);

  assert.equal(primaryAction(s).action, "start");
  const card = missionCardView(s, {});
  assert.equal(card.authorityWillBeAcquired, true);
  assert.ok(card.buttons.find((b) => b.action === "start" && b.enabled));
});

test("AUTHORITY_NOT_LOCAL_AGENT is never presented as the blocker for an eligible mission", () => {
  const s = S({ state: "NOT_READY", can_start: false, authority_status: "OPERATOR",
    start_eligible: true, authority_blocks_start: true,
    start_block_reason: "AUTHORITY_NOT_LOCAL_AGENT", mission_id: "msn-1" });
  const gate = startGate(s, { connected: true });
  assert.equal(gate.canStart, true);
  const blockers = startBlockers(s);
  assert.equal(blockers.some((b) => /AUTHORITY_NOT_LOCAL_AGENT/.test(b)), false, blockers);
  // It is stated as information about what the press will do, not as something to go and fix.
  assert.deepEqual(blockers, [START_ACQUIRES_AUTHORITY_NOTE]);
});

test("execution_ready=true is eligible and NOT deferred — Scout is ready under LOCAL_AGENT", () => {
  const e = startEligibility(S({ state: "READY", authority_status: "LOCAL_AGENT",
    start_eligible: true, authority_blocks_start: false, execution_ready: true }));
  assert.deepEqual([e.eligible, e.deferredOnAuthority, e.executionReady], [true, false, true]);
});

test("start_eligible=false blocks, with SCOUT'S OWN reason — never a re-derived one", () => {
  const s = S({ state: "NOT_READY", can_start: false, mission_id: "msn-1",
    start_eligible: false, authority_blocks_start: false,
    start_block_reason: "Planning package route hash does not match the loaded mission" });
  const e = startEligibility(s);
  assert.equal(e.eligible, false);
  assert.match(e.reason, /route hash does not match/);
  const gate = startGate(s, { connected: true });
  assert.equal(gate.canStart, false);
  assert.equal(gate.code, START_BLOCK.NOT_ELIGIBLE);
  assert.match(gate.detail, /route hash does not match/);
});

test("can_start alone no longer decides — the explicit contract wins in both directions", () => {
  // can_start:true but Scout says NOT eligible → blocked.
  const a = startEligibility(S({ state: "READY", can_start: true, start_eligible: false,
    start_block_reason: "no planning package" }));
  assert.equal(a.eligible, false);
  // can_start:false but Scout says eligible → offered.
  const b = startEligibility(S({ state: "NOT_READY", can_start: false, start_eligible: true }));
  assert.equal(b.eligible, true);
});

test("an older Scout without the contract keeps the previous can_start reading", () => {
  const e = startEligibility(S({ state: "READY", can_start: true }));
  assert.deepEqual([e.eligible, e.source], [true, "can_start"]);
  const d = startEligibility(S({ state: "NOT_READY", can_start: false,
    authority_status: "OPERATOR" }));
  assert.deepEqual([d.eligible, d.deferredOnAuthority, d.source], [true, true, "can_start"]);
});

test("the hard guards still fail closed even when Scout claims eligibility", () => {
  // A status that asserts BOTH "eligible" and "the replanning controller owns the vehicle" is
  // self-contradictory. A contradiction is not a permission.
  const replanning = startEligibility(S({ state: "RUNNING", start_eligible: true,
    replanning: { active: true, fsm_state: "PLANNING" } }));
  assert.equal(replanning.eligible, false);

  const busy = startEligibility(S({ state: "READY", start_eligible: true,
    active_operation_id: "op-9" }));
  assert.equal(busy.eligible, false);

  const disabled = startEligibility(S({ state: "READY", start_eligible: true,
    mission_execution_enabled: false }));
  assert.equal(disabled.eligible, false);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// B. Mission/package binding and replacement conflicts
// ════════════════════════════════════════════════════════════════════════════════════════
test("BOUND does not block anything", () => {
  const v = bindingView(S({ state: "RUNNING",
    binding: { binding_state: BINDING.BOUND, bound_original_mission_id: "msn-1",
      package_mission_id: "msn-1" } }));
  assert.equal(v.reported, true);
  assert.equal(v.blocksNewMission, false);
});

test("STALE_MISMATCH blocks a new mission and names the two real remedies", () => {
  const v = bindingView(S({ state: "RUNNING",
    binding: { binding_state: BINDING.STALE_MISMATCH, bound_original_mission_id: "msn-old",
      package_mission_id: "msn-new" } }));
  assert.equal(v.blocksNewMission, true);
  assert.equal(v.text, MISSION_REPLACEMENT_BLOCKED_TEXT);
  assert.match(v.text, /Finish or explicitly terminate\/rearm/);
});

test("STALE_PACKAGE_DURING_ACTIVE_EXECUTION surfaces a conflict, never a silent switch", () => {
  const s = S({ state: "RUNNING", mission_id: "msn-old", can_start: false,
    start_eligible: true,
    package_conflict: { code: PACKAGE_CONFLICT.STALE_PACKAGE_DURING_ACTIVE_EXECUTION,
      package_mission_id: "msn-new", bound_original_mission_id: "msn-old",
      execution_state: "RUNNING" } });
  const card = missionCardView(s, {});
  assert.ok(card.replacementConflict, "the conflict must be stated");
  assert.equal(card.replacementConflict.text, MISSION_REPLACEMENT_BLOCKED_TEXT);
  // It outranks every other blocker — it is why a just-uploaded mission is not flying.
  assert.equal(card.blocker.text, "Another mission is still active");
  // …and the new mission is NOT shown as ready.
  assert.equal(startGate(s, { connected: true }).canStart, false);
});

test("OPERATION_IN_PROGRESS is a conflict too, and Start stays unavailable", () => {
  const s = S({ state: "PAUSED", mission_id: "msn-old", start_eligible: true,
    package_conflict: { code: PACKAGE_CONFLICT.OPERATION_IN_PROGRESS } });
  assert.equal(bindingView(s).blocksNewMission, true);
  const gate = startGate(s, { connected: true });
  assert.equal(gate.canStart, false);
  assert.equal(gate.code, START_BLOCK.MISSION_REPLACEMENT_CONFLICT);
});

test("no Stop is invented for a conflict — only what Scout actually supports is offered", () => {
  const s = S({ state: "RUNNING", mission_id: "msn-old",
    binding: { binding_state: BINDING.STALE_MISMATCH } });
  const ctl = lifecycleControls(s, {});
  assert.equal(ctl.buttons.some((b) => b.action === "stop" && b.enabled), false);
  assert.equal(MISSION_REPLACEMENT_BLOCKED_TEXT.includes("Stop"), false);
});

test("a Scout that reports no binding at all changes nothing", () => {
  const v = bindingView(S({ state: "READY", can_start: true }));
  assert.deepEqual([v.reported, v.blocksNewMission], [false, false]);
  assert.equal(startGate(S({ state: "READY", can_start: true, mission_id: "m" }),
    { connected: true }).canStart, true);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// C. COMPLETED_HOLD — a finished mission reads as finished
// ════════════════════════════════════════════════════════════════════════════════════════
test("COMPLETED_HOLD with a verified final LOITER renders as COMPLETED / Mission finished", () => {
  const s = S({ state: "COMPLETED_HOLD", mission_id: "msn-1",
    return_completion: { arrival_confirmed: true, final_loiter_verified: true } });
  const card = missionCardView(s, {});
  assert.equal(card.chip, "COMPLETED");
  assert.equal(card.headline, "Mission finished");
  assert.equal(card.completionNote.text, "Final LOITER verified");
  assert.equal(card.complete, true);
  assert.equal(card.tone, "ok");
});

test("a completed run is never shown as RUNNING or PAUSED, and offers Rearm next", () => {
  const s = S({ state: "COMPLETED_HOLD", can_pause: false, can_resume: false,
    return_completion: { final_loiter_verified: true } });
  const card = missionCardView(s, {});
  assert.equal(/RUNNING|PAUSED/.test(card.chip), false);
  assert.equal(card.buttons.some((b) => b.action === "pause" && b.enabled), false);
  assert.equal(card.buttons.some((b) => b.action === "resume" && b.enabled), false);
  assert.deepEqual(card.nextAction, { action: "rearm", label: "Rearm / prepare next mission" });
  // A fresh Start comes SECOND: the controller must be prepared first.
  const start = card.buttons.find((b) => b.action === "start");
  assert.equal(start.enabled, false);
});

test("reaching the final waypoint is NOT completion — only COMPLETED_HOLD + final LOITER is", () => {
  // At the last waypoint, arrival confirmed, persistence full — still not complete.
  const nearly = S({ state: "RETURNING_HOME", sequence: { current: 13, count: 14 },
    return_completion: { arrival_confirmed: true, persistence_s: 10,
      persistence_progress_s: 10, final_loiter_verified: false } });
  assert.equal(isComplete(nearly), false);
  assert.notEqual(missionCardView(nearly, {}).chip, "COMPLETED");

  // COMPLETED_HOLD without the LOITER evidence: shown as completed-state, with the gap stated.
  const unverified = S({ state: "COMPLETED_HOLD",
    return_completion: { final_loiter_verified: false } });
  const card = missionCardView(unverified, {});
  assert.equal(isComplete(unverified), false);
  assert.equal(card.chip, "COMPLETED");
  assert.match(card.headline, /final LOITER not verified/i);
  assert.equal(card.completionNote.tone, "warn");
});

// ════════════════════════════════════════════════════════════════════════════════════════
// D. Battery diagnostics — never render "unknown" as 0%
// ════════════════════════════════════════════════════════════════════════════════════════
test("battery_valid=false with raw -1 is unavailable, NOT 0%", () => {
  const v = batteryView(S({ state: "READY", battery_diagnostics: {
    battery_percent: -1, battery_valid: false, battery_raw: -1,
    battery_observed_at: "2026-08-07T10:00:00Z", telemetry_age_s: 2.5 } }));
  assert.equal(v.known, false);
  assert.equal(v.percent, null);
  assert.equal(v.text, "Battery telemetry temporarily unavailable");
  assert.equal(/\b0\s*%/.test(v.text), false);
  assert.match(v.detail, /raw -1/);
});

test("a valid reading is reported as its percentage", () => {
  const v = batteryView(S({ state: "READY", battery_diagnostics: {
    battery_percent: 67, battery_valid: true, battery_raw: 67 } }));
  assert.deepEqual([v.known, v.percent, v.text], [true, 67, "67%"]);
});

test("a null raw is unknown too, and no percentage is invented", () => {
  const v = batteryView(S({ state: "READY", battery_diagnostics: {
    battery_percent: null, battery_valid: false, battery_raw: null } }));
  assert.equal(v.known, false);
  assert.equal(v.percent, null);
});

test("a Scout that reports no diagnostics says nothing rather than 0%", () => {
  const v = batteryView(S({ state: "READY" }));
  assert.deepEqual([v.known, v.percent, v.text], [false, null, null]);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// E. The replan trigger LATCH — a consumed generation is not a pending retry
// ════════════════════════════════════════════════════════════════════════════════════════
const R = (body) => normalizeReplanStatus({ supported: true, reachable: true, scout: body });

test("an active trigger whose generation is consumed does NOT promise another attempt", () => {
  const latch = triggerLatch(R({ fsm_state: "SAFE_HOLD", cooldown_s: 45,
    trigger_active: true, trigger_generation: 3, consumed_trigger_generation: 3,
    trigger_consumed: true, terminal_reason: "SAFE_HOLD" }));
  assert.equal(latch.active, true);
  assert.equal(latch.consumed, true);
  assert.equal(latch.willRetryAutomatically, false);
  assert.equal(latch.rearmRequired, true);
  assert.match(latch.headline, /ACTIVE, generation consumed/);
  assert.match(latch.detail, /Attempt 3 consumed/);
  assert.match(latch.detail, /Outcome: SAFE_HOLD/);
  assert.match(latch.detail, /Re-arm required/);
});

test("the cooldown is explicitly NOT a countdown once the generation is spent", () => {
  const cd = cooldownView(R({ fsm_state: "SAFE_HOLD", cooldown_s: 45,
    trigger_active: true, trigger_generation: 3, consumed_trigger_generation: 3 }));
  assert.equal(cd.seconds, 45);
  assert.equal(cd.countsDownToRetry, false);
  assert.match(cd.text, /not a pending retry/);
});

test("an active, UNSPENT trigger on a live controller does still count down", () => {
  const st = R({ fsm_state: "MONITORING", cooldown_s: 12,
    trigger_active: true, trigger_generation: 4, consumed_trigger_generation: 3 });
  assert.equal(triggerLatch(st).willRetryAutomatically, true);
  const cd = cooldownView(st);
  assert.equal(cd.countsDownToRetry, true);
  assert.match(cd.text, /until the next attempt/);
});

test("consumption is inferred from the generation pair when the flag is absent", () => {
  const latch = triggerLatch(R({ fsm_state: "FAILED", trigger_active: true,
    trigger_generation: 2, consumed_trigger_generation: 2 }));
  assert.equal(latch.consumed, true);
  assert.equal(latch.willRetryAutomatically, false);
});

test("a missing generation pair is not evidence of consumption in either direction", () => {
  const latch = triggerLatch(R({ fsm_state: "MONITORING", trigger_active: true }));
  assert.equal(latch.consumed, false);
  assert.equal(latch.willRetryAutomatically, true);
});

test("a Scout that reports no trigger fields renders no latch at all", () => {
  assert.equal(triggerLatch(R({ fsm_state: "MONITORING" })).reported, false);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// F. The revised-mission signal that wakes the Map overlay
// ════════════════════════════════════════════════════════════════════════════════════════
test("a changed revised_mission_hash changes the signal", () => {
  const a = missionRevisionSignal({ replan: R({ revised_mission_hash: "sha256:aaa", revision: 1 }) });
  const b = missionRevisionSignal({ replan: R({ revised_mission_hash: "sha256:bbb", revision: 1 }) });
  assert.notEqual(a, b);
  assert.ok(a);
});

test("a changed revision number changes the signal", () => {
  const a = missionRevisionSignal({ replan: R({ revised_mission_hash: "sha256:aaa", revision: 1 }) });
  const b = missionRevisionSignal({ replan: R({ revised_mission_hash: "sha256:aaa", revision: 2 }) });
  assert.notEqual(a, b);
});

test("a VERIFIED readback changes the signal; an in-flight one does not", () => {
  const base = { revised_mission_hash: "sha256:aaa", revision: 1 };
  const pending = missionRevisionSignal({ replan: R({ ...base, readback_result: "PENDING" }) });
  const inflight = missionRevisionSignal({ replan: R({ ...base, readback_result: "IN_PROGRESS" }) });
  const verified = missionRevisionSignal({ replan: R({ ...base, readback_result: "VERIFIED" }) });
  assert.equal(pending, inflight, "a non-verified readback is not a redraw trigger");
  assert.notEqual(verified, pending);
});

test("entering MONITORING_REVISED changes the signal", () => {
  const monitoring = missionRevisionSignal({ replan: R({ fsm_state: "MONITORING" }) });
  const revised = missionRevisionSignal({ replan: R({ fsm_state: "MONITORING_REVISED" }) });
  assert.notEqual(monitoring, revised);
});

test("a changed active_route_hash on the mission-execution status changes the signal", () => {
  const before = missionRevisionSignal({
    missionExecution: { scout: { state: "RUNNING", active_route_hash: "sha256:orig" } } });
  const after = missionRevisionSignal({
    missionExecution: { scout: { state: "RETURNING_HOME", active_route_hash: "sha256:revised" } } });
  assert.notEqual(before, after);
});

test("unchanged evidence yields an unchanged signal — no download loop", () => {
  const body = { scout: { state: "RUNNING", active_route_hash: "sha256:same" } };
  assert.equal(missionRevisionSignal({ missionExecution: body }),
    missionRevisionSignal({ missionExecution: body }));
});

test("no evidence at all leaves the trigger dormant rather than firing on a fabrication", () => {
  assert.equal(missionRevisionSignal({}), undefined);
  assert.equal(missionRevisionSignal({ replan: R({}), missionExecution: null, vehicle: null }),
    undefined);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// G. Publish carries itself to a verdict — no manual package-sync step
// ════════════════════════════════════════════════════════════════════════════════════════
test("an unfinished publish is retried; a decided one is not", () => {
  for (const state of ["UPLOAD_IN_PROGRESS", "VERIFYING", "BUSY", "SCOUT_UNREACHABLE"]) {
    const n = nextPublishAttempt({ state, final: { agent_ready: false } }, 1);
    assert.equal(n.retry, true, state);
    assert.equal(n.delayMs, PUBLISH_RETRY.delayMs);
  }
  for (const state of ["READY", "REAL_MISMATCH", "PACKAGE_SYNC_REQUIRED", "BLOCKED"]) {
    assert.equal(nextPublishAttempt({ state, final: { agent_ready: false } }, 1).retry, false, state);
  }
});

test("a ready publish stops immediately", () => {
  const n = nextPublishAttempt({ state: "READY", final: { agent_ready: true } }, 1);
  assert.deepEqual([n.retry, n.reason], [false, "ready"]);
});

test("a request that never reached the backend is retried, then bounded", () => {
  assert.equal(nextPublishAttempt(null, 1).retry, true);
  assert.equal(nextPublishAttempt(null, PUBLISH_RETRY.maxAttempts).retry, false);
  assert.equal(nextPublishAttempt(null, PUBLISH_RETRY.maxAttempts).reason, "exhausted");
});

test("the retry budget is finite — it cannot poll Scout forever", () => {
  let attempt = 0;
  while (nextPublishAttempt({ state: "SCOUT_UNREACHABLE" }, ++attempt).retry) {
    assert.ok(attempt <= PUBLISH_RETRY.maxAttempts, "unbounded retry");
  }
  assert.equal(attempt, PUBLISH_RETRY.maxAttempts);
});
