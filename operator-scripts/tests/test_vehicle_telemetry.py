"""Regression tests for the canonical vehicle-telemetry normalization.

Every test here names a bug that was ACTUALLY on screen during the bench run: a value
Scout was demonstrably sending that the operator rendered as "— NO TELEM", a partial
update that erased a valid reading, or a derivation that would have turned an unknown
into a reassuring OK.

The live-payload tests run against tests/fixtures/scout-status-live.json — one real
POST /agent/status body captured off the wire from Scout's Local Agent, so the field
spellings under test are the ones actually sent rather than the ones we hoped for.
"""

import json
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import main
import vehicle_telemetry as vt

FIXTURE = Path(__file__).parent / "fixtures" / "scout-status-live.json"
LIVE_PACKET = json.loads(FIXTURE.read_text(encoding="utf-8"))
LIVE_PAYLOAD = LIVE_PACKET["payload"]


# ════════════════════════════════════════════════════════════════════════════════════
# The live payload — what Scout actually sends
# ════════════════════════════════════════════════════════════════════════════════════

class LivePayloadContract(unittest.TestCase):
    """The captured packet proves these groups arrive; if Scout stops sending one, the
    failure must show up here rather than as a silent blank row in the UI."""

    def test_every_expected_group_is_present_in_the_received_packet(self):
        for group in ("telemetry", "power", "failsafe", "imu", "freshness", "mavlink",
                      "mission", "communication", "service_status", "agent", "health"):
            self.assertIn(group, LIVE_PAYLOAD, f"{group} missing from the received packet")

    def test_communication_carries_the_new_link_diagnostics(self):
        comm = LIVE_PAYLOAD["communication"]
        for field in ("operator_connected", "rtt_ms", "seq", "vpn_status"):
            self.assertIn(field, comm, f"communication.{field} missing")


