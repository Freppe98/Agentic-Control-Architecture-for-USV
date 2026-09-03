"""
Orchestrates one validate/execute/ack cycle for a single operator command.

Validation order: malformed, then duplicate, then timestamp normalization
+ expiry, then supported type, then control authority, then (for
SET_MODE_AUTO/RTL/RETURN_HOME/MISSION_RESUME only) Home verification.
Duplicate is checked before everything past "malformed" -- including
expiry -- so a retried command_id short-circuits straight to resending
its original stored terminal result (see command_results.py) rather than
re-litigating whatever it was originally judged as (unsupported, expired,
an unparseable timestamp, ...) on every single redelivery. This is
deliberate, not incidental: a command whose
expires_at/issued_at/created_at/claimed_at can't be normalized (see
timestamp_utils.py) raises inside the same normalize-then-compare step
that used to crash on a raw ISO string here, and if duplicate were
checked after that step, a command stuck being redelivered with the same
bad timestamp would re-run (and re-fail, and re-record to
command_history) that normalization every single poll forever instead of
short-circuiting to a plain "duplicate" after the first attempt.

Control authority (motherpi/services/flask/services/control_authority.py)
is a blanket gate on the whole queue, not a per-command_type exemption:
the operator command queue is explicit operator intent, so every supported
command_type -- SET_HOME and LOITER included, no exceptions -- executes
while authority is OPERATOR and is rejected while it's LOCAL_AGENT (the
Local Agent's own autonomous decision-making owns the vehicle then, via a
separate write path this module has nothing to do with -- see README
"Authority model"). Checked after support so a redelivered unsupported
command_type is still reported as "unsupported", not "requires OPERATOR
authority". Home verification (command_executor.HOME_VERIFICATION_REQUIRED/
home_verified(), backed by motherpi/services/flask/services/
set_home_service.py) is checked last, after authority, since it's a
runtime precondition rather than a static property of the command_type --
LOITER/MANUAL/HOLD/PAUSE/ARM/DISARM are never in that set and are never
blocked by it.

mark_processed() is called for every non-malformed command *before*
execution is attempted, including ones that turn out unsupported or
expired -- once a command_id has been judged, redelivery of that exact id
must never re-trigger the judgment (and never a real motor command), even
if execution itself then fails. An operator-side retry of a failed command
is expected to arrive as a new command_id, not a redelivery of the old one.

Lifecycle: execution here is synchronous (call_local_endpoint is a single
blocking HTTP call to the local Flask service, for every command_type --
see command_executor.py's CommandSpec/ALLOWED_COMMANDS for how a given
command_type's request is shaped), so there is no long-running
"executing" phase to poll mid-flight -- every stage below happens within one
process_command() call. `lifecycle` still records each stage with its own
timestamp (requested -> accepted -> executing -> executed/failed, or
requested -> rejected) so the operator can see the real (if compressed)
progression rather than only a single terminal status -- the outer `status`
field keeps the exact accepted/rejected/executed/failed vocabulary already
documented in README.md, unchanged. command_history.py keeps a rolling
record of this independent of the single command_result push.
"""
import time

from config import USV_ID
import command_executor
import command_history
import command_normalization
import command_results
from command_log import is_duplicate, mark_processed
from models import make_event
from timestamp_utils import InvalidTimestamp, normalize_command_timestamps


def _expired(normalized_timestamps: dict) -> bool:
    expires_at = normalized_timestamps.get("expires_at")
    return expires_at is not None and time.time() > expires_at


