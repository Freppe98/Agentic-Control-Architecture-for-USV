from __future__ import annotations

from fsm_agent import CommState


def _comm_state_name(comm_state):
    return getattr(comm_state, "name", str(comm_state)).upper()


def allowed_groups(comm_state):
    state = _comm_state_name(comm_state)

    if state == CommState.CONNECTED.name:
        return [
            "telemetry",
            "mission",
            "communication",
            "agent",
            "health",
            "measurements",
            "fleet",
            "events",
        ]

    if state == CommState.PARTITIONED.name:
        return [
            "telemetry",
            "mission",
            "communication",
            "agent",
            "health",
            "fleet",
            "events",
        ]

    return [
        "communication",
        "agent",
        "health",
        "events",
    ]