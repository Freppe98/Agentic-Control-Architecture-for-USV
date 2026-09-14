# UAV companion reporting — integration guide for Scout

**Audience:** the developer implementing this in the real AqualityONE USV / Scout
repository. You do not need to read the operator station's code to use this document.

**Status: this contract is PROPOSED and unimplemented on Scout today.** Nothing here means
a UAV, a UAV radio link, onboard perception, or obstacle replanning currently exists or
works on the vehicle. It only defines the report Scout's Local Mission Agent should send to
the operator once those pieces exist, so the operator side (already built and tested) can
receive it correctly from day one.

This is the **concise** version. The exhaustive reference — every validation rule, every
edge case, the full rationale — is `COMPANION_CONTRACT.md` in the operator repository.
Read this first; go to that document when you need a rule this page doesn't cover.

---

## 1. The one-sentence version

Add an optional `companions` object to the `payload` of the status message you already
`POST` to the operator every ~1 Hz. Nothing else changes. Vehicles/versions that omit it
keep working exactly as before.

## 2. Where it goes

You already send something like this:

```json
{
  "message_type": "status",
  "schema_version": "1.0",
  "source": "usv-2",
  "target": "operator",
  "timestamp": 1786175327.0,
  "payload": {
    "usv_id": 2,
    "name": "Scout",
    "comm_state": "CONNECTED",
    "telemetry": { "...": "..." }
  }
}
```

Add one key inside `payload`:

```json
"payload": {
  "usv_id": 2, "name": "Scout", "comm_state": "CONNECTED", "telemetry": { "...": "..." },
  "companions": {
    "schema": "companion-v2",
    "session": { "id": "boot-3f9a2c1e", "seq": 42, "generation": 3 },
    "items": [ /* see §4 */ ]
  }
}
```

`session.id` — a string that changes every time your companion-reporting code restarts
(process boot). Easiest options: a UUID generated once at startup, or `f"{hostname}-{pid}-{boot_time}"`.

`session.generation` — an integer you **persist to local storage across your own process
restarts** (a one-line counter file is enough) and **increase by at least 1 every time your
companion-reporting code starts up**, including the very first time (start at 0 or 1, then
always go up from there — never reuse or reset it). This is the field the operator actually
uses to recognize "this is a newer run of you than the one I'm tracking" — see §7. If your
counter file is ever lost or unreadable, do not silently restart at 0 — that would make your
next real restart look like an OLDER, rejected run to the operator. Pick something guaranteed
to be higher instead (e.g. derive a value from wall-clock time).

`session.seq` — an integer you increase by 1 every time the CONTENT of `items` changes
(a companion appears/disappears, a hazard appears/disappears/changes, any field changes).
Re-send the same `seq` when nothing changed — that is the normal, expected heartbeat case.
`seq` only needs to keep increasing within one `generation`; it is fine (expected) to
restart it at any value when `generation` goes up.

**Do this and the ordering/freshness guarantees below are automatic — you do not need to
implement anything else for them to work.**

## 3. Required vs optional fields, at a glance

| Field | Required? |
|---|---|
| `companions.schema` | **required**, must be exactly `"companion-v2"` |
| `companions.session.id`, `companions.session.seq` | **required** |
| `companions.items` | **required** (may be `[]`) |
| item `companion_id`, `vehicle_type`, `parent_vehicle_id` | **required** |
| item `assignment`, `link.state` | **required** |
| item `display_name`, `activity`, `position`, `battery`, `inspection`, `hazards` | optional |
| hazard `hazard_id`, `revision`, `source`, `kind`, `observed_at`, `geometry`, `disposition.state` | **required** (per hazard) |
| hazard `uncertainty_m`, `exclusion`, `replan` | optional |

Every field the operator doesn't reject renders as reported; every field you omit renders
as "not reported" — there is no default value invented on the operator side, ever.

## 4. Minimal companion item

