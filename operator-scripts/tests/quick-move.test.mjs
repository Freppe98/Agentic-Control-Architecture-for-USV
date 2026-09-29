// quick-move.test.mjs — the Map's right-click "Move <vehicle> here" policy and view model.
// Run: `node --test tests/` (or `npm test`).
//
// What these pin, in the order the feature can hurt someone if it is wrong:
//   1. The action is never offered for a vehicle that has not advertised it. Being in the
//      fleet is not a capability.
//   2. The UI's refusal reasons are the SAME machine codes the backend refuses with, so a
//      disabled menu item never sends the operator after the wrong problem.
//   3. Delivery, movement and completion stay three separate claims. Nothing in this module
//      can turn "Scout got the command" into "the vehicle arrived".
//   4. Quick move is not mission progress: it never touches mission state, mission overlays
//      or the mission-write refresh set, and the Map never offers it for a companion.
//   5. Uncertainty is shown as uncertainty. An unconfirmed delivery says so.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  QM_REASONS, quickMoveCapability, quickMoveCurrent, quickMoveGate, quickMoveView,
  quickMoveLabel, validTarget, vehicleDistanceTo, fmtTarget, deliveryLine, completionNote,
  STALE_STATUS_S,
} from "../operator/lib/quick-move.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const mapSrc = read("../operator/pages/Map.js");
const libSrc = read("../operator/lib/quick-move.js");
const apiSrc = read("../operator/services/api.js");
const pySrc = read("../quick_move.py");

// ── Fixtures. Labelled as such: none of this is a real vehicle. ────────────────────────
const cap = (over = {}) => ({
  supported: true, reason: null, nav_ready: null, max_distance_m: null,
  vehicle_class: "SURFACE", source: "scout", ...over,
});
const TARGET = { lat: 56.7005, lng: 13.0038 };
const vehicle = ({ capability = cap(), current = null, armed = true, ...over } = {}) => ({
  id: 2, name: "Scout", lat: 56.6995, lng: 13.00215,
  comm_state: "CONNECTED", telemetry: { armed, mode: "GUIDED" },
  quick_move: capability === null
    ? { schema: "quick-move-v1", reported: false, capability: null, current }
    : { schema: "quick-move-v1", reported: true, capability, current },
  ...over,
});
const status = (over = {}) => ({
  state: "MOVING", target: TARGET, target_offset_m: 0, target_mismatch: false,
  distance_remaining_m: 40, arrival_confirmed: null, loiter_verified: null,
  mode: "GUIDED", reason: null, terminal: false, completion: null,
  updated: { observed_at: 1, min_age_s: 0.4, reporting_delay_s: 0.1,
             estimated_age_s: 0.5, freshness_quality: "LOWER_BOUND", clock_anomaly: false },
  status_age_s: 0.4, ...over,
});
const current = (over = {}) => ({
  command_id: "cmd-1", requested_target: TARGET, requested_age_s: 3,
  delivery_confirmed: true, cancel_requested: null, status: status(), ...over,
});
const OK = { connected: true, hasControl: true, authority: "OPERATOR", target: TARGET };

// ── 1. Capability: absence means unsupported ───────────────────────────────────────────
test("a vehicle that has advertised nothing is unsupported, not unknown", () => {
  const c = quickMoveCapability(vehicle({ capability: null }));
  assert.equal(c.supported, false);
  assert.equal(c.source, "absent");
  assert.equal(quickMoveGate(vehicle({ capability: null }), OK).code, "not_reported");
});

test("a vehicle row with no quick_move block at all is unsupported", () => {
  const v = { id: 2, name: "Scout", comm_state: "CONNECTED", telemetry: { armed: true } };
  assert.equal(quickMoveCapability(v).supported, false);
  assert.equal(quickMoveGate(v, OK).enabled, false);
});

test("being connected, armed and under OPERATOR control is not a capability", () => {
  // The exact combination that makes every OTHER command available must still not make
  // this one available, because none of it is evidence the vehicle can do a guided move.
  assert.equal(quickMoveGate(vehicle({ capability: null }), OK).enabled, false);
  assert.equal(quickMoveGate(vehicle(), OK).enabled, true);
});

test("Scout's own unsupported reason is shown, not a generic one", () => {
  const v = vehicle({ capability: cap({ supported: false, reason: "No GUIDED on this build." }) });
  const g = quickMoveGate(v, OK);
  assert.equal(g.code, "not_supported");
  assert.equal(g.reason, "No GUIDED on this build.");
});

