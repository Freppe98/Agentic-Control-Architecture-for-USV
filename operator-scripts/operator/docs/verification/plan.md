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
- Frontend `tests/planning.test.mjs` (node:test): Plan in NAV between Fleet/Mission; boundary
  enables no-go zones; stable zone ids; every generation input marks the route outdated;
  secondary-angle default; validation + vehicle gate upload; outdated/invalid block upload;
  Clear resets + `hasUnsavedWork`; draft round-trip; generated route → mission-contract params.

Run: `python -m unittest discover -s tests -p "test_*.py"` and `npm test`.
Result at commit: **backend 306 OK, frontend 221 pass**.

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

## Known limitations

- Obstacle **execution** / Local-Agent replanning is out of scope (planning-time no-go
  avoidance only). Dual-pass intersections are exposed as planning metadata for the future
  graph-based replanner; no arbitrary diagonal connectors are added.
- Duration is an estimate from the (optional) survey speed; a default speed is used and
  labelled when none is given.
- "Warn before navigating away" covers a real tab close/reload via `beforeunload`; in-app hash
  navigation is operator-initiated and not intercepted.
- Planning requires `shapely`/`pyproj`/`numpy`; without them the Plan endpoints return `503`
  and the page cannot generate (honest failure, not a fake route).