```json
{
  "companion_id": "uav-1",
  "vehicle_type": "UAV",
  "parent_vehicle_id": "usv-2",
  "assignment": "ASSIGNED",
  "link": { "state": "NEVER_CONNECTED", "last_peer_contact_at": null }
}
```

That's a complete, valid item: a UAV assigned to Scout that hasn't been heard from yet.
`parent_vehicle_id` must be the ID of the vehicle sending this packet (any spelling Scout
already uses for itself works — `"usv-2"`, `"2"`, `2`, its callsign).

## 5. Units, coordinates, timestamps

- Coordinates are `{"lat": <deg>, "lng": <deg>}` objects — never a bare `[lat, lng]` array
  (that's ambiguous with GeoJSON's `[lng, lat]` convention). Decimal degrees, WGS-84.
  `(0, 0)` is treated as "no fix", not a real position near Africa — don't send it.
- Altitude needs BOTH `altitude_m` and `altitude_ref` (one of `AMSL`, `AGL`,
  `RELATIVE_HOME`) or neither — an altitude with no stated reference is dropped, because
  25 m AGL and 25 m AMSL are different real-world altitudes.
- Heading is degrees, 0–360, true north.
- All timestamps (`observed_at`, `last_peer_contact_at`, `decided_at`, `updated_at`) are
  **Unix epoch seconds as a float, on the SAME clock as the outer envelope's own
  `timestamp` field.** If your UAV telemetry arrives with its own onboard clock, convert it
  to Scout's clock before writing `observed_at` — the operator has no way to do that
  conversion itself.
- **Always send the outer envelope `timestamp`.** Without it, the operator cannot judge
  freshness at all (everything reads "timing unknown") and the session-restart ordering
  guarantee (§7) has no protection either.

## 6. Freshness — why "when it happened" matters more than "when you sent it"

The operator does NOT compute an observation's age as "when did the packet arrive minus
when did the observation happen". Three numbers exist on the operator side, and the operator
never tells you (or itself) that something is definitely "fresh" — only that it's proven
**stale**, or that staleness could not be proven (**uncertain**), or that no timing exists
at all (**unknown**):

- A **guaranteed lower bound** (`min_age_s`) — always correct, never assumes anything about
  delivery time. This is the ONLY number the operator uses to decide something is stale.
- An **estimate that includes how long your packet appears to have taken to arrive**
  (`estimated_age_s`) — shown to the operator alongside the lower bound, labelled as an
  estimate, but **never used to decide staleness or to mark anything as current**. Why not:
  it's computed by comparing YOUR envelope clock against the OPERATOR's own wall clock, so
  if your clock is ahead of the operator's by roughly the same amount your packet was
  genuinely delayed, the two errors cancel out and a real 30-second-old observation can look
  like it arrived with 0 delay. There is no way for the operator to tell that has happened
  from the packet alone, so it never trusts this number for a "not stale" verdict — only the
  lower bound can prove staleness, and nothing proves freshness.

**Practical consequence for you:** you do not get to make something show as "fresh" on the
operator by sending it quickly — the operator UI never displays a fresh verdict for an
individual measurement, only a lower-bound age and (if the observation is old enough) a
proven-stale marking. This is intentional, not a gap you need to work around.

**Nothing you need to implement for this** — it falls entirely out of you sending an
accurate `observed_at` (the real time of the UAV measurement) and an accurate envelope
`timestamp` (the real time you composed this packet). Do not backdate or "freshen up"
either value. If your own outbound queue held the message for a while before sending, that
is fine and expected — report the true composition/observation times regardless. The
accuracy of even the lower bound depends on your own clock being self-consistent between
`observed_at` and your envelope `timestamp`, and on your UAV→Scout time conversion being
correct — the operator has no way to verify either of those independently.

## 7. What happens if a packet is lost, delayed, or duplicated

You do not need to do anything special for any of these — they are handled by the operator
as long as you follow the "always send your current, complete truth" rule (§8):

- **Lost packet:** the next one you send (with your normal, current state) supersedes it.
  Nothing you sent is "missed forever" as long as you keep reporting.
