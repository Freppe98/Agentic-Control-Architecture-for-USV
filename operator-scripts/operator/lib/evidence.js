// evidence.js — the PURE logic layer for Scout's STABILIZED EVIDENCE.
// No DOM, no fetch, no timers. Unit-tested in tests/evidence.test.mjs.
//
// WHAT THIS IS
// ------------
// Scout stabilizes every raw MAVLink observation into a record it stands behind:
//
//     { value, source, observed_at, age_s, state }
//
// with `state` one of FRESH / AGING / STALE / NEVER_OBSERVED. These are the SAME records
// Scout's continuous risk model and its energy-feasibility gate are computed from, which is
// exactly why this module reads them and computes nothing.
//
// THE ONE RULE: THERE IS NO OPERATOR-SIDE TTL.
// --------------------------------------------
// This station does not decide what "fresh" means, does not age a value against its own clock,
// and does not re-derive a state from `age_s`. Two reasons, and the second is the load-bearing
// one:
//
//   1. Polling does not create freshness. A value re-fetched every 2 seconds is not 2 seconds
//      old — it is as old as the last time the VEHICLE observed it, and only the vehicle knows
//      that.
//   2. A local threshold would disagree with Scout's. When Scout calls a battery reading STALE
//      and refuses to act on it, an operator screen calling the same reading FRESH is not a
//      cosmetic difference — it is the station contradicting the agent about the evidence
//      behind its own refusal.
//
// So `state` is displayed as Scout sent it, `age_s` is displayed as Scout measured it, and a
// signal Scout said nothing about is UNKNOWN. Never FRESH.

// The signals the diagnostics table shows, in reading order. Fixed so the table cannot reshuffle
// between polls. `path` is where the record lives in Scout's evidence block — GPS nests two of
// them under `gps`, and flattening that here keeps the renderer free of Scout's shape.
export const EVIDENCE_SIGNALS = [
  { key: "battery", label: "Battery", path: ["battery"] },
  { key: "gps_fix", label: "GPS fix", path: ["gps", "fix_type"] },
  { key: "gps_satellites", label: "GPS satellites", path: ["gps", "satellites"] },
  { key: "ekf", label: "EKF", path: ["ekf"] },
  { key: "heartbeat", label: "Heartbeat", path: ["heartbeat"] },
  { key: "mode", label: "Mode", path: ["mode"] },
  { key: "armed", label: "Armed", path: ["armed"] },
  { key: "position", label: "Position", path: ["position"] },
];

// Scout's freshness vocabulary, verbatim. UNKNOWN is NOT one of Scout's — it is this station's
// word for "Scout reported no record", and it is deliberately distinct from NEVER_OBSERVED,
// which is Scout stating that the vehicle has never seen the signal. "Scout said nothing" and
// "Scout says it has never happened" are different facts about different components.
export const EVIDENCE_STATES = ["FRESH", "AGING", "STALE", "NEVER_OBSERVED"];

// Existing card tones only (ok / caution / warn / idle). NEVER_OBSERVED and UNKNOWN are IDLE,
// not warn: an absent observation is a gap in the inputs, and colouring every gap as a fault
// teaches the operator to ignore the colour that means a fault.
export const EVIDENCE_TONE = {
  FRESH: "ok", AGING: "caution", STALE: "warn", NEVER_OBSERVED: "idle", UNKNOWN: "idle",
};

const isObj = (v) => v && typeof v === "object" && !Array.isArray(v);
const num = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const str = (v) => {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  return s === "" ? null : s;
};

function at(obj, path) {
  let cur = obj;
  for (const k of path) {
    if (!isObj(cur)) return null;
    cur = cur[k];
  }
  return isObj(cur) ? cur : null;
}

/** A record's value as display text. Objects (position) become their own coordinate pair rather
 *  than "[object Object]"; `false` stays "false" and never collapses into an absent value. */
