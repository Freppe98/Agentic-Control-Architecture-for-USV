# Agentic Control Architecture for Autonomous USVs

Master’s thesis project developing and evaluating an agentic supervisory-control architecture for autonomous Uncrewed Surface Vehicles operating under intermittent communication.

The system separates low-level vehicle control, onboard autonomy, and offboard mission supervision into explicit layers. Each USV has a Local Mission Agent that maintains situational awareness, evaluates communication and vehicle state, applies safety rules, and can continue operating when the Operator Station becomes unavailable.

The current prototype supports mission planning, validated MAVLink mission upload and readback, multi-USV monitoring, operator/local-agent authority transfer, communication-degradation experiments, and onboard mission adaptation.

## Installation

### Operator Station on Windows

The Operator Station consists of a Python/FastAPI backend and a browser-based frontend.

#### Prerequisites

- Git for Windows
- Python 3.11 or newer
- Node.js LTS, including npm
- PowerShell

IMPORTANT
Update the config on USVs you want to be able to connect to. Add your computers Wireguard URL.
-> AqualityONE/motherpi/services/local_mission_agent/config.py

Clone the repository:

```powershell
git clone <repository-url>
cd Agentic-Control-Architecture-for-USV\operator-scripts
```

Allow scripts for the current PowerShell session:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

Run the Operator Station installer:

```powershell
.\install_operator.ps1
```

The installer:

- verifies Git, Python, Node.js, and npm;
- creates a local Python virtual environment;
- installs pinned Python dependencies;
- installs locked Node dependencies;
- creates `.env` from `.env.example` when needed;
- runs installation and test checks.

Start the Operator Station:

```powershell
.\run_operator_backend.ps1
```

The backend normally listens on:

```text
http://localhost:8210
```

Open the Operator interface at:

```text
http://localhost:8210/app
```

The run script also prints the computer’s network addresses so the Operator Station can be reached from other devices.

### Reinstalling Dependencies

After pulling dependency changes, rerun:

```powershell
.\install_operator.ps1
```

The installer is designed to be safe to run repeatedly and does not overwrite an existing `.env`.

### Manual Development Setup

Python dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Node dependencies:

```powershell
npm ci
```

Run the backend:

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 8210
```

## System Overview

The architecture has three principal control layers:

```text
┌──────────────────────────────────────────────────────────────┐
│ Operator Station — offboard                                  │
│                                                              │
│ Mission planning · fleet monitoring · command history        │
│ communication experiments · mission replay · agent insight   │
└──────────────────────────────┬───────────────────────────────┘
                               │
                     4G / Wi-Fi / WireGuard
                     intermittent communication
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Local Mission Agent — onboard Raspberry Pi 5                 │
│                                                              │
│ State abstraction · communication monitoring · authority     │
│ command handling · safety policy · decision reasoning        │
│ mission supervision · future mission replanning              │
└──────────────────────────────┬───────────────────────────────┘
                               │
                         USB MAVLink
                               │
