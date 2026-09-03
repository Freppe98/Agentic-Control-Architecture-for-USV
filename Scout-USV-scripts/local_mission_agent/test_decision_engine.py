"""
Standalone tests for decision_engine.py -- current_decision/decision_reason,
watch_conditions, confidence, and policy. No pytest dependency:

    python3 test_decision_engine.py
"""
import unittest

import config
import decision_engine as de
from state_machine import MissionState


def _inputs(**overrides):
    base = {
        "communication_state": "CONNECTED",
        "operator_reachable": True,
        "heartbeat_age_s": 0.4,
        "mavlink_connected": True,
        "battery_percent": 80,
        "gps_fix_type": 3,
        "gps_satellites": 12,
        "vehicle_mode": "AUTO",
        "armed": True,
        "mission_state": MissionState.SEARCH,
        "mission_id": "mission-1",
        "mission_active": True,
        "current_waypoint": 4,
        "mission_count": 10,
        "control_authority": "LOCAL_AGENT",
    }
    base.update(overrides)
    return base


class TestDecide(unittest.TestCase):
    def test_low_battery_triggers_return_home(self):
        decision, reason = de.decide(_inputs(battery_percent=20))
        self.assertEqual(decision, de.RETURN_HOME)
        self.assertIn("20%", reason)
        self.assertIn(str(config.BATTERY_RTL_THRESHOLD_PERCENT), reason)

    def test_battery_at_threshold_does_not_trigger(self):
        decision, _ = de.decide(_inputs(battery_percent=config.BATTERY_RTL_THRESHOLD_PERCENT))
        self.assertNotEqual(decision, de.RETURN_HOME)

    def test_gps_lost_triggers_hold_position(self):
        decision, reason = de.decide(_inputs(gps_fix_type=0))
        self.assertEqual(decision, de.HOLD_POSITION)
        self.assertIn("fix_type=0", reason)

    def test_mavlink_disconnected_triggers_hold_position(self):
        decision, reason = de.decide(_inputs(mavlink_connected=False, heartbeat_age_s=12.0))
        self.assertEqual(decision, de.HOLD_POSITION)
        self.assertIn("12.0s", reason)

    def test_battery_outranks_gps_and_link(self):
        decision, _ = de.decide(_inputs(battery_percent=10, gps_fix_type=0, mavlink_connected=False))
        self.assertEqual(decision, de.RETURN_HOME)

    def test_mission_return_state_triggers_return_home(self):
        decision, reason = de.decide(_inputs(mission_state=MissionState.RETURN, current_waypoint=9, mission_count=10))
        self.assertEqual(decision, de.RETURN_HOME)
        self.assertIn("9/10", reason)

    def test_mission_completed_triggers_hold_position(self):
        decision, reason = de.decide(_inputs(mission_active=False, mission_id="mission-1", mission_state=MissionState.IDLE))
        self.assertEqual(decision, de.HOLD_POSITION)
        self.assertIn("mission-1", reason)

    def test_operator_authority_pauses_active_mission(self):
        decision, reason = de.decide(_inputs(control_authority="OPERATOR", mission_state=MissionState.SEARCH, mission_active=True))
        self.assertEqual(decision, de.PAUSE_MISSION)
        self.assertIn("OPERATOR", reason)

    def test_operator_authority_with_no_active_mission_does_not_pause(self):
        decision, _ = de.decide(_inputs(control_authority="OPERATOR", mission_state=MissionState.IDLE, mission_active=None, mission_id=None))
        self.assertNotEqual(decision, de.PAUSE_MISSION)

    def test_search_state_continues_search(self):
        decision, reason = de.decide(_inputs(mission_state=MissionState.SEARCH, current_waypoint=4, mission_count=10))
        self.assertEqual(decision, de.CONTINUE_SEARCH)
        self.assertIn("4/10", reason)

    def test_transit_state_continues_mission(self):
        decision, _reason = de.decide(_inputs(mission_state=MissionState.TRANSIT, mission_active=True))
        self.assertEqual(decision, de.CONTINUE_MISSION)

    def test_waiting_state_holds_position(self):
        decision, reason = de.decide(_inputs(mission_state=MissionState.WAITING, mission_active=False, mission_id="mission-2"))
        self.assertEqual(decision, de.HOLD_POSITION)
        self.assertIn("mission-2", reason)

    def test_error_state_holds_position(self):
        decision, reason = de.decide(_inputs(mission_state=MissionState.ERROR, mission_active=None))
        self.assertEqual(decision, de.HOLD_POSITION)
        self.assertIn("unavailable", reason)

    def test_idle_no_mission_holds_position(self):
        decision, _ = de.decide(_inputs(mission_state=MissionState.IDLE, mission_active=None, mission_id=None))
        self.assertEqual(decision, de.HOLD_POSITION)


