# Plan page — survey mission planning (verification)

The Plan page constructs a side-scan-sonar lawnmower survey and uploads it through the
**existing** read-back-verified `MISSION_UPLOAD` command path — it is not a second mission
framework. Planning is operator-owned; the vehicle receives a finalized, validated package.

## What it is

- **Navigation:** `Map · Fleet · Plan · Mission · Agent · …` — `Plan` sits between Fleet and
  Mission (`operator/lib/ui.js` `NAV`, icon `ICON.plan`; route wired in `operator/app.js`).
- **Layout:** the 4-column `has-dock` grid — rail | left tools panel | centre Leaflet map |
  right params/validation/summary — with a bottom action bar overlaid on the map.
- **State model (`operator/lib/planning.js`, pure/unit-tested):**
  `EMPTY → BOUNDARY_DEFINED → CONFIGURED → ROUTE_GENERATED → VALID → UPLOADING → UPLOADED`,
  plus `ROUTE_OUTDATED` (a generation input changed after generation) and `ERROR` (terminal
  upload failure). The state is **derived, never stored**, so the ordered workflow stays
  revisitable — returning to an earlier step re-derives the state.
- **Route generation ownership:** the coverage generator (`run_lawnmower_with_obstacles`) and
  the A* return planner (`compute_return_path`) are **ported from the Scout AqualityONE repo**
  (`flask/lawnmower_with_obstacles.py`, `flask/app.py`) into `planning.py`, preserving their
  geometry behaviour — not rewritten from memory. Generation/validation are deterministic and
  run without a live Scout. They need `shapely` + `pyproj` + `numpy`; when those are absent the
  planning endpoints answer an honest `503`, never a fabricated route.

## Backend endpoints (`main.py`)

| Endpoint | Purpose |
|---|---|
| `POST /api/planning/generate` | segmented route + `route_waypoints` + metrics + `navigable_boundary` + warnings |
| `POST /api/planning/validate` | deterministic pre-upload checks `{ok, errors, warnings, checks}` |
| `GET/POST/PUT/DELETE /api/planning/drafts[/{id}]` | JSON-file draft store (`planning_drafts/`) |
| `POST /api/commands` (`MISSION_UPLOAD`) | **reused** upload path — canonicalises + hashes the generated route |

## Automated tests

- Backend `tests/test_planning.py` (unittest + TestClient): valid generation, shoreline
  clearance shrinks the navigable area, no-go zones excluded from the coverage interior, lane
  spacing changes density, the survey angle changes the route, dual pass creates both passes +
  the transition/return segments, generation determinism (what makes "outdated" detectable),
  empty-inset / invalid-geometry / zero-spacing rejection, waypoint-limit warning, validation
  (ok, no-go interior crossing → error, over-limit → error, no route → error), draft CRUD, and
  a generated route hashing with `mission_contract` and uploading via `POST /api/commands`
  (with the over-limit route refused).
- Backend `tests/test_planning_quality.py` (unittest): the route-quality layer — line-of-sight
  connector compression (collapses a safe staircase, is blocked by a concave boundary / a no-go
  interior, deterministic), semantic cleanup (dedup / near-dup / collinear removal / genuine
  corner + anchor + endpoint preservation / tiny-zigzag / unsafe-shortcut rejection), and three
  asymmetric/concave regression fixtures (concave notch, multi-lobe, central obstacle) asserting
  safety, validity, connector reduction, monotonic sweep ordering and byte-stable generation.
- Frontend `tests/planning.test.mjs` (node:test): Plan in NAV between Fleet/Mission; boundary
  enables no-go zones; stable zone ids; every generation input marks the route outdated;
  secondary-angle default; validation + vehicle gate upload; outdated/invalid block upload;
  Clear resets + `hasUnsavedWork`; draft round-trip; generated route → mission-contract params.

Run: `python -m unittest discover -s tests -p "test_*.py"` and `npm test`.
Result at commit: **backend 346 OK, frontend 230 pass**.

## Live browser verification (Playwright, against the real backend)

Loaded `/app/#/plan`, seeded a live vehicle (Scout, id 2), then drove the whole workflow:

| Step | Observed state |
|---|---|
| initial | `EMPTY` |
| draw 4-corner boundary → Finish | `BOUNDARY_DEFINED` |
| set lane spacing 25 m + select Scout | `CONFIGURED`, Generate enabled |
| Generate | `ROUTE_GENERATED`, summary shows waypoints/areas/lengths |
| Validate | `VALID` |
| change lane spacing 12 m | `ROUTE_OUTDATED`, validation says "regenerate before validating" |

**Zero console/page errors.** Screenshot confirmed the operator-station look: green coverage
lanes inside the boundary, editable vertex handles, legend, and the honest "No planning home
is set — no return route was generated" and "Duration is an estimate only" notes.

