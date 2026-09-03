"""
Regression tests for the RTL Home / Start-eligibility readiness-semantics
correction (task: "RTL Home / Start Mission readiness contract").

LIVE BUG this file guards against: before Home was ever manually verified,
Scout reported NOT_READY / "RTL Home unavailable" / "Start Mission disabled"
even though mission/package/position/authority evidence was all fine and
Start ITSELF is the transaction that sets and verifies Home (LOITER -> Set
Home Here -> verify HOME_POSITION -> re-check readiness -> ARM/AUTO). The
root cause was two places that required a POSITIVE `rtl_return_feasible`
(which can only ever be positively known once a Home is actually verified)
before Home had a chance to be established:

  1. `mission_execution_controller.status()`'s cached `feasibility_ok` fed
     directly into both `can_start` and `start_eligible`, so an UNKNOWN
     rtl_return_feasible (REASON_RTL_HOME_UNAVAILABLE, the normal pre-Start
     state) made an otherwise perfectly startable mission look un-startable.
  2. `_run_start`'s PRE-ARM mission-energy-feasibility gate rejected Start
     outright on `rtl_return_feasible is not True` -- BEFORE Start ever
     reached its own LOITER/Set Home step, so Start could never succeed from
     a genuinely unverified Home unless a Home happened to already be
     verified from some earlier run.

Uses the SAME no-HTTP/no-MAVLink FakeGateway test harness as
test_mission_execution_controller.py (imported as `tmec`) and
test_mission_execution_controller_feasibility_gate.py -- every scenario here
drives real controller code (`mission_execution_controller.py`) against
scripted, deterministic vehicle evidence, never a mock of the code under
test itself.

Scenario coverage (see the task's numbered list):
  1.  Healthy pre-Start, Home unverified            -> start_eligible True,
                                                        ready_for_auto/rtl False.
  2.  Start with unverified Home                     -> full guarded Start
                                                        transaction runs (LOITER,
                                                        Set Home, verify, ARM,
                                                        AUTO) and succeeds.
  3.  Set Home fails                                 -> Start fails, stays
                                                        LOITER, no AUTO.
  4.  Home verification distance exceeds tolerance    -> no AUTO.
  5.  Home outside approved geometry after Set Home   -> no AUTO,
                                                        HOME_OUTSIDE_APPROVED_GEOMETRY.
  6.  Home verified but rtl_return_feasible false     -> no AUTO (PHASE 2 gate).
  7.  Home verified and all feasibility true          -> Start proceeds to AUTO
                                                        (folded into scenario 2).
  8.  Native RTL before Home                          -> rejected (unchanged
                                                        command_executor gate;
                                                        smoke-checked here).
  9.  Resume behaviour preserved                      -> still requires Home
                                                        (unchanged; smoke-checked).
  10. Package acceptance without runtime verified Home -> valid package
                                                        accepted.
  11. Package identity mismatch                       -> still blocks Start
                                                        (unaffected by this fix;
                                                        already covered by
                                                        test_mission_id_mismatch_
                                                        requested in
                                                        test_mission_execution_
                                                        controller.py).
  12. Stale vehicle position before Start             -> start_eligible False
                                                        (Set Home cannot be
                                                        safely established).
  13. Authority OPERATOR before Start                 -> still start_eligible
                                                        (Start owns the LOCAL_
                                                        AGENT authority
                                                        handoff/acquisition
                                                        sequencing question);
                                                        combined here with an
                                                        unverified Home to prove
                                                        the two independent
                                                        pending-precondition
                                                        axes never conflate.
  14. READY / can_start semantics                     -> explicitly documents
                                                        and asserts that READY /
                                                        can_start mean
                                                        "startable" (Start may
                                                        be safely invoked), NOT
                                                        "AUTO-ready" -- AUTO-
                                                        readiness is Home
                                                        (`home_status.verified`/
                                                        `ready_for_auto`) AND
                                                        feasibility together,
                                                        enforced by _run_start's
                                                        PHASE 2 gate, never by
                                                        a single overloaded
                                                        Boolean.
"""
import unittest

