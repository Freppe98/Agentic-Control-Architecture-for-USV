"""
Regression tests for the mission-route-identity-safety invariant (task:
mission-route-identity safety) -- the live bug where mission_feasibility.py
could confidently evaluate a STALE planning-package route even though Scout's
own readiness proof already knew that package did not match the current
Pixhawk mission:

    planning package: mission_id msn-25deb0e90e89, route_hash sha256:3733...,
                       22 waypoints, an old route ~1.6 km away
    Pixhawk:          route_hash sha256:76ae..., 24 route waypoints,
                       actual route ~486 m

Readiness already reported ROUTE_HASH_STALE / PLANNING_PACKAGE_STALE, but
mission_feasibility.py still evaluated the stale package route and published
a confident planned_completion_distance_m=2352.9 / mission_margin_percent=
-5.43 / mission_feasible=False / risk=CRITICAL / RETURN_HOME verdict --
computed from geometry that no longer described the vehicle's actual mission.
This must never happen again: see mission_feasibility.py's "ROUTE-IDENTITY
SAFETY INVARIANT" module-docstring section.

    python3 test_mission_feasibility_route_identity.py
"""
import unittest

import mission_execution_controller as mec
import mission_feasibility as mf
import planning_package as pp
import replan_config
import risk_config
import risk_model
import test_mission_execution_controller as tmec

STALE_PACKAGE_ROUTE_HASH = "sha256:" + "3733" + "0" * 60
CURRENT_PIXHAWK_ROUTE_HASH = "sha256:" + "76ae" + "0" * 60

# A stale planning-package route far from the vehicle's actual current
# position -- if evaluated, this alone would compute a large, misleading
# "planned completion distance" (the bug's own 2352.9 m figure was produced
# exactly this way: an old route evaluated as if it were still current). It
# must never be evaluated once its identity is unverified.
_STALE_OLD_ROUTE = [{"latitude": 10.02, "longitude": 10.0}]  # ~2.2 km from (10, 10)


class TestRouteIdentityPureFunction(unittest.TestCase):
    """Direct evaluate_mission_feasibility() proof: an unverified/stale route
    identity forces the mission dimension to UNKNOWN and suppresses its
    distance, no matter how the geometry itself would otherwise compute --
    while the RTL dimension, which never reads mission_route, is untouched."""

    def _evaluate(self, **kwargs):
        base = dict(
            current_position=(10.0, 10.0),
            position_age_s=1.0,
            mission_route=_STALE_OLD_ROUTE,
            current_sequence=1,
            planned_home=(10.02, 10.0),
            rtl_home=(10.0, 10.0),          # verified Pixhawk Home, ~0 m away
            physical_battery_percent=83.0,  # matches the live-bug bench battery
            injected_battery_percent=None,
            nominal_capacity_Ah=40.0,
            conservative_current_A=9.0,
            design_speed_mps=1.0,
            usable_capacity_factor=0.8,
            mission_reserve_fraction=0.15,
            rtl_reserve_fraction=0.05,
            max_position_age_s=5.0,
            now=1000.0,
        )
        base.update(kwargs)
        return mf.evaluate_mission_feasibility(**base)

    def test_stale_route_hash_mismatch_never_evaluated_as_authoritative(self):
        # Route IS present, but its identity is a PROVEN mismatch (the exact
        # live-bug shape): route_identity_verified=False with the reason a
        # real readiness proof would report.
        res = self._evaluate(route_identity_verified=False,
                             route_identity_reason=mf.REASON_PLANNING_PACKAGE_STALE)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_PLANNING_PACKAGE_STALE)
        self.assertIsNone(res.mission_feasible)
        self.assertIsNone(res.planned_completion_distance_m)
        self.assertIsNone(res.mission_margin_percent)
        self.assertIsNone(res.estimated_mission_capacity_Ah)
        self.assertFalse(res.route_identity_verified)

        # RTL is entirely independent -- a verified, near-zero-distance RTL
        # Home stays feasible even while the mission route is untrusted.
        self.assertTrue(res.rtl_return_feasible)
        self.assertIsNotNone(res.rtl_return_margin_percent)
        self.assertGreater(res.rtl_return_margin_percent, 0)

    def test_route_identity_unproven_yet_also_unknown(self):
        """No proof either way yet (e.g. the readiness machinery has not
        completed its first check) -- MUST NOT default to trusting the
        route. route_identity_verified=None (not False) still gates
        closed."""
        res = self._evaluate(route_identity_verified=None)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_MISSION_ROUTE_UNVERIFIED)
        self.assertIsNone(res.mission_feasible)
        self.assertIsNone(res.planned_completion_distance_m)
        self.assertTrue(res.rtl_return_feasible)

    def test_risk_model_does_not_hard_override_critical_on_stale_route(self):
        """The exact live-bug failure mode: a stale route's (now-suppressed)
        energy evidence must never manufacture a false ENERGY_HARD_INFEASIBLE
        / CRITICAL / RETURN_HOME verdict. mission_feasible=None (never
        False) for an unverified route means risk_model's hard-feasibility
        override never fires from this alone."""
        res = self._evaluate(route_identity_verified=False,
                             route_identity_reason=mf.REASON_PLANNING_PACKAGE_STALE)
        risk = risk_model.evaluate_risk(
            feasibility=res.to_dict(),
            comm_state="CONNECTED",
            control_authority="LOCAL_AGENT",
            mission_execution_status={"supported": True, "state": "READY"},
            replan_status={"fsm_state": "IDLE"},
            navigation_evidence={
                "gps": {"fix_type": {"state": "FRESH", "value": 3}},
                "ekf": {"state": "FRESH", "value": True},
                "position": {"state": "FRESH"},
            },
            failsafe={"status": "OK"},
            imu={"imu_health": "OK"},
            cfg=risk_config.DEFAULT,
        )
        self.assertFalse(risk.hard_constraint_violated)
        self.assertNotEqual(risk.level, risk_model.LEVEL_CRITICAL)
        self.assertNotEqual(risk.recommendation, risk_model.RECOMMEND_RETURN)


