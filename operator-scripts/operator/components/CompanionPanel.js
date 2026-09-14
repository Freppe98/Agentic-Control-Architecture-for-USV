// CompanionPanel — Scout's UAV companion, rendered for the Map dock. Pure HTML-string helpers
// (like VehicleDock): the page owns the container and wires clicks.
//
// READ-ONLY BY DESIGN. There is no UAV command, connection or flight control anywhere in this
// station — the only button here closes the panel. Everything shown is what Scout reported,
// labelled as such, with the age of its evidence; a field Scout did not report reads
// "not reported", never a default. See lib/companion.js for the state rules, and its
// `evidenceView` for how a delivery delay is surfaced rather than silently ignored.
import { noTelem } from "../lib/ui.js";
import { esc, escAttr } from "../lib/format.js";
import {
  companionTab, assignedCompanions, companionStatus, positionView, hazardsOf, hazardView,
  evidenceView, fmtEvidenceAge, STATE_SHORT, EVIDENCE_STALE_S,
} from "../lib/companion.js";

// A small quadcopter glyph — never a circle (that reads as a USV) — used both on the dock tab
// and, at the same colour, in the card header, so the two are visibly the same fact.
const UAV_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="10" y="10" width="4" height="4" rx="1"/><path d="M10 10 6 6M14 10l4-4M10 14l-4 4M14 14l4 4"/><circle cx="5" cy="5" r="2.1"/><circle cx="19" cy="5" r="2.1"/><circle cx="5" cy="19" r="2.1"/><circle cx="19" cy="19" r="2.1"/></svg>';

/** The compact dock TAB at the row's right edge — ALWAYS rendered (never ""), so every row keeps
 *  the same layout whether or not a companion is assigned (grey = no assignment / no evidence).
 *  Opens/closes `v`'s companion card; `isOpen` only changes the pressed styling, never the tab's
 *  colour, which is companionTab()'s alone. See lib/companion.js companionTab() for the state
 *  rules and updateLinkHistory() for the local "ever connected" evidence this reads. */
export function CompanionTab(v, history, isOpen) {
  const t = companionTab(v, history);
  return `<button type="button" class="cmp-tab lvl-${t.level}${isOpen ? " is-open" : ""}" data-cmp-parent="${escAttr(String(v.id))}" aria-pressed="${isOpen ? "true" : "false"}" aria-label="${escAttr(t.aria)}" title="${escAttr(t.title)}">${UAV_SVG}</button>`;
}

