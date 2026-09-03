"""
Standalone tests for bench_preflight.py (read-only bench readiness check).
No pytest dependency -- run directly:

    python3 test_bench_preflight.py

Every check is a pure function over already-fetched data, so most tests just
call it with a hand-built dict -- no mocking. The run_all() aggregation tests
patch bench_preflight's own fetchers so no live vehicle Flask service,
mavlink2rest, operator backend, or Local Agent process is required.
"""
import json
import time
import unittest
from unittest.mock import patch

import bench_preflight as bp
import mission_operation_status
from bench_preflight import PASS, FAIL, WARN, SKIP


def _hb(counter, age_s=0.1):
    """A HEARTBEAT-shaped mavlink2rest envelope with a last_update `age_s`
    seconds in the past and the given counter."""
    from datetime import datetime, timezone, timedelta

    ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).strftime(
        "%Y-%m-%dT%H:%M:%S.%f000Z"
    )
    return {"message": {"type": "HEARTBEAT"},
            "status": {"time": {"counter": counter, "last_update": ts}}}


# ── heartbeat counter ─────────────────────────────────────────────────────────

class TestHeartbeatCounter(unittest.TestCase):
    def test_advancing_passes(self):
        r = bp.check_heartbeat_counter(_hb(100), _hb(102))
        self.assertEqual(r["status"], PASS)
        self.assertEqual(r["evidence"]["counter_before"], 100)
        self.assertEqual(r["evidence"]["counter_after"], 102)

    def test_stalled_counter_fails(self):
        r = bp.check_heartbeat_counter(_hb(100), _hb(100))
        self.assertEqual(r["status"], FAIL)

    def test_decreasing_counter_fails(self):
        self.assertEqual(bp.check_heartbeat_counter(_hb(105), _hb(100))["status"], FAIL)

    def test_fetch_error_fails(self):
        self.assertEqual(
            bp.check_heartbeat_counter(None, None, "connection refused")["status"], FAIL
        )

    def test_missing_counter_field_fails(self):
        bad = {"status": {"time": {}}}
        self.assertEqual(bp.check_heartbeat_counter(bad, bad)["status"], FAIL)


# ── heartbeat / position age ──────────────────────────────────────────────────

class TestAges(unittest.TestCase):
    def test_fresh_heartbeat_passes(self):
        self.assertEqual(bp.check_heartbeat_age(_hb(1, age_s=0.2))["status"], PASS)

    def test_stale_heartbeat_fails(self):
        r = bp.check_heartbeat_age(_hb(1, age_s=10))
        self.assertEqual(r["status"], FAIL)
        self.assertGreaterEqual(r["evidence"]["age_s"], config_threshold())

    def test_unparseable_last_update_fails(self):
        bad = {"status": {"time": {"counter": 1, "last_update": "not-a-date"}}}
        self.assertEqual(bp.check_heartbeat_age(bad)["status"], FAIL)

    def test_fresh_position_passes(self):
        self.assertEqual(bp.check_position_age(_hb(1, age_s=0.5))["status"], PASS)

    def test_stale_position_fails(self):
        self.assertEqual(bp.check_position_age(_hb(1, age_s=30))["status"], FAIL)

    def test_position_fetch_error_fails(self):
        self.assertEqual(bp.check_position_age(None, "timeout")["status"], FAIL)


def config_threshold():
    return bp.config.BENCH_HEARTBEAT_MAX_AGE_S


# ── armed / mode ──────────────────────────────────────────────────────────────

class TestArmedAndMode(unittest.TestCase):
    def test_disarmed_passes(self):
        self.assertEqual(bp.check_disarmed({"armed": False})["status"], PASS)

    def test_armed_fails(self):
        self.assertEqual(bp.check_disarmed({"armed": True})["status"], FAIL)

    def test_unknown_armed_fails(self):
        self.assertEqual(bp.check_disarmed({"armed": None})["status"], FAIL)

    def test_state_fetch_error_fails_disarmed(self):
        self.assertEqual(bp.check_disarmed(None, "unreachable")["status"], FAIL)

    def test_manual_mode_passes(self):
        self.assertEqual(bp.check_mode_manual({"mode_name": "MANUAL"})["status"], PASS)

    def test_non_manual_mode_fails(self):
        r = bp.check_mode_manual({"mode_name": "AUTO"})
        self.assertEqual(r["status"], FAIL)

    def test_unknown_mode_fails(self):
        self.assertEqual(bp.check_mode_manual({"mode_name": None})["status"], FAIL)


# ── flask / authority ─────────────────────────────────────────────────────────