def process_command(command: dict, control_authority: str = "OPERATOR"):
    """
    Returns (result_payload, local_event) for one command dict received
    from the operator backend. Does not send anything over the network --
    the caller POSTs result_payload to the operator and records the event.

    `control_authority` is the vehicle state local_agent.py already reads
    fresh each loop iteration (vehicle_state["agent"]["control_authority"],
    see local_agent._current_authority) -- defaults to "OPERATOR" here
    (the state in which queued commands execute, and the safe startup
    default -- see control_authority.py) only so every other test/call
    site that doesn't care about this gate keeps behaving exactly as it
    did before the gate existed.
    """
    command_id = command.get("command_id")
    command_type = command.get("command_type")
    source = command.get("source") or command.get("requested_by") or "operator"
    requested_at = time.time()
    lifecycle = [{"status": "requested", "timestamp": round(requested_at, 3)}]

    def _stage(status):
        lifecycle.append({"status": status, "timestamp": round(time.time(), 3)})

    def _result(status, reason, extra=None, include_lifecycle=False):
        _stage(status)
        payload = {
            "command_id": command_id,
            "usv_id": USV_ID,
            "command_type": command_type,
            "source": source,
            "status": status,
            "reason": reason,
            "timestamp": time.time(),
            "lifecycle": lifecycle,
        }
        if extra is not None:
            # For a normalized result (mode/ARM/DISARM) the result block
            # carries the command lifecycle too, part of the stable normalized
            # contract (see command_normalization.py) -- copied so a later
            # mutation of `lifecycle` can't rewrite an already-recorded
            # terminal result. Pass-through results (SET_HOME) keep the raw
            # Flask body unchanged (operator tooling depends on that exact
            # shape), so lifecycle is only ever added when asked for.
            if include_lifecycle and isinstance(extra, dict):
                extra = {**extra, "lifecycle": list(lifecycle)}
            payload["result"] = extra
        event = make_event(
            f"command_{status}",
            message=f"Command {command_type or '?'} ({command_id}): {status} -- {reason}",
            detail={"command_id": command_id, "command_type": command_type},
            severity="warning" if status in ("rejected", "failed") else "info",
        )
        command_history.record({
            "command_id": command_id,
            "command_type": command_type,
            "source": source,
            "status": status,
            "reason": reason,
            "lifecycle": lifecycle,
        })
        # Authoritative terminal result for this command_id (see
        # command_results.py) -- first write wins, so this is a no-op if
        # one is already stored. store_result() itself no-ops on a falsy
        # command_id, which covers the "malformed" call above (no
        # command_id to key on, and nothing useful to resend anyway).
        command_results.store_result(command_id, payload)
        return payload, event

    if not command_id or not command_type:
        return _result("rejected", "malformed command: missing command_id or command_type")

    # Checked immediately after "malformed", before timestamp
    # normalization/expiry -- see the module docstring. A command_id
    # already judged (whatever the original verdict) must short-circuit
    # here on every redelivery rather than re-running validation that
    # could fail (or simply re-record to command_history) identically
    # forever.
    if is_duplicate(command_id):
        stored = command_results.get_stored_result(command_id)
        if stored is not None:
            # The authoritative terminal result from the first time this
            # command_id was processed -- resent exactly as originally
            # produced (same status/reason/result/nested ack fields),
            # never re-executed against the vehicle Flask endpoint and
            # never replaced by a fresh "duplicate" verdict. See
            # command_results.py.
            event = make_event(
                f"command_{stored.get('status', 'rejected')}",
                message=f"Command {stored.get('command_type') or '?'} ({command_id}): "
                        "redelivered, resending stored result",
                detail={"command_id": command_id, "command_type": stored.get("command_type")},
                severity="info",
            )
            return stored, event
        # No stored result to resend -- e.g. a command_id marked processed
        # before command_results.py existed, or a prior store write that
        # failed. Falls back to a plain duplicate rejection, same as this
        # path always behaved.
        return _result("rejected", "duplicate command_id, already processed")

    # Every timestamp field the operator backend may send (expires_at is
    # the only one currently compared against "now", but issued_at/
    # created_at/claimed_at are normalized too per the same contract, so a
    # future use of any of them never reintroduces this class of bug) is
    # normalized here, once, before any comparison. A field that can't be
    # normalized (e.g. a malformed string) must produce one terminal
    # rejected result and stop right here -- it must never raise past this
    # point, which is exactly what let the raw ISO string reach
    # `time.time() > expires_at` and crash out to the outer emergency
    # handler in local_agent.py in the reported bug.
    try:
        normalized_timestamps = normalize_command_timestamps(command)
    except InvalidTimestamp as e:
        mark_processed(command_id)
        return _result("rejected", f"invalid timestamp: {e}")

    if _expired(normalized_timestamps):
        mark_processed(command_id)
        return _result("rejected", "command expired before execution")

    if not command_executor.is_supported(command_type):
        mark_processed(command_id)
        return _result("rejected", f"unsupported command_type: {command_type}")

    if control_authority != "OPERATOR":
        mark_processed(command_id)
        return _result(
            "rejected",
            f"blocked: {command_type} requires OPERATOR control authority "
            f"(currently {control_authority})",
        )

    if command_type in command_executor.HOME_VERIFICATION_REQUIRED and not command_executor.home_verified():
        mark_processed(command_id)
        return _result(
            "rejected",
            f"home unverified: {command_type} requires a verified Pixhawk Home position "
            "(perform Set Home Here, see GET /agent/home_status)",
        )

    mark_processed(command_id)
    _stage("accepted")
    try:
        _stage("executing")
        # Recorded now, in addition to the terminal record _result() always
        # makes below, so a command in flight is visible on
        # GET /agent/command_history *before* the blocking call below
        # returns -- execution is a single synchronous HTTP call that can
        # take up to that command_type's own configured timeout (20s for
        # SET_HOME, see command_executor.ALLOWED_COMMANDS), and this is the
        # only trace of it left behind if this process dies mid-call
        # (command_history is in-memory only, wiped by a restart) instead of
        # returning normally.
        command_history.record({
            "command_id": command_id,
            "command_type": command_type,
            "source": source,
            "status": "executing",
            "reason": "execution in progress",
            "lifecycle": list(lifecycle),
        })
        # One call for every command_type -- command_executor.call_local_endpoint
        # decides internally (from the command_type's own declarative
        # CommandSpec) whether a JSON body is built. This orchestration
        # layer has no per-command_type knowledge of that shape and never
        # should: adding a future body-requiring command type must never
        # require touching this function.
        flask_result = command_executor.call_local_endpoint(command)

        # A 2xx from the vehicle Flask service is not, by itself, a successful
        # vehicle action. For every command_type with a verifiable end state
        # (mode changes, ARM/DISARM -- see command_normalization.is_normalized),
        # the terminal status is driven by whether the vehicle actually
        # reached and held the expected state, proven by fresh HEARTBEAT
        # evidence the Flask side read back -- never by the request merely
        # having been sent. Command_types without a single verifiable end
        # state (SET_HOME) keep their existing pass-through contract: they
        # already return their own accepted/verified block the operator reads.
        if command_normalization.is_normalized(command_type):
            normalized = command_normalization.normalize(command_type, flask_result)
            if normalized["executed"]:
                return _result("executed", "command executed and verified",
                               extra=normalized, include_lifecycle=True)
            return _result(
                "failed",
                normalized["error"] or "vehicle did not confirm the expected state",
                extra=normalized, include_lifecycle=True,
            )
        return _result("executed", "command executed successfully", extra=flask_result)
    except Exception as e:
        return _result("failed", f"local execution error: {e}")
