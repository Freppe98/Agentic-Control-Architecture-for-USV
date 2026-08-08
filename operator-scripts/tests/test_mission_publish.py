"""The mission publish transaction: Pixhawk → Operator record → Scout planning package.

Run from operator-scripts/:  python -m unittest tests.test_mission_publish   (no pytest).

WHAT THESE TESTS EXIST TO PIN
-----------------------------
A finalized survey used to be reported as "Uploaded & verified" the moment the flight
controller's read-back verified — while Scout still held the PREVIOUS mission's planning
package, because nothing in the station ever sent the new one. These tests hold the whole
publication transaction to its contract:

  • the phases run IN ORDER, and a failed precondition performs no later-stage work;
  • `agent_ready` is true ONLY after Scout's package has been READ BACK and proven to carry
    the same mission id, route hash and route waypoint count;
  • the Pixhawk item count may be route+1 (Home at seq 0) and that offset is applied, once;
  • the package is built from the NEWLY active mission, never the previously active one;
  • a Scout failure NEVER rolls back a verified Pixhawk mission — it durably records
    PACKAGE_SYNC_REQUIRED and offers a package-only retry;
  • the retry sends a package and CANNOT re-upload the mission;
  • publishing is idempotent, and concurrent publishes for one vehicle are refused BUSY;
  • a refreshing/unreachable Scout is never reported as a mismatch, and vice versa.

Every Scout (8090) and vehicle-Flask (8080) call is mocked by swapping `scout_replan.requests`
and `main.requests`. Nothing here touches real networking.
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_contract  # noqa: E402
import mission_publish  # noqa: E402
import replan_package  # noqa: E402
import scout_replan  # noqa: E402
import requests as real_requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2          # Scout — has a LOCAL_AGENT_API_BASE (8090) and a vehicle API (8080)
SAR_VID = 3            # SAR-001 — also configured

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                            "active-original-msn-329c2faff137.json")
FIXTURE_ROUTE_HASH = ("sha256:21e7f7d4ba7fd2c10ccea1621d290de0b8755966804fc6f9"
                      "754479e0ec60d990")
ROUTE_COUNT = 14                       # the real record's route waypoints
PIXHAWK_ITEMS = ROUTE_COUNT + 1        # + Home at seq 0


def real_record(mission_id=None, vid=SCOUT_VID):
    """A fresh deep copy of the captured verified mission record, optionally re-identified."""
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        rec = json.load(fh)
    rec["vehicle_id"] = vid
    rec["upload_status"] = "VERIFIED"
    if mission_id:
        rec["mission_id"] = mission_id
    return rec


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


class FakeLA:
    """Recording fake for scout_replan.requests. Responses are matched by (METHOD, suffix);
    an Exception value is raised (timeout / unreachable)."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.default = FakeResp({}, 200)

    def set(self, method, suffix, resp):
        self.responses[(method, suffix)] = resp

    def _resolve(self, method, url):
        self.calls.append((method, url))
        for (m, suffix), r in self.responses.items():
            if m == method and url.endswith(suffix):
                if isinstance(r, Exception):
                    raise r
                return r
        if isinstance(self.default, Exception):
            raise self.default
        return self.default

    def get(self, url, **kw):
        return self._resolve("GET", url)

    def request(self, method, url, **kw):
        return self._resolve(method, url)


def mission_items(n):
    """`n` MAVLink-ish mission items, as a Pixhawk read-back actually carries them."""
    return [{"seq": i, "command": 16, "lat": 56.0 + i / 1e4, "lng": 12.0, "alt": 0}
            for i in range(n)]


class PixhawkReq:
    """Fake for `main.requests` — the vehicle Flask API (8080) mission read-back. Counts calls
    so a test can PROVE how many live mission downloads a code path costs."""
    RequestException = real_requests.RequestException

    def __init__(self, body=None, raise_exc=None):
        self.body = body if body is not None else {
            # A complete, contract-shaped read-back: 15 items actually downloaded (Home at
            # seq 0 plus 14 route legs), and Scout reports BOTH counts explicitly — so the
            # Home offset is Scout's statement rather than an operator inference.
            "waypoints": mission_items(PIXHAWK_ITEMS), "count": PIXHAWK_ITEMS, "partial": False,
            "pixhawk_item_count": PIXHAWK_ITEMS, "route_waypoint_count": ROUTE_COUNT,
            "route_content_hash": FIXTURE_ROUTE_HASH,
        }
        self.raise_exc = raise_exc
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResp(self.body)


def stored_package(mission_id, route_hash=FIXTURE_ROUTE_HASH, count=ROUTE_COUNT):
    """Scout's v1 planning-package GET body for a package it holds."""
    body = {"stored": True, "usable": True,
            "package": {"mission_id": mission_id, "route_hash": route_hash},
            "summary": {"route_waypoint_count": count},
            "readiness": {"replanning_ready": True, "state": "REPLANNING_READY"}}
    return FakeResp(body)


