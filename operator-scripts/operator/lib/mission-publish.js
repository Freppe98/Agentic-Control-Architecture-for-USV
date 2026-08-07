// mission-publish.js — what the operator is SHOWN about publishing a planned mission, and the
// ONE readiness vocabulary Plan, Map and Agent all render from. No DOM, no fetch, no timers.
// Unit-tested in tests/mission-publish.test.mjs.
//
// WHY THIS MODULE EXISTS
// ----------------------
// Publishing a mission is not one step, it is three writes to three systems: the flight
// controller, the Operator's durable record, and Scout's planning package. The Plan page used to
// render a final green "Uploaded & verified" as soon as the FIRST of those verified, and nothing
// in the station ever performed the third. So the operator was told the mission was published
// while Scout still held the PREVIOUS mission's package — and only discovered it at Start, as a
// mismatch, with no way back except a manual curl.
//
// The backend now runs the whole transaction (mission_publish.py) and reports every phase. This
// module is the presentation over that: neutral progress while it runs, and exactly THREE honest
// endings — fully published, published to the flight controller but the Agent package is owed,
// or not verified at all. The middle one is the whole point: it is a real, common, recoverable
// state, and collapsing it into either "success" or "failure" is what made the defect invisible.
//
// THE SECOND JOB: one readiness vocabulary. Map and Agent used to phrase Scout's package
// evidence independently, so a Scout that was merely REFRESHING its readiness could render as a
// package mismatch on one page while the other showed the mission as ready. `readinessLabel`
// below is the single derivation, and it keeps three things apart that must never be conflated:
// a proven disagreement, an unavailable comparison, and an owed sync.

const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const arr = (v) => (Array.isArray(v) ? v : []);
const str = (v) => {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
};

// ---- The backend's phase names, and the neutral text for each ----------------------------
// NEUTRAL BY DESIGN: none of these is a warning. A publish in progress is the system doing
// exactly what was asked. Nothing here predicts the next phase or fabricates a percentage.
export const PHASES = [
  "VALIDATING_PLAN", "UPLOADING_PIXHAWK", "VERIFYING_PIXHAWK", "PERSISTING_OPERATOR_MISSION",
  "BUILDING_PLANNING_PACKAGE", "SYNCING_SCOUT_PACKAGE", "VERIFYING_SCOUT_PACKAGE", "READY",
];

export const PHASE_TEXT = {
  VALIDATING_PLAN: "Validating the approved plan…",
  UPLOADING_PIXHAWK: "Uploading mission to Pixhawk…",
  VERIFYING_PIXHAWK: "Verifying Pixhawk readback…",
  PERSISTING_OPERATOR_MISSION: "Saving active mission…",
  BUILDING_PLANNING_PACKAGE: "Preparing Agent planning package…",
  SYNCING_SCOUT_PACKAGE: "Synchronizing Agent planning package…",
  VERIFYING_SCOUT_PACKAGE: "Verifying Agent package…",
  READY: "Mission uploaded and Agent package synchronized",
};

/** The neutral progress line for a phase. Never a guess: an unknown phase gets a generic
 *  "Publishing mission…" rather than a fabricated step name. */
export function phaseText(phase) {
  return PHASE_TEXT[str(phase)] || "Publishing mission…";
}

// ---- The transaction's terminal states (mission_publish.py's vocabulary, verbatim) --------
export const PUBLISH_STATE = {
  READY: "READY",
  VERIFYING: "VERIFYING",
  UPLOAD_IN_PROGRESS: "UPLOAD_IN_PROGRESS",
  PACKAGE_SYNC_REQUIRED: "PACKAGE_SYNC_REQUIRED",
  SCOUT_UNREACHABLE: "SCOUT_UNREACHABLE",
  REAL_MISMATCH: "REAL_MISMATCH",
  BLOCKED: "BLOCKED",
  BUSY: "BUSY",
};

