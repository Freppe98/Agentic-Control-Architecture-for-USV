// mission-visibility.js — pure per-USV mission-overlay visibility policy. No DOM, no
// Leaflet: the single tested place (tests/mission-visibility.test.mjs) that decides
//   • whether a mission may be shown at all (valid, loaded, non-empty, complete);
//   • what the default visibility is after a fresh read (show a newly-valid mission unless
//     the operator has explicitly hidden THIS USV's overlay; a geometry change re-shows);
//   • the single stateful Show/Hide toggle's label / disabled / aria state.
//
// Visibility is PER-USV — never a global flag shared across vehicles. The Map page holds a
// small { shown, userHidden } record per USV and drives it through these helpers so the
// rules can't drift between the auto-fetch, manual Fetch, select, and toggle paths.

/**
 * May this mission be shown as a valid overlay? Requires a loaded, non-empty, complete and
 * (where Scout reports it) VALID mission with at least one positioned waypoint. An invalid,
 * partial, empty or never-loaded mission is never shown as if it were valid.
 * @param mission the cached Pixhawk read-back (or null)
 * @param positionedCount number of waypoints with a real lat/lng
 */
export function missionShowable(mission, positionedCount) {
  if (!mission) return false;
  if (mission.count == null || mission.count <= 0) return false;
  if (typeof mission.loaded === "boolean" && mission.loaded === false) return false;
  if (typeof mission.valid === "boolean" && mission.valid === false) return false;
  if (mission.partial === true) return false;
  return (positionedCount || 0) > 0;
}

/**
 * The next per-USV visibility after a fresh read.
 * @param prev prior { shown, userHidden } (or null/undefined for a first read)
 * @param showable result of missionShowable() for the new read
 * @param geometryChanged true when the mission GEOMETRY changed (successful upload/replan/clear)
 * @returns { shown, userHidden }
 *
 * Rules:
 *  - A geometry change clears the explicit-hide memory → a newly loaded mission shows again.
 *  - A showable mission is visible by default unless the operator explicitly hid THIS USV.
 *  - A non-showable mission (empty/invalid/partial) is never shown; the hide memory is kept
 *    (so it applies again if a valid mission returns without a geometry change).
 */
export function nextVisibility(prev, { showable, geometryChanged } = {}) {
  let userHidden = prev ? !!prev.userHidden : false;
  if (geometryChanged) userHidden = false;
  if (!showable) return { shown: false, userHidden };
  return { shown: !userHidden, userHidden };
}

/**
 * Flip the overlay in response to an explicit toggle click. Hiding records the explicit-hide
 * memory for this USV; showing clears it.
 */
export function toggleVisibility(prev) {
  const shown = !(prev && prev.shown);
  return { shown, userHidden: !shown };
}

/**
 * The single stateful Show/Hide toggle's presentation, derived from ACTUAL state (loading /
 * showable / shown) — never from the button's last label. State is conveyed by text + aria,
 * not colour alone.
 * @returns { disabled, label, ariaPressed, title, state }
 */
export function toggleButton({ loading, showable, shown } = {}) {
  if (loading) {
    return { disabled: true, label: "Loading mission…", ariaPressed: !!shown,
             title: "Mission data is loading…", state: "loading" };
  }
  if (!showable) {
    return { disabled: true, label: "No mission", ariaPressed: false,
             title: "No valid mission loaded for this vehicle to display.", state: "none" };
  }
  if (shown) {
    return { disabled: false, label: "Hide mission", ariaPressed: true,
             title: "Mission overlay is shown — click to hide it.", state: "shown" };
  }
  return { disabled: false, label: "Show mission", ariaPressed: false,
           title: "Mission overlay is hidden — click to show it.", state: "hidden" };
}
