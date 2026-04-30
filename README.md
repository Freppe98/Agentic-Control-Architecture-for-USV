# Agentic-Control-Architecture-for-USV
Master Thesis Agentic Control Architecture for Aquality Autonomous USVs.

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
