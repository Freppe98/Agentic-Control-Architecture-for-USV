"""
Standalone tests for transition_reasons.py -- the concrete "why" text behind
communication/mission/authority transitions. No pytest dependency:

    python3 test_transition_reasons.py
"""
import unittest

from transition_reasons import (
    comm_transition_reason, mission_transition_reason, authority_transition_reason,
)


class TestCommTransitionReason(unittest.TestCase):
    def test_connected_to_partitioned_names_vpn_still_active(self):
        reason = comm_transition_reason("CONNECTED", "PARTITIONED")
        self.assertIn("VPN", reason)

    def test_connected_to_disconnected_names_both_unreachable(self):
        reason = comm_transition_reason("CONNECTED", "DISCONNECTED")
        self.assertIn("unreachable", reason)

    def test_recovery_to_connected_is_distinguishable_by_prior_state(self):
        from_partitioned = comm_transition_reason("PARTITIONED", "CONNECTED")
        from_disconnected = comm_transition_reason("DISCONNECTED", "CONNECTED")
        self.assertNotEqual(from_partitioned, from_disconnected)

    def test_unmapped_pair_still_returns_a_string_not_crash(self):
        reason = comm_transition_reason("CONNECTED", "CONNECTED")
        self.assertIsInstance(reason, str)


class TestMissionTransitionReason(unittest.TestCase):
    def test_return_reason_cites_actual_waypoint_and_count(self):
        reason = mission_transition_reason("RETURN", 9, 10, "mission-1")
        self.assertIn("9/10", reason)

    def test_search_reason_cites_actual_waypoint(self):
        reason = mission_transition_reason("SEARCH", 4, 10, "mission-1")
        self.assertIn("4/10", reason)

    def test_error_reason_names_the_real_failure_mode(self):
        reason = mission_transition_reason("ERROR", None, None, "mission-1")
        self.assertIn("unavailable", reason)

    def test_waiting_reason_cites_mission_id_when_present(self):
        reason = mission_transition_reason("WAITING", None, None, "mission-42")
        self.assertIn("mission-42", reason)

    def test_idle_reason_when_no_mission(self):
        reason = mission_transition_reason("IDLE", None, None, None)
        self.assertIn("No mission", reason)


class TestAuthorityTransitionReason(unittest.TestCase):
    def test_uses_vehicle_supplied_reason_when_it_matches_the_transition(self):
        agent_block = {
            "control_authority_last_transition": {
                "from": "OPERATOR", "to": "LOCAL_AGENT",
                "reason": "Operator explicitly requested TAKE CONTROL",
            }
        }
        reason = authority_transition_reason(agent_block, "OPERATOR", "LOCAL_AGENT")
        self.assertEqual(reason, "Operator explicitly requested TAKE CONTROL")

    def test_falls_back_honestly_when_vehicle_reports_nothing(self):
        reason = authority_transition_reason({}, "OPERATOR", "LOCAL_AGENT")
        self.assertIn("not reported", reason)

    def test_falls_back_when_stored_transition_is_for_a_different_change(self):
        agent_block = {
            "control_authority_last_transition": {
                "from": "LOCAL_AGENT", "to": "OPERATOR", "reason": "some earlier reason",
            }
        }
        reason = authority_transition_reason(agent_block, "OPERATOR", "LOCAL_AGENT")
        self.assertIn("not reported", reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
