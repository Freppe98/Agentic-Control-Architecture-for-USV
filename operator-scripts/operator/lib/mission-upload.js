// mission-upload.js — pure, DOM-free mission-file parsing, validation, preview and
// upload-lifecycle mapping for the Mission page's upload workflow (unit-tested,
// tests/mission-upload.test.mjs + tests/mission-contract.test.mjs). No DOM, no network:
// the ONE tested place that turns an operator-supplied mission file into a queued
// MISSION_UPLOAD's params and tracks its Requested → Executing → Verified/Failed
// progression. The command lifecycle decision itself is delegated to lib/command.js so
// this never re-implements "what counts as verified".
//
// ── mission-contract-v1: who owns what ──────────────────────────────────────────
// The operator supplies ROUTE WAYPOINTS ONLY. Scout owns Pixhawk sequence 0 / Home and
// prepends it when writing to the flight controller, so a route of N waypoints leaves
// N + 1 items on the Pixhawk. Nothing here emits a seq, a MAVLink command code, a frame
// or an altitude — those are Scout's to choose, and a mission file that supplies them is
// REJECTED rather than previewed, because Scout would discard them and the operator would
// have approved a mission that is not the mission actually uploaded.
//
// Accepts two mission encodings, both yielding route waypoints:
//   • Waypoint JSON — an array of waypoints, or { waypoints|mission|items: [...] },
//     each item carrying latitude/longitude (+ optional loiter_time_s).
//   • GeoJSON — a FeatureCollection / Feature / geometry of Point or LineString. Point
//     features become one route waypoint each; a LineString's positions become ordered
//     route waypoints. Coordinates are GeoJSON order [lng, lat] — a third position
//     element is an altitude and is rejected, not silently dropped.
//
// ── route content hashing is NOT computed here, by design ────────────────────────
// The OPERATOR BACKEND is the single authoritative calculator (mission_contract.py,
// surfaced as params.expected_route_content_hash). This module CONSUMES that string and
// compares it to Scout's observed one; it never recomputes it. A second implementation in
// browser JavaScript is a second thing that can drift, and two drifted implementations
// produce a hash comparison that is worse than none — it reports mismatch for correct
// routes and, worse, can agree for the wrong reason. An earlier version did compute a
// local "wpm1:" FNV-1a hash that Scout never computed; it is gone and stays gone.
import { commandVerification } from "./command.js";

export const MISSION_CONTRACT_VERSION = "mission-contract-v1";

// Fields a route waypoint may carry. Everything else is refused — see MISSION_REJECTED.
const ALLOWED_FIELDS = new Set(["latitude", "longitude", "loiter_time_s"]);
// Aliases accepted for convenience, mapped onto the canonical names. Deliberately narrow:
// these are spellings of the SAME concept, never a different concept being coerced.
const LAT_ALIASES = ["latitude", "lat"];
const LNG_ALIASES = ["longitude", "lng", "lon", "long"];
// Fields that used to be part of the old operator-side schema, or that a GCS export
// commonly carries, which Scout owns and would discard. Rejected by name with the reason,
// so the operator edits the file rather than guessing what went wrong.
const MISSION_REJECTED = {
  seq: "sequence numbers are Scout-owned (Scout owns seq 0 / Home and numbers the route)",
  sequence: "sequence numbers are Scout-owned (Scout owns seq 0 / Home and numbers the route)",
  command: "MAVLink command codes are Scout-owned — Scout writes every route item as a NAV_WAYPOINT",
  frame: "MAVLink frames are Scout-owned",
  altitude: "altitude is not part of mission-contract-v1 (surface vessel)",
  alt: "altitude is not part of mission-contract-v1 (surface vessel)",
  z: "altitude is not part of mission-contract-v1 (surface vessel)",
};

function fail(...msgs) {
  return {
    ok: false, format: null, waypoints: [], routeCount: 0, pixhawkItemCount: 0,
    first: null, last: null, errors: msgs,
  };
}
function isNum(x) { return typeof x === "number" && Number.isFinite(x); }
function pick(raw, names) {
  for (const n of names) { if (isNum(raw[n])) return raw[n]; }
  return null;
}

