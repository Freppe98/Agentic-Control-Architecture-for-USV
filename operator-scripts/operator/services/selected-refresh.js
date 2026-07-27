// selected-refresh.js — ONE shared controller for keeping the selected USV's situation
// current, so the Map page (and any future consumer) does not reinvent the same timer +
// race-guard + visibility logic. It owns four concerns that were previously easy to get
// subtly wrong per-page:
//
//   1. Immediate refresh on selection — selecting a vehicle fires its lightweight state and
//      (via the mission tracker) its Pixhawk mission read at once, not on the next tick.
//   2. Late-response rejection — every request captures a generation token taken at select
//      time; a response that resolves after the operator has moved to another USV (or back)
//      is dropped, so a slow USV-A reply can never overwrite the USV-B view.
//   3. Overlap prevention — a lightweight/mission request in flight suppresses another of
//      the same kind, so a slow link cannot pile requests up.
//   4. Visibility pausing — while the browser tab is hidden the interval keeps ticking but
//      does no network work; it resumes on the next visible tick.
//
// It is dependency-injected (fetchers, timers, clock, visibility) so it is unit-tested with
// no DOM and no real timers (tests/selected-refresh.test.mjs). The app wires it to
// api.getPixhawkMission + the real interval in Map.js.
import { createMissionRefreshTracker } from "../lib/mission-refresh.js";

export function createSelectedRefresh({
  fetchState,                 // async (id) => lightweight selected-vehicle state (optional)
  fetchMission,               // async (id) => Pixhawk mission read-back
  onState,                    // (id, data) => void
  onMission,                  // (id, mission, changed) => void — changed=false on unreachable
  onError,                    // (kind, id, err) => void — quiet inline error, kind: state|mission
  intervalMs = 2000,
  missionFallbackMs = 20000,
  isHidden = () => (typeof document !== "undefined" ? document.hidden : false),
  now = () => Date.now(),
  setTimer = (fn, ms) => setInterval(fn, ms),
  clearTimer = (t) => clearInterval(t),
  tracker = createMissionRefreshTracker({ fallbackMs: missionFallbackMs }),
} = {}) {
  let activeId = null;
  let token = 0;              // bumped on every select — the generation guard
  // In-flight OWNERSHIP slots. Each is null or { id, token } identifying the request that
  // currently holds it. A global boolean was wrong: select() reset it so a NEW vehicle could
  // fetch immediately, but then the OLD vehicle's request could finish and clear the flag the
  // new request now owned — letting a second overlapping request start. So a request may only
  // clear the slot it originally acquired (same id AND selection token), and the overlap guard
  // only blocks a second request for the SAME owner. A superseded request neither blocks nor
  // clears the newer selection's slot.
  let stateSlot = null;
  let missionSlot = null;
  let timer = null;
  let stopped = false;

  const isStale = (id, myToken) => stopped || myToken !== token || id !== activeId;
  const owns = (slot, id, myToken) => slot != null && slot.id === id && slot.token === myToken;

  async function runState(id, myToken) {
    if (!fetchState) return;
    if (owns(stateSlot, id, myToken)) return;      // overlap guard — same owner already running
    stateSlot = { id, token: myToken };
    try {
      const data = await fetchState(id);
      if (isStale(id, myToken)) return;            // late response from an old selection
      if (onState) onState(id, data);
    } catch (err) {
      if (!isStale(id, myToken) && onError) onError("state", id, err);
    } finally {
      if (owns(stateSlot, id, myToken)) stateSlot = null;  // only clear the slot WE acquired
    }
  }

  async function runMission(id, myToken, reason, revisionSignal) {
    const inFlight = owns(missionSlot, id, myToken);
    const decision = tracker.shouldFetch(id, { reason, revisionSignal, now: now(), inFlight });
    if (!decision.fetch) return decision;
    missionSlot = { id, token: myToken };
    try {
      const mission = await fetchMission(id);
      if (isStale(id, myToken)) return decision;   // dropped — selection moved on
      if (mission && mission.reachable) {
        const meta = tracker.noteFetched(id, mission, now());
        if (reason === "revision") tracker.noteRevisionSignal(id, revisionSignal);
        if (onMission) onMission(id, mission, meta);
      } else {
        // Unreachable/empty: never cache it (keeps the last-known mission) — let the page
        // surface a quiet stale note without wiping the overlay.
        if (onMission) onMission(id, mission, { geometryChanged: false, progressChanged: false });
      }
    } catch (err) {
      if (!isStale(id, myToken) && onError) onError("mission", id, err);
    } finally {
      if (owns(missionSlot, id, myToken)) missionSlot = null;  // only clear the slot WE acquired
    }
    return decision;
  }

  // Select a vehicle: bump the generation token (so any pending request for the old selection
  // is dropped by the guard) and fire an immediate state + mission read. The in-flight slots
  // are NOT reset — a superseded request cannot clear the new selection's slot because the
  // owns() check requires a matching token. Passing null clears the active selection.
  function select(id) {
    activeId = id;
    token += 1;
    if (id == null) return;
    const myToken = token;
    runState(id, myToken);
    runMission(id, myToken, "select");
  }

  // A trigger the PAGE knows about: a mission-writing command completed ("command"), an
  // explicit operator Fetch ("manual"), or a fleet-reported revision change ("revision").
  function refreshMission(id, reason = "manual", { revisionSignal } = {}) {
    if (id == null || id !== activeId) return;
    runMission(id, token, reason, revisionSignal);
  }

  // One interval cycle: lightweight state + a fallback mission check. No-ops while hidden.
  function tick() {
    if (activeId == null || stopped) return;
    if (isHidden()) return;                        // visibility pause
    const myToken = token;
    runState(activeId, myToken);
    runMission(activeId, myToken, "fallback");
  }

  function start() {
    if (timer == null) timer = setTimer(tick, intervalMs);
  }

  function stop() {
    stopped = true;
    if (timer != null) { clearTimer(timer); timer = null; }
  }

  return {
    select, refreshMission, tick, start, stop, tracker,
    // introspection for tests
    _state: () => ({
      activeId, token, stopped,
      stateInFlight: stateSlot != null, missionInFlight: missionSlot != null,
      stateSlot: stateSlot && { ...stateSlot }, missionSlot: missionSlot && { ...missionSlot },
    }),
  };
}
