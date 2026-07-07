# Terminal verification

**Purpose**
- Vehicle-local shell ACCESS HELPER for Scout — not an in-browser terminal. Shows the
  SSH command, copies it, and links to the vehicle dashboard. No fake shell, no
  server-side command execution, no stored credentials.

**Why option 2 (SSH helper, not embedded terminal)**
- Probed Scout (10.0.2.10) for a web-terminal service: ttyd :7681, wetty :3000, and
  code-server were all unreachable; no terminal path on the dashboard. Reachable
  ports are the BlueOS management stack (BlueOS :80, Aquality dashboard :8080,
  AutoPilot Manager :8000, Version Chooser :8081, Wifi Manager :9000) — none is an
  authenticated web terminal. Security rule: without such a service, do not implement
  server-side command execution. So the page is an honest SSH access helper.

**Backend**
- ✓ api.getFleet() (ribbon comms counts + real vehicle name + operator-link state)
- (no new backend; SSH is a direct operator→vehicle connection, off the operator backend)

**Verified**
- ✓ route registered (#/terminal), nav item active, no longer the stub
- ✓ header "Terminal" + subheader "Vehicle-local shell access"
- ✓ card shows vehicle (Scout, from backend), Host 10.0.2.10, User motherpi
- ✓ SSH command shown: `ssh motherpi@10.0.2.10`
- ✓ Copy button copies the exact command (clipboard verified) + "Copied" feedback
      (secure-context clipboard API, with an execCommand fallback for non-secure hosts)
- ✓ Open Scout dashboard → http://10.0.2.10:8080/
- ✓ honest note: no web-terminal service; run the command in your own terminal
- ✓ link-state chip labelled as the operator telemetry link, not SSH reachability
- ✓ other pages still load (Pilot, Map)
- ✓ no operator-app console errors

**Notes**
- Per-vehicle SSH targets live in a small local map in Terminal.js (SSH_TARGETS),
  keyed by vehicle id — same pattern as Pilot's DASHBOARDS. Only Scout is listed.
- If a proper authenticated web-terminal service is later added to a vehicle, this
  page can gain an embedded iframe (like Pilot) without changing the backend.
