"""
Central place that turns a raw vehicle-Flask response into a stable,
*normalized* command outcome -- the one definition of "did the vehicle
actually do what the command asked", shared by every command_type that has a
verifiable end state.

Why this exists: a 2xx from the vehicle Flask service only proves the request
was received and handled without an HTTP error. It is NOT proof the vehicle
performed the action. A mode change can be ACKed and then immediately
reverted by an RC override or a failsafe; an arm request can be rejected by a
pre-arm check while the HTTP call still returns 200. `command_handler.py`
previously reported every 2xx as `status: executed`, conflating "the endpoint
answered" with "the mission mode / armed state is now what we asked for".

This module closes that gap for the command_types whose success is
verifiable:

  * MODE commands (SET_MODE_AUTO / SET_MODE_MANUAL / SET_MODE_HOLD /
    SET_MODE_LOITER / LOITER / RTL / RETURN_HOME / MISSION_PAUSE /
    MISSION_RESUME) -- the vehicle Flask `/nav/*` routes return
    mode_verification.set_mode_and_verify()'s
    accepted/verified/observed_mode. A mode command is only `executed` when
    accepted AND verified AND observed_mode equals the custom_mode this
    command_type was meant to reach (command_executor.MODE_COMMAND_EXPECTED)
    -- not merely the mode Flask *thinks* it requested, so a wrong-mode bug on
    the Flask side can never read back as success here.

  * ARM / DISARM -- the vehicle Flask routes return arm_verification's
    accepted/verified/armed/expected_armed. Success requires the final,
    freshly-read armed state to match what was asked.

Returned contract (stable across every normalized command_type):

    accepted       -- the vehicle accepted/began the action (reached the
                      requested mode at least once, or ACKed the arm).
    executed       -- the action actually took effect and held: this is the
                      single field command_handler.py keys the terminal
                      status off (executed -> "executed", else "failed").
    verified       -- proven via fresh vehicle evidence (HEARTBEAT
                      custom_mode / base_mode), never from HTTP success or an
                      ACK alone. For these command_types verified == executed.
    expected_state -- what the vehicle was supposed to end up in
                      ("AUTO"/"LOITER"/... or "ARMED"/"DISARMED").
    observed_state -- what it was actually observed in (same vocabulary), or
                      None if that could not be read.
    error          -- human-readable reason it is not executed, or None.

Every field the raw response already carried (observed_mode, ack_result,
samples, requested_mode, reason, message, ...) is preserved alongside these
so existing operator tooling that reads them keeps working -- the normalized
keys are layered on top, never a replacement that drops information.

command_handler.py attaches the command `lifecycle` to the result block it
builds from this outcome, so the persisted/returned result carries
lifecycle too.

Pass-through command_types (SET_HOME, and anything else with no single
verifiable end state) are deliberately NOT handled here -- command_handler.py
keeps their existing contract (SET_HOME already returns its own
accepted/verified/error block, whose semantics the operator station depends
on). is_normalized() is the one predicate that decides which path a
command_type takes.
"""
import command_executor

# Expected final armed state per command_type. ARM must end ARMED, DISARM must
# end DISARMED -- verified against a fresh HEARTBEAT base_mode read, never
# from the request being sent.
_ARM_EXPECTED = {"ARM": True, "DISARM": False}


def is_normalized(command_type: str) -> bool:
    """True for command_types this module produces a verified normalized
    outcome for (mode changes + ARM/DISARM + MISSION_UPLOAD + MISSION_CLEAR).
    command_handler.py routes only these through normalize(); everything else
    keeps its existing pass-through result contract."""
    return (command_type in command_executor.MODE_COMMAND_EXPECTED
            or command_type in _ARM_EXPECTED
            or command_type in ("MISSION_UPLOAD", "MISSION_CLEAR"))


def _error_text(raw: dict):
    """Human-readable error out of whatever shape the raw response used --
    an {"code","message"} dict (set_home/arm style), a bare string, or the
    mode routes' `reason`. None when nothing indicates an error."""
    err = raw.get("error")
    if isinstance(err, dict):
        return err.get("message") or err.get("code")
    if err:
        return str(err)
    return raw.get("reason")


def _mode_name(custom_mode):
    if custom_mode is None:
        return None
    return command_executor.ARDUROVER_MODE_NAMES.get(custom_mode, custom_mode)


