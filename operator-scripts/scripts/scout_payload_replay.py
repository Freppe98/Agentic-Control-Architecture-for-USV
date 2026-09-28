"""scout_payload_replay.py — replay CAPTURED Scout companion-v2 envelopes through the REAL
operator ingestion endpoint (POST /agent/status), in a runtime directory that can never be the
production one.

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

Each payload file is either ONE captured envelope (a JSON object), or a JSON array of envelopes
captured as a SEQUENCE (e.g. assignment -> hazard reported -> Scout accepts -> replan verified,
or a companion-subsystem restart) — posted to /agent/status in file order, exactly as Scout sent
them. See SCOUT_INTEGRATION_HANDOFF.md's "Handing back a compatibility capture" section for
exactly what to hand back and how to capture it.

WHAT "SUCCESS" MEANS HERE
--------------------------
An HTTP 200 from /agent/status is NOT success — the route returns 200 for almost anything
that names a known vehicle; a malformed `payload.companions` block is silently refused deeper
in the stack (companion_telemetry.ingest) without ever showing up in that response body. The
only place a companion-level rejection is visible is `GET /api/fleet/status`'s
`companions.rejections` — which is exactly what this script prints after every replay, per
touched vehicle, alongside the resulting session/seq/generation and item list. Read that output;
do not stop at "the script exited 0".
"""
import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAYLOAD_DIR = REPO_ROOT / "tests" / "fixtures" / "scout_companion_payloads"


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
    """One fixture file -> a list of envelope dicts, in POST order. A file is either a single
    envelope object or a JSON array of them (a captured sequence)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    raise ValueError(f"{path}: expected a JSON object or array of envelopes, got {type(data).__name__}")


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


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("payloads", nargs="?", default=str(DEFAULT_PAYLOAD_DIR),
                    help="a payload file, or a directory of them (default: "
                         f"{DEFAULT_PAYLOAD_DIR.relative_to(REPO_ROOT)})")
    ap.add_argument("--runtime-dir", default=None,
                    help="use this directory for runtime state instead of a fresh temp one "
                         "(still never the production runtime_data/)")
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
    touched_vehicles = set()
    for f in files:
        print(f"\n[scout-payload-replay] === {f.name} ===")
        envelopes = load_envelopes(f)
        for env, body, status in replay(client, envelopes):
            vid_raw = (env.get("payload") or {}).get("usv_id", env.get("source"))
            print(f"  POST /agent/status -> {status} {body}")
            if status != 200:
                print(f"  !! non-200 response for an envelope naming {vid_raw!r} - the outer "
                     "route did not even identify this as a known vehicle; see main.py's "
                     "extract_usv_id / known_vehicle_ids.")
            cid = operator_main.canonical_id(vid_raw)
            if cid is not None:
                touched_vehicles.add(cid)

    print("\n[scout-payload-replay] === resulting fleet companion state ===")
    fleet = client.get("/api/fleet/status").json()
    for row in fleet:
        if row["id"] not in touched_vehicles:
            continue
        print(f"\n{row.get('name') or row['id']} ({operator_main.vehicle_slug(row['id'])}):")
        print(summarize_companions(row))

    diag = client.get("/api/diagnostics").json()
    print("\n[scout-payload-replay] companion-session persistence:",
         diag.get("companion_sessions_persistence"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
