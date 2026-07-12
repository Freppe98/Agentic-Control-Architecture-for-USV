# Command & Control panel (Vehicle page)

Adds a command panel to the Vehicle page's **Control** card: Take Control / Release
Control, the nine reverse-path command buttons (AUTO/MANUAL/HOLD/GUIDED/RTL/PAUSE/
RESUME/ARM/DISARM), and a live command queue/history.

**Revision (superseding an earlier version of this doc):** this panel was first built
against a new, independent backend-owned authority store (`authority_by_id`,
`GET/POST /api/authority/{id}`, default OPERATOR, gating command create/deliver). That
duplicated the existing control-authority design and could show the operator two
different, possibly contradictory "who's in control" answers on the same page. It has
been removed. **Scout Flask remains the sole source of truth for control authority** —
see `commands.md` ("Control authority"). This doc now only covers the panel UI and how
it plugs into the existing Scout-proxy endpoints and command queue.

## Model
- Authority is read live from `GET /api/control_authority/{vehicle}` (Scout proxy, not
  cached — see `commands.md`). There is no operator-backend authority store.
- The panel's command buttons are enabled **only** when the latest Scout-confirmed
  authority is `LOCAL_AGENT`. Same gate as the Map page's Take Control / Release
  Control and the Vehicle page's own Authority row (`AuthoritySeg`) — one value, read
  once per page, never duplicated into a second opinion.
- Release must **first** succeed against Scout (`POST /api/control_authority/{vehicle}`
  `{authority:"OPERATOR"}`); only after Scout confirms does the backend cancel
  still-pending commands (`cancel_pending_commands` in `main.py`, called from
  `set_control_authority` — see `commands.md`). Take Control does not touch the queue.

## Backend (`main.py`)
- No authority state, no `/api/authority/{id}`. The command queue
  (`POST /api/commands`, `GET /api/commands/pending/{id}`) is gated only by comm-state
  and high-risk confirmation, as before — **not** by a stored authority value; the UI
  gate (buttons disabled unless Scout says LOCAL_AGENT) is what keeps commands from
  being issued while RC/manual holds authority.
- `cancel_pending_commands` (unchanged) still expires in-flight commands on a
  Scout-confirmed Release, so a stale QUEUED/SENT command can never fire after a later
  Take Control.

## Frontend (Vehicle page)
- `services/api.js`: `createCommand`, `getCommands` (command queue); `getControlAuthority`
  / `setControlAuthority` (Scout proxy, unchanged); `postJSON` returns `{ ok, status,
  data }` and does not throw on 4xx, so the panel can act on `needs_confirmation`
  without a try/catch per call.
- The panel lives inside the existing **Control** card (`Vehicle.js`), not a separate
  section — one Control area on the page, not two.  High-risk (ARM/DISARM/RTL/AUTO) are
  amber and prompt an extra confirmation (sent `confirm:true`); a DISCONNECTED create
  prompts "queue anyway?". The queue shows only backend-reported status — the UI never
  asserts a command executed.

## Notes / honesty
- Taking or releasing control performs **no** vehicle action by itself (no arm/disarm/
  mode change) — it only changes who may issue commands; Scout's own
  `/agent/control_authority` is the only place that value lives.
- Command execution is still only ever confirmed by a Local Agent result (see
  `commands.md`).
