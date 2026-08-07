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

// The replanning FSM's own in-flight transaction states, taken from the replanning logic layer
// rather than re-listed here — there is ONE list of what "a replan transaction is running"
// means, and both surfaces read it.
import { FSM_ACTIVE_ORDER } from "./replan.js";

// Scout's mission-execution states, verbatim and complete.
export const STATES = [
  "NOT_READY", "NOT_STARTED", "READY",
  "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED",
  "SETTING_HOME", "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
  "RUNNING",
  "PAUSE_REQUESTED", "PAUSED", "RESUME_REQUESTED",
  "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED", "STOPPED", "CANCELLED",
  "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED", "COMPLETED_HOLD",
  "SUSPENDED", "FAILED",
];

// The STOP contract — PENDING ON SCOUT (see SCOUT_STOP_API.md). The states below exist here so
// the model, the labels and the button mapping are complete the day Scout ships the endpoint.
// Until then `stopAvailability()` reports UNSUPPORTED and the control is disabled with that
// reason; Stop is never synthesized from a low-level LOITER plus operator-side state, never
// shown as FAILED, and Rearm is never offered in its place.
export const STOP_IN_TRANSACTION_STATES = [
  "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
];
// Terminal "the run was deliberately ended" states. A fresh Start is offered from here.
export const STOPPED_STATES = ["STOPPED", "CANCELLED"];
// Resting states a mission has not (or no longer) been started from.
export const STARTABLE_STATES = ["READY", "NOT_STARTED", ...STOPPED_STATES];

// An OVERLAY, not a state: Scout reports effective_state=REPLANNING while its stored state is
// still RUNNING / PAUSED / another live state. The replanning controller owns the vehicle.
export const EFFECTIVE_REPLANNING = "REPLANNING";

// THE ONLY evidence that may be presented as "the agent is replanning". Either Scout's replan
// controller says so outright (`replanning.active === true` / effective_state REPLANNING), or it
// reports one of ITS OWN transaction states — the FSM states lib/replan.js defines as a
// replanning transaction in flight. Nothing else qualifies, and in particular none of these do:
// a readiness refresh, a mission read-back refresh, a pending status request, state NOT_READY,
// stale evidence, temporary unavailability, missing fields, or a general busy/in-flight flag.
// Telling an operator the agent is replanning when it is idle is not a cosmetic error: it says
// the autonomy has taken the vehicle, which is a reason to stop and wait.
export const EXPLICIT_REPLAN_STATES = new Set([EFFECTIVE_REPLANNING, ...FSM_ACTIVE_ORDER]);

// Scout is in flight: show progress, disable the primary control, predict nothing. This is every
// state that is not a RESTING one (NOT_READY / READY / RUNNING / PAUSED / COMPLETED_HOLD /
// SUSPENDED / FAILED) — the transaction steps AND the return phase, which is likewise not a state
// the operator may act on.
export const TRANSITIONAL_STATES = [
  "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED", "SETTING_HOME",
  "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
  "PAUSE_REQUESTED", "RESUME_REQUESTED",
  ...STOP_IN_TRANSACTION_STATES,
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
  STOP_REQUESTED: "Stopping mission…",
  STOP_HOLD_REQUESTED: "Requesting stop hold…",
  STOP_HOLD_CONFIRMED: "Stop hold verified",
  RETURNING_HOME: "Returning to Home",
  HOME_ARRIVAL_PENDING: "Confirming Home arrival…",
  FINAL_HOLD_REQUESTED: "Requesting final hold…",
};

// Resting-state labels (the states that are not a step in a transaction).
export const STATE_LABELS = {
  NOT_READY: "Not ready",
  NOT_STARTED: "Not started",
  READY: "Ready to start",
  RUNNING: "Running",
  PAUSED: "Paused",
  STOPPED: "Stopped",
  CANCELLED: "Cancelled",
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
  // Codes the OPERATOR raises before Scout is contacted (mission_lifecycle.py). They are
  // deliberately worded as "we did not send this", never as a vehicle failure.
  NO_ACTIVE_MISSION_RECORD: "This vehicle has no active persisted mission record",
  START_PRECONDITIONS_NOT_MET: "The Start preconditions are not met",
  AUTHORITY_NOT_VERIFIED: "Control authority could not be verified as LOCAL_AGENT — Scout was " +
    "not contacted",
  MISSION_EXECUTION_UNSUPPORTED: "This Scout does not implement the mission-execution lifecycle",
};

export const OUTCOME = {
  ACCEPTED: "accepted",
  FAILED: "failed",
  REJECTED: "rejected",
  // The OPERATOR refused it: nothing left this station, so nothing can have taken effect.
  BLOCKED: "blocked",
  UNKNOWN: "unknown",
  UNAVAILABLE: "unavailable",
  UNSUPPORTED: "unsupported",
};

import { asText } from "./format.js";
// The readiness PRESENTATION vocabulary (the two pre-start states, the Start transaction's phase
// labels, the Home-during-Start note) lives in its own module; this one derives the gate those
// labels describe. The dependency runs one way only: mission-readiness.js imports nothing here.
import {
  READINESS, CHECKING_TEXT, START_BLOCK, START_BLOCK_TEXT, HOME_DURING_START_NOTE,
  startPhase, isStartTransactionState,
} from "./mission-readiness.js";

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
  "can_start", "can_pause", "can_resume", "can_stop",
];

/** True when a body actually IS a mission-execution status, not another endpoint's answer. */
export function isStatusBody(body) {
  if (!isObj(body)) return false;
  return STATUS_IDENTIFYING_FIELDS.some((f) => f in body);
}

/**
 * True ONLY on explicit replan evidence. STRICT by construction:
 *
 *   replan.active === true                       Scout's replan controller says so
 *   EXPLICIT_REPLAN_STATES.has(replan.state)     one of the controller's own transaction states
 *   effective_state === "REPLANNING"             Scout's overlay
 *
 * Never the truthiness of the replanning object (a `{active:false, fsm_state:"MONITORING"}`
 * block is an object, and an object is not a replan), never a missing field, never a busy or
 * refreshing flag. Missing, malformed and stale replan data all fail to NOT replanning, which
 * is the honest direction: absence of evidence is not evidence of autonomy taking the vehicle.
 *
 * @param replan Scout's `replanning` block (raw or normalized), or null/undefined
 * @param effectiveState Scout's `effective_state`, when the caller has it
 */