- **Delayed/buffered packet:** see §6 — it will not be shown as fresher than it really is.
- **Duplicate/retransmitted packet:** re-sending the exact same content is harmless and
  expected; the operator treats it as a normal heartbeat, not as new information.
- **Your own subsystem restarting:** generate a new `session.id` AND increase your persisted
  `session.generation` (§2). The operator recognizes this as a legitimate restart **by the
  generation number going up**, not by the id being different, and starts fresh from
  whatever you report from that point — it does not try to merge or reconcile against what
  the old session reported.
- **A delayed packet from a run you've already restarted past:** if a buffered/delayed
  packet from an OLDER generation shows up after the operator has already seen a newer one
  (e.g. it got stuck somewhere and arrives late), the operator rejects it and does **not**
  let it undo what the newer generation already reported. This is exactly why `generation`
  must keep going up and must never go backward or repeat — if two different `session.id`
  values ever show up claiming the SAME `generation` number, the operator treats that as a
  contradiction it can't resolve and rejects the packet outright (including refusing to even
  count it as a "Scout is still alive" heartbeat).
- **The operator itself restarting:** it remembers, per vehicle, only the ordering numbers
  (`id`/`seq`/`generation`) it last saw — never your actual companion data, which always
  starts as "no companion reported" again until you send a fresh packet. This has one
  consequence for you: your very first packet after an operator restart is still checked
  against the generation/seq it remembered from before, so a genuine restart on your side
  still needs a genuinely higher `generation` than whatever you last sent it, even if you
  can't know exactly what the operator currently remembers.

## 8. The rule that matters most: keep resending your CURRENT truth

Every `companions` object you send is a **complete snapshot**, not an event or a diff.

- If a UAV is unassigned, send `"items": []` — and keep sending it on every subsequent
  packet, the same way you keep sending `telemetry` every packet even when nothing moved.
  **Do not send it only once.** If that one packet is lost, the operator will otherwise go
  on showing a UAV as assigned forever.
- If a hazard no longer applies, simply leave it out of `hazards` — and keep leaving it out.
  Same reasoning: the operator only knows what's in your MOST RECENT applied snapshot.
- A companion or hazard not present in your `items`/`hazards` list is treated as **removed**,
  not "unknown" — so don't omit something you still consider valid just to save bytes.

## 9. Hazards

```json
{
  "hazard_id": "hz-buoy-1",
  "revision": 1,
  "source": "uav-1:eo_camera",
  "kind": "BUOY",
  "observed_at": 1786175320.0,
  "geometry": { "type": "point", "lat": 56.7006, "lng": 13.0042 },
  "uncertainty_m": 8.0,
  "exclusion": { "type": "polygon", "ring": [
    {"lat": 56.70038, "lng": 13.00379}, {"lat": 56.70038, "lng": 13.00461},
    {"lat": 56.70082, "lng": 13.00461}, {"lat": 56.70082, "lng": 13.00379}
  ]},
  "disposition": { "state": "REPORTED" }
}
```

- `hazard_id` must stay the SAME across updates to the same real-world hazard. `revision`
  must go UP by any amount whenever anything about that hazard changes (including just its
  disposition or replan outcome) — it never resets and never repeats for different content.
- `geometry` is what the sensor actually saw: a `point` (with `uncertainty_m` as a radius)
  or a `polygon`. `exclusion` is a SEPARATE, optional field — the area you are proposing (or
  Scout has decided) to avoid; it does not have to match `geometry`'s shape.
- `disposition.state` is Scout's own verdict:
  - `REPORTED` / `UNDER_REVIEW` — proposed, not yet decided. Shown to the operator as
    "proposed", never as an accepted exclusion.
  - `ACCEPTED` — Scout has decided to treat `exclusion` as a real no-go area. **This is the
    ONLY value that makes the operator draw it as an accepted exclusion** — nothing else
    (not a hazard existing, not a route change) causes that.
  - `REJECTED` / `EXPIRED` — dismissed.