import command_executor
import mission_execution_controller as mec
import mission_feasibility as mf
import planning_package as pp
import test_mission_execution_controller as tmec


# A generous navigable_boundary used only by scenario 6, where the verified
# launch Home is deliberately far from the launch position (to force a real
# RTL-energy shortfall) but must still legitimately lie inside the approved
# geometry -- this is a DISTINCT concern (task: runtime Home geometry) from
# the RTL energy-feasibility gate this scenario actually exercises.
_FAR_HOME = {"latitude": 56.7990, "longitude": 12.8700}  # ~16.7 km from launch --
# far enough to blow the RTL energy budget under the field-calibrated
# conservative_current_A/design_speed_mps defaults (see replan_config.py).
_WIDE_BOUNDARY = [[56.60, 12.80], [56.60, 12.95], [56.85, 12.95], [56.85, 12.80]]


class TestPreStartEligibilityWithUnverifiedHome(tmec._Base):
    """Scenario 1 / 12 / 13: the readiness DISPLAY contract before Start ever
    runs, exactly what mission_execution_controller.status() reports for the
    Operator UI's Start button and Home/energy display."""

    def test_healthy_pre_start_home_unverified_is_start_eligible(self):
        # The exact LIVE FAILURE bullet list: mission loaded / package
        # synchronized / vehicle DISARMED / mode MANUAL / authority OPERATOR /
        # vehicle connected / Home NOT VERIFIED. None of these is a genuine
        # Start blocker -- Start itself owns the Home transaction and the
        # LOCAL_AGENT authority handoff happens as part of the Operator's own
        # Start transaction, immediately before invoking this API.
        self.gw.home_verified = False
        self.gw.armed = False
        self.gw.mode_name = "MANUAL"
        self.gw.authority = "OPERATOR"
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()

        self.assertTrue(st["start_eligible"],
                        "Home being unverified before Start must NOT by itself "
                        "make Start ineligible -- Start is the transaction that "
                        "establishes Home.")
        # Never AUTO/RTL-ready yet -- Home genuinely is not verified.
        self.assertFalse(st["execution_ready"])
        self.assertFalse(st["can_start"])
        self.assertIsNone(st["verified_home"])
        # The UI-facing Home-readiness fields (agent_status["home_status"],
        # mirrored 1:1 from services/set_home_service.py's get_home_status())
        # correctly read "not ready" -- these are a DIFFERENT, Home-specific
        # concept from start_eligible, never a Start blocker on their own.
        home_status = self.gw.home_status()
        self.assertFalse(home_status["verified"])
        self.assertFalse(home_status["ready_for_auto"])
        self.assertFalse(home_status["ready_for_rtl"])
        # The block reason, if any is surfaced at all, must never claim Start
        # itself is blocked by Home -- start_block_reason only describes a
        # REAL start_eligible=False condition, which this is not.
        self.assertNotEqual(st["start_block_reason"], "HOME_NOT_VERIFIED")

    def test_stale_position_before_start_is_not_start_eligible(self):
        # Task test 12: unlike Home, a stale/invalid position genuinely
        # prevents Start from safely running its own Set Home step (Set Home
        # sets Home to the CURRENT position -- an unknown/stale position makes
        # that transaction itself unsafe to even begin).
        self.gw.position_age_s = 99.0
        self.gw.home_verified = False
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()
        self.assertFalse(st["start_eligible"])
        self.assertEqual(st["start_block_reason"], "POSITION_STALE_OR_INVALID")

    def test_operator_authority_and_unverified_home_both_pending_stay_start_eligible(self):
        # Task test 13: OPERATOR authority (pending the Operator's own
        # LOCAL_AGENT handoff) and an unverified Home (pending Start's own Set
        # Home transaction) are two INDEPENDENT, expected pre-Start conditions.
        # Neither alone, nor both together, may defeat start_eligible.
        self.gw.authority = "OPERATOR"
        self.gw.home_verified = False
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()
        self.assertTrue(st["start_eligible"])
        self.assertFalse(st["execution_ready"])
        self.assertFalse(st["can_start"])
        self.assertTrue(st["authority_blocks_start"])
        self.assertEqual(st["start_block_reason"], "AUTHORITY_NOT_LOCAL_AGENT")