export function isReplanning(replan, effectiveState = null) {
  if (isObj(replan) && replan.active === true) return true;
  const explicit = (v) => {
    const s = str(v);
    return !!s && EXPLICIT_REPLAN_STATES.has(s.toUpperCase());
  };
  if (isObj(replan) && (explicit(replan.state) || explicit(replan.fsm_state)
      || explicit(replan.fsmState))) return true;
  return explicit(effectiveState);
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
    // TRI-STATE, exactly like canStop below, and for the same reason: `true` / `false` / `null`
    // are three different facts and collapsing the third into the second is a lie the operator
    // acts on. A Scout that reports `can_pause:false` has REFUSED a pause right now; a Scout
    // whose status simply carries no `can_pause` key has said NOTHING about it — and a strict
    // `=== true` turned that silence into a Pause button disabled with the fabricated reason
    // "Scout reports can_pause=false", which is the defect this tri-state removes. When the key
    // is absent, Scout's own STATE is the authority (see pauseAvailability / resumeAvailability):
    // RUNNING means the mission is running, so Pause is offered and Scout arbitrates it.
    canPause: "can_pause" in s ? s.can_pause === true : null,
    canResume: "can_resume" in s ? s.can_resume === true : null,
    pauseReported: "can_pause" in s,
    resumeReported: "can_resume" in s,
    // Stop is the one operation whose SUPPORT is in question, so it is modelled in three
    // values rather than two: true / false / null. PRESENCE of `can_stop` is the support
    // signal — a Scout that has shipped Stop reports it either way; one that has not omits
    // it, and `null` is what makes the UI say "this Scout version has no Stop" instead of
    // the untrue "you cannot stop right now". See SCOUT_STOP_API.md.
    canStop: "can_stop" in s ? s.can_stop === true : null,
    stopSupported: "can_stop" in s,
    missionExecutionEnabled: s.mission_execution_enabled,
    lastError: first(s, "last_error"),
    timestamps: { start: str(ts.start), pause: str(ts.pause), resume: str(ts.resume) },
    home: {
      verified: isObj(s.verified_home) ? s.verified_home : null,
      verificationDistanceM: typeof s.home_verification_distance_m === "number"
        ? s.home_verification_distance_m : null,
      // Scout's EXPLICIT declaration that it will not enter the Start transaction without an
      // ALREADY verified Home. Absent — which is the normal case — the Start transaction owns
      // Set Home and verifies it as one of its own phases, so an unverified Home before Start is
      // a step that has not happened yet, not a defect. Only this declared flag may withhold
      // Start for Home; it is never inferred from a missing verified_home block.
      requiredBeforeStart: first(s, "requires_verified_home", "start_requires_verified_home")
        === true || (isObj(s.config) && (s.config.requires_verified_home === true
          || s.config.start_requires_verified_home === true)),
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
      // Explicit evidence only — see isReplanning(). `s.replanning` being present, or the
      // status being mid-refresh, proves nothing.
      active: isReplanning(rp, effective),
      state: str(rp.state),
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

/** The replanning controller's own state, whichever field Scout spelled it in. Display only. */
function replanFsm(S) {
  return (S && S.replanning && (S.replanning.fsmState || S.replanning.state)) || null;
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
        (replanFsm(S) ? ` (replanning FSM: ${replanFsm(S)})` : "") };
  }
  if (S.activeOperationId || S.transitional) {
    return { action: null, label: stateLabel(S.state), enabled: false, tone: "caution",
      reason: S.activeOperationId
        ? `Scout is processing operation ${S.activeOperationId}`
        : "Scout is mid-transaction" };
  }
  // Pause / Resume come from the SHARED hold derivation (pauseAvailability / resumeAvailability),
  // so this control and the Map card's buttons answer the same question the same way — including
  // the rule that an ABSENT can_pause is silence, not a refusal.
  const pause = pauseAvailability(S);
  if (pause.available) {
    return { action: "pause", label: "Pause Mission", enabled: pause.enabled, tone: "ok",
      reason: pause.reason };
  }
  const resume = resumeAvailability(S);
  if (resume.available) {
    return { action: "resume", label: "Resume Mission", enabled: resume.enabled, tone: "ok",
      reason: resume.reason };
  }
  if (S.canStart) {
    return { action: "start", label: "Start Mission", enabled: true, tone: "ok", reason: null };
  }
  if (S.state === "COMPLETED_HOLD") {
    return { action: null, label: "Mission complete", enabled: false, tone: "ok",
      reason: "Rearm the mission controller to prepare a new run" };
  }
  if (S.state === "SUSPENDED" || S.state === "FAILED") {
    // asText, never String(): Scout's last_error is frequently a structured {code, message},
    // and String() would put the literal text "[object Object]" where the failure reason goes.
    return { action: null, label: stateLabel(S.state), enabled: false, tone: "warn",
      reason: asText(S.lastError) || `Scout reports ${S.state}` };
  }
  return { action: null, label: stateLabel(S.state), enabled: false, tone: "idle",
    reason: `Scout reports ${S.state || "no state"} and offers no action` };
}

// States in which a mission is already under way, so a Start would be a second one. Kept apart
// from TRANSITIONAL_STATES because RETURNING_HOME belongs here (the run is still happening) while
// it is deliberately NOT a moment in which Pause and Stop are denied.
export const RUNNING_STATES = ["RUNNING", "PAUSED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING",
  "FINAL_HOLD_REQUESTED"];

