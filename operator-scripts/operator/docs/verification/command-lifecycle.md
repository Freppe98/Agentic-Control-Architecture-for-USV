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
- `lib/mission-upload.js` — `parseMission` (route-waypoint JSON **or** GeoJSON
  Point/LineString → route count, Pixhawk item count, first/last, validation),
  `missionUploadParams`, `missionUploadStage` (Requested → Executing → Verified/Failed),
  `missionUploadCompare` (expected vs observed route count / Pixhawk item count / route
  content hash), `liveUploadMatches`. **No hash is computed here** — see
  `mission-contract-v1` below.
- `services/api.js` — `uploadMission(id, params)` / `clearMission(id)` route through
  `createCommand` (`confirm:true`). **No `source` argument** — provenance is server-owned.
  `getCommandCapabilities()` reports which command types the backend can deliver today.
  `getEvents`/`getEventLog` pass the structured `detail` through.

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
- **Mission** — **Upload** tab: paste/file a GeoJSON or route-waypoint JSON route → preview
  (**Route waypoints: N**, **Pixhawk items after upload: N+1 including Home**, first/last)
  → `Upload route to Pixhawk` (confirm, OPERATOR-gated, duplicate-suppressed while active)
  → progress track driven by Scout's live `agent.mission_upload` → after verified, re-fetch
  the Pixhawk mission and show **expected vs observed route count / Pixhawk item count**.
  `Clear Pixhawk mission` is enabled under ordinary command gating and is verified by an
  independent empty read-back. Normal read-back stays on Overview. No waypoint jumping.

## Manual browser verification

1. `uvicorn main:app --reload`, open `/app` → **Mission** → **Upload**.
2. Take OPERATOR control on **Map** or **Vehicle** first (Upload is disabled otherwise,
   with a note).
3. Paste
   `{"contract_version":"mission-contract-v1","waypoints":[{"latitude":56.6501,"longitude":12.8701,"loiter_time_s":0},{"latitude":56.6512,"longitude":12.8725,"loiter_time_s":0}]}`
   → **Validate & preview** shows format `waypoints`, **Route waypoints: 2**, **Pixhawk
   items after upload: 3 including Home (seq 0, Scout-owned)**, first/last, and
   **Expected route content hash** abbreviated to `sha256:5fe4c2352fc9…` (hover for the full
   value; it must read
   `sha256:5fe4c2352fc9183e121538a8e199131159cdda66658ccb755c7db1ff54672bfd`). The hash comes
   from the backend via `POST /api/missions/preview` — the browser never computes it.
4. Paste the OLD schema `[{"seq":0,"command":16,"lat":56.70,"lng":13.00,"alt":0}]` → it is
   **rejected**, naming `seq`, `command` and `alt` as Scout-owned. Nothing is uploaded.
5. **Upload route to Pixhawk** → confirm (the dialog states both counts). The button is
   suppressed while active. The progress track advances Requested → Executing → Verified/
   Failed: **Executing** appears while Scout's `agent.mission_upload.active` is true *and*
   its `command_id` matches this command. A verified upload re-fetches the Pixhawk mission
   and shows the count compare (or a MISMATCH verdict). A transport-only EXECUTED without
   accepted/verified reads Failed.
6. `Clear Pixhawk mission…` is **enabled** (given OPERATOR control and no upload in flight).
   Its confirm dialog states: the stored route will be removed; the operation is rejected
   while armed or in AUTO; Home may remain as Pixhawk item 0; and success comes from a fresh
   read-back, not from sending MISSION_CLEAR_ALL. After a verified clear the Pixhawk mission
   is re-fetched and the empty representation (`NO_ITEMS` or `HOME_ONLY`) is shown.
7. **Vehicle** → Control card: command history shows lifecycle timestamps + expected/observed
   (as **Pixhawk items**). **Map**: "Last command" shows the progression + terminal pill.
   **Events** → Commands filter: structured command rows. **Agent**: "Latest action"
   reflects executed/blocked.
8. GeoJSON: paste a `FeatureCollection` of `Point` features (coordinates `[lng, lat]`) — the
   preview un-swaps them to lat/lng and defaults `loiter_time_s` to 0.
9. Provenance: `curl -X POST /api/commands -d '{"vehicle_id":2,"type":"SET_MODE_LOITER",
   "confirm":true,"source":"MISSION_AGENT"}'` → the returned record reads
   `"source": "OPERATOR"`. A browser cannot attribute its command to the autonomy.

## mission-contract-v1 — ownership

The **operator supplies route waypoints only**. **Scout owns Pixhawk sequence 0 / Home** and
prepends it, so `Pixhawk item count = route waypoint count + 1`. Nothing operator-side emits
a `seq`, MAVLink `command`, `frame` or `altitude`; a file supplying them is rejected rather
than previewed, because Scout would discard them and the operator would have approved a
mission that is not the one uploaded.

## Scout-contract assumptions still to confirm

