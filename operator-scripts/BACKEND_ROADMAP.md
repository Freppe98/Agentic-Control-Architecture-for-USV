# BACKEND_ROADMAP.md

Every data slot the operator station reserves but the system does not yet fill — audited into an owned, prioritized plan. The goal: turn `BACKEND_GAP` from a permanent UI state into a shrinking backlog. A slot leaves this document when it ships (or is deliberately dropped).

Cross-reference: field semantics live in [`DATA_DICTIONARY.md`](DATA_DICTIONARY.md); the availability-state UI contract lives there too (a `BACKEND_GAP` renders to the operator as **Unavailable / No data / Not installed**, never as jargon).

## Ownership

| Owner | What it is | Can this repo change it? |
|---|---|---|
| **USV** | Autopilot + hull sensors (MAVLink/serial): RSSI, GPS sats, voltages, temps, cameras, water/bathymetry probes | No — hardware/firmware; only *forwarding* is in scope |
| **Local Agent** | Onboard decision agent on the Pi (`Scripts/fsm_agent.py`, `agent_runner.py`, `global_adapter.py`) — cognition + the `POST /agent/status` payload it emits | Yes — agent contract is in this project |
| **Operator backend** | FastAPI (`operator-scripts/main.py`): comms derivation, registry, config, event store, command queue, new `GET/POST` endpoints | **Yes — primary lever** |
| **Frontend** | Operator station (`operator/`): values derivable from data already on hand | **Yes** |

## Disposition (per the review question)
- **B-field** → real backend field (Operator backend stores/derives/exposes it)
- **A-out** → onboard-agent output (Local Agent must emit it in the payload; backend passes through)
- **F-derive** → derived frontend value (compute from existing fields; no backend work)
- **drop** → should not exist (remove the slot or fold into another)

## Transport paths
- **A** `USV sensor → MAVLink → Local Agent → POST /agent/status → Operator backend → GET /api/fleet/status → Frontend`
- **B** `Local Agent (cognition) → POST /agent/status.payload → backend → GET /api/fleet/status → Frontend`
- **C** `Operator backend (internal) → new GET/POST endpoint → Frontend`
- **D** `Frontend derives from fields already present`
- **E** `Frontend → POST /api/command → backend queue → Local Agent (next contact) → USV` (reverse/control)

## Priority
**P0** unblocks a thesis-central capability or many pages · **P1** high value, page-blocking · **P2** valuable, needs sensor/agent work · **P3** low value / derivable / cosmetic.

---

## Communication (the primary axis)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| **comms-state transition log** | Map timeline, Autonomy trace, thesis metrics | Operator backend | B-field | on transition | C → `GET /api/comms/history/{id}` | (enables Timeline) | **P0** |
| `rssi` | Vehicle, Video, Autonomy | USV → Agent | A-out | 1 s | A | No data received | P1 |
| `connectivity` / `operator_reachable` / `buffered_packets` | Vehicle, Autonomy | Local Agent | A-out (partial today) | on packet | B | No data received | P1 |
| `latency_rtt` | Vehicle | Operator backend | B-field (measure ack round-trip) | 1 s | C | No data received | P2 |

## Autonomy — agent cognition (thesis centerpiece)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| `behavior_state` (explicit, not approx. from mission_state) | Autonomy, Fleet, Map | Local Agent | A-out | on change | B | live / last-known | **P0** |
| `behavior_from` (previous state) | Autonomy | Local Agent | A-out | on change | B | Feature unavailable | **P0** |
| `decision_confidence` | Autonomy | Local Agent | A-out | on eval | B | Feature unavailable | **P0** |
| `decision_rationale` | Autonomy | Local Agent | A-out | on eval | B | Feature unavailable | **P0** |
| `active_constraints` (met/unmet inputs) | Autonomy | Local Agent | A-out | on eval | B | Feature unavailable | P1 |
| `next_transitions` (watch conditions) | Autonomy | Local Agent | A-out | on eval | B | Feature unavailable | P1 |
| `decision_trace` | Autonomy | Local Agent + Operator backend | A-out + B-field (comms nodes from the log above) | append | B+C | Feature unavailable | P1 |
| `next_eval_s` (countdown) | Autonomy | Local Agent | A-out | 1 s | B | Feature unavailable | P2 |

## Mission
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| named mission scope / registry | Ribbon, Mission | Operator backend | B-field | slow | C → `GET/POST /api/mission` | Feature unavailable | P1 |
| `assigned` (assigned vs depot pool) | Fleet, Mission, Vehicle | Operator backend | B-field (registry assignment) | on task change | C | Feature unavailable | P1 |
| mission ETA / time remaining | Map, Mission | Local Agent | A-out (agent knows plan + progress) | on change | B | Feature unavailable | P2 |
| mission-level coverage total | Map, Mission | Frontend | F-derive (avg of per-vehicle — already done) | slow | D | — | done |

## Command / control (reverse path)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| command lifecycle (Return Home / Pause / Resume / Loiter) | Map, Mission, Autonomy | Operator backend + Local Agent | B-field + A-out | on action | E → `POST /api/command` | Feature unavailable | P1 |
| command status (Pending/Executing/Finished/Failed, "queues until next contact") | Map, Mission | Operator backend | B-field | on change | E→B | Feature unavailable | P1 |

