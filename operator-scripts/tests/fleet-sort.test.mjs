// Fleet roster ordering (operator/lib/fleet-sort.js) — the fix for rows swapping on their own.
//
// The Fleet table defaulted to `{ key: "age", dir: -1 }`: sort by last contact, recomputed on
// every 2 s poll. Scout and SAR-001 post at slightly different moments, so their
// `last_seen_age_s` values crossed and the two rows traded places while the operator was
// looking at them. Row position is how an operator finds a vehicle — it must be a function
// of identity, not of which packet happened to land last.
//
// Run: `node --test tests/` (or `npm test`).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_SORT, sortFleet, nextSort, compareVehicleIds, isSortKey,
} from "../operator/lib/fleet-sort.js";

const veh = (id, name, extra = {}) => ({
  id,
  vehicle_id: typeof id === "number" ? `usv-${id}` : String(id),
  name,
  online: true,
  comm_state: "CONNECTED",
  last_seen_age_s: 0.5,
  battery: null,
  coverage: null,
  health: {},
  telemetry: {},
  ...extra,
});

const SCOUT = (extra) => veh(2, "Scout", { battery: 79, ...extra });
const SAR = (extra) => veh(3, "SAR-001", { battery: 50, ...extra });
const USV1 = (extra) => veh(1, "USV-1", { comm_state: "UNKNOWN", online: false, last_seen_age_s: null, contacted: false, ...extra });

const ids = (rows) => rows.map((v) => v.id);

// ---------------------------------------------------------------------------------
// Default ordering
// ---------------------------------------------------------------------------------

test("the default sort is canonical id, not last contact", () => {
  assert.equal(DEFAULT_SORT.key, "default");
  assert.notEqual(DEFAULT_SORT.key, "age", "sorting by a live value by default is the bug");
  assert.deepEqual(ids(sortFleet([SAR(), USV1(), SCOUT()])), [1, 2, 3]);
});

test("default order is stable across 20 alternating Scout/SAR updates", () => {
  // Exactly the live scenario: two vehicles reporting ~1 Hz at different moments, so each
  // poll one of them has just been heard from and the other is a second stale.
  const seen = new Set();
  for (let i = 0; i < 20; i++) {
    const scoutJustPosted = i % 2 === 0;
    const rows = [
      USV1(),
      SCOUT({ last_seen_age_s: scoutJustPosted ? 0.2 : 1.4, battery: 79 - i }),
      SAR({ last_seen_age_s: scoutJustPosted ? 1.3 : 0.1, battery: 50 - i }),
    ];
    seen.add(ids(sortFleet(rows)).join(","));
  }
  assert.deepEqual([...seen], ["1,2,3"], "the roster order must not vary with arrival timing");
});

test("different last_seen_age_s values do not change the default order", () => {
  const a = sortFleet([SCOUT({ last_seen_age_s: 0.1 }), SAR({ last_seen_age_s: 29 })]);
  const b = sortFleet([SCOUT({ last_seen_age_s: 29 }), SAR({ last_seen_age_s: 0.1 })]);
  assert.deepEqual(ids(a), [2, 3]);
  assert.deepEqual(ids(b), [2, 3], "Scout stays above SAR-001 even when it is the stale one");
});

test("a reordered fleet response renders in the same default order", () => {
  const orders = [
    [USV1(), SCOUT(), SAR()],
    [SAR(), SCOUT(), USV1()],
    [SCOUT(), SAR(), USV1()],
    [SAR(), USV1(), SCOUT()],
  ];
  for (const rows of orders) {
    assert.deepEqual(ids(sortFleet(rows)), [1, 2, 3],
      "rendered order comes from canonical id, never from response position");
  }
});

test("battery changes do not move rows under the default sort", () => {
  const climbing = sortFleet([SCOUT({ battery: 5 }), SAR({ battery: 99 })]);
  const falling = sortFleet([SCOUT({ battery: 99 }), SAR({ battery: 5 })]);
  assert.deepEqual(ids(climbing), [2, 3]);
  assert.deepEqual(ids(falling), [2, 3]);
});

