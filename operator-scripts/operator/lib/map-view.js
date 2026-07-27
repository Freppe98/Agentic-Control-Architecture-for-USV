// map-view.js — pure initial-map-view selection + coordinate validation + viewport
// persistence for the Plan page (and reusable by any map page). No Leaflet, no DOM beyond
// localStorage; the picking logic is unit-tested (tests/map-view.test.mjs).
//
// The Plan page used to always open on a hardcoded Toftasjön coordinate. This module
// replaces that with a priority order, keeping mission coordinates in WGS84 and validating
// every candidate before it is used:
//   1. fresh position of the currently selected USV
//   2. fresh position of another fleet USV (most recently contacted)
//   3. browser/operator geolocation (async — supplied once permission resolves)
//   4. last saved Plan-page viewport (localStorage)
//   5. Toftasjön fallback (only when nothing else is valid)
//
// A lower rank number is a stronger source. The page tracks the rank it last centred on and
// only recentres for a STRICTLY stronger source (and only if the operator has not manually
// panned/zoomed), so an async geolocation fix can upgrade the fallback view but a later,
// even stronger fleet fix can still upgrade the geolocation one.

export const VIEW_RANK = { selected: 1, fleet: 2, geolocation: 3, saved: 4, fallback: 5 };
export const DEFAULT_ZOOM = 16;
// The historical Toftasjön coordinate — kept ONLY as the last-resort fallback.
export const TOFTASJON = [56.699893, 13.002148];
export const DEFAULT_MAX_FRESH_AGE_S = 120; // a position older than this is not "fresh"

const SAVED_VIEW_KEY = "operator.plan.viewport.v1";

let storage = (() => {
  try { return typeof localStorage !== "undefined" ? localStorage : null; } catch (e) { return null; }
})();

/** A finite, in-range WGS84 coordinate. Rejects NaN/Infinity/strings and out-of-range. */
export function isValidLatLng(lat, lng) {
  return typeof lat === "number" && typeof lng === "number" &&
    Number.isFinite(lat) && Number.isFinite(lng) &&
    lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180;
}

/** Null Island (0,0) and its immediate neighbourhood — the classic "no fix / default 0"
 *  artefact, never a real survey position. */
export function isNullIsland(lat, lng, eps = 1e-4) {
  return Math.abs(lat) < eps && Math.abs(lng) < eps;
}

/** A vehicle's position IF it is usable: valid, not Null Island, and fresh (last contact
 *  within maxAgeS). Returns { lat, lng, ageS } or null. A null/absent age is treated as
 *  NOT fresh — priorities 1 & 2 require a genuinely current fix. */
export function freshVehiclePosition(v, maxAgeS = DEFAULT_MAX_FRESH_AGE_S) {
  if (!v) return null;
  const lat = v.lat, lng = v.lng, ageS = v.last_seen_age_s;
  if (!isValidLatLng(lat, lng) || isNullIsland(lat, lng)) return null;
  if (ageS == null || !(ageS <= maxAgeS)) return null;
  return { lat, lng, ageS };
}

/** The most recently contacted OTHER fleet USV with a fresh valid position, or null.
 *  "Most recently contacted" = smallest last_seen_age_s. */
export function bestFleetPosition(fleet, { excludeId = null, maxAgeS = DEFAULT_MAX_FRESH_AGE_S } = {}) {
  let best = null;
  (fleet || []).forEach((v) => {
    if (!v || v.id === excludeId) return;
    const p = freshVehiclePosition(v, maxAgeS);
    if (!p) return;
    if (!best || p.ageS < best.ageS) best = { ...p, id: v.id };
  });
  return best;
}

/**
 * Pick the strongest currently-available initial view. Any source may be absent (fleet
 * empty at first paint, geolocation not yet granted, no saved viewport); the fallback is
 * always returned if nothing better is valid. Callers pass whatever they currently have and
 * compare the returned `rank` to what they last centred on.
 *
 * @returns { center:[lat,lng], zoom, source, rank }
 */
export function pickInitialView({
  selected = null,       // the selected vehicle object (or null)
  fleet = [],            // full fleet array
  selectedId = null,     // selected vehicle id (to exclude from the fleet scan)
  geo = null,            // { lat, lng } from the browser Geolocation API, once resolved
  saved = null,          // { center:[lat,lng], zoom } from localStorage
  fallback = TOFTASJON,  // [lat,lng]
  maxFreshAgeS = DEFAULT_MAX_FRESH_AGE_S,
  defaultZoom = DEFAULT_ZOOM,
} = {}) {
  const sel = freshVehiclePosition(selected, maxFreshAgeS);
  if (sel) return { center: [sel.lat, sel.lng], zoom: defaultZoom, source: "selected", rank: VIEW_RANK.selected };

  const other = bestFleetPosition(fleet, { excludeId: selectedId, maxAgeS: maxFreshAgeS });
  if (other) return { center: [other.lat, other.lng], zoom: defaultZoom, source: "fleet", rank: VIEW_RANK.fleet };

  if (geo && isValidLatLng(geo.lat, geo.lng) && !isNullIsland(geo.lat, geo.lng))
    return { center: [geo.lat, geo.lng], zoom: defaultZoom, source: "geolocation", rank: VIEW_RANK.geolocation };

  if (saved && Array.isArray(saved.center) && isValidLatLng(saved.center[0], saved.center[1]))
    return { center: [saved.center[0], saved.center[1]], zoom: saved.zoom || defaultZoom, source: "saved", rank: VIEW_RANK.saved };

  return { center: [fallback[0], fallback[1]], zoom: defaultZoom, source: "fallback", rank: VIEW_RANK.fallback };
}

/** Read the last saved Plan-page viewport, or null if none/invalid. */
export function getSavedViewport() {
  try {
    const raw = storage && storage.getItem(SAVED_VIEW_KEY);
    const obj = raw ? JSON.parse(raw) : null;
    if (obj && Array.isArray(obj.center) && isValidLatLng(obj.center[0], obj.center[1]))
      return { center: [obj.center[0], obj.center[1]], zoom: typeof obj.zoom === "number" ? obj.zoom : DEFAULT_ZOOM };
  } catch (e) { /* ignore */ }
  return null;
}

/** Persist the Plan-page viewport. Silently no-ops when storage is unavailable or the
 *  coordinate is invalid (never persist a garbage centre). */
export function setSavedViewport(center, zoom) {
  if (!Array.isArray(center) || !isValidLatLng(center[0], center[1])) return false;
  try {
    if (storage) storage.setItem(SAVED_VIEW_KEY, JSON.stringify({ center: [center[0], center[1]], zoom }));
    return true;
  } catch (e) { return false; }
}

/** Test seam. */
export function _setStorageForTest(s) { storage = s; }
