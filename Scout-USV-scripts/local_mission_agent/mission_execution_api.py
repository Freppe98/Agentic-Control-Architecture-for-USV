"""
Operation layer for the Local Agent's mission-execution HTTP surface.

agent_server.py stays a thin request parser; every operation's validation and
result shaping lives here -- the same routes->services split the replanning
surface (replan_api.py) and the vehicle Flask side use. Every function returns
(http_status, body_dict).

These are DIRECT request/response operations against the Local Agent's own
inbound server (port 8090), deliberately separate from /agent/replan/* and NEVER
the deliver-once operator command queue. The Operator Backend proxies to this
surface exactly as it already proxies the replanning surface.

Routes served (see agent_server.py):
    GET  /agent/mission_execution/status
    POST /agent/mission_execution/start   {"mission_id": "..."}   (body optional)
    POST /agent/mission_execution/pause
    POST /agent/mission_execution/resume
    POST /agent/mission_execution/rearm
    POST /agent/mission_execution/stop
    POST /agent/mission_execution/reprove_binding   {"mission_id": "..."}   (body optional)

Idempotency (task section 9): a Start while already RUNNING reports the current
state instead of starting twice; a Pause while already PAUSED succeeds
idempotently; a Resume while already RUNNING reports already running. These are
handled inside the controller and surfaced here unchanged.

reprove_binding is READ-ONLY (Operator "Full Refresh"): it re-proves mission-
execution binding evidence against the CURRENT stored planning package and a
fresh Pixhawk mission readback -- the same identity proof Start itself uses --
and restores verified_route_hash / start_eligible / start_block_reason without
ever uploading, clearing, or writing a mission, touching Home, changing mode,
or arming/disarming. See MissionExecutionController.reprove_binding()'s
docstring for the full contract.
"""
from typing import Any, Tuple

import mission_execution_runtime


def _no_controller() -> Tuple[int, dict]:
    return 503, {"accepted": False,
                 "error": {"code": "CONTROLLER_NOT_READY",
                           "message": "mission-execution controller not initialised yet"}}


def _controller():
    return mission_execution_runtime.get_controller()


def get_status() -> Tuple[int, dict]:
    ctrl = _controller()
    if ctrl is None:
        # Status is always answerable even before the controller exists, but
        # advertise unsupported rather than 503 so the Operator Station can
        # render a stable "not ready" surface.
        return 200, {"supported": False, "state": None,
                     "error": {"code": "CONTROLLER_NOT_READY",
                               "message": "mission-execution controller not initialised yet"}}
    return 200, ctrl.status()


def start(body: Any = None) -> Tuple[int, dict]:
    ctrl = _controller()
    if ctrl is None:
        return _no_controller()
    mission_id = None
    if isinstance(body, dict):
        mission_id = body.get("mission_id")
    result = ctrl.start(mission_id)
    return _http_for(result), result


def pause(body: Any = None) -> Tuple[int, dict]:
    ctrl = _controller()
    if ctrl is None:
        return _no_controller()
    result = ctrl.pause()
    return _http_for(result), result


def resume(body: Any = None) -> Tuple[int, dict]:
    ctrl = _controller()
    if ctrl is None:
        return _no_controller()
    result = ctrl.resume()
    return _http_for(result), result


def rearm(body: Any = None) -> Tuple[int, dict]:
    ctrl = _controller()
    if ctrl is None:
        return _no_controller()
    result = ctrl.rearm()
    return _http_for(result), result


def reprove_binding(body: Any = None) -> Tuple[int, dict]:
    """Read-only on-demand re-proof of mission-execution binding evidence
    (Operator "Full Refresh"). See MissionExecutionController.reprove_binding()
    for the full contract; NEVER uploads/clears/writes a mission or touches the
    vehicle. `mission_id` in the body, if present, is an OPERATOR-SIDE
    EXPECTATION/constraint only -- never proof (task section 13)."""
    ctrl = _controller()
    if ctrl is None:
        return _no_controller()
    expected_mission_id = None
    if isinstance(body, dict):
        expected_mission_id = body.get("mission_id")
    result = ctrl.reprove_binding(expected_mission_id)
    return _http_for(result), result


def stop(body: Any = None) -> Tuple[int, dict]:
    """Operator-requested Stop: safe abort + reset-to-start. A rejected request
    (BUSY / precondition / arbitration conflict) is a 409; an accepted Stop --
    including one that then fails closed on the vehicle -- is a 200 with the
    structured outcome/stop-evidence/error in the body, matching Start/Pause/
    Resume/Rearm."""
    ctrl = _controller()
    if ctrl is None:
        return _no_controller()
    result = ctrl.stop()
    return _http_for(result), result


def _http_for(result: dict) -> int:
    """Map a controller result to an HTTP status. A rejected request (a
    precondition/arbitration refusal) is a 409 Conflict; everything the
    controller accepted -- including an operation that then failed on the
    vehicle -- is a 200 with the structured outcome/error in the body, matching
    the /agent/set_home and /agent/upload_mission convention where the caller
    inspects the body, not the HTTP status."""
    if not isinstance(result, dict):
        return 200
    if result.get("accepted") is False:
        return 409
    return 200
