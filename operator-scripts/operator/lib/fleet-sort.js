// fleet-sort.js — deterministic ordering for the Fleet roster. Pure, no DOM, no fetch.
//
// Why this exists
// ---------------
// The Fleet table defaulted to `{ key: "age", dir: -1 }` — sort by LAST CONTACT, recomputed
// on every 2 s poll. `last_seen_age_s` changes continuously and independently per vehicle,
// so with two USVs posting at slightly different moments their ages cross and Scout/SAR-001
// swapped rows on their own. Row position is how an operator finds a vehicle; it must not
// be a function of which packet happened to land last.
//
// Two smaller defects came with it: the "Vehicle" comparator did `a.id - b.id`, which is NaN
// for a vehicle whose canonical id is a string (an undefined ordering), and there was no
// tie-breaker at all — equal values (two null batteries, two vehicles with no health signal)
// left order to whatever the response happened to contain.
//
// Rules
// -----
//   • Default order is CANONICAL ID ascending — a value that does not change with traffic.
//     It matches the registry/backend order (usv-1, usv-2 Scout, usv-3 SAR-001), so Scout
//     stays above SAR-001 regardless of who reported most recently.
//   • Every sort, including operator-chosen ones, breaks ties on canonical id. Two rows can
//     never trade places while their sort values are equal.
//   • Live-value sorts are compared on a QUANTIZED key (whole-second age, integer battery),
//     so two nearly equal values do not reorder at the poll rate. The displayed value is
//     untouched — only the comparison is coarse.
//   • Sorting never mutates the array it is given; the caller's fleet state is shared with
//     the rollup, the counts and the telemetry cache.
//   • Selection is not part of any comparator — a selected row never jumps to the top.
import { commState } from "./ui.js";
import { deriveHealth, healthRank } from "../components/HealthBadge.js";

/** The default ordering: canonical id ascending. Never "most recently updated". */
export const DEFAULT_SORT = { key: "default", dir: 1 };

const commRank = { connected: 0, partitioned: 1, disconnected: 2, unknown: 3 };

/**
 * Total order over canonical vehicle ids, which may be numbers (2, 3) or slug strings
 * ("sar-001") in one fleet. Numeric ids sort numerically and ahead of string ids; string
 * ids sort lexicographically. Deterministic for every pair, so it is a safe tie-breaker.
 */
export function compareVehicleIds(a, b) {
  const an = typeof a === "number" && Number.isFinite(a);
  const bn = typeof b === "number" && Number.isFinite(b);
  if (an && bn) return a - b;
  if (an) return -1;
  if (bn) return 1;
  const as = a == null ? "" : String(a);
  const bs = b == null ? "" : String(b);
  return as < bs ? -1 : as > bs ? 1 : 0;
}

// Sort keys → a comparable number for one row. `null` means "this row has no value for
// this key"; those sort last in ascending order rather than pretending to be 0 or -1.
// Live values are quantized so jitter below the quantum cannot reorder rows.
const KEYS = {
  // Registry/canonical order. Handled by the tie-breaker alone — no live input at all.
  default: () => 0,
  name: () => 0,                                         // "Vehicle" column = canonical order
  comm: (v) => commRank[commState(v)] ?? commRank.unknown,
  // Whole seconds: a 0.3 s vs 0.4 s difference between two vehicles' packets is arrival
  // jitter, not information, and must not move a row.
  age: (v) => (v.last_seen_age_s == null ? null : Math.floor(v.last_seen_age_s)),
  health: (v) => { const h = deriveHealth(v); return h ? healthRank[h.sev] : null; },
  batt: (v) => (v.battery == null ? null : Math.round(v.battery)),
  cov: (v) => (v.coverage == null ? null : Math.round(v.coverage)),
};

/** True when `key` is a column the roster can sort by. */
export function isSortKey(key) { return Object.prototype.hasOwnProperty.call(KEYS, key); }

/**
 * A stably ordered COPY of `rows`.
 *
 * @param rows  fleet records from GET /api/fleet/status
 * @param sort  { key, dir } — dir 1 ascending, -1 descending. Defaults to DEFAULT_SORT.
 */
export function sortFleet(rows, sort = DEFAULT_SORT) {
  if (!Array.isArray(rows)) return [];
  const { key, dir } = sort && isSortKey(sort.key) ? sort : DEFAULT_SORT;
  const valueOf = KEYS[key];
  const direction = dir < 0 ? -1 : 1;
  return [...rows].sort((a, b) => {
    const x = valueOf(a);
    const y = valueOf(b);
    if (x !== y) {
      // Rows with no value for this key sort last in BOTH directions — "unknown" is not
      // an extreme of the scale, and letting it flip ends would move rows on every reverse.
      if (x == null) return 1;
      if (y == null) return -1;
      return (x - y) * direction;
    }
    // Equal (or equally absent) sort values → canonical id, always ascending. This is what
    // makes the order reproducible from the data alone rather than from arrival order.
    return compareVehicleIds(a.id, b.id);
  });
}

/**
 * The sort a header click produces: same column toggles direction, a new column starts in
 * its natural direction (ascending for identity/severity, descending for "most/newest
 * first" numerics). An explicitly chosen sort then STAYS active — polls never reset it.
 */
export function nextSort(current, key) {
  if (!isSortKey(key)) return current || DEFAULT_SORT;
  if (current && current.key === key) return { key, dir: current.dir < 0 ? 1 : -1 };
  return { key, dir: key === "name" || key === "comm" ? 1 : -1 };
}