class LiveNormalization(unittest.TestCase):
    """The blocks a live packet produces. These are the exact values the Vehicle page
    should show for usv-2 in the state the fixture was captured in."""

    def test_power_reads_the_canonical_block(self):
        power = vt.power_block(LIVE_PAYLOAD)
        self.assertEqual(power["battery_voltage_v"], 23.8)
        self.assertEqual(power["battery_current_a"], 0.21)
        self.assertEqual(power["battery_remaining_pct"], 89)
        self.assertEqual(power["source"], "PIXHAWK_BATTERY_MONITOR")
        self.assertEqual(power["reported_by"], "power")

    def test_mavlink_reads_scouts_actual_field_spellings(self):
        # The bug: the old normalizer read `connected` / `last_msg_age_s` / `msg_rate_hz`,
        # none of which the Local Agent sends, so a connected autopilot read NO TELEM.
        mav = vt.mavlink_block(LIVE_PAYLOAD)
        self.assertIs(mav["connected"], True)
        self.assertEqual(mav["heartbeat_age_s"], 0.2)
        self.assertEqual(mav["last_msg_age_s"], 0.2)

    def test_imu_is_a_summary_not_the_raw_dictionary(self):
        imu = vt.imu_block(LIVE_PAYLOAD)
        self.assertEqual(imu["health"], "OK")
        self.assertIs(imu["available"], True)
        self.assertNotIn("attitude", imu, "raw attitude must not reach a status row")

    def test_failsafe_status_is_carried_through_verbatim(self):
        self.assertEqual(vt.failsafe_block(LIVE_PAYLOAD)["status"], "OK")

    def test_freshness_exposes_every_stream_plus_the_worst(self):
        fresh = vt.freshness_block(LIVE_PAYLOAD)
        for key in vt.FRESHNESS_KEYS:
            self.assertIsNotNone(fresh[key], key)
        self.assertEqual(fresh["oldest_s"], max(fresh[k] for k in vt.FRESHNESS_KEYS))

    def test_service_status_is_counted_not_dumped(self):
        svc = vt.service_status_block(LIVE_PAYLOAD)
        self.assertEqual(svc["offline"], [])
        self.assertEqual(svc["required_offline"], [])
        self.assertEqual(svc["unknown"], ["influx"], "influx is optional and unknown")

    def test_link_carries_rtt_vpn_operator_connected_and_seq(self):
        link = vt.link_block(LIVE_PAYLOAD)
        self.assertIs(link["operator_connected"], True)
        self.assertEqual(link["rtt_ms"], 331.1)
        self.assertEqual(link["seq"], 302)
        self.assertEqual(link["vpn"]["status"], "RECENT_HANDSHAKE")
        self.assertEqual(link["vpn"]["interface"], "wg0")

    def test_home_is_not_verified_even_though_a_home_position_exists(self):
        home = LIVE_PAYLOAD["agent"]["home_status"]
        self.assertIsNotNone(home["home_position"], "a HOME_POSITION does exist")
        self.assertIs(home["verified"], False, "...and it is still NOT verified")

    def test_mission_is_present_on_the_autopilot(self):
        mission = vt.mission_block(LIVE_PAYLOAD)
        self.assertEqual(mission["mission_count"], 15)
        self.assertIs(mission["mission_present"], True)
        self.assertEqual(mission["current_waypoint_display"], "0 / 15")
        self.assertIsNone(mission["current_mission_id"], "a null id stays null")

    def test_agent_policy_object_is_flattened_to_strings(self):
        # Scout's Flask sends a STRING here; the Local Agent's POST sends an OBJECT.
        self.assertIsInstance(LIVE_PAYLOAD["agent"]["current_policy"], dict)
        summary = vt.agent_summary(LIVE_PAYLOAD)
        self.assertEqual(summary["current_policy"], "FULL_REPORTING")
        self.assertEqual(summary["current_behaviour"], "monitoring")
        self.assertEqual(summary["autonomy_level"], "ASSISTED")
        for key, value in summary.items():
            self.assertNotIsInstance(value, dict, f"{key} must never be an object")

    def test_leak_sensor_is_uncalibrated_not_safe(self):
        leak = vt.leak_sensor_block(LIVE_PAYLOAD)
        self.assertEqual(leak["state"], "UNCALIBRATED")
        self.assertIsNone(leak["leak_detected"])


# ════════════════════════════════════════════════════════════════════════════════════
# Zero / null / unknown semantics
# ════════════════════════════════════════════════════════════════════════════════════

class ZeroAndNullSemantics(unittest.TestCase):

    def test_zero_amps_is_a_reading_not_an_absence(self):
        power = vt.power_block({"power": {"battery_current_a": 0, "battery_voltage_v": 0.0}})
        self.assertEqual(power["battery_current_a"], 0)
        self.assertEqual(power["battery_voltage_v"], 0.0)

    def test_zero_rtt_survives_but_null_rtt_stays_null(self):
        self.assertEqual(vt.link_block({"communication": {"rtt_ms": 0}})["rtt_ms"], 0)
        self.assertIsNone(vt.link_block({"communication": {"rtt_ms": None}})["rtt_ms"])

    def test_waypoint_zero_is_a_real_waypoint(self):
        mission = vt.mission_block({"mission": {"current_waypoint": 0, "mission_count": 15}})
        self.assertEqual(mission["current_waypoint"], 0)
        self.assertIs(mission["mission_present"], True)

    def test_battery_minus_one_is_absence_not_a_reading(self):
        # MAVLink's "unknown" sentinel. Reading it as a value is what made a valid 90%
        # flicker to "—" and back at the poll rate.
        power = vt.power_block({"telemetry": {"battery": -1}})
        self.assertIsNone(power["battery_remaining_pct"])

    def test_booleans_are_never_mistaken_for_numbers(self):
        self.assertIsNone(vt._num(True))
        self.assertIsNone(vt._num(False))

    def test_a_missing_failsafe_is_not_nominal(self):
        self.assertIsNone(vt.failsafe_block({})["status"])
        self.assertIs(vt.failsafe_block({})["reported"], False)

    def test_an_unknown_failsafe_stays_unknown(self):
        self.assertEqual(vt.failsafe_block({"failsafe": {"status": "UNKNOWN"}})["status"],
                         "UNKNOWN")

    def test_mavlink_stays_available_when_msg_rate_is_null(self):
        mav = vt.mavlink_block({"mavlink": {"mavlink_connected": True,
                                            "mavlink_msg_rate_hz": None}})
        self.assertIs(mav["connected"], True)
        self.assertIsNone(mav["msg_rate_hz"])

    def test_mission_present_is_unknown_when_count_is_absent(self):
        self.assertIsNone(vt.mission_block({"mission": {}})["mission_present"])

    def test_a_null_readback_does_not_mean_no_mission(self):
        mission = vt.mission_block({"mission": {"mission_count": 15, "pixhawk_readback": None}})
        self.assertIs(mission["mission_present"], True)
        self.assertIs(mission["readback_available"], False)


