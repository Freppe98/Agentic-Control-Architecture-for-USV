# DATA_DICTIONARY.md

Single source of truth for every data field the operator station consumes. Fields are grouped by origin. **Source** tells you where the value comes from today; fields marked **NO TELEM** are not yet in the telemetry schema and must render with the "NO TELEM" slot convention (never invent a value).

Backend today (`main.py`): `GET /api/fleet/status` returns a list of normalized vehicles (see `normalize_agent_message`). `POST /agent/status` ingests the latest agent message; `GET /api/environment` returns weather. Comms state is derived from time-since-last-contact against the thresholds in `main.py` (`STALE_AFTER_SECONDS=8`, `PARTITIONED_AFTER_SECONDS=15`, `DISCONNECTED_AFTER_SECONDS=30`).

Legend — **Freq**: how often the value changes/should refresh. **Opt**: optional / may be absent.

## Data Availability States (first-class)

Every value/function slot must communicate **why** it is missing — never collapse everything into a generic "NO TELEM". Five states, rendered via `operator/lib/availability.js` (`AVAIL`, `availTag`, `availSlot`; `.av*` classes in `theme.css`):

| State | Meaning | Visual intent |
|---|---|---|
| `LIVE` | value available and fresh | normal value, no decoration |
| `LAST_KNOWN` | value exists but stale because comms are partitioned/disconnected | dimmed value + amber "LAST KNOWN · Xs" tag (comms axis, **not** a fault) |
| `FAULT` (UNAVAILABLE) | vehicle is *expected* to provide this but the sensor/subsystem is missing/broken/offline | fault ✕ + red tag — the **only** state that gets a ✕ |
| `NOT_APPLICABLE` | this vehicle has no such hardware/feature installed | muted "N/A", never alarm |
| `BACKEND_GAP` | frontend has a reserved slot the backend/schema doesn't expose yet | dim, dashed "NO BACKEND" tag — a *development* limitation that should disappear as backend grows |

Rules: ✕/fault means "expected but broken", never "not installed" (that's N/A) or "not wired yet" (that's BACKEND_GAP). **LAST_KNOWN must never turn OK into Warning** — health and communication stay independent. The operator must always be able to tell whether missing data is caused by comms, missing backend support, absent hardware, or a real fault. Applied to new pages first (Autonomy, Vehicle, Video, Pilot); existing `noTelem()` slots (which are mostly BACKEND_GAP) are migrated later in one system-wide pass.

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
| `connectivity` | string | `communication.connectivity` | Vehicle, Autonomy | on packet | yes | |
| `operator_reachable` | bool | `communication.operator_reachable` | Vehicle, Autonomy | on packet | yes | |
| `buffered_packets` | int | `communication.buffered_packets` | Vehicle, Autonomy | on packet | yes | store-and-forward depth |
| `rssi` | dBm | **NO TELEM** (autopilot has it, not forwarded) | Vehicle, Video timeline, Autonomy | 1 s | yes | Show slot; add once logged |
| `latency_rtt` | ms | **NO TELEM** | Vehicle | 1 s | yes | |

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

## Autonomy (the "why" — mostly NEW, needs backend support)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `behavior_state` | string | agent (approx by `mission_state` today) | Autonomy, Fleet, Map | on change | no | Searching/Holding/Returning/Loitering |
| `behavior_from` | string | agent decision log | Autonomy | on change | yes | previous state |
| `decision_confidence` | % | **NO TELEM** — agent must emit | Autonomy | on eval | yes | certainty in the behavior choice |
| `decision_rationale` | string | **NO TELEM** — agent must emit | Autonomy | on eval | yes | plain-language reason |
| `active_constraints` | list<{name,met,value}> | **NO TELEM** — agent must emit | Autonomy | on eval | yes | decision inputs |
| `next_transitions` | list<{to,condition}> | **NO TELEM** — agent must emit | Autonomy | on eval | yes | watch conditions |
| `decision_trace` | list<{kind,state,ts,note}> | **needs comms-state transition logging** | Autonomy | append | yes | state-machine history |
| `next_eval_s` | seconds | agent | Autonomy | 1 s | yes | countdown |

## Health / systems (the fault axis — separate from comms)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `health_state` | enum `OK\|CAUTION\|WARN` | **derived** per subsystem | Fleet, Vehicle | 1 s | no | named condition, not "Caution" alone |
| `cpu_load` | % | `health.cpu_load` | Vehicle | 1–5 s | yes | |
| `disk_usage` | % | `health.disk_usage` | Vehicle (Storage) | slow | yes | |
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
| `wind_speed` / `wind_direction` | m/s / deg | `/api/environment` | Map (wind widget) | 10 min | yes | |
| `temperature` / `weather_code` | °C / int | `/api/environment` | Map | 10 min | yes | |
| `local_time` | string | `/api/environment` | Map/ribbon | 1 s | no | |

## Config (drive the machine, not just UI prefs)
| Field | Type | Source | Pages | Freq | Opt | Notes |
|---|---|---|---|---|---|---|
| `stale_threshold_s` | int (8) | config | Configuration → all comms logic | on save | no | `STALE_AFTER_SECONDS` |
| `partitioned_threshold_s` | int (15) | config | Configuration → all | on save | no | `PARTITIONED_AFTER_SECONDS` |
| `disconnected_threshold_s` | int (30) | config | Configuration → all | on save | no | `DISCONNECTED_AFTER_SECONDS` |
| `heartbeat_interval_s` | int | config | Configuration | on save | yes | |
| `units` / `coord_format` / `base_layer` | enum | config | Map, Vehicle | on save | yes | |
| `alert_rules` | object | config | Events/bell | on save | yes | which conditions ring the bell |
