// selection.js — the single, shared "which USV is selected" store for the operator
// station. Before this, the Map page held `selId` and the Plan page held
// `model.vehicleId` as two INDEPENDENT selections; opening Plan after selecting a
// vehicle on the Map lost that context. This module is the one source of truth both
// pages read/write, so the selection follows the operator across pages.
//
// It is deliberately tiny and NOT a fleet-state store: it holds only an id (plus a
// last-selected timestamp), keyed per browser/operator-station in localStorage. It never
// invents a vehicle and never fetches — the fleet feed (api.getFleet) stays the authority
// on what a vehicle actually is. Multiple USVs and multiple operator stations are
// preserved: this is per-station state, and the value is only ever a USV id.
//
// The stored value is a CANONICAL vehicle id and nothing else — never a display name,
// never a fleet-array index. That matters with several live USVs: a vehicle renaming
// itself (USV-3 → SAR-001), the fleet list reordering, or the selected vehicle going
// stale must not move the operator's selection. Only an explicit selection changes it.
//
// No DOM beyond localStorage; the subscribe mechanism is a plain observer set so it is
// unit-testable (tests/selection.test.mjs) with an injected storage.

const KEY = "operator.selection.v1";

// Injectable storage so tests can exercise persistence without a real localStorage, and
// so a private-mode browser (localStorage throws) degrades to in-memory instead of
// crashing. Defaults to window.localStorage when present.
let storage = (() => {
  try { return typeof localStorage !== "undefined" ? localStorage : null; } catch (e) { return null; }
})();

let state = load();
const listeners = new Set();

function load() {
  try {
    const raw = storage && storage.getItem(KEY);
    const obj = raw ? JSON.parse(raw) : null;
    const id = obj && normalizeId(obj.id);
    return { id: id, selectedAt: obj && typeof obj.selectedAt === "number" ? obj.selectedAt : null };
  } catch (e) {
    return { id: null, selectedAt: null };
  }
}

function persist() {
  try { if (storage) storage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* private mode — in-memory only */ }
}

// Canonical vehicle id — the frontend mirror of the backend's one identity policy
// (operator-scripts/vehicle_registry.py). Every spelling of the SAME vehicle folds to one
// value, so a number and a string can never be two selections of one USV:
//
//   2, "2", "usv-2", "USV-2"  → 2                (numeric identity, what the fleet rows use)
//   "sar-001", "SAR_001"      → "sar-001"        (a vehicle with no numeric identity)
//   null / "" / {}            → null             (names no vehicle)
//
// A display name is NOT an identity and is deliberately not resolvable here: the operator
// station must never key selection or per-USV state by a name a vehicle can change.
export function canonicalVehicleId(v) {
  if (v == null || typeof v === "boolean") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string") return null;
  const token = v.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  if (!token) return null;
  if (/^\d+$/.test(token)) return Number(token);
  const m = /^usv-(\d+)$/.exec(token);
  return m ? Number(m[1]) : token;
}

/** True when two id spellings name the same vehicle. Use instead of a bare `===` on ids. */
export function sameVehicle(a, b) {
  const x = canonicalVehicleId(a);
  return x !== null && x === canonicalVehicleId(b);
}

function normalizeId(v) { return canonicalVehicleId(v); }

/** The currently selected USV id (number), or null if none. */
export function getSelectedVehicleId() { return state.id; }

/** Epoch ms of the most recent selection, or null. Lets a page prefer the most recently
 *  selected vehicle when several are available (Plan-page centering priority 2). */
export function getSelectedAt() { return state.selectedAt; }

/** Select a USV (or clear with null). No-ops (no notify, no timestamp bump) when the id
 *  is unchanged, so a repeated select from a poll never churns subscribers. Returns the
 *  normalized id actually stored. */
export function setSelectedVehicleId(id, now = Date.now()) {
  const norm = normalizeId(id);
  if (norm === state.id) return state.id;
  state = { id: norm, selectedAt: norm == null ? null : now };
  persist();
  listeners.forEach((fn) => { try { fn(norm); } catch (e) { /* observer must not break the setter */ } });
  return norm;
}

/** Subscribe to selection changes. Returns an unsubscribe function. */
export function subscribeSelection(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/** Test seam: swap the backing storage (and reload state from it). Not used in the app. */
export function _setStorageForTest(s) { storage = s; state = load(); listeners.clear(); }