// One raw item (object or GeoJSON position) → a canonical route waypoint, or { errors }.
// `pos` is the 1-based position in the ROUTE — deliberately not a `seq`, which the
// operator does not own.
function normWaypoint(raw, pos) {
  const errors = [];
  let lat = null, lng = null, loiter = 0;

  if (Array.isArray(raw)) {
    // GeoJSON position [lng, lat] — a third element is an altitude, which this contract
    // does not carry. Rejected, never quietly discarded.
    if (raw.length > 2) {
      errors.push(`Route waypoint ${pos}: coordinate carries an altitude — ${MISSION_REJECTED.altitude}.`);
    }
    // NOT coerced with unary + : a quoted coordinate means the producing tool lost its
    // typing, and the backend refuses it (main._mission_number). Accepting it here would
    // preview a clean route that then fails on upload.
    lng = isNum(raw[0]) ? raw[0] : null;
    lat = isNum(raw[1]) ? raw[1] : null;
  } else if (raw && typeof raw === "object") {
    for (const [field, why] of Object.entries(MISSION_REJECTED)) {
      if (field in raw) errors.push(`Route waypoint ${pos}: remove \`${field}\` — ${why}.`);
    }
    const known = new Set([...ALLOWED_FIELDS, ...LAT_ALIASES, ...LNG_ALIASES, ...Object.keys(MISSION_REJECTED)]);
    const unknown = Object.keys(raw).filter((k) => !known.has(k));
    if (unknown.length) {
      errors.push(`Route waypoint ${pos}: unsupported field(s) ${unknown.join(", ")} — ` +
        `mission-contract-v1 accepts only ${[...ALLOWED_FIELDS].join(", ")}.`);
    }
    lat = pick(raw, LAT_ALIASES);
    lng = pick(raw, LNG_ALIASES);
    if ("loiter_time_s" in raw) {
      if (!isNum(raw.loiter_time_s) || raw.loiter_time_s < 0) {
        errors.push(`Route waypoint ${pos}: \`loiter_time_s\` must be a number >= 0.`);
      } else {
        loiter = raw.loiter_time_s;
      }
    }
  } else {
    return { errors: [`Route waypoint ${pos} is not an object or coordinate pair.`] };
  }

  if (lat === null || Math.abs(lat) > 90) errors.push(`Route waypoint ${pos}: \`latitude\` must be a number in [-90, 90].`);
  if (lng === null || Math.abs(lng) > 180) errors.push(`Route waypoint ${pos}: \`longitude\` must be a number in [-180, 180].`);
  if (errors.length) return { errors };
  return { waypoint: { latitude: lat, longitude: lng, loiter_time_s: loiter } };
}

// Pull the raw route list out of whichever encoding was supplied.
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
 * Parse + validate a mission file (JSON string or already-parsed object) into route
 * waypoints.
 * @returns {{ ok, format, waypoints, routeCount, pixhawkItemCount, first, last, errors }}
 *   ok=false with a populated `errors` array when the file is unusable — the caller shows
 *   every error and does NOT enable Upload. `waypoints` are canonical
 *   {latitude, longitude, loiter_time_s} route items with NO seq/command/frame/altitude.
 *   `pixhawkItemCount` is routeCount + 1: Scout's Home at seq 0 plus the route.
 */
