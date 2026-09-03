"""
Local-Agent-side proof-freshness tests for the cache-first GET
/agent/pixhawk_mission (Flask readback coordinator).

Proves the freshness contract that keeps a stale/refreshing/busy cached readback
from ever satisfying a SAFETY proof:

  * planning_package.readback_is_fresh -- the single freshness predicate;
  * planning_package.verify_pixhawk_consistency -- mission acceptance fails
    closed on an unfresh readback;
  * mission_execution_gateway.prove_pixhawk_mission_readback -- requests a
    refresh and waits for the coordinator's refresh_generation to advance
    (non-blocking on the Flask side; polling here on the Local Agent thread);
  * api_client.get_pixhawk_mission_proof -- the same generation-advance wait
    used by the replan acceptance path.

No pytest, no hardware, no network -- run directly:

    python3 test_readback_freshness.py
"""
import time
import unittest
from unittest.mock import patch

import planning_package as pp
import api_client
import mission_execution_gateway as meg


def _direct(**over):
    """A trusted in-process DIRECT_TRANSACTION readback."""
    rb = {"reachable": True, "partial": False, "mission_valid": True,
          "route_content_hash": "sha256:" + "a" * 64, "route_waypoint_count": 3,
          "proof_source": pp.PROOF_SOURCE_DIRECT, "proof_completed_at": time.time()}
    rb.update(over)
    return rb


def _cached(**over):
    """A COORDINATED_CACHE readback (the shape GET /agent/pixhawk_mission serves)."""
    rb = {"reachable": True, "partial": False, "mission_valid": True,
          "route_content_hash": "sha256:" + "a" * 64, "route_waypoint_count": 3,
          "proof_source": pp.PROOF_SOURCE_CACHE, "cached": True, "stale": False,
          "refreshing": False, "busy": False,
          "observed_at": time.time(), "age_s": 1.0, "refresh_generation": 7}
    rb.update(over)
    return rb


def _no_envelope():
    """A valid-looking readback with NO proof_source -- must be rejected."""
    return {"reachable": True, "partial": False, "mission_valid": True,
            "route_content_hash": "sha256:" + "a" * 64, "route_waypoint_count": 3}


class TestReadbackIsFresh(unittest.TestCase):
    def test_recent_direct_transaction_is_fresh(self):
        ok, reason = pp.readback_is_fresh(_direct())
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_old_direct_transaction_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_direct(proof_completed_at=time.time() - 999))
        self.assertFalse(ok)
        self.assertIn("proof freshness limit", reason)

    def test_direct_transaction_without_completion_time_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_direct(proof_completed_at=None))
        self.assertFalse(ok)

    def test_missing_proof_source_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_no_envelope())
        self.assertFalse(ok)
        self.assertIn("proof_source", reason)

    def test_unknown_proof_source_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_cached(proof_source="BOGUS"))
        self.assertFalse(ok)
        self.assertIn("proof_source", reason)

    def test_young_cached_is_fresh(self):
        ok, _ = pp.readback_is_fresh(_cached(age_s=1.0))
        self.assertTrue(ok)

    def test_refreshing_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_cached(refreshing=True))
        self.assertFalse(ok)
        self.assertIn("refreshing", reason)

    def test_stale_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_cached(stale=True))
        self.assertFalse(ok)
        self.assertIn("stale", reason)

    def test_busy_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_cached(busy=True))
        self.assertFalse(ok)
        self.assertIn("busy", reason)

    def test_cached_older_than_limit_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_cached(age_s=pp.PROOF_MAX_CACHE_AGE_S + 1.0))
        self.assertFalse(ok)
        self.assertIn("proof freshness limit", reason)

    def test_cached_without_observed_at_is_not_fresh(self):
        ok, reason = pp.readback_is_fresh(_cached(observed_at=None))
        self.assertFalse(ok)

    def test_non_dict_is_not_fresh(self):
        ok, _ = pp.readback_is_fresh(None)
        self.assertFalse(ok)


