// Events.js — fleet-wide event log for triage. Feed comes from api.getEvents()
// (flattened per-vehicle payload events); ribbon comms counts from api.getFleet().
// Severity is honest: events with no level tag render UNSPEC, never a fabricated
// severity. Acknowledgement is a genuine operator action but SESSION-LOCAL (client
// side) — the backend has no persistent event log or ack endpoint yet, so we do not
// imply server persistence. Reuses Table (with row-tint) + shared ui helpers.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { Table } from "../components/Table.js";
import { SEV, evSeverity, evText, evTime, commState } from "../lib/ui.js";

const FILTERS = [
  ["all", "All"], ["unack", "Unacknowledged"],
  ["emergency", "Emergency"], ["warning", "Warning"], ["caution", "Caution"], ["info", "Info"],
];
const isAckable = (sev) => sev && SEV[sev].rank >= SEV.caution.rank;

export function Events(root) {
  let items = [];              // normalized + sorted (newest first)
  const acked = new Set();     // session-local acknowledgements, keyed per event
  let filter = "all";

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("events") +
    `<div class="page">
       <div class="toolbar"><h1>Events</h1><span class="count mono" id="ecount">—</span></div>
       <div class="rollup" id="erollup"></div>
       <div class="evbar" id="evfilters"></div>
       <div class="tablewrap" id="etw"></div>
       <div class="ev-note">
         <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>
         Feed reflects events present in the latest fleet payloads. A dedicated, persistent event log is a known backend gap; acknowledgements are session-local.
       </div>
     </div>`;

  // ---- normalize one flattened event {vehicle, vehicleId, event} ----
  function normalize(raw) {
    const sev = evSeverity(raw.event);
    const time = evTime(raw.event);
    const text = evText(raw.event);
    const key = `${raw.vehicleId}|${time ? time.label : "?"}|${text}`;
    return { ...raw, sev, time, text, key, acked: acked.has(key) };
  }

  const sevChip = (sev) => {
    const color = sev ? `var(--${SEV[sev].token})` : "var(--dim)";
    const label = sev ? SEV[sev].label : "UNSPEC";
    return `<span class="sevchip" style="--sc:${color}"><span class="sd"></span>${label}</span>`;
  };
  const metaOf = (it) => {
    const e = it.event || {}, m = [];
    if (e.type) m.push(`type ${e.type}`);
    if (e.source) m.push(`src ${e.source}`);
    return m.length ? `<span class="evmeta">${m.join(" · ")}</span>` : "";
  };
  const ackCell = (it) =>
    isAckable(it.sev)
      ? `<button class="ev-ack${it.acked ? " done" : ""}" data-key="${it.key}">${it.acked ? "Acked" : "Ack"}</button>`
      : "";

  const columns = [
    { key: "time", label: "Time", render: (it) => it.time ? `<span class="mono">${it.time.label}</span>` : `<span class="mono" style="color:var(--dim)">—</span>` },
    { key: "sev", label: "Severity", render: (it) => sevChip(it.sev) },
    { key: "veh", label: "Vehicle", render: (it) => `<span class="vname"><b>${it.vehicle || "USV-" + it.vehicleId}</b></span>` },
    { key: "msg", label: "Message", render: (it) => `<span class="tx">${it.text}</span>${metaOf(it)}` },
    { key: "ack", label: "", align: "num", render: ackCell },
  ];

  const unackCount = () => items.filter((it) => isAckable(it.sev) && !it.acked).length;

  function shown() {
    if (filter === "all") return items;
    if (filter === "unack") return items.filter((it) => isAckable(it.sev) && !it.acked);
    return items.filter((it) => it.sev === filter);
  }

  function renderFilters() {
    const counts = { all: items.length, unack: unackCount(), emergency: 0, warning: 0, caution: 0, info: 0 };
    items.forEach((it) => { if (it.sev) counts[it.sev]++; });
    document.getElementById("evfilters").innerHTML = FILTERS.map(([k, l]) =>
      `<button class="chip${filter === k ? " on" : ""}" data-f="${k}">${l}<span class="cc">${counts[k] ?? 0}</span></button>`
    ).join("");
    document.querySelectorAll("#evfilters .chip").forEach((b) => (b.onclick = () => { filter = b.dataset.f; render(); }));
  }

  function renderRollup() {
    const c = { emergency: 0, warning: 0, caution: 0, info: 0, none: 0 };
    items.forEach((it) => (it.sev ? c[it.sev]++ : c.none++));
    const vehicles = new Set(items.map((it) => it.vehicleId)).size;
    const un = unackCount();
    document.getElementById("erollup").innerHTML = `
      <div class="rtile"><span class="lbl">By severity</span><span class="v">${
        (c.emergency ? `<span class="seg" style="color:var(--emergency)">${c.emergency} emg</span>` : "") +
        (c.warning ? `<span class="seg" style="color:var(--warning)">${c.warning} warn</span>` : "") +
        (c.caution ? `<span class="seg" style="color:var(--caution)">${c.caution} caut</span>` : "") +
        `<span class="seg" style="color:var(--info)">${c.info} info</span>`
      }</span><span class="sub">${c.none ? c.none + " unspecified" : "all levels tagged"}</span></div>
      <div class="rtile"><span class="lbl">Unacknowledged</span><span class="v" style="color:${un ? "var(--warn)" : "var(--muted)"}">${un}</span><span class="sub">caution &amp; above</span></div>
      <div class="rtile"><span class="lbl">Total events</span><span class="v">${items.length}</span><span class="sub">currently reported</span></div>
      <div class="rtile"><span class="lbl">Vehicles</span><span class="v">${vehicles}</span><span class="sub">with events</span></div>`;
  }

  function renderTable() {
    const rows = shown();
    const tw = document.getElementById("etw");
    if (!rows.length) {
      tw.innerHTML = `<div class="empty-state" style="padding:16px 20px">${items.length ? "No events match this filter." : "No events reported by the fleet."}</div>`;
      return;
    }
    tw.innerHTML = Table(columns, rows, { idKey: "key", rowClass: (it) => `sev-${it.sev || "none"}${it.acked ? " acked" : ""}` });
    tw.querySelectorAll(".ev-ack").forEach((b) => (b.onclick = () => { acked.add(b.dataset.key); reflag(); render(); }));
  }

  // keep item.acked flags in sync with the session set (without re-fetching)
  function reflag() { items.forEach((it) => (it.acked = acked.has(it.key))); }

  function render() {
    document.getElementById("ecount").textContent = `${items.length} event${items.length === 1 ? "" : "s"}`;
    renderRollup(); renderFilters(); renderTable();
    updateRibbon({ alertCount: unackCount() });
  }

  function onEvents(data) {
    const list = Array.isArray(data) ? data : [];
    items = list.map(normalize).sort((a, b) => {
      const am = a.time && a.time.ms, bm = b.time && b.time.ms;
      if (am == null && bm == null) return 0;
      if (am == null) return 1;
      if (bm == null) return -1;
      return bm - am;
    });
    render();
  }

  function onFleet(data) {
    const fleet = Array.isArray(data) ? data : [];
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    updateRibbon({ counts: c });
  }

  const stopEvents = api.poll(api.getEvents, 2000, onEvents, () => {});
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopEvents(); stopFleet(); clearInterval(clockId); };
}
