# DATA_DICTIONARY.md

Single source of truth for every data field the operator station consumes. Fields are grouped by origin. **Source** tells you where the value comes from today; fields marked **NO TELEM** are not yet in the telemetry schema and must render with the "NO TELEM" slot convention (never invent a value).

Backend today (`main.py`): `GET /api/fleet/status` returns a list of normalized vehicles (see `normalize_agent_message`). `POST /agent/status` ingests the latest agent message; `GET /api/environment` returns weather. Comms state is derived from time-since-last-contact against the thresholds in `main.py` (`STALE_AFTER_SECONDS=8`, `PARTITIONED_AFTER_SECONDS=15`, `DISCONNECTED_AFTER_SECONDS=30`).

### Multi-USV identity and per-vehicle current state

Several USVs post to the same endpoint, so **identity and isolation are part of the data contract**:

- **Canonical vehicle id** — one id policy for the whole station, in `vehicle_registry.py` with the deployed configuration in `vehicles.json`. `canonical_id()` folds every spelling a vehicle may use for itself (`3`, `"3"`, `"usv-3"`, `"USV-3"`, and declared aliases such as the callsign `"SAR-001"`) to ONE value; its stable string form is the slug `usv-3`, published on every fleet row as `vehicle_id`. Aliases are declared by a human, never inferred, and an alias claimed by two vehicles is a startup error.
- **Display name is not an identity.** `name` is per-vehicle and sticky (the name that vehicle last reported, else its configured one). No state, cache, command, URL or selection is keyed by it — a vehicle renaming itself (`USV-3` → `SAR-001`) must not create, merge or move a record.
- **One record per USV.** `current_vehicle_state[canonical_id]` holds that vehicle's last accepted packet, its own `received_at`, its own monotonic `message_timestamp`, and its own last-known telemetry/agent groups. A packet from vehicle A updates exactly A: it can never overwrite B's telemetry, name, health, mission, authority or freshness, and never changes the operator's selection.
- **Freshness is per USV.** `comm_state` / `last_seen_age_s` come from that vehicle's own last contact. One vehicle reporting neither refreshes nor ages another; a silent vehicle keeps its last-known values and transitions CONNECTED → PARTITIONED → DISCONNECTED on its own clock.
- **Packet ordering is per USV.** The monotonic replay guard compares a packet only against the newest timestamp from the SAME vehicle. Interleaved arrivals (Scout, SAR, Scout, SAR) are normal traffic and never block each other.
- **The fleet response is complete every time.** Every configured vehicle exists from startup with `comm_state: UNKNOWN` and `contacted: false`; live data updates that same canonical record instead of adding a second row. No vehicle disappears, or reverts to a placeholder, because another vehicle reported.
- **Adding a USV is configuration, not code** — one entry in `vehicles.json`, then restart. See `operator/docs/verification/multi-usv-isolation.md`.

Legend — **Freq**: how often the value changes/should refresh. **Opt**: optional / may be absent.

## Data Availability States (first-class)

Every value/function slot must communicate **why** it is missing — never collapse everything into a generic "NO TELEM". Five states, rendered via `operator/lib/availability.js` (`AVAIL`, `availTag`, `availSlot`; `.av*` classes in `theme.css`):

| State | Meaning | Visual intent |
|---|---|---|
| `LIVE` | value available and fresh | normal value, no decoration |
| `LAST_KNOWN` | value exists but stale because comms are partitioned/disconnected | dimmed value + amber "LAST KNOWN · Xs" tag (comms axis, **not** a fault) |
| `FAULT` (UNAVAILABLE) | vehicle is *expected* to provide this but the sensor/subsystem is missing/broken/offline | fault ✕ + red tag — the **only** state that gets a ✕ |
| `NOT_APPLICABLE` | this vehicle has no such hardware/feature installed | muted "N/A", never alarm |
| `BACKEND_GAP` | frontend has a reserved slot the backend/schema doesn't expose yet | **development-only** — never shown to operators as-is; renders as an operator-facing reason (below). A dev tooltip may carry the real reason. |

