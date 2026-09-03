"""
Standalone tests for planning_package.py and route_hash.py.

    python3 test_planning_package.py

Covers: route_content_hash matches the published mission-contract-v1 golden
constants (so the Local-Agent copy cannot drift from the Flask contract),
package persistence/reload, backward compatibility for a route with no semantic
segment metadata, and the fail-closed usability gate.
"""
import os
import tempfile
import unittest

import planning_package as pp
import route_hash

# Published mission-contract-v1 constants (services/mission_contract.py). Pinned
# here so a change to the Local-Agent canonicalization fails loudly.
GOLDEN_HASH = "sha256:5fe4c2352fc9183e121538a8e199131159cdda66658ccb755c7db1ff54672bfd"
PROBE_HASH = "sha256:125c779021c1521fae67462719cdab588f871c3b44d808b362c0630f221998ad"

GOLDEN_ROUTE = [
    {"latitude": 56.6501, "longitude": 12.8701, "loiter_time_s": 0},
    {"latitude": 56.6512, "longitude": 12.8725, "loiter_time_s": 30},
]
PROBE_ROUTE = [
    {"latitude": 56.65012345678, "longitude": 12.87016789012, "loiter_time_s": 12.34567},
    {"latitude": 56.65127654321, "longitude": 12.87259876543, "loiter_time_s": 0.9994},
]


class TestRouteHash(unittest.TestCase):
    def test_golden_matches_mission_contract(self):
        self.assertEqual(route_hash.route_content_hash(GOLDEN_ROUTE), GOLDEN_HASH)

    def test_precision_probe_matches_mission_contract(self):
        self.assertEqual(route_hash.route_content_hash(PROBE_ROUTE), PROBE_HASH)

    def test_empty_route_is_none(self):
        self.assertIsNone(route_hash.route_content_hash([]))

    def test_hash_is_order_and_value_sensitive(self):
        moved = [dict(GOLDEN_ROUTE[0]), dict(GOLDEN_ROUTE[1])]
        moved[1] = {"latitude": 56.6513, "longitude": 12.8725, "loiter_time_s": 30}
        self.assertNotEqual(route_hash.route_content_hash(moved), GOLDEN_HASH)


class TestPlanningPackage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))

    def tearDown(self):
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))

    def test_save_and_load_roundtrip(self):
        pp.save("m1", GOLDEN_ROUTE, {"latitude": 56.6500, "longitude": 12.8700},
                usv_id="usv-2", revision=3)
        loaded = pp.load()
        self.assertEqual(loaded["mission_id"], "m1")
        self.assertEqual(loaded["revision"], 3)
        self.assertEqual(loaded["original_route_hash"], GOLDEN_HASH)
        self.assertEqual(len(loaded["route"]), 2)
        self.assertEqual(loaded["home"], {"latitude": 56.6500, "longitude": 12.8700})

    def test_load_none_when_absent(self):
        self.assertIsNone(pp.load())

    def test_backward_compat_route_without_segments(self):
        # A legacy route with no `segment` key must be accepted, tagged
        # UNSPECIFIED, and still usable.
        pkg = pp.save("legacy", GOLDEN_ROUTE, {"latitude": 56.65, "longitude": 12.87})
        self.assertTrue(all(wp["segment"] == pp.SEGMENT_UNSPECIFIED for wp in pkg["route"]))
        self.assertTrue(pp.is_usable(pkg))

    def test_segment_tags_preserved(self):
        route = [
            {"latitude": 56.6501, "longitude": 12.8701, "segment": pp.SEGMENT_OUTBOUND_TRANSIT},
            {"latitude": 56.6512, "longitude": 12.8725, "segment": pp.SEGMENT_PRIMARY_SURVEY},
        ]
        pkg = pp.save("m2", route, {"latitude": 56.65, "longitude": 12.87})
        segs = [wp["segment"] for wp in pkg["route"]]
        self.assertEqual(segs, [pp.SEGMENT_OUTBOUND_TRANSIT, pp.SEGMENT_PRIMARY_SURVEY])

    def test_unknown_segment_falls_back_to_unspecified(self):
        route = [{"latitude": 56.6501, "longitude": 12.8701, "segment": "NONSENSE"}]
        pkg = pp.build_package("m", route, {"latitude": 56.65, "longitude": 12.87})
        self.assertEqual(pkg["route"][0]["segment"], pp.SEGMENT_UNSPECIFIED)

    def test_is_usable_requires_home_and_route(self):
        self.assertFalse(pp.is_usable(None))
        self.assertFalse(pp.is_usable(pp.build_package("m", GOLDEN_ROUTE, None)))
        self.assertFalse(pp.is_usable(pp.build_package("m", [], {"latitude": 56.65, "longitude": 12.87})))
        self.assertTrue(pp.is_usable(pp.build_package("m", GOLDEN_ROUTE, {"latitude": 56.65, "longitude": 12.87})))

    def test_summary_is_bounded(self):
        pkg = pp.save("m", GOLDEN_ROUTE, {"latitude": 56.65, "longitude": 12.87})
        s = pp.summary(pkg)
        self.assertTrue(s["stored"])
        self.assertTrue(s["usable"])
        self.assertEqual(s["route_waypoint_count"], 2)
        self.assertNotIn("route", s)  # bounded: never the full route


if __name__ == "__main__":
    unittest.main(verbosity=2)
