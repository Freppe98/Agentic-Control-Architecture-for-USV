# Multi-USV current-state isolation — root cause + per-vehicle state model

Branch `feature/operator-plan-route-quality`. With two live USVs (Scout and SAR-001) posting to
the same Operator Station, almost every page alternated between fleet states every few seconds
without the operator touching anything: Scout complete then UNKNOWN, SAR-001 complete then an
empty "USV-3", roles reversing at the packet/poll rate.

## Exact cause (two defects on the same path)

### 1. One global "current state" for the whole fleet

`main.py` held a single `latest_agent_status` (the last packet from *any* vehicle) and a single
`latest_agent_received_at` (when *any* vehicle last posted). `GET /api/fleet/status` built a
hardcoded `FLEET_TEMPLATE` list of UNKNOWN rows and spliced in the ONE normalized row derived
from that global packet:

```python
fleet = [dict(usv) for usv in FLEET_TEMPLATE]
if latest_agent_status:
    live_usv = normalize_agent_message(latest_agent_status)   # the most recent packet only
    ...replace the row whose id matches...
```

So at any instant exactly one vehicle could be populated, and every other vehicle reverted to a
static placeholder. `normalize_agent_message` also derived comm-state from the *global*
`latest_agent_received_at`, so a vehicle's freshness was a function of whenever anyone last
posted — a silent USV looked CONNECTED, and a live one could not age on its own clock.

This also explains the name flip: the live row's name came from the packet (`SAR-001`), the
placeholder row's name came from the template constant (`USV-3`). Same vehicle, two names,
alternating with whoever posted last.

Reproduced against the pre-fix code (Scout `usv_id: 2`, SAR `usv_id: 3`, interleaved 1 Hz):

```
after Scout packet 1 : id=2 Scout   CONNECTED batt=79 | id=3 USV-3   UNKNOWN batt=None
after SAR   packet 1 : id=2 Scout   UNKNOWN   batt=None | id=3 SAR-001 CONNECTED batt=50
after Scout packet 2 : id=2 Scout   CONNECTED batt=78 | id=3 USV-3   UNKNOWN batt=None
after SAR   packet 2 : id=2 Scout   UNKNOWN   batt=None | id=3 SAR-001 CONNECTED batt=49
```

### 2. Identity resolution that could not fail safely

`extract_usv_id` was `int(str(raw).replace("usv-", ""))` with `except: return 2`. Any identity
it could not parse — a callsign such as `"SAR-001"` — silently resolved to **Scout's id**. In
that spelling, SAR's telemetry, name, health and mission landed on Scout's record and renamed
Scout to "SAR-001". Reproduced pre-fix (SAR sending `usv_id: "SAR-001"`):

```
after SAR packet 1 : id=2 SAR-001 CONNECTED batt=50 lat=56.71   ← Scout's record, SAR's data
```

## Fix

### Canonical identity (`vehicle_registry.py`, `vehicles.json` — new)

One explicit `canonical_id()` used everywhere a vehicle identity enters the backend. Every
spelling of one vehicle folds to one value (`3`, `"3"`, `"usv-3"`, `"USV-3"`, `"SAR-001"` → `3`,
slug `usv-3`); aliases are declared in `vehicles.json`, never inferred, and an alias claimed by
two vehicles is a startup error. Display name is a separate, per-vehicle, sticky field and is
never an identity key. An unresolvable packet is now **rejected with 400**, not merged into
whichever vehicle happens to be first.

### Per-USV current state (`main.py`)

`current_vehicle_state[canonical_id]` — one independent record per vehicle holding
`raw_latest`, `received_at`, `message_timestamp`, `display_name` and views onto that vehicle's
last-known telemetry/agent groups. A packet updates exactly one entry. The monotonic replay
guard, the arrival timestamp, the stale/partitioned/disconnected classification and the
last-known caches are all per USV. `GET /api/fleet/status` is assembled from **all** records
every time, each normalized independently against its own `received_at`; a configured vehicle
that has never reported gets `comm_state: UNKNOWN`, `contacted: false` and the same full field
set, so live data updates that record rather than adding or replacing a row.

