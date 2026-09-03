#send telemetry

#send sonar?

#allow images/video

#↓

#PARTITIONED

#telemetry only

#↓

#DISCONNECTED

#store locally

from config import CONNECTED_INTERVAL, PARTITIONED_INTERVAL, DISCONNECTED_INTERVAL


def telemetry_interval(comm_state):
    if comm_state == "CONNECTED":
        return CONNECTED_INTERVAL
    if comm_state == "PARTITIONED":
        return PARTITIONED_INTERVAL
    return DISCONNECTED_INTERVAL


def allowed_groups(comm_state):
    if comm_state == "CONNECTED":
        return [
            "telemetry",
            "mavlink",
            "mission",
            "communication",
            "agent",
            "health",
            "measurements",
            "fleet",
            "events",
            "transitions",
        ]

    if comm_state == "PARTITIONED":
        return [
            "telemetry",
            "mavlink",
            "mission",
            "communication",
            "agent",
            "health",
            "fleet",
            "events",
            "transitions",
        ]

    return ["mavlink", "communication", "agent", "health", "events", "transitions"]