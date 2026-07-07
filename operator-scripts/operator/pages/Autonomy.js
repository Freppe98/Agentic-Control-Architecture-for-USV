// Autonomy.js — the "why": the on-board agent's reasoning for the selected vehicle,
// and the first consumer of the Data Availability States (lib/availability.js).
// Honesty by construction:
//   • Current behavior is approximated from mission_state — LIVE when connected,
//     LAST_KNOWN when comms are partitioned/disconnected (trust decays with age).
//   • Communication assumptions (link state, operator-reachable, buffered packets)
//     are LIVE from the payload.
//   • The agent's reasoning fields — decision confidence, rationale, previous state,
//     active constraints, next transitions, and the decision-trace history — are
//     BACKEND_GAP (api.getAutonomy() is null until the agent emits them). Never faked.
// Health and comms stay independent: LAST_KNOWN never becomes a fault.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, cls, fmtAge } from "../lib/ui.js";
import { AVAIL, availSlot, availTag } from "../lib/availability.js";

const clockSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>';
const warnSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
const gapSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M4 12h16M4 17h10"/></svg>';

export function Autonomy(root) {
  let fleet = [], selId = null;

  root.className = "app dock-main";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("autonomy") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Reasoning</span></div>
       <div class="veh-list" id="veh-list"></div>
       <div class="dock-foot">
         ${clockSvg}
         <span>Availability — ${availTag(AVAIL.LIVE)} fresh · ${availTag(AVAIL.LAST_KNOWN)} stale (comms) · ${availTag(AVAIL.GAP)} not emitted by backend yet. Reasoning trust decays with contact age; comms and health stay independent.</span>
       </div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Autonomy</h1><span class="count mono" id="acount">—</span></div>
       <div class="vcontent"><div class="detail" id="detail"></div></div>
     </div>`;

  const row = (k, val) => `<div class="mrow"><span class="k">${k}</span><span class="val keep">${val}</span></div>`;
  const card = (title, cond, rowsHtml, full = false) =>
    `<div class="sub${full ? " full" : ""}"><div class="sub-head idle"><span class="hd" style="background:var(--accent)"></span><span class="nm">${title}</span><span class="cond">${cond}</span></div><div class="metrics">${rowsHtml}</div></div>`;
  const gapCard = (title, msg, full = true) =>
    `<div class="sub${full ? " full" : ""}"><div class="sub-head idle"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${availTag(AVAIL.GAP)}</span></div><div style="padding:12px 13px"><div class="no-telem-box">${gapSvg}${msg}</div></div></div>`;

  function renderDetail() {
    const v = fleet.find((x) => x.id === selId);
    const box = document.getElementById("detail");
    if (!v) { box.innerHTML = `<div class="empty-state" style="padding:8px 0">No vehicle selected</div>`; return; }
    document.getElementById("acount").textContent = `${v.name || "USV-" + v.id} · agent reasoning`;

    const st = commState(v), connected = st === "connected";
    const stale = st === "partitioned" || st === "disconnected";
    const hasContact = v.last_seen_age_s != null;
    const age = v.last_seen_age_s;
    const comm = v.communication || {};

    // Behavior is approximated from mission_state today (per DATA_DICTIONARY).
    // Template placeholders (UNKNOWN/LOST) and never-contacted vehicles have no real
    // behavior to report — that is a GAP / no-contact, never a fabricated LAST_KNOWN.
    const rawBehavior = v.status || (v.mission_data && v.mission_data.mission_state) || null;
    const behavior = (hasContact && rawBehavior && !["unknown", "lost"].includes(String(rawBehavior).toLowerCase())) ? rawBehavior : null;
    const behaviorSlot = behavior
      ? availSlot(connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN, { value: behavior, age: connected ? null : age })
      : availSlot(AVAIL.GAP, { label: hasContact ? "not reported" : "no contact" });

    // Current-state block (live position in the state machine)
    const astate = `
      <div class="astate">
        <div class="node"><span class="k">Link</span><span class="val">${CommsPill(v, { full: true })}</span></div>
        <span class="arrow">→</span>
        <div class="node"><span class="k">Behavior</span><span class="val">${behaviorSlot}</span></div>
        <span class="sp"></span>
        <div class="node end"><span class="k">Last contact</span><span class="val txt-${cls(v)}">${fmtAge(age)}</span></div>
      </div>`;

    const banner = !hasContact
      ? `<div class="stale-note" style="margin:0 0 14px;color:var(--muted)">${warnSvg}No contact established with this vehicle — no live reasoning is available.</div>`
      : stale
        ? `<div class="stale-note" style="margin:0 0 14px">${warnSvg}Reasoning is LAST KNOWN as of ${fmtAge(age)} ago — the agent may have transitioned since last contact. Trust decays with contact age; comms and health remain independent.</div>`
        : "";

    // Behavior & decision — behavior live/last-known, the rest agent-emitted (gap)
    const behaviorCard = card("Behavior & decision", behavior ? (connected ? availTag(AVAIL.LIVE) : availTag(AVAIL.LAST_KNOWN)) : availTag(AVAIL.GAP),
      row("Current behavior", behaviorSlot) +
      row("Decision confidence", availSlot(AVAIL.GAP, { label: "agent must emit" })) +
      row("Previous behavior", availSlot(AVAIL.GAP, { label: "needs decision log" })) +
      row("Rationale", availSlot(AVAIL.GAP, { label: "agent must emit" })) +
      row("Next evaluation", availSlot(AVAIL.GAP, { label: "agent must emit" })));

    // Communication assumptions — live from payload
    const commCard = card("Communication assumptions", availTag(connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN),
      row("Link state", `<span class="txt-${cls(v)}">${st.toUpperCase()}</span>`) +
      row("Operator reachable", comm.operator_reachable != null
        ? availSlot(connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN, { value: comm.operator_reachable ? "Yes" : "No", age: connected ? null : age })
        : availSlot(AVAIL.GAP, { label: "not reported" })) +
      row("Buffered packets", comm.buffered_packets != null
        ? availSlot(connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN, { value: String(comm.buffered_packets), age: connected ? null : age })
        : availSlot(AVAIL.GAP, { label: "not reported" })) +
      row("Reasoning trust", connected
        ? availSlot(AVAIL.LIVE, { value: "Current" })
        : hasContact ? availSlot(AVAIL.LAST_KNOWN, { value: "Decaying", age }) : availSlot(AVAIL.GAP, { label: "no contact" })));

    const constraints = gapCard("Active constraints",
      "Decision inputs (met / unmet) are not emitted by the agent yet — the backend has no reasoning schema. Shown as a reserved slot, not fabricated.");
    const transitions = gapCard("Next transitions & watch conditions",
      "The agent does not publish candidate transitions or watch conditions yet — BACKEND_GAP until the reasoning schema exists.");

    // Decision trace — current position is live; the history needs a transition log
    const trace = `
      <div class="sub full"><div class="sub-head idle"><span class="hd"></span><span class="nm">Decision trace</span><span class="cond">${availTag(AVAIL.GAP)}</span></div>
        <div class="metrics"><div class="mrow"><span class="k">Current node</span><span class="val keep"><span class="av-slot">${CommsPill(v, { full: true })}<span class="arrow" style="color:var(--dim)">·</span>${behaviorSlot}</span></span></div></div>
        <div style="padding:0 13px 13px"><div class="no-telem-box">${gapSvg}State-machine history (Connected → Searching → Partitioned → Holding → Disconnected → Return Home) needs a comms-state transition log. Only the current node above is live.</div></div>
      </div>`;

    box.innerHTML =
      astate + banner +
      `<div class="subgrid">${behaviorCard}${commCard}</div>` +
      constraints + transitions + trace;
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) selId = fleet[0].id;
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { selId = +el.dataset.id; onFleet(fleet); }));
    renderDetail();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); };
}
