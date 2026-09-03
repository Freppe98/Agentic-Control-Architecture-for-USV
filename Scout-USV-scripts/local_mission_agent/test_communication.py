"""
Tests for communication.py's E3 addition: resolve_comm_state() and
CommunicationMonitor.source, which distinguish REAL evidence from a
SIMULATED experiment_injection.communication_state override.

Also (P0-1) covers get_comm_state()'s real CONNECTED/PARTITIONED/
DISCONNECTED derivation and vpn_ok()'s freshness delegation to
wireguard_status() -- a stale-but-present WireGuard handshake must not be
treated as usable evidence. _parse_wg_dump()'s own boundary/malformed-input
coverage lives in test_network_telemetry.py; these tests focus on vpn_ok()
and get_comm_state() wiring that logic into the classifier.

    python3 test_communication.py
"""
import unittest
from unittest.mock import patch

import communication as comm
import experiment_injection as ei


class TestResolveCommState(unittest.TestCase):
    def setUp(self):
        ei.clear()

    def tearDown(self):
        ei.clear()

    def test_no_injection_uses_real_evidence(self):
        with patch("communication.get_comm_state", return_value="PARTITIONED") as m:
            state, source = comm.resolve_comm_state(vehicle_id="usv-2")
        self.assertEqual(state, "PARTITIONED")
        self.assertEqual(source, comm.SOURCE_REAL)
        m.assert_called_once()

    def test_active_injection_overrides_without_polling_real_evidence(self):
        ei.inject(communication_state="DISCONNECTED", now=100.0)
        with patch("communication.get_comm_state") as m:
            state, source = comm.resolve_comm_state(vehicle_id="usv-2", now=100.5)
        self.assertEqual(state, "DISCONNECTED")
        self.assertEqual(source, comm.SOURCE_SIMULATED)
        m.assert_not_called()  # real polling skipped entirely while overridden

    def test_injection_for_a_different_vehicle_does_not_apply(self):
        ei.inject(communication_state="DISCONNECTED", target_vehicle="usv-9", now=100.0)
        with patch("communication.get_comm_state", return_value="CONNECTED"):
            state, source = comm.resolve_comm_state(vehicle_id="usv-2", now=100.5)
        self.assertEqual(state, "CONNECTED")
        self.assertEqual(source, comm.SOURCE_REAL)

    def test_energy_only_injection_does_not_affect_comm_state(self):
        # An active injection that overrides battery_percent only (no
        # communication_state) must not accidentally suppress real comm
        # polling -- the two overrides are independent fields on the same
        # store, not a package deal.
        ei.inject(battery_percent=12.0, now=100.0)
        with patch("communication.get_comm_state", return_value="CONNECTED") as m:
            state, source = comm.resolve_comm_state(vehicle_id="usv-2", now=100.5)
        self.assertEqual(state, "CONNECTED")
        self.assertEqual(source, comm.SOURCE_REAL)
        m.assert_called_once()

    def test_real_evidence_resumes_automatically_after_expiry(self):
        ei.inject(communication_state="DISCONNECTED", duration_s=10.0, now=100.0)
        state, source = comm.resolve_comm_state(vehicle_id="usv-2", now=105.0)
        self.assertEqual((state, source), ("DISCONNECTED", comm.SOURCE_SIMULATED))
        with patch("communication.get_comm_state", return_value="CONNECTED"):
            state, source = comm.resolve_comm_state(vehicle_id="usv-2", now=111.0)  # past expiry
        self.assertEqual((state, source), ("CONNECTED", comm.SOURCE_REAL))


