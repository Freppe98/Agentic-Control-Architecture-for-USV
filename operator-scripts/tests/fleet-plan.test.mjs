// Tests for lib/fleet-plan.js — the pure fleet-mission state model + upload orchestration.
// Run: node --test tests/fleet-plan.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import * as F from "../operator/lib/fleet-plan.js";
import { defaultParams } from "../operator/lib/planning.js";

const BOX = [[13.0, 56.699], [13.004, 56.699], [13.004, 56.7005], [13.0, 56.7005], [13.0, 56.699]];
function geom(over = {}) {
  return { boundary: BOX, noGoZones: [], params: { ...defaultParams(), lane_spacing_m: 25 }, ...over };
}
function twoVehicleFleet() {
  let f = F.emptyFleet();
  f = F.toggleVehicle(f, "usv-2");
  f = F.toggleVehicle(f, "usv-3");
  f = F.setVehicleHome(f, "usv-2", [12.999, 56.6996]);
  f = F.setVehicleHome(f, "usv-3", [13.005, 56.6996]);
  return f;
}

test("new fleet vehicles default to 1.0 m/s and mark it as default", () => {
  let f = F.toggleVehicle(F.emptyFleet(), "usv-2");
  const c = F.vehicleConfig(f, "usv-2");
  assert.equal(c.survey_speed_mps, F.DEFAULT_FLEET_SPEED_MPS);
  assert.equal(c.survey_speed_mps, 1.0);
  assert.equal(c.speedIsDefault, true);
});

test("a user-entered speed is preserved and no longer marked default", () => {
  let f = F.toggleVehicle(F.emptyFleet(), "usv-2");
  f = F.setVehicleSpeed(f, "usv-2", 2.0);
  assert.equal(F.vehicleConfig(f, "usv-2").survey_speed_mps, 2.0);
  assert.equal(F.vehicleConfig(f, "usv-2").speedIsDefault, false);
});

test("vehicle count is derived from selection; toggling adds/removes", () => {
  let f = F.emptyFleet();
  assert.equal(F.selectedCount(f), 0);
  f = F.toggleVehicle(f, "usv-2");
  f = F.toggleVehicle(f, "usv-3");
  assert.equal(F.selectedCount(f), 2);
  f = F.toggleVehicle(f, "usv-2"); // remove
  assert.equal(F.selectedCount(f), 1);
  assert.equal(F.vehicleConfig(f, "usv-2"), null);
});

test("distinct default colours per vehicle", () => {
  const f = twoVehicleFleet();
  assert.notEqual(F.vehicleConfig(f, "usv-2").colour, F.vehicleConfig(f, "usv-3").colour);
});

test("operator home is not overwritten by a reported home", () => {
  let f = F.toggleVehicle(F.emptyFleet(), "usv-2");
  f = F.setVehicleHome(f, "usv-2", [1, 2]);
  f = F.useReportedHome(f, "usv-2", [9, 9]); // should be ignored (operator picked one)
  assert.deepEqual(F.vehicleConfig(f, "usv-2").home, [1, 2]);
});

test("canGenerateFleet requires two vehicles, homes, boundary and spacing", () => {
  assert.equal(F.canGenerateFleet(F.emptyFleet(), geom()), false);
  let f = F.toggleVehicle(F.emptyFleet(), "usv-2");
  f = F.setVehicleHome(f, "usv-2", [12.999, 56.6996]);
  assert.equal(F.canGenerateFleet(f, geom()), false); // only one vehicle
  const two = twoVehicleFleet();
  assert.equal(F.canGenerateFleet(two, geom()), true);
  assert.equal(F.canGenerateFleet(two, geom({ params: { ...defaultParams(), lane_spacing_m: null } })), false);
});

test("fleetPlanningBody carries selection, homes, speeds and settings", () => {
  const body = F.fleetPlanningBody(twoVehicleFleet(), geom());
  assert.equal(body.vehicles.length, 2);
  assert.equal(body.minimum_fleet_separation_m, F.DEFAULT_FLEET_SEPARATION_M);
  assert.equal(body.lane_spacing_m, 25);
  assert.deepEqual(body.vehicles.map((v) => v.vehicle_id).sort(), ["usv-2", "usv-3"]);
});

test("changing a home marks a generated plan outdated", () => {
  let f = twoVehicleFleet();
  const g = geom();
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }, { vehicle_id: "usv-3" }], validation: { ok: true } });
  assert.equal(F.isFleetOutdated(f, g), false);
  f = F.setVehicleHome(f, "usv-2", [12.5, 56.5]);
  assert.equal(F.isFleetOutdated(f, g), true);
});

test("changing shared geometry marks a generated plan outdated", () => {
  let f = twoVehicleFleet();
  const g1 = geom();
  f = F.applyFleetGenerated(f, g1, { vehicles: [{ vehicle_id: "usv-2" }], validation: { ok: true } });
  const g2 = geom({ params: { ...defaultParams(), lane_spacing_m: 40 } });
  assert.equal(F.isFleetOutdated(f, g2), true);
});

