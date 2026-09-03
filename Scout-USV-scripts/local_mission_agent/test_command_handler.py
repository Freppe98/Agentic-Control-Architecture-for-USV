"""
Standalone tests for the operator command validation/execution path
(command_handler.process_command). No pytest dependency -- run directly:

    python3 test_command_handler.py

Uses a temp COMMAND_LOG_FILE/COMMAND_RESULTS_FILE per run so this never
touches the real command_log.jsonl/command_results.json, and monkeypatches
command_executor.call_local_endpoint so no real Flask/mavlink2rest/Pixhawk
is required.
"""
import os
import tempfile
import time
import unittest
import uuid

import config
# Point the persisted command-dedup/result state at throwaway temp files
# BEFORE importing anything that touches them. command_log.py /
# command_results.py read config.<NAME> dynamically (never a value snapshot at
# import), so this override is authoritative no matter the import order and
# nothing here ever writes the real command_log.jsonl/command_results.json in
# the source tree -- the bleed that previously committed test command_ids.
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")

import command_executor
import command_handler
import command_history
import command_results


def tearDownModule():
    """Remove the throwaway temp files this module pointed config at, so a
    full-suite run leaves nothing behind in /tmp."""
    for path in (config.COMMAND_LOG_FILE, config.COMMAND_RESULTS_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _make_command(command_type="SET_MODE_HOLD", expires_in=60, command_id=None, params=None):
    return {
        "command_id": command_id or str(uuid.uuid4()),
        "usv_id": "usv-2",
        "command_type": command_type,
        "issued_at": time.time(),
        "expires_at": time.time() + expires_in if expires_in is not None else None,
        "params": params if params is not None else {},
        "requested_by": "test",
    }


def _verified_result_for(command, timeout=None):
    """A vehicle-Flask response that normalizes to a verified, executed
    outcome for whichever command_type is given -- mode commands report the
    exact custom_mode command_normalization expects them to reach, ARM/DISARM
    the matching final armed state, everything else (SET_HOME) a bare success
    the handler passes through unchanged. Used as the default fake execution
    so a test that doesn't care about the vehicle's response still models a
    *real* success (a 2xx alone is no longer treated as executed), and never
    makes a real HTTP call to the live vehicle Flask service."""
    ct = command["command_type"]
    expected = command_executor.MODE_COMMAND_EXPECTED.get(ct)
    if expected:
        return {"accepted": True, "verified": True, "observed_mode": expected[1], "reason": None}
    if ct == "ARM":
        return {"accepted": True, "verified": True, "armed": True, "expected_armed": True,
                "ack_result": "MAV_RESULT_ACCEPTED", "error": None}
    if ct == "DISARM":
        return {"accepted": True, "verified": True, "armed": False, "expected_armed": False,
                "ack_result": "MAV_RESULT_ACCEPTED", "error": None}
    return {"status": "ok"}


class TestCommandHandler(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified
        # Default execution mocks the local Flask call so no test here ever
        # reaches the real vehicle Flask service / Pixhawk (a live one on
        # 127.0.0.1:8080 would otherwise actually change the vehicle's mode).
        # Returns a verified response for the command_type under test;
        # individual tests override this with their own fake as needed.
        command_executor.call_local_endpoint = _verified_result_for
        # Default True: most tests here use RTL/AUTO purely as a stand-in
        # "some command that calls the local endpoint", not to exercise the
        # Home-verification gate itself -- see TestHomeVerificationGate for
        # that. Individual tests override this back to False as needed.
        command_executor.home_verified = lambda: True

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def test_malformed_missing_command_id(self):
        payload, event = command_handler.process_command({"command_type": "SET_MODE_HOLD"})
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("malformed", payload["reason"])

    def test_expired_command_rejected(self):
        cmd = _make_command(expires_in=-10)
        payload, event = command_handler.process_command(cmd)
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("expired", payload["reason"])

    def test_unsupported_command_type_rejected(self):
        cmd = _make_command(command_type="CLEAR_MISSION")
        payload, event = command_handler.process_command(cmd)
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("unsupported", payload["reason"])

    def test_guided_unsupported_no_endpoint_yet(self):
        cmd = _make_command(command_type="SET_MODE_GUIDED")
        payload, event = command_handler.process_command(cmd)
        self.assertEqual(payload["status"], "rejected")
        self.assertIn("unsupported", payload["reason"])

    def test_duplicate_command_id_resends_original_executed_result(self):
        """The first terminal (executed) result must win over any later
        redelivery -- a duplicate poll resends that exact original result,
        not a fresh 'rejected: duplicate' verdict (see command_results.py)."""
        call_count = {"n": 0}

        def fake_call(command, timeout=None):
            call_count["n"] += 1
            return _verified_result_for(command)

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_MODE_HOLD", command_id="dup-1")
        first, _ = command_handler.process_command(cmd)
        self.assertEqual(first["status"], "executed")

        for _ in range(8):
            redelivered, _ = command_handler.process_command(cmd)
            self.assertEqual(redelivered, first)

        self.assertEqual(call_count["n"], 1, "a redelivered command_id must never re-reach the Flask endpoint")

    def test_supported_command_executes_successfully(self):
        seen = {}

        def fake_call(command, timeout=None):
            seen["command_type"] = command["command_type"]
            # A real verified HOLD result, plus an extra field to prove the
            # raw response's own fields survive normalization.
            return {"accepted": True, "verified": True, "observed_mode": 4,
                    "reason": None, "message": "HOLD mode"}

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_MODE_HOLD")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(seen["command_type"], "SET_MODE_HOLD")
        # Raw fields preserved through normalization ...
        self.assertEqual(payload["result"]["message"], "HOLD mode")
        # ... alongside the normalized contract.
        self.assertTrue(payload["result"]["verified"])
        self.assertEqual(payload["result"]["expected_state"], "HOLD")
        self.assertEqual(payload["result"]["observed_state"], "HOLD")
        self.assertEqual(event["type"], "command_executed")

    def test_local_flask_failure_reported_as_failed_not_crash(self):
        def fake_call(command, timeout=None):
            raise RuntimeError("connection refused")

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="RTL")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "failed")
        self.assertIn("connection refused", payload["reason"])

    def test_all_in_slice_command_types_map_to_endpoints(self):
        for command_type in (
            "SET_MODE_AUTO", "SET_MODE_MANUAL", "SET_MODE_HOLD", "LOITER", "SET_MODE_LOITER",
            "RTL", "RETURN_HOME", "MISSION_PAUSE", "MISSION_RESUME",
            "ARM", "DISARM", "SET_HOME",
        ):
            self.assertTrue(
                command_executor.is_supported(command_type),
                f"{command_type} should be supported",
            )

    def test_loiter_supported_and_maps_to_loiter_endpoint(self):
        self.assertTrue(command_executor.is_supported("LOITER"))
        spec = command_executor.ALLOWED_COMMANDS["LOITER"]
        self.assertEqual((spec.method, spec.path), ("POST", "/nav/loiter"))
        self.assertIsNone(spec.build_body, "LOITER must be a plain bodyless call")

    def test_set_mode_loiter_supported_and_maps_to_same_loiter_endpoint(self):
        """Production operator traffic sends SET_MODE_LOITER (matching the
        SET_MODE_AUTO/SET_MODE_MANUAL/SET_MODE_HOLD naming convention), which
        was previously entirely absent from the registry and rejected as
        unsupported. It must reach the exact same /nav/loiter endpoint as
        LOITER -- not a second, parallel command path."""
        self.assertTrue(command_executor.is_supported("SET_MODE_LOITER"))
        spec = command_executor.ALLOWED_COMMANDS["SET_MODE_LOITER"]
        self.assertEqual((spec.method, spec.path), ("POST", "/nav/loiter"))
        self.assertIsNone(spec.build_body, "SET_MODE_LOITER must be a plain bodyless call")
        self.assertIs(
            command_executor.ALLOWED_COMMANDS["SET_MODE_LOITER"],
            command_executor.ALLOWED_COMMANDS["LOITER"],
            "LOITER and SET_MODE_LOITER must be the exact same CommandSpec object, "
            "not two independently maintained registry entries",
        )

    def test_set_mode_loiter_not_in_home_verification_required(self):
        """LOITER must remain available as a safety command regardless of
        Home verification -- SET_MODE_LOITER, its alias, must never be added
        to the gate either."""
        self.assertNotIn("SET_MODE_LOITER", command_executor.HOME_VERIFICATION_REQUIRED)

    def test_set_mode_loiter_accepted_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        cmd = _make_command(command_type="SET_MODE_LOITER")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")

    def test_set_mode_loiter_executes_successfully_and_preserves_result(self):
        seen = {}

        def fake_call(command, timeout=None):
            seen["command_type"] = command["command_type"]
            return {"accepted": True, "verified": True, "observed_mode": 5, "reason": None}

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_MODE_LOITER")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(seen["command_type"], "SET_MODE_LOITER")
        self.assertTrue(payload["result"]["verified"])
        self.assertEqual(event["type"], "command_executed")

    def test_set_mode_loiter_flask_rejection_becomes_terminal_failure(self):
        def fake_call(command, timeout=None):
            raise RuntimeError("connection refused")

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_MODE_LOITER")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "failed")
        self.assertIn("connection refused", payload["reason"])
        self.assertEqual(event["type"], "command_failed")

    def test_still_out_of_scope_commands_not_allowed(self):
        for command_type in ("CLEAR_MISSION", "JUMP_TO_WAYPOINT", "SET_MODE_GUIDED"):
            self.assertFalse(
                command_executor.is_supported(command_type),
                f"{command_type} must stay out of scope for now",
            )

    def test_arm_supported_and_maps_to_arm_on_endpoint(self):
        self.assertTrue(command_executor.is_supported("ARM"))
        spec = command_executor.ALLOWED_COMMANDS["ARM"]
        self.assertEqual((spec.method, spec.path), ("POST", "/nav/ArmOn"))

    def test_disarm_supported_and_maps_to_disarm_endpoint(self):
        self.assertTrue(command_executor.is_supported("DISARM"))
        spec = command_executor.ALLOWED_COMMANDS["DISARM"]
        self.assertEqual((spec.method, spec.path), ("POST", "/nav/Disarm"))

    def test_arm_executes_successfully(self):
        seen = {}

        def fake_call(command, timeout=None):
            seen["command_type"] = command["command_type"]
            return _verified_result_for(command)

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="ARM")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(seen["command_type"], "ARM")
        self.assertEqual(event["type"], "command_executed")

    def test_disarm_executes_successfully(self):
        seen = {}

        def fake_call(command, timeout=None):
            seen["command_type"] = command["command_type"]
            return _verified_result_for(command)

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="DISARM")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(seen["command_type"], "DISARM")
        self.assertEqual(event["type"], "command_executed")

    def test_duplicate_arm_not_executed_twice(self):
        call_count = {"n": 0}

        def fake_call(command, timeout=None):
            call_count["n"] += 1
            return _verified_result_for(command)

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="ARM", command_id="arm-dup-1")

        first, _ = command_handler.process_command(cmd)
        self.assertEqual(first["status"], "executed")

        second, _ = command_handler.process_command(cmd)
        self.assertEqual(second, first, "a redelivered ARM must resend the original executed result, not a fresh rejection")

        self.assertEqual(call_count["n"], 1, "ARM must only reach the Flask endpoint once")

    def test_expired_arm_rejected_without_executing(self):
        called = {"n": 0}

        def fake_call(command, timeout=None):
            called["n"] += 1
            return {"status": "Arm mode in on"}

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="ARM", expires_in=-5)
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "rejected")
        self.assertIn("expired", payload["reason"])
        self.assertEqual(called["n"], 0, "an expired ARM must never reach the Flask endpoint")

    def test_arm_local_flask_failure_reported_as_failed(self):
        def fake_call(command, timeout=None):
            raise RuntimeError("connection refused")

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="ARM")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "failed")
        self.assertIn("connection refused", payload["reason"])
        self.assertEqual(event["type"], "command_failed")

    def test_disarm_local_flask_failure_reported_as_failed(self):
        def fake_call(command, timeout=None):
            raise RuntimeError("timeout")

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="DISARM")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "failed")
        self.assertIn("timeout", payload["reason"])
        self.assertEqual(event["type"], "command_failed")


