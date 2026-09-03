"""
Standalone tests for the Local Agent's side of control authority: it is
vehicle state read from GET /agent/state (see
motherpi/services/flask/services/control_authority.py for where it's
actually owned) -- the Local Agent has no server or state of its own for
this, it only reads vehicle_state["agent"]["control_authority"] each loop
and passes it through to command_handler.process_command(), which gates
the whole operator command queue on it (no per-command_type exemption --
see README "Authority model"). Run directly:

    python3 test_control_authority.py

Uses a temp COMMAND_LOG_FILE/BUFFER_FILE/COMMAND_RESULTS_FILE per run, same
pattern as test_command_handler.py, and clears OPERATOR_URLS so a command
result ack fails over instantly instead of spending real time timing out
against the real (unreachable in a test run) operator stations.

COMMAND_RESULTS_FILE must be overridden here for the same reason as the other
two: these tests drive the real _poll_and_execute_commands, which stores a
terminal result per command_id. Without the override those synthetic ids
(authority-gate-operator-1, ...) are written into the *live* runtime
command_results.json and persist there as retained records indistinguishable
from real vehicle operations.
"""
import os
import tempfile
import unittest

import config
config.COMMAND_LOG_FILE = tempfile.mktemp(suffix=".jsonl")
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")
config.COMMAND_RESULTS_FILE = tempfile.mktemp(suffix=".json")
config.OPERATOR_URLS = []


def tearDownModule():
    for path in (config.COMMAND_LOG_FILE, config.BUFFER_FILE,
                 config.COMMAND_RESULTS_FILE):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

import command_executor
import local_agent


class TestCurrentAuthorityExtraction(unittest.TestCase):
    """local_agent._current_authority: read vehicle_state, fail safe to OPERATOR."""

    def test_reads_authority_from_vehicle_state(self):
        vehicle_state = {"agent": {"control_authority": "LOCAL_AGENT"}}
        self.assertEqual(local_agent._current_authority(vehicle_state), "LOCAL_AGENT")

    def test_defaults_to_operator_when_field_missing(self):
        vehicle_state = {"agent": {}}
        self.assertEqual(local_agent._current_authority(vehicle_state), "OPERATOR")

    def test_defaults_to_operator_when_agent_block_missing(self):
        vehicle_state = {}
        self.assertEqual(local_agent._current_authority(vehicle_state), "OPERATOR")

    def test_startup_default_constant_is_operator(self):
        """The Local Agent's own fallback constant must match the vehicle
        Flask service's startup default (control_authority.py's module-level
        _authority = OPERATOR) -- both sides fail safe to the same value."""
        self.assertEqual(local_agent.DEFAULT_CONTROL_AUTHORITY, "OPERATOR")


class TestCommandExecutionGate(unittest.TestCase):
    """
    The command-relay gate, final model: the operator command queue is
    explicit operator intent, so a supported command_type executes while
    authority is OPERATOR and is rejected while it's LOCAL_AGENT (the Local
    Agent's own autonomous writes, gated separately by autonomy_gate.py,
    own the vehicle then) -- no per-command_type exemption (strict model,
    see README "Authority model"). Polling always happens regardless of
    authority; only comm_state == DISCONNECTED skips it entirely (README:
    no local buffering of inbound commands, nothing to poll against).

    Polling always claims the operator's deliver-once queue (see
    mock_operator.py) regardless of outcome -- there is no way to "un-claim"
    a command afterwards, so a command blocked by the authority gate is a
    terminal "rejected" result, not a silently-still-pending one.
    """

    def setUp(self):
        self._orig_get_pending = local_agent.get_pending_commands
        self._orig_call = command_executor.call_local_endpoint

    def tearDown(self):
        local_agent.get_pending_commands = self._orig_get_pending
        command_executor.call_local_endpoint = self._orig_call

    def test_operator_authority_polls(self):
        called = {"n": 0}

        def fake_get_pending(usv_id):
            called["n"] += 1
            return []

        local_agent.get_pending_commands = fake_get_pending
        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(called["n"], 1, "must poll for commands while authority is OPERATOR")

    def test_local_agent_authority_polls(self):
        called = {"n": 0}

        def fake_get_pending(usv_id):
            called["n"] += 1
            return []

        local_agent.get_pending_commands = fake_get_pending
        local_agent._poll_and_execute_commands("CONNECTED", "LOCAL_AGENT", [])

        self.assertEqual(called["n"], 1, "must poll for commands while authority is LOCAL_AGENT too")

    def test_disconnected_never_polls_regardless_of_authority(self):
        called = {"n": 0}

        def fake_get_pending(usv_id):
            called["n"] += 1
            return []

        local_agent.get_pending_commands = fake_get_pending
        local_agent._poll_and_execute_commands("DISCONNECTED", "OPERATOR", [])
        local_agent._poll_and_execute_commands("DISCONNECTED", "LOCAL_AGENT", [])

        self.assertEqual(called["n"], 0, "must never poll while DISCONNECTED, regardless of authority")

    def test_queued_command_executes_while_operator_authority_holds(self):
        """The actual Set Home symptom this gate exists to avoid repeating:
        a queued command (SET_HOME here, but the strict model applies
        identically to every supported command_type) must reach the
        vehicle Flask endpoint while authority is OPERATOR."""
        queue = [{"command_id": "authority-gate-operator-1", "command_type": "SET_HOME"}]

        def fake_get_pending(usv_id):
            due, queue[:] = list(queue), []
            return due

        local_agent.get_pending_commands = fake_get_pending
        called = {"n": 0}
        command_executor.call_local_endpoint = lambda command, timeout=None: called.update(n=called["n"] + 1) or {"accepted": True, "verified": True}

        local_agent._poll_and_execute_commands("CONNECTED", "OPERATOR", [])

        self.assertEqual(len(queue), 0, "polling always claims the deliver-once queue")
        self.assertEqual(called["n"], 1, "a supported queued command must reach the vehicle Flask endpoint while authority is OPERATOR")

    def test_queued_command_claimed_and_rejected_while_local_agent_authority_holds(self):
        """The queue is claimed (deliver-once) but never reaches the
        vehicle Flask endpoint, and is reported back to the operator as an
        explicit rejection rather than left to look permanently "in
        flight" -- while authority is LOCAL_AGENT, the queue is not the
        Local Agent's source of vehicle-control intent."""
        queue = [{"command_id": "authority-gate-local-agent-1", "command_type": "SET_MODE_HOLD"}]

        def fake_get_pending(usv_id):
            due, queue[:] = list(queue), []
            return due

        local_agent.get_pending_commands = fake_get_pending
        called = {"n": 0}
        command_executor.call_local_endpoint = lambda command, timeout=None: called.update(n=called["n"] + 1)

        local_agent._poll_and_execute_commands("CONNECTED", "LOCAL_AGENT", [])

        self.assertEqual(len(queue), 0, "polling always claims the deliver-once queue")
        self.assertEqual(called["n"], 0, "a queued command must never reach the vehicle Flask endpoint while authority is LOCAL_AGENT")


