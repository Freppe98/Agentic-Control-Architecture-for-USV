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
      // Scout's decision_policy ActionRequest — the FINAL Scout implementation added this to
      // replan_controller.status(), so it is published HERE, on the canonical replan status
      // body, and nowhere else. Read from this field alone: never from mission-execution's
      // `risk` block, never from a speculative top-level field, and never inferred from
      // `decision`, `reason_codes` or `fsm_state`. See actionRequestView below.
      actionRequest: first(s, "action_request"),
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
    // ── The safe-return TRIGGER, and whether its generation has been spent ────────────────
    // Scout no longer retries the same condition when a cooldown expires. A replan attempt —
    // successful or failed — CONSUMES the trigger generation it ran for, and another attempt
    // needs an explicitly NEW generation (clear + reapply the injection, rearm the controller,
    // or a new original mission execution). So "trigger still active" and "another attempt is
    // coming" are now different facts, and the UI must stop implying the second from the first.
    // Scout's ACTION REQUEST — the third of the four independent layers (risk → advice →
    // action request → FSM). It is read TOLERANTLY and never fabricated: a Scout that does not
    // emit one leaves `reported:false`, and the UI must say "not reported", never "NONE".
    // "NONE" is a claim Scout makes; silence is not the same claim.
    actionRequest: {
      reported: ["action_request", "requested_action", "operator_action_request"]
        .some((k) => s[k] !== undefined),
      code: first(s, "action_request", "requested_action", "operator_action_request"),
    },
    trigger: {
      active: first(s, "trigger_active"),
      generation: first(s, "trigger_generation"),
      consumedGeneration: first(s, "consumed_trigger_generation"),
      consumed: first(s, "trigger_consumed"),
      terminalReason: first(s, "terminal_reason"),
      reported: ["trigger_active", "trigger_generation", "consumed_trigger_generation",
        "trigger_consumed", "terminal_reason"].some((k) => s[k] !== undefined),
    },
    missionRevision: {
      originalHash: first(s, "original_mission_hash", "original_route_hash"),
      revisedHash: first(s, "revised_mission_hash", "revised_route_hash"),
      revision: first(s, "revision"),
      preservedCount: first(s, "preserved_waypoint_count"),
      removedCount: first(s, "removed_waypoint_count"),
      insertedCount: first(s, "inserted_waypoint_count"),
      revisedCount: first(s, "revised_waypoint_count"),
      // The ORIGINAL route's waypoint count, so the revised count has something to be read
      // against. Absent when Scout does not report it — never back-filled from the operator's
      // own record, which would make an agreement look proven when it was assumed.
      originalCount: first(s, "original_waypoint_count", "original_route_count"),
      strategy: first(s, "strategy"),
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

// ── Scout's decision_policy ACTION REQUEST → the operator's compact word ───────────────────
// Published on Scout's canonical replan status (`GET /agent/replan/status` → `action_request`),
// alongside `current_decision` / `reason_codes` / `fsm_state` / `current_step` / `strategy` — NOT
// on mission-execution's `risk` block. DISPLAY ONLY, exactly like `decision`: never a button,
// never gates a control, never issues a command. Absent reads "—", never NONE — a Scout that has
// not (yet) started reporting this field has said nothing, which is different from Scout saying
// "no action requested".
export const ACTION_REQUEST_TEXT = {
  NONE: "NONE",
  REQUEST_RETURN_HOME: "REQUEST RETURN HOME",
  REQUEST_HOLD: "REQUEST HOLD",
};
export const ACTION_REQUEST_TONE = {
  NONE: "ok", REQUEST_RETURN_HOME: "warn", REQUEST_HOLD: "warn",
};

/**
 * Scout's decision_policy ActionRequest, read from `normalizeReplanStatus().decision.actionRequest`
 * — the authoritative source per Scout's final `replan_controller.status()` contract. Independent
 * of `decision`, `reason_codes` and `fsm_state`; this station never derives it from any of them,
 * and it never becomes a button or a command. An unrecognised code is shown exactly as Scout sent
 * it.
 *
 * @param norm normalizeReplanStatus() output
 * @returns {{ reported, code, text, tone, known }}
 */
export function actionRequestView(norm) {
  const S = norm || normalizeReplanStatus(null);
  const code = (S.decision && S.decision.actionRequest) || null;
  if (!code) return { reported: false, code: null, text: "—", tone: "idle", known: false };
  const up = String(code).toUpperCase();
  const known = Object.prototype.hasOwnProperty.call(ACTION_REQUEST_TEXT, up);
  return {
    reported: true, code: up, known,
    text: known ? ACTION_REQUEST_TEXT[up] : up,
    tone: known ? ACTION_REQUEST_TONE[up] : "idle",
  };
}

/**
 * THE TRIGGER LATCH: is another automatic attempt actually coming, or is this one spent?
 *
 * The distinction is the whole point. A safe-return trigger can remain ACTIVE (the condition
 * that raised it has not gone away) while the attempt it raised has already run and FAILED. The
 * cooldown counter keeps ticking, and a UI that shows a cooldown next to an active trigger tells
 * the operator "it will try again shortly" — which is now false. Scout consumes the trigger
 * generation on every attempt, success or failure, and will not retry the same condition. Another
 * attempt requires an explicitly NEW generation: clear and reapply the experiment injection,
 * rearm the replanning controller, or run a new original mission.
 *
 * `consumed` is true when Scout says so outright, or when the consumed generation has caught up
 * with the current one. A missing generation pair is not evidence either way, so it stays false.
 *
 * @param norm normalizeReplanStatus() output
 * @returns {{ reported, active, consumed, generation, consumedGeneration, attempt,
 *             terminalReason, willRetryAutomatically, rearmRequired, headline, detail }}
 */
export function triggerLatch(norm) {
  const S = norm || normalizeReplanStatus(null);
  const t = S.trigger || {};
  const tx = S.transaction || {};
  const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
  const generation = num(t.generation);
  const consumedGeneration = num(t.consumedGeneration);
  const active = t.active === true;
  const consumed = t.consumed === true
    || (generation !== null && consumedGeneration !== null && consumedGeneration >= generation);
  // "Another automatic attempt is coming" needs ALL of: a live trigger, an unspent generation,
  // and a controller that is not sitting in a terminal state. Anything less is not a promise the
  // station may make on Scout's behalf.
  const willRetryAutomatically = active && !consumed && !tx.terminal;
  const rearmRequired = (active && consumed) || (!!tx.terminal && active);
  const attempt = consumedGeneration !== null ? consumedGeneration
    : (generation !== null ? generation : null);

  let headline = null;
  if (active && consumed) {
    headline = "SAFE RETURN TRIGGER — ACTIVE, generation consumed";
  } else if (active) {
    headline = "SAFE RETURN TRIGGER — ACTIVE";
  } else if (t.reported) {
    headline = "No safe-return trigger active";
  }
  const detail = [
    attempt !== null ? `Attempt ${attempt} consumed` : null,
    t.terminalReason ? `Outcome: ${t.terminalReason}` : (tx.fsmState && tx.terminal
      ? `Outcome: ${tx.fsmState}` : null),
    rearmRequired ? "Re-arm required for another attempt" : null,
  ].filter(Boolean).join(" · ") || null;

  return { reported: !!t.reported, active, consumed, generation, consumedGeneration, attempt,
    terminalReason: t.terminalReason || null, willRetryAutomatically, rearmRequired,
    headline, detail };
}

/**
 * Whether a COOLDOWN may be presented as a countdown to another attempt.
 *
 * Scout still reports `cooldown_s`, and it is still a real number — but after the generation is
 * consumed it no longer counts down to anything. Showing it as a pending retry is the specific
 * misreading this guard exists to prevent.
 */
export function cooldownView(norm) {
  const S = norm || normalizeReplanStatus(null);
  const seconds = S.transaction && typeof S.transaction.cooldownS === "number"
    ? S.transaction.cooldownS : null;
  const latch = triggerLatch(S);
  if (seconds === null) return { seconds: null, countsDownToRetry: false, text: null };
  if (!latch.willRetryAutomatically) {
    return { seconds, countsDownToRetry: false,
      text: `${seconds}s (not a pending retry — the trigger generation is consumed)` };
  }
  return { seconds, countsDownToRetry: true, text: `${seconds}s until the next attempt` };
}

// ── The revised-mission signal that wakes the Map overlay ────────────────────────────────
// When Scout replans and uploads a REVISED mission to the Pixhawk, the Map must show the new
// return route without the operator pressing Refresh — and without polling the full mission
// download on a timer, which is expensive over the link.
//
// So the signal below is a cheap, stable STRING derived from authoritative lifecycle evidence the
// station is already reading. It changes exactly when the route on the vehicle can have changed:
//
//   revised_mission_hash / revision   Scout's own identity for the revision it produced
//   readback_result VERIFIED          the revision is confirmed to be ON the flight controller
//   MONITORING_REVISED / return state the replan FSM has handed back onto a revised route
//   active_route_hash                 the mission-execution status' current route identity
//
// Unchanged evidence yields an unchanged signal, and the refresh tracker then does nothing. When
// NO evidence is present at all it returns `undefined` — the trigger stays dormant rather than
// firing on a fabricated value. Manual Refresh is unaffected either way.
const RETURN_FSM_STATES = new Set(["MONITORING_REVISED", "RESUME_REQUESTED", "VERIFYING_REVISION"]);

export function missionRevisionSignal({ replan = null, missionExecution = null,
  vehicle = null } = {}) {
  const parts = [];
  const push = (label, value) => {
    if (value === null || value === undefined || value === "") return;
    parts.push(`${label}:${value}`);
  };

  if (replan) {
    const S = replan && replan.missionRevision ? replan : normalizeReplanStatus(replan);
    const rev = S.missionRevision || {};
    push("rh", rev.revisedHash);
    push("rev", rev.revision);
    // Only a VERIFIED readback means the revision is actually on the flight controller. An
    // in-flight or failed one is not a reason to redraw.
    const readback = rev.readbackResult;
    const outcome = isObj(readback) ? first(readback, "outcome", "result", "state") : readback;
    if (String(outcome || "").toUpperCase() === "VERIFIED") push("rb", "VERIFIED");
    const fsm = String((S.transaction && S.transaction.fsmState) || "").toUpperCase();
    if (RETURN_FSM_STATES.has(fsm)) push("fsm", fsm);
  }

  if (missionExecution) {
    const body = isObj(missionExecution.scout) ? missionExecution.scout
      : (isObj(missionExecution.summary) ? missionExecution.summary : missionExecution);
    push("ah", first(body, "active_route_hash"));
    const rp = isObj(body.replanning) ? body.replanning : {};
    const fsm = String(first(rp, "fsm_state", "state") || "").toUpperCase();
    if (RETURN_FSM_STATES.has(fsm)) push("mxfsm", fsm);
  }

  if (vehicle) {
    const md = isObj(vehicle.mission_data) ? vehicle.mission_data : {};
    push("v", md.active_revision_id ?? md.active_route_hash ?? md.mission_changed_at
      ?? vehicle.active_revision_id ?? vehicle.route_hash);
  }

  return parts.length ? parts.join("|") : undefined;
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
    // The ORIGINAL planning constraints (no-go zones, navigable boundary, planned route, planning
    // home) taken from the immutable original mission record — the reference geometry the map must
    // keep showing after Scout replaces the Pixhawk mission. Empty/absent when no record was
    // supplied; never derived from any route.
    planning: originalPlanningGeometry(original),
    // The map must NOT draw an obstacle layer — obstacle replanning is disabled by Scout.
    obstacleLayer: false,
  };
}