class LeakSensorSafety(unittest.TestCase):
    """The single most dangerous row on the page: an uncalibrated leak sensor must never
    read as safe, and a sensor that IS reporting must never read as no-telemetry."""

    def test_uncalibrated_polarity_never_reports_no_leak(self):
        block = vt.leak_sensor_block({"health": {"system": {"leak_sensor": {
            "available": True, "signal": "LOW", "polarity": "uncalibrated",
            "leak_detected": None}}}})
        self.assertEqual(block["state"], "UNCALIBRATED")
        self.assertNotEqual(block["state"], "NO_LEAK")

    def test_a_calibrated_dry_sensor_does_report_no_leak(self):
        block = vt.leak_sensor_block({"health": {"system": {"leak_sensor": {
            "available": True, "signal": "LOW", "polarity": "active_high",
            "leak_detected": False}}}})
        self.assertEqual(block["state"], "NO_LEAK")

    def test_a_detected_leak_wins_over_everything(self):
        block = vt.leak_sensor_block({"health": {"system": {"leak_sensor": {
            "available": True, "polarity": "uncalibrated", "leak_detected": True}}}})
        self.assertEqual(block["state"], "LEAK")

    def test_no_sensor_at_all_is_unreported_not_safe(self):
        self.assertEqual(vt.leak_sensor_block({})["state"], "UNREPORTED")


# ════════════════════════════════════════════════════════════════════════════════════
# Packet-loss estimator
# ════════════════════════════════════════════════════════════════════════════════════

