// Unit tests for the Vehicle-Home status + deployment command-gating policy
// (operator/lib/home.js). Run: `node --test tests/` (or `npm test`).
//
// The gating policy is a SAFETY rule — these tests pin the exact interlock from the
// deployment workflow, especially that LOITER is NEVER Home-gated.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  homeStatus, commandGate, deploymentReadiness, metersBetween, fmtDistance,
  HOME_VERIFY_TOLERANCE_M,
  SAFETY_HOLD_TYPE, PRIMARY_MODES, ADVANCED_MODES, isSafetyHold,
} from "../operator/lib/home.js";

// ---- helpers ----
const veh = (home, lat = 56.7, lng = 13.0) => ({ id: 2, lat, lng, home });
const HOME_HERE = { available: true, lat: 56.70001, lng: 13.00001, source: "pixhawk" };
const HOME_FAR = { available: true, lat: 56.7, lng: 12.97, source: "pixhawk" }; // ~1.8 km west
// A full-control, mission-ready-but-unverified context.
const baseCtx = {
  hasControl: true, homeVerified: false, connected: true,
  posValid: true, gpsFresh: true, missionLoaded: true, setHomePending: false,
};

// ---- distance helpers ----
test("metersBetween returns null on missing coord, ~0 on identical", () => {
  assert.equal(metersBetween(1, 2, null, 2), null);
  assert.ok(metersBetween(56.7, 13.0, 56.7, 13.0) < 0.001);
});
test("fmtDistance switches metres → km", () => {
  assert.equal(fmtDistance(1.4), "1.4 m");
  assert.equal(fmtDistance(420), "420 m");
  assert.equal(fmtDistance(1600), "1.6 km");
});

// ---- A. Home status states ----
test("Home UNKNOWN when Pixhawk home not received", () => {
  const hs = homeStatus(veh(null));
  assert.equal(hs.state, "unknown");
  assert.equal(hs.available, false);
  assert.match(hs.reason, /has not been received/i);
});

test("Home NOT VERIFIED and far from Scout reports the distance", () => {
  const hs = homeStatus(veh(HOME_FAR));
  assert.equal(hs.state, "unverified");
  assert.ok(hs.distanceM > 1000, `expected >1km, got ${hs.distanceM}`);
  assert.match(hs.reason, /km from Scout/i);
  assert.match(hs.reason, /before autonomous operation/i);
});

test("Home VERIFIED reads from v.home.verified + verified_at age", () => {
  const now = Date.now();
  const v = veh({ ...HOME_HERE, verified: true, verified_at: new Date(now - 8000).toISOString(), verification_distance_m: 1.4 });
  const hs = homeStatus(v, { now });
  assert.equal(hs.state, "verified");
  assert.equal(hs.verified, true);
  assert.ok(hs.verifiedAgeS >= 7 && hs.verifiedAgeS <= 9);
  assert.equal(hs.verifiedDistanceM, 1.4);
  assert.equal(hs.reason, null); // never a "set home" nag once verified
});

test("a stale Scout home_status is NEVER surfaced as verified, even if it once was", () => {
  // Scout's own `verified:true` from a moment ago must not be trusted once `stale`
  // is set — the backend already forces verified:false when stale, but this pins
  // that homeStatus() renders whatever it's given and never re-derives verified
  // from anything else (no client-side latching either).
  const v = veh({ ...HOME_HERE, verified: false, stale: true, reason: "Scout has not confirmed Home status recently — treating as unverified." });
  const hs = homeStatus(v);
  assert.equal(hs.state, "unverified");
  assert.equal(hs.stale, true);
  assert.match(hs.reason, /has not confirmed Home status recently/i);
});

test("a command's own 'confirmed' feedback phase is not a homeStatus phase — verified never gets forced", () => {
  // homeStatus only recognizes 'idle'/'pending'/'failed' phases; passing anything else
  // (as effectiveHomeStatus does for a successful SET_HOME command result) must fall
  // through to the settled v.home state, never fabricate "verified".
  const v = veh({ ...HOME_HERE, verified: false });
  const hs = homeStatus(v, { phase: "confirmed" });
  assert.equal(hs.state, "unverified");
  assert.equal(hs.verified, false);
});

test("Pending phase overrides settled state", () => {
  const hs = homeStatus(veh(HOME_HERE), { phase: "pending" });
  assert.equal(hs.state, "pending");
});

test("Failed phase surfaces the structured failure message as the reason", () => {
  const hs = homeStatus(veh(HOME_HERE), { phase: "failed", failMessage: "No valid GPS fix." });
  assert.equal(hs.failMessage, "No valid GPS fix.");
  assert.equal(hs.reason, "No valid GPS fix.");
});

// ---- F. Command gating policy ----
test("AUTO disabled when Home unverified (with a Home reason)", () => {
  const g = commandGate("SET_MODE_AUTO", baseCtx);
  assert.equal(g.enabled, false);
  assert.match(g.reason, /verify Home before AUTO/i);
});

test("RTL disabled when Home unverified", () => {
  const g = commandGate("RTL", baseCtx);
  assert.equal(g.enabled, false);
  assert.match(g.reason, /requires a verified Home/i);
});

test("RESUME MISSION disabled when Home unverified", () => {
  const g = commandGate("MISSION_RESUME", baseCtx);
  assert.equal(g.enabled, false);
  assert.match(g.reason, /verify Home before resuming/i);
});

test("AUTO / RTL / RESUME ENABLED once Home is verified", () => {
  const ctx = { ...baseCtx, homeVerified: true };
  for (const t of ["SET_MODE_AUTO", "RTL", "MISSION_RESUME"]) {
    const g = commandGate(t, ctx);
    assert.equal(g.enabled, true, `${t} should be enabled when verified`);
    assert.equal(g.reason, null);
  }
});