- `replan` is optional and describes what Scout did about an ACCEPTED hazard:
  `NOT_REQUIRED`, `PENDING`, `IN_PROGRESS`, `VERIFIED`, `FAILED`, `BLOCKED`.
  **Only send `VERIFIED` after Scout has itself read back and hash-verified the revised
  mission on the vehicle.** The operator takes this at face value and does not re-derive
  or double-check it — if you send `VERIFIED` prematurely, the operator will show it as
  verified. If you never send a `replan` at all, the operator shows "not reported", which
  is honest and preferred over guessing.

## 10. Validation the operator will silently apply (know before you test)

- Polygons: 3–64 points, no self-intersection, non-zero area, and no side longer than
  ~5 km across the whole shape.
- `uncertainty_m`: 0–1000.
- Numbers must be finite — no `NaN` / `Infinity` (most JSON libraries reject these anyway;
  Python's does not by default, so double-check if you're using `json.dumps` directly).
- `observed_at` may not be more than ~2 seconds ahead of your own envelope `timestamp`
  (small clock jitter is fine; a genuinely future timestamp is rejected), and not more than
  24 hours old.
- Anything rejected simply doesn't update that one field/hazard/companion — it does not
  crash the request or drop your other telemetry.

## 11. Full examples

**Assigned, not yet in contact:**
```json
{"companion_id":"uav-1","vehicle_type":"UAV","parent_vehicle_id":"usv-2",
 "display_name":"UAV-1","assignment":"ASSIGNED",
 "link":{"state":"NEVER_CONNECTED","last_peer_contact_at":null},
 "activity":{"state":"IDLE"},"hazards":[]}
```

**Actively inspecting, fresh telemetry:**
```json
{"companion_id":"uav-1","vehicle_type":"UAV","parent_vehicle_id":"usv-2",
 "display_name":"UAV-1","assignment":"ASSIGNED",
 "link":{"state":"CONNECTED","last_peer_contact_at":1786175326.6},
 "activity":{"state":"INSPECTING","task_id":"insp-0001","route_revision":3},
 "position":{"lat":56.7003,"lng":13.0035,"heading_deg":45.0,"altitude_m":25.0,
             "altitude_ref":"AGL","observed_at":1786175326.5},
 "battery":{"remaining_pct":71.0,"observed_at":1786175326.5},
 "inspection":{"progress_pct":40.0,"observed_at":1786175326.5},"hazards":[]}
```

