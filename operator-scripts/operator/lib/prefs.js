// prefs.js — operator preferences persisted in THIS BROWSER only (localStorage).
// This is genuine client-side persistence, NOT a server profile: the backend has no
// config/preferences endpoint (known gap), so nothing here is ever sent to FastAPI.
// A value stored here does not retroactively affect a page that does not read it —
// migrated pages opt in via getPref(). Keep this honest: never imply a server save.

const KEY = "operator.prefs.v1";

export const PREF_DEFAULTS = {
  speed_units: "ms",       // ms | kn
  coord_format: "decimal", // decimal | dms
  base_layer: "streets",   // streets | dark | satellite
  clock_24h: true,
};

function load() {
  try {
    return { ...PREF_DEFAULTS, ...(JSON.parse(localStorage.getItem(KEY)) || {}) };
  } catch (e) {
    return { ...PREF_DEFAULTS };
  }
}

let cache = load();

/** Whether localStorage is usable at all (private mode / disabled → false). */
export function prefsPersistable() {
  try {
    const probe = "__op_probe__";
    localStorage.setItem(probe, "1");
    localStorage.removeItem(probe);
    return true;
  } catch (e) {
    return false;
  }
}

export function getPrefs() { return { ...cache }; }
export function getPref(k) { return cache[k]; }

/** Set one preference. Returns true only if it was actually persisted to disk. */
export function setPref(k, v) {
  cache = { ...cache, [k]: v };
  try {
    localStorage.setItem(KEY, JSON.stringify(cache));
    return true;
  } catch (e) {
    return false; // storage unavailable — caller must surface this honestly
  }
}

export function resetPrefs() {
  cache = { ...PREF_DEFAULTS };
  try { localStorage.removeItem(KEY); } catch (e) { /* noop */ }
  return getPrefs();
}
