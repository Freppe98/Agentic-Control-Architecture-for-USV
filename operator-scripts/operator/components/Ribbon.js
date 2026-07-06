// Ribbon(opts) — global top bar: mission scope, status, search, fleet summary,
// bell, mission clock. Dynamic bits carry ids so the page updates them in place
// (updateRibbon) rather than re-rendering the whole bar every second.
//
// NOTE (backend mismatch): a named "mission scope" and mission status are not in
// the backend yet (no mission registry). We show an honest live-fleet label and
// mark the status slot NO TELEM instead of inventing "Search — Lake Bolmen".

export function Ribbon({ missionLabel = "Live fleet", missionStatusKnown = false, missionStatus = "", counts = { c: 0, p: 0, d: 0 }, alertCount = 0 } = {}) {
  const status = missionStatusKnown
    ? `<span class="mstatus">${missionStatus}</span>`
    : `<span class="mstatus no-telem" title="No named-mission scope in backend yet">No mission</span>`;
  return `
  <div class="ribbon">
    <div class="rib-brand"><span class="mark"></span></div>
    <div class="rib-seg">
      <button class="mission-select" title="Mission scope">
        <span class="lbl">Mission</span>
        <span class="cap"><b>${missionLabel}</b></span>
        <span class="chev">▼</span>
      </button>
      ${status}
    </div>
    <div class="rib-seg grow">
      <div class="search" tabindex="0">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/></svg>
        <span>Search vehicles, pages, systems…</span>
      </div>
    </div>
    <div class="rib-seg">
      <div class="fleet-sum">
        <span class="lbl">Fleet</span>
        <span class="cnt"><span class="dot c"></span><span id="rib-c">${counts.c}</span></span>
        <span class="cnt"><span class="dot p"></span><span id="rib-p">${counts.p}</span></span>
        <span class="cnt"><span class="dot d"></span><span id="rib-d">${counts.d}</span></span>
      </div>
    </div>
    <div class="rib-seg">
      <div class="bell" title="${alertCount} notifications">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>
        <span class="badge" id="rib-bell"${alertCount ? "" : ' style="display:none"'}>${alertCount}</span>
      </div>
    </div>
    <div class="rib-seg"><div class="clock"><span class="lbl">Local time</span><span class="t" id="rib-clock">--:--:--</span></div></div>
  </div>`;
}

/** update the live bits without re-rendering the whole ribbon */
export function updateRibbon({ counts, alertCount, clock } = {}) {
  if (counts) {
    const set = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    set("rib-c", counts.c); set("rib-p", counts.p); set("rib-d", counts.d);
  }
  if (clock != null) { const e = document.getElementById("rib-clock"); if (e) e.textContent = clock; }
  if (alertCount != null) {
    const b = document.getElementById("rib-bell");
    if (b) { b.textContent = alertCount; b.style.display = alertCount ? "" : "none"; }
  }
}
