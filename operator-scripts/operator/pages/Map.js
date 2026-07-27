// Map.js — first migrated page. Full-bleed Leaflet map + live fleet from api.js,
// left dock (roster + mission progress), right inspector. Reuses shared components.
// IA first: markers/roster/inspector wired to real data; NO-TELEM slots where the
// backend can't back the mockup (comms timeline, mission ETA/remaining).
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
import { StatusBadges } from "../components/StatusBadges.js";
import { AuthoritySeg } from "../components/AuthoritySeg.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { COL, cls, commState, fmtAge, pad3, noTelem, opsStale } from "../lib/ui.js";
import { createAuthorityController, handoffGate } from "../lib/authority.js";
import { AVAIL, availSlot } from "../lib/availability.js";
import { homeStatus, commandGate, commandGateCtx, deploymentReadiness, fmtDistance, fmtAgo, isSafetyHold, SAFETY_HOLD_TITLE, setHomeOutcome } from "../lib/home.js";
import { commandVerification, hasPendingOfType, commandStages } from "../lib/command.js";
import { classifyMissionWaypoints, missionCounts, remainingRouteDistanceM, etaSeconds, fmtDuration } from "../lib/mission.js";
import { getSelectedVehicleId, setSelectedVehicleId } from "../lib/selection.js";
import { createSelectedRefresh } from "../services/selected-refresh.js";
import { MISSION_WRITE_COMMANDS, missionWriteNeedsRefetch } from "../lib/mission-refresh.js";

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
const MAP_MISSION = [["MISSION_PAUSE", "PAUSE MISSION"], ["MISSION_RESUME", "RESUME MISSION"]];
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
  let commsHist = null;          // comms-state transition log for the selected vehicle
  let cmds = [];                 // command queue + history for the selected vehicle
  // Control-authority state machine (pending → confirmed/rejected/timeout). Re-renders
  // the inspector on every phase change so the display always reflects the effective
  // authority confirmed by Scout, never the button press.
  const authCtl = createAuthorityController(() => renderInspector());
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
      if (id === selId) {
        // Progress text (current WP / %) always refreshes; the map overlay is rebuilt only
        // when the GEOMETRY changed OR execution PROGRESS (current_seq) moved — unchanged
        // geometry at the same progress is never needlessly redrawn.
        renderPxm(); renderDock();
        if ((meta.geometryChanged || meta.progressChanged) && pxmState(id).shown) drawMissionOverlay(id);
      }
    },
    onError: (kind, id, err) => {
      if (kind === "mission" && id === selId) { pxmState(id).note = "error"; renderPxm(); }
      logQuiet(kind, err);
    },
    intervalMs: 5000,          // cadence at which the fallback deadline is checked
    missionFallbackMs: 20000,  // full mission re-read at most this often absent a real trigger
  });

  // The mission-revision signal for a vehicle, read off the fleet payload IF the backend
  // surfaces one (active_revision_id / active_route_hash / mission_changed_at — see main.py's
  // fleet-payload extension point). None exists today, so this returns undefined and the
  // "revision" trigger stays dormant until Scout/the backend reports it — no fabricated signal.
  function missionRevisionSignal(v) {
    if (!v) return undefined;
    const md = v.mission_data || {};
    return md.active_revision_id ?? md.active_route_hash ?? md.mission_changed_at
      ?? v.active_revision_id ?? v.route_hash ?? undefined;
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

  // Leaflet
  map = L.map("map", { zoomControl: true, attributionControl: false }).setView(HOME, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20 }).addTo(map);
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
    list.querySelectorAll(".vrow").forEach((el) => (el.onclick = () => select(+el.dataset.id)));

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
    return pxm[id] || (pxm[id] = { mission: null, fetchedAt: 0, loading: false, note: null, shown: false });
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
    try {
      const res = await api.getPixhawkMission(id);
      applyMissionRead(id, res);
      if (res && res.reachable) refreshController.tracker.noteFetched(id, res);
    } catch (e) {
      s.note = "error";
    } finally {
      s.loading = false;
      if (id === selId) { renderPxm(); renderDock(); if (s.shown) drawMissionOverlay(id); }
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

  function showMissionOverlay() {
    if (selId == null) return;
    const s = pxmState(selId);
    s.shown = true; drawMissionOverlay(selId); renderPxm();
  }
  function hideMissionOverlay() {
    if (selId == null) return;
    pxmState(selId).shown = false; clearMissionOverlay(); renderPxm();
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
    if (!s.shown) { s.shown = true; drawMissionOverlay(selId); renderPxm(); }
    const bounds = L.latLngBounds(pts.map((w) => [w.lat, w.lng]));
    map.fitBounds(bounds, { padding: [56, 56], maxZoom: 17 });
  }

  // Switch the overlay to follow the selected vehicle: drop the old one, redraw the new
  // vehicle's mission only if the operator had it shown. Called from select().
  function syncMissionOverlay() {
    clearMissionOverlay();
    const s = selId != null ? pxm[selId] : null;
    if (s && s.shown) drawMissionOverlay(selId);
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

    const hasWps = positionedWaypoints(id).length > 0;
    const shown = !!(s && s.shown);
    const fetched = !!(s && s.fetchedAt);

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
          <button data-pxm="show" ${hasWps && !shown ? "" : "disabled"} title="Show the mission overlay on the map">Show</button>
          <button data-pxm="center" ${hasWps ? "" : "disabled"} title="Fit the map to the mission (no refetch)">Center</button>
          <button data-pxm="hide" ${shown ? "" : "disabled"} title="Hide the mission overlay">Hide</button>
        </div>
      </div>`;

    box.querySelector('[data-pxm="fetch"]').onclick = () => fetchPixhawkMission(selId);
    box.querySelector('[data-pxm="show"]').onclick = () => showMissionOverlay();
    box.querySelector('[data-pxm="center"]').onclick = () => centerMission();
    box.querySelector('[data-pxm="hide"]').onclick = () => hideMissionOverlay();
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
    if (fire) refreshController.refreshMission(id, "command");
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

  function renderReadiness(gateCtx) {
    const r = deploymentReadiness(gateCtx);
    const items = r.items.map((i) =>
      `<div class="rdy-item ${i.ok ? "ok" : "no"}"><span class="rdy-mk">${i.ok ? "✓" : "✕"}</span>${i.label}</div>`).join("");
    const banner = r.ready
      ? `<div class="rdy-banner ok">READY FOR MISSION</div>`
      : `<div class="rdy-banner ${r.loiterAvailable ? "warn" : "dim"}" title="${r.loiterAvailable ? "LOITER remains available as an immediate anti-drift safety hold." : ""}">NOT READY</div>`;
    return `<div class="rdy">${items}</div>${banner}`;
  }

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
    const { canTake, canRelease, hasControl } = handoffGate(av, { stale });

    // Deployment interlock context — Vehicle Home is set + shown from the Pixhawk
    // Mission card (renderPxm); this gateCtx is only for the command gating below.
    const gateCtx = homeGateCtx(v);

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
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Control authority</span>${AuthoritySeg(authVal, { phase: av.phase, pending: av.pending })}</div>
        <div class="qa">
          <button data-authority="OPERATOR" ${canTake ? "" : "disabled"} title="Take Control — request OPERATOR authority">Take Control</button>
          <button data-authority="LOCAL_AGENT" ${canRelease ? "" : "disabled"} title="Release Control — hand authority back to the Local Agent">Release Control</button>
        </div>
        ${authNote(av, stale)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Deployment readiness</span></div>
        ${renderReadiness(gateCtx)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Vehicle Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">Pixhawk · state reported by vehicle</span></div>
        ${vehicleCommands(gateCtx, av, stale)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Agent Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">supervisory · local agent</span></div>
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
  }

  // Pending/rejected/timeout notice for the authority hand-off, plus the honest
  // "no authority source / unknown / RC override" states. Never claims success on a
  // click — only the effective value confirmed by Scout settles it.
  function authNote(av, stale) {
    if (stale) return `<div class="auth-note warn">Authority is UNKNOWN — telemetry is stale. Commands are locked until the link is current.</div>`;
    const p = av.pending;
    if (p && p.phase === "pending") return `<div class="auth-note pending">Requesting ${p.requested === "OPERATOR" ? "OPERATOR" : "LOCAL_AGENT"} authority — awaiting confirmation from the vehicle…</div>`;
    if (p && p.phase === "confirmed") return `<div class="auth-note ok">Authority confirmed: ${av.value}.</div>`;
    if (p && p.phase === "rejected") return `<div class="auth-note warn">Request rejected — ${p.reason || "not accepted"}.</div>`;
    if (p && p.phase === "timeout") return `<div class="auth-note warn">Request timed out — ${p.reason || "no confirmation"}.</div>`;
    if (!av.available) return `<div class="auth-note">No control-authority source for this vehicle.</div>`;
    if (!av.reachable) return `<div class="auth-note warn">Control-authority service unreachable — authority is UNKNOWN.</div>`;
    if (av.value === "RC") return `<div class="auth-note warn">RC transmitter override is active — it holds physical control.</div>`;
    if (av.value === "OPERATOR") return `<div class="auth-note ok">Operator holds control — commands enabled.</div>`;
    if (av.value === "LOCAL_AGENT") return `<div class="auth-note">Local Agent holds control — Take Control to command directly.</div>`;
    return "";
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
  function lockNote(hasControl, av, stale) {
    if (hasControl) return "";
    return `<div class="ctl-lock-note">${lockSvg}<span>${stale ? "Link not current" : av.value === "OPERATOR" ? "" : "Commands are locked"} — Take Control (OPERATOR, Scout-confirmed) to enable.</span></div>`;
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

  // Agent/supervisory commands ONLY — pause/resume the mission the local agent runs —
  // plus the agent's current and immediately-previous status (no long history here;
  // the Agent page owns the full reasoning view). Current status is approximated from
  // mission_state (LIVE while connected, LAST KNOWN when stale); the previous status
  // needs an onboard decision log the agent does not emit yet → honest gap. "Last
  // command" here is scoped to MISSION-type commands only, mirroring vehicleCommands.
  function agentCommands(gateCtx, av, stale, v) {
    return cmdBtns(MAP_MISSION, gateCtx) + lockNote(gateCtx.hasControl, av, stale) + agentStatusBlock(v) + cmdStatus(MAP_MISSION_TYPES);
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
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) {
      // First fleet payload: adopt the shared selection if it still names a real vehicle,
      // else fall back to a reporting vehicle. Routing through select() gives the controller
      // the immediate mission read too.
      select(resolveInitialSelection());
    } else if (selId != null) {
      // Mission-revision auto-refresh: when the fleet feed reports a changed mission-revision
      // signal for the SELECTED vehicle, the controller refetches the full mission (and skips
      // it when the signal is unchanged). Dormant until the backend surfaces such a field —
      // see missionRevisionSignal / main.py's fleet-payload extension point.
      const sig = missionRevisionSignal(fleet.find((x) => x.id === selId));
      if (sig !== undefined) refreshController.refreshMission(selId, "revision", { revisionSignal: sig });
    }
    updateMarkers(); renderDock(); renderPxm(); renderInspector(); updateHomeMarker();
    updateRibbon({ counts: counts() });
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
    if (map) { map.remove(); map = null; }
  };
}
