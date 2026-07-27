// telemetry-cache.js — pure, per-USV last-known telemetry merge. No DOM, no fetch.
//
// The Map page polls GET /api/fleet/status every ~2 s and REPLACES its whole fleet array
// with the response. If one poll's vehicle object carries a null numeric field (e.g. a
// battery that momentarily reads absent), rendering it directly shows "—" and the value
// flickers valid → "—" → valid at the poll rate. The backend now carries battery forward via
// last-known (the real fix), but this frontend layer is defence-in-depth so ANY absent
// numeric field in a partial update can never erase a previously valid displayed value — and
// it is per-USV, so one vehicle's telemetry never bleeds into another's.
//
// Rules (last-known-value semantics):
//   • a valid (finite number) incoming value updates the cache and is shown;
//   • an absent value (null/undefined/non-number) keeps the last-known value;
//   • only an EXPLICIT unavailable/reset signal clears it (never a bare null);
//   • freshness/staleness is NOT invented here — the merged vehicle keeps its own comm_state
//     and last_seen_age_s, so the page's existing stale styling still marks a retained value
//     as stale during degradation and never presents it as fresh.

// Numeric telemetry fields that get last-known semantics. Position (lat/lng) is deliberately
// EXCLUDED — a retained position must never be plotted as a current marker; the backend owns
// position last-known and the map hides an absent fix rather than fabricating one.
export const MERGED_FIELDS = ["battery", "speed", "heading"];

function isValidNumber(x) { return typeof x === "number" && Number.isFinite(x); }

// An EXPLICIT "this sensor is unavailable / value was reset" signal for a field, distinct from
// merely-absent. A bare null/undefined is ABSENT (keep last-known) and never clears. A vehicle
// may signal a real clear with `<field>_available === false`.
function explicitlyCleared(vehicle, field) {
  return !!vehicle && vehicle[field + "_available"] === false;
}

export function createTelemetryCache(fields = MERGED_FIELDS) {
  const cache = new Map(); // id -> { <field>: lastKnownValidValue } (per USV — never shared)

  function merge(vehicle) {
    if (!vehicle || vehicle.id == null) return vehicle;
    const id = vehicle.id;
    const prev = cache.get(id) || {};
    const next = { ...prev };
    const out = { ...vehicle };
    for (const f of fields) {
      const incoming = vehicle[f];
      if (explicitlyCleared(vehicle, f)) {
        next[f] = null; out[f] = null;                       // explicit unavailable/reset
      } else if (isValidNumber(incoming)) {
        next[f] = incoming; out[f] = incoming;               // valid → update + show
      } else {
        // absent → keep last-known (null only if we never had a valid value, so a first-ever
        // missing field still renders "—" honestly).
        out[f] = prev[f] == null ? (incoming == null ? null : incoming) : prev[f];
      }
    }
    cache.set(id, next);
    return out;
  }

  return {
    merge,
    /** Merge a whole fleet array, each vehicle against ITS OWN per-USV cache. */
    mergeFleet(list) { return Array.isArray(list) ? list.map(merge) : []; },
    /** Last-known values for a USV (copy), or {} if none. */
    get(id) { return { ...(cache.get(id) || {}) }; },
    /** Explicitly forget a USV's cache (e.g. deprovision / hard reset). */
    reset(id) { cache.delete(id); },
    clear() { cache.clear(); },
  };
}
