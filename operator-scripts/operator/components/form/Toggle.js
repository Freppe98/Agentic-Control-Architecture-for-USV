// Toggle(opts) — labeled on/off switch for the config form. Pure HTML string; the
// page flips it via delegation on [data-pref]. One implementation of a boolean
// control across the station.
// opts: { id, label, value, hint?, disabled? }
export function Toggle({ id, label, value, hint = "", disabled = false }) {
  return `<div class="fld">
    <div class="fld-l"><span class="fld-lbl">${label}</span>${hint ? `<span class="fld-hint">${hint}</span>` : ""}</div>
    <div class="fld-c"><button class="tgl${value ? " on" : ""}" role="switch" aria-checked="${value ? "true" : "false"}" data-pref="${id}"${disabled ? " disabled" : ""}><span class="knob"></span></button></div>
  </div>`;
}
