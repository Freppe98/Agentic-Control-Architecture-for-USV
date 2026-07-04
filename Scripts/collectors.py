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


def get_agent_status(comm_state):
    return {
        "current_communication_state": getattr(comm_state, "name", comm_state),
        "current_mission_state": MissionState.IDLE.name,
        "current_policy": policy_for_comm(comm_state),
        "decision_reason": "communication-aware status update",
        "current_behaviour": "monitoring",
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