// ── upload orchestration ────────────────────────────────────────────────────────────────
test("beginUpload sets every vehicle PENDING; nextPendingVehicle walks them", () => {
  let f = twoVehicleFleet();
  f = F.beginUpload(f);
  assert.equal(f.upload.fleetStatus, F.FLEET_UPLOAD_STATES.UPLOADING);
  assert.equal(F.nextPendingVehicle(f), "usv-2");
  f = F.markVehicle(f, "usv-2", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  assert.equal(F.nextPendingVehicle(f), "usv-3");
});

test("a generated-but-not-uploaded fleet is NOT_STARTED, not a phantom UPLOADING", () => {
  let f = twoVehicleFleet();
  const g = geom();
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }, { vehicle_id: "usv-3" }], validation: { ok: true } });
  assert.equal(F.deriveFleetStatus(f), F.FLEET_UPLOAD_STATES.NOT_STARTED);
});

test("one success + one failure derives PARTIALLY_UPLOADED", () => {
  let f = F.beginUpload(twoVehicleFleet());
  f = F.markVehicle(f, "usv-2", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  f = F.markVehicle(f, "usv-3", F.VEHICLE_UPLOAD_STATES.FAILED, { error: "unavailable" });
  assert.equal(f.upload.fleetStatus, F.FLEET_UPLOAD_STATES.PARTIALLY_UPLOADED);
});

test("retryFailed re-pends only failed vehicles, never verified ones", () => {
  let f = F.beginUpload(twoVehicleFleet());
  f = F.markVehicle(f, "usv-2", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  f = F.markVehicle(f, "usv-3", F.VEHICLE_UPLOAD_STATES.FAILED);
  f = F.retryFailed(f);
  assert.equal(f.upload.vehicles["usv-2"].status, F.VEHICLE_UPLOAD_STATES.VERIFIED);
  assert.equal(f.upload.vehicles["usv-3"].status, F.VEHICLE_UPLOAD_STATES.PENDING);
  assert.equal(F.nextPendingVehicle(f), "usv-3"); // only the failed one is retried
});

test("all verified derives VERIFIED and fleetReady", () => {
  let f = F.beginUpload(twoVehicleFleet());
  const g = geom();
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }, { vehicle_id: "usv-3" }], validation: { ok: true }, fleet_plan_id: "fleet-x" });
  f = F.beginUpload(f);
  f = F.markVehicle(f, "usv-2", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  f = F.markVehicle(f, "usv-3", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  assert.equal(f.upload.fleetStatus, F.FLEET_UPLOAD_STATES.VERIFIED);
  assert.equal(F.fleetReady(f, g), true);
});

test("uploaded ≠ verified: an UPLOADED-only fleet is not ready", () => {
  let f = twoVehicleFleet();
  const g = geom();
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }, { vehicle_id: "usv-3" }], validation: { ok: true } });
  f = F.beginUpload(f);
  f = F.markVehicle(f, "usv-2", F.VEHICLE_UPLOAD_STATES.UPLOADED);
  f = F.markVehicle(f, "usv-3", F.VEHICLE_UPLOAD_STATES.UPLOADED);
  assert.equal(F.fleetReady(f, g), false);
});

test("regenerating after a verified upload marks prior missions STALE", () => {
  let f = twoVehicleFleet();
  const g = geom();
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }, { vehicle_id: "usv-3" }], validation: { ok: true } });
  f = F.beginUpload(f);
  f = F.markVehicle(f, "usv-2", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  f = F.markVehicle(f, "usv-3", F.VEHICLE_UPLOAD_STATES.VERIFIED);
  // regenerate (a new plan) → prior verified missions become STALE, fleet not ready
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }, { vehicle_id: "usv-3" }], validation: { ok: true } });
  assert.equal(f.upload.vehicles["usv-2"].status, F.VEHICLE_UPLOAD_STATES.STALE);
  assert.equal(F.fleetReady(f, g), false);
});

test("canUploadFleet requires a current, validated plan", () => {
  let f = twoVehicleFleet();
  const g = geom();
  assert.equal(F.canUploadFleet(f, g), false); // no plan
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }], validation: { ok: false } });
  assert.equal(F.canUploadFleet(f, g), false); // invalid
  f = F.applyFleetGenerated(f, g, { vehicles: [{ vehicle_id: "usv-2" }], validation: { ok: true } });
  assert.equal(F.canUploadFleet(f, g), true);
});

test("finalizePayloadForVehicle reuses the single-vehicle finalize shape", () => {
  const f = twoVehicleFleet();
  const vp = { vehicle_id: "usv-2", mission_package: { route_waypoints: [] } };
  const p = F.finalizePayloadForVehicle(f, vp);
  assert.equal(p.vehicle_id, "usv-2");
  assert.equal(p.confirm, true);
  assert.equal(p.upload_context, "OPERATOR_REPLACEMENT");
  assert.ok(p.mission_package);
});