// ══════════════════════════════════════════════════════════════════════════════════════════
// THE E2 EXPERIMENT'S REFERENCE GEOMETRY
// ══════════════════════════════════════════════════════════════════════════════════════════
//
// The E2 water experiment has to be VISUALLY INDISPUTABLE: the vehicle went around a no-go zone
// on the way out, Scout replanned a constrained safe return, and the revised route went around
// the SAME zone rather than straight through it. Once Scout uploads the revision, the Pixhawk
// carries only the return route — the outbound route and the obstacle are gone from the flight
// controller entirely. So the map has to keep the ORIGINAL planning geometry from a source that
// the replan cannot overwrite.
//
// That source is the operator's own immutable ACTIVE ORIGINAL MISSION RECORD (revision 0,
// GET /api/vehicles/{id}/missions/active-original). It already carries every input the map needs:
//
//     route_waypoints                   the approved outbound route  [{latitude, longitude}, …]
//     planning_inputs.no_go_zones       the AUTHORITATIVE no-go rings  [[[lng, lat], …], …]
//     planning_inputs.navigable_boundary   the shoreline-offset navigable area
//     planning_inputs.planning_home     the planned home/route origin  [lng, lat]
//     planning_inputs.shoreline_clearance_m   scalar metadata, NOT geometry
//     metrics.no_go_zone_count          the count the planner itself recorded
//
// TWO RULES ARE LOAD-BEARING HERE:
//
//   1. NO-GO GEOMETRY IS NEVER INFERRED. Not from the route's shape, not from a gap between
//      waypoints, not from where the vehicle turned. It comes from the planning inputs or it is
//      reported absent. A fabricated obstacle on an examiner's map would be the single worst
//      thing this station could draw.
//   2. AN ABSENT RECORD DEGRADES QUIETLY. `present:false` with empty lists — the active mission
//      still draws, and nothing is invented to fill the gap.
//
// Coordinate convention: planning inputs are stored [lng, lat] (the planner's GeoJSON-order
// rings); route waypoints are {latitude, longitude} objects. Everything below returns
// [lat, lng] pairs, which is what Leaflet takes. The two orders are NEVER guessed apart by
// magnitude — the record's own convention is followed exactly.

