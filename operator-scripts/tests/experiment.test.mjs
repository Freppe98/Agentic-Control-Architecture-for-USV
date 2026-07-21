// Unit + guard tests for the network-impairment Experiment page.
// Run: `node --test tests/` (or `npm test`).
//
// Two kinds, matching the rest of the suite: (1) pure-logic tests over
// operator/lib/experiment.js and the shared nav model (no DOM); (2) source guards
// (readFileSync) for wiring that has no DOM test infra here — nav registration, the
// route, the widened-rail tokens, the stop endpoint, reset-doesn't-apply, and the
// deliberate ABSENCE of a vehicle-authority gate on the experiment form.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { NAV, ICON, svgIcon } from "../operator/lib/ui.js";
import {
  DIRECTIONS, LIMITS, EXPERIMENT_DEFAULTS, defaultForm, validateExperiment, canApply,
  requiresConfirmation, impairmentFieldsActive, normalizePayload, normalizeDirection,
  experimentStatus, activeSummary, STATUS,
} from "../operator/lib/experiment.js";

const read = (p) => readFileSync(new URL(p, import.meta.url), "utf8");

// ---- 1. Experiment appears in navigation ----
test("Experiment is a primary nav item, placed just before Configuration", () => {
  const entry = NAV.find(([k]) => k === "experiment");
  assert.ok(entry, "NAV must contain an 'experiment' route");
  assert.equal(entry[1], "Experiment");
  const keys = NAV.map(([k]) => k);
  assert.ok(keys.indexOf("experiment") < keys.indexOf("config"), "Experiment sits before Configuration");
  assert.ok(keys.indexOf("experiment") > keys.indexOf("autonomy"), "…and after Agent");
  // it has a real icon, not an empty glyph
  assert.ok(ICON.experiment && ICON.experiment.length > 0, "experiment icon defined");
  assert.match(svgIcon("experiment"), /<svg[\s\S]*<path/, "svgIcon renders a path for experiment");
});

// ---- 2. Experiment route loads (registered in the router) ----
test("the router imports and maps the Experiment page", () => {
  const app = read("../operator/app.js");
  assert.match(app, /import\s*\{\s*Experiment\s*\}\s*from\s*"\.\/pages\/Experiment\.js"/);
  assert.match(app, /experiment:\s*Experiment/, "routes table maps experiment → Experiment");
});

// ---- 3. Sidebar width + icon sizing come from centralized tokens (and are ~50% larger) ----
test("nav pillar + icon sizes live in centralized tokens, widened ~50%", () => {
  const vars = read("../operator/styles/variables.css");
  const theme = read("../operator/styles/theme.css");
  const px = (re) => { const m = vars.match(re); assert.ok(m, `token ${re} defined`); return Number(m[1]); };
  const rail = px(/--rail-w:\s*(\d+)px/);
  const nav = px(/--nav-size:\s*(\d+)px/);
  const icon = px(/--nav-icon:\s*(\d+)px/);

  // ~50% wider/larger than the previous 52 / 40 / 20 baseline (allow a small tolerance)
  const ratio = (v, base) => v / base;
  assert.ok(ratio(rail, 52) >= 1.4 && ratio(rail, 52) <= 1.6, `rail-w ${rail}px ≈ +50% of 52`);
  assert.ok(ratio(nav, 40) >= 1.4 && ratio(nav, 40) <= 1.6, `nav-size ${nav}px ≈ +50% of 40`);
  assert.ok(ratio(icon, 20) >= 1.4 && ratio(icon, 20) <= 1.6, `nav-icon ${icon}px ≈ +50% of 20`);

  // the rail + icon dimensions must reference the tokens, never hardcode px
  assert.match(theme, /\.app\.no-dock\s*\{[^}]*var\(--rail-w\)/, "grid offset uses --rail-w");
  assert.match(theme, /\.nav\s*\{[^}]*width:\s*var\(--nav-size\)/, ".nav width uses --nav-size");
  assert.match(theme, /\.nav svg\s*\{[^}]*var\(--nav-icon\)/, ".nav svg uses --nav-icon");
  assert.doesNotMatch(theme, /\.nav svg\s*\{[^}]*\b20px\b/, ".nav svg must not hardcode the old 20px");
});

// ---- 4. Direction supports all three (asymmetric) values ----
test("Direction offers exactly the three api values", () => {
  assert.deepEqual(DIRECTIONS.map(([v]) => v), ["operator_to_scout", "scout_to_operator", "both"]);
  for (const v of ["operator_to_scout", "scout_to_operator", "both"]) {
    assert.equal(normalizeDirection(v), v);
  }
  assert.equal(normalizeDirection("nonsense"), null, "unknown direction is rejected");
});

// ---- 5. Invalid values disable Apply ----
test("out-of-range / negative values make the form invalid (Apply disabled)", () => {
  assert.equal(canApply(defaultForm()), true, "safe defaults are valid");
  assert.equal(canApply({ ...defaultForm(), latency_ms: -5 }), false, "negative latency");
  assert.equal(canApply({ ...defaultForm(), latency_ms: 20000 }), false, "latency over max");
  assert.equal(canApply({ ...defaultForm(), packet_loss_pct: 150 }), false, "loss over 100%");
  assert.equal(canApply({ ...defaultForm(), duration_s: 0 }), false, "duration below min");
  const r = validateExperiment({ ...defaultForm(), latency_ms: -5 });
  assert.equal(r.valid, false);
  assert.match(r.errors.latency_ms, /negative/i);
});