### Frontend

Selection is keyed only by canonical id and never by display name, array position, or "which
vehicle is currently connected":

- `lib/selection.js` — `canonicalVehicleId()` / `sameVehicle()` mirror the backend policy; the
  store keeps a string canonical id verbatim instead of coercing everything to a number.
- `pages/Fleet.js`, `pages/Vehicle.js` — dropped their page-local `selId` (Fleet seeded it from
  `fleet[0]`, i.e. list position) and now read/write the shared store and subscribe to it.
- `pages/Map.js`, `Agent.js`, `Mission.js`, `Pilot.js`, `Vehicle.js`, `Fleet.js` — vehicle-row
  clicks resolve through `canonicalVehicleId(...)` instead of `+el.dataset.id`, which is `NaN`
  for a vehicle whose canonical id is a string.

Polling cadence is unchanged (2 s), and per-USV last-known behaviour (battery `-1`/null does not
erase a valid reading, partial packets do not erase unrelated fields, stale data is marked stale,
positions are never fabricated) is preserved.

### Diagnostics (bounded)

Both `[STATUS]` and `[FLEET]` are change-driven; neither ever prints a payload. Measured live:
**50 status packets + 25 fleet polls produce 3 lines**, not 51.

```
[STATUS] canonical_id=usv-2 source=usv-2   accepted=true comm=CONNECTED mission=EXECUTING (first-contact)
[STATUS] canonical_id=usv-3 source=SAR-001 accepted=true comm=CONNECTED mission=IDLE (first-contact)
[FLEET]  vehicles=3 connected=2 partitioned=0 disconnected=0 unknown=1
```

A `[STATUS]` line is emitted on first contact, on any change to that vehicle's status
signature — `accepted`, rejection reason, `comm_state`, mission state, mode, armed, or the
`source` string that resolved to its canonical id — and otherwise at most once per
`STATUS_HEARTBEAT_SECONDS` (60). Continuously varying telemetry (battery, position, heading)
is deliberately *not* in the signature; that is the UI's job. Dedup state is per canonical id,
so one chatty vehicle can neither suppress nor trigger another's line.

A rejection carries its own evidence, and a repeat of the same rejection is suppressed:

```
[STATUS] canonical_id=usv-3 source=usv-3 accepted=false reason=stale_timestamp \
         prev_ts=1764... msg_ts=1764... delta_s=-5.0 comm=CONNECTED mission=IDLE (change)
[STATUS] canonical_id=usv-3 source=usv-3 accepted=true recovered_after=3 ... (change)
```

The same per-USV counters are readable at any time from `GET /agent/status` →
`vehicles["usv-3"]`: `accepted_packets`, `rejected_packets`, `reject_streak` (0 = accepting
now) and `last_reject` (`reason`, `accepted_ts`, `packet_ts`, `delta_s`, `streak`).
`last_reject` deliberately survives recovery — an episode is normally investigated after it
has already ended.

## Tests

- `tests/test_multi_usv_state.py` (41) — both vehicles complete simultaneously; a later packet
  from either changes nothing on the other; interleaved packets never make a row go UNKNOWN;
  per-USV staleness both ways; per-USV monotonic guard (one vehicle's clock cannot block
  another); per-USV last-known battery; every identity spelling → one record; no duplicate
  USV-3/SAR-001 rows; a callsign packet cannot land on Scout; an unidentified packet is
  rejected; a third and a non-numeric-id vehicle join with no code change; no cross-vehicle
  writes to mission/authority/agent/health/position; stable ordering; full row contract.
- `tests/multi-usv-selection.test.mjs` (19) — selection survives repeated updates, reordering,
  renaming, staleness and alternating "who is connected"; no auto-select of first/newest;
  per-USV telemetry caches independent; nothing keyed by display name; pages share one
  canonical selected id; a fourth USV needs no branch.
