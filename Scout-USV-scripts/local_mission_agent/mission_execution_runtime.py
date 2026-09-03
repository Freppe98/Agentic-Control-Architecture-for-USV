"""
Process-local registry that lets the Local Agent's inbound HTTP server
(agent_server.py) reach the single live MissionExecutionController that main()'s
loop owns -- the same "shared module holds the live object" pattern
replan_runtime.py uses for the replanning controller and runtime_status.py uses
for the main-loop heartbeat.

The controller is registered once at startup, before the HTTP server starts, so
a /agent/mission_execution/* request always finds it.
"""
import threading

_lock = threading.Lock()
_controller = None


def register(controller) -> None:
    global _controller
    with _lock:
        _controller = controller


def get_controller():
    return _controller
