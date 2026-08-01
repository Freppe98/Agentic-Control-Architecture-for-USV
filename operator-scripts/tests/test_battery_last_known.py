"""Backend last-known battery semantics — the fix for the ~2 s telemetry flicker.

A MAVLink battery_remaining of -1 means "unknown/unavailable this packet"; it must be
treated like an absent field (fall back to the last real value) and must never be stored
into last_known_telemetry, so a single -1 packet cannot flip a valid 97% to "—" and back.
"""
import unittest

import main
from fastapi.testclient import TestClient

SCOUT_VID = 2


def _pkt(tel, ts):
    return {"message_type": "status", "source": "usv-2", "timestamp": ts,
            "payload": {"usv_id": SCOUT_VID, "comm_state": "CONNECTED", "telemetry": tel}}


class TestBatteryLastKnown(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.current_vehicle_state.clear()
        main.last_known_telemetry.clear()
        main.latest_msg_ts_by_id.clear()
        main.last_seen_by_id.clear()

    def _battery(self):
        row = [v for v in self.client.get("/api/fleet/status").json() if v["id"] == SCOUT_VID][0]
        return row["battery"]

    def _post(self, tel, ts):
        self.client.post("/agent/status", json=_pkt(tel, ts))

    def test_valid_then_missing_battery_keeps_last_known(self):
        self._post({"battery": 97, "lat": 56.7, "lng": 13.0}, 1000)
        self.assertEqual(self._battery(), 97)
        self._post({"lat": 56.7, "lng": 13.0}, 1001)          # battery omitted
        self.assertEqual(self._battery(), 97)

    def test_minus_one_sentinel_does_not_clobber_last_known(self):
        self._post({"battery": 97, "lat": 56.7, "lng": 13.0}, 1000)
        self._post({"battery": -1, "lat": 56.7, "lng": 13.0}, 1001)   # transient "unknown"
        self.assertEqual(self._battery(), 97, "a -1 packet must not flip 97% to None")
        self._post({"battery": -1, "lat": 56.7, "lng": 13.0}, 1002)   # sustained does not poison
        self.assertEqual(self._battery(), 97)

    def test_minus_one_not_stored_in_last_known(self):
        self._post({"battery": 97, "lat": 56.7, "lng": 13.0}, 1000)
        self._post({"battery": -1, "lat": 56.7, "lng": 13.0}, 1001)
        self.assertEqual(main.last_known_telemetry[SCOUT_VID]["battery"], 97,
                         "last_known battery must stay the real value, never the -1 sentinel")

    def test_newer_valid_value_replaces(self):
        self._post({"battery": 97, "lat": 56.7, "lng": 13.0}, 1000)
        self._post({"battery": 88, "lat": 56.7, "lng": 13.0}, 1001)
        self.assertEqual(self._battery(), 88)

    def test_real_zero_is_kept(self):
        self._post({"battery": 0, "lat": 56.7, "lng": 13.0}, 1000)
        self.assertEqual(self._battery(), 0, "0% is a real reading, not absence")

    def test_first_ever_unknown_battery_is_none(self):
        self._post({"battery": -1, "lat": 56.7, "lng": 13.0}, 1000)
        self.assertIsNone(self._battery(), "no prior value → honest '—'")


if __name__ == "__main__":
    unittest.main()
