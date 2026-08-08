// mission-readiness.js — what the operator is SHOWN about Start readiness. No DOM, no fetch,
// no timers. Unit-tested in tests/mission-readiness.test.mjs.
//
// WHY THIS MODULE LOOKS THE WAY IT DOES
// -------------------------------------
// The Map used to poll GET .../mission-execution/preflight on the ordinary refresh interval and
// render its verdict directly into the Agent Mission card. That preflight is EXPENSIVE and
// SHORT-LIVED: the backend serves its Pixhawk read-back evidence through a 10 s cache
// (main.PIXHAWK_READBACK_TTL_S), so roughly every tenth poll paid for a live MAVLink mission
// download, and a download that timed out or arrived partial answered `can_start:false` — with
// three blockers, because the package hash chain and Scout's replanning readiness are both
// anchored on the read-back. None of those is a fact about the vehicle. On a completely stable,
// DISARMED, IDLE Scout the card therefore alternated every few seconds between
//
//     READY / Start Mission        and        NOT_READY / Replanning readiness not confirmed
//
// A proof-with-a-lifetime cache was the previous attempt to absorb that transient. It worked,
// but it kept the expensive poll and paid for it with a model the operator had to hold in their
// head: a last-proven verdict, an expiry, a "re-verifying" indicator, and a Start button whose
// availability depended on all three. The simpler and more honest answer is to stop polling the
// proof at all:
//
//   • START AVAILABILITY comes from OBVIOUS, STABLE blockers only — disconnected, unsupported,
//     no mission, another operation active, explicit replanning, already running, a terminal
//     state needing Rearm. That is `startGate()` in lib/mission-execution.js, and every one of
//     its inputs changes only when something real changes.
//   • THE AUTHORITATIVE PROOF is the one the Start transaction performs — fresh, fail-closed,
//     before any vehicle write (mission_lifecycle.run_start, which forces a live read-back).
//     It always ran there. The polled copy was never the gate, only a preview of it, and a
//     preview that withdraws a button is worse than no preview.
//   • A PREFLIGHT MAY STILL RUN ONCE at a meaningful moment — vehicle selection, after a mission
//     upload, after a package sync, on reconnect, or from an explicit Refresh — and be shown as
//     INFORMATION (preflightNote below). It never feeds the gate, so it cannot flicker it.
//
// Nothing about Scout's safety checks, mission identity validation, the LOITER exemption or the
// Start transaction's ordering is touched by any of this. Only the polling and its presentation.

const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const str = (v) => {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
};

/** The two presentation states a PRE-START card can be in. There is deliberately no CHECKING
 *  state and no UNKNOWN state any more: readiness is no longer something the station is
 *  perpetually in the middle of determining. */
export const READINESS = {
  READY: "READY",
  NOT_READY: "NOT_READY",
};

// ---- The Start transaction's phases (what the operator is told WHILE Start runs) ----------
//
// One click issues ONE backend transaction, so the station cannot see inside it directly. What
// it can see is Scout's own canonical state, which the ordinary status poll keeps current at
// 3 s — and Scout moves through the transaction's steps in its own states. Each phase label
// below is therefore either Scout's observed step or, before Scout has moved, the phase the
// operator backend is provably in (it checks preconditions first, then takes authority).
//
// NEUTRAL BY DESIGN. None of these is a warning: a Start in progress is the system doing exactly
// what was asked. Nothing here predicts the next phase or fabricates a percentage.
export const CHECKING_TEXT = "Checking mission readiness…";

export const START_PHASES = ["preflight", "authority", "hold", "home", "auto"];

export const START_PHASE_TEXT = {
  preflight: CHECKING_TEXT,
  authority: "Taking agent control…",
  hold: "Holding position…",
  home: "Setting and verifying Home…",
  auto: "Starting AUTO…",
};

