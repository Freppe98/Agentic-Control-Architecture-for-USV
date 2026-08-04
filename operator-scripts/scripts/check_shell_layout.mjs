#!/usr/bin/env node
// check_shell_layout.mjs — runtime assertions that the application shell actually OWNS
// the screen: every region occupies its own grid cell and nothing overlaps anything else.
//
// Why this exists as its own check rather than more of the viewport-overflow sweep:
// a Leaflet map rendered at 1920x1080 over the entire shell is, trivially, "inside the
// viewport". Overflow tests cannot see it. Only the RELATIVE geometry of the regions can
// — the map must start below the ribbon, right of the rail and dock, and end before the
// inspector. That is the assertion set below.
//
// Usage:
//   node scripts/check_shell_layout.mjs [--base http://127.0.0.1:8210] [--shots DIR]
// Requires a running operator backend and the `playwright` package (dev-only; it is not a
// runtime dependency of the station and is deliberately not in package.json).

import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

const arg = (name, dflt) => {
  const i = process.argv.indexOf(`--${name}`);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : dflt;
};
const BASE = arg("base", "http://127.0.0.1:8210");
const SHOTS = arg("shots", null);

let chromium;
try { ({ chromium } = await import("playwright")); }
catch {
  console.error("playwright is not installed — `npm install --no-save playwright && npx playwright install chromium`");
  process.exit(2);
}

const VIEWPORTS = [[1366, 768], [1920, 1080]];
const ROUTES = [
  { route: "map", map: "#map", appClass: "app has-dock" },
  { route: "plan", map: "#plan-map", appClass: "app has-dock plan" },
];

/** Every direct child of .app, with the placement facts that matter. */
const probeShell = (mapSel) => {
  const box = (e) => { const r = e.getBoundingClientRect(); return { t: Math.round(r.top), l: Math.round(r.left), b: Math.round(r.bottom), r: Math.round(r.right), w: Math.round(r.width), h: Math.round(r.height) }; };
  const of = (sel) => { const e = document.querySelector(sel); return e ? box(e) : null; };
  const app = document.getElementById("app");
  const children = [...app.children].map((e) => {
    const s = getComputedStyle(e);
    return {
      cls: (e.className || e.tagName).toString(),
      gridRow: `${s.gridRowStart}/${s.gridRowEnd}`,
      gridColumn: `${s.gridColumnStart}/${s.gridColumnEnd}`,
      position: s.position, zIndex: s.zIndex, rect: box(e),
    };
  });
  const mapEl = document.querySelector(mapSel);
  return {
    appClass: app.className,
    appRect: box(app),
    children,
    ribbon: of(".ribbon"), rail: of(".rail"), dock: of(".dock"),
    mapWrap: of(".map-wrap"), mapStage: of(".map-stage"), map: mapEl ? box(mapEl) : null,
    inspector: of(".inspector"), page: of(".page"), contentMain: of(".content-main"),
    mapWrapPosition: (() => { const e = document.querySelector(".map-wrap"); return e ? getComputedStyle(e).position : null; })(),
    mapStagePosition: (() => { const e = document.querySelector(".map-stage"); return e ? getComputedStyle(e).position : null; })(),
    // the containing block the map actually resolved against — the single fact that
    // distinguishes "map fills its cell" from "map fills the screen"
    mapOffsetParent: mapEl ? (mapEl.offsetParent ? (mapEl.offsetParent.className || mapEl.offsetParent.tagName) : "NONE (initial containing block)") : null,
    mapInsideStage: mapEl ? !!(document.querySelector(".map-stage") || {}).contains?.(mapEl) : null,
  };
};

const fails = [];
const checks = [];
function check(label, ok, detail = "") {
  checks.push({ label, ok, detail });
  if (!ok) fails.push(`${label}${detail ? " — " + detail : ""}`);
}
/** two rects share more than a hairline of area */
const overlaps = (a, b) =>
  a && b && Math.min(a.r, b.r) - Math.max(a.l, b.l) > 1 && Math.min(a.b, b.b) - Math.max(a.t, b.t) > 1;

const browser = await chromium.launch();
if (SHOTS) mkdirSync(SHOTS, { recursive: true });

