"""
Focused tests for MissionExecutionController.reprove_binding() -- the
read-only, on-demand binding-reproof operation backing the Operator "Full
Refresh" button (task: Scout-side binding-reproof gap).

Regression scenario (the central case this exists to fix): after a restart,
the stored planning package and a fresh Pixhawk mission readback can already
agree on the same route hash while mission-execution's cached readiness
evidence (verified_route_hash / mission_id) is still unpopulated, because the
passive background readiness proof only ever runs while idle (NOT_READY/
READY) and is not itself synchronous/on-demand. reprove_binding() exposes that
SAME proof (_resolve_start_prerequisites -- the identical identity rule Start
uses) synchronously, without ever uploading, clearing, or writing a mission,
touching Home, changing mode, or arming/disarming.

    python3 test_mission_execution_reprove.py
"""
import os
import tempfile
import unittest

import mission_execution_config as me_cfg
import mission_execution_controller as mec
import mission_feasibility as mf
import planning_package as pp
import replan_config
import write_arbiter
from test_mission_execution_controller import (
    FakeGateway, _Base, _cfg, _store_verified_package)


class TestReproveCentralRegression(_Base):
    """Section 20: the exact reported regression -- package/Pixhawk already
    agree (mission_id m1, route hash H) but the controller's readiness cache
    is unpopulated (as it is on a fresh process before the first passive
    refresh / observe() has run), so verified_route_hash is null and
    mission_feasibility reads MISSION_ROUTE_UNVERIFIED. A single reprove
    restores it -- no restart, no new mission upload."""

    def test_reprove_restores_verified_route_hash_and_clears_unverified(self):
        ctrl = self._ctrl()
        # Simulate the post-restart gap directly: nothing has proven readiness
        # yet in this fresh process.
        st_before = ctrl.status()
        self.assertIsNone(st_before["binding"]["verified_route_hash"])
        self.assertEqual(st_before["binding"]["binding_state"], "UNBOUND")

        package = pp.load()
        snap = self._snapshot(mode="LOITER")
        feas_before = mf.evaluate_from_snapshot(
            snap, package, None, replan_config.DEFAULT,
            mission_binding=st_before["binding"])
        self.assertEqual(feas_before.reason, mf.REASON_MISSION_ROUTE_UNVERIFIED)
        self.assertIsNone(feas_before.mission_feasible)

        result = ctrl.reprove_binding()

        self.assertTrue(result["accepted"])
        self.assertEqual(result["outcome"], "REPROVED")
        self.assertTrue(result["ok"])
        self.assertTrue(result["read_only"])
        self.assertEqual(result["verified_route_hash"], self.route_hash)
        self.assertEqual(result["mission_id"], "m1")
        self.assertTrue(result["start_eligible"])
        self.assertTrue(result["can_start"])
        self.assertIsNone(result["start_block_reason"])
        # binding_state is the literal FSM concept (BOUND == a LIVE execution
        # holds this mission_id) -- it legitimately stays UNBOUND before a
        # real Start; what reprove restores is the READINESS EVIDENCE that a
        # Start (and therefore a real BOUND) depends on. See reprove_binding's
        # docstring.
        self.assertEqual(result["binding_state"], "UNBOUND")

        st_after = ctrl.status()
        self.assertEqual(st_after["binding"]["verified_route_hash"], self.route_hash)
        self.assertEqual(st_after["state"], mec.READY)

        feas_after = mf.evaluate_from_snapshot(
            snap, package, None, replan_config.DEFAULT,
            mission_binding=st_after["binding"])
        self.assertTrue(feas_after.route_identity_verified)
        self.assertIsNotNone(feas_after.mission_feasible)
        self.assertIsNotNone(feas_after.planned_completion_distance_m)

        # No vehicle write of any kind.
        self.assertEqual(self.gw.write_calls, [])
        self.assertNotIn("set_home", self.gw.calls)
        self.assertNotIn("arm", self.gw.calls)
        self.assertNotIn("loiter", self.gw.calls)
        self.assertNotIn("auto", self.gw.calls)
        self.assertNotIn("upload", self.gw.calls)
        self.assertNotIn("set_current", self.gw.calls)


