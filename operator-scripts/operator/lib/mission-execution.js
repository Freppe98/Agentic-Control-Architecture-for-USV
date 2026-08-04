// mission-execution.js — the PURE logic layer for Scout's mission-execution lifecycle.
// No DOM, no fetch, no timers. Unit-tested in tests/mission-execution.test.mjs.
//
// SCOUT OWNS THE LIFECYCLE. Start is ONE Scout-side transaction (verified LOITER → set Home to
// the current launch position → verify Home → synchronize the planning package → verified AUTO →
// progression confirmation → RUNNING); Pause and Resume are likewise single Scout transactions.
// This module runs NO second FSM: it derives what the card renders strictly from Scout's own
// canonical status (`can_start` / `can_pause` / `can_resume` / `state` / `effective_state` /
// `active_operation_id`) and from the operation result Scout returned.
//
// Three rules are load-bearing, and each exists because its opposite is a lie the operator would
// act on:
//   1. The primary button is derived from STATUS ONLY — never from the previous label, never
//      from the last clicked action. A click does not change the button; Scout's next
//      authoritative status does.
//   2. HTTP 200 is not success. A 200 whose body carries `error` (or `accepted:false`) is a
//      vehicle-level FAILURE, shown with Scout's exact code and message.
//   3. Nothing is fabricated for an older Scout. A 404 on these routes means the lifecycle is
//      unsupported — not READY, not can_start, not a verified Home, not a completed hold.

// Scout's mission-execution states, verbatim and complete.
export const STATES = [
  "NOT_READY", "READY",
  "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED",
  "SETTING_HOME", "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
  "RUNNING",
  "PAUSE_REQUESTED", "PAUSED", "RESUME_REQUESTED",
  "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED", "COMPLETED_HOLD",
  "SUSPENDED", "FAILED",
];

// An OVERLAY, not a state: Scout reports effective_state=REPLANNING while its stored state is
// still RUNNING / PAUSED / another live state. The replanning controller owns the vehicle.
export const EFFECTIVE_REPLANNING = "REPLANNING";

// Scout is in flight: show progress, disable the primary control, predict nothing. This is every
// state that is not a RESTING one (NOT_READY / READY / RUNNING / PAUSED / COMPLETED_HOLD /
// SUSPENDED / FAILED) — the transaction steps AND the return phase, which is likewise not a state
// the operator may act on.
export const TRANSITIONAL_STATES = [
  "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED", "SETTING_HOME",
  "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
  "PAUSE_REQUESTED", "RESUME_REQUESTED",
  "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED",
];

// Terminal states a controller Rearm is offered from.
export const REARMABLE_STATES = ["COMPLETED_HOLD", "SUSPENDED", "FAILED"];

// Progress labels for the states Scout passes THROUGH. Each names the real step Scout is
// performing, so the operator can see where a Start actually stalled.
export const TRANSITION_LABELS = {
  START_REQUESTED: "Preparing start…",
  START_HOLD_REQUESTED: "Requesting launch hold…",
  START_HOLD_CONFIRMED: "Launch hold verified",
  SETTING_HOME: "Setting launch Home…",
  VERIFYING_HOME: "Verifying Home…",
  SYNCHRONIZING_PACKAGE: "Synchronizing mission package…",
  STARTING_AUTO: "Starting AUTO…",
  PAUSE_REQUESTED: "Pausing mission…",
  RESUME_REQUESTED: "Resuming mission…",
  RETURNING_HOME: "Returning to Home",
  HOME_ARRIVAL_PENDING: "Confirming Home arrival…",
  FINAL_HOLD_REQUESTED: "Requesting final hold…",
};

// Resting-state labels (the states that are not a step in a transaction).
export const STATE_LABELS = {
  NOT_READY: "Not ready",
  READY: "Ready to start",
  RUNNING: "Running",
  PAUSED: "Paused",
  COMPLETED_HOLD: "Completed — holding at Home",
  SUSPENDED: "Suspended",
  FAILED: "Failed",
};

