"""
Rolling audit trail of real state transitions this Local Agent has observed
or made -- communication, mission, and control-authority changes -- each
with the concrete trigger that caused it, not a generic description.

Distinct from the `events` list built up in local_agent.py's main loop:
`events` is a short-lived human-readable feed capped at MAX_LOCAL_EVENTS and
cleared every time a status send succeeds (see local_agent.py), so the
operator only ever sees "what's new". This module is the complementary
persistent record -- it is never cleared, so every poll of GET /agent/state
(via the status message's `transitions` field) replays the same bounded
history, which is what lets the operator backend reconstruct "why is the
agent in the state it's in" after reconnecting, not just "what just happened".

In-memory only, same lifetime as everything else in this process -- a Local
Agent restart starts a fresh log, same as runtime_status.py's liveness clock.
"""
import time
from collections import deque

MAX_TRANSITIONS = 100

_transitions = deque(maxlen=MAX_TRANSITIONS)

# transition_type -> experiment-recorder event type (task sections 15/16/17/20).
# Every one of these categories already flows through this one function
# (communication/mission/authority/decision from local_agent.py's main loop,
# mission_execution from mission_execution_controller._transition, replan from
# replan_controller._transition, experiment from experiment_injection._record),
# so hooking here gives the recorder authoritative state-change evidence for
# every experiment (final field families: E1 nominal, E2 energy-triggered
# adaptation, E3 communication degradation/loss, E4 operator authority
# takeover) without a second transition log or any change to the
# controllers' own state machines (task section 43).
_RECORDER_EVENT_TYPE = {
    "communication": "COMMUNICATION_STATE_CHANGED",
    "mission": "MISSION_STATE_CHANGED",
    "authority": "CONTROL_AUTHORITY_CHANGED",
    "decision": "AGENT_DECISION_CHANGED",
    "mission_execution": "MISSION_EXECUTION_STATE_CHANGED",
    "replan": "REPLAN_STATE_CHANGED",
    "experiment": "EXPERIMENT_CONDITION_APPLIED",
}


def record_transition(transition_type: str, from_: str, to: str, reason: str,
                      extra: "dict | None" = None) -> dict:
    """
    transition_type: "communication" | "mission" | "authority" -- the kind
    of state this transition belongs to, so the operator can filter/group.
    reason: the actual trigger, e.g. "Heartbeat timeout exceeded" or "Final
    waypoint reached", not a boilerplate "state changed" message.
    extra: optional causal measurements already available in memory at the
    caller's call site (task section 10) -- e.g. operator_reachable,
    buffered_message_count for a communication transition. Forwarded ONLY to
    the experiment recorder's event data, never into the operator-facing
    `entry` below, so this stays additive and never changes the shape of
    GET /agent/state's `transitions` field.
    """
    entry = {
        "timestamp": round(time.time(), 2),
        "type": transition_type,
        "from": from_,
        "to": to,
        "reason": reason,
    }
    _transitions.append(entry)
    _record_to_experiment(transition_type, from_, to, reason, extra)
    return entry


def _record_to_experiment(transition_type: str, from_: str, to: str, reason: str,
                          extra: "dict | None" = None) -> None:
    """Best-effort forward to the experiment recorder, if one is registered
    and a run is active -- see experiment_recorder.py's fail-open contract.
    Never raises, never blocks; a missing/disabled/inactive recorder is the
    normal case outside an experiment run."""
    try:
        import experiment_recording_runtime
        recorder = experiment_recording_runtime.get_recorder()
        if recorder is None:
            return
        event_type = _RECORDER_EVENT_TYPE.get(transition_type, f"{transition_type.upper()}_STATE_CHANGED")
        data = {"from": from_, "to": to, "reason": reason}
        if isinstance(extra, dict):
            # Only whatever the caller actually had on hand -- never
            # fabricated, never a new measurement taken here (task section
            # 10: "if only some fields are available, record only those").
            data.update({k: v for k, v in extra.items() if v is not None})
        recorder.record_event(
            event_type, source=f"transition_log:{transition_type}",
            data=data, priority="high",
        )
    except Exception:
        pass


def get_recent(n: int = MAX_TRANSITIONS) -> list:
    return list(_transitions)[-n:]


def get_recent_by_type(transition_type: str, n: int = MAX_TRANSITIONS) -> list:
    """
    Same rolling log, filtered to one transition_type -- used for
    payload.agent.decision_timeline (type == "decision") so the Agent page
    gets a dedicated field instead of filtering payload.transitions itself.
    """
    return [e for e in _transitions if e["type"] == transition_type][-n:]


def last() -> "dict | None":
    return _transitions[-1] if _transitions else None
