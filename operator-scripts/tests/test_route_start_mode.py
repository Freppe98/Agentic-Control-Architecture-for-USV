"""`route_start_mode`: where EXECUTION begins vs which geometry is APPROVED.

Run from operator-scripts/:  python -m unittest tests.test_route_start_mode   (no pytest)

THE BUG THIS FILE EXISTS FOR
----------------------------
`route_start_mode: first_approach` says the uploaded mission starts at the first approach
waypoint A1 rather than at the planning Home. Generation implemented that by simply NOT building
the Home → A1 connector at all — so the approved transit geometry lost its Home end on the
approach side. The Home corridor is derived from approved transit geometry, and with the Home →
A1 leg missing the approach chain (A1 → … → survey entry) and the Home-anchored return chain
(survey → R1 → … → Home) were two disconnected pieces. Buffering them produced a MultiPolygon,
the single-ring contract refused it, no corridor was emitted, and a mission with a Home outside
the navigable area then failed its own geometry proof:

    HOME_OUTSIDE_APPROVED_GEOMETRY + TRANSIT_OUTSIDE_APPROVED_GEOMETRY

(the documented `HOME_CORRIDOR_DISCONNECTED` limitation, from the corridor's own
"the approved transit geometry is not contiguous" refusal).

The route-start mode may only choose WHERE THE EXECUTED ROUTE BEGINS. It must not change the
safety meaning of the approved Home/transit geometry. So the two are now separate:

    segments                          the EXECUTION route — flattened, hashed, uploaded, flown
    planning_only_transit_segments    APPROVED transit that is deliberately NOT executed
                                      (the Home → A1 leg under `first_approach`)

    execution start != geometry provenance start

WHAT IS PINNED HERE
-------------------
  • both modes produce ONE coherent approved Home↔survey network, and the corridor is derived
    from it rather than from the execution subset;
  • the planning-only leg is real, generated, safety-checked geometry — never fabricated, and
    refused outright when it cannot be routed (no invented corridor, no widened region);
  • it never enters the route, the flattening, the execution order or the route hash;
  • a genuinely disconnected approved chain fails with a code naming WHICH link broke;
  • approach and return stay separate lists — a return is never a silent reversed approach.
"""
import copy
import math
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import planning  # noqa: E402
import replan_package  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# Finalizing writes the durable mission snapshot — point it at a throwaway directory for this
# whole module so a test run can never touch the station's real store.
main.MISSION_STORE_DIR = pathlib.Path(tempfile.mkdtemp(prefix="operator-route-start-"))
main.MISSION_STORE_PATH = main.MISSION_STORE_DIR / "mission_store.json"

SCOUT_VID = 2

requires_geometry = unittest.skipUnless(
    planning.PLANNING_AVAILABLE, "shapely/pyproj/numpy not installed")

# ── the same metric fixture frame the mission-geometry contract tests use ──────────────────
LAT0, LNG0 = 56.6790, 12.8100
_M_PER_DEG_LAT = 111320.0
_M_PER_DEG_LNG = 111320.0 * math.cos(math.radians(LAT0))


def P(east_m, north_m):
    """[lng, lat] for a point `east_m`/`north_m` metres from the fixture origin."""
    return [round(LNG0 + east_m / _M_PER_DEG_LNG, 7), round(LAT0 + north_m / _M_PER_DEG_LAT, 7)]


# A 240 m x 180 m survey. With the default 5 m shoreline clearance the navigable geometry is the
# 230 m x 170 m rectangle inset from it.
BOUNDARY = [P(0, 0), P(240, 0), P(240, 180), P(0, 180)]

# THE CASE THAT USED TO FAIL: a launch/recovery Home 30 m south of the shore, outside the
# navigable geometry, reached by an approach chain and left by a separate return chain.
HOME_OUTSIDE = P(120, -30)
A1, A2 = P(120, -15), P(120, 20)       # approach: outside the inset, then inside the survey
R1, R2 = P(60, 20), P(60, -15)         # return: a DIFFERENT track back toward Home

