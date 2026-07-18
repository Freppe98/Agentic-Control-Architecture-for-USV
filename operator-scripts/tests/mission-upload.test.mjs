// Unit tests for the mission-upload workflow helpers (operator/lib/mission-upload.js).
// Run: `node --test tests/` (or `npm test`).
//
// Pins: (1) parsing/validation of canonical waypoint JSON and GeoJSON into a preview
// (count, first/last, deterministic hash); (2) the upload-lifecycle mapping onto
// Requested → Accepted → Executing → Verified/Failed, including mismatch, rejected and
// timeout; (3) never labelling an upload verified merely because the file reached Scout;
// (4) expected-vs-observed read-back comparison.
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  parseMission, missionHash, missionUploadParams,
  missionUploadStage, missionUploadCompare,
} from "../operator/lib/mission-upload.js";

// ---- parsing / validation / preview ----------------------------------------

test("parses a canonical waypoint array with count + first/last + hash", () => {
  const p = parseMission(JSON.stringify([
    { seq: 0, lat: 56.70, lng: 13.00, alt: 0 },
    { seq: 1, lat: 56.71, lng: 13.01, alt: 0 },
    { seq: 2, lat: 56.72, lng: 13.02, alt: 0 },
  ]));
  assert.equal(p.ok, true);
  assert.equal(p.format, "waypoints");
  assert.equal(p.count, 3);
  assert.deepEqual(p.first, { lat: 56.70, lng: 13.00 });
  assert.deepEqual(p.last, { lat: 56.72, lng: 13.02 });
  assert.match(p.hash, /^wpm1:[0-9a-f]{8}$/);
});

test("parses a { waypoints: [...] } wrapper object", () => {
  const p = parseMission({ waypoints: [{ lat: 1, lng: 2 }, { lat: 3, lng: 4 }] });
  assert.equal(p.ok, true);
  assert.equal(p.count, 2);
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
  assert.equal(p.count, 2);
  assert.deepEqual(p.first, { lat: 56.70, lng: 13.00 });   // note: lat/lng un-swapped
});

test("parses a GeoJSON LineString into ordered waypoints", () => {
  const p = parseMission({ type: "LineString", coordinates: [[13, 56], [13.1, 56.1], [13.2, 56.2]] });
  assert.equal(p.ok, true);
  assert.equal(p.count, 3);
  assert.deepEqual(p.last, { lat: 56.2, lng: 13.2 });
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

test("rejects a waypoint with an out-of-range / missing lat-lng", () => {
  const p = parseMission([{ lat: 999, lng: 13 }, { lat: 56, lng: 13 }]);
  assert.equal(p.ok, false);
  assert.match(p.errors[0], /invalid or missing lat\/lng/);
});

test("rejects an unrecognized format", () => {
  const p = parseMission({ something: "else" });
  assert.equal(p.ok, false);
  assert.match(p.errors[0], /Unrecognized mission format/);
});

// ---- hashing ---------------------------------------------------------------

test("missionHash is deterministic and content-sensitive", () => {
  const a = [{ seq: 0, lat: 56.7, lng: 13.0, alt: 0 }, { seq: 1, lat: 56.8, lng: 13.1, alt: 0 }];
  const b = [{ seq: 0, lat: 56.7, lng: 13.0, alt: 0 }, { seq: 1, lat: 56.8, lng: 13.2, alt: 0 }];
  assert.equal(missionHash(a), missionHash(a));   // stable
  assert.notEqual(missionHash(a), missionHash(b)); // one coord differs → different hash
});

test("missionUploadParams carries expected count + hash for read-back verification", () => {
  const p = parseMission([{ lat: 1, lng: 2 }, { lat: 3, lng: 4 }]);
  const params = missionUploadParams(p);
  assert.equal(params.expected_count, 2);
  assert.equal(params.expected_hash, p.hash);
  assert.equal(params.waypoints.length, 2);
});

// ---- upload lifecycle mapping ----------------------------------------------

test("stage: QUEUED/SENT → Requested (pending)", () => {
  assert.deepEqual(missionUploadStage({ type: "MISSION_UPLOAD", status: "QUEUED" }).stage, "Requested");
  assert.equal(missionUploadStage({ type: "MISSION_UPLOAD", status: "SENT" }).state, "pending");
});

test("stage: ACCEPTED → Accepted (pending)", () => {
  const s = missionUploadStage({ type: "MISSION_UPLOAD", status: "ACCEPTED" });
  assert.equal(s.stage, "Accepted");
  assert.equal(s.state, "pending");
});

test("stage: ACCEPTED with a Scout EXECUTING lifecycle stage → Executing", () => {
  const s = missionUploadStage({
    type: "MISSION_UPLOAD", status: "ACCEPTED",
    scout_lifecycle: [{ stage: "ACCEPTED" }, { stage: "EXECUTING" }],
  });
  assert.equal(s.stage, "Executing");
});

test("stage: EXECUTED + mission_result verified → Verified (done)", () => {
  const s = missionUploadStage({ type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "verified" });
  assert.equal(s.stage, "Verified");
  assert.equal(s.state, "done");
});

test("stage: EXECUTED but read-back MISMATCH (mission_result failed) → Failed, not Verified", () => {
  const s = missionUploadStage({
    type: "MISSION_UPLOAD", status: "EXECUTED", mission_result: "failed",
    reason: "Pixhawk holds 4 waypoints after upload — expected 5.",
  });
  assert.equal(s.stage, "Failed");
  assert.equal(s.state, "failed");
  assert.match(s.reason, /expected 5/);
});

test("stage: transport-EXECUTED with no verification is NOT Verified (never green on transport alone)", () => {
  // A non-conforming Scout that reports EXECUTED without accepted/verified must not read
  // as a verified upload.
  const s = missionUploadStage({ type: "MISSION_UPLOAD", status: "EXECUTED" });
  assert.equal(s.stage, "Failed");
});

test("stage: REJECTED → Failed with the reason", () => {
  const s = missionUploadStage({ type: "MISSION_UPLOAD", status: "REJECTED", reason: "Vehicle busy" });
  assert.equal(s.stage, "Failed");
  assert.equal(s.reason, "Vehicle busy");
});

test("stage: EXPIRED → Failed as a timeout (mission state unknown)", () => {
  const s = missionUploadStage({ type: "MISSION_UPLOAD", status: "EXPIRED" });
  assert.equal(s.stage, "Failed");
  assert.match(s.reason, /timed out/i);
});

// ---- expected vs observed read-back comparison -----------------------------

test("compare: count + hash both match → match true", () => {
  const c = missionUploadCompare(
    { expected_count: 3, expected_hash: "wpm1:abc" },
    { count: 3, hash: "wpm1:abc" });
  assert.equal(c.countMatch, true);
  assert.equal(c.hashMatch, true);
  assert.equal(c.match, true);
});

test("compare: count differs → match false", () => {
  const c = missionUploadCompare({ expected_count: 5, expected_hash: "wpm1:abc" }, { count: 4, hash: "wpm1:abc" });
  assert.equal(c.countMatch, false);
  assert.equal(c.match, false);
});

test("compare: Scout reports no hash → hashMatch null, falls back to count", () => {
  const c = missionUploadCompare({ expected_count: 3, expected_hash: "wpm1:abc" }, { count: 3 });
  assert.equal(c.hashMatch, null);
  assert.equal(c.countMatch, true);
  assert.equal(c.match, true);
});

test("compare: nothing comparable → match null (never a claimed match)", () => {
  const c = missionUploadCompare({}, {});
  assert.equal(c.match, null);
});
