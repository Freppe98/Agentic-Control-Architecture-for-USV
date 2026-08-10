// map-inspector.test.mjs — the Map's right-panel INFORMATION ARCHITECTURE.
//
// The defect this pins: the inspector was functionally correct but said the same thing in four
// places. Control authority appeared in the Status badges, again as a tab strip on Vehicle
// Readiness, again as a Control Owner card, again as a row in the Agent Mission card and again
// as a narrated sentence in Manual Control — and the Agent Mission card carried paragraphs
// explaining Pixhawk readback hashes, planning packages and why a Scout has no Stop endpoint.
// On an operational surface that is not thoroughness, it is noise the operator has to read
// past to reach the mode buttons.
//
// Two things must now be provably true:
//   1. ORDER — Status → Vehicle Commands → Agent Mission → Vehicle Readiness. The primary
//      immediate manual controls sit directly under Status; supervising the agent comes after.
//   2. ONE PLACE PER FACT — authority is displayed only in the Status area, and every long
//      explanation lives in a `title` tooltip (and on the Agent diagnostics page), never as
//      body text in the card.
//
// Nothing here weakens the safety model: authority is still in the data, still gates every
// write, and Take Control is still an explicit, always-reachable manual override.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  missionCardView, normalizeStatus, shortStartBlocker, shortMissionId, firstClause,
} from "../operator/lib/mission-execution.js";
import { deploymentReadiness } from "../operator/lib/home.js";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p) => readFileSync(join(here, p), "utf8");
const mapSrc = read("../operator/pages/Map.js");

// The inspector's own template — the region whose ORDER is the product decision.
const inspector = (() => {
  const from = mapSrc.indexOf("function renderInspector");
  assert.ok(from > 0, "renderInspector must exist");
  const start = mapSrc.indexOf("box.innerHTML = `", from);
  const end = mapSrc.indexOf("box.querySelectorAll", start);
  assert.ok(start > 0 && end > start, "the inspector template must be locatable");
  return mapSrc.slice(start, end);
})();
/** The body of one Map.js function, by its declaration text. */
// A named function's source. `len` is an UPPER BOUND only: the slice also stops at the
// function's own closing brace (the first `\n  }` at declaration indentation), so a guard reads
// exactly one function and never leaks into whatever is declared after it. Without that bound a
// guard silently starts asserting against the next declaration the moment a function grows.
const sliceOf = (decl, len) => {
  const i = mapSrc.indexOf(decl);
  assert.ok(i > 0, `${decl} must exist`);
  const end = mapSrc.indexOf("\n  }", i);
  return mapSrc.slice(i, Math.min(i + len, end === -1 ? mapSrc.length : end + 4));
};
/** Position of a section's title in the inspector template (−1 when absent). */
const at = (label) => inspector.indexOf(`<span class="lbl">${label}</span>`);

const envelope = (over = {}) => ({
  ok: true, supported: true, reachable: true,
  scout: {
    supported: true,
    state: "READY", effective_state: "READY", active_operation_id: null,
    mission_id: "msn-04fcc4a91f137", mode: "LOITER",
    sequence: { current: 0, count: 41 },
    replanning: { active: false, fsm_state: "MONITORING" },
    return_completion: { final_loiter_verified: false },
    authority_status: "LOCAL_AGENT",
    can_start: true, can_pause: false, can_resume: false,
    mission_execution_enabled: true, last_error: null,
    ...over,
  },
});
const S = (over) => normalizeStatus(envelope(over));
const actions = (card) => card.buttons.map((b) => b.action);

