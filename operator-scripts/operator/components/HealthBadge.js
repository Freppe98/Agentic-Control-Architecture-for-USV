// HealthBadge — the health/fault axis. Round severity dot + NAMED condition
// (never "Caution" alone). Kept separate from CommsPill. Health is DERIVED from
// real backend inputs (leak_detected, battery); returns null when no signal exists
// so callers render NO-TELEM instead of implying "OK".
import { noTelem } from "../lib/ui.js";

/** derive {sev, cond} from a normalized vehicle, or null if no health signal */
export function deriveHealth(v) {
  const h = v.health || {};
  if (h.leak_detected === true) return { sev: "warn", cond: "Leak detected" };
  if (v.battery != null && v.battery < 20) return { sev: "warn", cond: "Battery critical" };
  if (v.battery != null && v.battery < 40) return { sev: "caution", cond: "Battery low" };
  if (v.battery != null || Object.keys(h).length) return { sev: "ok", cond: "OK" };
  return null; // no health inputs at all
}

export function HealthBadge(v) {
  const hd = deriveHealth(v);
  if (!hd) return noTelem("no telem");
  return `<span class="hb ${hd.sev}"><span class="hd"></span>${hd.cond}</span>`;
}

export const healthRank = { ok: 0, caution: 1, warn: 2 };
