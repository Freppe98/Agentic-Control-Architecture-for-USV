"""Startup / reconnect reconciliation of the approved mission against observed vehicle state.

Run from operator-scripts/:  python -m unittest tests.test_mission_reconcile   (no pytest).

WHAT THESE TESTS EXIST TO PIN
-----------------------------
The station restored an active-mission pointer from disk and never re-examined it. A pointer
left at a SUPERSEDED record made every fresh Scout/Pixhawk comparison run against the wrong
mission, so a vehicle carrying an approved, persisted, verified mission was reported as

    "Agent package does not match the approved mission"

and the only way out was Plan → Finish & upload — which mints a NEW mission id and REWRITES the
flight controller's mission with content that was already on it.

The rules pinned here:

  • an approved record whose canonical route hash IS the one the flight controller reports
    becomes the active record again — no new mission id, no upload;
  • record identity and content identity stay apart: differing mission ids over an identical
    canonical route are never a content mismatch;
  • a live complete read-back re-derives VERIFIED for a record whose upload result was lost
    with the in-memory command queue;
  • package_sync_state is RECOMPUTED from live Agent evidence, in both directions;
  • a route no approved record carries is NEVER adopted, never approved, never made active;
  • insufficient evidence yields RECONCILING and changes nothing — a mismatch is never
    declared from evidence we do not have;
  • reconciliation performs NO mission upload and issues NO vehicle command. Asserted against
    a backend whose upload/command hooks raise on contact;
  • vehicles are completely isolated from one another.
"""
import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import mission_contract  # noqa: E402
import mission_reconcile as mr  # noqa: E402

SCOUT_VID = 2
SAR_VID = 3


def waypoints(n, *, lat0=56.66):
    return [{"latitude": round(lat0 + i / 1e4, 7), "longitude": 12.88, "loiter_time_s": 0}
            for i in range(n)]


def record(mission_id, vid=SCOUT_VID, *, n=14, lat0=56.66, upload_status="VERIFIED",
           package_sync_state=None, created_at="2026-08-01T00:00:00+00:00"):
    """An immutable approved mission record whose route_hash genuinely describes its route."""
    wps = waypoints(n, lat0=lat0)
    return {
        "mission_id": mission_id, "vehicle_id": vid, "mission_revision": 0,
        "route_waypoints": wps, "route_hash": mission_contract.route_content_hash(wps),
        "upload_status": upload_status, "verified_at": None,
        "package_sync_state": package_sync_state, "package_sync_error": None,
        "created_at": created_at, "immutable": True,
    }


def readback(route_hash, *, count=14, reachable=True, partial=False, age=0.0):
    return {"reachable": reachable, "partial": partial, "route_content_hash": route_hash,
            "route_waypoint_count": count, "pixhawk_item_count": count + 1,
            "evidence_age_s": age, "evidence_cached": age > 0}


def package(mission_id, route_hash, *, count=14, stored=True, usable=True):
    return {"stored": stored, "usable": usable, "mission_id": mission_id,
            "route_hash": route_hash, "route_count": count}


class Store:
    """An in-memory operator mission store plus a Deps over it, recording every persist."""

    def __init__(self, records, active):
        self.records = {r["mission_id"]: copy.deepcopy(r) for r in records}
        self.active = dict(active)
        self.persists = 0

    def deps(self):
        return mr.Deps(
            vehicle_records=lambda v: [r for r in self.records.values() if r["vehicle_id"] == v],
            active_mission_id=lambda v: self.active.get(v),
            set_active=lambda v, mid: self.active.__setitem__(v, mid),
            persist=self._persist,
        )

    def _persist(self):
        self.persists += 1
        return True

    def run(self, vid=SCOUT_VID, **kw):
        kw.setdefault("package_reachable", True)
        return mr.reconcile(self.deps(), vid, **kw)


