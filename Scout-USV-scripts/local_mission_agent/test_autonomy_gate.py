"""
Standalone tests for autonomy_gate.check_autonomous_write_authority -- the
gate every Local Agent autonomous vehicle-control write (as opposed to
an operator-command-relay write, see test_command_handler.py/
test_control_authority.py for that separate gate) calls immediately
before writing -- mission_execution_controller.py's and replan_controller.py's
own `_authorized()` helpers wrap this check; see autonomy_gate.py's module
docstring for the full caller list and the narrow, deliberate LOITER-only
safety-hold exception. Run directly:

    python3 test_autonomy_gate.py
"""
import unittest

import autonomy_gate


class TestAutonomousWriteGate(unittest.TestCase):
    def test_allowed_under_local_agent_authority(self):
        allowed, reason = autonomy_gate.check_autonomous_write_authority("LOCAL_AGENT")
        self.assertTrue(allowed)
        self.assertIn("LOCAL_AGENT", reason)

    def test_blocked_under_operator_authority(self):
        allowed, reason = autonomy_gate.check_autonomous_write_authority("OPERATOR")
        self.assertFalse(allowed)
        self.assertIn("requires LOCAL_AGENT control authority", reason)
        self.assertIn("OPERATOR", reason)

    def test_fails_closed_on_unrecognized_authority_value(self):
        """A malformed/unrecognized value (e.g. a failed GET /agent/state
        fetch that fell back to something unexpected) must never be treated
        as a grant -- only the exact string "LOCAL_AGENT" allows a write."""
        allowed, reason = autonomy_gate.check_autonomous_write_authority("")
        self.assertFalse(allowed)

        allowed, reason = autonomy_gate.check_autonomous_write_authority(None)
        self.assertFalse(allowed)

        allowed, reason = autonomy_gate.check_autonomous_write_authority("local_agent")
        self.assertFalse(allowed, "must be case-sensitive, never a loose match")


if __name__ == "__main__":
    unittest.main(verbosity=2)
