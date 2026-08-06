"""Backend tests for the Scout mission-execution integration (task Sections 1, 9, 10, 11, 12).

Run from operator-scripts/:  python -m unittest tests.test_mission_execution  (no pytest).

Scout's Local Agent owns the mission-execution lifecycle outright; the Operator backend is a
THIN, per-vehicle proxy (scout_mission_execution.py over the shared Local Agent transport in
scout_replan.py). These tests mock every Scout HTTP call by swapping `scout_replan.requests` for
a recording fake — NOTHING here touches real networking. They pin:

  • all five Scout routes reachable through the operator routes, on the SELECTED vehicle's 8090
    base — a call for usv-2 never reaches usv-3's Local Agent;
  • a write TIMEOUT is UNKNOWN (202), never a definite failure, and is RECONCILED by a status
    read rather than resent;
  • HTTP 409 is preserved as a distinct rejection (precondition / lifecycle / arbitration);
  • HTTP 200 carrying `error` (or accepted:false) is a vehicle-level FAILURE, not a success —
    Scout's exact error code and message survive;
  • an older Scout that 404s the routes is supported:false — no fabricated READY / can_start /
    verified Home / continuation / completed hold;
  • the lifecycle never touches the operator command queue or the Flask (8080) Pixhawk surface;
  • every write is recorded with the fields the task requires, and status polling does not
    produce duplicate lifecycle events.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import scout_replan  # noqa: E402
import scout_mission_execution as mx  # noqa: E402
import requests as real_requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2          # Scout — configured with a LOCAL_AGENT_API_BASE (8090) route
SAR_VID = 3            # SAR-001 — also configured
NO_LA_VID = 1          # USV-1 — configured identity, but NO LOCAL_AGENT_API_BASE route

SCOUT_BASE = main.LOCAL_AGENT_API_BASE[SCOUT_VID]
SAR_BASE = main.LOCAL_AGENT_API_BASE[SAR_VID]


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json


class FakeLA:
    """A recording fake for the shared Local Agent transport (scout_replan.requests). Match
    responses by (METHOD, path-suffix); a value that is an Exception is raised (timeout /
    unreachable). Exposes the real RequestException so the transport's except clause catches."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []                 # [(method, url, json_body)]
        self.responses = {}             # {(METHOD, suffix): FakeResp | Exception | [seq]}
        self.default = FakeResp({}, 200)

    def set(self, method, suffix, resp):
        self.responses[(method, suffix)] = resp

    def _resolve(self, method, url, json_body=None):
        self.calls.append((method, url, json_body))
        for (m, suffix), r in self.responses.items():
            if m == method and url.endswith(suffix):
                if isinstance(r, list):          # a scripted sequence, one per call
                    r = r.pop(0) if len(r) > 1 else r[0]
                if isinstance(r, Exception):
                    raise r
                return r
        if isinstance(self.default, Exception):
            raise self.default
        return self.default

    def get(self, url, **kw):
        return self._resolve("GET", url)

    def request(self, method, url, **kw):
        return self._resolve(method, url, kw.get("json"))

    def urls(self, method=None):
        return [u for (m, u, _b) in self.calls if method is None or m == method]


def status_body(**over):
    """A realistic canonical status body; override any field per test."""
    body = {
        "supported": True,
        "state": "READY", "effective_state": "READY", "active_operation_id": None,
        "mission_id": "msn-0001",
        "original_route_hash": "sha256:aaa", "active_route_hash": "sha256:aaa",
        "verified_home": {"latitude": 56.0, "longitude": 12.0},
        "home_verification_distance_m": 0.4,
        "mode": "LOITER",
        "sequence": {"current": 0, "count": 10, "before_pause": None, "at_resume": None,
                     "first_after_resume": None, "continuation_verified": None},
        "timestamps": {"start": None, "pause": None, "resume": None},
        "replanning": {"active": False, "fsm_state": "MONITORING"},
        "return_completion": {"distance_to_home_m": None, "arrival_radius_m": 7.5,
                              "persistence_s": 4, "persistence_progress_s": 0,
                              "arrival_confirmed": False, "final_loiter_verified": False},
        "authority_status": "LOCAL_AGENT",
        "can_start": True, "can_pause": False, "can_resume": False,
        "mission_execution_enabled": True, "config": {}, "last_error": None, "history": [],
    }
    body.update(over)
    return body


