// fleet-plan.js — pure, DOM-free state model for FLEET Mission planning on the Plan page.
// Unit-tested (tests/fleet-plan.test.mjs). No DOM, no network. It is the fleet counterpart to
// lib/planning.js: the ONE place that holds fleet selection + per-vehicle config, decides when a
// generated fleet plan is stale, builds the backend request, and runs the multi-vehicle upload
// orchestration (per-vehicle progress, partial failure, retry).
//
// The SHARED survey geometry (boundary, no-go zones, spacing, angle, dual pass) is reused verbatim
// from the single-vehicle planning model — a fleet does NOT redraw a separate survey area. This
// module layers fleet-specific state ON TOP of that geometry, exactly as fleet_planning.py layers
// allocation on top of planning.py.
//
// COORDINATE CONVENTION: [lng, lat], identical to lib/planning.js.

export const PLANNING_MODES = ["single", "fleet"];
export const DEFAULT_FLEET_SPEED_MPS = 1.0;         // matches backend DEFAULT_PLANNING_SPEED_MPS
export const DEFAULT_FLEET_SEPARATION_M = 10;
export const MIN_FLEET_VEHICLES = 2;
export const BALANCE_METRICS = ["estimated_duration", "distance"];

// Stable, visually distinct per-vehicle colours. Deliberately avoids red (no-go/errors), the
// shoreline/navigable green and the survey-boundary blue used by shared geometry.
export const FLEET_COLOURS = ["#4C8DFF", "#A78BFA", "#3ECF8E", "#F2A93B", "#22C3E6", "#E36AC7"];

// Fleet-wide upload status (derived, never stored authoritatively).
export const FLEET_UPLOAD_STATES = {
  NOT_STARTED: "NOT_STARTED", READY: "READY", UPLOADING: "UPLOADING",
  PARTIALLY_UPLOADED: "PARTIALLY_UPLOADED", VERIFYING: "VERIFYING",
  VERIFIED: "VERIFIED", FAILED: "FAILED", STALE: "STALE",
};
// Per-vehicle upload status.
export const VEHICLE_UPLOAD_STATES = {
  PENDING: "PENDING", UPLOADING: "UPLOADING", UPLOADED: "UPLOADED",
  VERIFYING: "VERIFYING", VERIFIED: "VERIFIED", FAILED: "FAILED", SKIPPED: "SKIPPED",
  STALE: "STALE",
};

function num(v) { return typeof v === "number" && Number.isFinite(v) ? v : null; }
function round7(v) { return Math.round(v * 1e7) / 1e7; }

/** A fresh fleet extension. Held ALONGSIDE the single-vehicle planning model (which owns the
 *  shared geometry); this object owns only the fleet-specific state. */
export function emptyFleet() {
  return {
    selectedVehicleIds: [],          // stable selection order
    vehicleConfig: {},               // id -> { colour, home, homeSource, survey_speed_mps, speedIsDefault, include }
    minimumFleetSeparationM: DEFAULT_FLEET_SEPARATION_M,
    balanceMetric: "estimated_duration",
    manualAssignments: null,         // { lineId: vehicleId } | null
    generated: null,                 // backend fleet plan
    generatedRevision: null,         // fleetInputRevision at generation
    validation: null,                // backend/last fleet validation
    upload: emptyUpload(),
  };
}

function emptyUpload() {
  return { fleetStatus: FLEET_UPLOAD_STATES.NOT_STARTED, vehicles: {}, startedAt: 0 };
}

/** Default per-vehicle config, colour picked from the palette by selection order. */
function defaultVehicleConfig(fleet, id, seed) {
  const idx = fleet.selectedVehicleIds.length % FLEET_COLOURS.length;
  return {
    colour: FLEET_COLOURS[idx],
    home: (seed && seed.home) || null,
    homeSource: seed && seed.home ? "vehicle" : null,   // "vehicle" (reported) | "operator"
    survey_speed_mps: DEFAULT_FLEET_SPEED_MPS,
    speedIsDefault: true,
    include: true,
  };
}

/** Toggle a vehicle in/out of the fleet selection. Adding seeds a default config (optionally
 *  using the vehicle's reported home as the initial planning home). Deriving the count from the
 *  selection — never a separate numeric input. Removing drops its config and clears a stale plan. */
