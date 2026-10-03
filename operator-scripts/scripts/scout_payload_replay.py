"""scout_payload_replay.py — replay CAPTURED Scout envelopes (companion-v2 AND quick-move-v1)
through the REAL operator ingestion endpoint (POST /agent/status), in a runtime directory that
can never be the production one.

WHY THIS EXISTS
---------------
`scripts/companion_fixture.py` and `tests/test_companion_telemetry.py` are OPERATOR-authored:
they encode this station's own understanding of companion-v2, so a payload they build proves
this module parses what WE THINK the contract says — never that it parses what Scout's actual
companion state store / publisher actually emits. This script closes that gap by replaying
REAL, off-the-wire Scout envelopes (captured the same way `tests/fixtures/scout-status-live.json`
was — see `tests/test_vehicle_telemetry.py`'s module docstring) through the exact same FastAPI
route a live Scout POSTs to, and reports what was actually accepted, rejected, and why.

ISOLATION
---------
This process forces `OPERATOR_RUNTIME_DIR` to a fresh temporary directory BEFORE importing
`main` (see `main.py`'s `_resolve_runtime_dir`), so it can never resolve, read or write the
station's real `runtime_data/` — no live vehicle state and no production companion-session
generation record is reachable from here, whether or not this process happens to be started
under a test runner. `main.is_production_mission_store()` is asserted False as a second,
independent check before anything is replayed.

USAGE
-----
    python scripts/scout_payload_replay.py
        # replays every *.json file in tests/fixtures/scout_companion_payloads/ (if any)

    python scripts/scout_payload_replay.py path/to/one_capture.json
    python scripts/scout_payload_replay.py path/to/a/directory/

    python scripts/scout_payload_replay.py tests/fixtures/scout_quick_move_payloads/ \
        --correlate-commands
        # quick-move captures: see CORRELATED COMMAND IDS below

Each payload file is either ONE captured envelope (a JSON object), or a JSON array of envelopes
captured as a SEQUENCE (e.g. assignment -> hazard reported -> Scout accepts -> replan verified,
or a companion-subsystem restart) — posted to /agent/status in file order, exactly as Scout sent
them. See SCOUT_INTEGRATION_HANDOFF.md's "Handing back a compatibility capture" section for
exactly what to hand back and how to capture it.

CORRELATED COMMAND IDS (quick-move captures only)
-------------------------------------------------
A quick move is CORRELATED: `payload.quick_move.active.command_id` names an operator command,
and the operator deliberately refuses a status for an id it never issued
(`unknown_command_id`) so a delayed or foreign report can never put a target on the map. A
capture recorded on Scout therefore carries ids from SCOUT's run, which this operator process
has never issued — so a bare replay proves only that the block PARSES, and every status is
(correctly) refused.

`--correlate-commands` closes that gap honestly rather than by weakening the guard: for each
distinct `command_id` in the capture, it issues a REAL `POST /api/commands` quick move to the
same vehicle and rewrites that id — and ONLY that id — to the one the operator actually
minted. The capture files on disk are never modified; the rewrite happens in memory, and every
rewrite is printed. Ordering, timing, states and every other field are left exactly as Scout
recorded them.

WHAT THIS DOES AND DOES NOT PROVE
----------------------------------
It proves REPORTING COMPATIBILITY: that this operator build parses, orders, correlates and
renders what Scout's publisher actually emits. It does NOT prove DELIVERY of a real
operator-generated command to a real vehicle, and it is not evidence that any vehicle moved.
Those need the bench steps in QUICK_MOVE_HANDOFF.md.

WHAT "SUCCESS" MEANS HERE
--------------------------
An HTTP 200 from /agent/status is NOT success — the route returns 200 for almost anything
that names a known vehicle; a malformed `payload.companions` block is silently refused deeper
in the stack (companion_telemetry.ingest) without ever showing up in that response body. The
only place a companion-level rejection is visible is `GET /api/fleet/status`'s
`companions.rejections` — which is exactly what this script prints after every replay, per
touched vehicle, alongside the resulting session/seq/generation and item list. The same is true
of `quick_move.rejections`. Read that output; do not stop at "the script exited 0".
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAYLOAD_DIR = REPO_ROOT / "tests" / "fixtures" / "scout_companion_payloads"
QUICK_MOVE_PAYLOAD_DIR = REPO_ROOT / "tests" / "fixtures" / "scout_quick_move_payloads"


def _force_isolated_runtime_dir(explicit=None):
    """Set OPERATOR_RUNTIME_DIR BEFORE `main` is imported anywhere in this process, so
    `main._resolve_runtime_dir()` takes its "env" branch unconditionally (see that function and
    tests/test_mission_store_isolation.py's `test_an_explicit_override_still_wins`) rather than
    depending on this script happening to be started under a test runner. Returns the directory
    actually used."""
    import tempfile
    path = explicit or tempfile.mkdtemp(prefix="scout-companion-replay-")
    Path(path).mkdir(parents=True, exist_ok=True)
    os.environ["OPERATOR_RUNTIME_DIR"] = str(path)
    return str(path)


def load_envelopes(path):
    """One fixture file -> a FLAT list of envelope dicts, in POST order. A file is either a
    single envelope object or a JSON array of them (a captured sequence).

    For a file that carries several INDEPENDENT scenarios, use load_scenario_groups() instead —
    this function deliberately keeps its original "the envelopes in this file" meaning, because
    the companion harness and its tests depend on exactly that."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        scenarios = data.get("scenarios")
        if isinstance(scenarios, dict):
            return [env for envs in scenarios.values() if isinstance(envs, list) for env in envs]
        return [data]
    raise ValueError(f"{path}: expected a JSON object or array of envelopes, got {type(data).__name__}")


def load_scenario_groups(path):
    """One fixture file -> [(scenario_name, [envelope, ...]), ...], in POST order.

    Three accepted shapes, so a real Scout capture export drops in unchanged:
      * a single envelope object                -> one group, named after the file
      * a JSON array of envelopes               -> one group (a captured sequence)
      * {"scenarios": {name: [envelope, ...]}}  -> one group PER SCENARIO
    The third exists because Scout's fixture exports several INDEPENDENT scenarios. Replaying
    them as one stream lets one scenario's accepted state satisfy the next one's preconditions
    — and, worse, lets main.py's per-vehicle monotonic envelope guard silently refuse every
    scenario after the first as `stale_timestamp` while the run still looks like a pass. See
    isolate_scenario()."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("scenarios"), dict):
        return [(name, envs) for name, envs in data["scenarios"].items()
                if isinstance(envs, list)]
    return [(Path(path).stem, load_envelopes(path))]


def discover_payload_files(directory):
    d = Path(directory)
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.json") if p.is_file())


def replay(client, envelopes):
    """POST each envelope through the REAL /agent/status route, in order. Returns
    [(envelope, response_json_or_None, http_status), ...] — never raises on a non-200 response;
    that is itself a compatibility finding for the caller to report, not a harness bug."""
    results = []
    for env in envelopes:
        r = client.post("/agent/status", json=env)
        try:
            body = r.json()
        except Exception:
            body = None
        results.append((env, body, r.status_code))
    return results


def summarize_companions(fleet_row):
    """A human-readable compatibility summary for one fleet row's `companions` block — what was
    actually ACCEPTED and REJECTED, and why. Never just 'HTTP 200' (see module docstring)."""
    blk = (fleet_row or {}).get("companions") or {}
    lines = [f"  schema={blk.get('schema')!r} reported={blk.get('reported')} "
             f"cleared={blk.get('cleared')} block_age_s={blk.get('block_age_s')}"]
    session = blk.get("session")
    if session:
        lines.append(f"  session: id={session.get('id')!r} seq={session.get('seq')} "
                     f"generation={session.get('generation')} age_s={session.get('age_s')}")
    else:
        lines.append("  session: none accepted yet")
    rej = blk.get("rejections") or {}
    if rej.get("count"):
        lines.append(f"  REJECTIONS: {rej['count']} total - {rej.get('by_reason')} "
                     f"(last: {rej.get('last_reason')})")
    else:
        lines.append("  rejections: none")
    items = blk.get("items") or []
    if not items:
        lines.append("  items: none")
    for item in items:
        link = item.get("link") or {}
        lines.append(f"  item {item.get('companion_id')!r}: assignment={item.get('assignment_state')} "
                     f"link={link.get('state')} status_age_s={item.get('status_age_s')} "
                     f"hazards={len(item.get('hazards') or [])}")
    return "\n".join(lines)


def summarize_quick_move(fleet_row):
    """A human-readable compatibility summary for one fleet row's `quick_move` block. Like
    summarize_companions, this is the ONLY place a block-level refusal is visible — the
    /agent/status route returns 200 for a quick_move block it silently refused."""
    blk = (fleet_row or {}).get("quick_move") or {}
    cap = blk.get("capability") or {}
    lines = [f"  schema={blk.get('schema')!r} reported={blk.get('reported')} "
             f"block_age_s={blk.get('block_age_s')}"]
    lines.append(f"  capability: supported={cap.get('supported')} source={cap.get('source')!r} "
                 f"nav_ready={cap.get('nav_ready')} max_distance_m={cap.get('max_distance_m')} "
                 f"reason={cap.get('reason')!r}")
    rej = blk.get("rejections") or {}
    if rej.get("count"):
        lines.append(f"  REJECTIONS: {rej['count']} total - {rej.get('by_reason')} "
                     f"(last: {rej.get('last_reason')})")
    else:
        lines.append("  rejections: none")
    cur = blk.get("current")
    if not cur:
        lines.append("  current: none tracked")
        return "\n".join(lines)
    lines.append(f"  current: command_id={cur.get('command_id')!r} "
                 f"delivery_confirmed={cur.get('delivery_confirmed')} "
                 f"requested_age_s={cur.get('requested_age_s')}")
    if cur.get("cancel_requested"):
        lines.append(f"    cancel_requested: {cur['cancel_requested']}")
    st = cur.get("status")
    if not st:
        lines.append("    status: NONE - Scout has not reported a movement for this id")
        return "\n".join(lines)
    lines.append(f"    state={st.get('state')} terminal={st.get('terminal')} "
                 f"completion={st.get('completion')}")
    lines.append(f"    arrival_confirmed={st.get('arrival_confirmed')} "
                 f"loiter_verified={st.get('loiter_verified')} mode={st.get('mode')!r} "
                 f"mode_incoherent={st.get('mode_incoherent')}")
    lines.append(f"    distance_remaining_m={st.get('distance_remaining_m')} "
                 f"target_mismatch={st.get('target_mismatch')} "
                 f"target_offset_m={st.get('target_offset_m')}")
    lines.append(f"    status_age_s={st.get('status_age_s')} updated={st.get('updated')}")
    if st.get("reason"):
        lines.append(f"    reason={st['reason']!r}")
    return "\n".join(lines)


def quick_move_command_ids(envelopes):
    """Every (vehicle_id_as_written, command_id) a quick-move capture reports on, in first-seen
    order. Ordering is preserved because a later scenario may supersede an earlier one, and
    minting the replacement command out of order would not reproduce that."""
    seen, out = set(), []
    for env in envelopes:
        payload = env.get("payload") or {}
        blk = payload.get("quick_move")
        if not isinstance(blk, dict):
            continue
        active = blk.get("active")
        if not isinstance(active, dict):
            continue
        cid = active.get("command_id")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append((payload.get("usv_id", env.get("source")), cid))
    return out


def rewrite_command_ids(envelopes, mapping):
    """Return a DEEP-COPIED envelope list with each captured command_id replaced by the
    operator-minted one. The capture files on disk are never touched — a capture is evidence,
    and evidence that the harness edits in place is not evidence any more."""
    import copy
    out = copy.deepcopy(envelopes)
    for env in out:
        blk = ((env.get("payload") or {}).get("quick_move") or {})
        active = blk.get("active") if isinstance(blk, dict) else None
        if isinstance(active, dict) and active.get("command_id") in mapping:
            active["command_id"] = mapping[active["command_id"]]
    return out


def correlate(client, operator_main, envelopes, verbose=True):
    """Mint a real operator QUICK_MOVE per captured command_id and return the id mapping.

    Each one goes through the REAL POST /api/commands, so the operator's own dispatch gate
    applies. A refusal is reported and that id is left unmapped (its statuses will then be
    refused as unknown_command_id, which is the honest outcome, not a harness failure)."""
    mapping = {}
    for vid_raw, captured_id in quick_move_command_ids(envelopes):
        target = None
        for env in envelopes:
            active = (((env.get("payload") or {}).get("quick_move") or {}).get("active"))
            if isinstance(active, dict) and active.get("command_id") == captured_id:
                target = active.get("target")
                break
        if not isinstance(target, dict):
            if verbose:
                print(f"  !! captured command {captured_id!r} has no usable target - not correlated")
            continue
        r = client.post("/api/commands", json={
            "vehicle_id": vid_raw, "type": "QUICK_MOVE", "params": {"target": target}})
        body = r.json() if r.content else {}
        if r.status_code == 200 and body.get("command"):
            mapping[captured_id] = body["command"]["id"]
            if verbose:
                print(f"  correlated {captured_id!r} -> {body['command']['id']!r} "
                      f"(real POST /api/commands, vehicle {vid_raw!r})")
        elif verbose:
            print(f"  !! could not mint a command for {captured_id!r} on vehicle {vid_raw!r}: "
                  f"HTTP {r.status_code} {body.get('code')} {body.get('message')}")
    return mapping


def envelope_vehicle_ids(operator_main, envelopes):
    """Canonical vehicle ids an envelope list names."""
    out = set()
    for env in envelopes:
        raw = (env.get("payload") or {}).get("usv_id", env.get("source"))
        cid = operator_main.canonical_id(raw)
        if cid is not None:
            out.add(cid)
    return out


def isolate_scenario(operator_main, vehicle_ids):
    """Reset per-vehicle state so an INDEPENDENT scenario starts from a clean slate.

    THIS IS NOT COSMETIC. main.py enforces a per-vehicle MONOTONIC envelope-timestamp guard:
    a packet older than the newest one already accepted for that vehicle is refused outright,
    before any quick_move or companions block is ever parsed. Scout exports its scenarios
    independently, so each one starts at its own t0 — which is in the PAST relative to the
    previous scenario's last envelope. Replayed into shared state, every scenario after the
    first is silently dropped as `stale_timestamp`, the fleet row keeps showing the previous
    scenario's result, and the run LOOKS like a pass.

    That is a property of the replay harness, not a defect in the guard: the guard is doing
    exactly its job (refusing a replayed older packet), and a real Scout never emits two
    scenarios on one vehicle with overlapping clocks. So the fix belongs here — clear the
    high-water mark and the per-vehicle blocks between scenarios, as if each scenario were a
    separate run. Command records are deliberately NOT cleared: correlation ids minted for an
    earlier scenario must stay retired, so a cross-scenario leak is still caught."""
    for vid in vehicle_ids:
        operator_main.latest_msg_ts_by_id.pop(vid, None)
        operator_main.quick_move_state_by_id.pop(vid, None)
        operator_main.companion_state_by_id.pop(vid, None)
        operator_main._last_quick_move_state.pop(vid, None)


def replay_group(client, operator_main, envelopes, args):
    """Replay ONE scenario, in order, and return the canonical vehicle ids it touched.

    Scenarios are replayed as separate GROUPS because Scout's exported scenarios are
    INDEPENDENT of one another: streaming them together would let one scenario's accepted
    state satisfy the next one's preconditions, and a refusal that should have been visible
    would quietly pass."""
    touched = set()
    isolate_scenario(operator_main, envelope_vehicle_ids(operator_main, envelopes))
    if args.correlate_commands:
        # The capability block has to land BEFORE a command can be minted, so replay the
        # capture once to establish capability/telemetry, then correlate, then replay the
        # rewritten sequence in its ORIGINAL order. The first pass's statuses are refused as
        # unknown_command_id by design — that is the guard working, not a failure.
        replay(client, envelopes)
        for env in envelopes:
            vid_raw = (env.get("payload") or {}).get("usv_id", env.get("source"))
            cid = operator_main.canonical_id(vid_raw)
            if cid is not None:
                # A quick move is only creatable while the operator believes the link is
                # current. The replay is not driving the 1 Hz comms monitor, so state it
                # explicitly rather than letting a harness artifact look like a gate failure.
                operator_main.comms_state_by_id[cid] = "CONNECTED"
        mapping = correlate(client, operator_main, envelopes)
        envelopes = rewrite_command_ids(envelopes, mapping)
        # The correlation pass above already consumed these exact timestamps, so the REAL
        # pass below would be refused wholesale as `stale_timestamp` by the per-vehicle
        # monotonic guard. Clear only the high-water mark — the capability the first pass
        # established has to survive, or the rewritten statuses lose their vehicle.
        for vid in envelope_vehicle_ids(operator_main, envelopes):
            operator_main.latest_msg_ts_by_id.pop(vid, None)

    for env, body, status in replay(client, envelopes):
        vid_raw = (env.get("payload") or {}).get("usv_id", env.get("source"))
        print(f"  POST /agent/status -> {status} {body}")
        if isinstance(body, dict) and body.get("stale"):
            # A 200 that changed nothing. Called out explicitly because it is the single
            # easiest way for a replay to look like a pass while having proved nothing.
            print(f"  !! REFUSED AS STALE ({body.get('reason')}) - this envelope changed NO "
                  "state. Its timestamp is older than one already accepted for this vehicle.")
        if status != 200:
            print(f"  !! non-200 response for an envelope naming {vid_raw!r} - the outer "
                  "route did not even identify this as a known vehicle; see main.py's "
                  "extract_usv_id / known_vehicle_ids.")
        cid = operator_main.canonical_id(vid_raw)
        if cid is not None:
            touched.add(cid)
            operator_main.comms_state_by_id[cid] = "CONNECTED"

    # PER-SCENARIO state, printed here rather than only once at the end. Scenarios that share
    # a vehicle overwrite each other, so a single summary after the whole run shows only the
    # last one — and would hide every earlier scenario's actual outcome behind it.
    fleet = {row["id"]: row for row in client.get("/api/fleet/status").json()}
    for cid in sorted(touched, key=str):
        row = fleet.get(cid)
        if row and (row.get("quick_move") or {}).get("reported"):
            print("  -- resulting quick_move state --")
            print(summarize_quick_move(row))
    return touched


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("payloads", nargs="?", default=str(DEFAULT_PAYLOAD_DIR),
                    help="a payload file, or a directory of them (default: "
                         f"{DEFAULT_PAYLOAD_DIR.relative_to(REPO_ROOT)})")
    ap.add_argument("--runtime-dir", default=None,
                    help="use this directory for runtime state instead of a fresh temp one "
                         "(still never the production runtime_data/)")
    ap.add_argument("--correlate-commands", action="store_true",
                    help="quick-move captures: mint a REAL operator QUICK_MOVE per captured "
                         "command_id and rewrite that id in memory, so the operator's "
                         "correlation guard is satisfied without being weakened. The capture "
                         "files on disk are never modified.")
    ap.add_argument("--authority", default="OPERATOR",
                    help="the control authority the stubbed live read reports, so a correlated "
                         "command can be minted without a reachable Scout (default: OPERATOR)")
    args = ap.parse_args(argv)

    runtime_dir = _force_isolated_runtime_dir(args.runtime_dir)
    sys.path.insert(0, str(REPO_ROOT))
    import main as operator_main  # imported AFTER the env var is set — see _resolve_runtime_dir
    from fastapi.testclient import TestClient

    if operator_main.is_production_mission_store():
        raise SystemExit("refusing to run: this process resolved the PRODUCTION runtime "
                         "directory — this should be impossible after setting OPERATOR_RUNTIME_DIR")
    print(f"[scout-payload-replay] isolated runtime dir: {runtime_dir}")
    print(f"[scout-payload-replay] companion sessions file: {operator_main.COMPANION_SESSIONS_PATH}")

    target = Path(args.payloads)
    files = [target] if target.is_file() else discover_payload_files(target)
    if not files:
        print(f"\n[scout-payload-replay] no payload files found at {target} - nothing to replay.")
        print("See SCOUT_INTEGRATION_HANDOFF.md's 'Handing back a compatibility capture' section "
              "for exactly what to supply: one real POST /agent/status body per file (or a JSON "
              "array of them for a sequence), captured off the wire exactly as "
              "tests/fixtures/scout-status-live.json was.")
        return 0

    client = TestClient(operator_main.app)
    if args.correlate_commands:
        # The dispatch gate re-reads authority LIVE from Scout, and there is no Scout here.
        # Stubbing the READ is the narrow thing to stub: every other prerequisite (capability
        # advertised, armed, link current) still has to be genuinely satisfied by the capture
        # itself, so a gate failure is still a real finding.
        operator_main.read_control_authority = lambda vid: {
            "ok": True, "vehicle_id": vid, "available": True, "reachable": True,
            "authority": args.authority}
        print(f"[scout-payload-replay] authority read stubbed as {args.authority!r} "
              "(correlation mode only)")

    touched_vehicles = set()
    for f in files:
        for scenario, envelopes in load_scenario_groups(f):
            print(f"\n[scout-payload-replay] === {f.name} :: {scenario} ===")
            touched_vehicles |= replay_group(client, operator_main, envelopes, args)

    print("\n[scout-payload-replay] === resulting fleet state ===")
    for row in client.get("/api/fleet/status").json():
        if row["id"] not in touched_vehicles:
            continue
        print(f"\n{row.get('name') or row['id']} ({operator_main.vehicle_slug(row['id'])}):")
        companions_seen = (row.get("companions") or {}).get("reported")
        quick_move_seen = (row.get("quick_move") or {}).get("reported")
        if companions_seen:
            print(" companions:")
            print(summarize_companions(row))
        if quick_move_seen:
            print(" quick_move:")
            print(summarize_quick_move(row))
        if not companions_seen and not quick_move_seen:
            print("  no companion or quick-move block was accepted for this vehicle")

    diag = client.get("/api/diagnostics").json()
    print("\n[scout-payload-replay] companion-session persistence:",
         diag.get("companion_sessions_persistence"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