def op_body(operation="start", **over):
    body = {
        "accepted": True, "operation": operation, "outcome": "RUNNING",
        "operation_id": "op-123", "execution_state": "RUNNING", "mission_id": "msn-0001",
        "route_hash": "sha256:aaa", "previous_state": "READY", "current_state": "RUNNING",
        "verified_mode": "AUTO",
        "home_result": {"accepted": True, "verified": True,
                        "requested_position": {"latitude": 56.0, "longitude": 12.0},
                        "home_position": {"latitude": 56.0, "longitude": 12.0},
                        "verification_distance_m": 0.4, "error": None},
        "sequence": {"current": 0, "count": 10, "before_pause": None, "at_resume": None,
                     "first_after_resume": None, "continuation_verified": None},
        "error": None, "final": True, "idempotent": False,
    }
    body.update(over)
    return body


def green_readiness(**over):
    """A readiness verdict in which every Start precondition the OPERATOR owns is satisfied.

    Start is no longer a naked proxy: it is a transaction that first requires a VERIFIED mission
    record, a matching Pixhawk read-back hash, a stored/usable/consistent planning package and
    Scout's replanning readiness (mission_lifecycle.start_preconditions). Tests that are about
    the PROXY semantics — 409s, 200-with-error, timeouts, reconciliation — pin those semantics,
    not the preconditions, so the harness arms them and the precondition behaviour gets its own
    dedicated tests in tests/test_mission_lifecycle.py."""
    out = {
        "ok": True, "mission_ready": True, "replanning_ready": True,
        "vehicle_mission": {"mission_id": "msn-0001", "record_present": True,
                            "route_hash": "sha256:aaa", "upload_status": "VERIFIED",
                            "pixhawk_verified": True, "readback_reachable": True,
                            "readback_hash": "sha256:aaa", "readback_hash_match": True,
                            "home_valid": True, "home_source": "verified_home"},
        "planning_package": {"stored": True, "usable": True, "consistent": True,
                             "mission_id": "msn-0001", "mission_id_match": True,
                             "route_hash": "sha256:aaa", "hash_match": True,
                             "consistency": "PLANNING_PACKAGE_CONSISTENT"},
        "limitations": [],
    }
    out.update(over)
    return out


class MissionExecutionTestCase(unittest.TestCase):
    """Shared harness: a fake Local Agent + a clean per-test operator state, with the Start
    preconditions and the control-authority proxy ARMED GREEN (see green_readiness) so these
    tests keep pinning what they were written to pin — the Scout proxy's HTTP semantics."""

    def setUp(self):
        self.fake = FakeLA()
        self._real_requests = scout_replan.requests
        scout_replan.requests = self.fake
        self.client = TestClient(main.app)
        main.mission_execution_operations.clear()
        main._mx_observed.clear()
        main.event_log.clear()
        main.commands.clear() if hasattr(main, "commands") else None

        # A canonical status is the default so a Start's precondition read finds a startable
        # Scout. Individual tests override it (or clear the whole response map for the
        # older-Scout / unreachable cases).
        self.set_status(status_body())

        # Green preconditions: an active VERIFIED mission record, matching hashes, a consistent
        # package, and a Scout already holding LOCAL_AGENT authority (so acquire_authority
        # short-circuits and the test's POSTs are Scout lifecycle calls only).
        self._real_readiness = main._compute_replan_readiness
        self._real_read_authority = main.read_control_authority
        self._real_apply_authority = main.apply_control_authority
        self.authority_value = "LOCAL_AGENT"
        self.authority_writes = []
        main._compute_replan_readiness = (
            lambda vid, base, *, max_readback_age_s=main.PIXHAWK_READBACK_TTL_S: green_readiness())
        main.read_control_authority = lambda vid: {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
            "authority": self.authority_value, "source": "scout"}

        def _apply(vid, authority, source="operator"):
            self.authority_writes.append((vid, authority, source))
            self.authority_value = authority
            return {"ok": True, "vehicle_id": vid, "requested": authority,
                    "authority": authority, "available": True, "reachable": True}, 200
        main.apply_control_authority = _apply

        for vid in (SCOUT_VID, SAR_VID):
            main.active_original_by_vehicle[vid] = "msn-0001"
        main.original_missions["msn-0001"] = {
            "mission_id": "msn-0001", "upload_status": "VERIFIED", "route_hash": "sha256:aaa"}

    def tearDown(self):
        scout_replan.requests = self._real_requests
        main._compute_replan_readiness = self._real_readiness
        main.read_control_authority = self._real_read_authority
        main.apply_control_authority = self._real_apply_authority
        for vid in (SCOUT_VID, SAR_VID):
            main.active_original_by_vehicle.pop(vid, None)
        main.original_missions.pop("msn-0001", None)

    # -- helpers ---------------------------------------------------------------------
    def set_status(self, body, status=200):
        self.fake.set("GET", "/agent/mission_execution/status", FakeResp(body, status))

    def set_op(self, operation, resp):
        self.fake.set("POST", f"/agent/mission_execution/{operation}", resp)

    def set_status_sequence(self, *responses):
        """Script consecutive GET /status answers. A START now READS status twice: once for its
        own precondition check (which must find a startable Scout) and once to reconcile an
        UNKNOWN write (which is where the state under test belongs). The last entry sticks."""
        self.fake.set("GET", "/agent/mission_execution/status", list(responses))

    def only_default(self, resp):
        """Drop every matched response so `resp` answers EVERY call — the older-Scout (404) and
        unreachable-Scout cases, which must not be masked by the harness's default status."""
        self.fake.responses.clear()
        self.fake.default = resp


