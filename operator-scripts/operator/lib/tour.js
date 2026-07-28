// tour.js — the guided introduction to the Operator Station.
//
// A spotlight tour: each step routes to the page it talks about, dims everything
// except the element it is describing, and anchors an explanatory popup beside it.
// The cut-out stays interactive (the mask is four rects around the hole, not one
// sheet over it), so the operator can actually try the thing being explained.
//
// Content rule — the same one the rest of this UI follows: describe only what the
// station really does. Nothing here promises a capability the backend does not
// have, and no step performs an action on a vehicle. The tour reads the UI; it
// never commands it.
//
// Persistence is localStorage in THIS BROWSER only (like lib/prefs.js), under its
// own key so that resetting operator preferences does not re-trigger the tour.

const SEEN_KEY = "operator.tour.v1";
const GAP = 14;   // popup ↔ spotlight gap
const EDGE = 12;  // minimum viewport margin
const PAD = 6;    // spotlight padding around the target

/** The six steps the operator asked for, in workflow order. `target` is a list of
 *  selectors tried in order — the first one actually laid out wins, so a step still
 *  anchors sensibly when an optional overlay (e.g. the legend) is absent. No target
 *  at all → a centered card with no cut-out. */
export const TOUR_STEPS = [
  {
    id: "welcome",
    label: "Welcome",
    route: "map",
    title: "Welcome to the Operator Station",
    body: `
      <p>This is the single operator view over the USV fleet. It polls the operator
      backend, which in turn talks to the local agent aboard each vehicle — so every
      number you see here is <b>reported</b>, never simulated. Where a value has not
      been reported, the UI shows <span class="tour-chip">—</span> or
      <span class="tour-chip">NO TELEM</span> instead of a guess.</p>
      <p>This guide walks the six things you need for a sortie: the sidebar, checking
      a USV's status, planning a mission, following it on the map, and bringing the
      vehicle home for pickup.</p>
      <p class="tour-tip">Nothing in this guide sends a command. Reopen it any time
      from the <b>Guide</b> button in the top bar.</p>`,
  },
  {
    id: "sidebar",
    label: "Sidebar",
    route: "map",
    target: [".rail"],
    placement: "right",
    title: "The sidebar — where everything lives",
    body: `
      <p>The icon rail is the only navigation. Hover any icon for its name; the
      current page carries the blue marker on its left edge.</p>
      <ul>
        <li><b>Map</b> — the live picture: vehicle positions, mission overlay, commands.</li>
        <li><b>Fleet</b> — the whole roster as a sortable triage table.</li>
        <li><b>Plan</b> — draw a survey and generate the coverage route.</li>
        <li><b>Mission</b> — the mission record and its command history.</li>
        <li><b>Agent</b> — what the onboard autonomy is doing and why.</li>
        <li><b>Pilot</b> · <b>Vehicle</b> — manual piloting and the full per-vehicle command panel.</li>
        <li><b>Events</b> · <b>Experiment</b> · <b>Configuration</b> — log, trial runs, and station settings.</li>
      </ul>
      <p class="tour-tip">Below the divider, <b>Terminal</b> and <b>Messages</b> are
      engineering tools rather than part of the normal operating flow.</p>`,
  },
  {
    id: "status",
    label: "USV status",
    route: "map",
    target: ["#inspector", ".dock"],
    placement: "left",
    title: "Check the USV before you commit to anything",
    body: `
      <p>Pick a vehicle in the <b>Vehicles</b> dock on the left; this inspector on the
      right is then all about that one USV. Work down it before every sortie:</p>
      <ul>
        <li><b>Comms &amp; last contact</b> — CONN / PART / DISC, and how many seconds
        since the vehicle was last heard from.</li>
        <li><b>Status</b> — armed state and flight mode.</li>
        <li><b>Control authority</b> — who holds the wheel. <b>Take Control</b> requests
        OPERATOR authority; <b>Release Control</b> hands it back to the local agent.</li>
        <li><b>Deployment readiness</b> — the interlocks that must be satisfied first,
        Vehicle Home above all.</li>
        <li><b>Telemetry</b>, <b>comms transitions</b> and <b>recent events</b>.</li>
      </ul>
      <p class="tour-tip">If the link is anything other than CONNECTED, mode and arming
      read <b>UNKNOWN</b> and the command buttons lock. That is deliberate: a reading we
      can no longer confirm is not shown as fact. Authority only counts once the vehicle
      has <b>confirmed</b> it — a pressed button is not control.</p>`,
  },
  {
    id: "planning",
    label: "Mission planning",
    route: "plan",
    target: [".dock", ".map-wrap"],
    placement: "right",
    title: "Plan the survey",
    body: `
      <p>On the <b>Plan</b> page you build the mission in this panel and by clicking on
      the map:</p>
      <ul>
        <li>Draw the <b>survey boundary</b>, then any <b>no-go zones</b>.</li>
        <li>Set <b>approach</b> waypoints (A1→) into the survey and <b>return</b>
        waypoints (→R1) back out. Both optional.</li>
        <li>Drop a <b>planning home</b> and generate the <b>coverage passes</b>; spacing
        and the shoreline offset shape the resulting lawnmower route.</li>
        <li>The action bar along the bottom of the map validates the plan and, when it
        passes, uploads it to the vehicle.</li>
      </ul>
      <p class="tour-tip">Planning home is route geometry only — it does <b>not</b> set
      the Pixhawk HOME_POSITION or the RTL point. Upload is gated: the vehicle must be
      disarmed or in a <b>confirmed LOITER</b>, which is why the workflow is
      AUTO → LOITER → Upload. Planning never starts a mission by itself.</p>`,
  },
  {
    id: "follow",
    label: "Follow on the map",
    route: "map",
    target: ["#legend", ".map-wrap"],
    placement: "right",
    title: "Follow the mission on the map",
    body: `
      <p>Back on the <b>Map</b>, the uploaded mission is drawn over the live picture and
      this legend is the key to it:</p>
      <ul>
        <li>Each USV is a dot coloured by comms with a heading arrow. A <b>dashed ring</b>
        means the position is stale — the link is not current, so that dot may have moved.</li>
        <li>Waypoints are numbered and shaded <b>completed</b> / <b>current</b> /
        <b>upcoming</b>; zoom out and the numbers drop away, leaving the track.</li>
        <li>The <b>progress bar</b> along the bottom of the map gives current waypoint
        and percent complete.</li>
        <li><b>Center on USV</b> / <b>Center on me</b> at the top right follow the vehicle
        or your own device position.</li>
      </ul>
      <p class="tour-tip">A newly read mission shows itself automatically, and re-shows
      after an upload, replan or clear — but if you hide it by hand, routine refreshes
      leave it hidden.</p>`,
  },
  {
    id: "recovery",
    label: "Return &amp; pickup",
    route: "map",
    target: ["#inspector", ".map-wrap"],
    placement: "left",
    title: "Return to home and pickup",
    body: `
      <p>Recovery runs from the <b>Vehicle Commands</b> block in this inspector:</p>
      <ul>
        <li><b>Vehicle Home</b> is the RTL recovery point, set and verified from the
        Pixhawk mission card on this page. Until it is verified, AUTO, RTL and RESUME
        stay locked — hovering a disabled button explains why.</li>
        <li><b>RTL</b> sends the USV back to that verified Home on its own. It asks for
        confirmation first, then <b>queues</b> the command: it is not applied until the
        local agent reports back.</li>
        <li>For the pickup itself, use <b>LOITER</b> — the active anti-drift hold that
        keeps station while you come alongside. <b>HOLD</b> is passive and will drift
        with wind and current.</li>
        <li>Once the vehicle is secured, <b>DISARM</b> to stop the motors.</li>
      </ul>
      <p class="tour-tip">ARM, DISARM, RTL and AUTO are high-risk commands: each one
      needs confirmed OPERATOR authority and its own confirmation. Under RC override the
      physical transmitter wins regardless.</p>`,
  },
];

