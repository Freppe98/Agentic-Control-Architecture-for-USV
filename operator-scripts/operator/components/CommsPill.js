// CommsPill(state, {full}) — the comms axis, one implementation everywhere.
// full=false → short chip (CONN/PART/DISC/UNK); full=true → dot + word.
// Never use this for health/faults — that's HealthBadge.
import { cls, commState, SHORT } from "../lib/ui.js";

export function CommsPill(state, { full = false } = {}) {
  const c = cls(state);
  const s = commState(state);
  return full
    ? `<span class="commpill ${c}"><i></i>${s}</span>`
    : `<span class="pill ${c}">${SHORT[s]}</span>`;
}
