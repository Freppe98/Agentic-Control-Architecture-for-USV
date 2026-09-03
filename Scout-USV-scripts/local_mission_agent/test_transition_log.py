"""
Standalone tests for transition_log.py -- the rolling communication/mission/
authority transition audit trail. No pytest dependency:

    python3 test_transition_log.py
"""
import unittest
import unittest.mock as mock

import experiment_recording_runtime
import transition_log


class TestTransitionLog(unittest.TestCase):
    def setUp(self):
        transition_log._transitions.clear()

    def test_empty_log_has_no_last_transition(self):
        self.assertIsNone(transition_log.last())

    def test_record_transition_is_retrievable(self):
        transition_log.record_transition("communication", "CONNECTED", "PARTITIONED", "Heartbeat timeout")
        entry = transition_log.last()
        self.assertEqual(entry["type"], "communication")
        self.assertEqual(entry["from"], "CONNECTED")
        self.assertEqual(entry["to"], "PARTITIONED")
        self.assertEqual(entry["reason"], "Heartbeat timeout")
        self.assertIn("timestamp", entry)

    def test_get_recent_returns_in_order(self):
        transition_log.record_transition("mission", "IDLE", "TRANSIT", "Mission activated")
        transition_log.record_transition("mission", "TRANSIT", "SEARCH", "First waypoint reached")
        recent = transition_log.get_recent()
        self.assertEqual([e["to"] for e in recent], ["TRANSIT", "SEARCH"])

    def test_log_bounded_to_max_transitions(self):
        for i in range(transition_log.MAX_TRANSITIONS + 20):
            transition_log.record_transition("mission", str(i), str(i + 1), "test")
        self.assertEqual(len(transition_log.get_recent()), transition_log.MAX_TRANSITIONS)
        # oldest entries are dropped, not the newest
        self.assertEqual(transition_log.last()["to"], str(transition_log.MAX_TRANSITIONS + 20))


class _CapturingRecorder:
    """Records every record_event() call verbatim -- the same fail-open
    surface a real ExperimentRecorder exposes, minus any disk I/O."""
    def __init__(self):
        self.events = []

    def record_event(self, event_type, source, data=None, priority="high"):
        self.events.append({"type": event_type, "source": source, "data": data, "priority": priority})


class TestCommunicationEventEvidence(unittest.TestCase):
    """Task section 10/20: COMMUNICATION_STATE_CHANGED events enriched with
    causal measurements ALREADY available in memory at the transition call
    site -- never a new measurement taken merely for recording."""

    def setUp(self):
        transition_log._transitions.clear()
        self.recorder = _CapturingRecorder()
        experiment_recording_runtime.register(self.recorder)

    def tearDown(self):
        experiment_recording_runtime.register(None)

    def test_extra_causal_fields_forwarded_to_recorder_event(self):
        transition_log.record_transition(
            "communication", "PARTITIONED", "CONNECTED", "Operator reachable again",
            extra={"operator_reachable": True, "buffered_message_count": 7},
        )
        self.assertEqual(len(self.recorder.events), 1)
        ev = self.recorder.events[0]
        self.assertEqual(ev["type"], "COMMUNICATION_STATE_CHANGED")
        self.assertEqual(ev["data"]["from"], "PARTITIONED")
        self.assertEqual(ev["data"]["to"], "CONNECTED")
        self.assertEqual(ev["data"]["operator_reachable"], True)
        self.assertEqual(ev["data"]["buffered_message_count"], 7)

    def test_missing_extra_fields_are_simply_absent_not_fabricated(self):
        transition_log.record_transition(
            "communication", "CONNECTED", "PARTITIONED", "Operator endpoint stopped responding",
            extra={"operator_reachable": False},  # only ONE causal field on hand
        )
        data = self.recorder.events[0]["data"]
        self.assertEqual(data["operator_reachable"], False)
        self.assertNotIn("vpn_reachable", data)
        self.assertNotIn("telemetry_age_s", data)
        self.assertNotIn("buffered_message_count", data)

    def test_none_valued_extra_fields_are_dropped_not_recorded_as_null(self):
        transition_log.record_transition(
            "communication", "CONNECTED", "DISCONNECTED", "Internet unreachable",
            extra={"operator_reachable": False, "vpn_reachable": None},
        )
        data = self.recorder.events[0]["data"]
        self.assertIn("operator_reachable", data)
        self.assertNotIn("vpn_reachable", data)

    def test_no_extra_still_records_base_transition(self):
        transition_log.record_transition("communication", "CONNECTED", "PARTITIONED", "reason")
        self.assertEqual(len(self.recorder.events), 1)
        self.assertEqual(self.recorder.events[0]["data"],
                         {"from": "CONNECTED", "to": "PARTITIONED", "reason": "reason"})

    def test_extra_never_triggers_a_network_measurement(self):
        """transition_log itself must never call out to communication.py's
        network-probing functions merely to enrich a recorder event -- the
        `extra` dict must be exactly what the caller already had on hand."""
        with mock.patch("communication.vpn_ok", side_effect=AssertionError("must not be called")), \
             mock.patch("communication.operator_ok", side_effect=AssertionError("must not be called")), \
             mock.patch("communication.wireguard_status", side_effect=AssertionError("must not be called")):
            transition_log.record_transition(
                "communication", "CONNECTED", "PARTITIONED", "reason",
                extra={"operator_reachable": False, "buffered_message_count": 3},
            )
        self.assertEqual(len(self.recorder.events), 1)

    def test_extra_ignored_for_non_communication_transition_types_stays_correct(self):
        transition_log.record_transition("authority", "OPERATOR", "LOCAL_AGENT", "granted",
                                         extra={"note": "not a communication transition"})
        ev = self.recorder.events[0]
        self.assertEqual(ev["type"], "CONTROL_AUTHORITY_CHANGED")
        self.assertEqual(ev["data"]["note"], "not a communication transition")


if __name__ == "__main__":
    unittest.main(verbosity=2)
