// Unit tests for the shared command-lifecycle helpers (operator/lib/command.js).
// Run: `node --test tests/` (or `npm test`).
//
// These pin the two rules the Map and Vehicle command panels must apply identically:
//  1. commandVerification — EXECUTED is a success ONLY when the per-type verification
//     passed (SET_HOME home_result, RTL rtl_result); otherwise it is a failed attempt.
//  2. hasPendingOfType — duplicate-press suppression while a same-type command is
//     nonterminal (the LOITER safety-hold guard).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  commandVerification, hasPendingOfType, isNonterminal,
  commandSource, commandStages, outcomeFrom,
} from "../operator/lib/command.js";

// ---- commandVerification --------------------------------------------------

test("RTL EXECUTED + rtl_result confirmed → verified true (green)", () => {
  assert.equal(commandVerification({ type: "RTL", status: "EXECUTED", rtl_result: "confirmed" }).verified, true);
});

test("RTL EXECUTED + rtl_result failed → verified false (a failed attempt, not success)", () => {
  assert.equal(commandVerification({ type: "RTL", status: "EXECUTED", rtl_result: "failed" }).verified, false);
});

test("RTL EXECUTED with NO rtl_result → verified false (never optimistic on transport alone)", () => {
  // A non-conforming/legacy Local Agent that reports EXECUTED without the classifier
  // must not read as a confirmed RTL.
  assert.equal(commandVerification({ type: "RTL", status: "EXECUTED" }).verified, false);
});

test("RTL not-yet-EXECUTED (SENT) → verified null (rule not applicable yet)", () => {
  assert.equal(commandVerification({ type: "RTL", status: "SENT" }).verified, null);
});

test("SET_HOME EXECUTED + home_result verified → verified true", () => {
  assert.equal(commandVerification({ type: "SET_HOME", status: "EXECUTED", home_result: "verified" }).verified, true);
});

test("SET_HOME EXECUTED + home_result failed → verified false", () => {
  assert.equal(commandVerification({ type: "SET_HOME", status: "EXECUTED", home_result: "failed" }).verified, false);
});

test("LOITER EXECUTED → verified null (plain success; EXECUTED stands)", () => {
  assert.equal(commandVerification({ type: "SET_MODE_LOITER", status: "EXECUTED" }).verified, null);
});

test("AUTO/MANUAL EXECUTED → verified null (no separate verification)", () => {
  assert.equal(commandVerification({ type: "SET_MODE_AUTO", status: "EXECUTED" }).verified, null);
  assert.equal(commandVerification({ type: "SET_MODE_MANUAL", status: "EXECUTED" }).verified, null);
});

test("REJECTED/FAILED → verified null (the terminal status already carries the failure)", () => {
  assert.equal(commandVerification({ type: "RTL", status: "REJECTED" }).verified, null);
  assert.equal(commandVerification({ type: "SET_MODE_LOITER", status: "FAILED" }).verified, null);
});

test("null/garbage command → verified null (never throws)", () => {
  assert.equal(commandVerification(null).verified, null);
  assert.equal(commandVerification(undefined).verified, null);
});

// ---- isNonterminal / hasPendingOfType -------------------------------------

test("isNonterminal is true for QUEUED/SENT/ACCEPTED, false for terminal", () => {
  for (const s of ["QUEUED", "SENT", "ACCEPTED"]) assert.equal(isNonterminal({ status: s }), true, s);
  for (const s of ["EXECUTED", "REJECTED", "FAILED", "EXPIRED"]) assert.equal(isNonterminal({ status: s }), false, s);
});

test("hasPendingOfType: a nonterminal LOITER blocks a duplicate LOITER press", () => {
  const cmds = [{ type: "SET_MODE_LOITER", status: "SENT" }];
  assert.equal(hasPendingOfType(cmds, "SET_MODE_LOITER"), true);
});

test("hasPendingOfType: a terminal LOITER no longer blocks (button re-enables)", () => {
  const cmds = [{ type: "SET_MODE_LOITER", status: "REJECTED" }];
  assert.equal(hasPendingOfType(cmds, "SET_MODE_LOITER"), false);
});

test("hasPendingOfType: a pending LOITER does not block a different type (RTL)", () => {
  const cmds = [{ type: "SET_MODE_LOITER", status: "SENT" }];
  assert.equal(hasPendingOfType(cmds, "RTL"), false);
});

test("hasPendingOfType: safe on empty/non-array input", () => {
  assert.equal(hasPendingOfType([], "RTL"), false);
  assert.equal(hasPendingOfType(null, "RTL"), false);
  assert.equal(hasPendingOfType(undefined, "SET_MODE_LOITER"), false);
});

// ---- generalized verification: every mode command renders Scout's verification -----
// The stabilized contract carries a normalized `verification` block for EVERY command
// type; a mode command Scout marks verified:true is a confirmed success showing
// expected-vs-observed, and one it marks verified:false is a FAILED attempt.

const MODE_CMDS = ["SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_LOITER", "SET_MODE_HOLD",
  "ARM", "DISARM", "MISSION_PAUSE", "MISSION_RESUME"];

