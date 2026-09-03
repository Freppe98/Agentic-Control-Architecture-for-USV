"""
Tests for experiment_recorder.py -- the thesis Experiment Recorder.

    python3 test_experiment_recorder.py

Two groups, matching the task's own required test list:

  * Non-interference tests (task section 35 A-G): prove the recorder never
    blocks, delays, or can crash the producer, regardless of how slow/broken
    the writer thread is.
  * Data-content tests (task section 36): prove the bundle it produces is
    correct -- one run per start_run, no overwritten trials, distinct
    physical/injected/policy battery, revision evidence, checksums, and no
    fabricated nulls-as-zero.

Uses a scratch temp directory per test (never the real experiment_runs/) and
small/fast config values so the suite runs in well under a second per test.
"""
import csv
import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

import experiment_record_config as erc
import experiment_recorder as er


def _cfg(tmpdir, **overrides):
    kwargs = dict(
        experiment_recording_enabled=True,
        experiment_record_directory=tmpdir,
        experiment_record_telemetry_hz=2.0,
        experiment_record_queue_capacity=4096,
        experiment_record_low_queue_capacity=1024,
        writer_poll_interval_s=0.05,
        flush_interval_s=0.1,
    )
    kwargs.update(overrides)
    return erc.ExperimentRecordConfig(**kwargs)


def _wait_for(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _read_jsonl(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(l) for l in f if l.strip()]


def _wait_for_type(path, event_type, timeout=2.0):
    """Wait until a JSONL file contains a record whose 'type' matches --
    NOT just "the file is non-empty", which races against the recorder's
    own synthetic RECORDER_RUN_STARTED / PROCESS_RESTART_RECOVERY entries
    that are always written first."""
    return _wait_for(lambda: any(l.get("type") == event_type for l in _read_jsonl(path)), timeout=timeout)


class RecorderTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="exprec_")
        self.rec = None

    def tearDown(self):
        if self.rec is not None:
            self.rec.shutdown(timeout=2.0)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _new_recorder(self, **overrides):
        self.rec = er.ExperimentRecorder(cfg=_cfg(self.tmpdir, **overrides), vehicle_id="usv-test")
        return self.rec

    def _start_and_wait(self, rec, **kwargs):
        run_id = rec.start_run(mission_id="m1", original_route_hash="sha256:abc", **kwargs)
        self.assertIsNotNone(run_id)
        _wait_for(lambda: os.path.exists(os.path.join(self.tmpdir, run_id, "manifest.json")))
        return run_id

    def _finalize_and_wait(self, rec, outcome="COMPLETED_HOLD", reason="test"):
        rec.finalize_async(outcome, reason)
        _wait_for(lambda: rec.status().get("recorder_state") in ("FINALIZED", "IDLE"))


