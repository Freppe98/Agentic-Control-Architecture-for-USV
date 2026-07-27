// planning.js — pure, DOM-free state model for the Plan page (survey mission planning).
// Unit-tested (tests/planning.test.mjs). No DOM, no network: the ONE place that decides the
// page's planning state, whether a generated route is outdated, and how a draft serializes.
// Route generation itself lives in the backend (planning.py, ported from Scout); this module
// prepares its inputs, tracks the small page-specific state machine around it, and turns a
// generated route into the mission-contract upload params via lib/mission-upload.js.
//
// ── the state machine (a SMALL page-specific model, not a workflow engine) ───────────────
// EMPTY → BOUNDARY_DEFINED → CONFIGURED → ROUTE_GENERATED → VALID → UPLOADING → UPLOADED,
// with ROUTE_OUTDATED whenever a generation-affecting input changes after generation, and
// ERROR for a terminal upload failure. The workflow is ordered but revisitable: the operator
// can return to any earlier step, and doing so re-derives the state from the model — the
// state is never stored, always computed from the geometry + params + generation revision.
import { missionUploadParams, parseMission } from "./mission-upload.js";

export const PLAN_STATES = {
  EMPTY: "EMPTY",                     // no survey boundary yet
  BOUNDARY_DEFINED: "BOUNDARY_DEFINED", // boundary drawn, spacing not set
  CONFIGURED: "CONFIGURED",           // boundary + spacing set, no route yet
  ROUTE_OUTDATED: "ROUTE_OUTDATED",   // a route exists but an input changed since
  ROUTE_GENERATED: "ROUTE_GENERATED", // a current route exists, not yet validated OK
  VALID: "VALID",                     // current route + validation passed
  UPLOADING: "UPLOADING",             // upload command in flight
  UPLOADED: "UPLOADED",               // upload verified
  ERROR: "ERROR",                     // terminal upload failure
};

/** The planning parameters, with the sensible defaults the UI starts from. Lane spacing is
 *  intentionally null (no invented sonar default): the operator must choose it per the sonar
 *  swath width and desired overlap. Speed has a conservative default used only for the
 *  duration ESTIMATE. */
export function defaultParams() {
  return {
    shoreline_clearance_m: 5,
    lane_spacing_m: null,
    primary_angle_deg: 0,
    dual_pass: false,
    secondary_angle_deg: null,     // null → primary + 90 at generation time
    survey_speed_mps: 1.5,
  };
}

/** A fresh, empty planning model. */
export function emptyModel() {
  return {
    vehicleId: null,
    boundary: null,                // closed ring [[lng,lat],...] or null
    noGoZones: [],                 // [{ id, ring }]
    home: null,                    // [lng,lat] or null
    approach: [],                  // [[lng,lat],...] operator approach waypoints (A1,A2,...)
    returns: [],                   // [[lng,lat],...] operator return waypoints (R1,R2,...)
    routeStartMode: "planning_home",  // "planning_home" | "first_approach"
    params: defaultParams(),
    generated: null,               // backend generate result (operator-survey-plan-v1 package)
    generatedRevision: null,       // inputRevision at the moment of generation
    validation: null,              // backend validate result
    upload: { phase: "idle", cmdId: null, error: null, at: 0, result: null },
    _zoneSeq: 0,
  };
}

/** Route-start modes offered by the page. Planning home is the prototype default. */
export const ROUTE_START_MODES = ["planning_home", "first_approach"];
export const ROUTE_START_LABEL = {
  planning_home: "Planning home",
  first_approach: "First approach waypoint",
};

