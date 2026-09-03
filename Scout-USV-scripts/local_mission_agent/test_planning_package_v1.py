"""
Standalone tests for the replan-planning-package-v1 receiving/validation/storage
and readiness contract (planning_package.py's v1 surface + replan_api.py's
acceptance flow).

    python3 test_planning_package_v1.py

No live hardware: the Pixhawk readback (pixhawk_mission.build_pixhawk_mission_status)
is mocked. Covers the full acceptance matrix, live-Pixhawk consistency, atomic
envelope storage (rejection never destroys a usable package; idempotency;
generation; original-vs-active separation), and the readiness state machine.
"""
import math
import os
import tempfile
import unittest

import pixhawk_mission
import planning_package as pp
import replan_api
import route_hash
from config import USV_ID

_ROUTE = [
    {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0, "segment": "OUTBOUND_TRANSIT"},
    {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0, "segment": "PRIMARY_SURVEY"},
]
_HOME = {"latitude": 56.6490, "longitude": 12.8700}
_NAV_RING = [[12.86, 56.64], [12.89, 56.64], [12.89, 56.66], [12.86, 56.66]]
_ROUTE_HASH = route_hash.route_content_hash(_ROUTE)
_DUMMY_HASH = "sha256:" + "0" * 64


def _v1_body(**overrides):
    body = {
        "package_version": "replan-planning-package-v1",
        "route_contract_version": "mission-contract-v1",
        "mission_id": "msn-329c2faff137",
        "mission_revision": 0,
        "vehicle_id": USV_ID,
        "planning_home": [_HOME["longitude"], _HOME["latitude"]],
        "boundary": [list(_NAV_RING)],
        "navigable_geometry": [list(_NAV_RING)],
        "no_go_zones": [],
        "shoreline_clearance_m": 1,
        "route_waypoints": [dict(w) for w in _ROUTE],
        "segments": [],
        "original_execution_order": [1, 2],
        "immutable": True,
        "created_at": "2026-08-05T00:00:00Z",
        "source": "OPERATOR_STATION",
    }
    body.update(overrides)
    if "route_hash" not in overrides:
        rw = body.get("route_waypoints")
        try:
            body["route_hash"] = route_hash.route_content_hash(rw) if rw else _DUMMY_HASH
        except Exception:
            body["route_hash"] = _DUMMY_HASH
    return body


def _readback(route=None, **overrides):
    route = _ROUTE if route is None else route
    rb = {"reachable": True, "mission_valid": True, "partial": False,
          "route_content_hash": route_hash.route_content_hash(route),
          "route_waypoint_count": len(route), "error": None,
          # Explicit fresh COORDINATED_CACHE proof envelope (the shape GET
          # /agent/pixhawk_mission now returns). Tests override to exercise
          # stale/refreshing/unverified rejection.
          "proof_source": pp.PROOF_SOURCE_CACHE, "cached": True, "stale": False,
          "refreshing": False, "busy": False, "observed_at": 1000.0, "age_s": 0.5,
          "refresh_generation": 1}
    rb.update(overrides)
    return rb


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        self._orig_pixhawk = pixhawk_mission.build_pixhawk_mission_status
        self._orig_pixhawk_proof = pixhawk_mission.build_pixhawk_mission_proof
        self._readbacks = [_readback()]  # each call pops the next (last repeats)
        pixhawk_mission.build_pixhawk_mission_status = self._next_readback
        # Acceptance (put_planning_package) uses the fresh proof variant; route
        # it through the same queued-readback mock so the consistency/generation
        # logic is exercised without touching the network.
        pixhawk_mission.build_pixhawk_mission_proof = self._next_readback

    def _next_readback(self):
        if len(self._readbacks) > 1:
            return self._readbacks.pop(0)
        return self._readbacks[0]

    def set_pixhawk(self, *readbacks):
        self._readbacks = list(readbacks)

    def tearDown(self):
        pixhawk_mission.build_pixhawk_mission_status = self._orig_pixhawk
        pixhawk_mission.build_pixhawk_mission_proof = self._orig_pixhawk_proof
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))


