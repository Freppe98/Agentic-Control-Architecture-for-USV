"""Backend tests for the Set-Home deployment endpoint + the fleet `home` block.

Run from operator-scripts/:  python -m unittest tests.test_set_home  (no pytest needed).

Scout is a not-yet-shipped Flask endpoint, so these tests monkeypatch main.requests to
simulate its POST /agent/set_home responses (accepted / verified / rejected / timeouts /
out-of-tolerance) and assert the operator backend never claims success before Scout's
read-back verification, and surfaces a structured failure code otherwise.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2  # the only vehicle with a Scout API base configured (SCOUT_API_BASE)


class FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"{self.status_code}")


class FakeRequests:
    """Stand-in for main.requests: records the last POST and returns a scripted body,
    or raises RequestException to simulate an unreachable Scout."""
    RequestException = Exception
    HTTPError = Exception

    def __init__(self, *, post_body=None, raise_exc=None):
        self.post_body = post_body
        self.raise_exc = raise_exc
        self.last_post = None

    def post(self, url, json=None, timeout=None):
        self.last_post = {"url": url, "json": json}
        if self.raise_exc:
            raise self.raise_exc
        return FakeResp(self.post_body)

    def get(self, *a, **k):  # unused here, kept for completeness
        raise self.RequestException("no get")


class SetHomeTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        # Isolate per-test global state.
        main.home_verification_by_id.clear()
        main.commands.clear()
        main.commands_by_id.clear()
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"  # fresh link by default
        self._orig_requests = main.requests

    def tearDown(self):
        main.requests = self._orig_requests

    def set_scout(self, **kw):
        fake = FakeRequests(**kw)
        # Make the exception types line up so `except requests.RequestException` catches.
        fake.RequestException = self._orig_requests.RequestException
        main.requests = fake
        return fake

    def call(self, body, vid=SCOUT_VID):
        return self.client.post(f"/api/vehicles/{vid}/commands/set-home", json=body)

    HERE = {"lat": 56.70000, "lng": 13.00000, "confirm": True}


class TestSetHomeSuccess(SetHomeTestBase):
    def test_verified_within_tolerance_marks_verified(self):
        self.set_scout(post_body={
            "accepted": True, "verified": True,
            "home": {"lat": 56.700001, "lng": 13.000001}, "distance_m": 1.4,
        })
        r = self.call(self.HERE)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["verified"])
        self.assertEqual(data["phase"], "verified")
        self.assertAlmostEqual(data["distance_m"], 1.4, places=3)
        # Recorded server-side → fleet `home` block reflects it.
        self.assertTrue(main.home_verification_by_id[SCOUT_VID]["verified"])
        self.assertEqual(data["command"]["type"], "SET_HOME")
        self.assertEqual(data["command"]["status"], "EXECUTED")

    def test_home_block_updates_after_success(self):
        self.set_scout(post_body={"accepted": True, "verified": True,
                                  "home": {"lat": 56.7, "lng": 13.0}, "distance_m": 0.5})
        self.call(self.HERE)
        block = main.home_block(SCOUT_VID, {"home_position": {"lat": 56.7, "lng": 13.0}}, {})
        self.assertTrue(block["available"])
        self.assertTrue(block["verified"])
        self.assertIsNotNone(block["verified_at"])


class TestSetHomeGuards(SetHomeTestBase):
    def test_requires_confirmation(self):
        self.set_scout(post_body={"accepted": True, "verified": True,
                                  "home": {"lat": 56.7, "lng": 13.0}, "distance_m": 0.2})
        r = self.call({"lat": 56.7, "lng": 13.0})  # no confirm
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.json()["needs_confirmation"])
        self.assertNotIn(SCOUT_VID, main.home_verification_by_id)

    def test_missing_gps_fails_gps_unavailable(self):
        self.set_scout(post_body={"accepted": True, "verified": True})
        r = self.call({"confirm": True})  # no lat/lng
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["code"], "gps_unavailable")
        self.assertFalse(r.json()["verified"])

    def test_stale_link_fails_position_stale(self):
        main.comms_state_by_id[SCOUT_VID] = "DISCONNECTED"
        self.set_scout(post_body={"accepted": True, "verified": True})
        r = self.call(self.HERE)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "position_stale")

    def test_unknown_vehicle_404(self):
        r = self.call(self.HERE, vid=999)
        self.assertEqual(r.status_code, 404)

    def test_no_scout_api_configured_is_unreachable(self):
        main.comms_state_by_id[1] = "CONNECTED"
        # vehicle 1 has no SCOUT_API_BASE entry.
        r = self.call(self.HERE, vid=1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["code"], "scout_unavailable")


class TestSetHomeFailures(SetHomeTestBase):
    def test_scout_unreachable(self):
        self.set_scout(raise_exc=self._orig_requests.RequestException("conn refused"))
        r = self.call(self.HERE)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["code"], "scout_unavailable")
        self.assertNotIn(SCOUT_VID, main.home_verification_by_id)

    def test_command_rejected(self):
        self.set_scout(post_body={"accepted": False, "verified": False, "reason": "no fix"})
        r = self.call(self.HERE)
        self.assertEqual(r.json()["code"], "command_rejected")
        self.assertFalse(r.json()["verified"])
        self.assertEqual(main.commands[-1]["status"], "REJECTED")

    def test_ack_timeout(self):
        self.set_scout(post_body={"error_code": "ACK_TIMEOUT"})
        self.assertEqual(self.call(self.HERE).json()["code"], "ack_timeout")

    def test_readback_timeout_when_accepted_but_unverified(self):
        self.set_scout(post_body={"accepted": True, "verified": False})
        self.assertEqual(self.call(self.HERE).json()["code"], "readback_timeout")

    def test_verification_out_of_tolerance(self):
        # Scout says verified, but read back 40 m away → backend refuses to trust it.
        self.set_scout(post_body={
            "accepted": True, "verified": True,
            "home": {"lat": 56.70036, "lng": 13.0}, "distance_m": 40.0,
        })
        r = self.call(self.HERE)
        self.assertEqual(r.json()["code"], "verification_out_of_tolerance")
        self.assertFalse(r.json()["verified"])
        self.assertNotIn(SCOUT_VID, main.home_verification_by_id)

    def test_out_of_tolerance_computed_when_scout_omits_distance(self):
        # No distance_m from Scout → backend computes it from the read-back home.
        self.set_scout(post_body={"accepted": True, "verified": True,
                                  "home": {"lat": 56.71, "lng": 13.0}})  # ~1.1 km
        r = self.call(self.HERE)
        self.assertEqual(r.json()["code"], "verification_out_of_tolerance")


class TestHomeBlock(SetHomeTestBase):
    def test_absent_home_is_unavailable_not_zero(self):
        block = main.home_block(SCOUT_VID, {}, {})
        self.assertFalse(block["available"])
        self.assertIsNone(block["lat"])
        self.assertFalse(block["verified"])

    def test_live_home_extracted_from_payload(self):
        block = main.home_block(SCOUT_VID, {"home_position": {"lat": 56.7, "lng": 13.0}}, {})
        self.assertTrue(block["available"])
        self.assertAlmostEqual(block["lat"], 56.7)
        self.assertEqual(block["source"], "pixhawk")


if __name__ == "__main__":
    unittest.main()
