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
