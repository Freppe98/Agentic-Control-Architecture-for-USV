import threading
import time

from config import (
    USV_ID, USV_NAME, OPERATOR_URLS, BUFFER_FILE, LOCAL_FLASK_URL,
    LOCAL_AGENT_HTTP_HOST, LOCAL_AGENT_HTTP_PORT,
)
from communication import CommunicationMonitor, wireguard_status
from information_policy import telemetry_interval, allowed_groups
from api_client import (
    get_vehicle_state, send_to_operator, get_pending_commands,
    classify_command_result_ack, ACK_TERMINAL_ORPHAN,
)
from collectors import (
    get_communication_status,
    get_agent_status,
    count_buffered_packets,
    build_service_status,
)
from models import make_message, make_event
from buffer import buffer_message, flush_buffer
from state_machine import MissionRunner
from command_handler import process_command
import command_history
import command_log
import command_results
import mission_operation_status
import mission_upload_worker
import runtime_status
import agent_server
import transition_log
import decision_engine
import decision_policy
import decision_snapshot
import energy_policy
import experiment_injection
import planning_package
import replan_config
import replan_controller
import replan_gateway
import replan_runtime
import mission_execution_config
import mission_execution_controller
import mission_execution_gateway
import mission_execution_runtime
import mission_feasibility
import risk_config
import risk_model
import write_arbiter
import experiment_record_config
import experiment_recorder
import experiment_recording_runtime
from transition_reasons import (
    comm_transition_reason, mission_transition_reason, authority_transition_reason,
)

MAX_LOCAL_EVENTS = 20
DEFAULT_CONTROL_AUTHORITY = "OPERATOR"


def _note_command_result_ack(command_id, response):
    """
    Record how the Operator acknowledged a delivered command_result.

    Every acknowledgement reaching here is terminal (send_to_operator only
    returns on a 2xx), so the caller clears the retained result either way --
    this classifies *what happened to it*, it does not decide retention.

    A terminal orphan is not an applied result and is never reported as one:
    the Operator matched no current command and archived the payload as a
    historical audit record. That is a real divergence -- the vehicle
    performed an operation the Operator has no live command for -- so it is
    logged distinctly instead of passing silently as a normal delivery.
    """
    disposition = classify_command_result_ack(response)
    if disposition == ACK_TERMINAL_ORPHAN:
        print(f"[LOCAL AGENT] Operator acknowledged command result {command_id} as an "
              f"ORPHANED historical record (unknown command id): archived, NOT applied "
              f"to any current command. Acknowledgement is terminal -- not retrying.")
    return disposition


def _send_buffered(message):
    """Route a buffered message to its endpoint by message_type on flush.

    A buffered command_result is a retry of a delivery that was already
    marked started (mark_delivery_attempt, either by _deliver_command_result's
    first attempt or a previous flush) -- this only needs to record the
    outcome, success or failure, of *this* attempt. It never touches the
    mission operation's `state`; see mission_operation_status.py."""
    is_command_result = message.get("message_type") == "command_result"
    command_id = (message.get("payload") or {}).get("command_id") if is_command_result else None
    endpoint = "/agent/command_result" if is_command_result else "/agent/status"
    if command_id:
        try:
            mission_operation_status.mark_delivery_attempt(command_id)
        except Exception:
            pass
    try:
        response = send_to_operator(endpoint, message)
    except Exception as e:
        if command_id:
            try:
                mission_operation_status.mark_delivery_failed(command_id, error=e)
            except Exception:
                pass
        raise
    if is_command_result:
        # Operator has now acknowledged this command_id -- the stored
        # authoritative result (command_results.py) has done its job and a
        # future redelivery of this exact command_id is no longer expected
        # (deliver-once operator queue), so stop retaining it. A terminal
        # orphan ack qualifies: the Operator has permanently archived it and
        # will never match it to a command, so retrying could only repeat the
        # same orphaning forever. Returning normally is also what drops this
        # message from the buffer -- flush_buffer keeps only what raised.
        _note_command_result_ack(command_id, response)
        if command_id:
            command_results.clear_result(command_id)
            try:
                mission_operation_status.mark_delivery_acknowledged(command_id)
            except Exception:
                pass
    return response


def _deliver_command_result(result_payload):
    """Post one terminal command_result to the operator, or buffer it for
    retry on failure (and drop the stored authoritative result once the
    operator has acknowledged it). Extracted so both the synchronous command
    path and the background MISSION_UPLOAD worker deliver results the exact
    same way -- there is one result-delivery implementation, not two.

    This is result DELIVERY, not the mission operation's local outcome: that
    outcome was already recorded terminal (COMPLETED/FAILED) by
    _record_mission_operation before this ever runs, from a fresh vehicle
    readback, and nothing here can change it. What this function tracks, via
    mission_operation_status.mark_delivery_attempt/acknowledged/failed, is
    only whether the operator has been told -- so a temporarily unreachable
    operator delays notification, never the already-proven local result. A
    command_id with no mission_operation record tracking it (i.e. every
    non-mission command) makes those calls harmless no-ops (see
    mission_operation_status._update_delivery's command_id guard)."""
    result_message = make_message(
        message_type="command_result",
        source=USV_ID,
        target="operator",
        payload=result_payload,
    )
    command_id = result_payload.get("command_id")
    if command_id:
        try:
            mission_operation_status.mark_delivery_attempt(command_id)
        except Exception:
            pass
    try:
        response = send_to_operator("/agent/command_result", result_message)
        _note_command_result_ack(command_id, response)
        if command_id:
            command_results.clear_result(command_id)
            try:
                mission_operation_status.mark_delivery_acknowledged(command_id)
            except Exception:
                pass
    except Exception as e:
        buffer_message(result_message)
        if command_id:
            try:
                mission_operation_status.mark_delivery_failed(command_id, error=e)
            except Exception:
                pass
        print(f"[LOCAL AGENT] Could not send command result, buffering: {e}")


def _build_busy_rejection(command):
    """Terminal rejection for a MISSION_UPLOAD that arrives while another
    upload is already in flight -- the bounded worker allows only one at a
    time (see mission_upload_worker.py). Marked processed and stored like any
    other terminal verdict so a redelivery resends this exact result rather
    than re-judging it."""
    command_id = command.get("command_id")
    command_type = command.get("command_type")
    source = command.get("source") or command.get("requested_by") or "operator"
    now = time.time()
    reason = "rejected: a mission upload is already in progress (only one at a time)"
    lifecycle = [
        {"status": "requested", "timestamp": round(now, 3)},
        {"status": "rejected", "timestamp": round(now, 3)},
    ]
    payload = {
        "command_id": command_id,
        "usv_id": USV_ID,
        "command_type": command_type,
        "source": source,
        "status": "rejected",
        "reason": reason,
        "timestamp": now,
        "lifecycle": lifecycle,
    }
    if command_id:
        try:
            command_log.mark_processed(command_id)
        except Exception:
            pass
    command_history.record({
        "command_id": command_id,
        "command_type": command_type,
        "source": source,
        "status": "rejected",
        "reason": reason,
        "lifecycle": lifecycle,
    })
    if command_id:
        try:
            command_results.store_result(command_id, payload)
        except Exception:
            pass
    event = make_event(
        "command_rejected",
        message=f"Command {command_type or '?'} ({command_id}): rejected -- {reason}",
        detail={"command_id": command_id, "command_type": command_type},
        severity="warning",
    )
    return payload, event


