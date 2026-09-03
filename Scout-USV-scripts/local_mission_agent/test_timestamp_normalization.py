"""
Regression coverage for the real live SET_HOME failure: the Operator
Backend sends expires_at (and issued_at/created_at/claimed_at) as
ISO-8601 strings, e.g. "2026-07-17T09:52:58.498992+00:00", not the
time.time()-style floats every prior test/mock in this repo used.
command_handler._expired() used to compare that raw string directly
against time.time(), raising "'>' not supported between instances of
'float' and 'str'" -- which escaped process_command() entirely (nothing
inside it catches a bug in validation, only in the final Flask call) and
was only ever caught by local_agent.py's outer "unexpected local agent
error" handler. Because that outer handler never marked the command_id
processed, a redelivery of the same command_id crashed identically
forever -- unbounded retries, one new "failed" history record per poll.

Three layers are covered here:
  - TestNormalizeTimestamp: timestamp_utils.py in isolation.
  - TestCommandHandlerTimestampContract: command_handler.process_command
    with every accepted/rejected timestamp shape, proving the fix at the
    exact call site that used to crash.
  - TestProductionSetHomeShapeAndDedup: the literal production command
    (same command_id, same ISO expires_at, same legacy lat/lng params)
    driven all the way through local_agent._poll_and_execute_commands,
    including simulating the observed "same command_id redelivered every
    ~4 seconds" symptom.

Run directly: python3 test_timestamp_normalization.py
"""
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

import config
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")

import api_client
import command_executor
import command_handler
import command_history
import command_results
import local_agent
from timestamp_utils import InvalidTimestamp, normalize_command_timestamps, normalize_timestamp


def _iso(dt: datetime) -> str:
    return dt.isoformat()