# A full-width no-go wall SOUTH of the survey. With the 5 m no-go clearance its effective
# exclusion spans north 30 m south … 15 m south, which is a wall nothing may cross: anything left
# on its far side is genuinely cut off from the survey, not merely inconvenient to reach.
WALL = [P(-60, -25), P(300, -25), P(300, -20), P(-60, -20)]


def inputs(**over):
    """Planning inputs at the stated defaults (shoreline 5 m, no-go 5 m, lanes 10 m)."""
    body = {
        "boundary": BOUNDARY,
        "shoreline_clearance_m": planning.DEFAULT_SHORELINE_CLEARANCE_M,
        "no_go_clearance_m": planning.DEFAULT_NO_GO_CLEARANCE_M,
        "lane_spacing_m": planning.DEFAULT_LANE_SPACING_M,
        "primary_angle_deg": 90,
        "dual_pass": False,
        "home": HOME_OUTSIDE,
    }
    body.update(over)
    return body


def kinds(segments):
    return [s["kind"] for s in segments or []]


def first_wp(package):
    wp = package["route_waypoints"][0]
    return [round(wp["longitude"], 7), round(wp["latitude"], 7)]


def last_wp(package):
    wp = package["route_waypoints"][-1]
    return [round(wp["longitude"], 7), round(wp["latitude"], 7)]


def near(a, b, tol_m=2.0):
    """True when two [lng, lat] points are within `tol_m` metres of each other."""
    dx = (a[0] - b[0]) * _M_PER_DEG_LNG
    dy = (a[1] - b[1]) * _M_PER_DEG_LAT
    return math.hypot(dx, dy) <= tol_m


def ring_contains(ring, point):
    """Ray-casting point-in-ring, written here rather than imported so the assertion does not
    lean on the same library the code under test uses."""
    lon, lat = float(point[0]), float(point[1])
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            xin = (x2 - x1) * (lat - y1) / (y2 - y1) + x1
            if lon < xin:
                inside = not inside
    return inside


def codes_of(exc):
    return sorted(set(exc.codes))


# ══════════════════════════════════════════════════════════════════════════════════════════
# 1–2. PLANNING_HOME — the executed route and the approved geometry are the same thing
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestPlanningHomeMode(unittest.TestCase):
    """`planning_home`: execution begins at the planning Home. Every approved transit leg is
    also an executed leg, so there is no planning-only geometry at all — which is exactly what
    makes the new field's EMPTINESS here the assertion that matters."""

    def test_case_1_no_approach_home_to_survey_to_home(self):
        package = planning.generate_survey(inputs(route_start_mode="planning_home"))
        self.assertEqual(package["metrics"]["route_start_mode"], "planning_home")
        self.assertTrue(package["geometry_check"]["ok"],
                        package["geometry_check"]["failures"])
        # Home → survey entry → coverage → Home, with no approach/return waypoints in between.
        self.assertEqual(kinds(package["segments"]),
                         ["survey_entry_connector", "primary", "return_connector"])
        self.assertTrue(near(first_wp(package), HOME_OUTSIDE))
        self.assertTrue(near(last_wp(package), HOME_OUTSIDE))
        self.assertEqual(package["planning_only_transit_segments"], [],
                         "nothing is approved that is not executed in this mode")
        self.assertIsNotNone(package["home_corridor"],
                             package["home_corridor_meta"].get("reason"))

    def test_case_2_approach_and_return_are_executed_and_approved(self):
        package = planning.generate_survey(inputs(
            route_start_mode="planning_home", approach_waypoints=[A1, A2],
            return_waypoints=[R1, R2]))
        self.assertEqual(kinds(package["segments"]),
                         ["start_connector", "approach", "survey_entry_connector", "primary",
                          "return_connector", "return_approach", "final_home_connector"])
        self.assertTrue(near(first_wp(package), HOME_OUTSIDE), "execution begins at Home")
        self.assertTrue(near(last_wp(package), HOME_OUTSIDE), "and ends at Home")
        self.assertEqual(package["planning_only_transit_segments"], [])
        # The approved transit network IS the executed transit legs here.
        approved = planning.approved_transit_segments(
            package["segments"], package["planning_only_transit_segments"])
        self.assertEqual([s["segment_id"] for s in approved],
                         [s["segment_id"] for s in package["segments"]
                          if s["kind"] in planning.HOME_CORRIDOR_SOURCE_KINDS])
        self.assertTrue(package["geometry_check"]["ok"],
                        package["geometry_check"]["failures"])
        self.assertIsNotNone(package["home_corridor"],
                             package["home_corridor_meta"].get("reason"))


