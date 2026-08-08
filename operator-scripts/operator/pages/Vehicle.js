// Vehicle.js — the professional diagnostics page. Fleet systems matrix, then the
// selected vehicle organized into named sections an operator can scan top-to-bottom:
// Vehicle Health, Control, Power, Communication, Local Agent, Sensors, System.
//
// Every diagnostic here is ALWAYS LIVE — there is no "Run System Check" button or
// simulated delay. The previous version's check ran a fake 550ms timer and then
// re-displayed values already computed from the fleet payload (Scout exposes no real
// diagnostics endpoint — see BACKEND_ROADMAP.md); that click-and-wait added workload
// for zero new information, so every one of its checks now renders continuously in
// the section it actually belongs to instead.
//
// Vehicle Health's Home verification / Current waypoint / Mission loaded reuse the
// SAME Pixhawk mission readback + lib/mission.js math as Map.js and Mission.js — this
// page can never disagree with those two about the same vehicle's mission state.
//
// Reuses VehicleDock, CommsPill, BatteryBar, AuthoritySeg, ui/home/mission helpers.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
import { AuthoritySeg } from "../components/AuthoritySeg.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, cls, fmtAge, pad3, noTelem } from "../lib/ui.js";
import { createAuthorityController, handoffGate } from "../lib/authority.js";
import { isSafetyHold, SAFETY_HOLD_TITLE, homeStatus, commandGate, commandGateCtx } from "../lib/home.js";
import { classifyMissionWaypoints, missionCounts } from "../lib/mission.js";
import { commandVerification, commandSource, commandStages } from "../lib/command.js";
import { canonicalVehicleId, getSelectedVehicleId, setSelectedVehicleId, subscribeSelection }
  from "../lib/selection.js";
import { esc, escAttr } from "../lib/format.js";
import * as vt from "../lib/vehicle-telemetry.js";

const MXCOLS = [["battery", "Battery"], ["sensors", "Sensors"], ["gps", "GPS"], ["compass", "Compass"], ["storage", "Storage"], ["cpu", "CPU"], ["network", "Network"]];
const SEV_ORDER = { ok: 0, caution: 1, warn: 2 };
const SEV_LABEL = { ok: "Nominal", warn: "Warning", caution: "Caution" };
function sevLabel(sev) { return SEV_LABEL[sev] || "No signal"; }
function sevClass(sev) { return sev || "idle"; }
function worstSev(...sevs) {
  let w = null;
  sevs.forEach((sv) => { if (sv && sv in SEV_ORDER) { if (w == null || SEV_ORDER[sv] > SEV_ORDER[w]) w = sv; } });
  return w;
}

// Command & Control: the safe command set for the reverse path. High-risk commands
// (ARM/DISARM touch the motors; AUTO/RTL change what the vehicle does on its own) get
// an extra operator confirmation and are sent with confirm:true. Labels are the
// operator's shorthand; the value is the backend command type. Buttons are enabled
// only when the latest Scout-confirmed control authority is OPERATOR — see the
// Control card below; there is no separate/independent authority store. Under
// LOCAL_AGENT the station is read-only for vehicle writes (strict ownership: no
// SET_HOME/LOITER exemption), and under RC the physical override wins — both leave
// hasControl false, which is the single write-enable predicate (lib/authority.js).
//
// Mode presentation follows the shared taxonomy (lib/home.js): LOITER is the Scout's
// PRIMARY safety hold (active anti-drift) and sits in the primary row beside AUTO /
// MANUAL / RTL; the mission + arming controls follow. HOLD and GUIDED are demoted to a
// collapsed "Advanced modes" group — HOLD is a PASSIVE hold (kept for backend
// compatibility) and must never read as LOITER's equal.
// Mission START / PAUSE / RESUME are deliberately absent, and must not come back. Scout's Local
// Agent owns the mission-execution lifecycle as complete verified transactions, and the station
// exposes exactly ONE lifecycle action path: the Agent page's Mission lifecycle card
// (/agent/mission_execution/*). The legacy queued MISSION_PAUSE / MISSION_RESUME were a second,
// competing pause/resume that records no mission sequence and verifies no continuation. What
// remains below are genuine manual supervisory mode commands — never the implementation of Start.
const PRIMARY_CMDS = [
  ["SET_MODE_AUTO", "AUTO"], ["SET_MODE_MANUAL", "MANUAL"],
  ["SET_MODE_LOITER", "LOITER"], ["RTL", "RTL"],
  ["ARM", "ARM"], ["DISARM", "DISARM"],
];
const ADVANCED_CMDS = [["SET_MODE_HOLD", "HOLD"], ["SET_MODE_GUIDED", "GUIDED"]];
const CMDS = [...PRIMARY_CMDS, ...ADVANCED_CMDS];  // combined lookup (labels, routing)
const HIGH_RISK = new Set(["ARM", "DISARM", "RTL", "SET_MODE_AUTO"]);
const CMD_STATUS_CLS = { QUEUED: "u", SENT: "p", ACCEPTED: "p", EXECUTED: "c", REJECTED: "d", FAILED: "d", EXPIRED: "u" };
// Normalized outcome → pill tint (VERIFIED/EXECUTED success green; FAILED/REJECTED/EXPIRED
// red; non-terminal amber/grey). One mapping for the detailed command history.
const OUTCOME_CLS = { VERIFIED: "c", EXECUTED: "c", FAILED: "d", REJECTED: "d", EXPIRED: "u", PENDING: "p", QUEUED: "u", SENT: "p", ACCEPTED: "p" };
const CMD_TERMINAL_V = new Set(["EXECUTED", "REJECTED", "FAILED", "EXPIRED"]);
const fmtClock = (iso) => { if (!iso) return "—"; const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false }); };
// escAttr/esc come from lib/format.js (asText-backed): the local one here was a bare
// String() + quote-escape, which turns a structured value into "[object Object]" inside
// a tooltip exactly as it did in the visible rows.