**BACKEND_GAP is developer-only and must never leak to an operator.** In the UI it maps to one of these operator-facing reasons (the operator should not need to know the backend is unfinished):

| Operator-facing label | Use when |
|---|---|
| **Feature unavailable** / "Unavailable" | capability not built yet (default GAP tag) |
| **No data received** / "No data" | expected field the vehicle/agent simply isn't sending |
| **Not installed** | hardware/feature absent on this hull (usually `NOT_APPLICABLE`) |
| **Unsupported by this vehicle** | vehicle model doesn't support it (usually `NOT_APPLICABLE`) |

Rules: ✕/fault means "expected but broken", never "not installed" (that's N/A) or "not wired yet" (that's BACKEND_GAP). **LAST_KNOWN must never turn OK into Warning** — health and communication stay independent. The operator must always be able to tell whether missing data is caused by comms, missing backend support, absent hardware, or a real fault — but "backend not implemented" is expressed as "unavailable", not as jargon. Every BACKEND_GAP is tracked with an owner and disposition in `BACKEND_ROADMAP.md`. Applied to new pages first (Agent, Vehicle, Video, Pilot); existing `noTelem()` slots migrate later in one system-wide pass.

## Identity & registry
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `id` / `usv_id` | int | agent payload `usv_id` | all | static | no | Stable vehicle id |
| `name` | string | payload `name` / registry | all | static | no | e.g. `USV-2` |
| `callsign` | string | registry (config) | all | static | yes | e.g. `Scout`; **not in backend yet** — registry field |
| `onboard_ip` / `onboard_port` | string / int | registry (config) | Pilot | static | yes | Pi 5 dashboard, e.g. `10.0.2.10:8080`; **registry, not in payload** |
| `assigned` | bool | mission scope | Fleet, Vehicle | on task change | no | In current mission vs depot pool |
| `fleet_role` / `assigned_sector` / `formation` | string | `fleet_info.*` | Fleet, Mission | slow | yes | |

## Communication (the primary axis)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `comm_state` | enum `CONNECTED\|PARTITIONED\|DISCONNECTED\|UNKNOWN` | **derived** from `last_seen_age_s` vs thresholds | all | 1 s | no | The comms axis — never merged with health |
| `last_seen` | ISO timestamp | server receive time | all | on packet | no | |
| `last_seen_age_s` | seconds | **derived** (now − last_seen) | all (freshness) | 1 s | no | Drives "Last contact Xs" + stale dimming |
| `connectivity` | string | `communication.connectivity` | Vehicle, Agent | on packet | yes | |
| `operator_reachable` | bool | `communication.operator_reachable` | Vehicle, Agent | on packet | yes | |
| `buffered_packets` | int | `communication.buffered_packets` | Vehicle, Agent | on packet | yes | store-and-forward depth |
| `rssi` | dBm | **NO TELEM** (autopilot has it, not forwarded) | Vehicle, Video timeline, Agent | 1 s | yes | Show slot; add once logged |
| `latency_rtt` | ms | **NO TELEM** | Vehicle | 1 s | yes | |
| `mavlink.heartbeat_age_s` | s | **derived** in `mavlink_evidence()` from Scout MAVLink fields | Vehicle (Diagnostics: Pixhawk heartbeat, Pixhawk card) | 1 s | yes | REAL HEARTBEAT age only — never inferred from GPS/arrival. Absent → NOT AVAILABLE. See BACKEND_ROADMAP → "Pixhawk heartbeat / MAVLink evidence" |
| `mavlink.connected` / `last_msg_age_s` / `msg_rate_hz` / `parser_errors` | bool/s/Hz/int | `communication.mavlink_*` / `payload.mavlink.*` | Vehicle (Diagnostics: MAVLink) | 1 s | yes | MAVLink link state + freshness. Absent → NOT AVAILABLE |

