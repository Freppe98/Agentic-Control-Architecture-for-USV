# Configuration verification

**Backend**
- ✓ api.getFleet() (vehicle registry — live from GET /api/fleet/status)
- ✗ no configuration read/write endpoint exists (see gaps)

**Verified** (Playwright against live backend on :8199)
- ✓ three sections render: Communication thresholds, Operator preferences, Vehicle registry
- ✓ honest section tags: "Backend-defined · read-only", "Saved in this browser", "Live · read-only"
- ✓ threshold values read-only: 8 / 15 / 30 s (mirror main.py constants); heartbeat interval → NO-TELEM
- ✓ ThresholdTimeline: 4 comms bands (live / stale / partitioned / disconnected), ticks at 8/15/30
- ✓ registry lists live fleet (USV-1, Scout, USV-3); Scout = CONN after POST; callsign & onboard address → NO-TELEM
- ✓ preference change flashes "Saved in this browser"; toggle flips (aria-checked)
- ✓ persistence: speed_units=kn and clock_24h=false survive reload (localStorage `operator.prefs.v1`); nothing sent to backend
- ✓ reset to defaults restores values (speed_units → ms)
- ✓ ribbon fleet counts + clock update
- ✓ no console errors
- ✓ classic dashboard intact (/ → 200, "Aquality Fleet")l

**Honesty notes**
- Thresholds are the backend's compiled-in constants (main.py), shown read-only — no runtime endpoint reads or writes them. If those constants change, update `BACKEND_THRESHOLDS` in Config.js to match.
- Operator preferences persist in the browser only (localStorage); this is not a server profile. A stored preference does not affect a page that does not yet read it — migrated pages opt in via `getPref()`.
- Registry identity is live; callsign / onboard address are registry-only fields the backend does not carry (NO-TELEM), not fabricated.

**Known backend gaps**
- configuration write endpoint (e.g. POST /api/config) — thresholds are read-only until it exists
- server-side operator profile / preference persistence
- vehicle registry endpoint (add/edit vehicles, callsign, onboard address)
- heartbeat interval is not exposed by the backend
