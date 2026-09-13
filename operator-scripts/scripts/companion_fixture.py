"""companion_fixture.py — OPT-IN development fixture for the companion-v2 contract.

NOT PART OF NORMAL OPERATION. Nothing imports this module at runtime and the backend never
loads it: it only runs when a developer starts it by hand, and it refuses any target that is
not this machine's loopback interface. Every vehicle it speaks for is named "(FIXTURE)", so a
screen showing its data can never be mistaken for a live Scout. Restart the backend afterwards
to drop the fixture names and state (the operator backend keeps them in memory only).

It replays the scenarios the companion contract has to survive (COMPANION_CONTRACT.md) by
POSTing Scout-shaped status envelopes to a LOCAL backend's /agent/status:

    python scripts/companion_fixture.py --list
    python scripts/companion_fixture.py --scenario active_inspection --duration 30
    python scripts/companion_fixture.py --scenario all            # every step, in order

The same builders are imported by tests/test_companion_telemetry.py, so the fixture and the
tests exercise exactly the same payloads.

SNAPSHOT SESSION/SEQ/GENERATION: every block() call needs an explicit `seq` — this module never
invents one silently, because the whole point of the session/seq mechanism (companion_telemetry.py)
is that the CALLER states its ordering intent. `next_seq()` is a simple auto-incrementing counter
for scenarios that just want "this is new content"; scenarios illustrating the ordering
guarantees (stale replay, session restart) construct their seq values explicitly.

`generation` defaults to this fixture process's own CURRENT tracked publisher generation
(`_generation_state`) and normally never needs to be passed explicitly — call `bump_generation()`
to simulate Scout's companion-reporting subsystem restarting (a genuinely HIGHER generation and a
fresh session id become "current" for every later block() call), or pass `generation=`/`session_id=`
explicitly to construct an out-of-sequence (e.g. retired) block on purpose, as the
`retired_generation_replay` scenario does. See COMPANION_CONTRACT.md's PUBLISHER GENERATIONS.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlparse

SCHEMA = "companion-v2"
SCOUT, SAR = 2, 3
SCOUT_POS = (56.69950, 13.00215)     # near the Map page's default view
SAR_POS = (56.70100, 13.00500)
UAV_POS = (56.70030, 13.00350)
BUOY = (56.70060, 13.00420)
_OMIT = object()
LOOPBACK = {"127.0.0.1", "localhost", "::1"}

# This fixture process's own tracked publisher identity: a generation (starts at 1) plus the
# session id that goes with it. A real Scout persists `generation` across its own restarts and
# increases it on every one, regenerating `id` alongside it — see `bump_generation()`, which is
# the ONLY supported way this identity changes. `SESSION_ID` is kept as a module-level alias to
# the STARTING id, for anything that wants "the fixture's very first session" specifically
# (e.g. constructing a deliberately-retired block after a bump).
SESSION_ID = f"fixture-{uuid.uuid4().hex[:12]}"
_generation_state = {"n": 1, "id": SESSION_ID}
_seq_counter = {"n": 0}


def next_seq():
    """A fresh, always-higher seq value for 'this is new/current content'."""
    _seq_counter["n"] += 1
    return _seq_counter["n"]


def current_generation():
    """(generation, session_id) this fixture process currently reports as — the values block()
    uses by default."""
    return _generation_state["n"], _generation_state["id"]


def bump_generation():
    """Simulate Scout's companion-reporting subsystem restarting: a genuinely HIGHER generation
    and a fresh session id, from this call onward, for every block() that doesn't explicitly
    override them. Returns the new (generation, session_id) so a scenario can remember the OLD
    ones too (e.g. to construct a deliberately-retired replay afterward)."""
    _generation_state["n"] += 1
    _generation_state["id"] = f"fixture-{uuid.uuid4().hex[:12]}"
    return _generation_state["n"], _generation_state["id"]


# --- payload builders ---------------------------------------------------------------------

def envelope(usv_id, ts, *, companions=_OMIT, name=None, pos=None, envelope_ts=_OMIT,
             omit_timestamp=False):
    """One Scout-shaped status envelope. `companions` omitted → a pre-companion payload.

    `ts` drives the fixture's own posting cadence / telemetry values; `envelope_ts` (default:
    same as `ts`) is what actually goes in the envelope's `timestamp` field — kept separate so a
    scenario can simulate a DELAYED or DISCONTINUOUS envelope timestamp without disturbing the
    surrounding telemetry. `omit_timestamp` drops the field entirely (a Local Agent that sends
    no timestamp at all — freshness for that packet's evidence must then read UNKNOWN)."""
    lat, lng = pos or (SCOUT_POS if usv_id == SCOUT else SAR_POS)
    payload = {
        "usv_id": usv_id,
        "name": name or ("Scout (FIXTURE)" if usv_id == SCOUT else "SAR-001 (FIXTURE)"),
        "comm_state": "CONNECTED",
        "telemetry": {"lat": lat, "lng": lng, "heading": 90, "groundspeed": 1.0,
                      "battery": 82, "mode": "AUTO", "armed": True},
        "mission": {"mission_state": "SEARCHING"},
    }
    if companions is not _OMIT:
        payload["companions"] = companions
    env = {"message_type": "status", "schema_version": "1.0", "source": f"usv-{usv_id}",
           "target": "operator", "payload": payload}
    if not omit_timestamp:
        env["timestamp"] = ts if envelope_ts is _OMIT else envelope_ts
    return env


def block(*items, seq, session_id=None, generation=None):
    """A companion-v2 block. `seq` is ALWAYS explicit (see module docstring) — pass `next_seq()`
    for "this is new/current content", or a specific value to illustrate the ordering rules.
    `session_id`/`generation` default to this fixture's CURRENT tracked publisher identity
    (`current_generation()`) — override them together to construct an out-of-sequence block on
    purpose (a retired generation, an inconsistent id/generation pairing, etc.)."""
    gen, sid = current_generation()
    return {"schema": SCHEMA,
            "session": {"id": session_id or sid, "seq": seq,
                       "generation": gen if generation is None else generation},
            "items": list(items)}


def square(center, half_m):
    """A closed 4-vertex square ring of {lat, lng} objects around `center`."""
    dlat = half_m / 111320.0
    dlng = half_m / (111320.0 * 0.5495)          # cos(56.7°)
    la, ln = center
    return [{"lat": la - dlat, "lng": ln - dlng}, {"lat": la - dlat, "lng": ln + dlng},
            {"lat": la + dlat, "lng": ln + dlng}, {"lat": la + dlat, "lng": ln - dlng},
            {"lat": la - dlat, "lng": ln - dlng}]


def uav(t, *, link="CONNECTED", peer_at=_OMIT, activity="INSPECTING", task="insp-0001",
        route_rev=3, obs_at=_OMIT, position=True, battery=71.0, inspection=40.0,
        hazards=_OMIT, assignment="ASSIGNED", parent="usv-2", companion_id="uav-1"):
    """One companion item as Scout would report it at packet time `t`."""
    peer_at = t - 0.4 if peer_at is _OMIT else peer_at
    obs_at = t - 0.5 if obs_at is _OMIT else obs_at
    item = {
        "companion_id": companion_id, "vehicle_type": "UAV", "parent_vehicle_id": parent,
        "display_name": "UAV-1 (FIXTURE)", "assignment": assignment,
        "link": {"state": link, "last_peer_contact_at": peer_at},
        "activity": {"state": activity, "task_id": task, "route_revision": route_rev},
    }
    if position:
        item["position"] = {"lat": UAV_POS[0], "lng": UAV_POS[1], "heading_deg": 45.0,
                            "altitude_m": 25.0, "altitude_ref": "AGL", "observed_at": obs_at}
    if battery is not None:
        item["battery"] = {"remaining_pct": battery, "observed_at": obs_at}
    if inspection is not None:
        item["inspection"] = {"progress_pct": inspection, "observed_at": obs_at}
    if hazards is not _OMIT:
        item["hazards"] = hazards
    return item


def buoy(observed_at, *, revision=1, disposition="REPORTED", reason=None, decided_at=None,
         replan=None):
    """The proposed-buoy hazard: a point with 8 m uncertainty and a 50 m square exclusion."""
    h = {
        "hazard_id": "hz-buoy-1", "revision": revision, "source": "uav-1:eo_camera",
        "kind": "BUOY", "observed_at": observed_at,
        "geometry": {"type": "point", "lat": BUOY[0], "lng": BUOY[1]},
        "uncertainty_m": 8.0,
        "exclusion": {"type": "polygon", "ring": square(BUOY, 25.0)},
        "disposition": {"state": disposition, "reason": reason, "decided_at": decided_at},
    }
    if replan is not None:
        h["replan"] = replan
    return h


def sar(t):
    return envelope(SAR, t)


# --- scenarios: build(t0, t, k, seq) → [envelopes]  (t0=run start, t=tick, k=tick#, seq=this
#     scenario's base seq, freshly allocated once per scenario by the runner) -----------------

def _accepted(t0):
    return buoy(t0, revision=2, disposition="ACCEPTED",
                reason="Exclusion accepted by Scout (fixture)", decided_at=t0,
                replan={"state": "IN_PROGRESS", "reason": "Computing a detour (fixture)",
                        "updated_at": t0})


def _verified(t0):
    return buoy(t0, revision=3, disposition="ACCEPTED",
                reason="Exclusion accepted by Scout (fixture)", decided_at=t0,
                replan={"state": "VERIFIED", "mission_revision": 4,
                        "route_hash": "sha256:fixture0000000000000000", "updated_at": t0,
                        "reason": "Revised mission read back and hash-verified (fixture)"})


def _stale_snapshot_replay(t0, t, k, seq):
    """Establish a valid snapshot at `seq`, then (on later ticks) replay an ENVELOPE that is
    itself newer (passes main.py's own guard) but carries the companion payload at `seq - 1` —
    an older snapshot in the SAME session. The verified hazard from tick 0 must survive
    untouched; the replayed REPORTED-only hazard must never be applied."""
    if k == 0:
        return [envelope(SCOUT, t, companions=block(uav(t, hazards=[_verified(t0)]), seq=seq))]
    return [envelope(SCOUT, t, companions=block(uav(t, hazards=[buoy(t0)]),
                                                seq=max(seq - 1, 0)))]


def _session_restart(t0, t, k, seq):
    """Tick 0 establishes companion state under the fixture's current generation. From tick 1
    the companion subsystem genuinely RESTARTS — bump_generation() gives it a higher generation
    number and a fresh session id, trusted as a publisher restart because the generation itself
    increased (never because the id merely differs — see COMPANION_CONTRACT.md's PUBLISHER
    GENERATIONS). A different companion (uav-2) makes the replacement visible; uav-1's ACTIVE
    state from tick 0 must be fully gone, not merged with it."""
    if k == 0:
        return [envelope(SCOUT, t, companions=block(uav(t, link="LOST", hazards=[]), seq=seq))]
    if k == 1:
        bump_generation()
    return [envelope(SCOUT, t, companions=block(
        uav(t, companion_id="uav-2", link="NEVER_CONNECTED", activity="IDLE", route_rev=None,
            position=False, battery=None, inspection=None, hazards=[]), seq=1))]


_retired = {}


def _retired_generation_replay(t0, t, k, seq):
    """Part 1's exact required scenario: session/generation A is accepted (tick 0); Scout's
    companion subsystem restarts to generation B (tick 1); a LATER envelope (still newer, still
    passes main.py's own per-vehicle timestamp guard) then replays A's old, now-RETIRED
    generation (tick 2+). B's state must survive completely untouched — this is what
    `_apply_session`'s generation check (not session-id trust) is for."""
    if k == 0:
        _retired["generation"], _retired["id"] = current_generation()
        return [envelope(SCOUT, t, companions=block(uav(t, link="LOST", hazards=[]), seq=seq))]
    if k == 1:
        bump_generation()
        return [envelope(SCOUT, t, companions=block(
            uav(t, companion_id="uav-2", link="CONNECTED", activity="INSPECTING", hazards=[]),
            seq=1))]
    # k >= 2: replay generation A's own old snapshot (uav-1, LOST) — same id/generation it
    # always had, now retired. If accepted, this would wrongly revert B's uav-2/CONNECTED state.
    return [envelope(SCOUT, t, companions=block(
        uav(t, companion_id="uav-1", link="LOST", hazards=[]), seq=99,
        session_id=_retired["id"], generation=_retired["generation"]))]


def _repeated_clear_after_drop(t0, t, k, seq):
    """Tick 0: UAV assigned. Tick 1 (the FIRST 'items: []' unassignment) is simply never sent —
    simulating a lost packet. Tick 2+: Scout resends its CURRENT truth (still unassigned), which
    clears the operator's state even though the first clear never arrived — because a snapshot
    is a statement of current fact Scout keeps repeating, not a one-shot event."""
    if k == 0:
        return [envelope(SCOUT, t, companions=block(uav(t, hazards=[]), seq=seq))]
    if k == 1:
        return []
    return [envelope(SCOUT, t, companions=block(seq=seq + 1))]


SCENARIOS = {
    "legacy": (
        "Older Scout payload with no companion block at all.",
        lambda t0, t, k, seq: [envelope(SCOUT, t)]),
    "assigned_no_contact": (
        "UAV assigned to Scout, never heard from yet.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, link="NEVER_CONNECTED", peer_at=None, activity="IDLE", task=None,
            route_rev=None, position=False, battery=None, inspection=None, hazards=[]),
            seq=seq))]),
    "active_inspection": (
        "UAV inspecting ahead with a fresh position.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(t, hazards=[]), seq=seq))]),
    "proposed_buoy": (
        "UAV reports a buoy and proposes an exclusion; Scout has not decided.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, inspection=55.0, hazards=[buoy(t0)]), seq=seq))]),
    "accepted_replanning": (
        "Scout accepts the exclusion and reports replanning in progress.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, inspection=60.0, hazards=[_accepted(t0)]), seq=seq))]),
    "replan_verified": (
        "Scout reports the revised mission verified on the vehicle.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, inspection=65.0, hazards=[_verified(t0)]), seq=seq))]),
    "duplicate_out_of_order": (
        "Repeated hazard ids/revisions and an out-of-order UAV position (same seq: unchanged content).",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, obs_at=(t - 0.5) if k == 0 else (t0 - 30.0),
            hazards=([_verified(t0), _accepted(t0), _verified(t0)] if k == 0
                     else [_accepted(t0)])), seq=seq))]),
    "duplicate_delivery": (
        "The exact same envelope, posted twice — must not manufacture freshness on replay.",
        lambda t0, t, k, seq: [envelope(SCOUT, t0 + 500.0, envelope_ts=t0 + 500.0,
            companions=block(uav(t0 + 500.0, obs_at=t0 + 499.5), seq=seq))]),
    "delayed_delivery": (
        "Envelope composed 30s before it 'arrives': fresh at send time, must not read as fresh now.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, envelope_ts=t - 30.0, companions=block(
            uav(t - 30.0, obs_at=t - 30.2, peer_at=t - 30.1), seq=seq))]),
    "unknown_timing": (
        "Envelope carries no timestamp at all: freshness reads UNKNOWN, never fresh.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, omit_timestamp=True,
            companions=block(uav(t, hazards=[]), seq=seq))]),
    "clock_discontinuity": (
        "Envelope timestamp looks like it's from the future: flagged as a clock anomaly. "
        "NOTE: this ALSO advances main.py's own per-vehicle envelope-timestamp high-water mark "
        "into the future (a pre-existing property of that guard, not something this module "
        "changes) — every NORMAL-timestamped packet posted afterward is rejected as stale until "
        "real time catches up. Kept LAST in ALL_ORDER for exactly this reason; run it standalone "
        "or last if you also want to see later scenarios apply normally in the same session.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, envelope_ts=t + 30.0, companions=block(
            uav(t + 30.0, obs_at=t + 29.5), seq=seq))]),
    "stale_snapshot_replay": (
        "A newer envelope carrying an OLDER companion snapshot (same generation) must not "
        "overwrite the newer one.",
        _stale_snapshot_replay),
    "session_restart": (
        "Scout's companion subsystem restarts: a HIGHER generation is trusted and replaces state.",
        _session_restart),
    "retired_generation_replay": (
        "A -> B -> a delayed, now-retired generation A snapshot: B's state must survive untouched.",
        _retired_generation_replay),
    "repeated_clear_after_drop": (
        "The first clear packet is lost; a later REPEATED clear still succeeds.",
        _repeated_clear_after_drop),
    "uav_link_lost": (
        "Scout still reporting, but it has lost the UAV: fresh packets, old UAV data.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, link="LOST", peer_at=t0 - 20.0, obs_at=t0 - 20.0, activity="UNKNOWN",
            hazards=[_verified(t0)]), seq=seq))]),
    "second_usv": (
        "A second USV (SAR-001) with no companion, beside Scout's.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(t, hazards=[]), seq=seq)),
                               sar(t)]),
    "scout_contact_loss": (
        "One Scout packet, then silence: everything Scout said becomes last-known.",
        lambda t0, t, k, seq: [envelope(SCOUT, t, companions=block(uav(
            t, hazards=[_verified(t0)]), seq=seq))] if k == 0 else []),
}

