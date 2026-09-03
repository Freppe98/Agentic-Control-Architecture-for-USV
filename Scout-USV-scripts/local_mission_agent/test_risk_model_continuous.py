"""
Continuous risk evaluation wired into the mission-execution controller (task
sections 18/19/27): proves the SAME evaluation local_agent.py's main loop
runs every iteration (risk_model.evaluate_from_agent_state, exactly as wired
in local_agent.py) produces an updated result as evidence changes, is pushed
into the controller via update_risk_assessment() the same way energy_
feasibility already is, and is visible on GET /agent/mission_execution/status
(mission_execution_controller.status()["risk"]) -- without requiring Start,
and without issuing any vehicle action.

    python3 test_risk_model_continuous.py
"""
import unittest

import mission_execution_controller as mec
import risk_config
import risk_model as rm


_ME_STATUS_RUNNING = {"supported": True, "state": "RUNNING", "binding": {"binding_state": "BOUND"}}
_REPLAN_MONITORING = {"fsm_state": "MONITORING"}
_NAV_HEALTHY = {
    "gps": {"fix_type": {"value": 3, "state": "FRESH", "age_s": 0.2}},
    "ekf": {"value": True, "state": "FRESH", "age_s": 0.2},
    "position": {"state": "FRESH", "age_s": 0.2},
}


def _feasible(mission_margin=50.0, rtl_margin=50.0, mission_feasible=True, rtl_feasible=True):
    return {
        "status": "FEASIBLE", "mission_feasible": mission_feasible, "rtl_return_feasible": rtl_feasible,
        "mission_margin_percent": mission_margin, "rtl_return_margin_percent": rtl_margin,
        "battery_percent": 80.0, "battery_source": "PHYSICAL",
        "physical_battery_percent": 80.0, "injected_battery_percent": None,
    }


class TestStatusBeforeAnyEvaluation(unittest.TestCase):
    def test_never_evaluated_is_explicit_not_a_fabricated_low(self):
        ctrl = mec.MissionExecutionController(gateway=None)
        risk_block = ctrl.status()["risk"]
        self.assertIsNone(risk_block["score"])
        self.assertEqual(risk_block["dominant_reason"], "NOT_YET_EVALUATED")
        self.assertNotEqual(risk_block["level"], rm.LEVEL_LOW)


class TestContinuousRisk(unittest.TestCase):
    def setUp(self):
        self.cfg = risk_config.RiskConfig()
        self.ctrl = mec.MissionExecutionController(gateway=None)  # never called below

    def _evaluate_and_push(self, *, comm_state, control_authority, feasibility, nav_evidence,
                           failsafe=None, imu=None):
        result = rm.evaluate_risk(
            feasibility=feasibility, comm_state=comm_state, control_authority=control_authority,
            mission_execution_status=self.ctrl.status(), replan_status=_REPLAN_MONITORING,
            navigation_evidence=nav_evidence, failsafe=failsafe or {"status": "OK"},
            imu=imu or {"imu_health": "OK"}, cfg=self.cfg,
        )
        self.ctrl.update_risk_assessment(result.to_dict())
        return result

    def test_result_updates_as_evidence_changes_across_iterations(self):
        r1 = self._evaluate_and_push(comm_state="CONNECTED", control_authority="LOCAL_AGENT",
                                     feasibility=_feasible(), nav_evidence=_NAV_HEALTHY)
        self.assertEqual(self.ctrl.status()["risk"]["level"], rm.LEVEL_LOW)

        # Communication degrades on the very next evaluation -- no vehicle
        # action taken to produce this, no Start required.
        r2 = self._evaluate_and_push(comm_state="DISCONNECTED", control_authority="OPERATOR",
                                     feasibility=_feasible(), nav_evidence=_NAV_HEALTHY)
        st = self.ctrl.status()
        self.assertGreater(st["risk"]["components"]["communication"]["score"], 0.0)
        self.assertNotEqual(r1.score, r2.score)

    def test_energy_state_change_updates_risk_on_next_cycle(self):
        self._evaluate_and_push(comm_state="CONNECTED", control_authority="LOCAL_AGENT",
                                feasibility=_feasible(), nav_evidence=_NAV_HEALTHY)
        before = self.ctrl.status()["risk"]["level"]
        self.assertEqual(before, rm.LEVEL_LOW)

        # Battery injection collapses mission feasibility -- feasibility.py's
        # own contract (mission_feasible False) -- risk must go CRITICAL with
        # hard_constraint_violated true on the very next evaluation.
        self._evaluate_and_push(comm_state="CONNECTED", control_authority="LOCAL_AGENT",
                                feasibility=_feasible(mission_margin=-5.0, mission_feasible=False),
                                nav_evidence=_NAV_HEALTHY)
        after = self.ctrl.status()["risk"]
        self.assertEqual(after["level"], rm.LEVEL_CRITICAL)
        self.assertTrue(after["hard_constraint_violated"])

    def test_status_exposes_full_contract(self):
        self._evaluate_and_push(comm_state="CONNECTED", control_authority="LOCAL_AGENT",
                                feasibility=_feasible(), nav_evidence=_NAV_HEALTHY)
        risk_block = self.ctrl.status()["risk"]
        for key in ("score", "level", "components", "weights", "dominant_component",
                   "dominant_reason", "hard_constraint_violated", "confidence",
                   "evaluated_at", "recommendation",
                   # Aggregate-semantics correction task section 5/19 --
                   # governing-vs-weighted distinction, always present.
                   "weighted_score", "weighted_level", "component_floor_level",
                   "component_floor_reason", "component_floor_source",
                   "hard_override_level"):
            self.assertIn(key, risk_block)
        for name in ("energy", "communication", "navigation", "health", "mission"):
            self.assertIn(name, risk_block["components"])

    def test_status_surfaces_governing_level_over_weighted_level(self):
        # The exact regression this task fixes, observed through the SAME
        # status() surface GET /agent/mission_execution/status returns
        # (task section 19/21's DISCONNECTED API example).
        self._evaluate_and_push(
            comm_state="DISCONNECTED", control_authority="OPERATOR",
            feasibility=_feasible(), nav_evidence=_NAV_HEALTHY)
        risk_block = self.ctrl.status()["risk"]
        self.assertAlmostEqual(risk_block["score"], 0.2375)
        self.assertEqual(risk_block["weighted_level"], rm.LEVEL_LOW)
        self.assertEqual(risk_block["component_floor_level"], rm.LEVEL_HIGH)
        self.assertEqual(risk_block["level"], rm.LEVEL_HIGH)
        self.assertNotEqual(risk_block["level"], rm.LEVEL_LOW)

    def test_risk_never_affects_can_start_or_can_pause(self):
        """Task section 18/21: risk is observational only in this task --
        gates nothing. can_start/can_pause/can_resume are computed entirely
        independently of self._risk."""
        before = self.ctrl.status()
        self._evaluate_and_push(comm_state="DISCONNECTED", control_authority="OPERATOR",
                                feasibility=_feasible(mission_margin=-5.0, mission_feasible=False),
                                nav_evidence={})
        after = self.ctrl.status()
        self.assertEqual(before["can_start"], after["can_start"])
        self.assertEqual(before["can_pause"], after["can_pause"])
        self.assertEqual(before["can_resume"], after["can_resume"])


if __name__ == "__main__":
    unittest.main()
