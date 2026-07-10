// api.js — the ONLY module that talks to FastAPI.
// Pages/components call these methods; they never fetch() directly.
// If the backend changes, this file changes — not ten pages.
// Endpoints (see main.py): GET /api/fleet/status, GET /agent/status,
// POST /agent/status, GET /api/comms/history/{id}, GET /api/events,
// GET /api/environment.

const BASE = "";

async function getJSON(path) {
  const res = await fetch(BASE + path, { headers: { "Accept": "application/json" } });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

/**
 * POST helper that does NOT throw on 4xx — command/authority endpoints answer 409
 * (needs_confirmation / control not engaged) with a meaningful body the caller must
 * act on. Returns { ok, status, data } so pages can branch on it.
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
 * getAutonomy / getMissionScope are NOT YET in the backend — kept here so pages import
 * from one place; they return null so callers render the availability slot instead of
 * inventing data. getCommsHistory is now live (GET /api/comms/history/{id}).
 * See DATA_DICTIONARY.md and BACKEND_ROADMAP.md.
 */
/** Operator-side comms-state transition log for a vehicle (timeline + durations). */
export function getCommsHistory(id) { return getJSON(`/api/comms/history/${id}`); }

// --- Control authority + command queue (reverse/control path) ---
// Authority (OPERATOR default / LOCAL_AGENT engaged) is independent of comm-state and
// gates whether commands may be created/delivered. It also rides on getFleet() as
// v.authority, so pages usually read it from there; these are for reads/actions.

/** Current control authority for one vehicle. */
export function getAuthority(id) { return getJSON(`/api/authority/${id}`); }

/** Engage ("LOCAL_AGENT") / release ("OPERATOR") control. Returns { ok, status, data }. */
export function setAuthority(id, authority, by = "operator") {
  return postJSON(`/api/authority/${id}`, { authority, by });
}

/** Create a command. body: { vehicle_id, type, params?, confirm? }. { ok, status, data }. */
export function createCommand(body) { return postJSON("/api/commands", body); }

/** All commands for a vehicle (active queue + history) — the panel view. */
export function getCommands(id) { return getJSON(`/api/commands/${id}`); }
export async function getAutonomy(/* id */)     { return null; } // needs agent reasoning fields
export async function getMissionScope()         { return null; } // needs named-mission registry

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
