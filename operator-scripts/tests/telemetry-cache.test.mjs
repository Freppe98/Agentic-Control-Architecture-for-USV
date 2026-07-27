// Unit tests for the per-USV last-known telemetry merge (operator/lib/telemetry-cache.js) —
// the fix for the ~2 s battery flicker (97% → "—" → 97%).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createTelemetryCache, MERGED_FIELDS } from "../operator/lib/telemetry-cache.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const veh = (id, over = {}) => ({ id, name: "USV-" + id, comm_state: "CONNECTED", lat: 56.7, lng: 13.0, battery: null, ...over });

test("a valid value followed by a partial update with no battery stays 97", () => {
  const c = createTelemetryCache();
  assert.equal(c.merge(veh(2, { battery: 97 })).battery, 97);
  assert.equal(c.merge(veh(2, { battery: null })).battery, 97, "absent battery keeps last-known");
  assert.equal(c.merge(veh(2, {})).battery, 97, "undefined battery keeps last-known");
});

test("battery:null does not clear a last-known value (null is absence, not a clear signal)", () => {
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97 }));
  assert.equal(c.merge(veh(2, { battery: null })).battery, 97);
});

test("a newer valid value replaces the old value", () => {
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97 }));
  assert.equal(c.merge(veh(2, { battery: 88 })).battery, 88);
  assert.equal(c.merge(veh(2, { battery: 0 })).battery, 0, "a real 0% is a valid value, not absence");
});

test("stale age keeps the retained value (does not become '—'); freshness is separate", () => {
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97, comm_state: "CONNECTED", last_seen_age_s: 1 }));
  // A degraded poll: link stale, battery absent this packet.
  const out = c.merge(veh(2, { battery: null, comm_state: "DISCONNECTED", last_seen_age_s: 42 }));
  assert.equal(out.battery, 97, "value retained through degradation");
  assert.equal(out.comm_state, "DISCONNECTED", "freshness carried through untouched");
  assert.equal(out.last_seen_age_s, 42, "age carried through so the UI marks it stale");
});

test("first-ever missing battery displays as absent (null → '—')", () => {
  const c = createTelemetryCache();
  assert.equal(c.merge(veh(2, { battery: null })).battery, null);
  assert.equal(c.merge(veh(2, {})).battery, null);
});

test("an explicit unavailable/reset signal clears the cached value", () => {
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97 }));
  const out = c.merge(veh(2, { battery: null, battery_available: false }));
  assert.equal(out.battery, null, "explicit battery_available:false clears");
  // and it stays cleared until a new valid value arrives
  assert.equal(c.merge(veh(2, { battery: null })).battery, null);
  assert.equal(c.merge(veh(2, { battery: 90 })).battery, 90);
});

test("per-USV caches are independent: USV A battery never affects USV B", () => {
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97 }));
  c.merge(veh(3, { battery: 40 }));
  // A partial poll where BOTH omit battery must keep each USV's own value.
  const fleet = c.mergeFleet([veh(2, { battery: null }), veh(3, { battery: null })]);
  assert.equal(fleet.find((v) => v.id === 2).battery, 97);
  assert.equal(fleet.find((v) => v.id === 3).battery, 40);
  // A brand-new USV with no history shows absent, unaffected by others.
  assert.equal(c.merge(veh(9, { battery: null })).battery, null);
});

test("alternating full and partial fleet updates do not flicker (97 → absent → 97 …)", () => {
  const c = createTelemetryCache();
  const seq = [97, null, undefined, null, 97, null];
  const shown = seq.map((b) => c.mergeFleet([veh(2, b === undefined ? {} : { battery: b })])[0].battery);
  assert.deepEqual(shown, [97, 97, 97, 97, 97, 97], "battery never drops to null between valid reads");
});

test("the merge covers battery, speed and heading (other partial numeric fields)", () => {
  assert.deepEqual(MERGED_FIELDS, ["battery", "speed", "heading"]);
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97, speed: 1.4, heading: 210 }));
  const out = c.merge(veh(2, { battery: null, speed: null, heading: null }));
  assert.deepEqual([out.battery, out.speed, out.heading], [97, 1.4, 210]);
});

test("position (lat/lng) is NOT retained by the cache — never plot a stale marker as current", () => {
  const c = createTelemetryCache();
  c.merge(veh(2, { battery: 97, lat: 56.7, lng: 13.0 }));
  const out = c.merge(veh(2, { battery: null, lat: null, lng: null }));
  assert.equal(out.lat, null, "absent position stays absent (not last-known)");
  assert.equal(out.lng, null);
});

// ---- the 2-second refresh must remain enabled (not reduced/disabled) ----
test("Map page keeps the 2000 ms fleet poll and merges through the cache", () => {
  const MAP_SRC = readFileSync(join(HERE, "..", "operator", "pages", "Map.js"), "utf-8");
  assert.match(MAP_SRC, /api\.getFleet,\s*2000/, "2 s fleet poll unchanged");
  assert.match(MAP_SRC, /telemCache\.mergeFleet/, "onFleet routes through the per-USV merge");
});
