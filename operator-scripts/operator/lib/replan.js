// replan.js — the PURE logic layer for the replanning supervisory view. No DOM, no fetch.
//
// Everything here is deterministic derivation over Scout's own status/config bodies, so the
// Agent page (and the config/experiment controls) render Scout's truth without re-deciding
// anything. Scout owns the FSM, the energy decision and every vehicle action; this module
// only NORMALIZES what Scout reports and enforces the operator-side SAFETY SEQUENCING the
// task mandates (never let an active forced-return injection slide straight from dry-run into
// real execution). It is unit-tested in tests/replan.test.mjs with no browser.

// Scout's replanning FSM. Idle = monitoring (nothing in flight); terminal-failure = needs a
// controller rearm before it can run again; active = a transaction is mid-flight (config
// PATCH is rejected 409 while active, and real-execution must never be armed during one).
export const FSM_IDLE = ["MONITORING", "MONITORING_REVISED"];
export const FSM_TERMINAL = ["SAFE_HOLD", "SUSPENDED", "FAILED", "FALLBACK_RTL"];
export const FSM_ACTIVE_ORDER = [
  "HOLD_REQUESTED", "HOLD_CONFIRMED", "PLANNING", "VALIDATING",
  "UPLOAD_REQUESTED", "VERIFYING_REVISION", "RESUME_REQUESTED",
];

// The staged execution UX (task Section 5) maps Scout's TWO independent flags
// (autonomous_execution_enabled, dry_run) onto one legible ladder. The two flags are never
// coupled — this is only a presentation over them.
export const STAGE = {
  DISABLED: "DISABLED",
  DRY_RUN: "DRY_RUN_ENABLED",
  REAL: "REAL_EXECUTION_ENABLED",
};

export const PACKAGE_CONSISTENT = "PLANNING_PACKAGE_CONSISTENT";

const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const arr = (v) => (Array.isArray(v) ? v : []);

/** First present (non-null) value among several candidate field spellings. */
function first(obj, ...keys) {
  if (!isObj(obj)) return null;
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  return null;
}

/** True when the FSM state means a transaction is mid-flight (config/real-exec must wait). */
export function isTransactionActive(fsmState) {
  const s = String(fsmState || "").toUpperCase();
  return FSM_ACTIVE_ORDER.includes(s);
}
/** True when the FSM is in a terminal-failure state that a controller rearm/reset clears. */
export function isTerminal(fsmState) {
  return FSM_TERMINAL.includes(String(fsmState || "").toUpperCase());
}

/** Scout's two config flags → the staged execution label. autonomous_execution_enabled and
 *  dry_run are INDEPENDENT; this only reads them, it never couples them. */
export function executionStage(config) {
  const enabled = !!first(config, "autonomous_execution_enabled");
  const dryRun = !!first(config, "dry_run");
  if (!enabled) return STAGE.DISABLED;
  return dryRun ? STAGE.DRY_RUN : STAGE.REAL;
}

/** The Scout config PATCH that moves to a target stage — mapping the ladder back onto Scout's
 *  two independent flags WITHOUT touching anything else (RTL fallback is never auto-enabled). */
export function stagePatch(stage) {
  switch (stage) {
    case STAGE.DISABLED: return { autonomous_execution_enabled: false };
    case STAGE.DRY_RUN: return { autonomous_execution_enabled: true, dry_run: true };
    case STAGE.REAL: return { autonomous_execution_enabled: true, dry_run: false };
    default: throw new Error(`unknown execution stage: ${stage}`);
  }
}

/** The ordered list of reasons REAL execution must NOT be armed right now (task Section 6
 *  safety rule). An EMPTY list means the staged flow may proceed to real execution. This is
 *  the interlock that forbids: active forced-return injection → dry-run → real execution.
 *
 *  ctx: { injectionActive, transactionActive, packageConsistent, homeValid, authorityKnown } */
export function realExecutionBlockers(ctx = {}) {
  const blockers = [];
  if (ctx.injectionActive)
    blockers.push("Clear the active experiment injection first (a forced-return simulation is live)");
  if (ctx.transactionActive)
    blockers.push("A replanning transaction is active — reset/rearm the controller first");
  if (!ctx.packageConsistent)
    blockers.push("Planning package is not consistent on Scout");
  if (!ctx.homeValid)
    blockers.push("Home is not valid/verified");
  if (ctx.authorityKnown === false)
    blockers.push("Control authority status is not understood");
  return blockers;
}

/** Convenience: real execution is allowed only when there are zero blockers. */
export function canEnableRealExecution(ctx = {}) {
  return realExecutionBlockers(ctx).length === 0;
}

