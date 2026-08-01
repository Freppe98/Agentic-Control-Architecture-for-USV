"""Outbound vehicle-local API routing — one generic path, keyed by canonical vehicle id.

Run from operator-scripts/:  python -m unittest tests.test_vehicle_api_routing

Context. `VEHICLE_API_BASE` is the map that says WHERE this station reaches a vehicle's own
Flask API (control authority, Pixhawk mission read-back, experiment control). It is
deliberately separate from `vehicles.json` / REGISTRY, which only says WHO exists and how to
resolve their pushed telemetry. Until SAR-001's address was verified, vehicle 3 had no row
here and every vehicle-local endpoint answered an honest available:false for it. Now that
`usv-3 -> http://10.0.3.10:8080` is verified from both ends, real outbound traffic can leave
this station for a SECOND vehicle — so the property that matters most is no longer "does it
work" but "can a request for one USV ever reach the other".

These tests pin exactly that, with every outbound HTTP call mocked (no live USV required):

  • vehicle 2 resolves ONLY to Scout's base; vehicle 3 ONLY to SAR's;
  • every canonical spelling of a vehicle (3, "3", "usv-3", "USV-3", "SAR-001") resolves to
    the SAME base, and the display name is never a routing key;
  • an unknown vehicle resolves to NO base — never a fallback onto Scout;
  • authority reads/writes and mission read-backs proxy to the requested vehicle only;
  • the command queue (a push/claim path, NOT this proxy map) keeps vehicle_id 2 and 3
    commands strictly separate in both creation and polling;
  • a failing SAR request cannot poison Scout's availability or its cached mission.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT = 2                       # canonical id, display name "Scout"
SAR = 3                         # canonical id, display name "SAR-001"
SCOUT_BASE = "http://10.0.2.10:8080"
SAR_BASE = "http://10.0.3.10:8080"

# Every accepted spelling of SAR's identity. The display name/callsign is a REGISTRY alias,
# so it resolves — but it resolves to the canonical id, which is what does the routing.
SAR_SPELLINGS = [3, "3", "usv-3", "USV-3", "usv_3", "SAR-001", "sar-001", "SAR001", "SAR"]


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1"

    def raise_for_status(self):
        if self.status_code >= 400:
            raise main.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._json


class FakeHttp:
    """Records every outbound call and answers per-URL, so a test can assert not just that
    the right vehicle was reached but that the other one was never touched. `fail_hosts`
    makes one vehicle unreachable while the other keeps working."""

    def __init__(self):
        self.calls = []                  # [ (method, url) ]
        self.responses = {}              # url-substring -> FakeResp
        self.fail_hosts = set()          # substrings whose requests raise

    def _answer(self, method, url):
        self.calls.append((method, url))
        for host in self.fail_hosts:
            if host in url:
                raise self.RequestException(f"simulated unreachable: {url}")
        for frag, resp in self.responses.items():
            if frag in url:
                return resp
        return FakeResp({})

    def get(self, url, **kw):
        return self._answer("GET", url)

    def post(self, url, **kw):
        return self._answer("POST", url)

    def delete(self, url, **kw):
        return self._answer("DELETE", url)

    # --- assertions helpers -------------------------------------------------------
    def urls(self, method=None):
        return [u for m, u in self.calls if method is None or m == method]

    def hosts_touched(self):
        return {u.split("/agent/")[0] for u in self.urls()}


class RoutingTestBase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(main.app)
        main.current_vehicle_state.clear()
        main.last_known_telemetry.clear()
        main.last_known_agent.clear()
        main.latest_msg_ts_by_id.clear()
        main.comms_state_by_id.clear()
        main.commands.clear()
        main.commands_by_id.clear()
        main.event_log.clear()
        main._experiment_tracked.clear()
        main._last_experiment_vehicle_id = SCOUT

        self._orig_requests = main.requests
        self.http = FakeHttp()
        self.http.RequestException = self._orig_requests.RequestException
        self.http.HTTPError = self._orig_requests.HTTPError
        main.requests = self.http

    def tearDown(self):
        main.requests = self._orig_requests


# ---------------------------------------------------------------------------------
# 1. Pure resolution: the map itself
# ---------------------------------------------------------------------------------
class RouteResolutionTests(RoutingTestBase):
    def test_scout_resolves_only_to_scout_base(self):
        for spelling in (2, "2", "usv-2", "USV-2", "Scout"):
            self.assertEqual(main.vehicle_api_base(spelling), SCOUT_BASE, spelling)

    def test_sar_resolves_only_to_sar_base(self):
        for spelling in SAR_SPELLINGS:
            self.assertEqual(main.vehicle_api_base(spelling), SAR_BASE, spelling)

    def test_every_sar_spelling_resolves_to_one_identical_base(self):
        bases = {main.vehicle_api_base(s) for s in SAR_SPELLINGS}
        self.assertEqual(bases, {SAR_BASE})

    def test_the_two_vehicles_never_share_a_base(self):
        self.assertNotEqual(main.vehicle_api_base(SCOUT), main.vehicle_api_base(SAR))

    def test_unknown_vehicle_resolves_to_no_base(self):
        for unknown in ("usv-99", 99, "nope", "", None, "9f2c-uuid-like", "usv-", 0):
            self.assertIsNone(main.vehicle_api_base(unknown), unknown)

    def test_unknown_vehicle_never_falls_back_to_scout(self):
        """The single most important negative: a missing route is None, never Scout's URL.
        A fallback here would send another vehicle's authority traffic to Scout."""
        for unknown in ("usv-99", 99, "nope", None, "SAR-002"):
            self.assertNotEqual(main.vehicle_api_base(unknown), SCOUT_BASE, unknown)

    def test_configured_route_ports_are_the_flask_api_not_agent_diagnostics(self):
        """8080 is the vehicle Flask API. 8090 is the Local Agent DIAGNOSTICS server, which
        (probed live on SAR) answers /agent/pixhawk_mission but 404s /agent/control_authority
        and /agent/experiment/network — so a row pointed there looks healthy on the mission
        pages while control silently fails. That half-working mode is why this is pinned."""
        for cid, base in main.VEHICLE_API_BASE.items():
            self.assertTrue(base.endswith(":8080"), f"{cid} -> {base}")
            self.assertNotIn(":8090", base)

    def test_registry_membership_does_not_imply_a_route(self):
        """usv-1 is a configured, monitored vehicle with no verified address. That is a
        supported state, and proves appearing in vehicles.json never grants command reach."""
        self.assertIn(1, main.REGISTRY.configured_ids())
        self.assertIsNone(main.vehicle_api_base(1))


