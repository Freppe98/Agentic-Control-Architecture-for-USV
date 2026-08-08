"""Scout's explicit Start-eligibility contract, and the mission/package binding, in the backend.

Run from operator-scripts/:  python -m unittest tests.test_start_eligibility

WHAT THESE PIN
--------------
Scout is the authority on whether a mission may start, and it now says so with four explicit
fields instead of a single `can_start`:

    start_eligible / execution_ready / authority_blocks_start / start_block_reason

The reading these tests exist to enforce is the one the bench run got wrong:

    start_eligible = true, authority_blocks_start = true

is a WELL-PREPARED mission waiting for the authority hand-off the Start transaction performs as
its FIRST phase — not a broken one. The Operator keeps owning the transfer (OPERATOR → release to
LOCAL_AGENT → Scout Start); it simply stops telling the operator to arrange it by hand.

Everything else stays exactly as it was: the mission record must be VERIFIED, the Pixhawk
read-back hash must match, the planning package must be stored/usable/consistent, replanning
readiness must be true, and a proven binding conflict blocks. No gate is loosened here.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import mission_lifecycle as ml  # noqa: E402
import scout_mission_execution as mx  # noqa: E402


def summary(**body):
    """A summarized Scout status, built through the REAL summarizer so these tests exercise the
    same field extraction the proxy does — never a hand-written dict that could drift from it."""
    return mx.summarize_status({"outcome": mx.OUTCOME_ACCEPTED, "supported": True,
                                "reachable": True, "scout": body})


class SummarizerTests(unittest.TestCase):
    """The new fields survive the trip through summarize_status, with PRESENCE preserved."""

    def test_the_eligibility_contract_is_carried_verbatim(self):
        s = summary(state="NOT_READY", can_start=False, start_eligible=True,
                    execution_ready=False, authority_blocks_start=True,
                    start_block_reason="AUTHORITY_NOT_LOCAL_AGENT")
        self.assertTrue(s["eligibility_reported"])
        self.assertIs(s["start_eligible"], True)
        self.assertIs(s["execution_ready"], False)
        self.assertIs(s["authority_blocks_start"], True)
        self.assertEqual(s["start_block_reason"], "AUTHORITY_NOT_LOCAL_AGENT")

    def test_an_older_scout_reports_the_contract_as_ABSENT_not_as_false(self):
        # The distinction is load-bearing: a missing field read as `false` would refuse every
        # Start on a Scout that simply predates the contract.
        s = summary(state="READY", can_start=True)
        self.assertFalse(s["eligibility_reported"])
        self.assertIsNone(s["start_eligible"])
        self.assertIsNone(s["authority_blocks_start"])

    def test_binding_and_conflict_are_carried_verbatim(self):
        s = summary(state="RUNNING",
                    binding={"binding_state": "STALE_MISMATCH",
                             "bound_original_mission_id": "msn-old",
                             "package_mission_id": "msn-new",
                             "package_route_hash": "sha256:new",
                             "verified_route_hash": "sha256:old"},
                    package_conflict={"code": "STALE_PACKAGE_DURING_ACTIVE_EXECUTION",
                                      "execution_state": "RUNNING"})
        self.assertEqual(s["binding_state"], "STALE_MISMATCH")
        self.assertEqual(s["bound_original_mission_id"], "msn-old")
        self.assertEqual(s["package_mission_id"], "msn-new")
        self.assertEqual(s["package_conflict_code"], "STALE_PACKAGE_DURING_ACTIVE_EXECUTION")

    def test_battery_diagnostics_are_carried_and_minus_one_is_never_a_percentage(self):
        s = summary(state="RUNNING", battery_diagnostics={
            "battery_percent": -1, "battery_valid": False, "battery_raw": -1,
            "telemetry_age_s": 3.0})
        self.assertIs(s["battery_valid"], False)
        self.assertEqual(s["battery_diagnostics"]["battery_raw"], -1)

    def test_a_status_carrying_only_the_new_fields_is_still_a_status(self):
        # STATUS_IDENTIFYING_FIELDS must recognise the contract, or a Scout that reports it
        # without `state` would be misread as "an older Scout answering another endpoint".
        self.assertTrue(mx.is_status_body({"start_eligible": True}))
        self.assertTrue(mx.is_status_body({"execution_ready": False}))


class StartEligibilityTests(unittest.TestCase):
    """The rule itself."""

    def test_eligible_with_authority_blocked_is_ELIGIBLE_and_DEFERRED(self):
        e = ml.start_eligibility(summary(
            state="NOT_READY", can_start=False, authority_status="OPERATOR",
            start_eligible=True, authority_blocks_start=True, execution_ready=False))
        self.assertTrue(e["eligible"])
        self.assertTrue(e["deferred_on_authority"])
        self.assertFalse(e["execution_ready"])
        self.assertEqual(e["source"], "scout")
        # The reason explains what the Start will DO, never what the operator must fix.
        self.assertIn("acquires and verifies", e["reason"])

    def test_execution_ready_is_eligible_and_not_deferred(self):
        e = ml.start_eligibility(summary(
            state="READY", authority_status="LOCAL_AGENT", start_eligible=True,
            authority_blocks_start=False, execution_ready=True))
        self.assertEqual([e["eligible"], e["deferred_on_authority"], e["execution_ready"]],
                         [True, False, True])

    def test_not_eligible_blocks_with_scouts_own_reason(self):
        e = ml.start_eligibility(summary(
            state="NOT_READY", can_start=False, start_eligible=False,
            start_block_reason="Planning package route hash does not match the loaded mission"))
        self.assertFalse(e["eligible"])
        self.assertEqual(e["reason"],
                         "Planning package route hash does not match the loaded mission")

    def test_can_start_alone_no_longer_decides_in_either_direction(self):
        # can_start TRUE but Scout says not eligible -> blocked.
        blocked = ml.start_eligibility(summary(state="READY", can_start=True,
                                               start_eligible=False,
                                               start_block_reason="no planning package"))
        self.assertFalse(blocked["eligible"])
        # can_start FALSE but Scout says eligible -> offered.
        offered = ml.start_eligibility(summary(state="NOT_READY", can_start=False,
                                               start_eligible=True))
        self.assertTrue(offered["eligible"])

    def test_an_older_scout_keeps_the_previous_can_start_reading(self):
        e = ml.start_eligibility(summary(state="READY", can_start=True))
        self.assertEqual([e["eligible"], e["source"]], [True, "can_start"])
        d = ml.start_eligibility(summary(state="NOT_READY", can_start=False,
                                         authority_status="OPERATOR"))
        self.assertEqual([d["eligible"], d["deferred_on_authority"], d["source"]],
                         [True, True, "can_start"])

    def test_the_hard_guards_still_fail_closed_on_a_contradictory_status(self):
        for extra, why in (
            ({"replanning": {"active": True}}, "replanning"),
            ({"active_operation_id": "op-1"}, "operation"),
            ({"mission_execution_enabled": False}, "disabled"),
        ):
            e = ml.start_eligibility(summary(state="READY", start_eligible=True, **extra))
            self.assertFalse(e["eligible"], why)
            self.assertEqual(e["source"], why)

    def test_an_unreadable_status_is_never_eligible(self):
        e = ml.start_eligibility({"present": False})
        self.assertFalse(e["eligible"])
        self.assertFalse(e["deferred_on_authority"])


class BindingViewTests(unittest.TestCase):

    def test_bound_blocks_nothing(self):
        v = ml.binding_view(summary(state="RUNNING", binding={"binding_state": "BOUND"}))
        self.assertTrue(v["reported"])
        self.assertFalse(v["blocks_new_mission"])

    def test_stale_mismatch_blocks_and_names_the_remedy(self):
        v = ml.binding_view(summary(state="RUNNING",
                                    binding={"binding_state": "STALE_MISMATCH"}))
        self.assertTrue(v["blocks_new_mission"])
        self.assertIn("Finish the active mission", v["message"])
        # No invented Stop: Scout does not implement one.
        self.assertNotIn("press Stop", v["message"])

    def test_an_active_execution_conflict_blocks(self):
        for code in ("STALE_PACKAGE_DURING_ACTIVE_EXECUTION", "OPERATION_IN_PROGRESS"):
            v = ml.binding_view(summary(state="RUNNING", package_conflict={"code": code}))
            self.assertTrue(v["blocks_new_mission"], code)
            self.assertEqual(v["conflict_code"], code)

    def test_a_scout_reporting_no_binding_reports_nothing(self):
        v = ml.binding_view(summary(state="READY", can_start=True))
        self.assertFalse(v["reported"])
        self.assertFalse(v["blocks_new_mission"])

    def test_scouts_own_conflict_message_is_carried_not_replaced(self):
        v = ml.binding_view(summary(state="RUNNING", package_conflict={
            "code": "OPERATION_IN_PROGRESS", "message": "start transaction op-42 is running"}))
        self.assertIn("start transaction op-42 is running", v["message"])


class StartPreconditionsTests(unittest.TestCase):
    """The five evidence gates are UNCHANGED, and the binding is added as its own check."""

    def _deps(self, *, record, readiness):
        return ml.Deps(
            active_mission_id=lambda vid: "msn-1",
            mission_record=lambda mid: record,
            readiness=lambda vid, base, fresh=False: readiness,
            get_authority=lambda vid: {"authority": "OPERATOR", "reachable": True},
            set_authority=lambda vid, value: {"ok": True, "authority": value})

    RECORD = {"mission_id": "msn-1", "upload_status": "VERIFIED"}
    GOOD_READINESS = {
        "vehicle_mission": {"readback_hash_match": True, "readback_reachable": True},
        "planning_package": {"stored": True, "usable": True, "consistent": True,
                             "scout_reachable": True},
        "replanning_ready": True,
    }

    def test_an_eligible_mission_with_authority_still_OPERATOR_passes_every_check(self):
        s = summary(state="NOT_READY", can_start=False, authority_status="OPERATOR",
                    start_eligible=True, authority_blocks_start=True)
        pre = ml.start_preconditions(self._deps(record=self.RECORD, readiness=self.GOOD_READINESS),
                                     2, "http://scout", mission_id="msn-1", summary=s)
        self.assertTrue(pre["ok"], pre["blockers"])
        check = next(c for c in pre["checks"] if c["key"] == "start_eligibility")
        self.assertTrue(check["ok"])
        self.assertTrue(check["deferred_on_authority"])

    def test_a_hash_mismatch_still_blocks_an_eligible_mission(self):
        rd = dict(self.GOOD_READINESS,
                  vehicle_mission={"readback_hash_match": False, "readback_reachable": True})
        s = summary(state="READY", start_eligible=True, execution_ready=True)
        pre = ml.start_preconditions(self._deps(record=self.RECORD, readiness=rd),
                                     2, "http://scout", mission_id="msn-1", summary=s)
        self.assertFalse(pre["ok"])
        self.assertTrue(any("read-back hash does not match" in b for b in pre["blockers"]))

    def test_a_stale_unreachable_readback_still_blocks(self):
        rd = dict(self.GOOD_READINESS,
                  vehicle_mission={"readback_hash_match": False, "readback_reachable": False})
        s = summary(state="READY", start_eligible=True)
        pre = ml.start_preconditions(self._deps(record=self.RECORD, readiness=rd),
                                     2, "http://scout", mission_id="msn-1", summary=s)
        self.assertFalse(pre["ok"])
        self.assertTrue(any("unreachable" in b for b in pre["blockers"]))

    def test_an_unverified_mission_record_still_blocks(self):
        s = summary(state="READY", start_eligible=True)
        pre = ml.start_preconditions(
            self._deps(record={"mission_id": "msn-1", "upload_status": "QUEUED"},
                       readiness=self.GOOD_READINESS),
            2, "http://scout", mission_id="msn-1", summary=s)
        self.assertFalse(pre["ok"])
        self.assertTrue(any("not VERIFIED" in b for b in pre["blockers"]))

    def test_an_absent_planning_package_still_blocks(self):
        rd = dict(self.GOOD_READINESS,
                  planning_package={"stored": False, "usable": False, "consistent": False,
                                    "scout_reachable": True})
        s = summary(state="READY", start_eligible=True)
        pre = ml.start_preconditions(self._deps(record=self.RECORD, readiness=rd),
                                     2, "http://scout", mission_id="msn-1", summary=s)
        self.assertFalse(pre["ok"])

    def test_a_package_conflict_is_its_own_named_check(self):
        s = summary(state="RUNNING", start_eligible=True,
                    package_conflict={"code": "STALE_PACKAGE_DURING_ACTIVE_EXECUTION"})
        pre = ml.start_preconditions(self._deps(record=self.RECORD, readiness=self.GOOD_READINESS),
                                     2, "http://scout", mission_id="msn-1", summary=s)
        self.assertFalse(pre["ok"])
        check = next(c for c in pre["checks"] if c["key"] == "mission_binding")
        self.assertFalse(check["ok"])
        self.assertIn("another mission is active", check["detail"].lower())

    def test_home_absent_before_start_is_not_a_precondition_failure(self):
        # UNCHANGED behaviour, asserted so the new eligibility rule cannot have moved it: the
        # Start transaction sets and verifies Home as one of its own phases.
        s = summary(state="NOT_READY", start_eligible=True, authority_blocks_start=True)
        pre = ml.start_preconditions(self._deps(record=self.RECORD, readiness=self.GOOD_READINESS),
                                     2, "http://scout", mission_id="msn-1", summary=s)
        self.assertTrue(pre["ok"], pre["blockers"])


class PreflightSurfaceTests(unittest.TestCase):
    """The read-only preflight reports the two facts the Map card needs, at the top level."""

    def _deps(self, summary_body):
        return ml.Deps(
            active_mission_id=lambda vid: "msn-1",
            mission_record=lambda mid: {"mission_id": "msn-1", "upload_status": "VERIFIED"},
            readiness=lambda vid, base, fresh=False: StartPreconditionsTests.GOOD_READINESS,
            get_authority=lambda vid: {"authority": "OPERATOR", "reachable": True},
            set_authority=lambda vid, value: {"ok": True, "authority": value})

    def test_preflight_reports_that_start_will_acquire_authority(self):
        body = {"state": "NOT_READY", "can_start": False, "authority_status": "OPERATOR",
                "start_eligible": True, "authority_blocks_start": True}
        real_get = mx.get_status
        mx.get_status = lambda base: {"outcome": mx.OUTCOME_ACCEPTED, "supported": True,
                                      "reachable": True, "scout": body}
        try:
            out = ml.preflight(self._deps(body), 2, "http://scout")
        finally:
            mx.get_status = real_get
        self.assertTrue(out["can_start"], out["blockers"])
        self.assertTrue(out["authority_will_be_acquired"])
        self.assertFalse(out["execution_ready"])
        self.assertIn("binding", out)

    def test_preflight_without_an_active_mission_still_returns_the_full_shape(self):
        real_get = mx.get_status
        mx.get_status = lambda base: {"outcome": mx.OUTCOME_ACCEPTED, "supported": True,
                                      "reachable": True, "scout": {"state": "READY"}}
        deps = ml.Deps(
            active_mission_id=lambda vid: None,
            mission_record=lambda mid: None,
            readiness=lambda vid, base, fresh=False: {},
            get_authority=lambda vid: {"authority": "OPERATOR"},
            set_authority=lambda vid, value: {"ok": True, "authority": value})
        try:
            out = ml.preflight(deps, 2, "http://scout")
        finally:
            mx.get_status = real_get
        self.assertFalse(out["can_start"])
        self.assertEqual(out["error_code"], "NO_ACTIVE_MISSION")
        for key in ("binding", "authority_will_be_acquired", "execution_ready"):
            self.assertIn(key, out)


if __name__ == "__main__":
    unittest.main()
