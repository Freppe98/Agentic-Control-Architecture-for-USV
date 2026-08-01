"""Canonical vehicle identity for the Operator Station — ONE explicit policy, one function.

Why this module exists
----------------------
Two live USVs (Scout and SAR-001) reach the operator over the same POST /agent/status
endpoint, and they do NOT spell their identity the same way. Scout's Local Agent sends
`payload.usv_id = 2` with `source = "usv-2"`; a second vehicle may send `3`, `"3"`,
`"usv-3"`, `"USV-3"` or even its callsign `"SAR-001"`. The previous inline resolver was
`int(str(raw).replace("usv-", ""))` with an `except: return 2` fallback, which silently
mapped ANY non-numeric identity (e.g. `"SAR-001"`) onto **Scout's** id — one vehicle's
telemetry, name and health landing on another vehicle's record.

Policy (deliberate, and separate from display name)
---------------------------------------------------
* The **canonical vehicle id** is the value every per-USV store, command, mission cache,
  URL and selection is keyed by. For every vehicle that has a numeric identity — which is
  every real vehicle today — it is that **integer** (Scout = 2, SAR = 3). Integers are what
  Scout's existing records, the command queue and the frontend already use, so canonical
  identity stays backward compatible and nothing has to be silently migrated.
* Its stable **string form** is the slug `"usv-2"` / `"usv-3"`, published on every fleet row
  as `vehicle_id` and accepted anywhere an id is parsed. Slug and integer are two spellings
  of ONE identity, never two identities.
* A vehicle that reports only a non-numeric identity and is not configured here is keyed by
  its normalized slug string (e.g. `"sar-001"`). Dict keys are opaque, so such a vehicle is
  a first-class fleet member with no special-case code — it simply has no legacy numeric id.
* The **display name** ("Scout", "SAR-001") is a separate, per-vehicle field. It is NEVER an
  identity key: a vehicle renaming itself must not create, merge or move a record.

Aliases are EXPLICIT, never inferred
------------------------------------
`"SAR-001" -> 3` is a mapping a human declares in the registry, because it cannot be
derived. Anything undeclared resolves to its own distinct canonical id rather than being
guessed onto an existing vehicle — guessing is how two different future USVs would silently
merge into one record. Alias collisions are rejected at load time, loudly.

Configuration
-------------
Data-driven, no code change to add a vehicle: drop a `vehicles.json` next to main.py (or
point `OPERATOR_VEHICLE_REGISTRY` at one). Shape:

    {
      "usv-2": {"id": 2, "display_name": "Scout",   "aliases": [2, "2", "USV-2", "Scout"]},
      "usv-3": {"id": 3, "display_name": "SAR-001", "aliases": [3, "3", "USV-3", "SAR-001"]}
    }

`id` may be omitted when the key is of the form `usv-<n>` (it is then derived). `aliases`
are optional — the key, the slug and the numeric id are always aliases of themselves.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

# Vehicles the station knows about before any of them has ever made contact. These exist in
# GET /api/fleet/status from startup with UNKNOWN comms, and live packets UPDATE these same
# records rather than creating a second row (no static USV-3 placeholder alongside a dynamic
# SAR-001 row — one canonical record per vehicle, always).
DEFAULT_REGISTRY = {
    "usv-1": {"id": 1, "display_name": "USV-1", "aliases": []},
    "usv-2": {"id": 2, "display_name": "Scout", "aliases": ["Scout"]},
    "usv-3": {"id": 3, "display_name": "SAR-001", "aliases": ["SAR-001", "SAR001", "SAR"]},
}

REGISTRY_FILENAME = "vehicles.json"
REGISTRY_ENV_VAR = "OPERATOR_VEHICLE_REGISTRY"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_USV_NUM_RE = re.compile(r"^usv-(\d+)$")


class RegistryError(ValueError):
    """A malformed or ambiguous vehicle registry (raised at load time, never at runtime)."""


def normalize_token(raw) -> str:
    """Fold any identity spelling to one comparable token.

    `3` / `"3"` -> "3";  `"usv-3"` / `"USV-3"` / `"USV_3"` / `"usv 3"` -> "usv-3";
    `"SAR-001"` / `"SAR_001"` / `"sar 001"` -> "sar-001". Returns "" for absent/blank.
    Case and separator style are spelling, not identity — everything else is preserved.
    """
    if raw is None:
        return ""
    if isinstance(raw, bool):          # bools are ints in Python; never a vehicle id
        return ""
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    token = _SLUG_RE.sub("-", str(raw).strip().lower()).strip("-")
    return token


def _numeric_from_token(token: str):
    """The integer identity a token denotes ("3" -> 3, "usv-3" -> 3), else None."""
    if token.isdigit():
        return int(token)
    m = _USV_NUM_RE.match(token)
    return int(m.group(1)) if m else None


class VehicleRegistry:
    """Immutable alias index + display names. Built once at import; see load_registry()."""

    def __init__(self, entries: dict):
        self._order = []        # configured canonical ids, in declaration order
        self._display = {}      # canonical id -> configured display name
        self._slug = {}         # canonical id -> canonical slug ("usv-2")
        self._alias = {}        # normalized token -> canonical id
        self._source = None     # where this registry came from (diagnostics)

        for key, spec in (entries or {}).items():
            if str(key).startswith("_"):
                continue          # "_comment" and friends — documentation, not a vehicle
            spec = spec or {}
            if not isinstance(spec, dict):
                raise RegistryError(f"vehicle {key!r}: entry must be an object")
            key_token = normalize_token(key)
            if not key_token:
                raise RegistryError("vehicle registry contains a blank key")
            declared = spec.get("id", spec.get("numeric_id"))
            numeric = declared if isinstance(declared, int) and not isinstance(declared, bool) \
                else _numeric_from_token(key_token)
            if declared is not None and numeric is None:
                raise RegistryError(f"vehicle {key!r}: id must be an integer")
            cid = numeric if numeric is not None else key_token

            if cid in self._display:
                raise RegistryError(f"vehicle {key!r}: duplicate canonical id {cid!r}")
            self._order.append(cid)
            self._display[cid] = str(spec.get("display_name") or spec.get("name") or key)
            self._slug[cid] = f"usv-{numeric}" if numeric is not None else key_token

            # Self-aliases (key, slug, bare number) plus every explicitly declared alias.
            tokens = [key_token, self._slug[cid]]
            if numeric is not None:
                tokens.append(str(numeric))
            for a in (spec.get("aliases") or []):
                t = normalize_token(a)
                if t:
                    tokens.append(t)
            for t in tokens:
                owner = self._alias.get(t)
                if owner is not None and owner != cid:
                    raise RegistryError(
                        f"alias {t!r} is claimed by both {owner!r} and {cid!r} — an alias "
                        f"must map to exactly one vehicle")
                self._alias[t] = cid

    # --- identity -------------------------------------------------------------------
    def canonical_id(self, raw):
        """THE canonical-vehicle-id function. Returns an int, a slug string, or None.

        None means "this value names no vehicle" — callers must treat that as an error
        rather than substituting a default vehicle (defaulting is what let SAR's packets
        overwrite Scout).
        """
        token = normalize_token(raw)
        if not token:
            return None
        hit = self._alias.get(token)
        if hit is not None:
            return hit
        numeric = _numeric_from_token(token)
        return numeric if numeric is not None else token

    def slug(self, cid) -> str:
        """Stable string form of a canonical id ("usv-2"), safe in URLs and logs."""
        if cid in self._slug:
            return self._slug[cid]
        return f"usv-{cid}" if isinstance(cid, int) else str(cid)

    def numeric_id(self, cid):
        """The legacy integer id, or None for a vehicle that has no numeric identity."""
        return cid if isinstance(cid, int) else None

    def default_display_name(self, cid) -> str:
        """Configured display name, else a readable fallback derived from the id."""
        if cid in self._display:
            return self._display[cid]
        return f"USV-{cid}" if isinstance(cid, int) else str(cid).upper()

    # --- membership -----------------------------------------------------------------
    def configured_ids(self) -> list:
        """Canonical ids that exist before first contact, in declaration order."""
        return list(self._order)

    def is_configured(self, cid) -> bool:
        return cid in self._display

    def describe(self) -> str:
        return f"{len(self._order)} configured vehicle(s) from {self._source or 'built-in defaults'}"


def load_registry(path=None) -> VehicleRegistry:
    """Load the registry from `path`, $OPERATOR_VEHICLE_REGISTRY, ./vehicles.json, or the
    built-in defaults. A malformed file is a hard error: silently falling back to defaults
    would route a configured vehicle's packets to the wrong record."""
    candidate = path or os.environ.get(REGISTRY_ENV_VAR)
    if candidate is None:
        default_path = Path(__file__).resolve().parent / REGISTRY_FILENAME
        candidate = default_path if default_path.exists() else None
    if candidate is None:
        reg = VehicleRegistry(DEFAULT_REGISTRY)
        reg._source = None
        return reg
    p = Path(candidate)
    try:
        entries = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RegistryError(f"cannot read vehicle registry {p}: {exc}") from exc
    if not isinstance(entries, dict):
        raise RegistryError(f"vehicle registry {p} must be a JSON object of vehicles")
    reg = VehicleRegistry(entries)
    reg._source = str(p)
    return reg
