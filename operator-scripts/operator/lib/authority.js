// authority.js — control-authority state machine shared by the Map and Vehicle pages.
//
// The operator NEVER decides authority from a button press. A request enters a
// PENDING phase and only becomes CONFIRMED when the effective authority reported by
// Scout matches what was requested; if Scout answers with a different/failed result
// it is REJECTED, and if it never confirms within AUTH_TIMEOUT_MS it is TIMEOUT.
// This keeps the displayed authority honest — it always reflects the vehicle/Scout's
// confirmed effective state, not an optimistic assumption.
//
// Effective-authority values (see main.py) — the FINALIZED strict-ownership contract.
// Startup/default authority is OPERATOR.
//   OPERATOR    — the human operator owns supervisory command authority. The station is
//                 read/write: every supported action is enabled subject to its OWN
//                 safety gates (Home interlock, GPS, connectivity). Scout disables the
//                 Local Agent's autonomous writes. Release Control is available.
//   LOCAL_AGENT — autonomy owns authority. The station is READ-ONLY for vehicle-control
//                 and configuration writes: EVERY write action is disabled, with NO
//                 exceptions — SET_HOME and LOITER are deliberately NOT exempt (strict
//                 ownership). Take Control always remains available.
//   RC          — an independent physical override with the HIGHEST priority. Reported
//                 only, never requestable (REQUESTABLE_AUTHORITY excludes it). While RC
//                 holds control, hasControl is false, so software writes stay disabled
//                 and the UI never claims the operator or the agent has effective control.
//
// hasControl is the single write-enable predicate for the whole station, and handoffGate
// below is the single Take/Release predicate — neither is ever re-derived per page.

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
 * The Take Control / Release Control policy — pure, and the ONE place it is authored
 * (Map and Vehicle both render their hand-off buttons from this, so the two surfaces
 * can never drift apart). Separate from `hasControl`, which gates vehicle WRITES:
 * a hand-off is itself never a vehicle write, it is a request to Scout's dedicated
 * authority endpoint and deliberately does not touch the command queue.
 *
 * Contract:
 *   Take Control    — available whenever authority is not already a confirmed OPERATOR.
 *                     This includes LOCAL_AGENT (where it MUST always remain available)
 *                     and RC (the request is honest: it takes effect once RC releases).
 *   Release Control — available only on a confirmed OPERATOR (there is nothing to
 *                     release otherwise).
 * Both are withheld while a request is in flight (`busy`), while the link is not
 * current (`stale` — a hand-off cannot be confirmed over a dead link), and when the
 * vehicle has no authority source at all (`available:false`).
 *
 * @param view   the controller's view() output
 * @param opts   { stale } — operator-side link state, which view() cannot know
 * @returns {{ canTake, canRelease, hasControl, busy }}
 */
export function handoffGate(view, { stale = false } = {}) {
  const av = view || {};
  const busy = av.phase === "pending";
  const hasControl = !stale && av.hasControl === true;
  const blocked = av.available === false || stale || busy;
  return {
    canTake: !blocked && !hasControl,
    canRelease: !blocked && av.value === "OPERATOR",
    hasControl,
    busy,
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
