// "[object Object]" must never reach the operator.
//
// During the bench run the Agent page printed the literal text `[object Object]` in two places an
// operator reads to decide what to do — Current Policy and Last error. That is worse than a
// blank: it looks like a value, it occupies the slot where a reason belongs, and it says nothing
// about a vehicle that may be moving.
//
// The cause is always the same: `${value}` (or String(value)) on a STRUCTURED value. Scout
// legitimately sends structured values — a communication policy as `{value, source}`, an error as
// `{code, message}`, an energy calculation, a nested diagnostic — so the fix is to FORMAT them
// (lib/format.js asText), never to stringify or hide them.
//
// These tests come at it from both sides: the SOURCE of the Agent page (no raw interpolation of a
// field Scout sends structured) and the BEHAVIOUR of every derivation that feeds it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { asText, textOr, esc } from "../operator/lib/format.js";
import {
  normalizeStatus, startBlockers, transactionSummary, operationSummary, missionCardView,
  primaryAction, startFailure, lifecycleControls, errorText,
} from "../operator/lib/mission-execution.js";
import { normalizeReplanStatus, triggerLatch, cooldownView } from "../operator/lib/replan.js";

const here = dirname(fileURLToPath(import.meta.url));
const agentSrc = readFileSync(join(here, "..", "operator", "pages", "Agent.js"), "utf8");

const BAD = "[object Object]";

/** Recursively assert nothing anywhere in a rendered structure is the coercion string. */
function assertNoObjectObject(value, where) {
  if (value === null || value === undefined) return;
  if (typeof value === "string") {
    assert.equal(value.includes(BAD), false, `${where}: ${value}`);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((v, i) => assertNoObjectObject(v, `${where}[${i}]`));
    return;
  }
  if (typeof value === "object") {
    for (const [k, v] of Object.entries(value)) assertNoObjectObject(v, `${where}.${k}`);
  }
}

// The shapes Scout actually sends structured, and that were being coerced.
const STRUCTURED = {
  policy: { value: "REDUCED_REPORTING", source: "COMMS_DEGRADED" },
  error: { code: "PACKAGE_SYNC_FAILED", message: "upload rejected after 3 attempts" },
  codeOnly: { code: "NO_PLANNING_PACKAGE" },
  messageOnly: { message: "the link dropped mid-transaction" },
  energy: { margin_percent: -5, reserve_wh: 12.5 },
  nested: { detail: { reason: "battery below reserve" }, code: "ENERGY_LOW" },
  bare: { a: 1, b: true },
};

// ════════════════════════════════════════════════════════════════════════════════════════
// A. The formatter itself
// ════════════════════════════════════════════════════════════════════════════════════════
test("asText renders every structured Scout shape as readable text", () => {
  for (const [name, value] of Object.entries(STRUCTURED)) {
    const t = asText(value);
    assert.equal(typeof t, "string", name);
    assert.equal(t.includes(BAD), false, `${name}: ${t}`);
    assert.ok(t.length > 0, name);
  }
  assert.equal(asText(STRUCTURED.error), "PACKAGE_SYNC_FAILED — upload rejected after 3 attempts");
  assert.equal(asText(STRUCTURED.codeOnly), "NO_PLANNING_PACKAGE");
  assert.equal(asText(STRUCTURED.messageOnly), "the link dropped mid-transaction");
  // A value with no human field is shown as its own content, never dropped and never coerced.
  assert.match(asText(STRUCTURED.energy), /margin_percent=-5/);
});

test("nothing is lost: an object with no message still reports its fields", () => {
  assert.match(asText(STRUCTURED.bare), /a=1/);
  assert.match(asText(STRUCTURED.bare), /b=yes/);
});

test("absence stays absent — the caller renders its own dash, never the word null", () => {
  assert.equal(asText(null), null);
  assert.equal(asText(undefined), null);
  assert.equal(asText(""), null);
  assert.equal(textOr(null), "—");
});

