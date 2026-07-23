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
import { missionUploadStage, UPLOAD_STAGES } from "../lib/mission-upload.js";
import { hasPendingOfType } from "../lib/command.js";

const infoIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v0.01M11 12h1v4h1"/></svg>';

// Distinct styles per geometry/segment kind — kept restrained so the map never becomes
// visually overwhelming (a handful of hues + line styles, not a rainbow).
const SEG_STYLE = {
  primary:   { color: "#3ECF8E", weight: 3, opacity: 0.9 },
  secondary: { color: "#A78BFA", weight: 3, opacity: 0.9, dashArray: "1 0" },
  transit:   { color: "#F2A93B", weight: 2.4, opacity: 0.85, dashArray: "7 6" },
  transition:{ color: "#8FA3B8", weight: 2, opacity: 0.7, dashArray: "3 5" },
  return:    { color: "#F2A93B", weight: 2.4, opacity: 0.85, dashArray: "2 6" },
};
const BOUNDARY_STYLE = { color: "#4C8DFF", weight: 2.2, opacity: 0.95, fill: false };
const NAVIGABLE_STYLE = { color: "#3ECF8E", weight: 1, opacity: 0.5, dashArray: "4 5", fill: true, fillColor: "#3ECF8E", fillOpacity: 0.05 };
const NOGO_STYLE = { color: "#E5484D", weight: 1.6, opacity: 0.9, fill: true, fillColor: "#E5484D", fillOpacity: 0.16 };
const NOGO_SEL_STYLE = { ...NOGO_STYLE, weight: 2.4, dashArray: "5 4" };

const WORKFLOW = [
  ["vehicle", "Vehicle"], ["area", "Survey Area"], ["restrictions", "Restrictions"],
  ["pattern", "Survey Pattern"], ["preview", "Route Preview"], ["validate", "Validate"],
  ["finish", "Finish & Upload"],
];

