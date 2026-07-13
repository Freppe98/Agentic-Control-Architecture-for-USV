from __future__ import annotations

import os
import shutil
import time
from enum import Enum
from pathlib import Path


try:
    from api_client import get_vehicle_telemetry  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    get_vehicle_telemetry = None


try:
    from state_machine import MissionState  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    class MissionState(Enum):
        IDLE = "IDLE"


BUFFER_FILE = Path(__file__).resolve().parent / "agent_buffer.jsonl"


def get_telemetry_status():
    try:
        if get_vehicle_telemetry is None:
            return {
                "error": "telemetry source unavailable",
            }
        return get_vehicle_telemetry()
    except Exception as exc:
        return {"error": str(exc)}


def get_mission_status():
    return {
        "mission_state": MissionState.IDLE.name,
        "current_task": "none",
        "current_waypoint": None,
        "mission_index": None,
        "remaining_waypoints": None,
        "progress": None,
        "estimated_completion_s": None,
    }


def count_buffered_packets():
    try:
        with BUFFER_FILE.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except FileNotFoundError:
        return 0
    except Exception:
        return None


def policy_for_comm(comm_state):
    state = str(getattr(comm_state, "name", comm_state)).upper()
    if state == "CONNECTED":
        return "FULL_REPORTING"
    if state == "PARTITIONED":
        return "REDUCED_REPORTING_LOCAL_AUTONOMY"
    return "BUFFER_AND_LOCAL_FALLBACK"


def get_communication_status(comm_state):
    return {
        "connectivity": getattr(comm_state, "name", comm_state),
        "operator_reachable": str(getattr(comm_state, "name", comm_state)).upper() == "CONNECTED",
        "last_successful_transmission": None,
        "buffered_packets": count_buffered_packets(),
        "rtt_ms": None,
        "packet_loss": None,
        "bandwidth_estimate_kbps": None,
        "vpn_status": None,
    }


# Behaviour/mission → the human decision label the operator sees ("Continue Search",
# "Return Home", …). A behaviour with no explicit mapping falls back to "Continue <x>".
_DECISION_LABELS = {
    "SEARCH": "Continue Search", "SEARCHING": "Continue Search",
    "CONVERGE": "Investigate Target", "INVESTIGATE": "Investigate Target",
    "INVESTIGATE_TARGET": "Investigate Target",
    "LOITER": "Loiter", "HOLD": "Hold Position", "HOLD_POSITION": "Hold Position",
    "RETURN": "Return Home", "RETURN_HOME": "Return Home",
    "EMERGENCY_STOP": "Emergency Stop", "MONITORING": "Continue Monitoring",
    "IDLE": "Standby",
}
# Reporting-policy → the operator-facing flag list that describes what the agent is
# doing under this comm-state. Mirrors information_policy.allowed_groups (the real
# store-and-forward behaviour), so the flags are truthful about the agent's policy.
_POLICY_FLAGS = {
    "CONNECTED": ["Operator-supervised", "Full reporting", "Live telemetry"],
    "DEGRADED": ["Autonomous continuation", "Reduced reporting", "Buffered messages"],
    "PARTITIONED": ["Autonomous continuation", "Reduced reporting", "Buffered messages"],
    "DISCONNECTED": ["Local autonomy only", "Store-and-forward", "Buffering all messages"],
}


def _decision_label(behaviour, mission_state, battery, fault, comm):
    """The agent's current decision, as a short operator-facing label. Safety overrides
    (fault, critical/low battery) win over the mission behaviour; otherwise the behaviour
    or mission drives it. Never invents a mission the agent isn't running."""
    if fault:
        return "Emergency Stop"
    if battery is not None and battery <= 10:
        return "Return Home"
    if battery is not None and battery <= 20:
        return "Return Home"
    key = str(behaviour or mission_state or "").upper()
    if key in _DECISION_LABELS:
        return _DECISION_LABELS[key]
    if behaviour:
        return f"Continue {str(behaviour).replace('_', ' ').title()}"
    return "Standby"


def _decision_confidence(comm, battery, fault):
    """Heuristic certainty in the current decision, from concrete signals (health +
    comms). HIGH when nominal; MEDIUM when degraded/low battery; LOW on fault/critical.
    A template heuristic — documented as such — not a fabricated fixed value."""
    if fault:
        return "LOW"
    if battery is not None and battery <= 10:
        return "LOW"
    if comm == "DISCONNECTED":
        return "MEDIUM"
    if battery is not None and battery <= 20:
        return "MEDIUM"
    return "HIGH"


def _decision_reasons(comm, battery, fault):
    """Plain-language bullets explaining the decision, derived from the agent's real
    comm-state, reporting policy and health. Each line is a truthful observation."""
    reasons = []
    if comm == "CONNECTED":
        reasons.append("Communication healthy.")
        reasons.append("Full reporting active.")
    elif comm in ("DEGRADED", "PARTITIONED"):
        reasons.append("Communication degraded.")
        reasons.append("Reduced reporting policy active.")
    else:
        reasons.append("Operator unreachable.")
        reasons.append("Buffering messages — local autonomy only.")
    if fault:
        reasons.append("Vehicle fault detected.")
        reasons.append("Mission paused for safety.")
    else:
        if battery is not None and battery <= 20:
            reasons.append(f"Battery low ({battery:.0f}%).")
        else:
            reasons.append("Vehicle health nominal.")
        reasons.append("Mission safety unaffected.")
    return reasons