// Scout's structured start/lifecycle error codes → readable operator text. This is PRESENTATION
// of Scout's own code (which is always shown alongside), never a reimplementation of Scout's
// precondition logic — the operator station does not decide any of these.
export const ERROR_TEXT = {
  NO_ACTIVE_MISSION: "Scout has no active mission",
  NO_PLANNING_PACKAGE: "Scout holds no planning package",
  MISSION_ID_MISMATCH: "The planning package / mission id does not match the expected mission",
  POSITION_STALE_OR_INVALID: "Position is stale or invalid",
  PIXHAWK_STATE_UNAVAILABLE: "Pixhawk state is unavailable",
  AUTHORITY_LOST: "Control authority was lost",
  LOITER_NOT_VERIFIED: "The launch LOITER could not be verified",
  SET_HOME_FAILED: "Setting Home failed",
  PACKAGE_SYNC_FAILED: "Planning-package synchronization failed",
  PACKAGE_INCONSISTENT_AFTER_SYNC: "The planning package was inconsistent after synchronization",
  AUTO_NOT_VERIFIED: "AUTO could not be verified",
  PROGRESSION_UNCONFIRMED: "Mission progression could not be confirmed",
  MISSION_EXECUTION_DISABLED: "Mission execution is disabled on Scout",
  REPLANNING_ACTIVE: "The replanning controller owns the vehicle",
  ARBITRATION_BUSY: "Another write is in progress on Scout (write arbitration)",
};

export const OUTCOME = {
  ACCEPTED: "accepted",
  FAILED: "failed",
  REJECTED: "rejected",
  UNKNOWN: "unknown",
  UNAVAILABLE: "unavailable",
  UNSUPPORTED: "unsupported",
};

const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const str = (v) => {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
};
function first(obj, ...keys) {
  if (!isObj(obj)) return null;
  for (const k of keys) if (obj[k] !== undefined && obj[k] !== null) return obj[k];
  return null;
}

// Fields that IDENTIFY a body as a mission-execution status. This guard is not theoretical: an
// older Local Agent routes with `path.startswith("/agent/mission")`, so it answers
// GET /agent/mission_execution/status with HTTP 200 and its legacy PIXHAWK MISSION READBACK.
// Accepting that as a status would render a card claiming the lifecycle is supported while every
// field is blank — precisely the fabrication that is forbidden. Presence of the KEY is what
// counts, because `active_operation_id: null` is a legitimate value.
export const STATUS_IDENTIFYING_FIELDS = [
  "state", "effective_state", "execution_state", "mission_execution_enabled",
  "can_start", "can_pause", "can_resume",
];

/** True when a body actually IS a mission-execution status, not another endpoint's answer. */
export function isStatusBody(body) {
  if (!isObj(body)) return false;
  return STATUS_IDENTIFYING_FIELDS.some((f) => f in body);
}

/** True for a state Scout passes through inside a transaction. */
export function isTransitional(state) {
  return TRANSITIONAL_STATES.includes(String(state || "").toUpperCase());
}
/** True for a terminal state a Rearm is offered from. */
export function isRearmable(state) {
  return REARMABLE_STATES.includes(String(state || "").toUpperCase());
}
/** True when Scout reports a state this build does not know — displayed as-is and flagged,
 *  never silently bucketed into a state the operator would act on. */
export function isUnknownState(state) {
  const s = str(state);
  return !!s && !STATES.includes(s.toUpperCase());
}

/** Human label for a state: its progress label while transitional, else its resting label,
 *  else the raw state Scout sent (never blank, never invented). */
export function stateLabel(state) {
  const s = str(state);
  if (!s) return "Unknown";
  const up = s.toUpperCase();
  return TRANSITION_LABELS[up] || STATE_LABELS[up] || s;
}

/** Readable text for a Scout error code, followed by the code itself (which is always shown). */
export function errorText(code) {
  const c = str(code);
  if (!c) return null;
  return ERROR_TEXT[c.toUpperCase()] || c;
}

