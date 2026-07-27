// Unit tests for the shared selected-USV refresh controller
// (operator/services/selected-refresh.js). Fully dependency-injected — no DOM, no real
// timers — so every behaviour in the task's test list is deterministic.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createSelectedRefresh } from "../operator/services/selected-refresh.js";

const flush = () => new Promise((r) => setTimeout(r, 0));
function deferred() { let resolve; const promise = new Promise((r) => (resolve = r)); return { promise, resolve }; }
const REACHABLE = (extra = {}) => ({ reachable: true, count: 3, current_seq: 0, ...extra });
// Common DI knobs that keep the controller off real timers/DOM.
const base = { setTimer: () => 1, clearTimer: () => {}, isHidden: () => false, now: () => 1000 };

test("selecting a vehicle triggers an immediate state and mission fetch", async () => {
  const st = [], mi = [];
  const c = createSelectedRefresh({
    ...base,
    fetchState: (id) => { st.push(id); return Promise.resolve({}); },
    fetchMission: (id) => { mi.push(id); return Promise.resolve(REACHABLE()); },
    onState: () => {}, onMission: () => {},
  });
  c.select(7); await flush();
  assert.deepEqual(st, [7]);
  assert.deepEqual(mi, [7]);
});

test("a late response from a previously selected USV is ignored", async () => {
  const d = { 1: deferred(), 2: deferred() };
  const onM = [];
  const c = createSelectedRefresh({
    ...base,
    fetchMission: (id) => d[id].promise,
    onMission: (id) => onM.push(id),
  });
  c.select(1);            // fetch for 1 pending
  c.select(2);            // operator switches to 2 before 1 resolves
  d[1].resolve(REACHABLE({ count: 1 }));  // late USV-1 reply
  await flush();
  assert.deepEqual(onM, [], "late USV-1 response must not apply");
  d[2].resolve(REACHABLE({ count: 2 }));
  await flush();
  assert.deepEqual(onM, [2], "USV-2 response applies");
});

test("no overlapping mission requests while one is in flight", async () => {
  let count = 0;
  const d = deferred();
  const c = createSelectedRefresh({
    ...base,
    fetchMission: () => { count++; return d.promise; },
    onMission: () => {},
  });
  c.select(5);   // fetch #1 in flight
  c.tick();      // fallback while in flight → suppressed
  c.tick();
  assert.equal(count, 1);
  d.resolve(REACHABLE());
  await flush();
});

test("repeated ticks refresh lightweight state", async () => {
  let states = 0;
  const c = createSelectedRefresh({
    ...base,
    fetchState: () => { states++; return Promise.resolve({}); },
    fetchMission: () => Promise.resolve(REACHABLE()),
    onState: () => {}, onMission: () => {},
  });
  c.select(1); await flush();
  c.tick(); await flush();
  c.tick(); await flush();
  assert.equal(states, 3);
});

test("the full mission is not re-fetched while its identity is fresh (fallback)", async () => {
  let count = 0, nowVal = 1000;
  const c = createSelectedRefresh({
    ...base, now: () => nowVal, missionFallbackMs: 20000,
    fetchMission: () => { count++; return Promise.resolve(REACHABLE()); },
    onMission: () => {},
  });
  c.select(1); await flush();               // #1, cached at 1000
  nowVal = 1100; c.tick(); await flush();    // fresh → no fetch
  assert.equal(count, 1);
  nowVal = 21000; c.tick(); await flush();   // window elapsed → re-fetch
  assert.equal(count, 2);
});

test("a command trigger forces a mission re-fetch", async () => {
  let count = 0;
  const c = createSelectedRefresh({
    ...base, now: () => 1000,
    fetchMission: () => { count++; return Promise.resolve(REACHABLE()); },
    onMission: () => {},
  });
  c.select(1); await flush();
  c.refreshMission(1, "command"); await flush();
  assert.equal(count, 2);
});

test("onMission reports geometryChanged: true first, false on unchanged", async () => {
  const onM = [];
  const mission = REACHABLE({ route_content_hash: "abc" });
  const c = createSelectedRefresh({
    ...base,
    fetchMission: () => Promise.resolve(mission),
    onMission: (id, m, meta) => onM.push(meta.geometryChanged),
  });
  c.select(1); await flush();                    // first → changed
  c.refreshMission(1, "manual"); await flush();  // same identity → unchanged
  assert.deepEqual(onM, [true, false]);
});

