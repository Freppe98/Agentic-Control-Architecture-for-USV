"""
Local-Agent-side route content hash, byte-for-byte compatible with the vehicle
Flask service's mission-contract-v1 (services/mission_contract.py's
route_content_hash).

The two are separate processes -- the Local Agent must never import the Flask
service package (same reason command_executor.py keeps its own copy of the
ArduRover mode table). This module is therefore a deliberate, pinned copy of
the mission-contract-v1 route canonicalization, so the revision metadata Scout
records for a safe-return mission carries the SAME route_content_hash the Flask
upload/readback will compute for it. test_planning_package.py pins this against
the published golden constant so the two implementations cannot silently drift.

Canonicalization (identical to mission-contract-v1, route items 1..N, Home
excluded):
  per point: sequence (1..N), command MAV_CMD_NAV_WAYPOINT, frame
  MAV_FRAME_GLOBAL_RELATIVE_ALT, latitude round(lat,7), longitude round(lon,7),
  altitude 0.0, param1 round(loiter,3), param2/3/4 0.0; sorted by sequence;
  json.dumps(sort_keys=True, separators=(",", ":")); sha256 hex; "sha256:" prefix.
"""
import hashlib
import json
from typing import List, Optional

ROUTE_COMMAND = "MAV_CMD_NAV_WAYPOINT"
ROUTE_FRAME = "MAV_FRAME_GLOBAL_RELATIVE_ALT"
ROUTE_ALTITUDE = 0.0
HASH_PREFIX = "sha256:"


def _canonical_items(route: List[dict]) -> List[dict]:
    items = []
    for i, wp in enumerate(route):
        items.append({
            "sequence": i + 1,
            "command": ROUTE_COMMAND,
            "frame": ROUTE_FRAME,
            "latitude": round(float(wp["latitude"]), 7),
            "longitude": round(float(wp["longitude"]), 7),
            "altitude": ROUTE_ALTITUDE,
            "param1": round(float(wp.get("loiter_time_s", 0) or 0), 3),
            "param2": 0.0,
            "param3": 0.0,
            "param4": 0.0,
        })
    return items


def _canonical_json(items: List[dict]) -> str:
    ordered = sorted(items, key=lambda wp: wp["sequence"])
    return json.dumps(ordered, sort_keys=True, separators=(",", ":"))


def route_content_hash(route: List[dict]) -> Optional[str]:
    """"sha256:..." over the canonical route (route items 1..N, Home excluded).
    None for an empty route -- there is no route content to hash."""
    if not route:
        return None
    digest = hashlib.sha256(_canonical_json(_canonical_items(route)).encode("utf-8")).hexdigest()
    return f"{HASH_PREFIX}{digest}"
