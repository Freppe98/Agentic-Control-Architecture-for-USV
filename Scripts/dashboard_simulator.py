#!/usr/bin/env python3
"""
dashboard_simulator.py

Sends UDP JSON messages to the GlobalAdapter for testing.
"""

from __future__ import annotations

import argparse
import json
import socket
import time


# Editable defaults (override via CLI if desired)
DEFAULT_IP = "127.0.0.1"
DEFAULT_PORT = 15000
DEFAULT_PERIOD_S = 1.0
DEFAULT_MISSION_GOAL = "SEARCH_AREA"
DEFAULT_OVERRIDE = "" # (HOLD_POSITION, RETURN_HOME, etc.)
DEFAULT_EMERGENCY_STOP = False
DEFAULT_EXPECTED_NEIGHBORS = 0
DEFAULT_CONNECTED_NEIGHBORS = 0
DEFAULT_TARGET_CONFIDENCE = 0.0
DEFAULT_WAYPOINTS = [
	{"id": "wp1", "lat": 56.7, "lon": 13.0, "alt_m": 0.0, "loiter_radius_m": 0.0},
	{"id": "wp2", "lat": 56.7005, "lon": 13.0005, "alt_m": 0.0, "loiter_radius_m": 0.0},
]


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Send simulated dashboard messages over UDP."
	)
	parser.add_argument(
		"--ip",
		default=DEFAULT_IP,
		help="Destination IP for GlobalAdapter.",
	)
	parser.add_argument(
		"--port",
		type=int,
		default=DEFAULT_PORT,
		help="Destination port for GlobalAdapter.",
	)
	parser.add_argument(
		"--period",
		type=float,
		default=DEFAULT_PERIOD_S,
		help="Seconds between messages.",
	)
	parser.add_argument(
		"--mission-goal",
		default=DEFAULT_MISSION_GOAL,
		help="Mission goal (e.g., SEARCH_AREA, RETURN_HOME).",
	)
	parser.add_argument(
		"--override",
		default=DEFAULT_OVERRIDE,
		help="Operator override goal (empty for none).",
	)
	parser.add_argument(
		"--emergency-stop",
		action="store_true",
		default=DEFAULT_EMERGENCY_STOP,
		help="Send emergency stop flag.",
	)
	parser.add_argument(
		"--expected-neighbors",
		type=int,
		default=DEFAULT_EXPECTED_NEIGHBORS,
		help="Expected swarm neighbor count.",
	)
	parser.add_argument(
		"--connected-neighbors",
		type=int,
		default=DEFAULT_CONNECTED_NEIGHBORS,
		help="Connected swarm neighbor count.",
	)
	parser.add_argument(
		"--target-confidence",
		type=float,
		default=DEFAULT_TARGET_CONFIDENCE,
		help="Target confidence in [0, 1].",
	)
	return parser.parse_args()


def build_payload(args: argparse.Namespace) -> dict:
	return {
		"type": "heartbeat",
		"timestamp_s": time.time(),
		"mission_goal": args.mission_goal,
		"operator_override": args.override or None,
		"emergency_stop": bool(args.emergency_stop),
		"expected_neighbors": int(args.expected_neighbors),
		"connected_neighbors": int(args.connected_neighbors),
		"target_confidence": float(args.target_confidence),
		"waypoints": DEFAULT_WAYPOINTS,
	}


def main() -> None:
	args = parse_args()
	addr = (args.ip, args.port)

	sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

	print(f"[DashboardSim] Sending UDP to {args.ip}:{args.port}")
	print(f"[DashboardSim] Period: {args.period}s")

	try:
		while True:
			payload = build_payload(args)
			data = json.dumps(payload).encode("utf-8")
			sock.sendto(data, addr)
			time.sleep(args.period)
	except KeyboardInterrupt:
		print("\n[DashboardSim] Stopping.")
	finally:
		sock.close()


if __name__ == "__main__":
	main()
