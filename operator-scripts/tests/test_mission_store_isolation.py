"""The production mission store is UNREACHABLE from a test process.

Run from operator-scripts/:  python -m unittest tests.test_mission_store_isolation

WHY THIS FILE EXISTS
--------------------
During live bench testing `runtime_data/mission_store.json` was found holding exactly one
mission — `msn-restart`, a fixture id — with `active_original_by_vehicle["2"]` pointing at it,
while Scout and the Pixhawk were flying a different, real mission. The station then refused the
Agent package as "does not match approved mission", which was the CORRECT answer to a corrupted
question.

The cause was a test. `tests/test_mission_publish.py` isolated the store by monkeypatching
`main._save_mission_store` rather than the store PATH; one test restored the real writer and ran
a publish, so the real atomic write ran against the test's own cleared in-memory store and
replaced the production snapshot with a single seeded fixture.

It survived review because a FULL `unittest discover` run does not reproduce it: discovery
imports every module before running any test, and `tests/test_planning.py` redirects
`main.MISSION_STORE_PATH` at import time. So the production file was protected only by module
import ORDER — and running one module alone, exactly as the per-feature verification docs
instruct, removed that protection entirely.

Isolation that depends on import order is not isolation. main.py now resolves the runtime
directory once, in the module that owns the store, and refuses the production path whenever a
test runner is in the process (see `_resolve_runtime_dir`). These tests hold that guarantee:

  • no test process resolves the production store;
  • the real writer, the real record-status projection and a real publish transaction all leave
    the production file byte-for-byte unchanged;
  • the COMPLETE suite leaves it byte-for-byte unchanged (proved by running it, in a subprocess).
"""
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402

REPO_ROOT = pathlib.Path(main.__file__).resolve().parent
PRODUCTION_STORE = REPO_ROOT / "runtime_data" / "mission_store.json"

# Set in the child process so the full-suite proof below does not recurse into itself.
INNER_ENV = "OPERATOR_STORE_ISOLATION_INNER"


def production_fingerprint():
    """(sha256, size) of the production store, or None when it does not exist. `None` is a real
    answer and is compared as one: a test that CREATES the store where there was none is just as
    much a contamination as one that rewrites it."""
    if not PRODUCTION_STORE.exists():
        return None
    data = PRODUCTION_STORE.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


class ProductionStoreIsUnreachableTests(unittest.TestCase):
    """The structural guarantee: this process cannot even NAME the production store."""

    def test_the_resolved_store_is_not_the_production_store(self):
        self.assertFalse(main.is_production_mission_store(),
                         f"a test process resolved the PRODUCTION mission store "
                         f"({main.MISSION_STORE_PATH}) — every test write would land on the "
                         f"operator's approved missions")

    def test_the_resolved_store_is_outside_the_repository_runtime_dir(self):
        resolved = pathlib.Path(main.MISSION_STORE_PATH).resolve()
        self.assertNotEqual(resolved, PRODUCTION_STORE.resolve())
        self.assertFalse(str(resolved).startswith(str((REPO_ROOT / "runtime_data").resolve())),
                         f"{resolved} is inside the repository's runtime_data/")

    def test_a_test_runner_is_detected_from_the_process_not_from_a_flag(self):
        # The detection must not need a test to remember anything — a NEW test file inherits it.
        self.assertTrue(main._test_runner_in_process())

    def test_an_explicit_override_still_wins(self):
        # A deployment (or a test that wants a directory it chose itself) can still say where.
        with tempfile.TemporaryDirectory() as tmp:
            os.environ[main.RUNTIME_DIR_ENV] = tmp
            try:
                path, reason = main._resolve_runtime_dir()
            finally:
                os.environ.pop(main.RUNTIME_DIR_ENV, None)
        self.assertEqual(reason, "env")
        self.assertEqual(pathlib.Path(path), pathlib.Path(tmp))

    def test_without_a_test_runner_the_production_directory_is_used(self):
        # The interlock must not have quietly moved a REAL backend's store: with no override and
        # no test runner in the process, the resolution is still runtime_data/.
        real = main._test_runner_in_process
        main._test_runner_in_process = lambda: False
        try:
            os.environ.pop(main.RUNTIME_DIR_ENV, None)
            path, reason = main._resolve_runtime_dir()
        finally:
            main._test_runner_in_process = real
        self.assertEqual(reason, "production")
        self.assertEqual(pathlib.Path(path).resolve(), (REPO_ROOT / "runtime_data").resolve())


