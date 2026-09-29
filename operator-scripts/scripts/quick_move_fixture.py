"""quick_move_fixture.py — OPT-IN development fixture for the `quick-move-v1` contract.

NOT PART OF NORMAL OPERATION, AND NOT A VEHICLE. Nothing imports this module at runtime and
the backend never loads it: it only runs when a developer starts it by hand, and it refuses
any target that is not this machine's loopback interface. Every vehicle it speaks for is
named "(FIXTURE)", so a screen showing its data can never be mistaken for a live Scout.
Restart the backend afterwards to drop the fixture names and state (the operator backend
keeps quick-move state in memory only).

IT SENDS NOTHING TO A VEHICLE. It POSTs Scout-SHAPED status envelopes to a LOCAL operator
backend's /agent/status — the same direction a real Scout reports in. There is no code path
here that commands anything, and simulating Scout is the ONLY thing it does:

    python scripts/quick_move_fixture.py --list
    python scripts/quick_move_fixture.py --scenario arrive_and_hold
    python scripts/quick_move_fixture.py --scenario all

The builders below are imported by tests/test_quick_move.py, so the fixture and the tests
exercise exactly the same payloads.

A scenario that needs a real operator-issued command id takes one: a quick move is
CORRELATED by command_id (quick_move.py), and a fixture that invented its own would only
ever exercise the `unknown_command_id` rejection. Pass `--command-id` with the id from a
real POST /api/commands, or let `--issue` create one against the local backend first.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

SCHEMA = "quick-move-v1"
SCOUT = 2
SCOUT_POS = (56.69950, 13.00215)      # near the Map page's default view
TARGET = (56.70050, 13.00380)         # ~115 m north-east of SCOUT_POS
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


# --- payload builders (shared with tests/test_quick_move.py) --------------------------

def capability(supported=True, *, reason=None, nav_ready=None, max_distance_m=2000.0,
               vehicle_class="SURFACE"):
    """Scout's advertisement. ABSENCE of this object means unsupported everywhere in the
    station, so a scenario that wants "this vehicle cannot do it" omits the block or sends
    `supported: false` — it never simply leaves the field out and hopes."""
    cap = {"supported": supported, "vehicle_class": vehicle_class}
    if reason is not None:
        cap["reason"] = reason
    if nav_ready is not None:
        cap["nav_ready"] = nav_ready
    if max_distance_m is not None:
        cap["max_distance_m"] = max_distance_m
    return cap


def active(command_id, state, *, target=TARGET, updated_at, distance_remaining_m=None,
           arrival_confirmed=None, loiter_verified=None, mode=None, reason=None):
    """One movement status. `updated_at` is REQUIRED and explicit: it is Scout's own clock
    reading for the observation, and the operator's whole freshness model is built on it —
    a fixture that defaulted it to "now" would make every scenario look artificially fresh
    and would never exercise the delayed-delivery case this station exists for."""
    out = {"command_id": command_id, "state": state, "updated_at": updated_at,
           "target": {"lat": target[0], "lng": target[1]}}
    if distance_remaining_m is not None:
        out["distance_remaining_m"] = distance_remaining_m
    if arrival_confirmed is not None:
        out["arrival_confirmed"] = arrival_confirmed
    if loiter_verified is not None:
        out["loiter_verified"] = loiter_verified
    if mode is not None:
        out["mode"] = mode
    if reason is not None:
        out["reason"] = reason
    return out


def block(*, cap=None, active_status=None, include_active=True, schema=SCHEMA):
    """The `payload.quick_move` block. `include_active=False` builds a capability-only
    heartbeat (the key is ABSENT, which means "no information"), distinct from
    `active_status=None` (the key is present and null, which means "nothing is running")."""
    out = {"schema": schema}
    if cap is not None:
        out["capability"] = cap
    if include_active:
        out["active"] = active_status
    return out


def envelope(quick_move_block, *, vid=SCOUT, timestamp=None, pos=SCOUT_POS, armed=True,
             mode="GUIDED"):
    """A Scout status envelope carrying a quick-move block, with just enough telemetry for
    the operator's dispatch gate to have something real to read."""
    ts = time.time() if timestamp is None else timestamp
    return {
        "message_type": "status", "schema_version": "1.0",
        "source": "usv-%d" % vid, "target": "operator", "timestamp": ts,
        "payload": {
            "usv_id": vid, "name": "Scout (FIXTURE)", "comm_state": "CONNECTED",
            "telemetry": {"lat": pos[0], "lng": pos[1], "armed": armed, "mode": mode,
                          "battery": 74, "groundspeed": 0.8, "heading": 45},
            "quick_move": quick_move_block,
        },
    }


# --- scenarios ------------------------------------------------------------------------
# Each returns a list of (label, envelope, sleep_after_s). Labels are printed so a
# developer watching the Map knows which step produced what is on screen.

def _t(base, offset):
    return base + offset


