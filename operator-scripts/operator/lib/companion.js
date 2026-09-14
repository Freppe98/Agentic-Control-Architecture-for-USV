// companion.js — pure view policy for Scout's UAV companion (companion-v2). No DOM, no Leaflet.
//
// The backend (companion_telemetry.py) validates Scout's report and publishes it on the PARENT's
// fleet row as `v.companions`. This module turns that into operator-facing states, and it is the
// single tested place that decides:
//   • which companions belong to a row (only items naming THIS row as parent — defence in depth);
//   • the dock indicator state: active / idle / stale / lost / unknown, with text, not colour only;
//   • whether a UAV position or hazard is current or last-known — see `evidenceView` below, the
//     ONE place that turns the backend's three-number freshness model into display text;
//   • how a hazard reads: proposed observation vs Scout-accepted exclusion vs dismissed, and what
//     Scout reported about replanning — a missing outcome stays "not reported", never inferred.
//
// Four facts are kept apart on purpose and never derived from one another:
//   assignment (Scout says the UAV is its companion), radio reachability (Scout's own link claim
//   plus the age of the last peer contact), task activity, and fresh position evidence.
// And Scout↔operator freshness (the parent's comm_state) stays separate from UAV↔Scout freshness:
// when Scout itself is silent, nothing it last said about the UAV is presented as current.
//
// FRESHNESS: the backend never collapses "how old" into one number (see companion_telemetry.py's
// EVIDENCE TIME / CLASSIFICATION sections). Every timestamped fact — position, battery,
// inspection, link contact, hazard observation, hazard disposition/replan decision times —
// arrives as a small object:
//   { observed_at, min_age_s, reporting_delay_s, estimated_age_s, freshness_quality, clock_anomaly }
// `min_age_s` is a guaranteed LOWER BOUND (assumes zero delivery delay) — the ONLY number this
// module uses to decide STALE. `reporting_delay_s` is an ESTIMATE of how long the envelope took to
// arrive, which conflates real transit delay with any Scout<->operator clock offset — it can be
// wrong in EITHER direction (e.g. Scout's clock 30s ahead plus a genuine 30s delivery delay reads
// as zero delay, hiding a real 60s-old observation behind a small min_age_s AND a reassuring
// estimate). `estimated_age_s` = min_age_s + reporting_delay_s, DISPLAY ONLY.
//
// So there is no automated "FRESH" verdict anywhere in this module: without a validated upper
// bound on an observation's age (this station has none, and invents none), a small min_age_s
// proves nothing about the true age — it is UNCERTAIN, not fresh. `evidenceView()` is the ONE
// place a backend freshness object becomes one of three states — STALE / UNCERTAIN / UNKNOWN —
// plus honestly-labelled display text, so a UAV measurement is never called fresh just because
// its envelope happens to have just arrived, or because its lower bound happens to look small.
import { commState } from "./ui.js";

/** Evidence older than this reads as last-known (matches the backend's PARTITIONED threshold). */
export const EVIDENCE_STALE_S = 15;
/** A UAV position older than this is drawn as stale. */
export const POSITION_FRESH_S = 10;
/** A reporting delay (envelope transit-time estimate) above this is worth calling out on its
 *  own — most packets on a healthy link arrive within a second or two of being composed. */
export const REPORTING_DELAY_NOTABLE_S = 5;

const ACTIVE_ACTIVITY = new Set(["LAUNCHING", "TRANSIT", "INSPECTING", "HOLDING", "RETURNING", "LANDING"]);
const IDLE_ACTIVITY = new Set(["IDLE", "STANDBY", "LANDED"]);

export const STATE_SHORT = { active: "ACTIVE", idle: "IDLE", stale: "STALE", lost: "LOST", unknown: "UNKNOWN" };

