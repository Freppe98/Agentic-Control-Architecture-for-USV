"""Backend tests for DURABLE companion-session ordering (main.py: _save_companion_sessions /
_load_companion_sessions, on top of companion_telemetry.py's session/seq/generation ordering).

Run from operator-scripts/:  python -m unittest tests.test_companion_session_persistence

WHY THIS FILE EXISTS
---------------------
COMPANION_CONTRACT.md documented a real gap: the operator persisted the accepted
`{id, seq, generation}` ordering triplet only when `generation` changed, not on an ordinary
same-generation `seq` advance. That left a window, right after an operator restart, where a
delayed/buffered packet carrying an already-superseded `seq` (same generation) could be accepted
as if it were new — because the freshly-restarted process had only ever seen the OLD, stale `seq`
persisted from before the last generation change, not the newer one actually accepted afterward.

The fix (companion_telemetry.ingest() now signals "worth persisting" for an ordinary `seq`
advance too, not only a generation advance) is exercised here through the REAL
`POST /agent/status` route and the REAL `_save_companion_sessions` / `_load_companion_sessions`
functions — this module has no I/O of its own (see its own docstring), so the actual restart
behaviour can only be pinned at this layer, not inside tests/test_companion_telemetry.py.

These tests also pin the EXPLICIT persistence-failure behaviour: a write that cannot land on
disk must never raise, must never block ingestion, and must never be silent — it is tracked
(`main._companion_sessions_persist_*`), retried, and surfaced on `GET /api/diagnostics`.

Every test writes to its own temp directory; the station's real runtime_data is never touched
(see tests/test_mission_store_isolation.py for the structural guarantee this relies on —
COMPANION_SESSIONS_PATH is always derived from MISSION_STORE_DIR).
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import main  # noqa: E402
import companion_telemetry as ct  # noqa: E402
import companion_fixture as fx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT = fx.SCOUT


def env(t, seq, *, generation=1, session_id="scout-session", hazards=None):
    """A minimal, real companion-v2 envelope at packet time `t`, explicit seq/generation."""
    return fx.envelope(SCOUT, t, companions={
        "schema": ct.SCHEMA,
        "session": {"id": session_id, "seq": seq, "generation": generation},
        "items": [fx.uav(t, hazards=[] if hazards is None else hazards)],
    })


class CompanionSessionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="operator-companion-sessions-test-"))
        self._real_dir = main.MISSION_STORE_DIR
        self._real_companion_sessions_path = main.COMPANION_SESSIONS_PATH
        main.MISSION_STORE_DIR = self.tmp
        main.COMPANION_SESSIONS_PATH = self.tmp / "companion_sessions.json"
        self._real_companion_state = dict(main.companion_state_by_id)
        main.companion_state_by_id.clear()
        # Real status-endpoint bookkeeping this route also touches, so a fresh test starts with
        # no cross-test history for Scout (same clearing test_companion_telemetry.py's Base does).
        self._cleared_stores = (
            main.current_vehicle_state, main.last_known_telemetry, main.last_known_agent,
            main.latest_msg_ts_by_id, main.last_seen_by_id, main.comms_state_by_id,
            main.comms_history_by_id, main.last_agent_decision_by_id,
            main.last_mission_state_by_id, main.last_known_groups, main.packet_loss_by_id)
        self._saved_stores = [dict(s) for s in self._cleared_stores]
        for s in self._cleared_stores:
            s.clear()
        self._real_persist_ok = main._companion_sessions_persist_ok
        self._real_persist_failures = main._companion_sessions_persist_failures
        self._real_persist_last_error = main._companion_sessions_persist_last_error
        self._real_persist_last_attempt = main._companion_sessions_persist_last_attempt
        main._companion_sessions_persist_ok = True
        main._companion_sessions_persist_failures = 0
        main._companion_sessions_persist_last_error = None
        main._companion_sessions_persist_last_attempt = None
        self.client = TestClient(main.app)

    def tearDown(self):
        main.MISSION_STORE_DIR = self._real_dir
        main.COMPANION_SESSIONS_PATH = self._real_companion_sessions_path
        main.companion_state_by_id.clear()
        main.companion_state_by_id.update(self._real_companion_state)
        for s, saved in zip(self._cleared_stores, self._saved_stores):
            s.clear()
            s.update(saved)
        main._companion_sessions_persist_ok = self._real_persist_ok
        main._companion_sessions_persist_failures = self._real_persist_failures
        main._companion_sessions_persist_last_error = self._real_persist_last_error
        main._companion_sessions_persist_last_attempt = self._real_persist_last_attempt
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _post(self, envelope):
        r = self.client.post("/agent/status", json=envelope)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _persisted(self):
        with open(main.COMPANION_SESSIONS_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def _row(self):
        rows = [v for v in self.client.get("/api/fleet/status").json() if v["id"] == SCOUT]
        self.assertEqual(len(rows), 1)
        return rows[0]

    # ── the fix itself: a same-generation seq advance is persisted, not only a generation
    #    change ──────────────────────────────────────────────────────────────────────────
    def test_a_same_generation_seq_advance_is_persisted_immediately(self):
        t = time.time()
        self._post(env(t, seq=1))                       # "new" — always persisted
        self.assertEqual(self._persisted()["vehicles"]["usv-2"]["seq"], 1)
        self._post(env(t + 1, seq=2))                    # "advance_seq" — must ALSO persist
        self.assertEqual(self._persisted()["vehicles"]["usv-2"]["seq"], 2,
                         "a same-generation seq advance must be durable, not only a generation "
                         "change — see COMPANION_CONTRACT.md's Snapshot ordering / Operator "
                         "restarts section")
        self._post(env(t + 2, seq=5))
        self.assertEqual(self._persisted()["vehicles"]["usv-2"]["seq"], 5)

    def test_an_idempotent_resend_at_an_unchanged_seq_does_not_rewrite_the_file(self):
        t = time.time()
        self._post(env(t, seq=1))
        written_at_seq_1 = main.COMPANION_SESSIONS_PATH.stat().st_mtime_ns
        time.sleep(0.01)
        self._post(env(t + 1, seq=1))                    # identical seq: "unchanged" outcome
        self.assertEqual(main.COMPANION_SESSIONS_PATH.stat().st_mtime_ns, written_at_seq_1,
                         "an idempotent heartbeat resend must not trigger a disk write")

    # ── the actual restart scenario the gap was about ──────────────────────────────────────
    def test_a_restart_seeded_from_the_persisted_seq_rejects_an_already_superseded_replay(self):
        t = time.time()
        self._post(env(t, seq=1))
        self._post(env(t + 1, seq=2))
        self._post(env(t + 2, seq=3))                    # the newest ACCEPTED seq before "restart"
        self.assertEqual(self._persisted()["vehicles"]["usv-2"]["seq"], 3)

        # Simulate an operator restart: drop ALL in-memory companion state (as a fresh process
        # would have) and seed ONLY the ordering triplet from what was actually persisted on
        # disk — exactly what main.py's lifespan does via _load_companion_sessions().
        main.companion_state_by_id.clear()
        status = main._load_companion_sessions()
        self.assertIn("restored ordering state for 1 vehicle", status)
        self.assertEqual(main.companion_state_by_id[SCOUT]["companions"], {},
                         "no companion data is restored — only ordering protection")

        # A delayed packet from BEFORE the restart, carrying the OLD seq=2 (already superseded
        # by seq=3 before the crash): under the pre-fix behaviour (persist only on generation
        # change) this would have looked "new" to a process that never itself saw seq=3. It must
        # now be rejected.
        self._post(env(t + 10, seq=2))
        row = self._row()
        self.assertEqual(row["companions"]["rejections"]["by_reason"].get("stale_snapshot_seq"), 1)
        self.assertEqual(row["companions"]["items"], [],
                         "the stale replay must not resurrect companion state after the restart")

        # A genuinely newer packet (seq=4) still applies normally afterward.
        self._post(env(t + 11, seq=4))
        row = self._row()
        self.assertEqual(len(row["companions"]["items"]), 1)
        self.assertEqual(row["companions"]["session"]["seq"], 4)

    def test_a_generation_advance_is_still_persisted_and_protected_across_a_restart(self):
        """The pre-existing guarantee (generation changes were always persisted) must survive
        this change unchanged — this is a non-regression pin, not new behaviour."""
        t = time.time()
        self._post(env(t, seq=1, generation=1, session_id="s1"))
        self._post(env(t + 1, seq=1, generation=2, session_id="s2"))   # genuine restart
        self.assertEqual(self._persisted()["vehicles"]["usv-2"],
                         {"id": "s2", "seq": 1, "generation": 2})

        main.companion_state_by_id.clear()
        main._load_companion_sessions()
        self._post(env(t + 20, seq=99, generation=1, session_id="s1"))  # retired generation replay
        row = self._row()
        self.assertEqual(row["companions"]["rejections"]["by_reason"].get(
            "retired_session_generation"), 1)
        self.assertEqual(row["companions"]["items"], [])

    # ── explicit persistence-failure behaviour ─────────────────────────────────────────────
    def test_a_failed_write_is_tracked_explicitly_and_does_not_raise(self):
        main.MISSION_STORE_DIR = self.tmp / "not-a-directory"
        main.MISSION_STORE_DIR.write_text("a file, not a directory", encoding="utf-8")
        main.COMPANION_SESSIONS_PATH = main.MISSION_STORE_DIR / "companion_sessions.json"

        self.assertTrue(main._companion_sessions_persist_ok, "starts healthy")
        ok = self._post(env(time.time(), seq=1))
        self.assertTrue(ok["ok"], "ingestion itself must not fail because persistence failed")
        self.assertFalse(main._companion_sessions_persist_ok)
        self.assertEqual(main._companion_sessions_persist_failures, 1)
        self.assertIsNotNone(main._companion_sessions_persist_last_error)
        self.assertIsNotNone(main._companion_sessions_persist_last_attempt)

        # Consecutive failures keep counting, not resetting.
        self._post(env(time.time() + 1, seq=2))
        self.assertEqual(main._companion_sessions_persist_failures, 2)

    def test_in_memory_ordering_protection_still_works_while_persistence_is_broken(self):
        """A stuck disk must not open a replay hole for the REST OF THIS PROCESS's life — only
        durability ACROSS A CRASH is what a failed write actually loses (see the module
        docstring)."""
        main.MISSION_STORE_DIR = self.tmp / "not-a-directory"
        main.MISSION_STORE_DIR.write_text("a file, not a directory", encoding="utf-8")
        main.COMPANION_SESSIONS_PATH = main.MISSION_STORE_DIR / "companion_sessions.json"

        t = time.time()
        self._post(env(t, seq=1))
        self._post(env(t + 1, seq=5))
        self.assertFalse(main._companion_sessions_persist_ok, "persistence really is broken here")
        self._post(env(t + 2, seq=3))                     # an older seq, same (unwritten) process
        row = self._row()
        self.assertEqual(row["companions"]["rejections"]["by_reason"].get("stale_snapshot_seq"), 1)
        self.assertEqual(row["companions"]["session"]["seq"], 5, "the replay did not apply")

    def test_a_recovered_write_clears_the_failure_state_and_logs_recovery(self):
        main.MISSION_STORE_DIR = self.tmp / "not-a-directory"
        main.MISSION_STORE_DIR.write_text("a file, not a directory", encoding="utf-8")
        main.COMPANION_SESSIONS_PATH = main.MISSION_STORE_DIR / "companion_sessions.json"
        self._post(env(time.time(), seq=1))
        self.assertFalse(main._companion_sessions_persist_ok)

        # The disk becomes writable again (what the retry in _comms_monitor_loop exercises).
        (self.tmp / "not-a-directory").unlink()
        main.MISSION_STORE_DIR = self.tmp
        main.COMPANION_SESSIONS_PATH = self.tmp / "companion_sessions.json"
        self.assertTrue(main._save_companion_sessions())
        self.assertTrue(main._companion_sessions_persist_ok)
        self.assertEqual(main._companion_sessions_persist_failures, 0)
        self.assertIsNone(main._companion_sessions_persist_last_error)

    def test_diagnostics_surfaces_persistence_health_honestly(self):
        main.MISSION_STORE_DIR = self.tmp / "not-a-directory"
        main.MISSION_STORE_DIR.write_text("a file, not a directory", encoding="utf-8")
        main.COMPANION_SESSIONS_PATH = main.MISSION_STORE_DIR / "companion_sessions.json"
        self._post(env(time.time(), seq=1))

        diag = self.client.get("/api/diagnostics").json()
        health = diag["companion_sessions_persistence"]
        self.assertFalse(health["ok"])
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertIsNotNone(health["last_error"])
        self.assertEqual(health["path"], str(main.COMPANION_SESSIONS_PATH))

    # ── legitimate repeated reports / state restoration keep working (not just the replay
    #    rejection above) ──────────────────────────────────────────────────────────────────
    def test_a_repeated_heartbeat_at_the_same_seq_after_a_restart_is_accepted_as_idempotent(self):
        t = time.time()
        self._post(env(t, seq=1))
        self._post(env(t + 1, seq=2))
        main.companion_state_by_id.clear()
        main._load_companion_sessions()
        # Scout's very next packet after the restart simply repeats its last-known truth at the
        # SAME seq (a normal heartbeat, not a new change) — this must be accepted, not rejected
        # as if it were a stale replay, and it must (re)populate companion data that a restart
        # legitimately starts empty.
        self._post(env(t + 30, seq=2))
        row = self._row()
        self.assertEqual(row["companions"]["rejections"]["count"], 0)
        self.assertEqual(len(row["companions"]["items"]), 1)
        self.assertEqual(row["companions"]["session"]["seq"], 2)


if __name__ == "__main__":
    unittest.main()