const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

/** One [lng, lat] planning point (or a {latitude,longitude} / {lat,lng} object) → [lat, lng]. */
function planningPoint(p) {
  if (Array.isArray(p) && p.length >= 2) {
    const lng = num(p[0]), lat = num(p[1]);
    return lat === null || lng === null ? null : [lat, lng];
  }
  if (isObj(p)) {
    const lat = num(p.latitude !== undefined ? p.latitude : p.lat);
    const lng = num(p.longitude !== undefined ? p.longitude : (p.lng !== undefined ? p.lng : p.lon));
    return lat === null || lng === null ? null : [lat, lng];
  }
  return null;
}

/** One route waypoint ({latitude,longitude} — or {lat,lng}) → [lat, lng]. */
function routePoint(w) {
  if (!isObj(w)) return Array.isArray(w) ? planningPoint(w) : null;
  const lat = num(w.latitude !== undefined ? w.latitude : w.lat);
  const lng = num(w.longitude !== undefined ? w.longitude : (w.lng !== undefined ? w.lng : w.lon));
  return lat === null || lng === null ? null : [lat, lng];
}

/**
 * One polygon entry → a [lat,lng] ring, or null when it carries no usable geometry.
 * Tolerates the spellings the planner and Scout each use for the same thing: a bare ring, a
 * `{polygon|ring|coordinates}` wrapper, or a GeoJSON Polygon. A ring needs 3 real points to be
 * a polygon at all — anything less is dropped rather than drawn as a degenerate shape.
 */
