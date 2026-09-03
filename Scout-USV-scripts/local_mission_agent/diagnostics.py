"""
Local Agent's half of the Vehicle Health page: combines checks this process
can answer directly (operator link, its own liveness, internet/VPN) with the
vehicle Flask API's half (mavlink2rest/Pixhawk/GPS/battery/RC/camera/
mission/cpu/memory/storage -- services/diagnostics_service.py on the Flask
side, fetched here over HTTP via api_client.get_diagnostics()).

Everything here is read-only: GETs against the vehicle Flask API and the
operator, plus local pings/loadavg reads. Nothing calls a /nav/* write
endpoint or touches control authority.

build_diagnostics() -> GET /agent/diagnostics shape (per-component
OK/WARNING/FAIL/UNKNOWN, each with a measured_at timestamp).
build_system_check() -> POST /agent/system_check shape (PASS/WARN/FAIL/
UNKNOWN checklist + overall + started_at/finished_at/duration_seconds),
reusing build_diagnostics() rather than re-deriving the same signals twice.
"""
import time

import communication
import runtime_status
import process_health
import transition_log
from api_client import get_diagnostics as _fetch_flask_diagnostics
from api_client import get_vehicle_state, get_control_authority

# Components the vehicle Flask API answers (services/diagnostics_service.py
# there); copied through verbatim into this endpoint's response.
_FLASK_KEYS = [
    "mavlink", "pixhawk", "gps", "battery", "rc_receiver",
    "camera", "mission_service", "storage", "cpu", "memory",
]

_STATUS_TO_CHECK = {"OK": "PASS", "WARNING": "WARN", "FAIL": "FAIL", "UNKNOWN": "UNKNOWN"}


def _status(status: str, message: str = None, evidence: dict = None) -> dict:
    """
    `evidence` carries the raw measured values behind the status/message
    verdict (e.g. {"alive": True, "cpu_percent": 4.2}) so the operator
    backend can derive its own PASS/WARN/FAIL directly instead of only ever
    seeing this module's own thresholding -- see README "Diagnostic evidence".
    """
    out = {"status": status, "measured_at": round(time.time(), 2)}
    if message:
        out["message"] = message
    if evidence:
        out.update(evidence)
    return out


def _diag_communication(comm_state: str) -> dict:
    if comm_state == "CONNECTED":
        return _status("OK", "operator reachable")
    if comm_state == "PARTITIONED":
        return _status("WARNING", "operator unreachable, VPN link active")
    return _status("FAIL", "operator and VPN unreachable")


def _diag_local_agent() -> dict:
    age = runtime_status.seconds_since_alive()
    cpu_percent = process_health.cpu_percent()
    if age is None:
        return _status("UNKNOWN", "no main loop iteration recorded yet",
                        evidence={"alive": False, "cpu_percent": cpu_percent})
    evidence = {"alive": True, "cpu_percent": cpu_percent}
    if age < 10:
        return _status("OK", f"main loop iterated {round(age, 1)}s ago", evidence=evidence)
    if age < 30:
        return _status("WARNING", f"main loop stalled, last iteration {round(age, 1)}s ago", evidence=evidence)
    return _status("FAIL", f"main loop stalled, last iteration {round(age, 1)}s ago",
                    evidence={"alive": False, "cpu_percent": cpu_percent})


def _diag_network() -> dict:
    """
    Known limitation: communication.vpn_ok() shells out to `sudo -n wg
    show` and returns False both when there's genuinely no handshake *and*
    when the command itself fails (e.g. passwordless sudo not configured on
    this Pi) -- it only warns once via print in that second case rather
    than surfacing it here as UNKNOWN. See communication.py's vpn_ok()
    docstring/comments. Not re-derived here to avoid duplicating that
    module's logic; a stale FAIL on this field with no matching print
    warning in the Local Agent's log is a signal, not a bug.
    """
    if communication.internet_ok():
        return _status("OK", "internet reachable")
    if communication.vpn_ok():
        return _status("WARNING", "internet unreachable, VPN handshake active")
    return _status("FAIL", "internet and VPN unreachable")


