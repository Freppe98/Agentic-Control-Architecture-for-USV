#!/usr/bin/env python3
"""
Read-only bench diagnostic for the mission-execution lifecycle.

    python3 mission_execution_diag.py            # one-shot snapshot
    python3 mission_execution_diag.py --watch    # refresh every 2s

Prints, in one place, everything an operator on the bench needs to reason about
a Start/Pause/Resume/return-completion run WITHOUT an operator station and
WITHOUT any write path:

  * mission-execution state (+ derived REPLANNING) and can_start/pause/resume
  * replanning FSM state
  * Pixhawk mode + authority (observed values)
  * verified Home + home verification distance
  * planning-package consistency, mission id / route hash
  * current mission sequence and count
  * pause/resume sequence evidence + continuation_verified
  * distance to Home + arrival radius / persistence progress
  * active simulated experiment injection (if any)

STRICTLY READ-ONLY. It only issues GETs against the Local Agent inbound server
(port 8090) and the vehicle Flask service (LOCAL_FLASK_URL). It has NO
direct-MAVLink path and can neither change mode, set Home, nor start/pause/resume
a mission -- to drive the lifecycle, POST the /agent/mission_execution/* routes
(see BENCH_MISSION_EXECUTION.md). Mirrors bench_preflight.py's read-only stance.
"""
import argparse
import json
import sys
import time
import urllib.request

from config import LOCAL_FLASK_URL, LOCAL_AGENT_HTTP_PORT

_AGENT = f"http://127.0.0.1:{LOCAL_AGENT_HTTP_PORT}"


def _get(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode()), None
    except Exception as e:
        return None, str(e)


def _fmt(v):
    return "-" if v is None else v


def snapshot() -> str:
    lines = []
    me, me_err = _get(f"{_AGENT}/agent/mission_execution/status")
    replan, _ = _get(f"{_AGENT}/agent/replan/status")
    auth, _ = _get(f"{LOCAL_FLASK_URL}/agent/control_authority")
    home, _ = _get(f"{LOCAL_FLASK_URL}/agent/home_status")
    state, _ = _get(f"{LOCAL_FLASK_URL}/agent/state")

    lines.append("== Mission Execution ==")
    if me is None:
        lines.append(f"  mission_execution status UNAVAILABLE ({me_err})")
    else:
        seq = me.get("sequence", {}) or {}
        rc = me.get("return_completion", {}) or {}
        rp = me.get("replanning", {}) or {}
        lines.append(f"  state            : {_fmt(me.get('state'))}  (effective: {_fmt(me.get('effective_state'))})")
        lines.append(f"  can start/pause/resume : {me.get('can_start')} / {me.get('can_pause')} / {me.get('can_resume')}")
        lines.append(f"  active_operation : {_fmt(me.get('active_operation_id'))}")
        lines.append(f"  mission_id       : {_fmt(me.get('mission_id'))}")
        lines.append(f"  route hash orig  : {_fmt(me.get('original_route_hash'))}")
        lines.append(f"  route hash active: {_fmt(me.get('active_route_hash'))}")
        lines.append(f"  verified_home    : {_fmt(me.get('verified_home'))}  dist={_fmt(me.get('home_verification_distance_m'))} m")
        lines.append(f"  mode (observed)  : {_fmt(me.get('mode'))}")
        lines.append(f"  sequence         : current={_fmt(seq.get('current'))}/{_fmt(seq.get('count'))} "
                     f"before_pause={_fmt(seq.get('before_pause'))} at_resume={_fmt(seq.get('at_resume'))} "
                     f"first_after_resume={_fmt(seq.get('first_after_resume'))} "
                     f"continuation_verified={_fmt(seq.get('continuation_verified'))}")
        lines.append(f"  return           : dist_to_home={_fmt(rc.get('distance_to_home_m'))} m "
                     f"radius={_fmt(rc.get('arrival_radius_m'))} m "
                     f"persist={_fmt(rc.get('persistence_progress_s'))}/{_fmt(rc.get('persistence_s'))} s "
                     f"arrived={_fmt(rc.get('arrival_confirmed'))} final_loiter={_fmt(rc.get('final_loiter_verified'))}")
        lines.append(f"  replanning       : active={_fmt(rp.get('active'))} fsm={_fmt(rp.get('fsm_state'))}")
        if me.get("last_error"):
            lines.append(f"  last_error       : {me['last_error'].get('code')} -- {me['last_error'].get('message')}")

    lines.append("== Replanning ==")
    if replan is None:
        lines.append("  replan status UNAVAILABLE")
    else:
        pc = replan.get("planning_package_consistency", {}) or {}
        lines.append(f"  fsm_state        : {_fmt(replan.get('fsm_state'))}  running={_fmt(replan.get('running'))}")
        lines.append(f"  decision         : {_fmt(replan.get('current_decision'))}  simulated={_fmt(replan.get('simulated'))}")
        lines.append(f"  package consistency: {_fmt(pc.get('state'))}")

    lines.append("== Vehicle (observed) ==")
    lines.append(f"  authority        : {_fmt((auth or {}).get('authority'))}")
    if home is not None:
        lines.append(f"  home verified    : {home.get('verified')} ready_for_auto={home.get('ready_for_auto')} "
                     f"dist_from_vehicle={_fmt(home.get('distance_from_vehicle_m'))} m")
    if state is not None:
        tel = state.get("telemetry", {}) or {}
        mis = state.get("mission", {}) or {}
        agent = state.get("agent", {}) or {}
        lines.append(f"  mode / armed     : {_fmt(tel.get('mode_name'))} / {_fmt(tel.get('armed'))}")
        lines.append(f"  mission          : id={_fmt(mis.get('current_mission_id'))} "
                     f"seq={_fmt(mis.get('current_waypoint'))}/{_fmt(mis.get('mission_count'))} "
                     f"active={_fmt(mis.get('mission_active'))}")
        inj = agent.get("experiment_injection")
        if inj:
            lines.append(f"  active simulation: {json.dumps(inj)[:200]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Read-only mission-execution bench diagnostic.")
    ap.add_argument("--watch", action="store_true", help="refresh every 2 seconds")
    args = ap.parse_args()
    if not args.watch:
        print(snapshot())
        return
    try:
        while True:
            print("\033[2J\033[H", end="")  # clear
            print(snapshot())
            print("\n(--watch: refreshing every 2s, Ctrl+C to stop)")
            time.sleep(2)
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
