// NavRail(active) — icon navigation in the frozen order, with the divider
// before the dev tools. Emits data-route on each item; app.js delegates clicks.
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
