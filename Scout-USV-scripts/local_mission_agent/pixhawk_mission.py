"""
Builds the GET /agent/pixhawk_mission payload agent_server.py serves -- the
schema the operator station's Pixhawk Mission card actually consumes
(mission_loaded/mission_valid/count/current_seq/hash/waypoints/partial).
The operator backend polls this as a proxy; this module (plus
motherpi/services/flask/services/mission_service.py's
download_pixhawk_mission(), which does the real MAVLink work) is "the real
Scout side" it proxies to.

Same resilience-wrapper role mission.py plays for the legacy GET
/agent/mission: the only outbound call here is api_client.get_pixhawk_mission(),
a GET, so this module can never reach a /nav/* write endpoint. Adds
`last_fetch_age` (seconds since the last *fully valid* mission download) and
falls back to the last confirmed-good mission if the vehicle Flask API is
briefly unreachable, rather than leaving the operator card blank -- always
clearly marked via a non-null `error`, never presented as fresh. A live (if
degraded) response from the vehicle Flask API is always surfaced as-is and
never masked by the cache; the cache only kicks in when that API couldn't be
reached at all.

Scout (the vehicle) remains the sole owner of mission state -- nothing here
or in mission_service.py ever uploads, modifies, deletes, or overwrites a
mission. Upload is explicitly out of scope for this endpoint.
"""
import time
from typing import Optional

from api_client import get_pixhawk_mission as _fetch_flask_pixhawk_mission
from api_client import get_pixhawk_mission_proof as _fetch_flask_pixhawk_mission_proof

_last_good_result: Optional[dict] = None
_last_good_at: Optional[float] = None


def _unavailable(error: str) -> dict:
    return {
        # mission-contract-v1 shape, mirroring the vehicle Flask service's
        # own unavailable payload (services/mission_service.py) so an
        # operator card reading route_waypoint_count/route_content_hash sees
        # the field present-and-null rather than absent when Scout's Flask
        # API is unreachable.
        "contract_version": "mission-contract-v1",
        "mission_loaded": None,
        "mission_valid": None,
        "count": None,
        "pixhawk_item_count": None,
        "route_waypoint_count": None,
        "current_seq": None,
        "hash": None,
        "full_mission_hash": None,
        "route_content_hash": None,
        "waypoints": [],
        "partial": None,
        "duplicate_sequences": [],
        "invalid_sequences": [],
        "unsupported_sequences": [],
        "error": error,
        "reachable": False,
        "fetched_at": None,
        "schema_version": 1,
    }


def build_pixhawk_mission_status() -> dict:
    global _last_good_result, _last_good_at

    try:
        result = _fetch_flask_pixhawk_mission()
        fetch_error = None
    except Exception as e:
        result = None
        fetch_error = f"vehicle Flask API unreachable: {e}"

    now = time.time()

    if result is not None and result.get("mission_valid"):
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


def build_pixhawk_mission_proof() -> dict:
    """
    A FRESH, proof-grade Pixhawk readback for SAFETY consumers (replan
    acceptance / route-consistency). Unlike build_pixhawk_mission_status(),
    which is a cache-tolerant DISPLAY read, this requests a coordinator refresh
    and waits for the refresh generation to advance (see
    api_client.get_pixhawk_mission_proof) and NEVER substitutes a last-known
    mission on failure -- it fails closed with a reachable=False payload the
    caller's freshness gate (planning_package.readback_is_fresh /
    verify_pixhawk_consistency) rejects. The proof never presents stale evidence
    as fresh.
    """
    try:
        return _fetch_flask_pixhawk_mission_proof()
    except Exception as e:
        return _unavailable(f"vehicle Flask API unreachable: {e}")
