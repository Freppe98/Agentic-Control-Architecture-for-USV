// Vehicle.js — per-hull systems. Fleet systems matrix + subsystem breakdown for
// the selected vehicle, from live/derived fields. Subsystem severity is derived
// from real inputs (battery, leak, disk, cpu, comms, gps/heading); everything not
// in the telemetry schema (temps, voltages, compass cal, rssi, latency) renders
// NO-TELEM. Reuses VehicleDock, CommsPill, BatteryBar, HealthBadge, ui helpers.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, cls, fmtAge, pad3, noTelem } from "../lib/ui.js";

const MXCOLS = [["battery", "Battery"], ["sensors", "Sensors"], ["gps", "GPS"], ["compass", "Compass"], ["storage", "Storage"], ["cpu", "CPU"], ["network", "Network"]];
const SEV_ORDER = { ok: 0, caution: 1, warn: 2 };

// Command & Control: the safe command set for the reverse path. High-risk commands
// (ARM/DISARM touch the motors; AUTO/RTL change what the vehicle does on its own) get an
// extra operator confirmation and are sent with confirm:true. Labels are the operator's
// shorthand; the value is the backend command type.
const CMDS = [
  ["SET_MODE_AUTO", "AUTO"], ["SET_MODE_MANUAL", "MANUAL"], ["SET_MODE_HOLD", "HOLD"],
  ["SET_MODE_GUIDED", "GUIDED"], ["RTL", "RTL"], ["MISSION_PAUSE", "PAUSE"],
  ["MISSION_RESUME", "RESUME"], ["ARM", "ARM"], ["DISARM", "DISARM"],
];
const HIGH_RISK = new Set(["ARM", "DISARM", "RTL", "SET_MODE_AUTO"]);
const CMD_STATUS_CLS = { QUEUED: "u", SENT: "p", ACCEPTED: "p", EXECUTED: "c", REJECTED: "d", FAILED: "d", EXPIRED: "u" };

