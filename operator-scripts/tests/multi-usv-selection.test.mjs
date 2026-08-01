// Frontend multi-USV isolation — the UI half of the two-live-vehicle bug.
//
// With Scout (usv-2) and SAR-001 (usv-3) both reporting, the fleet feed alternated between
// "Scout complete / SAR unknown" and "SAR complete / Scout unknown" every couple of seconds.
// The backend fix means the feed no longer does that — these tests pin the frontend rules
// that must hold even when it does, so a degraded feed can never move the operator's
// selection or bleed one vehicle's values into another's row:
//
//   • selection is keyed ONLY by canonical vehicle id — never by display name, never by
//     array position, never by "the vehicle that is currently connected";
//   • repeated fleet updates, reordering, renaming and going stale leave selection alone;
//   • per-USV telemetry caches stay independent;
//   • a vehicle reporting nothing this poll blanks nobody else.
//
// Run: `node --test tests/` (or `npm test`).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  canonicalVehicleId, sameVehicle, getSelectedVehicleId, setSelectedVehicleId,
  subscribeSelection, _setStorageForTest,
} from "../operator/lib/selection.js";
import { createTelemetryCache } from "../operator/lib/telemetry-cache.js";

function fakeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

// A fleet row in the shape GET /api/fleet/status returns.
const veh = (id, name, extra = {}) => ({
  id,
  vehicle_id: typeof id === "number" ? `usv-${id}` : String(id),
  name,
  online: true,
  comm_state: "CONNECTED",
  last_seen_age_s: 0.4,
  battery: null,
  heading: null,
  speed: null,
  lat: null,
  lng: null,
  telemetry: {},
  ...extra,
});

const SCOUT = (extra) => veh(2, "Scout", { battery: 79, heading: 90, speed: 1.2, lat: 56.70, lng: 13.00, telemetry: { mode: "AUTO" }, ...extra });
const SAR = (extra) => veh(3, "SAR-001", { battery: 50, heading: 180, speed: 0.4, lat: 56.71, lng: 13.01, telemetry: { mode: "MANUAL" }, ...extra });

// ---------------------------------------------------------------------------------
// Canonical identity
// ---------------------------------------------------------------------------------

test("every spelling of one vehicle's id folds to a single canonical value", () => {
  for (const spelling of [2, "2", "usv-2", "USV-2", " usv_2 "]) {
    assert.equal(canonicalVehicleId(spelling), 2, `${JSON.stringify(spelling)} → 2`);
  }
  assert.equal(canonicalVehicleId("sar-001"), "sar-001");
  assert.equal(canonicalVehicleId("SAR_001"), "sar-001", "separator style is spelling, not identity");
  for (const notAnId of [null, undefined, "", "   ", {}, [], true, NaN]) {
    assert.equal(canonicalVehicleId(notAnId), null, `${JSON.stringify(notAnId)} names no vehicle`);
  }
});

test("numeric and string forms of one id are never two selections", () => {
  _setStorageForTest(fakeStorage());
  const seen = [];
  subscribeSelection((id) => seen.push(id));
  setSelectedVehicleId(2);
  setSelectedVehicleId("2");
  setSelectedVehicleId("usv-2");
  assert.deepEqual(seen, [2], "the same vehicle in three spellings is ONE selection change");
  assert.equal(getSelectedVehicleId(), 2);
});

test("sameVehicle compares identities, not spellings — and never matches a non-id", () => {
  assert.ok(sameVehicle(2, "usv-2"));
  assert.ok(sameVehicle("USV-3", 3));
  assert.ok(!sameVehicle(2, 3));
  assert.ok(!sameVehicle(null, null), "two non-ids are not the same vehicle");
});

test("a display name is not resolvable as an identity", () => {
  // "Scout" must never resolve to usv-2 in the frontend: names are display data a vehicle
  // can change, and keying selection or caches by one is what made USV-3/SAR-001 flip.
  assert.equal(canonicalVehicleId("Scout"), "scout");
  assert.notEqual(canonicalVehicleId("Scout"), 2);
  assert.ok(!sameVehicle("Scout", 2));
});

// ---------------------------------------------------------------------------------
// Selection stability across fleet updates
// ---------------------------------------------------------------------------------

// The rule every page's onFleet must obey: a fleet payload replaces the ROSTER, never the
// SELECTION. Only an explicit operator select, or the selected vehicle leaving the fleet,
// may change it. This models that contract so it can be asserted against many payloads.
function applyFleet(rows, { select = null } = {}) {
  if (select !== null) setSelectedVehicleId(select);
  const selId = getSelectedVehicleId();
  return {
    rows,
    selId,
    selectedRow: rows.find((v) => sameVehicle(v.id, selId)) || null,
  };
}