class TestReprovePackageMismatch(_Base):
    """Sections 21/22: the stored package's route hash disagrees with the
    fresh Pixhawk readback. Scout has exactly ONE package-vs-Pixhawk hash
    comparison (the same one Start uses) -- there is no independent
    third "approved" reference to tell package-is-wrong apart from
    Pixhawk-changed, so both directions of a genuine hash disagreement are
    reported the same way: PACKAGE_MISMATCH (the existing
    PLANNING_PACKAGE_STALE evidence state), whose remedy is the Operator's
    separate explicit package-sync action -- reprove never rewrites the
    package itself."""

    def test_hash_disagreement_reports_package_mismatch_and_stays_unbound(self):
        self.gw.pixhawk_route_hash = "sha256:" + "ab" * 32  # != stored package hash
        ctrl = self._ctrl()

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "PACKAGE_MISMATCH")
        self.assertFalse(result["ok"])
        self.assertIsNone(ctrl.status()["binding"]["verified_route_hash"])
        self.assertFalse(ctrl.status()["can_start"])
        self.assertEqual(self.gw.write_calls, [])
        # The stored package itself is never rewritten by reprove.
        self.assertEqual(pp.load().get("original_route_hash"), self.route_hash)


class TestReprovePixhawkMismatch(_Base):
    """A non-hash Pixhawk-side evidence conflict (the vehicle's own reported
    current_mission_id disagrees with the resolved identity) while the route
    hash itself matches -- classified separately from a package/route hash
    disagreement."""

    def test_vehicle_mission_id_conflict_reports_pixhawk_mismatch(self):
        ctrl = self._ctrl()
        self.gw.mission_id = "some-other-mission"  # vehicle_state.mission.current_mission_id

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "PIXHAWK_MISMATCH")
        self.assertFalse(result["ok"])
        self.assertEqual(self.gw.write_calls, [])
        # Diagnostic accuracy fix: the fresh, matching Pixhawk route hash WAS
        # obtained (only the mission-id axis disagreed) -- it must be
        # reported here, never null, even on this failure path.
        self.assertEqual(result["pixhawk_route_hash"], self.route_hash)


