// map-layout.test.mjs — the pure arithmetic behind the shared Leaflet layout contract
// (operator/lib/map-layout.js). The DOM/ResizeObserver wiring is exercised in the browser
// (docs/verification/responsive-layout.md); what is unit-tested here is the part that
// silently produces a wrong offset if it drifts: how much space a map corner is reserving,
// and that a burst of resize notifications collapses into one invalidateSize.
import { test } from "node:test";
import assert from "node:assert/strict";
import { cornerExtent, frameCoalesced } from "../operator/lib/map-layout.js";

test("empty corner reserves nothing", () => {
  assert.deepEqual(cornerExtent([], 8), { h: 0, w: 0 });
  assert.deepEqual(cornerExtent(null, 8), { h: 0, w: 0 });
});

test("single overlay reserves its own box, with no gap added", () => {
  assert.deepEqual(cornerExtent([{ width: 80, height: 64 }], 8), { h: 64, w: 80 });
});

test("stacked overlays sum their heights and add one gap between each pair", () => {
  const r = cornerExtent([{ width: 80, height: 64 }, { width: 120, height: 30 }], 8);
  assert.equal(r.h, 64 + 30 + 8);
  assert.equal(r.w, 120, "width is the widest overlay, not the sum");
});

test("a hidden overlay contributes neither height nor a phantom gap", () => {
  // A conditional overlay (the Plan status banner before a plan exists) collapses to a
  // zero box. If it still counted as a stack member, the Leaflet zoom control would sit
  // 8px lower than it should on every viewport, forever.
  const r = cornerExtent([{ width: 0, height: 0 }, { width: 120, height: 30 }], 8);
  assert.deepEqual(r, { h: 30, w: 120 });
});

test("all-hidden corner is exactly zero, not a bare gap", () => {
  assert.deepEqual(cornerExtent([{ width: 0, height: 0 }, { width: 0, height: 0 }], 8), { h: 0, w: 0 });
});

test("frameCoalesced runs the work once per frame however often it is called", () => {
  // A drag-resize fires the ResizeObserver dozens of times per second and each raw
  // invalidateSize is a full Leaflet re-layout.
  let scheduled = null, calls = 0, ids = 0;
  const schedule = (fn) => { scheduled = fn; return ++ids; };
  const run = frameCoalesced(() => { calls++; }, schedule, () => {});
  run(); run(); run();
  assert.equal(calls, 0, "nothing runs before the frame");
  scheduled();
  assert.equal(calls, 1, "the whole burst collapsed into one call");
  run();
  scheduled();
  assert.equal(calls, 2, "a later burst schedules again");
});

test("cancel drops a pending frame so a removed map is never invalidated", () => {
  let scheduled = null, calls = 0, cancelled = [];
  const run = frameCoalesced(() => { calls++; }, (fn) => { scheduled = fn; return 7; }, (id) => cancelled.push(id));
  run();
  run.cancel();
  assert.deepEqual(cancelled, [7]);
  run.cancel();
  assert.deepEqual(cancelled, [7], "cancelling twice is a no-op");
  // after cancelling, a fresh call must still be able to schedule
  run();
  scheduled();
  assert.equal(calls, 1);
});
