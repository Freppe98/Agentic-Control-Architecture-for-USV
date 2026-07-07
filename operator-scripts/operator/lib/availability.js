// availability.js — first-class "why is this missing?" states. The operator must be
// able to tell whether a value is fresh, stale-due-to-comms, broken, absent hardware,
// or simply not wired in the backend yet. Do NOT collapse these into one NO-TELEM.
// See DATA_DICTIONARY.md (Data Availability States).
//
// Independence rule: LAST_KNOWN is a COMMS condition — it never implies a health fault.
// Only FAULT gets a ✕. N/A = not installed. BACKEND_GAP = dev limitation, not a fault.

export const AVAIL = {
  LIVE: "live",
  LAST_KNOWN: "last_known",
  FAULT: "fault",       // expected but broken/offline (UNAVAILABLE)
  NA: "na",             // not installed / not applicable
  GAP: "backend_gap",   // reserved slot; backend/schema doesn't expose it yet
};

// Operator-facing tag text. NOTE: BACKEND_GAP is a DEVELOPMENT-ONLY bucket — the
// operator must never see "BACKEND_GAP"/"NO BACKEND" or dev-speak. It renders as a
// neutral "Unavailable" (callers pass a more specific operator reason such as
// "No data received" / "Not installed" / "Unsupported by this vehicle"); the dev
// reason belongs in BACKEND_ROADMAP.md, not on screen (optionally in a title tooltip).
const META = {
  live:        { cls: "live",       tag: "LIVE" },
  last_known:  { cls: "last-known", tag: "LAST KNOWN" },
  fault:       { cls: "fault",      tag: "No data" },     // expected but broken/offline
  na:          { cls: "na",         tag: "N/A" },
  backend_gap: { cls: "gap",        tag: "Unavailable" }, // operator-facing, never "NO BACKEND"
};

/**
 * Small inline chip naming the state (operator-facing). `label` overrides the tag
 * text; `dev` is an optional developer-only note surfaced as a title tooltip.
 */
export function availTag(state, label, dev) {
  const m = META[state] || META.backend_gap;
  const title = dev ? ` title="${String(dev).replace(/"/g, "&quot;")}"` : "";
  return `<span class="av av-${m.cls}"${title}>${label || m.tag}</span>`;
}

/**
 * Render a value OR the appropriate missing-state slot, wrapped in `.av-slot`.
 * opts: { value, label, age, dev } — value shown for LIVE/LAST_KNOWN; label overrides
 * the tag text (use an operator-facing reason for GAP); age (s) annotates LAST_KNOWN;
 * dev is an optional developer tooltip (never primary operator text).
 */
export function availSlot(state, { value = null, label = null, age = null, dev = null } = {}) {
  const wrap = (inner) => `<span class="av-slot">${inner}</span>`;
  switch (state) {
    case AVAIL.LIVE:
      return wrap(`<span class="av-val">${value == null ? "—" : value}</span>`);
    case AVAIL.LAST_KNOWN:
      return wrap(
        `<span class="av-val av-stale">${value == null ? "—" : value}</span>` +
        availTag(AVAIL.LAST_KNOWN, age != null ? `LAST KNOWN · ${Math.round(age)}s` : label, dev)
      );
    case AVAIL.FAULT:
      return wrap(`<span class="av-val av-faultmark">✕</span>` + availTag(AVAIL.FAULT, label, dev));
    case AVAIL.NA:
      return wrap(`<span class="av-val av-dim">—</span>` + availTag(AVAIL.NA, label, dev));
    case AVAIL.GAP:
    default:
      return wrap(`<span class="av-val av-dim">—</span>` + availTag(AVAIL.GAP, label, dev));
  }
}
