// VehicleDock — the left roster used by Map, Vehicle, Autonomy, Pilot.
// Pure render helpers; pages own the scrolling container (#veh-list) and wire
// clicks, so the same rows compose with page-specific panels (mission progress,
// dock footer, etc.) without layout coupling.
import { statusDot, commState, cls, fmtAge } from "../lib/ui.js";
import { CompanionChip } from "./CompanionPanel.js";

const activity = (v) => v.status || v.mission || (v.telemetry && v.telemetry.mode) || "—";

// opts.companions — render the UAV companion chip beside a vehicle that reports an assigned
// companion. Opt-in: only a page that also wires the chip's click (Map) asks for it.
export function vehicleRow(v, selId, opts = {}) {
  const conn = commState(v) === "connected";
  const sub = conn ? String(activity(v)) : `Last contact ${fmtAge(v.last_seen_age_s)}`;
  const batt = v.battery == null ? "—" : v.battery + "%";
  const btc = v.battery != null && v.battery < 20 ? "txt-d" : v.battery != null && v.battery < 40 ? "txt-p" : "";
  // The dock is a fixed-width column, so a long name/task truncates with an ellipsis
  // (theme.css) — the title carries the full value so nothing is actually lost.
  const nm = v.name || "USV-" + v.id;
  return `<div class="vrow ${v.id === selId ? "sel" : ""}" data-id="${v.id}">
    ${statusDot(v)}
    <span class="body"><span class="nm" title="${nm}">${nm}</span><span class="sub ${conn ? "" : "txt-" + cls(v)}" title="${sub}">${sub}</span></span>
    <span class="mid"><span class="bt ${btc}">${batt}</span>${opts.companions ? CompanionChip(v) : ""}</span>
  </div>`;
}

export function vehicleRows(fleet, selId, opts = {}) {
  if (!fleet.length) return `<div class="empty-state" style="padding:10px 12px">No vehicles</div>`;
  return fleet.map((v) => vehicleRow(v, selId, opts)).join("");
}
