// Agent.js — the "Agent" page (route key `autonomy`, kept for back-compat). A digested,
// at-a-glance decision view so the comms-degradation experiment is legible to anyone:
//   Current Situation → Current Decision (+ Reason, Confidence) → Watch Conditions →
//   Current Policy → Recent Transitions, with the detailed Observed State below.
//
// Honesty by construction (see lib/availability.js + the UI honesty principle):
//   • Current Situation is REAL — comm state, operator-reachable, health, mission,
//     authority, all from live telemetry the operator backend has. LIVE / LAST_KNOWN.
//   • The agent's own cognition — decision, reason, confidence, policy flags — is shown
//     VERBATIM from Scout's payload.agent.* (forwarded as `agent_status`). The frontend
//     NEVER writes or paraphrases reasoning; a field Scout does not emit reads
//     "Unavailable". (Scout owns this — see collectors.get_agent_status / SIM.)
//   • Watch Conditions are evaluated from REAL observed signals (battery, heartbeat,
//     GPS, operator link), overlaid with the agent's own self-assessment when it sends
//     one. Recent Transitions come from the operator's recorded event log.
//   • Numeric sentinels (-1, 9999) are treated as "no data", never shown as values.
// Health and comms stay independent: LAST_KNOWN never becomes a fault.
import * as api from "../services/api.js";
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { vehicleRows } from "../components/VehicleDock.js";
import { commState, cls, fmtAge } from "../lib/ui.js";
import { AVAIL, availSlot, availTag } from "../lib/availability.js";
import { createAuthorityController } from "../lib/authority.js";

const clockSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4l3 2"/></svg>';
const warnSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/></svg>';
const gapSvg =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h16M4 12h16M4 17h10"/></svg>';

const LVL_COLOR = { ok: "var(--connected)", caution: "var(--partitioned)", warn: "var(--disconnected)", idle: "var(--dim)" };
const EV_BADGE = {
  comms: ["COMMS", "p"], agent: ["AGENT", "c"], mission: ["MISSION", "c"],
  authority: ["AUTHORITY", "p"], command: ["COMMAND", "u"], vehicle: ["VEHICLE", "u"],
};
// Confidence / watch-state tokens → the shared .pill tint class (c/p/d/u).
const CONF_TINT = { HIGH: "c", MEDIUM: "p", MED: "p", MODERATE: "p", LOW: "d" };
const WATCH_TINT = { OK: "c", WARN: "p", DEGRADED: "p", LOST: "d", FAIL: "d", UNKNOWN: "u" };
const COMMS_FROM_MSG = [
  [/restored|first contact|recovered|connected/i, "CONNECTED"],
  [/partition/i, "PARTITIONED"],
  [/lost|disconnect/i, "DISCONNECTED"],
];

function clean(v) {
  if (v === null || v === undefined) return null;
  const s = String(v).trim();
  if (s === "" || ["unknown", "none", "n/a", "null", "undefined"].includes(s.toLowerCase())) return null;
  return s;
}
function num(v, { max = null } = {}) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return null;
  if (max != null && n >= max) return null;
  return n;
}
const pill = (label, tint) => `<span class="pill ${tint}">${label}</span>`;

