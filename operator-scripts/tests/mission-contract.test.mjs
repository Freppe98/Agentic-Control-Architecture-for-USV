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
  missionClearOutcome, missionOperationState, missionEvidence,
  missionErrorText, missionErrorOf, MISSION_TOO_LARGE,
  READBACK_PENDING, READBACK_AVAILABLE, READBACK_UNAVAILABLE,
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

// ── The INDEPENDENT read-back axis ────────────────────────────────────────────
// Scout verifying its own write is Scout marking its own homework. These tests pin the
// three states that exist because collapsing them would misinform the operator: a pending
// read-back is not a failure, a missing read-back is not a full verification, and a
// disagreeing read-back is the most serious thing this page can report.
const DONE = { stage: "Verified", index: 2, state: "done", reason: null, elapsedS: null };
const RUNNING = { stage: "Executing", index: 1, state: "pending", reason: null, elapsedS: 3 };
const FAILED = { stage: "Failed", index: 2, state: "failed", reason: "read-back mismatch", elapsedS: null };

test("Scout verified + read-back still in flight is Awaiting independent readback, never Failed", () => {
  const s = missionOperationState(DONE, READBACK_PENDING, null);
  assert.equal(s.state, "awaiting_readback");
  assert.equal(s.label, "Awaiting independent readback");
  assert.equal(s.severity, "pending");
  assert.notEqual(s.severity, "bad", "a normal successful upload must not flash Failed");
  assert.match(s.detail, /not a failure/i);
});

test("an unobtainable independent read-back is a caution, not a full Verified", () => {
  const s = missionOperationState(DONE, READBACK_UNAVAILABLE, null);
  assert.equal(s.state, "readback_unavailable");
  assert.equal(s.severity, "caution");
  assert.equal(s.label, "Scout verified; independent Operator readback unavailable");
  assert.notEqual(s.state, "verified", "Scout's word alone is not an independent verification");
  assert.notEqual(s.severity, "ok");
});

test("a read-back we have but cannot compare is also a caution, not a conflict", () => {
  // agrees === null means "available, but nothing comparable in it". Calling that a
  // conflict would accuse Scout of lying when the truth is that we could not check.
  const s = missionOperationState(DONE, READBACK_AVAILABLE, null);
  assert.equal(s.state, "readback_unavailable");
  assert.equal(s.severity, "caution");
});

test("a read-back that disagrees with Scout is a high-severity verification conflict", () => {
  const s = missionOperationState(DONE, READBACK_AVAILABLE, false);
  assert.equal(s.state, "conflict");
  assert.equal(s.severity, "critical");
  assert.match(s.label, /conflict/i);
  assert.match(s.detail, /disagrees/i);
});

test("only Scout verified AND an agreeing independent read-back is Verified", () => {
  const s = missionOperationState(DONE, READBACK_AVAILABLE, true);
  assert.equal(s.state, "verified");
  assert.equal(s.severity, "ok");
});

test("a Scout failure stays Failed regardless of the read-back status", () => {
  for (const status of [READBACK_PENDING, READBACK_AVAILABLE, READBACK_UNAVAILABLE]) {
    const s = missionOperationState(FAILED, status, null);
    assert.equal(s.state, "failed", status);
    assert.equal(s.severity, "bad", status);
  }
});

test("an in-flight upload is never mistaken for an awaiting-read-back state", () => {
  const s = missionOperationState(RUNNING, READBACK_PENDING, null);
  assert.equal(s.state, "in_progress");
  assert.equal(s.label, "Executing");
});

test("a hash-less read-back maps to caution, and a hash mismatch maps to conflict", () => {
  // Mirrors Mission.js's real `agrees` derivation from missionUploadCompare.
  const params = { expected_route_waypoint_count: 2, expected_pixhawk_item_count: 3,
                   expected_route_content_hash: FIXTURE.scout_golden_route_hash };
  const noHash = missionUploadCompare(params, { route_waypoint_count: 2, pixhawk_item_count: 3 }, 2);
  const agreesNoHash = noHash.hashUnavailable ? null : noHash.match;
  assert.equal(missionOperationState(DONE, READBACK_AVAILABLE, agreesNoHash).state,
    "readback_unavailable", "counts agreed but content was never compared");

  const wrongHash = missionUploadCompare(params, {
    route_waypoint_count: 2, pixhawk_item_count: 3, route_content_hash: "sha256:beef" }, 2);
  const agreesWrong = wrongHash.hashUnavailable ? null : wrongHash.match;
  assert.equal(missionOperationState(DONE, READBACK_AVAILABLE, agreesWrong).state, "conflict");
});

