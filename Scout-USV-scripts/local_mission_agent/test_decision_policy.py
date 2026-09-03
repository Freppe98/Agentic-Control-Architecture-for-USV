"""
Unit tests for decision_policy.py -- the authoritative decision policy that
maps risk_model.RiskResult.recommendation to an ActionRequest consumed by
replan_controller.observe() (E2 water-trial integration task).

    python3 test_decision_policy.py
"""
import unittest
from types import SimpleNamespace

import decision_policy as dp
import risk_model


def _risk(recommendation, level=risk_model.LEVEL_LOW, dominant_reason="NOMINAL",
         hard_constraint_violated=False, evaluated_at=1000.0):
    return SimpleNamespace(
        recommendation=recommendation,
        level=level,
        dominant_reason=dominant_reason,
        hard_constraint_violated=hard_constraint_violated,
        evaluated_at=evaluated_at,
    )


def _feasibility(mission_feasible=True, rtl_return_feasible=True, status="FEASIBLE"):
    return SimpleNamespace(
        mission_feasible=mission_feasible,
        rtl_return_feasible=rtl_return_feasible,
        status=status,
    )


def _snapshot(snapshot_id="snap-1"):
    return SimpleNamespace(snapshot_id=snapshot_id)


class TestActionMapping(unittest.TestCase):
    def setUp(self):
        self.policy = dp.DecisionPolicy()

    def test_continue_maps_to_none(self):
        req = self.policy.evaluate(_risk(risk_model.RECOMMEND_CONTINUE), _feasibility(), _snapshot())
        self.assertEqual(req.action, dp.ACTION_NONE)

    def test_continue_with_caution_maps_to_none(self):
        req = self.policy.evaluate(
            _risk(risk_model.RECOMMEND_CONTINUE_WITH_CAUTION, level=risk_model.LEVEL_ELEVATED),
            _feasibility(), _snapshot())
        self.assertEqual(req.action, dp.ACTION_NONE)

    def test_return_home_maps_to_request_return_home(self):
        req = self.policy.evaluate(
            _risk(risk_model.RECOMMEND_RETURN, level=risk_model.LEVEL_CRITICAL),
            _feasibility(mission_feasible=False, rtl_return_feasible=True), _snapshot())
        self.assertEqual(req.action, dp.ACTION_REQUEST_RETURN_HOME)

    def test_hold_maps_to_request_hold(self):
        req = self.policy.evaluate(
            _risk(risk_model.RECOMMEND_HOLD, level=risk_model.LEVEL_CRITICAL),
            _feasibility(mission_feasible=False, rtl_return_feasible=False), _snapshot())
        self.assertEqual(req.action, dp.ACTION_REQUEST_HOLD)


class TestActionRequestContract(unittest.TestCase):
    def setUp(self):
        self.policy = dp.DecisionPolicy()

    def test_carries_source_snapshot_reason_codes_level_recommendation_evidence(self):
        risk = _risk(risk_model.RECOMMEND_RETURN, level=risk_model.LEVEL_CRITICAL,
                     dominant_reason="ENERGY_HARD_MISSION_INFEASIBLE",
                     hard_constraint_violated=True, evaluated_at=1234.5)
        feas = _feasibility(mission_feasible=False, rtl_return_feasible=True, status="INFEASIBLE")
        req = self.policy.evaluate(risk, feas, _snapshot("snap-42"), now=1234.5)

        self.assertEqual(req.source_snapshot_id, "snap-42")
        self.assertEqual(req.risk_level, risk_model.LEVEL_CRITICAL)
        self.assertEqual(req.recommendation, risk_model.RECOMMEND_RETURN)
        self.assertEqual(req.feasibility_evidence,
                         {"mission_feasible": False, "rtl_return_feasible": True, "status": "INFEASIBLE"})
        self.assertIn(risk_model.LEVEL_CRITICAL, req.reason_codes)
        self.assertIn("ENERGY_HARD_MISSION_INFEASIBLE", req.reason_codes)
        self.assertIn("HARD_CONSTRAINT_VIOLATED", req.reason_codes)
        self.assertIn("RTL_RETURN_FEASIBLE", req.reason_codes)
        self.assertEqual(req.created_at, 1234.5)

    def test_to_dict_is_json_serializable_shape(self):
        req = self.policy.evaluate(_risk(risk_model.RECOMMEND_CONTINUE), _feasibility(), _snapshot())
        d = req.to_dict()
        self.assertEqual(set(d.keys()), {
            "action", "source_snapshot_id", "reason_codes", "risk_level",
            "recommendation", "feasibility_evidence", "generation", "created_at",
        })
        self.assertIsInstance(d["reason_codes"], list)


