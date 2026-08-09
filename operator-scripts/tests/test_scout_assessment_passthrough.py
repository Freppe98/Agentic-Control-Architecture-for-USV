"""Backend tests for the Scout ASSESSMENT pass-through (risk, energy feasibility, Home
verification recovery, stabilized evidence).

Run from operator-scripts/:  python -m unittest tests.test_scout_assessment_passthrough

Scout owns every judgement in this file. It computes mission and RTL feasibility, the continuous
component risk, the weighted score, the non-compensatory severity floors, the hard feasibility
override, the governing level, the advisory recommendation, whether Home is verified and how
fresh each observation is. The operator backend's ONLY job with all of it is to carry it through
without altering, summarizing away, or re-deriving any of it.

What these tests pin, and why each one is a lie if it breaks:

  • Scout's `risk` and `energy_feasibility` blocks reach the frontend BYTE FOR BYTE under
    `scout`. The frontend renders that block, so a backend that dropped or reshaped a field
    would silently remove an explanation the operator needs — or worse, remove the governing
    level and leave the weighted one.
  • `summary.risk_level` is Scout's `risk.level` and NEVER its `weighted_level` or a level
    derived from `score`. Scout's floors are non-compensatory, so the two disagree exactly when
    it matters: a weighted LOW under a HIGH communication floor governs as HIGH.
  • The backend contains no feasibility, risk, threshold or freshness arithmetic of its own.
  • `home.verification_recovery` survives to the fleet payload — and NEVER promotes an
    unverified Home. `verified` alone decides.
  • The evidence proxy passes Scout's records through and fabricates nothing: an unreachable
    Scout is reachable:false with evidence:None, never an empty-but-fine evidence set.
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

SCOUT_VID = 2
SCOUT_LA_BASE = main.LOCAL_AGENT_API_BASE[SCOUT_VID]
SCOUT_FLASK_BASE = main.VEHICLE_API_BASE[SCOUT_VID]


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise real_requests.RequestException(f"HTTP {self.status_code}")


class FakeLA:
    """Recording fake for the shared Local Agent transport (scout_replan.requests)."""
    RequestException = real_requests.RequestException

    def __init__(self, body):
        self.body = body
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(("GET", url))
        return FakeResp(self.body, 200)

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        return FakeResp(self.body, 200)


# ── Scout's live blocks, captured verbatim from the running vehicle ────────────────────────
RISK_LOW = {
    "score": 0.1273, "level": "LOW",
    "weighted_score": 0.1273, "weighted_level": "LOW",
    "component_floor_level": None, "component_floor_reason": None,
    "component_floor_source": None,
    "hard_constraint_violated": False, "hard_override_level": None,
    "confidence": "HIGH", "recommendation": "CONTINUE",
    "feasibility_status": "FEASIBLE",
    "dominant_component": "energy", "dominant_reason": "ENERGY_MARGIN_TIGHTENING",
    "evaluated_at": 1786301906.107,
    "weights": {"energy": 0.3, "communication": 0.25, "navigation": 0.25,
                "health": 0.1, "mission": 0.1},
    "components": {
        "energy": {"name": "energy", "score": 0.4243, "weight": 0.3,
                   "weighted_score": 0.1273, "reason": "ENERGY_MARGIN_TIGHTENING",
                   "evidence": {"worst_margin_percent": 17.27}},
        "communication": {"name": "communication", "score": 0.0, "weight": 0.25,
                          "weighted_score": 0.0, "reason": "COMMUNICATION_CONNECTED",
                          "evidence": {"communication_state": "CONNECTED"}},
    },
}

ENERGY_FEASIBLE = {
    "status": "FEASIBLE", "reason": "SUFFICIENT_ENERGY",
    "message": "mission margin 17.27%, RTL return margin 78.92% -- both positive.",
    "battery_percent": 89, "battery_source": "PHYSICAL",
    "physical_battery_percent": 89, "injected_battery_percent": None,
    "current_sequence": 0, "remaining_waypoint_count": 14,
    "planned_home": {"latitude": 56.6635397, "longitude": 12.8813428,
                     "source": "PLANNING_PACKAGE"},
    "rtl_home": {"latitude": 56.6635241, "longitude": 12.8815107,
                 "source": "PIXHAWK_VERIFIED_HOME"},
    "planned_completion_distance_m": 1851.8, "rtl_return_distance_m": 2.4,
    "estimated_mission_energy_percent": 61.73, "estimated_rtl_return_energy_percent": 0.08,
    "reserve_margin_percent": 10.0, "usable_range_m": 3000.0,
    "mission_margin_percent": 17.27, "rtl_return_margin_percent": 78.92,
    "mission_feasible": True, "rtl_return_feasible": True,
    "mission_geometry_source": "CURRENT_POSITION_TO_REMAINING_ROUTE",
    "rtl_return_geometry_source": "RTL_STRAIGHT_LINE_ESTIMATE",
    "evaluated_at": 1786301906.106, "position_age_s": 0.08, "max_position_age_s": 5.0,
}

EVIDENCE = {
    "battery": {"age_s": 0.087, "observed_at": 1786301934.798, "source": "SYS_STATUS",
                "state": "FRESH", "value": 89},
    "gps": {"fix_type": {"age_s": 0.087, "observed_at": 1786301934.798,
                         "source": "GPS_RAW_INT", "state": "FRESH", "value": 3},
            "satellites": {"age_s": 0.087, "observed_at": 1786301934.798,
                           "source": "GPS_RAW_INT", "state": "FRESH", "value": 23}},
    "position": {"age_s": 0.113, "observed_at": 1786301934.772,
                 "source": "GLOBAL_POSITION_INT", "state": "FRESH",
                 "value": {"lat": 56.6635204, "lng": 12.8814768}},
}


def status_body(**over):
    body = {
        "supported": True,
        "state": "READY", "effective_state": "READY", "active_operation_id": None,
        "mission_id": "msn-0001", "mode": "LOITER",
        "sequence": {"current": 0, "count": 23},
        "replanning": {"active": False, "fsm_state": "MONITORING"},
        "authority_status": "LOCAL_AGENT",
        "can_start": True, "can_pause": False, "can_resume": False,
        "mission_execution_enabled": True, "last_error": None,
        "energy_feasibility": dict(ENERGY_FEASIBLE),
        "risk": dict(RISK_LOW),
    }
    body.update(over)
    return body


class AssessmentPassthroughTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        self._real = scout_replan.requests

    def tearDown(self):
        scout_replan.requests = self._real

    def _status(self, body):
        scout_replan.requests = FakeLA(body)
        r = self.client.get(f"/api/vehicles/usv-{SCOUT_VID}/mission-execution/status")
        self.assertEqual(r.status_code, 200)
        return r.json()

    # ── the blocks reach the frontend untouched ───────────────────────────────────────────
    def test_risk_and_energy_blocks_are_byte_for_byte_verbatim_under_scout(self):
        out = self._status(status_body())
        self.assertEqual(out["scout"]["risk"], RISK_LOW)
        self.assertEqual(out["scout"]["energy_feasibility"], ENERGY_FEASIBLE)
        # Every nested field survives — the component evidence and the weights included.
        self.assertEqual(out["scout"]["risk"]["components"]["energy"]["weighted_score"], 0.1273)
        self.assertEqual(out["scout"]["risk"]["weights"]["energy"], 0.3)
        self.assertEqual(out["scout"]["energy_feasibility"]["rtl_home"]["source"],
                         "PIXHAWK_VERIFIED_HOME")

    def test_summary_carries_both_blocks_rather_than_dropping_them(self):
        out = self._status(status_body())
        self.assertEqual(out["summary"]["risk"], RISK_LOW)
        self.assertEqual(out["summary"]["energy_feasibility"], ENERGY_FEASIBLE)
        self.assertEqual(out["summary"]["energy_mission_feasible"], True)
        self.assertEqual(out["summary"]["energy_rtl_return_feasible"], True)

    # ── THE governing-level rule ──────────────────────────────────────────────────────────
    def test_summary_risk_level_is_the_governing_level_not_the_weighted_one(self):
        """Scout's own worked example: a reassuring weighted score, a severe single component,
        and a governing level that is neither of the numbers on the left. The backend must
        report HIGH — reporting the weighted LOW would tell an operator with a dead link and no
        proven autonomous continuation that their situation is fine."""
        out = self._status(status_body(risk={
            **RISK_LOW,
            "score": 0.2375, "weighted_score": 0.2375, "weighted_level": "LOW",
            "component_floor_level": "HIGH",
            "component_floor_reason": "COMMUNICATION_DISCONNECTED_NO_AUTONOMOUS_EXECUTION",
            "component_floor_source": "communication",
            "level": "HIGH", "recommendation": "HOLD_RECOMMENDED",
        }))
        self.assertEqual(out["summary"]["risk_level"], "HIGH")
        self.assertNotEqual(out["summary"]["risk_level"], "LOW")
        self.assertEqual(out["summary"]["risk_recommendation"], "HOLD_RECOMMENDED")
        # And the pre-floor inputs are still there for the explanation.
        self.assertEqual(out["summary"]["risk"]["weighted_level"], "LOW")
        self.assertEqual(out["summary"]["risk"]["component_floor_level"], "HIGH")

    def test_absent_risk_and_energy_stay_absent_and_never_default(self):
        body = status_body()
        body.pop("risk")
        body.pop("energy_feasibility")
        out = self._status(body)
        self.assertIsNone(out["summary"]["risk"])
        self.assertIsNone(out["summary"]["risk_level"])
        self.assertIsNone(out["summary"]["risk_recommendation"])
        self.assertIsNone(out["summary"]["energy_feasibility"])
        self.assertIsNone(out["summary"]["energy_mission_feasible"])
        # Never a fabricated LOW / FEASIBLE.
        self.assertNotIn("LOW", str(out["summary"]["risk_level"]))

    def test_backend_recomputes_no_level_from_the_score(self):
        """A high score with a LOW governing level (Scout's own combination is possible when a
        floor is absent and the weighting is generous) is reported as Scout sent it."""
        out = self._status(status_body(risk={**RISK_LOW, "score": 0.88,
                                             "weighted_score": 0.88, "level": "LOW"}))
        self.assertEqual(out["summary"]["risk_level"], "LOW")

    def test_no_risk_or_feasibility_arithmetic_lives_in_the_operator_backend(self):
        """Scout's energy floor boundaries (15 / 5 / 0 percent) and its risk thresholds are
        Scout's. If they ever appear as comparisons here, a second policy has started growing
        in the operator backend and the two will fork."""
        path = os.path.join(os.path.dirname(__file__), "..", "scout_mission_execution.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        for name in ("mission_margin_percent", "rtl_return_margin_percent", "weighted_score",
                     "score", "level"):
            # Every one of these is CARRIED, never compared. A comparison against any of them
            # is the operator backend starting to hold an opinion Scout already holds.
            for op in (">", "<", ">=", "<="):
                self.assertNotIn(f"{name} {op}", src, f"{name} {op}")


class HomeRecoveryPassthroughTest(unittest.TestCase):
    """Scout's Home VERIFICATION RECOVERY is provenance, never a second source of truth."""

    def _home(self, hs):
        payload = {"usv_id": SCOUT_VID, "agent": {"home_status": hs},
                   "telemetry": {"lat": 56.66, "lng": 12.88}}
        main.comms_state_by_id[SCOUT_VID] = "CONNECTED"
        return main.home_block(SCOUT_VID, payload, payload["telemetry"])

    BASE = {
        "home_position": {"latitude": 56.6635241, "longitude": 12.8815107, "source": "pixhawk"},
        "reachable": True, "ready_for_auto": True, "ready_for_rtl": True, "reason": None,
        "verification_distance_m": 0.04, "verification_method": "set_home_current_position",
        "verified": True, "verified_at": 1786289819.75,
        "verification_recovery": {
            "checked_at": 1786285996.23, "state": "RECOVERED",
            "reason": "Restored from persisted proof: fresh HOME_POSITION matched within 0.0m.",
        },
    }

    def test_verification_recovery_survives_to_the_fleet_payload(self):
        blk = self._home(dict(self.BASE))
        self.assertEqual(blk["verified"], True)
        self.assertEqual(blk["verification_recovery"]["state"], "RECOVERED")
        self.assertIn("Restored from persisted proof",
                      blk["verification_recovery"]["reason"])
        self.assertEqual(blk["verification_recovery"]["checked_at"], 1786285996.23)

    def test_a_recovered_recovery_never_promotes_an_unverified_home(self):
        blk = self._home({**self.BASE, "verified": False})
        self.assertEqual(blk["verified"], False)
        # The recovery evidence is still carried — it is provenance, and hiding it would remove
        # the very explanation of why the Home is in the state it is.
        self.assertEqual(blk["verification_recovery"]["state"], "RECOVERED")
        # …and the coordinates are still present. Their presence proved nothing.
        self.assertIsNotNone(blk["lat"])

    def test_a_scout_that_reports_no_recovery_gets_none_invented(self):
        hs = dict(self.BASE)
        hs.pop("verification_recovery")
        self.assertIsNone(self._home(hs)["verification_recovery"])

    def test_a_scout_that_reports_no_home_status_at_all_claims_nothing(self):
        main.last_known_agent.pop(SCOUT_VID, None)
        blk = main.home_block(SCOUT_VID, {"usv_id": SCOUT_VID}, {})
        self.assertEqual(blk["verified"], False)
        self.assertIsNone(blk["verification_recovery"])


