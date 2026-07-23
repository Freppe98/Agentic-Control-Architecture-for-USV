// Unit tests for the Plan page's pure state model (operator/lib/planning.js).
// Run: `node --test tests/` (or `npm test`).
//
// Pins the SMALL page-specific state machine and the invariants the task requires of it:
// (1) Plan appears in the frozen navigation; (2) drawing a boundary enables the tools that
// depend on it; (3) no-go zones only after a boundary, with stable local ids; (4) any
// generation-affecting parameter change marks the route OUTDATED; (5) upload is gated on a
// current, validated route + a selected vehicle; (6) Clear resets everything and asks only
// when there is work to lose; (7) a draft round-trips; (8) a generated route maps onto the
// existing mission-contract upload params (no second framework).
import { test } from "node:test";
import assert from "node:assert/strict";
import { NAV } from "../operator/lib/ui.js";
import * as P from "../operator/lib/planning.js";

// A small square boundary near the project home (GeoJSON [lng,lat], closed).
const RING = [[13.000, 56.699], [13.004, 56.699], [13.004, 56.7005], [13.000, 56.7005], [13.000, 56.699]];
const ZONE = [[13.0018, 56.6996], [13.0022, 56.6996], [13.0022, 56.6999], [13.0018, 56.6999]];

// A stand-in generate() result (shape only — geometry math is the backend's, tested there).
function fakeGenerated(model, wpCount = 12) {
  return {
    ok: true,
    segments: [{ kind: "primary", coordinates: [[13.0, 56.699], [13.004, 56.699]], length_m: 200 }],
    route_waypoints: Array.from({ length: wpCount }, (_, i) => ({ latitude: 56.699 + i * 1e-4, longitude: 13.0 + i * 1e-4, loiter_time_s: 0 })),
    metrics: { waypoint_count: wpCount, total_length_m: 1000, dual_pass: false },
    navigable_boundary: [], warnings: [], generated_at: new Date().toISOString(),
  };
}

// ---- (1) navigation --------------------------------------------------------
test("Plan appears in the navigation between Fleet and Mission", () => {
  const keys = NAV.map(([k]) => k);
  assert.ok(keys.includes("plan"), "plan route present in NAV");
  assert.equal(keys.indexOf("plan"), keys.indexOf("fleet") + 1, "plan sits right after fleet");
  assert.equal(keys.indexOf("mission"), keys.indexOf("plan") + 1, "mission follows plan");
});

// ---- (2) boundary enables dependent tools ----------------------------------
test("EMPTY until a boundary exists; drawing it enables no-go zones", () => {
  let m = P.emptyModel();
  assert.equal(P.planState(m), P.PLAN_STATES.EMPTY);
  assert.equal(P.hasBoundary(m), false);
  assert.equal(P.canAddZone(m), false, "no-go disabled without a boundary");
  m = P.setBoundary(m, RING);
  assert.equal(P.hasBoundary(m), true);
  assert.equal(P.canAddZone(m), true, "no-go enabled once a boundary exists");
  assert.equal(P.planState(m), P.PLAN_STATES.BOUNDARY_DEFINED);
});

test("state advances to CONFIGURED once lane spacing is set", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  assert.equal(P.canGenerate(m), false);
  m = P.setParam(m, "lane_spacing_m", 25);
  assert.equal(P.canGenerate(m), true);
  assert.equal(P.planState(m), P.PLAN_STATES.CONFIGURED);
});

// ---- (3) no-go zones: stable ids, add/remove -------------------------------
test("no-go zones get stable local ids and can be removed", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  m = P.addNoGoZone(m, ZONE);
  m = P.addNoGoZone(m, ZONE.map((p) => [p[0] + 0.001, p[1]]));
  assert.equal(m.noGoZones.length, 2);
  assert.deepEqual(m.noGoZones.map((z) => z.id), ["ngz-1", "ngz-2"]);
  const id0 = m.noGoZones[0].id;
  m = P.removeNoGoZone(m, id0);
  assert.equal(m.noGoZones.length, 1);
  assert.equal(m.noGoZones[0].id, "ngz-2", "remaining id is unchanged");
  m = P.addNoGoZone(m, ZONE);           // ids never reused within a session
  assert.equal(m.noGoZones[1].id, "ngz-3");
});

// ---- (4) parameter change marks the route outdated -------------------------
test("changing a generation input marks a generated route OUTDATED", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m));
  assert.equal(P.isOutdated(m), false);
  assert.equal(P.planState(m), P.PLAN_STATES.ROUTE_GENERATED);
  const before = P.inputRevision(m);
  m = P.setParam(m, "lane_spacing_m", 10);      // spacing change
  assert.notEqual(P.inputRevision(m), before);
  assert.equal(P.isOutdated(m), true);
  assert.equal(P.planState(m), P.PLAN_STATES.ROUTE_OUTDATED);
});

