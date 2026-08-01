"""Bounded [STATUS] diagnostics — one line per meaningful CHANGE, not per packet.

With two USVs posting at ~1 Hz, a line per packet is ~120 lines a minute of identical text,
and the transitions that actually matter ([COMMS], [EVENT], a vehicle whose packets start
being rejected) drown in it. [STATUS] is now change-driven and deduplicated PER VEHICLE:
first contact, any change in this vehicle's status signature, and otherwise at most one
heartbeat line per STATUS_HEARTBEAT_SECONDS.

These tests capture real stdout from the real endpoint — not a mock logger — so they pin the
behaviour an operator actually sees in the terminal.
"""
import io
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

import main
from fastapi.testclient import TestClient

SCOUT = 2
SAR = 3


def packet(usv_id, *, ts, name=None, comm="CONNECTED", mode="AUTO", mission="EXECUTING",
           battery=79, source=None, armed=True):
    return {"message_type": "status", "schema_version": "1.0",
            "source": source or f"usv-{usv_id}", "target": "operator", "timestamp": ts,
            "payload": {"usv_id": usv_id, "name": name, "comm_state": comm,
                        "telemetry": {"battery": battery, "mode": mode, "armed": armed},
                        "mission": {"mission_state": mission}}}


class StatusLoggingBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.current_vehicle_state.clear()
        main.last_known_telemetry.clear()
        main.last_known_agent.clear()
        main.latest_msg_ts_by_id.clear()
        main.last_seen_by_id.clear()
        main.comms_state_by_id.clear()
        main.comms_history_by_id.clear()
        main._status_log_state.clear()
        main._unidentified_log_at = None
        main._last_fleet_summary = None
        main.vehicle_names = {c: main.REGISTRY.default_display_name(c)
                              for c in main.REGISTRY.configured_ids()}

    def post_capture(self, *packets):
        """POST packets and return only the [STATUS] lines they printed."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            for pkt in packets:
                self.client.post("/agent/status", json=pkt)
        return [ln for ln in buf.getvalue().splitlines() if ln.startswith("[STATUS]")]

    def lines_for(self, lines, cid):
        return [ln for ln in lines if f"canonical_id={main.vehicle_slug(cid)}" in ln]


class BoundedStatusLines(StatusLoggingBase):

    def test_first_accepted_packet_logs_once(self):
        lines = self.post_capture(packet(2, ts=1000, name="Scout"))
        self.assertEqual(len(lines), 1, lines)
        self.assertIn("canonical_id=usv-2", lines[0])
        self.assertIn("accepted=true", lines[0])
        self.assertIn("(first-contact)", lines[0])

    def test_repeated_unchanged_accepted_packets_do_not_repeat_the_line(self):
        pkts = [packet(2, ts=1000 + i, name="Scout", battery=79 - i) for i in range(30)]
        lines = self.post_capture(*pkts)
        self.assertEqual(len(lines), 1,
                         f"30 unchanged packets must print once, got {len(lines)}: {lines}")

    def test_a_steady_two_usv_stream_is_two_lines_not_sixty(self):
        pkts = []
        for i in range(30):
            pkts.append(packet(2, ts=1000 + i, name="Scout"))
            pkts.append(packet(3, ts=1000 + i, name="SAR-001", mode="MANUAL", mission="IDLE"))
        lines = self.post_capture(*pkts)
        self.assertEqual(len(lines), 2, f"expected one first-contact line each: {lines}")
        self.assertEqual(len(self.lines_for(lines, SCOUT)), 1)
        self.assertEqual(len(self.lines_for(lines, SAR)), 1)

    def test_a_changing_telemetry_value_alone_is_not_a_change(self):
        """Battery/position vary continuously; they are the UI's job, not the log's."""
        self.post_capture(packet(2, ts=1000, name="Scout", battery=79))
        lines = self.post_capture(*[packet(2, ts=1001 + i, name="Scout", battery=70 - i)
                                    for i in range(10)])
        self.assertEqual(lines, [])

    def test_mode_change_logs(self):
        self.post_capture(packet(2, ts=1000, name="Scout", mode="AUTO"))
        lines = self.post_capture(packet(2, ts=1001, name="Scout", mode="LOITER"))
        self.assertEqual(len(lines), 1)
        self.assertIn("(change)", lines[0])

    def test_mission_state_change_logs(self):
        self.post_capture(packet(2, ts=1000, name="Scout", mission="EXECUTING"))
        lines = self.post_capture(packet(2, ts=1001, name="Scout", mission="PAUSED"))
        self.assertEqual(len(lines), 1)
        self.assertIn("mission=PAUSED", lines[0])

    def test_communication_state_transition_logs(self):
        self.post_capture(packet(2, ts=1000, name="Scout", comm="CONNECTED"))
        lines = self.post_capture(packet(2, ts=1001, name="Scout", comm="PARTITIONED"))
        self.assertEqual(len(lines), 1)
        self.assertIn("comm=PARTITIONED", lines[0])

    def test_identity_resolution_change_logs(self):
        """The same canonical vehicle suddenly identifying itself differently is exactly the
        kind of thing that must not pass silently."""
        self.post_capture(packet(3, ts=1000, name="SAR-001", source="usv-3"))
        lines = self.post_capture(packet(3, ts=1001, name="SAR-001", source="SAR-001"))
        self.assertEqual(len(lines), 1)
        self.assertIn("source=SAR-001", lines[0])

    def test_a_heartbeat_line_appears_at_the_configured_low_rate(self):
        self.post_capture(packet(2, ts=1000, name="Scout"))
        # Nothing changes; only the passage of time may produce another line.
        state = main._status_log_state[SCOUT]
        state["at"] = datetime.now(timezone.utc) - timedelta(
            seconds=main.STATUS_HEARTBEAT_SECONDS + 1)
        lines = self.post_capture(packet(2, ts=1001, name="Scout"))
        self.assertEqual(len(lines), 1)
        self.assertIn("(heartbeat)", lines[0])
        # …and immediately falls silent again.
        self.assertEqual(self.post_capture(packet(2, ts=1002, name="Scout")), [])

    def test_never_prints_the_payload(self):
        lines = self.post_capture(packet(2, ts=1000, name="Scout", battery=79))
        for ln in lines:
            self.assertNotIn("telemetry", ln)
            self.assertNotIn("payload", ln)
            self.assertLess(len(ln), 200, "a status line stays one compact line")


class RejectionLogging(StatusLoggingBase):

    def test_accepted_to_rejected_logs_with_a_reason_and_evidence(self):
        self.post_capture(packet(2, ts=2000, name="Scout"))
        lines = self.post_capture(packet(2, ts=1500, name="Scout"))     # replayed
        self.assertEqual(len(lines), 1)
        self.assertIn("accepted=false", lines[0])
        self.assertIn("reason=stale_timestamp", lines[0])
        self.assertIn("prev_ts=2000", lines[0])
        self.assertIn("msg_ts=1500", lines[0])
        self.assertIn("delta_s=-500", lines[0])

    def test_repeated_identical_rejections_are_suppressed(self):
        self.post_capture(packet(2, ts=2000, name="Scout"))
        first = self.post_capture(packet(2, ts=1500, name="Scout"))
        repeats = self.post_capture(*[packet(2, ts=1500, name="Scout") for _ in range(20)])
        self.assertEqual(len(first), 1)
        self.assertEqual(repeats, [], "a 1 Hz rejection storm must print once, not 20 times")

    def test_rejected_to_accepted_recovery_logs(self):
        self.post_capture(packet(2, ts=2000, name="Scout"))
        self.post_capture(packet(2, ts=1500, name="Scout"))
        lines = self.post_capture(packet(2, ts=2001, name="Scout"))
        self.assertEqual(len(lines), 1)
        self.assertIn("accepted=true", lines[0])

    def test_logging_state_is_independent_per_usv(self):
        self.post_capture(packet(2, ts=2000, name="Scout"),
                          packet(3, ts=2000, name="SAR-001", mode="MANUAL", mission="IDLE"))
        # SAR starts being rejected; Scout keeps streaming unchanged and stays silent.
        lines = self.post_capture(
            packet(3, ts=1000, name="SAR-001", mode="MANUAL", mission="IDLE"),
            packet(2, ts=2001, name="Scout"),
            packet(3, ts=1000, name="SAR-001", mode="MANUAL", mission="IDLE"),
            packet(2, ts=2002, name="Scout"),
        )
        self.assertEqual(len(self.lines_for(lines, SAR)), 1, "SAR's rejection logs once")
        self.assertEqual(self.lines_for(lines, SCOUT), [],
                         "SAR's rejections must not make Scout log, or unsuppress it")

    def test_one_vehicles_change_does_not_reset_anothers_dedup(self):
        self.post_capture(packet(2, ts=2000, name="Scout"),
                          packet(3, ts=2000, name="SAR-001", mode="MANUAL", mission="IDLE"))
        lines = self.post_capture(
            packet(3, ts=2001, name="SAR-001", mode="HOLD", mission="IDLE"),   # SAR changes
            packet(2, ts=2001, name="Scout"),                                  # Scout unchanged
        )
        self.assertEqual(len(self.lines_for(lines, SAR)), 1)
        self.assertEqual(self.lines_for(lines, SCOUT), [])

    def test_unidentified_packets_are_rate_limited(self):
        bad = {"message_type": "status", "payload": {"comm_state": "CONNECTED"}}
        buf = io.StringIO()
        with redirect_stdout(buf):
            for _ in range(15):
                r = self.client.post("/agent/status", json=bad)
                self.assertEqual(r.status_code, 400)
        lines = [ln for ln in buf.getvalue().splitlines() if ln.startswith("[STATUS]")]
        self.assertEqual(len(lines), 1, f"an unidentified-packet storm logs once: {lines}")
        self.assertIn("reason=unidentified_vehicle", lines[0])


class FleetSummaryStaysChangeDriven(StatusLoggingBase):

    def _fleet_lines(self, polls):
        buf = io.StringIO()
        with redirect_stdout(buf):
            for _ in range(polls):
                self.client.get("/api/fleet/status")
        return [ln for ln in buf.getvalue().splitlines() if ln.startswith("[FLEET]")]

    def test_repeated_polls_with_no_change_print_nothing_new(self):
        self.post_capture(packet(2, ts=1000, name="Scout"))
        first = self._fleet_lines(1)
        self.assertEqual(len(first), 1)
        self.assertEqual(self._fleet_lines(20), [], "a 2 s poll must not print 20 lines")

    def test_a_new_vehicle_connecting_prints_one_updated_summary(self):
        self.post_capture(packet(2, ts=1000, name="Scout"))
        self._fleet_lines(1)
        self.post_capture(packet(3, ts=1000, name="SAR-001", mode="MANUAL", mission="IDLE"))
        lines = self._fleet_lines(3)
        self.assertEqual(len(lines), 1)
        self.assertIn("connected=2", lines[0])


class RejectionEvidenceIsInspectable(StatusLoggingBase):
    """Item 3's diagnostic surface: an 'accepted=false for a while' episode must be
    diagnosable after the fact, not only from terminal scrollback."""

    def test_agent_status_exposes_per_usv_counters_and_the_last_rejection(self):
        self.post_capture(packet(2, ts=2000, name="Scout"),
                          packet(3, ts=5000, name="SAR-001", mode="MANUAL", mission="IDLE"))
        self.post_capture(*[packet(3, ts=1200, name="SAR-001", mode="MANUAL", mission="IDLE")
                            for _ in range(4)])
        vehicles = self.client.get("/agent/status").json()["vehicles"]
        sar = vehicles["usv-3"]
        self.assertEqual(sar["accepted_packets"], 1)
        self.assertEqual(sar["rejected_packets"], 4)
        self.assertEqual(sar["last_reject"]["reason"], "stale_timestamp")
        self.assertEqual(sar["last_reject"]["accepted_ts"], 5000)
        self.assertEqual(sar["last_reject"]["packet_ts"], 1200)
        self.assertEqual(sar["last_reject"]["delta_s"], -3800,
                         "the size of the backward step is what distinguishes a replayed "
                         "packet from a time base that restarted")
        self.assertEqual(sar["reject_streak"], 4, "still rejecting right now")
        # Scout is untouched by SAR's trouble.
        self.assertEqual(vehicles["usv-2"]["rejected_packets"], 0)
        self.assertEqual(vehicles["usv-2"]["reject_streak"], 0)
        self.assertIsNone(vehicles["usv-2"]["last_reject"])

    def test_recovery_ends_the_streak_but_keeps_the_evidence(self):
        """An episode is investigated after it recovers — evidence that deletes itself on
        recovery is evidence you never get to read."""
        self.post_capture(packet(3, ts=5000, name="SAR-001"),
                          *[packet(3, ts=1200, name="SAR-001") for _ in range(3)])
        rec = main.current_vehicle_state[SAR]
        self.assertEqual(rec["reject_streak"], 3)
        lines = self.post_capture(packet(3, ts=5001, name="SAR-001"))
        self.assertEqual(rec["reject_streak"], 0, "the streak ends on recovery")
        self.assertIsNotNone(rec["last_reject"], "…but the evidence survives it")
        self.assertEqual(rec["last_reject"]["delta_s"], -3800)
        self.assertEqual(len(lines), 1)
        self.assertIn("recovered_after=3", lines[0],
                      "the log closes the episode with how many packets were lost")

    def test_the_response_body_names_the_rejection_reason(self):
        self.client.post("/agent/status", json=packet(2, ts=2000, name="Scout"))
        r = self.client.post("/agent/status", json=packet(2, ts=1000, name="Scout"))
        self.assertTrue(r.json()["stale"])
        self.assertEqual(r.json()["reason"], "stale_timestamp")

    def test_a_rejected_packet_still_refreshes_only_receive_freshness(self):
        """Unchanged replay-protection semantics: arrival proves the link is alive for THAT
        vehicle; it never accepts the stale content, and it never touches another vehicle."""
        self.post_capture(packet(2, ts=2000, name="Scout", battery=79),
                          packet(3, ts=2000, name="SAR-001", battery=50, mode="MANUAL"))
        old = datetime.now(timezone.utc) - timedelta(seconds=20)
        main.last_seen_by_id[SAR] = old
        main.current_vehicle_state[SAR]["received_at"] = old

        self.post_capture(packet(3, ts=1000, name="SAR-001", battery=1, mode="HOLD"))
        rows = {v["id"]: v for v in self.client.get("/api/fleet/status").json()}
        self.assertEqual(rows[SAR]["comm_state"], "CONNECTED", "arrival refreshed freshness")
        self.assertEqual(rows[SAR]["battery"], 50, "…but the stale content was not accepted")
        self.assertEqual(rows[SAR]["telemetry"]["mode"], "MANUAL")
        self.assertEqual(rows[SCOUT]["battery"], 79, "and Scout is untouched")


if __name__ == "__main__":
    unittest.main()