class EvidenceProxyTest(unittest.TestCase):
    """The stabilized-evidence read: a pass-through, with no operator-side freshness policy."""

    def setUp(self):
        self.client = TestClient(main.app)
        self._real = main.requests

    def tearDown(self):
        main.requests = self._real

    def _proxy(self, resp, vehicle=f"usv-{SCOUT_VID}"):
        class FakeFlask:
            RequestException = real_requests.RequestException

            def __init__(self, r):
                self.r = r
                self.urls = []

            def get(self, url, **kw):
                self.urls.append(url)
                if isinstance(self.r, Exception):
                    raise self.r
                return self.r

        self.fake = FakeFlask(resp)
        main.requests = self.fake
        return self.client.get(f"/api/vehicles/{vehicle}/agent/evidence")

    def test_evidence_is_read_from_the_flask_api_and_passed_through_verbatim(self):
        r = self._proxy(FakeResp({"evidence": EVIDENCE, "freshness": {"battery_s": 0.12},
                                  "state_timestamp": 1786301934.89}))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # Port 8080 — the Flask API. NEVER 8090, which 404s this path.
        self.assertTrue(self.fake.urls[0].startswith(SCOUT_FLASK_BASE))
        self.assertTrue(self.fake.urls[0].endswith("/agent/state"))
        self.assertEqual(body["evidence"], EVIDENCE)
        self.assertEqual(body["supported"], True)
        self.assertEqual(body["reachable"], True)
        self.assertEqual(body["evidence"]["battery"]["state"], "FRESH")
        self.assertEqual(body["evidence"]["battery"]["age_s"], 0.087)

    def test_an_unreachable_scout_is_reachable_false_with_no_evidence(self):
        r = self._proxy(real_requests.RequestException("connection refused"))
        body = r.json()
        self.assertEqual(r.status_code, 200)          # never a console 5xx on a poll
        self.assertEqual(body["reachable"], False)
        self.assertIsNone(body["evidence"])
        # An unread evidence set is NOT an empty-but-fine one.
        self.assertNotEqual(body["evidence"], {})

    def test_a_scout_without_an_evidence_block_is_unsupported_not_empty(self):
        r = self._proxy(FakeResp({"telemetry": {"battery": 89}}))
        body = r.json()
        self.assertEqual(body["reachable"], True)
        self.assertEqual(body["supported"], False)
        self.assertIsNone(body["evidence"])

    def test_a_vehicle_with_no_flask_route_is_available_false(self):
        r = self._proxy(FakeResp({}), vehicle="usv-1")
        body = r.json()
        self.assertEqual(body["available"], False)
        self.assertIsNone(body["evidence"])

    def test_an_unknown_vehicle_is_a_404(self):
        r = self._proxy(FakeResp({}), vehicle="usv-99")
        self.assertEqual(r.status_code, 404)

    def test_the_proxy_computes_no_age_and_no_freshness_state(self):
        """The whole point of reading Scout's records is that the ages and states are its own.
        A backend that recomputed either would disagree with the evidence behind Scout's own
        refusals."""
        import inspect
        import re
        # Comments and the docstring are stripped first: this function's own prose says FRESH
        # precisely in order to forbid fabricating one. The guard is about the CODE.
        src = inspect.getsource(main.read_agent_evidence)
        src = re.sub(r'"""[\s\S]*?"""', "", src)
        src = re.sub(r"^\s*#.*$", "", src, flags=re.M)
        for forbidden in ("time.time", "datetime.now", "FRESH", "STALE", "AGING",
                          "age_s >", "age_s <"):
            self.assertNotIn(forbidden, src, forbidden)


if __name__ == "__main__":
    unittest.main()
