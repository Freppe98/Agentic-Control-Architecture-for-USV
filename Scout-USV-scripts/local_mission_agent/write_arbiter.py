"""
The single shared vehicle-write arbiter (task section 6 / 8).

Two independent controllers in this process can each initiate multi-step,
multi-second vehicle-write sequences (LOITER/AUTO/set_home/upload):

  * the mission-execution controller (mission_execution_controller.py) -- the
    original mission lifecycle: Start / Pause / Resume / final completion hold;
  * the energy replanning controller (replan_controller.py) -- the safe-return
    transaction.

They run on different threads (mission-execution operations on the inbound HTTP
server's request thread, a replan transaction on a daemon thread launched from
the main loop), so without a shared gate they could issue competing mode
commands at the same instant. This module is that gate: exactly ONE owner may
hold the write token at a time. It is intentionally tiny, process-local, and
NON-blocking -- a controller that cannot get the token does not queue behind the
other, it declines the operation and reports the current owner, so the operator
gets an immediate, explicit "busy" answer rather than a stalled request.

Contract:
  * acquire(owner) -> token (opaque str) if free, else None. Non-blocking.
  * release(token) -> True if `token` matched the live holder and was released.
    A stale/foreign token is ignored (returns False) so a late release from a
    previous owner can never free another owner's lock.
  * current_owner() -> the owner label currently holding the token, or None.

Owners must hold the token for the WHOLE write sequence and release it in a
finally-block. Passive monitoring (RUNNING/RETURNING_HOME observation, status
reads) performs no vehicle write and MUST NOT hold the token -- only the actual
write sequences do, so the boat is never blocked from a safety command by an
idle monitor.

This does not replace either controller's own one-action-at-a-time lock; it sits
above both, coordinating the two against each other. The replan controller keeps
its internal action lock unchanged (this arbiter is applied at its call site in
local_agent.py, so replan_controller.py itself is not modified).
"""
import threading
import uuid
from typing import Optional

# Owner labels (stable strings surfaced in status).
OWNER_MISSION_EXECUTION = "MISSION_EXECUTION"
OWNER_REPLANNING = "REPLANNING"

_gate = threading.Lock()          # the actual mutual-exclusion primitive
_state_lock = threading.Lock()    # guards _owner / _token
_owner: Optional[str] = None
_token: Optional[str] = None


def acquire(owner: str) -> Optional[str]:
    """Try to take the write token for `owner`. Returns an opaque token string
    on success, or None if another owner already holds it. Never blocks."""
    if not _gate.acquire(blocking=False):
        return None
    token = uuid.uuid4().hex
    with _state_lock:
        globals()["_owner"] = owner
        globals()["_token"] = token
    return token


def release(token: Optional[str]) -> bool:
    """Release the token iff `token` is the live holder. A foreign/stale token
    is ignored so a previous owner's late release cannot free a new owner."""
    with _state_lock:
        if token is None or token != _token:
            return False
        globals()["_owner"] = None
        globals()["_token"] = None
    _gate.release()
    return True


def current_owner() -> Optional[str]:
    with _state_lock:
        return _owner


def is_held() -> bool:
    with _state_lock:
        return _owner is not None


def _reset_for_tests() -> None:
    """Force-release any held token. Test-only -- never call in production."""
    global _owner, _token
    with _state_lock:
        held = _owner is not None
        _owner = None
        _token = None
    if held:
        try:
            _gate.release()
        except RuntimeError:
            pass