let rootEl = null, popEl = null, ringEl = null, masks = {}, titleEl = null, bodyEl = null,
    counterEl = null, dotsEl = null, prevBtn = null, nextBtn = null;
let idx = 0, isOpen = false, rafId = null, lastKey = "", routeToken = 0;

/* ---------- persistence ----------
   Same shape as lib/map-view.js: a swappable storage handle so the flag logic is
   unit-testable without a browser. */

let storage = (() => { try { return window.localStorage; } catch (e) { return null; } })();
export function _setStorageForTest(s) { storage = s; }

/** True when the tour has already been completed/dismissed in this browser.
 *  Storage unavailable → true: a station that cannot remember the answer must not
 *  re-open the tour on every single reload. The ? button still works. */
export function tourSeen() {
  try { return storage ? storage.getItem(SEEN_KEY) === "done" : true; } catch (e) { return true; }
}
function markSeen() {
  try { if (storage) storage.setItem(SEEN_KEY, "done"); } catch (e) { /* nothing to do */ }
}
/** Clear the flag so the tour auto-opens again on the next load. */
export function resetTourSeen() {
  try { if (storage) storage.removeItem(SEEN_KEY); } catch (e) { /* nothing to do */ }
}

/* ---------- DOM ---------- */

function ensureDom() {
  if (rootEl) return;
  rootEl = document.createElement("div");
  rootEl.className = "tour-root";
  rootEl.hidden = true;
  rootEl.innerHTML = `
    <div class="tour-mask" data-m="t"></div>
    <div class="tour-mask" data-m="r"></div>
    <div class="tour-mask" data-m="b"></div>
    <div class="tour-mask" data-m="l"></div>
    <div class="tour-ring" hidden></div>
    <div class="tour-pop" role="dialog" aria-modal="false" aria-labelledby="tour-title">
      <div class="tour-arrow"></div>
      <div class="tour-h">
        <span class="tour-kicker">Operator guide</span>
        <span class="tour-count" id="tour-count"></span>
        <button class="tour-x" type="button" title="Close the guide (Esc)" aria-label="Close the guide">✕</button>
      </div>
      <h2 class="tour-title" id="tour-title"></h2>
      <div class="tour-b" id="tour-body"></div>
      <div class="tour-dots" id="tour-dots"></div>
      <div class="tour-f">
        <button class="tour-btn tour-skip" type="button">Skip</button>
        <span class="tour-sp"></span>
        <button class="tour-btn tour-prev" type="button">Back</button>
        <button class="tour-btn tour-primary tour-next" type="button">Next</button>
      </div>
    </div>`;
  document.body.appendChild(rootEl);

  popEl = rootEl.querySelector(".tour-pop");
  ringEl = rootEl.querySelector(".tour-ring");
  titleEl = rootEl.querySelector(".tour-title");
  bodyEl = rootEl.querySelector("#tour-body");
  counterEl = rootEl.querySelector("#tour-count");
  dotsEl = rootEl.querySelector("#tour-dots");
  prevBtn = rootEl.querySelector(".tour-prev");
  nextBtn = rootEl.querySelector(".tour-next");
  rootEl.querySelectorAll(".tour-mask").forEach((m) => { masks[m.dataset.m] = m; });

  rootEl.querySelector(".tour-x").onclick = () => closeTour();
  rootEl.querySelector(".tour-skip").onclick = () => closeTour();
  prevBtn.onclick = () => goto(idx - 1);
  nextBtn.onclick = () => (idx >= TOUR_STEPS.length - 1 ? closeTour() : goto(idx + 1));
  dotsEl.onclick = (e) => {
    const d = e.target.closest("[data-i]");
    if (d) goto(+d.dataset.i);
  };
  // Clicking the dimmed area is a no-op — the tour is dismissed deliberately, via
  // Skip/✕/Esc, so a stray click on the backdrop never loses the operator's place.
  rootEl.querySelectorAll(".tour-mask").forEach((m) => (m.onclick = (e) => e.stopPropagation()));
}

