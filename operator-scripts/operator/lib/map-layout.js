// map-layout.js — the shared Leaflet layout contract. Every map instance in the operator
// station uses this; there is no per-page resize or per-page control offset.
//
// It solves two problems that are really the same problem — "Leaflet does not know the
// box it was given changed":
//
//  1. SIZE. A Leaflet map caches its pixel size at init. Anything that changes the size of
//     the cell it sits in (window resize, a responsive breakpoint crossing, a bottom bar
//     appearing/disappearing, a panel collapsing, coming back to a map route, the tab
//     becoming visible again, entering fullscreen) leaves the map rendering at the old
//     size — grey bands, tiles in the wrong place, clicks landing off-target. A single
//     setTimeout after first paint, which is what the Plan page used to do, only covers
//     the very first of those. A ResizeObserver on the stage covers all of them, because
//     every one of those events ultimately changes the stage's box.
//
//  2. CORNER SPACE. Leaflet places its own controls in fixed corners and knows nothing
//     about our overlays. Rather than hard-coding an offset that happens to work at
//     1920x1080, we MEASURE the overlays we put in a corner and publish the result as CSS
//     custom properties on the stage (--map-tr-h/--map-tr-w for the top-right stack,
//     --map-tl-h for the top-left one). theme.css then offsets the Leaflet corner, the
//     legend and the map toast off those variables, so the layout is correct at every
//     viewport and after every re-render.
//
// invalidateSize is coalesced through requestAnimationFrame: a drag-resize fires the
// observer dozens of times per second and each raw call is a full Leaflet re-layout.

/** Round up to whole pixels; a fractional CSS var offset makes controls shimmer. */
const px = (n) => `${Math.ceil(n || 0)}px`;

/**
 * Total height and max width of a set of overlay elements sharing one map corner.
 * Pure — no DOM reads — so the arithmetic is unit-testable. Hidden elements (width and
 * height both 0) contribute nothing, including no gap, which is what makes a conditional
 * overlay collapse cleanly instead of leaving a phantom offset behind.
 *
 * @param {Array<{width:number,height:number}>} boxes measured rects, in stacking order
 * @param {number} gap gap between two stacked overlays
 * @returns {{ h:number, w:number }}
 */
export function cornerExtent(boxes, gap = 0) {
  const live = (boxes || []).filter((b) => b && (b.width > 0 || b.height > 0));
  if (!live.length) return { h: 0, w: 0 };
  const h = live.reduce((sum, b) => sum + (b.height || 0), 0) + gap * (live.length - 1);
  const w = live.reduce((max, b) => Math.max(max, b.width || 0), 0);
  return { h, w };
}

/**
 * Coalesce repeated calls into one per animation frame. Returned function is safe to call
 * at observer frequency; `cancel()` drops any pending frame (used on teardown so a
 * removed map is never invalidated).
 */
export function frameCoalesced(fn, schedule = requestAnimationFrame, cancelFrame = cancelAnimationFrame) {
  let pending = null;
  const run = () => { pending = null; fn(); };
  const wrapped = () => { if (pending == null) pending = schedule(run); };
  wrapped.cancel = () => { if (pending != null) { cancelFrame(pending); pending = null; } };
  return wrapped;
}

/**
 * Wire a Leaflet map to its stage element.
 *
 * @param {L.Map} map
 * @param {HTMLElement} stage  the positioned .map-stage the map fills
 * @param {object} corners     { topLeft: [el], topRight: [el] } — our own overlays, so
 *                             Leaflet's controls can be pushed clear of them
 * @returns {() => void} cleanup — disconnects every observer and listener
 */
export function attachMapLayout(map, stage, { topLeft = [], topRight = [] } = {}) {
  if (!map || !stage) return () => {};
  const gap = 8;
  const tl = topLeft.filter(Boolean);
  const tr = topRight.filter(Boolean);

  const publishCorners = () => {
    const rect = (el) => el.getBoundingClientRect();
    const a = cornerExtent(tl.map(rect), gap);
    const b = cornerExtent(tr.map(rect), gap);
    stage.style.setProperty("--map-tl-h", px(a.h));
    stage.style.setProperty("--map-tr-h", px(b.h));
    stage.style.setProperty("--map-tr-w", px(b.w));
  };

  // pan:false — a size change must not scroll the operator's view off the vehicle it is
  // watching; the centre is preserved and only the canvas grows/shrinks.
  const refresh = frameCoalesced(() => {
    publishCorners();
    try { map.invalidateSize({ pan: false }); } catch (e) { /* map already removed */ }
  });

  const observers = [];
  if (typeof ResizeObserver === "function") {
    const stageObs = new ResizeObserver(refresh);
    stageObs.observe(stage);
    observers.push(stageObs);
    // Overlay content is re-rendered on every model change (a longer status banner, a
    // second row of view controls); observing them keeps the reserved corner honest
    // without the pages having to remember to call anything.
    if (tl.length || tr.length) {
      const ovObs = new ResizeObserver(refresh);
      [...tl, ...tr].forEach((el) => ovObs.observe(el));
      observers.push(ovObs);
    }
  }

  // Belt-and-braces for the cases a ResizeObserver does not see, or sees too early:
  // a tab restored from the background has a zero-size stage while hidden, and
  // fullscreen changes can settle after the observer has already fired.
  const onWindow = () => refresh();
  const onVisible = () => { if (!document.hidden) refresh(); };
  window.addEventListener("resize", onWindow);
  window.addEventListener("orientationchange", onWindow);
  document.addEventListener("fullscreenchange", onWindow);
  document.addEventListener("visibilitychange", onVisible);

  refresh();

  return function cleanup() {
    refresh.cancel();
    observers.forEach((o) => o.disconnect());
    window.removeEventListener("resize", onWindow);
    window.removeEventListener("orientationchange", onWindow);
    document.removeEventListener("fullscreenchange", onWindow);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