class PacketLoss(unittest.TestCase):

    def feed(self, est, seqs, start=1000.0, step=1.0):
        t = start
        for s in seqs:
            est.observe(s, t)
            t += step
        return t

    def test_unmeasured_until_enough_samples(self):
        est = vt.PacketLossEstimator(min_samples=20)
        t = self.feed(est, range(1, 11))
        result = est.estimate(t)
        self.assertEqual(result["state"], "UNMEASURED")
        self.assertIsNone(result["loss_pct"], "never a fabricated 0% before measurement")

    def test_a_clean_stream_measures_zero_loss(self):
        est = vt.PacketLossEstimator(min_samples=20)
        t = self.feed(est, range(1, 41))
        result = est.estimate(t)
        self.assertEqual(result["state"], "MEASURED")
        self.assertEqual(result["loss_pct"], 0.0)

    def test_sequence_gaps_produce_the_expected_estimate(self):
        est = vt.PacketLossEstimator(min_samples=10)
        # 1..40 with every 10th missing: 4 lost out of 40 expected.
        t = self.feed(est, [s for s in range(1, 41) if s % 10 != 0])
        result = est.estimate(t)
        self.assertEqual(result["expected"], 39)   # 1..39 inclusive (40 never arrived)
        self.assertEqual(result["received"], 36)
        self.assertEqual(result["lost"], 3)
        self.assertAlmostEqual(result["loss_pct"], round(100 * 3 / 39, 1))

    def test_duplicates_are_not_counted_twice(self):
        est = vt.PacketLossEstimator(min_samples=10)
        t = self.feed(est, list(range(1, 21)) + list(range(1, 21)))
        result = est.estimate(t)
        self.assertEqual(result["received"], 20, "a retransmit is not a second delivery")
        self.assertEqual(result["loss_pct"], 0.0)
        self.assertEqual(result["duplicates"], 20)

    def test_out_of_order_arrival_fills_its_gap_instead_of_creating_loss(self):
        est = vt.PacketLossEstimator(min_samples=10)
        order = [s for s in range(1, 21) if s != 7] + [7]   # 7 arrives late, in-window
        t = self.feed(est, order)
        result = est.estimate(t)
        self.assertEqual(result["lost"], 0, "a late packet must reduce, never add, loss")
        self.assertEqual(result["resets"], 0)

    def test_a_counter_restart_does_not_report_massive_loss(self):
        est = vt.PacketLossEstimator(min_samples=10)
        t = self.feed(est, range(500, 540))
        est.observe(1, t)                       # Local Agent restarted; seq back to 1
        t += 1
        # Immediately after the restart the window has been discarded, so the honest
        # answer is "measuring again" — NOT "we just lost 499 packets".
        restarted = est.estimate(t)
        self.assertEqual(restarted["resets"], 1)
        self.assertEqual(restarted["state"], "UNMEASURED")
        self.assertIsNone(restarted["loss_pct"])
        # Once the window refills it measures normally, from the NEW counter base.
        for s in range(2, 15):
            est.observe(s, t)
            t += 1
        refilled = est.estimate(t)
        self.assertEqual(refilled["state"], "MEASURED")
        self.assertEqual(refilled["loss_pct"], 0.0, "the restart is not retroactive loss")

    def test_a_huge_forward_jump_is_treated_as_a_reinit(self):
        est = vt.PacketLossEstimator(min_samples=10)
        t = self.feed(est, range(1, 21))
        est.observe(999999, t)
        result = est.estimate(t)
        self.assertEqual(result["resets"], 1)
        self.assertNotEqual(result.get("loss_pct"), 99.9)

    def test_an_outage_ages_out_instead_of_reporting_as_loss(self):
        est = vt.PacketLossEstimator(window_s=120.0, min_samples=10)
        t = self.feed(est, range(1, 31))
        # Ten minutes of silence, then the stream resumes with a large REAL seq gap:
        # the vehicle kept counting while we could not hear it.
        t += 600
        # Straight after the reconnect there is not enough recent history to say anything.
        self.assertEqual(est.estimate(t)["state"], "UNMEASURED")
        for s in range(631, 646):
            est.observe(s, t)
            t += 1
        result = est.estimate(t)
        # The pre-outage samples have aged out of the 120 s window, so the estimate covers
        # only the resumed stream. A disconnection is a disconnection — it must not be
        # laundered into a 95% packet-loss figure for a link that is now clean.
        self.assertEqual(result["state"], "MEASURED")
        self.assertEqual(result["loss_pct"], 0.0)
        self.assertEqual(result["received"], 15)

    def test_a_vehicle_that_sends_no_seq_never_becomes_measurable(self):
        est = vt.PacketLossEstimator(min_samples=5)
        for _ in range(50):
            est.observe(None, 1000.0)
        self.assertEqual(est.estimate(1000.0)["state"], "UNMEASURED")

    def test_estimators_are_per_vehicle(self):
        a, b = vt.PacketLossEstimator(min_samples=5), vt.PacketLossEstimator(min_samples=5)
        self.feed(a, range(1, 21))
        self.feed(b, [1, 50, 99])
        self.assertEqual(a.estimate(1100.0)["loss_pct"], 0.0)
        self.assertEqual(b.estimate(1100.0)["state"], "UNMEASURED")


# ════════════════════════════════════════════════════════════════════════════════════
# Partial-update merge semantics
# ════════════════════════════════════════════════════════════════════════════════════