class TestReproveVehicleMissionLabelNamespace(_Base):
    """Mission binding/reproof identity bug regression: Flask's legacy
    `/start_mission` operator-typed sensor-logging label
    ("<YYYY-MM-DD_HH-MM>_<free-text name>", set via a wholly separate
    InfluxDB-tagging feature) is a DIFFERENT identifier namespace from this
    controller's canonical msn-* planning-package mission identity -- the
    Pixhawk/MAVLink mission carries no such id at all. It must never be
    compared byte-for-byte against the resolved package identity. Reproduces
    the exact reported observed state: package msn-183d11e892ff, vehicle
    current_mission_id "2026-08-20_11-54_biltema 1", identical route hash."""

    _CANONICAL_MISSION_ID = "msn-183d11e892ff"
    _LEGACY_OPERATOR_LABEL = "2026-08-20_11-54_biltema 1"

    def _canonical_ctrl(self):
        self.route_hash = _store_verified_package(self._CANONICAL_MISSION_ID)
        self.gw.pixhawk_route_hash = self.route_hash
        self.gw.mission_id = self._LEGACY_OPERATOR_LABEL
        return self._ctrl()

    # (a) same approved route hash + human-readable vehicle mission ID +
    # canonical msn-* package ID -> safely binds/reproves.
    def test_legacy_operator_label_with_matching_hash_reproves_and_binds(self):
        ctrl = self._canonical_ctrl()

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "REPROVED")
        self.assertTrue(result["ok"])
        self.assertEqual(result["verified_route_hash"], self.route_hash)
        self.assertEqual(result["mission_id"], self._CANONICAL_MISSION_ID)
        self.assertTrue(result["start_eligible"])
        self.assertTrue(result["can_start"])
        self.assertIsNone(result["start_block_reason"])
        self.assertEqual(self.gw.write_calls, [])

        # A real Start then actually binds -- no re-upload, no package
        # rewrite required to get there.
        start_res = ctrl.start(self._CANONICAL_MISSION_ID)
        self.assertEqual(start_res["outcome"], mec.RUNNING)
        st = ctrl.status()
        self.assertEqual(st["binding"]["binding_state"], "BOUND")
        self.assertEqual(st["binding"]["bound_original_mission_id"], self._CANONICAL_MISSION_ID)

    # (b) different route hash -> remains blocked even with the legacy label
    # present (the namespace fix must not mask a genuine hash disagreement).
    def test_legacy_operator_label_does_not_mask_real_hash_mismatch(self):
        ctrl = self._canonical_ctrl()
        self.gw.pixhawk_route_hash = "sha256:" + "ab" * 32  # genuinely different

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "PACKAGE_MISMATCH")
        self.assertFalse(result["ok"])
        self.assertIsNone(ctrl.status()["binding"]["verified_route_hash"])
        self.assertFalse(ctrl.status()["can_start"])
        self.assertEqual(self.gw.write_calls, [])

    # (c) genuinely conflicting CANONICAL mission identities -> remains
    # blocked. The fix is a namespace exclusion, not a blanket "ignore
    # mission-id mismatches".
    def test_conflicting_canonical_vehicle_mission_id_remains_blocked(self):
        ctrl = self._canonical_ctrl()
        self.gw.mission_id = "msn-000000000000"  # a DIFFERENT canonical id: comparable, genuine conflict

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "PIXHAWK_MISMATCH")
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason_code"], "MISSION_ID_CONFLICT")
        self.assertIsNone(ctrl.status()["binding"]["verified_route_hash"])
        self.assertFalse(ctrl.status()["can_start"])
        self.assertEqual(self.gw.write_calls, [])

    # (d) stale/unavailable readback -> remains blocked even with a legacy
    # label present.
    def test_legacy_operator_label_with_unreachable_pixhawk_remains_blocked(self):
        ctrl = self._canonical_ctrl()
        self.gw.pixhawk_reachable = False

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "EVIDENCE_UNAVAILABLE")
        self.assertFalse(result["ok"])
        self.assertIsNone(ctrl.status()["binding"]["verified_route_hash"])
        self.assertEqual(self.gw.write_calls, [])

    # (e) restart/reproof against the EXISTING approved package -- no
    # re-upload required. Mirrors TestReproveCentralRegression using the
    # exact reported identifiers.
    def test_restart_reproof_binds_existing_package_without_reupload(self):
        ctrl = self._canonical_ctrl()
        st_before = ctrl.status()
        self.assertIsNone(st_before["binding"]["verified_route_hash"])
        self.assertEqual(st_before["binding"]["binding_state"], "UNBOUND")

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "REPROVED")
        self.assertTrue(result["ok"])
        # No package upload/rewrite of any kind -- the SAME stored package is
        # what got proven.
        self.assertEqual(self.gw.write_calls, [])
        self.assertNotIn("upload", self.gw.calls)
        self.assertEqual(pp.load().get("mission_id"), self._CANONICAL_MISSION_ID)

    # (f) diagnostics report the actual route hash/evidence when available --
    # the exact reported bug (`pixhawk_route_hash: null` alongside
    # PIXHAWK_MISMATCH / MISSION_ID_CONFLICT) covered directly.
    def test_pixhawk_mismatch_diagnostic_reports_actual_route_hash(self):
        ctrl = self._canonical_ctrl()
        self.gw.mission_id = "msn-000000000000"

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "PIXHAWK_MISMATCH")
        self.assertEqual(result["reason_code"], "MISSION_ID_CONFLICT")
        self.assertIsNotNone(result["pixhawk_route_hash"])
        self.assertEqual(result["pixhawk_route_hash"], self.route_hash)


class TestReproveEvidenceUnavailable(_Base):
    """Section 23: a Pixhawk readback timeout/unreachable is a distinct,
    non-mismatch outcome -- missing evidence must never collapse into a
    definitive mismatch, and must never leave the vehicle written to."""

    def test_pixhawk_unreachable_reports_evidence_unavailable(self):
        ctrl = self._ctrl()
        self.gw.pixhawk_reachable = False

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "EVIDENCE_UNAVAILABLE")
        self.assertFalse(result["ok"])
        self.assertIsNone(ctrl.status()["binding"]["verified_route_hash"])
        self.assertEqual(self.gw.write_calls, [])

    def test_pixhawk_timeout_reports_evidence_unavailable_not_mismatch(self):
        ctrl = self._ctrl()
        self.gw.readback_error = [TimeoutError("Pixhawk readback timed out")] * 2

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "EVIDENCE_UNAVAILABLE")
        self.assertEqual(self.gw.write_calls, [])