# ══════════════════════════════════════════════════════════════════════════════════════════
# 3–5. FIRST_APPROACH — THE REGRESSION. Execution starts at A1; Home stays approved.
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestFirstApproachRegression(unittest.TestCase):
    """THE case that used to fail: Home outside the navigable geometry, approach waypoints,
    `first_approach`, and a return chain. Before the fix this raised
    HOME_OUTSIDE_APPROVED_GEOMETRY + TRANSIT_OUTSIDE_APPROVED_GEOMETRY because the corridor was
    refused as "the approved transit geometry is not contiguous"."""

    def setUp(self):
        self.package = planning.generate_survey(inputs(
            route_start_mode="first_approach", approach_waypoints=[A1, A2],
            return_waypoints=[R1, R2]))

    # ── case 3: execution starts at A1, Home stays approved ────────────────────────────────
    def test_the_previously_failing_mission_now_generates_and_proves_its_geometry(self):
        self.assertTrue(self.package["geometry_check"]["ok"],
                        self.package["geometry_check"]["failures"])
        self.assertEqual(self.package["metrics"]["route_start_mode"], "first_approach")

    def test_execution_begins_at_the_first_approach_waypoint_not_at_home(self):
        self.assertTrue(near(first_wp(self.package), A1),
                        f"the uploaded route must begin at A1, not {first_wp(self.package)}")
        self.assertFalse(near(first_wp(self.package), HOME_OUTSIDE),
                         "Home is NOT silently inserted at the head of the execution route")
        self.assertNotIn("start_connector", kinds(self.package["segments"]),
                         "the Home → A1 leg is not an executed segment in this mode")

    def test_the_return_chain_still_reaches_home(self):
        self.assertEqual(kinds(self.package["segments"])[-3:],
                         ["return_connector", "return_approach", "final_home_connector"])
        self.assertTrue(near(last_wp(self.package), HOME_OUTSIDE),
                        "first_approach moves the START of execution, never its end")

    def test_home_to_A1_is_approved_planning_only_geometry(self):
        planning_only = self.package["planning_only_transit_segments"]
        self.assertEqual(len(planning_only), 1)
        leg = planning_only[0]
        self.assertEqual(leg["kind"], "home_transit_connector")
        self.assertTrue(leg["planning_only"], "flagged on the segment itself")
        self.assertTrue(near(leg["coordinates"][0], HOME_OUTSIDE), "it starts at Home")
        self.assertTrue(near(leg["coordinates"][-1], A1), "and ends at A1")
        self.assertNotIn(leg["segment_id"],
                         [s["segment_id"] for s in self.package["segments"]])

    def test_the_home_corridor_covers_home_A1_and_the_survey_entry_geometry(self):
        corridor = self.package["home_corridor"]
        self.assertIsNotNone(corridor, self.package["home_corridor_meta"].get("reason"))
        meta = self.package["home_corridor_meta"]
        self.assertTrue(meta["contains_planning_home"])
        self.assertTrue(meta["overlaps_navigable"])
        self.assertTrue(meta["covers_transit_path"])
        self.assertEqual(meta["planning_only_source_count"], 1)
        self.assertIn("home_transit_connector", meta["source_segment_kinds"])
        self.assertTrue(ring_contains(corridor, HOME_OUTSIDE), "Home is inside the corridor")
        self.assertTrue(ring_contains(corridor, A1), "A1 is inside the corridor")

    def test_the_corridor_is_not_derivable_from_the_execution_segments_alone(self):
        """The heart of the fix. Deriving from the execution subset — which is what the code
        used to do — still refuses, and names the contiguity failure. Nothing was loosened; the
        SOURCE was corrected to the approved geometry."""
        ring, meta = planning.home_corridor_ring(
            segments=self.package["segments"], navigable_geometry=self.package["navigable_boundary"],
            no_go_zones=[], planning_home=HOME_OUTSIDE,
            no_go_clearance_m=planning.DEFAULT_NO_GO_CLEARANCE_M)
        self.assertIsNone(ring)
        self.assertIn("not contiguous", meta["reason"])

    def test_derivation_stays_deterministic(self):
        again = planning.generate_survey(inputs(
            route_start_mode="first_approach", approach_waypoints=[A1, A2],
            return_waypoints=[R1, R2]))
        self.assertEqual(self.package["home_corridor"], again["home_corridor"])
        self.assertEqual(self.package["route_hash"], again["route_hash"])
        self.assertEqual(self.package["planning_only_transit_segments"],
                         again["planning_only_transit_segments"])

    # ── case 4: the route hash is the uploaded route, and only that ────────────────────────
    def test_case_4_the_route_hash_covers_only_the_executed_route(self):
        # The authoritative proof: flattening the EXECUTION segments alone reproduces the
        # package's route and its hash exactly. A planning-only leg that had leaked into the
        # route would break this equality, whatever else it left intact.
        flat, order = planning._flatten_segments(copy.deepcopy(self.package["segments"]))
        route = planning._route_waypoints(planning._dedup(flat))
        self.assertEqual(route, self.package["route_waypoints"])
        self.assertEqual(planning._route_hash(route), self.package["route_hash"])
        self.assertEqual(len(order), len(self.package["original_execution_order"]))

    def test_case_4_no_execution_order_entry_comes_from_a_planning_only_segment(self):
        planning_ids = {s["segment_id"] for s in self.package["planning_only_transit_segments"]}
        sources = {e["source_segment_id"] for e in self.package["original_execution_order"]}
        self.assertTrue(planning_ids)
        self.assertEqual(planning_ids & sources, set())
        self.assertTrue(all(sid.startswith("seg-") for sid in sources))

    def test_case_4_route_quality_describes_the_uploaded_route_only(self):
        # A planning-only connector must not inflate the "waypoints removed by cleanup" figures,
        # which are a statement about the route the operator is about to upload.
        q = self.package["route_quality"]
        self.assertEqual(q["final_waypoint_count"], len(self.package["route_waypoints"]))
        self.assertGreaterEqual(q["removed_waypoint_count"], 0)

    # ── case 5: Home outside navigable geometry, finalized ─────────────────────────────────
    def test_case_5_home_is_outside_the_navigable_geometry_and_still_approved(self):
        checks = self.package["geometry_check"]["checks"]
        self.assertTrue(checks["home_within_approved"])
        self.assertTrue(checks["corridor_overlaps_navigable"])
        self.assertTrue(checks["corridor_contains_home"])
        self.assertTrue(checks["corridor_covers_transit"])
        self.assertEqual(checks["planning_only_transit_segment_count"], 1)
        # …and it really is outside the navigable geometry — otherwise this proves nothing.
        bare = planning.check_mission_geometry(
            segments=self.package["segments"],
            planning_only_transit_segments=self.package["planning_only_transit_segments"],
            route_waypoints=self.package["route_waypoints"],
            navigable_geometry=self.package["navigable_boundary"], no_go_zones=[],
            no_go_clearance_m=planning.DEFAULT_NO_GO_CLEARANCE_M,
            planning_home=HOME_OUTSIDE, home_corridor=None)
        self.assertFalse(bare["ok"])
        self.assertIn("HOME_OUTSIDE_APPROVED_GEOMETRY", [f["code"] for f in bare["failures"]])

    def test_case_13_a_first_approach_mission_finalizes(self):
        client = TestClient(main.app)
        resp = client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": self.package, "confirm": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        record = main.original_missions[resp.json()["mission"]["mission_id"]]
        # The record makes the distinction auditable rather than leaving it to be re-derived.
        self.assertEqual(record["planning_only_transit_segments"],
                         self.package["planning_only_transit_segments"])
        self.assertEqual(record["segments"], self.package["segments"])
        self.assertEqual(record["planning_inputs"]["route_start_mode"], "first_approach")
        self.assertEqual(record["route_waypoints"], self.package["route_waypoints"])
        self.assertTrue(planning.check_package_geometry(record)["ok"])

    def test_case_14_the_record_yields_a_corridor_usable_for_retrace_approved(self):
        client = TestClient(main.app)
        resp = client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": self.package, "confirm": True})
        self.assertEqual(resp.status_code, 200, resp.text)
        record = main.original_missions[resp.json()["mission"]["mission_id"]]
        pkg, meta = replan_package.build_v1_package(record, vehicle_id=SCOUT_VID)
        self.assertTrue(meta["home_corridor_supplied"])
        self.assertEqual(pkg["home_corridor"], self.package["home_corridor"])
        self.assertTrue(ring_contains(pkg["home_corridor"], HOME_OUTSIDE))
        # The wire package still carries the EXECUTION route and only that.
        self.assertEqual(pkg["route_hash"], self.package["route_hash"])
        self.assertEqual(len(pkg["segments"]), len(self.package["segments"]))
        self.assertNotIn("planning_only_transit_segments", pkg,
                         "the v1 Scout contract is unchanged — the corridor already carries it")

    def test_case_14_a_record_rederives_the_same_corridor_from_its_stored_geometry(self):
        # Strip the stored ring so derivation runs on the record's own segments, the way a
        # historical record is handled — the answer must be the same corridor, not a refusal.
        record = {
            "segments": self.package["segments"],
            "planning_only_transit_segments": self.package["planning_only_transit_segments"],
            "navigable_geometry": self.package["navigable_boundary"],
            "no_go_zones": [],
            "planning_inputs": self.package["planning_inputs"],
        }
        ring, meta = replan_package.derive_home_corridor(record)
        self.assertIsNotNone(ring, meta.get("reason"))
        self.assertEqual(ring, self.package["home_corridor"])


