"""
Turns the same observations local_agent.py already gathers each loop
iteration (communication, mission, control authority, battery, GPS, MAVLink
link) into a single labeled current_decision + decision_reason for the Agent
page, plus the watch conditions, policy, and confidence behind it.

This is a read-only reasoning/labeling layer -- nothing here calls a /nav/*
endpoint, changes control authority, or issues a command. See
command_executor.py for the only code path that actually writes to the
vehicle; this module only describes what the situation calls for, for a
human reading the Agent page.

Architecture, kept explicit so this stays a Scout-side reasoning layer and
not a UI-summary generator:

    measurements (vehicle_state)
      -> observations   (build_decision_inputs -- raw evidence, no verdicts)
      -> decision        (decide -- current_decision + decision_reason)
      -> reasoning        (build_watch_conditions / confidence / build_policy)
      -> transition history (local_agent.py records a "decision" entry in
                              transition_log.py whenever current_decision
                              changes; never on every iteration)
"""
from state_machine import MissionState
from collectors import policy_for_comm
import config

RETURN_HOME = "Return Home"
CONTINUE_SEARCH = "Continue Search"
CONTINUE_MISSION = "Continue Mission"
PAUSE_MISSION = "Pause Mission"
HOLD_POSITION = "Hold Position"

_ACTIVE_MISSION_STATES = (MissionState.TRANSIT, MissionState.SEARCH, MissionState.RETURN)

_CONFIDENCE_INPUTS = ("battery_percent", "gps_fix_type", "mavlink_connected")