## Telemetry
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `battery` | % | `telemetry.battery` | all | 1–5 s | no | `-1` → unknown; **stays vivid even when stale** |
| `lat` / `lng` | float | `telemetry.lat/lng` | Map, Vehicle | 1 s | no | Fallback default if invalid |
| `heading` | deg | `telemetry.heading` | Map, Vehicle, Video, Compass | 1 s | no | |
| `speed` / `groundspeed` | kn/(m/s) | `telemetry.groundspeed\|speed` | Fleet, Video, Vehicle | 1 s | no | Confirm unit at source |
| `alt` | m | `telemetry.alt` | Vehicle | 1 s | yes | |
| `armed` | bool | `telemetry.armed` | Vehicle, Pilot | on change | yes | |
| `mode` | string | `telemetry.mode` | all | on change | no | `AUTO\|HOLD\|RTL\|LOITER\|...` |
| `gps_fix` / `gps_sats` / `hdop` | string/int/float | autopilot (partial) | Vehicle | 1 s | yes | sats/HDOP borderline — mark NO TELEM if not forwarded |

## Mission & coverage
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `mission_state` | string | `mission.mission_state` | all | on change | no | |
| `mission_active` | bool | `mission.mission_active` | Mission | on change | yes | |
| `current_waypoint_display` | string | `mission.current_waypoint_display` | Mission | on change | yes | |
| `mission_count` | int | `mission.mission_count` | Mission | slow | yes | |
| `coverage` | % | payload `coverage` | Map, Fleet, Mission | slow | yes | mission total vs per-vehicle `sector` |
| `sector_swept` | % | per-vehicle coverage | Fleet, Vehicle, Mission | slow | yes | |

## Agent (the "why" — mostly NEW, needs backend support)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `behavior_state` | string | agent (approx by `mission_state` today) | Agent, Fleet, Map | on change | no | Searching/Holding/Returning/Loitering |
| `behavior_from` | string | agent decision log | Agent | on change | yes | previous state |
| `decision_confidence` | % | **NO TELEM** — agent must emit | Agent | on eval | yes | certainty in the behavior choice |
| `decision_rationale` | string | **NO TELEM** — agent must emit | Agent | on eval | yes | plain-language reason |
| `active_constraints` | list<{name,met,value}> | **NO TELEM** — agent must emit | Agent | on eval | yes | decision inputs |
| `next_transitions` | list<{to,condition}> | **NO TELEM** — agent must emit | Agent | on eval | yes | watch conditions |
| `decision_trace` | list<{kind,state,ts,note}> | **needs comms-state transition logging** | Agent | append | yes | state-machine history |
| `next_eval_s` | seconds | agent | Agent | 1 s | yes | countdown |

## Health / systems (the fault axis — separate from comms)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `health_state` | enum `OK\|CAUTION\|WARN` | **derived** per subsystem | Fleet, Vehicle | 1 s | no | named condition, not "Caution" alone |
| `cpu_load` | % | `health.cpu_load` | Vehicle | 1–5 s | yes | |
| `disk_usage` | % | `health.disk_usage` | Vehicle (Storage) | slow | yes | |
| `ram_usage` | % | `health.ram_usage` | Vehicle (Diagnostics) | 1–5 s | yes | `get_ram_usage()` — posix only, `null` elsewhere |
| `flask_status` | string | `health.flask_status` | Vehicle (Services) | slow | yes | |
| `leak_detected` | bool | `health.leak_detected` | Vehicle (Safety), Events | 1 s | no | safety-critical |
| `pack_voltage` / `current` / `endurance` | V/A/min | **NO TELEM** | Vehicle (Battery) | 1 s | yes | |
| `cpu_temp` / `batt_temp` / `water_temp` / `motor_temp` | °C | **NO TELEM** | Vehicle (Temperatures) | 1 s | yes | whole card is NO TELEM today |
| `compass_cal` / `declination` / `field_strength` | — | **NO TELEM** | Vehicle (Compass) | slow | yes | heading is live; the rest NO TELEM |
| `firmware` / `schema_version` | string | envelope `schema_version` | Vehicle (Firmware) | static | yes | |

## Measurements (SAR payload)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `water_quality` | object | `measurements.water_quality` | Vehicle (Sensors) | 1 s | yes | shape TBD — show "streaming" until modeled |
| `bathymetry` | object | `measurements.bathymetry` | Vehicle (Sensors) | 1 s | yes | |