@requires_geometry
class TestFirstApproachRouteIdentity(unittest.TestCase):
    """Mission identity: the mode affects the route and its hash exactly when — and only when —
    it changes the uploaded waypoint sequence."""

    def test_the_two_modes_differ_only_by_the_home_to_A1_leg(self):
        common = dict(approach_waypoints=[A1, A2], return_waypoints=[R1, R2])
        home_first = planning.generate_survey(inputs(route_start_mode="planning_home", **common))
        approach_first = planning.generate_survey(
            inputs(route_start_mode="first_approach", **common))
        self.assertNotEqual(home_first["route_hash"], approach_first["route_hash"],
                            "the uploaded sequence really does differ, so the hash must")
        # The first_approach route is the planning_home route with its leading Home→A1 run
        # removed — the tail (survey + return + Home) is byte-identical.
        tail = [(w["latitude"], w["longitude"]) for w in approach_first["route_waypoints"]]
        full = [(w["latitude"], w["longitude"]) for w in home_first["route_waypoints"]]
        self.assertEqual(full[-len(tail) + 1:], tail[1:],
                         "everything from A1 onward is the same executed geometry")
        self.assertLess(len(tail), len(full))

    def test_the_mode_alone_does_not_move_the_hash_when_it_cannot_apply(self):
        # No approach waypoints: `first_approach` has nothing to start from and falls back to the
        # planning Home. Same executed route, therefore the same identity.
        a = planning.generate_survey(inputs(route_start_mode="planning_home"))
        b = planning.generate_survey(inputs(route_start_mode="first_approach"))
        self.assertEqual(a["route_hash"], b["route_hash"])
        self.assertEqual(b["metrics"]["route_start_mode"], "planning_home")


