// Unit tests for the Operator-side mission-upload eligibility policy
// (operator/lib/upload-policy.js) — the armed + confirmed-LOITER gating.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { uploadEligibility, UPLOAD_LEVEL } from "../operator/lib/upload-policy.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLAN_SRC = readFileSync(join(HERE, "..", "operator", "pages", "Plan.js"), "utf-8");
const POLICY_SRC = readFileSync(join(HERE, "..", "operator", "lib", "upload-policy.js"), "utf-8");

// ---- static source guarantees ----
test("Upload never auto-commands LOITER (no SET_MODE_LOITER in the upload path)", () => {
  assert.ok(!/SET_MODE_LOITER/.test(PLAN_SRC), "Plan page issues no LOITER command");
  assert.ok(!/SET_MODE_LOITER/.test(POLICY_SRC), "upload policy issues no LOITER command");
});
test("Plan page gates upload through the shared eligibility policy", () => {
  assert.match(PLAN_SRC, /uploadEligibility|uploadGate/, "uses the eligibility policy");
});
test("finalize payload carries upload_context for Scout metadata", () => {
  const planningSrc = readFileSync(join(HERE, "..", "operator", "lib", "planning.js"), "utf-8");
  assert.match(planningSrc, /upload_context/, "upload_context threaded into finalize payload");
  assert.match(planningSrc, /OPERATOR_REPLACEMENT/, "operator-replacement context value present");
});

// A connected, authorised, no-pending baseline. Individual tests override fields.
const base = { connected: true, hasAuthority: true, missionPending: false };

test("disarmed vehicle may upload (existing prerequisites)", () => {
  const r = uploadEligibility({ ...base, armed: false, mode: "AUTO" });
  assert.equal(r.allowed, true);
  assert.equal(r.level, UPLOAD_LEVEL.OK);
});

test("armed + confirmed fresh LOITER is allowed — not blocked just for being armed", () => {
  const r = uploadEligibility({ ...base, armed: true, mode: "LOITER", modeFresh: true, groundspeed: 0.1 });
  assert.equal(r.allowed, true);
  assert.equal(r.level, UPLOAD_LEVEL.WARN);
  assert.match(r.message, /holding position in LOITER/);
});

test("armed + LOITER with high groundspeed still allowed, but warns Scout may reject", () => {
  const r = uploadEligibility({ ...base, armed: true, mode: "LOITER", modeFresh: true, groundspeed: 2.0 });
  assert.equal(r.allowed, true);
  assert.match(r.message, /groundspeed/i);
});

test("armed + LOITER allowed when groundspeed is unavailable (Scout does final check)", () => {
  const r = uploadEligibility({ ...base, armed: true, mode: "LOITER", modeFresh: true, groundspeed: null });
  assert.equal(r.allowed, true);
});

for (const mode of ["AUTO", "MANUAL", "RTL", "GUIDED"]) {
  test(`armed ${mode} is rejected — requires confirmed LOITER`, () => {
    const r = uploadEligibility({ ...base, armed: true, mode, modeFresh: true });
    assert.equal(r.allowed, false);
    assert.equal(r.level, UPLOAD_LEVEL.BLOCK);
    assert.match(r.reason, /confirmed LOITER/);
  });
}

test("armed + unknown mode is rejected — waiting for fresh mode", () => {
  const r = uploadEligibility({ ...base, armed: true, mode: null, modeFresh: true });
  assert.equal(r.allowed, false);
  assert.match(r.reason, /fresh vehicle mode/);
});

test("armed + stale mode is rejected — waiting for fresh mode", () => {
  const r = uploadEligibility({ ...base, armed: true, mode: "LOITER", modeFresh: false });
  assert.equal(r.allowed, false);
  assert.match(r.reason, /fresh vehicle mode/);
});

test("disconnected is rejected regardless of armed/mode", () => {
  const r = uploadEligibility({ ...base, connected: false, armed: false, mode: "LOITER" });
  assert.equal(r.allowed, false);
  assert.match(r.reason, /disconnected/i);
});

test("missing required operator authority is rejected", () => {
  const r = uploadEligibility({ ...base, hasAuthority: false, armed: false });
  assert.equal(r.allowed, false);
  assert.match(r.reason, /OPERATOR control/);
});

test("another mission operation already pending is rejected", () => {
  const r = uploadEligibility({ ...base, missionPending: true, armed: false });
  assert.equal(r.allowed, false);
  assert.match(r.reason, /already in progress/);
});

test("unknown armed state (field unavailable) does not block — Scout authoritative", () => {
  const r = uploadEligibility({ ...base, armed: null, mode: "AUTO" });
  assert.equal(r.allowed, true, "cannot confirm armed → let Scout enforce");
});

test("modeFresh defaults to the connection state when omitted", () => {
  // armed + LOITER, connected but modeFresh omitted → treated fresh (connected) → allowed
  const r = uploadEligibility({ connected: true, hasAuthority: true, missionPending: false, armed: true, mode: "LOITER" });
  assert.equal(r.allowed, true);
});