class TestRouteIdentityFromSnapshotResolution(unittest.TestCase):
    """_resolve_route_identity / evaluate_from_snapshot: proves the adapter
    resolves route identity from mission_execution_controller's own existing
    binding proof (never a second, parallel hash system)."""

    class _FakeSnapshot:
        latitude = 10.0
        longitude = 10.0
        position_age_s = 1.0
        current_sequence = 1
        home_latitude = 10.0
        home_longitude = 10.0
        home_valid = True
        battery_percent = 83.0

    class _FakeCfg:
        nominal_capacity_Ah = 40.0
        conservative_current_A = 9.0
        design_speed_mps = 1.0
        usable_capacity_factor = 0.8
        mission_reserve_fraction = 0.15
        rtl_reserve_fraction = 0.05
        max_position_age_s = 5.0

    def _package(self, route_hash):
        return {"route": _STALE_OLD_ROUTE, "route_hash": route_hash,
               "mission_id": "msn-25deb0e90e89"}

    def test_no_binding_evidence_wired_preserves_legacy_behaviour(self):
        package = self._package(STALE_PACKAGE_ROUTE_HASH)
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg())
        self.assertTrue(res.route_identity_verified)  # not wired -- unaffected

    def test_mismatched_binding_forces_mission_unknown(self):
        package = self._package(STALE_PACKAGE_ROUTE_HASH)
        binding = {"package_route_hash": STALE_PACKAGE_ROUTE_HASH,
                  "verified_route_hash": CURRENT_PIXHAWK_ROUTE_HASH}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg(),
                                        mission_binding=binding)
        self.assertEqual(res.status, mf.STATUS_UNKNOWN)
        self.assertEqual(res.reason, mf.REASON_PLANNING_PACKAGE_STALE)
        self.assertIsNone(res.mission_feasible)
        self.assertIsNone(res.planned_completion_distance_m)
        # RTL is untouched.
        self.assertTrue(res.rtl_return_feasible)

    def test_never_proven_binding_forces_mission_unknown_unverified(self):
        package = self._package(STALE_PACKAGE_ROUTE_HASH)
        binding = {"package_route_hash": STALE_PACKAGE_ROUTE_HASH, "verified_route_hash": None}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg(),
                                        mission_binding=binding)
        self.assertEqual(res.reason, mf.REASON_MISSION_ROUTE_UNVERIFIED)
        self.assertIsNone(res.mission_feasible)

    def test_matching_binding_restores_normal_evaluation(self):
        """Package match recovery (task section 7): once the binding's
        verified_route_hash equals the CURRENT package's route hash, mission
        feasibility evaluates immediately, normally -- no restart, no stale
        UNKNOWN latch."""
        package = self._package(CURRENT_PIXHAWK_ROUTE_HASH)
        binding = {"package_route_hash": CURRENT_PIXHAWK_ROUTE_HASH,
                  "verified_route_hash": CURRENT_PIXHAWK_ROUTE_HASH}
        res = mf.evaluate_from_snapshot(self._FakeSnapshot(), package, None, self._FakeCfg(),
                                        mission_binding=binding)
        self.assertTrue(res.route_identity_verified)
        self.assertIsNotNone(res.planned_completion_distance_m)
        self.assertIsNotNone(res.mission_feasible)