## Manual test procedure

1. `python -m uvicorn main:app --host 127.0.0.1 --port 8199` (or `run_operator_backend.ps1`).
   Optionally POST a live vehicle to `/agent/status` (see `docs/verification/*`), else the
   template roster (ids 1/2/3) is used.
2. Open `http://127.0.0.1:8199/app/#/plan`.
3. **Vehicle:** pick a vehicle in the left "1 · Vehicle" select.
4. **Survey area:** click **Draw boundary**, click ≥3 map points, then **Finish** (or
   double-click). Confirm draggable vertex handles appear and the state chip reads
   `BOUNDARY_DEFINED`.
5. **Restrictions:** click **Add no-go zone** (enabled only now), draw a small polygon inside
   the boundary, Finish. It appears in the zone list with a stable id and a red fill; **✕**
   removes it.
6. **Home & transit:** **Set home** then click a point (a home marker appears, draggable).
   **Add transit WP** then click points; reorder with ▲/▼, remove with ✕.
7. **Survey pattern (right panel):** set **Lane spacing** (e.g. 20 m), **Survey angle**,
   optionally tick **Dual pass**. State reaches `CONFIGURED`.
8. **Generate route** (bottom bar). State → `ROUTE_GENERATED`; the map shows distinct primary
   (green) / secondary (purple) / transition / transit / return styles and the navigable
   overlay; the right summary fills in (areas, lengths, waypoint count / limit, est. duration).
