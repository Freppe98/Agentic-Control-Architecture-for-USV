// AuthoritySeg — compact 3-segment control-authority indicator: RC · Operator ·
// Local Agent, backed by the effective authority from GET /api/control_authority/{id}
// ("OPERATOR" | "LOCAL_AGENT" | "RC" | null). See main.py and lib/authority.js.
//
//   OPERATOR    → Operator segment lit (operator holds the wheel).
//   LOCAL_AGENT → Local Agent segment lit (autonomy holds the wheel).
//   RC          → RC segment lit ACTIVE — a real RC transmitter takeover, distinct
//                 from RC's baseline "ready" (override always available as a hardware
//                 fallback, but not currently seizing control).
//
// A second arg carries the request phase from the authority controller so a hand-off
// in flight reads as "pending" rather than silently showing the old value as settled.
export function AuthoritySeg(authVal, opts = {}) {
  const v = authVal == null ? null : String(authVal).toUpperCase();
  const op = v === "OPERATOR";
  const la = v === "LOCAL_AGENT";
  const rc = v === "RC";
  const unknown = !op && !la && !rc;
  const phase = opts.phase || null;               // 'pending'|'confirmed'|'rejected'|'timeout'
  const pendingTo = opts.pending && opts.pending.requested;
  const busy = phase === "pending";

  const rcCls = rc ? " on" : " ready";             // active takeover vs always-available
  const title = `Control authority: ${op ? "Operator" : la ? "Local Agent" : rc ? "RC override active" : "Unknown"}`
    + (busy ? ` · requesting ${pendingTo === "OPERATOR" ? "Operator" : "Local Agent"}…` : "")
    + " · RC always retains hardware override";

  return `<span class="authseg${unknown ? " unk" : ""}${busy ? " pending" : ""}" title="${title}">
    <span class="aseg${rcCls}">RC</span>
    <span class="aseg${op ? " on" : ""}${busy && pendingTo === "OPERATOR" ? " req" : ""}">Operator</span>
    <span class="aseg${la ? " on" : ""}${busy && pendingTo === "LOCAL_AGENT" ? " req" : ""}">Local Agent</span>
  </span>`;
}
