// start-readiness-layers.test.mjs — START ELIGIBILITY IS NOT AUTO READINESS IS NOT RTL READINESS.
//
// THE DEFECT THIS PINS. On a healthy, fully prepared mission sitting on the slipway — package
// consistent, hash matched, Scout reporting `start_eligible:true` — Home had simply not been set
// yet, because setting it is a PHASE OF START. The station rendered that as:
//
//     Agent NOT_READY
//     RTL Home unavailable
//     Start Mission  [disabled]
//
// beside a line that read "Home will be set during Start". Three of those four statements were
// about the step the disabled button was about to perform, and the fourth contradicted them.
// A manual Set Home made Start available instantly, which taught the operator to perform, by
// hand and outside Scout's guard, the exact transaction Scout owns end to end.
//
// SCOUT ANSWERS FOUR DIFFERENT QUESTIONS AND THEY ARE NOT ONE QUESTION:
//
//   start_eligible   may the guarded Start TRANSACTION be entered?   agent.mission_execution
//   home verified    is there a proven runtime Home right now?       agent.home_status.verified
//   ready_for_auto   would Scout accept AUTO this instant?           agent.home_status
//   ready_for_rtl    would Scout accept RTL this instant?            agent.home_status
//
// The rules that must hold forever after:
//   • The Start button follows Scout's `start_eligible`. NEVER `ready_for_auto`, NEVER
//     `ready_for_rtl`, NEVER `verified`, and never a locally reconstructed eligibility.
//   • AUTO and RTL readiness are DISPLAYED, separately and honestly, and reach no gate.
//   • `start_eligible:false` still withholds Start, with Scout's own words — package stale,
//     hash mismatch, position stale, mission verification unavailable. Only HOME pre-verification
//     is deferred, and nothing else about the package contract moves.
//   • A Scout that reports no eligibility contract is NOT guessed eligible.
//
// Scout is untouched: Start still calls POST /api/vehicles/{id}/mission-execution/start, which
// performs its own fresh, fail-closed proof and owns authority, LOITER, Set Home, verification,
// ARM and AUTO. The operator station is supervisory and never issues Set Home on Start's behalf.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeStatus, missionCardView, startGate, startEligibility,
} from "../operator/lib/mission-execution.js";
import {
  readinessView, readinessLayers, READINESS, START_BLOCK, HOME_DURING_START_NOTE,
  RTL_AFTER_HOME_NOTE, AUTO_WAITING_FOR_START_TEXT, RTL_WAITING_FOR_HOME_TEXT,
} from "../operator/lib/mission-readiness.js";
import { homeStatus } from "../operator/lib/home.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const mapSrc = read("../operator/pages/Map.js");

