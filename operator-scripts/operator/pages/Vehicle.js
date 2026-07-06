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

export function Vehicle(root) {
  let fleet = [], selId = null;

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
    mx.querySelectorAll("tbody tr").forEach((tr) => (tr.onclick = () => { selId = +tr.dataset.id; renderMatrix(); renderDetail(); }));
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
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { selId = +el.dataset.id; onFleet(fleet); }));
    renderMatrix(); renderDetail();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); };
}
