#!/usr/bin/env python3

#mavlink_adapter.py

from __future__ import annotations

import time
from typing import Optional
from fsm_agent import (
    InputData,
    Position,
    VehicleHealth,
    SwarmStatus,
    AgentState,
    CommState,
    GoalType,
)


class MavlinkAdapter:
    def __init__(self) -> None:
        self.last_heartbeat_time: Optional[float] = None
        self.last_position_time: Optional[float] = None

        self.lat: float = 0.0
        self.lon: float = 0.0
        self.heading_deg: float = 0.0
        self.battery_percent: float = 100.0
        self.fault_detected: bool = False

        self.current_fsm_state: AgentState = AgentState.IDLE
        self.current_goal: GoalType = GoalType.SEARCH_AREA
        self.last_intent_timestamp_s: float = 0.0

    def update_from_msg(self, msg) -> None:
        now = time.time()
        mtype = msg.get_type()

        if mtype == "HEARTBEAT":
            self.last_heartbeat_time = now

        elif mtype == "GLOBAL_POSITION_INT":
            self.lat = msg.lat / 1e7
            self.lon = msg.lon / 1e7
            if getattr(msg, "hdg", 65535) != 65535:
                self.heading_deg = msg.hdg / 100.0
            self.last_position_time = now

        elif mtype == "SYS_STATUS":
            battery_remaining = getattr(msg, "battery_remaining", -1)
            if battery_remaining is not None and battery_remaining >= 0:
                self.battery_percent = float(battery_remaining)

        elif mtype == "STATUSTEXT":
            text = getattr(msg, "text", "")
            if "FAIL" in text.upper() or "ERROR" in text.upper():
                self.fault_detected = True

    def derive_comm_state(self) -> CommState:
        now = time.time()

        if self.last_heartbeat_time is None:
            return CommState.DISCONNECTED

        age = now - self.last_heartbeat_time

        if age < 3:
            return CommState.CONNECTED
        if age < 8:
            return CommState.DEGRADED
        return CommState.DISCONNECTED

    def build_input(self) -> InputData:
        now = time.time()
        comm_state = self.derive_comm_state()

        return InputData(
            timestamp_s=now,
            current_state=self.current_fsm_state,
            current_goal=self.current_goal,
            comm_state=comm_state,
            position=Position(
                x=self.lat,
                y=self.lon,
                heading_deg=self.heading_deg,
            ),
            health=VehicleHealth(
                battery_percent=self.battery_percent,
                fault_detected=self.fault_detected,
            ),
            swarm=SwarmStatus(
                swarm_connected=(comm_state != CommState.DISCONNECTED),
                connected_neighbors=0,
                expected_neighbors=0,
            ),
            target_confidence=0.0,
            operator_override_goal=None,
            emergency_stop_requested=False,
            last_intent_timestamp_s=self.last_intent_timestamp_s,
        )