/* ---------- geometry ---------- */

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/** First selector that resolves to a laid-out element, else null. */
function resolveTarget(step) {
  for (const sel of step.target || []) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    if (r.width > 4 && r.height > 4) return el;
  }
  return null;
}

/** Poll for the step's target after a route change; resolves null on timeout so a
 *  missing element degrades to a centered card rather than hanging the tour. */
function waitForTarget(step, token, timeoutMs = 2500) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    (function poll() {
      if (token !== routeToken || !isOpen) return resolve(null);
      const el = resolveTarget(step);
      if (el || Date.now() - t0 > timeoutMs) return resolve(el);
      setTimeout(poll, 60);
    })();
  });
}

/** Candidate popup position on one side of the hole, or null when it does not fit. */
function sidePos(side, r, pw, ph, vw, vh) {
  let x, y;
  if (side === "right") {
    x = r.right + GAP; y = r.top + r.height / 2 - ph / 2;
    if (x + pw > vw - EDGE) return null;
  } else if (side === "left") {
    x = r.left - GAP - pw; y = r.top + r.height / 2 - ph / 2;
    if (x < EDGE) return null;
  } else if (side === "bottom") {
    y = r.bottom + GAP; x = r.left + r.width / 2 - pw / 2;
    if (y + ph > vh - EDGE) return null;
  } else {
    y = r.top - GAP - ph; x = r.left + r.width / 2 - pw / 2;
    if (y < EDGE) return null;
  }
  return { x: clamp(x, EDGE, Math.max(EDGE, vw - EDGE - pw)), y: clamp(y, EDGE, Math.max(EDGE, vh - EDGE - ph)), side };
}

