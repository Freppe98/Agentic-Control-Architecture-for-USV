"""
Focused tests for obstacle_model.py -- classification and expiry.

No pytest dependency:

    python3 test_obstacle_model.py
"""
import unittest

import config
import obstacle_model as om


class TestClassification(unittest.TestCase):
    def _event(self, **kw):
        # Fixed detected_at so expiry is deterministic; pass now= to classify.
        base = dict(event_type=om.OBSTACLE_AHEAD, distance_m=10,
                    source="EXPERIMENT_INJECTION", confidence=1.0,
                    expires_after_s=30, detected_at=1000.0)
        base.update(kw)
        return om.ObstacleEvent(**base)

    def test_long_range_proposes_detour(self):
        ev = self._event(distance_m=10)
        self.assertEqual(ev.classify(now=1001.0), om.LONG_RANGE)
        self.assertEqual(ev.recommended_action(now=1001.0), om.ACTION_PROPOSE_DETOUR)

    def test_close_obstacle_selects_loiter_only(self):
        ev = self._event(distance_m=3)
        self.assertEqual(ev.classify(now=1001.0), om.CLOSE)
        # Close -> LOITER only. Never a detour proposal, never a reverse.
        self.assertEqual(ev.recommended_action(now=1001.0), om.ACTION_LOITER)
        self.assertNotEqual(ev.recommended_action(now=1001.0),
                            om.ACTION_PROPOSE_DETOUR)

    def test_boundary_is_close(self):
        ev = self._event(distance_m=config.OBSTACLE_CLOSE_DISTANCE_M)
        self.assertEqual(ev.classify(now=1001.0), om.CLOSE)

    def test_clear_event_no_action(self):
        ev = self._event(event_type=om.OBSTACLE_CLEARED, distance_m=None)
        self.assertEqual(ev.classify(now=1001.0), om.CLEAR)
        self.assertEqual(ev.recommended_action(now=1001.0), om.ACTION_NONE)

    def test_zero_confidence_is_clear(self):
        ev = self._event(confidence=0.0)
        self.assertEqual(ev.classify(now=1001.0), om.CLEAR)


class TestExpiry(unittest.TestCase):
    def _event(self, **kw):
        base = dict(distance_m=10, expires_after_s=30, detected_at=1000.0)
        base.update(kw)
        return om.ObstacleEvent(**base)

    def test_not_expired_within_window(self):
        ev = self._event()
        self.assertFalse(ev.is_expired(now=1029.0))
        self.assertEqual(ev.classify(now=1029.0), om.LONG_RANGE)

    def test_expired_after_window(self):
        ev = self._event()
        self.assertTrue(ev.is_expired(now=1031.0))
        # An expired long-range obstacle must NOT still force a detour.
        self.assertEqual(ev.classify(now=1031.0), om.EXPIRED)
        self.assertEqual(ev.recommended_action(now=1031.0), om.ACTION_NONE)

    def test_expiry_beats_distance(self):
        # Even a close obstacle, once expired, is stale (no LOITER forced).
        ev = self._event(distance_m=3)
        self.assertEqual(ev.classify(now=1031.0), om.EXPIRED)


class TestRoundTrip(unittest.TestCase):
    def test_from_dict_to_dict(self):
        ev = om.ObstacleEvent.from_dict({
            "event_type": "OBSTACLE_AHEAD", "distance_m": 10,
            "source": "EXPERIMENT_INJECTION", "confidence": 1.0,
            "expires_after_s": 30, "detected_at": 1000.0,
        })
        d = ev.to_dict()
        self.assertEqual(d["distance_m"], 10)
        self.assertEqual(d["event_type"], "OBSTACLE_AHEAD")
        self.assertEqual(d["detected_at"], 1000.0)


if __name__ == "__main__":
    unittest.main()
