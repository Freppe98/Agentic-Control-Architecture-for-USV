"""
Standalone tests for experiment_injection.py.

    python3 test_experiment_injection.py

Covers: injection set/clear, expiry, target-vehicle filtering, and that every
active injection is always tagged source=SIMULATED in status.
"""
import unittest

import experiment_injection as ei


class TestInjection(unittest.TestCase):
    def setUp(self):
        ei.clear()

    def tearDown(self):
        ei.clear()

    def test_none_when_unset(self):
        self.assertIsNone(ei.active("usv-2"))
        self.assertFalse(ei.status("usv-2")["active"])

    def test_inject_and_read(self):
        ei.inject(force_safe_return=True, now=100.0)
        a = ei.active("usv-2", now=100.5)
        self.assertIsNotNone(a)
        self.assertTrue(a["force_safe_return"])
        self.assertEqual(a["source"], ei.SOURCE_SIMULATED)

    def test_expiry(self):
        ei.inject(force_safe_return=True, duration_s=10.0, now=100.0)
        self.assertIsNotNone(ei.active("usv-2", now=105.0))     # within window
        self.assertIsNone(ei.active("usv-2", now=111.0))        # expired
        # expired injection is cleared as a side effect
        self.assertFalse(ei.status("usv-2", now=112.0)["active"])

    def test_target_vehicle_filtering(self):
        ei.inject(force_safe_return=True, target_vehicle="usv-9", now=100.0)
        self.assertIsNone(ei.active("usv-2", now=100.5))        # different target
        self.assertIsNotNone(ei.active("usv-9", now=100.5))     # matching target

    def test_status_always_marks_simulated(self):
        ei.inject(battery_percent=5.0, now=100.0)
        s = ei.status("usv-2", now=100.5)
        self.assertTrue(s["active"])
        self.assertEqual(s["source"], ei.SOURCE_SIMULATED)
        self.assertEqual(s["injection"]["battery_percent"], 5.0)

    def test_clear(self):
        ei.inject(force_safe_return=True)
        ei.clear()
        self.assertIsNone(ei.active("usv-2"))


class TestCommunicationStateInjection(unittest.TestCase):
    """E3: the communication_state override -- same store, same source tag,
    same expiry/target-vehicle rules as the E2 energy overrides above; only
    the new field itself and its validation are specific to this task."""

    def setUp(self):
        ei.clear()

    def tearDown(self):
        ei.clear()

    def test_inject_and_read(self):
        ei.inject(communication_state="DISCONNECTED", now=100.0)
        a = ei.active("usv-2", now=100.5)
        self.assertIsNotNone(a)
        self.assertEqual(a["communication_state"], "DISCONNECTED")
        self.assertEqual(a["source"], ei.SOURCE_SIMULATED)

    def test_all_three_states_accepted(self):
        for state in ei.COMMUNICATION_STATES:
            ei.inject(communication_state=state, now=100.0)
            a = ei.active("usv-2", now=100.5)
            self.assertEqual(a["communication_state"], state)
            ei.clear()

    def test_expiry_reverts_to_no_override(self):
        ei.inject(communication_state="DISCONNECTED", duration_s=10.0, now=100.0)
        self.assertIsNotNone(ei.active("usv-2", now=105.0))
        self.assertIsNone(ei.active("usv-2", now=111.0))  # expired -- caller falls back to real evidence
        self.assertFalse(ei.status("usv-2", now=112.0)["active"])

    def test_can_coexist_with_energy_overrides(self):
        # Not a competing system -- a single injection may carry both an
        # energy override (E2) and a communication override (E3) at once.
        ei.inject(battery_percent=12.0, communication_state="PARTITIONED", now=100.0)
        a = ei.active("usv-2", now=100.5)
        self.assertEqual(a["battery_percent"], 12.0)
        self.assertEqual(a["communication_state"], "PARTITIONED")

    def test_target_vehicle_filtering_applies_to_comm_override_too(self):
        ei.inject(communication_state="DISCONNECTED", target_vehicle="usv-9", now=100.0)
        self.assertIsNone(ei.active("usv-2", now=100.5))
        self.assertIsNotNone(ei.active("usv-9", now=100.5))


class TestCommunicationStateValidation(unittest.TestCase):
    def test_valid_state_accepted(self):
        kwargs, code, message = ei.validate({"communication_state": "PARTITIONED"}, "usv-2")
        self.assertIsNotNone(kwargs)
        self.assertIsNone(code)
        self.assertEqual(kwargs["communication_state"], "PARTITIONED")

    def test_invalid_state_rejected(self):
        kwargs, code, message = ei.validate({"communication_state": "OFFLINE"}, "usv-2")
        self.assertIsNone(kwargs)
        self.assertEqual(code, "INVALID_VALUE")

    def test_non_string_state_rejected(self):
        kwargs, code, message = ei.validate({"communication_state": 3}, "usv-2")
        self.assertIsNone(kwargs)
        self.assertEqual(code, "INVALID_VALUE")

    def test_communication_state_alone_satisfies_at_least_one_override(self):
        # Before this task, communication_state didn't exist, so a body
        # containing only it fell through to "at least one override is
        # required" -- must now be recognized on its own.
        kwargs, code, message = ei.validate({"communication_state": "DISCONNECTED"}, "usv-2")
        self.assertIsNotNone(kwargs)
        self.assertIsNone(code)

    def test_empty_body_still_rejected(self):
        kwargs, code, message = ei.validate({}, "usv-2")
        self.assertIsNone(kwargs)
        self.assertEqual(code, "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main(verbosity=2)
