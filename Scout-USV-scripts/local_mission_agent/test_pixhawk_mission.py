"""
Standalone tests for pixhawk_mission.py (GET /agent/pixhawk_mission logic).
No pytest dependency -- run directly:

    python3 test_pixhawk_mission.py

Monkeypatches api_client entirely -- no live vehicle Flask service,
mavlink2rest, or Pixhawk required. See
services/flask/test_mission_service.py for coverage of the actual MAVLink
mission-download handshake and hash generation this module sits on top of.
"""
import time
import unittest
from unittest.mock import patch

import pixhawk_mission


def _valid_mission(count=2, current_seq=1, mission_hash="deadbeef"):
    return {
        "mission_loaded": count > 0,
        "mission_valid": True,
        "count": count,
        "current_seq": current_seq,
        "hash": mission_hash,
        "waypoints": [{"sequence": i, "latitude": 1.0 + i, "longitude": 2.0 + i,
                        "altitude": 0.0, "command": "MAV_CMD_NAV_WAYPOINT",
                        "frame": "MAV_FRAME_GLOBAL_RELATIVE_ALT", "autocontinue": True,
                        "loiter_time": 0, "param1": 0, "param2": 0, "param3": 0, "param4": 0}
                       for i in range(count)],
        "partial": False,
        "duplicate_sequences": [],
        "invalid_sequences": [],
        "unsupported_sequences": [],
        "error": None,
        "reachable": True,
        "fetched_at": round(time.time(), 2),
        "schema_version": 1,
    }


class TestBuildPixhawkMissionStatusHappyPath(unittest.TestCase):
    def setUp(self):
        pixhawk_mission._last_good_result = None
        pixhawk_mission._last_good_at = None

    def test_passes_through_a_valid_result(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(count=3)):
            result = pixhawk_mission.build_pixhawk_mission_status()
        self.assertTrue(result["mission_loaded"])
        self.assertTrue(result["mission_valid"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(len(result["waypoints"]), 3)
        self.assertFalse(result["partial"])
        self.assertIsNone(result["error"])

    def test_hash_is_passed_through_unmodified(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(mission_hash="abc123")):
            result = pixhawk_mission.build_pixhawk_mission_status()
        self.assertEqual(result["hash"], "abc123")

    def test_zero_waypoint_mission_passes_through(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(count=0)):
            result = pixhawk_mission.build_pixhawk_mission_status()
        self.assertFalse(result["mission_loaded"])
        self.assertTrue(result["mission_valid"])
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["waypoints"], [])

    def test_current_seq_passed_through_directly_not_estimated(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(current_seq=5)):
            result = pixhawk_mission.build_pixhawk_mission_status()
        self.assertEqual(result["current_seq"], 5)

    def test_last_fetch_age_near_zero_immediately_after_success(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission()):
            result = pixhawk_mission.build_pixhawk_mission_status()
        self.assertIsNotNone(result["last_fetch_age"])
        self.assertLess(result["last_fetch_age"], 1.0)


class TestBuildPixhawkMissionStatusDegradedFlask(unittest.TestCase):
    def setUp(self):
        pixhawk_mission._last_good_result = None
        pixhawk_mission._last_good_at = None

    def test_flask_unreachable_with_no_prior_success_reports_unavailable_not_fake(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", side_effect=RuntimeError("connection refused")):
            result = pixhawk_mission.build_pixhawk_mission_status()
        self.assertIsNone(result["mission_loaded"])
        self.assertIsNone(result["mission_valid"])
        self.assertIsNone(result["count"])
        self.assertIsNone(result["hash"])
        self.assertEqual(result["waypoints"], [])
        self.assertIsNone(result["last_fetch_age"])
        self.assertIn("connection refused", result["error"])

    def test_flask_unreachable_after_prior_success_falls_back_to_last_known_mission(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(count=4, mission_hash="h1")):
            first = pixhawk_mission.build_pixhawk_mission_status()
        self.assertTrue(first["mission_valid"])

        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", side_effect=RuntimeError("timeout")):
            second = pixhawk_mission.build_pixhawk_mission_status()

        self.assertTrue(second["mission_loaded"])
        self.assertEqual(second["count"], 4)
        self.assertEqual(second["hash"], "h1")
        self.assertEqual(len(second["waypoints"]), 4)
        self.assertIn("showing last known mission", second["error"])
        self.assertIsNotNone(second["last_fetch_age"])
        self.assertGreaterEqual(second["last_fetch_age"], 0.0)

    def test_invalid_result_is_not_cached_as_last_known_good(self):
        """A download that only partially completed (mission_valid=False,
        partial=True) must not poison the fallback cache -- only a fully
        confirmed mission counts as 'last known good'."""
        good = _valid_mission(count=2, mission_hash="good-hash")
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=good):
            pixhawk_mission.build_pixhawk_mission_status()

        partial = dict(_valid_mission(count=5, mission_hash=None))
        partial["mission_valid"] = False
        partial["partial"] = True
        partial["waypoints"] = partial["waypoints"][:1]
        partial["error"] = "timed out after fetching 1/5 waypoints"
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=partial):
            during_partial = pixhawk_mission.build_pixhawk_mission_status()
        # The live (if degraded) result is still surfaced as-is.
        self.assertFalse(during_partial["mission_valid"])
        self.assertTrue(during_partial["partial"])
        self.assertIsNone(during_partial["hash"])

        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", side_effect=RuntimeError("down")):
            after_flask_down = pixhawk_mission.build_pixhawk_mission_status()
        # Falls back to the last *valid* mission (count=2), not the partial one.
        self.assertEqual(after_flask_down["count"], 2)
        self.assertEqual(after_flask_down["hash"], "good-hash")


class TestMultipleSequentialDownloads(unittest.TestCase):
    def setUp(self):
        pixhawk_mission._last_good_result = None
        pixhawk_mission._last_good_at = None

    def test_repeated_polls_each_reflect_current_flask_state(self):
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(count=1)):
            first = pixhawk_mission.build_pixhawk_mission_status()
        with patch("pixhawk_mission._fetch_flask_pixhawk_mission", return_value=_valid_mission(count=1)):
            second = pixhawk_mission.build_pixhawk_mission_status()

        for result in (first, second):
            self.assertTrue(result["mission_loaded"])
            self.assertEqual(result["count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
