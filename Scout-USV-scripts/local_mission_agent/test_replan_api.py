"""
Standalone tests for the replanning HTTP surface: replan_api.py (operation
layer) and agent_server.py routing.

    python3 test_replan_api.py

Covers planning-package upload/read/validate/clear + hash compatibility +
consistency, experiment injection set/read/expiry/clear + bounds, runtime
config read/patch (including rejection during an active transaction and the
two-distinct-flags rule), the status endpoint matching the controller schema,
reset, idempotency, and end-to-end HTTP routing through the real server.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

import agent_server
import experiment_injection as ei
import pixhawk_mission
import planning_package as pp
import replan_api
import replan_config
import replan_controller as rc
import replan_runtime
import route_hash
from config import USV_ID

GOLDEN_HASH = "sha256:5fe4c2352fc9183e121538a8e199131159cdda66658ccb755c7db1ff54672bfd"
_DUMMY_HASH = "sha256:" + "0" * 64

_ROUTE = [
    {"latitude": 56.6500, "longitude": 12.8700, "loiter_time_s": 0, "segment": "OUTBOUND_TRANSIT"},
    {"latitude": 56.6510, "longitude": 12.8700, "loiter_time_s": 0, "segment": "PRIMARY_SURVEY"},
]
_HOME = {"latitude": 56.6490, "longitude": 12.8700}
# navigable_geometry / boundary wire rings are GeoJSON [longitude, latitude].
_NAV_RING = [[12.86, 56.64], [12.89, 56.64], [12.89, 56.66], [12.86, 56.66]]
_ROUTE_HASH = route_hash.route_content_hash(_ROUTE)


def _v1_body(**overrides):
    """A complete, valid replan-planning-package-v1 body. route_hash is derived
    from route_waypoints (after overrides) so the package is self-consistent."""
    body = {
        "package_version": "replan-planning-package-v1",
        "route_contract_version": "mission-contract-v1",
        "mission_id": "m1",
        "mission_revision": 0,
        "vehicle_id": USV_ID,
        "planning_home": [_HOME["longitude"], _HOME["latitude"]],
        "boundary": [list(_NAV_RING)],
        "navigable_geometry": [list(_NAV_RING)],
        "no_go_zones": [],
        "shoreline_clearance_m": 1,
        "route_waypoints": [dict(w) for w in _ROUTE],
        "segments": [],
        "original_execution_order": [],
        "immutable": True,
        "created_at": "2026-08-05T00:00:00Z",
        "source": "OPERATOR_STATION",
    }
    body.update(overrides)
    if "route_hash" not in overrides:
        rw = body.get("route_waypoints")
        try:
            body["route_hash"] = route_hash.route_content_hash(rw) if rw else _DUMMY_HASH
        except Exception:
            body["route_hash"] = _DUMMY_HASH
    return body


def _pixhawk_ok(route=None):
    """A fresh, proof-attributed Pixhawk readback consistent with `route`
    (default _ROUTE) -- the COORDINATED_CACHE envelope GET /agent/pixhawk_mission
    now returns."""
    route = _ROUTE if route is None else route
    return {"reachable": True, "mission_valid": True, "partial": False,
            "route_content_hash": route_hash.route_content_hash(route),
            "route_waypoint_count": len(route), "error": None,
            "proof_source": pp.PROOF_SOURCE_CACHE, "cached": True, "stale": False,
            "refreshing": False, "busy": False, "observed_at": 1000.0, "age_s": 0.5,
            "refresh_generation": 1}


class _DummyGateway:
    def current_authority(self): return "LOCAL_AGENT"


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        ei.clear()
        replan_config.clear_overrides()
        cfg, _ = replan_config.resolve()
        self.ctrl = rc.ReplanController(cfg=cfg, gateway=_DummyGateway(),
                                        status_store=rc.StatusStore(path=os.path.join(self.dir, "st.json")))
        self.energy = __import__("energy_policy").EnergyPolicy(cfg)
        replan_runtime.register(self.ctrl, self.energy)
        # Mock the live Pixhawk readback (no hardware in automated tests). Default
        # is a mission consistent with _ROUTE; individual tests override
        # self.pixhawk to exercise unavailable/partial/mismatch/changed paths.
        self._orig_pixhawk = pixhawk_mission.build_pixhawk_mission_status
        self._orig_pixhawk_proof = pixhawk_mission.build_pixhawk_mission_proof
        self.pixhawk = _pixhawk_ok()
        pixhawk_mission.build_pixhawk_mission_status = lambda: self.pixhawk
        # Acceptance (put_planning_package) now uses the fresh proof variant --
        # mock it to the same fixture so tests exercise the consistency logic,
        # not the network. Proof-freshness itself is covered by dedicated tests.
        pixhawk_mission.build_pixhawk_mission_proof = lambda: self.pixhawk

    def tearDown(self):
        pixhawk_mission.build_pixhawk_mission_status = self._orig_pixhawk
        pixhawk_mission.build_pixhawk_mission_proof = self._orig_pixhawk_proof
        pp._reset_for_tests(os.path.join(self.dir, "pkg.json"))
        ei.clear()
        replan_config.clear_overrides()


# ── Planning package (replan-planning-package-v1) ─────────────────────────────
class TestPlanningPackageAPI(_Base):
    def test_put_valid_and_get(self):
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 200)
        self.assertTrue(out["accepted"])
        self.assertEqual(out["mission_id"], "m1")
        self.assertEqual(out["mission_revision"], 0)
        self.assertEqual(out["route_waypoint_count"], 2)
        self.assertEqual(out["route_hash"], _ROUTE_HASH)
        self.assertTrue(out["readiness"]["replanning_ready"])
        code, out = replan_api.get_planning_package()
        self.assertTrue(out["stored"])
        self.assertTrue(out["usable"])
        self.assertEqual(out["package"]["mission_id"], "m1")
        self.assertEqual(out["readiness"]["state"], pp.READY_USABLE)
        self.assertIsNone(out["readiness"]["connector_proven_safe"])

    def test_acceptance_rejects_stale_cached_readback(self):
        # The acceptance path uses the FRESH proof readback; a stale cached
        # readback must fail closed (503) and store nothing -- never validate
        # the package against evidence that may no longer match the Pixhawk.
        stale = _pixhawk_ok()
        stale.update({"cached": True, "stale": True,
                      "observed_at": 1000.0, "age_s": 30.0})
        self.pixhawk = stale
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 503)
        self.assertFalse(out["accepted"])
        self.assertFalse(out["stored"])
        self.assertEqual(out["error"]["code"], "PIXHAWK_READBACK_STALE")
        # Nothing was stored.
        _, got = replan_api.get_planning_package()
        self.assertFalse(got["stored"])

    def test_acceptance_rejects_readback_without_proof_source(self):
        # A valid-looking readback (correct hash/count) with NO proof_source
        # must not be accepted -- absence of provenance is never fresh.
        unattributed = _pixhawk_ok()
        for k in ("proof_source", "cached", "stale", "refreshing", "busy",
                  "observed_at", "age_s", "refresh_generation"):
            unattributed.pop(k, None)
        self.pixhawk = unattributed
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 503)
        self.assertFalse(out["stored"])
        self.assertEqual(out["error"]["code"], "PIXHAWK_READBACK_UNVERIFIED")

    def test_acceptance_rejects_refreshing_cached_readback(self):
        refreshing = _pixhawk_ok()
        refreshing.update({"cached": True, "refreshing": True,
                           "observed_at": 1000.0, "age_s": 1.0})
        self.pixhawk = refreshing
        code, out = replan_api.put_planning_package(_v1_body())
        self.assertEqual(code, 503)
        self.assertEqual(out["error"]["code"], "PIXHAWK_UNAVAILABLE")

    def test_route_hash_matches_contract(self):
        body = _v1_body(route_waypoints=[
            {"latitude": 56.6501, "longitude": 12.8701, "loiter_time_s": 0},
            {"latitude": 56.6512, "longitude": 12.8725, "loiter_time_s": 30}])
        pkg, code, _msg = pp.validate_package_v1(body, USV_ID)
        self.assertIsNone(code)
        self.assertEqual(pkg["route_hash"], GOLDEN_HASH)

    def test_wrong_target_usv(self):
        code, out = replan_api.put_planning_package(_v1_body(vehicle_id="usv-99"))
        self.assertEqual(code, 400)
        self.assertEqual(out["error"]["code"], "WRONG_TARGET_USV")

    def test_invalid_home(self):
        code, out = replan_api.put_planning_package(_v1_body(planning_home=[0.0, 0.0]))
        self.assertEqual(out["error"]["code"], "INVALID_HOME")

    def test_empty_route(self):
        code, out = replan_api.put_planning_package(_v1_body(route_waypoints=[]))
        self.assertEqual(out["error"]["code"], "EMPTY_ROUTE")

    def test_missing_mission_id(self):
        code, out = replan_api.put_planning_package(_v1_body(mission_id=""))
        self.assertEqual(out["error"]["code"], "MISSING_MISSION_ID")

    def test_unsupported_segment(self):
        code, out = replan_api.put_planning_package(_v1_body(route_waypoints=[
            {"latitude": 56.65, "longitude": 12.87, "segment": "BOGUS"}]))
        self.assertEqual(out["error"]["code"], "UNSUPPORTED_SEGMENT")

    def test_invalid_coordinate(self):
        code, out = replan_api.put_planning_package(_v1_body(route_waypoints=[
            {"latitude": 200.0, "longitude": 12.87}]))
        self.assertEqual(out["error"]["code"], "INVALID_COORDINATE")

    def test_route_too_large(self):
        big = [{"latitude": 56.65 + i * 1e-5, "longitude": 12.87} for i in range(pp.MAX_ROUTE_WAYPOINTS + 1)]
        code, out = replan_api.put_planning_package(_v1_body(route_waypoints=big))
        self.assertEqual(out["error"]["code"], "ROUTE_TOO_LARGE")

    def test_delete_is_idempotent(self):
        replan_api.put_planning_package(_v1_body())
        code, out = replan_api.delete_planning_package()
        self.assertTrue(out["cleared"])
        code, out = replan_api.delete_planning_package()
        self.assertFalse(out["cleared"])

    def test_persistence_after_restart(self):
        replan_api.put_planning_package(_v1_body())
        # Simulate a restart by reloading through a fresh load().
        loaded = pp.load()
        self.assertEqual(loaded["mission_id"], "m1")
        self.assertEqual(loaded["original_route_hash"], pp.route_hash.route_content_hash(loaded["route"]))

    def test_consistency_states(self):
        replan_api.put_planning_package(_v1_body(mission_id="m1"))
        pkg = pp.load()
        self.assertEqual(pp.check_consistency(pkg, "m1")[0], pp.CONSISTENCY_OK)
        self.assertEqual(pp.check_consistency(pkg, "m2")[0], pp.CONSISTENCY_MISSION_MISMATCH)
        self.assertEqual(pp.check_consistency(None, "m1")[0], pp.CONSISTENCY_MISSING)
        self.assertEqual(
            pp.check_consistency(pkg, "m1", current_route_hash="sha256:deadbeef")[0],
            pp.CONSISTENCY_HASH_MISMATCH)


class TestHomeCorridorRuntimeMutationRemoved(_Base):
    # home_corridor is approved mission safety geometry that must be produced
    # and verified by Operator BEFORE mission finalization; Scout consumes it
    # read-only. The runtime PATCH workaround is gone -- prove neither the API
    # function nor the planning_package function exists anymore, so nothing at
    # runtime can attach/widen/replace a corridor after acceptance. The
    # corresponding HTTP route removal is covered in TestHTTPRouting.
    def test_patch_home_corridor_function_removed(self):
        self.assertFalse(hasattr(replan_api, "patch_home_corridor"))
        self.assertFalse(hasattr(pp, "update_home_corridor"))


# ── Experiment injection ──────────────────────────────────────────────────────
class TestExperimentAPI(_Base):
    def test_set_read_clear(self):
        code, out = replan_api.put_experiment({"force_safe_return": True})
        self.assertEqual(code, 200)
        self.assertEqual(out["source"], "SIMULATED")
        code, out = replan_api.get_experiment()
        self.assertTrue(out["active"])
        self.assertEqual(out["source"], "SIMULATED")
        code, out = replan_api.delete_experiment()
        self.assertTrue(out["cleared"])
        code, out = replan_api.delete_experiment()  # idempotent
        self.assertFalse(out["cleared"])

    def test_no_override_rejected(self):
        code, out = replan_api.put_experiment({})
        self.assertEqual(code, 400)
        self.assertEqual(out["error"]["code"], "INVALID_REQUEST")

    def test_invalid_duration(self):
        code, out = replan_api.put_experiment({"force_safe_return": True, "duration_s": -5})
        self.assertEqual(out["error"]["code"], "INVALID_VALUE")

    def test_excessive_duration_capped(self):
        code, out = replan_api.put_experiment({"force_safe_return": True, "duration_s": 999999})
        self.assertEqual(code, 200)
        span = out["injection"]["expires_at"] - out["injection"]["created_at"]
        self.assertLessEqual(span, ei.MAX_DURATION_S + 0.01)

    def test_wrong_target_vehicle(self):
        code, out = replan_api.put_experiment({"force_safe_return": True, "target_vehicle": "usv-99"})
        self.assertEqual(out["error"]["code"], "WRONG_TARGET_USV")

    def test_battery_out_of_bounds(self):
        code, out = replan_api.put_experiment({"battery_percent": 150})
        self.assertEqual(out["error"]["code"], "OUT_OF_BOUNDS")

    # ── E3: communication_state override, same endpoint, no new route ──────
    def test_communication_state_set_read_clear(self):
        code, out = replan_api.put_experiment({"communication_state": "DISCONNECTED", "duration_s": 60})
        self.assertEqual(code, 200)
        self.assertEqual(out["source"], "SIMULATED")
        self.assertEqual(out["injection"]["communication_state"], "DISCONNECTED")
        code, out = replan_api.get_experiment()
        self.assertTrue(out["active"])
        self.assertEqual(out["injection"]["communication_state"], "DISCONNECTED")
        code, out = replan_api.delete_experiment()
        self.assertTrue(out["cleared"])

    def test_communication_state_invalid_value_rejected(self):
        code, out = replan_api.put_experiment({"communication_state": "OFFLINE"})
        self.assertEqual(code, 400)
        self.assertEqual(out["error"]["code"], "INVALID_VALUE")

    def test_communication_state_targets_this_scout_by_default(self):
        code, out = replan_api.put_experiment(
            {"communication_state": "PARTITIONED", "target_vehicle": "usv-2"})
        self.assertEqual(code, 200)
        code, out = replan_api.put_experiment(
            {"communication_state": "PARTITIONED", "target_vehicle": "usv-99"})
        self.assertEqual(out["error"]["code"], "WRONG_TARGET_USV")


# ── Runtime config ────────────────────────────────────────────────────────────
class TestConfigAPI(_Base):
    def test_get_config_shape(self):
        code, out = replan_api.get_config()
        self.assertEqual(code, 200)
        self.assertIn("values", out)
        self.assertIn("sources", out)
        self.assertIn("patchable", out)
        self.assertEqual(out["sources"]["dry_run"], "default")

    def test_patch_valid(self):
        code, out = replan_api.patch_config({"cooldown_s": 45})
        self.assertEqual(code, 200)
        self.assertEqual(out["values"]["cooldown_s"], 45)
        self.assertEqual(out["sources"]["cooldown_s"], "runtime")
        # applied live to the controller
        self.assertEqual(self.ctrl.cfg.cooldown_s, 45)

    def test_patch_unsupported_field(self):
        code, out = replan_api.patch_config({"connect_gap_max_m_typo": 10})
        self.assertEqual(code, 400)
        self.assertEqual(out["error"]["code"], "UNSUPPORTED_SETTING")

    def test_patch_out_of_bounds(self):
        code, out = replan_api.patch_config({"critical_battery_percent": 999})
        self.assertEqual(out["error"]["code"], "OUT_OF_BOUNDS")

    def test_execution_requires_two_distinct_flags(self):
        # Clearing dry_run must NOT enable autonomous execution, and vice versa.
        replan_api.patch_config({"dry_run": False})
        code, out = replan_api.get_config()
        self.assertFalse(out["values"]["autonomous_execution_enabled"])
        replan_api.patch_config({"autonomous_execution_enabled": True})
        code, out = replan_api.get_config()
        self.assertTrue(out["values"]["autonomous_execution_enabled"])
        self.assertFalse(out["values"]["dry_run"])  # still whatever we set, distinct choice

    def test_patch_rejected_during_active_transaction(self):
        self.ctrl._action_lock.acquire()
        try:
            code, out = replan_api.patch_config({"cooldown_s": 10})
            self.assertEqual(code, 409)
            self.assertEqual(out["error"]["code"], "TRANSACTION_ACTIVE")
        finally:
            self.ctrl._action_lock.release()


# ── Status / reset ────────────────────────────────────────────────────────────
class TestStatusAndReset(_Base):
    def test_status_matches_controller_schema(self):
        code, out = replan_api.get_status()
        self.assertEqual(code, 200)
        self.assertEqual(set(out.keys()), set(self.ctrl.status().keys()))
        self.assertIn("fsm_state", out)
        self.assertIn("planning_package_consistency", out)
        self.assertIn("geometry_validation", out)
        self.assertIn("config", out)

    def test_reset_terminal_state(self):
        # Drive the controller to a terminal state cheaply.
        self.ctrl._state = rc.SAFE_HOLD
        self.ctrl._last_terminal_at = 1.0
        code, out = replan_api.reset()
        self.assertEqual(code, 200)
        self.assertTrue(out["reset"])
        self.assertEqual(self.ctrl.status()["fsm_state"], rc.MONITORING)

    def test_reset_rejected_during_active(self):
        self.ctrl._action_lock.acquire()
        try:
            code, out = replan_api.reset()
            self.assertEqual(code, 409)
            self.assertEqual(out["error"]["code"], "RESET_REJECTED")
        finally:
            self.ctrl._action_lock.release()


# ── End-to-end HTTP routing ───────────────────────────────────────────────────
class TestHTTPRouting(_Base):
    def setUp(self):
        super().setUp()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), agent_server.Handler)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def _req(self, method, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_put_get_status_over_http(self):
        code, out = self._req("PUT", "/agent/replan/planning_package", _v1_body())
        self.assertEqual(code, 200)
        self.assertTrue(out["accepted"])
        code, out = self._req("GET", "/agent/replan/status")
        self.assertEqual(code, 200)
        self.assertIn("fsm_state", out)

    def test_post_planning_package_over_http(self):
        # POST and PUT map to the same acceptance operation.
        code, out = self._req("POST", "/agent/replan/planning_package", _v1_body())
        self.assertEqual(code, 200)
        self.assertTrue(out["accepted"])
        self.assertTrue(out["readiness"]["replanning_ready"])
        code, out = self._req("GET", "/agent/replan/planning_package")
        self.assertEqual(code, 200)
        self.assertTrue(out["stored"])
        self.assertTrue(out["usable"])

    def test_patch_config_over_http(self):
        code, out = self._req("PATCH", "/agent/replan/config", {"cooldown_s": 33})
        self.assertEqual(code, 200)
        self.assertEqual(out["values"]["cooldown_s"], 33)

    def test_patch_home_corridor_route_removed_over_http(self):
        # The runtime Home-corridor mutation workaround is gone: this path must
        # no longer be routed at all (falls through to the generic 404), not
        # merely reject the body.
        code, out = self._req("PUT", "/agent/replan/planning_package", _v1_body())
        self.assertEqual(code, 200)
        code, out = self._req("PATCH", "/agent/replan/planning_package/home_corridor", {
            "home_corridor": [[12.86, 56.69], [12.88, 56.69], [12.88, 56.71], [12.86, 56.71]]})
        self.assertEqual(code, 404)

    def test_experiment_over_http(self):
        code, out = self._req("PUT", "/agent/replan/experiment", {"force_safe_return": True})
        self.assertEqual(code, 200)
        self.assertEqual(out["source"], "SIMULATED")
        code, out = self._req("DELETE", "/agent/replan/experiment")
        self.assertTrue(out["cleared"])

    def test_communication_state_experiment_over_http(self):
        # E3: the exact PUT the task describes, over the real HTTP surface --
        # same route as the E2 overrides above, no new endpoint.
        code, out = self._req("PUT", "/agent/replan/experiment", {
            "communication_state": "DISCONNECTED", "duration_s": 60, "target_vehicle": "usv-2"})
        self.assertEqual(code, 200)
        self.assertEqual(out["source"], "SIMULATED")
        self.assertEqual(out["injection"]["communication_state"], "DISCONNECTED")
        code, out = self._req("GET", "/agent/replan/experiment")
        self.assertTrue(out["active"])
        code, out = self._req("DELETE", "/agent/replan/experiment")
        self.assertTrue(out["cleared"])

    def test_bad_json_body_over_http(self):
        url = f"http://127.0.0.1:{self.port}/agent/replan/planning_package"
        req = urllib.request.Request(url, data=b"{not json", method="PUT",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code, out = r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            code, out = e.code, json.loads(e.read().decode())
        self.assertEqual(code, 400)
        self.assertEqual(out["error"]["code"], "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main(verbosity=2)