export function toggleVehicle(fleet, id, seed) {
  id = String(id);
  if (fleet.selectedVehicleIds.includes(id)) {
    const ids = fleet.selectedVehicleIds.filter((x) => x !== id);
    const cfg = { ...fleet.vehicleConfig };
    delete cfg[id];
    return invalidateFleet({ ...fleet, selectedVehicleIds: ids, vehicleConfig: cfg });
  }
  const cfg = { ...fleet.vehicleConfig, [id]: defaultVehicleConfig(fleet, id, seed) };
  return invalidateFleet({ ...fleet, selectedVehicleIds: [...fleet.selectedVehicleIds, id], vehicleConfig: cfg });
}

export function isSelected(fleet, id) { return fleet.selectedVehicleIds.includes(String(id)); }
export function selectedCount(fleet) { return fleet.selectedVehicleIds.length; }

/** Set a vehicle's planning home (operator-chosen — never silently overwritten by telemetry). */
export function setVehicleHome(fleet, id, pt) {
  return updateConfig(fleet, id, { home: pt, homeSource: "operator" });
}
/** Adopt the vehicle's reported home ONLY when the operator has not already picked one. */
export function useReportedHome(fleet, id, pt) {
  const c = fleet.vehicleConfig[String(id)];
  if (!c || c.homeSource === "operator" || !pt) return fleet;
  return updateConfig(fleet, id, { home: pt, homeSource: "vehicle" });
}
export function setVehicleSpeed(fleet, id, mps) {
  const v = num(mps);
  return updateConfig(fleet, id, { survey_speed_mps: v == null ? DEFAULT_FLEET_SPEED_MPS : v,
                                   speedIsDefault: v == null });
}
export function setVehicleColour(fleet, id, colour) { return updateConfig(fleet, id, { colour }); }
export function setSeparation(fleet, m) {
  const v = num(m);
  return invalidateFleet({ ...fleet, minimumFleetSeparationM: v == null || v <= 0 ? DEFAULT_FLEET_SEPARATION_M : v });
}
export function setBalanceMetric(fleet, metric) {
  return invalidateFleet({ ...fleet, balanceMetric: BALANCE_METRICS.includes(metric) ? metric : "estimated_duration" });
}

function updateConfig(fleet, id, patch) {
  id = String(id);
  const cur = fleet.vehicleConfig[id];
  if (!cur) return fleet;
  return invalidateFleet({ ...fleet, vehicleConfig: { ...fleet.vehicleConfig, [id]: { ...cur, ...patch } } });
}

/** Any generation-affecting change clears validation and (via isFleetOutdated) marks the plan
 *  stale — a plan validated a moment ago is not evidence about one the inputs just changed. */
function invalidateFleet(fleet) { return { ...fleet, validation: null }; }

export function vehicleConfig(fleet, id) { return fleet.vehicleConfig[String(id)] || null; }
export function everyHomeSet(fleet) {
  return fleet.selectedVehicleIds.length > 0
    && fleet.selectedVehicleIds.every((id) => {
      const c = fleet.vehicleConfig[id];
      return c && Array.isArray(c.home) && num(c.home[0]) != null && num(c.home[1]) != null;
    });
}

/** Fleet generation needs >= 2 vehicles, every home set, a boundary and a positive lane spacing.
 *  `geom` is the single-vehicle planning model (shared geometry). */
export function canGenerateFleet(fleet, geom) {
  return selectedCount(fleet) >= MIN_FLEET_VEHICLES && everyHomeSet(fleet)
    && !!(geom && geom.boundary && geom.boundary.length >= 4)
    && num(geom.params && geom.params.lane_spacing_m) > 0;
}

/** The POST /api/planning/fleet/generate request body — the single place the fleet input shape
 *  is defined, so generate and validate can never disagree. */
export function fleetPlanningBody(fleet, geom) {
  const p = (geom && geom.params) || {};
  return {
    boundary: geom.boundary,
    shoreline_clearance_m: num(p.shoreline_clearance_m) || 0,
    lane_spacing_m: num(p.lane_spacing_m),
    primary_angle_deg: ((num(p.primary_angle_deg) || 0) % 360 + 360) % 360,
    dual_pass: !!p.dual_pass,
    secondary_angle_deg: num(p.secondary_angle_deg),
    minimum_fleet_separation_m: fleet.minimumFleetSeparationM,
    balance_metric: fleet.balanceMetric,
    no_go_zones: (geom.noGoZones || []).map((z) => z.ring),
    manual_assignments: fleet.manualAssignments,
    vehicles: fleet.selectedVehicleIds.map((id) => {
      const c = fleet.vehicleConfig[id] || {};
      return { vehicle_id: id, vehicle_name: c.name || id, colour: c.colour,
               home: c.home, survey_speed_mps: c.survey_speed_mps };
    }),
  };
}

