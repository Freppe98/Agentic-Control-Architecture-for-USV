// Config.js — the operator station's configuration surface. Honest by construction:
// the backend has no configuration endpoint, so this page never pretends to write to
// FastAPI. It is organized into three sections whose backing is clearly distinct:
//
//   1. Communication thresholds — REAL backend behavior, but READ-ONLY. The values
//      mirror the compiled-in constants in main.py (STALE/PARTITIONED/DISCONNECTED_
//      AFTER_SECONDS). There is no runtime endpoint to read or change them, so we
//      show them read-only and name that gap plainly. Editing them is a backend gap.
//   2. Operator preferences — GENUINE client-side persistence (localStorage, this
//      browser only). Stored honestly; pages honor them as they are migrated.
//   3. Vehicle registry — LIVE read-only data from api.getFleet(). Registry-only
//      fields the backend does not carry (callsign, onboard address) render NO-TELEM.
//
// Reuses: Ribbon, NavRail, Table, CommsPill, ThresholdTimeline, form/Select, form/Toggle.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { Table } from "../components/Table.js";
import { CommsPill } from "../components/CommsPill.js";
import { ThresholdTimeline } from "../components/ThresholdTimeline.js";
import { Select } from "../components/form/Select.js";
import { Toggle } from "../components/form/Toggle.js";
import { commState, noTelem } from "../lib/ui.js";
import { getPrefs, setPref, resetPrefs, prefsPersistable, PREF_DEFAULTS } from "../lib/prefs.js";

// Mirrors main.py: STALE_AFTER_SECONDS / PARTITIONED_AFTER_SECONDS /
// DISCONNECTED_AFTER_SECONDS. These are the backend's compiled-in constants; there is
// no endpoint to read them at runtime, so they are duplicated here READ-ONLY. If the
// backend constants change, update these to match. Never presented as editable.
const BACKEND_THRESHOLDS = { stale: 8, partitioned: 15, disconnected: 30 };

const infoIcon =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v0.01M11 12h1v4h1"/></svg>';