# clock_discontinuity is deliberately LAST: it posts an envelope timestamp ~30s ahead of real
# time, which advances main.py's own per-vehicle envelope-timestamp high-water mark into the
# future (see that scenario's own docstring) — every normal-timestamped packet posted
# afterward in the SAME run would be rejected as stale until real time catches up. Putting it
# last means every other scenario in `--scenario all` applies normally.
ALL_ORDER = ["legacy", "assigned_no_contact", "active_inspection", "proposed_buoy",
             "accepted_replanning", "replan_verified", "duplicate_out_of_order",
             "duplicate_delivery", "delayed_delivery", "unknown_timing",
             "stale_snapshot_replay", "session_restart", "retired_generation_replay",
             "repeated_clear_after_drop", "uav_link_lost", "scout_contact_loss",
             "clock_discontinuity"]


# --- runner --------------------------------------------------------------------------------

def check_loopback(base):
    """Refuse anything but this machine. A fixture posted to a real station would put
    fabricated Scout telemetry in front of an operator."""
    host = urlparse(base).hostname
    if host not in LOOPBACK:
        raise SystemExit(f"refusing to post fixture data to {base!r}: loopback targets only "
                         f"({', '.join(sorted(LOOPBACK))})")


def post(base, env):
    req = urllib.request.Request(base.rstrip("/") + "/agent/status",
                                 data=json.dumps(env).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def run(names, base, duration, interval, with_sar):
    check_loopback(base)
    t0 = time.time()
    for name in names:
        describe, build = SCENARIOS[name]
        print(f"[fixture] {name}: {describe}")
        seq = next_seq()      # ONE seq per scenario; reused across its ticks (unchanged content)
        start, k = time.time(), 0
        while time.time() - start < duration:
            t = time.time()
            envs = build(t0, t, k, seq)
            if with_sar and name not in ("second_usv",):
                envs = envs + [sar(t)]
            for env in envs:
                post(base, env)
            if k == 0 and name == "scout_contact_loss":
                print("[fixture]   Scout now silent: PARTITIONED after 15 s, DISCONNECTED after 30 s")
            k += 1
            time.sleep(interval)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--list", action="store_true", help="list scenarios and exit")
    ap.add_argument("--scenario", default=None, help="scenario name, or 'all'")
    ap.add_argument("--base", default="http://127.0.0.1:8199", help="LOCAL backend base URL")
    ap.add_argument("--duration", type=float, default=8.0, help="seconds per scenario")
    ap.add_argument("--interval", type=float, default=2.0, help="seconds between packets")
    ap.add_argument("--no-sar", action="store_true", help="do not post SAR-001 alongside")
    args = ap.parse_args(argv)
    if args.list or not args.scenario:
        for n, (d, _) in SCENARIOS.items():
            print(f"  {n:24s} {d}")
        return 0
    names = ALL_ORDER if args.scenario == "all" else [args.scenario]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {', '.join(unknown)}")
    try:
        run(names, args.base, args.duration, args.interval, not args.no_sar)
    except urllib.error.URLError as exc:
        raise SystemExit(f"cannot reach {args.base}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