# ---------------------------------------------------------------------------------
# 2. Control authority — read and write proxy to the requested vehicle only
# ---------------------------------------------------------------------------------
class ControlAuthorityRoutingTests(RoutingTestBase):
    def test_sar_authority_read_reaches_sar_only(self):
        self.http.responses = {SAR_BASE: FakeResp({"authority": "OPERATOR"})}
        r = self.client.get(f"/api/control_authority/{SAR}")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["vehicle_id"], SAR)
        self.assertTrue(body["available"])
        self.assertTrue(body["reachable"])
        self.assertEqual(body["authority"], "OPERATOR")
        self.assertEqual(self.http.urls("GET"), [f"{SAR_BASE}/agent/control_authority"])
        self.assertNotIn(SCOUT_BASE, self.http.hosts_touched())

    def test_scout_authority_read_reaches_scout_only(self):
        self.http.responses = {SCOUT_BASE: FakeResp({"authority": "LOCAL_AGENT"})}
        body = self.client.get(f"/api/control_authority/{SCOUT}").json()
        self.assertEqual(body["vehicle_id"], SCOUT)
        self.assertEqual(body["authority"], "LOCAL_AGENT")
        self.assertEqual(self.http.urls("GET"), [f"{SCOUT_BASE}/agent/control_authority"])
        self.assertNotIn(SAR_BASE, self.http.hosts_touched())

    def test_every_sar_spelling_reads_the_same_sar_endpoint(self):
        self.http.responses = {SAR_BASE: FakeResp({"authority": "OPERATOR"})}
        for spelling in ("3", "usv-3", "USV-3", "SAR-001"):
            self.http.calls.clear()
            body = self.client.get(f"/api/control_authority/{spelling}").json()
            self.assertEqual(body["vehicle_id"], SAR, spelling)
            self.assertEqual(self.http.urls("GET"),
                             [f"{SAR_BASE}/agent/control_authority"], spelling)

    def test_authority_write_posts_to_the_requested_vehicle_only(self):
        """A SAR 'Take Control' must never hand the wheel on Scout."""
        self.http.responses = {SAR_BASE: FakeResp({"authority": "OPERATOR"})}
        r = self.client.post(f"/api/control_authority/{SAR}", json={"authority": "OPERATOR"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["vehicle_id"], SAR)
        self.assertEqual(self.http.urls("POST"), [f"{SAR_BASE}/agent/control_authority"])
        self.assertNotIn(SCOUT_BASE, self.http.hosts_touched())

    def test_authority_write_for_unrouted_vehicle_is_refused_not_redirected(self):
        r = self.client.post("/api/control_authority/1", json={"authority": "OPERATOR"})
        self.assertEqual(r.status_code, 409)
        self.assertFalse(r.json()["available"])
        self.assertEqual(self.http.calls, [])      # nothing left the station

    def test_unknown_vehicle_authority_read_is_404_and_sends_nothing(self):
        r = self.client.get("/api/control_authority/usv-99")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.http.calls, [])

    def test_unrouted_but_known_vehicle_reads_available_false(self):
        r = self.client.get("/api/control_authority/1")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertFalse(body["available"])
        self.assertIsNone(body["authority"])
        self.assertEqual(self.http.calls, [])

    def test_sar_authority_failure_does_not_affect_scout(self):
        """An unreachable SAR is SAR's problem only — Scout must still read normally, and
        SAR's own answer stays honest (reachable:false), never Scout's cached value."""
        self.http.fail_hosts = {SAR_BASE}
        self.http.responses = {SCOUT_BASE: FakeResp({"authority": "OPERATOR"})}

        sar = self.client.get(f"/api/control_authority/{SAR}").json()
        self.assertTrue(sar["available"])
        self.assertFalse(sar["reachable"])
        self.assertIsNone(sar["authority"])

        scout = self.client.get(f"/api/control_authority/{SCOUT}").json()
        self.assertTrue(scout["reachable"])
        self.assertEqual(scout["authority"], "OPERATOR")