def sc_capability_only(cmd_id, base):
    """Scout advertises Quick move and nothing is running. The menu item becomes available;
    no target marker, no status card."""
    return [("capability advertised, nothing active",
             envelope(block(cap=capability(), active_status=None), timestamp=_t(base, 0)), 2)]


def sc_unsupported(cmd_id, base):
    """Scout explicitly reports Quick move unavailable. The menu item must be disabled with
    Scout's own reason, never silently missing."""
    return [("capability: supported=false",
             envelope(block(cap=capability(False, reason="GUIDED mode is not available on "
                                                         "this vehicle build."),
                            active_status=None), timestamp=_t(base, 0)), 2)]


def sc_arrive_and_hold(cmd_id, base):
    """The happy path, and the one the whole contract is shaped around: STARTING → MOVING →
    ARRIVED → COMPLETED, where COMPLETED carries BOTH arrival_confirmed and loiter_verified.
    Only this last step may read as a confirmed, finished hold."""
    return [
        ("STARTING (entering GUIDED)",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "STARTING", updated_at=_t(base, 0), mode="GUIDED")), timestamp=_t(base, 0)), 3),
        ("MOVING (115 m to run)",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "MOVING", updated_at=_t(base, 3), distance_remaining_m=115.0,
             mode="GUIDED")), timestamp=_t(base, 3)), 3),
        ("MOVING (40 m to run)",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "MOVING", updated_at=_t(base, 6), distance_remaining_m=40.0,
             mode="GUIDED")), timestamp=_t(base, 6)), 3),
        ("ARRIVED — at target, LOITER not yet verified",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "ARRIVED", updated_at=_t(base, 9), distance_remaining_m=1.2,
             arrival_confirmed=True, loiter_verified=False, mode="GUIDED")),
             timestamp=_t(base, 9)), 3),
        ("COMPLETED — arrival confirmed AND LOITER verified",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "COMPLETED", updated_at=_t(base, 12), distance_remaining_m=0.6,
             arrival_confirmed=True, loiter_verified=True, mode="LOITER")),
             timestamp=_t(base, 12)), 3),
    ]


def sc_completed_without_evidence(cmd_id, base):
    """Scout claims COMPLETED but confirms neither arrival nor LOITER. The operator must
    show the claim AND the caveat — never a green, finished hold."""
    return [("COMPLETED with no arrival/LOITER evidence",
             envelope(block(cap=capability(), active_status=active(
                 cmd_id, "COMPLETED", updated_at=_t(base, 0))), timestamp=_t(base, 0)), 3)]


def sc_cancelled_by_loiter(cmd_id, base):
    """Moving, then the operator presses Stop / LOITER and Scout reports CANCELLED. Press
    Stop on the card between the two steps to see the "stop requested" state in between."""
    return [
        ("MOVING",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "MOVING", updated_at=_t(base, 0), distance_remaining_m=90.0,
             mode="GUIDED")), timestamp=_t(base, 0)), 8),
        ("CANCELLED (operator LOITER)",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "CANCELLED", updated_at=_t(base, 8), mode="LOITER",
             reason="Cancelled by an operator LOITER command.")), timestamp=_t(base, 8)), 3),
    ]


def sc_failed(cmd_id, base):
    """Scout could not perform the move. A readable reason is mandatory — a bare FAILED
    tells the operator nothing they can act on."""
    return [("FAILED",
             envelope(block(cap=capability(), active_status=active(
                 cmd_id, "FAILED", updated_at=_t(base, 0), mode="LOITER",
                 reason="GUIDED mode was refused by the flight controller.")),
                 timestamp=_t(base, 0)), 3)]


def sc_stale_replay(cmd_id, base):
    """An out-of-order replay: a MOVING status stamped EARLIER arrives after a later one.
    It must be rejected (`stale_status`), leaving the newer state on screen — not rewind the
    distance remaining. Watch the rejection counter on the fleet row."""
    return [
        ("MOVING (t+6, 40 m)",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "MOVING", updated_at=_t(base, 6), distance_remaining_m=40.0)),
             timestamp=_t(base, 6)), 3),
        ("replayed MOVING (t+2, 115 m) — must be REFUSED as stale",
         envelope(block(cap=capability(), active_status=active(
             cmd_id, "MOVING", updated_at=_t(base, 2), distance_remaining_m=115.0)),
             timestamp=_t(base, 7)), 3),
    ]


def sc_superseded_status(cmd_id, base):
    """A status for a command id this station is NOT tracking. This is what a delayed report
    about an already-replaced move looks like, and it must never restore the old target."""
    return [("status for an unknown/retired command id — must be REFUSED",
             envelope(block(cap=capability(), active_status=active(
                 "00000000-0000-0000-0000-000000000000", "MOVING",
                 target=(56.70200, 13.00900), updated_at=_t(base, 0),
                 distance_remaining_m=300.0)), timestamp=_t(base, 0)), 3)]