/**
 * Normalize the backend's mission-execution status envelope into what the card renders.
 * `res` is the api.js response ({ scout, summary, supported, reachable, outcome }) or Scout's
 * body directly. EVERY field is Scout's word or null — supported:false and reachable:false stay
 * honest, and READY / can_start / verified Home / continuation / completion are never defaulted.
 */
export function normalizeStatus(res) {
  const reachable = !!res && res.reachable !== false;
  const s = isObj(res && res.scout) ? res.scout : (isObj(res) ? res : {});
  // The body check only decides SUPPORT when Scout actually answered. An unreachable Scout told
  // us nothing about whether it implements the lifecycle, so it stays "unavailable" (a link
  // problem the operator can fix) rather than being mislabelled "unsupported" (a Scout version).
  const supported = !!res && res.supported !== false
    && !(isObj(res.scout) && res.scout.supported === false)
    && (!reachable || isStatusBody(s));
  const present = supported && reachable && Object.keys(s).length > 0;

  const seq = isObj(s.sequence) ? s.sequence : {};
  const rc = isObj(s.return_completion) ? s.return_completion : {};
  const rp = isObj(s.replanning) ? s.replanning : {};
  const ts = isObj(s.timestamps) ? s.timestamps : {};
  const state = str(s.state);
  const effective = str(s.effective_state) || state;

  return {
    supported, reachable, present,
    state,
    effectiveState: effective,
    unknownState: isUnknownState(state),
    transitional: isTransitional(state),
    activeOperationId: str(s.active_operation_id),
    missionId: str(s.mission_id),
    originalRouteHash: str(s.original_route_hash),
    activeRouteHash: str(s.active_route_hash),
    mode: str(s.mode),
    authority: str(first(s, "authority_status", "authority")),
    canStart: s.can_start === true,
    canPause: s.can_pause === true,
    canResume: s.can_resume === true,
    missionExecutionEnabled: s.mission_execution_enabled,
    lastError: first(s, "last_error"),
    timestamps: { start: str(ts.start), pause: str(ts.pause), resume: str(ts.resume) },
    home: {
      verified: isObj(s.verified_home) ? s.verified_home : null,
      verificationDistanceM: typeof s.home_verification_distance_m === "number"
        ? s.home_verification_distance_m : null,
    },
    sequence: {
      current: seq.current ?? null,
      count: seq.count ?? null,
      beforePause: seq.before_pause ?? null,
      atResume: seq.at_resume ?? null,
      firstAfterResume: seq.first_after_resume ?? null,
      continuationVerified: seq.continuation_verified ?? null,
      reported: Object.keys(seq).length > 0,
    },
    replanning: {
      active: rp.active === true || effective === EFFECTIVE_REPLANNING,
      fsmState: str(rp.fsm_state),
    },
    returnCompletion: {
      reported: Object.keys(rc).length > 0,
      distanceToHomeM: typeof rc.distance_to_home_m === "number" ? rc.distance_to_home_m : null,
      arrivalRadiusM: typeof rc.arrival_radius_m === "number" ? rc.arrival_radius_m : null,
      persistenceS: typeof rc.persistence_s === "number" ? rc.persistence_s : null,
      persistenceProgressS: typeof rc.persistence_progress_s === "number"
        ? rc.persistence_progress_s : null,
      arrivalConfirmed: rc.arrival_confirmed ?? null,
      finalLoiterVerified: rc.final_loiter_verified ?? null,
    },
    history: Array.isArray(s.history) ? s.history : [],
    config: isObj(s.config) ? s.config : {},
  };
}