const LINK_TEXT = {
  CONNECTED: "Connected", DEGRADED: "Degraded", LOST: "Lost",
  NEVER_CONNECTED: "No contact yet", UNKNOWN: "Not reported",
};
const DISPOSITION_TEXT = {
  REPORTED: "Proposed — awaiting Scout review",
  UNDER_REVIEW: "Proposed — Scout reviewing",
  ACCEPTED: "Accepted by Scout as exclusion",
  REJECTED: "Rejected by Scout",
  EXPIRED: "Expired (Scout)",
  UNKNOWN: "Scout response not reported",
};
// tone → the pxm-chip / note modifier the page already styles (ok / pending / warn / dim)
const REPLAN = {
  NOT_REQUIRED: ["No replan required (Scout)", "dim"],
  PENDING: ["Replanning pending", "pending"],
  IN_PROGRESS: ["Replanning in progress", "pending"],
  VERIFIED: ["Revised mission verified by Scout", "ok"],
  FAILED: ["Replanning failed", "warn"],
  BLOCKED: ["Replanning blocked", "warn"],
  UNKNOWN: ["Replanning state not recognised", "dim"],
};
const ALT_REF_TEXT = { AGL: "AGL", AMSL: "AMSL", RELATIVE_HOME: "above UAV home" };

const isObj = (x) => x !== null && typeof x === "object" && !Array.isArray(x);
const num = (x) => (typeof x === "number" && Number.isFinite(x) ? x : null);
const title = (tok) => String(tok).toLowerCase().replace(/_/g, " ").replace(/^\w/, (m) => m.toUpperCase());

/** "<1 s" / "12 s" / "4 min" / "2 h" — or null when the age is unknown. */
export function fmtEvidenceAge(s) {
  const a = num(s);
  if (a == null) return null;
  if (a < 1) return "<1 s";
  if (a < 120) return `${Math.round(a)} s`;
  if (a < 7200) return `${Math.round(a / 60)} min`;
  return `${Math.round(a / 3600)} h`;
}

/**
 * The ONE place a backend freshness object `{min_age_s, reporting_delay_s, estimated_age_s,
 * freshness_quality, clock_anomaly}` becomes display text and a classification.
 *
 * `known` — evidence exists at all (the field itself was reported, even if its age isn't).
 * `quality` — "LOWER_BOUND" (min_age_s is a trustworthy FLOOR — never a maximum-age guarantee)
 *   or "UNKNOWN" (no envelope timestamp was available for this packet, so NOTHING about the
 *   observation's current age can be said).
 * `state` — "STALE" (min_age_s alone already exceeds `thresholdS` — safe, since the true age can
 *   only be larger), "UNCERTAIN" (a small/known min_age_s that does NOT prove the observation is
 *   current — nothing rules out an unaccounted-for delivery delay), or "UNKNOWN" (quality
 *   UNKNOWN — no envelope timestamp at all, so not even a lower bound exists). There is
 *   deliberately no "FRESH" state: this module never asserts one, because doing so from
 *   `estimated_age_s`/`reporting_delay_s` alone is exactly the bug this model exists to avoid
 *   (see the module header comment for the concrete counter-example).
 * `stale` — `state !== "UNCERTAIN"` (i.e. STALE or UNKNOWN), a plain boolean for callers that
 *   only want ONE "can this be presented as current" verdict, e.g. the existing LAST-KNOWN
 *   visual treatment — UNKNOWN evidence gets the SAME (safe) treatment as PROVEN-stale evidence
 *   here, because "we cannot say how old this is" is, if anything, a WEAKER claim than a known
 *   lower bound, never a stronger one.
 * `uncertain` — `state === "UNCERTAIN"` exactly, for callers that want a lighter-weight "recency
 *   not confirmed" caveat distinct from both PROVEN-stale and UNKNOWN-timing treatments.
 * `ageText` — ready-to-render, ALWAYS phrased as a lower bound ("at least Ns ago"), with an
 *   explicit "(delivery delay ~Ds)" appended when the reporting-delay estimate is notable, or
 *   "timing unknown" when quality is UNKNOWN. Never a bare "Ns ago", which would read as a
 *   confirmed precise reading this module cannot actually back up.
 */
