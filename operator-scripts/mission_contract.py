"""mission-contract-v1 route content hashing — the Operator side of Scout's contract.

WHY THIS MODULE EXISTS, AND WHY IT IS THE ONLY CALCULATOR
---------------------------------------------------------
Counts alone cannot prove a mission. A route whose two waypoints were swapped, or one
whose longitude is wrong in the fourth decimal, still has N route legs and N+1 Pixhawk
items. The route content hash is the only axis that proves the route on the flight
controller is byte-for-byte the route the operator approved.

That proof only holds if BOTH sides compute the identical digest over the identical
bytes. So there is exactly one calculator, here, in the Operator backend. There is
deliberately NO frontend JavaScript implementation: a second implementation is a second
thing that can drift, and a hash comparison between two drifted implementations reports
"mismatch" for a route that is actually correct — or, far worse, silently agrees for the
wrong reason. The browser renders `expected_route_content_hash` as an opaque string it
receives from this module and never recomputes it.

This replaces the removed operator-side `wpm1:` FNV-1a hash, which was invented locally,
was never computed by Scout, and therefore fed a comparison that could not fail
meaningfully. Removing it and shipping null was correct; this module is what null was
waiting for.

THE CANONICALIZATION (Scout-owned; MISSION_CONTRACT_v1.md is authoritative)
--------------------------------------------------------------------------
Hash input is the ROUTE ONLY — Home is excluded. Scout owns Pixhawk seq 0 / Home and
prepends it when writing to the FC; the operator never sends Home and therefore cannot
hash it. A `full_mission_hash` (which does include Home) is a different value over
different bytes and is NEVER substituted for this one.

Each route waypoint becomes exactly this item:

    {"sequence":   <1-based position in route order, int>,
     "command":    "MAV_CMD_NAV_WAYPOINT",          # fixed — Scout writes every leg as one
     "frame":      "MAV_FRAME_GLOBAL_RELATIVE_ALT", # fixed
     "latitude":   round(float(lat), 7),            # ~11 mm; matches MAVLink 1e7 int scaling
     "longitude":  round(float(lng), 7),
     "altitude":   0.0,                             # fixed — surface vessel
     "param1":     round(float(loiter_time_s), 3),  # NAV_WAYPOINT hold time, seconds
     "param2":     0.0,
     "param3":     0.0,
     "param4":     0.0}

then: sort items by `sequence`; serialize the bare LIST (no wrapper object) with
`json.dumps(items, sort_keys=True, separators=(",", ":"))`; encode UTF-8; SHA-256;
prefix the lowercase hex digest with `sha256:`.

The rounding is load-bearing in both directions. It is what lets a float that arrives as
`56.6501` and one that arrives as `56.65010000000001` hash identically, and it is why an
int `0` and a float `0.0` loiter canonicalize to the same bytes. Changing either
precision changes every hash and silently breaks agreement with Scout.

Pinned cross-system golden value — see tests/fixtures/mission-contract-v1.json and
tests/test_mission_contract.py, which fail loudly if this module ever drifts from it.
"""

import hashlib
import json

CONTRACT_VERSION = "mission-contract-v1"

# Fixed MAVLink identity of every route leg. Strings, not numeric enum codes: the contract
# hashes the symbolic names, so 16 / 3 would produce entirely different bytes.
ROUTE_ITEM_COMMAND = "MAV_CMD_NAV_WAYPOINT"
ROUTE_ITEM_FRAME = "MAV_FRAME_GLOBAL_RELATIVE_ALT"

# Decimal places. Scout-owned; do not tune these to make a test pass.
COORDINATE_PRECISION = 7
LOITER_PRECISION = 3

HASH_PREFIX = "sha256:"


def canonical_route_items(waypoints):
    """The exact list of dicts that gets hashed, in hash order.

    Exposed separately from the digest so tests and diagnostics can assert on the
    structure itself — when a hash disagrees with Scout, the useful question is which
    field differs, and a bare hex string cannot answer it.

    `waypoints` is route order: index 0 is the first leg. Sequence is 1-based because
    Scout's Home occupies 0; a route item is never sequence 0.
    """
    items = [
        {
            "sequence": int(i),
            "command": ROUTE_ITEM_COMMAND,
            "frame": ROUTE_ITEM_FRAME,
            "latitude": round(float(wp["latitude"]), COORDINATE_PRECISION),
            "longitude": round(float(wp["longitude"]), COORDINATE_PRECISION),
            "altitude": 0.0,
            "param1": round(float(wp.get("loiter_time_s", 0) or 0), LOITER_PRECISION),
            "param2": 0.0,
            "param3": 0.0,
            "param4": 0.0,
        }
        for i, wp in enumerate(waypoints, start=1)
    ]
    # Already built in order; sorting is explicit because the contract specifies it, and
    # because it makes the ordering guarantee independent of how the list was constructed.
    items.sort(key=lambda item: item["sequence"])
    return items


def canonical_route_json(waypoints):
    """The exact UTF-8 string that gets hashed. Separate from the digest for the same
    diagnostic reason as canonical_route_items: a byte diff is readable, a hash diff is not."""
    return json.dumps(canonical_route_items(waypoints), sort_keys=True, separators=(",", ":"))


def route_content_hash(waypoints):
    """`sha256:<hex>` over the canonical route. Home excluded. See module docstring."""
    digest = hashlib.sha256(canonical_route_json(waypoints).encode("utf-8")).hexdigest()
    return HASH_PREFIX + digest