export function parseMission(input) {
  let data = input;
  if (typeof input === "string") {
    if (!input.trim()) return fail("No mission provided.");
    try { data = JSON.parse(input); } catch (e) { return fail("Not valid JSON: " + e.message); }
  } else if (!input || typeof input !== "object") {
    return fail("No mission provided.");
  }
  // A file that declares a contract_version must declare THIS one — a v2 file parsed by
  // v1 rules would upload silently-wrong content.
  if (data && !Array.isArray(data) && data.contract_version != null
      && String(data.contract_version) !== MISSION_CONTRACT_VERSION) {
    return fail(`Unsupported contract_version "${data.contract_version}" — this station speaks ${MISSION_CONTRACT_VERSION}.`);
  }
  const { format, items } = rawItems(data);
  if (!format || !Array.isArray(items)) {
    return fail("Unrecognized mission format — expected a route waypoint array, { waypoints: [...] }, or GeoJSON.");
  }
  if (!items.length) return fail("Mission contains no route waypoints.");

  const waypoints = [], errors = [];
  items.forEach((raw, i) => {
    const r = normWaypoint(raw, i + 1);
    if (r.errors) errors.push(...r.errors);
    else waypoints.push(r.waypoint);
  });
  // All-or-nothing: uploading "most of" a route to a flight controller is worse than
  // uploading none of it.
  if (errors.length) {
    return { ok: false, format, waypoints: [], routeCount: 0, pixhawkItemCount: 0, first: null, last: null, errors };
  }
  const first = waypoints[0], last = waypoints[waypoints.length - 1];
  return {
    ok: true, format, waypoints,
    routeCount: waypoints.length,
    pixhawkItemCount: waypoints.length + 1,   // + Scout's Home at seq 0
    first: { lat: first.latitude, lng: first.longitude },
    last: { lat: last.latitude, lng: last.longitude },
    errors: [],
  };
}

/**
 * The MISSION_UPLOAD params to queue for a parsed, valid mission (never called for an
 * invalid one). Route waypoints only — no seq-0 Home is ever generated here. The backend
 * re-validates and re-derives both counts (it is the authority; this is the request), and
 * owns the route content hash once Scout's canonicalization is available.
 */
export function missionUploadParams(parsed) {
  return {
    contract_version: MISSION_CONTRACT_VERSION,
    waypoints: parsed.waypoints,
  };
}

// Requested → Executing → Verified. Scout does not post an intermediate ACCEPTED command
// result (the operator backend redelivers nonterminal commands at-least-once, so an
// ACCEPTED post would be redelivered and is deliberately not required of Scout); live
// progress instead comes from agent.mission_upload. Hence three stages, not four.
export const UPLOAD_STAGES = ["Requested", "Executing", "Verified"];

/**
 * Map a MISSION_UPLOAD command record + Scout's live upload state onto the operator-facing
 * stage. Terminal verified/failed comes from the SHARED commandVerification (never a
 * second rule): an EXECUTED upload whose read-back did not match is `Failed`, never
 * `Verified` — the file reaching Scout is never success on its own.
 *
 * @param cmd the tracked command record
 * @param missionUpload Scout's live `agent.mission_upload` ({active, state, command_id,
 *   elapsed_s}) or null. It only advances the stage when its `command_id` MATCHES this
 *   command: a background upload for some other command must never colour this one's
 *   progress, and an unmatched/absent state simply leaves the queue status to speak.
 * @returns {{ stage, index, state: 'idle'|'pending'|'done'|'failed', reason, elapsedS }}
 */
export function missionUploadStage(cmd, missionUpload) {
  if (!cmd) return { stage: "Idle", index: -1, state: "idle", reason: null, elapsedS: null };
  const status = cmd.status;
  const v = commandVerification(cmd);
  if (status === "EXECUTED") {
    return v.verified === true
      ? { stage: "Verified", index: 2, state: "done", reason: null, elapsedS: null }
      : { stage: "Failed", index: 2, state: "failed", reason: v.reason || "Upload was not verified by read-back.", elapsedS: null };
  }
  if (status === "REJECTED" || status === "FAILED" || status === "EXPIRED") {
    return {
      stage: "Failed", index: 2, state: "failed", elapsedS: null,
      reason: v.reason || cmd.reason || (status === "EXPIRED"
        ? "Upload timed out before Scout reported a result — mission state unknown."
        : "Upload failed."),
    };
  }
  // Live worker state, but only for THIS command.
  if (liveUploadMatches(missionUpload, cmd.id) && missionUpload.active) {
    return {
      stage: "Executing", index: 1, state: "pending", reason: null,
      elapsedS: missionUpload.elapsed_s ?? null,
    };
  }
  // Fallback for a Scout that reports a lifecycle array but has not yet shipped the
  // agent.mission_upload group (the transitional state this branch is written for). The
  // backend already merges Scout's own stages into cmd.lifecycle / cmd.scout_lifecycle,
  // so an EXECUTING there is real progress and there is no reason to discard it and sit
  // at Requested for the whole upload. The live block is preferred when present because
  // it carries elapsed_s and a command_id to match on.
  if (scoutReportedExecuting(cmd)) {
    return { stage: "Executing", index: 1, state: "pending", reason: null, elapsedS: null };
  }
  return { stage: "Requested", index: 0, state: "pending", reason: null, elapsedS: null };
}

