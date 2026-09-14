// VehicleDock — the left roster used by Map, Vehicle, Autonomy, Pilot.
// Pure render helpers; pages own the scrolling container (#veh-list) and wire
// clicks, so the same rows compose with page-specific panels (mission progress,
// dock footer, etc.) without layout coupling.
import { statusDot, commState, cls, fmtAge } from "../lib/ui.js";
import { CompanionTab } from "./CompanionPanel.js";

const activity = (v) => v.status || v.mission || (v.telemetry && v.telemetry.mode) || "—";

// opts.companions — render the UAV companion tab at the row's right edge. Opt-in: only a page
// that also wires the tab's click (Map) asks for it; every other consumer of this row is
// unaffected — no tab, no reserved space for one.
// opts.companionHistory — the Map from lib/companion.js's updateLinkHistory(), forwarded
// unchanged to companionTab() so a companion previously seen CONNECTED can read LOST as red
// rather than grey. Safe to omit (reads as "no local evidence yet").
// opts.companionOpenId — the vehicle id whose companion card is currently open, so its tab can
// show a pressed state; never drives colour, only the `is-open` affordance.
export function vehicleRow(v, selId, opts = {}) {
  const conn = commState(v) === "connected";
  const sub = conn ? String(activity(v)) : `Last contact ${fmtAge(v.last_seen_age_s)}`;
  const batt = v.battery == null ? "—" : v.battery + "%";
  const btc = v.battery != null && v.battery < 20 ? "txt-d" : v.battery != null && v.battery < 40 ? "txt-p" : "";
  // The dock is a fixed-width column, so a long name/task truncates with an ellipsis
  // (theme.css) — the title carries the full value so nothing is actually lost.
  const nm = v.name || "USV-" + v.id;
  // Battery lives beside the activity/last-contact text now (left-hand info group), not at the
  // row's far right — the right edge is reserved for the companion tab alone. The combined
  // string is also the row's title (tooltip), since the activity text alone used to be it.
  return `<div class="vrow ${v.id === selId ? "sel" : ""}" data-id="${v.id}">
    ${statusDot(v)}
    <span class="body"><span class="nm" title="${nm}">${nm}</span>
      <span class="sub ${conn ? "" : "txt-" + cls(v)}" title="${sub} · ${batt}">${sub} · <span class="vb ${btc}">${batt}</span></span></span>
    ${opts.companions ? `<span class="vrow-cmp">${CompanionTab(v, opts.companionHistory, v.id === opts.companionOpenId)}</span>` : ""}
  </div>`;
}

export function vehicleRows(fleet, selId, opts = {}) {
  if (!fleet.length) return `<div class="empty-state" style="padding:10px 12px">No vehicles</div>`;
  return fleet.map((v) => vehicleRow(v, selId, opts)).join("");
}