function polygonRing(entry) {
  let ring = entry;
  if (isObj(entry)) {
    ring = entry.polygon || entry.ring || entry.coordinates || entry.points || null;
    // GeoJSON Polygon: coordinates is a LIST OF RINGS; the first is the outer one.
    if (Array.isArray(ring) && Array.isArray(ring[0]) && Array.isArray(ring[0][0])) ring = ring[0];
  }
  if (!Array.isArray(ring)) return null;
  const pts = ring.map(planningPoint).filter(Boolean);
  return pts.length >= 3 ? pts : null;
}

function polygonList(value) {
  return arr(value).map(polygonRing).filter(Boolean);
}

/**
 * The immutable original mission record → the reference geometry the E2 map draws.
 *
 * @param record the active-original mission record (or null)
 * @returns {{ present, missionId, revision, routeHash, route, routeCount,
 *             noGoZones, noGoZoneCount, noGoReported, navigableBoundary, home,
 *             shorelineClearanceM, source }}
 *
 * `noGoReported` is the distinction the E2 preflight is built on: it is TRUE when the record
 * carries a no-go field at all (an explicit empty list is an answer — the planner looked and
 * recorded none) and FALSE when no such field exists (nobody has said anything). A count of 0
 * with `noGoReported:true` is exactly the bad experiment configuration we must catch before the
 * boat is in the water, and it is not the same fact as "the record predates the field".
 */
