// evidence.test.mjs — Scout's STABILIZED EVIDENCE, displayed with no operator-side TTL.
//
// These are the records Scout's own risk model and energy-feasibility gate run on. The station
// shows them so an operator can see the observation behind a verdict; it must never become a
// second opinion about them.
//
// THE RULES BEING PINNED:
//
//   1. THE STATE IS SCOUT'S. FRESH / AGING / STALE / NEVER_OBSERVED are displayed as sent. No
//      threshold here converts an age into a state — a local rule would disagree with the very
//      evidence behind Scout's refusals, and the operator would be shown the disagreement as if
//      it were the vehicle's condition.
//   2. THE AGE IS SCOUT'S. `age_s` is measured from the VEHICLE'S observation, not from our
//      fetch. Polling does not create freshness.
//   3. ABSENCE IS NEVER FRESH. A signal Scout did not report is UNKNOWN, and UNKNOWN is kept
//      distinct from NEVER_OBSERVED: "Scout said nothing" and "Scout says the vehicle has never
//      seen it" are facts about different components.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeEvidence, worstEvidenceState, evidenceValueText,
  EVIDENCE_SIGNALS, EVIDENCE_STATES, EVIDENCE_TONE,
} from "../operator/lib/evidence.js";

const here = dirname(fileURLToPath(import.meta.url));
const evSrc = readFileSync(join(here, "../operator/lib/evidence.js"), "utf8");
const agentSrc = readFileSync(join(here, "../operator/pages/Agent.js"), "utf8");

// Scout's live evidence block, captured verbatim from the running vehicle.
const LIVE = {
  armed: { age_s: 0.549, observed_at: 1786301934.337, source: "HEARTBEAT", state: "FRESH",
    value: false },
  battery: { age_s: 0.087, observed_at: 1786301934.798, source: "SYS_STATUS", state: "FRESH",
    value: 89 },
  ekf: { age_s: 0.087, observed_at: 1786301934.798, source: "EKF_STATUS_REPORT", state: "FRESH",
    value: true },
  gps: {
    fix_type: { age_s: 0.087, observed_at: 1786301934.798, source: "GPS_RAW_INT",
      state: "FRESH", value: 3 },
    satellites: { age_s: 0.087, observed_at: 1786301934.798, source: "GPS_RAW_INT",
      state: "FRESH", value: 23 },
  },
  heartbeat: { age_s: 0.549, observed_at: 1786301934.337, source: "HEARTBEAT", state: "FRESH",
    value: true },
  mode: { age_s: 0.549, observed_at: 1786301934.337, source: "HEARTBEAT", state: "FRESH",
    value: 0 },
  position: { age_s: 0.113, observed_at: 1786301934.772, source: "GLOBAL_POSITION_INT",
    state: "FRESH", value: { lat: 56.6635204, lng: 12.8814768 } },
};
const res = (evidence, over = {}) => ({
  ok: true, vehicle_id: "usv-2", available: true, reachable: true, source: "scout",
  evidence, supported: evidence !== null, freshness: { battery_s: 0.12 },
  state_timestamp: 1786301934.89, ...over,
});
const sig = (view, key) => view.signals.find((s) => s.key === key);

// ── P. the ordinary case ───────────────────────────────────────────────────────────────────
test("P. a FRESH GPS fix displays FRESH, with Scout's age, source and value", () => {
  const view = normalizeEvidence(res(LIVE));
  assert.equal(view.present, true);
  assert.equal(view.supported, true);

  const gps = sig(view, "gps_fix");
  assert.equal(gps.state, "FRESH");
  assert.equal(gps.known, true);
  assert.equal(gps.tone, "ok");
  assert.equal(gps.ageS, 0.087);              // Scout's own number, not recomputed
  assert.equal(gps.source, "GPS_RAW_INT");
  assert.equal(gps.valueText, "3");
  assert.equal(sig(view, "gps_satellites").valueText, "23");
});

// ── O. a stale reading ─────────────────────────────────────────────────────────────────────
test("O. a STALE battery displays STALE — and is never softened by a young age", () => {
  // Scout's verdict and Scout's age arrive together, and the verdict wins even when the number
  // beside it looks harmless. The station does not second-guess a STALE with an age comparison.
  const view = normalizeEvidence(res({
    ...LIVE,
    battery: { ...LIVE.battery, state: "STALE", age_s: 0.4 },
  }));
  const b = sig(view, "battery");
  assert.equal(b.state, "STALE");
  assert.equal(b.tone, "warn");
  assert.equal(b.ageS, 0.4);
  assert.equal(worstEvidenceState(view), "STALE");
});

test("AGING is its own state and is not rounded to FRESH or to STALE", () => {
  const view = normalizeEvidence(res({
    ...LIVE, heartbeat: { ...LIVE.heartbeat, state: "AGING", age_s: 2.6 },
  }));
  assert.equal(sig(view, "heartbeat").state, "AGING");
  assert.equal(sig(view, "heartbeat").tone, "caution");
  assert.equal(worstEvidenceState(view), "AGING");
});

// ── Q. absence, in each of its distinct forms ──────────────────────────────────────────────
test("Q. a signal Scout omitted reads UNKNOWN with no age — never FRESH", () => {
  const { battery, ...rest } = LIVE;
  const view = normalizeEvidence(res(rest));
  const b = sig(view, "battery");
  assert.equal(b.reported, false);
  assert.equal(b.state, "UNKNOWN");
  assert.notEqual(b.state, "FRESH");
  assert.equal(b.ageS, null);
  assert.equal(b.valueText, null);
  assert.equal(b.tone, "idle");               // a gap is not a fault
});