// ── 2. The gate, and its parity with the backend ───────────────────────────────────────
test("the UI reason codes are exactly the backend's, in the same order", () => {
  // The backend re-runs this policy at dispatch. If the two lists drift, a disabled menu
  // item explains one problem while the refusal would name another.
  // The tuple ends at a `)` in column 0 — NOT at the first `)` in the text, which lands
  // inside one of the trailing comments ("(fail closed)") and silently truncates the list.
  const block = /REASON_ORDER = \(([\s\S]*?)\n\)/.exec(pySrc);
  assert.ok(block, "quick_move.py must declare REASON_ORDER as a tuple");
  const backend = block[1].split("\n")
    .map((line) => /^\s*"([a-z_]+)",/.exec(line))
    .filter(Boolean).map((m) => m[1]);
  assert.deepEqual(QM_REASONS, backend,
    "operator/lib/quick-move.js QM_REASONS must mirror quick_move.py REASON_ORDER");
});

test("every reason code has operator-facing prose", () => {
  for (const code of QM_REASONS) {
    const g = quickMoveGate(vehicle({ capability: cap({ supported: false }) }), OK);
    assert.ok(typeof g.reason === "string" && g.reason.length > 0);
    assert.ok(libSrc.includes(`${code}:`), `${code} must have prose in QM_TEXT`);
  }
});

test("a disconnected vehicle is refused — a Quick move is never queued for later", () => {
  const g = quickMoveGate(vehicle(), { ...OK, connected: false });
  assert.equal(g.code, "link_not_current");
  assert.match(g.reason, /never queued/i);
});

test("no confirmed OPERATOR control means no Quick move", () => {
  assert.equal(quickMoveGate(vehicle(), { ...OK, hasControl: false, authority: "LOCAL_AGENT" }).code,
    "not_operator");
});

test("an unreadable authority is its own refusal, not 'take control'", () => {
  const g = quickMoveGate(vehicle(), { ...OK, hasControl: false, authority: null });
  assert.equal(g.code, "authority_unreadable");
  assert.doesNotMatch(g.reason, /Take Control/);
});

test("an authority change still being confirmed does not say 'take control' either", () => {
  const g = quickMoveGate(vehicle(), { ...OK, hasControl: false, authority: "OPERATOR" });
  assert.equal(g.code, "not_operator");
  assert.match(g.reason, /confirm/i);
});

test("a disarmed vehicle and an unknown armed state are different refusals", () => {
  assert.equal(quickMoveGate(vehicle({ armed: false }), OK).code, "disarmed");
  const v = vehicle(); delete v.telemetry.armed;
  assert.equal(quickMoveGate(v, OK).code, "armed_unknown");
});

test("navigation readiness blocks only when EXPLICITLY unavailable", () => {
  assert.equal(quickMoveGate(vehicle({ capability: cap({ nav_ready: false }) }), OK).code,
    "nav_not_ready");
  // Scout saying nothing is not a readiness failure and must never be invented as one.
  assert.equal(quickMoveGate(vehicle({ capability: cap({ nav_ready: null }) }), OK).enabled, true);
  assert.equal(quickMoveGate(vehicle({ capability: cap({ nav_ready: true }) }), OK).enabled, true);
});

test("an invalid target is refused", () => {
  for (const bad of [null, {}, { lat: 91, lng: 13 }, { lat: 56.7, lng: 181 },
                     { lat: 0, lng: 0 }, { lat: "x", lng: 13 }]) {
    assert.equal(quickMoveGate(vehicle(), { ...OK, target: bad }).code, "bad_target");
  }
  assert.equal(validTarget(TARGET), true);
});

test("beyond Scout's advertised range is refused with both distances", () => {
  const g = quickMoveGate(vehicle({ capability: cap({ max_distance_m: 50 }) }), OK);
  assert.equal(g.code, "too_far");
  assert.match(g.reason, /50 m/);
});

test("a Quick move already in flight blocks a second one", () => {
  assert.equal(quickMoveGate(vehicle(), { ...OK, busy: true }).code, "busy");
});

// ── 3. Delivery, movement and completion are three separate claims ────────────────────
test("an unconfirmed delivery is uncertainty, not failure and not success", () => {
  const line = deliveryLine({ delivery_confirmed: false }, { status: "EXPIRED" });
  assert.equal(line.uncertain, true);
  assert.match(line.text, /may or may not/i);
  assert.doesNotMatch(line.text, /failed/i);
});

test("an ACCEPTED command is delivery evidence and explicitly not arrival", () => {
  const line = deliveryLine({ delivery_confirmed: false }, { status: "EXECUTED" });
  assert.match(line.text, /awaiting movement status/i);
  assert.doesNotMatch(line.text, /arriv/i);
});

