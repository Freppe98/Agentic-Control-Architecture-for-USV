"""
AUTHORITATIVE DECISION POLICY -- the single deterministic bridge between the
continuous risk model's advisory recommendation and the replan FSM.

This module answers a THIRD, distinct question from risk_model.py and
mission_feasibility.py (E2 water-trial integration task, "three distinct
concepts" section):

    risk_model.py            -- "how severe is the current situation?"
                                  (LOW/ELEVATED/HIGH/CRITICAL)
    decision_policy.py        -- "what mission-level outcome does the Agent
                                  recommend, and does that recommendation
                                  warrant asking the replan FSM to act?"
                                  (THIS module -- action request only)
    replan_controller.py      -- "what procedural step is currently being
                                  executed?" (MONITORING/PLANNING/... FSM)

Architecture:

        RiskResult (risk_model.py)          MissionFeasibilityResult
                    \\                              /
                     v                            v
                        decision_policy.evaluate()
                                 |
                           ActionRequest
                                 |
                 replan_controller.observe(..., action_request=...)
                                 |
                      replan FSM (owns ALL vehicle writes)

decision_policy.py NEVER calls replan_controller directly and NEVER issues a
vehicle command -- it only produces an ActionRequest value that the caller
(local_agent.py) passes into replan_controller.observe(), the FSM's own
single existing entry point. Duplicate-transaction prevention is NOT this
module's job: replan_controller.py already owns a fully tested trigger-
generation/consumed latch (see its module docstring) that the action feeds
into exactly like the pre-existing energy_policy signal does. The generation
counter here exists purely for OBSERVABILITY (so the recorder can show
"this is the 2nd cycle asking for the same return", "already consumed" vs.
"a fresh escalation") -- it grants no authority of its own.

No ARM/mode-change/LOITER/RTL/mission-upload/replan/authority write of any
kind happens anywhere in this module. Every function here is pure.
"""
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import risk_model

ACTION_NONE = "NONE"
ACTION_REQUEST_RETURN_HOME = "REQUEST_RETURN_HOME"
ACTION_REQUEST_HOLD = "REQUEST_HOLD"

_RECOMMENDATION_TO_ACTION = {
    risk_model.RECOMMEND_CONTINUE: ACTION_NONE,
    risk_model.RECOMMEND_CONTINUE_WITH_CAUTION: ACTION_NONE,
    risk_model.RECOMMEND_RETURN: ACTION_REQUEST_RETURN_HOME,
    risk_model.RECOMMEND_HOLD: ACTION_REQUEST_HOLD,
}


@dataclass(frozen=True)
class ActionRequest:
    """Immutable output of DecisionPolicy.evaluate() -- what the authoritative
    decision policy is asking the replan FSM to do, and the evidence behind
    that ask. Carries everything section 3 of the task requires: source
    snapshot id, reason codes, risk level, recommendation, feasibility
    evidence, a generation/idempotency identity, and a timestamp."""
    action: str                             # ACTION_NONE / ACTION_REQUEST_RETURN_HOME / ACTION_REQUEST_HOLD
    source_snapshot_id: Optional[str]
    reason_codes: Tuple[str, ...]
    risk_level: str
    recommendation: str
    feasibility_evidence: Dict[str, Any]    # mission_feasible, rtl_return_feasible, status
    generation: int                         # observability-only edge-detected counter, see module docstring
    created_at: Optional[float]

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "source_snapshot_id": self.source_snapshot_id,
            "reason_codes": list(self.reason_codes),
            "risk_level": self.risk_level,
            "recommendation": self.recommendation,
            "feasibility_evidence": dict(self.feasibility_evidence),
            "generation": self.generation,
            "created_at": self.created_at,
        }


def _reason_codes(risk_result, feasibility_evidence: Dict[str, Any]) -> Tuple[str, ...]:
    codes = [risk_result.level]
    if risk_result.dominant_reason:
        codes.append(risk_result.dominant_reason)
    if risk_result.hard_constraint_violated:
        codes.append("HARD_CONSTRAINT_VIOLATED")
    rtl_feasible = feasibility_evidence.get("rtl_return_feasible")
    if rtl_feasible is True:
        codes.append("RTL_RETURN_FEASIBLE")
    elif rtl_feasible is False:
        codes.append("RTL_RETURN_INFEASIBLE")
    else:
        codes.append("RTL_RETURN_FEASIBILITY_UNKNOWN")
    return tuple(codes)


class DecisionPolicy:
    """Maps risk_model.RiskResult.recommendation to an ActionRequest. Stateful
    for the observability generation counter (edge-detected NONE -> non-NONE,
    mirroring replan_controller.py's own trigger-generation pattern, but a
    wholly separate instance -- see module docstring) AND for the latest
    feasibility evidence (below) -- both are read-only observability/evidence
    caches, never used to decide anything inside THIS module; evaluate()'s
    own return value is still a pure function of its arguments."""

    def __init__(self):
        self._active = False
        self._generation = 0
        self._latest_feasibility_evidence: Optional[Dict[str, Any]] = None

    def evaluate(self, risk_result, feasibility_result, snapshot, now: Optional[float] = None) -> ActionRequest:
        recommendation = risk_result.recommendation
        action = _RECOMMENDATION_TO_ACTION.get(recommendation, ACTION_NONE)

        want = action != ACTION_NONE
        if want and not self._active:
            self._generation += 1
        self._active = want

        feasibility_evidence = {
            "mission_feasible": feasibility_result.mission_feasible,
            "rtl_return_feasible": feasibility_result.rtl_return_feasible,
            "status": feasibility_result.status,
        }
        self._latest_feasibility_evidence = feasibility_evidence
        return ActionRequest(
            action=action,
            source_snapshot_id=getattr(snapshot, "snapshot_id", None),
            reason_codes=_reason_codes(risk_result, feasibility_evidence),
            risk_level=risk_result.level,
            recommendation=recommendation,
            feasibility_evidence=feasibility_evidence,
            generation=self._generation,
            created_at=now if now is not None else risk_result.evaluated_at,
        )

    def latest_feasibility_evidence(self) -> Optional[Dict[str, Any]]:
        """The feasibility_evidence from the MOST RECENT evaluate() call, or
        None before the first call. Intended as a lazy callback
        (replan_controller.py's `feasibility_fn`, E2 water-trial integration
        task section 15) so RTL fallback can check CURRENT rtl_return_
        feasible -- continuously refreshed by the main loop every iteration,
        including while a replan transaction runs on its own thread, so this
        is current to within one main-loop iteration, never a value bound at
        transaction start."""
        return self._latest_feasibility_evidence
