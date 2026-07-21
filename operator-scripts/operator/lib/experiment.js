// experiment.js — pure logic for the network-impairment Experiment page: safe defaults,
// validation limits, request-payload normalization, and derivation of the CONFIRMED
// experiment state from the backend. No DOM, no fetch — the page wires those; this is the
// unit-tested core (see tests/experiment.test.mjs).
//
// Scope boundary: this manipulates the Operator↔Scout COMMUNICATIONS link (an experiment
// control), NOT Pixhawk command authority. There is deliberately no OPERATOR/LOCAL_AGENT
// gate here — vehicle control authority does not own the network path (see the page note
// and BACKEND_ROADMAP.md). The backend is the only thing that can confirm an impairment is
// live; nothing here is ever optimistically "active".

/** Direction the impairment is applied. Mobile links are often asymmetric, so this is a
 *  first-class parameter. `[apiValue, label]` — api values are what the backend receives. */
export const DIRECTIONS = [
  ["operator_to_scout", "Operator → Scout"],
  ["scout_to_operator", "Scout → Operator"],
  ["both", "Both"],
];
const DIRECTION_VALUES = DIRECTIONS.map(([v]) => v);

/** Frontend safety limits. `bandwidth_kbit_s` is special: null/blank = unlimited. */
export const LIMITS = {
  latency_ms:      { min: 0, max: 10000 },
  jitter_ms:       { min: 0, max: 5000 },
  packet_loss_pct: { min: 0, max: 100 },
  bandwidth_kbit_s:{ min: 1, max: 1000000 },  // positive kbit/s, or null = unlimited
  duration_s:      { min: 1, max: 3600 },
  duplication_pct: { min: 0, max: 100 },
  reordering_pct:  { min: 0, max: 100 },
};

/** Safe defaults — a harmless, inactive profile. Never applied on load. */
export const EXPERIMENT_DEFAULTS = Object.freeze({
  latency_ms: 0,
  jitter_ms: 0,
  packet_loss_pct: 0,
  bandwidth_kbit_s: null,   // unlimited
  duplication_pct: 0,
  reordering_pct: 0,
  full_disconnect: false,
  direction: "both",
  duration_s: 60,
});

/** A fresh, mutable copy of the safe defaults. */
export function defaultForm() { return { ...EXPERIMENT_DEFAULTS }; }

/** Lifecycle states. `unavailable` is UI-only (the experiment API is a backend gap). */
export const STATUS = {
  INACTIVE: "inactive",
  APPLYING: "applying",
  ACTIVE: "active",
  STOPPING: "stopping",
  FAILED: "failed",
  UNAVAILABLE: "unavailable",
};
const STATUS_LABEL = {
  inactive: "Inactive",
  applying: "Applying…",
  active: "Active",
  stopping: "Stopping…",
  failed: "Failed",
  unavailable: "Unavailable",
};

/** Normalize a direction to its api value, or null when unrecognized. */
export function normalizeDirection(v) {
  const s = String(v ?? "").trim();
  if (DIRECTION_VALUES.includes(s)) return s;
  // tolerate the human label too, so a mis-wired control still resolves
  const byLabel = DIRECTIONS.find(([, l]) => l === s);
  return byLabel ? byLabel[0] : null;
}

/** Human label for a direction api value. */
export function directionLabel(v) {
  const d = DIRECTIONS.find(([val]) => val === normalizeDirection(v));
  return d ? d[1] : "—";
}

// Blank bandwidth means "unlimited"; everything else must be a real number.
const isBlank = (v) => v === null || v === undefined || String(v).trim() === "";
const toNum = (v) => (isBlank(v) ? NaN : Number(v));

function rangeError(value, { min, max }, unit = "") {
  const n = Number(value);
  if (isBlank(value) || Number.isNaN(n)) return "Enter a number";
  if (n < 0) return "Cannot be negative";
  if (n < min) return `Minimum ${min}${unit}`;
  if (n > max) return `Maximum ${max}${unit}`;
  return null;
}

/**
 * Validate a form. Returns { valid, errors, requiresConfirmation }.
 *   errors — { field: message } for every field that is out of range.
 *   valid  — no errors on ACTIVE fields (Full Disconnect makes the netem fields inactive,
 *            so a dimmed leftover value never blocks a disconnect experiment).
 *   requiresConfirmation — true when Full Disconnect is on (an explicit confirm is required).
 */
