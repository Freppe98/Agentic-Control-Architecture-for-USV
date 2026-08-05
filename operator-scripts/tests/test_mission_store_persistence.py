"""Backend tests for the DURABLE mission store (main.py: _save_mission_store / _load_mission_store).

Run from operator-scripts/:  python -m unittest tests.test_mission_store_persistence

An approved, verified mission and which mission is ACTIVE per vehicle are the only two pieces
of operator state that cannot be reconstructed after a restart — only a fresh plan → upload →
verified read-back creates one, and losing the record silently drops replanning readiness for
a vehicle that is still flying that route. So exactly those two stores are persisted, and
these tests pin both halves of that contract:

  * what IS persisted — finalization, upload-status changes, verification, active-original
    replacement — and that a reload restores it byte-for-byte;
  * what is NOT — no telemetry, no commands, no events, no readiness / Pixhawk read-back
    caches, no Scout package responses. Restoring live evidence would be a fabricated
    observation; it is re-read from the vehicle and from Scout on the next poll;
  * that the loader FAILS CLOSED: a corrupt, truncated, incompatible or internally
    inconsistent snapshot starts an EMPTY store, never a partially-loaded one, and the file
    is left on disk for inspection rather than repaired;
  * that the write is atomic — no temp file survives, and a failed write never takes the
    in-memory truth down with it.

Every test writes to its own temp directory; the station's real runtime_data is never touched.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                            "active-original-msn-329c2faff137.json")


def a_record(mission_id="msn-store-0001", vid=2, upload_status="VERIFIED"):
    """A self-consistent record: its stored route_hash IS the canonical hash of its own
    waypoints, which is what the loader's integrity check verifies."""
    wps = [{"latitude": 56.0 + i * 1e-4, "longitude": 12.0 + i * 1e-4, "loiter_time_s": 0}
           for i in range(6)]
    return {
        "mission_id": mission_id, "mission_revision": 0, "vehicle_id": vid,
        "route_waypoints": wps,
        "route_hash": mission_contract.route_content_hash(wps),
        "original_execution_order": [{"execution_seq": i, "source_segment_kind": "primary"}
                                     for i in range(len(wps))],
        "navigable_geometry": [[[12.0, 56.0], [12.01, 56.0], [12.01, 56.01], [12.0, 56.0]]],
        "no_go_zones": [],
        "planning_inputs": {"planning_home": {"latitude": 56.0, "longitude": 12.0}},
        "metrics": {"shoreline_clearance_m": 5},
        "upload_status": upload_status, "verified_at": None, "immutable": True,
    }


class MissionStorePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="operator-mission-store-test-"))
        self._real_dir, self._real_path = main.MISSION_STORE_DIR, main.MISSION_STORE_PATH
        main.MISSION_STORE_DIR = self.tmp
        main.MISSION_STORE_PATH = self.tmp / "mission_store.json"
        self._real_missions = dict(main.original_missions)
        self._real_active = dict(main.active_original_by_vehicle)
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()

    def tearDown(self):
        main.MISSION_STORE_DIR, main.MISSION_STORE_PATH = self._real_dir, self._real_path
        main.original_missions.clear()
        main.original_missions.update(self._real_missions)
        main.active_original_by_vehicle.clear()
        main.active_original_by_vehicle.update(self._real_active)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self, rec, active=True):
        main.original_missions[rec["mission_id"]] = rec
        if active:
            main.active_original_by_vehicle[rec["vehicle_id"]] = rec["mission_id"]

    def _written(self):
        with open(main.MISSION_STORE_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    # ── round trip ────────────────────────────────────────────────────────────────────
    def test_save_then_load_restores_the_record_and_the_active_pointer(self):
        rec = a_record()
        self._seed(rec)
        self.assertTrue(main._save_mission_store())
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        main._load_mission_store()
        self.assertEqual(main.original_missions[rec["mission_id"]], rec)
        self.assertEqual(main.active_original_by_vehicle, {2: rec["mission_id"]})

    def test_verified_status_survives_the_reload(self):
        self._seed(a_record(upload_status="VERIFIED"))
        main._save_mission_store()
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        main._load_mission_store()
        self.assertEqual(main.original_missions["msn-store-0001"]["upload_status"], "VERIFIED")

    def test_the_real_captured_record_round_trips_unchanged(self):
        # The captured verified mission, not a hand-written one: the whole risk is that a
        # snapshot quietly reshapes structures only the real planner produces.
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["vehicle_id"] = 2
        self._seed(rec)
        main._save_mission_store()
        main.original_missions.clear()
        main._load_mission_store()
        self.assertEqual(main.original_missions[rec["mission_id"]], rec)

    def test_vehicle_keys_survive_json_stringification(self):
        # JSON object keys are strings; vehicle ids are ints. A round trip must not leave the
        # active map keyed by "2" — every lookup in the station uses the int id.
        self._seed(a_record(vid=3, mission_id="msn-store-usv3"))
        main._save_mission_store()
        self.assertEqual(list(self._written()["active_original_by_vehicle"]), ["3"])
        main.active_original_by_vehicle.clear()
        main._load_mission_store()
        self.assertEqual(main.active_original_by_vehicle, {3: "msn-store-usv3"})

    def test_active_replacement_is_what_the_next_load_sees(self):
        first, second = a_record("msn-first"), a_record("msn-second")
        self._seed(first)
        main._save_mission_store()
        self._seed(second)                       # replaces usv-2's active original
        main._save_mission_store()
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        main._load_mission_store()
        self.assertEqual(main.active_original_by_vehicle, {2: "msn-second"})
        self.assertIn("msn-first", main.original_missions)   # history is kept, not the pointer

    # ── scope: exactly two stores, nothing else ───────────────────────────────────────
    def test_only_the_two_mission_stores_are_persisted(self):
        self._seed(a_record())
        main._save_mission_store()
        self.assertEqual(set(self._written()),
                         {"version", "saved_at", "original_missions",
                          "active_original_by_vehicle"})

    def test_transient_state_is_never_written(self):
        # Telemetry, commands, events, the readiness / Pixhawk read-back caches and Scout's
        # package responses are LIVE EVIDENCE. A restored copy would be a fabricated
        # observation, so none of it may appear in the snapshot.
        self._seed(a_record())
        main.last_known_agent[2] = {"telemetry": {"battery_percent": 42}}
        main._pixhawk_readback_cache[2] = ("when", {"route_content_hash": "sha256:cached"})
        main._save_mission_store()
        blob = json.dumps(self._written())
        for leak in ("battery_percent", "sha256:cached", "last_known_agent", "commands",
                     "event_log", "replan_operations", "readback"):
            self.assertNotIn(leak, blob)

    # ── fail closed ───────────────────────────────────────────────────────────────────
    def test_missing_snapshot_starts_empty_without_complaint(self):
        self.assertIn("no snapshot", main._load_mission_store())
        self.assertEqual(main.original_missions, {})
        self.assertEqual(main.active_original_by_vehicle, {})

    def _refuses(self, payload, *, raw=None):
        """Write a bad snapshot, load it, and assert nothing at all was adopted."""
        self._seed(a_record("msn-should-not-survive"))
        main.MISSION_STORE_DIR.mkdir(parents=True, exist_ok=True)
        with open(main.MISSION_STORE_PATH, "w", encoding="utf-8") as fh:
            fh.write(raw if raw is not None else json.dumps(payload))
        status = main._load_mission_store()
        self.assertTrue(status.startswith("refused"), status)
        self.assertEqual(main.original_missions, {})          # nothing partially loaded
        self.assertEqual(main.active_original_by_vehicle, {})
        self.assertTrue(main.MISSION_STORE_PATH.exists())     # left on disk for inspection

    def test_truncated_json_is_refused(self):
        self._refuses(None, raw='{"version": 1, "original_missions": {"msn-a"')

    def test_wrong_version_is_refused(self):
        self._refuses({"version": 99, "original_missions": {}, "active_original_by_vehicle": {}})

    def test_altered_route_hash_is_refused(self):
        rec = a_record("msn-altered")
        rec["route_hash"] = "sha256:not-the-hash-of-these-waypoints"
        self._refuses({"version": 1, "original_missions": {"msn-altered": rec},
                       "active_original_by_vehicle": {"2": "msn-altered"}})

    def test_a_single_bad_record_rejects_the_whole_file(self):
        # Half a mission store is worse than none: it would present a mission whose geometry
        # may not be what the vehicle is carrying.
        good, bad = a_record("msn-good"), a_record("msn-bad")
        bad["route_waypoints"] = []
        self._refuses({"version": 1,
                       "original_missions": {"msn-good": good, "msn-bad": bad},
                       "active_original_by_vehicle": {"2": "msn-good"}})

    def test_dangling_active_pointer_is_refused(self):
        self._refuses({"version": 1, "original_missions": {},
                       "active_original_by_vehicle": {"2": "msn-nowhere"}})

    def test_active_pointer_to_another_vehicles_mission_is_refused(self):
        rec = a_record("msn-owned-by-2", vid=2)
        self._refuses({"version": 1, "original_missions": {"msn-owned-by-2": rec},
                       "active_original_by_vehicle": {"3": "msn-owned-by-2"}})

    def test_unknown_upload_status_is_refused(self):
        rec = a_record("msn-weird-status")
        rec["upload_status"] = "PROBABLY_FINE"
        self._refuses({"version": 1, "original_missions": {"msn-weird-status": rec},
                       "active_original_by_vehicle": {}})

    def test_mismatched_mission_id_key_is_refused(self):
        rec = a_record("msn-claims-other")
        self._refuses({"version": 1, "original_missions": {"msn-different-key": rec},
                       "active_original_by_vehicle": {}})

    # ── atomicity / write failure ─────────────────────────────────────────────────────
    def test_write_leaves_no_temp_file_behind(self):
        self._seed(a_record())
        main._save_mission_store()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["mission_store.json"])

    def test_an_unwritable_store_never_takes_down_the_in_memory_truth(self):
        rec = a_record()
        self._seed(rec)
        main.MISSION_STORE_DIR = self.tmp / "a-file-not-a-dir"
        main.MISSION_STORE_DIR.write_text("in the way", encoding="utf-8")
        main.MISSION_STORE_PATH = main.MISSION_STORE_DIR / "mission_store.json"
        self.assertFalse(main._save_mission_store())          # reported, not raised
        self.assertEqual(main.original_missions[rec["mission_id"]], rec)


if __name__ == "__main__":
    unittest.main()
