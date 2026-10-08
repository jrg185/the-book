import assert from "node:assert/strict";

// Half a cent. Two USD amounts inside this gap are the same money.
export const CENT_TOL = 0.005;

export function approxCents(a, b, tol = CENT_TOL) {
  if (typeof a !== "number" || typeof b !== "number") return false;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return false;
  return Math.abs(a - b) < tol;
}

export function assertCentsEqual(actual, expected, message) {
  if (actual == null || expected == null) {
    assert.equal(actual, expected, message);
    return;
  }
  assert.ok(
    approxCents(actual, expected),
    message ?? `${actual} is not within half a cent of ${expected}`
  );
}

export function assertCentsDiffer(actual, expected, message) {
  if (
    actual == null ||
    expected == null ||
    typeof actual !== "number" ||
    typeof expected !== "number" ||
    !Number.isFinite(actual) ||
    !Number.isFinite(expected)
  ) {
    assert.notEqual(actual, expected, message);
    return;
  }
  assert.ok(!approxCents(actual, expected), message ?? `${actual} and ${expected} match at cents`);
}

// Half of 0.01%. Two book fractions inside this gap are the same reading
// at two decimal places of a percent. Strict: a gap of exactly 5e-5 fails.
export const FRAC_TOL = 5e-5;

export function approxFrac(a, b, tol = FRAC_TOL) {
  if (typeof a !== "number" || typeof b !== "number") return false;
  if (!Number.isFinite(a) || !Number.isFinite(b)) return false;
  return Math.abs(a - b) < tol;
}

export function assertFracEqual(actual, expected, message) {
  if (actual == null || expected == null) {
    assert.equal(actual, expected, message);
    return;
  }
  assert.ok(
    approxFrac(actual, expected),
    message ?? `${actual} is not within half of 0.01% of ${expected}`
  );
}
