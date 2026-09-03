"""
Experiment Recorder integration for mission-energy feasibility (task section
10/16): proves the feasibility evidence local_agent.py adds to every decision
snapshot round-trips through the REAL ExperimentRecorder, and that a Start
rejected for INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION leaves enough evidence in
a run's timeline (MISSION_FEASIBILITY_CHECK, MISSION_START_REJECTED) to
reconstruct physical battery / injected battery / estimated requirement /
reserve / the resulting negative margin / the rejection reason -- and that
none of this depends on the recorder for correctness (it stays fail-open).

    python3 test_mission_feasibility_recorder.py

Reuses the existing recorder test fixtures (test_experiment_recorder.py) and
the existing mission-execution controller fixtures
(test_mission_execution_controller.py) rather than building new ones.
"""
import json
import os
import unittest

import experiment_injection
import experiment_recorder as er
import mission_execution_controller as mec
import mission_feasibility as mf
import test_experiment_recorder as ter
import test_mission_execution_controller as tmec


class DecisionSnapshotFeasibilityTests(ter.RecorderTestCase):
    """Direct recorder-level proof (no controller involved) that a decision
    snapshot carrying the `feasibility` sub-dict local_agent.py now adds is
    preserved verbatim -- the recorder is schema-agnostic evidence storage,
    never a second copy of feasibility's own logic."""

    def test_decision_snapshot_contains_full_feasibility_evidence(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_decision({
            "feasibility": {
                "status": mf.STATUS_INFEASIBLE,
                "reason": mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION,
                "mission_feasible": False,
                "rtl_return_feasible": True,
                "planned_completion_distance_m": 900.0,
                "rtl_return_distance_m": 50.0,
                "estimated_mission_energy_percent": 30.0,
                "estimated_rtl_return_energy_percent": 1.7,
                "mission_margin_percent": -7.0,
                "rtl_return_margin_percent": 14.0,
                "reserve_margin_percent": 10.0,
                "battery_percent": 5.0,
                "battery_source": mf.SOURCE_INJECTED,
                "physical_battery_percent": 94.0,
                "injected_battery_percent": 5.0,
                "planned_home": {"latitude": 56.6635397, "longitude": 12.8813428,
                                 "source": mf.HOME_SOURCE_PLANNING_PACKAGE},
                "rtl_home": {"latitude": 56.6490, "longitude": 12.8700,
                            "source": mf.HOME_SOURCE_PIXHAWK_VERIFIED_HOME},
                "mission_geometry_source": mf.MISSION_METHOD_REMAINING_ROUTE,
                "rtl_return_geometry_source": mf.RTL_METHOD_STRAIGHT_LINE,
            },
        })
        run_dir = os.path.join(self.tmpdir, run_id)
        path = os.path.join(run_dir, "decision_snapshots.jsonl")
        self.assertTrue(ter._wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        feas = lines[-1]["feasibility"]
        self.assertEqual(feas["status"], mf.STATUS_INFEASIBLE)
        self.assertFalse(feas["mission_feasible"])
        self.assertTrue(feas["rtl_return_feasible"])
        self.assertEqual(feas["planned_completion_distance_m"], 900.0)
        self.assertEqual(feas["rtl_return_distance_m"], 50.0)
        self.assertEqual(feas["mission_margin_percent"], -7.0)
        self.assertEqual(feas["rtl_return_margin_percent"], 14.0)
        # The example from the task write-up: mission infeasible (-7%), RTL
        # return still feasible (+14%) -- the distinction a SAFE_RETURN
        # trigger would later act on -- both present and distinct in the
        # SAME snapshot.
        self.assertNotEqual(feas["mission_margin_percent"] > 0, feas["rtl_return_margin_percent"] > 0)
        # The physical/injected/policy distinction survives inside the
        # feasibility evidence too, exactly like the existing top-level
        # battery block already proves for energy_policy (task section 19/2).
        self.assertEqual(feas["physical_battery_percent"], 94.0)
        self.assertEqual(feas["injected_battery_percent"], 5.0)
        self.assertEqual(feas["battery_percent"], 5.0)
        self.assertNotEqual(feas["physical_battery_percent"], feas["battery_percent"])
        # Both Home identities remain distinguishable in the recorded evidence
        # (task section 8) -- never blended into one generic "Home".
        self.assertEqual(feas["planned_home"]["source"], mf.HOME_SOURCE_PLANNING_PACKAGE)
        self.assertEqual(feas["rtl_home"]["source"], mf.HOME_SOURCE_PIXHAWK_VERIFIED_HOME)
        self.assertNotEqual(feas["planned_home"]["latitude"], feas["rtl_home"]["latitude"])


class StartRejectionRecorderTests(tmec._Base):
    """Through the REAL MissionExecutionController + a REAL ExperimentRecorder
    writing to a scratch directory -- proves a feasibility-rejected Start
    leaves reconstructable evidence in the run's timeline."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.rec_dir = tempfile.mkdtemp(prefix="exprec_feas_")
        import experiment_record_config as erc
        cfg = erc.ExperimentRecordConfig(
            experiment_recording_enabled=True, experiment_record_directory=self.rec_dir,
            experiment_record_telemetry_hz=2.0, experiment_record_queue_capacity=256,
            experiment_record_low_queue_capacity=256,
            writer_poll_interval_s=0.02, flush_interval_s=0.05,
        )
        self.rec = er.ExperimentRecorder(cfg=cfg, vehicle_id="usv-2")

    def tearDown(self):
        import shutil
        self.rec.shutdown(timeout=2.0)
        shutil.rmtree(self.rec_dir, ignore_errors=True)
        experiment_injection.clear()
        super().tearDown()

    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(recorder=self.rec, **kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl

    def test_rejected_start_leaves_full_feasibility_evidence_in_timeline(self):
        experiment_injection.inject(battery_percent=5.0, target_vehicle="usv-2")
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)

        # A rejected Start is a terminal FAILED transition, which finalizes
        # the recording session (async) -- wait for that to settle before
        # reading run_id (finalize_async clears _run_id synchronously, but
        # last_finalized_run_id is only set once the writer thread catches
        # up).
        self.assertTrue(ter._wait_for(
            lambda: self.rec.status()["recorder_state"] in ("FINALIZED", "IDLE")))
        run_id = self.rec.status().get("run_id") or self.rec.status().get("last_finalized_run_id")
        self.assertIsNotNone(run_id)
        run_dir = os.path.join(self.rec_dir, run_id)
        timeline_path = os.path.join(run_dir, "timeline.jsonl")
        self.assertTrue(ter._wait_for_type(timeline_path, "MISSION_START_REJECTED"))
        lines = ter._read_jsonl(timeline_path)
        types = [l["type"] for l in lines]
        self.assertIn("MISSION_FEASIBILITY_CHECK", types)
        self.assertIn("MISSION_START_REJECTED", types)

        rejected = next(l for l in lines if l["type"] == "MISSION_START_REJECTED")
        data = rejected["data"]
        self.assertEqual(data["reason"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        # Full reconstructable evidence: effective (injected) battery, the
        # estimated mission requirement, the reserve, and the resulting
        # negative margin -- everything needed to explain WHY, without
        # re-deriving it from telemetry.
        self.assertEqual(data["battery_percent"], 5.0)
        self.assertEqual(data["battery_source"], mf.SOURCE_INJECTED)
        self.assertIsNotNone(data["physical_battery_percent"])
        self.assertNotEqual(data["physical_battery_percent"], data["battery_percent"])
        self.assertIsNotNone(data["estimated_mission_capacity_Ah"])
        self.assertIsNotNone(data["mission_reserve_capacity_Ah"])
        self.assertIsNotNone(data["available_capacity_Ah"])
        self.assertLessEqual(data["mission_margin_percent"], 0)
        self.assertFalse(data["mission_feasible"])

        # No vehicle write happened either way (recorder evidence corroborates
        # the gateway-level proof in test_mission_execution_controller_
        # feasibility_gate.py).
        self.assertEqual(self.gw.write_calls, [])

    def test_recorder_failure_does_not_affect_start_rejection_behaviour(self):
        """A recorder that raises on every call must not change whether Start
        is rejected -- the feasibility gate's own control flow decides that,
        not the recorder (task section 10: 'Do not let recorder failure
        affect Start behavior')."""
        class RaisingRecorder:
            def start_run(self, *a, **kw):
                raise RuntimeError("boom")

            def record_event(self, *a, **kw):
                raise RuntimeError("boom")

        experiment_injection.inject(battery_percent=5.0, target_vehicle="usv-2")
        ctrl = self._ctrl(recorder=RaisingRecorder())
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        self.assertEqual(self.gw.write_calls, [])

    def test_healthy_battery_recorder_shows_feasible_check_and_normal_start(self):
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)

        run_id = self.rec.status().get("run_id") or self.rec.status().get("last_finalized_run_id")
        self.assertIsNotNone(run_id)
        run_dir = os.path.join(self.rec_dir, run_id)
        timeline_path = os.path.join(run_dir, "timeline.jsonl")
        self.assertTrue(ter._wait_for_type(timeline_path, "MISSION_FEASIBILITY_CHECK"))
        lines = ter._read_jsonl(timeline_path)
        types = [l["type"] for l in lines]
        self.assertIn("MISSION_FEASIBILITY_CHECK", types)
        self.assertNotIn("MISSION_START_REJECTED", types)
        check = next(l for l in lines if l["type"] == "MISSION_FEASIBILITY_CHECK")
        self.assertTrue(check["data"]["mission_feasible"])


if __name__ == "__main__":
    unittest.main()