export function evidenceView(fr, thresholdS = EVIDENCE_STALE_S) {
  if (!isObj(fr) || fr.freshness_quality !== "LOWER_BOUND") {
    return { known: isObj(fr), ageText: "timing unknown", state: "UNKNOWN", stale: true,
             uncertain: false, quality: "UNKNOWN", clockAnomaly: !!(isObj(fr) && fr.clock_anomaly),
             minAgeS: null, estimatedAgeS: null, delayNoteS: null };
  }
  const minAgeS = num(fr.min_age_s);
  const delayS = num(fr.reporting_delay_s);
  const estimatedAgeS = num(fr.estimated_age_s);
  const notable = delayS != null && delayS > REPORTING_DELAY_NOTABLE_S;
  const isStale = minAgeS == null || minAgeS > thresholdS;
  const ageText = minAgeS == null ? "timing unknown"
    : `at least ${fmtEvidenceAge(minAgeS)} ago` + (notable ? ` (delivery delay ~${fmtEvidenceAge(delayS)})` : "");
  return {
    known: true, ageText, state: isStale ? "STALE" : "UNCERTAIN", stale: isStale,
    uncertain: !isStale, quality: "LOWER_BOUND", clockAnomaly: !!fr.clock_anomaly,
    minAgeS, estimatedAgeS, delayNoteS: notable ? delayS : null,
  };
}

const parentCurrent = (v) => commState(v) === "connected";

/** Companions reported BY this row, and only those that name this row as their parent. */
export function companionsOf(v) {
  const blk = v && isObj(v.companions) ? v.companions : null;
  const items = blk && Array.isArray(blk.items) ? blk.items : [];
  return items.filter((c) => isObj(c) && typeof c.companion_id === "string" && c.parent_id === v.id);
}

export function assignedCompanions(v) {
  return companionsOf(v).filter((c) => c.assignment_state === "ASSIGNED");
}

/**
 * Operator-facing status of one companion, judged in a fixed order: is Scout itself current →
 * is Scout's report of this companion current → what does Scout say about the link → how old is
 * the last peer contact → what is the UAV doing. Each step can only make the answer LESS certain.
 */
export function companionStatus(c, v) {
  const link = isObj(c.link) ? c.link : {};
  const linkState = typeof link.state === "string" ? link.state : "UNKNOWN";
  const peerEv = evidenceView(link.last_peer_contact, EVIDENCE_STALE_S);
  const statusAgeS = num(c.status_age_s);      // pure local-monotonic elapsed time; no cross-
                                                // clock estimate needed for this one (see companion_telemetry.py)
  const activity = isObj(c.activity) && typeof c.activity.state === "string" ? c.activity.state : null;
  const type = c.vehicle_type || "UAV";
  const name = c.display_name || c.companion_id;
  const parent = (v && v.name) || "Scout";
  const scoutCurrent = parentCurrent(v);
  const reportCurrent = scoutCurrent && statusAgeS != null && statusAgeS <= EVIDENCE_STALE_S;
  let state, label, reason;
  if (!scoutCurrent) {
    state = "stale"; label = `${type} last known`;
    reason = `${parent} is not in contact — this is what it last reported${statusAgeS != null ? ` ${fmtEvidenceAge(statusAgeS)} ago` : ""}.`;
  } else if (!reportCurrent) {
    state = "stale"; label = `${type} not reported`;
    reason = `${parent} is reporting, but has not reported ${name}${statusAgeS != null ? ` for ${fmtEvidenceAge(statusAgeS)}` : ""}.`;
  } else if (linkState === "LOST") {
    state = "lost"; label = `${type} lost`; reason = `${parent} reports its link to ${name} is lost.`;
  } else if (linkState === "DEGRADED") {
    state = "stale"; label = `${type} degraded`; reason = `${parent} reports a degraded link to ${name}.`;
  } else if (linkState === "CONNECTED") {
    // Only a PROVEN-stale (or entirely unknown) peer-contact age forces "no recent contact" —
    // a small/UNCERTAIN one does not disprove reachability, so it falls through to Scout's own
    // activity claim instead (see companion_telemetry.py's CLASSIFICATION: there is no "fresh"
    // verdict this module can assert from the evidence alone).
    if (peerEv.state !== "UNCERTAIN") {
      state = "stale"; label = `${type} no recent contact`;
      reason = `${parent} reports connected, but its last contact with ${name} was ${peerEv.known ? peerEv.ageText : "not reported"}.`;
    } else if (activity && ACTIVE_ACTIVITY.has(activity)) {
      // "active" reflects SCOUT'S OWN CURRENT CLAIM (safe: built from status_age_s, a pure
      // local-receipt fact) — it is not, and must never be read as, independent proof that the
      // UAV's own measurements are fresh. See positionView/hazardView for that distinction.
      state = "active"; label = `${type} active`; reason = `${name}: ${title(activity)} (reported by ${parent}).`;
    } else if (activity && IDLE_ACTIVITY.has(activity)) {
      state = "idle"; label = `${type} idle`; reason = `${name}: ${title(activity)} (reported by ${parent}).`;
    } else {
      state = "unknown"; label = `${type} activity unknown`; reason = `${parent} has not reported what ${name} is doing.`;
    }
  } else if (linkState === "NEVER_CONNECTED") {
    state = "unknown"; label = `${type} no contact yet`; reason = `${name} is assigned, but ${parent} has not heard from it yet.`;
  } else {
    state = "unknown"; label = `${type} link unknown`; reason = `${parent} does not report the link to ${name}.`;
  }
  return { state, label, reason, linkState, linkText: LINK_TEXT[linkState] || LINK_TEXT.UNKNOWN,
           peerEv, statusAgeS, activity, current: reportCurrent };
}

