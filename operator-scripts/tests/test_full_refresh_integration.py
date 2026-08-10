"""Integration tests for the Agent Mission FULL REFRESH endpoint, through the real FastAPI route
and main.py wiring, with the Scout HTTP transport faked at scout_replan.requests / main.requests
(the same two seams tests/test_reconcile_integration.py and tests/test_mission_lifecycle.py use).

Run from operator-scripts/:  python -m unittest tests.test_full_refresh_integration   (no pytest).

THE CENTRAL REGRESSION (task Section 21). Operator approved mission A / hash H, Pixhawk reports
H, Scout's planning package reports mission A / H / usable, but Scout's mission-execution status
reports `binding_state: UNBOUND, verified_route_hash: null` — the observed defect: everything
proves the SAME mission is already on the vehicle, yet Start stays blocked, and nothing except a
redundant mission re-upload has ever recovered it. `ScriptedScout` below is STATEFUL specifically
so a test can prove the ONE thing that matters: this station's binding-reproof POST is issued and
observed BEFORE the fresh status read that decides the final `binding_state` — never the other
way around, which would silently report last round's stale UNBOUND forever.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
import mission_full_refresh  # noqa: E402
import scout_replan  # noqa: E402
import requests as real_requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

VID = 2   # "Scout" in main.VEHICLE_API_BASE / LOCAL_AGENT_API_BASE
MISSION_ID = "msn-full-refresh"


def waypoints(n, lat0=56.10):
    return [{"latitude": round(lat0 + i / 1e4, 7), "longitude": 12.88, "loiter_time_s": 0}
            for i in range(n)]


WPS = waypoints(6)
ROUTE_HASH = mission_contract.route_content_hash(WPS)
OTHER_HASH = mission_contract.route_content_hash(waypoints(6, lat0=57.20))


def approved_record(mission_id=MISSION_ID, wps=WPS, *, vid=VID, upload_status="VERIFIED"):
    return {
        "mission_id": mission_id, "vehicle_id": vid, "route_waypoints": list(wps),
        "route_hash": mission_contract.route_content_hash(wps), "mission_revision": 0,
        "planning_inputs": {}, "navigable_geometry": None, "no_go_zones": [], "segments": [],
        "original_execution_order": [], "metrics": {}, "immutable": True,
        "upload_status": upload_status, "verified_at": "2026-08-01T00:00:05+00:00",
        "created_at": "2026-08-01T00:00:00+00:00",
        "package_sync_state": "SYNCED", "package_sync_error": None,
        "package_synced_at": "2026-08-01T00:00:06+00:00",
    }


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


class ScriptedScout:
    """A stateful fake for scout_replan.requests (the Local Agent, 8090). `binding_state` starts
    UNBOUND; the reprove handler is the ONLY thing that may move it, and only when
    `reprove_outcome == "accept"` — mirroring the read-only reproof contract
    (scout_mission_execution.post_reprove): a rejected/unsupported/unknown reprove must leave it
    exactly where it was."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []
        self.binding_state = "UNBOUND"
        self.verified_route_hash = None
        self.reprove_supported = True
        self.reprove_outcome = "accept"        # "accept" | "reject" | "unknown"
        self.package_body = {"stored": True, "usable": True,
                             "package": {"mission_id": MISSION_ID, "route_hash": ROUTE_HASH},
                             "summary": {"route_waypoint_count": len(WPS)},
                             "readiness": {"replanning_ready": True}}
        self.replan_status_extra = {}
        self.mission_execution_reachable = True

    @property
    def writes(self):
        return [(m, u) for (m, u, _b) in self.calls if m != "GET"]

    def get(self, url, **kw):
        return self._resolve("GET", url, None)

    def request(self, method, url, **kw):
        return self._resolve(method, url, kw.get("json"))

    def _resolve(self, method, url, body):
        self.calls.append((method, url, body))
        if not self.mission_execution_reachable:
            raise real_requests.exceptions.ConnectionError("no route to host")
        if url.endswith("/agent/mission_execution/reprove"):
            if not self.reprove_supported:
                return FakeResp({}, 404)
            if self.reprove_outcome == "accept":
                self.binding_state = "BOUND"
                self.verified_route_hash = ROUTE_HASH
                return FakeResp({"accepted": True, "outcome": "REPROVED"}, 200)
            if self.reprove_outcome == "reject":
                return FakeResp({"error": "REPROVE_NOT_CONCLUSIVE"}, 409)
            raise real_requests.exceptions.Timeout("no response")
        if url.endswith("/agent/mission_execution/status"):
            return FakeResp({
                "supported": True, "state": "READY", "effective_state": "READY",
                "active_operation_id": None, "mission_id": MISSION_ID, "mode": "MANUAL",
                "sequence": {"current": 0, "count": len(WPS)},
                "replanning": {"active": False, "fsm_state": "MONITORING"},
                "return_completion": {}, "authority_status": "OPERATOR",
                "can_start": self.binding_state == "BOUND", "can_pause": False,
                "can_resume": False, "mission_execution_enabled": True, "last_error": None,
                "start_eligible": self.binding_state == "BOUND", "execution_ready": False,
                "authority_blocks_start": True,
                "binding": {"binding_state": self.binding_state,
                           "verified_route_hash": self.verified_route_hash,
                           "bound_original_mission_id":
                               MISSION_ID if self.binding_state == "BOUND" else None},
            }, 200)
        if url.endswith("/agent/replan/status"):
            body = {"supported": True}
            body.update(self.replan_status_extra)
            return FakeResp(body, 200)
        if url.endswith("/agent/replan/planning_package"):
            return FakeResp(self.package_body, 200)
        return FakeResp({}, 200)


