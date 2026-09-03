"""
The single authoritative persistent record of the most recent MISSION_UPLOAD
or MISSION_CLEAR -- what state it is in, what was expected, what was observed,
and how it ended.

Why this exists separately from mission_upload_worker.status()
-------------------------------------------------------------
The worker's status() is a LIVE block: in-memory, tiny, and gone the instant
the worker frees its slot. That is exactly right for what it does -- telling
the operator "an upload is happening right now" in every periodic status
payload, cheaply, without blocking on the upload itself. It is kept, unchanged.

But it answers only "is something running". It cannot answer "how did the last
one end", because the moment an upload finishes the block reverts to idle. An
operator whose link dropped during an upload reconnects to an idle worker and
no way to find out what happened, which is the one question that actually
matters after a connectivity interruption.

So the two coexist deliberately:

  mission_upload_worker.status()  -- live, ephemeral, "is it running now"
  this module                     -- authoritative, persistent, "what happened"

Persistence is the point, not an implementation detail. This record survives
both the operation completing AND the Local Agent process restarting, so the
terminal details stay fetchable long after the operation itself is over. It is
retained until the NEXT mission operation begins rather than expiring on a
timer -- this process cannot know when the operator will manage to reconnect,
so a fixed TTL would be a guess that silently loses the answer precisely in the
long-outage case the record exists for.

States
------
    IDLE               no mission operation has run in this record's lifetime
    ACCEPTED           validated and admitted; nothing sent to the vehicle yet
    EXECUTING          the MAVLink transaction is in progress
    VERIFYING          the transaction finished; the fresh readback is running
    DELIVERING_RESULT  legacy/defensive value only -- see "Local outcome vs.
                        result delivery" below. Nothing in this codebase writes
                        it as `state` anymore.
    COMPLETED          terminal, succeeded
    FAILED             terminal, did not succeed (see `error`)

COMPLETED and FAILED are the only terminal states. Everything else means an
operation was in flight when the record was last written -- which, after a
process restart, is the signal that a MAVLink transaction was interrupted with
an unknown vehicle-side outcome. See recover_after_restart().

Local outcome vs. result delivery
----------------------------------
`state` answers exactly one question: did the upload/clear itself succeed on
the vehicle, as proven by a fresh readback? Once finish() writes COMPLETED or
FAILED, that answer is final -- it is the physically-verified truth and
nothing about the operator link can change it. Earlier this module also used a
DELIVERING_RESULT `state` value to mean "the operator hasn't been told yet",
written by the caller *after* finish() already ran. That conflated two
different failure domains: a vehicle-proven-successful upload sat permanently
non-terminal (GET /agent/mission_operation never reported COMPLETED) for as
long as the operator happened to be unreachable, even though nothing was
actually still uncertain -- only the notification was pending. A live process
that never restarts had no path back out of it at all, since only
recover_after_restart() ever resolved DELIVERING_RESULT to a terminal state.

Whether the operator has been told is now tracked in the separate `delivery`
sub-record (DELIVERY_PENDING -> DELIVERY_IN_PROGRESS -> DELIVERY_ACKNOWLEDGED,
via mark_delivery_attempt/mark_delivery_acknowledged/mark_delivery_failed),
created by finish() alongside the terminal `state` but updated independently
of it from then on. A stuck or repeatedly-failing delivery leaves `delivery`
at PENDING/DELIVERING for retry -- exactly as long as it takes -- without ever
moving `state` off the already-proven COMPLETED/FAILED outcome. The
authoritative resend content itself still lives in command_results.py, keyed
by command_id, as before; `delivery` here is observability over that resend,
not a second copy of it.

Boundedness
-----------
This is a status record, not a log: exactly ONE operation is stored (the most
recent). The diagnostics carried alongside it are the bounded structures the
Flask services build (mission_upload_service._new_diagnostics), never raw
MAVLink. The operator polls this, so it must stay small.
"""
import json
import os
import threading
import time

import config

_lock = threading.Lock()

# Terminal states -- the operation's outcome is known and recorded.
TERMINAL_STATES = ("COMPLETED", "FAILED")