class PublishTestCase(unittest.TestCase):
    """Shared wiring: both transports mocked, both stores isolated per test."""

    def setUp(self):
        self.client = TestClient(main.app)
        self.fake = FakeLA()
        self._real_sr = scout_replan.requests
        scout_replan.requests = self.fake
        self._real_main = main.requests
        self.pixhawk = PixhawkReq()
        main.requests = self.pixhawk
        main._pixhawk_readback_cache.clear()
        # The mission stores are module-level; snapshot and restore them so no test can leak
        # an active mission into another (which is exactly the staleness class under test).
        self._missions = dict(main.original_missions)
        self._active = dict(main.active_original_by_vehicle)
        main.original_missions.clear()
        main.active_original_by_vehicle.clear()
        del main.publish_operations[:]
        # Persistence is exercised in its own test; elsewhere it must not touch the disk.
        self._real_save = main._save_mission_store
        self.saves = []
        main._save_mission_store = lambda: (self.saves.append(1), True)[1]
        main.last_known_agent[SCOUT_VID] = {"home_status": {"home_position": {
            "latitude": 56.679159, "longitude": 12.811089}}}

    def tearDown(self):
        scout_replan.requests = self._real_sr
        main.requests = self._real_main
        main._save_mission_store = self._real_save
        main._pixhawk_readback_cache.clear()
        main.original_missions.clear()
        main.original_missions.update(self._missions)
        main.active_original_by_vehicle.clear()
        main.active_original_by_vehicle.update(self._active)

    def seed(self, mission_id="msn-publish-1", *, vid=SCOUT_VID, upload_status="VERIFIED"):
        rec = real_record(mission_id, vid)
        rec["upload_status"] = upload_status
        main.original_missions[mission_id] = rec
        main.active_original_by_vehicle[vid] = mission_id
        return rec

    def accept_and_store(self, mission_id, route_hash=FIXTURE_ROUTE_HASH, count=ROUTE_COUNT):
        """Scout accepts the POST and then reports holding exactly that package."""
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"accepted": True, "stored": True}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      stored_package(mission_id, route_hash, count))

    def publish(self, vid=SCOUT_VID, body=None):
        return self.client.post(f"/api/vehicles/{vid}/missions/publish", json=body or {})

    def sync(self, vid=SCOUT_VID, body=None):
        return self.client.post(f"/api/vehicles/{vid}/replan/planning-package/sync",
                                json=body or {})


