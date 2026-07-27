// Unit tests for the shared cross-page selection store (operator/lib/selection.js).
// Run: `node --test tests/` (or `npm test`).
import { test } from "node:test";
import assert from "node:assert/strict";
import {
  getSelectedVehicleId, setSelectedVehicleId, getSelectedAt,
  subscribeSelection, _setStorageForTest,
} from "../operator/lib/selection.js";

function fakeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    _map: m,
  };
}

test("starts empty with no backing value", () => {
  _setStorageForTest(fakeStorage());
  assert.equal(getSelectedVehicleId(), null);
  assert.equal(getSelectedAt(), null);
});

test("normalizes numeric strings and rejects garbage", () => {
  _setStorageForTest(fakeStorage());
  assert.equal(setSelectedVehicleId("3"), 3);
  assert.equal(getSelectedVehicleId(), 3);
  assert.equal(setSelectedVehicleId("not-a-number"), null);
  assert.equal(getSelectedVehicleId(), null);
});

test("null clears the selection and its timestamp", () => {
  _setStorageForTest(fakeStorage());
  setSelectedVehicleId(2, 1000);
  assert.equal(getSelectedAt(), 1000);
  setSelectedVehicleId(null);
  assert.equal(getSelectedVehicleId(), null);
  assert.equal(getSelectedAt(), null);
});

test("notifies subscribers only on a real change", () => {
  _setStorageForTest(fakeStorage());
  const seen = [];
  const off = subscribeSelection((id) => seen.push(id));
  setSelectedVehicleId(4);
  setSelectedVehicleId(4);   // unchanged → no notify, no timestamp churn
  setSelectedVehicleId(5);
  off();
  setSelectedVehicleId(6);   // after unsubscribe → not seen
  assert.deepEqual(seen, [4, 5]);
});

test("persists across a reload from the same storage", () => {
  const s = fakeStorage();
  _setStorageForTest(s);
  setSelectedVehicleId(9, 4242);
  _setStorageForTest(s);     // simulate a fresh page load reading the same storage
  assert.equal(getSelectedVehicleId(), 9);
  assert.equal(getSelectedAt(), 4242);
});

test("survives a private-mode storage that throws on write", () => {
  const throwing = { getItem: () => null, setItem: () => { throw new Error("denied"); }, removeItem: () => {} };
  _setStorageForTest(throwing);
  assert.equal(setSelectedVehicleId(1), 1);   // in-memory still works
  assert.equal(getSelectedVehicleId(), 1);
});
