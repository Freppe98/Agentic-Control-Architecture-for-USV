// Experiment.js — thesis experiment control surface. Injects CONTROLLED communication
// impairment (latency / jitter / loss / bandwidth / duplication / reordering, or a full
// disconnect) between the Operator Station and Scout, so degraded and ASYMMETRIC links can
// be reproduced on demand during experiments.
//
// Honest by construction, like Config: the browser never runs `tc`/firewall/PowerShell —
// it sends a structured request to a network-impairment experiment API and renders the
// state that API CONFIRMS. There is no backend implementation in this repo yet, so the
// endpoint is a known gap: the page shows an honest "Unavailable" rather than pretending an
// impairment is live. The impairment is a COMMS-link experiment, not a Pixhawk command —
// there is deliberately no OPERATOR/LOCAL_AGENT authority gate on this form.
//
// Reuses: Ribbon, NavRail, cfg-card/cfg-h/cfg-body/cfg-note/fld/sel/tgl/modal classes.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { commState } from "../lib/ui.js";
import {
  DIRECTIONS, LIMITS, defaultForm, validateExperiment, normalizePayload,
  impairmentFieldsActive, requiresConfirmation, experimentStatus, activeSummary,
  directionLabel, STATUS,
} from "../lib/experiment.js";

const infoIcon =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v0.01M11 12h1v4h1"/></svg>';

const BADGE_CLS = {
  [STATUS.ACTIVE]: "ok", [STATUS.APPLYING]: "pending", [STATUS.STOPPING]: "pending",
  [STATUS.FAILED]: "warn", [STATUS.INACTIVE]: "idle", [STATUS.UNAVAILABLE]: "gap",
};

