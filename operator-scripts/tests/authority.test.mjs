// Unit tests for the FINALIZED strict-ownership control-authority contract
// (operator/lib/authority.js + the write gate in operator/lib/home.js).
// Run: `node --test tests/` (or `npm test`).
//
// This is a SAFETY contract, so it is pinned here rather than trusted to the pages:
//   OPERATOR    — station is read/write; every supported action follows its OWN gates.
//   LOCAL_AGENT — station is read-only for vehicle writes. NO exceptions: SET_HOME and
//                 LOITER are deliberately NOT exempt. Take Control stays available.
//   RC          — physical override, highest priority, reported only, never requestable.
// Startup/default authority is OPERATOR.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createAuthorityController, handoffGate, normAuthority, AUTH_TIMEOUT_MS,
} from "../operator/lib/authority.js";
import { commandGate } from "../operator/lib/home.js";

// Every write action the contract lists for the Operator Station.
const WRITE_ACTIONS = [
  "SET_HOME", "SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_LOITER",
  "RTL", "ARM", "DISARM", "MISSION_PAUSE", "MISSION_RESUME",
];

// A Scout GET body, as main.py's _scout_authority_read normalizes it.
const scoutSays = (authority) => ({
  ok: true, available: true, reachable: true, authority, source: "scout",
});

// Non-authority conditions all satisfied, so a disable can only be authority's doing.
const ctxFor = (hasControl) => ({
  hasControl, homeVerified: true, connected: true,
  posValid: true, gpsFresh: true, missionLoaded: true, setHomePending: false,
});

// ---- A. OPERATOR: read/write, subject to each action's own gates ----
test("OPERATOR → every supported write action is enabled when its own gates pass", () => {
  const ctx = ctxFor(true);
  for (const type of WRITE_ACTIONS) {
    assert.equal(commandGate(type, ctx).enabled, true, `${type} must be enabled under OPERATOR`);
  }
});

test("OPERATOR → actions still obey their OWN safety gates (authority is not a bypass)", () => {
  // The Home interlock still bites under OPERATOR...
  const unverified = { ...ctxFor(true), homeVerified: false };
  for (const type of ["SET_MODE_AUTO", "RTL", "MISSION_RESUME"]) {
    assert.equal(commandGate(type, unverified).enabled, false, `${type} stays Home-gated`);
  }
  // ...while LOITER/MANUAL, which are not Home-gated, remain enabled.
  assert.equal(commandGate("SET_MODE_LOITER", unverified).enabled, true);
  assert.equal(commandGate("SET_MODE_MANUAL", unverified).enabled, true);
  // SET_HOME still needs a fresh, valid fix even with authority.
  assert.equal(commandGate("SET_HOME", { ...ctxFor(true), gpsFresh: false }).enabled, false);
});

// ---- B. LOCAL_AGENT: read-only for writes, strictly, with no exemptions ----
test("LOCAL_AGENT → ALL write actions disabled, with no exceptions", () => {
  const ctx = ctxFor(false);   // every other condition is perfect
  for (const type of WRITE_ACTIONS) {
    assert.equal(commandGate(type, ctx).enabled, false, `${type} must be disabled under LOCAL_AGENT`);
  }
});

test("LOCAL_AGENT → SET_HOME and LOITER are NOT exempt (strict ownership model)", () => {
  // Pins the finalized decision explicitly: these two were considered as exceptions
  // and deliberately rejected. If either flips to enabled here, the model changed.
  const ctx = ctxFor(false);
  assert.equal(commandGate("SET_HOME", ctx).enabled, false);
  assert.equal(commandGate("SET_MODE_LOITER", ctx).enabled, false);
  // SET_HOME explains itself so the operator knows the remedy.
  assert.match(commandGate("SET_HOME", ctx).reason, /Take Control \(OPERATOR\)/i);
});

// ---- C. Hand-off affordances ----
test("LOCAL_AGENT → Take Control remains enabled (and Release does not apply)", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("LOCAL_AGENT"));
  const g = handoffGate(c.view(), { stale: false });
  assert.equal(g.canTake, true, "Take Control MUST always remain available under LOCAL_AGENT");
  assert.equal(g.canRelease, false);
  assert.equal(g.hasControl, false);
});

test("OPERATOR → Release Control remains enabled, Take Control is redundant", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("OPERATOR"));
  const g = handoffGate(c.view(), { stale: false });
  assert.equal(g.canRelease, true);
  assert.equal(g.canTake, false);   // already held
  assert.equal(g.hasControl, true);
});