class TestConfidence(unittest.TestCase):
    def test_all_present_is_high(self):
        level, missing = de.confidence(_inputs())
        self.assertEqual(level, "HIGH")
        self.assertEqual(missing, [])

    def test_one_missing_is_medium(self):
        level, missing = de.confidence(_inputs(battery_percent=None))
        self.assertEqual(level, "MEDIUM")
        self.assertIn("battery_percent", missing)

    def test_all_missing_is_low(self):
        level, missing = de.confidence(_inputs(battery_percent=None, gps_fix_type=None, mavlink_connected=None))
        self.assertEqual(level, "LOW")
        self.assertEqual(len(missing), 3)


class TestWatchConditions(unittest.TestCase):
    def test_battery_condition_triggered(self):
        conditions = de.build_watch_conditions(_inputs(battery_percent=10))
        battery_cond = next(c for c in conditions if c["condition"] == "Battery < RTL threshold")
        self.assertTrue(battery_cond["triggered"])

    def test_battery_condition_unknown_when_missing(self):
        conditions = de.build_watch_conditions(_inputs(battery_percent=None))
        battery_cond = next(c for c in conditions if c["condition"] == "Battery < RTL threshold")
        self.assertIsNone(battery_cond["triggered"])

    def test_operator_take_control_triggered(self):
        conditions = de.build_watch_conditions(_inputs(control_authority="OPERATOR"))
        cond = next(c for c in conditions if c["condition"] == "Operator Take Control")
        self.assertTrue(cond["triggered"])

    def test_mission_completed_triggered(self):
        conditions = de.build_watch_conditions(_inputs(mission_active=False, mission_id="mission-1"))
        cond = next(c for c in conditions if c["condition"] == "Mission completed")
        self.assertTrue(cond["triggered"])

    def test_all_five_conditions_present(self):
        names = {c["condition"] for c in de.build_watch_conditions(_inputs())}
        self.assertEqual(names, {
            "Battery < RTL threshold", "Heartbeat timeout", "GPS lost",
            "Mission completed", "Operator Take Control",
        })


class _FakeRunner:
    mission_id = "mission-1"


def _vehicle_state(battery, **overrides):
    telemetry = {
        "battery": battery, "gps_fix_type": 3, "gps_satellites": 14,
        "mode_name": "AUTO", "armed": True, "ekf_ok": True,
    }
    mavlink = {
        "mavlink_connected": True, "heartbeat_age_s": 0.4,
        "mavlink_last_msg_age_s": 0.1, "mavlink_msg_rate_hz": 1.0,
        "parser_errors": None, "measured_at": 1000.0,
    }
    mission = {"mission_active": True, "current_waypoint": 4, "mission_count": 10}
    state = {"telemetry": telemetry, "mavlink": mavlink, "mission": mission}
    state.update(overrides)
    return state


