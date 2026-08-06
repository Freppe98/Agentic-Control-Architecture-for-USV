// home.js — pure Vehicle-Home status + deployment command-gating policy.
//
// No DOM, no imports: the single source of truth for (1) what state the Pixhawk
// HOME_POSITION is in for the selected Scout, and (2) which commands the Home-
// verification interlock enables. Imported by Map.js AND unit-tested directly
// (tests/home.test.mjs). The gating policy is a SAFETY rule, so it lives in exactly
// one tested place and is never re-implemented per button.
//
// Interlock policy (see the deployment workflow):
//   LOITER  — critical safety action (holds position, prevents drifting). Enabled
//             whenever connectivity + operator authority permit. NEVER Home-gated.
//   MANUAL  — enabled on the normal prerequisites (operator control). NEVER Home-gated.
//   SET_HOME— requires connectivity + a fresh valid GPS fix + operator control, and no
//             set-home already pending. Does not itself require a verified Home.
//   AUTO / RTL / RESUME — disabled until Home is VERIFIED (an old/garage Home is unsafe).

// --- Mode-control taxonomy (shared by Map + Vehicle so LOITER is presented the same) ---
// LOITER is the Scout's PRIMARY safety hold: it actively holds position (anti-drift), so
// it belongs in the primary mode row beside AUTO / MANUAL / RTL and stays available even
// when Home is unverified / mission readiness is false. SET_MODE_HOLD is a PASSIVE hold
// (the USV may drift with wind or current) kept only for backend compatibility — an
// advanced/secondary control, NEVER presented as LOITER's equal.
export const SAFETY_HOLD_TYPE = "SET_MODE_LOITER";
export const PRIMARY_MODES = ["SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_LOITER", "RTL"];
export const ADVANCED_MODES = ["SET_MODE_HOLD", "SET_MODE_GUIDED"];
export function isSafetyHold(type) { return type === SAFETY_HOLD_TYPE; }
// One place to author the LOITER affordance so both pages say the same thing.
export const SAFETY_HOLD_TITLE = "Active anti-drift safety hold. Always available.";

export const HOME_VERIFY_TOLERANCE_M = 5;

/** Great-circle distance in metres between two lat/lng points, or null if incomplete. */
export function metersBetween(aLat, aLng, bLat, bLng) {
  if ([aLat, aLng, bLat, bLng].some((v) => v == null || Number.isNaN(+v))) return null;
  const R = 6371000, toR = Math.PI / 180;
  const dLat = (+bLat - +aLat) * toR, dLng = (+bLng - +aLng) * toR;
  const la = +aLat * toR, lb = +bLat * toR;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la) * Math.cos(lb) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
}

/** Human distance: metres under 1 km, else km with one decimal. */
export function fmtDistance(m) {
  if (m == null) return "—";
  if (m < 1000) return `${m < 10 ? m.toFixed(1) : Math.round(m)} m`;
  return `${(m / 1000).toFixed(m < 10000 ? 1 : 0)} km`;
}