def _diag_authority() -> dict:
    """
    Read-only report of the current control authority (OPERATOR/LOCAL_AGENT)
    -- not a health verdict, just an observation, so UNKNOWN (not FAIL) on
    failure to reach the vehicle Flask API, consistent with the rest of this
    function's failure mode. build_system_check()'s own Authority Service
    check is stricter (FAIL on unreachable) since it's a pre-deployment gate,
    not an always-on status field -- see _check_authority_service().
    """
    try:
        authority = get_control_authority()
        return _status("OK", f"authority={authority}")
    except Exception as e:
        return _status("UNKNOWN", f"vehicle Flask API unreachable: {e}")


def build_diagnostics() -> dict:
    comm_state = communication.get_comm_state()

    result = {
        "generated_at": round(time.time(), 2),
        "communication": _diag_communication(comm_state),
        "local_agent": _diag_local_agent(),
        "network": _diag_network(),
        "authority": _diag_authority(),
    }

    try:
        flask_diag = _fetch_flask_diagnostics()
        for key in _FLASK_KEYS:
            result[key] = flask_diag.get(key) or _status("UNKNOWN", "not reported by vehicle Flask API")
    except Exception as e:
        for key in _FLASK_KEYS:
            result[key] = _status("UNKNOWN", f"vehicle Flask API unreachable: {e}")

    return result


def _check(name: str, status: str, message: str = None) -> dict:
    out = {"name": name, "status": status}
    if message:
        out["message"] = message
    return out


def _check_from_diag(name: str, component: dict) -> dict:
    return _check(name, _STATUS_TO_CHECK[component["status"]], component.get("message"))


def _check_telemetry() -> dict:
    try:
        telemetry = get_vehicle_state().get("telemetry", {})
        if not telemetry or "error" in telemetry:
            return _check("Telemetry", "FAIL", telemetry.get("error", "no telemetry reported"))
        lat, lng = telemetry.get("lat"), telemetry.get("lng")
        if lat is None or lng is None:
            return _check("Telemetry", "FAIL", "no GPS position reported")
        return _check("Telemetry", "PASS", f"lat={lat} lng={lng}")
    except Exception as e:
        return _check("Telemetry", "UNKNOWN", f"vehicle Flask API unreachable: {e}")


def _check_authority_service() -> dict:
    try:
        authority = get_control_authority()
        return _check("Authority Service", "PASS", f"responding, authority={authority}")
    except Exception as e:
        return _check("Authority Service", "FAIL", f"unreachable: {e}")


# Worst-first: a single FAIL always wins; UNKNOWN outranks WARN outranks
# PASS on the theory that "we couldn't check it" is not the same as "it's
# fine" for a pre-deployment gate -- an unresolved UNKNOWN should still
# make a human look before dropping the vehicle in the water.
_SEVERITY = {"FAIL": 3, "WARN": 2, "UNKNOWN": 1, "PASS": 0}
_OVERALL_FOR_RANK = {3: "FAIL", 2: "WARN", 1: "WARN", 0: "PASS"}


def build_system_check() -> dict:
    """
    Lightweight pre-deployment readiness check: read-only, no mode changes,
    no arming, no RC interaction, no MAVLink writes. See README "System
    check" for exactly what each check does and does not verify.
    """
    started_at = round(time.time(), 2)
    diag = build_diagnostics()

    checks = [
        _check_from_diag("MAVLink2Rest Reachability", diag["mavlink"]),
        _check_from_diag("Pixhawk Heartbeat", diag["pixhawk"]),
        _check_from_diag("Local Agent", diag["local_agent"]),
        _check_telemetry(),
        _check_from_diag("GPS", diag["gps"]),
        _check_authority_service(),
    ]

    overall_rank = max(_SEVERITY[c["status"]] for c in checks)
    finished_at = round(time.time(), 2)

    return {
        "overall": _OVERALL_FOR_RANK[overall_rank],
        "checks": checks,
        "generated_at": diag["generated_at"],
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": round(finished_at - started_at, 3),
    }
