"""
Conservative, transparent energy policy for the safe-return decision.

This is deliberately NOT a learned or coulomb-counting battery model. It turns
a return DISTANCE into an estimated return COST in percent using a single
configurable usable-range figure, and decides a safe return is needed when
either:

  * battery is at/below a hard critical floor (critical_battery_percent), OR
  * the predicted margin -- battery minus estimated return cost minus a
    configured reserve -- is not positive.

The normal (non-critical) decision is therefore about *return feasibility*,
not a bare battery percentage, which is the point of section 3: a boat far from
Home at 40% may need to turn back while one beside Home at 25% does not.

Every input to the calculation is returned in the result so the Agent page can
show the working. A simulated experiment injection can override the battery, or
the margin, or force the decision outright -- always flagged simulated.

Debounce: a single evaluation never triggers a return on its own. The trigger
condition must hold for `persistence_count` consecutive evaluations (state held
on the EnergyPolicy instance), so one noisy battery/GPS sample cannot start a
transaction. Unavailable battery is treated as UNKNOWN, never as 0% -- it
cannot, by itself, make a return look necessary.
"""
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

import replan_config

DECISION_REPLAN_SAFE_RETURN = "REPLAN_SAFE_RETURN"
DECISION_MONITOR = "MONITOR"

# Reason codes (stable, machine-readable).
CODE_CRITICAL_BATTERY = "CRITICAL_BATTERY"
CODE_MARGIN_NON_POSITIVE = "ENERGY_MARGIN_NON_POSITIVE"
CODE_SIMULATED_FORCED = "SIMULATED_FORCED"
CODE_BATTERY_UNAVAILABLE = "BATTERY_UNAVAILABLE"
CODE_FEASIBLE = "RETURN_FEASIBLE"


@dataclass
class EnergyResult:
    decision: str
    reason: str
    reason_codes: List[str]
    triggered_raw: bool           # this observation's condition, before debounce
    persisted: bool               # debounce satisfied
    consecutive_triggers: int
    persistence_required: int
    simulated: bool
    simulated_fields: List[str]
    inputs: Dict[str, Any]

    def to_dict(self) -> dict:
        return asdict(self)


class EnergyPolicy:
    """Holds the debounce counter across evaluations. One instance per
    controller. `cfg` is a replan_config.ReplanConfig (DEFAULT if omitted)."""

    def __init__(self, cfg: Optional[replan_config.ReplanConfig] = None):
        self.cfg = cfg or replan_config.DEFAULT
        self._consecutive = 0

    def reset(self) -> None:
        """Clear the debounce counter -- called after a transaction ends so a
        fresh streak is required before the next trigger."""
        self._consecutive = 0

    def evaluate(self, snapshot, injection: Optional[dict] = None) -> EnergyResult:
        cfg = self.cfg
        injection = injection or {}
        simulated_fields: List[str] = []

        # Battery: injection override wins, tagged simulated.
        battery = snapshot.battery_percent
        if injection.get("battery_percent") is not None:
            battery = injection["battery_percent"]
            simulated_fields.append("battery_percent")

        distance = snapshot.estimated_safe_return_distance_m
        usable_range = cfg.usable_range_m
        reserve = cfg.reserve_margin_percent

        # Return cost: distance / usable range, as a percentage. None when we
        # have no distance estimate at all.
        return_cost_percent = (
            None if distance is None or usable_range <= 0
            else round(distance / usable_range * 100.0, 2)
        )

        # Margin: injection override wins, tagged simulated; else computed.
        if injection.get("energy_margin_percent") is not None:
            margin = float(injection["energy_margin_percent"])
            simulated_fields.append("energy_margin_percent")
        elif battery is not None and return_cost_percent is not None:
            margin = round(battery - return_cost_percent - reserve, 2)
        else:
            margin = None

        reason_codes: List[str] = []
        triggered_raw = False
        reason = ""

        if injection.get("force_safe_return"):
            triggered_raw = True
            reason_codes.append(CODE_SIMULATED_FORCED)
            simulated_fields.append("force_safe_return")
            reason = "Simulated experiment injection forced a safe-return decision."
        elif battery is not None and battery <= cfg.critical_battery_percent:
            triggered_raw = True
            reason_codes.append(CODE_CRITICAL_BATTERY)
            reason = (
                f"Battery {battery}% is at/below the hard critical floor "
                f"{cfg.critical_battery_percent}%."
            )
        elif margin is not None and margin <= 0:
            triggered_raw = True
            reason_codes.append(CODE_MARGIN_NON_POSITIVE)
            reason = (
                f"Predicted safe-return margin {margin}% is not positive "
                f"(battery {battery}% - return cost {return_cost_percent}% - "
                f"reserve {reserve}%)."
            )
        elif battery is None and injection.get("energy_margin_percent") is None:
            reason_codes.append(CODE_BATTERY_UNAVAILABLE)
            reason = (
                "Battery unavailable and no simulated margin supplied; cannot "
                "assess return feasibility -- not triggering (unavailable is not 0%)."
            )
        else:
            reason_codes.append(CODE_FEASIBLE)
            reason = (
                f"Return feasible: margin {margin}% > 0 "
                f"(battery {battery}%, return cost {return_cost_percent}%, reserve {reserve}%)."
            )

        # Debounce.
        if triggered_raw:
            self._consecutive += 1
        else:
            self._consecutive = 0
        persistence_required = max(1, cfg.energy_persistence_count)
        persisted = triggered_raw and self._consecutive >= persistence_required

        decision = DECISION_REPLAN_SAFE_RETURN if persisted else DECISION_MONITOR
        if triggered_raw and not persisted:
            reason += (
                f" Debounce {self._consecutive}/{persistence_required} -- holding "
                "until the condition persists."
            )

        inputs = {
            "battery_percent": battery,
            "battery_valid": snapshot.battery_percent is not None,
            "safe_return_distance_m": distance,
            "usable_range_m": usable_range,
            "return_cost_percent": return_cost_percent,
            "reserve_margin_percent": reserve,
            "margin_percent": margin,
            "critical_battery_percent": cfg.critical_battery_percent,
        }

        return EnergyResult(
            decision=decision,
            reason=reason,
            reason_codes=reason_codes,
            triggered_raw=triggered_raw,
            persisted=persisted,
            consecutive_triggers=self._consecutive,
            persistence_required=persistence_required,
            simulated=bool(simulated_fields),
            simulated_fields=simulated_fields,
            inputs=inputs,
        )