export function Vehicle(root) {
  let fleet = [], selId = null, cmds = [];

  root.className = "app dock-main";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("vehicle") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Systems</span></div>
       <div class="veh-list" id="veh-list"></div>
       <div class="dock-foot">
         <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>
         <span>Health (faults) and Comms (link state) are independent axes. Values tagged <b>no telem</b> are not yet in the telemetry schema.</span>
       </div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Vehicle</h1><span class="count mono" id="vcount">—</span></div>
       <div class="vcontent">
         <div class="sect"><span class="lbl">Command &amp; control</span><span class="tag" id="ctltag">selected vehicle</span></div>
         <div id="control"></div>
         <div class="sect"><span class="lbl">Fleet systems matrix</span><span class="tag">vehicles × subsystems · click a row for detail</span></div>
         <div class="mxwrap" id="mxwrap"></div>
         <div class="sect"><span class="lbl">Vehicle detail</span><span class="tag" id="dettag"></span></div>
         <div class="detail" id="detail"></div>
       </div>
     </div>`;

  // ---- derive subsystem severity + display value from live data ----
  function subsys(v) {
    const h = v.health || {}, comm = commState(v);
    const num = (x) => (x == null ? null : x);
    return {
      battery: v.battery == null ? { sev: null } : { sev: v.battery < 20 ? "warn" : v.battery < 40 ? "caution" : "ok", val: v.battery + "%" },
      sensors: h.leak_detected === true ? { sev: "warn", val: "LEAK" } : { sev: "ok", val: "OK" },
      gps: v.lat != null && v.lng != null ? { sev: "ok", val: "3D" } : { sev: "caution", val: "NO FIX" },
      compass: v.heading != null ? { sev: "ok", val: pad3(v.heading) + "°" } : { sev: null },
      storage: num(h.disk_usage) == null ? { sev: null } : { sev: h.disk_usage > 90 ? "warn" : h.disk_usage > 75 ? "caution" : "ok", val: h.disk_usage + "%" },
      cpu: num(h.cpu_load) == null ? { sev: null } : { sev: h.cpu_load > 85 ? "warn" : h.cpu_load > 65 ? "caution" : "ok", val: h.cpu_load + "%" },
      network: comm === "connected" ? { sev: "ok", val: "CONN" } : comm === "partitioned" ? { sev: "caution", val: "PART" } : comm === "disconnected" ? { sev: "warn", val: "DISC" } : { sev: "idle", val: "UNK" },
    };
  }
  function overallSev(s) {
    let worst = null;
    Object.values(s).forEach((x) => { if (x.sev && x.sev !== "idle") { if (worst == null || SEV_ORDER[x.sev] > SEV_ORDER[worst]) worst = x.sev; } });
    return worst; // null → no signal
  }

  // ---- matrix ----
  function mcell(s) {
    if (!s || s.sev == null) return `<td class="cell"><span class="mcell na"><span class="mdot"></span><span class="mv">—</span></span></td>`;
    return `<td class="cell"><span class="mcell ${s.sev}"><span class="mdot"></span><span class="mv">${s.val}</span></span></td>`;
  }
  function renderMatrix() {
    const head = `<tr><th class="veh">Vehicle</th>${MXCOLS.map(([, l]) => `<th>${l}</th>`).join("")}</tr>`;
    const body = fleet.map((v) => {
      const s = subsys(v);
      return `<tr data-id="${v.id}" class="${v.id === selId ? "sel" : ""}">
        <td class="vcell"><span class="vc-in"><span class="statdot" style="background:var(--${cls(v) === "c" ? "connected" : cls(v) === "p" ? "partitioned" : cls(v) === "d" ? "disconnected" : "unknown"})"></span><b>${v.name || "USV-" + v.id}</b>${CommsPill(v)}</span></td>
        ${MXCOLS.map(([k]) => mcell(s[k])).join("")}
      </tr>`;
    }).join("");
    const mx = document.getElementById("mxwrap");
    mx.innerHTML = `<table class="mx"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    mx.querySelectorAll("tbody tr").forEach((tr) => (tr.onclick = () => { selId = +tr.dataset.id; cmds = []; renderMatrix(); renderDetail(); renderControl(); refreshCommands(); }));
  }

  // ---- detail ----
  const bar = (pct, color) => `<span class="bar" style="width:64px;flex:none"><i style="width:${pct}%;background:${color}"></i></span>`;
  const row = (k, val, extra = "") => `<div class="mrow"><span class="k">${k}</span><span class="val ${extra}">${val}</span></div>`;
  const naRow = (k) => `<div class="mrow"><span class="k">${k}</span><span class="val na">${noTelem("no telem")}</span></div>`;
  function subCard(title, sev, rowsHtml, stale) {
    const head = sev == null ? "idle" : sev;
    const cond = sev == null ? "No telemetry" : sev === "ok" ? "Nominal" : sev === "warn" ? "Warning" : sev === "caution" ? "Caution" : "Idle";
    return `<div class="sub"><div class="sub-head ${head}"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${cond}</span></div><div class="metrics${stale ? " stale" : ""}">${rowsHtml}</div></div>`;
  }

  function renderDetail() {
    const v = fleet.find((x) => x.id === selId);
    const box = document.getElementById("detail");
    if (!v) { box.innerHTML = `<div class="empty-state" style="padding:8px 0">No vehicle selected</div>`; return; }
    document.getElementById("dettag").textContent = `${v.name || "USV-" + v.id} · subsystem breakdown`;
    const s = subsys(v), ov = overallSev(s), stale = commState(v) !== "connected";
    const h = v.health || {}, meas = v.measurements || {}, t = v.telemetry || {}, schema = (v.agent && v.agent.schema_version) || "—";

    // faults for overview
    const faults = MXCOLS.map(([k, l]) => ({ k, l, ...s[k] })).filter((x) => x.sev === "caution" || x.sev === "warn");
    const faultsHtml = faults.length
      ? faults.map((f) => `<div class="frow"><span class="fd" style="background:var(--${f.sev})"></span><span class="txt-${f.sev === "warn" ? "d" : "p"}">${f.l} — ${f.val}</span></div>`).join("")
      : `<div class="frow none">No active faults — all reporting subsystems nominal</div>`;

    const healthCard = `<div class="sub full"><div class="sub-head ${ov == null ? "idle" : ov}"><span class="hd"></span><span class="nm">Health overview</span><span class="cond">${ov == null ? "No signal" : ov === "ok" ? "Nominal" : ov === "warn" ? "Warning" : "Caution"}</span></div>
      <div class="faults">${faultsHtml}</div>
      <div class="metrics" style="border-top:1px solid var(--line)">
        ${row("Operator reachable", (v.communication && v.communication.operator_reachable != null) ? (v.communication.operator_reachable ? '<span class="txt-c">Yes</span>' : '<span class="txt-p">No</span>') : noTelem("no telem"), "keep")}
        ${row("Firmware", schema === "—" ? noTelem("no telem") : "v" + schema, "keep")}
        ${row("Services", h.flask_status ? "Flask " + h.flask_status : noTelem("no telem"), "keep")}
      </div></div>`;

    const battery = subCard("Battery", s.battery.sev,
      row("Charge", BatteryBar(v.battery), "keep") +
      row("State", v.battery == null ? noTelem("no telem") : (v.battery < 40 ? "Discharging · low" : "Discharging"), "keep") +
      naRow("Pack voltage") + naRow("Current draw"), false);

    const sensors = subCard("Sensors", s.sensors.sev,
      row("Leak sensor", h.leak_detected === true ? '<span class="txt-d">LEAK DETECTED</span>' : (h.leak_detected === false ? "No leak" : noTelem("no telem")), "keep") +
      row("Water quality", meas.water_quality ? "Streaming" : noTelem("no telem")) +
      row("Bathymetry", meas.bathymetry ? "Logging" : noTelem("no telem")), stale);

    const gps = subCard("GPS", s.gps.sev,
      row("Fix", v.lat != null && v.lng != null ? "3D" : "No fix") +
      row("Position", v.lat != null ? `${(+v.lat).toFixed(5)}, ${(+v.lng).toFixed(5)}` : noTelem("no telem")) +
      naRow("Satellites") + naRow("HDOP"), stale);

    const compass = subCard("Compass", s.compass.sev,
      row("Heading", v.heading != null ? pad3(v.heading) + "°" : noTelem("no telem")) +
      naRow("Declination") + naRow("Calibration"), stale);

    const storage = subCard("Storage", s.storage.sev,
      (h.disk_usage != null ? row("Disk usage", `${bar(h.disk_usage, "var(--connected)")}<span class="pcw">${h.disk_usage}%</span>`) : naRow("Disk usage")) +
      (h.disk_usage != null ? row("Free", `${100 - h.disk_usage}%`) : "") + naRow("Log size"), stale);

    const cpu = subCard("CPU", s.cpu.sev,
      (h.cpu_load != null ? row("Load", `${bar(h.cpu_load, h.cpu_load > 85 ? "var(--disconnected)" : h.cpu_load > 65 ? "var(--partitioned)" : "var(--connected)")}<span class="pcw">${h.cpu_load}%</span>`) : naRow("Load")) +
      naRow("Memory") + naRow("Uptime"), stale);

    const temps = `<div class="sub"><div class="sub-head idle"><span class="hd"></span><span class="nm">Temperatures</span><span class="cond">No telemetry</span></div><div class="metrics">${naRow("CPU temp") + naRow("Battery temp") + naRow("Water temp") + naRow("Motor temp")}</div></div>`;

    const c = v.communication || {};
    const network = subCard("Network", s.network.sev,
      row("Connectivity", `<span class="txt-${cls(v)}">${commState(v).toUpperCase()}</span>`, "keep") +
      row("Operator reachable", c.operator_reachable != null ? (c.operator_reachable ? '<span class="txt-c">Yes</span>' : '<span class="txt-p">No</span>') : noTelem("no telem"), "keep") +
      row("Buffered packets", c.buffered_packets != null ? String(c.buffered_packets) : noTelem("no telem"), "keep") +
      naRow("RSSI") + naRow("Latency"), false);

    const banner = stale
      ? `<div class="stale-note" style="margin:0 0 14px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>Telemetry as of ${fmtAge(v.last_seen_age_s)} ago — not live. Sensors, GPS, compass, storage &amp; CPU are last-known; battery and link state remain current.</div>`
      : "";

    box.innerHTML = `
      <div class="dhead">
        <span class="dname">${v.name || "USV-" + v.id}</span>
        ${CommsPill(v, { full: true })}
        <span class="ovr ${ov == null ? "ok" : ov}"><span class="hd"></span>${ov == null ? "No signal" : ov === "ok" ? "OK" : ov === "warn" ? "Warning" : "Caution"}</span>
        <span class="sp"></span>
        <span class="contact"><span class="lbl">Last contact</span><span class="big txt-${cls(v)}">${fmtAge(v.last_seen_age_s)}</span></span>
      </div>
      ${banner}
      ${healthCard}
      <div class="subgrid">${battery}${sensors}${gps}${compass}${storage}${cpu}${temps}${network}</div>`;
  }

  // ---- Command & Control (authority + command queue for the selected vehicle) ----
  const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
  const fmtClock = (iso) => { if (!iso) return "—"; const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false }); };

  function renderControl() {
    const box = document.getElementById("control");
    if (!box) return;
    const v = fleet.find((x) => x.id === selId);
    const tag = document.getElementById("ctltag");
    if (!v) { box.innerHTML = `<div class="empty-state" style="padding:8px 0">No vehicle selected</div>`; if (tag) tag.textContent = ""; return; }
    const vname = v.name || "USV-" + v.id;
    if (tag) tag.textContent = `${vname} · authority & command queue`;

    // Authority is INDEPENDENT of comm-state. Default/unknown → OPERATOR (observe-only).
    const authority = String(v.authority || "OPERATOR").toUpperCase();
    const engaged = authority === "LOCAL_AGENT";

    const authBadge = engaged
      ? `<span class="auth-badge on"><i></i>CONTROL ENGAGED</span>`
      : `<span class="auth-badge"><i></i>OBSERVE ONLY</span>`;
    const authNote = engaged
      ? `Operator commands are <b>enabled</b> for ${vname}. Authority is independent of link state — releasing returns to observe-only without changing the vehicle's mode.`
      : `Safe default — command buttons are disabled. Press <b>Engage Control</b> to permit commands to execute. Engaging does not arm the vehicle or change its mode.`;
    const authBtn = engaged
      ? `<button class="ctl-auth release" data-auth="OPERATOR">Release Control</button>`
      : `<button class="ctl-auth engage" data-auth="LOCAL_AGENT">Engage Control</button>`;

    const btns = CMDS.map(([type, label]) => {
      const hr = HIGH_RISK.has(type);
      return `<button class="ctl-cmd${hr ? " hr" : ""}" data-cmd="${type}"${engaged ? "" : " disabled"} title="${type}${hr ? " · confirmation required" : ""}">${label}</button>`;
    }).join("");

    const queue = cmds.length
      ? cmds.slice(0, 8).map((c) => {
          const clsx = CMD_STATUS_CLS[c.status] || "u";
          const when = c.completed_at || c.claimed_at || c.created_at;
          const note = c.reason || c.warning || "";
          return `<div class="ctl-row"><span class="ctl-type mono">${c.type}</span><span class="pill ${clsx}">${c.status}</span><span class="ctl-when mono">${fmtClock(when)}</span>${note ? `<span class="ctl-note" title="${note.replace(/"/g, "&quot;")}">${note}</span>` : ""}</div>`;
        }).join("")
      : `<div class="ctl-empty">No commands issued for ${vname} yet.</div>`;

    box.innerHTML = `
      <div class="ctl-panel">
        <div class="ctl-auth-bar${engaged ? " engaged" : ""}">
          <div class="ctl-auth-l"><span class="lbl">Control authority</span>${authBadge}</div>
          <div class="ctl-auth-note">${authNote}</div>
          ${authBtn}
        </div>
        <div class="ctl-cmds${engaged ? "" : " locked"}">${btns}</div>
        ${engaged ? "" : `<div class="ctl-lock-note">${lockSvg}<span>Commands are locked. Engage control to enable them.</span></div>`}
        <div class="ctl-queue">
          <div class="ctl-queue-h"><span class="lbl">Command queue &amp; history</span><span class="tag">status is reported by the vehicle — never assumed</span></div>
          ${queue}
        </div>
      </div>`;

    const ab = box.querySelector(".ctl-auth");
    if (ab) ab.onclick = () => toggleAuthority(ab.dataset.auth, vname);
    box.querySelectorAll(".ctl-cmd").forEach((b) => (b.onclick = () => sendCommand(b.dataset.cmd, vname)));
  }

  async function toggleAuthority(target, vname) {
    if (target === "LOCAL_AGENT" &&
        !window.confirm(`Engage control of ${vname}?\n\nThis grants permission for operator commands to execute. It does NOT arm the vehicle or change its mode.`)) return;
    const res = await api.setAuthority(selId, target);
    if (!res.ok) window.alert((res.data && res.data.message) || "Authority change failed.");
    await refreshAll();
  }

  async function sendCommand(type, vname) {
    const label = (CMDS.find(([t]) => t === type) || [null, type])[1];
    const highRisk = HIGH_RISK.has(type);
    if (highRisk &&
        !window.confirm(`Confirm ${label} for ${vname}?\n\nThe command is queued for the vehicle to execute. It is NOT applied until the local agent reports back.`)) return;
    let res = await api.createCommand({ vehicle_id: selId, type, confirm: highRisk });
    if (!res.ok && res.data && res.data.needs_confirmation) {
      if (!window.confirm(`${res.data.message}\n\nQueue anyway?`)) return;
      res = await api.createCommand({ vehicle_id: selId, type, confirm: true });
    }
    if (!res.ok) window.alert((res.data && res.data.message) || "Command was not accepted.");
    await refreshCommands();
  }

  async function refreshCommands() {
    if (selId == null) return;
    try { const d = await api.getCommands(selId); cmds = (d && d.commands) || []; }
    catch (e) { cmds = []; }
    renderControl();
  }

  async function refreshAll() {
    try { onFleet(await api.getFleet()); } catch (e) { /* keep last */ }
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const st = commState(v); if (st === "connected") c.c++; else if (st === "partitioned") c.p++; else if (st === "disconnected") c.d++; });
    return c;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) selId = fleet[0].id;
    document.getElementById("vcount").textContent = `${fleet.length} vehicles`;
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { selId = +el.dataset.id; cmds = []; onFleet(fleet); }));
    renderMatrix(); renderDetail(); renderControl();
    updateRibbon({ counts: counts() });
    refreshCommands();
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); };
}
