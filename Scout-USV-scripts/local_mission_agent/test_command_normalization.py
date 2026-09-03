"""
Standalone tests for command_normalization.py -- the one place a raw vehicle
Flask response is turned into a verified normalized outcome. No pytest
dependency:

    python3 test_command_normalization.py

The whole point of this module is that a 2xx from the vehicle Flask service is
NOT proof the vehicle did the thing, so every test here asserts the mapping
from raw evidence to accepted/executed/verified/expected_state/observed_state/
error rather than trusting HTTP success.
"""
import unittest

import command_normalization as cn


class TestIsNormalized(unittest.TestCase):
    def test_mode_and_arm_commands_are_normalized(self):
        for ct in ("SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_HOLD",
                   "SET_MODE_LOITER", "LOITER", "RTL", "RETURN_HOME",
                   "MISSION_PAUSE", "MISSION_RESUME", "ARM", "DISARM",
                   "MISSION_UPLOAD"):
            self.assertTrue(cn.is_normalized(ct), ct)

    def test_set_home_and_unknown_are_not_normalized(self):
        self.assertFalse(cn.is_normalized("SET_HOME"))
        self.assertFalse(cn.is_normalized("CLEAR_MISSION"))


class TestModeNormalization(unittest.TestCase):
    def test_verified_correct_mode_is_executed(self):
        out = cn.normalize("SET_MODE_AUTO",
                           {"accepted": True, "verified": True, "observed_mode": 10, "reason": None})
        self.assertTrue(out["accepted"])
        self.assertTrue(out["verified"])
        self.assertTrue(out["executed"])
        self.assertEqual(out["expected_state"], "AUTO")
        self.assertEqual(out["observed_state"], "AUTO")
        self.assertIsNone(out["error"])

    def test_accepted_but_not_verified_is_not_executed(self):
        """Reached the mode once then reverted -- a transient blip must never
        read back as a successful vehicle action."""
        out = cn.normalize("RTL",
                           {"accepted": True, "verified": False, "observed_mode": 0,
                            "reason": "entered RTL but reverted to custom_mode=0"})
        self.assertTrue(out["accepted"])
        self.assertFalse(out["verified"])
        self.assertFalse(out["executed"])
        self.assertEqual(out["expected_state"], "RTL")
        self.assertEqual(out["observed_state"], "MANUAL")
        self.assertIn("reverted", out["error"])

    def test_flask_verified_but_wrong_observed_mode_is_not_executed(self):
        """Even if the Flask side claims verified, a mode that isn't the one
        THIS command_type was meant to reach is a failure -- guards against a
        wrong-mode Flask bug being trusted."""
        out = cn.normalize("SET_MODE_HOLD",
                           {"accepted": True, "verified": True, "observed_mode": 5, "reason": None})
        self.assertFalse(out["verified"])
        self.assertFalse(out["executed"])
        self.assertEqual(out["expected_state"], "HOLD")
        self.assertEqual(out["observed_state"], "LOITER")
        self.assertIn("HOLD not confirmed", out["error"])

    def test_never_reached_mode_is_not_executed(self):
        out = cn.normalize("SET_MODE_LOITER",
                           {"accepted": False, "verified": False, "observed_mode": 0,
                            "reason": "never reported custom_mode=5"})
        self.assertFalse(out["accepted"])
        self.assertFalse(out["executed"])
        self.assertIn("never reported", out["error"])

    def test_mission_pause_expects_loiter_not_hold(self):
        """MISSION_PAUSE's verified vehicle state is LOITER (5), not HOLD."""
        ok = cn.normalize("MISSION_PAUSE",
                          {"accepted": True, "verified": True, "observed_mode": 5})
        self.assertTrue(ok["executed"])
        self.assertEqual(ok["expected_state"], "LOITER")

        hold = cn.normalize("MISSION_PAUSE",
                            {"accepted": True, "verified": True, "observed_mode": 4})
        self.assertFalse(hold["executed"])

    def test_raw_fields_are_preserved(self):
        out = cn.normalize("SET_MODE_HOLD",
                           {"accepted": True, "verified": True, "observed_mode": 4,
                            "ack_result": "MAV_RESULT_ACCEPTED", "samples": [4, 4],
                            "requested_mode": "HOLD", "message": "custom"})
        self.assertEqual(out["ack_result"], "MAV_RESULT_ACCEPTED")
        self.assertEqual(out["samples"], [4, 4])
        self.assertEqual(out["message"], "custom")

    def test_non_dict_raw_is_never_success(self):
        out = cn.normalize("SET_MODE_AUTO", None)
        self.assertFalse(out["executed"])
        self.assertFalse(out["verified"])