class TestCommunicationMonitorSource(unittest.TestCase):
    def setUp(self):
        ei.clear()

    def tearDown(self):
        ei.clear()

    def test_source_none_before_first_poll(self):
        mon = comm.CommunicationMonitor()
        self.assertIsNone(mon.source)

    def test_source_tracks_real_polls(self):
        mon = comm.CommunicationMonitor()
        with patch("communication.get_comm_state", return_value="CONNECTED"):
            state = mon.poll()
        self.assertEqual(state, "CONNECTED")
        self.assertEqual(mon.source, comm.SOURCE_REAL)

    def test_source_tracks_simulated_override(self):
        mon = comm.CommunicationMonitor()
        ei.inject(communication_state="DISCONNECTED")
        state = mon.poll()
        self.assertEqual(state, "DISCONNECTED")
        self.assertEqual(mon.source, comm.SOURCE_SIMULATED)

    def test_recovery_edge_detected_across_simulated_to_real_transition(self):
        # A synthetic DISCONNECTED trial expiring and real evidence resuming
        # as CONNECTED must still be detected as a genuine recovery edge --
        # just_recovered doesn't (and shouldn't) care which source produced
        # either side of the transition.
        mon = comm.CommunicationMonitor()
        ei.inject(communication_state="DISCONNECTED", duration_s=0.01)
        mon.poll()
        self.assertEqual(mon.state, "DISCONNECTED")
        self.assertFalse(mon.just_recovered)

        import time
        time.sleep(0.02)  # let the injection expire
        with patch("communication.get_comm_state", return_value="CONNECTED"):
            mon.poll()
        self.assertEqual(mon.state, "CONNECTED")
        self.assertEqual(mon.source, comm.SOURCE_REAL)
        self.assertTrue(mon.just_recovered)

    def test_stale_or_unknown_real_evidence_never_becomes_connected(self):
        # get_comm_state() itself fails closed (never CONNECTED unless
        # operator_ok() actually succeeded) -- resolve_comm_state must not
        # weaken that by defaulting an unrecognized/None real result to
        # CONNECTED.
        with patch("communication.get_comm_state", return_value="DISCONNECTED"):
            state, source = comm.resolve_comm_state(vehicle_id="usv-2")
        self.assertNotEqual(state, "CONNECTED")
        self.assertEqual(source, comm.SOURCE_REAL)


class TestVpnOkFreshness(unittest.TestCase):
    """
    P0-1: vpn_ok() must require a RECENT_HANDSHAKE from wireguard_status(),
    not merely the presence of a "latest handshake" value. These mock
    wireguard_status() directly (its own age-math is covered against real
    `wg show ... dump` fixtures in test_network_telemetry.py) so each case
    below only exercises vpn_ok()'s own accept/reject wiring.
    """

    def setUp(self):
        comm._vpn_check_warned = False  # each test gets a clean warn-once latch

    def test_recent_handshake_is_ok(self):
        with patch("communication.wireguard_status",
                    return_value={"status": "RECENT_HANDSHAKE", "last_handshake_age_s": 5.0}):
            self.assertTrue(comm.vpn_ok())

    def test_stale_handshake_is_not_ok(self):
        # The core P0-1 case: a handshake value exists (age reported) but is
        # older than the freshness threshold -- must fail, not pass merely
        # because a timestamp was present.
        with patch("communication.wireguard_status",
                    return_value={"status": "STALE", "last_handshake_age_s": 9999.0}):
            self.assertFalse(comm.vpn_ok())

    def test_no_handshake_is_not_ok(self):
        with patch("communication.wireguard_status",
                    return_value={"status": "NO_HANDSHAKE", "last_handshake_age_s": None}):
            self.assertFalse(comm.vpn_ok())

    def test_missing_evidence_down_is_not_ok(self):
        with patch("communication.wireguard_status",
                    return_value={"status": "DOWN", "last_handshake_age_s": None}):
            self.assertFalse(comm.vpn_ok())

    def test_unknown_command_failure_fails_closed_not_ok(self):
        # Command failure / unparseable output must fail closed -- ambiguous
        # evidence is never treated as "recent".
        with patch("communication.wireguard_status",
                    return_value={"status": "UNKNOWN", "last_handshake_age_s": None}):
            self.assertFalse(comm.vpn_ok())


