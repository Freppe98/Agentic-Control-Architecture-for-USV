// companion.test.mjs — Scout's UAV companion (companion-v2) on the operator side:
// operator/lib/companion.js (view policy), components/CompanionPanel.js, the VehicleDock chip,
// and the Map page's wiring (isolation, cleanup, no command path, freshness model).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  companionsOf, assignedCompanions, companionStatus, companionTab, updateLinkHistory, positionView,
  hazardView, hazardsOf, replanView, companionMapPlan, planSignature, fmtEvidenceAge,
  evidenceView, EVIDENCE_STALE_S, POSITION_FRESH_S, REPORTING_DELAY_NOTABLE_S,
} from "../operator/lib/companion.js";
import { CompanionPanel, CompanionTab } from "../operator/components/CompanionPanel.js";
import { vehicleRow, vehicleRows } from "../operator/components/VehicleDock.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const read = (...p) => readFileSync(join(HERE, "..", ...p), "utf-8");
const MAP_SRC = read("operator", "pages", "Map.js");
const PANEL_SRC = read("operator", "components", "CompanionPanel.js");
const LIB_SRC = read("operator", "lib", "companion.js");

// A "LOWER_BOUND"-quality freshness object, as the backend publishes it. `min` is the
// guaranteed lower bound; `delay` is the reporting-delay estimate (defaults to 0 — a healthy
// link, DISPLAY ONLY — see lib/companion.js's evidenceView for why it never drives staleness).
function fresh(min, delay = 0, { anomaly = false, observedAt = 1000 } = {}) {
  return {
    observed_at: observedAt, min_age_s: min, reporting_delay_s: delay,
    estimated_age_s: min + delay, freshness_quality: "LOWER_BOUND", clock_anomaly: anomaly,
  };
}
const unknownFresh = { observed_at: 1000, min_age_s: null, reporting_delay_s: null,
                       estimated_age_s: null, freshness_quality: "UNKNOWN", clock_anomaly: false };

function uav(over = {}) {
  return {
    companion_id: "uav-1", vehicle_type: "UAV", display_name: "UAV-1", parent_id: 2,
    assignment_state: "ASSIGNED", status_age_s: 1,
    link: { state: "CONNECTED", last_peer_contact: fresh(0.4) },
    activity: { state: "INSPECTING", task_id: "insp-1", route_revision: 3 },
    position: { lat: 56.7003, lng: 13.0035, heading_deg: 45, altitude_m: 25, altitude_ref: "AGL",
               ...fresh(0.5) },
    battery: { remaining_pct: 71, ...fresh(0.5) }, inspection: null,
    hazards_reported: true, hazards_age_s: 1, hazards: [], ...over,
  };
}
const block = (items) => ({ schema: "companion-v2", reported: true,
                            session: { id: "s1", seq: 1, age_s: 1 }, items });
const scout = (items, over = {}) => ({ id: 2, name: "Scout", comm_state: "CONNECTED", companions: block(items), ...over });
const SAR = { id: 3, name: "SAR-001", comm_state: "CONNECTED",
             companions: { schema: "companion-v2", reported: false, session: null, items: [] } };
const point = { type: "point", lat: 56.7006, lng: 13.0042 };
const square = { type: "polygon", ring_latlng: [[56.7, 13.0], [56.7, 13.001], [56.701, 13.001], [56.701, 13.0]] };
function hz(over = {}) {
  return {
    hazard_id: "hz-buoy-1", revision: 1, source: "uav-1:eo_camera", kind: "BUOY",
    observed: fresh(12), geometry: point, uncertainty_m: 8, exclusion: square,
    disposition: { state: "REPORTED", reason: null, decided: null }, replan: null, ...over,
  };
}

// ---- backward compatibility & assignment ----------------------------------------------------
test("a legacy row, a cleared block and an unassigned companion all show the grey 'no companion' tab — never a fabricated identity", () => {
  assert.deepEqual(companionsOf({ id: 2, name: "Scout", comm_state: "CONNECTED" }), []);
  const noCmp = (v) => { const t = companionTab(v); assert.equal(t.level, "grey"); assert.equal(t.companionId, null); assert.equal(t.aria, "No companion assigned"); };
  noCmp({ id: 2, comm_state: "CONNECTED" });
  noCmp(scout([]));
  noCmp(scout([uav({ assignment_state: "UNASSIGNED" })]));
  noCmp(scout([uav({ assignment_state: "UNKNOWN" })]));
  // The tab component never renders "" (unlike the old opt-out chip) — every row keeps the same
  // layout, grey or not — and it is exactly one <button>, never nested in another.
  const html = CompanionTab(scout([]), new Map(), false);
  assert.equal((html.match(/<button/g) || []).length, 1);
});