# ══════════════════════════════════════════════════════════════════════════════════════════
# 6–8. A GENUINELY DISCONNECTED APPROVED CHAIN FAILS CLEARLY
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestDisconnectedApprovedChain(unittest.TestCase):
    """When the operator's transit geometry cannot form ONE approved connected Home↔survey
    network, generation refuses with a code naming the broken link. Nothing is repaired: no
    corridor is invented, the raw boundary is never substituted, the no-go buffer is never
    tunnelled through, and no operator waypoint is moved or replaced."""

    def test_case_6_a_no_go_wall_between_home_and_A1_fails_closed(self):
        with self.assertRaises(planning.GeometryConsistencyError) as ctx:
            planning.generate_survey(inputs(
                home=P(120, -40), approach_waypoints=[P(120, -10), A2],
                return_waypoints=[R1], no_go_zones=[WALL],
                route_start_mode="first_approach"))
        self.assertEqual(codes_of(ctx.exception), ["HOME_TO_APPROACH_DISCONNECTED"])
        self.assertIn("planning Home", str(ctx.exception))

    def test_case_6_the_same_wall_fails_in_planning_home_mode_too(self):
        # The refusal is a property of the APPROVED geometry, not of the execution mode — the
        # Home → A1 leg is required in both, so both refuse identically.
        with self.assertRaises(planning.GeometryConsistencyError) as ctx:
            planning.generate_survey(inputs(
                home=P(120, -40), approach_waypoints=[P(120, -10), A2],
                return_waypoints=[R1], no_go_zones=[WALL],
                route_start_mode="planning_home"))
        self.assertEqual(codes_of(ctx.exception), ["HOME_TO_APPROACH_DISCONNECTED"])

    def test_case_6_no_corridor_is_invented_for_the_refused_geometry(self):
        # Whatever the operator drew, a corridor may only ever be derived from transit legs this
        # station routed and validated. With none, the answer is None and a named reason.
        ring, meta = planning.home_corridor_ring(
            segments=[], planning_only_transit_segments=[],
            navigable_geometry=planning._navigable_rings_deg(
                BOUNDARY, planning.DEFAULT_SHORELINE_CLEARANCE_M),
            no_go_zones=[WALL], planning_home=P(120, -40),
            no_go_clearance_m=planning.DEFAULT_NO_GO_CLEARANCE_M)
        self.assertIsNone(ring)
        self.assertIn("no approved transit", meta["reason"])

    def test_case_7_an_approach_that_cannot_reach_the_survey_fails_closed(self):
        with self.assertRaises(planning.GeometryConsistencyError) as ctx:
            planning.generate_survey(inputs(
                home=P(120, -45), approach_waypoints=[P(120, -40)], no_go_zones=[WALL],
                route_start_mode="first_approach"))
        self.assertEqual(codes_of(ctx.exception), ["APPROACH_TO_SURVEY_DISCONNECTED"])

    def test_case_8_a_return_chain_cut_off_from_the_survey_fails_closed(self):
        with self.assertRaises(planning.GeometryConsistencyError) as ctx:
            planning.generate_survey(inputs(
                home=P(120, -10), approach_waypoints=[P(120, -8), A2],
                return_waypoints=[P(60, -45)], no_go_zones=[WALL],
                route_start_mode="first_approach"))
        self.assertEqual(codes_of(ctx.exception), ["SURVEY_TO_RETURN_DISCONNECTED"])

    def test_the_chain_codes_are_registered_operator_geometry_codes(self):
        # RETURN_TO_HOME_DISCONNECTED guards the final Home leg. It is the mirror of the three
        # above and is not separately reachable through generate_survey today: the Home →
        # approach (or Home → survey entry) connector is built FIRST and already proves Home is
        # reachable from the navigable region, so a later Home leg cannot be the first to fail.
        # The guard stays because that raise site is real — without it the one remaining way it
        # can fire would surface as an uncoded ConnectorError while its three siblings are coded.
        for code in ("HOME_TO_APPROACH_DISCONNECTED", "APPROACH_TO_SURVEY_DISCONNECTED",
                     "SURVEY_TO_RETURN_DISCONNECTED", "RETURN_TO_HOME_DISCONNECTED"):
            self.assertIn(code, planning.GEOMETRY_ERROR_CODES)

    def test_a_refused_mission_produces_no_package_at_all(self):
        # Fail CLOSED: the caller gets an exception, never a package with a warning attached.
        for mode in planning.ROUTE_START_MODES:
            with self.assertRaises(planning.GeometryConsistencyError):
                planning.generate_survey(inputs(
                    home=P(120, -40), approach_waypoints=[P(120, -10), A2],
                    no_go_zones=[WALL], route_start_mode=mode))


