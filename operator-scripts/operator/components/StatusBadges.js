// StatusBadges — compact operator-glance strip for the selected vehicle: armed
// state, flight mode, control authority (RC/Operator/Local Agent), comms, mission
// state. Deliberately small pills, not panels (Live Fleet stays operational, not
// diagnostic — subsystem detail lives on the Vehicle page). Every value is a real
// field (telemetry.armed/mode, the dedicated control-authority fetch, comm_state,
// mission_state) or the honest no-telem slot; nothing here is fabricated.
//
// Freshness: armed / mode / authority are operational facts we can only assert while
// the link is CONNECTED. When the vehicle is stale (opsStale), those chips read
// UNKNOWN instead of a possibly-outdated ARMED/DISARMED/mode — never assert an
// operational state we can no longer confirm (comms and health stay independent, so
// battery/last-known position elsewhere are unaffected).
import { CommsPill } from "./CommsPill.js";
import { AuthoritySeg } from "./AuthoritySeg.js";
import { noTelem, opsStale } from "../lib/ui.js";

export function StatusBadges(v, authVal, authOpts = {}) {
  const t = (v && v.telemetry) || {};
  const stale = opsStale(v);
  const unknownChip = (title) =>
    `<span class="vbadge idle" title="${title}"><span class="hd"></span>UNKNOWN</span>`;

  const armed = t.armed;
  const armedChip = stale
    ? unknownChip("Arm state unknown — telemetry is stale (link not current)")
    : armed == null
      ? `<span class="vbadge idle">${noTelem("no telem")}</span>`
      : `<span class="vbadge ${armed ? "warn" : "ok"}"><span class="hd"></span>${armed ? "ARMED" : "DISARMED"}</span>`;

  const modeChip = stale
    ? unknownChip("Mode unknown — telemetry is stale (link not current)")
    : t.mode
      ? `<span class="vbadge accent">${t.mode}</span>`
      : `<span class="vbadge idle">${noTelem("no telem")}</span>`;

  const mission = v.status || (v.mission_data && v.mission_data.mission_state) || v.mission;
  const missionChip = mission && mission !== "UNKNOWN" && mission !== "Unknown"
    ? `<span class="vbadge accent">${mission}</span>`
    : `<span class="vbadge idle">${noTelem("no telem")}</span>`;

  // Authority is likewise only trustworthy while current; when stale, show UNKNOWN
  // rather than the last effective value.
  const authSeg = stale ? AuthoritySeg(null, authOpts) : AuthoritySeg(authVal, authOpts);

  return `<div class="vbadges">
    ${armedChip}
    ${modeChip}
    ${authSeg}
    ${CommsPill(v)}
    ${missionChip}
  </div>`;
}
