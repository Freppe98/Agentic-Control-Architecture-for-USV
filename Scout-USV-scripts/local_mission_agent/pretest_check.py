"""
Pre-test readiness printout for a practical communication-degradation
mission test (see README "Practical comm-degradation test"). Read-only:
builds decision_inputs/decide()/confidence() exactly the way local_agent.py's
main loop does for one iteration, via the same real GET /agent/state and
GET /agent/control_authority reads -- no /nav/* call, no mission upload, no
authority write, nothing here can move the vehicle. What this prints is
guaranteed consistent with what the running Local Agent would report,
because it's the same decision_engine functions on the same evidence, not a
separate summary.

    python3 pretest_check.py
"""
import sys

import communication
import decision_engine
from api_client import get_vehicle_state, get_control_authority
from state_machine import MissionRunner


def _yn(value):
    if value is None:
        return "UNKNOWN"
    return "YES" if value else "NO"


def main() -> int:
    print("=== Scout Local Agent -- pre-test check ===")

    comm_state = communication.get_comm_state()
    print(f"Communication state:    {comm_state}")
    print(f"Operator reachable:     {_yn(comm_state == 'CONNECTED')}")

    try:
        vehicle_state = get_vehicle_state()
    except Exception as e:
        print(f"Vehicle Flask API:      UNREACHABLE ({e})")
        print("\nCannot read telemetry/mission/authority -- fix this before testing.")
        return 1

    telemetry = vehicle_state.get("telemetry", {}) or {}
    mavlink = vehicle_state.get("mavlink", {}) or {}
    vs_mission = vehicle_state.get("mission", {}) or {}

    try:
        authority = get_control_authority()
    except Exception as e:
        authority = None
        print(f"Control authority:      UNKNOWN (vehicle Flask API unreachable for this call: {e})")

    mission_runner = MissionRunner()
    mission_state = mission_runner.update(vs_mission)

    inputs = decision_engine.build_decision_inputs(
        vehicle_state, comm_state, mission_state, mission_runner, authority or "OPERATOR",
    )
    decision, reason = decision_engine.decide(inputs)
    confidence, missing = decision_engine.confidence(inputs)

    print(f"Pixhawk connected:      {_yn(mavlink.get('mavlink_connected'))}")
    print(f"Heartbeat age (s):      {mavlink.get('heartbeat_age_s')}")
    print(f"MAVLink message age (s):{mavlink.get('mavlink_last_msg_age_s')}")
    print(f"GPS fix type:           {telemetry.get('gps_fix_type')} (satellites={telemetry.get('gps_satellites')})")
    print(f"Vehicle mode:           {telemetry.get('mode_name')}")
    print(f"Armed:                  {_yn(telemetry.get('armed'))}")
    raw_battery = telemetry.get("battery")
    print(f"Battery raw value:      {raw_battery!r}")
    print(f"Battery available:      {_yn(inputs['battery_percent'] is not None)} (normalized={inputs['battery_percent']})")
    print(f"Mission loaded:         {_yn(bool(mission_runner.mission_id))} (id={mission_runner.mission_id!r})")
    print(f"Mission count:          {vs_mission.get('mission_count')}")
    print(f"Current waypoint:       {vs_mission.get('current_waypoint')}")
    if authority is not None:
        print(f"Control authority:      {authority}")
    print(f"Current decision:       {decision}")
    print(f"Decision reason:        {reason}")
    print(f"Decision confidence:    {confidence} (missing={missing})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
