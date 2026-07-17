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
  setHomeOutcome, SET_HOME_QUEUE_GRACE_MS, SET_HOME_LOST_GRACE_MS,
  SET_HOME_DEADLINE_SLACK_MS, SET_HOME_FALLBACK_TTL_MS,
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

// ---- F. In-flight SET_HOME can never hang on "Setting…" ----
// The bug these pin: the UI resolved the pending flash ONLY on a terminal command
// status, so anything that stops a status from ever arriving (a Local Agent that
// never executes SET_HOME, an operator-backend restart wiping the in-memory queue,
// a POST that never settles) left the button on "Setting…" indefinitely.
const T0 = 1_000_000_000_000;
const cmdRec = (over = {}) => ({
  id: "c1", status: "QUEUED",
  expires_at: new Date(T0 + 300_000).toISOString(),   // backend COMMAND_TTL_S = 300 s
  ...over,
});

test("SET_HOME confirmed ONLY on EXECUTED + home_result verified", () => {
  const out = setHomeOutcome({
    cmd: cmdRec({ status: "EXECUTED", home_result: "verified" }),
    cmdId: "c1", startedAt: T0, now: T0 + 4000,
  });
  assert.equal(out.phase, "confirmed");
});

test("a bare EXECUTED with no home_result is a FAILURE, never an optimistic success", () => {
  // EXECUTED means only "the Local Agent reached Scout Flask" — not that Home was set.
  const out = setHomeOutcome({
    cmd: cmdRec({ status: "EXECUTED", reason: "No ack from the Pixhawk." }),
    cmdId: "c1", startedAt: T0, now: T0 + 4000,
  });
  assert.equal(out.phase, "failed");
  assert.match(out.message, /No ack from the Pixhawk/);
});

test("the backend's own terminal status wins and carries Scout's real reason", () => {
  for (const [status, code] of [["REJECTED", "rejected"], ["FAILED", "failed"], ["EXPIRED", "expired"]]) {
    const out = setHomeOutcome({ cmd: cmdRec({ status }), cmdId: "c1", startedAt: T0, now: T0 + 4000 });
    assert.equal(out.phase, "failed");
    assert.equal(out.code, code);
  }
});

test("a QUEUED command still inside its TTL stays pending (no premature failure)", () => {
  const out = setHomeOutcome({ cmd: cmdRec(), cmdId: "c1", startedAt: T0, now: T0 + 299_000 });
  assert.equal(out.phase, "pending");
});

test("a Scout that NEVER reports a result times out at the command's own TTL", () => {
  // The exact reported bug: the command sits QUEUED/SENT forever.
  const out = setHomeOutcome({
    cmd: cmdRec({ status: "SENT" }), cmdId: "c1",
    startedAt: T0, now: T0 + 300_000 + SET_HOME_DEADLINE_SLACK_MS,
  });
  assert.equal(out.phase, "failed");
  assert.equal(out.code, "timeout");
  // Honest: it must NOT claim Home was or was not set.
  assert.match(out.message, /re-check Home status/i);
});

test("the deadline comes from the command's expires_at, not an invented client number", () => {
  // A backend with a longer TTL must not be contradicted by the client.
  const longTtl = cmdRec({ status: "SENT", expires_at: new Date(T0 + 900_000).toISOString() });
  assert.equal(setHomeOutcome({ cmd: longTtl, cmdId: "c1", startedAt: T0, now: T0 + 600_000 }).phase, "pending");
  assert.equal(setHomeOutcome({ cmd: longTtl, cmdId: "c1", startedAt: T0, now: T0 + 900_001 + SET_HOME_DEADLINE_SLACK_MS }).phase, "failed");
});

test("a record with no usable expires_at still times out via the fallback TTL", () => {
  for (const bad of [{ expires_at: null }, { expires_at: "not-a-date" }]) {
    const cmd = cmdRec({ status: "SENT", ...bad });
    assert.equal(setHomeOutcome({ cmd, cmdId: "c1", startedAt: T0, now: T0 + 1000 }).phase, "pending");
    const out = setHomeOutcome({ cmd, cmdId: "c1", startedAt: T0, now: T0 + SET_HOME_FALLBACK_TTL_MS });
    assert.equal(out.phase, "failed", `expires_at ${bad.expires_at} must still time out`);
  }
});

