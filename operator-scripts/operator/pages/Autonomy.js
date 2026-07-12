// Autonomy.js — the "Agent" page: WHY the agent is behaving as it is, for the
// selected vehicle. Reorganized around the reasoning process (situation → decision →
// inputs → policy → watch conditions → history) rather than a debug dump.
//
// Honesty by construction (see lib/availability.js + the UI honesty principle):
//   • Current Situation and Decision Inputs are REAL — derived from live telemetry
//     the operator backend actually has (comm state, battery, GPS, mission_state,
//     control authority, vehicle state). LIVE while connected, LAST_KNOWN when stale.
//   • The agent's own outputs — the decision label, its confidence and rationale, its
//     policies, the conditions it is watching, and its decision history — are
//     BACKEND_GAP: the agent does not emit them yet, so they render as honest
//     "Unavailable" slots, never fabricated. What the operator backend can observe
//     (the reason bullets) is shown, clearly framed as observations, NOT as the
//     agent's stated rationale.
// Health and comms stay independent: LAST_KNOWN never becomes a fault.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { CommsPill } from "../components/CommsPill.js";
import { AuthoritySeg } from "../components/AuthoritySeg.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, cls, fmtAge, pad3 } from "../lib/ui.js";
import { AVAIL, availSlot, availTag } from "../lib/availability.js";
import { createAuthorityController } from "../lib/authority.js";

const clockSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>';
const warnSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
const gapSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M4 12h16M4 17h10"/></svg>';

const LVL_COLOR = { ok: "var(--connected)", caution: "var(--partitioned)", warn: "var(--disconnected)", idle: "var(--dim)" };