class TestBatteryNormalization(unittest.TestCase):
    """
    Priority 1 regression coverage: ArduPilot reports battery_remaining as
    -1 when the power module is disconnected/no charge estimate is
    available -- that must normalize to None (unavailable), never be read
    as a real 0-100% value, and must never trigger the RTL threshold or be
    described as "below threshold" in decision_reason.
    """

    def test_negative_one_is_unavailable_not_low(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(-1), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        self.assertIsNone(inputs["battery_percent"])

        decision, reason = de.decide(inputs)
        self.assertNotEqual(decision, de.RETURN_HOME)
        self.assertNotIn("below", reason.lower())
        self.assertNotIn("battery", reason.lower())

        battery_cond = next(
            c for c in de.build_watch_conditions(inputs) if c["condition"] == "Battery < RTL threshold"
        )
        self.assertIsNone(battery_cond["current_value"])
        self.assertIsNone(battery_cond["triggered"])

    def test_null_battery_is_unavailable(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(None), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        self.assertIsNone(inputs["battery_percent"])
        decision, _ = de.decide(inputs)
        self.assertNotEqual(decision, de.RETURN_HOME)

    def test_zero_percent_is_real_and_triggers_rtl(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(0), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        self.assertEqual(inputs["battery_percent"], 0)
        decision, reason = de.decide(inputs)
        self.assertEqual(decision, de.RETURN_HOME)
        self.assertIn("0%", reason)

    def test_valid_low_battery_triggers_rtl(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(15), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        self.assertEqual(inputs["battery_percent"], 15)
        decision, reason = de.decide(inputs)
        self.assertEqual(decision, de.RETURN_HOME)
        self.assertIn("15%", reason)

    def test_valid_healthy_battery_does_not_trigger_rtl(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(68), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        self.assertEqual(inputs["battery_percent"], 68)
        decision, _ = de.decide(inputs)
        self.assertNotEqual(decision, de.RETURN_HOME)

    def test_out_of_range_high_value_is_unavailable(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(150), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        self.assertIsNone(inputs["battery_percent"])

    def test_situation_vehicle_health_uses_same_normalization(self):
        situation = de.build_situation(
            _vehicle_state(-1), "CONNECTED", MissionState.SEARCH, "LOCAL_AGENT", "ASSISTED", "HIGH",
        )
        self.assertIsNone(situation["vehicle_health"]["battery_percent"])

    def test_confidence_treats_unavailable_battery_as_missing(self):
        inputs = de.build_decision_inputs(
            _vehicle_state(-1), "CONNECTED", MissionState.SEARCH, _FakeRunner(), "LOCAL_AGENT",
        )
        level, missing = de.confidence(inputs)
        self.assertIn("battery_percent", missing)
        self.assertNotEqual(level, "HIGH")


class TestMavlinkEvidence(unittest.TestCase):
    """Priority 2: payload.mavlink gains real, already-fetched vehicle-state
    fields (gps/mode/armed/ekf/last_message_age_s), additive only -- nothing
    already in the vehicle Flask side's mavlink block is renamed/removed."""

    def test_merges_telemetry_derived_fields_into_mavlink_block(self):
        evidence = de.build_mavlink_evidence(_vehicle_state(68))
        self.assertTrue(evidence["mavlink_connected"])
        self.assertEqual(evidence["heartbeat_age_s"], 0.4)
        self.assertEqual(evidence["last_message_age_s"], 0.1)
        self.assertEqual(evidence["gps_fix_type"], 3)
        self.assertEqual(evidence["gps_satellites"], 14)
        self.assertEqual(evidence["vehicle_mode"], "AUTO")
        self.assertTrue(evidence["armed"])
        self.assertTrue(evidence["ekf_ok"])
        # original link-timing fields are preserved, not dropped
        self.assertEqual(evidence["mavlink_msg_rate_hz"], 1.0)
        self.assertIsNone(evidence["parser_errors"])

    def test_missing_mavlink_block_still_yields_real_telemetry_fields(self):
        state = _vehicle_state(68, mavlink={})
        evidence = de.build_mavlink_evidence(state)
        self.assertIsNone(evidence["mavlink_connected"])
        self.assertIsNone(evidence["heartbeat_age_s"])
        self.assertEqual(evidence["gps_fix_type"], 3)
        self.assertEqual(evidence["vehicle_mode"], "AUTO")

    def test_never_infers_heartbeat_from_gps(self):
        # GPS/telemetry present and healthy, but mavlink link block reports
        # no heartbeat at all -- mavlink_connected must stay None, not be
        # inferred True just because GPS/telemetry look fine.
        state = _vehicle_state(68, mavlink={"heartbeat_age_s": None, "mavlink_connected": None})
        evidence = de.build_mavlink_evidence(state)
        self.assertIsNone(evidence["mavlink_connected"])
        self.assertEqual(evidence["gps_fix_type"], 3)


class TestBuildPolicy(unittest.TestCase):
    def test_operator_directed_when_authority_operator_and_mission_active(self):
        policy = de.build_policy("CONNECTED", MissionState.SEARCH, "OPERATOR")
        self.assertEqual(policy["mission_policy"], "OPERATOR_DIRECTED")

    def test_autonomous_continuation_when_disconnected(self):
        policy = de.build_policy("DISCONNECTED", MissionState.SEARCH, "LOCAL_AGENT")
        self.assertEqual(policy["mission_policy"], "AUTONOMOUS_CONTINUATION_BUFFERED")
        self.assertEqual(policy["autonomy_level"], "AUTONOMOUS")

    def test_supervised_continuation_when_connected_and_local_agent(self):
        policy = de.build_policy("CONNECTED", MissionState.SEARCH, "LOCAL_AGENT")
        self.assertEqual(policy["mission_policy"], "SUPERVISED_CONTINUATION")
        self.assertEqual(policy["autonomy_level"], "ASSISTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
