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
import { parseMission, missionUploadParams, missionUploadStage, missionUploadCompare, missionClearOutcome, missionOperationState, missionEvidence, missionErrorText, missionErrorOf, MISSION_TOO_LARGE, UPLOAD_STAGES, READBACK_PENDING, READBACK_AVAILABLE, READBACK_UNAVAILABLE } from "../lib/mission-upload.js";
import { hasPendingOfType } from "../lib/command.js";
import { canonicalVehicleId } from "../lib/selection.js";

const TABS = [["overview", "Overview"], ["upload", "Upload"], ["replay", "Replay"], ["statistics", "Statistics"], ["export", "Export"]];
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
  // Mission-upload workflow state (per selected vehicle; reset on switch). `text` is the
  // pasted mission, `parsed` its validated preview, `cmdId` the tracked MISSION_UPLOAD/
  // MISSION_CLEAR command (its lifecycle is read from `cmds`), `readbackAt` guards a
  // single post-verified re-fetch of the Pixhawk mission for the expected-vs-observed compare.
  // `readback` is the OPERATOR'S OWN post-operation observation, held separately from the
  // shared `pxm` cache on purpose: pxm holds "the mission as of the last time anyone
  // refreshed", which may predate the operation entirely. Evidence about an upload has to
  // be a fetch made AFTER that upload, and `status` records whether we actually got one
  // ("unavailable" is a real, reportable outcome, not an empty mission).
  let upload = { text: "", parsed: null, expected: null, cmdId: null, at: 0, error: null,
                 readback: { status: null, at: 0, mission: null, error: null } };
  let showTechnical = false;   // bench-test / thesis-evidence panel (off by default)
  let authority = null;   // confirmed control authority for the selected vehicle (Scout proxy)
  // Backend-declared command capabilities ({TYPE: {supported, reason}}). Fetched once per
  // page mount: which commands are deliverable is a backend/Scout-contract fact, not a
  // per-vehicle one, and the UI must never enable a button the backend would refuse.
  let capabilities = null;

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

  function loadAuthority(id) {
    if (id == null) return;
    api.getControlAuthority(id).then((a) => { if (id === selId) { authority = a; if (tab === "upload") renderBody(); } }).catch(() => {});
  }

  function selectVehicle(id) {
    if (id === selId) return;
    selId = id;
    cmds = []; commsHist = null;
    upload = { text: "", parsed: null, expected: null, cmdId: null, at: 0, error: null,
               readback: { status: null, at: 0, mission: null, error: null } };
    authority = null; loadAuthority(id);
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

  // ---- Upload tab (mission upload / clear → Pixhawk, read-back verified) ----------
  // A validated mission (GeoJSON or canonical waypoint JSON) is queued as MISSION_UPLOAD
  // via the SAME command pipeline as every other command (api.uploadMission → the queue).
  // Success is NEVER "the file reached Scout": the lifecycle is tracked Requested →
  // Accepted → Executing → Verified/Failed (missionUploadStage), and only after Scout
  // reports it verified do we re-fetch the Pixhawk mission and compare expected vs observed
  // count/hash (missionUploadCompare). Duplicate presses are suppressed while an upload is
  // active. No waypoint jumping is offered. Mission clear gets the same confirm + read-back.
  function escapeHtml(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  /** escapeHtml + quotes — REQUIRED for anything interpolated into a quoted HTML
   *  attribute. escapeHtml alone leaves `"` intact, which would terminate the attribute
   *  early and inject raw markup; backend-supplied strings (e.g. a capability reason
   *  echoing a Scout error) are not guaranteed quote-free. */
  function escapeAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }
  function trackedUpload() { return upload.cmdId ? (cmds.find((c) => c.id === upload.cmdId) || null) : null; }
  function hasControl() { return !!(authority && authority.authority === "OPERATOR"); }
  function uploadActive() { return hasPendingOfType(cmds, "MISSION_UPLOAD") || hasPendingOfType(cmds, "MISSION_CLEAR"); }
  /** Backend capability for a command type. Unknown (capabilities not fetched yet) is
   *  treated as SUPPORTED so a slow fetch does not grey out a working button; the backend
   *  refuses an unsupported command anyway, so the worst case is an honest error, never a
   *  silently unverifiable write. */
  function capabilityOf(type) {
    const c = capabilities && capabilities.commands && capabilities.commands[type];
    return c ? { supported: c.supported !== false, reason: c.reason || null }
             : { supported: true, reason: null };
  }
  /** The backend's maximum route waypoints per upload, READ from the capabilities endpoint
   *  — never a second constant declared here. A local copy would be exactly the drift that
   *  lets the preview accept a route the upload refuses. Null until capabilities land. */
  function maxRouteWaypoints() {
    const n = capabilities && capabilities.max_route_waypoints;
    return typeof n === "number" && Number.isFinite(n) ? n : null;
  }
  /** Scout's live background-upload state for the selected vehicle, or null. */
  function liveUpload(v) { return (v && v.mission_upload) || null; }

  function parseUploadNow() {
    upload.parsed = upload.text.trim() ? parseMission(upload.text) : null;
    upload.expected = null;
    upload.error = null;
    renderBody();
    // Local parsing gives the counts immediately; the expected route content hash can only
    // come from the backend, which is the single authoritative calculator (there is no
    // JavaScript hash implementation, by design). Fetched second so a slow or failed
    // preview never blocks the operator from seeing their route's errors and counts.
    if (upload.parsed && upload.parsed.ok) fetchExpected(upload.parsed);
  }
  /** Ask the backend to canonicalize the parsed route so the preview can show the exact
   *  hash that will be verified. Nothing is queued. A failure leaves `expected` null and
   *  the preview says the hash is unavailable — never a fabricated or locally computed
   *  stand-in, which is exactly the defect the removed wpm1 hash represented. */
  async function fetchExpected(p) {
    const res = await api.previewMission(missionUploadParams(p));
    if (upload.parsed !== p) return;   // operator edited the route while we were waiting
    upload.expected = res.ok && res.data && res.data.params ? res.data.params : null;
    renderBody();
  }
  /** First 12 hex characters of a `sha256:…` digest, for a dense readable preview. The
   *  FULL value is always available in the element's title — abbreviating in the title too
   *  would leave the operator no way to compare it against Scout's. */
  function shortHash(h) {
    if (!h) return null;
    const hex = String(h).replace(/^sha256:/, "");
    return "sha256:" + hex.slice(0, 12) + "…";
  }
  async function doUpload(v) {
    const p = upload.parsed;
    if (!p || !p.ok || uploadActive() || !hasControl()) return;
    if (!window.confirm(
      `Upload ${p.routeCount} route waypoints to ${v.name || "USV-" + v.id}'s Pixhawk?\n\n` +
      `The Pixhawk will hold ${p.pixhawkItemCount} items after upload (${p.routeCount} route waypoints + Home at seq 0, which Scout owns and adds).\n\n` +
      `This OVERWRITES the mission currently stored on the flight controller. It is confirmed ONLY by a read-back after upload — never by the file reaching Scout.`)) return;
    const res = await api.uploadMission(v.id, missionUploadParams(p));
    if (res.ok && res.data && res.data.command) {
      upload.cmdId = res.data.command.id; upload.at = Date.now(); upload.error = null;
      upload.readback = { status: null, at: 0, mission: null, error: null };
    } else {
      // The backend lists every mission-contract violation it found — show them all, so a
      // rejected file is fixable in one pass instead of one error at a time.
      const d = res.data || {};
      upload.error = Array.isArray(d.errors) && d.errors.length
        ? d.errors.join(" ")
        : (d.message || d.error || "Upload was not accepted by the operator backend.");
    }
    loadCommands(v.id); renderBody();
  }
  async function doClear(v) {
    if (!capabilityOf("MISSION_CLEAR").supported) return;
    if (uploadActive() || !hasControl()) return;
    // Every claim in this dialog is one the backend can actually back up: the rejection
    // conditions are Scout's, Home's survival is an ArduPilot fact (HOME_ONLY is a valid
    // empty state), and success really is judged by a fresh read-back — not by the
    // MISSION_CLEAR_ALL message being sent.
    if (!window.confirm(
      `Clear the mission stored on ${v.name || "USV-" + v.id}'s Pixhawk?\n\n` +
      `• The stored ROUTE will be removed from the flight controller.\n` +
      `• The operation is REJECTED while the vehicle is armed or in AUTO.\n` +
      `• Home may remain as Pixhawk item 0 — that is a correctly cleared mission, not a failure.\n` +
      `• Success is confirmed by a fresh read-back showing no route, never merely by sending MISSION_CLEAR_ALL.`)) return;
    const res = await api.clearMission(v.id);
    if (res.ok && res.data && res.data.command) {
      upload.cmdId = res.data.command.id; upload.at = Date.now(); upload.error = null;
      upload.readback = { status: null, at: 0, mission: null, error: null };
    } else {
      upload.error = (res.data && (res.data.message || res.data.error)) || "Clear was not accepted by the operator backend.";
    }
    loadCommands(v.id); renderBody();
  }
  // After Scout reports a terminal verified result, fetch the Pixhawk mission ONCE more —
  // the Operator's own, INDEPENDENT observation. Scout verifying its own write is Scout
  // marking its own homework; this fetch is the second opinion, and its failure is itself a
  // reportable outcome (READBACK_UNAVAILABLE), never silently treated as "no mission".
  async function doIndependentReadback(v) {
    upload.readback = { status: READBACK_PENDING, at: 0, mission: null, error: null };
    renderBody();
    try {
      const res = await api.getPixhawkMission(v.id);
      if (res && res.reachable && res.available !== false) {
        upload.readback = { status: READBACK_AVAILABLE, at: Date.now(), mission: res, error: null };
        // Keep the page's shared cache current too — Overview should not still show a
        // pre-operation mission once we have a fresher one in hand.
        const s = pxmState(v.id);
        s.mission = res; s.fetchedAt = Date.now();
        s.note = res.partial ? "partial" : null;
      } else {
        upload.readback = {
          status: READBACK_UNAVAILABLE, at: Date.now(), mission: null,
          error: res && res.available === false ? "Scout exposes no mission API." : "Scout unreachable.",
        };
      }
    } catch (e) {
      upload.readback = { status: READBACK_UNAVAILABLE, at: Date.now(), mission: null,
                          error: "Read-back fetch failed." };
    }
    renderBody();
  }
  function maybeReadback(v) {
    const cmd = trackedUpload();
    if (!cmd) return;
    if (missionUploadStage(cmd, liveUpload(v)).state === "done" && upload.readback.status == null) {
      // Marked PENDING synchronously so this render already shows "Awaiting independent
      // readback" rather than a state derived from a stale cache.
      upload.readback = { status: READBACK_PENDING, at: 0, mission: null, error: null };
      doIndependentReadback(v);
    }
  }

  function renderUpload(v) {
    const vname = v.name || "USV-" + v.id;
    maybeReadback(v);
    const p = upload.parsed;
    const cmd = trackedUpload();
    const live = liveUpload(v);
    const stg = cmd ? missionUploadStage(cmd, live) : null;
    const busy = uploadActive();
    const control = hasControl();
    const clearCap = capabilityOf("MISSION_CLEAR");

    // The preview states BOTH counts explicitly. "Route waypoints" is what the operator
    // supplied; "Pixhawk items after upload" is what the flight controller will hold, and
    // it is N+1 because Scout owns and prepends Home at seq 0. Showing only one of the two
    // is how an operator comes to believe a read-back of N+1 items is a mismatch.
    const preview = !p ? "" : p.ok
      ? `<div class="mu-preview">
           <div class="mrow"><span class="k">Format</span><span class="val">${p.format}</span></div>
           <div class="mrow"><span class="k">Route waypoints</span><span class="val">${p.routeCount}</span></div>
           <div class="mrow"><span class="k">Pixhawk items after upload</span><span class="val">${p.pixhawkItemCount} <span style="color:var(--muted)">including Home (seq 0, Scout-owned)</span></span></div>
           <div class="mrow"><span class="k">First route waypoint</span><span class="val">${p.first.lat.toFixed(6)}, ${p.first.lng.toFixed(6)}</span></div>
           <div class="mrow"><span class="k">Last route waypoint</span><span class="val">${p.last.lat.toFixed(6)}, ${p.last.lng.toFixed(6)}</span></div>
           <div class="mrow"><span class="k">Expected route content hash</span><span class="val">${
             upload.expected && upload.expected.expected_route_content_hash
               ? `<span title="${escapeAttr(upload.expected.expected_route_content_hash)}" style="font-family:var(--mono,monospace)">${escapeHtml(shortHash(upload.expected.expected_route_content_hash))}</span>`
               : noTelem("operator backend did not return an expected hash")}</span></div>
         </div>`
      : `<div class="mu-err">${p.errors.map((e) => `• ${escapeHtml(e)}`).join("<br>")}</div>`;

    const hashCell = (h) => h
      ? `<span title="${escapeAttr(h)}" style="font-family:var(--mono,monospace)">${escapeHtml(shortHash(h))}</span>`
      : "—";

    // ── The two observations, kept separate ──────────────────────────────────────
    // `rb` is the OPERATOR's own post-operation read-back — deliberately NOT the shared
    // pxm cache, which may predate this operation and would turn a stale mission into
    // "evidence". Null until our own fetch lands.
    const rb = upload.readback.mission;
    const rbStatus = upload.readback.status;
    let cmp = null, clearOut = null, agrees = null;
    if (cmd && stg && stg.state === "done") {
      if (cmd.type === "MISSION_CLEAR") {
        const rbRoute = rb && rb.waypoints ? classifyMissionWaypoints(rb.waypoints).route.length : null;
        clearOut = missionClearOutcome(cmd.result, rb || {}, rbRoute);
        agrees = clearOut.readbackAgrees;
      } else {
        const obsRoute = rb && rb.waypoints ? classifyMissionWaypoints(rb.waypoints).route.length : null;
        cmp = missionUploadCompare(cmd.params, rb || {}, obsRoute);
        // A read-back we HAVE but which carries no route hash cannot agree or disagree —
        // it is missing evidence (caution), not a conflict. Reporting it as a conflict
        // would accuse Scout of lying when the truth is that we could not check.
        agrees = cmp.hashUnavailable ? null : cmp.match;
      }
    }
    const opState = missionOperationState(stg, rbStatus, agrees);

    // The final step reads Verified only when the INDEPENDENT read-back backed it up.
    // While that fetch is outstanding the step is still in progress, never "Failed" —
    // the whole point of the awaiting state is that it is not a failure.
    const FINAL_STEP = { verified: ["done", "Verified"], conflict: ["failed", "Conflict"],
                         failed: ["failed", "Failed"],
                         readback_unavailable: ["caution", "Scout only"],
                         awaiting_readback: ["on", "Awaiting readback"] };
    const trackHtml = stg ? `<div class="mu-track">${UPLOAD_STAGES.map((name, i) => {
      const final = FINAL_STEP[opState.state];
      if (i === UPLOAD_STAGES.length - 1 && final) {
        return `<div class="mu-step ${final[0]}">${final[1]}</div>`;
      }
      let cls = "";
      if (i < stg.index) cls = "done";
      else if (i === stg.index) cls = stg.state === "failed" ? "failed" : stg.state === "done" ? "done" : "on";
      return `<div class="mu-step ${cls}">${stg.state === "failed" && i === stg.index ? "Failed" : name}</div>`;
    }).join("")}</div>` : "";

    let verdict = "";
    if (stg) {
      if (opState.state === "failed") {
        // When Scout supplied a STRUCTURED error, that error is the whole explanation and
        // is rendered from Scout's own fields — no generic "may be unchanged or partial"
        // tail, which fits any failure and helps with none. MISSION_TOO_LARGE in particular
        // is fully actionable on its own: it names the limit and what was submitted.
        const scoutErr = missionErrorOf(cmd);
        const structured = missionErrorText(scoutErr);
        verdict = structured
          ? `<div class="mu-verdict bad">${cmd.type === "MISSION_CLEAR" ? "Clear" : "Upload"} refused by Scout — ${escapeHtml(structured)}${scoutErr.code ? ` <span style="color:var(--muted);font-family:var(--mono,monospace)">[${escapeHtml(scoutErr.code)}]</span>` : ""}</div>`
          : `<div class="mu-verdict bad">${cmd.type === "MISSION_CLEAR" ? "Clear" : "Upload"} failed — ${escapeHtml(stg.reason || "not verified by read-back")}. The Pixhawk mission may be unchanged or partial — use Overview → Refresh to check.</div>`;
      } else if (opState.state === "awaiting_readback") {
        // Explicitly NOT rendered as Failed. Scout has reported success; our own fetch is
        // simply still in flight, and a "Failed" that flickers on every successful upload
        // teaches the operator to ignore the real one.
        verdict = `<div class="mu-verdict pending">Awaiting independent readback — ${escapeHtml(opState.detail)}</div>`;
      } else if (opState.state === "readback_unavailable") {
        verdict = `<div class="mu-verdict caution">Scout verified; independent Operator readback unavailable — ${escapeHtml(upload.readback.error || (cmp && cmp.hashUnavailable ? "the read-back carried no route content hash to compare." : "the Pixhawk mission could not be re-fetched."))} This is <b>not</b> a full verification: only Scout's own report supports it. Retry the read-back, or use Overview → Refresh, before treating this mission as confirmed.</div>`;
      } else if (opState.state === "conflict") {
        // Highest severity on this page: two systems disagree about the same flight
        // controller, so neither can be trusted about this mission.
        const what = cmd.type === "MISSION_CLEAR"
          ? `Scout reported the mission cleared, but the Operator's own read-back still lists ${clearOut.readbackRoute} route waypoints.`
          : (cmp.hashMatch === false
              ? `Scout reported the upload verified, but the Operator's own read-back gives a DIFFERENT route content hash — expected ${hashCell(cmp.expectedHash)}, observed ${hashCell(cmp.observedHash)}. The counts may agree; the route on the flight controller is not the route you approved.`
              : `Scout reported the upload verified, but the Operator's own read-back shows ${cmp.observedRoute ?? "?"} route waypoints / ${cmp.observedItems ?? "?"} Pixhawk items against an expected ${cmp.expectedRoute} / ${cmp.expectedItems}.`);
        verdict = `<div class="mu-verdict bad"><b>VERIFICATION CONFLICT.</b> ${what} One of the two reports is wrong. Do not fly this mission — re-read the mission on the Overview tab and re-upload before proceeding.</div>`;
      } else if (opState.state === "verified" && cmd.type === "MISSION_CLEAR") {
        const shape = clearOut.representation === "HOME_ONLY"
          ? "Home remains as Pixhawk item 0 — a correctly cleared mission on this flight controller."
          : "The flight controller holds no mission items at all.";
        verdict = `<div class="mu-verdict ok">Mission cleared — verified route count 0, empty representation ${escapeHtml(clearOut.representation)}. ${shape} Confirmed by the Operator's own independent read-back.</div>`;
      } else if (opState.state === "verified") {
        // All three axes agreed, AND our independent read-back is what they were checked
        // against. Each axis is named with its value: "Verified" alone does not tell the
        // operator WHAT was proven.
        verdict = `<div class="mu-verdict ok">Verified — verified route count ${cmp.observedRoute}, verified Pixhawk item count ${cmp.observedItems} (including Home), verified route content hash ${hashCell(cmp.observedHash)}. Confirmed by the Operator's own independent read-back: the route on the flight controller is byte-for-byte the route you approved.</div>`;
      } else {
        // Executing is Scout's OWN live worker state for THIS command id — never inferred
        // from "some upload is running". elapsed_s is Scout's, not a local timer.
        const elapsed = stg.elapsedS != null ? ` (${Math.round(stg.elapsedS)}s elapsed, reported by Scout)` : "";
        const detail = stg.stage === "Executing"
          ? `Scout's upload worker is writing the mission to the Pixhawk${elapsed}.`
          : `Queued for delivery — Scout has not started the transfer yet.`;
        verdict = `<div class="mu-verdict pending">In progress — ${stg.stage}. ${detail} An upload is verified only once the read-back matches; the file reaching Scout is not success.</div>`;
      }
    }

    // ── Technical details (bench testing / thesis evidence) ─────────────────────
    // Collapsed by default and deliberately SEPARATE from the verdict above: the operator
    // needs one sentence about whether the mission is on the vehicle, not a field dump.
    // This panel is for a controlled bench test, where the useful question is which
    // specific field disagreed. Every row is a value the backend or Scout actually
    // reported — an absent field renders "—", never a plausible-looking default.
    const res = (cmd && cmd.result) || {};
    const par = (cmd && cmd.params) || {};
    const err = cmd && (cmd.error || res.error);
    const errCode = err && typeof err === "object" ? err.code : (res.error_code ?? null);
    const errMsg = err && typeof err === "object" ? err.message : (typeof err === "string" ? err : null);
    const mine = liveUpload(v) && cmd && String(liveUpload(v).command_id) === String(cmd.id) ? liveUpload(v) : null;
    const techRow = (k, val, mono) =>
      `<div class="mrow"><span class="k">${k}</span><span class="val"${mono ? ' style="font-family:var(--mono,monospace)"' : ""}>${val == null || val === "" ? "—" : val}</span></div>`;
    const techHtml = !cmd || !showTechnical ? "" : `
      <div class="mu-preview" id="mu-tech">
        ${techRow("command_id", escapeHtml(cmd.id), true)}
        ${techRow("contract_version", escapeHtml(par.contract_version ?? ""))}
        ${techRow("expected_route_waypoint_count", par.expected_route_waypoint_count)}
        ${techRow("expected_pixhawk_item_count", par.expected_pixhawk_item_count)}
        ${techRow("expected_route_content_hash", par.expected_route_content_hash ? hashCell(par.expected_route_content_hash) : null)}
        ${techRow("Scout worker state", mine ? `${escapeHtml(mine.state || "—")} (active: ${mine.active ? "yes" : "no"})` : "not reported for this command")}
        ${techRow("Elapsed upload time", mine && mine.elapsed_s != null ? `${Math.round(mine.elapsed_s)}s (reported by Scout)` : null)}
        ${techRow("observed_route_waypoint_count", res.observed_route_waypoint_count ?? res.route_waypoint_count)}
        ${techRow("observed_pixhawk_item_count", res.observed_pixhawk_item_count ?? res.pixhawk_item_count)}
        ${techRow("observed_route_content_hash", res.observed_route_content_hash ? hashCell(res.observed_route_content_hash) : null)}
        ${techRow("MAVLink acknowledgement", escapeHtml(res.acknowledgement ?? ""))}
        ${cmd.type === "MISSION_CLEAR" ? techRow("empty_representation", escapeHtml(res.empty_representation ?? "")) : ""}
        ${techRow("Error code", escapeHtml(errCode ?? ""), true)}
        ${techRow("Error message", escapeHtml(missionErrorText(missionErrorOf(cmd)) ?? errMsg ?? ""))}
        ${errCode === MISSION_TOO_LARGE ? `
        ${techRow("maximum_route_waypoints", missionErrorOf(cmd).maximum_route_waypoints)}
        ${techRow("observed_route_waypoints", missionErrorOf(cmd).observed_route_waypoints)}` : ""}
        ${techRow("Independent readback status", escapeHtml(rbStatus ?? "not started"))}
        ${techRow("Independent readback at", upload.readback.at ? escapeHtml(new Date(upload.readback.at).toISOString()) : null, true)}
        ${techRow("Operation state", `${escapeHtml(opState.label)} (${escapeHtml(opState.state)})`)}
        <div class="mu-row" style="margin-top:10px">
          <button class="cfg-btn" id="mu-evidence">Export evidence (JSON)</button>
          <button class="diag-btn" id="mu-readback-retry"${rbStatus === READBACK_PENDING ? " disabled" : ""}>Retry independent readback</button>
        </div>
      </div>`;

    const controlNote = control ? "" : `<div class="mu-err">Control authority is ${authority && authority.authority ? authority.authority : "unknown"} — take OPERATOR control (Map or Vehicle page) before uploading. Uploads are disabled until the operator holds authority.</div>`;

    document.getElementById("mbody").innerHTML = `
      <div class="msect"><span class="lbl">Mission upload</span><span class="tag">${vname} · write a validated mission to the Pixhawk (read-back verified)</span></div>
      <div style="padding:0 20px 20px">
        <div class="mu-drop">
          <textarea id="mu-text" placeholder='Paste a route — route waypoints only: {"contract_version":"mission-contract-v1","waypoints":[{"latitude":56.6501,"longitude":12.8701,"loiter_time_s":0}]} — or GeoJSON (Point features / a LineString). Do NOT include seq, command, frame or altitude: Scout owns those and owns Home at seq 0.'>${escapeHtml(upload.text)}</textarea>
          <div class="mu-row">
            <input type="file" id="mu-file" accept=".json,.geojson,application/json" />
            <button class="cfg-btn" id="mu-validate">Validate &amp; preview</button>
            <button class="cfg-btn" id="mu-upload"${(!p || !p.ok || busy || !control) ? " disabled" : ""}>${busy ? "Upload in progress…" : "Upload route to Pixhawk"}</button>
            <button class="diag-btn" id="mu-clear"${(busy || !control || !clearCap.supported) ? " disabled" : ""} style="margin-left:auto"
              title="${escapeAttr(clearCap.supported ? "Clears the mission stored on the Pixhawk." : clearCap.reason)}">Clear Pixhawk mission…</button>
          </div>
          ${clearCap.supported ? "" : `<div class="cfg-note" style="border-color:var(--caution);color:var(--caution)">${infoIcon}<span><b>Clear Pixhawk mission unavailable.</b> ${escapeHtml(clearCap.reason || "The operator backend does not currently accept MISSION_CLEAR.")}</span></div>`}
          ${controlNote}
          ${upload.error ? `<div class="mu-err">${escapeHtml(upload.error)}</div>` : ""}
          ${preview}
        </div>
        ${trackHtml}${verdict}
        ${cmd ? `<div class="mu-row" style="margin-top:10px">
          <button class="diag-btn" id="mu-tech-toggle">${showTechnical ? "Hide" : "Show"} technical details</button>
          <span style="font-size:11px;color:var(--muted)">Field-level contract detail for bench testing — the summary above is the operator-facing result.</span>
        </div>${techHtml}` : ""}
        <div class="ev-note" style="margin-top:14px">${infoIcon}You supply <b>route waypoints only</b>. Scout owns Pixhawk sequence 0 / Home and prepends it, so a route of N waypoints leaves <b>N + 1</b> items on the flight controller — that is a correct upload, not a mismatch. <code>seq</code>, <code>command</code>, <code>frame</code> and <code>altitude</code> are rejected rather than previewed, because Scout would discard them and you would have approved a mission that is not the one uploaded. Upload is confirmed only by re-downloading the mission from the Pixhawk and matching the expected counts <b>and the route content hash</b> — never because the file reached Scout. The hash is a SHA-256 over the canonical route (Home excluded), computed by the operator backend and compared against Scout's; it is the only axis that can catch two swapped waypoints or a wrong coordinate, which have the same counts as a correct route. An upload whose hash is missing or differs is reported as NOT verified, never as a count-only success. A completed upload is reported as <b>Verified</b> only once the Operator's OWN independent read-back has confirmed it — until that fetch lands you will see "Awaiting independent readback", which is not a failure. If that read-back cannot be obtained you get a caution, not a green result: Scout's word alone is not an independent verification. ${maxRouteWaypoints() != null ? `A single upload accepts at most <b>${maxRouteWaypoints()} route waypoints</b>${capabilities && capabilities.max_route_waypoints_source === "scout-contract" ? " — defined and enforced by Scout under mission-contract-v1; the Operator mirrors it so an oversized route is refused at preview, before anything is transmitted" : ""}; the same limit applies to preview and to upload, so a route that previews will not be refused on send.` : ""} Normal mission read-back stays available on the Overview tab. Waypoint jumping is not offered.</div>
      </div>`;

    const ta = document.getElementById("mu-text");
    if (ta) ta.oninput = () => { upload.text = ta.value; };   // store only — no re-render (keeps focus)
    const vb = document.getElementById("mu-validate");
    if (vb) vb.onclick = () => { if (ta) upload.text = ta.value; parseUploadNow(); };
    const tt = document.getElementById("mu-tech-toggle");
    if (tt) tt.onclick = () => { showTechnical = !showTechnical; renderBody(); };
    const rr = document.getElementById("mu-readback-retry");
    if (rr) rr.onclick = () => doIndependentReadback(v);
    const ev = document.getElementById("mu-evidence");
    // One operation's evidence, not a general experiment framework: exactly the command
    // on screen, its lifecycle, Scout's result, our independent read-back and the
    // comparison between them — the record a bench test or the thesis cites.
    if (ev) ev.onclick = () => downloadBlob(
      `mission-evidence-${cmd.type.toLowerCase()}-${cmd.id}.json`,
      JSON.stringify(missionEvidence({
        cmd, live: liveUpload(v), readback: upload.readback.mission,
        readbackStatus: rbStatus, readbackAt: upload.readback.at,
        comparison: cmp || clearOut || null, operationState: opState, requestedAt: upload.at,
      }), null, 2),
      "application/json");
    const ub = document.getElementById("mu-upload"); if (ub) ub.onclick = () => doUpload(v);
    const cb = document.getElementById("mu-clear"); if (cb) cb.onclick = () => doClear(v);
    const fb = document.getElementById("mu-file");
    if (fb) fb.onchange = () => {
      const f = fb.files && fb.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => { upload.text = String(r.result || ""); parseUploadNow(); };
      r.readAsText(f);
    };
  }

  function renderBody() {
    const v = currentVehicle();
    document.getElementById("mtitle").textContent = v ? `${v.name || "USV-" + v.id} · ${commState(v)}` : "No vehicle selected";
    if (!v) { document.getElementById("mbody").innerHTML = `<div class="empty-state" style="padding:20px">No vehicle selected</div>`; return; }
    if (tab === "overview") renderOverview(v);
    else if (tab === "upload") renderUpload(v);
    else if (tab === "replay") renderReplay(v);
    else if (tab === "statistics") renderStatistics(v);
    else renderExport(v);
  }

  function renderDock() {
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => selectVehicle(canonicalVehicleId(el.dataset.id))));
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

  // Command capabilities are a backend/Scout-contract fact, not per-vehicle state — fetched
  // once per mount. A failure leaves `capabilities` null, which capabilityOf() treats as
  // supported (the backend still refuses an unsupported command).
  api.getCommandCapabilities().then((c) => { capabilities = c; if (tab === "upload") renderBody(); }).catch(() => {});

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, updateFeedIndicator, "fleet");
  const stopEvents = api.poll(api.getEventLog, 5000, onEvents, () => {});
  const commsId = setInterval(() => loadCommsHistory(selId), 3000);
  const commandsId = setInterval(() => loadCommands(selId), 3000);
  const authorityId = setInterval(() => loadAuthority(selId), 3000);  // control-authority gate for Upload
  const clockId = setInterval(() => { updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }); updateFeedIndicator(); }, 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
  updateFeedIndicator();

  return function cleanup() { stopFleet(); stopEvents(); clearInterval(commsId); clearInterval(commandsId); clearInterval(authorityId); clearInterval(clockId); };
}
