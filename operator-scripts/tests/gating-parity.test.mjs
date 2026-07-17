// Regression tests: the Map and Vehicle pages MUST gate every command identically.
// Run: `node --test tests/` (or `npm test`).
//
// The bug this pins: Vehicle.js used to gate its command buttons on `hasControl` alone
// and never called commandGate(), so under OPERATOR with an UNVERIFIED Home the Vehicle
// page enabled AUTO/RTL while the Map page correctly disabled them — the same vehicle,
// the same instant, two different answers to "may I press this?".
//
// Both pages now build their context with the shared commandGateCtx() and resolve it
// with the shared commandGate() (operator/lib/home.js). The mirrors below reproduce each
// page's ACTUAL call — including the inputs they legitimately source differently
// (missionLoaded; only Map can have a Set Home in flight) — and assert the outcomes
// still agree for every command both pages render.
import { test } from "node:test";
import assert from "node:assert/strict";
import { commandGate, commandGateCtx } from "../operator/lib/home.js";
import { createAuthorityController, handoffGate } from "../operator/lib/authority.js";

// Commands rendered on the Vehicle page (PRIMARY_CMDS + ADVANCED_CMDS). Map renders the
// same types across its mode/safety/mission rows. SET_HOME is Map-only (no Vehicle button).
const SHARED_CMDS = [
  "SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_LOITER", "RTL",
  "MISSION_PAUSE", "MISSION_RESUME", "ARM", "DISARM",
  "SET_MODE_HOLD", "SET_MODE_GUIDED",
];

const scoutSays = (authority) => ({ ok: true, available: true, reachable: true, authority });

function authView(authority, stale) {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays(authority));
  return handoffGate(c.view(), { stale });
}

// --- Mirrors of each page's real context construction ---
// Map.js homeGateCtx(): hasControl from its controller, missionLoaded from its pxm cache,
// plus the transient Set-Home phase (Map owns the Set Home button).
const mapCtx = (v, { authority, stale = false, setHomePhase = "idle" } = {}) =>
  commandGateCtx(v, {
    hasControl: authView(authority, stale).hasControl,
    connected: !stale,
    missionLoaded: true,
    setHomePending: setHomePhase === "pending",
    homePhase: setHomePhase,
  });

// Vehicle.js renderDetail(): hasControl from its controller, missionLoaded from its
// mission stats. No Set Home control exists on this page, so nothing can be pending.
const vehicleCtx = (v, { authority, stale = false } = {}) =>
  commandGateCtx(v, {
    hasControl: authView(authority, stale).hasControl,
    connected: !stale,
    missionLoaded: true,
  });

const vehAt = (home) => ({ id: 2, lat: 56.7, lng: 13.0, home });
const HOME_UNVERIFIED = { available: true, lat: 56.7, lng: 12.97, verified: false }; // ~1.8 km away
const HOME_VERIFIED = { available: true, lat: 56.70001, lng: 13.00001, verified: true };

const outcomes = (ctx) => Object.fromEntries(
  SHARED_CMDS.map((t) => { const g = commandGate(t, ctx); return [t, { enabled: g.enabled, reason: g.reason }]; }));

// ---- A. The exact regression: OPERATOR + Home unverified ----
test("OPERATOR + Home UNVERIFIED → AUTO/RTL disabled, LOITER/MANUAL enabled — on BOTH pages", () => {
  const v = vehAt(HOME_UNVERIFIED);
  for (const [page, ctx] of [["Map", mapCtx(v, { authority: "OPERATOR" })],
                             ["Vehicle", vehicleCtx(v, { authority: "OPERATOR" })]]) {
    assert.equal(commandGate("SET_MODE_AUTO", ctx).enabled, false, `${page}: AUTO must be Home-gated`);
    assert.equal(commandGate("RTL", ctx).enabled, false, `${page}: RTL must be Home-gated`);
    assert.equal(commandGate("MISSION_RESUME", ctx).enabled, false, `${page}: RESUME must be Home-gated`);
    assert.equal(commandGate("SET_MODE_LOITER", ctx).enabled, true, `${page}: LOITER must stay enabled`);
    assert.equal(commandGate("SET_MODE_MANUAL", ctx).enabled, true, `${page}: MANUAL must stay enabled`);
  }
});

test("OPERATOR + Home UNVERIFIED → Map and Vehicle agree on every command AND every reason", () => {
  const v = vehAt(HOME_UNVERIFIED);
  assert.deepEqual(outcomes(vehicleCtx(v, { authority: "OPERATOR" })),
                   outcomes(mapCtx(v, { authority: "OPERATOR" })));
});

test("the Home-interlock disable carries its explanation on the Vehicle page too", () => {
  // Vehicle used to have no reason to show at all (it never called commandGate).
  const ctx = vehicleCtx(vehAt(HOME_UNVERIFIED), { authority: "OPERATOR" });
  assert.match(commandGate("SET_MODE_AUTO", ctx).reason, /verify Home before AUTO/i);
  assert.match(commandGate("RTL", ctx).reason, /requires a verified Home/i);
  assert.match(commandGate("MISSION_RESUME", ctx).reason, /verify Home before resuming/i);
  // ...and an authority lock is NOT a Home reason (the lock note owns that message).
  const locked = vehicleCtx(vehAt(HOME_UNVERIFIED), { authority: "LOCAL_AGENT" });
  assert.equal(commandGate("SET_MODE_AUTO", locked).reason, null);
});