// ---- isolation ---------------------------------------------------------------------------------
test("an item naming another parent is never shown on this row", () => {
  const leaked = scout([uav({ parent_id: 3 })]);
  assert.deepEqual(companionsOf(leaked), []);
  assert.equal(companionTab(leaked).level, "grey");
  assert.equal(companionsOf(SAR).length, 0);
});

test("map plan keys are namespaced by parent — the same companion id never merges", () => {
  const sarWith = { ...SAR, companions: block([uav({ parent_id: 3, link: { state: "LOST", last_peer_contact: fresh(50) } })]) };
  const plan = companionMapPlan([scout([uav()]), sarWith]);
  assert.deepEqual(plan.map((e) => e.key), ["2::uav-1", "3::uav-1"]);
  assert.equal(plan[0].status.state, "active");
  assert.equal(plan[1].status.state, "lost");
  assert.deepEqual(companionMapPlan([SAR]), []);
});

// ---- companionStatus (activity/currency — the CARD's states, independent of the tab's link colour) --
test("companionStatus distinguishes active / idle / lost / unknown with text, not colour alone", () => {
  const st = (c, v = {}) => companionStatus(c, scout([c], v));
  assert.equal(st(uav()).state, "active");
  assert.equal(st(uav()).label, "UAV active");
  assert.match(st(uav()).reason, /UAV-1: Inspecting \(reported by Scout\)/);
  assert.equal(st(uav({ activity: { state: "IDLE" } })).state, "idle");
  assert.equal(st(uav({ activity: { state: "LANDED" } })).state, "idle");
  assert.equal(st(uav({ link: { state: "LOST", last_peer_contact: fresh(30) } })).state, "lost");
  assert.equal(st(uav({ link: { state: "NEVER_CONNECTED", last_peer_contact: null }, activity: null })).state, "unknown");
  assert.equal(st(uav({ activity: null })).state, "unknown", "connected but activity not reported");
  assert.equal(st(uav({ link: {} })).state, "unknown");
});

test("stale covers every way the evidence is PROVEN to stop being current", () => {
  const st = (c, v = {}) => companionStatus(c, scout([c], v)).state;
  assert.equal(st(uav(), { comm_state: "PARTITIONED" }), "stale", "Scout itself not current");
  assert.equal(st(uav(), { comm_state: "DISCONNECTED" }), "stale");
  assert.equal(st(uav({ status_age_s: EVIDENCE_STALE_S + 5 })), "stale", "Scout omitted the block");
  assert.equal(st(uav({ status_age_s: null })), "stale");
  assert.equal(st(uav({ link: { state: "CONNECTED", last_peer_contact: fresh(40) } })), "stale", "PROVEN old peer contact (min_age_s alone crosses the threshold)");
  assert.equal(st(uav({ link: { state: "CONNECTED", last_peer_contact: null } })), "stale");
  assert.equal(st(uav({ link: { state: "DEGRADED", last_peer_contact: fresh(1) } })), "stale");
  // Unknown-quality evidence (no envelope timestamp) reads the same as proven-stale — neither
  // may ever be read as confirming reachability.
  assert.equal(st(uav({ link: { state: "CONNECTED", last_peer_contact: unknownFresh } })), "stale");
});

test("a small lower-bound age with a notable reporting delay is UNCERTAIN, never forced stale", () => {
  // THE counterexample this rewrite exists for: a small min_age_s does not prove freshness, but
  // neither may the unvalidated delay estimate be used to assert staleness either — this reads
  // as UNCERTAIN and falls through to Scout's own activity claim (status_age_s is unaffected by
  // any of this: it never compares Scout's clock to anything).
  const st = (c, v = {}) => companionStatus(c, scout([c], v));
  const uncertainContact = uav({ link: { state: "CONNECTED", last_peer_contact: fresh(1, 20) } });
  assert.equal(st(uncertainContact).state, "active",
    "reporting_delay_s is display-only and must never force a staleness verdict");
});