class TestCommandLifecycle(unittest.TestCase):
    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified
        command_executor.home_verified = lambda: True
        command_history._history.clear()

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def test_executed_command_lifecycle_progresses_through_all_stages(self):
        command_executor.call_local_endpoint = _verified_result_for
        cmd = _make_command(command_type="SET_MODE_HOLD")
        payload, _ = command_handler.process_command(cmd)

        stages = [s["status"] for s in payload["lifecycle"]]
        self.assertEqual(stages, ["requested", "accepted", "executing", "executed"])
        self.assertEqual(payload["status"], "executed")

    def test_failed_command_lifecycle_stops_at_failed(self):
        def fake_call(command, timeout=None):
            raise RuntimeError("connection refused")
        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="RTL")
        payload, _ = command_handler.process_command(cmd)

        stages = [s["status"] for s in payload["lifecycle"]]
        self.assertEqual(stages, ["requested", "accepted", "executing", "failed"])

    def test_rejected_command_lifecycle_skips_accepted_and_executing(self):
        cmd = _make_command(command_type="CLEAR_MISSION")
        payload, _ = command_handler.process_command(cmd)

        stages = [s["status"] for s in payload["lifecycle"]]
        self.assertEqual(stages, ["requested", "rejected"])

    def test_lifecycle_timestamps_are_monotonic(self):
        command_executor.call_local_endpoint = _verified_result_for
        cmd = _make_command(command_type="SET_MODE_HOLD")
        payload, _ = command_handler.process_command(cmd)

        timestamps = [s["timestamp"] for s in payload["lifecycle"]]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_source_defaults_to_operator_when_not_supplied(self):
        command_executor.call_local_endpoint = _verified_result_for
        cmd = _make_command(command_type="SET_MODE_HOLD")
        del cmd["requested_by"]
        payload, _ = command_handler.process_command(cmd)
        self.assertEqual(payload["source"], "operator")

    def test_source_passed_through_when_supplied(self):
        command_executor.call_local_endpoint = _verified_result_for
        cmd = _make_command(command_type="SET_MODE_HOLD")
        cmd["source"] = "operator-ui:jane"
        payload, _ = command_handler.process_command(cmd)
        self.assertEqual(payload["source"], "operator-ui:jane")

    def test_command_recorded_in_history(self):
        command_executor.call_local_endpoint = _verified_result_for
        cmd = _make_command(command_type="SET_MODE_HOLD", command_id="hist-1")
        command_handler.process_command(cmd)

        recent = command_history.get_recent()
        self.assertEqual(recent[-1]["command_id"], "hist-1")
        self.assertEqual(recent[-1]["status"], "executed")

    def test_command_history_records_start_and_end_of_execution(self):
        """Requirement: a command must be visible in command_history both
        when execution begins (status "executing") and when it ends
        (status "executed"/"failed") -- not only once at the terminal
        state -- so a command still in flight (or one whose process died
        mid-call) leaves a trace instead of the history staying empty."""
        command_executor.call_local_endpoint = _verified_result_for
        cmd = _make_command(command_type="SET_MODE_HOLD", command_id="hist-start-end")
        command_handler.process_command(cmd)

        recent = command_history.get_recent()
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["command_id"], "hist-start-end")
        self.assertEqual(recent[0]["status"], "executing")
        self.assertEqual(recent[1]["command_id"], "hist-start-end")
        self.assertEqual(recent[1]["status"], "executed")

    def test_command_history_records_start_and_failed_end(self):
        def fake_call(command, timeout=None):
            raise RuntimeError("connection refused")

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="RTL", command_id="hist-fail-1")
        command_handler.process_command(cmd)

        recent = command_history.get_recent()
        self.assertEqual(len(recent), 2)
        self.assertEqual(recent[0]["status"], "executing")
        self.assertEqual(recent[1]["status"], "failed")

    def test_rejected_command_recorded_once_no_executing_stage(self):
        """A rejected command (malformed/expired/duplicate/unsupported/
        home-unverified) never reaches execution, so it must only ever
        produce the single terminal history record it always has -- no
        "executing" record, since execution never began."""
        cmd = _make_command(command_type="CLEAR_MISSION", command_id="hist-rejected-1")
        command_handler.process_command(cmd)

        recent = command_history.get_recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["status"], "rejected")

    def test_history_bounded_to_max(self):
        command_executor.call_local_endpoint = _verified_result_for
        for i in range(command_history.MAX_HISTORY + 10):
            command_handler.process_command(_make_command(command_type="SET_MODE_HOLD", command_id=f"h-{i}"))
        self.assertEqual(len(command_history.get_recent()), command_history.MAX_HISTORY)


