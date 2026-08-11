// agent-risk.test.mjs — Scout's CONTINUOUS RISK MODEL, as the operator station displays it.
//
// Scout's instantaneous risk model is frozen and authoritative. It computes:
//
//     stabilized evidence
//         → hard mission/RTL feasibility  +  continuous component risk
//         → weighted continuous score
//         + non-compensatory component severity floors
//         + hard feasibility override
//         → GOVERNING risk level  →  advisory recommendation
//
// The station's whole job is to show that result faithfully. These tests pin the four ways it
// could stop doing so, each of which is a lie an operator would act on:
//
//   1. DISPLAYING THE SCORE. `risk.score` / `risk.weighted_level` are PRE-FLOOR inputs. Scout's
//      floors are non-compensatory by design — one severe component governs regardless of how
//      reassuring the weighted average is — so a station that mapped the score to a level would
//      report LOW for a vehicle its own agent has assessed as HIGH. `risk.level` is the ONLY
//      authoritative field, and the regression at the centre of this file is Scout's own worked
//      example of the two disagreeing.
//   2. COMPUTING A LEVEL. There is no operator-side risk model and there must not be one. A risk
//      level is a claim about the vehicle's situation, and only the agent holding that situation
//      can make it.
//   3. FAILING OPEN. An absent risk block reads "—". Never LOW. "Nothing looked wrong" and "no
//      component of this system has assessed risk" are different statements, and only the second
//      one is true when Scout says nothing.
//   4. ACTING ON RISK. The recommendation is advisory TEXT for a human. It is not a button, it
//      gates nothing, and no command is ever generated from it. Readiness stays separate: Start
//      comes from Scout's own start_eligible / start_block_reason, so a HIGH risk does not
//      disable a control Scout has not itself refused, and a LOW risk does not enable one.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeStatus, riskView, recommendationView, riskComponents, missionCardView, startGate,
  RISK_LEVELS, RECOMMENDATION_TEXT,
} from "../operator/lib/mission-execution.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const mapSrc = read("../operator/pages/Map.js");
const agentSrc = read("../operator/pages/Agent.js");