// ---- the dock TAB — Scout-reported LINK CONDITION only, never activity -----------------------
// The dock row's compact right-hand tab is a different question from companionStatus() above: it
// answers ONLY "what does Scout currently report about the radio link to the assigned companion",
// on a fixed four-way scale (green/yellow/red/grey) that has to stay legible as a small icon with
// no room for words. Activity ("inspecting" vs "idle") is deliberately NOT part of this — connected
// does not mean surveying, so folding activity into the tab's colour would let a mid-mission pause
// misread as a link problem or vice versa. See companionStatus/companionSection for activity.
//
// RED is the one level this module will not hand out cheaply: Scout's own `link.state` enum
// already distinguishes NEVER_CONNECTED from LOST, but nothing in the contract stops a buggy or
// freshly-restarted publisher from reporting LOST for a companion it has, in truth, never actually
// connected to (see SCOUT_INTEGRATION_HANDOFF.md's proposed `link.ever_connected` field — Scout
// does not report one today, so nothing here may invent one). Until Scout reports its own
// connection history explicitly, a LOST report only turns the tab red when THIS OPERATOR SESSION
// has itself previously observed that exact assignment (`${parentId}::${companionId}`) reporting
// CONNECTED or DEGRADED — see `updateLinkHistory` below. That evidence is scoped to the current
// assignment on purpose: unassigning, or Scout swapping in a different `companion_id`, drops the
// key entirely, so a replacement companion can never inherit a predecessor's history.
export const TAB_LEVELS = ["green", "yellow", "red", "grey"];

/**
 * Reducer for the operator-local "has this assignment ever reported CONNECTED/DEGRADED" evidence.
 * Pure: takes the previous history (a Map, or nothing) and the current fleet, returns a NEW Map —
 * callers (Map.js) hold the returned value and pass it back in on the next fleet poll, the same
 * pattern as lib/telemetry-cache.js. Keyed by `${parentId}::${companionId}`, and ONLY for the
 * currently ASSIGNED companion on each row: a key that stops being reported as ASSIGNED (removed,
 * or explicitly unassigned) is dropped on the very next update, never carried across a later
 * reassignment — see the module note above.
 */
export function createLinkHistory() {
  return new Map();
}