export function Autonomy(root) {
  let fleet = [], selId = null;

  // Control authority for the selected vehicle — a dedicated read (GET
  // /api/control_authority/{id}, a live proxy to Scout), fed through the shared
  // controller so it is only ever the Scout-confirmed effective value, never guessed.
  const authCtl = createAuthorityController(() => renderDetail());
  function loadAuthority(id) {
    if (id == null) return;
    api.getControlAuthority(id).then((a) => {
      if (id === selId) authCtl.setServer(a);
    }).catch(() => {
      if (id === selId) authCtl.setServer({ ok: true, available: true, reachable: false, authority: null });
    });
  }

  root.className = "app dock-main";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail("autonomy") +
    `<div class="dock">
       <div class="dock-h"><span class="lbl">Vehicles</span><span class="lbl">Reasoning</span></div>
       <div class="veh-list" id="veh-list"></div>
       <div class="dock-foot">
         ${clockSvg}
         <span>Agent reasoning — ${availTag(AVAIL.LIVE)} observed now · ${availTag(AVAIL.LAST_KNOWN)} last contact · ${availTag(AVAIL.GAP)} the agent must emit it. Observations decay with contact age; comms and health stay independent.</span>
       </div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Agent</h1><span class="count mono" id="acount">—</span></div>
       <div class="vcontent"><div class="detail" id="detail"></div></div>
     </div>`;

  const row = (k, val) => `<div class="mrow"><span class="k">${k}</span><span class="val keep">${val}</span></div>`;
  // A .sub card with a free-text condition in the head.
  const card = (title, cond, condCls, bodyHtml, full = true) =>
    `<div class="sub${full ? " full" : ""}"><div class="sub-head ${condCls}"><span class="hd"></span><span class="nm">${title}</span><span class="cond">${cond}</span></div>${bodyHtml}</div>`;
  const gapBody = (msg) => `<div style="padding:12px 13px"><div class="no-telem-box">${gapSvg}${msg}</div></div>`;

  function renderDetail() {
    const v = fleet.find((x) => x.id === selId);
    const box = document.getElementById("detail");
    if (!v) { box.innerHTML = `<div class="empty-state" style="padding:8px 0">No vehicle selected</div>`; return; }
    document.getElementById("acount").textContent = `${v.name || "USV-" + v.id} · agent reasoning`;

    const st = commState(v), connected = st === "connected";
    const stale = st === "partitioned" || st === "disconnected";
    const hasContact = v.last_seen_age_s != null;
    const age = v.last_seen_age_s;
    const t = v.telemetry || {};
    const hasPos = v.lat != null && v.lng != null;

    // Behavior/mission state — approximated from mission_state (per DATA_DICTIONARY).
    const rawBehavior = v.status || (v.mission_data && v.mission_data.mission_state) || null;
    const behavior = (hasContact && rawBehavior && !["unknown", "lost"].includes(String(rawBehavior).toLowerCase())) ? rawBehavior : null;

    // Control authority (Scout-confirmed effective value, or unknown).
    const av = authCtl.view();
    const authVal = stale ? null : av.value;
    const authSeg = AuthoritySeg(authVal, { phase: av.phase, pending: av.pending });

    // ---- freshness helper: a real value renders LIVE (connected) or LAST_KNOWN (stale)
    const liveState = connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN;
    const freshSlot = (value) => availSlot(liveState, { value, age: connected ? null : age });

    // ---- Current Situation (summary strip) ----
    const commLvl = st === "connected" ? "ok" : st === "partitioned" ? "caution" : st === "disconnected" ? "warn" : "idle";
    const health = healthSummary(v);
    const missionCell = behavior
      ? freshSlot(behavior)
      : availSlot(AVAIL.GAP, { label: hasContact ? "No data" : "No contact" });
    const authCell = authVal
      ? `<span class="txt-${authVal === "OPERATOR" ? "p" : authVal === "RC" ? "d" : "c"}">${authVal === "LOCAL_AGENT" ? "Local Agent" : authVal === "OPERATOR" ? "Operator" : "RC"}</span>`
      : availSlot(AVAIL.GAP, { label: stale ? "Unknown (stale)" : !av.reachable ? "Unreachable" : "Unknown" });
    const situation = `
      <div class="sitgrid">
        ${sitCell("Communication", `<span class="sd" style="background:${LVL_COLOR[commLvl]}"></span>${st.toUpperCase()}`)}
        ${sitCell("Mission", missionCell)}
        ${sitCell("Vehicle health", `<span class="sd" style="background:${LVL_COLOR[health.level]}"></span>${health.label}`)}
        ${sitCell("Authority", authCell)}
        ${sitCell("Decision confidence", availSlot(AVAIL.GAP, { dev: "agent must emit decision_confidence" }))}
      </div>`;

    const banner = !hasContact
      ? `<div class="stale-note" style="margin:0 0 14px;color:var(--muted)">${warnSvg}No contact established with this vehicle — no live reasoning is available.</div>`
      : stale
        ? `<div class="stale-note" style="margin:0 0 14px">${warnSvg}Observations are LAST KNOWN as of ${fmtAge(age)} ago — the agent may have transitioned since last contact.</div>`
        : "";

    // ---- Current Decision ----
    // The decision label + confidence + rationale are the agent's own outputs (gap).
    // The "reason" bullets are observations the operator backend can see — framed as
    // such, NOT presented as the agent's stated reasoning.
    const obs = observations(v, st, behavior, av, connected, hasContact, hasPos, stale);
    const obsHtml = obs.map((o) =>
      `<div class="ritem"><span class="rdot" style="background:${LVL_COLOR[o.level]}"></span><span class="rtx">${o.tx}</span>${o.tag ? `<span class="rav">${availTag(o.tag)}</span>` : ""}</div>`
    ).join("");
    const decisionCard = card("Current Decision", availTag(AVAIL.GAP), "idle",
      `<div class="metrics">
         ${row("Decision", availSlot(AVAIL.GAP, { label: "Unavailable", dev: 'agent must emit current_decision (e.g. "Continue Search")' }))}
         ${row("Decision confidence", availSlot(AVAIL.GAP, { dev: "agent must emit decision_confidence" }))}
       </div>
       <div class="reason-head"><span class="lbl">Reason — operator-derived observations</span></div>
       <div class="rlist">${obsHtml}</div>
       <div class="reason-note">${gapSvg}These are observations the operator backend can see now — not the agent's stated rationale, which the agent must emit.</div>`);

    // ---- Decision Inputs (the observations the agent uses — all REAL) ----
    const batteryCell = v.battery == null
      ? availSlot(AVAIL.GAP, { label: "Not reported" })
      : freshSlot(v.battery + "%");
    const gpsCell = hasPos ? freshSlot("3D fix")
      : availSlot(hasContact ? liveState : AVAIL.GAP, { value: "No fix", label: "No contact" });
    const vehStateCell = stale ? '<span class="txt-u">UNKNOWN — stale</span>'
      : (t.armed == null && t.mode == null) ? availSlot(AVAIL.GAP, { label: "Not reported" })
      : `${t.armed == null ? "" : (t.armed ? '<span class="txt-d">ARMED</span>' : '<span class="txt-c">DISARMED</span>')}${t.mode ? ` · ${t.mode}` : ""}`;
    const inputsCard = card("Decision Inputs", connected ? availTag(AVAIL.LIVE) : hasContact ? availTag(AVAIL.LAST_KNOWN) : availTag(AVAIL.GAP),
      connected ? "ok" : hasContact ? "caution" : "idle",
      `<div class="metrics">
         ${row("Communication", `<span class="txt-${cls(v)}">${st.toUpperCase()}</span>`)}
         ${row("Battery", batteryCell)}
         ${row("GPS", gpsCell)}
         ${row("Mission", missionCell)}
         ${row("Authority", authSeg)}
         ${row("Vehicle state", vehStateCell)}
       </div>`, false);

    // ---- Current Policy (agent-emitted — gap) ----
    const policyCard = card("Current Policy", availTag(AVAIL.GAP), "idle",
      `<div class="metrics">
         ${row("Communication policy", availSlot(AVAIL.GAP, { dev: "agent must emit its comms policy (e.g. store-and-forward, RTL-on-loss)" }))}
         ${row("Autonomy level", availSlot(AVAIL.GAP, { dev: "agent must emit autonomy_level" }))}
         ${row("Mission policy", availSlot(AVAIL.GAP, { dev: "agent must emit its mission policy" }))}
       </div>`, false);

    // ---- Watch Conditions (what the agent is monitoring — agent-emitted, gap) ----
    const watchCandidates = [
      "Battery below RTL threshold", "Heartbeat timeout", "Operator Take Control",
      "Mission completion", "GPS degradation",
    ];
    const watchCard = card("Watch Conditions", availTag(AVAIL.GAP), "idle",
      `<div class="reason-note" style="border:none;padding:11px 13px 4px">${gapSvg}The conditions the agent is actively monitoring — and its thresholds — must be emitted by the agent. Expected conditions:</div>
       <div class="rlist">${watchCandidates.map((c) =>
        `<div class="ritem dim"><span class="rdot" style="background:var(--dim)"></span><span class="rtx">${c}</span><span class="rav">${availTag(AVAIL.GAP)}</span></div>`).join("")}</div>`);

    // ---- Previous Decision + Recent Decisions (need an onboard decision log — gap) ----
    const prevCard = card("Previous Decision", availTag(AVAIL.GAP), "idle",
      gapBody("The immediately previous decision and its reason need an onboard decision log the agent does not emit yet."), false);
    const recentCard = card("Recent Decisions", availTag(AVAIL.GAP), "idle",
      gapBody("A compact decision timeline (~5 entries) will appear here once the agent emits a decision log."), false);

    box.innerHTML = `
      <div class="sect" style="padding:0 0 8px"><span class="lbl">Current Situation</span><span class="tag">observed by the operator backend · the agent's own outputs are marked Unavailable</span></div>
      ${situation}
      ${banner}
      ${decisionCard}
      <div class="subgrid two">${inputsCard}${policyCard}</div>
      ${watchCard}
      <div class="subgrid two">${prevCard}${recentCard}</div>`;
  }

  // Overall vehicle-health summary from the REAL signals (battery + leak). Comms is a
  // separate axis and is not folded in here.
  function healthSummary(v) {
    if ((v.health || {}).leak_detected === true) return { level: "warn", label: "Leak detected" };
    const b = v.battery;
    if (b == null) return { level: "idle", label: "No signal" };
    if (b < 20) return { level: "warn", label: `Battery ${b}%` };
    if (b < 40) return { level: "caution", label: `Battery ${b}%` };
    return { level: "ok", label: "Nominal" };
  }

  // Reason bullets — real observations, each with its own freshness tag.
  function observations(v, st, behavior, av, connected, hasContact, hasPos, stale) {
    const liveTag = connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN;
    const out = [];
    out.push({
      tx: st === "connected" ? "Communication healthy" : st === "partitioned" ? "Communication partitioned"
        : st === "disconnected" ? "Communication lost" : "Communication unknown",
      level: st === "connected" ? "ok" : st === "partitioned" ? "caution" : st === "disconnected" ? "warn" : "idle",
      tag: hasContact ? AVAIL.LIVE : AVAIL.GAP,
    });
    out.push(behavior
      ? { tx: `Mission ${behavior}`, level: /paus|hold|abort/i.test(behavior) ? "caution" : "ok", tag: liveTag }
      : { tx: "Mission state unavailable", level: "idle", tag: AVAIL.GAP });
    const b = v.battery;
    out.push(b == null ? { tx: "Battery not reported", level: "idle", tag: AVAIL.GAP }
      : b < 20 ? { tx: `Battery low (${b}%)`, level: "warn", tag: liveTag }
      : b < 40 ? { tx: `Battery ${b}% — monitor`, level: "caution", tag: liveTag }
      : { tx: `Battery healthy (${b}%)`, level: "ok", tag: liveTag });
    // Effective authority is only trustworthy while the link is current (opsStale) —
    // stale telemetry masks it to UNKNOWN, exactly as the situation strip and the rest
    // of the app do; never assert "operator in control" off a stale link.
    out.push(stale ? { tx: "Authority unknown — link stale", level: "idle", tag: AVAIL.GAP }
      : av.value === "OPERATOR" ? { tx: "Operator has taken control", level: "caution", tag: av.reachable ? AVAIL.LIVE : AVAIL.GAP }
      : av.value === "LOCAL_AGENT" ? { tx: "Operator has not requested intervention", level: "ok", tag: av.reachable ? AVAIL.LIVE : AVAIL.GAP }
      : av.value === "RC" ? { tx: "RC override active", level: "warn", tag: AVAIL.LIVE }
      : { tx: "Authority unknown", level: "idle", tag: AVAIL.GAP });
    out.push(hasPos ? { tx: "GPS fix acquired", level: "ok", tag: liveTag }
      : { tx: "No GPS fix", level: "caution", tag: hasContact ? liveTag : AVAIL.GAP });
    return out;
  }

  function counts() {
    const c = { c: 0, p: 0, d: 0 };
    fleet.forEach((v) => { const s = commState(v); if (s === "connected") c.c++; else if (s === "partitioned") c.p++; else if (s === "disconnected") c.d++; });
    return c;
  }

  function select(id) {
    if (id === selId) return;
    selId = id;
    authCtl.reset();
    loadAuthority(id);
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) {
      selId = (fleet.find((v) => v.online) || fleet.find((v) => v.lat != null) || fleet[0]).id;
      loadAuthority(selId);
    }
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { select(+el.dataset.id); onFleet(fleet); }));
    renderDetail();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const authorityId = setInterval(() => loadAuthority(selId), 2000);
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); clearInterval(authorityId); authCtl.dispose(); };
}

// One cell of the Current Situation strip.
function sitCell(k, val) {
  return `<div class="sitcell"><span class="k">${k}</span><span class="v">${val}</span></div>`;
}