// The three outcome kinds the Plan page renders. `partial` is a first-class outcome, not a
// flavour of failure: the mission IS on the flight controller and IS the active record.
export const OUTCOME = {
  PROGRESS: "progress",
  OK: "ok",
  PARTIAL: "partial",
  FAILED: "failed",
};

/** True when the transaction got far enough to PROVE the flight controller carries the
 *  approved route. Read from the phase list, not from the state — a later phase failing must
 *  not retract a proof that was actually made. */
export function pixhawkVerified(env) {
  if (!isObj(env)) return false;
  return arr(env.phases).some(
    (p) => isObj(p) && p.phase === "VERIFYING_PIXHAWK" && p.status === "ok");
}

/**
 * The Plan page's view of a publish result.
 *
 * @param env  the backend publish envelope, or null when none has been obtained
 * @returns {{ kind, state, phase, headline, detail, agentReady, canRetrySync, missionId,
 *             routeHash, routeCount, idempotent }|null}
 */
export function publishView(env) {
  if (!isObj(env)) return null;
  const final = isObj(env.final) ? env.final : {};
  const agentReady = final.agent_ready === true;
  const state = str(env.state);
  const phase = str(env.phase);
  const verified = pixhawkVerified(env);
  const base = {
    state, phase, agentReady,
    missionId: str(env.mission_id),
    routeHash: str(env.expected_route_hash),
    routeCount: typeof env.expected_route_count === "number" ? env.expected_route_count : null,
    idempotent: env.idempotent === true,
    detail: str(env.message),
  };

  if (agentReady) {
    return { ...base, kind: OUTCOME.OK,
      headline: "Mission uploaded and Agent package synchronized",
      canRetrySync: false };
  }
  // Still running. The Pixhawk write is a queued command, so "not finished" is the normal
  // answer for the first seconds after Upload and must read as progress, never as a problem.
  if (state === PUBLISH_STATE.UPLOAD_IN_PROGRESS
      || (state === PUBLISH_STATE.VERIFYING && !verified)
      || state === PUBLISH_STATE.BUSY) {
    return { ...base, kind: OUTCOME.PROGRESS, headline: phaseText(phase), canRetrySync: false };
  }
  // The flight controller is proven to carry the approved route; the Agent package is not
  // proven to match it. The mission is NOT rolled back and the operator is offered the one
  // action that can close the gap — which sends a package and nothing else.
  if (verified) {
    return { ...base, kind: OUTCOME.PARTIAL,
      headline: "Mission uploaded to Pixhawk · Agent package synchronization required",
      canRetrySync: true };
  }
  return { ...base, kind: OUTCOME.FAILED,
    headline: "Mission upload could not be verified", canRetrySync: false };
}

// ---- The shared readiness vocabulary (Map + Agent) ----------------------------------------
export const READINESS_STATE = {
  READY: "READY",
  VERIFYING: "VERIFYING",
  PACKAGE_SYNC_REQUIRED: "PACKAGE_SYNC_REQUIRED",
  SCOUT_UNREACHABLE: "SCOUT_UNREACHABLE",
  REAL_MISMATCH: "REAL_MISMATCH",
  NO_MISSION: "NO_MISSION",
};

export const READINESS_TEXT = {
  READY: "Agent package synchronized",
  VERIFYING: "Verifying Agent readiness…",
  PACKAGE_SYNC_REQUIRED: "Agent package synchronization required",
  SCOUT_UNREACHABLE: "Agent unreachable — package state unknown",
  REAL_MISMATCH: "Agent package does not match the approved mission",
  NO_MISSION: "No active mission for this vehicle",
};

// Scout's own transient readiness state. It means Scout is RE-DERIVING its verdict, which is not
// a claim about the package at all — presenting it as a mismatch is the specific lie this
// constant exists to prevent.
export const SCOUT_REFRESHING = "REPLANNING_READINESS_REFRESHING";

