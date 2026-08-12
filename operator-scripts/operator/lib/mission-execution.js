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
  "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
  "STOP_VERIFYING_MISSION", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
  "STOP_VERIFYING_REWIND", "STOP_RESETTING", "STOP_VERIFYING_RESET",
  "STOPPED", "CANCELLED",
  "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "FINAL_HOLD_REQUESTED", "COMPLETED_HOLD",
  "SUSPENDED", "FAILED",
];

// ── STOP: Scout's own SAFE-ABORT transaction ───────────────────────────────────────────────
// Stop is a first-class Scout lifecycle operation, NOT a raw Pixhawk stop and NOT a mission
// deletion. Scout performs the whole sequence itself:
//
//   verified LOITER → verify the active mission identity → restore the immutable ORIGINAL
//   mission if a verified revised route is installed → rewind the original to its start →
//   verify the rewind → reset mission-execution / replan / test state → clear the simulated
//   experiment injection → invalidate the prior runtime Home → return supervisory authority to
//   OPERATOR → re-prove the mission evidence
//
// The station forwards ONE intent and renders Scout's own evidence. It never sends a LOITER, a
// mission upload, a rewind, a reset or an authority write to emulate any step, and it never
// offers the legacy raw /nav/stop.
//
// A SUCCESSFUL Stop normally comes to rest at state=NOT_READY with start_eligible=true and
// authority_blocks_start=true. That is the EXPECTED landing — authority is deliberately back
// with the operator — and must never be presented as a mission failure. Start stays available,
// because the Start transaction is what hands authority to the Local Agent again.
export const STOP_IN_TRANSACTION_STATES = [
  "STOP_REQUESTED", "STOP_HOLD_REQUESTED", "STOP_HOLD_CONFIRMED",
  "STOP_VERIFYING_MISSION", "STOP_RESTORING_ORIGINAL", "STOP_REWINDING",
  "STOP_VERIFYING_REWIND", "STOP_RESETTING", "STOP_VERIFYING_RESET",
];
// Terminal "the run was deliberately ended" states Scout may REST in. Under the current contract
// it normally settles in NOT_READY instead, with the verdict in its `stop` evidence block — so
// nothing here may require STOPPED in order to call a Stop successful.
export const STOPPED_STATES = ["STOPPED", "CANCELLED"];
// Resting states a mission has not (or no longer) been started from.
export const STARTABLE_STATES = ["READY", "NOT_STARTED", ...STOPPED_STATES];

// Scout's structured Stop failure codes. Each is raised AFTER the vehicle is safely holding —
// Scout reaches a verified LOITER before it restores, rewinds or resets anything — so a Stop
// that fails with one of these leaves the vehicle HELD and the reset INCOMPLETE.
export const STOP_ERROR_CODES = [
  "STOP_ACTIVE_MISSION_UNKNOWN", "STOP_RESTORE_UPLOAD_FAILED", "STOP_RESTORE_HASH_MISMATCH",
  "STOP_REWIND_NOT_VERIFIED", "STOP_HOLD_NOT_VERIFIED", "STOP_MISSION_ID_MISMATCH",
];

// The fields of Scout's `stop` evidence block, in the order the operator reads them.
export const STOP_EVIDENCE_FIELDS = [
  "hold_verified", "original_restored", "active_hash_before", "original_hash", "revised_hash",
  "rewind_verified", "sequence_after", "replan_reset", "experiment_cleared", "authority_after",
  "ready_for_start", "outcome",
];

// The operator-facing progress line for each step of Scout's stop transaction. Each names the
// REAL step Scout is performing, so an operator can see exactly where a Stop stalled — never a
// predicted next state and never a fake percentage.
export const STOP_TRANSITION_LABELS = {
  STOP_REQUESTED: "Stopping mission…",
  STOP_HOLD_REQUESTED: "Holding position…",
  STOP_HOLD_CONFIRMED: "Position hold verified",
  STOP_VERIFYING_MISSION: "Verifying active mission…",
  STOP_RESTORING_ORIGINAL: "Restoring original mission…",
  STOP_REWINDING: "Rewinding mission…",
  STOP_VERIFYING_REWIND: "Verifying rewind…",
  STOP_RESETTING: "Clearing execution and replan state…",
  STOP_VERIFYING_RESET: "Verifying reset…",
};

// The generic first line, used while the POST is in flight and Scout has not yet published a
// state of its own. It is deliberately the same sentence as STOP_REQUESTED: the operator sees
// one continuous progression, not a placeholder replaced by a different word a moment later.
export const STOPPING_TEXT = "Stopping mission…";

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
  ...STOP_TRANSITION_LABELS,
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
  // Scout's ENERGY-feasibility refusals. Each names WHICH of the two questions failed — Scout's
  // Start gate requires both — because "not enough energy" alone does not tell an operator
  // whether the vehicle can still get home.
  INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION:
    "Scout reports insufficient energy to complete the planned mission",
  INSUFFICIENT_ENERGY_FOR_RTL_RETURN:
    "Scout reports insufficient energy to return to the verified RTL Home",
  BATTERY_INVALID: "The battery estimate is unavailable, so energy feasibility cannot be decided",
  POSITION_STALE: "Position data is stale, so energy feasibility cannot be decided",
  RTL_HOME_UNAVAILABLE: "No verified RTL Home is available to evaluate a return against",
  MISSION_UNAVAILABLE: "No mission is available to evaluate",
  ARBITRATION_BUSY: "Another write is in progress on Scout (write arbitration)",
  // Scout's STOP failure codes. Every one of them is raised AFTER the vehicle is safely
  // holding, so each reading says what is true of the vehicle as well as what failed.
  STOP_ACTIVE_MISSION_UNKNOWN:
    "Scout could not identify the active mission, so it did not restore or rewind anything",
  STOP_RESTORE_UPLOAD_FAILED:
    "The original mission could not be uploaded back to the flight controller",
  STOP_RESTORE_HASH_MISMATCH:
    "The restored mission did not read back as the approved original route",
  STOP_REWIND_NOT_VERIFIED:
    "Scout could not verify that the mission was rewound to its start",
  STOP_HOLD_NOT_VERIFIED: "The stop hold (LOITER) could not be verified",
  STOP_MISSION_ID_MISMATCH:
    "Scout is running a different mission than the one the Stop named",
  STOP_NOT_SUPPORTED: "This Scout does not implement the Stop lifecycle operation",
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
// A finite number or null. Anything else — a string, a NaN, a missing key — is NOT a reading.
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
// A PERCENTAGE reading. Negative is the "I do not know" sentinel this fleet actually sends
// (battery_remaining = -1), and rendering that as a battery level is the exact lie the station
// forbids: a flat battery is an emergency, an absent reading is a gap.
const pct = (v) => {
  const n = num(v);
  return n === null || n < 0 ? null : n;
};
// TRI-STATE. `true` / `false` / `null` are three different facts: Scout proved it, Scout
// disproved it, Scout said nothing. Collapsing the third into the second reports a verdict
// Scout never issued.
const bool3 = (v) => (v === true ? true : v === false ? false : null);
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
  "start_eligible", "execution_ready", "authority_blocks_start",
];

// ── ENERGY FEASIBILITY: Scout's CONTINUOUS mission-energy verdict ──────────────────────────
//
// Scout evaluates, on every status, whether the vehicle can still finish the Operator-planned
// mission AND whether it could abort right now and reach its current verified Pixhawk/RTL Home.
// Those are TWO different questions and the station never merges them into one "home margin":
//
//   MISSION MARGIN      can Scout complete the REMAINING operator-planned mission?
//   RTL RETURN MARGIN   can Scout abort NOW and return to the verified RTL Home?
//
// NOTHING HERE IS COMPUTED LOCALLY. The station has no battery model, no range model and no
// reserve policy; it displays Scout's own `mission_feasible` / `rtl_return_feasible` verdicts and
// Scout's own margins. Scout's Start gate requires BOTH verdicts, so the card's compact reading
// must reflect both — a mission that is completable but unreturnable is NOT "FEASIBLE".
export const ENERGY = {
  FEASIBLE: "FEASIBLE",              // both verdicts true
  INSUFFICIENT: "INSUFFICIENT",      // mission_feasible === false
  RTL_INSUFFICIENT: "RTL_INSUFFICIENT", // mission completable, RTL return NOT
  CHECKING: "CHECKING",              // Scout cannot evaluate right now (freshness)
  UNKNOWN: "UNKNOWN",                // Scout evaluated to UNKNOWN, or a verdict is missing
  NONE: "NONE",                      // this Scout reports no energy feasibility at all
};

