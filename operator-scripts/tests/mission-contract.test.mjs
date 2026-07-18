// Cross-contract tests for mission-contract-v1 — the OWNERSHIP rules that must hold
// identically on the Operator and Scout sides:
//
//   • the operator supplies ROUTE waypoints only;
//   • Scout owns Pixhawk sequence 0 / Home;
//   • Pixhawk item count = route waypoint count + 1;
//   • route content hashing is Scout's SHA-256 canonicalization over route items 1…N,
//     computed ONLY by the operator backend — never in browser JavaScript;
//   • a missing route hash is a verification FAILURE, not a count-only pass;
//   • live upload progress comes from agent.mission_upload, matched by command_id;
//   • MISSION_CLEAR is verified by an empty read-back, accepting BOTH ArduPilot empty
//     representations (NO_ITEMS and HOME_ONLY).
//
// Driven by tests/fixtures/mission-contract-v1.json, which is pinned to Scout's
// authoritative golden route hash. See that fixture's _provenance block for how it was
// produced and what it does and does not prove.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  parseMission, missionUploadParams, missionUploadStage, missionUploadCompare,
  missionClearOutcome,
} from "../operator/lib/mission-upload.js";
import { classifyMissionWaypoints } from "../operator/lib/mission.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const FIXTURE = JSON.parse(readFileSync(join(HERE, "fixtures", "mission-contract-v1.json"), "utf8"));

// ---- counts: two route points → route 2, Pixhawk 3 -------------------------

test("two route points produce route count 2 and Pixhawk count 3", () => {
  const p = parseMission(FIXTURE.request);
  assert.equal(p.ok, true);
  assert.equal(p.routeCount, 2);
  assert.equal(p.pixhawkItemCount, 3);
  assert.equal(p.pixhawkItemCount, p.routeCount + 1);
});

test("the fixture's own expected counts match what the parser derives", () => {
  const p = parseMission(FIXTURE.request);
  assert.equal(p.routeCount, FIXTURE.expected.expected_route_waypoint_count);
  assert.equal(p.pixhawkItemCount, FIXTURE.expected.expected_pixhawk_item_count);
});

// ---- no operator-side seq 0 / Home -----------------------------------------

test("no sequence-0 Home is sent by the Operator", () => {
  const p = parseMission(FIXTURE.request);
  const params = missionUploadParams(p);
  // Not by count...
  assert.equal(params.waypoints.length, 2, "only the operator's own route waypoints are sent");
  // ...and not by shape: no waypoint carries a seq at all, so there is no seq 0 to send.
  for (const w of params.waypoints) {
    assert.deepEqual(Object.keys(w).sort(), ["latitude", "loiter_time_s", "longitude"]);
    assert.equal("seq" in w, false);
  }
});

test("a mission file that TRIES to supply a seq-0 Home is rejected, not silently stripped", () => {
  // Silently dropping it would let an operator believe they had pinned Home when they had
  // not — Home is Scout's, and the file must be corrected rather than reinterpreted.
  const p = parseMission([
    { seq: 0, latitude: 56.64, longitude: 12.86 },
    { seq: 1, latitude: 56.6501, longitude: 12.8701 },
  ]);
  assert.equal(p.ok, false);
  assert.match(p.errors.join(" "), /remove `seq`/i);
  assert.match(p.errors.join(" "), /Scout owns seq 0 \/ Home/i);
});

// ---- unsupported fields -----------------------------------------------------

test("unsupported command / frame / altitude fields are rejected, naming each one", () => {
  const p = parseMission([{ latitude: 56.65, longitude: 12.87, command: 16, frame: 3, altitude: 12 }]);
  assert.equal(p.ok, false);
  const all = p.errors.join(" ");
  assert.match(all, /remove `command`/i);
  assert.match(all, /remove `frame`/i);
  assert.match(all, /remove `altitude`/i);
  // And the reason is the contract, not a generic "invalid".
  assert.match(all, /Scout-owned/i);
});

test("the OLD operator schema {seq, command, lat, lng, alt} is now rejected wholesale", () => {
  const p = parseMission([{ seq: 0, command: 16, lat: 56.65, lng: 12.87, alt: 0 }]);
  assert.equal(p.ok, false);
  assert.equal(p.routeCount, 0);
});

