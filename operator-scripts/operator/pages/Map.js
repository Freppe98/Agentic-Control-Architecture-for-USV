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
import { createAuthorityController } from "../lib/authority.js";
import { AVAIL, availSlot } from "../lib/availability.js";

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
const HIGH_RISK = new Set(["ARM", "DISARM", "RTL", "SET_MODE_AUTO"]);
const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';
// Command lifecycle → the operator-facing phase it represents (requested/pending,
// acknowledged, confirmed effective, rejected, timeout).
const CMD_PHASE = {
  QUEUED: ["requested", "u"], SENT: ["pending", "p"], ACCEPTED: ["acknowledged", "p"],
  EXECUTED: ["confirmed", "c"], REJECTED: ["rejected", "d"], FAILED: ["failed", "d"],
  EXPIRED: ["timed out", "u"],
};
const CMD_LABEL = Object.fromEntries([...MAP_MODES, ...MAP_SAFETY, ...MAP_MISSION]);

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
  const timers = [];
  // Pixhawk mission readback — per-vehicle, view-only. Each entry:
  //   { mission, fetchedAt, loading, note }  (see fetchPixhawkMission / renderPxm)
  // `mission` is the last SUCCESSFUL (reachable) Scout readback and is never wiped by a
  // later failed fetch — a subsequent unreachable attempt only sets `note`, so the card
  // keeps showing the last-known mission with an honest "Scout unavailable" status.
  // `shown` toggles the map overlay WITHOUT refetching (toggling never hits Scout).
  const pxm = {};
  let missionLayer = null;   // the single Leaflet layer group currently drawn (selected vehicle)

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
     </div>
     <aside class="inspector" id="inspector"></aside>`;

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
    return L.divIcon({
      className: "",
      html: `<div class="usv-marker${sel ? " is-sel" : ""}" style="opacity:${stale ? 0.82 : 1}">
        ${sel ? '<div class="selring"></div>' : ""}
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

    // Mission progress — backend has (optional) per-vehicle coverage but no mission-level
    // ETA/remaining or named scope, so those slots are honest NO-TELEM.
    const cov = fleet.map((v) => v.coverage).find((c) => c != null);
    document.getElementById("mprog").innerHTML =
      `<div class="row"><span class="lbl">Mission progress</span></div>` +
      (cov != null
        ? `<div class="top"><span class="lbl">Coverage</span><span class="pct mono">${cov}%</span></div>
           <div class="bar"><i style="width:${cov}%;background:var(--connected)"></i></div>`
        : `<div class="no-telem-box"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 20V10M12 20V4M20 20v-7"/></svg>Coverage not reported</div>`) +
      `<div class="mgrid">
         <div><span class="lbl">Remaining</span><span class="v">${noTelem()}</span></div>
         <div><span class="lbl">ETA</span><span class="v">${noTelem()}</span></div>
       </div>`;
  }

  // ---- Pixhawk mission (view-only readback + map overlay) ------------------
  function pxmState(id) {
    return pxm[id] || (pxm[id] = { mission: null, fetchedAt: 0, loading: false, note: null, shown: false });
  }

  // Fetch the mission stored on the vehicle's Pixhawk (a live Scout proxy). A reachable
  // reply replaces the displayed mission; an unreachable one (or a thrown 404) only sets
  // a note so the last-known mission is preserved. Never called by Show/Hide.
  async function fetchPixhawkMission(id) {
    if (id == null) return;
    const s = pxmState(id);
    s.loading = true; renderPxm();
    try {
      const res = await api.getPixhawkMission(id);
      if (res && res.reachable) {
        s.mission = res; s.fetchedAt = Date.now();
        s.note = res.available === false ? "no-api" : (res.partial ? "partial" : null);
      } else {
        s.note = (res && res.available === false) ? "no-api" : "unreachable";
      }
    } catch (e) {
      s.note = "error";
    } finally {
      s.loading = false;
      if (id === selId) { renderPxm(); if (s.shown) drawMissionOverlay(id); }
    }
  }

  // ---- Mission geometry & HOME detection ----------------------------------
  // Pixhawk stores sequence 0 as a home / current-location item that is NOT part of
  // the stored survey: it typically uses MAV_FRAME_GLOBAL (absolute alt) while the
  // survey legs use MAV_FRAME_GLOBAL_RELATIVE_ALT, and it sits at the vehicle's
  // current position — often kilometres from the survey cluster. We render it as a
  // separate HOME marker, keep it OUT of the route polyline, and EXCLUDE it from the
  // mission-fit bounds so Center frames the survey, not the whole transit. Scout's
  // data is never rewritten — the original seq / frame stay visible in the popup.
  const METERS = (a, b) => {
    const R = 6371000, toR = Math.PI / 180;
    const dLat = (b[0] - a[0]) * toR, dLng = (b[1] - a[1]) * toR;
    const la = a[0] * toR, lb = b[0] * toR;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(la) * Math.cos(lb) * Math.sin(dLng / 2) ** 2;
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  };
  // true = absolute-global frame (home candidate), false = relative/terrain global
  // frame (a survey leg), null = unknown / non-global. Accepts MAVLink ints
  // (0 = GLOBAL, 3/6/10/11 = relative/terrain) or the string spellings Scout forwards.
  function frameIsAbsGlobal(frame) {
    if (frame == null || frame === "") return null;
    if (typeof frame === "number") return frame === 0 ? true : [3, 6, 10, 11].includes(frame) ? false : null;
    const s = String(frame).toUpperCase();
    if (!s.includes("GLOBAL")) return null;
    return !(s.includes("RELATIVE") || s.includes("TERRAIN"));
  }

  // Split a vehicle's positioned waypoints into { home, route } in sequence order. The
  // lowest-seq item is a HOME candidate; it is treated as HOME only when it is clearly
  // not a survey leg — a distinct absolute-global frame, OR a geographic outlier far
  // from the rest of the cluster. Otherwise it stays an ordinary route waypoint, so a
  // mission whose seq 0 genuinely is a normal leg is not mis-split.
  function classifyMission(id) {
    const wps = positionedWaypoints(id).slice()
      .sort((a, b) => (a.seq == null ? 0 : a.seq) - (b.seq == null ? 0 : b.seq));
    if (wps.length < 2) return { home: null, route: wps };
    const cand = wps[0], rest = wps.slice(1);
    const frameSaysHome = frameIsAbsGlobal(cand.frame) === true
      && rest.some((w) => frameIsAbsGlobal(w.frame) === false);
    // geographic outlier: candidate far from the cluster centroid relative to the
    // cluster's own span, and past an absolute floor so a tight cluster still splits.
    const cLat = rest.reduce((s, w) => s + w.lat, 0) / rest.length;
    const cLng = rest.reduce((s, w) => s + w.lng, 0) / rest.length;
    let span = 0;
    for (const w of rest) span = Math.max(span, METERS([cLat, cLng], [w.lat, w.lng]));
    const geoSaysHome = METERS([cand.lat, cand.lng], [cLat, cLng]) > Math.max(span * 3, 400);
    return (frameSaysHome || geoSaysHome) ? { home: cand, route: rest } : { home: null, route: wps };
  }

  // A numbered survey waypoint — a small, discreet marker that never competes with the
  // round comm-colored vehicle dot. The number lives in its own span so it can be
  // hidden at wide zoom (leaving just the dot). The current waypoint gets a stronger
  // ring, not a bigger box.
  function wpIcon(w, isCurrent) {
    return L.divIcon({
      className: "",
      html: `<div class="wp-marker${isCurrent ? " cur" : ""}"><span class="wp-num">${w.seq == null ? "•" : w.seq}</span></div>`,
      iconSize: [20, 20], iconAnchor: [10, 10],
    });
  }
  const homeSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V20h14V9.5"/></svg>';
  function homeIcon(isCurrent) {
    return L.divIcon({
      className: "",
      html: `<div class="home-marker${isCurrent ? " cur" : ""}" title="Home / current position">${homeSvg}</div>`,
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
      <div class="wp-pop-h">${isHome ? "HOME" : `WP ${w.seq == null ? "—" : w.seq}`}${isCur ? '<span class="wp-cur-tag">CURRENT</span>' : ""}</div>
      ${row("Type", isHome ? "Home" : "Waypoint")}
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
    const layer = L.layerGroup();
    if (route.length > 1) {
      L.polyline(route.map((w) => [w.lat, w.lng]),
        { className: "mission-line", color: "#4C8DFF", weight: 1.6, opacity: 0.6, dashArray: "6 7", lineCap: "round" }).addTo(layer);
    }
    route.forEach((w) => {
      L.marker([w.lat, w.lng], { icon: wpIcon(w, isCur(w)), zIndexOffset: isCur(w) ? 1000 : 0 })
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
    // "WP 0" — the operator is not sitting on a survey waypoint 0.
    const mc = m ? classifyMission(id) : { home: null };
    const curText = cur == null ? "—"
      : (mc.home && mc.home.seq === cur) ? `Home (seq ${cur})`
      : (count != null ? `WP ${cur} / ${count}` : `WP ${cur}`);

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

    box.innerHTML = `
      <div class="pxm-h">
        <span class="lbl">Pixhawk mission</span>
        <span class="pxm-chip ${chipCls}">${chipText}</span>
      </div>
      <div class="pxm-grid">
        <div class="pxm-row"><span class="k">Loaded</span><span class="v">${count == null ? "—" : `${count} waypoint${count === 1 ? "" : "s"}`}</span></div>
        <div class="pxm-row"><span class="k">Current</span><span class="v">${curText}</span></div>
        ${integ ? `<div class="pxm-row"><span class="k">Integrity</span><span class="pxm-integ ${integ[1]}">${integ[0]}</span></div>` : ""}
        <div class="pxm-row"><span class="k">Last download</span><span class="v" id="pxm-age">${pxmAgeText(s)}</span></div>
        ${hash ? `<div class="pxm-row"><span class="k">Mission id</span><span class="v" title="${hash}">${hash.slice(0, 8)}</span></div>` : ""}
      </div>
      ${cachedNote}
      <div class="pxm-actions">
        <button data-pxm="fetch" ${s && s.loading ? "disabled" : ""} title="Fetch the mission stored on the Pixhawk">${fetched ? "Refresh" : "Fetch"}</button>
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
      if (id === selId) { cmds = (d && d.commands) || []; renderInspector(); }
    }).catch(() => { if (id === selId) { cmds = []; renderInspector(); } });
  }

  async function sendCommand(type) {
    const v = fleet.find((x) => x.id === selId);
    const vname = v ? (v.name || "USV-" + v.id) : "vehicle";
    const label = CMD_LABEL[type] || type;
    const highRisk = HIGH_RISK.has(type);
    if (highRisk &&
        !window.confirm(`Confirm ${label} for ${vname}?\n\nThe command is queued for the vehicle to execute. It is NOT applied until the local agent reports back.`)) return;
    let res = await api.createCommand({ vehicle_id: selId, type, confirm: highRisk });
    if (!res.ok && res.data && res.data.needs_confirmation) {
      if (!window.confirm(`${res.data.message}\n\nQueue anyway?`)) return;
      res = await api.createCommand({ vehicle_id: selId, type, confirm: true });
    }
    if (!res.ok) window.alert((res.data && res.data.message) || "Command was not accepted.");
    loadCommands(selId);
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
    const busy = av.phase === "pending";
    const hasControl = !stale && av.hasControl;
    const canTake = av.available && !stale && !hasControl && !busy;
    const canRelease = av.available && !stale && av.value === "OPERATOR" && !busy;

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
        <div class="sec-title"><span class="lbl">Vehicle Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">Pixhawk · state reported by vehicle</span></div>
        ${vehicleCommands(hasControl, av, stale)}
      </div>
      <div class="isec">
        <div class="sec-title"><span class="lbl">Agent Commands</span><span class="tag" style="margin-left:auto;font-family:var(--font-mono);font-size:10px;color:var(--dim)">supervisory · local agent</span></div>
        ${agentCommands(hasControl, av, stale, v)}
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
      btn.onclick = () => {
        const target = btn.dataset.authority;
        if (target === "OPERATOR" &&
            !window.confirm(`Take control of ${v.name || "USV-" + v.id}?\n\nThis requests OPERATOR authority so operator commands can execute. It does NOT arm the vehicle or change its mode.`)) return;
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

  // A row of command buttons; the ARM/DISARM etc. high-risk ones carry the caution
  // style + a confirmation. Enabled only on confirmed OPERATOR authority.
  function cmdBtns(items, hasControl) {
    return `<div class="ctl-cmds${hasControl ? "" : " locked"}">` +
      items.map(([type, label]) => {
        const hr = HIGH_RISK.has(type);
        return `<button class="ctl-cmd${hr ? " hr" : ""}" data-cmd="${type}"${hasControl ? "" : " disabled"} title="${type}${hr ? " · confirmation required" : ""}">${label}</button>`;
      }).join("") + `</div>`;
  }
  function lockNote(hasControl, av, stale) {
    if (hasControl) return "";
    return `<div class="ctl-lock-note">${lockSvg}<span>${stale ? "Link not current" : av.value === "OPERATOR" ? "" : "Commands are locked"} — Take Control (OPERATOR, Scout-confirmed) to enable.</span></div>`;
  }

  // Vehicle/Pixhawk commands ONLY — real ArduRover modes + the ARM/DISARM safety pair.
  // Independent of the local agent. The "Last command" line reports the queue lifecycle.
  function vehicleCommands(hasControl, av, stale) {
    return cmdBtns(MAP_VEHICLE, hasControl) + lockNote(hasControl, av, stale) + cmdStatus();
  }

  // Agent/supervisory commands ONLY — pause/resume the mission the local agent runs —
  // plus the agent's current and immediately-previous status (no long history here;
  // the Agent page owns the full reasoning view). Current status is approximated from
  // mission_state (LIVE while connected, LAST KNOWN when stale); the previous status
  // needs an onboard decision log the agent does not emit yet → honest gap.
  function agentCommands(hasControl, av, stale, v) {
    return cmdBtns(MAP_MISSION, hasControl) + lockNote(hasControl, av, stale) + agentStatusBlock(v);
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
  // rejected/timeout), reported by the vehicle via the queue — never assumed.
  function cmdStatus() {
    if (!cmds.length) return "";
    const TERMINAL = ["EXECUTED", "REJECTED", "FAILED", "EXPIRED"];
    const c = cmds.find((x) => !TERMINAL.includes(x.status)) || cmds[0];
    const [phase, k] = CMD_PHASE[c.status] || ["—", "u"];
    const note = c.reason || c.warning || "";
    return `<div class="cmd-status"><span class="lbl">Last command</span><span class="ctl-type mono">${CMD_LABEL[c.type] || c.type}</span><span class="pill ${k}">${phase}</span>${note ? `<span class="cmd-reason" title="${note.replace(/"/g, "&quot;")}">${note}</span>` : ""}</div>`;
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
    }
    // Snap the map to the selected vehicle (only if it has a known position — a
    // never-contacted vehicle has none, so there is nothing to snap to).
    const v = fleet.find((x) => x.id === id);
    if (map && v && v.lat != null && v.lng != null) map.panTo([v.lat, v.lng]);
    renderDock(); renderPxm(); syncMissionOverlay(); renderInspector(); updateMarkers();
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
      selId = defaultSelection();
      loadCommsHistory(selId); loadAuthority(selId); loadCommands(selId);
    }
    updateMarkers(); renderDock(); renderPxm(); renderInspector();
    updateRibbon({ counts: counts() });
  }
  function onEnv(e) {
    env = e || {};
    const s = document.getElementById("wind-spd"), a = document.getElementById("wind-arrow"), f = document.getElementById("wind-frm");
    if (env.wind_speed == null || env.wind_direction == null) { s.textContent = "No data"; a.style.opacity = ".3"; f.textContent = ""; }
    else { s.textContent = (+env.wind_speed).toFixed(1) + " m/s"; a.style.opacity = "1"; a.style.transform = `rotate(${(+env.wind_direction + 180) % 360}deg)`; f.textContent = `from ${env.wind_direction}°`; }
  }

  // polling + clock
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const stopEnv = api.poll(api.getEnvironment, 10000, onEnv, () => {});
  timers.push(setInterval(() => loadCommsHistory(selId), 3000));  // refresh selected vehicle's comms log
  timers.push(setInterval(() => loadAuthority(selId), 2000));  // refresh selected vehicle's control authority
  timers.push(setInterval(() => loadCommands(selId), 3000));  // refresh selected vehicle's command lifecycle
  timers.push(setInterval(tickPxmAge, 1000));  // keep the "last fetch … ago" line live
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() {
    stopFleet(); stopEnv(); clearInterval(clockId); timers.forEach(clearInterval);
    authCtl.dispose();
    clearMissionOverlay();
    if (map) { map.remove(); map = null; }
  };
}
