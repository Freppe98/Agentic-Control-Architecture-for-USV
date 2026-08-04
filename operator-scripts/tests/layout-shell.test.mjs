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
const mapPage = readFileSync(join(here, "..", "operator", "pages", "Map.js"), "utf8");
const planPage = readFileSync(join(here, "..", "operator", "pages", "Plan.js"), "utf8");

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

test("an absolutely positioned map can never escape its grid cell", () => {
  // REGRESSION. `#map { position:absolute; inset:0 }` resolves against its nearest
  // POSITIONED ancestor. When .map-wrap was converted from position:relative to
  // display:grid and lost its positioning, the only thing holding the map inside its grid
  // cell was .map-stage being present in the DOM — and a browser serving one cached older
  // page module against this stylesheet made the map 1920x1080 over the whole shell:
  // ribbon, rail, dock and inspector all buried. Both levels must stay positioned.
  assert.match(rule(".map-wrap"), /position:\s*relative/,
    ".map-wrap must stay positioned as the containment backstop");
  assert.match(rule(".map-stage"), /position:\s*relative/);
  assert.match(rule("#map, #plan-map"), /position:\s*absolute/);
  assert.match(rule("#map, #plan-map"), /inset:\s*0/);
  // and nothing may lift a shell region out of the grid or over it
  for (const sel of [".map-wrap", ".map-stage", ".ribbon", ".rail", ".dock", ".inspector",
                     ".page", ".content-main"]) {
    assert.doesNotMatch(rule(sel), /position:\s*fixed/, `${sel} must not be position:fixed`);
  }
  // only the ribbon may span the full grid width
  for (const sel of [".map-wrap", ".rail", ".dock", ".inspector", ".page", ".content-main"]) {
    assert.doesNotMatch(rule(sel), /grid-column:\s*1\s*\/\s*-1/, `${sel} must not span every column`);
    assert.doesNotMatch(rule(sel), /grid-row:\s*1\s*\/\s*-1/, `${sel} must not span every row`);
  }
  assert.match(rule(".ribbon"), /grid-column:\s*1 \/ -1/, "the ribbon is the one region that spans the shell width");
});

test("no later rule re-declares the placement of a shell region", () => {
  // A generic `.map-wrap { … }` appearing again further down theme.css would silently win
  // over the placement above by source order. Placement properties may be declared exactly
  // once per region (a page-scoped override like `.app.plan .map-wrap` is fine — it is a
  // different, more specific selector, and is counted separately).
  const PLACEMENT = /(^|;)\s*(grid-row|grid-column|position)\s*:/;
  for (const sel of [".map-wrap", ".map-stage", ".rail", ".dock", ".inspector", ".page", ".content-main"]) {
    const decls = RULES.filter((r) => r.sel === sel && PLACEMENT.test(r.body));
    assert.equal(decls.length, 1, `${sel} declares placement in ${decls.length} separate rules — source order then decides the layout`);
  }
});

