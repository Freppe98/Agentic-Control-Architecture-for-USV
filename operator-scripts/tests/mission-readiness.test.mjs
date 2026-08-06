// mission-readiness.test.mjs — the NO-POLLED-PREFLIGHT contract, the stable Start gate, and the
// strict replanning evidence rule.
//
// THE DEFECT THIS PINS. On a stable, CONNECTED Scout — DISARMED, MANUAL, waypoint 0 / 41, agent
// status IDLE, no revised route, no replan transaction — the Map's Agent Mission card alternated
// every few seconds between
//
//     READY / Start Mission        and        NOT_READY / Replanning readiness not confirmed
//
// The vehicle was not changing. The Map was re-running the Start preflight on its ordinary
// refresh interval, and that preflight is an EXPENSIVE, SHORT-LIVED proof: the backend serves its
// Pixhawk read-back evidence through a 10 s cache (main.PIXHAWK_READBACK_TTL_S), so roughly every
// tenth poll paid for a live MAVLink mission download. A download that timed out or arrived
// partial answered can_start:false — with THREE blockers, because the package hash chain and
// Scout's replanning readiness are both anchored on the read-back. Not one of them was a fact
// about the vehicle, and one of them carried the word "replanning".
//
// The rules that must hold forever after:
//   • The Map's polling NEVER calls mission-execution/preflight. Status, and only status.
//   • Start availability comes from STABLE blockers only — disconnected, unsupported, no mission,
//     an active operation, explicit replanning, already running, a terminal state needing Rearm.
//     A missing / refreshing / stale readback proof may never withdraw Start.
//   • A one-shot preflight refresh is INFORMATION. `checking` is a spinner; it changes no button.
//   • "Agent is replanning" comes ONLY from explicit replan evidence — replan.active === true or
//     one of the replan controller's own transaction states. Never from a refresh flag, a pending
//     status request, NOT_READY, stale evidence, a missing field, or a busy flag.
//   • An unverified Home before Start is "Home will be set during Start", not a blocker.
//
// Scout is untouched by all of this: the Start endpoint still performs its own fresh, fail-closed
// proof before any write, and now forces a LIVE read-back to do it.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  normalizeStatus, missionCardView, isReplanning, shortStartBlocker, startGate, startFailure,
  EXPLICIT_REPLAN_STATES, EFFECTIVE_REPLANNING, READINESS_CHIP_STATES, START_FAILURE_TITLE,
  interpretTransaction, OUTCOME,
} from "../operator/lib/mission-execution.js";
import {
  READINESS, CHECKING_TEXT, START_BLOCK, START_PHASES, START_PHASE_TEXT,
  HOME_DURING_START_NOTE, readinessView, preflightNote, startPhase,
} from "../operator/lib/mission-readiness.js";
import { FSM_ACTIVE_ORDER, FSM_IDLE } from "../operator/lib/replan.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");