class TestGetCommStateClassification(unittest.TestCase):
    """
    P0-1: the full CONNECTED / PARTITIONED / DISCONNECTED decision in
    get_comm_state(), with vpn_ok() driven through wireguard_status() so a
    stale handshake cannot be misclassified as PARTITIONED.
    """

    def test_operator_reachable_is_connected(self):
        with patch("communication.operator_ok", return_value=True):
            self.assertEqual(comm.get_comm_state(), "CONNECTED")

    def test_operator_unreachable_fresh_wg_is_partitioned(self):
        with patch("communication.operator_ok", return_value=False), \
             patch("communication.internet_ok", return_value=True), \
             patch("communication.wireguard_status",
                   return_value={"status": "RECENT_HANDSHAKE", "last_handshake_age_s": 10.0}):
            self.assertEqual(comm.get_comm_state(), "PARTITIONED")

    def test_operator_unreachable_stale_wg_is_disconnected(self):
        # This is the exact audit scenario: a handshake value exists but is
        # stale -- must classify as DISCONNECTED, not PARTITIONED.
        with patch("communication.operator_ok", return_value=False), \
             patch("communication.internet_ok", return_value=True), \
             patch("communication.wireguard_status",
                   return_value={"status": "STALE", "last_handshake_age_s": 500.0}):
            self.assertEqual(comm.get_comm_state(), "DISCONNECTED")

    def test_operator_unreachable_no_handshake_is_disconnected(self):
        with patch("communication.operator_ok", return_value=False), \
             patch("communication.internet_ok", return_value=True), \
             patch("communication.wireguard_status",
                   return_value={"status": "NO_HANDSHAKE", "last_handshake_age_s": None}):
            self.assertEqual(comm.get_comm_state(), "DISCONNECTED")

    def test_operator_unreachable_unknown_wg_evidence_is_disconnected(self):
        # Missing/invalid handshake evidence (command failed / unparseable)
        # must fail closed to DISCONNECTED, never be treated as recent.
        with patch("communication.operator_ok", return_value=False), \
             patch("communication.internet_ok", return_value=True), \
             patch("communication.wireguard_status",
                   return_value={"status": "UNKNOWN", "last_handshake_age_s": None}):
            self.assertEqual(comm.get_comm_state(), "DISCONNECTED")

    def test_no_internet_is_disconnected_even_with_fresh_wg(self):
        # internet_ok() is checked before vpn_ok() in get_comm_state() and
        # short-circuits to DISCONNECTED -- preserved as-is (P0-1 does not
        # touch this ordering/role).
        with patch("communication.operator_ok", return_value=False), \
             patch("communication.internet_ok", return_value=False), \
             patch("communication.wireguard_status",
                   return_value={"status": "RECENT_HANDSHAKE", "last_handshake_age_s": 1.0}) as wg:
            self.assertEqual(comm.get_comm_state(), "DISCONNECTED")
            wg.assert_not_called()


class TestCommStateNoDebounceByDesign(unittest.TestCase):
    """
    P0-1 asked us to check for an existing comm_state debounce/hysteresis
    mechanism before adding one. There isn't one: CommunicationMonitor.poll()
    (communication.py) and local_agent.py's main loop both apply
    get_comm_state()'s result immediately, with no persistence counter (the
    only debounce mechanism in this service is energy_policy.py's, which is
    unrelated). This test pins that: a single-poll transient DISCONNECTED
    observation is reflected immediately, not filtered -- documenting current
    behavior rather than introducing a new debounce this task didn't ask for.
    """

    def test_single_transient_poll_is_reflected_immediately(self):
        mon = comm.CommunicationMonitor()
        with patch("communication.get_comm_state", return_value="CONNECTED"):
            mon.poll()
        self.assertEqual(mon.state, "CONNECTED")

        with patch("communication.get_comm_state", return_value="DISCONNECTED"):
            mon.poll()
        # No debounce exists today, so one transient bad poll already flips
        # the reported state -- this is documenting/pinning existing
        # behavior, not asserting it is the only valid design.
        self.assertEqual(mon.state, "DISCONNECTED")


if __name__ == "__main__":
    unittest.main(verbosity=2)