class GroupMerge(unittest.TestCase):

    def test_a_group_present_in_the_packet_replaces_the_stored_one(self):
        store = {}
        vt.observe_groups(store, 2, {"power": {"battery_voltage_v": 23.8, "brick_valid": True}})
        vt.observe_groups(store, 2, {"power": {"battery_voltage_v": 22.1}})
        value, stale = vt.effective_group(store, 2, {"power": {"battery_voltage_v": 22.1}}, "power")
        self.assertEqual(value, {"battery_voltage_v": 22.1})
        self.assertNotIn("brick_valid", value,
                         "a present group is authoritative — no field resurrection")
        self.assertFalse(stale)

    def test_a_group_absent_from_the_packet_falls_back_and_is_marked_stale(self):
        store = {}
        vt.observe_groups(store, 2, {"power": {"battery_voltage_v": 23.8}})
        value, stale = vt.effective_group(store, 2, {"mission": {"mission_count": 15}}, "power")
        self.assertEqual(value, {"battery_voltage_v": 23.8})
        self.assertTrue(stale)

    def test_an_empty_group_does_not_overwrite_a_real_snapshot(self):
        store = {}
        vt.observe_groups(store, 2, {"imu": {"imu_health": "OK"}})
        vt.observe_groups(store, 2, {"imu": {}})
        self.assertEqual(store[2]["imu"], {"imu_health": "OK"})

    def test_groups_are_isolated_per_vehicle(self):
        store = {}
        vt.observe_groups(store, 2, {"power": {"battery_voltage_v": 23.8}})
        vt.observe_groups(store, 3, {"power": {"battery_voltage_v": 11.1}})
        self.assertEqual(store[2]["power"]["battery_voltage_v"], 23.8)
        self.assertEqual(store[3]["power"]["battery_voltage_v"], 11.1)
        value, _ = vt.effective_group(store, 3, {}, "power")
        self.assertEqual(value["battery_voltage_v"], 11.1)


# ════════════════════════════════════════════════════════════════════════════════════
# End-to-end through the real ingest + fleet endpoints
# ════════════════════════════════════════════════════════════════════════════════════

def packet(vid, *, seq=None, **groups):
    payload = {"usv_id": vid, "name": f"USV-{vid}", "comm_state": "CONNECTED"}
    payload.update(groups)
    if seq is not None:
        payload.setdefault("communication", {})["seq"] = seq
    return {"message_type": "status", "source": f"usv-{vid}", "payload": payload}