# ── 1. All five Scout routes, on the selected vehicle's Local Agent base ────────────────
class TestRoutes(MissionExecutionTestCase):
    def test_status_route_reaches_scout_status(self):
        self.set_status(status_body())
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"{SCOUT_BASE}/agent/mission_execution/status", self.fake.urls("GET"))
        self.assertEqual(r.json()["summary"]["state"], "READY")

    def test_all_four_write_routes_hit_their_scout_route(self):
        self.set_status(status_body())
        for op in ("start", "pause", "resume", "rearm"):
            self.set_op(op, FakeResp(op_body(op), 200))
            r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/{op}")
            self.assertEqual(r.status_code, 200, op)
            self.assertIn(f"{SCOUT_BASE}/agent/mission_execution/{op}", self.fake.urls("POST"), op)

    def test_the_stop_route_reaches_scouts_stop(self):
        """PENDING ON SCOUT: the route exists and is exercised end to end here; against a real
        Scout today it 404s and answers `unsupported`. See SCOUT_STOP_API.md."""
        self.set_status(status_body(state="RUNNING", can_start=False, can_pause=True))
        self.set_op("stop", FakeResp(op_body("stop", current_state="STOPPED",
                                             verified_mode="LOITER"), 200))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/stop")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"{SCOUT_BASE}/agent/mission_execution/stop", self.fake.urls("POST"))

    def test_start_forwards_the_active_persisted_mission_id(self):
        self.set_op("start", FakeResp(op_body("start"), 200))
        main.active_original_by_vehicle[SCOUT_VID] = "msn-active-77"
        main.original_missions["msn-active-77"] = {
            "mission_id": "msn-active-77", "upload_status": "VERIFIED",
            "route_hash": "sha256:aaa"}
        try:
            self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        finally:
            main.original_missions.pop("msn-active-77", None)
        sent = [b for (m, u, b) in self.fake.calls if u.endswith("/start")][0]
        self.assertEqual(sent, {"mission_id": "msn-active-77"})

    def test_a_ui_supplied_mission_id_that_mismatches_is_rejected_locally(self):
        """The persisted active record WINS. A browser must never be able to point a Start at a
        route the operator did not approve, so a mismatching id is refused HERE — Scout is not
        contacted at all, which is what makes `blocked` distinct from Scout's own rejection."""
        self.set_op("start", FakeResp(op_body("start"), 200))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start",
                             json={"mission_id": "msn-somebody-elses"})
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertEqual(d["outcome"], "blocked")
        self.assertEqual(d["error_code"], "MISSION_ID_MISMATCH")
        self.assertIn("msn-0001", d["error"])
        self.assertEqual([u for u in self.fake.urls("POST")], [],
                         "a locally-rejected Start must never reach Scout")

    def test_a_ui_supplied_mission_id_matching_the_active_record_is_accepted(self):
        self.set_op("start", FakeResp(op_body("start"), 200))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start",
                             json={"mission_id": "msn-0001"})
        self.assertEqual(r.status_code, 200)
        sent = [b for (m, u, b) in self.fake.calls if u.endswith("/start")][0]
        self.assertEqual(sent, {"mission_id": "msn-0001"})

    def test_start_without_an_active_mission_record_is_blocked_not_guessed(self):
        """No active record means there is nothing to forward. The Start is refused with an
        explicit reason rather than sent with an empty body and left to Scout to sort out."""
        main.active_original_by_vehicle.pop(SCOUT_VID, None)
        self.set_op("start", FakeResp(op_body("start"), 200))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error_code"], "NO_ACTIVE_MISSION")
        self.assertEqual(self.fake.urls("POST"), [])

    def test_pause_and_resume_send_an_empty_json_body(self):
        for op in ("pause", "resume", "rearm"):
            self.set_op(op, FakeResp(op_body(op), 200))
            self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/{op}")
            sent = [b for (m, u, b) in self.fake.calls if u.endswith(f"/{op}")][0]
            self.assertEqual(sent, {}, op)

    def test_unknown_vehicle_is_404_and_never_reaches_a_local_agent(self):
        r = self.client.get("/api/vehicles/usv-99/mission-execution/status")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.fake.calls, [])

    def test_vehicle_without_a_local_agent_route_is_unsupported_not_guessed(self):
        r = self.client.post(f"/api/vehicles/{NO_LA_VID}/mission-execution/start")
        self.assertEqual(r.status_code, 200)
        self.assertIs(r.json()["supported"], False)
        self.assertEqual(self.fake.calls, [])          # no other vehicle's base substituted


