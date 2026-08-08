// Unit tests for the mission-refresh policy (operator/lib/mission-refresh.js): the pure
// "when do we re-download the full Pixhawk mission, and did its geometry change" logic.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  missionIdentity, missionProgress, createMissionRefreshTracker,
  MISSION_WRITE_COMMANDS, MISSION_WRITE_SUCCESS, missionWriteNeedsRefetch,
} from "../operator/lib/mission-refresh.js";

// ---- identity precedence (GEOMETRY only — current_seq excluded) ----
test("missionIdentity prefers route_content_hash, then full/mission hash, then count", () => {
  assert.equal(missionIdentity({ route_content_hash: "a", full_mission_hash: "b", hash: "c", count: 4 }), "rch:a");
  assert.equal(missionIdentity({ full_mission_hash: "b", hash: "c", count: 4 }), "fmh:b");
  assert.equal(missionIdentity({ hash: "c", count: 4 }), "h:c");
  assert.equal(missionIdentity({ count: 4 }), "c:4");
  assert.equal(missionIdentity({ pixhawk_item_count: 5 }), "c:5");
});
test("missionIdentity is null when nothing usable is present", () => {
  assert.equal(missionIdentity(null), null);
  assert.equal(missionIdentity({}), null);
  assert.equal(missionIdentity({ reachable: false }), null);
});
test("geometry identity ignores current_seq; progress is a separate signal", () => {
  // Same geometry, different progress → identical identity, different progress.
  assert.equal(missionIdentity({ count: 4, current_seq: 1 }), missionIdentity({ count: 4, current_seq: 2 }));
  assert.equal(missionProgress({ current_seq: 2 }), 2);
  assert.equal(missionProgress({ count: 4 }), null);
  assert.equal(missionProgress(null), null);
});

// ---- geometry vs progress change reporting ----
test("unchanged geometry with a new current_seq → geometryChanged false, progressChanged true", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(2, { route_content_hash: "abc", current_seq: 1 });
  const meta = t.noteFetched(2, { route_content_hash: "abc", current_seq: 2 });
  assert.equal(meta.geometryChanged, false, "geometry (route_content_hash) unchanged");
  assert.equal(meta.progressChanged, true, "current_seq advanced");
  assert.equal(t.progressOf(2), 2, "new progress recorded");
});
test("identical geometry AND progress → nothing changed", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(2, { route_content_hash: "abc", current_seq: 3 });
  const meta = t.noteFetched(2, { route_content_hash: "abc", current_seq: 3 });
  assert.deepEqual(meta, { geometryChanged: false, progressChanged: false });
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

// ---- geometry change reporting ----
test("noteFetched reports geometryChanged: true first, false unchanged, true on change", () => {
  const t = createMissionRefreshTracker();
  assert.equal(t.noteFetched(2, { route_content_hash: "a" }).geometryChanged, true);   // first
  assert.equal(t.noteFetched(2, { route_content_hash: "a" }).geometryChanged, false);  // unchanged
  assert.equal(t.noteFetched(2, { route_content_hash: "b" }).geometryChanged, true);   // changed
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

// ---- mission-write refetch policy ----
test("missionWriteNeedsRefetch: refetch on success, not on ordinary failure", () => {
  assert.equal(missionWriteNeedsRefetch("VERIFIED", null), true);
  assert.equal(missionWriteNeedsRefetch("EXECUTED", null), true);
  assert.equal(missionWriteNeedsRefetch("FAILED", null), false);
  assert.equal(missionWriteNeedsRefetch("REJECTED", { error: "denied" }), false);
  assert.equal(missionWriteNeedsRefetch("EXPIRED", null), false);
});
test("missionWriteNeedsRefetch: a failure that flags uncertain/partial state DOES refetch", () => {
  assert.equal(missionWriteNeedsRefetch("FAILED", { partial: true }), true);
  assert.equal(missionWriteNeedsRefetch("FAILED", { vehicle_state_uncertain: true }), true);
  assert.equal(missionWriteNeedsRefetch("REJECTED", { mission_state_uncertain: true }), true);
  assert.ok(MISSION_WRITE_SUCCESS.has("VERIFIED"));
});

// ---- "stop" is a FORCE reason: Scout restores and rewinds inside its own transaction ----
// A Stop can legitimately leave the geometry byte-for-byte identical (a run that was never
// replanned) while the SEQUENCE has moved back to zero. If the cache were consulted the overlay
// would keep showing the run's last waypoint as active, so the download must be unconditional.
test("a completed Stop always forces a fresh mission download", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(1, { route_content_hash: "a", current_seq: 7 }, 1000);
  assert.deepEqual(t.shouldFetch(1, { reason: "stop", now: 1001 }), { fetch: true, why: "stop" });
  // …even immediately after a fallback read said the cache was fresh.
  assert.equal(t.shouldFetch(1, { reason: "fallback", now: 1001 }).fetch, false);
  assert.equal(t.shouldFetch(1, { reason: "stop", now: 1001 }).fetch, true);
});

test("a Stop that restored the original mission reports the geometry change", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(1, { route_content_hash: "revised", current_seq: 7 }, 1000);
  const after = t.noteFetched(1, { route_content_hash: "original", current_seq: 0 }, 2000);
  assert.deepEqual(after, { geometryChanged: true, progressChanged: true });
});

test("a Stop on a never-replanned run still reports the rewind as a progress change", () => {
  const t = createMissionRefreshTracker();
  t.noteFetched(1, { route_content_hash: "original", current_seq: 7 }, 1000);
  const after = t.noteFetched(1, { route_content_hash: "original", current_seq: 0 }, 2000);
  assert.equal(after.geometryChanged, false, "the route is the same route");
  assert.equal(after.progressChanged, true, "…but the mission was rewound to its start");
});

test("an in-flight fetch still suppresses a duplicate stop-triggered download", () => {
  const t = createMissionRefreshTracker();
  assert.deepEqual(t.shouldFetch(1, { reason: "stop", inFlight: true }),
    { fetch: false, why: "in-flight" });
});
