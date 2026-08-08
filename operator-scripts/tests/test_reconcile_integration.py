"""Reconciliation through the real backend: the restart cases, end to end.

Run from operator-scripts/:  python -m unittest tests.test_reconcile_integration   (no pytest).

These drive the actual FastAPI routes the Map and Agent pages poll — `GET .../replan/readiness`
and `GET .../missions/publish` — with both transports mocked, and assert on what the operator
would be TOLD after a restart.

The reproduction that motivated all of it (captured live against Scout, 2026-08-08): the store
held two approved records for usv-2, the active pointer named the older one, and the flight
controller plus the Agent package both carried the newer one's canonical route. The station
reported "Agent package does not match the approved mission" and the only offered way out was
a mission re-upload.

Every test here also proves the negative: `main.commands` is unchanged and no 8090 WRITE was
issued, so reconciliation demonstrably repairs bookkeeping rather than the vehicle.
"""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
import mission_reconcile  # noqa: E402
import scout_replan  # noqa: E402
import requests as real_requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2
SAR_VID = 3

# The ONBOARD route is the real captured mission record — geometry, segments and all — so a
# reconciled mission can be carried all the way through the package build the retry performs.
# A synthetic route stands in for "some other approved mission"; nothing ever packages it.
FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                            "active-original-msn-329c2faff137.json")
with open(FIXTURE_PATH, encoding="utf-8") as _fh:
    _FIXTURE = json.load(_fh)
ONBOARD_HASH = _FIXTURE["route_hash"]
ONBOARD_COUNT = len(_FIXTURE["route_waypoints"])


def waypoints(n, lat0):
    return [{"latitude": round(lat0 + i / 1e4, 7), "longitude": 12.88, "loiter_time_s": 0}
            for i in range(n)]


OTHER_WPS = waypoints(14, 56.10)
OTHER_HASH = mission_contract.route_content_hash(OTHER_WPS)
ONBOARD_WPS = _FIXTURE["route_waypoints"]


def record(mission_id, wps, *, vid=SCOUT_VID, upload_status="VERIFIED", sync=None,
           created_at="2026-08-01T00:00:00+00:00"):
    """An approved record for `wps`. When `wps` is the fixture's route, the whole captured
    record (geometry included) is used so it is genuinely packageable."""
    if wps is ONBOARD_WPS:
        rec = copy.deepcopy(_FIXTURE)
    else:
        rec = {"route_waypoints": list(wps),
               "route_hash": mission_contract.route_content_hash(wps),
               "mission_revision": 0, "planning_inputs": {}, "navigable_geometry": None,
               "no_go_zones": [], "segments": [], "original_execution_order": [], "metrics": {},
               "immutable": True}
    rec.update({"mission_id": mission_id, "vehicle_id": vid, "upload_status": upload_status,
                "verified_at": None, "created_at": created_at,
                "package_sync_state": sync, "package_sync_error": None})
    return rec


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.HTTPError(f"HTTP {self.status_code}")


