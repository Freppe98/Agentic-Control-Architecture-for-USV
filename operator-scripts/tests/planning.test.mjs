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
  // Lane spacing now carries a working 10 m default, so a boundary alone is already
  // generatable — CONFIGURED, not BOUNDARY_DEFINED.
  assert.equal(P.planState(m), P.PLAN_STATES.CONFIGURED);
});

test("BOUNDARY_DEFINED is what a CLEARED lane spacing looks like", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  assert.equal(P.canGenerate(m), true, "the default lane spacing is immediately usable");
  m = P.setParam(m, "lane_spacing_m", null);      // operator cleared the field
  assert.equal(P.canGenerate(m), false);
  assert.equal(P.planState(m), P.PLAN_STATES.BOUNDARY_DEFINED);
  m = P.setParam(m, "lane_spacing_m", 25);
  assert.equal(P.canGenerate(m), true);
  assert.equal(P.planState(m), P.PLAN_STATES.CONFIGURED);
});

// ---- planning-parameter defaults (a FRESH plan) ----------------------------
test("a fresh plan starts at 5 m shoreline clearance, 5 m no-go clearance, 10 m lane spacing", () => {
  const p = P.defaultParams();
  assert.equal(p.shoreline_clearance_m, 5);
  assert.equal(p.no_go_clearance_m, 5);
  assert.equal(p.lane_spacing_m, 10);
  // The empty model the page renders from carries the same values — the UI shows them at once.
  const m = P.emptyModel();
  assert.equal(m.params.shoreline_clearance_m, 5);
  assert.equal(m.params.no_go_clearance_m, 5);
  assert.equal(m.params.lane_spacing_m, 10);
});

test("no_go_clearance_m reaches the backend request body, including an explicit 0", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  assert.equal(P.planningInputs(m).no_go_clearance_m, 5, "the default is sent, not omitted");
  m = P.setParam(m, "no_go_clearance_m", 12);
  assert.equal(P.planningInputs(m).no_go_clearance_m, 12);
  m = P.setParam(m, "no_go_clearance_m", 0);
  assert.equal(P.planningInputs(m).no_go_clearance_m, 0, "explicit 0 is sent as 0, not defaulted");
});

test("changing the no-go clearance outdates a generated route", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  m = P.addNoGoZone(m, ZONE);
  m = P.applyGenerated(m, fakeGenerated(m));
  assert.equal(P.isOutdated(m), false);
  const before = P.inputRevision(m);
  m = P.setParam(m, "no_go_clearance_m", 12);
  assert.notEqual(P.inputRevision(m), before, "no-go clearance is generation-affecting");
  assert.equal(P.isOutdated(m), true);
  assert.equal(P.planState(m), P.PLAN_STATES.ROUTE_OUTDATED);
  assert.equal(m.validation, null, "the previous validation no longer applies");
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

test("a draft round-trips all three clearance/spacing parameters", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  m = P.setParam(m, "shoreline_clearance_m", 7);
  m = P.setParam(m, "no_go_clearance_m", 12);
  m = P.setParam(m, "lane_spacing_m", 18);
  const back = P.fromDraft(P.toDraft(m, "Lake B"));
  assert.equal(back.params.shoreline_clearance_m, 7);
  assert.equal(back.params.no_go_clearance_m, 12);
  assert.equal(back.params.lane_spacing_m, 18);
  // An explicit ZERO clearance is a real operator choice and must survive as 0, not become 5.
  const zeroed = P.fromDraft(P.toDraft(P.setParam(m, "no_go_clearance_m", 0)));
  assert.equal(zeroed.params.no_go_clearance_m, 0);
});

test("an OLD draft without no_go_clearance_m loads with the 5 m default and does not crash", () => {
  const legacy = { vehicle_id: 2, plan: { boundary: RING, no_go_zones: [{ id: "ngz-1", ring: ZONE }],
                                          params: { shoreline_clearance_m: 5, lane_spacing_m: 25,
                                                    primary_angle_deg: 0, dual_pass: false } } };
  const m = P.fromDraft(legacy);
  assert.equal(m.params.no_go_clearance_m, 5, "missing no-go clearance takes the new default");
  assert.equal(m.params.lane_spacing_m, 25, "a stored lane spacing is not overwritten");
  assert.equal(P.planningInputs(m).no_go_clearance_m, 5);
  assert.equal(P.planState(m), P.PLAN_STATES.CONFIGURED);
});