export function validateExperiment(form) {
  const errors = {};
  // netem fields — the impairment mechanism (inactive under Full Disconnect)
  const netemChecks = {
    latency_ms: rangeError(form.latency_ms, LIMITS.latency_ms, " ms"),
    jitter_ms: rangeError(form.jitter_ms, LIMITS.jitter_ms, " ms"),
    packet_loss_pct: rangeError(form.packet_loss_pct, LIMITS.packet_loss_pct, "%"),
    duplication_pct: rangeError(form.duplication_pct, LIMITS.duplication_pct, "%"),
    reordering_pct: rangeError(form.reordering_pct, LIMITS.reordering_pct, "%"),
  };
  // bandwidth: blank = unlimited (valid); otherwise a positive number in range
  if (!isBlank(form.bandwidth_kbit_s)) {
    netemChecks.bandwidth_kbit_s = rangeError(form.bandwidth_kbit_s, LIMITS.bandwidth_kbit_s, " kbit/s");
  }
  // duration + direction always apply, disconnect or not
  const durationErr = rangeError(form.duration_s, LIMITS.duration_s, " s");
  const directionErr = normalizeDirection(form.direction) ? null : "Choose a direction";

  const netemActive = impairmentFieldsActive(form);
  for (const [field, msg] of Object.entries(netemChecks)) {
    if (msg) errors[field] = msg;   // recorded for inline display regardless
  }
  if (durationErr) errors.duration_s = durationErr;
  if (directionErr) errors.direction = directionErr;

  // `valid` only considers errors that would actually take effect
  const blocking = [];
  if (netemActive) blocking.push(...Object.keys(netemChecks).filter((f) => netemChecks[f]));
  if (durationErr) blocking.push("duration_s");
  if (directionErr) blocking.push("direction");

  return {
    valid: blocking.length === 0,
    errors,
    requiresConfirmation: form.full_disconnect === true,
  };
}

/** Convenience: may Apply be enabled? (form is valid) */
export function canApply(form) { return validateExperiment(form).valid; }

/** Full Disconnect requires an explicit confirmation before it is applied. */
export function requiresConfirmation(form) { return form.full_disconnect === true; }

/** Are the netem impairment fields the ACTIVE mechanism? False under Full Disconnect
 *  (which uses a firewall block instead), so the page dims/disables them. */
export function impairmentFieldsActive(form) { return form.full_disconnect !== true; }

/**
 * Normalize a validated form into the request body the experiment API receives.
 * Numbers are coerced; bandwidth blank → null (unlimited); direction → api value.
 * This is the single source of the payload shape (asserted in tests).
 */
export function normalizePayload(form, { vehicleId = null } = {}) {
  const num = (v, fallback = 0) => { const n = toNum(v); return Number.isNaN(n) ? fallback : n; };
  return {
    vehicle_id: vehicleId,
    latency_ms: num(form.latency_ms),
    jitter_ms: num(form.jitter_ms),
    packet_loss_pct: num(form.packet_loss_pct),
    bandwidth_kbit_s: isBlank(form.bandwidth_kbit_s) ? null : num(form.bandwidth_kbit_s),
    duplication_pct: num(form.duplication_pct),
    reordering_pct: num(form.reordering_pct),
    full_disconnect: form.full_disconnect === true,
    direction: normalizeDirection(form.direction) || "both",
    duration_s: num(form.duration_s, LIMITS.duration_s.min),
  };
}

/**
 * Derive the CONFIRMED display state from the backend GET response.
 *   null            → { key: 'unavailable' }  (experiment API not reachable — a backend gap)
 *   { active:true } → { key: 'active', active:true }
 * The active badge is driven by `state.active === true` ONLY — a status string that claims
 * "active" without the confirmed flag is never treated as active (never optimistic).
 */
export function experimentStatus(state) {
  if (state == null) return { key: STATUS.UNAVAILABLE, label: STATUS_LABEL.unavailable, active: false };
  const active = state.active === true;
  let key;
  if (active) key = STATUS.ACTIVE;
  else if (state.status === STATUS.FAILED) key = STATUS.FAILED;
  else if (state.status === STATUS.APPLYING) key = STATUS.APPLYING;
  else if (state.status === STATUS.STOPPING) key = STATUS.STOPPING;
  else key = STATUS.INACTIVE;   // includes a status:"active" that lacks the confirmed flag
  return { key, label: STATUS_LABEL[key], active };
}

/** Format a remaining-seconds value for the live summary. */
export function fmtRemaining(s) { return s == null ? "—" : `${Math.round(s)} s remaining`; }

/**
 * Compact live-summary lines for a confirmed-active experiment (from the backend's
 * `profile` + `direction` + `remaining_s`). Empty array when nothing is confirmed active.
 */
export function activeSummary(state) {
  if (!state || state.active !== true) return [];
  const p = state.profile || {};
  const lines = [directionLabel(state.direction) + " direction" + (normalizeDirection(state.direction) === "both" ? "s" : "")];
  if (p.full_disconnect) {
    lines.push("Full disconnect (link blocked)");
  } else {
    if (p.latency_ms) lines.push(`${p.latency_ms} ms latency`);
    if (p.jitter_ms) lines.push(`${p.jitter_ms} ms jitter`);
    if (p.packet_loss_pct) lines.push(`${p.packet_loss_pct}% loss`);
    if (p.bandwidth_kbit_s) lines.push(`${p.bandwidth_kbit_s} kbit/s`);
    if (p.duplication_pct) lines.push(`${p.duplication_pct}% duplication`);
    if (p.reordering_pct) lines.push(`${p.reordering_pct}% reordering`);
  }
  if (state.remaining_s != null) lines.push(fmtRemaining(state.remaining_s));
  return lines;
}