const row = (k, v) => `<div class="pxm-row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
// Plain LOCAL-elapsed ages (status_age_s, hazards_age_s — monotonic, single-clock, no delivery-
// delay ambiguity) still use this. Cross-clock evidence (position/battery/link/hazard observed
// times) instead carries its own ready-made `.ageText` from lib/companion.js's evidenceView.
const ago = (s) => (fmtEvidenceAge(s) == null ? "age unknown" : `${fmtEvidenceAge(s)} ago`);
const lastKnown = (s) => `<span class="cmp-lk">LAST KNOWN · ${esc(ago(s))}</span>`;
const lastKnownEv = (ev) => `<span class="cmp-lk">LAST KNOWN · ${esc(ev.ageText)}</span>`;
// A clock/delivery anomaly is surfaced, never hidden — it means the age numbers above it may be
// understating the truth by more than usual, not that anything is broken.
const clockNote = (ev) => (ev.clockAnomaly
  ? `<div class="cmp-clockwarn" title="This envelope's own timestamp looks like it is from the future relative to the operator's clock — either the two clocks disagree or one of them jumped. Age estimates here may be unreliable beyond the guaranteed lower bound.">⚠ clock mismatch</div>`
  : "");
// A small, non-alarming caveat for UNCERTAIN evidence — a small lower-bound age that does NOT
// prove the reading is current (see lib/companion.js's evidenceView). Deliberately reuses the
// existing muted `.cmp-sub-i` style (already used for "Scout-reported" elsewhere) rather than
// the amber LAST KNOWN treatment, which stays reserved for PROVEN-stale evidence.
const uncertainNote = (text) => `<div class="cmp-sub-i">${esc(text)}</div>`;

function linkRow(s) {
  const text = s.current ? esc(s.linkText) : `last reported ${esc(s.linkText)}`;
  const contact = s.peerEv.known ? `contact ${esc(s.peerEv.ageText)}` : "no contact time reported";
  const caveat = s.current && s.peerEv.uncertain
    ? uncertainNote("Contact evidence is a lower bound only — current reachability is not independently confirmed.")
    : "";
  return row("Link", `${text}<div class="cmp-sub">${contact} · Scout-reported</div>${caveat}${clockNote(s.peerEv)}`);
}

function activityRow(c, s) {
  const a = c.activity;
  if (!a || !a.state) return row("Activity", noTelem());
  const bits = [esc(a.state.replace(/_/g, " ").toLowerCase())];
  if (a.task_id) bits.push(`task ${esc(a.task_id)}`);
  if (a.route_revision != null) bits.push(`route rev ${esc(a.route_revision)}`);
  return row("Activity", bits.join(" · ") + (s.current ? "" : lastKnown(s.statusAgeS)));
}

function batteryRow(c, s) {
  const b = c.battery;
  if (!b || b.remaining_pct == null) return row("Battery", noTelem());
  const ev = evidenceView(b, EVIDENCE_STALE_S);
  const stale = !s.current || ev.stale;
  const uncertain = s.current && ev.uncertain;
  return row("Battery", `${Math.round(b.remaining_pct)} %` +
    (stale ? lastKnownEv(ev) : (uncertain ? uncertainNote("recency not confirmed") : "")) + clockNote(ev));
}

function positionRow(c, v) {
  const p = positionView(c, v);
  if (!p) return row("Position", noTelem());
  const alt = p.altText ? esc(p.altText) : "altitude not reported";
  return row("Position", `${p.lat.toFixed(5)}, ${p.lng.toFixed(5)}` +
    `<div class="cmp-sub">${alt} · observed ${esc(p.ageText)}</div>` +
    (p.stale ? `<span class="cmp-lk">LAST KNOWN</span>`
      : p.uncertain ? uncertainNote("Reported position — not a confirmed current fix.") : "") +
    (p.quality === "UNKNOWN" ? uncertainNote("Envelope carried no timestamp — freshness cannot be judged.") : "") +
    (p.clockAnomaly ? `<div class="cmp-clockwarn" title="This envelope's own timestamp looks like it is from the future relative to the operator's clock.">⚠ clock mismatch</div>` : ""));
}

// Inspection progress ONLY when Scout actually reports it — it is Scout's number, not a coverage
// claim, and it says nothing about whether an inspected area is safe.
function inspectionRow(c) {
  const i = c.inspection;
  if (!i || i.progress_pct == null) return "";
  return row("Inspection", `${Math.round(i.progress_pct)} % <span class="cmp-sub-i">Scout-reported</span>`);
}

function hazardRow(c, v) {
  if (!c.hazards_reported) return row("Latest hazard", noTelem());
  const list = hazardsOf(c);
  if (!list.length) return row("Latest hazard", "None reported");
  const h = hazardView(list[0], c, v);
  const more = list.length > 1 ? ` <span class="cmp-sub-i">+${list.length - 1} more</span>` : "";
  return `<div class="cmp-hz hz-${h.cls}${h.stale ? " is-stale" : ""}">
      <div class="pxm-row"><span class="k">Latest hazard</span><span class="v">${esc(h.kind)}${more}</span></div>
      <div class="cmp-hz-disp">${esc(h.dispositionText)}${h.dispositionAgeText ? ` <span class="cmp-sub-i">(${esc(h.dispositionAgeText)})</span>` : ""}</div>
      <div class="pxm-note ${h.replan.tone}">${esc(h.replan.text)}${h.replan.ageText ? ` <span class="cmp-sub-i">(${esc(h.replan.ageText)})</span>` : ""}</div>
      <div class="cmp-sub">observed ${esc(h.ageText)} · ${esc(h.source || "source not reported")} · ${esc(h.uncertaintyText)}</div>
      ${h.stale ? `<span class="cmp-lk">LAST KNOWN · ${esc(c.hazards_age_s == null ? "age unknown" : ago(c.hazards_age_s))}</span>`
        : h.uncertain ? uncertainNote("Observation age is a lower bound only — not confirmed current.") : ""}
      ${clockNote(h)}
    </div>`;
}

function companionSection(c, v, tab) {
  const s = companionStatus(c, v);
  const name = c.display_name || c.companion_id;
  return `<div class="cmp-item">
      <div class="cmp-name">
        <span class="cmp-tab-dot lvl-${tab.level}" title="${escAttr(tab.title)}">${UAV_SVG}</span>
        <span class="mono">${esc(name)}</span><span class="cmp-via">via ${esc(v.name || "Scout")}</span>
        <span class="pxm-chip cmp-state st-${s.state}" title="${escAttr(s.reason)}">${esc((c.vehicle_type || "UAV") + " · " + STATE_SHORT[s.state])}</span></div>
      ${s.current ? "" : `<div class="pxm-note warn">${esc(s.reason)}</div>`}
      <div class="pxm-grid">
        ${linkRow(s)}
        ${activityRow(c, s)}
        ${batteryRow(c, s)}
        ${positionRow(c, v)}
        ${inspectionRow(c)}
        ${hazardRow(c, v)}
      </div>
    </div>`;
}

/** The dock panel for ONE parent vehicle's companion — always the SAME companion (list[0], the
 *  same deterministic choice companionTab() makes) the tab opened, never every item Scout has
 *  ever mentioned. An item Scout reports but has UNASSIGNED is deliberately excluded here — the
 *  same rule the tab itself follows (assignedCompanions), so pressing a grey "no assignment" tab
 *  can never open a card that still shows a UAV identity. */
export function CompanionPanel(v, history) {
  const list = v ? assignedCompanions(v) : [];
  const head = `<div class="pxm-h"><span class="lbl">Companion</span>
      <button type="button" class="cmp-close" data-cmp-close aria-label="Close companion details" title="Close">×</button></div>`;
  if (!list.length) {
    return head + `<div class="no-telem-box">No companion assigned to ${esc((v && v.name) || "this vehicle")}</div>`;
  }
  const c = list[0];
  const tab = v ? companionTab(v, history) : null;
  return head + companionSection(c, v, tab || { level: "grey", title: "" }) +
    `<div class="pxm-note">Observations reported through ${esc(v.name || "Scout")}. They never change the
      operator's mission or plan; an inspected area is not confirmed safe, and a hazard leaving this
      panel means Scout stopped reporting it — it does NOT mean Scout removed any exclusion from its
      own onboard plan, which is Scout's decision alone.</div>`;
}
