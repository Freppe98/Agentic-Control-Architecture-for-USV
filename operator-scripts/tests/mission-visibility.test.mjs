// Unit tests for the per-USV mission-overlay visibility policy
// (operator/lib/mission-visibility.js): default-visible rules + the single stateful toggle.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  missionShowable, nextVisibility, toggleVisibility, toggleButton,
} from "../operator/lib/mission-visibility.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const MAP_SRC = readFileSync(join(HERE, "..", "operator", "pages", "Map.js"), "utf-8");

// ---- static source: the two separate Show/Hide buttons are gone, one toggle remains ----
test("Map page has a single stateful mission toggle, not separate Show/Hide buttons", () => {
  assert.ok(!/data-pxm="show"/.test(MAP_SRC), "separate Show button removed");
  assert.ok(!/data-pxm="hide"/.test(MAP_SRC), "separate Hide button removed");
  assert.match(MAP_SRC, /data-pxm="toggle"/, "single toggle button present");
  assert.match(MAP_SRC, /aria-pressed=/, "toggle exposes aria-pressed state");
});

const LOADED = { count: 4, loaded: true, valid: true, partial: false };

// ---- showable ----
test("a loaded, valid, non-empty, positioned mission is showable", () => {
  assert.equal(missionShowable(LOADED, 4), true);
});
test("invalid / partial / empty / unpositioned / unreachable missions are NOT showable", () => {
  assert.equal(missionShowable(null, 0), false, "no mission");
  assert.equal(missionShowable({ ...LOADED, valid: false }, 4), false, "invalid");
  assert.equal(missionShowable({ ...LOADED, partial: true }, 4), false, "partial");
  assert.equal(missionShowable({ ...LOADED, loaded: false }, 4), false, "not loaded");
  assert.equal(missionShowable({ count: 0 }, 0), false, "empty (count 0)");
  assert.equal(missionShowable({ count: null }, 0), false, "no count");
  assert.equal(missionShowable(LOADED, 0), false, "no positioned waypoints");
});

// ---- default visibility ----
test("first valid load defaults to visible", () => {
  assert.deepEqual(nextVisibility(null, { showable: true, geometryChanged: true }), { shown: true, userHidden: false });
});
test("an explicit hide persists across an unchanged periodic read", () => {
  const hidden = { shown: false, userHidden: true };
  // periodic fetch of the SAME geometry must not force it back visible
  assert.deepEqual(nextVisibility(hidden, { showable: true, geometryChanged: false }), { shown: false, userHidden: true });
});
test("a geometry change (successful upload/replan) re-shows a hidden overlay", () => {
  const hidden = { shown: false, userHidden: true };
  assert.deepEqual(nextVisibility(hidden, { showable: true, geometryChanged: true }), { shown: true, userHidden: false });
});
test("a non-showable (empty after clear) mission is never shown, hide memory preserved", () => {
  assert.deepEqual(nextVisibility({ shown: true, userHidden: false }, { showable: false, geometryChanged: true }), { shown: false, userHidden: false });
  assert.deepEqual(nextVisibility({ shown: false, userHidden: true }, { showable: false, geometryChanged: false }), { shown: false, userHidden: true });
});
test("a previously-shown mission stays shown on an unchanged read (stale link retains it)", () => {
  assert.deepEqual(nextVisibility({ shown: true, userHidden: false }, { showable: true, geometryChanged: false }), { shown: true, userHidden: false });
});

// ---- toggle click ----
test("toggleVisibility flips and records the explicit choice", () => {
  assert.deepEqual(toggleVisibility({ shown: true, userHidden: false }), { shown: false, userHidden: true });
  assert.deepEqual(toggleVisibility({ shown: false, userHidden: true }), { shown: true, userHidden: false });
});

// ---- toggle button presentation ----
test("valid hidden mission → 'Show mission', aria-pressed false", () => {
  const b = toggleButton({ loading: false, showable: true, shown: false });
  assert.equal(b.label, "Show mission");
  assert.equal(b.ariaPressed, false);
  assert.equal(b.disabled, false);
});
test("valid visible mission → 'Hide mission', aria-pressed true", () => {
  const b = toggleButton({ loading: false, showable: true, shown: true });
  assert.equal(b.label, "Hide mission");
  assert.equal(b.ariaPressed, true);
  assert.equal(b.disabled, false);
});
test("no valid mission → disabled 'No mission'", () => {
  const b = toggleButton({ loading: false, showable: false, shown: false });
  assert.equal(b.label, "No mission");
  assert.equal(b.disabled, true);
});
test("loading → disabled, non-destructive 'Loading mission…'", () => {
  const b = toggleButton({ loading: true, showable: true, shown: true });
  assert.equal(b.label, "Loading mission…");
  assert.equal(b.disabled, true);
  assert.equal(b.ariaPressed, true, "reflects current visibility, not a reset");
});
test("aria state matches visibility across states", () => {
  assert.equal(toggleButton({ showable: true, shown: true }).ariaPressed, true);
  assert.equal(toggleButton({ showable: true, shown: false }).ariaPressed, false);
});

// ---- per-USV independence (state objects are independent) ----
test("two USVs keep independent visibility (USV A hide does not affect USV B)", () => {
  const a = { shown: false, userHidden: true };  // A hidden
  const b = { shown: true, userHidden: false };  // B visible
  assert.deepEqual(nextVisibility(a, { showable: true, geometryChanged: false }), { shown: false, userHidden: true });
  assert.deepEqual(nextVisibility(b, { showable: true, geometryChanged: false }), { shown: true, userHidden: false });
});
