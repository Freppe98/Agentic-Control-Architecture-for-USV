// E2 EXPERIMENT EVIDENCE — the map model, the reference geometry and the preflight checklist.
//
// The experiment these tests protect: the vehicle flies an approved mission around ONE no-go
// polygon; at the far side its battery is driven critically low; Scout replans a constrained
// safe return with its RETRACE_APPROVED strategy and uploads it; the vehicle returns around the
// SAME polygon. The recording has to make it indisputable that the return did NOT cut straight
// through the obstacle.
//
// That evidence is fragile in one specific way: once Scout uploads the revision, the flight
// controller carries ONLY the return route. The outbound route and the obstacle are gone from the
// vehicle. So the map's reference geometry has to come from a source the replan cannot touch —
// the operator's own immutable original mission record — and it must never be inferred from
// anything else. These tests pin exactly that, plus the four-layer separation and the wording rule
// that keeps a constrained safe return from being called native RTL.
//
// Pure logic only (lib/replan.js) plus source guards for the Map/Agent wiring that has no DOM
// harness here. Run: `node --test tests/` (or `npm test`).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  originalPlanningGeometry, replanMapModel, normalizeReplanStatus, missionRevisionSignal,
  safeReturnBanner, e2PreflightChecks, CHECK, E2_EXPECTED_NO_GO_ZONES, SAFE_RETURN_PHASE_TEXT,
} from "../operator/lib/replan.js";
import { createMissionRefreshTracker } from "../operator/lib/mission-refresh.js";
import { normalizeStatus } from "../operator/lib/mission-execution.js";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");
const mapSrc = read("../operator/pages/Map.js");
const agentSrc = read("../operator/pages/Agent.js");

// The E2 shape: a small survey route with ONE no-go polygon sitting in the middle of it. Planning
// inputs are stored [lng, lat] (the planner's own GeoJSON-order rings); route waypoints are
// {latitude, longitude} objects. Both orders appear here deliberately.
const NO_GO_RING = [[12.8110, 56.6790], [12.8113, 56.6790], [12.8113, 56.6792], [12.8110, 56.6792]];
const ORIGINAL_RECORD = {
  mission_id: "msn-e2-0001",
  mission_revision: 0,
  route_hash: "sha256:original",
  planning_inputs: {
    boundary: [[12.8105, 56.6788], [12.8118, 56.6788], [12.8118, 56.6795], [12.8105, 56.6795]],
    navigable_boundary: [[[12.8106, 56.6789], [12.8117, 56.6789], [12.8117, 56.6794], [12.8106, 56.6789]]],
    no_go_zones: [NO_GO_RING],
    shoreline_clearance_m: 1,
    planning_home: [12.8108, 56.6791],
  },
  route_waypoints: [
    { latitude: 56.67910, longitude: 12.81080 },
    { latitude: 56.67885, longitude: 12.81100 },
    { latitude: 56.67885, longitude: 12.81150 },
    { latitude: 56.67940, longitude: 12.81150 },
  ],
  metrics: { no_go_zone_count: 1, waypoint_count: 4 },
};

const ACTIVE_ORIGINAL = {
  route_content_hash: "sha256:original",
  waypoints: ORIGINAL_RECORD.route_waypoints.map((w, i) => ({ seq: i, lat: w.latitude, lng: w.longitude })),
};
const ACTIVE_REVISED = {
  route_content_hash: "sha256:revised",
  waypoints: [
    { seq: 0, lat: 56.67940, lng: 12.81150 },
    { seq: 1, lat: 56.67950, lng: 12.81100 },   // around the north side of the zone
    { seq: 2, lat: 56.67910, lng: 12.81080 },
  ],
};

