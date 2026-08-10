"""Integration tests for the Agent Mission FULL REFRESH endpoint, through the real FastAPI route
and main.py wiring, with the Scout HTTP transport faked at scout_replan.requests / main.requests
(the same two seams tests/test_reconcile_integration.py and tests/test_mission_lifecycle.py use).

Run from operator-scripts/:  python -m unittest tests.test_full_refresh_integration   (no pytest).

THE CENTRAL REGRESSION (task Section 17), CORRECTED. Operator approved mission A / hash H,
Pixhawk reports H, Scout's planning package reports mission A / H / usable, and Scout's
mission-execution status starts at `state NOT_READY, verified_route_hash: null,
binding_state: UNBOUND` — the observed defect: everything proves the SAME mission is already on
the vehicle, yet Start stays blocked, and nothing except a redundant mission re-upload has ever
recovered it.

THE SEMANTIC CORRECTION: a successful reprove (`outcome: REPROVED`) restores
`verified_route_hash` / `state: READY` / `start_eligible` / `can_start` — proof of the route —
but correctly LEAVES `binding_state: UNBOUND`, because binding means a LIVE execution owns the
mission identity, which is not true for an idle, not-yet-started mission. `binding_state` only
becomes BOUND once the mission is actually RUNNING. A Full Refresh that required BOUND for
success would reproduce exactly the bug this correction exists to fix — see
`test_healthy_current_mission_recovers_from_unbound_without_a_reupload` below, the central
regression this file exists to pin.

`ScriptedScout` below is STATEFUL specifically so a test can prove the ONE thing that matters:
this station's binding-reproof POST (`/agent/mission_execution/reprove_binding`) is issued and
observed BEFORE the fresh status read that decides the final proof state — never the other way
around, which would silently report last round's stale UNBOUND-and-unproven state forever."""
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
    """A stateful fake for scout_replan.requests (the Local Agent, 8090).

    `verified_route_hash` is PROOF state, restored by a successful reprove
    (`outcome in {REPROVED, ALREADY_PROVEN}`) from a fresh Pixhawk/package proof — modeled here
    by `reprove_route_hash`, the hash Scout would prove. `execution_state` is the SEPARATE
    lifecycle/binding axis: NOT_READY/READY while idle, RUNNING once a run owns the mission —
    `binding_state` is derived from it (BOUND only while RUNNING) and is NEVER moved by a
    successful reprove on its own, exactly mirroring Scout's real contract (task Section 1): a
    proven, READY, idle mission stays UNBOUND, and BOUND is meaningful only for a live run.
    Every other reprove outcome (a rejection, an unsupported route, a definite mismatch Scout
    itself proved, BUSY, …) must leave BOTH axes exactly where they were."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []
        self.verified_route_hash = None
        self.execution_state = "NOT_READY"     # "NOT_READY" | "READY" | "RUNNING" — test-set
        self.reprove_supported = True
        # One of scout_mission_execution.REPROVE_OUTCOMES, or "TIMEOUT" (an unknown transport
        # failure) / "GENERIC_REJECT" (a bare non-409 4xx) for the two transport-level gaps that
        # never reach Scout's own outcome vocabulary at all.
        self.reprove_outcome = "REPROVED"
        self.reprove_route_hash = ROUTE_HASH
        self.package_body = {"stored": True, "usable": True,
                             "package": {"mission_id": MISSION_ID, "route_hash": ROUTE_HASH},
                             "summary": {"route_waypoint_count": len(WPS)},
                             "readiness": {"replanning_ready": True}}
        self.replan_status_extra = {}
        self.mission_execution_reachable = True

    @property
    def binding_state(self):
        return "BOUND" if self.execution_state == "RUNNING" else "UNBOUND"

    @property
    def bound_original_mission_id(self):
        return MISSION_ID if self.execution_state == "RUNNING" else None

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
        if url.endswith("/agent/mission_execution/reprove_binding"):
            if not self.reprove_supported:
                return FakeResp({}, 404)
            if self.reprove_outcome == "TIMEOUT":
                raise real_requests.exceptions.Timeout("no response")
            if self.reprove_outcome == "GENERIC_REJECT":
                return FakeResp({"error": "REPROVE_NOT_CONCLUSIVE"}, 400)
            if self.reprove_outcome == "BUSY":
                return FakeResp({"outcome": "BUSY"}, 409)
            if self.reprove_outcome in ("REPROVED", "ALREADY_PROVEN"):
                self.verified_route_hash = self.reprove_route_hash
                if self.execution_state == "NOT_READY":
                    self.execution_state = "READY"
                return FakeResp({"accepted": True, "outcome": self.reprove_outcome}, 200)
            # PACKAGE_MISMATCH / PIXHAWK_MISMATCH / MISSION_ID_MISMATCH / NO_CURRENT_PACKAGE /
            # NO_CURRENT_MISSION / LIFECYCLE_NOT_REPROVABLE / EVIDENCE_UNAVAILABLE /
            # INTERNAL_ERROR — Scout answers 200 with its own outcome word and changes NOTHING.
            return FakeResp({"accepted": False, "outcome": self.reprove_outcome}, 200)
        if url.endswith("/agent/mission_execution/status"):
            ready_now = self.execution_state == "READY" and self.verified_route_hash is not None
            return FakeResp({
                "supported": True, "state": self.execution_state,
                "effective_state": self.execution_state,
                "active_operation_id": None, "mission_id": MISSION_ID,
                "mode": "AUTO" if self.execution_state == "RUNNING" else "MANUAL",
                "sequence": {"current": 0, "count": len(WPS)},
                "replanning": {"active": False, "fsm_state": "MONITORING"},
                "return_completion": {}, "authority_status":
                    "LOCAL_AGENT" if self.execution_state == "RUNNING" else "OPERATOR",
                "can_start": ready_now,
                "can_pause": self.execution_state == "RUNNING", "can_resume": False,
                "mission_execution_enabled": True, "last_error": None,
                "start_eligible": bool(self.verified_route_hash)
                                  and self.execution_state != "RUNNING",
                "execution_ready": False,
                "authority_blocks_start": self.execution_state != "RUNNING",
                "binding": {"binding_state": self.binding_state,
                           "verified_route_hash": self.verified_route_hash,
                           "bound_original_mission_id": self.bound_original_mission_id},
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

    # ── The central regression (task Section 17), CORRECTED ───────────────────────────────
    def test_healthy_current_mission_recovers_from_unbound_without_a_reupload(self):
        # Initial: approved mission A/H, Pixhawk H, package A/H, Scout NOT_READY,
        # verified_route_hash null, binding UNBOUND, MISSION_ROUTE_UNVERIFIED-equivalent.
        self.set_active(approved_record())
        self.scout.execution_state = "NOT_READY"
        self.scout.verified_route_hash = None
        self.scout.reprove_outcome = "REPROVED"

        r = self.full_refresh()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["mission"]["reconciliation"], mission_full_refresh.MATCHED)
        self.assertEqual(body["binding"]["reproof_outcome"], "REPROVED")
        self.assertTrue(body["binding"]["reproof_success"])
        self.assertEqual(body["binding"]["verified_route_hash"], ROUTE_HASH)
        # THE CORRECTED ASSERTION: binding stays UNBOUND — this is the healthy, expected idle
        # result, NOT a failure. BOUND would mean a live execution owns the mission identity,
        # which is not true before Start.
        self.assertEqual(body["binding"]["binding_state"], "UNBOUND")
        self.assertTrue(body["readiness"]["can_start"])
        self.assertIsNone(body["readiness"].get("error_code"))
        # No mission_id was minted; the SAME approved record is still the one active.
        self.assertEqual(set(main.original_missions), {MISSION_ID})
        self.assertEqual(main.active_original_by_vehicle[VID], MISSION_ID)
        self.assert_no_vehicle_mutating_write()

    def test_repeated_refresh_already_proven_is_idempotent(self):
        # Second refresh: Scout answers ALREADY_PROVEN (task Section 18). Same mission_id, same
        # route hash, still READY, no mutation, no mission upload, no warning.
        self.set_active(approved_record())
        self.scout.reprove_outcome = "REPROVED"
        first = self.full_refresh().json()
        self.assertTrue(first["ok"], first)

        self.scout.reprove_outcome = "ALREADY_PROVEN"
        second = self.full_refresh().json()
        self.assertTrue(second["ok"], second)
        self.assertEqual(second["binding"]["reproof_outcome"], "ALREADY_PROVEN")
        self.assertTrue(second["binding"]["reproof_success"])
        self.assertEqual(first["mission"]["reconciliation"], second["mission"]["reconciliation"])
        self.assertEqual(first["binding"]["binding_state"], second["binding"]["binding_state"])
        self.assertEqual(first["binding"]["verified_route_hash"],
                         second["binding"]["verified_route_hash"])
        self.assertTrue(second["readiness"]["can_start"])
        self.assertNotEqual(first["operation_id"], second["operation_id"])
        self.assert_no_vehicle_mutating_write()

    # ── The active-execution rule (task Sections 7, 19) ────────────────────────────────────
    def test_running_mission_stays_bound_and_is_not_reset_or_rewound(self):
        # Scout status: state RUNNING, binding BOUND, bound_original_mission_id A. Scout
        # reprove: ALREADY_PROVEN. Full Refresh must not reset state, rewind, or alter the
        # mission — the active BOUND remains visible and healthy.
        self.set_active(approved_record())
        self.scout.execution_state = "RUNNING"
        self.scout.verified_route_hash = ROUTE_HASH
        self.scout.reprove_outcome = "ALREADY_PROVEN"

        r = self.full_refresh()
        body = r.json()
        self.assertTrue(body["ok"], body)
        self.assertEqual(body["binding"]["binding_state"], "BOUND")
        self.assertEqual(body["binding"]["bound_original_mission_id"], MISSION_ID)
        self.assertEqual(body["binding"]["reproof_outcome"], "ALREADY_PROVEN")
        self.assertTrue(body["binding"]["reproof_success"])
        # RUNNING is unaffected by the refresh — still RUNNING/BOUND afterwards, nothing rewound.
        self.assertEqual(self.scout.execution_state, "RUNNING")
        self.assertEqual(self.scout.binding_state, "BOUND")
        self.assert_no_vehicle_mutating_write()

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
        # Task Section 9/20: approved == Pixhawk != package. Scout only sees package-vs-Pixhawk
        # at its own layer and may answer PACKAGE_MISMATCH — the Operator's own three-way
        # evidence is what reclassifies this as PACKAGE_SYNC_REQUIRED (Case 2), never Scout's
        # raw word displayed as the final system classification.
        self.set_active(approved_record())
        self.scout.package_body = {"stored": True, "usable": True,
                                   "package": {"mission_id": MISSION_ID, "route_hash": OTHER_HASH},
                                   "summary": {"route_waypoint_count": len(WPS)},
                                   "readiness": {"replanning_ready": False}}
        self.scout.reprove_outcome = "PACKAGE_MISMATCH"
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["mission"]["reconciliation"],
                         mission_full_refresh.PACKAGE_SYNC_REQUIRED)
        self.assertEqual(body["binding"]["reproof_outcome"], "PACKAGE_MISMATCH")
        self.assertFalse(body["readiness"]["can_start"])
        # Start remains blocked; the explicit "Retry Agent Sync" package-sync route is unaffected
        # and no package write happened here.
        self.assert_no_vehicle_mutating_write()

    # ── Pixhawk mismatch: definite, fail closed ────────────────────────────────────────────
    def test_pixhawk_mismatch_is_definite_and_fails_closed(self):
        # Task Section 9/21: approved == package != Pixhawk (Case 1) — classified PIXHAWK_MISMATCH
        # regardless of what Scout's own reprove said.
        self.set_active(approved_record())
        self.pixhawk.route_hash = OTHER_HASH
        self.scout.reprove_outcome = "PIXHAWK_MISMATCH"
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["mission"]["reconciliation"], mission_full_refresh.PIXHAWK_MISMATCH)
        self.assertFalse(body["readiness"]["can_start"])
        # No mission was uploaded to repair the mismatch.
        self.assert_no_vehicle_mutating_write()

    # ── Mission id mismatch: definite, fail closed, explicit (task Section 22) ────────────
    def test_mission_id_mismatch_is_definite_and_fails_closed(self):
        # Scout's trusted package identity names a DIFFERENT mission than the Operator's
        # approved one, even though the route hash happens to match — Full Refresh reports it
        # explicitly and Start stays blocked.
        self.set_active(approved_record())
        self.scout.reprove_outcome = "MISSION_ID_MISMATCH"
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["binding"]["reproof_outcome"], "MISSION_ID_MISMATCH")
        self.assertTrue(body["binding"]["reproof_fail_closed"])
        self.assertFalse(body["binding"]["reproof_success"])
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

    # ── Reprove-level evidence unavailable (task Section 23): incomplete, never a mismatch ──
    def test_reprove_evidence_unavailable_is_incomplete_not_a_mismatch(self):
        # Pixhawk proof unavailable/refreshing/stale at Scout's own layer — Scout answers
        # EVIDENCE_UNAVAILABLE, never PIXHAWK_MISMATCH or PACKAGE_SYNC_REQUIRED, and the refresh
        # result is incomplete rather than a false mismatch. No write.
        self.set_active(approved_record())
        self.scout.reprove_outcome = "EVIDENCE_UNAVAILABLE"
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["binding"]["reproof_outcome"], "EVIDENCE_UNAVAILABLE")
        self.assertTrue(body["binding"]["reproof_inconclusive"])
        self.assertFalse(body["ok"])
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
        self.scout.reprove_outcome = "GENERIC_REJECT"
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["binding"]["binding_state"], "UNBOUND")
        self.assertIsNone(body["binding"]["reproof_outcome"])
        self.assertEqual(body["binding"]["reproof_transport_outcome"], scout_replan.OUTCOME_REJECTED)
        self.assert_no_vehicle_mutating_write()

    def test_unsupported_reprove_leaves_binding_unbound_and_is_marked_unsupported(self):
        self.set_active(approved_record())
        self.scout.reprove_supported = False
        r = self.full_refresh()
        body = r.json()
        self.assertEqual(body["binding"]["binding_state"], "UNBOUND")
        self.assertFalse(body["binding"]["reproof_supported"])

    # ── BUSY: handled cleanly, no retry storm (task Section 24) ────────────────────────────
    def test_reprove_busy_is_handled_cleanly_and_button_can_retry(self):
        self.set_active(approved_record())
        self.scout.reprove_outcome = "BUSY"
        r = self.full_refresh()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["ok"])
        self.assertEqual(body["binding"]["reproof_outcome"], "BUSY")
        self.assert_no_vehicle_mutating_write()
        # The vehicle lock is released once this request completes — a subsequent refresh (the
        # "button eventually re-enabled" behaviour) is not itself rejected as busy.
        self.scout.reprove_outcome = "REPROVED"
        r2 = self.full_refresh()
        self.assertEqual(r2.status_code, 200)
        self.assertTrue(r2.json()["ok"])

    # ── Ordering proof: reprove happens BEFORE the status this response is built from ─────
    def test_reprove_precedes_the_status_read_it_is_meant_to_affect(self):
        self.set_active(approved_record())
        self.scout.reprove_outcome = "REPROVED"
        self.full_refresh()
        urls = [u for (_m, u, _b) in self.scout.calls]
        reprove_idx = next(i for i, u in enumerate(urls) if u.endswith("reprove_binding"))
        status_idx = next(i for i, u in enumerate(urls) if u.endswith("mission_execution/status"))
        self.assertLess(reprove_idx, status_idx)

    # ── Endpoint client proof (task Section 25) ────────────────────────────────────────────
    def test_calls_exactly_reprove_binding_with_the_approved_mission_id_as_an_expectation(self):
        self.set_active(approved_record())
        self.scout.reprove_outcome = "REPROVED"
        self.full_refresh()
        reprove_calls = [(m, u, b) for (m, u, b) in self.scout.calls
                         if u.endswith("reprove_binding") or "/reprove" in u]
        self.assertEqual(len(reprove_calls), 1, self.scout.calls)
        method, url, sent_body = reprove_calls[0]
        self.assertTrue(url.endswith("/agent/mission_execution/reprove_binding"), url)
        self.assertEqual(method, "POST")
        self.assertEqual(sent_body, {"mission_id": MISSION_ID})
        # Never the legacy speculative name, never a fallback probe.
        self.assertFalse(any(u.endswith("/agent/mission_execution/reprove")
                             and not u.endswith("reprove_binding")
                             for (_m, u, _b) in self.scout.calls))
        # Never a mission upload, a package sync POST, or any other write route.
        self.assert_no_vehicle_mutating_write()

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