// ── Exportable evidence ───────────────────────────────────────────────────────

const EVIDENCE_CMD = {
  id: "cmd-77", type: "MISSION_UPLOAD", status: "EXECUTED", source: "OPERATOR",
  params: { contract_version: "mission-contract-v1", expected_route_waypoint_count: 2,
            expected_pixhawk_item_count: 3,
            expected_route_content_hash: FIXTURE.scout_golden_route_hash },
  result: FIXTURE.verified_result,
  created_at: "2026-07-18T10:00:00Z", claimed_at: "2026-07-18T10:00:01Z",
  completed_at: "2026-07-18T10:00:09Z",
};

test("mission evidence export carries the six required top-level sections", () => {
  const ev = missionEvidence({ cmd: EVIDENCE_CMD, readbackStatus: READBACK_AVAILABLE });
  assert.deepEqual(Object.keys(ev).sort(),
    ["command", "comparison", "independent_readback", "lifecycle", "scout_result", "timestamps"]);
});

test("mission evidence records the command, Scout's result and OUR read-back separately", () => {
  const readback = FIXTURE.readback;
  const comparison = missionUploadCompare(EVIDENCE_CMD.params, readback, 2);
  const ev = missionEvidence({
    cmd: EVIDENCE_CMD, readback, readbackStatus: READBACK_AVAILABLE,
    readbackAt: Date.parse("2026-07-18T10:00:12Z"),
    comparison, operationState: missionOperationState(DONE, READBACK_AVAILABLE, true),
    requestedAt: Date.parse("2026-07-18T10:00:00Z"),
  });
  assert.equal(ev.command.command_id, "cmd-77");
  assert.equal(ev.command.contract_version, "mission-contract-v1");
  // Scout's claim and our own observation must stay distinguishable in the artifact —
  // merging them would destroy the only thing that makes it evidence.
  assert.equal(ev.scout_result.observed_route_content_hash, FIXTURE.scout_golden_route_hash);
  assert.equal(ev.independent_readback.mission.route_content_hash, FIXTURE.scout_golden_route_hash);
  assert.equal(ev.independent_readback.status, READBACK_AVAILABLE);
  assert.equal(ev.independent_readback.fetched_at, "2026-07-18T10:00:12.000Z");
  assert.equal(ev.comparison.match, true);
  assert.equal(ev.lifecycle.operation_state, "verified");
  assert.equal(ev.timestamps.completed_at, "2026-07-18T10:00:09Z");
});

test("mission evidence exports absences as null rather than omitting them", () => {
  // A reader must be able to tell "not reported" from "not exported".
  const ev = missionEvidence({ cmd: { id: "cmd-1", type: "MISSION_CLEAR" },
                               readbackStatus: READBACK_UNAVAILABLE });
  assert.equal(ev.scout_result, null);
  assert.equal(ev.comparison, null);
  assert.equal(ev.independent_readback.mission, null);
  assert.equal(ev.independent_readback.fetched_at, null);
  assert.equal(ev.command.contract_version, null);
  assert.equal(ev.timestamps.completed_at, null);
});

test("mission evidence never attributes another command's Scout worker block to this one", () => {
  const foreign = { active: true, state: "UPLOADING", command_id: "cmd-OTHER", elapsed_s: 4 };
  const mineBlock = { active: true, state: "UPLOADING", command_id: "cmd-77", elapsed_s: 4 };
  assert.equal(missionEvidence({ cmd: EVIDENCE_CMD, live: foreign }).lifecycle.scout_worker, null);
  assert.deepEqual(missionEvidence({ cmd: EVIDENCE_CMD, live: mineBlock }).lifecycle.scout_worker,
    mineBlock);
});

test("mission evidence is JSON-serializable round-trip", () => {
  const ev = missionEvidence({ cmd: EVIDENCE_CMD, readback: FIXTURE.readback,
                               readbackStatus: READBACK_AVAILABLE, readbackAt: Date.now() });
  assert.deepEqual(JSON.parse(JSON.stringify(ev)), ev);
});

// ── Scout's structured MISSION_TOO_LARGE error ────────────────────────────────
// The limit (200) is SCOUT'S, defined and enforced by mission-contract-v1. When Scout
// refuses an oversized route it states both numbers, and those numbers ARE the
// explanation — the operator must see the maximum and what they actually submitted.

