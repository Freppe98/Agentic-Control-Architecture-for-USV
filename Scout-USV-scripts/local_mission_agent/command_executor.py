"""
Maps operator command types to local Flask (mavlink2rest) endpoints.

Mode changes, RTL, mission pause/resume, ARM/DISARM, SET_HOME, and
MISSION_UPLOAD all go
through one execution path: ALLOWED_COMMANDS maps each command_type to a
CommandSpec (method, path, and -- only for the few command types that need
one -- a build_body callable). call_local_endpoint(command) is the single
place a request is ever made: it always takes the full command dict, looks
up its spec, and branches exactly once on whether that spec carries a
build_body callable. There is no per-command_type function and no second
registry ("which command_types need special handling") to keep in sync
with ALLOWED_COMMANDS -- a future command type that needs a body is "write
a small build_body function and reference it in ALLOWED_COMMANDS", never a
new executor and never a new branch in command_handler.py.

ARM/DISARM/SET_HOME are validated and executed exactly like every other
command type here (expiry/dedup/support checks in command_handler, same
path through call_local_endpoint) -- this module does not add a second
confirmation gate of its own. The Operator Backend is the only thing the
frontend ever talks to; the Local Agent has no inbound HTTP surface of its
own for issuing commands (see agent_server.py, strictly read-only) --
SET_HOME reaches the vehicle Flask service exactly the way every other
command type does: queued by the operator backend, polled via
GET /agent/commands, validated and executed by
command_handler.process_command()/this module, result pushed back via
POST /agent/command_result.

The actual "is anyone even allowed to send this right now" gate is control
authority (motherpi/services/flask/services/control_authority.py): the
operator command queue is explicit operator intent, so command_handler.
process_command() executes every supported command_type while authority is
OPERATOR and rejects all of them while it's LOCAL_AGENT (the Local Agent's
own autonomous decision-making owns the vehicle then, not the queue) --
see README "Authority model". There is no per-command_type exemption from
this: SET_HOME and LOITER work under OPERATOR and are rejected under
LOCAL_AGENT exactly like every other command_type. call_local_endpoint has
no authority check of its own because it has exactly one caller and that
caller already refuses to invoke it for a gated command; adding a second
check here would just be an unreachable no-op, not real defense in depth.
If a second call site is ever added, it must gate on control authority too.
"""
from typing import Callable, NamedTuple, Optional

import requests

from config import LOCAL_FLASK_URL
import api_client

_DEFAULT_TIMEOUT_S = 5.0
# SET_HOME's own internal bounded wait for COMMAND_ACK/HOME_POSITION (see
# services/set_home_service.py) can take several seconds -- a client-side
# timeout shorter than that would cut off a request that would otherwise
# have finished normally. Declared on SET_HOME's own CommandSpec below, not
# a special case in call_local_endpoint.
_SET_HOME_TIMEOUT_S = 20.0
# MISSION_UPLOAD's vehicle-side work (MISSION_CLEAR_ALL + the full
# MISSION_REQUEST_INT handshake + a complete fresh readback -- see
# services/mission_upload_service.py) can take tens of seconds; the client
# timeout must comfortably exceed the Flask side's own overall+readback bound
# (~45s worst case) so a slow-but-succeeding upload isn't cut off here. This
# is exactly why MISSION_UPLOAD runs on the bounded background worker
# (mission_upload_worker.py) rather than blocking the main reporting loop.
_MISSION_UPLOAD_TIMEOUT_S = 60.0
# MISSION_CLEAR's vehicle-side work is a single MISSION_CLEAR_ALL plus a
# bounded optional ack wait plus one complete fresh readback (see
# services/mission_clear_service.py) -- far shorter than an upload's
# item-by-item handshake, but the readback alone can take ~25s worst case, so
# the client bound must comfortably exceed that.
_MISSION_CLEAR_TIMEOUT_S = 40.0


class CommandSpec(NamedTuple):
    """
    Declarative description of how one command_type reaches its vehicle
    Flask endpoint -- the single place that shape lives, instead of a
    method+path dict plus a separate "needs a body" set plus a separate
    execution function to keep in sync with it. `build_body`, when given,
    receives the full command dict (command_id/params/...) and must return
    the JSON body to send; when None (the default, and the case for every
    command type except SET_HOME today) the request carries no body at
    all -- see call_local_endpoint().
    """
    method: str
    path: str
    build_body: Optional[Callable[[dict], dict]] = None
    timeout: float = _DEFAULT_TIMEOUT_S


def _upload_mission_body(command: dict) -> dict:
    """JSON body for POST /agent/upload_mission (see agent_routes.py there):
    command_id plus the requested mission, sent as either a canonical
    `waypoints` list ([{latitude,longitude,loiter_time}, ...]) or the operator
    UI's `geojson` -- whichever the operator command carried in params. The
    vehicle Flask service validates/canonicalizes and verifies it (see
    services/mission_upload_service.py); this only relays the operator's
    request, it does not build or validate the mission itself."""
    params = command.get("params") or {}
    body = {"command_id": command["command_id"]}
    if params.get("waypoints") is not None:
        body["waypoints"] = params["waypoints"]
    elif params.get("geojson") is not None:
        body["geojson"] = params["geojson"]
    # Optional diagnostic/audit metadata (OPERATOR_REPLACEMENT / AGENT_REPLAN)
    # forwarded verbatim when the operator command carried it; the vehicle Flask
    # service treats it as metadata only and never lets it bypass a safety check.
    if params.get("upload_context") is not None:
        body["upload_context"] = params["upload_context"]
    return body


