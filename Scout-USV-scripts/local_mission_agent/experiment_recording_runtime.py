"""
Process-local registry that lets the Local Agent's inbound HTTP server
(agent_server.py) reach the single live ExperimentRecorder that main()'s loop
owns -- the same "shared module holds the live object" pattern
mission_execution_runtime.py uses for the mission-execution controller and
replan_runtime.py uses for the replanning controller.

Registered once at startup, before the HTTP server starts, so a
/agent/experiment_recording/* request always finds it.
"""
import threading

_lock = threading.Lock()
_recorder = None


def register(recorder) -> None:
    global _recorder
    with _lock:
        _recorder = recorder


def get_recorder():
    return _recorder