test("LOITER stays ENABLED when Home is unverified (critical anti-drift safety)", () => {
  const g = commandGate("SET_MODE_LOITER", baseCtx); // homeVerified:false
  assert.equal(g.enabled, true);
  assert.equal(g.reason, null);
});

test("LOITER stays available when overall mission readiness is false", () => {
  const ctx = { ...baseCtx, homeVerified: false, missionLoaded: false };
  const r = deploymentReadiness(ctx);
  assert.equal(r.ready, false);
  assert.equal(r.loiterAvailable, true); // safety hold independent of readiness
  assert.equal(commandGate("SET_MODE_LOITER", ctx).enabled, true);
});

test("MANUAL follows the ordinary control rule, not Home verification", () => {
  assert.equal(commandGate("SET_MODE_MANUAL", baseCtx).enabled, true); // hasControl, unverified
  assert.equal(commandGate("SET_MODE_MANUAL", { ...baseCtx, hasControl: false }).enabled, false);
});

test("ARM/DISARM are not coupled to Home verification", () => {
  assert.equal(commandGate("ARM", baseCtx).enabled, true);
  assert.equal(commandGate("DISARM", baseCtx).enabled, true);
});

test("No control → home-gated commands disabled WITHOUT a Home reason (lock note owns it)", () => {
  const g = commandGate("SET_MODE_AUTO", { ...baseCtx, hasControl: false });
  assert.equal(g.enabled, false);
  assert.equal(g.reason, null);
});

// ---- C. Set Home button enable conditions ----
test("SET_HOME enabled only with control + connectivity + fresh valid GPS + no pending", () => {
  assert.equal(commandGate("SET_HOME", baseCtx).enabled, true);
  assert.equal(commandGate("SET_HOME", { ...baseCtx, gpsFresh: false }).enabled, false); // stale GPS
  assert.equal(commandGate("SET_HOME", { ...baseCtx, posValid: false }).enabled, false); // no fix
  assert.equal(commandGate("SET_HOME", { ...baseCtx, connected: false }).enabled, false); // disconnected
  assert.equal(commandGate("SET_HOME", { ...baseCtx, hasControl: false }).enabled, false);
  assert.equal(commandGate("SET_HOME", { ...baseCtx, setHomePending: true }).enabled, false); // already pending
});

test("SET_HOME stale-GPS disable carries the 'wait for a current fix' reason", () => {
  const g = commandGate("SET_HOME", { ...baseCtx, gpsFresh: false });
  assert.match(g.reason, /stale/i);
});

// ---- G. Deployment readiness ----
test("READY only when all five conditions hold; Home is one of them", () => {
  const ready = deploymentReadiness({ ...baseCtx, homeVerified: true });
  assert.equal(ready.ready, true);
  const notReady = deploymentReadiness({ ...baseCtx, homeVerified: false });
  assert.equal(notReady.ready, false);
  const homeItem = notReady.items.find((i) => i.key === "home");
  assert.equal(homeItem.ok, false);
});

test("tolerance constant is a small, positive metre value", () => {
  assert.ok(HOME_VERIFY_TOLERANCE_M > 0 && HOME_VERIFY_TOLERANCE_M <= 25);
});

// ---- LOITER as the primary safety hold; HOLD demoted (mode taxonomy) ----
test("LOITER is the designated safety hold; HOLD is not", () => {
  assert.equal(SAFETY_HOLD_TYPE, "SET_MODE_LOITER");
  assert.equal(isSafetyHold("SET_MODE_LOITER"), true);
  assert.equal(isSafetyHold("SET_MODE_HOLD"), false);
});

test("LOITER sits in the PRIMARY command area beside AUTO/MANUAL/RTL", () => {
  for (const t of ["SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_LOITER", "RTL"]) {
    assert.ok(PRIMARY_MODES.includes(t), `${t} should be a primary mode`);
  }
});

test("HOLD is NOT presented as a primary safety action (advanced/secondary only)", () => {
  assert.ok(!PRIMARY_MODES.includes("SET_MODE_HOLD"), "HOLD must not be a primary mode");
  assert.ok(ADVANCED_MODES.includes("SET_MODE_HOLD"), "HOLD belongs in advanced modes");
  assert.notEqual(SAFETY_HOLD_TYPE, "SET_MODE_HOLD");
  assert.ok(!ADVANCED_MODES.includes("SET_MODE_LOITER"), "LOITER must never be demoted");
});

test("routing preserves the correct command types (LOITER vs HOLD are distinct)", () => {
  // The taxonomy carries backend command types verbatim — the button routes exactly this.
  assert.ok(PRIMARY_MODES.includes("SET_MODE_LOITER"));
  assert.ok(ADVANCED_MODES.includes("SET_MODE_HOLD") && ADVANCED_MODES.includes("SET_MODE_GUIDED"));
});

test("LOITER stays enabled with unverified Home AND false readiness (regression guard)", () => {
  const ctx = { hasControl: true, homeVerified: false, connected: true,
                posValid: true, gpsFresh: true, missionLoaded: false, setHomePending: false };
  assert.equal(deploymentReadiness(ctx).ready, false);
  assert.equal(commandGate("SET_MODE_LOITER", ctx).enabled, true);
  // ...while the Home-gated trio stays disabled in the very same context.
  for (const t of ["SET_MODE_AUTO", "RTL", "MISSION_RESUME"]) {
    assert.equal(commandGate(t, ctx).enabled, false, `${t} must stay Home-gated`);
  }
});