/**
 * WHETHER START MAY BE OFFERED — from OBVIOUS, STABLE blockers only.
 *
 * This is the gate the Map uses, and it is deliberately shallow. Every input below changes only
 * when something real changes: the link goes down, Scout's version has no lifecycle, the vehicle
 * has no active mission, an operation starts, the replanning controller takes the vehicle, the
 * mission is already running, or a terminal state needs a Rearm first.
 *
 * WHAT IS DELIBERATELY NOT HERE, and why:
 *
 *   the Start preflight's verdict.   It is a fresh, expensive, SHORT-LIVED proof whose Pixhawk
 *       read-back is served through a 10 s cache. Polling it made a stable vehicle's Start button
 *       appear and disappear every few seconds on evidence that had nothing to do with the
 *       vehicle. The proof still runs — inside the Start transaction, fresh, before any write
 *       (mission_lifecycle.run_start), which is the only place it can actually protect anything.
 *   a missing / refreshing / stale readback.   Same reason. An in-flight background read is not
 *       a fact about the vehicle and must never change what the operator may do.
 *   an unverified Home.   The Start transaction sets Home to the launch position and verifies it
 *       as one of its own phases. It blocks here ONLY if Scout explicitly declares it requires an
 *       existing verified Home first (status.home.requiredBeforeStart).
 *   control authority.    Scout reports NOT_READY / can_start:false whenever authority is not yet
 *       LOCAL_AGENT — the very condition the Start transaction resolves as its first phase. That
 *       is why Scout's own `can_start:false` is not a blocker here either; the backend's
 *       start_eligibility() applies the same single deferral, and Scout arbitrates the Start.
 *
 * This is NOT a safety gate and does not pretend to be one. It decides what is OFFERED; the
 * backend transaction decides what is DONE, fail-closed, and refuses anything it cannot prove.
 *
 * @param status  normalized (or raw) Scout mission-execution status
 * @param opts.connected  the vehicle's link state (false = disconnected)
 * @param opts.busy       a lifecycle operation is in flight from this station
 * @param opts.missionId  the active mission id when Scout's status does not carry one (the
 *                        operator backend knows the active persisted record)
 * @returns {{ canStart, code, reason, detail }}
 */
export function startGate(status, { connected = true, busy = false, missionId = null } = {}) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const state = String(S.state || "").toUpperCase();
  const block = (code, reason = null, detail = null) => ({
    canStart: false, code,
    reason: reason || START_BLOCK_TEXT[code],
    detail: detail || reason || START_BLOCK_TEXT[code],
  });

  if (busy) return block(START_BLOCK.BUSY);
  if (connected === false) {
    return block(START_BLOCK.DISCONNECTED, null,
      "The vehicle is not reporting. A mission cannot be started over a link that is down.");
  }
  if (!S.supported) return block(START_BLOCK.UNSUPPORTED);
  if (!S.reachable || !S.present) return block(START_BLOCK.STATUS_UNAVAILABLE);
  if (S.missionExecutionEnabled === false) {
    return block(START_BLOCK.UNSUPPORTED, "Mission execution is disabled on Scout");
  }
  // Explicit replan evidence only — isReplanning(). A readiness refresh, a pending status
  // request, NOT_READY, stale evidence and missing fields all fail to NOT replanning.
  if (S.replanning.active) {
    return block(START_BLOCK.REPLANNING, null,
      "The replanning controller owns the vehicle" +
      (replanFsm(S) ? ` (replanning FSM: ${replanFsm(S)})` : "") +
      " — Start stays disabled until Scout hands control back.");
  }
  if (S.activeOperationId) {
    return block(START_BLOCK.OPERATION_ACTIVE,
      `Scout is already processing operation ${S.activeOperationId}`);
  }
  if (S.transitional && state !== "RETURNING_HOME") {
    return block(START_BLOCK.OPERATION_ACTIVE, `Scout is mid-transaction (${stateLabel(state)})`);
  }
  if (RUNNING_STATES.includes(state)) {
    return block(START_BLOCK.ALREADY_RUNNING,
      `The mission is already ${stateLabel(state).toLowerCase()}`);
  }
  if (isRearmable(state)) {
    return block(START_BLOCK.REARM_REQUIRED, null,
      `Scout reports ${state}. Rearm the mission controller to prepare a fresh run before ` +
      "starting again.");
  }
  const mid = str(S.missionId) || str(missionId);
  if (!mid) {
    return block(START_BLOCK.NO_MISSION, null,
      "This vehicle has no active mission. Finalize and upload a mission before starting.");
  }
  // The ONE Home case that may withhold Start, and only on Scout's explicit declaration.
  if (S.home.requiredBeforeStart && !isObj(S.home.verified)) {
    return block(START_BLOCK.HOME_REQUIRED);
  }
  return { canStart: true, code: null, reason: null,
    detail: "Start runs one backend transaction: a fresh preflight, the authority hand-off, a " +
      "fresh mission and package proof, a verified LOITER, Set Home and verify, then AUTO and " +
      "verify. No vehicle write happens before its own proof succeeds." };
}

// The states each hold control belongs to. PAUSE is offered while the run is under way
// (RETURNING_HOME included — the mission is still happening and holding it is meaningful);
// RESUME only after a pause, which is the one thing that makes "Resume" honest.
export const PAUSABLE_STATES = ["RUNNING", "RETURNING_HOME"];
export const RESUMABLE_STATES = ["PAUSED"];

/**
 * Whether PAUSE / RESUME may be offered, and why not — the shared derivation behind both the
 * Map card's buttons and the Agent page's primary control, so the two can never disagree.
 *
 * THE RULE, and why it is not "trust can_pause":
 *   Scout's STATE is the authority for WHICH hold control exists. RUNNING means there is a run
 *   to hold; PAUSED means there is a hold to release. Scout's `can_*` flag is the authority for
 *   whether it will accept that operation RIGHT NOW — but only when Scout actually sends it.
 *   An ABSENT flag is not a refusal, and treating it as one is what left a RUNNING mission with
 *   no usable Pause control on the Map. So:
 *
 *     flag true    → enabled.
 *     flag false   → shown, DISABLED, with Scout's own can_* answer as the reason. Scout has
 *                    refused; the station does not talk it into a button that would 409.
 *     flag absent  → enabled. The state is the authority, and nothing is fabricated: the backend
 *                    pause/resume transaction is fail-closed and Scout arbitrates the write.
 *
 * Everything Scout owns the moment for still wins first: an unreadable status, the replanning
 * controller, an active operation id and a mid-transaction state all withhold the control.
 *
 * @returns {{ available, enabled, reason, reported }} — `available:false` means "not this state",
 *   which is what hides the control entirely rather than showing a dead one.
 */
function holdAvailability(status, { op, states, canField, flag, reported }) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const state = String(S.state || "").toUpperCase();
  if (!S.supported || !S.present) {
    return { available: false, enabled: false, reported: false,
      reason: `Scout mission-execution status is unavailable — ${op} cannot be offered` };
  }
  if (!states.includes(state)) {
    return { available: false, enabled: false, reported, reason: null };
  }
  if (S.replanning.active) {
    return { available: true, enabled: false, reported,
      reason: "The replanning controller owns the vehicle" };
  }
  if (S.activeOperationId) {
    return { available: true, enabled: false, reported,
      reason: `Scout is already processing operation ${S.activeOperationId}` };
  }
  if (S.transitional && state !== "RETURNING_HOME") {
    return { available: true, enabled: false, reported,
      reason: `Scout is mid-transaction (${stateLabel(state)})` };
  }
  if (flag === false) {
    return { available: true, enabled: false, reported,
      reason: `Scout reports ${canField}=false in ${state}` };
  }
  return { available: true, enabled: true, reported, reason: null };
}