// ════════════════════════════════════════════════════════════════════════════════════════
// A. The original mission record's no-go polygon reaches the map model
// ════════════════════════════════════════════════════════════════════════════════════════
test("A. one no-go zone on the original record is available to the map model, as [lat,lng]", () => {
  const m = replanMapModel(normalizeReplanStatus(null),
    { original: ORIGINAL_RECORD, active: ACTIVE_ORIGINAL });
  const p = m.planning;
  assert.equal(p.present, true);
  assert.equal(p.noGoZoneCount, 1);
  assert.equal(p.noGoReported, true);
  assert.equal(p.noGoZones.length, 1);
  // Leaflet order: [lat, lng]. The record stores [lng, lat], so a silent pass-through here would
  // put the obstacle in the Indian Ocean.
  assert.deepEqual(p.noGoZones[0][0], [56.6790, 12.8110]);
  assert.equal(p.noGoZones[0].length, 4);
  // …and the rest of the reference geometry, in the same order.
  assert.deepEqual(p.route[0], [56.67910, 12.81080]);
  assert.equal(p.routeCount, 4);
  assert.equal(p.navigableBoundary.length, 1);
  assert.deepEqual(p.home, [56.6791, 12.8108]);
  assert.equal(p.shorelineClearanceM, 1);
  assert.equal(p.missionId, "msn-e2-0001");
  assert.equal(p.source, "ACTIVE_ORIGINAL_MISSION_RECORD");
});

test("A2. the endpoint envelope { ok, vehicle_id, mission } is accepted as well as the record", () => {
  const viaEnvelope = originalPlanningGeometry({ ok: true, vehicle_id: 2, mission: ORIGINAL_RECORD });
  assert.deepEqual(viaEnvelope, originalPlanningGeometry(ORIGINAL_RECORD));
});