┌──────────────────────────────▼───────────────────────────────┐
│ Pixhawk 6C / ArduRover — low-level vehicle control           │
│                                                              │
│ Stabilization · navigation · waypoint execution · LOITER     │
│ AUTO · MANUAL · RTL · arming · sensor and power telemetry    │
└──────────────────────────────────────────────────────────────┘
```

The Pixhawk remains responsible for deterministic low-level navigation. The Local Mission Agent supervises the mission and applies higher-level safety and autonomy decisions. The Operator Station provides planning, monitoring, and explicit supervisory commands.

## Research Objective

The project investigates whether an onboard agent can improve the resilience and transparency of USV operations under degraded or unavailable communication.

The design focuses on:

- continued safe operation during communication loss;
- clear separation between operator and autonomous authority;
- explainable onboard decisions;
- validated mission transfer;
- bounded mission adaptation;
- multi-USV and multi-operator extensibility;
- reproducible communication-degradation experiments.

The implemented agent is currently deterministic and policy-based. It uses explicit state, thresholds, and transition rules rather than unrestricted generative control.

## Current Capabilities

### Mission Planning

The Plan page supports:

- drawing a navigable survey area;
- defining shoreline safety distance;
- drawing no-go zones;
- configuring survey-line spacing;
- generating boustrophedon or lawnmower routes;
- adding approach and return waypoints;
- validating the generated mission;
- uploading the mission to a selected USV.

The planning map initially centers using the following priority:

1. selected USV position;
2. another recently contacted USV;
3. operator browser location;
4. previously saved map viewport;
5. configured fallback location.

### Mission Contract and Validated Upload

Operator missions use a versioned mission contract:

```text
mission-contract-v1
```

Mission transfer includes:

- canonical waypoint representation;
- SHA-256 route identity;
- validation before transmission;
- support for `MISSION_REQUEST` and `MISSION_REQUEST_INT`;
- precision-preserving `MISSION_ITEM_INT` transmission;
- MAVLink acknowledgement handling;
- full Pixhawk mission readback;
- exact or transport-normalized verification;
- bounded mismatch diagnostics.

A successful HTTP request alone is not considered proof of mission installation. The uploaded mission must be read back from the Pixhawk and verified.

### Mission Display

The Map page:

- fetches the selected USV mission automatically;
- shows a valid loaded mission by default;
- tracks mission progress and active waypoint;
- retains the last known mission during communication loss;
- provides one stateful `Show mission` / `Hide mission` control;
- caches mission identity per USV;
- avoids downloading complete mission geometry on every heartbeat.

### Vehicle Commands

Supported supervisory actions include:

- AUTO;
- MANUAL;
- LOITER;
- RTL;
- ARM;
- DISARM;
- SET HOME;
- pause mission;
- resume mission;
- upload mission;
- clear mission.

LOITER is treated as a high-priority safety command because it actively holds position and reduces uncontrolled drifting.

### Upload While Armed in LOITER

Mission upload is permitted while armed only when the USV is safely holding position in verified LOITER.

The onboard safety policy requires:

- fresh heartbeat;
- confirmed LOITER mode;
- fresh position;
- fresh groundspeed;
- groundspeed at or below the configured threshold;
- no active mode transition;
- no concurrent mission write.

Mission upload never automatically:

- enters LOITER;
- disarms the vehicle;
- resumes AUTO.

A typical operational sequence is:

```text
AUTO
→ LOITER
→ confirm stationary hold
→ upload and verify revised mission
→ remain in LOITER
→ explicitly resume AUTO
```

This workflow is shared by operator-initiated mission replacement and future agent-initiated replanning.

### Control Authority

Two supervisory-authority states are used:

```text
OPERATOR
LOCAL_AGENT
```

`OPERATOR` means explicit human supervisory commands may execute.

`LOCAL_AGENT` means autonomous supervision owns command authority. Non-exempt operator commands are rejected clearly rather than silently ignored.

LOITER remains available as a safety command.

### Communication Awareness

The Operator Station uses application-data arrival age as the primary communication indicator:

```text
CONNECTED
PARTITIONED
DISCONNECTED
```

Additional diagnostic information may include:

- round-trip time;
- packet loss;
- jitter;
- throughput;
- command acknowledgement delay;
- WireGuard handshake age;
- disconnect duration;
- recovery time.

The Map page retains last-known state during degradation and marks it stale instead of replacing useful information with empty values.

### Communication Experiments

The Experiment page supports controlled network impairment parameters such as:

- latency;
- jitter;
- packet loss;
- bandwidth;
- impairment direction;
- experiment duration;
- full disconnect.

The intended Linux implementation uses `tc netem` and explicit firewall rules. Experiments are logged for later evaluation.

### Fleet Support

The architecture is keyed by USV ID and designed for expansion to multiple vehicles and Operator Stations.

The Operator interface includes:

- fleet roster;
- selected-USV state;
- per-USV mission cache;
- per-USV telemetry cache;
- communication and health state;
- command history;
- agent decision information;
- event history.

## Operator Interface

The Operator Station currently contains the following main pages:

```text
Map
Fleet
Mission
Plan
Video
Pilot
Vehicle
Agent
Events
Experiment
Configuration
Terminal
Messages
```

### Map

Primary operational view containing:

- fleet vehicle panel;
- selected-USV marker;
- current mission;
- mission progress;
- vehicle telemetry;
- communication freshness;
- control authority;
- vehicle and agent commands;
- Home-position state.

### Plan

Mission-generation interface for survey-area, no-go-zone, and route construction.

### Agent

Displays onboard autonomy information such as:

- current behavior;
- mission state;
- communication state;
- current policy;
- decision reason;
- constraints;
- recent transitions.

### Experiment

Configures and records controlled communication degradation.

## Repository Structure

```text
Agentic-Control-Architecture-for-USV/
│
├── operator-scripts/
│   ├── main.py
│   │   └── FastAPI Operator backend
│   │
│   ├── operator/
│   │   ├── app.js
│   │   ├── index.html
│   │   ├── components/
│   │   ├── pages/
│   │   ├── services/
│   │   ├── lib/
│   │   ├── styles/
│   │   └── docs/verification/
│   │
│   ├── tests/
│   │   ├── JavaScript frontend tests
│   │   └── Python backend tests
│   │
│   ├── install_operator.ps1
│   ├── run_operator_backend.ps1
│   ├── requirements.txt
│   ├── package.json
│   ├── package-lock.json
│   └── .env.example
│
├── local-agent-scripts/
│   └── Local Agent support and deployment scripts
│
├── Scripts/
│   └── General utility and development scripts
│
└── README.md
```

The current Scout vehicle software is deployed on the Raspberry Pi 5 in the AqualityONE vehicle codebase. The Operator Station communicates with it through the Local Mission Agent rather than connecting directly from the browser.

## Data Flow

### Vehicle to Local Mission Agent

Typical Pixhawk-derived state includes:

```text
position:
  latitude
  longitude
  altitude
  heading