## Events & notifications
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `events[]` | list<{ts,severity,vehicle,message,ack}> | `events` | Events, Map, Mission | append | no | permanent record |
| `severity` | enum `INFO\|CAUTION\|WARNING\|EMERGENCY` | event | Events | — | no | row-tint + dot |
| `acknowledged` | bool | operator action (client/server) | Events, bell | on ack | no | bell = unack CAUTION+ (transient) |

## Environment
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `wind_speed` / `wind_direction` | m/s / deg | `/api/environment` | Map (wind widget) | 10 min | yes | `null` when unavailable — widget hides/dims, never errors |
| `temperature` / `weather_code` | °C / int | `/api/environment` | Map | 10 min | yes | `null` when unavailable |
| `local_time` | string | `/api/environment` | Map/ribbon | 1 s | no | `safe_local_time()` never raises (ZoneInfo may be absent on some hosts) |
| `available` / `stale` / `source_age_s` | bool/bool/s | `/api/environment` | Map | 10 min | no | Stable partial schema — endpoint NEVER 500s; `available:false`+`stale:true` = served from last-known cache after a fetch failure |

Effective **control authority** (`GET /api/control_authority/{id}` → `authority`) is one of `OPERATOR` (operator holds the wheel), `LOCAL_AGENT` (autonomy), `RC` (RC transmitter override — reported only, not requestable), or `null` (unknown/unreachable/no source). The read also carries `available` (a source is configured) and `reachable` (we reached it). Take Control → OPERATOR, Release Control → LOCAL_AGENT. Stale link → the UI shows authority/arm/mode as UNKNOWN, never a last value.

## Config (drive the machine, not just UI prefs)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `stale_threshold_s` | int (8) | config | Configuration → all comms logic | on save | no | `STALE_AFTER_SECONDS` |
| `partitioned_threshold_s` | int (15) | config | Configuration → all | on save | no | `PARTITIONED_AFTER_SECONDS` |
| `disconnected_threshold_s` | int (30) | config | Configuration → all | on save | no | `DISCONNECTED_AFTER_SECONDS` |
| `heartbeat_interval_s` | int | config | Configuration | on save | yes | |
| `units` / `coord_format` / `base_layer` | enum | config | Map, Vehicle | on save | yes | |
| `alert_rules` | object | config | Events/bell | on save | yes | which conditions ring the bell |

## Route quality — survey-frame alignment (`mission_package.route_quality`)

Produced by `planning.generate_survey`. Every field is a plain count or length over the geometry
that was actually emitted — no scores, no estimates.

**The distinction these fields exist to make.** A `primary` / `secondary` segment is *not* all
coverage. It is the clipped survey lane fragments — the sonar passes — with the inter-fragment
**transitions** concatenated between them. Only the fragments carry the survey-frame contract: a
sonar pass must run parallel to the survey angle, because a stable heading at a fixed lane spacing
is what makes the swaths overlap. A transition is the vessel repositioning between two *finished*
fragments and takes the shortest heading its safety proof allows. Counting the two together
charged legitimate transit geometry to the coverage contract.

| Field | Type | Meaning | Expected |
|---|---|---|---|
| `survey_aligned_coverage_segment_count` | int | Survey FRAGMENT legs on the U or V axis of their own pass frame | > 0 |
| `non_survey_aligned_coverage_segment_count` | int | Survey FRAGMENT legs at an arbitrary heading — **arbitrary-angle sonar geometry** | **0** |
| `survey_aligned_transition_count` | int | Inter-fragment TRANSIT legs that happen to lie on a survey axis | any |
| `non_survey_aligned_transition_count` | int | Inter-fragment TRANSIT legs at an arbitrary heading | any — **not** a defect |
| `survey_aligned_segment_count` | int | Compatibility alias of `survey_aligned_coverage_segment_count` | > 0 |
| `non_survey_aligned_segment_count` | int | Compatibility alias of `non_survey_aligned_coverage_segment_count`. **COVERAGE FRAGMENTS ONLY** — before the split it summed the whole coverage polyline; the difference is exactly the transition legs, now reported above | **0** |
| `direct_transit_transition_count` | int | Transitions taken as one straight leg at any heading (`_aligned_transition` tier 0) | any |
| `shortest_safe_transition_count` | int | Transitions where the straight leg was unsafe and a **bounded local detour** (tier 0b) beat the survey-frame candidate | any |
| `shortest_safe_transition_distance_m` | m | Total emitted length of those detours | — |
| `shortest_safe_saved_distance_m` | m | How much shorter they are than the aligned candidates they replaced | — |
| `aligned_direct_transition_count` | int | Transitions taken straight *because* they were already U/V-aligned (tier 1) | any |
| `orthogonal_transition_count` | int | Transitions built as a survey-frame L or bypass staircase (tiers 2–3) | any |
| `fallback_connector_count` | int | Transitions that reached the generic grid-A* connector (tier 4) | **0** normally |
| `coverage_fragment_length_m` | m | Geodesic length of the survey FRAGMENTS alone | — |
| `in_coverage_transition_length_m` | m | Geodesic length of the transitions inside the coverage segments | — |

