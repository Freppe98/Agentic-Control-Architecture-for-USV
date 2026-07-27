// Unit tests for the Plan-page initial-view policy + coordinate validation
// (operator/lib/map-view.js).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  isValidLatLng, isNullIsland, freshVehiclePosition, bestFleetPosition,
  pickInitialView, getSavedViewport, setSavedViewport, _setStorageForTest,
  TOFTASJON, DEFAULT_ZOOM, VIEW_RANK,
} from "../operator/lib/map-view.js";

function fakeStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
}

// ---- coordinate validation ----
test("isValidLatLng accepts in-range numbers, rejects the rest", () => {
  assert.equal(isValidLatLng(56.7, 13.0), true);
  assert.equal(isValidLatLng(-90, 180), true);
  assert.equal(isValidLatLng(91, 13), false);
  assert.equal(isValidLatLng(56, 181), false);
  assert.equal(isValidLatLng(NaN, 13), false);
  assert.equal(isValidLatLng("56.7", 13), false);
  assert.equal(isValidLatLng(null, null), false);
});
test("isNullIsland flags (0,0) and its neighbourhood", () => {
  assert.equal(isNullIsland(0, 0), true);
  assert.equal(isNullIsland(0.00001, -0.00002), true);
  assert.equal(isNullIsland(56.7, 13.0), false);
});

// ---- freshness ----
const veh = (o) => ({ id: 1, lat: 56.7, lng: 13.0, last_seen_age_s: 5, ...o });
test("freshVehiclePosition requires valid, non-null-island, fresh", () => {
  assert.deepEqual(freshVehiclePosition(veh({})), { lat: 56.7, lng: 13.0, ageS: 5 });
  assert.equal(freshVehiclePosition(veh({ last_seen_age_s: 999 })), null); // stale
  assert.equal(freshVehiclePosition(veh({ last_seen_age_s: null })), null); // unknown age
  assert.equal(freshVehiclePosition(veh({ lat: 0, lng: 0 })), null);        // null island
  assert.equal(freshVehiclePosition(veh({ lat: 200 })), null);              // invalid
  assert.equal(freshVehiclePosition(null), null);
});
test("bestFleetPosition picks the most recently contacted, excludes id, skips bad", () => {
  const fleet = [
    veh({ id: 1, last_seen_age_s: 30 }),
    veh({ id: 2, last_seen_age_s: 3, lat: 57.0 }),
    veh({ id: 3, last_seen_age_s: 999 }),      // stale — skipped
    veh({ id: 4, lat: 0, lng: 0 }),            // null island — skipped
  ];
  assert.deepEqual(bestFleetPosition(fleet, { excludeId: 5 }), { lat: 57.0, lng: 13.0, ageS: 3, id: 2 });
  // exclude the freshest → next freshest valid one wins
  assert.equal(bestFleetPosition(fleet, { excludeId: 2 }).id, 1);
});

// ---- priority order ----
test("selected fresh USV wins (rank 1)", () => {
  const v = pickInitialView({ selected: veh({}), fleet: [veh({ id: 9, lat: 57 })], selectedId: 1 });
  assert.deepEqual(v.center, [56.7, 13.0]);
  assert.equal(v.source, "selected");
  assert.equal(v.rank, VIEW_RANK.selected);
});
test("stale selected falls through to a fresh other fleet USV (rank 2)", () => {
  const v = pickInitialView({ selected: veh({ last_seen_age_s: 999 }), selectedId: 1, fleet: [veh({ id: 2, lat: 57, last_seen_age_s: 4 })] });
  assert.equal(v.source, "fleet");
  assert.deepEqual(v.center, [57, 13.0]);
});
test("no fresh USV → geolocation (rank 3)", () => {
  const v = pickInitialView({ fleet: [], geo: { lat: 55.6, lng: 12.9 }, saved: { center: [1, 1], zoom: 10 } });
  assert.equal(v.source, "geolocation");
  assert.deepEqual(v.center, [55.6, 12.9]);
});
test("no geo → saved viewport (rank 4)", () => {
  const v = pickInitialView({ fleet: [], saved: { center: [55.6, 12.9], zoom: 12 } });
  assert.equal(v.source, "saved");
  assert.equal(v.zoom, 12);
});
test("nothing valid → Toftasjön fallback (rank 5)", () => {
  const v = pickInitialView({ fleet: [], geo: { lat: 0, lng: 0 }, saved: { center: [999, 0], zoom: 9 } });
  assert.equal(v.source, "fallback");
  assert.deepEqual(v.center, [TOFTASJON[0], TOFTASJON[1]]);
  assert.equal(v.zoom, DEFAULT_ZOOM);
});

// ---- viewport persistence ----
test("setSavedViewport / getSavedViewport round-trip, rejecting invalid", () => {
  _setStorageForTest(fakeStorage());
  assert.equal(getSavedViewport(), null);
  assert.equal(setSavedViewport([56.7, 13.0], 15), true);
  assert.deepEqual(getSavedViewport(), { center: [56.7, 13.0], zoom: 15 });
  assert.equal(setSavedViewport([999, 0], 15), false);          // invalid centre not persisted
  assert.deepEqual(getSavedViewport(), { center: [56.7, 13.0], zoom: 15 }); // unchanged
});
