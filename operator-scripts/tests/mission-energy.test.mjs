// mission-energy.test.mjs — the two COMPACT LIVE STATUSES on the Agent Mission card: Scout's
// continuous mission-energy feasibility, and Scout's own agent risk level.
//
// THE THINGS THAT MUST BE PROVABLY TRUE, FOREVER:
//
//   1. Neither value is computed here. The station has no battery model, no range model, no
//      reserve policy and no risk model. It displays Scout's `mission_feasible` /
//      `rtl_return_feasible` verdicts, Scout's margins and Scout's `risk.level`, or it displays
//      nothing. A Scout that reports neither block must not crash the page and must not be
//      given a fabricated verdict.
//   2. MISSION MARGIN and RTL RETURN MARGIN are DIFFERENT QUESTIONS and are never merged into a
//      single "home margin". The compact percentage is the MISSION margin — "can I finish the
//      planned route?" — but a run Scout can complete and NOT return from must never read
//      "FEASIBLE +20%", because Scout's own Start gate refuses on exactly that.
//   3. UNKNOWN is NEUTRAL. A verdict Scout could not reach is a gap in its inputs, not an
//      emergency, and colouring it like one teaches an operator to ignore the colour that does
//      mean an emergency.
//   4. RISK never reads LOW on this station's initiative. Until Scout reports a level it is "—".
//   5. The Start button is unaffected. It comes from the gate (can_start / start_eligible /
//      start_block_reason) and no energy reading may add to, withdraw or override it.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeStatus, energyView, riskView, energyMarginText, energyDetail, missionCardView,
  startGate, shortStartBlocker, errorText,
  ENERGY, RISK_LEVELS, RISK_TONE, START_BLOCK_REASON_TEXT,
} from "../operator/lib/mission-execution.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const mapSrc = read("../operator/pages/Map.js");
const agentSrc = read("../operator/pages/Agent.js");
const themeSrc = read("../operator/styles/theme.css");

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

// Scout's live healthy shape, verbatim from the contract.
const HEALTHY = {
  status: "FEASIBLE",
  reason: "SUFFICIENT_ENERGY",
  battery_percent: 92,
  battery_source: "PHYSICAL",
  physical_battery_percent: 92,
  injected_battery_percent: null,
  current_sequence: 0,
  remaining_waypoint_count: 15,
  planned_home: { lat: 59.1, lon: 17.6 },
  rtl_home: { lat: 59.1, lon: 17.6 },
  planned_completion_distance_m: 1856.2,
  rtl_return_distance_m: 9.8,
  estimated_mission_energy_percent: 61.87,
  estimated_rtl_return_energy_percent: 0.33,
  reserve_margin_percent: 10,
  usable_range_m: 2460.0,
  mission_margin_percent: 20.13,
  rtl_return_margin_percent: 81.67,
  mission_feasible: true,
  rtl_return_feasible: true,
  mission_geometry_source: "CURRENT_POSITION_TO_REMAINING_ROUTE",
  rtl_return_geometry_source: "RTL_STRAIGHT_LINE_ESTIMATE",
  evaluated_at: "2026-08-09T10:22:41Z",
  position_age_s: 0.4,
  max_position_age_s: 5.0,
};
const withEnergy = (over = {}) => S({ energy_feasibility: { ...HEALTHY, ...over } });

// ── 1. FEASIBLE ─────────────────────────────────────────────────────────────────────────
test("both verdicts true reads FEASIBLE with the MISSION margin, rounded", () => {
  const v = energyView(withEnergy());
  assert.equal(v.state, ENERGY.FEASIBLE);
  assert.equal(v.text, "FEASIBLE +20%");
  assert.equal(v.tone, "ok");
  assert.equal(v.marginPercent, 20.13);
});