test("comm-state changes do not move rows under the default sort", () => {
  const rows = [SCOUT({ comm_state: "DISCONNECTED", online: false }), SAR()];
  assert.deepEqual(ids(sortFleet(rows)), [2, 3], "going stale must not demote a row");
});

test("a never-contacted placeholder keeps its registry position", () => {
  assert.deepEqual(ids(sortFleet([SCOUT(), SAR(), USV1()])), [1, 2, 3]);
});

// ---------------------------------------------------------------------------------
// Selection independence
// ---------------------------------------------------------------------------------

test("selecting a row does not move it — selection is not an input to any comparator", () => {
  const rows = [USV1(), SCOUT(), SAR()];
  const before = ids(sortFleet(rows));
  // Selection lives in lib/selection.js and is passed to Table() separately; sortFleet is
  // never told which row is selected, so it cannot promote one.
  assert.deepEqual(ids(sortFleet(rows)), before);
  // Structural: the sorter is never handed a selected id, so it cannot promote one.
  const src = sortFleet.toString();
  assert.ok(!/select/i.test(src), "sortFleet must not reference selection at all");
});

// ---------------------------------------------------------------------------------
// Operator-chosen sorts
// ---------------------------------------------------------------------------------

test("clicking Last Contact explicitly sorts by last contact", () => {
  const rows = [SCOUT({ last_seen_age_s: 40 }), SAR({ last_seen_age_s: 2 }), USV1()];
  const desc = sortFleet(rows, { key: "age", dir: -1 });
  assert.deepEqual(ids(desc).slice(0, 2), [2, 3], "oldest contact first when descending");
  const asc = sortFleet(rows, { key: "age", dir: 1 });
  assert.deepEqual(ids(asc).slice(0, 2), [3, 2], "freshest first when ascending");
});

test("clicking Battery sorts by battery", () => {
  const rows = [SCOUT({ battery: 20 }), SAR({ battery: 90 })];
  assert.deepEqual(ids(sortFleet(rows, { key: "batt", dir: -1 })), [3, 2]);
  assert.deepEqual(ids(sortFleet(rows, { key: "batt", dir: 1 })), [2, 3]);
});

test("an explicitly chosen sort stays active and only toggles on a repeat click", () => {
  let sort = DEFAULT_SORT;
  sort = nextSort(sort, "batt");
  assert.deepEqual(sort, { key: "batt", dir: -1 }, "numeric columns open highest-first");
  sort = nextSort(sort, "batt");
  assert.deepEqual(sort, { key: "batt", dir: 1 }, "same column toggles");
  sort = nextSort(sort, "name");
  assert.deepEqual(sort, { key: "name", dir: 1 }, "identity columns open ascending");
  sort = nextSort(sort, "not-a-column");
  assert.deepEqual(sort, { key: "name", dir: 1 }, "an unknown key changes nothing");
});

test("nearly equal live values do not reorder — the age key is quantized", () => {
  // 0.2 s vs 0.9 s apart is arrival jitter between two 1 Hz agents, not information.
  const a = sortFleet([SCOUT({ last_seen_age_s: 0.2 }), SAR({ last_seen_age_s: 0.9 })], { key: "age", dir: -1 });
  const b = sortFleet([SCOUT({ last_seen_age_s: 0.9 }), SAR({ last_seen_age_s: 0.2 })], { key: "age", dir: -1 });
  assert.deepEqual(ids(a), [2, 3]);
  assert.deepEqual(ids(b), [2, 3], "sub-second jitter must not swap rows even under age sort");
  // A real difference still sorts.
  const real = sortFleet([SCOUT({ last_seen_age_s: 1.2 }), SAR({ last_seen_age_s: 45 })], { key: "age", dir: -1 });
  assert.deepEqual(ids(real), [3, 2]);
});

