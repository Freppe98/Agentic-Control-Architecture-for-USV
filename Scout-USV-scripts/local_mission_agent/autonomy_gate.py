"""
Gate for a *Local Agent autonomous vehicle-control write* -- a write to the
vehicle Flask service's /nav/*, /agent/set_home, or mission endpoints that
the Local Agent's own decision-making initiates, as opposed to one relayed
from an operator-queued command (see command_handler.py/command_executor.py
for that separate, already-gated path).

Callers today: mission_execution_controller.py's and replan_controller.py's
own `_authorized()` helpers (each: fresh gateway.current_authority() read +
this check, immediately before a write) gate every ARM/LOITER-as-launch-hold/
Set Home/AUTO/upload/set_mission_current/RTL write those two controllers
issue; mission_progression.py uses it to decide whether a progression sample
came from an authorized write. decision_engine.py remains read-only reasoning
(feeds status/reporting only, see its own module docstring) and state_
machine.py in-memory phase bookkeeping with no I/O -- neither calls this.

One narrow, deliberate exception: each controller's own `_ensure_loiter()`
issues LOITER (and only LOITER) as a fail-closed safety hold WITHOUT calling
this gate first -- see that method's own docstring for the full rationale.
It is not evidence that other writes are ungated; every non-LOITER write
above calls this gate immediately before itself, with no exception.

Contract for any future caller:

  1. Call check_autonomous_write_authority(control_authority) immediately
     before *every* write attempt -- never once at startup, never cached
     across iterations or across a mission's lifetime. `control_authority`
     must be freshly read the same way local_agent.py's main loop already
     does for the command-relay path (vehicle_state["agent"]["control_authority"]
     via GET /agent/state, see local_agent._current_authority) -- that
     existing read-every-iteration pattern already satisfies "fresh", so a
     future autonomous writer should reuse the same per-iteration value
     local_agent.py's main loop computes, not add a second cached copy of
     its own.
  2. If the result is not allowed, the write must not be attempted at all
     for that cycle -- try again next cycle with a freshly read authority,
     the same way a rejected operator command would need to be reissued.
  3. Authority transitioning away from LOCAL_AGENT (e.g. an operator
     pressing "Take Control", handled entirely by
     motherpi/services/flask/services/control_authority.py -- the Local
     Agent has no part in that transition and cannot delay or deny it) must
     never be raced: because this gate is re-checked every cycle rather
     than cached, the very next check after such a transition already
     returns not-allowed, so autonomous writes stop within one cycle with
     no extra coordination required.

RC/physical override is deliberately NOT a parameter here. The only RC
signal this codebase has (diagnostics_service._diag_rc_receiver's
RC_CHANNELS presence/freshness check) proves an RC receiver is connected and
broadcasting, not that a human is actively moving the sticks -- treating
"receiver present" as "human is overriding right now" would be wrong (a
receiver can broadcast continuously with the sticks centered). Physical RC
input is observed and reported to the operator (vehicle_state["telemetry"]:
mode/mode_name/armed, and GET /agent/diagnostics: rc_receiver) but
deliberately does not mutate control_authority or gate anything here -- see
README "Authority model" for the full reasoning. A reliable "RC is actively
overriding" signal is a real MAVLink question (e.g. HEARTBEAT.base_mode's
MAV_MODE_FLAG_MANUAL_INPUT_ENABLED bit, cross-checked against non-centered
RC_CHANNELS values) intentionally left as separate future work rather than
guessed at here.
"""
from typing import Tuple

LOCAL_AGENT = "LOCAL_AGENT"


def check_autonomous_write_authority(control_authority: str) -> Tuple[bool, str]:
    """
    Returns (allowed, reason). `allowed` is True only when control_authority
    is exactly "LOCAL_AGENT" -- any other value (including "OPERATOR", an
    unrecognized string, or a caller passing through a failed-fetch fallback)
    fails closed to "not allowed", since an autonomous write must never
    proceed on an authority value it doesn't unambiguously recognize as a
    grant.
    """
    if control_authority != LOCAL_AGENT:
        return False, (
            f"blocked: autonomous vehicle-control write requires LOCAL_AGENT "
            f"control authority (currently {control_authority})"
        )
    return True, "LOCAL_AGENT control authority confirmed"
