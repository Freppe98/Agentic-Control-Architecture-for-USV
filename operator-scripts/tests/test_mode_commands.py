"""Backend tests for the Pixhawk mode commands, focused on the LOITER-as-primary-safety
change. Run:  python -m unittest tests.test_mode_commands  (no pytest needed).

Confirms the command ROUTING is unchanged by the UI reshuffle: LOITER routes as
SET_MODE_LOITER with no forced confirmation (quick access), and SET_MODE_HOLD is still
accepted for backend compatibility.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VID = 2


class ModeCommandTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.commands.clear()
        main.commands_by_id.clear()
        main.comms_state_by_id[VID] = "CONNECTED"  # fresh link so nothing needs confirm

    def create(self, ctype, **extra):
        return self.client.post("/api/commands", json={"vehicle_id": VID, "type": ctype, **extra})

    def test_loiter_is_a_valid_command_type(self):
        self.assertIn("SET_MODE_LOITER", main.COMMAND_TYPES)

    def test_loiter_routes_as_set_mode_loiter_and_queues(self):
        r = self.create("SET_MODE_LOITER")
        self.assertEqual(r.status_code, 200)
        cmd = r.json()["command"]
        self.assertEqual(cmd["type"], "SET_MODE_LOITER")
        self.assertEqual(cmd["status"], "QUEUED")

    def test_loiter_needs_no_confirmation_quick_access(self):
        # LOITER is a safety hold — it must be quickly accessible (not confirm-gated).
        self.assertNotIn("SET_MODE_LOITER", main.CONFIRM_REQUIRED_TYPES)
        r = self.create("SET_MODE_LOITER")  # no confirm flag
        self.assertEqual(r.status_code, 200)

    def test_hold_still_supported_for_compatibility(self):
        self.assertIn("SET_MODE_HOLD", main.COMMAND_TYPES)  # kept, not removed
        r = self.create("SET_MODE_HOLD")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["command"]["type"], "SET_MODE_HOLD")

    def test_loiter_and_hold_are_distinct_types(self):
        a = self.create("SET_MODE_LOITER").json()["command"]["type"]
        b = self.create("SET_MODE_HOLD").json()["command"]["type"]
        self.assertNotEqual(a, b)


if __name__ == "__main__":
    unittest.main()
