# Events verification

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