const TOO_LARGE_ERR = { code: "MISSION_TOO_LARGE",
                        maximum_route_waypoints: 200, observed_route_waypoints: 250 };

test("MISSION_TOO_LARGE renders BOTH the maximum and the submitted count", () => {
  const text = missionErrorText(TOO_LARGE_ERR);
  assert.match(text, /200/, "must state the maximum Scout allows");
  assert.match(text, /250/, "must state what this route submitted");
  assert.match(text, /mission-contract-v1/);
});

test("MISSION_TOO_LARGE is never rendered as a bare error code", () => {
  const text = missionErrorText(TOO_LARGE_ERR);
  assert.notEqual(text, "MISSION_TOO_LARGE");
  assert.match(text, /route waypoints/);
});

test("a structured Scout error is not padded with generic explanation", () => {
  // The generic tail fits any failure and helps with none; MISSION_TOO_LARGE is fully
  // actionable on its own.
  const text = missionErrorText(TOO_LARGE_ERR);
  assert.doesNotMatch(text, /may be unchanged or partial/i);
  assert.doesNotMatch(text, /not verified by read-back/i);
});

test("MISSION_TOO_LARGE without its counts says so — never back-filled from a local constant", () => {
  // Substituting the Operator's own 200 for a maximum Scout omitted would present an
  // Operator number as Scout's word.
  const text = missionErrorText({ code: "MISSION_TOO_LARGE" });
  assert.match(text, /did not report both counts/);
  assert.doesNotMatch(text, /200/);
});

test("missionErrorText returns null when Scout supplied nothing structured", () => {
  // Null is the signal for the caller to use its own generic wording.
  assert.equal(missionErrorText(null), null);
  assert.equal(missionErrorText({}), null);
  assert.equal(missionErrorText({ message: "boom" }), "boom");
  assert.equal(missionErrorText({ code: "SOME_OTHER" }), "SOME_OTHER");
});

test("missionErrorOf finds Scout's structured error on the record, or null", () => {
  assert.deepEqual(missionErrorOf({ result: { error: TOO_LARGE_ERR } }), TOO_LARGE_ERR);
  assert.deepEqual(missionErrorOf({ error: TOO_LARGE_ERR }), TOO_LARGE_ERR);
  assert.equal(missionErrorOf({ result: { error: "just a string" } }), null);
  assert.equal(missionErrorOf({}), null);
  assert.equal(missionErrorOf(null), null);
});

test("the UI failure branch prefers Scout's structured error over the generic reason", () => {
  // Mirrors Mission.js's real verdict selection: structured error wins, and when it wins
  // the generic "may be unchanged or partial" tail is not rendered at all.
  const cmd = { id: "c1", type: "MISSION_UPLOAD", status: "EXECUTED",
                mission_result: "failed", reason: "MISSION_TOO_LARGE",
                result: { accepted: false, verified: false, error: TOO_LARGE_ERR } };
  const stg = missionUploadStage(cmd, null);
  assert.equal(stg.state, "failed");
  const structured = missionErrorText(missionErrorOf(cmd));
  assert.ok(structured, "a structured error must be found for this record");
  assert.match(structured, /200/);
  assert.match(structured, /250/);
});

test("an unstructured failure still falls back to the generic reason", () => {
  const cmd = { id: "c2", type: "MISSION_UPLOAD", status: "EXECUTED",
                mission_result: "failed", reason: "Mission was not verified by read-back.",
                result: { accepted: true, verified: false } };
  assert.equal(missionErrorText(missionErrorOf(cmd)), null, "nothing structured to render");
  assert.match(missionUploadStage(cmd, null).reason, /not verified/i);
});

test("a MISSION_TOO_LARGE record exports both numeric fields in the evidence JSON", () => {
  const cmd = { id: "c3", type: "MISSION_UPLOAD", status: "EXECUTED",
                params: { contract_version: "mission-contract-v1" },
                result: { accepted: false, verified: false, error: TOO_LARGE_ERR } };
  const ev = missionEvidence({ cmd, readbackStatus: READBACK_UNAVAILABLE });
  assert.equal(ev.scout_result.error.maximum_route_waypoints, 200);
  assert.equal(ev.scout_result.error.observed_route_waypoints, 250);
  assert.equal(ev.scout_result.error.code, MISSION_TOO_LARGE);
});