// ── A. Right-panel section order ────────────────────────────────────────────────────────
test("the inspector order is Status → Vehicle Commands → Agent Mission → Vehicle readiness", () => {
  const status = at("Status");
  const commands = at("Vehicle Commands");
  const agent = at("Agent Mission");
  const readiness = at("Vehicle readiness");
  for (const [name, i] of [["Status", status], ["Vehicle Commands", commands],
    ["Agent Mission", agent], ["Vehicle readiness", readiness]]) {
    assert.ok(i >= 0, `${name} section must be present in the inspector`);
  }
  assert.ok(status < commands,
    "Vehicle Commands must sit directly below Status — the primary immediate manual controls");
  assert.ok(commands < agent,
    "Agent Mission must come AFTER Vehicle Commands, never before it");
  assert.ok(agent < readiness, "Vehicle readiness follows Agent Mission");
});

test("the vehicle header is first and the secondary information stays last", () => {
  assert.ok(inspector.indexOf('class="idcard"') < at("Status"), "vehicle header is first");
  for (const later of ["Supervisory agent · decision state", "Communication · transitions",
    "Recent events"]) {
    assert.ok(at(later) > at("Vehicle readiness"), `${later} is secondary information`);
  }
});

test("nothing sits between Status and Vehicle Commands", () => {
  const between = inspector.slice(at("Status"), at("Vehicle Commands"));
  const otherTitles = [...between.matchAll(/<span class="lbl">([^<]+)<\/span>/g)]
    .map((m) => m[1]).filter((l) => l !== "Status");
  assert.deepEqual(otherTitles, [], `unexpected section(s) before Vehicle Commands: ${otherTitles}`);
});