9. Change any parameter or edit geometry → state → `ROUTE_OUTDATED`; **Validate**/**Finish &
   Upload** disable until you **Regenerate**.
10. **Validate**. Errors/warnings render next to the section and gate upload.
11. **Finish & Upload:** requires the selected vehicle to hold **OPERATOR** control (take it on
    the Map/Vehicle page first). Confirm the dialog; the mission is queued as `MISSION_UPLOAD`
    and verified by read-back. On success the banner shows the waypoint count + route hash and
    an "Open on Map" link; on failure the plan is preserved (never auto-cleared). Upload never
    starts the mission.
12. **Save draft** / **Load draft** round-trips the whole plan (JSON files under
    `planning_drafts/`). **Clear** asks for confirmation when there is work to lose, resets the
    page, and issues no vehicle command.

## Route corrections (feature/operator-plan-route-corrections)

### Approach vs Return semantics
- **Approach waypoints (A1, A2, …)** — the operator-approved route *into* the survey, visited
  in numbered order *before* coverage: `planning home → A1 → A2 → survey entry`. They are NOT
  points visited before returning home.
- **Return waypoints (R1, R2, …)** — a *separate* optional route *out* of the survey toward
  home: `last coverage point → safe return connector → R1 → R2 → planning home`. The backend
  never auto-reverses the approach; **Use reversed approach** copies `A3→A2→A1` into the
  return list as an editable starting point.
- Migration: old drafts/callers using `transit`/`transit_waypoints` load transparently as
  approach waypoints — no saved plan breaks.

### Planning Home vs Pixhawk HOME_POSITION
- **Planning Home** is route-planning geometry only. It is where the planned route starts
  (`Start route from → Planning home | First approach waypoint`) and where the return route
  ends. It does **not** change the Pixhawk `HOME_POSITION` or the RTL home — an upload never
  moves Home (Scout owns Pixhawk seq 0 / Home; the operator never sends it).

### Route segment kinds and colours
`start_connector`, `approach` (orange dashed, A-markers, arrows), `survey_entry_connector`
(orange dotted), `primary` (green solid), `pass_transition` (grey dashed), `secondary`
(purple solid), `return_connector` (amber dashed), `return_approach` (amber dashed, R-markers),
`final_home_connector` (amber dotted). One hue family per phase — never one orange for
approach + transition + return. No-go zones keep an unmistakable **red** fill/outline in their
own map pane *above* the navigable fill, so generating the route can never grey them out.

### Connector policy (safe connectors)
Every gap between coverage/operator sections is an **explicit connector segment**, so the flat
Pixhawk route carries no invisible straight jump. One strategy: `planning.safe_connector`
accepts the direct segment only when it stays inside the navigable (shoreline-offset) region
and clears no-go interiors; otherwise it routes a **bounded deterministic 4-neighbour grid
A\*** inside the navigable region (no diagonal shortcuts, no second planner). It repairs every
unsafe coverage lane turn / no-go-split bridge / pass transition — the fix for the observed
asymmetric-concave "connector leaves the polygon" defect. If no safe path exists, generation
**errors** rather than emitting a bad connector. Generation requires **one connected
navigable region**; a clearance/no-go split is rejected with a clear message.

### Mission package + immutable original revision 0
- Generation returns the **`operator-survey-plan-v1`** package: typed `segments` (with
  execution-seq ranges), `original_execution_order`, `route_waypoints`, the canonical
  `route_hash`, echoed `planning_inputs`, `navigable_boundary`, `metrics` and `warnings`.
- **Finish & Upload** calls `POST /api/missions/finalize`, which stores ONE immutable original
  mission record (`mission_revision: 0`, `immutable: true`) retaining that geometry AND creates
  the unchanged, read-back-verified `MISSION_UPLOAD` command. The record's `route_hash` is the
  **same** `mission_contract.route_content_hash` the upload is verified against. A verified
  read-back marks the original mission `VERIFIED`; a failed upload keeps the record with
  `upload_status: FAILED` and the plan intact. Read-only: `GET /api/missions/original/{id}`,
  `GET /api/vehicles/{id}/missions/active-original`.

### Browser verification procedure
1. Start the backend (`./run_operator_backend.ps1`), open `http://127.0.0.1:8210/app/#/plan`.
2. Select a vehicle; draw a **concave** boundary (a notch/bay); set clearance + lane spacing.
3. Add a no-go zone inside it — confirm it is **red** before generation.
4. Add 2–3 **approach** waypoints (see `A1→A2→…` with arrows), a **planning home**, and a
   couple of **return** waypoints (try **Use reversed approach**).
5. **Generate** — confirm: the no-go zone is **still red** (not greyed); the route shows all
   nine segment kinds in their distinct colours; every coverage segment stays inside the green
   navigable area even around the concavity; no straight jump crosses the notch. The connectors
   around the notch are now a few **turn points**, not a dense staircase, and Route Summary
   shows a **"Waypoints reduced: raw → final (−N redundant)"** row.
6. **Validate** — should pass; deliberately drag a return WP across the no-go interior and
   re-generate/validate to see the exact offending segment reported and **Upload** stay blocked.
7. Take OPERATOR control, **Finish & Upload** — the banner shows `mission id · rev 0 · N wp ·
   hash …`; when Scout's read-back verifies, the original record flips to `VERIFIED`.
8. Reload with an old draft using `transit` — it loads as approach waypoints (no break).

### Deferred (Scout-side replanning — NOT implemented here)
The immutable record reserves `mission_revision` / `parent_revision_id` / `revision_reason` /
`blocked_segments` / `derived_from_route_hash` for the future flow: *original revision 0 →
Scout obstacle event → Local Agent creates revision 1 → revised flat route uploaded and
verified → Operator stores revision 1 linked to the original*. None of that execution — no
graph search, no LOITER/replan/resume, no full-package delivery to Scout — is built in this
task; only the record and read-only APIs the future work needs.

## Route quality (feature/operator-plan-route-quality)

This is route **cleanup** — fewer, cleaner mission items — **not** continuous-curvature
trajectory smoothing. The Pixhawk still flies **straight segments between mission items**; the
work only removes waypoints the straight legs already pass through and collapses grid staircases
to their genuine turn points. Every safety and upload invariant above is preserved.

### Confirmed cause of the irregularity
Measured, not assumed. On the asymmetric/concave fixtures the dominant problem was
**unsimplified grid-A\* connectors**: `safe_connector` returned one waypoint per grid cell with
only exact-duplicate removal, so a single straight-ish corridor became a **33–46-point
staircase** (plus long collinear runs). Fragment **ordering was already coherent** — the ported
boustrophedon + projection order is monotonic by sweep row (0–1 immediate backtracks measured),
so it is retained, instrumented and asserted, **not** rewritten (a speculative rewrite of the
fragile ported stitch could only risk coverage). Confirmed before any code changed.

### Row-aware fragment ordering (retained + verified)
Coverage advances **monotonically through the sweep rows**, alternating sweep direction, exactly
as the ported generator produces. Each coverage sub-path is decomposed into **fragments** with
metadata (`row_index`, `sweep_coordinate`, `length_m`, endpoints, point count) in
`route_quality.coverage_fragments`, and `fragment_reorders` counts consecutive fragments that
regress along the sweep axis. The regression tests assert `fragment_reorders == 0` and that
execution order equals sweep-row order — i.e. no cross-row jumping.

### Connector line-of-sight compression (`safe-line-of-sight-v1`)
After A\* returns a safe grid path, `_compress_los` keeps `P0`, then from the current kept point
takes the **furthest** later `Pj` whose direct `Pi→Pj` is safe **under the same predicate the
connector was routed with**, and repeats. Because every retained hop is re-verified with
`segment_is_safe`, the safe corridor is never widened. **Generic RDP is not trusted**: RDP
minimises perpendicular error, which can cut a corner through unapproved water or a no-go
interior; here every proposed shortcut is checked with the real geometric safety predicate, so a
shortcut across a concave notch or a no-go zone is refused and the turn point kept.

### Semantic cleanup + per-kind policy (`semantic-path-cleanup-v1`)
`clean_path` runs, in order: exact-dedup → near-duplicate merge (< `CLEANUP_MIN_SPACING_M`) →
safety-checked collinear removal (< `CLEANUP_COLLINEAR_DEG`, bypass re-verified) → for aggressive
connector kinds a line-of-sight pass → collinear removal again. **Points never removed** (the
semantic anchors): first/last of every segment, operator **approach**/**return** waypoints,
survey entry, coverage fragment endpoints, and (by construction of the segmentation) every
segment join and planning-home connector endpoint. Policy by kind: **aggressive** LOS for
generated connectors (`start`/`survey_entry`/`pass_transition`/`return`/`final_home`);
**moderate** for `approach`/`return_approach` (operator WPs preserved, only generated
in-between points compressed); **conservative** for `primary`/`secondary` coverage (dedup +
provably-collinear only, no broad shortcuts across lanes).

