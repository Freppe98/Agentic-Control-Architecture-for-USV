// mission-refresh.js — pure policy for WHEN to re-download a vehicle's full Pixhawk
// mission, and how to tell whether the geometry actually changed. No DOM, no fetch, no
// timers: just the decision, so it is unit-tested directly (tests/mission-refresh.test.mjs)
// and the Map page only has to wire fetch + draw to it.
//
// The problem this solves: the full Pixhawk mission is expensive to download and must NOT
// be re-fetched on every 2 s heartbeat. It should be fetched only on the real triggers
// (selection, a mission-writing command that completed, a reported mission-revision change,
// a slow fallback, or an explicit manual Fetch) and, once fetched, cached per USV and NOT
// redrawn unless its identity changed. Everything here is keyed by USV id.

// The strongest stable identity the mission read-back carries, in preference order:
//   route_content_hash  — the mission-contract-v1 route identity (best; ignores Home)
//   full_mission_hash / hash — a whole-mission hash (Home included)
//   count / pixhawk_item_count — item count (weakest, but always present on a real read)
// Two reads with the same identity describe the same geometry, so the overlay need not be
// rebuilt. Returns null when nothing usable is present (an unreachable/empty read).
export function missionIdentity(mission) {
  if (!mission || typeof mission !== "object") return null;
  if (mission.route_content_hash) return "rch:" + mission.route_content_hash;
  if (mission.full_mission_hash) return "fmh:" + mission.full_mission_hash;
  if (mission.hash) return "h:" + mission.hash;
  const count = mission.pixhawk_item_count != null ? mission.pixhawk_item_count : mission.count;
  // current_seq is folded in so mission PROGRESS (which leg is active) still refreshes the
  // overlay even when the geometry/count is unchanged — otherwise a survey that advanced a
  // waypoint would keep drawing the old "current" marker until the count happened to change.
  if (count != null) return "c:" + count + "/s:" + (mission.current_seq == null ? "-" : mission.current_seq);
  return null;
}

// Reasons that ALWAYS force a fresh download regardless of cache — an explicit operator
// action or an event that means the on-vehicle mission may have just been rewritten.
const FORCE_REASONS = new Set(["manual", "select", "command", "replan"]);

const DEFAULT_FALLBACK_MS = 20000; // slow safety refresh (task C.5: every 15–30 s)

/**
 * Per-USV mission-refresh tracker. Records the last successfully fetched identity + time
 * and the last mission-revision signal seen, and decides whether a given trigger warrants
 * a new download. Stateful but self-contained (no I/O) so it is trivially testable.
 */
export function createMissionRefreshTracker({ fallbackMs = DEFAULT_FALLBACK_MS } = {}) {
  const recs = new Map(); // id -> { identity, fetchedAt, revisionSignal }
  const rec = (id) => recs.get(id) || null;

  return {
    /** Record a SUCCESSFUL (reachable) read. Returns whether the identity changed vs the
     *  previous cached one — the Map page uses this to skip a redundant overlay rebuild. */
    noteFetched(id, mission, now = Date.now()) {
      const identity = missionIdentity(mission);
      const prev = rec(id);
      const changed = !prev || prev.identity !== identity;
      recs.set(id, { identity, fetchedAt: now, revisionSignal: prev ? prev.revisionSignal : null });
      return changed;
    },

    /** Mark the latest mission-revision signal from the fleet feed as "seen" for a vehicle,
     *  so a subsequent identical signal does not re-trigger. */
    noteRevisionSignal(id, signal) {
      const prev = rec(id);
      recs.set(id, prev ? { ...prev, revisionSignal: signal } : { identity: null, fetchedAt: 0, revisionSignal: signal });
    },

    /** The cached identity for a vehicle, or null if never fetched. */
    identityOf(id) { const r = rec(id); return r ? r.identity : null; },

    /**
     * Should the full mission be downloaded now?
     * @param id vehicle id
     * @param reason "select" | "manual" | "command" | "replan" | "revision" | "fallback"
     * @param revisionSignal the mission-revision value from the fleet feed for this vehicle
     *        (active_revision_id / route_hash / waypoint_count / …). Undefined today — the
     *        backend does not yet surface one — so "revision" triggers stay dormant until it
     *        does, exactly as documented in main.py's fleet-payload extension point.
     * @param inFlight true if a fetch for this vehicle is already running (overlap guard)
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

/** The set of command types whose successful completion means the on-vehicle mission was
 *  (or may have been) rewritten and must be re-read. Used by the Map page to trigger a
 *  "command" refetch when one of these reaches a terminal, successful state. */
export const MISSION_WRITE_COMMANDS = new Set(["MISSION_UPLOAD", "MISSION_CLEAR", "MISSION_REPLAN"]);