export function originalPlanningGeometry(record) {
  const empty = {
    present: false, missionId: null, revision: null, routeHash: null,
    route: [], routeCount: 0, noGoZones: [], noGoZoneCount: null, noGoReported: false,
    navigableBoundary: [], home: null, shorelineClearanceM: null,
    source: null,
  };
  if (!isObj(record)) return empty;
  // Some callers hold the endpoint envelope ({ ok, vehicle_id, mission }) rather than the record.
  const rec = isObj(record.mission) ? record.mission : record;
  if (!isObj(rec)) return empty;

  const inputs = isObj(rec.planning_inputs) ? rec.planning_inputs : {};
  const metrics = isObj(rec.metrics) ? rec.metrics : {};

  // NO-GO: planning inputs first (what the route was planned AGAINST), then the record's own
  // top-level copy. Never the route, never the segments, never the metrics count alone.
  const noGoRaw = inputs.no_go_zones !== undefined ? inputs.no_go_zones
    : (rec.no_go_zones !== undefined ? rec.no_go_zones : undefined);
  const noGoReported = noGoRaw !== undefined && noGoRaw !== null;
  const noGoZones = polygonList(noGoRaw);
  // The count is the RECORD's own count of the zones it planned against. Prefer the drawable
  // rings we actually resolved; fall back to the raw list length, then to the planner's metric,
  // so a zone we could not parse still counts as present rather than silently vanishing.
  let noGoZoneCount = null;
  if (noGoReported) noGoZoneCount = Math.max(noGoZones.length, arr(noGoRaw).length);
  else if (num(metrics.no_go_zone_count) !== null) noGoZoneCount = num(metrics.no_go_zone_count);

  const boundaryRaw = inputs.navigable_boundary !== undefined ? inputs.navigable_boundary
    : rec.navigable_geometry;
  const route = arr(rec.route_waypoints).map(routePoint).filter(Boolean);

  return {
    present: true,
    missionId: rec.mission_id != null ? rec.mission_id : null,
    revision: rec.mission_revision != null ? rec.mission_revision : null,
    routeHash: rec.route_hash != null ? rec.route_hash : null,
    route,
    routeCount: route.length,
    noGoZones,
    noGoZoneCount,
    // A record that carries no no-go field at all, but whose planner metric recorded a count,
    // has still "reported" the fact — the geometry is what is missing, not the answer.
    noGoReported: noGoReported || noGoZoneCount !== null,
    navigableBoundary: polygonList(boundaryRaw),
    home: planningPoint(inputs.planning_home),
    shorelineClearanceM: num(inputs.shoreline_clearance_m),
    source: "ACTIVE_ORIGINAL_MISSION_RECORD",
  };
}

