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
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { NAV } from "../operator/lib/ui.js";
import * as P from "../operator/lib/planning.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLAN_SRC = readFileSync(join(HERE, "..", "operator", "pages", "Plan.js"), "utf-8");

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
  m = P.setApproach(m, [[12.9995, 56.6985]]);
  m = P.setReturns(m, [[12.9996, 56.6986]]);
  m = P.addNoGoZone(m, ZONE);
  m = { ...m, vehicleId: 2 };
  m = P.applyGenerated(m, fakeGenerated(m));
  const draft = P.toDraft(m, "Lake A");
  const back = P.fromDraft({ ...draft, vehicle_id: draft.vehicle_id });
  assert.deepEqual(back.boundary, m.boundary);
  assert.equal(back.noGoZones.length, 1);
  assert.equal(back.noGoZones[0].id, "ngz-1");
  assert.deepEqual(back.home, m.home);
  assert.deepEqual(back.approach, m.approach);
  assert.deepEqual(back.returns, m.returns);
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

// ── Approach / Return waypoints (renamed from Transit) ─────────────────────────────────
test("approach and return are separate ordered lists that keep insertion order", () => {
  let m = P.emptyModel();
  m = P.setApproach(m, [[13.0, 56.699], [13.001, 56.6992], [13.002, 56.6994]]);
  m = P.setReturns(m, [[13.003, 56.700], [13.0035, 56.7002]]);
  assert.deepEqual(m.approach.map((p) => p[0]), [13.0, 13.001, 13.002], "approach A1→A2→A3 order");
  assert.deepEqual(m.returns.map((p) => p[0]), [13.003, 13.0035], "return R1→R2 order");
  const inp = P.planningInputs(m);
  assert.deepEqual(inp.approach_waypoints, m.approach);
  assert.deepEqual(inp.return_waypoints, m.returns);
});

test("'Use reversed approach' copies the approach list in reverse, editable and separate", () => {
  let m = P.setApproach(P.emptyModel(), [[13.0, 56.699], [13.001, 56.6992], [13.002, 56.6994]]);
  m = P.reversedApproach(m);
  assert.deepEqual(m.returns.map((p) => p[0]), [13.002, 13.001, 13.0], "returns are A3→A2→A1");
  assert.notEqual(m.returns, m.approach, "return list is a distinct array (editable)");
  // Editing the return list afterwards does not touch the approach list.
  const edited = P.setReturns(m, [...m.returns, [13.004, 56.6996]]);
  assert.equal(edited.approach.length, 3, "approach unchanged after editing returns");
});

test("approach/return edits invalidate a generated route (outdated)", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m));
  assert.equal(P.isOutdated(m), false);
  const afterApproach = P.setApproach(m, [[12.9995, 56.6985]]);
  assert.equal(P.isOutdated(afterApproach), true, "adding an approach WP outdates the route");
  const afterReturn = P.setReturns(m, [[12.9996, 56.6986]]);
  assert.equal(P.isOutdated(afterReturn), true, "adding a return WP outdates the route");
  const afterStart = P.setRouteStart(m, "first_approach");
  assert.equal(P.isOutdated(afterStart), true, "changing route-start mode outdates the route");
});

test("route-start mode defaults to planning_home and only accepts known modes", () => {
  const m = P.emptyModel();
  assert.equal(m.routeStartMode, "planning_home");
  assert.equal(P.setRouteStart(m, "first_approach").routeStartMode, "first_approach");
  assert.equal(P.setRouteStart(m, "bogus").routeStartMode, "planning_home", "unknown → default");
});

test("a draft with the OLD transit field still loads (migration to approach)", () => {
  const legacy = { vehicle_id: 2, plan: { boundary: RING, transit: [[12.9995, 56.6985]], params: { lane_spacing_m: 25 } } };
  const m = P.fromDraft(legacy);
  assert.deepEqual(m.approach, [[12.9995, 56.6985]], "old `transit` loads as approach");
  assert.deepEqual(m.returns, []);
});

// ── finalize payload + mission identity ────────────────────────────────────────────────
test("finalizePayload carries the full package + vehicle; null without a route/vehicle", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.applyGenerated(m, fakeGenerated(m));
  assert.equal(P.finalizePayload(m), null, "no vehicle → no payload");
  m = { ...m, vehicleId: 2 };
  const body = P.finalizePayload(m);
  assert.equal(body.vehicle_id, 2);
  assert.equal(body.confirm, true);
  assert.equal(body.mission_package, m.generated, "sends the whole generated package");
});

// ── static contract checks on the Plan page source (DOM-free) ──────────────────────────
test("Plan page controls use Approach/Return wording, not the old Transit", () => {
  assert.match(PLAN_SRC, /Add approach WP/, "Approach control present");
  assert.match(PLAN_SRC, /Add return WP/, "Return control present");
  assert.match(PLAN_SRC, /Use reversed approach/, "reversed-approach convenience present");
  assert.ok(!/Add transit WP/.test(PLAN_SRC), "old 'Add transit WP' control removed");
  assert.ok(!/setTransit/.test(PLAN_SRC), "no reference to the removed setTransit");
});

test("Plan page puts no-go zones in a dedicated pane above the navigable fill", () => {
  assert.match(PLAN_SRC, /pl-nogo/, "no-go pane assigned");
  assert.match(PLAN_SRC, /pl-navigable/, "navigable pane assigned");
  // The no-go pane z-index must be higher than the navigable pane's (red stays on top).
  const zOf = (name) => {
    const m = PLAN_SRC.match(new RegExp(`\\["${name}",\\s*(\\d+)\\]`));
    return m ? Number(m[1]) : null;
  };
  assert.ok(zOf("pl-nogo") > zOf("pl-navigable"), "no-go pane sits above navigable pane");
  assert.ok(zOf("pl-route") > zOf("pl-nogo"), "route lines sit above no-go zones");
});

test("Plan page defines a distinct style for every ordered segment kind", () => {
  for (const kind of ["start_connector", "approach", "survey_entry_connector", "primary",
                      "pass_transition", "secondary", "return_connector", "return_approach",
                      "final_home_connector"]) {
    assert.match(PLAN_SRC, new RegExp(`${kind}:`), `SEG_STYLE has ${kind}`);
  }
});
