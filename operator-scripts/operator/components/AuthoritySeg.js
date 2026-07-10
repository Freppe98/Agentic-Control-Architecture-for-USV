// AuthoritySeg — compact 3-segment control-authority indicator: RC · Operator ·
// Local Agent. Backed by a 2-value enum (GET /api/control_authority/{id} returns
// "OPERATOR"|"LOCAL_AGENT", see SYSTEM_INFORMATION_MODEL.md "Control authority"),
// so only the Operator/Local Agent segments toggle from live data. RC is rendered
// "ready" on every render, not from a telemetry field — there is no per-vehicle RC
// hardware-presence signal in the schema (a real one would be a Diagnostics/RC
// receiver check, not this widget). It reflects a stated architecture invariant
// instead: RC retains hardware-level override regardless of software authority
// (SYSTEM_INFORMATION_MODEL.md, commands.md "Control authority"). That is a design
// guarantee, not a live reading — never treat it as a stand-in for real telemetry.
export function AuthoritySeg(authVal) {
  const op = authVal === "OPERATOR";
  const la = authVal === "LOCAL_AGENT";
  const unknown = !op && !la;
  return `<span class="authseg${unknown ? " unk" : ""}" title="Control authority: ${op ? "Operator" : la ? "Local Agent" : "Unknown"} · RC always retains override">
    <span class="aseg ready">RC</span>
    <span class="aseg${op ? " on" : ""}">Operator</span>
    <span class="aseg${la ? " on" : ""}">Local Agent</span>
  </span>`;
}
