# Autonomy verification

First page built on the **Data Availability States** (lib/availability.js).

**Backend**
- ✓ api.getFleet() — comms state, `last_seen_age_s`, `status`/`mission_data.mission_state`, `communication.{operator_reachable,buffered_packets}`
- ✗ api.getAutonomy() returns null — the agent reasoning schema does not exist (BACKEND_GAP)

**Verified** (Playwright against live backend on :8199)
- ✓ per-vehicle selection via reused VehicleDock roster
- ✓ **LIVE** (Scout, connected): current behavior SEARCHING (from mission_state); link CONNECTED; operator reachable Yes; buffered packets 3; reasoning trust "Current"
- ✓ **LAST_KNOWN** (Scout after >15s no contact → PARTITIONED): behavior "SEARCHING · LAST KNOWN · 21s" (dimmed value + amber tag); operator-reachable / buffered / trust all tagged LAST KNOWN · Xs; card headers flip to LAST KNOWN; trust reads "Decaying"; banner explains agent may have transitioned
- ✓ comms continues to degrade CONNECTED → PARTITIONED → DISCONNECTED; trust age increments (26s → 40s); link state stays live/correct
- ✓ **BACKEND_GAP** fields (decision confidence, previous behavior, rationale, next evaluation, active constraints, next transitions, decision-trace history) render dashed "NO BACKEND" / "agent must emit" slots — never fabricated
- ✓ **no-contact vehicle** (USV-1, never reported): behavior, operator-reachable, buffered, trust all "— no contact"; banner "No contact established"; template placeholder status (UNKNOWN) is NOT shown as a fake LAST_KNOWN
- ✓ health/comms independence: LAST_KNOWN never renders a fault (no ✕, no red) — amber comms tag only
- ✓ decision trace: current node is live (CommsPill · behavior); history is an honest BACKEND_GAP box
- ✓ availability legend in dock foot (LIVE / LAST KNOWN / NO BACKEND)
- ✓ no console errors
- ✓ classic dashboard intact (/ → 200)

**Shared fix**
- `.vrow .sub` (roster caption) had the same `.sub` border/background collision fixed earlier for `.rtile .sub`; reset it — verified 0px border, roster reads clean (also improves Map/Vehicle).

**Honesty notes**
- Behavior is explicitly "approximated from mission_state" per DATA_DICTIONARY; it is LIVE only when connected, else LAST_KNOWN with contact age.
- The FAULT and NOT_APPLICABLE states exist in the helper but are not exercised here (no expected-but-broken or absent-hardware fields on this page) — they will appear first on Vehicle/Video/Pilot.

**Known backend gaps**
- agent reasoning schema (confidence, rationale, constraints, next transitions) — drives most of this page
- comms-state transition log — needed for the decision-trace history
