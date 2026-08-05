"""Backend tests for the Scout replanning integration (task Sections 1, 2, 3, 10, 11, 12).

Run from operator-scripts/:  python -m unittest tests.test_replan_integration  (no pytest).

The Operator backend is a THIN PROXY to Scout's Local Agent replanning API on port 8090
(scout_replan.py) and builds the approved planning package from an immutable mission record
(replan_package.py). These tests mock every Scout HTTP call by swapping `scout_replan.requests`
for a recording fake — NOTHING here touches real networking. They pin:

  • every /agent/replan/* proxy route forwards to the SELECTED vehicle's 8090 base;
  • a write TIMEOUT is UNKNOWN (202), never a definite failure — reconciled by a later GET;
  • Scout's structured error code is preserved; a 409 is a distinct rejection, not a net fault;
  • an older Scout that 404s a route is supported:false, never a fabricated success;
  • target-USV isolation — a write to usv-2 never reaches usv-3's base URL;
  • planning-package construction: labels from real stages, route hash UNCHANGED by metadata,
    altered/invalid records fail closed;
  • the combined readiness rules (MISSION READY / REPLANNING READY).
"""
import json
import os
import sys
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import scout_replan  # noqa: E402
import replan_package  # noqa: E402
import mission_contract  # noqa: E402
import requests as real_requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2          # Scout — configured with a LOCAL_AGENT_API_BASE (8090) route
SAR_VID = 3            # SAR-001 — also configured
NO_LA_VID = 1          # USV-1 — configured identity, but NO LOCAL_AGENT_API_BASE route


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json

    def raise_for_status(self):
        # _scout_mission_read (the Pixhawk read-back on the Flask 8080 API) calls this;
        # the 8090 replan client does not. Raising the real exception type keeps that
        # path's `except requests.RequestException` working.
        if self.status_code >= 400:
            raise real_requests.HTTPError(f"HTTP {self.status_code}")


