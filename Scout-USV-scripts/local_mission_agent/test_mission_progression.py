"""
Standalone tests for mission_progression.py -- the ONE shared progression
verifier used by Start, Resume, and the safe-return revised-mission AUTO.

    python3 test_mission_progression.py

These test the verifier directly (no controller) so the shared contract is
pinned independently of any caller: ACTIVE_TRUE / sequence-advance / movement
prove RUNNING; ACTIVE_UNKNOWN and a bare inactive sample are retried, never an
immediate failure; disarm / mode-leaving-AUTO / authority-loss / identity-change
fail immediately; stale position and GPS jitter never prove; and the watch lasts
the full configured deadline. That both controllers delegate here (not a second
copy) is asserted in test_mission_execution_controller.py and
test_replan_controller.py.
"""
import unittest
from dataclasses import dataclass
from typing import Optional

import mission_progression as mp


@dataclass
class FakeSnap:
    armed: Optional[bool] = True
    mode_name: Optional[str] = "AUTO"
    control_authority: Optional[str] = "LOCAL_AGENT"
    mission_id: Optional[str] = None
    current_sequence: Optional[int] = 1
    mission_count: Optional[int] = 4
    mission_active: Optional[bool] = None
    mission_active_evidence: Optional[str] = mp.ACTIVE_UNKNOWN
    # Age (seconds) of the mission_active_evidence observation -- None (the
    # default, matching every fixture/test that predates the freshness fix)
    # means "unreported", trusted exactly like before; a KNOWN age exceeding
    # the context's max_position_age_s is what the freshness fix now rejects.
    mission_active_evidence_age_s: Optional[float] = None
    latitude: Optional[float] = 56.6490
    longitude: Optional[float] = 12.8700
    position_age_s: Optional[float] = 0.5
    groundspeed: Optional[float] = 0.0


class AdvancingClock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


# A survey route whose item 1 (Pixhawk seq 1) is the first target.
_ROUTE = [
    {"latitude": 56.6500, "longitude": 12.8700},
    {"latitude": 56.6510, "longitude": 12.8700},
]


class _Base(unittest.TestCase):
    def _ctx(self, snaps, expected_mission_id=None, route=None,
             poll=0.5, min_disp=1.5, max_age=5.0):
        """Build a context whose read_snapshot yields the given snapshots in
        order (the last one repeats once exhausted). `snaps` is a list of FakeSnap
        or None (a failed read)."""
        self.clock = AdvancingClock(1000.0)
        seq = list(snaps)
        state = {"i": 0}

        def read():
            i = state["i"]
            state["i"] += 1
            return seq[i] if i < len(seq) else seq[-1]

        return mp.ProgressionContext(
            read_snapshot=read,
            target_for_sequence=mp.route_target_lookup(route or _ROUTE),
            expected_mission_id=expected_mission_id,
            poll_interval_s=poll, min_displacement_m=min_disp, max_position_age_s=max_age,
            clock=self.clock, sleep=self.clock.advance)