test("an old draft that stored a NULL lane spacing loads with the 10 m default", () => {
  const legacy = { plan: { boundary: RING, params: { lane_spacing_m: null, shoreline_clearance_m: 5 } } };
  const m = P.fromDraft(legacy);
  assert.equal(m.params.lane_spacing_m, 10);
  assert.equal(m.params.no_go_clearance_m, 5);
  // secondary_angle_deg is the one parameter whose stored null is meaningful (→ primary + 90).
  const withSec = P.fromDraft({ plan: { boundary: RING, params: { secondary_angle_deg: null } } });
  assert.equal(withSec.params.secondary_angle_deg, null);
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

test("Plan page exposes No-go clearance in the Planning Parameters panel", () => {
  assert.match(PLAN_SRC, /"No-go clearance"/, "the parameter is labelled in the panel");
  assert.match(PLAN_SRC, /Minimum routing clearance from operator-defined no-go zones\./,
               "the existing help affordance carries the agreed tooltip text");
  assert.match(PLAN_SRC, /wireNum\("pp-ngclear", "no_go_clearance_m"\)/,
               "editing it goes through setParam, so it invalidates like other geometry params");
  // Ordered directly after Shoreline clearance and before Lane spacing, in BOTH panels.
  const order = [...PLAN_SRC.matchAll(/"(Shoreline clearance|No-go clearance|Lane spacing)"/g)]
    .map((mm) => mm[1]);
  assert.deepEqual(order.slice(0, 3), ["Shoreline clearance", "No-go clearance", "Lane spacing"]);
  assert.deepEqual(order.slice(3, 6), ["Shoreline clearance", "No-go clearance", "Lane spacing"],
                   "the fleet Shared survey pattern panel uses the same order");
});

test("the original no-go polygon keeps its red style; the buffered exclusion is a separate outline", () => {
  // The red fill+outline is unchanged and still owns the pl-nogo pane.
  assert.match(PLAN_SRC, /const NOGO_STYLE = \{ color: "#E5484D".*fill: true.*pane: "pl-nogo" \}/);
  // The derived exclusion is unfilled, dashed, and NOT in the no-go pane — it can never
  // visually replace the operator-drawn zone.
  const excl = PLAN_SRC.match(/const NOGO_EXCLUSION_STYLE = \{[^}]*\}/);
  assert.ok(excl, "a distinct style exists for the buffered exclusion");
  assert.match(excl[0], /fill: false/);
  assert.match(excl[0], /dashArray/);
  assert.ok(!/pane: "pl-nogo"/.test(excl[0]), "the exclusion does not sit in the no-go pane");
  assert.match(PLAN_SRC, /no_go_exclusion_rings/, "drawn only from a real generation result");
});

test("Plan page defines a distinct style for every ordered segment kind", () => {
  for (const kind of ["start_connector", "approach", "survey_entry_connector", "primary",
                      "pass_transition", "secondary", "return_connector", "return_approach",
                      "final_home_connector"]) {
    assert.match(PLAN_SRC, new RegExp(`${kind}:`), `SEG_STYLE has ${kind}`);
  }
});

// ---- the mission-geometry contract on the page side -------------------------------------
// The geometry itself is proven in the backend (planning.check_mission_geometry, pinned by
// tests/test_mission_geometry.py). What the page owes the contract is narrower and is what
// these pin: a draft must round-trip every input the proof depends on, the finalize payload
// must carry the whole proven package unaltered, and the approved corridor must be drawn from
// the generation result rather than approximated.

test("a draft round-trips every input the geometry proof depends on", () => {
  // approach, return, no-go clearance, shoreline clearance, lane spacing — in one round trip,
  // because a draft that loses any one of them reloads as a DIFFERENT mission whose stored
  // route was proven against geometry the page no longer holds.
  let m = P.setBoundary(P.emptyModel(), RING);
  m = P.setParam(m, "shoreline_clearance_m", 6);
  m = P.setParam(m, "no_go_clearance_m", 8);
  m = P.setParam(m, "lane_spacing_m", 14);
  m = P.setHome(m, [12.999, 56.698]);
  m = P.setApproach(m, [[12.9995, 56.6985], [13.0000, 56.6988]]);
  m = P.setReturns(m, [[13.0010, 56.6988], [12.9996, 56.6986]]);
  m = P.addNoGoZone(m, ZONE);
  m = { ...m, vehicleId: 2 };
  m = P.applyGenerated(m, fakeGenerated(m));

  const back = P.fromDraft(P.toDraft(m, "Lake C"));
  assert.equal(back.params.shoreline_clearance_m, 6);
  assert.equal(back.params.no_go_clearance_m, 8);
  assert.equal(back.params.lane_spacing_m, 14);
  assert.deepEqual(back.approach, m.approach);
  assert.deepEqual(back.returns, m.returns);
  assert.deepEqual(back.home, m.home);
  assert.deepEqual(back.noGoZones[0].ring, m.noGoZones[0].ring, "the DRAWN zone, not a buffer");
  // The restored route is still current: nothing the proof depends on changed in transit.
  assert.equal(P.isOutdated(back), false);
  assert.equal(P.inputRevision(back), P.inputRevision(m));
});

test("the finalize payload ships the whole generated package, geometry included", () => {
  // The backend re-proves the package it is handed. Sending a thinned or re-assembled body
  // would either fail that proof or, worse, pass a different one.
  let m = P.setBoundary(P.emptyModel(), RING);
  m = { ...m, vehicleId: 2 };
  const generated = { ...fakeGenerated(m), home_corridor: [[13.0, 56.698], [13.001, 56.698], [13.001, 56.6985]],
                      home_corridor_meta: { available: true, half_width_m: 6 } };
  m = P.applyGenerated(m, generated);
  const payload = P.finalizePayload(m);
  assert.equal(payload.mission_package, generated, "the package is passed by reference, unedited");
  assert.deepEqual(payload.mission_package.home_corridor, generated.home_corridor);
  assert.equal(payload.confirm, true);
});

test("Plan page draws the approved Home corridor subtly, only from a real generation", () => {
  const style = PLAN_SRC.match(/const HOME_CORRIDOR_STYLE = \{[^}]*\}/);
  assert.ok(style, "a distinct style exists for the approved Home corridor");
  assert.match(style[0], /dashArray/, "dashed, so it reads as approved transit, not survey area");
  assert.match(style[0], /fillOpacity: 0\.0\d/, "translucent enough not to dominate");
  assert.ok(!/pane: "pl-nogo"/.test(style[0]),
            "the corridor never sits in the no-go pane — red stays the operator's own geometry");
  // Drawn from the generation result's own field; the page never derives or approximates one.
  assert.match(PLAN_SRC, /model\.generated && model\.generated\.home_corridor/);
  assert.match(PLAN_SRC, /Approved Home corridor/, "labelled for the operator");
});