// Scout's own states inside its Start transaction → the phase each one IS. SYNCHRONIZING_PACKAGE
// is folded into "Starting AUTO…" because it is the last step before the mode change and naming
// it separately tells the operator nothing they can act on.
export const START_TRANSACTION_STATES = [
  "START_REQUESTED", "START_HOLD_REQUESTED", "START_HOLD_CONFIRMED",
  "SETTING_HOME", "VERIFYING_HOME", "SYNCHRONIZING_PACKAGE", "STARTING_AUTO",
];

const START_PHASE_BY_STATE = {
  // Scout registers the intent and arbitrates it; from the operator's side this is the window in
  // which agent control is being taken and confirmed.
  START_REQUESTED: "authority",
  START_HOLD_REQUESTED: "hold",
  START_HOLD_CONFIRMED: "hold",
  SETTING_HOME: "home",
  VERIFYING_HOME: "home",
  SYNCHRONIZING_PACKAGE: "auto",
  STARTING_AUTO: "auto",
};

/**
 * The phase to show while a Start is in flight, from Scout's canonical state.
 *
 * Before Scout has moved out of its resting state the backend is provably still in its own first
 * phase — the fresh preflight — so that is what is shown. Never a timer, never a guess about a
 * step the station has no evidence for.
 *
 * @returns {{ phase: string, text: string }}
 */
export function startPhase(state) {
  const s = String(state || "").toUpperCase();
  const phase = START_PHASE_BY_STATE[s] || "preflight";
  return { phase, text: START_PHASE_TEXT[phase] };
}

/** True for a state Scout occupies only inside its Start transaction. */
export function isStartTransactionState(state) {
  return START_TRANSACTION_STATES.includes(String(state || "").toUpperCase());
}

// ---- Why Start is withheld (stable causes only) ------------------------------------------
//
// EVERY code here names something an operator can see and act on, and every one of them is
// stable: it changes when the vehicle, the mission or Scout changes, never because a background
// read is momentarily in flight. There is deliberately NO code for "the readiness proof is
// missing / refreshing / stale / unavailable" — that was the flicker.
export const START_BLOCK = {
  BUSY: "BUSY",
  DISCONNECTED: "DISCONNECTED",
  UNSUPPORTED: "UNSUPPORTED",
  STATUS_UNAVAILABLE: "STATUS_UNAVAILABLE",
  REPLANNING: "REPLANNING",
  OPERATION_ACTIVE: "OPERATION_ACTIVE",
  ALREADY_RUNNING: "ALREADY_RUNNING",
  REARM_REQUIRED: "REARM_REQUIRED",
  NO_MISSION: "NO_MISSION",
  // Raised ONLY when Scout EXPLICITLY declares it requires an existing verified Home before the
  // transaction may be entered. Absent that declaration the Start transaction owns Set Home, so
  // an unverified Home is information (HOME_DURING_START_NOTE), never a blocker.
  HOME_REQUIRED: "HOME_REQUIRED",
  // Scout's OWN explicit verdict (`start_eligible:false`). Stable and authoritative — it is a
  // statement about the mission, not a background read that happened to be in flight — so it
  // belongs in the gate, and it is always shown with Scout's own `start_block_reason`.
  NOT_ELIGIBLE: "NOT_ELIGIBLE",
  // A NEW mission was uploaded while the PREVIOUS run still owns the vehicle (Scout reports
  // binding STALE_MISMATCH or a package conflict). The new mission is not ready and must not
  // be shown as such; the remedy is Scout's, not an invented Stop.
  MISSION_REPLACEMENT_CONFLICT: "MISSION_REPLACEMENT_CONFLICT",
};