test("the compact percentage is the MISSION margin and never the RTL return margin", () => {
  // The two differ by 60 points in the live shape. Whichever one the card prints, it must be
  // answering "can I complete the planned mission?" — not "could I abort home right now?".
  const v = energyView(withEnergy());
  assert.match(v.text, /\+20%/);
  assert.doesNotMatch(v.text, /8[12]%/, "the RTL return margin must not be the card figure");
  // …and neither margin is ever LABELLED generically as a "home margin": every operator-visible
  // string names which question it answers. (Source comments may use the phrase to say why it is
  // forbidden — the guard is on what renders, not on what is explained.)
  const labels = [
    ...[...agentSrc.matchAll(/row\("([^"]+)"/g)].map((m) => m[1]),
    ...[...mapSrc.matchAll(/<span class="k">([^<]+)<\/span>/g)].map((m) => m[1]),
    energyDetail(withEnergy().energy),
    energyView(withEnergy()).text, energyView(withEnergy()).reasonText,
  ];
  for (const l of labels) assert.doesNotMatch(String(l), /home margin/i, String(l));
  // Both margins are named as their own question wherever they are shown together.
  assert.ok(labels.includes("Mission margin") && labels.includes("RTL return margin"));
});

test("margins round to whole signed percentages without a decimal tail", () => {
  assert.equal(energyMarginText(20.13), "+20%");
  assert.equal(energyMarginText(-7.2), "-7%");
  assert.equal(energyMarginText(4.4), "+4%");
  assert.equal(energyMarginText(0), "+0%");
  // A rounded -0 must never print as "-0%" — it is the same reading as zero.
  assert.equal(energyMarginText(-0.2), "+0%");
  assert.doesNotMatch(energyMarginText(-0.2), /-0/);
  assert.equal(energyMarginText(null), null);
  assert.equal(energyMarginText("20"), null, "a string is not a reading");
});

// ── 2. MISSION INFEASIBLE ───────────────────────────────────────────────────────────────
test("mission_feasible=false reads INSUFFICIENT with the deficit", () => {
  const v = energyView(withEnergy({
    status: "INFEASIBLE", reason: "INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION",
    mission_margin_percent: -7.2, mission_feasible: false,
  }));
  assert.equal(v.state, ENERGY.INSUFFICIENT);
  assert.equal(v.text, "INSUFFICIENT -7%");
  assert.equal(v.tone, "warn");
});

test("a proven mission deficit outranks an unevaluated RTL return", () => {
  // Scout proved the mission cannot be completed but could not evaluate the return. Showing
  // UNKNOWN there would hide a deficit the operator has to act on.
  const v = energyView(withEnergy({
    status: "INFEASIBLE", mission_feasible: false, mission_margin_percent: -12.6,
    rtl_return_feasible: null, rtl_return_margin_percent: null,
  }));
  assert.equal(v.state, ENERGY.INSUFFICIENT);
  assert.equal(v.text, "INSUFFICIENT -13%");
});

// ── 3. MISSION FEASIBLE / RTL RETURN INFEASIBLE ─────────────────────────────────────────
test("a completable mission Scout cannot return from reads RTL INSUFFICIENT, not FEASIBLE", () => {
  const v = energyView(withEnergy({
    status: "INFEASIBLE", reason: "INSUFFICIENT_ENERGY_FOR_RTL_RETURN",
    mission_feasible: true, mission_margin_percent: 20.13,
    rtl_return_feasible: false, rtl_return_margin_percent: -3.4,
  }));
  assert.equal(v.state, ENERGY.RTL_INSUFFICIENT);
  assert.equal(v.text, "RTL INSUFFICIENT");
  assert.equal(v.tone, "warn");
  // The reassuring reading is specifically the one that must not appear.
  assert.doesNotMatch(v.text, /FEASIBLE \+/);
});

// ── 4. UNKNOWN ──────────────────────────────────────────────────────────────────────────
test("BATTERY_INVALID reads UNKNOWN, neutral — never a red emergency", () => {
  const v = energyView(withEnergy({
    status: "UNKNOWN", reason: "BATTERY_INVALID",
    battery_percent: -1, mission_feasible: null, rtl_return_feasible: null,
    mission_margin_percent: null, rtl_return_margin_percent: null,
  }));
  assert.equal(v.state, ENERGY.UNKNOWN);
  assert.equal(v.text, "UNKNOWN");
  assert.equal(v.tone, "idle", "an unknown is a gap in Scout's inputs, not an emergency");
  assert.equal(v.reasonText, "Battery estimate unavailable");
  // The -1 sentinel is "I do not know", never a battery level.
  assert.equal(withEnergy({ battery_percent: -1 }).energy.batteryPercent, null);
});

test("POSITION_STALE reads CHECKING — Scout is waiting for a usable input", () => {
  const v = energyView(withEnergy({
    status: "UNKNOWN", reason: "POSITION_STALE",
    mission_feasible: null, rtl_return_feasible: null, position_age_s: 31.2,
  }));
  assert.equal(v.state, ENERGY.CHECKING);
  assert.equal(v.text, "CHECKING");
  assert.equal(v.tone, "idle");
});

test("status=UNKNOWN is honoured even when both verdicts happen to read true", () => {
  const v = energyView(withEnergy({ status: "UNKNOWN", reason: "MISSION_UNAVAILABLE" }));
  assert.equal(v.state, ENERGY.UNKNOWN);
  assert.equal(v.tone, "idle");
});

test("an unrecognised reason is shown as Scout sent it, never replaced", () => {
  const v = energyView(withEnergy({
    status: "UNKNOWN", reason: "SOME_NEW_SCOUT_REASON",
    mission_feasible: null, rtl_return_feasible: null,
  }));
  assert.equal(v.reasonText, "SOME_NEW_SCOUT_REASON");
  assert.match(v.detail, /SOME_NEW_SCOUT_REASON/);
});

// ── 5. NO energy_feasibility AT ALL ─────────────────────────────────────────────────────
test("a Scout with no energy_feasibility falls back to a neutral dash and does not crash", () => {
  const v = energyView(S({}));
  assert.equal(v.reported, false);
  assert.equal(v.state, ENERGY.NONE);
  assert.equal(v.text, "—");
  assert.equal(v.tone, "idle");
  assert.equal(v.marginText, null);
  // …and every card path still renders.
  for (const state of ["READY", "NOT_READY", "RUNNING", "PAUSED", "COMPLETED_HOLD", "FAILED"]) {
    const card = missionCardView(S({ state, can_start: true, can_pause: true, can_stop: true }));
    assert.equal(card.energy.text, "—", state);
    assert.equal(card.risk.text, "—", state);
  }
  // An unreachable / unsupported Scout too — the fields exist on every return path.
  for (const st of [normalizeStatus({ reachable: false, scout: {} }),
    normalizeStatus({ supported: false, scout: {} })]) {
    const card = missionCardView(st, {});
    assert.equal(card.energy.text, "—");
    assert.equal(card.risk.text, "—");
  }
});

test("a malformed energy block is not read as a verdict", () => {
  const v = energyView(S({ energy_feasibility: { status: "FEASIBLE" } }));
  // status alone is not two proven verdicts.
  assert.equal(v.state, ENERGY.UNKNOWN);
  assert.equal(v.tone, "idle");
  // Non-numeric margins never become numbers.
  const bad = S({ energy_feasibility: { mission_margin_percent: "20.1", mission_feasible: true,
    rtl_return_feasible: true } });
  assert.equal(bad.energy.missionMarginPercent, null);
  assert.equal(energyView(bad).text, "FEASIBLE");
});

// ── 6-8. RISK ───────────────────────────────────────────────────────────────────────────
test("Scout's risk LOW is displayed as LOW", () => {
  const v = riskView(S({ risk: { level: "LOW", score: 0.27 } }));
  assert.equal(v.reported, true);
  assert.equal(v.level, "LOW");
  assert.equal(v.text, "LOW");
  assert.equal(v.tone, "ok");
  assert.equal(v.score, 0.27);
});

test("ELEVATED / HIGH / CRITICAL / UNKNOWN each carry their own label and tone", () => {
  for (const level of RISK_LEVELS) {
    const v = riskView(S({ risk: { level } }));
    assert.equal(v.text, level);
    assert.equal(v.known, true);
    assert.equal(v.tone, RISK_TONE[level], level);
  }
  // The tones come from the card's EXISTING vocabulary — no new colour is introduced.
  assert.deepEqual([...new Set(Object.values(RISK_TONE))].sort(),
    ["caution", "idle", "ok", "warn"]);
  // …and HIGH / CRITICAL are told apart by their label, since they share the warn colour.
  assert.notEqual(riskView(S({ risk: { level: "HIGH" } })).text,
    riskView(S({ risk: { level: "CRITICAL" } })).text);
});

test("no risk field reads a quiet dash — never LOW", () => {
  const v = riskView(S({}));
  assert.equal(v.reported, false);
  assert.equal(v.level, null);
  assert.equal(v.text, "—");
  assert.equal(v.tone, "idle");
  assert.notEqual(v.text, "LOW");
  assert.match(v.detail, /never\s+computes one/);
});

test("a risk level this build does not know is shown as sent, neutrally", () => {
  const v = riskView(S({ risk: { level: "SEVERE" } }));
  assert.equal(v.text, "SEVERE");
  assert.equal(v.known, false);
  assert.equal(v.tone, "idle");
  assert.match(v.detail, /not recognised/);
});

test("risk_level / risk_score spellings are accepted with no other change", () => {
  const v = riskView(S({ risk: { risk_level: "ELEVATED", risk_score: 0.51 } }));
  assert.equal(v.text, "ELEVATED");
  assert.equal(v.tone, "caution");
  assert.equal(v.score, 0.51);
});

test("the operator station computes no risk anywhere", () => {
  // No arithmetic feeding a risk level: riskView reads Scout's field and nothing else.
  const lib = read("../operator/lib/mission-execution.js");
  const from = lib.indexOf("export function riskView");
  assert.ok(from > 0, "riskView must exist");
  const end = lib.indexOf("\r\n}", from) + 1 || lib.indexOf("\n}", from);
  const body = lib.slice(from, end);
  assert.doesNotMatch(body, /energyView|missionMargin|batteryPercent/,
    "risk must not be derived from energy or battery on this station");
});

// ── 9. THE START BUTTON IS UNAFFECTED ───────────────────────────────────────────────────
test("energy never adds to, withdraws or overrides the Start decision", () => {
  // Scout says the mission cannot be completed AND cannot be returned from, but has NOT set
  // start_eligible=false. The station does not invent the refusal Scout did not make.
  const infeasible = {
    ...HEALTHY, status: "INFEASIBLE", mission_feasible: false, rtl_return_feasible: false,
    mission_margin_percent: -7.2,
  };
  const st = S({ energy_feasibility: infeasible, start_eligible: true,
    execution_ready: true, authority_blocks_start: false });
  const gate = startGate(st, { connected: true });
  assert.equal(gate.canStart, true, "the gate reads Scout's eligibility, not our energy view");
  const card = missionCardView(st, { startBlocked: !gate.canStart, readiness: null });
  assert.equal(card.buttons.find((b) => b.action === "start").enabled, true);
  assert.equal(card.energy.state, ENERGY.INSUFFICIENT, "…while the status still says so plainly");

  // And the converse: a healthy energy reading cannot ENABLE a Start Scout refused.
  const refused = S({ energy_feasibility: HEALTHY, start_eligible: false, can_start: false,
    start_block_reason: "INSUFFICIENT_ENERGY_FOR_RTL_RETURN" });
  assert.equal(startGate(refused, { connected: true }).canStart, false);
});

test("Scout's energy refusal reaches the card as readable text, not a raw code", () => {
  const st = S({ state: "NOT_READY", can_start: false, start_eligible: false,
    start_block_reason: "INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION",
    energy_feasibility: { ...HEALTHY, status: "INFEASIBLE", mission_feasible: false,
      mission_margin_percent: -7.2 } });
  const gate = startGate(st, { connected: true });
  assert.equal(gate.canStart, false);
  const card = missionCardView(st, { startBlocked: true, startBlockedReason: gate.reason });
  assert.equal(card.blocker.text, "Insufficient energy for planned mission");
  assert.ok(card.blocker.text.length <= 44, card.blocker.text);
  // The energy row explains it with the number, so the blocker line does not repeat it.
  assert.equal(card.energy.text, "INSUFFICIENT -7%");
});

test("every new reason code has a short card line and a full sentence", () => {
  const codes = ["INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION", "INSUFFICIENT_ENERGY_FOR_RTL_RETURN",
    "BATTERY_INVALID", "POSITION_STALE", "RTL_HOME_UNAVAILABLE", "MISSION_UNAVAILABLE"];
  for (const code of codes) {
    const short = shortStartBlocker(code);
    assert.equal(short, START_BLOCK_REASON_TEXT[code], code);
    assert.notEqual(short, code, `${code} must not reach the operator as a raw code`);
    assert.ok(short.length <= 44, `${code}: ${short}`);
    // …and the diagnostics page's full sentence exists and is not the bare code either.
    assert.notEqual(errorText(code), code, code);
  }
  // A code embedded in Scout's own sentence is still recognised.
  assert.equal(shortStartBlocker("Scout refused: INSUFFICIENT_ENERGY_FOR_RTL_RETURN (margin -3.4)"),
    "Insufficient energy for RTL return");
  // …and an unrelated reason is untouched by the new mapping.
  assert.equal(shortStartBlocker("Scout reports NOT_READY"), "Scout reports NOT_READY");
});

// ── 10. THE CARD STAYS COMPACT AND RENDERS ──────────────────────────────────────────────
test("the two statuses are ROWS, not a new panel, gauge or graph", () => {
  const i = mapSrc.indexOf("function renderAgentMission");
  const render = mapSrc.slice(i, mapSrc.indexOf("\n  }", i));
  // Rendered through the card's existing row form.
  assert.match(render, /<span class="k">Energy<\/span>/);
  assert.match(render, /<span class="k">Risk<\/span>/);
  assert.match(render, /card\.energy\.text/);
  assert.match(render, /card\.risk\.text/);
  // Both slots carry their evidence in a title tooltip, like every other slot on this card.
  assert.match(render, /card\.energy\.detail/);
  assert.match(render, /card\.risk\.detail/);
  // No gauge, no graph, no battery-injection control, and no internal scroll.
  for (const banned of [/<canvas/, /<svg[^>]*class="[^"]*gauge/, /overflow\s*:\s*auto/,
    /data-mx-inject/, /injected_battery/, /setInject|injectBattery/]) {
    assert.doesNotMatch(render, banned, String(banned));
  }
  // Exactly two new rows — the card does not grow an energy detail dump.
  assert.equal((render.match(/class="k">(Energy|Risk)</g) || []).length, 2);
});