const envelope = (over = {}) => ({
  ok: true, supported: true, reachable: true,
  scout: {
    supported: true,
    state: "READY", effective_state: "READY", active_operation_id: null,
    mission_id: "msn-329c2faff137", mode: "LOITER",
    sequence: { current: 0, count: 15 },
    replanning: { active: false, fsm_state: "MONITORING" },
    authority_status: "LOCAL_AGENT",
    can_start: true, can_pause: false, can_resume: false,
    mission_execution_enabled: true, last_error: null,
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));

// Scout's live LOW shape, captured verbatim from the running vehicle.
const RISK_LOW = {
  score: 0.1273,
  level: "LOW",
  weighted_score: 0.1273,
  weighted_level: "LOW",
  component_floor_level: null,
  component_floor_reason: null,
  component_floor_source: null,
  hard_constraint_violated: false,
  hard_override_level: null,
  confidence: "HIGH",
  recommendation: "CONTINUE",
  feasibility_status: "FEASIBLE",
  dominant_component: "energy",
  dominant_reason: "ENERGY_MARGIN_TIGHTENING",
  evaluated_at: 1786301906.107,
  weights: { energy: 0.3, communication: 0.25, navigation: 0.25, health: 0.1, mission: 0.1 },
  components: {
    energy: { name: "energy", score: 0.4243, weight: 0.3, weighted_score: 0.1273,
      reason: "ENERGY_MARGIN_TIGHTENING",
      evidence: { mission_feasible: true, worst_margin_percent: 17.27 } },
    communication: { name: "communication", score: 0.0, weight: 0.25, weighted_score: 0.0,
      reason: "COMMUNICATION_CONNECTED", evidence: { communication_state: "CONNECTED" } },
    navigation: { name: "navigation", score: 0.0, weight: 0.25, weighted_score: 0.0,
      reason: "NAVIGATION_NOMINAL", evidence: null },
    health: { name: "health", score: 0.0, weight: 0.1, weighted_score: 0.0,
      reason: "HEALTH_NOMINAL", evidence: null },
    mission: { name: "mission", score: 0.0, weight: 0.1, weighted_score: 0.0,
      reason: "MISSION_NOMINAL", evidence: null },
  },
};

// ── A. the ordinary case ───────────────────────────────────────────────────────────────────
test("A. a governing LOW displays LOW", () => {
  const v = riskView(S({ risk: RISK_LOW }));
  assert.equal(v.reported, true);
  assert.equal(v.level, "LOW");
  assert.equal(v.text, "LOW");
  assert.equal(v.known, true);
  assert.equal(v.tone, "ok");
  // The pre-floor inputs are carried for the explanation, not substituted for the verdict.
  assert.equal(v.weightedLevel, "LOW");
  assert.equal(v.weightedScore, 0.1273);
  assert.equal(v.floorActive, false);
  assert.equal(v.governedBy, "weighted");
  assert.equal(v.confidence, "HIGH");
  assert.equal(v.dominantComponent, "energy");
  assert.equal(v.dominantReason, "ENERGY_MARGIN_TIGHTENING");
});

// ── B. THE REGRESSION ──────────────────────────────────────────────────────────────────────
// Scout's own worked example. A reassuring weighted score, a severe single component, and a
// governing level that is neither of the numbers on the left. If this test ever fails, the card
// is telling an operator with a disconnected link and no proven autonomous continuation that
// their situation is LOW risk.
test("B. weighted LOW under a HIGH component floor displays HIGH — never LOW", () => {
  const risk = {
    ...RISK_LOW,
    score: 0.2375,
    weighted_score: 0.2375,
    weighted_level: "LOW",
    component_floor_level: "HIGH",
    component_floor_reason: "COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION",
    component_floor_source: "communication",
    level: "HIGH",
    recommendation: "HOLD_RECOMMENDED",
  };
  const v = riskView(S({ risk }));

  assert.equal(v.level, "HIGH");
  assert.equal(v.text, "HIGH");
  assert.notEqual(v.text, "LOW");
  assert.equal(v.tone, "warn");

  // The card renders the governing level, not the score's level.
  const card = missionCardView(S({ risk }), {});
  assert.equal(card.risk.text, "HIGH");
  assert.notEqual(card.risk.text, "LOW");

  // …and the pre-floor inputs survive intact so the Agent page can EXPLAIN the difference.
  assert.equal(v.weightedLevel, "LOW");
  assert.equal(v.weightedScore, 0.2375);
  assert.equal(v.floorLevel, "HIGH");
  assert.equal(v.floorReason, "COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION");
  assert.equal(v.floorSource, "communication");
  assert.equal(v.floorActive, true);
  assert.equal(v.governedBy, "floor");
  // The tooltip names the floor rather than burying it.
  assert.match(v.detail, /severity floor HIGH/);
  assert.match(v.detail, /COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION/);
});

test("B2. a hard feasibility violation governs above both the floor and the weighted score", () => {
  const v = riskView(S({
    risk: {
      ...RISK_LOW,
      weighted_score: 0.31, weighted_level: "ELEVATED",
      component_floor_level: "HIGH", component_floor_reason: "ENERGY_MARGIN_CRITICAL",
      component_floor_source: "energy",
      hard_constraint_violated: true, hard_override_level: "CRITICAL",
      level: "CRITICAL", recommendation: "RETURN_RECOMMENDED",
    },
  }));
  assert.equal(v.level, "CRITICAL");
  assert.equal(v.governedBy, "hard");
  assert.equal(v.hardConstraintViolated, true);
  assert.equal(v.hardOverrideLevel, "CRITICAL");
  assert.match(v.detail, /hard constraint violated → CRITICAL/);
});

// ── C. the top of the scale ────────────────────────────────────────────────────────────────
test("C. a governing CRITICAL displays CRITICAL", () => {
  const v = riskView(S({ risk: { ...RISK_LOW, level: "CRITICAL", score: 0.91 } }));
  assert.equal(v.text, "CRITICAL");
  assert.equal(v.tone, "warn");
  assert.equal(missionCardView(S({ risk: { ...RISK_LOW, level: "CRITICAL" } }), {}).risk.text,
    "CRITICAL");
});

// ── D. absence ─────────────────────────────────────────────────────────────────────────────
test("D. an absent risk block reads '—', never LOW", () => {
  const v = riskView(S({}));
  assert.equal(v.reported, false);
  assert.equal(v.level, null);
  assert.equal(v.text, "—");
  assert.notEqual(v.text, "LOW");
  assert.equal(v.tone, "idle");
  assert.equal(missionCardView(S({}), {}).risk.text, "—");
});

test("D2. a risk block with no level claims nothing, and a score alone never becomes a level", () => {
  // Scout sent numbers but no verdict. The numbers are NOT mapped to a level here — a score of
  // 0.05 is as unreadable to this station as a score of 0.95.
  for (const score of [0.05, 0.95]) {
    const v = riskView(S({ risk: { score, weighted_score: score, weighted_level: "LOW" } }));
    assert.equal(v.level, null);
    assert.equal(v.text, "—");
    assert.equal(v.reported, true);
    assert.match(v.detail, /no governing level/);
  }
});

test("D3. an unrecognised level is shown exactly as Scout sent it, never bucketed", () => {
  const v = riskView(S({ risk: { ...RISK_LOW, level: "SEVERE" } }));
  assert.equal(v.text, "SEVERE");
  assert.equal(v.known, false);
  assert.equal(v.tone, "idle");
  assert.equal(RISK_LEVELS.includes("SEVERE"), false);
});

// ── E. the advisory recommendation ─────────────────────────────────────────────────────────
test("E. RETURN_RECOMMENDED displays RETURN, and generates no command", () => {
  const st = S({ risk: { ...RISK_LOW, level: "HIGH", recommendation: "RETURN_RECOMMENDED" } });
  const r = recommendationView(st);
  assert.equal(r.reported, true);
  assert.equal(r.code, "RETURN_RECOMMENDED");
  assert.equal(r.text, "RETURN");
  assert.equal(r.known, true);
  assert.equal(missionCardView(st, {}).recommendation.text, "RETURN");

  // The card's BUTTONS are the lifecycle's own and are untouched by the recommendation: no RTL,
  // no return, no stop is offered because Scout advised one. Advice is for the human.
  const buttons = missionCardView(st, {}).buttons.map((b) => b.action);
  for (const forbidden of ["rtl", "return", "return_home"]) {
    assert.equal(buttons.includes(forbidden), false, forbidden);
  }
});

test("E2. every recommendation maps to its one compact word", () => {
  assert.deepEqual(RECOMMENDATION_TEXT, {
    CONTINUE: "CONTINUE",
    CONTINUE_WITH_CAUTION: "CAUTION",
    HOLD_RECOMMENDED: "HOLD",
    RETURN_RECOMMENDED: "RETURN",
    // Both spellings Scout has shipped for the same two advisories. RETURN_HOME is the ADVICE
    // "bring the vehicle home" — it is not RTL and it is not the constrained safe-return route.
    HOLD: "HOLD",
    RETURN_HOME: "RETURN HOME",
  });
  for (const [code, word] of Object.entries(RECOMMENDATION_TEXT)) {
    assert.equal(recommendationView(S({ risk: { ...RISK_LOW, recommendation: code } })).text, word);
  }
  // An unknown recommendation is reported as Scout sent it, not silently dropped or mapped.
  const odd = recommendationView(S({ risk: { ...RISK_LOW, recommendation: "DIVERT" } }));
  assert.equal(odd.text, "DIVERT");
  assert.equal(odd.known, false);
  // Absent → "—", never a reassuring CONTINUE.
  const none = recommendationView(S({ risk: { ...RISK_LOW, recommendation: null } }));
  assert.equal(none.reported, false);
  assert.equal(none.text, "—");
  assert.notEqual(none.text, "CONTINUE");
});

// ── the component breakdown ────────────────────────────────────────────────────────────────
test("the component breakdown reads Scout's numbers and multiplies nothing out", () => {
  const comps = riskComponents(S({ risk: RISK_LOW }));
  assert.deepEqual(comps.map((c) => c.name),
    ["energy", "communication", "navigation", "health", "mission"]);
  const energy = comps[0];
  assert.equal(energy.score, 0.4243);
  assert.equal(energy.weight, 0.3);
  // Scout's OWN weighted_score, not score × weight computed here.
  assert.equal(energy.weightedContribution, 0.1273);
  assert.equal(energy.reason, "ENERGY_MARGIN_TIGHTENING");
  assert.deepEqual(energy.evidence, { mission_feasible: true, worst_margin_percent: 17.27 });
});

test("a component Scout omits weighted_score for reads '—' rather than a computed product", () => {
  const comps = riskComponents(S({
    risk: { ...RISK_LOW, components: { energy: { score: 0.5, weight: 0.3 } } },
  }));
  assert.equal(comps[0].score, 0.5);
  assert.equal(comps[0].weight, 0.3);
  assert.equal(comps[0].weightedContribution, null);   // NOT 0.15
});

test("a component Scout adds later is appended, never dropped", () => {
  const comps = riskComponents(S({
    risk: { ...RISK_LOW,
      components: { ...RISK_LOW.components, weather: { score: 0.2, weight: 0.05 } } },
  }));
  assert.equal(comps.at(-1).name, "weather");
  assert.equal(comps.length, 6);
});

test("no risk path can render [object Object], however nested Scout's evidence is", () => {
  const st = S({
    risk: { ...RISK_LOW, level: "HIGH",
      reason: { code: "ENERGY", message: "margin thin" },
      component_floor_reason: { code: "ENERGY_TIGHT", message: "5% < margin < 15%" },
      components: { navigation: { score: 0.4, weight: 0.25, weighted_score: 0.1,
        reason: "GPS_DEGRADED",
        evidence: { gps_fix_type: { value: 2, state: "AGING", age_s: 3.2 } } } } },
  });
  const v = riskView(st);
  const rendered = [v.text, v.detail, v.floorReason, ...riskComponents(st).map((c) => c.reason)]
    .filter(Boolean).join(" ");
  assert.doesNotMatch(rendered, /\[object Object\]/, rendered);
});

test("the tooltip stays a line — the nested components object is NOT dumped into it", () => {
  // On live Scout the components object is kilobytes of nested evidence. A hover is not a page.
  const v = riskView(S({ risk: RISK_LOW }));
  assert.ok(v.detail.length < 400, `risk tooltip is ${v.detail.length} chars: ${v.detail}`);
  assert.doesNotMatch(v.detail, /weighted_score|evidence/);
});

// ── READINESS IS NOT RISK ──────────────────────────────────────────────────────────────────
test("a HIGH governing risk does not disable Start, and a LOW one does not enable it", () => {
  // HIGH risk, Scout says the mission is startable → Start stays available. Scout is the only
  // component that may refuse a Start, and it has not.
  const high = S({ risk: { ...RISK_LOW, level: "HIGH", recommendation: "HOLD_RECOMMENDED" },
    start_eligible: true, authority_blocks_start: false, execution_ready: true });
  const gHigh = startGate(high, { connected: true, busy: false, missionId: "msn-329c2faff137" });
  assert.equal(gHigh.canStart, true, gHigh.reason || "");

  // LOW risk, Scout refuses on a stale route hash → Start stays blocked, for Scout's reason and
  // not a risk-flavoured one.
  const low = S({ risk: RISK_LOW, can_start: false,
    start_eligible: false, start_block_reason: "ROUTE_HASH_STALE" });
  const gLow = startGate(low, { connected: true, busy: false, missionId: "msn-329c2faff137" });
  assert.equal(gLow.canStart, false);
  assert.doesNotMatch(String(gLow.reason || ""), /risk/i);
});

// ── the pages ──────────────────────────────────────────────────────────────────────────────
test("the Map card reads risk.level only and never maps a score to a level", () => {
  const i = mapSrc.indexOf("const live =");
  const block = mapSrc.slice(i, i + 1200);
  assert.match(block, /card\.risk\.text/);
  // No local level vocabulary and no threshold arithmetic anywhere near the compact rows.
  assert.doesNotMatch(block, /"LOW"|'LOW'/);
  assert.doesNotMatch(block, /score\s*[<>]=?/);
  // The advisory word is TEXT in a row, never a button.
  assert.match(block, /class="k">Advice<\/span>/);
  assert.doesNotMatch(block, /<button[\s\S]{0,200}rec\./);
});

test("neither page contains an operator-side risk threshold", () => {
  // The energy floor boundaries (15% / 5% / 0%) belong to Scout. If any of them appear as a
  // comparison in the station's code, a second policy has started growing here.
  for (const [name, src] of [["Map.js", mapSrc], ["Agent.js", agentSrc]]) {
    assert.doesNotMatch(src, /Margin\w*\s*[<>]=?\s*(?:15|5|0)\b/, name);
    assert.doesNotMatch(src, /score\s*[<>]=?\s*0?\.\d/, name);
  }
});

test("the Agent page explains the governing level with Scout's own stages", () => {
  const i = agentSrc.indexOf("function mxRiskRows");
  const block = agentSrc.slice(i, agentSrc.indexOf("\n  }", i));
  for (const label of ["Governing risk level (Scout)", "Recommendation (advisory)", "Confidence",
    "Weighted score", "Weighted level", "Severity floor level", "Severity floor source",
    "Severity floor reason", "Hard constraint violated", "Hard override level",
    "Dominant component", "Dominant reason", "Evaluated"]) {
    assert.ok(block.includes(`"${label}"`), label);
  }
  // Absence is still handled, and the word LOW is never authored into this block.
  assert.match(block, /if \(!view\.reported\)/);
  assert.doesNotMatch(block, /"LOW"/);
  // The component table exists and reads Scout's own contribution.
  const c = agentSrc.indexOf("function mxRiskComponentRows");
  assert.ok(c > 0, "the component breakdown must exist");
  const cblock = agentSrc.slice(c, agentSrc.indexOf("\n  }", c));
  assert.match(cblock, /weightedContribution/);
  assert.doesNotMatch(cblock, /c\.score\s*\*\s*c\.weight/);
  // Trailing zeros are dropped by re-parsing the rounded number, never by trimming characters:
  // /0+$/ against "100.0000" yields "1", which would misreport a weight by two orders of
  // magnitude in the one table whose whole purpose is to show Scout's arithmetic.
  assert.doesNotMatch(cblock, /replace\(\/0\+\$\//);
  assert.match(cblock, /String\(Number\(v\.toFixed/);
});
