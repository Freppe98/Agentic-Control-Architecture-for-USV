// app.js — hash router. Mounts the active page into #app; cleans up the previous.
// The `autonomy` route key is preserved for back-compat (the nav label + page title
// read "Agent"); the Agent page module lives in pages/Agent.js.
import { Map } from "./pages/Map.js";
import { Fleet } from "./pages/Fleet.js";
import { Plan } from "./pages/Plan.js";
import { Vehicle } from "./pages/Vehicle.js";
import { Mission } from "./pages/Mission.js";
import { Agent } from "./pages/Agent.js";
import { Events } from "./pages/Events.js";
import { Config } from "./pages/Config.js";
import { Pilot } from "./pages/Pilot.js";
import { Terminal } from "./pages/Terminal.js";
import { Experiment } from "./pages/Experiment.js";
import { Stub } from "./pages/Stub.js";
import { mountTour, openTour, tourSeen } from "./lib/tour.js";

const routes = { map: Map, fleet: Fleet, plan: Plan, vehicle: Vehicle, mission: Mission, autonomy: Agent, events: Events, experiment: Experiment, config: Config, pilot: Pilot, terminal: Terminal };
const root = document.getElementById("app");
let cleanup = null;

function render() {
  const key = location.hash.replace(/^#\/?/, "") || "map";
  if (cleanup) { try { cleanup(); } catch (e) { /* noop */ } cleanup = null; }
  const page = routes[key];
  cleanup = page ? page(root) : Stub(root, key);
}

// nav rail click / keyboard → route
function navFrom(target) { return target.closest && target.closest(".nav[data-route]"); }
document.addEventListener("click", (e) => { const n = navFrom(e.target); if (n) location.hash = "#/" + n.dataset.route; });
document.addEventListener("keydown", (e) => { if (e.key === "Enter") { const n = navFrom(e.target); if (n) location.hash = "#/" + n.dataset.route; } });

// guide button (bottom of the rail) → the tour overlay, not a route. It lives inside
// #app, which every page rebuilds, so this is delegated like the nav handlers above.
function helpFrom(target) { return target.closest && target.closest("[data-tour-open]"); }
document.addEventListener("click", (e) => { if (helpFrom(e.target)) openTour(0); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Enter" && e.key !== " ") return;
  if (helpFrom(e.target)) { e.preventDefault(); openTour(0); }
});

// Nav-rail tooltips. The rail became a scroll container (13 fixed nav items cannot fit
// a 768px laptop at full size), and a scroll container clips absolutely-positioned
// children on BOTH axes — so the tip is position:fixed and placed from the button's rect
// here. It is clamped to the viewport so a tooltip on the first or last item is never
// half off-screen. Delegated, because every page rebuilds #app.
function placeTip(navEl) {
  const tip = navEl && navEl.querySelector(".tip");
  if (!tip) return;
  const r = navEl.getBoundingClientRect();
  tip.style.left = `${Math.round(r.right + 12)}px`;
  tip.style.top = "0px";                       // measure at a known origin first
  const h = tip.offsetHeight || 22;
  const top = Math.min(Math.max(6, r.top + r.height / 2 - h / 2), window.innerHeight - h - 6);
  tip.style.top = `${Math.round(top)}px`;
}
document.addEventListener("mouseover", (e) => { const n = navFrom(e.target); if (n) placeTip(n); });
document.addEventListener("focusin", (e) => { const n = navFrom(e.target); if (n) placeTip(n); });

window.addEventListener("hashchange", render);
render();

// The overlay lives on <body>, outside #app, so a page re-render never wipes it.
mountTour();
// First run only: auto-open once, then remembered in this browser (lib/tour.js).
// Delayed so the first page has laid out and the spotlight anchors to a real rect.
if (!tourSeen()) setTimeout(() => openTour(0), 700);
