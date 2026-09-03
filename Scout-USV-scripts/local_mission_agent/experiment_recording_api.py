"""
Operation layer for the Local Agent's experiment-recording HTTP surface.

agent_server.py stays a thin request parser; every operation's validation and
result shaping lives here -- the same routes->services split
mission_execution_api.py / replan_api.py use. Every function returns
(http_status, body_dict).

Routes served (see agent_server.py):
    GET   /agent/experiment_recording/config
    PATCH /agent/experiment_recording/config
    GET   /agent/experiment_recording/status
    GET   /agent/experiment_recording/runs
    GET   /agent/experiment_recording/runs/{run_id}
    POST  /agent/experiment_recording/annotation

This surface is DESCRIPTIVE / OBSERVATIONAL only (task section 4): PATCH
config sets metadata for the NEXT run and never touches vehicle or impairment
state; POST annotation only enqueues a note. Nothing here can start/stop a
mission, change authority, or affect replanning -- see experiment_recorder.py's
own contract (recorder failures degrade recording, never mission execution).
"""
from typing import Any, Tuple

import experiment_recording_runtime


def _no_recorder() -> Tuple[int, dict]:
    return 200, {"enabled": False, "active": False,
                 "error": {"code": "RECORDER_NOT_READY",
                           "message": "experiment recorder not initialised yet"}}


def _recorder():
    return experiment_recording_runtime.get_recorder()


def get_config(body: Any = None) -> Tuple[int, dict]:
    rec = _recorder()
    if rec is None:
        return _no_recorder()
    return 200, {"next_run": rec.get_next_run_config()}


def patch_config(body: Any) -> Tuple[int, dict]:
    rec = _recorder()
    if rec is None:
        return _no_recorder()
    if not isinstance(body, dict):
        return 400, {"accepted": False,
                     "error": {"code": "INVALID_REQUEST", "message": "request body must be a JSON object"}}
    updated = rec.configure_next_run(body)
    return 200, {"accepted": True, "next_run": updated}


def get_status() -> Tuple[int, dict]:
    rec = _recorder()
    if rec is None:
        return _no_recorder()
    return 200, rec.status()


def get_runs() -> Tuple[int, dict]:
    rec = _recorder()
    if rec is None:
        return _no_recorder()
    return 200, {"runs": rec.list_runs()}


def get_run(run_id: str) -> Tuple[int, dict]:
    rec = _recorder()
    if rec is None:
        return _no_recorder()
    run = rec.get_run(run_id)
    if run is None:
        return 404, {"error": {"code": "RUN_NOT_FOUND", "message": f"no run {run_id!r}"}}
    return 200, run


def post_annotation(body: Any) -> Tuple[int, dict]:
    rec = _recorder()
    if rec is None:
        return _no_recorder()
    if not isinstance(body, dict) or not body.get("category") or not body.get("name"):
        return 400, {"accepted": False,
                     "error": {"code": "INVALID_REQUEST",
                               "message": "category and name are required"}}
    rec.record_annotation(
        category=body["category"], name=body["name"],
        data=body.get("data"), source=body.get("source") or "operator_annotation",
    )
    # Fire-and-forget by design (task section 27) -- always reports accepted;
    # the recorder's own status/degraded surface is where delivery is verified.
    return 200, {"accepted": True}