class TestHomeVerificationGate(unittest.TestCase):
    """
    AUTO/RTL/RESUME must be rejected while Home is unverified; LOITER/
    MANUAL (and every other command type never in
    command_executor.HOME_VERIFICATION_REQUIRED) must remain available
    regardless -- see command_handler.py's validation order and
    command_executor.HOME_VERIFICATION_REQUIRED.
    """

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified
        self._call_count = 0

        def fake_call(command, timeout=None):
            self._call_count += 1
            return _verified_result_for(command)

        command_executor.call_local_endpoint = fake_call

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def test_auto_rejected_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        cmd = _make_command(command_type="SET_MODE_AUTO")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "rejected")
        self.assertIn("home unverified", payload["reason"])
        self.assertEqual(self._call_count, 0, "AUTO must never reach the local endpoint while Home is unverified")

    def test_rtl_rejected_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        for command_type in ("RTL", "RETURN_HOME"):
            payload, _ = command_handler.process_command(_make_command(command_type=command_type))
            self.assertEqual(payload["status"], "rejected", command_type)
            self.assertIn("home unverified", payload["reason"], command_type)
        self.assertEqual(self._call_count, 0, "RTL/RETURN_HOME must never reach the local endpoint while Home is unverified")

    def test_resume_rejected_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        cmd = _make_command(command_type="MISSION_RESUME")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "rejected")
        self.assertIn("home unverified", payload["reason"])
        self.assertEqual(self._call_count, 0, "MISSION_RESUME must never reach the local endpoint while Home is unverified")

    def test_loiter_accepted_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        cmd = _make_command(command_type="LOITER")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_manual_accepted_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        cmd = _make_command(command_type="SET_MODE_MANUAL")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_hold_accepted_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        cmd = _make_command(command_type="SET_MODE_HOLD")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_arm_disarm_accepted_when_home_unverified(self):
        command_executor.home_verified = lambda: False
        for command_type in ("ARM", "DISARM"):
            payload, _ = command_handler.process_command(_make_command(command_type=command_type))
            self.assertEqual(payload["status"], "executed", command_type)

    def test_auto_accepted_once_home_verified(self):
        command_executor.home_verified = lambda: True
        cmd = _make_command(command_type="SET_MODE_AUTO")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(self._call_count, 1)

    def test_loiter_not_in_home_verification_required(self):
        """LOITER must never be added to HOME_VERIFICATION_REQUIRED -- this
        is the literal safety invariant the task requires, asserted
        directly against the set rather than only indirectly via behavior."""
        self.assertNotIn("LOITER", command_executor.HOME_VERIFICATION_REQUIRED)
        self.assertNotIn("SET_MODE_MANUAL", command_executor.HOME_VERIFICATION_REQUIRED)