/**
 * THE primary lifecycle control, derived EXCLUSIVELY from Scout's status.
 *
 * Returns { action, label, enabled, tone, reason } where `action` is "start" | "pause" |
 * "resume" | null. The caller must not remember the previous label or the last click — pressing
 * Start does not turn the button into Pause; Scout's next authoritative status does.
 *
 * Precedence, and why:
 *   unsupported / unavailable  — nothing may be claimed about an older or unreachable Scout.
 *   replanning owns the vehicle — no competing action while effective_state=REPLANNING.
 *   an operation is in flight   — active_operation_id or a transitional state: show progress.
 *   can_pause → can_resume → can_start — Scout reports these mutually exclusively; ordering the
 *       running case first means an inconsistent Scout could never offer "Start" on a vehicle it
 *       simultaneously says is pausable.
 *   terminal states             — disabled, with Rearm offered separately (see rearmAvailability).
 */
export function primaryAction(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);

  if (!S.supported) {
    return { action: null, label: "Mission lifecycle unsupported", enabled: false, tone: "idle",
      reason: "Mission lifecycle not supported by this Scout version" };
  }
  if (!S.reachable || !S.present) {
    return { action: null, label: "Lifecycle status unavailable", enabled: false, tone: "idle",
      reason: "Scout mission-execution status is unavailable — no action can be offered" };
  }
  if (S.replanning.active) {
    return { action: null, label: "Agent Replanning…", enabled: false, tone: "caution",
      reason: "The replanning controller owns the vehicle" +
        (S.replanning.fsmState ? ` (replanning FSM: ${S.replanning.fsmState})` : "") };
  }
  if (S.activeOperationId || S.transitional) {
    return { action: null, label: stateLabel(S.state), enabled: false, tone: "caution",
      reason: S.activeOperationId
        ? `Scout is processing operation ${S.activeOperationId}`
        : "Scout is mid-transaction" };
  }
  if (S.canPause) {
    return { action: "pause", label: "Pause Mission", enabled: true, tone: "ok", reason: null };
  }
  if (S.canResume) {
    return { action: "resume", label: "Resume Mission", enabled: true, tone: "ok", reason: null };
  }
  if (S.canStart) {
    return { action: "start", label: "Start Mission", enabled: true, tone: "ok", reason: null };
  }
  if (S.state === "COMPLETED_HOLD") {
    return { action: null, label: "Mission complete", enabled: false, tone: "ok",
      reason: "Rearm the mission controller to prepare a new run" };
  }
  if (S.state === "SUSPENDED" || S.state === "FAILED") {
    return { action: null, label: stateLabel(S.state), enabled: false, tone: "warn",
      reason: S.lastError ? String(S.lastError) : `Scout reports ${S.state}` };
  }
  return { action: null, label: stateLabel(S.state), enabled: false, tone: "idle",
    reason: `Scout reports ${S.state || "no state"} and offers no action` };
}

/** Whether to offer "Rearm Mission Controller", and why not when it is withheld. Rearm is only
 *  meaningful from a terminal state, and never while an operation or replanning is in flight. */
export function rearmAvailability(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  if (!S.supported || !S.present) return { available: false, enabled: false, reason: null };
  if (!isRearmable(S.state)) return { available: false, enabled: false, reason: null };
  if (S.replanning.active)
    return { available: true, enabled: false, reason: "Replanning is active" };
  if (S.activeOperationId)
    return { available: true, enabled: false, reason: "An operation is in progress" };
  return { available: true, enabled: true, reason: null };
}

/**
 * Why Start is not available, worded from Scout's OWN reported status (plus the error code of the
 * last attempted operation). This does NOT re-derive Scout's preconditions — it reports what
 * Scout said. An empty list with canStart false simply means Scout gave no reason.
 */
