# Comms-state transition log verification (backend #1)

First backend addition from `BACKEND_ROADMAP.md`. Operator-backend-owned (see `SYSTEM_INFORMATION_MODEL.md`).

**Added**
- `main.py`: 1 s background monitor (lifespan task) records per-vehicle comm-state transitions from arrival age
- `GET /api/comms/history/{vehicle_id}` → `{ current, transitions[{state, from, ts, since_last_seen_s}], durations_s, generated_at }`
- `POST /agent/status` logs the CONNECTED transition on each fresh packet
- `services/api.js`: `getCommsHistory(id)` now hits the endpoint (was a null stub)

**Verified** (fresh instance on :8200, live lifecycle test)
- ✓ first packet → `None → CONNECTED @ 0.0s`
- ✓ `CONNECTED → PARTITIONED @ 15.7s` (threshold 15 s, caught within the 1 s monitor)
- ✓ `PARTITIONED → DISCONNECTED @ 30.8s` (threshold 30 s, no intermediate state missed)
- ✓ recovery packet → `DISCONNECTED → CONNECTED @ 0.0s`
- ✓ `durations_s` accumulates total time per state (the thesis "total disconnected time" metric)
- ✓ never-contacted vehicle → `current UNKNOWN`, empty transitions (no fabricated history)
- ✓ transitions logged independent of frontend polling (server-side monitor)
- ✓ `GET /api/fleet/status`, `GET /api/environment`, classic `/`, `/app/` all still 200
- ✓ no server exceptions; `/app` boots with no console errors
- ✓ frontend→backend slice: `api.getCommsHistory(2)` from the browser returns live data

**Notes / honesty**
- This is the **operator-side** comm-state (arrival age) — the reachability the operator experiences. It is deliberately distinct from the vehicle's self-reported `payload.comm_state` (the Local Agent's link view); see SYSTEM_INFORMATION_MODEL.md.
- In-memory log (resets on restart). Durable storage is out of scope for #1; the event log (#2) will persist and will also emit these transitions as events.
- Per-vehicle tracking keys on the posted `usv_id`; today one vehicle reports, so history exists for it and UNKNOWN for template hulls.

**Next (per roadmap)**
- #2 persistent event log — emit these comms transitions as acknowledgeable events
- Frontend: wire the Map comms timeline + Agent page's decision-trace comms nodes to this endpoint (page work, resumes after backend #1–#3)
