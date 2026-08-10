"""Backend tests for the Agent Mission FULL REFRESH transaction (mission_full_refresh.py).

Run from operator-scripts/:  python -m unittest tests.test_mission_full_refresh   (no pytest).

These are UNIT tests against `mission_full_refresh.run_full_refresh` with a hand-built `Deps` —
no FastAPI, no real Scout transport, no real mission_lifecycle. That keeps them fast and focused
on THIS module's own responsibilities: stage sequencing, the three-way reconciliation vocabulary,
the read-only binding-reproof contract (never fabricating BOUND), energy/risk pass-through, and
single-flight locking. End-to-end wiring through main.py + a fake Scout HTTP transport (the
central UNBOUND regression, package/Pixhawk mismatch, concurrency, zero-vehicle-writes) lives in
tests/test_full_refresh_integration.py.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import mission_full_refresh as fr  # noqa: E402
import scout_replan  # noqa: E402

MISSION_ID = "msn-1"
HASH_A = "sha256:aaaaaaaa"
HASH_B = "sha256:bbbbbbbb"


def approved_record(*, route_hash=HASH_A, count=5, **over):
    rec = {
        "route_waypoints": [{}] * count, "route_hash": route_hash,
        "created_at": "2026-08-01T00:00:00+00:00", "verified_at": "2026-08-01T00:00:05+00:00",
        "upload_status": "VERIFIED", "package_sync_state": "SYNCED",
        "package_sync_error": None, "package_synced_at": "2026-08-01T00:00:06+00:00",
    }
    rec.update(over)
    return rec


def preflight_result(*, can_start=True, proof_complete=True, mission_id=MISSION_ID,
                      pixhawk_hash=HASH_A, pixhawk_reachable=True, pixhawk_partial=False,
                      package_hash=HASH_A, package_reachable=True, package_consistent=True,
                      binding_state="UNBOUND", verified_route_hash=HASH_A, blockers=None):
    readiness = {
        "vehicle_mission": {
            "readback_hash": pixhawk_hash, "readback_reachable": pixhawk_reachable,
            "readback_partial": pixhawk_partial, "readback_age_s": 0.0, "readback_cached": False,
            "readback_route_count": 5, "readback_current_seq": 0,
        },
        "planning_package": {
            "mission_id": mission_id, "route_hash": package_hash, "route_count": 5,
            "stored": True, "usable": True, "consistent": package_consistent,
            "scout_reachable": package_reachable,
        },
        "reconciliation": {"outcome": "SYNCHRONIZED", "reason": "ALREADY_CONSISTENT"},
    }
    return {
        "ok": can_start, "mission_id": mission_id, "can_start": can_start,
        "checks": [], "blockers": blockers if blockers is not None else ([] if can_start else ["x"]),
        "readiness": readiness,
        "binding": {"state": binding_state, "verified_route_hash": verified_route_hash,
                    "bound_original_mission_id": mission_id, "conflict_code": None,
                    "blocks_new_mission": False, "reported": True},
        "proof_complete": proof_complete, "readiness_refreshing": False,
        "readiness_reason_code": None, "readiness_reason": None,
        "summary": {"binding_state": binding_state, "present": True},
        "authority": {"authority": "OPERATOR"},
    }


def reprove_body(scout_outcome, *, http_status=200):
    """A raw transport result carrying Scout's own reprove-outcome WORD in the body — the shape
    `deps.reprove()` returns and `scout_mission_execution.interpret_reprove_binding` narrows."""
    return {"outcome": scout_replan.OUTCOME_ACCEPTED, "http_status": http_status,
            "scout": {"accepted": True, "outcome": scout_outcome}}


class FakeDeps:
    """Records every call so a test can prove ORDER (reprove before the fresh proof) and prove
    the ONLY write attempted is the speculative reprove — nothing else in this Deps interface
    can write anything, so recording calls is a complete audit."""

    def __init__(self, *, mission_record=None, preflight=None, reprove=None,
                 replan_body=None, home=None, evidence=None):
        self.calls = []
        self._mission_record = mission_record
        self._preflight = preflight or preflight_result()
        self._reprove = reprove or {"outcome": scout_replan.OUTCOME_UNSUPPORTED}
        self._replan_body = replan_body if replan_body is not None else {}
        self._home = home if home is not None else {"verified": True, "reason": None}
        self._evidence = evidence

    def active_mission_id(self, vid):
        return MISSION_ID if self._mission_record else None

    def mission_record(self, mid):
        return self._mission_record

    def run_preflight(self, vid, base, *, fresh):
        self.calls.append(("preflight", vid, base, fresh))
        return self._preflight

    def reprove(self, base, mission_id):
        self.calls.append(("reprove", base, mission_id))
        return self._reprove

    def replan_status(self, base):
        self.calls.append(("replan_status", base))
        return {"scout": self._replan_body}

    def home_view(self, vid):
        self.calls.append(("home", vid))
        return self._home

    def agent_state(self, vid, flask_base):
        self.calls.append(("agent_state", vid, flask_base))
        return self._evidence

    def record_operation(self, result):
        self.calls.append(("record", result["operation_id"]))

    def as_deps(self):
        return fr.Deps(
            active_mission_id=self.active_mission_id, mission_record=self.mission_record,
            run_preflight=self.run_preflight, reprove=self.reprove,
            replan_status=self.replan_status, home_view=self.home_view,
            agent_state=self.agent_state, record_operation=self.record_operation)


def run(deps, *, vid=2, base="http://x:8090", flask_base="http://x:8080", slug="usv-2"):
    return fr.run_full_refresh(deps.as_deps(), vid, base, flask_base, slug)


class ClassifyReconciliationTests(unittest.TestCase):
    """The task's Section 10 three-way vocabulary — a pure function, tested exhaustively."""

    def test_matched_when_all_three_hashes_agree(self):
        outcome, _ = fr.classify_reconciliation(
            approved_hash=HASH_A, pixhawk_usable=True, pixhawk_hash=HASH_A,
            package_reachable=True, package_hash=HASH_A, package_valid=True)
        self.assertEqual(outcome, fr.MATCHED)

    def test_missing_approved_hash_is_evidence_unavailable_not_mismatch(self):
        outcome, _ = fr.classify_reconciliation(
            approved_hash=None, pixhawk_usable=True, pixhawk_hash=HASH_A,
            package_reachable=True, package_hash=HASH_A, package_valid=True)
        self.assertEqual(outcome, fr.EVIDENCE_UNAVAILABLE)

    def test_unusable_pixhawk_readback_is_evidence_unavailable_not_mismatch(self):
        # Section 25: a transient read failure must never read as a definitive mismatch.
        outcome, _ = fr.classify_reconciliation(
            approved_hash=HASH_A, pixhawk_usable=False, pixhawk_hash=None,
            package_reachable=True, package_hash=HASH_A, package_valid=True)
        self.assertEqual(outcome, fr.EVIDENCE_UNAVAILABLE)

    def test_pixhawk_hash_disagreement_is_a_definite_mismatch(self):
        outcome, _ = fr.classify_reconciliation(
            approved_hash=HASH_A, pixhawk_usable=True, pixhawk_hash=HASH_B,
            package_reachable=True, package_hash=HASH_A, package_valid=True)
        self.assertEqual(outcome, fr.PIXHAWK_MISMATCH)

    def test_unreachable_package_is_evidence_unavailable(self):
        outcome, _ = fr.classify_reconciliation(
            approved_hash=HASH_A, pixhawk_usable=True, pixhawk_hash=HASH_A,
            package_reachable=False, package_hash=None, package_valid=None)
        self.assertEqual(outcome, fr.EVIDENCE_UNAVAILABLE)

    def test_package_reported_invalid_is_package_invalid(self):
        outcome, _ = fr.classify_reconciliation(
            approved_hash=HASH_A, pixhawk_usable=True, pixhawk_hash=HASH_A,
            package_reachable=True, package_hash=HASH_A, package_valid=False)
        self.assertEqual(outcome, fr.PACKAGE_INVALID)

    def test_package_hash_disagreement_is_sync_required_not_mismatch(self):
        outcome, _ = fr.classify_reconciliation(
            approved_hash=HASH_A, pixhawk_usable=True, pixhawk_hash=HASH_A,
            package_reachable=True, package_hash=HASH_B, package_valid=True)
        self.assertEqual(outcome, fr.PACKAGE_SYNC_REQUIRED)