- `tests/test_status_logging.py` (22) — bounded [STATUS]: first contact logs once, 30 unchanged
  packets log nothing, a steady two-USV stream is 2 lines not 60, mode/mission/comm/identity
  changes log, heartbeat at the configured rate, accepted→rejected logs with evidence, a
  repeated identical rejection is suppressed, rejected→accepted logs `recovered_after`, dedup
  state is per USV, unidentified packets are rate-limited, [FLEET] stays change-driven, and the
  per-USV rejection counters are inspectable on `GET /agent/status`.
- `tests/fleet-sort.test.mjs` (19) — default order stable across 20 alternating updates,
  unaffected by age/battery/comm changes and by response reordering; explicit age/battery sorts
  work and persist; canonical-id tie-breaker in both directions; sub-second jitter cannot
  reorder; mixed string/numeric ids are a total order; sorting does not mutate shared state.
- Baselines: backend **443 pass**, frontend **360 pass**, `check_operator_baseline.ps1`
  **BASELINE PASS**.

## Manual two-USV verification

1. Start the Operator backend: `./run_operator_backend.ps1`.
2. Start the Scout Local Agent; then start the SAR Local Agent.
3. `curl http://127.0.0.1:8000/api/fleet/status` — both `usv-2` and `usv-3` are present and
   CONNECTED **in the same response**, each with its own battery/mode/position.
   `[FLEET] vehicles=3 connected=2 …` appears once in the backend log, not per poll.
4. Open `/app#/map`, select Scout, leave it for ≥ 1 minute.
5. Selection never changes; Scout's data never alternates with UNKNOWN; SAR's row updates
   independently in the dock.
6. Select SAR-001 and repeat — it stays selected and complete, and Scout keeps updating.
7. Stop the Scout agent only: Scout ages CONNECTED → PARTITIONED (>15 s) → DISCONNECTED (>30 s)
   showing last-known values marked stale; SAR stays CONNECTED and complete throughout.
8. Restart Scout: it returns to CONNECTED with no effect on SAR.
9. Stop SAR only and verify the inverse.
10. Issue a command with Scout selected and confirm `GET /api/commands/usv-3` stays empty while
    `GET /api/commands/usv-2` shows it (`vehicle_id: 2`) — routing follows the canonical
    selected vehicle, not the display name.
11. Switch pages (Map → Fleet → Vehicle → Mission): all show the same selected vehicle, and each
    vehicle's mission overlay/readback stays its own.
12. **Fleet ordering** — open `/app#/fleet` and watch for ≥ 1 minute without touching anything:
    the rows stay `USV-1`, `Scout`, `SAR-001` and never swap, including while one vehicle is
    stale and the other is live. Click **Last Contact**: it sorts by age and stays sorted that
    way across polls (a repeat click reverses it). Click **Vehicle** to return to id order.
    Select a row — it highlights where it is and does not move.
13. **Log volume** — with both agents streaming at ~1 Hz, the backend terminal is quiet:
    two `[STATUS] … (first-contact)` lines, one `[FLEET]` line, then nothing until something
    actually changes (a mode/mission/comm change, a rejection, or the 60 s heartbeat).
    50 packets + 25 polls produced 3 lines in measurement.
14. **Rejection evidence** — if a vehicle shows `accepted=false`, read
    `GET /agent/status` → `vehicles["usv-3"]`. `reject_streak > 0` means it is still being
    rejected; `last_reject.delta_s` and `packet_ts` identify which of cases A–D above it is
    (see the table). No action is needed for case D.

## Follow-up: Fleet rows swapping position

**Cause.** Not a state problem — a sort problem, entirely in the frontend. `Fleet.js` opened
with `sort = { key: "age", dir: -1 }`: order by **last contact**, recomputed on every 2 s poll.
Scout and SAR-001 post at slightly different moments, so their `last_seen_age_s` values crossed
and the rows traded places on their own. Two smaller defects rode along: the "Vehicle"
comparator did `a.id - b.id`, which is `NaN` for a string canonical id (an undefined ordering),
and there was no tie-breaker at all, so equal values (two null batteries, two vehicles with no
health signal) left order to whatever the response happened to contain. The backend response
order was already stable, and `Array.sort` was already applied to a copy — `fleet` itself was
never mutated.

