# Situational-awareness auto-refresh + Plan dynamic initial view

Branch `feature/operator-plan-route-quality`. Two usability improvements — automatic
selected-USV refresh on the Map page, and a dynamic initial map position on the Plan page.
Frontend-only; no Scout changes, no mission-contract-v1 changes, no new dependencies.

## Added (new modules)
- `operator/lib/selection.js` — the ONE shared "which USV is selected" store (localStorage,
  keyed by numeric USV id). Map writes it on select; Plan adopts it at load and on change.
- `operator/lib/mission-refresh.js` — pure policy: `missionIdentity()` (strongest stable id:
  route_content_hash → full_mission_hash → hash → count/current_seq) + a per-USV
  `createMissionRefreshTracker()` deciding when to re-download the full mission, and whether
  the geometry actually changed. `MISSION_WRITE_COMMANDS` = {UPLOAD, CLEAR, REPLAN}.
- `operator/lib/map-view.js` — pure initial-view picker (`pickInitialView`), coordinate
  validation (`isValidLatLng`, `isNullIsland`, `freshVehiclePosition`, `bestFleetPosition`),
  and Plan-viewport persistence.
- `operator/services/selected-refresh.js` — one DI'd refresh controller: immediate fetch on
  select, interval fallback, generation-token late-response rejection, overlap guard,
  visibility pause.

## Changed
- `operator/services/api.js` — `poll()` gained a backward-compatible `opts.pauseWhenHidden`
  (+ injectable `isHidden`); a hidden tab skips the network call and rechecks shortly.
- `operator/pages/Map.js` — selection is shared; selecting a USV immediately reads its Pixhawk
  mission via the controller; the full mission is re-read only on select, a completed
  mission-write command, a (dormant) fleet revision signal, a ~20 s fallback, or manual Fetch;
  fleet/env polls pause when hidden; late USV-A reads never overwrite USV-B. Manual Fetch is
  unchanged as an always-works recovery action (bypasses the overlap guard, shows errors).
- `operator/pages/Plan.js` — dynamic initial view (selected USV → other fleet USV →
  geolocation → saved viewport → Toftasjön), render-then-recenter, manual-interaction lock,
  "Center on USV" / "Center on me" controls, viewport persistence, shared selection.
- `operator/styles/theme.css` — `.plan-viewctl` overlay styling.

## Refresh strategy & intervals
- Lightweight selected-USV state: the existing 2 s fleet poll (whole roster in one call) +
  the existing 2–3 s authority/commands/comms polls. Now pause while the tab is hidden.
- Full Pixhawk mission: NOT on every heartbeat. Fetched only on the triggers below; cached
  per USV by mission identity; the overlay is redrawn only when the identity changed.

## Full-mission refetch triggers
1. Vehicle selected (immediate).
2. A MISSION_UPLOAD / MISSION_CLEAR / MISSION_REPLAN command reaches a terminal state (once
   per command id).
3. Fleet feed reports a changed mission-revision signal for the selected USV — **dormant**:
   the backend does not surface `active_revision_id`/`active_route_hash`/`mission_changed_at`
   yet (see API limitation below); the comparison is wired and will activate when it does.
4. Slow fallback: at most one re-read per ~20 s.
5. Manual Fetch (explicit).

## Plan-page centre priority
selected fresh USV → most-recently-contacted fresh fleet USV → browser geolocation →
saved viewport → Toftasjön (final fallback only). Fresh = valid WGS84, not Null Island, last
contact ≤ 120 s. Render is never blocked on geolocation; recentre only upgrades to a strictly
stronger source and stops once the operator pans/zooms.

## Tests (node:test, all green)
- `tests/selection.test.mjs` (6) — normalization, notify-on-change, persistence, private-mode.
- `tests/mission-refresh.test.mjs` (12) — identity precedence, force/fallback/revision triggers,
  in-flight suppression, per-USV keying, change reporting.
- `tests/map-view.test.mjs` (12) — validation, freshness, priority order, viewport round-trip.
- `tests/selected-refresh.test.mjs` (11) — immediate fetch, late-response rejection, no overlap,
  repeated refresh, fallback caching, command trigger, unchanged identity, stale-retention,
  hidden-tab pause, stop.
- `tests/poll-visibility.test.mjs` (2) — pauseWhenHidden pause/resume + back-compat.
- Baselines: frontend `npm test` → 271 pass (was 230). Backend `python -m unittest` → 346 pass.

## Manual verification steps
1. `/app#/map`: select a USV → position/telemetry update live (~2 s) and the Pixhawk mission
   is fetched automatically (card shows counts without pressing Fetch).
2. Rapidly switch USV A→B→A → the B view never flashes A's mission (late-response guard).
3. Upload/clear a mission (Plan → Finish, or a MISSION_CLEAR) → the Map mission overlay
   re-reads once the command settles, without manual Fetch.
4. Background the tab a while → the ribbon feed stops advancing; foreground → it resumes at once.
5. Kill Scout mid-view → mission/telemetry retain last-known + are marked stale; restore → live.
6. `/app#/plan` with a fresh USV selected → opens centred on that USV; with none, allows
   geolocation → centres there; deny → saved viewport or Toftasjön. Pan away → no snap-back;
   "Center on USV" recentres.

## API limitation requiring a separate Scout/backend change
The lightweight fleet payload (`GET /api/fleet/status`) carries **no** cheap mission-revision
signal (generation / route hash / item count / current_seq) per vehicle. So "refetch when the
vehicle reports a changed mission revision" (trigger 3) and progress-only updates from
`current_seq` without a full download cannot fire today — they fall back to the ~20 s safety
re-read. Closing this needs Scout to report, and `main.py` to surface on the per-vehicle fleet
dict, a field such as `active_revision_id` / `active_route_hash` / `mission_changed_at` (the
extension point is already documented in `main.py` `normalize_agent_message`). No parallel API
was invented; the operator-facing frontend already consumes it via `missionRevisionSignal()`.
