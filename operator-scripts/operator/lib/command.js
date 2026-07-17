// command.js — pure, DOM-free command-lifecycle helpers shared by the Map and Vehicle
// command panels (and unit-tested directly, tests/command.test.mjs). One tested place
// for the two rules that both pages must apply IDENTICALLY:
//
//   1. Verification-aware terminal outcome. A command's outer status EXECUTED means only
//      that the Local Agent completed the attempt against Scout — it is NOT proof the
//      vehicle did the thing. For the commands whose nested Scout result the backend
//      classifies (SET_HOME → home_result, RTL → rtl_result), EXECUTED must render as a
//      SUCCESS only when that per-type verification passed; otherwise it is a failed
//      attempt shown in the failure colour, never an optimistic green "confirmed".
//   2. Duplicate-press prevention. While a command of a given type is still nonterminal
//      (QUEUED/SENT/ACCEPTED), its button is suppressed so a rapid second press cannot
//      queue a duplicate — important for LOITER, the primary anti-drift safety hold.

export const CMD_TERMINAL = new Set(["EXECUTED", "REJECTED", "FAILED", "EXPIRED"]);

/**
 * Verification-aware outcome of ONE command record.
 * @param cmd a command record ({ type, status, home_result?, rtl_result? })
 * @returns {{ verified: boolean|null }}
 *   verified === true  → a genuinely confirmed vehicle action (render as success/green)
 *   verified === false → EXECUTED transport but the per-type verification FAILED
 *                        (render as failure/red; cmd.reason carries the real reason)
 *   verified === null  → not applicable: either not EXECUTED yet, or a command type with
 *                        no separate verification (AUTO/MANUAL/LOITER/ARM/…), where
 *                        EXECUTED already means success and the plain status stands.
 */
export function commandVerification(cmd) {
  if (!cmd || cmd.status !== "EXECUTED") return { verified: null };
  if (cmd.type === "SET_HOME") return { verified: cmd.home_result === "verified" };
  if (cmd.type === "RTL") return { verified: cmd.rtl_result === "confirmed" };
  return { verified: null };
}

/** True when a command is still in flight (not a terminal status). */
export function isNonterminal(cmd) {
  return !!cmd && !CMD_TERMINAL.has(cmd.status);
}

/**
 * Is there already a nonterminal command of this exact type in the list? Used to
 * suppress a duplicate press while one of the same type is still pending — the button
 * stays visible (LOITER must remain a visible safety option) but is disabled until the
 * outstanding command reaches a terminal state.
 */
export function hasPendingOfType(cmds, type) {
  return Array.isArray(cmds) && cmds.some((c) => c && c.type === type && isNonterminal(c));
}