# The states in which the MAVLink transaction's outcome is genuinely UNKNOWN if
# the process stops. These -- and only these -- become UNKNOWN_AFTER_RESTART.
#
# DELIVERING_RESULT is deliberately NOT in this set, kept as a defensive
# no-op branch in recover_after_restart() for any record written by an older
# build. By the time that state was written the operation had already
# finished and its outcome had already been persisted (both here and,
# authoritatively per command_id, in command_results.py). A crash during
# delivery loses nothing: the operator redelivers the command_id and
# command_results resends the stored terminal result, with no MAVLink
# operation repeated. Treating it as unknown would report a completed,
# verified upload as indeterminate and demand a pointless re-upload.
INTERRUPTIBLE_STATES = ("ACCEPTED", "EXECUTING", "VERIFYING")

STATE_IDLE = "IDLE"
STATE_ACCEPTED = "ACCEPTED"
STATE_EXECUTING = "EXECUTING"
STATE_VERIFYING = "VERIFYING"
STATE_DELIVERING_RESULT = "DELIVERING_RESULT"  # legacy value -- see module docstring
STATE_COMPLETED = "COMPLETED"
STATE_FAILED = "FAILED"

# `delivery` sub-record states -- whether the operator has been notified of an
# already-terminal `state`. Independent of `state`; see module docstring.
DELIVERY_PENDING = "PENDING"
DELIVERY_IN_PROGRESS = "DELIVERING"
DELIVERY_ACKNOWLEDGED = "ACKNOWLEDGED"

_STATE_FILE = getattr(
    config, "MISSION_OPERATION_STATE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mission_operation_state.json"),
)


def _now() -> float:
    return time.time()


def _empty_record() -> dict:
    """The IDLE record. Every field the contract names is present-and-null
    rather than absent, so an operator card can read a stable shape whether or
    not an operation has ever run."""
    return {
        "command_id": None,
        "command_type": None,
        "state": STATE_IDLE,
        "started_at": None,
        "updated_at": None,
        "elapsed_s": 0,
        "expected_route_waypoint_count": 0,
        "expected_pixhawk_item_count": 0,
        "expected_route_content_hash": None,
        "observed_route_waypoint_count": 0,
        "observed_pixhawk_item_count": 0,
        "observed_route_content_hash": None,
        "acknowledgement": None,
        "empty_representation": None,
        "error": None,
        "diagnostics": None,
        # None until finish() produces a terminal result to deliver -- see
        # "Local outcome vs. result delivery" above.
        "delivery": None,
    }


def _empty_delivery() -> dict:
    return {
        "status": DELIVERY_PENDING,
        "attempts": 0,
        "last_attempt_at": None,
        "last_error": None,
        "acknowledged_at": None,
    }


def _read() -> dict:
    try:
        with open(_STATE_FILE, "r") as f:
            record = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return _empty_record()
    if not isinstance(record, dict):
        return _empty_record()
    # Merge onto the empty shape so a record written by an older build that
    # lacked a field still reads back with every field present.
    merged = _empty_record()
    merged.update(record)
    return merged


def _write(record: dict) -> None:
    # Written via a temp file + atomic replace: a status record truncated by a
    # power cut mid-write would be unparseable, and this module's whole purpose
    # is surviving exactly that kind of interruption.
    tmp = f"{_STATE_FILE}.tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, _STATE_FILE)
    except OSError as e:
        print(f"[MISSION_OP] could not persist mission operation state: {e}")


def _stamp(record: dict) -> dict:
    now = _now()
    record["updated_at"] = now
    started = record.get("started_at")
    record["elapsed_s"] = round(now - started, 2) if started else 0
    return record


def get() -> dict:
    """The current authoritative record. Always a full record, never None."""
    with _lock:
        record = _read()
        # elapsed_s is recomputed on read for a still-running operation so a
        # polling operator sees it advance without this module needing a timer.
        if record.get("state") not in TERMINAL_STATES and record.get("started_at"):
            record["elapsed_s"] = round(_now() - record["started_at"], 2)
        return record


def begin(command_id, command_type, expected_route_waypoint_count=None,
          expected_pixhawk_item_count=None, expected_route_content_hash=None) -> dict:
    """
    Start a new operation record in ACCEPTED, REPLACING whatever was there.

    Replacing is deliberate: this is a "most recent operation" record, and the
    previous operation's details have necessarily already been delivered (or
    buffered for delivery) as a command_result by the time a new operation is
    admitted. Keeping a history here would duplicate command_history.py.
    """
    with _lock:
        record = _empty_record()
        record.update({
            "command_id": command_id,
            "command_type": command_type,
            "state": STATE_ACCEPTED,
            "started_at": _now(),
            "expected_route_waypoint_count": expected_route_waypoint_count,
            "expected_pixhawk_item_count": expected_pixhawk_item_count,
            "expected_route_content_hash": expected_route_content_hash,
        })
        _stamp(record)
        _write(record)
        return record


