"""
Integration tests proving the experiment recorder's fail-open contract
(task section 0/35 C) holds through the REAL controllers, not just the
recorder's own unit tests: a recorder that raises on every call must never
stop Start, Stop, or a replan transaction from succeeding.

    python3 test_experiment_recorder_controller_integration.py

Reuses the existing fake-gateway test fixtures from
test_mission_execution_controller.py / test_replan_controller.py rather than
building a second copy of them.
"""
import json
import os
import shutil
import tempfile
import unittest

import experiment_record_config as erc
import experiment_recorder as er
import experiment_recording_runtime
import mission_execution_controller as mec
import replan_controller as rc
import test_experiment_recorder as ter
import test_mission_execution_controller as tmec
import test_replan_controller as trc


class RaisingRecorder:
    """A recorder double whose every method raises -- proves the controllers'
    own try/except wrapping (not the recorder's) is what keeps them safe."""

    def start_run(self, *a, **kw):
        raise RuntimeError("recorder.start_run boom")

    def finalize_async(self, *a, **kw):
        raise RuntimeError("recorder.finalize_async boom")

    def record_revision(self, *a, **kw):
        raise RuntimeError("recorder.record_revision boom")

    def record_event(self, *a, **kw):
        raise RuntimeError("recorder.record_event boom")


class MissionExecutionRecorderIsolationTests(tmec._Base):
    def test_start_succeeds_when_recorder_raises_on_every_call(self):
        ctrl = self._ctrl(recorder=RaisingRecorder())
        ctrl.observe(self._snapshot(mode="LOITER"), None)  # NOT_READY -> READY
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(res["verified_mode"], "AUTO")

    def test_stop_succeeds_when_recorder_raises_on_every_call(self):
        replan_hook = tmec._ReplanHook()
        ctrl = self._ctrl(recorder=RaisingRecorder(),
                          replan_status_fn=replan_hook.status,
                          replan_reset_fn=replan_hook.reset,
                          experiment_reset_fn=lambda: True)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        res = ctrl.stop()
        self.assertTrue(res["accepted"])
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

    def test_terminal_transition_finalizes_without_raising(self):
        """Directly exercises _transition(terminal=True) -- the finalize
        hook -- with a raising recorder attached, independent of any full
        Start/Stop sequence."""
        ctrl = self._ctrl(recorder=RaisingRecorder())
        try:
            ctrl._transition(mec.FAILED, "synthetic terminal for test", terminal=True)
        except Exception as e:  # pragma: no cover
            self.fail(f"_transition raised with a broken recorder attached: {e}")
        self.assertEqual(ctrl.status()["state"], mec.FAILED)