class TestPositiveProof(_Base):
    def test_active_true_proves(self):
        # Proof A requires a KNOWN, in-bound age (freshness-semantics
        # correction) -- the minimal proving setup now supplies one, exactly
        # like a real fresh MISSION_CURRENT sample would.
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=0.5)])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "A")

    def test_active_true_with_unreported_age_does_not_prove_via_a(self):
        # Freshness-semantics correction: an UNREPORTED age is UNPROVABLE, not
        # "assume fresh" -- see DecisionSnapshot.mission_active_evidence_age_s's
        # own docstring ("None is 'freshness unprovable', never 'assume
        # fresh'"). Proof A must therefore require a KNOWN, in-bound age.
        # Stationary and seq unchanged, so B/C cannot fire either -> the watch
        # must run the full deadline and end unconfirmed, never crash/raise and
        # never treat the unknown age as an immediate failure (see
        # test_active_true_unreported_age_then_sequence_advance_still_proves
        # for the "B/C still work" half of this same rule).
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=None,
                                  current_sequence=1)],
                        max_age=5.0, poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])
        self.assertEqual(r["failure_code"], "PROGRESSION_UNCONFIRMED")
        self.assertFalse(r["samples"][0]["mission_active_proven"])
        self.assertGreaterEqual(r["actual_elapsed_s"], 2.0)

    def test_active_true_unreported_age_then_sequence_advance_still_proves(self):
        # The other half of the "age None must not create an immediate
        # failure" rule: an ACTIVE_TRUE-but-unprovable-age sample does not
        # itself prove progression, but it does not block B/C from proving it
        # normally on a later sample either -- unprovable freshness is simply
        # "no proof A here", not a fatal condition.
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=None,
                                  current_sequence=1),
                         FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=None,
                                  current_sequence=2)],
                        max_age=5.0)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "B")

    def test_active_true_fresh_age_proves(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=1.0)],
                        max_age=5.0)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "A")
        self.assertTrue(r["samples"][0]["mission_active_proven"])

    def test_active_true_stale_age_does_not_prove(self):
        # Task test 11: an ANCIENT cached ACTIVE_TRUE (age known and beyond
        # the freshness bound) must never prove a NEW Start -- stationary,
        # baseline seq == observed seq, no movement, so B/C cannot fire
        # either -- this must run the full deadline and fail closed.
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=99.0,
                                  current_sequence=1)],
                        max_age=5.0, poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])
        self.assertEqual(r["failure_code"], "PROGRESSION_UNCONFIRMED")
        self.assertFalse(r["samples"][0]["mission_active_proven"])
        self.assertGreaterEqual(r["actual_elapsed_s"], 2.0)

    def test_active_true_stale_age_then_fresh_sample_proves(self):
        # The ancient sample alone never proves it, but a LATER, genuinely
        # fresh ACTIVE_TRUE sample still proves progression normally --
        # the freshness fix rejects the STALE sample, not the mission.
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=99.0),
                         FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                  mission_active_evidence_age_s=0.5)],
                        max_age=5.0)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "A")

    def test_sequence_advance_proves(self):
        # Baseline seq 1; a later sample at seq 2 -> proof B.
        ctx = self._ctx([FakeSnap(current_sequence=1),
                         FakeSnap(current_sequence=1),
                         FakeSnap(current_sequence=2)])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "B")

    def test_already_at_seq1_does_not_prove(self):
        # Seq 1 selected before AUTO and staying at 1 is NOT progression.
        ctx = self._ctx([FakeSnap(current_sequence=1)], poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])
        self.assertEqual(r["failure_code"], "PROGRESSION_UNCONFIRMED")

    def test_movement_toward_target_proves(self):
        # Baseline south of target (56.6500); moving north reduces distance -> C.
        base = FakeSnap(latitude=56.6490, current_sequence=1)
        moved = FakeSnap(latitude=56.6497, current_sequence=1)  # ~78 m north
        ctx = self._ctx([base, base, moved])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "C")

    def test_gps_jitter_does_not_prove(self):
        base = FakeSnap(latitude=56.6490, current_sequence=1)
        jitter = FakeSnap(latitude=56.64900002, current_sequence=1)  # ~0.02 m
        ctx = self._ctx([base, jitter, base, jitter], poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])

    def test_stale_position_does_not_prove_movement(self):
        # Big displacement but stale telemetry -> proof C is refused.
        base = FakeSnap(latitude=56.6490, current_sequence=1, position_age_s=0.5)
        moved_stale = FakeSnap(latitude=56.6510, current_sequence=1, position_age_s=99.0)
        ctx = self._ctx([base, base, moved_stale], poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])


class TestRetriedNotImmediateFailure(_Base):
    def test_active_unknown_is_retried_not_failed(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN)], poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        # Ran the whole window instead of failing on the first UNKNOWN sample.
        self.assertFalse(r["proven"])
        self.assertEqual(r["failure_code"], "PROGRESSION_UNCONFIRMED")
        self.assertGreaterEqual(r["actual_elapsed_s"], 2.0)

    def test_active_false_explicit_is_retried_not_failed(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_FALSE_EXPLICIT)], poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])
        self.assertEqual(r["failure_code"], "PROGRESSION_UNCONFIRMED")

    def test_active_unknown_stays_unknown_even_with_fresh_age(self):
        # The A-proof freshness fix is scoped to ACTIVE_TRUE only -- it must
        # never reinterpret ACTIVE_UNKNOWN as provable/true just because a
        # (fresh, known) age happens to be attached, and must never collapse
        # ACTIVE_UNKNOWN to a false/failure. Stationary/no seq advance, so this
        # must still run the full deadline unconfirmed -- three-valued
        # semantics preserved, not two-valued.
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN,
                                  mission_active_evidence_age_s=0.1,
                                  current_sequence=1)],
                        max_age=5.0, poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        self.assertFalse(r["proven"])
        self.assertEqual(r["failure_code"], "PROGRESSION_UNCONFIRMED")
        self.assertFalse(r["samples"][0]["mission_active_proven"])
        self.assertEqual(r["mission_active_evidence_observed"], [mp.ACTIVE_UNKNOWN])

    def test_unreadable_sample_is_retried_not_failed(self):
        # A None read (failed telemetry) then a proving sample -> proven, not
        # failed. The proving sample carries a known, fresh age (proof A now
        # requires one -- freshness-semantics correction).
        ctx = self._ctx([None, None, FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                              mission_active_evidence_age_s=0.5)])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])

    def test_delayed_active_true_succeeds(self):
        # The proving sample carries a known, fresh age (proof A now requires
        # one -- freshness-semantics correction); the preceding UNKNOWN
        # samples are retried, never an immediate failure, exactly as before.
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN)] * 3
                        + [FakeSnap(mission_active_evidence=mp.ACTIVE_TRUE,
                                   mission_active_evidence_age_s=0.5)])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertEqual(r["proof"], "A")