# ══════════════════════════════════════════════════════════════════════════════════════
# The successful transaction
# ══════════════════════════════════════════════════════════════════════════════════════
class SuccessfulPublishTests(PublishTestCase):

    def test_full_publish_runs_every_phase_in_order_and_is_agent_ready(self):
        rec = self.seed("msn-full-1")
        self.accept_and_store("msn-full-1")
        r = self.publish()
        self.assertEqual(r.status_code, 200)
        env = r.json()

        self.assertEqual([p["phase"] for p in env["phases"]], [
            "VALIDATING_PLAN", "UPLOADING_PIXHAWK", "VERIFYING_PIXHAWK",
            "PERSISTING_OPERATOR_MISSION", "BUILDING_PLANNING_PACKAGE",
            "SYNCING_SCOUT_PACKAGE", "VERIFYING_SCOUT_PACKAGE", "READY"])
        self.assertTrue(all(p["status"] == "ok" for p in env["phases"]))
        self.assertEqual(env["state"], "READY")
        self.assertEqual(env["phase"], "READY")
        self.assertTrue(env["ok"])
        self.assertEqual(env["final"], {"mission_id_match": True, "hash_match": True,
                                        "count_match": True, "agent_ready": True})
        self.assertEqual(env["mission_id"], rec["mission_id"])
        self.assertEqual(env["expected_route_hash"], FIXTURE_ROUTE_HASH)
        self.assertEqual(env["expected_route_count"], ROUTE_COUNT)

    def test_the_three_copies_agree_on_hash_and_count(self):
        self.seed("msn-agree")
        self.accept_and_store("msn-agree")
        env = self.publish().json()
        pix, scout, store = env["pixhawk"], env["scout"], env["operator_store"]
        self.assertEqual(pix["route_hash"], store["route_hash"])
        self.assertEqual(scout["package_route_hash"], store["route_hash"])
        self.assertEqual(pix["route_waypoint_count"], ROUTE_COUNT)
        self.assertEqual(scout["package_route_count"], ROUTE_COUNT)
        self.assertEqual(store["route_waypoint_count"], ROUTE_COUNT)

    def test_raw_pixhawk_item_count_is_route_plus_home_and_never_compared_raw(self):
        self.seed("msn-home-offset")
        self.accept_and_store("msn-home-offset")
        env = self.publish().json()
        pix = env["pixhawk"]
        # The raw item count is REPORTED as 15 and the route count as 14 — the Home offset is
        # applied once, and the two numbers are never conflated.
        self.assertEqual(pix["raw_item_count"], PIXHAWK_ITEMS)
        self.assertEqual(pix["route_waypoint_count"], ROUTE_COUNT)
        self.assertEqual(pix["raw_item_count"], pix["route_waypoint_count"] + 1)
        self.assertTrue(pix["count_match"])
        self.assertTrue(env["final"]["agent_ready"])

    def test_home_offset_is_derived_when_scout_reports_only_raw_items(self):
        # A read-back with no explicit contract counts but a real item list: the Home rule is
        # applied to the raw list rather than assumed, and the source of the number is stated.
        self.pixhawk.body = {"waypoints": mission_items(PIXHAWK_ITEMS), "count": PIXHAWK_ITEMS,
                             "partial": False, "route_content_hash": FIXTURE_ROUTE_HASH}
        self.seed("msn-raw-items")
        self.accept_and_store("msn-raw-items")
        env = self.publish().json()
        self.assertEqual(env["pixhawk"]["route_waypoint_count"], ROUTE_COUNT)
        self.assertEqual(env["pixhawk"]["route_count_source"], "raw_items_minus_home")
        self.assertTrue(env["final"]["agent_ready"])

    def test_an_unknown_count_is_unknown_never_zero(self):
        # No contract counts and no items: the count is UNKNOWN. Deriving "0 route waypoints"
        # from a read-back that reported nothing would fabricate an observation.
        self.pixhawk.body = {"waypoints": [], "count": 0, "partial": False,
                             "route_content_hash": FIXTURE_ROUTE_HASH}
        self.seed("msn-unknown-count")
        self.accept_and_store("msn-unknown-count")
        env = self.publish().json()
        self.assertIsNone(env["pixhawk"]["route_waypoint_count"])
        self.assertIsNone(env["pixhawk"]["count_match"])
        # The hash still proves the route, so the publication completes.
        self.assertTrue(env["final"]["agent_ready"])

    def test_the_package_posted_is_the_v1_package_for_the_active_record(self):
        rec = self.seed("msn-exact-pkg")
        sent = {}

        def capture(method, url, **kw):
            self.fake.calls.append((method, url))
            if method == "POST":
                sent["json"] = kw.get("json")
                return FakeResp({"accepted": True})
            return stored_package("msn-exact-pkg")
        self.fake.request = capture
        self.fake.get = lambda url, **kw: capture("GET", url)
        env = self.publish().json()
        expected, _ = replan_package.build_v1_package(rec, vehicle_id="usv-2")
        self.assertEqual(sent["json"], expected)
        self.assertEqual(env["package_sent"], expected)
        self.assertTrue(env["final"]["agent_ready"])

    def test_publish_targets_only_the_selected_vehicles_local_agent(self):
        self.seed("msn-sar", vid=SAR_VID)
        self.accept_and_store("msn-sar")
        self.publish(vid=SAR_VID)
        urls = [u for _, u in self.fake.calls]
        self.assertTrue(urls)
        self.assertTrue(all(u.startswith("http://10.0.3.10:8090") for u in urls))
        self.assertFalse(any(u.startswith("http://10.0.2.10:8090") for u in urls))

    def test_the_normal_upload_creates_AND_syncs_the_package_in_ONE_call(self):
        # THE bench finding: package synchronization had to be curl'd by hand. It must not be a
        # step the operator knows about — the single publish transaction that follows a verified
        # upload builds the package, sends it, reads it back and proves all three identities.
        rec = self.seed("msn-one-call")
        self.accept_and_store("msn-one-call")
        env = self.publish().json()

        # ONE call. No package-sync request was needed to reach READY.
        self.assertTrue(env["final"]["agent_ready"])
        self.assertEqual(env["state"], "READY")
        self.assertEqual(env["operation"], "publish")       # not "package_sync"
        # The package WAS built and sent within it.
        self.assertIn("BUILDING_PLANNING_PACKAGE", [p["phase"] for p in env["phases"]])
        posts = [u for m, u in self.fake.calls if m == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].endswith("/agent/replan/planning_package"))
        # …and the durable record records the sync as done, so a restart does not re-owe it.
        self.assertEqual(main.original_missions[rec["mission_id"]]["package_sync_state"], "SYNCED")

    def test_the_whole_authoritative_chain_is_proven_in_that_one_transaction(self):
        # immutable Operator original mission == verified Pixhawk route == Scout planning package
        self.seed("msn-chain")
        self.accept_and_store("msn-chain")
        env = self.publish().json()
        store, pix, scout = env["operator_store"], env["pixhawk"], env["scout"]
        self.assertEqual(store["active_mission_id"], "msn-chain")
        self.assertEqual(store["upload_status"], "VERIFIED")
        self.assertEqual(store["route_hash"], pix["route_hash"])
        self.assertEqual(store["route_hash"], scout["package_route_hash"])
        self.assertEqual(scout["package_mission_id"], "msn-chain")
        self.assertTrue(pix["hash_match"])
        self.assertEqual([env["final"]["mission_id_match"], env["final"]["hash_match"],
                          env["final"]["count_match"]], [True, True, True])

    def test_the_failed_stage_is_named_exactly_and_nothing_later_is_claimed(self):
        # "surface exact failed stage" — the phase list stops where it stopped, the error names
        # the cause, and no later phase is reported as ok.
        self.seed("msn-stage")
        self.pixhawk.body = dict(self.pixhawk.body, route_content_hash="sha256:something-else")
        env = self.publish().json()
        self.assertEqual(env["error"], "PIXHAWK_HASH_MISMATCH")
        self.assertEqual(env["phase"], "VERIFYING_PIXHAWK")
        phases = [p["phase"] for p in env["phases"]]
        self.assertEqual(phases[-1], "VERIFYING_PIXHAWK")
        for later in ("BUILDING_PLANNING_PACKAGE", "SYNCING_SCOUT_PACKAGE", "READY"):
            self.assertNotIn(later, phases)
        self.assertFalse(env["final"]["agent_ready"])
        # Previous authoritative state is preserved where safe: the record is still there.
        self.assertIn("msn-stage", main.original_missions)

    def test_the_package_carries_the_approved_home_corridor_when_one_is_proven(self):
        # The corridor rides on the SAME transaction — there is no second step that adds it, and
        # no runtime Home is involved in deriving it.
        rec = self.seed("msn-corridor")
        sent = {}

        def capture(method, url, **kw):
            self.fake.calls.append((method, url))
            if method == "POST":
                sent["json"] = kw.get("json")
                return FakeResp({"accepted": True})
            return stored_package("msn-corridor")
        self.fake.request = capture
        self.fake.get = lambda url, **kw: capture("GET", url)
        self.publish()

        expected, meta = replan_package.build_v1_package(rec, vehicle_id="usv-2")
        self.assertEqual(sent["json"], expected)
        if meta["home_corridor_supplied"]:
            ring = sent["json"]["home_corridor"]
            self.assertGreaterEqual(len({tuple(p) for p in ring}), 3)
            self.assertNotEqual(ring[0], ring[-1])            # implicitly closed
            for lon, lat in ring:                             # [lon, lat] on the wire
                self.assertTrue(-180 <= lon <= 180 and -90 <= lat <= 90)
        else:
            self.assertNotIn("home_corridor", sent["json"])

    def test_publish_issues_no_vehicle_command(self):
        self.seed("msn-no-cmd")
        self.accept_and_store("msn-no-cmd")
        before = len(main.commands)
        self.publish()
        self.assertEqual(len(main.commands), before)
        # The only 8090 write is the package POST.
        writes = [(m, u) for m, u in self.fake.calls if m != "GET"]
        self.assertEqual([m for m, _ in writes], ["POST"])
        self.assertTrue(writes[0][1].endswith("/agent/replan/planning_package"))


