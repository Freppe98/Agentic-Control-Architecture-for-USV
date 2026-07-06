// Mission.js — the mission scope page. Overview tab is live from api.getFleet()
// per-vehicle mission fields (mission_data, coverage, fleet_info); the Replay /
// Statistics / Export tabs are honest scaffolding, disabled until a mission-history
// backend exists. No backend routes added.
//
// Honesty boundaries:
//   • Participation is REAL — driven by mission_data.mission_active (a live field).
//   • Coverage tile is a TRANSPARENT aggregate (avg across reporting vehicles),
//     labelled as such — the backend has no mission-level coverage total.
//   • Named mission scope and ETA / time-remaining are the mission-object gap
//     (GET /api/mission) → NO-TELEM, never fabricated.
//   • assigned/depot split has no backend field → surfaced in the footnote, not faked.
// Reuses: Ribbon, NavRail, Table, CommsPill, rollup tiles, bar() — no new components.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { Table } from "../components/Table.js";
import { CommsPill } from "../components/CommsPill.js";
import { commState, statusDot, noTelem } from "../lib/ui.js";

const TABS = [
  ["overview", "Overview", true],
  ["replay", "Replay", false],
  ["statistics", "Statistics", false],
  ["export", "Export", false],
];
const LOCKED_HINT = "Available after a mission completes — needs backend mission history";

const infoIcon =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v0.01M11 12h1v4h1"/></svg>';

export function Mission(root) {
  let fleet = [];

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("mission") +
    `<div class="page">
       <div class="toolbar"><h1>Mission</h1><span class="count" id="mscope">no named scope</span></div>
       <div class="mtabs" id="mtabs">${TABS.map(([k, l, on]) =>
         `<button class="mtab${k === "overview" ? " on" : ""}${on ? "" : " locked"}" data-tab="${k}"${on ? "" : ` title="${LOCKED_HINT}"`}>${l}${on ? "" : ' <span class="lock">·</span>'}</button>`
       ).join("")}</div>
       <div class="mission-body" id="mbody"></div>
     </div>`;

  // vehicles actively participating in a mission (real field, last-known when stale)
  const inMission = () => fleet.filter((v) => v.mission_data && v.mission_data.mission_active);

  const columns = [
    { key: "veh", label: "Vehicle", render: (v) => `<span class="vname">${statusDot(v)}<b>${v.name || "USV-" + v.id}</b></span>` },
    { key: "comms", label: "Comms", render: (v) => CommsPill(v) },
    { key: "role", label: "Role", render: (v) => txt(v.fleet_info && v.fleet_info.fleet_role) },
    { key: "sector", label: "Sector", render: (v) => txt(v.fleet_info && v.fleet_info.assigned_sector) },
    { key: "wp", label: "Waypoint", render: (v) => `<span class="mono">${clean(v.mission_data && v.mission_data.current_waypoint_display) || "—"}</span>` },
    { key: "state", label: "State", render: (v) => txt(v.mission_data && v.mission_data.mission_state) },
    { key: "cov", label: "Coverage", align: "num", render: (v) =>
        v.coverage == null ? `<span style="color:var(--dim)">—</span>`
          : `<span class="batt-inline"><span class="bar"><i style="width:${v.coverage}%;background:var(--accent)"></i></span><span class="bpc">${v.coverage}%</span></span>` },
  ];

  const clean = (s) => (s == null || s === "" ? null : String(s));
  const txt = (s) => (clean(s) ? `<span>${clean(s)}</span>` : `<span style="color:var(--dim)">—</span>`);

  function summaryTiles() {
    const mv = inMission();
    const covVals = mv.map((v) => v.coverage).filter((c) => c != null);
    const covAvg = covVals.length ? Math.round(covVals.reduce((a, b) => a + b, 0) / covVals.length) : null;
    const states = {};
    mv.forEach((v) => { const s = (v.mission_data && v.mission_data.mission_state) || "—"; states[s] = (states[s] || 0) + 1; });
    const stateSeg = Object.keys(states).length
      ? Object.entries(states).map(([s, n]) => `<span class="seg">${n} ${s.toLowerCase()}</span>`).join("")
      : `<span style="color:var(--dim)">—</span>`;

    return `
      <div class="rollup">
        <div class="rtile"><span class="lbl">Mission scope</span><span class="v">${noTelem("no named scope")}</span><span class="sub">named-mission registry is a backend gap</span></div>
        <div class="rtile"><span class="lbl">Activity</span><span class="v">${stateSeg}</span><span class="sub">${mv.length} vehicle${mv.length === 1 ? "" : "s"} in mission</span></div>
        <div class="rtile"><span class="lbl">Coverage</span><span class="v">${covAvg == null ? "—" : covAvg + "%"}</span><span class="sub">${covVals.length ? "avg of " + covVals.length + " reporting" : "not reported"}</span></div>
        <div class="rtile"><span class="lbl">ETA / remaining</span><span class="v">${noTelem("no mission object")}</span><span class="sub">needs GET /api/mission</span></div>
      </div>`;
  }

  function renderOverview() {
    const mv = inMission();
    const table = mv.length
      ? `<div class="tablewrap" style="flex:none">${Table(columns, mv, { idKey: "id" })}</div>`
      : `<div class="empty-state" style="padding:16px 20px">No vehicle reports an active mission. Assignment lives in the backend mission object, which does not exist yet.</div>`;

    document.getElementById("mbody").innerHTML =
      summaryTiles() +
      `<div class="msect"><span class="lbl">Participating vehicles</span></div>` +
      table +
      `<div class="ev-note">${infoIcon}
         Overview is live from per-vehicle mission fields. A named mission scope, mission-level ETA/remaining, the assigned-vs-depot split, and Replay / Statistics / Export all require a mission-object backend (GET /api/mission) that does not exist yet — they are shown as gaps, not fabricated.
       </div>`;
  }

  function scopeLabel() {
    document.getElementById("mscope").textContent = `${inMission().length} in mission · no named scope`;
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    renderOverview(); scopeLabel();
    updateRibbon({ counts: counts() });
  }

  // Locked tabs are inert (honest scaffold); only Overview is interactive today.
  document.querySelectorAll("#mtabs .mtab.locked").forEach((b) => (b.onclick = () => {}));

  renderOverview();
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); };
}
