// lib/mission.js — pure Pixhawk-mission classification + progress/distance math.
//
// No DOM, no imports beyond lib/home.js's metersBetween: the single tested place for
// "how many waypoints are left / what % complete / how far to go" for a vehicle's
// Pixhawk mission readback (api.getPixhawkMission). Shared by Map.js (map overlay +
// progress bar), Mission.js (Mission Overview / Statistics) and Vehicle.js (Vehicle
// Health) so the three pages can never disagree on the same vehicle's numbers —
// mirrors the lib/home.js pattern (one tested policy, several DOM consumers).
import { metersBetween } from "./home.js";

// true = absolute-global frame (home candidate), false = relative/terrain global frame
// (a survey leg), null = unknown / non-global. Accepts MAVLink ints (0 = GLOBAL,
// 3/6/10/11 = relative/terrain) or the string spellings Scout forwards.
export function frameIsAbsGlobal(frame) {
  if (frame == null || frame === "") return null;
  if (typeof frame === "number") return frame === 0 ? true : [3, 6, 10, 11].includes(frame) ? false : null;
  const s = String(frame).toUpperCase();
  if (!s.includes("GLOBAL")) return null;
  return !(s.includes("RELATIVE") || s.includes("TERRAIN"));
}

/**
 * Split a Pixhawk mission's waypoints into { home, route }, sequence-ordered.
 * Pixhawk stores sequence 0 as a home / current-location item that is NOT part of the
 * stored survey: it typically uses MAV_FRAME_GLOBAL (absolute alt) while the survey legs
 * use MAV_FRAME_GLOBAL_RELATIVE_ALT, and it sits at the vehicle's current position —
 * often kilometres from the survey cluster. The lowest-seq item is a HOME candidate; it
 * is treated as HOME only when it is clearly not a survey leg — a distinct absolute-
 * global frame, OR a geographic outlier far from the rest of the cluster. Otherwise it
 * stays an ordinary route waypoint, so a mission whose seq 0 genuinely is a normal leg
 * is not mis-split.
 * @param waypoints raw waypoints array (only lat/lng-positioned ones are considered)
 */
export function classifyMissionWaypoints(waypoints) {
  const wps = (waypoints || []).filter((w) => w && w.lat != null && w.lng != null).slice()
    .sort((a, b) => (a.seq == null ? 0 : a.seq) - (b.seq == null ? 0 : b.seq));
  if (wps.length < 2) return { home: null, route: wps };
  const cand = wps[0], rest = wps.slice(1);
  const frameSaysHome = frameIsAbsGlobal(cand.frame) === true
    && rest.some((w) => frameIsAbsGlobal(w.frame) === false);
  // geographic outlier: candidate far from the cluster centroid relative to the
  // cluster's own span, and past an absolute floor so a tight cluster still splits.
  const cLat = rest.reduce((s, w) => s + w.lat, 0) / rest.length;
  const cLng = rest.reduce((s, w) => s + w.lng, 0) / rest.length;
  let span = 0;
  for (const w of rest) span = Math.max(span, metersBetween(cLat, cLng, w.lat, w.lng) || 0);
  const geoSaysHome = (metersBetween(cand.lat, cand.lng, cLat, cLng) || 0) > Math.max(span * 3, 400);
  return (frameSaysHome || geoSaysHome) ? { home: cand, route: rest } : { home: null, route: wps };
}

/**
 * Completed / remaining counts + progress % for a route against Scout's own
 * current_seq. `completed` = route waypoints strictly before the current one;
 * `remaining` = the current waypoint plus everything after it (completed + remaining
 * always equals total, so a "Completed / Remaining / Total" readout is internally
 * consistent). currentSeq == null → progress is genuinely unknown (never guessed):
 * completed/remaining/pct all come back null, not a fabricated 0%.
 * @returns {{ total, completed, remaining, currentIndex, pct }}
 */
export function missionCounts(route, currentSeq) {
  const total = route.length;
  if (currentSeq == null || !total) return { total, completed: null, remaining: null, currentIndex: -1, pct: null };
  let idx = route.findIndex((w) => w.seq === currentSeq);
  if (idx === -1) idx = route.findIndex((w) => w.seq != null && w.seq >= currentSeq);
  if (idx === -1) idx = total; // current is past the whole route (mission effectively done)
  const completed = Math.max(0, Math.min(idx, total));
  const remaining = total - completed;
  const pct = total ? Math.round((completed / total) * 100) : null;
  return { total, completed, remaining, currentIndex: idx, pct };
}

/**
 * Remaining route distance in metres: vehicle's current position → current waypoint →
 * each subsequent leg to the end of the route. Real geometry over real coordinates
 * (F-derived, never fabricated) — null when position or mission progress is unknown.
 */
export function remainingRouteDistanceM(route, currentSeq, vehLat, vehLng) {
  if (vehLat == null || vehLng == null || !route.length) return null;
  const { currentIndex } = missionCounts(route, currentSeq);
  if (currentIndex < 0) return null;
  const rest = route.slice(currentIndex);
  if (!rest.length) return 0;
  let total = metersBetween(vehLat, vehLng, rest[0].lat, rest[0].lng) || 0;
  for (let i = 0; i < rest.length - 1; i++) {
    total += metersBetween(rest[i].lat, rest[i].lng, rest[i + 1].lat, rest[i + 1].lng) || 0;
  }
  return total;
}

/**
 * Seconds to cover a distance at a given speed, or null when speed isn't a real,
 * meaningfully-positive live reading — never a divide-by-near-zero fake ETA.
 */
export function etaSeconds(distanceM, speedMps) {
  if (distanceM == null || speedMps == null || speedMps < 0.15) return null;
  return distanceM / speedMps;
}

/** Seconds → "3m 20s" / "48s" / "—". Compact, for ETA readouts. */
export function fmtDuration(s) {
  if (s == null) return "—";
  s = Math.max(0, Math.round(s));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60), rem = s % 60;
  if (m < 60) return `${m}m ${rem}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