// ---- B. LOCAL_AGENT: strict ownership, both pages ----
test("LOCAL_AGENT → every write command disabled on BOTH pages", () => {
  const v = vehAt(HOME_VERIFIED);   // even with a perfect Home, authority still locks
  for (const [page, ctx] of [["Map", mapCtx(v, { authority: "LOCAL_AGENT" })],
                             ["Vehicle", vehicleCtx(v, { authority: "LOCAL_AGENT" })]]) {
    for (const type of SHARED_CMDS) {
      assert.equal(commandGate(type, ctx).enabled, false, `${page}: ${type} must be disabled under LOCAL_AGENT`);
    }
  }
  assert.deepEqual(outcomes(vehicleCtx(v, { authority: "LOCAL_AGENT" })),
                   outcomes(mapCtx(v, { authority: "LOCAL_AGENT" })));
});

// ---- C. Full parity matrix ----
test("PARITY: Map and Vehicle agree across every authority × Home × link combination", () => {
  const cases = [];
  for (const authority of ["OPERATOR", "LOCAL_AGENT", "RC", null]) {
    for (const home of [HOME_VERIFIED, HOME_UNVERIFIED, null]) {
      for (const stale of [false, true]) {
        cases.push({ authority, home, stale });
      }
    }
  }
  for (const { authority, home, stale } of cases) {
    const v = vehAt(home);
    const label = `authority=${authority} home=${home ? (home.verified ? "verified" : "unverified") : "none"} stale=${stale}`;
    assert.deepEqual(outcomes(vehicleCtx(v, { authority, stale })),
                     outcomes(mapCtx(v, { authority, stale })), `pages diverged for ${label}`);
  }
  assert.equal(cases.length, 24);
});

test("OPERATOR + Home VERIFIED → the Home-gated trio unlocks on both pages", () => {
  const v = vehAt(HOME_VERIFIED);
  for (const [page, ctx] of [["Map", mapCtx(v, { authority: "OPERATOR" })],
                             ["Vehicle", vehicleCtx(v, { authority: "OPERATOR" })]]) {
    for (const type of SHARED_CMDS) {
      assert.equal(commandGate(type, ctx).enabled, true, `${page}: ${type} should be enabled`);
    }
  }
});

// ---- D. The shared context builder derives, rather than trusting, its inputs ----
test("commandGateCtx derives homeVerified/posValid/gpsFresh — pages never re-implement them", () => {
  const ctx = commandGateCtx(vehAt(HOME_VERIFIED), { hasControl: true, connected: true });
  assert.equal(ctx.homeVerified, true);
  assert.equal(ctx.posValid, true);
  assert.equal(ctx.gpsFresh, true);
  // No position → posValid false, so SET_HOME cannot be offered.
  const noPos = commandGateCtx({ id: 2, lat: null, lng: null, home: HOME_VERIFIED }, { hasControl: true, connected: true });
  assert.equal(noPos.posValid, false);
  assert.equal(commandGate("SET_HOME", noPos).enabled, false);
  // A dead link is never a fresh fix.
  const offline = commandGateCtx(vehAt(HOME_VERIFIED), { hasControl: true, connected: false });
  assert.equal(offline.gpsFresh, false);
  assert.equal(offline.connected, false);
});

test("a Set Home in flight (Map only) stops Home reading as verified while it is replaced", () => {
  const v = vehAt(HOME_VERIFIED);
  const pending = mapCtx(v, { authority: "OPERATOR", setHomePhase: "pending" });
  assert.equal(pending.homeVerified, false, "Home mid-change must not read as verified");
  assert.equal(pending.setHomePending, true);
  assert.equal(commandGate("SET_HOME", pending).enabled, false, "no second Set Home while one is pending");
  // LOITER is still never Home-gated, even mid-change.
  assert.equal(commandGate("SET_MODE_LOITER", pending).enabled, true);
});

// ---- E. Guards: prove this suite can actually FAIL, and that the pages still route
// through the shared policy. There is no DOM test infra here (vanilla ES modules, no
// build step), so the mirrors above cannot catch a page that stops calling commandGate
// altogether — these two tests close that gap.
import { readFileSync } from "node:fs";

test("guard: the OLD hasControl-only gate would diverge (these assertions are not vacuous)", () => {
  // What Vehicle.js used to produce under OPERATOR: authority passes, so every button
  // enabled, Home interlock ignored. It must NOT match Map's real outcomes.
  const legacyVehicle = Object.fromEntries(SHARED_CMDS.map((t) => [t, { enabled: true, reason: null }]));
  const mapReal = outcomes(mapCtx(vehAt(HOME_UNVERIFIED), { authority: "OPERATOR" }));
  assert.notDeepEqual(legacyVehicle, mapReal,
    "if these matched, the parity tests above would prove nothing");
});

test("guard: both pages route button enablement through the shared commandGate policy", () => {
  const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
  for (const page of ["../operator/pages/Map.js", "../operator/pages/Vehicle.js"]) {
    const src = read(page);
    assert.match(src, /commandGate\s*\(/, `${page} must resolve buttons via commandGate()`);
    assert.match(src, /commandGateCtx\s*\(/, `${page} must build its context via commandGateCtx()`);
    // The regression itself: a command button disabled straight off hasControl,
    // bypassing the Home interlock.
    assert.doesNotMatch(src, /data-cmd="\$\{type\}"\$\{\s*hasControl\s*\?/,
      `${page} must not gate a command button on hasControl alone`);
  }
});