class TestFlaskAndAuthority(unittest.TestCase):
    def test_flask_reachable_passes(self):
        self.assertEqual(bp.check_flask({"state_timestamp": 1.0})["status"], PASS)

    def test_flask_unreachable_fails(self):
        self.assertEqual(bp.check_flask(None, "conn refused")["status"], FAIL)

    def test_authority_operator_passes(self):
        self.assertEqual(bp.check_authority("OPERATOR")["status"], PASS)

    def test_authority_local_agent_fails(self):
        self.assertEqual(bp.check_authority("LOCAL_AGENT")["status"], FAIL)

    def test_authority_fetch_error_fails(self):
        self.assertEqual(bp.check_authority(None, "timeout")["status"], FAIL)


# ── local agent / operator reachability ───────────────────────────────────────

class TestLocalAgentAndOperator(unittest.TestCase):
    def test_agent_not_running_skips(self):
        r = bp.check_local_agent(running=False, responded=False)
        self.assertEqual(r["status"], SKIP)

    def test_agent_running_and_responding_passes(self):
        self.assertEqual(
            bp.check_local_agent(running=True, responded=True)["status"], PASS
        )

    def test_agent_running_not_responding_fails(self):
        self.assertEqual(
            bp.check_local_agent(running=True, responded=False)["status"], FAIL
        )

    def test_operator_any_reachable_passes(self):
        r = bp.check_operator([("http://a:1", False), ("http://b:2", True)])
        self.assertEqual(r["status"], PASS)

    def test_operator_none_reachable_fails(self):
        r = bp.check_operator([("http://a:1", False), ("http://b:2", False)])
        self.assertEqual(r["status"], FAIL)

    def test_operator_none_configured_fails(self):
        self.assertEqual(bp.check_operator([])["status"], FAIL)


# ── mission readback ──────────────────────────────────────────────────────────

def _good_mission():
    return {
        "contract_version": "mission-contract-v1",
        "mission_valid": True,
        "pixhawk_item_count": 4,
        "route_waypoint_count": 3,
        "route_content_hash": "sha256:aaa",
        "full_mission_hash": "sha256:bbb",
        "current_seq": 0,
        "generation": 3,
        "reachable": True,
        "partial": False,
        "stale": False,
        "cached": False,
        "error": None,
    }


class TestMissionReadback(unittest.TestCase):
    def test_complete_contract_passes(self):
        self.assertEqual(bp.check_mission_readback(_good_mission())["status"], PASS)

    def test_empty_mission_still_passes(self):
        m = _good_mission()
        m.update({"pixhawk_item_count": 0, "route_waypoint_count": 0, "current_seq": 0})
        self.assertEqual(bp.check_mission_readback(m)["status"], PASS)

    def test_missing_contract_field_fails(self):
        m = _good_mission()
        m["route_content_hash"] = None
        r = bp.check_mission_readback(m)
        self.assertEqual(r["status"], FAIL)
        self.assertIn("route_content_hash", r["evidence"]["missing_contract_fields"])

    def test_wrong_contract_version_fails(self):
        m = _good_mission()
        m["contract_version"] = "legacy"
        self.assertEqual(bp.check_mission_readback(m)["status"], FAIL)

    def test_partial_fails(self):
        m = _good_mission()
        m["partial"] = True
        self.assertEqual(bp.check_mission_readback(m)["status"], FAIL)

    def test_invalid_mission_fails(self):
        m = _good_mission()
        m["mission_valid"] = False
        self.assertEqual(bp.check_mission_readback(m)["status"], FAIL)

    def test_stale_but_complete_warns(self):
        m = _good_mission()
        m["stale"] = True
        self.assertEqual(bp.check_mission_readback(m)["status"], WARN)

    def test_error_field_fails(self):
        self.assertEqual(
            bp.check_mission_readback({"error": "download timeout"})["status"], FAIL
        )

    def test_fetch_error_fails(self):
        self.assertEqual(bp.check_mission_readback(None, "unreachable")["status"], FAIL)


# ── home verification ─────────────────────────────────────────────────────────

class TestHome(unittest.TestCase):
    def test_verified_passes(self):
        home = {"verified": True, "reachable": True, "verification_distance_m": 0.01,
                "verification_method": "set_home_current_position"}
        self.assertEqual(bp.check_home(home)["status"], PASS)

    def test_not_verified_warns(self):
        home = {"verified": False, "reachable": True, "reason": "no home set"}
        self.assertEqual(bp.check_home(home)["status"], WARN)

    def test_unreachable_fails(self):
        self.assertEqual(bp.check_home({"reachable": False})["status"], FAIL)

    def test_error_fails(self):
        self.assertEqual(bp.check_home({"error": "boom"})["status"], FAIL)

    def test_fetch_error_fails(self):
        self.assertEqual(bp.check_home(None, "timeout")["status"], FAIL)


