// mission-progress-bar.test.mjs — the Map page's bottom-of-map progress/ETA readout
// (operator/lib/mission.js's etaBarText + how Map.js wires it), written after removing
// the dock's duplicate "Mission progress" card (see MEMORY / SCOUT_INTEGRATION_HANDOFF
// for context). Two things must stay provably true now that there is only ONE
// progress/ETA presentation on the page instead of two:
//
//   1. HONEST ETA — "ETA <duration>" only with a real speed-derived estimate, "ETA — no
//      speed" only when the remaining distance is real but speed isn't (never a
//      fabricated arrival time, never a divide-by-near-zero), "ETA —" when even the
//      distance is unknown. Never an arrival clock — always remaining time.
//   2. ONE SOURCE — Map.js computes progress/ETA in exactly one place
//      (selectedMissionStats, backed by lib/mission.js) and the bottom strip is the
//      only renderer left; there is no second, differently-computed number to disagree
//      with it, and no leftover "Mission progress" dock card/markup.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  missionCounts, remainingRouteDistanceM, etaSeconds, fmtDuration, etaBarText,
} from "../operator/lib/mission.js";

const here = dirname(fileURLToPath(import.meta.url));
const mapSrc = readFileSync(join(here, "..", "operator", "pages", "Map.js"), "utf8");

// ---- etaBarText: the honest-fallback rule, isolated from any DOM/render wiring ----
test("etaBarText renders a real estimate through the shared duration formatter", () => {
  assert.equal(etaBarText(200, 500), `ETA ${fmtDuration(200)}`);
  assert.equal(etaBarText(0, 0), `ETA ${fmtDuration(0)}`); // a completed mission's zero-length remainder
});
test("etaBarText says 'no speed' only when distance is real but the estimate is not", () => {
  assert.equal(etaBarText(null, 340), "ETA — no speed");
  assert.equal(etaBarText(null, 0), "ETA — no speed"); // 0 m left is a real distance, not an absence
});
test("etaBarText falls back to a bare dash when even distance is unknown — never blames speed for that", () => {
  assert.equal(etaBarText(null, null), "ETA —");
});
test("etaBarText never fabricates a duration from a null estimate, whatever the distance", () => {
  for (const remDistM of [null, 0, 12, 5000]) {
    assert.doesNotMatch(etaBarText(null, remDistM), /\d/, "no digit may appear without a real etaS");
  }
});

// ---- progress-source consistency: the same lib/mission.js pipeline that used to feed
// both the dock card and the bottom strip must still produce one internally-consistent
// answer, and switching the inputs to another vehicle's data must never keep the first
// vehicle's numbers around (there is no cache in this pipeline to leak from). ----
function stats(route, currentSeq, vehLat, vehLng, speed) {
  const { total, completed, remaining, pct } = missionCounts(route, currentSeq);
  const remDistM = remainingRouteDistanceM(route, currentSeq, vehLat, vehLng);
  const etaS = etaSeconds(remDistM, speed);
  return { total, completed, remaining, pct, remDistM, etaS, etaText: etaBarText(etaS, remDistM) };
}
const ROUTE_A = [
  { seq: 1, lat: 56.700, lng: 13.002 },
  { seq: 2, lat: 56.701, lng: 13.003 },
  { seq: 3, lat: 56.702, lng: 13.004 },
];
const ROUTE_B = [
  { seq: 1, lat: 40.0, lng: -3.0 },
  { seq: 2, lat: 40.01, lng: -3.0 },
];

test("completed + remaining always reconstructs total (no off-by-one from the seq-0 Home split)", () => {
  const s = stats(ROUTE_A, 2, 56.6995, 13.0015, 1.2);
  assert.equal(s.completed + s.remaining, s.total);
  assert.equal(s.total, ROUTE_A.length, "Home (seq 0) must already be excluded by classifyMissionWaypoints upstream — route here is survey-only");
});