function num(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function round7(v) { return Math.round(v * 1e7) / 1e7; }

/** Normalize the secondary angle: explicit value, else primary + 90 (mod 360). */
export function effectiveSecondaryAngle(params) {
  const p = params || {};
  const primary = ((num(p.primary_angle_deg) || 0) % 360 + 360) % 360;
  const sec = num(p.secondary_angle_deg);
  return sec == null ? (primary + 90) % 360 : ((sec % 360) + 360) % 360;
}

/** A stable local id for the next no-go zone (never reused within a session). */
export function nextZoneId(model) {
  return "ngz-" + (model._zoneSeq + 1);
}

/** Add a no-go zone ring, returning a new model. Zones may only be added once a boundary
 *  exists — enforced by the caller (canAddZone), mirrored here for safety. */
export function addNoGoZone(model, ring) {
  const seq = model._zoneSeq + 1;
  return {
    ...model,
    _zoneSeq: seq,
    noGoZones: [...model.noGoZones, { id: "ngz-" + seq, ring: closeRing(ring) }],
  };
}
export function removeNoGoZone(model, id) {
  return { ...model, noGoZones: model.noGoZones.filter((z) => z.id !== id) };
}

/** True once a survey boundary exists — the precondition for drawing no-go zones and for
 *  generating a route. */
export function hasBoundary(model) { return !!(model.boundary && model.boundary.length >= 4); }
export function canAddZone(model) { return hasBoundary(model); }

/** A ring is drawable/usable when it has at least 3 DISTINCT vertices. */
export function ringIsValid(ring) {
  if (!Array.isArray(ring)) return false;
  const uniq = [];
  for (const p of ring) {
    if (!Array.isArray(p) || num(p[0]) == null || num(p[1]) == null) return false;
    const key = round7(p[0]) + "," + round7(p[1]);
    if (!uniq.includes(key)) uniq.push(key);
  }
  return uniq.length >= 3;
}
/** Close a ring (first === last) without mutating the input. */
export function closeRing(ring) {
  if (!Array.isArray(ring) || !ring.length) return ring;
  const r = ring.map((p) => [p[0], p[1]]);
  const a = r[0], b = r[r.length - 1];
  if (a[0] !== b[0] || a[1] !== b[1]) r.push([a[0], a[1]]);
  return r;
}

/** The request body for POST /api/planning/generate (and /validate). Pure derivation from
 *  the model — the single place input shape is defined, so generate and validate can never
 *  disagree about what the inputs were. */
export function planningInputs(model) {
  const p = model.params || {};
  return {
    boundary: model.boundary,
    shoreline_clearance_m: num(p.shoreline_clearance_m) || 0,
    lane_spacing_m: num(p.lane_spacing_m),
    primary_angle_deg: ((num(p.primary_angle_deg) || 0) % 360 + 360) % 360,
    dual_pass: !!p.dual_pass,
    secondary_angle_deg: effectiveSecondaryAngle(p),
    survey_speed_mps: num(p.survey_speed_mps),
    no_go_zones: model.noGoZones.map((z) => z.ring),
    home: model.home,
    route_start_mode: model.routeStartMode || "planning_home",
    approach_waypoints: model.approach || [],
    return_waypoints: model.returns || [],
  };
}

/** A stable revision string over EVERY generation-affecting input. Two models with the same
 *  geometry + parameters produce the same revision; any change to the boundary, a no-go zone,
 *  the home, transit waypoints, spacing, clearance, angle, dual-pass or speed changes it — so
 *  a stored generatedRevision that no longer matches is exactly "the route is outdated". */
export function inputRevision(model) {
  const i = planningInputs(model);
  const ring = (r) => (r || []).map((p) => [round7(p[0]), round7(p[1])]);
  const canonical = {
    b: ring(i.boundary),
    z: (i.no_go_zones || []).map(ring),
    h: i.home ? [round7(i.home[0]), round7(i.home[1])] : null,
    ap: ring(i.approach_waypoints),
    rt: ring(i.return_waypoints),
    m: i.route_start_mode,
    c: i.shoreline_clearance_m,
    s: i.lane_spacing_m,
    a: i.primary_angle_deg,
    d: i.dual_pass,
    a2: i.secondary_angle_deg,
    v: i.survey_speed_mps,
  };
  return JSON.stringify(canonical);
}

/** True when a route exists but a generation-affecting input has changed since it was made.
 *  An outdated route may not be uploaded until regenerated. */
export function isOutdated(model) {
  return !!model.generated && model.generatedRevision !== inputRevision(model);
}

export function hasRoute(model) {
  return !!(model.generated && Array.isArray(model.generated.route_waypoints)
            && model.generated.route_waypoints.length);
}

/** Boundary + a positive lane spacing → generation is possible. */
export function canGenerate(model) {
  return hasBoundary(model) && num(model.params && model.params.lane_spacing_m) > 0;
}

/** Upload is allowed only with a vehicle selected, a CURRENT (not outdated) generated route,
 *  and a passing validation. */
export function canUpload(model) {
  return model.vehicleId != null && hasRoute(model) && !isOutdated(model)
         && !!(model.validation && model.validation.ok)
         && model.upload.phase !== "uploading";
}

/** Derive the single planning state from the model. Never stored — always computed, so
 *  returning to an earlier step re-derives it correctly. */
export function planState(model) {
  const up = model.upload || {};
  if (up.phase === "uploading") return PLAN_STATES.UPLOADING;
  if (up.phase === "uploaded") return PLAN_STATES.UPLOADED;
  if (up.phase === "error") return PLAN_STATES.ERROR;
  if (!hasBoundary(model)) return PLAN_STATES.EMPTY;
  if (!hasRoute(model)) {
    return num(model.params && model.params.lane_spacing_m) > 0
      ? PLAN_STATES.CONFIGURED : PLAN_STATES.BOUNDARY_DEFINED;
  }
  if (isOutdated(model)) return PLAN_STATES.ROUTE_OUTDATED;
  if (model.validation && model.validation.ok) return PLAN_STATES.VALID;
  return PLAN_STATES.ROUTE_GENERATED;
}

/** Human-facing description of each state — the operator always sees WHY the page is where
 *  it is, and what unblocks the next step. */
export const PLAN_STATE_LABEL = {
  EMPTY: "Draw the survey boundary to begin.",
  BOUNDARY_DEFINED: "Boundary defined — set the lane spacing to configure the survey.",
  CONFIGURED: "Configured — generate the survey route.",
  ROUTE_OUTDATED: "Route is outdated — an input changed. Regenerate before uploading.",
  ROUTE_GENERATED: "Route generated — validate it before uploading.",
  VALID: "Validated — ready to finish & upload.",
  UPLOADING: "Uploading the mission to the vehicle…",
  UPLOADED: "Mission uploaded and verified.",
  ERROR: "Upload failed — the plan is preserved; review and retry.",
};

// ── applying edits: any change to a generation input clears validation and (implicitly, via
//    isOutdated) marks the route outdated. Validation is cleared because a route validated a
//    moment ago is not evidence about a route the operator just changed the inputs for.
function invalidateValidation(model) {
  return { ...model, validation: null };
}

/** Set the whole boundary (or clear it with null). Clearing the boundary also drops no-go
 *  zones (they require a boundary) and any generated route. */
export function setBoundary(model, ring) {
  if (ring == null) {
    return invalidateValidation({ ...model, boundary: null, noGoZones: [], generated: null,
                                  generatedRevision: null });
  }
  return invalidateValidation({ ...model, boundary: closeRing(ring) });
}
export function setHome(model, pt) { return invalidateValidation({ ...model, home: pt }); }
/** Approach waypoints — the operator-approved route INTO the survey (A1, A2, ...). Any edit
 *  invalidates a generated route (via isOutdated), exactly like a geometry change. */
export function setApproach(model, pts) { return invalidateValidation({ ...model, approach: pts || [] }); }
/** Return waypoints — the operator-approved route OUT of the survey toward home (R1, R2, ...). */
export function setReturns(model, pts) { return invalidateValidation({ ...model, returns: pts || [] }); }
export function setRouteStart(model, mode) {
  return invalidateValidation({ ...model, routeStartMode: ROUTE_START_MODES.includes(mode) ? mode : "planning_home" });
}
/** Copy the approach waypoints into the return list in REVERSE order (A3→A2→A1 becomes the
 *  return route). Deliberately explicit — the backend never auto-reverses; the operator asks
 *  for this convenience and the result stays fully editable. */
export function reversedApproach(model) {
  return setReturns(model, [...(model.approach || [])].reverse().map((p) => [p[0], p[1]]));
}
export function setParam(model, key, value) {
  return invalidateValidation({ ...model, params: { ...model.params, [key]: value } });
}

/** Record a fresh generation result, stamping the revision it was generated from. */
export function applyGenerated(model, result) {
  return { ...model, generated: result, generatedRevision: inputRevision(model), validation: null };
}
export function applyValidation(model, result) {
  return { ...model, validation: result };
}

/** Reset ALL planning geometry, generated route, validation and unsaved state — the Clear
 *  action. Keeps nothing except a fresh empty model (vehicle re-selection is the caller's
 *  choice, mirroring the roster dock). */
export function clearModel() { return emptyModel(); }

/** True when Clear should ask for confirmation — any geometry or generated data exists. */
export function hasUnsavedWork(model) {
  return hasBoundary(model) || model.noGoZones.length > 0 || model.home != null
         || (model.approach && model.approach.length > 0)
         || (model.returns && model.returns.length > 0) || !!model.generated;
}

// ── drafts ───────────────────────────────────────────────────────────────────────────────
/** Serialize a model into a draft payload (an editable planning document, NOT an uploaded
 *  mission). Preserves everything the task requires: vehicle, boundary, clearance, no-go
 *  zones, spacing, angles, dual-pass, home, transit, the generated route+metadata, and both
 *  revisions so a loaded draft knows whether its route is still current. */
export function toDraft(model, name) {
  return {
    name: name || null,
    vehicle_id: model.vehicleId,
    state: planState(model),
    plan: {
      boundary: model.boundary,
      no_go_zones: model.noGoZones,
      home: model.home,
      approach: model.approach,
      returns: model.returns,
      route_start_mode: model.routeStartMode,
      params: model.params,
      generated: model.generated,
      generated_revision: model.generatedRevision,
      input_revision: inputRevision(model),
      validation: model.validation,
    },
  };
}

/** Rebuild a model from a stored draft. The zone sequence is restored past the highest
 *  existing id so new zones never collide with loaded ones. */
export function fromDraft(draft) {
  const d = (draft && draft.plan) || {};
  const zones = Array.isArray(d.no_go_zones) ? d.no_go_zones : [];
  let maxSeq = 0;
  for (const z of zones) {
    const m = /(\d+)$/.exec(z && z.id ? String(z.id) : "");
    if (m) maxSeq = Math.max(maxSeq, +m[1]);
  }
  // MIGRATION: old drafts stored `transit`; load it as the approach list so a saved plan
  // never breaks. The new `approach`/`returns` fields win when present.
  const approach = Array.isArray(d.approach) ? d.approach
                 : (Array.isArray(d.transit) ? d.transit : []);
  return {
    ...emptyModel(),
    vehicleId: draft && draft.vehicle_id != null ? draft.vehicle_id : null,
    boundary: d.boundary || null,
    noGoZones: zones,
    home: d.home || null,
    approach,
    returns: Array.isArray(d.returns) ? d.returns : [],
    routeStartMode: ROUTE_START_MODES.includes(d.route_start_mode) ? d.route_start_mode : "planning_home",
    params: { ...defaultParams(), ...(d.params || {}) },
    generated: d.generated || null,
    generatedRevision: d.generated_revision || null,
    validation: d.validation || null,
    _zoneSeq: maxSeq,
  };
}

// ── upload bridge: a generated route → the SAME mission-contract-v1 upload params a pasted
//    mission uses (lib/mission-upload.js). No second contract, no second hash path — the
//    generated route_waypoints are already {latitude, longitude, loiter_time_s}. ──────────
/** Turn a generated route into MISSION_UPLOAD params, or null if there is no usable route.
 *  Routed through parseMission so the exact same validation a pasted file gets applies to a
 *  generated one — a generated route is not trusted more than a typed one. */
export function uploadParamsFromModel(model) {
  if (!hasRoute(model)) return null;
  const parsed = parseMission({
    contract_version: "mission-contract-v1",
    waypoints: model.generated.route_waypoints,
  });
  return parsed.ok ? missionUploadParams(parsed) : null;
}

/** The body for POST /api/missions/finalize: the FULL generated operator-survey-plan-v1
 *  package plus the target vehicle. Finalize stores the immutable original mission record
 *  (segments, planning inputs, navigable geometry, execution order) AND creates the
 *  unchanged MISSION_UPLOAD command in one call — so the Operator no longer loses the
 *  geometry after flattening it for the Pixhawk. Returns null when there is no route. */
export function finalizePayload(model) {
  if (!hasRoute(model) || model.vehicleId == null) return null;
  return { vehicle_id: model.vehicleId, mission_package: model.generated, confirm: true };
}

/** The mission identity to show after a successful finalized upload. */
export function missionIdentity(model) {
  const g = model.generated || {};
  const m = model.upload && model.upload.result ? model.upload.result : {};
  return {
    missionId: m.missionId || null,
    revision: m.revision != null ? m.revision : 0,
    hash: m.hash || g.route_hash || null,
    waypoints: (g.metrics && g.metrics.waypoint_count) || (g.route_waypoints || []).length,
  };
}

/** Approximate planar area (m²) of a lng/lat ring, for the LIVE left-panel readout before a
 *  route is generated. Labeled approximate in the UI; the authoritative geodesic area comes
 *  back in the generate metrics. Equirectangular shoelace around the ring's own latitude. */
export function approxAreaM2(ring) {
  if (!ringIsValid(ring)) return 0;
  const r = closeRing(ring);
  const latRef = r.reduce((s, p) => s + p[1], 0) / r.length;
  const mPerDegLat = 111320;
  const mPerDegLng = 111320 * Math.cos((latRef * Math.PI) / 180);
  let area = 0;
  for (let i = 0; i < r.length - 1; i++) {
    const x1 = r[i][0] * mPerDegLng, y1 = r[i][1] * mPerDegLat;
    const x2 = r[i + 1][0] * mPerDegLng, y2 = r[i + 1][1] * mPerDegLat;
    area += x1 * y2 - x2 * y1;
  }
  return Math.abs(area) / 2;
}
