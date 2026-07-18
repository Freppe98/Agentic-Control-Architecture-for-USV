// Unit tests for the mission-upload workflow helpers (operator/lib/mission-upload.js).
// Run: `node --test tests/` (or `npm test`).
//
// Pins: (1) parsing/validation of route-waypoint JSON and GeoJSON into a preview (route
// count, Pixhawk item count, first/last); (2) the upload-lifecycle mapping onto
// Requested → Executing → Verified/Failed, driven by Scout's live agent.mission_upload;
// (3) never labelling an upload verified merely because the file reached Scout;
// (4) expected-vs-observed read-back comparison on both counts and the route hash.
//
// Cross-contract behaviour (mission-contract-v1 ownership rules) lives in
// tests/mission-contract.test.mjs.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseMission, missionUploadParams, missionUploadStage, missionUploadCompare,
  liveUploadMatches, UPLOAD_STAGES, MISSION_CONTRACT_VERSION,
} from "../operator/lib/mission-upload.js";

// ---- parsing / validation / preview ----------------------------------------

test("parses a route waypoint array with counts + first/last", () => {
  const p = parseMission(JSON.stringify([
    { latitude: 56.70, longitude: 13.00 },
    { latitude: 56.71, longitude: 13.01 },
    { latitude: 56.72, longitude: 13.02 },
  ]));
  assert.equal(p.ok, true);
  assert.equal(p.format, "waypoints");
  assert.equal(p.routeCount, 3);
  assert.equal(p.pixhawkItemCount, 4);          // + Scout's Home at seq 0
  assert.deepEqual(p.first, { lat: 56.70, lng: 13.00 });
  assert.deepEqual(p.last, { lat: 56.72, lng: 13.02 });
});

test("parses a { contract_version, waypoints } request object", () => {
  const p = parseMission({
    contract_version: MISSION_CONTRACT_VERSION,
    waypoints: [{ latitude: 1, longitude: 2 }, { latitude: 3, longitude: 4 }],
  });
  assert.equal(p.ok, true);
  assert.equal(p.routeCount, 2);
  assert.equal(p.pixhawkItemCount, 3);
});

test("rejects a mission declaring a different contract_version", () => {
  const p = parseMission({ contract_version: "mission-contract-v2", waypoints: [{ latitude: 1, longitude: 2 }] });
  assert.equal(p.ok, false);
  assert.match(p.errors[0], /Unsupported contract_version/);
});

test("accepts lat/lng aliases and defaults loiter_time_s to 0", () => {
  const p = parseMission([{ lat: 56.7, lng: 13.0 }, { lat: 56.8, lon: 13.1 }]);
  assert.equal(p.ok, true);
  assert.deepEqual(p.waypoints[0], { latitude: 56.7, longitude: 13.0, loiter_time_s: 0 });
});

test("keeps an explicit loiter_time_s", () => {
  const p = parseMission([{ latitude: 56.7, longitude: 13.0, loiter_time_s: 30 }]);
  assert.equal(p.ok, true);
  assert.equal(p.waypoints[0].loiter_time_s, 30);
});

test("rejects a negative or non-numeric loiter_time_s", () => {
  assert.equal(parseMission([{ latitude: 1, longitude: 2, loiter_time_s: -5 }]).ok, false);
  assert.equal(parseMission([{ latitude: 1, longitude: 2, loiter_time_s: "30" }]).ok, false);
});

test("parses GeoJSON Point features (coordinates are [lng, lat])", () => {
  const p = parseMission(JSON.stringify({
    type: "FeatureCollection",
    features: [
      { type: "Feature", geometry: { type: "Point", coordinates: [13.00, 56.70] } },
      { type: "Feature", geometry: { type: "Point", coordinates: [13.01, 56.71] } },
    ],
  }));
  assert.equal(p.ok, true);
  assert.equal(p.format, "geojson");
  assert.equal(p.routeCount, 2);
  assert.equal(p.pixhawkItemCount, 3);
  assert.deepEqual(p.first, { lat: 56.70, lng: 13.00 });   // note: lat/lng un-swapped
  assert.deepEqual(p.waypoints[0], { latitude: 56.70, longitude: 13.00, loiter_time_s: 0 });
});

test("parses a GeoJSON LineString into ordered route waypoints", () => {
  const p = parseMission({ type: "LineString", coordinates: [[13, 56], [13.1, 56.1], [13.2, 56.2]] });
  assert.equal(p.ok, true);
  assert.equal(p.routeCount, 3);
  assert.equal(p.pixhawkItemCount, 4);
  assert.deepEqual(p.last, { lat: 56.2, lng: 13.2 });
});

test("rejects quoted (string) coordinates in both encodings — the backend refuses them too", () => {
  // A clean preview that then 400s on upload is worse than an honest rejection here.
  assert.equal(parseMission({ type: "LineString", coordinates: [["12.87", "56.65"], ["12.88", "56.66"]] }).ok, false);
  assert.equal(parseMission([{ latitude: "56.65", longitude: "12.87" }]).ok, false);
});

