# Pilot verification

**Purpose**
- Operational/dev bridge to the vehicle-local dashboard. Embeds the web UI the
  vehicle already serves (Scout — http://10.0.2.10:8080/) in an iframe. No pilot /
  teleoperation controls of its own; no fabricated vehicle data.

**Backend**
- ✓ api.getFleet() (ribbon comms counts + real vehicle display name)
- (dashboard is fetched directly by the browser, cross-origin — not via the operator backend)

**Verified**
- ✓ route registered (#/pilot), nav item active
- ✓ header "Pilot" + subheader "Embedded vehicle-local dashboard"
- ✓ target shows vehicle name (Scout, from backend) + URL http://10.0.2.10:8080/
- ✓ iframe mounts with correct src, no sandbox (interaction preserved)
- ✓ Reload remounts the frame (fresh navigation)
- ✓ Open in new tab → http://10.0.2.10:8080/ (href + window.open)
- ✓ status note is honest: load-timeout heuristic + always-available fallback,
      never asserts the dashboard is up (cross-origin content is unreadable)
- ✓ no console errors from the operator app itself
- ✓ other pages still load (Map, Events)

**Notes**
- The embedded dashboard was not reachable from the dev host (vehicle VM network),
  so the iframe area is blank there — expected. The browser fires `load` even on an
  error/blocked page, which is why the status keeps the "if it stays blank, open in
  a new tab" caveat rather than claiming success.
- Per-vehicle URLs live in a small local map in Pilot.js (DASHBOARDS), keyed by
  vehicle id. Only vehicles that actually serve a dashboard are listed. No
  Configuration API.

**Known limitations**
- Servers that set X-Frame-Options / frame-ancestors will refuse to render inside
  the frame; the Open-in-new-tab fallback covers this.
