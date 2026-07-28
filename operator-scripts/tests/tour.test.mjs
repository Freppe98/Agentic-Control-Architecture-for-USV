// Unit tests for the guided-tour model + placement policy (operator/lib/tour.js).
// The DOM/spotlight painting needs a browser and is verified by hand
// (operator/docs/verification/guided-tour.md); what is testable here is the step
// model staying in sync with the real routes, and the geometry that decides where
// the popup lands.
import { test } from "node:test";
import assert from "node:assert/strict";
import { TOUR_STEPS, pickPlacement, tourSeen, resetTourSeen, _setStorageForTest } from "../operator/lib/tour.js";
import { NAV } from "../operator/lib/ui.js";

function fakeStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) };
}
const rect = (left, top, width, height) => ({ left, top, width, height, right: left + width, bottom: top + height });

// ---- step model ----
test("the tour covers the six briefed steps, in workflow order", () => {
  assert.deepEqual(TOUR_STEPS.map((s) => s.id),
    ["welcome", "sidebar", "status", "planning", "follow", "recovery"]);
});

test("every step has an id, label, title and body", () => {
  for (const s of TOUR_STEPS) {
    assert.ok(s.id && s.label && s.title && s.body, `step ${s.id} is incomplete`);
  }
});

test("step ids are unique", () => {
  assert.equal(new Set(TOUR_STEPS.map((s) => s.id)).size, TOUR_STEPS.length);
});

test("every step routes to a page that actually exists in the nav model", () => {
  const routes = new Set(NAV.map(([k]) => k).filter((k) => k !== "_sep"));
  for (const s of TOUR_STEPS) {
    assert.ok(routes.has(s.route), `step ${s.id} routes to unknown page "${s.route}"`);
  }
});

test("targets are selector lists so a missing overlay falls through to a backup", () => {
  for (const s of TOUR_STEPS) {
    if (s.target == null) continue;             // welcome is a centred card by design
    assert.ok(Array.isArray(s.target), `step ${s.id} target must be an array`);
    assert.ok(s.target.length > 0, `step ${s.id} target list is empty`);
    s.target.forEach((sel) => assert.equal(typeof sel, "string"));
  }
});

test("the tour never describes an action it also performs", () => {
  // Belt-and-braces on the content rule: a step is text + selectors only.
  for (const s of TOUR_STEPS) {
    assert.equal(typeof s.body, "string");
    assert.ok(!("onEnter" in s) && !("action" in s), `step ${s.id} must not carry a side effect`);
  }
});

// ---- placement policy ----
const VP = { w: 1600, h: 900 };
const POP = { w: 392, h: 420 };

test("prefers the requested side when it fits", () => {
  // the nav rail: a narrow full-height strip on the left → popup goes right
  const p = pickPlacement(rect(0, 48, 78, 852), POP.w, POP.h, "right", VP);
  assert.equal(p.side, "right");
  assert.equal(p.x, 78 + 14);
});

test("flips to the opposite side when the preferred one does not fit", () => {
  // the inspector: pinned to the right edge, so "right" cannot fit → flips left
  const p = pickPlacement(rect(1256, 48, 344, 852), POP.w, POP.h, "right", VP);
  assert.equal(p.side, "left");
  assert.ok(p.x + POP.w <= 1256 - 14 + 1);
});

test("a target filling the viewport centres the popup inside the spotlight", () => {
  const p = pickPlacement(rect(0, 0, VP.w, VP.h), POP.w, POP.h, "right", VP);
  assert.equal(p.side, "center");
  assert.equal(p.x, Math.round(VP.w / 2 - POP.w / 2));
  assert.equal(p.y, Math.round(VP.h / 2 - POP.h / 2));
});

test("the popup is always clamped inside the viewport margins", () => {
  const cases = [
    [rect(0, 0, 20, 20), "top"], [rect(1580, 880, 20, 20), "bottom"],
    [rect(800, 0, 200, 10), "top"], [rect(10, 400, 30, 30), "left"],
  ];
  for (const [r, prefer] of cases) {
    const p = pickPlacement(r, POP.w, POP.h, prefer, VP);
    assert.ok(p.x >= 12 && p.x + POP.w <= VP.w - 12 + 1, `x out of bounds: ${p.x}`);
    assert.ok(p.y >= 12 && p.y + POP.h <= VP.h - 12 + 1, `y out of bounds: ${p.y}`);
  }
});

test("a viewport narrower than the popup still yields a finite position", () => {
  const p = pickPlacement(rect(0, 0, 100, 100), 392, 420, "right", { w: 320, h: 300 });
  assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y));
  assert.equal(p.x, 12);
  assert.equal(p.y, 12);
});

// ---- first-run persistence ----
test("the tour auto-opens once, then is remembered", () => {
  const s = fakeStorage();
  _setStorageForTest(s);
  resetTourSeen();
  assert.equal(tourSeen(), false, "a fresh station should see the tour");
  s.setItem("operator.tour.v1", "done");        // what closeTour() writes
  assert.equal(tourSeen(), true);
  resetTourSeen();
  assert.equal(tourSeen(), false, "resetTourSeen re-arms the first run");
});

test("unusable storage reports seen, so the tour never nags on every reload", () => {
  _setStorageForTest({
    getItem() { throw new Error("SecurityError"); },
    setItem() { throw new Error("SecurityError"); },
    removeItem() { throw new Error("SecurityError"); },
  });
  assert.equal(tourSeen(), true);
  _setStorageForTest(null);
  assert.equal(tourSeen(), true);
});
