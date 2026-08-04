// Plan.js — dedicated first-class survey-planning page. Construct a side-scan-sonar
// lawnmower survey (boundary, shoreline clearance, no-go zones, lane spacing, angle,
// one/dual pass, home, transit), generate a segmented route from the operator backend
// (planning.py, ported from Scout), validate it, and upload it through the EXISTING,
// read-back-verified MISSION_UPLOAD command path — never a second mission framework.
//
// LAYOUT: the app's 4-column has-dock grid maps onto the recommended layout —
//   rail | LEFT tools panel (.dock) | CENTRE Leaflet map (.map-wrap) | RIGHT params/
//   validation/summary (.inspector) — with a bottom action bar overlaid on the map. The map
//   is the dominant element and the page stays usable on a laptop screen.
//
// STATE: the small page-specific state machine lives in lib/planning.js (pure, unit-tested);
// this page holds only the Leaflet/DOM plumbing and the in-progress drawing buffer. Every
// geometry/param edit goes through the lib's immutable helpers, so "route is outdated after
// an input change" is derived, never tracked by hand.
//
// COORDINATE CONVENTION: the model stores GeoJSON [lng, lat]; Leaflet wants [lat, lng]. The
// two are bridged ONLY by toLL/fromLL here, so the [lng,lat] contract the backend and
// mission-contract share is never violated by a silent axis swap.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { commState, noTelem } from "../lib/ui.js";
import * as P from "../lib/planning.js";
import * as FP from "../lib/fleet-plan.js";
import { missionUploadStage, UPLOAD_STAGES } from "../lib/mission-upload.js";
import { hasPendingOfType } from "../lib/command.js";
import { uploadEligibility, UPLOAD_LEVEL } from "../lib/upload-policy.js";
import { getSelectedVehicleId, setSelectedVehicleId } from "../lib/selection.js";
import { attachMapLayout } from "../lib/map-layout.js";
import { pickInitialView, getSavedViewport, setSavedViewport, isValidLatLng, isNullIsland,
         TOFTASJON, DEFAULT_ZOOM, VIEW_RANK } from "../lib/map-view.js";

const infoIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v0.01M11 12h1v4h1"/></svg>';

// Distinct style per ORDERED segment kind — one hue family per phase so the route reads as a
// sequence, never one ambiguous orange for approach + transition + return (the old defect):
//   orange  = approach into the survey      green  = primary coverage
//   grey    = primary→secondary transition   purple = secondary coverage
//   amber   = return out of the survey
const SEG_STYLE = {
  start_connector:        { color: "#F2A93B", weight: 2.2, opacity: 0.85, dashArray: "2 6" },
  approach:               { color: "#F2A93B", weight: 2.6, opacity: 0.95, dashArray: "9 6" },
  survey_entry_connector: { color: "#F2A93B", weight: 2.2, opacity: 0.95, dashArray: "2 5" },
  primary:                { color: "#3ECF8E", weight: 3,   opacity: 0.95 },
  pass_transition:        { color: "#C7D2DE", weight: 2,   opacity: 0.8, dashArray: "4 5" },
  secondary:              { color: "#A78BFA", weight: 3,   opacity: 0.95 },
  return_connector:       { color: "#F5C542", weight: 2.4, opacity: 0.9, dashArray: "7 6" },
  return_approach:        { color: "#F5C542", weight: 2.6, opacity: 0.95, dashArray: "9 6" },
  final_home_connector:   { color: "#F5C542", weight: 2.2, opacity: 0.9, dashArray: "2 6" },
};
// Segment kinds that carry a direction the operator must read (approach/return order).
const ARROW_KINDS = new Set(["approach", "return_approach", "start_connector",
                             "survey_entry_connector", "return_connector", "final_home_connector"]);
const BOUNDARY_STYLE = { color: "#4C8DFF", weight: 2.2, opacity: 0.95, fill: false, pane: "pl-boundary" };
const NAVIGABLE_STYLE = { color: "#3ECF8E", weight: 1, opacity: 0.5, dashArray: "4 5", fill: true, fillColor: "#3ECF8E", fillOpacity: 0.05, pane: "pl-navigable" };
// No-go zones keep an UNMISTAKABLE red fill+outline and live in their own pane ABOVE the
// navigable fill, so route generation (which draws the translucent green navigable area) can
// never grey them out — before or after generation. Selection thickens the outline but never
// changes the red semantics.
const NOGO_STYLE = { color: "#E5484D", weight: 1.8, opacity: 0.95, fill: true, fillColor: "#E5484D", fillOpacity: 0.22, pane: "pl-nogo" };
const NOGO_SEL_STYLE = { ...NOGO_STYLE, weight: 2.6, dashArray: "5 4" };

const WORKFLOW = [
  ["vehicle", "Vehicle"], ["area", "Survey Area"], ["restrictions", "Restrictions"],
  ["pattern", "Survey Pattern"], ["preview", "Route Preview"], ["validate", "Validate"],
  ["finish", "Finish & Upload"],
];