class TestImmediateFailures(_Base):
    def test_disarm_fails_immediately(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN),
                         FakeSnap(armed=False)])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 10.0)
        self.assertEqual(r["failure_code"], "VEHICLE_DISARMED")
        self.assertLess(r["actual_elapsed_s"], 5.0)

    def test_mode_left_auto_fails_immediately(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN),
                         FakeSnap(mode_name="MANUAL")])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 10.0)
        self.assertEqual(r["failure_code"], "MODE_LEFT_AUTO")

    def test_authority_loss_fails_immediately(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN),
                         FakeSnap(control_authority="OPERATOR")])
        r = mp.watch(ctx, mp.capture_baseline(ctx), 10.0)
        self.assertEqual(r["failure_code"], "AUTHORITY_LOST")

    def test_identity_change_fails_immediately(self):
        ctx = self._ctx([FakeSnap(mission_id="m1", mission_active_evidence=mp.ACTIVE_UNKNOWN),
                         FakeSnap(mission_id="other")],
                        expected_mission_id="m1")
        r = mp.watch(ctx, mp.capture_baseline(ctx), 10.0)
        self.assertEqual(r["failure_code"], "MISSION_IDENTITY_CHANGED")

    def test_legacy_operator_mission_label_does_not_fail_identity_gate(self):
        # Mission binding/reproof identity bug regression: vehicle_state.
        # mission.current_mission_id reporting Flask's legacy /start_mission
        # operator sensor-logging label ("<YYYY-MM-DD_HH-MM>_<name>") must
        # never be treated as a genuine mission-identity change -- it is a
        # different identifier namespace from the canonical msn-* identity
        # this watch proves against (see planning_package.
        # is_legacy_operator_mission_label). Proof still proceeds normally
        # (via evidence A here) despite the label disagreeing byte-for-byte
        # with expected_mission_id throughout the whole watch.
        ctx = self._ctx(
            [FakeSnap(mission_id="2026-08-20_11-54_biltema 1",
                     mission_active_evidence=mp.ACTIVE_TRUE,
                     mission_active_evidence_age_s=0.5)],
            expected_mission_id="msn-183d11e892ff")
        r = mp.watch(ctx, mp.capture_baseline(ctx), 5.0)
        self.assertTrue(r["proven"])
        self.assertIsNone(r["failure_code"])

    def test_genuinely_different_canonical_identity_still_fails_immediately(self):
        # The namespace exclusion above must not become "ignore all identity
        # mismatches" -- a vehicle-reported id that is NOT in the legacy
        # label format and genuinely disagrees with expected_mission_id still
        # fails closed exactly as before this fix.
        ctx = self._ctx(
            [FakeSnap(mission_id="msn-183d11e892ff", mission_active_evidence=mp.ACTIVE_UNKNOWN),
             FakeSnap(mission_id="msn-000000000000")],
            expected_mission_id="msn-183d11e892ff")
        r = mp.watch(ctx, mp.capture_baseline(ctx), 10.0)
        self.assertEqual(r["failure_code"], "MISSION_IDENTITY_CHANGED")


class TestDeadlineAndDiagnostics(_Base):
    def test_full_deadline_and_sample_count(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN, current_sequence=1)],
                        poll=0.4)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 6.0)
        self.assertGreaterEqual(r["actual_elapsed_s"], 6.0)
        self.assertLess(r["actual_elapsed_s"], 6.0 + 0.4 + 0.01)
        self.assertGreaterEqual(r["sample_count"], 13)   # ~ 6.0 / 0.4

    def test_diagnostic_fields_present(self):
        ctx = self._ctx([FakeSnap(mission_active_evidence=mp.ACTIVE_UNKNOWN, current_sequence=1)],
                        poll=0.5)
        r = mp.watch(ctx, mp.capture_baseline(ctx), 2.0)
        for key in ("configured_timeout_s", "actual_elapsed_s", "sample_count", "baseline",
                    "final_sequence", "final_position", "final_mode", "final_armed",
                    "authority", "max_groundspeed", "max_distance_moved_m",
                    "mission_active_evidence_observed", "samples"):
            self.assertIn(key, r)
        self.assertEqual(r["configured_timeout_s"], 2.0)
        s0 = r["samples"][0]
        for key in ("elapsed_s", "armed", "mode_name", "mission_active_evidence",
                    "current_sequence", "latitude", "longitude", "authority", "mission_id"):
            self.assertIn(key, s0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