// Scout's `reason` codes → operator text. PRESENTATION of Scout's own code, exactly like
// ERROR_TEXT: an unrecognised reason is shown as-is rather than replaced.
//
// ORDER IS LOAD-BEARING. energyReasonText() also matches a code EMBEDDED in a longer sentence,
// and "SUFFICIENT_ENERGY" is a substring of "INSUFFICIENT_ENERGY_FOR_…" — so the insufficiency
// codes must be tried first or a deficit would read as sufficiency, which is the one direction
// this mapping must never fail in.
export const ENERGY_REASON_TEXT = {
  INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION: "Insufficient energy for the planned mission",
  INSUFFICIENT_ENERGY_FOR_RTL_RETURN: "Insufficient energy for the RTL return",
  SUFFICIENT_ENERGY: "Sufficient energy for the mission and the RTL return",
  BATTERY_INVALID: "Battery estimate unavailable",
  POSITION_STALE: "Position data stale",
  RTL_HOME_UNAVAILABLE: "RTL Home unavailable",
  MISSION_UNAVAILABLE: "Mission unavailable",
};

/** Operator text for Scout's energy `reason`. Accepts the bare code, a code inside Scout's own
 *  sentence, or a structured {code, message} — and returns the text UNCHANGED when the code is
 *  one this build has not seen, so a new Scout reason is still reported honestly. */
export function energyReasonText(reason) {
  const t = asText(reason);
  if (!t) return null;
  const up = t.toUpperCase();
  if (ENERGY_REASON_TEXT[up]) return ENERGY_REASON_TEXT[up];
  for (const [code, text] of Object.entries(ENERGY_REASON_TEXT)) {
    if (up.includes(code)) return text;
  }
  return t;
}

// The reasons that mean "Scout is waiting for usable input", as opposed to "Scout cannot answer".
// A stale fix resolves itself on the next position; an invalid battery or a missing RTL Home does
// not. Both are NEUTRAL — an unknown is not an emergency and must never render as one.
export const ENERGY_CHECKING_REASONS = ["POSITION_STALE"];

// ── RISK: Scout's own authoritative agent risk level ───────────────────────────────────────
//
// Scout runs the continuous risk model and this station DISPLAYS it. Nothing here computes a
// score, a level, a threshold or a floor, and nothing infers one from energy, comms or anything
// else — a risk level is a claim about the vehicle's situation, and the only component holding
// that situation is the agent. A Scout that reports no risk block reads "—", never LOW.
//
// SCOUT'S PIPELINE, and why only ONE of its outputs may be displayed as THE level:
//
//   weighted continuous score        →  risk.weighted_score / risk.weighted_level
//   + non-compensatory floors        →  risk.component_floor_level / _reason / _source
//   + hard feasibility override      →  risk.hard_constraint_violated / risk.hard_override_level
//   ────────────────────────────────────────────────────────────────────────────────────────
//   = GOVERNING level                →  risk.level          ← the only authoritative one
//
// The floors are NON-COMPENSATORY on purpose: a single severe component raises the governing
// level regardless of how reassuring the weighted average is. Scout's own worked example —
// score 0.2375, weighted_level LOW, component_floor_level HIGH
// (COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION), level HIGH — is exactly the case a
// station that rendered the score would get backwards, so `risk.level` is read directly and the
// score is never mapped to a level here. See riskView().
export const RISK_LEVELS = ["LOW", "ELEVATED", "HIGH", "CRITICAL", "UNKNOWN"];

// Existing card tones only (ok / caution / warn / idle) — no new colour vocabulary. HIGH and
// CRITICAL share the warn colour and are told apart by their label, which is what the card's
// three semantic colours can honestly support.
export const RISK_TONE = {
  LOW: "ok", ELEVATED: "caution", HIGH: "warn", CRITICAL: "warn", UNKNOWN: "idle",
};

// Scout's risk COMPONENTS, in the order the operator reads them. Fixed here so the breakdown
// cannot silently reorder between polls; a component Scout adds that is not in this list is
// still displayed, appended after these (see riskComponents).
export const RISK_COMPONENTS = ["energy", "communication", "navigation", "health", "mission"];

// ── Scout's ADVISORY recommendation → the operator's compact word ──────────────────────────
// DISPLAY ONLY, and deliberately inert. It is not a button, it does not enable or disable a
// control, and it never produces a command: the recommendation is Scout's advice to a human,
// and turning advice into an affordance would make the risk model a control path it was never
// designed or authorised to be. A recommendation this build does not recognise is shown exactly
// as Scout sent it.
//
// CONTINUE / CONTINUE_WITH_CAUTION / RETURN_HOME / HOLD is the current (final) vocabulary.
// Both spellings Scout has used for the same two advisories are recognised (…_RECOMMENDED and
// the bare RETURN_HOME / HOLD), so a build that ships either renders a word rather than a raw
// enum. RETURN_HOME is the ADVICE "bring the vehicle home" — it is not RTL, and it is not the
// safe-return route: what Scout then plans and uploads is a constrained safe-return mission.
// This station never infers HOLD from a risk level or an FSM step: HOLD is shown ONLY when
// Scout's own recommendation field says HOLD (or the older HOLD_RECOMMENDED).
export const RECOMMENDATION_TEXT = {
  CONTINUE: "CONTINUE",
  CONTINUE_WITH_CAUTION: "CAUTION",
  RETURN_HOME: "RETURN HOME",
  HOLD: "HOLD",
  // Backward compatibility — an older Scout's spelling of the same two advisories.
  HOLD_RECOMMENDED: "HOLD",
  RETURN_RECOMMENDED: "RETURN",
};
export const RECOMMENDATION_TONE = {
  CONTINUE: "ok", CONTINUE_WITH_CAUTION: "caution", RETURN_HOME: "warn", HOLD: "warn",
  HOLD_RECOMMENDED: "warn", RETURN_RECOMMENDED: "warn",
};

// Scout's mission/package binding vocabulary, verbatim.
export const BINDING = { UNBOUND: "UNBOUND", BOUND: "BOUND", STALE_MISMATCH: "STALE_MISMATCH" };
// Conflict codes raised when a NEW package arrives against a run Scout cannot replace.
export const PACKAGE_CONFLICT = {
  STALE_PACKAGE_DURING_ACTIVE_EXECUTION: "STALE_PACKAGE_DURING_ACTIVE_EXECUTION",
  OPERATION_IN_PROGRESS: "OPERATION_IN_PROGRESS",
};
const ACTIVE_CONFLICT_CODES = new Set(Object.values(PACKAGE_CONFLICT));

