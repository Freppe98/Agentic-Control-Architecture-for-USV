# Agentic-Control-Architecture-for-USV
Master Thesis Agentic Control Architecture for Aquality Autonomous USVs.


USV / Raspberry Pi 5
│
├── adapters/
│   ├── mavlink_adapter.py        # Pixhawk 6C via USB serial
│   ├── global_adapter.py         # server/operator via router, 4G/Wi-Fi
│   ├── swarm_adapter.py          # other USVs via server/router
│   ├── sonar_adapter.py          # sonar via Ethernet
│   └── sensor_adapter.py         # I2C, UART, GPIO sensors
│
├── core/
│   ├── state_abstraction.py      # timestamps, freshness, confidence, fusion
│   ├── intent_contract.py        # goals, constraints, authority, validity
│   ├── fsm_agent.py              # decision-making logic
│   └── safety_monitor.py         # hard safety rules / fallback triggers
│
├── outputs/
│   ├── command_adapter.py        # converts decisions to MAVLink/server msgs
│   ├── logger.py                 # experiment logs
│   └── telemetry_publisher.py    # sends abstracted state upward
│
└── simulation/
    ├── dashboard_simulator.py    # fake global server/operator
    ├── swarm_simulator.py        # fake other USVs
    ├── sensor_simulator.py       # fake sonar/sensor inputs
    └── network_emulation/        # Mininet/tc configs