class TestReproveMissionIdMismatch(_Base):
    """Section 13/24/29: the Operator-supplied expected mission_id is a
    CONSTRAINT, never proof -- a wrong expectation must fail the response
    closed WITHOUT corrupting Scout's own, independently-proven, genuinely
    healthy internal readiness evidence for the mission that actually is
    current."""

    def test_wrong_expected_mission_id_fails_closed_without_corrupting_state(self):
        ctrl = self._ctrl()

        result = ctrl.reprove_binding(expected_mission_id="not-the-real-one")

        self.assertEqual(result["outcome"], "MISSION_ID_MISMATCH")
        self.assertFalse(result["ok"])
        self.assertEqual(result["expected_mission_id"], "not-the-real-one")
        # Internal evidence reflects the REAL current mission -- untouched by
        # the caller's mistaken expectation.
        st = ctrl.status()
        self.assertEqual(st["binding"]["verified_route_hash"], self.route_hash)
        self.assertEqual(st["mission_id"], "m1")
        self.assertTrue(st["can_start"])
        self.assertEqual(self.gw.write_calls, [])

    def test_matching_expected_mission_id_succeeds(self):
        ctrl = self._ctrl()
        result = ctrl.reprove_binding(expected_mission_id="m1")
        self.assertEqual(result["outcome"], "REPROVED")
        self.assertEqual(result["expected_mission_id"], "m1")


class TestReproveIdempotence(_Base):
    """Section 14/25: repeated calls with unchanged evidence are safe --
    ALREADY_PROVEN on the second call, same binding, no writes."""

    def test_second_call_is_already_proven(self):
        ctrl = self._ctrl()
        first = ctrl.reprove_binding()
        self.assertEqual(first["outcome"], "REPROVED")

        second = ctrl.reprove_binding()
        self.assertEqual(second["outcome"], "ALREADY_PROVEN")
        self.assertTrue(second["ok"])
        self.assertEqual(second["verified_route_hash"], first["verified_route_hash"])
        self.assertEqual(self.gw.write_calls, [])


class TestReproveRunningMission(_Base):
    """Sections 10/25/26: a live original mission is already bound
    (RUNNING). reprove never re-runs the identity proof against a live
    execution -- it reports the existing binding read-only, idempotently, and
    never resets sequence, mode, or replanning state."""

    def _running_ctrl(self):
        ctrl = self._ctrl()
        res = ctrl.start("m1")
        self.assertEqual(res["outcome"], mec.RUNNING)
        return ctrl

    def test_running_mission_reports_already_proven_no_writes(self):
        ctrl = self._running_ctrl()
        writes_before = list(self.gw.write_calls)
        seq_before = ctrl.status()["sequence"]

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "ALREADY_PROVEN")
        self.assertEqual(result["binding_state"], "BOUND")
        self.assertEqual(result["bound_original_mission_id"], "m1")
        self.assertEqual(ctrl.status()["state"], mec.RUNNING)
        self.assertEqual(ctrl.status()["sequence"], seq_before)
        # No NEW vehicle writes issued by reprove itself.
        self.assertEqual(self.gw.write_calls, writes_before)

    def test_running_mission_with_active_route_diverged_from_original_stays_bound(self):
        # Simulate a post-replan-handoff state: the ORIGINAL hash stays what
        # was bound at Start, while the ACTIVE (revised) hash has since
        # diverged -- reprove must read the ORIGINAL binding, never compare
        # the revised route and declare a mismatch, and never touch either.
        ctrl = self._running_ctrl()
        original_before = ctrl.status()["binding"]["bound_original_route_hash"]
        with ctrl._state_lock:
            ctrl._active_route_hash = "sha256:" + "ff" * 32  # revised route, diverged
        writes_before = list(self.gw.write_calls)

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "ALREADY_PROVEN")
        self.assertEqual(result["binding_state"], "BOUND")
        st = ctrl.status()
        self.assertEqual(st["binding"]["bound_original_route_hash"], original_before)
        self.assertEqual(st["active_route_hash"], "sha256:" + "ff" * 32)
        self.assertEqual(self.gw.write_calls, writes_before)  # unchanged by reprove