# ── 2. Selected-USV isolation ───────────────────────────────────────────────────────────
class TestIsolation(MissionExecutionTestCase):
    def test_a_write_for_scout_never_reaches_sar(self):
        self.set_op("start", FakeResp(op_body("start"), 200))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        urls = self.fake.urls()
        self.assertTrue(all(u.startswith(SCOUT_BASE) for u in urls), urls)
        self.assertFalse(any(SAR_BASE in u for u in urls), urls)

    def test_each_vehicle_uses_its_own_local_agent_base(self):
        self.set_op("pause", FakeResp(op_body("pause"), 200))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/pause")
        self.client.post(f"/api/vehicles/{SAR_VID}/mission-execution/pause")
        self.assertIn(f"{SCOUT_BASE}/agent/mission_execution/pause", self.fake.urls("POST"))
        self.assertIn(f"{SAR_BASE}/agent/mission_execution/pause", self.fake.urls("POST"))

    def test_operation_results_do_not_leak_across_vehicles(self):
        self.set_op("start", FakeResp(op_body("start"), 200))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        scout_ops = self.client.get(
            f"/api/mission-execution/operations?vehicle_id={SAR_VID}").json()["operations"]
        self.assertEqual(scout_ops, [])
        mine = self.client.get(
            f"/api/mission-execution/operations?vehicle_id={SCOUT_VID}").json()["operations"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["vehicle_id"], "usv-2")

    def test_one_scout_in_replanning_does_not_change_another_vehicles_status(self):
        """usv-2 REPLANNING and usv-3 RUNNING are read from two different Local Agents; the
        operator holds no shared lifecycle state that could couple them."""
        self.fake.set("GET", "/agent/mission_execution/status", FakeResp(status_body(
            state="RUNNING", effective_state="REPLANNING",
            replanning={"active": True, "fsm_state": "PLANNING"}), 200))
        a = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status").json()
        self.assertTrue(a["summary"]["replanning_active"])
        # Re-point the fake at a plain RUNNING body and read the OTHER vehicle.
        self.fake.set("GET", "/agent/mission_execution/status",
                      FakeResp(status_body(state="RUNNING", effective_state="RUNNING",
                                           can_start=False, can_pause=True), 200))
        b = self.client.get(f"/api/vehicles/{SAR_VID}/mission-execution/status").json()
        self.assertFalse(b["summary"]["replanning_active"])
        self.assertTrue(b["summary"]["can_pause"])


