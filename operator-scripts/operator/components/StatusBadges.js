// StatusBadges — compact operator-glance strip for the selected vehicle: armed
// state, flight mode, control authority (RC/Operator/Local Agent), comms, mission
// state. Deliberately small pills, not panels (Live Fleet stays operational, not
// diagnostic — subsystem detail lives on the Vehicle page). Every value is a real
// field (telemetry.armed/mode, the dedicated control-authority fetch, comm_state,
// mission_state) or the honest no-telem slot; nothing here is fabricated.
import { CommsPill } from "./CommsPill.js";
import { AuthoritySeg } from "./AuthoritySeg.js";
import { noTelem } from "../lib/ui.js";

export function StatusBadges(v, authVal) {
  const t = (v && v.telemetry) || {};

  const armed = t.armed;
  const armedChip = armed == null
    ? `<span class="vbadge idle">${noTelem("no telem")}</span>`
    : `<span class="vbadge ${armed ? "warn" : "ok"}"><span class="hd"></span>${armed ? "ARMED" : "DISARMED"}</span>`;

  const modeChip = t.mode
    ? `<span class="vbadge accent">${t.mode}</span>`
    : `<span class="vbadge idle">${noTelem("no telem")}</span>`;

  const mission = v.status || (v.mission_data && v.mission_data.mission_state) || v.mission;
  const missionChip = mission && mission !== "UNKNOWN" && mission !== "Unknown"
    ? `<span class="vbadge accent">${mission}</span>`
    : `<span class="vbadge idle">${noTelem("no telem")}</span>`;

  return `<div class="vbadges">
    ${armedChip}
    ${modeChip}
    ${AuthoritySeg(authVal)}
    ${CommsPill(v)}
    ${missionChip}
  </div>`;
}