export function startBlockers(status, { lastErrorCode = null } = {}) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const out = [];
  if (!S.supported) { out.push("Mission lifecycle not supported by this Scout version"); return out; }
  if (!S.present) { out.push("Scout mission-execution status is unavailable"); return out; }
  if (S.canStart) return out;

  if (S.missionExecutionEnabled === false)
    out.push("Mission execution is disabled on Scout (MISSION_EXECUTION_DISABLED)");
  if (S.replanning.active)
    out.push("Replanning is active — the replanning controller owns the vehicle");
  if (S.activeOperationId)
    out.push(`An operation is already in progress (${S.activeOperationId})`);
  if (S.authority && S.authority !== "LOCAL_AGENT")
    out.push(`Scout reports authority ${S.authority} — mission execution needs LOCAL_AGENT`);
  if (S.state && !["READY", "NOT_READY"].includes(S.state) && !S.transitional)
    out.push(`Scout is in ${S.state}, not READY`);
  else if (S.state === "NOT_READY")
    out.push("Scout reports NOT_READY — mission, planning package, position or authority is not in place");
  if (S.lastError) out.push(`Scout last error: ${typeof S.lastError === "string" ? S.lastError : JSON.stringify(S.lastError)}`);
  if (lastErrorCode) {
    const t = errorText(lastErrorCode);
    out.push(`Last attempt: ${lastErrorCode}${t && t !== lastErrorCode ? ` — ${t}` : ""}`);
  }
  return out;
}

/**
 * The mission is complete ONLY when Scout reports BOTH the COMPLETED_HOLD state AND a verified
 * final LOITER. Arrival alone, a persistence bar at 100%, or RETURNING_HOME ending is NOT
 * completion — Scout stays outside COMPLETED_HOLD and reports an error if final LOITER cannot
 * be verified, and the station must show that rather than a green tick.
 */
export function isComplete(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  return S.state === "COMPLETED_HOLD" && S.returnCompletion.finalLoiterVerified === true;
}

/** Continuation evidence after a Resume: verified / NOT verified / not reported. `false` is a
 *  prominent warning even when the mode is AUTO — AUTO proves the mode change, not continuation. */
export function continuationView(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const cv = S.sequence.continuationVerified;
  if (cv === true) {
    return { state: "verified", tone: "ok", warning: false,
      message: "Continuation from the paused waypoint was verified." };
  }
  if (cv === false) {
    return { state: "not_verified", tone: "warn", warning: true,
      message: "AUTO resumed, but continuation from the paused waypoint was not verified. " +
        "Check whether the Pixhawk restarted the mission at waypoint 0." };
  }
  return { state: "unavailable", tone: "idle", warning: false,
    message: "Continuation has not been tested — no pause/resume cycle has been reported." };
}

/** Return-completion progress for the persistence dwell, or null when Scout reports none.
 *  `fraction` is clamped to 0..1; it is a display of Scout's counters, never an arrival decision. */
export function returnProgress(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const rc = S.returnCompletion;
  if (!rc.reported || rc.persistenceS === null) return null;
  const total = rc.persistenceS;
  const done = rc.persistenceProgressS === null ? 0 : rc.persistenceProgressS;
  const fraction = total > 0 ? Math.max(0, Math.min(1, done / total)) : 0;
  return { done, total, fraction, arrivalConfirmed: rc.arrivalConfirmed,
    finalLoiterVerified: rc.finalLoiterVerified, distanceToHomeM: rc.distanceToHomeM,
    arrivalRadiusM: rc.arrivalRadiusM };
}

/**
 * Interpret an operation response from api.js ({ ok, status, data }) into the OPERATIONAL verdict.
 * Derived here from the HTTP status and Scout's body rather than trusting a single backend field,
 * because rule 2 above is the one this station must never get wrong:
 *
 *   HTTP 200 + body.error (or accepted:false)  → failed     (a vehicle-level failure)
 *   HTTP 200 + supported:false                 → unsupported (older Scout)
 *   HTTP 409                                   → rejected   (precondition / lifecycle /
 *                                                            replanning / arbitration)
 *   HTTP 4xx                                   → rejected
 *   HTTP 202                                   → unknown    (reconcile, never resend)
 *   HTTP 503                                   → unavailable
 */