class RealWritesNeverTouchProductionTests(unittest.TestCase):
    """The behavioural guarantee: exercise the REAL persistence paths and prove the production
    file did not move. These deliberately do NOT stub `_save_mission_store` — a stub would prove
    nothing about the writer that caused the incident."""

    def setUp(self):
        self.before = production_fingerprint()
        self._missions = dict(main.original_missions)
        self._active = dict(main.active_original_by_vehicle)

    def tearDown(self):
        main.original_missions.clear()
        main.original_missions.update(self._missions)
        main.active_original_by_vehicle.clear()
        main.active_original_by_vehicle.update(self._active)
        self.assertEqual(production_fingerprint(), self.before,
                         "the production mission store changed during this test")

    def _seed(self):
        fixture = pathlib.Path(__file__).parent / "fixtures" / \
            "active-original-msn-329c2faff137.json"
        with open(fixture, encoding="utf-8") as fh:
            rec = json.load(fh)
        rec["mission_id"] = "msn-isolation-probe"
        rec["vehicle_id"] = 2
        rec["upload_status"] = "VERIFIED"
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        main.original_missions["msn-isolation-probe"] = rec
        main.active_original_by_vehicle[2] = "msn-isolation-probe"
        return rec

    def test_the_real_writer_writes_somewhere_that_is_not_production(self):
        self._seed()
        self.assertTrue(main._save_mission_store())
        # It really did write — this is not a no-op that trivially passes tearDown.
        self.assertTrue(pathlib.Path(main.MISSION_STORE_PATH).exists())
        with open(main.MISSION_STORE_PATH, encoding="utf-8") as fh:
            missions, active = main._validate_mission_store(json.load(fh))
        self.assertEqual(active[2], "msn-isolation-probe")
        self.assertIn("msn-isolation-probe", missions)

    def test_the_upload_status_projection_persists_without_touching_production(self):
        # _sync_mission_record_status calls _save_mission_store on every status change — the
        # other real path into the writer.
        rec = self._seed()
        rec["upload_status"] = "QUEUED"
        main.mission_id_by_command["cmd-isolation"] = "msn-isolation-probe"
        try:
            main._sync_mission_record_status({
                "id": "cmd-isolation", "type": "MISSION_UPLOAD",
                "status": "EXECUTED", "mission_result": "verified"})
        finally:
            main.mission_id_by_command.pop("cmd-isolation", None)
        self.assertEqual(rec["upload_status"], "VERIFIED")

    def test_a_full_publish_transaction_leaves_production_untouched(self):
        # The exact shape of the incident: a publish that gets past the Pixhawk proof, fails at
        # Scout, reaches _mark_sync_required and calls the REAL persist hook. Both transports are
        # faked so the path is deterministic; the WRITER is not.
        import requests as real_requests
        import scout_replan
        from fastapi.testclient import TestClient

        rec = self._seed()
        route_hash, count = rec["route_hash"], len(rec["route_waypoints"])

        class Readback:
            RequestException = real_requests.RequestException

            def get(self, url, **kw):
                class R:
                    status_code = 200
                    content = b"1"

                    def json(self):
                        return {"waypoints": [{"seq": i} for i in range(count + 1)],
                                "count": count + 1, "partial": False,
                                "pixhawk_item_count": count + 1,
                                "route_waypoint_count": count,
                                "route_content_hash": route_hash}

                    def raise_for_status(self):
                        pass
                return R()

        class ScoutDown:
            RequestException = real_requests.RequestException

            def get(self, url, **kw):
                raise real_requests.ConnectionError("no route to host")

            def request(self, method, url, **kw):
                raise real_requests.ConnectionError("no route to host")

        real_main_req, real_scout_req = main.requests, scout_replan.requests
        main.requests, scout_replan.requests = Readback(), ScoutDown()
        main._pixhawk_readback_cache.clear()
        try:
            r = TestClient(main.app).post("/api/vehicles/2/missions/publish", json={})
        finally:
            main.requests, scout_replan.requests = real_main_req, real_scout_req
            main._pixhawk_readback_cache.clear()

        env = r.json()
        self.assertEqual(env["state"], "SCOUT_UNREACHABLE")
        # PROOF the real writer ran on this path: the owed sync was recorded and persisted.
        self.assertEqual(
            main.original_missions["msn-isolation-probe"]["package_sync_state"], "REQUIRED")


class TheWholeSuiteLeavesProductionUnchangedTests(unittest.TestCase):
    """The end-to-end guarantee the incident actually needs: run the COMPLETE suite and compare
    the production store byte-for-byte. Runs in a subprocess so the child is a genuine, separate
    `python -m unittest` process resolving its own store; `INNER_ENV` stops it recursing."""

    @unittest.skipIf(os.environ.get(INNER_ENV) == "1",
                     "inner run of the suite — the outer process owns this proof")
    def test_running_the_complete_suite_does_not_change_the_production_store(self):
        before = production_fingerprint()
        env = dict(os.environ)
        env[INNER_ENV] = "1"
        # Deliberately NOT setting OPERATOR_RUNTIME_DIR: the child must protect itself through
        # main.py's own resolution, which is the thing under test.
        env.pop(main.RUNTIME_DIR_ENV, None)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=900)
        after = production_fingerprint()
        self.assertEqual(after, before,
                         "the complete test suite changed runtime_data/mission_store.json\n"
                         f"before={before} after={after}")
        self.assertEqual(proc.returncode, 0,
                         f"the inner suite failed:\n{proc.stderr[-4000:]}")

    @unittest.skipIf(os.environ.get(INNER_ENV) == "1", "inner run — see above")
    def test_running_a_single_module_alone_does_not_change_the_production_store(self):
        # The case that actually bit: `python -m unittest tests.test_mission_publish` on its own,
        # with none of the modules whose import-time redirect used to mask the defect.
        before = production_fingerprint()
        env = dict(os.environ)
        env[INNER_ENV] = "1"
        env.pop(main.RUNTIME_DIR_ENV, None)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_mission_publish"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(production_fingerprint(), before,
                         "running tests.test_mission_publish alone changed the production store")
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])


if __name__ == "__main__":
    unittest.main()