// ---- companionTab / updateLinkHistory: the dock TAB's green/yellow/red/grey link colour --------
// Distinct from companionStatus above on purpose: a tab never reflects activity, only what Scout
// currently reports about the RADIO LINK, plus this session's own "has it ever connected" evidence.
test("never connected reads grey, with an honest tooltip, no red or yellow", () => {
  const v = scout([uav({ link: { state: "NEVER_CONNECTED", last_peer_contact: null } })]);
  const t = companionTab(v);
  assert.equal(t.level, "grey");
  assert.match(t.title, /has not heard from it yet/);
});

test("a CONNECTED report reads green", () => {
  const t = companionTab(scout([uav()]));
  assert.equal(t.level, "green");
  assert.equal(t.aria, "UAV companion — reported connected via Scout");
});

test("a DEGRADED report reads yellow, distinct from both connected and lost", () => {
  const t = companionTab(scout([uav({ link: { state: "DEGRADED", last_peer_contact: fresh(1) } })]));
  assert.equal(t.level, "yellow");
  assert.equal(t.aria, "UAV companion — link degraded");
});

test("LOST after this session observed CONNECTED/DEGRADED reads red — 'previously connected'", () => {
  const key = "2::uav-1";
  let hist = updateLinkHistory(new Map(), [scout([uav()])]);                 // CONNECTED seen once
  assert.equal(hist.get(key), true);
  const lostAfter = scout([uav({ link: { state: "LOST", last_peer_contact: fresh(50) } })]);
  hist = updateLinkHistory(hist, [lostAfter]);                               // LOST does not erase history
  const t = companionTab(lostAfter, hist);
  assert.equal(t.level, "red");
  assert.equal(t.aria, "UAV companion — connection lost");
  assert.match(t.title, /previously connected/);
  // DEGRADED counts as connection evidence too (a degraded link is still an established one).
  let hist2 = updateLinkHistory(new Map(), [scout([uav({ link: { state: "DEGRADED", last_peer_contact: fresh(1) } })])]);
  assert.equal(hist2.get(key), true);
});

test("a first-ever LOST report with NO prior connection evidence stays grey, never red", () => {
  const lost = scout([uav({ link: { state: "LOST", last_peer_contact: fresh(5) } })]);
  const hist = updateLinkHistory(new Map(), [lost]);          // this session has NEVER seen it connected
  assert.equal(hist.get("2::uav-1"), false);
  const t = companionTab(lost, hist);
  assert.equal(t.level, "grey");
  assert.match(t.title, /no prior connection has been observed/);
  // Also true with no history object supplied at all (undefined — never treated as false evidence
  // of loss, but also never treated as proof of a prior connection).
  assert.equal(companionTab(lost).level, "grey");
});

test("Scout itself going stale reads grey/unavailable — never inferred as a UAV link failure", () => {
  // A CONNECTED companion whose PARENT (Scout) is no longer current: the tab must not keep
  // showing green (that would assert liveness Scout can no longer vouch for), and must not turn
  // red either (no UAV-side evidence of loss exists at all here) — only grey/unknown.
  const stale = scout([uav()], { comm_state: "DISCONNECTED" });
  const hist = updateLinkHistory(new Map(), [scout([uav()])]);   // was connected while Scout was live
  const t = companionTab(stale, hist);
  assert.equal(t.level, "grey");
  assert.equal(t.aria, "Companion status unavailable");
  assert.match(t.title, /not in contact/);
  assert.match(t.title, /Connected/, "the last reported condition is retained in the tooltip");
  // Same for a merely-stale status block (Scout itself current, but hasn't refreshed this
  // companion recently).
  const staleReport = scout([uav({ status_age_s: EVIDENCE_STALE_S + 5 })]);
  assert.equal(companionTab(staleReport, hist).level, "grey");
});

test("reconnection restores the appropriate reported state", () => {
  let hist = new Map();
  const never = scout([uav({ link: { state: "NEVER_CONNECTED", last_peer_contact: null } })]);
  hist = updateLinkHistory(hist, [never]);
  assert.equal(companionTab(never, hist).level, "grey");
  const connected = scout([uav()]);
  hist = updateLinkHistory(hist, [connected]);
  assert.equal(companionTab(connected, hist).level, "green");
  const lost = scout([uav({ link: { state: "LOST", last_peer_contact: fresh(10) } })]);
  hist = updateLinkHistory(hist, [lost]);
  assert.equal(companionTab(lost, hist).level, "red", "history from the earlier CONNECTED report survives");
  const reconnected = scout([uav()]);
  hist = updateLinkHistory(hist, [reconnected]);
  assert.equal(companionTab(reconnected, hist).level, "green");
});