class TestControlAuthorityGate(unittest.TestCase):
    """
    control_authority is a blanket gate on the whole operator command
    queue, with no per-command_type exemption: the queue is explicit
    operator intent, so every supported command_type -- SET_HOME and
    LOITER included, no exceptions -- executes while authority is OPERATOR
    and is rejected while it's LOCAL_AGENT (the Local Agent's own
    autonomous writes, gated separately by autonomy_gate.py, own the
    vehicle then). See local_agent.py's _poll_and_execute_commands, which
    always polls and passes control_authority straight through to
    process_command().
    """

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified
        self._call_count = 0
        command_executor.home_verified = lambda: True

        def fake_call(command, timeout=None):
            self._call_count += 1
            return _verified_result_for(command)

        command_executor.call_local_endpoint = fake_call

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def test_supported_commands_execute_under_operator_authority(self):
        for command_type in ("SET_HOME", "LOITER", "SET_MODE_LOITER", "SET_MODE_AUTO", "ARM", "DISARM", "RTL", "SET_MODE_HOLD"):
            self._call_count = 0
            cmd = _make_command(command_type=command_type, command_id=f"op-auth-{command_type}")
            payload, _ = command_handler.process_command(cmd, control_authority="OPERATOR")

            self.assertEqual(payload["status"], "executed", command_type)
            self.assertEqual(self._call_count, 1, f"{command_type} must reach the local endpoint while authority is OPERATOR")

    def test_supported_commands_rejected_under_local_agent_authority(self):
        for command_type in ("SET_HOME", "LOITER", "SET_MODE_LOITER", "SET_MODE_AUTO", "ARM", "DISARM", "RTL", "SET_MODE_HOLD"):
            self._call_count = 0
            cmd = _make_command(command_type=command_type, command_id=f"la-auth-{command_type}")
            payload, _ = command_handler.process_command(cmd, control_authority="LOCAL_AGENT")

            self.assertEqual(payload["status"], "rejected", command_type)
            self.assertIn("OPERATOR control authority", payload["reason"], command_type)
            self.assertEqual(self._call_count, 0, f"{command_type} must never reach the local endpoint while authority is LOCAL_AGENT")

    def test_rejection_reason_matches_documented_format(self):
        cmd = _make_command(command_type="SET_MODE_AUTO", command_id="reason-format-1")
        payload, _ = command_handler.process_command(cmd, control_authority="LOCAL_AGENT")

        self.assertEqual(payload["reason"], "blocked: SET_MODE_AUTO requires OPERATOR control authority (currently LOCAL_AGENT)")

    def test_authority_rejection_marks_command_processed(self):
        """A command rejected for lacking authority must still be marked
        processed -- a redelivery of the same command_id resends that
        original "blocked" result exactly (see command_results.py), even
        once authority has since changed to OPERATOR, rather than
        re-litigating the authority gate every time, same convention as
        expired/unsupported."""
        cmd = _make_command(command_type="SET_MODE_HOLD", command_id="auth-dup-1")
        first, _ = command_handler.process_command(cmd, control_authority="LOCAL_AGENT")
        self.assertEqual(first["status"], "rejected")

        second, _ = command_handler.process_command(cmd, control_authority="OPERATOR")
        self.assertEqual(second, first)

    def test_authority_exempt_commands_no_longer_exists(self):
        """The strict model has no per-command_type exemption from the
        authority gate -- this attribute must not exist at all."""
        self.assertFalse(hasattr(command_executor, "AUTHORITY_EXEMPT_COMMANDS"))


