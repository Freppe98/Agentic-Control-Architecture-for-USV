// quick-move.js — pure, DOM-free policy + view model for the Map's right-click
// "Move <vehicle> here" action. No imports beyond the shared distance/format helpers,
// so it is unit-tested directly (tests/quick-move.test.mjs).
//
// WHAT A QUICK MOVE IS. One OPERATOR-authority action: send the selected surface vehicle
// directly to a clicked latitude/longitude, after which Scout enters and VERIFIES LOITER.
// It is independent of mission planning in both directions — it works with no mission and
// no geometry at all, and it never uploads, replaces, clears or restores a mission, never
// arms the vehicle, never takes authority and never resumes a mission afterwards.
//
// THE THREE FACTS THIS MODULE REFUSES TO MERGE (the backend keeps them apart too; see
// quick_move.py):
//   DELIVERY   — did the command reach Scout? The queue record's own lifecycle, plus the
//                much stronger evidence of Scout naming the command id back to us.
//   MOVEMENT   — what is the vehicle doing? Scout's state + distance remaining.
//   COMPLETION — arrived AND confirmed holding. NEVER inferred from either of the above.
// A command that was delivered, acknowledged, or even reported EXECUTED by the queue has
// not arrived anywhere. `quickMoveView` will not produce an arrival phrase from anything
// except Scout's own COMPLETED + arrival_confirmed + loiter_verified.
//
// NO TIMERS DECIDE ANYTHING HERE. This module reads ages the backend computed on its
// monotonic clock and renders them; it never runs a countdown that flips a state, changes
// a flight mode or resumes a mission. The only thing elapsed time does on this surface is
// make a reading read as LAST KNOWN instead of current.

import { metersBetween, fmtDistance, fmtAgo } from "./home.js";

// --- eligibility reason codes -------------------------------------------------------
// These MUST stay identical to quick_move.py's REASON_ORDER, in the same order: the
// backend re-runs the same policy at dispatch, and a UI that disabled for one reason
// while the backend refused for another would tell the operator to fix the wrong thing.
// tests/quick-move.test.mjs reads both files and pins the two lists together.
export const QM_REASONS = [
  "not_reported",
  "not_supported",
  "link_not_current",
  "authority_unreadable",
  "not_operator",
  "disarmed",
  "armed_unknown",
  "nav_not_ready",
  "bad_target",
  "too_far",
  "busy",
];

// Operator-facing prose, authored once. Deliberately NOT copied from the backend's
// strings: the backend answers a caller, this answers the person holding the mouse.
const QM_TEXT = {
  not_reported: "This vehicle has not reported Quick move support.",
  not_supported: "Scout reports Quick move is unavailable on this vehicle.",
  link_not_current: "Scout is not connected. A Quick move is never queued for later delivery.",
  authority_unreadable: "Control authority could not be read — a Quick move is never sent on an assumed authority.",
  not_operator: "Take Control (OPERATOR) before commanding a Quick move.",
  disarmed: "The vehicle is disarmed — it cannot move.",
  armed_unknown: "The armed state is not currently known.",
  nav_not_ready: "Scout reports navigation readiness is unavailable.",
  bad_target: "That position is not a usable coordinate.",
  too_far: "That position is beyond the Quick move range Scout advertises.",
  busy: "A Quick move is already in flight for this vehicle.",
};

export const QM_RUNNING = ["STARTING", "MOVING", "ARRIVED"];
export const QM_TERMINAL = ["COMPLETED", "CANCELLED", "FAILED"];

// Movement state → the phrase shown on the card, and its tint class (the station's
// c/p/d/u comm-colour vocabulary, reused so one palette means one thing everywhere).
// NOTE what is NOT here: no state maps to a green "arrived" on its own. COMPLETED's
// wording is decided by `completion` below, because a COMPLETED that Scout did not back
// with arrival + verified LOITER evidence is a claim, not an arrival.
const QM_STATE_TEXT = {
  STARTING: ["Starting", "p"],
  MOVING: ["Moving to target", "p"],
  ARRIVED: ["At target — confirming hold", "p"],
  COMPLETED: ["Arrived and holding", "c"],
  CANCELLED: ["Cancelled", "u"],
  FAILED: ["Failed", "d"],
};

