# Events verification

> **Superseded by [`event-log.md`](event-log.md).** This records the first,
> flattened-from-payload version of the Events page. The persistent server-side event
> store and acknowledgement flag documented in `event-log.md` replaced it. Kept as a
> dated historical record; not a description of current behavior.

**Backend**
- ✓ api.getEvents() (flattened per-vehicle payload events)

**Verified**
- ✓ severity mapping (severity/level/priority → EMERGENCY|WARNING|CAUTION|INFO)
- ✓ UNSPEC handling (untagged events, no fabricated severity)
- ✓ newest first
- ✓ filters (per-severity + unacknowledged, with counts)
- ✓ acknowledgement (session-local, caution-and-above)
- ✓ ribbon count (bell tracks unack, decrements on ack)
- ✓ no console errors
- ✓ classic dashboard intact (/)

**Known backend gaps**
- persistent event log
- persistent acknowledgement