### Objective route-quality metrics (`route_quality`)
No vague score — every number is re-derivable: `raw`/`final`/`removed` waypoint counts,
`raw`/`final` connector waypoint counts, connector length before/after, coverage fragment count,
fragment reorders, backtracking events (a true `A→B→A` return-to-origin spike, **not** a
legitimate lane U-turn), minimum segment length. The UI Route Summary shows only the compact
**"Waypoints reduced: raw → final (−N redundant)"** row; the detail stays in the package/tests.
The canonical mission-contract **hash is computed from the final simplified route** (unchanged
calculator, unchanged upload protocol); `generation_algorithm` records the per-stage provenance.

### Validation after cleanup (unchanged defence)
`validate_plan` is not bypassed: it independently re-checks that the flattened segment route
equals `route_waypoints`, no invisible jumps, coverage stays inside the navigable region,
connectors clear no-go interiors, operator WPs remain in order, coordinates are finite, the hash
recomputes from the final route, and the count is within the limit. A shortcut that would fail
any of these is rejected locally by the safety predicate before it is ever emitted.

### Measured before → after (three regression fixtures, min of 5 runs)
| Fixture | Total wp | Connector wp | Connector length (m) | Gen time (ms) |
|---|---|---|---|---|
| concave notch | 94 → **64** | 36 → **6** | 506.4 → 505.7 | 17.8 → 20.8 |
| multi-lobe (wide notch) | 118 → **66** | 37 → **12** (raw A\* 64) | 517.0 → 504.9 | 18.6 → 23.3 |
| central obstacle | 116 → **83** | 36 → **6** | 506.4 → 505.7 | 27.5 → 31.4 |

Connector **length is essentially unchanged** (LOS only cuts staircase corners while staying
safe), coverage geometry is preserved (coverage length unchanged, validation passes), and
generation stays **well under 35 ms** — the compression re-uses a cached safety predicate
(`_safe_cache`) so the extra passes stay inside the bounded planning budget.

### Deterministic / bounded
Identical inputs produce a **byte-equivalent route and hash** (asserted per fixture). The grid
size limit (`MAX_GRID_CELLS_PER_AXIS`) is unchanged; the safety predicate is memoised by
normalised endpoint pair so the O(n²) compression re-checks nothing twice.

## Known limitations

- Route quality is **cleanup, not smoothing**: the vehicle flies straight legs between items;
  no splines/Dubins/curvature. A legitimate coverage U-turn in a narrow lobe is retained (it is
  required by the geometry), not counted as a backtrack.
- Fragment ordering is the ported monotonic sweep order — **not** a global TSP; a genuinely
  multi-lobed shape that the ported coverage generator itself cannot cover validly is out of
  scope (this is a coverage-generation limit, not a cleanup one).
- Obstacle **execution** / Local-Agent replanning is out of scope (planning-time no-go
  avoidance only). Dual-pass intersections are exposed as planning metadata for the future
  graph-based replanner; no arbitrary diagonal connectors are added.
- The safe-connector grid is bounded (`MAX_GRID_CELLS_PER_AXIS`); a navigable area too large
  for the chosen lane spacing raises an excessive-resolution generation error rather than
  running unbounded — increase lane spacing or shrink the survey.
- The immutable mission record store is in-memory (resets on backend restart), like the
  command queue and event log; durable storage is a later item.
- Duration is an estimate from the (optional) survey speed; a default speed is used and
  labelled when none is given.
- "Warn before navigating away" covers a real tab close/reload via `beforeunload`; in-app hash
  navigation is operator-initiated and not intercepted.
- Planning requires `shapely`/`pyproj`/`numpy`; without them the Plan endpoints return `503`
  and the page cannot generate (honest failure, not a fake route).