/** Whether Pause may be offered (see holdAvailability). Pause HOLDS the mission in a verified
 *  LOITER — it ends nothing, clears nothing and is never a substitute for Stop. */
export function pauseAvailability(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  return holdAvailability(S, { op: "Pause", states: PAUSABLE_STATES, canField: "can_pause",
    flag: S.canPause, reported: S.pauseReported === true });
}

/** Whether Resume may be offered (see holdAvailability). Resume returns the vehicle to AUTO
 *  through Scout's own transaction; the station never sends AUTO itself to emulate it. */
export function resumeAvailability(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  return holdAvailability(S, { op: "Resume", states: RESUMABLE_STATES, canField: "can_resume",
    flag: S.canResume, reported: S.resumeReported === true });
}

/**
 * Whether Stop can be offered at all, and why not.
 *
 * Three distinct answers, because collapsing them would be a lie in either direction:
 *   unsupported — this Scout has no Stop endpoint (`can_stop` absent from its status). The
 *                 control is shown DISABLED with that reason, never hidden and never faked
 *                 from a low-level LOITER plus operator-side state.
 *   supported, not now — Scout has Stop and says `can_stop:false` right now.
 *   supported, enabled — Scout says `can_stop:true`.
 */
export function stopAvailability(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  if (!S.supported || !S.present) {
    return { supported: false, enabled: false,
      reason: "Scout mission-execution status is unavailable — Stop cannot be offered" };
  }
  if (S.canStop === null) {
    return { supported: false, enabled: false,
      reason: "This Scout does not implement POST /agent/mission_execution/stop yet. Stop is " +
        "unavailable — it is not emulated from a LOITER command, and Rearm is not a " +
        "substitute. Pause holds the mission without ending it." };
  }
  if (S.replanning.active) {
    return { supported: true, enabled: false,
      reason: "The replanning controller owns the vehicle" };
  }
  if (S.activeOperationId || S.transitional) {
    return { supported: true, enabled: false, reason: "Scout is mid-transaction" };
  }
  if (S.canStop !== true) {
    return { supported: true, enabled: false,
      reason: `Scout reports can_stop=false in ${S.state || "its current state"}` };
  }
  return { supported: true, enabled: true, reason: null };
}

/**
 * THE state-driven lifecycle control set for the Map's Agent Mission card.
 *
 * This is the mapping the product decision specifies, and it is derived from SCOUT'S STATUS
 * ONLY — never from the last click, never from the previous label:
 *
 *   READY / NOT_STARTED       [ Start Mission ]
 *   RUNNING / RETURNING_HOME  [ Pause Mission ]  [ Stop Mission ]
 *   PAUSED                    [ Resume Mission ]  [ Stop Mission ]
 *   STOPPED / CANCELLED       [ Start Mission ]
 *   FAILED / SUSPENDED        failure shown + [ Rearm Mission Controller ]  [ Take Control ]
 *   COMPLETED_HOLD            completed + final LOITER shown; a fresh Start only AFTER the
 *                             controller has been rearmed for a new execution
 *
 * A RUNNING mission's primary button is "Pause Mission" and never "Resume" — Resume exists only
 * after a Pause, and labelling a live mission's button Resume invites an operator to press it
 * believing the mission is stopped.
 *
 * WHICH control exists comes from the STATE; whether it is ENABLED comes from Scout's `can_*`
 * flag WHEN SCOUT SENDS ONE (pauseAvailability / resumeAvailability). A Scout that omits the flag
 * has said nothing, so the control is offered and Scout arbitrates the write — it is not turned
 * into a dead button carrying a refusal Scout never issued.
 *
 * @param status    normalized (or raw) Scout mission-execution status
 * @param opts.busy an operation is in flight from THIS station — every button is disabled and
 *                  no second submission is possible
 * @param opts.startBlocked  the backend preflight's verdict (Start preconditions unmet)
 * @param opts.startBlockedReason  the preflight's own words for why
 * @returns {{ buttons: Array, state, stateLabel, tone, failure, complete, notice }}
 */