**Fix.** `operator/lib/fleet-sort.js` (new, pure and unit-tested):

| | before | after |
|---|---|---|
| default order | `last_seen_age_s` descending — changes with traffic | canonical id ascending (`usv-1`, Scout `usv-2`, SAR-001 `usv-3`) |
| tie-breaker | none | canonical id, always ascending, in every sort |
| id comparison | `a.id - b.id` (`NaN` for a string id) | `compareVehicleIds` — a total order over mixed number/string ids |
| live-value sorts | raw values | quantized (whole-second age, integer battery) so sub-quantum jitter cannot reorder |
| absent values | coerced to `-1`, so they flipped ends on reverse | sort last in **both** directions |

Operator-chosen sorts still work — clicking Last Contact, Battery, Comms or Health sorts by
that column and stays active until changed; a repeat click toggles direction. Selection is not
an input to any comparator, so a selected row never jumps. `sortFleet` returns a copy, so the
records shared with the rollup, the counts and the telemetry cache are untouched.

## Follow-up: why SAR's packets were `accepted=false` for a period

**Reason: the monotonic guard's high-water mark, working exactly as designed.**
`latest_msg_ts_by_id[vid]` only ever moves forward, and only on an accepted packet. Once it
holds `T`, every packet from that vehicle with `timestamp < T` is rejected until its own clock
passes `T` again — a bounded window that ends without intervention. That is precisely the
observed shape: SAR rejected repeatedly, then recovering on its own, with Scout unaffected
because the mark is strictly per USV.

Four conditions produce that signature. Each was driven against the real endpoint:

| # | condition | verdict pattern | distinguishing evidence |
|---|---|---|---|
| A | SAR's OS clock stepped **backwards** (a companion Pi has no RTC: it boots on a restored time and NTP later corrects it — backwards if the boot estimate was ahead) | `AAARRRAA` | `delta_s` ≈ the size of the correction; `packet_ts` epoch-scale |
| B | Local Agent restarted with a **non-wall-clock base** (e.g. `time.monotonic()` = seconds since boot) | `AAARRRRR` | `packet_ts` small (< 10⁶) while `accepted_ts` is also small; recovery takes as long as the previous session ran |
| C | one **future-dated outlier** poisoned the mark | `AARRRA` | `accepted_ts` ahead of wall clock |
| D | **store-and-forward drain** — on reconnect the newest packet arrives first, then the buffered backlog flushes | `ARRRRAA` | `delta_s` small negative (seconds), self-clearing |

**Not the cause: a backend restart.** Verified directly — after a restart `latest_msg_ts_by_id`
is empty, so `prev_ts is None` and the first packet is accepted however old it is, re-seeding
the mark. A restart alone cannot start a rejection streak. Pinned by
`test_a_backend_restart_alone_cannot_cause_a_rejection_streak`.

**Rejected content still updates only receive freshness**, as intended and verified: the
arrival refreshes `last_seen`/comm-state for that vehicle (a packet reaching us proves the link
is carrying data now), while the stale content is refused — battery, mode and position keep
their last accepted values, and no other vehicle is touched.

**No behaviour change was made**, per the instruction not to implement timestamp-reset handling
without proof from the real payload, and not to weaken replay protection. What was added is the
evidence needed to tell the four cases apart from a single episode: `delta_s`, `packet_ts`,
`accepted_ts` and `streak` on the log line and on `GET /agent/status`. **D is most likely** if
SAR had a link interruption before the episode — it is the guard doing its job and needs no
change. If the live `delta_s` instead shows B (`packet_ts` in the thousands rather than
~1.7 × 10⁹), the correct fix is a Local-Agent session/boot id or per-session sequence numbers so
a restart is recognised as a new session rather than a replay — a payload change, not a looser
guard. If it shows C, the safe fix is refusing to advance the mark beyond wall clock plus a
tolerance, which tightens the guard rather than relaxing it.

## Adding another USV

Add one entry to `operator-scripts/vehicles.json` and restart the backend — no code change:

```json
"usv-4": { "id": 4, "display_name": "Guardian", "aliases": ["GUARDIAN-4"] }
```