# ══════════════════════════════════════════════════════════════════════════════════════
# Agent-ready is not granted early
# ══════════════════════════════════════════════════════════════════════════════════════
class AgentReadyGatingTests(PublishTestCase):

    def test_pixhawk_upload_still_queued_is_pending_not_failed(self):
        self.seed("msn-queued", upload_status="QUEUED")
        r = self.publish()
        self.assertEqual(r.status_code, 202)              # accepted, unresolved — not an error
        env = r.json()
        self.assertEqual(env["phase"], "UPLOADING_PIXHAWK")
        self.assertEqual(env["state"], "UPLOAD_IN_PROGRESS")
        self.assertEqual(env["phases"][-1]["status"], "pending")
        self.assertFalse(env["final"]["agent_ready"])
        # A failed precondition performs NO later-stage work: nothing reached Scout, and no
        # Pixhawk mission download was paid for.
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.pixhawk.calls, 0)

    def test_failed_pixhawk_upload_is_named_and_stops_there(self):
        rec = self.seed("msn-fc-failed", upload_status="FAILED")
        rec["upload_failure_reason"] = "Pixhawk holds 3 route waypoints after upload"
        env = self.publish().json()
        self.assertEqual(env["error"], "PIXHAWK_UPLOAD_FAILED")
        self.assertIn("3 route waypoints", env["message"])
        self.assertEqual(self.fake.calls, [])

    def test_unreachable_readback_blocks_before_any_scout_contact(self):
        self.seed("msn-fc-unreachable")
        main.requests = PixhawkReq(raise_exc=real_requests.ConnectionError("offline"))
        env = self.publish().json()
        self.assertEqual(env["error"], "PIXHAWK_READBACK_UNREACHABLE")
        self.assertFalse(env["pixhawk"]["reachable"])
        self.assertEqual(self.fake.calls, [])

    def test_partial_readback_is_verifying_not_a_mismatch(self):
        self.seed("msn-fc-partial")
        self.pixhawk.body = dict(self.pixhawk.body, partial=True)
        env = self.publish().json()
        self.assertEqual(env["error"], "PIXHAWK_READBACK_PARTIAL")
        self.assertEqual(env["state"], "VERIFYING")       # NOT REAL_MISMATCH
        self.assertEqual(self.fake.calls, [])

    def test_readback_hash_mismatch_is_a_real_mismatch(self):
        self.seed("msn-fc-hashmm")
        self.pixhawk.body = dict(self.pixhawk.body, route_content_hash="sha256:other")
        env = self.publish().json()
        self.assertEqual(env["error"], "PIXHAWK_HASH_MISMATCH")
        self.assertEqual(env["state"], "REAL_MISMATCH")
        self.assertEqual(self.fake.calls, [])

    def test_readback_route_count_mismatch_is_caught_separately_from_the_hash(self):
        self.seed("msn-fc-countmm")
        self.pixhawk.body = dict(self.pixhawk.body, route_waypoint_count=13)
        env = self.publish().json()
        self.assertEqual(env["error"], "PIXHAWK_COUNT_MISMATCH")
        self.assertEqual(self.fake.calls, [])

    def test_scout_package_readback_id_mismatch_is_named_specifically(self):
        self.seed("msn-pkg-idmm")
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp({"accepted": True}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      stored_package("msn-SOMEONE-ELSE"))
        env = self.publish().json()
        self.assertEqual(env["error"], "SCOUT_PACKAGE_ID_MISMATCH")
        self.assertEqual(env["state"], "REAL_MISMATCH")
        self.assertFalse(env["final"]["agent_ready"])
        self.assertFalse(env["final"]["mission_id_match"])

    def test_scout_package_readback_hash_mismatch_is_named_specifically(self):
        self.seed("msn-pkg-hashmm")
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp({"accepted": True}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      stored_package("msn-pkg-hashmm", route_hash="sha256:stale"))
        env = self.publish().json()
        self.assertEqual(env["error"], "SCOUT_PACKAGE_HASH_MISMATCH")
        self.assertFalse(env["final"]["hash_match"])

    def test_scout_package_readback_count_mismatch_is_named_specifically(self):
        self.seed("msn-pkg-countmm")
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp({"accepted": True}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      stored_package("msn-pkg-countmm", count=13))
        env = self.publish().json()
        self.assertEqual(env["error"], "SCOUT_PACKAGE_COUNT_MISMATCH")
        self.assertFalse(env["final"]["count_match"])

    def test_an_unavailable_comparison_is_verifying_never_a_mismatch(self):
        # Scout stores a package but reports no route count: the comparison could not be MADE.
        # That is not agreement and not disagreement, and it must not be dressed as either.
        self.seed("msn-pkg-nocount")
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp({"accepted": True}))
        self.fake.set("GET", "/agent/replan/planning_package",
                      FakeResp({"stored": True, "usable": True,
                                "package": {"mission_id": "msn-pkg-nocount",
                                            "route_hash": FIXTURE_ROUTE_HASH}}))
        env = self.publish().json()
        self.assertEqual(env["error"], "SCOUT_PACKAGE_READBACK_FAILED")
        self.assertEqual(env["state"], "VERIFYING")
        self.assertFalse(env["final"]["agent_ready"])
        self.assertIsNone(env["final"]["count_match"])

    def test_accepted_post_alone_is_not_agent_ready(self):
        # The regression this whole change exists for: Scout says "accepted" but still reports
        # holding the PREVIOUS mission's package. Acceptance is not verification.
        self.seed("msn-new")
        self.fake.set("POST", "/agent/replan/planning_package", FakeResp({"accepted": True}))
        self.fake.set("GET", "/agent/replan/planning_package", stored_package("msn-previous"))
        env = self.publish().json()
        self.assertFalse(env["final"]["agent_ready"])
        self.assertEqual(env["scout"]["post_outcome"], "accepted")
        self.assertEqual(env["error"], "SCOUT_PACKAGE_ID_MISMATCH")