## Events
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| persistent event log | Events, Map, Mission | Operator backend | B-field | append | C → `GET /api/events` | Feature unavailable | P1 |
| persistent acknowledgement | Events, bell | Operator backend | B-field | on ack | C → `POST /api/events/{id}/ack` | Feature unavailable | P2 |

## Configuration
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| threshold **read** (stale/partitioned/disconnected) | Config → all comms logic | Operator backend | B-field | on save | C → `GET /api/config` | Feature unavailable | P1 |
| threshold **write** | Config | Operator backend | B-field | on save | C → `POST /api/config` | Feature unavailable | P2 |
| `heartbeat_interval_s` | Config | Operator backend | B-field | static | C | Feature unavailable | P3 |
| `alert_rules` (what rings the bell) | Events/bell | Operator backend | B-field | on save | C | Feature unavailable | P3 |
| `units` / `coord_format` / `base_layer` | Config, Map, Vehicle | Frontend | F-derive (localStorage — done) | on save | D | — | done |

## Vehicle — systems & registry
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| `onboard_ip` / `onboard_port` | Pilot | Operator backend | B-field (registry) | static | C | Feature unavailable | P1 (blocks Pilot) |
| `pack_voltage` / `current` | Vehicle (Battery) | USV BMS → Agent | A-out | 1 s | A | No data received | P2 |
| `cpu_load` / `disk_usage` / `flask_status` | Vehicle | Local Agent (Pi) | A-out (partial today) | 1–5 s | B | No data received | P2 |
| `gps_sats` / `hdop` | Vehicle (GPS) | USV → Agent | A-out | 1 s | A | No data received | P2 |
| `cpu_temp` / `batt_temp` / `water_temp` / `motor_temp` | Vehicle (Temps) | USV/Pi sensors | A-out where sensor exists, else **NOT_APPLICABLE** | 1 s | A | No data received / Not installed | P2 |
| `water_quality` / `bathymetry` | Vehicle (Sensors) | USV sensor → Agent | A-out (define schema) | 1 s | A | No data received | P2 |
| `fleet_role` / `assigned_sector` / `formation` | Fleet, Mission | Local Agent / registry | A-out (partial via `fleet_info`) | slow | B | No data received | P2 |
| `callsign` | all | Operator backend | B-field (registry) | static | C | Feature unavailable | P3 |
| `firmware` / `schema_version` | Vehicle | Local Agent (envelope) | A-out — **done** | static | B | — | done |
| `leak_detected` | Vehicle, Events | Agent — **done** | — | 1 s | A | — | done |

## Video / Pilot (pages not yet migrated)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| camera feed / frame state (LIVE/FROZEN/NO SIGNAL) | Video, Pilot | USV camera → Agent | A-out (stream or snapshot URL) | 15–30 fps | A | No data received | P2 |
| onboard dashboard embed + reachability | Pilot | Operator backend | B-field (needs `onboard_ip` + reach probe) | on nav | C | Feature unavailable | P2 |

## Drop / derive (should not become backend fields)
- **`endurance` (min)** — F-derive from battery % and current draw when voltage/current land; no dedicated field.
- **`compass declination`** — F-derive from lat/lng via a WMM table; not a telemetry field.
- **`compass field_strength`** — low operator value; **drop** unless a calibration workflow needs it.
- **mission-level coverage total** — already F-derived (avg of per-vehicle); no backend total needed.

---

## Recommended first implementations (backend-owned, before resuming page migration)

Ordered by value ÷ effort, limited to what this repo can ship without hardware:

1. **Comms-state transition log — `GET /api/comms/history/{id}` (P0, Operator backend).**
   The backend already derives `comm_state` every request; have it record per-vehicle transitions (state, `ts`) and expose the history. **Unblocks three reserved features at once** — the Map comms Timeline, the Autonomy decision-trace comms nodes, and the thesis evaluation metric *total disconnected time*. Highest value, fully in `main.py`, no agent or hardware dependency.

2. **`GET /api/config` (P1, Operator backend).**
   Return the live thresholds (currently compiled constants). Turns the Configuration page from "mirrors main.py" into a genuinely live read (a one-endpoint change). Cheap honesty win; sets up `POST /api/config` later.

3. **Persistent event log — `GET /api/events` + server-side store (P1, Operator backend).**
   Replaces the flattened-from-payload feed and session-local acks with a real record — the Events page's two known gaps. Moderate effort.

4. **Agent reasoning schema (P0 value, Local Agent — cross-component).**
   Highest *thesis* value but not backend-only: `Scripts/fsm_agent.py` must emit `behavior_state`, `decision_confidence`, `decision_rationale`, `behavior_from`, `next_eval_s` in the payload; `normalize_agent_message` already forwards unknown payload fields, so the backend is mostly ready. Bigger change to the agent contract — worth a dedicated pass after #1–#3.

Anything touching `main.py` or the `POST /agent/status` schema is an outward-facing contract change — propose, get a green light, then implement.