test("a real, sufficient speed yields a live ETA; a stationary vehicle honestly says so", () => {
  const moving = stats(ROUTE_A, 1, 56.6995, 13.0015, 1.2);
  assert.match(moving.etaText, /^ETA \d/, "a real speed must produce a real duration, not a dash");
  const stopped = stats(ROUTE_A, 1, 56.6995, 13.0015, 0);
  assert.equal(stopped.etaText, "ETA — no speed", "0 m/s must never render as an infinite or fabricated ETA");
  const noTelemetry = stats(ROUTE_A, 1, 56.6995, 13.0015, null);
  assert.equal(noTelemetry.etaText, "ETA — no speed");
});

test("no active mission (empty route) yields an honest 'ETA —', never a stale duration", () => {
  const s = stats([], null, 56.6995, 13.0015, 1.2);
  assert.equal(s.total, 0);
  assert.equal(s.remDistM, null);
  assert.equal(s.etaText, "ETA —");
});

test("switching vehicles never carries one vehicle's progress/ETA onto another's numbers", () => {
  // Vehicle A: partway through a 3-leg route, moving.
  const a = stats(ROUTE_A, 2, 56.6995, 13.0015, 1.5);
  // Vehicle B: a completely different, shorter route, at its very first waypoint, stationary.
  const b = stats(ROUTE_B, 1, 39.999, -3.0, 0);
  assert.notEqual(a.total, b.total, "fixture sanity: the two routes must actually differ");
  assert.notEqual(a.etaText, b.etaText);
  // Recomputing B after A must reproduce B's own answer exactly — nothing sticky from A.
  const bAgain = stats(ROUTE_B, 1, 39.999, -3.0, 0);
  assert.deepEqual(bAgain, b);
  assert.equal(bAgain.etaText, "ETA — no speed");
});

// ---- Map.js wiring: exactly one progress/ETA presentation, reusing the shared helper ----
test("the dock's old duplicate 'Mission progress' card is gone", () => {
  assert.doesNotMatch(mapSrc, /id="mprog"/, "the #mprog dock card must be removed");
  assert.doesNotMatch(mapSrc, /class="mprog"/);
  assert.doesNotMatch(mapSrc, /Mission progress</, "the dock label text must not remain as dead markup");
});

test("selectedMissionStats is computed in exactly one place and consumed once", () => {
  const defs = mapSrc.match(/function selectedMissionStats/g) || [];
  assert.equal(defs.length, 1, "selectedMissionStats must be defined once");
  // Real call sites only: skip the declaration line itself and any prose comment
  // mentioning the function by name.
  const calls = mapSrc.split("\n")
    .filter((line) => /selectedMissionStats\(\)/.test(line))
    .filter((line) => !/^\s*\/\//.test(line))
    .filter((line) => !/function selectedMissionStats/.test(line));
  assert.equal(calls.length, 1, "selectedMissionStats must be called from exactly one renderer now that the dock card is gone");
});

test("the bottom-of-map strip reuses the shared ETA-honesty helper instead of reimplementing it", () => {
  assert.match(mapSrc, /etaBarText\(ms\.etaS,\s*ms\.remDistM\)/,
    "renderMissionBar must call the shared lib/mission.js helper, not a page-local fallback");
});

test("the strip's ETA is documented as remaining time, not an arrival clock", () => {
  const i = mapSrc.indexOf('id="mpb-label"');
  assert.ok(i > 0);
  const tag = mapSrc.slice(i, mapSrc.indexOf(">", i));
  assert.match(tag, /title="[^"]*REMAINING[^"]*"/i, "the label needs a tooltip clarifying ETA is remaining time, not an arrival clock");
});

test("the roster's flex-grow claims the space instead of a leftover reserved slot", () => {
  const css = readFileSync(join(here, "..", "operator", "styles", "theme.css"), "utf8");
  assert.doesNotMatch(css, /\.mprog\s*\{/, "the removed card's CSS block must not linger as dead style");
  assert.match(css, /\.veh-list\s*\{[^}]*flex:\s*1\s+1\s+auto/, "the roster must be the flex item that grows to fill freed space");
});