# ══════════════════════════════════════════════════════════════════════════════════════
# Mission identity: the ACTIVE record wins
# ══════════════════════════════════════════════════════════════════════════════════════
class MissionIdentityTests(PublishTestCase):

    def test_package_uses_the_newly_active_mission_not_the_previous_one(self):
        old = self.seed("msn-old")
        old["route_waypoints"] = old["route_waypoints"][:6]
        old["route_hash"] = mission_contract.route_content_hash(old["route_waypoints"])
        new = self.seed("msn-new-active")            # replaces the active mission
        self.accept_and_store("msn-new-active")
        sent = {}

        def capture(method, url, **kw):
            self.fake.calls.append((method, url))
            if method == "POST":
                sent["json"] = kw.get("json")
                return FakeResp({"accepted": True})
            return stored_package("msn-new-active")
        self.fake.request = capture
        self.fake.get = lambda url, **kw: capture("GET", url)

        env = self.publish().json()
        self.assertEqual(env["mission_id"], "msn-new-active")
        self.assertEqual(sent["json"]["mission_id"], "msn-new-active")
        self.assertEqual(sent["json"]["route_hash"], new["route_hash"])
        self.assertNotEqual(sent["json"]["route_hash"], old["route_hash"])
        self.assertTrue(env["final"]["agent_ready"])

    def test_a_stale_older_mission_can_never_be_selected(self):
        self.seed("msn-stale")
        self.seed("msn-current")                     # msn-current is now active
        env = self.publish(body={"mission_id": "msn-stale"}).json()
        self.assertEqual(env["error"], "MISSION_ID_MISMATCH")
        self.assertEqual(self.fake.calls, [])

    def test_another_vehicles_mission_is_refused_by_name(self):
        self.seed("msn-belongs-to-2", vid=SCOUT_VID)
        env = self.publish(vid=SAR_VID, body={"mission_id": "msn-belongs-to-2"}).json()
        self.assertEqual(env["error"], "MISSION_BELONGS_TO_ANOTHER_VEHICLE")
        self.assertEqual(self.fake.calls, [])

    def test_no_active_mission_is_blocked_not_crashed(self):
        env = self.publish().json()
        self.assertEqual(env["error"], "NO_MISSION_RECORD")
        self.assertEqual(env["state"], "BLOCKED")

    def test_an_altered_record_is_refused_before_anything_is_sent(self):
        rec = self.seed("msn-altered")
        rec["route_waypoints"][3]["latitude"] += 0.001     # hash no longer describes the route
        env = self.publish().json()
        self.assertEqual(env["error"], "MISSION_RECORD_ALTERED")
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.pixhawk.calls, 0)

    def test_publish_never_assumes_usv_2(self):
        for vid, base in ((SCOUT_VID, "http://10.0.2.10:8090"), (SAR_VID, "http://10.0.3.10:8090")):
            self.fake.calls = []
            mid = f"msn-multi-{vid}"
            self.seed(mid, vid=vid)
            self.accept_and_store(mid)
            env = self.publish(vid=vid).json()
            self.assertEqual(env["vehicle_id"], f"usv-{vid}")
            self.assertEqual(env["package_sent"]["vehicle_id"], f"usv-{vid}")
            self.assertTrue(all(u.startswith(base) for _, u in self.fake.calls))
            self.assertTrue(env["final"]["agent_ready"])