def set_state(state, command_id=None) -> dict:
    """
    Advance the current operation to `state`.

    A no-op if `command_id` is given and does not match the record's -- a late
    write from a superseded operation must never overwrite the current one's
    state, which is the failure mode a shared single-slot record invites.
    """
    with _lock:
        record = _read()
        if command_id is not None and record.get("command_id") != command_id:
            return record
        record["state"] = state
        _stamp(record)
        _write(record)
        return record


def finish(command_id, succeeded, observed_route_waypoint_count=None,
           observed_pixhawk_item_count=None, observed_route_content_hash=None,
           acknowledgement=None, empty_representation=None, error=None,
           diagnostics=None) -> dict:
    """
    Record the terminal outcome. This is what a reconnecting operator reads.

    Written BEFORE the result is delivered to the operator, so the terminal
    outcome is durable even if delivery fails or the process dies during it --
    which is the whole at-least-once guarantee: the vehicle must never be in a
    position where it performed an operation whose outcome it cannot report.

    `state` becomes COMPLETED/FAILED here and stays there -- this is the last
    write this function makes to `state`. A fresh `delivery` sub-record is
    opened in PENDING alongside it (see mark_delivery_attempt/
    mark_delivery_acknowledged/mark_delivery_failed): whether the operator has
    been told is tracked there from now on, independently of this already-
    final `state`.
    """
    with _lock:
        record = _read()
        if record.get("command_id") != command_id:
            # Terminal result for an operation this record no longer tracks.
            # Do not clobber a newer operation's record with an older one's
            # outcome; the authoritative per-command_id result still lives in
            # command_results.py, which is what redelivery actually resends.
            return record
        record.update({
            "state": STATE_COMPLETED if succeeded else STATE_FAILED,
            "observed_route_waypoint_count": observed_route_waypoint_count,
            "observed_pixhawk_item_count": observed_pixhawk_item_count,
            "observed_route_content_hash": observed_route_content_hash,
            "acknowledgement": acknowledgement,
            "empty_representation": empty_representation,
            "error": error,
            "diagnostics": diagnostics,
            "delivery": _empty_delivery(),
        })
        _stamp(record)
        _write(record)
        return record


def _update_delivery(command_id, mutate) -> dict:
    """Shared guard for the mark_delivery_* setters: a no-op, both for the
    `delivery` sub-record and for `state`, if `command_id` does not match the
    record currently tracked -- a late delivery attempt/ack for a superseded
    operation must never touch the current one's record, same reasoning as
    set_state()'s command_id guard. `mutate(delivery_dict)` edits in place."""
    with _lock:
        record = _read()
        if command_id is not None and record.get("command_id") != command_id:
            return record
        delivery = record.get("delivery") or _empty_delivery()
        mutate(delivery)
        record["delivery"] = delivery
        _stamp(record)
        _write(record)
        return record


def mark_delivery_attempt(command_id) -> dict:
    """An attempt to hand this operation's stored terminal result to the
    operator is starting (POST /agent/command_result, live or a buffered
    replay). Does not affect `state`."""
    def _mutate(delivery):
        delivery["status"] = DELIVERY_IN_PROGRESS
        delivery["attempts"] = (delivery.get("attempts") or 0) + 1
        delivery["last_attempt_at"] = _now()
    return _update_delivery(command_id, _mutate)


def mark_delivery_acknowledged(command_id) -> dict:
    """The operator has acknowledged this operation's result (2xx response).
    Does not affect `state` -- it was already terminal before this delivery
    was even attempted."""
    def _mutate(delivery):
        delivery["status"] = DELIVERY_ACKNOWLEDGED
        delivery["acknowledged_at"] = _now()
        delivery["last_error"] = None
    return _update_delivery(command_id, _mutate)