export const START_BLOCK_TEXT = {
  BUSY: "An operation is already in progress",
  DISCONNECTED: "The vehicle is disconnected",
  UNSUPPORTED: "Mission lifecycle not supported by this Scout version",
  STATUS_UNAVAILABLE: "Scout mission-execution status is unavailable",
  REPLANNING: "The replanning controller owns the vehicle",
  OPERATION_ACTIVE: "Scout is already processing a mission operation",
  ALREADY_RUNNING: "The mission is already running",
  REARM_REQUIRED: "Rearm the mission controller before a new run",
  NO_MISSION: "No active mission for this vehicle",
  HOME_REQUIRED: "Scout requires a verified Home before this mission can be started",
  NOT_ELIGIBLE: "Scout reports the mission is not eligible to start",
  MISSION_REPLACEMENT_CONFLICT:
    "New mission uploaded while another mission is active. Finish or explicitly " +
    "terminate/rearm the active mission before starting the new mission.",
};

// The Start transaction sets Home to the launch position and verifies it as one of its own
// phases. Before Start, therefore, an unverified Home is not a defect to fix — it is a step that
// has not happened yet, and this is how the card says so.
export const HOME_DURING_START_NOTE = "Home will be set during Start";

/**
 * The pre-start presentation verdict, derived from the STABLE gate.
 *
 * `checking` is carried for the one-shot informational preflight (an explicit Refresh, a read
 * after a mission upload) so the card can show a small spinner. It is a presentation flag ONLY:
 * `canStart` is copied from the gate and is never affected by it. That invariant is what makes
 * a refresh incapable of flickering the Start button, and it is asserted in the tests.
 *
 * @param gate  the output of startGate() (lib/mission-execution.js)
 * @returns {{ state, canStart, checking, code, reason, detail }}
 */
export function readinessView(gate, { refreshing = false } = {}) {
  const g = isObj(gate) ? gate : {};
  const canStart = g.canStart === true;
  return {
    state: canStart ? READINESS.READY : READINESS.NOT_READY,
    canStart,
    checking: refreshing === true,
    code: str(g.code),
    reason: canStart ? null : (str(g.reason) || "Start is not available"),
    detail: str(g.detail) || str(g.reason) || null,
  };
}

/**
 * The INFORMATIONAL reading of a one-shot preflight body. Displayed as a note; never a gate.
 *
 * Three outcomes, because collapsing them is what made the polled version misleading:
 *   ok: true   every precondition was read and passed
 *   ok: null   the backend declared its own evidence incomplete (proof_complete:false — the
 *              read-back could not be obtained this round). That is not a fact about the
 *              vehicle, so it is reported as "could not be checked", not as a failure.
 *   ok: false  every input was read and something genuinely does not pass
 *
 * @param preflight the backend preflight body, or null when none has been run
 * @param opts.at   when it was obtained (ms), carried through for the tooltip
 * @returns {{ ok, reason, detail, checkedAt, missionId }|null}
 */
export function preflightNote(preflight, { at = null } = {}) {
  if (!isObj(preflight) || !("can_start" in preflight)) return null;
  const checkedAt = typeof at === "number" ? at : null;
  const missionId = str(preflight.mission_id);
  const blockers = Array.isArray(preflight.blockers)
    ? preflight.blockers.map((b) => str(b)).filter(Boolean) : [];
  const detail = blockers.length ? blockers.join(" · ") : null;

  if (preflight.can_start === true) {
    return { ok: true, missionId, checkedAt,
      reason: "Start preconditions passed when last checked",
      detail: "The operator backend read every Start precondition and all of them passed. The " +
        "Start transaction re-proves all of it, fresh, before any vehicle write." };
  }
  if (preflight.proof_complete === false) {
    const code = str(preflight.readiness_reason_code);
    return { ok: null, missionId, checkedAt,
      reason: str(preflight.readiness_reason)
        || "Start preconditions could not be checked",
      detail: [str(preflight.readiness_reason), code && `evidence: ${code}`, detail]
        .filter(Boolean).join(" — ")
        || "The evidence the preconditions are computed from could not be obtained." };
  }
  return { ok: false, missionId, checkedAt,
    reason: detail || str(preflight.error) || "Start preconditions did not pass when last checked",
    detail: detail || str(preflight.error) };
}
