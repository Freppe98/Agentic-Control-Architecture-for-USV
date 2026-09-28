"""Scout companion-v2 COMPATIBILITY tests — real, off-the-wire Scout envelopes replayed through
the REAL `POST /agent/status` route (see scripts/scout_payload_replay.py for the shared replay
machinery and tests/fixtures/scout_companion_payloads/README.md for the capture format).

Run from operator-scripts/:  python -m unittest tests.test_scout_companion_compat -v

TWO TEST CLASSES, TWO DIFFERENT JOBS
-------------------------------------
`HarnessMechanicsTests` proves the REPLAY HARNESS ITSELF works — file discovery, single-envelope
vs sequence-array loading, and the "HTTP 200 is not success, check /api/fleet/status" assertion
style — using a small SYNTHETIC envelope built the same way `companion_fixture.py`/
`test_companion_telemetry.py` already do. It always runs, and proves nothing about Scout
compatibility (an operator-authored payload cannot prove that — see COMPANION_CONTRACT.md's
intro and the fixtures README) — only that this test file's own machinery is not the thing that
would produce a false pass or a false fail.

`ScoutCompatibilityTests` is the actual target: it discovers REAL captures under
`tests/fixtures/scout_companion_payloads/*.json` and replays them. With no captures present yet
(Scout has not implemented companion-v2 — see COMPANION_CONTRACT.md's status line) it SKIPS with
an actionable message rather than passing trivially or failing — a skip here means "nothing to
verify yet," which is an honest, different claim from "verified compatible."
"""
import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts")))

import main  # noqa: E402
import companion_telemetry as ct  # noqa: E402
import companion_fixture as fx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import scout_payload_replay as replay  # noqa: E402

PAYLOAD_DIR = Path(__file__).parent / "fixtures" / "scout_companion_payloads"
SCOUT = fx.SCOUT


class _ReplayTestCase(unittest.TestCase):
    """Shared setUp: a clean TestClient + a clean companion/status state, one per test — the
    SAME isolation every companion test file in this suite already relies on (the test-runner
    detection in main._resolve_runtime_dir routes MISSION_STORE_DIR, and therefore
    COMPANION_SESSIONS_PATH, away from runtime_data/ for the whole process; this just also
    clears the in-memory stores between tests)."""

    def setUp(self):
        self.client = TestClient(main.app)
        for store in (main.current_vehicle_state, main.last_known_telemetry,
                      main.last_known_agent, main.latest_msg_ts_by_id, main.last_seen_by_id,
                      main.comms_state_by_id, main.comms_history_by_id,
                      main.last_agent_decision_by_id, main.last_mission_state_by_id,
                      main.last_known_groups, main.packet_loss_by_id,
                      main.companion_state_by_id):
            store.clear()

    def replay_file(self, path):
        envelopes = replay.load_envelopes(path)
        results = replay.replay(self.client, envelopes)
        for env, body, status in results:
            vid_raw = (env.get("payload") or {}).get("usv_id", env.get("source"))
            self.assertEqual(status, 200,
                             f"{path.name}: envelope naming {vid_raw!r} was not even identified "
                             f"as a known vehicle (HTTP {status}, body={body})")
        return results

    def fleet_row(self, cid=SCOUT):
        rows = [v for v in self.client.get("/api/fleet/status").json() if v["id"] == cid]
        self.assertEqual(len(rows), 1, f"vehicle {cid!r} did not appear on the fleet endpoint")
        return rows[0]