# ── 3. HTTP semantics: 409, 200-with-error, timeout, older Scout ────────────────────────
class TestHttpSemantics(MissionExecutionTestCase):
    def test_409_is_a_preserved_rejection_not_a_network_fault(self):
        self.set_op("start", FakeResp({"accepted": False, "error": "REPLANNING_ACTIVE",
                                       "message": "replanning owns the vehicle"}, 409))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertEqual(d["operational_outcome"], mx.OUTCOME_REJECTED)
        self.assertEqual(d["scout_error_code"], "REPLANNING_ACTIVE")
        self.assertEqual(d["http_status"], 409)
        self.assertIs(d["reachable"], True)             # Scout answered — not unreachable

    def test_http_200_carrying_an_error_is_a_vehicle_level_FAILURE(self):
        """The load-bearing rule: Scout processed the request and the vehicle operation failed."""
        self.set_op("start", FakeResp(op_body(
            "start", accepted=False, outcome="FAILED", current_state="FAILED",
            verified_mode=None, error="LOITER_NOT_VERIFIED",
            message="mode did not read back as LOITER within 5s"), 200))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["operational_outcome"], mx.OUTCOME_FAILED)
        self.assertIs(d["ok"], False)                   # never reported as a success
        self.assertEqual(d["scout_error_code"], "LOITER_NOT_VERIFIED")
        self.assertEqual(d["scout_error_message"], "mode did not read back as LOITER within 5s")
        self.assertEqual(d["current_state"], "FAILED")

    def test_http_200_with_accepted_false_and_no_code_is_still_a_failure(self):
        self.set_op("pause", FakeResp(op_body("pause", accepted=False, error=None), 200))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/pause").json()
        self.assertEqual(d["operational_outcome"], mx.OUTCOME_FAILED)

    def test_a_clean_200_is_accepted_and_preserves_scouts_body(self):
        self.set_op("start", FakeResp(op_body("start"), 200))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start").json()
        self.assertEqual(d["operational_outcome"], mx.OUTCOME_ACCEPTED)
        self.assertIs(d["ok"], True)
        self.assertEqual(d["scout"]["operation_id"], "op-123")     # Scout's body, verbatim
        self.assertEqual(d["verified_mode"], "AUTO")
        self.assertEqual(d["home_result"]["verification_distance_m"], 0.4)

    def test_older_scout_404_is_unsupported_and_fabricates_nothing(self):
        self.only_default(FakeResp(None, 404))
        s = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.assertEqual(s.status_code, 200)          # a handled "not supported", not an error
        body = s.json()
        self.assertIs(body["supported"], False)
        summary = body["summary"]
        self.assertIs(summary["supported"], False)
        self.assertIsNone(summary["state"])           # no fabricated READY
        self.assertIsNone(summary["can_start"])       # no fabricated can_start
        self.assertIsNone(summary["final_loiter_verified"])   # no fabricated completion
        w = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        self.assertIs(w.json()["supported"], False)

    def test_a_200_carrying_another_endpoints_body_is_unsupported_not_a_blank_lifecycle(self):
        """OBSERVED on the deployed Scout: its Local Agent routes with
        `path.startswith("/agent/mission")`, so GET /agent/mission_execution/status is swallowed by
        the legacy /agent/mission handler and answers HTTP 200 with a PIXHAWK MISSION READBACK.
        Accepting that would render a lifecycle card claiming support with every field blank."""
        legacy = {"available": True, "cached": False, "current_waypoint": 0, "error": None,
                  "mission_count": 15, "mission_loaded": True, "mission_valid": True,
                  "mission_hash": "5606802827", "reachable": True, "schema_version": 1,
                  "stale": False, "waypoints": [{"latitude": 56.66, "longitude": 12.88}]}
        self.set_status(legacy)
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIs(body["supported"], False)
        self.assertIn("not a mission-execution status", body["error"])
        summary = body["summary"]
        self.assertIs(summary["supported"], False)
        self.assertIs(summary["present"], False)
        self.assertIsNone(summary["state"])
        self.assertIsNone(summary["can_start"])
        self.assertIsNone(summary["final_loiter_verified"])

    def test_a_real_status_body_is_still_accepted(self):
        self.set_status(status_body())
        self.assertIs(self.client.get(
            f"/api/vehicles/{SCOUT_VID}/mission-execution/status").json()["supported"], True)
        # Presence of the KEY is what identifies it — a null active_operation_id is legitimate.
        self.assertTrue(mx.is_status_body({"state": "READY"}))
        self.assertTrue(mx.is_status_body({"can_start": False}))
        self.assertFalse(mx.is_status_body({"mission_count": 15, "waypoints": []}))
        self.assertFalse(mx.is_status_body({}))

    def test_an_unrecognized_status_body_logs_no_lifecycle_events(self):
        self.set_status({"mission_count": 15, "waypoints": []})
        for _ in range(3):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.assertEqual([e for e in main.event_log if e["type"] == "mission-execution"], [])

    def test_an_unreachable_scout_status_is_unavailable_never_fabricated(self):
        self.only_default(real_requests.RequestException("connection refused"))
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.assertEqual(r.status_code, 503)
        self.assertIs(r.json()["summary"]["reachable"], False)
        self.assertIsNone(r.json()["summary"]["state"])