class TestVerifyConsistencyFreshness(unittest.TestCase):
    """Mission acceptance (verify_pixhawk_consistency) must fail closed on an
    unfresh readback, before any hash comparison."""

    def _package(self):
        return {"route_hash": "sha256:" + "a" * 64,
                "route": [{"latitude": 1.0, "longitude": 2.0}]}

    def test_stale_readback_rejected(self):
        ok, code, msg, ev = pp.verify_pixhawk_consistency(self._package(), _cached(stale=True))
        self.assertFalse(ok)
        self.assertEqual(code, "PIXHAWK_READBACK_STALE")

    def test_refreshing_readback_rejected_as_unavailable(self):
        ok, code, msg, ev = pp.verify_pixhawk_consistency(self._package(), _cached(refreshing=True))
        self.assertFalse(ok)
        self.assertEqual(code, "PIXHAWK_UNAVAILABLE")

    def test_busy_readback_rejected_as_unavailable(self):
        ok, code, msg, ev = pp.verify_pixhawk_consistency(self._package(), _cached(busy=True))
        self.assertFalse(ok)
        self.assertEqual(code, "PIXHAWK_UNAVAILABLE")

    def test_missing_proof_source_rejected_as_unverified(self):
        ok, code, msg, ev = pp.verify_pixhawk_consistency(self._package(), _no_envelope())
        self.assertFalse(ok)
        self.assertEqual(code, "PIXHAWK_READBACK_UNVERIFIED")

    def test_fresh_matching_readback_accepted(self):
        ok, code, msg, ev = pp.verify_pixhawk_consistency(
            self._package(), _cached(route_waypoint_count=1))
        self.assertTrue(ok, msg)


class _ScriptedGateway(meg.FlaskMissionExecutionGateway):
    """A gateway whose _get_pixhawk returns a scripted sequence, so the
    generation-advance polling of prove_pixhawk_mission_readback is exercised
    with no HTTP."""
    def __init__(self, sequence):
        super().__init__(base_url="http://scripted.invalid")
        self._sequence = list(sequence)
        self.calls = []

    def _get_pixhawk(self, refresh=False):
        self.calls.append(("refresh" if refresh else "read"))
        item = self._sequence.pop(0) if len(self._sequence) > 1 else self._sequence[0]
        return item


class TestGatewayProof(unittest.TestCase):
    def test_prove_waits_for_generation_advance_then_returns(self):
        # gen 7 while refreshing, then advances to 8 and clears refreshing.
        seq = [
            _cached(refresh_generation=7, refreshing=True),
            _cached(refresh_generation=7, refreshing=True),
            _cached(refresh_generation=8, refreshing=False, age_s=0.2),
        ]
        gw = _ScriptedGateway(seq)
        rb = gw.prove_pixhawk_mission_readback(max_wait_s=2.0, poll_interval_s=0.01)
        self.assertEqual(rb["refresh_generation"], 8)
        self.assertFalse(rb["refreshing"])
        self.assertEqual(gw.calls[0], "refresh")  # first call requests a refresh
        ok, _ = pp.readback_is_fresh(rb)
        self.assertTrue(ok)

    def test_prove_returns_unfresh_on_timeout(self):
        # Generation never advances -> returns the latest (still refreshing),
        # which the freshness gate rejects. Never a false-fresh.
        seq = [_cached(refresh_generation=7, refreshing=True)]
        gw = _ScriptedGateway(seq)
        rb = gw.prove_pixhawk_mission_readback(max_wait_s=0.2, poll_interval_s=0.01)
        self.assertTrue(rb["refreshing"])
        ok, _ = pp.readback_is_fresh(rb)
        self.assertFalse(ok)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._payload


class TestApiClientProof(unittest.TestCase):
    def test_proof_waits_for_generation_advance(self):
        seq = [
            _cached(refresh_generation=3, refreshing=True),
            _cached(refresh_generation=3, refreshing=True),
            _cached(refresh_generation=4, refreshing=False, age_s=0.1),
        ]
        state = {"i": 0}

        def fake_get(url, params=None, timeout=None):
            i = min(state["i"], len(seq) - 1)
            state["i"] += 1
            return _FakeResp(seq[i])

        with patch.object(api_client.requests, "get", side_effect=fake_get):
            rb = api_client.get_pixhawk_mission_proof(max_wait_s=2.0, poll_interval_s=0.01)
        self.assertEqual(rb["refresh_generation"], 4)
        self.assertFalse(rb["refreshing"])
        ok, _ = pp.readback_is_fresh(rb)
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main(verbosity=2)