class TestSetHomeCommand(unittest.TestCase):
    """
    SET_HOME reaches the vehicle Flask service exactly like every other
    operator command -- queued by the operator backend, polled via
    GET /agent/commands, validated (expiry/dedup/support) and executed by
    command_handler.process_command()/command_executor.py, result pushed
    back via POST /agent/command_result. The Local Agent has no inbound
    HTTP surface of its own for this (see agent_server.py); the operator
    UI only ever talks to the Operator Backend, which queues SET_HOME the
    same way it queues ARM/RTL/etc.

    There is exactly one execution function, command_executor.
    call_local_endpoint(command) -- no separate SET_HOME executor and no
    SET_HOME-specific branch in command_handler.py. These tests monkeypatch
    that single function, same as every other command_type's tests above.
    """

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call

    def test_set_home_supported_and_maps_to_agent_set_home_endpoint(self):
        self.assertTrue(command_executor.is_supported("SET_HOME"))
        spec = command_executor.ALLOWED_COMMANDS["SET_HOME"]
        self.assertEqual((spec.method, spec.path), ("POST", "/agent/set_home"))

    def test_set_home_declares_a_body_builder(self):
        """The declarative property that makes SET_HOME different from
        every other command_type -- a build_body callable on its own
        CommandSpec, not a separate registry or a separate function."""
        spec = command_executor.ALLOWED_COMMANDS["SET_HOME"]
        self.assertIsNotNone(spec.build_body)
        self.assertTrue(callable(spec.build_body))

    def test_set_home_never_requires_prior_home_verification(self):
        """Would be circular -- SET_HOME is how Home gets verified in the
        first place, so it must never be gated on already being verified."""
        self.assertNotIn("SET_HOME", command_executor.HOME_VERIFICATION_REQUIRED)

    def test_set_home_executes_through_process_command_via_call_local_endpoint(self):
        """Proves SET_HOME runs through the same process_command() entry
        point and the same call_local_endpoint(command) function every
        other command_type uses -- not a separate handler module."""
        seen = {}

        def fake_call(command, timeout=None):
            seen["command"] = command
            return {
                "accepted": True, "verified": True, "command_id": command["command_id"],
                "requested_position": {"latitude": 56.6505, "longitude": 12.8707},
                "home_position": {"latitude": 56.6505, "longitude": 12.8707, "altitude": 1.2},
                "verification_distance_m": 1.4, "ack_result": "MAV_RESULT_ACCEPTED", "error": None,
            }

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_HOME", params={"mode": "current_position"})
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertEqual(seen["command"]["command_id"], cmd["command_id"])
        self.assertEqual(seen["command"]["command_type"], "SET_HOME")
        self.assertTrue(payload["result"]["verified"])
        self.assertEqual(event["type"], "command_executed")

    def test_set_home_failed_verification_reported_as_executed_with_failure_body(self):
        """Mirrors every other command_type's convention: any non-exception
        HTTP response from the vehicle Flask service is "executed" at the
        command-protocol level -- the operator backend inspects
        result.accepted/result.verified/result.error itself, same as it
        would for any other command's result body."""
        command_executor.call_local_endpoint = lambda command, timeout=None: {
            "accepted": False, "verified": False, "command_id": command["command_id"],
            "requested_position": None, "home_position": None,
            "verification_distance_m": None, "ack_result": None,
            "error": {"code": "POSITION_STALE", "message": "stale"},
        }
        cmd = _make_command(command_type="SET_HOME")
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "executed")
        self.assertFalse(payload["result"]["verified"])
        self.assertEqual(payload["result"]["error"]["code"], "POSITION_STALE")

    def test_set_home_flask_unreachable_follows_same_failed_lifecycle_as_other_commands(self):
        """Same lifecycle (requested -> accepted -> executing -> failed)
        and same "failed" status/event vocabulary as e.g. ARM/RTL's own
        local-flask-failure tests above -- no SET_HOME-specific failure
        handling exists."""
        def fake_call(command, timeout=None):
            raise RuntimeError("connection refused")

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_HOME")
        payload, event = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "failed")
        self.assertIn("connection refused", payload["reason"])
        self.assertEqual(event["type"], "command_failed")
        stages = [s["status"] for s in payload["lifecycle"]]
        self.assertEqual(stages, ["requested", "accepted", "executing", "failed"])

    def test_set_home_duplicate_command_id_resends_stored_result_by_shared_dedup_path(self):
        """Same shared dedup path as test_duplicate_command_id_resends_
        original_executed_result, exercised with SET_HOME's own nested
        accepted/verified/ack_result/error result shape -- that nested
        shape must survive the resend unchanged, not just the outer
        status/reason."""
        call_count = {"n": 0}

        def fake_call(command, timeout=None):
            call_count["n"] += 1
            return {"accepted": True, "verified": True, "command_id": command["command_id"],
                    "requested_position": None, "home_position": None,
                    "verification_distance_m": None, "ack_result": None, "error": None}

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_HOME", command_id="set-home-dup-1")

        first, _ = command_handler.process_command(cmd)
        self.assertEqual(first["status"], "executed")

        second, _ = command_handler.process_command(cmd)
        self.assertEqual(second, first)
        self.assertEqual(second["result"], first["result"])
        self.assertEqual(call_count["n"], 1, "a redelivered SET_HOME command_id must never re-trigger execution")

    def test_set_home_expired_rejected_by_shared_expiry_path_without_executing(self):
        called = {"n": 0}

        def fake_call(command, timeout=None):
            called["n"] += 1
            return {"accepted": True, "verified": True}

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_HOME", expires_in=-5)
        payload, _ = command_handler.process_command(cmd)

        self.assertEqual(payload["status"], "rejected")
        self.assertIn("expired", payload["reason"])
        self.assertEqual(called["n"], 0, "an expired SET_HOME must never reach the Flask endpoint")

    def test_set_home_params_threaded_through_to_flask_body(self):
        seen = {}

        def fake_call(command, timeout=None):
            seen["params"] = command.get("params")
            return {"accepted": True, "verified": True}

        command_executor.call_local_endpoint = fake_call
        cmd = _make_command(command_type="SET_HOME", params={
            "mode": "current_position", "tolerance_m": 2.5, "freshness_s": 1.5,
        })
        command_handler.process_command(cmd)

        self.assertEqual(seen["params"], {"mode": "current_position", "tolerance_m": 2.5, "freshness_s": 1.5})

    def test_no_set_home_inbound_endpoint_on_local_agent_http_surface(self):
        """agent_server.py (the Local Agent's only inbound HTTP listener)
        must never reintroduce a direct Set Home write path -- the
        Operator Backend is the only thing the frontend talks to, and
        SET_HOME must only ever arrive via the queued-command path. Checks
        for the actual route-matching idiom every real route here uses
        (self.path.startswith("...")) rather than a bare substring search --
        the module's docstring legitimately mentions "/agent/set_home" in
        prose explaining this absence, so text presence alone isn't the
        signal."""
        import inspect
        import agent_server
        source = inspect.getsource(agent_server)
        self.assertNotIn('startswith("/agent/set_home")', source)
        self.assertNotIn('startswith("/agent/commands/set_home")', source)
        self.assertFalse(hasattr(agent_server, "set_home_handler"))

        # Behavioral confirmation, not just a source-text check: neither
        # verb reaches a Set Home route on a live Handler instance.
        handler = agent_server.Handler.__new__(agent_server.Handler)
        for path in ("/agent/set_home", "/agent/commands/set_home"):
            handler.path = path
            sent = {}
            handler._send_json = lambda obj, code=200: sent.update(obj=obj, code=code)
            handler.do_GET()
            self.assertEqual(sent["code"], 404, f"GET {path} must not be routed")
            handler.do_POST()
            self.assertEqual(sent["code"], 404, f"POST {path} must not be routed")