// The ONE sentence for a mission uploaded on top of a run that still owns the vehicle. It names
// only remedies the station can actually perform through Scout's own lifecycle: let the run
// finish, abort it with Stop, or rearm the controller. Nothing here is emulated locally.
export const MISSION_REPLACEMENT_BLOCKED_TEXT =
  "New mission uploaded while another mission is active. Finish the active mission, stop it, or " +
  "rearm the mission controller before starting the new mission.";

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
  const bind = isObj(s.binding) ? s.binding : {};
  const conflict = isObj(s.package_conflict) ? s.package_conflict : {};
  const batt = isObj(s.battery_diagnostics) ? s.battery_diagnostics : {};
  const nrg = isObj(s.energy_feasibility) ? s.energy_feasibility : {};
  const rsk = isObj(s.risk) ? s.risk : {};
  const stopBlk = isObj(s.stop) ? s.stop : {};
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
    // Tri-state for the SAME reason as canPause / canResume above: `true` / `false` / `null` are
    // three different facts. `false` is Scout refusing a stop right now and is shown as its own
    // reason; `null` is Scout saying NOTHING, in which case the lifecycle STATE is the authority
    // (see stopAvailability) and Scout arbitrates the write. `stopSupported` records only whether
    // Scout reported the flag at all — it is evidence for the diagnostics page, not a gate.
    canStop: "can_stop" in s ? s.can_stop === true : null,
    stopSupported: "can_stop" in s,
    // Scout's `stop` EVIDENCE block, verbatim. Booleans stay TRI-STATE (true / false / null):
    // "Scout could not verify the rewind" and "Scout said nothing about the rewind" are
    // different facts, and rounding the second into the first reports a failure Scout never
    // claimed. `reported:false` is what keeps a station that has never seen a Stop from
    // rendering a fabricated one.
    // asText, NOT str(): Scout's outcome and authority arrive as bare strings today, but a
    // structured {code, message} is a legitimate shape for either, and String()-ing one would
    // put the literal "[object Object]" exactly where the operator reads the verdict of an abort.
    stop: {
      reported: Object.keys(stopBlk).length > 0,
      holdVerified: stopBlk.hold_verified ?? null,
      originalRestored: stopBlk.original_restored ?? null,
      activeHashBefore: asText(stopBlk.active_hash_before),
      originalHash: asText(stopBlk.original_hash),
      revisedHash: asText(stopBlk.revised_hash),
      rewindVerified: stopBlk.rewind_verified ?? null,
      sequenceAfter: stopBlk.sequence_after ?? null,
      replanReset: stopBlk.replan_reset ?? null,
      experimentCleared: stopBlk.experiment_cleared ?? null,
      authorityAfter: asText(stopBlk.authority_after),
      readyForStart: stopBlk.ready_for_start ?? null,
      outcome: asText(stopBlk.outcome),
      raw: Object.keys(stopBlk).length > 0 ? stopBlk : null,
    },
    // Scout's EXPLICIT Start-eligibility contract. Tri-state on purpose: PRESENCE is what makes
    // it authoritative, and an older Scout that omits the keys must fall back to the can_start
    // reading rather than have a missing field read as `false` and refuse every Start.
    startEligible: "start_eligible" in s ? s.start_eligible === true : null,
    executionReady: "execution_ready" in s ? s.execution_ready === true : null,
    authorityBlocksStart: "authority_blocks_start" in s ? s.authority_blocks_start === true : null,
    startBlockReason: first(s, "start_block_reason"),
    eligibilityReported: "start_eligible" in s,
    binding: {
      reported: Object.keys(bind).length > 0 || Object.keys(conflict).length > 0,
      state: str(bind.binding_state),
      boundOriginalMissionId: str(bind.bound_original_mission_id),
      packageMissionId: str(bind.package_mission_id),
      packageRouteHash: str(bind.package_route_hash),
      verifiedRouteHash: str(bind.verified_route_hash),
      conflictCode: str(conflict.code),
      conflictExecutionState: str(conflict.execution_state),
      conflict: Object.keys(conflict).length > 0 ? conflict : null,
    },
    // Battery as Scout DIAGNOSES it. battery_valid:false / a -1 raw is UNKNOWN, and `percent`
    // stays null so no consumer can render the sentinel as 0%.
    battery: {
      reported: Object.keys(batt).length > 0,
      valid: batt.battery_valid === true,
      percent: batt.battery_valid === true && typeof batt.battery_percent === "number"
        && batt.battery_percent >= 0 ? batt.battery_percent : null,
      raw: batt.battery_raw ?? null,
      observedAt: str(batt.battery_observed_at),
      telemetryAgeS: typeof batt.telemetry_age_s === "number" ? batt.telemetry_age_s : null,
    },
    // Scout's CONTINUOUS energy-feasibility evaluation, verbatim. Every verdict stays TRI-STATE
    // and every number stays a number or null — a missing margin is not 0%, and an absent
    // `mission_feasible` is not `false`. A Scout that predates the contract reports nothing here
    // and `reported:false` is what keeps the card from inventing a verdict for it.
    energy: {
      reported: Object.keys(nrg).length > 0,
      // asText, NOT str(): Scout's status and reason are bare codes today, but a structured
      // {code, message} is a legitimate shape for either — and String()-ing one would print the
      // literal "[object Object]" exactly where the energy verdict goes.
      status: asText(nrg.status),
      reason: asText(nrg.reason),
      // Scout's own sentence about the verdict ("mission margin 17.27%, RTL return margin
      // 78.92% -- both positive at effective battery 89% (PHYSICAL)."). Shown as-is where
      // there is room for it; it is Scout's words, never reassembled from the numbers here.
      message: asText(nrg.message),
      // Percentages, through pct(): a negative is this fleet's "unknown" sentinel, never a level.
      batteryPercent: pct(nrg.battery_percent),
      batterySource: str(nrg.battery_source),
      physicalBatteryPercent: pct(nrg.physical_battery_percent),
      injectedBatteryPercent: pct(nrg.injected_battery_percent),
      currentSequence: nrg.current_sequence ?? null,
      remainingWaypointCount: nrg.remaining_waypoint_count ?? null,
      plannedHome: isObj(nrg.planned_home) ? nrg.planned_home : null,
      rtlHome: isObj(nrg.rtl_home) ? nrg.rtl_home : null,
      plannedCompletionDistanceM: num(nrg.planned_completion_distance_m),
      rtlReturnDistanceM: num(nrg.rtl_return_distance_m),
      estimatedMissionEnergyPercent: num(nrg.estimated_mission_energy_percent),
      estimatedRtlReturnEnergyPercent: num(nrg.estimated_rtl_return_energy_percent),
      reserveMarginPercent: num(nrg.reserve_margin_percent),
      usableRangeM: num(nrg.usable_range_m),
      // MARGINS are signed by design — a negative margin is the deficit, not an unknown — so
      // they go through num(), never pct().
      missionMarginPercent: num(nrg.mission_margin_percent),
      rtlReturnMarginPercent: num(nrg.rtl_return_margin_percent),
      missionFeasible: bool3(nrg.mission_feasible),
      rtlReturnFeasible: bool3(nrg.rtl_return_feasible),
      missionGeometrySource: str(nrg.mission_geometry_source),
      rtlReturnGeometrySource: str(nrg.rtl_return_geometry_source),
      evaluatedAt: str(nrg.evaluated_at),
      positionAgeS: num(nrg.position_age_s),
      maxPositionAgeS: num(nrg.max_position_age_s),
    },
    // Scout's OWN risk verdict, in full. The station reads every field and derives NONE of
    // them — see RISK_LEVELS for the pipeline these fields come out of.
    //
    // `level` is the GOVERNING level and is read from `risk.level` alone. `weightedLevel` and
    // `score` are the pre-floor inputs: they are kept so the Agent page can EXPLAIN how the
    // governing level was reached, and they are never substituted for it. Both spellings of
    // level/score are accepted so an older Scout that ships `risk_level` needs no code change.
    risk: {
      reported: Object.keys(rsk).length > 0,
      level: asText(first(rsk, "level", "risk_level")),
      score: num(first(rsk, "score", "risk_score")),
      weightedScore: num(rsk.weighted_score),
      weightedLevel: asText(rsk.weighted_level),
      // The non-compensatory severity floor. `component_floor_level` present means ONE
      // component was severe enough to raise the governing level on its own, whatever the
      // weighted average said. Null is "no floor was active", not "no floor exists".
      floorLevel: asText(rsk.component_floor_level),
      floorReason: asText(rsk.component_floor_reason),
      floorSource: asText(rsk.component_floor_source),
      // The hard feasibility override — Scout's mission/RTL feasibility gate outranking the
      // continuous model entirely. TRI-STATE: false is "Scout checked and no hard constraint
      // is violated"; null is "Scout said nothing", and the two must not look alike.
      hardConstraintViolated: bool3(rsk.hard_constraint_violated),
      hardOverrideLevel: asText(rsk.hard_override_level),
      confidence: asText(rsk.confidence),
      recommendation: asText(rsk.recommendation),
      dominantComponent: asText(rsk.dominant_component),
      dominantReason: asText(rsk.dominant_reason),
      feasibilityStatus: asText(rsk.feasibility_status),
      // Scout's own evaluation instant. Kept RAW (its epoch seconds, or whatever Scout sends)
      // because the age shown to the operator is computed against it at render time and must
      // never be confused with the age of our poll — polling creates no freshness.
      evaluatedAt: rsk.evaluated_at ?? null,
      components: isObj(rsk.components) ? rsk.components : null,
      weights: isObj(rsk.weights) ? rsk.weights : null,
      reason: asText(first(rsk, "reason", "detail", "summary")),
    },
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
 * SCOUT'S OWN Start-eligibility verdict, read from the explicit contract when it reports one.
 *
 * `can_start` alone is not the input any more, because it conflated two independent facts and
 * produced the misreading this station had to stop making. Scout does NOT seize LOCAL_AGENT
 * authority by itself, so
 *
 *     start_eligible = true, authority_blocks_start = true
 *
 * is the NORMAL pre-Start condition of a perfectly well-prepared mission — the Start transaction
 * acquires and verifies agent control as its FIRST phase. Presenting it as a broken or unready
 * mission told the operator to go and fix, by hand, the exact thing the button was about to do.
 *
 * @returns {{ eligible, deferredOnAuthority, executionReady, reason, source }}
 *   eligible            Start may be offered (subject to the stable blockers in startGate)
 *   deferredOnAuthority Start is available AND pressing it will take agent control first
 *   executionReady      Scout is ready to run right now, already under LOCAL_AGENT
 *   source              "scout" (the explicit contract) or "can_start" (an older Scout)
 */
