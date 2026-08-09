// agent-feasibility.test.mjs — ENERGY FEASIBILITY beside RISK, and the two homes kept apart.
//
// FEASIBILITY AND RISK ARE DIFFERENT QUESTIONS, and this file exists because it is tempting to
// let them bleed into each other:
//
//     feasibility  can the mission / the RTL return be completed with reserve intact?
//     risk         how close are we to conditions we do not want?
//
// Scout answers both, separately, and the station shows both, separately. The case that pins it
// is `ENERGY FEASIBLE +4%` beside `RISK HIGH`: the run is still completable, and the margin has
// tightened enough that Scout's governing level rose. That is not a contradiction to be smoothed
// away — it is the most useful thing the two rows can say together, and a station that relabelled
// the energy row "TIGHT" or "INSUFFICIENT" to match the risk colour would be inventing a verdict
// Scout never issued and hiding the one it did.
//
// Scout's own energy floor boundaries (worst margin ≥15% no floor · 5–15% ELEVATED · 0–5% HIGH ·
// ≤0% hard CRITICAL) are SCOUT'S. They are not reproduced here, not compared against here, and
// their appearance in this station's code would be a second policy quietly forking from the
// first.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeStatus, energyView, riskView, missionCardView, ENERGY,
} from "../operator/lib/mission-execution.js";
import { homeStatus } from "../operator/lib/home.js";

const here = dirname(fileURLToPath(import.meta.url));
const agentSrc = readFileSync(join(here, "../operator/pages/Agent.js"), "utf8");

