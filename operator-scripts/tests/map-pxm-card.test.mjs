// map-pxm-card.test.mjs — the Map page's bottom-left PIXHAWK MISSION dock card (compact layout).
//
// This card has no jsdom harness in this project (vanilla ES modules, no build step; see the
// other Map.js suites, e.g. mission-visibility.test.mjs and map-inspector.test.mjs, which pin
// the same file the same way): these tests read operator/pages/Map.js as text and assert on its
// renderPxm() template, the same convention those suites already use.
//
// What changed: the LAST DOWNLOAD row and the HOME verification row (chip + distance-from-Scout
// line) moved INTO the Refresh / Set Home buttons as a smaller secondary line each, so the card
// is shorter and the vehicle roster above it gains the freed height. Nothing about mission
// calculations, command gating or per-vehicle isolation changed — only where these two facts are
// drawn.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const HERE = dirname(fileURLToPath(import.meta.url));
const MAP_SRC = readFileSync(join(HERE, "..", "operator", "pages", "Map.js"), "utf-8");

// The renderPxm() template — the region under test.
const pxm = (() => {
  const start = MAP_SRC.indexOf("function renderPxm()");
  assert.ok(start > 0, "renderPxm must exist");
  const end = MAP_SRC.indexOf("\n  }", start);
  assert.ok(end > start, "renderPxm's body must be locatable");
  return MAP_SRC.slice(start, end);
})();