class TestNormalizeTimestamp(unittest.TestCase):
    """Unit coverage for timestamp_utils.normalize_timestamp in isolation."""

    def test_int_epoch_seconds(self):
        self.assertEqual(normalize_timestamp(1_700_000_000), 1_700_000_000.0)

    def test_float_epoch_seconds(self):
        self.assertEqual(normalize_timestamp(1_700_000_000.5), 1_700_000_000.5)

    def test_bool_rejected_even_though_int_subclass(self):
        with self.assertRaises(InvalidTimestamp):
            normalize_timestamp(True)

    def test_iso_with_explicit_utc_offset(self):
        result = normalize_timestamp("2026-07-17T09:52:58.498992+00:00")
        expected = datetime(2026, 7, 17, 9, 52, 58, 498992, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(result, expected, places=5)

    def test_iso_with_non_utc_offset(self):
        result = normalize_timestamp("2026-07-17T11:52:58+02:00")
        expected = datetime(2026, 7, 17, 9, 52, 58, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(result, expected, places=5)

    def test_iso_z_suffix(self):
        result = normalize_timestamp("2026-07-17T09:52:58.498992Z")
        expected = datetime(2026, 7, 17, 9, 52, 58, 498992, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(result, expected, places=5)

    def test_iso_naive_string_treated_as_explicit_utc_not_local_time(self):
        """Requirement 4: timezone-naive strings must be *explicitly*
        interpreted as UTC, never implicitly as the Pi's local timezone.
        This asserts the actual numeric result matches a UTC
        interpretation, not merely that it doesn't crash."""
        result = normalize_timestamp("2026-07-17T09:52:58")
        expected_utc = datetime(2026, 7, 17, 9, 52, 58, tzinfo=timezone.utc).timestamp()
        self.assertAlmostEqual(result, expected_utc, places=5)

    def test_malformed_string_raises_invalid_timestamp_not_bare_error(self):
        with self.assertRaises(InvalidTimestamp):
            normalize_timestamp("not-a-timestamp")

    def test_unsupported_type_raises_invalid_timestamp(self):
        for bad in (None, [], {}, object()):
            with self.assertRaises(InvalidTimestamp):
                normalize_timestamp(bad)

    def test_normalize_command_timestamps_only_touches_present_fields(self):
        result = normalize_command_timestamps({"expires_at": 100.0, "command_id": "x"})
        self.assertEqual(result, {"expires_at": 100.0})

    def test_normalize_command_timestamps_covers_all_four_fields(self):
        command = {
            "expires_at": "2026-07-17T09:52:58.498992+00:00",
            "issued_at": "2026-07-17T09:47:58.498992+00:00",
            "created_at": "2026-07-17T09:47:58.498992+00:00",
            "claimed_at": "2026-07-17T09:47:58.616838+00:00",
        }
        result = normalize_command_timestamps(command)
        self.assertEqual(set(result.keys()), {"expires_at", "issued_at", "created_at", "claimed_at"})
        for v in result.values():
            self.assertIsInstance(v, float)

    def test_normalize_command_timestamps_names_the_bad_field(self):
        command = {"expires_at": "garbage", "issued_at": 100.0}
        with self.assertRaises(InvalidTimestamp) as ctx:
            normalize_command_timestamps(command)
        self.assertIn("expires_at", str(ctx.exception))

    def test_raw_command_dict_never_mutated(self):
        """Requirement 5: normalized values are used internally, the raw
        command (needed for diagnostics) is preserved untouched."""
        command = {"expires_at": "2026-07-17T09:52:58.498992+00:00"}
        original = dict(command)
        normalize_command_timestamps(command)
        self.assertEqual(command, original)


class TestCommandHandlerTimestampContract(unittest.TestCase):
    """
    Integration coverage at the exact call site that crashed in
    production: command_handler.process_command()'s expiry check.
    """

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        command_history._history.clear()
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified
        command_executor.home_verified = lambda: True
        self._call_count = 0

        def fake_call(command, timeout=None):
            self._call_count += 1
            return {"accepted": True, "verified": True}

        command_executor.call_local_endpoint = fake_call

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def _cmd(self, command_id, expires_at):
        return {
            "command_id": command_id,
            "command_type": "SET_HOME",
            "params": {"mode": "current_position"},
            "expires_at": expires_at,
        }

    def test_future_iso_expires_at_executes(self):
        future = _iso(datetime.now(timezone.utc) + timedelta(minutes=5))
        payload, _ = command_handler.process_command(self._cmd("ts-1", future))
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_expired_iso_expires_at_is_rejected(self):
        past = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
        payload, _ = command_handler.process_command(self._cmd("ts-2", past))
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("expired", payload["reason"])
        self.assertEqual(self._call_count, 0)

    def test_numeric_epoch_expires_at_still_works(self):
        payload, _ = command_handler.process_command(self._cmd("ts-3", time.time() + 60))
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_z_suffix_expires_at_works(self):
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        payload, _ = command_handler.process_command(self._cmd("ts-4", future))
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_malformed_expires_at_returns_one_terminal_result_not_a_crash(self):
        """The exact reported bug: this used to raise
        "'>' not supported between instances of 'float' and 'str'" and
        escape process_command() entirely. Now it must return cleanly."""
        payload, event = command_handler.process_command(self._cmd("ts-5", "not-a-real-timestamp"))
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("invalid timestamp", payload["reason"])
        self.assertEqual(self._call_count, 0)
        self.assertEqual(len(command_history.get_recent()), 1, "exactly one terminal history record, no crash")

    def test_naive_expires_at_is_explicitly_utc_not_rejected(self):
        """This suite's documented choice (see timestamp_utils.py): a
        naive ISO string is explicitly treated as UTC rather than
        rejected. A naive timestamp 5 minutes in the future by UTC clock
        must execute."""
        future_naive = (datetime.now(timezone.utc) + timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        payload, _ = command_handler.process_command(self._cmd("ts-6", future_naive))
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_naive_expires_at_in_the_past_by_utc_is_rejected(self):
        """The other half of the explicit-UTC contract: a naive timestamp
        that is in the past *by UTC* must be rejected as expired -- if
        this process ever implicitly used local time instead, a Pi in a
        timezone ahead of UTC could accept an already-expired command."""
        past_naive = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(tzinfo=None).isoformat()
        payload, _ = command_handler.process_command(self._cmd("ts-7", past_naive))
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("expired", payload["reason"])

    def test_none_expires_at_never_expires(self):
        cmd = self._cmd("ts-8", None)
        payload, _ = command_handler.process_command(cmd)
        self.assertEqual(payload["status"], "executed")

    def test_malformed_timestamp_marks_command_processed_bounded_retry(self):
        """Requirement 8: a redelivery of a command that failed timestamp
        validation must not re-litigate the same crash/rejection -- it
        must resend that original stored "invalid timestamp" rejection
        exactly (see command_results.py), and must never grow history by
        more than the one record per delivery attempt."""
        cmd = self._cmd("ts-9", "still not a real timestamp")
        first, _ = command_handler.process_command(cmd)
        self.assertEqual(first["status"], "rejected")
        self.assertIn("invalid timestamp", first["reason"])

        second, _ = command_handler.process_command(dict(cmd))
        self.assertEqual(second, first)
        self.assertEqual(self._call_count, 0)


class TestProductionSetHomeShapeAndDedup(unittest.TestCase):
    """
    Drives the *exact* production command shape (real command_id, real
    ISO expires_at, real legacy lat/lng params) through
    local_agent._poll_and_execute_commands end to end, and reproduces the
    observed "same command_id redelivered every ~4 seconds" symptom to
    prove it no longer causes unbounded retries/crashes.
    """

    PRODUCTION_COMMAND_ID = "8f74bd62-c542-4e1f-8047-e3aada544a0c"

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        command_history._history.clear()
        self._orig_get_pending = api_client.get_pending_commands
        self._orig_send = api_client.send_to_operator
        self._orig_request = command_executor.requests.request
        self._orig_home_verified = command_executor.home_verified
        command_executor.home_verified = lambda: True
        self.posted = []

        def fake_send(endpoint, message):
            self.posted.append((endpoint, message))
            return {"ok": True, "operator": "http://fake", "response": {"ok": True}}

        api_client.send_to_operator = fake_send
        local_agent.send_to_operator = fake_send

    def tearDown(self):
        api_client.get_pending_commands = self._orig_get_pending
        api_client.send_to_operator = self._orig_send
        local_agent.get_pending_commands = self._orig_get_pending
        local_agent.send_to_operator = self._orig_send
        command_executor.requests.request = self._orig_request
        command_executor.home_verified = self._orig_home_verified

    def _production_command(self, expires_at=None):
        """Same shape as the real Operator Backend command reported in
        the incident, translated to the field names the Local Agent's
        command_handler.py has always consumed (command_id/command_type)
        -- api_client.get_pending_commands() returns whatever GET
        /agent/commands hands back, which is this shape."""
        return {
            "command_id": self.PRODUCTION_COMMAND_ID,
            "usv_id": 2,
            "command_type": "SET_HOME",
            "params": {"lat": 56.6634934, "lng": 12.8814627},  # legacy/browser-supplied
            "created_at": "2026-07-17T09:47:58.498992+00:00",
            "claimed_at": "2026-07-17T09:47:58.616838+00:00",
            "expires_at": expires_at or "2099-01-01T00:00:00+00:00",
        }

    def _fake_flask_response(self, body):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured.update(kwargs)

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return body

            return _Resp()

        command_executor.requests.request = fake_request
        return captured

    def _serve_once(self, commands):
        def fake_get_pending(usv_id):
            due, commands[:] = list(commands), []
            return due
        api_client.get_pending_commands = fake_get_pending
        local_agent.get_pending_commands = fake_get_pending

    def test_exact_production_set_home_reaches_flask_with_same_uuid_and_mode(self):
        captured = self._fake_flask_response({
            "accepted": True, "verified": True, "command_id": self.PRODUCTION_COMMAND_ID,
            "requested_position": {"latitude": 56.6634934, "longitude": 12.8814627},
            "home_position": {"latitude": 56.6634934, "longitude": 12.8814627, "altitude": 0.7},
            "verification_distance_m": 0.0, "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
        })
        self._serve_once([self._production_command()])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(captured["json"], {"command_id": self.PRODUCTION_COMMAND_ID, "mode": "current_position"})
        self.assertEqual(self.posted[0][1]["payload"]["status"], "executed")

    def test_legacy_lat_lng_ignored_and_never_forwarded(self):
        captured = self._fake_flask_response({"accepted": True, "verified": True})
        self._serve_once([self._production_command()])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertNotIn("lat", captured["json"])
        self.assertNotIn("lng", captured["json"])

    def test_production_expired_iso_expires_at_rejected_not_crashed(self):
        """The literal incident payload's expires_at
        ("2026-07-17T09:52:58.498992+00:00") already elapsed by the time
        this test runs -- must be a clean terminal rejection, never the
        '>' not supported between instances of 'float' and 'str' crash."""
        self._serve_once([self._production_command(expires_at="2026-07-17T09:52:58.498992+00:00")])

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        payload = self.posted[0][1]["payload"]
        self.assertIn(payload["status"], ("rejected", "failed"))
        self.assertNotIn("not supported between instances", payload["reason"])

    def test_same_command_id_redelivered_every_poll_is_not_reexecuted_or_rerecorded(self):
        """Reproduces the observed symptom: the same command_id offered
        again on (simulated) ~4-second polling intervals. Must settle
        into a single execution plus bounded "duplicate" rejections, not
        unbounded identical failure records."""
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"accepted": True, "verified": True}

            return _Resp()

        command_executor.requests.request = fake_request

        for _ in range(5):
            self._serve_once([self._production_command()])
            local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(call_count["n"], 1, "Flask must be called exactly once across 5 redeliveries")
        self.assertEqual(len(self.posted), 5, "every redelivery still gets a command_result posted back")
        statuses = [p[1]["payload"]["status"] for p in self.posted]
        self.assertEqual(statuses[0], "executed")
        self.assertTrue(all(s == "rejected" for s in statuses[1:]))
        self.assertTrue(all("duplicate" in p[1]["payload"]["reason"] for p in self.posted[1:]))

    def test_result_retry_does_not_reexecute_flask(self):
        """Requirement 9: once a terminal result exists for a command_id,
        a later redelivery of that same command_id (a result-delivery
        retry from the operator side) must resolve without ever calling
        the vehicle Flask service again."""
        call_count = {"n": 0}

        def fake_request(method, url, **kwargs):
            call_count["n"] += 1

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"accepted": True, "verified": True}

            return _Resp()

        command_executor.requests.request = fake_request

        self._serve_once([self._production_command()])
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])
        self.assertEqual(self.posted[-1][1]["payload"]["status"], "executed")

        self._serve_once([self._production_command()])
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(call_count["n"], 1)
        self.assertEqual(self.posted[-1][1]["payload"]["status"], "rejected")

    def test_malformed_timestamp_via_outer_loop_does_not_retry_forever(self):
        """Full end-to-end version of the fix: even simulating several
        redeliveries of a command with an unparseable expires_at (the
        production bug's shape before this fix), the Local Agent must
        settle after the first attempt -- one rejection, then bounded
        duplicates -- never repeating an "unexpected local agent error"
        crash."""
        cmd = self._production_command(expires_at="definitely-not-iso-8601")

        for _ in range(4):
            self._serve_once([dict(cmd)])
            local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        statuses = [p[1]["payload"]["status"] for p in self.posted]
        reasons = [p[1]["payload"]["reason"] for p in self.posted]
        self.assertTrue(all(s == "rejected" for s in statuses))
        self.assertNotIn("unexpected local agent error", reasons[0])
        self.assertTrue(all("duplicate" in r for r in reasons[1:]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
