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

---

# Mission integration testing — bench procedure

Added with the mission-operation evidence panel. This is the **controlled bench-test**
procedure for the Mission → Upload tab; it produces the artifacts the thesis cites.

**No live vehicle command is part of this procedure.** Steps 1-4 are entirely local
(backend + browser). Steps 5-8 require a bench Pixhawk/SITL already under test — run them
only on a vessel that is out of the water, disarmed, and authorised for bench testing.

## Automated (run first — nothing below is meaningful if these fail)

```
cd operator-scripts
python -m unittest discover -s tests -p "test_*.py"    # 188 tests
npm test                                                # 183 tests
node --check operator/pages/Mission.js
node --check operator/lib/mission-upload.js
python -c "import main"
```

## 1. Preview is side-effect free (no vehicle needed)

```
curl -s localhost:8199/api/commands | python -m json.tool > before.json
curl -s -X POST localhost:8199/api/missions/preview -H 'content-type: application/json' \
  -d '{"contract_version":"mission-contract-v1","waypoints":[{"latitude":56.6501,"longitude":12.8701,"loiter_time_s":0},{"latitude":56.6512,"longitude":12.8725,"loiter_time_s":30}]}'
curl -s localhost:8199/api/commands | python -m json.tool > after.json
diff before.json after.json          # MUST be empty — preview queues nothing
```
Expect `expected_route_content_hash` =
`sha256:5fe4c2352fc9183e121538a8e199131159cdda66658ccb755c7db1ff54672bfd`,
`expected_route_waypoint_count` 2, `expected_pixhawk_item_count` 3. Confirm the event log
length is unchanged (`GET /api/events`).

## 2. A browser-supplied expected hash is refused

```
curl -s -X POST localhost:8199/api/missions/preview -H 'content-type: application/json' \
  -d '{"waypoints":[{"latitude":56.65,"longitude":12.87}],"expected_route_content_hash":"sha256:deadbeef"}'
```
Expect **400**, `error: mission_contract_violation`, and an error naming
`expected_route_content_hash`. The digest is never echoed back.

## 3. The waypoint limit is shared by preview and upload

`GET /api/commands/capabilities` → note `max_route_waypoints` (200) and
`max_route_waypoints_source` (`scout-contract` — **Scout defines and enforces this limit
under mission-contract-v1; the Operator mirrors it**). Post a 201-waypoint route to **both**
`/api/missions/preview` and `/api/commands`: both must return 400 with the **identical**
`errors` array, and no command may be queued — the route is refused before transmission, so
Scout never sees it.

To check the **rendering** of Scout's own refusal (which needs no oversized upload), inject
a `MISSION_TOO_LARGE` result into a MISSION_UPLOAD command record and open the Upload tab:

```
{"accepted": false, "verified": false,
 "error": {"code": "MISSION_TOO_LARGE",
           "maximum_route_waypoints": 200, "observed_route_waypoints": 250}}
```
The verdict must read *"Upload refused by Scout — Mission too large — Scout accepts at most
**200** route waypoints under mission-contract-v1; this route submitted **250**."* with the
`[MISSION_TOO_LARGE]` code, and **no** generic "may be unchanged or partial" tail. The
technical panel must list `maximum_route_waypoints` and `observed_route_waypoints` as
separate rows.

## 4. High-precision probe (the precision proof)

```
python -c "import mission_contract as m; print(m.route_content_hash([{'latitude':56.65012345678,'longitude':12.87016789012,'loiter_time_s':12.34567},{'latitude':56.65127654321,'longitude':12.87259876543,'loiter_time_s':0.9994}]))"
```
Expect `sha256:125c779021c1521fae67462719cdab588f871c3b44d808b362c0630f221998ad` —
the same digest Scout produced independently. Record both in the log book.

## 5. Upload lifecycle on the bench vehicle

1. Take **OPERATOR** control (Map or Vehicle page) — Upload is disabled otherwise.
2. Mission → **Upload**, paste the two-waypoint route from step 1, **Validate & preview**.
   Expect route 2 / Pixhawk 3 and the abbreviated `sha256:5fe4c2352fc9…` (hover for full).
3. **Upload route to Pixhawk**, confirm the dialog.
4. Watch the track: *Requested → Executing → Awaiting readback → Verified*.
   **Record that "Failed" never appears during the awaiting window** — this is the
   regression this panel exists to catch.
5. Click **Show technical details** and capture the panel (screenshot + evidence JSON).

## 6. Independent-readback-unavailable caution

With the upload verified, stop Scout (or pull the link) and press
**Retry independent readback**. Expect the caution state
*"Scout verified; independent Operator readback unavailable"* — amber, **not** green
Verified and **not** red Failed. Restore the link and retry; it must return to Verified.

## 7. Verification conflict (high severity)

Only reproducible when Scout's report and the flight controller genuinely disagree. If a
conflict appears during any bench run, **export the evidence JSON immediately** — it is the
highest-value artifact this station produces, and the mission must not be flown.

## 8. Evidence export

**Export evidence (JSON)** in the technical panel downloads
`mission-evidence-<type>-<command_id>.json` containing `command`, `lifecycle`,
`scout_result`, `independent_readback`, `comparison`, `timestamps`. Scout's claim
(`scout_result`) and the Operator's own observation (`independent_readback`) are stored
**separately and deliberately** — merging them would destroy the only property that makes
the file evidence. Absent values are exported as `null`, never omitted.

**Scope note:** this is a per-operation evidence export for the current mission integration
tests, not a general experiment framework. No autonomous command execution and no
communication-impairment tooling is part of it.