// THE LIVE PAYLOAD from the bench observation: Scout stable and connected, vehicle DISARMED in
// MANUAL at waypoint 0 / 41, agent IDLE, replanning inactive, authority still with the operator
// (so Scout reports NOT_READY / can_start:false — the condition Start itself resolves).
const envelope = (over = {}) => ({
  ok: true, supported: true, reachable: true,
  scout: {
    supported: true,
    state: "NOT_READY", effective_state: "NOT_READY", active_operation_id: null,
    mission_id: "msn-329c2faff137", mode: "MANUAL",
    sequence: { current: 0, count: 41 },
    replanning: { active: false, fsm_state: "MONITORING" },
    return_completion: {},
    authority_status: "OPERATOR",
    can_start: false, can_pause: false, can_resume: false,
    mission_execution_enabled: true, last_error: null,
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));

/** The preflight body the backend returns while a readiness proof has not (yet) been obtained —
 *  its blockers carry the readiness check's own label, which is where the word came from. */
const READINESS_BLOCKERS = [
  "Pixhawk readback hash match: Pixhawk read-back is unreachable — the route on the flight " +
    "controller cannot be confirmed",
  "Scout replanning readiness: Scout does not report replanning readiness for this mission",
];
const preflight = (over = {}) => ({
  ok: true, mission_id: "msn-329c2faff137", can_start: true, blockers: [], checks: [],
  proof_complete: true, readiness_refreshing: false, readiness_reason_code: null, ...over,
});

/** The card as the Map builds it: the gate decides Start, the view presents it. */
const card = (status, { refreshing = false, connected = true, busy = false, ...rest } = {}) => {
  const gate = startGate(status, { connected, busy, missionId: rest.missionId || null });
  return missionCardView(status, {
    busy, startBlocked: !gate.canStart, startBlockedReason: gate.reason,
    readiness: readinessView(gate, { refreshing }), ...rest,
  });
};
const startBtn = (c) => c.buttons.find((b) => b.action === "start") || null;
const startEnabled = (c) => !!(startBtn(c) && startBtn(c).enabled);
const says = (c) => [c.headline, c.chip, c.blocker && c.blocker.text, c.checkingText,
  c.home && c.home.text, c.info && c.info.text].filter(Boolean).join(" | ");
/** The one thing that must never be said without explicit evidence. */
const claimsReplanning = (c) => /replanning|replan/i.test(says(c));

const mapSrc = read("../operator/pages/Map.js");
/** The body of a named function in Map.js, up to the next top-level `  function ` at the same
 *  indentation — so a source guard reads the WHOLE function, not a fixed-length slice. */
function fnBody(name) {
  const start = mapSrc.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `Map.js must define ${name}`);
  const next = mapSrc.indexOf("\n  }", start);
  return mapSrc.slice(start, next === -1 ? mapSrc.length : next + 4);
}

// ── A. THE MAP NEVER POLLS THE PREFLIGHT ────────────────────────────────────────────────
test("no recurring timer in the Map reads the Start preflight", () => {
  // The defect, as a source guard: every setInterval body, and every function any of them calls,
  // must be free of getMissionExecutionPreflight. This is the assertion that would have failed
  // before the change and the one that must never be allowed to pass again.
  const intervals = [...mapSrc.matchAll(/setInterval\(([\s\S]*?)\, \d+\)/g)].map((m) => m[1]);
  assert.ok(intervals.length >= 4, "the Map still polls several lightweight feeds");
  for (const body of intervals) {
    assert.doesNotMatch(body, /[Pp]reflight/, body);
  }
  // The status poll is the one that runs on the interval, and it fetches STATUS ONLY.
  assert.match(mapSrc, /setInterval\(\(\) => loadMissionStatus\(selId\), 3000\)/);
  const poll = fnBody("loadMissionStatus");
  assert.match(poll, /api\.getMissionExecutionStatus\(id\)/);
  assert.doesNotMatch(poll, /getMissionExecutionPreflight/);
  // …and the old combined loader is gone, so it cannot be re-attached to a timer by accident.
  assert.equal(mapSrc.includes("loadMissionExecution("), false,
    "the combined status+preflight loader must not survive");
});

