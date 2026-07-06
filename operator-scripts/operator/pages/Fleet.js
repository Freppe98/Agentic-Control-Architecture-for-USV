// Fleet.js — triage roster from live api.getFleet(). Reuses Map's components +
// new Table/HealthBadge. Columns: Vehicle · Comms · Last Contact · Health ·
// Battery · Current Task · Coverage(NO-TELEM if absent). No backend routes added.
// NOTE: assigned/depot split and per-vehicle coverage need backend support that
// doesn't exist yet, so they are not faked — coverage falls back to NO-TELEM.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
import { Table } from "../components/Table.js";
import { HealthBadge, deriveHealth, healthRank } from "../components/HealthBadge.js";
import { commState, cls, fmtAge, statusDot, noTelem } from "../lib/ui.js";

const commRank = { connected: 0, partitioned: 1, disconnected: 2, unknown: 3 };

export function Fleet(root) {
  let fleet = [], selId = null, sort = { key: "age", dir: -1 };

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("fleet") +
    `<div class="page">
       <div class="toolbar"><h1>Fleet</h1><span class="count mono" id="fcount">—</span></div>
       <div class="rollup" id="rollup"></div>
       <div class="tablewrap" id="tw"></div>
     </div>`;

  const activity = (v) => v.status || v.mission || (v.telemetry && v.telemetry.mode) || "—";

  const coverageCell = (v) =>
    v.coverage == null
      ? noTelem()
      : `<span class="batt-inline"><span class="bar"><i style="width:${v.coverage}%;background:var(--accent)"></i></span><span class="bpc">${v.coverage}%</span></span>`;

  const columns = [
    { key: "name", label: "Vehicle", sortable: true, render: (v) => `<span class="vname">${statusDot(v)}<b>${v.name || "USV-" + v.id}</b></span>` },
    { key: "comm", label: "Comms", sortable: true, render: (v) => CommsPill(v) },
    { key: "age", label: "Last Contact", align: "num", sortable: true, render: (v) => `<span class="mono txt-${cls(v)}">${fmtAge(v.last_seen_age_s)}</span>` },
    { key: "health", label: "Health", sortable: true, render: (v) => HealthBadge(v) },
    { key: "batt", label: "Battery", align: "num", sortable: true, render: (v) => BatteryBar(v.battery) },
    { key: "task", label: "Current Task", render: (v) => `<span>${String(activity(v))}</span>` },
    { key: "cov", label: "Coverage", align: "num", sortable: true, render: coverageCell },
  ];

  function sorted() {
    const arr = [...fleet];
    const k = sort.key;
    arr.sort((a, b) => {
      let x, y;
      if (k === "name") { x = a.id; y = b.id; }
      else if (k === "comm") { x = commRank[commState(a)]; y = commRank[commState(b)]; }
      else if (k === "age") { x = a.last_seen_age_s ?? -1; y = b.last_seen_age_s ?? -1; }
      else if (k === "health") { const ha = deriveHealth(a), hb = deriveHealth(b); x = ha ? healthRank[ha.sev] : -1; y = hb ? healthRank[hb.sev] : -1; }
      else if (k === "batt") { x = a.battery ?? -1; y = b.battery ?? -1; }
      else if (k === "cov") { x = a.coverage ?? -1; y = b.coverage ?? -1; }
      else { x = 0; y = 0; }
      return (x - y) * sort.dir;
    });
    return arr;
  }

  function renderTable() {
    document.getElementById("tw").innerHTML = Table(columns, sorted(), { selectedId: selId, sort });
    document.querySelectorAll("#tw th[data-sort-key]").forEach((th) => (th.onclick = () => {
      const key = th.dataset.sortKey;
      if (sort.key === key) sort.dir *= -1; else sort = { key, dir: key === "name" ? 1 : -1 };
      renderTable();
    }));
    document.querySelectorAll("#tw tbody tr").forEach((tr) => (tr.onclick = () => { selId = +tr.dataset.id; renderTable(); }));
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function renderRollup() {
    const c = counts();
    const health = { ok: 0, caution: 0, warn: 0, none: 0 };
    fleet.forEach((v) => { const h = deriveHealth(v); h ? health[h.sev]++ : health.none++; });
    const batts = fleet.map((v) => v.battery).filter((b) => b != null);
    const avg = batts.length ? Math.round(batts.reduce((a, b) => a + b, 0) / batts.length) : null;
    const cov = fleet.map((v) => v.coverage).find((x) => x != null);
    document.getElementById("rollup").innerHTML = `
      <div class="rtile"><span class="lbl">Comms</span><span class="v"><span class="seg"><span class="dot c"></span>${c.c}</span><span class="seg"><span class="dot p"></span>${c.p}</span><span class="seg"><span class="dot d"></span>${c.d}</span></span><span class="sub">${fleet.length} vehicles</span></div>
      <div class="rtile"><span class="lbl">Health</span><span class="v">${health.warn ? `<span class="seg" style="color:var(--warn)">${health.warn} warn</span>` : ""}${health.caution ? `<span class="seg" style="color:var(--caution)">${health.caution} caut</span>` : ""}<span class="seg" style="color:var(--ok)">${health.ok} ok</span></span><span class="sub">${health.none ? health.none + " no telem" : "derived from live inputs"}</span></div>
      <div class="rtile"><span class="lbl">Avg battery</span><span class="v">${avg == null ? "—" : avg + "%"}</span><span class="sub">${batts.length} reporting</span></div>
      <div class="rtile"><span class="lbl">Coverage</span><span class="v">${cov == null ? "—" : cov + "%"}</span><span class="sub">${cov == null ? "not reported" : "area coverage"}</span></div>`;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) selId = fleet[0].id;
    document.getElementById("fcount").textContent = `${fleet.length} vehicles`;
    renderRollup(); renderTable();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); };
}
