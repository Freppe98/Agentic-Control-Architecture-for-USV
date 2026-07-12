// Vehicle.js — the Health page. Fleet systems matrix, then the selected vehicle's
// Overall Health, Vehicle State, Control (authority + command queue) and
// Diagnostics, then subsystem cards. Subsystem severity is derived from real
// inputs (battery, leak, disk, cpu, comms, gps/heading); everything not in the
// telemetry schema (temps, voltages, compass cal, rssi, latency, camera,
// MAVLink/Pixhawk-level checks) renders NO-TELEM.
// Reuses VehicleDock, CommsPill, BatteryBar, AuthoritySeg, ui helpers.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { BatteryBar } from "../components/BatteryBar.js";
import { AuthoritySeg } from "../components/AuthoritySeg.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, cls, fmtAge, pad3, noTelem } from "../lib/ui.js";

const MXCOLS = [["battery", "Battery"], ["sensors", "Sensors"], ["gps", "GPS"], ["compass", "Compass"], ["storage", "Storage"], ["cpu", "CPU"], ["network", "Network"]];
const SEV_ORDER = { ok: 0, caution: 1, warn: 2 };

// Diagnostics: a client-side "Run System Check" that reports what the operator
// backend can actually verify today and is honest ("Not available") about the
// rest. Scout does not expose GET /agent/diagnostics or POST /agent/system_check
// yet (checked against this repo — see BACKEND_ROADMAP.md), so there is no
// diagnostics backend to proxy: this is real for the checks backed by a live
// field (comm state, local-agent reporting, GPS fix, battery, storage, CPU,
// memory, operator-reachability, Scout-confirmed authority) and an explicit gap
// slot (never a fake PASS) for the ones that need a MAVLink/Pixhawk/camera/RC/
// mission-service backend that doesn't exist in this repo yet.
const DIAG_CHECKS = [
  ["comm", "Communication"], ["mavlink", "MAVLink"], ["pixhawk", "Pixhawk heartbeat"],
  ["local_agent", "Local Agent"], ["gps", "GPS"], ["battery", "Battery"],
  ["rc", "RC receiver"], ["camera", "Camera"], ["mission", "Mission service"],
  ["storage", "Storage"], ["cpu", "CPU"], ["memory", "Memory"], ["network", "Network"],
  ["authority", "Authority service"],
];
const DIAG_RUN_MS = 550;

// Command & Control: the safe command set for the reverse path. High-risk commands
// (ARM/DISARM touch the motors; AUTO/RTL change what the vehicle does on its own) get
// an extra operator confirmation and are sent with confirm:true. Labels are the
// operator's shorthand; the value is the backend command type. Buttons are enabled
// only when the latest Scout-confirmed control authority is LOCAL_AGENT — see the
// Control card below; there is no separate/independent authority store.
const CMDS = [
  ["SET_MODE_AUTO", "AUTO"], ["SET_MODE_MANUAL", "MANUAL"], ["SET_MODE_HOLD", "HOLD"],
  ["SET_MODE_GUIDED", "GUIDED"], ["RTL", "RTL"], ["MISSION_PAUSE", "PAUSE"],
  ["MISSION_RESUME", "RESUME"], ["ARM", "ARM"], ["DISARM", "DISARM"],
];
const HIGH_RISK = new Set(["ARM", "DISARM", "RTL", "SET_MODE_AUTO"]);
const CMD_STATUS_CLS = { QUEUED: "u", SENT: "p", ACCEPTED: "p", EXECUTED: "c", REJECTED: "d", FAILED: "d", EXPIRED: "u" };
const fmtClock = (iso) => { if (!iso) return "—"; const d = new Date(iso); return Number.isNaN(d.getTime()) ? "—" : d.toLocaleTimeString([], { hour12: false }); };
const lockSvg = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="11" width="16" height="9" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/></svg>';