test("selecting Scout survives repeated fleet updates", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(2);
  for (let i = 0; i < 20; i++) {
    const state = applyFleet([SCOUT({ battery: 79 - i }), SAR({ battery: 50 - i })]);
    assert.equal(state.selId, 2);
    assert.equal(state.selectedRow.name, "Scout");
  }
});

test("SAR packets never change a selected Scout", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(2);
  // SAR reports and reports; Scout's row is unchanged and stale-free. Selection holds.
  for (let i = 0; i < 10; i++) {
    applyFleet([SCOUT(), SAR({ battery: 50 - i, telemetry: { mode: i % 2 ? "HOLD" : "MANUAL" } })]);
    assert.equal(getSelectedVehicleId(), 2);
  }
});

test("a display-name change does not change the canonical selection", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(3);
  const a = applyFleet([SCOUT(), veh(3, "USV-3")]);
  const b = applyFleet([SCOUT(), veh(3, "SAR-001")]);
  assert.equal(a.selId, 3);
  assert.equal(b.selId, 3, "usv-3 renaming itself is not a new vehicle");
  assert.equal(b.selectedRow.name, "SAR-001");
});

test("fleet array reordering does not change selection", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(2);
  const orders = [
    [SCOUT(), SAR(), veh(4, "Probe-4")],
    [SAR(), veh(4, "Probe-4"), SCOUT()],
    [veh(4, "Probe-4"), SCOUT(), SAR()],
  ];
  for (const rows of orders) {
    const state = applyFleet(rows);
    assert.equal(state.selId, 2, "selection must not be derived from array position");
    assert.equal(state.selectedRow.name, "Scout");
  }
});

test("a stale or disconnected selected vehicle remains selected", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(2);
  const stale = applyFleet([
    SCOUT({ comm_state: "DISCONNECTED", online: false, last_seen_age_s: 92 }),
    SAR(),
  ]);
  assert.equal(stale.selId, 2, "going stale is not a reason to switch vehicles");
  assert.equal(stale.selectedRow.comm_state, "DISCONNECTED");
});

test("no automatic selection of the most recently connected USV", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(2);
  // Alternating "who is live" — the exact shape of the reported bug. Selection must be flat.
  const changes = [];
  subscribeSelection((id) => changes.push(id));
  for (let i = 0; i < 12; i++) {
    const scoutLive = i % 2 === 0;
    applyFleet([
      SCOUT({ comm_state: scoutLive ? "CONNECTED" : "DISCONNECTED", online: scoutLive }),
      SAR({ comm_state: scoutLive ? "DISCONNECTED" : "CONNECTED", online: !scoutLive }),
    ]);
  }
  assert.deepEqual(changes, [], "alternating interleaved fleet responses caused no UI state change");
  assert.equal(getSelectedVehicleId(), 2);
});

test("an unselected roster does not auto-select the first row", () => {
  _setStorageForTest(fakeStorage());
  const state = applyFleet([SCOUT(), SAR()]);
  assert.equal(state.selId, null, "selection must not fall out of list position");
});

test("adding a fourth USV needs no special branch", () => {
  _setStorageForTest(fakeStorage());
  const rows = [SCOUT(), SAR(), veh(4, "Probe-4", { battery: 33 }), veh("probe-alpha", "Probe Alpha", { battery: 61 })];
  for (const row of rows) {
    setSelectedVehicleId(row.id);
    const state = applyFleet(rows);
    assert.equal(state.selectedRow.name, row.name, `${row.vehicle_id} selectable through the same path`);
    assert.ok(sameVehicle(state.selId, row.vehicle_id), "slug and id agree for every vehicle");
  }
});

test("selection is only cleared by an explicit operator action", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(3);
  applyFleet([SCOUT()]);                       // SAR temporarily missing from the payload
  assert.equal(getSelectedVehicleId(), 3, "a missing row does not silently clear selection");
  setSelectedVehicleId(null);                  // the operator (or an explicit removal) does
  assert.equal(getSelectedVehicleId(), null);
});

// ---------------------------------------------------------------------------------
// Per-USV caches
// ---------------------------------------------------------------------------------