test("esc goes through asText, so an escaped object is text and not a coercion", () => {
  assert.equal(esc(STRUCTURED.error).includes(BAD), false);
  assert.match(esc(STRUCTURED.error), /PACKAGE_SYNC_FAILED/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// B. Every derivation the Agent page renders from
// ════════════════════════════════════════════════════════════════════════════════════════
test("mission-execution derivations never coerce a structured last_error", () => {
  const s = normalizeStatus({ supported: true, reachable: true, scout: {
    state: "SUSPENDED", can_start: false, last_error: STRUCTURED.error,
    start_eligible: false, start_block_reason: STRUCTURED.codeOnly.code,
    battery_diagnostics: { battery_valid: false, battery_raw: -1 },
    binding: { binding_state: "STALE_MISMATCH" } } });

  assertNoObjectObject(startBlockers(s), "startBlockers");
  assertNoObjectObject(primaryAction(s), "primaryAction");
  assertNoObjectObject(lifecycleControls(s, {}), "lifecycleControls");
  assertNoObjectObject(missionCardView(s, {}), "missionCardView");
  // The failure sentence itself carries Scout's message, not its type.
  assert.match(primaryAction(s).reason, /upload rejected after 3 attempts/);
});

test("transaction and operation summaries carry structured errors as text", () => {
  const view = {
    outcome: "failed", code: "SET_HOME_FAILED", message: asText(STRUCTURED.error),
    resultingState: "FAILED", reconciliation: { resolved: "failed", detail: "no verdict" },
    phases: [],
  };
  assertNoObjectObject(transactionSummary(view), "transactionSummary");
  assertNoObjectObject(operationSummary(view), "operationSummary");
});

test("a start failure built from structured blockers is readable", () => {
  const fail = startFailure({ outcome: "blocked", code: "START_PRECONDITIONS_NOT_MET",
    message: STRUCTURED.messageOnly, blockers: [STRUCTURED.error, STRUCTURED.codeOnly] });
  assertNoObjectObject(fail, "startFailure");
  assert.match(fail.detail, /PACKAGE_SYNC_FAILED/);
});

test("errorText never returns a coercion for an unknown code", () => {
  assert.equal(errorText("SOMETHING_NEW"), "SOMETHING_NEW");
  assert.equal(errorText(null), null);
});

test("replan derivations never coerce a structured last_error or terminal reason", () => {
  const st = normalizeReplanStatus({ supported: true, reachable: true, scout: {
    fsm_state: "SAFE_HOLD", last_error: STRUCTURED.error, cooldown_s: 30,
    energy_calculation: STRUCTURED.energy, current_policy: STRUCTURED.policy,
    trigger_active: true, trigger_generation: 2, consumed_trigger_generation: 2,
    terminal_reason: "SAFE_HOLD" } });
  assertNoObjectObject(triggerLatch(st), "triggerLatch");
  assertNoObjectObject(cooldownView(st), "cooldownView");
  // The raw structured values are PRESERVED on the normalized model (they are rendered through
  // asText at the point of display) — this is about what reaches the DOM, not about dropping data.
  assert.deepEqual(st.transaction.lastError, STRUCTURED.error);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// C. The Agent page source — no raw interpolation of a field Scout sends structured
// ════════════════════════════════════════════════════════════════════════════════════════
test("the page's clean() goes through asText, not String()", () => {
  const fn = agentSrc.slice(agentSrc.indexOf("function clean("),
    agentSrc.indexOf("function num("));
  assert.match(fn, /asText\(/, "clean() must format structured values");
  assert.equal(/String\(v\)/.test(fn), false, "String(v) is what produced [object Object]");
});

test("the fields that printed [object Object] are no longer interpolated raw", () => {
  // Each of these was a real occurrence. `${t.lastError}` was the Last error row; `${f}` the
  // Current Policy flags; `${r.tx}` the decision reasons; `${decision}` the headline.
  for (const raw of ["${t.lastError}", "${vm.mission_id}", "<span class=\"rtx\">${f}</span>",
    "<span class=\"rtx\">${r.tx}</span>"]) {
    assert.equal(agentSrc.includes(raw), false, `raw interpolation still present: ${raw}`);
  }
});

test("the structured Scout fields are rendered through a formatter", () => {
  // Current Policy, Last error, decision reasons and policy flags all go through val()/esc(),
  // both of which route through asText.
  assert.match(agentSrc, /row\("Last error", asText\(t\.lastError\)/);
  assert.match(agentSrc, /policy_flags\s*\)\s*\n?\s*\?\s*a\.policy_flags\.map\(\(f\) => asText\(f\)\)/);
  assert.match(agentSrc, /const val = \(v\) => \{[\s\S]{0,120}asText\(v\)/);
});

test("the page imports the formatter it depends on", () => {
  assert.match(agentSrc, /import \{ asText, esc, escAttr \} from "\.\.\/lib\/format\.js"/);
});

// ════════════════════════════════════════════════════════════════════════════════════════
// D. The three state domains are labelled, and IDLE never means "no mission"
// ════════════════════════════════════════════════════════════════════════════════════════
test("the page names the three independent state domains explicitly", () => {
  for (const label of ["Supervisory decision engine", "Mission execution lifecycle",
    "Replanning lifecycle"]) {
    assert.ok(agentSrc.includes(label), `missing domain label: ${label}`);
  }
});

test("mission-execution state is shown in Current Situation as its own row", () => {
  assert.match(agentSrc, /row\("Mission execution \(Scout\)"/);
  assert.match(agentSrc, /row\("Vehicle mission state \(telemetry\)"/);
});

test("a supervisory 'no mission' claim beside a live mission raises the contradiction note", () => {
  assert.match(agentSrc, /claimsNoMission/);
  assert.match(agentSrc, /const contradiction = claimsNoMission && mxLive/);
  // The note must say which subsystem answers the question, not merely that they differ.
  assert.match(agentSrc, /The mission IS running/);
});
