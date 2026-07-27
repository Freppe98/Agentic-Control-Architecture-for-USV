# Battery telemetry flicker (97% → "—" → 97%) — root cause + last-known fix

Branch `feature/operator-plan-route-quality`. The selected-USV Map battery alternated at the
2 s fleet-poll rate.

## Exact cause
`GET /api/fleet/status` (the 2 s `onFleet` poll) returned `battery` alternating `97` / `null`.
Scout sends the MAVLink `battery_remaining = -1` "unknown/unavailable" sentinel on some packets.
In `main.py normalize_agent_message`:
- `-1` is not `None`, so the last-known fallback (`if battery is None: …`) was skipped;
- line 407 `battery if battery != -1 else None` mapped `-1` → `None`;
- `receive_agent_status` stored `-1` into `last_known_telemetry`, poisoning the fallback.

So a single `-1` packet flipped a valid `97` to `None`; `onFleet` replaced `fleet` wholesale and
`BatteryBar` / `vehicleRow` rendered `null` as "—". Not the alternate-endpoint / partial-object
theories — a single endpoint, single field, transient sentinel.

## Fix (root cause + defence-in-depth)
- **Backend (`main.py`)** — treat `-1` like an absent field: fall back to last-known, and never
  store `-1` into `last_known_telemetry`. So the endpoint stops emitting the flicker for every
  consumer (Map, Vehicle, …). First-ever `-1` with no prior value still yields `null` → honest "—".
- **Frontend (`operator/lib/telemetry-cache.js`, new)** — per-USV last-known merge applied in
  `onFleet` before `fleet` is replaced: a finite number updates the cache; a null/undefined value
  keeps the last-known; only an explicit `<field>_available === false` clears it. Covers
  `battery`, `speed`, `heading` (NOT lat/lng — never plot a stale position as current). Keyed by
  USV id, so one vehicle never affects another. Freshness is untouched — the merged vehicle keeps
  its own `comm_state` / `last_seen_age_s`, so a retained value is still marked stale on degradation
  and never shown as fresh. The 2 s poll is unchanged.

## Tests
- `tests/telemetry-cache.test.mjs` (11) — keep-on-absent, no-clear-on-null, newer-valid-replaces,
  stale-retains-not-dash, first-ever-missing shows "—", explicit clear, per-USV independence,
  alternating full/partial no flicker, speed/heading merged, lat/lng not retained, 2 s poll intact.
- `tests/test_battery_last_known.py` (6) — `-1` does not clobber/poison last-known, missing keeps
  last-known, newer replaces, real `0%` kept, first-ever `-1` → `None`.
- Baselines: frontend **321 pass**, backend **354 pass**.

## Manual verification
1. Run backend + Scout (or replay a stream with intermittent `battery:-1`), open `/app#/map`,
   select the USV.
2. Battery holds steady at its last valid % across polls (no 2 s "—" blink); it updates only when a
   new valid % arrives.
3. Pull the link (DISCONNECTED) → battery shows the last-known value with the existing stale
   styling ("Telemetry as of … — not live"), not "—".
4. Two USVs with different batteries → each holds its own value; a partial poll for one never blanks
   the other.