for (const type of MODE_CMDS) {
  test(`${type} EXECUTED + backend verification verified:true → verified true (VERIFIED)`, () => {
    const cmd = { type, status: "EXECUTED",
      verification: { verified: true, outcome: "VERIFIED", expected: type.replace("SET_MODE_", ""), observed: type.replace("SET_MODE_", "") } };
    const v = commandVerification(cmd);
    assert.equal(v.verified, true);
    assert.equal(v.outcome, "VERIFIED");
  });

  test(`${type} EXECUTED but Scout verified:false → verified false (renders FAILED)`, () => {
    const cmd = { type, status: "EXECUTED",
      result: { verified: false, expected_mode: "AUTO", observed_mode: "MANUAL",
                error: { message: "Mode did not change" } } };
    const v = commandVerification(cmd);
    assert.equal(v.verified, false);
    assert.equal(v.outcome, "FAILED");
    assert.equal(v.expected, "AUTO");
    assert.equal(v.observed, "MANUAL");
    assert.equal(v.reason, "Mode did not change");
  });
}

test("mode command EXECUTED with NO verification (older record) → verified null (plain success)", () => {
  const v = commandVerification({ type: "SET_MODE_AUTO", status: "EXECUTED" });
  assert.equal(v.verified, null);
  assert.equal(v.outcome, "EXECUTED");
});

test("older RTL/SET_HOME EXECUTED with no verification → conservative (verified false, never green)", () => {
  assert.equal(commandVerification({ type: "RTL", status: "EXECUTED" }).verified, false);
  assert.equal(commandVerification({ type: "SET_HOME", status: "EXECUTED" }).verified, false);
});

test("backend verification block wins over per-type fields when present", () => {
  // Even a record whose home_result is absent renders per the backend's normalized block.
  const cmd = { type: "SET_HOME", status: "EXECUTED", verification: { verified: true, outcome: "VERIFIED" } };
  assert.equal(commandVerification(cmd).verified, true);
});

test("MISSION_UPLOAD EXECUTED + mission_result verified → verified true", () => {
  assert.equal(commandVerification({ type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "verified" }).verified, true);
});
test("MISSION_UPLOAD EXECUTED + mission_result failed → verified false (mismatch)", () => {
  const v = commandVerification({ type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "failed",
    reason: "Pixhawk holds 4 waypoints after upload — expected 5." });
  assert.equal(v.verified, false);
  assert.equal(v.outcome, "FAILED");
  assert.match(v.reason, /expected 5/);
});

// ---- outcomeFrom vocabulary ------------------------------------------------
test("outcomeFrom: normalized terminal labels", () => {
  assert.equal(outcomeFrom("QUEUED", null), "PENDING");
  assert.equal(outcomeFrom("SENT", null), "PENDING");
  assert.equal(outcomeFrom("EXECUTED", true), "VERIFIED");
  assert.equal(outcomeFrom("EXECUTED", false), "FAILED");
  assert.equal(outcomeFrom("EXECUTED", null), "EXECUTED");
  assert.equal(outcomeFrom("REJECTED", null), "REJECTED");
  assert.equal(outcomeFrom("EXPIRED", null), "EXPIRED");
});

// ---- source propagation ----------------------------------------------------
test("commandSource: OPERATOR / LOCAL_AGENT / MISSION_AGENT, conservative default", () => {
  assert.equal(commandSource({ source: "OPERATOR" }), "OPERATOR");
  assert.equal(commandSource({ source: "LOCAL_AGENT" }), "LOCAL_AGENT");
  assert.equal(commandSource({ source: "MISSION_AGENT" }), "MISSION_AGENT");
  assert.equal(commandSource({ created_by: "operator" }), "OPERATOR");   // legacy created_by
  assert.equal(commandSource({}), "OPERATOR");                            // missing → conservative
});

// ---- lifecycle -------------------------------------------------------------
test("commandStages: prefers the backend lifecycle, else assembles from timestamps", () => {
  const backend = { lifecycle: [{ stage: "QUEUED", ts: "t0" }, { stage: "EXECUTED", ts: "t1" }] };
  assert.deepEqual(commandStages(backend), backend.lifecycle);
  const legacy = { status: "EXECUTED", created_at: "t0", claimed_at: "t1", completed_at: "t2" };
  assert.deepEqual(commandStages(legacy), [
    { stage: "QUEUED", ts: "t0" }, { stage: "SENT", ts: "t1" }, { stage: "EXECUTED", ts: "t2" },
  ]);
});

// ---- Map / Vehicle result parity -------------------------------------------
// Both pages read the SAME commandVerification, so the outcome they render for a given
// record is identical by construction. Pin that the function is a pure mapping: two
// separate calls (Map's and Vehicle's) on the same record yield deep-equal results.
test("Map/Vehicle parity: commandVerification is a pure mapping (same record → same result)", () => {
  const records = [
    { type: "RTL", status: "EXECUTED", rtl_result: "confirmed" },
    { type: "RTL", status: "EXECUTED", rtl_result: "failed", reason: "Pixhawk remained in MANUAL" },
    { type: "SET_HOME", status: "EXECUTED", home_result: "verified" },
    { type: "SET_MODE_AUTO", status: "EXECUTED", result: { verified: false, observed_mode: "MANUAL" } },
    { type: "SET_MODE_LOITER", status: "EXECUTED" },
    { type: "MISSION_UPLOAD", status: "ACCEPTED" },
  ];
  for (const r of records) {
    assert.deepEqual(commandVerification(r), commandVerification({ ...r }),
      `parity for ${r.type}/${r.status}`);
  }
});