# ── Offline structural / semantic validation ──────────────────────────────────
class TestValidation(_Base):
    def _reject(self, body, code):
        pkg, c, _msg = pp.validate_package_v1(body, USV_ID)
        self.assertIsNone(pkg, f"expected rejection {code}")
        self.assertEqual(c, code)

    def test_valid_accepted(self):
        pkg, code, msg = pp.validate_package_v1(_v1_body(), USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(pkg["mission_id"], "msn-329c2faff137")
        self.assertEqual(pkg["route_hash"], _ROUTE_HASH)
        self.assertEqual(pkg["original_route_hash"], _ROUTE_HASH)  # legacy alias
        self.assertTrue(pkg["immutable"])
        # geometry canonicalized wire [lon,lat] -> internal [lat,lon]
        self.assertEqual(pkg["navigable_geometry"][0][0], [56.64, 12.86])
        self.assertEqual(pkg["home"], {"latitude": 56.6490, "longitude": 12.8700})
        self.assertEqual(pkg["planning_home"], [12.8700, 56.6490])

    def test_home_corridor_absent_is_empty_backward_compatible(self):
        pkg, code, _ = pp.validate_package_v1(_v1_body(), USV_ID)
        self.assertIsNone(code)
        self.assertEqual(pkg.get("home_corridor"), [])   # optional, absent -> []

    def test_home_corridor_accepted_and_canonicalized(self):
        # Task section 4: an approved Home/launch corridor survives acceptance,
        # canonicalized wire [lon,lat] -> internal [lat,lon].
        corridor = [[12.8695, 56.6465], [12.8705, 56.6465],
                    [12.8705, 56.6505], [12.8695, 56.6505]]
        pkg, code, msg = pp.validate_package_v1(_v1_body(home_corridor=corridor), USV_ID)
        self.assertIsNone(code, msg)
        self.assertTrue(len(pkg["home_corridor"]) >= 3)
        self.assertEqual(pkg["home_corridor"][0], [56.6465, 12.8695])

    def test_malformed_home_corridor_rejected(self):
        self._reject(_v1_body(home_corridor=[[12.86, 56.64], [12.89, 56.64]]),
                     "INVALID_HOME_CORRIDOR")

    def test_route_waypoint_outside_navigable_geometry_rejected(self):
        # A route leg the Operator's own navigable_geometry doesn't cover can
        # never be safely retraced later -- reject at acceptance, not at an
        # emergency replan.
        route = [dict(w) for w in _ROUTE]
        route.append({"latitude": 56.70, "longitude": 12.87,  # well outside _NAV_RING
                       "loiter_time_s": 0, "segment": "PRIMARY_SURVEY"})
        self._reject(_v1_body(route_waypoints=route), "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY")

    def test_home_outside_navigable_geometry_without_corridor_rejected(self):
        self._reject(_v1_body(planning_home=[12.87, 56.70]),  # well outside _NAV_RING
                     "HOME_OUTSIDE_NAVIGABLE_GEOMETRY")

    def test_home_outside_navigable_geometry_covered_by_corridor_accepted(self):
        # Same out-of-boundary Home, but an approved corridor that contains it
        # proves the connector -- must be accepted, mirroring safe_return_planner's
        # own home_in_boundary-or-home_in_corridor contract.
        corridor = [[12.86, 56.66], [12.88, 56.66], [12.88, 56.68], [12.86, 56.68]]
        pkg, code, msg = pp.validate_package_v1(
            _v1_body(planning_home=[12.87, 56.665], home_corridor=corridor), USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(pkg["home"], {"latitude": 56.665, "longitude": 12.87})

    def test_undersized_navigable_geometry_not_rescued_by_raw_boundary(self):
        # Raw-boundary fallback regression (must NOT happen): the Operator's
        # navigable_geometry is undersized -- it does not reach the approved
        # route -- but the raw `boundary` submitted in the SAME request DOES
        # contain it. Even though `boundary` alone would prove the route
        # safe, it is provenance only (see planning_package module docstring)
        # and must never be substituted for navigable_geometry; the package
        # must fail closed, not be rescued.
        narrow_nav = [[12.86, 56.64], [12.89, 56.64], [12.89, 56.6505], [12.86, 56.6505]]
        pkg, code, msg = pp.validate_package_v1(
            _v1_body(navigable_geometry=[narrow_nav], boundary=[list(_NAV_RING)]), USV_ID)
        self.assertIsNone(pkg)
        self.assertEqual(code, "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY")

    def test_route_regenerated_inside_navigable_geometry_accepted(self):
        # Corrected version of the same scenario: once navigable_geometry
        # itself (not the raw boundary) actually contains the approved route,
        # acceptance succeeds and navigable_boundary (what safe_return_planner
        # validates against) is exactly navigable_geometry -- never `boundary`.
        pkg, code, msg = pp.validate_package_v1(
            _v1_body(navigable_geometry=[list(_NAV_RING)], boundary=[list(_NAV_RING)]), USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(pkg["navigable_boundary"], pkg["navigable_geometry"][0])

    def test_route_outside_both_navigable_geometry_and_boundary_rejected(self):
        # No rescue possible when NEITHER submitted polygon proves
        # containment -- must still fail closed exactly as before.
        route = [dict(w) for w in _ROUTE]
        route.append({"latitude": 56.70, "longitude": 12.87,
                       "loiter_time_s": 0, "segment": "PRIMARY_SURVEY"})
        self._reject(_v1_body(route_waypoints=route, boundary=[list(_NAV_RING)]),
                     "ROUTE_OUTSIDE_NAVIGABLE_GEOMETRY")

    def test_first_approach_route_with_home_corridor_accepted(self):
        # Operator's route_start_mode=first_approach fix: the execution route
        # begins directly at A1 (the first survey waypoint) -- no
        # OUTBOUND_TRANSIT leg from Home is present in route_waypoints -- and
        # the finalized approved Home->A1 connector is carried as
        # home_corridor instead. Scout does not need, and this wire body does
        # not carry, a planning_only_transit_segments key.
        nav = [[12.86, 56.66], [12.89, 56.66], [12.89, 56.68], [12.86, 56.68]]
        corridor = [[12.865, 56.647], [12.875, 56.647], [12.875, 56.663], [12.865, 56.663]]
        route = [
            {"latitude": 56.6610, "longitude": 12.8700, "loiter_time_s": 0, "segment": "PRIMARY_SURVEY"},
            {"latitude": 56.6620, "longitude": 12.8700, "loiter_time_s": 0, "segment": "PRIMARY_SURVEY"},
        ]
        body = _v1_body(route_waypoints=route, navigable_geometry=[nav],
                        boundary=[nav], home_corridor=corridor)
        self.assertNotIn("planning_only_transit_segments", body)
        pkg, code, msg = pp.validate_package_v1(body, USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(pkg["route_waypoints"][0]["latitude"], 56.6610)  # execution route begins at A1
        self.assertTrue(len(pkg["home_corridor"]) >= 3)
        self.assertNotIn("planning_only_transit_segments", pkg)

    def test_route_crossing_its_own_no_go_zone_rejected(self):
        # No previous check ever verified the ORIGINAL approved route doesn't
        # cross its own declared no-go zone -- only the later safe-return
        # retrace route was checked. A no-go zone has no safe fallback (unlike
        # navigable_geometry/boundary): this must hard-reject, never rescue.
        zone = [[12.8695, 56.6502], [12.8705, 56.6502],
                [12.8705, 56.6508], [12.8695, 56.6508]]  # straddles _ROUTE (lon 12.87)
        self._reject(_v1_body(no_go_zones=[zone]), "ROUTE_CROSSES_NO_GO_ZONE")

    def test_empty_no_go_zones_accepted(self):
        pkg, code, _ = pp.validate_package_v1(_v1_body(no_go_zones=[]), USV_ID)
        self.assertIsNone(code)
        self.assertEqual(pkg["no_go_zones"], [])          # explicitly checked zero
        self.assertTrue(pp.summary(pkg)["no_go_zones_present"])

    def test_single_no_go_polygon_counted_exactly_one_end_to_end(self):
        # E2 preflight evidence: a package carrying exactly ONE no-go polygon
        # must report no_go_zone_count == 1 (not merely no_go_zones_present ==
        # True, which would also be True for an empty-but-present list) all
        # the way from acceptance through the compact status summary that
        # /agent/replan/planning_package and /agent/replan/status expose.
        zone = [[12.8795, 56.6505], [12.8805, 56.6505],  # clear of _ROUTE (lon 12.87)
                [12.8805, 56.6515], [12.8795, 56.6515]]
        pkg, code, msg = pp.validate_package_v1(_v1_body(no_go_zones=[zone]), USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(len(pkg["no_go_zones"]), 1)

        s = pp.summary(pkg)
        self.assertTrue(s["no_go_zones_present"])
        self.assertEqual(s["no_go_zone_count"], 1)
        # A second, distinct polygon -> count tracks polygons, not vertices
        # or rings-within-a-polygon.
        zone2 = [[12.90, 56.70], [12.91, 56.70], [12.91, 56.71], [12.90, 56.71]]
        pkg2, code2, msg2 = pp.validate_package_v1(_v1_body(no_go_zones=[zone, zone2]), USV_ID)
        self.assertIsNone(code2, msg2)
        self.assertEqual(pp.summary(pkg2)["no_go_zone_count"], 2)

    def test_home_corridor_evidence_exposed_pre_start_in_summary(self):
        # Home/corridor evidence must be visible in the compact status BEFORE
        # Start / before any replan validation has run (unlike
        # geometry_validation's home_corridor_checked, which only populates
        # once a replan transaction actually calls validate_route).
        corridor = [[12.8695, 56.6465], [12.8705, 56.6465],
                    [12.8705, 56.6505], [12.8695, 56.6505]]
        pkg, code, msg = pp.validate_package_v1(_v1_body(home_corridor=corridor), USV_ID)
        self.assertIsNone(code, msg)
        self.assertTrue(pp.summary(pkg)["has_home_corridor"])

        pkg_none, code_none, _ = pp.validate_package_v1(_v1_body(), USV_ID)
        self.assertIsNone(code_none)
        self.assertFalse(pp.summary(pkg_none)["has_home_corridor"])

    def test_no_go_clearance_m_accepted_and_stored(self):
        # A. New package field: no_go_clearance_m is accepted and stored/
        # surfaced end-to-end (contract + status summary).
        pkg, code, msg = pp.validate_package_v1(_v1_body(no_go_clearance_m=5.0), USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(pkg["no_go_clearance_m"], 5.0)
        self.assertEqual(pp.summary(pkg)["no_go_clearance_m"], 5.0)

    def test_no_go_clearance_m_missing_normalizes_to_zero(self):
        # B. Legacy package (no no_go_clearance_m on the wire) is accepted and
        # the EFFECTIVE clearance is 0.0 -- never silently upgraded to the new
        # 5.0 m Operator default, which would change historical mission
        # semantics for a package stored before this field existed.
        body = _v1_body()
        self.assertNotIn("no_go_clearance_m", body)
        pkg, code, msg = pp.validate_package_v1(body, USV_ID)
        self.assertIsNone(code, msg)
        self.assertEqual(pkg["no_go_clearance_m"], 0.0)
        self.assertEqual(pp.summary(pkg)["no_go_clearance_m"], 0.0)
        self.assertEqual(pp.no_go_clearance_m_of(pkg), 0.0)

    def test_no_go_clearance_m_negative_rejected(self):
        # C. Invalid values fail closed.
        self._reject(_v1_body(no_go_clearance_m=-1), "INVALID_NO_GO_CLEARANCE")

    def test_no_go_clearance_m_nan_rejected(self):
        self._reject(_v1_body(no_go_clearance_m=float("nan")), "INVALID_NO_GO_CLEARANCE")

    def test_no_go_clearance_m_infinity_rejected(self):
        self._reject(_v1_body(no_go_clearance_m=float("inf")), "INVALID_NO_GO_CLEARANCE")

    def test_no_go_clearance_m_non_numeric_string_rejected(self):
        self._reject(_v1_body(no_go_clearance_m="far"), "INVALID_NO_GO_CLEARANCE")

    def test_no_go_clearance_m_malformed_object_rejected(self):
        self._reject(_v1_body(no_go_clearance_m={"metres": 5}), "INVALID_NO_GO_CLEARANCE")

    def test_status_summary_exposes_no_go_clearance_with_zone_count(self):
        # D. Status: preflight visibility combining no_go_zone_count and
        # no_go_clearance_m (E2 preflight: no_go_zone_count=1, no_go_clearance_m=5.0).
        zone = [[12.8795, 56.6505], [12.8805, 56.6505],
                [12.8805, 56.6515], [12.8795, 56.6515]]
        pkg, code, msg = pp.validate_package_v1(
            _v1_body(no_go_zones=[zone], no_go_clearance_m=5.0), USV_ID)
        self.assertIsNone(code, msg)
        s = pp.summary(pkg)
        self.assertEqual(s["no_go_zone_count"], 1)
        self.assertEqual(s["no_go_clearance_m"], 5.0)

    def test_missing_no_go_zones_distinguished_from_empty(self):
        body = _v1_body()
        del body["no_go_zones"]
        self._reject(body, "MISSING_NO_GO_ZONES")

    def test_missing_navigable_geometry_rejected(self):
        body = _v1_body()
        del body["navigable_geometry"]
        self._reject(body, "MISSING_NAVIGABLE_GEOMETRY")

    def test_malformed_navigable_geometry_rejected(self):
        self._reject(_v1_body(navigable_geometry=[[[12.86, 56.64], [12.89, 56.64]]]),
                     "INVALID_NAVIGABLE_GEOMETRY")  # ring < 3 vertices

    def test_malformed_coordinates_rejected(self):
        self._reject(_v1_body(route_waypoints=[{"latitude": "abc", "longitude": 12.87}]),
                     "INVALID_COORDINATE")

    def test_nan_rejected(self):
        self._reject(_v1_body(route_waypoints=[{"latitude": float("nan"), "longitude": 12.87}]),
                     "INVALID_COORDINATE")

    def test_infinity_rejected(self):
        self._reject(_v1_body(planning_home=[float("inf"), 56.6]), "INVALID_HOME")
        self._reject(_v1_body(navigable_geometry=[[[float("inf"), 56.64], [12.89, 56.64], [12.89, 56.66]]]),
                     "INVALID_NAVIGABLE_GEOMETRY")

    def test_latlon_order_mistake_rejected(self):
        # A detectable swap: correct planning_home is [lon=112.87, lat=56.649];
        # given [lat, lon] the latitude slot holds 112.87 (impossible latitude)
        # while the longitude slot holds a plausible latitude -> detectable.
        self._reject(_v1_body(planning_home=[56.6490, 112.8700]), "LATLON_ORDER")
        # a no-go ring vertex with latitude 200 in the latitude slot
        self._reject(_v1_body(no_go_zones=[[[12.86, 200.0], [12.89, 56.64], [12.89, 56.66]]]),
                     "INVALID_NO_GO_ZONE")

    def test_unsupported_package_version_rejected(self):
        self._reject(_v1_body(package_version="replan-planning-package-v2"),
                     "UNSUPPORTED_PACKAGE_VERSION")

    def test_unsupported_route_contract_rejected(self):
        self._reject(_v1_body(route_contract_version="mission-contract-v99"),
                     "UNSUPPORTED_ROUTE_CONTRACT")

    def test_wrong_vehicle_rejected(self):
        self._reject(_v1_body(vehicle_id="usv-99"), "WRONG_TARGET_USV")

    def test_mission_id_missing_rejected(self):
        self._reject(_v1_body(mission_id=""), "MISSING_MISSION_ID")

    def test_route_hash_missing_rejected(self):
        self._reject(_v1_body(route_hash=None), "INVALID_ROUTE_HASH")

    def test_route_hash_malformed_rejected(self):
        self._reject(_v1_body(route_hash="deadbeef"), "INVALID_ROUTE_HASH")
        self._reject(_v1_body(route_hash="sha256:xyz"), "INVALID_ROUTE_HASH")

    def test_route_hash_inconsistent_rejected(self):
        # Well-formed but does not match the route_waypoints content.
        self._reject(_v1_body(route_hash=_DUMMY_HASH), "ROUTE_HASH_INCONSISTENT")

    def test_not_immutable_rejected(self):
        self._reject(_v1_body(immutable=False), "NOT_IMMUTABLE")
        self._reject(_v1_body(immutable="true"), "NOT_IMMUTABLE")

    def test_revision_nonzero_rejected(self):
        self._reject(_v1_body(mission_revision=1), "IMMUTABLE_ORIGINAL_ONLY")

    def test_revision_non_integer_rejected(self):
        self._reject(_v1_body(mission_revision="0"), "INVALID_REVISION")


# ── Live-Pixhawk consistency at acceptance ────────────────────────────────────
class TestPixhawkConsistency(_Base):
    def test_matching_hash_accepted_and_stored(self):
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 200)
        self.assertTrue(out["stored"])
        self.assertEqual(out["pixhawk_hash_used"], _ROUTE_HASH)
        self.assertTrue(pp.is_usable(pp.load()))

    def test_pixhawk_unavailable_rejected(self):
        self.set_pixhawk(_readback(reachable=False, route_content_hash=None,
                                   route_waypoint_count=None, error="flask down"))
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 503)
        self.assertEqual(out["error"]["code"], "PIXHAWK_UNAVAILABLE")
        self.assertIsNone(pp.load())  # nothing stored

    def test_partial_readback_rejected(self):
        self.set_pixhawk(_readback(partial=True))
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 503)
        self.assertEqual(out["error"]["code"], "PIXHAWK_READBACK_PARTIAL")
        self.assertIsNone(pp.load())

    def test_route_hash_unavailable_rejected(self):
        self.set_pixhawk(_readback(route_content_hash=None))
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 503)
        self.assertEqual(out["error"]["code"], "ROUTE_HASH_UNAVAILABLE")

    def test_hash_mismatch_rejected(self):
        other = [{"latitude": 56.70, "longitude": 12.90, "loiter_time_s": 0}]
        self.set_pixhawk(_readback(route=other))
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 409)
        self.assertEqual(out["error"]["code"], "ROUTE_HASH_MISMATCH")
        self.assertIsNone(pp.load())

    def test_route_count_conflict_rejected(self):
        # Same hash reported but a conflicting waypoint count.
        self.set_pixhawk(_readback(route_waypoint_count=14))
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 409)
        self.assertEqual(out["error"]["code"], "ROUTE_COUNT_MISMATCH")

    def test_active_mission_changed_during_validation_rejected(self):
        # First readback matches, second (the re-check) differs.
        other = [{"latitude": 56.70, "longitude": 12.90, "loiter_time_s": 0}]
        self.set_pixhawk(_readback(), _readback(route=other))
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 409)
        self.assertEqual(out["error"]["code"], "ACTIVE_MISSION_CHANGED")
        self.assertIsNone(pp.load())