test("geometry change (a no-go zone) also marks the route outdated", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m));
  m = P.addNoGoZone(m, ZONE);
  assert.equal(P.isOutdated(m), true);
});

test("angle and dual-pass changes change the revision", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  const base = P.inputRevision(m);
  assert.notEqual(P.inputRevision(P.setParam(m, "primary_angle_deg", 45)), base);
  assert.notEqual(P.inputRevision(P.setParam(m, "dual_pass", true)), base);
});

// ---- secondary angle default ----------------------------------------------
test("secondary angle defaults to primary + 90", () => {
  const m = P.setParam(P.emptyModel(), "primary_angle_deg", 30);
  assert.equal(P.effectiveSecondaryAngle(m.params), 120);
  const m2 = P.setParam(m, "secondary_angle_deg", 200);
  assert.equal(P.effectiveSecondaryAngle(m2.params), 200);
});

// ---- (5) validation + upload gating ----------------------------------------
test("VALID only after a passing validation; upload gated on vehicle + current route", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m));
  assert.equal(P.canUpload(m), false, "no vehicle, not validated");
  m = P.applyValidation(m, { ok: true, errors: [], warnings: [], checks: {} });
  assert.equal(P.planState(m), P.PLAN_STATES.VALID);
  assert.equal(P.canUpload(m), false, "still no vehicle selected");
  m = { ...m, vehicleId: 2 };
  assert.equal(P.canUpload(m), true);
});

test("an outdated route cannot be uploaded even if previously validated", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m));
  m = P.applyValidation(m, { ok: true, errors: [], warnings: [], checks: {} });
  m = { ...m, vehicleId: 2 };
  assert.equal(P.canUpload(m), true);
  m = P.setParam(m, "lane_spacing_m", 10);   // outdates the route
  assert.equal(P.canUpload(m), false);
  assert.equal(P.planState(m), P.PLAN_STATES.ROUTE_OUTDATED);
});

test("a failing validation blocks upload and is not VALID", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = { ...P.applyGenerated(m, fakeGenerated(m)), vehicleId: 2 };
  m = P.applyValidation(m, { ok: false, errors: ["boom"], warnings: [], checks: {} });
  assert.notEqual(P.planState(m), P.PLAN_STATES.VALID);
  assert.equal(P.canUpload(m), false);
});

// ---- (6) clear + unsaved-work ----------------------------------------------
test("Clear resets everything; hasUnsavedWork reflects real content", () => {
  let m = P.emptyModel();
  assert.equal(P.hasUnsavedWork(m), false, "empty plan has nothing to lose");
  m = P.setBoundary(m, RING);
  assert.equal(P.hasUnsavedWork(m), true);
  m = P.clearModel();
  assert.equal(P.hasBoundary(m), false);
  assert.equal(m.noGoZones.length, 0);
  assert.equal(m.generated, null);
  assert.equal(P.hasUnsavedWork(m), false);
});

// ---- (7) draft round-trip --------------------------------------------------
test("a draft preserves geometry, params, route and revisions", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.setHome(m, [12.999, 56.698]);
  m = P.setTransit(m, [[12.9995, 56.6985]]);
  m = P.addNoGoZone(m, ZONE);
  m = { ...m, vehicleId: 2 };
  m = P.applyGenerated(m, fakeGenerated(m));
  const draft = P.toDraft(m, "Lake A");
  const back = P.fromDraft({ ...draft, vehicle_id: draft.vehicle_id });
  assert.deepEqual(back.boundary, m.boundary);
  assert.equal(back.noGoZones.length, 1);
  assert.equal(back.noGoZones[0].id, "ngz-1");
  assert.deepEqual(back.home, m.home);
  assert.deepEqual(back.transit, m.transit);
  assert.equal(back.params.lane_spacing_m, 25);
  assert.equal(back.vehicleId, 2);
  assert.equal(P.isOutdated(back), false, "restored route is current, not outdated");
  // A new zone on the restored model does not collide with the loaded one.
  const grown = P.addNoGoZone(back, ZONE);
  assert.equal(grown.noGoZones[1].id, "ngz-2");
});

// ---- (8) generated route → existing mission-contract upload params ---------
test("a generated route becomes mission-contract-v1 upload params (route only)", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m, 5));
  const params = P.uploadParamsFromModel(m);
  assert.ok(params, "params produced");
  assert.equal(params.contract_version, "mission-contract-v1");
  assert.equal(params.waypoints.length, 5);
  for (const w of params.waypoints) {
    assert.ok(!("seq" in w) && !("command" in w) && !("altitude" in w), "route-only waypoint");
  }
  assert.equal(P.uploadParamsFromModel(P.emptyModel()), null, "no route → no params");
});