test("rejects invalid JSON", () => {
  const p = parseMission("{not json");
  assert.equal(p.ok, false);
  assert.match(p.errors[0], /Not valid JSON/);
});

test("rejects an empty mission", () => {
  assert.equal(parseMission("[]").ok, false);
  assert.equal(parseMission({ waypoints: [] }).ok, false);
});

test("rejects a waypoint with an out-of-range / missing latitude-longitude", () => {
  const p = parseMission([{ latitude: 999, longitude: 13 }, { latitude: 56, longitude: 13 }]);
  assert.equal(p.ok, false);
  assert.match(p.errors[0], /`latitude` must be a number/);
});

test("rejects an unrecognized format", () => {
  const p = parseMission({ something: "else" });
  assert.equal(p.ok, false);
  assert.match(p.errors[0], /Unrecognized mission format/);
});

test("one bad waypoint refuses the WHOLE mission (never a partial upload)", () => {
  const p = parseMission([{ latitude: 56, longitude: 13 }, { latitude: 999, longitude: 13 }]);
  assert.equal(p.ok, false);
  assert.equal(p.waypoints.length, 0);
  assert.equal(p.routeCount, 0);
});

// ---- request params --------------------------------------------------------

test("missionUploadParams sends contract_version + route waypoints only", () => {
  const p = parseMission([{ latitude: 1, longitude: 2 }, { latitude: 3, longitude: 4 }]);
  const params = missionUploadParams(p);
  assert.deepEqual(Object.keys(params).sort(), ["contract_version", "waypoints"]);
  assert.equal(params.contract_version, MISSION_CONTRACT_VERSION);
  assert.equal(params.waypoints.length, 2);
  // No counts and no hash are asserted client-side: the backend is the authority that
  // re-derives them from the waypoints it actually received.
  assert.equal(params.expected_route_waypoint_count, undefined);
  assert.equal(params.expected_route_content_hash, undefined);
});

// ---- upload lifecycle mapping ----------------------------------------------

test("three stages: Requested → Executing → Verified (no ACCEPTED stage)", () => {
  // Scout is NOT required to post an intermediate ACCEPTED command result — the operator
  // backend redelivers nonterminal commands, so such a post would be redelivered anyway.
  assert.deepEqual(UPLOAD_STAGES, ["Requested", "Executing", "Verified"]);
});

test("stage: QUEUED/SENT with no live state → Requested (pending)", () => {
  assert.equal(missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "QUEUED" }, null).stage, "Requested");
  assert.equal(missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "SENT" }, null).state, "pending");
});

test("stage: EXECUTED + mission_result verified → Verified (done)", () => {
  const s = missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "verified" }, null);
  assert.equal(s.stage, "Verified");
  assert.equal(s.state, "done");
});

test("stage: EXECUTED but read-back MISMATCH (mission_result failed) → Failed, not Verified", () => {
  const s = missionUploadStage({
    id: "c1", type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "failed",
    reason: "Pixhawk holds 4 route waypoints after upload — expected 5.",
  }, null);
  assert.equal(s.stage, "Failed");
  assert.equal(s.state, "failed");
  assert.match(s.reason, /expected 5/);
});

test("stage: transport-EXECUTED with no verification is NOT Verified (never green on transport alone)", () => {
  const s = missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "EXECUTED" }, null);
  assert.equal(s.stage, "Failed");
});

test("stage: REJECTED → Failed with the reason", () => {
  const s = missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "REJECTED", reason: "Vehicle busy" }, null);
  assert.equal(s.stage, "Failed");
  assert.equal(s.reason, "Vehicle busy");
});

test("stage: EXPIRED → Failed as a timeout (mission state unknown)", () => {
  const s = missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "EXPIRED" }, null);
  assert.equal(s.stage, "Failed");
  assert.match(s.reason, /timed out/i);
});

test("falls back to Scout's lifecycle array when agent.mission_upload is absent", () => {
  // Transitional Scout: reports a lifecycle but not the live worker group. That EXECUTING
  // is real progress the backend already holds — sitting at Requested would discard it.
  const cmd = { id: "c1", type: "MISSION_UPLOAD", status: "SENT", lifecycle: [{ stage: "SENT" }, { stage: "EXECUTING" }] };
  const s = missionUploadStage(cmd, null);
  assert.equal(s.stage, "Executing");
  assert.equal(s.elapsedS, null);   // no live block ⇒ no Scout-reported elapsed time
});

test("the live block wins over the lifecycle array when both are present", () => {
  const cmd = { id: "c1", type: "MISSION_UPLOAD", status: "SENT", lifecycle: [{ stage: "EXECUTING" }] };
  const s = missionUploadStage(cmd, { active: true, command_id: "c1", elapsed_s: 7 });
  assert.equal(s.stage, "Executing");
  assert.equal(s.elapsedS, 7, "elapsed_s comes from the live block");
});

test("a lifecycle array for ANOTHER command's stages does not fabricate Executing", () => {
  const cmd = { id: "c1", type: "MISSION_UPLOAD", status: "SENT", lifecycle: [{ stage: "QUEUED" }, { stage: "SENT" }] };
  assert.equal(missionUploadStage(cmd, null).stage, "Requested");
});

