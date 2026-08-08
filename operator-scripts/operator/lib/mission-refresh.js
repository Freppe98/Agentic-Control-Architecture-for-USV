// mission-refresh.js — pure policy for WHEN to re-download a vehicle's full Pixhawk
// mission, and how to tell whether the geometry vs. the execution progress changed. No DOM,
// no fetch, no timers: just the decision, so it is unit-tested directly
// (tests/mission-refresh.test.mjs) and the Map page only has to wire fetch + draw to it.
//
// The problem this solves: the full Pixhawk mission is expensive to download and must NOT
// be re-fetched on every 2 s heartbeat. It should be fetched only on the real triggers
// (selection, a mission-writing command that SUCCEEDED, a reported mission-revision change,
// a slow fallback, or an explicit manual Fetch) and, once fetched, cached per USV and NOT
// redrawn unless something actually changed. Everything here is keyed by USV id.
//
// GEOMETRY vs PROGRESS are deliberately separate signals:
//   • geometry identity (route_content_hash / full_mission_hash / hash / item count) — the
//     shape of the route. Unchanged geometry must NOT trigger a full overlay rebuild.
//   • progress (current_seq) — which leg is executing. A changed current_seq must still move
//     the active-waypoint marker / progress even when the geometry is byte-for-byte identical.

// The strongest stable GEOMETRY identity the mission read-back carries, in preference order.
// current_seq is intentionally EXCLUDED — progress is tracked separately (missionProgress).
export function missionIdentity(mission) {
  if (!mission || typeof mission !== "object") return null;
  if (mission.route_content_hash) return "rch:" + mission.route_content_hash;
  if (mission.full_mission_hash) return "fmh:" + mission.full_mission_hash;
  if (mission.hash) return "h:" + mission.hash;
  const count = mission.pixhawk_item_count != null ? mission.pixhawk_item_count : mission.count;
  if (count != null) return "c:" + count;
  return null;
}

// Execution progress: the current mission sequence (which leg is active), or null when the
// read-back does not report one.
export function missionProgress(mission) {
  if (!mission || typeof mission !== "object") return null;
  return mission.current_seq == null ? null : mission.current_seq;
}

// Reasons that ALWAYS force a fresh download regardless of cache — an explicit operator
// action or an event that means the on-vehicle mission may have just been rewritten.
//
// "stop" is one of them because Scout's Stop transaction restores the immutable ORIGINAL mission
// when a verified revised route is installed and rewinds it to its start. The geometry identity
// can legitimately come back UNCHANGED (a stop on a run that was never replanned), so the cache
// must not be trusted to notice: the sequence moved even when the route did not, and the overlay,
// the active-waypoint marker and the progress readout all have to be re-read from ground truth.
const FORCE_REASONS = new Set(["manual", "select", "command", "replan", "stop"]);

const DEFAULT_FALLBACK_MS = 20000; // slow safety refresh (task C.5: every 15–30 s)

/**
 * Per-USV mission-refresh tracker. Records the last successfully fetched geometry identity,
 * progress and time, plus the last mission-revision signal seen, and decides whether a given
 * trigger warrants a new download. Stateful but self-contained (no I/O) so it is trivially
 * testable.
 */
export function createMissionRefreshTracker({ fallbackMs = DEFAULT_FALLBACK_MS } = {}) {
  const recs = new Map(); // id -> { identity, progress, fetchedAt, revisionSignal }
  const rec = (id) => recs.get(id) || null;

  return {
    /** Record a SUCCESSFUL (reachable) read. Returns { geometryChanged, progressChanged }:
     *  geometryChanged drives a full overlay rebuild; progressChanged drives an active-
     *  waypoint/progress update even when the geometry is unchanged. */
    noteFetched(id, mission, now = Date.now()) {
      const identity = missionIdentity(mission);
      const progress = missionProgress(mission);
      const prev = rec(id);
      const geometryChanged = !prev || prev.identity !== identity;
      const progressChanged = !prev || prev.progress !== progress;
      recs.set(id, { identity, progress, fetchedAt: now, revisionSignal: prev ? prev.revisionSignal : null });
      return { geometryChanged, progressChanged };
    },

    /** Mark the latest mission-revision signal from the fleet feed as "seen" for a vehicle,
     *  so a subsequent identical signal does not re-trigger. */
    noteRevisionSignal(id, signal) {
      const prev = rec(id);
      recs.set(id, prev ? { ...prev, revisionSignal: signal }
        : { identity: null, progress: null, fetchedAt: 0, revisionSignal: signal });
    },

    /** The cached GEOMETRY identity for a vehicle, or null if never fetched. */
    identityOf(id) { const r = rec(id); return r ? r.identity : null; },
    /** The cached progress (current_seq) for a vehicle, or null. */
    progressOf(id) { const r = rec(id); return r ? r.progress : null; },

    /**
     * Should the full mission be downloaded now?
     * @param reason "select" | "manual" | "command" | "replan" | "revision" | "fallback"
     * @param revisionSignal the mission-revision value from the fleet feed for this vehicle.
     *        Undefined today (the backend does not surface one), so "revision" stays dormant.
     * @param inFlight true if a fetch for this vehicle+selection is already running.
     * @returns { fetch, why }
     */
    shouldFetch(id, { reason, revisionSignal, now = Date.now(), inFlight = false } = {}) {
      if (inFlight) return { fetch: false, why: "in-flight" };
      if (FORCE_REASONS.has(reason)) return { fetch: true, why: reason };
      const r = rec(id);
      if (reason === "revision") {
        if (revisionSignal == null) return { fetch: false, why: "no-revision-signal" };
        if (!r || r.identity == null) return { fetch: true, why: "revision-first" };
        if (r.revisionSignal !== revisionSignal) return { fetch: true, why: "revision-changed" };
        return { fetch: false, why: "revision-unchanged" };
      }
      if (reason === "fallback") {
        if (!r || !r.fetchedAt) return { fetch: true, why: "fallback-never" };
        return (now - r.fetchedAt) >= fallbackMs
          ? { fetch: true, why: "fallback-stale" }
          : { fetch: false, why: "fallback-fresh" };
      }
      return { fetch: false, why: "no-trigger" };
    },

    reset(id) { recs.delete(id); },
    clear() { recs.clear(); },
  };
}

/** The command types whose completion MAY mean the on-vehicle mission changed. */
export const MISSION_WRITE_COMMANDS = new Set(["MISSION_UPLOAD", "MISSION_CLEAR", "MISSION_REPLAN"]);

/** Terminal outcomes (from lib/command.js commandVerification) that count as a successful
 *  mission write — the mission on the vehicle was (re)written, so re-read it. */
export const MISSION_WRITE_SUCCESS = new Set(["VERIFIED", "EXECUTED"]);

/**
 * Whether a SETTLED mission-write command warrants a full mission re-read. A success
 * (verified/executed) always does. A plain rejected/failed/expired operation does NOT —
 * the on-vehicle mission is presumed unchanged — UNLESS its result explicitly flags that
 * the vehicle mission may be in an uncertain/partial state, in which case reading ground
 * truth is safer than trusting the last-known overlay.
 * @param outcome the normalized outcome string from commandVerification()
 * @param result the command's raw result object (may carry explicit uncertainty flags)
 */
export function missionWriteNeedsRefetch(outcome, result) {
  if (MISSION_WRITE_SUCCESS.has(outcome)) return true;
  if (result && typeof result === "object" && (
    result.vehicle_state_uncertain === true ||
    result.mission_state_uncertain === true ||
    result.partial === true)) return true;
  return false;
}