A vehicle that reports an id not in the file is still accepted and appears as a discovered
fleet member (slug `usv-4`, or e.g. `probe-alpha` for a non-numeric id). Configure it when you
want a stable display name before first contact, or a callsign alias.

That gives **monitoring only.** A registry entry is enough for live telemetry because the
vehicle *pushes* it to us (`POST /agent/status`), which needs no address at all. It grants no
outbound reach: control authority, Pixhawk mission read-back and experiment control require a
second, independent thing — a **verified** route in `VEHICLE_API_BASE` (`main.py`). Do not
claim a vehicle is commandable merely because it appears in `vehicles.json`.

### The two maps, side by side

| | `vehicles.json` / `REGISTRY` | `VEHICLE_API_BASE` (`main.py`) |
|---|---|---|
| Answers | *who exists*, how their packets resolve, what operators call them | *where we reach them* for vehicle-local API calls |
| Direction | inbound — the vehicle **pushes** telemetry to us | outbound — this station **pulls from / posts to** the vehicle |
| Needs an address | no | yes, a real **verified** one |
| Enables | fleet presence, selection, telemetry, comms state, the command **queue** | control authority read/write, Pixhawk mission read-back, experiment control |
| Missing entry means | vehicle is unknown / not pre-registered | endpoints answer an honest `available:false` (a supported state) |

Both halves are needed for a fully functional vehicle. Keys are canonical ids, so `3`, `"3"`,
`"usv-3"`, `"USV-3"` and the configured alias `"SAR-001"` all resolve to the same row; the
display name is never a routing or storage key.

### Verified routes

| Vehicle | Canonical id | Route | Status |
|---|---|---|---|
| Scout | `2` | `http://10.0.2.10:8080` | verified over WireGuard |
| SAR-001 | `3` | `http://10.0.3.10:8080` | verified over WireGuard from both ends |
| USV-1 | `1` | — | registered, monitored, no address |

**Port note — 8090 is a trap, not a clean error.** `8080` is the vehicle's Gunicorn/Flask API
(behind `docker-proxy`) and is the only port this map may name. `8090` on the same host is the
Python **Local Agent diagnostics** server. Probed live on SAR during this pass:

| Route on `10.0.3.10` | `:8080` (Flask API) | `:8090` (diagnostics) |
|---|---|---|
| `/agent/pixhawk_mission` | `200` | `200` — full, correct-looking mission |
| `/agent/control_authority` | `200` | `404` |
| `/agent/experiment/network` | `200` | `404` |
| `/agent/state` | `200` | `404` |

A row pointed at `8090` would therefore render a healthy Mission/Map page while control
authority and experiment control silently failed — worse than an obviously dead address.

Never add a **guessed** address. An absent route degrades honestly (`available:false`); a wrong
one sends authority and mission traffic to whatever host actually holds that IP. Routing is
pinned by `tests/test_vehicle_api_routing.py`, including that an unknown vehicle resolves to no
base and never falls back to Scout.

## Limitations

- All state remains in-memory and resets on backend restart (unchanged; same as the event log
  and comms history). The registry is the only part that survives a restart.
- `VEHICLE_API_BASE` / `DASHBOARDS` / `SSH_TARGETS` are still hardcoded per-vehicle maps for the
  vehicle-local Flask API, dashboard and terminal — a vehicle added via `vehicles.json` appears
  in the fleet but has no live authority/mission/experiment proxy until it is added there too.
  Keyed by canonical id, so the spelling is consistent. **Adding a USV therefore takes both
  halves: a registry identity AND a verified API route.** `VEHICLE_API_BASE` now carries two
  verified routes (Scout `10.0.2.10:8080`, SAR-001 `10.0.3.10:8080`); `DASHBOARDS` and
  `SSH_TARGETS` still list Scout only.
- The SAR Local Agent is not in this repository; its exact payload spelling was not captured
  live. The operator boundary accepts every spelling listed above and logs the resolved
  canonical id per packet, so the live contract is verifiable from `[STATUS]` lines.