// ---- A. The moved rows are gone from the card body -------------------------------------
test("the separate LAST DOWNLOAD row is gone", () => {
  assert.doesNotMatch(pxm, /<span class="k">Last download<\/span>/);
});
test("the separate HOME verification chip row is gone", () => {
  assert.doesNotMatch(pxm, /<span class="k">Home<\/span>\s*<span class="pxm-chip/);
});
test("the redundant LOADED row label is gone (the header chip already says it)", () => {
  assert.doesNotMatch(pxm, /<span class="k">Loaded<\/span>/);
});
test("no distance-to-home figure is drawn in this card", () => {
  // The old card interpolated hs.distanceM / hs.verifiedDistanceM into a "<dist> from Scout"
  // line; neither field may be read inside renderPxm any more. (A plain substring check on
  // "from Scout" is too broad — an unrelated comment a few lines up legitimately reads "...
  // shown ONLY from Scout-provided signals...".)
  assert.doesNotMatch(pxm, /hs\.distanceM/);
  assert.doesNotMatch(pxm, /hs\.verifiedDistanceM/);
});

// ---- B. The renamed / shortened labels are present --------------------------------------
test("the loaded-route row is labelled Route, not Loaded", () => {
  assert.match(pxm, /<span class="k">Route<\/span>/);
});
test("the loaded-route sub-text is shortened (no 'seq' word, still names Home's sequence)", () => {
  assert.match(pxm, /Home \(\$\{mc\.home\.seq/, "Home(<seq>) without the word 'seq' inline");
  assert.doesNotMatch(pxm, /\+ Home \(seq \$\{/, "the old '+ Home (seq N)' wording must be gone");
});
test("Approved plan is shortened to Plan, with an identifying tooltip", () => {
  assert.doesNotMatch(pxm, />Approved plan</);
});
const refPlanRow = (() => {
  const start = MAP_SRC.indexOf("function refPlanRow(id)");
  const end = MAP_SRC.indexOf("\n  }", start);
  return MAP_SRC.slice(start, end);
})();
test("refPlanRow renders the PLAN label with an operator-approved-plan description", () => {
  assert.match(refPlanRow, />Plan<\/span>/);
  // The description text itself (PLAN_LABEL_DESC) is declared just above the function and
  // referenced from inside it — checked against the whole file rather than the sliced
  // function body, which starts exactly at the `function` keyword.
  assert.match(MAP_SRC, /operator-approved plan/i);
});
test("the approved-plan count still uses 'no-go zone', not Scout's own 'exclusion' word " +
  "(that word is reserved for Scout's accepted-hazard exclusions elsewhere on this page)", () => {
  assert.match(refPlanRow, /no-go zone/);
});

// ---- C. Metadata now lives INSIDE the buttons, not as separate rows ---------------------
test("the Refresh button carries a live mission-download-age secondary line", () => {
  assert.match(pxm, /data-pxm="fetch"/);
  assert.match(pxm, /<span class="pxm-btn-sub" id="pxm-age">\$\{downloadLabel\}<\/span>/);
});
test("the Set Home button carries the Home-verification state as its secondary line", () => {
  assert.match(pxm, /data-pxm="set-home"/);
  assert.match(pxm, /<span class="pxm-btn-sub \$\{homeState\.cls\}">\$\{homeState\.text\}<\/span>/);
});
test("both action buttons keep a distinct main label span alongside the secondary line", () => {
  const mains = pxm.match(/<span class="pxm-btn-main">/g) || [];
  assert.equal(mains.length, 2, "exactly Refresh and Set Home carry the two-line treatment");
});

// ---- D. Handlers are bound on the BUTTON, never on the nested secondary span ------------
test("no click handler is attached to a nested span — only to the data-pxm buttons", () => {
  assert.doesNotMatch(pxm, /pxm-btn-sub["'].*\.onclick/);
  assert.doesNotMatch(pxm, /pxm-btn-main["'].*\.onclick/);
  assert.match(pxm, /box\.querySelector\('\[data-pxm="fetch"\]'\)\.onclick/);
  assert.match(pxm, /box\.querySelector\('\[data-pxm="toggle"\]'\)\.onclick/);
  assert.match(pxm, /box\.querySelector\('\[data-pxm="center"\]'\)\.onclick/);
  assert.match(pxm, /setHomeBtn\.onclick/);
});

// ---- E. The Home state mapping is shared, not re-implemented here -----------------------
test("renderPxm derives the button's Home state from the shared homeButtonState(), not an inline chip map", () => {
  assert.match(pxm, /homeButtonState\(hs\)/);
  assert.doesNotMatch(pxm, /\["Verified", "ok"\]/, "the old inline four-way chip literal must be gone");
});
test("Map.js imports homeButtonState from lib/home.js", () => {
  assert.match(MAP_SRC, /import \{[^}]*homeButtonState[^}]*\} from "\.\.\/lib\/home\.js"/);
});

// ---- F. The polling tick updates text only, never re-binds the handler ------------------
test("tickPxmAge updates the #pxm-age span's textContent only", () => {
  const start = MAP_SRC.indexOf("function tickPxmAge()");
  const end = MAP_SRC.indexOf("\n  }", start);
  const fn = MAP_SRC.slice(start, end);
  assert.match(fn, /getElementById\("pxm-age"\)/);
  assert.match(fn, /\.textContent\s*=/);
  assert.doesNotMatch(fn, /\.onclick/, "the tick must never touch a handler");
  assert.doesNotMatch(fn, /innerHTML/, "the tick must never re-render the button");
});

// ---- G. Second button row (Hide/Show mission | Center) is untouched ---------------------
test("the second action row still resolves to the single mission toggle and Center", () => {
  assert.match(pxm, /data-pxm="toggle"/);
  assert.match(pxm, /data-pxm="center"/);
  assert.doesNotMatch(pxm, /data-pxm="show"/);
  assert.doesNotMatch(pxm, /data-pxm="hide"/);
});

// ---- H. CSS: the compaction is scoped to this card, not the shared .pxm class -----------
const CSS_SRC = readFileSync(join(HERE, "..", "operator", "styles", "theme.css"), "utf-8");
test("the tightened padding/gap is scoped to #pxm, leaving the shared .pxm base (used by the " +
  "companion panel too) at its original spacing", () => {
  assert.match(CSS_SRC, /#pxm\s*\{[^}]*padding:/);
  assert.match(CSS_SRC, /\.pxm\s*\{[^}]*padding:12px 14px 14px/, "the shared base rule must be unchanged");
});
test("the two-line button styling only targets .pxm-btns2, never the plain toggle/Center row", () => {
  assert.match(CSS_SRC, /\.pxm-btns2 button \{[^}]*flex-direction:column/);
});
