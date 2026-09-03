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
| **comms-state transition log** | Map timeline, Agent trace, thesis metrics | Operator backend | B-field | on transition | C → `GET /api/comms/history/{id}` | (enables Timeline) | **P0** |
| `rssi` | Vehicle, Video, Agent | USV → Agent | A-out | 1 s | A | No data received | P1 |
| `connectivity` / `operator_reachable` / `buffered_packets` | Vehicle, Agent | Local Agent | A-out (partial today) | on packet | B | No data received | P1 |
| `latency_rtt` | Vehicle | Operator backend | B-field (measure ack round-trip) | 1 s | C | No data received | P2 |

## Agent — agent cognition (thesis centerpiece)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| `behavior_state` (explicit, not approx. from mission_state) | Agent, Fleet, Map | Local Agent | A-out | on change | B | live / last-known | **P0** |
| `behavior_from` (previous state) | Agent | Local Agent | A-out | on change | B | Feature unavailable | **P0** |
| `decision_confidence` | Agent | Local Agent | A-out | on eval | B | Feature unavailable | **P0** |
| `decision_rationale` | Agent | Local Agent | A-out | on eval | B | Feature unavailable | **P0** |
| `active_constraints` (met/unmet inputs) | Agent | Local Agent | A-out | on eval | B | Feature unavailable | P1 |
| `next_transitions` (watch conditions) | Agent | Local Agent | A-out | on eval | B | Feature unavailable | P1 |
| `decision_trace` | Agent | Local Agent + Operator backend | A-out + B-field (comms nodes from the log above) | append | B+C | Feature unavailable | P1 |
| `next_eval_s` (countdown) | Agent | Local Agent | A-out | 1 s | B | Feature unavailable | P2 |