test("Q2. NEVER_OBSERVED is Scout's statement and stays distinct from our UNKNOWN", () => {
  const view = normalizeEvidence(res({
    ...LIVE,
    position: { state: "NEVER_OBSERVED", value: null, source: null, observed_at: null,
      age_s: null },
  }));
  const p = sig(view, "position");
  assert.equal(p.reported, true);             // Scout DID report — it reported never having seen it
  assert.equal(p.state, "NEVER_OBSERVED");
  assert.equal(p.known, true);
  assert.notEqual(p.state, "UNKNOWN");
  assert.equal(EVIDENCE_STATES.includes("NEVER_OBSERVED"), true);
  assert.equal(EVIDENCE_STATES.includes("UNKNOWN"), false);
});

test("Q3. an unreachable Scout, an unsupported Scout and a missing block stay distinguishable", () => {
  const unreachable = normalizeEvidence(res(null, { reachable: false, supported: false }));
  assert.equal(unreachable.reachable, false);
  assert.equal(unreachable.present, false);

  const unconfigured = normalizeEvidence(res(null, { available: false, reachable: false }));
  assert.equal(unconfigured.available, false);

  const older = normalizeEvidence(res(null, { supported: false }));
  assert.equal(older.reachable, true);
  assert.equal(older.supported, false);

  // In every one of them, every signal is UNKNOWN. None is FRESH.
  for (const view of [unreachable, unconfigured, older, normalizeEvidence(null)]) {
    assert.equal(view.signals.length, EVIDENCE_SIGNALS.length);
    for (const s of view.signals) {
      assert.equal(s.state, "UNKNOWN", s.key);
      assert.equal(s.ageS, null, s.key);
    }
  }
});

test("an unrecognised state is shown as sent, with a neutral tone", () => {
  const view = normalizeEvidence(res({ ...LIVE, ekf: { ...LIVE.ekf, state: "DEGRADED" } }));
  assert.equal(sig(view, "ekf").state, "DEGRADED");
  assert.equal(sig(view, "ekf").known, false);
  assert.equal(sig(view, "ekf").tone, "idle");
});

// ── values ─────────────────────────────────────────────────────────────────────────────────
test("values render honestly — a position is a coordinate pair, and false is not absent", () => {
  const view = normalizeEvidence(res(LIVE));
  assert.equal(sig(view, "position").valueText, "56.663520, 12.881477");
  // `armed: false` is an OBSERVATION. Collapsing it into "—" would report a gap where Scout
  // reported a fact.
  assert.equal(sig(view, "armed").valueText, "false");
  assert.equal(sig(view, "ekf").valueText, "true");
  assert.equal(sig(view, "mode").valueText, "0");
  assert.equal(evidenceValueText(null), null);
  assert.doesNotMatch(String(view.signals.map((s) => s.valueText).join(" ")), /\[object Object\]/);
});

test("the signal list and its order are fixed, so the table cannot reshuffle between polls", () => {
  const view = normalizeEvidence(res(LIVE));
  assert.deepEqual(view.signals.map((s) => s.key),
    ["battery", "gps_fix", "gps_satellites", "ekf", "heartbeat", "mode", "armed", "position"]);
});

test("worstEvidenceState ranks an actively-stale observation above an absent one", () => {
  // A gap must not outrank an observation Scout has marked as too old: the STALE reading is the
  // one that tells the operator something is wrong now.
  const view = normalizeEvidence(res({
    battery: { ...LIVE.battery, state: "STALE" },
    position: { ...LIVE.position, state: "NEVER_OBSERVED" },
  }));
  assert.equal(worstEvidenceState(view), "STALE");
  assert.equal(EVIDENCE_TONE.NEVER_OBSERVED, "idle");
  assert.equal(EVIDENCE_TONE.UNKNOWN, "idle");
});

// ── no local freshness policy, anywhere ────────────────────────────────────────────────────
test("the evidence layer contains no TTL, no clock and no age comparison", () => {
  // Comments are stripped first: this module's own doc block says the words "TTL" and "stale"
  // precisely to forbid them, and the guard is about the CODE.
  const code = evSrc.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
  // If any of these appear, the station has started deciding what "fresh" means.
  assert.doesNotMatch(code, /Date\.now|new Date/);
  assert.doesNotMatch(code, /age\w*\s*[<>]=?\s*\d/i);
  assert.doesNotMatch(code, /TTL|MAX_AGE|STALE_AFTER/i);
  // The state is read, never assigned from a comparison.
  assert.match(code, /str\(rec\.state\)/);
});

test("the Agent page shows Scout's state and age and computes neither", () => {
  const i = agentSrc.indexOf("function evidenceCard");
  assert.ok(i > 0, "the Agent page must carry the evidence card");
  const block = agentSrc.slice(i, agentSrc.indexOf("\n  }", i));
  assert.match(block, /ev\.normalizeEvidence/);
  assert.match(block, /s\.ageS/);
  assert.match(block, /s\.state/);
  // No local ageing: no clock read and no threshold in the card.
  assert.doesNotMatch(block, /Date\.now/);
  assert.doesNotMatch(block, /ageS\s*[<>]=?\s*\d+\s*\)/);
  // Unreachable and unsupported are shown as themselves, not as an empty-but-fine table.
  assert.match(block, /!view\.reachable/);
  assert.match(block, /!view\.supported/);
});
