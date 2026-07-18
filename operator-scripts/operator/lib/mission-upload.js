// mission-upload.js — pure, DOM-free mission-file parsing, validation, preview, hashing
// and upload-lifecycle mapping for the Mission page's upload workflow (unit-tested,
// tests/mission-upload.test.mjs). No DOM, no network: the ONE tested place that turns an
// operator-supplied mission file into a queued MISSION_UPLOAD's params and tracks its
// Requested → Accepted → Executing → Verified/Failed progression against the Scout
// read-back. The command lifecycle decision itself is delegated to lib/command.js so
// this never re-implements "what counts as verified".
//
// Accepts two mission encodings:
//   • Canonical waypoint JSON — an array of waypoints, or { waypoints|mission|items: [...] }
//     where each item carries lat/lng (+ optional seq, command, alt).
//   • GeoJSON — a FeatureCollection / Feature / geometry of Point or LineString. Point
//     features become one waypoint each; a LineString's positions become ordered
//     waypoints. Coordinates are GeoJSON order [lng, lat, alt?].
import { commandVerification } from "./command.js";

const DEFAULT_CMD = 16; // MAV_CMD_NAV_WAYPOINT

function fail(msg) {
  return { ok: false, format: null, waypoints: [], count: 0, first: null, last: null, hash: null, errors: [msg] };
}
function round(v, dp) {
  if (v == null || Number.isNaN(+v)) return null;
  const f = 10 ** dp;
  return Math.round(+v * f) / f;
}
function validLat(x) { return typeof x === "number" && Number.isFinite(x) && Math.abs(x) <= 90; }
function validLng(x) { return typeof x === "number" && Number.isFinite(x) && Math.abs(x) <= 180; }

// One raw item (object or GeoJSON position) → a normalized waypoint, or { error }.
function normWaypoint(raw, seq) {
  let lat = null, lng = null, alt = null, command = DEFAULT_CMD;
  if (Array.isArray(raw)) {
    // GeoJSON position [lng, lat, alt?]
    lng = raw.length > 0 ? +raw[0] : null;
    lat = raw.length > 1 ? +raw[1] : null;
    alt = raw.length > 2 ? +raw[2] : null;
  } else if (raw && typeof raw === "object") {
    lat = pickNum(raw.lat, raw.latitude, raw.y);
    lng = pickNum(raw.lng, raw.lon, raw.long, raw.longitude, raw.x);
    alt = pickNum(raw.alt, raw.altitude, raw.z);
    if (raw.command != null) command = +raw.command;
    if (raw.seq != null) seq = +raw.seq;
  } else {
    return { error: `Waypoint ${seq} is not an object or coordinate pair.` };
  }
  if (!validLat(lat) || !validLng(lng)) {
    return { error: `Waypoint ${seq} has an invalid or missing lat/lng.` };
  }
  return { waypoint: { seq, command, lat, lng, alt: alt == null || Number.isNaN(alt) ? null : alt } };
}
function pickNum(...vals) {
  for (const v of vals) { if (v != null && v !== "" && Number.isFinite(+v)) return +v; }
  return null;
}

// Pull the raw waypoint list out of whichever encoding was supplied.
function rawItems(data) {
  if (Array.isArray(data)) return { format: "waypoints", items: data };
  if (!data || typeof data !== "object") return { format: null, items: null };
  const type = String(data.type || "").toLowerCase();
  if (type === "featurecollection" && Array.isArray(data.features)) {
    const items = [];
    for (const f of data.features) {
      const g = f && f.geometry;
      if (!g) continue;
      if (String(g.type).toLowerCase() === "point" && Array.isArray(g.coordinates)) items.push(g.coordinates);
      else if (String(g.type).toLowerCase() === "linestring" && Array.isArray(g.coordinates)) items.push(...g.coordinates);
    }
    return { format: "geojson", items };
  }
  if (type === "feature" && data.geometry) return rawItems({ type: "featurecollection", features: [data] });
  if (type === "point" || type === "linestring") return rawItems({ type: "featurecollection", features: [{ geometry: data }] });
  for (const k of ["waypoints", "mission", "items", "mission_items"]) {
    if (Array.isArray(data[k])) return { format: "waypoints", items: data[k] };
  }
  return { format: null, items: null };
}

/**
 * Parse + validate a mission file (JSON string or already-parsed object).
 * @returns {{ ok, format, waypoints, count, first, last, hash, errors }}
 *   ok=false with a populated `errors` array when the file is unusable — the caller shows
 *   the errors and does NOT enable Upload. `first`/`last` are {lat,lng} of the route ends.
 */