test("the live rows are withheld for a Scout whose status could not be read", () => {
  const i = mapSrc.indexOf("const live =");
  assert.ok(i > 0, "the live status block must exist");
  assert.match(mapSrc.slice(i, i + 120), /card\.present === false \? ""/);
});

test("the row tones reuse the card's existing semantic classes", () => {
  for (const tone of ["ok", "caution", "warn", "idle"]) {
    assert.match(themeSrc, new RegExp(`\\.amx-row \\.v\\.${tone}\\s*\\{`), tone);
  }
  // No new colour variable was invented for these rows.
  const block = themeSrc.slice(themeSrc.indexOf(".amx-row .v.ok"),
    themeSrc.indexOf(".amx-row .v.idle") + 60);
  assert.doesNotMatch(block, /#[0-9a-f]{3,8}\b/i, "reuse the theme variables, do not add hexes");
});

test("the identity rows stay identity-only — Energy and Risk are their own slot", () => {
  // card.rows is Mission / WP and nothing else, so the compactness guards keep meaning what
  // they meant: the live statuses did not smuggle themselves into the identity grid.
  const card = missionCardView(withEnergy({ }), {});
  assert.deepEqual(card.rows.map((r) => r.k), ["Mission", "WP"]);
  assert.equal(card.rows.some((r) => /energy|risk/i.test(r.k)), false);
});

test("a RUNNING mission still shows the live energy status", () => {
  // The running card drops the identity rows in favour of MODE · WP; energy is exactly the
  // reading an operator wants mid-run, so it must survive that branch.
  const card = missionCardView(S({ state: "RUNNING", mode: "AUTO", can_pause: true,
    can_stop: true, sequence: { current: 4, count: 15 },
    energy_feasibility: { ...HEALTHY, mission_margin_percent: 4.4 } }), {});
  assert.equal(card.headline, "AUTO · WP 4 / 15");
  assert.deepEqual(card.rows, []);
  assert.equal(card.energy.text, "FEASIBLE +4%");
});

test("no energy or risk path can render [object Object]", () => {
  const card = missionCardView(S({
    energy_feasibility: { ...HEALTHY, reason: { code: "BATTERY_INVALID", message: "no reading" },
      status: "UNKNOWN", mission_feasible: null, rtl_return_feasible: null },
    risk: { level: "HIGH", reason: { code: "ENERGY", message: "margin thin" },
      components: { energy: 0.6, communication: 0.1 } },
  }), {});
  const rendered = [card.energy.text, card.energy.detail, card.energy.reasonText,
    card.risk.text, card.risk.detail].filter(Boolean).join(" ");
  assert.doesNotMatch(rendered, /\[object Object\]/, rendered);
});

// ── FRESHNESS + THE AGENT DIAGNOSTICS BLOCK ─────────────────────────────────────────────
test("evaluation freshness lives in the tooltip, not as a new card line", () => {
  const d = energyDetail(withEnergy().energy);
  assert.match(d, /position age 0\.4 s of 5 s/);
  assert.match(d, /evaluated 2026-08-09T10:22:41Z/);
  // Both margins are named in full in the evidence — neither is a bare "margin".
  assert.match(d, /mission margin \+20\.1%/);
  assert.match(d, /RTL return margin \+81\.7%/);
  // The card body gains no timestamp line of its own.
  const i = mapSrc.indexOf("const live =");
  assert.doesNotMatch(mapSrc.slice(i, i + 900), /evaluatedAt|position age/);
});

test("the Agent page carries the detailed evidence, both margins kept apart", () => {
  const i = agentSrc.indexOf("function mxEnergyRows");
  assert.ok(i > 0, "the Agent page must carry the energy diagnostics block");
  const block = agentSrc.slice(i, agentSrc.indexOf("\n  }", i));
  for (const label of ["Mission margin", "RTL return margin", "Mission feasible",
    "RTL return feasible", "Planned completion distance", "Direct-return distance",
    "Effective battery", "Reserve margin", "Mission geometry", "RTL geometry"]) {
    assert.ok(block.includes(`"${label}"`), label);
  }
  // The TWO HOMES are named apart and never both called "Home": the mission margin is measured
  // against the planning package's home, the RTL margin against the Pixhawk's verified safety
  // Home, and conflating them would let a healthy RTL margin read as proof about a point the
  // vehicle would never return to.
  assert.ok(block.includes(`"Planned Mission Home"`));
  assert.ok(block.includes(`"Verified RTL Home"`));
  // Only Scout's SOURCE word is printed for each — raw coordinates still are NOT, because the
  // Home card is where a Home is inspected and RTL verification is Scout's `verified` flag,
  // never something a coordinate's presence implies.
  assert.doesNotMatch(block, /latitude|longitude/);
  // The block is withheld entirely for a Scout that reports no energy block.
  assert.match(block, /if \(!e\.reported\)/);
  // …and the risk block never claims a level Scout did not send.
  const r = agentSrc.indexOf("function mxRiskRows");
  const rblock = agentSrc.slice(r, agentSrc.indexOf("\n  }", r));
  assert.match(rblock, /if \(!view\.reported\)/);
  assert.doesNotMatch(rblock, /"LOW"/);
});

test("the diagnostics rows are wired into the existing lifecycle card, not a new panel", () => {
  assert.match(agentSrc, /\$\{mxBatteryRows\(S\)\}\s*\n\s*\$\{mxEnergyRows\(S\)\}\s*\n\s*\$\{mxRiskRows\(S, RS\)\}/);
});