export function updateLinkHistory(history, fleet) {
  const next = new Map(history instanceof Map ? history : []);
  const seen = new Set();
  for (const v of Array.isArray(fleet) ? fleet : []) {
    for (const c of assignedCompanions(v)) {
      const key = `${v.id}::${c.companion_id}`;
      seen.add(key);
      const link = isObj(c.link) ? c.link : {};
      if (link.state === "CONNECTED" || link.state === "DEGRADED") next.set(key, true);
      else if (!next.has(key)) next.set(key, false);
    }
  }
  for (const key of [...next.keys()]) if (!seen.has(key)) next.delete(key);
  return next;
}

/** The ONE dock tab for a row — NEVER null (an unassigned/unavailable row still renders a grey
 *  tab, so every row keeps the same layout; see VehicleDock/CompanionPanel.CompanionTab).
 *  `history` is the Map from updateLinkHistory (or undefined/null — treated as "no local
 *  evidence yet", never as false evidence of loss). `aria`/`title` use the exact wording this
 *  station's tabs are documented to show (SCOUT_INTEGRATION_HANDOFF.md-adjacent — see
 *  docs/verification/uav-companion.md), so screen readers and hover both agree with the colour. */
export function companionTab(v, history) {
  if (!v) return { level: "grey", parentId: null, companionId: null, aria: "No companion assigned", title: "No companion assigned" };
  const list = assignedCompanions(v);
  if (!list.length) {
    return { level: "grey", parentId: v.id, companionId: null,
             aria: "No companion assigned", title: "No companion assigned" };
  }
  // Deterministic choice for a multi-companion row: always the FIRST assigned item in Scout's own
  // `items` order. Never re-picked based on which one looks "most interesting" as reports arrive —
  // that would make the tab (and the card it opens) flip identity on its own between polls.
  const c = list[0];
  const type = c.vehicle_type || "UAV";
  const name = c.display_name || c.companion_id;
  const parent = v.name || "Scout";
  const base = { parentId: v.id, companionId: c.companion_id, name, type };
  const statusAgeS = num(c.status_age_s);
  const scoutCurrent = parentCurrent(v);
  const reportCurrent = scoutCurrent && statusAgeS != null && statusAgeS <= EVIDENCE_STALE_S;
  const link = isObj(c.link) ? c.link : {};
  const linkState = typeof link.state === "string" ? link.state : "UNKNOWN";
  const linkText = LINK_TEXT[linkState] || LINK_TEXT.UNKNOWN;

  if (!reportCurrent) {
    // Scout itself went quiet, or simply hasn't refreshed this companion's block recently: this
    // is NOT evidence the UAV link failed (that would be inferring a UAV fact from a Scout fact —
    // exactly what this module exists to avoid), so the tab goes grey/unknown, never red/yellow,
    // and the tooltip says whose report is stale, plus what it LAST said.
    const why = !scoutCurrent ? `${parent} is not in contact` : `${parent} has not reported ${name} recently`;
    return { ...base, level: "grey", aria: "Companion status unavailable",
      title: `Companion status unavailable — ${why}. Last companion-link report: ${linkText}.` };
  }
  if (linkState === "CONNECTED") {
    return { ...base, level: "green", aria: `UAV companion — reported connected via ${parent}`,
      title: `${parent} reports ${name} connected.` };
  }
  if (linkState === "DEGRADED") {
    return { ...base, level: "yellow", aria: "UAV companion — link degraded",
      title: `${parent} reports a degraded link to ${name}.` };
  }
  if (linkState === "LOST") {
    const key = `${v.id}::${c.companion_id}`;
    const everConnected = !!(history && typeof history.get === "function" && history.get(key));
    if (everConnected) {
      return { ...base, level: "red", aria: "UAV companion — connection lost",
        title: `${parent} reports its link to ${name} is lost. It was previously connected.` };
    }
    // A first-ever LOST report with no CONNECTED/DEGRADED sighting behind it: honest grey, not a
    // fabricated "lost" for something that (as far as this station has ever seen) never connected.
    return { ...base, level: "grey", aria: "Companion status unavailable",
      title: `${parent} reports ${name} as lost, but no prior connection has been observed for this assignment this session.` };
  }
  if (linkState === "NEVER_CONNECTED") {
    return { ...base, level: "grey", aria: "Companion status unavailable",
      title: `${name} is assigned, but ${parent} has not heard from it yet.` };
  }
  return { ...base, level: "grey", aria: "Companion status unavailable",
    title: `${parent} does not report the link to ${name}.` };
}

