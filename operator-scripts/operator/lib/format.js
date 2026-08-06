// format.js — the ONE place a structured value becomes operator-readable text.
//
// WHY THIS EXISTS: `${value}` on an object produces "[object Object]", and that string was
// reaching the operator in mission-lifecycle errors and policy fields. It is worse than a blank:
// it looks like a value, occupies the place where a reason belongs, and tells the operator
// nothing about a vehicle that may be moving. Scout legitimately sends structured errors
// (`{code, message}`), structured policy values (`{value, source}`) and structured energy
// calculations, so the fix is to FORMAT them, not to stringify or hide them.
//
// Every rule here is about staying honest:
//   • null / undefined / "" stay NULL, so the caller renders its own "—" rather than the word
//     "null" or an empty gap that reads as a value;
//   • an object is rendered from its own most-specific human field (message → detail → reason →
//     error → code) when it has one, else as readable `key=value` pairs — never dropped;
//   • nothing is ever coerced with String() on an object, anywhere in the station.
//
// No DOM, no imports. Unit-tested in tests/format.test.mjs.

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);

// Field names that carry a HUMAN sentence, in the order a reader wants them: the message a
// Scout wrote beats the code it classified under, which beats nothing at all.
const MESSAGE_KEYS = ["message", "detail", "reason", "description", "error", "text"];
// Field names that carry a MACHINE code worth showing alongside the message.
const CODE_KEYS = ["code", "error_code", "errorCode"];

/**
 * Readable text for anything, or null when there is genuinely nothing to say.
 * @param {*} value
 * @param {{ maxPairs?: number, separator?: string }} opts
 * @returns {string|null}
 */
export function asText(value, { maxPairs = 8, separator = " · " } = {}) {
  if (value === null || value === undefined) return null;
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number") return Number.isFinite(value) ? String(value) : null;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) {
    const parts = value.map((v) => asText(v, { maxPairs, separator })).filter(Boolean);
    return parts.length ? parts.join("; ") : null;
  }
  if (isObj(value)) {
    const message = firstString(value, MESSAGE_KEYS);
    const code = firstString(value, CODE_KEYS);
    if (message && code && message !== code) return `${code} — ${message}`;
    if (message) return message;
    if (code) return code;
    // No human field: show the object's own content as pairs rather than losing it. Bounded so
    // one large nested blob cannot flood a status line.
    const entries = Object.entries(value);
    const parts = [];
    for (const [k, v] of entries) {
      const t = asText(v, { maxPairs: 2, separator: ", " });
      if (t === null) continue;
      parts.push(`${k}=${t}`);
      if (parts.length >= maxPairs) break;
    }
    if (!parts.length) return null;
    const more = entries.length - parts.length;
    return parts.join(separator) + (more > 0 ? `${separator}(+${more} more)` : "");
  }
  return String(value);
}

function firstString(obj, keys) {
  for (const k of keys) {
    const v = obj[k];
    if (typeof v === "string" && v.trim()) return v.trim();
  }
  return null;
}

/** asText with a caller-chosen placeholder — for the many display slots that want "—". */
export function textOr(value, fallback = "—") {
  const t = asText(value);
  return t === null ? fallback : t;
}

/** HTML-escape. Scout's strings reach the DOM through template literals, and a value that
 *  happens to contain `<` must render as text, not as markup. */
export function esc(value) {
  const t = asText(value);
  if (t === null) return "";
  return t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** Escaped text for an HTML attribute, with a placeholder for "nothing to say". */
export function escAttr(value, fallback = "") {
  const t = asText(value);
  return t === null ? fallback : esc(t);
}