export function lifecycleControls(status, {
  busy = false, startBlocked = false, startBlockedReason = null,
} = {}) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const state = String(S.state || "").toUpperCase();
  const stop = stopAvailability(S);
  const pause = pauseAvailability(S);
  const resume = resumeAvailability(S);
  const rearm = rearmAvailability(S);
  const failure = (state === "FAILED" || state === "SUSPENDED")
    ? (asText(S.lastError) || `Scout reports ${state}`) : null;
  const complete = isComplete(S);
  const out = { buttons: [], state: S.state, stateLabel: stateLabel(S.state), tone: "idle",
    failure, complete, notice: null, stop };

  const btn = (action, label, { enabled, reason = null, tone = "ok", kind = "primary" }) =>
    ({ action, label, enabled: enabled && !busy, tone, kind,
      reason: busy ? "An operation is already in progress" : reason });

  if (!S.supported) {
    out.notice = "Mission lifecycle not supported by this Scout version.";
    return out;
  }
  if (!S.reachable || !S.present) {
    out.notice = "Scout mission-execution status is unavailable — no lifecycle action can be " +
      "offered. Nothing about the mission is assumed.";
    return out;
  }
  if (S.replanning.active) {
    out.tone = "caution";
    out.notice = "The replanning controller owns the vehicle" +
      (replanFsm(S) ? ` (replanning FSM: ${replanFsm(S)})` : "") +
      " — mission lifecycle actions stay disabled until Scout hands control back.";
    return out;
  }
  // RETURNING_HOME is transitional for the purposes of "Scout is moving through a sequence",
  // but it is NOT a moment in which the operator must be denied control: the mission is still
  // under way and Pause / Stop remain meaningful there. Every other transitional state is a
  // step INSIDE one of Scout's own transactions, where a competing command would be unsafe.
  const inFlight = S.activeOperationId || (S.transitional && state !== "RETURNING_HOME");
  if (inFlight) {
    out.tone = "caution";
    out.notice = S.activeOperationId
      ? `Scout is processing operation ${S.activeOperationId}.`
      : "Scout is mid-transaction.";
    return out;
  }

  const startReason = startBlocked
    ? (asText(startBlockedReason) || "Start preconditions are not met")
    : null;
  const addStart = (label = "Start Mission", extraReason = null) => out.buttons.push(
    btn("start", label, { enabled: !startBlocked && !extraReason,
      reason: extraReason || startReason, tone: "ok" }));
  const addStop = () => out.buttons.push(
    btn("stop", "Stop Mission", { enabled: stop.enabled, reason: stop.reason, tone: "warn",
      kind: "secondary" }));

  if (state === "RUNNING" || state === "RETURNING_HOME") {
    out.tone = "ok";
    // NEVER "Resume" here. The mission is running; Resume is meaningful only after a Pause.
    out.buttons.push(btn("pause", "Pause Mission", { enabled: pause.enabled, tone: "ok",
      reason: pause.reason }));
    addStop();
    return out;
  }
  if (state === "PAUSED") {
    out.tone = "caution";
    out.buttons.push(btn("resume", "Resume Mission", { enabled: resume.enabled, tone: "ok",
      reason: resume.reason }));
    addStop();
    return out;
  }
  if (state === "FAILED" || state === "SUSPENDED") {
    out.tone = "warn";
    if (rearm.available) {
      out.buttons.push(btn("rearm", "Rearm Mission Controller",
        { enabled: rearm.enabled, reason: rearm.reason, tone: "caution", kind: "secondary" }));
    }
    // Take Control is offered here as an explicit manual override — a failed or suspended run
    // is exactly when an operator may need the wheel back by hand. It is NOT part of any
    // automatic lifecycle transaction.
    out.buttons.push(btn("take-control", "Take Control",
      { enabled: S.authority !== "OPERATOR", tone: "caution", kind: "secondary",
        reason: S.authority === "OPERATOR" ? "The operator already holds control" : null }));
    return out;
  }
  if (state === "COMPLETED_HOLD") {
    out.tone = complete ? "ok" : "caution";
    out.notice = complete
      ? "Mission complete — Scout reports COMPLETED_HOLD with a verified final LOITER."
      : "Scout reports COMPLETED_HOLD but the final LOITER is NOT verified.";
    if (rearm.available) {
      out.buttons.push(btn("rearm", "Rearm Mission Controller",
        { enabled: rearm.enabled, reason: rearm.reason, tone: "caution", kind: "secondary" }));
    }
    // A new Start is deliberate and comes SECOND: the controller must be prepared for a fresh
    // execution first, so Start stays disabled until Scout leaves COMPLETED_HOLD.
    addStart("Start Mission",
      "Rearm the mission controller first — a completed run must be prepared for a fresh " +
      "execution before a new Start.");
    return out;
  }
  if (STARTABLE_STATES.includes(state) || state === "NOT_READY") {
    out.tone = STOPPED_STATES.includes(state) ? "caution" : "idle";
    addStart();
    return out;
  }

  out.notice = `Scout reports ${S.state || "no state"} and offers no lifecycle action.`;
  return out;
}

// ---- The COMPACT Agent Mission card (the Map is an operational surface) -----------------
//
// The Map is where a mission is OPERATED; the Agent page is where it is EXPLAINED. The card
// model below renders the same status-derived truth as lifecycleControls, reduced to what an
// operator needs at a glance: one state chip, ONE short line, at most two identity rows and at
// most ONE concise blocker.
//
// Nothing is discarded to achieve that. Every short form is produced as a PAIR — the visible
// text plus the full explanation, which the card carries in the element's `title`. The complete
// evidence (readback hashes, planning-package state, authority orchestration, replanning FSM)
// stays on the Agent diagnostics page. Authority is deliberately NOT a field here: it is shown
// once, in the inspector's Status area, and repeating it in this card is what made three
// different sections narrate the same fact.

// States in which the card shows a live "MODE · WP c / n" line instead of the identity rows —
// a running or held mission is watched, not identified.
export const RUNNING_LIKE_STATES = ["RUNNING", "RETURNING_HOME", "PAUSED"];

/** A long explanation reduced to its first clause and capped. The visible half of a
 *  short/tooltip pair — never used without the full text being carried in the tooltip. */
export function firstClause(text, max = 44) {
  const t = asText(text);
  if (!t) return null;
  const s = (t.split(/(?:[.;·]|\s—\s|\n)/)[0] || t).trim() || t.trim();
  return s.length <= max ? s : `${s.slice(0, max - 1).trimEnd()}…`;
}

/**
 * The SHORT visible text for a blocked Start. The preflight's own words — package consistency,
 * Pixhawk readback evidence, authority orchestration — are the tooltip, never the card body.
 * Matching is on the reason Scout/the backend actually sent; an unrecognised one is shortened
 * rather than replaced, so a new backend message is still reported honestly.
 */
export function shortStartBlocker(reason) {
  const t = asText(reason);
  if (!t) return "Start preconditions not met";
  if (/no active mission/i.test(t)) return "No active mission";
  if (/disabled/i.test(t)) return "Mission execution disabled on Scout";
  // "Agent is replanning" is NOT derivable from text. The preflight's own wording routinely
  // contains the word — "Scout replanning readiness", the readiness check's label — and matching
  // on it is what made a passive readiness refresh announce an active replan every ~10 s while
  // the vehicle sat DISARMED in MANUAL at waypoint 0. The replanning HEADLINE is shown from
  // Scout's explicit evidence only (isReplanning / missionCardView); this function reports the
  // READINESS check by its own meaning: unconfirmed, not running.
  if (/replanning readiness/i.test(t)) return "Replanning readiness not confirmed";
  if (/authority/i.test(t)) return "Control authority not verified";
  if (/package|readback|hash|verif|consisten/i.test(t)) return "Mission verification unavailable";
  if (/position/i.test(t)) return "Position not usable";
  if (/unavailable|unreachable|could not be read|not supported/i.test(t))
    return "Waiting for Scout mission status";
  return firstClause(t);
}

// ---- A failed Start, as ONE compact actionable error ---------------------------------------
//
// The Start transaction's own preflight is the authoritative one, and when it refuses, it refuses
// with everything it knows: up to five precondition checks, each with its own sentence. Printing
// all of them on the Map produced a stack of competing warning lines nobody could act on. The
// operator needs exactly two things at that moment — that the mission did not start, and the ONE
// fact that has to change. Everything else is the tooltip and the Agent diagnostics page.
export const START_FAILURE_TITLE = "Mission could not start";