export function startEligibility(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const state = String(S.state || "").toUpperCase();
  const no = (reason, source) => ({ eligible: false, deferredOnAuthority: false,
    executionReady: false, reason, source });

  // The same three guards, in the same order, as the backend's start_eligibility(). Each reads a
  // DIFFERENT Scout field, so a status asserting both "eligible" and "the replanning controller
  // owns the vehicle" is self-contradictory — and a contradiction fails closed.
  if (!S.present) return no("Scout mission-execution status is unavailable", "status");
  if (S.replanning.active) return no("The replanning controller owns the vehicle", "replanning");
  if (S.activeOperationId) {
    return no(`Scout is already processing operation ${S.activeOperationId}`, "operation");
  }
  if (S.missionExecutionEnabled === false) {
    return no("Mission execution is disabled on Scout (MISSION_EXECUTION_DISABLED)", "disabled");
  }

  if (S.eligibilityReported) {
    if (S.executionReady === true) {
      return { eligible: true, deferredOnAuthority: false, executionReady: true,
        reason: null, source: "scout" };
    }
    if (S.startEligible === true) {
      return { eligible: true, deferredOnAuthority: S.authorityBlocksStart === true,
        executionReady: false, reason: null, source: "scout" };
    }
    return { eligible: false, deferredOnAuthority: false, executionReady: false,
      // Scout's own words, verbatim — never a re-derivation of its preconditions.
      reason: asText(S.startBlockReason)
        || `Scout reports the mission is not eligible to start${state ? ` in ${state}` : ""}`,
      source: "scout" };
  }
  // An older Scout: the previous reading, with the ONE authority deferral it always had.
  if (S.canStart) {
    return { eligible: true, deferredOnAuthority: false, executionReady: false,
      reason: null, source: "can_start" };
  }
  const authority = String(S.authority || "").toUpperCase();
  if (STARTABLE_STATES.includes(state) || state === "NOT_READY") {
    if (authority && authority !== "LOCAL_AGENT") {
      return { eligible: true, deferredOnAuthority: true, executionReady: false,
        reason: null, source: "can_start" };
    }
  }
  return { eligible: false, deferredOnAuthority: false, executionReady: false,
    reason: `Scout reports can_start=false${state ? ` in ${state}` : ""}`, source: "can_start" };
}

/** The label + note for a Start button, given Scout's eligibility. The label never changes —
 *  an operator looking for "Start Mission" must find "Start Mission" — but when authority will
 *  be acquired the card SAYS so, because that is a real thing the press is about to do. */
export const START_ACQUIRES_AUTHORITY_NOTE =
  "Start will take Local Agent control of this vehicle first, verify it, and then run Scout's " +
  "own start transaction.";

/**
 * Scout's mission/package binding, and whether a newly uploaded mission may be shown as ready.
 *
 * `blocksNewMission` is deliberately narrow and is the whole point: when Scout reports
 * STALE_MISMATCH, or a conflict raised because the PREVIOUS run still owns the vehicle, a new
 * package must NOT quietly render as "ready to start". The station names the situation and the
 * only two real remedies. It does NOT invent a Stop — Scout has none, and a synthesized one
 * would be a second lifecycle competing with Scout's.
 *
 * @returns {{ reported, state, conflictCode, blocksNewMission, text, detail }}
 */
export function bindingView(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const b = S.binding || {};
  if (!b.reported) {
    return { reported: false, state: null, conflictCode: null, blocksNewMission: false,
      text: null, detail: null };
  }
  const blocks = b.state === BINDING.STALE_MISMATCH || ACTIVE_CONFLICT_CODES.has(b.conflictCode);
  const detail = [
    b.state ? `binding ${b.state}` : null,
    b.conflictCode || null,
    b.conflictExecutionState ? `execution ${b.conflictExecutionState}` : null,
    b.boundOriginalMissionId ? `bound mission ${b.boundOriginalMissionId}` : null,
    b.packageMissionId ? `package mission ${b.packageMissionId}` : null,
  ].filter(Boolean).join(" · ");
  return {
    reported: true, state: b.state, conflictCode: b.conflictCode,
    blocksNewMission: blocks,
    text: blocks ? MISSION_REPLACEMENT_BLOCKED_TEXT : null,
    detail: detail || null,
  };
}

/**
 * Battery, for display, from Scout's own diagnostics.
 *
 * The rule this exists to enforce: a `-1` raw (or `battery_valid:false`) is Scout saying it does
 * not KNOW, and "unknown" must never render as 0%. A zero-percent battery is an emergency; a
 * missing reading is a link or sensor gap. The two must not look alike.
 *
 * @returns {{ known, percent, text, detail }}
 */
export function batteryView(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const b = S.battery || {};
  if (!b.reported) return { known: false, percent: null, text: null, detail: null };
  const detail = [
    b.raw === null || b.raw === undefined ? null : `raw ${b.raw}`,
    b.observedAt ? `observed ${b.observedAt}` : null,
    b.telemetryAgeS === null ? null : `telemetry age ${b.telemetryAgeS}s`,
  ].filter(Boolean).join(" · ") || null;
  if (!b.valid || b.percent === null) {
    return { known: false, percent: null,
      text: "Battery telemetry temporarily unavailable", detail };
  }
  return { known: true, percent: b.percent, text: `${b.percent}%`, detail };
}

/** A margin percentage as the card prints it: signed, whole, never a decimal tail. `20.13` is
 *  "+20%", `-7.2` is "-7%". Null in, null out — the caller renders its own placeholder. */
export function energyMarginText(percent) {
  const n = num(percent);
  if (n === null) return null;
  const r = Math.round(n);
  const whole = Object.is(r, -0) ? 0 : r;
  return `${whole >= 0 ? "+" : ""}${whole}%`;
}

/** Scout's energy evidence as ONE readable line for the card's tooltip. Both margins are named
 *  in full — MISSION and RTL RETURN are different questions and neither is "home margin". */
export function energyDetail(energy) {
  const e = isObj(energy) ? energy : {};
  if (!e.reported) return null;
  const m = (v) => (num(v) === null ? null : `${Math.round(v)} m`);
  const p1 = (v) => (num(v) === null ? null : `${v.toFixed(1)}%`);
  const signed1 = (v) => (num(v) === null ? null : `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`);
  const yn = (v) => (v === true ? "yes" : v === false ? "no" : null);
  return [
    energyReasonText(e.reason),
    signed1(e.missionMarginPercent) ? `mission margin ${signed1(e.missionMarginPercent)}` : null,
    signed1(e.rtlReturnMarginPercent)
      ? `RTL return margin ${signed1(e.rtlReturnMarginPercent)}` : null,
    yn(e.missionFeasible) ? `mission feasible ${yn(e.missionFeasible)}` : null,
    yn(e.rtlReturnFeasible) ? `RTL return feasible ${yn(e.rtlReturnFeasible)}` : null,
    m(e.plannedCompletionDistanceM) ? `planned ${m(e.plannedCompletionDistanceM)}` : null,
    m(e.rtlReturnDistanceM) ? `RTL return ${m(e.rtlReturnDistanceM)}` : null,
    e.batteryPercent === null ? null : `battery ${Math.round(e.batteryPercent)}%`
      + (e.batterySource ? ` (${e.batterySource})` : ""),
    p1(e.reserveMarginPercent) ? `reserve ${p1(e.reserveMarginPercent)}` : null,
    e.positionAgeS === null ? null : `position age ${e.positionAgeS.toFixed(1)} s`
      + (e.maxPositionAgeS === null ? "" : ` of ${e.maxPositionAgeS} s`),
    e.evaluatedAt ? `evaluated ${e.evaluatedAt}` : null,
  ].filter(Boolean).join(" · ") || null;
}

/**
 * THE compact ENERGY status, from Scout's authoritative verdicts only.
 *
 * The card answers "can I complete the planned mission?", so the visible percentage is the
 * MISSION margin and never the RTL return margin. But Scout's Start gate requires BOTH verdicts,
 * so the reading must reflect both — a run that can be completed but not returned from is NOT
 * shown as a reassuring "FEASIBLE +20%".
 *
 * Order of decision, and why it is not the order the four cases are usually listed in:
 *   1. mission_feasible === false      an explicit refusal outranks a missing sibling field. A
 *                                      Scout that proved the mission infeasible while it could
 *                                      not evaluate the RTL return has still told the operator
 *                                      something they must act on, and showing UNKNOWN there
 *                                      would hide a proven deficit.
 *   2. rtl_return_feasible === false   likewise explicit, and the one case the compact reading
 *                                      would otherwise misrepresent.
 *   3. anything not proven true        UNKNOWN / CHECKING. Neutral, never an emergency.
 *   4. both proven true                FEASIBLE, with the mission margin.
 *
 * @returns {{ reported, state, text, tone, marginPercent, marginText, reason, reasonText,
 *             missionFeasible, rtlReturnFeasible, detail }}
 */
export function energyView(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const e = S.energy || {};
  const reason = e.reason || null;
  const reasonText = energyReasonText(reason);
  const marginText = energyMarginText(e.missionMarginPercent);
  const base = {
    reported: e.reported === true,
    marginPercent: e.missionMarginPercent ?? null, marginText,
    reason, reasonText,
    missionFeasible: e.missionFeasible ?? null,
    rtlReturnFeasible: e.rtlReturnFeasible ?? null,
    detail: energyDetail(e),
  };

  // An older Scout said NOTHING about energy. "—" is the honest reading; it is not a failure and
  // it must not disable anything.
  if (!e.reported) {
    return { ...base, state: ENERGY.NONE, text: "—", tone: "idle",
      detail: "This Scout does not report mission-energy feasibility." };
  }
  if (e.missionFeasible === false) {
    return { ...base, state: ENERGY.INSUFFICIENT, tone: "warn",
      text: marginText ? `INSUFFICIENT ${marginText}` : "INSUFFICIENT" };
  }
  if (e.rtlReturnFeasible === false) {
    // The mission may well be completable. The vehicle could not get back, and Scout's Start
    // gate refuses on exactly this — so the card says which of the two failed, not "FEASIBLE".
    return { ...base, state: ENERGY.RTL_INSUFFICIENT, tone: "warn", text: "RTL INSUFFICIENT" };
  }
  if (e.missionFeasible === true && e.rtlReturnFeasible === true
      && String(e.status || "").toUpperCase() !== "UNKNOWN") {
    return { ...base, state: ENERGY.FEASIBLE, tone: "ok",
      text: marginText ? `FEASIBLE ${marginText}` : "FEASIBLE" };
  }
  // Neither dimension is proven. NEUTRAL by design: an unknown is a gap in the inputs, not an
  // emergency, and colouring it red would teach the operator to ignore the colour that matters.
  const up = (reason || "").toUpperCase();
  const checking = ENERGY_CHECKING_REASONS.some((c) => up.includes(c));
  return { ...base, state: checking ? ENERGY.CHECKING : ENERGY.UNKNOWN, tone: "idle",
    text: checking ? "CHECKING" : "UNKNOWN" };
}

