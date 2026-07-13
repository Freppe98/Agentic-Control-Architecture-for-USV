// api.js — the ONLY module that talks to FastAPI.
// Pages/components call these methods; they never fetch() directly.
// If the backend changes, this file changes — not ten pages.
// Endpoints (see main.py): GET /api/fleet/status, GET /agent/status,
// POST /agent/status, GET /api/comms/history/{id}, GET /api/events,
// GET /api/environment, GET/POST /api/control_authority/{id} (Scout proxy),
// POST /api/commands, GET /api/commands/{id} (reverse command queue).

const BASE = "";

async function getJSON(path) {
  const res = await fetch(BASE + path, { headers: { "Accept": "application/json" } });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

/**
 * POST helper that does NOT throw on 4xx — command endpoints answer 409
 * (needs_confirmation) with a meaningful body the caller must act on. Returns
 * { ok, status, data } so pages can branch on it.
 */
async function postJSON(path, body) {
  const res = await fetch(BASE + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "Accept": "application/json" },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* empty body */ }
  return { ok: res.ok, status: res.status, data };
}

/** Full fleet, already normalized server-side (comm_state, last_seen_age_s, telemetry, …). */
export function getFleet() {
  return getJSON("/api/fleet/status");
}

/** Single vehicle by id (derived from the fleet list; one endpoint today). */
export async function getVehicle(id) {
  const fleet = await getFleet();
  return fleet.find((v) => v.id === id) || null;
}

/** Weather / wind for the map overlay. */
export function getEnvironment() {
  return getJSON("/api/environment");
}

/** Raw latest agent status envelope (debug / Messages page). */
export function getAgentStatus() {
  return getJSON("/agent/status");
}

/**
 * Persistent event log from the operator backend (GET /api/events): comms-state
 * transitions + vehicle-reported events, from one server-side store. Adapted here
 * to the {vehicle, vehicleId, event} shape the Events page already consumes, so the
 * page is unchanged — the backend swap lives in this module, not in the page.
 */
export async function getEvents() {
  const data = await getJSON("/api/events");
  const list = Array.isArray(data) ? data : (data.events || []);
  return list.map((e) => ({
    vehicle: e.vehicle,
    vehicleId: e.vehicle_id,
    event: {
      id: e.id,
      severity: e.severity,
      timestamp: e.ts,
      message: e.message,
      type: e.type,
      source: e.source,
      acknowledged: e.acknowledged,
    },
  }));
}

/**
 * getMissionScope is NOT YET in the backend — kept here so pages import from one place;
 * it returns null so callers render the availability slot instead of inventing data.
 * getCommsHistory / getEvents are live. See DATA_DICTIONARY.md and BACKEND_ROADMAP.md.
 */
/** Operator-side comms-state transition log for a vehicle (timeline + durations). */
export function getCommsHistory(id) { return getJSON(`/api/comms/history/${id}`); }

/**
 * Flat operator event log (GET /api/events) for the Agent page's Previous Decision +
 * Recent Timeline: comms, agent-decision, mission, authority and command transitions,
 * chronological (oldest→newest). Flat shape { id, ts, severity, message, type, source,
 * vehicleId, vehicle } — distinct from getEvents(), which adapts to the Events page.
 */
export async function getEventLog() {
  const data = await getJSON("/api/events");
  const list = Array.isArray(data) ? data : (data.events || []);
  return list.map((e) => ({
    id: e.id, ts: e.ts, severity: e.severity, message: e.message,
    type: e.type, source: e.source, vehicleId: e.vehicle_id, vehicle: e.vehicle,
  }));
}

/**
 * Agent reasoning for a vehicle. Now sourced directly from the fleet payload's
 * `agent_status` block (payload.agent.* the backend forwards — behaviour, decision
 * reason, policy, autonomy level, …). The Agent page reads it off the vehicle object;
 * this helper stays so callers have a single import point. Returns the block or {}.
 */
export async function getAgentReasoning(id) {
  const v = await getVehicle(id);
  return (v && v.agent_status) || {};
}

export async function getMissionScope()         { return null; } // needs named-mission registry

// --- Control authority (dedicated API — deliberately NOT the command queue) ---
// Scout Flask (motherpi/services/flask) is the sole source of truth for control
// authority ("OPERATOR" default — RC has exclusive authority — or "LOCAL_AGENT",
// engaged by the operator). The operator backend holds no authority state of its
// own; every call below is a live, synchronous proxy to Scout. See main.py and
// docs/verification/commands.md ("Control authority").

/** Confirmed control authority for a vehicle ("OPERATOR" or "LOCAL_AGENT"), read live from Scout. */
export function getControlAuthority(id) { return getJSON(`/api/control_authority/${id}`); }

/**
 * Request a control-authority hand-off (Take Control / Release Control) — a direct
 * proxy to Scout Flask's own POST /agent/control_authority. Returns { ok, status,
 * data }: data is Scout's response verbatim on success, or the backend's error body
 * (e.g. Scout unreachable) on failure — never thrown, so callers can branch on it.
 * On a confirmed Release (OPERATOR), the operator backend also cancels any still-
 * pending commands for this vehicle (see cancel_pending_commands in main.py) —
 * queue safety, not an authority value the backend invents.
 */
export function setControlAuthority(id, authority) {
  return postJSON(`/api/control_authority/${id}`, { authority });
}

// --- Command queue (reverse/control path) ---
// Gated in the UI by control authority above (buttons disabled unless the latest
// Scout-confirmed authority is LOCAL_AGENT) — the backend queue itself only gates
// on comm-state + high-risk confirmation (see main.py POST /api/commands).

/** Create a command. body: { vehicle_id, type, params?, confirm? }. Returns { ok, status, data }. */
export function createCommand(body) { return postJSON("/api/commands", body); }

/** All commands for a vehicle (active queue + history) — the panel view. */
export function getCommands(id) { return getJSON(`/api/commands/${id}`); }

// --- Pixhawk mission (view-only readback — a live Scout proxy, NOT the command queue) ---
// The mission currently STORED ON THE PIXHAWK for a vehicle (what the flight controller
// actually holds), fetched on demand for testing/verification. Deliberately separate
// from mission_state/coverage progress and from the operator command queue. The backend
// (GET /api/vehicles/{id}/pixhawk-mission) proxies live to Scout and always answers with
// a stable schema — { available, reachable, count, current_seq, waypoints[], partial } —
// so an unreachable Scout is an honest reachable:false, never a thrown fetch. See main.py.

/** Fetch the mission stored on a vehicle's Pixhawk (live Scout readback). */
export function getPixhawkMission(id) { return getJSON(`/api/vehicles/${id}/pixhawk-mission`); }

/** Small polling helper so pages don't each reinvent setInterval + error handling. */
export function poll(fn, ms, onData, onError) {
  let stopped = false;
  async function tick() {
    if (stopped) return;
    try { onData(await fn()); }
    catch (err) { if (onError) onError(err); }
    if (!stopped) setTimeout(tick, ms);
  }
  tick();
  return () => { stopped = true; };
}
