// mission-lock.js — the bounded "a mission write is in progress" lock, as a pure function
// (unit-tested, tests/mission-lock.test.mjs). No DOM, no network.
//
// WHY THIS EXISTS
// The Plan page held the lock as a single page-local flag: `model.upload.phase ===
// "uploading"`. Nothing bounded it. It was cleared in exactly one place — when a polled
// command record reached a terminal state — so every path that never produced such a
// record left the operator permanently unable to upload, with the generic message
// "Another mission operation is already in progress." while the backend queue was empty:
//
//   • the finalize request rejected (backend restart, connection reset, tab offline).
//     `phase` stayed "uploading" with `cmdId: null` and nothing ever looked at it again.
//   • the tracked command was not in the queue (a different vehicle selected, a queue that
//     no longer holds the id). The sync returned early on the missing record, forever.
//   • no timeout of any kind; `upload.at` was recorded and never read.
//
// THE OWNERSHIP RULE THIS ENCODES
// The frontend flag is only an OPTIMISTIC lock covering the short window between the
// operator pressing Upload and the command becoming visible in the backend queue. Once the
// command is visible, the BACKEND is the authority: it owns the lifecycle and expires any
// non-terminal command at its TTL (main.py `_expire_stale_commands`), and the caller reads
// that through hasPendingOfType(). So this module never has to invent a lifecycle — it only
// has to refuse to hold a lock that no backend record supports.
//
// The lock therefore blocks only while a real operation is running, and every exit is
// bounded: settled, submit-timeout, or tracking-lost.
import { missionUploadStage } from "./mission-upload.js";

/** A finalize request that has produced no command id in this long is not going to. */
export const SUBMIT_TIMEOUT_MS = 20000;
/** How long a tracked command may be absent from the polled queue before we stop believing
 *  in it. Comfortably longer than the 3 s command poll, so a slow poll never releases a
 *  genuinely running upload. */
export const TRACKING_GRACE_MS = 20000;

const IN_PROGRESS = "uploading";

/**
 * @param phase            model.upload.phase
 * @param cmdId            the tracked MISSION_UPLOAD command id, or null before it exists
 * @param startedAt        epoch ms when the operator pressed Upload (model.upload.at)
 * @param commands         the polled command list for the selected vehicle
 * @param missionUpload    Scout's live agent.mission_upload block, or null
 * @param now              epoch ms (injectable for tests)
 * @returns {{
 *   locked: boolean,          // may a new mission write start?
 *   state: string,            // idle | submitting | in_flight | settled | submit_timeout | tracking_lost
 *   label: string|null,       // operator-facing "what is it waiting for", never generic
 *   release: {phase,error}|null  // a model patch the caller must apply to end a dead lock
 * }}
 */
export function missionLockState({
  phase, cmdId = null, startedAt = 0, commands = null, missionUpload = null,
  now = Date.now(), submitTimeoutMs = SUBMIT_TIMEOUT_MS, trackingGraceMs = TRACKING_GRACE_MS,
} = {}) {
  if (phase !== IN_PROGRESS) {
    return { locked: false, state: "idle", label: null, release: null };
  }
  const age = startedAt ? now - startedAt : 0;

  // ── the optimistic window: the finalize call has not yielded a command id yet ──
  if (cmdId == null) {
    if (age > submitTimeoutMs) {
      return {
        locked: false, state: "submit_timeout", label: null,
        release: { phase: "error", error: "The upload request never reached the operator backend — nothing was sent to the vehicle. The plan is preserved; try again." },
      };
    }
    return { locked: true, state: "submitting", label: "Submitting the mission to the operator backend", release: null };
  }

  // ── the command exists: the backend queue is the authority from here on ──
  const cmd = Array.isArray(commands) ? commands.find((c) => c && c.id === cmdId) : null;
  if (!cmd) {
    // Not in the queue. Either the poll has not caught up (normal, briefly) or the backend
    // has no record of it at all (a restart that lost the queue, a vehicle switch). Only the
    // second is a dead lock, and only time can tell them apart.
    if (age > trackingGraceMs) {
      return {
        locked: false, state: "tracking_lost", label: null,
        release: { phase: "error", error: "The operator backend has no record of this upload — it may have been lost in a restart. The mission on the vehicle is unchanged unless a later readback says otherwise; re-check the Pixhawk mission before retrying." },
      };
    }
    return { locked: true, state: "submitting", label: "Waiting for the operator backend to queue the upload", release: null };
  }

  const stg = missionUploadStage(cmd, missionUpload);
  if (stg.state === "done" || stg.state === "failed") {
    // Terminal. The caller's own sync turns this into "uploaded"/"error"; the lock is
    // already open, so a settled command can never hold it.
    return { locked: false, state: "settled", label: null, release: null };
  }
  // Genuinely in flight. Say WHAT it is waiting for — "Requested" / "Executing" —
  // never the generic "another mission operation".
  return { locked: true, state: "in_flight", label: `Mission upload ${stg.stage.toLowerCase()}`, release: null };
}

/**
 * The message the upload gate should carry while the lock is held. Deliberately specific:
 * an operator told "another mission operation is already in progress" cannot tell a real
 * in-flight upload from a stuck flag, and has no idea whether waiting will help.
 */
export function lockMessage(lock) {
  if (!lock || !lock.locked) return null;
  return `${lock.label} — wait for it to finish before uploading again.`;
}