# ══════════════════════════════════════════════════════════════════════════════════════
# Failure preserves the mission; retry sends a package only
# ══════════════════════════════════════════════════════════════════════════════════════
class FailureAndRetryTests(PublishTestCase):

    def _scout_down(self):
        self.fake.set("POST", "/agent/replan/planning_package",
                      real_requests.ConnectionError("no route to host"))

    def test_scout_unreachable_after_a_verified_upload_requires_a_package_sync(self):
        self.seed("msn-scout-down")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "boom"}, status=400))
        r = self.publish()
        env = r.json()
        self.assertEqual(env["error"], "SCOUT_PACKAGE_POST_FAILED")
        self.assertEqual(env["state"], "PACKAGE_SYNC_REQUIRED")
        # The verified Pixhawk mission and the active record are PRESERVED.
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-scout-down")
        rec = main.original_missions["msn-scout-down"]
        self.assertEqual(rec["upload_status"], "VERIFIED")
        self.assertEqual(rec["package_sync_state"], "REQUIRED")
        self.assertEqual(rec["package_sync_error"], "SCOUT_PACKAGE_POST_FAILED")
        # The Pixhawk verification phase still stands as OK — a Scout failure does not retract it.
        phases = {p["phase"]: p["status"] for p in env["phases"]}
        self.assertEqual(phases["VERIFYING_PIXHAWK"], "ok")

    def test_package_sync_required_survives_a_backend_restart(self):
        # The REAL writer is exercised here — that is the point of the test — but it is pointed
        # at a directory THIS test owns. Restoring the real writer while the module-level path
        # still resolved to `runtime_data/` is precisely how a single fixture mission ended up
        # overwriting the station's approved mission store; main.py now also refuses the
        # production path under a test runner, and this keeps the two tests independent besides.
        tmp = pathlib.Path(tempfile.mkdtemp(prefix="operator-publish-restart-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        real_dir, real_path = main.MISSION_STORE_DIR, main.MISSION_STORE_PATH
        self.seed("msn-restart")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "nope"}, status=400))
        main.MISSION_STORE_DIR, main.MISSION_STORE_PATH = tmp, tmp / "mission_store.json"
        main._save_mission_store = self._real_save          # exercise the real snapshot
        try:
            self.publish()
            # Read what actually landed ON DISK, not just the in-memory snapshot — a restart
            # restores from the file, so the file is the thing under test.
            with open(main.MISSION_STORE_PATH, encoding="utf-8") as fh:
                missions, active = main._validate_mission_store(json.load(fh))
        finally:
            main._save_mission_store = lambda: True
            main.MISSION_STORE_DIR, main.MISSION_STORE_PATH = real_dir, real_path
        self.assertEqual(active[SCOUT_VID], "msn-restart")
        self.assertEqual(missions["msn-restart"]["package_sync_state"], "REQUIRED")
        self.assertEqual(missions["msn-restart"]["upload_status"], "VERIFIED")

    def test_retry_syncs_the_package_and_does_not_upload_the_pixhawk_again(self):
        self.seed("msn-retry")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "nope"}, status=400))
        self.publish()
        commands_before = len(main.commands)
        self.fake.calls = []
        self.accept_and_store("msn-retry")

        r = self.sync()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["synced"])
        self.assertTrue(body["agent_ready"])
        # PROOF the retry cannot re-upload: no command was created, the only 8090 write is the
        # package POST, and no MISSION_UPLOAD phase ran.
        self.assertEqual(len(main.commands), commands_before)
        self.assertEqual([m for m, _ in self.fake.calls if m != "GET"], ["POST"])
        self.assertTrue(all(u.endswith("/agent/replan/planning_package")
                            or "/agent/replan/" in u for _, u in self.fake.calls))
        self.assertEqual(body["publish"]["operation"], "package_sync")
        self.assertEqual(main.original_missions["msn-retry"]["package_sync_state"], "SYNCED")

    def test_retry_uses_the_verified_active_mission_not_a_supplied_stale_one(self):
        self.seed("msn-retry-stale")
        self.seed("msn-retry-active")
        env = self.sync(body={"mission_id": "msn-retry-stale"}).json()
        self.assertEqual(env["error"], "mission_id_mismatch")
        self.assertEqual(self.fake.calls, [])

    def test_a_post_failure_preserves_the_active_operator_mission(self):
        self.seed("msn-preserve")
        self._scout_down()
        self.publish()
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-preserve")
        self.assertIn("msn-preserve", main.original_missions)

    def test_an_unknown_post_is_reconciled_by_the_readback_never_resent(self):
        # Scout's verdict never reached us (a timeout). The package slot is idempotent, so the
        # READ-BACK resolves it — the write is not repeated and is not called a failure.
        self.seed("msn-unknown")
        self.fake.set("POST", "/agent/replan/planning_package",
                      real_requests.Timeout("no verdict"))
        self.fake.set("GET", "/agent/replan/planning_package", stored_package("msn-unknown"))
        env = self.publish().json()
        self.assertEqual(env["scout"]["post_outcome"], "unknown")
        self.assertTrue(env["final"]["agent_ready"])
        self.assertEqual(len([m for m, _ in self.fake.calls if m == "POST"]), 1)


