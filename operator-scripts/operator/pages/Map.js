// Map.js — first migrated page. Full-bleed Leaflet map + live fleet from api.js,
// left dock (roster + mission progress), right inspector. Reuses shared components.
// IA first: markers/roster/inspector wired to real data; NO-TELEM slots where the
// backend can't back the mockup (comms timeline, mission ETA/remaining).
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
// StatusBadges carries the AuthoritySeg — and it is the ONLY place the Map displays control
// authority. The inspector used to repeat it in the readiness title, a Control Owner card, the
// Manual Control card and the Agent Mission grid; four readouts of one fact is not four times
// the clarity. AuthoritySeg is deliberately NOT imported here any more.
import { StatusBadges } from "../components/StatusBadges.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { COL, cls, commState, fmtAge, pad3, noTelem, opsStale } from "../lib/ui.js";
import { createAuthorityController, handoffGate } from "../lib/authority.js";
import { AVAIL, availSlot } from "../lib/availability.js";
import { homeStatus, commandGate, commandGateCtx, deploymentReadiness, fmtDistance, fmtAgo, isSafetyHold, SAFETY_HOLD_TITLE, setHomeOutcome } from "../lib/home.js";
import { commandVerification, hasPendingOfType, commandStages } from "../lib/command.js";
import { classifyMissionWaypoints, missionCounts, remainingRouteDistanceM, etaSeconds, fmtDuration } from "../lib/mission.js";
import { canonicalVehicleId, getSelectedVehicleId, setSelectedVehicleId } from "../lib/selection.js";
import { createSelectedRefresh } from "../services/selected-refresh.js";
import { MISSION_WRITE_COMMANDS, missionWriteNeedsRefetch } from "../lib/mission-refresh.js";
import { missionRevisionSignal } from "../lib/replan.js";
import { missionShowable, nextVisibility, toggleVisibility, toggleButton } from "../lib/mission-visibility.js";
import { createTelemetryCache } from "../lib/telemetry-cache.js";
import { attachMapLayout } from "../lib/map-layout.js";
import * as mx from "../lib/mission-execution.js";
import { readinessView, preflightNote } from "../lib/mission-readiness.js";
import { readinessLabel, READINESS_STATE as PKG_STATE } from "../lib/mission-publish.js";
import { asText, esc, escAttr } from "../lib/format.js";

const HOME = [56.699893, 13.002148];

// The primary map view carries ONLY the essential operational controls, split into
// two clearly separated cards so the operator never confuses commanding the vehicle
// with supervising the agent:
//   • Vehicle Commands — real ArduRover / Pixhawk modes + the ARM/DISARM safety pair.
//     Completely independent of the local agent. (GUIDED/HOLD live on the Vehicle page;
//     the map keeps the primary workflow short.)
//   • Agent Commands — supervisory MISSION pause/resume (NOT Pixhawk modes).
// Every button goes through the same command pipeline (api.createCommand) and its
// state is reported by the vehicle — never assumed from the click.
const MAP_MODES = [
  ["SET_MODE_AUTO", "AUTO"], ["SET_MODE_MANUAL", "MANUAL"],
  ["SET_MODE_LOITER", "LOITER"], ["RTL", "RTL"],
];
const MAP_SAFETY = [["ARM", "ARM"], ["DISARM", "DISARM"]];
const MAP_VEHICLE = [...MAP_MODES, ...MAP_SAFETY];
// The legacy QUEUED MISSION_PAUSE / MISSION_RESUME commands are NOT here and must not come back.
// They were a SECOND, competing pause/resume that neither records the mission sequence nor
// verifies continuation. Mission Start / Pause / Resume / Stop live in the Agent Mission card
// below, which calls ONE orchestrated operator endpoint per intent — the endpoint that also
// performs and verifies the authority hand-off. The mode buttons above stay: they are explicit
// MANUAL supervisory commands and are never the implementation of a mission lifecycle operation.
const MAP_MISSION = [];
const MAP_VEHICLE_TYPES = new Set(MAP_VEHICLE.map(([t]) => t));
const MAP_MISSION_TYPES = new Set(MAP_MISSION.map(([t]) => t));
const HIGH_RISK = new Set(["ARM", "DISARM", "RTL", "SET_MODE_AUTO"]);
// Per-command confirmation copy for the themed modal (never a generic one-size-fits-
// all sentence for a high-risk command — ARM in particular carries physical risk that
// a mode change does not). Falls back to a generic sentence for anything not listed.
const CMD_CONFIRM_COPY = {
  ARM: (vn) => `<p>Arm <b>${vn}</b>?</p><p><b>The motor will be live and the vehicle may move once armed.</b> Confirm the area is clear of people and obstacles before proceeding.</p><p>The command is queued for the vehicle to execute — it is NOT applied until the local agent reports back.</p>`,
  DISARM: (vn) => `<p>Disarm <b>${vn}</b>?</p><p>The motor will stop. If the vehicle is underway, it will stop responding to propulsion immediately.</p><p>The command is queued for the vehicle to execute — it is NOT applied until the local agent reports back.</p>`,
  RTL: (vn) => `<p>Send <b>${vn}</b> to RTL (Return-to-Launch)?</p><p>The vehicle will autonomously navigate back to its verified Home position.</p><p>The command is queued for the vehicle to execute — it is NOT applied until the local agent reports back.</p>`,
  SET_MODE_AUTO: (vn) => `<p>Switch <b>${vn}</b> to AUTO?</p><p>The vehicle will resume autonomous mission execution.</p><p>The command is queued for the vehicle to execute — it is NOT applied until the local agent reports back.</p>`,
};
const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
// Command lifecycle → the operator-facing phase it represents (requested/pending,
// acknowledged, confirmed effective, rejected, timeout).
const CMD_PHASE = {
  QUEUED: ["requested", "u"], SENT: ["pending", "p"], ACCEPTED: ["acknowledged", "p"],
  EXECUTED: ["confirmed", "c"], REJECTED: ["rejected", "d"], FAILED: ["failed", "d"],
  EXPIRED: ["timed out", "u"],
};
// Normalized terminal outcome → (label, pill tint) for the "Last command" line.
const CMD_OUTCOME = {
  VERIFIED: ["verified", "c"], EXECUTED: ["executed", "c"], FAILED: ["failed", "d"],
  REJECTED: ["rejected", "d"], EXPIRED: ["timed out", "u"],
};
// Compact stage names for the "requested › sent › confirmed" progression line.
const STAGE_SHORT = {
  QUEUED: "requested", SENT: "sent", ACCEPTED: "ack", EXECUTING: "executing",
  EXECUTED: "executed", REJECTED: "rejected", FAILED: "failed", EXPIRED: "expired",
};
const CMD_TERMINAL_M = new Set(["EXECUTED", "REJECTED", "FAILED", "EXPIRED"]);
const CMD_LABEL = Object.fromEntries([...MAP_MODES, ...MAP_SAFETY, ...MAP_MISSION]);
// Distinct recovery/home glyph for the Vehicle Home (Pixhawk HOME_POSITION) marker —
// a home inside a location pin, deliberately unlike the numbered mission waypoints and
// the mission-readback house icon. Colour comes from the verification state (CSS).
const vehHomeSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21s7-6.4 7-11a7 7 0 1 0-14 0c0 4.6 7 11 7 11Z"/><path d="M9.2 10.4 12 8l2.8 2.4V14H9.2z"/></svg>';