// ---- 6. Full disconnect disables the normal impairment fields ----
test("Full Disconnect makes the netem fields inactive (and dimmed leftovers don't block Apply)", () => {
  const on = { ...defaultForm(), full_disconnect: true };
  assert.equal(impairmentFieldsActive(on), false);
  assert.equal(impairmentFieldsActive(defaultForm()), true);
  // an invalid latency left over from before is dimmed → it must NOT block a disconnect run
  const withStaleInvalid = { ...on, latency_ms: -999 };
  assert.equal(canApply(withStaleInvalid), true, "dimmed netem field can't block Full Disconnect");
  // but duration is still active and still validated
  assert.equal(canApply({ ...on, duration_s: 0 }), false, "duration still gates Full Disconnect");
});

// ---- 7. Full disconnect requires explicit confirmation ----
test("Full Disconnect requires confirmation; a normal impairment does not", () => {
  assert.equal(requiresConfirmation({ ...defaultForm(), full_disconnect: true }), true);
  assert.equal(requiresConfirmation(defaultForm()), false);
  assert.equal(validateExperiment({ ...defaultForm(), full_disconnect: true }).requiresConfirmation, true);
});

// ---- 8. Apply request uses the correct normalized payload ----
test("normalizePayload produces the exact request body (numbers coerced, blank bandwidth → null)", () => {
  const form = {
    latency_ms: "500", jitter_ms: "100", packet_loss_pct: "10", bandwidth_kbit_s: "512",
    duplication_pct: 0, reordering_pct: 0, full_disconnect: false, direction: "both", duration_s: "60",
  };
  assert.deepEqual(normalizePayload(form, { vehicleId: 2 }), {
    vehicle_id: 2, latency_ms: 500, jitter_ms: 100, packet_loss_pct: 10, bandwidth_kbit_s: 512,
    duplication_pct: 0, reordering_pct: 0, full_disconnect: false, direction: "both", duration_s: 60,
  });
  // blank bandwidth → unlimited (null), missing vehicle → null
  const unlimited = normalizePayload({ ...defaultForm(), bandwidth_kbit_s: "" });
  assert.equal(unlimited.bandwidth_kbit_s, null);
  assert.equal(unlimited.vehicle_id, null);
});

// ---- 9. Backend-confirmed state controls the active badge (never optimistic) ----
test("experimentStatus marks ACTIVE only on the backend's confirmed active flag", () => {
  assert.equal(experimentStatus(null).key, STATUS.UNAVAILABLE, "no API → unavailable, not inactive");
  assert.equal(experimentStatus({ status: "inactive", active: false }).key, STATUS.INACTIVE);
  const active = experimentStatus({ status: "active", active: true });
  assert.equal(active.key, STATUS.ACTIVE);
  assert.equal(active.active, true);
  // a status string claiming "active" WITHOUT the confirmed flag is never treated as active
  const claimed = experimentStatus({ status: "active", active: false });
  assert.equal(claimed.active, false);
  assert.notEqual(claimed.key, STATUS.ACTIVE);
  assert.equal(experimentStatus({ status: "failed", active: false }).key, STATUS.FAILED);
});

test("activeSummary only summarizes a CONFIRMED-active experiment", () => {
  assert.deepEqual(activeSummary(null), []);
  assert.deepEqual(activeSummary({ status: "active", active: false, profile: { latency_ms: 500 } }), []);
  const lines = activeSummary({
    active: true, direction: "both", remaining_s: 42,
    profile: { latency_ms: 500, jitter_ms: 100, packet_loss_pct: 10, bandwidth_kbit_s: 512 },
  });
  assert.match(lines.join("\n"), /500 ms latency/);
  assert.match(lines.join("\n"), /42 s remaining/);
});

// ---- 10. Stop action calls the stop endpoint ----
test("the stop endpoint is a DELETE on the experiment resource, and the page calls it", () => {
  const apiSrc = read("../operator/services/api.js");
  assert.match(apiSrc, /export function stopNetworkExperiment\(\)\s*\{\s*return delJSON\("\/api\/experiment\/network"\)/);
  assert.match(apiSrc, /method:\s*"DELETE"/, "delJSON issues a DELETE");
  const page = read("../operator/pages/Experiment.js");
  assert.match(page, /api\.stopNetworkExperiment\(/, "Stop button calls the stop endpoint");
  assert.match(page, /api\.applyNetworkExperiment\(/, "Apply button calls the apply endpoint");
});

// ---- 11. Reset does not apply an experiment ----
test("Reset restores safe defaults without applying", () => {
  assert.deepEqual(defaultForm(), { ...EXPERIMENT_DEFAULTS });
  assert.equal(EXPERIMENT_DEFAULTS.full_disconnect, false);
  assert.equal(EXPERIMENT_DEFAULTS.bandwidth_kbit_s, null);
  assert.equal(EXPERIMENT_DEFAULTS.duration_s, 60);
  // the reset handler must not issue an apply request
  const page = read("../operator/pages/Experiment.js");
  const body = page.match(/function onReset\(\)\s*\{([\s\S]*?)\n  \}/);
  assert.ok(body, "onReset handler found");
  assert.match(body[1], /defaultForm\(\)/, "reset rebuilds the safe defaults");
  assert.doesNotMatch(body[1], /applyNetworkExperiment/, "reset never applies an experiment");
});

// ---- 12. Vehicle control authority does NOT gate the experiment form ----
test("the experiment form is independent of OPERATOR/LOCAL_AGENT vehicle authority", () => {
  // validateExperiment takes only the form — there is no authority parameter to thread.
  assert.equal(validateExperiment.length, 1, "validateExperiment(form) has no authority arg");
  const page = read("../operator/pages/Experiment.js");
  assert.doesNotMatch(page, /from\s*"\.\.\/lib\/(authority|home)\.js"/, "page does not import the authority/command gates");
  assert.doesNotMatch(page, /commandGate|hasControl|setControlAuthority/, "page never consults vehicle control authority");
});
