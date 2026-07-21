// app.js — hash router. Mounts the active page into #app; cleans up the previous.
// The `autonomy` route key is preserved for back-compat (the nav label + page title
// read "Agent"); the Agent page module lives in pages/Agent.js.
import { Map } from "./pages/Map.js";
import { Fleet } from "./pages/Fleet.js";
import { Vehicle } from "./pages/Vehicle.js";
import { Mission } from "./pages/Mission.js";
import { Agent } from "./pages/Agent.js";
import { Events } from "./pages/Events.js";
import { Config } from "./pages/Config.js";
import { Pilot } from "./pages/Pilot.js";
import { Terminal } from "./pages/Terminal.js";
import { Experiment } from "./pages/Experiment.js";
import { Stub } from "./pages/Stub.js";

const routes = { map: Map, fleet: Fleet, vehicle: Vehicle, mission: Mission, autonomy: Agent, events: Events, experiment: Experiment, config: Config, pilot: Pilot, terminal: Terminal };
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
