"""
Builds the GET /agent/mission payload agent_server.py serves -- the Local
Agent's half of the Pixhawk Mission card: a resilience/staleness wrapper
around the vehicle Flask service's real mission-download handshake
(motherpi/services/flask/services/mission_service.py), the same role
diagnostics.py plays for GET /agent/diagnostics.

Read-only: the only outbound call here is api_client.get_mission(), a GET.
Nothing in this module can reach a /nav/* write endpoint.

Adds one thing the Flask side can't: `last_fetch_age`, how long ago the
most recent *fully valid* mission download succeeded. The Flask side has no
persistent state (each request is a fresh live query), so if the vehicle
Flask API is briefly unreachable or a download times out mid-way, this
module falls back to the last confirmed-good mission rather than leaving
the operator card blank -- clearly marked via `error` and a growing
`last_fetch_age`, never silently presented as fresh.
"""
import time
from typing import Optional

from api_client import get_mission as _fetch_flask_mission

_last_good_result: Optional[dict] = None
_last_good_at: Optional[float] = None


def _unavailable(error: str) -> dict:
    return {
        "available": False,
        "reachable": False,
        "fetched_at": None,
        "mission_count": None,
        "current_waypoint": None,
        "home_position": None,
        "waypoints": [],
        "mission_loaded": None,
        "mission_valid": None,
        "error": error,
        "mission_hash": None,
        "mission_version": None,
        "schema_version": 1,
    }


def build_mission_status() -> dict:
    global _last_good_result, _last_good_at

    try:
        result = _fetch_flask_mission()
        fetch_error = None
    except Exception as e:
        result = None
        fetch_error = f"vehicle Flask API unreachable: {e}"

    now = time.time()

    if result is not None and result.get("available") and result.get("mission_valid"):
        _last_good_result = result
        _last_good_at = now

    if result is not None:
        payload = dict(result)
    elif _last_good_result is not None:
        payload = dict(_last_good_result)
        payload["error"] = f"{fetch_error}; showing last known mission"
    else:
        payload = _unavailable(fetch_error)

    payload["last_fetch_age"] = round(now - _last_good_at, 2) if _last_good_at is not None else None
    return payload