/**
 * THE compact RISK status — Scout's GOVERNING level, or nothing.
 *
 * `risk.level` is read directly and is the only field that decides what is displayed. It is NOT
 * derived from `risk.score`, and it is NOT `risk.weighted_level`: Scout's governing level is the
 * weighted level raised by any non-compensatory component floor and then by any hard-feasibility
 * override, so the two disagree exactly when it matters most. Scout's own worked example —
 *
 *     score 0.2375 · weighted_level LOW · component_floor_level HIGH
 *     (COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION) · level HIGH
 *
 * — must display HIGH. A station that rendered the score would show LOW for a vehicle whose own
 * agent has assessed it as HIGH, which is the single worst direction this reading can fail in.
 *
 * There is no operator-side risk model and there must not be one. When Scout reports no risk
 * block this reads "—", quietly; it never reads LOW.
 *
 * A level this build does not recognise is displayed AS SENT with a neutral tone and
 * `known:false`, never bucketed into a level the operator would act on.
 *
 * The returned view also carries the EXPLANATION fields (weighted score/level, the floor, the
 * hard override, confidence, the dominant component) so the Agent page can show how the
 * governing level was reached. They are evidence for a human; nothing consumes them as a gate.
 *
 * @returns {{ reported, level, known, text, tone, score, weightedScore, weightedLevel,
 *             floorLevel, floorReason, floorSource, floorActive, hardConstraintViolated,
 *             hardOverrideLevel, confidence, dominantComponent, dominantReason, evaluatedAt,
 *             governedBy, detail }}
 */
export function riskView(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const r = S.risk || {};
  const score = r.score ?? null;
  const explain = {
    score,
    weightedScore: r.weightedScore ?? null,
    weightedLevel: r.weightedLevel || null,
    floorLevel: r.floorLevel || null,
    floorReason: r.floorReason || null,
    floorSource: r.floorSource || null,
    floorActive: !!r.floorLevel,
    hardConstraintViolated: r.hardConstraintViolated ?? null,
    hardOverrideLevel: r.hardOverrideLevel || null,
    confidence: r.confidence || null,
    dominantComponent: r.dominantComponent || null,
    dominantReason: r.dominantReason || null,
    evaluatedAt: r.evaluatedAt ?? null,
  };
  if (!r.reported || !r.level) {
    return { ...explain, reported: r.reported === true, level: null, known: false, text: "—",
      tone: "idle", governedBy: null,
      detail: r.reported === true
        ? "Scout reported a risk block but no governing level — nothing is claimed about risk."
        : "This Scout does not report an agent risk level yet. The operator station never "
          + "computes one." };
  }
  const level = r.level.toUpperCase();
  const known = RISK_LEVELS.includes(level);

  // WHICH stage of Scout's pipeline produced the governing level. Reported, not recomputed:
  // this only names the stage whose level Scout's own fields show matching the governing one,
  // and stays null when they do not — it never re-runs the floor or the override logic.
  const eq = (v) => !!v && String(v).toUpperCase() === level;
  const governedBy = r.hardConstraintViolated === true && eq(r.hardOverrideLevel) ? "hard"
    : eq(r.floorLevel) ? "floor"
      : eq(r.weightedLevel) ? "weighted" : null;

  // The tooltip. Deliberately a short line and NOT the components object: dumping the nested
  // per-component evidence here produced a multi-kilobyte hover on live Scout, which is not a
  // tooltip anyone reads. The breakdown belongs on the Agent page (see riskComponents).
  const detail = [
    known ? null : "Risk level not recognised by this build — shown exactly as Scout sent it",
    r.hardConstraintViolated === true
      ? `hard constraint violated${r.hardOverrideLevel ? ` → ${r.hardOverrideLevel}` : ""}` : null,
    r.floorLevel
      ? `severity floor ${r.floorLevel}${r.floorReason ? ` (${r.floorReason})` : ""}` : null,
    r.weightedLevel || r.weightedScore !== null
      ? `weighted ${r.weightedLevel || "—"}${r.weightedScore === null ? "" : ` ${r.weightedScore}`}`
      : score === null ? null : `score ${score}`,
    r.dominantComponent
      ? `dominant ${r.dominantComponent}${r.dominantReason ? ` (${r.dominantReason})` : ""}` : null,
    r.confidence ? `confidence ${r.confidence}` : null,
    r.reason || null,
  ].filter(Boolean).join(" · ") || `Scout reports risk level ${level}.`;

  return { ...explain, reported: true, level, known, text: level,
    tone: known ? RISK_TONE[level] : "idle", governedBy, detail };
}

/**
 * Scout's ADVISORY recommendation, mapped to the operator's compact word.
 *
 * DISPLAY ONLY. This never becomes a button, never enables or disables a control and never
 * produces a command — see RECOMMENDATION_TEXT. An unrecognised recommendation is shown exactly
 * as Scout sent it, with a neutral tone, rather than being bucketed into one of the four.
 *
 * @returns {{ reported, code, text, tone, known }}
 */
export function recommendationView(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const code = (S.risk && S.risk.recommendation) || null;
  if (!code) return { reported: false, code: null, text: "—", tone: "idle", known: false };
  const up = code.toUpperCase();
  const known = Object.prototype.hasOwnProperty.call(RECOMMENDATION_TEXT, up);
  return {
    reported: true, code: up, known,
    text: known ? RECOMMENDATION_TEXT[up] : up,
    tone: known ? RECOMMENDATION_TONE[up] : "idle",
  };
}

/**
 * Scout's per-component risk breakdown, as a stable ordered list for the diagnostics table.
 *
 * PURE PRESENTATION of Scout's own numbers. `weightedContribution` is read from Scout's
 * `weighted_score` field and is NOT computed as score × weight here: recomputing it would be a
 * second model quietly disagreeing with the first whenever Scout changes how a component
 * contributes. When Scout omits it, the cell stays null and reads "—".
 *
 * RISK_COMPONENTS fixes the order so the table cannot reshuffle between polls; a component Scout
 * adds later is appended after the known ones rather than dropped.
 *
 * @returns {Array<{ name, score, weight, weightedContribution, reason, evidence }>}
 */
