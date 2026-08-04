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
import { HealthBadge, deriveHealth } from "../components/HealthBadge.js";
import { commState, cls, fmtAge, statusDot, noTelem } from "../lib/ui.js";
import { canonicalVehicleId, getSelectedVehicleId, setSelectedVehicleId, subscribeSelection }
  from "../lib/selection.js";
import { DEFAULT_SORT, sortFleet, nextSort } from "../lib/fleet-sort.js";

export function Fleet(root) {
  // Selection is the SHARED canonical one, not a page-local id: this page used to keep its
  // own `selId` and seed it from fleet[0] — a selection derived from list position, which
  // silently moved whenever the fleet order or membership changed. Highlighting here now
  // means the same thing as on Map/Vehicle, and follows the operator across pages.
  //
  // The default sort was `{ key: "age", dir: -1 }` — last contact, recomputed every 2 s poll.
  // With two USVs reporting at slightly different moments their ages crossed and the rows
  // swapped on their own. Default is now canonical-id order (lib/fleet-sort.js), which no
  // amount of traffic can change; an operator-chosen sort still stays active until changed.
  let fleet = [], selId = getSelectedVehicleId(), sort = DEFAULT_SORT;

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
    // name truncates at 26ch (theme.css) so one long name cannot widen the table past the
    // viewport; title keeps the full value inspectable
    { key: "name", label: "Vehicle", sortable: true, render: (v) => { const n = v.name || "USV-" + v.id; return `<span class="vname">${statusDot(v)}<b title="${n}">${n}</b></span>`; } },
    { key: "comm", label: "Comms", sortable: true, render: (v) => CommsPill(v) },
    { key: "age", label: "Last Contact", align: "num", sortable: true, render: (v) => `<span class="mono txt-${cls(v)}">${fmtAge(v.last_seen_age_s)}</span>` },
    { key: "health", label: "Health", sortable: true, render: (v) => HealthBadge(v) },
    { key: "batt", label: "Battery", align: "num", sortable: true, render: (v) => BatteryBar(v.battery) },
    { key: "task", label: "Current Task", render: (v) => `<span>${String(activity(v))}</span>` },
    { key: "cov", label: "Coverage", align: "num", sortable: true, render: coverageCell },
  ];

  function renderTable() {
    // sortFleet returns a COPY — `fleet` stays in the order the backend sent it, because the
    // rollup, the counts and (on other pages) the telemetry cache read the same records.
    document.getElementById("tw").innerHTML =
      Table(columns, sortFleet(fleet, sort), { selectedId: selId, sort });
    document.querySelectorAll("#tw th[data-sort-key]").forEach((th) => (th.onclick = () => {
      sort = nextSort(sort, th.dataset.sortKey);
      renderTable();
    }));
    document.querySelectorAll("#tw tbody tr").forEach((tr) => (tr.onclick = () => {
      // canonicalVehicleId, not `+id`: a vehicle whose canonical id is a string (one with
      // no numeric identity) must stay selectable, and `+"sar-001"` is NaN.
      selId = setSelectedVehicleId(canonicalVehicleId(tr.dataset.id));
      renderTable();
    }));
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
    // A fleet poll replaces the roster but NEVER the selection: no auto-select of the
    // first row, the newest row, or the most recently connected vehicle. An unselected
    // roster simply renders unselected until the operator picks a vehicle.
    fleet = Array.isArray(data) ? data : [];
    document.getElementById("fcount").textContent = `${fleet.length} vehicles`;
    renderRollup(); renderTable();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  // Follow a selection made on another page (Map/Vehicle) without re-deriving one here.
  const unsubscribe = subscribeSelection((id) => { selId = id; if (fleet.length) renderTable(); });
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); unsubscribe(); clearInterval(clockId); };
}