def mark_delivery_failed(command_id, error=None) -> dict:
    """A delivery attempt failed (network error, non-2xx, buffered for
    retry). Goes back to PENDING, not a dead end -- delivery may retry
    indefinitely; only `state`, already terminal, is authoritative about the
    upload itself."""
    def _mutate(delivery):
        delivery["status"] = DELIVERY_PENDING
        delivery["last_error"] = str(error) if error is not None else None
    return _update_delivery(command_id, _mutate)


def recover_after_restart() -> dict:
    """
    Called once at Local Agent startup, BEFORE any command is processed.

    If the persisted record is in a non-terminal state, a mission operation was
    in flight when this process last stopped. The MAVLink transaction behind it
    was interrupted at an unknown point: the vehicle may hold nothing, a
    complete mission, or a partially transferred one, and there is no way from
    here to tell which.

    That is converted into a TERMINAL FAILED state with a structured
    UNKNOWN_AFTER_RESTART error, and deliberately NOT resumed. Resuming would
    mean continuing a MISSION_ITEM_INT exchange from a sequence number this
    process never observed, against a vehicle-side transaction state that
    cannot be proven -- writing waypoints blind into a mission of unknown
    content. A fresh operator-initiated retry starts from MISSION_CLEAR_ALL and
    a known-empty vehicle, which is the only starting state that can actually
    be verified.

    Returns the (possibly updated) record.
    """
    with _lock:
        record = _read()
        state = record.get("state")

        if state == STATE_DELIVERING_RESULT:
            # Legacy value -- see module docstring. Nothing currently writes
            # this as `state`, but a record persisted by an older build could
            # still have it: the outcome was determined and persisted before
            # delivery began, and command_results.py still holds the
            # authoritative result to resend, so restore the terminal state
            # the recorded outcome implies rather than inventing an unknown
            # one. See INTERRUPTIBLE_STATES.
            record["state"] = STATE_FAILED if record.get("error") else STATE_COMPLETED
            _stamp(record)
            _write(record)
            state = record["state"]

        # An in-flight delivery attempt (mark_delivery_attempt was called, no
        # ack or failure recorded yet) is interrupted by the same restart, not
        # completed by it -- reset it to PENDING so it is retried rather than
        # left showing DELIVERING forever with nothing left running to move it.
        # This never touches `state`: the local outcome it reports is either
        # already terminal (nothing to do) or was just resolved above.
        delivery = record.get("delivery")
        if isinstance(delivery, dict) and delivery.get("status") == DELIVERY_IN_PROGRESS:
            delivery["status"] = DELIVERY_PENDING
            record["delivery"] = delivery
            _stamp(record)
            _write(record)

        if state not in INTERRUPTIBLE_STATES:
            return record

        command_type = record.get("command_type") or "mission operation"
        record["state"] = STATE_FAILED
        record["error"] = {
            "code": "UNKNOWN_AFTER_RESTART",
            "message": (
                f"the Local Agent restarted while a {command_type} was in state "
                f"{state}; the MAVLink transaction was interrupted at an unknown "
                "point and the mission now on the vehicle cannot be determined "
                "from here. It was NOT resumed -- a partially transferred "
                "mission cannot be continued from an unproven sequence. Issue a "
                "fresh mission command (a new command_id) to re-establish a "
                "known state."
            ),
            "interrupted_state": state,
            "requires_fresh_retry": True,
        }
        _stamp(record)
        _write(record)
        print(f"[MISSION_OP] recovered interrupted {command_type} "
              f"({record.get('command_id')}) in state {state} -> FAILED "
              "UNKNOWN_AFTER_RESTART (not resumed)")
        return record


def interrupted_command_id() -> object:
    """The command_id of an operation that was in flight at the last restart
    and has been failed with UNKNOWN_AFTER_RESTART, else None.

    Lets the command path answer a redelivery of that exact id with the real
    structured reason instead of a bare "duplicate, already processed" -- the
    operator needs to know the vehicle state is unknown, not merely that the
    id was seen before.
    """
    record = get()
    error = record.get("error") or {}
    if error.get("code") == "UNKNOWN_AFTER_RESTART":
        return record.get("command_id")
    return None


def _reset_for_tests(path=None) -> None:
    """Test-only: point at a scratch file and clear it."""
    global _STATE_FILE
    with _lock:
        if path is not None:
            _STATE_FILE = path
        try:
            os.remove(_STATE_FILE)
        except OSError:
            pass