motion:
  groundspeed
  current mission sequence

vehicle:
  armed
  mode
  heartbeat age

power:
  battery percentage
  voltage
  current

gps:
  fix type
  satellite count
  horizontal dilution

mission:
  mission count
  mission validity
  current sequence
  route hash
```

MAVLink sentinel values such as an unavailable battery percentage are treated as missing observations rather than valid measurements. Last-known values are retained with independent freshness information.

### Local Mission Agent to Operator Station

A status message contains normalized state such as:

```json
{
  "usv_id": "usv-2",
  "communication_state": "CONNECTED",
  "mission_state": "EXECUTING",
  "control_authority": "OPERATOR",
  "current_behavior": "monitoring",
  "decision_reason": "Mission execution within current safety constraints",
  "telemetry": {
    "latitude": 56.7,
    "longitude": 13.0,
    "heading": 180,
    "groundspeed": 1.2,
    "battery": 97,
    "armed": true,
    "mode": "AUTO"
  }
}
```

### Operator Station to Local Mission Agent

Operator commands are queued with:

- command ID;
- vehicle ID;
- command type;
- parameters;
- creation time;
- expiry time;
- requested communication state;
- result and acknowledgement history.

Mission uploads may also include an audit context:

```text
OPERATOR_REPLACEMENT
AGENT_REPLAN
```

The context does not bypass onboard safety checks.

## Safety Principles

The prototype follows several explicit safety rules:

- low-level control remains on the Pixhawk;
- mission writes are serialized;
- mission upload is verified by readback;
- uncertain or stale state fails closed for safety-critical actions;
- LOITER remains available as an active anti-drift safety action;
- mission upload does not hide mode changes inside the upload operation;
- failed replanning leaves the vehicle in LOITER;
- AUTO resumes only after explicit operator or agent approval;
- stale data is retained but clearly marked as not live;
- operator authority and autonomous authority are represented explicitly.

## Running Tests

From `operator-scripts`:

### Frontend Tests

```powershell
npm test
```

or:

```powershell
node --test "tests/**/*.test.mjs"
```

### Backend Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Run both before committing Operator Station changes.

## Configuration

Local configuration belongs in:

```text
operator-scripts/.env
```

Use `.env.example` as the template.

The real `.env` is excluded from Git and should not contain committed credentials, VPN keys, or private tokens.

Common settings include:

- Operator backend host;
- Operator backend port;
- logging level;
- known Local Agent endpoints;
- development or deployment mode.

## Typical Deployment Workflow

```text
1. Start the Operator backend.
2. Start the Local Mission Agent on the USV.
3. Confirm fleet contact and telemetry freshness.
4. Select the USV in the Operator interface.
5. Set or verify the Pixhawk Home position.
6. Draw and validate a mission on the Plan page.
7. Upload the mission.
8. Verify Pixhawk readback and mission hash.
9. Arm the USV.
10. Enter AUTO and monitor execution.
11. Use LOITER as the primary active safety hold.
12. Resume, replace, or replan the mission only after verification.
```

## Current Thesis Scope

The current thesis prototype concentrates on supervisory autonomy rather than replacing the Pixhawk navigation controller.

The main evaluation areas are:

- behavior under intermittent communication;
- communication-state classification;
- operator versus Local Agent authority;
- mission-transfer integrity;
- explainability of autonomous decisions;
- safe mission interruption and replacement;
- bounded path replanning;
- system recovery after communication restoration.

## Planned Development

Remaining or future work includes:

- agent-triggered mission replanning;
- obstacle-aware detour generation;
- emergency short-range obstacle behavior;
- lightweight mission-generation and `current_seq` reporting;
- improved energy-risk estimation;
- integration of forward obstacle sensors;
- fleet-level mission allocation;
- evaluation using repeatable field and communication experiments.

## Important Limitations

This repository contains a research prototype, not a certified marine control system.

Mission-planning backgrounds and aerial imagery may contain positional offsets. GNSS mission coordinates remain authoritative, while map imagery is treated as an operator aid.

Battery, communication, and environmental thresholds must be calibrated for the actual vehicle before field operation.

All field tests should include:

- manual RC override;
- accessible emergency stop;
- verified Home position;
- conservative shoreline margins;
- appropriate local permissions;
- continuous visual supervision during development testing.