const envelope = (over = {}) => ({
  ok: true, supported: true, reachable: true,
  scout: {
    supported: true, state: "RUNNING", effective_state: "RUNNING",
    mission_id: "msn-329c2faff137", mode: "AUTO",
    sequence: { current: 4, count: 23 },
    replanning: { active: false, fsm_state: "MONITORING" },
    authority_status: "LOCAL_AGENT", mission_execution_enabled: true,
    can_start: false, can_pause: true, can_resume: false,
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));

// Scout's live feasible shape, captured verbatim from the running vehicle.
const FEASIBLE = {
  status: "FEASIBLE",
  reason: "SUFFICIENT_ENERGY",
  message: "mission margin 17.27%, RTL return margin 78.92% -- both positive at effective "
    + "battery 89% (PHYSICAL).",
  battery_percent: 89, battery_source: "PHYSICAL",
  physical_battery_percent: 89, injected_battery_percent: null,
  current_sequence: 4, remaining_waypoint_count: 14,
  planned_home: { latitude: 56.6635397, longitude: 12.8813428, source: "PLANNING_PACKAGE" },
  rtl_home: { latitude: 56.6635241, longitude: 12.8815107, source: "PIXHAWK_VERIFIED_HOME" },
  planned_completion_distance_m: 1851.8, rtl_return_distance_m: 2.4,
  estimated_mission_energy_percent: 61.73, estimated_rtl_return_energy_percent: 0.08,
  reserve_margin_percent: 10.0, usable_range_m: 3000.0,
  mission_margin_percent: 17.27, rtl_return_margin_percent: 78.92,
  mission_feasible: true, rtl_return_feasible: true,
  mission_geometry_source: "CURRENT_POSITION_TO_REMAINING_ROUTE",
  rtl_return_geometry_source: "RTL_STRAIGHT_LINE_ESTIMATE",
  evaluated_at: 1786301906.106, position_age_s: 0.08, max_position_age_s: 5.0,
};

// ── F. the ordinary healthy reading ────────────────────────────────────────────────────────
test("F. feasible with a +17% mission margin reads FEASIBLE +17%", () => {
  const v = energyView(S({ energy_feasibility: FEASIBLE }));
  assert.equal(v.state, ENERGY.FEASIBLE);
  assert.equal(v.text, "FEASIBLE +17%");
  assert.equal(v.tone, "ok");
  assert.equal(missionCardView(S({ energy_feasibility: FEASIBLE }), {}).energy.text,
    "FEASIBLE +17%");
});

// ── G. THE ONE THAT MATTERS ────────────────────────────────────────────────────────────────
test("G. FEASIBLE +4% coexists with RISK HIGH — the energy row is not relabelled to match", () => {
  const st = S({
    energy_feasibility: { ...FEASIBLE, mission_margin_percent: 4.4,
      message: "mission margin 4.40%, RTL return margin 61.20% -- both positive." },
    risk: {
      score: 0.2812, weighted_score: 0.2812, weighted_level: "LOW",
      component_floor_level: "HIGH", component_floor_reason: "ENERGY_MARGIN_CRITICAL",
      component_floor_source: "energy",
      hard_constraint_violated: false, hard_override_level: null,
      level: "HIGH", recommendation: "HOLD_RECOMMENDED", confidence: "HIGH",
    },
  });
  const card = missionCardView(st, {});

  // The energy row still states Scout's feasibility verdict and Scout's margin.
  assert.equal(card.energy.text, "FEASIBLE +4%");
  assert.equal(card.energy.state, ENERGY.FEASIBLE);
  assert.notEqual(card.energy.text, "INSUFFICIENT");
  assert.doesNotMatch(card.energy.text, /TIGHT/);

  // The risk row states Scout's governing level. Both are true at once, and both are shown.
  assert.equal(card.risk.text, "HIGH");
  assert.equal(card.recommendation.text, "HOLD");

  // And the feasibility verdicts themselves are untouched by the risk level.
  const e = energyView(st);
  assert.equal(e.missionFeasible, true);
  assert.equal(e.rtlReturnFeasible, true);
});

// ── H / I. the two ways it fails, told apart ───────────────────────────────────────────────
test("H. mission_feasible false reads INSUFFICIENT", () => {
  const v = energyView(S({
    energy_feasibility: { ...FEASIBLE, status: "INSUFFICIENT",
      reason: "INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION",
      mission_feasible: false, mission_margin_percent: -6.2 },
  }));
  assert.equal(v.state, ENERGY.INSUFFICIENT);
  assert.match(v.text, /^INSUFFICIENT/);
  assert.equal(v.tone, "warn");
});

test("I. mission completable but the RTL return is not reads RTL INSUFFICIENT", () => {
  const v = energyView(S({
    energy_feasibility: { ...FEASIBLE, status: "INSUFFICIENT",
      reason: "INSUFFICIENT_ENERGY_FOR_RTL_RETURN",
      mission_feasible: true, rtl_return_feasible: false,
      rtl_return_margin_percent: -3.1 },
  }));
  assert.equal(v.state, ENERGY.RTL_INSUFFICIENT);
  assert.equal(v.text, "RTL INSUFFICIENT");
  // NOT a reassuring FEASIBLE: Scout's Start gate refuses on exactly this, and a run that can be
  // completed but not returned from is the case the compact reading would otherwise misstate.
  assert.notEqual(v.text, "FEASIBLE +17%");
});

// ── J. absence ─────────────────────────────────────────────────────────────────────────────
test("J. unknown or absent feasibility never reads FEASIBLE", () => {
  const none = energyView(S({}));
  assert.equal(none.state, ENERGY.NONE);
  assert.equal(none.text, "—");
  assert.notEqual(none.text, "FEASIBLE");
  assert.equal(missionCardView(S({}), {}).energy.text, "—");

  const unknown = energyView(S({
    energy_feasibility: { status: "UNKNOWN", reason: "BATTERY_INVALID",
      mission_feasible: null, rtl_return_feasible: null },
  }));
  assert.equal(unknown.state, ENERGY.UNKNOWN);
  assert.equal(unknown.text, "UNKNOWN");
  assert.equal(unknown.tone, "idle");     // a gap is not an emergency

  // A single missing verdict is enough to withhold FEASIBLE — both must be proven true.
  const half = energyView(S({
    energy_feasibility: { ...FEASIBLE, rtl_return_feasible: null },
  }));
  assert.notEqual(half.state, ENERGY.FEASIBLE);
});

// ── K. battery provenance ──────────────────────────────────────────────────────────────────
test("K. an injected battery is visible as simulated provenance in the diagnostics", () => {
  const st = S({
    energy_feasibility: { ...FEASIBLE, battery_percent: 22, battery_source: "INJECTED",
      physical_battery_percent: 89, injected_battery_percent: 22 },
  });
  const e = st.energy;
  assert.equal(e.batteryPercent, 22);
  assert.equal(e.batterySource, "INJECTED");
  assert.equal(e.physicalBatteryPercent, 89);
  assert.equal(e.injectedBatteryPercent, 22);
  // Physical and effective stay distinguishable — an injected value never overwrites the reading
  // the vehicle actually reported.
  assert.notEqual(e.physicalBatteryPercent, e.injectedBatteryPercent);

  // The page shows all three plus the source, and flags the simulation explicitly.
  const i = agentSrc.indexOf("function mxEnergyRows");
  const block = agentSrc.slice(i, agentSrc.indexOf("\n  }", i));
  for (const label of ["Physical battery", "Injected battery (simulated)", "Effective battery",
    "Battery source"]) {
    assert.ok(block.includes(`"${label}"`), label);
  }
  assert.match(block, /SIMULATED injected value/);
});

test("a -1 battery sentinel stays unknown and never renders as 0%", () => {
  const e = S({ energy_feasibility: { ...FEASIBLE, battery_percent: -1,
    physical_battery_percent: -1 } }).energy;
  assert.equal(e.batteryPercent, null);
  assert.equal(e.physicalBatteryPercent, null);
  assert.notEqual(e.batteryPercent, 0);
});

// ── the split, on the page ─────────────────────────────────────────────────────────────────
test("the Agent page splits MISSION COMPLETION from RTL RETURN", () => {
  const i = agentSrc.indexOf("function mxEnergyRows");
  const block = agentSrc.slice(i, agentSrc.indexOf("\n  }", i));
  assert.match(block, /MISSION COMPLETION/);
  assert.match(block, /RTL RETURN/);
  for (const label of ["Mission feasible", "Mission margin", "Remaining waypoints",
    "Planned completion distance", "Estimated mission energy", "Mission geometry",
    "RTL return feasible", "RTL return margin", "Direct-return distance",
    "Estimated RTL return energy", "RTL geometry", "Reserve margin", "Usable range"]) {
    assert.ok(block.includes(`"${label}"`), label);
  }
  // The page states the distinction rather than leaving the reader to infer it.
  assert.match(block, /Feasibility is also <b>not<\/b> risk/);
});

// ── L / M / N. the two homes, and verification that is never inferred ──────────────────────
const HOME = (over = {}) => ({
  id: 2,
  home: {
    source: "scout", available: true, reachable: true,
    lat: 56.6635241, lng: 12.8815107,
    verified: true, verified_at: "2026-08-09T15:36:59Z",
    verification_method: "set_home_current_position", verification_distance_m: 0.04,
    verification_recovery: {
      state: "RECOVERED", checked_at: 1786285996.23,
      reason: "Restored from persisted proof: fresh HOME_POSITION matched within 0.0m and the "
        + "current Pixhawk boot session matched within 0.0s -- no Set Home required.",
    },
    ready_for_auto: true, ready_for_rtl: true, reason: null, stale: false,
    ...over,
  },
  lat: 56.6635204, lng: 12.8814768,
});

test("L. a verification recovered after restart stays VERIFIED and names its recovery", () => {
  const hs = homeStatus(HOME(), {});
  assert.equal(hs.state, "verified");
  assert.equal(hs.verified, true);
  assert.equal(hs.recoveryState, "RECOVERED");
  assert.equal(hs.recoveredAfterRestart, true);
  assert.match(hs.recoveryReason, /Restored from persisted proof/);
  assert.equal(hs.verificationMethod, "set_home_current_position");
});

test("M. coordinates present with verified:false is UNVERIFIED — recovery never promotes it", () => {
  // Both halves: a plain unverified Home, and one whose recovery block says RECOVERED while
  // Scout still reports verified:false. Neither may read as verified.
  for (const over of [
    { verified: false, verification_recovery: null },
    { verified: false },                                  // RECOVERED recovery, unverified Home
    { verified: false, verification_recovery: { state: "RECOVERED", reason: "matched" } },
  ]) {
    const hs = homeStatus(HOME(over), {});
    assert.equal(hs.verified, false, JSON.stringify(over));
    assert.equal(hs.state, "unverified", JSON.stringify(over));
    assert.equal(hs.recoveredAfterRestart, false, JSON.stringify(over));
    // …and the coordinates are still there. Their presence proved nothing.
    assert.equal(hs.homeLat, 56.6635241);
  }
});

test("M2. an unavailable recovery reports its own state and Scout's reason, unverified", () => {
  const hs = homeStatus(HOME({
    verified: false,
    verification_recovery: { state: "UNAVAILABLE", reason: "Pixhawk boot session changed" },
  }), {});
  assert.equal(hs.state, "unverified");
  assert.equal(hs.recoveryState, "UNAVAILABLE");
  assert.equal(hs.recoveredAfterRestart, false);
  assert.equal(hs.recoveryReason, "Pixhawk boot session changed");
});

test("N. the planned Mission Home and the verified RTL Home are never given the same label", () => {
  // Scout measures the two margins against two different points, and they differ here.
  const e = S({ energy_feasibility: FEASIBLE }).energy;
  assert.notDeepEqual(e.plannedHome, e.rtlHome);
  assert.equal(e.plannedHome.source, "PLANNING_PACKAGE");
  assert.equal(e.rtlHome.source, "PIXHAWK_VERIFIED_HOME");

  const i = agentSrc.indexOf("function mxEnergyRows");
  const block = agentSrc.slice(i, agentSrc.indexOf("\n  }", i));
  assert.ok(block.includes(`"Planned Mission Home"`));
  assert.ok(block.includes(`"Verified RTL Home"`));
  // Neither is labelled with the bare word, which would let one be read as the other.
  assert.equal(block.includes(`row("Home"`), false);
});

test("a Scout that reports no verification_recovery claims none — the fields stay null", () => {
  const hs = homeStatus(HOME({ verification_recovery: undefined }), {});
  assert.equal(hs.recoveryState, null);
  assert.equal(hs.recoveryReason, null);
  assert.equal(hs.recoveredAfterRestart, false);
  // The Home itself is still verified — an absent recovery block is not a failed recovery.
  assert.equal(hs.verified, true);
});
