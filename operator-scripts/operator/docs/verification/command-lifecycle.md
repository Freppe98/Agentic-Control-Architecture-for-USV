# Stabilized command lifecycle + mission upload verification

Operator half of the stabilized command lifecycle and mission-upload workflow. Extends the
existing queue (`command-lifecycle` builds on `commands.md` / `set-home.md`) without
breaking any existing record: QUEUED/SENT/EXECUTED compatibility is preserved, the Scout
per-type classifiers (`home_result`, `rtl_result`) are retained, and a normalized
verification/lifecycle layer is added on top. All frontend requests still route through
`operator/services/api.js`; pages never call Scout directly.

## Backend (`main.py`)

- **Normalized `source`** (`OPERATOR` / `LOCAL_AGENT` / `MISSION_AGENT`) on every record
  (`normalize_source`, conservative default `OPERATOR`), **forwarded to Scout** in
  `agent_command_view()` (delivered shape is now `{command_id, command_type, source,
  params, expires_at}`).
- **Normalized `verification` block** on every record — the ONE type-agnostic outcome the
  UI reads: `{ verified: true|false|null, outcome: PENDING|VERIFIED|EXECUTED|FAILED|
  REJECTED|EXPIRED, expected, observed, reason }` (`build_command_verification`). Rebuilt
  on every state change (`_refresh_command_derived`, called from create / claim / result /
  expire / cancel).
  - `SET_HOME` → `home_result`, `RTL` → `rtl_result`, `MISSION_UPLOAD`/`MISSION_CLEAR` →
    `mission_result` (dedicated classifiers).
  - Any other command: `verified` comes from `result.verified` when Scout reports it —
    **EXECUTED + `verified:false` renders as FAILED** — else `null` (a plain mode/arming
    EXECUTED stays a plain success; AUTO/MANUAL/LOITER/HOLD/ARM/DISARM/PAUSE/RESUME
    back-compat is intact).
  - Conservative for unknown/older records: an `RTL`/`SET_HOME`/`MISSION_*` EXECUTED with
    no per-type field reads **unverified**, never an optimistic green.
- **Retained lifecycle array** `lifecycle` = backend queue stages (QUEUED/SENT/terminal
  with timestamps) merged with Scout's own `result.lifecycle` (kept verbatim on
  `scout_lifecycle`). **Structured `error`** captured from `result.error`.
- **`MISSION_UPLOAD` / `MISSION_CLEAR`** added to `COMMAND_TYPES` + `CONFIRM_REQUIRED_TYPES`
  (+ `RISK_WARNING`). `_annotate_mission_upload_result` verifies a write ONLY on
  accepted + verified + matching read-back count (and hash when both present); a
  mismatch / not-accepted / not-verified is `mission_result = "failed"` with Scout's real
  reason — **never "successful" just because the file reached Scout**.
- **Command events carry structured `detail`** (`command_id`, `command_type`,
  `command_source`, `stage`, `outcome`, `verified`, `expected`, `observed`, `reason`) for
  the Events page (`_command_event` / `_append_event(detail=…)`).

## Frontend (adapter isolated in `lib/command.js` + `lib/mission-upload.js`)

- `lib/command.js` `commandVerification(cmd)` reads the backend's normalized block when
  present and otherwise recomputes the identical decision from the per-type fields — so a
  minor Scout schema change is a one-file change. Also `commandSource`, `commandStages`,
  `outcomeFrom`. **Map and Vehicle both call this one function → result parity by
  construction** (`tests/command.test.mjs`).
- `lib/mission-upload.js` — `parseMission` (canonical waypoint JSON **or** GeoJSON
  Point/LineString → count, first/last, deterministic `wpm1:` hash, validation),
  `missionUploadParams`, `missionUploadStage` (Requested → Accepted → Executing →
  Verified/Failed), `missionUploadCompare` (expected vs observed count/hash).
- `services/api.js` — `uploadMission(id, params)` / `clearMission(id)` route through
  `createCommand` (`confirm:true`, forwards `source`). `getEvents`/`getEventLog` pass the
  structured `detail` through.

## Pages

- **Map** — "Last command" now shows the full compact progression (`requested › sent ›
  confirmed`) plus the normalized terminal pill (VERIFIED / FAILED / REJECTED / …) and
  expected→observed, from the shared `commandVerification`.
- **Vehicle** — detailed command history rows: type + source chip + normalized outcome
  pill, lifecycle **stage timestamps**, **expected vs observed** state, and the structured
  failure reason.
- **Events** — command events render structured detail (id, type, source, stage,
  verification outcome pill, expected→observed); new **Commands** filter chip.
- **Agent** — "Latest action" line under Recent Transitions shows the most recent command's
  type + Scout-reported result (executed / blocked / failed / pending). Sourced only from
  emitted command events — no invented reasoning.
- **Mission** — new **Upload** tab: paste/file a GeoJSON or waypoint-JSON mission → preview
  (count, first/last, expected hash) → `Upload to Pixhawk` (confirm, OPERATOR-gated,
  duplicate-suppressed while active) → progress track → after verified, re-fetch the
  Pixhawk mission and show **expected vs observed count/hash**. `Clear Pixhawk mission`
  gets the same confirm + read-back. Normal read-back stays on Overview. No waypoint
  jumping.

## Manual browser verification

1. `uvicorn main:app --reload`, open `/app` → **Mission** → **Upload**.
2. Take OPERATOR control on **Map** or **Vehicle** first (Upload is disabled otherwise,
   with a note).
3. Paste `[{"seq":0,"lat":56.70,"lng":13.00},{"seq":1,"lat":56.71,"lng":13.01}]` → **Validate
   & preview** shows format `waypoints`, 2 waypoints, first/last, an expected `wpm1:` hash.
4. **Upload to Pixhawk** → confirm. The button is suppressed while active. The progress
   track advances Requested → Accepted → Executing → Verified/Failed as Scout reports
   results; a verified upload re-fetches the Pixhawk mission and shows the count/hash compare
   (or a MISMATCH verdict). A transport-only EXECUTED without accepted/verified reads Failed.
5. **Vehicle** → Control card: command history shows lifecycle timestamps + expected/observed.
   **Map**: "Last command" shows the progression + terminal pill. **Events** → Commands
   filter: structured command rows. **Agent**: "Latest action" reflects executed/blocked.
6. GeoJSON: paste a `FeatureCollection` of `Point` features (coordinates `[lng, lat]`) — the
   preview un-swaps them to lat/lng.

## Scout-contract assumptions still to confirm

- **Mission hash agreement** — the expected hash is computed operator-side (`wpm1:` FNV-1a
  over `[seq,command,lat(7dp),lng(7dp),alt(2dp)]`). Hash-level read-back verification is
  only meaningful if Scout's `GET /agent/pixhawk_mission` reports a hash computed with the
  **same** canonicalisation. Until confirmed, the compare falls back to **count** match
  (still honest — never claims a hash match it can't prove).
- **`MISSION_UPLOAD` result shape** — assumed `{accepted, verified, observed_count,
  observed_hash?, error?, lifecycle?}` (tolerant of `count`/`mission_count`,
  `hash`/`mission_hash` spellings). Fine-grained `EXECUTING` progress relies on Scout
  emitting a `result.lifecycle` array; without it the track shows Accepted, not Executing.
- **`source` on delivery** — Scout must tolerate the added `source` field on
  `GET /agent/commands` (additive; existing agents ignore unknown fields).