# ---------------------------------------------------------------------------------
# 3. Pixhawk mission read-back — per-vehicle, never cross-contaminated
# ---------------------------------------------------------------------------------
def mission_resp(count, *, first_lat):
    """A vehicle-shaped /agent/pixhawk_mission body with `count` positioned waypoints."""
    return FakeResp({
        "reachable": True, "mission_loaded": True, "mission_valid": True, "partial": False,
        "contract_version": "mission-contract-v1",
        "pixhawk_item_count": count + 1, "route_waypoint_count": count,
        "count": count, "current_seq": 0,
        "waypoints": [
            {"sequence": i, "command": 16, "latitude": first_lat + i * 0.001,
             "longitude": 12.88, "altitude": 8.0}
            for i in range(count)
        ],
    })


class PixhawkMissionRoutingTests(RoutingTestBase):
    def setUp(self):
        super().setUp()
        # Two clearly distinguishable missions: SAR's 20 route waypoints (the live vehicle's
        # real shape) vs a 5-waypoint Scout mission at a different latitude.
        self.http.responses = {
            SAR_BASE: mission_resp(20, first_lat=56.6635),
            SCOUT_BASE: mission_resp(5, first_lat=57.1000),
        }

    def test_sar_mission_fetch_reaches_sar_only(self):
        body = self.client.get(f"/api/vehicles/{SAR}/pixhawk-mission").json()
        self.assertEqual(body["vehicle_id"], SAR)
        self.assertTrue(body["available"])
        self.assertTrue(body["reachable"])
        self.assertEqual(body["count"], 20)
        self.assertEqual(body["route_waypoint_count"], 20)
        self.assertEqual(self.http.urls("GET"), [f"{SAR_BASE}/agent/pixhawk_mission"])
        self.assertNotIn(SCOUT_BASE, self.http.hosts_touched())

    def test_scout_mission_fetch_reaches_scout_only(self):
        body = self.client.get(f"/api/vehicles/{SCOUT}/pixhawk-mission").json()
        self.assertEqual(body["vehicle_id"], SCOUT)
        self.assertEqual(body["count"], 5)
        self.assertEqual(self.http.urls("GET"), [f"{SCOUT_BASE}/agent/pixhawk_mission"])
        self.assertNotIn(SAR_BASE, self.http.hosts_touched())

    def test_missions_stay_independent_across_repeated_switching(self):
        """Switching SAR -> Scout -> SAR must return each vehicle's own mission every time;
        neither read-back may leak the other's waypoints or counts."""
        for _ in range(3):
            sar = self.client.get(f"/api/vehicles/{SAR}/pixhawk-mission").json()
            scout = self.client.get(f"/api/vehicles/{SCOUT}/pixhawk-mission").json()
            self.assertEqual(sar["count"], 20)
            self.assertEqual(scout["count"], 5)
            self.assertAlmostEqual(sar["waypoints"][0]["lat"], 56.6635, places=4)
            self.assertAlmostEqual(scout["waypoints"][0]["lat"], 57.1000, places=4)

    def test_every_sar_spelling_returns_the_same_sar_mission(self):
        for spelling in ("3", "usv-3", "USV-3", "SAR-001"):
            body = self.client.get(f"/api/vehicles/{spelling}/pixhawk-mission").json()
            self.assertEqual(body["vehicle_id"], SAR, spelling)
            self.assertEqual(body["count"], 20, spelling)

    def test_unknown_vehicle_mission_is_404_and_sends_nothing(self):
        r = self.client.get("/api/vehicles/usv-99/pixhawk-mission")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.http.calls, [])

    def test_unrouted_vehicle_mission_is_available_false_not_scouts_mission(self):
        body = self.client.get("/api/vehicles/1/pixhawk-mission").json()
        self.assertFalse(body["available"])
        self.assertEqual(body["waypoints"], [])
        self.assertEqual(body["count"], 0)
        self.assertEqual(self.http.calls, [])

    def test_failed_sar_read_cannot_poison_scouts_mission(self):
        """A SAR failure must not make Scout look unavailable, and must not hand SAR's
        response slot to Scout's data. The operator backend caches no mission of its own."""
        self.http.fail_hosts = {SAR_BASE}

        sar = self.client.get(f"/api/vehicles/{SAR}/pixhawk-mission").json()
        self.assertTrue(sar["available"])         # the route exists...
        self.assertFalse(sar["reachable"])        # ...the vehicle just did not answer
        self.assertEqual(sar["waypoints"], [])
        self.assertEqual(sar["count"], 0)

        scout = self.client.get(f"/api/vehicles/{SCOUT}/pixhawk-mission").json()
        self.assertTrue(scout["reachable"])
        self.assertEqual(scout["count"], 5)

        # And SAR recovers on its own once reachable again, still with SAR's mission.
        self.http.fail_hosts = set()
        again = self.client.get(f"/api/vehicles/{SAR}/pixhawk-mission").json()
        self.assertEqual(again["count"], 20)