# ── Atomic storage / idempotency / rejection safety ───────────────────────────
class TestStorage(_Base):
    def test_rejected_replacement_preserves_previous(self):
        replan_api.put_planning_package(_v1_body())
        self.assertTrue(pp.is_usable(pp.load()))
        gen1 = pp.load_envelope()["generation"]
        # A second POST that fails Pixhawk consistency must NOT destroy the first.
        self.set_pixhawk(_readback(reachable=False, route_content_hash=None, error="down"))
        code, out = replan_api.put_planning_package(_v1_body(mission_id="msn-new"))
        self.assertEqual(out["accepted"], False)
        env = pp.load_envelope()
        self.assertEqual(env["generation"], gen1)
        self.assertEqual(env["original_package"]["mission_id"], "msn-329c2faff137")
        self.assertTrue(pp.is_usable(pp.load()))

    def test_idempotent_repeat(self):
        replan_api.put_planning_package(_v1_body())
        env1 = pp.load_envelope()
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 200)
        self.assertTrue(out["idempotent"])
        env2 = pp.load_envelope()
        self.assertEqual(env2["generation"], env1["generation"])       # not bumped
        self.assertEqual(env2["received_at"], env1["received_at"])     # preserved

    def test_different_original_replaces_and_bumps_generation(self):
        replan_api.put_planning_package(_v1_body())
        gen1 = pp.load_envelope()["generation"]
        other = [{"latitude": 56.70, "longitude": 12.90, "loiter_time_s": 0}]
        self.set_pixhawk(_readback(route=other))
        code, out = replan_api.put_planning_package(_v1_body(mission_id="msn-other",
                                                             route_waypoints=other))
        self.assertEqual(code, 200)
        self.assertFalse(out["idempotent"])
        env = pp.load_envelope()
        self.assertEqual(env["generation"], gen1 + 1)
        self.assertEqual(env["original_package"]["mission_id"], "msn-other")

    def test_original_and_active_separated(self):
        replan_api.put_planning_package(_v1_body())
        env = pp.load_envelope()
        self.assertIn("original_package", env)
        self.assertIn("active_package", env)
        self.assertEqual(env["active_package_revision"], 0)
        self.assertEqual(env["revision_history"], [])
        self.assertEqual(pp.load_original()["mission_revision"], 0)

    def test_home_sync_leaves_original_pristine(self):
        replan_api.put_planning_package(_v1_body())
        new_home = {"latitude": 56.6495, "longitude": 12.8705}
        active = pp.update_home(new_home)
        self.assertEqual(active["home"], new_home)
        # original safety envelope Home untouched
        self.assertEqual(pp.load_original()["home"], {"latitude": 56.6490, "longitude": 12.8700})
        # route hash / consistency unaffected
        self.assertEqual(active["original_route_hash"], _ROUTE_HASH)

    def test_store_is_atomic_replace(self):
        # A store envelope file exists and reloads as a whole object.
        replan_api.put_planning_package(_v1_body())
        raw = pp._read_raw_nolock()
        self.assertEqual(raw["store_version"], pp.STORE_VERSION)