// Did Scout's own lifecycle array report EXECUTING for this command? Tolerant of the
// stage/status/name spellings and of a bare string entry.
function scoutReportedExecuting(cmd) {
  for (const arr of [cmd.scout_lifecycle, cmd.lifecycle]) {
    if (!Array.isArray(arr)) continue;
    for (const s of arr) {
      const name = String((s && (s.stage || s.status || s.name)) || s || "").toUpperCase();
      if (name === "EXECUTING") return true;
    }
  }
  return false;
}

/**
 * Does Scout's live upload state describe THIS command? Matching is by command_id and
 * nothing else — never "an upload is active, so it must be mine". Returns false for a
 * missing state, a missing id on either side, or a different command's upload.
 */
export function liveUploadMatches(missionUpload, commandId) {
  if (!missionUpload || typeof missionUpload !== "object") return false;
  if (commandId == null || missionUpload.command_id == null) return false;
  return String(missionUpload.command_id) === String(commandId);
}

/**
 * Compare the operator's expected mission against a Pixhawk mission read-back
 * (api.getPixhawkMission), on three axes:
 *   • route waypoint count      — N (the read-back's items excluding Scout's seq-0 Home)
 *   • Pixhawk item count        — N + 1 (everything the FC holds, Home included)
 *   • route content hash        — Scout's route_content_hash over items 1…N, NEVER its
 *                                 full-mission hash (which includes the Home the operator
 *                                 never sent and therefore could not have hashed)
 * Under mission-contract-v1 the hash axis is REQUIRED: a missing expected or observed
 * route hash makes the comparison FAIL, not pass-on-counts. Matching counts cannot detect
 * two swapped waypoints or a wrong coordinate, so "counts agreed, content unchecked" must
 * never render as a verified route — that is precisely the false assurance this contract
 * exists to remove. `match` is therefore true only when all three axes agree.
 *
 * Observed counts prefer Scout's EXPLICIT read-back fields (route_waypoint_count,
 * pixhawk_item_count). The locally derived route count and the legacy `count` remain as
 * compatibility fallbacks for a Scout that predates the explicit fields.
 *
 * @param params the command's stored params (backend-canonical)
 * @param readback api.getPixhawkMission result
 * @param routeCount locally derived route waypoint count (Home excluded) — the caller
 *   derives it with lib/mission.js classifyMissionWaypoints, the same split the map
 *   overlay uses. Used only when Scout does not report route_waypoint_count.
 */
