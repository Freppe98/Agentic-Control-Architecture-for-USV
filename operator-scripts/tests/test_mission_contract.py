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
        # precision change. This probe can — it is Operator-computed, NOT Scout-pinned, so
        # it proves internal stability only. See the fixture's _provenance note.
        probe = FIXTURE["high_precision_probe"]
        self.assertEqual(mission_contract.canonical_route_json(probe["waypoints"]),
                         probe["operator_computed_canonical_json"])
        self.assertEqual(mission_contract.route_content_hash(probe["waypoints"]),
                         probe["operator_computed_route_content_hash"])

    def test_the_hash_is_prefixed_and_is_a_sha256_hex_digest(self):
        got = mission_contract.route_content_hash(self.ROUTE)
        self.assertTrue(got.startswith("sha256:"))
        self.assertEqual(len(got[len("sha256:"):]), 64)
        int(got[len("sha256:"):], 16)  # raises unless it is hex


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
