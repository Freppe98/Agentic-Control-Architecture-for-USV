"""
Mission revision metadata: the auditable record of one replanning transaction's
output -- what strategy produced the revised mission, from which snapshot and
transition, how the route changed, and how validation / upload / readback went.

This is a data container the replan controller populates as a transaction
progresses; it never performs I/O of its own. It is what the operator inspects
to answer "what did the agent replace the mission with, and why".

The route_content_hash fields use the Local-Agent-side route_hash (byte-for-byte
mission-contract-v1), so revised_route_hash here is directly comparable to the
expected/observed hash the Flask upload/readback reports.
"""
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

STRATEGY_SAFE_RETURN_HOME = "SAFE_RETURN_HOME"


@dataclass
class MissionRevision:
    mission_id: Optional[str]
    parent_revision: Optional[int]
    new_revision: int
    decision_snapshot_id: Optional[str]
    transition_id: Optional[str]
    strategy: str = STRATEGY_SAFE_RETURN_HOME
    reason_codes: List[str] = field(default_factory=list)
    original_route_hash: Optional[str] = None
    revised_route_hash: Optional[str] = None
    created_at: float = field(default_factory=lambda: round(time.time(), 3))

    # Route change accounting (section 4, "where meaningful"). For SAFE_RETURN_HOME
    # the revised route is built by retracing already-approved waypoints, so:
    #   preserved_waypoints -- approved points reused (retraced), in the new order
    #   removed_waypoints   -- approved points dropped (the un-traversed survey legs)
    #   inserted_waypoints  -- points not from the approved route (the current
    #                          position connector and the Home terminus)
    preserved_waypoint_count: int = 0
    removed_waypoint_count: int = 0
    inserted_waypoint_count: int = 0
    revised_waypoint_count: int = 0

    # ── Shortest-safe-return planner evidence (safe_return_planner.py) ─────────
    # Which of SHORTEST_SAFE_RETURN / RETRACE_APPROVED_FALLBACK actually
    # produced the route above, plus the diagnostic evidence a thesis/ops
    # reviewer needs to see it wasn't a mislabeled long retrace: whether the
    # direct current->Home segment was itself provably safe, how many
    # visibility-graph candidate nodes the constrained search considered (2
    # for a direct win), whether the retrace fallback had to be used, and how
    # long planning took. route_distance_m is the SAME geo.path_length_m the
    # actual-route energy recheck already computes -- duplicated here only so
    # it sits next to the strategy that produced it, not a second formula.
    planner_strategy: Optional[str] = None
    planner_route_distance_m: Optional[float] = None
    planner_direct_path_valid: Optional[bool] = None
    planner_candidate_node_count: Optional[int] = None
    planner_fallback_used: Optional[bool] = None
    planner_runtime_s: Optional[float] = None

    # ── Identity / proof evidence (CRITICAL ISSUE 2, thesis + safety) ──────────
    # ORIGINAL mission identity, proven FRESH immediately before any vehicle write:
    #   original_route_count -- the approved original route waypoint count
    #   original_proof       -- the fresh proof record (package id/hash/count vs.
    #                           the fresh Pixhawk route hash/count, source, age,
    #                           timestamp) that gated the whole transaction
    # REVISED (safe-return) mission identity, so later execution monitoring
    # compares against the REVISED hash -- never the original:
    #   replan_operation_id / revision identity -- see transition_id / new_revision
    #   revised_route_count  -- the revised route waypoint count
    #   revised_proof        -- the fresh revised readback hash/count verification
    #   revised_progression  -- bounded shared-verifier progression evidence
    # trigger_reason_codes reuses reason_codes above; verified_home is captured
    # for audit. proof timestamps are recorded inside the *_proof records.
    original_route_count: Optional[int] = None
    original_proof: Optional[Dict[str, Any]] = None
    revised_route_count: Optional[int] = None
    revised_proof: Optional[Dict[str, Any]] = None
    revised_progression: Optional[Dict[str, Any]] = None
    verified_home: Optional[Dict[str, Any]] = None

    # Outcomes, filled in as the transaction advances. None until reached.
    validation_result: Optional[Dict[str, Any]] = None
    upload_operation_result: Optional[Dict[str, Any]] = None
    readback_verification_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        return asdict(self)