export function missionUploadCompare(params, readback, routeCount) {
  const p = params || {}, r = readback || {};
  const expectedRoute = p.expected_route_waypoint_count ?? null;
  const expectedItems = p.expected_pixhawk_item_count ?? null;
  const expectedHash = p.expected_route_content_hash ?? null;
  // Scout's own count wins over our local derivation: Scout owns the Home/route split, so
  // where it states the split explicitly, re-deriving it here would be second-guessing the
  // authority. The local count stays as the fallback for older Scout builds.
  const observedRoute = r.route_waypoint_count ?? routeCount ?? null;
  const observedItems = r.pixhawk_item_count ?? r.count ?? null;
  // Scout's ROUTE hash only. `r.hash` / `r.full_mission_hash` cover Home too — a different
  // value over different bytes — and are deliberately NEVER consulted here.
  const observedHash = r.route_content_hash ?? null;

  const cmp = (a, b) => (a != null && b != null ? Number(a) === Number(b) : null);
  const routeMatch = cmp(expectedRoute, observedRoute);
  const itemsMatch = cmp(expectedItems, observedItems);
  const hashMatch = expectedHash != null && observedHash != null
    ? String(expectedHash) === String(observedHash) : null;

  // hashMatch === null (either side absent) is a FAILURE, not an abstention.
  const match = hashMatch === true && routeMatch !== false && itemsMatch !== false;
  return {
    expectedRoute, observedRoute, expectedItems, observedItems, expectedHash, observedHash,
    routeMatch, itemsMatch, hashMatch, match,
    // True when content could not be checked at all. The UI uses this to say WHY the
    // upload is unverified — "content not compared" reads very differently from
    // "content did not match", and the operator needs to tell them apart.
    hashUnavailable: hashMatch === null,
    // Whether the read-back's route count came from Scout or from our local derivation.
    routeCountSource: r.route_waypoint_count != null ? "scout" : "derived",
  };
}

/** The two empty states ArduPilot legitimately reports after a clear. Both are correct:
 *  some stacks wipe every item, others retain Home at seq 0. Mirrors
 *  main.MISSION_EMPTY_REPRESENTATIONS. */
export const EMPTY_REPRESENTATIONS = ["NO_ITEMS", "HOME_ONLY"];

/**
 * Classify a MISSION_CLEAR against Scout's result and an independent Pixhawk read-back.
 *
 * A clear is verified when Scout says accepted + cleared + verified, reports ZERO route
 * waypoints, and names a recognised empty representation. The Pixhawk ITEM count is
 * deliberately NOT required to be zero: HOME_ONLY (item count 1, route count 0) is a
 * correctly cleared mission, and demanding 0 would fail real clears on ArduPilot stacks
 * that retain Home. What must be empty is the ROUTE.
 *
 * @param result the command's Scout result
 * @param readback api.getPixhawkMission result, fetched fresh AFTER the clear — the
 *   independent confirmation. Its own route count is reported separately so the UI can
 *   show that the FC really is empty, not just that Scout said so.
 * @param routeCount locally derived route count from the read-back (Home excluded),
 *   used only when Scout's read-back omits route_waypoint_count.
 */
export function missionClearOutcome(result, readback, routeCount) {
  const r = result || {}, rb = readback || {};
  const observedRoute = r.observed_route_waypoint_count ?? null;
  const observedItems = r.observed_pixhawk_item_count ?? null;
  const representation = r.empty_representation ?? null;

  const reasons = [];
  if (r.accepted !== true) reasons.push("Scout did not accept the clear");
  if (r.cleared !== true) reasons.push("Scout did not report the mission as cleared");
  if (r.verified !== true) reasons.push("Scout did not verify the clear by read-back");
  if (observedRoute == null) reasons.push("Scout reported no observed route waypoint count");
  else if (Number(observedRoute) !== 0) reasons.push(`${observedRoute} route waypoints remain`);
  if (!EMPTY_REPRESENTATIONS.includes(representation)) {
    reasons.push(`unrecognised empty representation ${representation == null ? "(none)" : representation}`);
  }

  // The independent axis: what the Pixhawk itself reports now, derived without Scout's
  // claim. A clear that Scout calls verified while the FC still lists a route is exactly
  // the disagreement worth surfacing.
  const rbRoute = rb.route_waypoint_count ?? routeCount ?? null;
  const rbItems = rb.pixhawk_item_count ?? rb.count ?? null;

  return {
    verified: reasons.length === 0,
    reasons,
    observedRoute, observedItems, representation,
    readbackRoute: rbRoute, readbackItems: rbItems,
    // Only meaningful once a read-back exists; null when there is nothing to disagree with.
    readbackAgrees: rbRoute == null ? null : Number(rbRoute) === 0,
  };
}
