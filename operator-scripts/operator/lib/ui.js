// ui.js — shared constants & tiny helpers used across components/pages.
// The comms axis and health axis colors mirror variables.css (kept in sync there).

export const COL = {
  connected: "#3ECF8E", partitioned: "#F2A93B", disconnected: "#E5484D", unknown: "#5B6673",
};

/** normalize any comms value → 'connected'|'partitioned'|'disconnected'|'unknown' */
export function commState(v) {
  const s = String((v && (v.comm_state ?? v.comms)) ?? v ?? "unknown").toLowerCase();
  return ["connected", "partitioned", "disconnected", "unknown"].includes(s) ? s : "unknown";
}
export const cls = (s) => ({ connected: "c", partitioned: "p", disconnected: "d", unknown: "u" }[commState(s)]);
export const SHORT = { connected: "CONN", partitioned: "PART", disconnected: "DISC", unknown: "UNK" };

export function battColor(b) {
  if (b == null) return "var(--muted)";
  return b < 20 ? "var(--disconnected)" : b < 40 ? "var(--partitioned)" : "var(--connected)";
}

export const fmtAge = (s) => (s == null ? "—" : `${Math.round(s)}s`);

/** Operational state (armed / mode / effective authority) is only trustworthy while
 *  the link is current. Anything not CONNECTED means the last reading may no longer
 *  hold, so callers must render UNKNOWN rather than a stale ARMED/DISARMED/mode —
 *  never assert an operational fact we can no longer confirm. */
export const opsStale = (v) => commState(v) !== "connected";
export const pad3 = (n) => String(n ?? 0).padStart(3, "0");

/** build an element from an HTML string */
export const el = (html) => {
  const t = document.createElement("template");
  t.innerHTML = String(html).trim();
  return t.content.firstElementChild;
};

/** small shared render primitives */
export const statusDot = (state) => `<span class="statdot" style="background:${COL[commState(state)]}"></span>`;
export const bar = (pct, color = "var(--accent)") =>
  `<span class="bar" style="display:block"><i style="width:${pct == null ? 0 : pct}%;background:${color}"></i></span>`;
export const noTelem = (label = "not reported") =>
  `<span class="no-telem-val">—<span class="no-telem-tag">${label}</span></span>`;

/** Event severity model — the four levels in DATA_DICTIONARY (INFO|CAUTION|WARNING|EMERGENCY).
 *  token maps to a CSS var in variables.css; rank drives triage ordering + the bell threshold. */
export const SEV = {
  emergency: { rank: 4, label: "EMERGENCY", token: "emergency" },
  warning:   { rank: 3, label: "WARNING",   token: "warning" },
  caution:   { rank: 2, label: "CAUTION",   token: "caution" },
  info:      { rank: 1, label: "INFO",      token: "info" },
};

/** Normalize an event's severity from whatever field the agent used, or null when the
 *  event carries no level at all — we tag that UNSPEC rather than inventing a severity. */
export function evSeverity(e) {
  const raw = String((e && (e.severity ?? e.level ?? e.priority ?? e.sev)) ?? "").toLowerCase();
  if (!raw) return null;
  if (raw.startsWith("emerg") || raw === "critical" || raw === "fatal") return "emergency";
  if (raw.startsWith("warn")) return "warning";
  if (raw.startsWith("caut") || raw === "alert" || raw === "major") return "caution";
  if (raw.startsWith("info") || raw === "notice" || raw === "debug" || raw === "minor") return "info";
  return null;
}

/** Human title for an event (mirrors the classic dashboard's field precedence). */
export function evText(e) {
  if (e == null) return "";
  if (typeof e === "string") return e;
  if (Array.isArray(e)) return e.join(" • ");
  return String(e.title ?? e.message ?? e.text ?? e.event ?? e.name ?? e.action ?? JSON.stringify(e));
}

/** Parse an event timestamp → { ms, label } or null when absent/unparseable. */
export function evTime(e) {
  const raw = e && (e.timestamp ?? e.time ?? e.ts ?? e.created_at ?? e.createdAt ?? e.date);
  if (raw == null || raw === "") return null;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return { ms: null, label: String(raw) };
  return { ms: d.getTime(), label: d.toLocaleTimeString([], { hour12: false }) };
}

/** Frozen navigation model (order + labels + icons). Keys are router routes. */
export const NAV = [
  ["map", "Map"], ["fleet", "Fleet"], ["mission", "Mission"], ["autonomy", "Agent"],
  ["video", "Video"], ["pilot", "Pilot"], ["vehicle", "Vehicle"], ["events", "Events"],
  ["experiment", "Experiment"], ["config", "Configuration"], ["_sep", ""],
  ["terminal", "Terminal"], ["messages", "Messages"],
];

export const ICON = {
  map: '<path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Z"/><path d="M9 4v14M15 6v14"/>',
  fleet: '<path d="M3 14h18l-2 5H5l-2-5Z"/><path d="M6 14V6l6-3 6 3v8"/><path d="M12 3v11"/>',
  mission: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="3"/><path d="M12 1v3M12 20v3M1 12h3M20 12h3"/>',
  autonomy: '<circle cx="6" cy="12" r="2.4"/><circle cx="18" cy="6" r="2.4"/><circle cx="18" cy="18" r="2.4"/><path d="M8.4 12h3.6a2 2 0 0 0 2-2l1.6-2M8.4 12h3.6a2 2 0 0 1 2 2l1.6 2"/>',
  video: '<rect x="2" y="6" width="13" height="12" rx="2"/><path d="M15 10l6-3v10l-6-3"/>',
  pilot: '<rect x="2" y="7" width="20" height="10" rx="4"/><circle cx="8" cy="12" r="1.6"/><circle cx="16" cy="12" r="1.6"/>',
  vehicle: '<path d="M3 13h18l-2 6H5l-2-6Z"/><path d="M7 13V7h6l3 6"/><path d="M12 7V4"/>',
  events: '<path d="M4 6h16M4 12h16M4 18h10"/>',
  config: '<circle cx="12" cy="12" r="3.2"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
  experiment: '<path d="M9.5 3h5M10.5 3v6.2L5.4 18a1.7 1.7 0 0 0 1.5 2.6h10.2A1.7 1.7 0 0 0 18.6 18l-5.1-8.8V3"/><path d="M7.8 14.5h8.4"/>',
  terminal: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  messages: '<path d="M4 5h16v11H9l-4 3V5Z"/>',
};

export function svgIcon(key, stroke = 1.7) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round">${ICON[key] || ""}</svg>`;
}