// ── route-start mode: WHERE EXECUTION BEGINS, not what geometry is approved ─────────────
// The backend owns the geometry (tests/test_route_start_mode.py). These pin the page's side of
// the same distinction: the mode round-trips, changing it invalidates a generated route, the
// help text says what stays true of Home, and the page sends/draws the approved-but-not-executed
// geometry the backend returns instead of quietly dropping it.

test("a draft round-trips planning home, route-start mode, approach and return", () => {
  let m = P.setBoundary(P.emptyModel(), RING);
  m = P.setHome(m, [13.002, 56.6985]);
  m = P.setApproach(m, [[13.0015, 56.699], [13.002, 56.6992]]);
  m = P.setReturns(m, [[13.003, 56.6995], [13.0035, 56.6988]]);
  m = P.setRouteStart(m, "first_approach");
  const back = P.fromDraft(P.toDraft(m, "First-approach plan"));
  assert.deepEqual(back.home, [13.002, 56.6985], "planning home survives");
  assert.equal(back.routeStartMode, "first_approach", "the route-start mode survives");
  assert.deepEqual(back.approach, m.approach, "approach list survives in order");
  assert.deepEqual(back.returns, m.returns, "return list survives in order, separately");
  assert.notDeepEqual(back.returns, [...back.approach].reverse(),
                      "the return list is never re-derived from the approach on load");
  // The revision is over the mode too, so a reloaded draft is not spuriously outdated.
  assert.equal(P.inputRevision(back), P.inputRevision(m));
});