export function Plan(root) {
  const L = window.L;
  let fleet = [];
  let model = P.emptyModel();
  let mode = null;            // active drawing mode: null|'boundary'|'nogo'|'home'|'transit'
  let draftRing = [];         // in-progress polygon vertices ([lng,lat]) while drawing
  let selected = null;        // { type:'boundary' } | { type:'nogo', id } | { type:'transit', index } | { type:'home' }
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
       <div id="plan-map"></div>
       <div class="ov plan-banner" id="plan-banner"></div>
       <div class="ov legend plan-legend" id="plan-legend">
         <div class="legend-h"><span class="lbl">Legend</span></div>
         <div class="legend-body">
           <div class="li"><span class="pl-sw boundary"></span>Survey boundary</div>
           <div class="li"><span class="pl-sw navigable"></span>Navigable (shoreline-offset)</div>
           <div class="li"><span class="pl-sw nogo"></span>No-go zone</div>
           <div class="li"><span class="pl-sw transit"></span>Transit / return</div>
           <div class="li"><span class="pl-sw primary"></span>Primary pass</div>
           <div class="li"><span class="pl-sw secondary"></span>Secondary pass</div>
           <div class="li"><span class="pl-sw home"></span>Planning home</div>
         </div>
       </div>
       <div class="ov toast" id="plan-toast"></div>
       <div class="plan-actionbar" id="plan-actions"></div>
     </div>
     <aside class="inspector plan-inspector" id="plan-inspector"></aside>`;

  // ---- Leaflet ----
  const HOME_VIEW = [56.699893, 13.002148];
  const map = L.map("plan-map", { zoomControl: true, attributionControl: false }).setView(HOME_VIEW, 16);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 20 }).addTo(map);
  map.on("click", onMapClick);
  map.on("dblclick", onMapDblClick);
  map.on("contextmenu", onMapRightClick);

  // Layer groups, rebuilt on every render — simple and flicker-free at this scale.
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
    else if (mode === "transit") { apply(P.setTransit(model, [...model.transit, pt])); }
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
    else if (selected.type === "transit") apply(P.setTransit(model, model.transit.filter((_, i) => i !== selected.index)));
    selected = null; renderAll();
  }
  async function clearAll() {
    if (P.hasUnsavedWork(model) && !window.confirm(
      "Clear the entire plan?\n\nThis removes the boundary, no-go zones, home, transit " +
      "waypoints, the generated route and validation. It does NOT touch the mission stored " +
      "on any vehicle and issues no command. This cannot be undone.")) return;
    history.length = 0; selected = null; draftRing = []; mode = null;
    const keepVehicle = model.vehicleId;
    model = P.clearModel(); model.vehicleId = keepVehicle;
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
    const mk = L.marker(toLL(ring[idx]), { draggable: true, icon: L.divIcon({ className: "", html: '<div class="plan-vhandle"></div>', iconSize: [12, 12], iconAnchor: [6, 6] }) });
    mk.on("drag", (e) => onMove(idx, fromLL(e.latlng), false));
    mk.on("dragend", (e) => onMove(idx, fromLL(e.latlng), true));
    return mk;
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
    // Navigable (shoreline-offset) area — only from a generated route (never faked).
    const nav = model.generated && model.generated.navigable_boundary;
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

    // Generated route segments (distinct styles) + generated waypoint dots.
    if (model.generated && Array.isArray(model.generated.segments)) {
      model.generated.segments.forEach((s) => {
        const style = SEG_STYLE[s.kind];
        if (style && s.coordinates.length > 1) {
          L.polyline(s.coordinates.map(toLL), { ...style }).addTo(layers.route);
        }
      });
      (model.generated.route_waypoints || []).forEach((w, i) => {
        L.circleMarker([w.latitude, w.longitude], { radius: 2.6, color: "#0C141C", weight: 0.6, fillColor: "#DCE3EC", fillOpacity: 0.9 })
          .bindTooltip(`WP ${i + 1}`, { sticky: true }).addTo(layers.markers);
      });
    }

    // Transit waypoints (ordered, distinct from coverage waypoints).
    model.transit.forEach((pt, i) => {
      const sel = selected && selected.type === "transit" && selected.index === i;
      L.marker(toLL(pt), { icon: L.divIcon({ className: "", html: `<div class="plan-transit-wp${sel ? " sel" : ""}">${i + 1}</div>`, iconSize: [18, 18], iconAnchor: [9, 9] }) })
        .on("click", (e) => { L.DomEvent.stop(e); selected = { type: "transit", index: i }; renderAll(); })
        .addTo(layers.markers);
    });

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
  function renderTools() {
    const st = P.planState(model);
    document.getElementById("plan-state").textContent = st;
    document.getElementById("plan-state").className = "lbl plan-state-chip " + st.toLowerCase();

    const steps = WORKFLOW.map(([k, label]) => {
      const done = stepDone(k);
      return `<div class="plan-step ${done ? "done" : ""}"><span class="pl-dot">${done ? "✓" : ""}</span>${label}</div>`;
    }).join("");

    const vehOpts = fleet.map((v) => `<option value="${v.id}" ${v.id === model.vehicleId ? "selected" : ""}>${v.name || "USV-" + v.id} · ${commState(v)}</option>`).join("");
    const drawing = (mode === "boundary" || mode === "nogo");
    const zoneList = model.noGoZones.map((z) => `<div class="plan-item ${selected && selected.type === "nogo" && selected.id === z.id ? "sel" : ""}" data-zone="${z.id}"><span>${z.id}</span><button class="plan-x" data-rmzone="${z.id}" title="Remove zone">✕</button></div>`).join("") || `<div class="plan-empty">No no-go zones</div>`;
    const transitList = model.transit.map((_, i) => `<div class="plan-item ${selected && selected.type === "transit" && selected.index === i ? "sel" : ""}" data-transit="${i}"><span>WP ${i + 1}</span><span class="plan-item-btns"><button data-tup="${i}" title="Move up" ${i === 0 ? "disabled" : ""}>▲</button><button data-tdn="${i}" title="Move down" ${i === model.transit.length - 1 ? "disabled" : ""}>▼</button><button class="plan-x" data-rmtransit="${i}" title="Remove">✕</button></span></div>`).join("") || `<div class="plan-empty">No transit waypoints</div>`;

    document.getElementById("plan-tools").innerHTML = `
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

      <div class="plan-sec"><span class="lbl">Home &amp; transit</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool ${mode === "home" ? "on" : ""}" id="pl-home">${model.home ? "Move home" : "Set home"}</button>
        <button class="plan-tool ${mode === "transit" ? "on" : ""}" id="pl-transit">Add transit WP</button>
      </div>
      <div class="plan-list">${transitList}</div>

      <div class="plan-sec"><span class="lbl">Edit</span></div>
      <div class="plan-btnrow">
        <button class="plan-tool" id="pl-undo" ${history.length || draftRing.length ? "" : "disabled"}>Undo</button>
        <button class="plan-tool" id="pl-delsel" ${selected ? "" : "disabled"}>Delete selected</button>
        <button class="plan-tool danger" id="pl-clear">Clear all</button>
      </div>`;

    // wire
    const veh = document.getElementById("plan-veh");
    if (veh) veh.onchange = () => { const id = veh.value ? +veh.value : null; model = { ...model, vehicleId: id }; selectVehicleSideEffects(id); renderAll(); };
    bind("pl-draw-boundary", () => setMode("boundary"));
    bind("pl-del-boundary", () => apply(P.setBoundary(model, null)));
    bind("pl-draw-nogo", () => { if (P.canAddZone(model)) setMode("nogo"); });
    bind("pl-home", () => setMode("home"));
    bind("pl-transit", () => setMode("transit"));
    bind("pl-finish", finishShape);
    bind("pl-cancel", cancelDraw);
    bind("pl-undo", undo);
    bind("pl-delsel", deleteSelected);
    bind("pl-clear", clearAll);
    document.querySelectorAll("[data-rmzone]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); apply(P.removeNoGoZone(model, b.dataset.rmzone)); if (selected && selected.id === b.dataset.rmzone) selected = null; renderAll(); });
    document.querySelectorAll("[data-zone]").forEach((el) => el.onclick = () => { selected = { type: "nogo", id: el.dataset.zone }; renderAll(); });
    document.querySelectorAll("[data-rmtransit]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); apply(P.setTransit(model, model.transit.filter((_, i) => i !== +b.dataset.rmtransit))); selected = null; renderAll(); });
    document.querySelectorAll("[data-tup]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); reorderTransit(+b.dataset.tup, -1); });
    document.querySelectorAll("[data-tdn]").forEach((b) => b.onclick = (e) => { e.stopPropagation(); reorderTransit(+b.dataset.tdn, +1); });
  }
  function bind(id, fn) { const e = document.getElementById(id); if (e) e.onclick = fn; }
  function reorderTransit(i, dir) {
    const t = [...model.transit]; const j = i + dir;
    if (j < 0 || j >= t.length) return;
    [t[i], t[j]] = [t[j], t[i]];
    apply(P.setTransit(model, t));
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

  // ═══════════════ RIGHT PANEL: params, validation, summary ═══════════════
  function renderInspector() {
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
      ["Total route length", fmtLen(m.total_length_m)],
      ["Coverage length", fmtLen(m.coverage_length_m)],
      ["Transit / return length", fmtLen(m.transit_length_m)],
      ["Waypoints", `${m.waypoint_count}${model.generated.max_route_waypoints ? ` / ${model.generated.max_route_waypoints}` : ""}`],
      ["Estimated duration", m.estimated_duration_s != null ? `${fmtDur(m.estimated_duration_s)} (est.${m.survey_speed_is_default ? ", default speed" : ""})` : noTelem("no speed")],
      ["Generated", model.generated.generated_at ? new Date(model.generated.generated_at).toLocaleTimeString([], { hour12: false }) : "—"],
    ];
    const warns = (model.generated.warnings || []).map((w) => `<li class="warn">${esc(w)}</li>`).join("");
    return `<div class="plan-sumgrid">${rows.map(([k, val]) => `<div class="plan-srow"><span class="k">${k}</span><span class="v">${val}</span></div>`).join("")}</div>
      ${warns ? `<ul class="plan-vlist">${warns}</ul>` : ""}
      <div class="plan-note">${infoIcon}<span>Duration is an estimate only. Distances are geodesic over the generated route.</span></div>`;
  }

  // ═══════════════ ACTION BAR + BANNER ═══════════════
  function renderActions() {
    const st = P.planState(model);
    const genLabel = P.hasRoute(model) ? (P.isOutdated(model) ? "Regenerate" : "Regenerate") : "Generate route";
    document.getElementById("plan-actions").innerHTML = `
      <button class="pl-act" id="act-clear">Clear</button>
      <button class="pl-act" id="act-savedraft">Save draft</button>
      <button class="pl-act" id="act-loaddraft">Load draft${drafts.length ? ` (${drafts.length})` : ""}</button>
      <div class="pl-act-grow"></div>
      <button class="pl-act primary" id="act-generate" ${P.canGenerate(model) && !busyGen ? "" : "disabled"}>${busyGen ? "Generating…" : genLabel}</button>
      <button class="pl-act" id="act-validate" ${P.hasRoute(model) && !P.isOutdated(model) && !busyVal ? "" : "disabled"}>${busyVal ? "Validating…" : "Validate"}</button>
      <button class="pl-act success" id="act-upload" ${P.canUpload(model) ? "" : "disabled"} title="${uploadTitle()}">Finish &amp; Upload</button>`;
    bind("act-clear", clearAll);
    bind("act-savedraft", saveDraft);
    bind("act-loaddraft", openLoadDraft);
    bind("act-generate", doGenerate);
    bind("act-validate", doValidate);
    bind("act-upload", doUpload);
  }
  function uploadTitle() {
    if (model.vehicleId == null) return "Select a vehicle first";
    if (!P.hasRoute(model)) return "Generate a route first";
    if (P.isOutdated(model)) return "Route is outdated — regenerate first";
    if (!(model.validation && model.validation.ok)) return "Validate the route first";
    if (!hasControl()) return "Take OPERATOR control (Map/Vehicle page) before uploading";
    return "Finalize and upload the mission through the verified path";
  }
  function renderBanner() {
    const st = P.planState(model);
    const b = document.getElementById("plan-banner");
    let cls = "info", extra = "";
    if (st === "ROUTE_OUTDATED" || st === "ERROR") cls = "warn";
    else if (st === "VALID" || st === "UPLOADED") cls = "ok";
    if (genError) { cls = "warn"; extra = ` — ${esc(genError)}`; }
    const uploadInfo = renderUploadStatus();
    b.className = "ov plan-banner " + cls;
    b.innerHTML = `<span class="pl-state">${st}</span><span class="pl-msg">${P.PLAN_STATE_LABEL[st] || ""}${extra}</span>${uploadInfo}`;
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
  function trackedUpload() { return model.upload.cmdId ? (cmds.find((c) => c.id === model.upload.cmdId) || null) : null; }
  async function doUpload() {
    if (!P.canUpload(model)) return;
    if (!hasControl()) { showToast("Take OPERATOR control on the Map or Vehicle page before uploading.", "warn"); return; }
    const params = P.uploadParamsFromModel(model);
    if (!params) { showToast("The generated route could not be prepared for upload.", "warn"); return; }
    const v = fleet.find((x) => x.id === model.vehicleId);
    const vname = v ? (v.name || "USV-" + v.id) : "the vehicle";
    const n = model.generated.metrics.waypoint_count;
    if (!window.confirm(
      `Finish & upload this survey to ${vname}?\n\n` +
      `${n} route waypoints (Scout adds Home at seq 0 → ${n + 1} Pixhawk items).\n\n` +
      `This OVERWRITES the mission stored on the flight controller and is confirmed only by ` +
      `read-back verification. It does NOT start the mission.`)) return;
    model = { ...model, upload: { phase: "uploading", cmdId: null, error: null, at: Date.now(), result: null } };
    renderAll();
    const res = await api.uploadMission(model.vehicleId, params);
    if (res.ok && res.data && res.data.command) {
      model = { ...model, upload: { ...model.upload, cmdId: res.data.command.id } };
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
      return `<span class="pl-upl ok">Uploaded &amp; verified · ${u.result.waypoints} wp · hash ${h} · <a href="#/map" class="pl-link">Open on Map</a></span>`;
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
    (model.generated && model.generated.route_waypoints || []).forEach((w) => pts.push([w.latitude, w.longitude]));
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

  // ═══════════════ fleet poll + lifecycle ═══════════════
  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    // Refresh the vehicle <select> label + upload lifecycle without disturbing drawing.
    renderTools();
    if (model.vehicleId != null) { syncUploadFromCommands(); }
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

  api.getCommandCapabilities().then((c) => { capabilities = c; }).catch(() => {});
  loadDrafts();
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, updateFeed, "fleet");
  const cmdTimer = setInterval(() => loadCommands(model.vehicleId), 3000);
  const authTimer = setInterval(() => loadAuthority(model.vehicleId), 4000);
  const clockId = setInterval(() => { updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }); }, 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  // Warn before navigating away with unsaved planning changes (hash router → beforeunload
  // covers a real tab close/reload; the in-app nav is a hash change the operator initiates).
  function beforeUnload(e) { if (P.hasUnsavedWork(model) && model.upload.phase !== "uploaded") { e.preventDefault(); e.returnValue = ""; } }
  window.addEventListener("beforeunload", beforeUnload);

  renderAll();
  // Leaflet needs a size recalc once the grid has laid out.
  setTimeout(() => map.invalidateSize(), 60);

  return function cleanup() {
    stopFleet(); clearInterval(cmdTimer); clearInterval(authTimer); clearInterval(clockId);
    window.removeEventListener("beforeunload", beforeUnload);
    if (toastTimer) clearTimeout(toastTimer);
    map.remove();
  };
}
