// Unit tests for the mission-refresh policy (operator/lib/mission-refresh.js): the pure
// "when do we re-download the full Pixhawk mission, and did its geometry change" logic.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  missionIdentity, createMissionRefreshTracker, MISSION_WRITE_COMMANDS,
} from "../operator/lib/mission-refresh.js";

// ---- identity precedence ----
test("missionIdentity prefers route_content_hash, then full/mission hash, then count", () => {
  assert.equal(missionIdentity({ route_content_hash: "a", full_mission_hash: "b", hash: "c", count: 4 }), "rch:a");
  assert.equal(missionIdentity({ full_mission_hash: "b", hash: "c", count: 4 }), "fmh:b");
  assert.equal(missionIdentity({ hash: "c", count: 4 }), "h:c");
  assert.equal(missionIdentity({ count: 4, current_seq: 2 }), "c:4/s:2");
  assert.equal(missionIdentity({ pixhawk_item_count: 5 }), "c:5/s:-");
});
test("missionIdentity is null when nothing usable is present", () => {
  assert.equal(missionIdentity(null), null);
  assert.equal(missionIdentity({}), null);
  assert.equal(missionIdentity({ reachable: false }), null);
});
test("progress (current_seq) changes the count-based identity", () => {
  assert.notEqual(missionIdentity({ count: 4, current_seq: 1 }), missionIdentity({ count: 4, current_seq: 2 }));
});

// ---- forced triggers ----
for (const reason of ["select", "manual", "command", "replan"]) {
  test(`reason '${reason}' always forces a fetch`, () => {
    const t = createMissionRefreshTracker();
    assert.equal(t.shouldFetch(2, { reason }).fetch, true);
  });
}
test("an in-flight fetch suppresses any trigger", () => {
  const t = createMissionRefreshTracker();
  assert.equal(t.shouldFetch(2, { reason: "select", inFlight: true }).fetch, false);
});

// ---- fallback ----
test("fallback fetches when never fetched, then only after the window", () => {
  const t = createMissionRefreshTracker({ fallbackMs: 20000 });
  assert.equal(t.shouldFetch(2, { reason: "fallback", now: 1000 }).fetch, true); // never fetched
  t.noteFetched(2, { count: 3, current_seq: 0 }, 1000);
  assert.equal(t.shouldFetch(2, { reason: "fallback", now: 1000 + 100 }).fetch, false);   // fresh
  assert.equal(t.shouldFetch(2, { reason: "fallback", now: 1000 + 20000 }).fetch, true);  // stale
});

// ---- revision ----
test("revision stays dormant without a signal, fires on change, not on repeat", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(2, { route_content_hash: "x" }, 1);
  assert.equal(t.shouldFetch(2, { reason: "revision", revisionSignal: undefined }).fetch, false);
  assert.equal(t.shouldFetch(2, { reason: "revision", revisionSignal: "r2" }).fetch, true);
  t.noteRevisionSignal(2, "r2");
  assert.equal(t.shouldFetch(2, { reason: "revision", revisionSignal: "r2" }).fetch, false);
  assert.equal(t.shouldFetch(2, { reason: "revision", revisionSignal: "r3" }).fetch, true);
});

// ---- identity change reporting ----
test("noteFetched reports whether the identity changed", () => {
  const t = createMissionRefreshTracker();
  assert.equal(t.noteFetched(2, { route_content_hash: "a" }), true);   // first
  assert.equal(t.noteFetched(2, { route_content_hash: "a" }), false);  // unchanged
  assert.equal(t.noteFetched(2, { route_content_hash: "b" }), true);   // changed
});
test("tracking is keyed per vehicle", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(1, { count: 3, current_seq: 0 }, 5);
  // Vehicle 2 has never been fetched → fallback must still fetch even though 1 is fresh.
  assert.equal(t.shouldFetch(2, { reason: "fallback", now: 5 }).fetch, true);
});

test("MISSION_WRITE_COMMANDS covers upload / clear / replan", () => {
  assert.ok(MISSION_WRITE_COMMANDS.has("MISSION_UPLOAD"));
  assert.ok(MISSION_WRITE_COMMANDS.has("MISSION_CLEAR"));
  assert.ok(MISSION_WRITE_COMMANDS.has("MISSION_REPLAN"));
  assert.equal(MISSION_WRITE_COMMANDS.has("RTL"), false);
});
