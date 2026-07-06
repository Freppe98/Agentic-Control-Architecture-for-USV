// BatteryBar(pct) — one battery visual for the whole app.
// Threshold color; stays vivid even when surrounding telemetry is stale.
// null → "—" (unknown), never a fabricated number.
import { battColor } from "../lib/ui.js";

export function BatteryBar(pct) {
  const col = battColor(pct);
  const w = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return `<span class="batt-inline"><span class="bar"><i style="width:${w}%;background:${col}"></i></span><span class="bpc" style="color:${col}">${pct == null ? "—" : pct + "%"}</span></span>`;
}