const OPPOSITE = { right: "left", left: "right", top: "bottom", bottom: "top" };

/** Placement policy, pure so it can be unit-tested without a browser: try the step's
 *  preferred side, then its opposite, then the remaining sides; if the target is so
 *  large that nothing fits beside it (a full-height map pane), centre the popup
 *  inside the spotlight rather than shoving it off-screen. */
export function pickPlacement(r, pw, ph, prefer, viewport) {
  const { w: vw, h: vh } = viewport;
  const order = [prefer, OPPOSITE[prefer], "bottom", "top", "right", "left"].filter(Boolean);
  for (const s of order) {
    const p = sidePos(s, r, pw, ph, vw, vh);
    if (p) return p;
  }
  return {
    x: clamp(r.left + r.width / 2 - pw / 2, EDGE, Math.max(EDGE, vw - EDGE - pw)),
    y: clamp(r.top + r.height / 2 - ph / 2, EDGE, Math.max(EDGE, vh - EDGE - ph)),
    side: "center",
  };
}

/** Paint mask, ring and popup for the current step. `el` may be null (centered card). */
function layout(el, prefer) {
  const vw = window.innerWidth, vh = window.innerHeight;
  const pw = popEl.offsetWidth, ph = popEl.offsetHeight;

  if (!el) {
    // No anchor: one full-screen dim, no cut-out, popup centered.
    masks.t.style.cssText = `left:0;top:0;width:${vw}px;height:${vh}px`;
    ["r", "b", "l"].forEach((k) => (masks[k].style.cssText = "width:0;height:0"));
    ringEl.hidden = true;
    popEl.style.left = `${clamp(vw / 2 - pw / 2, EDGE, vw - EDGE - pw)}px`;
    popEl.style.top = `${clamp(vh / 2 - ph / 2, EDGE, vh - EDGE - ph)}px`;
    popEl.dataset.side = "center";
    return;
  }

  const b = el.getBoundingClientRect();
  const r = {
    left: clamp(b.left - PAD, 0, vw), top: clamp(b.top - PAD, 0, vh),
    right: clamp(b.right + PAD, 0, vw), bottom: clamp(b.bottom + PAD, 0, vh),
  };
  r.width = r.right - r.left; r.height = r.bottom - r.top;

  masks.t.style.cssText = `left:0;top:0;width:${vw}px;height:${r.top}px`;
  masks.b.style.cssText = `left:0;top:${r.bottom}px;width:${vw}px;height:${Math.max(0, vh - r.bottom)}px`;
  masks.l.style.cssText = `left:0;top:${r.top}px;width:${r.left}px;height:${r.height}px`;
  masks.r.style.cssText = `left:${r.right}px;top:${r.top}px;width:${Math.max(0, vw - r.right)}px;height:${r.height}px`;

  ringEl.hidden = false;
  ringEl.style.cssText = `left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px`;

  const pos = pickPlacement(r, pw, ph, prefer, { w: vw, h: vh });
  popEl.style.left = `${pos.x}px`;
  popEl.style.top = `${pos.y}px`;
  popEl.dataset.side = pos.side;

  // Point the arrow at the target's centre along the popup's anchored edge.
  const arrow = popEl.querySelector(".tour-arrow");
  if (pos.side === "center") { arrow.style.display = "none"; }
  else {
    arrow.style.display = "";
    if (pos.side === "left" || pos.side === "right") {
      arrow.style.top = `${clamp(r.top + r.height / 2 - pos.y, 16, Math.max(16, ph - 16))}px`;
      arrow.style.left = "";
    } else {
      arrow.style.left = `${clamp(r.left + r.width / 2 - pos.x, 16, Math.max(16, pw - 16))}px`;
      arrow.style.top = "";
    }
  }
}

