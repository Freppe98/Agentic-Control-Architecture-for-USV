# Mission verification

**Backend**
- ✓ api.getFleet() — per-vehicle mission fields: `mission_data` (mission_state, mission_active, current_waypoint_display, mission_count), `coverage`, `fleet_info` (fleet_role, assigned_sector, formation)
- ✗ no mission-object endpoint (GET /api/mission) — see gaps

**Verified** (Playwright against live backend on :8199)
- ✓ tab bar: Overview active/interactive; Replay / Statistics / Export rendered but locked (disabled, "available after a mission completes" hint)
- ✓ participation is real: vehicle listed only when `mission_data.mission_active === true` (Scout in, template USV-1/USV-3 out)
- ✓ vehicle row live: role (Primary sweep), sector (Sector B), waypoint (WP 4 / 12), state (SEARCHING), coverage bar (42%)
- ✓ Activity tile aggregates states honestly ("1 searching", "1 vehicle in mission")
- ✓ Coverage tile is a transparent aggregate ("42%", sub "avg of 1 reporting") — not a fabricated mission total
- ✓ Mission scope → NO-TELEM ("no named scope"); ETA / remaining → NO-TELEM ("no mission object")
- ✓ comms degradation: Scout CONNECTED → PARTITIONED (>15s) → DISCONNECTED (>30s) while staying "in mission" — comms axis kept separate from mission participation (last-known)
- ✓ empty state honest when no vehicle reports an active mission
- ✓ ribbon fleet counts + clock update
- ✓ no console errors
- ✓ classic dashboard intact (/ → 200, "Aquality Fleet")

**Shared-style fix (also improves Fleet & Events)**
- `.rtile .sub` (rollup caption) was inheriting the Vehicle page's `.sub` subsystem-card `border`/`background`, drawing a faint box around every rollup caption. Reset border/background on `.rtile .sub`; verified the box is gone on Mission and Fleet with no regressions.

**Honesty notes**
- Only mission-*object* concepts are NO-TELEM (named scope, ETA/remaining). Fields the agent genuinely reports (mission_active, state, waypoint, coverage, role/sector) are shown live; absent per-vehicle values render "—", distinct from the tagged NO-TELEM gaps.
- Locked tabs are inert scaffolding, not fake content — no invented replay/statistics/export data.

**Known backend gaps**
- mission-object endpoint (GET /api/mission): named scope, mission-level ETA/remaining, plan/waypoint list
- assigned-vs-depot split (no `assigned` field) — participation is inferred from `mission_active` only
- mission history → Replay / Statistics / Export tabs stay locked until it exists
