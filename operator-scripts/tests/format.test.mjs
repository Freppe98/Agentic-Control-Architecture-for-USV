// format.test.mjs — the one rule: a structured value NEVER reaches the operator as
// "[object Object]".
//
// This is not cosmetic. "[object Object]" appeared in mission-lifecycle errors and policy
// fields, i.e. exactly where the operator looks to find out why a vehicle did not do what was
// asked. It looks like a value, occupies the place a reason belongs, and says nothing.
import test from "node:test";
import assert from "node:assert/strict";
import { asText, textOr, esc, escAttr } from "../operator/lib/format.js";

test("nothing ever coerces to [object Object]", () => {
  const cases = [
    {}, { a: 1 }, { code: "X" }, { message: "boom" }, { nested: { deep: { deeper: 1 } } },
    [{ a: 1 }, { b: 2 }], { error: { code: "E", message: "m" } },
    new Date(0), { toString: null },
  ];
  for (const c of cases) {
    for (const out of [String(asText(c)), textOr(c), esc(c), escAttr(c)]) {
      assert.doesNotMatch(out, /\[object Object\]/, JSON.stringify(c));
    }
  }
});

test("null-ish values stay null so the caller renders its own placeholder", () => {
  for (const v of [null, undefined, "", "   "]) assert.equal(asText(v), null);
  assert.equal(textOr(null), "—");
  assert.equal(textOr(null, "n/a"), "n/a");
  // …and never the WORD "null", which reads as a value.
  assert.notEqual(asText(null), "null");
  assert.equal(esc(null), "");
});

test("a structured Scout error renders its code and message, in that order", () => {
  assert.equal(asText({ code: "SET_HOME_FAILED", message: "no ack from Pixhawk" }),
    "SET_HOME_FAILED — no ack from Pixhawk");
  assert.equal(asText({ message: "no ack" }), "no ack");
  assert.equal(asText({ error_code: "AUTO_NOT_VERIFIED" }), "AUTO_NOT_VERIFIED");
  // A message that IS the code is not repeated.
  assert.equal(asText({ code: "X", message: "X" }), "X");
});

test("an object with no human field is shown as its own content, never dropped", () => {
  assert.equal(asText({ battery_percent: 12, margin: -3 }), "battery_percent=12 · margin=-3");
  assert.equal(asText({ ok: true, done: false }), "ok=yes · done=no");
  // Bounded, so one large blob cannot flood a status line — but the truncation is declared.
  const big = Object.fromEntries(Array.from({ length: 20 }, (_, i) => [`k${i}`, i]));
  const t = asText(big);
  assert.match(t, /\(\+12 more\)/);
});

test("empty containers are null, not an empty-looking value", () => {
  assert.equal(asText({}), null);
  assert.equal(asText([]), null);
  assert.equal(asText({ a: null, b: undefined }), null);
});

test("primitives keep their meaning", () => {
  assert.equal(asText("  hi  "), "hi");
  assert.equal(asText(0), "0");
  assert.equal(asText(false), "no");
  assert.equal(asText(true), "yes");
  assert.equal(asText(NaN), null);         // not a number the operator can act on
  assert.equal(asText(["a", null, "b"]), "a; b");
});

test("esc renders markup as text — a Scout string is never trusted as HTML", () => {
  assert.equal(esc("<b>x</b>"), "&lt;b&gt;x&lt;/b&gt;");
  assert.equal(esc({ message: '"quoted" & <tagged>' }),
    "&quot;quoted&quot; &amp; &lt;tagged&gt;");
  assert.equal(escAttr(null, "fallback"), "fallback");
});
