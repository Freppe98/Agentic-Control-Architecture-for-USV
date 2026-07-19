"""Backend tests for mission-contract-v1 — the Operator/Scout mission upload contract.

Run from operator-scripts/:  python -m unittest tests.test_mission_contract

Four independent things are tested here:

1. REQUEST CANONICALIZATION (TestMissionUploadRequestSchema). The operator supplies ROUTE
   waypoints only. main.canonical_mission_upload_params validates them, refuses the fields
   Scout owns (seq / command / frame / altitude), and derives BOTH counts the read-back is
   judged against: expected_route_waypoint_count = N and expected_pixhawk_item_count = N+1
   (Scout prepends Home at seq 0).

2. RESULT CLASSIFICATION (TestMissionUploadResultClassification). Status EXECUTED means
   only "the Local Agent completed the attempt against Scout". A verified upload requires
   accepted + uploaded + verified + both counts matching + the route content hash matching.
   The hash compared is Scout's ROUTE hash over items 1…N, never its full-mission hash
   (which includes the Home the operator never sent). A MISSING hash is a failure, not a
   count-only pass — matching counts cannot detect two swapped waypoints.

3. PROVENANCE (TestCommandSourceIsServerOwned). `source` is server-owned: every command
   created through the browser-facing POST /api/commands is stamped OPERATOR, and a
   body-supplied LOCAL_AGENT / MISSION_AGENT cannot spoof it. The thesis's authority
   analysis rests on these records, so a client-settable source would invalidate them.

4. MISSION_CLEAR (TestMissionClear). Queueable and verified against an independent empty
   read-back, accepting BOTH ArduPilot empty representations (NO_ITEMS and HOME_ONLY).

5. ROUTE CONTENT HASH (TestRouteContentHash). The canonicalization in mission_contract.py,
   pinned against Scout's golden hash, plus the properties that make it a real proof:
   position, ordering and loiter all change the digest; Home is excluded; int and float
   spellings of the same number canonicalize identically.
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "mission-contract-v1.json")
with open(FIXTURE_PATH, encoding="utf-8") as fh:
    FIXTURE = json.load(fh)

# The canonical two-point route from the contract brief.
ROUTE_REQUEST = FIXTURE["request"]


def _reset_commands():
    main.commands.clear()
    main.commands_by_id.clear()


class TestMissionUploadRequestSchema(unittest.TestCase):
    """The operator sends route waypoints only; the backend derives N and N+1."""

    def test_two_route_points_give_route_2_and_pixhawk_3(self):
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        self.assertEqual(params["expected_route_waypoint_count"], 2)
        self.assertEqual(params["expected_pixhawk_item_count"], 3)
        self.assertEqual(params["expected_pixhawk_item_count"],
                         params["expected_route_waypoint_count"] + 1)
        self.assertEqual(params["contract_version"], "mission-contract-v1")

    def test_matches_the_shared_fixture(self):
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        for key, want in FIXTURE["expected"].items():
            self.assertEqual(params[key], want, key)

    def test_no_seq_zero_home_is_ever_generated(self):
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        self.assertEqual(len(params["waypoints"]), 2)
        for wp in params["waypoints"]:
            self.assertEqual(set(wp), {"latitude", "longitude", "loiter_time_s"})
            self.assertNotIn("seq", wp)

    def test_loiter_time_defaults_to_zero(self):
        params = main.canonical_mission_upload_params(
            {"waypoints": [{"latitude": 56.65, "longitude": 12.87}]})
        self.assertEqual(params["waypoints"][0]["loiter_time_s"], 0)

    def test_expected_route_content_hash_is_the_scout_golden_value(self):
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        self.assertEqual(params["expected_route_content_hash"],
                         FIXTURE["scout_golden_route_hash"])

    def test_canonical_params_shape_is_exactly_the_contract(self):
        # No leftover placeholder keys: route_content_hash_status described a gap that no
        # longer exists, and a stale status field would keep the UI explaining an absence.
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        self.assertEqual(set(params), {
            "contract_version", "waypoints",
            "expected_route_waypoint_count", "expected_pixhawk_item_count",
            "expected_route_content_hash"})

    def test_rejects_scout_owned_fields_naming_each(self):
        with self.assertRaises(main.MissionContractError) as ctx:
            main.canonical_mission_upload_params({"waypoints": [
                {"latitude": 56.65, "longitude": 12.87,
                 "seq": 0, "command": 16, "frame": 3, "altitude": 12},
            ]})
        joined = " ".join(ctx.exception.errors)
        for field in ("seq", "command", "frame", "altitude"):
            self.assertIn(field, joined)

    def test_rejects_the_old_operator_schema_wholesale(self):
        with self.assertRaises(main.MissionContractError):
            main.canonical_mission_upload_params(
                {"waypoints": [{"seq": 0, "command": 16, "lat": 56.65, "lng": 12.87, "alt": 0}]})

    def test_rejects_out_of_range_and_non_numeric_coordinates(self):
        for bad in ({"latitude": 999, "longitude": 12.87},
                    {"latitude": "56.65", "longitude": 12.87},
                    {"latitude": 56.65, "longitude": None}):
            with self.assertRaises(main.MissionContractError):
                main.canonical_mission_upload_params({"waypoints": [bad]})

    def test_rejects_an_empty_or_missing_route(self):
        for bad in ({"waypoints": []}, {}, {"waypoints": "nope"}):
            with self.assertRaises(main.MissionContractError):
                main.canonical_mission_upload_params(bad)

    def test_rejects_a_foreign_contract_version(self):
        with self.assertRaises(main.MissionContractError):
            main.canonical_mission_upload_params(
                {"contract_version": "mission-contract-v2",
                 "waypoints": [{"latitude": 56.65, "longitude": 12.87}]})

    def test_one_bad_waypoint_refuses_the_whole_route(self):
        # Uploading "most of" a route to a flight controller is worse than uploading none.
        with self.assertRaises(main.MissionContractError):
            main.canonical_mission_upload_params({"waypoints": [
                {"latitude": 56.65, "longitude": 12.87},
                {"latitude": 999, "longitude": 12.88},
            ]})


class TestMissionUploadEndpoint(unittest.TestCase):
    """POST /api/commands stores the canonical params, or refuses with every error."""

    def setUp(self):
        _reset_commands()
        self.client = TestClient(main.app)

    def test_queued_command_carries_both_expected_counts(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD",
            "params": ROUTE_REQUEST, "confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        params = r.json()["command"]["params"]
        self.assertEqual(params["expected_route_waypoint_count"], 2)
        self.assertEqual(params["expected_pixhawk_item_count"], 3)
        self.assertEqual(len(params["waypoints"]), 2)

    def test_contract_violation_is_a_400_listing_every_error(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "confirm": True,
            "params": {"waypoints": [{"latitude": 56.65, "longitude": 12.87,
                                      "command": 16, "altitude": 5}]}})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertEqual(body["error"], "mission_contract_violation")
        self.assertGreaterEqual(len(body["errors"]), 2)
        self.assertEqual(len(main.commands), 0, "a refused mission must not be queued")


class TestMissionUploadResultClassification(unittest.TestCase):
    """Verified requires accepted + verified + both counts + the route content hash."""

    def _cmd(self, params=None, result=None):
        cmd = {
            "type": "MISSION_UPLOAD", "status": "EXECUTED",
            "params": params if params is not None else main.canonical_mission_upload_params(ROUTE_REQUEST),
            "result": result, "reason": None,
        }
        main._annotate_mission_upload_result(cmd)
        return cmd

    def test_accepted_verified_matching_counts_is_verified(self):
        cmd = self._cmd(result=dict(FIXTURE["verified_result"]))
        self.assertEqual(cmd["mission_result"], "verified")

    def test_route_waypoint_count_mismatch_fails(self):
        cmd = self._cmd(result={"accepted": True, "verified": True,
                                "observed_route_waypoint_count": 1,
                                "observed_pixhawk_item_count": 3})
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("route waypoints", cmd["reason"])

    def test_pixhawk_item_count_mismatch_fails_even_when_route_count_agrees(self):
        # 2 route waypoints but only 2 items on the FC: Scout's Home is missing. Checking
        # the route count alone would have called this a success.
        cmd = self._cmd(result={"accepted": True, "verified": True,
                                "observed_route_waypoint_count": 2,
                                "observed_pixhawk_item_count": 2})
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("items", cmd["reason"])

    def test_route_content_hash_mismatch_fails(self):
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        params["expected_route_content_hash"] = "sha256:aaa"
        cmd = self._cmd(params=params, result={
            "accepted": True, "verified": True,
            "observed_route_waypoint_count": 2, "observed_pixhawk_item_count": 3,
            "observed_route_content_hash": "sha256:bbb"})
        self.assertEqual(cmd["mission_result"], "failed")

    def test_full_mission_hash_is_never_substituted_for_the_route_hash(self):
        # `hash` / `full_mission_hash` cover Home too, so they are a DIFFERENT value over
        # different bytes. Accepting one as the route hash would manufacture a content
        # proof out of a number that was never compared to anything. With no genuine
        # observed route hash present, this upload is unverifiable — not verified.
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        for field in ("hash", "full_mission_hash"):
            cmd = self._cmd(params=params, result={
                "accepted": True, "verified": True,
                "observed_route_waypoint_count": 2, "observed_pixhawk_item_count": 3,
                field: params["expected_route_content_hash"]})
            self.assertEqual(cmd["mission_result"], "failed", field)
            self.assertIn("observed_route_content_hash", cmd["reason"])

    def test_missing_observed_route_hash_is_a_failure_not_count_only_success(self):
        # The defect this whole contract exists to prevent: both counts agree, so a
        # count-only check would render a green "Verified" for a route whose contents were
        # never compared at all.
        cmd = self._cmd(result={
            "accepted": True, "uploaded": True, "verified": True,
            "observed_route_waypoint_count": 2, "observed_pixhawk_item_count": 3})
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("do not prove", cmd["reason"])

    def test_missing_expected_route_hash_is_a_failure(self):
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        params["expected_route_content_hash"] = None
        cmd = self._cmd(params=params, result=dict(FIXTURE["verified_result"]))
        self.assertEqual(cmd["mission_result"], "failed")

    def test_explicit_uploaded_false_fails(self):
        result = dict(FIXTURE["verified_result"])
        result["uploaded"] = False
        self.assertEqual(self._cmd(result=result)["mission_result"], "failed")

    def test_absent_uploaded_field_is_not_held_against_scout(self):
        # `uploaded` is optional in the contract; absence is not evidence of a failed write.
        result = dict(FIXTURE["verified_result"])
        result.pop("uploaded")
        self.assertEqual(self._cmd(result=result)["mission_result"], "verified")

    def test_not_accepted_or_not_verified_fails(self):
        self.assertEqual(self._cmd(result={"accepted": False, "verified": True})["mission_result"], "failed")
        self.assertEqual(self._cmd(result={"accepted": True, "verified": False})["mission_result"], "failed")

    def test_transport_success_alone_is_never_verified(self):
        self.assertEqual(self._cmd(result={})["mission_result"], "failed")
        self.assertEqual(self._cmd(result=None)["mission_result"], "failed")

    def test_verification_block_reports_pixhawk_item_counts(self):
        cmd = {
            "type": "MISSION_UPLOAD", "status": "EXECUTED",
            "params": main.canonical_mission_upload_params(ROUTE_REQUEST),
            "result": dict(FIXTURE["verified_result"]), "reason": None,
        }
        main._annotate_mission_upload_result(cmd)
        ver = main.build_command_verification(cmd)
        self.assertTrue(ver["verified"])
        self.assertEqual(ver["outcome"], "VERIFIED")
        self.assertEqual(ver["expected"], "3 Pixhawk items")
        self.assertEqual(ver["observed"], "3 Pixhawk items")


class TestCommandSourceIsServerOwned(unittest.TestCase):
    """A browser cannot attribute its own command to the autonomy."""

    def setUp(self):
        _reset_commands()
        self.client = TestClient(main.app)

    def _post(self, **extra):
        body = {"vehicle_id": SCOUT_VID, "type": "SET_MODE_LOITER", "confirm": True}
        body.update(extra)
        return self.client.post("/api/commands", json=body)

    def test_browser_source_mission_agent_cannot_spoof_provenance(self):
        r = self._post(source="MISSION_AGENT")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["command"]["source"], "OPERATOR")

    def test_browser_source_local_agent_cannot_spoof_provenance(self):
        r = self._post(source="LOCAL_AGENT")
        self.assertEqual(r.json()["command"]["source"], "OPERATOR")

    def test_created_by_cannot_smuggle_a_source_either(self):
        # normalize_source() infers a source from free text, so a created_by of
        # "local_agent" would previously have produced a LOCAL_AGENT record.
        r = self._post(created_by="local_agent autonomy")
        self.assertEqual(r.json()["command"]["source"], "OPERATOR")

    def test_a_mission_upload_is_also_stamped_operator(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "confirm": True,
            "source": "MISSION_AGENT", "params": ROUTE_REQUEST})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["command"]["source"], "OPERATOR")

    def test_the_agent_facing_view_forwards_the_server_owned_source(self):
        self._post(source="LOCAL_AGENT")
        r = self.client.get(f"/agent/commands?usv_id=usv-{SCOUT_VID}")
        self.assertEqual(r.json()["commands"][0]["source"], "OPERATOR")

    def test_normalize_source_still_serves_trusted_backend_callers(self):
        # Provenance is not hard-wired to OPERATOR everywhere — only the browser-facing
        # endpoint is. A trusted internal caller can still author an autonomy record.
        self.assertEqual(main.normalize_source("MISSION_AGENT"), "MISSION_AGENT")
        self.assertEqual(main.normalize_source("LOCAL_AGENT"), "LOCAL_AGENT")


class TestMissionClear(unittest.TestCase):
    """MISSION_CLEAR is queueable and judged by an independent empty read-back."""

    def setUp(self):
        _reset_commands()
        self.client = TestClient(main.app)

    def _clear(self, result):
        cmd = {"type": "MISSION_CLEAR", "status": "EXECUTED", "params": {},
               "result": result, "reason": None}
        main._annotate_mission_upload_result(cmd)
        return cmd

    def test_mission_clear_is_queueable(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_CLEAR", "params": {}, "confirm": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["command"]["type"], "MISSION_CLEAR")
        self.assertEqual(len(main.commands), 1)

    def test_capabilities_endpoint_declares_it_supported(self):
        r = self.client.get("/api/commands/capabilities")
        self.assertEqual(r.status_code, 200)
        caps = r.json()["commands"]
        self.assertTrue(caps["MISSION_CLEAR"]["supported"])
        self.assertIsNone(caps["MISSION_CLEAR"]["reason"])
        self.assertTrue(caps["MISSION_UPLOAD"]["supported"])

    def test_no_items_representation_verifies(self):
        self.assertEqual(
            self._clear(dict(FIXTURE["clear_result_no_items"]))["mission_result"], "verified")

    def test_home_only_representation_verifies(self):
        # Item count 1 with route count 0: ArduPilot retained Home at seq 0. That is a
        # correctly cleared mission, so requiring item count == 0 would fail a real clear.
        cmd = self._clear(dict(FIXTURE["clear_result_home_only"]))
        self.assertEqual(cmd["mission_result"], "verified")
        self.assertEqual(cmd["result"]["observed_pixhawk_item_count"], 1)

    def test_a_remaining_route_fails(self):
        cmd = self._clear({"accepted": True, "cleared": True, "verified": True,
                           "observed_route_waypoint_count": 2,
                           "observed_pixhawk_item_count": 3,
                           "empty_representation": "NO_ITEMS"})
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("route waypoints", cmd["reason"])

    def test_cleared_false_fails_even_when_the_readback_looks_empty(self):
        result = dict(FIXTURE["clear_result_no_items"])
        result["cleared"] = False
        self.assertEqual(self._clear(result)["mission_result"], "failed")

    def test_unrecognised_empty_representation_fails(self):
        result = dict(FIXTURE["clear_result_no_items"])
        result["empty_representation"] = "PROBABLY_EMPTY"
        cmd = self._clear(result)
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("empty representation", cmd["reason"])

    def test_missing_observed_route_count_fails(self):
        result = dict(FIXTURE["clear_result_no_items"])
        result.pop("observed_route_waypoint_count")
        self.assertEqual(self._clear(result)["mission_result"], "failed")


class TestRouteContentHash(unittest.TestCase):
    """The canonicalization itself — pinned to Scout, and proven to be sensitive to the
    things a route hash must be sensitive to."""

    ROUTE = ROUTE_REQUEST["waypoints"]

    def test_matches_scouts_pinned_golden_hash(self):
        # THE cross-system assertion. If this fails, the Operator and Scout no longer agree
        # on what a route is, and every "Verified" upload is meaningless.
        self.assertEqual(mission_contract.route_content_hash(self.ROUTE),
                         FIXTURE["scout_golden_route_hash"])

    def test_canonical_json_is_byte_exact(self):
        self.assertEqual(mission_contract.canonical_route_json(self.ROUTE),
                         FIXTURE["canonical_json"])

    def test_canonical_items_match_the_fixture(self):
        self.assertEqual(mission_contract.canonical_route_items(self.ROUTE),
                         FIXTURE["canonical_items"])

    def test_sequence_is_one_based_and_home_is_excluded(self):
        # Home is Scout's, at seq 0. A route item is never sequence 0, and the hashed list
        # holds exactly N items for an N-waypoint route — no Home smuggled in.
        items = mission_contract.canonical_route_items(self.ROUTE)
        self.assertEqual([i["sequence"] for i in items], [1, 2])
        self.assertEqual(len(items), len(self.ROUTE))

    def test_a_hash_over_a_route_with_home_prepended_differs(self):
        # Concrete form of "Home is excluded": if Home leaked into the hashed route, the
        # digest would be this other value, and Scout would never match it.
        home = {"latitude": 56.6400, "longitude": 12.8600, "loiter_time_s": 0}
        self.assertNotEqual(mission_contract.route_content_hash([home] + list(self.ROUTE)),
                            FIXTURE["scout_golden_route_hash"])

    def test_moving_a_waypoint_changes_the_hash(self):
        moved = [dict(self.ROUTE[0], latitude=56.6502), self.ROUTE[1]]
        self.assertNotEqual(mission_contract.route_content_hash(moved),
                            FIXTURE["scout_golden_route_hash"])

    def test_reordering_the_route_changes_the_hash(self):
        # The case counts provably cannot catch: same two waypoints, same N, same N+1,
        # different route. This is the reason the hash exists.
        swapped = [self.ROUTE[1], self.ROUTE[0]]
        self.assertNotEqual(mission_contract.route_content_hash(swapped),
                            FIXTURE["scout_golden_route_hash"])

    def test_changing_loiter_changes_the_hash(self):
        reloitered = [self.ROUTE[0], dict(self.ROUTE[1], loiter_time_s=45)]
        self.assertNotEqual(mission_contract.route_content_hash(reloitered),
                            FIXTURE["scout_golden_route_hash"])

    def test_int_and_float_spellings_canonicalize_identically(self):
        # 0 vs 0.0 vs 0.000 must be one route, not three. Without this, a JSON producer
        # that emits ints would disagree with one that emits floats for the same mission.
        as_int = [{"latitude": 56.6501, "longitude": 12.8701, "loiter_time_s": 0},
                  {"latitude": 56.6512, "longitude": 12.8725, "loiter_time_s": 30}]
        as_float = [{"latitude": 56.6501, "longitude": 12.8701, "loiter_time_s": 0.0},
                    {"latitude": 56.6512, "longitude": 12.8725, "loiter_time_s": 30.000}]
        self.assertEqual(mission_contract.route_content_hash(as_int),
                         mission_contract.route_content_hash(as_float))
        self.assertEqual(mission_contract.route_content_hash(as_int),
                         FIXTURE["scout_golden_route_hash"])

    def test_float_noise_below_the_rounding_floor_is_absorbed(self):
        # 56.6501 and 56.65010000000001 are the same point; they must hash the same, or a
        # round-trip through a float parser would invalidate a correct mission.
        noisy = [{"latitude": 56.65010000000001, "longitude": 12.8701, "loiter_time_s": 0},
                 {"latitude": 56.6512, "longitude": 12.87249999999999, "loiter_time_s": 30}]
        self.assertEqual(mission_contract.route_content_hash(noisy),
                         FIXTURE["scout_golden_route_hash"])

    def test_the_rounding_precisions_are_the_scout_specified_ones(self):
        spec = FIXTURE["canonicalization"]
        self.assertEqual(mission_contract.COORDINATE_PRECISION, spec["coordinate_precision"])
        self.assertEqual(mission_contract.LOITER_PRECISION, spec["loiter_precision"])

    def test_high_precision_probe_pins_the_rounding(self):
        # The golden route's coordinates only carry 4 decimals, so they cannot detect a
        # precision change. This probe can — and it is now CROSS-SYSTEM verified: Scout and
        # the Operator ran it independently and produced the same digest. The full proof
        # lives in TestHighPrecisionProbeIsCrossSystemVerified.
        probe = FIXTURE["high_precision_probe"]
        self.assertEqual(mission_contract.canonical_route_json(probe["waypoints"]),
                         probe["canonical_json"])
        self.assertEqual(mission_contract.route_content_hash(probe["waypoints"]),
                         probe["scout_computed_route_content_hash"])

    def test_the_hash_is_prefixed_and_is_a_sha256_hex_digest(self):
        got = mission_contract.route_content_hash(self.ROUTE)
        self.assertTrue(got.startswith("sha256:"))
        self.assertEqual(len(got[len("sha256:"):]), 64)
        int(got[len("sha256:"):], 16)  # raises unless it is hex


class TestHighPrecisionProbeIsCrossSystemVerified(unittest.TestCase):
    """The precision proof. The 4-decimal golden route cannot discriminate a coordinate
    precision of 4 from one of 9; this probe can, and Scout and the Operator computed its
    digest INDEPENDENTLY and agreed. These tests are what keep that claim true."""

    PROBE = FIXTURE["high_precision_probe"]

    def test_scout_and_operator_recorded_the_same_probe_digest(self):
        # The cross-system assertion itself. Two separately recorded fields, asserted equal
        # — a single field could not express that two systems agreed.
        self.assertEqual(self.PROBE["scout_computed_route_content_hash"],
                         self.PROBE["operator_computed_route_content_hash"])
        self.assertTrue(self.PROBE["cross_system_verified"])

    def test_this_module_reproduces_scouts_probe_digest(self):
        self.assertEqual(mission_contract.route_content_hash(self.PROBE["waypoints"]),
                         self.PROBE["scout_computed_route_content_hash"])

    def test_probe_canonical_json_is_byte_exact(self):
        self.assertEqual(mission_contract.canonical_route_json(self.PROBE["waypoints"]),
                         self.PROBE["canonical_json"])

    def test_the_probe_actually_discriminates_coordinate_precision(self):
        # Without this, the probe could be passing for the wrong reason. Shifting a
        # coordinate below the 7th decimal but above the 8th MUST move the digest at
        # precision 7 — which is what makes agreement on it evidence about the rounding.
        wps = [dict(w) for w in self.PROBE["waypoints"]]
        wps[0]["latitude"] = wps[0]["latitude"] + 1e-7
        self.assertNotEqual(mission_contract.route_content_hash(wps),
                            self.PROBE["scout_computed_route_content_hash"])

    def test_the_probe_actually_discriminates_loiter_precision(self):
        wps = [dict(w) for w in self.PROBE["waypoints"]]
        wps[0]["loiter_time_s"] = wps[0]["loiter_time_s"] + 1e-3
        self.assertNotEqual(mission_contract.route_content_hash(wps),
                            self.PROBE["scout_computed_route_content_hash"])

    def test_the_probe_carries_more_decimals_than_the_precisions_it_pins(self):
        # A probe whose inputs did not exceed the rounding floor would prove nothing.
        for wp in self.PROBE["waypoints"]:
            for key in ("latitude", "longitude"):
                decimals = len(repr(float(wp[key])).split(".")[1])
                self.assertGreater(decimals, mission_contract.COORDINATE_PRECISION, key)


class TestMissionPreviewIsSideEffectFree(unittest.TestCase):
    """POST /api/missions/preview must be READ-ONLY. It exists so the operator can see the
    hash they are about to approve; if it could queue, log or mutate anything, merely
    LOOKING at a mission would change the system's state."""

    def setUp(self):
        _reset_commands()
        self.events_before = len(main.event_log)
        self.client = TestClient(main.app)

    def _preview(self, body=None):
        return self.client.post("/api/missions/preview",
                                json=body if body is not None else ROUTE_REQUEST)

    def test_preview_succeeds_and_returns_the_canonical_params(self):
        r = self._preview()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(r.json()["params"]["expected_route_content_hash"],
                         FIXTURE["scout_golden_route_hash"])

    def test_preview_creates_no_command(self):
        self._preview()
        self.assertEqual(len(main.commands), 0)
        self.assertEqual(len(main.commands_by_id), 0)

    def test_preview_creates_no_event(self):
        self._preview()
        self.assertEqual(len(main.event_log), self.events_before)

    def test_preview_changes_no_authority_or_vehicle_state(self):
        before_auth = json.dumps(main.last_authority_by_id, sort_keys=True, default=str)
        before_comms = json.dumps(main.comms_state_by_id, sort_keys=True, default=str)
        self._preview()
        self.assertEqual(json.dumps(main.last_authority_by_id, sort_keys=True, default=str),
                         before_auth)
        self.assertEqual(json.dumps(main.comms_state_by_id, sort_keys=True, default=str),
                         before_comms)

    def test_a_rejected_preview_also_creates_nothing(self):
        r = self._preview({"waypoints": [{"latitude": 999, "longitude": 12.87}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(main.commands), 0)
        self.assertEqual(len(main.event_log), self.events_before)

    def test_malformed_json_is_a_400_not_a_500(self):
        r = self.client.post("/api/missions/preview", content=b"{not json",
                             headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"], "mission_contract_violation")


class TestPreviewAndCommandCanonicalizationParity(unittest.TestCase):
    """Preview and command creation must canonicalize IDENTICALLY. A preview that shows a
    hash the upload would not produce shows the operator a mission they cannot send."""

    def setUp(self):
        _reset_commands()
        self.client = TestClient(main.app)

    ROUTES = (
        ROUTE_REQUEST,
        {"waypoints": [{"latitude": 56.65, "longitude": 12.87}]},                  # loiter default
        {"contract_version": "mission-contract-v1",
         "waypoints": FIXTURE["high_precision_probe"]["waypoints"]},               # high precision
        {"waypoints": [{"latitude": -33.8, "longitude": 151.2, "loiter_time_s": 5}]},
    )

    def test_preview_params_equal_the_queued_commands_params(self):
        for route in self.ROUTES:
            with self.subTest(route=route):
                _reset_commands()
                preview = self.client.post("/api/missions/preview", json=route).json()["params"]
                queued = self.client.post("/api/commands", json={
                    "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD",
                    "params": route, "confirm": True}).json()["command"]["params"]
                self.assertEqual(preview, queued)

    def test_both_paths_go_through_the_one_canonicalizer(self):
        # Parity by construction, not by two implementations happening to agree today.
        for route in self.ROUTES:
            preview = self.client.post("/api/missions/preview", json=route).json()["params"]
            self.assertEqual(preview, main.canonical_mission_upload_params(route))

    def test_the_same_violation_produces_the_same_errors_on_both_paths(self):
        bad = {"waypoints": [{"latitude": 56.65, "longitude": 12.87, "seq": 0, "altitude": 3}]}
        p = self.client.post("/api/missions/preview", json=bad)
        c = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "params": bad, "confirm": True})
        self.assertEqual(p.status_code, 400)
        self.assertEqual(c.status_code, 400)
        self.assertEqual(p.json()["errors"], c.json()["errors"])


class TestRouteWaypointLimit(unittest.TestCase):
    """ONE shared maximum, enforced in the shared canonicalizer so preview and command
    creation cannot diverge. The number is SCOUT'S: mission-contract-v1 defines and enforces
    MAX_ROUTE_WAYPOINTS = 200. The Operator mirrors it so an oversized route fails at
    preview, before transmission — Scout stays the authority, this is a fail-fast mirror."""

    def setUp(self):
        _reset_commands()
        self.client = TestClient(main.app)

    def _route(self, n):
        return {"waypoints": [{"latitude": 56.0 + i * 1e-5, "longitude": 12.0}
                              for i in range(n)]}

    def test_a_route_at_the_limit_is_accepted(self):
        params = main.canonical_mission_upload_params(self._route(main.MAX_ROUTE_WAYPOINTS))
        self.assertEqual(params["expected_route_waypoint_count"], main.MAX_ROUTE_WAYPOINTS)

    def test_one_over_the_limit_is_refused_with_a_clear_error(self):
        with self.assertRaises(main.MissionContractError) as ctx:
            main.canonical_mission_upload_params(self._route(main.MAX_ROUTE_WAYPOINTS + 1))
        joined = " ".join(ctx.exception.errors)
        self.assertIn(str(main.MAX_ROUTE_WAYPOINTS), joined)
        self.assertIn("at most", joined)

    def test_an_excessive_route_reports_one_error_not_thousands(self):
        # A 5000-waypoint route must report the one actionable problem. Per-waypoint errors
        # would bury it in noise the operator cannot act on.
        with self.assertRaises(main.MissionContractError) as ctx:
            main.canonical_mission_upload_params(self._route(5000))
        self.assertEqual(len(ctx.exception.errors), 1)

    def test_preview_and_command_both_enforce_it(self):
        over = self._route(main.MAX_ROUTE_WAYPOINTS + 1)
        p = self.client.post("/api/missions/preview", json=over)
        c = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "params": over, "confirm": True})
        self.assertEqual(p.status_code, 400)
        self.assertEqual(c.status_code, 400)
        self.assertEqual(p.json()["errors"], c.json()["errors"])
        self.assertEqual(len(main.commands), 0, "an over-limit route must not be queued")

    def test_capabilities_report_200_with_scout_contract_provenance(self):
        body = self.client.get("/api/commands/capabilities").json()
        self.assertEqual(body["max_route_waypoints"], 200)
        self.assertEqual(body["max_route_waypoints"], main.MAX_ROUTE_WAYPOINTS)
        # The limit is Scout's, declared through mission-contract-v1. This assertion is what
        # keeps the UI wording ("defined and enforced by Scout") true.
        self.assertEqual(body["max_route_waypoints_source"], "scout-contract")
        self.assertNotEqual(body["max_route_waypoints_source"], "operator-temporary")

    def test_the_operator_limit_mirrors_scouts_contract_value(self):
        # A local re-tune would put the two systems back out of agreement — the exact defect
        # the earlier Operator-chosen placeholder risked.
        self.assertEqual(main.MAX_ROUTE_WAYPOINTS, 200)

    def test_the_refusal_names_scout_as_the_owner_of_the_limit(self):
        # The operator must not be told a local policy refused their mission when the flight
        # system's contract did.
        with self.assertRaises(main.MissionContractError) as ctx:
            main.canonical_mission_upload_params(self._route(201))
        joined = " ".join(ctx.exception.errors)
        self.assertIn("Scout", joined)
        self.assertIn(main.MISSION_CONTRACT_VERSION, joined)
        self.assertNotIn("Scout states none", joined)

    def test_preview_rejects_201(self):
        r = self.client.post("/api/missions/preview", json=self._route(201))
        self.assertEqual(r.status_code, 400)
        self.assertIn("200", " ".join(r.json()["errors"]))

    def test_command_creation_rejects_201(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD",
            "params": self._route(201), "confirm": True})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(main.commands), 0)

    def test_200_is_accepted_by_both_paths(self):
        # The boundary in the other direction: 200 must not be off-by-one refused.
        at_limit = self._route(200)
        self.assertEqual(self.client.post("/api/missions/preview", json=at_limit).status_code, 200)
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD",
            "params": at_limit, "confirm": True})
        self.assertEqual(r.status_code, 200, r.text)