// ── The live bench payload, as Scout actually sends it ────────────────────────────────────
// Taken from tests/fixtures/scout-status-live.json: DISARMED in MANUAL, authority OPERATOR,
// package consistent, route hash matched — and `start_eligible:true` with `can_start:false`,
// which is the pairing the whole contract exists to keep apart.
const envelope = (over = {}) => ({
  ok: true, supported: true, reachable: true,
  scout: {
    supported: true,
    state: "NOT_READY", effective_state: "NOT_READY", active_operation_id: null,
    mission_id: "msn-48614d039d59", mode: "MANUAL",
    sequence: { current: 0, count: 15 },
    replanning: { active: false, fsm_state: "MONITORING" },
    return_completion: {},
    authority_status: "OPERATOR",
    can_start: false, can_pause: false, can_resume: false,
    execution_ready: false, start_eligible: true,
    start_block_reason: "AUTHORITY_NOT_LOCAL_AGENT", authority_blocks_start: true,
    verified_home: null, home_verification_distance_m: null,
    mission_execution_enabled: true, last_error: null,
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));

// Scout's continuously-reported agent.home_status, mirrored by the backend's home_block() onto
// the fleet row. The UNVERIFIED case is the fixture's own: verified false, both interlocks false,
// with Scout's own sentence for why.
const homeRow = (over = {}) => ({
  home: {
    reported: true, source: "scout", available: true, reachable: true,
    lat: 56.650388, lng: 12.8709758,
    home_position: { latitude: 56.650388, longitude: 12.8709758 },
    verified: false, verified_at: null,
    verification_method: null, verification_distance_m: null, verification_recovery: null,
    ready_for_auto: false, ready_for_rtl: false,
    reason: "Home has not been verified this runtime -- perform Set Home Here before AUTO/RTL/RESUME.",
    stale: false, ...over,
  },
  lat: 56.6635657, lng: 12.8814324,
});
const homeVerifiedRow = () => homeRow({
  verified: true, verified_at: "2026-08-13T09:00:00Z", verification_method: "READBACK",
  verification_distance_m: 0.4, ready_for_auto: true, ready_for_rtl: true, reason: null,
});

/** The card exactly as Map.js renderAgentMission builds it: the gate decides Start, the layers
 *  present Scout's home_status, and the two are wired from the same call. */
const card = (status, { connected = true, busy = false, v = homeRow(), ...rest } = {}) => {
  const gate = startGate(status, { connected, busy, missionId: rest.missionId || null });
  const hs = homeStatus(v);
  return missionCardView(status, {
    busy, startBlocked: !gate.canStart, startBlockedReason: gate.reason,
    readiness: readinessView(gate),
    homeVerified: hs.verified,
    homeState: hs.state, homeReported: hs.reported, homeStale: hs.stale,
    homeReason: hs.reason || hs.scoutReason,
    readyForAuto: hs.reported ? hs.readyForAuto : null,
    readyForRtl: hs.reported ? hs.readyForRtl : null,
    ...rest,
  });
};
const startBtn = (c) => c.buttons.find((b) => b.action === "start") || null;
const startEnabled = (c) => !!(startBtn(c) && startBtn(c).enabled);
/** Everything the card actually SAYS, so a forbidden sentence cannot hide in an unchecked slot. */
const says = (c) => [
  c.headline, c.chip, c.blocker && c.blocker.text, c.home && c.home.text,
  c.info && c.info.text, ...(c.readinessLayers ? c.readinessLayers.rows.map((r) => r.text) : []),
].filter(Boolean).join(" | ");

// ══ 1. start_eligible true + Home unverified → START ENABLED ═══════════════════════════════
test("1. start_eligible:true with an UNVERIFIED Home leaves Start ENABLED", () => {
  const st = S();
  assert.equal(homeStatus(homeRow()).verified, false, "the premise: Home is not verified");
  const gate = startGate(st, { connected: true });
  assert.equal(gate.canStart, true, "an unverified Home may not withhold Start");
  assert.equal(gate.code, null);
  const c = card(st);
  assert.equal(startEnabled(c), true, "THE BUG: Start was disabled here");
  assert.equal(c.chip, "READY");
  assert.equal(c.blocker, null, "there is nothing to fix — Home is a pending step, not a fault");
});

// ══ 2. ready_for_auto false + start_eligible true → START ENABLED ══════════════════════════
test("2. ready_for_auto:false never gates Start while Scout says start_eligible", () => {
  const st = S();
  const v = homeRow({ ready_for_auto: false, ready_for_rtl: true });
  assert.equal(startGate(st, { connected: true }).canStart, true);
  const c = card(st, { v });
  assert.equal(startEnabled(c), true);
  // The Start button asks "may I begin the guarded transaction?", not "could I command AUTO
  // this instant?". Those are different questions and only Scout's start_eligible answers the
  // first — startEligibility() reads it and nothing else about AUTO.
  const elig = startEligibility(st);
  assert.equal(elig.eligible, true);
  assert.equal(elig.source, "scout");
});

// ══ 3. ready_for_rtl false + start_eligible true → START ENABLED ═══════════════════════════
test("3. ready_for_rtl:false never gates Start, and never reads as a Start blocker", () => {
  const st = S();
  const c = card(st, { v: homeRow({ ready_for_rtl: false }) });
  assert.equal(startEnabled(c), true);
  assert.equal(c.blocker, null);
  // The forbidden sentence: "RTL Home unavailable" must not appear anywhere on a card whose
  // Start is offered. RTL readiness is shown — as its own layer, in its own words.
  assert.equal(/RTL Home unavailable/i.test(says(c)), false, says(c));
  assert.equal(c.readinessLayers.rtl.text, RTL_WAITING_FOR_HOME_TEXT);
  assert.equal(c.readinessLayers.rtl.tone, null, "a pending step carries no warning tone");
});

// ══ 4. Package stale → START DISABLED (the package contract is untouched) ══════════════════
test("4. a STALE planning package still disables Start, in Scout's own words", () => {
  const st = S({ start_eligible: false, start_block_reason: "PLANNING_PACKAGE_STALE" });
  const gate = startGate(st, { connected: true });
  assert.equal(gate.canStart, false);
  assert.equal(gate.code, START_BLOCK.NOT_ELIGIBLE);
  assert.match(gate.detail, /PLANNING_PACKAGE_STALE/);
  const c = card(st);
  assert.equal(startEnabled(c), false);
  assert.equal(c.chip, "NOT_READY");
  assert.equal(c.blocker.text, "Agent planning package is stale");
});

// ══ 5. Mission hash mismatch → START DISABLED ══════════════════════════════════════════════
test("5. a route-hash mismatch still disables Start", () => {
  const st = S({ start_eligible: false, start_block_reason: "ROUTE_HASH_STALE" });
  const c = card(st);
  assert.equal(startEnabled(c), false);
  assert.equal(c.blocker.text, "Agent planning package is stale");

  // And Scout's binding verdict — a NEW mission uploaded while the PREVIOUS run owns the
  // vehicle — is its own, more specific refusal, still ahead of everything else.
  const conflict = S({
    binding: { binding_state: "STALE_MISMATCH", package_mission_id: "msn-new" },
  });
  const g = startGate(conflict, { connected: true });
  assert.equal(g.canStart, false);
  assert.equal(g.code, START_BLOCK.MISSION_REPLACEMENT_CONFLICT);
});

// ══ 6. Position stale / Scout start_eligible false → START DISABLED ════════════════════════
test("6. Scout's own start_eligible:false is decisive, whatever the reason", () => {
  for (const [reason, shown] of [
    ["POSITION_STALE", "Position data stale"],
    ["MISSION_UNAVAILABLE", "Mission unavailable"],
    ["BATTERY_INVALID", "Battery estimate unavailable"],
    ["INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION", "Insufficient energy for planned mission"],
  ]) {
    const c = card(S({ start_eligible: false, start_block_reason: reason }));
    assert.equal(startEnabled(c), false, reason);
    assert.equal(c.chip, "NOT_READY", reason);
    assert.equal(c.blocker.text, shown, reason);
  }
});

// ══ 7. Home-unverified messaging ═══════════════════════════════════════════════════════════
test("7. an unverified Home reads 'Home will be set and verified during Start'", () => {
  const c = card(S());
  assert.equal(HOME_DURING_START_NOTE, "Home will be set and verified during Start");
  assert.equal(c.home.text, HOME_DURING_START_NOTE);
  assert.equal(c.home.tone, null, "it is information, not a warning");
  // The secondary detail is available where it belongs — in the note's own tooltip — and says
  // RTL BECOMES available, never that it is unavailable.
  assert.match(c.home.title, new RegExp(RTL_AFTER_HOME_NOTE));
  assert.equal(RTL_AFTER_HOME_NOTE, "RTL becomes available after Home verification");

  // The three layers, side by side, exactly as the target reading:
  //   Home: Not verified · AUTO readiness: Waiting for Start Home setup ·
  //   RTL readiness: Waiting for verified Home · Start Mission: ENABLED
  assert.deepEqual(c.readinessLayers.rows.map((r) => [r.label, r.text]), [
    ["Home", "Not verified"],
    ["AUTO readiness", AUTO_WAITING_FOR_START_TEXT],
    ["RTL readiness", RTL_WAITING_FOR_HOME_TEXT],
  ]);
  assert.equal(startEnabled(c), true);
  // None of the three is tinted as a fault.
  for (const r of c.readinessLayers.rows) assert.notEqual(r.tone, "warn", r.label);
});

// ══ 8. Home verified → the normal READY display ════════════════════════════════════════════
test("8. a VERIFIED Home reads READY, with the pre-start Home note withdrawn", () => {
  const c = card(S(), { v: homeVerifiedRow() });
  assert.equal(startEnabled(c), true);
  assert.equal(c.chip, "READY");
  assert.equal(c.home, null, "nothing left to say about Home — it is verified");
  assert.deepEqual(c.readinessLayers.rows.map((r) => [r.label, r.text, r.tone]), [
    ["Home", "Verified", "ok"],
    ["AUTO readiness", "Ready", "ok"],
    ["RTL readiness", "Available", "ok"],
  ]);
});

test("8b. a VERIFIED Home that Scout still refuses AUTO/RTL for is a real gap and says so", () => {
  const v = homeRow({ verified: true, verified_at: "2026-08-13T09:00:00Z",
    ready_for_auto: false, ready_for_rtl: false, reason: "RTL home rejected by the flight controller" });
  const c = card(S(), { v });
  assert.equal(c.readinessLayers.home.text, "Verified");
  assert.equal(c.readinessLayers.auto.tone, "warn");
  assert.equal(c.readinessLayers.rtl.tone, "warn");
  assert.match(c.readinessLayers.rtl.title, /rejected by the flight controller/);
  // It is STILL not a Start gate. Scout owns that verdict and has not withdrawn it.
  assert.equal(startEnabled(c), true);
});

// ══ 9. A Start in flight ═══════════════════════════════════════════════════════════════════
test("9. a Start in flight shows the transaction and cannot be double-submitted", () => {
  const busyGate = startGate(S(), { connected: true, busy: true });
  assert.equal(busyGate.canStart, false);
  assert.equal(busyGate.code, START_BLOCK.BUSY);
  const c = card(S(), { busy: true, starting: true });
  assert.equal(startEnabled(c), false, "the button is rendered disabled from the same flag");
  assert.equal(c.working, true);
  assert.equal(c.startPhase, "preflight");
  assert.equal(c.blocker, null, "the card shows the operation it is waiting on, not a blocker");

  // Scout's own transaction states drive the phase line, including the Home phase — so the
  // operator watches Start do the Home work rather than being told to do it first.
  assert.equal(card(S({ state: "SETTING_HOME" }), { starting: true }).startPhase, "home");
  assert.equal(card(S({ state: "VERIFYING_HOME" }), { starting: true }).startPhase, "home");

  // The synchronous guard in Map.js, not just the disabled attribute: a second press during the
  // round-trip returns before any request is made.
  const onAction = mapSrc.slice(mapSrc.indexOf("async function onMissionAction"));
  assert.match(onAction.slice(0, 400), /if \(mission\.busy\) return;/);
  const tx = mapSrc.slice(mapSrc.indexOf("function missionTransaction"));
  assert.match(tx.slice(0, 400), /if \(mission\.busy \|\| selId == null\) return;/);
  assert.match(tx.slice(0, 600), /mission\.busy = true;/);
});

// ══ 10. A Start that fails during Set Home ═════════════════════════════════════════════════
test("10. a Start that fails while setting Home shows Scout's failure, never a fake success", () => {
  // Scout refused inside its own Home phase. The card must report the failure, keep the chip out
  // of READY, and never present the run as started.
  const st = S({ state: "FAILED", start_eligible: false,
    start_block_reason: "HOME_NOT_VERIFIED",
    last_error: { code: "HOME_NOT_VERIFIED",
      message: "Set Home read-back was 41.2 m from the requested launch position" } });
  const c = card(st);
  assert.equal(startEnabled(c), false);
  assert.notEqual(c.chip, "READY");
  assert.equal(c.blocker.text, "HOME_NOT_VERIFIED");
  assert.match(c.blocker.title, /41\.2 m from the requested launch position/);
  // The pre-start "Home will be set and verified during Start" promise is NOT repeated over a
  // Home step that has already been attempted and failed. It is a promise about something still
  // ahead; here the failure is the whole message.
  assert.equal(c.home, null);
  assert.equal(c.readinessLayers, null);
  assert.equal(/will be set and verified during Start/.test(says(c)), false, says(c));

  // A Start that never reached the vehicle is reported as exactly that, and not as a vehicle
  // failure — nothing is claimed to have happened that did not.
  const blocked = card(S({ start_eligible: false, start_block_reason: "PLANNING_PACKAGE_MISSING" }));
  assert.equal(startEnabled(blocked), false);
  assert.equal(blocked.blocker.text, "Agent planning package missing");
});

// ══ 11. Manual Set Home survives as an explicit operator tool ══════════════════════════════
test("11. manual Set Home remains available, independently, and is never automatic", () => {
  // The control still exists on the Map, still gated only by its own preconditions.
  assert.match(mapSrc, /data-cmd="SET_HOME"|SET_HOME/);
  // Nothing in the Start path issues it. The Start click calls ONE endpoint and no other.
  const onAction = mapSrc.slice(mapSrc.indexOf("async function onMissionAction"),
    mapSrc.indexOf("async function onMissionAction") + 2000);
  assert.match(onAction, /api\.startMissionExecution\(id, \{\}\)/);
  assert.equal(/SET_HOME/.test(onAction), false, "Start must not set Home from the operator side");
  const api = read("../operator/services/api.js");
  assert.match(api, /mission-execution\/start/, "Start still calls Scout's own start endpoint");
});

// ══ 12. An older / partial Scout: fail closed, never guess ═════════════════════════════════
test("12. a Scout that reports no eligibility contract is never GUESSED eligible", () => {
  // The contract keys are absent entirely. `eligibilityReported` is false and the reading falls
  // back to can_start — which, with authority already LOCAL_AGENT and can_start:false, is a
  // definite refusal and stays one.
  const bare = normalizeStatus({ ok: true, supported: true, reachable: true, scout: {
    supported: true, state: "NOT_READY", mission_id: "msn-1", can_start: false,
    authority_status: "LOCAL_AGENT", mission_execution_enabled: true } });
  assert.equal(bare.eligibilityReported, false);
  assert.equal(bare.startEligible, null, "a MISSING field is null, never false and never true");
  const elig = startEligibility(bare);
  assert.equal(elig.eligible, false);
  assert.equal(elig.source, "can_start");
  assert.equal(startGate(bare, { connected: true }).canStart, false);

  // And an unreadable status claims nothing at all in either direction — whether the read failed
  // (STATUS_UNAVAILABLE) or there is no Scout to read (UNSUPPORTED), Start is withheld and the
  // two are told apart rather than blurred.
  const unread = normalizeStatus({ ok: false, supported: true, reachable: false, scout: null });
  assert.equal(startEligibility(unread).eligible, false);
  assert.equal(startGate(unread, { connected: true }).code, START_BLOCK.STATUS_UNAVAILABLE);
  const absent = normalizeStatus(null);
  assert.equal(startEligibility(absent).eligible, false);
  assert.equal(startGate(absent, { connected: true }).canStart, false);
  assert.equal(startGate(absent, { connected: true }).code, START_BLOCK.UNSUPPORTED);

  // A Scout that reports no home_status at all does not have its fail-closed `false` defaults
  // presented as an answer it never gave.
  const c = card(S(), { v: { home: { reported: false, available: false, reachable: null,
    verified: false, ready_for_auto: false, ready_for_rtl: false, stale: false,
    reason: "Scout does not report Home status yet." } } });
  assert.deepEqual(c.readinessLayers.rows.map((r) => r.text),
    ["Not reported", "Not reported", "Not reported"]);
  assert.equal(startEnabled(c), true, "silence about Home is still not a Start blocker");
});

// ══ THE STRUCTURAL INVARIANTS ══════════════════════════════════════════════════════════════

test("the readiness layers reach no gate — startGate never reads AUTO/RTL/verified", () => {
  // Exhaustive over the 2×2×2 of Scout's three home_status facts: the gate's verdict is
  // identical in all eight, because none of them is an input to it.
  const base = startGate(S(), { connected: true });
  for (const verified of [true, false]) {
    for (const ready_for_auto of [true, false]) {
      for (const ready_for_rtl of [true, false]) {
        const v = homeRow({ verified, ready_for_auto, ready_for_rtl });
        const c = card(S(), { v });
        assert.equal(startEnabled(c), base.canStart,
          `verified=${verified} auto=${ready_for_auto} rtl=${ready_for_rtl}`);
      }
    }
  }
  // And the gate's own source carries no reference to either interlock.
  const src = read("../operator/lib/mission-execution.js");
  const fn = src.slice(src.indexOf("export function startGate"),
    src.indexOf("export const PAUSABLE_STATES"));
  for (const forbidden of ["readyForAuto", "readyForRtl", "ready_for_auto", "ready_for_rtl"]) {
    assert.equal(fn.includes(forbidden), false, `startGate must not read ${forbidden}`);
  }
});

test("the badge and the button are ONE derivation — NOT_READY beside an enabled Start is impossible", () => {
  // The chip answers "may this mission be started?", so it follows the same gate the button does.
  for (const over of [
    {},
    { start_eligible: false, start_block_reason: "PLANNING_PACKAGE_STALE" },
    { state: "RUNNING", can_pause: true },
    { state: "COMPLETED_HOLD" },
  ]) {
    const c = card(S(over));
    if (c.chip === "READY") assert.equal(startEnabled(c), true, JSON.stringify(over));
    if (c.chip === "NOT_READY") assert.equal(startEnabled(c), false, JSON.stringify(over));
  }
  // READY means "ready to START" — Scout's own `execution_ready` (ready for AUTO right now) is a
  // DIFFERENT layer and is reported under its own name, never as this badge.
  const c = card(S());
  assert.equal(c.chip, "READY");
  assert.equal(readinessView(startGate(S(), { connected: true })).state, READINESS.READY);
  assert.equal(S().executionReady, false, "READY does NOT mean ready for AUTO this instant");
});

test("the Start control is fed by ONE authoritative source — the mission-execution status", () => {
  // PRECEDENCE, as a source guard. Inside renderAgentMission the gate is built from `S` (the
  // mission-execution status) plus link/busy only. The fleet payload's home_status is read into
  // `hs` and passed to the CARD, never to the gate — so a fleet poll landing after a
  // mission-execution poll can never turn a start_eligible:true into NOT_READY due to Home.
  const start = mapSrc.indexOf("function renderAgentMission");
  const body = mapSrc.slice(start, mapSrc.indexOf("\n  }", mapSrc.indexOf("return `<div class=\"amx\">", start)));
  const gateCall = body.slice(body.indexOf("mx.startGate("), body.indexOf("const rv ="));
  for (const f of ["hs", "homeStatus", "readyFor", "verified"]) {
    assert.equal(gateCall.includes(f), false, `the Start gate must not read ${f}`);
  }
  assert.match(body, /const hs = homeStatus\(v\);/);
  assert.match(body, /homeVerified: hs\.verified/);
});

test("readinessLayers is pure, tri-state, and never invents a verdict", () => {
  // Stale is its own answer: Scout's LAST word, not a current permission.
  const stale = readinessLayers({ homeState: "unverified", homeReported: true, homeStale: true,
    readyForAuto: false, readyForRtl: false });
  assert.deepEqual(stale.rows.map((r) => r.text),
    ["Last known — not confirmed", "Last known — not confirmed", "Last known — not confirmed"]);
  for (const r of stale.rows) assert.equal(r.tone, "idle");

  // Nothing supplied at all: three honest silences, no fabricated readiness.
  assert.deepEqual(readinessLayers().rows.map((r) => r.text),
    ["Not reported", "Not reported", "Not reported"]);

  // The ONE case where an unverified Home is a warning: Scout explicitly declaring it requires
  // a verified Home before the transaction may be entered.
  const required = readinessLayers({ homeState: "unverified", homeReported: true,
    readyForAuto: false, readyForRtl: false, homeRequiredBeforeStart: true });
  assert.equal(required.home.tone, "warn");
  assert.match(required.home.title, /requires an already-verified Home/);
});