class PixhawkReq:
    """main.requests — the vehicle Flask API (8080): Pixhawk read-back and /agent/state."""
    RequestException = real_requests.RequestException

    def __init__(self, route_hash=ROUTE_HASH, count=len(WPS), *, reachable=True, partial=False):
        self.route_hash, self.count = route_hash, count
        self.reachable, self.partial = reachable, partial
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if not self.reachable:
            raise real_requests.exceptions.ConnectionError("no route to host")
        if url.endswith("/agent/state"):
            return FakeResp({"ok": True}, 200)
        return FakeResp({
            "waypoints": [{"seq": i, "command": 16, "lat": 56.0, "lng": 12.0, "alt": 0}
                          for i in range(self.count + 1)],
            "count": self.count + 1, "partial": self.partial,
            "pixhawk_item_count": self.count + 1, "route_waypoint_count": self.count,
            "route_content_hash": self.route_hash,
        }, 200)


class FullRefreshIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self.scout = ScriptedScout()
        self._real_sr, scout_replan.requests = scout_replan.requests, self.scout
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
        main._save_mission_store = lambda: True
        main.last_known_agent[VID] = {"home_status": {
            "home_position": {"latitude": 56.679159, "longitude": 12.811089},
            "reachable": True, "verified": True, "verification_method": "PIXHAWK_READBACK",
            "ready_for_auto": True, "ready_for_rtl": True}}
        main.current_vehicle_state[VID] = {"raw_latest": {
            "agent": {"home_status": {
                "home_position": {"latitude": 56.679159, "longitude": 12.811089},
                "reachable": True, "verified": True,
                "verification_method": "PIXHAWK_READBACK",
                "ready_for_auto": True, "ready_for_rtl": True}}}, "received_at": None}
        main.comms_state_by_id[VID] = "CONNECTED"
        self._full_refresh_ops = list(main.full_refresh_operations)
        main.full_refresh_operations.clear()

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
        main.full_refresh_operations.clear()
        main.full_refresh_operations.extend(self._full_refresh_ops)

    def set_active(self, rec):
        main.original_missions[rec["mission_id"]] = rec
        main.active_original_by_vehicle[VID] = rec["mission_id"]

    def full_refresh(self):
        r = self.client.post(f"/api/vehicles/{VID}/mission-execution/full-refresh")
        return r

    def assert_no_vehicle_mutating_write(self):
        """No write reached Scout except the read-only reproof POST — never a mission upload,
        a package POST, a Home set, a mode/authority change. `PixhawkReq` above is GET-only by
        construction, so the 8080 side can never write anything either."""
        forbidden = ("/agent/mission_execution/start", "/agent/mission_execution/pause",
                    "/agent/mission_execution/resume", "/agent/mission_execution/stop",
                    "/agent/mission_execution/rearm", "/agent/replan/planning_package",
                    "/agent/replan/experiment", "/agent/replan/reset", "/agent/replan/config")
        for method, url in self.scout.writes:
            self.assertNotIn(method, ("DELETE",), url)
            for f in forbidden:
                self.assertFalse(url.endswith(f), f"unexpected vehicle write: {method} {url}")
        self.assertEqual(len(main.commands), len(self._commands),
                         "Full Refresh must create no command (and so cannot upload a mission)")

    # ── The central regression (Section 21) ────────────────────────────────────────────────
    def test_healthy_current_mission_recovers_from_unbound_without_a_reupload(self):
        self.set_active(approved_record())
        self.scout.binding_state = "UNBOUND"
        self.scout.verified_route_hash = None
        self.scout.reprove_outcome = "accept"

        r = self.full_refresh()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["mission"]["reconciliation"], mission_full_refresh.MATCHED)
        self.assertEqual(body["binding"]["binding_state"], "BOUND")
        self.assertEqual(body["binding"]["verified_route_hash"], ROUTE_HASH)
        self.assertTrue(body["readiness"]["can_start"])
        # No mission_id was minted; the SAME approved record is still the one active.
        self.assertEqual(set(main.original_missions), {MISSION_ID})
        self.assertEqual(main.active_original_by_vehicle[VID], MISSION_ID)
        self.assert_no_vehicle_mutating_write()

    def test_repeated_refresh_is_idempotent(self):
        self.set_active(approved_record())
        self.scout.reprove_outcome = "accept"
        first = self.full_refresh().json()
        second = self.full_refresh().json()
        self.assertEqual(first["mission"]["reconciliation"], second["mission"]["reconciliation"])
        self.assertEqual(first["binding"]["binding_state"], second["binding"]["binding_state"])
        self.assertNotEqual(first["operation_id"], second["operation_id"])

    # ── Missing approved mission: fail closed ──────────────────────────────────────────────
    def test_no_active_mission_fails_closed(self):
        r = self.full_refresh()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["error_code"], "NO_ACTIVE_MISSION")
        self.assertEqual(self.scout.calls, [], "nothing should be contacted with no active mission")

    # ── Package mismatch: PACKAGE_SYNC_REQUIRED, never silently repaired ───────────────────
    def test_package_mismatch_reports_sync_required_and_writes_no_package(self):
        self.set_active(approved_record())
        self.scout.package_body = {"stored": True, "usable": True,
                                   "package": {"mission_id": MISSION_ID, "route_hash": OTHER_HASH},
                                   "summary": {"route_waypoint_count": len(WPS)},
                                   "readiness": {"replanning_ready": False}}
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["mission"]["reconciliation"],
                         mission_full_refresh.PACKAGE_SYNC_REQUIRED)
        self.assertFalse(body["readiness"]["can_start"])
        self.assert_no_vehicle_mutating_write()

    # ── Pixhawk mismatch: definite, fail closed ────────────────────────────────────────────
    def test_pixhawk_mismatch_is_definite_and_fails_closed(self):
        self.set_active(approved_record())
        self.pixhawk.route_hash = OTHER_HASH
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["mission"]["reconciliation"], mission_full_refresh.PIXHAWK_MISMATCH)
        self.assertFalse(body["readiness"]["can_start"])
        self.assert_no_vehicle_mutating_write()

    # ── Transient failure: unavailable, never a mismatch ───────────────────────────────────
    def test_pixhawk_unreachable_is_evidence_unavailable_not_a_mismatch(self):
        self.set_active(approved_record())
        self.pixhawk.reachable = False
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["mission"]["reconciliation"], mission_full_refresh.EVIDENCE_UNAVAILABLE)
        self.assertNotEqual(body["mission"]["reconciliation"], mission_full_refresh.PIXHAWK_MISMATCH)
        self.assert_no_vehicle_mutating_write()

    # ── Scout unavailable entirely ──────────────────────────────────────────────────────────
    def test_scout_unavailable_is_reported_not_crashed(self):
        self.set_active(approved_record())
        self.scout.mission_execution_reachable = False
        r = self.full_refresh()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertFalse(body["readiness"]["can_start"])

    # ── Rebind failure: reprove rejected, binding stays exactly as Scout reports ──────────
    def test_rejected_reprove_leaves_binding_unbound(self):
        self.set_active(approved_record())
        self.scout.reprove_outcome = "reject"
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["binding"]["binding_state"], "UNBOUND")
        self.assertEqual(body["binding"]["reproof_outcome"], scout_replan.OUTCOME_REJECTED)
        self.assert_no_vehicle_mutating_write()

    def test_unsupported_reprove_leaves_binding_unbound_and_is_marked_unsupported(self):
        self.set_active(approved_record())
        self.scout.reprove_supported = False
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["binding"]["binding_state"], "UNBOUND")
        self.assertFalse(body["binding"]["reproof_supported"])

    # ── Ordering proof: reprove happens BEFORE the status this response is built from ─────
    def test_reprove_precedes_the_status_read_it_is_meant_to_affect(self):
        self.set_active(approved_record())
        self.scout.reprove_outcome = "accept"
        self.full_refresh()
        urls = [u for (_m, u, _b) in self.scout.calls]
        reprove_idx = next(i for i, u in enumerate(urls) if u.endswith("reprove"))
        status_idx = next(i for i, u in enumerate(urls) if u.endswith("mission_execution/status"))
        self.assertLess(reprove_idx, status_idx)

    # ── Concurrency: single-flight per vehicle ─────────────────────────────────────────────
    def test_concurrent_refresh_for_the_same_vehicle_is_rejected_busy(self):
        self.set_active(approved_record())
        with mission_full_refresh.vehicle_refresh_lock(VID):
            r = self.full_refresh()
        self.assertEqual(r.status_code, 409)
        body = r.json()
        self.assertEqual(body["error_code"], "FULL_REFRESH_BUSY")
        # Nothing was contacted while busy.
        self.assertEqual(self.scout.calls, [])

    def test_a_refresh_after_the_lock_is_released_succeeds_normally(self):
        self.set_active(approved_record())
        with mission_full_refresh.vehicle_refresh_lock(VID):
            pass
        r = self.full_refresh()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    # ── Diagnostics trace + logging surface (task Section 29/26 observability) ────────────
    def test_operation_is_recorded_in_the_diagnostics_trace(self):
        self.set_active(approved_record())
        self.full_refresh()
        trace = self.client.get(f"/api/mission-execution/full-refresh/operations?vehicle_id={VID}").json()
        self.assertEqual(trace["count"], 1)
        self.assertEqual(trace["operations"][0]["vehicle_id"], "usv-2")


if __name__ == "__main__":
    unittest.main()
