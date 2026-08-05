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
import { canonicalVehicleId } from "../lib/selection.js";
import * as replan from "../lib/replan.js";
import * as mx from "../lib/mission-execution.js";

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
// Mission-execution outcome → pill tint. `failed` (an HTTP 200 whose body carried Scout's error)
// and `unknown` are deliberately distinct: one is a definite vehicle-level failure, the other is
// an undecided outcome awaiting reconciliation — never the same colour, never the same word.
const MX_TINT = { accepted: "c", failed: "d", rejected: "d", unknown: "p", unavailable: "u", unsupported: "u", pending: "p" };
const escAttr = (s) => String(s).replace(/"/g, "&quot;");
/** A lat/lng pair from Scout rendered as a fixed-precision coordinate, or an honest dash. */
function coord(p) {
  if (!p || typeof p !== "object") return `<span class="txt-u">—</span>`;
  const lat = Number(p.latitude ?? p.lat), lng = Number(p.longitude ?? p.lng ?? p.lon);
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return `<span class="txt-u">—</span>`;
  return `<span class="mono">${lat.toFixed(6)}, ${lng.toFixed(6)}</span>`;
}

export function Agent(root) {
  let fleet = [], selId = null, events = [];

  // Replanning supervisory state, per SELECTED vehicle only (isolation: switching vehicles
  // clears it, so no Scout's replan state can leak onto another's panel). All read-only from
  // Scout except explicit operator writes; `replanMsg` holds the last write's outcome pill.
  let replanStatus = null, replanReadiness = null, replanConfig = null, replanExperiment = null;
  let replanMsg = null, replanBusy = false, replanForVid = null;

  // Mission-execution lifecycle state, per SELECTED vehicle only. `mxStatus` is Scout's canonical
  // status (the ONLY thing the primary button is derived from — never the last click, never the
  // previous label); `mxResult` is the last operation's interpreted outcome; `mxOps` is the
  // backend's write trace. All tagged with `mxForVid` so a fetch that lands after the operator
  // switched vehicles is discarded rather than rendered on the wrong Scout.
  let mxStatus = null, mxOps = [], mxResult = null, mxBusy = false, mxForVid = null;

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

  // Poll Scout's replanning state for the SELECTED vehicle. READS ONLY — a poll never writes,
  // so reconnect/poll can never resend a package/config/injection. Results are tagged with the
  // vehicle they were fetched for and dropped if the selection changed meanwhile (isolation).
  function loadReplan(id) {
    if (id == null) { replanStatus = replanReadiness = replanConfig = replanExperiment = null; return; }
    const forId = id;
    Promise.allSettled([
      api.getReplanStatus(id), api.getReplanReadiness(id),
      api.getReplanConfig(id), api.getReplanExperiment(id),
    ]).then(([st, rd, cf, ex]) => {
      if (forId !== selId) return;                 // selection moved — discard stale fetch
      replanStatus = st.status === "fulfilled" ? st.value : null;
      replanReadiness = rd.status === "fulfilled" ? rd.value : null;
      replanConfig = cf.status === "fulfilled" ? cf.value : null;
      replanExperiment = ex.status === "fulfilled" ? ex.value : null;
      replanForVid = forId;
      renderDetail();
    });
  }

  // Poll Scout's mission-execution lifecycle for the SELECTED vehicle. READS ONLY — a poll never
  // starts, pauses, resumes or rearms anything, so reconnect/poll can never re-issue a lifecycle
  // operation. Tagged with the vehicle it was fetched for and discarded if the selection moved.
  function loadMissionExecution(id) {
    if (id == null) { mxStatus = null; mxOps = []; return; }
    const forId = id;
    Promise.allSettled([
      api.getMissionExecutionStatus(id), api.getMissionExecutionOperations(id),
    ]).then(([st, ops]) => {
      if (forId !== selId) return;                 // selection moved — discard stale fetch
      mxStatus = st.status === "fulfilled" ? st.value : null;
      const list = ops.status === "fulfilled" ? ops.value : null;
      mxOps = list && Array.isArray(list.operations) ? list.operations : [];
      mxForVid = forId;
      renderDetail();
    });
  }

  // ONE explicit lifecycle write, then reconcile by re-reading Scout's status. The button label
  // is NOT changed optimistically: it stays whatever Scout's last authoritative status derives,
  // merely disabled while the write is in flight. A 200 carrying Scout's error is a FAILURE and
  // is shown as one; an unknown (202) is shown as unknown with the backend's reconciliation
  // verdict, and is never retried automatically.
  function mxWrite(label, fn) {
    if (mxBusy || selId == null) return;
    const id = selId;
    mxBusy = true;
    mxResult = { label, view: { outcome: "pending" } };
    renderDetail();
    Promise.resolve(fn(id)).then((r) => {
      if (id !== selId) return;                    // isolation: never show on another vehicle
      mxResult = { label, view: mx.interpretOperation(r), at: new Date().toISOString() };
    }).catch((e) => {
      if (id !== selId) return;
      mxResult = { label, view: { outcome: mx.OUTCOME.UNAVAILABLE, message: String(e) } };
    }).finally(() => { mxBusy = false; loadMissionExecution(id); });
  }

  // One explicit supervisory WRITE, then reconcile with a fresh read. `busy` disables the
  // controls; the outcome pill shows accepted / rejected / unknown (unknown is NOT a failure —
  // the following read reconciles Scout's actual state). Writes only ever fire from here.
  function replanWrite(label, fn) {
    if (replanBusy || selId == null) return;
    const id = selId;
    replanBusy = true; replanMsg = { label, outcome: "pending" }; renderDetail();
    Promise.resolve(fn(id)).then((r) => {
      const data = (r && r.data) || r || {};
      const outcome = data.outcome || (r && r.ok ? "accepted" : "rejected");
      replanMsg = { label, outcome, code: data.scout_error_code, error: data.error };
    }).catch((e) => { replanMsg = { label, outcome: "unavailable", error: String(e) }; })
      .finally(() => { replanBusy = false; loadReplan(id); });
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
    const transitionsCard = card("Recent Transitions", "", "idle",
      latestActionRow(vEvents) + transitionChain(vEvents, st, decision), false);

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
      ${missionExecutionSection(v)}
      ${replanSection(v, { connected, stale })}
      <div class="subgrid two">${transitionsCard}${inputsCard}</div>
      ${timelineCard}`;
    wireMissionExecution();
    wireReplan();
  }

  // ================= Mission execution lifecycle (Scout /agent/mission_execution/*) =========
  // The station's ONE authoritative lifecycle control. Scout owns the whole transaction — the
  // operator never issues a separate LOITER / Set Home / AUTO sequence to implement Start, and
  // never runs a second FSM. Everything below is derived by lib/mission-execution.js from Scout's
  // canonical status; the button follows STATUS, not the last click.
  function missionExecutionSection(v) {
    // Isolation guard: only render lifecycle state actually fetched for THIS vehicle.
    const forThis = mxForVid != null && v && mxForVid === v.id;
    const raw = forThis ? mxStatus : null;
    const S = mx.normalizeStatus(raw);
    const ops = forThis ? mxOps : [];
    const res = forThis ? mxResult : null;

    const head = `<div class="sect" style="padding:14px 0 6px"><span class="lbl">Mission execution (Scout Local Agent)</span>
        <span class="tag">Scout owns the whole transaction — start, pause, resume, replanning handoff, return and final hold. The operator issues no separate LOITER, Set Home or AUTO for Start.</span>
        ${res ? `<span style="margin-left:8px">${rp(res.label + ": " + (res.view.outcome === "pending" ? "sending…" : mx.outcomeLabel(res.view.outcome)), MX_TINT[res.view.outcome] || "u")}</span>` : ""}
      </div>`;

    if (!S.supported) {
      return head + gapBody("Mission lifecycle not supported by this Scout version. No state, readiness, Home verification, continuation or completion is shown, because this Scout reports none.");
    }
    if (!S.present) {
      return head + gapBody("Scout mission-execution status is unavailable — the Local Agent did not answer. Nothing about the lifecycle can be shown; no action is offered.");
    }
    return head +
      `<div class="subgrid two">${mxControlCard(S, res, ops)}${mxHomeCard(S, ops)}</div>
       <div class="subgrid two">${mxSequenceCard(S)}${mxReturnCard(S)}</div>
       ${mxOperationsCard(ops)}`;
  }

  // --- Primary control: header + the single lifecycle action, derived from status only -------
  function mxControlCard(S, res, ops) {
    const act = mx.primaryAction(S);
    const rearm = mx.rearmAvailability(S);
    const lastCode = (ops.slice(-1)[0] || {}).scout_error_code || null;
    const blockers = mx.startBlockers(S, { lastErrorCode: lastCode });
    const complete = mx.isComplete(S);

    // Disabled while a write is in flight — but the LABEL never changes optimistically.
    const btnEnabled = act.enabled && !mxBusy;
    const effectiveDiffers = S.effectiveState && S.effectiveState !== S.state;
    const stateCond = S.replanning.active ? rp("REPLANNING", "p")
      : complete ? rp("COMPLETE", "c")
      : S.transitional || S.activeOperationId ? rp("IN PROGRESS", "p")
      : rp(String(S.state || "—"), S.state === "RUNNING" ? "c" : S.state === "FAILED" || S.state === "SUSPENDED" ? "d" : "u");

    return card("Mission lifecycle", stateCond,
      S.state === "FAILED" || S.state === "SUSPENDED" ? "caution" : S.replanning.active ? "caution" : complete ? "ok" : S.state === "RUNNING" ? "ok" : "idle",
      `<div class="metrics">
         ${row("State", `<b>${val(S.state)}</b> — ${mx.stateLabel(S.state)}${S.unknownState ? " " + rp("UNRECOGNIZED STATE", "p") : ""}`)}
         ${row("Effective state", effectiveDiffers
            ? `<b>${val(S.effectiveState)}</b>${S.replanning.active ? " " + rp("replanning controller owns the vehicle", "p") : ""}`
            : `<span class="txt-u">same as state</span>`)}
         ${row("Vehicle mode", val(S.mode))}
         ${row("Authority", S.authority ? `<span class="txt-${S.authority === "LOCAL_AGENT" ? "c" : "p"}">${S.authority}</span>` : `<span class="txt-u">—</span>`)}
         ${row("Mission id", `<span class="mono">${val(S.missionId)}</span>`)}
         ${row("Active route hash", `<span class="mono">${shortHash(S.activeRouteHash)}</span>`)}
         ${row("Original route hash", `<span class="mono">${shortHash(S.originalRouteHash)}</span>`)}
         ${row("Active operation id", S.activeOperationId ? `<span class="mono">${S.activeOperationId}</span>` : `<span class="txt-u">none</span>`)}
         ${row("Mission execution enabled", S.missionExecutionEnabled === false ? rp("DISABLED", "d") : S.missionExecutionEnabled === true ? rp("ENABLED", "c") : rp("not reported", "u"))}
       </div>
       <div style="padding:10px 13px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
         <button class="btn" id="mx-primary" data-action="${act.action || ""}" ${btnEnabled ? "" : "disabled"} title="${escAttr(act.reason || "")}">${act.label}</button>
         ${rearm.available ? `<button class="btn ghost" id="mx-rearm" ${rearm.enabled && !mxBusy ? "" : "disabled"} title="Resets the Local Agent mission-execution state only — no vehicle command, no mode change, no Pixhawk mission cleared, no mission re-uploaded">Rearm Mission Controller</button>` : ""}
         ${mxBusy ? `<span class="cond">sending — waiting for Scout's authoritative result…</span>` : act.reason ? `<span class="cond">${act.reason}</span>` : ""}
       </div>
       ${res && res.view.outcome !== "pending" ? `<div class="reason-note">${res.view.outcome === "accepted" ? gapSvg : warnSvg}<span><b>${res.label}</b>: ${mx.operationSummary(res.view)}</span></div>` : ""}
       ${!act.enabled && act.action === null && blockers.length ? `<div class="reason-note">${warnSvg}<span>Start unavailable: ${blockers.join(" · ")}</span></div>` : ""}
       ${rearm.available ? `<div class="reason-note">${gapSvg}<span>Rearm resets the Local Agent mission-execution controller. It does <b>not</b> clear the Pixhawk mission, switch vehicle mode or re-upload the original mission — it prepares the controller for another explicitly prepared run.</span></div>` : ""}
       <div class="reason-note">${gapSvg}<span>${mx.START_HOME_NOTE}</span></div>`, false);
  }

  // --- Home: what Start does to it, and what Scout verified ---------------------------------
  function mxHomeCard(S, ops) {
    // The requested launch Home comes from the most recent operation that carried a home_result;
    // it is Scout's report of what it asked for, never an operator-side Home decision.
    const lastHome = ops.slice().reverse().map((o) => o.home_result).find((h) => h && typeof h === "object") || null;
    const req = lastHome && lastHome.requested_position;
    const ver = (lastHome && lastHome.home_position) || S.home.verified;
    const dist = S.home.verificationDistanceM != null ? S.home.verificationDistanceM
      : (lastHome && typeof lastHome.verification_distance_m === "number" ? lastHome.verification_distance_m : null);
    const verified = lastHome ? lastHome.verified : (S.home.verified ? true : null);
    const rp2 = S.returnCompletion;
    return card("Home (set and verified by Scout)",
      verified === true ? rp("VERIFIED", "c") : verified === false ? rp("NOT VERIFIED", "d") : rp("not reported", "u"),
      verified === true ? "ok" : verified === false ? "caution" : "idle",
      `<div class="metrics">
         ${row("Requested launch Home", coord(req))}
         ${row("Verified Home", coord(ver))}
         ${row("Verification distance", dist == null ? `<span class="txt-u">—</span>` : dist.toFixed(2) + " m")}
         ${row("Home verification", verified === true ? rp("VERIFIED BY SCOUT", "c") : verified === false ? rp("NOT VERIFIED", "d") : rp("not reported", "u"))}
         ${row("Package Home synchronized", lastHome && lastHome.package_synchronized != null ? (lastHome.package_synchronized ? rp("SYNCHRONIZED", "c") : rp("NOT SYNCHRONIZED", "d")) : rp("not reported", "u"))}
         ${row("Distance to Home (return)", rp2.distanceToHomeM == null ? `<span class="txt-u">—</span>` : rp2.distanceToHomeM.toFixed(1) + " m")}
         ${row("Home error", lastHome && lastHome.error ? `<span class="txt-d">${lastHome.error}</span>` : `<span class="txt-u">—</span>`)}
       </div>
       <div class="reason-note">${warnSvg}<span><b>Start Mission resets Home to the vehicle's current launch position.</b> Scout sets it, reads it back, verifies it and synchronizes the planning package to it. The Home in the original plan is not retained.</span></div>`, false);
  }

  // --- Pause/resume sequence evidence, including the continuation warning --------------------
  function mxSequenceCard(S) {
    const q = S.sequence, cont = mx.continuationView(S);
    const contPill = cont.state === "verified" ? rp("CONTINUATION VERIFIED", "c")
      : cont.state === "not_verified" ? rp("CONTINUATION NOT VERIFIED", "d")
      : rp("not tested", "u");
    return card("Pause / resume sequence evidence", contPill,
      cont.state === "not_verified" ? "caution" : cont.state === "verified" ? "ok" : "idle",
      `<div class="metrics">
         ${row("Current / count", q.current == null && q.count == null ? `<span class="txt-u">—</span>` : `${q.current == null ? "?" : q.current} / ${q.count == null ? "?" : q.count}`)}
         ${row("Before pause", val(q.beforePause))}
         ${row("At resume", val(q.atResume))}
         ${row("First after resume", val(q.firstAfterResume))}
         ${row("Continuation verified", q.continuationVerified === true ? rp("TRUE", "c") : q.continuationVerified === false ? rp("FALSE", "d") : rp("not reported", "u"))}
         ${row("Started / paused / resumed", `<span class="mono">${fmtTime(S.timestamps.start)} · ${fmtTime(S.timestamps.pause)} · ${fmtTime(S.timestamps.resume)}</span>`)}
       </div>
       ${cont.state === "not_verified"
          ? `<div class="reason-note" style="border-left:3px solid var(--disconnected)">${warnSvg}<span><b>AUTO resumed, but continuation from the paused waypoint was not verified.</b> Check whether the Pixhawk restarted the mission at waypoint 0. The mode transition succeeded — continuation did not.</span></div>`
          : `<div class="reason-note">${gapSvg}<span>${cont.message}</span></div>`}`, false);
  }

  // --- Replanning handoff + return completion ------------------------------------------------
  function mxReturnCard(S) {
    const prog = mx.returnProgress(S);
    const rc = S.returnCompletion;
    const complete = mx.isComplete(S);
    const pct = prog ? Math.round(prog.fraction * 100) : 0;
    const cond = complete ? rp("COMPLETED_HOLD · FINAL LOITER VERIFIED", "c")
      : S.state === "RETURNING_HOME" || S.state === "HOME_ARRIVAL_PENDING" || S.state === "FINAL_HOLD_REQUESTED"
        ? rp(String(S.state), "p")
        : S.replanning.active ? rp("REPLANNING", "p") : rp("idle", "u");
    return card("Replanning handoff & return completion", cond,
      complete ? "ok" : S.replanning.active || S.state === "RETURNING_HOME" ? "caution" : "idle",
      `<div class="metrics">
         ${row("Mission execution", `<b>${val(S.effectiveState || S.state)}</b>`)}
         ${row("Replanning FSM", S.replanning.fsmState ? `<b>${S.replanning.fsmState}</b>` : `<span class="txt-u">—</span>`)}
         ${row("Distance to Home", rc.distanceToHomeM == null ? `<span class="txt-u">—</span>` : rc.distanceToHomeM.toFixed(1) + " m")}
         ${row("Arrival radius", rc.arrivalRadiusM == null ? `<span class="txt-u">—</span>` : rc.arrivalRadiusM + " m")}
         ${row("Arrival persistence", prog ? `${prog.done} / ${prog.total} s` : `<span class="txt-u">—</span>`)}
         ${row("Arrival confirmed", rc.arrivalConfirmed === true ? rp("CONFIRMED", "c") : rc.arrivalConfirmed === false ? rp("NOT CONFIRMED", "u") : rp("not reported", "u"))}
         ${row("Final LOITER verified", rc.finalLoiterVerified === true ? rp("VERIFIED", "c") : rc.finalLoiterVerified === false ? rp("NOT VERIFIED", "d") : rp("not reported", "u"))}
         ${row("Mission complete", complete ? rp("COMPLETE", "c") : rp("NOT COMPLETE", "u"))}
       </div>
       ${prog ? `<div style="padding:2px 13px 10px"><div style="height:6px;border-radius:3px;background:var(--line);overflow:hidden"><div style="height:100%;width:${pct}%;background:${complete ? "var(--connected)" : "var(--partitioned)"}"></div></div><div class="cond" style="margin-top:4px">arrival persistence ${prog.done} / ${prog.total} s (${pct}%)</div></div>` : ""}
       ${S.replanning.active
          ? `<div class="reason-note">${warnSvg}<span>The replanning controller owns the vehicle. Mission execution issues no competing mode command, and Start / Pause / Resume stay disabled until Scout hands control back.</span></div>`
          : ""}
       <div class="reason-note">${gapSvg}<span>The mission is complete only when Scout reports <b>COMPLETED_HOLD</b> <i>and</i> <b>final_loiter_verified = true</b>. Reaching Home, or the persistence bar filling, is not completion. Replanning ending in SAFE_HOLD / SUSPENDED / FAILED / FALLBACK_RTL leaves mission execution SUSPENDED — the original mission is not resumed automatically.</span></div>`, false);
  }

  // --- Lifecycle write trace (operation results, not poll-derived guesses) --------------------
  function mxOperationsCard(ops) {
    if (!ops.length) return card("Mission lifecycle operations", "", "idle",
      `<div style="padding:10px 13px"><span class="cond">No start / pause / resume / rearm has been issued for this vehicle in this session.</span></div>`, true);
    return card("Mission lifecycle operations", `${ops.length}`, "idle",
      `<div class="rlist" style="padding:8px 13px">
         ${ops.slice(-12).reverse().map((o) => `<div class="ritem">
            <span class="cond mono">${fmtTime(o.requested_at)}</span>
            <span class="rtx"><b>${String(o.operation || "").toUpperCase()}</b> ${rp(mx.outcomeLabel(o.outcome), MX_TINT[o.outcome] || "u")}${o.resulting_state ? " → " + o.resulting_state : ""}${o.verified_mode ? " · mode " + o.verified_mode : ""}${o.scout_error_code ? ` · <span class="txt-d">${o.scout_error_code}</span>` : ""}${o.continuation_verified === false ? " · " + rp("CONTINUATION NOT VERIFIED", "d") : ""}${o.reconciliation ? ` · reconciled: ${o.reconciliation.resolved}` : ""}</span>
            <span class="rav mono">${o.operation_id || ""}</span>
          </div>`).join("")}
       </div>`, true);
  }

  function wireMissionExecution() {
    const primary = document.getElementById("mx-primary");
    if (primary) primary.onclick = () => {
      const action = primary.dataset.action;
      if (action === "start") {
        if (!window.confirm("Start Mission?\n\nScout will hold position, set the CURRENT launch position as Home, verify it, synchronize the planning package and then start AUTO. The originally planned Home is not retained.")) return;
        mxWrite("Start Mission", (id) => api.startMissionExecution(id, {}));
      } else if (action === "pause") {
        mxWrite("Pause Mission", (id) => api.pauseMissionExecution(id));
      } else if (action === "resume") {
        mxWrite("Resume Mission", (id) => api.resumeMissionExecution(id));
      }
    };
    const rearmBtn = document.getElementById("mx-rearm");
    if (rearmBtn) rearmBtn.onclick = () => {
      if (!window.confirm("Rearm the mission controller?\n\nThis resets the Local Agent's mission-execution state only. It does NOT clear the Pixhawk mission, does NOT change vehicle mode and does NOT re-upload the original mission.")) return;
      mxWrite("Rearm controller", (id) => api.rearmMissionExecution(id));
    };
  }

  // ================= Replanning supervisory view (Scout Local Agent /agent/replan/*) =======
  // Seven concepts kept DISTINCT (task Section 4): communication (above), Pixhawk mode, control
  // authority (above), replanning FSM state, decision, mission revision, planning-package
  // readiness. Everything here is Scout's word, normalized by lib/replan.js; the frontend
  // never generates an alternate transition or a competing decision.
  const rp = (label, tint) => `<span class="pill ${tint}">${label}</span>`;
  const val = (v) => (v === null || v === undefined || v === "" ? `<span class="txt-u">—</span>` : String(v));
  const shortHash = (h) => (h ? String(h).replace(/^sha256:/, "").slice(0, 12) + "…" : "—");
  const OUTCOME_TINT = { accepted: "c", rejected: "d", unknown: "p", unavailable: "u", unsupported: "u", pending: "p" };

  function replanSection(v, { connected, stale }) {
    // Isolation guard: only render replan state actually fetched for THIS vehicle.
    const forThis = replanForVid != null && v && replanForVid === v.id;
    const S = forThis ? replan.normalizeReplanStatus(replanStatus) : replan.normalizeReplanStatus(null);
    const rd = forThis && replanReadiness ? replanReadiness : null;
    const cfg = forThis && replanConfig ? replanConfig : null;
    const exp = forThis && replanExperiment ? replanExperiment : null;

    if (replanStatus && replanStatus.supported === false) {
      return `<div class="sect" style="padding:14px 0 6px"><span class="lbl">Replanning (Scout Local Agent)</span></div>
        ${gapBody("Replanning not supported by this Scout version.")}`;
    }

    return `<div class="sect" style="padding:14px 0 6px"><span class="lbl">Replanning (Scout Local Agent)</span>
        <span class="tag">safe-return lifecycle — decision, FSM, mission revision &amp; package shown verbatim from Scout ${S.decision.simulated ? rp("SIMULATED", "p") : ""}</span>
        ${replanMsg ? `<span style="margin-left:8px">${rp(replanMsg.label + ": " + replan.outcomeLabel(replanMsg.outcome) + (replanMsg.code ? " (" + replanMsg.code + ")" : ""), OUTCOME_TINT[replanMsg.outcome] || "u")}</span>` : ""}
      </div>
      <div class="subgrid two">${readinessCard(rd)}${decisionReplanCard(S)}</div>
      <div class="subgrid two">${transactionCard(S)}${missionRevisionCard(S)}</div>
      <div class="subgrid two">${execConfigCard(cfg, S, rd, exp, av2(v, stale))}${experimentCard(exp, v)}</div>
      ${transitionsReplanCard(S)}`;
  }

  function av2(v, stale) {
    const view = authCtl.view();
    return { authorityKnown: !stale && view.reachable !== false && view.value != null, value: stale ? null : view.value };
  }

  function readinessCard(rd) {
    if (!rd) return card("Mission / Replanning readiness", availTag(AVAIL.GAP), "idle",
      gapBody("Scout replanning readiness unavailable."), true);
    const vm = rd.vehicle_mission || {}, pk = rd.planning_package || {};
    const badge = (ok, on, off) => rp(ok ? on : off, ok ? "c" : "d");
    const lims = Array.isArray(rd.limitations) ? rd.limitations : [];
    return card("Mission / Replanning readiness",
      `${badge(rd.mission_ready, "MISSION READY", "MISSION NOT READY")} ${badge(rd.replanning_ready, "REPLANNING READY", "NOT READY")}`,
      rd.replanning_ready ? "ok" : rd.mission_ready ? "caution" : "idle",
      `<div class="metrics">
         ${row("Vehicle mission", `${vm.mission_id ? `<span class="mono">${vm.mission_id}</span>` : "—"}`)}
         ${row("Pixhawk verified", badge(vm.pixhawk_verified, "VERIFIED", "NOT VERIFIED"))}
         ${row("Readback hash match", vm.readback_reachable ? badge(vm.readback_hash_match, "MATCH", "NO MATCH") : rp("readback unreachable", "u"))}
         ${row("Home valid", badge(vm.home_valid, "VALID" + (vm.home_source ? " · " + vm.home_source : ""), "INVALID"))}
         ${row("Package stored", badge(pk.stored, "STORED", "NOT STORED"))}
         ${row("Package consistency", pk.consistency ? rp(pk.consistency.replace("PLANNING_PACKAGE_", ""), pk.consistent ? "c" : "d") : badge(pk.consistent, "CONSISTENT", "NOT CONSISTENT"))}
         ${row("Mission id / hash match", `${badge(pk.mission_id_match, "ID", "ID?")} ${pk.hash_comparison_available ? badge(pk.hash_match, "HASH", "HASH✗") : rp("hash n/a", "u")}`)}
         ${pk.scout_state == null && pk.scout_replanning_ready == null ? ""
           : row("Scout readiness", `${badge(pk.scout_replanning_ready, "READY", "NOT READY")}${pk.scout_state ? " " + rp(pk.scout_state, "u") : ""}`)}
         ${row("Boundary supplied", badge(vm.boundary_supplied, "YES", "NO"))}
         ${row("Connector proven safe", pk.connector_proven_safe == null ? rp("unknown", "u") : badge(pk.connector_proven_safe, "PROVEN", "NOT PROVEN"))}
       </div>
       ${lims.length ? `<div class="reason-note">${warnSvg}<span>Limitations: ${lims.map((l) => l).join(" · ")}</span></div>` : ""}
       <div style="padding:9px 13px;display:flex;gap:8px;flex-wrap:wrap">
         <button class="btn" id="rp-pkg-put" ${replanBusy ? "disabled" : ""}>Upload approved planning package</button>
         <button class="btn ghost" id="rp-pkg-del" ${replanBusy ? "disabled" : ""}>Clear package</button>
       </div>`, true);
  }

  function decisionReplanCard(S) {
    const d = S.decision;
    if (!S.present) return card("Replanning decision", availTag(AVAIL.GAP), "idle",
      gapBody("Scout is not reporting a replanning decision."), false);
    const eng = d.energy && typeof d.energy === "object"
      ? Object.entries(d.energy).map(([k, x]) => `${k}=${x}`).join(", ") : val(d.energy);
    return card("Replanning decision", d.simulated ? rp("SIMULATED INPUT", "p") : availTag(AVAIL.LIVE), d.simulated ? "caution" : "ok",
      `<div class="metrics">
         ${row("Decision", `<b>${val(d.decision)}</b>`)}
         ${row("Reason codes", d.reasonCodes.length ? d.reasonCodes.map((c) => rp(c, "u")).join(" ") : "—")}
         ${row("Reason", val(d.reason))}
         ${row("Snapshot id", `<span class="mono">${val(d.snapshotId)}</span>`)}
         ${row("Energy calculation", eng)}
         ${row("Trigger persistence", val(d.persistence))}
         ${row("Real battery", d.realBattery == null ? "—" : d.realBattery + "%")}
         ${row("Simulated battery / margin", d.simulated ? rp("simulated — see experiment", "p") : "—")}
       </div>`, false);
  }

  function transactionCard(S) {
    const t = S.transaction;
    if (!S.present) return card("Replanning transaction", availTag(AVAIL.GAP), "idle",
      gapBody("No replanning transaction reported."), false);
    const cond = t.active ? rp("ACTIVE", "p") : t.terminal ? rp("TERMINAL", "d") : rp(String(t.fsmState || "IDLE"), "c");
    return card("Replanning transaction", cond, t.terminal ? "caution" : t.active ? "caution" : "ok",
      `<div class="metrics">
         ${row("FSM state", `<b>${val(t.fsmState)}</b>`)}
         ${row("Current step", val(t.currentStep))}
         ${row("Transition id", `<span class="mono">${val(t.transitionId)}</span>`)}
         ${row("Revision", val(t.revision))}
         ${row("Strategy", val(t.strategy))}
         ${row("Retry count", val(t.retryCount))}
         ${row("Cooldown", t.cooldownS == null ? "—" : t.cooldownS + "s")}
         ${row("Authority", val(t.authority))}
         ${row("Authority-blocked recommendation", val(t.authorityBlockedRecommendation))}
         ${row("Fallback", val(t.fallback))}
         ${row("Last error", t.lastError ? `<span class="txt-d">${t.lastError}</span>` : "—")}
       </div>
       <div style="padding:9px 13px">
         <button class="btn ghost" id="rp-reset" ${replanBusy || t.active ? "disabled" : ""} title="Rearms the Local Agent controller — issues NO vehicle command, does not change mode or the Pixhawk mission">Rearm replanning controller</button>
         ${t.active ? `<span class="cond" style="margin-left:8px">reset is refused while a transaction is active</span>` : ""}
       </div>`, false);
  }

  function missionRevisionCard(S) {
    const m = S.missionRevision;
    if (!S.present) return card("Mission revision", availTag(AVAIL.GAP), "idle",
      gapBody("No mission revision reported."), false);
    return card("Mission revision", val(m.revision === null ? "—" : "rev " + m.revision), "ok",
      `<div class="metrics">
         ${row("Original hash", `<span class="mono">${shortHash(m.originalHash)}</span>`)}
         ${row("Revised hash", `<span class="mono">${shortHash(m.revisedHash)}</span>`)}
         ${row("Preserved waypoints", val(m.preservedCount))}
         ${row("Removed waypoints", val(m.removedCount))}
         ${row("Inserted waypoints", val(m.insertedCount))}
         ${row("Revised waypoints", val(m.revisedCount))}
         ${row("Validation result", val(m.validationResult))}
         ${row("Upload result", val(m.uploadResult))}
         ${row("Readback result", val(m.readbackResult))}
       </div>`, false);
  }

  function execConfigCard(cfg, S, rd, exp, auth) {
    const scout = cfg && cfg.scout ? cfg.scout : null;
    if (!scout) return card("Execution configuration", availTag(AVAIL.GAP), "idle",
      gapBody("Scout replanning config unavailable."), false);
    const stage = replan.executionStage(scout);
    const injectionActive = !!(exp && exp.scout && (exp.scout.active || exp.scout.source === "SIMULATED"));
    const blockers = replan.realExecutionBlockers({
      injectionActive, transactionActive: S.transaction.active,
      packageConsistent: !!(rd && rd.planning_package && rd.planning_package.consistent),
      homeValid: !!(rd && rd.vehicle_mission && rd.vehicle_mission.home_valid),
      authorityKnown: auth.authorityKnown,
    });
    const stageBadge = stage === replan.STAGE.REAL ? rp("REAL EXECUTION", "d")
      : stage === replan.STAGE.DRY_RUN ? rp("DRY-RUN", "p") : rp("DISABLED", "u");
    const src = (k) => (scout.sources && scout.sources[k]) ? `<span class="cond">${scout.sources[k]}</span>` : "";
    return card("Execution configuration", stageBadge, stage === replan.STAGE.REAL ? "caution" : "ok",
      `<div class="metrics">
         ${row("Autonomous execution", `${scout.autonomous_execution_enabled ? "ENABLED" : "DISABLED"} ${src("autonomous_execution_enabled")}`)}
         ${row("Dry run", `${scout.dry_run ? "TRUE" : "FALSE"} ${src("dry_run")}`)}
         ${row("RTL fallback", `${scout.rtl_fallback_enabled ? "ENABLED" : "DISABLED (default)"} ${src("rtl_fallback_enabled")}`)}
         ${row("Critical battery", val(scout.critical_battery_percent) + "%")}
         ${row("Reserve margin", val(scout.reserve_margin_percent) + "%")}
       </div>
       <div style="padding:9px 13px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
         <button class="btn ghost" id="rp-stage-disabled" ${replanBusy ? "disabled" : ""}>Disable</button>
         <button class="btn" id="rp-stage-dry" ${replanBusy ? "disabled" : ""}>Enable dry-run</button>
         <button class="btn" id="rp-stage-real" ${replanBusy || blockers.length ? "disabled" : ""} title="${blockers.join(" · ")}">Enable real execution</button>
       </div>
       ${blockers.length ? `<div class="reason-note">${warnSvg}<span>Real execution blocked: ${blockers.join(" · ")}</span></div>`
         : `<div class="reason-note">${gapSvg}<span>Real execution requires: clear experiment, no active transaction, consistent package, valid Home, explicit confirm. Runtime overrides disappear when Scout restarts.</span></div>`}`, false);
  }

  function experimentCard(exp, v) {
    const scout = exp && exp.scout ? exp.scout : null;
    const active = !!(scout && (scout.active || scout.source === "SIMULATED"));
    const realBattery = num(v && v.battery);
    return card("Energy replanning experiment", active ? rp("SIMULATED · ACTIVE", "p") : rp("inactive", "u"), active ? "caution" : "idle",
      `<div class="metrics">
         ${row("Real battery (telemetry)", realBattery == null ? "—" : realBattery + "%")}
         ${row("Injection state", active ? rp("SIMULATED", "p") : "none")}
         ${row("Created", val(scout && (scout.created_at || scout.created)))}
         ${row("Expires", val(scout && (scout.expires_at || scout.expiry)))}
         ${row("Overrides", scout && scout.overrides ? Object.entries(scout.overrides).map(([k, x]) => `${k}=${x}`).join(", ") : (active ? "(applied)" : "—"))}
       </div>
       <div style="padding:8px 13px;display:grid;grid-template-columns:1fr 1fr;gap:6px 10px">
         <label class="cond" style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="rp-exp-force"> Force safe return</label>
         <label class="cond">Battery % <input type="number" id="rp-exp-batt" min="0" max="100" style="width:60px"></label>
         <label class="cond">Energy margin % <input type="number" id="rp-exp-margin" style="width:60px"></label>
         <label class="cond">Duration s <input type="number" id="rp-exp-dur" min="1" max="3600" style="width:70px" placeholder="300"></label>
       </div>
       <div style="padding:4px 13px 11px;display:flex;gap:8px">
         <button class="btn" id="rp-exp-apply" ${replanBusy ? "disabled" : ""}>Apply injection</button>
         <button class="btn ghost" id="rp-exp-clear" ${replanBusy ? "disabled" : ""}>Clear</button>
       </div>
       <div class="reason-note">${gapSvg}<span>Always SIMULATED — real telemetry is never overwritten. Clear the injection and rearm the controller before enabling real execution.</span></div>`, false);
  }

  function transitionsReplanCard(S) {
    if (!S.present || !S.transitions.length) return card("Recent replanning transitions", "", "idle",
      `<div style="padding:10px 13px"><span class="cond">${S.present ? "No transitions reported." : "Scout not reporting transitions."}</span></div>`, true);
    return card("Recent replanning transitions", `${S.transitions.length}`, "idle",
      `<div class="rlist" style="padding:8px 13px">
         ${S.transitions.slice(-12).map((tr) => `<div class="ritem">
            <span class="cond mono">${fmtTime(tr.timestamp)}</span>
            <span class="rtx"><b>${val(tr.from)}</b> → <b>${val(tr.to)}</b>${tr.reason ? " · " + tr.reason : ""}</span>
            <span class="rav mono">${tr.transitionId || ""}</span>
            ${tr.simulated ? rp("SIM", "p") : ""}
          </div>`).join("")}
       </div>`, true);
  }

  function wireReplan() {
    const on = (id, fn) => { const el = document.getElementById(id); if (el) el.onclick = fn; };
    on("rp-pkg-put", () => replanWrite("Package upload", (id) => api.putReplanPackage(id, {})));
    on("rp-pkg-del", () => replanWrite("Package clear", (id) => api.deleteReplanPackage(id)));
    on("rp-reset", () => replanWrite("Controller rearm", (id) => api.resetReplanController(id)));
    on("rp-stage-disabled", () => replanWrite("Config: disable", (id) => api.patchReplanConfig(id, replan.stagePatch(replan.STAGE.DISABLED))));
    on("rp-stage-dry", () => replanWrite("Config: dry-run", (id) => api.patchReplanConfig(id, replan.stagePatch(replan.STAGE.DRY_RUN))));
    on("rp-stage-real", () => {
      if (!window.confirm("Enable REAL execution? Scout may command the vehicle (LOITER/AUTO/RTL). Confirm the experiment is cleared and no transaction is active.")) return;
      replanWrite("Config: real execution", (id) => api.patchReplanConfig(id, replan.stagePatch(replan.STAGE.REAL)));
    });
    on("rp-exp-apply", () => {
      const payload = replan.injectionPayload({
        forceSafeReturn: document.getElementById("rp-exp-force") && document.getElementById("rp-exp-force").checked,
        batteryPercent: fieldVal("rp-exp-batt"), energyMarginPercent: fieldVal("rp-exp-margin"), durationS: fieldVal("rp-exp-dur"),
      });
      if (!replan.injectionHasOverride(payload)) { replanMsg = { label: "Injection", outcome: "rejected", error: "at least one override required" }; renderDetail(); return; }
      replanWrite("Injection apply", (id) => api.putReplanExperiment(id, payload));
    });
    on("rp-exp-clear", () => replanWrite("Injection clear", (id) => api.deleteReplanExperiment(id)));
  }
  function fieldVal(id) { const el = document.getElementById(id); return el && el.value !== "" ? el.value : null; }

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

  // Latest operator/agent ACTION and whether Scout reported it executed or blocked —
  // sourced ONLY from the backend's command lifecycle events (which carry structured
  // detail: command_type/source/stage/outcome). Never invents an outcome: "blocked" is a
  // REJECTED result, "executed" a VERIFIED/EXECUTED one, "failed" a FAILED/EXPIRED one —
  // all Scout-reported. Renders nothing until a command event exists for this vehicle.
  function latestActionRow(vEvents) {
    const cmdEv = vEvents.filter((e) => e.type === "command" && e.detail).slice(-1)[0];
    if (!cmdEv) return "";
    const d = cmdEv.detail;
    const outcome = d.outcome || d.stage || "";
    const verb = outcome === "REJECTED" ? ["blocked", "d"]
      : (outcome === "VERIFIED" || outcome === "EXECUTED") ? ["executed", "c"]
      : (outcome === "FAILED" || outcome === "EXPIRED") ? ["failed", "d"]
      : ["pending", "p"];
    return `<div class="agent-last-action">
      <span class="k">Latest action</span>
      <span class="v"><span class="ctl-type mono">${d.command_type || "—"}</span> ${pill(verb[0], verb[1])}${d.command_source ? `<span class="src-chip">${d.command_source}</span>` : ""}</span>
    </div>`;
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
    // Isolation: clear the previous vehicle's replan + mission-execution panels immediately (so
    // no stale lifecycle state, operation result or completion claim can be read as this
    // vehicle's), then load this one's.
    replanStatus = replanReadiness = replanConfig = replanExperiment = null;
    replanMsg = null; replanForVid = null;
    mxStatus = null; mxOps = []; mxResult = null; mxForVid = null;
    loadAuthority(id);
    loadReplan(id);
    loadMissionExecution(id);
  }

  function onFleet(data) {
    fleet = Array.isArray(data) ? data : [];
    if (selId == null && fleet.length) {
      selId = (fleet.find((v) => v.online) || fleet.find((v) => v.lat != null) || fleet[0]).id;
      loadAuthority(selId);
      loadReplan(selId);
      loadMissionExecution(selId);
    }
    document.getElementById("veh-list").innerHTML = vehicleRows(fleet, selId);
    document.querySelectorAll("#veh-list .vrow").forEach((el) => (el.onclick = () => { select(canonicalVehicleId(el.dataset.id)); onFleet(fleet); }));
    renderDetail();
    updateRibbon({ counts: counts() });
  }

  const stopFleet = api.poll(api.getFleet, 2000, onFleet, () => {});
  const authorityId = setInterval(() => loadAuthority(selId), 2000);
  const eventsId = setInterval(loadEvents, 3000);
  // Replan status/readiness/config/experiment poll for the SELECTED vehicle. Reads only —
  // never a write — so this poll can never resend a supervisory operation. Skipped while a
  // write is in flight so a reconciling read does not race the write's own follow-up read.
  const replanId = setInterval(() => { if (!replanBusy) loadReplan(selId); }, 2500);
  // Mission-execution status poll for the SELECTED vehicle. Reads only — it can never start,
  // pause, resume or rearm anything, so a reconnect or a backgrounded tab cannot re-issue a
  // lifecycle operation. Skipped while a write is in flight so the reconciling read that follows
  // the write is the one that decides the button, and paced faster than the replan poll because
  // Start/Pause/Resume move through several transitional states.
  const mxId = setInterval(() => { if (!mxBusy) loadMissionExecution(selId); }, 2000);
  loadEvents();
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });

  return function cleanup() { stopFleet(); clearInterval(clockId); clearInterval(authorityId); clearInterval(eventsId); clearInterval(replanId); clearInterval(mxId); authCtl.dispose(); };
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
