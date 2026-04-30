#!/usr/bin/env python3
"""
global_adapter.py

Receives high-level offboard messages from a dashboard/server/operator layer.

Purpose:
- Represent the unreliable 4G/Wi-Fi/server link
- Convert raw network messages into clean FSM input fields
- Track heartbeat age, staleness, overrides, emergency stop, and swarm status

This adapter should be degraded in simulation using Mininet/tc/netem.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from fsm_agent import CommState, GoalType, SwarmStatus


# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class GlobalAdapterConfig:
    listen_ip: str = "0.0.0.0"
    listen_port: int = 15000

    connected_timeout_s: float = 3.0
    degraded_timeout_s: float = 8.0
    disconnected_timeout_s: float = 15.0

    intent_validity_s: float = 10.0

    socket_timeout_s: float = 0.05

    default_expected_neighbors: int = 0


# =============================================================================
# GLOBAL / OFFBOARD STATE
# =============================================================================

@dataclass
class GlobalState:
    last_heartbeat_time_s: Optional[float] = None
    last_message_time_s: Optional[float] = None
    last_intent_timestamp_s: float = 0.0

    mission_goal: GoalType = GoalType.SEARCH_AREA
    operator_override_goal: Optional[GoalType] = None
    emergency_stop_requested: bool = False

    expected_neighbors: int = 0
    connected_neighbors: int = 0

    target_confidence: float = 0.0

    raw_last_message: Optional[dict] = None


# =============================================================================
# GLOBAL ADAPTER
# =============================================================================

class GlobalAdapter:
    """
    Receives UDP JSON messages from the global/server/operator layer.

    Expected message example:

    {
        "type": "heartbeat",
        "timestamp_s": 1710000000.0,
        "mission_goal": "SEARCH_AREA",
        "operator_override": null,
        "emergency_stop": false,
        "expected_neighbors": 3,
        "connected_neighbors": 2,
        "target_confidence": 0.2
    }
    """

    def __init__(self, config: GlobalAdapterConfig = GlobalAdapterConfig()) -> None:
        self.config = config
        self.state = GlobalState(
            expected_neighbors=config.default_expected_neighbors
        )

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((config.listen_ip, config.listen_port))
        self.sock.settimeout(config.socket_timeout_s)

        print(
            f"[GlobalAdapter] Listening on UDP "
            f"{config.listen_ip}:{config.listen_port}"
        )

    # -------------------------------------------------------------------------
    # PUBLIC UPDATE FUNCTION
    # -------------------------------------------------------------------------

    def update(self) -> None:
        """
        Non-blocking receive.
        Call this once per FSM loop.
        """
        try:
            data, addr = self.sock.recvfrom(4096)
        except socket.timeout:
            return

        try:
            payload = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            print("[GlobalAdapter] Warning: received invalid JSON")
            return

        self._handle_message(payload)

    # -------------------------------------------------------------------------
    # MESSAGE HANDLING
    # -------------------------------------------------------------------------

    def _handle_message(self, payload: dict) -> None:
        now = time.time()

        self.state.last_message_time_s = now
        self.state.raw_last_message = payload

        msg_type = payload.get("type", "unknown")

        if msg_type == "heartbeat":
            self.state.last_heartbeat_time_s = now

        if "mission_goal" in payload:
            parsed_goal = self._parse_goal(payload.get("mission_goal"))
            if parsed_goal is not None:
                self.state.mission_goal = parsed_goal
                self.state.last_intent_timestamp_s = payload.get(
                    "timestamp_s", now
                )

        if "operator_override" in payload:
            self.state.operator_override_goal = self._parse_goal(
                payload.get("operator_override")
            )

        if "emergency_stop" in payload:
            self.state.emergency_stop_requested = bool(
                payload.get("emergency_stop")
            )

        if "expected_neighbors" in payload:
            self.state.expected_neighbors = int(payload.get("expected_neighbors", 0))

        if "connected_neighbors" in payload:
            self.state.connected_neighbors = int(payload.get("connected_neighbors", 0))

        if "target_confidence" in payload:
            self.state.target_confidence = float(payload.get("target_confidence", 0.0))

    # -------------------------------------------------------------------------
    # FSM-FACING OUTPUTS
    # -------------------------------------------------------------------------

    def derive_comm_state(self) -> CommState:
        """
        Communication quality of the offboard/global link.
        This is the value that should be passed into the FSM.
        """
        now = time.time()

        if self.state.last_heartbeat_time_s is None:
            return CommState.DISCONNECTED

        age = now - self.state.last_heartbeat_time_s

        if age < self.config.connected_timeout_s:
            return CommState.CONNECTED

        if age < self.config.degraded_timeout_s:
            return CommState.DEGRADED

        if age < self.config.disconnected_timeout_s:
            return CommState.PARTITIONED

        return CommState.DISCONNECTED

    def get_current_goal(self) -> GoalType:
        return self.state.mission_goal

    def get_operator_override(self) -> Optional[GoalType]:
        return self.state.operator_override_goal

    def get_emergency_stop(self) -> bool:
        return self.state.emergency_stop_requested

    def get_target_confidence(self) -> float:
        return self.state.target_confidence

    def get_last_intent_timestamp(self) -> float:
        return self.state.last_intent_timestamp_s

    def get_swarm_status(self) -> SwarmStatus:
        comm_state = self.derive_comm_state()

        return SwarmStatus(
            swarm_connected=(comm_state != CommState.DISCONNECTED),
            connected_neighbors=self.state.connected_neighbors,
            expected_neighbors=self.state.expected_neighbors,
        )

    # -------------------------------------------------------------------------
    # HELPERS
    # -------------------------------------------------------------------------

    @staticmethod
    def _parse_goal(value) -> Optional[GoalType]:
        if value is None:
            return None

        if isinstance(value, GoalType):
            return value

        if isinstance(value, str):
            value = value.upper()

            try:
                return GoalType[value]
            except KeyError:
                print(f"[GlobalAdapter] Warning: unknown goal '{value}'")
                return None

        return None

    def close(self) -> None:
        self.sock.close()


# =============================================================================
# STANDALONE TEST
# =============================================================================

def main() -> None:
    adapter = GlobalAdapter()

    try:
        while True:
            adapter.update()

            print(
                f"comm={adapter.derive_comm_state().name} | "
                f"goal={adapter.get_current_goal().name} | "
                f"override={adapter.get_operator_override()} | "
                f"estop={adapter.get_emergency_stop()} | "
                f"neighbors={adapter.state.connected_neighbors}/"
                f"{adapter.state.expected_neighbors} | "
                f"target_conf={adapter.get_target_confidence():.2f}"
            )

            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\n[GlobalAdapter] Stopping.")
        adapter.close()


if __name__ == "__main__":
    main()