class TestCallLocalEndpointBodyBuilding(unittest.TestCase):
    """
    Unit coverage for command_executor.call_local_endpoint's body-building
    branch (SET_HOME today) *and* its bodyless branch (every other
    command_type), independent of command_handler.py's dispatch -- mocks
    requests.request directly. One function, one branch point
    (CommandSpec.build_body present or not); these tests exercise both
    sides of that same branch rather than two separate functions.
    """

    def setUp(self):
        self._orig_request = command_executor.requests.request

    def tearDown(self):
        command_executor.requests.request = self._orig_request

    def _fake_request(self, captured, response_body):
        def fake_request(method, url, **kwargs):
            # **kwargs (not explicit json=None/timeout=None defaults) so
            # `captured` only ever contains a key the real call actually
            # passed -- a call_local_endpoint bug that started passing
            # json=None explicitly, instead of omitting it, would otherwise
            # be invisible to test_no_body_command_sends_bare_call_unchanged.
            captured["method"] = method
            captured["url"] = url
            captured.update(kwargs)

            class _Resp:
                def raise_for_status(self):
                    pass

                def json(self):
                    return response_body

            return _Resp()

        return fake_request

    def test_builds_body_from_command_id_and_params(self):
        captured = {}
        command_executor.requests.request = self._fake_request(captured, {"accepted": True, "verified": True})

        command = {
            "command_id": "chk-1", "command_type": "SET_HOME",
            "params": {"mode": "current_position", "tolerance_m": 3.0},
        }
        result = command_executor.call_local_endpoint(command)

        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/agent/set_home"))
        self.assertEqual(captured["json"], {
            "command_id": "chk-1", "mode": "current_position", "tolerance_m": 3.0,
        })
        self.assertTrue(result["verified"])

    def test_defaults_mode_to_current_position_when_params_missing(self):
        captured = {}
        command_executor.requests.request = self._fake_request(captured, {"accepted": True})

        command = {"command_id": "chk-2", "command_type": "SET_HOME"}
        command_executor.call_local_endpoint(command)

        self.assertEqual(captured["json"], {"command_id": "chk-2", "mode": "current_position"})

    def test_freshness_s_included_only_when_provided(self):
        captured = {}
        command_executor.requests.request = self._fake_request(captured, {"accepted": True})

        command = {
            "command_id": "chk-3", "command_type": "SET_HOME",
            "params": {"mode": "current_position", "freshness_s": 1.5},
        }
        command_executor.call_local_endpoint(command)

        self.assertEqual(captured["json"], {
            "command_id": "chk-3", "mode": "current_position", "freshness_s": 1.5,
        })

    def test_no_body_command_sends_bare_call_unchanged(self):
        """A command_type with no build_body (every type except SET_HOME
        today) must never receive a `json` kwarg at all -- proving the
        bodyless branch is byte-for-byte the same bare call it always
        was, not `json=None` or some other new default."""
        captured = {}
        command_executor.requests.request = self._fake_request(captured, {"status": "ok"})

        command = {"command_id": "chk-4", "command_type": "ARM"}
        result = command_executor.call_local_endpoint(command)

        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/nav/ArmOn"))
        self.assertNotIn("json", captured)
        self.assertEqual(result, {"status": "ok"})

    def test_set_home_uses_its_own_longer_default_timeout(self):
        captured = {}
        command_executor.requests.request = self._fake_request(captured, {"accepted": True})

        command_executor.call_local_endpoint({"command_id": "chk-5", "command_type": "SET_HOME"})
        set_home_timeout = captured["timeout"]

        command_executor.call_local_endpoint({"command_id": "chk-6", "command_type": "ARM"})
        arm_timeout = captured["timeout"]

        self.assertGreater(set_home_timeout, arm_timeout)

    def test_explicit_timeout_override_wins_over_spec_default(self):
        captured = {}
        command_executor.requests.request = self._fake_request(captured, {"status": "ok"})

        command_executor.call_local_endpoint({"command_id": "chk-7", "command_type": "ARM"}, timeout=42.0)

        self.assertEqual(captured["timeout"], 42.0)


