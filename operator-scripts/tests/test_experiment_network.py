"""Backend tests for the network-impairment experiment orchestration API (Stage 1).

Run from operator-scripts/:  python -m unittest tests.test_experiment_network  (no pytest).

The Operator backend is a THIN PROXY to Scout's experiment controller
(GET/POST/DELETE {SCOUT_API_BASE[vid]}/agent/experiment/network). These tests mock every
Scout HTTP call by swapping `main.requests` for a recording fake — NOTHING here runs tc or
touches real networking. They pin the contract the frontend depends on:

  • a valid Stage-1 request is normalized and forwarded, with a BACKEND-generated experiment_id;
  • vehicle_id maps to the correct Scout base URL (the SCOUT_API_BASE map, not a hard-coded addr);
  • Scout-confirmed active state passes through, and the backend NEVER fabricates active=true;
  • unsupported direction / bandwidth / duplication / reordering / full_disconnect are rejected
    in Stage 1 with a clear 400 (never a generic 500);
  • invalid ranges are rejected BEFORE forwarding;
  • an unreachable Scout produces a stable, non-500 unavailable response;
  • GET inactive, DELETE active, repeated DELETE is harmless;
  • the read timeout is bounded and latency-aware;
  • history records request / confirmed / rejected / stop / expiry / unreachable;
  • the endpoints are independent of control authority and comm-state gating.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2  # the only vehicle with a Scout API base configured (SCOUT_API_BASE)


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1"  # non-empty so main does r.json()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class FakeScout:
    """Records every call and returns queued/keyed responses. Set `.raise_exc` to a
    RequestException instance to simulate an unreachable Scout on the next call."""

    def __init__(self):
        self.calls = []            # [ (method, url, kwargs) ]
        self.get_resp = FakeResp({"active": False})
        self.post_resp = FakeResp({"active": True})
        self.delete_resp = FakeResp({"active": False})
        self.raise_exc = None

    def _maybe_raise(self):
        if self.raise_exc is not None:
            exc, self.raise_exc = self.raise_exc, None
            raise exc

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw)); self._maybe_raise(); return self.get_resp

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw)); self._maybe_raise(); return self.post_resp

    def delete(self, url, **kw):
        self.calls.append(("DELETE", url, kw)); self._maybe_raise(); return self.delete_resp


VALID_BODY = {
    "vehicle_id": SCOUT_VID, "latency_ms": 500, "jitter_ms": 100, "packet_loss_pct": 10,
    "bandwidth_kbit_s": None, "duplication_pct": 0, "reordering_pct": 0,
    "full_disconnect": False, "direction": "scout_to_operator", "duration_s": 60,
}

# What Scout confirms after an apply (active, with an echoed experiment_id + profile).
def scout_active(experiment_id="scout-echo-id"):
    return FakeResp({
        "active": True, "experiment_id": experiment_id,
        "started_at": "2026-07-21T10:00:00+00:00", "ends_at": "2026-07-21T10:01:00+00:00",
        "remaining_s": 60, "direction": "scout_to_operator",
        "profile": {"latency_ms": 500, "jitter_ms": 100, "packet_loss_pct": 10},
    })


class ExperimentTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        # Isolate per-test global state.
        main.experiment_history.clear()
        main.event_log.clear()
        main._experiment_tracked.clear()
        main._last_experiment_vehicle_id = SCOUT_VID
        # A real RequestException type on the fake for HTTPError etc.
        self._orig_requests = main.requests
        self.scout = FakeScout()
        # Preserve the real exception classes the endpoints catch.
        self.scout.RequestException = self._orig_requests.RequestException
        self.scout.HTTPError = self._orig_requests.HTTPError
        main.requests = self.scout

    def tearDown(self):
        main.requests = self._orig_requests

    def actions(self):
        return [h["action"] for h in main.experiment_history]


class TestApply(ExperimentTestBase):
    def test_valid_request_is_normalized_and_forwarded_with_backend_experiment_id(self):
        self.scout.post_resp = scout_active()
        r = self.client.post("/api/experiment/network", json=VALID_BODY)
        self.assertEqual(r.status_code, 200)
        # exactly one POST, to the correct Scout base URL derived from SCOUT_API_BASE
        posts = [c for c in self.scout.calls if c[0] == "POST"]
        self.assertEqual(len(posts), 1)
        method, url, kw = posts[0]
        self.assertEqual(url, f"{main.SCOUT_API_BASE[SCOUT_VID]}/agent/experiment/network")
        sent = kw["json"]
        # backend generated a UUID experiment_id; the browser sent none
        self.assertIn("experiment_id", sent)
        self.assertNotIn("experiment_id", VALID_BODY)
        self.assertEqual(len(sent["experiment_id"]), 36)  # uuid4 string
        # normalized forward shape: vehicle_id dropped, direction forced scout_to_operator
        self.assertNotIn("vehicle_id", sent)
        self.assertEqual(sent["direction"], "scout_to_operator")
        self.assertEqual(sent["latency_ms"], 500)
        self.assertEqual(sent["duration_s"], 60)

    def test_vehicle_id_maps_to_scout_base_url(self):
        self.scout.post_resp = scout_active()
        self.client.post("/api/experiment/network", json=VALID_BODY)
        url = [c for c in self.scout.calls if c[0] == "POST"][0][1]
        self.assertTrue(url.startswith(main.SCOUT_API_BASE[SCOUT_VID]))
        self.assertIn(":8080", url)  # Scout Flask default port

    def test_scout_confirmed_active_passes_through(self):
        self.scout.post_resp = scout_active("scout-echo-id")
        r = self.client.post("/api/experiment/network", json=VALID_BODY).json()
        self.assertTrue(r["active"])
        self.assertEqual(r["status"], "active")
        self.assertEqual(r["experiment_id"], "scout-echo-id")
        self.assertEqual(r["direction"], "scout_to_operator")
        self.assertEqual(r["remaining_s"], 60)
        self.assertEqual(r["profile"]["latency_ms"], 500)
        self.assertTrue(r["available"])

    def test_backend_never_fabricates_active_true(self):
        # Scout accepted the request but does NOT (yet) confirm it active.
        self.scout.post_resp = FakeResp({"active": False})
        r = self.client.post("/api/experiment/network", json=VALID_BODY).json()
        self.assertFalse(r["active"])
        self.assertEqual(r["status"], "inactive")
        # …but the backend still carries the experiment_id it generated
        self.assertTrue(r["experiment_id"])

    def test_our_experiment_id_is_carried_when_scout_echoes_none(self):
        self.scout.post_resp = FakeResp({"active": True})  # active but no id echoed
        r = self.client.post("/api/experiment/network", json=VALID_BODY).json()
        self.assertTrue(r["active"])
        self.assertTrue(r["experiment_id"])  # ours, not None

    def test_history_records_request_and_confirmation(self):
        self.scout.post_resp = scout_active()
        self.client.post("/api/experiment/network", json=VALID_BODY)
        self.assertIn("requested", self.actions())
        self.assertIn("confirmed_active", self.actions())
        req = next(h for h in main.experiment_history if h["action"] == "requested")
        self.assertEqual(req["result"], "forwarded")
        self.assertEqual(req["duration_s"], 60)
        self.assertTrue(req["experiment_id"])


class TestCapabilityRejection(ExperimentTestBase):
    def _reject(self, patch):
        body = {**VALID_BODY, **patch}
        return self.client.post("/api/experiment/network", json=body)

    def _assert_unsupported(self, r, token):
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "unsupported experiment profile")
        self.assertEqual(data["supported_stage"], 1)
        self.assertIn(token, data["unsupported"])
        # nothing was forwarded to Scout
        self.assertEqual([c for c in self.scout.calls if c[0] == "POST"], [])
        # recorded as a rejection
        self.assertIn("rejected", self.actions())

    def test_direction_both_rejected(self):
        self._assert_unsupported(self._reject({"direction": "both"}), "direction=both")

    def test_direction_operator_to_scout_rejected(self):
        self._assert_unsupported(self._reject({"direction": "operator_to_scout"}),
                                 "direction=operator_to_scout")

    def test_bandwidth_rejected(self):
        self._assert_unsupported(self._reject({"bandwidth_kbit_s": 512}), "bandwidth_kbit_s")

    def test_duplication_rejected(self):
        self._assert_unsupported(self._reject({"duplication_pct": 5}), "duplication_pct")

    def test_reordering_rejected(self):
        self._assert_unsupported(self._reject({"reordering_pct": 5}), "reordering_pct")

    def test_full_disconnect_rejected(self):
        self._assert_unsupported(self._reject({"full_disconnect": True}), "full_disconnect")

    def test_multiple_unsupported_all_listed(self):
        r = self._reject({"direction": "both", "bandwidth_kbit_s": 100, "full_disconnect": True})
        data = r.json()
        for token in ("direction=both", "bandwidth_kbit_s", "full_disconnect"):
            self.assertIn(token, data["unsupported"])


class TestRangeRejection(ExperimentTestBase):
    def _reject(self, patch):
        return self.client.post("/api/experiment/network", json={**VALID_BODY, **patch})

    def test_negative_latency_rejected_before_forwarding(self):
        r = self._reject({"latency_ms": -5})
        self.assertEqual(r.status_code, 400)
        data = r.json()
        self.assertEqual(data["error"], "invalid experiment parameters")
        self.assertIn("latency_ms", data["invalid"])
        self.assertEqual([c for c in self.scout.calls if c[0] == "POST"], [])

    def test_latency_over_max_rejected(self):
        self.assertEqual(self._reject({"latency_ms": 20000}).status_code, 400)

    def test_loss_over_100_rejected(self):
        r = self._reject({"packet_loss_pct": 150})
        self.assertIn("packet_loss_pct", r.json()["invalid"])

    def test_duration_below_min_rejected(self):
        r = self._reject({"duration_s": 0})
        self.assertIn("duration_s", r.json()["invalid"])

    def test_valid_ranges_do_forward(self):
        self.scout.post_resp = scout_active()
        r = self._reject({"latency_ms": 10000, "jitter_ms": 5000, "packet_loss_pct": 100,
                          "duration_s": 3600})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len([c for c in self.scout.calls if c[0] == "POST"]), 1)


class TestGet(ExperimentTestBase):
    def test_inactive_state(self):
        self.scout.get_resp = FakeResp({"active": False})
        r = self.client.get("/api/experiment/network").json()
        self.assertEqual(r["status"], "inactive")
        self.assertFalse(r["active"])
        self.assertEqual(r["vehicle_id"], SCOUT_VID)
        self.assertTrue(r["available"])
        self.assertIsNone(r["experiment_id"])

    def test_active_state_passthrough(self):
        self.scout.get_resp = scout_active("live-1")
        r = self.client.get("/api/experiment/network").json()
        self.assertTrue(r["active"])
        self.assertEqual(r["experiment_id"], "live-1")

    def test_unreachable_scout_stable_unavailable_non_500(self):
        self.scout.raise_exc = main.requests.RequestException("boom")
        r = self.client.get("/api/experiment/network")
        self.assertNotEqual(r.status_code, 500)
        data = r.json()
        self.assertEqual(data["status"], "unavailable")
        self.assertFalse(data["active"])
        self.assertFalse(data["available"])
        self.assertEqual(data["error"], "Scout experiment controller unreachable")

    def test_get_records_expiry_after_active_then_inactive(self):
        # poll 1: active
        self.scout.get_resp = scout_active("exp-x")
        self.client.get("/api/experiment/network")
        self.assertIn("confirmed_active", self.actions())
        # poll 2: Scout auto-expired the experiment
        self.scout.get_resp = FakeResp({"active": False})
        self.client.get("/api/experiment/network")
        self.assertIn("expired_automatically", self.actions())


class TestDelete(ExperimentTestBase):
    def test_stop_active_experiment_records_manual_stop(self):
        # make one active first (so there is a known experiment to stop)
        self.scout.post_resp = scout_active("stop-me")
        self.client.post("/api/experiment/network", json=VALID_BODY)
        self.scout.delete_resp = FakeResp({"active": False})
        r = self.client.delete("/api/experiment/network")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["active"])
        self.assertIn("stopped_manually", self.actions())
        # the DELETE went to the right Scout URL
        dels = [c for c in self.scout.calls if c[0] == "DELETE"]
        self.assertEqual(dels[0][1], f"{main.SCOUT_API_BASE[SCOUT_VID]}/agent/experiment/network")

    def test_repeated_delete_is_harmless(self):
        self.scout.delete_resp = FakeResp({"active": False})
        r1 = self.client.delete("/api/experiment/network")
        r2 = self.client.delete("/api/experiment/network")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertFalse(r1.json()["active"])
        self.assertFalse(r2.json()["active"])
        # nothing was active, so no manual-stop spam
        self.assertNotIn("stopped_manually", self.actions())

    def test_delete_never_optimistic_when_scout_still_active(self):
        self.scout.delete_resp = FakeResp({"active": True, "experiment_id": "still-on"})
        r = self.client.delete("/api/experiment/network").json()
        self.assertTrue(r["active"])  # reflected truth, not an optimistic inactive

    def test_stop_when_scout_unreachable_is_stable_non_500(self):
        self.scout.raise_exc = main.requests.RequestException("boom")
        r = self.client.delete("/api/experiment/network")
        self.assertNotEqual(r.status_code, 500)
        self.assertFalse(r.json()["active"])
        self.assertFalse(r.json()["available"])
        self.assertIn("apply_failed", self.actions())


class TestTimeoutPolicy(ExperimentTestBase):
    def test_read_timeout_is_latency_aware_and_bounded(self):
        # low latency → near the base; huge latency → capped, never unbounded
        low = main._experiment_read_timeout(500, 100)
        high = main._experiment_read_timeout(10000, 5000)
        self.assertGreaterEqual(low, main.EXPERIMENT_READ_BASE)
        self.assertGreater(high, low)                                   # scales with latency
        self.assertLessEqual(high, main.EXPERIMENT_READ_CAP)           # firm upper bound
        self.assertEqual(high, main.EXPERIMENT_READ_CAP)               # 5+2*15=35 → capped 20

    def test_apply_passes_bounded_latency_aware_timeout_to_scout(self):
        self.scout.post_resp = scout_active()
        self.client.post("/api/experiment/network", json={**VALID_BODY, "latency_ms": 500})
        kw = [c for c in self.scout.calls if c[0] == "POST"][0][2]
        connect, read = kw["timeout"]
        self.assertEqual(connect, main.EXPERIMENT_CONNECT_TIMEOUT)
        self.assertGreater(read, main.EXPERIMENT_READ_BASE)            # accounts for 500 ms
        self.assertLessEqual(read, main.EXPERIMENT_READ_CAP)

    def test_apply_ack_lost_does_not_declare_failure(self):
        # A scout_to_operator impairment can drop the apply ack — the endpoint must not call
        # the experiment failed; it records apply_failed and points the caller at GET polling.
        self.scout.raise_exc = main.requests.RequestException("timeout")
        r = self.client.post("/api/experiment/network", json=VALID_BODY)
        self.assertNotEqual(r.status_code, 500)
        data = r.json()
        self.assertFalse(data["active"])            # never optimistic
        self.assertTrue(data["experiment_id"])      # we still generated one
        self.assertIn("apply_failed", self.actions())


class TestAuthorityIndependence(ExperimentTestBase):
    def test_experiment_endpoints_ignore_comm_state_and_authority(self):
        # Even DISCONNECTED and with LOCAL_AGENT holding authority, the experiment endpoints
        # work — this is infrastructure control, not a Pixhawk command.
        main.comms_state_by_id[SCOUT_VID] = "DISCONNECTED"
        main.last_authority_by_id[SCOUT_VID] = "LOCAL_AGENT"
        self.scout.post_resp = scout_active()
        r = self.client.post("/api/experiment/network", json=VALID_BODY)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["active"])

    def test_no_confirm_flag_required(self):
        # Unlike ARM/DISARM/SET_HOME, an experiment apply never demands confirm:true.
        self.scout.post_resp = scout_active()
        body = {k: v for k, v in VALID_BODY.items()}
        r = self.client.post("/api/experiment/network", json=body)
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(r.status_code, 409)


class TestUnconfiguredVehicle(ExperimentTestBase):
    def test_apply_to_vehicle_without_scout_base_is_stable_409(self):
        r = self.client.post("/api/experiment/network", json={**VALID_BODY, "vehicle_id": 1})
        self.assertEqual(r.status_code, 409)
        data = r.json()
        self.assertFalse(data["available"])
        self.assertEqual(data["vehicle_id"], 1)

    def test_get_for_vehicle_without_scout_base_is_stable_200_unavailable(self):
        r = self.client.get("/api/experiment/network?vehicle_id=1")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["available"])
        self.assertEqual(r.json()["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