// One detailed command-history row: type + normalized source + verification-aware
// outcome, the lifecycle stages with timestamps, expected-vs-observed state, and the
// structured failure reason. Outcome/verification come from the SHARED, tested
// commandVerification (lib/command.js) so this can never disagree with the Map panel.
function stageLine(cm) {
  return commandStages(cm).map((s) => `${s.stage}${s.ts ? " " + fmtClock(s.ts) : ""}`).join("  →  ");
}
function commandRow(cm) {
  const v = commandVerification(cm);
  const outcome = CMD_TERMINAL_V.has(cm.status) ? v.outcome : cm.status;
  const clsx = OUTCOME_CLS[outcome] || "u";
  const eo = (v.expected != null || v.observed != null)
    ? `<div class="ctl-eo">expected <b>${v.expected ?? "—"}</b> · observed <b>${v.observed ?? "—"}</b></div>` : "";
  const reason = v.reason || cm.warning || "";
  const note = reason ? `<div class="ctl-note" title="${escAttr(reason)}">${reason}</div>` : "";
  return `<div class="ctl-row col">
    <div class="ctl-row-h"><span class="ctl-type mono">${cm.type}</span><span class="src-chip">${commandSource(cm)}</span><span class="pill ${clsx}">${outcome}</span></div>
    <div class="ctl-life">${stageLine(cm)}</div>
    ${eo}${note}
  </div>`;
}
const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';