test("A3. tolerant polygon spellings resolve; a degenerate ring is dropped, not drawn", () => {
  const wrapped = originalPlanningGeometry({ planning_inputs: { no_go_zones: [{ polygon: NO_GO_RING }] } });
  assert.equal(wrapped.noGoZones.length, 1);
  const geojson = originalPlanningGeometry({
    planning_inputs: { no_go_zones: [{ type: "Polygon", coordinates: [NO_GO_RING] }] } });
  assert.equal(geojson.noGoZones.length, 1);
  // Two points is a line, not a polygon. It is dropped from the drawable set — but the record
  // still REPORTED a zone, so the count keeps it (a zone we cannot draw must not vanish silently).
  const degenerate = originalPlanningGeometry({
    planning_inputs: { no_go_zones: [[[12.81, 56.67], [12.82, 56.67]]] } });
  assert.equal(degenerate.noGoZones.length, 0);
  assert.equal(degenerate.noGoZoneCount, 1);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// B. Before the replan — the original and the active route coexist
// ════════════════════════════════════════════════════════════════════════════════════════
test("B. before a replan the original reference and the live active route both exist", () => {
  const m = replanMapModel(normalizeReplanStatus({ scout: { fsm_state: "MONITORING" } }),
    { original: ORIGINAL_RECORD, active: ACTIVE_ORIGINAL });
  assert.deepEqual(m.layers.map((l) => l.kind), ["original", "active"]);
  assert.equal(m.revisedAvailable, false);
  assert.equal(m.contradiction, false);
  assert.equal(m.authoritativeActiveHash, "sha256:original");
  // The reference geometry is present the whole time — it is not something that appears only
  // once a replan happens.
  assert.equal(m.planning.noGoZones.length, 1);
  assert.equal(m.planning.route.length, 4);
  // Original and active are DISTINCT layers; the reference is never drawn as the active route.
  const original = m.layers.find((l) => l.kind === "original");
  const active = m.layers.find((l) => l.kind === "active");
  assert.equal(original.emphasis, "subdued");
  assert.equal(active.emphasis, "clear");
});

// ════════════════════════════════════════════════════════════════════════════════════════
// C. After active_route_hash changes — the revised route is fetched, the reference survives
// ════════════════════════════════════════════════════════════════════════════════════════
test("C1. a changed active_route_hash makes the refresh tracker fetch the revised route", () => {
  const tracker = createMissionRefreshTracker();
  const VID = 2;

  const before = missionRevisionSignal({
    missionExecution: { scout: { state: "RUNNING", active_route_hash: "sha256:original" } } });
  // First read of the original route.
  assert.equal(tracker.shouldFetch(VID, { reason: "revision", revisionSignal: before }).fetch, true);
  tracker.noteFetched(VID, { route_content_hash: "sha256:original", count: 4, current_seq: 1 });
  tracker.noteRevisionSignal(VID, before);
  // Same evidence next poll → no download. An overlay refresh must not become a poll loop.
  assert.deepEqual(tracker.shouldFetch(VID, { reason: "revision", revisionSignal: before }),
    { fetch: false, why: "revision-unchanged" });

  // Scout replans, uploads and verifies. The active route hash moves.
  const after = missionRevisionSignal({
    replan: { scout: { fsm_state: "MONITORING_REVISED", revision: 1,
                       revised_mission_hash: "sha256:revised", readback_result: "VERIFIED" } },
    missionExecution: { scout: { state: "RETURNING_HOME", active_route_hash: "sha256:revised" } } });
  assert.notEqual(after, before);
  assert.deepEqual(tracker.shouldFetch(VID, { reason: "revision", revisionSignal: after }),
    { fetch: true, why: "revision-changed" });

  // The download lands: the GEOMETRY changed, which is what rebuilds the active overlay.
  const noted = tracker.noteFetched(VID, { route_content_hash: "sha256:revised", count: 3, current_seq: 0 });
  assert.equal(noted.geometryChanged, true);
});

test("C2. after the revised route is installed, the original route and no-go zone remain", () => {
  const revised = normalizeReplanStatus({ scout: {
    fsm_state: "MONITORING_REVISED", revision: 1, strategy: "RETRACE_APPROVED",
    original_mission_hash: "sha256:original", revised_mission_hash: "sha256:revised",
    readback_result: "VERIFIED" } });
  const m = replanMapModel(revised, { original: ORIGINAL_RECORD, active: ACTIVE_REVISED });

  // The ACTIVE layer is now the revised return route…
  const active = m.layers.find((l) => l.kind === "active");
  assert.equal(active.hash, "sha256:revised");
  assert.equal(active.waypoints.length, 3);
  assert.equal(m.authoritativeActiveHash, "sha256:revised");
  assert.equal(m.contradiction, false);

  // …and the ORIGINAL route is still a layer, and the no-go polygon is still available. This is
  // the whole experiment: the examiner must be able to see both at once.
  const original = m.layers.find((l) => l.kind === "original");
  assert.ok(original, "the original route must survive the replan as a reference layer");
  assert.equal(original.hash, "sha256:original");
  assert.equal(m.planning.noGoZones.length, 1);
  assert.deepEqual(m.planning.noGoZones[0][0], [56.6790, 12.8110]);
  assert.equal(m.planning.route.length, 4);
});

test("C3. the map wires the reference layer so a mission replacement cannot remove it", () => {
  // The active-mission overlay is torn down and rebuilt on every geometry change. The reference
  // layer has its OWN group and its own teardown — if clearMissionOverlay() ever took it with it,
  // the obstacle would vanish from the map at the exact moment it matters most.
  const clearMission = mapSrc.slice(mapSrc.indexOf("function clearMissionOverlay()"),
    mapSrc.indexOf("function clearMissionOverlay()") + 260);
  assert.equal(/originalLayer/.test(clearMission), false,
    "clearMissionOverlay must not touch the reference layer");
  assert.match(mapSrc, /function clearOriginalOverlay\(\)/);
  assert.match(mapSrc, /function drawOriginalOverlay\(id\)/);
  // Drawn from the record's model, never from the Pixhawk read-back cache.
  const draw = mapSrc.slice(mapSrc.indexOf("function drawOriginalOverlay(id)"),
    mapSrc.indexOf("function drawOriginalOverlay(id)") + 1200);
  assert.match(draw, /originalModel\(id\)/);
  assert.equal(/pxm\[/.test(draw), false, "the reference layer must not read the mission cache");
  // Reference panes sit BELOW Leaflet's default overlay pane (400) so the live route stays on top.
  const panes = mapSrc.match(/const REF_PANES = \[([^\]]*\])[^;]*/)[0];
  for (const z of panes.match(/\d{3}/g)) assert.ok(Number(z) < 400, `pane z ${z} must be below 400`);
});

test("C4. the map's revision trigger is wired to the mission-execution status poll", () => {
  // The auto-refresh path, end to end: status poll → revision evidence → controller → download.
  assert.match(mapSrc, /noteRevisionEvidence\(forId\);/);
  assert.match(mapSrc, /refreshController\.refreshMission\(id, "revision", \{ revisionSignal: sig \}\)/);
  assert.match(mapSrc, /missionExecution: mission\.forVid === id \? mission\.status : null/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// D. No-go geometry is NEVER inferred from the route
// ════════════════════════════════════════════════════════════════════════════════════════
test("D. a record with a route but no no-go field yields NO zones and no fabricated count", () => {
  const noZones = { ...ORIGINAL_RECORD, planning_inputs: {
    ...ORIGINAL_RECORD.planning_inputs, no_go_zones: undefined }, metrics: {} };
  delete noZones.planning_inputs.no_go_zones;
  const g = originalPlanningGeometry(noZones);
  assert.equal(g.present, true);
  assert.equal(g.route.length, 4, "the route is still available");
  assert.deepEqual(g.noGoZones, [], "a route shape is not an obstacle");
  assert.equal(g.noGoZoneCount, null, "an absent field is not a count of zero");
  assert.equal(g.noGoReported, false);
});

test("D2. an EXPLICIT empty no-go list is an answer, distinct from an absent field", () => {
  const empty = originalPlanningGeometry({ ...ORIGINAL_RECORD,
    planning_inputs: { ...ORIGINAL_RECORD.planning_inputs, no_go_zones: [] }, metrics: {} });
  assert.deepEqual(empty.noGoZones, []);
  assert.equal(empty.noGoZoneCount, 0, "the planner looked and recorded none");
  assert.equal(empty.noGoReported, true);
});

test("D3. the map page derives no-go geometry only from the planning model", () => {
  // No local ring-building, no route-derived polygon, no convex hull anywhere on the page.
  assert.equal(/no_go_zones/.test(mapSrc), false,
    "the Map must read no-go zones through originalPlanningGeometry, not by hand");
  assert.match(mapSrc, /originalPlanningGeometry/);
  const draw = mapSrc.slice(mapSrc.indexOf("function drawOriginalOverlay(id)"),
    mapSrc.indexOf("function drawOriginalOverlay(id)") + 1200);
  assert.match(draw, /g\.noGoZones\.forEach/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// E. Missing original planning geometry degrades gracefully
// ════════════════════════════════════════════════════════════════════════════════════════
test("E. no original record: the active mission still displays and nothing is fabricated", () => {
  for (const missing of [null, undefined, {}, { ok: true, mission: null }, "nonsense", 42]) {
    const g = originalPlanningGeometry(missing);
    assert.deepEqual(g.noGoZones, [], String(missing));
    assert.equal(g.noGoZoneCount, null, String(missing));
    assert.equal(g.noGoReported, false, String(missing));
    assert.deepEqual(g.route, [], String(missing));
  }
  const m = replanMapModel(normalizeReplanStatus(null), { original: null, active: ACTIVE_REVISED });
  // The active mission is unaffected — a missing reference never withholds the live route.
  const active = m.layers.find((l) => l.kind === "active");
  assert.ok(active);
  assert.equal(active.hash, "sha256:revised");
  assert.equal(m.planning.present, false);
  assert.equal(m.layers.some((l) => l.kind === "original"), false);
});

test("E2. a record whose planning inputs are junk does not throw and draws nothing", () => {
  const junk = originalPlanningGeometry({ mission_id: "msn-x", planning_inputs: {
    no_go_zones: [null, 7, {}, [], [["a", "b"], ["c", "d"], ["e", "f"]]],
    navigable_boundary: "not-a-ring", planning_home: [NaN, 12] },
    route_waypoints: [{ latitude: null, longitude: 12 }, "x", { lat: 56, lng: 12 }] });
  assert.deepEqual(junk.noGoZones, []);
  assert.deepEqual(junk.navigableBoundary, []);
  assert.equal(junk.home, null);
  assert.deepEqual(junk.route, [[56, 12]], "only the one usable waypoint survives");
});

// ════════════════════════════════════════════════════════════════════════════════════════
// F. Risk / Advice / Action Request / Replanning FSM stay independent
// ════════════════════════════════════════════════════════════════════════════════════════
test("F1. the four layers are read from four separate Scout fields", () => {
  const mxS = normalizeStatus({ scout: { state: "RUNNING",
    risk: { level: "CRITICAL", recommendation: "RETURN_HOME" } } });
  const R = normalizeReplanStatus({ scout: { fsm_state: "HOLD_REQUESTED",
    action_request: "REQUEST_RETURN_HOME" } });

  assert.equal(mxS.risk.level, "CRITICAL");
  assert.equal(mxS.risk.recommendation, "RETURN_HOME");
  assert.equal(R.actionRequest.reported, true);
  assert.equal(R.actionRequest.code, "REQUEST_RETURN_HOME");
  assert.equal(R.transaction.fsmState, "HOLD_REQUESTED");
});

test("F2. an FSM in HOLD_REQUESTED does NOT rewrite the mission-level Advice as HOLD", () => {
  // LOITER/HOLD_REQUESTED is an EXECUTION STEP of the safe return. The advice is still Scout's
  // own mission-level recommendation, and nothing derives one from the other.
  const mxS = normalizeStatus({ scout: { state: "RUNNING",
    risk: { level: "CRITICAL", recommendation: "RETURN_HOME" } } });
  const R = normalizeReplanStatus({ scout: { fsm_state: "HOLD_REQUESTED",
    action_request: "REQUEST_RETURN_HOME" } });
  const checks = e2PreflightChecks({ missionExecution: mxS, replanStatus: R }).checks;
  const by = Object.fromEntries(checks.map((c) => [c.key, c]));
  assert.equal(by.advice.value, "RETURN_HOME");
  assert.notEqual(by.advice.value, "HOLD");
  assert.equal(by.replan_fsm.value, "HOLD_REQUESTED");
  assert.equal(by.risk.value, "CRITICAL");
  assert.equal(by.action_request.value, "REQUEST_RETURN_HOME");
});

test("F3. a Scout that emits no action_request is reported as silent, never as NONE", () => {
  const R = normalizeReplanStatus({ scout: { fsm_state: "MONITORING" } });
  assert.equal(R.actionRequest.reported, false);
  assert.equal(R.actionRequest.code, null);
  const by = Object.fromEntries(e2PreflightChecks({ replanStatus: R }).checks.map((c) => [c.key, c]));
  assert.equal(by.action_request.state, CHECK.UNKNOWN);
  assert.match(by.action_request.value, /not reported/);
  // A CRITICAL risk does not manufacture a request either.
  const critical = normalizeStatus({ scout: { state: "RUNNING", risk: { level: "CRITICAL" } } });
  const by2 = Object.fromEntries(
    e2PreflightChecks({ missionExecution: critical, replanStatus: R }).checks.map((c) => [c.key, c]));
  assert.equal(by2.action_request.state, CHECK.UNKNOWN);
});

test("F4. the Agent page renders the four layers as four rows from four sources", () => {
  const layer = agentSrc.slice(agentSrc.indexOf("function layerCard(v, S)"),
    agentSrc.indexOf("function layerCard(v, S)") + 2400);
  assert.match(layer, /risk\.level/);
  assert.match(layer, /risk\.recommendation/);
  assert.match(layer, /S\.actionRequest/);
  assert.match(layer, /S\.transaction \? S\.transaction\.fsmState/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// G. The constrained safe return is never called native RTL
// ════════════════════════════════════════════════════════════════════════════════════════
test("G1. every safe-return phase reads as a safe return; only FALLBACK_RTL says RTL", () => {
  for (const [state, text] of Object.entries(SAFE_RETURN_PHASE_TEXT)) {
    if (state === "FALLBACK_RTL") {
      // The one legitimate use: the autopilot's own return, which is NOT the constrained route.
      assert.match(text, /RTL/);
      assert.match(text, /NOT constrained/);
    } else {
      assert.equal(/RTL/.test(text), false, `${state} must not be called RTL: "${text}"`);
    }
  }
  assert.equal(safeReturnBanner(normalizeReplanStatus({ scout: { fsm_state: "VALIDATING" } })).text,
    "Safe return — validating");
  assert.equal(safeReturnBanner(normalizeReplanStatus({ scout: { fsm_state: "MONITORING_REVISED" } })).text,
    "Replanned safe return active");
  // MONITORING is the resting state: the map says nothing rather than inventing a status line.
  assert.equal(safeReturnBanner(normalizeReplanStatus({ scout: { fsm_state: "MONITORING" } })), null);
  assert.equal(safeReturnBanner(normalizeReplanStatus(null)), null);
});

test("G2. the Agent page's revision card names a safe return, not an RTL", () => {
  const cardSrc = agentSrc.slice(agentSrc.indexOf("function missionRevisionCard(S)"),
    agentSrc.indexOf("function execConfigCard("));
  assert.match(cardSrc, /Safe return mission revision/);
  // Every RTL mention in that card must be the explicit disclaimer that this is NOT native RTL.
  const rtlLines = cardSrc.split("\n").filter((l) => /RTL/.test(l));
  assert.ok(rtlLines.length > 0, "the distinction must be stated, not merely avoided");
  for (const line of rtlLines) {
    assert.match(line, /not<\/b> native Pixhawk RTL|FALLBACK_RTL|rtl_fallback_enabled/,
      `RTL used without the native-fallback distinction: ${line.trim()}`);
  }
});

test("G3. the map's replanning line never uses RTL for the constrained route", () => {
  const banner = mapSrc.slice(mapSrc.indexOf("function renderReplanBanner()"),
    mapSrc.indexOf("function renderReplanBanner()") + 900);
  assert.equal(/"RTL"|'RTL'/.test(banner), false);
  assert.match(banner, /safeReturnBanner/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// The E2 preflight checklist itself
// ════════════════════════════════════════════════════════════════════════════════════════
const READY_INPUTS = {
  missionExecution: normalizeStatus({ scout: { state: "READY",
    energy_feasibility: { mission_feasible:true }, risk: { level: "LOW", recommendation: "CONTINUE" } } }),
  replanStatus: normalizeReplanStatus({ scout: { fsm_state: "MONITORING", action_request: "NONE" } }),
  readiness: { vehicle_mission: { mission_id: "msn-e2-0001", pixhawk_verified: true,
                                  readback_reachable: true, readback_hash_match: true, home_valid: true },
               planning_package: { no_go_zone_count: 1 } },
  packageVerdict: { state: "READY", text: "Agent package synchronized" },
  planning: originalPlanningGeometry(ORIGINAL_RECORD),
  scoutNoGoZoneCount: 1,
  homeVerified: true,
};

test("a correctly configured E2 passes every check", () => {
  const r = e2PreflightChecks(READY_INPUTS);
  const failing = r.checks.filter((c) => c.state !== CHECK.PASS).map((c) => `${c.key}=${c.state}:${c.value}`);
  assert.deepEqual(failing, []);
  assert.equal(r.ready, true);
  assert.equal(E2_EXPECTED_NO_GO_ZONES, 1);
});

test("zones PRESENT but a count of ZERO is caught — presence alone is not the test", () => {
  const emptyPlan = originalPlanningGeometry({ ...ORIGINAL_RECORD,
    planning_inputs: { ...ORIGINAL_RECORD.planning_inputs, no_go_zones: [] }, metrics: {} });
  assert.equal(emptyPlan.noGoReported, true, "the record DID report on no-go zones");
  const r = e2PreflightChecks({ ...READY_INPUTS, planning: emptyPlan, scoutNoGoZoneCount: 0 });
  const by = Object.fromEntries(r.checks.map((c) => [c.key, c]));
  assert.equal(by.no_go_zones.state, CHECK.FAIL);
  assert.equal(by.no_go_zones.value, "0");
  assert.equal(by.no_go_zones_package.state, CHECK.FAIL);
  assert.equal(r.ready, false);
});

test("two no-go zones is also not the planned E2", () => {
  const two = originalPlanningGeometry({ ...ORIGINAL_RECORD, planning_inputs: {
    ...ORIGINAL_RECORD.planning_inputs, no_go_zones: [NO_GO_RING, NO_GO_RING] } });
  const by = Object.fromEntries(e2PreflightChecks({ ...READY_INPUTS, planning: two, scoutNoGoZoneCount: 2 })
    .checks.map((c) => [c.key, c]));
  assert.equal(by.no_go_zones.state, CHECK.FAIL);
  assert.equal(by.no_go_zones.value, "2");
});

test("a Scout package that disagrees with the approved record's zone count is flagged", () => {
  const by = Object.fromEntries(
    e2PreflightChecks({ ...READY_INPUTS, scoutNoGoZoneCount: 0 }).checks.map((c) => [c.key, c]));
  assert.equal(by.no_go_zones.state, CHECK.PASS, "the approved record is still correct");
  assert.equal(by.no_go_zones_package.state, CHECK.FAIL, "Scout is planning against something else");
});

test("an ABSENT plan stays absent — a derived model is never re-normalized into existence", () => {
  // Regression: the preflight used to tell a record from an already-derived model by its
  // `source` field. An absent-record model has a null source but is still a valid model, so
  // re-normalizing it produced present:true for a plan that does not exist — and the checklist
  // then reported "no no-go field on the record" about a vehicle with no record at all.
  const absent = originalPlanningGeometry(null);
  assert.equal(absent.present, false);
  const by = Object.fromEntries(
    e2PreflightChecks({ ...READY_INPUTS, planning: absent }).checks.map((c) => [c.key, c]));
  assert.equal(by.no_go_zones.state, CHECK.UNKNOWN);
  assert.equal(by.no_go_zones.value, "no original mission record");
  // A RECORD is still accepted directly, and both routes agree.
  const viaRecord = e2PreflightChecks({ ...READY_INPUTS, planning: ORIGINAL_RECORD });
  const viaModel = e2PreflightChecks({ ...READY_INPUTS, planning: originalPlanningGeometry(ORIGINAL_RECORD) });
  assert.deepEqual(viaRecord.checks, viaModel.checks);
});

test("an unanswered check is UNKNOWN and never counted as a pass", () => {
  const r = e2PreflightChecks({});
  assert.equal(r.ready, false);
  assert.equal(r.failed.length, 0, "nothing has FAILED — nothing has been answered");
  assert.ok(r.unknown.length >= 10);
  for (const c of r.checks) assert.notEqual(c.state, CHECK.PASS, c.key);
});

test("the E2 trigger state is legible: CRITICAL / RETURN_HOME / REQUEST_RETURN_HOME / FSM", () => {
  const triggered = e2PreflightChecks({
    ...READY_INPUTS,
    missionExecution: normalizeStatus({ scout: { state: "RUNNING",
      energy_feasibility: { mission_feasible:false }, risk: { level: "CRITICAL", recommendation: "RETURN_HOME" } } }),
    replanStatus: normalizeReplanStatus({ scout: { fsm_state: "PLANNING",
      action_request: "REQUEST_RETURN_HOME", strategy: "RETRACE_APPROVED" } }),
  });
  const by = Object.fromEntries(triggered.checks.map((c) => [c.key, c]));
  assert.equal(by.risk.value, "CRITICAL");
  assert.equal(by.advice.value, "RETURN_HOME");
  assert.equal(by.action_request.value, "REQUEST_RETURN_HOME");
  assert.equal(by.replan_fsm.value, "PLANNING");
  // The no-go constraint is untouched by the trigger — the return is still planned against it.
  assert.equal(by.no_go_zones.state, CHECK.PASS);
});

test("the preflight reads a read-back it could not obtain as UNKNOWN, not as a failure", () => {
  const by = Object.fromEntries(e2PreflightChecks({ ...READY_INPUTS,
    readiness: { vehicle_mission: { mission_id: "msn-e2-0001", readback_reachable: false },
                 planning_package: { no_go_zone_count: 1 } } }).checks.map((c) => [c.key, c]));
  assert.equal(by.route_identity.state, CHECK.UNKNOWN);
  assert.match(by.route_identity.value, /unreachable/);
});

test("the Agent page renders the E2 preflight, sourced from the approved record", () => {
  assert.match(agentSrc, /function e2PreflightCard\(v, S, rd\)/);
  assert.match(agentSrc, /replan\.e2PreflightChecks/);
  assert.match(agentSrc, /replan\.originalPlanningGeometry/);
  assert.match(agentSrc, /api\.getActiveOriginalMission\(id\)/);
  // Scout's own package count, carried through by the backend and compared — not re-derived.
  assert.match(agentSrc, /rd\.planning_package\s*\n?\s*\?\s*rd\.planning_package\.no_go_zone_count/);
});

test("the Agent page follows the SHARED vehicle selection", () => {
  // Reading the Map's evidence for one USV and this page's E2 evidence for another is a mistake
  // that costs a whole experiment run.
  assert.match(agentSrc, /getSelectedVehicleId, setSelectedVehicleId/);
  assert.match(agentSrc, /setSelectedVehicleId\(id\);/);
  assert.match(agentSrc, /const shared = getSelectedVehicleId\(\);/);
});

test("the Map legend names every E2 layer exactly once", () => {
  const legend = mapSrc.slice(mapSrc.indexOf('id="legend"'), mapSrc.indexOf('id="legend-body"') + 2200);
  for (const entry of ["Original approved mission (reference)", "No-go zone (original plan)",
                       "Active mission on the vehicle", "Vehicle Home (RTL point)"]) {
    assert.equal(legend.split(entry).length - 1, 1, `legend entry "${entry}"`);
  }
});