# ── proves the harness works, using a SYNTHETIC (operator-authored) capture only ──────────────
class HarnessMechanicsTests(_ReplayTestCase):
    def test_discover_payload_files_finds_json_files_sorted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "b.json").write_text("{}", encoding="utf-8")
            (tmp / "a.json").write_text("{}", encoding="utf-8")
            (tmp / "notes.txt").write_text("ignore me", encoding="utf-8")
            found = replay.discover_payload_files(tmp)
            self.assertEqual([p.name for p in found], ["a.json", "b.json"])

    def test_discover_payload_files_on_a_missing_directory_is_empty_not_an_error(self):
        self.assertEqual(replay.discover_payload_files(Path("no/such/directory")), [])

    def test_load_envelopes_accepts_a_single_object_or_an_array(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            single = Path(tmp) / "single.json"
            single.write_text(json.dumps({"message_type": "status"}), encoding="utf-8")
            self.assertEqual(replay.load_envelopes(single), [{"message_type": "status"}])

            seq = Path(tmp) / "seq.json"
            seq.write_text(json.dumps([{"a": 1}, {"a": 2}]), encoding="utf-8")
            self.assertEqual(replay.load_envelopes(seq), [{"a": 1}, {"a": 2}])

    def test_load_envelopes_rejects_a_file_that_is_neither(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text('"just a string"', encoding="utf-8")
            with self.assertRaises(ValueError):
                replay.load_envelopes(bad)

    def test_replay_posts_a_synthetic_capture_through_the_real_endpoint_and_is_visible_on_fleet_status(self):
        # SYNTHETIC on purpose — this proves the machinery, not Scout compatibility (see the
        # module docstring). Built with companion_fixture, exactly like every other operator-
        # authored companion test in this suite.
        t = time.time()
        envelopes = [fx.envelope(SCOUT, t, companions=fx.block(
            fx.uav(t, hazards=[fx.buoy(t)]), seq=1))]
        results = self.replay_file_from_envelopes(envelopes)
        self.assertEqual(len(results), 1)
        row = self.fleet_row()
        self.assertEqual(row["companions"]["schema"], "companion-v2")
        self.assertEqual(row["companions"]["rejections"]["count"], 0,
                         "HTTP 200 alone proves nothing — this checks the ACTUAL accepted state")
        self.assertEqual(len(row["companions"]["items"]), 1)
        self.assertEqual(row["companions"]["items"][0]["companion_id"], "uav-1")
        self.assertEqual(len(row["companions"]["items"][0]["hazards"]), 1)

    def test_a_malformed_companion_block_is_reported_as_a_rejection_not_a_500(self):
        # The exact case scripts/scout_payload_replay.py's docstring warns about: a bad
        # companion block never shows up as a non-200 — only in fleet-status rejections.
        t = time.time()
        bad = fx.envelope(SCOUT, t, companions={"schema": "companion-v2",
                                                "session": {"id": "s", "seq": 1, "generation": 1},
                                                "items": "not a list"})
        results = self.replay_file_from_envelopes([bad])
        self.assertEqual(results[0][2], 200, "a malformed companion block must not 500")
        row = self.fleet_row()
        self.assertEqual(row["companions"]["rejections"]["by_reason"].get("items_not_list"), 1)

    def replay_file_from_envelopes(self, envelopes):
        return replay.replay(self.client, envelopes)


# ── the actual target: real, captured Scout output ─────────────────────────────────────────
class ScoutCompatibilityTests(_ReplayTestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture_files = replay.discover_payload_files(PAYLOAD_DIR)

    def setUp(self):
        if not self.capture_files:
            self.skipTest(
                "no captured Scout companion-v2 payloads yet at "
                f"{PAYLOAD_DIR.relative_to(Path(main.__file__).resolve().parent)} — this is "
                "expected until Scout implements companion-v2 (see COMPANION_CONTRACT.md's "
                "status line). See SCOUT_INTEGRATION_HANDOFF.md's 'Handing back a compatibility "
                "capture' section for exactly what to drop in that directory, and "
                "scripts/scout_payload_replay.py to replay it by hand in the meantime.")
        super().setUp()

    def test_every_capture_is_accepted_as_a_well_formed_companion_v2_block(self):
        for path in self.capture_files:
            with self.subTest(file=path.name):
                self.replay_file(path)
                envelopes = replay.load_envelopes(path)
                vids = {main.canonical_id((e.get("payload") or {}).get("usv_id", e.get("source")))
                       for e in envelopes}
                vids.discard(None)
                for vid in vids:
                    row = self.fleet_row(vid)
                    blk = row["companions"]
                    rej = blk["rejections"]
                    self.assertEqual(
                        rej["count"], 0,
                        f"{path.name} ({main.vehicle_slug(vid)}): Scout's own payload was "
                        f"rejected by companion_telemetry.py — {rej['by_reason']} "
                        f"(last: {rej['last_reason']}). This is a REAL compatibility finding, "
                        "not a harness bug: see COMPANION_CONTRACT.md section 8 for what each "
                        "reason means and fix the publisher, not this assertion.")
                    self.assertEqual(blk["schema"], "companion-v2")
                    self.assertIsNotNone(blk["session"], f"{path.name}: no session was accepted")


if __name__ == "__main__":
    unittest.main()
