// Stub.js — placeholder for pages not yet migrated. Keeps the app runnable and
// navigable while the page waits its turn in the migration queue.
import { NavRail } from "../components/NavRail.js";
import { Ribbon, updateRibbon } from "../components/Ribbon.js";
import { NAV } from "../lib/ui.js";

export function Stub(root, key) {
  const label = (NAV.find(([k]) => k === key) || [null, key])[1];
  root.className = "app no-dock";
  root.innerHTML =
    Ribbon({ missionLabel: "Live fleet" }) +
    NavRail(key) +
    `<div class="stub">
       <svg class="st-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 8v5l3 2"/><circle cx="12" cy="12" r="9"/></svg>
       <h2>${label} — not migrated yet</h2>
       <p>This page is next in the migration queue. The design is frozen in the approved wireframe; it will appear here once wired to the live backend.</p>
     </div>`;
  const clockId = setInterval(() => updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) }), 1000);
  updateRibbon({ clock: new Date().toLocaleTimeString([], { hour12: false }) });
  return () => clearInterval(clockId);
}