export function Vehicle(root) {
  // The SHARED canonical selection (lib/selection.js), not a page-local one: this page
  // used to keep its own id, so opening it could land on a different vehicle than the one
  // the operator had selected on the Map. All pages now agree on one canonical selected id.
  let fleet = [], selId = getSelectedVehicleId(), cmds = [];
  const pxm = {};  // per-vehicle Pixhawk mission cache — same contract as Map.js/Mission.js

  // Control authority — a dedicated read (GET /api/control_authority/{id}, a live
  // proxy to Scout's Flask API), fed through the shared authority controller so a
  // hand-off is PENDING until the effective value Scout reports confirms it (then
  // confirmed / rejected / timeout). NOT fleet data, NOT the command queue. Scout is
  // the sole source of truth; the operator backend holds no authority of its own.
  //   Take Control  → request OPERATOR   Release Control → request LOCAL_AGENT
  const authCtl = createAuthorityController(() => renderDetail());
  function loadAuthority(id) {
    if (id == null) return;
    api.getControlAuthority(id).then((a) => {
      if (id === selId) authCtl.setServer(a);
    }).catch(() => {
      if (id === selId) authCtl.setServer({ ok: true, available: true, reachable: false, authority: null });
    });
  }
  function refreshCommands() {
    const id = selId;
    if (id == null) return;
    api.getCommands(id).then((d) => {
      if (id !== selId) return;
      cmds = (d && d.commands) || [];
      renderDetail();
    }).catch(() => {
      if (id !== selId) return;
      cmds = [];
      renderDetail();
    });
  }
  function pxmState(id) { return pxm[id] || (pxm[id] = { mission: null, fetchedAt: 0, loading: false, note: null }); }
  async function fetchPixhawkMission(id) {
    if (id == null) return;
    const s = pxmState(id);
    s.loading = true; if (id === selId) renderDetail();
    try {
      const res = await api.getPixhawkMission(id);
      if (res && res.reachable) { s.mission = res; s.fetchedAt = Date.now(); s.note = res.available === false ? "no-api" : (res.partial ? "partial" : null); }
      else s.note = (res && res.available === false) ? "no-api" : "unreachable";
    } catch (e) { s.note = "error"; }
    finally { s.loading = false; if (id === selId) renderDetail(); }
  }
  function selectVehicle(id) {
    id = canonicalVehicleId(id);
    if (id !== selId) {
      selId = id; setSelectedVehicleId(id); authCtl.reset(); loadAuthority(id);
      cmds = []; refreshCommands();
      // Auto-fetch once per vehicle per page visit (this page's whole purpose is
      // showing vehicle state) — Refresh stays a deliberate action for re-fetches,
      // the same caution Map.js's live Scout proxy already uses.
      if (!pxm[id] || !pxm[id].fetchedAt) fetchPixhawkMission(id);
    }
  }

  root.className = "app dock-main";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("vehicle") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Systems</span></div>
       <div class="veh-list" id="veh-list"></div>
       <div class="dock-foot">
         <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>
         <span>Every diagnostic below is live — no "run check" button or simulated delay. Values tagged <b>Not reported</b> have no telemetry field yet; never invented.</span>
       </div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Vehicle</h1><span class="count mono" id="vcount">—</span></div>
       <div class="vcontent">
         <div class="sect"><span class="lbl">Fleet systems matrix</span><span class="tag">vehicles × subsystems · click a row for detail</span></div>
         <div class="mxwrap" id="mxwrap"></div>
         <div class="sect"><span class="lbl">Vehicle diagnostics</span><span class="tag" id="dettag"></span></div>
         <div class="detail" id="detail"></div>
       </div>
     </div>`;

  // ---- derive subsystem severity + display value from live data (feeds the matrix
  // AND the section severities below — one computation, never two slightly different
  // judgements of the same battery/leak/gps reading) ----
  // Leak sensor → matrix cell. LEAK is a warning, a calibrated "no leak" is OK, and an
  // UNCALIBRATED / unreported sensor has NO severity at all (sev:null renders "—"):
  // it is not evidence of health and must not be counted as a nominal subsystem.
  function leakCell(v) {
    switch (((v && v.leak_sensor) || {}).state) {
      case "LEAK": return { sev: "warn", val: "LEAK" };
      case "NO_LEAK": return { sev: "ok", val: "OK" };
      case "UNCALIBRATED": return { sev: null, val: "UNCAL" };
      case "UNAVAILABLE": return { sev: "caution", val: "N/A" };
      default: return { sev: null };
    }
  }

  function subsys(v) {
    const h = v.health || {}, comm = commState(v);
    const num = (x) => (x == null ? null : x);
    return {
      battery: v.battery == null ? { sev: null } : { sev: v.battery < 20 ? "warn" : v.battery < 40 ? "caution" : "ok", val: v.battery + "%" },
      // Sensors used to read `health.leak_detected` and fall through to a flat "OK" —
      // which claimed a nominal leak sensor from a null. The leak sensor is currently
      // UNCALIBRATED (readable pin, unknown polarity), and an uncalibrated sensor is not
      // an OK: it gets its own dim "UNCAL" cell so the matrix never asserts safety it
      // cannot back up.
      sensors: leakCell(v),
      gps: v.lat != null && v.lng != null ? { sev: "ok", val: "3D" } : { sev: "caution", val: "NO FIX" },
      compass: v.heading != null ? { sev: "ok", val: pad3(v.heading) + "°" } : { sev: null },
      storage: num(h.disk_usage) == null ? { sev: null } : { sev: h.disk_usage > 90 ? "warn" : h.disk_usage > 75 ? "caution" : "ok", val: h.disk_usage + "%" },
      cpu: num(h.cpu_load) == null ? { sev: null } : { sev: h.cpu_load > 85 ? "warn" : h.cpu_load > 65 ? "caution" : "ok", val: h.cpu_load + "%" },
      network: comm === "connected" ? { sev: "ok", val: "CONN" } : comm === "partitioned" ? { sev: "caution", val: "PART" } : comm === "disconnected" ? { sev: "warn", val: "DISC" } : { sev: "idle", val: "UNK" },
    };
  }
  function overallSev(s) {
    let worst = null;
    Object.values(s).forEach((x) => { if (x.sev && x.sev !== "idle") { if (worst == null || SEV_ORDER[x.sev] > SEV_ORDER[worst]) worst = x.sev; } });
    return worst; // null → no signal
  }

  // ---- matrix ----
  function mcell(s) {
    if (!s || s.sev == null) return `<td class="cell"><span class="mcell na"><span class="mdot"></span><span class="mv">—</span></span></td>`;
    return `<td class="cell"><span class="mcell ${s.sev}"><span class="mdot"></span><span class="mv">${s.val}</span></span></td>`;
  }
  function renderMatrix() {
    const head = `<tr><th class="veh">Vehicle</th>${MXCOLS.map(([, l]) => `<th>${l}</th>`).join("")}</tr>`;
    const body = fleet.map((v) => {
      const s = subsys(v);
      return `<tr data-id="${v.id}" class="${v.id === selId ? "sel" : ""}">
        <td class="vcell"><span class="vc-in"><span class="statdot" style="background:var(--${cls(v) === "c" ? "connected" : cls(v) === "p" ? "partitioned" : cls(v) === "d" ? "disconnected" : "unknown"})"></span><b title="${v.name || "USV-" + v.id}">${v.name || "USV-" + v.id}</b>${CommsPill(v)}</span></td>
        ${MXCOLS.map(([k]) => mcell(s[k])).join("")}
      </tr>`;
    }).join("");
    const mx = document.getElementById("mxwrap");
    mx.innerHTML = `<table class="mx"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    mx.querySelectorAll("tbody tr").forEach((tr) => (tr.onclick = () => { selectVehicle(tr.dataset.id); renderMatrix(); renderDetail(); }));
  }

  // ---- detail ----
  const bar = (pct, color) => `<span class="bar" style="width:64px;flex:none"><i style="width:${pct}%;background:${color}"></i></span>`;
  const row = (k, val, extra = "") => `<div class="mrow"><span class="k">${k}</span><span class="val ${extra}">${val}</span></div>`;
  const naRow = (k, reason = "no telem") => `<div class="mrow"><span class="k">${k}</span><span class="val na">${noTelem(reason)}</span></div>`;

  // ---- one diagnostic row from a lib/vehicle-telemetry.js record ----
  // The record already decided WHAT the value is and WHY it might be missing; this only
  // decides how that reads on screen. Colour follows the availability state, so a value
  // that is merely unmeasured never wears a fault colour, and a fault never renders in
  // the same grey as an absent field. Every interpolated value goes through esc()
  // (asText-backed), so a structured value can never reach the DOM as "[object Object]".
  const DIAG_CLS = {
    [vt.ST.LIVE]: "txt-c",
    [vt.ST.LAST_KNOWN]: "txt-p",
    [vt.ST.UNKNOWN]: "txt-p",
    [vt.ST.FAULT]: "txt-d",
  };
  function diagCell(r) {
    if (!r) return noTelem("no telem");
    if (r.value == null) return noTelem(r.label || "not reported");
    const cls = DIAG_CLS[r.state] || "";
    // `detailTooltipOnly` keeps a LONG breakdown (the per-service list) off the status
    // line while still one hover away — inline it would be an object dump in prose form.
    const detail = r.detail && !r.detailTooltipOnly
      ? `<span class="diag-detail">${esc(r.detail)}</span>` : "";
    const title = r.detail ? ` title="${escAttr(r.detail)}"` : "";
    return `<span class="${cls}"${title}>${esc(r.value)}</span>${detail}`;
  }
  const diagRow = (k, r) => row(k, diagCell(r), "keep");
  // Full-width, always-live named section (Vehicle Health / Power / Communication /
  // Local Agent / Sensors / System) — a free-text condition label, not a severity
  // derived one, so a section with no bad signal at all can still say "Nominal".
  function panelCard(title, condLabel, condClass, bodyHtml) {
    return `<div class="sub full"><div class="sub-head ${condClass}"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${condLabel}</span></div>${bodyHtml}</div>`;
  }

  // RC retains hardware-level override regardless of software control authority
  // (SYSTEM_INFORMATION_MODEL.md / commands.md "Control authority") — a stated
  // architecture invariant, not a per-vehicle telemetry field. Rendered as a
  // constant "Always" rather than NO-TELEM so it never misreads as "unknown
  // whether the safety fallback works".
  const rcAlwaysCell = '<span class="txt-c" title="RC transmitter always retains hardware-level override, independent of software control authority">Always</span>';

  // ---- Control actions (authority hand-off + command queue, Scout-confirmed only) ----
  // Take Control → OPERATOR, Release Control → LOCAL_AGENT. The request goes PENDING
  // and is confirmed only when Scout's reported effective authority matches — the
  // controller (not this click) decides confirmed/rejected/timeout.
  async function authorityAction(target, vname) {
    if (target === "OPERATOR" &&
        !window.confirm(`Take control of ${vname}?\n\nThis requests OPERATOR authority so operator commands can execute. It does NOT arm the vehicle or change its mode.`)) return;
    const res = await authCtl.request(target, (a) => api.setControlAuthority(selId, a));
    if (res && !res.ok) window.alert((res.data && (res.data.message || res.data.error)) || "Authority change failed.");
    refreshCommands();
  }

  async function sendCommand(type, vname) {
    const label = (CMDS.find(([t]) => t === type) || [null, type])[1];
    const highRisk = HIGH_RISK.has(type);
    if (highRisk &&
        !window.confirm(`Confirm ${label} for ${vname}?\n\nThe command is queued for the vehicle to execute. It is NOT applied until the local agent reports back.`)) return;
    let res = await api.createCommand({ vehicle_id: selId, type, confirm: highRisk });
    if (!res.ok && res.data && res.data.needs_confirmation) {
      if (!window.confirm(`${res.data.message}\n\nQueue anyway?`)) return;
      res = await api.createCommand({ vehicle_id: selId, type, confirm: true });
    }
    if (!res.ok) window.alert((res.data && res.data.message) || "Command was not accepted.");
    refreshCommands();
  }

  // Pixhawk mission readback → { home, route, cur, counts }, or null when not fetched
  // yet / unavailable. Same classification + progress math as Map.js/Mission.js
  // (lib/mission.js) — this page never computes a second, different waypoint count.
  function missionStatsFor(v) {
    const s = pxm[v.id];
    if (!s || !s.mission) return null;
    const { home, route } = classifyMissionWaypoints(s.mission.waypoints || []);
    const cur = s.mission.current_seq;
    return { home, route, cur, counts: missionCounts(route, cur) };
  }

  function renderDetail() {
    const v = fleet.find((x) => x.id === selId);
    const box = document.getElementById("detail");
    if (!v) { box.innerHTML = `<div class="empty-state" style="padding:8px 0">No vehicle selected</div>`; return; }
    const vname = v.name || "USV-" + v.id;
    document.getElementById("dettag").textContent = `${vname} · live diagnostics`;
    const s = subsys(v), ov = overallSev(s), stale = commState(v) !== "connected";
    // `schema` is the STATUS MESSAGE schema version from the envelope — it is not, and
    // never was, vehicle firmware. It used to be rendered under a "Firmware" label, which
    // told the operator a version number about the wrong thing entirely.
    const h = v.health || {}, t = v.telemetry || {}, md = v.mission_data || {},
      schema = (v.agent && v.agent.schema_version) || "—";

    // faults for the top-of-page glance
    const faults = MXCOLS.map(([k, l]) => ({ k, l, ...s[k] })).filter((x) => x.sev === "caution" || x.sev === "warn");
    const faultsHtml = faults.length
      ? faults.map((f) => `<div class="frow"><span class="fd" style="background:var(--${f.sev})"></span><span class="txt-${f.sev === "warn" ? "d" : "p"}">${f.l} — ${f.val}</span></div>`).join("")
      : `<div class="frow none">No active faults — all reporting subsystems nominal</div>`;

    // Control authority via the controller (pending → confirmed/rejected/timeout).
    // Write-enable + hand-off affordances both come from the one authored policy
    // (lib/authority.js handoffGate) — identical to Map's, never a second derivation.
    const av = authCtl.view();
    const authVal = stale ? null : av.value;
    const { canTake, canRelease, hasControl, busy } = handoffGate(av, { stale });
    const authoritySeg = AuthoritySeg(authVal, { phase: av.phase, pending: av.pending });
    // Operator connected — the vehicle's own canonical claim that its last status POST
    // reached us (communication.operator_connected), NOT control authority and NOT
    // "a browser is open". See lib/vehicle-telemetry.js operatorConnectedRow.
    const operatorConnCell = diagCell(vt.operatorConnectedRow(v));
    const armed = t.armed;
    const armedCell = stale ? '<span class="txt-u">UNKNOWN</span>'
      : armed == null ? noTelem("no telem")
      : armed ? '<span class="txt-d">ARMED</span>' : '<span class="txt-c">DISARMED</span>';
    const modeCell = stale ? '<span class="txt-u">UNKNOWN</span>' : (t.mode || noTelem("no telem"));
    const mav = v.mavlink || {};
    const heartbeatCell = mav.heartbeat_age_s != null
      ? `<span class="txt-${mav.heartbeat_age_s <= 3 ? "c" : mav.heartbeat_age_s <= 10 ? "p" : "d"}">${mav.heartbeat_age_s.toFixed(1)}s ago</span>`
      : noTelem("no telem");
    const rcActiveCell = stale || !av.reachable ? noTelem("no telem")
      : av.value === "RC" ? '<span class="txt-d">ACTIVE</span>' : '<span class="txt-c">Inactive</span>';

    // Operator Backend link — the SAME feed-health api.js tracks for the Ribbon's
    // Operator Link indicator (poll key "fleet", shared across pages), so this row and
    // the Ribbon can never disagree about whether the backend itself is reachable.
    const feedH = api.getFeedHealth("fleet");
    const feedAgeS = feedH && feedH.lastOkAt != null ? (Date.now() - feedH.lastOkAt) / 1000 : null;
    const operatorBackendCell = feedAgeS == null ? noTelem("connecting")
      : feedAgeS <= 4 ? '<span class="txt-c">Live</span>'
      : feedAgeS <= 12 ? `<span class="txt-p">Delayed ${Math.round(feedAgeS)}s</span>`
      : `<span class="txt-d">Unreachable ${Math.round(feedAgeS)}s</span>`;

    // Pixhawk mission readback (Vehicle Health's Home/Waypoint/Loaded rows) — never a
    // second computation from what Map.js/Mission.js already trust (lib/mission.js).
    const ms = missionStatsFor(v);
    const hs = homeStatus(v, {});
    // Command-gate context, built by the SAME shared builder Map uses (lib/home.js), so
    // both pages gate every button identically. The Vehicle page has no Set Home control,
    // so no Set Home request can be pending from here.
    const gateCtx = commandGateCtx(v, {
      hasControl,
      connected: !stale,
      missionLoaded: !!(ms && ms.counts.total > 0),
    });
    const homeCls = hs.state === "verified" ? "ok" : hs.state === "pending" ? "pending" : hs.state === "unknown" ? "dim" : "warn";
    const homeTxt = hs.state === "verified" ? "Verified" : hs.state === "pending" ? "Setting…" : hs.state === "unknown" ? "Unknown" : "Not verified";
    // Home verification NOTE: `hs` is the shared homeStatus (lib/home.js) reading the
    // backend's mirror of agent.home_status.verified. A HOME_POSITION existing on the
    // Pixhawk is NOT verification — Scout currently reports one ~1.6 km away with
    // verified:false — so this chip must follow `verified` alone. The reason Scout wrote
    // is surfaced beside it rather than left in a tooltip nobody hovers.
    const homeReason = hs.state === "verified" ? null : hs.reason;
    // Current waypoint / Mission loaded come from Scout's CONTINUOUS mission report
    // (mission_count / current_waypoint_display), not from the operator's on-demand
    // readback proxy. Both rows previously said "NOT FETCHED" for a vehicle that was
    // reporting "0 / 15" every second — the proxy answers a different question (the full
    // item list, for drawing and hashing the route), which now has its own row below.
    const curWpCell = diagCell(vt.currentWaypointRow(v, ms));
    const missionLoadedCell = diagCell(vt.missionLoadedRow(v));
    const readbackCell = diagCell(vt.missionReadbackRow(v, !!ms));
    const pixhawkCell = mav.heartbeat_age_s != null
      ? (mav.heartbeat_age_s <= 3 ? '<span class="txt-c">Connected</span>' : mav.heartbeat_age_s <= 10 ? '<span class="txt-p">Degraded</span>' : '<span class="txt-d">No heartbeat</span>')
      : mav.connected === true ? '<span class="txt-c">Connected</span>' : mav.connected === false ? '<span class="txt-d">Disconnected</span>' : noTelem("no telem");

    // ---- section 1: Vehicle Health ----
    const healthSev = stale ? null : worstSev(hs.state === "verified" ? "ok" : "caution", s.gps.sev);
    const healthRows = `
        ${row("Pixhawk", pixhawkCell, "keep")}
        ${row("Heartbeat", heartbeatCell, "keep")}
        ${row("GPS", v.lat != null && v.lng != null ? '<span class="txt-c">3D fix</span>' : '<span class="txt-p">No fix</span>', "keep")}
        ${diagRow("EKF", vt.ekfRow(v))}
        ${row("RC override active", rcActiveCell, "keep")}
        ${row("Armed", armedCell, "keep")}
        ${row("Mode", modeCell, "keep")}
        ${row("Mission", md.mission_state || v.status || noTelem("no telem"), "keep")}
        ${row("Home verification",
              `<span class="pxm-chip ${homeCls}">${homeTxt}</span>`
              + (homeReason ? `<span class="diag-detail">${esc(homeReason)}</span>` : ""), "keep")}
        ${row("Current waypoint", curWpCell, "keep")}
        ${row("Mission loaded", missionLoadedCell, "keep")}
        ${row("Route readback", readbackCell, "keep")}`;

    // ---- section 2: Control (authority hand-off + command queue) ----
    const controlCond = busy ? "Requesting…"
      : hasControl ? "Operator engaged"
      : av.value === "LOCAL_AGENT" ? "Local Agent (autonomy)"
      : av.value === "RC" ? "RC override active"
      : "Unknown";
    const controlClass = hasControl ? "ok" : av.value === "LOCAL_AGENT" ? "idle" : busy ? "caution" : "warn";
    const authBadge = hasControl
      ? `<span class="auth-badge on"><i></i>OPERATOR ENGAGED</span>`
      : `<span class="auth-badge"><i></i>${busy ? "REQUESTING…" : av.value === "LOCAL_AGENT" ? "LOCAL AGENT" : av.value === "RC" ? "RC OVERRIDE" : "UNKNOWN"}</span>`;
    const p = av.pending;
    const authNote = busy
      ? `Requesting <b>${p && p.requested === "OPERATOR" ? "OPERATOR" : "LOCAL AGENT"}</b> authority for ${vname} — awaiting confirmation from the vehicle. Commands stay locked until confirmed.`
      : p && p.phase === "rejected"
        ? `Authority request rejected — ${p.reason || "not accepted"}.`
      : p && p.phase === "timeout"
        ? `Authority request timed out — ${p.reason || "no confirmation from the vehicle"}.`
      : hasControl
        ? `Operator commands are <b>enabled</b> for ${vname}. Releasing hands authority back to the Local Agent without changing the vehicle's mode.`
      : av.value === "LOCAL_AGENT"
        ? `The Local Agent holds control (autonomy). Press <b>Take Control</b> to request OPERATOR authority. This does not arm the vehicle or change its mode.`
      : av.value === "RC"
        ? `An RC transmitter override is active — it holds physical control. Take Control requests OPERATOR authority once RC releases.`
      : stale
        ? `Authority is <b>UNKNOWN</b> — telemetry is stale. Commands stay locked until the link is current.`
        : `Control authority is unknown — Scout's control-authority service did not respond. Commands stay locked until authority is confirmed.`;
    // Take Control must stay available whenever the operator does not already hold a
    // confirmed OPERATOR authority — LOCAL_AGENT included. Enablement is handoffGate's.
    const authBtn = hasControl
      ? `<button class="ctl-auth release" data-auth="LOCAL_AGENT"${canRelease ? "" : " disabled"}>Release Control</button>`
      : `<button class="ctl-auth engage" data-auth="OPERATOR"${canTake ? "" : " disabled"}>Take Control</button>`;
    // Button enablement + disabled reasons come from the SHARED policy (commandGate,
    // lib/home.js) on the SHARED context (commandGateCtx) — identical to Map's cmdRow,
    // never a second set of conditions authored here. Authority permits writes; each
    // command still answers to its own prerequisites (AUTO/RTL/RESUME need a verified
    // Home; LOITER/MANUAL never do). A Home-interlock disable explains itself on hover.
    const renderCmd = ([type, label]) => {
      const hr = HIGH_RISK.has(type);
      const safety = isSafetyHold(type);
      const g = commandGate(type, gateCtx);
      const homeLocked = !g.enabled && !!g.reason;   // disabled by the Home interlock, not the authority lock
      const title = g.reason || (safety ? SAFETY_HOLD_TITLE : `${type}${hr ? " · confirmation required" : ""}`);
      return `<button class="ctl-cmd${hr ? " hr" : ""}${safety ? " safety" : ""}${homeLocked ? " home-locked" : ""}" data-cmd="${type}"${g.enabled ? "" : " disabled"} title="${title.replace(/"/g, "&quot;")}">${label}</button>`;
    };
    const primaryBtns = PRIMARY_CMDS.map(renderCmd).join("");
    const advancedBtns = ADVANCED_CMDS.map(renderCmd).join("");
    const queueHtml = cmds.length
      ? cmds.slice(0, 10).map(commandRow).join("")
      : `<div class="ctl-empty">No commands issued for ${vname} yet.</div>`;
    const controlCard = panelCard("Control", controlCond, controlClass,
      `<div class="metrics">
        ${row("Authority", authoritySeg, "keep")}
        ${row("Operator connected", operatorConnCell, "keep")}
        ${row("RC override policy", rcAlwaysCell, "keep")}
      </div>
      <div style="padding:13px;display:flex;flex-direction:column;gap:12px;border-top:1px solid var(--line)">
        <div class="ctl-auth-bar${hasControl ? " engaged" : ""}">
          <div class="ctl-auth-l"><span class="lbl">Engage</span>${authBadge}</div>
          <div class="ctl-auth-note">${authNote}</div>
          ${authBtn}
        </div>
        <div class="ctl-cmds${hasControl ? "" : " locked"}">${primaryBtns}</div>
        ${hasControl ? "" : `<div class="ctl-lock-note">${lockSvg}<span>Commands are locked. Take Control (Scout-confirmed) to enable them.</span></div>`}
        <div class="ctl-advanced-note">These are manual supervisory mode commands. Mission <b>Start</b>, <b>Pause</b> and <b>Resume</b> are Scout-owned transactions and live on the <b>Agent</b> page's Mission lifecycle card — Start there holds position, sets and verifies Home, synchronizes the planning package and starts AUTO as one verified operation.</div>
        <details class="ctl-advanced">
          <summary>Advanced modes</summary>
          <div class="ctl-cmds${hasControl ? "" : " locked"}">${advancedBtns}</div>
          <div class="ctl-advanced-note">HOLD is a <b>passive</b> hold — the USV may drift with wind or current. For an active anti-drift safety hold use <b>LOITER</b> above.</div>
        </details>
        <div class="ctl-queue">
          <div class="ctl-queue-h"><span class="lbl">Command queue &amp; history</span><span class="tag">status is reported by the vehicle — never assumed</span></div>
          ${queueHtml}
        </div>
      </div>`);

    // ---- section 3: Power ----
    // All four readings come from Scout's canonical `power` block (backend falls back to
    // the legacy telemetry.battery_* spellings). Every one of these was a hardcoded
    // NO-TELEM placeholder while Scout reported 23.8 V / 0.2 A / 89 % every second.
    // Remaining % prefers power.battery_remaining_pct because telemetry.battery carries
    // MAVLink's -1 "unknown" sentinel.
    const remaining = vt.batteryRemainingRow(v);
    const powerRows = `
        ${diagRow("Battery voltage", vt.batteryVoltageRow(v))}
        ${diagRow("Battery current", vt.batteryCurrentRow(v))}
        ${diagRow("Power source", vt.powerSourceRow(v))}
        ${row("Remaining %", remaining.value == null ? noTelem(remaining.label || "not reported")
                                                     : BatteryBar(remaining.pct), "keep")}
        ${diagRow("Failsafe status", vt.failsafeRow(v))}`;

    // ---- section 4: Communication ----
    // Two DIFFERENT links live in this card and must never be conflated:
    //   Scout Pi ↔ Operator over 4G/WireGuard — WireGuard, RTT, packet loss, freshness
    //   Pixhawk ↔ Pi over USB                 — MAVLink
    // Telemetry freshness (CONNECTED/PARTITIONED/DISCONNECTED) stays ARRIVAL-AGE derived,
    // which is the thesis's degradation model; the rest are diagnostic inputs to it and
    // never replace it.
    const commRows = `
        ${diagRow("WireGuard", vt.wireguardRow(v))}
        ${row("Operator Backend", operatorBackendCell, "keep")}
        ${row("Local Agent", v.online ? '<span class="txt-c">Online</span>' : '<span class="txt-d">Offline</span>', "keep")}
        ${diagRow("MAVLink", vt.mavlinkRow(v))}
        ${row("Telemetry freshness", `<span class="txt-${cls(v)}">${fmtAge(v.last_seen_age_s)}</span> · ${commState(v).toUpperCase()}`, "keep")}
        ${diagRow("Packet loss", vt.packetLossRow(v))}
        ${diagRow("RTT", vt.rttRow(v))}`;

    // ---- section 5: Local Agent (payload.agent.* — forwarded verbatim, same field
    // precedence the Agent page uses, so the two pages never disagree) ----
    // The previous derivation was `String(a.current_policy)`. Scout's Flask /agent/state
    // exposes current_policy as the STRING "FULL_REPORTING", but the Local Agent's POST
    // sends it as an OBJECT ({communication_policy, mission_policy, autonomy_level,
    // current_behaviour}) — so the operator read the literal text "[object Object]" where
    // a policy belongs. The SAME object is why Current behaviour said "not emitted":
    // current_behaviour is nested INSIDE it, not a sibling. Both are resolved in
    // lib/vehicle-telemetry.js agentRows, which only ever returns strings.
    const ag = vt.agentRows(v);
    const agentLive = [ag.behaviour, ag.decision, ag.policy, ag.reason]
      .some((r) => r.value != null);
    const agentRows = `
        ${diagRow("Current behaviour", ag.behaviour)}
        ${diagRow("Current decision", ag.decision)}
        ${diagRow("Current policy", ag.policy)}
        ${diagRow("Autonomy level", ag.autonomy)}
        ${row("Control authority", authoritySeg, "keep")}
        ${diagRow("Decision reason", ag.reason)}`;

    // ---- section 6: Sensors ----
    // Leak sensor NOTE: this row must never read "No leak" from the current telemetry.
    // Scout reports the pin as readable with signal LOW but polarity `uncalibrated` —
    // nobody has established whether LOW means dry or flooded — so leak_detected is null
    // and the honest state is UNCALIBRATED. The old row read `health.leak_detected`
    // (always null here) and printed a generic NO TELEM for a sensor that is plainly
    // reporting. Camera/sonar likewise name WHY they are unavailable rather than
    // implying the station is blind.
    const sensorRows = `
        ${row("GPS", v.lat != null ? `${(+v.lat).toFixed(5)}, ${(+v.lng).toFixed(5)}` : noTelem("no telem"), "keep")}
        ${diagRow("GPS satellites", vt.gpsSatellitesRow(v))}
        ${row("Compass", v.heading != null ? pad3(v.heading) + "°" : noTelem("no telem"), "keep")}
        ${diagRow("IMU", vt.imuRow(v))}
        ${diagRow("Camera", vt.cameraRow(v))}
        ${diagRow("Sonar / bathymetry", vt.bathymetryRow(v))}
        ${diagRow("Leak sensor", vt.leakSensorRow(v))}`;

    // ---- section 7: System ----
    const sysRows = `
        ${h.cpu_load != null ? row("CPU", `${bar(h.cpu_load, h.cpu_load > 85 ? "var(--disconnected)" : h.cpu_load > 65 ? "var(--partitioned)" : "var(--connected)")}<span class="pcw">${h.cpu_load}%</span>`, "keep") : naRow("CPU")}
        ${h.ram_usage != null ? row("Memory", `${bar(h.ram_usage, h.ram_usage > 90 ? "var(--disconnected)" : h.ram_usage > 75 ? "var(--partitioned)" : "var(--connected)")}<span class="pcw">${h.ram_usage}%</span>`, "keep") : naRow("Memory")}
        ${diagRow("Temperature", vt.temperatureRow(v))}
        ${h.disk_usage != null ? row("Disk usage", `${bar(h.disk_usage, "var(--connected)")}<span class="pcw">${h.disk_usage}%</span>`, "keep") : naRow("Disk usage")}
        ${diagRow("Service status", vt.serviceStatusRow(v))}
        ${row("Status schema", schema === "—" ? noTelem("not reported") : "v" + schema, "keep")}
        ${naRow("Firmware", "not reported by this vehicle")}`;

    const powerSev = s.battery.sev;
    const commSev = worstSev(s.network.sev, feedAgeS != null && feedAgeS > 12 ? "warn" : null);
    const agentSev = !agentLive ? null : v.online ? "ok" : "warn";
    const sensorsSev = s.sensors.sev;
    const sysSev = worstSev(s.cpu.sev, s.storage.sev);

    const banner = stale
      ? `<div class="stale-note" style="margin:0 0 14px"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>Telemetry as of ${fmtAge(v.last_seen_age_s)} ago — not live. Sensors, GPS, compass, storage &amp; CPU are last-known; battery and link state remain current.</div>`
      : "";

    box.innerHTML = `
      <div class="dhead">
        <span class="dname">${vname}</span>
        ${CommsPill(v, { full: true })}
        <span class="ovr ${ov == null ? "ok" : ov}"><span class="hd"></span>${ov == null ? "No signal" : ov === "ok" ? "OK" : ov === "warn" ? "Warning" : "Caution"}</span>
        <span class="sp"></span>
        <span class="contact"><span class="lbl">Last contact</span><span class="big txt-${cls(v)}">${fmtAge(v.last_seen_age_s)}</span></span>
      </div>
      ${banner}
      <div class="faults" style="margin-bottom:14px;border:1px solid var(--line);border-radius:var(--r);background:var(--panel)">${faultsHtml}</div>
      ${panelCard("Vehicle Health", sevLabel(healthSev), sevClass(healthSev), `<div class="metrics${stale ? " stale" : ""}">${healthRows}</div>`)}
      ${controlCard}
      ${panelCard("Power", sevLabel(powerSev), sevClass(powerSev), `<div class="metrics">${powerRows}</div>`)}
      ${panelCard("Communication", sevLabel(commSev), sevClass(commSev), `<div class="metrics">${commRows}</div>`)}
      ${panelCard("Local Agent", sevLabel(agentSev), sevClass(agentSev), `<div class="metrics${stale ? " stale" : ""}">${agentRows}</div>`)}
      ${panelCard("Sensors", sevLabel(sensorsSev), sevClass(sensorsSev), `<div class="metrics${stale ? " stale" : ""}">${sensorRows}</div>`)}
      ${panelCard("System", sevLabel(sysSev), sevClass(sysSev), `<div class="metrics">${sysRows}</div>`)}`;

    const authBtnEl = box.querySelector(".ctl-auth");
    if (authBtnEl) authBtnEl.onclick = () => authorityAction(authBtnEl.dataset.auth, vname);
    box.querySelectorAll(".ctl-cmd").forEach((b) => (b.onclick = () => sendCommand(b.dataset.cmd, vname)));
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const st = commState(v); if (st === "connected") c.c++; else if (st === "partitioned") c.p++; else if (st === "disconnected") c.d++; });
    return c;
  }

  // Operator Link — shared with Map.js/Mission.js via the SAME api.js poll key
  // ("fleet"), so the Ribbon's backend-reachability indicator (and this page's own
  // "Operator Backend" row) read identically no matter which page is open.
  function updateFeedIndicator() {
    const h = api.getFeedHealth("fleet");
    if (!h || (h.lastOkAt == null && h.lastErrAt == null)) { updateRibbon({ feed: { cls: "dim", label: "CONNECTING…" } }); return; }
    if (h.lastOkAt == null) { updateRibbon({ feed: { cls: "bad", label: "BACKEND UNREACHABLE" } }); return; }
    const ageS = (Date.now() - h.lastOkAt) / 1000;
    if (ageS <= 4) updateRibbon({ feed: { cls: "ok", label: "LIVE" } });
    else if (ageS <= 12) updateRibbon({ feed: { cls: "warn", label: `DELAYED ${Math.round(ageS)}s` } });
    else updateRibbon({ feed: { cls: "bad", label: `UNREACHABLE ${Math.round(ageS)}s` } });
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    // FIRST payload only: open on a vehicle that is actually reporting rather than a
    // never-contacted row. Every later poll leaves the selection alone — with several live
    // USVs, re-deriving "the reporting one" each poll would drag the operator between
    // vehicles as they take turns reporting, and a stale selection must stay selected.
    if (selId == null && fleet.length) {
      selectVehicle((fleet.find((x) => x.online) || fleet.find((x) => x.lat != null) || fleet[0]).id);
    }
    document.getElementById("vcount").textContent = `${fleet.length} vehicles`;
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { selectVehicle(el.dataset.id); onFleet(fleet); }));
    renderMatrix(); renderDetail();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, updateFeedIndicator, "fleet");
  // Follow a selection made on another page without re-deriving one here.
  const unsubscribe = subscribeSelection((id) => { if (id !== selId) selectVehicle(id); });
  const authorityId = setInterval(() => loadAuthority(selId), 2000);  // refresh selected vehicle's control authority
  const commandsId = setInterval(() => refreshCommands(), 3000);  // refresh selected vehicle's command queue
  const clockId = setInterval(() => { updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }); updateFeedIndicator(); }, 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
  updateFeedIndicator();

  return function cleanup() { stopFleet(); unsubscribe(); clearInterval(clockId); clearInterval(authorityId); clearInterval(commandsId); authCtl.dispose(); };
}