# ── unresolved mission operation ──────────────────────────────────────────────

class TestUnresolvedMissionOp(unittest.TestCase):
    def test_idle_passes(self):
        r = bp.check_no_unresolved_mission_op({"state": "IDLE", "error": None})
        self.assertEqual(r["status"], PASS)

    def test_completed_passes(self):
        r = bp.check_no_unresolved_mission_op({"state": "COMPLETED", "error": None})
        self.assertEqual(r["status"], PASS)

    def test_in_flight_executing_fails(self):
        r = bp.check_no_unresolved_mission_op({"state": "EXECUTING", "error": None})
        self.assertEqual(r["status"], FAIL)

    def test_delivering_result_fails(self):
        r = bp.check_no_unresolved_mission_op(
            {"state": mission_operation_status.STATE_DELIVERING_RESULT, "error": None}
        )
        self.assertEqual(r["status"], FAIL)

    def test_unknown_after_restart_fails(self):
        record = {"state": "FAILED", "error": {"code": "UNKNOWN_AFTER_RESTART"}}
        r = bp.check_no_unresolved_mission_op(record)
        self.assertEqual(r["status"], FAIL)

    def test_plain_failed_warns(self):
        record = {"state": "FAILED", "error": {"code": "VALIDATION_REJECTED"}}
        r = bp.check_no_unresolved_mission_op(record)
        self.assertEqual(r["status"], WARN)


# ── outbound buffer (items 4 & 5: surfaced read-only, never dropped) ──────────

class TestOutboundBuffer(unittest.TestCase):
    def test_empty_buffer_passes(self):
        r = bp.check_outbound_buffer([], [])
        self.assertEqual(r["status"], PASS)

    def test_buffered_command_result_warns(self):
        msgs = [{"message_type": "command_result", "payload": {"command_id": "x1"}}]
        r = bp.check_outbound_buffer(msgs, [])
        self.assertEqual(r["status"], WARN)
        self.assertEqual(r["evidence"]["buffered_command_results"], 1)
        self.assertIn("x1", r["evidence"]["buffered_command_result_ids"])

    def test_retained_results_warn_even_with_empty_buffer(self):
        r = bp.check_outbound_buffer([], ["r1", "r2"])
        self.assertEqual(r["status"], WARN)
        self.assertEqual(r["evidence"]["retained_command_result_ids"], ["r1", "r2"])

    def test_non_command_result_messages_ignored(self):
        msgs = [{"message_type": "status", "payload": {}}]
        r = bp.check_outbound_buffer(msgs, [])
        self.assertEqual(r["status"], PASS)
        self.assertEqual(r["evidence"]["buffered_messages_total"], 1)


# ── summarize / run_all aggregation ───────────────────────────────────────────

class TestSummarize(unittest.TestCase):
    def test_any_fail_makes_overall_fail(self):
        checks = [bp._result("a", PASS, ""), bp._result("b", FAIL, "")]
        s = bp.summarize(checks)
        self.assertEqual(s["overall"], FAIL)
        self.assertFalse(s["ready"])

    def test_warn_and_skip_do_not_fail_overall(self):
        checks = [bp._result("a", PASS, ""), bp._result("b", WARN, ""),
                  bp._result("c", SKIP, "")]
        s = bp.summarize(checks)
        self.assertEqual(s["overall"], PASS)
        self.assertTrue(s["ready"])
        self.assertEqual(s["counts"][WARN], 1)
        self.assertEqual(s["counts"][SKIP], 1)

    def test_summary_is_json_serializable(self):
        s = bp.summarize([bp._result("a", PASS, "ok", {"n": 1})])
        json.dumps(s)  # must not raise