# ══════════════════════════════════════════════════════════════════════════════════════════
# 9–10. APPROACH AND RETURN STAY SEPARATE
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestApproachAndReturnRemainIndependent(unittest.TestCase):

    def test_case_9_an_explicit_return_is_never_replaced_by_the_reversed_approach(self):
        package = planning.generate_survey(inputs(
            route_start_mode="first_approach", approach_waypoints=[A1, A2],
            return_waypoints=[R1, R2]))
        echoed = package["planning_inputs"]
        self.assertEqual(echoed["approach_waypoints"], [A1, A2])
        self.assertEqual(echoed["return_waypoints"], [R1, R2])
        self.assertNotEqual(echoed["return_waypoints"], list(reversed(echoed["approach_waypoints"])))
        # The RETURN geometry runs along the operator's own R track, not back down the approach.
        ret = [s for s in package["segments"] if s["kind"] == "return_approach"][0]
        for lng, _lat in ret["coordinates"]:
            self.assertLess(abs(lng - R1[0]), abs(lng - A1[0]) + 1e-9,
                            "the return leg follows R1/R2, not a reversed A1/A2")

    def test_case_9_omitting_the_return_does_not_synthesize_one_from_the_approach(self):
        package = planning.generate_survey(inputs(
            route_start_mode="first_approach", approach_waypoints=[A1, A2]))
        self.assertEqual(package["planning_inputs"]["return_waypoints"], [])
        self.assertNotIn("return_approach", kinds(package["segments"]),
                         "no return polyline is invented when the operator defined none")
        # A generated direct connector back to Home is the documented behaviour, and it is a
        # CONNECTOR, not a reversed copy of the operator's approach waypoints.
        self.assertIn("return_connector", kinds(package["segments"]))
        self.assertTrue(near(last_wp(package), HOME_OUTSIDE))

    def test_case_10_use_reversed_approach_is_an_ordinary_return_list(self):
        # The Plan page's explicit "Use reversed approach" action populates return_waypoints with
        # the reversed approach. The backend has no special path for it — it validates exactly
        # like any other operator-authored return list, in both modes.
        reversed_approach = [A2, A1]
        for mode in planning.ROUTE_START_MODES:
            package = planning.generate_survey(inputs(
                route_start_mode=mode, approach_waypoints=[A1, A2],
                return_waypoints=reversed_approach))
            self.assertTrue(package["geometry_check"]["ok"],
                            (mode, package["geometry_check"]["failures"]))
            self.assertEqual(package["planning_inputs"]["return_waypoints"], reversed_approach)
            self.assertIsNotNone(package["home_corridor"],
                                 package["home_corridor_meta"].get("reason"))
            self.assertTrue(near(last_wp(package), HOME_OUTSIDE))


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE PLANNING-ONLY LEG IS HELD TO THE SAME SAFETY RULES AS AN EXECUTED ONE
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestPlanningOnlyGeometryIsChecked(unittest.TestCase):
    """Approving an UNCHECKED leg is how a corridor would come to cover water nothing validated.
    The planning-only leg goes through the same containment and no-go sweep as an executed one,
    and `validate_plan` / finalize re-prove it from the submitted package."""

    def setUp(self):
        self.package = planning.generate_survey(inputs(
            route_start_mode="first_approach", approach_waypoints=[A1, A2],
            return_waypoints=[R1, R2]))
        self.navigable = self.package["navigable_boundary"]

    def test_a_tampered_planning_only_leg_is_caught_by_the_geometry_proof(self):
        tampered = copy.deepcopy(self.package["planning_only_transit_segments"])
        # Drag the leg's Home end 400 m out to sea — well outside anything approved.
        tampered[0]["coordinates"][0] = P(1200, -600)
        report = planning.check_mission_geometry(
            segments=self.package["segments"], planning_only_transit_segments=tampered,
            route_waypoints=self.package["route_waypoints"],
            navigable_geometry=self.navigable, no_go_zones=[],
            no_go_clearance_m=planning.DEFAULT_NO_GO_CLEARANCE_M,
            planning_home=HOME_OUTSIDE, home_corridor=self.package["home_corridor"])
        self.assertFalse(report["ok"])
        self.assertIn("TRANSIT_OUTSIDE_APPROVED_GEOMETRY", [f["code"] for f in report["failures"]])
        # The offender is named as planning-only, so the operator knows which of the two it is.
        offending = [f for f in report["failures"]
                     if f["code"] == "TRANSIT_OUTSIDE_APPROVED_GEOMETRY"][0]
        self.assertIn("planning-only", offending["message"])

    def test_validate_plan_reproves_a_first_approach_package(self):
        body = {**inputs(route_start_mode="first_approach", approach_waypoints=[A1, A2],
                         return_waypoints=[R1, R2]),
                "segments": self.package["segments"],
                "planning_only_transit_segments": self.package["planning_only_transit_segments"],
                "route_waypoints": self.package["route_waypoints"],
                "route_hash": self.package["route_hash"]}
        report = planning.validate_plan(body, max_route_waypoints=1000)
        self.assertTrue(report["ok"], report["errors"])
        self.assertTrue(report["checks"]["geometry_consistent"])

    def test_validate_plan_without_the_planning_only_leg_cannot_prove_the_corridor(self):
        # Not a demand that callers send it — a demonstration that the field is load-bearing, so
        # a caller that drops it gets a refusal rather than a quietly weaker proof.
        body = {**inputs(route_start_mode="first_approach", approach_waypoints=[A1, A2],
                         return_waypoints=[R1, R2]),
                "segments": self.package["segments"],
                "route_waypoints": self.package["route_waypoints"]}
        report = planning.validate_plan(body, max_route_waypoints=1000)
        self.assertFalse(report["ok"])
        self.assertIn("HOME_OUTSIDE_APPROVED_GEOMETRY", report["checks"]["geometry_codes"])

    def test_finalize_refuses_a_package_whose_planning_only_leg_was_edited(self):
        client = TestClient(main.app)
        tampered = copy.deepcopy(self.package)
        tampered["planning_only_transit_segments"][0]["coordinates"][0] = P(1200, -600)
        resp = client.post("/api/missions/finalize", json={
            "vehicle_id": SCOUT_VID, "mission_package": tampered, "confirm": True})
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertEqual(resp.json()["error"], "mission_geometry_inconsistent")
        self.assertIn("TRANSIT_OUTSIDE_APPROVED_GEOMETRY", resp.json()["codes"])