// ── B. Authority is displayed exactly once, in the Status area ───────────────────────────
test("the Map renders the authority indicator only through the Status badges", () => {
  // AuthoritySeg is what draws RC | OPERATOR | LOCAL AGENT. StatusBadges owns it; the Map must
  // not import or place a second one (it used to sit in the Vehicle readiness title).
  assert.doesNotMatch(mapSrc, /^import \{ AuthoritySeg \}/m);
  assert.doesNotMatch(inspector, /AuthoritySeg\(/);
  assert.match(inspector, /StatusBadges\(v, authVal/);
  // …and the badges are in the Status section, not anywhere below it.
  assert.ok(inspector.indexOf("StatusBadges(") > at("Status"));
  assert.ok(inspector.indexOf("StatusBadges(") < at("Vehicle Commands"));
});

test("there is no Control Owner card in the normal expanded layout", () => {
  // Markup, not prose: the card's class and its interpolated label must both be gone.
  assert.doesNotMatch(mapSrc, /rdy-owner/);
  assert.doesNotMatch(mapSrc, /\$\{[^}]*controlOwner/);
  assert.doesNotMatch(inspector, /Control owner/i);
  // The readiness view returns items + banner only — nothing else.
  const rdy = sliceOf("function renderReadiness", 1800);
  assert.match(rdy, /VEHICLE READY/);
  assert.match(rdy, /VEHICLE NOT READY/);
  assert.match(rdy, /return `<div class="rdy">\$\{items\}<\/div>\$\{banner\}`;/);
});

test("Vehicle readiness has no authority tabs and no authority checklist item", () => {
  const title = inspector.slice(at("Vehicle readiness"), at("Vehicle readiness") + 260);
  assert.doesNotMatch(title, /AuthoritySeg|authVal, \{ phase/);
  // The underlying policy still scores only vehicle deployment evidence.
  const r = deploymentReadiness({ connected: true, gpsFresh: true, posValid: true,
    missionLoaded: true, homeVerified: true, authority: "LOCAL_AGENT" });
  assert.deepEqual(r.items.map((i) => i.label),
    ["Pixhawk connected", "GPS ready", "Mission loaded", "Home verified"]);
  assert.equal(r.ready, true, "authority is not a deployment-readiness input");
});

test("the readiness banner is decided by deployment evidence alone", () => {
  for (const authority of ["OPERATOR", "LOCAL_AGENT", "RC", null]) {
    const ok = deploymentReadiness({ connected: true, gpsFresh: true, posValid: true,
      missionLoaded: true, homeVerified: true, authority });
    const bad = deploymentReadiness({ connected: true, gpsFresh: true, posValid: true,
      missionLoaded: true, homeVerified: false, authority });
    assert.equal(ok.ready, true, `READY with authority=${authority}`);
    assert.equal(bad.ready, false, `NOT READY with authority=${authority}`);
  }
});

test("the authority narration is one concise line, and only beside Status", () => {
  assert.doesNotMatch(mapSrc, /function authNote\b/);
  assert.match(mapSrc, /function authStatusNote/);
  // Called exactly once, in the Status section.
  const calls = [...inspector.matchAll(/authStatusNote\(/g)];
  assert.equal(calls.length, 1);
  assert.ok(inspector.indexOf("authStatusNote(") < at("Vehicle Commands"));
  // A settled owner produces NOTHING: the badge already says who holds the wheel.
  const note = sliceOf("function authStatusNote", 2600);
  assert.doesNotMatch(note, /Operator holds control/);
  assert.doesNotMatch(note, /Local Agent holds control/);
  assert.doesNotMatch(note, /Authority confirmed/);
  // The unreachable/stale cases are marked, not repeated, and carry the detail as a tooltip.
  assert.match(note, /Authority unconfirmed/);
  assert.match(note, /Authority not current/);
  assert.match(note, /title="\$\{escAttr\(title \|\| ""\)\}"/);
});

test("the command lock note explains the cause without restating the owner", () => {
  const lock = sliceOf("function lockNote", 700);
  assert.match(lock, /Take Control to enable/);
  assert.doesNotMatch(lock, /av\.value === "OPERATOR"/);
});

// ── C. Take Control survives as the explicit manual override ────────────────────────────
test("Take Control is rendered with the manual commands, not in a card of its own", () => {
  assert.match(inspector, /takeControl\(av, stale, canTake\)/);
  const cmds = at("Vehicle Commands");
  assert.ok(inspector.indexOf("takeControl(") > cmds, "Take Control belongs to Vehicle Commands");
  assert.ok(inspector.indexOf("takeControl(") < at("Agent Mission"),
    "Take Control sits immediately below the manual commands");
  // The separate Manual Control section is gone.
  assert.equal(at("Manual control"), -1);
});

test("Take Control remains available when LOCAL_AGENT owns authority", () => {
  const fn = sliceOf("function takeControl", 1500);
  // Shown unless the operator ALREADY holds confirmed control — which includes LOCAL_AGENT.
  assert.match(fn, /if \(hasControl\) return "";/);
  assert.match(fn, /data-authority="OPERATOR"/);
  assert.match(fn, /Take Control</);
  // Never hidden merely because it cannot be pressed: disabled + a reason instead.
  assert.match(fn, /\$\{canTake \? "" : "disabled"\}/);
  // The lifecycle model likewise still offers it out of a failed/suspended run.
  const failed = missionCardView(S({ state: "FAILED", can_start: false,
    authority_status: "LOCAL_AGENT", last_error: "AUTO_NOT_VERIFIED" }));
  assert.ok(actions(failed).includes("take-control"));
  assert.equal(failed.buttons.find((b) => b.action === "take-control").enabled, true);
});

test("advanced authority controls stay, collapsed, and are not a prominent card", () => {
  assert.match(inspector, /<details class="adv-auth">/);
  assert.doesNotMatch(inspector, /<details class="adv-auth" open/);
  const adv = inspector.slice(inspector.indexOf('<details class="adv-auth">'));
  assert.match(adv, /data-authority="LOCAL_AGENT"/);      // Release Control preserved
  assert.match(adv, /Release Control/);
});

// ── D. The Agent Mission card is compact ────────────────────────────────────────────────
test("READY shows a chip, one short line, Mission + WP, and Start", () => {
  const card = missionCardView(S({ state: "READY", can_start: true }));
  assert.equal(card.chip, "READY");
  assert.equal(card.headline, "Ready to start");
  assert.deepEqual(card.rows.map((r) => [r.k, r.v]),
    [["Mission", "msn-04fcc4…f137"], ["WP", "0 / 41"]]);
  assert.deepEqual(card.buttons.map((b) => b.label), ["Start Mission"]);
  assert.equal(card.blocker, null);
});

test("RUNNING and PAUSED show MODE · WP as the one line, and drop the identity rows", () => {
  const running = missionCardView(S({ state: "RUNNING", mode: "AUTO", can_start: false,
    can_pause: true, can_stop: true, sequence: { current: 4, count: 41 } }));
  assert.equal(running.chip, "RUNNING");
  assert.equal(running.headline, "AUTO · WP 4 / 41");
  assert.deepEqual(running.rows, []);
  assert.deepEqual(running.buttons.map((b) => b.label), ["Pause Mission", "Stop Mission"]);

  const paused = missionCardView(S({ state: "PAUSED", mode: "LOITER", can_start: false,
    can_resume: true, can_stop: true, sequence: { current: 4, count: 41 } }));
  assert.equal(paused.chip, "PAUSED");
  assert.equal(paused.headline, "LOITER · WP 4 / 41");
  assert.deepEqual(paused.rows, []);
  assert.deepEqual(paused.buttons.map((b) => b.label), ["Resume Mission", "Stop Mission"]);
});

test("the card variants stay STATE-driven, never click-driven", () => {
  // The same assertion the lifecycle model carries, restated at the card level: a RUNNING
  // mission's primary control is Pause and can never read Resume.
  const running = missionCardView(S({ state: "RUNNING", can_pause: true, can_stop: true }));
  assert.equal(running.buttons[0].label, "Pause Mission");
  assert.equal(running.buttons.some((b) => /resume/i.test(b.label)), false);
  // Busy disables every button without changing a single label.
  const busy = missionCardView(S({ state: "RUNNING", can_pause: true, can_stop: true }),
    { busy: true });
  assert.deepEqual(busy.buttons.map((b) => b.label), running.buttons.map((b) => b.label));
  assert.equal(busy.buttons.every((b) => !b.enabled), true);
});

test("the Agent Mission card never displays authority as a detail row", () => {
  for (const authority of ["OPERATOR", "LOCAL_AGENT", "RC"]) {
    for (const state of ["READY", "RUNNING", "PAUSED", "FAILED", "COMPLETED_HOLD"]) {
      const card = missionCardView(S({ state, authority_status: authority, can_start: true,
        can_pause: true, can_resume: true, can_stop: true }));
      assert.equal(card.rows.some((r) => /authority/i.test(r.k)), false, `${state}/${authority}`);
      const rendered = [card.headline, ...card.rows.map((r) => `${r.k} ${r.v}`)].join(" ");
      assert.doesNotMatch(rendered, /\b(OPERATOR|LOCAL_AGENT|RC)\b/, `${state}/${authority}`);
    }
  }
  // …and the rendered card carries no Authority row / authority caption in the template.
  const render = sliceOf("function renderAgentMission", 8000);
  assert.doesNotMatch(render, /Authority/);
  assert.doesNotMatch(render, /authority handled/);
});

// ── E. One short blocker, with the evidence in the tooltip ──────────────────────────────
test("a blocked Start shows a concise line and keeps the full reason for the tooltip", () => {
  const full = "Pixhawk readback could not be confirmed and the planning package readiness " +
    "evidence is incomplete.";
  const card = missionCardView(S({ state: "READY", can_start: true }),
    { startBlocked: true, startBlockedReason: full });
  assert.equal(card.blocker.text, "Mission verification unavailable");
  assert.ok(card.blocker.text.length <= 44, card.blocker.text);
  assert.equal(card.blocker.title, full, "the detail is preserved for the title tooltip");
});

test("a DEFINITIVE stale package is never shortened to 'verification unavailable'", () => {
  // ROUTE_HASH_STALE is Scout's answer AFTER it made the comparison: the package it holds is
  // not the route on the flight controller. The generic /hash|package|verif/ rule shortened it
  // to "Mission verification unavailable" — a sentence that says the check could not be run —
  // and printed it beside the readiness line's definitive one. Captured live on usv-2
  // (2026-08-09) with the Agent holding the PREVIOUS mission's package.
  for (const code of ["ROUTE_HASH_STALE", "PLANNING_PACKAGE_STALE"]) {
    const text = shortStartBlocker(code);
    assert.equal(text, "Agent planning package is stale", code);
    assert.doesNotMatch(text, /unavailable|unknown|could not/i, code);
    assert.ok(text.length <= 44, text);
  }
  assert.equal(shortStartBlocker("PLANNING_PACKAGE_MISSING"), "Agent planning package missing");
  assert.equal(shortStartBlocker("PLANNING_PACKAGE_UNUSABLE"), "Agent planning package unusable");
});

test("evidence that genuinely could not be obtained still reads as unavailable", () => {
  // The other half of the same rule: the wording is reserved for it, not abolished.
  assert.equal(shortStartBlocker("The Pixhawk readback could not be verified"),
               "Mission verification unavailable");
});

test("a stale-package blocker keeps Scout's own code in the tooltip", () => {
  const card = missionCardView(S({ state: "READY", can_start: true }),
    { startBlocked: true, startBlockedReason: "ROUTE_HASH_STALE" });
  assert.equal(card.blocker.text, "Agent planning package is stale");
  assert.equal(card.blocker.title, "ROUTE_HASH_STALE");
});

test("an unavailable Scout status reads STATUS UNAVAILABLE with a short tooltip", () => {
  const card = missionCardView(normalizeStatus({ reachable: false, scout: {} }), {
    unavailableDetail: "Mission lifecycle status could not be read from Scout Local Agent " +
      "port 8090.",
  });
  assert.equal(card.chip, "STATUS UNAVAILABLE");
  assert.equal(card.headline, "Waiting for Scout mission status");
  assert.match(card.headlineTitle, /port 8090/);
  assert.deepEqual(card.buttons, []);
  assert.deepEqual(card.rows, []);
});

test("a Stop Scout refuses right now reads as ONE short line, with its reason on hover", () => {
  const card = missionCardView(S({ state: "RUNNING", can_start: false, can_pause: true,
    can_stop: false }));
  // Stop is still SHOWN, disabled — never hidden, so the operator can see WHY it is unavailable.
  const stop = card.buttons.find((b) => b.action === "stop");
  assert.ok(stop);
  assert.equal(stop.enabled, false);
  assert.equal(card.blocker.text, "Scout reports can_stop=false in RUNNING");
  assert.match(card.blocker.title, /can_stop=false/);
});

test("only ONE blocker is ever shown, even when several things are wrong at once", () => {
  const card = missionCardView(S({ state: "FAILED", can_start: false, can_stop: false,
    last_error: { code: "PACKAGE_SYNC_FAILED", message: "upload rejected after 3 attempts" } }),
    { startBlocked: true, startBlockedReason: "Planning package: not stored" });
  assert.ok(card.blocker, "a failure must be reported");
  assert.equal(typeof card.blocker.text, "string");
  assert.equal(card.blocker.text, "PACKAGE_SYNC_FAILED");
  assert.match(card.blocker.title, /upload rejected after 3 attempts/);
});

test("no blocker is shown while an operation of ours is in flight", () => {
  const card = missionCardView(S({ state: "READY", can_start: true }),
    { busy: true, startBlocked: true, startBlockedReason: "Planning package: not stored" });
  assert.equal(card.blocker, null, "the card already shows the operation it is waiting on");
});

test("the card body carries no paragraph — every long explanation is a title tooltip", () => {
  // The bound is an UPPER limit; sliceOf still stops at the function's own closing brace, so
  // raising it as the card grows keeps the guard reading exactly one function. (It was 8000
  // until the card gained its completion, replacement-conflict, authority and battery slots,
  // 14000 until the ENERGY/RISK/ADVICE live rows gained their contract commentary, and 17000
  // until the Full Refresh note gained its reprove-outcome headline mapping.)
  const render = sliceOf("function renderAgentMission", 20000);
  // Every text slot the card renders must be paired with a title attribute.
  for (const pair of [/class="amx-h" title=/, /class="amx-note\$\{[\s\S]*?\}" title=/,
    /class="amx-result [\s\S]*?" title=/,
    // The slots added for the new Scout contract carry their evidence the same way.
    /class="amx-note warn" title=/]) {
    assert.match(render, pair, String(pair));
  }
  // Every `<div class="amx-note…` opened in this card is followed by a title attribute before
  // its tag closes. Counted rather than pattern-matched per slot, so a new note cannot be added
  // without its tooltip. (Scanning up to the tag's `>` avoids tripping over the quotes inside a
  // `${…}` class expression.)
  const notes = [...render.matchAll(/<div class="amx-note/g)];
  assert.ok(notes.length >= 4, `expected several note slots, found ${notes.length}`);
  for (const m of notes) {
    const tag = render.slice(m.index, render.indexOf(">", m.index));
    assert.ok(tag.includes(" title="), `an amx-note without a tooltip: ${tag.slice(0, 80)}`);
  }
  // The old paragraph notes are gone: no notice/failure/blocked/stop essays stacked in the body.
  for (const gone of ["const notice =", "const failure =", "const blockedNote =",
    "const stopNote ="]) {
    assert.equal(render.includes(gone), false, gone);
  }
  // The transaction outcome is a word, not the full summary line.
  assert.match(render, /mx\.outcomeLabel\(res\.view\.outcome\)/);
  assert.match(render, /escAttr\([^)]*mx\.transactionSummary\(res\.view\)\)/);
});

test("shortStartBlocker reports an unrecognised reason honestly rather than replacing it", () => {
  assert.equal(shortStartBlocker("Scout reports NOT_READY"), "Scout reports NOT_READY");
  assert.equal(shortStartBlocker(null), "Start preconditions not met");
  assert.equal(shortStartBlocker("Scout has no active mission"), "No active mission");
  assert.equal(shortStartBlocker({ code: "AUTHORITY_NOT_VERIFIED", message: "not verified" }),
    "Control authority not verified");
  // Long prose is capped, never printed whole.
  const long = shortStartBlocker("a".repeat(200));
  assert.ok(long.length <= 44, long);
});

test("firstClause and shortMissionId shorten without inventing", () => {
  assert.equal(firstClause("AUTO_NOT_VERIFIED — mode never read back as AUTO"),
    "AUTO_NOT_VERIFIED");
  assert.equal(firstClause(null), null);
  assert.equal(shortMissionId("msn-04fcc4a91f137"), "msn-04fcc4…f137");
  assert.equal(shortMissionId("msn-short"), "msn-short");
  assert.equal(shortMissionId(null), null);
});

// ── Full Refresh note: reprove-outcome wording (task Sections 12/13) ────────────────────
// binding_state=BOUND means a LIVE execution owns the mission identity — it is NOT a route-proof
// signal, so the note's headline must never call a healthy idle UNBOUND a failure, and must
// summarize Scout's own reprove-outcome word the way the task specifies, never the generic
// "binding failed" the old implementation would have shown.
test("the Full Refresh note never says 'binding failed' for a healthy idle mission", () => {
  const render = sliceOf("function renderAgentMission", 20000);
  assert.doesNotMatch(render, /[Bb]inding failed/);
  assert.doesNotMatch(render, /UNBOUND.*fail/i);
});

test("the Full Refresh note headline covers REPROVED, ALREADY_PROVEN, and the fail-closed / " +
  "incomplete outcomes", () => {
  const render = sliceOf("function renderAgentMission", 20000);
  // Successful idle proof and its idempotent repeat (task Section 13).
  assert.match(render, /route verified and ready/);
  assert.match(render, /current proof already valid/);
  // A genuine mismatch — reclassified through the Operator's own reconciliation (Section 9),
  // never Scout's raw PACKAGE_MISMATCH word displayed as the final system classification.
  assert.match(render, /planning package synchronization required/);
  assert.match(render, /flight-controller route does not match the approved mission/);
  assert.match(render, /MISSION_ID_MISMATCH/);
  // Inconclusive rounds (BUSY, EVIDENCE_UNAVAILABLE, …) read as incomplete, never a mismatch.
  assert.match(render, /already in progress on Scout/);
  assert.match(render, /mission evidence unavailable/);
  // The raw binding word is diagnostic detail only — kept in the tooltip, not the headline.
  assert.match(render, /const bindingDetail =/);
});

// ── F. Safety and gating are untouched by the layout change ─────────────────────────────
test("the manual command set is unchanged and still sits below Status", () => {
  assert.match(mapSrc, /\["SET_MODE_AUTO", "AUTO"\], \["SET_MODE_MANUAL", "MANUAL"\]/);
  assert.match(mapSrc, /\["SET_MODE_LOITER", "LOITER"\], \["RTL", "RTL"\]/);
  assert.match(mapSrc, /const MAP_SAFETY = \[\["ARM", "ARM"\], \["DISARM", "DISARM"\]\]/);
  assert.match(inspector, /vehicleCommands\(gateCtx, av, stale\)/);
});

test("the LOITER safety exemption and the authority write-gate are still in force", () => {
  // Layout may move; the gate may not. LOITER is never Home-gated, and every write still needs
  // a Scout-confirmed OPERATOR authority.
  const cmd = sliceOf("function cmdBtns", 1600);
  assert.match(cmd, /isSafetyHold\(type\)/);
  assert.match(cmd, /commandGate\(type, gateCtx\)/);
  assert.match(mapSrc, /hasControl: !stale && authCtl\.view\(\)\.hasControl/);
});

test("the Start authority orchestration is unchanged — one endpoint per intent", () => {
  const wiring = sliceOf("async function onMissionAction", 3000);
  assert.doesNotMatch(wiring, /setControlAuthority\([^)]*LOCAL_AGENT/);
  assert.match(mapSrc, /api\.startMissionExecution\(id, \{\}\)/);
  // The Start confirmation still tells the operator the transaction handles authority itself.
  assert.match(wiring, /transferred to the <b>Local Agent<\/b> and verified first/);
  // …and the button's own tooltip still carries the full transaction description.
  assert.match(mapSrc, /transfers control authority to the Local/);
});

// ── G. Structured values ────────────────────────────────────────────────────────────────
test("no card path can render [object Object]", () => {
  const card = missionCardView(S({
    state: "FAILED", can_start: false,
    mode: { value: "AUTO", source: "pixhawk" },
    last_error: { code: "PACKAGE_SYNC_FAILED", detail: { stage: "upload", attempts: 3 } },
  }), { startBlocked: true, startBlockedReason: { blockers: [{ label: "package" }] } });
  const rendered = [card.chip, card.headline, card.headlineTitle,
    ...card.rows.map((r) => `${r.k} ${r.v} ${r.title}`),
    card.blocker && `${card.blocker.text} ${card.blocker.title}`,
    ...card.buttons.map((b) => `${b.label} ${b.reason}`)].filter(Boolean).join(" ");
  assert.doesNotMatch(rendered, /\[object Object\]/, rendered);
});

test("the Map renders every dynamic value through the shared formatter", () => {
  assert.match(mapSrc, /import \{ asText, esc, escAttr \} from "\.\.\/lib\/format\.js"/);
  const render = sliceOf("function renderAgentMission", 8000);
  assert.doesNotMatch(render, /\$\{String\(/);
});