# ── 4. Unknown outcomes and reconciliation (never a resend) ─────────────────────────────
class TestUnknownAndReconciliation(MissionExecutionTestCase):
    def test_a_write_timeout_is_unknown_not_a_failure(self):
        self.set_op("start", real_requests.Timeout("read timed out"))
        self.set_status_sequence(
            FakeResp(status_body(), 200),      # preflight: startable
            FakeResp(status_body(state="RUNNING", can_start=False, can_pause=True), 200))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        self.assertEqual(r.status_code, 202)         # accepted-but-unconfirmed
        self.assertEqual(r.json()["operational_outcome"], mx.OUTCOME_UNKNOWN)

    def test_a_timed_out_write_is_never_automatically_resent(self):
        self.set_op("start", real_requests.Timeout("read timed out"))
        self.set_status(status_body(state="RUNNING"))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        posts = [u for u in self.fake.urls("POST")]
        self.assertEqual(len(posts), 1, "a timed-out write must be attempted exactly once")
        # …and the reconciliation is a READ, not another write.
        self.assertIn(f"{SCOUT_BASE}/agent/mission_execution/status", self.fake.urls("GET"))

    def test_unknown_start_reconciles_to_running_by_reading_status(self):
        self.set_op("start", real_requests.Timeout("boom"))
        self.set_status_sequence(
            FakeResp(status_body(), 200),      # preflight: startable
            FakeResp(status_body(state="RUNNING", effective_state="RUNNING", mode="AUTO",
                                 mission_id="msn-0001", can_start=False, can_pause=True), 200))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start").json()
        rec = d["reconciliation"]
        self.assertEqual(rec["resolved"], "running")
        self.assertIs(rec["mission_id_match"], True)
        self.assertEqual(rec["mode"], "AUTO")

    def test_unknown_start_against_a_different_mission_is_a_mismatch_not_a_success(self):
        self.set_op("start", real_requests.Timeout("boom"))
        self.set_status_sequence(
            FakeResp(status_body(), 200),      # preflight: startable
            FakeResp(status_body(state="RUNNING", mission_id="msn-OTHER"), 200))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start").json()
        self.assertEqual(d["reconciliation"]["resolved"], "mission_mismatch")
        self.assertIs(d["reconciliation"]["mission_id_match"], False)

    def test_unknown_start_still_ready_resolves_to_ready_not_started(self):
        self.set_op("start", real_requests.Timeout("boom"))
        self.set_status(status_body(state="READY"))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start").json()
        self.assertEqual(d["reconciliation"]["resolved"], "ready")

    def test_unknown_pause_reconciles_on_paused_state_mode_and_sequence(self):
        self.set_op("pause", real_requests.Timeout("boom"))
        self.set_status(status_body(
            state="PAUSED", mode="LOITER", can_pause=False, can_resume=True,
            sequence={"current": 4, "count": 10, "before_pause": 4, "at_resume": None,
                      "first_after_resume": None, "continuation_verified": None}))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/pause").json()
        rec = d["reconciliation"]
        self.assertEqual(rec["resolved"], "paused")
        self.assertIn("LOITER", rec["detail"])
        self.assertIn("4/10", rec["detail"])
        self.assertEqual(rec["sequence"]["before_pause"], 4)

    def test_unknown_pause_that_did_not_land_resolves_to_running(self):
        self.set_op("pause", real_requests.Timeout("boom"))
        self.set_status(status_body(state="RUNNING", mode="AUTO"))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/pause").json()
        self.assertEqual(d["reconciliation"]["resolved"], "running")
        self.assertIn("did not take effect", d["reconciliation"]["detail"])

    def test_unknown_resume_reports_continuation_evidence(self):
        self.set_op("resume", real_requests.Timeout("boom"))
        self.set_status(status_body(
            state="RUNNING", mode="AUTO",
            sequence={"current": 5, "count": 10, "before_pause": 4, "at_resume": 4,
                      "first_after_resume": 5, "continuation_verified": False}))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/resume").json()
        rec = d["reconciliation"]
        self.assertEqual(rec["resolved"], "running")
        self.assertIs(rec["continuation_verified"], False)
        self.assertIn("continuation NOT verified", rec["detail"])

    def test_an_in_flight_operation_leaves_the_outcome_undecided(self):
        self.set_op("start", real_requests.Timeout("boom"))
        self.set_status_sequence(
            FakeResp(status_body(), 200),      # preflight: startable
            FakeResp(status_body(state="SETTING_HOME", active_operation_id="op-9"), 200))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start").json()
        self.assertEqual(d["reconciliation"]["resolved"], "in_progress")

    def test_a_failed_reconciling_read_stays_unknown(self):
        self.set_op("start", real_requests.Timeout("boom"))
        self.set_status_sequence(
            FakeResp(status_body(), 200),      # preflight succeeded…
            real_requests.RequestException("still down"))   # …then the link died
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start").json()
        self.assertEqual(d["reconciliation"]["resolved"], mx.OUTCOME_UNKNOWN)

    def test_a_5xx_is_unknown_because_the_write_may_have_landed(self):
        self.set_op("resume", FakeResp({"error": "internal"}, 500))
        self.set_status(status_body(state="RUNNING"))
        r = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/resume")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["operational_outcome"], mx.OUTCOME_UNKNOWN)

    def test_suspended_and_failed_reconcile_to_themselves(self):
        for state, resolved in (("SUSPENDED", "suspended"), ("FAILED", "failed")):
            self.set_op("resume", real_requests.Timeout("boom"))
            self.set_status(status_body(state=state, last_error="AUTO_NOT_VERIFIED"))
            d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/resume").json()
            self.assertEqual(d["reconciliation"]["resolved"], resolved)

    def test_completed_hold_without_a_verified_final_loiter_says_so(self):
        self.set_op("resume", real_requests.Timeout("boom"))
        rc = status_body()["return_completion"]
        rc.update(arrival_confirmed=True, final_loiter_verified=False)
        self.set_status(status_body(state="COMPLETED_HOLD", return_completion=rc))
        d = self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/resume").json()
        self.assertEqual(d["reconciliation"]["resolved"], "completed")
        self.assertIn("final LOITER is NOT verified", d["reconciliation"]["detail"])