- **Route content hash — IMPLEMENTED.** The **Operator backend** is the authoritative
  calculator (`mission_contract.route_content_hash`); there is deliberately no frontend
  implementation, and none may be reintroduced — a second calculator is a second thing that
  can drift from Scout. Canonicalization: route items only (Home excluded), 1-based
  `sequence`, fixed `MAV_CMD_NAV_WAYPOINT` / `MAV_FRAME_GLOBAL_RELATIVE_ALT`, lat/lng to 7
  dp, altitude `0.0`, `loiter_time_s` → `param1` to 3 dp, `param2..4` `0.0`, then
  `json.dumps(sort_keys=True, separators=(",", ":"))`, UTF-8, SHA-256, `sha256:` prefix.
  Pinned against Scout's golden hash in `tests/fixtures/mission-contract-v1.json`.
  A missing expected or observed route hash is an explicit verification **failure**, never a
  count-only pass. *(Superseded history: an earlier operator-side `wpm1:` FNV-1a hash was
  locally invented, never computed by Scout, and was removed rather than re-guessed.)*
- **Coordinate/loiter precision — CROSS-SYSTEM ARTIFACT-VERIFIED (no longer an assumption).**
  The golden route carries only 4 decimals and so could never discriminate a coordinate
  precision of 4 from one of 9 — that gap was real and is now closed. A high-precision
  two-waypoint probe (11-decimal coordinates, 5-decimal loiter) was run **independently on
  Scout and on the Operator**, and both produced
  `sha256:125c779021c1521fae67462719cdab588f871c3b44d808b362c0630f221998ad`.
  The digest moves if `COORDINATE_PRECISION` leaves 7 or `LOITER_PRECISION` leaves 3, so the
  agreement is evidence about the rounding itself rather than a relayed specification. Both
  systems' values are recorded separately in the fixture's `high_precision_probe` and
  asserted equal (`TestHighPrecisionProbeIsCrossSystemVerified`).
- **Maximum route waypoints — SCOUT-OWNED (200).** mission-contract-v1 defines and enforces
  `MAX_ROUTE_WAYPOINTS = 200`; Scout refuses an oversized mission with the structured error
  `{code: "MISSION_TOO_LARGE", maximum_route_waypoints, observed_route_waypoints}`.
  `main.MAX_ROUTE_WAYPOINTS` **mirrors** that number — it is not an independent Operator
  judgement and must not be tuned locally; if Scout's limit changes, this constant follows.
  It is enforced inside `canonical_mission_upload_params`, the one function both
  `POST /api/missions/preview` and `POST /api/commands` call, so a route that previews can
  never be refused on send. Published as `max_route_waypoints_source: "scout-contract"` on
  `/api/commands/capabilities`.
  **Why the Operator validates too, given Scout enforces it:** local rejection fails the
  mission at *preview* — before anything is queued and before a byte reaches the vehicle —
  rather than after a round trip. Scout remains the **authority**: its refusal is final, and
  a `MISSION_TOO_LARGE` it returns is rendered from Scout's own two numbers
  (`main.mission_error_text` / `missionErrorText`), never padded with generic wording and
  never back-filled from the local constant when Scout omits a count.
- **Preview is read-only by construction.** `POST /api/missions/preview` creates no command,
  appends no event, and mutates no authority or vehicle state; it calls only the shared
  canonicalizer. A browser-supplied `expected_route_content_hash` (or either expected count)
  is **refused**, never echoed — a caller that could supply the expected hash would be
  choosing the value its own upload is later "verified" against.
- **`MISSION_UPLOAD` result shape** — `{accepted, uploaded?, verified,
  observed_route_waypoint_count, observed_pixhawk_item_count, observed_route_content_hash,
  error?}` (tolerant of `route_waypoint_count` / `pixhawk_item_count` / `observed_count` /
  `count` spellings). `uploaded` is checked only when present. Scout's **full-mission**
  `hash` / `full_mission_hash` is never compared against the route hash — it includes the
  Home the operator never sent, so it is a different value over different bytes.
- **`MISSION_CLEAR` result shape** — `{contract_version, accepted, cleared, verified,
  observed_pixhawk_item_count, observed_route_waypoint_count, empty_representation,
  acknowledgement, error}`. Verified requires `accepted` + `cleared` + `verified` +
  `observed_route_waypoint_count == 0` + `empty_representation` ∈ {`NO_ITEMS`, `HOME_ONLY`}.
  The Pixhawk item count is **not** required to be 0 — ArduPilot may keep Home at seq 0.
- **`agent.mission_upload`** — assumed `{active, state, command_id, elapsed_s}` in the
  status payload's `agent` group, with `command_id` equal to the operator's command id.
  Progress is mapped **only** on an id match; without the group the track shows Requested
  until the terminal result lands. Scout is **not** required to post an intermediate
  ACCEPTED command result (the backend redelivers nonterminal commands, so it would simply
  be redelivered).
- **Independent read-back** — after Scout reports a terminal verified result, the Operator
  fetches `GET /api/vehicles/{id}/pixhawk-mission` **again**, as its own second observation.
  Scout verifying its own write is Scout marking its own homework, so the UI reports
  **Verified** only once that independent fetch agrees. While it is outstanding the state is
  *Awaiting independent readback* (**never** Failed — a Failed that flickers on every
  successful upload teaches the operator to discount the real one); if it cannot be obtained
  the state is a **caution** (*Scout verified; independent Operator readback unavailable*),
  not a green result; if it disagrees with Scout the state is a high-severity
  **verification conflict**.
- **`source` on delivery** — Scout must tolerate the `source` field on
  `GET /agent/commands` (additive; existing agents ignore unknown fields). It is always
  `OPERATOR` for records created through the browser endpoint.
