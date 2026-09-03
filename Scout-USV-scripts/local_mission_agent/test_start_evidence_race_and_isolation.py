"""
Fixed-behaviour regressions for the Scout Start Mission investigation's four
PROVEN root causes and their MINIMAL production fixes:

  A. Start proof acquisition (mission_execution_controller._acquire_start_proof)
     -- a bounded, in-transaction retry for TRANSIENT Pixhawk-readback
     unavailability (busy/refreshing/stale), never for a definitive failure.
  B. AUTO/mode verification post-command heartbeat freshness -- covered by
     motherpi/services/flask/test_mode_verification.py's
     TestAutoVerificationRequiresPostCommandFreshHeartbeat (a different
     service/test binary; not duplicated here).
  C. mission_active_evidence (ACTIVE_TRUE) freshness -- covered by
     test_mission_progression.py's TestPositiveProof.
     test_active_true_stale_age_does_not_prove / _fresh_age_proves (not
     duplicated here; this file only smoke-checks the controller wiring).
  D. Failed-Start terminal recorder evidence (mission_execution_controller.
     _end_operation's terminal_evidence -> _transition -> recorder).

Uses the SAME no-HTTP/no-MAVLink FakeGateway harness as
test_mission_execution_controller.py (imported as `tmec`), and the same
AdvancingClock pattern tmec's own TestProgressionWatch uses so every bounded
wait in these tests is simulated instantly, never real wall-clock time.
"""
import unittest

import mission_execution_controller as mec
import planning_package as pp
import test_mission_execution_controller as tmec
import write_arbiter


