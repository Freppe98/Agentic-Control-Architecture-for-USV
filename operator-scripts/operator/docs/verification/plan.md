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

## No-go clearance (`no_go_clearance_m`)

A configurable minimum routing distance between generated geometry and every operator-drawn
no-go polygon, added beside the existing planning parameters.

### Where it is applied

**One place: `planning._NavGrid.__init__`.** The projected union of the drawn zones
(`nogo_original`) is buffered outward by `no_go_clearance_m` into `nogo` — the *exclusion* —
and `navigable = inset − nogo`. Round joins are deliberate: a round outward buffer is exactly
the set of points within N m of the polygon, which is the minimum-distance semantic the
parameter promises (a mitre join would silently push corners out by N·√2).

Because every consumer already judges against that one grid, the clearance reaches all of them
without a parallel distance check: coverage repair (`repair_path`), every connector kind
(`safe_connector`), the LOS-compression safety predicate, `validate_plan`, and the fleet
survey-line clip in `fleet_planning._survey_lines`. The ported degree-ring generators
(`run_lawnmower_with_obstacles`) receive `grid.exclusion_rings_deg()` instead of the raw zones,
so the coverage lanes themselves — not only the connectors — respect it.

### Original vs buffered geometry

| | geometry | where it lives |
|---|---|---|
| **Original** (authoritative, drawn by the operator, red on Map/Plan) | `nogo_original` | `planning_inputs.no_go_zones`, the immutable mission record, the Scout package |
| **Buffered exclusion** (derived routing geometry) | `nogo` | `generate` response `no_go_exclusion_rings` only — a thin dashed unfilled outline in the `pl-navigable` pane |

The buffered ring never replaces the red polygon and is never stored as an input.

### Waypoint **and** segment enforcement

Both halves are checked, independently, so a route cannot pass by filtering waypoints alone:

- `checks.waypoints_clear_no_go` — every route waypoint is outside the exclusion (runs even
  when a plan is submitted without `segments`).
- `checks.route_clears_no_go` — every segment *leg* clears the exclusion. Two individually
  clear endpoints whose straight leg cuts an exclusion corner **fail here**.
- `checks.no_go_buffer_valid` / `navigable_connected` — invalid buffer geometry and "no
  navigable space left after the clearance operations" are explicit errors.

### Connector defect found and fixed

Applying the clearance exposed a real hole in `safe_connector`: the route endpoint is a
planning coordinate, not a grid cell, so `to_grid` snapped it up to half a diagonal away and
that **stitch leg was emitted unverified** — cutting an exclusion corner the A* had carefully
routed around. `nearest_free` now only accepts a cell the endpoint can reach by a leg that
clears the exclusion, and every emitted hop is re-checked before return (`ConnectorError`
rather than a silently invalid connector). Scoped to the no-go predicate on purpose:
approach/return/home legs legitimately start outside the inset, so containment keeps its
existing tolerance semantics.

### Defaults (a fresh plan)

`shoreline_clearance_m = 5`, `no_go_clearance_m = 5`, `lane_spacing_m = 10` — declared once in
`planning.py` (`DEFAULT_*`) and mirrored in `operator/lib/planning.js` `defaultParams()`. An
**absent** field takes the default; a **supplied** one is validated (`>= 0`, spacing `> 0`), so
an explicit `0` clearance means zero clearance and an explicit `0` spacing is still an error.
Old drafts load through `paramsFromDraft`, which treats a stored `null` as "not stated".

### Measured (E2 parameters: shoreline 5 m, lane 10 m, one rectangular zone)

| `no_go_clearance_m` | min waypoint distance | min **segment** distance |
|---|---|---|
| 0 | 0.00 m | 0.00 m |
| 5 | 4.99 m | 4.85 m |
| 10 | 9.99 m | 9.89 m |

(The ~0.15 m shortfall is round-buffer chord discretisation, inside `COVER_TOL_M`.)

### Scout package

`replan-planning-package-v1` has **no** `no_go_clearance_m` field, and `V1_FIELDS` defines the
wire package as an exact key set Scout's receiver validates against. The clearance is therefore
**not** put on the wire and geometry is **not** pre-buffered: Scout receives the original
`no_go_zones`, and `meta.no_go_clearance_m` / `meta.no_go_clearance_in_package: false` plus an
explicit limitation report the gap. **Scout contract extension still required** — see Known
limitations.