# ══════════════════════════════════════════════════════════════════════════════════════════
# BACKWARD COMPATIBILITY
# ══════════════════════════════════════════════════════════════════════════════════════════

@requires_geometry
class TestHistoricalRecordsAreUnaffected(unittest.TestCase):
    """A mission planned before `planning_only_transit_segments` existed has none — its approved
    transit geometry simply IS its execution transit geometry, exactly as before. Nothing is
    invented for it and nothing is retro-tightened."""

    def test_a_record_without_the_field_derives_its_corridor_as_before(self):
        package = planning.generate_survey(inputs(
            route_start_mode="planning_home", approach_waypoints=[A1, A2],
            return_waypoints=[R1, R2]))
        record = {
            "segments": package["segments"],
            "navigable_geometry": package["navigable_boundary"],
            "no_go_zones": [],
            "planning_inputs": package["planning_inputs"],
        }
        self.assertNotIn("planning_only_transit_segments", record)
        ring, meta = replan_package.derive_home_corridor(record)
        self.assertIsNotNone(ring, meta.get("reason"))
        self.assertEqual(ring, package["home_corridor"])

    def test_mission_geometry_arguments_reads_a_missing_field_as_empty(self):
        args = planning.mission_geometry_arguments({"segments": [], "route_waypoints": []})
        self.assertEqual(args["planning_only_transit_segments"], [])

    def test_approved_transit_segments_of_a_legacy_package_is_its_transit_legs(self):
        package = planning.generate_survey(inputs(route_start_mode="planning_home"))
        approved = planning.approved_transit_segments(package["segments"], None)
        self.assertEqual([s["kind"] for s in approved],
                         [s["kind"] for s in package["segments"]
                          if s["kind"] in planning.HOME_CORRIDOR_SOURCE_KINDS])


if __name__ == "__main__":
    unittest.main()