// blocker/check text → the ONE sentence. Ordered: the read-back is checked first because the
// package hash chain and Scout's replanning readiness are both ANCHORED on it, so a failed
// read-back reliably produces all three blockers and only the first one is the cause.
const START_FAILURE_RULES = [
  [/read-?back/i, "Pixhawk mission readback could not be verified."],
  [/planning package/i, "The planning package is not consistent with the approved mission."],
  // "mission record" only — never a bare /VERIFIED/, which also appears inside
  // AUTHORITY_NOT_VERIFIED, LOITER_NOT_VERIFIED and AUTO_NOT_VERIFIED and would mis-attribute
  // all three to the mission upload.
  [/mission record/i, "The mission upload is not verified."],
  [/replanning readiness/i, "Scout replanning readiness is not confirmed."],
  [/replanning controller|REPLANNING_ACTIVE/i, "The replanning controller owns the vehicle."],
  [/authority/i, "Control authority could not be verified."],
  [/start eligibility|can_start/i, "Scout would not accept a Start in its current state."],
  [/no active (persisted )?mission/i, "This vehicle has no active mission."],
  [/position/i, "Position is stale or invalid."],
];

/**
 * ONE compact, actionable error for a Start that did not happen, plus the full evidence for the
 * tooltip. Returns null for a Start that is still running or that succeeded.
 *
 * `blocked` is called out for what it is: the OPERATOR backend refused before Scout was
 * contacted, so NOTHING reached the vehicle. That is a materially different thing to tell an
 * operator than "the vehicle failed", and it is the outcome a failed preflight produces.
 *
 * @param view  interpretTransaction() output for the start operation
 * @returns {{ title, text, detail, blocked }|null}
 */
export function startFailure(view) {
  if (!isObj(view)) return null;
  const outcome = str(view.outcome);
  if (!outcome || outcome === OUTCOME.ACCEPTED || outcome === "pending") return null;

  const blockers = Array.isArray(view.blockers) ? view.blockers.map(asText).filter(Boolean) : [];
  const code = str(view.code);
  const message = asText(view.message);
  const haystack = [...blockers, code, message].filter(Boolean).join(" · ");

  let text = null;
  for (const [re, sentence] of START_FAILURE_RULES) {
    if (re.test(haystack)) { text = sentence; break; }
  }
  if (!text && code) {
    const t = errorText(code);
    text = t && t !== code ? `${t}.` : `Scout reported ${code}.`;
  }
  if (!text) text = firstClause(message, 72) || `The Start was ${outcomeLabel(outcome).toLowerCase()}.`;

  const blocked = outcome === OUTCOME.BLOCKED;
  const detail = [
    blocked ? "The operator backend refused the Start before Scout was contacted — no vehicle "
      + "write was issued." : null,
    code ? (errorText(code) && errorText(code) !== code ? `${code} — ${errorText(code)}` : code)
      : null,
    message && message !== code ? message : null,
    ...blockers,
  ].filter(Boolean).join(" · ");

  return { title: START_FAILURE_TITLE, text, detail: detail || text, blocked };
}

/** Abbreviated mission id for a narrow row; the full id is always the row's tooltip. */
export function shortMissionId(id) {
  const s = str(id);
  if (!s) return null;
  return s.length <= 14 ? s : `${s.slice(0, 10)}…${s.slice(-4)}`;
}

// The PRE-START resting states, in which the card's chip answers "may this mission be started?"
// rather than repeating Scout's internal wording. Scout reports NOT_READY whenever authority is
// not yet LOCAL_AGENT — the very condition the Start transaction resolves as its first phase —
// so a card that printed NOT_READY beside an enabled Start Mission button was showing the
// operator two contradictory answers to one question. Scout's own state stays in the tooltip.
export const READINESS_CHIP_STATES = ["READY", "NOT_READY", "NOT_STARTED"];

// The readiness verdict → chip / headline. TWO values, because the gate has two: Start is either
// offered or it is withheld for a stable, nameable reason. There is no third "we are still
// finding out" chip, because the station is no longer perpetually finding out.
const READINESS_CHIP = {
  READY: { chip: "READY", tone: "ok", headline: "Ready to start" },
  NOT_READY: { chip: "NOT_READY", tone: "idle", headline: "Not ready to start" },
};

/**
 * THE compact Agent Mission card model.
 *
 * @param status   normalized (or raw) Scout mission-execution status
 * @param opts.busy / startBlocked / startBlockedReason  as lifecycleControls
 * @param opts.missionId          fallback identity when Scout reports none (the backend
 *                                preflight knows the active persisted mission)
 * @param opts.unavailableDetail  the tooltip for an unreadable status (where it was read from)
 * @param opts.readiness          the pre-start view (lib/mission-readiness.js readinessView) —
 *                                { state, canStart, checking, reason, detail }. Optional:
 *                                without it the card presents Scout's raw state, which is what
 *                                the Agent diagnostics page wants.
 * @param opts.starting           a START is in flight from this station. Drives the phase-
 *                                specific neutral progress line (Checking mission readiness… →
 *                                Taking agent control… → Holding position… → Setting and
 *                                verifying Home… → Starting AUTO…).
 * @param opts.homeVerified       tri-state override for "is Home verified" when the caller has a
 *                                better source than Scout's mission-execution status (the fleet
 *                                payload's continuously-reported home_status).
 * @param opts.preflight          the INFORMATIONAL one-shot preflight note (preflightNote), or
 *                                null. Shown as a note; it never affects buttons.
 * @returns {{ chip, tone, headline, headlineTitle, detail, rows, blocker, buttons, working,
 *             startPhase, checking, checkingText, home, info, stop, complete, state, present }}
 *   headline  ONE short status sentence — "Ready to start", or "AUTO · WP 4 / 41" while running
 *   rows      identity only ({k, v, title, mono}); NEVER an authority row
 *   blocker   at most ONE { text, title, tone } — several long failures are never stacked
 *   home      the neutral pre-start Home line ({ text, title }) or null
 *   checking  a one-shot preflight refresh is in flight: render a small spinner. It is NOT
 *             `working` (a Scout transaction), NOT replanning, and it NEVER changes a button.
 */
