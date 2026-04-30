#!/usr/bin/env python3
"""
command_adapter.py

Uploads waypoint missions to a Pixhawk via MAVLink.
"""

from __future__ import annotations

import time
from typing import List, Dict, Any, Optional

from pymavlink import mavutil


class CommandAdapter:
    def __init__(self, master) -> None:
        self.master = master

    def upload_mission(
        self,
        waypoints: List[Dict[str, Any]],
        timeout_s: float = 5.0,
    ) -> bool:
        if not waypoints:
            return False

        if not self._clear_mission(timeout_s=timeout_s):
            print("[CommandAdapter] Warning: mission clear failed.")

        target_system = self.master.target_system
        target_component = self.master.target_component

        self.master.mav.mission_count_send(
            target_system,
            target_component,
            len(waypoints),
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
        )

        for seq in range(len(waypoints)):
            request = self._wait_for_request(seq, timeout_s=timeout_s)
            if request is None:
                print(f"[CommandAdapter] Timeout waiting for request {seq}.")
                return False

            wp = waypoints[seq]
            lat = float(wp.get("lat", 0.0))
            lon = float(wp.get("lon", 0.0))
            alt = float(wp.get("alt_m", 0.0))

            self.master.mav.mission_item_int_send(
                target_system,
                target_component,
                seq,
                mavutil.mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                mavutil.mavlink.MAV_CMD_NAV_WAYPOINT,
                0,
                1 if seq == 0 else 0,
                float(wp.get("hold_time_s", 0.0)),
                float(wp.get("acceptance_radius_m", 0.0)),
                float(wp.get("loiter_radius_m", 0.0)),
                float(wp.get("yaw_deg", 0.0)),
                int(lat * 1e7),
                int(lon * 1e7),
                alt,
                mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
            )

        ack = self.master.recv_match(
            type="MISSION_ACK",
            blocking=True,
            timeout=timeout_s,
        )
        if ack is None:
            print("[CommandAdapter] No MISSION_ACK received.")
            return False

        if getattr(ack, "type", None) not in (
            mavutil.mavlink.MAV_MISSION_ACCEPTED,
            None,
        ):
            print(f"[CommandAdapter] Mission rejected: {getattr(ack, 'type', None)}")
            return False

        print("[CommandAdapter] Mission upload complete.")
        return True

    def _clear_mission(self, timeout_s: float = 3.0) -> bool:
        target_system = self.master.target_system
        target_component = self.master.target_component
        self.master.mav.mission_clear_all_send(
            target_system,
            target_component,
            mavutil.mavlink.MAV_MISSION_TYPE_MISSION,
        )

        ack = self.master.recv_match(
            type="MISSION_ACK",
            blocking=True,
            timeout=timeout_s,
        )
        return ack is not None

    def _wait_for_request(
        self,
        seq: int,
        timeout_s: float = 5.0,
    ) -> Optional[Any]:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = self.master.recv_match(
                type=["MISSION_REQUEST_INT", "MISSION_REQUEST"],
                blocking=True,
                timeout=0.5,
            )
            if msg is None:
                continue
            if getattr(msg, "seq", None) == seq:
                return msg
        return None