export function evidenceValueText(value) {
  if (value === null || value === undefined) return null;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : null;
  if (isObj(value)) {
    const lat = num(value.lat ?? value.latitude);
    const lng = num(value.lng ?? value.longitude);
    if (lat !== null && lng !== null) return `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
    const parts = Object.entries(value)
      .map(([k, v]) => { const t = evidenceValueText(v); return t === null ? null : `${k}=${t}`; })
      .filter(Boolean);
    return parts.length ? parts.join(", ") : null;
  }
  return str(value);
}

/**
 * Normalize the backend's evidence envelope into what the diagnostics table renders.
 *
 * `res` is api.getAgentEvidence()'s response ({ ok, available, reachable, supported, evidence,
 * freshness, state_timestamp }) or Scout's evidence dict directly.
 *
 * NOTHING is defaulted. An unreachable Scout, an unconfigured vehicle and a Scout that predates
 * stabilized evidence each stay distinguishable, and every signal Scout did not report comes
 * back with state UNKNOWN and a null age — because the one answer that must never be invented
 * here is a reassuring FRESH.
 *
 * @returns {{ available, reachable, supported, present, stateTimestamp, freshness,
 *             signals: Array<{ key, label, reported, value, valueText, source, observedAt,
 *                              ageS, state, known, tone }> }}
 */
export function normalizeEvidence(res) {
  const envelope = isObj(res) && ("evidence" in res || "reachable" in res || "available" in res);
  const available = envelope ? res.available !== false : isObj(res);
  const reachable = envelope ? res.reachable !== false : isObj(res);
  const block = envelope
    ? (isObj(res.evidence) ? res.evidence : null)
    : (isObj(res) ? res : null);
  // PRESENCE of the block is the support signal. A reachable Scout that answered without one
  // does not implement stabilized evidence; an UNREACHABLE Scout told us nothing about whether
  // it does, so it stays unsupported-unknown rather than being blamed for a version it may have.
  const supported = envelope && res.supported !== undefined
    ? res.supported === true
    : block !== null;

  const signals = EVIDENCE_SIGNALS.map(({ key, label, path }) => {
    const rec = block ? at(block, path) : null;
    if (!rec) {
      return { key, label, reported: false, value: null, valueText: null, source: null,
        observedAt: null, ageS: null, state: "UNKNOWN", known: false, tone: "idle" };
    }
    const state = (str(rec.state) || "UNKNOWN").toUpperCase();
    const known = EVIDENCE_STATES.includes(state);
    return {
      key, label, reported: true,
      value: rec.value ?? null,
      valueText: evidenceValueText(rec.value),
      source: str(rec.source),
      observedAt: rec.observed_at ?? null,
      // Scout's OWN measured age. Never recomputed from observed_at, and never replaced by the
      // age of our poll.
      ageS: num(rec.age_s),
      state, known,
      tone: known ? EVIDENCE_TONE[state] : "idle",
    };
  });

  return {
    available, reachable, supported,
    present: available && reachable && block !== null,
    stateTimestamp: envelope ? (res.state_timestamp ?? null) : null,
    // Scout's older per-subsystem seconds map, carried alongside for the diagnostics page. It is
    // a DIFFERENT, coarser signal and never fills in for a missing evidence record.
    freshness: envelope && isObj(res.freshness) ? res.freshness : null,
    signals,
  };
}

/** The worst state present, for a card condition chip. Ordered by operational severity, with
 *  UNKNOWN / NEVER_OBSERVED deliberately BELOW stale rather than above: an unreported signal is
 *  a gap, and a gap must not outrank an observation Scout has actively marked as too old. */
const SEVERITY = { STALE: 3, AGING: 2, NEVER_OBSERVED: 1, UNKNOWN: 1, FRESH: 0 };
export function worstEvidenceState(view) {
  const sigs = (view && view.signals) || [];
  if (!sigs.length) return null;
  let worst = null;
  for (const s of sigs) {
    if (worst === null || (SEVERITY[s.state] ?? 1) > (SEVERITY[worst] ?? 1)) worst = s.state;
  }
  return worst;
}
