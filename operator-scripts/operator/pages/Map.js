// Map.js — first migrated page. Full-bleed Leaflet map + live fleet from api.js,
// left dock (roster + mission progress), right inspector. Reuses shared components.
// IA first: markers/roster/inspector wired to real data; NO-TELEM slots where the
// backend can't back the mockup (comms timeline, mission ETA/remaining).
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
import { StatusBadges } from "../components/StatusBadges.js";
import { AuthoritySeg } from "../components/AuthoritySeg.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { COL, cls, commState, fmtAge, pad3, noTelem, opsStale } from "../lib/ui.js";
import { createAuthorityController } from "../lib/authority.js";
import { AVAIL, availSlot } from "../lib/availability.js";

const HOME = [56.699893, 13.002148];

// The primary map view carries ONLY the essential operational controls, split into
// two clearly separated cards so the operator never confuses commanding the vehicle
// with supervising the agent:
//   • Vehicle Commands — real ArduRover / Pixhawk modes + the ARM/DISARM safety pair.
//     Completely independent of the local agent. (GUIDED/HOLD live on the Vehicle page;
//     the map keeps the primary workflow short.)
//   • Agent Commands — supervisory MISSION pause/resume (NOT Pixhawk modes).
// Every button goes through the same command pipeline (api.createCommand) and its
// state is reported by the vehicle — never assumed from the click.
const MAP_MODES = [
  ["SET_MODE_AUTO", "AUTO"], ["SET_MODE_MANUAL", "MANUAL"],
  ["SET_MODE_LOITER", "LOITER"], ["RTL", "RTL"],
];
const MAP_SAFETY = [["ARM", "ARM"], ["DISARM", "DISARM"]];
const MAP_VEHICLE = [...MAP_MODES, ...MAP_SAFETY];
const MAP_MISSION = [["MISSION_PAUSE", "PAUSE MISSION"], ["MISSION_RESUME", "RESUME MISSION"]];
const HIGH_RISK = new Set(["ARM", "DISARM", "RTL", "SET_MODE_AUTO"]);
const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
// Command lifecycle → the operator-facing phase it represents (requested/pending,
// acknowledged, confirmed effective, rejected, timeout).
const CMD_PHASE = {
  QUEUED: ["requested", "u"], SENT: ["pending", "p"], ACCEPTED: ["acknowledged", "p"],
  EXECUTED: ["confirmed", "c"], REJECTED: ["rejected", "d"], FAILED: ["failed", "d"],
  EXPIRED: ["timed out", "u"],
};
const CMD_LABEL = Object.fromEntries([...MAP_MODES, ...MAP_SAFETY, ...MAP_MISSION]);