test("a terminal command ignores live upload state entirely", () => {
  // A stale 'active' worker report must never drag a finished command back to Executing.
  const live = { active: true, state: "UPLOADING", command_id: "c1", elapsed_s: 3 };
  const s = missionUploadStage({ id: "c1", type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "verified" }, live);
  assert.equal(s.stage, "Verified");
});

// ---- live upload state matching --------------------------------------------

test("liveUploadMatches only on an equal command_id", () => {
  assert.equal(liveUploadMatches({ command_id: "c1" }, "c1"), true);
  assert.equal(liveUploadMatches({ command_id: "c2" }, "c1"), false);
  assert.equal(liveUploadMatches({ command_id: null }, "c1"), false);
  assert.equal(liveUploadMatches(null, "c1"), false);
  assert.equal(liveUploadMatches({ command_id: "c1" }, null), false);
});

// ---- expected vs observed read-back comparison -----------------------------

test("compare: both counts match but no hash available → match FALSE, not a count-only pass", () => {
  // Under mission-contract-v1 the content axis is required. Two swapped waypoints have
  // exactly these counts, so rendering this as verified is the false assurance the hash
  // was introduced to remove.
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 3, expected_pixhawk_item_count: 4, expected_route_content_hash: null },
    { count: 4 }, 3);
  assert.equal(c.routeMatch, true);
  assert.equal(c.itemsMatch, true);
  assert.equal(c.hashMatch, null);
  assert.equal(c.hashUnavailable, true);
  assert.equal(c.match, false);
});

test("compare: prefers Scout's explicit read-back counts over the local derivation", () => {
  // Scout owns the Home/route split, so where it states the split explicitly that wins.
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 2, expected_pixhawk_item_count: 3, expected_route_content_hash: "sha256:r" },
    { route_waypoint_count: 2, pixhawk_item_count: 3, count: 99, route_content_hash: "sha256:r" },
    /* locally derived, deliberately wrong */ 7);
  assert.equal(c.observedRoute, 2);
  assert.equal(c.observedItems, 3);
  assert.equal(c.routeCountSource, "scout");
  assert.equal(c.match, true);
});

test("compare: falls back to the derived route count and legacy count when Scout omits them", () => {
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 2, expected_pixhawk_item_count: 3, expected_route_content_hash: "sha256:r" },
    { count: 3, route_content_hash: "sha256:r" }, 2);
  assert.equal(c.observedRoute, 2);
  assert.equal(c.observedItems, 3);
  assert.equal(c.routeCountSource, "derived");
  assert.equal(c.match, true);
});

test("compare: route count differs → match false", () => {
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 5, expected_pixhawk_item_count: 6 }, { count: 6 }, 4);
  assert.equal(c.routeMatch, false);
  assert.equal(c.match, false);
});

test("compare: Pixhawk item count differs (Home dropped) → match false", () => {
  // 3 route waypoints read back correctly, but only 3 items on the FC: Scout's Home is
  // missing. Checking the route count alone would have called this a success.
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 3, expected_pixhawk_item_count: 4 }, { count: 3 }, 3);
  assert.equal(c.routeMatch, true);
  assert.equal(c.itemsMatch, false);
  assert.equal(c.match, false);
});

test("compare: uses Scout's route_content_hash, never its full-mission hash", () => {
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 2, expected_pixhawk_item_count: 3, expected_route_content_hash: "sha256:route" },
    { count: 3, route_content_hash: "sha256:route", hash: "sha256:FULL-MISSION-INCLUDING-HOME" }, 2);
  assert.equal(c.observedHash, "sha256:route");
  assert.equal(c.hashMatch, true);
  assert.equal(c.match, true);
});

test("compare: a full-mission hash alone is NOT treated as the route hash", () => {
  // `hash` / `full_mission_hash` include Home — a different value over different bytes.
  // Substituting one would manufacture a content proof from a number never compared.
  for (const field of ["hash", "full_mission_hash"]) {
    const c = missionUploadCompare(
      { expected_route_waypoint_count: 2, expected_pixhawk_item_count: 3, expected_route_content_hash: "sha256:route" },
      { count: 3, [field]: "sha256:route" }, 2);
    assert.equal(c.observedHash, null, field);
    assert.equal(c.hashMatch, null, field);   // unverifiable, NOT a mismatch and NOT a pass
    assert.equal(c.match, false, field);      // and NOT a count-only success
  }
});

test("compare: route content hash differs → match false even when counts agree", () => {
  const c = missionUploadCompare(
    { expected_route_waypoint_count: 2, expected_pixhawk_item_count: 3, expected_route_content_hash: "sha256:aaa" },
    { count: 3, route_content_hash: "sha256:bbb" }, 2);
  assert.equal(c.routeMatch, true);
  assert.equal(c.itemsMatch, true);
  assert.equal(c.hashMatch, false);
  assert.equal(c.match, false);
});

test("compare: nothing comparable → match false (never a claimed match)", () => {
  const c = missionUploadCompare({}, {}, null);
  assert.equal(c.match, false);
  assert.equal(c.hashUnavailable, true);
});
