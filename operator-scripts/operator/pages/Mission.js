// Mission.js — full mission management page: per-vehicle Overview / Replay /
// Statistics / Export. Mission execution is inherently per-Pixhawk in this system
// (no fleet-wide named-mission object exists — BACKEND_ROADMAP.md P1 gap), so the
// page follows the same roster-dock + detail-pane pattern as Map.js and Vehicle.js:
// pick a vehicle on the left, see everything about ITS mission on the right.
//
// Every number here comes from data already live elsewhere in the app — reused
// through lib/mission.js so this page can never disagree with Map.js's Pixhawk
// Mission card about the same vehicle's waypoint counts:
//   • Pixhawk mission readback (api.getPixhawkMission) — waypoints, current_seq, hash.
//   • Command queue (api.getCommands) — 100% operator-issued (the Local Agent only
//     claims/reports results, never creates a record), so its length IS "operator
//     interventions", and per-type counts ARE real RTL/LOITER command counts.
//   • Event log (api.getEventLog) — main.py already emits first-class typed
//     transitions (type: "comms" | "agent" | "mission") on real change, so
//     "Communication interruptions" and "Agent transitions" are real counts, not
//     estimates.
// Replay and the exact wording "Mission report (PDF)" are honest about what's real:
// Replay is a designed placeholder (no position-history backend exists yet); CSV/
// JSON/print-to-PDF export are REAL because the data is already on the page and a
// client-side download needs no backend.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, noTelem } from "../lib/ui.js";
import { homeStatus, fmtDistance } from "../lib/home.js";
import { classifyMissionWaypoints, missionCounts, remainingRouteDistanceM, etaSeconds, fmtDuration } from "../lib/mission.js";

const TABS = [["overview", "Overview"], ["replay", "Replay"], ["statistics", "Statistics"], ["export", "Export"]];
const infoIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v0.01M11 12h1v4h1"/></svg>';