test("a rejected command reports Scout's own reason", () => {
  const line = deliveryLine({ delivery_confirmed: false },
                            { status: "REJECTED", reason: "GUIDED refused." });
  assert.equal(line.text, "GUIDED refused.");
  assert.equal(line.uncertain, false);
});

test("no movement state produces an arrival phrase on its own", () => {
  for (const state of ["STARTING", "MOVING", "ARRIVED"]) {
    const v = vehicle({ current: current({ status: status({ state }) }) });
    const view = quickMoveView(v, { connected: true });
    assert.equal(view.completion, null, `${state} is not a completion`);
    assert.doesNotMatch(view.stateText, /^Arrived and holding/,
      `${state} must not read as a finished hold`);
  }
});

test("ARRIVED is not COMPLETED — the hold is not yet verified", () => {
  const v = vehicle({ current: current({ status: status({ state: "ARRIVED", arrival_confirmed: true,
                                                          loiter_verified: false }) }) });
  const view = quickMoveView(v, { connected: true });
  assert.match(view.stateText, /confirming hold/i);
  assert.equal(view.completion, null);
});

test("a completion the backend confirmed carries no caveat", () => {
  const st = status({ state: "COMPLETED", terminal: true, completion: "CONFIRMED",
                      arrival_confirmed: true, loiter_verified: true });
  assert.equal(completionNote(st), null);
  const view = quickMoveView(vehicle({ current: current({ status: st }) }), { connected: true });
  assert.equal(view.stateText, "Arrived and holding");
});

test("a claimed completion says what evidence is missing", () => {
  const note = completionNote(status({ state: "COMPLETED", completion: "CLAIMED_UNVERIFIED" }));
  assert.match(note, /arrival/);
  assert.match(note, /verified LOITER/);
  assert.match(note, /unconfirmed/i);
});

test("a completion missing only the verified hold still reads as unconfirmed", () => {
  const note = completionNote(status({ state: "COMPLETED", completion: "CLAIMED_UNVERIFIED",
                                       arrival_confirmed: true, loiter_verified: false }));
  assert.match(note, /verified LOITER/);
  assert.doesNotMatch(note, /arrival or/);
});

// ── 4. Staleness and reconnection are handled honestly ────────────────────────────────
test("a movement we can no longer refresh reads as last known, never as current", () => {
  const v = vehicle({ current: current() });
  const live = quickMoveView(v, { connected: true });
  assert.equal(live.lastKnown, false);
  const dark = quickMoveView(v, { connected: false });
  assert.equal(dark.lastKnown, true);
  assert.match(dark.stateText, /last known/);
  assert.equal(dark.stateCls, "u");
});

test("a stalled report goes last-known even while the link still reads connected", () => {
  const v = vehicle({ current: current({ status: status({ status_age_s: STALE_STATUS_S + 5 }) }) });
  assert.equal(quickMoveView(v, { connected: true }).lastKnown, true);
});

test("nothing to show produces nothing — no empty card, no phantom target", () => {
  assert.equal(quickMoveView(vehicle(), { connected: true }), null);
  assert.equal(quickMoveCurrent(vehicle()), null);
});

test("a Scout target away from the clicked point is flagged, not quietly accepted", () => {
  const v = vehicle({ current: current({ status: status({ target_mismatch: true,
                                                          target_offset_m: 37.4 }) }) });
  const view = quickMoveView(v, { connected: true });
  assert.equal(view.targetMismatch, true);
  assert.equal(view.targetOffsetM, 37.4);
});

test("a stop REQUEST is never rendered as a stop", () => {
  const v = vehicle({ current: current({ cancel_requested: { reason: "Operator commanded LOITER.",
                                                             age_s: 2 } }) });
  const view = quickMoveView(v, { connected: true });
  assert.ok(view.cancelRequested);
  assert.equal(view.state, "MOVING", "the movement continues until Scout says otherwise");
});

test("distance remaining is only ever Scout's number", () => {
  const v = vehicle({ current: current({ status: status({ distance_remaining_m: null }) }) });
  assert.equal(quickMoveView(v, { connected: true }).remainingText, null,
    "the station must not substitute its own straight-line distance here");
});

// ── 5. Labels and distances the menu shows ────────────────────────────────────────────
test("the menu names the vehicle, never just its id", () => {
  assert.equal(quickMoveLabel(vehicle()), "Move Scout here");
  assert.equal(quickMoveLabel({ id: 3 }), "Move USV-3 here");
});

test("distance is null — not zero — when the vehicle has no usable position", () => {
  assert.equal(vehicleDistanceTo({ id: 2, lat: null, lng: null }, TARGET), null);
  assert.ok(vehicleDistanceTo(vehicle(), TARGET) > 100);
});