export function missionCardView(status, {
  busy = false, startBlocked = false, startBlockedReason = null,
  missionId = null, unavailableDetail = null, readiness = null,
  starting = false, homeVerified = null, preflight = null,
} = {}) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const ctl = lifecycleControls(S, { busy, startBlocked, startBlockedReason });
  const state = String(S.state || "").toUpperCase();
  const seq = S.sequence;
  const wp = (seq.current == null && seq.count == null) ? null
    : `${seq.current == null ? "?" : seq.current} / ${seq.count == null ? "?" : seq.count}`;

  const rv = isObj(readiness) ? readiness : null;
  const pf = isObj(preflight) ? preflight : null;
  const out = {
    state: S.state, present: S.present, tone: ctl.tone, chip: state || "—",
    headline: null, headlineTitle: asText(ctl.notice), rows: [], blocker: null,
    buttons: ctl.buttons, working: false, startPhase: null, checking: false, checkingText: null,
    home: null, info: null, stop: ctl.stop, complete: ctl.complete,
  };

  // Nothing may be claimed about an older or unreachable Scout — and the operator is told so in
  // four words, with where the read failed in the tooltip.
  if (!S.supported) {
    out.chip = "UNSUPPORTED";
    out.headline = "Mission lifecycle unsupported";
    return out;
  }
  if (!S.reachable || !S.present) {
    out.chip = "STATUS UNAVAILABLE";
    out.headline = "Waiting for Scout mission status";
    out.headlineTitle = asText(unavailableDetail)
      || "Mission lifecycle status could not be read from the Scout Local Agent.";
    return out;
  }
  // THE ONE PLACE this card says "replanning" — and only on Scout's explicit evidence
  // (normalizeStatus → isReplanning). A readiness refresh, a mission read-back refresh, a
  // pending status request, NOT_READY, stale evidence or a missing field never reach here.
  if (S.replanning.active) {
    out.headline = "Agent is replanning";
    return out;
  }
  // A Start in flight, or any Scout transaction. While the START transaction runs the line is
  // PHASE-SPECIFIC and NEUTRAL — the five phases in lib/mission-readiness.js, each of them
  // Scout's observed step (or, before Scout has moved, the backend's provable first phase).
  // Nothing here is a warning: a Start in progress is the system doing what was asked.
  if (starting || S.activeOperationId || (S.transitional && state !== "RETURNING_HOME")) {
    out.working = true;
    if (starting || isStartTransactionState(state)) {
      const ph = startPhase(state);
      out.startPhase = ph.phase;
      out.headline = ph.text;
    } else {
      out.headline = stateLabel(S.state);
    }
    return out;
  }

  // A one-shot preflight refresh (explicit Refresh, a read after an upload/sync/reconnect) is a
  // PASSIVE, INFORMATIONAL verification: a small spinner and nothing else. It never withdraws a
  // button — `readiness.canStart` comes from the stable gate and is untouched by this flag — and
  // it is never a warning and never replanning. That separation is the whole anti-flicker rule.
  if (rv && rv.checking) {
    out.checking = true;
    out.checkingText = CHECKING_TEXT;
  }

  if (RUNNING_LIKE_STATES.includes(state)) {
    // Watching, not identifying: the live mode and waypoint ARE the status line.
    const parts = [];
    if (S.mode) parts.push(S.mode);
    if (wp) parts.push(`WP ${wp}`);
    out.headline = parts.length ? parts.join(" · ") : stateLabel(S.state);
  } else {
    out.headline = stateLabel(S.state);
    // Pre-start: the chip, the headline and the Start button are ONE derivation, so the card can
    // never show NOT_READY beside an enabled Start. Scout's own state is kept in the tooltip —
    // nothing is hidden, it is simply no longer the answer to the question the chip asks.
    if (rv && READINESS_CHIP_STATES.includes(state)) {
      const pres = READINESS_CHIP[rv.state] || READINESS_CHIP[READINESS.NOT_READY];
      out.chip = pres.chip;
      out.tone = pres.tone;
      out.headline = pres.headline;
      out.headlineTitle = [`Scout reports ${S.state}`, asText(rv.detail), asText(ctl.notice)]
        .filter(Boolean).join(" — ");
    }
    const mid = shortMissionId(S.missionId || missionId);
    if (mid) out.rows.push({ k: "Mission", v: mid, title: S.missionId || missionId, mono: true });
    if (wp) out.rows.push({ k: "WP", v: wp, title: null });

    // HOME, before Start. The Start transaction sets Home to the launch position and verifies it
    // as one of its own phases, so an unverified Home here is a step that has not happened yet —
    // not a defect, and (unless Scout explicitly declares otherwise) not a reason to withhold
    // Start. It is stated once, neutrally, instead of being repeated as a warning.
    const verified = homeVerified === true || homeVerified === false
      ? homeVerified : isObj(S.home.verified);
    if (!verified) {
      out.home = S.home.requiredBeforeStart
        ? { text: "Home must be verified before Start", tone: "warn",
            title: "This Scout declares that it requires an already-verified Home before it will " +
              "enter the Start transaction." }
        : { text: HOME_DURING_START_NOTE, tone: null,
            title: START_HOME_NOTE };
    }
  }

  // The one-shot preflight, as INFORMATION. It is never a button state and never a blocker: the
  // Start transaction re-proves all of it, fresh and fail-closed, before any vehicle write.
  if (pf) {
    out.info = pf.ok === true
      ? { text: "Readiness checks passed", tone: null, title: asText(pf.detail) }
      : pf.ok === null
        ? { text: "Readiness could not be checked", tone: null, title: asText(pf.detail) }
        : { text: `Last check: ${shortStartBlocker(pf.reason)}`, tone: null,
            title: asText(pf.detail) || asText(pf.reason) };
  }

  // Exactly ONE blocker, in the order an operator needs it. `busy` is not a blocker: every
  // button is already disabled and the card is showing the operation it is waiting on.
  if (!busy) {
    const btn = (a) => ctl.buttons.find((b) => b.action === a);
    const start = btn("start"), stop = btn("stop");
    const held = ["pause", "resume"].map(btn).find((b) => b && !b.enabled);
    if (ctl.failure) {
      out.blocker = { text: firstClause(ctl.failure), title: asText(ctl.failure), tone: "warn" };
    } else if (start && !start.enabled) {
      // ONE short line naming a STABLE cause — disconnected, no mission, replanning, already
      // running, Rearm first. It cannot say "verifying", "unproven" or "stale", because none of
      // those can withdraw Start any more.
      out.blocker = { text: shortStartBlocker(start.reason), title: asText(rv && rv.detail)
        || asText(start.reason), tone: "warn" };
    } else if (stop && !stop.enabled) {
      out.blocker = {
        text: ctl.stop && ctl.stop.supported === false
          ? "Stop unavailable on this Scout" : firstClause(stop.reason),
        title: asText(stop.reason), tone: null };
    } else if (held) {
      out.blocker = { text: firstClause(held.reason), title: asText(held.reason), tone: null };
    }
  }
  return out;
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
  // Authority is deliberately NOT listed as a blocker. Mission execution does need LOCAL_AGENT,
  // but the Start transaction acquires and verifies it as its first phase — telling the
  // operator to go and press Release Control is exactly the manual authority management this
  // station no longer requires. It is reported as information, not as something to fix.
  if (S.state && !["READY", "NOT_READY", "NOT_STARTED", ...STOPPED_STATES].includes(S.state)
      && !S.transitional)
    out.push(`Scout is in ${S.state}, not READY`);
  else if (S.state === "NOT_READY")
    out.push("Scout reports NOT_READY — mission, planning package, position or authority is not in place");
  const lastError = asText(S.lastError);
  if (lastError) out.push(`Scout last error: ${lastError}`);
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
  // asText, not str(): `data.error` and Scout's nested error are frequently objects, and
  // String()-ing one puts "[object Object]" where the operator expects a reason.
  const message = asText(data.scout_error_message)
    || (isObj(scout.error) ? asText(first(scout.error, "message", "detail")) : null)
    || asText(data.error);

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

