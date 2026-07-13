# Pixhawk mission readback (Map page)

View-only fetch of the mission **currently stored on a vehicle's Pixhawk** and its
overlay on the map. Primarily a testing/verification feature. It is deliberately a
separate axis from `mission_state`/coverage progress and from the operator command
queue: this is what the flight controller actually holds, read live from Scout.

## Card (left dock, between the vehicle roster and Mission Progress)
`PIXHAWK MISSION` compact card (`operator/pages/Map.js` → `renderPxm`):

```
PIXHAWK MISSION            [status chip]
Loaded        12 waypoints
Current       WP 4 / 12
Last download 8s ago
Mission id    a1b2c3d4        (only when Scout sends a hash)
<cached-mission note, when applicable>
[ Fetch / Refresh ]
[ Show ] [ Center ] [ Hide ]
```

- **Fetch/Refresh** hits Scout (via the operator proxy). Label is *Fetch* before the
  first successful fetch, *Refresh* after.
- **Show / Center / Hide** are pure client-side view controls over the already-cached
  mission — **none of them refetch** (they never touch Scout):
  - **Show** draws the overlay (enabled only when the cached mission has positioned
    waypoints and the overlay is hidden).
  - **Center** fits the map to the mission bounds (`map.fitBounds`, no refetch), turning
    the overlay on first if hidden. Enabled whenever there are positioned waypoints —
    including while offline against the cached mission.
  - **Hide** removes the overlay (enabled only when shown).
- **Last download** is the age of the last *successful* readback and ticks live (1 s
  interval, `tickPxmAge` → `fmtSince`, `s`/`m`/`h`).

### Cached mission is never discarded
Per-vehicle state (`pxm[id]`) keeps the last **successful** (`reachable`) readback in
`s.mission`. A later failed fetch (unreachable / thrown) only sets `s.note` — it never
wipes `s.mission`. The card then keeps showing the cached counts and a distinct note:

> **Showing last downloaded mission — not re-confirmed with Scout.**

so the operator can always tell **communication status** (the chip) apart from
**cached-mission availability** (the grid + note). The cached mission stays
**showable / centerable while offline**.

### Status chip states (current comm/fetch status only)
| Chip | Condition |
|---|---|
| `Fetching…` (dim) | request in flight |
| `Loaded` (green) | reachable, ≥1 waypoint |
| `No mission loaded` (dim) | reachable, 0 waypoints (or Scout `loaded:false`) |
| `Partial download` (amber) | Scout flagged partial, or fewer items than the reported count |
| `Mission invalid` (amber) | Scout `valid:false` |
| `Scout unavailable` (amber) | Scout API configured but unreachable (cache, if any, preserved) |
| `No mission API` (dim) | vehicle has no Scout API configured (ids 1, 3) |
| `Fetch failed` (amber) | thrown request (e.g. 404 unknown vehicle) |
| `Not fetched` (dim) | nothing fetched yet |

### Optional Scout integrity fields (consumed, never invented) — mission-compare prep
`mission.hash` / `mission.loaded` / `mission.valid` are read **only if Scout sends
them**. `hash` → a `Mission id` line (first 8 chars, full value in the title) that a
later **Compare against operator mission** can key off; `loaded:false` → `No mission
loaded`; `valid:false` → `Mission invalid` chip + a caution note. Nothing is fabricated
when the fields are absent. The backend proxy passes these through verbatim and computes
**none** of its own (Scout stays the sole mission owner).

## Map overlay (`drawMissionOverlay`)
- Numbered waypoint markers — **rounded squares** (`.wp-marker`), distinct from the
  round comm-colored vehicle dot; the **current** waypoint is green with a ring
  (`.wp-marker.cur`).
- Dashed connecting polyline in seq order.
- Popup per waypoint: sequence, latitude, longitude, command (name), altitude, and
  loiter time when available (Leaflet popup shell dark-themed).
- Only waypoints with a real position are plotted — a positionless item (e.g. `RTL`)
  is **never** drawn at 0,0.
- The overlay is a single Leaflet layer group; it survives map pans/zooms and the 2 s
  fleet-marker refresh (which never touches it).
- **Changing the selected vehicle switches overlays** (`syncMissionOverlay` in
  `select`): the old overlay is dropped and the new vehicle's is redrawn only if the
  operator had it shown.

## Backend (`main.py`)
`GET /api/vehicles/{vehicle_id}/pixhawk-mission` — a live, synchronous proxy to Scout's
`GET /agent/pixhawk_mission`, exactly the `control_authority` pattern. The operator
backend holds **no** mission state of its own. Three honesty cases:

| Case | Response |
|---|---|
| unknown vehicle id | JSON **404** |
| known, no Scout API | 200 `available:false` (nothing to read) |
| known, Scout configured | live proxy; `reachable:false` + empty list if Scout does not answer — never a console 5xx |