def _mission_operation_expectations(command):
    """The expected counts/hash for a MISSION_UPLOAD, derived from the command
    body the operator sent. Best-effort: this is a status record, so a body we
    cannot read leaves the expectations null rather than failing the command --
    the vehicle Flask service does the authoritative validation."""
    body = command.get("payload") or command.get("body") or {}
    waypoints = body.get("waypoints") if isinstance(body, dict) else None
    if not isinstance(waypoints, list):
        return None, None
    return len(waypoints), len(waypoints) + 1


def _record_mission_operation(command, result_payload):
    """Persist the terminal outcome of a mission operation to the authoritative
    record (mission_operation_status.py) BEFORE the result is delivered, so a
    reconnecting operator can still fetch the details after the ephemeral
    worker block has gone idle -- and even if delivery itself never succeeds.

    finish() is the LAST write this makes to the record's `state`: a verified
    upload/clear becomes COMPLETED (or a genuinely failed one FAILED) right
    here and stays that way regardless of what happens to the operator link
    afterwards. Whether the result has actually reached the operator is a
    separate question, tracked in the record's `delivery` sub-status from this
    point on by _deliver_command_result/_send_buffered (see
    mission_operation_status.py's "Local outcome vs. result delivery") -- this
    function does not touch it."""
    command_id = command.get("command_id")
    status = (result_payload or {}).get("status")
    # The vehicle Flask service's structured mission result, if the executor
    # relayed one -- this is where the contract counts/hashes/diagnostics live.
    detail = (result_payload or {}).get("result") or {}
    if not isinstance(detail, dict):
        detail = {}
    error = detail.get("error")
    if error is None and status not in ("executed",):
        error = {"code": "COMMAND_FAILED",
                 "message": (result_payload or {}).get("reason")}
    try:
        mission_operation_status.finish(
            command_id,
            succeeded=(status == "executed" and detail.get("verified", True) is not False),
            observed_route_waypoint_count=detail.get("observed_route_waypoint_count"),
            observed_pixhawk_item_count=detail.get("observed_pixhawk_item_count"),
            observed_route_content_hash=detail.get("observed_route_content_hash"),
            acknowledgement=detail.get("acknowledgement"),
            empty_representation=detail.get("empty_representation"),
            error=error,
            diagnostics=detail.get("diagnostics"),
        )
    except Exception as e:  # a status record must never break command delivery
        print(f"[LOCAL AGENT] could not record mission operation status: {e}")


def _tracked_mission_run(command, control_authority):
    """process_command wrapped in mission-operation state transitions. The
    states bracket the real phases: EXECUTING while the MAVLink transaction
    runs, then the terminal outcome recorded from the result itself.

    Skips both transitions entirely for a command_id command_log.py already
    knows -- a redelivery of an already-completed operation whose worker slot
    has since gone idle (so it reaches this function at all, unlike an
    IN_FLIGHT redelivery which try_start() absorbs before this runs). Without
    this guard, process_command's own dedup path below still correctly avoids
    a second Pixhawk write, but its "duplicate, already processed" (or
    already-cleared-and-resent) result is not "executed" -- feeding it to
    _record_mission_operation would flip an already-COMPLETED record to
    FAILED for a command that changed nothing on the vehicle. The mission
    operation record answers "what did the most recent NEW admission do", not
    "what did the most recent poll return"."""
    command_id = command.get("command_id")
    is_fresh = not command_log.is_duplicate(command_id)
    if is_fresh:
        mission_operation_status.set_state(
            mission_operation_status.STATE_EXECUTING, command_id
        )
    result_payload, event = process_command(command, control_authority)
    if is_fresh:
        _record_mission_operation(command, result_payload)
    return result_payload, event


def _handle_mission_upload(command, control_authority, local_events):
    """Route a MISSION_UPLOAD onto the bounded background worker so a long
    upload never freezes the main reporting loop. The worker runs the same
    command_handler.process_command (full validation/dedup/authority/
    normalization) off-thread and delivers the terminal result via
    _deliver_command_result when done; the accepted/executing state is
    surfaced in the periodic status payload (agent.mission_upload). A second
    concurrent upload (a DIFFERENT command_id) is rejected terminally right
    here.

    Redelivery semantics: the operator backend delivers at-least-once and
    keeps redelivering a SENT command until a terminal result arrives, so a
    long upload's own command_id will be offered again while it is still
    running. try_start() distinguishes that ("IN_FLIGHT") from a genuinely
    different concurrent upload ("BUSY"). A redelivery of the in-flight
    command is a no-op here on purpose: no second execution, no BUSY
    rejection, and no second terminal result -- the original execution is
    still running and delivers exactly one terminal result when it finishes.
    Live progress reaches the operator through agent.mission_upload in the
    periodic status payload, NOT through an intermediate command_result;
    posting an intermediate ACCEPTED result would satisfy the backend's
    "terminal result arrived" condition and stop the redelivery that is the
    backend's own retry mechanism, so that must not change unless the
    operator redelivery semantics change with it."""
    command_id = command.get("command_id")
    # Open the authoritative record BEFORE handing the command to the worker.
    # If the process dies at any point from here until the terminal result is
    # written, the record is left in a non-terminal state -- which is exactly
    # what recover_after_restart() reads at next startup to fail the operation
    # closed with UNKNOWN_AFTER_RESTART instead of resuming it blind.
    # Only on a genuinely new start: an IN_FLIGHT redelivery or a BUSY
    # rejection must not reset the running operation's record -- and neither
    # may a redelivery of a command_id that already completed and whose
    # worker slot has since gone idle. That last case reaches here whenever
    # the operator's at-least-once queue offers the same command_id again
    # after the terminal result was already produced (this Local Agent's
    # own delivery may or may not have reached it yet) -- command_log.py
    # already knows this id, so it is never a genuinely new operation.
    # _tracked_mission_run makes the matching check before touching `state`;
    # see its docstring.
    if not mission_upload_worker.is_busy() and not command_log.is_duplicate(command_id):
        expected_route, expected_items = _mission_operation_expectations(command)
        mission_operation_status.begin(
            command_id, "MISSION_UPLOAD",
            expected_route_waypoint_count=expected_route,
            expected_pixhawk_item_count=expected_items,
        )
    outcome = mission_upload_worker.try_start(
        command,
        run_fn=lambda c: _tracked_mission_run(c, control_authority),
        finalize_fn=_deliver_command_result,
    )
    if outcome == "IN_FLIGHT":
        # Deliberately silent: no event, no result, no execution. Logging a
        # line is enough -- this is the expected steady state during a long
        # upload, not an anomaly, and emitting an event per poll would flood
        # the operator's event stream for the whole duration of the upload.
        print(f"[LOCAL AGENT] MISSION_UPLOAD ({command_id}): redelivered while still in flight -- ignoring")
    elif outcome == "BUSY":
        payload, event = _build_busy_rejection(command)
        local_events.append(event)
        print(f"[LOCAL AGENT] MISSION_UPLOAD ({command_id}): rejected -- a different upload is already in progress")
        _deliver_command_result(payload)
    else:
        local_events.append(make_event(
            "command_accepted",
            message=f"MISSION_UPLOAD ({command_id}) accepted -- uploading in background",
            detail={"command_id": command_id, "command_type": "MISSION_UPLOAD"},
        ))
        print(f"[LOCAL AGENT] MISSION_UPLOAD ({command_id}): accepted -- background upload started")