# ══════════════════════════════════════════════════════════════════════════════════════
# CASE A — everything already agrees
# ══════════════════════════════════════════════════════════════════════════════════════
class AlreadySynchronizedTests(unittest.TestCase):

    def test_operator_pixhawk_and_package_agree_is_synchronized_with_no_change(self):
        rec = record("msn-a", package_sync_state="SYNCED")
        store = Store([rec], {SCOUT_VID: "msn-a"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package("msn-a", rec["route_hash"]))
        self.assertEqual(v["outcome"], mr.SYNCHRONIZED)
        self.assertTrue(v["conclusive"])
        self.assertEqual(v["actions"], [])
        self.assertFalse(v["rebound"])
        self.assertEqual(store.active[SCOUT_VID], "msn-a")
        self.assertEqual(store.persists, 0, "a healthy station must not rewrite its store")

    def test_restart_with_everything_unchanged_needs_no_new_mission_id(self):
        rec = record("msn-a", package_sync_state="SYNCED")
        store = Store([rec], {SCOUT_VID: "msn-a"})
        before = set(store.records)
        store.run(readback=readback(rec["route_hash"]),
                  package_evidence=package("msn-a", rec["route_hash"]))
        self.assertEqual(set(store.records), before)


# ══════════════════════════════════════════════════════════════════════════════════════
# CASE B — the active pointer is stale, another approved record IS the mission on board
# ══════════════════════════════════════════════════════════════════════════════════════
class StaleActivePointerTests(unittest.TestCase):

    def setUp(self):
        self.old = record("msn-old", lat0=56.10, package_sync_state="REQUIRED",
                          created_at="2026-08-01T00:00:00+00:00")
        self.cur = record("msn-current", lat0=56.66, package_sync_state="SYNCED",
                          created_at="2026-08-08T00:00:00+00:00")
        self.store = Store([self.old, self.cur], {SCOUT_VID: "msn-old"})

    def test_matching_approved_record_becomes_active_again(self):
        v = self.store.run(readback=readback(self.cur["route_hash"]),
                           package_evidence=package("msn-current", self.cur["route_hash"]))
        self.assertEqual(v["outcome"], mr.SYNCHRONIZED)
        self.assertTrue(v["rebound"])
        self.assertEqual(self.store.active[SCOUT_VID], "msn-current")
        self.assertEqual([a["action"] for a in v["actions"]], [mr.ACTION_REBIND])
        self.assertEqual(v["actions"][0]["from"], "msn-old")
        self.assertEqual(self.store.persists, 1)

    def test_rebinding_creates_no_new_mission_record(self):
        before = set(self.store.records)
        self.store.run(readback=readback(self.cur["route_hash"]),
                       package_evidence=package("msn-current", self.cur["route_hash"]))
        self.assertEqual(set(self.store.records), before)

    def test_the_superseded_record_is_preserved_untouched(self):
        self.store.run(readback=readback(self.cur["route_hash"]),
                       package_evidence=package("msn-current", self.cur["route_hash"]))
        kept = self.store.records["msn-old"]
        self.assertEqual(kept["route_hash"], self.old["route_hash"])
        self.assertEqual(kept["upload_status"], "VERIFIED")

    def test_several_records_with_the_same_route_resolve_deterministically(self):
        # Same canonical route, three records, none of them the active one. The newest wins,
        # and the choice must not depend on iteration order.
        twin_a = record("msn-twin-a", lat0=56.66, created_at="2026-08-02T00:00:00+00:00")
        twin_b = record("msn-twin-b", lat0=56.66, created_at="2026-08-05T00:00:00+00:00")
        twin_c = record("msn-twin-c", lat0=56.66, created_at="2026-08-03T00:00:00+00:00")
        for order in ([twin_a, twin_b, twin_c], [twin_c, twin_b, twin_a], [twin_b, twin_a, twin_c]):
            store = Store([self.old] + order, {SCOUT_VID: "msn-old"})
            store.run(readback=readback(twin_a["route_hash"]),
                      package_evidence=package("msn-twin-b", twin_a["route_hash"]))
            self.assertEqual(store.active[SCOUT_VID], "msn-twin-b")

    def test_an_already_active_record_wins_over_an_identical_twin(self):
        twin = record("msn-twin", lat0=56.66, created_at="2026-09-01T00:00:00+00:00")
        store = Store([self.cur, twin], {SCOUT_VID: "msn-current"})
        store.run(readback=readback(self.cur["route_hash"]),
                  package_evidence=package("msn-current", self.cur["route_hash"]))
        self.assertEqual(store.active[SCOUT_VID], "msn-current",
                         "a healthy pointer must not churn onto an identical twin")


# ══════════════════════════════════════════════════════════════════════════════════════
# TASK 4 — record identity is not content identity
# ══════════════════════════════════════════════════════════════════════════════════════
class IdentityDomainTests(unittest.TestCase):

    def test_differing_mission_ids_over_one_route_is_not_a_content_mismatch(self):
        rec = record("msn-operator-id", package_sync_state="SYNCED")
        store = Store([rec], {SCOUT_VID: "msn-operator-id"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package("msn-scout-id", rec["route_hash"]))
        self.assertNotEqual(v["outcome"], mr.MISMATCH)
        self.assertEqual(v["outcome"], mr.PACKAGE_SYNC_REQUIRED)
        self.assertEqual(v["reason"], mr.PACKAGE_IDENTITY_MISMATCH)
        self.assertEqual(store.active[SCOUT_VID], "msn-operator-id")

    def test_a_route_hash_match_is_what_selects_the_record_not_the_id(self):
        # Scout's package names a mission id the operator has never heard of; the ROUTE is one
        # the operator approved. The route decides.
        rec = record("msn-known", lat0=56.66)
        store = Store([rec], {SCOUT_VID: None})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package("msn-unknown-to-us", rec["route_hash"]))
        self.assertEqual(store.active[SCOUT_VID], "msn-known")
        self.assertEqual(v["active_route_hash"], rec["route_hash"])


# ══════════════════════════════════════════════════════════════════════════════════════
# Re-deriving VERIFIED — the upload result that died with the in-memory command queue
# ══════════════════════════════════════════════════════════════════════════════════════
class VerificationRecoveryTests(unittest.TestCase):

    def test_a_live_readback_reverifies_a_record_stuck_at_queued(self):
        rec = record("msn-q", upload_status="QUEUED", package_sync_state="REQUIRED")
        store = Store([rec], {SCOUT_VID: "msn-q"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package("msn-q", rec["route_hash"]))
        self.assertEqual(store.records["msn-q"]["upload_status"], "VERIFIED")
        self.assertEqual(store.records["msn-q"]["verified_by"], "RECONCILED_READBACK")
        self.assertIn(mr.ACTION_VERIFY, [a["action"] for a in v["actions"]])

    def test_a_recorded_upload_FAILURE_is_reported_not_erased(self):
        rec = record("msn-f", upload_status="FAILED")
        store = Store([rec], {SCOUT_VID: "msn-f"})
        store.run(readback=readback(rec["route_hash"]),
                  package_evidence=package("msn-f", rec["route_hash"]))
        self.assertEqual(store.records["msn-f"]["upload_status"], "FAILED")

    def test_a_record_whose_hash_does_not_describe_its_own_waypoints_is_never_promoted(self):
        rec = record("msn-tampered", upload_status="QUEUED")
        rec["route_waypoints"] = waypoints(14, lat0=57.00)     # hash no longer describes these
        store = Store([rec], {SCOUT_VID: "msn-tampered"})
        store.run(readback=readback(rec["route_hash"]),
                  package_evidence=package("msn-tampered", rec["route_hash"]))
        self.assertEqual(store.records["msn-tampered"]["upload_status"], "QUEUED")


# ══════════════════════════════════════════════════════════════════════════════════════
# package_sync_state is recomputed from live evidence, in both directions
# ══════════════════════════════════════════════════════════════════════════════════════
class PackageSyncStateTests(unittest.TestCase):

    def test_a_stale_restored_REQUIRED_is_cleared_when_scout_proves_the_package(self):
        rec = record("msn-p", package_sync_state="REQUIRED")
        rec["package_sync_error"] = "SCOUT_PACKAGE_POST_FAILED"
        store = Store([rec], {SCOUT_VID: "msn-p"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package("msn-p", rec["route_hash"]))
        self.assertEqual(store.records["msn-p"]["package_sync_state"], "SYNCED")
        self.assertIsNone(store.records["msn-p"]["package_sync_error"])
        self.assertEqual(v["outcome"], mr.SYNCHRONIZED)

    def test_a_stale_restored_SYNCED_becomes_REQUIRED_when_scout_holds_another_package(self):
        rec = record("msn-p", package_sync_state="SYNCED")
        other = record("msn-other", lat0=56.10)
        store = Store([rec, other], {SCOUT_VID: "msn-p"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package("msn-other", other["route_hash"]))
        self.assertEqual(store.records["msn-p"]["package_sync_state"], "REQUIRED")
        self.assertEqual(v["outcome"], mr.PACKAGE_SYNC_REQUIRED)

    def test_a_missing_package_is_package_sync_required_not_a_mismatch(self):
        rec = record("msn-p", package_sync_state="SYNCED")
        store = Store([rec], {SCOUT_VID: "msn-p"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence=package(None, None, stored=False))
        self.assertEqual(v["outcome"], mr.PACKAGE_SYNC_REQUIRED)
        self.assertEqual(v["reason"], mr.PACKAGE_NOT_STORED)
        self.assertEqual(store.records["msn-p"]["package_sync_state"], "REQUIRED")

    def test_an_unreachable_agent_leaves_the_recorded_sync_state_alone(self):
        rec = record("msn-p", package_sync_state="SYNCED")
        store = Store([rec], {SCOUT_VID: "msn-p"})
        v = store.run(readback=readback(rec["route_hash"]), package_evidence=None,
                      package_reachable=False)
        self.assertEqual(store.records["msn-p"]["package_sync_state"], "SYNCED")
        self.assertEqual(v["outcome"], mr.RECONCILING)
        self.assertEqual(v["reason"], mr.PACKAGE_UNREACHABLE)
        self.assertTrue(v["pixhawk_settled"])

    def test_a_package_reporting_only_some_identities_is_not_treated_as_agreement(self):
        rec = record("msn-p")
        store = Store([rec], {SCOUT_VID: "msn-p"})
        v = store.run(readback=readback(rec["route_hash"]),
                      package_evidence={"stored": True, "usable": True, "mission_id": "msn-p",
                                        "route_hash": None, "route_count": None})
        self.assertNotEqual(v["outcome"], mr.SYNCHRONIZED)
        self.assertEqual(v["outcome"], mr.RECONCILING)
        self.assertEqual(v["reason"], mr.PACKAGE_INCOMPLETE_EVIDENCE)


# ══════════════════════════════════════════════════════════════════════════════════════
# CASE D/E — unknown content is never adopted; a real disagreement stays a disagreement
# ══════════════════════════════════════════════════════════════════════════════════════
class UnapprovedAndMismatchTests(unittest.TestCase):

    def test_a_hash_no_approved_record_carries_is_never_auto_approved(self):
        rec = record("msn-approved", lat0=56.10)
        store = Store([rec], {SCOUT_VID: "msn-approved"})
        unknown = mission_contract.route_content_hash(waypoints(9, lat0=55.0))
        v = store.run(readback=readback(unknown, count=9),
                      package_evidence=package("msn-scout-only", unknown, count=9))
        self.assertEqual(v["outcome"], mr.MISMATCH)
        self.assertEqual(store.active[SCOUT_VID], "msn-approved")
        self.assertEqual(set(store.records), {"msn-approved"})
        self.assertEqual(store.persists, 0)

    def test_with_no_active_record_at_all_the_outcome_is_UNAPPROVED_MISSION(self):
        store = Store([], {})
        unknown = mission_contract.route_content_hash(waypoints(9, lat0=55.0))
        v = store.run(readback=readback(unknown, count=9),
                      package_evidence=package("msn-scout-only", unknown, count=9))
        self.assertEqual(v["outcome"], mr.UNAPPROVED_MISSION)
        self.assertEqual(v["reason"], mr.NO_APPROVED_RECORD)
        self.assertEqual(store.active, {})

    def test_a_genuine_three_way_content_disagreement_is_retained(self):
        rec = record("msn-approved", lat0=56.10)
        store = Store([rec], {SCOUT_VID: "msn-approved"})
        onboard = mission_contract.route_content_hash(waypoints(14, lat0=55.5))
        agent = mission_contract.route_content_hash(waypoints(14, lat0=54.5))
        v = store.run(readback=readback(onboard), package_evidence=package("msn-x", agent))
        self.assertEqual(v["outcome"], mr.MISMATCH)


# ══════════════════════════════════════════════════════════════════════════════════════
# CASE F — no verdict without evidence
# ══════════════════════════════════════════════════════════════════════════════════════
class InsufficientEvidenceTests(unittest.TestCase):

    def setUp(self):
        self.rec = record("msn-e", package_sync_state="SYNCED")
        self.other = record("msn-other", lat0=56.10)
        self.store = Store([self.rec, self.other], {SCOUT_VID: "msn-other"})

    def assert_untouched(self, verdict, reason):
        self.assertEqual(verdict["outcome"], mr.RECONCILING)
        self.assertFalse(verdict["conclusive"])
        self.assertEqual(verdict["reason"], reason)
        self.assertEqual(verdict["actions"], [])
        self.assertEqual(self.store.active[SCOUT_VID], "msn-other")
        self.assertEqual(self.store.persists, 0)

    def test_no_readback_at_all(self):
        self.assert_untouched(self.store.run(readback=None), mr.NO_READBACK)

    def test_unreachable_readback(self):
        self.assert_untouched(
            self.store.run(readback=readback(self.rec["route_hash"], reachable=False)),
            mr.NO_READBACK)

    def test_partial_readback(self):
        self.assert_untouched(
            self.store.run(readback=readback(self.rec["route_hash"], partial=True)),
            mr.READBACK_PARTIAL)

    def test_readback_without_a_route_hash(self):
        self.assert_untouched(self.store.run(readback=readback(None)),
                              mr.READBACK_HASH_UNAVAILABLE)

    def test_readback_older_than_the_proof_bound(self):
        self.assert_untouched(
            self.store.run(readback=readback(self.rec["route_hash"],
                                             age=mr.MAX_PROOF_AGE_S + 0.1)),
            mr.READBACK_STALE)


# ══════════════════════════════════════════════════════════════════════════════════════
# Multi-USV isolation
# ══════════════════════════════════════════════════════════════════════════════════════
class VehicleIsolationTests(unittest.TestCase):

    def test_reconciling_one_vehicle_never_touches_another(self):
        scout_old = record("msn-scout-old", SCOUT_VID, lat0=56.10)
        scout_cur = record("msn-scout-cur", SCOUT_VID, lat0=56.66,
                           created_at="2026-08-08T00:00:00+00:00")
        sar = record("msn-sar", SAR_VID, lat0=57.20, package_sync_state="REQUIRED")
        store = Store([scout_old, scout_cur, sar],
                      {SCOUT_VID: "msn-scout-old", SAR_VID: "msn-sar"})
        store.run(SCOUT_VID, readback=readback(scout_cur["route_hash"]),
                  package_evidence=package("msn-scout-cur", scout_cur["route_hash"]))
        self.assertEqual(store.active[SCOUT_VID], "msn-scout-cur")
        self.assertEqual(store.active[SAR_VID], "msn-sar")
        self.assertEqual(store.records["msn-sar"]["package_sync_state"], "REQUIRED")

    def test_another_vehicles_record_is_never_selected_even_on_an_exact_hash_match(self):
        # SAR-001's approved record carries the exact route Scout's flight controller reports.
        # It must NOT be adopted for Scout: ownership is checked, not inferred from the hash.
        sar = record("msn-sar", SAR_VID, lat0=56.66)
        scout_old = record("msn-scout-old", SCOUT_VID, lat0=56.10)
        store = Store([sar, scout_old], {SCOUT_VID: "msn-scout-old", SAR_VID: "msn-sar"})
        v = store.run(SCOUT_VID, readback=readback(sar["route_hash"]),
                      package_evidence=package("msn-sar", sar["route_hash"]))
        self.assertEqual(v["outcome"], mr.MISMATCH)
        self.assertEqual(store.active[SCOUT_VID], "msn-scout-old")


# ══════════════════════════════════════════════════════════════════════════════════════
# The structural guarantee: reconciliation cannot reach the vehicle
# ══════════════════════════════════════════════════════════════════════════════════════
class NoVehicleWriteTests(unittest.TestCase):

    def test_the_module_imports_nothing_that_can_upload_or_command(self):
        # Checked on the IMPORT GRAPH, not on the prose: the docstring names MISSION_UPLOAD
        # precisely because it promises never to issue one. What matters is that nothing which
        # could reach the vehicle is in scope.
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(mr))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "datetime", "mission_contract"},
                         "reconciliation may import only the hash calculator and the stdlib — "
                         f"found {sorted(imported)}")
        for forbidden in ("mission_publish", "scout_replan", "requests", "main", "httpx"):
            self.assertNotIn(forbidden, imported)

    def test_a_full_rebind_runs_with_every_vehicle_facing_dep_absent(self):
        # The Deps surface is the ONLY way out of this module, and it contains no vehicle hook
        # at all. A rebind + re-verify + sync-state recompute completes with nothing else wired.
        old = record("msn-old", lat0=56.10)
        cur = record("msn-cur", lat0=56.66, upload_status="QUEUED",
                     package_sync_state="REQUIRED", created_at="2026-08-08T00:00:00+00:00")
        store = Store([old, cur], {SCOUT_VID: "msn-old"})
        v = store.run(readback=readback(cur["route_hash"]),
                      package_evidence=package("msn-cur", cur["route_hash"]))
        self.assertEqual(v["outcome"], mr.SYNCHRONIZED)
        self.assertEqual(sorted(a["action"] for a in v["actions"]),
                         sorted([mr.ACTION_REBIND, mr.ACTION_VERIFY, mr.ACTION_SYNC_STATE]))
        self.assertEqual(set(vars(store.deps())),
                         {"vehicle_records", "active_mission_id", "set_active", "persist"})


if __name__ == "__main__":
    unittest.main()
