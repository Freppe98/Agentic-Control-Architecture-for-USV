// mission-lock.test.mjs — the bounded mission-write lock (operator/lib/mission-lock.js).
//
// The defect these pin: the Plan page's lock was an unbounded page-local flag cleared in
// exactly one place, so every path that never produced a terminal command record left the
// operator permanently unable to upload — with the backend queue empty and the vehicle
// disarmed and eligible.
import { test } from "node:test";
import assert from "node:assert/strict";
import { missionLockState, lockMessage, SUBMIT_TIMEOUT_MS, TRACKING_GRACE_MS } from "../operator/lib/mission-lock.js";
import { uploadEligibility } from "../operator/lib/upload-policy.js";

const T0 = 1_000_000;
const cmd = (over = {}) => ({
  id: "cmd-1", type: "MISSION_UPLOAD", status: "PENDING",
  result: null, lifecycle: [], ...over,
});
/** an EXECUTED upload whose read-back verified — what commandVerification calls verified */
const verified = () => cmd({
  status: "EXECUTED",
  result: { verified: true, uploaded: true, executed: true,
            expected_state: "MISSION_UPLOADED", observed_state: "MISSION_UPLOADED" },
  verification: { verified: true, outcome: "VERIFIED" },
});

test("idle when no upload is in progress", () => {
  for (const phase of ["idle", "uploaded", "error", undefined]) {
    const l = missionLockState({ phase, now: T0 });
    assert.equal(l.locked, false, `phase ${phase} must not lock`);
    assert.equal(l.state, "idle");
    assert.equal(l.release, null);
  }
});

// ── 1. a pending upload blocks another upload ──────────────────────────────────
test("a pending upload blocks another upload", () => {
  const l = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0,
    commands: [cmd({ status: "PENDING" })], now: T0 + 3000,
  });
  assert.equal(l.locked, true);
  assert.equal(l.state, "in_flight");
  assert.equal(l.release, null);
  // and the gate blocks with a SPECIFIC reason, not the generic sentence
  const gate = uploadEligibility({
    connected: true, armed: false, mode: "MANUAL", hasAuthority: true,
    missionPending: true, missionPendingReason: lockMessage(l),
  });
  assert.equal(gate.allowed, false);
  assert.match(gate.reason, /Mission upload/);
  assert.doesNotMatch(gate.reason, /Another mission operation/);
});

test("the in-flight label names the stage rather than saying 'another operation'", () => {
  const executing = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0, now: T0 + 3000,
    commands: [cmd({ status: "PENDING", lifecycle: [{ stage: "EXECUTING" }] })],
  });
  assert.match(executing.label, /executing/i);
  assert.match(lockMessage(executing), /wait for it to finish/);
});

// ── 2. a confirmed upload releases the lock ────────────────────────────────────
test("a confirmed (EXECUTED + read-back verified) upload releases the lock", () => {
  const l = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0,
    commands: [verified()], now: T0 + 5000,
  });
  assert.equal(l.locked, false);
  assert.equal(l.state, "settled");
  assert.equal(l.release, null, "the page's own sync turns this into phase 'uploaded'");
});

// ── 3. a rejected upload releases the lock ─────────────────────────────────────
test("a rejected or failed upload releases the lock", () => {
  for (const status of ["REJECTED", "FAILED"]) {
    const l = missionLockState({
      phase: "uploading", cmdId: "cmd-1", startedAt: T0,
      commands: [cmd({ status, reason: "Scout refused the mission." })], now: T0 + 5000,
    });
    assert.equal(l.locked, false, `${status} must not hold the lock`);
    assert.equal(l.state, "settled");
  }
});

// ── 4. a timed-out upload releases the lock ────────────────────────────────────
test("an EXPIRED command (the backend's own TTL) releases the lock", () => {
  const l = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0,
    commands: [cmd({ status: "EXPIRED" })], now: T0 + 400_000,
  });
  assert.equal(l.locked, false);
  assert.equal(l.state, "settled");
});

test("a finalize that never produced a command releases the lock after the submit timeout", () => {
  // The exact shape of the permanent lock: fetch rejected, so cmdId stayed null.
  const early = missionLockState({ phase: "uploading", cmdId: null, startedAt: T0, now: T0 + 2000 });
  assert.equal(early.locked, true, "still submitting — must block a double press");
  assert.equal(early.state, "submitting");
  assert.match(early.label, /Submitting/);

  const late = missionLockState({ phase: "uploading", cmdId: null, startedAt: T0, now: T0 + SUBMIT_TIMEOUT_MS + 1 });
  assert.equal(late.locked, false);
  assert.equal(late.state, "submit_timeout");
  assert.equal(late.release.phase, "error");
  assert.match(late.release.error, /never reached the operator backend/);
  assert.match(late.release.error, /nothing was sent to the vehicle/,
    "the operator must be told the vehicle was NOT touched");
});

// ── 5. a stale backend command after reload does not stay pending forever ──────
test("a tracked command the backend has no record of releases the lock after the grace", () => {
  const within = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0, commands: [], now: T0 + 5000,
  });
  assert.equal(within.locked, true, "the 3s poll may simply not have caught up yet");
  assert.equal(within.state, "submitting");

  const past = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0, commands: [], now: T0 + TRACKING_GRACE_MS + 1,
  });
  assert.equal(past.locked, false);
  assert.equal(past.state, "tracking_lost");
  assert.equal(past.release.phase, "error");
  assert.match(past.release.error, /no record of this upload/);
  assert.match(past.release.error, /re-check the Pixhawk mission/,
    "an upload whose fate is unknown must not be reported as harmless");
});

test("a queue holding only OTHER vehicles' commands does not keep the lock alive", () => {
  const l = missionLockState({
    phase: "uploading", cmdId: "cmd-1", startedAt: T0,
    commands: [cmd({ id: "someone-else", status: "PENDING" })], now: T0 + TRACKING_GRACE_MS + 1,
  });
  assert.equal(l.locked, false);
  assert.equal(l.state, "tracking_lost");
});

test("a fresh page (phase idle) is never locked by a stale backend record", () => {
  // A reload rebuilds the model at phase 'idle'. Anything still genuinely pending in the
  // BACKEND queue is the caller's separate hasPendingOfType() check — not this flag.
  const l = missionLockState({
    phase: "idle", cmdId: "cmd-1", startedAt: T0,
    commands: [cmd({ status: "PENDING" })], now: T0 + 999_999,
  });
  assert.equal(l.locked, false);
  assert.equal(l.state, "idle");
});

// ── 6. disarmed + MANUAL is eligible when nothing is pending ───────────────────
test("disarmed + MANUAL is eligible when no mission operation is pending", () => {
  const l = missionLockState({ phase: "uploaded", cmdId: "cmd-1", startedAt: T0, commands: [verified()], now: T0 + 60_000 });
  assert.equal(l.locked, false);
  const gate = uploadEligibility({
    connected: true, armed: false, mode: "MANUAL", modeFresh: true,
    hasAuthority: true, missionPending: l.locked, missionPendingReason: lockMessage(l),
  });
  assert.equal(gate.allowed, true, "the exact reported state: disarmed, MANUAL, queue clean");
  assert.match(gate.message, /disarmed/);
});

test("the generic message is still the fallback when no reason is supplied", () => {
  const gate = uploadEligibility({ connected: true, armed: false, hasAuthority: true, missionPending: true });
  assert.equal(gate.reason, "Another mission operation is already in progress.");
});
