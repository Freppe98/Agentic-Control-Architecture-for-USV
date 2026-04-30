#!/usr/bin/env python3
"""
agentic_layer_fsm.py

A lightweight finite state machine (FSM) agentic layer template for a USV/UAV/robot.
Designed for Raspberry Pi 5 using only Python standard library.

Structure:
1. SETTINGS / CUSTOMIZATION
2. INPUT MODELS
3. OUTPUT MODELS
4. FSM LOGIC
5. EXAMPLE MAIN LOOP

"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum, auto
from typing import Optional, Dict, Any, Tuple
import time
import json
import logging


# =============================================================================
# 1. SETTINGS / CUSTOMIZATION
# =============================================================================

class AgentState(Enum):
    IDLE = auto()
    SEARCH = auto()
    CONVERGE = auto()
    LOITER = auto()
    HOLD = auto()
    RETURN = auto()
    DEGRADED = auto()
    PARTITIONED = auto()
    EMERGENCY_STOP = auto()


class CommState(Enum):
    CONNECTED = auto()
    DEGRADED = auto()
    PARTITIONED = auto()
    DISCONNECTED = auto()


class GoalType(Enum):
    NONE = auto()
    SEARCH_AREA = auto()
    INVESTIGATE_TARGET = auto()
    HOLD_POSITION = auto()
    RETURN_HOME = auto()
    LOITER_AREA = auto()
    EMERGENCY_STOP = auto()


@dataclass
class Config:
    """
    Easily customizable settings.
    Tune these values to match your mission logic.
    """
    low_battery_threshold: float = 20.0          # %
    critical_battery_threshold: float = 10.0     # %
    target_detect_threshold: float = 0.75        # confidence [0.0 - 1.0]
    stale_intent_timeout_s: float = 10.0         # intent expires after this (future: goes back to a previous position to get communication back?)
    disconnected_hold_time_s: float = 5.0        # how long to hold before return
    enable_partition_mode: bool = True
    enable_emergency_stop: bool = True
    default_speed_limit_mps: float = 1.5 # meters per second
    degraded_speed_limit_mps: float = 1.0 # meters per second
    return_speed_limit_mps: float = 1.2 # meters per second
    home_position: Tuple[float, float] = (0.0, 0.0) # Can be input from dashboard (set home)
    


# =============================================================================
# 2. INPUT MODELS
# =============================================================================

@dataclass
class Position: # We should get from MAVLink
    x: float
    y: float
    heading_deg: float


@dataclass
class VehicleHealth: # We should get from MAVLink
    battery_percent: float
    fault_detected: bool = False


@dataclass
class SwarmStatus: # Input from global agent / swarm control?
    swarm_connected: bool = True
    connected_neighbors: int = 0
    expected_neighbors: int = 0


@dataclass
class InputData:
    """
    Clear input section for the FSM.
    This is the only object the decision logic needs.
    """
    timestamp_s: float
    current_state: AgentState
    current_goal: GoalType
    comm_state: CommState
    position: Position
    health: VehicleHealth
    swarm: SwarmStatus
    target_confidence: float = 0.0
    operator_override_goal: Optional[GoalType] = None
    emergency_stop_requested: bool = False
    last_intent_timestamp_s: float = 0.0


# =============================================================================
# 3. OUTPUT MODELS
# =============================================================================

@dataclass
class Intent:
    """
    High-level output of the agentic layer.
    This is what you later map to MAVLink / autopilot commands.
    """
    goal_type: GoalType
    target_position: Optional[Tuple[float, float]] = None # short-term or long-term?
    speed_limit_mps: float = 0.0 # meters per second
    ttl_s: float = 0.0 # how long this intent is valid before re-evaluation (can be overridden by stale intent logic)
    priority: int = 0 #
    constraints: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OutputData:
    """
    Clear output section for the FSM.
    """
    next_state: AgentState
    intent: Intent
    explanation: str
    output_timestamp_s: float


# =============================================================================
# 4. FSM LOGIC
# =============================================================================

class AgenticFSM:
    """
    Main FSM class.
    Keeps config and internal timers.
    """

    def __init__(self, config: Config):
        self.config = config
        self.disconnected_since_s: Optional[float] = None

    def is_intent_stale(self, input_data: InputData) -> bool:
        age = input_data.timestamp_s - input_data.last_intent_timestamp_s
        return age > self.config.stale_intent_timeout_s

    def decide(self, input_data: InputData) -> OutputData:
        """
        Main decision function.

        Logic priority:
        1. Emergency / critical safety
        2. Low battery
        3. Communication degradation / partition
        4. Operator override
        5. Mission logic
        """

        now = input_data.timestamp_s
        stale = self.is_intent_stale(input_data)

        # ---------------------------------------------------------------------
        # A. EMERGENCY / CRITICAL SAFETY
        # ---------------------------------------------------------------------
        if self.config.enable_emergency_stop and (
            input_data.emergency_stop_requested
            or input_data.health.fault_detected
        ):
            return OutputData(
                next_state=AgentState.EMERGENCY_STOP,
                intent=Intent(
                    goal_type=GoalType.EMERGENCY_STOP,
                    speed_limit_mps=0.0,
                    ttl_s=1.0,
                    priority=100,
                    constraints={"reason": "emergency_or_fault"}
                ),
                explanation="Emergency stop triggered by operator or onboard fault.",
                output_timestamp_s=now,
            )

        # ---------------------------------------------------------------------
        # B. LOW BATTERY
        # ---------------------------------------------------------------------
        if input_data.health.battery_percent <= self.config.critical_battery_threshold:
            return OutputData(
                next_state=AgentState.RETURN,
                intent=Intent(
                    goal_type=GoalType.RETURN_HOME,
                    target_position=self.config.home_position,
                    speed_limit_mps=self.config.return_speed_limit_mps,
                    ttl_s=5.0,
                    priority=90,
                    constraints={"reason": "critical_battery"}
                ),
                explanation="Critical battery: return home immediately.",
                output_timestamp_s=now,
            )

        if input_data.health.battery_percent <= self.config.low_battery_threshold:
            return OutputData(
                next_state=AgentState.RETURN,
                intent=Intent(
                    goal_type=GoalType.RETURN_HOME,
                    target_position=self.config.home_position,
                    speed_limit_mps=self.config.return_speed_limit_mps,
                    ttl_s=5.0,
                    priority=80,
                    constraints={"reason": "low_battery"}
                ),
                explanation="Low battery: switching to return-home behavior.",
                output_timestamp_s=now,
            )

        # ---------------------------------------------------------------------
        # C. COMMUNICATION HANDLING
        # ---------------------------------------------------------------------
        if input_data.comm_state == CommState.DISCONNECTED:
            if self.disconnected_since_s is None:
                self.disconnected_since_s = now

            disconnected_duration = now - self.disconnected_since_s

            if disconnected_duration < self.config.disconnected_hold_time_s:
                return OutputData(
                    next_state=AgentState.HOLD,
                    intent=Intent(
                        goal_type=GoalType.HOLD_POSITION,
                        target_position=(input_data.position.x, input_data.position.y),
                        speed_limit_mps=0.0,
                        ttl_s=2.0,
                        priority=70,
                        constraints={"reason": "temporary_disconnect"}
                    ),
                    explanation="Disconnected: temporarily holding position.",
                    output_timestamp_s=now,
                )
            else:
                return OutputData(
                    next_state=AgentState.RETURN,
                    intent=Intent(
                        goal_type=GoalType.RETURN_HOME,
                        target_position=self.config.home_position,
                        speed_limit_mps=self.config.return_speed_limit_mps,
                        ttl_s=5.0,
                        priority=75,
                        constraints={"reason": "persistent_disconnect"}
                    ),
                    explanation="Disconnected too long: returning home.",
                    output_timestamp_s=now,
                )
        else:
            self.disconnected_since_s = None

        if input_data.comm_state == CommState.PARTITIONED and self.config.enable_partition_mode:
            return OutputData(
                next_state=AgentState.PARTITIONED,
                intent=Intent(
                    goal_type=GoalType.LOITER_AREA,
                    target_position=(input_data.position.x, input_data.position.y),
                    speed_limit_mps=self.config.degraded_speed_limit_mps,
                    ttl_s=4.0,
                    priority=60,
                    constraints={"reason": "swarm_partition", "local_autonomy_only": True}
                ),
                explanation="Swarm partition detected: switching to local partition mode.",
                output_timestamp_s=now,
            )

        if input_data.comm_state == CommState.DEGRADED:
            # If there is a strong target signal, still allow investigation.
            if input_data.target_confidence >= self.config.target_detect_threshold:
                return OutputData(
                    next_state=AgentState.CONVERGE,
                    intent=Intent(
                        goal_type=GoalType.INVESTIGATE_TARGET,
                        speed_limit_mps=self.config.degraded_speed_limit_mps,
                        ttl_s=3.0,
                        priority=65,
                        constraints={"reason": "degraded_but_high_target_confidence"}
                    ),
                    explanation="Comms degraded, but target confidence is high: investigate target.",
                    output_timestamp_s=now,
                )

            return OutputData(
                next_state=AgentState.DEGRADED,
                intent=Intent(
                    goal_type=GoalType.SEARCH_AREA,
                    speed_limit_mps=self.config.degraded_speed_limit_mps,
                    ttl_s=3.0,
                    priority=50,
                    constraints={"reason": "degraded_comms", "reduced_reporting": True}
                ),
                explanation="Communication degraded: continue limited local search.",
                output_timestamp_s=now,
            )

        # ---------------------------------------------------------------------
        # D. OPERATOR OVERRIDE
        # ---------------------------------------------------------------------
        if input_data.operator_override_goal is not None:
            override_goal = input_data.operator_override_goal

            if override_goal == GoalType.RETURN_HOME:
                return OutputData(
                    next_state=AgentState.RETURN,
                    intent=Intent(
                        goal_type=GoalType.RETURN_HOME,
                        target_position=self.config.home_position,
                        speed_limit_mps=self.config.return_speed_limit_mps,
                        ttl_s=5.0,
                        priority=85,
                        constraints={"reason": "operator_override"}
                    ),
                    explanation="Operator override: return home.",
                    output_timestamp_s=now,
                )

            if override_goal == GoalType.HOLD_POSITION:
                return OutputData(
                    next_state=AgentState.HOLD,
                    intent=Intent(
                        goal_type=GoalType.HOLD_POSITION,
                        target_position=(input_data.position.x, input_data.position.y),
                        speed_limit_mps=0.0,
                        ttl_s=3.0,
                        priority=85,
                        constraints={"reason": "operator_override"}
                    ),
                    explanation="Operator override: hold position.",
                    output_timestamp_s=now,
                )

        # ---------------------------------------------------------------------
        # E. MISSION LOGIC
        # ---------------------------------------------------------------------
        # Stale intent handling
        if stale:
            return OutputData(
                next_state=AgentState.LOITER,
                intent=Intent(
                    goal_type=GoalType.LOITER_AREA,
                    target_position=(input_data.position.x, input_data.position.y),
                    speed_limit_mps=self.config.default_speed_limit_mps,
                    ttl_s=2.0,
                    priority=40,
                    constraints={"reason": "stale_intent"}
                ),
                explanation="Previous intent became stale: loitering safely.",
                output_timestamp_s=now,
            )

        # If target confidence is high, investigate
        if input_data.target_confidence >= self.config.target_detect_threshold:
            return OutputData(
                next_state=AgentState.CONVERGE,
                intent=Intent(
                    goal_type=GoalType.INVESTIGATE_TARGET,
                    speed_limit_mps=self.config.default_speed_limit_mps,
                    ttl_s=4.0,
                    priority=55,
                    constraints={"reason": "target_detected"}
                ),
                explanation="Target confidence above threshold: investigate target.",
                output_timestamp_s=now,
            )

        # Default search behavior
        if input_data.current_goal == GoalType.SEARCH_AREA:
            return OutputData(
                next_state=AgentState.SEARCH,
                intent=Intent(
                    goal_type=GoalType.SEARCH_AREA,
                    speed_limit_mps=self.config.default_speed_limit_mps,
                    ttl_s=4.0,
                    priority=30,
                    constraints={"reason": "continue_search"}
                ),
                explanation="Continuing normal search behavior.",
                output_timestamp_s=now,
            )

        # If no goal is defined, stay idle/hold
        return OutputData(
            next_state=AgentState.IDLE,
            intent=Intent(
                goal_type=GoalType.HOLD_POSITION,
                target_position=(input_data.position.x, input_data.position.y),
                speed_limit_mps=0.0,
                ttl_s=2.0,
                priority=10,
                constraints={"reason": "no_active_goal"}
            ),
            explanation="No active goal: holding current position.",
            output_timestamp_s=now,
        )


# =============================================================================
# 5. EXAMPLE MAIN LOOP
# =============================================================================

def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s"
    )


def output_to_json(output_data: OutputData) -> str:
    """
    Convert output to a clean JSON string for logging / IPC / network sending.
    """
    payload = {
        "next_state": output_data.next_state.name,
        "intent": {
            "goal_type": output_data.intent.goal_type.name,
            "target_position": output_data.intent.target_position,
            "speed_limit_mps": output_data.intent.speed_limit_mps,
            "ttl_s": output_data.intent.ttl_s,
            "priority": output_data.intent.priority,
            "constraints": output_data.intent.constraints,
        },
        "explanation": output_data.explanation,
        "output_timestamp_s": output_data.output_timestamp_s,
    }
    return json.dumps(payload, indent=2)


def example_input() -> InputData:
    """
    Example input payload.
    Replace this later with:
    - MAVLink telemetry
    - socket input
    - dashboard messages
    - swarm summaries
    """
    now = time.time()
    return InputData(
        timestamp_s=now,
        current_state=AgentState.SEARCH,
        current_goal=GoalType.SEARCH_AREA,
        comm_state=CommState.CONNECTED,
        position=Position(x=12.5, y=8.2, heading_deg=93.0),
        health=VehicleHealth(battery_percent=64.0, fault_detected=False),
        swarm=SwarmStatus(swarm_connected=True, connected_neighbors=2, expected_neighbors=3),
        target_confidence=0.20,
        operator_override_goal=None,
        emergency_stop_requested=False,
        last_intent_timestamp_s=now - 2.0,
    )


def main() -> None:
    setup_logging()

    config = Config()
    agent = AgenticFSM(config=config)

    # Example single-step execution
    input_data = example_input()
    output_data = agent.decide(input_data)

    logging.info("FSM decision complete.")
    print("\n=== INPUT ===")
    print(input_data)

    print("\n=== OUTPUT ===")
    print(output_to_json(output_data))


if __name__ == "__main__":
    main()