test("a vanished command record (operator backend restarted) fails after the grace, not forever", () => {
  // The in-memory queue resets on restart, so the tracked id is gone for good.
  assert.equal(setHomeOutcome({ cmd: null, cmdId: "c1", startedAt: T0, now: T0 + 1000 }).phase, "pending");
  const out = setHomeOutcome({ cmd: null, cmdId: "c1", startedAt: T0, now: T0 + SET_HOME_LOST_GRACE_MS + 1 });
  assert.equal(out.phase, "failed");
  assert.equal(out.code, "lost");
  assert.match(out.message, /re-check Home status/i);
});

test("a POST that never confirms a queued command fails after the queue grace", () => {
  // fetch() has no timeout of its own — without this the flash pends forever.
  assert.equal(setHomeOutcome({ cmd: null, cmdId: null, startedAt: T0, now: T0 + 1000 }).phase, "pending");
  const out = setHomeOutcome({ cmd: null, cmdId: null, startedAt: T0, now: T0 + SET_HOME_QUEUE_GRACE_MS + 1 });
  assert.equal(out.phase, "failed");
  assert.equal(out.code, "not_queued");
  assert.match(out.message, /was not changed|not confirm/i);
});

test("GUARANTEE: pending is always bounded — no input pends past the fallback TTL", () => {
  const far = T0 + SET_HOME_FALLBACK_TTL_MS + 60_000;
  const cases = [
    { cmd: null, cmdId: null },
    { cmd: null, cmdId: "c1" },
    { cmd: cmdRec({ status: "QUEUED" }), cmdId: "c1" },
    { cmd: cmdRec({ status: "SENT" }), cmdId: "c1" },
    { cmd: cmdRec({ status: "ACCEPTED" }), cmdId: "c1" },
    { cmd: cmdRec({ status: "SENT", expires_at: null }), cmdId: "c1" },
  ];
  for (const c of cases) {
    const out = setHomeOutcome({ ...c, startedAt: T0, now: far });
    assert.notEqual(out.phase, "pending", `${c.cmd ? c.cmd.status : "no record"} must not pend forever`);
    assert.ok(out.message, "a terminal outcome must always carry an operator-facing message");
  }
});

// ---- G. The timeout is a FALLBACK ONLY — a real Scout result always wins ----
// Pins the contract with the updated Scout: the bounded-pending work must never
// interfere with, delay, or override a genuine terminal result.
test("a fast Scout success resolves immediately — the timeout never interferes", () => {
  const out = setHomeOutcome({
    cmd: cmdRec({ status: "EXECUTED", home_result: "verified" }),
    cmdId: "c1", startedAt: T0, now: T0 + 1200,   // Scout answered in 1.2 s
  });
  assert.equal(out.phase, "confirmed");
});

test("a terminal result OVERRIDES the deadline, even if it lands late", () => {
  // Terminal status is checked before any deadline, so a slow-but-real Scout answer
  // is still reported as Scout reported it — never masked by a client timeout.
  const late = T0 + 600_000; // long past expires_at
  assert.equal(setHomeOutcome({
    cmd: cmdRec({ status: "EXECUTED", home_result: "verified" }), cmdId: "c1",
    startedAt: T0, now: late,
  }).phase, "confirmed");
  // ...and a late rejection keeps Scout's real reason rather than the timeout copy.
  const rej = setHomeOutcome({
    cmd: cmdRec({ status: "REJECTED", reason: "blocked: SET_HOME requires a GPS fix" }),
    cmdId: "c1", startedAt: T0, now: late,
  });
  assert.equal(rej.code, "rejected");
  assert.match(rej.message, /blocked: SET_HOME requires a GPS fix/);
});

test("an authority rejection is surfaced verbatim, not as a comms/timeout failure", () => {
  const out = setHomeOutcome({
    cmd: cmdRec({ status: "REJECTED", reason: "blocked: SET_HOME requires LOCAL_AGENT control authority" }),
    cmdId: "c1", startedAt: T0, now: T0 + 2000,
  });
  assert.equal(out.phase, "failed");
  assert.equal(out.code, "rejected");
  assert.equal(out.message, "blocked: SET_HOME requires LOCAL_AGENT control authority");
  assert.doesNotMatch(out.message, /timed out|never reported|lost track/i);
});