test("unassignment returns to grey, and a replacement companion never inherits the old one's history", () => {
  let hist = updateLinkHistory(new Map(), [scout([uav()])]);   // uav-1 CONNECTED
  assert.equal(hist.get("2::uav-1"), true);
  const unassigned = scout([uav({ assignment_state: "UNASSIGNED" })]);
  hist = updateLinkHistory(hist, [unassigned]);
  assert.equal(companionTab(unassigned, hist).level, "grey");
  assert.equal(hist.has("2::uav-1"), false, "an unassigned key is pruned, not just ignored");
  // A DIFFERENT companion_id takes over the same parent, immediately reported LOST: it must read
  // grey (no evidence for THIS assignment), never red from uav-1's old history.
  const replacement = scout([uav({ companion_id: "uav-2", link: { state: "LOST", last_peer_contact: fresh(5) } })]);
  hist = updateLinkHistory(hist, [replacement]);
  assert.equal(companionTab(replacement, hist).level, "grey");
  assert.equal(hist.has("2::uav-1"), false);
});

test("a snapshot replay cannot restore a companion that is no longer in the fleet payload", () => {
  let hist = updateLinkHistory(new Map(), [scout([uav()])]);
  assert.equal(hist.get("2::uav-1"), true);
  hist = updateLinkHistory(hist, [scout([])]);                 // Scout now reports items: [] (§8)
  assert.equal(hist.has("2::uav-1"), false);
  // A later poll that (incorrectly) replayed the old CONNECTED item would simply re-establish
  // fresh evidence from what it actually contains — it cannot "restore" anything on its own; the
  // point is the ABSENT report cannot be undone by anything other than a new report.
  assert.equal(companionTab(scout([]), hist).level, "grey");
});

test("when Scout is silent its last CONNECTED claim is never presented as current", () => {
  const s = companionStatus(uav(), { id: 2, name: "Scout", comm_state: "DISCONNECTED" });
  assert.equal(s.current, false);
  assert.equal(s.state, "stale");
  assert.equal(s.label, "UAV last known");
  assert.match(s.reason, /Scout is not in contact/);
  const html = CompanionPanel(scout([uav()], { comm_state: "DISCONNECTED" }));
  assert.match(html, /last reported Connected/);
  assert.match(html, /LAST KNOWN/);
});

test("more than one assigned companion still resolves to ONE deterministic tab and card — never a random pick", () => {
  const v = scout([uav(), uav({ companion_id: "uav-2" })]);
  assert.equal(assignedCompanions(v).length, 2);
  const t = companionTab(v);
  assert.equal(t.companionId, "uav-1", "always Scout's FIRST reported item — never re-picked as reports arrive");
  assert.equal((CompanionTab(v, new Map(), false).match(/<button/g) || []).length, 1);
  assert.match(CompanionPanel(v), /uav-1|UAV-1/, "the card shows the SAME companion the tab identifies");
  assert.doesNotMatch(CompanionPanel(v), /uav-2/);
  assert.equal(assignedCompanions(scout([uav(), uav({ companion_id: "uav-2", assignment_state: "UNASSIGNED" })])).length, 1);
});

