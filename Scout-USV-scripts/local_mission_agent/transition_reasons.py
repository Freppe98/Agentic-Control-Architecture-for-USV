"""
Concrete, non-generic "why" text for the three kinds of state transition the
Local Agent observes or makes -- communication, mission, and control
authority -- for transition_log.py / agent.decision_reason. Pure functions,
derived from the same data the transition itself was decided from, not
boilerplate like "state changed".
"""

# communication.get_comm_state()'s own decision order is CONNECTED (operator
# reachable) -> DISCONNECTED (no internet) -> PARTITIONED (VPN handshake) ->
# DISCONNECTED (neither) -- these strings describe exactly that logic for
# the transition actually observed, not a re-probe of the network.
_COMM_REASONS = {
    ("CONNECTED", "PARTITIONED"): "Operator endpoint stopped responding; VPN link still active, continuing mission with reduced reporting.",
    ("CONNECTED", "DISCONNECTED"): "Operator endpoint and network both unreachable.",
    ("PARTITIONED", "DISCONNECTED"): "VPN handshake lost while operator endpoint was already unreachable.",
    ("PARTITIONED", "CONNECTED"): "Operator endpoint reachable again after being reachable only via VPN.",
    ("DISCONNECTED", "PARTITIONED"): "VPN handshake established; operator endpoint still unreachable.",
    ("DISCONNECTED", "CONNECTED"): "Operator endpoint reachable again after being fully unreachable.",
}


def comm_transition_reason(from_state: str, to_state: str) -> str:
    return _COMM_REASONS.get(
        (from_state, to_state),
        f"Communication state changed from {from_state} to {to_state}.",
    )


def mission_transition_reason(to_state: str, waypoint, count, mission_id) -> str:
    if to_state == "IDLE":
        return "No mission assigned by the vehicle."
    if to_state == "WAITING":
        return f"Mission {mission_id!r} uploaded but not yet started." if mission_id else "No mission active."
    if to_state == "ERROR":
        return "Vehicle mission status unavailable (Flask process or MAVLink bridge unreachable)."
    if to_state == "TRANSIT":
        if waypoint is not None and count is not None:
            return f"Mission activated; heading to first waypoint ({waypoint}/{count})."
        return "Mission activated; waypoint progress not yet reported."
    if to_state == "SEARCH":
        return f"Waypoint {waypoint}/{count} reached; continuing search pattern."
    if to_state == "RETURN":
        return f"Final waypoint reached ({waypoint}/{count}); returning to base."
    return f"Mission phase changed to {to_state}."


def authority_transition_reason(vehicle_agent_block: dict, from_authority: str, to_authority: str) -> str:
    """
    The Local Agent only ever observes control_authority, it never sets it --
    so the real "why" has to come from whoever called
    POST /agent/control_authority on the vehicle Flask service (see
    services/control_authority.py.set_authority's `reason` argument). Falls
    back to an honest "not reported" rather than guessing operator intent.
    """
    last_transition = (vehicle_agent_block or {}).get("control_authority_last_transition")
    if (
        isinstance(last_transition, dict)
        and last_transition.get("from") == from_authority
        and last_transition.get("to") == to_authority
        and last_transition.get("reason")
    ):
        return last_transition["reason"]
    return f"Control authority changed from {from_authority} to {to_authority} (reason not reported by vehicle service)."