/** A stable revision over EVERY fleet-generation-affecting input (selection, per-vehicle home/
 *  speed, shared geometry, separation, balance, manual overrides). A stored generatedRevision
 *  that no longer matches is exactly "the fleet allocation is out of date". */
export function fleetInputRevision(fleet, geom) {
  const p = (geom && geom.params) || {};
  const ring = (r) => (r || []).map((pt) => [round7(pt[0]), round7(pt[1])]);
  const canonical = {
    b: ring(geom && geom.boundary),
    z: (geom && geom.noGoZones || []).map((zz) => ring(zz.ring)),
    c: num(p.shoreline_clearance_m) || 0,
    s: num(p.lane_spacing_m),
    a: ((num(p.primary_angle_deg) || 0) % 360 + 360) % 360,
    d: !!p.dual_pass,
    a2: num(p.secondary_angle_deg),
    sep: fleet.minimumFleetSeparationM,
    bal: fleet.balanceMetric,
    m: fleet.manualAssignments,
    v: fleet.selectedVehicleIds.map((id) => {
      const cc = fleet.vehicleConfig[id] || {};
      return [id, cc.home ? [round7(cc.home[0]), round7(cc.home[1])] : null, cc.survey_speed_mps];
    }),
  };
  return JSON.stringify(canonical);
}

export function isFleetOutdated(fleet, geom) {
  return !!fleet.generated && fleet.generatedRevision !== fleetInputRevision(fleet, geom);
}
export function hasFleetPlan(fleet) {
  return !!(fleet.generated && Array.isArray(fleet.generated.vehicles) && fleet.generated.vehicles.length);
}

/** Record a fresh fleet generation, stamping the revision it came from and marking any prior
 *  uploaded missions STALE (they belong to an older plan version). */
export function applyFleetGenerated(fleet, geom, result) {
  const upload = anyUploadInProgress(fleet) || anyVerified(fleet)
    ? staleUpload(fleet) : emptyUpload();
  return { ...fleet, generated: result, generatedRevision: fleetInputRevision(fleet, geom),
           validation: (result && result.validation) || null, upload };
}
export function applyFleetValidation(fleet, result) { return { ...fleet, validation: result }; }

// ── upload orchestration ───────────────────────────────────────────────────────────────────
const V = VEHICLE_UPLOAD_STATES;
const FS = FLEET_UPLOAD_STATES;

/** True when the fleet plan is safe to begin uploading: a current (not stale) plan whose fleet
 *  validation has no blocking errors. */
export function canUploadFleet(fleet, geom) {
  return hasFleetPlan(fleet) && !isFleetOutdated(fleet, geom)
    && !!(fleet.validation && fleet.validation.ok)
    && fleet.upload.fleetStatus !== FS.UPLOADING;
}

/** Begin (or re-begin) a fleet upload: every selected vehicle → PENDING. */
export function beginUpload(fleet) {
  const vehicles = {};
  for (const id of fleet.selectedVehicleIds) vehicles[id] = { status: V.PENDING, cmdId: null,
    missionId: null, hash: null, error: null };
  return { ...fleet, upload: { fleetStatus: FS.UPLOADING, vehicles, startedAt: Date.now() } };
}

/** Retry: only FAILED (or STALE) vehicles return to PENDING; VERIFIED missions are NOT touched. */
export function retryFailed(fleet) {
  const vehicles = { ...fleet.upload.vehicles };
  let any = false;
  for (const id of Object.keys(vehicles)) {
    if (vehicles[id].status === V.FAILED || vehicles[id].status === V.STALE) {
      vehicles[id] = { ...vehicles[id], status: V.PENDING, error: null };
      any = true;
    }
  }
  return any ? { ...fleet, upload: { ...fleet.upload, fleetStatus: FS.UPLOADING, vehicles } } : fleet;
}

/** The next vehicle id awaiting upload (sequential orchestration), or null when none remain. */
export function nextPendingVehicle(fleet) {
  return fleet.selectedVehicleIds.find((id) => (fleet.upload.vehicles[id] || {}).status === V.PENDING) || null;
}

