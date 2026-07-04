from __future__ import annotations

import json
import time

from collectors import (
    get_agent_status,
    get_communication_status,
    get_fleet_status,
    get_health_status,
    get_measurements_status,
    get_mission_status,
    get_telemetry_status,
)
from information_policy import allowed_groups


USV_ID = 2
USV_NAME = "Scout"


def make_message(message_type: str, source: str, target: str, payload: dict) -> dict:
    return {
        "message_type": message_type,
        "schema_version": "1.0",
        "source": source,
        "target": target,
        "timestamp": time.time(),
        "payload": payload,
    }


def build_status_payload(comm_state) -> dict:
    groups = allowed_groups(comm_state)

    payload = {
        "usv_id": USV_ID,
        "name": USV_NAME,
        "comm_state": getattr(comm_state, "name", comm_state),
        "groups": groups,
    }

    if "telemetry" in groups:
        payload["telemetry"] = get_telemetry_status()
    if "mission" in groups:
        payload["mission"] = get_mission_status()
    if "communication" in groups:
        payload["communication"] = get_communication_status(comm_state)
    if "agent" in groups:
        payload["agent"] = get_agent_status(comm_state)
    if "health" in groups:
        payload["health"] = get_health_status()
    if "measurements" in groups:
        payload["measurements"] = get_measurements_status()

    payload["fleet"] = get_fleet_status()
    payload["events"] = []

    return payload


def build_status_message(comm_state) -> dict:
    payload = build_status_payload(comm_state)
    return make_message(
        message_type="status",
        source=f"usv-{USV_ID}",
        target="operator",
        payload=payload,
    )


def main() -> None:
    message = build_status_message("CONNECTED")
    print(json.dumps(message, indent=2))


if __name__ == "__main__":
    main()