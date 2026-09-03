"""
Standalone tests for buffer.py -- local storage/flush for unsent packets,
in particular the MAX_BUFFERED_MESSAGES cap (Priority 5: a route mismatch
like the operator backend 405ing /agent/command_result must not grow
agent_buffer.jsonl without bound). Run directly:

    python3 test_buffer.py
"""
import tempfile
import unittest

import config
config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")
config.MAX_BUFFERED_MESSAGES = 5

import buffer


class TestBufferCap(unittest.TestCase):
    def setUp(self):
        config.BUFFER_FILE = tempfile.mktemp(suffix=".jsonl")
        buffer.BUFFER_FILE = config.BUFFER_FILE
        # buffer.py snapshots both BUFFER_FILE and MAX_BUFFERED_MESSAGES via
        # `from config import ...` at its own import time, so overriding
        # config alone isn't enough if buffer was imported first (e.g. by
        # another test module earlier in discovery). Re-point the module's
        # own binding, same as BUFFER_FILE above, so these cap tests hold
        # regardless of import order.
        buffer.MAX_BUFFERED_MESSAGES = config.MAX_BUFFERED_MESSAGES

    def test_buffering_under_cap_keeps_everything(self):
        for i in range(3):
            buffer.buffer_message({"n": i})
        self.assertEqual([m["n"] for m in buffer.read_buffered_messages()], [0, 1, 2])

    def test_buffering_past_cap_drops_oldest_first(self):
        for i in range(config.MAX_BUFFERED_MESSAGES + 10):
            buffer.buffer_message({"n": i})
        messages = buffer.read_buffered_messages()
        self.assertEqual(len(messages), config.MAX_BUFFERED_MESSAGES)
        # newest entries survive, oldest were dropped
        self.assertEqual(
            [m["n"] for m in messages],
            list(range(10, 10 + config.MAX_BUFFERED_MESSAGES)),
        )

    def test_a_message_type_that_always_fails_cannot_grow_the_buffer_unbounded(self):
        """
        Simulates the real scenario this cap exists for: every command_result
        send fails identically (e.g. a persistent 405 route mismatch, not a
        transient connectivity gap), so buffer_message() is called far more
        times than MAX_BUFFERED_MESSAGES over a long session.
        """
        def always_fails(_message):
            raise RuntimeError("405 Method Not Allowed")

        for i in range(200):
            try:
                always_fails({"command_id": i})
            except RuntimeError:
                buffer.buffer_message({"command_id": i})

        self.assertLessEqual(len(buffer.read_buffered_messages()), config.MAX_BUFFERED_MESSAGES)

    def test_repeated_command_result_for_same_command_id_does_not_append_new_lines(self):
        """Requirement 3: repeated polls for the same command_id (each
        redelivery producing its own buffer_message() call, since posting
        keeps failing) must not append new command_result lines
        indefinitely -- a buffered entry for a command_id is replaced in
        place, not duplicated."""
        def command_result(command_id, status):
            return {"message_type": "command_result", "payload": {"command_id": command_id, "status": status}}

        buffer.buffer_message(command_result("cid-1", "executed"))
        for _ in range(8):
            buffer.buffer_message(command_result("cid-1", "executed"))

        messages = buffer.read_buffered_messages()
        matching = [m for m in messages if m["payload"]["command_id"] == "cid-1"]
        self.assertEqual(len(matching), 1)

    def test_command_result_dedup_keeps_latest_content_for_the_same_id(self):
        buffer.buffer_message({"message_type": "command_result", "payload": {"command_id": "cid-2", "status": "executed"}})
        buffer.buffer_message({"message_type": "command_result", "payload": {"command_id": "cid-2", "status": "failed"}})

        messages = buffer.read_buffered_messages()
        matching = [m for m in messages if m["payload"]["command_id"] == "cid-2"]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["payload"]["status"], "failed")

    def test_command_result_dedup_does_not_affect_other_command_ids_or_message_types(self):
        buffer.buffer_message({"message_type": "command_result", "payload": {"command_id": "cid-a", "status": "executed"}})
        buffer.buffer_message({"message_type": "command_result", "payload": {"command_id": "cid-b", "status": "executed"}})
        buffer.buffer_message({"message_type": "status", "payload": {"comm_state": "CONNECTED"}})

        self.assertEqual(len(buffer.read_buffered_messages()), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