test("an old draft without route_start_mode loads as planning_home", () => {
  const legacy = { plan: { boundary: RING, home: [13.002, 56.6985], approach: [[13.0015, 56.699]] } };
  assert.equal(P.fromDraft(legacy).routeStartMode, "planning_home");
});

test("switching route-start mode invalidates the generated route and the validation", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = P.setHome(m, [13.002, 56.6985]);
  m = P.setApproach(m, [[13.0015, 56.699]]);
  m = P.applyGenerated(m, fakeGenerated(m));
  m = P.applyValidation(m, { ok: true, errors: [], warnings: [], checks: {} });
  assert.equal(P.isOutdated(m), false);
  assert.equal(P.planState(m), P.PLAN_STATES.VALID);
  const switched = P.setRouteStart(m, "first_approach");
  assert.equal(P.isOutdated(switched), true, "the route no longer matches its inputs");
  assert.equal(switched.validation, null, "a validation of the previous route is not evidence");
  assert.equal(P.planState(switched), P.PLAN_STATES.ROUTE_OUTDATED);
  assert.equal(P.canUpload(switched), false, "an outdated route may not be uploaded");
});

test("the route-start help says Home keeps its safety meaning under first approach", () => {
  const help = P.ROUTE_START_HELP;
  assert.match(help.planning_home, /begins from Planning Home/i);
  assert.match(help.first_approach, /begins at the first approach waypoint/i);
  // The old wording left "first approach" open to reading as "Home no longer applies", which is
  // the opposite of the semantics — Home stays the return reference and the corridor anchor.
  assert.match(help.first_approach, /Planning Home remains/i);
  assert.match(help.first_approach, /Home-corridor anchor/i);
  assert.deepEqual(Object.keys(help).sort(), [...P.ROUTE_START_MODES].sort(),
                   "every offered mode has help text, and no mode is described that is not offered");
  assert.match(PLAN_SRC, /P\.ROUTE_START_HELP\[model\.routeStartMode\]/,
               "the page renders the help for the SELECTED mode");
});

test("validation is sent the approved transit geometry, not only the execution segments", () => {
  // The backend re-derives the Home corridor from the approved transit geometry. Sending only
  // `segments` would make it derive from a subset and refuse a corridor generation proved.
  assert.match(PLAN_SRC, /planning_only_transit_segments: model\.generated\.planning_only_transit_segments \|\| \[\]/);
  assert.match(PLAN_SRC, /segments: model\.generated\.segments/);
});

test("the page draws approved planning-only transit distinctly from the flown route", () => {
  const style = PLAN_SRC.match(/const PLANNING_ONLY_SEG_STYLE = \{[^}]*\}/);
  assert.ok(style, "planning-only transit has its own style");
  assert.match(style[0], /dashArray/, "dashed, so it never reads as a leg the vehicle will fly");
  // Drawn from the generation result's own field — the page never derives or approximates it.
  assert.match(PLAN_SRC, /model\.generated\.planning_only_transit_segments \|\| \[\]/);
  assert.match(PLAN_SRC, /not part of the uploaded route/,
               "labelled as approved geometry that is not executed");
  // No waypoint dots and no arrows: it has no execution sequence, because it is not uploaded.
  assert.ok(!/ARROW_KINDS\.has\("home_transit_connector"\)/.test(PLAN_SRC));
});

test("finalize still sends the whole generated package, planning-only geometry included", () => {
  let m = P.setParam(P.setBoundary(P.emptyModel(), RING), "lane_spacing_m", 25);
  m = { ...m, vehicleId: 2 };
  const generated = { ...fakeGenerated(m),
    planning_only_transit_segments: [{ segment_id: "pln-01-home_transit_connector",
                                       kind: "home_transit_connector", planning_only: true,
                                       coordinates: [[13.0, 56.698], [13.0, 56.699]] }] };
  m = P.applyGenerated(m, generated);
  const payload = P.finalizePayload(m);
  assert.equal(payload.mission_package, generated, "passed by reference, unedited");
  assert.equal(payload.mission_package.planning_only_transit_segments.length, 1,
               "the record can only make the distinction auditable if the field reaches it");
});