class TestScoutMissionTooLargeResult(unittest.TestCase):
    """A MISSION_TOO_LARGE that comes back FROM Scout must be rendered from Scout's own two
    numbers — not flattened to a bare code, not padded with generic wording, and never
    back-filled from the Operator's local constant."""

    TOO_LARGE = {
        "accepted": False, "verified": False,
        "error": {"code": "MISSION_TOO_LARGE",
                  "maximum_route_waypoints": 200, "observed_route_waypoints": 250},
    }

    def _cmd(self, result):
        cmd = {"type": "MISSION_UPLOAD", "status": "EXECUTED",
               "params": main.canonical_mission_upload_params(ROUTE_REQUEST),
               "result": result, "reason": None}
        main._annotate_mission_upload_result(cmd)
        return cmd

    def test_both_numeric_fields_survive_into_the_reason(self):
        cmd = self._cmd(dict(self.TOO_LARGE))
        self.assertEqual(cmd["mission_result"], "failed")
        self.assertIn("200", cmd["reason"])
        self.assertIn("250", cmd["reason"])

    def test_the_structured_result_is_preserved_verbatim_on_the_record(self):
        # The UI and the evidence export both read the raw error object; flattening it to a
        # string here would lose the fields they render as separate rows.
        cmd = self._cmd(dict(self.TOO_LARGE))
        err = cmd["result"]["error"]
        self.assertEqual(err["code"], "MISSION_TOO_LARGE")
        self.assertEqual(err["maximum_route_waypoints"], 200)
        self.assertEqual(err["observed_route_waypoints"], 250)

    def test_no_generic_explanation_replaces_scouts_structured_error(self):
        cmd = self._cmd(dict(self.TOO_LARGE))
        self.assertNotIn("was not accepted by Scout", cmd["reason"])
        self.assertNotIn("not verified by Scout's read-back", cmd["reason"])

    def test_the_reason_is_never_a_bare_error_code(self):
        cmd = self._cmd(dict(self.TOO_LARGE))
        self.assertNotEqual(cmd["reason"], "MISSION_TOO_LARGE")
        self.assertIn("route waypoints", cmd["reason"])

    def test_missing_counts_are_reported_as_missing_not_back_filled(self):
        # Substituting MAX_ROUTE_WAYPOINTS for a maximum Scout omitted would present an
        # Operator constant as Scout's word.
        text = main.mission_error_text({"code": "MISSION_TOO_LARGE"})
        self.assertIn("did not report both counts", text)
        self.assertNotIn("200", text)

    def test_an_unstructured_error_still_falls_back_to_the_generic_wording(self):
        cmd = self._cmd({"accepted": False, "verified": False})
        self.assertIn("not accepted by Scout", cmd["reason"])

    def test_mission_error_text_returns_none_when_there_is_nothing_structured(self):
        self.assertIsNone(main.mission_error_text({}))
        self.assertIsNone(main.mission_error_text(None))
        self.assertEqual(main.mission_error_text({"message": "boom"}), "boom")


