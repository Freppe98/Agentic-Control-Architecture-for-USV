# SYSTEM_INFORMATION_MODEL.md

Architectural reference for **who owns which information** in the agentic USV control system. Every field in [`DATA_DICTIONARY.md`](DATA_DICTIONARY.md) and every gap in [`BACKEND_ROADMAP.md`](BACKEND_ROADMAP.md) resolves to exactly one **source of truth** here. Backend work should not invent a field whose owner is elsewhere — it should forward, derive, or wait.

## The five owners

| Owner | Where it runs | Source of truth for | Never the source of |
|---|---|---|---|
| **Vehicle (USV)** | Pixhawk autopilot + hull sensors, over MAVLink | Raw physics: position, heading, speed, mode/armed, battery, GPS fix/sats, leak, temps, voltages, camera frames, sonar/water probes | Any decision or interpretation |
| **Local Agent** | Onboard Pi, per USV (`Scripts/`: `fsm_agent.py`, `agent_runner.py`, `collectors.py`, `local_agent.py`, `global_adapter.py`, `information_policy.py`) | Onboard cognition + the report it sends: behaviour/decision, autonomy level, **store-and-forward reporting policy**, buffered-packet count, its own view of the offboard link, Pi health | Fleet-wide truth; operator-side link quality |
| **Mission Agent** | Global/mission layer upstream of each Local Agent (reaches the Pi via `global_adapter.py` UDP) | Mission intent: goal, waypoint plan, operator overrides, emergency-stop, sector/formation assignment, target confidence, swarm coordination | Per-vehicle telemetry; UI state |
| **Operator Backend** | FastAPI (`operator-scripts/main.py`) | The **operator-side** truth: arrival-age → comm-state, comms-transition log, event log, config (thresholds), command queue, fleet aggregation, environment | Anything the vehicle/agent measures (only relays it) |
| **Frontend** | Operator station (`operator/`) | Presentation + local prefs only (units, coord format, base layer). Derives display values (last-contact age styling, coverage averages) | Any authoritative data — it must never be a source of truth |

## Data flow

```
 Mission Agent ──(goal, waypoints, overrides; UDP)──▶ Local Agent (Pi)
                                                        │  reads ▲ MAVLink
                                                        ▼        │
                                                   Vehicle (USV autopilot + sensors)
 Local Agent ──(status envelope, store-and-forward by comm-state)──▶ Operator Backend
   POST /agent/status  { message_type, source: usv-N, target: operator, payload:{...} }
 Operator Backend ──(derive comm-state from arrival age; log transitions; aggregate)──▶
   GET /api/fleet/status · GET /api/comms/history/{id} · GET /api/events · GET /api/config
 Frontend ──(poll ~2s; render availability states)──▶ Operator
 Operator ──(command)──▶ Operator Backend ──(queue; deliver on next contact)──▶ Local Agent
```

## Two comm-state perspectives (a thesis-critical distinction)

Comm-state is **derived twice, by different owners, and they are not the same signal**:

1. **Agent-side link view** — `global_adapter.derive_comm_state()` on the Pi, from the *heartbeat age of the offboard link* (CONNECTED <3s · DEGRADED <8s · PARTITIONED <15s · DISCONNECTED ≥15s). This drives the Local Agent's **reporting policy** (`information_policy.allowed_groups`) — i.e. what the vehicle *chooses to send* while degraded — and is reported as `payload.comm_state`.
2. **Operator-side arrival view** — `main.py`, from *age since the last `POST /agent/status`* (CONNECTED · PARTITIONED >15s · DISCONNECTED >30s; STALE 8s only dims telemetry). This is what the operator UI shows and what the **comms-transition log** records — the operator's ground truth of reachability.

The roadmap's comms-transition log is the **operator-side** view (reachability as the operator experiences it), which is what the thesis "total disconnected time" metric needs. When both are available the UI can later contrast them (what the operator sees vs. what the vehicle believes) — a genuine agentic-comms insight, not a discrepancy to hide.

## Store-and-forward reporting policy (Local Agent)

`information_policy.allowed_groups(comm_state)` is the intermittent-comms behaviour at the heart of the thesis: as the link degrades the agent drops data groups (CONNECTED → all; PARTITIONED → no `measurements`; DISCONNECTED → only `communication`/`agent`/`health`/`events`) and buffers the rest. Consequences the UI must respect: a field being absent in a degraded payload is **LAST_KNOWN / No data received**, *not* a fault — the vehicle deliberately withheld it. Owner of the policy: Local Agent.

## Source-of-truth map (major objects)

| Information object | Owner | Reaches backend via | Notes |
|---|---|---|---|
| Position / heading / speed / mode / armed | Vehicle | `payload.telemetry` | autopilot; relayed untouched |
| Battery / GPS fix / leak / temps / voltages | Vehicle | `payload.telemetry` / `payload.health` | many not yet forwarded (roadmap) |
| Behaviour / decision reason / autonomy level | **Local Agent** | `payload.agent.*` | emitted by `get_agent_status`; **now forwarded** by `normalize_agent_message` as `agent_status` (with last-known carry-forward) and shown verbatim on the Agent page |
| Reporting policy / buffered packets | Local Agent | `payload.agent` / `payload.communication` | store-and-forward depth |
| Agent-side comm-state | Local Agent | `payload.comm_state` | link *the vehicle believes it has* |
| Mission goal / waypoint plan / overrides / sector | **Mission Agent** | not yet surfaced to backend | needs a mission-object path (roadmap P1) |
| Operator-side comm-state + transition log | **Operator Backend** | derived in `main.py` | the UI's comm axis; #1 in the plan |
| Event log / acknowledgement | Operator Backend | to be added | #2 in the plan |
| Thresholds / config | Operator Backend | compiled today → `GET /api/config` | #3 in the plan |
| Fleet aggregation / environment | Operator Backend | `GET /api/fleet/status`, `/api/environment` | already live |
| Units / coord format / base layer | Frontend | localStorage | never sent to backend |

## Implications for backend work
- **Forward before you invent.** `payload.agent.*` and `payload.communication.*` already carry more than the backend surfaces; forwarding them closes Autonomy gaps without any agent change.
- **Respect owner boundaries.** The Operator Backend must not synthesize telemetry or reasoning — only relay (Vehicle/Agent), derive from arrival (comm-state), or store operator-side records (events, config, commands).
- **Absent ≠ broken.** Under the reporting policy, missing groups are deliberate — render LAST_KNOWN / No data, never FAULT.
