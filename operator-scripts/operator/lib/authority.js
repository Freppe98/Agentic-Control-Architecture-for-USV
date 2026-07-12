// authority.js — control-authority state machine shared by the Map and Vehicle pages.
//
// The operator NEVER decides authority from a button press. A request enters a
// PENDING phase and only becomes CONFIRMED when the effective authority reported by
// Scout matches what was requested; if Scout answers with a different/failed result
// it is REJECTED, and if it never confirms within AUTH_TIMEOUT_MS it is TIMEOUT.
// This keeps the displayed authority honest — it always reflects the vehicle/Scout's
// confirmed effective state, not an optimistic assumption.
//
// Effective-authority values (see main.py):
//   OPERATOR    — operator holds the wheel (Take Control); operator commands allowed.
//   LOCAL_AGENT — autonomy holds the wheel (Release Control).
//   RC          — RC transmitter override active (reported only, never requestable).

export const AUTH_TIMEOUT_MS = 8000;    // no effective confirmation within this → TIMEOUT
export const AUTH_CONFIRM_HOLD_MS = 2500;  // how long a CONFIRMED flash lingers before settling
export const AUTH_REJECT_HOLD_MS = 6000;   // how long a REJECTED/TIMEOUT notice lingers

/** Normalize a GET/POST authority body (main.py schema) into a stable shape. */
export function normAuthority(raw) {
  if (!raw || typeof raw !== "object") {
    return { available: false, reachable: false, value: null, reason: null };
  }
  const v = raw.authority == null ? null : String(raw.authority).toUpperCase();
  return {
    available: raw.available !== false,
    reachable: raw.reachable === true,
    value: v === "OPERATOR" || v === "LOCAL_AGENT" || v === "RC" ? v : null,
    reason: raw.reason || raw.message || raw.error || null,
  };
}

/**
 * A per-vehicle authority controller. `onChange` is invoked whenever the derived view
 * changes (server update, phase transition, timer) so the page can re-render.
 */
export function createAuthorityController(onChange) {
  let server = null;     // last normalized GET result (the confirmed effective state)
  let pending = null;    // { requested, at, phase: 'pending'|'confirmed'|'rejected'|'timeout', reason }
  let timer = null;

  const emit = () => { try { onChange && onChange(); } catch (e) { /* noop */ } };
  const clearTimer = () => { if (timer) { clearTimeout(timer); timer = null; } };

  // Feed a live GET result. If a request is pending and the effective authority now
  // matches what was requested, it is CONFIRMED (by the vehicle/Scout, not the click).
  function setServer(raw) {
    server = normAuthority(raw);
    if (pending && pending.phase === "pending" && server.value === pending.requested) {
      pending = { ...pending, phase: "confirmed" };
      clearTimer();
      timer = setTimeout(() => { pending = null; timer = null; emit(); }, AUTH_CONFIRM_HOLD_MS);
    }
    emit();
  }

  // Issue a hand-off request. Enters PENDING immediately; resolves via setServer (poll
  // or the POST's own echoed effective value), an explicit rejection, or the timeout.
  async function request(target, postFn) {
    if (target !== "OPERATOR" && target !== "LOCAL_AGENT") return { ok: false };
    pending = { requested: target, at: Date.now(), phase: "pending", reason: null };
    clearTimer();
    timer = setTimeout(() => {
      if (pending && pending.phase === "pending") {
        pending = { ...pending, phase: "timeout",
          reason: "No effective-authority confirmation from the vehicle." };
        clearTimer();
        timer = setTimeout(() => { pending = null; timer = null; emit(); }, AUTH_REJECT_HOLD_MS);
        emit();
      }
    }, AUTH_TIMEOUT_MS);
    emit();

    let res;
    try { res = await postFn(target); } catch (e) { res = { ok: false }; }

    if (!res || !res.ok) {
      const reason = (res && res.data && (res.data.message || res.data.error))
        || "The authority change was not accepted.";
      pending = { requested: target, at: Date.now(), phase: "rejected", reason };
      clearTimer();
      timer = setTimeout(() => { pending = null; timer = null; emit(); }, AUTH_REJECT_HOLD_MS);
      emit();
      return res;
    }
    // POST accepted — Scout echoes the acknowledged effective value; confirm against it.
    if (res.data && res.data.authority != null) setServer(res.data);
    return res;
  }

  function reset() { server = null; pending = null; clearTimer(); }
  function dispose() { clearTimer(); }

  // Derived view the pages render from.
  function view() {
    const value = server ? server.value : null;
    const available = server ? server.available : true;
    const reachable = server ? server.reachable : false;
    const busy = !!(pending && pending.phase === "pending");
    return {
      value,                       // confirmed effective authority (or null = unknown)
      available,                   // vehicle has an authority source at all
      reachable,                   // we reached that source on the last read
      pending,                     // { requested, phase, reason } or null
      phase: pending ? pending.phase : (value ? "settled" : "unknown"),
      // Operator commands are enabled ONLY on a confirmed OPERATOR authority, and are
      // withheld while any request is in flight (conservative: no optimistic enable).
      hasControl: value === "OPERATOR" && !busy,
      reason: server ? server.reason : null,
    };
  }

  return { setServer, request, reset, dispose, view };
}