class TestArmNormalization(unittest.TestCase):
    def test_arm_verified_armed_is_executed(self):
        out = cn.normalize("ARM",
                           {"accepted": True, "verified": True, "armed": True,
                            "expected_armed": True, "ack_result": "MAV_RESULT_ACCEPTED", "error": None})
        self.assertTrue(out["executed"])
        self.assertEqual(out["expected_state"], "ARMED")
        self.assertEqual(out["observed_state"], "ARMED")
        self.assertIsNone(out["error"])

    def test_disarm_verified_disarmed_is_executed(self):
        out = cn.normalize("DISARM",
                           {"accepted": True, "verified": True, "armed": False,
                            "expected_armed": False, "error": None})
        self.assertTrue(out["executed"])
        self.assertEqual(out["expected_state"], "DISARMED")
        self.assertEqual(out["observed_state"], "DISARMED")

    def test_arm_rejected_is_not_executed(self):
        out = cn.normalize("ARM",
                           {"accepted": False, "verified": False, "armed": False,
                            "ack_result": "MAV_RESULT_FAILED",
                            "error": {"code": "ACK_REJECTED", "message": "prearm check failed"}})
        self.assertFalse(out["executed"])
        self.assertEqual(out["observed_state"], "DISARMED")
        self.assertEqual(out["error"], "prearm check failed")

    def test_arm_wrong_readback_is_not_executed(self):
        """ACK accepted but the fresh HEARTBEAT still shows disarmed -- the
        request being ACKed is not proof the vehicle armed."""
        out = cn.normalize("ARM",
                           {"accepted": True, "verified": False, "armed": False,
                            "ack_result": "MAV_RESULT_ACCEPTED",
                            "error": "armed state never became True"})
        self.assertFalse(out["executed"])
        self.assertEqual(out["observed_state"], "DISARMED")

    def test_arm_unknown_armed_state_is_not_executed(self):
        out = cn.normalize("ARM",
                           {"accepted": True, "verified": True, "armed": None, "error": None})
        self.assertFalse(out["executed"])
        self.assertIsNone(out["observed_state"])


class TestUploadNormalization(unittest.TestCase):
    def test_verified_upload_is_executed(self):
        out = cn.normalize("MISSION_UPLOAD",
                           {"contract_version": "mission-contract-v1",
                            "accepted": True, "uploaded": True, "verified": True,
                            "expected_route_waypoint_count": 3,
                            "observed_route_waypoint_count": 3,
                            "expected_pixhawk_item_count": 4,
                            "observed_pixhawk_item_count": 4,
                            "expected_route_content_hash": "sha256:h",
                            "observed_route_content_hash": "sha256:h", "error": None})
        self.assertTrue(out["executed"])
        self.assertEqual(out["expected_state"], "MISSION_UPLOADED")
        self.assertEqual(out["observed_state"], "MISSION_UPLOADED")
        # mission-contract-v1 raw fields preserved for the operator.
        self.assertEqual(out["observed_route_waypoint_count"], 3)
        self.assertEqual(out["observed_pixhawk_item_count"], 4)
        self.assertEqual(out["observed_route_content_hash"], "sha256:h")

    def test_uploaded_but_unverified_is_not_executed(self):
        """Items sent + acked (uploaded) but the readback didn't match --
        must NOT be reported as a successful vehicle action."""
        out = cn.normalize("MISSION_UPLOAD",
                           {"accepted": True, "uploaded": True, "verified": False,
                            "error": {"code": "VERIFICATION_FAILED",
                                      "message": "route waypoint count mismatch "
                                                 "(expected 3, observed 2)"}})
        self.assertFalse(out["executed"])
        self.assertIsNone(out["observed_state"])
        self.assertIn("route waypoint count mismatch", out["error"])

    def test_rejected_upload_is_not_executed(self):
        out = cn.normalize("MISSION_UPLOAD",
                           {"accepted": False, "uploaded": False, "verified": False,
                            "error": {"code": "VEHICLE_ARMED", "message": "vehicle is armed"}})
        self.assertFalse(out["executed"])
        self.assertEqual(out["error"], "vehicle is armed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
