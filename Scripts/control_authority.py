#!/usr/bin/env python3
"""
control_authority.py

Tracks whether this Local Agent currently holds control of the Pixhawk, by reading
Scout's own Flask API (GET /agent/control_authority — motherpi/services/flask, a
separate on-vehicle service). Control authority is vehicle state owned by that
service, not a queued operator command, so this is a plain read, not a poll/ack
round-trip.

Defaults to OPERATOR (RC has exclusive authority) until Scout Flask reports
LOCAL_AGENT. The Local Agent must never assume control on its own — callers gate
any Pixhawk write behind has_control().
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class ControlAuthority:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.authority = "OPERATOR"

    def has_control(self) -> bool:
        return self.authority == "LOCAL_AGENT"

    def poll(self, timeout_s: float = 2.0) -> str:
        """Read the current authority from Scout Flask. Returns the current value;
        unchanged (and logged) on any network/parse failure or an unrecognized
        value — a dropped link or a malformed response must not silently grant
        control."""
        url = f"{self.base_url}/agent/control_authority"
        try:
            with urllib.request.urlopen(url, timeout=timeout_s) as resp:
                data = json.load(resp)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            print(f"[ControlAuthority] poll failed: {exc}")
            return self.authority

        reported = data.get("authority")
        if reported not in ("LOCAL_AGENT", "OPERATOR"):
            print(f"[ControlAuthority] poll returned unrecognized authority: {reported!r}")
            return self.authority

        if reported != self.authority:
            print(f"[ControlAuthority] authority: {self.authority} -> {reported}")
        self.authority = reported
        return self.authority
