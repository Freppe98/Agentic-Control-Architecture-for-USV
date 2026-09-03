"""
Tests for local_agent._wireguard_recorder_fields() -- the E3 instrumentation
task's pure mapping from communication.wireguard_status()'s already-computed
result to the two new recorder-only columns (wireguard_handshake_age_s,
wireguard_fresh).

This function does not call `wg`, does not touch communication.py, and is
never consulted by get_comm_state()/vpn_ok()/resolve_comm_state() -- it only
reads the SAME status dict those already produce, for the recorder's benefit.
These tests exercise it directly with synthetic wireguard_status()-shaped
dicts (the exact vocabulary _parse_wg_dump() produces: RECENT_HANDSHAKE /
STALE / NO_HANDSHAKE / DOWN / UNKNOWN), so they never need a live `wg` call
and never re-derive the 180s threshold themselves.

Run directly: python3 test_local_agent_wireguard_recorder_fields.py
"""
import tempfile
import unittest

# Mirror the existing test_local_agent_command_flow.py convention: redirect
# config file paths to tempfiles BEFORE importing local_agent, so importing
# it (a module-level side effect of other tests in this suite) never touches
# real on-disk state.
import config
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")

import local_agent


class TestWireguardRecorderFields(unittest.TestCase):
    # A: fresh handshake -- numeric age recorded, wireguard_fresh == True.
    def test_fresh_handshake_below_180s(self):
        wg_status = {
            "interface": "wg0", "interface_up": True,
            "status": "RECENT_HANDSHAKE", "last_handshake_age_s": 12.3, "peers": 1,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertEqual(result["wireguard_handshake_age_s"], 12.3)
        self.assertIs(result["wireguard_fresh"], True)

    def test_fresh_handshake_boundary_just_under_180s(self):
        # Mirrors test_network_telemetry.py's own 179s-is-fresh boundary case
        # -- this function must not re-derive that boundary, only pass the
        # already-authoritative status through.
        wg_status = {
            "interface": "wg0", "interface_up": True,
            "status": "RECENT_HANDSHAKE", "last_handshake_age_s": 179.0, "peers": 1,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertEqual(result["wireguard_handshake_age_s"], 179.0)
        self.assertIs(result["wireguard_fresh"], True)

    # B: stale handshake (age >= 180) -- numeric age recorded, fresh == False.
    def test_stale_handshake_at_180s_boundary(self):
        wg_status = {
            "interface": "wg0", "interface_up": True,
            "status": "STALE", "last_handshake_age_s": 180.0, "peers": 1,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertEqual(result["wireguard_handshake_age_s"], 180.0)
        self.assertIs(result["wireguard_fresh"], False)

    def test_stale_handshake_well_past_threshold(self):
        wg_status = {
            "interface": "wg0", "interface_up": True,
            "status": "STALE", "last_handshake_age_s": 612.4, "peers": 1,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertEqual(result["wireguard_handshake_age_s"], 612.4)
        self.assertIs(result["wireguard_fresh"], False)

    # C: unavailable/no-handshake evidence -- age None, freshness None (never
    # guessed True/False when the classifier itself has no fresh/stale
    # opinion to report).
    def test_never_handshaked_is_none_not_false(self):
        wg_status = {
            "interface": "wg0", "interface_up": True,
            "status": "NO_HANDSHAKE", "last_handshake_age_s": None, "peers": 1,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertIsNone(result["wireguard_handshake_age_s"])
        self.assertIsNone(result["wireguard_fresh"])

    def test_interface_down_no_peers_is_none(self):
        wg_status = {
            "interface": "wg0", "interface_up": True,
            "status": "DOWN", "last_handshake_age_s": None, "peers": 0,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertIsNone(result["wireguard_handshake_age_s"])
        self.assertIsNone(result["wireguard_fresh"])

    def test_command_unavailable_is_none(self):
        # wireguard_status()'s own "can't tell" case (passwordless sudo not
        # configured, wg absent, etc.) -- must stay None, never fabricated to
        # either True or False.
        wg_status = {
            "interface": "wg0", "interface_up": None,
            "status": "UNKNOWN", "last_handshake_age_s": None, "peers": 0,
        }
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertIsNone(result["wireguard_handshake_age_s"])
        self.assertIsNone(result["wireguard_fresh"])

    def test_does_not_invent_zero_for_missing_age(self):
        # Explicit regression guard for the "do not invent 0" requirement --
        # an unavailable age must never come back as 0/0.0.
        wg_status = {"status": "UNKNOWN", "last_handshake_age_s": None}
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertIsNone(result["wireguard_handshake_age_s"])
        self.assertNotEqual(result["wireguard_handshake_age_s"], 0)

    def test_only_returns_the_two_new_keys(self):
        # Keeps this a small, additive mapping -- not a place that grows
        # unrelated fields over time.
        wg_status = {"status": "RECENT_HANDSHAKE", "last_handshake_age_s": 1.0}
        result = local_agent._wireguard_recorder_fields(wg_status)
        self.assertEqual(set(result.keys()), {"wireguard_handshake_age_s", "wireguard_fresh"})


if __name__ == "__main__":
    unittest.main()