class StopFinalizationOrderTests(tmec._Base):
    """Task section 13: reproduces the two REAL bundles end to end through
    the actual MissionExecutionController (not a hand-built event stream) --
    Start -> RUNNING -> Stop -> verified LOITER hold -> restore/rewind ->
    reset -> authority OPERATOR -> STOP_COMPLETE, against a REAL
    ExperimentRecorder writing to a scratch directory. Proves the finalized
    summary is internally consistent: a terminal_reason that says
    "authority -> OPERATOR" can never coexist with final_authority ==
    LOCAL_AGENT."""

    def setUp(self):
        super().setUp()
        self.rec_dir = tempfile.mkdtemp(prefix="exprec_ctrl_")
        cfg = erc.ExperimentRecordConfig(
            experiment_recording_enabled=True, experiment_record_directory=self.rec_dir,
            experiment_record_telemetry_hz=2.0, experiment_record_queue_capacity=256,
            experiment_record_low_queue_capacity=256,
            writer_poll_interval_s=0.02, flush_interval_s=0.05,
        )
        self.rec = er.ExperimentRecorder(cfg=cfg, vehicle_id="usv-test")
        # The controller's OWN direct recorder calls (start_run/record_event/
        # finalize_async in mission_execution_controller.py) use self._recorder
        # (injected below); but every _transition() call ALSO goes through
        # transition_log.record_transition(), which looks the live recorder
        # up via experiment_recording_runtime's global registry -- the same
        # split real local_agent.py wires (mission_execution_controller.py's
        # `recorder=recorder` kwarg AND experiment_recording_runtime.register
        # (recorder), both pointed at the SAME instance). Registering here
        # reproduces that, so STOP_REQUESTED/STOP_HOLD_REQUESTED/
        # VERIFYING_RESET/... (task section 23's expected timeline) actually
        # reach this run's timeline.jsonl too.
        experiment_recording_runtime.register(self.rec)

    def tearDown(self):
        experiment_recording_runtime.register(None)
        self.rec.shutdown(timeout=2.0)
        shutil.rmtree(self.rec_dir, ignore_errors=True)
        super().tearDown()

    def _stop_ctrl(self):
        replan_hook = tmec._ReplanHook()
        return self._ctrl(recorder=self.rec, replan_status_fn=replan_hook.status,
                          replan_reset_fn=replan_hook.reset, experiment_reset_fn=lambda: True)

    def _run_id(self):
        run_id = self.rec.status().get("run_id") or self.rec.status().get("last_finalized_run_id")
        self.assertIsNotNone(run_id)
        return run_id

    def test_stop_complete_summary_never_contradicts_terminal_reason(self):
        ctrl = self._stop_ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)  # NOT_READY -> READY
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        run_id = self._run_id()

        self.gw.current_seq = 2  # mid-route, like the real bundles
        stop_res = ctrl.stop()
        self.assertTrue(stop_res["accepted"])
        self.assertEqual(stop_res["stop"]["authority_after"], "OPERATOR")
        self.assertIn(ctrl.status()["state"], (mec.READY, mec.NOT_READY))

        self.assertTrue(ter._wait_for(
            lambda: self.rec.status()["recorder_state"] in ("FINALIZED", "IDLE"), timeout=3.0))

        run_dir = os.path.join(self.rec_dir, run_id)
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)

        self.assertEqual(summary["run"]["result"], "STOP_COMPLETE")
        self.assertEqual(summary["vehicle"]["final_mode"], "LOITER")
        self.assertEqual(summary["vehicle"]["final_authority"], "OPERATOR")
        self.assertEqual(summary["stop"]["authority_after"], "OPERATOR")
        self.assertTrue(summary["stop"]["hold_verified"])
        self.assertTrue(summary["stop"]["ready_for_start"])

        # The exact real-bundle contradiction must be structurally impossible.
        reason = summary["run"]["terminal_reason"] or ""
        self.assertIn("authority -> OPERATOR", reason)
        self.assertNotEqual(summary["vehicle"]["final_authority"], "LOCAL_AGENT")

        timeline_path = os.path.join(run_dir, "timeline.jsonl")
        lines = ter._read_jsonl(timeline_path)
        types = [l["type"] for l in lines]
        # Every mission-execution _transition() call is forwarded as a
        # MISSION_EXECUTION_STATE_CHANGED event with the concrete from/to in
        # `data` (see transition_log._RECORDER_EVENT_TYPE) -- so the desired
        # Stop ordering (task section 23) is asserted against `to` values,
        # not the event `type` itself.
        mission_exec_to = [l["data"]["to"] for l in lines if l["type"] == "MISSION_EXECUTION_STATE_CHANGED"]
        self.assertIn("MISSION_START_REQUESTED", types)
        for expected_to in ("STOP_REQUESTED", "STOP_HOLD_REQUESTED",
                            "STOP_HOLD_CONFIRMED", "VERIFYING_RESET"):
            self.assertIn(expected_to, mission_exec_to,
                          f"transition to {expected_to!r} missing from timeline: {mission_exec_to}")
        self.assertIn("STOP_COMPLETE", types)
        # STOP_COMPLETE (the terminal evidence) must appear BEFORE finalization
        # -- proven indirectly here by its mere presence in the FINALIZED file
        # (finalization only happens after the writer drains everything
        # queued ahead of the FINALIZE marker -- see test_experiment_recorder's
        # own FIFO-ordering test for the direct proof) -- and strictly AFTER
        # VERIFYING_RESET, matching the desired ordering.
        verifying_reset_idx = max(i for i, l in enumerate(lines)
                                  if l["type"] == "MISSION_EXECUTION_STATE_CHANGED"
                                  and l["data"]["to"] == "VERIFYING_RESET")
        self.assertLess(verifying_reset_idx, types.index("STOP_COMPLETE"))


class ReplanRecorderIsolationTests(trc._Base):
    def test_replan_transaction_succeeds_when_recorder_raises_on_every_call(self):
        ctrl = self._ctrl(recorder=RaisingRecorder())
        result = ctrl.run_transaction(self._snapshot())
        # The fixture's default snapshot/config (battery 12%, critical
        # threshold) drives the SAME full success sequence
        # TestSuccess.test_full_fsm_sequence proves -- the key assertion here
        # is that a recorder raising on every call (start_run/record_revision/
        # record_event/finalize_async, all reached during this transaction)
        # never prevents that outcome.
        self.assertEqual(result["outcome"], rc.MONITORING_REVISED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