test("per-USV telemetry caches stay independent under interleaved partial updates", () => {
  const cache = createTelemetryCache();
  cache.mergeFleet([SCOUT({ battery: 79 }), SAR({ battery: 50 })]);

  // Scout reports without a battery; SAR reports one. Each keeps its OWN last-known.
  let merged = cache.mergeFleet([SCOUT({ battery: null }), SAR({ battery: 49 })]);
  assert.equal(merged.find((v) => v.id === 2).battery, 79, "Scout keeps its own last-known");
  assert.equal(merged.find((v) => v.id === 3).battery, 49);

  // Now the reverse.
  merged = cache.mergeFleet([SCOUT({ battery: 77 }), SAR({ battery: null })]);
  assert.equal(merged.find((v) => v.id === 2).battery, 77);
  assert.equal(merged.find((v) => v.id === 3).battery, 49, "SAR keeps its own last-known");

  assert.equal(cache.get(2).battery, 77);
  assert.equal(cache.get(3).battery, 49);
  assert.notEqual(cache.get(2).battery, cache.get(3).battery,
    "one vehicle's battery must never appear on another");
});

test("incomplete data for one vehicle does not blank another", () => {
  const cache = createTelemetryCache();
  cache.mergeFleet([SCOUT(), SAR()]);
  // Scout drops out of telemetry entirely (degraded packet): SAR is untouched.
  const merged = cache.mergeFleet([
    veh(2, "Scout", { comm_state: "PARTITIONED", online: true, last_seen_age_s: 17 }),
    SAR(),
  ]);
  const sar = merged.find((v) => v.id === 3);
  assert.equal(sar.battery, 50);
  assert.equal(sar.heading, 180);
  assert.equal(sar.speed, 0.4);
  const scout = merged.find((v) => v.id === 2);
  assert.equal(scout.battery, 79, "Scout shows its own last-known…");
  assert.equal(scout.comm_state, "PARTITIONED", "…and is still visibly marked stale");
});

test("no per-USV cache is keyed by display name", () => {
  const cache = createTelemetryCache();
  cache.mergeFleet([veh(3, "USV-3", { battery: 50 })]);
  // The same vehicle, renamed. A name-keyed cache would lose the last-known value here.
  const merged = cache.mergeFleet([veh(3, "SAR-001", { battery: null })]);
  assert.equal(merged[0].battery, 50, "the cache followed the id, not the name");
  assert.deepEqual(cache.get("SAR-001"), {}, "nothing is stored under a display name");
});

test("stale retained values are never presented as fresh, and position is never fabricated", () => {
  const cache = createTelemetryCache();
  cache.mergeFleet([SCOUT()]);
  const merged = cache.mergeFleet([
    veh(2, "Scout", { comm_state: "DISCONNECTED", online: false, last_seen_age_s: 90 }),
  ])[0];
  assert.equal(merged.battery, 79, "last-known battery is retained");
  assert.equal(merged.comm_state, "DISCONNECTED", "freshness is untouched by the cache");
  assert.equal(merged.lat, null, "a retained position is NOT plotted as current");
  assert.equal(merged.lng, null);
});

test("a battery of null or -1 does not erase a valid previous reading", () => {
  const cache = createTelemetryCache();
  cache.mergeFleet([SCOUT({ battery: 79 }), SAR({ battery: 50 })]);
  const merged = cache.mergeFleet([SCOUT({ battery: null }), SAR({ battery: null })]);
  assert.equal(merged[0].battery, 79);
  assert.equal(merged[1].battery, 50);
});

// ---------------------------------------------------------------------------------
// Cross-page agreement
// ---------------------------------------------------------------------------------

test("Map, Fleet and Vehicle read the same canonical selected id", async () => {
  _setStorageForTest(fakeStorage());
  // Every page imports the same module instance, so a selection made on one is the
  // selection every other page reads — there is no page-local copy to drift.
  const mod = await import("../operator/lib/selection.js");
  mod.setSelectedVehicleId("usv-3");
  assert.equal(getSelectedVehicleId(), 3);
  assert.equal(mod.getSelectedVehicleId(), 3);
  setSelectedVehicleId(2);
  assert.equal(mod.getSelectedVehicleId(), 2);
});

test("pages that consume the shared selection use no display-name or index keys", async () => {
  // A structural guard: the pages wired to the shared store must select via
  // setSelectedVehicleId/canonicalVehicleId, not `+dataset.id` (NaN for a string id) and
  // not fleet[0]. Cheap to keep true, and it is exactly what regressed before.
  const { readFile } = await import("node:fs/promises");
  for (const page of ["Map.js", "Fleet.js", "Vehicle.js"]) {
    const src = await readFile(new URL(`../operator/pages/${page}`, import.meta.url), "utf8");
    assert.ok(src.includes("lib/selection.js"), `${page} must use the shared selection store`);
    assert.ok(!/\+\s*(el|tr|b)\.dataset\.id/.test(src),
      `${page} must not coerce a vehicle id with + (breaks non-numeric canonical ids)`);
    assert.ok(!/selId\s*=\s*fleet\[0\]/.test(src), `${page} must not select by array position`);
  }
});
