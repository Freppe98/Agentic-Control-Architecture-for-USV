# Agentic-Control-Architecture-for-USV
Master Thesis Agentic Control Architecture for Aquality Autonomous USVs.

## 📁 Project Structure

```text
USV / Raspberry Pi 5
│
├── adapters/
│   ├── mavlink_adapter.py        # Pixhawk 6C via USB (MAVLink)
│   ├── global_adapter.py         # Server/operator via router (4G/Wi-Fi)
│   ├── swarm_adapter.py          # Other USVs via network
│   ├── sonar_adapter.py          # Sonar via Ethernet
│   └── sensor_adapter.py         # I2C, UART, GPIO sensors
│
├── core/
│   ├── state_abstraction.py      # Timestamping, freshness, confidence, fusion
│   ├── intent_contract.py        # Goals, constraints, authority, validity
│   ├── fsm_agent.py              # Communication-aware decision logic
│   └── safety_monitor.py         # Safety rules and fallback triggers
│
├── outputs/
│   ├── command_adapter.py        # Converts decisions → MAVLink / server msgs
│   ├── logger.py                 # Experiment logging
│   └── telemetry_publisher.py    # Sends abstracted state upstream
│
└── simulation/
    ├── dashboard_simulator.py    # Simulated operator / global layer
    ├── swarm_simulator.py        # Simulated USV swarm
    ├── sensor_simulator.py       # Simulated sensors / sonar
    └── network_emulation/        # Mininet / tc netem configurations
```

## Parameters 1. Pixhawk → Agent (MAVLink Telemetry)

```text
position:
  lat (float)
  lon (float)
  heading_deg (float)

velocity:
  ground_speed_mps (float)

mission:
  current_waypoint (int)
  waypoint_reached (bool)

battery:
  percent (float)
  voltage (float, optional)
  current (float, optional)

health:
  fault_detected (bool)

pixhawk_link:
  heartbeat_age_s (float)
  connected (bool)

gps:
  fix_type (int)
  satellites_visible (int)
  hdop (float)
```

## Parameters 2. Agent → Pixhawk

```text
SET_MODE:
  HOLD
  AUTO
  GUIDED
  RTL

MISSION_CONTROL:
  upload_waypoints
  clear_mission

NAVIGATION:
  set_target_position (lat, lon)
  change_speed (m/s)

SAFETY:
  arm / disarm
```

## Parameters 3. Global → Agent (Unreliable Link)

```text
{
  "type": "mission_update",
  "timestamp_s": float,
  "operator_command": "EXECUTE | STOP | RETURN_HOME | UPDATE",
  "mission_goal": "SEARCH_AREA | HOLD_POSITION | RETURN_HOME | LOITER_AREA",

  "waypoints": [
    {"id": "wp1", "lat": float, "lon": float}
  ],

  "speed_limit_mps": float,

  "operator_override": "RETURN_HOME | HOLD_POSITION | null",
  "emergency_stop": bool,

  "expected_neighbors": int,
  "connected_neighbors": int,

  "communication_status": {
    "latency_ms": float,
    "packet_loss": float
  },

  "target_confidence": float
}

comm_state:
  CONNECTED
  DEGRADED
  PARTITIONED
  DISCONNECTED

last_intent_timestamp_s (float)
```

## Parameters 4. Agent → Global (Telemetry / Feedback)

```text
{
  "type": "usv_status",
  "timestamp_s": float,

  "usv_id": "string",

  "agent_state": "IDLE | SEARCH | DEGRADED | RETURN | HOLD | EMERGENCY_STOP",
  "active_goal": "SEARCH_AREA | RETURN_HOME | ...",

  "current_position": {
    "lat": float,
    "lon": float,
    "heading_deg": float
  },

  "battery_percent": float,

  "communication_state": "CONNECTED | DEGRADED | PARTITIONED | DISCONNECTED",

  "last_global_message_age_s": float,
  "last_pixhawk_heartbeat_age_s": float,

  "current_waypoint_id": "string",

  "fault_detected": bool,

  "explanation": "string"
}
```