class TestReproveRearmableStates(_Base):
    """Sections 10/26: SUSPENDED/FAILED/COMPLETED_HOLD/RECOVERY_PENDING all
    require an explicit rearm() (the existing, deliberate unbind step) before
    identity can be safely re-proved. reprove never performs that unbind
    itself -- it fails closed with LIFECYCLE_NOT_REPROVABLE, read-only."""

    def test_suspended_state_is_not_reprovable(self):
        ctrl = self._ctrl()
        # Force a definitive Start failure that lands in SUSPENDED: authority
        # not LOCAL_AGENT is evidence-proven-but-authority-blocked, not
        # SUSPENDED -- use a hard failure instead (loiter not verified during
        # Start drives a fail-closed SUSPENDED/FAILED outcome).
        self.gw.loiter_verified = False
        ctrl.start("m1")
        st = ctrl.status()
        self.assertIn(st["state"], (mec.SUSPENDED, mec.FAILED))

        writes_before = list(self.gw.write_calls)
        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "LIFECYCLE_NOT_REPROVABLE")
        self.assertFalse(result["ok"])
        self.assertEqual(ctrl.status()["state"], st["state"])  # unchanged
        self.assertEqual(self.gw.write_calls, writes_before)  # no new writes


class TestReproveBusy(_Base):
    """Section 15: a concurrent lifecycle operation (or an in-flight readiness
    refresh) yields BUSY rather than blocking or racing."""

    def test_action_lock_held_reports_busy(self):
        ctrl = self._ctrl()
        self.assertTrue(ctrl._action_lock.acquire(blocking=False))
        try:
            result = ctrl.reprove_binding()
            self.assertEqual(result["outcome"], "BUSY")
            self.assertFalse(result["accepted"])
        finally:
            ctrl._action_lock.release()

    def test_readiness_refresh_inflight_reports_busy(self):
        ctrl = self._ctrl()
        with ctrl._state_lock:
            ctrl._readiness_refresh_inflight = True
        try:
            result = ctrl.reprove_binding()
            self.assertEqual(result["outcome"], "BUSY")
        finally:
            with ctrl._state_lock:
                ctrl._readiness_refresh_inflight = False


class TestReproveNoPackage(_Base):
    """No usable stored package at all -- distinct from a mismatch."""

    def test_no_package_reports_no_current_package(self):
        write_arbiter._reset_for_tests()
        self.dir = tempfile.mkdtemp()
        pp._reset_for_tests(os.path.join(self.dir, "empty_pkg.json"))
        ctrl = self._ctrl()

        result = ctrl.reprove_binding()

        self.assertEqual(result["outcome"], "NO_CURRENT_PACKAGE")
        self.assertFalse(result["ok"])
        self.assertEqual(self.gw.write_calls, [])


class TestReproveNoWriteExhaustive(_Base):
    """Section 28: explicit no-write proof across every outcome branch this
    module can drive -- assert zero calls to any vehicle-affecting gateway
    method, in every scenario, not just the happy path."""

    _WRITE_METHODS = ("loiter", "auto", "set_home", "arm", "set_current", "upload", "set_authority")

    def _assert_no_writes(self):
        for name in self._WRITE_METHODS:
            self.assertNotIn(name, self.gw.calls, f"reprove_binding must never call gateway.{name}")

    def test_no_writes_on_success(self):
        ctrl = self._ctrl()
        ctrl.reprove_binding()
        self._assert_no_writes()

    def test_no_writes_on_package_mismatch(self):
        self.gw.pixhawk_route_hash = "sha256:" + "cd" * 32
        ctrl = self._ctrl()
        ctrl.reprove_binding()
        self._assert_no_writes()

    def test_no_writes_on_evidence_unavailable(self):
        self.gw.pixhawk_reachable = False
        ctrl = self._ctrl()
        ctrl.reprove_binding()
        self._assert_no_writes()

    def test_no_writes_when_busy(self):
        ctrl = self._ctrl()
        ctrl._action_lock.acquire(blocking=False)
        try:
            ctrl.reprove_binding()
        finally:
            ctrl._action_lock.release()
        self._assert_no_writes()


if __name__ == "__main__":
    unittest.main(verbosity=2)