# ── Runtime Home-corridor mutation removed / original geometry immutable ──────
class TestHomeCorridorRuntimeMutationRemoved(_Base):
    def test_update_home_corridor_function_removed(self):
        # home_corridor is approved mission safety geometry: it must be
        # produced and verified by Operator BEFORE mission finalization, and
        # Scout must consume it read-only. There is no longer any function
        # capable of attaching/widening/replacing a corridor after acceptance.
        self.assertFalse(hasattr(pp, "update_home_corridor"))

    def test_original_safety_geometry_immutable_after_acceptance(self):
        corridor = [[12.86, 56.64], [12.89, 56.64], [12.89, 56.66], [12.86, 56.66]]
        replan_api.put_planning_package(_v1_body(home_corridor=corridor))
        original_before = pp.load_original()
        # Runtime Home tracking (update_home) is still allowed -- it only ever
        # touches the ACTIVE revision, never the immutable original package's
        # safety geometry.
        pp.update_home({"latitude": 56.70, "longitude": 12.87})
        original_after = pp.load_original()
        self.assertEqual(original_after["navigable_boundary"], original_before["navigable_boundary"])
        self.assertEqual(original_after["home_corridor"], original_before["home_corridor"])
        self.assertEqual(original_after["no_go_zones"], original_before["no_go_zones"])
        self.assertEqual(original_after["no_go_clearance_m"], original_before["no_go_clearance_m"])
        # Runtime Home tracking does not touch the active package's safety
        # geometry either -- only "home" itself moves.
        active = pp.load()
        self.assertEqual(active["home_corridor"], original_before["home_corridor"])
        self.assertEqual(active["navigable_boundary"], original_before["navigable_boundary"])


