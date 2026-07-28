// NavRail(active) — icon navigation in the frozen order, with the divider
// before the dev tools. Emits data-route on each item; app.js delegates clicks.
//
// The rail is sized so the frozen NAV list fits without scrolling; the guided-tour
// button lives in the ribbon (components/Ribbon.js) rather than here, so it never
// competes with the navigation items for vertical space.
import { NAV, svgIcon } from "../lib/ui.js";

export function NavRail(active) {
  const items = NAV.map(([key, label]) => {
    if (key === "_sep") return `<div class="sep"></div>`;
    return `<div class="nav${key === active ? " active" : ""}" data-route="${key}" role="button" tabindex="0" aria-label="${label}">
      ${svgIcon(key)}<span class="tip">${label}</span>
    </div>`;
  }).join("");
  return `<nav class="rail">${items}</nav>`;
}