export function Map(root) {
  const L = window.L;
  let fleet = [], selId = null, env = null, map = null;
  let commsHist = null;          // comms-state transition log for the selected vehicle
  let cmds = [];                 // command queue + history for the selected vehicle
  // Control-authority state machine (pending → confirmed/rejected/timeout). Re-renders
  // the inspector on every phase change so the display always reflects the effective
  // authority confirmed by Scout, never the button press.
  const authCtl = createAuthorityController(() => renderInspector());
  const markers = {};
  const timers = [];

  root.className = "app has-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("map") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Live</span></div>
       <div class="veh-list" id="veh-list"><div class="empty-state" style="padding:10px 12px">Connecting…</div></div>
       <div class="mprog" id="mprog"></div>
     </div>
     <div class="map-wrap">
       <div id="map"></div>
       <div class="ov wind" id="wind"><div class="lbl">Wind</div><div class="arrow" id="wind-arrow">➜</div><div class="spd" id="wind-spd">—</div><div class="frm" id="wind-frm"></div></div>
     </div>
     <aside class="inspector" id="inspector"></aside>`;

  // Leaflet
  map = L.map("map", { zoomControl: true, attributionControl: false }).setView(HOME, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20 }).addTo(map);

  function makeIcon(v) {
    const st = commState(v), color = COL[st], stale = st !== "connected", sel = v.id === selId;
    // Circular USV dot (comm-colored, id inside) + a heading arrow orbiting above it.
    // Selection is the clean halo ring only. NOTE: the selected class is `is-sel`, not
    // `sel` — a bare `.sel` collides with the form-select style (theme.css) and paints
    // the marker as a large dark rectangle.
    return L.divIcon({
      className: "",
      html: `<div class="usv-marker${sel ? " is-sel" : ""}" style="opacity:${stale ? 0.82 : 1}">
        ${sel ? '<div class="selring"></div>' : ""}
        <div class="heading" style="transform:rotate(${v.heading == null ? 0 : v.heading}deg)"><span class="arw" style="color:${color}">▲</span></div>
        <div class="dot" style="background:${color}"><span>${v.id}</span></div>
        ${stale ? `<div class="age" style="color:${color}">${fmtAge(v.last_seen_age_s)}</div>` : ""}
      </div>`,
      iconSize: [40, 40], iconAnchor: [20, 20],
    });
  }

  function updateMarkers() {
    fleet.forEach((v) => {
      // Never plot a fabricated position: a vehicle that has not reported a valid
      // position (never contacted / no GPS fix) has no marker rather than a fake one.
      if (v.lat == null || v.lng == null) {
        if (markers[v.id]) { map.removeLayer(markers[v.id]); delete markers[v.id]; }
        return;
      }
      const ll = [v.lat, v.lng];
      if (!markers[v.id]) {
        markers[v.id] = L.marker(ll, { icon: makeIcon(v) }).addTo(map).on("click", () => select(v.id));
      } else {
        markers[v.id].setLatLng(ll).setIcon(makeIcon(v));
      }
    });
  }

  function activity(v) { return v.status || v.mission || (v.telemetry && v.telemetry.mode) || "—"; }

  function renderDock() {
    const list = document.getElementById("veh-list");
    list.innerHTML = vehicleRows(fleet, selId);
    list.querySelectorAll(".vrow").forEach((el) => (el.onclick = () => select(+el.dataset.id)));

    // Mission progress — backend has (optional) per-vehicle coverage but no mission-level
    // ETA/remaining or named scope, so those slots are honest NO-TELEM.
    const cov = fleet.map((v) => v.coverage).find((c) => c != null);
    document.getElementById("mprog").innerHTML =
      `<div class="row"><span class="lbl">Mission progress</span></div>` +
      (cov != null
        ? `<div class="top"><span class="lbl">Coverage</span><span class="pct mono">${cov}%</span></div>
           <div class="bar"><i style="width:${cov}%;background:var(--connected)"></i></div>`
        : `<div class="no-telem-box"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20V10M12 20V4M20 20v-7"/></svg>Coverage not reported</div>`) +
      `<div class="mgrid">
         <div><span class="lbl">Remaining</span><span class="v">${noTelem()}</span></div>
         <div><span class="lbl">ETA</span><span class="v">${noTelem()}</span></div>
       </div>`;
  }

  function normEvent(e) {
    if (e == null) return "";
    if (typeof e === "string") return e;
    return e.title || e.message || e.text || e.event || e.name || JSON.stringify(e);
  }

  // Comms-state transition log for the selected vehicle (GET /api/comms/history/{id}).
  // Loaded on selection + refreshed on a timer; cached in commsHist and re-rendered.
  function loadCommsHistory(id) {
    if (id == null) { commsHist = null; return; }
    api.getCommsHistory(id).then((h) => {
      if (id === selId) { commsHist = h; renderInspector(); }
    }).catch(() => {});
  }

  // Control authority for the selected vehicle — a direct, dedicated read (GET
  // /api/control_authority/{id}, itself a live proxy to Scout's own Flask API), NOT
  // part of the fleet payload and NOT the command queue. Loaded on selection +
  // refreshed on a timer. Fed into the authority controller, which confirms any
  // pending hand-off against the effective value Scout reports. A network failure
  // reads as unreachable/unknown — never a guessed authority.
  function loadAuthority(id) {
    if (id == null) return;
    api.getControlAuthority(id).then((a) => {
      if (id === selId) authCtl.setServer(a);
    }).catch(() => {
      if (id === selId) authCtl.setServer({ ok: true, available: true, reachable: false, authority: null });
    });
  }

  // Command queue + history for the selected vehicle (the reverse/control path). Used
  // to show each command's lifecycle: requested → sent → acknowledged → confirmed /
  // rejected / timed-out. Never assumes success — the vehicle reports the status.
  function loadCommands(id) {
    if (id == null) { cmds = []; return; }
    api.getCommands(id).then((d) => {
      if (id === selId) { cmds = (d && d.commands) || []; renderInspector(); }
    }).catch(() => { if (id === selId) { cmds = []; renderInspector(); } });
  }

  async function sendCommand(type) {
    const v = fleet.find((x) => x.id === selId);
    const vname = v ? (v.name || "USV-" + v.id) : "vehicle";
    const label = CMD_LABEL[type] || type;
    const highRisk = HIGH_RISK.has(type);
    if (highRisk &&
        !window.confirm(`Confirm ${label} for ${vname}?\n\nThe command is queued for the vehicle to execute. It is NOT applied until the local agent reports back.`)) return;
    let res = await api.createCommand({ vehicle_id: selId, type, confirm: highRisk });
    if (!res.ok && res.data && res.data.needs_confirmation) {
      if (!window.confirm(`${res.data.message}\n\nQueue anyway?`)) return;
      res = await api.createCommand({ vehicle_id: selId, type, confirm: true });
    }
    if (!res.ok) window.alert((res.data && res.data.message) || "Command was not accepted.");
    loadCommands(selId);
  }

  function commsTimeline() {
    const h = commsHist;
    const clk = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>';
    if (!h || !Array.isArray(h.transitions) || !h.transitions.length)
      return `<div class="no-telem-box">${clk}No communication transitions recorded yet</div>`;
    const rows = h.transitions.slice(-6).reverse().map((t) => {
      const color = COL[commState(t.state)] || COL.unknown;
      const tm = t.ts ? new Date(t.ts).toLocaleTimeString([], { hour12: false }) : "—";
      const from = t.from ? `${t.from} → ` : "";
      return `<div class="ev"><span class="sv" style="background:${color}"></span><span class="tm">${tm}</span><span class="tx">${from}${t.state}</span></div>`;
    }).join("");
    const disc = h.durations_s && h.durations_s.DISCONNECTED;
    const foot = disc
      ? `<div class="mgrid" style="margin-top:8px"><div><span class="lbl">Total disconnected</span><span class="v txt-d">${Math.round(disc)}s</span></div></div>`
      : "";
    return `<div class="events">${rows}</div>${foot}`;
  }

  function renderInspector() {
    const box = document.getElementById("inspector");
    const v = fleet.find((x) => x.id === selId);
    if (!v) { box.innerHTML = `<div class="isec"><div class="empty-state">No vehicle selected</div></div>`; return; }
    const st = commState(v), stale = st !== "connected", t = v.telemetry || {};
    const events = Array.isArray(v.events) ? v.events.slice(-6).reverse() : [];

    // Control authority — a dedicated fetch fed through the authority controller
    // (pending → confirmed/rejected/timeout), NOT fleet data and NOT the command queue.
    //   Take Control  → request OPERATOR (operator holds the wheel; commands enabled)
    //   Release Control → request LOCAL_AGENT (autonomy resumes)
    // Vehicle/agent commands stay disabled unless authority is a CONFIRMED OPERATOR —
    // never enabled optimistically on a button press or while a request is in flight.
    const av = authCtl.view();
    const authVal = stale ? null : av.value;
    const busy = av.phase === "pending";
    const hasControl = !stale && av.hasControl;
    const canTake = av.available && !stale && !hasControl && !busy;
    const canRelease = av.available && !stale && av.value === "OPERATOR" && !busy;

    box.innerHTML = `
      <div class="isec">
        <div class="idcard">
          <div class="idtop">
            <div class="idrow"><span class="idname">${v.name || "USV-" + v.id}</span>${CommsPill(v, { full: true })}</div>
            <div class="idassign">${String(activity(v))}</div>
          </div>
          <div class="idcontact ${stale ? "warn" : ""}">
            <span class="big txt-${cls(v)}">${v.last_seen_age_s == null ? "—" : Math.round(v.last_seen_age_s)}</span><span class="u">s ago</span>
            <span class="cap"><span class="lbl">Last contact</span><span class="mono" style="font-size:11px;color:var(--muted)">${v.online ? "online" : "—"}</span></span>
          </div>
        </div>
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Status</span></div>
        ${StatusBadges(v, authVal, { phase: av.phase, pending: av.pending })}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Control authority</span>${AuthoritySeg(authVal, { phase: av.phase, pending: av.pending })}</div>
        <div class="qa">
          <button data-authority="OPERATOR" ${canTake ? "" : "disabled"} title="Take Control — request OPERATOR authority">Take Control</button>
          <button data-authority="LOCAL_AGENT" ${canRelease ? "" : "disabled"} title="Release Control — hand authority back to the Local Agent">Release Control</button>
        </div>
        ${authNote(av, stale)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Vehicle Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">Pixhawk · state reported by vehicle</span></div>
        ${vehicleCommands(hasControl, av, stale)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Agent Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">supervisory · local agent</span></div>
        ${agentCommands(hasControl, av, stale, v)}
      </div>
      <div class="isec">
        <div class="tele ${stale ? "stale" : ""}">
          <div class="cell full batt"><div class="k">Battery</div><div style="margin-top:5px">${BatteryBar(v.battery)}</div></div>
          <div class="cell"><div class="k">Ground speed</div><div class="v">${v.speed == null ? "—" : v.speed}<small> ${v.speed == null ? "" : "m/s"}</small></div></div>
          <div class="cell"><div class="k">Heading</div><div class="v">${v.heading == null ? "—" : pad3(v.heading)}<small>${v.heading == null ? "" : "°"}</small></div></div>
          <div class="cell"><div class="k">Latitude</div><div class="v" style="font-size:13px">${v.lat != null ? (+v.lat).toFixed(5) : "—"}</div></div>
          <div class="cell"><div class="k">Longitude</div><div class="v" style="font-size:13px">${v.lng != null ? (+v.lng).toFixed(5) : "—"}</div></div>
          <div class="cell"><div class="k">Mode</div><div class="v" style="font-size:13px">${stale ? "UNKNOWN" : (t.mode || "—")}</div></div>
        </div>
        ${stale ? `<div class="stale-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>Telemetry as of ${fmtAge(v.last_seen_age_s)} ago — not live</div>` : ""}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Communication · transitions</span></div>
        ${commsTimeline()}
      </div>
      <div class="isec" style="border-bottom:none">
        <div class="sec-title"><span class="lbl">Recent events</span></div>
        <div class="events">${events.length ? events.map((e) => `<div class="ev"><span class="sv" style="background:var(--muted)"></span><span class="tx">${normEvent(e)}</span></div>`).join("") : '<div class="empty-state">No recent events</div>'}</div>
      </div>`;

    box.querySelectorAll(".qa button[data-authority]").forEach((btn) => {
      btn.onclick = () => {
        const target = btn.dataset.authority;
        if (target === "OPERATOR" &&
            !window.confirm(`Take control of ${v.name || "USV-" + v.id}?\n\nThis requests OPERATOR authority so operator commands can execute. It does NOT arm the vehicle or change its mode.`)) return;
        authCtl.request(target, (a) => api.setControlAuthority(v.id, a));
      };
    });
    box.querySelectorAll("button[data-cmd]").forEach((btn) => {
      btn.onclick = () => sendCommand(btn.dataset.cmd);
    });
  }

  // Pending/rejected/timeout notice for the authority hand-off, plus the honest
  // "no authority source / unknown / RC override" states. Never claims success on a
  // click — only the effective value confirmed by Scout settles it.
  function authNote(av, stale) {
    if (stale) return `<div class="auth-note warn">Authority is UNKNOWN — telemetry is stale. Commands are locked until the link is current.</div>`;
    const p = av.pending;
    if (p && p.phase === "pending") return `<div class="auth-note pending">Requesting ${p.requested === "OPERATOR" ? "OPERATOR" : "LOCAL_AGENT"} authority — awaiting confirmation from the vehicle…</div>`;
    if (p && p.phase === "confirmed") return `<div class="auth-note ok">Authority confirmed: ${av.value}.</div>`;
    if (p && p.phase === "rejected") return `<div class="auth-note warn">Request rejected — ${p.reason || "not accepted"}.</div>`;
    if (p && p.phase === "timeout") return `<div class="auth-note warn">Request timed out — ${p.reason || "no confirmation"}.</div>`;
    if (!av.available) return `<div class="auth-note">No control-authority source for this vehicle.</div>`;
    if (!av.reachable) return `<div class="auth-note warn">Control-authority service unreachable — authority is UNKNOWN.</div>`;
    if (av.value === "RC") return `<div class="auth-note warn">RC transmitter override is active — it holds physical control.</div>`;
    if (av.value === "OPERATOR") return `<div class="auth-note ok">Operator holds control — commands enabled.</div>`;
    if (av.value === "LOCAL_AGENT") return `<div class="auth-note">Local Agent holds control — Take Control to command directly.</div>`;
    return "";
  }

  // A row of command buttons; the ARM/DISARM etc. high-risk ones carry the caution
  // style + a confirmation. Enabled only on confirmed OPERATOR authority.
  function cmdBtns(items, hasControl) {
    return `<div class="ctl-cmds${hasControl ? "" : " locked"}">` +
      items.map(([type, label]) => {
        const hr = HIGH_RISK.has(type);
        return `<button class="ctl-cmd${hr ? " hr" : ""}" data-cmd="${type}"${hasControl ? "" : " disabled"} title="${type}${hr ? " · confirmation required" : ""}">${label}</button>`;
      }).join("") + `</div>`;
  }
  function lockNote(hasControl, av, stale) {
    if (hasControl) return "";
    return `<div class="ctl-lock-note">${lockSvg}<span>${stale ? "Link not current" : av.value === "OPERATOR" ? "" : "Commands are locked"} — Take Control (OPERATOR, Scout-confirmed) to enable.</span></div>`;
  }

  // Vehicle/Pixhawk commands ONLY — real ArduRover modes + the ARM/DISARM safety pair.
  // Independent of the local agent. The "Last command" line reports the queue lifecycle.
  function vehicleCommands(hasControl, av, stale) {
    return cmdBtns(MAP_VEHICLE, hasControl) + lockNote(hasControl, av, stale) + cmdStatus();
  }

  // Agent/supervisory commands ONLY — pause/resume the mission the local agent runs —
  // plus the agent's current and immediately-previous status (no long history here;
  // the Agent page owns the full reasoning view). Current status is approximated from
  // mission_state (LIVE while connected, LAST KNOWN when stale); the previous status
  // needs an onboard decision log the agent does not emit yet → honest gap.
  function agentCommands(hasControl, av, stale, v) {
    return cmdBtns(MAP_MISSION, hasControl) + lockNote(hasControl, av, stale) + agentStatusBlock(v);
  }

  function agentStatusBlock(v) {
    const connected = commState(v) === "connected";
    const age = v.last_seen_age_s, hasContact = age != null;
    const raw = v.status || (v.mission_data && v.mission_data.mission_state) || null;
    const behavior = (hasContact && raw && !["unknown", "lost"].includes(String(raw).toLowerCase())) ? raw : null;
    const curSlot = behavior
      ? availSlot(connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN, { value: behavior, age: connected ? null : age })
      : availSlot(AVAIL.GAP, { label: hasContact ? "No data" : "No contact", dev: "agent status approx. from mission_state" });
    const prevSlot = availSlot(AVAIL.GAP, { label: "Unavailable", dev: "agent must emit a decision/status log for the previous state" });
    return `<div class="agent-status">
      <div class="asrow"><span class="k">Current agent status</span><span class="v">${curSlot}</span></div>
      <div class="asrow"><span class="k">Previous agent status</span><span class="v">${prevSlot}</span></div>
    </div>`;
  }

  // Most-relevant command's lifecycle phase (requested/pending/acknowledged/confirmed/
  // rejected/timeout), reported by the vehicle via the queue — never assumed.
  function cmdStatus() {
    if (!cmds.length) return "";
    const TERMINAL = ["EXECUTED", "REJECTED", "FAILED", "EXPIRED"];
    const c = cmds.find((x) => !TERMINAL.includes(x.status)) || cmds[0];
    const [phase, k] = CMD_PHASE[c.status] || ["—", "u"];
    const note = c.reason || c.warning || "";
    return `<div class="cmd-status"><span class="lbl">Last command</span><span class="ctl-type mono">${CMD_LABEL[c.type] || c.type}</span><span class="pill ${k}">${phase}</span>${note ? `<span class="cmd-reason" title="${note.replace(/"/g, "&quot;")}">${note}</span>` : ""}</div>`;
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function select(id) {
    if (id !== selId) {
      selId = id; commsHist = null; loadCommsHistory(id);
      authCtl.reset(); loadAuthority(id);
      cmds = []; loadCommands(id);
    }
    // Snap the map to the selected vehicle (only if it has a known position — a
    // never-contacted vehicle has none, so there is nothing to snap to).
    const v = fleet.find((x) => x.id === id);
    if (map && v && v.lat != null && v.lng != null) map.panTo([v.lat, v.lng]);
    renderDock(); renderInspector(); updateMarkers();
  }

  // Default to a vehicle that is actually reporting (so the primary view opens on a
  // live vehicle with a real authority source) rather than blindly the first template
  // row — picking a placeholder vehicle 1 is what made the authority poll hit an
  // unconfigured id and 404 in a loop.
  function defaultSelection() {
    return (fleet.find((v) => v.online) || fleet.find((v) => v.lat != null) || fleet[0]).id;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) {
      selId = defaultSelection();
      loadCommsHistory(selId); loadAuthority(selId); loadCommands(selId);
    }
    updateMarkers(); renderDock(); renderInspector();
    updateRibbon({ counts: counts() });
  }
  function onEnv(e) {
    env = e || {};
    const s = document.getElementById("wind-spd"), a = document.getElementById("wind-arrow"), f = document.getElementById("wind-frm");
    if (env.wind_speed == null || env.wind_direction == null) { s.textContent = "No data"; a.style.opacity = ".3"; f.textContent = ""; }
    else { s.textContent = (+env.wind_speed).toFixed(1) + " m/s"; a.style.opacity = "1"; a.style.transform = `rotate(${(+env.wind_direction + 180) % 360}deg)`; f.textContent = `from ${env.wind_direction}°`; }
  }

  // polling + clock
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const stopEnv = api.poll(api.getEnvironment, 10000, onEnv, () => {});
  timers.push(setInterval(() => loadCommsHistory(selId), 3000));  // refresh selected vehicle's comms log
  timers.push(setInterval(() => loadAuthority(selId), 2000));  // refresh selected vehicle's control authority
  timers.push(setInterval(() => loadCommands(selId), 3000));  // refresh selected vehicle's command lifecycle
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() {
    stopFleet(); stopEnv(); clearInterval(clockId); timers.forEach(clearInterval);
    authCtl.dispose();
    if (map) { map.remove(); map = null; }
  };
}
