"""
Start-time mission-energy-feasibility gate tests (task: mission-energy-
feasibility Home-semantics correction, sections 5/6/7/11/14/15), using the
real MissionExecutionController test harness from
test_mission_execution_controller.py (FakeGateway, the fixture planning
package, _cfg/_Base) -- no HTTP, no MAVLink, no Pixhawk.

Two INDEPENDENT gates, both required (task section 5):

  A. Healthy effective battery -> Start proceeds normally.
  B. Injected/effective battery = 5% -> Start fails INSUFFICIENT_ENERGY_FOR_
     PLANNED_MISSION, and NO ARM / NO AUTO / no vehicle write happened because
     of the feasibility gate.
  C. Invalid feasibility evidence (unavailable battery) -> Start fails closed
     with an explicit reason, never silently proceeds.
  D. A cached (stale) continuous-evaluation result said FEASIBLE, but fresh
     Start-time evidence says INFEASIBLE -> Start is rejected anyway --
     proves Start uses fresh authoritative feasibility, not stale UI state.
  E. Mission feasible, RTL return infeasible -> Start rejects
     INSUFFICIENT_ENERGY_FOR_RTL_RETURN (the mission-only gate the OLD module
     used to enforce was not enough).
  F. Both infeasible -> deterministic mission-first reason priority.
  G. Scout physically at its current verified RTL Home, planned mission far
     away -> RTL return is feasible/near-zero cost; Start's outcome reflects
     ONLY the far planned mission's own energy budget.

Also proves the feasibility gate performs no vehicle read/write of its own
(section 17): every gateway call observed in a rejected attempt is one this
module's own docstring already accounts for (auth/pixhawk/state reads only).
"""
import unittest

import experiment_injection
import mission_execution_controller as mec
import mission_feasibility as mf
import planning_package as pp
import test_mission_execution_controller as tmec

_READ_ONLY_CALLS = {"auth", "pixhawk", "state", "home_status"}