`normalize_mission_item` tolerates field spellings (seq/sequence, command/cmd, lat/x…)
and accepts both float degrees and MAVLink `int1e7` x/y (`_mission_coord` divides by
1e7 when `|v|>180`). `loiter_time` comes from `param1` for `LOITER_TIME` (cmd 19).
`partial` is set if Scout flags it or the list is shorter than the reported `count`.
`MAV_CMD_NAMES` maps common codes to readable names (unmapped → `CMD <n>`).
`hash` / `loaded` / `valid` are **passed through only when present** in Scout's reply —
the proxy never computes a hash or validity of its own (no duplicated mission ownership).

## Verification (2026-07-12)
Backend run with vehicle 2 (Scout) pointed at a mock emitting a 12-item lawnmower
mission (MISSION_ITEM_INT scaled ints, a `LOITER_TIME` at seq 4 = current, a final
positionless `RTL`, plus `hash/loaded/valid`).

Endpoint:
- `GET /api/vehicles/2/pixhawk-mission` → `count:12`, `current_seq:4`, `LOITER_TIME`
  with `loiter_time:30.0`, `RTL` with `lat/lng:null`, int1e7 → correct degrees,
  `hash:"a1b2c3d4e5f60718"`, `loaded:true`, `valid:true` passed through.
- `GET /api/vehicles/1/pixhawk-mission` → `available:false` (no Scout API).
- `GET /api/vehicles/9/...` → **404**.
- Scout down → `available:true, reachable:false, count:0, reason:"…unreachable"`.

UI (Playwright, 1500×900, `#/map`):
- Card renders in the dock between roster and Mission Progress; matches the existing
  card language. `Mission id a1b2c3d4` shown from the pass-through hash.
- Fetch → `Loaded / 12 waypoints / WP 4 / 12 / 1s ago`; buttons become
  `Refresh` + `Show / Center / Hide`.
- Show → 11 numbered markers (12 minus the positionless RTL) + dashed polyline; WP 4
  green with ring; vehicle dot stays distinct.
- **Center** → `map.fitBounds` frames the mission (no refetch); overlay persists.
- **Zoom out ×2** → markers stay fixed 22 px and readable; overlay intact (11 markers).
- Waypoint popup shows seq 4, lat 56.700493, lng 13.002748, `LOITER_TIME`, 2 m, 30 s.
- **Repeated vehicle switching** (4× USV-1 → USV-3 → Scout): marker count is exactly
  `0 / 0 / 11` every round with **no accumulation** — `document` holds 11 `.wp-marker`
  nodes at the end. Layer cleanup (`clearMissionOverlay` drops the layer group **and**
  closes any popup) is perfect; switching auto-switches overlays.
- Switch to USV-1 → overlay auto-removed (0 markers), card `No mission API`,
  Show/Hide disabled.
- **Cached mission survives Scout going down**: fetch (mock up) → cache populated; mock
  killed; Refresh → `SCOUT UNAVAILABLE` chip, but `Loaded 12 waypoints / WP 4 / 12`
  and `Mission id` remain, with the note *"Showing last downloaded mission — not
  re-confirmed with Scout."* Center stays enabled (mission still framable offline).
  Nothing discarded.
- **No console errors** across the whole flow.

## Overlay polish + HOME handling (2026-07-13)

Second pass: turn the overlay from a debug visualization into a **discreet operational
overlay**, and handle the real Pixhawk **sequence-0 home / current-location item** so
it never distorts the route or the Center fit.

### Files changed
| File | Change |
|---|---|
| `operator/pages/Map.js` | `classifyMission()` splits the mission into `{ home, route }` (`frameIsAbsGlobal` + `METERS` haversine); route polyline & `centerMission` bounds now use **route only** (HOME excluded); separate `homeIcon` marker drawn beneath the vehicle (`zIndexOffset:-1000`); markers shrunk + number moved to its own `.wp-num` span; `applyMissionZoom()` toggles `.mission-faded` on `zoomend` (no layer rebuild); richer `wpPopup` (HOME/Waypoint type, frame, `CURRENT` tag) with `cmdLabel()` command-string fallback; card gains a Scout-only `Integrity` badge and a `Home (seq N)` current line. |
| `operator/styles/theme.css` | `.wp-marker` smaller/translucent + hover contrast; `.home-marker` (distinct home glyph); `.mission-faded` rules (hide `.wp-num`, shrink dots) for wide zoom; `.wp-cur-tag`, `.pxm-integ` badges. Polyline restyled inline (weight 1.6, opacity .6, dashed). |
| `main.py` | **none** — the proxy already forwards `frame` and leaves `command` as Scout's string with `command_name:null`; all HOME/route logic is client-side and never rewrites Scout data. |