/** The UAV position as the map should draw it, or null when none was reported. `stale` alone
 *  drives the existing LAST-KNOWN visual treatment (definite, min_age_s-based); `uncertain`
 *  is for a lighter-weight "recency not confirmed" caveat — this is NEVER presented as a
 *  current, confirmed fix (see the module header comment: there is no proven-fresh state). */
export function positionView(c, v) {
  const p = isObj(c.position) ? c.position : null;
  if (!p) return null;
  const lat = num(p.lat), lng = num(p.lng);
  if (lat == null || lng == null || Math.abs(lat) > 90 || Math.abs(lng) > 180) return null;
  const ev = evidenceView(p, POSITION_FRESH_S);
  const alt = num(p.altitude_m);
  const ref = ALT_REF_TEXT[p.altitude_ref];
  return {
    lat, lng, headingDeg: num(p.heading_deg),
    altText: alt != null && ref ? `${Math.round(alt)} m ${ref}` : null,
    stale: !parentCurrent(v) || ev.stale, uncertain: parentCurrent(v) && ev.uncertain,
    ageText: ev.ageText, quality: ev.quality, clockAnomaly: ev.clockAnomaly,
    minAgeS: ev.minAgeS, estimatedAgeS: ev.estimatedAgeS, delayNoteS: ev.delayNoteS,
    observedAt: p.observed_at,
  };
}

function validGeometry(g) {
  if (!isObj(g)) return null;
  if (g.type === "point") {
    const lat = num(g.lat), lng = num(g.lng);
    return lat != null && lng != null ? { type: "point", lat, lng } : null;
  }
  if (g.type === "polygon" && Array.isArray(g.ring_latlng)) {
    const ring = g.ring_latlng.filter((p) => Array.isArray(p) && num(p[0]) != null && num(p[1]) != null);
    return ring.length >= 3 && ring.length === g.ring_latlng.length ? { type: "polygon", ring } : null;
  }
  return null;
}

/** Scout's replanning outcome for a hazard — { state, text, tone, ageText }. Absent stays
 *  "not reported"; the decision TIME gets the same three-number freshness treatment as
 *  everything else, so a stale "VERIFIED" reads as stale, not as a fresh confirmation. */
export function replanView(r) {
  if (!isObj(r)) return { state: null, text: "Replanning: not reported", tone: "dim", ageText: null };
  const st = REPLAN[r.state] ? r.state : "UNKNOWN";
  let [text, tone] = REPLAN[st];
  if (st === "VERIFIED" && num(r.mission_revision) != null) text += ` (rev ${r.mission_revision})`;
  const ev = evidenceView(r.updated, EVIDENCE_STALE_S);
  return { state: st, text, tone, reason: typeof r.reason === "string" ? r.reason : null,
           ageText: ev.known ? ev.ageText : null, updatedEv: ev };
}

/** Whether Scout's hazard report for this companion is current, or last-known — the BLOCK/
 *  LIST-level currency (does the operator still trust this is the current hazard set), distinct
 *  from any individual hazard's own OBSERVATION age (see hazardView's own `stale`, which ORs
 *  the two together). */
function hazardsStale(c, v) {
  const sa = num(c.status_age_s), ha = num(c.hazards_age_s);
  return !parentCurrent(v) || sa == null || sa > EVIDENCE_STALE_S || ha == null || ha > EVIDENCE_STALE_S;
}

/**
 * One hazard as the operator should read it. `cls` is "accepted" ONLY for an explicit ACCEPTED
 * disposition; REJECTED/EXPIRED are "dismissed"; everything else — including a missing or
 * unrecognised disposition — is a "proposed" observation, never an exclusion.
 */