export function Plan(root) {
  const L = window.L;
  let fleet = [];
  let model = P.emptyModel();
  // Adopt the shared cross-page selection so a vehicle picked on the Map page is already
  // selected here (and the initial view can centre on it). Reuses the one selection model
  // rather than a Plan-private one.
  const sharedSel = getSelectedVehicleId();
  if (sharedSel != null) model = { ...model, vehicleId: sharedSel };
  let mode = null;            // active drawing mode: null|'boundary'|'nogo'|'home'|'approach'|'return'|'fhome'
  // Planning mode (task Step 1). 'single' keeps the existing single-vehicle workflow verbatim;
  // 'fleet' exposes the multi-vehicle workflow layered on the SAME shared geometry (model.boundary,
  // model.noGoZones, model.params). Fleet-specific state lives in fleetModel (lib/fleet-plan.js).
  let planMode = "single";
  let fleetModel = FP.emptyFleet();
  let fleetHomeTarget = null;  // vehicle id whose planning home the next map click sets
  let isolateVehicle = null;   // fleet map: show only this vehicle's routes, or null for all
  let busyFleet = false;       // fleet generate/validate in flight
  let fleetError = null;
  let draftRing = [];         // in-progress polygon vertices ([lng,lat]) while drawing
  let selected = null;        // { type:'boundary'|'nogo'|'approach'|'return'|'home', id?/index? }
  const history = [];         // undo stack of prior models (drawing/edit actions)
  let drafts = [];            // saved-draft summaries
  let busyGen = false, busyVal = false;
  let genError = null;        // last generation error message
  let cmds = [];              // command queue for the selected vehicle (upload lifecycle)
  let authority = null;       // control authority for the selected vehicle
  let capabilities = null;    // backend command capabilities

  const toLL = (pt) => [pt[1], pt[0]];
  const fromLL = (ll) => [ll.lng, ll.lat];

  root.className = "app has-dock plan";
  root.innerHTML =
    Ribbon({ missionLabel: "Survey planning" }) +
    NavRail("plan") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Plan</span><span class="lbl plan-state-chip" id="plan-state">—</span></div>
       <div class="plan-tools" id="plan-tools"></div>
       <div class="dock-foot">${infoIcon}<span>Planning is operator-owned. The vehicle receives a finalized, validated mission through the existing verified upload path — nothing here starts a mission.</span></div>
     </div>
     <div class="map-wrap">
       <div class="map-stage" id="plan-map-stage">
       <div id="plan-map"></div>
       <div class="ov plan-banner" id="plan-banner"></div>
       <div class="ov legend plan-legend" id="plan-legend">
         <div class="legend-h"><span class="lbl">Legend</span></div>
         <div class="legend-body">
           <div class="li"><span class="pl-sw boundary"></span>Survey boundary</div>
           <div class="li"><span class="pl-sw navigable"></span>Navigable (shoreline-offset)</div>
           <div class="li"><span class="pl-sw nogo"></span>No-go zone</div>
           <div class="li"><span class="pl-sw approach"></span>Approach (A1→) &amp; entry</div>
           <div class="li"><span class="pl-sw primary"></span>Primary coverage</div>
           <div class="li"><span class="pl-sw transition"></span>Pass transition</div>
           <div class="li"><span class="pl-sw secondary"></span>Secondary coverage</div>
           <div class="li"><span class="pl-sw return"></span>Return (→R1) &amp; home connector</div>
           <div class="li"><span class="pl-sw home"></span>Planning home</div>
         </div>
       </div>
       <div class="ov toast" id="plan-toast"></div>
       <div class="ov plan-viewctl" id="plan-viewctl">
         <button id="pl-center-usv" title="Centre the map on the selected USV's position">Center on USV</button>
         <button id="pl-center-op" title="Centre the map on your device location (asks permission)">Center on me</button>
       </div>
       </div>
       <div class="plan-actionbar" id="plan-actions"></div>
     </div>
     <aside class="inspector plan-inspector" id="plan-inspector"></aside>`;

  // ---- Leaflet ----
  // Dynamic initial view (task 2). Render IMMEDIATELY at the best synchronous source (a saved
  // Plan viewport, else the Toftasjön fallback); fresh USV positions and browser geolocation
  // arrive asynchronously and upgrade the view via recenterIfBetter() — without ever blocking
  // first paint. `viewRank` is the priority of the source currently centred on (lower =
  // stronger); a new source only recentres if it is STRICTLY stronger AND the operator has
  // not manually panned/zoomed. All coordinates stay WGS84 — this only moves the camera.
  let viewRank = Infinity;
  let userInteracted = false;   // operator panned/zoomed → stop automatic recentering
  let programmaticMove = false; // guard so our own setView is not mistaken for interaction
  let geo = null;               // resolved browser geolocation { lat, lng }, once granted
  let geoRequested = false;

  const init0 = pickInitialView({ saved: getSavedViewport(), fallback: TOFTASJON });
  viewRank = init0.rank;
  // Zoom moves TOP-RIGHT, beneath the Center-on view controls (theme.css offsets it by
  // the measured --map-tr-h). It used to sit top-left directly on top of the plan status
  // banner, which is where the drawing instructions and every generation/validation error
  // are printed — the two controls fought for the same 30x38 px on every viewport.
  // trackResize:false — lib/map-layout.js is the SINGLE owner of resize for every map in
  // the station. Leaflet's built-in window listener also calls invalidateSize, but with
  // debounceMoveend, which parks a 200 ms timer that fires `moveend` on a map the router
  // may already have removed (crash: "_leaflet_pos of undefined" in the moveend handler
  // below). One owner, no dangling timer.
  const map = L.map("plan-map", { zoomControl: false, attributionControl: true, trackResize: false })
    .setView(init0.center, init0.zoom);
  L.control.zoom({ position: "topright" }).addTo(map);
  L.control.scale({ position: "bottomright", imperial: false }).addTo(map);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20, attribution: "© OpenStreetMap" }).addTo(map);
  // Shared resize + corner-measurement contract (lib/map-layout.js). The banner is a
  // top-left overlay, so the legend's max-height is derived from it and the two can never
  // collide however long a validation message grows.
  const detachMapLayout = attachMapLayout(map, document.getElementById("plan-map-stage"), {
    topLeft: [document.getElementById("plan-banner")],
    topRight: [document.getElementById("plan-viewctl")],
  });

  // Interaction tracking + viewport persistence. The initial setView above ran BEFORE these
  // handlers were attached, so it does not count as interaction or persist a fallback view.
  map.on("movestart", () => { if (!programmaticMove) userInteracted = true; });
  map.on("moveend", () => {
    const wasProgrammatic = programmaticMove;
    programmaticMove = false;
    // _mapPane is gone once the map has been removed; a late event must not persist a
    // viewport (or throw) on a torn-down map.
    if (!wasProgrammatic && map._mapPane) { const c = map.getCenter(); setSavedViewport([c.lat, c.lng], map.getZoom()); }
  });

  // Explicit stacking panes (task PART 3). The order guarantees the no-go RED fill/outline
  // always sits ABOVE the translucent navigable fill — so generating the route can never
  // grey a no-go zone out — while route lines and markers sit above the zones.
  const PANES = [["pl-navigable", 410], ["pl-boundary", 420], ["pl-nogo", 430],
                 ["pl-route", 450], ["pl-markers", 460], ["pl-handles", 470]];
  PANES.forEach(([name, z]) => { map.createPane(name); map.getPane(name).style.zIndex = String(z); });

  map.on("click", onMapClick);
  map.on("dblclick", onMapDblClick);
  map.on("contextmenu", onMapRightClick);

  // Layer groups, rebuilt on every render — simple and flicker-free at this scale. Each
  // layer's SHAPES carry the matching `pane` so the z-order above is what actually renders.
  const layers = {
    boundary: L.layerGroup().addTo(map),
    navigable: L.layerGroup().addTo(map),
    nogo: L.layerGroup().addTo(map),
    route: L.layerGroup().addTo(map),
    markers: L.layerGroup().addTo(map),
    draft: L.layerGroup().addTo(map),
    handles: L.layerGroup().addTo(map),
  };

  // ---- model plumbing ----
  function apply(newModel, pushHistory = true) {
    if (pushHistory) history.push(model);
    model = newModel;
    renderAll();
  }
  function renderAll() { drawGeometry(); renderTools(); renderInspector(); renderActions(); renderBanner(); }

  // ═══════════════ DRAWING INTERACTION ═══════════════
  function setMode(m) {
    mode = mode === m ? null : m;
    draftRing = [];
    // Double-click zoom must be off while drawing so a finishing double-click doesn't zoom.
    if (mode) map.doubleClickZoom.disable(); else map.doubleClickZoom.enable();
    renderAll();
  }
  function onMapClick(e) {
    const pt = fromLL(e.latlng);
    if (mode === "boundary" || mode === "nogo") { draftRing.push(pt); drawDraft(); renderTools(); }
    else if (mode === "home") { apply(P.setHome(model, pt)); mode = null; map.doubleClickZoom.enable(); }
    else if (mode === "fhome" && fleetHomeTarget != null) { fleetModel = FP.setVehicleHome(fleetModel, fleetHomeTarget, pt); mode = null; fleetHomeTarget = null; map.doubleClickZoom.enable(); renderAll(); }
    else if (mode === "approach") { apply(P.setApproach(model, [...model.approach, pt])); }
    else if (mode === "return") { apply(P.setReturns(model, [...model.returns, pt])); }
  }
  function onMapDblClick(e) { if (mode === "boundary" || mode === "nogo") finishShape(); }
  function onMapRightClick(e) {
    if ((mode === "boundary" || mode === "nogo") && draftRing.length) { draftRing.pop(); drawDraft(); renderTools(); }
  }
  function finishShape() {
    if (!P.ringIsValid(draftRing)) { showToast("A polygon needs at least 3 points.", "warn"); return; }
    if (mode === "boundary") apply(P.setBoundary(model, draftRing));
    else if (mode === "nogo") apply(P.addNoGoZone(model, draftRing));
    draftRing = []; mode = null; map.doubleClickZoom.enable();
  }
  function cancelDraw() { draftRing = []; mode = null; map.doubleClickZoom.enable(); renderAll(); }

  function undo() {
    if (draftRing.length) { draftRing.pop(); drawDraft(); renderTools(); return; }
    if (!history.length) return;
    model = history.pop(); renderAll();
  }
  function deleteSelected() {
    if (!selected) return;
    if (selected.type === "boundary") apply(P.setBoundary(model, null));
    else if (selected.type === "nogo") apply(P.removeNoGoZone(model, selected.id));
    else if (selected.type === "home") apply(P.setHome(model, null));
    else if (selected.type === "approach") apply(P.setApproach(model, model.approach.filter((_, i) => i !== selected.index)));
    else if (selected.type === "return") apply(P.setReturns(model, model.returns.filter((_, i) => i !== selected.index)));
    selected = null; renderAll();
  }
  async function clearAll() {
    if (P.hasUnsavedWork(model) && !window.confirm(
      "Clear the entire plan?\n\nThis removes the boundary, no-go zones, home, approach and " +
      "return waypoints, the generated route and validation. It does NOT touch the mission " +
      "stored on any vehicle and issues no command. This cannot be undone.")) return;
    history.length = 0; selected = null; draftRing = []; mode = null; fleetHomeTarget = null;
    const keepVehicle = model.vehicleId;
    model = P.clearModel(); model.vehicleId = keepVehicle;
    if (planMode === "fleet") { fleetModel = FP.emptyFleet(); isolateVehicle = null; fleetError = null; }
    map.doubleClickZoom.enable();
    renderAll();
  }

  // ═══════════════ MAP RENDERING ═══════════════
  function drawDraft() {
    layers.draft.clearLayers();
    if (!draftRing.length) return;
    const lls = draftRing.map(toLL);
    if (lls.length > 1) L.polyline(lls, { color: "#4C8DFF", weight: 2, dashArray: "5 5", opacity: 0.9 }).addTo(layers.draft);
    lls.forEach((ll, i) => L.circleMarker(ll, { radius: 4, color: "#4C8DFF", fillColor: "#4C8DFF", fillOpacity: 1 })
      .bindTooltip(String(i + 1), { permanent: false }).addTo(layers.draft));
  }

  function vertexHandle(ring, idx, onMove) {
    // A draggable vertex marker for polygon editing. Excludes the closing duplicate point.
    // A divIcon marker (not circleMarker, which isn't natively draggable) carries the handle.
    const mk = L.marker(toLL(ring[idx]), { draggable: true, pane: "pl-handles", icon: L.divIcon({ className: "", html: '<div class="plan-vhandle"></div>', iconSize: [12, 12], iconAnchor: [6, 6] }) });
    mk.on("drag", (e) => onMove(idx, fromLL(e.latlng), false));
    mk.on("dragend", (e) => onMove(idx, fromLL(e.latlng), true));
    return mk;
  }

  // Directional arrows along a leg the operator must read in order (screen-space angle,
  // latitude-corrected so it stays right without a zoom listener). Decorative, non-interactive.
  function drawArrows(coords, color) {
    for (let i = 0; i < coords.length - 1; i++) {
      const a = toLL(coords[i]), b = toLL(coords[i + 1]);
      const latR = (a[0] * Math.PI) / 180;
      const ang = (Math.atan2(-(b[0] - a[0]), (b[1] - a[1]) * Math.cos(latR)) * 180) / Math.PI;
      const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
      L.marker(mid, { interactive: false, pane: "pl-route", icon: L.divIcon({ className: "", html: `<div class="plan-arrow" style="color:${color};transform:rotate(${ang}deg)">➤</div>`, iconSize: [14, 14], iconAnchor: [7, 7] }) }).addTo(layers.route);
    }
  }

  // Render an ordered operator waypoint list (approach A1.., or return R1..): a dashed
  // directional line through the points + numbered, selectable, draggable markers. Distinct
  // labels (A vs R) so the two sets are never indistinguishable numbered dots.
  function drawWaypointList(list, type, prefix, color) {
    if (!list || !list.length) return;
    const setter = type === "approach" ? P.setApproach : P.setReturns;
    if (list.length > 1) {
      L.polyline(list.map(toLL), { color, weight: 2, opacity: 0.75, dashArray: "8 6", pane: "pl-route" }).addTo(layers.markers);
      drawArrows(list, color);
    }
    list.forEach((pt, i) => {
      const sel = selected && selected.type === type && selected.index === i;
      const mk = L.marker(toLL(pt), { draggable: true, pane: "pl-markers", icon: L.divIcon({ className: "", html: `<div class="plan-wp ${type}${sel ? " sel" : ""}">${prefix}${i + 1}</div>`, iconSize: [22, 18], iconAnchor: [11, 9] }) });
      mk.on("click", (e) => { L.DomEvent.stop(e); selected = { type, index: i }; renderAll(); });
      mk.on("dragend", (e) => { const pts = list.map((p) => [p[0], p[1]]); pts[i] = fromLL(e.latlng); apply(setter(model, pts)); });
      mk.addTo(layers.markers);
    });
  }

  function drawGeometry() {
    Object.values(layers).forEach((lg) => { if (lg !== layers.draft) lg.clearLayers(); });

    // Boundary (+ editable vertex handles when it's not being redrawn).
    if (model.boundary && model.boundary.length >= 4) {
      const ring = model.boundary;
      const poly = L.polygon(ring.slice(0, -1).map(toLL), { ...BOUNDARY_STYLE });
      poly.on("click", (e) => { L.DomEvent.stop(e); selected = { type: "boundary" }; renderAll(); });
      poly.addTo(layers.boundary);
      if (mode !== "boundary") {
        for (let i = 0; i < ring.length - 1; i++) {
          layers.handles.addLayer(vertexHandle(ring, i, (idx, pt, commit) => moveBoundaryVertex(idx, pt, commit)));
        }
      }
    }
    // Navigable (shoreline-offset) area — only from a generated route (never faked). In fleet
    // mode it comes from the fleet plan's shared geometry.
    const nav = planMode === "fleet"
      ? (fleetModel.generated && fleetModel.generated.shared_geometry && fleetModel.generated.shared_geometry.navigable_boundary)
      : (model.generated && model.generated.navigable_boundary);
    if (Array.isArray(nav)) nav.forEach((r) => L.polygon(r.map(toLL), { ...NAVIGABLE_STYLE }).addTo(layers.navigable));

    // No-go zones.
    model.noGoZones.forEach((z) => {
      const isSel = selected && selected.type === "nogo" && selected.id === z.id;
      const poly = L.polygon(P.closeRing(z.ring).slice(0, -1).map(toLL), isSel ? { ...NOGO_SEL_STYLE } : { ...NOGO_STYLE });
      poly.on("click", (e) => { L.DomEvent.stop(e); selected = { type: "nogo", id: z.id }; renderAll(); });
      poly.bindTooltip(z.id, { sticky: true });
      poly.addTo(layers.nogo);
      if (isSel) {
        const cr = P.closeRing(z.ring);
        for (let i = 0; i < cr.length - 1; i++) {
          layers.handles.addLayer(vertexHandle(cr, i, (idx, pt, commit) => moveZoneVertex(z.id, idx, pt, commit)));
        }
      }
    });

    if (planMode === "fleet") { drawFleetGeometry(); drawDraft(); return; }

    // Generated route segments (distinct styles per kind) + directional arrows on the
    // approach/return/connector legs + generated waypoint dots.
    if (model.generated && Array.isArray(model.generated.segments)) {
      model.generated.segments.forEach((s) => {
        const style = SEG_STYLE[s.kind];
        if (style && s.coordinates.length > 1) {
          L.polyline(s.coordinates.map(toLL), { ...style, pane: "pl-route" }).addTo(layers.route);
          if (ARROW_KINDS.has(s.kind)) drawArrows(s.coordinates, style.color);
        }
      });
      (model.generated.route_waypoints || []).forEach((w, i) => {
        L.circleMarker([w.latitude, w.longitude], { radius: 2.6, color: "#0C141C", weight: 0.6, fillColor: "#DCE3EC", fillOpacity: 0.9, pane: "pl-markers" })
          .bindTooltip(`WP ${i + 1}`, { sticky: true }).addTo(layers.markers);
      });
    }

    // Approach waypoints A1, A2, … (ordered route INTO the survey) + their directional line
    // even before a route is generated, so the click order is always visually obvious.
    drawWaypointList(model.approach, "approach", "A", "#F2A93B");
    // Return waypoints R1, R2, … (ordered route OUT toward home).
    drawWaypointList(model.returns, "return", "R", "#F5C542");

    // Planning home marker (distinct from vehicle Home / RTL point — this is planning only).
    if (model.home) {
      const sel = selected && selected.type === "home";
      const mk = L.marker(toLL(model.home), { draggable: true, icon: L.divIcon({ className: "", html: `<div class="plan-home${sel ? " sel" : ""}">⌂</div>`, iconSize: [24, 24], iconAnchor: [12, 20] }) });
      mk.on("click", (e) => { L.DomEvent.stop(e); selected = { type: "home" }; renderAll(); });
      mk.on("dragend", (e) => apply(P.setHome(model, fromLL(e.latlng))));
      mk.addTo(layers.markers);
    }
    drawDraft();
  }

  function moveBoundaryVertex(idx, pt, commit) {
    const ring = model.boundary.slice(0, -1);
    ring[idx] = pt;
    if (commit) apply(P.setBoundary(model, ring));
    else { model = { ...model, boundary: P.closeRing(ring), validation: null }; layers.boundary.clearLayers();
           L.polygon(ring.map(toLL), { ...BOUNDARY_STYLE }).addTo(layers.boundary); }
  }
  function moveZoneVertex(id, idx, pt, commit) {
    const z = model.noGoZones.find((x) => x.id === id); if (!z) return;
    const ring = P.closeRing(z.ring).slice(0, -1);
    ring[idx] = pt;
    if (commit) apply({ ...model, noGoZones: model.noGoZones.map((x) => x.id === id ? { ...x, ring: P.closeRing(ring) } : x), validation: null });
  }

  // ═══════════════ LEFT PANEL: tools + workflow ═══════════════
  // Planning-mode selector (task Step 1) — rendered at the top of the dock in BOTH modes.
  function modeToggleHtml() {
    return `<div class="plan-sec"><span class="lbl">Planning mode</span></div>
      <div class="plan-modeseg" role="radiogroup" aria-label="Planning mode">
        <button class="plan-modebtn ${planMode === "single" ? "on" : ""}" id="pm-single" role="radio" aria-checked="${planMode === "single"}">Single Vehicle</button>
        <button class="plan-modebtn ${planMode === "fleet" ? "on" : ""}" id="pm-fleet" role="radio" aria-checked="${planMode === "fleet"}">Fleet Mission</button>
      </div>`;
  }
  function wireModeToggle() {
    bind("pm-single", () => switchMode("single"));
    bind("pm-fleet", () => switchMode("fleet"));
  }
  // Switching modes preserves the shared survey geometry (boundary, no-go zones, params) but does
  // NOT carry incompatible per-vehicle state across. It resets only the generated allocation/route,
  // never the drawn polygon or zones.
  function switchMode(m) {
    if (m === planMode) return;
    mode = null; fleetHomeTarget = null; map.doubleClickZoom.enable();
    planMode = m;
    renderAll(); fitToPlan();
  }

  function renderTools() {
    const st = planMode === "fleet" ? FP.deriveFleetStatus(fleetModel) : P.planState(model);
    document.getElementById("plan-state").textContent = st;
    document.getElementById("plan-state").className = "lbl plan-state-chip " + String(st).toLowerCase();
    if (planMode === "fleet") { renderFleetTools(); return; }

    const steps = WORKFLOW.map(([k, label]) => {
      const done = stepDone(k);
      return `<div class="plan-step ${done ? "done" : ""}"><span class="pl-dot">${done ? "✓" : ""}</span>${label}</div>`;
    }).join("");

    const vehOpts = fleet.map((v) => `<option value="${v.id}" ${v.id === model.vehicleId ? "selected" : ""}>${v.name || "USV-" + v.id} · ${commState(v)}</option>`).join("");
    const drawing = (mode === "boundary" || mode === "nogo");
    const zoneList = model.noGoZones.map((z) => `<div class="plan-item ${selected && selected.type === "nogo" && selected.id === z.id ? "sel" : ""}" data-zone="${z.id}"><span>${z.id}</span><button class="plan-x" data-rmzone="${z.id}" title="Remove zone">✕</button></div>`).join("") || `<div class="plan-empty">No no-go zones</div>`;
    const wpRow = (kind, i, n, prefix) => `<div class="plan-item ${selected && selected.type === kind && selected.index === i ? "sel" : ""}" data-wp="${kind}:${i}"><span>${prefix}${i + 1}</span><span class="plan-item-btns"><button data-wpup="${kind}:${i}" title="Move up" ${i === 0 ? "disabled" : ""}>▲</button><button data-wpdn="${kind}:${i}" title="Move down" ${i === n - 1 ? "disabled" : ""}>▼</button><button class="plan-x" data-wprm="${kind}:${i}" title="Remove">✕</button></span></div>`;
    const approachList = model.approach.map((_, i) => wpRow("approach", i, model.approach.length, "A")).join("") || `<div class="plan-empty">No approach waypoints</div>`;
    const returnList = model.returns.map((_, i) => wpRow("return", i, model.returns.length, "R")).join("") || `<div class="plan-empty">No return waypoints</div>`;
    const startOpts = P.ROUTE_START_MODES.map((m) => `<option value="${m}" ${model.routeStartMode === m ? "selected" : ""}>${P.ROUTE_START_LABEL[m]}</option>`).join("");

    document.getElementById("plan-tools").innerHTML = `
      ${modeToggleHtml()}
      <div class="plan-sec"><span class="lbl">Workflow</span></div>
      <div class="plan-steps">${steps}</div>

      <div class="plan-sec"><span class="lbl">1 · Vehicle</span></div>
      <div class="plan-field"><select id="plan-veh"><option value="">Select a vehicle…</option>${vehOpts}</select></div>

      <div class="plan-sec"><span class="lbl">2 · Survey area</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool ${mode === "boundary" ? "on" : ""}" id="pl-draw-boundary">${model.boundary ? "Redraw boundary" : "Draw boundary"}</button>
        <button class="plan-tool" id="pl-del-boundary" ${model.boundary ? "" : "disabled"}>Delete boundary</button>
      </div>
      ${drawing && mode === "boundary" ? `<div class="plan-draw-hint">Click to add vertices (${draftRing.length}). Double-click or Finish to close. Right-click removes the last point.<div class="plan-btnrow"><button id="pl-finish" ${P.ringIsValid(draftRing) ? "" : "disabled"}>Finish</button><button id="pl-cancel">Cancel</button></div></div>` : ""}

      <div class="plan-sec"><span class="lbl">3 · Restrictions</span></div>
      <div class="plan-btnrow"><button class="plan-tool ${mode === "nogo" ? "on" : ""}" id="pl-draw-nogo" ${P.canAddZone(model) ? "" : "disabled"} title="${P.canAddZone(model) ? "Draw a no-go zone" : "Draw the survey boundary first"}">Add no-go zone</button></div>
      ${drawing && mode === "nogo" ? `<div class="plan-draw-hint">Click to add vertices (${draftRing.length}). Double-click or Finish to close.<div class="plan-btnrow"><button id="pl-finish" ${P.ringIsValid(draftRing) ? "" : "disabled"}>Finish</button><button id="pl-cancel">Cancel</button></div></div>` : ""}
      <div class="plan-list">${zoneList}</div>

      <div class="plan-sec"><span class="lbl">Planning home</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool ${mode === "home" ? "on" : ""}" id="pl-home">${model.home ? "Move home" : "Set home"}</button>
      </div>
      <div class="plan-note plan-note-sm">${infoIcon}<span>Planning Home is route-planning geometry only. It does <b>not</b> change the Pixhawk HOME_POSITION or the RTL home.</span></div>
      <div class="plan-field"><label class="plan-fl" for="pl-start">Start route from</label><select id="pl-start" ${model.home ? "" : "disabled title='Set a planning home to start there'"}>${startOpts}</select></div>

      <div class="plan-sec"><span class="lbl">Approach waypoints</span></div>
      <div class="plan-help">Approach waypoints define the operator-approved route into the survey area before coverage begins. They are visited in numbered order (A1 → A2 → survey entry).</div>
      <div class="plan-btnrow">
        <button class="plan-tool ${mode === "approach" ? "on" : ""}" id="pl-approach">Add approach WP</button>
        <button class="plan-tool" id="pl-approach-clear" ${model.approach.length ? "" : "disabled"}>Clear</button>
      </div>
      <div class="plan-list">${approachList}</div>

      <div class="plan-sec"><span class="lbl">Return waypoints</span></div>
      <div class="plan-help">Return waypoints define the route out of the survey back toward planning home (last coverage point → R1 → R2 → home). Optional.</div>
      <div class="plan-btnrow">
        <button class="plan-tool ${mode === "return" ? "on" : ""}" id="pl-return">Add return WP</button>
        <button class="plan-tool" id="pl-return-rev" ${model.approach.length ? "" : "disabled"} title="Copy the approach waypoints in reverse into the return list (stays editable)">Use reversed approach</button>
        <button class="plan-tool" id="pl-return-clear" ${model.returns.length ? "" : "disabled"}>Clear</button>
      </div>
      <div class="plan-list">${returnList}</div>

      <div class="plan-sec"><span class="lbl">Edit</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool" id="pl-undo" ${history.length || draftRing.length ? "" : "disabled"}>Undo</button>
        <button class="plan-tool" id="pl-delsel" ${selected ? "" : "disabled"}>Delete selected</button>
        <button class="plan-tool danger" id="pl-clear">Clear all</button>
      </div>`;

    // wire
    wireModeToggle();
    const veh = document.getElementById("plan-veh");
    if (veh) veh.onchange = () => {
      const id = veh.value ? +veh.value : null;
      model = { ...model, vehicleId: id };
      setSelectedVehicleId(id);          // keep the shared cross-page selection in sync
      selectVehicleSideEffects(id);
      userInteracted = false;            // choosing a vehicle re-engages follow…
      centerOnSelected({ explicit: false }); // …and centres on it when it has a valid position
      renderAll();
    };
    bind("pl-draw-boundary", () => setMode("boundary"));
    bind("pl-del-boundary", () => apply(P.setBoundary(model, null)));
    bind("pl-draw-nogo", () => { if (P.canAddZone(model)) setMode("nogo"); });
    bind("pl-home", () => setMode("home"));
    bind("pl-approach", () => setMode("approach"));
    bind("pl-return", () => setMode("return"));
    bind("pl-approach-clear", () => { apply(P.setApproach(model, [])); if (selected && selected.type === "approach") selected = null; renderAll(); });
    bind("pl-return-clear", () => { apply(P.setReturns(model, [])); if (selected && selected.type === "return") selected = null; renderAll(); });
    bind("pl-return-rev", () => { apply(P.reversedApproach(model)); showToast("Return list set to the reversed approach — still editable.", "ok"); });
    bind("pl-finish", finishShape);
    bind("pl-cancel", cancelDraw);
    bind("pl-undo", undo);
    bind("pl-delsel", deleteSelected);
    bind("pl-clear", clearAll);
    const startSel = document.getElementById("pl-start");
    if (startSel) startSel.onchange = () => apply(P.setRouteStart(model, startSel.value), false);
    document.querySelectorAll("[data-rmzone]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); apply(P.removeNoGoZone(model, b.dataset.rmzone)); if (selected && selected.id === b.dataset.rmzone) selected = null; renderAll(); });
    document.querySelectorAll("[data-zone]").forEach((el) => el.onclick = () => { selected = { type: "nogo", id: el.dataset.zone }; renderAll(); });
    document.querySelectorAll("[data-wp]").forEach((el) => el.onclick = () => { const [k, i] = el.dataset.wp.split(":"); selected = { type: k, index: +i }; renderAll(); });
    document.querySelectorAll("[data-wprm]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); const [k, i] = b.dataset.wprm.split(":"); removeWp(k, +i); });
    document.querySelectorAll("[data-wpup]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); const [k, i] = b.dataset.wpup.split(":"); reorderWp(k, +i, -1); });
    document.querySelectorAll("[data-wpdn]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); const [k, i] = b.dataset.wpdn.split(":"); reorderWp(k, +i, +1); });
  }
  function bind(id, fn) { const e = document.getElementById(id); if (e) e.onclick = fn; }
  function wpListOf(kind) { return kind === "approach" ? model.approach : model.returns; }
  function setWpList(kind, pts) { return kind === "approach" ? P.setApproach(model, pts) : P.setReturns(model, pts); }
  function removeWp(kind, i) { apply(setWpList(kind, wpListOf(kind).filter((_, j) => j !== i))); selected = null; renderAll(); }
  function reorderWp(kind, i, dir) {
    const t = [...wpListOf(kind)]; const j = i + dir;
    if (j < 0 || j >= t.length) return;
    [t[i], t[j]] = [t[j], t[i]];
    apply(setWpList(kind, t));
  }
  function stepDone(k) {
    switch (k) {
      case "vehicle": return model.vehicleId != null;
      case "area": return P.hasBoundary(model);
      case "restrictions": return P.hasBoundary(model);   // optional step; satisfied once area exists
      case "pattern": return P.canGenerate(model);
      case "preview": return P.hasRoute(model) && !P.isOutdated(model);
      case "validate": return !!(model.validation && model.validation.ok);
      case "finish": return model.upload.phase === "uploaded";
      default: return false;
    }
  }

  // ═══════════════ FLEET MISSION: dock tools ═══════════════
  // Shared survey geometry (boundary + no-go zones) reuses the SAME drawing tools and model as
  // single mode; only vehicle selection, per-vehicle homes/speed/colour and fleet settings are
  // fleet-specific. The count is DERIVED from the selection — never a numeric input.
  function reportedHome(v) {
    const h = v && v.home;
    if (h && h.latitude != null && h.longitude != null) return [h.longitude, h.latitude];
    if (h && h.lat != null && h.lng != null) return [h.lng, h.lat];
    return null;
  }
  function fleetVehName(id) {
    const v = fleet.find((x) => String(x.id) === String(id));
    return v ? (v.name || "USV-" + id) : "USV-" + id;
  }
  function renderFleetTools() {
    const rows = fleet.map((v) => {
      const id = String(v.id);
      const sel = FP.isSelected(fleetModel, id);
      const cs = commState(v);
      const pos = isValidLatLng(v.lat, v.lng) && !isNullIsland(v.lat, v.lng) ? "position ✓" : "no position";
      return `<label class="fleet-pick ${sel ? "on" : ""}"><input type="checkbox" data-fveh="${id}" ${sel ? "checked" : ""}/>
        <span class="fp-name">${esc(v.name || "USV-" + id)}</span>
        <span class="fp-meta">${esc(id)} · <span class="cs-${cs}">${cs}</span> · ${pos}</span></label>`;
    }).join("") || `<div class="plan-empty">No vehicles in the registry yet.</div>`;

    const cards = fleetModel.selectedVehicleIds.map((id) => {
      const c = FP.vehicleConfig(fleetModel, id) || {};
      const v = fleet.find((x) => String(x.id) === String(id));
      const rep = v ? reportedHome(v) : null;
      const homeTxt = c.home ? `${c.home[1].toFixed(5)}, ${c.home[0].toFixed(5)} <span class="fp-hsrc">(${c.homeSource === "operator" ? "operator" : "reported"})</span>` : `<span class="plan-empty">not set</span>`;
      const setting = mode === "fhome" && String(fleetHomeTarget) === String(id);
      return `<div class="fleet-card" style="border-left:3px solid ${c.colour}">
        <div class="fc-h"><span class="fc-sw" style="background:${c.colour}"></span><b>${esc(fleetVehName(id))}</b><span class="fc-id">${esc(id)}</span></div>
        <div class="fc-row"><span>Home</span><span>${homeTxt}</span></div>
        <div class="plan-btnrow">
          <button class="plan-tool ${setting ? "on" : ""}" data-fhset="${id}">${c.home ? "Move home" : "Set home on map"}</button>
          ${rep && c.homeSource !== "operator" ? `<button class="plan-tool" data-fhrep="${id}">Use reported</button>` : ""}
        </div>
        <div class="fc-row"><label for="fs-${id}">Survey speed</label><span class="plan-inp"><input id="fs-${id}" data-fspeed="${id}" type="number" step="any" value="${c.survey_speed_mps}"/> <span class="u">m/s</span></span></div>
      </div>`;
    }).join("");

    const balOpts = FP.BALANCE_METRICS.map((b) => `<option value="${b}" ${fleetModel.balanceMetric === b ? "selected" : ""}>${b === "estimated_duration" ? "Estimated duration" : "Route distance"}</option>`).join("");
    const drawing = (mode === "boundary" || mode === "nogo");
    const zoneList = model.noGoZones.map((z) => `<div class="plan-item ${selected && selected.type === "nogo" && selected.id === z.id ? "sel" : ""}" data-zone="${z.id}"><span>${z.id}</span><button class="plan-x" data-rmzone="${z.id}" title="Remove zone">✕</button></div>`).join("") || `<div class="plan-empty">No no-go zones</div>`;

    document.getElementById("plan-tools").innerHTML = `
      ${modeToggleHtml()}
      <div class="plan-sec"><span class="lbl">Fleet vehicles (${FP.selectedCount(fleetModel)} selected)</span></div>
      <div class="plan-help">Select two or more vehicles. A disconnected vehicle can still be planned; upload is gated on availability.</div>
      <div class="fleet-picklist">${rows}</div>

      <div class="plan-sec"><span class="lbl">Per-vehicle configuration</span></div>
      ${cards || `<div class="plan-empty">Select vehicles to configure their home &amp; speed.</div>`}

      <div class="plan-sec"><span class="lbl">Shared survey area</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool ${mode === "boundary" ? "on" : ""}" id="pl-draw-boundary">${model.boundary ? "Redraw boundary" : "Draw boundary"}</button>
        <button class="plan-tool" id="pl-del-boundary" ${model.boundary ? "" : "disabled"}>Delete</button>
      </div>
      ${drawing && mode === "boundary" ? `<div class="plan-draw-hint">Click to add vertices (${draftRing.length}). Double-click or Finish to close.<div class="plan-btnrow"><button id="pl-finish" ${P.ringIsValid(draftRing) ? "" : "disabled"}>Finish</button><button id="pl-cancel">Cancel</button></div></div>` : ""}
      <div class="plan-btnrow"><button class="plan-tool ${mode === "nogo" ? "on" : ""}" id="pl-draw-nogo" ${P.canAddZone(model) ? "" : "disabled"}>Add no-go zone</button></div>
      ${drawing && mode === "nogo" ? `<div class="plan-draw-hint">Click to add vertices (${draftRing.length}). Double-click or Finish to close.<div class="plan-btnrow"><button id="pl-finish" ${P.ringIsValid(draftRing) ? "" : "disabled"}>Finish</button><button id="pl-cancel">Cancel</button></div></div>` : ""}
      <div class="plan-list">${zoneList}</div>

      <div class="plan-sec"><span class="lbl">Fleet settings</span></div>
      <div class="plan-prow"><label for="fleet-sep">Min. route separation <span class="plan-hint" title="Planning/warning threshold for the minimum planned distance between vehicle routes. NOT a runtime collision guarantee.">?</span></label><span class="plan-inp"><input id="fleet-sep" type="number" step="any" value="${fleetModel.minimumFleetSeparationM}"/> <span class="u">m</span></span></div>
      <div class="plan-prow"><label for="fleet-bal">Balance by</label><span class="plan-inp"><select id="fleet-bal">${balOpts}</select></span></div>

      <div class="plan-sec"><span class="lbl">Edit</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool" id="pl-undo" ${history.length || draftRing.length ? "" : "disabled"}>Undo</button>
        <button class="plan-tool danger" id="pl-clear">Clear all</button>
      </div>`;

    // wire
    wireModeToggle();
    document.querySelectorAll("[data-fveh]").forEach((cb) => cb.onchange = () => {
      const id = cb.dataset.fveh;
      const v = fleet.find((x) => String(x.id) === id);
      fleetModel = FP.toggleVehicle(fleetModel, id, { home: v ? reportedHome(v) : null });
      renderAll();
    });
    document.querySelectorAll("[data-fhset]").forEach((b) => b.onclick = () => {
      fleetHomeTarget = b.dataset.fhset; setMode("fhome");
    });
    document.querySelectorAll("[data-fhrep]").forEach((b) => b.onclick = () => {
      const id = b.dataset.fhrep; const v = fleet.find((x) => String(x.id) === id);
      fleetModel = FP.setVehicleHome(fleetModel, id, reportedHome(v)); renderAll();
    });
    document.querySelectorAll("[data-fspeed]").forEach((inp) => inp.onchange = () => {
      const v = inp.value === "" ? null : Number(inp.value);
      fleetModel = FP.setVehicleSpeed(fleetModel, inp.dataset.fspeed, v); renderAll();
    });
    bind("pl-draw-boundary", () => setMode("boundary"));
    bind("pl-del-boundary", () => apply(P.setBoundary(model, null)));
    bind("pl-draw-nogo", () => { if (P.canAddZone(model)) setMode("nogo"); });
    bind("pl-finish", finishShape);
    bind("pl-cancel", cancelDraw);
    bind("pl-undo", undo);
    bind("pl-clear", clearAll);
    const sep = document.getElementById("fleet-sep");
    if (sep) sep.onchange = () => { fleetModel = FP.setSeparation(fleetModel, Number(sep.value)); renderAll(); };
    const bal = document.getElementById("fleet-bal");
    if (bal) bal.onchange = () => { fleetModel = FP.setBalanceMetric(fleetModel, bal.value); renderAll(); };
    document.querySelectorAll("[data-rmzone]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); apply(P.removeNoGoZone(model, b.dataset.rmzone)); renderAll(); });
    document.querySelectorAll("[data-zone]").forEach((el) => el.onclick = () => { selected = { type: "nogo", id: el.dataset.zone }; renderAll(); });
  }

  // ═══════════════ RIGHT PANEL: params, validation, summary ═══════════════
  function renderInspector() {
    if (planMode === "fleet") { renderFleetInspector(); return; }
    const p = model.params, m = model.generated && model.generated.metrics;
    const inp = document.getElementById("plan-inspector");
    const fld = (label, id, val, unit, hint) => `<div class="plan-prow"><label for="${id}">${label}${hint ? ` <span class="plan-hint" title="${hint}">?</span>` : ""}</label><span class="plan-inp"><input id="${id}" type="number" value="${val == null ? "" : val}" step="any"/> <span class="u">${unit}</span></span></div>`;

    inp.innerHTML = `
      <div class="plan-card">
        <div class="msect"><span class="lbl">Planning parameters</span></div>
        <div class="plan-params">
          ${fld("Shoreline clearance", "pp-clear", p.shoreline_clearance_m, "m", "Distance inward from the operator-drawn survey boundary — keeps the route off the shore.")}
          ${fld("Lane spacing", "pp-space", p.lane_spacing_m, "m", "Distance between parallel side-scan survey lines — choose from the sonar swath width and desired overlap/quality. This is NOT waypoint spacing.")}
          ${fld("Survey angle", "pp-angle", p.primary_angle_deg, "°", "Orientation of the survey lines, 0–359°.")}
          ${fld("Survey speed", "pp-speed", p.survey_speed_mps, "m/s", "Used ONLY to estimate mission duration — it is not uploaded.")}
          <div class="plan-prow"><label for="pp-dual">Dual pass</label><span class="plan-inp"><input id="pp-dual" type="checkbox" ${p.dual_pass ? "checked" : ""}/> <span class="u">perpendicular 2nd pass</span></span></div>
          ${p.dual_pass ? fld("Secondary angle", "pp-angle2", P.effectiveSecondaryAngle(p), "°", "Defaults to primary + 90°.") : ""}
        </div>
        <div class="plan-note">${infoIcon}<span><b>Lane spacing</b> is the gap between parallel survey lines (sonar coverage). <b>Waypoints</b> are generated at turns and required route points — they are not the survey parameter.</span></div>
      </div>

      <div class="plan-card">
        <div class="msect"><span class="lbl">Validation</span></div>
        ${renderValidation()}
      </div>

      <div class="plan-card">
        <div class="msect"><span class="lbl">Route summary</span></div>
        ${renderSummary(m)}
      </div>`;

    // wire param inputs (change marks route outdated via lib immutability)
    wireNum("pp-clear", "shoreline_clearance_m");
    wireNum("pp-space", "lane_spacing_m");
    wireNum("pp-angle", "primary_angle_deg");
    wireNum("pp-speed", "survey_speed_mps");
    wireNum("pp-angle2", "secondary_angle_deg");
    const dual = document.getElementById("pp-dual");
    if (dual) dual.onchange = () => apply(P.setParam(model, "dual_pass", dual.checked), false);
  }
  function wireNum(id, key) {
    const e = document.getElementById(id);
    if (!e) return;
    e.onchange = () => {
      const v = e.value === "" ? null : Number(e.value);
      apply(P.setParam(model, key, Number.isFinite(v) ? v : null), false);
    };
  }
  function renderValidation() {
    const v = model.validation;
    if (!P.hasRoute(model)) return `<div class="plan-empty">Generate a route to validate it.</div>`;
    if (P.isOutdated(model)) return `<div class="plan-vbad">The route is outdated — regenerate before validating.</div>`;
    if (!v) return `<div class="plan-empty">Not validated yet — press Validate.</div>`;
    const errs = (v.errors || []).map((e) => `<li class="bad">${esc(e)}</li>`).join("");
    const warns = (v.warnings || []).map((w) => `<li class="warn">${esc(w)}</li>`).join("");
    const checks = Object.entries(v.checks || {}).map(([k, val]) => `<div class="plan-check ${val === true ? "ok" : val === false ? "bad" : "dim"}"><span>${k}</span><span>${val === true ? "✓" : val === false ? "✕" : "—"}</span></div>`).join("");
    return `<div class="plan-vhead ${v.ok ? "ok" : "bad"}">${v.ok ? "VALID — ready to upload" : "INVALID — resolve the errors below"}</div>
      ${errs || warns ? `<ul class="plan-vlist">${errs}${warns}</ul>` : ""}
      <div class="plan-checks">${checks}</div>`;
  }
  function renderSummary(m) {
    if (!m) return `<div class="plan-empty">No route generated yet.</div>`;
    const rows = [
      ["Selected vehicle", vehName(model.vehicleId)],
      ["Boundary area", fmtArea(m.boundary_area_m2)],
      ["Navigable area", fmtArea(m.navigable_area_m2)],
      ["Shoreline clearance", `${m.shoreline_clearance_m} m`],
      ["Lane spacing", `${m.lane_spacing_m} m`],
      ["No-go zones", String(m.no_go_zone_count)],
      ["Pass mode", m.dual_pass ? "Dual pass" : "One pass"],
      ["Primary angle", `${m.primary_angle_deg}°`],
      ["Secondary angle", m.dual_pass ? `${m.secondary_angle_deg}°` : "—"],
      ["Route start", P.ROUTE_START_LABEL[m.route_start_mode] || "—"],
      ["Approach / return WPs", `${m.approach_waypoint_count ?? 0} / ${m.return_waypoint_count ?? 0}`],
      ["Total route length", fmtLen(m.total_length_m)],
      ["Coverage length", fmtLen(m.coverage_length_m)],
      ["Approach / return length", fmtLen(m.transit_length_m)],
      ["Waypoints", `${m.waypoint_count}${model.generated.max_route_waypoints ? ` / ${model.generated.max_route_waypoints}` : ""}`],
      // Compact route-cleanup diagnostic only (PART 10): raw → final and how many redundant
      // points the safety-checked simplification removed. Detailed per-fragment metrics stay
      // in the mission package/tests, not the operator UI.
      ...(m.route_quality && m.route_quality.removed_waypoint_count > 0
        ? [["Waypoints reduced", `${m.route_quality.raw_waypoint_count} → ${m.route_quality.final_waypoint_count} (−${m.route_quality.removed_waypoint_count} redundant)`]]
        : []),
      ["Estimated duration", m.estimated_duration_s != null ? `${fmtDur(m.estimated_duration_s)} (est.${m.survey_speed_is_default ? ", default speed" : ""})` : noTelem("no speed")],
      ["Generated", model.generated.generated_at ? new Date(model.generated.generated_at).toLocaleTimeString([], { hour12: false }) : "—"],
    ];
    const warns = (model.generated.warnings || []).map((w) => `<li class="warn">${esc(w)}</li>`).join("");
    return `<div class="plan-sumgrid">${rows.map(([k, val]) => `<div class="plan-srow"><span class="k">${k}</span><span class="v">${val}</span></div>`).join("")}</div>
      ${warns ? `<ul class="plan-vlist">${warns}</ul>` : ""}
      <div class="plan-note">${infoIcon}<span>Duration is an estimate only. Distances are geodesic over the generated route.</span></div>`;
  }

  // ═══════════════ FLEET MISSION: map, inspector, actions, upload ═══════════════
  // The shared geometry model is `model` (boundary/no-go/params); fleetModel holds fleet state.
  function fleetColour(id) { const c = FP.vehicleConfig(fleetModel, id); return (c && c.colour) || "#4C8DFF"; }
  // Route-kind dash: survey solid, approach dashed, return dotted — each in the vehicle's colour.
  function fleetSegStyle(kind, colour) {
    if (kind === "primary" || kind === "secondary") return { color: colour, weight: 3, opacity: 0.95 };
    if (kind === "pass_transition") return { color: colour, weight: 2, opacity: 0.7, dashArray: "4 5" };
    if (kind === "final_home_connector" || kind === "return_connector" || kind === "return_approach")
      return { color: colour, weight: 2.4, opacity: 0.9, dashArray: "2 5" };   // return: dotted
    return { color: colour, weight: 2.6, opacity: 0.9, dashArray: "9 6" };      // approach: dashed
  }
  function drawFleetGeometry() {
    // Per-vehicle planning-home markers (always shown so homes can be set before generation).
    fleetModel.selectedVehicleIds.forEach((id) => {
      const c = FP.vehicleConfig(fleetModel, id); if (!c || !c.home) return;
      const mk = L.marker(toLL(c.home), { draggable: true, pane: "pl-markers", icon: L.divIcon({ className: "", html: `<div class="plan-home" style="color:${c.colour}">⌂</div>`, iconSize: [24, 24], iconAnchor: [12, 20] }) });
      mk.bindTooltip(`${fleetVehName(id)} home`, { sticky: true });
      mk.on("dragend", (e) => { fleetModel = FP.setVehicleHome(fleetModel, id, fromLL(e.latlng)); renderAll(); });
      mk.addTo(layers.markers);
    });
    const fp = fleetModel.generated;
    if (!fp || !Array.isArray(fp.vehicles)) return;
    fp.vehicles.forEach((vp) => {
      if (isolateVehicle && String(isolateVehicle) !== String(vp.vehicle_id)) return;
      const colour = vp.colour || fleetColour(vp.vehicle_id);
      (vp.mission_package.segments || []).forEach((s) => {
        if (s.coordinates.length > 1) {
          L.polyline(s.coordinates.map(toLL), { ...fleetSegStyle(s.kind, colour), pane: "pl-route" }).addTo(layers.route);
          if (s.kind !== "primary" && s.kind !== "secondary") drawArrows(s.coordinates, colour);
        }
      });
    });
  }

  function renderFleetInspector() {
    const inp = document.getElementById("plan-inspector");
    const p = model.params;
    const fld = (label, id, val, unit, hint) => `<div class="plan-prow"><label for="${id}">${label}${hint ? ` <span class="plan-hint" title="${hint}">?</span>` : ""}</label><span class="plan-inp"><input id="${id}" type="number" value="${val == null ? "" : val}" step="any"/> <span class="u">${unit}</span></span></div>`;
    inp.innerHTML = `
      <div class="plan-card">
        <div class="msect"><span class="lbl">Shared survey pattern</span></div>
        <div class="plan-params">
          ${fld("Shoreline clearance", "pp-clear", p.shoreline_clearance_m, "m")}
          ${fld("Lane spacing", "pp-space", p.lane_spacing_m, "m", "Distance between parallel survey lines. Distinct from the fleet route separation.")}
          ${fld("Survey angle", "pp-angle", p.primary_angle_deg, "°")}
          <div class="plan-prow"><label for="pp-dual">Dual pass</label><span class="plan-inp"><input id="pp-dual" type="checkbox" ${p.dual_pass ? "checked" : ""}/> <span class="u">2nd pass, clipped per vehicle</span></span></div>
          ${p.dual_pass ? fld("Secondary angle", "pp-angle2", P.effectiveSecondaryAngle(p), "°") : ""}
        </div>
      </div>
      <div class="plan-card">
        <div class="msect"><span class="lbl">Fleet validation</span></div>
        ${renderFleetValidation()}
      </div>
      <div class="plan-card">
        <div class="msect"><span class="lbl">Fleet summary</span></div>
        ${renderFleetSummary()}
        <div class="plan-note">${infoIcon}<span>Fleet planning performs static partitioning and pre-deployment route-conflict validation. It reduces planned route overlap but does <b>not</b> replace runtime vehicle-to-vehicle collision detection or avoidance.</span></div>
      </div>`;
    wireNum("pp-clear", "shoreline_clearance_m");
    wireNum("pp-space", "lane_spacing_m");
    wireNum("pp-angle", "primary_angle_deg");
    wireNum("pp-angle2", "secondary_angle_deg");
    const dual = document.getElementById("pp-dual");
    if (dual) dual.onchange = () => apply(P.setParam(model, "dual_pass", dual.checked), false);
  }
  function renderFleetValidation() {
    if (!FP.hasFleetPlan(fleetModel)) return `<div class="plan-empty">Generate a fleet plan to validate it.</div>`;
    if (FP.isFleetOutdated(fleetModel, model)) return `<div class="plan-vbad">Fleet allocation is out of date — an input changed. Regenerate and validate before upload.</div>`;
    const v = fleetModel.validation;
    if (!v) return `<div class="plan-empty">Not validated yet — press Validate fleet.</div>`;
    const errs = (v.errors || []).map((e) => `<li class="bad">${esc(e)}</li>`).join("");
    const warns = (v.warnings || []).map((w) => `<li class="warn">${esc(w)}</li>`).join("");
    const checks = Object.entries(v.checks || {}).map(([k, val]) => `<div class="plan-check ${val === true ? "ok" : val === false ? "bad" : "dim"}"><span>${k}</span><span>${val === true ? "✓" : val === false ? "✕" : "—"}</span></div>`).join("");
    const sep = v.metrics && v.metrics.minimum_cross_route_separation_m;
    return `<div class="plan-vhead ${v.ok ? "ok" : "bad"}">${v.ok ? "VALID — ready to upload" : "INVALID — resolve the errors below"}</div>
      ${sep != null ? `<div class="plan-srow"><span class="k">Min. planned route separation</span><span class="v">${sep} m</span></div>` : ""}
      ${errs || warns ? `<ul class="plan-vlist">${errs}${warns}</ul>` : ""}
      <div class="plan-checks">${checks}</div>`;
  }
  function renderFleetSummary() {
    const fp = fleetModel.generated;
    if (!fp) return `<div class="plan-empty">No fleet plan generated yet.</div>`;
    const s = fp.allocation_summary || {};
    const vrows = (fp.vehicles || []).map((vp) => {
      const u = fleetModel.upload.vehicles[String(vp.vehicle_id)] || {};
      const m = vp.metrics; const up = u.status || "—";
      const iso = isolateVehicle && String(isolateVehicle) === String(vp.vehicle_id);
      // Each vehicle owns its own result AND its own failure reason. One vehicle's long
      // backend error is confined to that vehicle's card (it wraps, and the list scrolls),
      // so it can never overflow the panel or hide the other vehicles' statuses.
      const err = u.error ? `<div class="fp-err" title="${esc(String(u.error))}">${esc(String(u.error))}</div>` : "";
      return `<div class="fleet-sumcard ${iso ? "iso" : ""}" data-iso="${vp.vehicle_id}" style="border-left:3px solid ${vp.colour}">
        <div class="fc-h"><span class="fc-sw" style="background:${vp.colour}"></span><b title="${esc(vp.vehicle_name)}">${esc(vp.vehicle_name)}</b><span class="fc-id">${esc(String(vp.vehicle_id))}</span><span class="fp-up ${up.toLowerCase()}">${up}</span></div>
        ${err}
        <div class="fleet-metgrid">
          <span>Lines</span><span>${m.assigned_survey_line_count}</span>
          <span>Waypoints</span><span>${m.waypoint_count}</span>
          <span>Survey</span><span>${fmtLen(m.survey_distance_m)}</span>
          <span>Approach/return</span><span>${fmtLen(m.approach_distance_m)} / ${fmtLen(m.return_distance_m)}</span>
          <span>Total</span><span>${fmtLen(m.total_distance_m)}</span>
          <span>Speed</span><span>${m.survey_speed_mps} m/s${m.survey_speed_is_default ? " (def)" : ""}</span>
          <span>Est. duration</span><span>${fmtDur(m.estimated_duration_s)}</span>
        </div></div>`;
    }).join("");
    const frows = [
      ["Vehicles", String(s.vehicle_count)],
      ["Survey lines", String(s.survey_line_count)],
      ["Total survey distance", fmtLen(s.total_survey_distance_m)],
      ["Max / min est. duration", `${fmtDur(s.max_estimated_duration_s)} / ${fmtDur(s.min_estimated_duration_s)}`],
      ["Imbalance", `${s.imbalance_percent}%`],
      ["Unassigned lines", String((s.unassigned_survey_line_ids || []).length)],
      ["Duplicated lines", String((s.duplicate_survey_line_ids || []).length)],
      ["Fleet plan", esc(fp.fleet_plan_id || "—") + " · v" + (fp.fleet_plan_version || 1)],
    ];
    const iso = `<div class="plan-btnrow"><button class="plan-tool ${isolateVehicle ? "" : "on"}" id="fleet-showall">Show all</button></div>`;
    const html = `<div class="fleet-legend">${(fp.vehicles || []).map((vp) => `<span class="fleet-leg" data-iso="${vp.vehicle_id}"><span class="fc-sw" style="background:${vp.colour}"></span>${esc(vp.vehicle_name)}</span>`).join("")}</div>
      ${iso}
      <div class="fleet-sumlist">${vrows}</div>
      <div class="plan-sumgrid">${frows.map(([k, val]) => `<div class="plan-srow"><span class="k">${k}</span><span class="v">${val}</span></div>`).join("")}</div>`;
    setTimeout(() => {
      document.querySelectorAll("[data-iso]").forEach((el) => el.onclick = () => { isolateVehicle = el.dataset.iso; renderAll(); });
      bind("fleet-showall", () => { isolateVehicle = null; renderAll(); });
    }, 0);
    return html;
  }

  function renderFleetActions() {
    const canGen = FP.canGenerateFleet(fleetModel, model) && !busyFleet;
    const canVal = FP.hasFleetPlan(fleetModel) && !FP.isFleetOutdated(fleetModel, model) && !busyFleet;
    const canUp = FP.canUploadFleet(fleetModel, model);
    const anyFailed = Object.values(fleetModel.upload.vehicles || {}).some((v) => v.status === "FAILED" || v.status === "STALE");
    const genLabel = FP.hasFleetPlan(fleetModel) ? "Regenerate fleet" : "Generate fleet";
    document.getElementById("plan-actions").innerHTML = `
      <button class="pl-act" id="act-clear">Clear</button>
      <div class="pl-act-grow"></div>
      <button class="pl-act primary" id="act-fgen" ${canGen ? "" : "disabled"} title="${esc(fleetGenTitle())}">${busyFleet ? "Working…" : genLabel}</button>
      <button class="pl-act" id="act-fval" ${canVal ? "" : "disabled"}>Validate fleet</button>
      ${anyFailed ? `<button class="pl-act" id="act-fretry">Retry failed</button>` : ""}
      <button class="pl-act success" id="act-fupload" ${canUp ? "" : "disabled"} title="${esc(canUp ? "Upload each child mission to its vehicle" : "Generate and validate a fleet plan first")}">Upload fleet</button>`;
    bind("act-clear", clearAll);
    bind("act-fgen", doGenerateFleet);
    bind("act-fval", doValidateFleet);
    bind("act-fretry", doRetryFailed);
    bind("act-fupload", doFleetUpload);
  }
  function fleetGenTitle() {
    if (FP.selectedCount(fleetModel) < FP.MIN_FLEET_VEHICLES) return "Select at least two vehicles";
    if (!FP.everyHomeSet(fleetModel)) return "Set a planning home for every selected vehicle";
    if (!P.hasBoundary(model)) return "Draw the shared survey boundary";
    if (!(model.params.lane_spacing_m > 0)) return "Set the lane spacing";
    return "Generate the fleet allocation and child missions";
  }

  async function doGenerateFleet() {
    if (!FP.canGenerateFleet(fleetModel, model) || busyFleet) return;
    busyFleet = true; fleetError = null; renderActions(); renderBanner();
    try {
      const res = await api.generateFleet(FP.fleetPlanningBody(fleetModel, model));
      if (res && res.ok) { fleetModel = FP.applyFleetGenerated(fleetModel, model, res); isolateVehicle = null; fitToPlan(); }
      else { fleetError = (res && (res.message || (res.errors && res.errors.join(" ")))) || "Fleet generation failed."; }
    } catch (e) { fleetError = "Fleet generation request failed."; }
    finally { busyFleet = false; renderAll(); }
  }
  async function doValidateFleet() {
    if (!FP.hasFleetPlan(fleetModel) || busyFleet) return;
    busyFleet = true; renderActions();
    try {
      const res = await api.validateFleet(fleetModel.generated);
      fleetModel = FP.applyFleetValidation(fleetModel, res);
    } catch (e) { fleetModel = FP.applyFleetValidation(fleetModel, { ok: false, errors: ["Fleet validation request failed."], warnings: [], checks: {} }); }
    finally { busyFleet = false; renderAll(); }
  }
  function doRetryFailed() { fleetModel = FP.retryFailed(fleetModel); renderAll(); uploadNextPending(); }
  async function doFleetUpload() {
    if (!FP.canUploadFleet(fleetModel, model)) return;
    const n = fleetModel.selectedVehicleIds.length;
    if (!window.confirm(
      `Upload ${n} child missions — one to each selected vehicle?\n\n` +
      `Each mission is finalized and uploaded through the verified read-back path and stored as ` +
      `an immutable original mission record. This OVERWRITES each vehicle's flight-controller ` +
      `mission and is confirmed only by read-back. It does NOT start any mission.`)) return;
    fleetModel = FP.beginUpload(fleetModel); renderAll();
    await uploadNextPending();
  }
  async function uploadNextPending() {
    const id = FP.nextPendingVehicle(fleetModel);
    if (!id) { renderAll(); return; }
    const vp = FP.vehiclePlan(fleetModel, id);
    fleetModel = FP.markVehicle(fleetModel, id, "UPLOADING"); renderAll();
    const v = fleet.find((x) => String(x.id) === String(id));
    // Availability guard — a selected vehicle is never silently omitted; an unavailable one is
    // marked FAILED with its reason, and the others still proceed.
    if (!v) {
      fleetModel = FP.markVehicle(fleetModel, id, "FAILED", { error: "Vehicle not in the registry." });
      renderAll(); return uploadNextPending();
    }
    try {
      const res = await api.finalizeMission(FP.finalizePayloadForVehicle(fleetModel, vp));
      if (res.ok && res.data && res.data.command) {
        const rec = res.data.mission || {};
        fleetModel = FP.markVehicle(fleetModel, id, "UPLOADING", { cmdId: res.data.command.id, missionId: rec.mission_id, hash: vp.route_hash });
      } else {
        const d = res.data || {};
        const err = Array.isArray(d.errors) && d.errors.length ? d.errors.join(" ") : (d.message || d.error || "Upload was not accepted.");
        fleetModel = FP.markVehicle(fleetModel, id, "FAILED", { error: err });
      }
    } catch (e) {
      fleetModel = FP.markVehicle(fleetModel, id, "FAILED", { error: "Upload request failed." });
    }
    renderAll();
    await uploadNextPending();   // sequential: next selected vehicle
  }
  // Poll the per-vehicle upload command lifecycle → VERIFIED / FAILED (read-back verification is
  // the ONLY verified; a delivered file is never success on its own). Reuses missionUploadStage.
  function syncFleetUpload() {
    const uploading = fleetModel.selectedVehicleIds.filter((id) => {
      const u = fleetModel.upload.vehicles[id]; return u && u.status === "UPLOADING" && u.cmdId;
    });
    uploading.forEach((id) => {
      api.getCommands(id).then((d) => {
        const cmd = (d && d.commands || []).find((c) => c.id === fleetModel.upload.vehicles[id].cmdId);
        if (!cmd) return;
        const stg = missionUploadStage(cmd, null);
        if (stg.state === "done") { fleetModel = FP.markVehicle(fleetModel, id, "VERIFIED"); renderAll(); }
        else if (stg.state === "failed") { fleetModel = FP.markVehicle(fleetModel, id, "FAILED", { error: stg.reason }); renderAll(); }
      }).catch(() => {});
    });
  }

  // ═══════════════ ACTION BAR + BANNER ═══════════════
  function renderActions() {
    if (planMode === "fleet") { renderFleetActions(); return; }
    const st = P.planState(model);
    const genLabel = P.hasRoute(model) ? (P.isOutdated(model) ? "Regenerate" : "Regenerate") : "Generate route";
    const gate = uploadGate();
    // Show the upload eligibility hint once a route exists for the selected vehicle (block or
    // armed-warn), so the AUTO → LOITER → Upload workflow is guided. Text-carried, not colour-only.
    const showHint = model.vehicleId != null && P.hasRoute(model) && !P.isOutdated(model)
      && (gate.level === UPLOAD_LEVEL.BLOCK || gate.level === UPLOAD_LEVEL.WARN);
    const hint = showHint ? `<span class="pl-upload-hint ${gate.level}" title="${esc(gate.message)}">${esc(gate.message)}</span>` : "";
    document.getElementById("plan-actions").innerHTML = `
      <button class="pl-act" id="act-clear">Clear</button>
      <button class="pl-act" id="act-savedraft">Save draft</button>
      <button class="pl-act" id="act-loaddraft">Load draft${drafts.length ? ` (${drafts.length})` : ""}</button>
      <div class="pl-act-grow"></div>
      ${hint}
      <button class="pl-act primary" id="act-generate" ${P.canGenerate(model) && !busyGen ? "" : "disabled"}>${busyGen ? "Generating…" : genLabel}</button>
      <button class="pl-act" id="act-validate" ${P.hasRoute(model) && !P.isOutdated(model) && !busyVal ? "" : "disabled"}>${busyVal ? "Validating…" : "Validate"}</button>
      <button class="pl-act success" id="act-upload" ${P.canUpload(model) && gate.allowed ? "" : "disabled"} title="${esc(uploadTitle(gate))}">Finish &amp; Upload</button>`;
    bind("act-clear", clearAll);
    bind("act-savedraft", saveDraft);
    bind("act-loaddraft", openLoadDraft);
    bind("act-generate", doGenerate);
    bind("act-validate", doValidate);
    bind("act-upload", doUpload);
  }
  function uploadTitle(gate) {
    if (model.vehicleId == null) return "Select a vehicle first";
    if (!P.hasRoute(model)) return "Generate a route first";
    if (P.isOutdated(model)) return "Route is outdated — regenerate first";
    if (!(model.validation && model.validation.ok)) return "Validate the route first";
    if (gate && !gate.allowed) return gate.reason;
    return gate ? gate.message : "Finalize and upload the mission through the verified path";
  }
  function renderBanner() {
    const b = document.getElementById("plan-banner");
    if (planMode === "fleet") { renderFleetBanner(b); return; }
    const st = P.planState(model);
    let cls = "info", extra = "";
    if (st === "ROUTE_OUTDATED" || st === "ERROR") cls = "warn";
    else if (st === "VALID" || st === "UPLOADED") cls = "ok";
    if (genError) { cls = "warn"; extra = ` — ${esc(genError)}`; }
    const uploadInfo = renderUploadStatus();
    b.className = "ov plan-banner " + cls;
    b.innerHTML = `<span class="pl-state">${st}</span><span class="pl-msg">${P.PLAN_STATE_LABEL[st] || ""}${extra}</span>${uploadInfo}`;
  }
  const FLEET_STATE_LABEL = {
    NOT_STARTED: "Select vehicles, draw the shared area, then generate the fleet plan.",
    READY: "Fleet plan valid — ready to upload.",
    UPLOADING: "Uploading child missions to each vehicle…",
    PARTIALLY_UPLOADED: "Some vehicles verified, some failed — retry the failed ones.",
    VERIFYING: "Verifying uploaded missions…",
    VERIFIED: "All child missions uploaded and verified — fleet ready for operator launch.",
    FAILED: "Fleet upload failed — the plan is preserved; review and retry.",
    STALE: "Uploaded missions belong to an older plan — regenerate and re-upload.",
  };
  function renderFleetBanner(b) {
    let st = FP.deriveFleetStatus(fleetModel);
    let cls = "info", extra = "", label = FLEET_STATE_LABEL[st] || "";
    // Before any upload begins, speak the PLAN phase (generated / valid / outdated) instead of a
    // misleading upload status.
    if (st === "NOT_STARTED" && FP.hasFleetPlan(fleetModel)) {
      if (FP.isFleetOutdated(fleetModel, model)) { st = "OUT OF DATE"; cls = "warn"; label = "Fleet allocation is out of date — regenerate and validate before upload."; }
      else if (fleetModel.validation && fleetModel.validation.ok) { st = "READY"; cls = "ok"; label = "Fleet plan valid — ready to upload."; }
      else if (fleetModel.validation) { st = "INVALID"; cls = "warn"; label = "Fleet validation found blocking errors — resolve them before upload."; }
      else { st = "GENERATED"; label = "Fleet plan generated — validate it before upload."; }
    } else {
      if (st === "VERIFIED") cls = "ok";
      else if (st === "FAILED" || st === "PARTIALLY_UPLOADED" || st === "STALE") cls = "warn";
    }
    if (fleetError) { cls = "warn"; extra = ` — ${esc(fleetError)}`; }
    const verified = fleetModel.selectedVehicleIds.filter((id) => (fleetModel.upload.vehicles[id] || {}).status === "VERIFIED").length;
    const total = fleetModel.selectedVehicleIds.length;
    const uploading = Object.keys(fleetModel.upload.vehicles || {}).length > 0;
    const progress = (uploading && total) ? ` · ${verified}/${total} verified` : "";
    b.className = "ov plan-banner " + cls;
    b.innerHTML = `<span class="pl-state">${st}${progress}</span><span class="pl-msg">${label}${extra}</span>`;
  }

  // ═══════════════ GENERATE / VALIDATE ═══════════════
  async function doGenerate() {
    if (!P.canGenerate(model) || busyGen) return;
    busyGen = true; genError = null; renderActions(); renderBanner();
    try {
      const res = await api.generatePlan(P.planningInputs(model));
      if (res && res.ok) { apply(P.applyGenerated(model, res), false); fitToPlan(); }
      else { genError = (res && (res.message || (res.errors && res.errors.join(" ")))) || "Route generation failed."; }
    } catch (e) { genError = "Route generation request failed."; }
    finally { busyGen = false; renderAll(); }
  }
  async function doValidate() {
    if (!P.hasRoute(model) || P.isOutdated(model) || busyVal) return;
    busyVal = true; renderActions();
    try {
      const body = { ...P.planningInputs(model), route_waypoints: model.generated.route_waypoints, segments: model.generated.segments };
      const res = await api.validatePlan(body);
      apply(P.applyValidation(model, res), false);
    } catch (e) { apply(P.applyValidation(model, { ok: false, errors: ["Validation request failed."], warnings: [], checks: {} }), false); }
    finally { busyVal = false; renderAll(); }
  }

  // ═══════════════ UPLOAD (existing verified MISSION_UPLOAD path) ═══════════════
  function hasControl() { return !!(authority && authority.authority === "OPERATOR"); }
  function selectedVehicle() { return model.vehicleId != null ? fleet.find((x) => x.id === model.vehicleId) : null; }

  // Operator-side upload eligibility (armed + confirmed-LOITER policy). Early feedback only —
  // Scout remains the safety authority and performs the final stationary/groundspeed check.
  function uploadGate() {
    const v = selectedVehicle();
    const t = (v && v.telemetry) || {};
    const connected = v ? commState(v) === "connected" : false;
    const groundspeed = v && v.speed != null ? v.speed : (t.groundspeed != null ? t.groundspeed : null);
    const missionPending = model.upload.phase === "uploading"
      || hasPendingOfType(cmds, "MISSION_UPLOAD") || hasPendingOfType(cmds, "MISSION_CLEAR");
    return uploadEligibility({
      connected,
      armed: t.armed == null ? null : t.armed,
      mode: t.mode == null ? null : t.mode,
      modeFresh: connected,
      groundspeed,
      hasAuthority: hasControl(),
      authorityRequired: true,
      missionPending,
    });
  }
  function trackedUpload() { return model.upload.cmdId ? (cmds.find((c) => c.id === model.upload.cmdId) || null) : null; }
  async function doUpload() {
    if (!P.canUpload(model)) return;
    // Operator-side gate (armed + confirmed-LOITER policy). Scout still performs the final
    // authoritative safety check; this only avoids an obviously-doomed submit and guides the
    // AUTO → LOITER → Upload workflow. Never auto-commands LOITER.
    const gate = uploadGate();
    if (!gate.allowed) { showToast(gate.reason, "warn"); return; }
    const payload = P.finalizePayload(model);
    if (!payload) { showToast("The generated route could not be prepared for upload.", "warn"); return; }
    const v = fleet.find((x) => x.id === model.vehicleId);
    const vname = v ? (v.name || "USV-" + v.id) : "the vehicle";
    const n = model.generated.metrics.waypoint_count;
    if (!window.confirm(
      `Finish & upload this survey to ${vname}?\n\n` +
      `${n} route waypoints (Scout adds Home at seq 0 → ${n + 1} Pixhawk items).\n\n` +
      `An immutable original mission record (revision 0) is stored, then the mission is ` +
      `uploaded through the verified path. This OVERWRITES the mission on the flight ` +
      `controller and is confirmed only by read-back verification. It does NOT start the mission.`)) return;
    model = { ...model, upload: { phase: "uploading", cmdId: null, missionId: null, revision: 0, error: null, at: Date.now(), result: null } };
    renderAll();
    // Finalize: store the immutable original mission record (revision 0) AND create the
    // unchanged, read-back-verified MISSION_UPLOAD command in one call.
    const res = await api.finalizeMission(payload);
    if (res.ok && res.data && res.data.command) {
      const rec = res.data.mission || {};
      model = { ...model, upload: { ...model.upload, cmdId: res.data.command.id, missionId: rec.mission_id, revision: rec.mission_revision != null ? rec.mission_revision : 0 } };
    } else {
      const d = res.data || {};
      const err = Array.isArray(d.errors) && d.errors.length ? d.errors.join(" ") : (d.message || d.error || "Upload was not accepted by the operator backend.");
      model = { ...model, upload: { phase: "error", cmdId: null, error: err, at: Date.now(), result: null } };
    }
    loadCommands(model.vehicleId); renderAll();
  }
  function syncUploadFromCommands() {
    if (model.upload.phase !== "uploading" || !model.upload.cmdId) return;
    const cmd = trackedUpload();
    if (!cmd) return;
    const v = fleet.find((x) => x.id === model.vehicleId);
    const stg = missionUploadStage(cmd, (v && v.mission_upload) || null);
    if (stg.state === "done") {
      model = { ...model, upload: { ...model.upload, phase: "uploaded", result: {
        cmdId: cmd.id, hash: (cmd.params && cmd.params.expected_route_content_hash) || null,
        waypoints: (cmd.params && cmd.params.expected_route_waypoint_count) || model.generated.metrics.waypoint_count,
        vehicleId: model.vehicleId,
        missionId: model.upload.missionId || null,
        revision: model.upload.revision != null ? model.upload.revision : 0,
      } } };
      renderAll();
    } else if (stg.state === "failed") {
      model = { ...model, upload: { ...model.upload, phase: "error", error: stg.reason || "Upload was not verified by read-back." } };
      renderAll();
    }
  }
  function renderUploadStatus() {
    const u = model.upload;
    if (u.phase === "uploading") {
      const cmd = trackedUpload();
      const v = fleet.find((x) => x.id === model.vehicleId);
      const stg = cmd ? missionUploadStage(cmd, (v && v.mission_upload) || null) : null;
      return `<span class="pl-upl">${stg ? stg.stage : "Queued"}…</span>`;
    }
    if (u.phase === "error") return `<span class="pl-upl bad">Upload failed — ${esc(u.error || "not verified")}. Plan preserved.</span>`;
    if (u.phase === "uploaded" && u.result) {
      const h = u.result.hash ? String(u.result.hash).replace(/^sha256:/, "").slice(0, 12) : "—";
      const mid = u.result.missionId ? `${esc(u.result.missionId)} · rev ${u.result.revision} · ` : "";
      return `<span class="pl-upl ok">Uploaded &amp; verified · ${mid}${u.result.waypoints} wp · hash ${h} · <a href="#/map" class="pl-link">Open on Map</a></span>`;
    }
    return "";
  }

  // ═══════════════ DRAFTS ═══════════════
  async function saveDraft() {
    const name = window.prompt("Draft name:", `Survey ${new Date().toLocaleString()}`);
    if (name == null) return;
    const res = await api.createDraft(P.toDraft(model, name));
    if (res.ok) { showToast("Draft saved.", "ok"); loadDrafts(); } else showToast("Could not save draft.", "warn");
  }
  async function openLoadDraft() {
    await loadDrafts();
    if (!drafts.length) { showToast("No saved drafts.", "warn"); return; }
    const list = drafts.map((d, i) => `${i + 1}. ${d.name} (${d.state || "?"}, ${d.waypoint_count ?? "?"} wp)`).join("\n");
    const pick = window.prompt(`Load which draft?\n\n${list}\n\nEnter a number (or blank to cancel):`);
    const idx = pick ? +pick - 1 : -1;
    if (idx < 0 || idx >= drafts.length) return;
    const res = await api.getDraft(drafts[idx].id);
    if (res && res.ok && res.draft) {
      history.length = 0; selected = null; draftRing = []; mode = null;
      model = P.fromDraft(res.draft);
      renderAll(); fitToPlan(); showToast("Draft loaded.", "ok");
      selectVehicleSideEffects(model.vehicleId);
    } else showToast("Could not load draft.", "warn");
  }
  function loadDrafts() {
    return api.listDrafts().then((d) => { drafts = (d && d.drafts) || []; renderActions(); }).catch(() => {});
  }

  // ═══════════════ helpers ═══════════════
  function fitToPlan() {
    const pts = [];
    if (model.boundary) model.boundary.forEach((p) => pts.push(toLL(p)));
    if (planMode === "fleet") {
      fleetModel.selectedVehicleIds.forEach((id) => { const c = FP.vehicleConfig(fleetModel, id); if (c && c.home) pts.push(toLL(c.home)); });
      const fp = fleetModel.generated;
      if (fp && Array.isArray(fp.vehicles)) fp.vehicles.forEach((vp) => (vp.mission_package.route_waypoints || []).forEach((w) => pts.push([w.latitude, w.longitude])));
    } else {
      (model.generated && model.generated.route_waypoints || []).forEach((w) => pts.push([w.latitude, w.longitude]));
    }
    if (pts.length) map.fitBounds(L.latLngBounds(pts), { padding: [40, 40], maxZoom: 18 });
  }
  function vehName(id) { const v = fleet.find((x) => x.id === id); return id == null ? "—" : (v ? (v.name || "USV-" + id) : "USV-" + id); }
  function selectVehicleSideEffects(id) {
    cmds = []; authority = null;
    if (id != null) { loadCommands(id); loadAuthority(id); }
  }
  function loadCommands(id) { if (id == null) return; api.getCommands(id).then((d) => { if (id === model.vehicleId) { cmds = (d && d.commands) || []; syncUploadFromCommands(); renderBanner(); } }).catch(() => {}); }
  function loadAuthority(id) { if (id == null) return; api.getControlAuthority(id).then((a) => { if (id === model.vehicleId) { authority = a; renderActions(); } }).catch(() => {}); }

  let toastTimer = null;
  function showToast(msg, kind = "warn") {
    const box = document.getElementById("plan-toast");
    if (!box) return;
    box.className = `ov toast ${kind}`; box.textContent = msg; box.style.display = "flex";
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { box.style.display = "none"; }, 5000);
  }
  function esc(s) { return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
  function fmtArea(m2) { if (m2 == null) return "—"; return m2 >= 1e4 ? `${(m2 / 1e4).toFixed(2)} ha` : `${Math.round(m2)} m²`; }
  function fmtLen(m) { if (m == null) return "—"; return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`; }
  function fmtDur(s) { if (s == null) return "—"; const h = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = Math.round(s % 60); return h ? `${h}h ${mm}m` : mm ? `${mm}m ${ss}s` : `${ss}s`; }

  // ═══════════════ INITIAL VIEW: dynamic centering ═══════════════
  // Recenter to the strongest currently-available source, but only when it is STRICTLY
  // stronger than the source we are already on and the operator has not taken manual control
  // of the camera. Async arrivals (first fleet payload, geolocation) call this; a later even
  // stronger source (a fresh USV over a geolocation fix) can still upgrade it.
  function recenterIfBetter() {
    if (userInteracted) return;
    const selected = model.vehicleId != null ? fleet.find((v) => v.id === model.vehicleId) : null;
    const view = pickInitialView({
      selected, fleet, selectedId: model.vehicleId, geo,
      saved: getSavedViewport(), fallback: TOFTASJON,
    });
    if (view.rank < viewRank) {
      programmaticMove = true;
      map.setView(view.center, view.zoom, { animate: false });
      viewRank = view.rank;
    }
  }

  // Explicit / selection-driven centre on the selected USV. Validates the coordinate
  // (rejecting invalid + Null Island) before moving; `explicit` toasts on a missing position
  // and animates. Bypasses the strict-rank guard so switching between two USVs (both rank 1)
  // still recentres, and so an explicit button press works even after manual panning.
  function centerOnSelected({ explicit } = {}) {
    const v = model.vehicleId != null ? fleet.find((x) => x.id === model.vehicleId) : null;
    if (!v || !isValidLatLng(v.lat, v.lng) || isNullIsland(v.lat, v.lng)) {
      if (explicit) showToast("No valid position for the selected USV yet.", "warn");
      return false;
    }
    programmaticMove = true;
    map.setView([v.lat, v.lng], DEFAULT_ZOOM, { animate: !!explicit });
    viewRank = VIEW_RANK.selected;
    return true;
  }

  // Browser geolocation (task priority 3). Non-blocking; on success stores `geo` and either
  // runs the caller's callback or recenters if it is now the best source. Silent on denial —
  // other sources remain, and an automatic request never nags.
  function requestGeolocation(onOk, onErr) {
    if (typeof navigator === "undefined" || !navigator.geolocation) { if (onErr) onErr(); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        const g = { lat: pos.coords.latitude, lng: pos.coords.longitude };
        if (!isValidLatLng(g.lat, g.lng) || isNullIsland(g.lat, g.lng)) { if (onErr) onErr(); return; }
        geo = g;
        if (onOk) onOk(g); else recenterIfBetter();
      },
      () => { if (onErr) onErr(); },
      { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 }
    );
  }

  // Only request geolocation when no fresh USV already gives us a better view — so we do not
  // prompt the operator for location the common case where the fleet supplies the centre.
  function maybeRequestGeo() {
    if (geoRequested || userInteracted) return;
    if (viewRank <= VIEW_RANK.fleet) return;   // already centred on a USV
    geoRequested = true;
    requestGeolocation();
  }

  function centerOnOperator() {
    requestGeolocation(
      (g) => { programmaticMove = true; map.setView([g.lat, g.lng], DEFAULT_ZOOM, { animate: true }); viewRank = VIEW_RANK.geolocation; },
      () => showToast("Could not get your location (permission denied or unavailable).", "warn")
    );
  }

  // ═══════════════ fleet poll + lifecycle ═══════════════
  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    // Refresh the vehicle <select> label + upload lifecycle without disturbing drawing.
    renderTools();
    if (model.vehicleId != null) { syncUploadFromCommands(); }
    // Upgrade the initial view now that positions are available, then (if still needed)
    // fall back to geolocation.
    recenterIfBetter();
    maybeRequestGeo();
    updateRibbon({ counts: counts() });
    updateFeed();
  }
  function counts() { const c = { c: 0, p: 0, d: 0 }; fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; }); return c; }
  function updateFeed() {
    const h = api.getFeedHealth("fleet");
    if (!h || h.lastOkAt == null) { updateRibbon({ feed: { cls: h && h.lastErrAt ? "bad" : "dim", label: h && h.lastErrAt ? "BACKEND UNREACHABLE" : "CONNECTING…" } }); return; }
    const ageS = (Date.now() - h.lastOkAt) / 1000;
    updateRibbon({ feed: ageS <= 4 ? { cls: "ok", label: "LIVE" } : ageS <= 12 ? { cls: "warn", label: `DELAYED ${Math.round(ageS)}s` } : { cls: "bad", label: `UNREACHABLE ${Math.round(ageS)}s` } });
  }

  // Static map view-control buttons (wired once — not part of the re-rendered panels).
  bind("pl-center-usv", () => centerOnSelected({ explicit: true }));
  bind("pl-center-op", centerOnOperator);

  api.getCommandCapabilities().then((c) => { capabilities = c; }).catch(() => {});
  loadDrafts();
  // Load the adopted vehicle's command/authority context so an upload can proceed without
  // re-selecting it in the dropdown.
  if (model.vehicleId != null) selectVehicleSideEffects(model.vehicleId);
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, updateFeed, "fleet", { pauseWhenHidden: true });
  const cmdTimer = setInterval(() => { loadCommands(model.vehicleId); if (planMode === "fleet") syncFleetUpload(); }, 3000);
  const authTimer = setInterval(() => loadAuthority(model.vehicleId), 4000);
  const clockId = setInterval(() => { updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }); }, 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
  // Safety net: if the fleet never loads (backend down) a fresh USV view never arrives, so
  // still offer geolocation as a better-than-fallback source after a short delay.
  const geoTimer = setTimeout(maybeRequestGeo, 2500);

  // Warn before navigating away with unsaved planning changes (hash router → beforeunload
  // covers a real tab close/reload; the in-app nav is a hash change the operator initiates).
  function beforeUnload(e) {
    const fleetWork = planMode === "fleet" && (FP.hasFleetPlan(fleetModel) || FP.selectedCount(fleetModel) > 0)
      && !FP.fleetReady(fleetModel, model);
    if ((P.hasUnsavedWork(model) && model.upload.phase !== "uploaded") || fleetWork) { e.preventDefault(); e.returnValue = ""; }
  }
  window.addEventListener("beforeunload", beforeUnload);

  renderAll();
  // (no setTimeout size recalc any more — attachMapLayout's ResizeObserver fires as soon
  // as the grid gives the stage a real box, and keeps firing for every later change)

  return function cleanup() {
    stopFleet(); clearInterval(cmdTimer); clearInterval(authTimer); clearInterval(clockId);
    clearTimeout(geoTimer);
    window.removeEventListener("beforeunload", beforeUnload);
    if (toastTimer) clearTimeout(toastTimer);
    detachMapLayout();
    map.remove();
  };
}