## Mission geometry contract (finalize-mission-geometry)

Two live E2 replanning runs fell back to native Pixhawk RTL. Run 1: the verified runtime Home
ended outside both the mission's navigable boundary and its `home_corridor`. Run 2: **the
package itself was internally inconsistent** — several approved route waypoints lay outside the
`navigable_boundary` shipped in the same package, so `RETRACE_APPROVED` reused waypoints that
Scout's safe-return validation correctly refused.

### Root architectural inconsistency

Only the **coverage** kinds (`primary`, `secondary`, `pass_transition`) were ever required to
stay inside the shoreline inset. Every transit leg — `start_connector`, `approach`,
`survey_entry_connector`, `return_connector`, `return_approach`, `final_home_connector` — was
checked for no-go clearance **only** and was free to run anywhere, including straight out of
the navigable area to a Home on the shore (`safe_connector` emits the un-contained stitch leg
from the real endpoint to the first free grid cell by design). A package could therefore ship a
route its own `navigable_geometry` did not contain, and nothing refused it.

### The invariant, proven once

```
every finalized route segment ⊂ (navigable_geometry ∪ home_corridor)
                              − (no_go_zones ⊕ no_go_clearance_m)
and the planning Home is itself inside that approved geometry
```

`planning.check_mission_geometry` is the single implementation. It runs at **four** points, so
they cannot disagree: `generate_survey` (raises `GeometryConsistencyError` before returning a
package), `validate_plan` (reports as errors with the code prefixed), `POST
/api/missions/finalize` (400 `mission_geometry_inconsistent`, **no record and no
MISSION_UPLOAD command**), and `fleet_planning._build_child_mission` (per child).

Waypoints **and** legs **and** segments are all swept — a straight leg between two approved
points is not itself approved. Codes (`planning.GEOMETRY_ERROR_CODES`):
`INVALID_NAVIGABLE_GEOMETRY`, `ROUTE_EMPTY`, `ROUTE_WAYPOINT_INVALID`,
`ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY`, `TRANSIT_OUTSIDE_APPROVED_GEOMETRY`,
`ROUTE_NO_GO_VIOLATION`, `HOME_OUTSIDE_APPROVED_GEOMETRY`, `HOME_CORRIDOR_DISCONNECTED`,
`HOME_CORRIDOR_INCOMPLETE`, `HOME_CORRIDOR_NO_GO_VIOLATION`.

### The raw operator boundary is not a fallback

There is no path in the Operator that substitutes `planning_inputs.boundary` for the navigable
geometry — not in generation, validation, finalization, the mission record or either package
builder. A route that fits the boundary but not the inset **fails**, because accepting it would
silently discard the shoreline clearance that keeps the hull off the shore. Pinned by
`tests/test_mission_geometry.py::TestRawBoundaryIsNotAFallback`, which asserts both directions:
the fixture route *does* pass against the raw ring, and *is* refused against the real navigable
geometry.

### Home corridor, tightened

`home_corridor_ring` now takes `no_go_clearance_m` and **subtracts** the effective exclusion
from the buffered transit envelope before checking anything, rather than testing the raw zones
for contact. Subtracting keeps a merely-grazing corridor usable (it is dented) while refusing
one that only connects *through* the exclusion: the clip splitting the polygon, holing it, or
cutting it away from Home / the survey area / its own transit centreline are all refusals. The
exterior ring of a holed polygon would fill the hole straight back in, which is exactly the
legal tunnel that must never ship — hence the hole refusal. `HOME_CORRIDOR_HALF_WIDTH_M = 6.0`
is unchanged and is still the only corridor width in planning semantics.

The corridor is now **stored on the finalized record** (`home_corridor`, `home_corridor_meta`)
instead of being re-derived at package-build time, so Scout receives the exact ring finalization
proved. A historical record without the key is re-derived exactly as before, at clearance 0.

## Route-start mode: execution start ≠ geometry provenance start

`route_start_mode` chooses **where the executed mission route begins** — and nothing else. It
does not change which geometry is approved.

| Mode | Uploaded route | Approved Home↔survey transit network |
|---|---|---|
| `planning_home` | Home → approach → survey entry → survey → return → Home | identical to the executed transit legs |
| `first_approach` | **A1** → … → survey entry → survey → return → Home | Home → A1 → … → survey entry, **and** survey → return → Home |

