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
import { commandVerification, hasPendingOfType, isNonterminal } from "../operator/lib/command.js";

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
