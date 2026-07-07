// api.js — the ONLY module that talks to FastAPI.
// Pages/components call these methods; they never fetch() directly.
// If the backend changes, this file changes — not ten pages.
// Endpoints (see main.py): GET /api/fleet/status, GET /agent/status,
// POST /agent/status, GET /api/environment.

const BASE = "";

async function getJSON(path) {
  const res = await fetch(BASE + path, { headers: { "Accept": "application/json" } });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
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

/** Flattened event feed derived from per-vehicle payload events (until a dedicated endpoint exists). */
export async function getEvents() {
  const fleet = await getFleet();
  return fleet.flatMap((v) =>
    (v.events || []).map((e) => ({ vehicle: v.name, vehicleId: v.id, event: e }))
  );
}

/**
 * getAutonomy / getMissionScope are NOT YET in the backend — kept here so pages import
 * from one place; they return null so callers render the availability slot instead of
 * inventing data. getCommsHistory is now live (GET /api/comms/history/{id}).
 * See DATA_DICTIONARY.md and BACKEND_ROADMAP.md.
 */
/** Operator-side comms-state transition log for a vehicle (timeline + durations). */
export function getCommsHistory(id) { return getJSON(`/api/comms/history/${id}`); }
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