Under `first_approach` the **Home → A1 leg is approved planning-only geometry**: it is generated
by the same `safe_connector`, swept by the same containment/no-go proof, and is a corridor
source — but it is never concatenated into the route, so it cannot move the route hash.

Two clearly separated structures carry that, on the package and on the immutable record:

```
segments                        THE EXECUTION ROUTE — flattened, hashed, uploaded, flown
planning_only_transit_segments  APPROVED transit that is deliberately NOT executed
```

`planning.approved_transit_segments()` is the union and is the single authoritative corridor
source; `home_corridor_ring` and `check_mission_geometry` both take
`planning_only_transit_segments` explicitly. A record without the field (every mission planned
before it existed) reads as `[]` — its approved transit geometry *is* its execution transit
geometry, which is the previous behaviour exactly.

**Root cause of the old refusal.** `first_approach` used to skip building the Home → A1
connector entirely, and the corridor was derived from the emitted `segments` only. The approach
chain (A1 → … → survey entry) and the Home-anchored return chain (survey → R1 → … → Home) were
then two disconnected pieces; buffering them produced a MultiPolygon, the single-ring contract
refused it (*"the approved transit geometry is not contiguous"*), and a mission with a Home
outside the navigable area failed its own proof with `HOME_OUTSIDE_APPROVED_GEOMETRY` +
`TRANSIT_OUTSIDE_APPROVED_GEOMETRY`. The corridor derivation was tied too tightly to the
execution subset; the fix corrected the **source**, and loosened no check.

### The approved chain fails clearly when it is genuinely broken

A required link that cannot be routed is a hard, coded refusal — no invented corridor, no
widened region, no tunnel through a no-go buffer, no substituted or reversed waypoints:

`HOME_TO_APPROACH_DISCONNECTED`, `APPROACH_TO_SURVEY_DISCONNECTED`,
`SURVEY_TO_RETURN_DISCONNECTED`, `RETURN_TO_HOME_DISCONNECTED` (all in
`planning.GEOMETRY_ERROR_CODES`, raised as `GeometryConsistencyError` → the existing 400
`mission_geometry_inconsistent`). The first three are pinned by fixtures in
`tests/test_route_start_mode.py`. `RETURN_TO_HOME_DISCONNECTED` is the mirror guard on the final
Home leg and is not separately reachable today: the Home → approach (or Home → survey entry)
connector is built first and already proves Home is reachable from the navigable region, so a
later Home leg cannot be the first to fail. It stays so that raise site is coded like its three
siblings rather than surfacing as a bare `ConnectorError`.

Approach and return remain **separate operator lists**. "Use reversed approach" is an explicit
Plan-page action that populates `return_waypoints`; the backend has no reversal path and never
synthesizes a return from an approach.

### No runtime corridor patch

There is no Operator endpoint that PATCHes or attaches `home_corridor` after mission creation,
and the normal workflow never needed one — `REPLAN_PATCHABLE_FIELDS` is Scout's runtime replan
config (`dry_run`, `rtl_fallback_enabled`, battery thresholds …) and contains no geometry.
Nothing was removed.

## Known limitations

- **Scout replans against the zone boundary, not the operator's no-go clearance.** The v1
  planning package cannot carry `no_go_clearance_m`, so a Scout-authored safe return may come
  closer to a no-go zone than the operator's planning parameter required. Extending the Scout
  package contract with an additive `no_go_clearance_m` is the open item; nothing here invents
  the field or claims the constraint is synchronized.
- The **Home corridor** is now clipped by the buffered exclusion, not merely checked against the
  original zones (see *Mission geometry contract*). Remaining limit: the corridor proves a
  connector to the **planned** Home only. A runtime launch Home outside it is not covered and
  the corridor is never widened to reach one — Scout fails closed, which is correct. Planning
  Home and Pixhawk-verified Home stay distinct; nothing here blurs them.
- The corridor is a **single ring** by contract. A mission whose approved transit legs are
  genuinely not contiguous yields no corridor. `route_start_mode: first_approach` is no longer
  such a mission — see *Route-start mode* above; its Home → A1 leg is approved planning-only
  geometry and the corridor is derived from the approved network, not the execution subset.
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