// ---- evidenceView: STALE / UNCERTAIN / UNKNOWN — never an asserted "FRESH" --------------------
test("evidenceView never asserts fresh, and only min_age_s may ever assert stale", () => {
  assert.equal(evidenceView(null).state, "UNKNOWN");
  // UNKNOWN gets the SAME (safe) "cannot be presented as current" boolean as PROVEN-stale — not
  // knowing the age at all is, if anything, a weaker position than a known lower bound.
  assert.equal(evidenceView(null).stale, true);
  assert.equal(evidenceView(null).uncertain, false, "UNKNOWN is a distinct state from UNCERTAIN");
  assert.equal(evidenceView(null).quality, "UNKNOWN");
  assert.equal(evidenceView(unknownFresh).state, "UNKNOWN");
  assert.equal(evidenceView(unknownFresh).ageText, "timing unknown");

  // Small min_age, small/no delay: UNCERTAIN, phrased as a lower bound — never a bare, falsely
  // precise "Ns ago" that would read as a confirmed reading.
  const small = evidenceView(fresh(2, 0.5), EVIDENCE_STALE_S);
  assert.equal(small.state, "UNCERTAIN");
  assert.equal(small.stale, false);
  assert.equal(small.uncertain, true);
  assert.equal(small.ageText, "at least 2 s ago");

  // Small min_age, NOTABLE delay (the "30s transit delay" bug this model exists to fix): the
  // delay must be SURFACED in the text, but — critically — must NOT flip this to stale. Neither
  // reporting_delay_s nor estimated_age_s may ever drive the stale/not-stale verdict.
  const delayed = evidenceView(fresh(0.1, 30), EVIDENCE_STALE_S);
  assert.equal(delayed.state, "UNCERTAIN",
    "an unvalidated 30s delay estimate must not itself prove staleness");
  assert.equal(delayed.stale, false);
  assert.match(delayed.ageText, /at least <1 s ago \(delivery delay ~30 s\)/);
  assert.equal(delayed.minAgeS, 0.1);
  assert.equal(delayed.estimatedAgeS, 30.1, "still published, for the operator's own judgement");
  assert.equal(delayed.delayNoteS, 30);

  // A LARGE min_age_s alone — the ONLY thing allowed to assert STALE, exactly because the true
  // age can only be >= min_age_s.
  const definitelyStale = evidenceView(fresh(EVIDENCE_STALE_S + 1, 0), EVIDENCE_STALE_S);
  assert.equal(definitelyStale.state, "STALE");
  assert.equal(definitelyStale.stale, true);
  assert.equal(definitelyStale.uncertain, false);
  assert.match(definitelyStale.ageText, /^at least /);

  // A delay at/under the notable threshold is folded into the displayed number without a
  // separate callout, but the phrasing STILL never claims more than a lower bound.
  const mild = evidenceView(fresh(1, REPORTING_DELAY_NOTABLE_S), EVIDENCE_STALE_S);
  assert.equal(mild.delayNoteS, null);
  assert.equal(mild.ageText, "at least 1 s ago");
  assert.doesNotMatch(mild.ageText, /delivery delay/);

  // clock_anomaly is surfaced independent of the state.
  const anomaly = evidenceView(fresh(1, 0, { anomaly: true }), EVIDENCE_STALE_S);
  assert.equal(anomaly.clockAnomaly, true);
  assert.equal(anomaly.state, "UNCERTAIN");
});

test("Scout's clock ahead PLUS a matching delivery delay must not read as fresh or stale-proof", () => {
  // THE exact counterexample from the task: the two effects cancel in reporting_delay_s, so it
  // reads near zero even though the observation is genuinely much older. min_age_s only sees
  // the small legitimate same-clock delta and cannot detect this either — so the correct,
  // and ONLY honest, verdict is UNCERTAIN, never fresh and never (falsely) proven stale.
  const counterexample = evidenceView(fresh(5.0, 0.0), EVIDENCE_STALE_S);
  assert.equal(counterexample.state, "UNCERTAIN");
  assert.equal(counterexample.stale, false);
  assert.equal(counterexample.ageText, "at least 5 s ago");
});

test("evidence ages format without inventing precision", () => {
  assert.equal(fmtEvidenceAge(null), null);
  assert.equal(fmtEvidenceAge(0.4), "<1 s");
  assert.equal(fmtEvidenceAge(12.4), "12 s");
  assert.equal(fmtEvidenceAge(600), "10 min");
});

// ---- position freshness ------------------------------------------------------------------------
test("a UAV position with a PROVEN old, unknown-timing, or PARTITIONED-parent fix draws stale", () => {
  const v = scout([]);
  assert.equal(positionView(uav(), v).stale, false);
  assert.equal(positionView(uav({ position: { ...uav().position, ...fresh(POSITION_FRESH_S + 1) } }), v).stale, true);
  assert.equal(positionView(uav({ position: { ...uav().position, ...unknownFresh } }), v).stale, true);
  assert.equal(positionView(uav(), { ...v, comm_state: "PARTITIONED" }).stale, true);
  assert.equal(positionView(uav({ position: null }), v), null);
  assert.equal(positionView(uav({ position: { lat: 95, lng: 13, ...fresh(1) } }), v), null);
  assert.equal(positionView(uav({ position: { ...uav().position, altitude_ref: null } }), v).altText, null);
  assert.equal(positionView(uav(), v).altText, "25 m AGL");
  assert.equal(positionView(uav(), v).observedAt, 1000);
});

test("a small min_age with a big delivery delay is UNCERTAIN, not stale — never presented as a confirmed current fix", () => {
  const v = scout([]);
  const p = positionView(uav({ position: { ...uav().position, ...fresh(0.2, 30) } }), v);
  assert.equal(p.stale, false, "the unvalidated delay estimate must not force stale");
  assert.equal(p.uncertain, true, "but it is NOT proof of a current fix either");
  assert.match(p.ageText, /at least <1 s ago \(delivery delay ~30 s\)/);
  // A parent that is not currently connected still forces stale regardless of the evidence's
  // own state — Scout↔operator freshness and UAV evidence freshness are independent axes.
  assert.equal(positionView(uav({ position: { ...uav().position, ...fresh(0.2, 30) } }),
    { ...v, comm_state: "DISCONNECTED" }).stale, true);
});