export function riskComponents(status) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const comps = (S.risk && S.risk.components) || null;
  if (!isObj(comps)) return [];
  const weights = (S.risk && S.risk.weights) || null;
  const names = [
    ...RISK_COMPONENTS.filter((n) => n in comps),
    ...Object.keys(comps).filter((n) => !RISK_COMPONENTS.includes(n)),
  ];
  return names.map((name) => {
    const c = isObj(comps[name]) ? comps[name] : {};
    return {
      name: asText(c.name) || name,
      score: num(c.score),
      // Scout reports the weight on the component AND in a top-level `weights` map. The
      // component's own value wins; the map is the fallback for a Scout that only sends one.
      weight: num(c.weight) ?? (isObj(weights) ? num(weights[name]) : null),
      weightedContribution: num(c.weighted_score),
      reason: asText(c.reason),
      evidence: isObj(c.evidence) ? c.evidence : null,
    };
  });
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
  // NOT `can_start` alone. Scout's explicit contract decides, and `start_eligible:true` with
  // authority still OPERATOR is an OFFERED Start whose first phase takes agent control.
  const elig = startEligibility(S);
  if (elig.eligible && !bindingView(S).blocksNewMission) {
    return { action: "start", label: "Start Mission", enabled: true, tone: "ok",
      reason: elig.deferredOnAuthority ? START_ACQUIRES_AUTHORITY_NOTE : null };
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
  // A NEW mission on top of a run that still owns the vehicle. Scout's own binding/conflict
  // verdict, and a stable one. It is checked BEFORE the generic "already running" because it is
  // strictly more specific and more actionable: "the mission is already paused" is true but
  // silent about the thing the operator just did — uploading a different mission that is not
  // going to fly until this run ends.
  const bind = bindingView(S);
  if (bind.blocksNewMission) {
    return block(START_BLOCK.MISSION_REPLACEMENT_CONFLICT, null,
      [MISSION_REPLACEMENT_BLOCKED_TEXT, bind.detail].filter(Boolean).join(" — "));
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
  // Scout's OWN explicit refusal, last, and only when it actually refused. Note what does NOT
  // reach here: `authority_blocks_start` is not a blocker at all — it is the normal pre-Start
  // condition, and the Start transaction resolves it as its first phase.
  const elig = startEligibility(S);
  if (!elig.eligible) {
    return block(START_BLOCK.NOT_ELIGIBLE, asText(elig.reason) || undefined,
      asText(elig.reason)
        || "Scout reports the mission is not eligible to start and gave no reason.");
  }
  return { canStart: true, code: null,
    reason: null,
    // The card can say, before the press, exactly what the press will do about authority.
    authorityWillBeAcquired: elig.deferredOnAuthority,
    executionReady: elig.executionReady,
    detail: (elig.deferredOnAuthority ? START_ACQUIRES_AUTHORITY_NOTE + " " : "")
      + "Start runs one backend transaction: a fresh preflight, the authority hand-off, a "
      + "fresh mission and package proof, a verified LOITER, Set Home and verify, then AUTO and "
      + "verify. No vehicle write happens before its own proof succeeds." };
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

// The lifecycle states a Stop is MEANINGFUL from — Scout's own evidence, not the Pixhawk mode.
// A vehicle sitting in AUTO with no Scout run is not stoppable, and a Scout that is mid-write is
// not stoppable either; both of those are decided below, from the lifecycle, never from the mode.
//
// SUSPENDED is included on purpose: a run suspended by a failed replan still owns the vehicle,
// and a safe abort is exactly what an operator needs there. FINAL_HOLD_REQUESTED is deliberately
// NOT here — it is a step inside Scout's own return transaction, where a competing write is
// unsafe — while RETURNING_HOME and HOME_ARRIVAL_PENDING are, because the run is still under way.
export const STOPPABLE_STATES = [
  "RUNNING", "PAUSED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING", "SUSPENDED",
];

// The return-phase states in which the run is still happening rather than Scout being inside one
// of its own write transactions. Stop and Pause stay meaningful here.
const RETURN_PHASE_STOPPABLE = ["RETURNING_HOME", "HOME_ARRIVAL_PENDING"];

/**
 * Whether STOP may be offered, and why not — derived from SCOUT'S LIFECYCLE EVIDENCE.
 *
 * The rule mirrors pauseAvailability / resumeAvailability, and for the same reason: Scout's
 * STATE is the authority for whether there is a run to abort, and Scout's `can_stop` flag is the
 * authority for whether it will accept the write RIGHT NOW — but only when Scout actually sends
 * one.
 *
 *   state in STOPPABLE_STATES, or can_stop === true    → the control exists
 *   can_stop === true                                  → enabled
 *   can_stop === false                                 → shown DISABLED with Scout's own answer
 *   can_stop absent                                    → enabled; the state is the authority and
 *                                                        Scout arbitrates the write
 *
 * Everything Scout owns the moment for still wins first: an unreadable status, the replanning
 * controller, an active operation id, a mid-transaction state and a package/BUSY conflict all
 * withhold the control. `can_stop:true` in a state this build does not list is honoured — that
 * is what "any other state Scout explicitly supports" means.
 *
 * @returns {{ available, enabled, supported, reported, reason }} — `available:false` hides the
 *   control entirely rather than showing a dead one.
 */
export function stopAvailability(status, { busy = false } = {}) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const state = String(S.state || "").toUpperCase();
  const reported = S.stopSupported === true;
  const no = (reason) => ({ available: false, enabled: false, supported: true, reported, reason });
  const off = (reason) => ({ available: true, enabled: false, supported: true, reported, reason });

  if (!S.supported || !S.present) {
    return { available: false, enabled: false, supported: false, reported: false,
      reason: "Scout mission-execution status is unavailable — Stop cannot be offered" };
  }
  // WHICH control exists: Scout's lifecycle state, or Scout explicitly saying it will take one.
  if (!STOPPABLE_STATES.includes(state) && S.canStop !== true) return no(null);

  if (busy) return off("An operation is already in progress");
  if (S.replanning.active) return off("The replanning controller owns the vehicle");
  if (S.activeOperationId) {
    return off(`Scout is already processing operation ${S.activeOperationId}`);
  }
  // A mid-transaction state is a step INSIDE one of Scout's own writes. The RETURN phase is not
  // one of those — the mission is simply still under way there, and a safe abort is meaningful.
  if (S.transitional && !RETURN_PHASE_STOPPABLE.includes(state)) {
    return off(`Scout is mid-transaction (${stateLabel(state)})`);
  }
  // Scout's own conflict/BUSY verdict on the run that owns the vehicle.
  const conflict = str(S.binding && S.binding.conflictCode);
  if (conflict && ACTIVE_CONFLICT_CODES.has(conflict)) {
    return off(`Scout reports ${conflict} — another mission-execution operation owns the vehicle`);
  }
  if (S.canStop === false) {
    return off(`Scout reports can_stop=false in ${S.state || "its current state"}`);
  }
  return { available: true, enabled: true, supported: true, reported, reason: null };
}

/**
 * The operator-facing PROGRESS line for a Stop in flight, from Scout's own phases when it has
 * published one and from its `stop` evidence when it has not.
 *
 * Nothing here predicts the next state and nothing shows a percentage. Before Scout has moved at
 * all — the POST is still in flight — the line is the honest generic first step.
 *
 * @returns {{ phase, text }}
 */
export function stopPhase(state, stopEvidence = null) {
  const up = String(state || "").toUpperCase();
  if (STOP_TRANSITION_LABELS[up]) return { phase: up, text: STOP_TRANSITION_LABELS[up] };
  const ev = isObj(stopEvidence) ? stopEvidence : {};
  // Scout published no stop STATE, but its evidence says how far the transaction got. Read it
  // backwards — the last thing proven is the step just completed, so the line names the next one.
  if (ev.reported) {
    if (ev.rewindVerified === true) return { phase: "STOP_VERIFYING_RESET", text: "Verifying reset…" };
    if (ev.originalRestored === true) return { phase: "STOP_REWINDING", text: "Rewinding mission…" };
    if (ev.holdVerified === true) {
      return { phase: "STOP_RESTORING_ORIGINAL", text: "Restoring original mission…" };
    }
  }
  return { phase: "STOP_REQUESTED", text: STOPPING_TEXT };
}

/** True for a state inside Scout's stop transaction. */
export function isStopTransactionState(state) {
  return STOP_IN_TRANSACTION_STATES.includes(String(state || "").toUpperCase());
}

// The success lines a completed Stop shows, in order. Each is a CLAIM about a specific piece of
// Scout evidence, so a line only appears when Scout actually proved it — a Stop that never had a
// revised route to restore says "no revised route was installed", not "Original mission restored".
export const STOP_SUCCESS_TITLE = "Mission stopped";

/**
 * How a finished Stop reads on the card — success or failure, from Scout's evidence only.
 *
 * The load-bearing rule: after a successful Stop, Scout normally reports
 *
 *     state = NOT_READY, start_eligible = true, authority_blocks_start = true
 *
 * and that is NOT a failure. Authority is deliberately back with the operator, and the Start
 * button stays available because the Start transaction is what hands it to the Local Agent
 * again. This function must never render that combination as a problem.
 *
 * A FAILED Stop leaves Scout SUSPENDED after the safe hold: the vehicle is being held in LOITER
 * and the reset is incomplete. Scout's exact code is shown, and no recovery is suggested that
 * the station would perform automatically — it performs none.
 *
 * @param status normalized (or raw) Scout status
 * @param view   interpretTransaction() output for the stop operation, or null
 * @returns {{ ok, title, lines, code, text, detail, held }|null}
 */
// The resting states a completed Stop can have produced. Outside them the stop presentation is
// withheld entirely — Scout's `stop` block persists in its status, and narrating "Mission
// stopped" over a run that has since been RESTARTED would be the worst kind of stale claim.
const STOP_OUTCOME_STATES = [
  "NOT_READY", "NOT_STARTED", "READY", "STOPPED", "CANCELLED", "SUSPENDED", "FAILED",
];

export function stopOutcomeView(status, view = null) {
  const S = status && status.present !== undefined ? status : normalizeStatus(status);
  const v = isObj(view) ? view : null;
  const ev = S.stop || {};
  const state = String(S.state || "").toUpperCase();
  const outcome = v ? str(v.outcome) : null;
  if (!ev.reported && !v) return null;
  if (outcome === "pending") return null;
  if (!STOP_OUTCOME_STATES.includes(state)) return null;

  // WHOSE failure is this? A SUSPENDED run is not automatically a failed Stop — a failed replan
  // lands there too, and a `stop` block left over from an earlier abort must not be read as the
  // cause. It is a stop failure only when a STOP_* code says so, or when the transaction the
  // operator just ran was a stop that did not succeed.
  const code = str(v && v.code) || (v ? null : ev.outcome);
  const failedCode = code && STOP_ERROR_CODES.includes(String(code).toUpperCase()) ? code : null;
  const failedTransaction = !!outcome && outcome !== OUTCOME.ACCEPTED;
  const failed = !!failedCode || failedTransaction;
  if (!failed && state === "SUSPENDED") return null;   // someone else's SUSPENDED

  if (failed) {
    const held = ev.holdVerified === true || String(S.mode || "").toUpperCase() === "LOITER";
    const known = failedCode ? errorText(failedCode) : null;
    return {
      ok: false, held, code: failedCode || str(code) || null,
      title: "Mission stop did not complete",
      text: [
        held ? "The vehicle is being held in LOITER." : null,
        "The reset is incomplete.",
        known && known !== failedCode ? `${known}.` : null,
      ].filter(Boolean).join(" "),
      lines: [],
      detail: [
        failedCode || code || null,
        asText(v && v.message) || asText(S.lastError) || null,
        ev.reported ? stopEvidenceDetail(ev) : null,
      ].filter(Boolean).join(" · ") || null,
    };
  }

  // SUCCESS. Every line is a proven claim, and each is omitted rather than guessed.
  const lines = [];
  if (ev.holdVerified === true || String(S.mode || "").toUpperCase() === "LOITER") {
    lines.push("Vehicle held in LOITER");
  }
  if (ev.originalRestored === true) lines.push("Original mission restored");
  else if (ev.originalRestored === false) lines.push("No revised route was installed");
  if (ev.rewindVerified === true) lines.push("Original mission reset to start");
  if (ev.replanReset === true || ev.experimentCleared === true) {
    lines.push("Execution and replan test state cleared");
  }
  if ((ev.authorityAfter || "").toUpperCase() === "OPERATOR"
      || (v && v.authorityRestored === true)) {
    lines.push("Operator authority restored");
  }
  if (ev.readyForStart === true || S.startEligible === true) lines.push("Ready for a new Start");
  return {
    ok: true, held: true, code: null, title: STOP_SUCCESS_TITLE,
    text: lines.join(" · ") || "Scout completed the stop transaction.",
    lines,
    detail: stopEvidenceDetail(ev),
  };
}

/** Scout's stop evidence as ONE readable line for a tooltip. Never "[object Object]": every
 *  value goes through asText, and the hashes are shortened rather than dropped. */
export function stopEvidenceDetail(ev) {
  const e = isObj(ev) ? ev : {};
  if (!e.reported) return null;
  const flag = (v) => (v === true ? "yes" : v === false ? "no" : null);
  const hash = (h) => (h ? String(h).replace(/^sha256:/, "").slice(0, 12) + "…" : null);
  const parts = [
    e.outcome ? `outcome ${asText(e.outcome)}` : null,
    flag(e.holdVerified) ? `hold verified ${flag(e.holdVerified)}` : null,
    flag(e.originalRestored) ? `original restored ${flag(e.originalRestored)}` : null,
    hash(e.activeHashBefore) ? `active before ${hash(e.activeHashBefore)}` : null,
    hash(e.originalHash) ? `original ${hash(e.originalHash)}` : null,
    hash(e.revisedHash) ? `revised ${hash(e.revisedHash)}` : null,
    flag(e.rewindVerified) ? `rewind verified ${flag(e.rewindVerified)}` : null,
    e.sequenceAfter === null || e.sequenceAfter === undefined
      ? null : `sequence after ${asText(e.sequenceAfter)}`,
    flag(e.replanReset) ? `replan reset ${flag(e.replanReset)}` : null,
    flag(e.experimentCleared) ? `experiment cleared ${flag(e.experimentCleared)}` : null,
    e.authorityAfter ? `authority ${asText(e.authorityAfter)}` : null,
    flag(e.readyForStart) ? `ready for start ${flag(e.readyForStart)}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/**
 * THE state-driven lifecycle control set for the Map's Agent Mission card.
 *
 * This is the mapping the product decision specifies, and it is derived from SCOUT'S STATUS
 * ONLY — never from the last click, never from the previous label:
 *
 *   READY / NOT_STARTED       [ Start Mission ]
 *   RUNNING / RETURNING_HOME / HOME_ARRIVAL_PENDING
 *                             [ Pause Mission ]  [ Stop Mission ]
 *   PAUSED                    [ Resume Mission ]  [ Stop Mission ]
 *   STOPPED / CANCELLED       [ Start Mission ]
 *   SUSPENDED / FAILED        failure shown + [ Rearm Mission Controller ]  [ Stop Mission ]
 *                             [ Take Control ]
 *   COMPLETED_HOLD            completed + final LOITER shown; a fresh Start only AFTER the
 *                             controller has been rearmed for a new execution
 *
 * STOP SITS DIRECTLY BESIDE THE HOLD CONTROL, always, because the two are the operator's real
 * choice at that moment and they are NOT interchangeable:
 *
 *   Pause  temporary LOITER; the execution position is retained; Resume continues the SAME run.
 *   Stop   safe abort; LOITER; the original mission is restored if a revised route is installed,
 *          rewound to its beginning, the execution/replan test state is cleared and supervisory
 *          authority returns to the operator, ready for a clean new Start.
 *
 * Stop is NOT a destructive mission deletion and is never labelled as one: it clears no mission,
 * deletes no planning package and disarms nothing.
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
  const stop = stopAvailability(S, { busy });
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
  // The RETURN PHASE (RETURNING_HOME, HOME_ARRIVAL_PENDING) is transitional for the purposes of
  // "Scout is moving through a sequence", but it is NOT a moment in which the operator must be
  // denied control: the mission is still under way and Pause / Stop remain meaningful there.
  // Every other transitional state — including FINAL_HOLD_REQUESTED and the whole stop sequence
  // — is a step INSIDE one of Scout's own transactions, where a competing command is unsafe.
  const inFlight = S.activeOperationId
    || (S.transitional && !RETURN_PHASE_STOPPABLE.includes(state));
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
  // Stop is only ADDED where the state offers it (stopAvailability). It is never hidden when
  // merely un-pressable: a disabled Stop carrying Scout's own reason is what tells the operator
  // the abort exists and why it cannot be used this second.
  const addStop = () => {
    if (!stop.available) return;
    out.buttons.push(btn("stop", "Stop Mission",
      { enabled: stop.enabled, reason: stop.reason, tone: "warn", kind: "secondary" }));
  };

  if (state === "RUNNING" || RETURN_PHASE_STOPPABLE.includes(state)) {
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
    // A run suspended by a failed replan still owns the vehicle, so the safe abort belongs
    // beside Rearm. They are NOT the same action and neither substitutes for the other: Rearm
    // prepares the controller for another run and issues no vehicle command, while Stop holds
    // the vehicle, restores and rewinds the original mission, clears the execution/replan test
    // state and hands supervisory authority back to the operator.
    addStop();
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
// Scout's BARE reason codes → the short card line. Checked before the prose heuristics below,
// longest/most specific first, because the generic rules would blur codes that mean materially
// different things: `/position/i` reads POSITION_STALE as "Position not usable" (true but vague)
// and nothing at all distinguishes a mission deficit from an RTL-return deficit. Every string
// here is <= 44 characters, the card's short-line budget; the full reason is always the tooltip.
//
// The package-staleness codes are here for a second reason, and it is a correctness one rather
// than a brevity one. They are DEFINITIVE: Scout performed the comparison and it failed — it
// holds a package whose route hash is not the one on the flight controller. The generic
// `/hash|package|verif/` rule below shortened every one of them to "Mission verification
// unavailable", which states the opposite (that the check could not be run) and put an UNKNOWN
// phrasing on the card beside the readiness line's definitive one. Two contradictory sentences
// about one settled fact is what made a stale package look like a station that had lost its
// footing at startup.
export const START_BLOCK_REASON_TEXT = {
  PLANNING_PACKAGE_MISSING: "Agent planning package missing",
  PLANNING_PACKAGE_UNUSABLE: "Agent planning package unusable",
  PLANNING_PACKAGE_STALE: "Agent planning package is stale",
  ROUTE_HASH_STALE: "Agent planning package is stale",
  INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION: "Insufficient energy for planned mission",
  INSUFFICIENT_ENERGY_FOR_RTL_RETURN: "Insufficient energy for RTL return",
  RTL_HOME_UNAVAILABLE: "RTL Home unavailable",
  MISSION_UNAVAILABLE: "Mission unavailable",
  BATTERY_INVALID: "Battery estimate unavailable",
  POSITION_STALE: "Position data stale",
};

export function shortStartBlocker(reason) {
  const t = asText(reason);
  if (!t) return "Start preconditions not met";
  // Scout's own code, whether it arrived bare or embedded in a sentence.
  const up = t.toUpperCase();
  for (const [code, text] of Object.entries(START_BLOCK_REASON_TEXT)) {
    if (up.includes(code)) return text;
  }
  if (/another mission is active|while another mission/i.test(t))
    return "Another mission is still active";
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
  stopping = false, stopResult = null,
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
    // A finished run's second line and its one next action (see the COMPLETED_HOLD branch).
    completionNote: null, nextAction: null,
    // Set when a NEW mission cannot start because the PREVIOUS run still owns the vehicle.
    // Rendered as its own notice: it is neither a broken mission nor a Scout fault, and the two
    // real remedies are Scout's — finish the active run, or explicitly rearm it.
    replacementConflict: null,
    // How a FINISHED Stop reads (stopOutcomeView): the success lines, or Scout's exact failure
    // code with the "held in LOITER, reset incomplete" statement. Never a mission failure for
    // the normal NOT_READY + start_eligible landing.
    stopOutcome: null,
    // A Stop in flight: the phase-specific progress line, from Scout's own state/evidence.
    stopPhase: null,
    // Start is available AND pressing it will take Local Agent control first.
    authorityWillBeAcquired: false,
    // The two COMPACT LIVE STATUSES, both of them Scout's own verdict and neither of them
    // computed here. ENERGY answers "can the remaining planned mission be completed?" and carries
    // the MISSION margin — but it reads RTL INSUFFICIENT when Scout can complete the run and not
    // return from it, because Scout's Start gate requires both. RISK is Scout's GOVERNING
    // level (`risk.level`, floors and hard overrides already applied); until Scout reports one
    // it is a quiet "—" and never a reassuring LOW.
    //
    // THE TWO ARE INDEPENDENT AND ARE MEANT TO BE READ TOGETHER. Feasibility answers "can this
    // be finished with reserve intact?"; risk answers "how close are we to conditions we do not
    // want?". `ENERGY FEASIBLE +4%` beside `RISK HIGH` is not a contradiction to be smoothed
    // over — it is the honest reading of a run that Scout can still complete while its energy
    // margin has tightened enough to raise the governing level. Neither line is ever restated in
    // the other's terms, and no third "TIGHT" verdict is invented on this station to bridge them.
    //
    // Set unconditionally, BEFORE the unsupported / unavailable / replanning / in-flight returns
    // below, so every path leaves the card with a defined, honest reading rather than an absent
    // field a renderer would have to guess at.
    energy: energyView(S),
    risk: riskView(S),
    // Scout's advisory word (CONTINUE / CAUTION / RETURN HOME / HOLD). Display only — it is not
    // a button, it gates nothing, and it produces no command.
    recommendation: recommendationView(S),
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
  //
  // The FSM step (LOITER / PLANNING / VALIDATING / UPLOADING / RETURNING_HOME / RTL_FALLBACK /
  // SAFE_HOLD / …) is appended VERBATIM, exactly as Scout spelled it — this is presentation of
  // the one concise status line the card already has, never a new row, and never a translation
  // this station invents meaning for. It is the procedural step in flight, not the mission-level
  // advice: a LOITER step here is compatible with an ADVICE of RETURN HOME shown above, and this
  // line must never be read as, or replace, that advice.
  if (S.replanning.active) {
    const fsm = replanFsm(S);
    out.headline = fsm ? `Agent is replanning — ${fsm}` : "Agent is replanning";
    return out;
  }
  // Scout's binding/conflict verdict, read BEFORE the ordinary pre-start presentation so a newly
  // uploaded mission can never render as "Ready to start" while the previous run is still live.
  const bind = bindingView(S);
  if (bind.blocksNewMission) {
    out.replacementConflict = { text: bind.text, title: bind.detail || bind.text, tone: "warn" };
  }
  const eligibility = startEligibility(S);
  out.authorityWillBeAcquired = eligibility.eligible && eligibility.deferredOnAuthority;
  // A Start in flight, or any Scout transaction. While the START transaction runs the line is
  // PHASE-SPECIFIC and NEUTRAL — the five phases in lib/mission-readiness.js, each of them
  // Scout's observed step (or, before Scout has moved, the backend's provable first phase).
  // Nothing here is a warning: a Start in progress is the system doing what was asked.
  if (starting || stopping || S.activeOperationId
      || (S.transitional && !RETURN_PHASE_STOPPABLE.includes(state))) {
    out.working = true;
    // A STOP in flight gets Scout's own phases — Stopping mission… → Holding position… →
    // Restoring original mission… → Rewinding mission… → Verifying reset… — and never an
    // optimistic "stopped" before Scout's transaction has actually completed.
    if (stopping || isStopTransactionState(state)) {
      const ph = stopPhase(state, S.stop);
      out.stopPhase = ph.phase;
      out.headline = ph.text;
    } else if (starting || isStartTransactionState(state)) {
      const ph = startPhase(state);
      out.startPhase = ph.phase;
      out.headline = ph.text;
    } else {
      out.headline = stateLabel(S.state);
    }
    return out;
  }
  // A COMPLETED Stop, presented from Scout's evidence. Computed before the pre-start branches
  // below precisely because a successful stop rests in NOT_READY: without this the card would
  // show the perfectly normal "Not ready to start" and nothing about the abort that just ran.
  out.stopOutcome = stopOutcomeView(S, stopResult);

  // A one-shot preflight refresh (explicit Refresh, a read after an upload/sync/reconnect) is a
  // PASSIVE, INFORMATIONAL verification: a small spinner and nothing else. It never withdraws a
  // button — `readiness.canStart` comes from the stable gate and is untouched by this flag — and
  // it is never a warning and never replanning. That separation is the whole anti-flicker rule.
  if (rv && rv.checking) {
    out.checking = true;
    out.checkingText = CHECKING_TEXT;
  }

  // ── A FINISHED mission reads as finished ────────────────────────────────────────────────
  // COMPLETED_HOLD is a terminal state, and a run that has ended must never keep presenting as
  // RUNNING or PAUSED. The completion claim itself is unchanged and still strict: reaching the
  // last waypoint, or the arrival-persistence bar filling, is NOT completion — only Scout
  // reporting COMPLETED_HOLD *with* a verified final LOITER is (isComplete). When Scout reports
  // the state without the LOITER evidence, that gap is stated rather than rounded up.
  if (state === "COMPLETED_HOLD") {
    out.chip = "COMPLETED";
    out.tone = ctl.complete ? "ok" : "caution";
    out.headline = ctl.complete ? "Mission finished" : "Completed — final LOITER not verified";
    out.completionNote = ctl.complete
      ? { text: "Final LOITER verified", tone: null,
          title: "Scout reports COMPLETED_HOLD and a verified final LOITER — the run is over "
            + "and the vehicle is holding at Home." }
      : { text: "Final LOITER NOT verified", tone: "warn",
          title: "Scout reports COMPLETED_HOLD but could not verify the final LOITER." };
    const mid = shortMissionId(S.missionId || missionId);
    if (mid) out.rows.push({ k: "Mission", v: mid, title: S.missionId || missionId, mono: true });
    if (wp) out.rows.push({ k: "WP", v: wp, title: null });
    // The next action a finished run actually has. Rearm PREPARES the controller for the next
    // mission; it is not a Stop and issues no vehicle command.
    out.nextAction = ctl.buttons.some((b) => b.action === "rearm")
      ? { action: "rearm", label: "Rearm / prepare next mission" } : null;
    return out;
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
    if (out.stopOutcome && out.stopOutcome.ok === false) {
      // A failed Stop states itself in full, in its own block. Repeating Scout's last_error as a
      // second short line here would print the same failure twice.
    } else if (out.replacementConflict) {
      // Outranks every other blocker: it is the only one that explains why a mission the
      // operator JUST uploaded is not the mission this vehicle is going to fly.
      out.blocker = { text: "Another mission is still active", tone: "warn",
        title: out.replacementConflict.title };
    } else if (ctl.failure) {
      out.blocker = { text: firstClause(ctl.failure), title: asText(ctl.failure), tone: "warn" };
    } else if (start && !start.enabled) {
      // ONE short line naming a STABLE cause — disconnected, no mission, replanning, already
      // running, Rearm first. It cannot say "verifying", "unproven" or "stale", because none of
      // those can withdraw Start any more.
      out.blocker = { text: shortStartBlocker(start.reason), title: asText(rv && rv.detail)
        || asText(start.reason), tone: "warn" };
    } else if (stop && !stop.enabled) {
      out.blocker = { text: firstClause(stop.reason), title: asText(stop.reason), tone: null };
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
  const elig = startEligibility(S);
  const bind = bindingView(S);
  if (elig.eligible && !bind.blocksNewMission) {
    // Eligible. The ONE thing worth saying is what the press will do about authority — stated
    // as information, never as something the operator has to go and fix first.
    if (elig.deferredOnAuthority) out.push(START_ACQUIRES_AUTHORITY_NOTE);
    return out;
  }
  if (bind.blocksNewMission) {
    out.push(MISSION_REPLACEMENT_BLOCKED_TEXT + (bind.detail ? ` (${bind.detail})` : ""));
  }
  if (elig.source === "scout" && !elig.eligible && asText(elig.reason)) {
    out.push(`Scout: ${asText(elig.reason)}`);
  }

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