test("the preflight runs only from the allowed one-shot moments", () => {
  const refresh = fnBody("refreshPreflight");
  assert.match(refresh, /api\.getMissionExecutionPreflight\(id\)/);
  // Exactly one call site for the endpoint in the whole page.
  assert.equal((mapSrc.match(/getMissionExecutionPreflight\(/g) || []).length, 1);
  // Every caller passes a REASON, and the set of reasons is the allowed set.
  const reasons = [...mapSrc.matchAll(/refreshPreflight\([^,]+, "([a-z-]+)"\)/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(reasons)].sort(),
    ["manual", "mission", "reconnect", "selection", "transaction"]);
  // None of them is inside a setInterval / setTimeout.
  assert.doesNotMatch(mapSrc, /set(Interval|Timeout)\([^)]*refreshPreflight/);
});

test("a Start click issues EXACTLY ONE backend Start intent", () => {
  // One user intent → one operator endpoint. The browser does not preflight first, does not
  // sequence an authority transfer, and cannot submit twice: the endpoint below performs the
  // fresh proof, the hand-off and Scout's Start as ONE transaction with phases.
  const wiring = fnBody("onMissionAction");
  assert.equal((mapSrc.match(/api\.startMissionExecution\(/g) || []).length, 1);
  assert.match(wiring, /missionTransaction\("Start Mission", action, \(id\) => api\.startMissionExecution\(id, \{\}\)\)/);
  // No second call of any kind rides along with the click.
  assert.doesNotMatch(wiring, /getMissionExecutionPreflight/);
  assert.doesNotMatch(wiring, /setControlAuthority\([^)]*LOCAL_AGENT/);
  // Single-flight, in the handler AND in the render.
  assert.match(wiring, /if \(mission\.busy\) return;/);
  assert.match(fnBody("missionTransaction"), /if \(mission\.busy \|\| selId == null\) return;/);
  assert.match(mapSrc, /busy: mission\.busy/);
});

test("status polling still drives lifecycle state and waypoint progress", () => {
  // The lightweight feed keeps doing its job: state, mode, progress, active operation and the
  // explicit replanning overlay all come off the status the 3 s poll reads.
  const running = S({ state: "RUNNING", mode: "AUTO", can_start: false, can_pause: true,
    can_stop: true, sequence: { current: 4, count: 41 } });
  const c = card(running);
  assert.equal(c.headline, "AUTO · WP 4 / 41");
  assert.deepEqual(c.buttons.map((b) => b.action), ["pause", "stop"]);
  const mid = S({ state: "SETTING_HOME", active_operation_id: "op-9", can_start: false });
  assert.equal(card(mid).working, true);
  assert.deepEqual(card(mid).buttons, []);
});

// ── B. A REFRESH CANNOT CHANGE START AVAILABILITY ───────────────────────────────────────
test("a preflight refresh in flight leaves Start exactly as it was", () => {
  // THE ANTI-FLICKER INVARIANT. `checking` is a spinner; canStart comes from the gate. The two
  // are computed from different inputs on purpose, and this asserts they cannot be confused.
  const s = S({ state: "READY", can_start: true });
  const quiet = card(s);
  const refreshing = card(s, { refreshing: true });
  assert.equal(quiet.checking, false);
  assert.equal(refreshing.checking, true);
  assert.equal(refreshing.checkingText, CHECKING_TEXT);
  assert.equal(startEnabled(refreshing), startEnabled(quiet));
  assert.equal(refreshing.chip, quiet.chip);
  assert.equal(refreshing.headline, quiet.headline);
  assert.equal(claimsReplanning(refreshing), false, says(refreshing));
});

test("readinessView copies the gate's verdict and never overrides it with `refreshing`", () => {
  for (const canStart of [true, false]) {
    const gate = { canStart, code: canStart ? null : START_BLOCK.NO_MISSION, reason: "r" };
    for (const refreshing of [true, false]) {
      const rv = readinessView(gate, { refreshing });
      assert.equal(rv.canStart, canStart, `${canStart}/${refreshing}`);
      assert.equal(rv.state, canStart ? READINESS.READY : READINESS.NOT_READY);
      assert.equal(rv.checking, refreshing);
    }
  }
});

test("NO RECURRING FLICKER — repeated polls over an unchanged vehicle never repaint the card", () => {
  // The regression, simulated: twenty consecutive renders of the SAME stable status, with the
  // one-shot preflight refresh flapping on and off underneath (which is what the ~10 s read-back
  // cycle used to do). The chip, the headline and the Start button must be constant.
  const stable = S({ state: "READY", can_start: true });
  const seen = new Set();
  for (let i = 0; i < 20; i++) {
    const c = card(stable, { refreshing: i % 3 === 0 });
    seen.add(`${c.chip}|${c.headline}|${startEnabled(c)}`);
    assert.equal(claimsReplanning(c), false, says(c));
  }
  assert.deepEqual([...seen], ["READY|Ready to start|true"], [...seen].join(" ⇄ "));
});

test("a failed, incomplete or absent preflight never withdraws Start", () => {
  // Every shape the old poll could produce mid-transient — including the real three-blocker
  // payload — is now just a note, or nothing at all.
  const s = S({ state: "READY", can_start: true });
  const transient = preflight({
    can_start: false, ok: false, blockers: READINESS_BLOCKERS,
    proof_complete: false, readiness_refreshing: true,
    readiness_reason_code: "READBACK_UNAVAILABLE",
    readiness_reason: "The Pixhawk mission read-back could not be obtained",
  });
  for (const pf of [null, undefined, preflight({ can_start: false, blockers: READINESS_BLOCKERS }),
    transient]) {
    const c = card(s, { preflight: preflightNote(pf) });
    assert.equal(startEnabled(c), true, JSON.stringify(pf));
    assert.equal(c.chip, "READY", JSON.stringify(pf));
  }
  // …and the transient is reported as "could not be checked", never as a vehicle failure.
  const note = preflightNote(transient);
  assert.equal(note.ok, null);
  assert.match(note.reason, /could not be (obtained|checked)/i);
  assert.equal(preflightNote(preflight({ can_start: true })).ok, true);
  assert.equal(preflightNote(preflight({ can_start: false, blockers: ["x"] })).ok, false);
  assert.equal(preflightNote(null), null);
  assert.equal(preflightNote({ reachable: false }), null);
});

// ── C. The stable Start gate ────────────────────────────────────────────────────────────
test("a connected Scout with an active mission in a resting state offers Start", () => {
  for (const state of ["READY", "NOT_READY", "NOT_STARTED", "STOPPED", "CANCELLED"]) {
    const gate = startGate(S({ state, can_start: state === "READY" }));
    assert.equal(gate.canStart, true, state);
    assert.equal(gate.code, null, state);
  }
});

test("Scout's own can_start:false does not by itself withhold Start", () => {
  // Scout reports can_start:false whenever authority is not yet LOCAL_AGENT — the very condition
  // the Start transaction resolves as its first phase. The backend applies the same single
  // deferral (mission_lifecycle.start_eligibility) and Scout arbitrates the Start itself.
  const gate = startGate(S({ state: "NOT_READY", can_start: false, authority_status: "OPERATOR" }));
  assert.equal(gate.canStart, true);
});

test("each STABLE blocker disables Start with its own code", () => {
  const cases = [
    [START_BLOCK.DISCONNECTED, S({ state: "READY", can_start: true }), { connected: false }],
    [START_BLOCK.BUSY, S({ state: "READY", can_start: true }), { busy: true }],
    [START_BLOCK.UNSUPPORTED, normalizeStatus({ supported: false }), {}],
    [START_BLOCK.UNSUPPORTED, S({ mission_execution_enabled: false }), {}],
    [START_BLOCK.STATUS_UNAVAILABLE, normalizeStatus({ reachable: false, scout: {} }), {}],
    [START_BLOCK.REPLANNING, S({ replanning: { active: true, fsm_state: "PLANNING" } }), {}],
    [START_BLOCK.OPERATION_ACTIVE, S({ state: "READY", active_operation_id: "op-77" }), {}],
    [START_BLOCK.OPERATION_ACTIVE, S({ state: "STARTING_AUTO" }), {}],
    [START_BLOCK.ALREADY_RUNNING, S({ state: "RUNNING", can_pause: true }), {}],
    [START_BLOCK.ALREADY_RUNNING, S({ state: "PAUSED", can_resume: true }), {}],
    [START_BLOCK.ALREADY_RUNNING, S({ state: "RETURNING_HOME" }), {}],
    [START_BLOCK.REARM_REQUIRED, S({ state: "COMPLETED_HOLD" }), {}],
    [START_BLOCK.REARM_REQUIRED, S({ state: "FAILED" }), {}],
    [START_BLOCK.REARM_REQUIRED, S({ state: "SUSPENDED" }), {}],
    [START_BLOCK.NO_MISSION, S({ state: "READY", can_start: true, mission_id: null }), {}],
  ];
  for (const [code, status, opts] of cases) {
    const gate = startGate(status, opts);
    assert.equal(gate.canStart, false, code);
    assert.equal(gate.code, code, `${code} — got ${gate.code}`);
    assert.ok(gate.reason && gate.reason.length, code);
  }
});

test("EXPLICIT replan.active disables Start; nothing else called 'replanning' does", () => {
  assert.equal(startGate(S({ replanning: { active: true } })).code, START_BLOCK.REPLANNING);
  assert.equal(startGate(S({ effective_state: EFFECTIVE_REPLANNING })).code,
    START_BLOCK.REPLANNING);
  for (const st of FSM_ACTIVE_ORDER) {
    assert.equal(startGate(S({ replanning: { active: false, fsm_state: st } })).code,
      START_BLOCK.REPLANNING, st);
  }
  // A readiness/readback refresh, a pending request, NOT_READY, staleness and a busy flag are
  // NOT replanning and must not touch Start.
  for (const over of [{ readiness: { refreshing: true } }, { readiness_refresh_inflight: true },
    { readback_refreshing: true }, { status_request_pending: true }, { evidence_stale: true },
    { busy: true, inflight: true }, { replanning: { active: false, fsm_state: "MONITORING" } }]) {
    const gate = startGate(S(over));
    assert.equal(gate.canStart, true, JSON.stringify(over));
  }
});

test("the gate never consults a preflight, a readback or Home verification", () => {
  // A source guard on the DERIVATION itself. Comments and operator-facing strings are stripped
  // first — the gate is allowed to describe the fresh proof the Start transaction performs; what
  // it may not do is branch on any short-lived evidence.
  const lib = read("../operator/lib/mission-execution.js");
  const code = lib.slice(lib.indexOf("export function startGate"),
    lib.indexOf("export function stopAvailability"))
    .replace(/\/\/[^\n]*/g, "")
    .replace(/`(?:[^`\\]|\\.)*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'/g, '""');
  for (const forbidden of ["preflight", "readback", "read_back", "proof", "refresh", "stale",
    "S\\.canStart"]) {
    assert.doesNotMatch(code, new RegExp(forbidden, "i"), forbidden);
  }
  // The only Home reference it may make is Scout's EXPLICIT pre-start requirement.
  assert.match(code, /S\.home\.requiredBeforeStart/);
  assert.equal((code.match(/S\.home\./g) || []).length, 2, "requiredBeforeStart + its verified check");
});

// ── D. Home: the Start transaction owns it ──────────────────────────────────────────────
test("an unverified Home does not disable Start — it says Start will set it", () => {
  const s = S({ state: "READY", can_start: true });
  const c = card(s, { homeVerified: false });
  assert.equal(startEnabled(c), true);
  assert.ok(c.home);
  assert.equal(c.home.text, HOME_DURING_START_NOTE);
  assert.equal(c.home.tone, null, "a step that has not happened yet is not a warning");
  assert.match(c.home.title, /sets the current launch position as Home|launch position/i);
  // A verified Home says nothing at all — no second Home readout in this card.
  assert.equal(card(s, { homeVerified: true }).home, null);
});

test("Home withholds Start ONLY when Scout explicitly requires a pre-existing verified one", () => {
  const declared = S({ state: "READY", can_start: true, requires_verified_home: true });
  assert.equal(startGate(declared).code, START_BLOCK.HOME_REQUIRED);
  const c = card(declared, { homeVerified: false });
  assert.equal(startEnabled(c), false);
  assert.equal(c.home.tone, "warn");
  // With the Home actually verified, the declaration is satisfied.
  assert.equal(startGate(S({ state: "READY", can_start: true, requires_verified_home: true,
    verified_home: { lat: 1, lng: 2 } })).canStart, true);
  // A merely ABSENT verified_home block never implies the requirement.
  assert.equal(startGate(S({ state: "READY", can_start: true })).canStart, true);
});

// ── E. The Start transaction's phases, and its one compact failure ──────────────────────
test("a running Start shows phase-specific NEUTRAL status, never a warning", () => {
  const expected = [
    [null, "Checking mission readiness…"],
    ["READY", "Checking mission readiness…"],
    ["START_REQUESTED", "Taking agent control…"],
    ["START_HOLD_REQUESTED", "Holding position…"],
    ["START_HOLD_CONFIRMED", "Holding position…"],
    ["SETTING_HOME", "Setting and verifying Home…"],
    ["VERIFYING_HOME", "Setting and verifying Home…"],
    ["SYNCHRONIZING_PACKAGE", "Starting AUTO…"],
    ["STARTING_AUTO", "Starting AUTO…"],
  ];
  for (const [state, text] of expected) {
    assert.equal(startPhase(state).text, text, String(state));
    const c = card(S({ state: state || "READY", can_start: false }), { starting: true, busy: true });
    assert.equal(c.working, true, String(state));
    assert.equal(c.headline, text, String(state));
    assert.equal(claimsReplanning(c), false, says(c));
    assert.equal(c.blocker, null, "a Start in progress is not a blocker");
  }
  // The five phrases the task names, and no sixth.
  assert.deepEqual(START_PHASES.map((p) => START_PHASE_TEXT[p]),
    ["Checking mission readiness…", "Taking agent control…", "Holding position…",
      "Setting and verifying Home…", "Starting AUTO…"]);
});

test("a failed Start preflight reads as ONE compact actionable error", () => {
  // The real backend refusal: `blocked`, three blockers, the read-back at the head of the chain.
  const view = interpretTransaction({ status: 409, data: {
    outcome: "blocked", operation: "start", error_code: "START_PRECONDITIONS_NOT_MET",
    error: "Start preconditions are not met: …",
    blockers: [
      "Pixhawk readback hash match: Pixhawk read-back is unreachable — the route on the flight " +
        "controller cannot be confirmed",
      "Planning package stored, usable and consistent: not consistent",
      "Scout replanning readiness: Scout does not report replanning readiness for this mission",
    ],
    phases: [{ phase: "preconditions", status: "failed", detail: "…" }],
    authority: { before: "OPERATOR", after: null, required: "LOCAL_AGENT", verified: null },
  } });
  const fail = startFailure(view);
  assert.equal(fail.title, START_FAILURE_TITLE);
  assert.equal(fail.text, "Pixhawk mission readback could not be verified.");
  assert.equal(fail.blocked, true);
  // ONE line. The competing warnings live in the tooltip, in full.
  assert.equal(fail.text.split("\n").length, 1);
  assert.ok(fail.text.length <= 70, fail.text);
  for (const b of view.blockers) assert.ok(fail.detail.includes(b), b);
  assert.match(fail.detail, /no vehicle write was issued/i);
  // Nothing is reported for a Start that succeeded or is still running.
  assert.equal(startFailure({ outcome: OUTCOME.ACCEPTED }), null);
  assert.equal(startFailure({ outcome: "pending" }), null);
  assert.equal(startFailure(null), null);
});

test("other Start failures each get their own single sentence, never object coercion", () => {
  const of = (data) => startFailure(interpretTransaction({ status: 409, data })).text;
  assert.equal(of({ outcome: "blocked", blockers: ["Planning package stored, usable and " +
    "consistent: not stored"] }), "The planning package is not consistent with the approved mission.");
  assert.equal(of({ outcome: "blocked", blockers: ["Mission record VERIFIED: No active mission " +
    "record"] }), "The mission upload is not verified.");
  assert.equal(of({ outcome: "blocked", error_code: "AUTHORITY_NOT_VERIFIED" }),
    "Control authority could not be verified.");
  assert.equal(of({ outcome: "failed", error_code: "LOITER_NOT_VERIFIED" }),
    "The launch LOITER could not be verified.");
  const structured = startFailure(interpretTransaction({ status: 200, data: {
    outcome: "failed", error: { code: "SET_HOME_FAILED", message: "no ack from Pixhawk" } } }));
  assert.doesNotMatch(`${structured.text} ${structured.detail}`, /\[object Object\]/);
});

test("a failed Start leaves the lifecycle resting and offers Start again", () => {
  // Fail-closed means nothing moved: Scout is still resting, so the card still offers a Start.
  const c = card(S({ state: "READY", can_start: true }));
  assert.equal(startEnabled(c), true);
  assert.equal(c.working, false);
});

// ── F. "Agent is replanning" requires EXPLICIT evidence ─────────────────────────────────
test("every passive refresh / in-flight flag a payload might carry fails to NOT replanning", () => {
  const noise = [
    { readiness: { refreshing: true } },
    { readiness_refresh_inflight: true },
    { mission_readback: { refreshing: true } },
    { readback_refreshing: true },
    { status_request_pending: true },
    { busy: true, inflight: true },
    { state: "NOT_READY", effective_state: "NOT_READY" },
    { replanning: { active: false, fsm_state: "MONITORING", refreshing: true } },
    { replanning: { active: false, fsm_state: "MONITORING_REVISED" } },
    { evidence_stale: true, replanning: { active: false, stale: true } },
  ];
  for (const over of noise) {
    const s = S(over);
    assert.equal(s.replanning.active, false, JSON.stringify(over));
    assert.equal(claimsReplanning(card(s)), false, JSON.stringify(over));
  }
});

test("state NOT_READY on its own never shows replanning", () => {
  const s = S({ state: "NOT_READY", effective_state: "NOT_READY", can_start: false });
  assert.equal(s.replanning.active, false);
  assert.equal(claimsReplanning(card(s)), false, says(card(s)));
});

test("missing, empty and malformed replan data all fail to NOT replanning", () => {
  for (const over of [{ replanning: undefined }, { replanning: null }, { replanning: {} },
    { replanning: "REPLANNING" }, { replanning: [] }, { replanning: { active: "true" } },
    { replanning: { active: 1 } }, { replanning: { fsm_state: null } }]) {
    const s = S(over);
    assert.equal(s.replanning.active, false, JSON.stringify(over));
    assert.equal(claimsReplanning(card(s)), false, JSON.stringify(over));
  }
  // The whole object's TRUTHINESS is never the test — a present replanning block is not a replan.
  assert.equal(isReplanning({ active: false, fsm_state: "MONITORING" }), false);
  assert.equal(isReplanning({}), false);
  assert.equal(isReplanning(undefined), false);
  assert.equal(isReplanning(null, null), false);
});

test("replan.active=true DOES show replanning", () => {
  const s = S({ replanning: { active: true, fsm_state: "PLANNING" } });
  assert.equal(s.replanning.active, true);
  assert.equal(card(s).headline, "Agent is replanning");
  assert.equal(isReplanning({ active: true }), true);
});

test("an explicit replan transaction state shows replanning, an idle one does not", () => {
  for (const st of FSM_ACTIVE_ORDER) {
    assert.equal(isReplanning({ active: false, state: st }), true, st);
    assert.equal(isReplanning({ active: false, fsm_state: st }), true, st);
    assert.equal(card(S({ replanning: { active: false, fsm_state: st } })).headline,
      "Agent is replanning", st);
  }
  for (const st of FSM_IDLE) assert.equal(isReplanning({ active: false, state: st }), false, st);
  assert.equal(isReplanning({}, EFFECTIVE_REPLANNING), true);
  assert.equal(card(S({ effective_state: EFFECTIVE_REPLANNING })).headline, "Agent is replanning");
  assert.deepEqual([...EXPLICIT_REPLAN_STATES].sort(),
    [EFFECTIVE_REPLANNING, ...FSM_ACTIVE_ORDER].sort());
});

test("the preflight's 'Scout replanning readiness' blocker is NOT reported as an active replan", () => {
  const reason = READINESS_BLOCKERS.join(" · ");
  assert.notEqual(shortStartBlocker(reason), "Agent is replanning");
  assert.equal(shortStartBlocker(reason), "Replanning readiness not confirmed");
  // …and it now reaches the card only through the INFORMATIONAL note, which cannot gate anything.
  const c = card(S({ state: "READY", can_start: true }), {
    preflight: preflightNote(preflight({ can_start: false, blockers: READINESS_BLOCKERS })) });
  assert.equal(startEnabled(c), true);
  assert.notEqual(c.headline, "Agent is replanning");
});

// ── G. The chip, the headline and the button remain ONE derivation ──────────────────────
test("the pre-start chip answers the same question the Start button does", () => {
  for (const state of READINESS_CHIP_STATES) {
    const ready = card(S({ state, can_start: state === "READY" }));
    assert.equal(ready.chip, "READY", state);
    assert.equal(ready.headline, "Ready to start", state);
    assert.equal(startEnabled(ready), true, state);

    const blocked = card(S({ state, mission_id: null }));
    assert.equal(blocked.chip, "NOT_READY", state);
    assert.equal(startEnabled(blocked), false, state);
    assert.equal(blocked.blocker.text, "No active mission", state);
    // Scout's own state is never hidden — it moves to the tooltip.
    assert.match(blocked.headlineTitle, new RegExp(`Scout reports ${state}`), state);
  }
});

test("READINESS has exactly two values — there is no perpetual 'checking' verdict", () => {
  assert.deepEqual(Object.keys(READINESS).sort(), ["NOT_READY", "READY"]);
});