**Accounting note.** `metrics.coverage_length_m` is the length of the whole `primary`/`secondary`
polyline, so it *includes* the inter-fragment transits: `coverage_length_m ==
coverage_fragment_length_m + in_coverage_transition_length_m`. On real operator polygons the
transit share is 20–35 % of that figure. The Plan page still labels `coverage_length_m` as
"Coverage length"; splitting the display is tracked separately and is not part of this change.

The safety predicate is identical for every tier and every leg: `_NavGrid.segment_is_safe(...,
require_inside=True)` against `buildable` (shoreline-inset boundary MINUS the buffered no-go
exclusion, minus the wire margin). Alignment only ever decides which *already-safe* candidate is
preferred; it never admits an unsafe leg.

**Tier 0b (`shortest_safe_transit`) is bounded, not a general planner.** It is reached only when
the straight leg fails the predicate. The aligned tiers are evaluated first and produce exactly
the candidate they always produced; the detour is searched over at most `F4_MAX_LOCAL_VERTICES`
corners drawn from the a/b bounding box grown by `F4_LOCAL_MARGIN_M`, and is taken only when it
beats that candidate by `F4_MIN_GAIN_M` — so a near-tie never churns the route hash. Corners come
from `buildable` shrunk by `F4_VERTEX_INSET_M`, which yields both the concave-shoreline corners
and the buffered-exclusion corners already proven to be strictly inside the approved region.

### `metrics.route_quality` — BCD cell ORDER and ORIENTATION

Produced by `planning._bcd_cell_plan`, one entry per coverage pass. The boustrophedon cellular
decomposition (`_bcd_cells`) says *what* the cells are; these fields say **which cell was covered
when, which way round, and what each hand-over between them cost**. They exist because the
sequence is now chosen by measurement rather than by a fixed geometric key, and a measured
decision has to be inspectable: every number below is re-derivable from the emitted route.

