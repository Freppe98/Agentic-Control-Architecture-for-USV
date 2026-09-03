"""
Standalone tests for energy_policy.py.

    python3 test_energy_policy.py

Covers the conservative return-feasibility model, the hard critical floor,
invalid/missing battery handling (never treated as 0%), the debounce, and the
simulated experiment overrides.
"""
import unittest

import energy_policy as ep
import replan_config
from decision_snapshot import DecisionSnapshot


def _snapshot(**overrides):
    base = dict(
        snapshot_id="s1", created_at=0.0, vehicle_id="usv-2",
        latitude=56.66, longitude=12.9, position_age_s=0.2, heading=90.0, groundspeed=1.0,
        mode=10, mode_name="AUTO", armed=True,
        battery_percent=80.0, battery_valid=True, battery_raw=80, battery_voltage=15.0, battery_current=2.0,
        mission_id="m1", mission_hash=None, mission_revision=0, current_sequence=2, mission_count=5,
        mission_active=True, mission_active_evidence="ACTIVE_TRUE", mission_progress="2/5",
        home_latitude=56.65, home_longitude=12.87, home_valid=True, distance_to_home_m=500.0,
        estimated_remaining_survey_distance_m=800.0, estimated_safe_return_distance_m=600.0,
        communication_state="CONNECTED", telemetry_age_s=0.3,
        control_authority="LOCAL_AGENT", authority_age_s=None,
    )
    base.update(overrides)
    return DecisionSnapshot(**base)


def _cfg(**overrides):
    base = dict(usable_range_m=3000.0, reserve_margin_percent=10.0,
                critical_battery_percent=15.0, energy_persistence_count=1)
    base.update(overrides)
    # Fill remaining ReplanConfig fields with defaults.
    return replan_config.ReplanConfig(**{**replan_config.ReplanConfig().to_dict(), **base})


class TestEnergyMargin(unittest.TestCase):
    def test_feasible_return_does_not_trigger(self):
        # 80% battery, 600 m return over 3000 m range = 20% cost; margin
        # 80 - 20 - 10 = 50% > 0 -> monitor.
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot())
        self.assertEqual(r.decision, ep.DECISION_MONITOR)
        self.assertEqual(r.inputs["return_cost_percent"], 20.0)
        self.assertEqual(r.inputs["margin_percent"], 50.0)
        self.assertIn(ep.CODE_FEASIBLE, r.reason_codes)

    def test_negative_margin_triggers(self):
        # Far from home so the return cost eats the battery: 25% battery,
        # 2400 m return / 3000 m = 80% cost, margin 25 - 80 - 10 < 0.
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot(battery_percent=25.0, estimated_safe_return_distance_m=2400.0))
        self.assertEqual(r.decision, ep.DECISION_REPLAN_SAFE_RETURN)
        self.assertIn(ep.CODE_MARGIN_NON_POSITIVE, r.reason_codes)

    def test_critical_battery_triggers_regardless_of_distance(self):
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot(battery_percent=12.0, estimated_safe_return_distance_m=10.0))
        self.assertEqual(r.decision, ep.DECISION_REPLAN_SAFE_RETURN)
        self.assertIn(ep.CODE_CRITICAL_BATTERY, r.reason_codes)

    def test_return_feasibility_beats_bare_percentage(self):
        # A boat at 40% far from home may need to turn back...
        p = ep.EnergyPolicy(_cfg())
        far = p.evaluate(_snapshot(battery_percent=40.0, estimated_safe_return_distance_m=2900.0))
        self.assertEqual(far.decision, ep.DECISION_REPLAN_SAFE_RETURN)
        # ...while one beside home at 25% does not (margin still positive).
        p2 = ep.EnergyPolicy(_cfg())
        near = p2.evaluate(_snapshot(battery_percent=25.0, estimated_safe_return_distance_m=100.0))
        self.assertEqual(near.decision, ep.DECISION_MONITOR)


class TestBatteryValidity(unittest.TestCase):
    def test_unavailable_battery_is_not_zero(self):
        # battery None must NOT read as 0% and force a return.
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot(battery_percent=None, battery_valid=False))
        self.assertEqual(r.decision, ep.DECISION_MONITOR)
        self.assertIn(ep.CODE_BATTERY_UNAVAILABLE, r.reason_codes)
        self.assertIsNone(r.inputs["margin_percent"])

    def test_unavailable_battery_with_simulated_margin_still_works(self):
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot(battery_percent=None, battery_valid=False),
                       injection={"energy_margin_percent": -5.0})
        self.assertEqual(r.decision, ep.DECISION_REPLAN_SAFE_RETURN)
        self.assertTrue(r.simulated)


class TestDebounce(unittest.TestCase):
    def test_single_noisy_sample_does_not_trigger(self):
        p = ep.EnergyPolicy(_cfg(energy_persistence_count=3))
        snap = _snapshot(battery_percent=12.0)
        r1 = p.evaluate(snap)
        self.assertEqual(r1.decision, ep.DECISION_MONITOR)
        self.assertTrue(r1.triggered_raw)
        self.assertFalse(r1.persisted)
        r2 = p.evaluate(snap)
        self.assertEqual(r2.decision, ep.DECISION_MONITOR)
        r3 = p.evaluate(snap)
        self.assertEqual(r3.decision, ep.DECISION_REPLAN_SAFE_RETURN)  # 3rd consecutive

    def test_streak_resets_on_a_good_sample(self):
        p = ep.EnergyPolicy(_cfg(energy_persistence_count=3))
        low = _snapshot(battery_percent=12.0)
        good = _snapshot(battery_percent=80.0)
        p.evaluate(low); p.evaluate(low)
        p.evaluate(good)  # resets streak
        r = p.evaluate(low)
        self.assertEqual(r.decision, ep.DECISION_MONITOR)  # only 1 in the new streak
        self.assertEqual(r.consecutive_triggers, 1)


class TestSimulatedOverrides(unittest.TestCase):
    def test_force_safe_return(self):
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot(), injection={"force_safe_return": True})
        self.assertEqual(r.decision, ep.DECISION_REPLAN_SAFE_RETURN)
        self.assertIn(ep.CODE_SIMULATED_FORCED, r.reason_codes)
        self.assertTrue(r.simulated)
        self.assertIn("force_safe_return", r.simulated_fields)

    def test_battery_override_marked_simulated(self):
        p = ep.EnergyPolicy(_cfg())
        r = p.evaluate(_snapshot(battery_percent=90.0), injection={"battery_percent": 10.0})
        self.assertEqual(r.decision, ep.DECISION_REPLAN_SAFE_RETURN)
        self.assertIn("battery_percent", r.simulated_fields)
        self.assertEqual(r.inputs["battery_percent"], 10.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