def _current_authority(vehicle_state):
    """
    Extract control authority from the vehicle_state already fetched this
    iteration (vehicle_state["agent"]["control_authority"], set by the
    vehicle Flask service -- see README "Control authority"). A missing or
    malformed field fails safe to OPERATOR rather than assuming the Local
    Agent has authority it was never explicitly granted.

    Note this is read fresh from the Flask service every iteration, never
    cached across a Local Agent restart -- the Local Agent has no authority
    state of its own to lose or reset. Two different things can look
    similar but are not: (a) the Local Agent process starting up always
    begins passive, because it always starts by reading whatever the Flask
    service currently reports, and that Flask service itself always
    initializes to OPERATOR on its own restart; (b) if a human has already
    explicitly granted LOCAL_AGENT (POST /agent/control_authority) and the
    Local Agent process alone restarts while the Flask service keeps
    running, this will correctly keep reading LOCAL_AGENT and command
    execution resumes. (b) is not "the Local Agent influencing the Pixhawk
    simply because it's running" -- a human already made that call before
    this process came up; the gate that matters is the explicit grant, not
    this process's uptime.
    """
    return vehicle_state.get("agent", {}).get("control_authority", DEFAULT_CONTROL_AUTHORITY)


def _poll_and_execute_commands(comm_state, control_authority, local_events):
    """
    Poll the operator backend for pending commands and execute any that
    validate. Only meaningful when we might actually reach the operator --
    while DISCONNECTED the operator backend queues commands on its side and
    there is nothing to poll against (see README: no local buffering of
    inbound commands, only outbound status/results).

    control_authority (vehicle state owned by the vehicle Flask service,
    read from GET /agent/state each iteration -- see main()) is passed
    through to command_handler.process_command(), which gates the whole
    queue on it: the operator command queue is explicit operator intent, so
    every supported command_type executes while authority is OPERATOR and
    is rejected while it's LOCAL_AGENT (the Local Agent's own autonomous
    writes own the vehicle then -- see README "Authority model"). Polling
    always happens regardless of authority (only DISCONNECTED skips it) --
    the operator backend's command queue is deliver-once (GET /agent/commands
    never re-offers a claimed command_id, see mock_operator.py), so a
    command rejected for wrong authority is a *terminal* rejection with an
    explicit reason, not silently left pending, since polling has already
    claimed it from the operator's deliver-once queue.
    """
    if comm_state == "DISCONNECTED":
        return

    for command in get_pending_commands(USV_ID):
        # MISSION_UPLOAD is the one slow command -- run it on the bounded
        # background worker so the main loop keeps reporting. Everything else
        # stays synchronous (a single fast HTTP call).
        if isinstance(command, dict) and command.get("command_type") == "MISSION_UPLOAD":
            _handle_mission_upload(command, control_authority, local_events)
            continue
        # MISSION_CLEAR is fast enough to stay synchronous, but it is still a
        # write-side MAVLink transaction, so it gets the same authoritative
        # record as an upload: opened before execution, so an interruption
        # leaves a non-terminal state for recover_after_restart() to fail
        # closed rather than a silent gap.
        is_mission_clear = (
            isinstance(command, dict) and command.get("command_type") == "MISSION_CLEAR"
        )
        # Only for a genuinely new command_id -- see _handle_mission_upload's
        # matching guard and _tracked_mission_run's docstring. A redelivery of
        # an already-completed MISSION_CLEAR must not reopen the record: if it
        # did, and this call were the only guard, _tracked_mission_run's own
        # is_duplicate check would then skip the terminal write that closes
        # it again, leaving the record stuck non-terminal at ACCEPTED.
        if is_mission_clear and not command_log.is_duplicate(command.get("command_id")):
            mission_operation_status.begin(command.get("command_id"), "MISSION_CLEAR")
        try:
            if is_mission_clear:
                result_payload, event = _tracked_mission_run(command, control_authority)
            else:
                result_payload, event = process_command(command, control_authority)
        except Exception as e:
            # Defense in depth: process_command() already turns every
            # failure from the actual vehicle Flask call into a "failed"
            # result internally (see command_handler.py's own try/except
            # around call_local_endpoint). This catches anything
            # unexpected *outside* that guarded block instead -- a bug in
            # validation/dedup/history code, a malformed command dict, a
            # disk error writing command_log.jsonl, etc. Without this, such
            # an exception would propagate out of this loop and out of
            # main()'s while-loop entirely, killing the whole Local Agent
            # process with no command_result ever posted and no
            # command_history record ever made -- leaving the operator
            # backend's command permanently stuck at SENT (claimed_at set,
            # completed_at/result forever null) since a deliver-once queue
            # never re-offers the same command_id. This is the one place
            # that guarantees requirement (6)/(8): every claimed command
            # gets a terminal result, and none can remain SENT indefinitely.
            command_id = command.get("command_id") if isinstance(command, dict) else None
            command_type = command.get("command_type") if isinstance(command, dict) else None
            source = (
                (command.get("source") or command.get("requested_by") or "operator")
                if isinstance(command, dict) else "operator"
            )
            # Bounded retry (requirement 8): whatever unexpected bug landed
            # us here, this command_id must never be re-judged again --
            # without this, a redelivery of the same command_id would hit
            # the exact same bug and post the exact same "failed" result
            # forever (the live SET_HOME symptom this suite exists to
            # catch: a raw ISO expires_at string crashing _expired() before
            # command_handler.py's own mark_processed() call was ever
            # reached). Best-effort and silently swallowed -- if the disk
            # write itself is what's broken, that must not raise a second
            # exception out of this already-exceptional path and defeat
            # the one guarantee this handler exists to provide (a terminal
            # result always gets posted, the loop never dies).
            if command_id:
                try:
                    command_log.mark_processed(command_id)
                except Exception:
                    pass
            now = time.time()
            lifecycle = [
                {"status": "requested", "timestamp": round(now, 3)},
                {"status": "failed", "timestamp": round(now, 3)},
            ]
            reason = f"unexpected local agent error: {e}"
            result_payload = {
                "command_id": command_id,
                "usv_id": USV_ID,
                "command_type": command_type,
                "source": source,
                "status": "failed",
                "reason": reason,
                "timestamp": now,
                "lifecycle": lifecycle,
            }
            event = make_event(
                "command_failed",
                message=f"Command {command_type or '?'} ({command_id}): failed -- {reason}",
                detail={"command_id": command_id, "command_type": command_type},
                severity="warning",
            )
            command_history.record({
                "command_id": command_id,
                "command_type": command_type,
                "source": source,
                "status": "failed",
                "reason": reason,
                "lifecycle": lifecycle,
            })
            # Same authoritative-result guarantee as command_handler.py's
            # own terminal paths: this command_id must resend this exact
            # "failed" result on any redelivery, not re-run the same crash
            # or fabricate a fresh generic "duplicate" verdict. Best-effort
            # for the same reason as mark_processed() above.
            if command_id:
                try:
                    command_results.store_result(command_id, result_payload)
                except Exception:
                    pass
            print(f"[LOCAL AGENT] Unexpected error processing command {command_type} ({command_id}): {e}")

        local_events.append(event)
        print(f"[LOCAL AGENT] Command {result_payload.get('command_type')} "
              f"({result_payload.get('command_id')}): {result_payload['status']} -- {result_payload['reason']}")

        # Deliver the terminal result (post, or buffer on failure and drop the
        # stored authoritative copy once acknowledged) -- see
        # _deliver_command_result and command_results.clear_result's docstring.
        _deliver_command_result(result_payload)