def sc_target_mismatch(cmd_id, base):
    """Scout echoes a target well away from the one the operator clicked. The card must call
    that out — it is the one failure that would otherwise look like a healthy move."""
    return [("MOVING toward a DIFFERENT target than the one issued",
             envelope(block(cap=capability(), active_status=active(
                 cmd_id, "MOVING", target=(56.70120, 13.00600), updated_at=_t(base, 0),
                 distance_remaining_m=60.0)), timestamp=_t(base, 0)), 3)]


def sc_delayed_delivery(cmd_id, base):
    """An envelope composed 30 s before it is delivered. `min_age_s` stays an honest lower
    bound and `reporting_delay_s` shows the delay as a labelled ESTIMATE — the station never
    reports this as a fresh reading. Same model as COMPANION_CONTRACT.md section 3."""
    return [("MOVING, composed 30 s ago, delivered now",
             envelope(block(cap=capability(), active_status=active(
                 cmd_id, "MOVING", updated_at=time.time() - 30.2,
                 distance_remaining_m=70.0)), timestamp=time.time() - 30.0), 3)]


SCENARIOS = {
    "capability_only": sc_capability_only,
    "unsupported": sc_unsupported,
    "arrive_and_hold": sc_arrive_and_hold,
    "completed_without_evidence": sc_completed_without_evidence,
    "cancelled_by_loiter": sc_cancelled_by_loiter,
    "failed": sc_failed,
    "stale_replay": sc_stale_replay,
    "superseded_status": sc_superseded_status,
    "target_mismatch": sc_target_mismatch,
    "delayed_delivery": sc_delayed_delivery,
}
ALL_ORDER = [
    "capability_only", "arrive_and_hold", "completed_without_evidence",
    "cancelled_by_loiter", "failed", "target_mismatch", "stale_replay",
    "superseded_status", "delayed_delivery", "unsupported",
]


# --- transport (loopback only) --------------------------------------------------------

def _require_loopback(base_url):
    host = urlparse(base_url).hostname
    if host not in LOOPBACK:
        raise SystemExit("refusing to POST to a non-loopback host: %r. This fixture speaks "
                         "for a vehicle; it must never reach anything but a local backend "
                         "under a developer's own eyes." % (host,))


def _post(base_url, path, body):
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def issue_command(base_url, vid, target=TARGET):
    """Create a REAL quick move against the local backend and return its command id, so the
    scenarios below report against an id the operator station is actually tracking. This is
    a normal POST /api/commands: the backend's own dispatch gate applies, so it can refuse —
    which is itself useful output, not a fixture failure to paper over."""
    code, data = _post(base_url, "/api/commands", {
        "vehicle_id": vid, "type": "QUICK_MOVE",
        "params": {"target": {"lat": target[0], "lng": target[1]}}})
    if code != 200 or not data.get("command"):
        raise SystemExit("could not issue a quick move (HTTP %s): %s\n"
                         "The backend refused it — that is the dispatch gate working. Take "
                         "OPERATOR control, arm the vehicle and make sure a capability block "
                         "has been reported first (--scenario capability_only)."
                         % (code, data.get("message") or data))
    return data["command"]["id"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8210",
                    help="local operator backend (loopback only)")
    ap.add_argument("--scenario", default=None, help="scenario name, or 'all'")
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--vehicle", type=int, default=SCOUT)
    ap.add_argument("--command-id", default=None,
                    help="report against this existing quick-move command id")
    ap.add_argument("--issue", action="store_true",
                    help="create a real QUICK_MOVE first and report against its id")
    args = ap.parse_args(argv)

    if args.list or not args.scenario:
        print("scenarios:")
        for name in ALL_ORDER:
            doc = (SCENARIOS[name].__doc__ or "").strip().split("\n")[0]
            print("  %-28s %s" % (name, doc))
        return 0

    _require_loopback(args.base)
    cmd_id = args.command_id
    if args.issue:
        cmd_id = issue_command(args.base, args.vehicle)
        print("[fixture] issued QUICK_MOVE %s" % cmd_id)
    if cmd_id is None:
        cmd_id = "fixture-no-such-command"
        print("[fixture] no --command-id given: statuses will be REFUSED as unknown_command_id. "
              "That is a real scenario, but pass --issue to exercise the applied path.")

    names = ALL_ORDER if args.scenario == "all" else [args.scenario]
    for name in names:
        if name not in SCENARIOS:
            raise SystemExit("unknown scenario %r (try --list)" % name)
        base_ts = time.time()
        print("\n[fixture] === %s ===" % name)
        for label, env, pause in SCENARIOS[name](cmd_id, base_ts):
            env["payload"]["usv_id"] = args.vehicle
            env["source"] = "usv-%d" % args.vehicle
            code, _ = _post(args.base, "/agent/status", env)
            print("[fixture] %-58s HTTP %s" % (label, code))
            time.sleep(pause)
    return 0


if __name__ == "__main__":
    sys.exit(main())