// ---- hazards -----------------------------------------------------------------------------------
test("only an explicit ACCEPTED disposition reads as an accepted exclusion", () => {
  const v = scout([]), c = uav();
  const cls = (state) => hazardView(hz({ disposition: state === undefined ? undefined : { state } }), c, v).cls;
  assert.equal(cls("REPORTED"), "proposed");
  assert.equal(cls("UNDER_REVIEW"), "proposed");
  assert.equal(cls("UNKNOWN"), "proposed");
  assert.equal(cls("ACCEPTED_MAYBE"), "proposed");
  assert.equal(cls(undefined), "proposed");
  assert.equal(cls("ACCEPTED"), "accepted");
  assert.equal(cls("REJECTED"), "dismissed");
  assert.equal(cls("EXPIRED"), "dismissed");
  assert.equal(hazardView(hz({ disposition: undefined }), c, v).dispositionText, "Scout response not reported");
});

test("replanning outcomes stay distinct and a missing one stays unknown", () => {
  assert.equal(replanView(null).text, "Replanning: not reported");
  assert.equal(replanView(null).ageText, null);
  assert.equal(replanView(undefined).state, null);
  assert.equal(replanView({ state: "IN_PROGRESS" }).text, "Replanning in progress");
  assert.equal(replanView({ state: "PENDING" }).tone, "pending");
  assert.equal(replanView({ state: "VERIFIED", mission_revision: 4 }).text, "Revised mission verified by Scout (rev 4)");
  assert.equal(replanView({ state: "FAILED" }).tone, "warn");
  assert.equal(replanView({ state: "BLOCKED" }).text, "Replanning blocked");
  assert.equal(replanView({ state: "DONE" }).text, "Replanning state not recognised");
  const withTime = replanView({ state: "VERIFIED", mission_revision: 4, updated: fresh(3) });
  assert.equal(withTime.ageText, "at least 3 s ago");
});

test("hazards go stale with Scout, with an omitted block, or with an unrefreshed hazard list", () => {
  const c = uav({ hazards: [hz()] });
  assert.equal(hazardView(hz(), c, scout([])).stale, false);
  assert.equal(hazardView(hz(), c, scout([], { comm_state: "DISCONNECTED" })).stale, true);
  assert.equal(hazardView(hz(), uav({ hazards_age_s: 40 }), scout([])).stale, true);
  assert.equal(hazardView(hz(), uav({ status_age_s: 40 }), scout([])).stale, true);
  // The hazard's OWN PROVEN-old observation also feeds staleness, independent of the list check.
  assert.equal(hazardView(hz({ observed: fresh(EVIDENCE_STALE_S + 1) }), c, scout([])).stale, true);
  assert.equal(hazardView(hz({ observed: unknownFresh }), c, scout([])).stale, true);
  // A small min_age with an unvalidated delivery-delay estimate is UNCERTAIN, not stale — this
  // is the same counterexample as positionView's, applied to a hazard observation.
  const uncertain = hazardView(hz({ observed: fresh(0.1, 30) }), c, scout([]));
  assert.equal(uncertain.stale, false);
  assert.equal(uncertain.uncertain, true);
});

test("repeated hazard ids render once, at their highest revision", () => {
  const c = uav({ hazards: [hz({ revision: 2 }), hz({ revision: 3 }), hz({ revision: 1 }),
                            hz({ hazard_id: "hz-2", observed: fresh(3) })] });
  const list = hazardsOf(c);
  assert.deepEqual(list.map((h) => h.hazard_id).sort(), ["hz-2", "hz-buoy-1"]);
  assert.equal(list.find((h) => h.hazard_id === "hz-buoy-1").revision, 3, "highest revision wins");
  assert.equal(list.find((h) => h.hazard_id === "hz-2").revision, 1);
  const plan = companionMapPlan([scout([c])]);
  assert.equal(plan[0].hazards.length, 2);
});