def _run_replan_arbitrated(replan, snapshot):
    """Run a replan transaction while holding the shared write arbiter, so it can
    never write to the vehicle at the same instant as a mission-execution
    operation. If the arbiter is currently held (a Start/Pause/Resume/final-hold
    is in flight), this replan attempt is skipped for this cycle -- replan.observe()
    will re-trigger it on a later iteration once the arbiter is free. The replan
    controller keeps its own internal one-action lock; this only coordinates it
    against the mission-execution controller (replan_controller.py is unchanged)."""
    token = write_arbiter.acquire(write_arbiter.OWNER_REPLANNING)
    if token is None:
        print("[LOCAL AGENT] Replan deferred -- vehicle write arbiter held by "
              f"{write_arbiter.current_owner()}; will re-evaluate next iteration")
        return
    try:
        replan.run_transaction(snapshot)
    finally:
        write_arbiter.release(token)


# WireGuard handshake-freshness evidence for the experiment recorder ONLY (E3
# instrumentation task). Pure mapping from communication.wireguard_status()'s
# already-computed result -- callers pass in the SAME cached dict
# get_comm_state()/vpn_ok() already read this iteration (communication.py's
# _WG_TTL_S cache), never a second `wg` probe. Never reinterprets the 180s
# (WG_RECENT_HANDSHAKE_S) threshold itself: "status" is copied verbatim from
# _parse_wg_dump(), the one place that comparison happens. RECENT_HANDSHAKE is
# the only status _parse_wg_dump() calls fresh, STALE the only one it calls
# not-fresh; every other status (DOWN/NO_HANDSHAKE/UNKNOWN) means freshness
# itself cannot be established, which stays None here rather than being
# guessed. last_handshake_age_s is passed through unchanged -- already None
# in exactly those same unavailable cases, never fabricated to 0.
def _wireguard_recorder_fields(wg_status: dict) -> dict:
    return {
        "wireguard_handshake_age_s": wg_status.get("last_handshake_age_s"),
        "wireguard_fresh": {"RECENT_HANDSHAKE": True, "STALE": False}.get(wg_status.get("status")),
    }