class TestModeOutcomeNormalization(unittest.TestCase):
    """A 2xx from the vehicle Flask service is not a successful vehicle
    action: a mode command is only ever reported `executed` when the vehicle
    actually reached and held the expected mode (command_normalization.py).
    These exercise that end-to-end through process_command()."""

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified
        command_executor.home_verified = lambda: True

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def test_unverified_mode_change_is_failed_not_executed(self):
        command_executor.call_local_endpoint = lambda command, timeout=None: {
            "accepted": True, "verified": False, "observed_mode": 0,
            "reason": "entered RTL but reverted to custom_mode=0",
        }
        payload, event = command_handler.process_command(_make_command(command_type="RTL"))
        self.assertEqual(payload["status"], "failed")
        self.assertFalse(payload["result"]["verified"])
        self.assertEqual(payload["result"]["expected_state"], "RTL")
        self.assertEqual(event["type"], "command_failed")
        # lifecycle is part of the normalized result block.
        self.assertEqual(payload["result"]["lifecycle"][-1]["status"], "failed")

    def test_wrong_observed_mode_is_failed(self):
        command_executor.call_local_endpoint = lambda command, timeout=None: {
            "accepted": True, "verified": True, "observed_mode": 5,  # LOITER, not HOLD
        }
        payload, _ = command_handler.process_command(_make_command(command_type="SET_MODE_HOLD"))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["result"]["observed_state"], "LOITER")

    def test_verified_mode_change_is_executed_with_normalized_contract(self):
        command_executor.call_local_endpoint = lambda command, timeout=None: {
            "accepted": True, "verified": True, "observed_mode": 10,
        }
        payload, _ = command_handler.process_command(_make_command(command_type="SET_MODE_AUTO"))
        self.assertEqual(payload["status"], "executed")
        result = payload["result"]
        for key in ("accepted", "executed", "verified", "expected_state", "observed_state", "error", "lifecycle"):
            self.assertIn(key, result, f"normalized result must contain {key}")
        self.assertEqual(result["expected_state"], "AUTO")
        self.assertEqual(result["observed_state"], "AUTO")