test("a new current_seq is delivered even when geometry is unchanged", async () => {
  const events = [];
  let call = 0;
  const reads = [
    { reachable: true, route_content_hash: "abc", current_seq: 1 },
    { reachable: true, route_content_hash: "abc", current_seq: 2 }, // same geometry, advanced
  ];
  const c = createSelectedRefresh({
    ...base,
    fetchMission: () => Promise.resolve(reads[call++]),
    onMission: (id, m, meta) => events.push({ seq: m.current_seq, meta }),
  });
  c.select(1); await flush();                    // read #0
  c.refreshMission(1, "manual"); await flush();  // read #1: same route_content_hash, new seq
  assert.equal(events[1].meta.geometryChanged, false, "geometry (route_content_hash) unchanged");
  assert.equal(events[1].meta.progressChanged, true, "current_seq advanced");
  assert.equal(events[1].seq, 2, "new current_seq delivered to the progress consumer");
});

test("in-flight ownership is token/vehicle-specific: no cross-clear, no overlap, resumes after", async () => {
  const calls = []; // each fetch: { id, resolve }
  const c = createSelectedRefresh({
    ...base,
    fetchMission: (id) => new Promise((resolve) => calls.push({ id, resolve })),
    onMission: () => {},
  });
  // 1. a mission request is pending for USV A (=1)
  c.select(1);
  assert.deepEqual([calls.length, calls[0].id], [1, 1]);
  assert.equal(c._state().missionSlot.id, 1);
  // 2-3. selection changes to USV B (=2); B's mission request begins
  c.select(2);
  assert.deepEqual([calls.length, calls[1].id], [2, 2]);
  const slotB = c._state().missionSlot;
  assert.equal(slotB.id, 2);
  // 4-5. A finishes while B is still pending → must NOT clear B's ownership
  calls[0].resolve(REACHABLE({ count: 1 }));
  await flush();
  assert.deepEqual(c._state().missionSlot, slotB, "A's completion left B's slot intact");
  // 6. a timer tick or a command trigger must not start a second B request while B is pending
  c.tick(); await flush();
  c.refreshMission(2, "command"); await flush();
  assert.equal(calls.length, 2, "no second B request while B is in flight");
  // 7. once B finishes, a later valid trigger may start another request
  calls[1].resolve(REACHABLE({ count: 2 }));
  await flush();
  assert.equal(c._state().missionSlot, null, "B slot cleared after B completed");
  c.refreshMission(2, "command"); await flush();
  assert.equal(calls.length, 3, "a fresh trigger after B finished starts a new request");
});

test("an unreachable read is delivered but never cached (keeps last-known)", async () => {
  let count = 0, nowVal = 1000;
  const seq = [REACHABLE({ route_content_hash: "a" }), { reachable: false }];
  const c = createSelectedRefresh({
    ...base, now: () => nowVal, missionFallbackMs: 20000,
    fetchMission: () => Promise.resolve(seq[count++]),
    onMission: () => {},
  });
  c.select(1); await flush();                 // reachable cached at 1000
  nowVal = 21000; c.tick(); await flush();    // fallback → unreachable, NOT cached
  nowVal = 21001; c.tick(); await flush();    // still treated as never-cached → fetches again
  assert.equal(count, 3);
});

test("a hidden tab pauses the interval refresh", async () => {
  let states = 0, hidden = true;
  const c = createSelectedRefresh({
    ...base, isHidden: () => hidden,
    fetchState: () => { states++; return Promise.resolve({}); },
    fetchMission: () => Promise.resolve(REACHABLE()),
    onState: () => {}, onMission: () => {},
  });
  c.select(1); await flush();      // immediate on select regardless → states 1
  c.tick(); await flush();         // hidden → skipped
  assert.equal(states, 1);
  hidden = false;
  c.tick(); await flush();         // visible → refreshes
  assert.equal(states, 2);
});

test("stop halts all further work", async () => {
  let count = 0;
  const c = createSelectedRefresh({
    ...base,
    fetchState: () => { count++; return Promise.resolve({}); },
    fetchMission: () => Promise.resolve(REACHABLE()),
    onState: () => {}, onMission: () => {},
  });
  c.select(1); await flush();
  const after = count;
  c.stop();
  c.tick(); await flush();
  assert.equal(count, after);
});