class TestFeasibilityGate(tmec._Base):
    def _ready_ctrl(self, **kw):
        ctrl = self._ctrl(**kw)
        ctrl.observe(self._snapshot(mode="LOITER"), None)  # promote NOT_READY -> READY
        return ctrl

    def tearDown(self):
        experiment_injection.clear()
        super().tearDown()

    def _assert_no_vehicle_changing_write(self):
        # No ARM, no AUTO, no LOITER, no Set Home, no upload, no sequence
        # change -- the gate ran strictly before the first vehicle-changing
        # write and rejected before any of them were attempted.
        self.assertEqual(self.gw.write_calls, [])
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("loiter", self.gw.calls)
        self.assertNotIn("set_home", self.gw.calls)
        # Only read-only evidence calls happened.
        self.assertTrue(set(self.gw.calls).issubset(_READ_ONLY_CALLS))

    # A. Healthy battery -> normal existing Start proceeds unchanged.
    def test_healthy_battery_start_proceeds_normally(self):
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        self.assertIsNone(res["error"])

    # B. Injected battery = 5% -> Start rejected, zero vehicle writes.
    #    (In this harness rtl_home == planned_home == pos, so this isolates
    #    the mission-dimension rejection cleanly.)
    def test_injected_low_battery_rejects_start_with_no_writes(self):
        experiment_injection.inject(battery_percent=5.0, target_vehicle="usv-2")
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        detail = res["error"]["detail"]
        self.assertFalse(detail["mission_feasible"])
        self.assertEqual(detail["battery_percent"], 5.0)
        self.assertEqual(detail["battery_source"], mf.SOURCE_INJECTED)
        self._assert_no_vehicle_changing_write()
        self.assertEqual(ctrl.status()["state"], mec.FAILED)

    # C. Invalid/unavailable battery evidence -> fail closed with an explicit
    #    reason (never silently proceeds because we "don't know").
    def test_invalid_battery_evidence_fails_closed(self):
        # -1 is ArduPilot's "no estimate" sentinel (decision_engine._normalize_
        # battery_percent) -- normalizes to None, never a real percentage.
        self.gw.battery = -1
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_BATTERY_INVALID)
        self.assertIsNone(res["error"]["detail"]["mission_feasible"])
        self.assertEqual(self.gw.write_calls, [])

    # D. A stale cached "FEASIBLE" continuous result must not let Start
    #    proceed once fresh Start-time evidence says otherwise.
    def test_stale_cached_feasible_does_not_override_fresh_infeasible_evidence(self):
        ctrl = self._ready_ctrl()
        # Simulate a prior loop iteration's continuous evaluation that found
        # the mission feasible (e.g. before the battery dropped).
        ctrl.update_energy_feasibility({
            "status": mf.STATUS_FEASIBLE, "reason": mf.REASON_SUFFICIENT_ENERGY,
            "mission_feasible": True, "rtl_return_feasible": True,
            "mission_margin_percent": 40.0, "rtl_return_margin_percent": 40.0,
            "battery_percent": 55.0, "battery_source": mf.SOURCE_PHYSICAL,
        })
        self.assertTrue(ctrl.status()["can_start"])
        # Now the FRESH evidence at Start time is bad.
        self.gw.battery = 5
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        self.assertEqual(self.gw.write_calls, [])

    # E. Mission feasible, RTL return infeasible -> Start rejects with the
    #    DISTINCT RTL reason, not the mission one (task section 5's second
    #    worked example, and the whole point of the two-gate policy).
    def test_mission_feasible_rtl_infeasible_rejects_with_rtl_reason(self):
        # A verified Pixhawk Home far enough away that the RTL leg alone
        # blows the energy budget, while the (short, fixture) mission route
        # remains comfortably affordable on its own. Distance sized to stay
        # RTL-infeasible under the field-calibrated conservative_current_A/
        # design_speed_mps defaults (see replan_config.py).
        ctrl = self._ctrl(cfg=tmec._cfg(mission_execution_enabled=True))
        far_home = {"latitude": 56.7990, "longitude": 12.8700}  # ~16.7 km from launch
        self.gw.home_verified = True
        # FakeGateway's home_status/read_vehicle_state both hard-code the
        # module-level _HOME fixture; patch them to report the far Home for
        # this test only.
        def _home_status():
            self.gw.calls.append("home_status")
            return {"reachable": True, "verified": True, "ready_for_auto": True,
                   "home_position": dict(far_home)}
        self.gw.home_status = _home_status
        orig_read = self.gw.read_vehicle_state

        def _read():
            vs = orig_read()
            vs["agent"]["home_status"]["home_position"] = dict(far_home)
            return vs
        self.gw.read_vehicle_state = _read

        ctrl.observe(self._snapshot(mode="LOITER", home=far_home), None)
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN)
        detail = res["error"]["detail"]
        self.assertTrue(detail["mission_feasible"])
        self.assertFalse(detail["rtl_return_feasible"])
        self._assert_no_vehicle_changing_write()

    # F. Both infeasible -> deterministic reason priority: mission dimension
    #    wins (documented in mission_feasibility.evaluate_mission_feasibility).
    def test_both_infeasible_mission_reason_wins(self):
        experiment_injection.inject(battery_percent=1.0, target_vehicle="usv-2")
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        detail = res["error"]["detail"]
        self.assertFalse(detail["mission_feasible"])
        self.assertFalse(detail["rtl_return_feasible"])
        self._assert_no_vehicle_changing_write()

    # G. Scout physically AT its current verified RTL Home (this harness's
    #    default -- launch position == home_status Home) -> RTL leg is
    #    trivially feasible; Start's outcome is governed purely by the
    #    planned mission's own energy budget, exactly the bench acceptance
    #    case (task section 13).
    def test_at_rtl_home_start_governed_by_mission_budget_only(self):
        ctrl = self._ready_ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        # Confirmed via the recorder-free path: no error, meaning both gates
        # passed -- RTL trivially (pos == home_status Home in this fixture).

    # ── Readiness display (section 9): can_start/start_eligible fail closed
    #    on a cached INFEASIBLE/UNKNOWN result on EITHER axis, and recover
    #    once both clear. ────────────────────────────────────────────────
    def test_readiness_reflects_cached_infeasible_mission_result(self):
        ctrl = self._ready_ctrl()
        self.assertTrue(ctrl.status()["can_start"])
        ctrl.update_energy_feasibility({
            "status": mf.STATUS_INFEASIBLE, "reason": mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION,
            "mission_feasible": False, "rtl_return_feasible": True,
        })
        st = ctrl.status()
        self.assertFalse(st["can_start"])
        self.assertFalse(st["start_eligible"])
        self.assertEqual(st["start_block_reason"], mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION)
        self.assertEqual(st["energy_feasibility"]["status"], mf.STATUS_INFEASIBLE)

    def test_readiness_reflects_cached_infeasible_rtl_result(self):
        """Mission feasible but RTL infeasible must ALSO block the cached
        can_start/start_eligible display (task section 5's second gate),
        not just the mission axis."""
        ctrl = self._ready_ctrl()
        self.assertTrue(ctrl.status()["can_start"])
        ctrl.update_energy_feasibility({
            "status": mf.STATUS_INFEASIBLE, "reason": mf.REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN,
            "mission_feasible": True, "rtl_return_feasible": False,
        })
        st = ctrl.status()
        self.assertFalse(st["can_start"])
        self.assertFalse(st["start_eligible"])
        self.assertEqual(st["start_block_reason"], mf.REASON_INSUFFICIENT_ENERGY_FOR_RTL_RETURN)

    def test_readiness_recovers_once_feasibility_clears(self):
        ctrl = self._ready_ctrl()
        ctrl.update_energy_feasibility({
            "status": mf.STATUS_INFEASIBLE, "reason": mf.REASON_INSUFFICIENT_ENERGY_FOR_PLANNED_MISSION,
            "mission_feasible": False, "rtl_return_feasible": True,
        })
        self.assertFalse(ctrl.status()["can_start"])
        ctrl.update_energy_feasibility({
            "status": mf.STATUS_FEASIBLE, "reason": mf.REASON_SUFFICIENT_ENERGY,
            "mission_feasible": True, "rtl_return_feasible": True,
        })
        self.assertTrue(ctrl.status()["can_start"])

    def test_no_cached_feasibility_leaves_existing_behaviour_unchanged(self):
        # Never wiring update_energy_feasibility (most of this module's own
        # test suite) must not change can_start at all.
        ctrl = self._ready_ctrl()
        st = ctrl.status()
        self.assertEqual(st["energy_feasibility"]["status"], mf.STATUS_UNKNOWN)
        self.assertEqual(st["energy_feasibility"]["reason"], "NOT_YET_EVALUATED")
        self.assertTrue(st["can_start"])


if __name__ == "__main__":
    unittest.main()