def _normalize_mode(command_type: str, raw: dict) -> dict:
    name, code = command_executor.MODE_COMMAND_EXPECTED[command_type]
    accepted = bool(raw.get("accepted"))
    observed_mode = raw.get("observed_mode")
    mode_correct = observed_mode == code
    # verified only if the Flask side proved it verified AND the mode it
    # settled in is the one THIS command_type intended -- guards against a
    # Flask route that verified the wrong mode.
    verified = bool(raw.get("verified")) and mode_correct
    executed = verified

    error = None
    if not executed:
        error = _error_text(raw) or (
            f"{name} not confirmed: expected custom_mode={code}, "
            f"observed={observed_mode} (accepted={accepted}, "
            f"flask_verified={bool(raw.get('verified'))})"
        )

    out = dict(raw)
    out.update({
        "accepted": accepted,
        "executed": executed,
        "verified": verified,
        "expected_state": name,
        "observed_state": _mode_name(observed_mode),
        "error": error,
    })
    return out


def _normalize_arm(command_type: str, raw: dict) -> dict:
    expected_armed = _ARM_EXPECTED[command_type]
    expected_state = "ARMED" if expected_armed else "DISARMED"
    armed = raw.get("armed")
    observed_state = None if armed is None else ("ARMED" if armed else "DISARMED")
    accepted = bool(raw.get("accepted"))
    armed_correct = armed is not None and bool(armed) == expected_armed
    verified = bool(raw.get("verified")) and armed_correct
    executed = verified

    error = None
    if not executed:
        error = _error_text(raw) or (
            f"{command_type} not confirmed: expected {expected_state}, "
            f"observed armed={armed} (accepted={accepted}, "
            f"flask_verified={bool(raw.get('verified'))})"
        )

    out = dict(raw)
    out.update({
        "accepted": accepted,
        "executed": executed,
        "verified": verified,
        "expected_state": expected_state,
        "observed_state": observed_state,
        "error": error,
    })
    return out


def _normalize_upload(raw: dict) -> dict:
    """MISSION_UPLOAD: the vehicle Flask upload service returns accepted/
    uploaded/verified (see services/mission_upload_service.py). Success is
    `verified` -- the mission proven present via a fresh readback (count +
    content hash), never merely `uploaded` (items sent + acked)."""
    accepted = bool(raw.get("accepted"))
    verified = bool(raw.get("verified"))
    executed = verified
    error = None if executed else (
        _error_text(raw)
        or f"mission upload not verified (accepted={accepted}, uploaded={bool(raw.get('uploaded'))})"
    )
    out = dict(raw)
    out.update({
        "accepted": accepted,
        "executed": executed,
        "verified": verified,
        "expected_state": "MISSION_UPLOADED",
        "observed_state": "MISSION_UPLOADED" if verified else None,
        "error": error,
    })
    return out


def _normalize_clear(raw: dict) -> dict:
    """MISSION_CLEAR: the vehicle Flask clear service returns accepted/
    cleared/verified (see services/mission_clear_service.py). Success is
    `verified` -- a complete fresh readback proved no route remains on the
    vehicle -- never `cleared`, which only means MISSION_CLEAR_ALL was sent
    and not explicitly rejected. Reporting success from the send is precisely
    what the legacy /nav/clear_mission route did wrong."""
    accepted = bool(raw.get("accepted"))
    verified = bool(raw.get("verified"))
    executed = verified
    error = None if executed else (
        _error_text(raw)
        or f"mission clear not verified (accepted={accepted}, cleared={bool(raw.get('cleared'))})"
    )
    out = dict(raw)
    out.update({
        "accepted": accepted,
        "executed": executed,
        "verified": verified,
        "expected_state": "MISSION_EMPTY",
        "observed_state": "MISSION_EMPTY" if verified else None,
        "error": error,
    })
    return out


def normalize(command_type: str, raw) -> dict:
    """Normalize one raw vehicle-Flask response for a verifiable command_type.
    Never raises; a non-dict raw is treated as "no evidence" (nothing
    verified). Only call for command_types where is_normalized() is True."""
    raw = raw if isinstance(raw, dict) else {}
    if command_type in command_executor.MODE_COMMAND_EXPECTED:
        return _normalize_mode(command_type, raw)
    if command_type in _ARM_EXPECTED:
        return _normalize_arm(command_type, raw)
    if command_type == "MISSION_UPLOAD":
        return _normalize_upload(raw)
    if command_type == "MISSION_CLEAR":
        return _normalize_clear(raw)
    # Defensive: should be unreachable (is_normalized() gates callers). Never
    # fabricate success for an unknown shape.
    out = dict(raw)
    out.setdefault("accepted", bool(raw.get("accepted")))
    out.setdefault("verified", bool(raw.get("verified")))
    out.setdefault("executed", bool(raw.get("verified")))
    out.setdefault("expected_state", None)
    out.setdefault("observed_state", None)
    out.setdefault("error", _error_text(raw))
    return out