export function Experiment(root) {
  // ---- page state (never optimistic: `serverState` is the ONLY source of "active") ----
  let form = defaultForm();          // configured values (edited here; not applied)
  let serverState = null;            // last CONFIRMED backend state, or null = unavailable
  let apiReachable = false;          // did the last GET succeed at all?
  let clientPhase = "idle";          // idle | applying | stopping (transient; never "active")
  let requested = null;              // last payload we POSTed (the "requested" values)
  let lastError = null;              // last client-side action error
  let fleet = [];
  let fleetSig = "";                 // roster signature, so we only rebuild the target select on change
  let vehicleId = null;              // target Operator↔Scout link
  let history = [];                  // session-local action log (backend logging is a gap)

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("experiment") +
    `<div class="page">
       <div class="toolbar"><h1>Experiment</h1><span class="count">communication impairment</span></div>
       <div class="cfg" id="exp"></div>
     </div>`;

  // ---------- field builders ----------
  const numField = (field, label, hint, unit) => `
    <div class="fld exp-fld">
      <div class="fld-l"><span class="fld-lbl">${label}</span>${hint ? `<span class="fld-hint">${hint}</span>` : ""}</div>
      <div class="fld-c">
        <div class="exp-input-wrap">
          <input class="exp-input" id="exp-${field}" data-field="${field}" type="number"
                 inputmode="numeric" min="${LIMITS[field]?.min ?? 0}" ${LIMITS[field]?.max != null ? `max="${LIMITS[field].max}"` : ""}
                 value="${form[field] == null ? "" : form[field]}" />
          ${unit ? `<span class="exp-unit">${unit}</span>` : ""}
        </div>
        <span class="exp-err" id="err-${field}"></span>
      </div>
    </div>`;

  // percentage field with a slider + number box that stay in sync
  const pctField = (field, label, hint) => `
    <div class="fld exp-fld">
      <div class="fld-l"><span class="fld-lbl">${label}</span>${hint ? `<span class="fld-hint">${hint}</span>` : ""}</div>
      <div class="fld-c">
        <div class="exp-slider-row">
          <input class="exp-range" id="rng-${field}" data-field="${field}" type="range" min="0" max="100" step="1" value="${Number(form[field]) || 0}" />
          <div class="exp-input-wrap">
            <input class="exp-input sm" id="exp-${field}" data-field="${field}" type="number" inputmode="numeric" min="0" max="100" value="${form[field] == null ? "" : form[field]}" />
            <span class="exp-unit">%</span>
          </div>
        </div>
        <span class="exp-err" id="err-${field}"></span>
      </div>
    </div>`;

  const directionField = () => {
    const opts = DIRECTIONS.map(([v, l]) => `<option value="${v}"${v === form.direction ? " selected" : ""}>${l}</option>`).join("");
    return `
      <div class="fld exp-fld">
        <div class="fld-l"><span class="fld-lbl">Direction</span><span class="fld-hint">Mobile links can be asymmetric — impair one way or both</span></div>
        <div class="fld-c">
          <select class="sel" id="exp-direction" data-field="direction">${opts}</select>
          <span class="exp-err" id="err-direction"></span>
        </div>
      </div>`;
  };

  const targetField = () => {
    const opts = fleet.length
      ? fleet.map((v) => `<option value="${v.id}"${v.id === vehicleId ? " selected" : ""}>${v.name || "USV-" + v.id}</option>`).join("")
      : `<option value="">No vehicles reported</option>`;
    return `
      <div class="fld exp-fld">
        <div class="fld-l"><span class="fld-lbl">Target link</span><span class="fld-hint">Which Operator ↔ Scout link this impairment applies to</span></div>
        <div class="fld-c">
          <select class="sel" id="exp-target"${fleet.length ? "" : " disabled"}>${opts}</select>
        </div>
      </div>`;
  };

  // ---------- sections ----------
  function controlsSection() {
    return `
      <section class="cfg-card">
        <div class="cfg-h">
          <div>
            <h2>Communication impairment controls</h2>
            <p class="cfg-sub">These controls simulate degraded or asymmetric communication between the Operator Station and Scout for controlled experiments. Nothing is applied until you press Apply.</p>
          </div>
          <span class="cfg-tag local">Experiment control</span>
        </div>
        <div class="cfg-body">
          <div class="cfg-form">${targetField()}${directionField()}</div>
          <div class="cfg-form exp-impair" id="exp-impair">
            ${numField("latency_ms", "Latency", `Fixed added delay · ${LIMITS.latency_ms.min}–${LIMITS.latency_ms.max}`, "ms")}
            ${numField("jitter_ms", "Jitter", `Delay variation · ${LIMITS.jitter_ms.min}–${LIMITS.jitter_ms.max}`, "ms")}
            ${pctField("packet_loss_pct", "Packet loss", "Share of packets dropped")}
            ${numField("bandwidth_kbit_s", "Bandwidth limit", "Rate cap — leave blank for unlimited", "kbit/s")}
            ${numField("duration_s", "Duration", `How long to hold · ${LIMITS.duration_s.min}–${LIMITS.duration_s.max}`, "s")}
          </div>

          <details class="ctl-advanced exp-adv">
            <summary>Advanced impairment options</summary>
            <div class="cfg-form exp-impair-adv" id="exp-impair-adv">
              ${pctField("duplication_pct", "Duplication", "Share of packets duplicated (tc netem)")}
              ${pctField("reordering_pct", "Reordering", "Share of packets reordered (tc netem)")}
            </div>
          </details>

          <div class="exp-disconnect" id="exp-disconnect-box">
            <div class="exp-disconnect-l">
              <span class="fld-lbl">Full disconnect</span>
              <span class="fld-hint">Blocks the link entirely with a firewall rule (not tc netem). Latency, jitter, loss and bandwidth become inactive while this is on.</span>
            </div>
            <button class="tgl" id="exp-full_disconnect" role="switch" aria-checked="${form.full_disconnect ? "true" : "false"}"><span class="knob"></span></button>
          </div>

          <div class="cfg-actions exp-actions">
            <button class="cfg-btn primary" id="exp-apply">Apply impairment</button>
            <button class="cfg-btn" id="exp-stop">Stop experiment</button>
            <button class="cfg-btn" id="exp-reset">Reset values</button>
            <span class="exp-form-note" id="exp-form-note"></span>
          </div>

          <div class="cfg-note">
            ${infoIcon}
            The browser never runs <b>tc</b>, firewall or shell commands. Apply sends a
            structured request to a network-impairment experiment API; the page then renders
            only what that API confirms. tc netem carries delay, jitter, loss, rate,
            duplication and reordering; a firewall rule carries Full Disconnect.
          </div>
        </div>
      </section>`;
  }

  function render() {
    document.getElementById("exp").innerHTML =
      controlsSection() +
      `<section class="cfg-card"><div id="exp-status"></div></section>` +
      historySection();
    renderStatus();
    wire();
    refreshValidation();
    applyDisconnectDimming();
  }

  // ---------- status card ----------
  function statusView() {
    const s = experimentStatus(serverState);   // backend-confirmed
    // client transitional phases take visual precedence but are NEVER "active"
    if (clientPhase === "applying" && !s.active) return { key: STATUS.APPLYING, label: "Applying…", active: false };
    if (clientPhase === "stopping") return { key: STATUS.STOPPING, label: "Stopping…", active: false };
    return s;
  }

  function renderStatus() {
    const mount = document.getElementById("exp-status");
    if (!mount) return;
    const view = statusView();
    const st = serverState || {};
    const summary = activeSummary(serverState);
    const row = (k, v) => `<div class="exp-srow"><span class="k">${k}</span><span class="v">${v ?? "—"}</span></div>`;

    const liveBlock = view.active && summary.length
      ? `<div class="exp-live">
           <div class="exp-live-h">ACTIVE</div>
           ${summary.map((l) => `<div class="exp-live-l">${l}</div>`).join("")}
         </div>`
      : "";

    const requestedBlock = (clientPhase === "applying" && requested)
      ? `<div class="exp-requested">
           <span class="lbl">Requested</span>
           <span>${requested.full_disconnect ? "Full disconnect" : impairmentOneLine(requested)} · ${directionLabel(requested.direction)} · ${requested.duration_s}s — awaiting backend confirmation</span>
         </div>`
      : "";

    const unavailable = view.key === STATUS.UNAVAILABLE
      ? `<div class="cfg-note" style="margin-top:12px">
           ${infoIcon}
           The network-impairment experiment API is not reachable, so no experiment state can
           be confirmed. This endpoint (<span class="mono">/api/experiment/network</span>) is a
           known backend gap — see BACKEND_ROADMAP.md. Configured values above are safe to edit;
           Apply will report the API as unavailable rather than silently doing nothing.
         </div>`
      : "";

    mount.innerHTML = `
      <div class="cfg-h">
        <div><h2>Active experiment status</h2><p class="cfg-sub">Backend-confirmed state. The form above is never assumed active — only what the API reports appears here.</p></div>
        <span class="exp-badge ${BADGE_CLS[view.key] || "idle"}"><i></i>${view.label}</span>
      </div>
      <div class="cfg-body">
        ${liveBlock}
        ${requestedBlock}
        <div class="exp-status-grid">
          ${row("Status", view.label)}
          ${row("Started at", fmtTs(st.started_at))}
          ${row("Ends at", fmtTs(st.ends_at))}
          ${row("Time remaining", st.remaining_s != null ? `${Math.round(st.remaining_s)} s` : "—")}
          ${row("Direction", st.direction ? directionLabel(st.direction) : "—")}
          ${row("Current impairment", view.active ? impairmentOneLine(st.profile || {}) : "—")}
          ${row("Last error", lastError || st.error || "—")}
        </div>
        <div class="cfg-note">
          ${infoIcon}
          <span><b>Configured</b> = the values you are editing above. <b>Requested</b> = the last
          profile sent to the API. <b>Confirmed active</b> = what the backend reports is actually
          in effect. The impairment is never marked active on the click — only on backend confirmation.</span>
        </div>
        ${unavailable}
      </div>`;
  }

  function impairmentOneLine(p) {
    if (!p) return "—";
    if (p.full_disconnect) return "Full disconnect";
    const parts = [];
    if (p.latency_ms) parts.push(`${p.latency_ms} ms`);
    if (p.jitter_ms) parts.push(`±${p.jitter_ms} ms`);
    if (p.packet_loss_pct) parts.push(`${p.packet_loss_pct}% loss`);
    if (p.bandwidth_kbit_s) parts.push(`${p.bandwidth_kbit_s} kbit/s`);
    if (p.duplication_pct) parts.push(`${p.duplication_pct}% dup`);
    if (p.reordering_pct) parts.push(`${p.reordering_pct}% reorder`);
    return parts.length ? parts.join(" · ") : "no impairment";
  }

  // ---------- history (session-local placeholder) ----------
  function historySection() {
    const rows = history.length
      ? history.map((h) => `
          <tr>
            <td class="mono">${h.time}</td>
            <td>${h.action}</td>
            <td>${h.direction}</td>
            <td>${h.profile}</td>
            <td class="mono">${h.duration}</td>
            <td class="${h.resultCls}">${h.result}</td>
          </tr>`).join("")
      : `<tr><td colspan="6" class="exp-log-empty">No experiment actions yet this session.</td></tr>`;
    return `
      <section class="cfg-card">
        <div class="cfg-h">
          <div><h2>Experiment history</h2><p class="cfg-sub">Actions taken from this station this session — for thesis traceability and replay.</p></div>
          <span class="cfg-tag ro">Session-local</span>
        </div>
        <div class="cfg-body">
          <div class="cfg-tablewrap">
            <table class="dt exp-log">
              <thead><tr><th>Timestamp</th><th>Action</th><th>Direction</th><th>Profile</th><th>Duration</th><th>Result</th></tr></thead>
              <tbody>${rows}</tbody>
            </table>
          </div>
          <div class="cfg-note">
            ${infoIcon}
            A durable, server-side experiment log (persisted across reloads) is a known
            backend gap. This table records actions from this browser session only.
          </div>
        </div>
      </section>`;
  }

  function logAction(action, payload, result, resultCls = "") {
    history.unshift({
      time: new Date().toLocaleTimeString([], { hour12: false }),
      action,
      direction: payload ? directionLabel(payload.direction) : "—",
      profile: payload ? (payload.full_disconnect ? "Full disconnect" : impairmentOneLine(payload)) : "—",
      duration: payload ? `${payload.duration_s}s` : "—",
      result, resultCls,
    });
    history = history.slice(0, 25);
    const body = document.querySelector(".exp-log tbody");
    if (body) render();   // cheap: whole page re-render keeps the log + status in step
  }

  // ---------- validation / dimming ----------
  function refreshValidation() {
    const { valid, errors } = validateExperiment(form);
    for (const field of ["latency_ms", "jitter_ms", "packet_loss_pct", "bandwidth_kbit_s", "duration_s", "duplication_pct", "reordering_pct", "direction"]) {
      const span = document.getElementById(`err-${field}`);
      if (!span) continue;
      const active = impairmentFieldsActive(form) || ["duration_s", "direction"].includes(field);
      span.textContent = active && errors[field] ? errors[field] : "";
    }
    const apply = document.getElementById("exp-apply");
    if (apply) apply.disabled = !valid || clientPhase !== "idle";
    const stop = document.getElementById("exp-stop");
    if (stop) stop.disabled = clientPhase !== "idle";
    const note = document.getElementById("exp-form-note");
    if (note) {
      note.textContent = !valid ? "Fix the highlighted values before applying."
        : requiresConfirmation(form) ? "Full Disconnect will ask for confirmation."
        : "";
      note.className = "exp-form-note" + (!valid ? " err" : "");
    }
  }

  function applyDisconnectDimming() {
    const fd = form.full_disconnect === true;
    const box = document.getElementById("exp-disconnect-box");
    if (box) box.classList.toggle("armed", fd);
    const tgl = document.getElementById("exp-full_disconnect");
    if (tgl) { tgl.classList.toggle("on", fd); tgl.setAttribute("aria-checked", fd ? "true" : "false"); }
    // dim + disable the netem fields (kept visible, per the spec)
    for (const wrapId of ["exp-impair", "exp-impair-adv"]) {
      const wrap = document.getElementById(wrapId);
      if (!wrap) continue;
      wrap.classList.toggle("dimmed", fd);
      // duration stays active even under full disconnect
      wrap.querySelectorAll("input, .exp-range").forEach((inp) => {
        if (inp.dataset.field === "duration_s") return;
        inp.disabled = fd;
      });
    }
  }

  // ---------- wiring ----------
  function wire() {
    // numeric + slider inputs
    document.querySelectorAll("#exp .exp-input, #exp .exp-range").forEach((inp) => {
      inp.oninput = () => {
        const field = inp.dataset.field;
        const raw = inp.value;
        form[field] = raw === "" ? (field === "bandwidth_kbit_s" ? null : "") : Number(raw);
        // keep slider ↔ number in sync for the pct fields
        const twin = inp.classList.contains("exp-range")
          ? document.getElementById(`exp-${field}`)
          : document.getElementById(`rng-${field}`);
        if (twin && twin.value !== raw) twin.value = raw === "" ? 0 : raw;
        refreshValidation();
      };
    });
    const dir = document.getElementById("exp-direction");
    if (dir) dir.onchange = () => { form.direction = dir.value; refreshValidation(); };
    const target = document.getElementById("exp-target");
    if (target) target.onchange = () => { vehicleId = target.value ? Number(target.value) : null; };
    const fd = document.getElementById("exp-full_disconnect");
    if (fd) fd.onclick = () => { form.full_disconnect = !form.full_disconnect; applyDisconnectDimming(); refreshValidation(); };

    const apply = document.getElementById("exp-apply");
    if (apply) apply.onclick = onApply;
    const stop = document.getElementById("exp-stop");
    if (stop) stop.onclick = onStop;
    const reset = document.getElementById("exp-reset");
    if (reset) reset.onclick = onReset;
  }

  // ---------- actions ----------
  function onApply() {
    const { valid } = validateExperiment(form);
    if (valid !== true || clientPhase !== "idle") return;
    if (requiresConfirmation(form)) {
      confirmFullDisconnect(() => doApply());
    } else {
      doApply();
    }
  }

  async function doApply() {
    const payload = normalizePayload(form, { vehicleId });
    requested = payload;
    lastError = null;
    clientPhase = "applying";
    render();
    const res = await api.applyNetworkExperiment(payload);
    clientPhase = "idle";
    if (res.ok) {
      logAction("Applied impairment", payload, "Requested", "txt-p");
    } else {
      lastError = errText(res, "apply");
      logAction("Failed to apply", payload, lastError, "txt-d");
    }
    refresh();   // pull confirmed state immediately
  }

  async function onStop() {
    if (clientPhase !== "idle") return;
    lastError = null;
    clientPhase = "stopping";
    render();
    const res = await api.stopNetworkExperiment();
    clientPhase = "idle";
    if (res.ok) {
      logAction("Stopped manually", requested, "Stopped", "txt-c");
    } else {
      lastError = errText(res, "stop");
      logAction("Failed to stop", requested, lastError, "txt-d");
    }
    refresh();
  }

  function onReset() {
    // restore safe defaults WITHOUT applying anything
    form = defaultForm();
    render();
    lastError = null;
  }

  function errText(res, what) {
    if (res.status === 404 || res.status === 0 || res.status == null) return `Experiment API unavailable — cannot ${what} (backend gap)`;
    const msg = res.data && (res.data.message || res.data.error || res.data.detail);
    return msg ? String(msg) : `Request failed (${res.status})`;
  }

  // ---------- full-disconnect confirmation modal ----------
  function confirmFullDisconnect(onConfirm) {
    const ov = document.createElement("div");
    ov.className = "modal-ov";
    ov.innerHTML = `
      <div class="modal" role="dialog" aria-modal="true">
        <div class="modal-h">Confirm full disconnect</div>
        <div class="modal-b">
          <p>This will request a <b>full communication block</b> between the Operator Station and
             Scout using a firewall rule — no telemetry or commands will pass for the duration.</p>
          <div class="modal-kv"><span>Direction</span><b>${directionLabel(form.direction)}</b></div>
          <div class="modal-kv"><span>Duration</span><b>${form.duration_s} s</b></div>
          <p class="modal-warn">This is an experiment control, not a vehicle command. Confirm to apply.</p>
        </div>
        <div class="modal-f">
          <button class="modal-btn modal-cancel" id="exp-md-cancel">Cancel</button>
          <button class="modal-btn modal-confirm" id="exp-md-ok">Apply full disconnect</button>
        </div>
      </div>`;
    document.body.appendChild(ov);
    const close = () => ov.remove();
    ov.querySelector("#exp-md-cancel").onclick = close;
    ov.onclick = (e) => { if (e.target === ov) close(); };
    ov.querySelector("#exp-md-ok").onclick = () => { close(); onConfirm(); };
  }

  // ---------- helpers ----------
  function fmtTs(v) {
    if (!v) return "—";
    const d = new Date(v);
    return Number.isNaN(d.getTime()) ? String(v) : d.toLocaleTimeString([], { hour12: false });
  }

  // ---------- data ----------
  function onState(state) {
    serverState = state;
    apiReachable = true;
    renderStatus();
    refreshValidation();
    updateFeed(true);
  }
  function onStateErr() {
    // 404 / unreachable → the experiment API is a backend gap; render honestly, never active.
    serverState = null;
    apiReachable = false;
    renderStatus();
    updateFeed(false);
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (vehicleId == null && fleet.length) vehicleId = fleet[0].id;
    // rebuild the target select only when the roster's id set actually changes
    const sig = fleet.map((v) => v.id).join(",");
    if (sig !== fleetSig) { fleetSig = sig; render(); }
    updateRibbon({ counts: counts() });
  }
  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }
  function updateFeed(ok) {
    updateRibbon({ feed: ok
      ? { cls: "ok", label: "EXPERIMENT API", title: "Network-impairment experiment API reachable" }
      : { cls: "dim", label: "EXPERIMENT API", title: "Network-impairment experiment API not reachable (backend gap)" } });
  }

  function refresh() {
    api.getNetworkExperiment().then(onState).catch(onStateErr);
  }

  // ---------- boot ----------
  render();
  const stopState = api.poll(api.getNetworkExperiment, 2000, onState, onStateErr, "experiment");
  const stopFleet = api.poll(api.getFleet, 3000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopState(); stopFleet(); clearInterval(clockId); };
}