## Mission
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| named mission scope / registry | Ribbon, Mission | Operator backend | B-field | slow | C → `GET/POST /api/mission` | Feature unavailable | P1 |
| `assigned` (assigned vs depot pool) | Fleet, Mission, Vehicle | Operator backend | B-field (registry assignment) | on task change | C | Feature unavailable | P1 |
| mission ETA / time remaining | Map, Mission | Local Agent | A-out (agent knows plan + progress) | on change | B | Feature unavailable | P2 |
| mission-level coverage total | Map, Mission | Frontend | F-derive (avg of per-vehicle — already done) | slow | D | — | done |
| **Pixhawk mission readback** (mission stored on the flight controller — numbered waypoints + overlay, view-only) | Map | Scout Flask (download over MAVLink) + Operator backend (thin proxy) | B-field | on demand | C → `GET /api/vehicles/{id}/pixhawk-mission` (proxies Scout `GET /agent/pixhawk_mission`) | Scout unavailable | P1 · **operator proxy done** (see `verification/pixhawk-mission.md`) · needs Scout route |
| **Vehicle Home status (`payload.agent.home_status`)** — the ONE Scout-owned, continuously-reported source of Home verification truth: `verified`, `verified_at`, `verification_method`, `verification_distance_m`, `ready_for_auto`, `ready_for_rtl`, `reason`, `home_position`, `reachable`. `main.home_block()` mirrors it verbatim (never recomputes/latches it) — absent ⇒ honest "not reported"; a packet that omits it while the vehicle was previously reporting falls back to the last one Scout sent but is marked `stale` and forced `verified:false` (never silently trusted); the vehicle being non-CONNECTED does the same. | Map | Local Agent (forward Scout's continuous Home status) | A-out | on every status packet | B → `GET /api/fleet/status.home` | HOME UNKNOWN | P1 · **operator/frontend consumer done** (see `verification/set-home.md`) · needs Local Agent/Scout Flask to emit `payload.agent.home_status` |
| **Set Home command** (deployment: queue a `MAV_CMD_DO_SET_HOME` + read-back verify at the Scout's current position) | Map | Local Agent (`MAV_CMD_DO_SET_HOME` + read-back over MAVLink, via Scout Flask) + Operator backend (command queue) | B-field | on deploy | E → `POST /api/commands` (type `SET_HOME`) — a normal queued command, same as AUTO/RTL/LOITER/ARM/DISARM/PAUSE/RESUME; no direct HTTP call to Scout from the operator backend. **Command status `EXECUTED` means only "the Local Agent called Scout Flask" — it is NOT proof Set Home succeeded.** The command's own nested result (`result.accepted`/`result.verified`/`result.home_position`/`result.verification_distance_m`/`result.error`) is classified by `main._annotate_set_home_result` into `cmd["home_result"]` (`verified`/`failed`) for IMMEDIATE click feedback (a toast/pending flash) only — it never writes any permanent state. The permanent Home row above is the only thing that ever reads as verified. | never optimistic — a command result only ever produces transient feedback | P1 · **operator queue + result classification done** (see `verification/set-home.md`) · needs Local Agent to execute SET_HOME off the queue and report a result in the real Scout contract shape |
| **mission route content hash** (`mission-contract-v1`) — the ONLY axis that proves the on-FC route is byte-for-byte the route the operator approved. Counts alone cannot: a route with two waypoints swapped, or one coordinate wrong, has the correct N and N+1. **Done.** The Operator backend is the authoritative calculator (`mission_contract.route_content_hash`, called from `main.canonical_mission_upload_params`); there is deliberately NO frontend implementation, because a second calculator is a second thing that can drift from Scout. Canonicalization: route items only (Home excluded), 1-based `sequence`, fixed `MAV_CMD_NAV_WAYPOINT` / `MAV_FRAME_GLOBAL_RELATIVE_ALT`, lat/lng rounded to 7 dp, altitude 0.0, `loiter_time_s` → `param1` rounded to 3 dp, `param2..4` 0.0, `json.dumps(sort_keys=True, separators=(",", ":"))`, UTF-8 SHA-256, `sha256:` prefix. Pinned against Scout's golden hash in `tests/fixtures/mission-contract-v1.json`. A missing expected or observed route hash is an explicit verification FAILURE, never a count-only pass. *Historical note (superseded):* an earlier operator-side `wpm1:` FNV-1a hash was locally invented and never computed by Scout; it was removed rather than replaced with a second guess, and `expected_route_content_hash` shipped as `null` until Scout's spec arrived. | Mission | Scout (defines canonicalization) + Operator backend (implements it) | B-field | on upload | E → `POST /api/commands` (type `MISSION_UPLOAD`) | verification fails explicitly if either hash is absent | **done** |
| **`agent.mission_upload`** (live background-upload worker state: `{active, state, command_id, elapsed_s}`) — drives the Requested → Executing progress track. Mapped **only** when `command_id` matches the tracked command, so another command's transfer can never colour this one. Deliberately **not** last-known-backed: a replayed `active:true` would show "Executing" forever for an upload that died with the link. Scout is **not** required to post an intermediate ACCEPTED command result — the backend redelivers nonterminal commands, so such a post would simply be redelivered. | Mission | Local Agent / Scout | A-out | on every status packet while uploading | B → `GET /api/fleet/status.mission_upload` | (track shows Requested until it arrives) | P1 · **operator consumer done** · needs Scout to emit the group |
| **`MISSION_CLEAR`** — wipes the stored route, verified by an independent empty read-back. **Done.** Scout ships `POST /agent/clear_mission` through the queued `MISSION_CLEAR` command. Verified when `accepted` + `cleared` + `verified` + `observed_route_waypoint_count == 0` + `empty_representation` ∈ {`NO_ITEMS`, `HOME_ONLY`}. The Pixhawk ITEM count is deliberately NOT required to be 0: ArduPilot may retain Home at seq 0, and `HOME_ONLY` (item count 1, route count 0) is a correctly cleared mission. After a verified clear the Pixhawk mission is re-fetched independently and the resulting empty representation is shown. *Historical note (superseded):* this was previously refused `501 scout_update_required` with the button disabled, because Scout had no result contract to verify a clear against. | Mission | Scout + Operator backend | B-field | on action | E → `POST /api/commands` | clear reported NOT verified, with Scout's reason | **done** |

## Command / control (reverse path)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| command lifecycle (modes AUTO/MANUAL/HOLD/LOITER/GUIDED/RTL, ARM/DISARM, MISSION_PAUSE/RESUME, SET_HOME) | Map, Vehicle, Mission, Agent | Operator backend + Local Agent | B-field + A-out | on action | E → `POST /api/commands` | state reported by vehicle | P1 · **backend done** (see `verification/commands.md`) |
| command status (requested → sent → acknowledged → confirmed / rejected / timed-out) | Map, Vehicle, Mission | Operator backend | B-field | on change | E→B | never optimistic | P1 · **backend done** (queue + comm-state gating + Agent result) |
| **command & control panel** (Take Control→OPERATOR / Release→LOCAL_AGENT + 10 command buttons + queue, gated on Scout-confirmed authority, PENDING until effective) | Map, Vehicle | Scout Flask (authority) + Operator backend (queue) + Frontend | B-field | on action | E | Take Control / Release Control | P1 · **done** (see `verification/authority.md`, `verification/commands.md`) |

## Events
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| persistent event log | Events, Map, Mission | Operator backend | B-field | append | C → `GET /api/events` | — | **done** |
| persistent acknowledgement | Events, bell | Operator backend | B-field | on ack | C → `POST /api/events/{id}/ack` | Feature unavailable | P2 (id-ready) |

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
| `cpu_load` / `disk_usage` / `ram_usage` / `flask_status` | Vehicle | Local Agent (Pi) | A-out (partial today — `flask_status` still unset) | 1–5 s | B | No data received | P2 |
| `gps_sats` / `hdop` | Vehicle (GPS) | USV → Agent | A-out | 1 s | A | No data received | P2 |
| `cpu_temp` / `batt_temp` / `water_temp` / `motor_temp` | Vehicle (Temps) | USV/Pi sensors | A-out where sensor exists, else **NOT_APPLICABLE** | 1 s | A | No data received / Not installed | P2 |
| `water_quality` / `bathymetry` | Vehicle (Sensors) | USV sensor → Agent | A-out (define schema) | 1 s | A | No data received | P2 |
| `fleet_role` / `assigned_sector` / `formation` | Fleet, Mission | Local Agent / registry | A-out (partial via `fleet_info`) | slow | B | No data received | P2 |
| `callsign` | all | Operator backend | B-field (registry) | static | C | Feature unavailable | P3 |
| `firmware` / `schema_version` | Vehicle | Local Agent (envelope) | A-out — **done** | static | B | — | done |
| `leak_detected` | Vehicle, Events | Agent — **done** | — | 1 s | A | — | done |

## Diagnostics (Vehicle page — Run System Check)
| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| `GET /agent/diagnostics` / `POST /agent/system_check` (read-only) | Vehicle (Diagnostics) | Scout Flask (motherpi/services/flask) | B-field, once Scout exposes it — operator backend would add a thin proxy (`GET/POST /api/{diagnostics,system_check}/{vehicle}`), same pattern as `control_authority` | on demand | C | Not available | P2 |
| MAVLink / Pixhawk heartbeat / RC receiver / camera / mission-service checks | Vehicle (Diagnostics) | USV → Agent / Scout Flask | A-out — no field carries these yet, even once the endpoint above exists | on demand | A/C | Not available | P2 |

Checked against this repo 2026-07-12: `motherpi/services/flask` (Scout's Flask service, `Scripts/control_authority.py`'s counterpart) is not part of this codebase — only its client stub and the operator-backend's `control_authority` proxy are. No route named `diagnostics` or `system_check` exists anywhere in the repo. Until Scout ships one, `Vehicle.js`'s "Run System Check" computes its PASS/WARN/FAIL checks from fields the operator backend already has (comm state, local-agent reporting, GPS fix, battery, CPU/disk/RAM, operator-reachability, Scout-confirmed authority) and marks the rest **Not available** — never a guessed PASS.

### Pixhawk heartbeat / MAVLink evidence — Scout-side schema (consumed now)

Updated 2026-07-12: the operator backend now **consumes** real MAVLink/heartbeat evidence when Scout forwards it. `normalize_agent_message` (`mavlink_evidence()`) reads the candidate fields below off `payload.communication` / `payload.health` / `payload.mavlink` and exposes a stable `mavlink` block on every fleet vehicle. `Vehicle.js` diagnostics turn that into real **Pixhawk heartbeat** and **MAVLink** checks (PASS ≤3 s, WARN ≤10 s, FAIL beyond; NOT AVAILABLE when absent). **Heartbeat is never inferred from GPS/position or arrival age** — only a real MAVLink HEARTBEAT field counts. Scout should emit any of (first present wins):

| Operator field (`vehicle.mavlink.*`) | Scout source fields it accepts | Meaning |
|---|---|---|
| `heartbeat_age_s` | `communication.heartbeat_age_s`, `health.pixhawk_heartbeat_age_s`, or `communication.last_heartbeat` / `mavlink.last_heartbeat` (epoch/ISO → age) | seconds since the last MAVLink HEARTBEAT Scout received from the Pixhawk |
| `connected` | `communication.mavlink_connected` / `mavlink.connected` | MAVLink link up? |
| `last_msg_age_s` | `communication.mavlink_last_msg_age_s`, or `mavlink.last_msg_time` (epoch/ISO → age) | age of the last MAVLink message of any type |
| `msg_rate_hz` | `communication.mavlink_msg_rate_hz` / `mavlink.msg_rate_hz` | inbound MAVLink message rate |
| `parser_errors` | `communication.mavlink_parser_errors` / `mavlink.parser_errors` | parser health (optional) |

Until Scout forwards these, the two checks correctly read **Not available** (which never fails the overall System Check). The `battery` / `RC receiver` / `camera` / `mission service` checks likewise stay **Not available** while those subsystems are disabled. The **RC receiver detected/healthy** signal still has no telemetry field — Vehicle.js now separates the three RC concerns (override *policy* = always-available invariant; *receiver detected* = no telem; *override active* = derived from effective authority `== RC`).

**CORRECTION, 2026-08-08 (measured off the wire).** The spellings in the table above were a *proposal*, and Scout's Local Agent does not use them. A captured live `POST /agent/status` (`tests/fixtures/scout-status-live.json`) sends, inside `payload.mavlink`:

`mavlink_connected` · `heartbeat_age_s` · `mavlink_last_msg_age_s` · `last_message_age_s` · `mavlink_msg_rate_hz` · `parser_errors` · `measured_at`

Because `mavlink_evidence()` looked for `connected` / `last_msg_age_s` / `msg_rate_hz`, every field resolved to `None` and the MAVLink row read **NO TELEM against a connected autopilot**. The canonical spellings now live in `vehicle_telemetry.mavlink_block`; the legacy `last_heartbeat` / `last_msg_time` timestamp forms above are still accepted for a pre-update Local Agent. **Do not re-derive Scout's schema from this document — read the fixture.**

### Closed by the same pass (2026-08-08)

Scout was already sending all of these; the operator simply had no field to read them from. Each now has a normalized block on every fleet row (`vehicle_telemetry.py`) and a rendered row (`operator/lib/vehicle-telemetry.js`), documented in `verification/vehicle-telemetry.md`:

| Was | Now |
|---|---|
| Battery voltage / current / power source | `payload.power` → `vehicle.power` |
| Failsafe status | `payload.failsafe` → `vehicle.failsafe` |
| IMU health | `payload.imu` → `vehicle.imu` |
| Per-stream MAVLink freshness | `payload.freshness` → `vehicle.freshness` |
| Pi service status | `payload.service_status` → `vehicle.service_status` (summarized, never dumped) |
| WireGuard state | `communication.vpn_status` → `vehicle.link.vpn` |
| RTT (application-level, Scout→Operator→Scout) | `communication.rtt_ms` → `vehicle.link.rtt_ms` |
| Operator-connected | `communication.operator_connected` → `vehicle.link.operator_connected` |
| Packet loss | **operator-measured** from `communication.seq` → `vehicle.link.packet_loss` |
| Leak sensor | `health.system.leak_sensor` → `vehicle.leak_sensor` (currently `UNCALIBRATED`) |
| Mission presence vs route readback | `mission.mission_count` / `mission.pixhawk_readback` → `vehicle.mission_status` |

Still genuinely absent from Scout: **camera** (no field, service or health entry) and **vehicle firmware version** (the old "Firmware v1.0" row was the *status message* schema version, now labelled as such).

## Experiment — communication impairment (thesis experiment control)
The Experiment page injects controlled comms impairment between the Operator Station and Scout (latency/jitter/loss/bandwidth/duplication/reordering via `tc netem`; full disconnect via a firewall rule) so degraded and **asymmetric** links can be reproduced during experiments. The frontend is done and honest: it issues a structured request and renders **only** the state the API confirms — never optimistically "active". There is **no backend implementation in this repo**, and by design the browser never runs `tc`/firewall/shell commands. This impairment is a **comms-link experiment, not a Pixhawk command**, so there is deliberately no OPERATOR/LOCAL_AGENT authority gate on it.

| Slot | Pages | Owner | Disp. | Rate | Path | Operator label | Prio |
|---|---|---|---|---|---|---|---|
| **network-impairment experiment API** (`GET/POST/DELETE /api/experiment/network`) | Experiment | Experiment controller (Scout, `tc netem`) + Operator backend (thin proxy, same pattern as `control_authority`) | B-field | on action | C | Confirmed state (was **Unavailable**) | **DONE (Stage 1)** — Operator proxy implemented in `main.py`; forwards to `{VEHICLE_API_BASE}/agent/experiment/network`, backend-owned `experiment_id`, capability-gated, never optimistic (see `verification/experiment.md`, `tests/test_experiment_network.py`). Stage 2+ (`operator_to_scout` / `both` / bandwidth / duplication / reordering / full_disconnect) rejected with a clear 400 until Scout implements them. |
| durable experiment log (persisted actions across reloads) | Experiment | Experiment controller / Operator backend | B-field | append | C → `GET /api/experiment/network/history` | Session-local only → **backend history live** | P2 · in-process append-only history + `GET /api/experiment/network/history` implemented (same pattern as `event_log`); cross-restart durable storage still a gap |

**Proposed contract** (frontend service functions in `operator/services/api.js`; payload from `operator/lib/experiment.js normalizePayload`):

- `GET /api/experiment/network` → stable schema:
  `{ status: "inactive"|"applying"|"active"|"stopping"|"failed", active: bool, experiment_id, started_at, ends_at, remaining_s, direction, profile, error }`. **`active` is the ONLY thing that drives the ACTIVE badge** — a `status:"active"` without `active:true` is never rendered active.
- `POST /api/experiment/network` (apply) body:
  `{ vehicle_id, latency_ms, jitter_ms, packet_loss_pct, bandwidth_kbit_s|null, duplication_pct, reordering_pct, full_disconnect, direction, duration_s }`.
- `DELETE /api/experiment/network` (stop) → removes the active impairment immediately.
- Direction values (asymmetry is first-class): `operator_to_scout` · `scout_to_operator` · `both`.
- Mechanism split the controller must honor: `tc netem` for delay/jitter/loss/rate/duplication/reordering; a **firewall DROP rule** for `full_disconnect` (not netem).

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

## Implementation order (agreed 2026-07-07 — backend-owned, before resuming page migration)

Architectural owners per [`SYSTEM_INFORMATION_MODEL.md`](SYSTEM_INFORMATION_MODEL.md). All three are Operator-backend-owned and shippable without hardware:

1. ~~**Comms-state transition log — `GET /api/comms/history/{id}` (P0, Operator backend).**~~ ✅ **done** (see `verification/comms-history.md`)
   The backend already derives `comm_state`; a 1 s monitor records per-vehicle transitions (state, `from`, `ts`) and exposes the history + per-state durations. **Unblocks three reserved features at once** — the Map comms Timeline, the Agent page's decision-trace comms nodes, and the thesis metric *total disconnected time*. Fully in `main.py`.

2. ~~**Persistent event log — `GET /api/events` + server-side store (P1, Operator backend).**~~ ✅ **done** (see `verification/event-log.md`)
   Real, permanent server-side store replacing the flattened-from-payload feed. **Comms-state transitions from #1 are emitted into this log as events** (PARTITIONED → caution, DISCONNECTED → warning, restored/first-contact → info), and vehicle-reported `payload.events` are ingested + deduped. Events carry stable ids and an `acknowledged` flag — ack is *design-ready* but `POST /api/events/{id}/ack` is deferred to its own P2 slot (would require wiring the Events page's session-local ack to the server).

3. **Live configuration API — `GET /api/config` (P1, Operator backend).** ← next
   Return the live thresholds (currently compiled constants) so Configuration is a genuine read, not a mirror of `main.py`. Cheap honesty win; sets up `POST /api/config` later.

**Then** (cross-component, after #1–#3): **surface the agent reasoning already emitted.** Per the information model, `payload.agent.*` (`current_behaviour`, `decision_reason`, `autonomy_level`, `current_policy`) is already sent by the Local Agent but **dropped** in `normalize_agent_message` — forwarding it closes much of the Agent page with no agent change. `decision_confidence` / `next_eval_s` / `active_constraints` remain a genuine agent-contract addition.

Anything touching `main.py` or the `POST /agent/status` schema is an outward-facing contract change — propose, get a green light, then implement.

---

## Delivered with Scout (Local Agent, port 8090)

| Slot | Pages | Owner | Prio | State |
|---|---|---|---|---|
| **`POST /agent/mission_execution/stop` + `stop` evidence + `can_stop` in status** | Map (Agent Mission card), Agent (diagnostics) | Local Agent | **P0** | **Shipped both sides** |

Stop is Scout's own **safe-abort** lifecycle transaction: verified LOITER → verify the active
mission identity → restore the immutable original mission if a verified revised route is
installed → rewind it to its start → verify the rewind → reset execution / replan / test state →
clear the experiment injection → invalidate the runtime Home → return supervisory authority to
OPERATOR → re-prove the mission evidence.

The Operator side is a **proxy plus evidence**: `scout_mission_execution.post_stop`,
`mission_lifecycle.run_stop`, `POST /api/vehicles/{id}/mission-execution/stop`, and the Map /
Agent presentation. It reimplements no step of the sequence, sends no LOITER, upload, rewind,
reset or rearm, writes no authority of its own, and never exposes the legacy raw Pixhawk stop. A
successful Stop resting at `NOT_READY` + `start_eligible=true` + `authority_blocks_start=true` is
the expected landing, not a failure. See [`SCOUT_STOP_API.md`](SCOUT_STOP_API.md).

---

## E2 experiment evidence — open contract gaps (audited 2026-08-10)

Everything the E2 water experiment's map evidence needs already exists in operator-owned
contracts and is now consumed (see `operator/docs/verification/e2-experiment-evidence.md`). What
follows is what is still MISSING, stated rather than invented.

| Slot | Pages | Owner | Prio | State |
|---|---|---|---|---|
| **`action_request` on `GET /agent/replan/status`** | Agent (four-layer card, E2 preflight) | Local Agent | **P1** | **Missing — not emitted** |
| **`revised_route` / revised waypoint geometry in the replan status or planning package** | Map (revised-route overlay before read-back) | Local Agent | P2 | Missing — geometry is only observable via the Pixhawk read-back |
| **`no_go_zone_count` in the v1 planning-package summary** | Agent (E2 preflight) | Local Agent | P0 | **Shipped both sides** — Scout emits it; the operator now carries it through `_normalize_scout_package` → `readiness.planning_package.no_go_zone_count` |

**`action_request` (REQUEST_RETURN_HOME / REQUEST_HOLD / NONE).** The E2 trigger is meant to be
observable as four INDEPENDENT statements — risk `CRITICAL`, advice `RETURN_HOME`, action request
`REQUEST_RETURN_HOME`, then the FSM progression. Three of the four are on the wire today:

* risk level → `mission_execution/status` `risk.level`
* advice → `mission_execution/status` `risk.recommendation`
* FSM → `replan/status` `fsm_state`

There is **no `action_request` field on any operator-visible Scout contract** — not on
`/agent/replan/status`, not on `/agent/mission_execution/status`, not on the status packet. The
station reads `action_request` / `requested_action` / `operator_action_request` tolerantly
(`lib/replan.js normalizeReplanStatus`) and renders whichever one Scout starts sending; until then
the row reads **"Not emitted by this Scout build"**. It is deliberately NOT rendered as `NONE`:
"no request outstanding" is a claim Scout would be making, and silence is not that claim. No
operator-side endpoint was invented for it, and none should be — the field belongs to Scout.

**Revised-route geometry.** The map draws the revised safe return from the Pixhawk read-back once
Scout has uploaded it, so the E2 evidence does not depend on this. Before the upload lands there is
nothing to draw, and `replanMapModel` reports `revisedAvailable:false` rather than drawing a guess.