### Root cause — Center zoomed out too far / route crossed the map
The real mission's **seq 0 is a home/current-position item at the vehicle**, ~12 km from
the survey cluster, and it was treated as an ordinary waypoint. Two consequences:
1. **Bounds** — `centerMission` fit *all* positioned waypoints, so the fit box spanned
   the whole Scout→survey transit and the survey shrank to a corner.
2. **Route** — the polyline ran seq 0 → seq 1, drawing a long transit leg across the map.

Fix: `classifyMission` detects the home item by **frame mismatch** (`MAV_FRAME_GLOBAL`
vs the survey's `MAV_FRAME_GLOBAL_RELATIVE_ALT`) **or** as a **geographic outlier**
(distance to the cluster centroid > max(3×cluster-span, 400 m)). The home item is then
excluded from both the polyline and the Center bounds, and rendered as its own HOME
marker. A seq 0 that genuinely is a normal leg (same frame, inside the cluster) is left
as an ordinary route waypoint — nothing is mis-split.

### Real-Scout verification
Scout's `GET /agent/pixhawk_mission` lives on the vehicle network (`10.0.2.10:8080`),
unreachable from the dev box, so the run used a mock reproducing the **exact real
response shape** the task described: 14 items, seq 0 `MAV_FRAME_GLOBAL` at the current
position, seq 1..13 `MAV_FRAME_GLOBAL_RELATIVE_ALT` near 56.679, 12.811, `current_seq:0`,
**`command` as strings** (`"MAV_CMD_NAV_WAYPOINT"`) with **`command_name:null`**,
`valid:true`, `hash`. Proxy output confirmed: `count:14`, seq 0 `frame:"MAV_FRAME_GLOBAL"`,
strings passed through, `command_name:null`, `hash:"9f3ac71b2e6d480af12c"`.

Playwright (1500×900, `#/map`, Scout selected). Results vs the required checks:

| # | Check | Result |
|---|---|---|
| 1 | Fetch shows 14 waypoints | ✅ card `Loaded 14 waypoints`, `Current Home (seq 0)`, `Integrity VALID`, `Mission id 9f3ac71b` |
| 2 | Show renders the survey discreetly | ✅ 13 small numbered markers + thin dashed line ([04](img/04-center-survey.png)) |
| 3 | HOME visually separate | ✅ distinct home glyph, **not** numbered, not joined to WP 1 ([02](img/02-show-home-centered.png)) |
| 4 | Center zooms to the survey, not the transit | ✅ `fitBounds` over route-only frames the 13-point survey; home excluded |
| 5 | Vehicle marker dominant | ✅ home drawn beneath vehicle; 40 px comm dot over the 22 px home glyph ([02](img/02-show-home-centered.png)) |
| 6 | Repeated Show/Hide/Center → no duplicates | ✅ after 4× cycles: `13` wp-markers / `1` home-marker |
| 7 | Vehicle switching removes/restores overlay | ✅ USV-1 → `0/0`; back to Scout + Show → `13/1` |
| 8 | No console errors | ✅ 0 across the whole flow |
| 9 | Popup shows command text when `command_name` null | ✅ `Command WAYPOINT` (string fallback) on both HOME and survey popups ([05](img/05-popup-survey.png), [03](img/03-popup-home.png)) |

Extra confirmed: wide zoom drops the numbers to dots (`.mission-faded`, `wp-num`
hidden) with the same 13 markers — **no layer rebuild** ([06](img/06-zoomed-out-both.png));
survey popup (seq 7) shows `Frame GLOBAL_RELATIVE_ALT` + `Loiter 20 s`; HOME popup shows
`HOME · CURRENT`, `Type Home`, `Frame GLOBAL`.

Screenshots: [before/default](img/01-before-default.png) ·
[Show — home + vehicle](img/02-show-home-centered.png) ·
[HOME popup](img/03-popup-home.png) · [Center — survey](img/04-center-survey.png) ·
[survey popup](img/05-popup-survey.png) · [wide zoom](img/06-zoomed-out-both.png).

## Still dependent on Scout
Scout's Flask service (`motherpi/services/flask`) must expose
`GET /agent/pixhawk_mission` (download the mission over MAVLink and return
`{count, current_seq, waypoints[], partial}` or a raw item list). That route is **not**
in this repo — only the operator proxy is (same as `control_authority`). Until Scout
ships it, real vehicle 2 reads `Scout unavailable`; the mock above stands in for it.

## Designed for later (no redesign needed)
The card and `pxm[id]` payload are shaped to add **Compare against operator mission**,
**Upload mission**, and **Export mission** against the same fetched waypoints — extra
buttons in `.pxm-actions` / `.pxm-btns` and actions over `pxm[selId].mission`, no
structural change. The optional `hash` / `loaded` / `valid` fields are already consumed
end-to-end (proxy pass-through → `renderPxm`), so a mission-compare keying off `hash`
needs no new plumbing — only the compare logic itself. Upload is **not** implemented and
mission ownership stays entirely with Scout.