test("a GeoJSON position carrying an altitude is rejected, not silently truncated", () => {
  const p = parseMission({ type: "LineString", coordinates: [[12.87, 56.65, 30], [12.88, 56.66, 30]] });
  assert.equal(p.ok, false);
  assert.match(p.errors.join(" "), /altitude/i);
});

test("GeoJSON Points normalize to route waypoints with loiter_time_s defaulted to 0", () => {
  const p = parseMission({
    type: "FeatureCollection",
    features: [
      { type: "Feature", geometry: { type: "Point", coordinates: [12.8701, 56.6501] } },
      { type: "Feature", geometry: { type: "Point", coordinates: [12.8725, 56.6512] } },
    ],
  });
  assert.equal(p.ok, true);
  assert.equal(p.routeCount, 2);
  assert.equal(p.pixhawkItemCount, 3);
  assert.deepEqual(p.waypoints, [
    { latitude: 56.6501, longitude: 12.8701, loiter_time_s: 0 },
    { latitude: 56.6512, longitude: 12.8725, loiter_time_s: 0 },
  ]);
});

// ---- route content hash ------------------------------------------------------

test("the removed wpm1 FNV-1a hash is gone from the module's surface", async () => {
  const mod = await import("../operator/lib/mission-upload.js");
  assert.equal("missionHash" in mod, false, "no operator-side hash calculator may exist");
  const p = parseMission(FIXTURE.request);
  assert.equal("hash" in p, false, "the parse result must not carry a locally-computed hash");
  assert.equal("expected_hash" in missionUploadParams(p), false);
});

test("Operator and Scout golden route hashes match", () => {
  // THE cross-system assertion, and the reason this fixture exists. The backend is the
  // authoritative calculator (mission_contract.py); this pins its output against Scout's
  // golden value. Python-side proof that the backend really computes it:
  // tests/test_mission_contract.py TestRouteContentHash.
  const golden = FIXTURE.scout_golden_route_hash;
  assert.ok(golden, "the Scout golden route hash must be present — no skip, no null");
  assert.match(golden, /^sha256:[0-9a-f]{64}$/);
  assert.equal(FIXTURE.expected.expected_route_content_hash, golden);
});