# ── 5. No command queue, no Pixhawk route ───────────────────────────────────────────────
class TestNoQueueNoPixhawk(MissionExecutionTestCase):
    def test_a_lifecycle_operation_creates_no_queued_command(self):
        before = len(main.commands)
        self.set_op("start", FakeResp(op_body("start"), 200))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        self.assertEqual(len(main.commands), before)
        pending = self.client.get(f"/api/commands/pending/{SCOUT_VID}").json()
        items = pending if isinstance(pending, list) else pending.get("commands", [])
        self.assertEqual(items, [])

    def test_no_lifecycle_call_touches_the_flask_8080_pixhawk_surface(self):
        self.set_status(status_body())
        for op in ("start", "pause", "resume", "rearm"):
            self.set_op(op, FakeResp(op_body(op), 200))
            self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/{op}")
        self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        flask_base = main.VEHICLE_API_BASE.get(SCOUT_VID)
        for u in self.fake.urls():
            self.assertTrue(u.startswith(SCOUT_BASE), u)
            self.assertNotIn("/mission", u.replace("/agent/mission_execution", ""))
            if flask_base:
                self.assertFalse(u.startswith(flask_base) and "8090" not in u, u)

    def test_start_issues_no_separate_loiter_set_home_or_auto_call(self):
        """Scout owns the whole transaction: exactly ONE POST leaves the operator."""
        self.set_op("start", FakeResp(op_body("start"), 200))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        posts = self.fake.urls("POST")
        self.assertEqual(posts, [f"{SCOUT_BASE}/agent/mission_execution/start"])


# ── 6. Operation logging and lifecycle events ───────────────────────────────────────────
class TestOperationLogging(MissionExecutionTestCase):
    def test_a_write_is_recorded_with_every_required_field(self):
        self.set_op("start", FakeResp(op_body("start"), 200))
        main.active_original_by_vehicle[SCOUT_VID] = "msn-0001"
        try:
            self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/start")
        finally:
            main.active_original_by_vehicle.pop(SCOUT_VID, None)
        e = self.client.get("/api/mission-execution/operations").json()["operations"][-1]
        for field in ("vehicle_id", "operation", "requested_at", "outcome", "http_status",
                      "scout_error_code", "operation_id", "mission_id", "resulting_state",
                      "verified_mode", "unknown", "reconciliation", "route_hash", "sequence"):
            self.assertIn(field, e, field)
        self.assertEqual(e["vehicle_id"], "usv-2")
        self.assertEqual(e["operation"], "start")
        self.assertEqual(e["outcome"], mx.OUTCOME_ACCEPTED)
        self.assertEqual(e["http_status"], 200)
        self.assertEqual(e["operation_id"], "op-123")
        self.assertEqual(e["mission_id"], "msn-0001")
        self.assertEqual(e["resulting_state"], "RUNNING")
        self.assertIs(e["unknown"], False)

    def test_an_unknown_write_records_its_reconciliation(self):
        self.set_op("pause", real_requests.Timeout("boom"))
        self.set_status(status_body(state="PAUSED", mode="LOITER"))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/pause")
        e = self.client.get("/api/mission-execution/operations").json()["operations"][-1]
        self.assertIs(e["unknown"], True)
        self.assertEqual(e["reconciliation"]["resolved"], "paused")

    def test_a_failed_resume_continuation_raises_its_own_warning_event(self):
        seq = {"current": 0, "count": 10, "before_pause": 4, "at_resume": 4,
               "first_after_resume": 0, "continuation_verified": False}
        self.set_op("resume", FakeResp(op_body("resume", sequence=seq, verified_mode="AUTO"), 200))
        self.client.post(f"/api/vehicles/{SCOUT_VID}/mission-execution/resume")
        warnings = [e for e in main.event_log
                    if e["type"] == "mission-execution" and e["severity"] == "warning"]
        self.assertTrue(any("continuation was NOT verified" in e["message"] for e in warnings),
                        [e["message"] for e in main.event_log])

    def test_status_polling_does_not_duplicate_lifecycle_events(self):
        body = status_body(state="RUNNING", history=[
            {"timestamp": "2026-08-04T10:00:00Z", "from": "READY", "to": "RUNNING",
             "operation_id": "op-1"}])
        self.set_status(body)
        for _ in range(5):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        transitions = [e for e in main.event_log if e["type"] == "mission-execution"]
        self.assertEqual(len(transitions), 1, [e["message"] for e in transitions])

    def test_a_new_scout_transition_is_logged_once_when_it_appears(self):
        first = status_body(state="RUNNING", history=[
            {"timestamp": "t1", "from": "READY", "to": "RUNNING"}])
        self.set_status(first)
        self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        second = status_body(state="PAUSED", history=[
            {"timestamp": "t1", "from": "READY", "to": "RUNNING"},
            {"timestamp": "t2", "from": "RUNNING", "to": "PAUSED"}])
        self.set_status(second)
        for _ in range(3):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        msgs = [e["message"] for e in main.event_log if e["type"] == "mission-execution"]
        self.assertEqual(len(msgs), 2, msgs)

    def test_without_scout_history_a_state_change_is_observed_once(self):
        self.set_status(status_body(state="READY"))
        self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.set_status(status_body(state="RUNNING"))
        for _ in range(4):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        msgs = [e["message"] for e in main.event_log if e["type"] == "mission-execution"]
        self.assertEqual(len(msgs), 1, msgs)
        self.assertIn("READY -> RUNNING", msgs[0])

    def test_arrival_and_final_loiter_milestones_are_latched(self):
        rc = status_body()["return_completion"]
        rc.update(arrival_confirmed=True, final_loiter_verified=True, distance_to_home_m=2.0)
        self.set_status(status_body(state="COMPLETED_HOLD", return_completion=rc))
        for _ in range(4):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        msgs = [e["message"] for e in main.event_log if e["type"] == "mission-execution"]
        self.assertEqual(len([m for m in msgs if "arrival confirmed" in m.lower()]), 1, msgs)
        self.assertEqual(len([m for m in msgs if "Final LOITER verified" in m]), 1, msgs)

    def test_reads_are_never_recorded_as_operations(self):
        self.set_status(status_body())
        for _ in range(3):
            self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/status")
        self.assertEqual(self.client.get("/api/mission-execution/operations")
                         .json()["operations"], [])


