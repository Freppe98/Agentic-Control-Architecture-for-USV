// Select(opts) — labeled dropdown for the config form. Pure HTML string; the page
// wires change events via delegation on [data-pref]. Kept generic so it is the one
// implementation of a labeled select across the station.
// opts: { id, label, value, options:[[val,label],…], hint?, disabled? }
export function Select({ id, label, value, options, hint = "", disabled = false }) {
  const opts = options
    .map(([v, l]) => `<option value="${v}"${v === value ? " selected" : ""}>${l}</option>`)
    .join("");
  return `<div class="fld">
    <div class="fld-l"><span class="fld-lbl">${label}</span>${hint ? `<span class="fld-hint">${hint}</span>` : ""}</div>
    <div class="fld-c"><select class="sel" data-pref="${id}"${disabled ? " disabled" : ""}>${opts}</select></div>
  </div>`;
}