class RunFullRefreshTests(unittest.TestCase):
    def test_missing_approved_mission_fails_closed_without_touching_scout(self):
        deps = FakeDeps(mission_record=None)
        out = run(deps)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_code"], "NO_ACTIVE_MISSION")
        self.assertEqual(out["stages"][-1]["stage"], fr.STAGE_FAILED)
        self.assertEqual([s["stage"] for s in out["stages"]],
                          [fr.STAGE_STARTING, fr.STAGE_READING_APPROVED_MISSION, fr.STAGE_FAILED])
        # NOTHING was contacted — no reprove, no preflight, no reads of any kind.
        self.assertEqual(deps.calls, [])

    def test_healthy_idle_mission_reaches_matched_and_ready_with_binding_unbound(self):
        # THE CORRECTED CENTRAL REGRESSION (task Sections 1, 17): approved, Pixhawk and package
        # all carry H, Scout REPROVES the route (outcome REPROVED), and the mission is IDLE — so
        # Scout correctly reports binding UNBOUND. THIS IS THE HEALTHY END STATE, not a failure:
        # BOUND means a LIVE execution owns the mission identity, which is not true before Start.
        deps = FakeDeps(mission_record=approved_record(),
                        reprove=reprove_body("REPROVED"),
                        preflight=preflight_result(binding_state="UNBOUND"))
        out = run(deps)
        self.assertTrue(out["ok"])
        self.assertEqual(out["mission"]["reconciliation"], fr.MATCHED)
        self.assertEqual(out["binding"]["binding_state"], "UNBOUND")
        self.assertEqual(out["binding"]["reproof_outcome"], "REPROVED")
        self.assertTrue(out["binding"]["reproof_success"])
        self.assertFalse(out["binding"]["reproof_inconclusive"])
        self.assertFalse(out["binding"]["reproof_fail_closed"])
        self.assertTrue(out["binding"]["reproof_supported"])
        self.assertIsNotNone(out["binding"]["verified_route_hash"])
        self.assertEqual(out["readiness"]["can_start"], True)
        self.assertEqual(out["stages"][-1]["stage"], fr.STAGE_COMPLETE)
        stage_names = [s["stage"] for s in out["stages"]]
        self.assertEqual(stage_names, [
            fr.STAGE_STARTING, fr.STAGE_READING_APPROVED_MISSION, fr.STAGE_READING_PIXHAWK_MISSION,
            fr.STAGE_READING_PLANNING_PACKAGE, fr.STAGE_RECONCILING_MISSION,
            fr.STAGE_REPROVING_AGENT_BINDING, fr.STAGE_READING_HOME, fr.STAGE_READING_EVIDENCE,
            fr.STAGE_EVALUATING_FEASIBILITY, fr.STAGE_EVALUATING_RISK,
            fr.STAGE_VERIFYING_FINAL_READINESS, fr.STAGE_COMPLETE])

    def test_repeated_refresh_already_proven_is_idempotent_and_still_healthy(self):
        # Task Section 18: a second refresh, Scout answers ALREADY_PROVEN — a no-op/idempotent
        # healthy result, not a downgrade from the first REPROVED.
        deps = FakeDeps(mission_record=approved_record(),
                        reprove=reprove_body("ALREADY_PROVEN"),
                        preflight=preflight_result(binding_state="UNBOUND"))
        out = run(deps)
        self.assertTrue(out["ok"])
        self.assertEqual(out["binding"]["reproof_outcome"], "ALREADY_PROVEN")
        self.assertTrue(out["binding"]["reproof_success"])
        self.assertEqual(out["readiness"]["can_start"], True)

    def test_running_mission_keeps_binding_bound_and_is_not_reset(self):
        # Task Sections 7, 19 — the ACTIVE EXECUTION rule: a RUNNING mission's binding is
        # EXPECTED to be BOUND, Scout's reprove answers ALREADY_PROVEN, and Full Refresh must not
        # collapse, rewind or reinterpret that as anything other than a healthy active result.
        deps = FakeDeps(mission_record=approved_record(),
                        reprove=reprove_body("ALREADY_PROVEN"),
                        preflight=preflight_result(binding_state="BOUND"))
        out = run(deps)
        self.assertTrue(out["ok"])
        self.assertEqual(out["binding"]["binding_state"], "BOUND")
        self.assertEqual(out["binding"]["reproof_outcome"], "ALREADY_PROVEN")
        self.assertTrue(out["binding"]["reproof_success"])

    def test_reprove_is_attempted_before_the_fresh_proof_is_read(self):
        # Ordering matters: if reprove ran AFTER preflight, a real Scout's rebind would only show
        # up on the NEXT refresh, not this one.
        deps = FakeDeps(mission_record=approved_record())
        run(deps)
        kinds = [c[0] for c in deps.calls]
        self.assertEqual(kinds.index("reprove"), 0)
        self.assertLess(kinds.index("reprove"), kinds.index("preflight"))

    def test_reprove_unsupported_never_fabricates_bound(self):
        # An older/unmodified Scout 404s the reprove route today. Binding must be reported
        # EXACTLY as the fresh status shows it (UNBOUND here) — never upgraded locally.
        deps = FakeDeps(mission_record=approved_record(),
                        reprove={"outcome": scout_replan.OUTCOME_UNSUPPORTED},
                        preflight=preflight_result(binding_state="UNBOUND",
                                                    verified_route_hash=None, can_start=False))
        out = run(deps)
        self.assertEqual(out["binding"]["binding_state"], "UNBOUND")
        self.assertFalse(out["binding"]["reproof_supported"])
        self.assertIsNone(out["binding"]["reproof_outcome"])
        self.assertIsNone(out["binding"]["verified_route_hash"])
        self.assertEqual(out["readiness"]["can_start"], False)
        # Still a COMPLETE, coherent refresh — Full Refresh succeeded at TELLING the operator
        # the truth, even though it could not repair Scout's binding itself.
        self.assertEqual(out["mission"]["reconciliation"], fr.MATCHED)

    def test_reprove_rejected_or_unknown_also_never_fabricates_bound(self):
        for outcome in (scout_replan.OUTCOME_REJECTED, scout_replan.OUTCOME_UNKNOWN,
                        scout_replan.OUTCOME_UNAVAILABLE):
            with self.subTest(outcome=outcome):
                deps = FakeDeps(mission_record=approved_record(),
                                reprove={"outcome": outcome},
                                preflight=preflight_result(binding_state="UNBOUND"))
                out = run(deps)
                self.assertEqual(out["binding"]["binding_state"], "UNBOUND")
                # No Scout body carried a recognized outcome word for these transport failures —
                # the SCOUT outcome stays honestly None; the raw transport verdict is preserved
                # under its own field so diagnostics do not lose it.
                self.assertIsNone(out["binding"]["reproof_outcome"])
                self.assertEqual(out["binding"]["reproof_transport_outcome"], outcome)

    def test_reprove_definite_mismatch_outcomes_are_fail_closed_but_do_not_alone_fail_the_refresh(self):
        # Task Section 10: PACKAGE_MISMATCH / PIXHAWK_MISMATCH / MISSION_ID_MISMATCH are
        # CONCLUSIVE verdicts Scout itself proved — exactly like the Operator's own
        # PIXHAWK_MISMATCH reconciliation, a definite answer does not by itself make the refresh
        # incomplete. Start stays blocked via Scout's own can_start, read from a fresh status.
        for scout_outcome in ("PACKAGE_MISMATCH", "PIXHAWK_MISMATCH", "MISSION_ID_MISMATCH"):
            with self.subTest(scout_outcome=scout_outcome):
                deps = FakeDeps(mission_record=approved_record(),
                                reprove=reprove_body(scout_outcome),
                                preflight=preflight_result(binding_state="UNBOUND",
                                                            can_start=False))
                out = run(deps)
                self.assertEqual(out["binding"]["reproof_outcome"], scout_outcome)
                self.assertTrue(out["binding"]["reproof_fail_closed"])
                self.assertFalse(out["binding"]["reproof_success"])
                self.assertFalse(out["binding"]["reproof_inconclusive"])
                self.assertEqual(out["readiness"]["can_start"], False)
                # The refresh itself still ran to a complete, conclusive answer.
                self.assertTrue(out["ok"])

    def test_reprove_inconclusive_outcomes_make_the_refresh_incomplete(self):
        # Task Section 10: EVIDENCE_UNAVAILABLE / NO_CURRENT_PACKAGE / NO_CURRENT_MISSION / BUSY /
        # INTERNAL_ERROR mean Scout reached NO verdict this round — an unread input, exactly like
        # an unreachable Pixhawk or package, so the refresh is reported INCOMPLETE (`ok:false`)
        # even though the OPERATOR's own three-way reconciliation is otherwise MATCHED.
        for scout_outcome in ("EVIDENCE_UNAVAILABLE", "NO_CURRENT_PACKAGE", "NO_CURRENT_MISSION",
                              "INTERNAL_ERROR"):
            with self.subTest(scout_outcome=scout_outcome):
                deps = FakeDeps(mission_record=approved_record(),
                                reprove=reprove_body(scout_outcome),
                                preflight=preflight_result(binding_state="UNBOUND"))
                out = run(deps)
                self.assertEqual(out["binding"]["reproof_outcome"], scout_outcome)
                self.assertTrue(out["binding"]["reproof_inconclusive"])
                self.assertFalse(out["binding"]["reproof_success"])
                self.assertFalse(out["binding"]["reproof_fail_closed"])
                self.assertEqual(out["mission"]["reconciliation"], fr.MATCHED)
                self.assertFalse(out["ok"])

    def test_reprove_busy_is_a_definite_409_and_makes_the_refresh_incomplete(self):
        # Task Section 3/24: BUSY is carried on the transport as a definite HTTP 409, whether or
        # not Scout also echoes it in the body. Handled cleanly — no exception, no retry storm —
        # and the refresh is reported incomplete rather than a false success or a crash.
        deps = FakeDeps(mission_record=approved_record(),
                        reprove={"outcome": scout_replan.OUTCOME_REJECTED, "http_status": 409,
                                 "scout": {}},
                        preflight=preflight_result(binding_state="UNBOUND"))
        out = run(deps)
        self.assertEqual(out["binding"]["reproof_outcome"], "BUSY")
        self.assertTrue(out["binding"]["reproof_inconclusive"])
        self.assertFalse(out["ok"])

    def test_reprove_lifecycle_not_reprovable_neither_mutates_nor_fails_the_refresh(self):
        # Task Section 10: LIFECYCLE_NOT_REPROVABLE may be entirely legitimate (SUSPENDED/FAILED/
        # RUNNING/etc.) — nothing is mutated (this module has no write path besides the reprove
        # POST itself) and it does not, by itself, mark the refresh incomplete.
        deps = FakeDeps(mission_record=approved_record(),
                        reprove=reprove_body("LIFECYCLE_NOT_REPROVABLE"),
                        preflight=preflight_result(binding_state="BOUND"))
        out = run(deps)
        self.assertEqual(out["binding"]["reproof_outcome"], "LIFECYCLE_NOT_REPROVABLE")
        self.assertFalse(out["binding"]["reproof_success"])
        self.assertFalse(out["binding"]["reproof_inconclusive"])
        self.assertFalse(out["binding"]["reproof_fail_closed"])
        self.assertTrue(out["ok"])

    def test_pixhawk_mismatch_is_definite_and_fails_the_readiness_gate_not_the_refresh(self):
        deps = FakeDeps(mission_record=approved_record(route_hash=HASH_A),
                        preflight=preflight_result(pixhawk_hash=HASH_B, package_hash=HASH_A,
                                                    can_start=False))
        out = run(deps)
        self.assertEqual(out["mission"]["reconciliation"], fr.PIXHAWK_MISMATCH)
        self.assertEqual(out["readiness"]["can_start"], False)
        # The refresh itself still ran to a complete, conclusive answer.
        self.assertTrue(out["ok"])

    def test_package_mismatch_is_sync_required_never_silently_repaired(self):
        deps = FakeDeps(mission_record=approved_record(route_hash=HASH_A),
                        preflight=preflight_result(pixhawk_hash=HASH_A, package_hash=HASH_B,
                                                    package_consistent=False, can_start=False))
        out = run(deps)
        self.assertEqual(out["mission"]["reconciliation"], fr.PACKAGE_SYNC_REQUIRED)
        self.assertEqual(out["readiness"]["can_start"], False)
        # No call in this Deps interface can write a package — structurally proven, not just
        # asserted: FakeDeps exposes no such method for run_full_refresh to have called.
        self.assertNotIn("package_write", [c[0] for c in deps.calls])

    def test_transient_pixhawk_failure_is_incomplete_not_a_mismatch(self):
        # Section 25/27: a bounded read failure must resolve to EVIDENCE_UNAVAILABLE, and the
        # overall refresh is reported INCOMPLETE (`ok:false`) rather than a false mismatch.
        deps = FakeDeps(mission_record=approved_record(),
                        preflight=preflight_result(pixhawk_reachable=False, pixhawk_hash=None,
                                                    can_start=False, proof_complete=False))
        out = run(deps)
        self.assertEqual(out["mission"]["reconciliation"], fr.EVIDENCE_UNAVAILABLE)
        self.assertNotEqual(out["mission"]["reconciliation"], fr.PIXHAWK_MISMATCH)
        self.assertFalse(out["ok"])

    def test_energy_and_risk_are_passed_through_verbatim_never_fabricated(self):
        energy = {"feasible": True, "margin_wh": 12.3}
        risk = {"level": "LOW", "score": 0.2, "components": {"a": 1}, "floor": 0.1,
                "confidence": 0.9, "recommendation": "PROCEED"}
        deps = FakeDeps(mission_record=approved_record(), replan_body={
            "energy_feasibility": energy, "risk": risk})
        out = run(deps)
        self.assertEqual(out["energy_feasibility"], energy)
        self.assertEqual(out["risk"], risk)

    def test_energy_and_risk_absent_are_none_never_defaulted(self):
        deps = FakeDeps(mission_record=approved_record(), replan_body={})
        out = run(deps)
        self.assertIsNone(out["energy_feasibility"])
        self.assertIsNone(out["risk"])

    def test_home_is_reported_verbatim(self):
        home = {"verified": False, "reason": "Scout has not confirmed Home status recently.",
                "verification_method": None, "stale": True}
        deps = FakeDeps(mission_record=approved_record(), home=home)
        out = run(deps)
        self.assertEqual(out["home"], home)

    def test_result_is_read_only_by_construction(self):
        # The Deps interface mission_full_refresh is built against has exactly ONE call that can
        # reach Scout with a write (`reprove`, and that route's own contract forbids commanding
        # the vehicle — see scout_mission_execution.post_reprove_binding). Every other Deps method
        # is a read. This test proves the module calls nothing outside that fixed vocabulary.
        deps = FakeDeps(mission_record=approved_record())
        run(deps)
        allowed = {"reprove", "preflight", "replan_status", "home", "agent_state", "record"}
        self.assertTrue(all(c[0] in allowed for c in deps.calls), deps.calls)

    def test_operation_id_started_and_completed_timestamps_present(self):
        deps = FakeDeps(mission_record=approved_record())
        out = run(deps)
        self.assertTrue(out["operation_id"])
        self.assertTrue(out["started_at"])
        self.assertTrue(out["completed_at"])
        self.assertGreaterEqual(out["duration_s"], 0.0)
        self.assertTrue(out["read_only"])

    def test_record_operation_is_called_exactly_once_with_the_final_result(self):
        deps = FakeDeps(mission_record=approved_record())
        out = run(deps)
        records = [c for c in deps.calls if c[0] == "record"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][1], out["operation_id"])


class SingleFlightLockTests(unittest.TestCase):
    def test_second_refresh_for_the_same_vehicle_is_rejected_busy(self):
        vid = "unittest-vid-full-refresh"
        with fr.vehicle_refresh_lock(vid):
            self.assertTrue(fr.is_refreshing(vid))
            with self.assertRaises(fr.Busy):
                with fr.vehicle_refresh_lock(vid):
                    pass
        self.assertFalse(fr.is_refreshing(vid))

    def test_different_vehicles_do_not_contend(self):
        with fr.vehicle_refresh_lock("unittest-vid-a"):
            # A different vehicle's lock is independent — no Busy.
            with fr.vehicle_refresh_lock("unittest-vid-b"):
                pass


if __name__ == "__main__":
    unittest.main()
