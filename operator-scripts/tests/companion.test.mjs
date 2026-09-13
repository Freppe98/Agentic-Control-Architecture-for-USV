// companion.test.mjs — Scout's UAV companion (companion-v2) on the operator side:
// operator/lib/companion.js (view policy), components/CompanionPanel.js, the VehicleDock chip,
// and the Map page's wiring (isolation, cleanup, no command path, freshness model).
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  companionsOf, assignedCompanions, companionStatus, companionIndicator, positionView,
  hazardView, hazardsOf, replanView, companionMapPlan, planSignature, fmtEvidenceAge,
  evidenceView, EVIDENCE_STALE_S, POSITION_FRESH_S, REPORTING_DELAY_NOTABLE_S,
} from "../operator/lib/companion.js";
import { CompanionPanel, CompanionChip } from "../operator/components/CompanionPanel.js";
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
test("a legacy row, a cleared block and an unassigned companion show no indicator", () => {
  assert.deepEqual(companionsOf({ id: 2, name: "Scout", comm_state: "CONNECTED" }), []);
  assert.equal(companionIndicator({ id: 2, comm_state: "CONNECTED" }), null);
  assert.equal(companionIndicator(scout([])), null);
  assert.equal(companionIndicator(scout([uav({ assignment_state: "UNASSIGNED" })])), null);
  assert.equal(companionIndicator(scout([uav({ assignment_state: "UNKNOWN" })])), null);
  assert.equal(CompanionChip(scout([])), "");
});

// ---- isolation ---------------------------------------------------------------------------------
test("an item naming another parent is never shown on this row", () => {
  const leaked = scout([uav({ parent_id: 3 })]);
  assert.deepEqual(companionsOf(leaked), []);
  assert.equal(companionIndicator(leaked), null);
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

// ---- indicator states --------------------------------------------------------------------------
test("indicator distinguishes active / idle / lost / unknown with text, not colour alone", () => {
  const st = (c, v = {}) => companionIndicator(scout([c], v));
  assert.equal(st(uav()).state, "active");
  assert.equal(st(uav()).text, "UAV · ACTIVE");
  assert.match(st(uav()).aria, /UAV-1 via Scout: UAV active/);
  assert.equal(st(uav({ activity: { state: "IDLE" } })).state, "idle");
  assert.equal(st(uav({ activity: { state: "LANDED" } })).state, "idle");
  assert.equal(st(uav({ link: { state: "LOST", last_peer_contact: fresh(30) } })).state, "lost");
  assert.equal(st(uav({ link: { state: "NEVER_CONNECTED", last_peer_contact: null }, activity: null })).state, "unknown");
  assert.equal(st(uav({ activity: null })).state, "unknown", "connected but activity not reported");
  assert.equal(st(uav({ link: {} })).state, "unknown");
});

test("stale covers every way the evidence is PROVEN to stop being current", () => {
  const st = (c, v = {}) => companionIndicator(scout([c], v)).state;
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
  const st = (c, v = {}) => companionIndicator(scout([c], v));
  const uncertainContact = uav({ link: { state: "CONNECTED", last_peer_contact: fresh(1, 20) } });
  assert.equal(st(uncertainContact).state, "active",
    "reporting_delay_s is display-only and must never force a staleness verdict");
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

test("more than one assigned companion still renders ONE chip", () => {
  const ind = companionIndicator(scout([uav(), uav({ companion_id: "uav-2" })]));
  assert.equal(ind.more, 1);
  assert.equal((CompanionChip(scout([uav(), uav({ companion_id: "uav-2" })])).match(/<button/g) || []).length, 1);
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

// ---- dock chip ---------------------------------------------------------------------------------
test("the dock chip is opt-in and appears only beside the parent that reports it", () => {
  const s = scout([uav()]);
  assert.doesNotMatch(vehicleRow(s, 2), /cmp-chip/, "other pages' docks are unchanged");
  assert.match(vehicleRow(s, 2, { companions: true }), /class="cmp-chip st-active" data-cmp-parent="2"/);
  assert.match(vehicleRow(s, 2, { companions: true }), /UAV · ACTIVE/);
  const both = vehicleRows([s, SAR], 3, { companions: true });
  assert.equal((both.match(/cmp-chip/g) || []).length, 1);
  assert.doesNotMatch(vehicleRow(SAR, 3, { companions: true }), /cmp-chip/);
});

// ---- Map page wiring (static) -----------------------------------------------------------------
test("Map page wires the companion panel, layers and cleanup", () => {
  assert.match(MAP_SRC, /vehicleRows\(fleet, selId, \{ companions: true \}\)/);
  assert.match(MAP_SRC, /id="cmp-panel"/);
  assert.match(MAP_SRC, /cmpOpenFor === selId/, "panel renders only the selected vehicle");
  assert.match(MAP_SRC, /cmpOpenFor = null;\s*\}\s*\/\/ Snap the map/, "a vehicle switch closes the panel");
  assert.match(MAP_SRC, /clearOriginalOverlay\(\);\s*clearCompanionLayers\(\);/, "page cleanup removes companion layers");
  assert.match(MAP_SRC, /updateCompanionLayers\(\); renderCompanionPanel\(\);/, "every fleet poll reconciles");
  assert.match(MAP_SRC, /if \(!keep\.has\(k\)\)|if \(keep\.has\(k\)\) return;/, "vanished companions are removed");
  assert.match(MAP_SRC, /li-ic hzacc/, "legend explains accepted exclusions");
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