# ── Readiness state machine ───────────────────────────────────────────────────
class TestReadiness(_Base):
    def test_false_before_package(self):
        r = pp.build_readiness(_readback())
        self.assertFalse(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_MISSING)
        self.assertIsNone(r["connector_proven_safe"])

    def test_true_after_valid_matching_package(self):
        replan_api.put_planning_package(_v1_body())
        r = pp.build_readiness(_readback())
        self.assertTrue(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_USABLE)
        self.assertTrue(r["route_hash_match"])
        self.assertTrue(r["navigable_geometry_checked"])
        self.assertTrue(r["no_go_zones_checked"])
        self.assertTrue(r["mission_verified"])
        self.assertIsNone(r["connector_proven_safe"])  # never asserted true

    def test_pixhawk_unavailable_state(self):
        replan_api.put_planning_package(_v1_body())
        r = pp.build_readiness(_readback(reachable=False, route_content_hash=None, error="down"))
        self.assertFalse(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_PIXHAWK_UNAVAILABLE)

    def test_not_ready_when_readback_has_no_proof_source(self):
        # A matching hash is not enough: without an explicit proof_source the
        # readback is unverified and can never make replanning READY. This is a
        # FRESHNESS gap (no recognized proof), NOT a package inconsistency.
        replan_api.put_planning_package(_v1_body())
        rb = _readback()
        for k in ("proof_source", "cached", "stale", "refreshing", "busy",
                  "observed_at", "age_s", "refresh_generation"):
            rb.pop(k, None)
        r = pp.build_readiness(rb)
        self.assertFalse(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_PROOF_STALE)
        self.assertNotEqual(r["state"], pp.READY_PACKAGE_STALE)

    def test_not_ready_when_readback_refreshing(self):
        replan_api.put_planning_package(_v1_body())
        r = pp.build_readiness(_readback(refreshing=True))
        self.assertFalse(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_REFRESHING)
        self.assertNotEqual(r["state"], pp.READY_PACKAGE_STALE)

    def test_hash_comparison_unavailable_state(self):
        replan_api.put_planning_package(_v1_body())
        r = pp.build_readiness(_readback(route_content_hash=None))
        self.assertEqual(r["state"], pp.READY_HASH_UNAVAILABLE)

    def test_stale_when_pixhawk_mission_changes_later(self):
        replan_api.put_planning_package(_v1_body())
        other = [{"latitude": 56.70, "longitude": 12.90, "loiter_time_s": 0}]
        r = pp.build_readiness(_readback(route=other))
        self.assertFalse(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_STALE)
        self.assertFalse(r["route_hash_match"])

    def test_mission_id_inconsistent_is_stale(self):
        replan_api.put_planning_package(_v1_body())
        r = pp.build_readiness(_readback(mission_id="a-different-mission"))
        self.assertEqual(r["state"], pp.READY_STALE)
        self.assertFalse(r["mission_id_consistent"])

    def test_structurally_invalid_state(self):
        # Store a legacy flat package with no navigable geometry / no-go zones.
        pp.save("legacy", _ROUTE, _HOME)
        r = pp.build_readiness(_readback())
        self.assertFalse(r["replanning_ready"])
        self.assertEqual(r["state"], pp.READY_INVALID)