def main():
    print(f"[LOCAL AGENT] Starting (usv_id={USV_ID}, operators={OPERATOR_URLS}, buffer_file={BUFFER_FILE})")
    print(f"[LOCAL AGENT] Control authority is vehicle state owned by the vehicle Flask service "
          f"(GET/POST {LOCAL_FLASK_URL}/agent/control_authority) -- defaults to {DEFAULT_CONTROL_AUTHORITY} "
          "there on every restart of that service, and is never assumed here if it can't be read.")

    # Fail an interrupted mission transaction CLOSED before any command is
    # processed. A non-terminal record here means this process died mid-upload
    # or mid-clear; the vehicle-side outcome is unknowable from here, so it is
    # marked UNKNOWN_AFTER_RESTART and deliberately NOT resumed. See
    # mission_operation_status.recover_after_restart().
    mission_operation_status.recover_after_restart()

    # Thesis Experiment Recorder (experiment_recorder.py) -- constructed FIRST,
    # before either controller, so the SAME instance can be injected into both
    # (mirrors the write_arbiter idiom: one shared component both controllers
    # reference). OBSERVATIONAL, ASYNCHRONOUS, BEST-EFFORT, FAIL-OPEN with
    # respect to recording only (task section 0): disabling it via
    # EXPERIMENT_RECORDING_ENABLED=false makes every call below a fast no-op
    # with zero behavioural difference to mission execution.
    er_cfg, _ = experiment_record_config.resolve()
    recorder = experiment_recorder.ExperimentRecorder(cfg=er_cfg, vehicle_id=USV_ID)

    # Agent-controlled replanning (safe-return lifecycle). Inert by default:
    # replan_config resolves autonomous_execution_enabled=False, so the
    # controller only reasons and reports -- it never writes to the vehicle
    # until an operator explicitly enables autonomous execution. All vehicle
    # writes, when enabled, go through the existing Flask endpoints
    # (replan_gateway), never a parallel MAVLink path. Events raised on the
    # transaction's own daemon thread are buffered under a lock and drained
    # into local_events on the main thread each iteration.
    #
    # Built and REGISTERED (replan_runtime) BEFORE the inbound HTTP server
    # starts, so GET /agent/replan/status and the config/reset routes always
    # find a live controller.
    replan_cfg, _ = replan_config.resolve()
    # Continuous risk model (risk_model.py) -- resolved once here, same idiom
    # as replan_cfg/me_cfg above. Purely observational/advisory: nothing below
    # reads risk_cfg to gate a vehicle write.
    risk_cfg, _ = risk_config.resolve()
    replan_event_lock = threading.Lock()
    replan_event_buffer = []

    def _agent_event(event_type, message, severity):
        # Shared buffer for events raised off the main thread (replan
        # transaction daemon thread AND the mission-execution controller). The
        # main loop drains it under the lock each iteration.
        with replan_event_lock:
            replan_event_buffer.append(make_event(event_type, message=message, severity=severity))
        # Forward the SAME named lifecycle event to the experiment recorder.
        # This one closure is already the shared event_callback for BOTH
        # controllers' _emit() (mission_execution_started/paused/completed/
        # stopped, replan_completed/suspended/failed/safe_hold/fallback_rtl,
        # ...), so hooking here captures every named lifecycle event without
        # touching either controller's business logic (task section 43).
        # Best-effort/no-op if disabled or no run is currently active.
        try:
            recorder.record_event(event_type.upper(), source="event_callback",
                                  data={"message": message, "severity": severity})
        except Exception:
            pass

    def _bound_original_mission():
        # The mission-execution controller's bound ORIGINAL mission identity, for
        # the replan controller's fresh pre-replan proof (CRITICAL ISSUE 2).
        # Resolved lazily via the runtime registry: mission_exec is built just
        # below (it needs replan.status for its handoff), and a replan transaction
        # only ever runs long after both are registered.
        ctrl = mission_execution_runtime.get_controller()
        return ctrl.bound_original_mission() if ctrl is not None else None

    # Authoritative decision policy (E2 water-trial integration task): maps the
    # continuous risk model's recommendation to an ActionRequest, the SOLE
    # trigger fed into replan.observe() below -- see decision_policy.py's
    # module docstring. One instance for the process lifetime so its
    # observability generation counter is meaningful, and created BEFORE
    # `replan` so its latest_feasibility_evidence can be wired into
    # ReplanController as a lazy feasibility_fn callback (RTL fallback proof).
    decision_policy_instance = decision_policy.DecisionPolicy()

    replan = replan_controller.ReplanController(
        cfg=replan_cfg,
        gateway=replan_gateway.FlaskReplanGateway(),
        status_store=replan_controller.StatusStore(),
        event_callback=_agent_event,
        original_mission_fn=_bound_original_mission,
        recorder=recorder,
        feasibility_fn=decision_policy_instance.latest_feasibility_evidence,
    )
    # Fail any replan transaction interrupted by a process restart CLOSED
    # (UNKNOWN_AFTER_RESTART) before evaluating anything -- never resume it.
    replan.recover_after_restart()
    energy = energy_policy.EnergyPolicy(replan_cfg)
    replan_runtime.register(replan, energy)

    # Mission-execution controller: owns the ORIGINAL mission lifecycle (Start/
    # Pause/Resume/return completion), a DISTINCT controller from the replanning
    # FSM above. It shares the write arbiter with the replan path so the two can
    # never issue simultaneous vehicle writes, and reads the live replan status
    # (replan.status) for the handoff. Registered before the HTTP server starts
    # so /agent/mission_execution/* always finds it.
    def _stop_reset_replan():
        # Bounded internal reset hook Stop calls to reset the replanning
        # transaction / trigger latch for a fresh mission run -- the same reset the
        # operator's POST /agent/replan/reset performs (controller reset + energy
        # debounce), reused rather than duplicated. Never disturbs a running
        # transaction (replan.reset refuses one).
        result = replan.reset()
        try:
            energy.reset()
        except Exception:
            pass
        return result

    me_cfg = mission_execution_config.load()
    mission_exec = mission_execution_controller.MissionExecutionController(
        cfg=me_cfg,
        gateway=mission_execution_gateway.FlaskMissionExecutionGateway(),
        status_store=mission_execution_controller.StatusStore(),
        event_callback=_agent_event,
        replan_status_fn=replan.status,
        # Stop's bounded internal reset hooks (task): reset the replan transaction/
        # latch + energy debounce, and clear an active simulated experiment
        # injection so the next test starts clean. Neither touches real telemetry.
        replan_reset_fn=_stop_reset_replan,
        experiment_reset_fn=experiment_injection.clear,
        recorder=recorder,
    )
    # Fail any mission-execution operation interrupted by a process restart
    # CLOSED (UNKNOWN_AFTER_RESTART) before evaluating anything -- never resume.
    mission_exec.recover_after_restart()
    # Experiment-recorder restart reconciliation (task section 6) -- AFTER
    # mission-execution's own recovery has settled, so this reads the
    # POST-reconciliation state: reopen the previous run append-only if
    # mission execution reconciled to a live state, else finalize it
    # INTERRUPTED. Never deletes/truncates prior evidence either way.
    recorder.reconcile_after_restart(mission_exec.status())
    mission_execution_runtime.register(mission_exec)
    experiment_recording_runtime.register(recorder)

    threading.Thread(
        target=agent_server.serve_forever,
        args=(LOCAL_AGENT_HTTP_HOST, LOCAL_AGENT_HTTP_PORT),
        daemon=True,
    ).start()

    comm_monitor = CommunicationMonitor()
    mission_runner = MissionRunner()
    local_events = []
    last_success_ts = None
    # Application-level Scout<->Operator round-trip of the last POST
    # /agent/status (ms), and a monotonic status sequence number the operator
    # uses to derive uplink packet loss from gaps. Both flow out in the
    # communication block -- see collectors.get_communication_status.
    last_rtt_ms = None
    status_seq = 0
    prev_mission_state = mission_runner.state
    prev_authority = DEFAULT_CONTROL_AUTHORITY
    # current_decision/current_decision_reason: the decision engine's own
    # running notion of "what did we last decide", so a repeat evaluation
    # that lands on the same label doesn't get logged as a transition (see
    # decision_engine.decide()). previous_decision/previous_decision_reason
    # are only overwritten at the moment the label actually changes, so the
    # Agent page can always show "what changed from what" -- None until the
    # first change happens.
    current_decision = None
    current_decision_reason = None
    previous_decision = None
    previous_decision_reason = None
    # Set on a comm recovery edge; cleared once flush_buffer reports the
    # backlog fully drained. Flushing always happens *before* the live
    # status send below, in the same iteration, so a fresh view is always
    # the last thing the operator sees -- buffered history can never look
    # like current state. If the flush doesn't fully drain (operator drops
    # again mid-flush) or this iteration's vehicle fetch fails afterwards,
    # pending_flush stays set and retries on the next iteration.
    pending_flush = False

    try:
        while True:
            runtime_status.mark_alive()
            comm_state = comm_monitor.poll()
            groups = allowed_groups(comm_state)

            if comm_state != comm_monitor.previous_state and comm_monitor.previous_state is not None:
                comm_reason = comm_transition_reason(comm_monitor.previous_state, comm_state)
                print(f"[LOCAL AGENT] Comm state: {comm_monitor.previous_state} -> {comm_state}: {comm_reason}")
                # Causal measurements already available in memory at this
                # exact point (task section 10) -- NOT a new measurement:
                # comm_state itself is this iteration's fresh get_comm_state()
                # result (just computed by comm_monitor.poll() above), and
                # count_buffered_packets() is the same cheap local read the
                # backlog-flush check below already performs. Deliberately
                # does NOT include telemetry_age_s/heartbeat_age_s/
                # vpn_reachable here -- those are only computed later this
                # same iteration (after the vehicle-state fetch) and are not
                # yet "already available" at this call site without either
                # reordering the loop or taking a new measurement, both out
                # of scope for this patch (see FINAL REPORT section D).
                transition_log.record_transition(
                    "communication", comm_monitor.previous_state, comm_state, comm_reason,
                    extra={
                        "operator_reachable": comm_state == "CONNECTED",
                        "buffered_message_count": count_buffered_packets(),
                        # REAL (measured) or SIMULATED (an active experiment
                        # injection override -- task E3) -- so the audit trail
                        # never lets a synthetic trial be mistaken for a real
                        # link event. See communication.resolve_comm_state.
                        "source": comm_monitor.source,
                    },
                )
                local_events.append(make_event(
                    "communication_state_changed",
                    message=comm_reason,
                    detail={"from": comm_monitor.previous_state, "to": comm_state},
                ))

            if comm_monitor.just_recovered:
                pending_flush = True
                local_events.append(make_event(
                    "comm_recovered",
                    message=f"Communication recovered from {comm_monitor.previous_state}",
                    detail={"from": comm_monitor.previous_state},
                ))

            # Catches backlog that didn't originate from a comm-down edge --
            # e.g. a command_result stuck in the buffer because its endpoint
            # 404/405s (a route mismatch, not a connectivity gap) while
            # comm_state has stayed CONNECTED throughout, or a leftover
            # buffer file from a previous process restart. Without this, such
            # a backlog would only ever be retried on the next genuine
            # PARTITIONED/DISCONNECTED -> CONNECTED edge, which may never
            # happen in a session that never actually drops.
            if not pending_flush and comm_state == "CONNECTED" and count_buffered_packets() > 0:
                pending_flush = True

            if pending_flush:
                result = flush_buffer(_send_buffered)
                print("[LOCAL AGENT] Backlog flush:", result)
                pending_flush = result["remaining"] > 0

            try:
                vehicle_state = get_vehicle_state()
            except Exception as e:
                print("[LOCAL AGENT] Could not fetch local state:", e)
                time.sleep(2)
                continue

            mission_state = mission_runner.update(vehicle_state.get("mission", {}))
            if mission_state != prev_mission_state:
                vs_mission = vehicle_state.get("mission", {})
                mission_reason = mission_transition_reason(
                    mission_state, vs_mission.get("current_waypoint"),
                    vs_mission.get("mission_count"), mission_runner.mission_id,
                )
                transition_log.record_transition("mission", prev_mission_state, mission_state, mission_reason)
                local_events.append(make_event(
                    "mission_state_changed",
                    message=mission_reason,
                    detail={"from": prev_mission_state, "to": mission_state, "mission_id": mission_runner.mission_id},
                ))
                prev_mission_state = mission_state

            current_authority = _current_authority(vehicle_state)
            if current_authority != prev_authority:
                authority_reason = authority_transition_reason(
                    vehicle_state.get("agent", {}), prev_authority, current_authority,
                )
                transition_log.record_transition("authority", prev_authority, current_authority, authority_reason)
                local_events.append(make_event(
                    "control_authority_changed",
                    message=authority_reason,
                    detail={"from": prev_authority, "to": current_authority},
                ))
                prev_authority = current_authority

            _poll_and_execute_commands(comm_state, current_authority, local_events)
            del local_events[:-MAX_LOCAL_EVENTS]

            # Vehicle owns raw mission identity/progress; the Local Agent owns
            # the phase interpretation and layers it on top here.
            mission_payload = dict(vehicle_state.get("mission", {}))
            mission_payload.update(mission_runner.to_dict())

            # Decision engine: re-evaluated fresh every iteration from this
            # iteration's own observations (never cached across iterations),
            # so decision_reason always reflects current evidence rather than
            # whatever the last *transition* happened to be about.
            decision_inputs = decision_engine.build_decision_inputs(
                vehicle_state, comm_state, mission_state, mission_runner, current_authority,
            )
            new_decision, new_decision_reason = decision_engine.decide(decision_inputs)
            decision_confidence, decision_confidence_missing = decision_engine.confidence(decision_inputs)
            watch_conditions = decision_engine.build_watch_conditions(decision_inputs)
            current_policy = decision_engine.build_policy(comm_state, mission_state, current_authority)

            if new_decision != current_decision:
                if current_decision is not None:
                    transition_log.record_transition("decision", current_decision, new_decision, new_decision_reason)
                    local_events.append(make_event(
                        "decision_changed",
                        message=new_decision_reason,
                        detail={"from": current_decision, "to": new_decision},
                    ))
                previous_decision = current_decision
                previous_decision_reason = current_decision_reason
                current_decision = new_decision
            current_decision_reason = new_decision_reason

            situation = decision_engine.build_situation(
                vehicle_state, comm_state, mission_state, current_authority,
                current_policy["autonomy_level"], decision_confidence,
            )

            # ── Agent-controlled replanning: snapshot, feasibility, risk ────
            # Build the immutable snapshot from this iteration's observations.
            # feasibility_result/risk_result/action_request are all computed
            # BEFORE energy.evaluate()/replan.observe() below (E2 water-trial
            # integration task) -- none of the three actually depends on
            # energy_result, and every existing "read prior state before it's
            # mutated" ordering is preserved (mission_status/replan.status()
            # are pure reads either way).
            planning_pkg = planning_package.load()
            injection = experiment_injection.active(USV_ID)
            replan_snapshot = decision_snapshot.build_snapshot(
                vehicle_state, comm_state, current_authority,
                planning_package=planning_pkg, experiment_overrides=injection,
            )

            # ── Mission energy feasibility (continuous, advisory) ────────────
            # Two independent questions, about two distinct Homes (task:
            # mission-energy-feasibility Home-semantics correction -- see
            # mission_feasibility.py's module docstring for the full
            # reasoning): "can the REMAINING OPERATOR-PLANNED mission still be
            # completed on the current effective battery, with the reserve
            # held back?" (mission_feasible) and, separately, "if abandoned
            # RIGHT NOW, could the vehicle safely return to the CURRENT
            # VERIFIED Pixhawk/RTL Home?" (rtl_return_feasible) -- evaluated
            # EVERY iteration (before Start, and continuously while running).
            # Purely observational on its own: mission_exec's own can_start/
            # etc. only read the CACHED value below; the authoritative
            # decision-policy wiring further down is what actually turns this
            # (combined with risk) into a return/hold action request. Start
            # itself always re-evaluates fresh, inline, before ARM (see
            # mission_execution_controller._run_start).
            #
            # mission_status is read ONCE here (before update_energy_
            # feasibility()/observe() below mutate anything) and reused for
            # BOTH this call's mission_binding and the risk assessment's
            # mission_execution_status just below -- same "this iteration's
            # PRIOR state" snapshot either read already relied on, just taken
            # in one place instead of two slightly different ones.
            #
            # mission_binding (task: mission-route-identity safety) is
            # mission_execution_controller's own existing readiness/binding
            # proof against a live Pixhawk readback (planning_package.
            # build_readiness, refreshed by the controller's own background
            # poll) -- reused here, never a second, parallel hash comparison,
            # to gate the MISSION dimension on the route's identity actually
            # being proven current. A stale/mismatched/unproven package route
            # makes the mission dimension UNKNOWN; it never touches the RTL
            # dimension (see mission_feasibility.py's module docstring).
            mission_status = mission_exec.status()
            feasibility_result = mission_feasibility.evaluate_from_snapshot(
                replan_snapshot, planning_pkg, injection, replan_cfg,
                mission_binding=mission_status.get("binding"),
            )
            mission_exec.update_energy_feasibility(feasibility_result.to_dict())

            # ── Continuous risk assessment (observational/advisory only) ────
            # Evaluated EVERY iteration from evidence this iteration already
            # gathered (the same snapshot/feasibility/vehicle_state above, plus
            # the two controllers' own status() -- neither of which this call
            # mutates). Deterministic, cheap, side-effect-free: no vehicle
            # command, no mode/authority change follows from this module
            # itself (task: continuous risk model, sections 5/17/21) -- its
            # recommendation only becomes an action via decision_policy below.
            # mission_execution_status/replan_status are read BEFORE
            # mission_exec.observe()/replan.observe() below so risk reflects
            # this iteration's PRIOR state, exactly the same "advisory,
            # cached-for-next-read" ordering energy_feasibility above already
            # uses relative to mission_exec.update_energy_feasibility() -- one
            # iteration of lag on the mission/binding/replan-fsm sub-signals
            # only, never on energy/communication/navigation/health.
            risk_result = risk_model.evaluate_from_agent_state(
                feasibility=feasibility_result.to_dict(),
                comm_state=comm_state,
                control_authority=current_authority,
                vehicle_state=vehicle_state,
                mission_execution_status=mission_status,
                replan_status=replan.status(),
                cfg=risk_cfg,
            )
            mission_exec.update_risk_assessment(risk_result.to_dict())

            # ── Authoritative decision policy -> action request -> replan FSM ─
            # decision_policy.py (E2 water-trial integration task): the single
            # deterministic bridge from risk_result's recommendation to a
            # REQUEST_RETURN_HOME/REQUEST_HOLD action request. Produces a value
            # only -- issues no vehicle command itself; it is handed into
            # replan.observe() below, the FSM's own single existing entry
            # point, alongside the pre-existing legacy energy trigger.
            action_request = decision_policy_instance.evaluate(
                risk_result, feasibility_result, replan_snapshot,
            )

            # ── Agent-controlled replanning: trigger decision ─────────────────
            # evaluate the energy policy (honouring any active simulated
            # injection), and let the controller decide whether to start a
            # safe-return/hold transaction (from either the legacy energy
            # trigger or action_request above). The transaction runs on a
            # daemon thread so a multi-second LOITER/upload/AUTO sequence never
            # blocks the main reporting loop -- same pattern as the
            # MISSION_UPLOAD worker.
            energy_result = energy.evaluate(replan_snapshot, injection)
            replan_decision = replan.observe(replan_snapshot, energy_result, action_request=action_request)
            if replan_decision["start"]:
                print(f"[LOCAL AGENT] Replan starting: {replan_decision['reason']} "
                      f"(snapshot {replan_snapshot.snapshot_id})")
                threading.Thread(
                    target=_run_replan_arbitrated, args=(replan, replan_snapshot), daemon=True
                ).start()

            # ── Mission-execution lifecycle (original mission) ──────────────
            # Passive per-iteration update: mode/sequence observation, the
            # replanning handoff (RUNNING -> derived REPLANNING -> RETURNING_HOME
            # on MONITORING_REVISED, or -> SUSPENDED on a replan terminal
            # failure), and the return-to-Home arrival monitor. observe() never
            # writes; when it confirms arrival it asks for a final LOITER hold,
            # which is run on its own daemon thread so the multi-second LOITER
            # never blocks this loop -- same pattern as the replan transaction.
            me_decision = mission_exec.observe(replan_snapshot, replan.status())
            if me_decision["final_hold"]:
                print("[LOCAL AGENT] Mission execution: arrival confirmed -- running final LOITER hold")
                threading.Thread(target=mission_exec.run_final_hold, daemon=True).start()

            with replan_event_lock:
                if replan_event_buffer:
                    local_events.extend(replan_event_buffer)
                    replan_event_buffer.clear()

            agent_status = get_agent_status(comm_state, mission_state)
            agent_status["control_authority"] = current_authority
            # Pixhawk Home verification/readiness (services/set_home_service.py
            # on the vehicle Flask side, folded into vehicle_state["agent"] --
            # see agent_state.py there) -- passed through as-is, same idiom as
            # control_authority above, so the operator backend gets it via the
            # existing POST /agent/status push rather than a separate endpoint
            # the frontend would need to poll directly.
            agent_status["home_status"] = vehicle_state.get("agent", {}).get("home_status")
            # Bounded MISSION_UPLOAD worker state (idle/executing/delivering) --
            # surfaced here so an in-flight upload is visible to the operator
            # in the normal status stream, proving the main loop stays alive
            # and reporting while a long upload runs off-thread.
            agent_status["mission_upload"] = mission_upload_worker.status()
            # The authoritative persistent record of the most recent mission
            # operation. Kept ALONGSIDE the lightweight live block above, not
            # instead of it: that block goes idle the moment an upload ends,
            # while this one retains the terminal counts/hashes/diagnostics so
            # an operator reconnecting after a comm interruption can still
            # find out how the operation actually finished.
            agent_status["mission_operation"] = mission_operation_status.get()
            agent_status["current_policy"] = current_policy
            agent_status["current_decision"] = current_decision
            agent_status["decision_reason"] = current_decision_reason
            agent_status["previous_decision"] = previous_decision
            agent_status["previous_decision_reason"] = previous_decision_reason
            agent_status["decision_confidence"] = decision_confidence
            agent_status["decision_confidence_missing_inputs"] = decision_confidence_missing
            agent_status["decision_inputs"] = decision_inputs
            agent_status["watch_conditions"] = watch_conditions
            agent_status["decision_timeline"] = transition_log.get_recent_by_type("decision")
            agent_status["situation"] = situation
            # Agent-controlled replanning surface for the Operator Station: the
            # controller's full FSM/transaction status, this iteration's energy
            # calculation, and whether a simulated experiment injection is
            # active (always tagged SIMULATED). Reported every iteration whether
            # or not autonomous execution is enabled.
            agent_status["replan"] = replan.status()
            # Mission-execution lifecycle status (the canonical Start/Pause/
            # Resume/return surface the Operator Station derives its button
            # state from). Reported every iteration, distinct from replan above.
            agent_status["mission_execution"] = mission_exec.status()
            agent_status["energy_policy"] = energy_result.to_dict()
            # Mirrors mission_exec.status()["energy_feasibility"] (the value
            # that actually gates start_eligible/can_start there) at the top
            # level, alongside energy_policy above, for a single place the
            # Operator Agent Mission card can read "Energy: FEASIBLE +24%"
            # without reaching into mission_execution.
            agent_status["energy_feasibility"] = feasibility_result.to_dict()
            # Mirrors mission_exec.status()["risk"] at the top level, alongside
            # energy_feasibility above -- one place the Operator Agent Mission
            # card can read "Risk: LOW" without reaching into
            # mission_execution. Purely observational; see risk_model.py.
            agent_status["risk"] = risk_result.to_dict()
            # Authoritative decision policy's action request (E2 water-trial
            # integration task) -- mirrors agent_status["replan"]["action_request"]
            # at the top level, alongside risk/energy_feasibility above.
            agent_status["action_request"] = action_request.to_dict()
            agent_status["experiment_injection"] = experiment_injection.status(USV_ID)

            # ── Experiment-recorder decision snapshot + throttled telemetry ──
            # No-op (fast early-out) when disabled or no run is currently
            # active -- see experiment_recorder.ExperimentRecorder._run_ready.
            # Physical / injected / policy-used battery are kept explicitly
            # distinct (task section 19/2's E2/E5 requirement): replan_snapshot
            # carries the PHYSICAL reading, `injection` the raw override, and
            # energy_result.inputs the value the energy POLICY actually used.
            recorder.record_decision({
                "snapshot_id": replan_snapshot.snapshot_id,
                "position": {"latitude": replan_snapshot.latitude, "longitude": replan_snapshot.longitude},
                "speed": replan_snapshot.groundspeed, "heading": replan_snapshot.heading,
                "current_waypoint": replan_snapshot.current_sequence,
                "mission_count": replan_snapshot.mission_count,
                "distance_to_home_m": replan_snapshot.distance_to_home_m,
                "communication_state": comm_state,
                # REAL (measured) or SIMULATED (an active experiment injection
                # override -- task E3, communication.resolve_comm_state) --
                # so a decision record made under a synthetic comm trial is
                # always distinguishable from one made under real evidence.
                "communication_source": comm_monitor.source,
                "operator_reachable": decision_inputs.get("operator_reachable"),
                "telemetry_age_s": replan_snapshot.telemetry_age_s,
                "last_operator_contact_age_s": decision_inputs.get("heartbeat_age_s"),
                "battery": {
                    "physical_percent": replan_snapshot.battery_percent,
                    "raw": replan_snapshot.battery_raw,
                    "valid": replan_snapshot.battery_valid,
                    "injected_percent": (injection or {}).get("battery_percent"),
                    "policy_percent": energy_result.inputs.get("battery_percent"),
                    "simulated": energy_result.simulated,
                },
                "energy": energy_result.inputs,
                # Mission-energy-feasibility evidence (task: mission energy
                # feasibility, section 10) -- status/reason, both feasibility
                # booleans, both distances, both margins, reserve, and the
                # exact effective battery + source used, so the timeline alone
                # is enough to reconstruct why a margin went negative.
                "feasibility": feasibility_result.to_dict(),
                # Continuous risk assessment (task: continuous risk model,
                # section 20) -- the full RiskResult (score/level/per-component
                # scores/weights/dominant contributor/hard_constraint_violated/
                # confidence/recommendation), alongside feasibility above, so
                # post-hoc analysis (E1-E4: nominal, energy-triggered
                # adaptation, communication degradation/loss, authority
                # takeover) can reconstruct exactly why the score/level moved
                # on any given decision record without cross-referencing a
                # separate stream.
                "risk": risk_result.to_dict(),
                # Authoritative decision policy's action request (E2 water-
                # trial integration task section 12): together with risk above
                # and replan_state below, lets any decision record reconstruct
                # all three layers -- risk / recommendation / action_request /
                # fsm_state -- without cross-referencing a separate stream.
                "action_request": action_request.to_dict(),
                "mission_execution_state": agent_status["mission_execution"].get("state"),
                "replan_state": agent_status["replan"].get("fsm_state"),
                # replan_controller.py's OWN energy decision (MONITOR /
                # REPLAN_SAFE_RETURN, replan_controller.py:status()'s
                # current_decision) -- distinct from `replan_state` (the FSM's
                # procedural step) and from `current_decision` above
                # (decision_engine.py's separate label). Tracked as
                # replan_decision_change_count (task section 11).
                "replan_decision": agent_status["replan"].get("current_decision"),
                "authority": current_authority,
                "current_decision": current_decision,
                "previous_decision": previous_decision,
                "reason_codes": energy_result.reason_codes,
                "trigger_active": agent_status["replan"].get("trigger_active"),
                "trigger_generation": agent_status["replan"].get("trigger_generation"),
                "consumed_trigger_generation": agent_status["replan"].get("consumed_trigger_generation"),
                "trigger_consumed": agent_status["replan"].get("trigger_consumed"),
                "terminal_reason": agent_status["replan"].get("terminal_reason"),
                "strategy": agent_status["replan"].get("strategy"),
            })
            # Hand the freshly-computed telemetry fields off to the recorder's
            # in-memory latest-snapshot (task section 4) -- a cheap dict
            # replace, no I/O, returns immediately every iteration regardless
            # of main-loop cadence. A dedicated sampler thread inside the
            # recorder decides WHEN to actually turn this into a telemetry
            # record, at the configured experiment_record_telemetry_hz, fully
            # decoupled from however fast or slow this loop iterates (this is
            # the fix for the real-run telemetry undersampling: the old
            # in-loop throttle here could never exceed the loop's own,
            # much slower, I/O-bound cadence).
            # WireGuard handshake freshness evidence for the recorder ONLY
            # (E3 instrumentation task) -- see _wireguard_recorder_fields()
            # above. wireguard_status() is the exact same cached call
            # get_comm_state()/vpn_ok() already read this iteration
            # (communication.py's _WG_TTL_S cache), not a second `wg` probe.
            wg_recorder_fields = _wireguard_recorder_fields(wireguard_status())
            recorder.update_latest_telemetry_snapshot({
                "vehicle_id": USV_ID,
                "latitude": replan_snapshot.latitude, "longitude": replan_snapshot.longitude,
                "position_age_s": replan_snapshot.position_age_s,
                "altitude_m": vehicle_state.get("telemetry", {}).get("alt"),
                "speed_m_s": replan_snapshot.groundspeed, "heading_deg": replan_snapshot.heading,
                "mode": replan_snapshot.mode_name, "armed": replan_snapshot.armed,
                "current_waypoint": replan_snapshot.current_sequence,
                "mission_count": replan_snapshot.mission_count,
                "physical_battery_percent": replan_snapshot.battery_percent,
                "battery_raw": replan_snapshot.battery_raw, "battery_valid": replan_snapshot.battery_valid,
                # Which source the energy policy actually used this iteration --
                # reuses mission_feasibility.py's own existing evidence verbatim
                # (SOURCE_PHYSICAL / SOURCE_INJECTED / None), never a second
                # labelling scheme (energy-calibration recorder task, Phase 2).
                "battery_source": feasibility_result.battery_source,
                "injected_battery_percent": (injection or {}).get("battery_percent"),
                "policy_battery_percent": energy_result.inputs.get("battery_percent"),
                # Raw physical battery voltage/current (decision_snapshot.py,
                # sourced straight from vehicle MAVLink BATTERY_STATUS/SYS_STATUS
                # telemetry). experiment_injection.py has no voltage/current
                # override, so these are ALWAYS the physical measurement --
                # never overwritten or lost when a battery_percent injection is
                # active (energy-calibration recorder task, POWER TELEMETRY
                # REQUIREMENTS). This is the raw evidence Q_meas/E_meas
                # integration is computed from, offline, after the run.
                "voltage_V": replan_snapshot.battery_voltage,
                "current_A": replan_snapshot.battery_current,
                "distance_to_home_m": replan_snapshot.distance_to_home_m,
                "safe_return_distance_m": energy_result.inputs.get("safe_return_distance_m"),
                "usable_range_m": energy_result.inputs.get("usable_range_m"),
                "return_cost_percent": energy_result.inputs.get("return_cost_percent"),
                "energy_margin_percent": energy_result.inputs.get("margin_percent"),
                "communication_state": comm_state,
                "communication_source": comm_monitor.source,
                "operator_reachable": decision_inputs.get("operator_reachable"),
                "telemetry_age_s": replan_snapshot.telemetry_age_s,
                # Recorder-only WireGuard freshness evidence (E3
                # instrumentation task, see _wireguard_recorder_fields above)
                # -- observational, never consulted by get_comm_state()/
                # vpn_ok()/resolve_comm_state() themselves, so this can never
                # influence comm_state.
                **wg_recorder_fields,
                "operator_contact_age_s": decision_inputs.get("heartbeat_age_s"),
                "buffer_usage": count_buffered_packets(),
                "control_authority": current_authority,
                "autonomy_level": current_policy.get("autonomy_level"),
                "mission_execution_state": agent_status["mission_execution"].get("state"),
                "mission_execution_phase": agent_status["mission_execution"].get("phase"),
                "replan_state": agent_status["replan"].get("fsm_state"),
                "current_decision": current_decision,
            })
            # last_transition reflects the most recent *real* transition this
            # Local Agent has recorded (communication/mission/authority/
            # decision) -- see transition_reasons.py -- kept as its own field
            # rather than overwriting decision_reason, which now always comes
            # straight from this iteration's own decision engine evaluation.
            agent_status["last_transition"] = transition_log.last()

            status_seq += 1
            mavlink_evidence = decision_engine.build_mavlink_evidence(vehicle_state)
            payload = {
                "usv_id": USV_ID,
                "name": USV_NAME,
                "comm_state": comm_state,
                "groups": groups,
                "telemetry": vehicle_state.get("telemetry", {}),
                # MAVLink-derived vehicle-health blocks, passed through from the
                # vehicle Flask side (services/vehicle_derivations.py) so the
                # operator gets power source / failsafe / IMU health / per-group
                # freshness in the same push as everything else.
                "power": vehicle_state.get("power", {}),
                "failsafe": vehicle_state.get("failsafe", {}),
                "imu": vehicle_state.get("imu", {}),
                "freshness": vehicle_state.get("freshness", {}),
                "mavlink": mavlink_evidence,
                "mission": mission_payload,
                "communication": get_communication_status(
                    comm_state, last_success_ts, rtt_ms=last_rtt_ms, seq=status_seq,
                    source=comm_monitor.source,
                ),
                "service_status": build_service_status(
                    vehicle_state_ok=True,
                    mavlink_connected=mavlink_evidence.get("mavlink_connected"),
                    health=vehicle_state.get("health", {}),
                ),
                "agent": agent_status,
                "health": vehicle_state.get("health", {}),
                "measurements": vehicle_state.get("measurements", {}),
                "events": vehicle_state.get("events", []) + local_events,
                # Rolling audit trail (up to the latest 100) of every
                # communication/mission/authority transition this process has
                # observed, each with the concrete trigger -- see
                # transition_log.py. Unlike `events` above, this is never
                # cleared on a successful send, so a reconnecting operator
                # backend always gets the full recent history, not just a delta.
                "transitions": transition_log.get_recent(),
            }

            message = make_message(
                message_type="status",
                source=USV_ID,
                target="operator",
                payload=payload
            )

            try:
                _send_started = time.perf_counter()
                response = send_to_operator("/agent/status", message)
                # Application-level Scout<->Operator RTT: wall-clock of the POST
                # that actually reached the operator and came back. This is the
                # comms round-trip (Pi <-> 4G/WireGuard <-> Operator), not the
                # Pixhawk USB link. Reported on the *next* iteration's
                # communication block (last measured value).
                last_rtt_ms = round((time.perf_counter() - _send_started) * 1000.0, 1)
                print("[LOCAL AGENT] Sent:", comm_state, mission_state, response)
                last_success_ts = time.time()
                local_events.clear()
            except Exception as e:
                buffer_message(message)
                print(f"[LOCAL AGENT] Could not send, buffering (buffer now {count_buffered_packets()} total):", e)

            time.sleep(telemetry_interval(comm_state))
    except KeyboardInterrupt:
        print("\n[LOCAL AGENT] Shutdown requested (Ctrl+C), exiting cleanly.")


if __name__ == "__main__":
    main()