/** Normalize ONE Scout transition into the row the Recent Transitions list renders. Reads
 *  tolerant field spellings; the frontend never invents a transition Scout did not report. */
export function normalizeTransition(t) {
  if (!isObj(t)) return null;
  return {
    timestamp: first(t, "timestamp", "ts", "at"),
    from: first(t, "from", "from_state", "prev"),
    to: first(t, "to", "to_state", "state"),
    reason: first(t, "reason", "reason_code", "trigger"),
    transitionId: first(t, "transition_id", "id"),
    simulated: !!(first(t, "simulated") || first(t, "source") === "SIMULATED"),
  };
}

/** Normalize Scout's canonical replan status object into the sections the supervisory view
 *  renders. Every field is read defensively and left null when Scout does not emit it — a
 *  missing field reads "Unavailable", never a fabricated default. `result` is the api.js
 *  envelope ({ scout, supported, reachable, outcome }) OR Scout's body directly. */
export function normalizeReplanStatus(result) {
  const supported = result && result.supported !== false;
  const reachable = !result || result.reachable !== false;
  const s = isObj(result && result.scout) ? result.scout : (isObj(result) ? result : {});
  const fsmState = first(s, "fsm_state", "state");
  const energy = first(s, "energy_calculation", "energy");
  const geometry = first(s, "geometry_validation") || {};
  const decisionSimulated = !!(first(s, "simulated")
    || (isObj(first(s, "simulation_state")) && first(first(s, "simulation_state"), "active"))
    || first(s, "decision_source") === "SIMULATED");

  return {
    supported,
    reachable,
    present: supported && reachable && Object.keys(s).length > 0,
    execution: {
      enabled: first(s, "execution_enabled", "autonomous_execution_enabled"),
      dryRun: first(s, "dry_run"),
      mode: first(s, "mode"),
      obstacleExecutionEnabled: first(s, "obstacle_execution_enabled") === true,
    },
    decision: {
      decision: first(s, "decision", "current_decision"),
      reasonCodes: arr(first(s, "reason_codes")),
      reason: first(s, "reason", "decision_reason", "human_reason"),
      snapshotId: first(s, "snapshot_id"),
      energy,
      persistence: first(s, "trigger_persistence", "energy_persistence", "debounce"),
      simulated: decisionSimulated,
      simulationState: first(s, "simulation_state"),
      realBattery: first(s, "battery_percent", "real_battery_percent"),
    },
    transaction: {
      fsmState,
      currentStep: first(s, "current_step", "step"),
      transitionId: first(s, "transition_id"),
      revision: first(s, "revision"),
      strategy: first(s, "strategy"),
      retryCount: first(s, "retry_count"),
      cooldownS: first(s, "cooldown_s", "cooldown"),
      active: isTransactionActive(fsmState),
      terminal: isTerminal(fsmState),
      authority: first(s, "authority_status", "authority"),
      authorityBlockedRecommendation: first(s, "authority_blocked_recommendation"),
      lastError: first(s, "last_error"),
      fallback: first(s, "fallback_state", "fallback"),
    },
    missionRevision: {
      originalHash: first(s, "original_mission_hash", "original_route_hash"),
      revisedHash: first(s, "revised_mission_hash", "revised_route_hash"),
      revision: first(s, "revision"),
      preservedCount: first(s, "preserved_waypoint_count"),
      removedCount: first(s, "removed_waypoint_count"),
      insertedCount: first(s, "inserted_waypoint_count"),
      revisedCount: first(s, "revised_waypoint_count"),
      validationResult: first(s, "validation_result"),
      uploadResult: first(s, "upload_result"),
      readbackResult: first(s, "readback_result"),
    },
    package: {
      summary: first(s, "package_summary"),
      consistency: first(s, "package_consistency"),
      consistent: first(s, "package_consistency") === PACKAGE_CONSISTENT,
      geometry: isObj(geometry) ? geometry : {},
    },
    transitions: arr(first(s, "transition_history", "transitions"))
      .map(normalizeTransition).filter(Boolean),
  };
}

/** Human label for a scout_replan write outcome (accepted/rejected/unknown/unavailable/
 *  unsupported), for a status pill. Unknown is deliberately NOT "failed". */
export function outcomeLabel(outcome) {
  return ({
    accepted: "Accepted",
    rejected: "Rejected",
    unknown: "Unknown — reconcile",
    unavailable: "Unavailable",
    unsupported: "Not supported",
  })[outcome] || "—";
}

/** Build the explicit experiment-injection payload from the operator's inputs, dropping empty
 *  fields so at least one real override is sent (target_vehicle is forced server-side). */