class _AdvancingClock:
    """clock()/sleep() pair for a deterministic, instant bounded-retry test --
    identical convention to tmec.AdvancingClock, defined locally so this file
    has no import-order dependency on it."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


# ── A. START PROOF ACQUISITION (task section 1 / test items 1-3) ──────────────
class TestStartProofAcquisitionBoundedRetry(tmec._Base):
    """_acquire_start_proof wraps _resolve_start_prerequisites in a bounded
    retry, INSIDE the same Start transaction, for exactly the transient
    "not fresh/available YET" codes (_START_PROOF_TRANSIENT_CODES) -- never
    for a definitive failure. One ctrl.start() call now absorbs a readback
    that is transiently refreshing/busy/stale and becomes fresh within
    cfg.start_proof_timeout_s -- the operator does not need to press Start
    again."""

    def _ctrl_with_clock(self, **kw):
        clock = _AdvancingClock(1000.0)
        cfg = tmec._cfg(start_proof_timeout_s=6.0, start_proof_poll_interval_s=1.0)
        ctrl = self._ctrl(cfg=cfg, clock=clock, **kw)
        ctrl._sleep = clock.advance
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl, clock

    def _flaky_readback(self, real_readback, bad_calls, make_bad, make_good):
        """Wrap FakeGateway.pixhawk_mission_readback so the first `bad_calls`
        invocations apply `make_bad` (mutating self.gw to a transient-bad
        state) before delegating -- then, from call `bad_calls + 1` onward,
        `make_good` restores fresh evidence first. Models the coordinator's
        background refresh genuinely completing while Start's own bounded
        proof-acquisition loop is still polling."""
        state = {"n": 0}

        def wrapped():
            state["n"] += 1
            if state["n"] <= bad_calls:
                make_bad()
            else:
                make_good()
            return real_readback()
        return wrapped

    def test_transient_refreshing_readback_same_start_call_waits_and_succeeds(self):
        # Task test 1: readback flagged "refreshing" (a coordinator background
        # download in flight) for the first TWO reads, fresh from the third --
        # entirely within one ctrl.start() call, no rearm, no second press.
        real = self.gw.pixhawk_mission_readback
        def make_refreshing():
            self.gw.pixhawk_refreshing = True
            self.gw.pixhawk_stale = True
            self.gw.pixhawk_age_s = 99.0
        def make_fresh():
            self.gw.pixhawk_refreshing = False
            self.gw.pixhawk_stale = False
            self.gw.pixhawk_age_s = 0.5
        self.gw.pixhawk_mission_readback = self._flaky_readback(
            real, bad_calls=2, make_bad=make_refreshing, make_good=make_fresh)
        make_refreshing()  # the readback starts out refreshing

        ctrl, clock = self._ctrl_with_clock()
        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.RUNNING,
                         f"expected RUNNING, got {res}")
        self.assertEqual(res["mission_id"], "m1")
        self.assertEqual(res["route_hash"], self.route_hash)

    def test_transient_busy_readback_same_start_call_waits_and_succeeds(self):
        # Task test 2: readback flagged "busy" (mission-protocol lock held by
        # another transaction) for the first read only, fresh from the second.
        real = self.gw.pixhawk_mission_readback
        def make_busy():
            self.gw.pixhawk_busy = True
            self.gw.pixhawk_stale = True
            self.gw.pixhawk_age_s = 99.0
        def clear_busy():
            self.gw.pixhawk_busy = False
            self.gw.pixhawk_stale = False
            self.gw.pixhawk_age_s = 0.5
        self.gw.pixhawk_mission_readback = self._flaky_readback(
            real, bad_calls=1, make_bad=make_busy, make_good=clear_busy)
        make_busy()

        ctrl, clock = self._ctrl_with_clock()
        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.RUNNING, f"expected RUNNING, got {res}")

    def test_never_becoming_fresh_times_out_and_fails_with_diagnostics(self):
        # The readback stays refreshing/stale for MORE reads than the bounded
        # window allows -- Start must still fail closed (never wait forever,
        # never accept stale proof), and the failure carries proof_acquisition
        # diagnostics (attempts / elapsed_s / last_transient_reason).
        self.gw.pixhawk_refreshing = True
        self.gw.pixhawk_stale = True
        self.gw.pixhawk_age_s = 99.0
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertIn(res["error"]["code"], ("PIXHAWK_READBACK_STALE", "ROUTE_HASH_STALE"))
        acquisition = res["error"]["detail"]["proof_acquisition"]
        self.assertGreater(acquisition["attempts"], 1)
        self.assertGreaterEqual(acquisition["elapsed_s"], ctrl.cfg.start_proof_timeout_s)
        self.assertIsNotNone(acquisition["last_transient_reason"])
        self.assertEqual(self.gw.write_calls, [])

    def test_definitive_hash_mismatch_fails_immediately_with_zero_retry_delay(self):
        # Task test 3: a DEFINITIVE mismatch (not a transient staleness) must
        # never be retried -- the very first attempt already resolves, and
        # elapsed_s stays ~0 (no bounded-wait sleep was ever entered). Code/
        # transient-classification correction: a COMPLETED, FRESH proof of a
        # genuine mismatch (READY_PACKAGE_STALE) reports the definitive
        # "ROUTE_HASH_MISMATCH" code -- never "ROUTE_HASH_STALE", which
        # _START_PROOF_TRANSIENT_CODES treats as retryable.
        self.gw.pixhawk_route_hash = "sha256:" + ("0" * 64)  # definitively wrong
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "ROUTE_HASH_MISMATCH")
        self.assertEqual(res["error"]["detail"]["readiness_state"], "PLANNING_PACKAGE_STALE")
        acquisition = res["error"]["detail"]["proof_acquisition"]
        self.assertEqual(acquisition["attempts"], 1)
        self.assertEqual(acquisition["elapsed_s"], 0.0)  # clock never advanced -- no retry sleep
        self.assertEqual(self.gw.write_calls, [])

    def test_active_replan_fails_immediately_unaffected_by_proof_acquisition(self):
        # Task test 4: an active replan transaction still blocks Start on the
        # very first call, entirely outside (before) proof acquisition.
        ctrl, clock = self._ctrl_with_clock()
        tok = write_arbiter.acquire(write_arbiter.OWNER_REPLANNING)
        try:
            res = ctrl.start("m1")
        finally:
            write_arbiter.release(tok)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["error"]["code"], "REPLANNING_ACTIVE")
        self.assertEqual(self.gw.write_calls, [])


# ── A2. READY_* -> controller-code / transient-classification mapping ─────────
class TestRouteHashReadinessCodeClassification(tmec._Base):
    """Audit of the planning_package.build_readiness() `state` ->
    _resolve_start_prerequisites() controller-code mapping (code/transient-
    classification correction): build_readiness's own docstring reserves
    READY_PACKAGE_STALE EXCLUSIVELY for "a COMPLETED, FRESH proof [that]
    shows a genuine mismatch" -- definitive bad evidence -- while
    READY_REFRESHING / READY_PROOF_STALE / READY_HASH_UNAVAILABLE all mean
    "no fresh evidence YET / nothing to compare against yet", never a proven
    mismatch. The two genuine freshness-axis states must retry and can
    succeed once evidence catches up; READY_PACKAGE_STALE must never be
    retried -- it fails on the very first attempt, zero delay, zero writes.

    Monkeypatches ONLY planning_package.build_readiness (restored in
    tearDown), never planning_package.readback_is_fresh -- the FakeGateway's
    own readback stays genuinely fresh throughout, passing the REAL,
    unpatched freshness gate in _resolve_start_prerequisites exactly as
    production does (that gate already intercepts most real refreshing/
    stale readbacks before build_readiness is even reached -- see that
    gate's own comment); only what build_readiness classifies this already-
    fresh readback as is stubbed, so each `state` branch is exercised
    directly and deterministically without racing real wall-clock freshness
    windows."""

    def setUp(self):
        super().setUp()
        self._real_build_readiness = pp.build_readiness

    def tearDown(self):
        pp.build_readiness = self._real_build_readiness
        super().tearDown()

    def _ctrl_with_clock(self, **kw):
        clock = _AdvancingClock(1000.0)
        cfg = tmec._cfg(start_proof_timeout_s=6.0, start_proof_poll_interval_s=1.0)
        ctrl = self._ctrl(cfg=cfg, clock=clock, **kw)
        ctrl._sleep = clock.advance
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl, clock

    def _flaky_readiness(self, bad_state, bad_calls):
        """The first `bad_calls` calls report `bad_state` with
        route_hash_match forced False (the controller's mismatch branch);
        from call `bad_calls + 1` onward, delegates to the REAL
        build_readiness untouched -- models the coordinator's background
        readiness proof genuinely completing while Start's own bounded
        proof-acquisition loop is still polling."""
        real = self._real_build_readiness
        state = {"n": 0}

        def fake(readback, now=None):
            state["n"] += 1
            r = real(readback, now=now)
            if state["n"] <= bad_calls:
                r = dict(r)
                r["state"] = bad_state
                r["route_hash_match"] = False
            return r
        return fake

    def test_ready_refreshing_retries_and_can_later_succeed(self):
        # Task test 1.
        pp.build_readiness = self._flaky_readiness(pp.READY_REFRESHING, bad_calls=2)
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.RUNNING, f"expected RUNNING, got {res}")

    def test_ready_proof_stale_retries_and_can_later_succeed(self):
        # Task test 2.
        pp.build_readiness = self._flaky_readiness(pp.READY_PROOF_STALE, bad_calls=2)
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.RUNNING, f"expected RUNNING, got {res}")

    def test_ready_hash_unavailable_retries_and_can_later_succeed(self):
        # Task test 3 (first half): HASH_COMPARISON_UNAVAILABLE ("fresh
        # readback, but no route hash to compare against yet") is genuinely
        # retryable -- and, like the other two freshness-axis states,
        # resolves once real evidence (a hash to compare) actually becomes
        # available.
        pp.build_readiness = self._flaky_readiness(pp.READY_HASH_UNAVAILABLE, bad_calls=2)
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.RUNNING, f"expected RUNNING, got {res}")

    def test_ready_hash_unavailable_never_resolving_still_fails_closed(self):
        # Task test 3 (second half): retryable does NOT mean retried
        # indefinitely/unconditionally -- while it stays genuinely
        # unavailable for the WHOLE bounded window, Start still fails
        # closed once start_proof_timeout_s elapses (never an unbounded
        # wait), with zero vehicle writes.
        pp.build_readiness = self._flaky_readiness(pp.READY_HASH_UNAVAILABLE, bad_calls=999)
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "ROUTE_HASH_UNAVAILABLE")
        acquisition = res["error"]["detail"]["proof_acquisition"]
        self.assertGreaterEqual(acquisition["elapsed_s"], ctrl.cfg.start_proof_timeout_s)
        self.assertEqual(self.gw.write_calls, [])

    def test_ready_package_stale_fails_first_attempt_zero_delay_no_writes(self):
        # Task tests 4/5/6: a COMPLETED, FRESH proof of a genuine mismatch
        # (READY_PACKAGE_STALE) is DEFINITIVE -- never retried regardless of
        # start_proof_timeout_s (fails on attempt 1, elapsed_s stays 0.0 --
        # the clock never advances because no retry sleep is ever entered),
        # and no vehicle write occurs.
        pp.build_readiness = self._flaky_readiness(pp.READY_PACKAGE_STALE, bad_calls=999)
        ctrl, clock = self._ctrl_with_clock()

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "ROUTE_HASH_MISMATCH")
        acquisition = res["error"]["detail"]["proof_acquisition"]
        self.assertEqual(acquisition["attempts"], 1)
        self.assertEqual(acquisition["elapsed_s"], 0.0)
        self.assertEqual(self.gw.write_calls, [])


# ── B. LOCAL VS OPERATOR COMMUNICATION ISOLATION (task test 12) ───────────────
class TestPartitionedCommunicationNeverReachesTheSnapshot(tmec._Base):
    """Prove operator_reachable=false / comm_state=PARTITIONED cannot alter
    Pixhawk mode collection, mission progression collection, AUTO verification,
    or the progression watcher: mission_execution_controller._build_snapshot()
    always calls decision_snapshot.build_snapshot(..., comm_state=None, ...),
    a hardcoded constant, never derived from vehicle_state -- so even a
    vehicle_state document that carries a PARTITIONED communication block
    (exactly what GET /agent/state -> agent_state.py forwards) has no comm_state
    path into the snapshot the Start FSM/progression watcher reasons from. Kept
    exactly as the investigation proved it -- this task does not touch that
    architecture."""

    def test_build_snapshot_always_passes_comm_state_none(self):
        calls = []
        import decision_snapshot
        real_build_snapshot = decision_snapshot.build_snapshot

        def spy(vehicle_state, comm_state, *a, **kw):
            calls.append(comm_state)
            return real_build_snapshot(vehicle_state, comm_state, *a, **kw)

        decision_snapshot.build_snapshot = spy
        try:
            ctrl = self._ctrl()
            vs = self.gw.read_vehicle_state()
            vs["communication"] = {"connectivity": "PARTITIONED", "operator_reachable": False}
            ctrl._build_snapshot(vs)
        finally:
            decision_snapshot.build_snapshot = real_build_snapshot

        self.assertTrue(calls, "build_snapshot was never called")
        self.assertTrue(all(c is None for c in calls),
                        f"_build_snapshot must always pass comm_state=None; got {calls}")

    def test_start_succeeds_identically_whether_or_not_vehicle_state_reports_partitioned(self):
        orig_read = self.gw.read_vehicle_state

        def _read():
            vs = orig_read()
            vs["communication"] = {"connectivity": "PARTITIONED", "operator_reachable": False}
            return vs
        self.gw.read_vehicle_state = _read

        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertEqual(self.gw.write_calls, ["loiter", "set_home", "auto"])


# ── D. POST-FAILURE LOITER / RECORDER TERMINAL EVIDENCE (task tests 13-15) ────
class _FakeRecorder:
    """Captures every record_event() call -- enough to prove whether (and with
    what data) the controller tells the experiment recorder about a failure,
    without any real recorder/file I/O."""
    def __init__(self):
        self.events = []

    def record_event(self, event_type, source=None, data=None, priority=None):
        self.events.append({"event_type": event_type, "source": source,
                            "data": dict(data or {}), "priority": priority})

    def start_run(self, **kw):
        pass


class TestFailedStartLoiterRecoveryEvidence(tmec._Base):
    """Task tests 13/14/15 -- the failed-Start terminal-evidence fix
    (_end_operation now emits MISSION_EXECUTION_TERMINAL_EVIDENCE, reusing the
    SAME mechanism _run_final_hold's COMPLETED_HOLD success path already used,
    with a fresh post-LOITER-restore vehicle read)."""

    def _failing_progression_ctrl(self, recorder=None):
        cfg = tmec._cfg(start_progression_timeout_s=0.05, progression_poll_interval_s=0.02)
        ctrl = self._ctrl(cfg=cfg, recorder=recorder)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        # No positive progression proof will ever appear: baseline seq == 1,
        # ACTIVE_UNKNOWN, stationary -- forces the full-deadline
        # PROGRESSION_UNCONFIRMED failure path (ensure_loiter=True).
        self.gw.mission_active_evidence = "ACTIVE_UNKNOWN"
        self.gw.auto_sets_evidence = "ACTIVE_UNKNOWN"
        ctrl._sleep = lambda s: None  # no real wall-clock wait in the test
        return ctrl

    def test_failed_progression_reports_fresh_verified_loiter(self):
        # Task test 13: the live safety property -- LOITER was freshly
        # re-commanded and re-verified before the failure was reported.
        ctrl = self._failing_progression_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED")
        self.assertTrue(res["error"]["fallback_loiter_verified"])
        self.assertEqual(self.gw.mode_name, "LOITER")

    def test_recorder_final_mode_is_loiter_after_verified_failed_start_restoration(self):
        # Task test 14: the recorder now RECEIVES this fresh proof -- a
        # MISSION_EXECUTION_TERMINAL_EVIDENCE event whose final_mode is the
        # FRESH post-restore LOITER, never a stale periodic sample. This is
        # the root-cause fix for the live 'final_mode: AUTO' next to
        # 'terminal_reason: "...restoring LOITER"' contradiction (see
        # experiment_runs/run-20260813-202540-usv-2-5dccdefb/summary.json).
        recorder = _FakeRecorder()
        ctrl = self._failing_progression_ctrl(recorder=recorder)
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)

        terminal_events = [e for e in recorder.events
                           if e["event_type"] == "MISSION_EXECUTION_TERMINAL_EVIDENCE"]
        self.assertEqual(len(terminal_events), 1, recorder.events)
        data = terminal_events[0]["data"]
        self.assertEqual(data["final_mode"], "LOITER")
        self.assertTrue(data["final_armed"])
        self.assertEqual(data["mission_execution_state"], mec.FAILED)
        self.assertTrue(data["fallback_loiter_verified"])
        self.assertEqual(data["mission_id"], "m1")
        self.assertEqual(data["route_hash"], self.route_hash)

    def test_loiter_restoration_failure_is_explicitly_represented_not_fabricated(self):
        # Task test 15: when LOITER genuinely cannot be verified, the recorder
        # must be told fallback_loiter_verified=false and the OBSERVED mode
        # (still AUTO, or whatever fresh state actually shows) -- never a
        # fabricated LOITER claim, and the original safety failure reason
        # (PROGRESSION_UNCONFIRMED) is preserved, not overwritten.
        recorder = _FakeRecorder()
        ctrl = self._failing_progression_ctrl(recorder=recorder)
        # The INITIAL launch-safety-hold LOITER (before ARM/Home/AUTO) must
        # still succeed -- only the FALLBACK restore LOITER, sent after the
        # progression failure, fails here (a second RC/failsafe/link issue
        # at the worst possible moment, not a Start precondition problem).
        real_loiter = self.gw.command_loiter
        def flaky_loiter():
            self.gw.loiter_verified = (self.gw.calls.count("loiter") == 0)
            return real_loiter()
        self.gw.command_loiter = flaky_loiter
        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "PROGRESSION_UNCONFIRMED",
                         "the original safety failure reason must be preserved")
        self.assertFalse(res["error"]["fallback_loiter_verified"])

        terminal_events = [e for e in recorder.events
                           if e["event_type"] == "MISSION_EXECUTION_TERMINAL_EVIDENCE"]
        self.assertEqual(len(terminal_events), 1, recorder.events)
        data = terminal_events[0]["data"]
        self.assertFalse(data["fallback_loiter_verified"])
        # mode_name never changes on the fake gateway when loiter_verified is
        # False (command_loiter only flips it on success) -- final_mode
        # reports whatever was ACTUALLY observed (still AUTO here), never a
        # fabricated "LOITER".
        self.assertEqual(data["final_mode"], "AUTO")


if __name__ == "__main__":
    unittest.main(verbosity=2)
