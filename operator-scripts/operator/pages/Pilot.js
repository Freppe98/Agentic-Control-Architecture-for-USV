// Pilot.js — operational/dev bridge to the vehicle-local dashboard. This page does
// NOT implement teleoperation or any pilot control of its own; it embeds the web UI
// that the vehicle already serves (e.g. Scout at http://10.0.2.10:8080/) so an
// operator can reach vehicle-local tooling from inside the station. All interaction
// happens against the vehicle's own dashboard, not the operator backend — so we make
// no claim about live telemetry here beyond the ribbon/roster, which come from the
// backend as usual.
//
// Honesty note: the dashboard is fetched directly by the browser, cross-origin. We
// cannot read its contents (same-origin policy), and a server that sets
// X-Frame-Options / frame-ancestors can silently refuse to render inside the frame
// while the browser still fires `load`. So we never assert "the dashboard is up" —
// we report only what we can observe (a load event / no response) and always keep an
// Open-in-new-tab fallback.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { commState } from "../lib/ui.js";
import { canonicalVehicleId } from "../lib/selection.js";

// Small local vehicle→dashboard map (per the task: no Configuration API). Keyed by the
// CANONICAL vehicle id used everywhere else (vehicle registry / fleet status). Only vehicles
// with a real, reachable local dashboard belong here — we do not fabricate one for
// hulls that don't serve a web UI.
const DASHBOARDS = {
  2: "http://10.0.2.10:8080/", // Scout — vehicle-local web dashboard
};

const LOAD_TIMEOUT_MS = 8000;

export function Pilot(root) {
  const ids = Object.keys(DASHBOARDS).map(Number);
  let selId = ids[0] ?? null;
  let fleet = [];
  let loaded = false;
  let timeoutId = null;

  const nameOf = (id) => {
    const v = fleet.find((x) => x.id === id);
    return (v && v.name) || "USV-" + id;
  };
  const urlOf = (id) => DASHBOARDS[id];

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("pilot") +
    `<div class="page">
       <div class="toolbar">
         <h1>Pilot</h1>
         <span class="count">Embedded vehicle-local dashboard</span>
       </div>
       <div class="pilot-bar">
         <div class="pilot-target">
           <span class="lbl">Vehicle</span>
           <div class="pilot-veh" id="pilot-veh"></div>
           <a class="pilot-url mono" id="pilot-url" target="_blank" rel="noopener"></a>
         </div>
         <div class="pilot-status" id="pilot-status"></div>
         <div class="pilot-actions">
           <button class="pilot-btn" id="pilot-reload" title="Reload the embedded dashboard">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/></svg>
             Reload
           </button>
           <button class="pilot-btn" id="pilot-open" title="Open the dashboard in a new browser tab">
             <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
             Open in new tab
           </button>
         </div>
       </div>
       <div class="pilot-stage" id="pilot-stage"></div>
     </div>`;

  const stage = document.getElementById("pilot-stage");
  const statusEl = document.getElementById("pilot-status");
  const urlEl = document.getElementById("pilot-url");

  function setStatus(state) {
    const map = {
      connecting: ["u", "Connecting to dashboard…"],
      loaded: ["c", "Dashboard responded — interact below. If it stays blank, open in a new tab."],
      timeout: ["p", "No response yet — the dashboard may be unreachable or blocking embedding. Use Open in new tab."],
    };
    const [dot, text] = map[state] || map.connecting;
    statusEl.innerHTML = `<span class="statdot" style="background:var(--${{ c: "connected", p: "partitioned", d: "disconnected", u: "unknown" }[dot]})"></span><span>${text}</span>`;
  }

  function mountFrame() {
    const url = urlOf(selId);
    if (!url) {
      stage.innerHTML = `<div class="pilot-empty">No local dashboard is configured for this vehicle.</div>`;
      setStatus("timeout");
      return;
    }
    loaded = false;
    setStatus("connecting");
    // Rebuild the iframe so a reload is a genuine fresh navigation. No sandbox: this is
    // a trusted vehicle-local UI and full interaction must be preserved.
    stage.innerHTML = `<iframe class="pilot-frame" id="pilot-frame" src="${url}" title="${nameOf(selId)} dashboard" referrerpolicy="no-referrer"></iframe>`;
    const frame = document.getElementById("pilot-frame");
    frame.addEventListener("load", () => { loaded = true; setStatus("loaded"); });
    if (timeoutId) clearTimeout(timeoutId);
    timeoutId = setTimeout(() => { if (!loaded) setStatus("timeout"); }, LOAD_TIMEOUT_MS);
  }

  function renderTarget() {
    const url = urlOf(selId) || "";
    urlEl.textContent = url;
    urlEl.href = url || "#";
    const veh = document.getElementById("pilot-veh");
    if (ids.length <= 1) {
      veh.innerHTML = `<span class="pilot-vname">${nameOf(selId)}</span>`;
    } else {
      veh.innerHTML = ids.map((id) =>
        `<button class="pilot-chip${id === selId ? " on" : ""}" data-id="${id}">${nameOf(id)}</button>`
      ).join("");
      veh.querySelectorAll(".pilot-chip").forEach((b) => (b.onclick = () => {
        selId = canonicalVehicleId(b.dataset.id); renderTarget(); mountFrame();
      }));
    }
  }

  document.getElementById("pilot-reload").onclick = () => mountFrame();
  document.getElementById("pilot-open").onclick = () => {
    const url = urlOf(selId);
    if (url) window.open(url, "_blank", "noopener");
  };

  // Poll the fleet like the other pages: keeps the ribbon comms counts live and lets
  // the roster/target show the vehicle's real backend name instead of a hardcode.
  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    updateRibbon({ counts: c });
    renderTarget(); // refresh display names once fleet is known
  }

  renderTarget();
  mountFrame();

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() {
    stopFleet();
    clearInterval(clockId);
    if (timeoutId) clearTimeout(timeoutId);
  };
}