def _clear_mission_body(command: dict) -> dict:
    """JSON body for POST /agent/clear_mission (see agent_routes.py there):
    command_id only. A clear has nothing to configure and deliberately takes
    no force/override parameter -- the vehicle Flask service's armed/AUTO
    refusals are the safety property, not an obstacle to route around."""
    return {"command_id": command["command_id"]}


def _set_home_body(command: dict) -> dict:
    """
    JSON body services/set_home_service.py's POST /agent/set_home needs:
    command_id (echoed back in its response) and mode/tolerance_m/
    freshness_s (the operator backend's queued params) -- see README "Set
    Home" for the full request/response schema.
    """
    params = command.get("params") or {}
    body = {
        "command_id": command["command_id"],
        "mode": params.get("mode", "current_position"),
    }
    if params.get("tolerance_m") is not None:
        body["tolerance_m"] = params["tolerance_m"]
    if params.get("freshness_s") is not None:
        body["freshness_s"] = params["freshness_s"]
    return body


# LOITER's own CommandSpec, held as a single object (not re-declared per key)
# so "LOITER" and "SET_MODE_LOITER" below are provably one endpoint, not a
# parallel path that could silently drift apart. Two keys exist because
# production operator traffic sends "SET_MODE_LOITER" (matching the
# SET_MODE_AUTO/SET_MODE_MANUAL/SET_MODE_HOLD naming convention) while
# "LOITER" is the name this registry, README.md, and the existing test
# suite have documented since LOITER was first added -- rather than pick one
# and break the other caller, both map to the exact same spec/endpoint.
_LOITER_SPEC = CommandSpec("POST", "/nav/loiter")

# command_type -> CommandSpec(method, path, build_body, timeout)
ALLOWED_COMMANDS = {
    "SET_MODE_AUTO":   CommandSpec("POST", "/nav/AutoModeOn"),
    "SET_MODE_MANUAL": CommandSpec("POST", "/nav/manual"),
    "SET_MODE_HOLD":   CommandSpec("POST", "/nav/hold"),
    "LOITER":          _LOITER_SPEC,
    "SET_MODE_LOITER": _LOITER_SPEC,
    "RTL":             CommandSpec("POST", "/nav/rtl"),
    "RETURN_HOME":     CommandSpec("POST", "/nav/rtl"),
    "MISSION_PAUSE":   CommandSpec("POST", "/nav/pause"),
    "MISSION_RESUME":  CommandSpec("POST", "/nav/resume"),
    "ARM":             CommandSpec("POST", "/nav/ArmOn"),
    "DISARM":          CommandSpec("POST", "/nav/Disarm"),
    "SET_HOME":        CommandSpec("POST", "/agent/set_home", build_body=_set_home_body, timeout=_SET_HOME_TIMEOUT_S),
    # MISSION_UPLOAD carries a body (the requested mission) like SET_HOME, and
    # like SET_HOME reaches a dedicated /agent/* route rather than a /nav/*
    # one. It runs on the bounded background worker (mission_upload_worker.py),
    # not the synchronous path, so a long upload never freezes status
    # reporting -- see command_handler/local_agent. Never gated on Home
    # verification (it's an upload, not a Home-relative navigation command).
    "MISSION_UPLOAD":  CommandSpec("POST", "/agent/upload_mission", build_body=_upload_mission_body, timeout=_MISSION_UPLOAD_TIMEOUT_S),
    # MISSION_CLEAR: the Operator Station already exposes this, so Scout must
    # speak it. Reaches the tested, verified mission-clear service (send
    # MISSION_CLEAR_ALL -> optional fresh ack -> complete fresh readback ->
    # prove no route remains), NOT app.py's legacy /nav/clear_mission which
    # reports success from the message merely having been sent. Runs on the
    # synchronous path: a clear is a single short exchange plus one readback,
    # nothing like an upload's full item-by-item handshake. Never gated on
    # Home verification -- clearing a mission is not a Home-relative
    # navigation command. The armed/AUTO refusals live on the Flask side
    # (mission_clear_service._precondition_block).
    "MISSION_CLEAR":   CommandSpec("POST", "/agent/clear_mission", build_body=_clear_mission_body, timeout=_MISSION_CLEAR_TIMEOUT_S),
    # SET_MODE_GUIDED intentionally absent -- no Flask endpoint exists yet
    # for ArduRover GUIDED (custom_mode=15). Falls through to "unsupported".
}

