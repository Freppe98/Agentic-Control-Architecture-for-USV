"""
Tests for the Local Agent's Scout<->Operator network-telemetry additions:
communication.wireguard_status parsing and collectors.get_communication_status
/ build_service_status.

Pure -- no live wg, network, or operator. Run directly:

    python3 test_network_telemetry.py
"""
import unittest
from unittest.mock import patch

import communication
import collectors


# `wg show wg0 dump` fixture: interface line, then one peer line (tab-separated).
# Peer cols: pubkey psk endpoint allowed_ips latest_handshake rx tx keepalive.
_IFACE = "PRIV\tPUB\t59714\toff"
def _dump(handshake_epoch):
    peer = f"PEERPUB\t(none)\t1.2.3.4:51820\t10.0.0.0/16\t{handshake_epoch}\t100\t200\t25"
    return _IFACE + "\n" + peer + "\n"


class TestWireguardParse(unittest.TestCase):
    def test_recent_handshake(self):
        now = 1_000_000.0
        s = communication._parse_wg_dump(_dump(int(now) - 30), now)
        self.assertEqual(s["status"], "RECENT_HANDSHAKE")
        self.assertEqual(s["last_handshake_age_s"], 30.0)
        self.assertEqual(s["peers"], 1)
        self.assertTrue(s["interface_up"])

    def test_stale_handshake(self):
        now = 1_000_000.0
        s = communication._parse_wg_dump(_dump(int(now) - 9999), now)
        self.assertEqual(s["status"], "STALE")

    def test_boundary_age_179s_is_fresh(self):
        # P0-1: age < WG_RECENT_HANDSHAKE_S (180s) must be RECENT_HANDSHAKE.
        now = 1_000_000.0
        s = communication._parse_wg_dump(_dump(int(now) - 179), now)
        self.assertEqual(s["status"], "RECENT_HANDSHAKE")

    def test_boundary_age_180s_is_stale(self):
        # P0-1: age == WG_RECENT_HANDSHAKE_S (180s) is the threshold itself
        # and must NOT count as fresh -- "age < 180s = fresh, age >= 180s
        # = stale", not "<=".
        now = 1_000_000.0
        s = communication._parse_wg_dump(_dump(int(now) - 180), now)
        self.assertEqual(s["status"], "STALE")

    def test_boundary_age_181s_is_stale(self):
        now = 1_000_000.0
        s = communication._parse_wg_dump(_dump(int(now) - 181), now)
        self.assertEqual(s["status"], "STALE")

    def test_malformed_handshake_field_does_not_count_as_fresh(self):
        # An unparseable latest_handshake column is skipped (not crashed
        # on) and must not be treated as evidence of a live link -- with no
        # other parseable peer, this is indistinguishable from no handshake.
        now = 1_000_000.0
        iface = _IFACE
        peer = "PEERPUB\t(none)\t1.2.3.4:51820\t10.0.0.0/16\tNOT_A_NUMBER\t100\t200\t25"
        s = communication._parse_wg_dump(iface + "\n" + peer + "\n", now)
        self.assertEqual(s["status"], "NO_HANDSHAKE")

    def test_never_handshaked(self):
        now = 1_000_000.0
        s = communication._parse_wg_dump(_dump(0), now)
        self.assertEqual(s["status"], "NO_HANDSHAKE")
        self.assertIsNone(s["last_handshake_age_s"])

    def test_interface_only_no_peers(self):
        s = communication._parse_wg_dump(_IFACE + "\n", 1_000_000.0)
        self.assertEqual(s["status"], "DOWN")

    def test_empty_output_is_unknown(self):
        s = communication._parse_wg_dump("", 1_000_000.0)
        self.assertEqual(s["status"], "UNKNOWN")
        self.assertIsNone(s["interface_up"])

    def test_command_failure_is_unknown_not_down(self):
        # 12: an unreadable link is UNKNOWN, never a fabricated value.
        communication._wg_cache["value"] = None
        with patch("communication.subprocess.run") as run:
            run.return_value.returncode = 1
            run.return_value.stdout = ""
            s = communication.wireguard_status()
        self.assertEqual(s["status"], "UNKNOWN")


class TestCommunicationBlock(unittest.TestCase):
    def setUp(self):
        # Freeze wg so these assert only the comm-block wiring.
        self._patch = patch("collectors.wireguard_status",
                            return_value={"status": "RECENT_HANDSHAKE"})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_rtt_and_seq_pass_through(self):
        c = collectors.get_communication_status("CONNECTED", 123.0, rtt_ms=42.3, seq=7)
        self.assertEqual(c["rtt_ms"], 42.3)
        self.assertEqual(c["seq"], 7)
        self.assertTrue(c["operator_connected"])

    def test_zero_rtt_survives(self):
        # 15: a genuine 0.0 ms must survive (not be treated as "unmeasured").
        c = collectors.get_communication_status("CONNECTED", 123.0, rtt_ms=0.0, seq=1)
        self.assertEqual(c["rtt_ms"], 0.0)

    def test_packet_loss_unmeasured_is_none_not_zero(self):
        # 12: never fabricate 0% loss; it is honestly None on this side.
        c = collectors.get_communication_status("CONNECTED", 123.0)
        self.assertIsNone(c["packet_loss"])

    def test_disconnected_operator_not_connected(self):
        c = collectors.get_communication_status("DISCONNECTED", None)
        self.assertFalse(c["operator_connected"])

    def test_source_defaults_to_none_when_not_passed(self):
        # Pre-E3 call sites don't pass `source` -- must stay honestly
        # unlabelled (never fabricated to "REAL").
        c = collectors.get_communication_status("CONNECTED", 123.0)
        self.assertIsNone(c["source"])

    def test_source_passes_through_real(self):
        c = collectors.get_communication_status("PARTITIONED", None, source="REAL")
        self.assertEqual(c["source"], "REAL")

    def test_source_passes_through_simulated(self):
        # E3: a synthetic communication_state override (task's PUT
        # /agent/replan/experiment) must be distinguishable in the same
        # status payload real evidence uses -- never a fabricated "REAL".
        c = collectors.get_communication_status("DISCONNECTED", None, source="SIMULATED")
        self.assertEqual(c["source"], "SIMULATED")


class TestServiceStatus(unittest.TestCase):
    def test_composed_from_evidence(self):
        s = collectors.build_service_status(
            vehicle_state_ok=True, mavlink_connected=True,
            health={"docker": {"sensor": True, "gpio": False, "influx": None}},
        )
        self.assertEqual(s["local_mission_agent"], "online")
        self.assertEqual(s["vehicle_api"], "online")
        self.assertEqual(s["pixhawk_link"], "online")
        self.assertEqual(s["sensor_service"], "online")
        self.assertEqual(s["gpio_service"], "offline")
        self.assertEqual(s["influx"], "unknown")  # None -> unknown, not fabricated

    def test_unknown_mavlink_is_unknown_not_offline(self):
        s = collectors.build_service_status(True, None, {})
        self.assertEqual(s["pixhawk_link"], "unknown")

    def test_vehicle_api_offline_when_fetch_failed(self):
        s = collectors.build_service_status(False, False, {})
        self.assertEqual(s["vehicle_api"], "offline")


if __name__ == "__main__":
    unittest.main()
