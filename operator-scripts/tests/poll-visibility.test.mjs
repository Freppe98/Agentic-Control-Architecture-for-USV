// Verifies the pauseWhenHidden option on api.poll: a backgrounded tab stops fetching and
// resumes when foregrounded. Uses the injectable isHidden seam so no DOM is needed.
import { test } from "node:test";
import assert from "node:assert/strict";
import { poll } from "../operator/services/api.js";

test("poll pauses fetching while hidden and resumes when visible", async () => {
  let hidden = true, count = 0;
  const stop = poll(
    () => { count++; return Promise.resolve(1); },
    15, () => {}, null, null,
    { pauseWhenHidden: true, isHidden: () => hidden },
  );
  await new Promise((r) => setTimeout(r, 60));
  const whileHidden = count;
  hidden = false;
  await new Promise((r) => setTimeout(r, 60));
  stop();
  assert.equal(whileHidden, 0, "no fetch while hidden");
  assert.ok(count > 0, "fetch resumed once visible");
});

test("poll without the option keeps fetching regardless (back-compat)", async () => {
  let count = 0;
  const stop = poll(() => { count++; return Promise.resolve(1); }, 15, () => {});
  await new Promise((r) => setTimeout(r, 50));
  stop();
  assert.ok(count > 0);
});
