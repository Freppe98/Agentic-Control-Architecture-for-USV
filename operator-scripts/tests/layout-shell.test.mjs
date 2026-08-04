// layout-shell.test.mjs — guards the structural CSS invariants of the application shell.
//
// These are exactly the rules whose loss is INVISIBLE in review and catastrophic at run
// time: the app looked perfect at 1920x1080 while, at 1366x768, a bare `1fr` grid track
// silently inflated the body row to the nav rail's 886px min-content height and
// overflow:hidden ate the bottom 166px of every page — both map legends, the Plan action
// bar and the last two nav items. A unit test cannot see that, but it can see the one
// character that causes it.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(join(here, "..", "operator", "styles", "theme.css"), "utf8");
const vars = readFileSync(join(here, "..", "operator", "styles", "variables.css"), "utf8");

/** Comment-stripped { selector, body } pairs. Rules inside @media get their own entry;
 *  nothing here nests deeper than one level, so a flat scan is enough. */
function parse(sheet) {
  const clean = sheet.replace(/\/\*[\s\S]*?\*\//g, "");
  const out = [];
  const re = /([^{}]+)\{([^{}]*)\}/g;
  let m;
  while ((m = re.exec(clean))) {
    const sel = m[1].replace(/\s+/g, " ").replace(/^.*@media[^{]*\{/, "").trim();
    if (sel && !sel.startsWith("@")) out.push({ sel, body: m[2] });
  }
  return out;
}
const RULES = parse(css);

/** declarations of the rule whose (whitespace-normalised) selector list matches exactly */
function rule(selector) {
  const want = selector.replace(/\s+/g, " ").trim();
  const hit = RULES.find((r) => r.sel === want);
  assert.ok(hit, `no rule found for "${want}"`);
  return hit.body;
}

test("the shell owns viewport height with dvh and a floorless body row", () => {
  const app = rule(".app");
  assert.match(app, /height:\s*100dvh/, ".app must use 100dvh (a collapsing browser toolbar must not clip the shell)");
  assert.match(app, /grid-template-rows:\s*var\(--ribbon-h\)\s+minmax\(0,\s*1fr\)/,
    "the body row must be minmax(0,1fr): a bare 1fr keeps an auto (min-content) floor, and the nav rail then forces the shell taller than the viewport");
  assert.match(app, /overflow:\s*hidden/);
  assert.doesNotMatch(app, /100vw/, "100vw includes the scrollbar gutter — width:100% is the correct shell width");
});

test("every shell column layout lets the centre track shrink to zero", () => {
  for (const sel of [".app.has-dock", ".app.no-dock", ".app.dock-main"]) {
    const cols = rule(sel).match(/grid-template-columns:([^;]*)/);
    assert.ok(cols, `${sel} must declare grid-template-columns`);
    assert.match(cols[1], /minmax\(0,\s*1fr\)/, `${sel} centre track must be minmax(0,1fr)`);
    assert.doesNotMatch(cols[1], /(^|\s)1fr(\s|$)/, `${sel} must not use a bare 1fr track`);
  }
});

test("shell children may shrink below their content size", () => {
  assert.match(rule(".app > *"), /min-width:\s*0/);
  assert.match(rule(".app > *"), /min-height:\s*0/);
});

test("the map column gives its bottom bar a real row instead of overlaying the map", () => {
  const wrap = rule(".map-wrap");
  assert.match(wrap, /display:\s*grid/);
  assert.match(wrap, /grid-template-rows:\s*minmax\(0,\s*1fr\)\s+auto/,
    "stage takes the flexible row, the action/progress bar an auto row — this is what stops the bar burying the legend");
  for (const sel of [".plan-actionbar", ".mission-progress-bar"]) {
    const bar = rule(sel);
    assert.match(bar, /grid-row:\s*2/, `${sel} must occupy the map column's second row`);
    assert.doesNotMatch(bar, /position:\s*absolute/, `${sel} must not be absolutely positioned over the map`);
  }
});

test("every scrolling region can actually shrink (min-height:0 on the chain)", () => {
  for (const sel of [".page", ".content-main", ".dock", ".inspector", ".map-stage",
                     ".tablewrap", ".vcontent", ".mission-body", ".cfg", ".term-body",
                     ".veh-list", ".plan-tools", ".legend-body", ".rail"]) {
    assert.match(rule(sel), /min-height:\s*0/, `${sel} needs min-height:0 or its content pushes the shell past the viewport`);
  }
});

test("a scrolling flex column never compresses its own children", () => {
  // flex-shrink defaults to 1, so a scrolling column squeezes each card to fit and the
  // card's own overflow:hidden then eats the content. This cost Configuration two of four
  // threshold rows and two of three registry vehicles, with no scrollbar to hint at it.
  const guard = RULES.find((r) => /\.cfg > \*/.test(r.sel) && /\.veh-list > \*/.test(r.sel));
  assert.ok(guard, "the shared flex-shrink:0 guard for scrolling columns is missing");
  assert.match(guard.body, /flex-shrink:\s*0/);
  assert.match(rule(".cfg-card"), /flex:\s*none/);
});

test("the legend is bounded by the map stage and scrolls internally", () => {
  assert.match(rule(".legend"), /max-height:\s*calc\(100% - var\(--map-pad\) \* 2 - var\(--map-tl-h\)/,
    "legend height must derive from the stage and the measured top-left overlay, never a fixed offset");
  assert.match(rule(".legend-body"), /overflow-y:\s*auto/);
});

test("Leaflet control spacing comes from the shared tokens, not per-page pixels", () => {
  assert.match(rule(".leaflet-top.leaflet-right"), /padding-top:\s*calc\(var\(--map-tr-h\)/,
    "the zoom control must drop below whatever the top-right overlay actually measures");
  for (const sel of [".leaflet-left .leaflet-control", ".leaflet-right .leaflet-control",
                     ".leaflet-top .leaflet-control", ".leaflet-bottom .leaflet-control"]) {
    assert.match(rule(sel), /var\(--map-pad\)/, `${sel} must use --map-pad`);
  }
});

test("every z-index in the app comes from the documented scale", () => {
  const scale = ["--z-content", "--z-sticky-head", "--z-page-bar", "--z-nav-tip",
                 "--z-map-overlay", "--z-map-toast", "--z-modal", "--z-tour"];
  scale.forEach((v) => assert.match(vars, new RegExp(`${v}\\s*:\\s*\\d+`), `${v} must be defined in variables.css`));
  const literals = [...css.matchAll(/z-index:\s*([^;}]+)/g)]
    .map((m) => m[1].trim())
    .filter((v) => !v.startsWith("var(--z-"));
  assert.deepEqual(literals, [], `z-index literals left in theme.css: ${literals.join(", ")} — use the --z-* scale`);
});

test("the map overlay layer sits above Leaflet's own control layer", () => {
  const num = (name) => Number(vars.match(new RegExp(`${name}\\s*:\\s*(\\d+)`))[1]);
  // Leaflet hard-codes .leaflet-control at 800 and its corner containers at 1000; our
  // overlays carry operator state and must paint above them, hence the jump to 1100.
  assert.ok(num("--z-map-overlay") > 1000, "map overlays must clear Leaflet's corner containers (z 1000)");
  assert.ok(num("--z-map-toast") > num("--z-map-overlay"), "a command result must be readable over every other map overlay");
  assert.ok(num("--z-modal") > num("--z-map-toast"));
  assert.ok(num("--z-tour") > num("--z-modal"), "a tour step must never be painted under a dialog");
});

test("responsive dimensions are tokens, and the compact bands actually shrink them", () => {
  for (const v of ["--dock-w", "--inspector-w", "--map-pad", "--legend-w", "--page-gap", "--panel-pad"]) {
    assert.match(vars, new RegExp(`${v}\\s*:\\s*clamp\\(`), `${v} should be a clamp() so it scales with the viewport`);
  }
  // The nav rail is height-bound (13 fixed items), so the height bands are what keep
  // Terminal and Messages reachable on a 768px laptop.
  assert.match(vars, /@media \(max-height:900px\)/);
  assert.match(vars, /@media \(max-height:800px\)/);
  assert.match(vars, /@media \(max-width:1450px\)/);
  const short = vars.match(/@media \(max-height:800px\) \{[^}]*\{([^}]*)\}/)[1];
  assert.match(short, /--nav-size:\s*40px/);
  assert.ok(13 * 40 + 13 * 2 + 40 < 768 - 40, "13 nav items at the short-viewport size must fit under a 768px screen");
});

test("the nav rail scrolls rather than clipping navigation", () => {
  assert.match(rule(".rail"), /overflow-y:\s*auto/, "an unreachable Terminal/Messages icon is lost navigation, not a cosmetic issue");
  assert.match(rule(".nav"), /flex:\s*none/, "nav buttons must not be squeezed by the rail");
  // the tip must be fixed: the rail is a scroll container and would clip an inside tooltip
  assert.match(rule(".nav .tip"), /position:\s*fixed/);
});

test("operator-facing messages wrap instead of truncating", () => {
  for (const sel of [".plan-banner", ".toast", ".pl-upload-hint", ".fp-err", ".plan-vlist"]) {
    assert.match(rule(sel), /overflow-wrap:\s*anywhere|white-space:\s*normal/,
      `${sel} must wrap — a backend error the operator cannot finish reading is not actionable`);
  }
  assert.doesNotMatch(rule(".pl-upload-hint"), /text-overflow:\s*ellipsis/,
    "upload eligibility is text-carried, not colour-only: it must never be ellipsised away");
  // attribution is a tile licence obligation
  assert.doesNotMatch(rule(".leaflet-control-attribution"), /text-overflow:\s*ellipsis/);
});