test("hand-off is withheld while a request is in flight or the link is not current", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("LOCAL_AGENT"));
  assert.equal(handoffGate(c.view(), { stale: true }).canTake, false, "no hand-off over a dead link");
  const noSource = { ...c.view(), available: false };
  assert.equal(handoffGate(noSource, { stale: false }).canTake, false, "no authority source");
});

// ---- D. Startup / restart / reconnect ----
test("startup: Scout reporting OPERATOR selects OPERATOR and unlocks commands", () => {
  const c = createAuthorityController(() => {});
  assert.equal(c.view().value, null);                 // nothing assumed before the first read
  assert.equal(c.view().hasControl, false);           // never optimistic
  c.setServer(scoutSays("OPERATOR"));                 // default authority after startup
  assert.equal(c.view().value, "OPERATOR");
  assert.equal(c.view().hasControl, true);
  assert.equal(commandGate("SET_MODE_MANUAL", ctxFor(c.view().hasControl)).enabled, true);
});

test("reconnect after a Scout restart: a stale LOCAL_AGENT selection never persists", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("LOCAL_AGENT"));
  assert.equal(c.view().value, "LOCAL_AGENT");
  // Scout restarts and now reports its default OPERATOR — the UI must follow the
  // CURRENT reported state, not keep the last selection.
  c.setServer(scoutSays("OPERATOR"));
  assert.equal(c.view().value, "OPERATOR");
  assert.equal(c.view().hasControl, true);
  assert.equal(handoffGate(c.view(), {}).canRelease, true);
});

test("an unreachable authority read reports UNKNOWN and never claims control", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("OPERATOR"));
  c.setServer({ ok: true, available: true, reachable: false, authority: null });
  assert.equal(c.view().value, null);
  assert.equal(c.view().hasControl, false, "unknown authority must never read as control");
  for (const type of WRITE_ACTIONS) {
    assert.equal(commandGate(type, ctxFor(c.view().hasControl)).enabled, false);
  }
});

test("selecting another vehicle resets authority — no carry-over between vehicles", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("OPERATOR"));
  c.reset();
  assert.equal(c.view().value, null);
  assert.equal(c.view().hasControl, false);
});

// ---- E. RC: reported only, highest priority, never requestable ----
test("RC → reportable, but never a state the station can request", async () => {
  assert.equal(normAuthority({ authority: "RC", reachable: true }).value, "RC");
  const c = createAuthorityController(() => {});
  // request() only accepts the two requestable values (main.py REQUESTABLE_AUTHORITY).
  const res = await c.request("RC", async () => ({ ok: true }));
  assert.equal(res.ok, false, "RC must never be requestable by the Operator Station");
});

test("RC → software writes disabled; the UI never claims operator/agent control", () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("RC"));
  const v = c.view();
  assert.equal(v.value, "RC");                 // shown as its own condition...
  assert.equal(v.hasControl, false);           // ...and it is NOT operator control
  for (const type of WRITE_ACTIONS) {
    assert.equal(commandGate(type, ctxFor(v.hasControl)).enabled, false, `${type} disabled under RC`);
  }
  // Take Control stays offerable (it takes effect once RC physically releases).
  assert.equal(handoffGate(v, {}).canTake, true);
});

// ---- F. A hand-off is confirmed by Scout, never by the click ----
test("a hand-off stays PENDING (and commands locked) until Scout confirms the value", async () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("LOCAL_AGENT"));
  // Scout accepts the POST but has not yet reported the new effective value.
  const p = c.request("OPERATOR", async () => ({ ok: true, status: 200, data: {} }));
  assert.equal(c.view().phase, "pending");
  assert.equal(c.view().hasControl, false, "never optimistically enabled on the click");
  assert.equal(handoffGate(c.view(), {}).canTake, false, "withheld while in flight");
  await p;
  c.setServer(scoutSays("OPERATOR"));          // Scout confirms
  assert.equal(c.view().hasControl, true);
  c.dispose();
});

test("a rejected hand-off leaves authority where Scout says it is, with the reason", async () => {
  const c = createAuthorityController(() => {});
  c.setServer(scoutSays("LOCAL_AGENT"));
  await c.request("OPERATOR", async () => ({ ok: false, status: 502, data: { message: "Scout unreachable" } }));
  const v = c.view();
  assert.equal(v.pending.phase, "rejected");
  assert.match(v.pending.reason, /Scout unreachable/);
  assert.equal(v.value, "LOCAL_AGENT", "a failed request never moves the displayed authority");
  assert.equal(v.hasControl, false);
  c.dispose();
});

test("AUTH_TIMEOUT_MS bounds an unconfirmed hand-off", () => {
  assert.ok(AUTH_TIMEOUT_MS > 0 && AUTH_TIMEOUT_MS <= 30000);
});