/**
 * Interpret an ORCHESTRATED TRANSACTION response (POST .../mission-execution/{start|pause|
 * resume|stop}) — the envelope carrying the authority-transfer and Scout phases of ONE
 * operator intent.
 *
 * Distinct from interpretOperation above because a transaction has an outcome the raw proxy
 * cannot have: `blocked`, meaning the OPERATOR refused it and nothing reached the vehicle.
 * Presenting that as "rejected" would tell the operator Scout answered when Scout was never
 * contacted.
 */
export function interpretTransaction(res) {
  const data = isObj(res && res.data) ? res.data : (isObj(res) ? res : {});
  const phases = Array.isArray(data.phases) ? data.phases : [];
  const authority = isObj(data.authority) ? data.authority : {};
  const restore = phases.find((p) => p && p.phase === "authority-restore") || null;
  const verify = phases.find((p) => p && p.phase === "verification") || null;
  return {
    outcome: str(data.outcome) || OUTCOME.UNKNOWN,
    operation: str(data.operation),
    // asText everywhere: these fields carry Scout's structured errors and must never reach the
    // DOM through object coercion.
    message: asText(data.error) || asText(data.scout_error_message),
    code: str(data.error_code) || str(data.scout_error_code),
    missionId: str(data.mission_id),
    resultingState: str(data.resulting_state),
    verifiedMode: str(data.verified_mode),
    blockers: Array.isArray(data.blockers) ? data.blockers.map((b) => asText(b)).filter(Boolean) : [],
    phases: phases.map((p) => ({
      phase: str(p.phase), status: str(p.status), detail: asText(p.detail),
      verified: p.verified ?? null, requested: str(p.requested), observed: str(p.observed),
    })),
    authority: {
      before: str(authority.before), after: str(authority.after),
      required: str(authority.required), verified: authority.verified ?? null,
    },
    authorityRestored: restore ? restore.restored === true : null,
    authorityNote: restore ? asText(restore.detail) : null,
    verified: verify ? (verify.verified ?? null) : null,
    verificationNote: verify ? asText(verify.detail) : null,
    reconciliation: isObj(data.reconciliation) ? data.reconciliation : null,
    supported: data.supported !== false,
    httpStatus: (res && typeof res.status === "number") ? res.status : null,
  };
}

/** One-line operator summary of a transaction: its outcome, its reason, and — because the
 *  authority hand-off is part of the SAME operation — what happened to authority. */
export function transactionSummary(view) {
  if (!isObj(view)) return "—";
  const parts = [outcomeLabel(view.outcome)];
  if (view.code) {
    const t = errorText(view.code);
    parts.push(t && t !== view.code ? `${view.code} — ${t}` : view.code);
  }
  if (view.message && view.message !== view.code) parts.push(view.message);
  if (view.resultingState) parts.push(`state ${view.resultingState}`);
  // ACCEPTED IS NOT VERIFIED. The backend re-reads Scout's canonical status after every accepted
  // pause / resume (mission_lifecycle._verify_state) and answers `withheld` when the vehicle is
  // not where the operation says it should be — PAUSED in LOITER, RUNNING in AUTO. That verdict
  // was computed, returned and parsed here, and then shown to nobody: the operator read a bare
  // "Accepted" for a Pause whose LOITER had not been confirmed. It is part of the summary now.
  if (view.verified === false) {
    parts.push(view.verificationNote
      || "Scout accepted it, but the resulting state could NOT be verified");
  }
  if (view.authorityRestored === true) parts.push("authority returned to OPERATOR");
  else if (view.authorityRestored === false && view.authorityNote) parts.push(view.authorityNote);
  if (view.outcome === OUTCOME.UNKNOWN && view.reconciliation) {
    parts.push(`reconciled: ${view.reconciliation.resolved}`
      + (view.reconciliation.detail ? ` — ${asText(view.reconciliation.detail)}` : ""));
  }
  return parts.filter(Boolean).join(" · ");
}

/** Operator-facing label + tone for an operation outcome. "unknown" is deliberately NOT
 *  "failed", and "failed" is deliberately NOT rounded up to a success because HTTP said 200.
 *  "blocked" is its own word: the OPERATOR refused it and nothing reached the vehicle. */
export function outcomeLabel(outcome) {
  return ({
    accepted: "Accepted",
    failed: "Failed on the vehicle",
    rejected: "Rejected by Scout",
    blocked: "Blocked by the Operator — not sent",
    unknown: "Unknown — reconciling",
    unavailable: "Unavailable",
    unsupported: "Not supported",
    pending: "Sending…",
  })[outcome] || "—";
}

export const OUTCOME_TONE = {
  accepted: "ok", failed: "warn", rejected: "caution", blocked: "caution",
  unknown: "caution", unavailable: "idle", unsupported: "idle", pending: "caution",
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
