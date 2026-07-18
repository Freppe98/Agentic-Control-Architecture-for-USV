// command.js — pure, DOM-free command-lifecycle helpers shared by the Map and Vehicle
// command panels, the Events detail view and the Mission upload workflow (unit-tested
// directly, tests/command.test.mjs). ONE tested place for the rules every command
// consumer must apply IDENTICALLY:
//
//   1. Verification-aware terminal outcome. A command's outer status EXECUTED means only
//      that the Local Agent completed the attempt against Scout — it is NOT proof the
//      vehicle did the thing. The stabilized contract carries a normalized
//      `verification` block (verified/outcome/expected/observed/reason) the backend
//      computes for EVERY command type; this module reads that when present and, for
//      older/synthetic records, recomputes the same decision from the per-type fields
//      (SET_HOME→home_result, RTL→rtl_result, MISSION_*→mission_result, else
//      result.verified). An EXECUTED command with verified===false renders as FAILED;
//      an unknown/older record for a command that HAS a verification is treated
//      conservatively (never an optimistic green on transport success alone).
//   2. Duplicate-press prevention. While a command of a given type is still nonterminal
//      (QUEUED/SENT/ACCEPTED), its button is suppressed so a rapid second press cannot
//      queue a duplicate — important for LOITER (the anti-drift safety hold) and for
//      MISSION_UPLOAD (never fire a second upload while one is in flight).

export const CMD_TERMINAL = new Set(["EXECUTED", "REJECTED", "FAILED", "EXPIRED"]);
const TERMINAL_FAIL = new Set(["REJECTED", "FAILED", "EXPIRED"]);
const MISSION_WRITE = new Set(["MISSION_UPLOAD", "MISSION_CLEAR"]);

function firstOf(...vals) {
  for (const v of vals) if (v !== null && v !== undefined && v !== "") return v;
  return null;
}

// Per-type verified decision recomputed from an EXECUTED record's own fields — the
// fallback used when the backend's normalized `verification` block is absent (older or
// synthetic records). Returns true / false / null (null = no separate verification for
// this type, so EXECUTED already means success).
function perTypeVerified(cmd) {
  if (!cmd || cmd.status !== "EXECUTED") return null;
  if (cmd.type === "SET_HOME") return cmd.home_result === "verified";
  if (cmd.type === "RTL") return cmd.rtl_result === "confirmed";
  if (MISSION_WRITE.has(cmd.type)) return cmd.mission_result === "verified";
  const r = cmd.result;
  if (r && typeof r === "object" && "verified" in r) return r.verified === true;
  return null;
}

function expectedObserved(cmd, vf) {
  if (vf && (vf.expected != null || vf.observed != null)) {
    return { expected: vf.expected ?? null, observed: vf.observed ?? null };
  }
  const r = cmd && cmd.result && typeof cmd.result === "object" ? cmd.result : {};
  let expected = firstOf(r.expected_mode, r.requested_mode, r.expected_state, r.expected);
  let observed = firstOf(r.observed_mode, r.observed_state, r.observed, r.mode);
  if (cmd && MISSION_WRITE.has(cmd.type)) {
    const ec = cmd.params && cmd.params.expected_count;
    const oc = firstOf(r.observed_count, r.count, r.mission_count);
    if (ec != null) expected = `${ec} waypoints`;
    if (oc != null) observed = `${oc} waypoints`;
  }
  return { expected: expected ?? null, observed: observed ?? null };
}

/** Normalized terminal-outcome vocabulary: PENDING / VERIFIED / EXECUTED / FAILED /
 *  REJECTED / EXPIRED — the same labels the backend's _outcome_label emits. */
export function outcomeFrom(status, verified) {
  if (!CMD_TERMINAL.has(status)) return "PENDING";
  if (status === "EXECUTED") return verified === true ? "VERIFIED" : verified === false ? "FAILED" : "EXECUTED";
  return status;
}

/**
 * Verification-aware outcome of ONE command record — the single decision Map, Vehicle,
 * Events and Mission all read, so they can never disagree about the same command.
 * @param cmd a command record
 * @returns {{ verified: boolean|null, outcome: string, expected: *, observed: *, reason: string|null }}
 *   verified === true  → a genuinely confirmed vehicle action (render as success/green)
 *   verified === false → EXECUTED transport but the verification FAILED (render red; reason set)
 *   verified === null  → not applicable: not EXECUTED yet, or a command type with no
 *                        separate verification (AUTO/MANUAL/LOITER/ARM/…) — EXECUTED stands.
 */
export function commandVerification(cmd) {
  if (!cmd) return { verified: null, outcome: "—", expected: null, observed: null, reason: null };
  const vf = cmd.verification && typeof cmd.verification === "object" ? cmd.verification : null;
  const verified = vf && "verified" in vf ? (vf.verified ?? null) : perTypeVerified(cmd);
  const { expected, observed } = expectedObserved(cmd, vf);
  const outcome = vf && vf.outcome ? vf.outcome : outcomeFrom(cmd.status, verified);
  const err = cmd.error || (cmd.result && typeof cmd.result === "object" ? cmd.result.error : null);
  const reason = (verified === false || TERMINAL_FAIL.has(cmd.status))
    ? ((vf && vf.reason) || cmd.reason || (err && (err.message || err.code)) || null)
    : null;
  return { verified, outcome, expected, observed, reason };
}

/** Normalized command source (OPERATOR / LOCAL_AGENT / MISSION_AGENT), conservative
 *  default OPERATOR for records that predate the field. */
export function commandSource(cmd) {
  const raw = String((cmd && (cmd.source || cmd.created_by)) || "").toUpperCase();
  if (raw.includes("MISSION")) return "MISSION_AGENT";
  if (raw === "LOCAL_AGENT" || raw.includes("AGENT") || raw.includes("LOCAL") || raw.includes("AUTONOM")) return "LOCAL_AGENT";
  return "OPERATOR";
}

/** Ordered lifecycle stages ({stage, ts}) for a command — the backend's normalized
 *  `lifecycle` when present, else assembled from the record's own queue timestamps so an
 *  older record still shows a progression. */
export function commandStages(cmd) {
  if (cmd && Array.isArray(cmd.lifecycle) && cmd.lifecycle.length) return cmd.lifecycle;
  if (!cmd) return [];
  const out = [];
  if (cmd.created_at) out.push({ stage: "QUEUED", ts: cmd.created_at });
  if (cmd.claimed_at) out.push({ stage: "SENT", ts: cmd.claimed_at });
  if (cmd.completed_at) out.push({ stage: cmd.status, ts: cmd.completed_at });
  return out;
}

/** True when a command is still in flight (not a terminal status). */
export function isNonterminal(cmd) {
  return !!cmd && !CMD_TERMINAL.has(cmd.status);
}

/**
 * Is there already a nonterminal command of this exact type in the list? Used to
 * suppress a duplicate press while one of the same type is still pending — the button
 * stays visible (LOITER must remain a visible safety option) but is disabled until the
 * outstanding command reaches a terminal state. Also guards MISSION_UPLOAD (no second
 * upload while one is executing).
 */
export function hasPendingOfType(cmds, type) {
  return Array.isArray(cmds) && cmds.some((c) => c && c.type === type && isNonterminal(c));
}