# Command types whose execution requires a runtime-verified Pixhawk Home
# position (see motherpi/services/flask/services/set_home_service.py and
# GET /agent/home_status) -- an old/garage Home could otherwise send the
# USV toward the wrong location under AUTO/RTL/RESUME. The vehicle Flask
# service enforces this same gate independently on /nav/AutoModeOn,
# /nav/rtl, /nav/resume (defense in depth against a caller that bypasses
# the Local Agent entirely) -- checking it here too gives a clean
# "rejected: home unverified" reason before any network call is made,
# rather than surfacing the Flask side's 409 as a generic "failed".
#
# LOITER/SET_MODE_LOITER, SET_MODE_MANUAL, SET_MODE_HOLD, MISSION_PAUSE, ARM,
# DISARM, and SET_HOME are deliberately never in this set. LOITER in
# particular is one of the most important safety commands and must remain available
# regardless of Home verification. SET_HOME must never require Home to
# already be verified -- that would be circular and would permanently block
# the very first Set Home of a deployment.
HOME_VERIFICATION_REQUIRED = {"SET_MODE_AUTO", "RTL", "RETURN_HOME", "MISSION_RESUME"}

# ArduRover custom_mode each mode-changing command_type is meant to actually
# reach -- the Local Agent's own expected end-state, checked against the
# vehicle's *observed* HEARTBEAT.custom_mode (in the normalized result) rather
# than trusting a 200 from the vehicle Flask service. See
# command_normalization.py: a mode command is only ever reported "executed"
# when the vehicle proved verified==true AND observed_mode equals the value
# here.
#
# This mirrors services/mode_verification.ARDUROVER_CUSTOM_MODES on the
# vehicle Flask side (the authoritative name<->int table), duplicated here
# only because the Local Agent must never import from the vehicle Flask
# service package -- they are two processes. MISSION_PAUSE maps to LOITER
# (custom_mode 5), not HOLD: the operator action stays named MISSION_PAUSE but
# the vehicle fallback state is the LOITER safety hold (see README "Mission
# pause/resume").
MODE_COMMAND_EXPECTED = {
    "SET_MODE_AUTO":   ("AUTO", 10),
    "SET_MODE_MANUAL": ("MANUAL", 0),
    "SET_MODE_HOLD":   ("HOLD", 4),
    "LOITER":          ("LOITER", 5),
    "SET_MODE_LOITER": ("LOITER", 5),
    "RTL":             ("RTL", 11),
    "RETURN_HOME":     ("RTL", 11),
    "MISSION_PAUSE":   ("LOITER", 5),
    "MISSION_RESUME":  ("AUTO", 10),
}

# Compact int->name lookup for turning an observed HEARTBEAT.custom_mode back
# into a human-readable observed_state in the normalized result. Only needs to
# cover the modes these command_types can produce plus the neighbours a real
# vehicle might be sitting in; an unrecognized int is surfaced as-is (never
# guessed) by command_normalization.py.
ARDUROVER_MODE_NAMES = {
    0: "MANUAL", 1: "ACRO", 3: "STEERING", 4: "HOLD", 5: "LOITER",
    6: "FOLLOW", 7: "SIMPLE", 10: "AUTO", 11: "RTL", 12: "SMART_RTL",
    15: "GUIDED", 16: "INITIALISING",
}


def is_supported(command_type: str) -> bool:
    return command_type in ALLOWED_COMMANDS


def home_verified() -> bool:
    """
    True only if the vehicle Flask service reports Home as both verified
    (this runtime completed and confirmed a real Set Home operation -- see
    set_home_service.py) and ready. Fails safe to False on any fetch error
    -- an unreachable vehicle Flask service must never be read as
    "verified".
    """
    try:
        status = api_client.get_home_status()
        return bool(status.get("verified")) and bool(status.get("ready_for_auto"))
    except Exception:
        return False


def call_local_endpoint(command: dict, timeout: Optional[float] = None) -> dict:
    """
    Execute an already-validated command by calling its mapped local Flask
    endpoint (ALLOWED_COMMANDS[command["command_type"]]). Caller is
    responsible for expiry/dedup/support/home-verification checks before
    calling this (see command_handler.process_command) -- this is the one
    and only execution path every queued command_type goes through.

    Exactly one branch, driven by the command_type's own declarative spec:
    if its CommandSpec carries a build_body callable (today, only
    SET_HOME), the request is sent with a JSON body built from the full
    command dict; otherwise it's the same bare method+path call every
    command type has always used. Adding a future body-requiring command
    type never needs a new function or a new branch here -- only a new
    build_body referenced in ALLOWED_COMMANDS.

    `timeout` overrides the spec's own configured timeout when given
    (mainly for callers/tests that want a different bound); otherwise each
    command_type's own timeout (CommandSpec.timeout) is used.
    """
    spec = ALLOWED_COMMANDS[command["command_type"]]
    url = f"{LOCAL_FLASK_URL}{spec.path}"
    request_timeout = timeout if timeout is not None else spec.timeout

    if spec.build_body:
        r = requests.request(spec.method, url, json=spec.build_body(command), timeout=request_timeout)
    else:
        r = requests.request(spec.method, url, timeout=request_timeout)
    r.raise_for_status()
    return r.json()