/** Scout's advertised per-vehicle capability, in the shape the backend publishes.
 *  A vehicle with no `quick_move` block at all reads as unsupported — being present in
 *  the fleet is never evidence a vehicle can perform a guided move. */
export function quickMoveCapability(v) {
  const qm = v && v.quick_move;
  const cap = qm && qm.capability;
  if (!cap) {
    return { supported: false, reason: QM_TEXT.not_reported, navReady: null,
             maxDistanceM: null, source: "absent" };
  }
  return {
    supported: cap.supported === true,
    reason: cap.reason || null,
    // Tri-state, deliberately: null means Scout said nothing about navigation readiness,
    // which is not the same as reporting it unavailable and does not disable anything.
    navReady: typeof cap.nav_ready === "boolean" ? cap.nav_ready : null,
    maxDistanceM: typeof cap.max_distance_m === "number" ? cap.max_distance_m : null,
    source: cap.source || "scout",
  };
}

/** The quick move the backend is currently tracking for this vehicle, or null. */
export function quickMoveCurrent(v) {
  const qm = v && v.quick_move;
  return (qm && qm.current) || null;
}

/**
 * May the menu item be pressed right now? Pure, and the ONLY place the Map decides it.
 * Mirrors quick_move.eligibility server-side, in the same order, so the disable reason
 * the operator reads is the refusal they would have got.
 *
 * @param v         the selected fleet vehicle row
 * @param opts      { connected, hasControl, authority, target, busy }
 *                  `hasControl` is the station's ONE write-enable predicate from
 *                  lib/authority.js (a CONFIRMED OPERATOR with no hand-off in flight) —
 *                  the same predicate every other command on the page is gated by, so
 *                  Quick move can never be enabled in a state where AUTO or RTL is not.
 *                  `authority` is the value Scout actually reported, used ONLY to choose
 *                  which refusal to show: "not readable" and "someone else holds it" are
 *                  different problems and send the operator to different remedies.
 * @returns {{ enabled, code, reason }}
 */
export function quickMoveGate(v, { connected = false, hasControl = false, authority = null,
                                   target = null, busy = false } = {}) {
  const deny = (code) => ({ enabled: false, code, reason: QM_TEXT[code] });
  const cap = quickMoveCapability(v);
  if (cap.source === "absent") return deny("not_reported");
  if (!cap.supported) {
    return { enabled: false, code: "not_supported", reason: cap.reason || QM_TEXT.not_supported };
  }
  if (!connected) return deny("link_not_current");
  if (!hasControl) {
    const value = authority == null ? null : String(authority).toUpperCase();
    if (value == null || value === "") return deny("authority_unreadable");
    // A reported OPERATOR that is nonetheless not `hasControl` means a hand-off request is
    // still in flight. Same refusal code, different sentence: telling the operator to Take
    // Control when they just pressed it would be nonsense.
    if (value === "OPERATOR") {
      return { enabled: false, code: "not_operator",
               reason: "Waiting for the vehicle to confirm the authority change." };
    }
    return deny("not_operator");
  }
  const armed = v && v.telemetry && typeof v.telemetry.armed === "boolean"
    ? v.telemetry.armed : null;
  if (armed === false) return deny("disarmed");
  if (armed === null) return deny("armed_unknown");
  // EXPLICITLY unavailable only — an absent nav_ready is Scout saying nothing, and this
  // station does not manufacture a readiness failure from a field nobody sent.
  if (cap.navReady === false) return deny("nav_not_ready");
  if (!validTarget(target)) return deny("bad_target");
  if (cap.maxDistanceM != null) {
    const d = vehicleDistanceTo(v, target);
    if (d != null && d > cap.maxDistanceM) {
      return { enabled: false, code: "too_far",
               reason: `That position is ${fmtDistance(d)} away; Scout advertises a ${fmtDistance(cap.maxDistanceM)} Quick move range.` };
    }
  }
  if (busy) return deny("busy");
  return { enabled: true, code: null, reason: null };
}