/** Update ONE vehicle's upload record, then re-derive the fleet status. */
export function markVehicle(fleet, id, status, extra) {
  id = String(id);
  const cur = fleet.upload.vehicles[id] || {};
  const vehicles = { ...fleet.upload.vehicles, [id]: { ...cur, status, ...(extra || {}) } };
  const upload = { ...fleet.upload, vehicles };
  upload.fleetStatus = deriveFleetStatus({ ...fleet, upload });
  return { ...fleet, upload };
}

/** Derive the fleet-wide status from the per-vehicle records — never stored, always computed. */
export function deriveFleetStatus(fleet) {
  const ids = fleet.selectedVehicleIds;
  if (!ids.length) return FS.NOT_STARTED;
  // No upload has begun (no per-vehicle records) → NOT_STARTED, never a phantom "UPLOADING".
  if (!Object.keys(fleet.upload.vehicles || {}).length) return FS.NOT_STARTED;
  const st = ids.map((id) => (fleet.upload.vehicles[id] || {}).status || V.PENDING);
  if (st.some((s) => s === V.STALE)) return FS.STALE;
  if (st.every((s) => s === V.VERIFIED)) return FS.VERIFIED;
  if (st.some((s) => s === V.UPLOADING || s === V.VERIFYING || s === V.PENDING)) {
    // still working, unless everything left is terminal-failed
    if (st.every((s) => s === V.FAILED || s === V.VERIFIED || s === V.SKIPPED)) {
      return st.some((s) => s === V.VERIFIED) ? FS.PARTIALLY_UPLOADED : FS.FAILED;
    }
    return FS.UPLOADING;
  }
  // no in-flight work left
  const verified = st.filter((s) => s === V.VERIFIED).length;
  const failed = st.filter((s) => s === V.FAILED).length;
  if (verified === st.length) return FS.VERIFIED;
  if (verified > 0 && failed > 0) return FS.PARTIALLY_UPLOADED;
  if (failed === st.length) return FS.FAILED;
  return FS.PARTIALLY_UPLOADED;
}

function anyUploadInProgress(fleet) {
  return Object.values(fleet.upload.vehicles || {}).some(
    (v) => v.status === V.UPLOADING || v.status === V.VERIFYING || v.status === V.PENDING);
}
function anyVerified(fleet) {
  return Object.values(fleet.upload.vehicles || {}).some((v) => v.status === V.VERIFIED);
}
/** Mark VERIFIED/UPLOADED vehicles STALE (their mission belongs to an older plan version). */
function staleUpload(fleet) {
  const vehicles = {};
  for (const [id, v] of Object.entries(fleet.upload.vehicles || {})) {
    vehicles[id] = (v.status === V.VERIFIED || v.status === V.UPLOADED)
      ? { ...v, status: V.STALE } : { ...v, status: V.PENDING };
  }
  return { ...fleet, fleetStatus: FS.STALE, vehicles, startedAt: fleet.upload.startedAt };
}

/** The finalize payload for ONE vehicle's child mission — the SAME shape single-vehicle upload
 *  uses (reuses POST /api/missions/finalize). fleet metadata is carried on the package's
 *  planning_inputs; the executable route is unchanged and independently hashed. */
export function finalizePayloadForVehicle(fleet, vehiclePlan) {
  if (!vehiclePlan || !vehiclePlan.mission_package) return null;
  return {
    vehicle_id: vehiclePlan.vehicle_id,
    mission_package: vehiclePlan.mission_package,
    confirm: true,
    upload_context: "OPERATOR_REPLACEMENT",
    fleet_plan_id: fleet.generated && fleet.generated.fleet_plan_id,
    fleet_plan_version: fleet.generated && fleet.generated.fleet_plan_version,
  };
}

/** Fleet readiness for operator launch: a current valid plan, every required child uploaded AND
 *  verified. "Uploaded" alone is NOT ready. */
export function fleetReady(fleet, geom) {
  return hasFleetPlan(fleet) && !isFleetOutdated(fleet, geom)
    && !!(fleet.validation && fleet.validation.ok)
    && fleet.selectedVehicleIds.length >= MIN_FLEET_VEHICLES
    && fleet.selectedVehicleIds.every((id) => (fleet.upload.vehicles[id] || {}).status === V.VERIFIED);
}

/** Find a vehicle's plan in the generated fleet plan. */
export function vehiclePlan(fleet, id) {
  if (!hasFleetPlan(fleet)) return null;
  return fleet.generated.vehicles.find((v) => String(v.vehicle_id) === String(id)) || null;
}