/** Re-measure on every frame, but only repaint when something actually moved. */
function tick() {
  if (!isOpen) return;
  const step = TOUR_STEPS[idx];
  const el = resolveTarget(step);
  const b = el ? el.getBoundingClientRect() : null;
  const key = `${idx}|${window.innerWidth}x${window.innerHeight}|${b ? `${b.left},${b.top},${b.width},${b.height}` : "none"}|${popEl.offsetHeight}`;
  if (key !== lastKey) { lastKey = key; layout(el, step.placement || "bottom"); }
  rafId = requestAnimationFrame(tick);
}

/* ---------- step rendering ---------- */

function renderStep(step) {
  titleEl.innerHTML = step.title;
  bodyEl.innerHTML = step.body;
  counterEl.textContent = `Step ${idx + 1} of ${TOUR_STEPS.length}`;
  dotsEl.innerHTML = TOUR_STEPS.map((s, i) =>
    `<button class="tour-dot${i === idx ? " on" : ""}" type="button" data-i="${i}" title="${s.label}" aria-label="Step ${i + 1}: ${s.label}"></button>`).join("");
  prevBtn.disabled = idx === 0;
  nextBtn.textContent = idx === TOUR_STEPS.length - 1 ? "Done" : "Next";
  bodyEl.scrollTop = 0;
}

const currentRoute = () => location.hash.replace(/^#\/?/, "") || "map";

async function goto(i) {
  if (!isOpen) return;
  idx = clamp(i, 0, TOUR_STEPS.length - 1);
  const step = TOUR_STEPS[idx];
  renderStep(step);
  lastKey = "";                       // force a repaint even if the rect is unchanged

  const token = ++routeToken;
  if (step.route && currentRoute() !== step.route) {
    // Park the popup centred while the page swaps, so it never points at a stale rect.
    layout(null, "center");
    location.hash = "#/" + step.route;
    await waitForTarget(step, token);
    if (token !== routeToken || !isOpen) return;
  }
  lastKey = "";
}

/* ---------- public API ---------- */

export function openTour(startIndex = 0) {
  ensureDom();
  if (isOpen) { goto(startIndex); return; }
  isOpen = true;
  idx = clamp(startIndex, 0, TOUR_STEPS.length - 1);
  rootEl.hidden = false;
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("hashchange", onHash);
  goto(idx);
  rafId = requestAnimationFrame(tick);
  // Focus the primary action so Tab/Enter work without hunting for the popup.
  setTimeout(() => { try { nextBtn.focus(); } catch (e) { /* noop */ } }, 0);
}

export function closeTour() {
  if (!isOpen) return;
  isOpen = false;
  routeToken++;
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }
  document.removeEventListener("keydown", onKey, true);
  window.removeEventListener("hashchange", onHash);
  rootEl.hidden = true;
  lastKey = "";
  markSeen();
}

export function tourIsOpen() { return isOpen; }

function onKey(e) {
  if (!isOpen) return;
  if (e.key === "Escape") { e.preventDefault(); closeTour(); }
  else if (e.key === "ArrowRight") { e.preventDefault(); if (idx < TOUR_STEPS.length - 1) goto(idx + 1); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); goto(idx - 1); }
}

/** The operator may navigate the rail mid-tour. Don't fight them — just re-anchor
 *  on the new page; a step whose target isn't there falls back to a centred card. */
function onHash() { lastKey = ""; }

/** Create the overlay up front so the first open has nothing to build. */
export function mountTour() { ensureDom(); }