class TestSourceIsProvenanceNotAuthority(unittest.TestCase):
    """
    `source` records WHO asked. It is provenance only, and must never
    influence whether a command is allowed to run. Authority comes solely
    from the Scout-owned control-authority state.

    The failure mode being locked out: a command arriving with a
    privileged-looking source ("operator", "admin", "system", ...) being
    allowed to execute while authority is LOCAL_AGENT -- i.e. an attacker or
    a buggy backend escalating simply by relabelling a field.
    """

    def setUp(self):
        self._orig_get_pending = local_agent.get_pending_commands
        self._orig_send = local_agent.send_to_operator
        self._orig_call = command_executor.call_local_endpoint
        local_agent.send_to_operator = lambda path, message: {"status": "ok"}

    def tearDown(self):
        local_agent.get_pending_commands = self._orig_get_pending
        local_agent.send_to_operator = self._orig_send
        command_executor.call_local_endpoint = self._orig_call

    def _run_with_source(self, source, authority):
        queue = [{"command_id": f"src-{source}-{authority}",
                  "command_type": "SET_MODE_HOLD", "source": source}]

        def fake_get_pending(usv_id):
            due, queue[:] = list(queue), []
            return due

        local_agent.get_pending_commands = fake_get_pending
        called = {"n": 0}
        command_executor.call_local_endpoint = (
            lambda command, timeout=None: called.update(n=called["n"] + 1)
            or {"accepted": True, "verified": True, "observed_mode": 4}
        )
        local_agent._poll_and_execute_commands("CONNECTED", authority, [])
        return called["n"]

    def test_no_source_value_grants_authority_under_local_agent(self):
        for source in ("operator", "admin", "system", "OPERATOR", "local_agent", "root", ""):
            with self.subTest(source=source):
                self.assertEqual(
                    self._run_with_source(source, "LOCAL_AGENT"), 0,
                    f"source={source!r} must not let a command execute while authority is LOCAL_AGENT",
                )

    def test_no_source_value_blocks_execution_under_operator_authority(self):
        for source in ("operator", "unknown-source", "", "local_agent"):
            with self.subTest(source=source):
                self.assertEqual(
                    self._run_with_source(source, "OPERATOR"), 1,
                    f"source={source!r} must not block a command while authority is OPERATOR",
                )

    def test_source_is_preserved_in_the_result_as_provenance(self):
        """Preserved, just never consulted for authority."""
        from command_handler import process_command
        command_executor.call_local_endpoint = (
            lambda command, timeout=None: {"accepted": True, "verified": True, "observed_mode": 4}
        )
        payload, _event = process_command(
            {"command_id": "src-provenance-1", "command_type": "SET_MODE_HOLD",
             "source": "operator-station-42"},
            control_authority="OPERATOR",
        )
        self.assertEqual(payload["source"], "operator-station-42")


if __name__ == "__main__":
    unittest.main(verbosity=2)