| Field | Type | Meaning | Expected |
|---|---|---|---|
| `coverage_cell_plans[]` | list | One plan per coverage pass (`pass_kind`) | — |
| `coverage_cell_plans[].mode` | str | `exact-held-karp` (globally optimal for the cost below), `topology-aware-heuristic` above `BCD_EXACT_MAX_CELLS` cells, or `geometric-fallback` when no complete sequence is routable | `exact-held-karp` normally |
| `coverage_cell_plans[].cell_count` | int | Cells in this pass's decomposition | 1–7 observed |
| `coverage_cell_plans[].orientation_states_per_cell` | int | Legal whole-cell traversals evaluated per cell | 4 |
| `coverage_cell_plans[].adjacency` | list[list[int]] | The BCD topology graph: cell → the cells it meets across a sweep row (a split or a merge). **Never a clique** — the two columns beside one obstacle are not adjacent to each other | — |
| `coverage_cell_plans[].cell_order` | list[int] | Cell ids in the order they are covered, each exactly once | a permutation |
| `coverage_cell_plans[].cell_orientations[]` | list | Per visited cell: `cell_id`, `ascending` (which end of the sweep it is entered at), `first_row_forward` (which way its first lane is flown) | — |
| `coverage_cell_plans[].handovers[]` | list | Per inter-cell hand-over: `from_cell`, `to_cell`, `adjacent`, `length_m`, and the `category` (tier) that drew it | — |
| `coverage_cell_plans[].in_cell_transit_m` | m | The chosen traversals' WITHIN-cell lane-turn ladder — repositioning between consecutive lanes, straight-line in the survey frame. The **emitted** ladder is `in_coverage_transition_length_m − inter_cell_transit_length_m`; this is the search's own straight-line view of it | close to the emitted value |
| `coverage_cell_plans[].in_cell_transit_spread_m` | m | How much in-cell ladder the four traversals of each cell differ by, summed over cells — i.e. the size of the term the cost function deliberately does **not** optimise. Reported so a geometry where it grows is visible rather than silent | 0 on symmetric cells; 35.1 m on the 150° operator draft |
| `coverage_cell_plans[].inter_cell_transit_m` | m | Total hand-over distance between cells | — |
| `coverage_cell_plans[].largest_inter_cell_transit_m` | m | The worst single hand-over — the number a long cross-survey diagonal shows up in | — |
| `coverage_cell_plans[].non_adjacent_handover_count` | int | Hand-overs between cells the decomposition does not connect. One per obstacle is expected: the sweep has to cross it once | ≤ number of no-go zones |
| `coverage_cell_plans[].entry_boundary_m` | m \| null | The survey-entry connector the chosen first cell costs, from the end of the approach chain (or planning Home). Equals the emitted `survey_entry_connector` length | — |
| `coverage_cell_plans[].return_boundary_m` | m \| null | The return connector the chosen last cell costs, to the first return waypoint (or Home). Equals the emitted `return_connector` length | — |
| `coverage_cell_plans[].transition_evaluations` | int | Distinct candidate hand-overs actually measured | ≪ `n·(n-1)·16` |
| `coverage_cell_plans[].transition_cache_hits` | int | Repeat asks served from the memo | any |
| `coverage_cell_plans[].boundary_evaluations` | int | Distinct entry/return connectors measured | ≤ `8n` |
| `inter_cell_transit_length_m` | m | `inter_cell_transit_m` summed over passes | — |
| `largest_inter_cell_transit_m` | m | Worst single hand-over across all passes | — |
| `non_adjacent_cell_handover_count` | int | `non_adjacent_handover_count` summed over passes | — |

**What is optimised.** The objective is the total NON-COVERAGE distance the ordering is
responsible for: the inter-cell hand-overs plus the survey-entry and return connectors, each
measured with the *same* transition policy the route is drawn with (tier 0 → tier 0b → aligned →
A*). On top of the distance sit three small deterministic terms — `BCD_TOPOLOGY_PENALTY_LANES` (a
hand-over between cells the decomposition does not connect), `BCD_REVERSAL_PENALTY_LANES` (leaving
a cell on a heading that points away from the next one), and `BCD_FALLBACK_PENALTY_M` (a hand-over
only the generic A* can draw, which is a contract violation rather than a slightly worse route, so
its penalty is effectively lexicographic). The first two are lane-spacing multiples against
distances of tens to hundreds of metres, so **distance dominates by construction**.

**Coverage is an INPUT to this, never an output.** The lane family, the lane spacing, the survey
angle, the clip, the fragments and cell membership are all fixed before the ordering runs. For
identical planning input, before and after an ordering change, these are byte-identical: fragment
count, every fragment's coordinates, total survey-line length, shoreline and no-go clearance, and
which cell each fragment belongs to. What may change: the cell visitation order, the execution
order of fragments between cells, the forward/reverse orientation of whole cells, the transition
coordinates, the route waypoint count, the route hash and the total route length.

**A cell is still covered by a plain lawnmower.** Each of the four evaluated traversals of a cell
flies the *same* fragments, lane by lane through the sweep, with the along-U direction alternating
per lane — straight parallel passes at the survey angle at the configured spacing. The ordering
chooses among whole-cell traversals and among cells; it never reorders fragments within a cell and
never introduces diagonal, curved or free-form coverage. Only TRANSIT between finished cells takes
an arbitrary safe heading.