class FakeLA:
    """A recording fake for scout_replan.requests. Match responses by (METHOD, path-suffix);
    a value that is an Exception is raised (unreachable / timeout). Exposes the real
    RequestException so scout_replan's `except requests.RequestException` still catches."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []                 # [(method, url)]
        self.responses = {}             # {(METHOD, suffix): FakeResp | Exception}
        self.default = FakeResp({}, 200)

    def set(self, method, suffix, resp):
        self.responses[(method, suffix)] = resp

    def _resolve(self, method, url):
        self.calls.append((method, url))
        for (m, suffix), r in self.responses.items():
            if m == method and url.endswith(suffix):
                if isinstance(r, Exception):
                    raise r
                return r
        if isinstance(self.default, Exception):
            raise self.default
        return self.default

    def get(self, url, **kw):
        return self._resolve("GET", url)

    def request(self, method, url, **kw):
        return self._resolve(method, url)


def make_record(vid=SCOUT_VID, mission_id="msn-test-0001"):
    """A minimal but realistic immutable revision-0 mission record: four waypoints spanning
    approach → primary → secondary → return, with 1:1 segment provenance and the canonical hash."""
    route_waypoints = [
        {"latitude": 56.0000, "longitude": 12.0000, "loiter_time_s": 0},
        {"latitude": 56.0010, "longitude": 12.0000, "loiter_time_s": 0},
        {"latitude": 56.0010, "longitude": 12.0010, "loiter_time_s": 5},
        {"latitude": 56.0000, "longitude": 12.0010, "loiter_time_s": 0},
    ]
    order = [
        {"execution_seq": 0, "source_segment_kind": "approach"},
        {"execution_seq": 1, "source_segment_kind": "primary"},
        {"execution_seq": 2, "source_segment_kind": "secondary"},
        {"execution_seq": 3, "source_segment_kind": "return_approach"},
    ]
    return {
        "mission_id": mission_id, "mission_revision": 0, "vehicle_id": vid,
        "route_waypoints": route_waypoints,
        "route_hash": mission_contract.route_content_hash(route_waypoints),
        "original_execution_order": order,
        "navigable_geometry": [[[12.0, 56.0], [12.002, 56.0], [12.002, 56.002],
                                [12.0, 56.002], [12.0, 56.0]]],
        "no_go_zones": [],
        "planning_inputs": {"planning_home": {"latitude": 56.0, "longitude": 12.0},
                            "shoreline_clearance_m": 5},
        "metrics": {"shoreline_clearance_m": 5},
        "mission_package_version": "operator-survey-plan-v1",
        "route_contract_version": "mission-contract-v1",
        "input_revision": "rev-abc",
        "upload_status": "QUEUED",
    }


class ReplanPackageBuildTests(unittest.TestCase):
    """Pure planning-package construction (replan_package.py) — no HTTP."""

    def test_route_hash_unchanged_by_segment_metadata(self):
        rec = make_record()
        pkg, meta = replan_package.build_package(
            rec, {"latitude": 56.0, "longitude": 12.0}, usv_id="usv-2")
        # The package route carries a `segment` label on every item, yet the operator-computed
        # hash equals the bare mission-contract hash — labels are metadata, not hash input.
        self.assertEqual(meta["route_content_hash"], rec["route_hash"])
        self.assertEqual(meta["route_content_hash"],
                         mission_contract.route_content_hash(rec["route_waypoints"]))
        self.assertTrue(all("segment" in item for item in pkg["route"]))

    def test_semantic_labels_from_generation_stages(self):
        rec = make_record()
        pkg, _ = replan_package.build_package(
            rec, {"latitude": 56.0, "longitude": 12.0}, usv_id="usv-2")
        labels = [item["segment"] for item in pkg["route"]]
        self.assertEqual(labels, ["OUTBOUND_TRANSIT", "PRIMARY_SURVEY",
                                  "SECONDARY_SURVEY", "RETURN"])

    def test_pass_transition_maps_to_secondary_survey(self):
        self.assertEqual(replan_package.label_for_kind("pass_transition"), "SECONDARY_SURVEY")
        self.assertEqual(replan_package.label_for_kind("start_connector"), "OUTBOUND_TRANSIT")
        self.assertEqual(replan_package.label_for_kind("final_home_connector"), "RETURN")

    def test_unmappable_kind_fails_closed(self):
        with self.assertRaises(replan_package.PackageError):
            replan_package.label_for_kind("teleport")

    def test_invalid_home_refused(self):
        rec = make_record()
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_package(rec, {"latitude": 0, "longitude": 0}, usv_id="usv-2")
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_package(rec, None, usv_id="usv-2")

    def test_empty_route_refused(self):
        rec = make_record()
        rec["route_waypoints"] = []
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_package(rec, {"latitude": 56.0, "longitude": 12.0}, usv_id="usv-2")

    def test_altered_route_hash_refused(self):
        rec = make_record()
        rec["route_hash"] = "sha256:deadbeef"     # disagrees with the waypoints
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_package(rec, {"latitude": 56.0, "longitude": 12.0}, usv_id="usv-2")

    def test_boundary_and_limitations_present(self):
        rec = make_record()
        pkg, meta = replan_package.build_package(
            rec, {"latitude": 56.0, "longitude": 12.0}, usv_id="usv-2")
        self.assertTrue(meta["boundary_supplied"])
        self.assertTrue(pkg["navigable_boundary"])
        self.assertEqual(pkg["source"], "OPERATOR_STATION")
        # shoreline scalar limitation always reported honestly
        self.assertTrue(any("shoreline" in l for l in meta["limitations"]))

    def test_missing_provenance_fails_closed(self):
        rec = make_record()
        rec["original_execution_order"] = []       # a pre-segmentation record
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_package(rec, {"latitude": 56.0, "longitude": 12.0}, usv_id="usv-2")


class ReplanProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real = scout_replan.requests
        scout_replan.requests = self.fake
        self._ops_len = len(main.replan_operations)

    def tearDown(self):
        scout_replan.requests = self._real

    # --- reads ---------------------------------------------------------------------------
    def test_status_proxies_to_selected_vehicle_base(self):
        self.fake.set("GET", "/agent/replan/status", FakeResp({"fsm_state": "MONITORING"}))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/status")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["scout"]["fsm_state"], "MONITORING")
        # hit Scout's 8090 base for THIS vehicle
        self.assertTrue(any(u.startswith("http://10.0.2.10:8090") for _, u in self.fake.calls))

    def test_config_get_proxies(self):
        self.fake.set("GET", "/agent/replan/config", FakeResp({"dry_run": True}))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/config")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["scout"]["dry_run"])

    # --- writes: config ------------------------------------------------------------------
    def test_config_patch_forwards_only_patchable_fields(self):
        self.fake.set("PATCH", "/agent/replan/config", FakeResp({"applied": True}))
        r = self.client.patch(f"/api/vehicles/{SCOUT_VID}/replan/config",
                              json={"dry_run": True, "not_a_field": 9})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], "accepted")

    def test_config_patch_empty_is_400_without_forwarding(self):
        r = self.client.patch(f"/api/vehicles/{SCOUT_VID}/replan/config",
                              json={"not_a_field": 9})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.fake.calls, [])   # never forwarded

    def test_config_patch_409_is_transaction_active_not_network_fault(self):
        self.fake.set("PATCH", "/agent/replan/config",
                      FakeResp({"error_code": "TRANSACTION_ACTIVE"}, status=409))
        r = self.client.patch(f"/api/vehicles/{SCOUT_VID}/replan/config", json={"dry_run": False})
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["outcome"], "rejected")
        self.assertTrue(body.get("transaction_active"))
        self.assertEqual(body["scout_error_code"], "TRANSACTION_ACTIVE")

    # --- writes: planning package --------------------------------------------------------
    def _seed_mission(self, vid=SCOUT_VID, mission_id="msn-test-0001"):
        rec = make_record(vid, mission_id)
        main.original_missions[mission_id] = rec
        main.active_original_by_vehicle[vid] = mission_id
        return rec

    def test_put_package_builds_and_forwards_with_preserved_hash(self):
        rec = self._seed_mission()
        self.fake.set("PUT", "/agent/replan/planning_package",
                      FakeResp({"accepted": True, "stored": True, "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"],
                                "validation": {"ok": True}}))
        r = self.client.put(f"/api/vehicles/{SCOUT_VID}/replan/planning-package", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcome"], "accepted")
        self.assertEqual(body["operator_package"]["route_content_hash"], rec["route_hash"])
        self.assertEqual(body["operator_package"]["segment_label_counts"],
                         {"OUTBOUND_TRANSIT": 1, "PRIMARY_SURVEY": 1,
                          "SECONDARY_SURVEY": 1, "RETURN": 1})
        # operation recorded as accepted, with the mission id
        op = main.replan_operations[-1]
        self.assertEqual(op["operation"], "planning_package.put")
        self.assertEqual(op["outcome"], "accepted")
        self.assertEqual(op["mission_id"], rec["mission_id"])

    def test_put_package_timeout_is_unknown_and_reconcilable(self):
        rec = self._seed_mission(mission_id="msn-test-unknown")
        self.fake.set("PUT", "/agent/replan/planning_package", real_requests.Timeout("boom"))
        r = self.client.put(f"/api/vehicles/{SCOUT_VID}/replan/planning-package", json={})
        self.assertEqual(r.status_code, 202)          # accepted-but-unconfirmed, NOT a failure
        self.assertEqual(r.json()["outcome"], "unknown")
        self.assertEqual(main.replan_operations[-1]["outcome"], "unknown")
        # reconciliation: a later GET resolves actual Scout state (idempotent store landed it)
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": True, "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"]}))
        g = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/planning-package")
        self.assertEqual(g.status_code, 200)
        self.assertEqual(g.json()["scout"]["mission_id"], rec["mission_id"])

    def test_put_package_no_record_is_404(self):
        main.active_original_by_vehicle.pop(SAR_VID, None)
        r = self.client.put(f"/api/vehicles/{SAR_VID}/replan/planning-package", json={})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.fake.calls, [])

    def test_delete_package_idempotent(self):
        self.fake.set("DELETE", "/agent/replan/planning_package", FakeResp({"cleared": True}))
        r = self.client.delete(f"/api/vehicles/{SCOUT_VID}/replan/planning-package")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], "accepted")

    # --- writes: experiment --------------------------------------------------------------
    def test_experiment_put_requires_override_and_forces_target(self):
        # no override → 400, nothing forwarded
        r0 = self.client.put(f"/api/vehicles/{SCOUT_VID}/replan/experiment", json={})
        self.assertEqual(r0.status_code, 400)
        self.assertEqual(self.fake.calls, [])
        # a valid override → forwarded with target_vehicle forced to the selected vehicle
        captured = {}

        def capture(method, url, **kw):
            captured["json"] = kw.get("json")
            return FakeResp({"active": True, "source": "SIMULATED"})
        self.fake.request = capture
        r = self.client.put(f"/api/vehicles/{SCOUT_VID}/replan/experiment",
                            json={"force_safe_return": True, "target_vehicle": "usv-3"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["json"]["target_vehicle"], "usv-2")   # forced, not usv-3

    def test_experiment_get_and_delete(self):
        self.fake.set("GET", "/agent/replan/experiment", FakeResp({"active": False}))
        g = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/experiment")
        self.assertEqual(g.status_code, 200)
        self.fake.set("DELETE", "/agent/replan/experiment", FakeResp({"cleared": True}))
        d = self.client.delete(f"/api/vehicles/{SCOUT_VID}/replan/experiment")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json()["outcome"], "accepted")

    # --- reset ---------------------------------------------------------------------------
    def test_reset_forwards_and_409_while_active(self):
        self.fake.set("POST", "/agent/replan/reset", FakeResp({"rearmed": True}))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/reset")
        self.assertEqual(r.status_code, 200)
        self.fake.set("POST", "/agent/replan/reset",
                      FakeResp({"error_code": "TRANSACTION_ACTIVE"}, status=409))
        r2 = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/reset")
        self.assertEqual(r2.status_code, 409)
        self.assertTrue(r2.json().get("transaction_active"))

    # --- compatibility / isolation / errors ---------------------------------------------
    def test_unknown_vehicle_is_404(self):
        r = self.client.get("/api/vehicles/usv-999/replan/status")
        self.assertEqual(r.status_code, 404)

    def test_no_local_agent_route_is_supported_false(self):
        r = self.client.get(f"/api/vehicles/{NO_LA_VID}/replan/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["supported"])
        self.assertEqual(self.fake.calls, [])          # nothing to talk to → never forwarded

    def test_older_scout_404_is_supported_false(self):
        self.fake.set("GET", "/agent/replan/status", FakeResp({"error": "not found"}, status=404))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/status")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["supported"])
        self.assertEqual(r.json()["outcome"], "unsupported")

    def test_scout_error_code_preserved_on_rejection(self):
        self._seed_mission(mission_id="msn-hash-mismatch")
        self.fake.set("PUT", "/agent/replan/planning_package",
                      FakeResp({"error_code": "PLANNING_PACKAGE_HASH_MISMATCH"}, status=400))
        r = self.client.put(f"/api/vehicles/{SCOUT_VID}/replan/planning-package", json={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["scout_error_code"], "PLANNING_PACKAGE_HASH_MISMATCH")
        self.assertEqual(r.json()["outcome"], "rejected")

    def test_target_isolation_write_hits_only_selected_base(self):
        self._seed_mission(vid=SAR_VID, mission_id="msn-sar-1")
        self.fake.set("PUT", "/agent/replan/planning_package", FakeResp({"stored": True}))
        self.client.put(f"/api/vehicles/{SAR_VID}/replan/planning-package", json={})
        urls = [u for _, u in self.fake.calls]
        self.assertTrue(all(u.startswith("http://10.0.3.10:8090") for u in urls))
        self.assertFalse(any(u.startswith("http://10.0.2.10:8090") for u in urls))

    def test_operation_trace_lists_writes(self):
        self.fake.set("DELETE", "/agent/replan/planning_package", FakeResp({"cleared": True}))
        self.client.delete(f"/api/vehicles/{SCOUT_VID}/replan/planning-package")
        r = self.client.get(f"/api/replan/operations?vehicle_id={SCOUT_VID}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any(o["operation"] == "planning_package.delete"
                            for o in r.json()["operations"]))


class ReplanReadinessTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real_sr = scout_replan.requests
        scout_replan.requests = self.fake
        # Keep the Pixhawk readback (main.requests → Flask 8080) off the network. It returns
        # the seeded record's own route hash, because the hash chain readiness proves is
        # package == approved record == FLIGHT CONTROLLER — a readback that cannot confirm
        # the route leaves the chain unproven, which these ready-path tests are not about.
        self._real_main = main.requests
        self.pixhawk = PixhawkReq()
        main.requests = self.pixhawk
        main._pixhawk_readback_cache.clear()

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main
        main._pixhawk_readback_cache.clear()

    def _seed(self, upload_status="VERIFIED", mission_id="msn-ready-1"):
        rec = make_record(SCOUT_VID, mission_id)
        rec["upload_status"] = upload_status
        main.original_missions[mission_id] = rec
        main.active_original_by_vehicle[SCOUT_VID] = mission_id
        self.pixhawk.body = dict(self.pixhawk.body, route_content_hash=rec["route_hash"])
        main._pixhawk_readback_cache.clear()
        # a live verified Home so home_valid is true
        main.last_known_agent[SCOUT_VID] = {"home_status": {"home_position": {
            "latitude": 56.0, "longitude": 12.0}}}
        return rec

    def test_replanning_ready_when_all_conditions_met(self):
        rec = self._seed()
        consistent = {"package_consistency": "PLANNING_PACKAGE_CONSISTENT",
                      "mission_id": rec["mission_id"], "route_content_hash": rec["route_hash"],
                      "geometry_validation": {"boundary_available": True, "boundary_checked": True,
                                              "connector_proven_safe": True}}
        self.fake.set("GET", "/agent/replan/status", FakeResp(consistent))
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": True, "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"]}))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["mission_ready"])
        self.assertTrue(body["replanning_ready"])
        self.assertTrue(body["planning_package"]["consistent"])

    def test_not_ready_when_package_missing(self):
        rec = self._seed(mission_id="msn-missing-pkg")
        self.fake.set("GET", "/agent/replan/status",
                      FakeResp({"package_consistency": "PLANNING_PACKAGE_MISSING",
                                "mission_id": rec["mission_id"]}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": False}))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        body = r.json()
        self.assertTrue(body["mission_ready"])        # the vehicle mission is fine …
        self.assertFalse(body["replanning_ready"])    # … but the package is not (not hidden)

    def test_not_ready_when_pixhawk_not_verified(self):
        rec = self._seed(upload_status="QUEUED", mission_id="msn-unverified")
        self.fake.set("GET", "/agent/replan/status",
                      FakeResp({"package_consistency": "PLANNING_PACKAGE_CONSISTENT",
                                "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"]}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": True, "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"]}))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        body = r.json()
        self.assertFalse(body["mission_ready"])
        self.assertFalse(body["replanning_ready"])

    def test_hash_mismatch_blocks_and_is_listed(self):
        rec = self._seed(mission_id="msn-hashmm")
        self.fake.set("GET", "/agent/replan/status",
                      FakeResp({"package_consistency": "PLANNING_PACKAGE_CONSISTENT",
                                "mission_id": rec["mission_id"],
                                "route_content_hash": "sha256:different"}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": True, "mission_id": rec["mission_id"],
                                "route_content_hash": "sha256:different"}))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        body = r.json()
        self.assertTrue(body["planning_package"]["hash_mismatch"])
        self.assertFalse(body["replanning_ready"])
        self.assertTrue(any("hash" in l.lower() for l in body["limitations"]))


# ══════════════════════════════════════════════════════════════════════════════════════
# replan-planning-package-v1 (the lossless package + the MANUAL sync)
# ══════════════════════════════════════════════════════════════════════════════════════
# These tests run against a REAL, captured Operator mission record — the verified
# msn-329c2faff137 (14 route waypoints, 7 typed segments, a 14-entry detailed execution
# order, one navigable ring, zero no-go zones), saved verbatim as a fixture. Using the real
# record rather than a hand-written minimal one is the point: the whole risk in this contract
# is that the builder quietly reshapes structures that only the real planner produces.

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                            "active-original-msn-329c2faff137.json")
FIXTURE_MISSION_ID = "msn-329c2faff137"
FIXTURE_ROUTE_HASH = ("sha256:21e7f7d4ba7fd2c10ccea1621d290de0b8755966804fc6f9"
                      "754479e0ec60d990")


def real_record():
    """A fresh deep copy of the captured verified mission record. Fresh per call so one
    test's mutation can never leak into another's."""
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class PixhawkReq:
    """A fake for `main.requests` standing in for the vehicle Flask API (port 8080) mission
    read-back. Counts calls so a test can prove how many mission downloads a code path costs."""
    RequestException = real_requests.RequestException

    def __init__(self, body=None, raise_exc=None):
        self.body = body if body is not None else {
            "waypoints": [], "count": 0, "partial": False,
            "route_content_hash": FIXTURE_ROUTE_HASH,
        }
        self.raise_exc = raise_exc
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResp(self.body)


class V1PackageBuildTests(unittest.TestCase):
    """The pure builder against the real record. No HTTP anywhere in this class."""

    def test_exact_package_from_the_real_mission_record(self):
        pkg, meta = replan_package.build_v1_package(real_record())
        # The wire fields, exactly — an EXTRA key is as much a contract break as a missing one.
        self.assertEqual(tuple(pkg.keys()), replan_package.V1_FIELDS)
        self.assertEqual(pkg["package_version"], "replan-planning-package-v1")
        self.assertEqual(pkg["route_contract_version"], "mission-contract-v1")
        self.assertEqual(pkg["mission_id"], FIXTURE_MISSION_ID)
        self.assertEqual(pkg["mission_revision"], 0)
        self.assertEqual(pkg["vehicle_id"], "usv-2")          # canonical, from the record's 2
        self.assertEqual(pkg["route_hash"], FIXTURE_ROUTE_HASH)
        self.assertEqual(pkg["shoreline_clearance_m"], 1)
        self.assertIs(pkg["immutable"], True)
        self.assertEqual(pkg["source"], "OPERATOR_STATION")
        self.assertEqual(pkg["created_at"], real_record()["created_at"])
        self.assertEqual(len(pkg["route_waypoints"]), 14)
        self.assertEqual(meta["route_waypoint_count"], 14)
        self.assertEqual(meta["segment_count"], 7)
        self.assertEqual(meta["execution_order_count"], 14)

    def test_route_hash_is_copied_from_the_record_not_reinvented(self):
        rec = real_record()
        pkg, _ = replan_package.build_v1_package(rec)
        self.assertEqual(pkg["route_hash"], rec["route_hash"])
        # …and it is the SAME digest mission-contract-v1 derives from the same route, so the
        # copy is verified rather than merely trusted.
        self.assertEqual(pkg["route_hash"],
                         mission_contract.route_content_hash(rec["route_waypoints"]))

    def test_route_waypoints_are_the_hashed_objects_verbatim(self):
        rec = real_record()
        pkg, _ = replan_package.build_v1_package(rec)
        for sent, stored in zip(pkg["route_waypoints"], rec["route_waypoints"]):
            self.assertEqual(set(sent), {"latitude", "longitude", "loiter_time_s"})
            self.assertEqual(sent["latitude"], stored["latitude"])
            self.assertEqual(sent["longitude"], stored["longitude"])
        # The package route still hashes to the approved hash — nothing was added to it.
        self.assertEqual(mission_contract.route_content_hash(pkg["route_waypoints"]),
                         FIXTURE_ROUTE_HASH)

    def test_output_is_deterministic(self):
        a, _ = replan_package.build_v1_package(real_record())
        b, _ = replan_package.build_v1_package(real_record())
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_builder_does_not_mutate_the_record(self):
        rec = real_record()
        before = json.dumps(rec, sort_keys=True)
        replan_package.build_v1_package(rec)
        self.assertEqual(json.dumps(rec, sort_keys=True), before)

    def test_package_does_not_alias_the_record(self):
        # Deep copy, not a shared reference: mutating the package must not corrupt the
        # immutable record it was built from.
        rec = real_record()
        pkg, _ = replan_package.build_v1_package(rec)
        pkg["segments"][0]["coordinates"][0][0] = 0.0
        pkg["original_execution_order"][0]["source_segment_kind"] = "tampered"
        pkg["navigable_geometry"][0][0][0] = 0.0
        self.assertNotEqual(rec["segments"][0]["coordinates"][0][0], 0.0)
        self.assertEqual(rec["original_execution_order"][0]["source_segment_kind"],
                         "start_connector")
        self.assertNotEqual(rec["navigable_geometry"][0][0][0], 0.0)

    # ── geometry: nesting and coordinate convention ──────────────────────────────────────
    def test_geometry_nesting_and_lng_lat_convention_preserved(self):
        rec = real_record()
        pkg, _ = replan_package.build_v1_package(rec)
        # planning_home: ONE positional pair [lng, lat]
        self.assertEqual(pkg["planning_home"], rec["planning_inputs"]["planning_home"])
        self.assertEqual(len(pkg["planning_home"]), 2)
        self.assertGreater(pkg["planning_home"][1], pkg["planning_home"][0])  # lat > lng here
        # boundary: a FLAT ring of positional pairs
        self.assertEqual(pkg["boundary"], rec["planning_inputs"]["boundary"])
        self.assertTrue(all(len(p) == 2 for p in pkg["boundary"]))
        # navigable_geometry: a LIST OF RINGS, one level deeper than boundary
        self.assertEqual(pkg["navigable_geometry"], rec["navigable_geometry"])
        self.assertEqual(len(pkg["navigable_geometry"]), 1)
        self.assertTrue(all(len(p) == 2 for p in pkg["navigable_geometry"][0]))

    def test_empty_no_go_zones_preserved_as_empty_list(self):
        rec = real_record()
        self.assertEqual(rec["no_go_zones"], [])
        pkg, meta = replan_package.build_v1_package(rec)
        # `[]` is the operator's actual answer ("no zones"), NOT an absent field — it must
        # survive as a list, never become null and never be dropped from the package.
        self.assertIn("no_go_zones", pkg)
        self.assertEqual(pkg["no_go_zones"], [])
        self.assertIsNotNone(pkg["no_go_zones"])
        self.assertEqual(meta["no_go_zone_count"], 0)
        self.assertTrue(any("no-go" in l for l in meta["limitations"]))

    def test_populated_no_go_zones_keep_their_ring_nesting(self):
        rec = real_record()
        zone = [[12.8109, 56.6790], [12.8110, 56.6790], [12.8110, 56.6791], [12.8109, 56.6790]]
        rec["no_go_zones"] = [zone]
        pkg, meta = replan_package.build_v1_package(rec)
        self.assertEqual(pkg["no_go_zones"], [zone])
        self.assertEqual(meta["no_go_zone_count"], 1)

    # ── the detailed metadata v1 exists to preserve ──────────────────────────────────────
    def test_detailed_segments_preserved_in_full(self):
        rec = real_record()
        pkg, _ = replan_package.build_v1_package(rec)
        self.assertEqual(len(pkg["segments"]), 7)
        self.assertEqual(pkg["segments"], rec["segments"])   # every field, unchanged
        for seg in pkg["segments"]:
            for field in replan_package.V1_SEGMENT_FIELDS:
                self.assertIn(field, seg)
        self.assertEqual([s["kind"] for s in pkg["segments"]],
                         ["start_connector", "approach", "survey_entry_connector", "primary",
                          "return_connector", "return_approach", "final_home_connector"])
        # Execution-sequence ranges survive — they are how Scout maps a segment onto the route.
        self.assertEqual(pkg["segments"][0]["start_execution_seq"], 0)
        self.assertEqual(pkg["segments"][-1]["end_execution_seq"], 13)

    def test_detailed_original_execution_order_preserved_in_full(self):
        rec = real_record()
        pkg, _ = replan_package.build_v1_package(rec)
        self.assertEqual(len(pkg["original_execution_order"]), 14)
        self.assertEqual(pkg["original_execution_order"], rec["original_execution_order"])
        for entry in pkg["original_execution_order"]:
            for field in replan_package.V1_EXECUTION_ORDER_FIELDS:
                self.assertIn(field, entry)
        # It is a list of detailed OBJECTS, not an integer sequence list.
        self.assertTrue(all(isinstance(e, dict) for e in pkg["original_execution_order"]))
        self.assertEqual(pkg["original_execution_order"][0]["source_segment_id"],
                         "seg-01-start_connector")

    def test_execution_order_must_be_one_to_one_with_the_route(self):
        rec = real_record()
        rec["original_execution_order"] = rec["original_execution_order"][:-1]
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec)

    def test_thinned_metadata_fails_closed_rather_than_shipping(self):
        for field in ("segment_id", "kind", "coordinates", "start_execution_seq"):
            rec = real_record()
            rec["segments"][0].pop(field)
            with self.assertRaises(replan_package.PackageError):
                replan_package.build_v1_package(rec)
        for field in ("source_segment_id", "source_segment_kind", "source_index"):
            rec = real_record()
            rec["original_execution_order"][1].pop(field)
            with self.assertRaises(replan_package.PackageError):
                replan_package.build_v1_package(rec)

    # ── fail-closed guards ───────────────────────────────────────────────────────────────
    def test_altered_route_hash_refused(self):
        rec = real_record()
        rec["route_hash"] = "sha256:deadbeef"
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec)

    def test_altered_route_waypoint_refused(self):
        rec = real_record()
        rec["route_waypoints"][3]["latitude"] += 0.001    # hash no longer describes the route
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec)

    def test_non_revision_zero_and_mutable_records_refused(self):
        rec = real_record()
        rec["mission_revision"] = 1
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec)
        rec = real_record()
        rec["immutable"] = False
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec)

    def test_package_cannot_be_addressed_to_another_vehicle(self):
        rec = real_record()                               # vehicle_id 2
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec, vehicle_id="usv-3")
        # the record's own vehicle, in any spelling, is fine
        for spelling in (2, "2", "usv-2", "USV-2"):
            pkg, _ = replan_package.build_v1_package(rec, vehicle_id=spelling)
            self.assertEqual(pkg["vehicle_id"], "usv-2")

    def test_missing_geometry_refused(self):
        for field in ("navigable_geometry", "created_at", "route_hash"):
            rec = real_record()
            rec[field] = None
            if field == "navigable_geometry":
                rec["planning_inputs"]["navigable_boundary"] = None
            with self.assertRaises(replan_package.PackageError):
                replan_package.build_v1_package(rec)
        rec = real_record()
        rec["planning_inputs"]["planning_home"] = None
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(rec)

    def test_planning_home_fallback_accepts_the_records_positional_pair(self):
        # The record stores planning_home as a bare [lng, lat] pair, so normalize_home must
        # read that shape — otherwise the documented "fall back to the plan's planning home"
        # path never fires on a real record and a Scout with no verified Home looks home-less.
        rec = real_record()
        home = replan_package.normalize_home(rec["planning_inputs"]["planning_home"])
        self.assertEqual(home, {"latitude": 56.679159264264676,
                                "longitude": 12.81108856201172})
        self.assertIsNone(replan_package.normalize_home([0, 0]))       # null island, not a fix
        self.assertIsNone(replan_package.normalize_home([12.8]))       # not a pair
        self.assertIsNone(replan_package.normalize_home(None))

    def test_canonical_vehicle_id_helper(self):
        self.assertEqual(replan_package.canonical_vehicle_id(2), "usv-2")
        self.assertEqual(replan_package.canonical_vehicle_id("USV-3"), "usv-3")
        for bad in (None, "", "usv-x", 0, -1, True):
            self.assertIsNone(replan_package.canonical_vehicle_id(bad))


class V1SyncTests(unittest.TestCase):
    """POST /api/vehicles/{id}/replan/planning-package/sync — the manual, gated send."""

    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real_sr = scout_replan.requests
        scout_replan.requests = self.fake
        self._real_main = main.requests
        self.pixhawk = PixhawkReq()
        main.requests = self.pixhawk
        main._pixhawk_readback_cache.clear()

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main
        main._pixhawk_readback_cache.clear()

    def _seed(self, vid=SCOUT_VID, upload_status="VERIFIED", mission_id=FIXTURE_MISSION_ID):
        rec = real_record()
        rec["vehicle_id"] = vid
        rec["mission_id"] = mission_id
        rec["upload_status"] = upload_status
        main.original_missions[mission_id] = rec
        main.active_original_by_vehicle[vid] = mission_id
        return rec

    def _accept(self):
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"accepted": True, "stored": True,
                                "mission_id": FIXTURE_MISSION_ID,
                                "route_hash": FIXTURE_ROUTE_HASH}))

    def test_sync_sends_the_exact_v1_package(self):
        rec = self._seed()
        sent = {}

        def capture(method, url, **kw):
            sent["method"], sent["url"], sent["json"] = method, url, kw.get("json")
            return FakeResp({"accepted": True, "stored": True,
                             "mission_id": rec["mission_id"], "route_hash": rec["route_hash"]})
        self.fake.request = capture
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["synced"])
        self.assertEqual(sent["method"], "POST")
        self.assertEqual(sent["url"], "http://10.0.2.10:8090/agent/replan/planning_package")
        expected, _ = replan_package.build_v1_package(rec, vehicle_id="usv-2")
        self.assertEqual(sent["json"], expected)
        self.assertEqual(body["package_sent"], expected)
        # the evidence bundle the operator reviews
        self.assertEqual(body["operator_package"]["route_waypoint_count"], 14)
        self.assertTrue(body["route_unchanged_across_write"])
        self.assertIn("scout_package", body)
        self.assertIn("readiness", body)

    def test_unverified_mission_refused_without_contacting_scout(self):
        self._seed(upload_status="QUEUED", mission_id="msn-unverified-v1")
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertFalse(body["synced"])
        self.assertEqual(body["failed_stage"], "upload_status")
        self.assertEqual(body["error"], "mission_not_verified")
        self.assertEqual(self.fake.calls, [])          # nothing reached Scout
        self.assertEqual(self.pixhawk.calls, 0)        # and no mission download was paid for

    def test_hash_mismatch_refused(self):
        self._seed(mission_id="msn-v1-hashmm")
        self.pixhawk.body = dict(self.pixhawk.body, route_content_hash="sha256:somethingelse")
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["failed_stage"], "hash_match")
        self.assertEqual(body["error"], "route_hash_mismatch")
        self.assertEqual(self.fake.calls, [])          # never sent

    def test_unreachable_or_partial_readback_refused(self):
        self._seed(mission_id="msn-v1-readback")
        main._pixhawk_readback_cache.clear()
        main.requests = PixhawkReq(raise_exc=real_requests.ConnectionError("offline"))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"], "readback_unreachable")
        main._pixhawk_readback_cache.clear()
        main.requests = PixhawkReq({"waypoints": [], "count": 15, "partial": True,
                                    "route_content_hash": FIXTURE_ROUTE_HASH})
        r2 = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r2.status_code, 409)
        self.assertEqual(r2.json()["error"], "readback_partial")
        self.assertEqual(self.fake.calls, [])

    def test_wrong_vehicle_cannot_receive_another_vehicles_package(self):
        self._seed(vid=SCOUT_VID, mission_id="msn-v1-owned-by-2")
        r = self.client.post(f"/api/vehicles/{SAR_VID}/replan/planning-package/sync",
                             json={"mission_id": "msn-v1-owned-by-2"})
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["error"], "mission_belongs_to_another_vehicle")
        self.assertEqual(body["mission_vehicle_id"], "usv-2")
        self.assertEqual(self.fake.calls, [])
        # …and the builder refuses the same thing one layer down, independently.
        with self.assertRaises(replan_package.PackageError):
            replan_package.build_v1_package(
                main.original_missions["msn-v1-owned-by-2"], vehicle_id="usv-3")

    def test_sync_targets_only_the_selected_vehicles_local_agent(self):
        self._seed(vid=SAR_VID, mission_id="msn-v1-sar")
        self._accept()
        self.client.post(f"/api/vehicles/{SAR_VID}/replan/planning-package/sync", json={})
        urls = [u for _, u in self.fake.calls]
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith("http://10.0.3.10:8090") for u in urls))
        self.assertFalse(any(u.startswith("http://10.0.2.10:8090") for u in urls))

    def test_scout_rejection_is_surfaced_unchanged(self):
        self._seed(mission_id="msn-v1-rejected")
        scout_body = {"accepted": False,
                      "error": {"code": "PLANNING_PACKAGE_SCHEMA_INVALID",
                                "message": "original_execution_order[0] is not an integer"}}
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp(scout_body, status=400))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 400)
        body = r.json()
        self.assertFalse(body["synced"])
        self.assertEqual(body["failed_stage"], "scout_post")
        self.assertEqual(body["scout_post"]["outcome"], "rejected")
        # Scout's body verbatim — not summarized, not reinterpreted, not simplified away.
        self.assertEqual(body["scout_post"]["scout"], scout_body)

    def test_older_scout_without_the_v1_receiver_is_unsupported_not_success(self):
        self._seed(mission_id="msn-v1-old-scout")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "not found"}, status=404))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        body = r.json()
        self.assertFalse(body["synced"])
        self.assertFalse(body["scout_post"]["supported"])
        self.assertEqual(body["scout_post"]["outcome"], "unsupported")
        # No silent fall back to the OLD PUT contract when POST is unsupported.
        self.assertFalse(any(m == "PUT" for m, _ in self.fake.calls))

    def test_scout_timeout_is_unknown_never_a_fabricated_failure(self):
        self._seed(mission_id="msn-v1-timeout")
        self.fake.set("POST", "/agent/replan/planning_package", real_requests.Timeout("boom"))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["scout_post"]["outcome"], "unknown")
        self.assertEqual(main.replan_operations[-1]["outcome"], "unknown")

    def test_acceptance_makes_readiness_true(self):
        rec = self._seed(mission_id="msn-v1-ready")
        main.last_known_agent[SCOUT_VID] = {"home_status": {"home_position": {
            "latitude": 56.6791593, "longitude": 12.8110886}}}
        self._accept()
        # After acceptance Scout reports the package it stored and calls it consistent.
        stored = {"stored": True, "usable": True, "mission_id": rec["mission_id"],
                  "route_content_hash": rec["route_hash"], "route_waypoint_count": 14,
                  "consistency": main.PACKAGE_CONSISTENT,
                  "geometry_validation": {"boundary_available": True, "boundary_checked": True,
                                          "no_go_available": True, "no_go_checked": True,
                                          "connector_proven_safe": True}}
        self.fake.set("GET", "/agent/replan/planning_package", FakeResp(stored))
        self.fake.set("GET", "/agent/replan/status",
                      FakeResp({"package_consistency": main.PACKAGE_CONSISTENT,
                                "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"]}))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 200)
        readiness = r.json()["readiness"]
        self.assertTrue(readiness["mission_ready"])
        self.assertTrue(readiness["replanning_ready"])
        self.assertTrue(readiness["planning_package"]["stored"])
        self.assertTrue(readiness["planning_package"]["usable"])
        self.assertEqual(readiness["planning_package"]["route_count"], 14)
        self.assertTrue(readiness["planning_package"]["no_go_checked"])


class ReadinessPollingCostTests(unittest.TestCase):
    """Task 6: routine polling must not turn into a continuous Pixhawk mission download."""

    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real_sr = scout_replan.requests
        scout_replan.requests = self.fake
        self._real_main = main.requests
        self.pixhawk = PixhawkReq()
        main.requests = self.pixhawk
        main._pixhawk_readback_cache.clear()
        rec = real_record()
        rec["vehicle_id"] = SCOUT_VID
        main.original_missions[rec["mission_id"]] = rec
        main.active_original_by_vehicle[SCOUT_VID] = rec["mission_id"]

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main
        main._pixhawk_readback_cache.clear()

    def test_repeated_readiness_polls_share_one_bounded_readback(self):
        for _ in range(6):
            r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
            self.assertEqual(r.status_code, 200)
        # Six polls, ONE mission download — the rest reused age-labelled cached evidence.
        self.assertEqual(self.pixhawk.calls, 1)

    def test_cached_readback_is_labelled_with_its_age_never_shown_as_fresh(self):
        first = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness").json()
        self.assertFalse(first["vehicle_mission"]["readback_cached"])
        self.assertEqual(first["vehicle_mission"]["readback_age_s"], 0.0)
        second = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness").json()
        self.assertTrue(second["vehicle_mission"]["readback_cached"])
        self.assertIsNotNone(second["vehicle_mission"]["readback_age_s"])

    def test_expired_cache_is_refetched(self):
        self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        self.assertEqual(self.pixhawk.calls, 1)
        # age the cached entry past the TTL
        fetched_at, cached = main._pixhawk_readback_cache[SCOUT_VID]
        main._pixhawk_readback_cache[SCOUT_VID] = (
            fetched_at - timedelta(seconds=main.PIXHAWK_READBACK_TTL_S + 1), cached)
        self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        self.assertEqual(self.pixhawk.calls, 2)

    def test_sync_forces_fresh_reads_and_pays_exactly_two(self):
        # An explicit sync must NOT decide on cached evidence — it brackets the write with
        # two live consistency reads, and the readiness it computes afterwards reuses them.
        self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        before = self.pixhawk.calls
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp({"accepted": True}))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.pixhawk.calls - before, 2)
        body = r.json()
        self.assertFalse(body["pixhawk_readback_before"]["evidence_cached"])
        self.assertFalse(body["pixhawk_readback_after"]["evidence_cached"])

    def test_ordinary_polling_never_writes_to_scout(self):
        # The reconnect-safety rule, at the route level: a readiness/status/package poll makes
        # only GETs. Nothing a page refresh does can resend a package.
        for _ in range(3):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
            self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/status")
            self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/planning-package")
        self.assertTrue(self.fake.calls)
        self.assertEqual({m for m, _ in self.fake.calls}, {"GET"})


# ══════════════════════════════════════════════════════════════════════════════════════
# replan-planning-package-v1 GET — the NESTED Scout response the readiness view reads
# ══════════════════════════════════════════════════════════════════════════════════════
# Scout's v1 planning-package GET nests its evidence under package / summary / envelope /
# readiness. The operator normalizer used to read those fields flat off the response, so a
# stored, usable, Scout-certified-ready package surfaced as `mission_id: null`,
# `route_hash: null`, `hash_comparison_available: false` and REPLANNING READY false. These
# tests pin BOTH shapes through the one normalizer, and pin the comparisons the operator
# performs itself because Scout cannot.

LIVE_MISSION_ID = "msn-04fcc0083a7c"
LIVE_ROUTE_HASH = ("sha256:7b59e2e854b2d8aff4d537ea6d4cc1918e226d2777"
                   "9ffc2983b5d25ed24eaab8")
LIVE_ROUTE_COUNT = 40


def make_live_record(mission_id=LIVE_MISSION_ID, count=LIVE_ROUTE_COUNT, vid=SCOUT_VID):
    """A VERIFIED record shaped like the live mission: `count` route waypoints, its own
    canonical route hash, one navigable ring. Self-consistent — the hash is computed from
    these waypoints rather than pasted in, so nothing here can pass by coincidence."""
    rec = make_record(vid, mission_id)
    wps = [{"latitude": 56.0 + i * 1e-4, "longitude": 12.0 + i * 1e-4, "loiter_time_s": 0}
           for i in range(count)]
    rec["route_waypoints"] = wps
    rec["route_hash"] = mission_contract.route_content_hash(wps)
    rec["original_execution_order"] = [{"execution_seq": i, "source_segment_kind": "primary"}
                                       for i in range(count)]
    rec["upload_status"] = "VERIFIED"
    return rec


def v1_package_body(mission_id, route_hash, *, count=LIVE_ROUTE_COUNT, readiness=None,
                    no_go_zones=None, summary=None, stored=True, usable=True):
    """Scout's replan-planning-package-v1 GET body — the real nested shape.

    `readiness` / `summary` are shallow overrides so a test can flip ONE field (Scout's own
    readiness verdict, the no-go presence flags) without restating the whole response."""
    zones = [{"polygon": [[12.0, 56.0], [12.001, 56.0], [12.001, 56.001], [12.0, 56.0]]}] \
        if no_go_zones is None else no_go_zones
    ready = {"replanning_ready": True, "state": "REPLANNING_READY",
             "mission_verified": True, "route_hash_match": True,
             "navigable_geometry_checked": True, "no_go_zones_checked": True,
             # Scout cannot compare mission ids: the Pixhawk read-back exposes none.
             "mission_id_consistent": None, "connector_proven_safe": None}
    ready.update(readiness or {})
    summ = {"mission_id": mission_id, "route_hash": route_hash,
            "original_route_hash": route_hash, "route_waypoint_count": count,
            "has_navigable_geometry": True, "has_navigable_boundary": True,
            "no_go_zones_present": bool(zones), "no_go_zone_count": len(zones)}
    summ.update(summary or {})
    return {
        "stored": stored, "usable": usable,
        "package": {
            "mission_id": mission_id, "route_hash": route_hash,
            "original_route_hash": route_hash,
            "route_waypoints": [{"latitude": 56.0 + i * 1e-4, "longitude": 12.0 + i * 1e-4}
                                for i in range(count)],
            "navigable_geometry": [[[12.0, 56.0], [12.01, 56.0], [12.01, 56.01],
                                    [12.0, 56.0]]],
            "no_go_zones": zones,
        },
        "summary": summ,
        "envelope": {"package_version": "replan-planning-package-v1", "revision": 0},
        "readiness": ready,
    }


class V1PackageNormalizerTests(unittest.TestCase):
    """The pure reader (main._normalize_scout_package). No HTTP, no readiness rules — only
    'which field did we take, and from where'."""

    def test_nested_v1_mission_id_and_route_hash(self):
        ev = main._normalize_scout_package(v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH))
        self.assertEqual(ev["mission_id"], LIVE_MISSION_ID)
        self.assertEqual(ev["route_hash"], LIVE_ROUTE_HASH)
        self.assertTrue(ev["stored"])
        self.assertTrue(ev["usable"])

    def test_mission_id_precedence_package_then_summary_then_flat(self):
        body = v1_package_body("msn-from-package", LIVE_ROUTE_HASH)
        body["summary"]["mission_id"] = "msn-from-summary"
        body["mission_id"] = "msn-from-flat"
        self.assertEqual(main._normalize_scout_package(body)["mission_id"], "msn-from-package")
        del body["package"]["mission_id"]
        self.assertEqual(main._normalize_scout_package(body)["mission_id"], "msn-from-summary")
        del body["summary"]["mission_id"]
        self.assertEqual(main._normalize_scout_package(body)["mission_id"], "msn-from-flat")

    def test_route_hash_precedence_down_to_the_legacy_flat_keys(self):
        body = v1_package_body(LIVE_MISSION_ID, "sha256:from-package")
        body["package"]["original_route_hash"] = "sha256:from-package-original"
        body["summary"]["route_hash"] = "sha256:from-summary"
        body["summary"]["original_route_hash"] = "sha256:from-summary-original"
        body["route_hash"] = "sha256:from-flat"
        self.assertEqual(main._normalize_scout_package(body)["route_hash"], "sha256:from-package")
        del body["package"]["route_hash"]
        self.assertEqual(main._normalize_scout_package(body)["route_hash"],
                         "sha256:from-package-original")
        del body["package"]["original_route_hash"]
        self.assertEqual(main._normalize_scout_package(body)["route_hash"], "sha256:from-summary")
        del body["summary"]["route_hash"]
        self.assertEqual(main._normalize_scout_package(body)["route_hash"],
                         "sha256:from-summary-original")
        del body["summary"]["original_route_hash"]
        self.assertEqual(main._normalize_scout_package(body)["route_hash"], "sha256:from-flat")

    def test_route_count_from_summary(self):
        ev = main._normalize_scout_package(v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH))
        self.assertEqual(ev["route_count"], LIVE_ROUTE_COUNT)

    def test_route_count_falls_back_to_the_packages_own_route(self):
        body = v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH, count=7)
        del body["summary"]["route_waypoint_count"]
        self.assertEqual(main._normalize_scout_package(body)["route_count"], 7)
        # …and to `route` for a package that names the list that way instead.
        body["package"]["route"] = body["package"].pop("route_waypoints")
        self.assertEqual(main._normalize_scout_package(body)["route_count"], 7)
        # …and finally to the legacy flat counters.
        del body["package"]["route"]
        body["route_waypoint_count"] = 3
        self.assertEqual(main._normalize_scout_package(body)["route_count"], 3)

    def test_geometry_evidence_comes_from_summary_and_scout_readiness(self):
        ev = main._normalize_scout_package(v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH))
        self.assertTrue(ev["boundary_available"])       # summary.has_navigable_geometry
        self.assertTrue(ev["boundary_checked"])         # readiness.navigable_geometry_checked
        self.assertTrue(ev["no_go_available"])          # summary.no_go_zones_present
        self.assertTrue(ev["no_go_checked"])            # readiness.no_go_zones_checked

    def test_empty_no_go_set_is_explicit_evidence_not_an_absence(self):
        # Zero no-go zones is an ANSWER: Scout looked and reported none. Availability keys on
        # the field being reported at all, never on the count being non-zero.
        body = v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH, no_go_zones=[])
        ev = main._normalize_scout_package(body)
        self.assertEqual(body["summary"]["no_go_zone_count"], 0)
        self.assertFalse(body["summary"]["no_go_zones_present"])
        self.assertTrue(ev["no_go_available"])
        self.assertTrue(ev["no_go_checked"])

    def test_connector_proven_safe_null_is_preserved_never_widened(self):
        ev = main._normalize_scout_package(v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH))
        self.assertIsNone(ev["connector_proven_safe"])
        # an explicit False stays False, and an explicit True stays True
        for value in (False, True):
            ev = main._normalize_scout_package(
                v1_package_body(LIVE_MISSION_ID, LIVE_ROUTE_HASH,
                                readiness={"connector_proven_safe": value}))
            self.assertIs(ev["connector_proven_safe"], value)

    def test_legacy_flat_response_is_still_read(self):
        legacy = {"stored": True, "mission_id": "msn-legacy", "route_content_hash": "sha256:old",
                  "route_waypoint_count": 14}
        ev = main._normalize_scout_package(
            legacy, {"boundary_available": True, "boundary_checked": True,
                     "no_go_available": True, "no_go_checked": True,
                     "connector_proven_safe": True})
        self.assertEqual(ev["mission_id"], "msn-legacy")
        self.assertEqual(ev["route_hash"], "sha256:old")
        self.assertEqual(ev["route_count"], 14)
        self.assertTrue(ev["boundary_checked"])
        self.assertTrue(ev["no_go_checked"])
        self.assertIs(ev["connector_proven_safe"], True)
        self.assertIsNone(ev["scout_replanning_ready"])   # a pre-v1 Scout has no verdict

    def test_junk_and_missing_sections_never_raise(self):
        for body in (None, {}, [], "nope", {"package": None, "summary": 5, "readiness": []}):
            ev = main._normalize_scout_package(body)
            self.assertIsNone(ev["mission_id"])
            self.assertIsNone(ev["route_hash"])
            self.assertIsNone(ev["route_count"])


class V1ReadinessNormalizationTests(unittest.TestCase):
    """GET /api/vehicles/{id}/replan/readiness against a v1 Scout, end to end."""

    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real_sr = scout_replan.requests
        scout_replan.requests = self.fake
        self._real_main = main.requests
        self.pixhawk = PixhawkReq()
        main.requests = self.pixhawk
        main._pixhawk_readback_cache.clear()

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main
        main._pixhawk_readback_cache.clear()

    def _seed(self, mission_id=LIVE_MISSION_ID, readback_hash=None):
        """Seed the active mission and point the Pixhawk read-back at its route hash — the
        flight controller carrying exactly the approved route."""
        rec = make_live_record(mission_id)
        main.original_missions[mission_id] = rec
        main.active_original_by_vehicle[SCOUT_VID] = mission_id
        main.last_known_agent[SCOUT_VID] = {"home_status": {"home_position": {
            "latitude": 56.0, "longitude": 12.0}}}
        self.pixhawk.body = dict(self.pixhawk.body,
                                 route_content_hash=readback_hash or rec["route_hash"])
        main._pixhawk_readback_cache.clear()
        return rec

    def _scout_v1(self, rec, **kw):
        """Scout answers the package GET with the v1 nested body; status carries nothing the
        readiness view needs, exactly as a v1 Scout leaves it."""
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp(v1_package_body(rec["mission_id"], rec["route_hash"], **kw)))
        self.fake.set("GET", "/agent/replan/status", FakeResp({}))

    def _readiness(self):
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/replan/readiness")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_nested_v1_response_reports_the_package_and_clears_readiness(self):
        rec = self._seed()
        self._scout_v1(rec)
        body = self._readiness()
        pkg = body["planning_package"]
        self.assertTrue(body["mission_ready"])
        self.assertTrue(body["replanning_ready"])
        self.assertTrue(pkg["stored"])
        self.assertTrue(pkg["usable"])
        self.assertEqual(pkg["mission_id"], LIVE_MISSION_ID)
        self.assertTrue(pkg["mission_id_match"])
        self.assertEqual(pkg["route_hash"], rec["route_hash"])
        self.assertTrue(pkg["hash_match"])
        self.assertTrue(pkg["hash_comparison_available"])
        self.assertFalse(pkg["hash_mismatch"])
        self.assertTrue(pkg["consistent"])
        self.assertEqual(pkg["route_count"], LIVE_ROUTE_COUNT)
        self.assertTrue(pkg["boundary_available"])
        self.assertTrue(pkg["boundary_checked"])
        self.assertTrue(pkg["no_go_available"])
        self.assertTrue(pkg["no_go_checked"])
        self.assertIsNone(pkg["connector_proven_safe"])

    def test_operator_matches_the_mission_id_scout_reports_as_null(self):
        # Scout's mission_id_consistent is null — it cannot see an operator mission id in the
        # Pixhawk read-back. That is a "cannot compare", NOT a failure: the operator owns the
        # active mission record and makes the comparison itself.
        rec = self._seed()
        self._scout_v1(rec)
        pkg = self._readiness()["planning_package"]
        self.assertIsNone(pkg["scout_mission_id_consistent"])
        self.assertTrue(pkg["mission_id_match"])
        self.assertTrue(pkg["consistent"])

    def test_hash_match_spans_package_record_and_pixhawk(self):
        rec = self._seed()
        self._scout_v1(rec)
        self.assertTrue(self._readiness()["planning_package"]["hash_match"])
        # Same package, same record — but the flight controller is carrying something else.
        self._seed(mission_id="msn-readback-drift", readback_hash="sha256:some-other-route")
        rec2 = main.original_missions["msn-readback-drift"]
        self._scout_v1(rec2)
        body = self._readiness()
        pkg = body["planning_package"]
        self.assertFalse(pkg["hash_match"])          # the chain is broken at the vehicle
        self.assertFalse(pkg["hash_mismatch"])       # …not between package and record
        self.assertFalse(pkg["consistent"])
        self.assertFalse(body["replanning_ready"])
        self.assertTrue(any("read-back" in l for l in body["limitations"]))

    def test_mismatching_package_mission_id_stays_false(self):
        rec = self._seed(mission_id="msn-operator-side")
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp(v1_package_body("msn-a-different-mission", rec["route_hash"])))
        self.fake.set("GET", "/agent/replan/status", FakeResp({}))
        body = self._readiness()
        pkg = body["planning_package"]
        self.assertEqual(pkg["mission_id"], "msn-a-different-mission")
        self.assertFalse(pkg["mission_id_match"])
        self.assertFalse(pkg["consistent"])
        self.assertFalse(body["replanning_ready"])

    def test_mismatching_package_hash_stays_false(self):
        rec = self._seed(mission_id="msn-pkg-hash-drift")
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp(v1_package_body(rec["mission_id"], "sha256:not-the-approved-route")))
        self.fake.set("GET", "/agent/replan/status", FakeResp({}))
        body = self._readiness()
        pkg = body["planning_package"]
        self.assertTrue(pkg["hash_comparison_available"])
        self.assertFalse(pkg["hash_match"])
        self.assertTrue(pkg["hash_mismatch"])
        self.assertFalse(pkg["consistent"])
        self.assertFalse(body["replanning_ready"])
        self.assertTrue(any("hash" in l.lower() for l in body["limitations"]))

    def test_scout_readiness_false_blocks_operator_readiness(self):
        # Every operator-side comparison passes, yet Scout says it is not ready. Scout is the
        # authority on its own state, so REPLANNING READY stays false and says why.
        rec = self._seed(mission_id="msn-scout-not-ready")
        self._scout_v1(rec, readiness={"replanning_ready": False, "state": "PACKAGE_INCOMPLETE"})
        body = self._readiness()
        pkg = body["planning_package"]
        self.assertTrue(pkg["consistent"])            # the operator's own evidence is fine …
        self.assertFalse(body["replanning_ready"])    # … and Scout's verdict still governs
        self.assertFalse(pkg["scout_replanning_ready"])
        self.assertEqual(pkg["scout_state"], "PACKAGE_INCOMPLETE")
        self.assertTrue(any("Scout reports" in l for l in body["limitations"]))

    def test_empty_no_go_zones_still_read_as_available_and_checked(self):
        rec = self._seed(mission_id="msn-no-zones")
        self._scout_v1(rec, no_go_zones=[])
        pkg = self._readiness()["planning_package"]
        self.assertTrue(pkg["no_go_available"])
        self.assertTrue(pkg["no_go_checked"])
        self.assertTrue(pkg["consistent"])

    def test_connector_proven_safe_null_survives_to_the_response(self):
        rec = self._seed(mission_id="msn-connector-null")
        self._scout_v1(rec)
        pkg = self._readiness()["planning_package"]
        self.assertIsNone(pkg["connector_proven_safe"])
        self.assertNotIn("could not prove the current-position connector safe",
                         " ".join(self._readiness()["limitations"]))

    def test_legacy_flat_scout_still_reaches_replanning_ready(self):
        # The pre-v1 response, unchanged: flat mission_id / route_content_hash, a
        # geometry_validation block, a package-consistency verdict and NO readiness block.
        rec = self._seed(mission_id="msn-legacy-flat")
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": True, "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"],
                                "route_waypoint_count": LIVE_ROUTE_COUNT,
                                "consistency": main.PACKAGE_CONSISTENT,
                                "geometry_validation": {
                                    "boundary_available": True, "boundary_checked": True,
                                    "no_go_available": True, "no_go_checked": True,
                                    "connector_proven_safe": True}}))
        self.fake.set("GET", "/agent/replan/status",
                      FakeResp({"package_consistency": main.PACKAGE_CONSISTENT,
                                "mission_id": rec["mission_id"],
                                "route_content_hash": rec["route_hash"]}))
        body = self._readiness()
        pkg = body["planning_package"]
        self.assertEqual(pkg["mission_id"], rec["mission_id"])
        self.assertEqual(pkg["route_hash"], rec["route_hash"])
        self.assertEqual(pkg["route_count"], LIVE_ROUTE_COUNT)
        self.assertTrue(pkg["consistent"])
        self.assertTrue(body["replanning_ready"])
        self.assertIsNone(pkg["scout_replanning_ready"])

    def test_readiness_polling_of_a_v1_scout_never_syncs(self):
        # Readiness is polled every few seconds by the Agent page. It must stay read-only:
        # nothing here may re-send a package, on any response shape.
        rec = self._seed(mission_id="msn-poll-readonly")
        self._scout_v1(rec)
        for _ in range(4):
            self._readiness()
        self.assertTrue(self.fake.calls)
        self.assertEqual({m for m, _ in self.fake.calls}, {"GET"})
        self.assertFalse(any(u.endswith("/agent/replan/planning_package") and m != "GET"
                             for m, u in self.fake.calls))


if __name__ == "__main__":
    unittest.main()