/** A usable WGS-84 destination, or false. (0,0) is refused as a no-fix sentinel. */
export function validTarget(t) {
  if (!t || typeof t !== "object") return false;
  const lat = Number(t.lat), lng = Number(t.lng);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return false;
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return false;
  return !(lat === 0 && lng === 0);
}

/** Metres from the vehicle's last reported position to a target, or null when the
 *  vehicle has no usable position. Null is the honest answer — the menu shows no
 *  distance rather than a distance from a fabricated origin. */
export function vehicleDistanceTo(v, target) {
  if (!validTarget(target) || !v || v.lat == null || v.lng == null) return null;
  return metersBetween(v.lat, v.lng, target.lat, target.lng);
}

/** "56.700300, 13.003500" — a fixed 6-decimal destination readout (~0.1 m), so the
 *  operator can read back exactly where they sent the vehicle. */
export function fmtTarget(t) {
  if (!validTarget(t)) return "—";
  return `${Number(t.lat).toFixed(6)}, ${Number(t.lng).toFixed(6)}`;
}

/** The menu item's label. The vehicle NAME, never its id: the operator picked a named
 *  vehicle and the menu has to say which one it is about, because a mis-targeted move
 *  is the failure this label exists to prevent. */
export function quickMoveLabel(v) {
  const name = (v && (v.name || (v.id != null ? `USV-${v.id}` : null))) || "vehicle";
  return `Move ${name} here`;
}

/**
 * The whole render model for the Quick move status card and the target marker.
 * Pure: everything it reports is either something Scout said or something the backend
 * measured — nothing is derived from how long the browser has had the page open.
 *
 * @param v      the selected fleet vehicle row
 * @param opts   { connected, command } — `command` is the QUICK_MOVE queue record for
 *               `current.command_id` when the page has it, used ONLY for the delivery
 *               line; it can never contribute an arrival or completion claim.
 * @returns null when there is nothing to show, else a flat view model.
 */