export function Config(root) {
  let fleet = [];
  let prefs = getPrefs();
  const persistable = prefsPersistable();

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("config") +
    `<div class="page cfg-page">
       <div class="toolbar"><h1>Configuration</h1><span class="count">operator station</span></div>
       <div class="cfg" id="cfg"></div>
     </div>`;

  // ---- Section 1: Communication thresholds (read-only, backend-defined) ----
  function thresholdsSection() {
    const t = BACKEND_THRESHOLDS;
    const row = (label, val, desc) =>
      `<div class="cfg-ro">
         <div class="cfg-ro-l"><span class="fld-lbl">${label}</span><span class="fld-hint">${desc}</span></div>
         <div class="cfg-ro-v mono">${val}<small>s</small></div>
       </div>`;
    return `
      <section class="cfg-card">
        <div class="cfg-h">
          <div><h2>Communication thresholds</h2><p class="cfg-sub">Time-since-last-contact bands that derive every vehicle's comm-state.</p></div>
          <span class="cfg-tag ro">Backend-defined · read-only</span>
        </div>
        <div class="cfg-body">
          ${ThresholdTimeline(t)}
          <div class="cfg-ro-grid">
            ${row("Stale after", t.stale, "telemetry starts dimming; still connected")}
            ${row("Partitioned after", t.partitioned, "link degraded — comms partitioned")}
            ${row("Disconnected after", t.disconnected, "contact lost — vehicle offline")}
            <div class="cfg-ro">
              <div class="cfg-ro-l"><span class="fld-lbl">Heartbeat interval</span><span class="fld-hint">expected agent report cadence</span></div>
              <div class="cfg-ro-v">${noTelem("not in backend")}</div>
            </div>
          </div>
          <div class="cfg-note">
            ${infoIcon}
            These values are compiled into the backend (main.py) and drive comm-state for
            the whole fleet. A configuration write endpoint (e.g. POST /api/config) is a
            known backend gap, so they are read-only here — nothing on this page is sent
            to the server.
          </div>
        </div>
      </section>`;
  }

  // ---- Section 2: Operator preferences (client-side, this browser) ----
  function preferencesSection() {
    const controls =
      Select({ id: "speed_units", label: "Speed units", value: prefs.speed_units,
        options: [["ms", "Metres / second (m/s)"], ["kn", "Knots (kn)"]],
        hint: "Display unit for ground speed" }) +
      Select({ id: "coord_format", label: "Coordinate format", value: prefs.coord_format,
        options: [["decimal", "Decimal degrees"], ["dms", "Deg / min / sec"]],
        hint: "How lat/lng is shown" }) +
      Select({ id: "base_layer", label: "Map base layer", value: prefs.base_layer,
        options: [["streets", "Streets"], ["dark", "Dark"], ["satellite", "Satellite"]],
        hint: "Preferred map tiles" }) +
      Toggle({ id: "clock_24h", label: "24-hour clock", value: prefs.clock_24h,
        hint: "Ribbon and log timestamps" });

    const badge = persistable
      ? `<span class="cfg-tag local">Saved in this browser</span>`
      : `<span class="cfg-tag warn">Storage unavailable</span>`;

    return `
      <section class="cfg-card">
        <div class="cfg-h">
          <div><h2>Operator preferences</h2><p class="cfg-sub">Display choices for this workstation. No server profile exists.</p></div>
          ${badge}
        </div>
        <div class="cfg-body">
          <div class="cfg-form">${controls}</div>
          <div class="cfg-actions">
            <button class="cfg-btn" id="cfg-reset">Reset to defaults</button>
            <span class="cfg-save" id="cfg-save"></span>
          </div>
          <div class="cfg-note">
            ${infoIcon}
            ${persistable
              ? `Preferences persist in this browser only (localStorage) — they are never sent to the backend. Migrated pages read them as they are wired; a preference stored here does not change a page that does not yet consume it.`
              : `This browser is blocking local storage (e.g. private mode), so preferences cannot be saved and will reset on reload.`}
          </div>
        </div>
      </section>`;
  }

  // ---- Section 3: Vehicle registry (live, read-only) ----
  const registryColumns = [
    { key: "id", label: "ID", render: (v) => `<span class="mono">${v.id}</span>` },
    { key: "name", label: "Name", render: (v) => `<span class="vname"><b>${v.name || "USV-" + v.id}</b></span>` },
    { key: "callsign", label: "Callsign", render: () => noTelem("registry") },
    { key: "comms", label: "Comms", render: (v) => CommsPill(v) },
    { key: "onboard", label: "Onboard address", render: () => noTelem("registry") },
  ];

  function registrySection() {
    const body = fleet.length
      ? Table(registryColumns, fleet, { idKey: "id" })
      : `<div class="empty-state" style="padding:14px 16px">Waiting for fleet…</div>`;
    return `
      <section class="cfg-card">
        <div class="cfg-h">
          <div><h2>Vehicle registry</h2><p class="cfg-sub">Vehicles known to the backend fleet template, live from /api/fleet/status.</p></div>
          <span class="cfg-tag ro">Live · read-only</span>
        </div>
        <div class="cfg-body">
          <div class="cfg-tablewrap">${body}</div>
          <div class="cfg-note">
            ${infoIcon}
            Identity comes from the live fleet. Callsign and onboard address are registry
            fields the backend does not carry yet (shown NO-TELEM), and there is no
            endpoint to add or edit vehicles — registry editing is a known backend gap.
          </div>
        </div>
      </section>`;
  }

  function render() {
    document.getElementById("cfg").innerHTML =
      thresholdsSection() + preferencesSection() + registrySection();
    wirePreferences();
  }

  // Re-render only the registry table in place (avoids resetting an open <select>).
  function renderRegistryOnly() {
    const wrap = document.querySelector("#cfg .cfg-tablewrap");
    if (!wrap) return render();
    wrap.innerHTML = fleet.length
      ? Table(registryColumns, fleet, { idKey: "id" })
      : `<div class="empty-state" style="padding:14px 16px">Waiting for fleet…</div>`;
  }

  let saveTimer = null;
  function flashSave(msg, ok = true) {
    const el = document.getElementById("cfg-save");
    if (!el) return;
    el.textContent = msg;
    el.className = "cfg-save show" + (ok ? "" : " err");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { el.className = "cfg-save"; }, 2200);
  }

  function commit(id, value) {
    const ok = setPref(id, value);
    prefs = getPrefs();
    flashSave(ok ? "Saved in this browser" : "Could not save — storage blocked", ok);
  }

  function wirePreferences() {
    document.querySelectorAll('#cfg select[data-pref]').forEach((s) => {
      s.onchange = () => commit(s.dataset.pref, s.value);
    });
    document.querySelectorAll('#cfg button.tgl[data-pref]').forEach((b) => {
      b.onclick = () => {
        const next = !(b.getAttribute("aria-checked") === "true");
        b.classList.toggle("on", next);
        b.setAttribute("aria-checked", next ? "true" : "false");
        commit(b.dataset.pref, next);
      };
    });
    const reset = document.getElementById("cfg-reset");
    if (reset) reset.onclick = () => { prefs = resetPrefs(); render(); flashSave("Reset to defaults", persistable); };
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    renderRegistryOnly();
    updateRibbon({ counts: counts() });
  }

  render();
  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); clearTimeout(saveTimer); };
}