class EndToEnd(unittest.TestCase):
    """A real POST through the real route, read back off GET /api/fleet/status."""

    def setUp(self):
        self.client = TestClient(main.app)
        for store in (main.current_vehicle_state, main.last_known_telemetry,
                      main.last_known_agent, main.last_known_groups,
                      main.packet_loss_by_id, main.latest_msg_ts_by_id,
                      main.last_seen_by_id, main.comms_state_by_id):
            store.clear()

    def row(self, vid):
        fleet = self.client.get("/api/fleet/status").json()
        return next(v for v in fleet if v["id"] == vid)

    def test_a_live_scout_packet_populates_every_canonical_block(self):
        self.client.post("/agent/status", json=LIVE_PACKET)
        row = self.row(2)
        self.assertEqual(row["power"]["battery_voltage_v"], 23.8)
        self.assertEqual(row["power"]["battery_current_a"], 0.21)
        self.assertEqual(row["failsafe"]["status"], "OK")
        self.assertEqual(row["imu"]["health"], "OK")
        self.assertIs(row["mavlink"]["connected"], True)
        self.assertEqual(row["telemetry"]["gps_satellites"], 20)
        self.assertIs(row["telemetry"]["ekf_ok"], True)
        self.assertEqual(row["health"]["temperature"], 40.8)
        self.assertEqual(row["mission_status"]["mission_count"], 15)
        self.assertIs(row["home"]["verified"], False)
        self.assertEqual(row["agent_summary"]["current_policy"], "FULL_REPORTING")
        self.assertEqual(row["agent_summary"]["current_behaviour"], "monitoring")
        self.assertEqual(row["leak_sensor"]["state"], "UNCALIBRATED")
        self.assertIs(row["link"]["operator_connected"], True)
        self.assertEqual(row["link"]["vpn"]["status"], "RECENT_HANDSHAKE")
        self.assertEqual(row["service_status"]["required_offline"], [])

    def test_a_partial_gps_update_does_not_erase_power(self):
        self.client.post("/agent/status", json=packet(2, power={"battery_voltage_v": 23.8}))
        self.client.post("/agent/status", json=packet(2, telemetry={"lat": 56.1, "lng": 12.1}))
        row = self.row(2)
        self.assertEqual(row["power"]["battery_voltage_v"], 23.8)
        self.assertIn("power", row["stale_groups"])

    def test_a_partial_health_update_does_not_erase_telemetry(self):
        self.client.post("/agent/status",
                         json=packet(2, telemetry={"gps_satellites": 22, "ekf_ok": True}))
        self.client.post("/agent/status", json=packet(2, health={"cpu_load": 3.2}))
        row = self.row(2)
        self.assertEqual(row["telemetry"]["gps_satellites"], 22)
        self.assertIs(row["telemetry"]["ekf_ok"], True)
        self.assertEqual(row["health"]["cpu_load"], 3.2)

    def test_a_partial_mission_update_does_not_erase_agent_state(self):
        self.client.post("/agent/status", json=packet(
            2, agent={"current_policy": {"communication_policy": "FULL_REPORTING",
                                         "current_behaviour": "monitoring"}}))
        self.client.post("/agent/status", json=packet(2, mission={"mission_count": 15}))
        row = self.row(2)
        self.assertEqual(row["agent_summary"]["current_behaviour"], "monitoring")
        self.assertEqual(row["mission_status"]["mission_count"], 15)

    def test_two_vehicles_never_share_telemetry_or_loss_state(self):
        self.client.post("/agent/status", json=packet(
            2, seq=1, power={"battery_voltage_v": 23.8}, imu={"imu_health": "OK"}))
        self.client.post("/agent/status", json=packet(
            3, seq=900, power={"battery_voltage_v": 11.1}, imu={"imu_health": "WARNING"}))
        two, three = self.row(2), self.row(3)
        self.assertEqual(two["power"]["battery_voltage_v"], 23.8)
        self.assertEqual(three["power"]["battery_voltage_v"], 11.1)
        self.assertEqual(two["imu"]["health"], "OK")
        self.assertEqual(three["imu"]["health"], "WARNING")
        self.assertIsNot(main.packet_loss_by_id[2], main.packet_loss_by_id[3])

    def test_packet_loss_is_measured_per_vehicle_from_the_sequence_number(self):
        for seq in range(1, 41):
            if seq % 10 == 0:
                continue                                  # 4 packets never arrive
            self.client.post("/agent/status", json=packet(2, seq=seq))
        loss = self.row(2)["link"]["packet_loss"]
        self.assertEqual(loss["state"], "MEASURED")
        self.assertGreater(loss["loss_pct"], 0)
        self.assertEqual(loss["lost"], 3)

    def test_packet_loss_stays_unmeasured_before_enough_samples(self):
        for seq in range(1, 6):
            self.client.post("/agent/status", json=packet(2, seq=seq))
        loss = self.row(2)["link"]["packet_loss"]
        self.assertEqual(loss["state"], "UNMEASURED")
        self.assertIsNone(loss["loss_pct"])

    def test_a_never_contacted_vehicle_has_the_same_shape_as_a_live_one(self):
        live_keys = set(main.normalize_agent_message(LIVE_PACKET, cid=2).keys())
        template_keys = set(main.never_contacted_row(3).keys())
        self.assertEqual(live_keys - template_keys, set(),
                         "a never-contacted row must not be missing keys a live row has")


if __name__ == "__main__":
    unittest.main()