/**
 * The ONE readiness verdict Map and Agent both render.
 *
 * Precedence is deliberate, and every step of it exists because its opposite produced a false
 * warning on a healthy system:
 *
 *   1. no active mission            — nothing to be ready or unready about
 *   2. Scout is REFRESHING          — a re-derivation in flight is never a mismatch
 *   3. Scout is unreachable         — an unasked question is never a disagreement
 *   4. a PROVEN disagreement        — id/hash reported and different. This is the only
 *                                     REAL_MISMATCH, and it outranks an owed sync because it
 *                                     is a stronger, evidenced statement
 *   5. a sync is owed               — the backend durably recorded PACKAGE_SYNC_REQUIRED
 *   6. all three identities proven  — READY
 *   7. otherwise                    — VERIFYING: the comparison could not be completed. NOT a
 *                                     mismatch, and never rendered as one
 *
 * @param opts.publish   GET .../missions/publish body (record + package_sync_state), or null
 * @param opts.readiness GET .../replan/readiness body, or null
 * @param opts.refreshing  true while the station has a readiness read in flight
 */
export function readinessLabel({ publish = null, readiness = null, refreshing = false } = {}) {
  const pub = isObj(publish) ? publish : {};
  const rd = isObj(readiness) ? readiness : {};
  const pkg = isObj(rd.planning_package) ? rd.planning_package : {};
  const vm = isObj(rd.vehicle_mission) ? rd.vehicle_mission : {};
  const missionId = str(pub.mission_id) || str(vm.mission_id);
  const make = (state, detail) => ({ state, text: READINESS_TEXT[state], detail: detail || null });

  if (!missionId) return make(READINESS_STATE.NO_MISSION);

  const scoutState = str(pkg.scout_state);
  if (scoutState === SCOUT_REFRESHING || refreshing === true) {
    return make(READINESS_STATE.VERIFYING,
      scoutState === SCOUT_REFRESHING ? "Scout is re-deriving its replanning readiness." : null);
  }
  if (pkg.scout_reachable === false) {
    return make(READINESS_STATE.SCOUT_UNREACHABLE,
      "The package state could not be read, so it is neither confirmed nor contradicted.");
  }

  // A proven disagreement needs BOTH sides reported. `mission_id_match:false` with a null
  // package mission id is an unread comparison, not a mismatch — hence the explicit id check.
  const idReported = str(pkg.mission_id) != null;
  const idDisagrees = idReported && pkg.mission_id_match === false;
  const hashDisagrees = pkg.hash_mismatch === true;
  if (idDisagrees || hashDisagrees) {
    return make(READINESS_STATE.REAL_MISMATCH,
      hashDisagrees ? "The stored package route hash is not the approved route's hash."
        : "The stored package belongs to a different mission.");
  }

  if (str(pub.package_sync_state) === "REQUIRED") {
    return make(READINESS_STATE.PACKAGE_SYNC_REQUIRED, str(pub.package_sync_error));
  }
  if (pkg.stored === true && pkg.usable === true
      && pkg.mission_id_match === true && pkg.hash_match === true) {
    return make(READINESS_STATE.READY);
  }
  return make(READINESS_STATE.VERIFYING,
    "The package comparison has not completed — this is not a reported mismatch.");
}

/** True when the operator backend can already prove operator-level identity equality (the
 *  durable record says SYNCED and its hash is the one on the flight controller). A passive
 *  Scout readiness refresh must not overwrite that with a mismatch warning — callers pass this
 *  as `refreshing` suppression. */
export function operatorIdentityProven({ publish = null, readiness = null } = {}) {
  const pub = isObj(publish) ? publish : {};
  const vm = isObj(readiness) && isObj(readiness.vehicle_mission) ? readiness.vehicle_mission : {};
  return str(pub.package_sync_state) === "SYNCED"
    && str(pub.route_hash) != null
    && str(pub.route_hash) === str(vm.route_hash)
    && vm.readback_hash_match === true;
}