for (const [w, h] of VIEWPORTS) {
  const ctx = await browser.newContext({ viewport: { width: w, height: h } });
  await ctx.addInitScript(() => { try { localStorage.setItem("operator.tour.v1", "done"); } catch { /* noop */ } });
  const page = await ctx.newPage();
  const pageErrors = [];
  page.on("pageerror", (e) => pageErrors.push(String(e)));

  for (const { route, map: mapSel, appClass } of ROUTES) {
    const at = `${route} @${w}x${h}`;
    await page.goto(`${BASE}/app/#/${route}`, { waitUntil: "domcontentloaded" });
    await page.waitForTimeout(1500);
    const s = await page.evaluate(probeShell, mapSel);
    if (SHOTS) await page.screenshot({ path: join(SHOTS, `shell-${route}-${w}x${h}.png`) });

    // ── the root class must still match a declared grid-template-columns rule ──
    check(`${at}: root class is "${appClass}"`, s.appClass === appClass, `got "${s.appClass}"`);

    // ── every region exists and is visible ──
    for (const [name, r] of [["ribbon", s.ribbon], ["rail", s.rail], ["dock", s.dock],
                             ["map-wrap", s.mapWrap], ["map-stage", s.mapStage],
                             ["map", s.map], ["inspector", s.inspector]]) {
      check(`${at}: ${name} is present and visible`, !!r && r.w > 0 && r.h > 0, r ? `${r.w}x${r.h}` : "missing");
    }

    // ── the map is contained by the stage, not by the viewport ──
    check(`${at}: map is a descendant of .map-stage`, s.mapInsideStage === true);
    check(`${at}: .map-wrap is positioned (containment backstop)`, s.mapWrapPosition === "relative", `got ${s.mapWrapPosition}`);
    check(`${at}: .map-stage is positioned`, s.mapStagePosition === "relative", `got ${s.mapStagePosition}`);
    check(`${at}: map resolves against .map-stage, not the viewport`,
      /map-stage/.test(String(s.mapOffsetParent)), `offsetParent = ${s.mapOffsetParent}`);
    check(`${at}: map does NOT fill the viewport`,
      !(s.map && s.map.w >= w - 1 && s.map.h >= h - 1), s.map ? `${s.map.w}x${s.map.h} vs viewport ${w}x${h}` : "");

    // ── relative geometry: the shell owns the screen ──
    const { ribbon, rail, dock, mapWrap, inspector } = s;
    check(`${at}: ribbon spans the shell width`, ribbon.l <= 1 && ribbon.r >= w - 1, `${ribbon.l}..${ribbon.r}`);
    check(`${at}: ribbon is at the top`, ribbon.t <= 1);
    check(`${at}: rail starts below the ribbon`, rail.t >= ribbon.b - 1, `rail.t=${rail.t} ribbon.b=${ribbon.b}`);
    check(`${at}: rail is at the left edge`, rail.l <= 1);
    check(`${at}: rail ends before the dock`, rail.r <= dock.l + 1, `rail.r=${rail.r} dock.l=${dock.l}`);
    check(`${at}: map begins below the ribbon`, mapWrap.t >= ribbon.b - 1, `map.t=${mapWrap.t} ribbon.b=${ribbon.b}`);
    check(`${at}: map begins right of the dock`, mapWrap.l >= dock.r - 1, `map.l=${mapWrap.l} dock.r=${dock.r}`);
    check(`${at}: map ends before the inspector`, mapWrap.r <= inspector.l + 1, `map.r=${mapWrap.r} inspector.l=${inspector.l}`);
    check(`${at}: inspector reaches the right edge`, inspector.r >= w - 1, `inspector.r=${inspector.r}`);

    // ── no region overlaps any other ──
    const regions = { ribbon, rail, dock, "map-wrap": mapWrap, inspector };
    const names = Object.keys(regions);
    for (let i = 0; i < names.length; i++) {
      for (let j = i + 1; j < names.length; j++) {
        const a = regions[names[i]], b = regions[names[j]];
        check(`${at}: ${names[i]} does not overlap ${names[j]}`, !overlaps(a, b),
          overlaps(a, b) ? `${JSON.stringify(a)} ∩ ${JSON.stringify(b)}` : "");
      }
    }
    // the map element itself, not just its wrapper
    for (const [name, r] of [["ribbon", ribbon], ["rail", rail], ["dock", dock], ["inspector", inspector]]) {
      check(`${at}: the map element does not overlap the ${name}`, !overlaps(s.map, r));
    }

    // ── nothing in the shell is fixed or spanning the whole grid ──
    for (const c of s.children) {
      const spansAll = /\b1\s*\/\s*-1\b/.test(c.gridColumn) && !/ribbon/.test(c.cls);
      check(`${at}: ${c.cls} does not span every grid column`, !spansAll, c.gridColumn);
      check(`${at}: ${c.cls} is not position:fixed`, c.position !== "fixed");
    }

    if (process.env.VERBOSE) console.log(`\n${at} children:\n` + JSON.stringify(s.children, null, 1));
  }
  check(`@${w}x${h}: no page errors`, pageErrors.length === 0, pageErrors.join(" | "));
  await ctx.close();
}
await browser.close();

const passed = checks.filter((c) => c.ok).length;
console.log(`shell layout: ${passed}/${checks.length} checks passed`);
if (fails.length) {
  console.error("\nFAILED:");
  fails.forEach((f) => console.error("  ✖ " + f));
  process.exit(1);
}
if (SHOTS) {
  writeFileSync(join(SHOTS, "shell-checks.json"), JSON.stringify(checks, null, 2));
  console.log(`screenshots + results in ${SHOTS}`);
}
console.log("OK — the shell owns the screen on Map and Plan at every tested viewport.");
