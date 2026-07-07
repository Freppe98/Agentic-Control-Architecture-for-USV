# Persistent event log verification (backend #2)

Second backend addition from `BACKEND_ROADMAP.md`. Operator-backend-owned (see `SYSTEM_INFORMATION_MODEL.md`). Replaces the frontend's flatten-from-payload feed with one server-side store.

**Added**
- `main.py`: append-only `event_log` (in-memory, id-stable, capped `MAX_EVENTS=5000`) with two feeders:
  - **comms transitions** — `record_comms_state` now emits a first-class event on every transition, with deterministic severity: PARTITIONED → caution, DISCONNECTED → warning, restored/first-contact → info. Source `operator-backend`.
  - **vehicle-reported events** — `POST /agent/status.payload.events` ingested and deduped by fingerprint (explicit `id`, else own-timestamp + message), so re-sent packets store once. Severity/message derived server-side by the same rules as the frontend (`derive_event_severity` / `extract_event_message` mirror `lib/ui.js`), so severity is decided once and stored.
- `GET /api/events?limit=N` → `{ events[{id, ts, severity, type, source, vehicle_id, vehicle, message, acknowledged}], count, generated_at }`, chronological (frontend sorts newest-first). Stable `id` + `acknowledged:false` model future ack **without inventing the ack action** (no `POST .../ack` yet — that is the next roadmap slot).
- `services/api.js`: `getEvents()` now hits `GET /api/events` and adapts to the existing `{vehicle, vehicleId, event}` shape the Events page already consumes — the backend swap lives in api.js, the page is unchanged.

**Verified** (fresh instance on :8201, live lifecycle + Playwright against `/app/#/events`)
- ✓ empty log on boot (`count 0`, no fabricated history)
- ✓ first packet → `#1 info comms "First contact established"`; vehicle event `#2 warning "Leak detected"` ingested with its own timestamp, type/source preserved
- ✓ identical re-POST does **not** duplicate the vehicle event (count stays 2) and emits no second comms event (state unchanged)
- ✓ silence → `caution "Communication partitioned"` (>15s) then `warning "Communication lost"` (>30s), server-side, independent of frontend polling
- ✓ recovery packet → `info "Communication restored"`
- ✓ deterministic severities across the full lifecycle
- ✓ `?limit=2` returns the most recent 2 (ids 4,5) while `count` reflects the full log
- ✓ Events page renders it end-to-end: newest-first, correct severity chips, "Scout" resolved, filter counts (All/Unack/Warning/Caution/Info), session-local Ack on caution-and-above, bell tracks unack — no console errors, layout unchanged
- ✓ `GET /api/fleet/status`, `GET /api/comms/history/2`, `GET /api/environment`, classic `/`, `/app/` all still 200; no server exceptions

**Notes / honesty**
- In-memory store — resets on restart, like the comms log. Durable storage is out of scope for #2.
- Acknowledgement is still **session-local** in the UI; the log carries an `acknowledged` flag but there is no ack endpoint yet, so the page does not imply server-side ack. The updated Events note says exactly this.
- Dedup collapses untimestamped identical repeats to a single log entry (a log, not per-packet spam). Genuinely distinct occurrences need a stable event `id` from the agent — noted as the honest fix.
- The fleet payload still carries `events` (Map and the classic dashboard read it directly); only the operator Events feed switched source.

**Next (per roadmap)**
- #3 live configuration API — `GET /api/config` (live thresholds instead of compiled mirror)
- Later: persistent acknowledgement — `POST /api/events/{id}/ack` (now naturally supported by the stable ids)