export function Agent(root) {
  let fleet = [], selId = null, events = [];

  const authCtl = createAuthorityController(() => renderDetail());
  function loadAuthority(id) {
    if (id == null) return;
    api.getControlAuthority(id).then((a) => {
      if (id === selId) authCtl.setServer(a);
    }).catch(() => {
      if (id === selId) authCtl.setServer({ ok: true, available: true, reachable: false, authority: null });
    });
  }
  function loadEvents() {
    api.getEventLog().then((list) => { events = Array.isArray(list) ? list : []; renderDetail(); }).catch(() => {});
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
         <span>Agent decision view — ${availTag(AVAIL.LIVE)} observed now · ${availTag(AVAIL.LAST_KNOWN)} last contact · ${availTag(AVAIL.GAP)} the agent does not emit it. Decision, reason and confidence are shown verbatim from Scout; the operator station never writes them.</span>
       </div>
     </div>
     <div class="content-main">
       <div class="toolbar"><h1>Agent</h1><span class="count mono" id="acount">—</span></div>
       <div class="vcontent"><div class="detail" id="detail"></div></div>
     </div>`;

  const row = (k, val) => `<div class="mrow"><span class="k">${k}</span><span class="val keep">${val}</span></div>`;
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
    const a = v.agent_status || {};
    const comm = v.communication || {};
    const md = v.mission_data || {};
    const mav = v.mavlink || {};
    const hasPos = v.lat != null && v.lng != null;
    const agentLive = Object.keys(a).length > 0;

    const liveState = connected ? AVAIL.LIVE : AVAIL.LAST_KNOWN;
    const freshSlot = (value) => availSlot(liveState, { value, age: connected ? null : age });

    // ---- real observed signals ----
    const battery = num(v.battery);
    const operatorReachable = typeof comm.operator_reachable === "boolean" ? comm.operator_reachable : null;
    const missionState = clean(md.mission_state);
    const health = healthSummary(v);
    const av = authCtl.view();
    const authVal = stale ? null : av.value;

    // ---- agent-emitted cognition (verbatim; null when Scout omits it) ----
    const behaviour = clean(a.current_behaviour ?? a.behaviour ?? a.behavior);
    const decision = clean(a.current_decision) || (behaviour ? titleCase(behaviour) : null);
    const decisionFromBehaviour = !clean(a.current_decision) && !!behaviour;
    // Reason bullets: the agent's own decision_reasons (verbatim) when present, else
    // operator-observable bullets, flagged so the UI labels them as observations.
    let reasons = decisionReasons(a), reasonsObserved = false;
    if (!reasons) { reasons = observations(v, st, connected, hasContact, hasPos, stale, av); reasonsObserved = true; }
    const confidence = clean(a.decision_confidence);
    const policyFlags = Array.isArray(a.policy_flags) ? a.policy_flags.filter(Boolean) : [];
    const commPolicy = clean(a.current_policy ?? a.communication_policy);
    const autonomyLevel = clean(a.autonomy_level);

    // ================= Current Situation =================
    const authLabel = authVal
      ? `<span class="txt-${authVal === "OPERATOR" ? "p" : authVal === "RC" ? "d" : "c"}">${authVal === "LOCAL_AGENT" ? "LOCAL_AGENT" : authVal}</span>`
      : availSlot(AVAIL.GAP, { label: stale ? "Unknown (stale)" : !av.reachable ? "Unreachable" : "Unknown" });
    const situationCard = card("Current Situation",
      connected ? availTag(AVAIL.LIVE) : hasContact ? availTag(AVAIL.LAST_KNOWN) : availTag(AVAIL.GAP),
      connected ? "ok" : hasContact ? "caution" : "idle",
      `<div class="metrics">
         ${row("Communication", `<span class="txt-${cls(v)}">${st.toUpperCase()}</span>`)}
         ${row("Operator reachable", operatorReachable == null
            ? availSlot(AVAIL.GAP, { label: connected ? "No data received" : "No" })
            : freshSlot(operatorReachable ? "Yes" : "No"))}
         ${row("Vehicle health", `<span class="sd" style="background:${LVL_COLOR[health.level]};display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px"></span>${health.label}`)}
         ${row("Mission", missionState ? freshSlot(missionState) : availSlot(AVAIL.GAP, { label: hasContact ? "No data received" : "No contact" }))}
         ${row("Authority", authLabel)}
       </div>`, false);

    // ================= Current Decision (+ Reason, Confidence) =================
    const decCond = !agentLive ? availTag(AVAIL.GAP)
      : connected ? availTag(AVAIL.LIVE) : availTag(AVAIL.LAST_KNOWN, `LAST KNOWN · ${Math.round(age)}s`);
    const decCls = !agentLive ? "idle" : connected ? "ok" : "caution";
    const confPill = confidence
      ? pill(confidence.toUpperCase(), CONF_TINT[confidence.toUpperCase()] || "u")
      : availSlot(AVAIL.GAP, { label: "Confidence not emitted" });
    const decisionBig = decision
      ? `<span style="font-size:22px;font-weight:600;color:${stale ? "var(--dim)" : "var(--text)"};letter-spacing:.01em">${decision}</span>${decisionFromBehaviour ? `<span class="cond" style="margin-left:10px">from current_behaviour</span>` : ""}${stale ? availTag(AVAIL.LAST_KNOWN, `LAST KNOWN · ${Math.round(age)}s`) : ""}`
      : availSlot(AVAIL.GAP, { label: hasContact ? "Not emitted" : "No contact", dev: 'agent must emit current_decision (e.g. "Continue Search")' });
    const reasonHtml = reasons.length
      ? `<div class="rlist">${reasons.map((r) => `<div class="ritem"><span class="rdot" style="background:${LVL_COLOR[r.level || "ok"]}"></span><span class="rtx">${r.tx}</span>${r.tag ? `<span class="rav">${availTag(r.tag)}</span>` : ""}</div>`).join("")}</div>`
      : `<div style="padding:6px 13px"><div class="no-telem-box">${gapSvg}${hasContact ? "The agent has not sent a decision reason." : "No contact — no reason available."}</div></div>`;
    const decisionCard = `<div class="sub full"><div class="sub-head ${decCls}"><span class="hd"></span><span class="nm">Current Decision</span><span class="cond">${decCond}</span></div>
       <div style="display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 15px 8px;flex-wrap:wrap">
         <div>${decisionBig}</div>
         <div style="display:flex;align-items:center;gap:9px"><span class="k" style="font-family:var(--font-mono);font-size:10px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)">Confidence</span>${confPill}</div>
       </div>
       <div class="reason-head"><span class="lbl">Reason</span>${reasons.length ? `<span class="tag" style="margin-left:8px">${reasonsObserved ? "operator-derived observations" : "from the agent"}</span>` : ""}</div>
       ${reasonHtml}
       ${reasons.length && reasonsObserved ? `<div class="reason-note">${gapSvg}Scout did not send a decision reason; these are observations the operator backend can see now — not the agent's stated rationale.</div>` : ""}
     </div>`;

    // ================= Watch Conditions =================
    const watch = watchStates(v, { connected, stale, hasContact, hasPos, battery, mav, comm: st });
    const watchTriggered = watch.filter((w) => w.state === "LOST" || w.state === "WARN").length;
    const watchCard = card("Watch Conditions", `${watchTriggered} flagged`, watchTriggered ? "caution" : "ok",
      `<div class="metrics">${watch.map((w) => row(w.name,
        pill(w.state, WATCH_TINT[w.state] || "u") + (w.stale ? ` ${availTag(AVAIL.LAST_KNOWN, `LAST KNOWN · ${Math.round(age)}s`)}` : ""))).join("")}</div>`, false);

    // ================= Current Policy =================
    const policyBody = policyFlags.length
      ? `<div class="rlist">${policyFlags.map((f) => `<div class="ritem"><span class="rdot" style="background:var(--connected)"></span><span class="rtx">${f}</span>${stale ? `<span class="rav">${availTag(AVAIL.LAST_KNOWN)}</span>` : ""}</div>`).join("")}</div>
         <div class="metrics" style="border-top:1px solid var(--line)">
           ${row("Reporting policy", commPolicy ? freshSlot(commPolicy) : availSlot(AVAIL.GAP, { label: "No data received" }))}
           ${row("Autonomy level", autonomyLevel ? freshSlot(autonomyLevel) : availSlot(AVAIL.GAP, { label: "No data received" }))}
         </div>`
      : `<div class="metrics">
           ${row("Reporting policy", commPolicy ? freshSlot(commPolicy) : availSlot(AVAIL.GAP, { label: hasContact ? "No data received" : "No contact" }))}
           ${row("Autonomy level", autonomyLevel ? freshSlot(autonomyLevel) : availSlot(AVAIL.GAP, { label: hasContact ? "No data received" : "No contact" }))}
           ${row("Behaviour", behaviour ? freshSlot(behaviour) : availSlot(AVAIL.GAP, { label: hasContact ? "No data received" : "No contact" }))}
         </div>`;
    const policyCard = card("Current Policy",
      agentLive ? (connected ? availTag(AVAIL.LIVE) : availTag(AVAIL.LAST_KNOWN)) : availTag(AVAIL.GAP),
      agentLive ? (connected ? "ok" : "caution") : "idle", policyBody, false);

    // ================= Recent Transitions (compact chain) =================
    const vEvents = events.filter((e) => e.vehicleId === v.id || e.vehicleId == null);
    const transitionsCard = card("Recent Transitions", "", "idle", transitionChain(vEvents, st, decision), false);

    // ================= Observed State (detailed inputs) + full timeline =================
    const inputsCard = observedStateCard(v, { st, connected, stale, hasContact, age, t, comm, md, mav, battery, missionState, authVal, av, freshSlot });
    const timelineCard = recentTimelineCard(vEvents);

    const banner = !hasContact
      ? `<div class="stale-note" style="margin:0 0 14px;color:var(--muted)">${warnSvg}No contact established with this vehicle — no live reasoning is available.</div>`
      : stale
        ? `<div class="stale-note" style="margin:0 0 14px">${warnSvg}Decision, reason and inputs are LAST KNOWN as of ${fmtAge(age)} ago — the agent may have transitioned since last contact.</div>`
        : "";

    box.innerHTML = `
      <div class="sect" style="padding:0 0 8px"><span class="lbl">Agent decision view</span><span class="tag">situation &amp; watch conditions observed by the operator backend · decision, reason &amp; confidence shown verbatim from Scout</span></div>
      ${banner}
      <div class="subgrid two">${situationCard}${decisionCard}</div>
      <div class="subgrid two">${watchCard}${policyCard}</div>
      <div class="subgrid two">${transitionsCard}${inputsCard}</div>
      ${timelineCard}`;
  }

  // Reason bullets: prefer the agent's own decision_reasons (verbatim). If Scout sent
  // only the legacy single string, split it into lines. If it sent nothing, fall back to
  // operator-observable bullets, flagged `observed` so the UI labels them as such.
  function decisionReasons(a) {
    const list = a.decision_reasons ?? a.decision_reason;
    if (Array.isArray(list) && list.length) return list.filter(Boolean).map((tx) => ({ tx: String(tx), level: "ok" }));
    if (typeof list === "string" && clean(list)) {
      return clean(list).split(/(?<=\.)\s+/).filter(Boolean).map((tx) => ({ tx, level: "ok" }));
    }
    return null; // caller renders the "no reason" slot; observed fallback handled inline
  }

  // Operator-observable reason bullets, used ONLY when Scout sends no decision_reasons.
  // Real observations (comms, policy consequence, health, authority) — never the agent's
  // stated rationale (the UI labels them as observations).
  function observations(v, st, connected, hasContact, hasPos, stale, av) {
    const tag = connected ? AVAIL.LIVE : hasContact ? AVAIL.LAST_KNOWN : AVAIL.GAP;
    const out = [];
    out.push(st === "connected" ? { tx: "Communication healthy.", level: "ok", tag }
      : st === "partitioned" ? { tx: "Communication degraded.", level: "caution", tag }
      : st === "disconnected" ? { tx: "Operator unreachable.", level: "warn", tag }
      : { tx: "Communication unknown.", level: "idle", tag: AVAIL.GAP });
    if (st === "partitioned") out.push({ tx: "Reduced reporting policy active.", level: "caution", tag });
    else if (st === "disconnected") out.push({ tx: "Buffering messages — local autonomy only.", level: "warn", tag });
    const b = num(v.battery);
    out.push(b == null ? { tx: "Battery not reported.", level: "idle", tag: AVAIL.GAP }
      : b < 20 ? { tx: `Battery low (${b}%).`, level: "warn", tag }
      : { tx: "Vehicle health nominal.", level: "ok", tag });
    out.push({ tx: (v.health || {}).leak_detected === true ? "Mission paused for safety." : "Mission safety unaffected.", level: (v.health || {}).leak_detected === true ? "warn" : "ok", tag });
    return out;
  }

  function healthSummary(v) {
    if ((v.health || {}).leak_detected === true) return { level: "warn", label: "Leak detected" };
    const b = num(v.battery);
    if (b == null) return { level: "idle", label: "No signal" };
    if (b < 20) return { level: "warn", label: `Battery ${b}%` };
    if (b < 40) return { level: "caution", label: `Battery ${b}%` };
    return { level: "ok", label: "Healthy" };
  }

  // Watch conditions: operator-observed states from real forwarded signals, overlaid
  // with the agent's own self-assessment (agent_status.watch_conditions) where it sends
  // a definite (non-UNKNOWN) verdict. When the link is stale, vehicle-side conditions are
  // last-known (flagged) — only the Operator-link condition is currently observable.
  function watchStates(v, ctx) {
    const { connected, stale, hasContact, hasPos, battery, mav, comm } = ctx;
    const hbAge = num(mav.heartbeat_age_s, { max: 9000 });
    const hbKnown = mav.connected != null || hbAge != null;
    const opEval = {
      Battery: battery == null ? "UNKNOWN" : battery <= 10 ? "LOST" : battery <= 20 ? "WARN" : "OK",
      Heartbeat: mav.connected === false ? "LOST" : hbKnown ? "OK" : "UNKNOWN",
      GPS: !hasContact ? "UNKNOWN" : hasPos ? "OK" : "LOST",
      Operator: !hasContact ? "UNKNOWN" : comm === "connected" ? "OK" : "LOST",
    };
    const scout = {};
    (Array.isArray(v.agent_status && v.agent_status.watch_conditions) ? v.agent_status.watch_conditions : [])
      .forEach((w) => { if (w && w.name) scout[String(w.name)] = String(w.state || "").toUpperCase(); });
    return ["Battery", "Heartbeat", "GPS", "Operator"].map((name) => {
      const agentSt = scout[name];
      let state = (agentSt && agentSt !== "UNKNOWN") ? agentSt : opEval[name];
      if (!state) state = "UNKNOWN";
      // Vehicle-side conditions can't be confirmed live while stale (Operator can).
      const vehicleSide = name !== "Operator";
      return { name, state, stale: stale && vehicleSide && state !== "UNKNOWN" };
    });
  }

  // A compact vertical chain of the recent comms transitions ending in the current
  // decision — the "CONNECTED ↓ PARTITIONED ↓ Continue Search" view from the mockup.
  function transitionChain(vEvents, st, decision) {
    const states = [];
    for (const e of vEvents) {
      if (e.type !== "comms") continue;
      const m = COMMS_FROM_MSG.find(([re]) => re.test(e.message || ""));
      if (m) states.push(m[1]);
    }
    // current operator-side state as the latest node if it differs from the last logged
    const cur = st.toUpperCase();
    if (!states.length || states[states.length - 1] !== cur) states.push(cur);
    const chain = dedupeConsecutive(states).slice(-3);
    const nodes = chain.map((s) => ({ label: s, tint: s === "CONNECTED" ? "c" : s === "PARTITIONED" ? "p" : s === "DISCONNECTED" ? "d" : "u" }));
    const decNode = decision ? { label: decision, tint: "c", decision: true } : null;
    const all = decNode ? [...nodes, decNode] : nodes;
    if (!all.length) return gapBody("No transitions recorded yet.");
    const arrow = `<div style="text-align:center;color:var(--dim);font-size:14px;line-height:1;margin:2px 0">↓</div>`;
    return `<div style="padding:14px 15px">${all.map((n, i) =>
      `${i ? arrow : ""}<div style="text-align:center">${n.decision
        ? `<span style="display:inline-block;padding:5px 14px;border-radius:6px;border:1px solid var(--connected);color:var(--text);font-weight:600;font-family:var(--font-mono);font-size:12px">${n.label}</span>`
        : pill(n.label, n.tint)}</div>`).join("")}</div>`;
  }

  function observedStateCard(v, ctx) {
    const { st, connected, stale, hasContact, age, t, comm, md, mav, battery, missionState, authVal, av, freshSlot } = ctx;
    const hbAge = num(mav.heartbeat_age_s, { max: 9000 });
    const inp = (value, unit = "", opsSensitive = false) => {
      if (value === null || value === undefined || value === "") return availSlot(AVAIL.GAP, { label: "No data received" });
      if (opsSensitive && stale) return `<span class="txt-u">UNKNOWN — stale</span>`;
      return freshSlot(`${value}${unit}`);
    };
    const boolInp = (val, tt, ft) => (val == null ? availSlot(AVAIL.GAP, { label: "No data received" }) : inp(val ? tt : ft));
    const mode = clean(t.mode), armed = typeof t.armed === "boolean" ? t.armed : null;
    const authInp = authVal
      ? `<span class="txt-${authVal === "OPERATOR" ? "p" : authVal === "RC" ? "d" : "c"}">${authVal}</span>`
      : availSlot(AVAIL.GAP, { label: stale ? "Unknown (stale)" : !av.reachable ? "Unreachable" : "Unknown" });
    return card("Observed State · Decision Inputs",
      connected ? availTag(AVAIL.LIVE) : hasContact ? availTag(AVAIL.LAST_KNOWN) : availTag(AVAIL.GAP),
      connected ? "ok" : hasContact ? "caution" : "idle",
      `<div class="metrics">
         ${row("Communication state", `<span class="txt-${cls(v)}">${st.toUpperCase()}</span>`)}
         ${row("Operator reachable", boolInp(typeof comm.operator_reachable === "boolean" ? comm.operator_reachable : null, "Yes", "No"))}
         ${row("Heartbeat age", inp(hbAge, " s"))}
         ${row("MAVLink connected", boolInp(typeof mav.connected === "boolean" ? mav.connected : null, "Yes", "No"))}
         ${row("Battery", inp(battery, "%"))}
         ${row("GPS fix", v.lat != null && v.lng != null ? inp("3D fix (from position)") : inp(clean(t.gps_fix ?? t.gps_fix_type)))}
         ${row("GPS satellites", inp(num(t.gps_satellites ?? t.gps_sats)))}
         ${row("Vehicle mode", inp(mode, "", true))}
         ${row("Armed", armed == null ? availSlot(AVAIL.GAP, { label: "No data received" }) : inp(armed ? "ARMED" : "DISARMED", "", true))}
         ${row("Mission state", inp(missionState))}
         ${row("Current waypoint", inp(clean(md.current_waypoint ?? md.current_waypoint_display)))}
         ${row("Control authority", authInp)}
       </div>`, false);
  }

  function recentTimelineCard(vEvents) {
    if (!vEvents.length)
      return card("Recent Timeline", "—", "idle",
        gapBody("No transitions recorded yet. Comms, agent-decision, mission, authority and command changes will appear here as they happen."));
    const rows = [];
    let last = null;
    for (const e of vEvents.slice().reverse()) {
      const key = `${e.type}|${e.message}`;
      if (key === last) continue;
      last = key;
      const [badge, k] = EV_BADGE[e.type] || [String(e.type || "EVENT").toUpperCase(), "u"];
      rows.push(`<div class="ev">
        <span class="sv" style="background:${LVL_COLOR[sevLevel(e.severity)]}"></span>
        <span class="tm">${fmtTime(e.ts)}</span>
        <span class="tx"><span class="txt-${k}" style="font-family:var(--font-mono);font-size:10px;letter-spacing:.06em">${badge}</span> ${e.message}</span>
      </div>`);
      if (rows.length >= 10) break;
    }
    return card("Recent Timeline", `latest ${rows.length}`, "idle", `<div class="events" style="padding:4px 13px">${rows.join("")}</div>`);
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
  const eventsId = setInterval(loadEvents, 3000);
  loadEvents();
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); clearInterval(authorityId); clearInterval(eventsId); authCtl.dispose(); };
}

function titleCase(s) {
  return String(s).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}
function dedupeConsecutive(arr) {
  return arr.filter((x, i) => i === 0 || x !== arr[i - 1]);
}
function sevLevel(sev) {
  const s = String(sev || "").toLowerCase();
  if (s.startsWith("emerg") || s.startsWith("warn")) return "warn";
  if (s.startsWith("caut")) return "caution";
  if (s.startsWith("info")) return "ok";
  return "idle";
}
function fmtTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? String(ts) : d.toLocaleTimeString([], { hour12: false });
}
