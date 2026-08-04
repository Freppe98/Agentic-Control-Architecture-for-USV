// upload-policy.js — pure Operator-side mission-upload eligibility. No DOM: the single
// tested place (tests/upload-policy.test.mjs) that gives the operator EARLY feedback about
// whether a MISSION_UPLOAD is likely to be accepted, mirroring the Scout safety policy:
//
//   Scout accepts an upload when either
//     • the vehicle is DISARMED and the existing preconditions pass; OR
//     • the vehicle is ARMED and freshly confirmed in LOITER and stationary within Scout's
//       safety threshold.
//
// This module NEVER duplicates or weakens Scout's authoritative check — the final stationary/
// groundspeed decision stays on Scout. It only shapes the Operator button (enabled/disabled +
// message) so the workflow (AUTO → LOITER → wait for confirmed LOITER → Upload) is clear.
// armed=true is NOT an unconditional blocker; armed+confirmed-LOITER is allowed.

export const UPLOAD_LEVEL = { OK: "ok", WARN: "warn", BLOCK: "block" };
export const CONFIRMED_LOITER_MODE = "LOITER";
// A soft, non-authoritative groundspeed hint (m/s). Only used to WARN when we happen to have
// fresh groundspeed — never to block (Scout owns the real threshold).
export const LOITER_SPEED_HINT_MPS = 0.5;

const ok = (message) => ({ allowed: true, level: UPLOAD_LEVEL.OK, reason: null, message });
const warn = (message) => ({ allowed: true, level: UPLOAD_LEVEL.WARN, reason: null, message });
const block = (reason) => ({ allowed: false, level: UPLOAD_LEVEL.BLOCK, reason, message: reason });

/**
 * @param connected        operator-side comm state is CONNECTED (telemetry current)
 * @param armed            true / false / null|undefined (field unavailable)
 * @param mode             canonical flight mode string, or null when unknown
 * @param modeFresh        whether the mode reading is current (default: connected)
 * @param groundspeed      m/s, or null when unavailable
 * @param hasAuthority     operator holds OPERATOR control authority
 * @param authorityRequired whether authority is required (default true)
 * @param missionPending   another mission operation (upload/clear) is already in progress
 * @param missionPendingReason what it is waiting for, e.g. "Mission upload executing — wait
 *   for it to finish before uploading again." Optional; when the caller can name the stage
 *   it is always preferred, because the generic sentence leaves an operator unable to tell a
 *   real in-flight upload from a stuck flag, or to judge whether waiting will help.
 * @returns { allowed, level, reason, message }
 */
export function uploadEligibility({
  connected, armed, mode, modeFresh, groundspeed,
  hasAuthority, authorityRequired = true, missionPending, missionPendingReason = null,
  loiterSpeedHint = LOITER_SPEED_HINT_MPS,
} = {}) {
  if (modeFresh === undefined) modeFresh = !!connected;

  if (missionPending) return block(missionPendingReason || "Another mission operation is already in progress.");
  if (!connected) return block("Vehicle is disconnected — upload unavailable.");
  if (authorityRequired && !hasAuthority) return block("Take OPERATOR control before uploading.");

  // ARMED must be freshly confirmed in LOITER (Scout does the final stationary check).
  if (armed === true) {
    const m = mode == null ? null : String(mode).toUpperCase();
    if (m == null || !modeFresh) return block("Waiting for fresh vehicle mode.");
    if (m !== CONFIRMED_LOITER_MODE) return block(`Armed upload requires confirmed LOITER (mode is ${m}).`);
    if (typeof groundspeed === "number" && Number.isFinite(groundspeed) && groundspeed > loiterSpeedHint) {
      return warn("Armed in LOITER, but groundspeed looks high — Scout may reject if above the safe LOITER threshold.");
    }
    return warn("Upload allowed while armed: USV is holding position in LOITER.");
  }

  // Disarmed → allowed on the existing preconditions. Unknown armed-state (field unavailable)
  // → do not block on it; Scout performs the authoritative safety check.
  if (armed === false) return ok("Vehicle is disarmed — upload permitted (Scout verifies safety).");
  return ok("Armed state unknown — upload permitted; Scout will enforce safety.");
}