export function injectionPayload({ forceSafeReturn, energyMarginPercent, batteryPercent, durationS } = {}) {
  const body = {};
  if (forceSafeReturn) body.force_safe_return = true;
  if (energyMarginPercent !== undefined && energyMarginPercent !== null && energyMarginPercent !== "")
    body.energy_margin_percent = Number(energyMarginPercent);
  if (batteryPercent !== undefined && batteryPercent !== null && batteryPercent !== "")
    body.battery_percent = Number(batteryPercent);
  if (durationS !== undefined && durationS !== null && durationS !== "")
    body.duration_s = Number(durationS);
  return body;
}

/** True when an injection payload carries at least one override (the backend requires it). */
export function injectionHasOverride(payload) {
  return isObj(payload) && Object.keys(payload).length > 0;
}

// ── Map model (task Section 8) ──────────────────────────────────────────────────────────
// Derive, from Scout's status + the operator's missions, the ORDERED set of route layers the
// map should draw and the geometry-status badges it should show — WITHOUT inventing geometry.
// The consistency rule is load-bearing: mission id / revision / hash decide which single route
// is authoritative as "active", so the map never draws two contradictory active routes at once.
export const MAP_STYLE = {
  original: { role: "reference", emphasis: "subdued", dash: null },
  active: { role: "active", emphasis: "clear", dash: null },
  revised: { role: "revised", emphasis: "emphasized", dash: null },
  connector: { role: "connector", emphasis: "emphasized", dash: "dashed" },
};

/** normalized status (from normalizeReplanStatus) + { original, active } mission records
 *  ({ route_hash / route_waypoints }) → the map model. Layers only include geometry we
 *  actually have; a revised route with no waypoints is reported as available:false, never drawn
 *  from a fabricated path. `contradiction` is true when the live Pixhawk route disagrees with
 *  the revision Scout says is current — the map must resolve to ONE authoritative active route. */
export function replanMapModel(normStatus, { original = null, active = null } = {}) {
  const S = normStatus || normalizeReplanStatus(null);
  const rev = S.missionRevision || {};
  const t = S.transaction || {};
  const layers = [];

  if (original && (arr(original.route_waypoints).length || original.route_hash)) {
    layers.push({ kind: "original", ...MAP_STYLE.original, hash: original.route_hash,
      revision: original.mission_revision != null ? original.mission_revision : 0,
      waypoints: arr(original.route_waypoints) });
  }
  const activeHash = active ? (active.route_content_hash || active.route_hash) : null;
  if (active && (arr(active.waypoints).length || activeHash)) {
    layers.push({ kind: "active", ...MAP_STYLE.active, hash: activeHash,
      waypoints: arr(active.waypoints) });
  }

  // A revision exists once Scout reports a revised hash / revision > 0. We only draw the revised
  // route when Scout actually supplies its geometry; otherwise it is metadata (hash/revision).
  const hasRevision = !!(rev.revisedHash || (rev.revision != null && Number(rev.revision) > 0));
  const revisedWaypoints = arr(rev.revisedWaypoints || (S.package && S.package.summary && S.package.summary.revised_route));
  const revisedAvailable = hasRevision && revisedWaypoints.length > 0;
  if (revisedAvailable) {
    layers.push({ kind: "revised", ...MAP_STYLE.revised, hash: rev.revisedHash,
      revision: rev.revision, strategy: t.strategy, waypoints: revisedWaypoints });
  }

  // Contradiction: Scout reports a current revised route, but the live Pixhawk route still
  // hashes to something else (or to the original). The map resolves to the revised hash as the
  // authoritative one and flags that the readback has not caught up — never draws both as active.
  const contradiction = !!(hasRevision && rev.revisedHash && activeHash && activeHash !== rev.revisedHash);
  const authoritativeActiveHash = hasRevision && rev.revisedHash ? rev.revisedHash : activeHash;

  const g = (S.package && S.package.geometry) || {};
  const geometry = {
    boundaryAvailable: g.boundary_available === true,
    boundaryChecked: g.boundary_checked === true,
    noGoAvailable: g.no_go_available === true,
    noGoChecked: g.no_go_checked === true,
    connectorProvenSafe: g.connector_proven_safe === true,
    shorelineClearanceScalarOnly: g.shoreline_clearance_available !== true,
    boundaryLimitation: g.boundary_available !== true,
  };

  return {
    layers,
    revision: rev.revision,
    strategy: t.strategy,
    revisedAvailable,
    contradiction,
    authoritativeActiveHash,
    geometry,
    // The map must NOT draw an obstacle layer — obstacle replanning is disabled by Scout.
    obstacleLayer: false,
  };
}
