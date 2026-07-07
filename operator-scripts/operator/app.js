// app.js — hash router. Mounts the active page into #app; cleans up the previous.
// Only Map is migrated so far; every other route renders the Stub (app stays runnable).
import { Map } from "./pages/Map.js";
import { Fleet } from "./pages/Fleet.js";
import { Vehicle } from "./pages/Vehicle.js";
import { Mission } from "./pages/Mission.js";
import { Autonomy } from "./pages/Autonomy.js";
import { Events } from "./pages/Events.js";
import { Config } from "./pages/Config.js";
import { Stub } from "./pages/Stub.js";

const routes = { map: Map, fleet: Fleet, vehicle: Vehicle, mission: Mission, autonomy: Autonomy, events: Events, config: Config };
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

window.addEventListener("hashchange", render);
render();