**A proposed hazard** (goes in that item's `hazards` list — see §9 for the full object).

**Scout accepts it and starts replanning** — same `hazard_id`, `revision` bumped:
```json
"disposition":{"state":"ACCEPTED","reason":"Exclusion accepted","decided_at":1786175322.0},
"replan":{"state":"IN_PROGRESS","reason":"Computing a detour","updated_at":1786175322.0}
```

**Revised mission verified** — `revision` bumped again:
```json
"replan":{"state":"VERIFIED","mission_revision":4,"route_hash":"sha256:…",
          "updated_at":1786175335.0}
```

**Lost UAV contact** (keep reporting Scout's own status packet as normal — just describe
the UAV honestly):
```json
{"companion_id":"uav-1","link":{"state":"LOST","last_peer_contact_at":1786175300.0},
 "activity":{"state":"UNKNOWN"}, "...":"last known position/battery, unchanged"}
```

**Unassigning the UAV** — send this, and KEEP sending it every packet after (§8):
```json
"companions":{"schema":"companion-v2","session":{"id":"boot-3f9a2c1e","seq":13,"generation":3},"items":[]}
```

**Your companion-reporting process restarts** — new `session.id`, `generation` increased
from your persisted counter (`3 → 4`), `seq` restarts at 0:
```json
"companions":{"schema":"companion-v2","session":{"id":"boot-9a11f0c2","seq":0,"generation":4},"items":[ ... ]}
```

## 12. Minimum viable publisher for testing against the operator now

You can exercise this contract before any real UAV or radio link exists. A test script that
just `POST`s to `http://<operator-host>:<port>/agent/status` needs only:

```python
import time, json, urllib.request

session_id = "test-boot-1"
generation = 1   # bump this (and session_id) if you restart this script and want to
                 # exercise the operator's restart handling instead of resuming
seq = 0

def send(items):
    global seq
    seq += 1
    envelope = {
        "message_type": "status", "schema_version": "1.0", "source": "usv-2",
        "target": "operator", "timestamp": time.time(),
        "payload": {
            "usv_id": 2, "name": "Scout", "comm_state": "CONNECTED",
            "telemetry": {"lat": 56.7, "lng": 13.0, "battery": 80},
            "companions": {"schema": "companion-v2",
                          "session": {"id": session_id, "seq": seq, "generation": generation},
                          "items": items},
        },
    }
    req = urllib.request.Request(
        "http://127.0.0.1:8199/agent/status",
        data=json.dumps(envelope).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5)

now = time.time()
send([{"companion_id": "uav-1", "vehicle_type": "UAV", "parent_vehicle_id": "usv-2",
       "assignment": "ASSIGNED", "link": {"state": "CONNECTED", "last_peer_contact_at": now},
       "activity": {"state": "INSPECTING"},
       "position": {"lat": 56.701, "lng": 13.002, "altitude_m": 25, "altitude_ref": "AGL",
                    "observed_at": now}}])
```

The operator repository ships a much fuller version of exactly this idea — a
loopback-only, clearly-marked development fixture (`scripts/companion_fixture.py`) covering
every scenario in this document (assignment, hazards, delays, session restarts, clearing).
It is not something Scout runs; it is a reference for what a correct publisher looks like
and how the operator station is expected to react to it, useful for comparing your own
implementation's behaviour against.

## 13. Explicitly out of scope here

This contract does not define, and this integration does not require:
- UAV↔Scout serial/radio transport or protocol.
- UAV flight control or command path (there is none — this station can never send the UAV a
  command through this contract, on purpose).
- Onboard perception, detection, or classification algorithms.
- Obstacle-avoidance replanning logic itself (only *reporting the outcome* of Scout's own
  replanning is covered — see §9's `replan` field).

## 14. Unresolved / left for later

- No field yet exists for Scout to supply its own validated clock-offset or delivery-delay
  bound to sharpen the operator's freshness estimate beyond the guaranteed lower bound it
  can already compute from your envelope timestamp. This is deliberate for now — the
  operator will not guess at an upper bound it can't justify, so freshness stays "uncertain"
  rather than a fabricated "fresh". Flag it if a validated mechanism becomes worth building.
- The `session.generation` mechanism (§2, §7) is independent of the envelope `timestamp`,
  but freshness itself (§6) and the operator's OUTER per-vehicle packet-ordering guard (the
  one that rejects a `timestamp` that goes backward for your vehicle) still depend on you
  always sending an accurate envelope `timestamp`. That outer guard has its own known,
  pre-existing limitation — it has no upper bound, so a single accidentally-future
  `timestamp` from you can poison it for every normal packet afterward until real time
  catches up. This is a property of that guard, not of anything described in this document,
  and is out of scope here; just don't send a future timestamp.
- Nothing here specifies how Scout internally decides whether to accept a hazard or launch
  replanning — those decisions, and everything about UAV flight itself, remain entirely
  Scout/UAV-side design work this document does not attempt to anticipate.
- The operator's dock shows a compact green/yellow/red/grey tab for the link, built from
  `link.state` alone. Red ("previously connected, now lost") needs evidence the assignment
  was once `CONNECTED`/`DEGRADED` — nothing here lets you assert that directly today, so the
  operator falls back to its own local session history (reset on page reload) rather than
  ever inventing red from a bare `LOST`. An optional `link.ever_connected` boolean (true once
  YOU have ever observed this specific assignment connected) would let the operator show red
  reliably across a page reload too — not required; see `COMPANION_CONTRACT.md` §11.