class TestRunAll(unittest.TestCase):
    """run_all with every fetcher patched -- no live services touched, and
    sample_interval forced to 0 so the test does not sleep."""

    def _patches(self, hb_a, hb_b, pos, state, authority, pix, home,
                 running, operator, mission_op, buffered, retained):
        def fake_read_mavlink(msg_type):
            return {"HEARTBEAT": None, "GLOBAL_POSITION_INT": pos}[msg_type]

        # HEARTBEAT is fetched twice; return a/b in order.
        hb_seq = iter([hb_a, hb_b])

        def read_mavlink(msg_type):
            if msg_type == "HEARTBEAT":
                return next(hb_seq)
            return pos

        return [
            patch("bench_preflight._read_mavlink_message", side_effect=read_mavlink),
            patch("bench_preflight.get_vehicle_state", return_value=state),
            patch("bench_preflight.get_control_authority", return_value=authority),
            patch("bench_preflight.get_pixhawk_mission", return_value=pix),
            patch("bench_preflight.get_home_status", return_value=home),
            patch("bench_preflight._local_agent_running", return_value=running),
            patch("bench_preflight._probe_http", return_value=True),
            patch("bench_preflight._operator_reachability", return_value=operator),
            patch("bench_preflight.mission_operation_status.get", return_value=mission_op),
            patch("bench_preflight.read_buffered_messages", return_value=buffered),
            patch("bench_preflight._retained_command_result_ids", return_value=retained),
        ]

    def test_all_green_overall_pass(self):
        state = {"state_timestamp": 1.0,
                 "telemetry": {"armed": False, "mode_name": "MANUAL"}}
        patches = self._patches(
            hb_a=_hb(10), hb_b=_hb(12), pos=_hb(1, age_s=0.2),
            state=state, authority="OPERATOR", pix=_good_mission(),
            home={"verified": True, "reachable": True,
                  "verification_distance_m": 0.01, "verification_method": "m"},
            running=False,
            operator=[("http://op:1", True)],
            mission_op={"state": "IDLE", "error": None},
            buffered=[], retained=[],
        )
        for p in patches:
            p.start()
        try:
            record = bp.run_all(sample_interval=0)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(record["overall"], PASS)
        self.assertTrue(record["ready"])
        names = {c["name"]: c["status"] for c in record["checks"]}
        self.assertEqual(names["heartbeat_counter_advancing"], PASS)
        self.assertEqual(names["disarmed"], PASS)
        self.assertEqual(names["mode_manual"], PASS)
        self.assertEqual(names["authority_operator"], PASS)
        self.assertEqual(names["mission_readback"], PASS)
        self.assertEqual(names["local_agent_8090"], SKIP)
        json.dumps(record)

    def test_armed_and_stalled_counter_overall_fail(self):
        state = {"state_timestamp": 1.0,
                 "telemetry": {"armed": True, "mode_name": "AUTO"}}
        patches = self._patches(
            hb_a=_hb(10), hb_b=_hb(10), pos=_hb(1, age_s=0.2),
            state=state, authority="LOCAL_AGENT", pix=_good_mission(),
            home={"verified": True, "reachable": True,
                  "verification_distance_m": 0.01, "verification_method": "m"},
            running=True,
            operator=[("http://op:1", False)],
            mission_op={"state": "EXECUTING", "error": None},
            buffered=[], retained=[],
        )
        for p in patches:
            p.start()
        try:
            record = bp.run_all(sample_interval=0)
        finally:
            for p in patches:
                p.stop()

        self.assertEqual(record["overall"], FAIL)
        names = {c["name"]: c["status"] for c in record["checks"]}
        self.assertEqual(names["heartbeat_counter_advancing"], FAIL)
        self.assertEqual(names["disarmed"], FAIL)
        self.assertEqual(names["mode_manual"], FAIL)
        self.assertEqual(names["authority_operator"], FAIL)
        self.assertEqual(names["operator_reachable"], FAIL)
        self.assertEqual(names["no_unresolved_mission_op"], FAIL)

    def test_run_all_does_not_mutate_vehicle(self):
        """Guard rail: run_all must never call a state-mutating api_client
        function. Patch them to explode if touched."""
        state = {"state_timestamp": 1.0,
                 "telemetry": {"armed": False, "mode_name": "MANUAL"}}
        patches = self._patches(
            hb_a=_hb(10), hb_b=_hb(12), pos=_hb(1, age_s=0.2),
            state=state, authority="OPERATOR", pix=_good_mission(),
            home={"verified": True, "reachable": True,
                  "verification_distance_m": 0.01, "verification_method": "m"},
            running=False, operator=[("http://op:1", True)],
            mission_op={"state": "IDLE", "error": None}, buffered=[], retained=[],
        )
        import api_client

        def _boom(*a, **k):
            raise AssertionError("bench_preflight must not mutate vehicle state")

        patches.append(patch.object(api_client, "set_control_authority", _boom))
        patches.append(patch.object(api_client, "send_to_operator", _boom))
        for p in patches:
            p.start()
        try:
            bp.run_all(sample_interval=0)
        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