// ── The compact map message for a safe-return replan ──────────────────────────────────────
// One short line, from real Scout state only, so the Map does not become a diagnostics page.
//
// WORDING RULE: the constrained route Scout builds and uploads is a SAFE RETURN. It is NOT
// "RTL". Native Pixhawk RTL is a different mechanism — a straight-line autopilot behaviour that
// knows nothing about no-go zones — and it is the FALLBACK, reached only via FALLBACK_RTL. The
// two must never share a word on this station: an examiner reading "RTL" over a replanned route
// would be told the exact opposite of what the experiment is demonstrating.
export const SAFE_RETURN_PHASE_TEXT = {
  HOLD_REQUESTED: "Safe return — holding position",
  HOLD_CONFIRMED: "Safe return — holding position",
  PLANNING: "Safe return — planning",
  VALIDATING: "Safe return — validating",
  UPLOAD_REQUESTED: "Safe return — uploading",
  VERIFYING_REVISION: "Safe return — verifying revision",
  RESUME_REQUESTED: "Safe return — resuming",
  MONITORING_REVISED: "Replanned safe return active",
  SAFE_HOLD: "Safe return stopped — holding",
  SUSPENDED: "Safe return suspended",
  FAILED: "Safe return failed",
  // The ONE place RTL is the right word: Scout has fallen back to the autopilot's own
  // return-to-launch, which is NOT the constrained route.
  FALLBACK_RTL: "Fell back to native Pixhawk RTL — route is NOT constrained",
};

/**
 * The Map's compact replan line, or null when there is nothing real to say.
 * @param norm normalizeReplanStatus() output
 * @returns {{ text, tone, fsmState, strategy }|null}
 */
export function safeReturnBanner(norm) {
  const S = norm || normalizeReplanStatus(null);
  if (!S.present) return null;
  const t = S.transaction || {};
  const fsm = String(t.fsmState || "").toUpperCase();
  const text = SAFE_RETURN_PHASE_TEXT[fsm];
  if (!text) return null;                       // MONITORING and anything unknown say nothing
  const tone = fsm === "MONITORING_REVISED" ? "ok"
    : isTerminal(fsm) ? "warn" : "caution";
  return { text, tone, fsmState: fsm, strategy: t.strategy || null };
}

// ══════════════════════════════════════════════════════════════════════════════════════════
// E2 PREFLIGHT — is the experiment CONFIGURED the way the run assumes?
// ══════════════════════════════════════════════════════════════════════════════════════════
//
// Read-only evidence, assembled from contracts the station already reads. It gates nothing and
// commands nothing: its whole job is to let a bad experiment configuration be seen BEFORE Start,
// rather than discovered from a recording afterwards.
//
// Every check is TRI-STATE. "PASS" and "FAIL" both require the evidence to have arrived;
// "UNKNOWN" means nobody has answered, and an unanswered check is never counted as a pass.
export const CHECK = { PASS: "PASS", FAIL: "FAIL", UNKNOWN: "UNKNOWN" };

/** The planned E2 has exactly ONE no-go polygon in the middle. Presence alone is not enough:
 *  `no_go_zones_present:true` with a count of 0 is the specific misconfiguration to catch. */
export const E2_EXPECTED_NO_GO_ZONES = 1;

const check = (key, label, state, value, detail) => ({ key, label, state, value, detail });
const passIf = (cond) => (cond === true ? CHECK.PASS : cond === false ? CHECK.FAIL : CHECK.UNKNOWN);

/**
 * The E2 preflight checklist.
 *
 * @param missionExecution mx.normalizeStatus() output (or null) — state / risk / energy
 * @param replanStatus     normalizeReplanStatus() output (or null) — FSM / action request
 * @param readiness        the backend replan-readiness body (or null) — mission & package evidence
 * @param packageVerdict   readinessLabel() output (or null) — the shared package verdict
 * @param planning         originalPlanningGeometry() output (or null) — the operator's own record
 * @param scoutNoGoZoneCount Scout's package-summary count, when the backend reports one
 * @param homeVerified     Scout's continuously reported Home verification (tri-state)
 * @returns {{ checks, ready, failed, unknown }}
 */