# ── Freshness/consistency separation + last-successful-proof retention ─────────
class TestReadinessFreshnessSeparation(_Base):
    """The core defect fix: a stale/expired/refreshing PROOF is never mapped to
    PLANNING_PACKAGE_STALE, and the last successful consistency proof is retained
    (not erased) across those transient windows. PLANNING_PACKAGE_STALE is
    reserved for a COMPLETED, FRESH proof that shows a genuine mismatch."""

    def _put(self):
        replan_api.put_planning_package(_v1_body())

    def test_successful_package_stays_consistent_after_cache_ttl_expiry(self):
        # Task test: a successful package remains logically consistent after the
        # cache TTL expires -- PROOF_STALE (checking), never PACKAGE_STALE.
        self._put()
        r0 = pp.build_readiness(_readback())            # fresh -> READY, records proof
        self.assertEqual(r0["state"], pp.READY_USABLE)
        self.assertIsNotNone(r0["last_verified"])
        r1 = pp.build_readiness(_readback(age_s=pp.PROOF_MAX_CACHE_AGE_S + 20.0))
        self.assertEqual(r1["state"], pp.READY_PROOF_STALE)
        self.assertNotEqual(r1["state"], pp.READY_PACKAGE_STALE)
        self.assertFalse(r1["replanning_ready"])
        # The last successful consistency proof is RETAINED, with all fields.
        lv = r1["last_verified"]
        self.assertIsNotNone(lv)
        self.assertEqual(lv["last_verified_mission_id"], "msn-329c2faff137")
        self.assertEqual(lv["last_verified_route_hash"], _ROUTE_HASH)
        self.assertEqual(lv["last_verified_route_count"], len(_ROUTE))
        self.assertIsNotNone(lv["last_verified_generation"])
        self.assertIsNotNone(lv["last_verified_at"])

    def test_refresh_in_progress_does_not_become_package_stale(self):
        # Task test: refresh-in-progress does not become PLANNING_PACKAGE_STALE.
        self._put()
        pp.build_readiness(_readback())                 # establish a proof first
        for rb in (_readback(refreshing=True), _readback(busy=True)):
            r = pp.build_readiness(rb)
            self.assertEqual(r["state"], pp.READY_REFRESHING)
            self.assertNotEqual(r["state"], pp.READY_PACKAGE_STALE)
            self.assertIsNotNone(r["last_verified"])     # proof retained through refresh

    def test_completed_fresh_mismatch_becomes_package_stale(self):
        # Task test: a completed FRESH mismatch DOES become PLANNING_PACKAGE_STALE,
        # and a genuine mismatch erases the retained proof.
        self._put()
        pp.build_readiness(_readback())                 # record a verified proof
        other = [{"latitude": 56.70, "longitude": 12.90, "loiter_time_s": 0}]
        r = pp.build_readiness(_readback(route=other))  # fresh, different route
        self.assertEqual(r["state"], pp.READY_PACKAGE_STALE)
        self.assertEqual(r["state"], "PLANNING_PACKAGE_STALE")
        self.assertFalse(r["route_hash_match"])
        self.assertIsNone(r["last_verified"])           # genuine mismatch clears the proof

    def test_completed_fresh_count_mismatch_becomes_package_stale(self):
        self._put()
        r = pp.build_readiness(_readback(route_waypoint_count=99))
        self.assertEqual(r["state"], pp.READY_PACKAGE_STALE)

    def test_completed_fresh_invalid_mission_becomes_package_stale(self):
        self._put()
        r = pp.build_readiness(_readback(mission_valid=False))
        self.assertEqual(r["state"], pp.READY_PACKAGE_STALE)

    def test_completed_fresh_mission_id_mismatch_becomes_package_stale(self):
        self._put()
        r = pp.build_readiness(_readback(mission_id="a-different-mission"))
        self.assertEqual(r["state"], pp.READY_PACKAGE_STALE)
        self.assertFalse(r["mission_id_consistent"])

    def test_thirty_seconds_unchanged_pixhawk_stays_consistent(self):
        # Task test: package sync then 30 s of UNCHANGED Pixhawk state remains
        # consistent -- READY whenever fresh, PROOF_STALE ("checking") when the
        # cache momentarily lapses, NEVER PLANNING_PACKAGE_STALE, proof retained.
        self._put()
        base = 1000.0
        seen = set()
        for t in range(0, 31):
            # A periodically-expiring cache over an otherwise unchanged mission.
            age = (pp.PROOF_MAX_CACHE_AGE_S + 1.0) if (t and t % 5 == 0) else 0.5
            r = pp.build_readiness(
                _readback(observed_at=base + t, age_s=age), now=base + t)
            seen.add(r["state"])
            self.assertNotEqual(r["state"], pp.READY_PACKAGE_STALE)
            self.assertIsNotNone(r["last_verified"])     # retained the whole time
        self.assertIn(pp.READY_USABLE, seen)
        self.assertIn(pp.READY_PROOF_STALE, seen)

    def test_missing_package_clears_retained_proof(self):
        self._put()
        self.assertEqual(pp.build_readiness(_readback())["state"], pp.READY_USABLE)
        self.assertIsNotNone(pp.last_verified_proof())
        pp.clear()
        r = pp.build_readiness(_readback())
        self.assertEqual(r["state"], pp.READY_MISSING)
        self.assertIsNone(r["last_verified"])
        self.assertIsNone(pp.last_verified_proof())


if __name__ == "__main__":
    unittest.main(verbosity=2)