test("malformed geometry is not drawn", () => {
  const v = scout([]);
  const bad = hazardView(hz({ geometry: { type: "polygon", ring_latlng: [[1, 2], [3, "x"], [5, 6]] }, exclusion: null }), uav(), v);
  assert.equal(bad.geometry, null);
  const plan = companionMapPlan([scout([uav({ hazards: [hz({ geometry: null, exclusion: null })] })])]);
  assert.equal(plan[0].hazards.length, 0);
});

test("layer signature ignores the passage of time but not a state change", () => {
  const a = companionMapPlan([scout([uav({ hazards: [hz()] })])])[0];
  // Ages moved (12s -> 14s, 0.5s -> 2s) but stayed on the SAME side of every staleness
  // threshold, so nothing DRAWN actually changed.
  const b = companionMapPlan([scout([uav({ hazards: [hz({ observed: fresh(14) })],
    position: { ...uav().position, ...fresh(2) } })])])[0];
  assert.equal(planSignature(a), planSignature(b));
  const c = companionMapPlan([scout([uav({ hazards: [hz({ revision: 2, disposition: { state: "ACCEPTED" } })] })])])[0];
  assert.notEqual(planSignature(a), planSignature(c));
  const d = companionMapPlan([scout([uav({ hazards: [hz()] })], { comm_state: "DISCONNECTED" })])[0];
  assert.notEqual(planSignature(a), planSignature(d));
});

// ---- panel -------------------------------------------------------------------------------------
test("panel shows the reported facts, 'not reported' for the rest, and no controls", () => {
  const html = CompanionPanel(scout([uav({ battery: null, activity: null, hazards_reported: false })]));
  assert.match(html, /via Scout/);
  assert.match(html, /Battery<\/span><span class="v"><span class="no-telem-val">—<span class="no-telem-tag">not reported/);
  assert.match(html, /Activity<\/span><span class="v"><span class="no-telem-val">/);
  assert.match(html, /Latest hazard<\/span><span class="v"><span class="no-telem-val">/);
  assert.doesNotMatch(html, /Inspection/, "progress only when actually reported");
  assert.equal((html.match(/<button/g) || []).length, 1, "the only button closes the panel");
  assert.match(html, /data-cmp-close/);
  assert.match(html, /not confirmed safe/);
  assert.match(html, /Scout's decision alone/);
});

test("panel renders inspection progress only when reported, and the latest hazard with Scout's response", () => {
  const c = uav({ inspection: { progress_pct: 40, ...fresh(1) },
                  hazards: [hz({ revision: 3, disposition: { state: "ACCEPTED" },
                                 replan: { state: "VERIFIED", mission_revision: 4 } })] });
  const html = CompanionPanel(scout([c]));
  assert.match(html, /Inspection<\/span><span class="v">40 %/);
  assert.match(html, /Accepted by Scout as exclusion/);
  assert.match(html, /Revised mission verified by Scout \(rev 4\)/);
  assert.match(html, /25 m AGL/);
  assert.match(html, /hz-accepted/);
});

test("panel surfaces a notable delivery delay and an unknown-timing note, never a false fresh", () => {
  const delayed = uav({ position: { ...uav().position, ...fresh(0.2, 30) } });
  const html1 = CompanionPanel(scout([delayed]));
  assert.match(html1, /at least &lt;1 s ago \(delivery delay ~30 s\)/);
  const unknownTiming = uav({ position: { ...uav().position, ...unknownFresh } });
  const html2 = CompanionPanel(scout([unknownTiming]));
  assert.match(html2, /freshness cannot be judged/);
});

test("panel marks uncertain evidence as not-confirmed-current, distinct from LAST KNOWN and from Scout's own claims", () => {
  // Small min_age_s, no proof of freshness — the panel must say so without the stronger
  // PROVEN-stale LAST KNOWN treatment, and without contradicting the still-legitimate ACTIVE
  // chip state (built from status_age_s, which this evidence's uncertainty does not touch).
  const c = uav({
    link: { state: "CONNECTED", last_peer_contact: fresh(1, 20) },
    battery: { remaining_pct: 60, ...fresh(1, 20) },
    position: { ...uav().position, ...fresh(1, 20) },
  });
  const html = CompanionPanel(scout([c]));
  assert.match(html, /UAV · ACTIVE/, "the chip/heading state is unaffected — it rests on status_age_s");
  assert.match(html, /not independently confirmed/, "link caveat");
  assert.match(html, /recency not confirmed/, "battery caveat");
  assert.match(html, /not a confirmed current fix/, "position caveat");
  assert.doesNotMatch(html, /LAST KNOWN/, "uncertain evidence must not get the PROVEN-stale treatment");
});

test("panel shows a clock-mismatch note when the backend flags one", () => {
  const anomalyUav = uav({ battery: { remaining_pct: 50, ...fresh(1, 0, { anomaly: true }) } });
  const html = CompanionPanel(scout([anomalyUav]));
  assert.match(html, /clock mismatch/);
});

test("reported strings are escaped", () => {
  const html = CompanionPanel(scout([uav({ display_name: "<img src=x onerror=alert(1)>" })]));
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /&lt;img/);
});

