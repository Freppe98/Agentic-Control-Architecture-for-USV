# Fleet Mission planning — verification

Fleet planning extends the Plan page with a **Fleet Mission** mode that divides one shared
survey area between two or more registered USVs. It is a **static, pre-deployment**
deconfliction layer built on top of the single-vehicle planner — **not** runtime collision
avoidance.

> Fleet planning performs static partitioning and pre-deployment route-conflict validation. It
> reduces planned route overlap but does **not** replace runtime vehicle-to-vehicle collision
> detection or avoidance.

## Default survey speed

The single- and fleet-vehicle survey-speed default is **1.0 m/s** (was 1.5 m/s). Backend
`planning.DEFAULT_PLANNING_SPEED_MPS = 1.0`; frontend `defaultParams().survey_speed_mps = 1.0`;
fleet `fleet-plan.js DEFAULT_FLEET_SPEED_MPS = 1.0`. Explicit user/stored speeds are preserved.

## Architecture (reuse, not duplication)

```
shared survey geometry (planning.py _NavGrid / boustrophedon)
        ↓  fleet_planning._survey_lines  (survey lines with stable ids)
contiguous, home-aware allocation  (_allocate: bounded partition × permutation, makespan-balanced)
        ↓
one INDEPENDENT child mission per vehicle  (_build_child_mission → operator-survey-plan-v1)
        ↓  reuses planning._flatten_segments / _route_waypoints / _route_hash
existing per-vehicle upload path  (POST /api/missions/finalize, unchanged, per vehicle)
```

* **Survey lines** carry stable ids (`pass-1-line-0001`, `…-0002-a/-b` for no-go splits).
* **Allocation** assigns *complete contiguous line groups* (never waypoint chunks), balancing
  **estimated duration** (or distance) via makespan minimisation, which also gives each vehicle
  the region nearest its home. Deterministic; tie-broken by cost then vehicle id.
* **Two-pass** clips the second pass into each vehicle's primary-axis band, so a vehicle owns one
  geographic region across *both* passes.
* **Child missions** are ordinary `operator-survey-plan-v1` packages, each independently hashed
  with the same `mission_contract.route_content_hash` the Pixhawk upload verifies.

## Backend endpoints

* `POST /api/planning/fleet/generate` — shared geometry + selected vehicles → fleet plan
  (child missions, allocation summary, fleet validation). Read-only.
* `POST /api/planning/fleet/validate` — re-run fleet conflict validation on a fleet plan.
* Upload reuses `POST /api/missions/finalize` **once per vehicle** (frontend-orchestrated,
  sequential); there is no bespoke fleet-upload transport.

## Fleet conflict validation

Blocking errors: fewer than two vehicles; duplicate/invalid ids; a vehicle with no assigned
line; a line assigned twice; an unassigned retained line; identical/near-identical
(< 2 m) cross-vehicle waypoints; cross-vehicle route intersections; an invalid child mission.
Warnings: planned separation below the configured minimum; duration/distance imbalance;
an approach/return route running through another vehicle's survey region.

## Upload orchestration & partial failure

Per-vehicle states `PENDING → UPLOADING → VERIFIED | FAILED | STALE`; fleet states
`NOT_STARTED → UPLOADING → PARTIALLY_UPLOADED | VERIFIED | FAILED | STALE`. Each vehicle is
finalized independently and verified by read-back; **uploaded ≠ verified**. `Retry failed`
re-queues only `FAILED`/`STALE` vehicles — verified missions are never re-uploaded. Regenerating
after a verified upload marks prior missions `STALE`. No mission is ever auto-armed or launched.

## Automated tests

* `tests/test_fleet_planning.py` (27) — speed defaults/preservation, allocation (contiguous,
  once-each, deterministic, home-influenced, speed-balanced, split rows, more-vehicles-than-lines
  error, reverse-direction, two-pass bands), fleet validation (duplicate/near-dup waypoints,
  intersections, missing home, duplicate ids, unassigned/duplicate lines, separation warning),
  and the finalize reuse path (correct mission to correct vehicle, distinct child hashes).
* `tests/fleet-plan.test.mjs` (18) — the pure fleet state model + upload orchestration.
* All backend (540) and frontend (409) tests green.

## Manual browser verification (Playwright, against the live backend)

1. Single mode renders; survey-speed field shows **1.0**. ✓
2. **Planning mode** selector switches to Fleet Mission; vehicle picklist lists the registry. ✓
3. Selecting two vehicles derives two config cards (speed default 1.0 m/s). ✓
4. Setting a home per vehicle, drawing the shared boundary and lane spacing enables generation. ✓
5. **Generate fleet** renders two colour-coded contiguous regions (USV-1 west / Scout east),
   per-vehicle approach (dashed) / survey (solid) / return (dotted) routes, homes and a legend. ✓
6. Fleet summary shows contiguous line groups (7 vs 3 lines) balanced by **duration**
   (≈24m vs ≈23m — not line count), with an imbalance metric and min route separation (31 m). ✓
7. Fleet validation reads **VALID** with every check ✓; **Upload fleet** enables. ✓
8. Isolating a vehicle from the legend hides the other vehicle's routes; **Show all** restores. ✓
9. No application console errors (only an external OSM map-tile 404). ✓

## Known limitations

* Static only — no temporal scheduling or runtime deconfliction (out of scope by design).
* Two-pass uses centroid-band **clipping** by the primary sweep axis; a secondary line is
  assigned whole/clipped per band, which can leave a small overlap near a band boundary (covered
  by the separation warning).
* Allocation enumerates contiguous partitions up to a bound (`MAX_PARTITION_COMBOS`), then falls
  back to a single balanced split for very large line counts.
* Upload verification requires a reachable Local Agent; with no vehicle connected, missions sit
  `UPLOADING` until read-back (unit-tested for the partial-failure/retry transitions).