test("Map and Plan emit the map inside .map-stage, inside .map-wrap", () => {
  // The CSS backstop above makes a missing wrapper survivable; this keeps the markup
  // itself correct, so the map fills the stage rather than the whole map column.
  for (const [name, src, id] of [["Map", mapPage, "map"], ["Plan", planPage, "plan-map"]]) {
    const wrap = src.match(/<div class="map-wrap">([\s\S]*?)<aside/);
    assert.ok(wrap, `${name}.js must render a .map-wrap`);
    const stage = wrap[1].match(/<div class="map-stage"[^>]*>([\s\S]*?)<\/div>\s*<div class="(?:plan-actionbar|mission-progress-bar)/);
    assert.ok(stage, `${name}.js must wrap its map + overlays in .map-stage, with the bottom bar OUTSIDE it`);
    assert.match(stage[1], new RegExp(`<div id="${id}"`), `${name}.js: #${id} must live inside .map-stage`);
  }
});

test("the root class each map page sets still matches a declared grid", () => {
  // .app.plan has no grid of its own — it inherits .app.has-dock. If Plan.js ever set a
  // class combination with no matching grid-template-columns, every column would collapse.
  const declared = RULES.filter((r) => /grid-template-columns/.test(r.body) && r.sel.startsWith(".app"))
    .map((r) => r.sel.replace(/^\.app/, "").split(".").filter(Boolean));
  const rootClass = (src, name) => {
    const m = src.match(/root\.className\s*=\s*"([^"]+)"/);
    assert.ok(m, `${name}.js must set root.className`);
    return m[1].split(/\s+/);
  };
  for (const [name, src] of [["Map", mapPage], ["Plan", planPage]]) {
    const classes = rootClass(src, name);
    assert.ok(classes.includes("app"), `${name}.js root class must include "app"`);
    const matched = declared.some((need) => need.every((c) => classes.includes(c)));
    assert.ok(matched, `${name}.js root class "${classes.join(" ")}" matches no .app grid-template-columns rule`);
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
  const legend = rule(".legend");
  assert.match(legend, /calc\(100% - var\(--map-pad\) \* 2 - var\(--map-tl-h\)/,
    "legend height must derive from the stage and the measured top-left overlay, never a fixed offset");
  // The floor is the safety net: --map-tl-h is an absolute measurement subtracted from a
  // percentage of an independently-sized box, so the difference can reach zero. A legend at
  // max-height:0 does not look clipped — it disappears silently.
  assert.match(legend, /max-height:\s*max\(\s*\d+px\s*,/,
    "the legend's max-height needs a floor so a measured variable can never collapse it to nothing");
  assert.match(rule(".legend-body"), /overflow-y:\s*auto/);
  assert.match(rule(".legend > .legend-h"), /flex:\s*none/);
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

test("every scroll region shares one scrollbar design, declared once", () => {
  for (const v of ["--scrollbar-size", "--scrollbar-track", "--scrollbar-thumb", "--scrollbar-thumb-hover"]) {
    assert.match(vars, new RegExp(`${v}\\s*:`), `${v} must be a token in variables.css`);
  }
  // scoped to the station's own roots, so the browser window's outer scrollbar is untouched
  const bar = RULES.filter((r) => /::-webkit-scrollbar\b/.test(r.sel));
  assert.ok(bar.length, "the shared ::-webkit-scrollbar rule is missing");
  bar.forEach((r) => assert.match(r.sel, /#app|\.modal-ov|\.tour-root/,
    `unscoped scrollbar rule "${r.sel}" would restyle the browser window's own scrollbar`));
  // compact, and never invisible
  const size = Number(vars.match(/--scrollbar-size\s*:\s*(\d+)px/)[1]);
  assert.ok(size >= 4 && size <= 8, `--scrollbar-size ${size}px should be 4–8px`);
  assert.doesNotMatch(css, /scrollbar-width:\s*none/, "scrollbars must never be hidden outright");
  assert.doesNotMatch(vars, /--scrollbar-thumb\s*:\s*(transparent|rgba\([^)]*,\s*0\s*\))/,
    "a transparent thumb is a hidden scrollbar");
});

test("no element sets scrollbar-width without scrollbar-color", () => {
  // REGRESSION. In Blink, specifying EITHER standard scrollbar property switches the
  // element to the standard rendering path and disables every ::-webkit-scrollbar rule for
  // it. A bare `scrollbar-width:thin` on .rail therefore threw away the app's dark
  // scrollbar and rendered the bright OS default — while every panel that set nothing kept
  // the dark one, which is why it looked like a one-off bug rather than a CSS rule.
  const offenders = RULES
    .filter((r) => /scrollbar-width\s*:/.test(r.body) && !/scrollbar-color\s*:/.test(r.body))
    .map((r) => r.sel);
  assert.deepEqual(offenders, [],
    `these set scrollbar-width with no scrollbar-color, which disables ::-webkit-scrollbar in Blink: ${offenders.join(", ")}`);
  // and the standard properties must stay behind the guard that keeps Blink out of them
  assert.match(css, /@supports \(scrollbar-color: red blue\) and \(not selector\(::-webkit-scrollbar\)\)/,
    "the standard scrollbar properties must be fenced off from engines that have ::-webkit-scrollbar");
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