/** Seconds → "8 s ago" / "3 min ago", or "—". */
export function fmtAgo(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ago`;
}

/**
 * Settled + transient Home status for the inspector, marker and readiness block.
 *
 * `verified` (and everything else read off `v.home`) is Scout's OWN continuously-
 * reported status (payload.agent.home_status, normalized by the backend's
 * home_block()) — never something this function or the backend reconstructs from a
 * SET_HOME command's result. A command result only ever drives the transient
 * `phase` ("pending"/"failed") for immediate click feedback; it can never force
 * `state` to "verified" here. If Scout's status goes stale (link lost, Scout
 * restarted), `v.home.stale`/`v.home.verified` flipping is what un-verifies the UI —
 * this function just renders whatever the backend currently reports.
 *
 * @param v     fleet vehicle object (uses v.home, v.lat, v.lng)
 * @param opts  { phase: 'idle'|'pending'|'failed', failMessage, now (ms) }
 * @returns {{ state, available, verified, homeLat, homeLng, vehLat, vehLng,
 *             distanceM, verifiedAt, verifiedAgeS, verifiedDistanceM,
 *             verificationMethod, readyForAuto, readyForRtl, reachable, stale,
 *             reason, failMessage }}
 *   state: 'unknown' | 'unverified' | 'pending' | 'verified'
 */
export function homeStatus(v, { phase = "idle", failMessage = null, now = Date.now() } = {}) {
  const home = (v && v.home) || {};
  const available = home.available === true && home.lat != null && home.lng != null;
  const stale = home.stale === true;
  const verified = home.verified === true; // backend already forces this false when stale
  const homeLat = available ? +home.lat : null;
  const homeLng = available ? +home.lng : null;
  const vehLat = v && v.lat != null ? +v.lat : null;
  const vehLng = v && v.lng != null ? +v.lng : null;
  const distanceM = metersBetween(homeLat, homeLng, vehLat, vehLng);

  let verifiedAgeS = null;
  if (home.verified_at) {
    const t = new Date(home.verified_at).getTime();
    if (!Number.isNaN(t)) verifiedAgeS = Math.max(0, (now - t) / 1000);
  }

  let state;
  if (phase === "pending") state = "pending";
  else if (verified) state = "verified";
  else if (!available) state = "unknown";
  else state = "unverified";

  // Operator-facing reason when not verified (never shown for a verified Home).
  // Scout's own `reason` (e.g. a staleness explanation) wins when it sent one —
  // it is more accurate than anything we could infer locally.
  let reason = null;
  if (phase === "failed" && failMessage) reason = failMessage;
  else if (state !== "verified" && home.reason) reason = home.reason;
  else if (state === "unknown") reason = "Pixhawk Home has not been received.";
  else if (state === "unverified") {
    reason = distanceM != null
      ? `Pixhawk Home is ${fmtDistance(distanceM)} from Scout. Set Home at the deployment site before autonomous operation.`
      : "Pixhawk Home is not verified for this deployment site. Set Home before autonomous operation.";
  }

  return {
    state, available, verified, stale,
    homeLat, homeLng, vehLat, vehLng,
    distanceM,
    verifiedAt: home.verified_at || null,
    verifiedAgeS,
    verifiedDistanceM: home.verification_distance_m != null ? +home.verification_distance_m : null,
    verificationMethod: home.verification_method || null,
    readyForAuto: home.ready_for_auto === true,
    readyForRtl: home.ready_for_rtl === true,
    reachable: home.reachable == null ? null : !!home.reachable,
    reason,
    failMessage: phase === "failed" ? failMessage : null,
  };
}

// Commands the Home-verification interlock disables until Home is VERIFIED. Each
// reason is the ONE place its hover copy is authored — the single contextual
// explanation shown on the button itself (see the UI cleanup: the permanent
// "Deployment readiness" card is the only always-visible Home indicator; everything
// else, including these, is contextual/on-hover).
export const HOME_GATED = new Set(["SET_MODE_AUTO", "RTL", "MISSION_RESUME"]);
const HOME_REASONS = {
  SET_MODE_AUTO: "Set and verify Home before AUTO.",
  RTL: "RTL requires a verified Home.",
  MISSION_RESUME: "Verify Home before resuming.",
};

export function isHomeGated(type) { return HOME_GATED.has(type); }

/**
 * Build the commandGate context for a vehicle — the ONE place the gate's inputs are
 * derived, so no page re-implements "what counts as a valid position", "when is GPS
 * fresh", or "when is Home verified". Map and Vehicle both call this, which is what
 * makes their button enablement provably identical (tests/gating-parity.test.mjs).
 *
 * Everything derivable from the vehicle object is derived here. The rest are page-local
 * facts the vehicle object cannot supply, and are passed in:
 *   hasControl      — confirmed OPERATOR authority for THIS page's controller, already
 *                     resolved against link staleness (see lib/authority.js handoffGate).
 *   connected       — operator-side link state (commState(v) === "connected"); lives in
 *                     lib/ui.js, which this module deliberately does not import.
 *   missionLoaded   — from the page's own Pixhawk mission readback cache.
 *   setHomePending  — a Set Home request in flight from THIS page (Map only; the Vehicle
 *                     page has no Set Home control, so nothing can be pending from it).
 *   homePhase       — 'idle'|'pending'|'failed' transient Set-Home click feedback, so a
 *                     Home mid-change never reads as verified while it is being replaced.
 */
export function commandGateCtx(v, {
  hasControl = false, connected = false, missionLoaded = false,
  setHomePending = false, homePhase = "idle", now,
} = {}) {
  const hs = homeStatus(v, now == null ? { phase: homePhase } : { phase: homePhase, now });
  return {
    hasControl: !!hasControl,
    homeVerified: hs.state === "verified",
    connected: !!connected,
    posValid: !!(v && v.lat != null && v.lng != null),
    gpsFresh: !!connected,        // a current link is what makes the reported fix current
    missionLoaded: !!missionLoaded,
    setHomePending: !!setHomePending,
  };
}

/**
 * Gate one command against the deployment interlock.
 * @param type  backend command type (SET_MODE_AUTO, RTL, MISSION_RESUME, SET_MODE_LOITER, …)
 * @param ctx   { hasControl, homeVerified, connected, posValid, gpsFresh, setHomePending }
 * @returns {{ enabled, reason }} — reason is the Home-interlock explanation to show
 *          inline (null when the disable is the ordinary "no control" lock, handled
 *          elsewhere, or when enabled).
 */
export function commandGate(type, ctx = {}) {
  if (type === "SET_HOME") {
    let reason = null;
    if (!ctx.hasControl) reason = "Take Control (OPERATOR) to set Home.";
    else if (!ctx.connected) reason = "Scout is not connected.";
    else if (!ctx.posValid) reason = "No current Scout position — wait for a GPS fix.";
    else if (!ctx.gpsFresh) reason = "Scout position is stale — wait for a current, valid GPS fix.";
    else if (ctx.setHomePending) reason = "A Set Home request is already pending.";
    return { enabled: reason == null, reason };
  }
  // Every other vehicle command first needs confirmed OPERATOR control; that disable is
  // rendered by the shared lock note, so no Home reason is attached to it.
  if (!ctx.hasControl) return { enabled: false, reason: null };
  // LOITER (and MANUAL, ARM/DISARM, PAUSE) are NEVER Home-gated — LOITER in particular
  // is a critical anti-drift safety action that must stay available with an old Home.
  if (HOME_GATED.has(type) && !ctx.homeVerified) {
    return { enabled: false, reason: HOME_REASONS[type] };
  }
  return { enabled: true, reason: null };
}

// --- In-flight SET_HOME resolution (the "Setting…" flash can never hang) ----------
// The pending flash resolves on the command's queue lifecycle, but a lifecycle can
// stop arriving entirely: the Local Agent never reports a result, the operator
// backend restarts (the command queue is in-memory — main.py: "resets on restart"),
// so the tracked record simply vanishes, or the POST itself never settles. None of
// those produce a terminal status, so a UI that waits only for one waits forever.
//
// The deadline is READ OFF THE COMMAND (`expires_at`, stamped by the backend from
// COMMAND_TTL_S) rather than invented here, so the client and the backend can never
// disagree about when a command is dead. DEADLINE_SLACK lets the backend's own
// EXPIRED — which carries Scout's real reason — win the race; this is a backstop for
// when no status arrives at all, never the primary path.
export const SET_HOME_QUEUE_GRACE_MS = 15000;    // POST must confirm the command was queued
export const SET_HOME_LOST_GRACE_MS = 15000;     // queued record must appear in the poll
export const SET_HOME_DEADLINE_SLACK_MS = 5000;  // let the backend's own EXPIRED land first
export const SET_HOME_FALLBACK_TTL_MS = 315000;  // only if a record carries no expires_at

const SET_HOME_TERMINAL = new Set(["EXECUTED", "REJECTED", "FAILED", "EXPIRED"]);

/**
 * Resolve an in-flight SET_HOME into its click-feedback phase. Pure — the caller
 * supplies the tracked command record (or null) and the clock.
 *
 * Guarantees an exit: every branch either returns a terminal phase or is bounded by a
 * deadline, so "pending" can never be returned indefinitely for a fixed startedAt.
 *
 * Messages never claim Home is or is not set — on a timeout the operator genuinely
 * does not know, so they are told to re-check rather than given a fabricated verdict.
 *
 * @param cmd       the tracked command record from the queue poll, or null if absent
 * @param cmdId     the id we are tracking, or null while the POST is still in flight
 * @param startedAt ms timestamp of the click
 * @param now       ms
 * @returns {{ phase: 'pending'|'confirmed'|'failed', code, message }}
 */
export function setHomeOutcome({ cmd = null, cmdId = null, startedAt = 0, now = Date.now() } = {}) {
  const pending = { phase: "pending", code: null, message: null };
  const elapsed = now - startedAt;

  if (cmd && SET_HOME_TERMINAL.has(cmd.status)) {
    // A bare EXECUTED is NOT success: it means only that the Local Agent reached Scout
    // Flask. Only the backend's own home_result classification confirms Set Home.
    if (cmd.status === "EXECUTED" && cmd.home_result === "verified") {
      return { phase: "confirmed", code: null, message: null };
    }
    return {
      phase: "failed",
      code: String(cmd.home_result || cmd.status || "failed").toLowerCase(),
      message: cmd.reason || "Set Home was not accepted.",
    };
  }

  if (!cmdId) {
    // The POST has not returned a queued record yet. fetch() has no timeout of its own,
    // so a hung request would otherwise pend forever.
    if (elapsed <= SET_HOME_QUEUE_GRACE_MS) return pending;
    return {
      phase: "failed", code: "not_queued",
      message: "The operator backend did not confirm the Set Home request was queued. Home was not changed.",
    };
  }

  if (!cmd) {
    // Tracked, but the record is not in the queue: a poll that has not caught up yet
    // (fine, inside the grace) or a record that is gone for good — an operator-backend
    // restart wipes the in-memory queue, and no status will ever arrive for it.
    if (elapsed <= SET_HOME_LOST_GRACE_MS) return pending;
    return {
      phase: "failed", code: "lost",
      message: "Lost track of the Set Home command (the operator backend may have restarted). Re-check Home status before AUTO or RTL.",
    };
  }

  // Queued/SENT/ACCEPTED and still running: bounded by the backend's own TTL.
  let deadline = null;
  if (cmd.expires_at) {
    const t = new Date(cmd.expires_at).getTime();
    if (!Number.isNaN(t)) deadline = t + SET_HOME_DEADLINE_SLACK_MS;
  }
  if (deadline == null) deadline = startedAt + SET_HOME_FALLBACK_TTL_MS;
  if (now >= deadline) {
    return {
      phase: "failed", code: "timeout",
      message: "Scout never reported a result for Set Home. Home may or may not have been set — re-check Home status before AUTO or RTL.",
    };
  }
  return pending;
}

/**
 * VEHICLE DEPLOYMENT READINESS — is the vehicle itself fit to be operated?
 *
 * THREE CONCEPTS THAT ARE NOT THE SAME THING, and used to be conflated here:
 *
 *   1. Vehicle deployment readiness (THIS function) — Pixhawk connected, GPS ready, mission
 *      loaded, Home verified. Properties OF THE VEHICLE.
 *   2. Control owner — OPERATOR or LOCAL_AGENT. Reported alongside, never scored. It is a fact
 *      about who holds the wheel, not a defect.
 *   3. Agent mission readiness — Scout's canonical mission-execution state / can_start, the
 *      planning package's consistency, and the active verified mission identity + hash. Lives
 *      in the backend Start preflight and on the Map's Agent Mission card.
 *
 * "Operator authority" used to be a REQUIRED readiness item, which produced the exact
 * contradiction the bench test found: the Map read READY FOR MISSION while the operator held
 * control and flipped to NOT READY the moment authority moved to LOCAL_AGENT — i.e. it called
 * the vehicle unfit precisely when it was correctly configured to fly the mission. Handing
 * authority to the agent is what a mission REQUIRES; it can never make the vehicle "not ready".
 *
 * The converse is equally guarded: OPERATOR authority does not imply the agent mission is
 * startable. That question belongs to concept 3, which knows about the mission record, the
 * package and Scout's own eligibility — and to the Start transaction, which performs and
 * verifies the authority transfer itself.
 *
 * @param ctx { connected, gpsFresh, posValid, missionLoaded, homeVerified, hasControl, authority }
 * @returns {{ items, ready, loiterAvailable, controlOwner, blocksAgentMission }}
 *   ready         = every VEHICLE condition satisfied. Independent of who holds authority.
 *   controlOwner  = { value, label, isOperator, isAgent } — informational, never scored.
 *   loiterAvailable = LOITER remains an emergency/safety option even when !ready.
 */
export function deploymentReadiness(ctx = {}) {
  const gpsReady = !!(ctx.posValid && ctx.gpsFresh);
  const items = [
    { key: "pixhawk", label: "Pixhawk connected", ok: !!ctx.connected },
    { key: "gps", label: "GPS ready", ok: gpsReady },
    { key: "mission", label: "Mission loaded", ok: !!ctx.missionLoaded },
    { key: "home", label: "Home verified", ok: !!ctx.homeVerified },
  ];
  const authority = ctx.authority == null ? (ctx.hasControl ? "OPERATOR" : null)
    : String(ctx.authority).toUpperCase();
  return {
    items,
    ready: items.every((i) => i.ok),
    controlOwner: {
      value: authority,
      label: authority === "OPERATOR" ? "Operator"
        : authority === "LOCAL_AGENT" ? "Local Agent"
        : authority === "RC" ? "RC override" : "Unknown",
      isOperator: authority === "OPERATOR",
      isAgent: authority === "LOCAL_AGENT",
    },
    // LOITER only needs connectivity + operator control — independent of mission readiness.
    loiterAvailable: !!(ctx.connected && ctx.hasControl),
  };
}