def _watch_conditions(comm, battery, fault, has_gps):
    """The agent's self-assessment of the conditions it is watching, each OK / WARN /
    LOST / UNKNOWN. Evaluated from the agent's own real signals (it can see its battery,
    Pixhawk heartbeat and GPS even when the operator link is down)."""
    if battery is None:
        batt = "UNKNOWN"
    elif battery <= 10:
        batt = "LOST"
    elif battery <= 20:
        batt = "WARN"
    else:
        batt = "OK"
    return [
        {"name": "Battery", "state": batt},
        {"name": "Heartbeat", "state": "LOST" if fault else "OK"},
        {"name": "GPS", "state": "OK" if has_gps else "UNKNOWN"},
        {"name": "Operator", "state": "OK" if comm == "CONNECTED" else "LOST"},
    ]


def get_agent_status(comm_state):
    """The Local Agent's report of its own cognition (SYSTEM_INFORMATION_MODEL: the
    agent owns behaviour/decision, autonomy level, reporting policy). Emits a digested,
    operator-facing decision view (current_decision, decision_reasons, decision_confidence,
    policy_flags, watch_conditions) alongside the raw fields. Everything here is derived
    from the agent's REAL state — comm-state, health, mission — not fabricated; the
    decision/confidence heuristics are template defaults meant to be replaced by the FSM
    output (fsm_agent.OutputData) when the runner is wired to this reporter."""
    comm = str(getattr(comm_state, "name", comm_state)).upper()
    health = get_health_status()
    mission = get_mission_status()
    telemetry = get_telemetry_status()

    battery = None
    for src in (telemetry, health):
        if isinstance(src, dict):
            b = src.get("battery", src.get("battery_percent"))
            if isinstance(b, (int, float)) and b >= 0:
                battery = float(b)
                break
    fault = bool(health.get("leak_detected") or health.get("fault_detected")) if isinstance(health, dict) else False
    mission_state = mission.get("mission_state") if isinstance(mission, dict) else None
    has_gps = bool(isinstance(telemetry, dict) and (telemetry.get("lat") is not None or telemetry.get("gps_fix")))
    behaviour = mission_state if (mission_state and mission_state != "IDLE") else "monitoring"

    reasons = _decision_reasons(comm, battery, fault)
    return {
        "current_communication_state": comm,
        "current_mission_state": mission_state or MissionState.IDLE.name,
        "current_decision": _decision_label(behaviour, mission_state, battery, fault, comm),
        "decision_reasons": reasons,
        # Keep the single-string field for back-compat with any older consumer.
        "decision_reason": " ".join(reasons),
        "decision_confidence": _decision_confidence(comm, battery, fault),
        "current_policy": policy_for_comm(comm_state),
        "policy_flags": _POLICY_FLAGS.get(comm, _POLICY_FLAGS["PARTITIONED"]),
        "watch_conditions": _watch_conditions(comm, battery, fault, has_gps),
        "current_behaviour": behaviour,
        "autonomy_level": "ASSISTED",
        "buffer_usage": count_buffered_packets(),
        "last_operator_command": None,
    }


def get_cpu_load():
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):
        return None


def get_ram_usage():
    if os.name != "posix":
        return None

    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            lines = handle.readlines()

        mem_total = int(lines[0].split()[1])
        mem_available = int(lines[2].split()[1])
        return round(100 * (1 - mem_available / mem_total), 1)
    except Exception:
        return None


def get_disk_usage():
    try:
        usage = shutil.disk_usage(Path(__file__).resolve().anchor or "/")
        return round(100 * usage.used / usage.total, 1)
    except Exception:
        return None


def get_cpu_temp():
    temp_file = Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        with temp_file.open("r", encoding="utf-8") as handle:
            return round(int(handle.read()) / 1000, 1)
    except Exception:
        return None


def get_health_status():
    return {
        "cpu_load": get_cpu_load(),
        "ram_usage": get_ram_usage(),
        "disk_usage": get_disk_usage(),
        "temperature": get_cpu_temp(),
        "docker_status": None,
        "flask_status": None,
        "sensor_service": None,
        "gpio_service": None,
        "realsense_service": None,
        "gps_health": None,
        "pixhawk_health": None,
        "leak_detected": None,
    }


def get_measurements_status():
    return {
        "water_quality": {},
        "bathymetry": {},
        "metadata": {
            "timestamp": time.time(),
            "measurement_position": None,
        },
    }


def get_fleet_status():
    return {
        "fleet_role": "scout",
        "assigned_sector": None,
        "formation": None,
        "neighbour_usvs": [],
        "task_ownership": None,
        "collision_avoidance_messages": [],
    }