# ══════════════════════════════════════════════════════════════════════════════════════
# Idempotency and concurrency
# ══════════════════════════════════════════════════════════════════════════════════════
class IdempotencyTests(PublishTestCase):

    def test_republishing_the_same_mission_creates_no_new_identity(self):
        self.seed("msn-idem")
        # A STATEFUL Scout: its slot is empty until the first POST lands. That is what makes
        # `idempotent` meaningful — it reports whether the package was ALREADY there, and a
        # fake that always answers "stored" could not distinguish the two publishes.
        state = {"stored": False}

        def talk(method, url, **kw):
            self.fake.calls.append((method, url))
            if method == "POST":
                state["stored"] = True
                return FakeResp({"accepted": True})
            return stored_package("msn-idem") if state["stored"] else FakeResp({"stored": False})
        self.fake.request = talk
        self.fake.get = lambda url, **kw: talk("GET", url)

        first = self.publish().json()
        second = self.publish().json()
        self.assertEqual(first["mission_id"], second["mission_id"])
        self.assertEqual(first["expected_route_hash"], second["expected_route_hash"])
        self.assertTrue(second["final"]["agent_ready"])
        self.assertEqual(main.active_original_by_vehicle[SCOUT_VID], "msn-idem")
        # The first publish found nothing matching; the second found its own package already
        # stored, and says so.
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])

    def test_repeating_a_sync_for_a_matching_package_reports_idempotent_true(self):
        self.seed("msn-idem-sync")
        self.accept_and_store("msn-idem-sync")
        self.sync()
        body = self.sync().json()
        self.assertTrue(body["idempotent"])
        self.assertTrue(body["agent_ready"])

    def test_concurrent_publishes_for_one_vehicle_are_refused_busy(self):
        self.seed("msn-busy")
        with mission_publish.vehicle_publish_lock(SCOUT_VID):
            r = self.publish()
        self.assertEqual(r.status_code, 409)
        env = r.json()
        self.assertEqual(env["error"], "PUBLISH_BUSY")
        self.assertEqual(env["state"], "BUSY")
        self.assertEqual(self.fake.calls, [])

    def test_a_second_vehicles_publish_is_not_blocked_by_the_first(self):
        self.seed("msn-lock-2", vid=SCOUT_VID)
        self.seed("msn-lock-3", vid=SAR_VID)
        self.accept_and_store("msn-lock-3")
        with mission_publish.vehicle_publish_lock(SCOUT_VID):
            r = self.publish(vid=SAR_VID)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["final"]["agent_ready"])

    def test_the_lock_is_released_even_when_the_transaction_fails(self):
        self.seed("msn-lock-release")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "nope"}, status=400))
        self.publish()
        self.assertFalse(mission_publish.is_publishing(SCOUT_VID))

    def test_two_real_threads_do_not_interleave(self):
        self.seed("msn-threads")
        self.accept_and_store("msn-threads")
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait()
            results.append(self.publish().status_code)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Either both serialized to 200, or one was told BUSY. Never two interleaved writes.
        self.assertEqual(len(results), 2)
        self.assertTrue(set(results) <= {200, 409})


