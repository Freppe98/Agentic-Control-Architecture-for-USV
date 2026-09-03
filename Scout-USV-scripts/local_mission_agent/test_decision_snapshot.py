"""
Standalone tests for decision_snapshot.py.

    python3 test_decision_snapshot.py

Covers: time-consistent construction from a vehicle_state, immutability,
invalid/missing battery normalization (never 0), Home sourced from the verified
latch, and the conservative safe-return distance estimate.
"""
import dataclasses
import unittest

import decision_snapshot as dsm
import planning_package as pp


def _vehicle_state(**overrides):
    base = {
        "usv_id": "usv-2",
        "telemetry": {"lat": 56.6520, "lng": 12.8740, "battery": 45, "mode": 10,
                      "mode_name": "AUTO", "armed": True, "heading": 90, "groundspeed": 1.2,
                      "battery_voltage": 15.6, "battery_current": 3.0},
        "mavlink": {"heartbeat_age_s": 0.3, "last_message_age_s": 0.2, "mavlink_connected": True},
        "mission": {"current_mission_id": "m1", "mission_active": True,
                    "current_waypoint": 2, "mission_count": 4},
        "agent": {"control_authority": "LOCAL_AGENT",
                  "home_status": {"verified": True, "ready_for_auto": True,
                                  "home_position": {"latitude": 56.6500, "longitude": 12.8700}}},
    }
    base.update(overrides)
    return base


_ROUTE = [
    {"latitude": 56.6501, "longitude": 12.8701, "loiter_time_s": 0, "segment": pp.SEGMENT_OUTBOUND_TRANSIT},
    {"latitude": 56.6512, "longitude": 12.8725, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
    {"latitude": 56.6520, "longitude": 12.8740, "loiter_time_s": 0, "segment": pp.SEGMENT_PRIMARY_SURVEY},
]


def _package():
    return pp.build_package("m1", _ROUTE, {"latitude": 56.6500, "longitude": 12.8700}, usv_id="usv-2")


class TestSnapshotConsistency(unittest.TestCase):
    def test_basic_fields(self):
        snap = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT", planning_package=_package())
        self.assertEqual(snap.vehicle_id, "usv-2")
        self.assertEqual(snap.latitude, 56.6520)
        self.assertEqual(snap.mode_name, "AUTO")
        self.assertTrue(snap.armed)
        self.assertEqual(snap.communication_state, "CONNECTED")
        self.assertEqual(snap.control_authority, "LOCAL_AGENT")
        self.assertEqual(snap.current_sequence, 2)
        self.assertEqual(snap.mission_progress, "2/4")

    def test_immutable(self):
        snap = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            snap.battery_percent = 10  # type: ignore

    def test_unique_snapshot_ids(self):
        a = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT")
        b = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT")
        self.assertNotEqual(a.snapshot_id, b.snapshot_id)


class TestBatteryNormalization(unittest.TestCase):
    def test_minus_one_is_unavailable_not_zero(self):
        vs = _vehicle_state(telemetry={"lat": 56.65, "lng": 12.87, "battery": -1})
        snap = dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT")
        self.assertIsNone(snap.battery_percent)
        self.assertFalse(snap.battery_valid)
        self.assertEqual(snap.battery_raw, -1)

    def test_out_of_range_is_none(self):
        vs = _vehicle_state(telemetry={"lat": 56.65, "lng": 12.87, "battery": 150})
        snap = dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT")
        self.assertIsNone(snap.battery_percent)

    def test_genuine_zero_is_kept(self):
        vs = _vehicle_state(telemetry={"lat": 56.65, "lng": 12.87, "battery": 0})
        snap = dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT")
        self.assertEqual(snap.battery_percent, 0)
        self.assertTrue(snap.battery_valid)


class TestHomeAndDistances(unittest.TestCase):
    def test_home_from_verified_latch(self):
        snap = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT", planning_package=_package())
        self.assertTrue(snap.home_valid)
        self.assertEqual(snap.home_latitude, 56.6500)
        self.assertIsNotNone(snap.distance_to_home_m)

    def test_home_invalid_when_not_verified(self):
        vs = _vehicle_state(agent={"control_authority": "LOCAL_AGENT",
                                   "home_status": {"verified": False, "ready_for_auto": False}})
        snap = dsm.build_snapshot(vs, "CONNECTED", "LOCAL_AGENT", planning_package=_package())
        self.assertFalse(snap.home_valid)
        # Home coords still fall back to the package so a distance can be shown.
        self.assertEqual(snap.home_latitude, 56.6500)

    def test_safe_return_distance_is_conservative(self):
        # The retrace distance (via traversed approved waypoints) is >= the
        # straight-line distance to Home.
        snap = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT", planning_package=_package())
        self.assertGreaterEqual(snap.estimated_safe_return_distance_m, snap.distance_to_home_m)

    def test_experiment_overrides_recorded(self):
        snap = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT",
                                  experiment_overrides={"force_safe_return": True, "source": "SIMULATED"})
        self.assertEqual(snap.active_experiment_overrides["force_safe_return"], True)

    def test_obstacle_summary_placeholder_present_but_empty(self):
        snap = dsm.build_snapshot(_vehicle_state(), "CONNECTED", "LOCAL_AGENT")
        self.assertIsNone(snap.obstacle_summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