# ── 7. The client's own derivations (pure, no HTTP) ─────────────────────────────────────
class TestClientDerivations(unittest.TestCase):
    def test_every_scout_state_is_represented(self):
        for s in ("NOT_READY", "READY", "START_REQUESTED", "START_HOLD_REQUESTED",
                  "START_HOLD_CONFIRMED", "SETTING_HOME", "VERIFYING_HOME",
                  "SYNCHRONIZING_PACKAGE", "STARTING_AUTO", "RUNNING", "PAUSE_REQUESTED",
                  "PAUSED", "RESUME_REQUESTED", "RETURNING_HOME", "HOME_ARRIVAL_PENDING",
                  "FINAL_HOLD_REQUESTED", "COMPLETED_HOLD", "SUSPENDED", "FAILED"):
            self.assertIn(s, mx.STATES, s)

    def test_effective_replanning_is_an_overlay_not_a_stored_state(self):
        self.assertNotIn(mx.EFFECTIVE_REPLANNING, mx.STATES)
        summary = mx.summarize_status({"scout": status_body(
            state="RUNNING", effective_state="REPLANNING",
            replanning={"active": True, "fsm_state": "PLANNING"})})
        self.assertEqual(summary["state"], "RUNNING")
        self.assertEqual(summary["effective_state"], "REPLANNING")
        self.assertTrue(summary["replanning_active"])

    def test_a_nested_error_object_yields_code_and_message(self):
        r = mx.interpret_operation({
            "outcome": mx.OUTCOME_ACCEPTED, "http_status": 200,
            "scout": {"accepted": False,
                      "error": {"code": "SET_HOME_FAILED", "message": "no ack from Pixhawk"}}})
        self.assertEqual(r["operational_outcome"], mx.OUTCOME_FAILED)
        self.assertEqual(r["scout_error_code"], "SET_HOME_FAILED")
        self.assertEqual(r["scout_error_message"], "no ack from Pixhawk")

    def test_summarize_never_defaults_missing_safety_fields(self):
        s = mx.summarize_status({"scout": {"state": "READY"}})
        self.assertIsNone(s["can_start"])
        self.assertIsNone(s["continuation_verified"])
        self.assertIsNone(s["final_loiter_verified"])
        self.assertIsNone(s["return_completion"])


if __name__ == "__main__":
    unittest.main()