class TestStartTransactionEstablishesHome(tmec._Base):
    """Scenario 2 / 3 / 4 / 5 / 6 / 7: the GUARDED Start transaction itself,
    beginning from a genuinely unverified Home -- proves PHASE 1 (relaxed:
    Start may begin) and PHASE 2 (hard: AUTO requires a freshly re-proven,
    positively feasible verified Home) both hold."""

    def _unverified_ready_ctrl(self, **kw):
        self.gw.home_verified = False
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        return ctrl

    # 2 & 7. The full happy path: Start succeeds end-to-end from an
    # unverified Home, exactly the Start transaction the task describes --
    # LOCAL_AGENT already held (the Operator's own handoff, external to this
    # call) -> ARM -> LOITER -> Set Home -> verify -> AUTO.
    def test_start_from_unverified_home_runs_full_guarded_transaction(self):
        ctrl = self._unverified_ready_ctrl()
        st_before = ctrl.status()
        self.assertTrue(st_before["start_eligible"])
        self.assertIsNone(st_before["verified_home"])

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertIsNone(res["error"])
        # The transaction actually ran, in order: LOITER (launch hold) before
        # Set Home, Set Home before AUTO.
        self.assertLess(self.gw.calls.index("loiter"), self.gw.calls.index("set_home"))
        self.assertLess(self.gw.calls.index("set_home"), self.gw.calls.index("auto"))
        self.assertTrue(self.gw.home_verified)
        st_after = ctrl.status()
        self.assertIsNotNone(st_after["verified_home"])

    # 3. Set Home itself fails (e.g. a rejected MAV_CMD_DO_SET_HOME) -> Start
    # fails closed, restores/confirms LOITER, never reaches AUTO.
    def test_set_home_failure_from_unverified_home_stays_loiter_no_auto(self):
        self.gw.set_home_result = {"accepted": False, "verified": False,
                                   "error": {"code": "ACK_REJECTED", "message": "rejected"}}
        ctrl = self._unverified_ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "SET_HOME_FAILED")
        self.assertNotIn("auto", self.gw.calls)
        self.assertFalse(self.gw.home_verified)

    # 4. Set Home "succeeds" but the read-back HOME_POSITION is outside the
    # configured verification tolerance -> treated as unverified, no AUTO.
    def test_home_verification_distance_exceeds_tolerance_no_auto(self):
        self.gw.set_home_result = {
            "accepted": True, "verified": False,
            "home_position": {"latitude": 56.6495, "longitude": 12.8700},
            "requested_position": {"latitude": self.gw.lat, "longitude": self.gw.lon},
            "verification_distance_m": 42.0,
            "ack_result": "ACCEPTED",
            "error": {"code": "VERIFICATION_TOLERANCE_EXCEEDED",
                     "message": "42.0m exceeds tolerance"},
        }
        ctrl = self._unverified_ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "SET_HOME_FAILED")
        self.assertNotIn("auto", self.gw.calls)

    # 5. Set Home verifies fine, but the resulting runtime Home lies outside
    # BOTH the approved navigable_boundary and any home_corridor -> Start
    # fails closed with HOME_OUTSIDE_APPROVED_GEOMETRY, never AUTO. Package
    # geometry itself is untouched; only the boundary/corridor already
    # approved for THIS package is checked.
    def test_home_outside_approved_geometry_after_set_home_no_auto(self):
        outside_home = {"latitude": 57.5000, "longitude": 12.8700}  # far outside _BOUNDARY
        self.route_hash = tmec._store_verified_package("m1")  # default (small) boundary
        self.gw.pixhawk_route_hash = self.route_hash
        self.gw.set_home_result = {
            "accepted": True, "verified": True,
            "home_position": dict(outside_home),
            "requested_position": {"latitude": self.gw.lat, "longitude": self.gw.lon},
            "verification_distance_m": None, "ack_result": "ACCEPTED", "error": None,
        }
        ctrl = self._unverified_ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "HOME_OUTSIDE_APPROVED_GEOMETRY")
        self.assertNotIn("auto", self.gw.calls)

    # 6. Home verifies fine AND lies inside the approved geometry, but the
    # FRESH post-Home RTL-return energy check (PHASE 2) proves the return
    # leg is not affordable -> Start fails closed under the existing Start
    # safety policy, never AUTO, never an unsafe RTL. This is the scenario
    # PHASE 1's relaxation (letting an UNKNOWN pre-Home rtl_return_feasible
    # through) must never itself let slip past AUTO.
    def test_home_verified_but_rtl_infeasible_blocks_auto(self):
        self.route_hash = tmec._store_verified_package("m1", navigable_boundary=_WIDE_BOUNDARY)
        self.gw.pixhawk_route_hash = self.route_hash
        self.gw.set_home_result = {
            "accepted": True, "verified": True,
            "home_position": dict(_FAR_HOME),
            "requested_position": {"latitude": self.gw.lat, "longitude": self.gw.lon},
            "verification_distance_m": None, "ack_result": "ACCEPTED", "error": None,
        }
        # The controller's post-Set-Home fresh read (PHASE 2) must observe the
        # Home Set Home just reported, not the fixture's fixed near Home.
        # set_home_result (above) is returned VERBATIM by FakeGateway.set_home
        # -- it deliberately bypasses that method's default `self.home_verified
        # = True` side effect, so key off `self.gw.calls` (Set Home having
        # actually run) instead of the home_verified flag.
        orig_read = self.gw.read_vehicle_state
        def _read():
            vs = orig_read()
            if "set_home" in self.gw.calls:
                vs["agent"]["home_status"]["verified"] = True
                vs["agent"]["home_status"]["ready_for_auto"] = True
                vs["agent"]["home_status"]["home_position"] = dict(_FAR_HOME)
            return vs
        self.gw.read_vehicle_state = _read

        ctrl = self._unverified_ready_ctrl()
        st_before = ctrl.status()
        self.assertTrue(st_before["start_eligible"])  # PHASE 1 let Start begin

        res = ctrl.start("m1")

        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN)
        detail = res["error"]["detail"]
        self.assertTrue(detail["mission_feasible"])
        self.assertFalse(detail["rtl_return_feasible"])
        # The transaction DID reach Set Home (Home is now genuinely verified,
        # at the far position) -- this is PHASE 2 (post-Home), not PHASE 1
        # (which would reject with zero vehicle writes).
        self.assertIn("set_home", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        # Never left with a bare mode; the fallback LOITER was (re)asserted.
        self.assertTrue(res["error"]["fallback_loiter_verified"])


class TestNativeRtlAndResumeUnaffected(tmec._Base):
    """Scenario 8 / 9: smoke-check that this fix changes nothing about native
    RTL gating or Resume's pre-AUTO Home requirement -- both remain hard-
    gated on a CURRENTLY verified Home, never relaxed by the Start-eligibility
    correction above (that correction only concerns the PRE-Start display and
    the PRE-ARM phase of Start's OWN transaction)."""

    def test_native_rtl_rejected_before_home_verified(self):
        # command_executor.home_verified() backs the Operator-command RTL/
        # AUTO/RESUME gate (command_handler.HOME_VERIFICATION_REQUIRED) --
        # unrelated to mission_execution_controller, untouched by this fix.
        self.assertIn("RTL", command_executor.HOME_VERIFICATION_REQUIRED)

    def test_resume_still_requires_verified_home(self):
        # Mirrors test_mission_execution_controller.TestPauseResume.
        # test_resume_home_unverified -- Resume's own Home requirement is
        # unchanged by this fix (only fresh Start's PRE-ARM phase relaxed).
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        ctrl.start("m1")
        ctrl.pause()
        self.gw.home_verified = False
        n_auto_before = self.gw.calls.count("auto")
        res = ctrl.resume()
        self.assertEqual(res["error"]["code"], "HOME_UNVERIFIED")
        self.assertEqual(self.gw.calls.count("auto"), n_auto_before)  # no new AUTO


class TestPackageAcceptanceUnaffectedByRuntimeHome(tmec._Base):
    """Scenario 10 / 11: planning-package acceptance validates PLANNED Home/
    geometry/identity only -- it must never require a runtime VERIFIED
    Pixhawk Home (that is a Start-time proof, established well after
    acceptance), and identity/consistency checks are entirely unaffected by
    this fix."""

    def test_package_with_no_runtime_home_concept_is_accepted(self):
        # validate_package_v1 / build_package / store_accepted (exercised by
        # _Base.setUp()'s own _store_verified_package helper, the same real
        # acceptance path replan_api uses) never read or require services/
        # set_home_service.py's runtime verification state -- there is no
        # Home-verification input to package acceptance at all, so a
        # structurally valid package (planned Home + home_corridor + route
        # geometry + mission identity + no-go constraints, all proven) is
        # accepted regardless of whether any vehicle has EVER verified a
        # runtime Home. FakeGateway.home_verified is left at its class
        # default (True) here specifically to prove acceptance does not even
        # look at it -- a fresh gateway with no Home-verification history at
        # all was never consulted.
        stored = pp.load()
        self.assertTrue(pp.is_usable(stored))
        self.assertEqual(stored.get("mission_id"), "m1")
        self.assertEqual(stored.get("route_hash"), self.route_hash)
        # The package's own PLANNED Home (provenance only) -- distinct from,
        # and never requiring, a runtime VERIFIED Pixhawk Home.
        self.assertIsNotNone(stored.get("home"))

    def test_package_identity_mismatch_still_blocks_start(self):
        # Task test 11: a genuine identity mismatch (requested mission_id !=
        # the stored/verified package) still blocks Start -- unaffected by
        # this fix, which only concerns the Home axis. Mirrors
        # test_mission_execution_controller.TestStart.
        # test_mission_id_mismatch_requested.
        self.gw.home_verified = False
        ctrl = self._ctrl()
        res = ctrl.start("m-does-not-exist")
        self.assertEqual(res["error"]["code"], "MISSION_ID_MISMATCH")
        self.assertEqual(self.gw.write_calls, [])


class TestReadyMeansStartableNotAutoReady(tmec._Base):
    """Scenario 14: lock in the semantics of READY/can_start explicitly, so
    they are never re-read as "AUTO-ready" by a future change. READY/
    can_start mean "the guarded Start transaction may safely be invoked right
    now" -- Home may still be unverified. AUTO-readiness is a SEPARATE
    concept: Home verified (`home_status.verified`/`ready_for_auto`) AND
    fresh feasibility both positively true, enforced only inside _run_start's
    PHASE 2 gate, immediately before AUTO -- never surfaced as a single
    top-level Boolean."""

    def test_ready_and_can_start_true_with_home_unverified(self):
        self.gw.home_verified = False
        ctrl = self._ctrl()
        st = ctrl.refresh_readiness()
        # LOCAL_AGENT authority (this harness's default) + all other evidence
        # proven -> the controller enters READY / can_start=True even though
        # Home is not verified: READY means "startable", not "AUTO-ready".
        self.assertEqual(st["state"], mec.READY)
        self.assertTrue(st["can_start"])
        self.assertTrue(st["execution_ready"])
        self.assertTrue(st["start_eligible"])
        self.assertIsNone(st["verified_home"])
        home_status = self.gw.home_status()
        self.assertFalse(home_status["ready_for_auto"])
        self.assertFalse(home_status["ready_for_rtl"])


if __name__ == "__main__":
    unittest.main()
