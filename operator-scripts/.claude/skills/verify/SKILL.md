---
name: verify
description: Launch and drive the USV operator station (/app) to verify a frontend change end-to-end against the live FastAPI backend.
---

# Verify the operator station

The app is a static ES-module frontend (`operator/`) served by FastAPI (`main.py`)
at `/app`, alongside the classic dashboard at `/`. Pages poll `api.getFleet()`
(`GET /api/fleet/status`) every ~2s. There is one live-vehicle slot: the most
recent `POST /agent/status` payload replaces the matching template vehicle in
`FLEET_TEMPLATE`; the rest stay UNKNOWN.

## Launch backend
```bash
python -m uvicorn main:app --host 127.0.0.1 --port 8199 --log-level warning   # run_in_background
curl -s http://127.0.0.1:8199/api/fleet/status        # baseline = 3 UNKNOWN template vehicles
```

## Drive the UI (Playwright)
Chromium is installed at the user level. In the scratchpad: `npm install playwright`,
then load `http://127.0.0.1:8199/app/#/<route>` and scrape/screenshot.

Feed live data by POSTing an envelope to `/agent/status`, e.g.:
```json
{"payload":{"usv_id":2,"name":"Scout","comm_state":"CONNECTED","coverage":42,
 "telemetry":{"battery":76,"groundspeed":3.4,"heading":118},
 "mission":{"mission_state":"SEARCHING"},"health":{"leak_detected":false}}}
```
Wait ~2.8s for the poll, then re-scrape. `usv_id` must match a template id (1/2/3).

## Comms degradation (thesis-central)
Backend derives `comm_state` from age since last POST:
CONNECTED → PARTITIONED (>15s) → DISCONNECTED (>30s). Post once, then observe the
same vehicle over ~32s: the CommsPill (CONN/PART/DISC), the Last-Contact text class
(`txt-c`/`txt-p`/`txt-d`), and the rollup counts all shift. Battery stays vivid when
stale by design (see DATA_DICTIONARY.md).

## Gotchas
- `npx playwright install chromium` fetches the browser binary but NOT the npm
  package — `npm install playwright` separately or the import fails.
- Honesty checks: absent fields must render the NO-TELEM slot (`— not reported` /
  `— no telem`), never a fabricated value.
