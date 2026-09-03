"""
Tests for experiment_recording_api.py -- the Local Agent HTTP operation layer
for /agent/experiment_recording/*.

    python3 test_experiment_recording_api.py

Covers: no-controller-yet degrade, config GET/PATCH round-trip (descriptive
only), status passthrough, runs listing/detail, and the annotation endpoint
being fire-and-forget (task section 4/27 -- never affects vehicle state).
"""
import shutil
import tempfile
import unittest

import experiment_record_config as erc
import experiment_recorder as er
import experiment_recording_api as api
import experiment_recording_runtime as runtime


class ExperimentRecordingApiTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="exprec_api_")
        runtime.register(None)

    def tearDown(self):
        rec = runtime.get_recorder()
        if rec is not None:
            rec.shutdown(timeout=2.0)
        runtime.register(None)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_recorder(self, **overrides):
        cfg = erc.ExperimentRecordConfig(
            experiment_recording_enabled=True, experiment_record_directory=self.tmpdir,
            writer_poll_interval_s=0.05, flush_interval_s=0.1, **overrides,
        )
        rec = er.ExperimentRecorder(cfg=cfg, vehicle_id="usv-test")
        runtime.register(rec)
        return rec

    def test_status_before_recorder_registered(self):
        code, body = api.get_status()
        self.assertEqual(code, 200)
        self.assertFalse(body["enabled"])
        self.assertEqual(body["error"]["code"], "RECORDER_NOT_READY")

    def test_config_get_patch_round_trip_is_descriptive_only(self):
        self._make_recorder()
        code, body = api.get_config()
        self.assertEqual(code, 200)
        self.assertEqual(body["next_run"], {})

        code, body = api.patch_config({
            "experiment_id": "E3-degraded-comms", "experiment_type": "DEGRADED_COMMUNICATION",
            "trial_number": 2, "scenario": {"description": "40 percent packet loss"},
        })
        self.assertEqual(code, 200)
        self.assertTrue(body["accepted"])
        self.assertEqual(body["next_run"]["experiment_id"], "E3-degraded-comms")

        code, body = api.get_config()
        self.assertEqual(body["next_run"]["trial_number"], 2)

    def test_patch_config_rejects_non_object_body(self):
        self._make_recorder()
        code, body = api.patch_config(["not", "an", "object"])
        self.assertEqual(code, 400)
        self.assertFalse(body["accepted"])

    def test_status_reflects_active_run(self):
        rec = self._make_recorder()
        rec.start_run(mission_id="m1", original_route_hash="sha256:abc")
        code, body = api.get_status()
        self.assertEqual(code, 200)
        self.assertTrue(body["enabled"])
        # start_run() is fire-and-forget -- 'active' flips synchronously
        # (task section 5's "allocate unique run/session" step), independent
        # of whether the writer thread has created the directory yet.
        self.assertTrue(body["active"])

    def test_runs_listing_and_detail(self):
        rec = self._make_recorder()
        run_id = rec.start_run(mission_id="m1", original_route_hash="sha256:abc")

        def _has_manifest():
            return any(r["run_id"] == run_id for r in api.get_runs()[1]["runs"])

        import time
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not _has_manifest():
            time.sleep(0.02)

        code, body = api.get_runs()
        self.assertEqual(code, 200)
        self.assertTrue(any(r["run_id"] == run_id for r in body["runs"]))

        code, body = api.get_run(run_id)
        self.assertEqual(code, 200)
        self.assertEqual(body["run_id"], run_id)

        code, body = api.get_run("run-does-not-exist")
        self.assertEqual(code, 404)

    def test_annotation_is_fire_and_forget_and_never_errors(self):
        self._make_recorder()
        code, body = api.post_annotation({
            "category": "operator_note", "name": "entered_open_water",
            "data": {"note": "Beginning impairment stage"},
        })
        self.assertEqual(code, 200)
        self.assertTrue(body["accepted"])

    def test_annotation_requires_category_and_name(self):
        self._make_recorder()
        code, body = api.post_annotation({"data": {}})
        self.assertEqual(code, 400)
        self.assertFalse(body["accepted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