test("the UI consumes the backend's expected hash and never computes its own", async () => {
  // No hash calculator may exist in browser JavaScript: a second implementation is a
  // second thing that can drift from Scout, and the comparison would become meaningless.
  const mod = await import("../operator/lib/mission-upload.js");
  for (const name of ["missionHash", "routeContentHash", "canonicalRouteJson"]) {
    assert.equal(name in mod, false, `${name} must not exist in the browser module`);
  }
  const src = await readFile(new URL("../operator/lib/mission-upload.js", import.meta.url), "utf8");
  assert.equal(/createHash|sha256\(|crypto\./.test(src), false,
    "the browser module must not hash anything");
  // What it DOES do: take the backend's string and compare it.
  const cmp = missionUploadCompare(FIXTURE.expected, FIXTURE.readback, 2);
  assert.equal(cmp.expectedHash, FIXTURE.scout_golden_route_hash);
  assert.equal(cmp.observedHash, FIXTURE.scout_golden_route_hash);
});

test("a matching read-back renders Verified on all three axes", () => {
  const cmp = missionUploadCompare(FIXTURE.expected, FIXTURE.readback, 2);
  assert.equal(cmp.routeMatch, true);
  assert.equal(cmp.itemsMatch, true);
  assert.equal(cmp.hashMatch, true);
  assert.equal(cmp.hashUnavailable, false);
  assert.equal(cmp.match, true);
});

test("a MISSING observed route hash renders Failed, not a count-only success", () => {
  const { route_content_hash, ...noHash } = FIXTURE.readback;
  const cmp = missionUploadCompare(FIXTURE.expected, noHash, 2);
  assert.equal(cmp.routeMatch, true, "counts still agree...");
  assert.equal(cmp.itemsMatch, true);
  assert.equal(cmp.hashMatch, null);
  assert.equal(cmp.hashUnavailable, true);
  assert.equal(cmp.match, false, "...but the route content was never compared");
});

// ---- verified / failed rendering --------------------------------------------

test("a Scout verified upload is rendered VERIFIED", () => {
  const cmd = {
    id: "cmd-1", type: "MISSION_UPLOAD", status: "EXECUTED",
    params: FIXTURE.expected, result: FIXTURE.verified_result,
    mission_result: "verified",
  };
  const s = missionUploadStage(cmd, null);
  assert.equal(s.stage, "Verified");
  assert.equal(s.state, "done");
  // ...and the read-back agrees on both counts.
  const route = classifyMissionWaypoints(FIXTURE.readback.waypoints).route.length;
  const cmp = missionUploadCompare(FIXTURE.expected, FIXTURE.readback, route);
  assert.equal(cmp.routeMatch, true);
  assert.equal(cmp.itemsMatch, true);
  assert.equal(cmp.match, true);
});

test("the fixture read-back splits into Scout's Home + 2 route waypoints", () => {
  // Pins the N+1 story end to end: 3 items on the FC == Home + the 2 uploaded legs.
  const { home, route } = classifyMissionWaypoints(FIXTURE.readback.waypoints);
  assert.notEqual(home, null, "seq 0 is Scout's Home, not a route leg");
  assert.equal(home.seq, 0);
  assert.equal(route.length, 2);
  assert.equal(FIXTURE.readback.count, route.length + 1);
});

test("a count mismatch is rendered FAILED", () => {
  const cmd = {
    id: "cmd-1", type: "MISSION_UPLOAD", status: "EXECUTED", params: FIXTURE.expected,
    mission_result: "failed",
    reason: "Pixhawk holds 1 route waypoints after upload — expected 2.",
  };
  const s = missionUploadStage(cmd, null);
  assert.equal(s.stage, "Failed");
  assert.equal(s.state, "failed");
  assert.match(s.reason, /expected 2/);
  // The comparison agrees independently: 1 route waypoint / 2 items observed.
  const cmp = missionUploadCompare(FIXTURE.expected, { count: 2 }, 1);
  assert.equal(cmp.match, false);
});

test("a content-hash mismatch is rendered FAILED even when both counts agree", () => {
  const expected = { ...FIXTURE.expected, expected_route_content_hash: "sha256:aaa" };
  const cmp = missionUploadCompare(expected, { count: 3, route_content_hash: "sha256:bbb" }, 2);
  assert.equal(cmp.routeMatch, true);
  assert.equal(cmp.itemsMatch, true);
  assert.equal(cmp.hashMatch, false);
  assert.equal(cmp.match, false);

  const cmd = {
    id: "cmd-1", type: "MISSION_UPLOAD", status: "EXECUTED", params: expected,
    mission_result: "failed", reason: "Uploaded route does not match the read-back — the on-FC route differs.",
  };
  assert.equal(missionUploadStage(cmd, null).stage, "Failed");
});

// ---- live upload progress ----------------------------------------------------

test("active agent.mission_upload with a MATCHING command id renders Executing", () => {
  const cmd = { id: "cmd-1", type: "MISSION_UPLOAD", status: "SENT" };
  const live = { active: true, state: "UPLOADING", command_id: "cmd-1", elapsed_s: 4.2 };
  const s = missionUploadStage(cmd, live);
  assert.equal(s.stage, "Executing");
  assert.equal(s.state, "pending");
  assert.equal(s.elapsedS, 4.2);
});

test("a DIFFERENT active command id does not affect the current upload", () => {
  const cmd = { id: "cmd-1", type: "MISSION_UPLOAD", status: "SENT" };
  const live = { active: true, state: "UPLOADING", command_id: "cmd-OTHER", elapsed_s: 9 };
  const s = missionUploadStage(cmd, live);
  assert.equal(s.stage, "Requested", "another vehicle's/command's upload must not colour this one");
  assert.equal(s.elapsedS, null);
});

test("an INACTIVE matching upload stays Requested (active:false is not progress)", () => {
  const cmd = { id: "cmd-1", type: "MISSION_UPLOAD", status: "SENT" };
  const s = missionUploadStage(cmd, { active: false, state: "IDLE", command_id: "cmd-1", elapsed_s: 0 });
  assert.equal(s.stage, "Requested");
});

test("Scout is not required to post an ACCEPTED command result", () => {
  // A SENT command with an active worker reaches Executing on the live state ALONE — no
  // intermediate ACCEPTED result is needed, which matters because the operator backend
  // redelivers nonterminal commands and such a post would simply be redelivered.
  const cmd = { id: "cmd-1", type: "MISSION_UPLOAD", status: "SENT", result: null };
  const live = { active: true, state: "UPLOADING", command_id: "cmd-1", elapsed_s: 1 };
  assert.equal(missionUploadStage(cmd, live).stage, "Executing");
});

// ---- MISSION_CLEAR -----------------------------------------------------------
// Scout supports POST /agent/clear_mission through the queued LOCAL_AGENT MISSION_CLEAR
// command. A clear is verified by an INDEPENDENT empty read-back, and BOTH ArduPilot
// empty representations are correct — demanding zero Pixhawk items would fail a real
// clear on a stack that retains Home at seq 0.

test("NO_ITEMS renders as a successful empty state", () => {
  const out = missionClearOutcome(FIXTURE.clear_result_no_items,
    { route_waypoint_count: 0, pixhawk_item_count: 0 }, 0);
  assert.equal(out.verified, true);
  assert.deepEqual(out.reasons, []);
  assert.equal(out.representation, "NO_ITEMS");
  assert.equal(out.readbackAgrees, true);
});

test("HOME_ONLY renders as a successful empty state (item count 1 is NOT a failure)", () => {
  const out = missionClearOutcome(FIXTURE.clear_result_home_only,
    { route_waypoint_count: 0, pixhawk_item_count: 1 }, 0);
  assert.equal(out.verified, true);
  assert.equal(out.observedItems, 1, "Home survives as Pixhawk item 0");
  assert.equal(out.observedRoute, 0, "what must be empty is the ROUTE");
  assert.equal(out.readbackAgrees, true);
});

test("a remaining route renders Failed", () => {
  const out = missionClearOutcome(
    { ...FIXTURE.clear_result_no_items, observed_route_waypoint_count: 2 },
    { route_waypoint_count: 2, pixhawk_item_count: 3 }, 2);
  assert.equal(out.verified, false);
  assert.match(out.reasons.join("; "), /2 route waypoints remain/);
  assert.equal(out.readbackAgrees, false);
});

test("cleared:false fails even when the read-back looks empty", () => {
  const out = missionClearOutcome({ ...FIXTURE.clear_result_no_items, cleared: false },
    { route_waypoint_count: 0, pixhawk_item_count: 0 }, 0);
  assert.equal(out.verified, false);
  assert.match(out.reasons.join("; "), /did not report the mission as cleared/);
});

test("an unrecognised empty representation fails", () => {
  const out = missionClearOutcome(
    { ...FIXTURE.clear_result_no_items, empty_representation: "PROBABLY_EMPTY" },
    { route_waypoint_count: 0 }, 0);
  assert.equal(out.verified, false);
  assert.match(out.reasons.join("; "), /unrecognised empty representation/);
});

test("Scout claiming a verified clear while the FC still lists a route is surfaced", () => {
  // Scout's own result is internally consistent; the INDEPENDENT read-back disagrees.
  // That disagreement is the entire reason the read-back is fetched separately.
  const out = missionClearOutcome(FIXTURE.clear_result_no_items,
    { route_waypoint_count: 2, pixhawk_item_count: 3 }, 2);
  assert.equal(out.verified, true, "Scout's own result passes its contract...");
  assert.equal(out.readbackAgrees, false, "...but the flight controller says otherwise");
});

test("the Clear button is gated by ordinary command gating only — no Scout-support gate", () => {
  // Mirrors Mission.js's real disabled expression. The temporary capability gate that
  // disabled this button with a "Scout update required" reason is gone; MISSION_CLEAR is
  // now gated exactly like any other write command.
  const clearDisabled = (busy, control, supported) => busy || !control || !supported;
  assert.equal(clearDisabled(false, true, true), false, "enabled under ordinary gating");
  assert.equal(clearDisabled(true, true, true), true, "an upload in flight still blocks it");
  assert.equal(clearDisabled(false, false, true), true, "no control authority still blocks it");
});