export function Mission(root) {
  let fleet = [], selId = null, tab = "overview";
  // Per-vehicle Pixhawk mission cache — same shape/contract as Map.js's `pxm`
  // ({ mission, fetchedAt, loading, note }), independently held here because pages
  // are independently mounted (no cross-page store in this app — see api.js).
  const pxm = {};
  let cmds = [];          // command queue/history for the selected vehicle
  let commsHist = null;   // comms-state transition log for the selected vehicle
  let events = [];        // full event log, filtered client-side per vehicle below
  let exportNote = null;  // transient export status (e.g. "pop-up blocked")

  root.className = "app dock-main";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("mission") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Mission</span></div>
       <div class="veh-list" id="veh-list"><div class="empty-state" style="padding:10px 12px">Connecting…</div></div>
       <div class="dock-foot">${infoIcon}<span>Pick a vehicle to inspect its own Pixhawk mission — there is no fleet-wide mission object in this system yet.</span></div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Mission</h1><span class="count mono" id="mtitle">—</span></div>
       <div class="mtabs" id="mtabs">${TABS.map(([k, l]) => `<button class="mtab${k === "overview" ? " on" : ""}" data-tab="${k}">${l}</button>`).join("")}</div>
       <div class="mission-body" id="mbody"></div>
     </div>`;

  document.querySelectorAll("#mtabs .mtab").forEach((b) => (b.onclick = () => {
    tab = b.dataset.tab;
    document.querySelectorAll("#mtabs .mtab").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
    renderBody();
  }));

  // ---- Pixhawk mission fetch (same contract as Map.js fetchPixhawkMission) --------
  function pxmState(id) { return pxm[id] || (pxm[id] = { mission: null, fetchedAt: 0, loading: false, note: null }); }
  async function fetchPixhawkMission(id) {
    if (id == null) return;
    const s = pxmState(id);
    s.loading = true; if (id === selId) renderBody();
    try {
      const res = await api.getPixhawkMission(id);
      if (res && res.reachable) {
        s.mission = res; s.fetchedAt = Date.now();
        s.note = res.available === false ? "no-api" : (res.partial ? "partial" : null);
      } else {
        s.note = (res && res.available === false) ? "no-api" : "unreachable";
      }
    } catch (e) {
      s.note = "error";
    } finally {
      s.loading = false;
      if (id === selId) renderBody();
    }
  }

  function loadCommands(id) {
    if (id == null) { cmds = []; return; }
    api.getCommands(id).then((d) => { if (id === selId) { cmds = (d && d.commands) || []; renderBody(); } })
      .catch(() => { if (id === selId) { cmds = []; renderBody(); } });
  }
  function loadCommsHistory(id) {
    if (id == null) { commsHist = null; return; }
    api.getCommsHistory(id).then((h) => { if (id === selId) { commsHist = h; renderBody(); } }).catch(() => {});
  }

  function selectVehicle(id) {
    if (id === selId) return;
    selId = id;
    cmds = []; commsHist = null;
    loadCommands(id); loadCommsHistory(id);
    // Auto-fetch once per vehicle per page visit (this page's whole purpose is
    // showing the mission) — but never re-fetch on every fleet poll; Refresh stays a
    // deliberate operator action, same caution as Map.js's live Scout proxy.
    if (!pxm[id] || !pxm[id].fetchedAt) fetchPixhawkMission(id);
    renderDock(); renderBody();
  }

  // ---- shared mission math (identical contract to Map.js selectedMissionStats) ----
  function currentVehicle() { return fleet.find((v) => v.id === selId) || null; }
  function missionFor(id) {
    const s = pxm[id];
    if (!s || !s.mission) return null;
    const { home, route } = classifyMissionWaypoints(s.mission.waypoints || []);
    const cur = s.mission.current_seq;
    return { s, home, route, cur, counts: missionCounts(route, cur) };
  }
  function statsOf(v) {
    const m = missionFor(v.id);
    if (!m || !m.route.length) return null;
    const remDistM = remainingRouteDistanceM(m.route, m.cur, v.lat, v.lng);
    const etaS = etaSeconds(remDistM, v.speed);
    return { ...m, remDistM, etaS };
  }

  function curWaypointText(m) {
    if (!m || m.cur == null) return "—";
    if (m.home && m.home.seq === m.cur) return `Mission start (seq ${m.cur})`;
    return `WP ${m.cur} / ${m.route.length}`;
  }

  // ---- vehicle-scoped events (main.py already emits typed transitions on real
  // change — see the file header) --------------------------------------------------
  function vehicleEvents(id) { return events.filter((e) => e.vehicleId === id); }
  function commsInterruptions(id) {
    // "Interruption" = a transition INTO a degraded state (lost/partitioned), never
    // the "restored"/"first contact" half of the same axis — counted from severity,
    // which main.py already assigns deterministically per transition.
    return vehicleEvents(id).filter((e) => e.type === "comms" && (e.severity === "warning" || e.severity === "caution")).length;
  }
  function agentTransitions(id) { return vehicleEvents(id).filter((e) => e.type === "agent").length; }
  function firstMissionEventTs(id) {
    const ev = vehicleEvents(id).filter((e) => e.type === "mission").sort((a, b) => new Date(a.ts) - new Date(b.ts))[0];
    return ev ? new Date(ev.ts).getTime() : null;
  }

  function overviewFields(v) {
    const m = missionFor(v.id), st = statsOf(v);
    const c = m ? m.counts : { total: null, completed: null, remaining: null, pct: null };
    const hash = m && m.s.mission && m.s.mission.hash ? String(m.s.mission.hash).slice(0, 12) : null;
    const loaded = c.total != null && c.total > 0;
    return {
      "Mission name": noTelem("no mission registry"),
      "Mission ID": hash || "—",
      "Mission loaded": loaded ? "Yes" : (m ? "No" : noTelem("not fetched")),
      "Current waypoint": curWaypointText(m),
      "Total waypoints": c.total == null ? "—" : String(c.total),
      "Completed waypoints": c.completed == null ? "—" : String(c.completed),
      "Remaining waypoints": c.remaining == null ? "—" : String(c.remaining),
      "Mission progress": c.pct == null ? "—" : `${c.pct}%`,
      "Distance travelled": noTelem("no position-history log"),
      "Estimated remaining distance": st && st.remDistM != null ? fmtDistance(st.remDistM) : "—",
      "Estimated remaining time": st && st.etaS != null ? fmtDuration(st.etaS) : noTelem(v.speed == null ? "no speed telemetry" : "not moving"),
      "Current mission state": (v.mission_data && v.mission_data.mission_state) || v.status || "—",
    };
  }

  function statsFields(v) {
    const state = String((v.mission_data && v.mission_data.mission_state) || v.status || "").toLowerCase();
    const active = v.mission_data && v.mission_data.mission_active;
    const completed = /complete|done|finished/.test(state) ? "Yes" : active ? "In progress" : "Unknown";
    const firstMs = firstMissionEventTs(v.id);
    // Total disconnected time, straight from the operator-side comms-state transition
    // log (GET /api/comms/history/{id}) — the exact same figure Map.js's comms
    // timeline footer shows, reused here rather than a second computation.
    const disc = commsHist && commsHist.durations_s && commsHist.durations_s.DISCONNECTED;
    return {
      "Mission duration (observed this session)": firstMs != null ? fmtDuration((Date.now() - firstMs) / 1000) : noTelem("no mission-state change observed yet"),
      "Operator interventions": String(cmds.length),
      "Communication interruptions": String(commsInterruptions(v.id)),
      "Total disconnected time": disc != null ? fmtDuration(disc) : noTelem("no disconnection recorded"),
      "Agent transitions": String(agentTransitions(v.id)),
      "RTL commands issued": String(cmds.filter((c) => c.type === "RTL").length),
      "LOITER commands issued": String(cmds.filter((c) => c.type === "SET_MODE_LOITER").length),
      "Mission completed successfully": completed,
    };
  }

  // ---- Overview tab ----------------------------------------------------------
  function renderOverview(v) {
    const st = statsOf(v);
    const pct = st && st.counts.pct != null ? st.counts.pct : null;
    const fields = overviewFields(v);
    const headline = `
      <div class="rollup">
        <div class="rtile"><span class="lbl">Mission progress</span><span class="v">${pct == null ? "—" : pct + "%"}</span><span class="sub">${st && st.counts.completed != null ? `${st.counts.completed} / ${st.counts.total} waypoints` : "no mission loaded"}</span></div>
        <div class="rtile"><span class="lbl">Current waypoint</span><span class="v">${curWaypointText(st)}</span><span class="sub">Scout's own current_seq</span></div>
        <div class="rtile"><span class="lbl">Remaining distance</span><span class="v">${st && st.remDistM != null ? fmtDistance(st.remDistM) : "—"}</span><span class="sub">computed from real coordinates</span></div>
        <div class="rtile"><span class="lbl">Remaining time (est.)</span><span class="v">${st && st.etaS != null ? fmtDuration(st.etaS) : "—"}</span><span class="sub">${v.speed == null ? "needs live speed" : `at ${v.speed} m/s`}</span></div>
      </div>
      ${pct != null ? `<div class="mo-bar"><div class="mo-bar-fill" style="width:${pct}%"></div></div>` : ""}`;
    const rows = Object.entries(fields).map(([k, val]) => `<div class="cfg-ro"><div class="cfg-ro-l"><span class="fld-lbl">${k}</span></div><div class="cfg-ro-v">${val}</div></div>`).join("");
    const homeRow = (() => {
      const hs = homeStatus(v, {});
      const cls_ = hs.state === "verified" ? "ok" : hs.state === "pending" ? "pending" : hs.state === "unknown" ? "dim" : "warn";
      const txt = hs.state === "verified" ? "Verified" : hs.state === "pending" ? "Setting…" : hs.state === "unknown" ? "Unknown" : "Not verified";
      return `<div class="cfg-ro"><div class="cfg-ro-l"><span class="fld-lbl">Home verification</span><span class="fld-hint">RTL recovery point — set/verify on the Map page</span></div><div class="cfg-ro-v"><span class="pxm-chip ${cls_}">${txt}</span></div></div>`;
    })();
    const pxmS = pxm[v.id];
    const fetchNote = pxmS && pxmS.note
      ? `<div class="pxm-note warn">${{ "no-api": "Scout exposes no mission API.", unreachable: "Scout unavailable — showing last downloaded mission.", error: "Fetch failed — showing last downloaded mission.", partial: "Partial download — mission may be incomplete." }[pxmS.note] || pxmS.note}</div>`
      : "";
    document.getElementById("mbody").innerHTML = `
      <div class="msect"><span class="lbl">Mission overview</span><span class="tag">${v.name || "USV-" + v.id} · from the Pixhawk mission readback</span>
        <button class="diag-btn" id="mo-refresh" style="margin-left:auto" ${pxmS && pxmS.loading ? "disabled" : ""}>${pxmS && pxmS.loading ? "Fetching…" : "Refresh"}</button>
      </div>
      <div style="padding:0 20px 8px">${headline}${fetchNote}</div>
      <div class="msect"><span class="lbl">Mission detail</span></div>
      <div style="padding:0 20px 20px"><div class="cfg-ro-grid">${rows}${homeRow}</div></div>
      <div class="ev-note">${infoIcon}"Estimated remaining distance" is real geometry over the Pixhawk mission's own coordinates and the vehicle's current position — never fabricated. "Estimated remaining time" only appears while the vehicle reports a real, non-zero speed. "Distance travelled" and a named "Mission name" need backend support that doesn't exist yet (no position-history log, no mission registry — see BACKEND_ROADMAP.md) and are shown as honest gaps, not invented.</div>`;
    const refreshBtn = document.getElementById("mo-refresh");
    if (refreshBtn) refreshBtn.onclick = () => fetchPixhawkMission(v.id);
  }

  // ---- Replay tab (designed placeholder — see file header) -------------------
  function renderReplay(v) {
    const ticks = Array.from({ length: 9 }, (_, i) => `<div class="mr-tick" style="left:${(i / 8) * 100}%"></div>`).join("");
    document.getElementById("mbody").innerHTML = `
      <div class="msect"><span class="lbl">Mission replay</span><span class="tag">${v.name || "USV-" + v.id} · designed, not yet backed by data</span></div>
      <div style="padding:0 20px 20px">
        <div class="mreplay">
          <div class="mr-transport">
            <button class="mr-btn" disabled title="Requires a position/state history backend">⏮</button>
            <button class="mr-btn primary" disabled title="Requires a position/state history backend">▶ Play</button>
            <button class="mr-btn" disabled title="Requires a position/state history backend">⏭</button>
            <span class="mr-speed">Speed <select disabled><option>1×</option><option>4×</option><option>16×</option></select></span>
            <span class="mr-clock mono">--:--:-- → --:--:--</span>
          </div>
          <div class="mr-track">${ticks}<div class="mr-scrub" style="left:0%"></div></div>
          <div class="mr-legend">
            <span>Comms state</span><span>Vehicle mode</span><span>Control authority</span><span>Waypoint reached</span>
          </div>
        </div>
        <div class="cfg-note" style="margin-top:14px">${infoIcon}
          <span>Replay needs a position/state history the operator backend does not store yet — the fleet payload, comms-state log and command queue are all "current state only" (see SYSTEM_INFORMATION_MODEL.md). <b>Future architecture:</b> a time-series store keyed by vehicle + timestamp (position, mode, comm_state, authority, active command), exposed as <code>GET /api/missions/{id}/replay?from=&amp;to=</code>, would let this scrubber play back a real mission using the exact same comms-state and event-log data already collected today — nothing here would need to change shape, only gain a backing store.</span>
        </div>
      </div>`;
  }

  // ---- Statistics tab ----------------------------------------------------------
  function renderStatistics(v) {
    const fields = statsFields(v);
    const rows = Object.entries(fields).map(([k, val]) => `<div class="mrow"><span class="k">${k}</span><span class="val">${val}</span></div>`).join("");
    document.getElementById("mbody").innerHTML = `
      <div class="msect"><span class="lbl">Mission statistics</span><span class="tag">${v.name || "USV-" + v.id} · derived from the real event &amp; command logs</span></div>
      <div style="padding:0 20px 20px">
        <div class="sub full"><div class="sub-head idle"><span class="hd"></span><span class="nm">This session</span><span class="cond">${cmds.length + vehicleEvents(v.id).length} records</span></div>
          <div class="metrics">${rows}</div>
        </div>
        <div class="ev-note" style="margin-top:0">${infoIcon}"Operator interventions" counts every command ever queued for this vehicle — the Local Agent only claims and reports on commands, it never creates one, so this count is exclusively operator-issued. "RTL/LOITER commands issued" counts operator requests, not autonomous mode entries (the backend has no mode-transition log to detect those). "Mission completed successfully" is inferred from the reported mission-state text, not a dedicated completion field.</div>
      </div>`;
  }

  // ---- Export tab ----------------------------------------------------------
  function csvEscape(s) {
    s = String(s ?? "");
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  }
  function downloadBlob(filename, content, mime) {
    const blob = new Blob([content], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }
  function exportPayload(v) {
    return {
      vehicle: { id: v.id, name: v.name || `USV-${v.id}` },
      generated_at: new Date().toISOString(),
      overview: overviewFields(v),
      statistics: statsFields(v),
      commands: cmds,
      events: vehicleEvents(v.id),
    };
  }
  function doExportJson(v) {
    downloadBlob(`mission-${v.id}-${Date.now()}.json`, JSON.stringify(exportPayload(v), null, 2), "application/json");
  }
  function doExportCsv(v) {
    const p = exportPayload(v);
    const rows = ["section,key,value"];
    Object.entries(p.overview).forEach(([k, val]) => rows.push(["overview", csvEscape(k), csvEscape(val)].join(",")));
    Object.entries(p.statistics).forEach(([k, val]) => rows.push(["statistics", csvEscape(k), csvEscape(val)].join(",")));
    rows.push(""); rows.push("commands"); rows.push(["id", "type", "status", "created_at", "completed_at", "reason"].join(","));
    p.commands.forEach((c) => rows.push([c.id, c.type, c.status, c.created_at || "", c.completed_at || "", csvEscape(c.reason || c.warning || "")].join(",")));
    rows.push(""); rows.push("events"); rows.push(["id", "ts", "type", "severity", "message"].join(","));
    p.events.forEach((e) => rows.push([e.id, e.ts || "", e.type || "", e.severity || "", csvEscape(e.message || "")].join(",")));
    downloadBlob(`mission-${v.id}-${Date.now()}.csv`, rows.join("\n"), "text/csv");
  }
  function doExportPdf(v) {
    const p = exportPayload(v);
    const win = window.open("", "_blank", "width=820,height=1040");
    if (!win) { exportNote = "Pop-up blocked — allow pop-ups for this page to generate the report."; renderBody(); return; }
    exportNote = null;
    const table = (obj) => `<table>${Object.entries(obj).map(([k, val]) => `<tr><td>${k}</td><td>${val}</td></tr>`).join("")}</table>`;
    win.document.write(`<!doctype html><html><head><title>Mission report — ${p.vehicle.name}</title><meta charset="utf-8">
      <style>
        body{font-family:-apple-system,Segoe UI,Arial,sans-serif;color:#111;padding:28px;max-width:720px;margin:0 auto}
        h1{font-size:19px;margin:0 0 2px} .meta{color:#666;font-size:12px;margin-bottom:20px}
        h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#555;margin:22px 0 6px;border-bottom:1px solid #ddd;padding-bottom:4px}
        table{width:100%;border-collapse:collapse;font-size:12.5px} td{padding:5px 8px;border-bottom:1px solid #eee}
        td:first-child{color:#555;width:230px} @media print{body{padding:0}}
      </style></head><body>
      <h1>Mission report — ${p.vehicle.name}</h1>
      <div class="meta">Generated ${new Date(p.generated_at).toLocaleString()} · USV-Agentic Operator Station</div>
      <h2>Overview</h2>${table(p.overview)}
      <h2>Statistics</h2>${table(p.statistics)}
      </body></html>`);
    win.document.close();
    setTimeout(() => { try { win.print(); } catch (e) { /* noop */ } }, 300);
  }

  function exportCard(title, desc, available) {
    return `<div class="sub full">
      <div class="sub-head ${available ? "ok" : "idle"}"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${available ? "Available" : "Not yet available"}</span></div>
      <div style="padding:12px 13px;display:flex;align-items:center;gap:14px">
        <p style="margin:0;flex:1;font-size:12px;color:var(--muted);line-height:1.5">${desc}</p>
        <button class="cfg-btn" ${available ? "" : "disabled"} data-export="${title}">${available ? "Download" : "Not available"}</button>
      </div></div>`;
  }
  function renderExport(v) {
    document.getElementById("mbody").innerHTML = `
      <div class="msect"><span class="lbl">Export</span><span class="tag">${v.name || "USV-" + v.id} · exports exactly what this page shows — nothing invented</span></div>
      <div style="padding:0 20px 20px">
        ${exportCard("CSV", "Overview, statistics, the full command queue and the vehicle's event log as one CSV file.", true)}
        ${exportCard("JSON", "The same data as structured JSON — for feeding into external analysis or the thesis writeup.", true)}
        ${exportCard("Mission report (PDF)", "Opens a formatted, print-ready report in a new tab — use your browser's Print → Save as PDF. No PDF library is involved.", true)}
        ${exportNote ? `<div class="cfg-note" style="border-color:var(--caution);color:var(--caution)">${infoIcon}${exportNote}</div>` : ""}
      </div>`;
    document.querySelectorAll("[data-export]").forEach((btn) => {
      btn.onclick = () => {
        const kind = btn.dataset.export;
        if (kind === "CSV") doExportCsv(v);
        else if (kind === "JSON") doExportJson(v);
        else doExportPdf(v);
      };
    });
  }

  function renderBody() {
    const v = currentVehicle();
    document.getElementById("mtitle").textContent = v ? `${v.name || "USV-" + v.id} · ${commState(v)}` : "No vehicle selected";
    if (!v) { document.getElementById("mbody").innerHTML = `<div class="empty-state" style="padding:20px">No vehicle selected</div>`; return; }
    if (tab === "overview") renderOverview(v);
    else if (tab === "replay") renderReplay(v);
    else if (tab === "statistics") renderStatistics(v);
    else renderExport(v);
  }

  function renderDock() {
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => selectVehicle(+el.dataset.id)));
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) selectVehicle((fleet.find((v) => v.online) || fleet.find((v) => v.lat != null) || fleet[0]).id);
    renderDock();
    if (selId != null) renderBody();
    updateRibbon({ counts: counts() });
  }
  function onEvents(data) { events = Array.isArray(data) ? data : []; if (tab === "statistics" || tab === "export") renderBody(); }

  // Operator Link — shared with Map.js via the SAME api.js poll key ("fleet"), so the
  // Ribbon's backend-reachability indicator reads identically no matter which page the
  // operator is on. See the Map.js operational review (C1) for why this exists.
  function updateFeedIndicator() {
    const h = api.getFeedHealth("fleet");
    if (!h || (h.lastOkAt == null && h.lastErrAt == null)) { updateRibbon({ feed: { cls: "dim", label: "CONNECTING…" } }); return; }
    if (h.lastOkAt == null) { updateRibbon({ feed: { cls: "bad", label: "BACKEND UNREACHABLE" } }); return; }
    const ageS = (Date.now() - h.lastOkAt) / 1000;
    if (ageS <= 4) updateRibbon({ feed: { cls: "ok", label: "LIVE" } });
    else if (ageS <= 12) updateRibbon({ feed: { cls: "warn", label: `DELAYED ${Math.round(ageS)}s` } });
    else updateRibbon({ feed: { cls: "bad", label: `UNREACHABLE ${Math.round(ageS)}s` } });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, updateFeedIndicator, "fleet");
  const stopEvents = api.poll(api.getEventLog, 5000, onEvents, () => {});
  const commsId = setInterval(() => loadCommsHistory(selId), 3000);
  const commandsId = setInterval(() => loadCommands(selId), 3000);
  const clockId = setInterval(() => { updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }); updateFeedIndicator(); }, 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
  updateFeedIndicator();

  return function cleanup() { stopFleet(); stopEvents(); clearInterval(commsId); clearInterval(commandsId); clearInterval(clockId); };
}