test("the destination readout is precise enough to read back", () => {
  assert.equal(fmtTarget({ lat: 56.7, lng: 13 }), "56.700000, 13.000000");
  assert.equal(fmtTarget(null), "—");
});

// ── 6. Source guarantees: what the Map must and must not do ───────────────────────────
test("the Map binds the action to a right-click and closes it on Escape and outside click", () => {
  assert.match(mapSrc, /map\.on\("contextmenu", openQuickMenu\)/);
  assert.match(mapSrc, /e\.key === "Escape".*closeQuickMenu\(\)/);
  assert.match(mapSrc, /_onOutside = .*closeQuickMenu\(\)/);
  assert.match(mapSrc, /map\.on\("movestart zoomstart", closeQuickMenu\)/);
});

test("changing the selected vehicle tears the menu and the target marker down", () => {
  const select = mapSrc.slice(mapSrc.indexOf("function select(id)"),
                              mapSrc.indexOf("function defaultSelection"));
  assert.match(select, /closeQuickMenu\(\)/, "the menu is bound to one vehicle");
  assert.match(select, /qmMarker.*removeLayer/s, "the target marker never leaks across vehicles");
});

test("the Map sends the move with no second confirmation dialog", () => {
  const menu = mapSrc.slice(mapSrc.indexOf("function openQuickMenu"),
                            mapSrc.indexOf("async function sendQuickMove"));
  assert.doesNotMatch(menu, /confirmModal/, "the menu item sends directly");
  assert.match(menu, /sendQuickMove\(v\.id, target\)/);
});

test("the Map never runs a timer that decides arrival or changes a mode", () => {
  const region = mapSrc.slice(mapSrc.indexOf("// ── QUICK MOVE"),
                              mapSrc.indexOf("function updateHomeMarker"));
  assert.doesNotMatch(region, /setTimeout|setInterval/,
    "no browser timer may decide anything about a quick move");
  for (const forbidden of ["uploadMission", "clearMission", "startMissionExecution",
                           "resumeMissionExecution", "setControlAuthority", '"ARM"']) {
    assert.ok(!region.includes(forbidden),
      `the quick-move code must never call ${forbidden}`);
  }
});

test("Stop is the ordinary LOITER command, not a second cancel mechanism", () => {
  assert.match(mapSrc, /data-qm-stop.*sendCommand\(SAFETY_HOLD_TYPE\)/s);
  assert.ok(!mapSrc.includes("cancelQuickMove"), "there is no bespoke cancel endpoint");
});

test("a failed send reports uncertainty, never a fabricated failure", () => {
  const send = mapSrc.slice(mapSrc.indexOf("async function sendQuickMove"),
                            mapSrc.indexOf("function updateQuickMoveMarker"));
  assert.match(send, /may or may not have been sent/i);
});

test("the API helper never confirms a quick move into the offline queue", () => {
  const helper = apiSrc.slice(apiSrc.indexOf("export function quickMove"),
                              apiSrc.indexOf("// --- Mission upload"));
  assert.doesNotMatch(helper, /confirm:\s*true/,
    "a quick move must never be confirmed into the queue-until-contact path");
});

test("quick move is never treated as a mission write", () => {
  const refresh = read("../operator/lib/mission-refresh.js");
  assert.ok(!refresh.includes("QUICK_MOVE"),
    "a quick move must not trigger a Pixhawk mission refetch");
  const mission = read("../operator/lib/mission-upload.js");
  assert.ok(!mission.includes("QUICK_MOVE"));
});

test("the action is offered for the selected FLEET vehicle only — never a companion", () => {
  // Companions are not fleet vehicles: they have no fleet id and no command route, and
  // this is what structurally prevents the Map from ever pointing a guided move at a UAV.
  const region = mapSrc.slice(mapSrc.indexOf("function openQuickMenu"),
                              mapSrc.indexOf("async function sendQuickMove"));
  assert.match(region, /fleet\.find\(\(x\) => x\.id === selId\)/);
  assert.ok(!region.includes("companion") && !region.includes("cmp"),
    "the menu never resolves a companion");
});

test("the status card is not mission progress and does not claim to be", () => {
  const card = mapSrc.slice(mapSrc.indexOf("function renderQuickMove"),
                            mapSrc.indexOf("function updateHomeMarker"));
  for (const forbidden of ["mpbar", "missionCounts", "etaBarText", "syncMissionOverlay"]) {
    assert.ok(!card.includes(forbidden),
      `the quick-move card must not read mission progress (${forbidden})`);
  }
});