// ---- dock tab ------------------------------------------------------------------------------
test("the dock tab is opt-in, always rendered when asked for (grey included), and beside only the parent that reports it", () => {
  const s = scout([uav()]);
  assert.doesNotMatch(vehicleRow(s, 2), /cmp-tab/, "other pages' docks are unchanged — no tab, no reserved space");
  const row = vehicleRow(s, 2, { companions: true, companionHistory: new Map() });
  assert.match(row, /class="cmp-tab lvl-green" data-cmp-parent="2"/);
  assert.match(row, /aria-label="UAV companion — reported connected via Scout"/);
  const both = vehicleRows([s, SAR], 3, { companions: true, companionHistory: new Map() });
  assert.equal((both.match(/cmp-tab/g) || []).length, 2, "SAR gets a grey tab too — the right edge stays a consistent width on every row");
  assert.match(vehicleRow(SAR, 3, { companions: true, companionHistory: new Map() }), /cmp-tab lvl-grey/);
});

test("battery sits beside activity on the left; the right edge holds only the companion tab", () => {
  const v = { id: 2, name: "Scout", comm_state: "CONNECTED", status: "IDLE", battery: 90 };
  const row = vehicleRow(v, null, { companions: true, companionHistory: new Map() });
  assert.match(row, /class="sub[^"]*" title="IDLE · 90%">IDLE · <span class="vb[^"]*">90%<\/span><\/span>/);
  assert.doesNotMatch(row, /<span class="mid"/, "the old right-hand battery column is gone");
});

// ---- Map page wiring (static) -----------------------------------------------------------------
test("Map page wires the companion tab/card, layers and cleanup", () => {
  assert.match(MAP_SRC, /vehicleRows\(fleet, selId, \{ companions: true, companionHistory: cmpHistory, companionOpenId: cmpOpenFor \}\)/);
  assert.match(MAP_SRC, /id="cmp-panel"/);
  assert.match(MAP_SRC, /cmpOpenFor === selId/, "panel renders only the selected vehicle");
  assert.match(MAP_SRC, /cmpOpenFor = null;\s*\}\s*\/\/ Snap the map/, "a vehicle switch closes the panel");
  assert.match(MAP_SRC, /clearOriginalOverlay\(\);\s*clearCompanionLayers\(\);/, "page cleanup removes companion layers");
  assert.match(MAP_SRC, /updateCompanionLayers\(\); renderCompanionPanel\(\);/, "every fleet poll reconciles");
  assert.match(MAP_SRC, /if \(!keep\.has\(k\)\)|if \(keep\.has\(k\)\) return;/, "vanished companions are removed");
  assert.match(MAP_SRC, /li-ic hzacc/, "legend explains accepted exclusions");
  assert.match(MAP_SRC, /cmpHistory = updateLinkHistory\(cmpHistory, fleet\)/, "link history is rebuilt every fleet poll");
  assert.match(MAP_SRC, /parentId === cmpOpenFor\) closeCompanion\(\); else openCompanion\(parentId\)/, "pressing the same tab again closes the card");
});

test("the Map page never constructs the global Map — its own page function shadows it", () => {
  // `export function Map(root)` makes `new Map()` inside Map.js recurse into the page itself
  // (stack overflow on load, blank page). Use a plain object there instead.
  assert.doesNotMatch(MAP_SRC, /new Map\(/);
});

test("companion code has no command, network or mission-write path", () => {
  for (const src of [PANEL_SRC, LIB_SRC]) {
    assert.doesNotMatch(src, /\bapi\.|fetch\(|data-cmd|createCommand|missions?\//);
  }
  const start = MAP_SRC.indexOf("// ---- UAV companion (reported via Scout)");
  const end = MAP_SRC.indexOf("// ---- Pixhawk mission (view-only readback");
  assert.ok(start > 0 && end > start);
  assert.doesNotMatch(MAP_SRC.slice(start, end), /\bapi\.|fetch\(|createCommand|sendCommand|data-cmd/);
});