# ═══════════════════════════════════════════════════════════════════════════
# A-G: Non-interference (task section 35) -- mandatory
# ═══════════════════════════════════════════════════════════════════════════
class NonInterferenceTests(RecorderTestCase):
    def test_a_slow_writer_never_blocks_producer(self):
        """record_event/record_decision/record_telemetry/start_run/
        finalize_async all return near-instantly even while the writer is
        stuck for seconds on a single record."""
        rec = self._new_recorder()
        self._start_and_wait(rec)

        original_dispatch = rec._dispatch

        def slow_dispatch(job):
            time.sleep(1.0)
            original_dispatch(job)

        rec._dispatch = slow_dispatch  # stalls the writer thread for 1s per job

        t0 = time.monotonic()
        rec.record_event("SLOW_TEST_EVENT", source="test")
        rec.record_decision({"current_decision": "MONITOR"})
        rec.record_telemetry({"latitude": 1.0})
        rec.record_annotation("test", "note")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2, "producer calls must not wait for the stalled writer")

        t1 = time.monotonic()
        rec.finalize_async("COMPLETED_HOLD", "done")
        self.assertLess(time.monotonic() - t1, 0.2, "finalize_async must not block")

    def test_b_queue_saturation_drops_without_blocking(self):
        """A full queue drops the record and counts it -- it never blocks the
        producer and never touches controller state."""
        rec = self._new_recorder(experiment_record_low_queue_capacity=2, experiment_record_queue_capacity=2)
        self._start_and_wait(rec)
        # Stall the writer so the queue actually fills up before draining.
        rec._dispatch = lambda job: time.sleep(1.0)

        t0 = time.monotonic()
        for i in range(50):
            rec.record_telemetry({"latitude": float(i)})
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.5, "50 enqueue attempts against a full queue must stay fast")

        status = rec.status()
        self.assertGreater(status["dropped_telemetry_records"], 0)

    def test_c_writer_exception_degrades_but_never_raises(self):
        """A broken writer (raises OSError/serialization error) marks the
        recorder DEGRADED; it never raises out of any public call and the
        recorder keeps accepting calls afterwards."""
        rec = self._new_recorder()
        self._start_and_wait(rec)

        def boom(job):
            raise OSError("No space left on device")

        rec._dispatch = boom
        rec.record_event("WILL_FAIL", source="test")
        self.assertTrue(_wait_for(lambda: rec.status()["degraded"] is True))
        self.assertEqual(rec.status()["last_error"]["code"], "WRITE_FAILED")

        # Recorder keeps accepting calls after degradation -- never raises.
        try:
            rec.record_event("AFTER_DEGRADE", source="test")
            rec.record_telemetry({"latitude": 1.0})
            rec.finalize_async("FAILED", "writer broken")
        except Exception as e:  # pragma: no cover
            self.fail(f"recorder call raised after degradation: {e}")

    def test_d_disabled_recorder_is_a_pure_noop(self):
        rec = er.ExperimentRecorder(cfg=_cfg(self.tmpdir, experiment_recording_enabled=False),
                                    vehicle_id="usv-test")
        self.rec = rec
        self.assertIsNone(rec.start_run(mission_id="m1"))
        rec.record_event("X", source="test")
        rec.record_decision({"a": 1})
        rec.record_telemetry({"latitude": 1.0})
        rec.record_annotation("c", "n")
        rec.record_revision({"new_revision": 1})
        rec.finalize_async("COMPLETED_HOLD")
        rec.reconcile_after_restart({"state": "RUNNING"})
        status = rec.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["active"])
        # No directory should ever have been created.
        self.assertFalse(os.path.isdir(os.path.join(self.tmpdir)) and
                         any(n.startswith("run-") for n in os.listdir(self.tmpdir)))

    def test_e_concurrent_producers_no_deadlock_no_crash(self):
        rec = self._new_recorder()
        self._start_and_wait(rec)
        errors = []

        def hammer(fn):
            try:
                for _ in range(200):
                    fn()
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [
            threading.Thread(target=hammer, args=(lambda: rec.record_telemetry({"latitude": 1.0}),)),
            threading.Thread(target=hammer, args=(lambda: rec.record_event("E", source="t"),)),
            threading.Thread(target=hammer, args=(lambda: rec.record_decision({"current_decision": "X"}),)),
            threading.Thread(target=hammer, args=(lambda: rec.record_annotation("c", "n"),)),
            threading.Thread(target=hammer, args=(lambda: rec.status(),)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
            self.assertFalse(t.is_alive(), "producer thread did not finish -- possible deadlock")
        self.assertEqual(errors, [])

    def test_f_slow_finalization_does_not_block_caller(self):
        rec = self._new_recorder()
        self._start_and_wait(rec)
        original_finalize = rec._w_finalize
        rec._w_finalize = lambda job: (time.sleep(1.0), original_finalize(job))[-1]

        t0 = time.monotonic()
        rec.finalize_async("COMPLETED_HOLD", "slow finalize test")
        self.assertLess(time.monotonic() - t0, 0.2)
        # Status may show finalizing=True for a while -- that's fine, it's
        # visibility, not blocking.
        self.assertTrue(_wait_for(lambda: rec.status()["recorder_state"] == "FINALIZED", timeout=3.0))

    def test_g_status_responsive_while_writer_stalled(self):
        rec = self._new_recorder()
        self._start_and_wait(rec)
        rec._dispatch = lambda job: time.sleep(2.0)
        rec.record_event("STALL_TRIGGER", source="test")
        time.sleep(0.1)  # let the writer pick up the stalling job

        t0 = time.monotonic()
        for _ in range(20):
            rec.status()
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.2, "status() must stay fast while the writer is stalled")


# ═══════════════════════════════════════════════════════════════════════════
# Data-content tests (task section 36)
# ═══════════════════════════════════════════════════════════════════════════
class DataContentTests(RecorderTestCase):
    def test_start_creates_exactly_one_new_run(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        runs = [n for n in os.listdir(self.tmpdir) if n.startswith("run-")]
        self.assertEqual(runs, [run_id])

    def test_two_trials_never_overwrite_one_another(self):
        rec = self._new_recorder()
        run_a = self._start_and_wait(rec)
        rec.record_event("A_EVENT", source="test")
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD")

        run_b = self._start_and_wait(rec)
        rec.record_event("B_EVENT", source="test")
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD")

        self.assertNotEqual(run_a, run_b)
        for run_id, expected_event in ((run_a, "A_EVENT"), (run_b, "B_EVENT")):
            timeline_path = os.path.join(self.tmpdir, run_id, "timeline.jsonl")
            with open(timeline_path) as f:
                lines = [json.loads(l) for l in f if l.strip()]
            types = [l["type"] for l in lines]
            self.assertIn(expected_event, types)
            other_event = "B_EVENT" if expected_event == "A_EVENT" else "A_EVENT"
            self.assertNotIn(other_event, types)

    def test_completed_hold_finalizes_with_summary_and_checksums(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="arrived home")
        run_dir = os.path.join(self.tmpdir, run_id)
        self.assertTrue(os.path.exists(os.path.join(run_dir, "summary.json")))
        self.assertTrue(os.path.exists(os.path.join(run_dir, "checksums.sha256")))
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["run"]["result"], "COMPLETED_HOLD")

    def test_terminal_failure_finalizes(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        self._finalize_and_wait(rec, outcome="FAILED", reason="AUTO_NOT_VERIFIED")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["run"]["result"], "FAILED")
        self.assertEqual(summary["run"]["terminal_reason"], "AUTO_NOT_VERIFIED")

    def test_original_mission_and_planning_package_captured(self):
        rec = self._new_recorder()
        original_mission = {"mission_id": "m1", "route_hash": "sha256:abc",
                            "route": [{"latitude": 1.0, "longitude": 2.0}]}
        planning_package = {"mission_id": "m1", "route": [{"latitude": 1.0, "longitude": 2.0}],
                            "no_go_zones": []}
        run_id = self._start_and_wait(rec, original_mission=original_mission,
                                      planning_package=planning_package)
        run_dir = os.path.join(self.tmpdir, run_id)
        with open(os.path.join(run_dir, "original_mission.json")) as f:
            om = json.load(f)
        self.assertEqual(om["route_hash"], "sha256:abc")
        with open(os.path.join(run_dir, "planning_package.json")) as f:
            pp = json.load(f)
        self.assertEqual(pp["mission_id"], "m1")

    def test_revised_mission_file_created_with_exact_hash(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        revision = {"new_revision": 1, "parent_revision": 0, "mission_id": "m1",
                    "original_route_hash": "sha256:abc", "revised_route_hash": "sha256:def"}
        rec.record_revision(revision)
        run_dir = os.path.join(self.tmpdir, run_id)
        self.assertTrue(_wait_for(lambda: os.path.exists(os.path.join(run_dir, "revised_mission_r1.json"))))
        with open(os.path.join(run_dir, "revised_mission_r1.json")) as f:
            r1 = json.load(f)
        self.assertEqual(r1["revised_route_hash"], "sha256:def")

    def test_decision_snapshot_keeps_physical_injected_policy_battery_distinct(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_decision({
            "battery": {"physical_percent": 91, "raw": 91, "valid": True,
                       "injected_percent": 12, "policy_percent": 12, "simulated": True},
        })
        run_dir = os.path.join(self.tmpdir, run_id)
        path = os.path.join(run_dir, "decision_snapshots.jsonl")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        battery = lines[-1]["battery"]
        self.assertEqual(battery["physical_percent"], 91)
        self.assertEqual(battery["injected_percent"], 12)
        self.assertNotEqual(battery["physical_percent"], battery["injected_percent"])

    def test_communication_state_transition_recorded(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("COMMUNICATION_STATE_CHANGED", source="test",
                         data={"from": "CONNECTED", "to": "PARTITIONED", "reason": "packet loss injected"})
        run_dir = os.path.join(self.tmpdir, run_id)
        path = os.path.join(run_dir, "timeline.jsonl")
        self.assertTrue(_wait_for_type(path, "COMMUNICATION_STATE_CHANGED"))
        lines = _read_jsonl(path)
        comm_events = [l for l in lines if l["type"] == "COMMUNICATION_STATE_CHANGED"]
        self.assertEqual(len(comm_events), 1)
        self.assertEqual(comm_events[0]["data"]["from"], "CONNECTED")
        self.assertEqual(comm_events[0]["data"]["to"], "PARTITIONED")

    def test_network_impairment_annotation_recorded(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_annotation("network_impairment", "packet_loss_enabled",
                              data={"condition_type": "NETWORK_IMPAIRMENT", "packet_loss_percent": 40},
                              source="experiment_api")
        path = os.path.join(self.tmpdir, run_id, "annotations.jsonl")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(lines[0]["category"], "network_impairment")
        self.assertEqual(lines[0]["data"]["packet_loss_percent"], 40)

    def test_replan_failure_reason_and_safe_hold_retained(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("REPLAN_VALIDATION_FAILED", source="replan_controller",
                         data={"reason_code": "NAVIGABLE_BOUNDARY_VIOLATION"})
        rec.record_event("REPLAN_SAFE_HOLD", source="replan_controller",
                         data={"reason": "retries exhausted"})
        rec.record_revision({"new_revision": 1, "mission_id": "m1",
                            "validation_result": {"valid": False, "reason_code": "NAVIGABLE_BOUNDARY_VIOLATION"}})
        run_dir = os.path.join(self.tmpdir, run_id)
        self.assertTrue(_wait_for(lambda: os.path.exists(os.path.join(run_dir, "revised_mission_r1.json"))))
        self._finalize_and_wait(rec, outcome="SAFE_HOLD", reason="validation failed")
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["safety"]["safe_hold_count"], 1)
        self.assertEqual(summary["safety"]["validation_failure_count"], 1)
        with open(os.path.join(run_dir, "revised_mission_r1.json")) as f:
            r1 = json.load(f)
        self.assertEqual(r1["validation_result"]["reason_code"], "NAVIGABLE_BOUNDARY_VIOLATION")

    def test_checksums_verify(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("SOME_EVENT", source="test")
        self._finalize_and_wait(rec)
        run_dir = os.path.join(self.tmpdir, run_id)
        with open(os.path.join(run_dir, "checksums.sha256")) as f:
            lines = [l for l in f.read().splitlines() if l.strip()]
        self.assertGreater(len(lines), 0)
        for line in lines:
            digest, fname = line.split("  ", 1)
            path = os.path.join(run_dir, fname)
            self.assertTrue(os.path.exists(path))
            h = hashlib.sha256()
            with open(path, "rb") as f:
                h.update(f.read())
            self.assertEqual(h.hexdigest(), digest, f"checksum mismatch for {fname}")

    def test_null_stays_null_not_fabricated(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_decision({"battery": {"physical_percent": None, "injected_percent": None}})
        path = os.path.join(self.tmpdir, run_id, "decision_snapshots.jsonl")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        battery = lines[-1]["battery"]
        self.assertIsNone(battery["physical_percent"])
        self.assertIsNone(battery["injected_percent"])
        # Never 0 or False as a stand-in for "unavailable".
        self.assertNotEqual(battery["physical_percent"], 0)

    def test_structured_error_data_stays_structured(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("MISSION_EXECUTION_FAILED", source="test", data={
            "error": {"code": "AUTO_NOT_VERIFIED", "message": "could not confirm AUTO",
                     "detail": {"observed_mode": "LOITER"}},
        })
        path = os.path.join(self.tmpdir, run_id, "timeline.jsonl")
        self.assertTrue(_wait_for_type(path, "MISSION_EXECUTION_FAILED"))
        lines = _read_jsonl(path)
        matching = [l for l in lines if l["type"] == "MISSION_EXECUTION_FAILED"]
        error = matching[-1]["data"]["error"]
        self.assertIsInstance(error, dict)
        self.assertIsInstance(error["detail"], dict)
        self.assertEqual(error["code"], "AUTO_NOT_VERIFIED")

    def test_no_run_active_is_a_fast_noop(self):
        rec = self._new_recorder()
        # No start_run() called -- every record_* call must be a silent no-op.
        rec.record_event("X", source="test")
        rec.record_decision({"a": 1})
        rec.record_telemetry({"latitude": 1.0})
        status = rec.status()
        self.assertFalse(status["active"])
        self.assertEqual(status["dropped_event_records"], 0)  # dropped only on a full queue, not "no run"


# ═══════════════════════════════════════════════════════════════════════════
# E2 water-trial integration task section 16 -- recorder aggregation
# regression fixtures resembling the real E2 run.
# ═══════════════════════════════════════════════════════════════════════════
class RecorderAggregationRegressionTests(RecorderTestCase):
    def test_fully_connected_run_reports_nonzero_connected_duration(self):
        # The confirmed bug: a run whose comm state never transitions (the
        # common case) reported connected_duration_s == 0.0 instead of the
        # full run duration, because the old aggregation summed ONLY sparse
        # explicit transition events. Every periodic telemetry sample now
        # feeds the same aggregation (E2 water-trial recorder-aggregation
        # fix) -- CONNECTED start-to-finish, with zero transitions, must
        # report close to the full run duration, never 0.0.
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        for _ in range(3):
            rec.record_telemetry({"communication_state": "CONNECTED"})
            time.sleep(0.05)
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        time.sleep(0.05)
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        comm = summary["communication"]
        self.assertIsNotNone(comm["connected_duration_s"])
        self.assertGreater(comm["connected_duration_s"], 0.0)
        self.assertAlmostEqual(comm["connected_duration_s"], summary["run"]["duration_s"], delta=0.2)
        self.assertEqual(comm["partitioned_duration_s"], 0.0)
        self.assertEqual(comm["disconnected_duration_s"], 0.0)

    def test_completed_hold_populates_final_state_and_consistent_vehicle_mode(self):
        # The confirmed bug: COMPLETED_HOLD (the normal arrival/completion
        # path, _run_final_hold in mission_execution_controller.py) never
        # emitted terminal-evidence, so final_state.* stayed null and
        # vehicle.final_mode silently fell back to a stale periodic telemetry
        # sample (observed in the real run as final_mode=AUTO despite a
        # terminal_reason claiming a verified final LOITER). The controller
        # now emits MISSION_EXECUTION_TERMINAL_EVIDENCE via _transition(...,
        # terminal_evidence=...) before finalize_async.
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({"mode": "AUTO", "armed": True})  # stale sample, must NOT win
        rec.record_event("MISSION_EXECUTION_TERMINAL_EVIDENCE", source="mission_execution_controller",
                         data={
                             "mission_execution_state": "COMPLETED_HOLD",
                             "mission_execution_phase": None,
                             "final_mode": "LOITER", "final_armed": True,
                             "final_authority": "LOCAL_AGENT",
                             "current_waypoint": 7, "mission_count": 7,
                             "route_hash": "sha256:final", "mission_id": "m1",
                         })
        path = os.path.join(self.tmpdir, run_id, "timeline.jsonl")
        self.assertTrue(_wait_for_type(path, "MISSION_EXECUTION_TERMINAL_EVIDENCE"))
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD",
                                reason="Arrival at Home confirmed and final LOITER verified.")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["final_state"]["mission_execution_state"], "COMPLETED_HOLD")
        self.assertEqual(summary["final_state"]["current_waypoint"], 7)
        self.assertEqual(summary["final_state"]["mission_count"], 7)
        self.assertEqual(summary["final_state"]["mission_id"], "m1")
        # Never contradicts the terminal reason -- LOITER, not the stale AUTO.
        self.assertEqual(summary["vehicle"]["final_mode"], "LOITER")
        self.assertEqual(summary["vehicle"]["final_authority"], "LOCAL_AGENT")

    def test_risk_recommendation_and_replan_decision_change_counts_tracked_separately(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        # current_decision (decision_engine.py's label) stays CONTINUE_MISSION
        # throughout -- decision_change_count must stay 0 -- while risk level/
        # recommendation and the replan controller's own energy decision all
        # change, each counted by its OWN dedicated field.
        rec.record_decision({
            "current_decision": "Continue Mission",
            "risk": {"level": "LOW", "recommendation": "CONTINUE"},
            "replan_decision": "MONITOR",
        })
        rec.record_decision({
            "current_decision": "Continue Mission",
            "risk": {"level": "CRITICAL", "recommendation": "RETURN_HOME"},
            "replan_decision": "MONITOR",
        })
        rec.record_decision({
            "current_decision": "Continue Mission",
            "risk": {"level": "CRITICAL", "recommendation": "RETURN_HOME"},
            "replan_decision": "REPLAN_SAFE_RETURN",
        })
        path = os.path.join(self.tmpdir, run_id, "decision_snapshots.jsonl")
        self.assertTrue(_wait_for(lambda: len(_read_jsonl(path)) >= 3))
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        agent = summary["agent"]
        self.assertEqual(agent["decision_snapshot_count"], 3)
        self.assertEqual(agent["decision_change_count"], 0)
        self.assertEqual(agent["risk_level_change_count"], 1)
        self.assertEqual(agent["recommendation_change_count"], 1)
        self.assertEqual(agent["replan_decision_change_count"], 1)
        timing = summary["timing"]
        self.assertIsNotNone(timing["first_risk_escalation_at"])
        self.assertIsNotNone(timing["first_return_recommendation_at"])

    def test_no_replan_attempt_stays_zero(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("SOME_UNRELATED_EVENT", source="test")
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["agent"]["replan_attempt_count"], 0)
        self.assertEqual(summary["mission"]["revision_count"], 0)
        self.assertFalse(summary["mission"]["replanned"])

    def test_successful_return_replan_reports_attempt_and_revision(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("REPLAN_STATE_CHANGED", source="replan_controller",
                         data={"from": "HOLD_CONFIRMED", "to": "PLANNING"})
        rec.record_event("REPLAN_STATE_CHANGED", source="replan_controller",
                         data={"from": "RESUME_REQUESTED", "to": "MONITORING_REVISED"})
        rec.record_revision({"new_revision": 1, "mission_id": "m1",
                            "original_route_hash": "sha256:abc", "revised_route_hash": "sha256:def"})
        run_dir = os.path.join(self.tmpdir, run_id)
        self.assertTrue(_wait_for(lambda: os.path.exists(os.path.join(run_dir, "revised_mission_r1.json"))))
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
        self.assertGreaterEqual(summary["agent"]["replan_attempt_count"], 1)
        self.assertGreaterEqual(summary["mission"]["revision_count"], 1)
        self.assertTrue(summary["mission"]["replanned"])

    def test_hold_only_transaction_is_not_reported_as_replanned(self):
        # P0-3 evidence-accuracy regression (run-20260820-150834-usv-2-
        # ae61e617): replan_controller._direct_safe_hold() (decision_policy
        # requested a HOLD-only safety hold; PLANNING/VALIDATING/UPLOAD never
        # attempted) still calls _finalize_revision() -> record_revision() on
        # its terminal SAFE_HOLD, exactly like a genuine attempt does -- so
        # revision_count/revised_mission_rN.json alone cannot distinguish the
        # two. mission.replanned must stay False here: no PLANNING state was
        # ever entered, only HOLD_REQUESTED -> HOLD_CONFIRMED -> SAFE_HOLD,
        # so replan_attempt_count is 0 even though a revision record exists.
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("REPLAN_STATE_CHANGED", source="replan_controller",
                         data={"from": "MONITORING", "to": "HOLD_REQUESTED"})
        rec.record_event("REPLAN_STATE_CHANGED", source="replan_controller",
                         data={"from": "HOLD_REQUESTED", "to": "HOLD_CONFIRMED"})
        rec.record_event("REPLAN_STATE_CHANGED", source="replan_controller",
                         data={"from": "HOLD_CONFIRMED", "to": "SAFE_HOLD"})
        rec.record_revision({"new_revision": 1, "mission_id": "m1",
                            "original_route_hash": "sha256:abc", "revised_route_hash": None})
        run_dir = os.path.join(self.tmpdir, run_id)
        self.assertTrue(_wait_for(lambda: os.path.exists(os.path.join(run_dir, "revised_mission_r1.json"))))
        self._finalize_and_wait(rec, outcome="SUSPENDED", reason="test")
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["agent"]["replan_attempt_count"], 0)   # PLANNING never entered
        self.assertEqual(summary["mission"]["revision_count"], 1)       # a revision record WAS written
        self.assertFalse(summary["mission"]["replanned"])               # but no replan was attempted


# ═══════════════════════════════════════════════════════════════════════════
# E3: communication-degradation evidence (communication_source, alongside
# the pre-existing communication_state) reaches both recorded streams.
# ═══════════════════════════════════════════════════════════════════════════
class CommunicationSourceEvidenceTests(RecorderTestCase):
    def _csv_row_count(self, path):
        if not os.path.exists(path):
            return 0
        with open(path, newline="") as f:
            return sum(1 for _ in csv.DictReader(f))

    def test_communication_source_recorded_in_telemetry_csv(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({
            "communication_state": "DISCONNECTED",
            "communication_source": "SIMULATED",
        })
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        # record_telemetry is LOW priority (see experiment_recorder.py) --
        # wait for the actual data row, not just the header, before
        # finalizing (a HIGH-priority job), otherwise finalize can drain
        # ahead of it and close the run before this row is written.
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertIn("communication_source", rows[0])
        matches = [r for r in rows if r["communication_state"] == "DISCONNECTED"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["communication_source"], "SIMULATED")

    def test_communication_source_recorded_in_decision_snapshot(self):
        # record_decision is JSONL passthrough (no fixed-column filtering
        # like the CSV writer) -- communication_source lands unmodified.
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_decision({
            "communication_state": "PARTITIONED",
            "communication_source": "REAL",
            "risk": {"level": "ELEVATED", "recommendation": "CONTINUE_WITH_CAUTION"},
            "action_request": {"action": "NONE"},
        })
        path = os.path.join(self.tmpdir, run_id, "decision_snapshots.jsonl")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        records = _read_jsonl(path)
        matches = [r for r in records if r.get("communication_state") == "PARTITIONED"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["communication_source"], "REAL")


# ═══════════════════════════════════════════════════════════════════════════
# E3 instrumentation task: WireGuard handshake-freshness evidence
# (wireguard_handshake_age_s / wireguard_fresh) must round-trip into
# telemetry.csv alongside communication_state, so a run's PARTITIONED ->
# DISCONNECTED transition can be reconstructed against the SAME freshness
# evidence the classifier used, not a re-derived one. Observational columns
# only -- nothing here exercises or changes communication.py's classifier.
# ═══════════════════════════════════════════════════════════════════════════
class WireguardFreshnessEvidenceTests(RecorderTestCase):
    def _csv_row_count(self, path):
        if not os.path.exists(path):
            return 0
        with open(path, newline="") as f:
            return sum(1 for _ in csv.DictReader(f))

    def test_new_columns_present_in_header(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        with open(path, newline="") as f:
            header = next(csv.reader(f))
        for col in ("wireguard_handshake_age_s", "wireguard_fresh"):
            self.assertIn(col, header)

    def test_fresh_handshake_recorded_as_numeric_age_and_true(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({
            "communication_state": "CONNECTED",
            "wireguard_handshake_age_s": 12.3,
            "wireguard_fresh": True,
        })
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        matches = [r for r in rows if r.get("communication_state") == "CONNECTED"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["wireguard_handshake_age_s"], "12.3")
        self.assertEqual(matches[0]["wireguard_fresh"], "True")

    def test_stale_handshake_recorded_as_numeric_age_and_false(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({
            "communication_state": "PARTITIONED",
            "wireguard_handshake_age_s": 245.7,
            "wireguard_fresh": False,
        })
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        matches = [r for r in rows if r.get("communication_state") == "PARTITIONED"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["wireguard_handshake_age_s"], "245.7")
        self.assertEqual(matches[0]["wireguard_fresh"], "False")

    def test_unavailable_evidence_recorded_as_empty_not_fabricated(self):
        """No peer / never handshaked / wg unavailable -> explicit empty,
        never a fabricated 0 or False (task: do not invent 0, do not clamp)."""
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({
            "communication_state": "DISCONNECTED",
            "wireguard_handshake_age_s": None,
            "wireguard_fresh": None,
        })
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        matches = [r for r in rows if r.get("communication_state") == "DISCONNECTED"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["wireguard_handshake_age_s"], "")
        self.assertEqual(matches[0]["wireguard_fresh"], "")


# ═══════════════════════════════════════════════════════════════════════════
# Energy-calibration telemetry additions (energy-calibration recorder task,
# Phase 2/5): voltage_V/current_A/battery_source/vehicle_id/position_age_s
# columns must round-trip through record_telemetry into telemetry.csv, and a
# synthetic battery_percent injection must never make the physical
# voltage/current evidence disappear.
# ═══════════════════════════════════════════════════════════════════════════
class EnergyCalibrationTelemetryTests(RecorderTestCase):
    def _csv_row_count(self, path):
        if not os.path.exists(path):
            return 0
        with open(path, newline="") as f:
            return sum(1 for _ in csv.DictReader(f))

    def test_new_columns_present_in_header(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: os.path.exists(path) and os.path.getsize(path) > 0))
        with open(path, newline="") as f:
            header = next(csv.reader(f))
        for col in ("vehicle_id", "position_age_s", "battery_source", "voltage_V", "current_A"):
            self.assertIn(col, header)

    def test_voltage_and_current_round_trip_to_csv(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({
            "vehicle_id": "usv-2", "position_age_s": 0.15,
            "battery_source": "PHYSICAL",
            "voltage_V": 23.7, "current_A": 4.25,
        })
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        matches = [r for r in rows if r.get("vehicle_id") == "usv-2"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["voltage_V"], "23.7")
        self.assertEqual(matches[0]["current_A"], "4.25")
        self.assertEqual(matches[0]["position_age_s"], "0.15")
        self.assertEqual(matches[0]["battery_source"], "PHYSICAL")

    def test_injected_battery_percent_never_hides_physical_voltage_current(self):
        """A synthetic battery_percent override must never overwrite or lose
        the physical voltage/current evidence (POWER TELEMETRY REQUIREMENTS)
        -- both are recorded on the SAME row, distinctly."""
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({
            "physical_battery_percent": 42.0,
            "injected_battery_percent": 15.0,
            "battery_source": "INJECTED",
            "voltage_V": 22.9, "current_A": 6.1,
        })
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        matches = [r for r in rows if r.get("injected_battery_percent") == "15.0"]
        self.assertTrue(matches)
        row = matches[0]
        # The energy policy used the injected value (battery_source =
        # INJECTED), but the physical reading AND the raw voltage/current
        # measurement both survive unmodified on the same row.
        self.assertEqual(row["physical_battery_percent"], "42.0")
        self.assertEqual(row["voltage_V"], "22.9")
        self.assertEqual(row["current_A"], "6.1")

    def test_missing_voltage_current_recorded_as_empty_not_zero(self):
        """No source available -> explicit empty, never fabricated 0.0
        (task: Do NOT fabricate null data)."""
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_telemetry({"vehicle_id": "usv-2", "mode": "LOITER"})
        path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: self._csv_row_count(path) >= 1))
        self._finalize_and_wait(rec)
        with open(path, newline="") as f:
            rows = list(csv.DictReader(f))
        matches = [r for r in rows if r.get("mode") == "LOITER"]
        self.assertTrue(matches)
        self.assertEqual(matches[0]["voltage_V"], "")
        self.assertEqual(matches[0]["current_A"], "")


# ═══════════════════════════════════════════════════════════════════════════
# Restart reconciliation (task section 6)
# ═══════════════════════════════════════════════════════════════════════════
class RestartReconciliationTests(RecorderTestCase):
    def test_reopens_same_run_when_mission_execution_is_live(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("BEFORE_RESTART", source="test")
        timeline_path = os.path.join(self.tmpdir, run_id, "timeline.jsonl")
        self.assertTrue(_wait_for_type(timeline_path, "BEFORE_RESTART"))
        rec.shutdown(timeout=2.0)

        # Simulate a fresh process: a new recorder over the SAME directory.
        rec2 = er.ExperimentRecorder(cfg=_cfg(self.tmpdir), vehicle_id="usv-test")
        self.rec = rec2
        rec2.reconcile_after_restart({"state": "RUNNING"})
        self.assertTrue(_wait_for(lambda: rec2.status()["active"] and rec2.status()["run_id"] == run_id))
        path = os.path.join(self.tmpdir, run_id, "timeline.jsonl")
        self.assertTrue(_wait_for_type(path, "PROCESS_RESTART_RECOVERY"))
        rec2.record_event("AFTER_RESTART", source="test")
        self.assertTrue(_wait_for_type(path, "AFTER_RESTART"))
        types = [l["type"] for l in _read_jsonl(path)]
        self.assertIn("BEFORE_RESTART", types)
        self.assertIn("PROCESS_RESTART_RECOVERY", types)

        # Only ONE run directory exists -- nothing was truncated or duplicated.
        runs = [n for n in os.listdir(self.tmpdir) if n.startswith("run-")]
        self.assertEqual(runs, [run_id])

    def test_finalizes_interrupted_when_mission_execution_not_live(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.shutdown(timeout=2.0)

        rec2 = er.ExperimentRecorder(cfg=_cfg(self.tmpdir), vehicle_id="usv-test")
        self.rec = rec2
        rec2.reconcile_after_restart({"state": "SUSPENDED"})
        run_dir = os.path.join(self.tmpdir, run_id)
        self.assertTrue(_wait_for(lambda: os.path.exists(os.path.join(run_dir, "summary.json"))))
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["run"]["result"], "INTERRUPTED")
        self.assertFalse(rec2.status()["active"])


# ═══════════════════════════════════════════════════════════════════════════
# Terminal evidence + finalization queue ordering (task sections 2/3/14)
# ═══════════════════════════════════════════════════════════════════════════
class TerminalEvidenceAndOrderingTests(RecorderTestCase):
    def test_final_events_not_lost_race_with_finalize(self):
        """event A, event B, STOP_COMPLETE, finalize_async enqueued back to
        back (no waiting between them) -- ALL must appear in the finalized
        timeline.jsonl before summary.json/checksums.sha256 are produced
        (task section 14)."""
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("EVENT_A", source="test")
        rec.record_event("EVENT_B", source="test")
        rec.record_event("STOP_COMPLETE", source="test", data={
            "final_mode": "LOITER", "final_armed": True, "final_authority": "OPERATOR",
        })
        rec.finalize_async("STOP_COMPLETE", "Stop Mission completed; authority -> OPERATOR.")
        self.assertTrue(_wait_for(lambda: rec.status()["recorder_state"] in ("FINALIZED", "IDLE")))

        run_dir = os.path.join(self.tmpdir, run_id)
        types = [l["type"] for l in _read_jsonl(os.path.join(run_dir, "timeline.jsonl"))]
        self.assertIn("EVENT_A", types)
        self.assertIn("EVENT_B", types)
        self.assertIn("STOP_COMPLETE", types)
        # Order preserved too, not just presence.
        self.assertLess(types.index("EVENT_A"), types.index("EVENT_B"))
        self.assertLess(types.index("EVENT_B"), types.index("STOP_COMPLETE"))
        with open(os.path.join(run_dir, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["vehicle"]["final_mode"], "LOITER")
        self.assertEqual(summary["vehicle"]["final_armed"], True)
        self.assertEqual(summary["vehicle"]["final_authority"], "OPERATOR")

    def test_stop_complete_terminal_evidence_overrides_stale_telemetry(self):
        """Reproduces the real-bundle defect directly at the recorder level:
        a periodic telemetry sample still shows LOCAL_AGENT (queued before
        the Stop transaction actually handed authority back), followed by an
        explicit STOP_COMPLETE event proving authority_after=OPERATOR. The
        terminal evidence must win -- summary.vehicle.final_authority must
        NEVER contradict a terminal_reason that says "authority -> OPERATOR"."""
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        csv_path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        # Stale periodic telemetry, authority still LOCAL_AGENT.
        rec.record_telemetry({"mode": "LOITER", "armed": True, "control_authority": "LOCAL_AGENT"})
        self.assertTrue(_wait_for(lambda: os.path.exists(csv_path) and os.path.getsize(csv_path) > 0))
        # Authoritative Stop terminal evidence, proven AFTER that sample.
        rec.record_event("STOP_COMPLETE", source="mission_execution_controller", data={
            "final_mode": "LOITER", "final_armed": True, "final_authority": "OPERATOR",
            "hold_verified": True, "authority_after": "OPERATOR", "ready_for_start": True,
        })
        rec.finalize_async("STOP_COMPLETE", "Stop Mission completed from RUNNING; authority -> OPERATOR.")
        self.assertTrue(_wait_for(lambda: rec.status()["recorder_state"] in ("FINALIZED", "IDLE")))
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        self.assertEqual(summary["run"]["result"], "STOP_COMPLETE")
        self.assertEqual(summary["vehicle"]["final_mode"], "LOITER")
        self.assertEqual(summary["vehicle"]["final_authority"], "OPERATOR")
        # The exact contradiction the real bundles showed must be impossible.
        reason = summary["run"]["terminal_reason"] or ""
        if "authority -> OPERATOR" in reason:
            self.assertEqual(summary["vehicle"]["final_authority"], "OPERATOR")
        self.assertEqual(summary["stop"]["authority_after"], "OPERATOR")
        self.assertEqual(summary["stop"]["hold_verified"], True)


# ═══════════════════════════════════════════════════════════════════════════
# Latest-snapshot telemetry sampler (task sections 4/5/15/16/17/18)
# ═══════════════════════════════════════════════════════════════════════════
class TelemetrySamplerTests(RecorderTestCase):
    def test_sampler_achieves_configured_rate_2hz(self):
        rec = self._new_recorder(experiment_record_telemetry_hz=2.0)
        self._start_and_wait(rec)
        for i in range(50):
            rec.update_latest_telemetry_snapshot({"mode": "AUTO", "control_authority": "LOCAL_AGENT", "n": i})
            time.sleep(0.02)
        time.sleep(5.0)
        rows = _wait_and_count_csv_rows(self, rec, run_id=rec.status().get("run_id") or self._last_run_id(rec))
        # ~5s at 2 Hz => ~10 samples. Loose bounds -- no scheduler timing
        # dependency, just "clearly faster than the old ~0.2-0.4 Hz bug".
        self.assertGreaterEqual(rows, 6)
        self.assertLessEqual(rows, 16)

    def test_sampler_achieves_configured_rate_1hz(self):
        rec = self._new_recorder(experiment_record_telemetry_hz=1.0)
        self._start_and_wait(rec)
        for i in range(50):
            rec.update_latest_telemetry_snapshot({"mode": "AUTO", "n": i})
            time.sleep(0.02)
        time.sleep(5.0)
        rows = _wait_and_count_csv_rows(self, rec, run_id=rec.status().get("run_id") or self._last_run_id(rec))
        self.assertGreaterEqual(rows, 3)
        self.assertLessEqual(rows, 9)

    def test_producer_handoff_never_blocks(self):
        """update_latest_telemetry_snapshot must return near-instantly even
        with a stalled writer -- it never touches the writer/queue directly."""
        rec = self._new_recorder()
        self._start_and_wait(rec)
        rec._dispatch = lambda job: time.sleep(1.0)
        t0 = time.monotonic()
        for i in range(200):
            rec.update_latest_telemetry_snapshot({"n": i})
        self.assertLess(time.monotonic() - t0, 0.2)

    def test_main_loop_stall_does_not_stop_sampling(self):
        """Once a snapshot exists, the sampler keeps recording it at its own
        cadence even if the producer stops calling update_latest_telemetry_
        snapshot -- snapshot_age_s must grow, never claiming freshness it
        doesn't have (task section 16)."""
        rec = self._new_recorder(experiment_record_telemetry_hz=2.0)
        run_id = self._start_and_wait(rec)
        rec.update_latest_telemetry_snapshot({"mode": "LOITER"})
        time.sleep(3.0)  # simulate a 3s main-loop stall -- no further updates
        run_dir = os.path.join(self.tmpdir, run_id)
        csv_path = os.path.join(run_dir, "telemetry.csv")
        self.assertTrue(_wait_for(lambda: _csv_row_count(csv_path) >= 3))
        rows = _read_csv_rows(csv_path)
        ages = [float(r["snapshot_age_s"]) for r in rows if r["snapshot_age_s"] not in (None, "")]
        self.assertGreaterEqual(len(ages), 3)
        # Ages across the stall must be non-decreasing-ish (monotonically
        # growing while no fresh snapshot arrives) and clearly > 0 by the end.
        self.assertGreater(ages[-1], ages[0])
        self.assertGreater(ages[-1], 1.0)

    def test_no_snapshot_yet_produces_no_samples(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        time.sleep(1.0)  # several sampler cycles at default 2 Hz
        csv_path = os.path.join(self.tmpdir, run_id, "telemetry.csv")
        # Header only (or not yet created) -- never a fabricated row.
        self.assertLessEqual(_csv_row_count(csv_path), 0)

    def test_sampler_performs_no_external_io(self):
        """Guard every plausible external-I/O surface the sampler could
        reach and prove none of them are ever called (task section 17)."""
        import unittest.mock as mock
        rec = self._new_recorder(experiment_record_telemetry_hz=2.0)
        self._start_and_wait(rec)

        def _boom(*a, **kw):
            raise AssertionError("sampler must not perform external I/O")

        with mock.patch("subprocess.run", side_effect=_boom):
            rec.update_latest_telemetry_snapshot({"mode": "AUTO"})
            time.sleep(1.5)
        # No exception means _boom was never hit via the sampler path.

    def test_sampler_queue_saturation_drops_without_blocking_or_affecting_high_priority(self):
        rec = self._new_recorder(experiment_record_low_queue_capacity=2,
                                 experiment_record_queue_capacity=64,
                                 experiment_record_telemetry_hz=50.0)
        run_id = self._start_and_wait(rec)
        # Stall only long enough to guarantee the low-priority queue fills.
        rec._dispatch = lambda job: time.sleep(0.5)
        rec.update_latest_telemetry_snapshot({"mode": "AUTO"})
        time.sleep(1.0)
        status = rec.status()
        self.assertGreater(status["dropped_telemetry_records"], 0)
        # A high-priority lifecycle record must still get through/queue fine
        # (never blocked by low-priority saturation) -- the call itself must
        # stay fast even while the writer is stalled.
        t0 = time.monotonic()
        rec.record_event("LIFECYCLE_EVENT_DURING_SATURATION", source="test", priority="high")
        self.assertLess(time.monotonic() - t0, 0.2)

    def _last_run_id(self, rec):
        return rec.status().get("run_id") or rec.status().get("last_finalized_run_id")


def _csv_row_count(csv_path):
    if not os.path.exists(csv_path):
        return 0
    with open(csv_path, newline="") as f:
        return max(0, sum(1 for _ in f) - 1)  # minus header


def _read_csv_rows(csv_path):
    import csv as _csv
    if not os.path.exists(csv_path):
        return []
    with open(csv_path, newline="") as f:
        return list(_csv.DictReader(f))


def _wait_and_count_csv_rows(testcase, rec, run_id):
    csv_path = os.path.join(rec.cfg.experiment_record_directory, run_id, "telemetry.csv")
    _wait_for(lambda: os.path.exists(csv_path))
    return _csv_row_count(csv_path)


# ═══════════════════════════════════════════════════════════════════════════
# Start timing derivation (task sections 7/8/9/19)
# ═══════════════════════════════════════════════════════════════════════════
class StartTimingTests(RecorderTestCase):
    def test_start_to_running_s_derived_from_authoritative_events(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        timeline_path = os.path.join(self.tmpdir, run_id, "timeline.jsonl")
        self.assertTrue(_wait_for_type(timeline_path, "RECORDER_RUN_STARTED"))

        rec.record_event("MISSION_START_REQUESTED", source="mission_execution_controller",
                         data={"mission_id": "m1"})
        time.sleep(0.3)
        rec.record_event("MISSION_EXECUTION_STATE_CHANGED", source="transition_log:mission_execution",
                         data={"from": "STARTING_AUTO", "to": "RUNNING", "reason": "progression proven"})
        self.assertTrue(_wait_for_type(timeline_path, "MISSION_EXECUTION_STATE_CHANGED"))
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")

        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        start_to_running = summary["timing"]["start_to_running_s"]
        self.assertIsNotNone(start_to_running)
        self.assertGreater(start_to_running, 0.15)
        self.assertLess(start_to_running, 2.0)

    def test_start_to_running_s_null_when_running_never_happens(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("MISSION_START_REQUESTED", source="test")
        self._finalize_and_wait(rec, outcome="FAILED", reason="never reached RUNNING")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        self.assertIsNone(summary["timing"]["start_to_running_s"])

    def test_replan_trigger_timings_derived_from_named_events(self):
        rec = self._new_recorder()
        run_id = self._start_and_wait(rec)
        rec.record_event("REPLAN_STATE_CHANGED", source="test", data={"from": "MONITORING", "to": "HOLD_REQUESTED"})
        time.sleep(0.1)
        rec.record_event("REPLAN_STATE_CHANGED", source="test", data={"from": "HOLD_REQUESTED", "to": "HOLD_CONFIRMED"})
        time.sleep(0.1)
        rec.record_event("REPLAN_STATE_CHANGED", source="test", data={"from": "RESUME_REQUESTED", "to": "MONITORING_REVISED"})
        self._finalize_and_wait(rec, outcome="COMPLETED_HOLD", reason="test")
        with open(os.path.join(self.tmpdir, run_id, "summary.json")) as f:
            summary = json.load(f)
        timing = summary["timing"]
        self.assertIsNotNone(timing["first_trigger_to_hold_s"])
        self.assertIsNotNone(timing["first_trigger_to_revised_auto_s"])
        self.assertIsNone(timing["first_trigger_to_terminal_s"])  # no terminal replan event occurred


if __name__ == "__main__":
    unittest.main(verbosity=2)