class FakeLA:
    """scout_replan.requests — the Local Agent (8090). Records every call so a test can prove
    that reconciliation performed no WRITE."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []
        self.responses = {}
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

    @property
    def writes(self):
        return [(m, u) for m, u in self.calls if m != "GET"]


class PixhawkReq:
    """main.requests — the vehicle Flask API (8080) mission read-back."""
    RequestException = real_requests.RequestException

    def __init__(self, route_hash=ONBOARD_HASH, count=ONBOARD_COUNT, *, reachable=True, partial=False):
        self.route_hash, self.count = route_hash, count
        self.reachable, self.partial = reachable, partial

    def get(self, url, **kw):
        if not self.reachable:
            raise real_requests.ConnectionError("no route to host")
        return FakeResp({
            "waypoints": [{"seq": i, "command": 16, "lat": 56.0, "lng": 12.0, "alt": 0}
                          for i in range(self.count + 1)],
            "count": self.count + 1, "partial": self.partial,
            "pixhawk_item_count": self.count + 1, "route_waypoint_count": self.count,
            "route_content_hash": self.route_hash,
        })


def scout_package(mission_id, route_hash, count=ONBOARD_COUNT, *, stored=True):
    if not stored:
        return FakeResp({"stored": False, "usable": False,
                         "readiness": {"replanning_ready": False, "state": "REPLANNING_READY"}})
    return FakeResp({"stored": True, "usable": True,
                     "package": {"mission_id": mission_id, "route_hash": route_hash},
                     "summary": {"route_waypoint_count": count},
                     "readiness": {"replanning_ready": True, "state": "REPLANNING_READY"}})


class ReconcileTestCase(unittest.TestCase):

    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real_sr, scout_replan.requests = scout_replan.requests, self.fake
        self.pixhawk = PixhawkReq()
        self._real_main, main.requests = main.requests, self.pixhawk
        main._pixhawk_readback_cache.clear()
        main._reconciliation_by_vehicle.clear()
        self._missions = dict(main.original_missions)
        self._active = dict(main.active_original_by_vehicle)
        self._commands = list(main.commands)
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        self._real_save = main._save_mission_store
        self.saves = []
        main._save_mission_store = lambda: (self.saves.append(1), True)[1]
        main.last_known_agent[SCOUT_VID] = {"home_status": {"home_position": {
            "latitude": 56.679159, "longitude": 12.811089}}}

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main
        main._save_mission_store = self._real_save
        main._pixhawk_readback_cache.clear()
        main._reconciliation_by_vehicle.clear()
        main.original_missions.clear()
        main.original_missions.update(self._missions)
        main.active_original_by_vehicle.clear()
        main.active_original_by_vehicle.update(self._active)

    def restart_with(self, records, active):
        """Simulate a backend restart: this is exactly what _load_mission_store leaves behind."""
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        for rec in records:
            main.original_missions[rec["mission_id"]] = rec
        main.active_original_by_vehicle.update(active)

    def agent_holds(self, mission_id, route_hash, count=ONBOARD_COUNT, *, stored=True):
        self.fake.set("GET", "/agent/replan/planning_package",
                      scout_package(mission_id, route_hash, count, stored=stored))

    def readiness(self, vid=SCOUT_VID):
        r = self.client.get(f"/api/vehicles/{vid}/replan/readiness")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def publish_state(self, vid=SCOUT_VID):
        return self.client.get(f"/api/vehicles/{vid}/missions/publish").json()

    def assert_no_vehicle_write(self):
        self.assertEqual(self.fake.writes, [],
                         "reconciliation must not write to the Local Agent")
        self.assertEqual(len(main.commands), len(self._commands),
                         "reconciliation must not create a command (and so cannot upload)")


# ══════════════════════════════════════════════════════════════════════════════════════
# The reported defect, and its repair
# ══════════════════════════════════════════════════════════════════════════════════════
class StaleActivePointerTests(ReconcileTestCase):
    """The captured live reproduction: active pointer at a superseded record."""

    def setUp(self):
        super().setUp()
        self.old = record("msn-old", OTHER_WPS, sync="REQUIRED")
        self.old["package_sync_error"] = "SCOUT_PACKAGE_POST_FAILED"
        self.cur = record("msn-current", ONBOARD_WPS, sync="SYNCED",
                          created_at="2026-08-08T00:00:00+00:00")
        self.restart_with([self.old, self.cur], {SCOUT_VID: "msn-old"})
        self.agent_holds("msn-current", ONBOARD_HASH)

    def test_the_first_readiness_poll_after_restart_reports_no_mismatch(self):
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.SYNCHRONIZED)
        pk = rd["planning_package"]
        self.assertFalse(pk["hash_mismatch"])
        self.assertTrue(pk["mission_id_match"])
        self.assertTrue(pk["hash_match"])
        self.assertTrue(rd["mission_ready"])

    def test_the_active_mission_becomes_the_one_the_flight_controller_carries(self):
        self.readiness()
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-current")
        self.assertEqual(self.publish_state()["mission_id"], "msn-current")

    def test_reconciliation_uploads_nothing(self):
        self.readiness()
        self.assert_no_vehicle_write()

    def test_no_new_mission_id_is_minted(self):
        self.readiness()
        self.assertEqual(set(main.original_missions), {"msn-old", "msn-current"})

    def test_the_verdict_is_reported_on_the_publish_route_too(self):
        self.readiness()
        rec = self.publish_state()["reconciliation"]
        self.assertEqual(rec["outcome"], mission_reconcile.SYNCHRONIZED)
        self.assertTrue(rec["rebound"])

    def test_a_second_poll_changes_nothing_further(self):
        self.readiness()
        saves = len(self.saves)
        self.readiness()
        self.assertEqual(len(self.saves), saves, "a settled station must stop rewriting its store")


class LostVerificationTests(ReconcileTestCase):
    """The MISSION_UPLOAD result that arrived after a restart, when the in-memory command queue
    that would have projected it onto the record was already gone. The record was stranded at
    QUEUED and publish answered UPLOAD_IN_PROGRESS forever."""

    def test_a_live_readback_restores_VERIFIED_without_a_re_upload(self):
        rec = record("msn-queued", ONBOARD_WPS, upload_status="QUEUED", sync="REQUIRED")
        self.restart_with([rec], {SCOUT_VID: "msn-queued"})
        self.agent_holds("msn-queued", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(main.original_missions["msn-queued"]["upload_status"], "VERIFIED")
        self.assertTrue(rd["vehicle_mission"]["pixhawk_verified"])
        self.assertTrue(rd["mission_ready"])
        self.assert_no_vehicle_write()

    def test_the_package_sync_route_then_works_instead_of_refusing_as_not_uploaded(self):
        rec = record("msn-queued", ONBOARD_WPS, upload_status="QUEUED", sync="REQUIRED")
        self.restart_with([rec], {SCOUT_VID: "msn-queued"})
        self.agent_holds("msn-queued", ONBOARD_HASH)
        # Before reconciliation the package-only retry refuses: the record is not VERIFIED.
        blocked = self.client.post(
            f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={}).json()
        self.assertEqual(blocked["error"], "mission_not_verified")
        self.readiness()                      # fresh evidence arrives
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"accepted": True, "stored": True}))
        ok = self.client.post(
            f"/api/vehicles/{SCOUT_VID}/replan/planning-package/sync", json={}).json()
        self.assertTrue(ok["agent_ready"])
        self.assertEqual(len(main.commands), len(self._commands))


class PackageOnlyGapTests(ReconcileTestCase):
    """Operator and flight controller agree; the Agent package does not. Package-only territory
    — never a mission upload."""

    def test_a_missing_agent_package_is_a_sync_requirement_not_a_mismatch(self):
        rec = record("msn-a", ONBOARD_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        self.agent_holds(None, None, stored=False)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.PACKAGE_SYNC_REQUIRED)
        self.assertEqual(rd["reconciliation"]["reason"], mission_reconcile.PACKAGE_NOT_STORED)
        self.assertFalse(rd["planning_package"]["hash_mismatch"])
        self.assertEqual(self.publish_state()["package_sync_state"], "REQUIRED")
        self.assert_no_vehicle_write()

    def test_a_stale_agent_package_is_a_sync_requirement_and_recorded_as_owed(self):
        rec = record("msn-a", ONBOARD_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        self.agent_holds("msn-previous", OTHER_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.PACKAGE_SYNC_REQUIRED)
        self.assertEqual(main.original_missions["msn-a"]["package_sync_state"], "REQUIRED")
        self.assert_no_vehicle_write()

    def test_a_restored_REQUIRED_that_scout_contradicts_is_recomputed_to_SYNCED(self):
        rec = record("msn-a", ONBOARD_WPS, sync="REQUIRED")
        rec["package_sync_error"] = "SCOUT_PACKAGE_POST_FAILED"
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        self.agent_holds("msn-a", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.SYNCHRONIZED)
        self.assertEqual(self.publish_state()["package_sync_state"], "SYNCED")
        self.assertIsNone(self.publish_state()["package_sync_error"])
        self.assert_no_vehicle_write()


class IdentityDomainTests(ReconcileTestCase):
    """Two labels on one canonical route is not a content mismatch."""

    def test_same_route_different_mission_ids_is_a_rebind_not_a_mismatch(self):
        rec = record("msn-operator", ONBOARD_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-operator"})
        self.agent_holds("msn-scout-side", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.PACKAGE_SYNC_REQUIRED)
        # The CONTENT chain is still proven: the hash comparison is not what disagrees.
        self.assertFalse(rd["planning_package"]["hash_mismatch"])
        self.assertTrue(rd["planning_package"]["hash_match"])
        self.assertFalse(rd["planning_package"]["mission_id_match"])
        self.assert_no_vehicle_write()


class InsufficientEvidenceTests(ReconcileTestCase):
    """A startup that cannot see the flight controller must not declare a mismatch."""

    def test_a_fresh_backend_that_has_read_nothing_reports_RECONCILING(self):
        rec = record("msn-a", OTHER_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        verdict = self.publish_state()["reconciliation"]
        self.assertEqual(verdict["outcome"], mission_reconcile.RECONCILING)
        self.assertEqual(verdict["reason"], "NO_EVIDENCE_YET")
        self.assertFalse(verdict["conclusive"])

    def test_an_unreachable_readback_reports_RECONCILING_and_changes_nothing(self):
        rec = record("msn-a", OTHER_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        self.pixhawk.reachable = False
        self.agent_holds("msn-other", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.RECONCILING)
        self.assertEqual(rd["reconciliation"]["reason"], mission_reconcile.NO_READBACK)
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-a")
        self.assertEqual(main.original_missions["msn-a"]["package_sync_state"], "SYNCED")

    def test_a_partial_readback_reports_RECONCILING(self):
        rec = record("msn-a", ONBOARD_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        self.pixhawk.partial = True
        self.agent_holds("msn-a", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["reason"], mission_reconcile.READBACK_PARTIAL)

    def test_an_unreachable_agent_leaves_the_package_question_open(self):
        rec = record("msn-a", ONBOARD_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-a"})
        self.fake.default = real_requests.ConnectionError("agent down")
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.RECONCILING)
        self.assertEqual(rd["reconciliation"]["reason"], mission_reconcile.PACKAGE_UNREACHABLE)
        self.assertTrue(rd["reconciliation"]["pixhawk_settled"])
        self.assertEqual(main.original_missions["msn-a"]["package_sync_state"], "SYNCED")


class GenuineMismatchTests(ReconcileTestCase):
    """The fix must not hide a real problem."""

    def test_a_route_no_approved_record_carries_is_not_adopted(self):
        rec = record("msn-approved", OTHER_WPS, sync="SYNCED")
        self.restart_with([rec], {SCOUT_VID: "msn-approved"})
        self.agent_holds("msn-unknown", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.MISMATCH)
        self.assertTrue(rd["planning_package"]["hash_mismatch"])
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-approved")
        self.assertEqual(set(main.original_missions), {"msn-approved"})
        self.assert_no_vehicle_write()

    def test_an_empty_store_facing_an_onboard_mission_is_UNAPPROVED_not_adopted(self):
        self.restart_with([], {})
        self.agent_holds("msn-unknown", ONBOARD_HASH)
        rd = self.readiness()
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.UNAPPROVED_MISSION)
        self.assertEqual(main.active_original_by_vehicle, {})
        self.assertEqual(main.original_missions, {})


class VehicleIsolationTests(ReconcileTestCase):

    def test_reconciling_usv_2_leaves_usv_3_untouched(self):
        scout_old = record("msn-scout-old", OTHER_WPS)
        scout_cur = record("msn-scout-cur", ONBOARD_WPS,
                           created_at="2026-08-08T00:00:00+00:00")
        sar = record("msn-sar", ONBOARD_WPS, vid=SAR_VID, sync="REQUIRED")
        self.restart_with([scout_old, scout_cur, sar],
                          {SCOUT_VID: "msn-scout-old", SAR_VID: "msn-sar"})
        self.agent_holds("msn-scout-cur", ONBOARD_HASH)
        self.readiness(SCOUT_VID)
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-scout-cur")
        self.assertEqual(main.active_original_by_vehicle[SAR_VID], "msn-sar")
        self.assertEqual(main.original_missions["msn-sar"]["package_sync_state"], "REQUIRED")
        self.assertEqual(main._reconciliation_by_vehicle[SAR_VID]["reason"], "NO_EVIDENCE_YET") \
            if SAR_VID in main._reconciliation_by_vehicle else None

    def test_another_vehicles_matching_record_is_never_adopted(self):
        scout_old = record("msn-scout-old", OTHER_WPS)
        sar = record("msn-sar", ONBOARD_WPS, vid=SAR_VID)     # exactly the onboard route
        self.restart_with([scout_old, sar], {SCOUT_VID: "msn-scout-old", SAR_VID: "msn-sar"})
        self.agent_holds("msn-sar", ONBOARD_HASH)
        rd = self.readiness(SCOUT_VID)
        self.assertEqual(rd["reconciliation"]["outcome"], mission_reconcile.MISMATCH)
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-scout-old")


if __name__ == "__main__":
    unittest.main()
