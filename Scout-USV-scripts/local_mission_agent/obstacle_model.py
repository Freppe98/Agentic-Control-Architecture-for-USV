"""
Minimal obstacle-event model for the graph-detour thesis feature.

An obstacle event is an experiment-injected detection ahead of the vehicle:

    {
      "event_type": "OBSTACLE_AHEAD",
      "distance_m": 10,
      "source": "EXPERIMENT_INJECTION",
      "confidence": 1.0,
      "expires_after_s": 30
    }

This module only *models and classifies* an event -- it never touches the
vehicle. Classification is a pure function of the event plus the current
time, so it is deterministic and unit-testable with no I/O:

    CLOSE       (~3 m)  -> immediate LOITER only; do NOT reverse, do NOT
                          replan while still moving.
    LONG_RANGE  (~10 m) -> there is room to hold and compute a graph-based
                          detour proposal (dry-run) before acting.
    CLEAR                -> obstacle gone / never present: no action.
    EXPIRED             -> detection is stale (past expires_after_s): treated
                          as CLEAR so an old event can't keep forcing a
                          decision.

The distance boundary between CLOSE and LONG_RANGE is config-driven
(config.OBSTACLE_CLOSE_DISTANCE_M) so the experiment can tune it without a
code change.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import config

# Classification labels
CLOSE = "CLOSE"
LONG_RANGE = "LONG_RANGE"
CLEAR = "CLEAR"
EXPIRED = "EXPIRED"

# Recommended response per classification -- what the decision layer should do.
ACTION_LOITER = "LOITER"          # immediate station-keep, no reverse/replan
ACTION_PROPOSE_DETOUR = "PROPOSE_DETOUR"
ACTION_NONE = "NONE"

_ACTION_FOR = {
    CLOSE: ACTION_LOITER,
    LONG_RANGE: ACTION_PROPOSE_DETOUR,
    CLEAR: ACTION_NONE,
    EXPIRED: ACTION_NONE,
}

# Event-type sentinels the experiment can inject.
OBSTACLE_AHEAD = "OBSTACLE_AHEAD"
OBSTACLE_CLEARED = "OBSTACLE_CLEARED"


@dataclass
class ObstacleEvent:
    """One injected obstacle detection ahead of the vehicle."""
    event_type: str = OBSTACLE_AHEAD
    distance_m: Optional[float] = None
    source: str = "EXPERIMENT_INJECTION"
    confidence: float = 1.0
    expires_after_s: float = config.OBSTACLE_DEFAULT_EXPIRES_AFTER_S
    # When the detection was made. Defaults to now; the experiment may pass an
    # explicit detection time to test expiry deterministically.
    detected_at: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ObstacleEvent":
        data = data or {}
        return cls(
            event_type=data.get("event_type", OBSTACLE_AHEAD),
            distance_m=data.get("distance_m"),
            source=data.get("source", "EXPERIMENT_INJECTION"),
            confidence=data.get("confidence", 1.0),
            expires_after_s=data.get(
                "expires_after_s", config.OBSTACLE_DEFAULT_EXPIRES_AFTER_S),
            detected_at=data.get("detected_at", time.time()),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_type": self.event_type,
            "distance_m": self.distance_m,
            "source": self.source,
            "confidence": self.confidence,
            "expires_after_s": self.expires_after_s,
            "detected_at": round(self.detected_at, 3),
        }

    def age_s(self, now: Optional[float] = None) -> float:
        now = time.time() if now is None else now
        return now - self.detected_at

    def is_expired(self, now: Optional[float] = None) -> bool:
        if self.expires_after_s is None:
            return False
        return self.age_s(now) > self.expires_after_s

    def classify(self, now: Optional[float] = None) -> str:
        """
        Deterministic classification. Order matters: an expired event is
        stale regardless of the distance it once reported.
        """
        if self.is_expired(now):
            return EXPIRED
        if self.event_type == OBSTACLE_CLEARED:
            return CLEAR
        if self.distance_m is None or self.confidence is None or self.confidence <= 0:
            return CLEAR
        if self.distance_m <= config.OBSTACLE_CLOSE_DISTANCE_M:
            return CLOSE
        return LONG_RANGE

    def recommended_action(self, now: Optional[float] = None) -> str:
        return _ACTION_FOR[self.classify(now)]