export function quickMoveView(v, { connected = false, command = null } = {}) {
  const cur = quickMoveCurrent(v);
  if (!cur) return null;
  const st = cur.status || null;
  // When Scout never reported a movement at all, the COMMAND's own terminal outcome is the
  // only thing that happened, and it is what the state line must say. Scout rejects a quick
  // move at its `prepare()` precondition (not armed, a mission actively bound, …) without
  // ever registering a movement record, so this is the normal shape of a refusal — not an
  // edge case. Reading "Sent — awaiting Scout" beside a rejection reason is the one thing
  // this line must never do.
  const [stateText, stateCls] = st ? (QM_STATE_TEXT[st.state] || [st.state, "u"])
    : commandOnlyState(command);

  // How old Scout's last word on this move is, and whether we may still present it as
  // current. A status we cannot refresh (link down) is LAST KNOWN — it is never re-read
  // as "still moving", which would be the station asserting a fact it cannot confirm.
  const statusAgeS = st && typeof st.status_age_s === "number" ? st.status_age_s : null;
  const lastKnown = !!st && (!connected || (statusAgeS != null && statusAgeS > STALE_STATUS_S));

  return {
    commandId: cur.command_id,
    target: cur.requested_target,
    targetText: fmtTarget(cur.requested_target),
    requestedAgo: fmtAgo(cur.requested_age_s),

    // --- DELIVERY (never an arrival claim) ---
    delivery: deliveryLine(cur, command),
    deliveryConfirmed: cur.delivery_confirmed === true,

    // --- MOVEMENT ---
    state: st ? st.state : null,
    stateText: lastKnown ? `${stateText} (last known)` : stateText,
    stateCls: lastKnown ? "u" : stateCls,
    lastKnown,
    statusAgo: st ? fmtAgo(statusAgeS) : null,
    // Distance remaining is Scout's number, shown only when Scout sent one. The station
    // never substitutes its own straight-line distance here: that would answer a
    // different question (how far away is the point) than the one the label asks (how
    // much further does the vehicle have to go).
    remainingText: st && st.distance_remaining_m != null
      ? fmtDistance(st.distance_remaining_m) : null,
    mode: st ? st.mode : null,
    reason: st ? st.reason : null,

    // --- COMPLETION (the operator's verdict, not Scout's word alone) ---
    completion: st ? st.completion : null,
    completionNote: completionNote(st),
    // The vehicle reached the target but the hold was NOT confirmed — a terminal state
    // that is neither a success nor a "nothing happened". Surfaced on its own because the
    // operational consequence is specific and urgent: the vehicle is sitting at the point
    // the operator chose, possibly drifting, and "Failed" alone does not say that.
    arrivedNotHolding: !!(st && st.terminal && st.state !== "COMPLETED"
                          && st.arrival_confirmed === true),
    // Scout's report contradicts itself (verified hold + a non-LOITER observed mode).
    modeIncoherent: !!(st && st.mode_incoherent),

    // --- supersession / cancellation ---
    cancelRequested: cur.cancel_requested || null,
    stopAvailable: stopAvailable(st, command),
    terminal: !!st && st.terminal === true,

    // The command record ended in a refusal while Scout's newest movement report still
    // shows the move running. Surfaced as its own note: neither half may be dropped.
    refusedNote: refusedWhileRunningNote(st, command),

    // A Scout driving to a different place than the operator clicked. Published as a
    // first-class flag because it is the one failure mode that would otherwise look
    // exactly like a normal, healthy move.
    targetMismatch: !!(st && st.target_mismatch),
    targetOffsetM: st ? st.target_offset_m : null,
    scoutTarget: st ? st.target : null,
  };
}

// Beyond this, a movement status is no longer presented as current even on a link the
// station still calls CONNECTED. Scout reports its quick-move status on every packet, so
// a gap this long means the reports stopped, not that nothing changed.
export const STALE_STATUS_S = 10;

/**
 * The delivery sentence. THREE outcomes, and the middle one is the point: when the
 * station genuinely does not know whether Scout got the command, it says so rather than
 * picking the comforting answer or the alarming one.
 */
export function deliveryLine(cur, command) {
  if (cur && cur.delivery_confirmed === true) {
    return { text: "Scout has this move.", cls: "c", uncertain: false };
  }
  const status = command && command.status;
  if (status === "REJECTED" || status === "FAILED") {
    return { text: command.reason || "Scout rejected the move.", cls: "d", uncertain: false };
  }
  if (status === "EXPIRED") {
    return { text: "Delivery not confirmed — Scout may or may not have received this move. "
                   + "Check the vehicle before sending another.", cls: "u", uncertain: true };
  }
  if (status === "EXECUTED" || status === "ACCEPTED") {
    // The queue says Scout took it, but Scout has not reported the move back yet. That is
    // delivery evidence and nothing more — explicitly not arrival, and not movement.
    return { text: "Accepted by Scout — awaiting movement status.", cls: "p", uncertain: false };
  }
  return { text: "Sending…", cls: "p", uncertain: false };
}

/**
 * The state line for a move Scout never reported on — the COMMAND's own outcome is all
 * there is. Pure; `command` is the QUICK_MOVE queue record, or null while the POST is in
 * flight.
 */
export function commandOnlyState(command) {
  const status = command && command.status;
  if (status === "REJECTED") return ["Rejected by Scout", "d"];
  if (status === "FAILED") return ["Failed", "d"];
  // EXPIRED covers every operator-side termination: the link went away before delivery, a
  // newer move replaced it, Stop was pressed, or nothing collected it in time. The command
  // record's own `reason` says which, and the delivery line renders that — so this stays
  // deliberately neutral rather than guessing at a cause it does not know.
  if (status === "EXPIRED") return ["Not delivered", "u"];
  if (status === "EXECUTED" || status === "ACCEPTED") return ["Accepted — awaiting movement", "p"];
  return ["Sent — awaiting Scout", "p"];
}