export function hazardView(h, c, v) {
  const disp = isObj(h.disposition) && DISPOSITION_TEXT[h.disposition.state] ? h.disposition.state : "UNKNOWN";
  const cls = disp === "ACCEPTED" ? "accepted" : (disp === "REJECTED" || disp === "EXPIRED") ? "dismissed" : "proposed";
  const exclusion = validGeometry(h.exclusion);
  const unc = num(h.uncertainty_m);
  const obsEv = evidenceView(h.observed, EVIDENCE_STALE_S);
  const decidedEv = evidenceView(isObj(h.disposition) ? h.disposition.decided : null, EVIDENCE_STALE_S);
  const listStale = hazardsStale(c, v);
  return {
    id: h.hazard_id, revision: num(h.revision), kind: title(h.kind || "UNKNOWN"), cls,
    dispositionState: disp, dispositionText: DISPOSITION_TEXT[disp],
    dispositionReason: isObj(h.disposition) && typeof h.disposition.reason === "string" ? h.disposition.reason : null,
    dispositionAgeText: decidedEv.known ? decidedEv.ageText : null,
    replan: replanView(h.replan),
    source: typeof h.source === "string" ? h.source : null,
    ageText: obsEv.ageText, quality: obsEv.quality, clockAnomaly: obsEv.clockAnomaly,
    minAgeS: obsEv.minAgeS, estimatedAgeS: obsEv.estimatedAgeS, delayNoteS: obsEv.delayNoteS,
    uncertaintyM: unc,
    uncertaintyText: unc != null ? `±${Math.round(unc)} m (95 %)` : "Uncertainty not reported",
    geometry: validGeometry(h.geometry),
    exclusion: exclusion && exclusion.type === "polygon" ? exclusion : null,
    stale: listStale || obsEv.stale, uncertain: !listStale && obsEv.uncertain,
  };
}

/** Hazards of one companion, one entry per hazard id (highest revision wins), newest first —
 *  ranked by the honest GUARANTEED LOWER BOUND (never the delivery-delay-inclusive estimate,
 *  which is display-only), unknown-age last. */
export function hazardsOf(c) {
  const byId = new Map();
  for (const h of Array.isArray(c.hazards) ? c.hazards : []) {
    if (!isObj(h) || typeof h.hazard_id !== "string") continue;
    const prev = byId.get(h.hazard_id);
    if (!prev || (num(h.revision) ?? -1) > (num(prev.revision) ?? -1)) byId.set(h.hazard_id, h);
  }
  const ageOf = (h) => evidenceView(h.observed).minAgeS;
  return [...byId.values()].sort((a, b) => {
    const x = ageOf(a), y = ageOf(b);
    if (x == null) return 1;
    if (y == null) return -1;
    return x - y;
  });
}

/** Everything the map draws for companions, keyed `${parentId}::${companionId}` — never shared. */
export function companionMapPlan(fleet) {
  const out = [];
  const seen = new Set();
  for (const v of Array.isArray(fleet) ? fleet : []) {
    for (const c of companionsOf(v)) {
      const key = `${v.id}::${c.companion_id}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        key, parentId: v.id, parentName: v.name || "Scout", companionId: c.companion_id,
        name: c.display_name || c.companion_id, type: c.vehicle_type || "UAV",
        status: companionStatus(c, v), uav: positionView(c, v),
        hazards: hazardsOf(c).map((h) => hazardView(h, c, v)).filter((hv) => hv.geometry || hv.exclusion),
      });
    }
  }
  return out;
}

/** What makes a companion's map layer need a rebuild. Ages/age-text are deliberately excluded
 *  (tooltips read them live), so a layer is not torn down every poll just because time passed —
 *  only a change in DRAWN state (position, staleness, hazard membership/classification) rebuilds it. */
export function planSignature(entry) {
  return JSON.stringify({
    s: entry.status.state,
    u: entry.uav && [entry.uav.lat, entry.uav.lng, entry.uav.headingDeg, entry.uav.stale],
    h: entry.hazards.map((h) => [h.id, h.revision, h.cls, h.stale, h.uncertaintyM, h.geometry, h.exclusion]),
  });
}