class TestBrowserSuppliedExpectedHashIsRefused(unittest.TestCase):
    """The expected hash is what an upload is later verified AGAINST. A caller that could
    supply it would be choosing its own passing grade — that is not a verification."""

    def setUp(self):
        _reset_commands()
        self.client = TestClient(main.app)

    def test_a_supplied_expected_hash_is_refused_not_echoed(self):
        body = dict(ROUTE_REQUEST, expected_route_content_hash="sha256:deadbeef")
        with self.assertRaises(main.MissionContractError) as ctx:
            main.canonical_mission_upload_params(body)
        self.assertIn("expected_route_content_hash", " ".join(ctx.exception.errors))

    def test_supplied_expected_counts_are_refused_too(self):
        for field in ("expected_route_waypoint_count", "expected_pixhawk_item_count"):
            with self.assertRaises(main.MissionContractError):
                main.canonical_mission_upload_params(dict(ROUTE_REQUEST, **{field: 99}))

    def test_preview_refuses_a_browser_supplied_hash(self):
        r = self.client.post("/api/missions/preview",
                             json=dict(ROUTE_REQUEST, expected_route_content_hash="sha256:dead"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("expected_route_content_hash", " ".join(r.json()["errors"]))

    def test_command_creation_refuses_a_browser_supplied_hash(self):
        r = self.client.post("/api/commands", json={
            "vehicle_id": SCOUT_VID, "type": "MISSION_UPLOAD", "confirm": True,
            "params": dict(ROUTE_REQUEST, expected_route_content_hash="sha256:dead")})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(main.commands), 0)

    def test_the_derived_hash_is_always_the_backends_own(self):
        # The positive form: whatever the caller sends, the stored hash is computed here.
        params = main.canonical_mission_upload_params(ROUTE_REQUEST)
        self.assertEqual(params["expected_route_content_hash"],
                         mission_contract.route_content_hash(ROUTE_REQUEST["waypoints"]))


class TestNoDocumentationClaimsScoutHasNoLimit(unittest.TestCase):
    """A guard against the specific way this repository goes stale: the waypoint limit's
    PROVENANCE was documented in prose in several files, and prose does not fail a test when
    the underlying fact changes. This test does. Scout now owns the limit; any surviving
    "Scout declares no limit" is a false statement about the flight system's contract."""

    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    # Substrings that asserted the OLD (now false) provenance. Matched case-insensitively.
    FORBIDDEN = (
        "operator-temporary",
        "scout declares no",
        "scout states no",
        "scout states none",
        "operator-imposed limit",
        "no explicit limit",
    )
    SEARCHED = (".py", ".js", ".mjs", ".md", ".json", ".css")
    SKIP_DIRS = {"node_modules", ".git", "__pycache__", "img"}

    def _files(self):
        for dirpath, dirnames, filenames in os.walk(self.ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.SKIP_DIRS]
            for name in filenames:
                if name.endswith(self.SEARCHED):
                    yield os.path.join(dirpath, name)

    def test_no_file_still_claims_scout_has_no_waypoint_limit(self):
        offenders = []
        for path in self._files():
            # This test file necessarily CONTAINS the forbidden strings, as the list above.
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read().lower()
            except (UnicodeDecodeError, OSError):
                continue
            for phrase in self.FORBIDDEN:
                if phrase in text:
                    offenders.append(f"{os.path.relpath(path, self.ROOT)}: {phrase!r}")
        self.assertEqual(offenders, [], "stale waypoint-limit provenance:\n" + "\n".join(offenders))

    def test_the_docs_state_the_new_provenance_positively(self):
        # Removing the false claim is not enough — the true one has to be written down, or
        # the next reader has no provenance at all.
        doc = os.path.join(self.ROOT, "operator", "docs", "verification", "command-lifecycle.md")
        with open(doc, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("scout-contract", text)
        self.assertIn("MISSION_TOO_LARGE", text)


class TestMissionUploadLiveState(unittest.TestCase):
    """agent.mission_upload is normalized live-only — never replayed from last-known."""

    def setUp(self):
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"

    def test_normalizes_scouts_worker_state(self):
        block = main.mission_upload_block(SCOUT_VID, {"agent": {"mission_upload": {
            "active": True, "state": "uploading", "command_id": "cmd-1", "elapsed_s": 4.25}}})
        self.assertEqual(block, {"active": True, "state": "UPLOADING",
                                 "command_id": "cmd-1", "elapsed_s": 4.2, "source": "scout"})

    def test_absent_group_is_none_not_a_fabricated_idle(self):
        self.assertIsNone(main.mission_upload_block(SCOUT_VID, {"agent": {}}))
        self.assertIsNone(main.mission_upload_block(SCOUT_VID, {}))

    def test_a_disconnected_vehicle_reports_no_live_upload_state(self):
        # Replaying a cached active:true would leave the UI showing "Executing" forever
        # for an upload that died with the link.
        main.comms_state_by_id[SCOUT_VID] = "DISCONNECTED"
        self.assertIsNone(main.mission_upload_block(SCOUT_VID, {"agent": {"mission_upload": {
            "active": True, "state": "UPLOADING", "command_id": "cmd-1", "elapsed_s": 99}}}))


if __name__ == "__main__":
    unittest.main()
