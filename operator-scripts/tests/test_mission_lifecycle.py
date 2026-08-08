"""Backend tests for the authority-plus-lifecycle ORCHESTRATION (mission_lifecycle.py).

Run from operator-scripts/:  python -m unittest tests.test_mission_lifecycle  (no pytest).

The defect these pin: normal mission operation used to require the operator to press "Release
Control" on one page and "Start" on another, personally responsible for getting the internal
authority hand-off right. The station now performs that hand-off itself as PHASES of one
operation — which is only safe if every one of the following holds:

  • the ACTIVE PERSISTED mission id is what is forwarded, and a UI-supplied id that does not
    match it is rejected HERE, before Scout is contacted;
  • authority is transferred AND READ BACK as LOCAL_AGENT before Scout Start is contacted;
  • an unverified transfer stops the transaction — Scout is not contacted at all;
  • an accepted (or merely unconfirmed) Start is NEVER resent, and never has authority quietly
    taken back from under it;
  • authority returns to OPERATOR after a failed Start ONLY on proof: a definite pre-action
    refusal AND a canonical status read showing Scout resting in a pre-start state;
  • Pause keeps LOCAL_AGENT; Resume re-acquires it only if it was lost;
  • Stop is Scout's OWN safe-abort transaction: the Operator forwards one intent, writes no
    authority of its own, reimplements no step of the sequence, preserves Scout's evidence
    verbatim, and treats the NOT_READY + start_eligible landing as the expected success it is.

Every Scout HTTP call is mocked by swapping `scout_replan.requests`; the authority proxy is
mocked at main.read_control_authority / main.apply_control_authority, which are the two seams
the orchestration layer is injected with. NOTHING here touches real networking.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import main  # noqa: E402
import mission_lifecycle as ml  # noqa: E402
import scout_mission_execution as mx  # noqa: E402
import scout_replan  # noqa: E402
import requests as real_requests  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SCOUT_VID = 2
SCOUT_BASE = main.LOCAL_AGENT_API_BASE[SCOUT_VID]
MISSION_ID = "msn-0001"


class FakeResp:
    def __init__(self, json_data=None, status=200):
        self._json = json_data if json_data is not None else {}
        self.status_code = status
        self.content = b"1" if json_data is not None else b""

    def json(self):
        return self._json


class FakeLA:
    """Recording fake for the shared Local Agent transport. Match by (METHOD, path-suffix); an
    Exception value is raised (timeout / unreachable); a list is a scripted sequence, one per
    call, whose last entry sticks."""
    RequestException = real_requests.RequestException

    def __init__(self):
        self.calls = []
        self.responses = {}
        self.default = FakeResp({}, 200)
        # ONE ordered log shared with the authority and readiness stubs, so a test can assert
        # not just THAT a proof ran but that it ran BEFORE the first write.
        self.timeline = []

    def set(self, method, suffix, resp):
        self.responses[(method, suffix)] = resp

    def _resolve(self, method, url, json_body=None):
        self.calls.append((method, url, json_body))
        if method != "GET":
            self.timeline.append(("scout-write", url))
        for (m, suffix), r in self.responses.items():
            if m == method and url.endswith(suffix):
                if isinstance(r, list):
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
    body = {
        "supported": True,
        "state": "READY", "effective_state": "READY", "active_operation_id": None,
        "mission_id": MISSION_ID,
        "original_route_hash": "sha256:aaa", "active_route_hash": "sha256:aaa",
        "mode": "LOITER",
        "sequence": {"current": 0, "count": 10, "continuation_verified": None},
        "replanning": {"active": False, "fsm_state": "MONITORING"},
        "return_completion": {"final_loiter_verified": False},
        "authority_status": "LOCAL_AGENT",
        "can_start": True, "can_pause": False, "can_resume": False,
        "mission_execution_enabled": True, "last_error": None, "history": [],
    }
    body.update(over)
    return body


def op_body(operation="start", **over):
    body = {
        "accepted": True, "operation": operation, "operation_id": "op-123",
        "mission_id": MISSION_ID, "previous_state": "READY", "current_state": "RUNNING",
        "verified_mode": "AUTO", "error": None, "final": True, "idempotent": False,
    }
    body.update(over)
    return body


GREEN_READINESS = {
    "ok": True, "mission_ready": True, "replanning_ready": True,
    "vehicle_mission": {"mission_id": MISSION_ID, "record_present": True,
                        "route_hash": "sha256:aaa", "upload_status": "VERIFIED",
                        "pixhawk_verified": True, "readback_reachable": True,
                        "readback_hash": "sha256:aaa", "readback_hash_match": True,
                        "home_valid": True, "home_source": "verified_home"},
    "planning_package": {"stored": True, "usable": True, "consistent": True,
                         "mission_id": MISSION_ID, "mission_id_match": True,
                         "route_hash": "sha256:aaa", "hash_match": True,
                         "consistency": "PLANNING_PACKAGE_CONSISTENT"},
    "limitations": [],
}


class LifecycleTestCase(unittest.TestCase):
    """A fake Local Agent + a scriptable authority proxy. `self.authority` is the value a READ
    reports; `self.authority_writes` records every hand-off the transaction performed, which is
    what most of these tests actually assert on."""

    def setUp(self):
        self.fake = FakeLA()
        self._real_requests = scout_replan.requests
        scout_replan.requests = self.fake
        self.client = TestClient(main.app)
        main.mission_execution_operations.clear()
        main._mx_observed.clear()
        main.event_log.clear()

        self.set_status(status_body())
        self.readiness = dict(GREEN_READINESS)
        self.authority = "OPERATOR"          # the realistic starting point: the operator holds it
        self.authority_writes = []
        # A write that Scout refuses: the read keeps reporting the OLD value, so the read-back
        # verification fails. Set by test_failed_authority_verification_*.
        self.authority_write_ok = True
        self.authority_write_takes_effect = True

        self._real_readiness = main._compute_replan_readiness
        self._real_read = main.read_control_authority
        self._real_apply = main.apply_control_authority

        # Every readiness read the code under test performed, recorded with the read-back age it
        # allowed. That is what proves WHICH proof each caller ran: the read-only preflight may
        # answer from the bounded polling cache, but the Start transaction must force a live read
        # (max_readback_age_s == 0) before it authorizes any vehicle write.
        self.readiness_reads = []
        self.timeline = self.fake.timeline

        def _readiness(vid, base, *, max_readback_age_s=main.PIXHAWK_READBACK_TTL_S):
            self.readiness_reads.append(max_readback_age_s)
            self.timeline.append(("readiness", max_readback_age_s))
            return self.readiness
        main._compute_replan_readiness = _readiness
        main.read_control_authority = lambda vid: {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
            "authority": self.authority, "source": "scout"}

        def _apply(vid, authority, source="operator"):
            self.authority_writes.append((vid, authority, source))
            self.timeline.append(("authority-write", authority))
            if not self.authority_write_ok:
                return {"ok": False, "error": "Scout control-authority API unreachable",
                        "message": "Scout control-authority API unreachable"}, 502
            if self.authority_write_takes_effect:
                self.authority = authority
            return {"ok": True, "vehicle_id": vid, "requested": authority,
                    "authority": self.authority, "available": True, "reachable": True}, 200
        main.apply_control_authority = _apply

        main.active_original_by_vehicle[SCOUT_VID] = MISSION_ID
        main.original_missions[MISSION_ID] = {
            "mission_id": MISSION_ID, "upload_status": "VERIFIED", "route_hash": "sha256:aaa"}

    def tearDown(self):
        scout_replan.requests = self._real_requests
        main._compute_replan_readiness = self._real_readiness
        main.read_control_authority = self._real_read
        main.apply_control_authority = self._real_apply
        main.active_original_by_vehicle.pop(SCOUT_VID, None)
        main.original_missions.pop(MISSION_ID, None)

    # -- helpers ---------------------------------------------------------------------------
    def set_status(self, body, status=200):
        self.fake.set("GET", "/agent/mission_execution/status", FakeResp(body, status))

    def set_status_sequence(self, *responses):
        self.fake.set("GET", "/agent/mission_execution/status", list(responses))

    def set_op(self, operation, resp):
        self.fake.set("POST", f"/agent/mission_execution/{operation}", resp)

    def post(self, operation, **kw):
        return self.client.post(
            f"/api/vehicles/{SCOUT_VID}/mission-execution/{operation}", **kw)

    def scout_posts(self, operation=None):
        suffix = f"/agent/mission_execution/{operation}" if operation else "/agent/"
        return [u for u in self.fake.urls("POST") if u.endswith(suffix) or operation is None]

    def phase(self, body, name):
        return next((p for p in body.get("phases", []) if p["phase"] == name), None)


# ── 1. Mission identity: the persisted active record is the only source ──────────────────
class TestMissionIdentity(LifecycleTestCase):
    def test_start_forwards_the_active_persisted_mission_id(self):
        self.set_op("start", FakeResp(op_body(), 200))
        self.post("start")
        sent = [b for (m, u, b) in self.fake.calls if u.endswith("/start")][0]
        self.assertEqual(sent, {"mission_id": MISSION_ID})

    def test_a_mismatching_ui_supplied_id_is_rejected_before_scout_is_contacted(self):
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start", json={"mission_id": "msn-not-yours"})
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(d["error_code"], "MISSION_ID_MISMATCH")
        self.assertEqual(self.scout_posts("start"), [])
        # …and nothing touched authority either: a locally-refused Start moves nothing.
        self.assertEqual(self.authority_writes, [])
        self.assertEqual(self.authority, "OPERATOR")

    def test_blocked_is_a_distinct_outcome_from_scouts_own_rejection(self):
        """`rejected` means Scout refused. `blocked` means nothing ever left this station.
        Conflating them tells the operator the vehicle answered when it was never asked."""
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start", json={"mission_id": "msn-not-yours"}).json()
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertNotEqual(d["outcome"], mx.OUTCOME_REJECTED)


# ── 2. Start: authority is transferred and VERIFIED before Scout is contacted ────────────
class TestStartAuthority(LifecycleTestCase):
    def test_start_transfers_and_verifies_local_agent_before_contacting_scout(self):
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn((SCOUT_VID, "LOCAL_AGENT", "mission-execution"), self.authority_writes)
        auth = self.phase(d, ml.PHASE_AUTHORITY)
        self.assertIsNotNone(auth)
        self.assertEqual(auth["status"], ml.OK)
        self.assertIs(auth["verified"], True)
        self.assertEqual(auth["observed"], "LOCAL_AGENT")
        self.assertEqual(d["authority"]["before"], "OPERATOR")
        self.assertEqual(d["authority"]["after"], "LOCAL_AGENT")
        self.assertIs(d["authority"]["verified"], True)

    def test_the_operator_never_has_to_release_control_first(self):
        """The whole point: starting from OPERATOR authority, ONE call succeeds."""
        self.authority = "OPERATOR"
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start").json()
        self.assertEqual(d["outcome"], mx.OUTCOME_ACCEPTED)
        self.assertEqual(self.authority, "LOCAL_AGENT")

    def test_the_phases_are_reported_as_one_operation_not_two_commands(self):
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start").json()
        self.assertEqual(d["operation"], "start")
        names = [p["phase"] for p in d["phases"]]
        self.assertEqual(names.index(ml.PHASE_AUTHORITY) < names.index(ml.PHASE_OPERATION), True,
                         names)
        self.assertIn(ml.PHASE_MISSION, names)
        self.assertIn(ml.PHASE_PRECONDITIONS, names)

    def test_an_already_local_agent_vehicle_is_not_re_written(self):
        self.authority = "LOCAL_AGENT"
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start").json()
        self.assertEqual(self.authority_writes, [], "no redundant hand-off")
        self.assertIs(self.phase(d, ml.PHASE_AUTHORITY)["verified"], True)

    def test_failed_authority_verification_prevents_the_scout_start(self):
        """Scout accepted the POST but a READ still reports OPERATOR. That is NOT a transfer,
        and a Start issued on top of it would run a mission the agent does not own."""
        self.authority_write_takes_effect = False
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(d["error_code"], "AUTHORITY_NOT_VERIFIED")
        self.assertEqual(self.scout_posts("start"), [], "Scout Start must not be contacted")
        self.assertIs(self.phase(d, ml.PHASE_AUTHORITY)["verified"], False)

    def test_a_refused_authority_write_prevents_the_scout_start(self):
        self.authority_write_ok = False
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start").json()
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(self.scout_posts("start"), [])
        self.assertIn("unreachable", d["error"])


# ── 3. Start preconditions (evidence, not assumption) ───────────────────────────────────
class TestStartPreconditions(LifecycleTestCase):
    def _blocked(self):
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 409)
        d = r.json()
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(self.scout_posts("start"), [])
        self.assertEqual(self.authority_writes, [], "preconditions are checked BEFORE authority")
        return d

    def test_an_unverified_mission_record_blocks_the_start(self):
        main.original_missions[MISSION_ID]["upload_status"] = "PENDING"
        d = self._blocked()
        self.assertTrue(any("VERIFIED" in b for b in d["blockers"]), d["blockers"])

    def test_a_readback_hash_mismatch_blocks_the_start(self):
        self.readiness = {**GREEN_READINESS,
                          "vehicle_mission": {**GREEN_READINESS["vehicle_mission"],
                                              "readback_hash_match": False}}
        d = self._blocked()
        self.assertTrue(any("read-back" in b for b in d["blockers"]), d["blockers"])

    def test_an_inconsistent_planning_package_blocks_the_start(self):
        self.readiness = {**GREEN_READINESS,
                          "planning_package": {**GREEN_READINESS["planning_package"],
                                               "consistent": False}}
        d = self._blocked()
        self.assertTrue(any("planning package" in b.lower() for b in d["blockers"]), d["blockers"])

    def test_scout_replanning_readiness_false_blocks_the_start(self):
        self.readiness = {**GREEN_READINESS, "replanning_ready": False}
        d = self._blocked()
        self.assertTrue(any("replanning readiness" in b for b in d["blockers"]), d["blockers"])

    def test_operator_authority_alone_does_not_block_the_start(self):
        """OPERATOR authority makes Scout report can_start=false. That is the ONE false the
        transaction may look past — it is the very condition Start exists to resolve. Every
        other false still blocks."""
        self.authority = "OPERATOR"
        self.set_status(status_body(state="NOT_READY", can_start=False,
                                    authority_status="OPERATOR"))
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["outcome"], mx.OUTCOME_ACCEPTED)

    def test_replanning_ownership_still_blocks_even_from_operator_authority(self):
        self.set_status(status_body(state="RUNNING", can_start=False,
                                    authority_status="OPERATOR",
                                    replanning={"active": True, "fsm_state": "PLANNING"}))
        d = self._blocked()
        self.assertTrue(any("replanning controller" in b for b in d["blockers"]), d["blockers"])

    def test_the_preflight_route_reports_the_same_verdict_without_writing(self):
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/preflight")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIs(d["can_start"], True)
        self.assertEqual(d["mission_id"], MISSION_ID)
        self.assertEqual(len(d["checks"]), 5)
        self.assertEqual(self.fake.urls("POST"), [], "a preflight must write nothing")
        self.assertEqual(self.authority_writes, [])


# ── 4. A failed Start: authority comes back ONLY on proof ────────────────────────────────
class TestAuthorityRestore(LifecycleTestCase):
    def test_a_pre_action_rejection_with_scout_resting_restores_operator(self):
        """Scout refused with a 409 before commanding anything and a canonical read shows it
        resting in READY. That is proof, and only then is authority handed back."""
        self.set_op("start", FakeResp({"error": "NO_PLANNING_PACKAGE"}, 409))
        d = self.post("start").json()
        restore = self.phase(d, ml.PHASE_RESTORE)
        self.assertEqual(restore["status"], ml.OK)
        self.assertIs(restore["restored"], True)
        self.assertEqual(self.authority, "OPERATOR")
        self.assertIn((SCOUT_VID, "OPERATOR", "mission-execution"), self.authority_writes)

    def test_a_post_command_failure_never_takes_authority_back(self):
        """LOITER_NOT_VERIFIED happens AFTER Scout began commanding the vehicle. The vehicle
        state is Scout's to describe; taking the wheel back on a guess is how hardware gets
        hurt."""
        self.set_op("start", FakeResp(
            op_body(accepted=False, current_state="FAILED", error="LOITER_NOT_VERIFIED"), 200))
        d = self.post("start").json()
        restore = self.phase(d, ml.PHASE_RESTORE)
        self.assertEqual(restore["status"], ml.WITHHELD)
        self.assertIs(restore["restored"], False)
        self.assertEqual(self.authority, "LOCAL_AGENT")
        self.assertNotIn((SCOUT_VID, "OPERATOR", "mission-execution"), self.authority_writes)

    def test_an_unknown_start_never_takes_authority_back(self):
        self.set_op("start", real_requests.Timeout("read timed out"))
        self.set_status_sequence(FakeResp(status_body(), 200),
                                 FakeResp(status_body(state="RUNNING", can_start=False), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 202)
        d = r.json()
        self.assertEqual(d["outcome"], mx.OUTCOME_UNKNOWN)
        restore = self.phase(d, ml.PHASE_RESTORE)
        self.assertEqual(restore["status"], ml.WITHHELD)
        self.assertEqual(self.authority, "LOCAL_AGENT")

    def test_a_rejected_start_with_scout_RUNNING_never_steals_authority_back(self):
        """Scout refused this Start — because a mission is ALREADY running. Restoring OPERATOR
        here would pull the wheel out from under a live mission."""
        self.set_status_sequence(
            FakeResp(status_body(), 200),                                     # preflight
            FakeResp(status_body(state="RUNNING", can_start=False), 200))     # the proof read
        self.set_op("start", FakeResp({"error": "ARBITRATION_BUSY"}, 409))
        d = self.post("start").json()
        restore = self.phase(d, ml.PHASE_RESTORE)
        self.assertEqual(restore["status"], ml.WITHHELD)
        self.assertEqual(self.authority, "LOCAL_AGENT")

    def test_a_mid_transaction_scout_never_has_authority_taken_back(self):
        self.set_status_sequence(
            FakeResp(status_body(), 200),
            FakeResp(status_body(state="SETTING_HOME", active_operation_id="op-9"), 200))
        self.set_op("start", FakeResp({"error": "ARBITRATION_BUSY"}, 409))
        d = self.post("start").json()
        self.assertEqual(self.phase(d, ml.PHASE_RESTORE)["status"], ml.WITHHELD)
        self.assertEqual(self.authority, "LOCAL_AGENT")

    def test_an_unreadable_status_after_a_failed_start_withholds_the_restore(self):
        self.set_status_sequence(FakeResp(status_body(), 200),
                                 real_requests.RequestException("link died"))
        self.set_op("start", FakeResp({"error": "NO_PLANNING_PACKAGE"}, 409))
        d = self.post("start").json()
        self.assertEqual(self.phase(d, ml.PHASE_RESTORE)["status"], ml.WITHHELD)
        self.assertEqual(self.authority, "LOCAL_AGENT")

    def test_an_accepted_start_keeps_local_agent_authority(self):
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start").json()
        self.assertEqual(self.phase(d, ml.PHASE_RESTORE)["status"], ml.SKIPPED)
        self.assertEqual(self.authority, "LOCAL_AGENT")

    def test_an_accepted_start_is_never_resent_after_a_timeout(self):
        self.set_op("start", real_requests.Timeout("boom"))
        self.set_status_sequence(FakeResp(status_body(), 200),
                                 FakeResp(status_body(state="RUNNING", mode="AUTO",
                                                      can_start=False), 200))
        d = self.post("start").json()
        starts = [u for u in self.fake.urls("POST") if u.endswith("/start")]
        self.assertEqual(len(starts), 1, "a timed-out Start is attempted exactly once")
        self.assertEqual(d["reconciliation"]["resolved"], "running")


# ── 5. Pause and Resume ─────────────────────────────────────────────────────────────────
class TestPauseResume(LifecycleTestCase):
    def test_pause_keeps_local_agent_authority_and_writes_none(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(state="RUNNING", can_start=False, can_pause=True))
        self.set_op("pause", FakeResp(op_body("pause", current_state="PAUSED",
                                              verified_mode="LOITER"), 200))
        d = self.post("pause").json()
        self.assertEqual(self.authority_writes, [], "Pause must never move authority")
        self.assertEqual(self.authority, "LOCAL_AGENT")
        self.assertEqual(self.phase(d, ml.PHASE_AUTHORITY)["status"], ml.SKIPPED)

    def test_pause_requires_a_verified_paused_loiter_state(self):
        self.authority = "LOCAL_AGENT"
        # Pause performs NO preflight read, so the one status GET it makes is the verification.
        self.set_status(status_body(state="PAUSED", mode="LOITER", can_pause=False,
                                    can_resume=True))
        self.set_op("pause", FakeResp(op_body("pause", current_state="PAUSED",
                                              verified_mode="LOITER"), 200))
        d = self.post("pause").json()
        verify = self.phase(d, ml.PHASE_VERIFY)
        self.assertEqual(verify["status"], ml.OK)
        self.assertIs(verify["verified"], True)
        self.assertEqual(verify["observed_state"], "PAUSED")
        self.assertEqual(verify["observed_mode"], "LOITER")

    def test_an_accepted_pause_that_did_not_reach_paused_loiter_is_flagged_not_claimed(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(state="RUNNING", mode="AUTO"))
        self.set_op("pause", FakeResp(op_body("pause"), 200))
        d = self.post("pause").json()
        verify = self.phase(d, ml.PHASE_VERIFY)
        self.assertEqual(verify["status"], ml.WITHHELD)
        self.assertIs(verify["verified"], False)
        self.assertIn("RUNNING", verify["detail"])

    def test_resume_does_not_reacquire_authority_when_it_is_still_local_agent(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(state="PAUSED", can_start=False, can_resume=True))
        self.set_op("resume", FakeResp(op_body("resume"), 200))
        d = self.post("resume").json()
        self.assertEqual(self.authority_writes, [], "nothing to re-acquire")
        self.assertIs(self.phase(d, ml.PHASE_AUTHORITY)["verified"], True)

    def test_resume_reacquires_and_verifies_authority_when_it_was_lost(self):
        self.authority = "OPERATOR"          # e.g. the operator took control while paused
        self.set_status(status_body(state="PAUSED", can_start=False, can_resume=True))
        self.set_op("resume", FakeResp(op_body("resume"), 200))
        d = self.post("resume").json()
        self.assertIn((SCOUT_VID, "LOCAL_AGENT", "mission-execution"), self.authority_writes)
        self.assertEqual(self.authority, "LOCAL_AGENT")
        self.assertIs(self.phase(d, ml.PHASE_AUTHORITY)["verified"], True)

    def test_resume_with_an_unverifiable_authority_never_contacts_scout(self):
        self.authority = "OPERATOR"
        self.authority_write_takes_effect = False
        self.set_status(status_body(state="PAUSED", can_resume=True))
        self.set_op("resume", FakeResp(op_body("resume"), 200))
        d = self.post("resume").json()
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(d["error_code"], "AUTHORITY_NOT_VERIFIED")
        self.assertEqual([u for u in self.fake.urls("POST") if u.endswith("/resume")], [])


# ── 6. Stop — Scout's own safe-abort transaction, forwarded and evidenced ────────────────
#
# The contract this pins, and why each half matters:
#
#   Scout owns the WHOLE sequence — verified LOITER, verify the active mission identity, restore
#   the immutable original mission when a verified revised route is installed, rewind it to its
#   start, verify the rewind, reset execution/replan/test state, clear the experiment injection,
#   invalidate the runtime Home, return supervisory authority to OPERATOR, re-prove the evidence.
#
#   The OPERATOR forwards ONE intent and re-reads status. It sends no LOITER, no upload, no
#   rewind, no reset, no rearm and — the change from the previous contract — NO AUTHORITY WRITE.
#   A hand-off this station performed would prove nothing about the one Scout was supposed to
#   make, so the authority phase READS IT BACK and reports what it found.
STOP_OK_EVIDENCE = {
    "hold_verified": True, "original_restored": True,
    "active_hash_before": "sha256:revised", "original_hash": "sha256:aaa",
    "revised_hash": "sha256:revised", "rewind_verified": True, "sequence_after": 0,
    "replan_reset": True, "experiment_cleared": True, "authority_after": "OPERATOR",
    "ready_for_start": True, "outcome": "STOPPED",
}


def stopped_status(**over):
    """Scout's canonical status AFTER a successful stop: the run is over, the vehicle is holding,
    the original mission is rewound, and authority is deliberately back with the OPERATOR — which
    is exactly why the state is NOT_READY with start_eligible true and authority_blocks_start
    true. That combination is the EXPECTED landing, not a failure."""
    body = status_body(
        state="NOT_READY", effective_state="NOT_READY", mode="LOITER",
        can_start=False, can_pause=False, can_resume=False, can_stop=False,
        start_eligible=True, execution_ready=False, authority_blocks_start=True,
        authority_status="OPERATOR",
        sequence={"current": 0, "count": 10, "continuation_verified": None},
        stop=dict(STOP_OK_EVIDENCE))
    body.update(over)
    return body


class TestStop(LifecycleTestCase):
    # -- the proxy itself ------------------------------------------------------------------
    def test_stop_proxies_to_scouts_stop_route_with_the_active_mission_id(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER",
                                             stop=dict(STOP_OK_EVIDENCE)), 200))
        r = self.post("stop")
        self.assertEqual(r.status_code, 200)
        self.assertIn(f"{SCOUT_BASE}/agent/mission_execution/stop", self.fake.urls("POST"))
        sent = [b for (m, u, b) in self.fake.calls if u.endswith("/mission_execution/stop")][0]
        self.assertEqual(sent, {"mission_id": MISSION_ID})

    def test_the_operator_reimplements_no_part_of_the_stop_sequence(self):
        """ONE POST leaves this station. No LOITER, no upload, no rewind, no reset, no rearm, no
        RTL, no disarm, no planning-package write — and nothing through the command queue."""
        self.authority = "LOCAL_AGENT"
        before = len(main.commands)
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER",
                                             stop=dict(STOP_OK_EVIDENCE)), 200))
        self.post("stop")
        self.assertEqual(len(main.commands), before, "no queued command")
        self.assertEqual(self.fake.urls("POST"),
                         [f"{SCOUT_BASE}/agent/mission_execution/stop"])
        for forbidden in ("rtl", "disarm", "loiter", "mission_clear", "mission_upload",
                          "planning_package", "rearm", "reset", "experiment", "nav/stop"):
            self.assertFalse(any(forbidden in u for u in self.fake.urls()), forbidden)

    def test_stop_writes_no_authority_of_its_own(self):
        """Scout returns authority as part of ITS transaction. The Operator observes; it does not
        perform the hand-off, and it does not 'help' by writing OPERATOR itself."""
        self.authority = "OPERATOR"        # Scout already handed it back
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER",
                                             stop=dict(STOP_OK_EVIDENCE)), 200))
        d = self.post("stop").json()
        self.assertEqual(self.authority_writes, [], "the Operator writes no authority for a stop")
        restore = self.phase(d, ml.PHASE_RESTORE)
        self.assertEqual(restore["status"], ml.OK)
        self.assertIs(restore["restored"], True)
        self.assertIs(restore["written"], False)
        self.assertEqual(restore["observed"], "OPERATOR")
        self.assertEqual(d["authority"]["after"], "OPERATOR")

    def test_a_disagreeing_authority_readback_is_stated_not_papered_over(self):
        """Scout claims it returned authority; the read-back says otherwise. The Operator reports
        the disagreement and STILL writes nothing — Take Control is the explicit override."""
        self.authority = "LOCAL_AGENT"
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER",
                                             stop=dict(STOP_OK_EVIDENCE)), 200))
        d = self.post("stop").json()
        restore = self.phase(d, ml.PHASE_RESTORE)
        self.assertEqual(restore["status"], ml.WITHHELD)
        self.assertIs(restore["restored"], False)
        self.assertIs(restore["written"], False)
        self.assertEqual(restore["claimed"], "OPERATOR")
        self.assertEqual(restore["observed"], "LOCAL_AGENT")
        self.assertIn("Take Control", restore["detail"])
        self.assertEqual(self.authority_writes, [])
        self.assertEqual(self.authority, "LOCAL_AGENT")

    # -- success: NOT_READY + start_eligible is the EXPECTED landing -----------------------
    def test_a_successful_stop_is_accepted_even_though_scout_rests_in_not_ready(self):
        self.authority = "OPERATOR"
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER",
                                             stop=dict(STOP_OK_EVIDENCE)), 200))
        d = self.post("stop").json()
        self.assertEqual(d["outcome"], mx.OUTCOME_ACCEPTED)
        self.assertIs(d["ok"], True)
        self.assertNotEqual(d.get("resulting_state"), "FAILED")
        verify = self.phase(d, ml.PHASE_VERIFY)
        self.assertEqual(verify["status"], ml.OK)
        self.assertIs(verify["verified"], True)
        self.assertIs(verify["held_in_loiter"], True)
        self.assertIs(verify["start_eligible"], True)
        self.assertIs(verify["authority_blocks_start"], True)
        self.assertIn("NEW Start is eligible", verify["detail"])

    def test_scouts_stop_evidence_is_preserved_verbatim(self):
        self.authority = "OPERATOR"
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER"), 200))
        d = self.post("stop").json()
        ev = d["stop"]
        self.assertIs(ev["reported"], True)
        for field in mx.STOP_EVIDENCE_FIELDS:
            self.assertEqual(ev[field], STOP_OK_EVIDENCE[field], field)

    def test_evidence_scout_omits_stays_none_rather_than_false(self):
        """Tri-state: 'Scout could not verify the rewind' and 'Scout said nothing about the
        rewind' are different facts. Rounding the second into the first reports a failure Scout
        never claimed."""
        self.authority = "OPERATOR"
        self.set_status(stopped_status(stop={"hold_verified": True, "outcome": "STOPPED"}))
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER"), 200))
        ev = self.post("stop").json()["stop"]
        self.assertIs(ev["hold_verified"], True)
        self.assertIsNone(ev["rewind_verified"])
        self.assertIsNone(ev["original_restored"])

    def test_a_scout_that_reports_no_stop_block_produces_no_fabricated_evidence(self):
        self.authority = "OPERATOR"
        self.set_status(status_body(state="NOT_READY", mode="LOITER", start_eligible=True,
                                    authority_blocks_start=True))
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER"), 200))
        ev = self.post("stop").json()["stop"]
        self.assertIs(ev["reported"], False)
        self.assertTrue(all(ev[f] is None for f in mx.STOP_EVIDENCE_FIELDS))

    # -- failure after the safe hold ------------------------------------------------------
    def test_a_stop_that_fails_after_the_hold_reports_scouts_exact_code(self):
        for code in ("STOP_ACTIVE_MISSION_UNKNOWN", "STOP_RESTORE_UPLOAD_FAILED",
                     "STOP_RESTORE_HASH_MISMATCH", "STOP_REWIND_NOT_VERIFIED"):
            with self.subTest(code=code):
                self.fake.calls.clear()
                self.authority_writes.clear()
                self.authority = "LOCAL_AGENT"
                self.set_status(status_body(
                    state="SUSPENDED", effective_state="SUSPENDED", mode="LOITER",
                    can_start=False, can_stop=False, last_error={"code": code},
                    stop={"hold_verified": True, "original_restored": False,
                          "rewind_verified": False, "outcome": code}))
                self.set_op("stop", FakeResp(
                    op_body("stop", accepted=False, current_state="SUSPENDED",
                            verified_mode="LOITER", error={"code": code, "message": "scout said"}),
                    200))
                d = self.post("stop").json()
                self.assertEqual(d["outcome"], mx.OUTCOME_FAILED)
                self.assertEqual(d["scout_error_code"], code)
                self.assertIn(code, mx.STOP_ERROR_CODES)
                # …and no automatic recovery of any kind was attempted.
                self.assertEqual(self.authority_writes, [])
                self.assertEqual(self.fake.urls("POST"),
                                 [f"{SCOUT_BASE}/agent/mission_execution/stop"])

    def test_a_failed_stop_states_that_the_vehicle_is_held_and_the_reset_incomplete(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(
            state="SUSPENDED", effective_state="SUSPENDED", mode="LOITER", can_start=False,
            last_error={"code": "STOP_REWIND_NOT_VERIFIED", "message": "sequence read back as 4"},
            stop={"hold_verified": True, "rewind_verified": False,
                  "outcome": "STOP_REWIND_NOT_VERIFIED"}))
        # Scout accepted the request and reported the failure through its canonical state.
        self.set_op("stop", FakeResp(op_body("stop", current_state="SUSPENDED",
                                             verified_mode="LOITER"), 200))
        d = self.post("stop").json()
        verify = self.phase(d, ml.PHASE_VERIFY)
        self.assertEqual(verify["status"], ml.FAILED)
        self.assertIs(verify["verified"], False)
        self.assertIs(verify["held_in_loiter"], True)
        self.assertIn("HELD in LOITER", verify["detail"])
        self.assertIn("reset is incomplete", verify["detail"])
        self.assertIn("STOP_REWIND_NOT_VERIFIED", verify["detail"])

    def test_a_failed_stop_never_triggers_a_rearm_resume_auto_or_second_stop(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(
            state="SUSPENDED", mode="LOITER", can_start=False,
            last_error={"code": "STOP_RESTORE_UPLOAD_FAILED"},
            stop={"hold_verified": True, "outcome": "STOP_RESTORE_UPLOAD_FAILED"}))
        self.set_op("stop", FakeResp(op_body("stop", current_state="SUSPENDED",
                                             verified_mode="LOITER"), 200))
        self.post("stop")
        posts = self.fake.urls("POST")
        self.assertEqual(posts, [f"{SCOUT_BASE}/agent/mission_execution/stop"],
                         "exactly one write, and it is the stop the operator asked for")

    # -- mid-transaction and unsupported --------------------------------------------------
    def test_a_stop_still_working_through_its_sequence_confirms_nothing(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(state="STOP_RESTORING_ORIGINAL", mode="LOITER",
                                    active_operation_id="op-9"))
        self.set_op("stop", FakeResp(op_body("stop", current_state="STOP_RESTORING_ORIGINAL"),
                                     200))
        d = self.post("stop").json()
        verify = self.phase(d, ml.PHASE_VERIFY)
        self.assertEqual(verify["status"], ml.WITHHELD)
        self.assertIs(verify["verified"], False)
        self.assertEqual(self.authority_writes, [])

    def test_an_unsupported_scout_stop_is_explicit_and_changes_nothing(self):
        self.authority = "LOCAL_AGENT"
        self.set_status(status_body(state="RUNNING", can_start=False, can_pause=True))
        self.set_op("stop", FakeResp(None, 404))
        r = self.post("stop")
        self.assertEqual(r.status_code, 200)     # a handled "not supported", not an error
        d = r.json()
        self.assertIs(d["supported"], False)
        self.assertEqual(d["outcome"], mx.OUTCOME_UNSUPPORTED)
        self.assertEqual(d["error_code"], "STOP_NOT_SUPPORTED")
        self.assertIn("does not implement POST /agent/mission_execution/stop", d["error"])
        self.assertIn("raw Pixhawk stop is not offered", d["error"])
        self.assertIn("Rearm is not a substitute", d["error"])
        self.assertEqual(self.authority_writes, [], "an unsupported Stop moves no authority")
        self.assertEqual(self.authority, "LOCAL_AGENT")
        self.assertEqual(self.phase(d, ml.PHASE_RESTORE)["status"], ml.SKIPPED)

    def test_an_unknown_stop_is_reconciled_by_reading_status_never_resent(self):
        self.authority = "OPERATOR"
        self.set_status(stopped_status())
        self.set_op("stop", real_requests.ConnectionError("write timed out"))
        r = self.post("stop")
        self.assertEqual(r.status_code, 202)
        d = r.json()
        self.assertEqual(d["outcome"], mx.OUTCOME_UNKNOWN)
        self.assertEqual(d["reconciliation"]["resolved"], "stopped")
        # ONE attempt. A resend could re-run a whole restore/rewind on a vehicle that already did.
        self.assertEqual(len(self.fake.urls("POST")), 1)

    # -- the write trace -------------------------------------------------------------------
    def test_the_write_trace_records_the_stop_evidence(self):
        self.authority = "OPERATOR"
        self.set_status(stopped_status())
        self.set_op("stop", FakeResp(op_body("stop", current_state="NOT_READY",
                                             verified_mode="LOITER"), 200))
        self.post("stop")
        e = self.client.get("/api/mission-execution/operations").json()["operations"][-1]
        self.assertEqual(e["operation"], "stop")
        self.assertIs(e["stop"]["rewind_verified"], True)
        self.assertEqual(e["stop"]["authority_after"], "OPERATOR")
        self.assertEqual(e["stop"]["outcome"], "STOPPED")


class TestStopEligibilityAfterStop(LifecycleTestCase):
    """The landing a successful Stop leaves behind must not read as a broken mission."""

    def test_not_ready_with_start_eligible_and_authority_blocking_is_startable(self):
        summary = mx.summarize_status({"outcome": mx.OUTCOME_ACCEPTED, "supported": True,
                                       "reachable": True, "scout": stopped_status()})
        elig = ml.start_eligibility(summary)
        self.assertIs(elig["eligible"], True)
        self.assertIs(elig["deferred_on_authority"], True)
        self.assertIs(elig["execution_ready"], False)
        self.assertIn("acquires and verifies", elig["reason"])

    def test_a_start_right_after_a_stop_takes_authority_back_and_runs(self):
        self.authority = "OPERATOR"
        self.set_status(stopped_status())
        self.set_op("start", FakeResp(op_body(), 200))
        d = self.post("start").json()
        self.assertEqual(d["outcome"], mx.OUTCOME_ACCEPTED)
        self.assertIn((SCOUT_VID, "LOCAL_AGENT", "mission-execution"), self.authority_writes)
        self.assertEqual(self.authority, "LOCAL_AGENT")


# ── 7. The write trace records the authority hand-off ───────────────────────────────────
class TestTransactionTrace(LifecycleTestCase):
    def test_a_transaction_records_its_phases_and_authority(self):
        self.set_op("start", FakeResp(op_body(), 200))
        self.post("start")
        e = self.client.get("/api/mission-execution/operations").json()["operations"][-1]
        self.assertEqual(e["operation"], "start")
        self.assertEqual(e["mission_id"], MISSION_ID)
        self.assertEqual(e["authority"]["before"], "OPERATOR")
        self.assertEqual(e["authority"]["after"], "LOCAL_AGENT")
        self.assertTrue(any(p["phase"] == ml.PHASE_AUTHORITY for p in e["phases"]))

    def test_a_blocked_transaction_is_recorded_too(self):
        self.set_op("start", FakeResp(op_body(), 200))
        self.post("start", json={"mission_id": "msn-nope"})
        e = self.client.get("/api/mission-execution/operations").json()["operations"][-1]
        self.assertEqual(e["outcome"], ml.OUTCOME_BLOCKED)
        self.assertIsInstance(e["error"], str)


# ── 8. Pure derivations (no HTTP) ───────────────────────────────────────────────────────
class TestPureDerivations(unittest.TestCase):
    def test_readable_text_never_produces_a_python_repr_or_object_coercion(self):
        self.assertEqual(ml._text({"code": "X", "message": "boom"}), "boom")
        self.assertEqual(ml._text({"a": 1, "b": 2}), "a=1 · b=2")
        self.assertEqual(ml._text(["a", "b"]), "a; b")
        self.assertIsNone(ml._text(None))
        self.assertIsNone(ml._text("   "))
        self.assertNotIn("{", ml._text({"a": 1}))

    def test_pre_action_codes_exclude_everything_raised_after_a_vehicle_command(self):
        for code in ("LOITER_NOT_VERIFIED", "SET_HOME_FAILED", "PACKAGE_SYNC_FAILED",
                     "AUTO_NOT_VERIFIED", "PROGRESSION_UNCONFIRMED",
                     "PACKAGE_INCONSISTENT_AFTER_SYNC"):
            self.assertNotIn(code, mx.PRE_ACTION_ERROR_CODES, code)
        for code in ("NO_ACTIVE_MISSION", "NO_PLANNING_PACKAGE", "MISSION_ID_MISMATCH",
                     "REPLANNING_ACTIVE", "ARBITRATION_BUSY"):
            self.assertIn(code, mx.PRE_ACTION_ERROR_CODES, code)

    def test_pre_start_states_are_the_only_ones_a_restore_may_conclude_from(self):
        for s in ("RUNNING", "PAUSED", "RETURNING_HOME", "COMPLETED_HOLD", "SETTING_HOME"):
            self.assertNotIn(s, mx.PRE_START_STATES, s)
        for s in ("READY", "NOT_READY", "NOT_STARTED", "STOPPED", "CANCELLED"):
            self.assertIn(s, mx.PRE_START_STATES, s)

    def test_start_eligibility_defers_on_authority_and_on_nothing_else(self):
        base = {"present": True, "state": "NOT_READY", "can_start": False,
                "authority_status": "OPERATOR", "mission_execution_enabled": True,
                "replanning_active": False, "active_operation_id": None}
        e = ml.start_eligibility(base)
        self.assertTrue(e["eligible"])
        self.assertTrue(e["deferred_on_authority"])
        # …but never past replanning, an active operation, or a disabled lifecycle.
        for over in ({"replanning_active": True}, {"active_operation_id": "op-1"},
                     {"mission_execution_enabled": False}):
            self.assertFalse(ml.start_eligibility({**base, **over})["eligible"], over)
        # …and never from a state a mission cannot be started from.
        self.assertFalse(ml.start_eligibility({**base, "state": "RUNNING"})["eligible"])

    def test_an_unavailable_status_is_never_eligible(self):
        self.assertFalse(ml.start_eligibility({"present": False})["eligible"])


# ── 8. Proof completeness: "not ready" vs "we could not read the evidence" ───────────────
class TestProofCompleteness(LifecycleTestCase):
    """The ~10 s read-back transient. main.PIXHAWK_READBACK_TTL_S expires, the poll pays for a
    live MAVLink download, and a download that times out or arrives partial answers
    can_start:false — with THREE blockers, because the package hash chain and Scout's replanning
    readiness are both anchored on the read-back. Not one of those is a fact about the vehicle.

    The preflight must therefore say, in the PAYLOAD, whether its verdict is a proof. `can_start`
    is unaffected: an unread precondition is not a satisfied one, and Start stays fail-closed."""

    def _readiness(self, **vm_over):
        rd = {k: dict(v) if isinstance(v, dict) else v for k, v in GREEN_READINESS.items()}
        rd["vehicle_mission"].update(vm_over)
        return rd

    def preflight(self):
        return self.client.get(
            f"/api/vehicles/{SCOUT_VID}/mission-execution/preflight").json()

    def test_a_complete_green_read_is_a_complete_proof(self):
        self.readiness = self._readiness(readback_cached=False, readback_partial=False)
        d = self.preflight()
        self.assertIs(d["can_start"], True)
        self.assertIs(d["proof_complete"], True)
        self.assertIs(d["readiness_refreshing"], False)
        self.assertIsNone(d["readiness_reason_code"])

    def test_an_unreachable_live_readback_is_an_INCOMPLETE_proof(self):
        # Exactly the transient: the cache expired, the live download failed.
        self.readiness = self._readiness(readback_reachable=False, readback_hash=None,
                                         readback_hash_match=False, readback_cached=False)
        d = self.preflight()
        self.assertIs(d["can_start"], False, "Start still fails closed")
        self.assertIs(d["proof_complete"], False)
        self.assertIs(d["readiness_refreshing"], True)
        self.assertEqual(d["readiness_reason_code"], ml.EVIDENCE_READBACK_UNAVAILABLE)

    def test_a_partial_readback_is_an_INCOMPLETE_proof(self):
        # The nastier one: a partial download yields a DIFFERENT hash, so the blocker reads like
        # a genuine route mismatch. `readback_partial` is what proves it is an unread input.
        self.readiness = self._readiness(readback_partial=True, readback_hash="sha256:zzz",
                                         readback_hash_match=False, readback_cached=False)
        d = self.preflight()
        self.assertIs(d["proof_complete"], False)
        self.assertEqual(d["readiness_reason_code"], ml.EVIDENCE_READBACK_PARTIAL)

    def test_a_CACHED_failed_readback_is_incomplete_but_not_called_refreshing(self):
        self.readiness = self._readiness(readback_reachable=False, readback_hash_match=False,
                                         readback_cached=True)
        d = self.preflight()
        self.assertIs(d["proof_complete"], False)
        self.assertIs(d["readiness_refreshing"], False,
                      "a Scout that has been down for a minute is unavailable, not refreshing")

    def test_an_unreadable_scout_status_is_an_incomplete_proof(self):
        self.set_status({}, status=503)
        d = self.preflight()
        self.assertIs(d["proof_complete"], False)
        self.assertEqual(d["readiness_reason_code"], ml.EVIDENCE_STATUS_UNAVAILABLE)

    def test_an_unreadable_planning_package_is_an_incomplete_proof(self):
        rd = self._readiness()
        rd["planning_package"] = {**rd["planning_package"], "scout_reachable": False,
                                  "consistent": False}
        self.readiness = rd
        d = self.preflight()
        self.assertIs(d["proof_complete"], False)
        self.assertEqual(d["readiness_reason_code"], ml.EVIDENCE_PACKAGE_UNAVAILABLE)

    def test_an_OLDER_scout_is_a_complete_answer_not_a_gap(self):
        rd = self._readiness()
        rd["planning_package"] = {**rd["planning_package"], "scout_reachable": False,
                                  "scout_supported": False, "consistent": False}
        self.readiness = rd
        d = self.preflight()
        self.assertIs(d["proof_complete"], True,
                      "an older Scout is a permanent, complete answer — not a refresh")

    def test_a_PROVEN_failure_is_a_complete_proof(self):
        # Every input was read; the record simply is not VERIFIED. That withdraws Start for real.
        rd = self._readiness()
        rd["vehicle_mission"]["pixhawk_verified"] = False
        main.original_missions[MISSION_ID]["upload_status"] = "PENDING"
        self.readiness = rd
        d = self.preflight()
        self.assertIs(d["can_start"], False)
        self.assertIs(d["proof_complete"], True)
        self.assertIsNone(d["readiness_reason_code"])
        main.original_missions[MISSION_ID]["upload_status"] = "VERIFIED"

    def test_no_active_mission_record_is_a_complete_proof(self):
        main.active_original_by_vehicle.pop(SCOUT_VID, None)
        d = self.preflight()
        self.assertIs(d["can_start"], False)
        self.assertIs(d["proof_complete"], True)
        self.assertIs(d["readiness_refreshing"], False)
        main.active_original_by_vehicle[SCOUT_VID] = MISSION_ID

    def test_proof_completeness_never_changes_the_gate(self):
        # The Start transaction is fail-closed regardless: an incomplete proof blocks a write
        # just as firmly as a proven failure, and nothing here may loosen that.
        self.readiness = self._readiness(readback_reachable=False, readback_hash_match=False,
                                         readback_cached=False)
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(self.scout_posts("start"), [], "nothing may reach Scout")

    def test_the_classification_is_derived_from_evidence_not_from_blocker_text(self):
        # A blocker whose WORDING says "refreshing" while every input was read is still a
        # complete proof. The distinction is structural, never textual.
        rd = self._readiness()
        rd["planning_package"] = {**rd["planning_package"], "consistent": False,
                                  "consistency": "refreshing / unavailable / stale"}
        self.readiness = rd
        d = self.preflight()
        self.assertIs(d["can_start"], False)
        self.assertIs(d["proof_complete"], True)


# ── 9. The Start transaction is the ONLY proof that matters ──────────────────────────────
class TestStartProofIsFreshAndFirst(LifecycleTestCase):
    """The station no longer polls the preflight — the Map reads lightweight status and decides
    Start availability from stable lifecycle facts. That is only safe because THIS transaction
    proves everything itself, from a FRESH read, before it authorizes any write.

    The read-only preflight route is unchanged in meaning but is now explicitly a display: it may
    answer from the bounded read-back cache (main.PIXHAWK_READBACK_TTL_S), it authorizes nothing,
    and it is called once at a meaningful moment rather than on a refresh interval."""

    def test_start_forces_a_LIVE_readback_not_the_polling_cache(self):
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        self.assertEqual(r.status_code, 200)
        # max_readback_age_s == 0 ⇒ _pixhawk_readback pays for a live MAVLink download. A
        # ten-second-old hash is evidence about the past, and this is about to move a vehicle.
        self.assertEqual(self.readiness_reads, [0.0])

    def test_the_read_only_preflight_uses_the_bounded_cache_and_writes_nothing(self):
        r = self.client.get(f"/api/vehicles/{SCOUT_VID}/mission-execution/preflight")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.readiness_reads, [main.PIXHAWK_READBACK_TTL_S])
        self.assertEqual(self.fake.urls("POST"), [])
        self.assertEqual(self.authority_writes, [])

    def test_the_proof_runs_BEFORE_the_first_write_of_any_kind(self):
        self.set_op("start", FakeResp(op_body(), 200))
        self.post("start")
        kinds = [k for (k, _v) in self.timeline]
        self.assertIn("readiness", kinds)
        first_write = next(i for i, (k, _v) in enumerate(self.timeline)
                           if k in ("authority-write", "scout-write"))
        self.assertLess(kinds.index("readiness"), first_write,
                        "the fresh proof must precede the authority hand-off AND Scout's Start")
        # …and the first write really is the authority hand-off, not a vehicle command.
        self.assertEqual(self.timeline[first_write], ("authority-write", "LOCAL_AGENT"))

    def test_a_failed_proof_produces_NO_write_and_one_compact_actionable_error(self):
        # The transient the Map used to render: the live read-back failed, so the hash check, the
        # package consistency check and Scout's replanning readiness all report false.
        self.readiness = {
            **GREEN_READINESS,
            "replanning_ready": False,
            "vehicle_mission": {**GREEN_READINESS["vehicle_mission"],
                                "readback_reachable": False, "readback_hash_match": False,
                                "readback_cached": False},
            "planning_package": {**GREEN_READINESS["planning_package"], "consistent": False},
        }
        self.set_op("start", FakeResp(op_body(), 200))
        r = self.post("start")
        d = r.json()

        # 1. NOTHING was written. Not to Scout, not to authority.
        self.assertEqual(r.status_code, 409)
        self.assertEqual(d["outcome"], ml.OUTCOME_BLOCKED)
        self.assertEqual(self.scout_posts("start"), [])
        self.assertEqual(self.authority_writes, [])
        self.assertEqual(self.authority, "OPERATOR")
        self.assertEqual([k for (k, _v) in self.timeline if k.endswith("write")], [])

        # 2. ONE machine-readable code, with the full evidence carried alongside for the tooltip
        #    and the Agent diagnostics page — never several competing lines for the Map body.
        self.assertEqual(d["error_code"], "START_PRECONDITIONS_NOT_MET")
        self.assertTrue(any("read-back" in b for b in d["blockers"]), d["blockers"])
        self.assertGreaterEqual(len(d["blockers"]), 2,
                                "the full evidence is preserved — the station shortens it")
        # 3. …and the payload says whether the verdict was even a proof, so an unread input is
        #    never presented to the operator as a vehicle failure.
        self.assertIs(d["proof_complete"], False)
        self.assertEqual(d["readiness_reason_code"], ml.EVIDENCE_READBACK_UNAVAILABLE)

    def test_a_blocked_start_leaves_the_lifecycle_in_its_resting_state(self):
        # Fail-closed means fail-STILL: the vehicle's canonical state is exactly what it was, so
        # the card goes on offering Start rather than falling into a failed lifecycle.
        main.original_missions[MISSION_ID]["upload_status"] = "PENDING"
        self.set_op("start", FakeResp(op_body(), 200))
        self.post("start")
        after = self.client.get(
            f"/api/vehicles/{SCOUT_VID}/mission-execution/status").json()["summary"]
        self.assertEqual(after["state"], "READY")
        self.assertIsNone(after["active_operation_id"])

    def test_pause_resume_and_stop_do_not_force_a_readback_download(self):
        """Only Start gates on mission identity evidence. Making every operation pay for a live
        MAVLink download would reintroduce exactly the traffic this change removed."""
        for op in ("pause", "resume", "stop"):
            self.readiness_reads.clear()
            self.set_op(op, FakeResp(op_body(operation=op), 200))
            self.post(op)
            self.assertEqual(self.readiness_reads, [], op)

    def test_an_injected_readiness_callable_without_fresh_still_works(self):
        """Deps resolves the `fresh` keyword from the signature once, at construction. A callable
        that does not take it is called without it — never with a swallowed TypeError, which would
        answer a Start with stale evidence."""
        seen = []

        def legacy(vid, base):
            seen.append((vid, base))
            return dict(GREEN_READINESS)
        deps = ml.Deps(active_mission_id=lambda vid: MISSION_ID,
                       mission_record=lambda mid: {"upload_status": "VERIFIED"},
                       readiness=legacy, get_authority=lambda vid: {}, set_authority=lambda *a: {})
        self.assertFalse(deps._readiness_takes_fresh)
        self.assertEqual(deps.readiness_evidence(2, "base", fresh=True), dict(GREEN_READINESS))
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
