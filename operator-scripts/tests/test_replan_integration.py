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
import os
import sys
import unittest

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
        # Keep the Pixhawk readback (main.requests → Flask 8080) from hitting the network:
        # a raising fake makes _scout_mission_read resolve reachable:false immediately.
        self._real_main = main.requests

        class RaisingReq:
            RequestException = real_requests.RequestException

            def get(self, *a, **k):
                raise real_requests.ConnectionError("offline")
        main.requests = RaisingReq()

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main

    def _seed(self, upload_status="VERIFIED", mission_id="msn-ready-1"):
        rec = make_record(SCOUT_VID, mission_id)
        rec["upload_status"] = upload_status
        main.original_missions[mission_id] = rec
        main.active_original_by_vehicle[SCOUT_VID] = mission_id
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


if __name__ == "__main__":
    unittest.main()