# ---------------------------------------------------------------------------------
# 4. Command queue — a PUSH/claim path, not this proxy map, but the same isolation rule
# ---------------------------------------------------------------------------------
def status_packet(vid, ts):
    return {"message_type": "status", "schema_version": "1.0", "source": f"usv-{vid}",
            "target": "operator", "timestamp": ts,
            "payload": {"usv_id": vid, "comm_state": "CONNECTED",
                        "telemetry": {"battery": 90}}}


class CommandRoutingIsolationTests(RoutingTestBase):
    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc).timestamp()
        # Both vehicles CONNECTED, so command creation is not gated on confirmation.
        for vid in (SCOUT, SAR):
            self.client.post("/agent/status", json=status_packet(vid, now))

    def _create(self, vehicle, ctype="SET_MODE_LOITER"):
        return self.client.post("/api/commands", json={"vehicle_id": vehicle, "type": ctype})

    def test_sar_command_creation_carries_vehicle_id_3(self):
        for spelling in ("3", "usv-3", "USV-3", "SAR-001"):
            body = self._create(spelling).json()
            self.assertTrue(body["ok"], spelling)
            self.assertEqual(body["command"]["vehicle_id"], SAR, spelling)

    def test_scout_command_creation_carries_vehicle_id_2(self):
        for spelling in ("2", "usv-2", "USV-2", "Scout"):
            body = self._create(spelling).json()
            self.assertEqual(body["command"]["vehicle_id"], SCOUT, spelling)

    def test_command_creation_is_a_queue_operation_not_an_api_proxy(self):
        """Creating a command must not open any outbound vehicle-local HTTP connection —
        delivery is by the vehicle POLLING us, which is why a vehicle with no
        VEHICLE_API_BASE row can still be commanded."""
        self._create(SAR)
        self.assertEqual(self.http.calls, [])

    def test_polling_sar_cannot_claim_scout_commands(self):
        scout_cmd = self._create(SCOUT).json()["command"]["id"]
        sar_cmd = self._create(SAR).json()["command"]["id"]

        delivered = self.client.get("/agent/commands?usv_id=usv-3").json()
        ids = [c["command_id"] for c in delivered["commands"]]
        self.assertEqual(delivered["vehicle_id"], SAR)
        self.assertIn(sar_cmd, ids)
        self.assertNotIn(scout_cmd, ids)

        # Scout's command is untouched — still QUEUED, never claimed by SAR's poll.
        self.assertEqual(main.commands_by_id[scout_cmd]["status"], "QUEUED")

    def test_polling_scout_cannot_claim_sar_commands(self):
        scout_cmd = self._create(SCOUT).json()["command"]["id"]
        sar_cmd = self._create(SAR).json()["command"]["id"]

        delivered = self.client.get("/agent/commands?usv_id=usv-2").json()
        ids = [c["command_id"] for c in delivered["commands"]]
        self.assertEqual(delivered["vehicle_id"], SCOUT)
        self.assertIn(scout_cmd, ids)
        self.assertNotIn(sar_cmd, ids)
        self.assertEqual(main.commands_by_id[sar_cmd]["status"], "QUEUED")

    def test_pending_view_is_per_vehicle(self):
        scout_cmd = self._create(SCOUT).json()["command"]["id"]
        sar_cmd = self._create(SAR).json()["command"]["id"]
        sar_pending = self.client.get(f"/api/commands/pending/{SAR}").json()
        self.assertEqual([c["id"] for c in sar_pending["pending"]], [sar_cmd])
        scout_pending = self.client.get(f"/api/commands/pending/{SCOUT}").json()
        self.assertEqual([c["id"] for c in scout_pending["pending"]], [scout_cmd])

    def test_unknown_vehicle_command_is_404_and_never_queued_onto_scout(self):
        r = self._create("usv-99")
        self.assertEqual(r.status_code, 404)
        self.assertEqual([c["vehicle_id"] for c in main.commands], [])

    def test_unknown_vehicle_poll_is_404_not_scouts_queue(self):
        self._create(SCOUT)
        r = self.client.get("/agent/commands?usv_id=usv-99")
        self.assertEqual(r.status_code, 404)