/**
 * May the card offer its Stop (LOITER) button?
 *
 * Scout's status block is NOT the first thing that happens: the Local Agent claims the
 * command, runs `prepare()`, then spends up to several seconds inside the blocking GUIDED
 * transition before any `active` block can reach this station. The vehicle may already be
 * under way during that window, so a card that withholds Stop until the first status has
 * arrived withholds it exactly when it is most likely to be wanted. The ordinary LOITER
 * button in the mode row is always there, but the card must not look as though the move
 * cannot be stopped yet.
 *
 * Offered while the move is still running, and — when Scout has not reported a movement at
 * all — while the COMMAND is still live. A command that was terminally refused with no
 * movement ever reported has nothing to stop, and offering a hold there would imply the
 * station believes something is moving when it has no reason to.
 */
export function stopAvailable(st, command) {
  if (st) return QM_RUNNING.includes(st.state);
  const status = command && command.status;
  return !(status === "REJECTED" || status === "FAILED" || status === "EXPIRED");
}

/**
 * The note for a move whose COMMAND record ended in a refusal while Scout's newest
 * movement report still shows it running — or null when that is not the case.
 *
 * This is the mirror of the `commandOnlyState()` case below, and Scout's own implementation
 * is what makes it reachable rather than theoretical. Scout registers its movement record
 * in `prepare()` — which is also when the first `STARTING` can be published — and only
 * THEN runs `confirm_dispatch()`, which can still refuse the move (superseded, authority
 * changed, link not CONNECTED, expired). The operator's own invalidation reaches the same
 * shape from the other side: a record EXPIRED by a link transition that Scout had already
 * claimed and acted on.
 *
 * Both halves are facts and neither may be dropped. Rendering only the running status would
 * show a healthy move over a command this station knows was refused; rendering only the
 * refusal would claim the vehicle stood down, which NOTHING here can establish — only Scout
 * reports that, and it has not. So the card says both, and says plainly which it cannot
 * confirm.
 */
export function refusedWhileRunningNote(st, command) {
  if (!st || st.terminal === true) return null;
  const status = command && command.status;
  const label = { REJECTED: "rejected by Scout", FAILED: "failed",
                  EXPIRED: "terminated by this station" }[status];
  if (!label) return null;
  const why = command.reason ? ` (${command.reason})` : "";
  const phrase = (QM_STATE_TEXT[st.state] || [st.state])[0].toLowerCase();
  return `The command for this move was ${label}${why}, but Scout's last report still `
       + `shows it "${phrase}". Only Scout can report that the vehicle stood down, and it `
       + "has not — check the vehicle and command LOITER if it is still moving.";
}

/** The caveat under a claimed completion, or null. A COMPLETED that Scout did not back
 *  with BOTH confirmed arrival and verified LOITER — or whose own report contradicts
 *  itself — is shown as Scout's claim, not as a finished hold; the same rule a mission
 *  upload read-back follows. */
export function completionNote(st) {
  if (!st || st.state !== "COMPLETED") return null;
  if (st.completion === "CONFIRMED") return null;
  if (st.mode_incoherent) {
    // The sharper of the two cases: Scout did claim both pieces of evidence, but the same
    // report says the vehicle is in another mode. Naming the contradiction is the useful
    // thing — "did not confirm verified LOITER" would be flatly wrong here, since it did.
    return `Scout reported a verified LOITER hold but also reports the vehicle in `
         + `${st.mode}. The report contradicts itself — treat the hold as unconfirmed.`;
  }
  const missing = [];
  if (st.arrival_confirmed !== true) missing.push("arrival");
  if (st.loiter_verified !== true) missing.push("verified LOITER");
  return `Scout reported the move complete but did not confirm ${missing.join(" or ")}. `
       + "Treat the hold as unconfirmed.";
}
