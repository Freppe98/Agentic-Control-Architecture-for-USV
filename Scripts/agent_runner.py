#!/usr/bin/env python3
"""
agent_runner.py

Orchestrates MAVLink and Global adapters, merges inputs, and runs the FSM.
"""

from __future__ import annotations

import argparse
import csv
import json
import time

from pymavlink import mavutil

from fsm_agent import AgenticFSM, CommState, Config, GoalType, InputData, output_to_json
from command_adapter import CommandAdapter
from control_authority import ControlAuthority
from global_adapter import GlobalAdapter, GlobalAdapterConfig
from mavlink_adapter import MavlinkAdapter


def build_fsm_input(
    mavlink_adapter: MavlinkAdapter,
    global_adapter: GlobalAdapter,
) -> InputData:
    mavlink_input = mavlink_adapter.build_input()

    return InputData(
        timestamp_s=mavlink_input.timestamp_s,
        current_state=mavlink_adapter.current_fsm_state,
        current_goal=global_adapter.get_current_goal(),
        comm_state=global_adapter.derive_comm_state(),
        position=mavlink_input.position,
        health=mavlink_input.health,
        swarm=global_adapter.get_swarm_status(),
        target_confidence=global_adapter.get_target_confidence(),
        operator_override_goal=global_adapter.get_operator_override(),
        emergency_stop_requested=global_adapter.get_emergency_stop(),
        last_intent_timestamp_s=max(
            mavlink_adapter.last_intent_timestamp_s,
            global_adapter.get_last_intent_timestamp(),
        ),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run FSM with MAVLink + global inputs."
    )
    parser.add_argument(
        "--mavlink",
        default="udp:0.0.0.0:14550",
        help="MAVLink connection string.",
    )
    parser.add_argument(
        "--global-ip",
        default="0.0.0.0",
        help="UDP listen IP for global adapter.",
    )
    parser.add_argument(
        "--global-port",
        type=int,
        default=15000,
        help="UDP listen port for global adapter.",
    )
    parser.add_argument(
        "--home-lat",
        type=float,
        default=0.0,
        help="Home latitude for return behavior.",
    )
    parser.add_argument(
        "--home-lon",
        type=float,
        default=0.0,
        help="Home longitude for return behavior.",
    )
    parser.add_argument(
        "--log-csv",
        default="fsm_log.csv",
        help="CSV log output path.",
    )
    parser.add_argument(
        "--scout-api-url",
        default="http://127.0.0.1:8080",
        help="Scout's own Flask API base URL (motherpi/services/flask), for reading "
             "control authority. Defaults to localhost since it runs on the same host.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print(f"Connecting to MAVLink on {args.mavlink}")
    master = mavutil.mavlink_connection(args.mavlink)

    print("Waiting for heartbeat...")
    master.wait_heartbeat()
    print(f"Heartbeat received from system {master.target_system}")

    config = Config(home_position=(args.home_lat, args.home_lon))
    fsm = AgenticFSM(config)

    mavlink_adapter = MavlinkAdapter()
    global_adapter = GlobalAdapter(
        GlobalAdapterConfig(
            listen_ip=args.global_ip,
            listen_port=args.global_port,
        )
    )
    command_adapter = CommandAdapter(master)
    authority = ControlAuthority(args.scout_api_url)

    last_state = None
    last_waypoints_signature = None
    last_blocked_signature = None
    last_authority_poll = 0.0
    authority_poll_interval_s = 1.0

    with open(args.log_csv, "w", newline="") as log_file:
        writer = csv.writer(log_file)
        writer.writerow([
            "time",
            "fsm_state",
            "goal_type",
            "comm_state",
            "heartbeat_age_s",
            "lat",
            "lon",
            "battery",
            "heading",
            "global_comm",
            "target_confidence",
        ])

        try:
            while True:
                if time.time() - last_authority_poll >= authority_poll_interval_s:
                    authority.poll()
                    last_authority_poll = time.time()

                msg = master.recv_match(blocking=False)
                if msg is not None:
                    mavlink_adapter.update_from_msg(msg)

                global_adapter.update()

                input_data = build_fsm_input(mavlink_adapter, global_adapter)
                output_data = fsm.decide(input_data)

                waypoints = global_adapter.get_waypoints()
                waypoints_signature = json.dumps(waypoints, sort_keys=True)
                should_upload = (
                    waypoints
                    and waypoints_signature != last_waypoints_signature
                    and output_data.intent.goal_type == GoalType.SEARCH_AREA
                    and not input_data.emergency_stop_requested
                    and not input_data.health.fault_detected
                    and input_data.comm_state != CommState.DISCONNECTED
                )

                if should_upload and authority.has_control():
                    if command_adapter.upload_mission(waypoints):
                        last_waypoints_signature = waypoints_signature
                        last_blocked_signature = None
                elif should_upload and waypoints_signature != last_blocked_signature:
                    print("[ControlAuthority] Mission ready but OPERATOR holds "
                          "authority — not sending.")
                    last_blocked_signature = waypoints_signature

                mavlink_adapter.current_fsm_state = output_data.next_state
                mavlink_adapter.last_intent_timestamp_s = output_data.output_timestamp_s

                hb_age = (
                    9999.0
                    if mavlink_adapter.last_heartbeat_time is None
                    else time.time() - mavlink_adapter.last_heartbeat_time
                )

                if output_data.next_state != last_state:
                    print("\n=== FSM TRANSITION ===")
                    print(output_to_json(output_data))
                    print(
                        f"comm={input_data.comm_state.name} "
                        f"hb_age={hb_age:.2f}s "
                        f"pos=({input_data.position.x:.6f}, "
                        f"{input_data.position.y:.6f}) "
                        f"battery={input_data.health.battery_percent:.1f}% "
                        f"heading={input_data.position.heading_deg:.1f}"
                    )
                    last_state = output_data.next_state

                writer.writerow([
                    time.time(),
                    output_data.next_state.name,
                    output_data.intent.goal_type.name,
                    input_data.comm_state.name,
                    hb_age,
                    input_data.position.x,
                    input_data.position.y,
                    input_data.health.battery_percent,
                    input_data.position.heading_deg,
                    global_adapter.derive_comm_state().name,
                    input_data.target_confidence,
                ])
                log_file.flush()

                time.sleep(0.1)

        except KeyboardInterrupt:
            print("\nStopping runner cleanly.")
            global_adapter.close()


if __name__ == "__main__":
    main()
