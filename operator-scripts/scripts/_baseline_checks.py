"""Operator-baseline runtime checks (invoked by scripts/check_operator_baseline.ps1).

In-process, read-only sanity of the operator backend — NOT a load test and NOT a
second test framework. Run from the operator-scripts directory so `import main`
resolves. Prints a per-check line and, at the end, BASELINE-CHECKS PASS / FAIL and
exits 0/1 so the PowerShell wrapper can aggregate it with the test suites.

Covers exactly the things the mission-revision / auto-refresh flow depends on:
  - backend imports (no import/startup error),
  - the route table has no duplicate (method, path) — a stale/duplicate handler is
    a classic cause of a request hitting the wrong code,
  - the frontend's api.js endpoints all resolve to a real backend route (no call to
    an obsolete/renamed endpoint),
  - the fleet endpoint answers with a list of uniquely-identified vehicles,
  - the Pixhawk-mission readback answers with its stable schema for a known vehicle
    and an honest JSON 404 for an unknown one (per-USV routing sanity).
"""
import re
import sys
from pathlib import Path

# Resolve `import main` regardless of the caller's cwd: main.py lives one level up from
# this scripts/ directory. Also cd there so the relative api.js path below is stable.
OP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(OP_DIR))

failures = []


def ok(msg):
    print(f"  [OK]   {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")
    failures.append(msg)


def done():
    if failures:
        print(f"BASELINE-CHECKS FAIL ({len(failures)} issue(s))")
        sys.exit(1)
    print("BASELINE-CHECKS PASS")
    sys.exit(0)


# --- 1. Backend import / start check ---------------------------------------------
print("[checks] backend import ...")
try:
    import main
    ok("main:app imports cleanly")
except Exception as exc:  # pragma: no cover - reported, not raised
    fail(f"main import failed: {exc}")
    done()

from fastapi.routing import APIRoute  # noqa: E402  (import after main is confirmed)
from fastapi.testclient import TestClient  # noqa: E402

# --- 2. Duplicate-route / obsolete-endpoint check --------------------------------
print("[checks] route table ...")
seen = set()
dupes = []
route_index = {}  # method -> set(path templates)
for r in main.app.routes:
    if not isinstance(r, APIRoute):
        continue
    for m in (r.methods or set()):
        if m in ("HEAD", "OPTIONS"):
            continue
        key = (m, r.path)
        if key in seen:
            dupes.append(key)
        seen.add(key)
        route_index.setdefault(m, set()).add(r.path)
if dupes:
    for m, p in dupes:
        fail(f"duplicate route: {m} {p}")
else:
    ok(f"{len(seen)} routes, no duplicate (method, path)")

# The frontend talks to the backend through exactly one module (operator/services/api.js).
# Every fetch() path in it must resolve to a real backend route — a call to an obsolete or
# renamed endpoint is the "frontend calls to obsolete endpoints" failure mode. We match
# api.js's literal + template paths against the route table (turning any :param / ${id}
# into the FastAPI {param} form) so a drifted endpoint is caught here, not at runtime.
api_js = OP_DIR / "operator" / "services" / "api.js"


def _to_template(path):
    # `/api/commands/${id}` and `/api/comms/history/${id}` -> `/api/.../{p}`
    #
    # Two things are NOT part of a route path and must be cut before matching. A call whose
    # QUERY STRING is built by a nested template literal — `/api/x/operations${id != null ?
    # `?vehicle_id=${id}` : ""}` — is truncated mid-expression by the extraction regex above
    # (it stops at the inner backtick), so strip an unterminated `${…}` tail first; then drop
    # any query string. Both leave the real path, which is the only thing routing depends on.
    path = re.sub(r"\$\{[^}]*$", "", path)
    path = path.split("?", 1)[0]
    return re.sub(r"\$\{[^}]+\}", "{p}", path).rstrip()


def _route_matches(method, path):
    tmpl = _to_template(path)
    for known in route_index.get(method, ()):
        known_t = re.sub(r"\{[^}]+\}", "{p}", known)
        if known_t == tmpl:
            return True
    return False


if api_js.exists():
    src = api_js.read_text(encoding="utf-8")
    # getJSON("...") / postJSON(`...`) / delJSON(...) with the HTTP verb inferred from the helper.
    verb_by_helper = {"getJSON": "GET", "postJSON": "POST", "delJSON": "DELETE"}
    calls = re.findall(r"(getJSON|postJSON|delJSON)\(\s*[`\"']([^`\"']+)[`\"']", src)
    checked = 0
    bad = 0
    for helper, path in calls:
        if not path.startswith("/"):
            continue
        checked += 1
        if not _route_matches(verb_by_helper[helper], path):
            fail(f"api.js calls an endpoint with no backend route: {verb_by_helper[helper]} {path}")
            bad += 1
    if checked and not bad:
        ok(f"all {checked} api.js endpoints resolve to a backend route")
    elif not checked:
        fail("could not extract any endpoint from api.js (parser drift?)")
else:
    fail("operator/services/api.js not found")

# --- 3. Read-only fleet + mission endpoint checks (in-process) -------------------
print("[checks] read-only endpoints ...")
client = TestClient(main.app)

r = client.get("/api/fleet/status")
if r.status_code == 200 and isinstance(r.json(), list):
    fleet = r.json()
    ok(f"GET /api/fleet/status -> 200, {len(fleet)} vehicle(s)")
    ids = [v.get("id") for v in fleet]
    if all(i is not None for i in ids) and len(ids) == len(set(ids)):
        ok(f"vehicles uniquely identified: {ids}")
    else:
        fail(f"fleet vehicle ids missing or non-unique: {ids}")

    if fleet:
        vid = fleet[0]["id"]
        rm = client.get(f"/api/vehicles/{vid}/pixhawk-mission")
        body = rm.json() if rm.status_code == 200 else {}
        need = {"available", "reachable", "count", "current_seq", "waypoints", "partial"}
        if rm.status_code == 200 and need <= set(body):
            ok(f"GET pixhawk-mission (vehicle {vid}) -> 200, stable schema "
               f"(reachable={body.get('reachable')})")
        else:
            fail(f"pixhawk-mission schema/status off: {rm.status_code} keys={sorted(body)}")
else:
    fail(f"GET /api/fleet/status -> {r.status_code}")

r404 = client.get("/api/vehicles/999999/pixhawk-mission")
if r404.status_code == 404:
    ok("unknown vehicle id -> honest JSON 404 (per-USV routing)")
else:
    fail(f"unknown vehicle id -> {r404.status_code} (expected 404)")

# A malformed command ack must never 500 (a stray failure that would look like the
# operator backend fell over mid-flow).
r500 = client.post("/api/commands/none/result", content="not json",
                   headers={"Content-Type": "text/plain"})
if r500.status_code != 500:
    ok(f"malformed command ack -> {r500.status_code}, not a 500")
else:
    fail("malformed command ack -> 500 (endpoint crashed on a non-JSON body)")

done()