export function parseMission(input) {
  let data = input;
  if (typeof input === "string") {
    if (!input.trim()) return fail("No mission provided.");
    try { data = JSON.parse(input); } catch (e) { return fail("Not valid JSON: " + e.message); }
  } else if (!input || typeof input !== "object") {
    return fail("No mission provided.");
  }
  const { format, items } = rawItems(data);
  if (!format || !Array.isArray(items)) {
    return fail("Unrecognized mission format — expected a waypoint array, { waypoints: [...] }, or GeoJSON.");
  }
  if (!items.length) return fail("Mission contains no waypoints.");
  const waypoints = [], errors = [];
  items.forEach((raw, i) => {
    const r = normWaypoint(raw, i);
    if (r.error) errors.push(r.error);
    else waypoints.push(r.waypoint);
  });
  if (errors.length) return { ok: false, format, waypoints, count: waypoints.length, first: null, last: null, hash: null, errors };
  const first = waypoints[0], last = waypoints[waypoints.length - 1];
  return {
    ok: true, format, waypoints, count: waypoints.length,
    first: { lat: first.lat, lng: first.lng },
    last: { lat: last.lat, lng: last.lng },
    hash: missionHash(waypoints),
    errors: [],
  };
}

// Deterministic content hash of a mission's waypoints. Version-tagged ("wpm1:") so the
// algorithm is explicit and a comparison against Scout's read-back hash is unambiguous.
// NOTE: this is only meaningful for expected-vs-observed comparison if Scout computes the
// SAME canonical form + hash — a Scout-contract assumption still to be confirmed. FNV-1a
// (32-bit) over a canonical [seq,command,lat(7dp),lng(7dp),alt(2dp)] tuple string.
export function missionHash(waypoints) {
  const canon = (waypoints || []).map((w) => [
    w.seq ?? "", w.command ?? DEFAULT_CMD, round(w.lat, 7), round(w.lng, 7), round(w.alt, 2),
  ]);
  return "wpm1:" + fnv1a(JSON.stringify(canon));
}
function fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/** The MISSION_UPLOAD params to queue for a parsed, valid mission (never called for an
 *  invalid one). Carries the expected count + hash so the read-back can be verified. */
export function missionUploadParams(parsed) {
  return {
    format: parsed.format,
    waypoints: parsed.waypoints,
    expected_count: parsed.count,
    expected_hash: parsed.hash,
  };
}

export const UPLOAD_STAGES = ["Requested", "Accepted", "Executing", "Verified"];

function latestScoutStage(cmd) {
  let found = null;
  for (const arr of [cmd.scout_lifecycle, cmd.lifecycle]) {
    if (!Array.isArray(arr)) continue;
    for (const s of arr) {
      const name = String((s && (s.stage || s.status || s.name)) || s || "").toUpperCase();
      if (name === "EXECUTING" || name === "ACCEPTED") found = name; // last wins
    }
  }
  return found;
}

/**
 * Map a MISSION_UPLOAD / MISSION_CLEAR command record onto its operator-facing upload
 * stage. Terminal verified/failed comes from the SHARED commandVerification (never a
 * second rule): an EXECUTED upload whose read-back did not match is `Failed`, never
 * `Verified` — the file reaching Scout is never success on its own.
 * @returns {{ stage, index, state: 'idle'|'pending'|'done'|'failed', reason }}
 */
export function missionUploadStage(cmd) {
  if (!cmd) return { stage: "Idle", index: -1, state: "idle", reason: null };
  const status = cmd.status;
  const v = commandVerification(cmd);
  if (status === "EXECUTED") {
    return v.verified === true
      ? { stage: "Verified", index: 3, state: "done", reason: null }
      : { stage: "Failed", index: 3, state: "failed", reason: v.reason || "Upload was not verified by read-back." };
  }
  if (status === "REJECTED" || status === "FAILED" || status === "EXPIRED") {
    return {
      stage: "Failed", index: 3, state: "failed",
      reason: v.reason || cmd.reason || (status === "EXPIRED"
        ? "Upload timed out before Scout reported a result — mission state unknown."
        : "Upload failed."),
    };
  }
  const scoutStage = latestScoutStage(cmd);
  if (scoutStage === "EXECUTING") return { stage: "Executing", index: 2, state: "pending", reason: null };
  if (scoutStage === "ACCEPTED" || status === "ACCEPTED") return { stage: "Accepted", index: 1, state: "pending", reason: null };
  return { stage: "Requested", index: 0, state: "pending", reason: null };
}

/**
 * Compare the operator's expected mission (count/hash) against a Pixhawk mission
 * read-back (api.getPixhawkMission). Returns per-axis matches and an overall `match`
 * (false if any present axis disagrees, true if any present axis agrees, null when
 * nothing comparable — e.g. Scout reports no hash). Never claims a match it cannot prove.
 */
export function missionUploadCompare(params, readback) {
  const expectedCount = params && params.expected_count != null ? params.expected_count : null;
  const expectedHash = params && params.expected_hash != null ? params.expected_hash : null;
  const observedCount = readback && readback.count != null ? readback.count : null;
  const observedHash = readback && readback.hash != null ? readback.hash : null;
  const countMatch = expectedCount != null && observedCount != null ? expectedCount === observedCount : null;
  const hashMatch = expectedHash != null && observedHash != null ? String(expectedHash) === String(observedHash) : null;
  const match = (countMatch === false || hashMatch === false) ? false
    : (countMatch === true || hashMatch === true) ? true : null;
  return { expectedCount, observedCount, expectedHash, observedHash, countMatch, hashMatch, match };
}
