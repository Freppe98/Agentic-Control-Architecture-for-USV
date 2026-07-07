// Terminal.js — vehicle-local shell ACCESS HELPER for Scout. Deliberately NOT a
// terminal: a real in-browser terminal needs an authenticated web-terminal service
// (ttyd / wetty / code-server / VS Code Remote) running on the vehicle. Scout runs a
// BlueOS stack (dashboard :8080, autopilot/version/wifi managers) but exposes no such
// web-terminal endpoint, and the operator backend must never become an arbitrary
// command-execution channel. So this page does the honest thing: it shows the exact
// SSH command, lets the operator copy it into their own terminal, and links to the
// vehicle dashboard as a fallback. It never fakes a shell and stores no credentials.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { commState, cls, SHORT } from "../lib/ui.js";

// Small local vehicle→SSH-target map (same pattern as Pilot's DASHBOARDS; no
// Configuration API). Keyed by the vehicle id used everywhere else. Only vehicles we
// actually have shell access to belong here — no fabricated hosts.
const SSH_TARGETS = {
  2: { host: "10.0.2.10", user: "motherpi", dashboard: "http://10.0.2.10:8080/" }, // Scout
};

const sshCommand = (t) => `ssh ${t.user}@${t.host}`;

export function Terminal(root) {
  const ids = Object.keys(SSH_TARGETS).map(Number);
  let selId = ids[0] ?? null;
  let fleet = [];

  const nameOf = (id) => {
    const v = fleet.find((x) => x.id === id);
    return (v && v.name) || "USV-" + id;
  };

  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("terminal") +
    `<div class="page">
       <div class="toolbar">
         <h1>Terminal</h1>
         <span class="count">Vehicle-local shell access</span>
       </div>
       <div class="term-body">
         <div class="term-card" id="term-card"></div>
       </div>
     </div>`;

  function linkState(id) {
    const v = fleet.find((x) => x.id === id);
    const st = commState(v || {});
    return { st, klass: cls(v || {}), label: SHORT[st] };
  }

  function render() {
    const t = SSH_TARGETS[selId];
    const card = document.getElementById("term-card");
    if (!t) { card.innerHTML = `<div class="term-empty">No shell access is configured for this vehicle.</div>`; return; }
    const cmd = sshCommand(t);
    const link = linkState(selId);

    card.innerHTML = `
      <div class="term-head">
        <span class="term-icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></svg>
        </span>
        <div class="term-titles">
          <span class="term-veh">${nameOf(selId)}</span>
          <span class="term-sub">Vehicle-local shell · ${t.host}</span>
        </div>
        <span class="term-linkstate" title="Operator telemetry link (not SSH reachability)">
          <span class="statdot" style="background:var(--${{ c: "connected", p: "partitioned", d: "disconnected", u: "unknown" }[link.klass]})"></span>
          <span class="mono">${link.label}</span>
        </span>
      </div>

      <div class="term-fields">
        <div class="term-field"><span class="k">Host</span><span class="v mono">${t.host}</span></div>
        <div class="term-field"><span class="k">User</span><span class="v mono">${t.user}</span></div>
      </div>

      <div class="term-cmd-label"><span class="lbl">SSH command</span></div>
      <div class="term-cmd">
        <code class="term-cmd-text mono" id="term-cmd-text">${cmd}</code>
        <button class="term-copy" id="term-copy" title="Copy to clipboard">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
          <span id="term-copy-lbl">Copy</span>
        </button>
      </div>

      <div class="term-note">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>
        <span>An in-browser terminal is not available: the vehicle exposes no authenticated web-terminal
        service (ttyd / wetty / code-server), and the operator station will not run arbitrary commands
        server-side. Run the command above in your own terminal to open an SSH session with ${nameOf(selId)}.
        The link state shown is the operator telemetry link — SSH is a direct connection this station cannot verify.</span>
      </div>

      <div class="term-actions">
        <button class="pilot-btn" id="term-open-dash">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
          Open Scout dashboard
        </button>
      </div>`;

    // ---- copy (secure-context clipboard API, with a legacy execCommand fallback) ----
    const copyBtn = document.getElementById("term-copy");
    copyBtn.onclick = async () => {
      let ok = false;
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(cmd);
          ok = true;
        }
      } catch (e) { ok = false; }
      if (!ok) {
        const ta = document.createElement("textarea");
        ta.value = cmd; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
        document.body.removeChild(ta);
      }
      const lbl = document.getElementById("term-copy-lbl");
      copyBtn.classList.toggle("done", ok);
      lbl.textContent = ok ? "Copied" : "Press Ctrl+C";
      setTimeout(() => { copyBtn.classList.remove("done"); lbl.textContent = "Copy"; }, 1600);
    };

    document.getElementById("term-open-dash").onclick = () => {
      if (t.dashboard) window.open(t.dashboard, "_blank", "noopener");
    };
  }

  // Poll the fleet like the other pages: keeps the ribbon comms counts live and lets
  // the card show the vehicle's real backend name + current operator-link state.
  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    updateRibbon({ counts: c });
    render();
  }

  render();

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); };
}