export function e2PreflightChecks({
  missionExecution = null, replanStatus = null, readiness = null, packageVerdict = null,
  planning = null, scoutNoGoZoneCount = null, homeVerified = null,
  expectedNoGoZones = E2_EXPECTED_NO_GO_ZONES,
} = {}) {
  const S = isObj(missionExecution) ? missionExecution : {};
  const R = replanStatus && replanStatus.transaction ? replanStatus : normalizeReplanStatus(replanStatus);
  const rd = isObj(readiness) ? readiness : null;
  const vm = rd && isObj(rd.vehicle_mission) ? rd.vehicle_mission : {};
  const pk = rd && isObj(rd.planning_package) ? rd.planning_package : {};
  // `planning` may be a RECORD or an already-derived model. The two are told apart by the model's
  // own distinctive key, NOT by `source` — an absent-record model is a perfectly valid model with
  // a null source, and re-normalizing one used to hand back `present:true` for a plan that does
  // not exist, which is the exact class of fabrication this module exists to prevent.
  const isModel = isObj(planning)
    && Object.prototype.hasOwnProperty.call(planning, "noGoReported");
  const geom = isModel ? planning : originalPlanningGeometry(planning);
  const risk = isObj(S.risk) ? S.risk : {};
  const energy = isObj(S.energy) ? S.energy : {};
  const checks = [];

  // 1. Scout's own mission-execution readiness — the state Start is offered from.
  const mxState = S.present === false ? null : (S.state != null ? String(S.state).toUpperCase() : null);
  checks.push(check("mission_execution", "Mission execution", mxState === null ? CHECK.UNKNOWN
    : passIf(mxState === "READY"), mxState || "not reported",
    "Scout's canonical mission-execution state. E2 starts from READY."));

  // 2. Which approved mission this is.
  const missionId = vm.mission_id || geom.missionId || null;
  checks.push(check("mission_id", "Mission id", missionId ? CHECK.PASS : CHECK.UNKNOWN,
    missionId || "not established",
    "The approved mission record the run is executing."));

  // 3. Route identity — the record's hash proven against the flight controller's read-back.
  // An unreachable read-back, or a readiness body that never arrived, is UNKNOWN: nobody has
  // answered. Only an actual mismatch reported by a reachable read-back is a FAIL.
  const routeIdentity = (rd === null || vm.readback_reachable === false
    || vm.readback_hash_match === undefined) ? null
    : (vm.readback_hash_match === true && vm.pixhawk_verified === true);
  checks.push(check("route_identity", "Route identity verified", passIf(routeIdentity),
    vm.readback_reachable === false ? "read-back unreachable"
      : vm.readback_hash_match === true ? (vm.pixhawk_verified ? "hash match · upload VERIFIED" : "hash match · upload not verified")
      : vm.readback_hash_match === false ? "read-back hash does NOT match the approved route" : "not reported",
    "The Pixhawk read-back hashes to the approved route AND the upload verified."));

  // 4. The planning package on Scout — the SHARED verdict, never a second opinion.
  const pkgState = packageVerdict && packageVerdict.state ? packageVerdict.state : null;
  checks.push(check("package", "Planning package", pkgState === null ? CHECK.UNKNOWN
    : passIf(pkgState === "READY"), pkgState ? pkgState.replace(/_/g, " ") : "not reported",
    (packageVerdict && (packageVerdict.detail || packageVerdict.text))
      || "Scout must hold the approved package for this exact mission."));

  // 5. Home. Scout's continuously reported verification is preferred; the readiness body's
  //    home_valid is the fallback when nobody has asked Scout directly.
  const home = homeVerified === true ? true
    : homeVerified === false ? false
    : (vm.home_valid === undefined ? null : !!vm.home_valid);
  checks.push(check("home", "Home", passIf(home),
    home === true ? "verified" : home === false ? "not verified" : "not reported",
    "The Start transaction sets and verifies Home; before Start this is information."));

  // 6/7/8/9. THE FOUR INDEPENDENT LAYERS. Each is read from its OWN Scout field and none is
  // derived from another — an FSM in HOLD_REQUESTED does not make the mission-level Advice HOLD,
  // and a CRITICAL risk does not by itself make an action request exist.
  // missionFeasible is TRI-STATE on the normalized status (bool3): null is "Scout said nothing",
  // and it must not collapse into "not feasible".
  const feasible = energy.missionFeasible === undefined ? null : energy.missionFeasible;
  checks.push(check("feasibility", "Mission feasibility", passIf(feasible),
    feasible === true ? "FEASIBLE" : feasible === false ? "NOT FEASIBLE" : "not reported",
    "Scout's energy verdict for completing the planned mission."));

  const level = risk.level ? String(risk.level).toUpperCase() : null;
  checks.push(check("risk", "Risk", level === null ? CHECK.UNKNOWN : passIf(level === "LOW"),
    level || "not reported", "Scout's GOVERNING risk level, read from risk.level alone."));

  const advice = risk.recommendation ? String(risk.recommendation).toUpperCase() : null;
  checks.push(check("advice", "Advice", advice === null ? CHECK.UNKNOWN
    : passIf(advice === "CONTINUE"), advice || "not reported",
    "Scout's advisory recommendation. Display only — it is never turned into a command, and the "
    + "replanning FSM never overwrites it."));

  const ar = R.actionRequest || {};
  const arCode = ar.code ? String(ar.code).toUpperCase() : null;
  checks.push(check("action_request", "Action request",
    !ar.reported ? CHECK.UNKNOWN : passIf(arCode === "NONE" || arCode === null),
    arCode || (ar.reported ? "NONE" : "not reported by Scout"),
    ar.reported ? "Scout's explicit operator-action request."
      : "This Scout build does not emit an action_request field on /agent/replan/status. The "
        + "station will not invent one — see the E2 report's missing-contract note."));

  const fsm = R.transaction && R.transaction.fsmState
    ? String(R.transaction.fsmState).toUpperCase() : null;
  checks.push(check("replan_fsm", "Replanning FSM", fsm === null ? CHECK.UNKNOWN
    : passIf(fsm === "MONITORING"), fsm || "not reported",
    "The replanning controller's own state. E2 starts from MONITORING."));

  // 10. THE EXPERIMENT'S OWN CONSTRAINT. Presence of the field is not enough — the planned E2 has
  //     exactly one no-go polygon, and a record that reports zero is a bad configuration to catch
  //     on the bench, not in the water.
  const count = geom.noGoZoneCount;
  checks.push(check("no_go_zones", `No-go zones (expected ${expectedNoGoZones})`,
    !geom.present || count === null ? CHECK.UNKNOWN : passIf(count === expectedNoGoZones),
    !geom.present ? "no original mission record"
      : count === null ? "no no-go field on the record" : String(count),
    "Read from the immutable original mission record's planning inputs "
    + "(planning_inputs.no_go_zones) — never inferred from the route's shape."));

  // 11. Scout's own copy of the same count, when the package summary reports one. A DISAGREEMENT
  //     here means Scout is planning against different constraints than the operator approved.
  const scoutCount = num(scoutNoGoZoneCount);
  if (scoutCount !== null || count !== null) {
    const agrees = scoutCount === null ? null : (count === null ? null : scoutCount === count);
    checks.push(check("no_go_zones_package", "No-go zones in Scout's package",
      scoutCount === null ? CHECK.UNKNOWN
        : agrees === null ? CHECK.UNKNOWN : passIf(agrees && scoutCount === expectedNoGoZones),
      scoutCount === null ? "not reported by Scout" : String(scoutCount),
      "Scout's planning-package summary count, compared with the approved record's."));
  }

  const failed = checks.filter((c) => c.state === CHECK.FAIL);
  const unknown = checks.filter((c) => c.state === CHECK.UNKNOWN);
  return { checks, ready: failed.length === 0 && unknown.length === 0, failed, unknown };
}