class TestMissionPauseIsLoiter(unittest.TestCase):
    """MISSION_PAUSE stays named MISSION_PAUSE but its verified vehicle state
    is the LOITER safety hold, and -- like every safety hold -- it must remain
    available when Home is unverified."""

    def setUp(self):
        if os.path.exists(config.COMMAND_LOG_FILE):
            os.remove(config.COMMAND_LOG_FILE)
        if os.path.exists(config.COMMAND_RESULTS_FILE):
            os.remove(config.COMMAND_RESULTS_FILE)
        self._orig_call = command_executor.call_local_endpoint
        self._orig_home_verified = command_executor.home_verified

    def tearDown(self):
        command_executor.call_local_endpoint = self._orig_call
        command_executor.home_verified = self._orig_home_verified

    def test_mission_pause_never_requires_verified_home(self):
        self.assertNotIn("MISSION_PAUSE", command_executor.HOME_VERIFICATION_REQUIRED)

    def test_mission_pause_available_and_verified_as_loiter_when_home_unverified(self):
        command_executor.home_verified = lambda: False  # Home NOT verified
        command_executor.call_local_endpoint = lambda command, timeout=None: {
            "accepted": True, "verified": True, "observed_mode": 5,  # LOITER
        }
        payload, _ = command_handler.process_command(_make_command(command_type="MISSION_PAUSE"))
        self.assertEqual(payload["status"], "executed",
                         "MISSION_PAUSE must remain available while Home is unverified")
        self.assertEqual(payload["result"]["expected_state"], "LOITER")
        self.assertEqual(payload["result"]["observed_state"], "LOITER")

    def test_mission_pause_expects_loiter_not_hold(self):
        self.assertEqual(command_executor.MODE_COMMAND_EXPECTED["MISSION_PAUSE"], ("LOITER", 5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