export function Map(root) {
  const L = window.L;
  let fleet = [], selId = null, env = null, map = null;
  // Per-USV last-known telemetry merge. Guards against a partial fleet poll (a null numeric
  // field, e.g. a momentarily-absent battery) erasing a valid displayed value and flickering
  // it at the 2 s poll rate. Keyed by USV id so one vehicle never affects another.
  const telemCache = createTelemetryCache();
  let commsHist = null;          // comms-state transition log for the selected vehicle
  let cmds = [];                 // command queue + history for the selected vehicle
  // Control-authority state machine (pending → confirmed/rejected/timeout). Re-renders
  // the inspector on every phase change so the display always reflects the effective
  // authority confirmed by Scout, never the button press.
  const authCtl = createAuthorityController(() => renderInspector());
  // ---- Agent Mission (Scout's mission-execution lifecycle) — the NORMAL operational control ---
  // `status` is Scout's canonical status and is the ONLY thing the buttons are derived from;
  // `preflight` is the backend's Start-precondition verdict (the SAME function the Start
  // transaction enforces, so the card and the gate cannot disagree); `result` is the last
  // transaction's interpreted outcome. `forVid` tags every fetch with the vehicle it was made
  // for, so a reply that lands after the operator switched USVs is discarded rather than shown
  // against the wrong Scout. `busy` is the single-flight guard: while a transaction is in
  // flight every lifecycle button is disabled, so a double press cannot submit twice.
  //
  // THE PREFLIGHT IS NOT POLLED. It used to be, on this same 3 s tick, and that is what made a
  // stable vehicle's card alternate between READY / Start Mission and NOT_READY every few
  // seconds: the backend serves the preflight's Pixhawk read-back evidence through a 10 s cache
  // (main.PIXHAWK_READBACK_TTL_S), so roughly every tenth poll paid for a live MAVLink mission
  // download, and a download that timed out or arrived partial answered can_start:false — with
  // three blockers, because the package hash chain and Scout's replanning readiness are both
  // anchored on the read-back. Not one of those was a fact about the vehicle.
  //
  // So Start availability now comes from STABLE blockers only (mx.startGate), and the preflight
  // runs ONCE at a meaningful moment — vehicle selection, after a mission write, after a
  // lifecycle transaction (which synchronizes the package), on reconnect, or from the card's
  // explicit Refresh — and is shown as INFORMATION. `preflightAt` / `preflightFor` /
  // `preflightReason` are what it was, when, and why it was run; `refreshing` drives a small
  // spinner and NOTHING else. The authoritative proof is the Start transaction's own, which runs
  // fresh and fail-closed before any vehicle write (mission_lifecycle.run_start).
  const mission = {
    status: null, result: null, busy: false, forVid: null,
    preflight: null, preflightAt: null, preflightFor: null, preflightReason: null,
    refreshing: false,
    // The operator-side publication state (active mission, upload status, whether an Agent
    // package sync is owed). Read on the same one-shot moments as the preflight.
    publish: null, publishFor: null, syncing: false,
  };
  // Per-vehicle comm state from the previous fleet poll, so a DISCONNECTED → CONNECTED
  // transition can trigger exactly one preflight (task 8's "after reconnect") without any
  // recurring read.
  const lastCommState = {};
  const markers = {};
  let homeMarker = null;         // dedicated Vehicle Home (HOME_POSITION) marker, selected vehicle
  // Set-Home CLICK FEEDBACK ONLY — never the source of Home's verified/not-verified
  // state (that is always v.home, Scout's own continuously-reported home_status; see
  // lib/home.js homeStatus()). `phase` is idle/pending/confirmed/failed: "confirmed"
  // means the command's own result said Set Home succeeded (main._annotate_set_home_
  // result), a transient toast that self-clears — it does NOT flip the Home chip.
  // `cmdId` is the in-flight command's id, resolved via the SAME command queue poll
  // every other command uses (syncSetHomeFromCommands) — no bespoke transport.
  let setHome = { phase: "idle", code: null, message: null, at: 0, cmdId: null };
  let setHomeTimer = null;
  const timers = [];
  // Pixhawk mission readback — per-vehicle, view-only. Each entry:
  //   { mission, fetchedAt, loading, note }  (see fetchPixhawkMission / renderPxm)
  // `mission` is the last SUCCESSFUL (reachable) Scout readback and is never wiped by a
  // later failed fetch — a subsequent unreachable attempt only sets `note`, so the card
  // keeps showing the last-known mission with an honest "Scout unavailable" status.
  // `shown` toggles the map overlay WITHOUT refetching (toggling never hits Scout).
  const pxm = {};
  let missionLayer = null;   // the single Leaflet layer group currently drawn (selected vehicle)
  // Command types with a POST in flight right now — a synchronous guard against a rapid
  // double-press queuing a duplicate before the queue poll catches up (matters most for
  // LOITER, which has no confirmation modal to slow a double-click). Cleared per type in
  // sendCommand's finally; the queue-derived hasPendingOfType keeps the button disabled
  // afterwards until the outstanding command reaches a terminal state.
  const sending = new Set();
  // Mission-write commands (upload/clear/replan) whose completion we have already reacted
  // to with a mission refetch — so a settled command sitting in history does not re-trigger
  // a download on every command-queue poll. Keyed by command id.
  const missionWriteHandled = new Set();
  // Rate-limited quiet logging for automatic-refresh failures — an auto refresh must never
  // spam the console/operator (task F). At most one log per kind per window.
  const lastQuietLog = {};
  function logQuiet(kind, err) {
    const t = Date.now();
    if (lastQuietLog[kind] && t - lastQuietLog[kind] < 15000) return;
    lastQuietLog[kind] = t;
    console.warn(`[map] auto-refresh ${kind} failed:`, err && err.message ? err.message : err);
  }

  // Apply a Pixhawk mission read-back into the per-vehicle cache. A reachable read replaces
  // the displayed mission; an unreachable one only sets a note so the last-known mission is
  // preserved (never wiped) — the same honesty rule the manual Fetch used.
  function applyMissionRead(id, res) {
    const s = pxmState(id);
    if (res && res.reachable) {
      s.mission = res; s.fetchedAt = Date.now();
      s.note = res.available === false ? "no-api" : (res.partial ? "partial" : null);
    } else {
      s.note = (res && res.available === false) ? "no-api" : "unreachable";
    }
  }

  // ONE shared controller drives the selected vehicle's automatic mission refresh: an
  // immediate read on selection, a slow fallback safety refresh, and the command/revision
  // triggers below. Lightweight live state (position/battery/comms/authority) stays on the
  // existing 2 s fleet + authority/command polls — the full mission is deliberately NOT
  // pulled on every heartbeat. The controller drops any read that resolves after the
  // operator has moved to another USV (token guard), so a late USV-A reply never overwrites
  // the USV-B overlay.
  const refreshController = createSelectedRefresh({
    fetchMission: (id) => api.getPixhawkMission(id),
    onMission: (id, res, meta) => {
      applyMissionRead(id, res);
      // Default-visible: a newly loaded valid mission shows automatically; a successful
      // upload/replan/clear (geometryChanged) re-shows/updates it; an unchanged periodic read
      // never overrides an explicit hide. Progress text (current WP / %) always refreshes.
      applyReadVisibility(id, meta.geometryChanged, meta.geometryChanged || meta.progressChanged);
      if (id === selId) { renderPxm(); renderDock(); }
    },
    onError: (kind, id, err) => {
      if (kind === "mission" && id === selId) { pxmState(id).note = "error"; renderPxm(); }
      logQuiet(kind, err);
    },
    intervalMs: 5000,          // cadence at which the fallback deadline is checked
    missionFallbackMs: 20000,  // full mission re-read at most this often absent a real trigger
  });

  // The mission-revision signal for the SELECTED vehicle, from every authoritative source this
  // page already reads: Scout's mission-execution status (active route hash, replanning FSM) and
  // the fleet payload. NO LONGER DORMANT — when the agent replans a safe return and uploads the
  // revised mission, the active route hash changes and the overlay refetches itself, so the
  // operator sees the real return route without pressing Refresh.
  //
  // The derivation is pure and lives in lib/replan.js (unit-tested); this is only the wiring.
  // Unchanged evidence produces an unchanged signal and therefore no download, and `undefined`
  // (no evidence at all) leaves the trigger dormant rather than firing on a fabricated value.
  function revisionSignalFor(id) {
    const v = fleet.find((x) => x.id === id) || null;
    return missionRevisionSignal({
      vehicle: v,
      missionExecution: mission.forVid === id ? mission.status : null,
    });
  }

  // Ask the refresh controller to reconsider the overlay for a vehicle. The controller itself
  // decides whether anything is downloaded (it compares the signal against the last one seen),
  // so calling this on every status read is cheap and cannot start a download loop.
  function noteRevisionEvidence(id) {
    if (id == null || id !== selId) return;
    const sig = revisionSignalFor(id);
    if (sig !== undefined) refreshController.refreshMission(id, "revision", { revisionSignal: sig });
  }

  root.className = "app has-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("map") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Live</span></div>
       <div class="veh-list" id="veh-list"><div class="empty-state" style="padding:10px 12px">Connecting…</div></div>
       <div class="pxm" id="pxm"></div>
       <div class="mprog" id="mprog"></div>
     </div>
     <div class="map-wrap">
       <div class="map-stage" id="map-stage">
       <div id="map"></div>
       <div class="ov wind" id="wind"><div class="lbl">Wind</div><div class="arrow" id="wind-arrow">➜</div><div class="spd" id="wind-spd">—</div><div class="frm" id="wind-frm"></div></div>
       <div class="ov toast" id="map-toast"></div>
       <div class="ov legend" id="legend">
         <div class="legend-h"><span class="lbl">Legend</span><button class="legend-toggle" id="legend-toggle" title="Collapse legend">–</button></div>
         <div class="legend-body" id="legend-body">
           <div class="li-group">
             <div class="li"><span class="li-dot c"></span>Connected</div>
             <div class="li"><span class="li-dot p"></span>Partitioned — buffering</div>
             <div class="li"><span class="li-dot d"></span>Disconnected</div>
             <div class="li"><span class="li-dot dash"></span>Dashed ring = stale (link not current)</div>
           </div>
           <div class="li-group">
             <div class="li"><span class="li-ic veh"></span>Vehicle position</div>
             <div class="li"><span class="li-ic wp"></span>Upcoming waypoint</div>
             <div class="li"><span class="li-ic wp done"></span>Completed waypoint</div>
             <div class="li"><span class="li-ic wp cur"></span>Current waypoint</div>
           </div>
           <div class="li-group">
             <div class="li"><span class="li-ic mstart"></span>Mission start (Pixhawk seq 0)</div>
             <div class="li"><span class="li-ic vhome"></span>Vehicle Home (RTL point)</div>
           </div>
         </div>
       </div>
       </div>
       <div class="mission-progress-bar" id="mpbar" style="display:none">
         <div class="mpb-fill" id="mpb-fill" style="width:0%"></div>
         <div class="mpb-label" id="mpb-label"></div>
       </div>
     </div>
     <aside class="inspector" id="inspector"></aside>`;
  document.getElementById("legend-toggle").onclick = () => {
    const body = document.getElementById("legend-body"), btn = document.getElementById("legend-toggle");
    const collapsed = body.classList.toggle("collapsed");
    btn.textContent = collapsed ? "+" : "–";
    btn.title = collapsed ? "Expand legend" : "Collapse legend";
  };

  // Leaflet. Zoom is placed TOP-RIGHT (theme.css then drops it below the wind widget via
  // the measured --map-tr-h) so the top-left corner belongs to status/instruction text
  // alone — the two used to be stacked in the same corner. Attribution is on: OSM tiles
  // require it, and it now has an uncontested bottom-right corner to live in.
  // trackResize:false — lib/map-layout.js owns resize for every map (see Plan.js for why
  // Leaflet's own listener is the one that has to go).
  map = L.map("map", { zoomControl: false, attributionControl: true, trackResize: false }).setView(HOME, 16);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.control.scale({ position: "bottomright", imperial: false }).addTo(map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20, attribution: "© OpenStreetMap" }).addTo(map);
  // One shared resize/corner contract for every map in the station (lib/map-layout.js):
  // ResizeObserver on the stage → coalesced invalidateSize, plus the measured top-corner
  // extents that theme.css offsets the Leaflet controls, legend and toast from.
  const detachMapLayout = attachMapLayout(map, document.getElementById("map-stage"), {
    topRight: [document.getElementById("wind")],
  });
  // Wide zoom hides mission waypoint numbers (leaving dots); close zoom restores them.
  // A single container class toggle — the mission Leaflet layers are never recreated.
  map.on("zoomend", applyMissionZoom);

  function makeIcon(v) {
    const st = commState(v), color = COL[st], stale = st !== "connected", sel = v.id === selId;
    // Circular USV dot (comm-colored, id inside) + a heading arrow orbiting above it.
    // Selection is the clean halo ring only. NOTE: the selected class is `is-sel`, not
    // `sel` — a bare `.sel` collides with the form-select style (theme.css) and paints
    // the marker as a large dark rectangle.
    // Stale is never opacity-only (too subtle in bright outdoor light, and invisible to
    // a colour-blind operator relying on the amber/red hue alone): a dashed ring is
    // added around the dot so "this position may no longer be current" reads as a
    // distinct SHAPE, not just a dimmer version of the same shape.
    return L.divIcon({
      className: "",
      html: `<div class="usv-marker${sel ? " is-sel" : ""}${stale ? " is-stale" : ""}" style="opacity:${stale ? 0.88 : 1}">
        ${sel ? '<div class="selring"></div>' : ""}
        ${stale ? `<div class="staledash" style="border-color:${color}"></div>` : ""}
        <div class="heading" style="transform:rotate(${v.heading == null ? 0 : v.heading}deg)"><span class="arw" style="color:${color}">▲</span></div>
        <div class="dot" style="background:${color}"><span>${v.id}</span></div>
        ${stale ? `<div class="age" style="color:${color}">${fmtAge(v.last_seen_age_s)}</div>` : ""}
      </div>`,
      iconSize: [40, 40], iconAnchor: [20, 20],
    });
  }

  function updateMarkers() {
    fleet.forEach((v) => {
      // Never plot a fabricated position: a vehicle that has not reported a valid
      // position (never contacted / no GPS fix) has no marker rather than a fake one.
      if (v.lat == null || v.lng == null) {
        if (markers[v.id]) { map.removeLayer(markers[v.id]); delete markers[v.id]; }
        return;
      }
      const ll = [v.lat, v.lng];
      if (!markers[v.id]) {
        markers[v.id] = L.marker(ll, { icon: makeIcon(v) }).addTo(map).on("click", () => select(v.id));
      } else {
        markers[v.id].setLatLng(ll).setIcon(makeIcon(v));
      }
    });
  }

  function activity(v) { return v.status || v.mission || (v.telemetry && v.telemetry.mode) || "—"; }

  function renderDock() {
    const list = document.getElementById("veh-list");
    list.innerHTML = vehicleRows(fleet, selId);
    list.querySelectorAll(".vrow").forEach((el) => (el.onclick = () => select(canonicalVehicleId(el.dataset.id))));

    // Mission progress for the SELECTED vehicle — real waypoint counts + remaining
    // distance/ETA from the Pixhawk mission readback (lib/mission.js), the same numbers
    // the bottom-of-map progress bar shows. Falls back to the (optional) per-vehicle
    // coverage field when no mission is loaded yet, then to an honest empty state —
    // never a fabricated percentage.
    const ms = selectedMissionStats();
    const cov = fleet.map((v) => v.coverage).find((c) => c != null);
    let body;
    if (ms && ms.pct != null) {
      body = `<div class="top"><span class="lbl">Waypoints</span><span class="pct mono">${ms.pct}%</span></div>
           <div class="bar"><i style="width:${ms.pct}%;background:var(--connected)"></i></div>
           <div class="mgrid">
             <div><span class="lbl">Remaining</span><span class="v">${ms.remaining} / ${ms.total}</span></div>
             <div><span class="lbl">ETA</span><span class="v">${ms.etaS != null ? fmtDuration(ms.etaS) : noTelem("no speed")}</span></div>
           </div>`;
    } else if (cov != null) {
      body = `<div class="top"><span class="lbl">Coverage</span><span class="pct mono">${cov}%</span></div>
           <div class="bar"><i style="width:${cov}%;background:var(--connected)"></i></div>
           <div class="mgrid">
             <div><span class="lbl">Remaining</span><span class="v">${noTelem()}</span></div>
             <div><span class="lbl">ETA</span><span class="v">${noTelem()}</span></div>
           </div>`;
    } else {
      body = `<div class="no-telem-box"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20V10M12 20V4M20 20v-7"/></svg>No mission loaded for the selected vehicle</div>`;
    }
    document.getElementById("mprog").innerHTML = `<div class="row"><span class="lbl">Mission progress</span></div>` + body;
  }

  // ---- Pixhawk mission (view-only readback + map overlay) ------------------
  function pxmState(id) {
    // `shown` = overlay currently drawn; `userHidden` = the operator EXPLICITLY hid this
    // USV's overlay, so the default-visible rule must not force it back on until the mission
    // geometry changes. Both are PER-USV — never a global visibility flag.
    return pxm[id] || (pxm[id] = { mission: null, fetchedAt: 0, loading: false, note: null, shown: false, userHidden: false });
  }

  // Whether the SELECTED/target vehicle's cached mission may be shown as a valid overlay.
  function missionIsShowable(id) {
    const s = pxm[id];
    return missionShowable(s && s.mission, positionedWaypoints(id).length);
  }

  // Recompute per-USV visibility after a fresh read, then reflect it on the map. Never draws
  // an invalid/empty mission; a geometry change re-shows a newly loaded mission by default
  // (unless the operator hid this USV); an unchanged read never overrides an explicit hide.
  function applyReadVisibility(id, geometryChanged, changed) {
    const s = pxmState(id);
    const wasShown = s.shown;
    const showable = missionIsShowable(id);
    const nv = nextVisibility(s, { showable, geometryChanged });
    s.shown = nv.shown; s.userHidden = nv.userHidden;
    if (id !== selId || !map) return;
    if (!s.shown) { clearMissionOverlay(); return; }
    if (!wasShown || changed) drawMissionOverlay(id);   // newly visible, or geometry/progress moved
  }

  // Fetch the mission stored on the vehicle's Pixhawk (a live Scout proxy). A reachable
  // reply replaces the displayed mission; an unreachable one (or a thrown 404) only sets
  // a note so the last-known mission is preserved. Never called by Show/Hide.
  // Manual Fetch — an explicit operator recovery/diagnostic action. Deliberately INDEPENDENT
  // of the automatic controller (it bypasses the overlap guard so it always works, even mid
  // auto-refresh) and shows the loading spinner + a clear error, unlike the quiet auto path.
  // It still updates the shared mission tracker so the fallback timer treats the read as
  // fresh and does not immediately duplicate it.
  async function fetchPixhawkMission(id) {
    if (id == null) return;
    const s = pxmState(id);
    s.loading = true; renderPxm();
    let geometryChanged = false;
    try {
      const res = await api.getPixhawkMission(id);
      applyMissionRead(id, res);
      if (res && res.reachable) { geometryChanged = refreshController.tracker.noteFetched(id, res).geometryChanged; }
    } catch (e) {
      s.note = "error";
    } finally {
      s.loading = false;
      // A manual Fetch of a valid mission shows it by default too (same rule as auto-fetch);
      // force a redraw since the operator explicitly asked for the latest.
      applyReadVisibility(id, geometryChanged, true);
      if (id === selId) { renderPxm(); renderDock(); }
    }
  }

  // ---- Mission geometry & HOME detection ----------------------------------
  // Pixhawk stores sequence 0 as a home / current-location item that is NOT part of
  // the stored survey — see lib/mission.js classifyMissionWaypoints for why, and why
  // it's rendered as a separate marker, kept OUT of the route polyline, and EXCLUDED
  // from the mission-fit bounds so Center frames the survey, not the whole transit.
  // The classification + progress/distance math is shared with Mission.js and
  // Vehicle.js (lib/mission.js) so all three pages report the same numbers for the
  // same vehicle — never three slightly different waypoint counts.
  function classifyMission(id) {
    return classifyMissionWaypoints(positionedWaypoints(id));
  }

  const wpCheckSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round" class="wp-check"><path d="M5 13l4 4L19 7"/></svg>';
  // A numbered survey waypoint — a small, discreet marker that never competes with the
  // round comm-colored vehicle dot. The number lives in its own span so it can be
  // hidden at wide zoom (leaving just the dot). State is never color-only: geometry
  // changes too, so it reads correctly even in bright outdoor light or for a colour-
  // blind operator — remaining = rounded square + number, done = circle + checkmark,
  // current = the same square with a bigger pulsing ring (not a bigger box).
  // stateCls: "cur" (current waypoint), "done" (already passed), or "" (upcoming).
  function wpIcon(w, stateCls) {
    const inner = stateCls === "done" ? wpCheckSvg : `<span class="wp-num">${w.seq == null ? "•" : w.seq}</span>`;
    return L.divIcon({
      className: "",
      html: `<div class="wp-marker${stateCls ? " " + stateCls : ""}">${inner}</div>`,
      iconSize: [20, 20], iconAnchor: [10, 10],
    });
  }
  // ---- Vehicle Home marker (Pixhawk HOME_POSITION / RTL recovery point) -----------
  // A dedicated marker for the deployment home, driven by v.home — SEPARATE from the
  // mission overlay's seq-0 house and from the numbered mission waypoints, and never
  // joined to WP 1 by the route polyline. Colour tracks the verification state, AND —
  // so a first-time operator never has to click/hover to learn it — an always-visible
  // text label sits under the glyph (same convention as the stale-vehicle age chip):
  // clicking is only needed for the distance/verification-time detail in the popup.
  const VEH_HOME_LABEL = { verified: "VERIFIED", unverified: "NOT VERIFIED", pending: "SETTING…", unknown: "UNKNOWN" };
  function vehHomeIcon(state) {
    const label = VEH_HOME_LABEL[state] || "UNKNOWN";
    return L.divIcon({
      className: "",
      html: `<div class="veh-home-marker ${state}">${vehHomeSvg}<div class="veh-home-label">${label}</div></div>`,
      iconSize: [26, 26], iconAnchor: [13, 24],
    });
  }
  function vehHomeTooltip(v, hs) {
    const row = (k, val) => `<div class="wp-pop-row"><span>${k}</span><span>${val}</span></div>`;
    const verTxt = hs.state === "verified"
      ? `Verified${hs.verifiedAgeS != null ? ` · ${fmtAgo(hs.verifiedAgeS)}` : ""}`
      : hs.state === "pending" ? "Setting…" : hs.state === "unknown" ? "Unknown" : "Not verified";
    return `<div class="wp-pop">
      <div class="wp-pop-h">VEHICLE HOME</div>
      ${row("Type", "RTL recovery point")}
      ${row("Latitude", hs.homeLat != null ? hs.homeLat.toFixed(6) : "—")}
      ${row("Longitude", hs.homeLng != null ? hs.homeLng.toFixed(6) : "—")}
      ${row("Verification", verTxt)}
      ${row("Distance from Scout", fmtDistance(hs.distanceM))}
    </div>`;
  }
  function updateHomeMarker() {
    if (!map) return;
    const v = fleet.find((x) => x.id === selId);
    const hs = v ? effectiveHomeStatus(v) : null;
    if (!hs || !hs.available || hs.homeLat == null || hs.homeLng == null) {
      if (homeMarker) { map.removeLayer(homeMarker); homeMarker = null; }
      return;
    }
    const ll = [hs.homeLat, hs.homeLng];
    const icon = vehHomeIcon(hs.state);
    const tip = vehHomeTooltip(v, hs);
    if (!homeMarker) {
      homeMarker = L.marker(ll, { icon, zIndexOffset: -500 }).addTo(map).bindPopup(tip);
    } else {
      homeMarker.setLatLng(ll).setIcon(icon).setPopupContent(tip);
    }
  }

  // NOTE: this is the Pixhawk mission-readback's seq-0 item — the point the mission
  // was recorded near, NOT the RTL recovery point. It is deliberately never called
  // "Home" anywhere in the UI (title/popup/legend) to avoid it being mistaken for the
  // Vehicle Home marker below, which IS the authoritative RTL point — that confusion
  // is a real field-safety risk (see the map legend for both, side by side).
  const homeSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/></svg>';
  function homeIcon(isCurrent) {
    return L.divIcon({
      className: "",
      html: `<div class="home-marker${isCurrent ? " cur" : ""}" title="Mission start (Pixhawk seq 0) — not the RTL recovery point">${homeSvg}</div>`,
      iconSize: [22, 22], iconAnchor: [11, 11],
    });
  }

  // Readable command label: Scout's command_name when present, else the raw command
  // string (e.g. MAV_CMD_NAV_WAYPOINT) with the boilerplate prefix trimmed, else the
  // numeric code. Never renders "null" (the real proxy sends command as a string and
  // leaves command_name null).
  function cmdLabel(w) {
    if (w.command_name) return w.command_name;
    const c = w.command;
    if (c == null || c === "") return "—";
    if (typeof c === "string") return c.replace(/^MAV_CMD_(NAV_|DO_|CONDITION_)?/, "") || c;
    return String(c);
  }
  function frameLabel(f) {
    if (f == null || f === "") return "—";
    return typeof f === "string" ? f.replace(/^MAV_FRAME_/, "") : String(f);
  }

  function wpPopup(w, isCur, isHome) {
    const row = (k, v) => `<div class="wp-pop-row"><span>${k}</span><span>${v}</span></div>`;
    const loiter = w.loiter_time != null ? row("Loiter", `${w.loiter_time} s`) : "";
    return `<div class="wp-pop">
      <div class="wp-pop-h">${isHome ? "MISSION START" : `WP ${w.seq == null ? "—" : w.seq}`}${isCur ? '<span class="wp-cur-tag">CURRENT</span>' : ""}</div>
      ${row("Type", isHome ? "Mission start point (Pixhawk seq 0)" : "Waypoint")}
      ${row("Sequence", w.seq == null ? "—" : w.seq)}
      ${row("Command", cmdLabel(w))}
      ${row("Frame", frameLabel(w.frame))}
      ${row("Latitude", w.lat != null ? (+w.lat).toFixed(6) : "—")}
      ${row("Longitude", w.lng != null ? (+w.lng).toFixed(6) : "—")}
      ${row("Altitude", w.alt != null ? `${w.alt} m` : "—")}
      ${loiter}
    </div>`;
  }

  // Full, idempotent teardown of the overlay: drop the layer group (which removes its
  // markers + polyline) AND close any popup it opened, so repeated vehicle switching
  // can never leave a stray marker or a dangling popup behind.
  function clearMissionOverlay() {
    if (missionLayer) { map.removeLayer(missionLayer); missionLayer = null; }
    if (map) map.closePopup();
  }

  // Positioned waypoints for a vehicle's cached mission (those with a real lat/lng).
  function positionedWaypoints(id) {
    const s = pxm[id];
    if (!s || !s.mission) return [];
    return (s.mission.waypoints || []).filter((w) => w.lat != null && w.lng != null);
  }

  // Wide zoom hides the numbers (leaving dots) and shrinks the markers to cut noise;
  // close zoom restores them. Toggled by a single class on the map container — the
  // Leaflet layers are NEVER recreated on zoom (no flicker, no duplication).
  const MISSION_LABEL_ZOOM = 15;
  function applyMissionZoom() {
    if (map) map.getContainer().classList.toggle("mission-faded", map.getZoom() < MISSION_LABEL_ZOOM);
  }

  // Rebuild the overlay for one vehicle from its cached mission. Only waypoints with a
  // real position are plotted (a positionless item — e.g. RTL — is never drawn at 0,0).
  // HOME (seq 0 home/current item, when separate) is drawn as its own marker and is
  // never joined to WP 1 by the route polyline.
  function drawMissionOverlay(id) {
    clearMissionOverlay();
    const s = pxm[id];
    if (!map || !s || !s.mission) return;
    const { home, route } = classifyMission(id);
    if (!route.length && !home) return;
    const cur = s.mission.current_seq;
    const isCur = (w) => cur != null && w.seq === cur;
    // "Done" = already-passed survey legs, per Scout's own current_seq (missionCounts,
    // lib/mission.js) — never guessed locally. Absent current_seq (cur == null),
    // progress is unknown, so the whole route stays one undifferentiated "remaining"
    // line — honest, not a fabricated 0%.
    const { currentIndex } = missionCounts(route, cur);
    const isDone = (w) => cur != null && w.seq != null && w.seq < cur;
    const layer = L.layerGroup();
    if (route.length > 1) {
      if (cur == null) {
        L.polyline(route.map((w) => [w.lat, w.lng]),
          { className: "mission-line", color: "#4C8DFF", weight: 1.6, opacity: 0.6, dashArray: "6 7", lineCap: "round" }).addTo(layer);
      } else {
        // Split the route at the current waypoint so the map itself shows mission
        // progress, not just a text chip in the side panel. The boundary point is
        // included in BOTH legs so they visually connect with no gap.
        const donePts = route.slice(0, currentIndex + 1).map((w) => [w.lat, w.lng]);
        const remPts = route.slice(Math.max(currentIndex, 0)).map((w) => [w.lat, w.lng]);
        if (donePts.length > 1) L.polyline(donePts,
          { className: "mission-line done", color: "#3ECF8E", weight: 1.6, opacity: 0.45, lineCap: "round" }).addTo(layer);
        if (remPts.length > 1) L.polyline(remPts,
          { className: "mission-line", color: "#4C8DFF", weight: 1.8, opacity: 0.75, dashArray: "6 7", lineCap: "round" }).addTo(layer);
      }
    }
    route.forEach((w) => {
      const stateCls = isCur(w) ? "cur" : isDone(w) ? "done" : "";
      L.marker([w.lat, w.lng], { icon: wpIcon(w, stateCls), zIndexOffset: isCur(w) ? 1000 : 0 })
        .bindPopup(wpPopup(w, isCur(w), false)).addTo(layer);
    });
    if (home) {
      // Home usually sits AT the vehicle's current position. Keep it beneath the
      // vehicle marker (negative offset) so the round comm-colored dot stays dominant
      // when the two coincide — the home glyph is fully visible whenever they differ.
      L.marker([home.lat, home.lng], { icon: homeIcon(isCur(home)), zIndexOffset: -1000 })
        .bindPopup(wpPopup(home, isCur(home), true)).addTo(layer);
    }
    layer.addTo(map);
    missionLayer = layer;
    applyMissionZoom();
  }

  // The single stateful Show/Hide toggle's click. Derives the next state from ACTUAL
  // visibility (not the last label): hiding remembers the explicit choice for this USV,
  // showing clears it. No-op while loading or when there is no valid mission to show.
  function toggleMissionOverlay() {
    if (selId == null) return;
    const s = pxmState(selId);
    if (s.loading || !missionIsShowable(selId)) return;
    const nv = toggleVisibility(s);
    s.shown = nv.shown; s.userHidden = nv.userHidden;
    if (s.shown) drawMissionOverlay(selId); else clearMissionOverlay();
    renderPxm();
  }

  // Fit the map to the mission's bounds WITHOUT refetching — a pure client-side view
  // change over the already-cached waypoints. Frames the SURVEY route only: a separate
  // HOME item is excluded so Center does not zoom out to include the vehicle/home
  // position kilometres away. Ensures the overlay is shown so the operator sees what
  // was framed. Uses the last-known mission; safe while offline.
  function centerMission() {
    if (selId == null || !map) return;
    const { route } = classifyMission(selId);
    const pts = route.length ? route : positionedWaypoints(selId);
    if (!pts.length) return;
    const s = pxmState(selId);
    // Centering is an explicit "show me the mission" action — clear any prior hide.
    if (!s.shown) { s.shown = true; s.userHidden = false; drawMissionOverlay(selId); renderPxm(); }
    const bounds = L.latLngBounds(pts.map((w) => [w.lat, w.lng]));
    map.fitBounds(bounds, { padding: [56, 56], maxZoom: 17 });
  }

  // Switch the overlay to follow the selected vehicle: drop the old one, then restore THIS
  // USV's own visibility. A vehicle with a cached valid mission it never hid shows by default;
  // one it explicitly hid stays hidden — per-USV state, restored on every switch/return.
  function syncMissionOverlay() {
    clearMissionOverlay();
    if (selId == null) return;
    const s = pxmState(selId);
    const showable = missionIsShowable(selId);
    const nv = nextVisibility(s, { showable, geometryChanged: false });
    s.shown = nv.shown; s.userHidden = nv.userHidden;
    if (s.shown) drawMissionOverlay(selId);
  }

  // The chip carries the CURRENT communication / fetch status only — it is deliberately
  // separate from whether a cached mission exists (the grid below shows that). So a
  // "Scout unavailable" chip can sit above a fully-populated, last-downloaded mission.
  const PXM_CHIP = {
    loading:     ["Fetching…", "dim"],
    "no-api":    ["No mission API", "dim"],
    unreachable: ["Scout unavailable", "warn"],
    error:       ["Fetch failed", "warn"],
    partial:     ["Partial download", "warn"],
    invalid:     ["Mission invalid", "warn"],
    loaded:      ["Loaded", "ok"],
    empty:       ["No mission loaded", "dim"],
    none:        ["Not fetched", "dim"],
  };

  function fmtSince(ms) {
    if (!ms) return "—";
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    return `${Math.floor(s / 3600)}h ago`;
  }
  function pxmAgeText(s) { return s && s.fetchedAt ? fmtSince(s.fetchedAt) : "—"; }

  // Toggle glyph — a shape per state so it reads without relying on colour: an open eye when
  // shown, a struck-through eye when hidden, an empty/None slash otherwise.
  const eyeOpenSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M1.5 12S5 5 12 5s10.5 7 10.5 7-3.5 7-10.5 7S1.5 12 1.5 12Z"/><circle cx="12" cy="12" r="3"/></svg>';
  const eyeOffSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.6 6.2A9.6 9.6 0 0 1 12 6c7 0 10.5 6.5 10.5 6.5a15.8 15.8 0 0 1-3.3 3.9M6.2 7.6A15.9 15.9 0 0 0 1.5 12.5S5 19 12 19a9.5 9.5 0 0 0 4.2-1M3 3l18 18"/></svg>';
  function toggleIcon(state) {
    return state === "shown" ? eyeOpenSvg : eyeOffSvg;
  }

  function renderPxm() {
    const box = document.getElementById("pxm");
    if (!box) return;
    const id = selId;
    const s = id != null ? pxmState(id) : null;
    const m = s && s.mission;                 // last SUCCESSFULLY downloaded mission (cache)
    const count = m ? m.count : null;
    const cur = m ? m.current_seq : null;
    // Scout-provided integrity fields — consumed ONLY if present (never invented).
    const loadedFlag = m && typeof m.loaded === "boolean" ? m.loaded : null;
    const validFlag = m && typeof m.valid === "boolean" ? m.valid : null;
    const hash = m && m.hash ? String(m.hash) : null;

    // Chip = current comm/fetch status. A live failure (note) wins; otherwise reflect
    // the cached mission's own integrity/emptiness.
    let key = "none";
    if (s && s.loading) key = "loading";
    else if (s && s.note) key = s.note;              // unreachable / no-api / error / partial
    else if (validFlag === false) key = "invalid";
    else if (loadedFlag === false) key = "empty";
    else if (m && m.count > 0) key = "loaded";
    else if (m) key = "empty";
    const [chipText, chipCls] = PXM_CHIP[key] || PXM_CHIP.none;

    // Mission integrity — shown ONLY from Scout-provided signals, never inferred
    // locally: `valid` (VALID / INVALID) and `partial` (an incomplete download, which
    // the proxy sets when Scout flags it or fewer items arrived than the reported count).
    let integ = null;
    if (validFlag === false) integ = ["INVALID", "bad"];
    else if (m && m.partial) integ = ["PARTIAL", "warn"];
    else if (validFlag === true) integ = ["VALID", "ok"];

    // When the current sequence is a separated HOME item, name it as such rather than
    // "WP 0" — the operator is not sitting on a survey waypoint 0. The EXECUTABLE count
    // excludes a split-out Home (seq 0 is the RTL/home item, not a mission leg); the raw
    // MAVLink `count`/`seq` are preserved internally and still shown per-waypoint.
    const mc = m ? classifyMission(id) : { home: null };
    const homeSplit = !!(m && mc.home);
    const execCount = count == null ? null : (homeSplit ? Math.max(0, count - 1) : count);
    const curText = cur == null ? "—"
      : (mc.home && mc.home.seq === cur) ? `Home (seq ${cur})`
      : (execCount != null ? `WP ${cur} / ${execCount}` : `WP ${cur}`);

    const shown = !!(s && s.shown);
    const fetched = !!(s && s.fetchedAt);
    // Single stateful Show/Hide toggle — derived from ACTUAL loading/showable/shown state,
    // never from the last click. (No separate Show and Hide buttons remain.)
    const tb = toggleButton({ loading: !!(s && s.loading), showable: id != null && missionIsShowable(id), shown });

    // A cached mission is being shown while the live link is down / degraded — say so
    // explicitly so the operator never mistakes last-known counts for a live readback.
    const cachedNote = (m && s && s.note && ["unreachable", "error"].includes(s.note))
      ? `<div class="pxm-note warn">Showing last downloaded mission — not re-confirmed with Scout.</div>`
      : (validFlag === false
          ? `<div class="pxm-note warn">Scout reports this mission did not validate.</div>`
          : "");

    // ---- Vehicle Home (deployment: set + read-back-verify HOME_POSITION) --------
    // Compact status + the Set Home action live in this card beside the other
    // deployment/setup actions (Refresh, Show/Center/Hide) — a navigation command
    // (Vehicle Commands, right panel) this is not. All policy/state is read from
    // effectiveHomeStatus / homeGateCtx / commandGate (lib/home.js + this file's
    // shared helpers) — nothing is recomputed here.
    const v = id != null ? fleet.find((x) => x.id === id) : null;
    const hs = v ? effectiveHomeStatus(v) : null;
    const g = v ? commandGate("SET_HOME", homeGateCtx(v)) : { enabled: false, reason: null };
    // "Verified" here is ALWAYS hs.state === "verified" — i.e. v.home.verified as Scout
    // itself currently reports it (home_block(), sourced from payload.agent.home_status).
    // A successful SET_HOME command result never sets this chip directly; see the
    // "confirmed" phase below for the transient, non-authoritative click feedback.
    const homeChip = !hs ? ["—", "dim"]
      : hs.state === "verified" ? ["Verified", "ok"]
      : hs.state === "pending" ? ["Setting…", "pending"]
      : ["Not verified", hs.state === "unknown" ? "dim" : "warn"];
    let homeSub = null;
    if (hs) {
      if (setHome.phase === "confirmed") {
        // The command itself succeeded (Scout accepted + verified the read-back) —
        // NOT the same as the Home chip above reading Verified, which waits for
        // Scout's own continuous status to catch up on the next fleet poll.
        homeSub = "Set Home accepted by Scout — confirming Home status…";
      } else if (hs.failMessage) {
        homeSub = hs.failMessage;
      } else if (hs.state === "pending") {
        homeSub = "Verification pending";
      } else if (hs.state === "verified") {
        homeSub = hs.verifiedDistanceM != null ? `${fmtDistance(hs.verifiedDistanceM)} from Scout` : null;
      } else if (hs.distanceM != null) {
        homeSub = `${fmtDistance(hs.distanceM)} from Scout`;
      } else if (hs.state === "unknown") {
        homeSub = "Home not received";
      }
    }
    const homeSubCls = hs && hs.failMessage ? "warn" : (setHome.phase === "confirmed" ? "pending" : "");
    const setHomeLabel = setHome.phase === "pending" ? "Setting…" : "Set Home";
    const setHomeTitle = (g.enabled
      ? "Set the Pixhawk HOME / RTL recovery point to the Scout's current position"
      : (g.reason || (v ? "Set Home unavailable" : "No vehicle selected"))).replace(/"/g, "&quot;");

    box.innerHTML = `
      <div class="pxm-h">
        <span class="lbl">Pixhawk mission</span>
        <span class="pxm-chip ${chipCls}">${chipText}</span>
      </div>
      <div class="pxm-grid">
        <div class="pxm-row"><span class="k">Loaded</span><span class="v">${execCount == null ? "—" : `${execCount} waypoint${execCount === 1 ? "" : "s"}`}${homeSplit ? ` <span class="pxm-sub">+ Home (seq ${mc.home.seq == null ? 0 : mc.home.seq})</span>` : ""}</span></div>
        <div class="pxm-row"><span class="k">Current</span><span class="v">${curText}</span></div>
        ${integ ? `<div class="pxm-row"><span class="k">Integrity</span><span class="pxm-integ ${integ[1]}">${integ[0]}</span></div>` : ""}
        <div class="pxm-row"><span class="k">Last download</span><span class="v" id="pxm-age">${pxmAgeText(s)}</span></div>
        ${hash ? `<div class="pxm-row"><span class="k">Mission id</span><span class="v" title="${hash}">${hash.slice(0, 8)}</span></div>` : ""}
        <div class="pxm-row"><span class="k">Home</span><span class="pxm-chip ${homeChip[1]}">${homeChip[0]}</span></div>
        ${homeSub ? `<div class="pxm-note ${homeSubCls}">${homeSub}</div>` : ""}
      </div>
      ${cachedNote}
      <div class="pxm-actions">
        <div class="pxm-btns2">
          <button data-pxm="fetch" ${s && s.loading ? "disabled" : ""} title="Fetch the mission stored on the Pixhawk">${fetched ? "Refresh" : "Fetch"}</button>
          <button data-pxm="set-home" ${g.enabled ? "" : "disabled"} title="${setHomeTitle}">${setHomeLabel}</button>
        </div>
        <div class="pxm-btns">
          <button data-pxm="toggle" class="pxm-toggle ${tb.state}" ${tb.disabled ? "disabled" : ""} aria-pressed="${tb.ariaPressed}" title="${tb.title.replace(/"/g, "&quot;")}">${toggleIcon(tb.state)}<span>${tb.label}</span></button>
          <button data-pxm="center" ${id != null && missionIsShowable(id) ? "" : "disabled"} title="Fit the map to the mission (no refetch)">Center</button>
        </div>
      </div>`;

    box.querySelector('[data-pxm="fetch"]').onclick = () => fetchPixhawkMission(selId);
    box.querySelector('[data-pxm="toggle"]').onclick = () => toggleMissionOverlay();
    box.querySelector('[data-pxm="center"]').onclick = () => centerMission();
    const setHomeBtn = box.querySelector('[data-pxm="set-home"]');
    if (setHomeBtn) setHomeBtn.onclick = () => doSetHome();
    renderMissionBar();
  }

  // Real waypoint-progress + distance/ETA for the SELECTED vehicle, from the same
  // Pixhawk mission readback + lib/mission.js math the Pixhawk Mission card uses —
  // never a second, differently-computed number. null when no mission is loaded.
  function selectedMissionStats() {
    const v = fleet.find((x) => x.id === selId);
    const s = selId != null ? pxm[selId] : null;
    if (!v || !s || !s.mission) return null;
    const { route } = classifyMission(selId);
    if (!route.length) return null;
    const cur = s.mission.current_seq;
    const { total, completed, remaining, pct } = missionCounts(route, cur);
    const remDistM = remainingRouteDistanceM(route, cur, v.lat, v.lng);
    const etaS = etaSeconds(remDistM, v.speed);
    return { total, completed, remaining, pct, remDistM, etaS };
  }

  // Bottom-of-map mission-progress strip (H1, operational review) — the map surface
  // itself now shows "how much of the survey is left", not just a text chip in the
  // side panel. Hidden entirely when there is nothing real to show (no mission loaded,
  // or no vehicle selected) rather than rendering a fabricated 0%.
  function renderMissionBar() {
    const bar = document.getElementById("mpbar");
    if (!bar) return;
    const ms = selectedMissionStats();
    if (!ms || ms.pct == null) { bar.style.display = "none"; return; }
    bar.style.display = "flex";
    document.getElementById("mpb-fill").style.width = ms.pct + "%";
    document.getElementById("mpb-label").textContent =
      `WP ${ms.completed}/${ms.total} · ${ms.pct}%` + (ms.remDistM != null ? ` · ${fmtDistance(ms.remDistM)} remaining` : "");
  }

  function tickPxmAge() {
    const el = document.getElementById("pxm-age");
    if (el && selId != null) el.textContent = pxmAgeText(pxm[selId]);
  }

  function normEvent(e) {
    if (e == null) return "";
    if (typeof e === "string") return e;
    return e.title || e.message || e.text || e.event || e.name || JSON.stringify(e);
  }

  // Comms-state transition log for the selected vehicle (GET /api/comms/history/{id}).
  // Loaded on selection + refreshed on a timer; cached in commsHist and re-rendered.
  function loadCommsHistory(id) {
    if (id == null) { commsHist = null; return; }
    api.getCommsHistory(id).then((h) => {
      if (id === selId) { commsHist = h; renderInspector(); }
    }).catch(() => {});
  }

  // Control authority for the selected vehicle — a direct, dedicated read (GET
  // /api/control_authority/{id}, itself a live proxy to Scout's own Flask API), NOT
  // part of the fleet payload and NOT the command queue. Loaded on selection +
  // refreshed on a timer. Fed into the authority controller, which confirms any
  // pending hand-off against the effective value Scout reports. A network failure
  // reads as unreachable/unknown — never a guessed authority.
  function loadAuthority(id) {
    if (id == null) return;
    api.getControlAuthority(id).then((a) => {
      if (id === selId) authCtl.setServer(a);
    }).catch(() => {
      if (id === selId) authCtl.setServer({ ok: true, available: true, reachable: false, authority: null });
    });
  }

  // ---- Agent Mission: the LIGHTWEIGHT status poll ------------------------------------------
  // READS ONLY, and reads only Scout's canonical mission-execution status — the lifecycle state,
  // mode, waypoint progress, active operation and the explicit replanning overlay. A poll never
  // starts, pauses, resumes or stops anything, so a reconnect or a page refresh can never
  // re-issue a lifecycle operation.
  //
  // IT MUST NEVER CALL THE PREFLIGHT. That is the whole point of this change and it is pinned by
  // tests/mission-readiness.test.mjs: the preflight is an expensive, short-lived proof and
  // recomputing it on a timer made the Agent Mission card flicker between READY and NOT_READY on
  // a vehicle that was not changing. Start availability comes from mx.startGate(), whose inputs
  // are all here in the status; the authoritative proof runs inside the Start transaction.
  function loadMissionStatus(id) {
    if (id == null) { mission.status = null; mission.forVid = null; return; }
    const forId = id;
    api.getMissionExecutionStatus(id).then((st) => {
      if (forId !== selId) return;                 // selection moved — discard the stale fetch
      mission.status = st;
      mission.forVid = forId;
      // AUTHORITATIVE lifecycle evidence just arrived. If it says the route on the vehicle can
      // have changed — a replanned safe return uploaded and verified — the overlay refetches
      // itself here. The tracker suppresses an unchanged signal, so a steady mission costs
      // nothing extra.
      noteRevisionEvidence(forId);
      renderInspector();
    }).catch((e) => {
      if (forId !== selId) return;
      mission.status = null;
      mission.forVid = forId;
      logQuiet("mission-execution status", e);
      renderInspector();
    });
  }

  // ---- Agent Mission: the ONE-SHOT, NON-POLLED preflight ------------------------------------
  // Run at a moment where the answer can actually have changed — never on a timer:
  //
  //   "selection"    the operator selected this vehicle
  //   "mission"      a mission write (upload / clear / replan) succeeded on the vehicle
  //   "transaction"  a lifecycle transaction finished (Start synchronizes the planning package)
  //   "reconnect"    the vehicle came back after a disconnect
  //   "manual"       the operator pressed Refresh on the card
  //
  // Its result is INFORMATION (preflightNote → the card's `info` line). It does not feed the
  // Start gate, so a refresh — in flight, failed, or reporting an incomplete proof — can never
  // change which buttons the operator is offered.
  function refreshPreflight(id, reason) {
    if (id == null || mission.refreshing) return;
    const forId = id;
    mission.refreshing = true;
    if (forId === selId) renderInspector();
    // The publication state rides along on the SAME one-shot moments. It is read-only and free
    // (no Scout call, no Pixhawk download), and it is what lets this card say "the Agent package
    // is owed" instead of the far weaker "the planning package is not consistent" — the durable
    // record knows a sync is outstanding; Scout's package evidence alone cannot distinguish
    // that from a genuine disagreement.
    api.getPublishState(id).then((pb) => {
      if (forId === selId) { mission.publish = pb; mission.publishFor = forId; }
    }).catch(() => {});
    api.getMissionExecutionPreflight(id).then((pf) => {
      if (forId !== selId) return;
      mission.preflight = pf;
      mission.preflightAt = Date.now();
      mission.preflightFor = forId;
      mission.preflightReason = reason;
    }).catch((e) => {
      // A failed request tells us nothing about the vehicle, so the previous note stands
      // untouched — exactly as it must, since the note is not a gate.
      logQuiet("mission-execution preflight", e);
    }).finally(() => {
      mission.refreshing = false;
      if (forId === selId) renderInspector();
    });
  }

  // ONE orchestrated transaction per operator intent. The browser does NOT transfer authority
  // itself and does not sequence two calls: the endpoint below performs the authority hand-off,
  // verifies it by read-back and issues the Scout operation as one operation with phases.
  // `mission.busy` is set BEFORE the await and cleared in finally, so a second press during the
  // round-trip is impossible (the buttons are also rendered disabled from the same flag).
  function missionTransaction(label, action, fn) {
    if (mission.busy || selId == null) return;
    const id = selId;
    mission.busy = true;
    mission.result = { label, action, view: { outcome: "pending" }, at: Date.now() };
    renderInspector();
    Promise.resolve(fn(id)).then((r) => {
      if (id !== selId) return;                    // isolation: never show on another vehicle
      mission.result = { label, action, view: mx.interpretTransaction(r), at: Date.now() };
    }).catch((e) => {
      if (id !== selId) return;
      mission.result = { label, action, at: Date.now(),
        view: { outcome: mx.OUTCOME.UNAVAILABLE, message: asText(e && e.message) || String(e) } };
    }).finally(() => {
      mission.busy = false;
      loadMissionStatus(id);
      // A completed transaction is one of the non-polling preflight moments: Start synchronizes
      // the planning package, and every lifecycle operation can move the state the preconditions
      // are computed against. ONE read, here, rather than one every 3 s forever.
      refreshPreflight(id, "transaction");
      // The authority display is part of the SAME operation, so refresh it here rather than
      // waiting up to 2 s for the next poll to reveal who ended up holding the wheel.
      loadAuthority(id);
      renderInspector();
    });
  }

  // Command queue + history for the selected vehicle (the reverse/control path). Used
  // to show each command's lifecycle: requested → sent → acknowledged → confirmed /
  // rejected / timed-out. Never assumes success — the vehicle reports the status.
  function loadCommands(id) {
    if (id == null) { cmds = []; return; }
    api.getCommands(id).then((d) => {
      if (id === selId) {
        cmds = (d && d.commands) || [];
        syncSetHomeFromCommands();
        detectMissionWrites(id);
        renderInspector(); renderPxm(); updateHomeMarker();
      }
    }).catch(() => { if (id === selId) { cmds = []; renderInspector(); } });
  }

  // A mission-writing command (upload / clear / replan) that has reached a terminal state MAY
  // mean the mission stored on the vehicle changed. Re-read ground truth only when the write
  // actually SUCCEEDED (verified/executed) — or when a failed write explicitly reports an
  // uncertain/partial on-vehicle state (missionWriteNeedsRefetch). An ordinary rejected/failed
  // upload leaves the mission unchanged, so it does not force a download. Each command id is
  // evaluated once (missionWriteHandled) so a settled command never re-triggers on the 3 s poll.
  function detectMissionWrites(id) {
    let fire = false;
    cmds.forEach((c) => {
      if (!MISSION_WRITE_COMMANDS.has(c.type)) return;
      if (!CMD_TERMINAL_M.has(c.status)) return;
      if (missionWriteHandled.has(c.id)) return;
      missionWriteHandled.add(c.id);
      const v = commandVerification(c);
      if (missionWriteNeedsRefetch(v.outcome, c.result)) fire = true;
    });
    if (fire) {
      refreshController.refreshMission(id, "command");
      // A successful mission write is the other moment the Start preconditions genuinely change
      // (a new route means a new hash to read back and a new package to be consistent with), so
      // the one-shot preflight is re-run here — once per write, not once per poll.
      refreshPreflight(id, "mission");
    }
  }

  // Resolve the in-flight Set-Home command's queue lifecycle (QUEUED → SENT → EXECUTED/
  // FAILED/REJECTED/EXPIRED, reported by the Local Agent) into a transient click-feedback
  // flash ONLY — pending/confirmed/failed. This NEVER sets Home to "verified": command
  // status EXECUTED means only "the Local Agent successfully called Scout Flask"
  // (command-protocol semantics), not that Set Home succeeded. The backend's own
  // home_result classification (main._annotate_set_home_result, inspecting the nested
  // Scout result — accepted/verified/home_position/verification_distance_m) is what
  // decides confirmed vs. failed here; a bare EXECUTED with no home_result (e.g. an
  // older/non-conforming Local Agent) is treated as failed, never as an optimistic
  // success. The PERMANENT Verified/Not verified state always comes from v.home
  // (Scout's own continuously-reported home_status) on the next fleet poll — never
  // from this flash, which is cosmetic and self-clears.
  //
  // The decision itself is setHomeOutcome (lib/home.js) — pure and unit-tested — which
  // also bounds the wait: a Local Agent that never reports, a vanished command record
  // (the backend queue is in-memory and resets on restart) or a POST that never settles
  // all resolve to a "failed" timeout instead of pending forever. Driven by the command
  // poll AND by a 1 s watchdog, so the deadline still fires when polling itself is down.
  function syncSetHomeFromCommands() {
    if (setHome.phase !== "pending") return;   // only an in-flight request is resolvable
    const cmd = setHome.cmdId ? cmds.find((c) => c.id === setHome.cmdId) || null : null;
    const out = setHomeOutcome({ cmd, cmdId: setHome.cmdId, startedAt: setHome.at, now: Date.now() });
    if (out.phase === "pending") return;
    if (setHomeTimer) { clearTimeout(setHomeTimer); setHomeTimer = null; }
    setHome = { phase: out.phase, code: out.code, message: out.message, at: Date.now(), cmdId: null };
    const flashConfirmed = setHome.phase === "confirmed";
    setHomeTimer = setTimeout(() => {
      setHome = { phase: "idle", code: null, message: null, at: 0, cmdId: null };
      renderInspector(); renderPxm(); updateHomeMarker();
    }, flashConfirmed ? 4000 : 9000);
    renderInspector(); renderPxm(); updateHomeMarker();
  }

  // Transient, non-blocking command-result notice (replaces window.alert, which froze
  // the tab and could hide behind other windows on a multi-monitor field setup).
  let toastTimer = null;
  function showToast(message, kind = "warn") {
    const box = document.getElementById("map-toast");
    if (!box) return;
    box.className = `ov toast ${kind}`;
    box.textContent = message;
    box.style.display = "flex";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.style.display = "none"; }, 6000);
  }

  async function sendCommand(type) {
    // Ignore a duplicate press while one of the same type is already in flight or still
    // nonterminal in the queue (the synchronous `sending` guard closes the double-click
    // window before the queue poll catches up). See cmdBtns for the matching disable.
    if (sending.has(type) || hasPendingOfType(cmds, type)) return;
    const v = fleet.find((x) => x.id === selId);
    const vname = v ? (v.name || "USV-" + v.id) : "vehicle";
    const label = CMD_LABEL[type] || type;
    const highRisk = HIGH_RISK.has(type);
    sending.add(type);
    renderInspector();   // reflect the in-flight disable immediately
    try {
      if (highRisk) {
        const bodyHtml = CMD_CONFIRM_COPY[type]
          ? CMD_CONFIRM_COPY[type](vname)
          : `<p>Confirm ${label} for <b>${vname}</b>?</p><p>The command is queued for the vehicle to execute — it is NOT applied until the local agent reports back.</p>`;
        const ok = await confirmModal({ title: `${label}?`, bodyHtml, cancelLabel: "Cancel", confirmLabel: label });
        if (!ok) return;
      }
      let res = await api.createCommand({ vehicle_id: selId, type, confirm: highRisk });
      if (!res.ok && res.data && res.data.needs_confirmation) {
        const ok = await confirmModal({
          title: `${label} needs confirmation`,
          bodyHtml: `<p>${res.data.message}</p><p>Queue the command anyway?</p>`,
          cancelLabel: "Cancel", confirmLabel: "Queue anyway",
        });
        if (!ok) return;
        res = await api.createCommand({ vehicle_id: selId, type, confirm: true });
      }
      if (!res.ok) showToast((res.data && res.data.message) || "Command was not accepted.", "warn");
    } finally {
      sending.delete(type);
      loadCommands(selId);   // always refresh the queue (and re-render) after the attempt
    }
  }

  // ---- Vehicle Home (deployment: set + read-back-verify HOME_POSITION) ------------
  // The displayed Home status: v.home (Scout's own continuously-reported home_status)
  // overlaid ONLY with the transient pending/failed click feedback — a "confirmed"
  // command result is deliberately NOT passed through as a homeStatus phase and never
  // forces state to "verified" here. Home only ever reads VERIFIED once v.home.verified
  // itself is true on a later fleet poll; see the "confirmed" flash in renderPxm for the
  // separate, non-authoritative "command succeeded, confirming…" message.
  function effectiveHomeStatus(v) {
    const phase = setHome.phase === "pending" ? "pending"
      : setHome.phase === "failed" ? "failed" : "idle";
    return homeStatus(v, { phase, failMessage: setHome.message });
  }

  // Home-verification interlock context for the selected vehicle — computed once and
  // shared by the Pixhawk Mission card (Set Home + status) and the Vehicle Commands /
  // readiness gating below. The derivation lives in commandGateCtx (lib/home.js), the
  // SAME builder the Vehicle page uses, so the two pages cannot gate a button
  // differently; this function only supplies Map's page-local inputs. The policy itself
  // stays in commandGate (lib/home.js).
  function homeGateCtx(v) {
    const stale = commState(v) !== "connected";
    return commandGateCtx(v, {
      hasControl: !stale && authCtl.view().hasControl,
      connected: !stale,
      missionLoaded: !!(pxm[v.id] && pxm[v.id].mission && pxm[v.id].mission.count > 0),
      setHomePending: setHome.phase === "pending",
      // Mirrors effectiveHomeStatus: a Home mid-change must not read as verified.
      homePhase: setHome.phase === "pending" ? "pending" : setHome.phase === "failed" ? "failed" : "idle",
    });
  }

  // A confirmation modal — Set Home moves the RTL recovery point, so it must never be a
  // one-click action. Resolves true (SET AND VERIFY HOME) or false (CANCEL / Esc / scrim).
  function confirmModal({ title, bodyHtml, cancelLabel = "CANCEL", confirmLabel = "CONFIRM" }) {
    return new Promise((resolve) => {
      const ov = document.createElement("div");
      ov.className = "modal-ov";
      ov.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="${title}">
        <div class="modal-h">${title}</div>
        <div class="modal-b">${bodyHtml}</div>
        <div class="modal-f">
          <button class="modal-btn modal-cancel">${cancelLabel}</button>
          <button class="modal-btn modal-confirm">${confirmLabel}</button>
        </div></div>`;
      const done = (val) => { document.removeEventListener("keydown", onKey); ov.remove(); resolve(val); };
      const onKey = (e) => { if (e.key === "Escape") done(false); };
      ov.addEventListener("click", (e) => { if (e.target === ov) done(false); });
      ov.querySelector(".modal-cancel").onclick = () => done(false);
      ov.querySelector(".modal-confirm").onclick = () => done(true);
      document.addEventListener("keydown", onKey);
      document.body.appendChild(ov);
      ov.querySelector(".modal-confirm").focus();
    });
  }

  // SET_HOME is a normal queued command: this only creates the QUEUED record (api.setHome
  // → POST /api/commands, type SET_HOME) and shows "pending" — it never claims verified
  // on the click. The actual EXECUTED/FAILED result arrives later from the Local Agent
  // and is picked up by syncSetHomeFromCommands() on the next command-queue poll.
  async function doSetHome() {
    const v = fleet.find((x) => x.id === selId);
    if (!v) return;
    const body = `
      <p>The Scout's current GPS position will become the RTL recovery point.</p>
      <p>This should only be performed when the Scout is physically located at the intended recovery location.</p>`;
    const ok = await confirmModal({ title: "Set Pixhawk Home?", bodyHtml: body, cancelLabel: "Cancel", confirmLabel: "Set Home" });
    if (!ok) return;
    if (setHomeTimer) { clearTimeout(setHomeTimer); setHomeTimer = null; }
    setHome = { phase: "pending", code: null, message: null, at: Date.now(), cmdId: null };
    renderInspector(); renderPxm(); updateHomeMarker();
    let res;
    try { res = await api.setHome(v.id, { lat: v.lat, lng: v.lng }); }
    catch (e) { res = { ok: false, data: null }; }
    const cmd = res && res.data && res.data.command;
    if (res && res.ok && cmd) {
      // Queued — stay "pending" and track this command's id; the result lands async.
      setHome = { phase: "pending", code: null, message: null, at: Date.now(), cmdId: cmd.id };
    } else {
      const msg = (res && res.data && res.data.message) || "Set Home was not accepted.";
      setHome = { phase: "failed", code: "not_accepted", message: msg, at: Date.now(), cmdId: null };
      setHomeTimer = setTimeout(() => {
        setHome = { phase: "idle", code: null, message: null, at: 0, cmdId: null };
        renderInspector(); renderPxm(); updateHomeMarker();
      }, 9000);
    }
    // Refresh the command queue now (also picks up terminal results on the 3 s poll).
    loadCommands(selId);
    renderInspector(); renderPxm(); updateHomeMarker();
  }

  // VEHICLE deployment readiness — DEPLOYMENT EVIDENCE ONLY: Pixhawk connected, GPS ready,
  // mission loaded, Home verified. Properties OF THE VEHICLE, and the only inputs to the
  // VEHICLE READY / VEHICLE NOT READY banner.
  //
  // Authority is not rendered here at all — not as a tab, not as an item, not as a Control
  // Owner card. Two separate reasons, both load-bearing:
  //   • it is not a readiness fact. Handing authority to the Local Agent is what a mission
  //     REQUIRES, so it can never make the vehicle unfit — the inversion the bench test caught,
  //     which deploymentReadiness() still guards by never scoring it;
  //   • it is already shown, once, in the Status area above. A second readout here is where the
  //     inspector started saying the same thing in three places.
  // deploymentReadiness() keeps reporting controlOwner in its DATA — the safety gating and the
  // status APIs are untouched; this view simply does not print it a second time. Whether the
  // AGENT MISSION is startable is a third, separate question, answered by the Agent Mission
  // card from Scout's own state plus the backend Start preflight.
  function renderReadiness(gateCtx, authVal) {
    const r = deploymentReadiness({ ...gateCtx, authority: authVal });
    const items = r.items.map((i) =>
      `<div class="rdy-item ${i.ok ? "ok" : "no"}"><span class="rdy-mk">${i.ok ? "✓" : "✕"}</span>${i.label}</div>`).join("");
    const banner = r.ready
      ? `<div class="rdy-banner ok">VEHICLE READY</div>`
      : `<div class="rdy-banner ${r.loiterAvailable ? "warn" : "dim"}" title="${r.loiterAvailable ? "LOITER remains available as an immediate anti-drift safety hold." : ""}">VEHICLE NOT READY</div>`;
    return `<div class="rdy">${items}</div>${banner}`;
  }

  // ---- Agent Mission card — the NORMAL operational mission control ------------------------
  // Everything here is derived from Scout's canonical mission-execution status (never from the
  // last click, never from the previous label, and — since this change — never from a polled
  // Start preflight). The card's whole shape — chip, ONE short line, identity rows, at most ONE
  // concise blocker, buttons — comes from lib/mission-execution.js missionCardView(), the one
  // tested place the state→presentation mapping is authored, so the Map cannot drift from it.
  //
  // COMPACT BY CONTRACT. This card carries no paragraph: no Pixhawk-readback explanation, no
  // planning-package essay, no authority-orchestration narration, no "why this Scout has no
  // Stop endpoint", and never several failures concatenated. Each of those still exists — as
  // the `title` of the element it belongs to, and in full on the Agent diagnostics page.
  // Authority is not a row here; the Status area above shows it once.
  // THE AGENT PACKAGE LINE. Derived by the SAME function the Agent page renders from
  // (lib/mission-publish.js readinessLabel), so the two pages cannot disagree about one
  // vehicle's package. Its whole job is to keep apart four things the old single "the planning
  // package is not consistent with the approved mission" collapsed into one:
  //
  //   VERIFYING              Scout is re-deriving its own readiness, or the comparison has not
  //                          completed. Neutral — nothing is wrong and nothing is claimed.
  //   SCOUT_UNREACHABLE      the question could not be asked. Not a disagreement.
  //   PACKAGE_SYNC_REQUIRED  the operator backend knows a sync is OWED. The only one with an
  //                          action attached — and that action sends a package and nothing else.
  //   REAL_MISMATCH          both sides reported, and they differ. The only true warning.
  //
  // Rendered with the same one-line-plus-tooltip discipline as every other note on this card.
  const SYNC_BTN_TITLE = "Rebuild the approved planning package from the active mission and " +
    "send it to the Agent. Sends a package only — it issues no vehicle command and cannot " +
    "re-upload the Pixhawk mission.";

  function agentPackageLine(v, pfFor) {
    const pubFor = mission.publishFor != null && v && mission.publishFor === v.id;
    const verdict = readinessLabel({
      publish: pubFor ? mission.publish : null,
      readiness: (mission.preflight && pfFor && mission.preflight.readiness) || null,
      refreshing: mission.refreshing && pfFor,
    });
    if (verdict.state === PKG_STATE.NO_MISSION) return "";
    const retry = verdict.state === PKG_STATE.PACKAGE_SYNC_REQUIRED
      ? ` <button class="amx-refresh" data-mx-sync="1"${mission.syncing ? " disabled" : ""} title="${escAttr(SYNC_BTN_TITLE)}">Retry Agent Sync</button>`
      : "";
    return `<div class="amx-note${verdict.state === PKG_STATE.REAL_MISMATCH ? " warn" : ""}" title="${escAttr(verdict.detail || "")}">${esc(verdict.text)}${retry}</div>`;
  }

  function renderAgentMission(v) {
    // Isolation guard: only render lifecycle state actually fetched for THIS vehicle.
    const forThis = mission.forVid != null && v && mission.forVid === v.id;
    const S = mx.normalizeStatus(forThis ? mission.status : null);
    const pfFor = mission.preflightFor != null && v && mission.preflightFor === v.id;
    const pf = pfFor ? preflightNote(mission.preflight, { at: mission.preflightAt }) : null;
    const res = forThis ? mission.result : null;

    // START AVAILABILITY: stable blockers only. Disconnected, unsupported, no mission, another
    // operation in flight, explicit replanning, already running, a terminal state needing Rearm.
    // Nothing here is a short-lived proof, so nothing here can flicker. The Start transaction
    // performs the real, fresh, fail-closed proof before any vehicle write.
    const gate = mx.startGate(S, {
      connected: commState(v) === "connected",
      busy: mission.busy,
      missionId: mission.preflight && mission.preflight.mission_id,
    });
    const rv = readinessView(gate, { refreshing: mission.refreshing && pfFor });
    // A START issued from this station, as distinct from any other transaction — it is the one
    // with phase-specific operator copy.
    const starting = mission.busy && !!res && res.action === "start";
    const card = mx.missionCardView(S, {
      busy: mission.busy, startBlocked: !gate.canStart, startBlockedReason: gate.reason,
      readiness: rv, starting, preflight: pf,
      // Home comes from Scout's own continuously-reported home_status (lib/home.js), which is a
      // better source than the mission-execution status' verified_home block.
      homeVerified: homeStatus(v).verified,
      missionId: mission.preflight && mission.preflight.mission_id,
      unavailableDetail: "Mission lifecycle status could not be read from the Scout Local " +
        "Agent. Nothing about the mission is assumed — no lifecycle action is offered.",
    });

    // The chip + ONE short line. Whatever long thing the model had to say (the replanning FSM,
    // the operation id, the completion evidence) is this line's tooltip, not a paragraph.
    const head = `<div class="amx-h" title="${escAttr(card.headlineTitle || "")}">
        <span class="amx-state ${card.tone}">${esc(card.chip)}</span>
        <span class="amx-sub">${esc(card.headline)}</span>
      </div>`;

    const rows = card.rows.length
      ? `<div class="amx-grid">${card.rows.map((r) =>
          `<div class="amx-row"><span class="k">${esc(r.k)}</span><span class="v${r.mono ? " mono" : ""}" title="${escAttr(r.title || "")}">${esc(r.v)}</span></div>`).join("")}</div>`
      : "";

    // Progress. While the START transaction runs this is PHASE-SPECIFIC and NEUTRAL — "Checking
    // mission readiness…", "Taking agent control…", "Holding position…", "Setting and verifying
    // Home…", "Starting AUTO…" — each one Scout's observed step (or, before Scout has moved, the
    // backend's provable first phase). Never a predicted next state, never a fake percentage.
    const progress = card.working
      ? `<div class="amx-prog"><span class="amx-spin"></span><span>${esc(card.headline)}${S.activeOperationId ? ` · ${esc(S.activeOperationId)}` : ""}</span></div>`
      // A one-shot preflight refresh, in its own muted presentation. Deliberately not the
      // caution-orange progress line above — nothing is happening to the vehicle — and it
      // changes no button, because readiness.canStart came from the gate, not from this.
      : card.checking
        ? `<div class="amx-prog checking" title="${escAttr(rv.detail || "")}"><span class="amx-spin"></span><span>${esc(card.checkingText || "Checking…")}</span></div>`
        : "";

    const buttons = card.buttons.length
      ? `<div class="amx-btns">${card.buttons.map((b) => {
          const title = b.enabled ? mission_ACTION_TITLE[b.action] || "" : (b.reason || "");
          return `<button class="amx-btn ${b.kind}${b.tone === "warn" ? " warn" : ""}" data-mx="${b.action}"${b.enabled ? "" : " disabled"} title="${escAttr(title)}">${esc(b.label)}</button>`;
        }).join("")}</div>`
      : "";

    // ONE blocker line, short, with the full evidence on hover. Never a stack of failures.
    const blocker = card.blocker
      ? `<div class="amx-note${card.blocker.tone === "warn" ? " warn" : ""}" title="${escAttr(card.blocker.title || "")}">${esc(card.blocker.text)}</div>`
      : "";

    // Home, before Start: ONE neutral line saying the Start transaction will set it. It is not a
    // blocker and not a warning — the transaction sets Home to the launch position and verifies
    // it as one of its own phases.
    const home = card.home
      ? `<div class="amx-note${card.home.tone === "warn" ? " warn" : ""}" title="${escAttr(card.home.title || "")}">${esc(card.home.text)}</div>`
      : "";

    // A FINISHED run says so in its own line: "Final LOITER verified" beside the COMPLETED chip,
    // or the explicit gap when Scout reached COMPLETED_HOLD without verifying the final LOITER.
    const completion = card.completionNote
      ? `<div class="amx-note${card.completionNote.tone === "warn" ? " warn" : ""}" title="${escAttr(card.completionNote.title || "")}">${esc(card.completionNote.text)}</div>`
      : "";

    // A NEW mission uploaded while the previous run still owns the vehicle. Stated in full — it
    // is the one situation where the operator has just done something and nothing appears to
    // have happened, so the short-line discipline would cost more than it saves.
    const conflict = card.replacementConflict
      ? `<div class="amx-note warn" title="${escAttr(card.replacementConflict.title || "")}">${esc(card.replacementConflict.text)}</div>`
      : "";

    // Start is available AND pressing it takes agent control first. Said before the press, as
    // information — never as something to go and arrange by hand.
    const authorityNote = card.authorityWillBeAcquired && !card.working
      ? `<div class="amx-note" title="${escAttr(mx.START_ACQUIRES_AUTHORITY_NOTE)}">Start will take Local Agent control</div>`
      : "";

    // Scout's own battery diagnosis. `battery_valid:false` / a -1 raw is UNKNOWN, and unknown
    // must never render as 0% — a flat battery is an emergency, a missing reading is a gap.
    // Shown only when Scout reported diagnostics AND they say the reading is not usable: a known
    // percentage already has a home in the Status area, and repeating it here would be noise.
    const batt = mx.batteryView(S);
    const battery = batt.known || !batt.text ? ""
      : `<div class="amx-note" title="${escAttr(batt.detail || "")}">${esc(batt.text)}</div>`;

    // The one-shot preflight, as INFORMATION, beside the control that re-runs it. Both are muted:
    // this line never gates anything and pressing Refresh never touches the vehicle. Withheld
    // entirely for an unsupported or unreachable Scout — there is nothing there to preflight, and
    // offering a Refresh that cannot answer is its own small lie.
    const info = card.present === false ? "" : `<div class="amx-info">
        ${card.info ? `<span class="amx-info-t" title="${escAttr(card.info.title || "")}">${esc(card.info.text)}${mission.preflightAt && pfFor ? ` · ${esc(fmtAgo((Date.now() - mission.preflightAt) / 1000))}` : ""}</span>` : `<span class="amx-info-t">Readiness not checked yet</span>`}
        <button class="amx-refresh" data-mx-refresh="1"${mission.refreshing ? " disabled" : ""} title="Re-run the read-only Start preflight once. Writes nothing, commands nothing, and does not change which buttons are offered.">Refresh</button>
      </div>`;

    // ONE Agent-package line, from the shared derivation (see agentPackageLine).
    const pkgLine = card.present === false ? "" : agentPackageLine(v, pfFor);

    // A Start that did not happen reads as ONE compact actionable error; the precondition
    // evidence, Scout's code and the phase detail are the tooltip. Every other transaction keeps
    // the one-word outcome line.
    const startFail = res && res.action === "start" ? mx.startFailure(res.view) : null;
    const resultNote = startFail
      ? `<div class="amx-result warn" title="${escAttr([startFail.detail, mx.transactionSummary(res.view)].filter(Boolean).join(" — "))}">
           <b>${esc(startFail.title)}</b>: ${esc(startFail.text)}
         </div>`
      : res
        ? `<div class="amx-result ${res.view.outcome === "accepted" ? "ok" : res.view.outcome === "pending" ? "" : "warn"}" title="${escAttr(res.view.outcome === "pending" ? "" : mx.transactionSummary(res.view))}">
           <b>${esc(res.label)}</b>: ${esc(res.view.outcome === "pending" ? "sending…" : mx.outcomeLabel(res.view.outcome))}
         </div>`
        : "";

    return `<div class="amx">${head}${rows}${progress}${buttons}${conflict}${blocker}${completion}${authorityNote}${home}${battery}${pkgLine}${info}${resultNote}</div>`;
  }

  // Per-action hover copy for an ENABLED lifecycle button. Each says what the ONE operation
  // does, including its authority phase — the operator never arranges authority by hand.
  const mission_ACTION_TITLE = {
    start: "Start the mission: the operator station transfers control authority to the Local " +
      "Agent and verifies it, then Scout holds position, sets and verifies Home at the launch " +
      "position, synchronizes the planning package and starts AUTO.",
    pause: "Pause the mission: Scout records the sequence and commands a verified LOITER. " +
      "Control authority stays with the Local Agent.",
    resume: "Resume the mission from the paused waypoint. Authority is re-acquired and " +
      "verified only if it was lost.",
    stop: "End the mission run. Control authority returns to the operator only after Scout " +
      "reports STOPPED with a verified LOITER.",
    rearm: "Prepare the Local Agent's mission-execution controller for another run. Issues no " +
      "vehicle command, changes no mode, clears no Pixhawk mission.",
    "take-control": "Take Control — request OPERATOR authority as an explicit manual override.",
  };

  // (shortMissionId lives in lib/mission-execution.js now; the local authorityLabel() helper is
  // gone with the Agent Mission card's Authority row — the Status area is the one authority
  // readout in this inspector.)

  function commsTimeline() {
    const h = commsHist;
    const clk = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>';
    if (!h || !Array.isArray(h.transitions) || !h.transitions.length)
      return `<div class="no-telem-box">${clk}No communication transitions recorded yet</div>`;
    const rows = h.transitions.slice(-6).reverse().map((t) => {
      const color = COL[commState(t.state)] || COL.unknown;
      const tm = t.ts ? new Date(t.ts).toLocaleTimeString([], { hour12: false }) : "—";
      const from = t.from ? `${t.from} → ` : "";
      return `<div class="ev"><span class="sv" style="background:${color}"></span><span class="tm">${tm}</span><span class="tx">${from}${t.state}</span></div>`;
    }).join("");
    const disc = h.durations_s && h.durations_s.DISCONNECTED;
    const foot = disc
      ? `<div class="mgrid" style="margin-top:8px"><div><span class="lbl">Total disconnected</span><span class="v txt-d">${Math.round(disc)}s</span></div></div>`
      : "";
    return `<div class="events">${rows}</div>${foot}`;
  }

  function renderInspector() {
    const box = document.getElementById("inspector");
    const v = fleet.find((x) => x.id === selId);
    if (!v) { box.innerHTML = `<div class="isec"><div class="empty-state">No vehicle selected</div></div>`; return; }
    const st = commState(v), stale = st !== "connected", t = v.telemetry || {};
    const events = Array.isArray(v.events) ? v.events.slice(-6).reverse() : [];

    // Control authority — a dedicated fetch fed through the authority controller
    // (pending → confirmed/rejected/timeout), NOT fleet data and NOT the command queue.
    //   Take Control  → request OPERATOR (operator holds the wheel; commands enabled)
    //   Release Control → request LOCAL_AGENT (autonomy resumes)
    // Vehicle/agent commands stay disabled unless authority is a CONFIRMED OPERATOR —
    // never enabled optimistically on a button press or while a request is in flight.
    const av = authCtl.view();
    const authVal = stale ? null : av.value;
    // One authored policy for both the write-enable and the hand-off affordances
    // (lib/authority.js) — never re-derived here. See the strict-ownership contract there.
    const { canTake, canRelease } = handoffGate(av, { stale });

    // Deployment interlock context — Vehicle Home is set + shown from the Pixhawk
    // Mission card (renderPxm); this gateCtx is only for the command gating below.
    const gateCtx = homeGateCtx(v);

    // SECTION ORDER IS A PRODUCT DECISION, pinned by tests/map-inspector.test.mjs:
    //   vehicle header → Status → Vehicle Commands → Agent Mission → Vehicle readiness →
    //   secondary information.
    // Vehicle Commands sits DIRECTLY below Status because it is the primary immediate manual
    // control — the mode/ARM row an operator reaches for without reading anything else. Agent
    // Mission follows it, never precedes it: supervising the agent is the second question, and
    // a card that pushes the manual controls below the fold puts the slower answer first.
    // Authority appears exactly once, in Status.
    box.innerHTML = `
      <div class="isec">
        <div class="idcard">
          <div class="idtop">
            <div class="idrow"><span class="idname">${v.name || "USV-" + v.id}</span>${CommsPill(v, { full: true })}</div>
            <div class="idassign">${String(activity(v))}</div>
          </div>
          <div class="idcontact ${stale ? "warn" : ""}">
            <span class="big txt-${cls(v)}">${v.last_seen_age_s == null ? "—" : Math.round(v.last_seen_age_s)}</span><span class="u">s ago</span>
            <span class="cap"><span class="lbl">Last contact</span><span class="mono" style="font-size:11px;color:var(--muted)">${v.online ? "online" : "—"}</span></span>
          </div>
        </div>
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Status</span></div>
        ${StatusBadges(v, authVal, { phase: av.phase, pending: av.pending })}
        ${authStatusNote(av, stale)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Vehicle Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">Pixhawk · state reported by vehicle</span></div>
        ${vehicleCommands(gateCtx, av, stale)}
        ${takeControl(av, stale, canTake)}
        <details class="adv-auth">
          <summary>Advanced authority</summary>
          <div class="qa" style="margin-top:8px">
            <button data-authority="LOCAL_AGENT" ${canRelease ? "" : "disabled"} title="Release Control — hand authority back to the Local Agent">Release Control</button>
          </div>
          <div class="auth-note">Normal mission operation does not need this. Start, Resume and
            Stop transfer and verify authority themselves as part of the same operation.</div>
        </details>
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Agent Mission</span></div>
        ${renderAgentMission(v)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Vehicle readiness</span></div>
        ${renderReadiness(gateCtx, authVal)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Agent status</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">supervisory · local agent</span></div>
        ${agentCommands(gateCtx, av, stale, v)}
      </div>
      <div class="isec">
        <div class="tele ${stale ? "stale" : ""}">
          <div class="cell full batt"><div class="k">Battery</div><div style="margin-top:5px">${BatteryBar(v.battery)}</div></div>
          <div class="cell"><div class="k">Ground speed</div><div class="v">${v.speed == null ? "—" : v.speed}<small> ${v.speed == null ? "" : "m/s"}</small></div></div>
          <div class="cell"><div class="k">Heading</div><div class="v">${v.heading == null ? "—" : pad3(v.heading)}<small>${v.heading == null ? "" : "°"}</small></div></div>
          <div class="cell"><div class="k">Latitude</div><div class="v" style="font-size:13px">${v.lat != null ? (+v.lat).toFixed(5) : "—"}</div></div>
          <div class="cell"><div class="k">Longitude</div><div class="v" style="font-size:13px">${v.lng != null ? (+v.lng).toFixed(5) : "—"}</div></div>
          <div class="cell"><div class="k">Mode</div><div class="v" style="font-size:13px">${stale ? "UNKNOWN" : (t.mode || "—")}</div></div>
        </div>
        ${stale ? `<div class="stale-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>Telemetry as of ${fmtAge(v.last_seen_age_s)} ago — not live</div>` : ""}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Communication · transitions</span></div>
        ${commsTimeline()}
      </div>
      <div class="isec" style="border-bottom:none">
        <div class="sec-title"><span class="lbl">Recent events</span></div>
        <div class="events">${events.length ? events.map((e) => `<div class="ev"><span class="sv" style="background:var(--muted)"></span><span class="tx">${normEvent(e)}</span></div>`).join("") : '<div class="empty-state">No recent events</div>'}</div>
      </div>`;

    box.querySelectorAll(".qa button[data-authority]").forEach((btn) => {
      btn.onclick = async () => {
        const target = btn.dataset.authority;
        if (target === "OPERATOR") {
          const ok = await confirmModal({
            title: "Take Control?",
            bodyHtml: `<p>This requests OPERATOR authority for <b>${v.name || "USV-" + v.id}</b> so operator commands can execute.</p><p>This does <b>not</b> arm the vehicle or change its mode.</p>`,
            cancelLabel: "Cancel", confirmLabel: "Take Control",
          });
          if (!ok) return;
        }
        authCtl.request(target, (a) => api.setControlAuthority(v.id, a));
      };
    });
    box.querySelectorAll("button[data-cmd]").forEach((btn) => {
      btn.onclick = () => sendCommand(btn.dataset.cmd);
    });
    box.querySelectorAll("button[data-mx]").forEach((btn) => {
      btn.onclick = () => onMissionAction(btn.dataset.mx, v);
    });
    // The EXPLICIT preflight refresh: one read-only backend call, on demand. This is the only
    // operator-facing way the preflight runs on request, and it exists precisely so that not
    // polling it costs the operator nothing.
    box.querySelectorAll("button[data-mx-refresh]").forEach((btn) => {
      btn.onclick = () => refreshPreflight(v.id, "manual");
    });
    // Retry Agent Sync. Calls the package-sync endpoint ONLY — that route builds and sends a
    // planning package and contains no code that can command the vehicle, so this can never
    // re-upload the mission the flight controller already carries.
    box.querySelectorAll("button[data-mx-sync]").forEach((btn) => {
      btn.onclick = () => retryPackageSync(v.id);
    });
  }

  /** Re-send ONLY the Agent planning package for `id`, then re-read the publication state and
   *  the one-shot preflight so the card shows the new verdict rather than the old one. */
  function retryPackageSync(id) {
    if (id == null || mission.syncing) return;
    mission.syncing = true;
    renderInspector();
    api.syncReplanPackage(id, {}).catch((e) => {
      logQuiet("planning-package sync", e);
    }).finally(() => {
      mission.syncing = false;
      if (id === selId) refreshPreflight(id, "mission");
      renderInspector();
    });
  }

  // ONE user intent → ONE operator endpoint. The authority hand-off is part of that endpoint's
  // transaction, so there is deliberately no "release control, then start" sequence here.
  async function onMissionAction(action, v) {
    if (mission.busy) return;              // synchronous double-submit guard (buttons are also
                                           // rendered disabled from the same flag)
    const vname = v ? (v.name || "USV-" + v.id) : "vehicle";
    if (action === "start") {
      const ok = await confirmModal({
        title: "Start Mission?",
        bodyHtml: `<p>Start the agent mission on <b>${esc(vname)}</b>.</p>
          <p>Control authority is transferred to the <b>Local Agent</b> and verified first — you
          do not need to release control yourself.</p>
          <p>Scout then holds position, sets the <b>current launch position as Home</b>, verifies
          it, synchronizes the planning package and starts AUTO. <b>The originally planned Home
          is not retained.</b></p>`,
        cancelLabel: "Cancel", confirmLabel: "Start Mission",
      });
      if (!ok) return;
      missionTransaction("Start Mission", action, (id) => api.startMissionExecution(id, {}));
      return;
    }
    if (action === "pause") {
      missionTransaction("Pause", action, (id) => api.pauseMissionExecution(id));
      return;
    }
    if (action === "resume") {
      missionTransaction("Resume Mission", action, (id) => api.resumeMissionExecution(id));
      return;
    }
    if (action === "stop") {
      const ok = await confirmModal({
        title: "Stop Mission?",
        bodyHtml: `<p>End the mission run on <b>${esc(vname)}</b>.</p>
          <p>Scout holds position and settles in STOPPED. This does <b>not</b> disarm the
          vehicle, clear the Pixhawk mission, delete the planning package or invoke RTL.</p>
          <p>Control authority returns to you only once Scout reports STOPPED with a verified
          LOITER.</p>`,
        cancelLabel: "Cancel", confirmLabel: "Stop Mission",
      });
      if (!ok) return;
      missionTransaction("Stop Mission", action, (id) => api.stopMissionExecution(id));
      return;
    }
    if (action === "rearm") {
      const ok = await confirmModal({
        title: "Rearm the mission controller?",
        bodyHtml: `<p>This prepares the Local Agent's mission-execution controller for another
          run.</p><p>It issues <b>no</b> vehicle command, changes <b>no</b> mode, clears
          <b>no</b> Pixhawk mission and re-uploads <b>no</b> mission.</p>`,
        cancelLabel: "Cancel", confirmLabel: "Rearm",
      });
      if (!ok) return;
      missionTransaction("Rearm controller", action, (id) => api.rearmMissionExecution(id));
      return;
    }
    if (action === "take-control") {
      const ok = await confirmModal({
        title: "Take Control?",
        bodyHtml: `<p>This requests OPERATOR authority for <b>${esc(vname)}</b> so operator commands can execute.</p><p>This does <b>not</b> arm the vehicle or change its mode.</p>`,
        cancelLabel: "Cancel", confirmLabel: "Take Control",
      });
      if (!ok) return;
      authCtl.request("OPERATOR", (a) => api.setControlAuthority(v.id, a));
    }
  }

  // THE ONE authority narration in the inspector, and it sits beside Status — the same place
  // the AuthoritySeg shows the value. It reports only what the segmented badge cannot:
  // a hand-off in flight, a refused/timed-out request, and the freshness caveats that make the
  // displayed value last-known rather than current.
  //
  // A SETTLED OPERATOR or LOCAL_AGENT deliberately produces NOTHING. "Operator holds control"
  // under a badge already reading Operator is the duplication this refinement removes, and the
  // rest of the inspector must not repeat this line either — one warning, one place.
  //
  // Freshness is unchanged: the value shown is still whatever the controller confirmed (Scout's
  // effective authority), StatusBadges still renders UNKNOWN outright once the vehicle is
  // operationally stale, and nothing here promotes a stale value to a current one — it marks it.
  function authStatusNote(av, stale) {
    const line = (cls, text, title) =>
      `<div class="auth-note ${cls}" title="${escAttr(title || "")}">${esc(text)}</div>`;
    const p = av.pending;
    if (p && p.phase === "pending") {
      return line("pending", "Authority change requested…",
        `Requesting ${p.requested} authority — awaiting confirmation from the vehicle. The ` +
        "displayed authority stays at the last confirmed value until Scout confirms the change.");
    }
    if (p && p.phase === "rejected") {
      return line("warn", "Authority request rejected", p.reason || "The request was not accepted.");
    }
    if (p && p.phase === "timeout") {
      return line("warn", "Authority request timed out",
        p.reason || "No effective-authority confirmation arrived from the vehicle.");
    }
    if (stale) {
      return line("warn", "Authority not current",
        "Telemetry is stale, so the effective authority cannot be confirmed. Manual commands " +
        "stay locked until the link is current.");
    }
    if (!av.available) {
      return line("", "No authority source", "This vehicle reports no control-authority source.");
    }
    if (!av.reachable) {
      return line("warn", "Authority unconfirmed",
        "The control-authority service is unreachable. The value shown is the last one Scout " +
        "confirmed and is not being refreshed.");
    }
    if (av.value === "RC") {
      return line("warn", "RC override active",
        "An RC transmitter holds physical control. Software writes stay disabled until it releases.");
    }
    return "";   // settled OPERATOR / LOCAL_AGENT — the Status badge already says so
  }

  // The explicit manual override, kept as ONE compact button immediately below the manual
  // vehicle commands it unlocks. Shown whenever the operator does not already hold confirmed
  // control — which is exactly the LOCAL_AGENT (and RC, and unknown) case. It is never hidden
  // when merely un-pressable: a disabled button with a reason is what tells the operator the
  // override exists and why it cannot be used right now.
  //
  // When OPERATOR already holds authority there is nothing to take, so the row disappears
  // entirely rather than becoming a second Control Owner card — the Status badge is the
  // ownership display.
  function takeControl(av, stale, canTake) {
    const { hasControl } = handoffGate(av, { stale });
    if (hasControl) return "";
    const title = canTake
      ? "Take Control — request OPERATOR authority so you can command the vehicle directly. " +
        "This does not arm the vehicle or change its mode."
      : stale ? "Take Control is unavailable — the link is not current, so a hand-off could " +
          "not be confirmed."
      : av.phase === "pending" ? "An authority request is already in flight."
      : av.available === false ? "This vehicle reports no control-authority source."
      : "Take Control is unavailable right now.";
    return `<div class="qa" style="margin-top:9px">
      <button data-authority="OPERATOR" ${canTake ? "" : "disabled"} title="${escAttr(title)}">Take Control</button>
    </div>`;
  }

  // A row of command buttons. Each button is gated by the shared commandGate policy
  // (lib/home.js): confirmed OPERATOR authority first, then the Home-verification
  // interlock for AUTO / RTL / RESUME. LOITER is NEVER Home-gated — it stays enabled on
  // control alone (a critical anti-drift safety action). ARM/DISARM keep their existing
  // behaviour. High-risk ones carry the caution style + a confirmation.
  function cmdBtns(items, gateCtx) {
    return `<div class="ctl-cmds${gateCtx.hasControl ? "" : " locked"}">` +
      items.map(([type, label]) => {
        const hr = HIGH_RISK.has(type);
        const safety = isSafetyHold(type);       // LOITER — the primary anti-drift safety hold
        const g = commandGate(type, gateCtx);
        // A same-type command already in flight (POST pending, or a nonterminal record in
        // the queue) suppresses a duplicate press. The button stays VISIBLE — LOITER must
        // remain an at-a-glance safety option — but disabled with an "awaiting result"
        // hint until the outstanding command reaches a terminal state.
        const busy = sending.has(type) || hasPendingOfType(cmds, type);
        const dis = !g.enabled || busy;
        const homeLocked = !g.enabled && !!g.reason;   // disabled specifically by the Home interlock
        const title = busy ? `${label} already sent — awaiting the vehicle's result.`
          : g.reason || (safety ? SAFETY_HOLD_TITLE : `${type}${hr ? " · confirmation required" : ""}`);
        return `<button class="ctl-cmd${hr ? " hr" : ""}${safety ? " safety" : ""}${homeLocked ? " home-locked" : ""}${busy ? " awaiting" : ""}" data-cmd="${type}"${dis ? " disabled" : ""} title="${title.replace(/"/g, "&quot;")}">${label}</button>`;
      }).join("") + `</div>`;
  }
  // Why the command row is disabled — the cause, not a second authority readout. It names the
  // action that fixes it (Take Control, immediately below) and leaves "who holds the wheel" to
  // the Status area.
  function lockNote(hasControl, av, stale) {
    if (hasControl) return "";
    const why = stale ? "Link not current" : "Commands are locked";
    return `<div class="ctl-lock-note" title="Vehicle commands execute only on a Scout-confirmed OPERATOR authority.">${lockSvg}<span>${why} — Take Control to enable.</span></div>`;
  }
  // Vehicle/Pixhawk commands ONLY — real ArduRover modes + the ARM/DISARM safety pair.
  // Independent of the local agent. The "Last command" line reports the queue lifecycle
  // — scoped to VEHICLE-type commands only (cmdStatus filters by type) so a queued
  // MISSION_PAUSE/RESUME (an Agent command) can never show up here misattributed as a
  // Pixhawk mode change. Home-gated buttons (AUTO/RTL) explain themselves via their own
  // hover title (commandGate's reason, e.g. "Set and verify Home before AUTO.") — the
  // Deployment readiness card above is the one persistent Home indicator; no second
  // banner here.
  function vehicleCommands(gateCtx, av, stale) {
    return cmdBtns(MAP_VEHICLE, gateCtx) + lockNote(gateCtx.hasControl, av, stale) + cmdStatus(MAP_VEHICLE_TYPES);
  }

  // Agent/supervisory panel: the agent's current and immediately-previous status (no long
  // history here; the Agent page owns the full reasoning view). Current status is approximated
  // from mission_state (LIVE while connected, LAST KNOWN when stale); the previous status needs
  // an onboard decision log the agent does not emit yet → honest gap.
  //
  // The mission LIFECYCLE controls are in the Agent Mission card above — this page is the normal
  // operational surface, so Start / Pause / Resume / Stop belong here and nowhere else. The
  // Agent page keeps the diagnostic depth (identity, hashes, Home evidence, sequence evidence,
  // replanning FSM, return evidence, test injection, advanced reset) and no longer carries the
  // normal controls, so two surfaces can never disagree about whether the mission is running.
  function agentCommands(gateCtx, av, stale, v) {
    return agentStatusBlock(v) + cmdStatus(MAP_MISSION_TYPES);
  }

  function agentStatusBlock(v) {
    const connected = commState(v) === "connected";
    const age = v.last_seen_age_s, hasContact = age != null;
    const raw = v.status || (v.mission_data && v.mission_data.mission_state) || null;
    const behavior = (hasContact && raw && !["unknown", "lost"].includes(String(raw).toLowerCase())) ? raw : null;
    const curSlot = behavior
      ? availSlot(connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN, { value: behavior, age: connected ? null : age })
      : availSlot(AVAIL.GAP, { label: hasContact ? "No data" : "No contact", dev: "agent status approx. from mission_state" });
    const prevSlot = availSlot(AVAIL.GAP, { label: "Unavailable", dev: "agent must emit a decision/status log for the previous state" });
    return `<div class="agent-status">
      <div class="asrow"><span class="k">Current agent status</span><span class="v">${curSlot}</span></div>
      <div class="asrow"><span class="k">Previous agent status</span><span class="v">${prevSlot}</span></div>
    </div>`;
  }

  // Most-relevant command's lifecycle phase (requested/pending/acknowledged/confirmed/
  // rejected/timeout), reported by the vehicle via the queue — never assumed. Kept
  // compact: TYPE + phase pill only. Any failure/rejection detail is a hover tooltip on
  // the row, never permanent inline text — the operator reads "SET_HOME FAILED" at a
  // glance and hovers for why.
  function cmdStatus(types) {
    const scoped = types ? cmds.filter((c) => types.has(c.type)) : cmds;
    if (!scoped.length) return "";
    const c = scoped.find((x) => !CMD_TERMINAL_M.has(x.status)) || scoped[0];
    const v = commandVerification(c);
    // An outer status EXECUTED only means "the Local Agent completed the attempt". The
    // terminal pill uses the SHARED normalized outcome (commandVerification, lib/command.js):
    // VERIFIED/EXECUTED read green, FAILED/REJECTED/EXPIRED red — so a SET_HOME/RTL/
    // MISSION_UPLOAD that transported but did not verify shows "failed", never an
    // optimistic green. A still-running command shows its lifecycle phase instead.
    let phase, k;
    if (CMD_TERMINAL_M.has(c.status)) [phase, k] = CMD_OUTCOME[v.outcome] || ["—", "u"];
    else [phase, k] = CMD_PHASE[c.status] || ["—", "u"];
    // Full compact progression: the lifecycle stages this command actually passed through.
    const prog = commandStages(c).map((s) => STAGE_SHORT[s.stage] || String(s.stage).toLowerCase()).join(" › ");
    const eo = (v.expected != null || v.observed != null) ? `${v.expected ?? "—"} → ${v.observed ?? "—"}` : "";
    const note = v.reason || c.warning || "";
    const title = note ? ` title="${note.replace(/"/g, "&quot;")}"` : "";
    return `<div class="cmd-status"${title}><span class="lbl">Last command</span><span class="ctl-type mono">${CMD_LABEL[c.type] || c.type}</span><span class="pill ${k}">${phase}</span>` +
      `${prog ? `<div class="cmd-prog mono">${prog}${eo ? ` · ${eo}` : ""}</div>` : ""}</div>`;
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function select(id) {
    if (id !== selId) {
      selId = id; commsHist = null; loadCommsHistory(id);
      authCtl.reset(); loadAuthority(id);
      cmds = []; loadCommands(id);
      // Per-USV lifecycle state: never carry one Scout's mission state (or the outcome of a
      // transaction issued against it) onto another vehicle's card.
      mission.status = mission.preflight = mission.result = null;
      mission.forVid = mission.preflightFor = mission.preflightAt = mission.preflightReason = null;
      loadMissionStatus(id);
      // Initial vehicle selection is one of the allowed one-shot preflight moments.
      refreshPreflight(id, "selection");
      // Share the selection so the Plan page (and any other page) follows the operator to
      // this vehicle, and hand the id to the refresh controller — which immediately reads
      // the latest telemetry-adjacent mission overlay and ignores any late reply from the
      // vehicle we just switched away from.
      setSelectedVehicleId(id);
      refreshController.select(id);
      // Set-Home phase is per-vehicle — never carry a pending/failed flash (or a
      // tracked command id) across a switch.
      if (setHomeTimer) { clearTimeout(setHomeTimer); setHomeTimer = null; }
      setHome = { phase: "idle", code: null, message: null, at: 0, cmdId: null };
    }
    // Snap the map to the selected vehicle (only if it has a known position — a
    // never-contacted vehicle has none, so there is nothing to snap to).
    const v = fleet.find((x) => x.id === id);
    if (map && v && v.lat != null && v.lng != null) map.panTo([v.lat, v.lng]);
    renderDock(); renderPxm(); syncMissionOverlay(); renderInspector(); updateMarkers(); updateHomeMarker();
  }

  // Default to a vehicle that is actually reporting (so the primary view opens on a
  // live vehicle with a real authority source) rather than blindly the first template
  // row — picking a placeholder vehicle 1 is what made the authority poll hit an
  // unconfigured id and 404 in a loop.
  function defaultSelection() {
    return (fleet.find((v) => v.online) || fleet.find((v) => v.lat != null) || fleet[0]).id;
  }

  function onFleet(data) {
    // Merge each vehicle against its own last-known telemetry BEFORE it replaces the fleet —
    // a null battery/speed/heading in this poll keeps the previous valid value instead of
    // rendering "—". Freshness is untouched (comm_state/last_seen_age_s carry through), so a
    // retained value is still marked stale when the link degrades.
    fleet = telemCache.mergeFleet(Array.isArray(data) ? data : []);
    detectReconnect();
    if (selId == null && fleet.length) {
      // First fleet payload: adopt the shared selection if it still names a real vehicle,
      // else fall back to a reporting vehicle. Routing through select() gives the controller
      // the immediate mission read too.
      select(resolveInitialSelection());
    } else if (selId != null) {
      // Mission-revision auto-refresh: when the fleet feed reports a changed mission-revision
      // signal for the SELECTED vehicle, the controller refetches the full mission (and skips
      // it when the signal is unchanged). Fed by Scout's mission-execution status as well as the
      // fleet payload — see revisionSignalFor / lib/replan.js missionRevisionSignal.
      noteRevisionEvidence(selId);
    }
    updateMarkers(); renderDock(); renderPxm(); renderInspector(); updateHomeMarker();
    updateRibbon({ counts: counts() });
  }

  // A vehicle that has just come back from a disconnect is the last of the non-polling preflight
  // moments: the link was down, so whatever the last preflight said about the Pixhawk read-back
  // is simply old. ONE read on the transition — never a recurring one while it stays connected,
  // and never one while it stays disconnected either.
  function detectReconnect() {
    fleet.forEach((v) => {
      const now = commState(v);
      const was = lastCommState[v.id];
      lastCommState[v.id] = now;
      if (was && was !== "connected" && now === "connected" && v.id === selId) {
        refreshPreflight(v.id, "reconnect");
      }
    });
  }

  // Prefer the shared cross-page selection when it still names a vehicle in the current
  // fleet; otherwise pick a vehicle that is actually reporting.
  function resolveInitialSelection() {
    const shared = getSelectedVehicleId();
    if (shared != null && fleet.some((v) => v.id === shared)) return shared;
    return defaultSelection();
  }
  function onEnv(e) {
    env = e || {};
    const s = document.getElementById("wind-spd"), a = document.getElementById("wind-arrow"), f = document.getElementById("wind-frm");
    if (env.wind_speed == null || env.wind_direction == null) { s.textContent = "No data"; a.style.opacity = ".3"; f.textContent = ""; }
    else { s.textContent = (+env.wind_speed).toFixed(1) + " m/s"; a.style.opacity = "1"; a.style.transform = `rotate(${(+env.wind_direction + 180) % 360}deg)`; f.textContent = `from ${env.wind_direction}°`; }
  }

  // Operator Link — the fleet feed's real success/failure, tracked centrally in
  // api.js (poll's "fleet" key) and never guessed here. Distinct from any single
  // vehicle's comm_state: this answers "is the operator station itself still hearing
  // from the backend at all", which every page's fleet poll previously answered with
  // silence on failure (see the operational review — this was the top Critical gap).
  function feedIndicator() {
    const h = api.getFeedHealth("fleet");
    if (!h || (h.lastOkAt == null && h.lastErrAt == null))
      return { cls: "dim", label: "CONNECTING…", title: "Waiting for the first response from the operator backend." };
    if (h.lastOkAt == null)
      return { cls: "bad", label: "BACKEND UNREACHABLE", title: "No successful response yet from the operator backend — check the network and backend process." };
    const ageS = (Date.now() - h.lastOkAt) / 1000;
    if (ageS <= 4) return { cls: "ok", label: "LIVE", title: "Operator backend responding normally." };
    if (ageS <= 12) return { cls: "warn", label: `DELAYED ${Math.round(ageS)}s`, title: "The last successful fleet update was more than a few seconds ago — displayed data may be stale." };
    return { cls: "bad", label: `UNREACHABLE ${Math.round(ageS)}s`, title: "The operator backend has not responded in over 12 seconds — displayed data is stale and commands may not reach it." };
  }
  function updateFeedIndicator() { updateRibbon({ feed: feedIndicator() }); }

  // polling + clock
  // The fleet feed is the lightweight selected-vehicle refresh (position/battery/comms/mode
  // for the whole roster in one call). It pauses while the tab is hidden — a backgrounded
  // operator tab stops polling the backend/Scout and resumes on the next visible tick.
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, updateFeedIndicator, "fleet", { pauseWhenHidden: true });
  const stopEnv = api.poll(api.getEnvironment, 10000, onEnv, () => {}, null, { pauseWhenHidden: true });
  // Start the shared mission-refresh controller (fallback safety re-read + command/revision
  // triggers; the immediate read fires from select()).
  refreshController.start();
  // Resume promptly when the tab is refocused rather than waiting for the next interval.
  const onVisible = () => { if (!document.hidden) refreshController.tick(); };
  document.addEventListener("visibilitychange", onVisible);
  timers.push(setInterval(() => loadCommsHistory(selId), 3000));  // refresh selected vehicle's comms log
  timers.push(setInterval(() => loadAuthority(selId), 2000));  // refresh selected vehicle's control authority
  timers.push(setInterval(() => loadCommands(selId), 3000));  // refresh selected vehicle's command lifecycle
  // Agent Mission: Scout's canonical lifecycle STATUS only — state, mode, waypoint progress,
  // active operation, explicit replanning. READ-ONLY: this poll can never issue a lifecycle
  // operation, and it deliberately does NOT call the Start preflight, whose ~10 s read-back
  // recomputation is what made the card flicker between READY and NOT_READY. 3 s keeps the
  // card's state, progress and button set current at a fraction of the Scout traffic.
  timers.push(setInterval(() => loadMissionStatus(selId), 3000));
  // Watchdog for an in-flight Set Home. loadCommands() already resolves it on every
  // poll, but the poll is exactly what stops when the operator backend is unreachable —
  // and that is a case the flash must still time out of, so the deadline is also
  // evaluated here, independent of any feed. No-ops unless a request is pending.
  timers.push(setInterval(syncSetHomeFromCommands, 1000));
  timers.push(setInterval(tickPxmAge, 1000));  // keep the "last fetch … ago" line live
  const clockId = setInterval(() => {
    updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
    updateFeedIndicator();
  }, 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
  updateFeedIndicator();

  return function cleanup() {
    stopFleet(); stopEnv(); clearInterval(clockId); timers.forEach(clearInterval);
    refreshController.stop();
    document.removeEventListener("visibilitychange", onVisible);
    if (setHomeTimer) { clearTimeout(setHomeTimer); setHomeTimer = null; }
    authCtl.dispose();
    clearMissionOverlay();
    detachMapLayout();
    if (map) { map.remove(); map = null; }
  };
}
