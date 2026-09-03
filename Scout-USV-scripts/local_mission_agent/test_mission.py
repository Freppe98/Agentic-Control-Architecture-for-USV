"""
Standalone tests for mission.py (GET /agent/mission logic). No pytest
dependency -- run directly:

    python3 test_mission.py

Monkeypatches api_client entirely -- no live vehicle Flask service,
mavlink2rest, or Pixhawk required. See services/flask/test_mission_service.py
for coverage of the actual MAVLink mission-download handshake this module
sits on top of.
"""
import time
import unittest
from unittest.mock import patch

import mission


def _valid_mission(count=2, current_waypoint=1, fetched_at=None):
    return {
        "available": True,
        "reachable": True,
        "fetched_at": fetched_at if fetched_at is not None else round(time.time(), 2),
        "mission_count": count,
        "current_waypoint": current_waypoint,
        "home_position": {"latitude": 1.0, "longitude": 2.0, "altitude": 3.0},
        "waypoints": [{"sequence": i, "latitude": 1.0 + i, "longitude": 2.0 + i,
                        "altitude": 0.0, "command": "MAV_CMD_NAV_WAYPOINT",
                        "frame": "MAV_FRAME_GLOBAL_RELATIVE_ALT", "autocontinue": True,
                        "loiter_time": 0} for i in range(count)],
        "mission_loaded": count > 0,
        "mission_valid": True,
        "last_fetch_age": 0.0,
        "error": None,
        "mission_hash": None,
        "mission_version": None,
        "schema_version": 1,
    }


class TestBuildMissionStatusHappyPath(unittest.TestCase):
    def setUp(self):
        mission._last_good_result = None
        mission._last_good_at = None

    def test_passes_through_a_valid_result(self):
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(count=3)):
            result = mission.build_mission_status()
        self.assertTrue(result["available"])
        self.assertEqual(result["mission_count"], 3)
        self.assertEqual(len(result["waypoints"]), 3)
        self.assertIsNone(result["error"])

    def test_last_fetch_age_is_near_zero_immediately_after_success(self):
        with patch("mission._fetch_flask_mission", return_value=_valid_mission()):
            result = mission.build_mission_status()
        self.assertIsNotNone(result["last_fetch_age"])
        self.assertLess(result["last_fetch_age"], 1.0)

    def test_zero_waypoint_mission_passes_through(self):
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(count=0)):
            result = mission.build_mission_status()
        self.assertTrue(result["available"])
        self.assertEqual(result["mission_count"], 0)
        self.assertEqual(result["waypoints"], [])

    def test_current_waypoint_passed_through(self):
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(current_waypoint=5)):
            result = mission.build_mission_status()
        self.assertEqual(result["current_waypoint"], 5)


class TestBuildMissionStatusDegradedFlask(unittest.TestCase):
    def setUp(self):
        mission._last_good_result = None
        mission._last_good_at = None

    def test_flask_unreachable_with_no_prior_success_reports_unavailable_not_fake(self):
        with patch("mission._fetch_flask_mission", side_effect=RuntimeError("connection refused")):
            result = mission.build_mission_status()
        self.assertFalse(result["available"])
        self.assertIsNone(result["mission_count"])
        self.assertEqual(result["waypoints"], [])
        self.assertIsNone(result["last_fetch_age"])
        self.assertIn("connection refused", result["error"])

    def test_flask_unreachable_after_prior_success_falls_back_to_last_known_mission(self):
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(count=4)):
            first = mission.build_mission_status()
        self.assertTrue(first["available"])

        with patch("mission._fetch_flask_mission", side_effect=RuntimeError("timeout")):
            second = mission.build_mission_status()

        self.assertTrue(second["available"])
        self.assertEqual(second["mission_count"], 4)
        self.assertEqual(len(second["waypoints"]), 4)
        self.assertIn("showing last known mission", second["error"])
        self.assertIsNotNone(second["last_fetch_age"])
        self.assertGreaterEqual(second["last_fetch_age"], 0.0)

    def test_partial_invalid_result_is_not_cached_as_last_known_good(self):
        """A download that only partially completed (mission_valid=False)
        must not poison the fallback cache -- only a fully confirmed mission
        counts as 'last known good'."""
        good = _valid_mission(count=2)
        with patch("mission._fetch_flask_mission", return_value=good):
            mission.build_mission_status()

        partial = dict(_valid_mission(count=5))
        partial["mission_valid"] = False
        partial["waypoints"] = partial["waypoints"][:1]
        partial["error"] = "timed out after fetching 1/5 waypoints"
        with patch("mission._fetch_flask_mission", return_value=partial):
            during_partial = mission.build_mission_status()
        # The live (if degraded) result is still surfaced as-is, not silently
        # replaced by the cache, since the vehicle Flask API did answer.
        self.assertFalse(during_partial["mission_valid"])
        self.assertEqual(during_partial["mission_count"], 5)

        with patch("mission._fetch_flask_mission", side_effect=RuntimeError("down")):
            after_flask_down = mission.build_mission_status()
        # Falls back to the last *valid* mission (count=2), not the partial one.
        self.assertEqual(after_flask_down["mission_count"], 2)


class TestMultipleSequentialDownloads(unittest.TestCase):
    def setUp(self):
        mission._last_good_result = None
        mission._last_good_at = None

    def test_repeated_polls_each_reflect_current_flask_state(self):
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(count=1)):
            first = mission.build_mission_status()
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(count=1)):
            second = mission.build_mission_status()
        with patch("mission._fetch_flask_mission", return_value=_valid_mission(count=1)):
            third = mission.build_mission_status()

        for result in (first, second, third):
            self.assertTrue(result["available"])
            self.assertEqual(result["mission_count"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
