"""
Bounded single-command background worker for MISSION_UPLOAD.

Why this exists: a mission upload is the one operator command whose vehicle-
side execution is genuinely slow -- MISSION_CLEAR_ALL + the full
MISSION_REQUEST_INT handshake + a complete fresh readback can take tens of
seconds (services/mission_upload_service.py). Every other command_type
executes as a single fast blocking HTTP call, so command_handler.process_command
runs them inline in local_agent.py's main loop. Running an upload inline the
same way would freeze the whole loop for that long: no status/telemetry to the
operator, no comm-state polling, no LOITER/RTL command could even be picked up
-- the vehicle would look dead for the duration of an upload.

This worker moves exactly that one command_type onto a background thread while
the main reporting loop keeps running normally. It is deliberately minimal:

  * Bounded to ONE upload at a time. try_start() returns "BUSY" if an upload
    is already running (the caller turns that into a terminal rejection);
    there is no queue and no second worker -- concurrent mission writes to one
    Pixhawk are exactly what must not happen.
  * The actual execution is still command_handler.process_command (injected as
    run_fn) -- the SAME validation/dedup/authority/normalization path every
    other command uses, just invoked off the main thread. There is no second
    command implementation here.
  * Lifecycle is observable without blocking: status() reports
    idle -> executing -> delivering -> idle, surfaced in the Local Agent's
    periodic status payload (agent.mission_upload) so the operator sees an
    upload in flight; the terminal executed/failed result is delivered as a
    normal command_result via finalize_fn when it completes.

Thread-safety of the shared command_log/command_results files is handled by
those modules' own locks (added when this worker was introduced).
"""
import threading
import time

from models import make_event

_lock = threading.Lock()
_active = None  # {"command_id","command_type","started_at","state"} or None


def status() -> dict:
    """Current worker state for the periodic status payload. Never blocks on
    the upload itself -- just a snapshot."""
    with _lock:
        if _active is None:
            return {
                "active": False, "state": "idle",
                "command_id": None, "command_type": None,
                "started_at": None, "elapsed_s": None,
            }
        return {
            "active": True,
            "state": _active["state"],
            "command_id": _active["command_id"],
            "command_type": _active["command_type"],
            "started_at": _active["started_at"],
            "elapsed_s": round(time.time() - _active["started_at"], 2),
        }


def is_busy() -> bool:
    with _lock:
        return _active is not None


def _fallback_failed(command, exc):
    """Terminal 'failed' result if run_fn itself raises. process_command
    already converts vehicle-call failures into a 'failed' result internally,
    so this only fires on a genuinely unexpected error in the worker thread --
    but a terminal result must always be produced, exactly as local_agent.py's
    own emergency handler guarantees for the synchronous path."""
    command_id = command.get("command_id")
    command_type = command.get("command_type")
    source = command.get("source") or command.get("requested_by") or "operator"
    now = time.time()
    reason = f"unexpected upload worker error: {exc}"
    payload = {
        "command_id": command_id,
        "usv_id": command.get("usv_id"),
        "command_type": command_type,
        "source": source,
        "status": "failed",
        "reason": reason,
        "timestamp": now,
        "lifecycle": [
            {"status": "requested", "timestamp": round(now, 3)},
            {"status": "failed", "timestamp": round(now, 3)},
        ],
    }
    event = make_event(
        "command_failed",
        message=f"Command {command_type or '?'} ({command_id}): failed -- {reason}",
        detail={"command_id": command_id, "command_type": command_type},
        severity="warning",
    )
    return payload, event


def try_start(command, run_fn, finalize_fn) -> str:
    """
    Attempt to start a bounded background upload.

    run_fn(command) -> (result_payload, event): executes the command
    (blocking, on the worker thread). This is command_handler.process_command
    bound with the current control authority.

    finalize_fn(result_payload): delivers the terminal result to the operator
    (post/buffer/clear), same as the synchronous path.

    Returns:
      "STARTED"    -- the worker took the command and is running it in the
                      background; the caller should NOT also process it.
      "IN_FLIGHT"  -- THIS EXACT command_id is the upload already running.
                      The operator backend uses at-least-once delivery and
                      redelivers a SENT command until a terminal result
                      arrives, so a long upload is redelivered repeatedly
                      while it is still legitimately in progress. That is not
                      a second upload and must not be treated as one: the
                      caller does nothing at all -- no second execution, no
                      BUSY rejection, no duplicate terminal result. The
                      original execution is still running and will deliver
                      the one terminal result when it finishes. (Returning
                      BUSY here, as this worker previously did, produced a
                      terminal rejection for a command that was actually
                      succeeding, and that rejection raced the real result.)
      "BUSY"       -- a DIFFERENT upload is already running; the caller must
                      produce a terminal rejection itself (only one upload at
                      a time -- concurrent mission writes to one Pixhawk are
                      exactly what must not happen).
    """
    global _active
    command_id = command.get("command_id")
    command_type = command.get("command_type")

    with _lock:
        if _active is not None:
            # Same-id redelivery of the in-flight upload vs. a genuinely
            # different concurrent upload -- see the docstring. Guarded on a
            # truthy command_id so two malformed commands both missing an id
            # can never be conflated into "the same" upload.
            if command_id is not None and _active["command_id"] == command_id:
                return "IN_FLIGHT"
            return "BUSY"
        _active = {
            "command_id": command_id,
            "command_type": command_type,
            "started_at": time.time(),
            "state": "executing",
        }

    def _worker():
        global _active
        try:
            try:
                result_payload, _event = run_fn(command)
            except Exception as e:  # defensive -- see _fallback_failed
                result_payload, _event = _fallback_failed(command, e)
            with _lock:
                if _active is not None:
                    _active["state"] = "delivering"
            try:
                finalize_fn(result_payload)
            except Exception as e:
                # finalize_fn already buffers on failure; this only guards a
                # truly unexpected error so the worker still frees its slot.
                print(f"[LOCAL AGENT] Upload worker finalize error ({command_id}): {e}")
        finally:
            with _lock:
                _active = None

    threading.Thread(target=_worker, name="mission-upload-worker", daemon=True).start()
    return "STARTED"


def _reset_for_tests() -> None:
    """Test-only: force the worker back to idle."""
    global _active
    with _lock:
        _active = None