export function interpretOperation(res) {
  const data = isObj(res && res.data) ? res.data : (isObj(res) ? res : {});
  const httpStatus = (res && typeof res.status === "number") ? res.status : data.http_status;
  const scout = isObj(data.scout) ? data.scout : {};
  const code = str(data.scout_error_code) || str(scout.error_code)
    || (typeof scout.error === "string" ? str(scout.error)
      : isObj(scout.error) ? str(first(scout.error, "code", "error_code")) : null);
  const message = str(data.scout_error_message)
    || (isObj(scout.error) ? str(first(scout.error, "message", "detail")) : null)
    || str(data.error);

  const base = {
    code, message,
    operationId: str(scout.operation_id) || str(data.operation_id),
    resultingState: str(scout.current_state) || str(scout.execution_state) || str(data.current_state),
    verifiedMode: str(scout.verified_mode) || str(data.verified_mode),
    missionId: str(scout.mission_id) || str(data.mission_id),
    final: scout.final ?? data.final ?? null,
    idempotent: scout.idempotent ?? data.idempotent ?? null,
    sequence: isObj(scout.sequence) ? scout.sequence : (isObj(data.sequence) ? data.sequence : null),
    homeResult: isObj(scout.home_result) ? scout.home_result
      : (isObj(data.home_result) ? data.home_result : null),
    reconciliation: isObj(data.reconciliation) ? data.reconciliation : null,
    httpStatus: httpStatus ?? null,
  };

  if (data.supported === false || httpStatus === 404)
    return { ...base, outcome: OUTCOME.UNSUPPORTED };
  if (httpStatus === 202) return { ...base, outcome: OUTCOME.UNKNOWN };
  if (httpStatus === 503) return { ...base, outcome: OUTCOME.UNAVAILABLE };
  if (httpStatus === 409 || (typeof httpStatus === "number" && httpStatus >= 400 && httpStatus < 500))
    return { ...base, outcome: OUTCOME.REJECTED };
  if (typeof httpStatus === "number" && httpStatus >= 500)
    return { ...base, outcome: OUTCOME.UNKNOWN };
  // 2xx — Scout PROCESSED the request. It may still have failed on the vehicle.
  if (code || scout.accepted === false || data.accepted === false)
    return { ...base, outcome: OUTCOME.FAILED };
  return { ...base, outcome: OUTCOME.ACCEPTED };
}

/** Operator-facing label + tone for an operation outcome. "unknown" is deliberately NOT
 *  "failed", and "failed" is deliberately NOT rounded up to a success because HTTP said 200. */
export function outcomeLabel(outcome) {
  return ({
    accepted: "Accepted",
    failed: "Failed on the vehicle",
    rejected: "Rejected by Scout",
    unknown: "Unknown — reconciling",
    unavailable: "Unavailable",
    unsupported: "Not supported",
  })[outcome] || "—";
}

export const OUTCOME_TONE = {
  accepted: "ok", failed: "warn", rejected: "caution",
  unknown: "caution", unavailable: "idle", unsupported: "idle",
};

/** One-line operator summary of an operation result: outcome, Scout's exact code and message,
 *  the resulting state, and — for an unknown — how the reconciling read resolved it. */
export function operationSummary(view) {
  if (!isObj(view)) return "—";
  const parts = [outcomeLabel(view.outcome)];
  if (view.code) {
    const t = errorText(view.code);
    parts.push(t && t !== view.code ? `${view.code} — ${t}` : view.code);
  }
  if (view.message && view.message !== view.code) parts.push(view.message);
  if (view.resultingState) parts.push(`state ${view.resultingState}`);
  if (view.outcome === OUTCOME.UNKNOWN && view.reconciliation) {
    parts.push(`reconciled: ${view.reconciliation.resolved}`
      + (view.reconciliation.detail ? ` — ${view.reconciliation.detail}` : ""));
  }
  return parts.join(" · ");
}

/** The explanatory sentence the card must carry: Start RESETS Home to the launch position. */
export const START_HOME_NOTE =
  "Start Mission first holds position, sets the current launch position as Home, verifies it, " +
  "synchronizes the planning package, and then starts AUTO. The originally planned Home is not " +
  "retained.";