export function Vehicle(root) {
  let fleet = [], selId = null, cmds = [];
  let authority = null;  // { authority: "OPERATOR"|"LOCAL_AGENT" } for the selected vehicle, or null (unknown)
  let diag = { status: "idle", results: null, ranAt: null, durationMs: null };  // per-selected-vehicle; reset on selection change

  // Control authority — a dedicated read (GET /api/control_authority/{id}, itself a
  // live proxy to Scout's own Flask API), NOT part of the fleet payload and NOT the
  // command queue. Loaded on selection + refreshed on a timer, same pattern as the
  // fleet poll. null (not "OPERATOR") on any fetch failure — an unreachable Scout
  // must read as unknown, not a guess. Scout is the ONLY source of truth for
  // authority; the operator backend holds none of its own.
  function loadAuthority(id) {
    if (id == null) { authority = null; return; }
    api.getControlAuthority(id).then((a) => {
      if (id === selId) { authority = a; renderDetail(); }
    }).catch(() => {
      if (id === selId) { authority = null; renderDetail(); }
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
  function selectVehicle(id) {
    if (id !== selId) {
      selId = id; authority = null; loadAuthority(id);
      diag = { status: "idle", results: null, ranAt: null, durationMs: null };
      cmds = []; refreshCommands();
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
         <span>Health (faults) and Comms (link state) are independent axes. Values tagged <b>no telem</b> are not yet in the telemetry schema.</span>
       </div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Vehicle</h1><span class="count mono" id="vcount">—</span></div>
       <div class="vcontent">
         <div class="sect"><span class="lbl">Fleet systems matrix</span><span class="tag">vehicles × subsystems · click a row for detail</span></div>
         <div class="mxwrap" id="mxwrap"></div>
         <div class="sect"><span class="lbl">Vehicle health</span><span class="tag" id="dettag"></span></div>
         <div class="detail" id="detail"></div>
       </div>
     </div>`;

  // ---- derive subsystem severity + display value from live data ----
  function subsys(v) {
    const h = v.health || {}, comm = commState(v);
    const num = (x) => (x == null ? null : x);
    return {
      battery: v.battery == null ? { sev: null } : { sev: v.battery < 20 ? "warn" : v.battery < 40 ? "caution" : "ok", val: v.battery + "%" },
      sensors: h.leak_detected === true ? { sev: "warn", val: "LEAK" } : { sev: "ok", val: "OK" },
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
        <td class="vcell"><span class="vc-in"><span class="statdot" style="background:var(--${cls(v) === "c" ? "connected" : cls(v) === "p" ? "partitioned" : cls(v) === "d" ? "disconnected" : "unknown"})"></span><b>${v.name || "USV-" + v.id}</b>${CommsPill(v)}</span></td>
        ${MXCOLS.map(([k]) => mcell(s[k])).join("")}
      </tr>`;
    }).join("");
    const mx = document.getElementById("mxwrap");
    mx.innerHTML = `<table class="mx"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    mx.querySelectorAll("tbody tr").forEach((tr) => (tr.onclick = () => { selectVehicle(+tr.dataset.id); renderMatrix(); renderDetail(); }));
  }

  // ---- detail ----
  const bar = (pct, color) => `<span class="bar" style="width:64px;flex:none"><i style="width:${pct}%;background:${color}"></i></span>`;
  const row = (k, val, extra = "") => `<div class="mrow"><span class="k">${k}</span><span class="val ${extra}">${val}</span></div>`;
  const naRow = (k) => `<div class="mrow"><span class="k">${k}</span><span class="val na">${noTelem("no telem")}</span></div>`;
  function subCard(title, sev, rowsHtml, stale) {
    const head = sev == null ? "idle" : sev;
    const cond = sev == null ? "No telemetry" : sev === "ok" ? "Nominal" : sev === "warn" ? "Warning" : sev === "caution" ? "Caution" : "Idle";
    return `<div class="sub"><div class="sub-head ${head}"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${cond}</span></div><div class="metrics${stale ? " stale" : ""}">${rowsHtml}</div></div>`;
  }
  // Like subCard but with a free-text head condition instead of a severity-derived
  // one, and always full-width — used for the status/control cards, which report
  // state (and, for Control, host the command panel), not faults.
  function panelCard(title, condLabel, condClass, bodyHtml) {
    return `<div class="sub full"><div class="sub-head ${condClass}"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${condLabel}</span></div>${bodyHtml}</div>`;
  }

  // RC retains hardware-level override regardless of software control authority
  // (SYSTEM_INFORMATION_MODEL.md / commands.md "Control authority") — a stated
  // architecture invariant, not a per-vehicle telemetry field. Rendered as a
  // constant "Always" rather than NO-TELEM so it never misreads as "unknown
  // whether the safety fallback works".
  const rcAlwaysCell = '<span class="txt-c" title="RC transmitter always retains hardware-level override, independent of software control authority">Always</span>';

  function computeDiagnostics(v, authVal) {
    const cs = commState(v);
    const h = v.health || {};
    const c = v.communication || {};
    const pass = (note) => ({ status: "pass", note });
    const warn = (note) => ({ status: "warn", note });
    const fail = (note) => ({ status: "fail", note });
    const gap = () => ({ status: "gap", note: "No backend support yet" });
    // Same warn/fail breakpoints as the matrix/subsystem cards (subsys() above) — CPU
    // runs hotter-tolerant (85/65) than storage/memory (90/75), kept in sync here.
    const pctCheck = (val, label, hi, mid) => val == null ? gap() : val > hi ? fail(val + "% " + label) : val > mid ? warn(val + "% " + label) : pass(val + "% " + label);
    return {
      comm: cs === "connected" ? pass("Link current") : cs === "partitioned" ? warn("Link partitioned") : fail("Link down"),
      mavlink: gap(),
      pixhawk: gap(),
      local_agent: v.online ? pass("Reporting") : fail("Not reporting"),
      gps: v.lat != null && v.lng != null ? pass("3D fix") : warn("No fix"),
      battery: v.battery == null ? gap() : v.battery < 20 ? fail(v.battery + "% remaining") : v.battery < 40 ? warn(v.battery + "% remaining") : pass(v.battery + "% remaining"),
      rc: gap(),
      camera: gap(),
      mission: gap(),
      storage: pctCheck(h.disk_usage, "used", 90, 75),
      cpu: pctCheck(h.cpu_load, "load", 85, 65),
      memory: pctCheck(h.ram_usage, "used", 90, 75),
      network: c.operator_reachable == null ? gap() : c.operator_reachable ? pass("Operator reachable") : fail("Operator unreachable"),
      authority: authVal === "OPERATOR" || authVal === "LOCAL_AGENT" ? pass(authVal) : fail("Unreachable"),
    };
  }

  // Overall roll-up: worst of the checks that actually returned a signal (pass <
  // warn < fail); "Not available" checks never count toward it, and a run where
  // every check is a gap must never be reported as PASS — that would be inventing
  // a result the backend never gave us.
  function diagOverall(results) {
    if (!results) return null;
    const rank = { pass: 0, warn: 1, fail: 2 };
    let worst = null;
    Object.values(results).forEach((r) => {
      if (!(r.status in rank)) return;
      if (worst == null || rank[r.status] > rank[worst]) worst = r.status;
    });
    return worst;
  }

  function diagRow([id, label]) {
    const r = diag.results && diag.results[id];
    const state = r ? r.status : diag.status === "running" ? "running" : "idle";
    const text = { pass: "PASS", warn: "WARNING", fail: "FAIL", gap: "Not available", running: "Checking…", idle: "Not run" }[state];
    const cls_ = { pass: "ok", warn: "caution", fail: "warn", gap: "idle", running: "idle", idle: "idle" }[state];
    return `<div class="diag-item"><span class="dk">${label}</span><span class="dchip ${cls_}"${r && r.note ? ` title="${r.note}"` : ""}><span class="hd"></span>${text}</span></div>`;
  }

  function runDiagnostics() {
    const v = fleet.find((x) => x.id === selId);
    if (!v) return;
    const startedAt = Date.now();
    diag = { status: "running", results: null, ranAt: diag.ranAt, durationMs: diag.durationMs };
    renderDetail();
    setTimeout(() => {
      if (selId !== v.id) return; // selection moved on while "running"
      diag = {
        status: "done",
        results: computeDiagnostics(v, authority && authority.authority),
        ranAt: new Date().toISOString(),
        durationMs: Date.now() - startedAt,
      };
      renderDetail();
    }, DIAG_RUN_MS);
  }

  // ---- Control actions (authority hand-off + command queue), Scout-confirmed only ----
  async function authorityAction(target, vname) {
    if (target === "LOCAL_AGENT" &&
        !window.confirm(`Take control of ${vname}?\n\nThis grants permission for operator commands to execute. It does NOT arm the vehicle or change its mode.`)) return;
    const res = await api.setControlAuthority(selId, target);
    if (!res.ok) window.alert((res.data && (res.data.message || res.data.error)) || "Authority change failed.");
    loadAuthority(selId);
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

  function renderDetail() {
    const v = fleet.find((x) => x.id === selId);
    const box = document.getElementById("detail");
    if (!v) { box.innerHTML = `<div class="empty-state" style="padding:8px 0">No vehicle selected</div>`; return; }
    const vname = v.name || "USV-" + v.id;
    document.getElementById("dettag").textContent = `${vname} · health, control & diagnostics`;
    const s = subsys(v), ov = overallSev(s), stale = commState(v) !== "connected";
    const h = v.health || {}, meas = v.measurements || {}, t = v.telemetry || {}, md = v.mission_data || {}, schema = (v.agent && v.agent.schema_version) || "—";

    // faults for overview
    const faults = MXCOLS.map(([k, l]) => ({ k, l, ...s[k] })).filter((x) => x.sev === "caution" || x.sev === "warn");
    const faultsHtml = faults.length
      ? faults.map((f) => `<div class="frow"><span class="fd" style="background:var(--${f.sev})"></span><span class="txt-${f.sev === "warn" ? "d" : "p"}">${f.l} — ${f.val}</span></div>`).join("")
      : `<div class="frow none">No active faults — all reporting subsystems nominal</div>`;

    // Control authority is fetched separately (loadAuthority), not derived from fleet
    // data — it's a direct, dedicated proxy to Scout's own Flask API, the sole source
    // of truth. hasControl gates both the mission-command buttons below and this
    // page's command panel identically.
    const authVal = authority && authority.authority;
    const hasControl = authVal === "LOCAL_AGENT";
    const authoritySeg = AuthoritySeg(authVal);
    const operatorReachable = v.communication && v.communication.operator_reachable;
    const operatorReachCell = operatorReachable != null ? (operatorReachable ? '<span class="txt-c">Yes</span>' : '<span class="txt-p">No</span>') : noTelem("no telem");
    const armed = t.armed;
    const armedCell = armed == null ? noTelem("no telem") : armed ? '<span class="txt-d">ARMED</span>' : '<span class="txt-c">DISARMED</span>';

    // ---- Overall Health ----
    const healthCard = `<div class="sub full"><div class="sub-head ${ov == null ? "idle" : ov}"><span class="hd"></span><span class="nm">Overall Health</span><span class="cond">${ov == null ? "No signal" : ov === "ok" ? "Nominal" : ov === "warn" ? "Warning" : "Caution"}</span></div>
      <div class="faults">${faultsHtml}</div>
      <div class="metrics" style="border-top:1px solid var(--line)">
        ${row("Firmware", schema === "—" ? noTelem("no telem") : "v" + schema, "keep")}
        ${row("Services", h.flask_status ? "Flask " + h.flask_status : noTelem("no telem"), "keep")}
      </div></div>`;

    // ---- Vehicle State ----
    const vehicleStateCard = panelCard("Vehicle State", v.online ? "Reporting" : "Offline", v.online ? "ok" : "warn",
      `<div class="metrics">
        ${row("Armed", armedCell, "keep")}
        ${row("Mode", t.mode || noTelem("no telem"), "keep")}
        ${row("Authority", authoritySeg, "keep")}
        ${row("RC present", rcAlwaysCell, "keep")}
        ${row("Operator reachable", operatorReachCell, "keep")}
        ${row("Local Agent", v.online ? '<span class="txt-c">Online</span>' : '<span class="txt-d">Offline</span>', "keep")}
        ${row("Last heartbeat", fmtAge(v.last_seen_age_s), "keep")}
      </div>`);

    // ---- Control (authority hand-off + command queue, Scout-confirmed only) ----
    const controlCond = hasControl ? "Local Agent engaged" : authVal === "OPERATOR" ? "RC / manual" : "Unknown";
    const controlClass = hasControl ? "ok" : authVal === "OPERATOR" ? "idle" : "warn";
    const authBadge = hasControl
      ? `<span class="auth-badge on"><i></i>LOCAL AGENT ENGAGED</span>`
      : `<span class="auth-badge"><i></i>${authVal === "OPERATOR" ? "RC / OPERATOR" : "UNKNOWN"}</span>`;
    const authNote = hasControl
      ? `Operator commands are <b>enabled</b> for ${vname}. Releasing returns authority to RC/manual without changing the vehicle's mode.`
      : authVal === "OPERATOR"
        ? `RC holds exclusive authority — command buttons are disabled. Press <b>Take Control</b> to hand off to the Local Agent. This does not arm the vehicle or change its mode.`
        : `Control authority is unknown — Scout's control-authority API did not respond. Commands stay disabled until authority is confirmed.`;
    const authBtn = hasControl
      ? `<button class="ctl-auth release" data-auth="OPERATOR">Release Control</button>`
      : `<button class="ctl-auth engage" data-auth="LOCAL_AGENT">Take Control</button>`;
    const cmdBtns = CMDS.map(([type, label]) => {
      const hr = HIGH_RISK.has(type);
      return `<button class="ctl-cmd${hr ? " hr" : ""}" data-cmd="${type}"${hasControl ? "" : " disabled"} title="${type}${hr ? " · confirmation required" : ""}">${label}</button>`;
    }).join("");
    const queueHtml = cmds.length
      ? cmds.slice(0, 8).map((c) => {
          const clsx = CMD_STATUS_CLS[c.status] || "u";
          const when = c.completed_at || c.claimed_at || c.created_at;
          const note = c.reason || c.warning || "";
          return `<div class="ctl-row"><span class="ctl-type mono">${c.type}</span><span class="pill ${clsx}">${c.status}</span><span class="ctl-when mono">${fmtClock(when)}</span>${note ? `<span class="ctl-note" title="${note.replace(/"/g, "&quot;")}">${note}</span>` : ""}</div>`;
        }).join("")
      : `<div class="ctl-empty">No commands issued for ${vname} yet.</div>`;

    const controlCard = panelCard("Control", controlCond, controlClass,
      `<div class="metrics">
        ${row("Authority", authoritySeg, "keep")}
        ${row("Operator connected", operatorReachCell, "keep")}
        ${row("RC override available", rcAlwaysCell, "keep")}
      </div>
      <div style="padding:13px;display:flex;flex-direction:column;gap:12px;border-top:1px solid var(--line)">
        <div class="ctl-auth-bar${hasControl ? " engaged" : ""}">
          <div class="ctl-auth-l"><span class="lbl">Engage</span>${authBadge}</div>
          <div class="ctl-auth-note">${authNote}</div>
          ${authBtn}
        </div>
        <div class="ctl-cmds${hasControl ? "" : " locked"}">${cmdBtns}</div>
        ${hasControl ? "" : `<div class="ctl-lock-note">${lockSvg}<span>Commands are locked. Take Control (Scout-confirmed) to enable them.</span></div>`}
        <div class="ctl-queue">
          <div class="ctl-queue-h"><span class="lbl">Command queue &amp; history</span><span class="tag">status is reported by the vehicle — never assumed</span></div>
          ${queueHtml}
        </div>
      </div>`);

    // ---- Diagnostics ----
    const diagBtnLabel = diag.status === "running" ? "Checking…" : "Run System Check";
    // Overall result reflects only checks that actually ran (diagOverall) — a run
    // where every check comes back "Not available" reports NOT AVAILABLE, never PASS.
    const diagResultKey = diag.status === "done" ? diagOverall(diag.results) : null;
    const DIAG_RESULT_TEXT = { pass: "PASS", warn: "WARNING", fail: "FAIL" };
    const diagHeadCls = diag.status === "running" ? "idle"
      : diag.status !== "done" ? "idle"
      : diagResultKey === "fail" ? "warn" : diagResultKey === "warn" ? "caution" : diagResultKey === "pass" ? "ok" : "idle";
    const diagCondText = diag.status === "running" ? "Checking…"
      : diag.status !== "done" ? "Not run"
      : (DIAG_RESULT_TEXT[diagResultKey] || "NOT AVAILABLE");
    const diagMeta = diag.status === "done"
      ? ` · Last run ${fmtClock(diag.ranAt)} · ${(diag.durationMs / 1000).toFixed(1)}s`
      : "";
    const diagnosticsCard = `<div class="sub full">
        <div class="sub-head ${diagHeadCls}"><span class="hd"></span><span class="nm">Diagnostics</span><span class="cond">${diagCondText}</span>
          <button class="diag-btn" id="diag-run" ${diag.status === "running" ? "disabled" : ""}>${diagBtnLabel}</button>
        </div>
        <div class="diag-note">Run System Check reports what the operator backend can verify today; checks with no backend support yet are marked "Not available", never faked.${diagMeta}</div>
        <div class="diag-grid">${DIAG_CHECKS.map(diagRow).join("")}</div>
      </div>`;

    // ---- Subsystem cards ----
    const battery = subCard("Battery", s.battery.sev,
      row("Charge", BatteryBar(v.battery), "keep") +
      row("State", v.battery == null ? noTelem("no telem") : (v.battery < 40 ? "Discharging · low" : "Discharging"), "keep") +
      naRow("Pack voltage") + naRow("Current draw") + naRow("Battery temp"), false);

    const gps = subCard("GPS", s.gps.sev,
      row("Fix", v.lat != null && v.lng != null ? "3D" : "No fix") +
      row("Position", v.lat != null ? `${(+v.lat).toFixed(5)}, ${(+v.lng).toFixed(5)}` : noTelem("no telem")) +
      naRow("Satellites") + naRow("HDOP"), stale);

    const compass = subCard("Compass", s.compass.sev,
      row("Heading", v.heading != null ? pad3(v.heading) + "°" : noTelem("no telem")) +
      naRow("Declination") + naRow("Calibration"), stale);

    const cpu = subCard("CPU", s.cpu.sev,
      (h.cpu_load != null ? row("Load", `${bar(h.cpu_load, h.cpu_load > 85 ? "var(--disconnected)" : h.cpu_load > 65 ? "var(--partitioned)" : "var(--connected)")}<span class="pcw">${h.cpu_load}%</span>`) : naRow("Load")) +
      naRow("Memory") + naRow("Uptime") + naRow("CPU temp"), stale);

    const storage = subCard("Storage", s.storage.sev,
      (h.disk_usage != null ? row("Disk usage", `${bar(h.disk_usage, "var(--connected)")}<span class="pcw">${h.disk_usage}%</span>`) : naRow("Disk usage")) +
      (h.disk_usage != null ? row("Free", `${100 - h.disk_usage}%`) : "") + naRow("Log size"), stale);

    const c = v.communication || {};
    const network = subCard("Network", s.network.sev,
      row("Connectivity", `<span class="txt-${cls(v)}">${commState(v).toUpperCase()}</span>`, "keep") +
      row("Operator reachable", operatorReachCell, "keep") +
      row("Buffered packets", c.buffered_packets != null ? String(c.buffered_packets) : noTelem("no telem"), "keep") +
      naRow("RSSI") + naRow("Latency"), false);

    const sensors = subCard("Sensors", s.sensors.sev,
      row("Leak sensor", h.leak_detected === true ? '<span class="txt-d">LEAK DETECTED</span>' : (h.leak_detected === false ? "No leak" : noTelem("no telem")), "keep") +
      row("Water quality", meas.water_quality ? "Streaming" : noTelem("no telem")) +
      row("Bathymetry", meas.bathymetry ? "Logging" : noTelem("no telem")) +
      naRow("Water temp"), stale);

    const camera = subCard("Camera", null,
      naRow("Signal") + naRow("Resolution") + naRow("Recording"), false);

    const pixhawkSev = t.mode == null && armed == null ? null : armed ? "caution" : "ok";
    const pixhawk = subCard("Pixhawk", pixhawkSev,
      row("Armed", armedCell, "keep") +
      row("Mode", t.mode || noTelem("no telem"), "keep") +
      naRow("Firmware version") + naRow("Motor temp"), stale);

    const missionSev = md.mission_active == null ? null : md.mission_active ? "ok" : "idle";
    const mission = subCard("Mission", missionSev,
      row("Active", md.mission_active == null ? noTelem("no telem") : (md.mission_active ? '<span class="txt-c">Yes</span>' : "No")) +
      row("State", md.mission_state || v.status || noTelem("no telem")) +
      row("Waypoint", md.current_waypoint_display || noTelem("no telem")) +
      (v.coverage != null ? row("Coverage", v.coverage + "%") : naRow("Coverage")), stale);

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
      ${healthCard}
      ${vehicleStateCard}
      ${controlCard}
      ${diagnosticsCard}
      <div class="lbl" style="margin:18px 0 10px">Subsystem cards</div>
      <div class="subgrid">${battery}${gps}${compass}${cpu}${storage}${network}${sensors}${camera}${pixhawk}${mission}</div>`;

    const authBtnEl = box.querySelector(".ctl-auth");
    if (authBtnEl) authBtnEl.onclick = () => authorityAction(authBtnEl.dataset.auth, vname);
    box.querySelectorAll(".ctl-cmd").forEach((b) => (b.onclick = () => sendCommand(b.dataset.cmd, vname)));
    const runBtn = document.getElementById("diag-run");
    if (runBtn) runBtn.onclick = runDiagnostics;
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const st = commState(v); if (st === "connected") c.c++; else if (st === "partitioned") c.p++; else if (st === "disconnected") c.d++; });
    return c;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) selectVehicle(fleet[0].id);
    document.getElementById("vcount").textContent = `${fleet.length} vehicles`;
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { selectVehicle(+el.dataset.id); onFleet(fleet); }));
    renderMatrix(); renderDetail();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const authorityId = setInterval(() => loadAuthority(selId), 2000);  // refresh selected vehicle's control authority
  const commandsId = setInterval(() => refreshCommands(), 3000);  // refresh selected vehicle's command queue
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); clearInterval(authorityId); clearInterval(commandsId); };
}