test("equal sort values fall back to canonical id in both directions", () => {
  const tied = [SAR({ battery: 60 }), SCOUT({ battery: 60 }), veh(4, "Probe-4", { battery: 60 })];
  assert.deepEqual(ids(sortFleet(tied, { key: "batt", dir: -1 })), [2, 3, 4]);
  assert.deepEqual(ids(sortFleet(tied, { key: "batt", dir: 1 })), [2, 3, 4],
    "the tie-breaker is always ascending id, so reversing does not shuffle tied rows");
});

test("rows with no value for the sorted column sort last in both directions", () => {
  const rows = [USV1(), SCOUT({ battery: 79 }), SAR({ battery: 50 })];
  assert.equal(ids(sortFleet(rows, { key: "batt", dir: -1 })).at(-1), 1);
  assert.equal(ids(sortFleet(rows, { key: "batt", dir: 1 })).at(-1), 1,
    "'no battery' is not an extreme of the scale, so it must not flip ends");
});

// ---------------------------------------------------------------------------------
// Mixed canonical id types
// ---------------------------------------------------------------------------------

test("string and numeric canonical ids sort deterministically", () => {
  const rows = [veh("probe-alpha", "Probe Alpha"), SAR(), veh("sar-002", "SAR-002"), SCOUT()];
  const order = ids(sortFleet(rows));
  assert.deepEqual(order, [2, 3, "probe-alpha", "sar-002"]);
  // Same set, any input order → same output order.
  assert.deepEqual(ids(sortFleet([...rows].reverse())), order);
});

test("compareVehicleIds is a total order (never NaN)", () => {
  const values = [1, 2, 3, "probe-alpha", "sar-002", null, undefined];
  for (const a of values) {
    for (const b of values) {
      const r = compareVehicleIds(a, b);
      assert.ok(Number.isFinite(r), `compare(${JSON.stringify(a)}, ${JSON.stringify(b)}) = ${r}`);
      // Note: Math.sign(0) is 0 and -Math.sign(0) is -0, which strict assert treats as
      // different — compare the sum instead so the tie case reads correctly.
      assert.equal(Math.sign(r) + Math.sign(compareVehicleIds(b, a)), 0, "antisymmetric");
    }
  }
  // The old comparator did `a.id - b.id`, which is NaN for a string id.
  assert.ok(Number.isNaN("sar-001" - 2), "…which is exactly why this helper exists");
});

// ---------------------------------------------------------------------------------
// Purity
// ---------------------------------------------------------------------------------

test("sorting does not mutate the shared fleet state used by other components", () => {
  const rows = [SAR(), SCOUT(), USV1()];
  const snapshot = ids(rows);
  const rowRefs = [...rows];
  const out = sortFleet(rows, { key: "batt", dir: -1 });
  assert.deepEqual(ids(rows), snapshot, "the caller's array order is untouched");
  assert.notEqual(out, rows, "a new array is returned");
  for (const r of rowRefs) assert.ok(out.includes(r), "row objects are shared, not cloned");
});

test("a malformed or unknown sort falls back to the default order", () => {
  const rows = [SAR(), SCOUT()];
  for (const bad of [null, undefined, {}, { key: "nope", dir: -1 }, { key: 42 }]) {
    assert.deepEqual(ids(sortFleet(rows, bad)), [2, 3], `${JSON.stringify(bad)} → default order`);
  }
  assert.deepEqual(sortFleet(null), [], "a non-array fleet is empty, not a throw");
  assert.ok(isSortKey("age") && isSortKey("default") && !isSortKey("nope"));
});

test("the Fleet page uses the shared sorter and no live-value default", async () => {
  const { readFile } = await import("node:fs/promises");
  const src = await readFile(new URL("../operator/pages/Fleet.js", import.meta.url), "utf8");
  assert.ok(src.includes("lib/fleet-sort.js"), "Fleet.js must use the shared sorter");
  assert.ok(src.includes("sort = DEFAULT_SORT"), "…and open on the stable default");
  assert.ok(!/sort\s*=\s*\{\s*key:/.test(src),
    "Fleet.js must not hardcode a default sort object — the stable default lives in the lib");
  assert.ok(!/arr\.sort\(/.test(src), "no page-local comparator to drift from the tested one");
});