class TestRouteIdentityLiveControllerIntegration(tmec._Base):
    """End-to-end proof through the REAL MissionExecutionController + its own
    existing background readiness proof (planning_package.build_readiness
    against the FakeGateway's Pixhawk readback) -- no HTTP, no MAVLink.
    Proves: (5) Start stays fail-closed on the definitive ROUTE_HASH_MISMATCH
    while the package is stale (a COMPLETED, FRESH proof of a genuine
    mismatch -- never the retryable ROUTE_HASH_STALE code, which
    _START_PROOF_TRANSIENT_CODES treats as transient), (2/3) the continuous
    mission-energy evaluation reads UNKNOWN with RTL independently feasible
    from the SAME evidence, and (7) package-match recovery restores normal
    evaluation immediately, no restart."""

    def test_stale_package_start_blocked_and_energy_unknown_rtl_independent(self):
        # Make the stored package NOT match the fixture's Pixhawk route hash.
        self.gw.pixhawk_route_hash = CURRENT_PIXHAWK_ROUTE_HASH
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)

        st = ctrl.status()
        self.assertFalse(st["readiness"]["ready"])
        self.assertEqual(st["readiness"]["reason"], "ROUTE_HASH_MISMATCH")

        # Start remains fail-closed.
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.FAILED)
        self.assertEqual(res["error"]["code"], "ROUTE_HASH_MISMATCH")
        self.assertEqual(self.gw.write_calls, [])

        # The continuous mission-energy evaluation, wired the way
        # local_agent.py wires it (mission_binding=status()["binding"]),
        # reads UNKNOWN for the mission dimension -- never a confident
        # FEASIBLE/INFEASIBLE verdict from the untrusted package route --
        # while RTL (independent of mission_route) stays feasible.
        package = pp.load()
        snap = self._snapshot(mode="LOITER")
        feas = mf.evaluate_from_snapshot(
            snap, package, None, replan_config.DEFAULT,
            mission_binding=ctrl.status()["binding"])
        self.assertEqual(feas.status, mf.STATUS_UNKNOWN)
        self.assertIn(feas.reason, (mf.REASON_PLANNING_PACKAGE_STALE, mf.REASON_MISSION_ROUTE_UNVERIFIED))
        self.assertIsNone(feas.mission_feasible)
        self.assertIsNone(feas.planned_completion_distance_m)
        self.assertTrue(feas.rtl_return_feasible)

    def test_package_match_recovery_restores_evaluation_without_restart(self):
        # Start stale (mismatched).
        self.gw.pixhawk_route_hash = CURRENT_PIXHAWK_ROUTE_HASH
        ctrl = self._ctrl()
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        self.assertFalse(ctrl.status()["readiness"]["ready"])

        # Now the package is re-synced so its route hash matches what the
        # Pixhawk actually carries (tmec._store_verified_package stores a
        # package computed from the SAME fixture route/home _Base.setUp
        # already used, so its route_hash reproduces deterministically).
        matching_hash = tmec._store_verified_package("m1")
        self.gw.pixhawk_route_hash = matching_hash

        # No restart: just re-observe (exactly what local_agent.py's loop
        # already does every iteration) so the controller's own readiness
        # proof re-runs synchronously (readiness_poll_interval_s=0 in this
        # test harness's _cfg()).
        ctrl.observe(self._snapshot(mode="LOITER"), None)
        st = ctrl.status()
        self.assertTrue(st["readiness"]["ready"])
        self.assertEqual(st["binding"]["verified_route_hash"], matching_hash)

        package = pp.load()
        snap = self._snapshot(mode="LOITER")
        feas = mf.evaluate_from_snapshot(
            snap, package, None, replan_config.DEFAULT,
            mission_binding=ctrl.status()["binding"])
        self.assertTrue(feas.route_identity_verified)
        self.assertIsNotNone(feas.planned_completion_distance_m)
        self.assertIsNotNone(feas.mission_feasible)


if __name__ == "__main__":
    unittest.main()