def _normalize_battery_percent(value):
    """
    ArduPilot's BATTERY_STATUS.battery_remaining reports -1 when the
    autopilot has no way to estimate charge (no power module wired up, or
    it's disconnected) -- that is "unavailable", not "0% charged", and a
    -1 must never be compared against the RTL threshold as if it were a
    real reading (it would always read as "below threshold" and force a
    false Return Home). Anything outside the valid 0-100 range is equally
    not a real percentage. A genuine 0% is real and left as 0 -- that is
    the one value in range that *should* still trigger RTL.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if value < 0 or value > 100:
        return None
    return value


def build_decision_inputs(vehicle_state, comm_state, mission_state, mission_runner, control_authority) -> dict:
    """
    Pure evidence, no interpretation -- exactly what decide()/
    build_watch_conditions() below read to reach a decision. Exposed as-is on
    the status payload (payload.agent.decision_inputs) so the Agent page
    never has to trust a UI-computed summary, only the same values the
    decision engine itself used.
    """
    telemetry = vehicle_state.get("telemetry", {}) or {}
    mavlink = vehicle_state.get("mavlink", {}) or {}
    vs_mission = vehicle_state.get("mission", {}) or {}

    return {
        "communication_state": comm_state,
        "operator_reachable": comm_state == "CONNECTED",
        "heartbeat_age_s": mavlink.get("heartbeat_age_s"),
        "last_message_age_s": mavlink.get("mavlink_last_msg_age_s"),
        "mavlink_connected": mavlink.get("mavlink_connected"),
        "battery_percent": _normalize_battery_percent(telemetry.get("battery")),
        "gps_fix_type": telemetry.get("gps_fix_type"),
        "gps_satellites": telemetry.get("gps_satellites"),
        "vehicle_mode": telemetry.get("mode_name"),
        "armed": telemetry.get("armed"),
        "ekf_ok": telemetry.get("ekf_ok"),
        "mission_state": mission_state,
        "mission_id": mission_runner.mission_id,
        "mission_active": vs_mission.get("mission_active"),
        "current_waypoint": vs_mission.get("current_waypoint"),
        "mission_count": vs_mission.get("mission_count"),
        "control_authority": control_authority,
    }


def decide(inputs: dict) -> "tuple[str, str]":
    """
    Priority-ordered rules, first match wins -- vehicle safety (battery/GPS/
    link) outranks mission phase, which outranks control-authority deference.
    Every reason cites the actual evidence value that triggered it, never a
    generic "state changed" message.
    """
    battery = inputs["battery_percent"]
    gps_fix = inputs["gps_fix_type"]
    mission_state = inputs["mission_state"]

    if battery is not None and battery < config.BATTERY_RTL_THRESHOLD_PERCENT:
        return RETURN_HOME, (
            f"Battery at {battery}% is below the {config.BATTERY_RTL_THRESHOLD_PERCENT}% RTL threshold."
        )

    if gps_fix is not None and gps_fix < config.GPS_MIN_FIX_TYPE:
        return HOLD_POSITION, (
            f"GPS fix lost (fix_type={gps_fix}); holding position rather than "
            "navigating without a reliable position estimate."
        )

    if inputs["mavlink_connected"] is False:
        age = inputs["heartbeat_age_s"]
        return HOLD_POSITION, (
            f"MAVLink heartbeat lost ({age}s since last heartbeat); vehicle link unconfirmed."
            if age is not None else
            "MAVLink heartbeat lost; vehicle link unconfirmed."
        )

    if mission_state == MissionState.RETURN:
        wp, count = inputs["current_waypoint"], inputs["mission_count"]
        return RETURN_HOME, (
            f"Final waypoint reached ({wp}/{count}); returning to base."
            if wp is not None and count is not None else
            "Final waypoint reached; returning to base."
        )

    if inputs["mission_active"] is False and inputs["mission_id"] and mission_state != MissionState.ERROR:
        return HOLD_POSITION, (
            f"Mission {inputs['mission_id']!r} completed; standing by for a new mission assignment."
        )

    if inputs["control_authority"] == "OPERATOR" and mission_state in _ACTIVE_MISSION_STATES:
        return PAUSE_MISSION, (
            "Control authority is OPERATOR; Local Agent is standing by and will not "
            "relay mission-affecting commands until authority returns to LOCAL_AGENT."
        )

    if mission_state == MissionState.SEARCH:
        wp, count = inputs["current_waypoint"], inputs["mission_count"]
        return CONTINUE_SEARCH, (
            f"Waypoint {wp}/{count} reached; continuing search pattern."
            if wp is not None and count is not None else
            "Continuing search pattern."
        )

    if mission_state == MissionState.TRANSIT:
        return CONTINUE_MISSION, "Mission activated; heading to first waypoint."

    if mission_state == MissionState.WAITING:
        return HOLD_POSITION, (
            f"Mission {inputs['mission_id']!r} uploaded but not yet started."
            if inputs["mission_id"] else "Mission uploaded but not yet started."
        )

    if mission_state == MissionState.ERROR:
        return HOLD_POSITION, "Vehicle mission status unavailable (Flask process or MAVLink bridge unreachable)."

    return HOLD_POSITION, "No mission assigned; standing by."


def confidence(inputs: dict) -> "tuple[str, list]":
    """
    How complete decide()'s own inputs were -- not a verdict on whether the
    decision is "correct", only on how much evidence backed it. `missing`
    names the actual fields that were None, so the Agent page can show which
    reading is absent instead of just a bare confidence label.
    """
    missing = [name for name in _CONFIDENCE_INPUTS if inputs.get(name) is None]
    if not missing:
        level = "HIGH"
    elif len(missing) < len(_CONFIDENCE_INPUTS):
        level = "MEDIUM"
    else:
        level = "LOW"
    return level, missing


def build_watch_conditions(inputs: dict) -> list:
    """
    The actual transition conditions decide() evaluates, each with the real
    current value and threshold behind it -- not a static description.
    `triggered` is None (not False) when the underlying value is unavailable,
    so "not triggered" is never confused with "unknown".
    """
    battery = inputs["battery_percent"]
    gps_fix = inputs["gps_fix_type"]
    age = inputs["heartbeat_age_s"]
    mission_active = inputs["mission_active"]

    return [
        {
            "condition": "Battery < RTL threshold",
            "metric": "battery_percent",
            "current_value": battery,
            "threshold": config.BATTERY_RTL_THRESHOLD_PERCENT,
            "comparator": "<",
            "triggered": None if battery is None else battery < config.BATTERY_RTL_THRESHOLD_PERCENT,
        },
        {
            "condition": "Heartbeat timeout",
            "metric": "heartbeat_age_s",
            "current_value": age,
            "threshold": config.MAVLINK_HEARTBEAT_TIMEOUT_S,
            "comparator": ">=",
            "triggered": None if age is None else age >= config.MAVLINK_HEARTBEAT_TIMEOUT_S,
        },
        {
            "condition": "GPS lost",
            "metric": "gps_fix_type",
            "current_value": gps_fix,
            "threshold": config.GPS_MIN_FIX_TYPE,
            "comparator": "<",
            "triggered": None if gps_fix is None else gps_fix < config.GPS_MIN_FIX_TYPE,
        },
        {
            "condition": "Mission completed",
            "metric": "mission_active",
            "current_value": mission_active,
            "threshold": False,
            "comparator": "==",
            "triggered": None if mission_active is None else (
                mission_active is False and inputs["mission_id"] is not None
            ),
        },
        {
            "condition": "Operator Take Control",
            "metric": "control_authority",
            "current_value": inputs["control_authority"],
            "threshold": "OPERATOR",
            "comparator": "==",
            "triggered": inputs["control_authority"] == "OPERATOR",
        },
    ]


def build_policy(comm_state, mission_state, control_authority) -> dict:
    if control_authority == "OPERATOR" and mission_state in _ACTIVE_MISSION_STATES:
        mission_policy = "OPERATOR_DIRECTED"
    elif comm_state == "DISCONNECTED":
        mission_policy = "AUTONOMOUS_CONTINUATION_BUFFERED"
    elif comm_state == "PARTITIONED":
        mission_policy = "AUTONOMOUS_CONTINUATION_REDUCED_REPORTING"
    else:
        mission_policy = "SUPERVISED_CONTINUATION"

    return {
        "communication_policy": policy_for_comm(comm_state),
        "mission_policy": mission_policy,
        "autonomy_level": "ASSISTED" if comm_state == "CONNECTED" else "AUTONOMOUS",
        "current_behaviour": "monitoring" if comm_state == "CONNECTED" else "autonomous_continuation",
    }


def build_situation(vehicle_state, comm_state, mission_state, control_authority, autonomy_level, decision_confidence) -> dict:
    """
    Structured pointer into evidence already fetched this iteration for the
    Agent page's "Current Situation" block -- no new computation, no
    invented overall health verdict (that's what GET /agent/diagnostics is
    for). Real, already-fetched fields only.
    """
    telemetry = vehicle_state.get("telemetry", {}) or {}
    mavlink = vehicle_state.get("mavlink", {}) or {}

    return {
        "communication_state": comm_state,
        "operator_reachable": comm_state == "CONNECTED",
        "mission_state": mission_state,
        "control_authority": control_authority,
        "vehicle_health": {
            # Same normalization as build_decision_inputs() -- a raw -1 (or
            # any other out-of-0-100 sentinel) must never surface here as if
            # it were a real percentage; see _normalize_battery_percent().
            "battery_percent": _normalize_battery_percent(telemetry.get("battery")),
            "gps_fix_type": telemetry.get("gps_fix_type"),
            "gps_satellites": telemetry.get("gps_satellites"),
            "ekf_ok": telemetry.get("ekf_ok"),
            "mavlink_connected": mavlink.get("mavlink_connected"),
            "heartbeat_age_s": mavlink.get("heartbeat_age_s"),
            "last_message_age_s": mavlink.get("mavlink_last_msg_age_s"),
            "armed": telemetry.get("armed"),
            "vehicle_mode": telemetry.get("mode_name"),
        },
        "autonomy_level": autonomy_level,
        "decision_confidence": decision_confidence,
    }


def build_mavlink_evidence(vehicle_state: dict) -> dict:
    """
    payload.mavlink, extended: the vehicle Flask side's real link-health
    block (mavlink_connected/heartbeat_age_s/mavlink_last_msg_age_s/
    mavlink_msg_rate_hz/parser_errors/measured_at -- services/mavlink_health.py,
    unchanged, nothing renamed or removed) plus the MAVLink-derived
    vehicle-state fields the Flask side reports under telemetry rather than
    mavlink (gps_fix_type/gps_satellites/vehicle_mode/armed/ekf_ok) and
    last_message_age_s as an explicit alias for mavlink_last_msg_age_s --
    so one evidence block carries every MAVLink-sourced signal relevant to
    a comm-degradation read, not just link timing, without the Agent page
    having to cross-reference telemetry separately.

    Purely additive merge of two blocks already fetched this iteration --
    no extra mavlink2rest/Flask calls, and every value here is the exact
    same read build_decision_inputs() uses. Nothing is inferred from an
    unrelated field (heartbeat is never guessed from GPS presence, etc);
    a field that was never observed stays None here too.
    """
    telemetry = vehicle_state.get("telemetry", {}) or {}
    mavlink = dict(vehicle_state.get("mavlink", {}) or {})
    mavlink.setdefault("mavlink_connected", None)
    mavlink.setdefault("heartbeat_age_s", None)
    mavlink.setdefault("last_message_age_s", mavlink.get("mavlink_last_msg_age_s"))
    mavlink["gps_fix_type"] = telemetry.get("gps_fix_type")
    mavlink["gps_satellites"] = telemetry.get("gps_satellites")
    mavlink["vehicle_mode"] = telemetry.get("mode_name")
    mavlink["armed"] = telemetry.get("armed")
    mavlink["ekf_ok"] = telemetry.get("ekf_ok")
    return mavlink