# ---------------------------------------------------------------------------------
# 5. Experiment proxy — the third consumer of the same map
# ---------------------------------------------------------------------------------
class ExperimentRoutingTests(RoutingTestBase):
    def test_get_routes_by_explicit_vehicle_id(self):
        self.http.responses = {SAR_BASE: FakeResp({"active": False, "status": "inactive"})}
        self.client.get(f"/api/experiment/network?vehicle_id={SAR}")
        self.assertEqual(self.http.urls("GET"), [f"{SAR_BASE}/agent/experiment/network"])
        self.assertNotIn(SCOUT_BASE, self.http.hosts_touched())

    def test_get_accepts_slug_spellings_of_vehicle_three(self):
        """A supplied 'usv-3' must reach SAR. Before the route map carried two vehicles this
        argument was int-typed, so any non-numeric spelling silently fell back to the
        last-targeted vehicle — with two routable USVs that is a wrong-vehicle command."""
        self.http.responses = {SAR_BASE: FakeResp({"active": False})}
        self.client.get("/api/experiment/network?vehicle_id=usv-3")
        self.assertEqual(self.http.urls("GET"), [f"{SAR_BASE}/agent/experiment/network"])

    def test_supplied_unknown_vehicle_is_unavailable_not_scout(self):
        r = self.client.get("/api/experiment/network?vehicle_id=usv-99")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["available"])
        self.assertEqual(self.http.calls, [])       # nothing reached Scout

    def test_absent_vehicle_id_uses_the_documented_default_only(self):
        """No id supplied is the ONE case that falls back — to the last-targeted vehicle,
        Scout by default. A SUPPLIED but unrecognised id must never land here."""
        main._last_experiment_vehicle_id = SCOUT
        self.http.responses = {SCOUT_BASE: FakeResp({"active": False})}
        self.client.get("/api/experiment/network")
        self.assertEqual(self.http.urls("GET"), [f"{SCOUT_BASE}/agent/experiment/network"])


if __name__ == "__main__":
    unittest.main()