# ══════════════════════════════════════════════════════════════════════════════════════
# Diagnostics, persistence visibility and the publish trace
# ══════════════════════════════════════════════════════════════════════════════════════
class DiagnosticsTests(PublishTestCase):

    def test_diagnostics_names_the_process_the_store_and_the_active_missions(self):
        self.seed("msn-diag")
        d = self.client.get("/api/diagnostics").json()
        self.assertTrue(d["ok"])
        self.assertEqual(d["pid"], os.getpid())
        self.assertTrue(d["started_at"])
        self.assertIn("mission_store.json", d["mission_store_path"])
        row = next(r for r in d["active_missions"] if r["vehicle_id"] == "usv-2")
        self.assertEqual(row["mission_id"], "msn-diag")
        self.assertEqual(row["upload_status"], "VERIFIED")

    def test_diagnostics_reports_the_last_publish(self):
        self.seed("msn-diag-2")
        self.accept_and_store("msn-diag-2")
        self.publish()
        d = self.client.get("/api/diagnostics").json()
        self.assertEqual(d["last_publish"]["mission_id"], "msn-diag-2")
        self.assertEqual(d["last_publish"]["state"], "READY")
        self.assertTrue(d["last_publish"]["agent_ready"])

    def test_publish_state_is_readonly_and_makes_no_scout_or_pixhawk_call(self):
        self.seed("msn-state")
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/missions/publish")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mission_id"], "msn-state")
        self.assertEqual(body["route_waypoint_count"], ROUTE_COUNT)
        self.assertIsNone(body["package_sync_state"])
        self.assertFalse(body["publishing"])
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.pixhawk.calls, 0)

    def test_publish_state_reports_an_owed_sync(self):
        self.seed("msn-owed")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "nope"}, status=400))
        self.publish()
        body = self.client.get(f"/api/vehicles/{SCOUT_VID}/missions/publish").json()
        self.assertEqual(body["package_sync_state"], "REQUIRED")
        self.assertEqual(body["package_sync_error"], "SCOUT_PACKAGE_POST_FAILED")

    def test_the_publish_trace_records_each_attempt(self):
        self.seed("msn-trace")
        self.fake.set("POST", "/agent/replan/planning_package",
                      FakeResp({"error": "nope"}, status=400))
        self.publish()
        self.accept_and_store("msn-trace")
        self.publish()
        ops = self.client.get("/api/missions/publish/operations").json()["operations"]
        self.assertEqual([o["state"] for o in ops[-2:]], ["PACKAGE_SYNC_REQUIRED", "READY"])
        self.assertEqual([o["agent_ready"] for o in ops[-2:]], [False, True])

    def test_persistence_is_written_on_every_sync_state_change(self):
        self.seed("msn-persist")
        self.accept_and_store("msn-persist")
        before = len(self.saves)
        self.publish()
        self.assertGreater(len(self.saves), before)      # SYNCED was persisted


# ══════════════════════════════════════════════════════════════════════════════════════
# The pure count helper (no HTTP)
# ══════════════════════════════════════════════════════════════════════════════════════
class RouteCountRuleTests(unittest.TestCase):

    def test_explicit_scout_count_always_wins(self):
        got = mission_publish.route_count_from_readback(
            {"route_waypoint_count": 14, "pixhawk_item_count": 99}, expected=14)
        self.assertEqual(got, (14, "scout_route_waypoint_count"))

    def test_home_offset_applied_to_the_item_count(self):
        self.assertEqual(
            mission_publish.route_count_from_readback({"pixhawk_item_count": 15}, expected=14),
            (14, "raw_items_minus_home"))

    def test_a_mission_without_home_is_not_shifted(self):
        self.assertEqual(
            mission_publish.route_count_from_readback({"pixhawk_item_count": 14}, expected=14),
            (14, "raw_items_no_home"))

    def test_a_genuine_disagreement_is_reported_as_is(self):
        self.assertEqual(
            mission_publish.route_count_from_readback({"pixhawk_item_count": 9}, expected=14),
            (9, "raw_items"))

    def test_no_evidence_is_none_never_zero(self):
        self.assertEqual(mission_publish.route_count_from_readback({}, expected=14), (None, None))
        self.assertEqual(
            mission_publish.route_count_from_readback({"waypoints": [], "count": 0}, expected=14),
            (None, None))
        self.assertEqual(mission_publish.route_count_from_readback(None), (None, None))


if __name__ == "__main__":
    unittest.main()