class TestGenerationEdgeDetection(unittest.TestCase):
    """Observability-only edge detection (module docstring): generation
    increments only on a NONE -> non-NONE transition, mirroring
    replan_controller.py's own trigger-generation pattern."""

    def setUp(self):
        self.policy = dp.DecisionPolicy()

    def test_repeated_same_recommendation_keeps_same_generation(self):
        risk = _risk(risk_model.RECOMMEND_HOLD, level=risk_model.LEVEL_CRITICAL)
        feas = _feasibility(mission_feasible=False, rtl_return_feasible=False)
        first = self.policy.evaluate(risk, feas, _snapshot())
        second = self.policy.evaluate(risk, feas, _snapshot())
        third = self.policy.evaluate(risk, feas, _snapshot())
        self.assertEqual(first.generation, second.generation)
        self.assertEqual(second.generation, third.generation)
        self.assertGreater(first.generation, 0)

    def test_clearing_then_retriggering_creates_new_generation(self):
        risk_active = _risk(risk_model.RECOMMEND_RETURN, level=risk_model.LEVEL_CRITICAL)
        feas_active = _feasibility(mission_feasible=False, rtl_return_feasible=True)
        first = self.policy.evaluate(risk_active, feas_active, _snapshot())

        cleared = self.policy.evaluate(_risk(risk_model.RECOMMEND_CONTINUE), _feasibility(), _snapshot())
        self.assertEqual(cleared.action, dp.ACTION_NONE)

        second = self.policy.evaluate(risk_active, feas_active, _snapshot())
        self.assertGreater(second.generation, first.generation)

    def test_transition_from_return_to_hold_without_clearing_keeps_generation(self):
        # A still-continuously-active condition that merely reclassifies from
        # RETURN_HOME to HOLD (e.g. RTL feasibility is lost) is not a NONE ->
        # non-NONE edge -- generation is observability plumbing only, not the
        # duplicate-transaction guard (that lives entirely in replan_controller).
        first = self.policy.evaluate(
            _risk(risk_model.RECOMMEND_RETURN, level=risk_model.LEVEL_CRITICAL),
            _feasibility(mission_feasible=False, rtl_return_feasible=True), _snapshot())
        second = self.policy.evaluate(
            _risk(risk_model.RECOMMEND_HOLD, level=risk_model.LEVEL_CRITICAL),
            _feasibility(mission_feasible=False, rtl_return_feasible=False), _snapshot())
        self.assertEqual(first.generation, second.generation)


class TestLatestFeasibilityEvidence(unittest.TestCase):
    """replan_controller.py's RTL-fallback feasibility_fn callback (E2
    water-trial integration task section 15) -- the latest evaluate() call's
    feasibility_evidence, exposed for the FSM to check CURRENT
    rtl_return_feasible before ever commanding RTL."""

    def test_none_before_first_evaluate(self):
        policy = dp.DecisionPolicy()
        self.assertIsNone(policy.latest_feasibility_evidence())

    def test_reflects_most_recent_evaluate_call(self):
        policy = dp.DecisionPolicy()
        policy.evaluate(_risk(risk_model.RECOMMEND_CONTINUE),
                        _feasibility(rtl_return_feasible=True), _snapshot())
        self.assertEqual(policy.latest_feasibility_evidence()["rtl_return_feasible"], True)
        policy.evaluate(_risk(risk_model.RECOMMEND_HOLD, level=risk_model.LEVEL_CRITICAL),
                        _feasibility(rtl_return_feasible=False), _snapshot())
        self.assertEqual(policy.latest_feasibility_evidence()["rtl_return_feasible"], False)


class TestNoSideEffects(unittest.TestCase):
    """decision_policy.py must never itself be able to write to the vehicle --
    mirrors risk_model.py's own static-import guard (test_risk_model.py)."""

    _FORBIDDEN_IMPORTS = (
        "api_client", "command_executor", "command_handler", "write_arbiter",
        "mission_execution_gateway", "replan_gateway", "mission_upload_worker",
        "pixhawk_mission", "autonomy_gate", "replan_controller",
    )
    _FORBIDDEN_SYMBOLS = (
        "arm", "disarm", "set_mode", "loiter", "rtl", "set_home",
        "upload_mission", "execute_replan",
    )

    def test_module_imports_no_write_capable_dependency(self):
        with open("decision_policy.py") as f:
            source = f.read()
        for name in self._FORBIDDEN_IMPORTS:
            self.assertNotIn(f"import {name}", source,
                             f"decision_policy.py must not import {name!r} (write-capable)")

    def test_module_defines_no_vehicle_write_function(self):
        for name in self._FORBIDDEN_SYMBOLS:
            self.assertFalse(hasattr(dp, name), f"decision_policy.py must not define {name!r}")

    def test_evaluate_is_pure_and_returns_new_object_each_call(self):
        policy = dp.DecisionPolicy()
        risk = _risk(risk_model.RECOMMEND_CONTINUE)
        feas = _feasibility()
        snap = _snapshot()
        r1 = policy.evaluate(risk, feas, snap, now=1.0)
        r2 = policy.evaluate(risk, feas, snap, now=1.0)
        self.assertIsNot(r1, r2)
        self.assertEqual(r1.to_dict(), r2.to_dict())


if __name__ == "__main__":
    unittest.main()
