// ThresholdTimeline(t) — visualizes the comms-timing model the backend uses to derive
// comm_state from time-since-last-contact. READ-ONLY: it mirrors the backend's
// compiled-in constants (main.py). t = { stale, partitioned, disconnected } seconds.
// Bands are proportional, so the picture stays correct if the constants change.
// This is the COMMS axis — never health. See DATA_DICTIONARY (Config / Communication).
import { COL } from "../lib/ui.js";

export function ThresholdTimeline({ stale, partitioned, disconnected }) {
  const scale = disconnected * 1.25; // headroom past DISCONNECTED for the open band
  const pct = (s) => Math.max(0, Math.min(100, (s / scale) * 100));
  const seg = (from, to, color, hatch) =>
    `<span class="tl-seg${hatch ? " hatch" : ""}" style="left:${pct(from)}%;width:${pct(to) - pct(from)}%;--c:${color}"></span>`;
  const tick = (s) =>
    `<span class="tl-tick" style="left:${pct(s)}%"><span class="tl-tn mono">${s}s</span></span>`;

  // Ticks live in a sibling overlay, not inside .tl-track: the track needs overflow:hidden
  // to clip the coloured bands to its rounded ends, and that was also clipping every tick
  // mark and its "8s / 15s / 30s" label — the threshold numbers simply never rendered.
  return `
    <div class="tl">
      <div class="tl-trackwrap">
        <div class="tl-track">
          ${seg(0, stale, COL.connected, false)}
          ${seg(stale, partitioned, COL.connected, true)}
          ${seg(partitioned, disconnected, COL.partitioned, false)}
          ${seg(disconnected, scale, COL.disconnected, false)}
        </div>
        <div class="tl-ticks">${tick(stale)}${tick(partitioned)}${tick(disconnected)}</div>
      </div>
      <div class="tl-legend">
        <span><i style="background:${COL.connected}"></i>0–${stale}s · live</span>
        <span><i class="hz" style="color:${COL.connected}"></i>${stale}–${partitioned}s · connected, stale</span>
        <span><i style="background:${COL.partitioned}"></i>${partitioned}–${disconnected}s · partitioned</span>
        <span><i style="background:${COL.disconnected}"></i>≥${disconnected}s · disconnected</span>
      </div>
    </div>`;
}
