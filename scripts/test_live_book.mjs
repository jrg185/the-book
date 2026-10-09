import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  cardPositionRows,
  cryptoBookView,
  deriveSleeve,
  holdingRows,
  killRemainingUsd,
  LIVE_RAILS,
  mergeLiveBook,
  openPositionRows,
  reconciledPnl,
  SEEDS_USD,
  shownBooks,
  warehouseSleeveAsOf,
} from "../derive.js";
import { approxCents, approxFrac, assertCentsDiffer, assertCentsEqual, assertFracEqual } from "./cents.mjs";

const live = JSON.parse(readFileSync(new URL("../data/live_book.json", import.meta.url), "utf8"));
const summary = JSON.parse(readFileSync(new URL("../data/kpi_summary.json", import.meta.url), "utf8"));
const openPositions = JSON.parse(readFileSync(new URL("../data/open_positions.json", import.meta.url), "utf8"));
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const app = readFileSync(new URL("../app.js", import.meta.url), "utf8");

function seededBalance(sleeve) {
  const row = summary.find((item) => item.sleeve === sleeve);
  const seed = SEEDS_USD[sleeve];
  return Math.round((seed * row.running_balance_frac + Number.EPSILON) * 100) / 100;
}

function agenticTotal(book) {
  const cash = holdingRows(book).reduce((sum, row) => sum + row.valueUsd, 0);
  const lots = openPositionRows(book)
    .filter((row) => row.sleeve === "crypto")
    .reduce((sum, row) => sum + row.valueUsd, 0);
  return Math.round((cash + lots + Number.EPSILON) * 100) / 100;
}

function tickers(rows) {
  return (rows || []).map((row) => String(row?.ticker || "").trim().toUpperCase());
}

function cashRows(book) {
  const rows = Array.isArray(book?.holdings) ? book.holdings : [];
  return rows
    .filter((row) => {
      if (!row || !String(row.ticker || "").trim()) return false;
      return String(row.sleeve || "crypto").trim().toLowerCase() === "crypto";
    })
    .map((row) => [String(row.ticker).trim(), row.value_usd]);
}

function holdingsSum(book) {
  const total = holdingRows(book).reduce((sum, row) => sum + row.valueUsd, 0);
  return Math.round((total + Number.EPSILON) * 100) / 100;
}

function railDollars(bookUsd, frac) {
  return Math.round((bookUsd * frac + Number.EPSILON) * 100) / 100;
}

function roundCents(value) {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

function holdingNonzero(value) {
  return typeof value === "number" && Number.isFinite(value) && !approxCents(value, 0);
}

// book_usd is the holdings sum. One nonzero cash line is that sum. Two or
// more nonzero lines each leave the rest of the sum outside themselves, so
// none of them is the book. All-zero cash is a zero book.
function assertHoldingsVersusBook(rows, bookUsd, label = "") {
  const nonzero = rows.filter((row) => holdingNonzero(row.valueUsd));
  const tag = (ticker) => (label ? `${label} ${ticker}` : ticker);
  if (nonzero.length >= 2) {
    for (const row of rows) assertCentsDiffer(row.valueUsd, bookUsd, tag(row.ticker));
    return;
  }
  if (nonzero.length === 1) {
    assertCentsEqual(nonzero[0].valueUsd, bookUsd, tag(nonzero[0].ticker));
    return;
  }
  assertCentsEqual(bookUsd, 0, tag("book"));
  for (const row of rows) assertCentsEqual(row.valueUsd, 0, tag(row.ticker));
}

// Two dollar readings may land on the same cents. Require a difference only
// when the sources that produce them are already apart.
function assertCentsDifferWhen(actual, expected, apart, message) {
  if (apart) assertCentsDiffer(actual, expected, message);
  else assertCentsEqual(actual, expected, message);
}

function assertFracDifferWhen(actual, expected, apart, message) {
  if (!apart) {
    if (actual == null || expected == null) {
      assert.equal(actual, expected, message);
      return;
    }
    assertFracEqual(actual, expected, message);
    return;
  }
  if (actual == null || expected == null) {
    assert.notEqual(actual, expected, message);
    return;
  }
  assert.equal(
    approxFrac(actual, expected),
    false,
    message ?? `${actual} and ${expected} match as fractions`
  );
}

// Combined seed the export subtracts from cash + lots. Read from the canonical
// config, the same file the writer and derive.js load.
function exportCombinedSeed() {
  const config = JSON.parse(readFileSync(new URL("../config/book_seeds.json", import.meta.url), "utf8"));
  const rows = (config.seeds || []).filter((row) => String(row.sleeve).toLowerCase() === "combined");
  assert.ok(rows.length > 0, "combined book seed");
  rows.sort((a, b) => String(a.effective_from).localeCompare(String(b.effective_from)));
  return Number(rows[rows.length - 1].seed_usd);
}

const combinedSeed = exportCombinedSeed();

// day_realized_usd is optional. When the gross and sell-fee audit fields are
// present, gross - fees matches the net within one cent.
function assertOptionalDayRealized(drop) {
  if (!drop || !Object.hasOwn(drop, "day_realized_usd")) return;
  assert.equal(Number.isFinite(drop.day_realized_usd), true);
  if (!Object.hasOwn(drop, "day_realized_gross_usd") || !Object.hasOwn(drop, "day_sell_fees_usd")) return;
  assert.equal(Number.isFinite(drop.day_realized_gross_usd), true);
  assert.equal(Number.isFinite(drop.day_sell_fees_usd), true);
  const gap = drop.day_realized_gross_usd - drop.day_sell_fees_usd - drop.day_realized_usd;
  assert.ok(Math.abs(gap) <= 0.01);
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// A numeric token with no digit, dot, or word character immediately before
// it, and no digit or dot immediately after it.
function hasBoundedNumericLiteral(text, literal) {
  return new RegExp(`(?<![\\d.\\w])${escapeRegExp(literal)}(?![\\d.])`).test(text);
}

// Printed form plus the two-decimal cents form when that is the same amount
// (1.8 also matches 1.80). A toFixed result that rounds away is left out.
function numericLiteralForms(value) {
  const plain = String(value);
  const cents = value.toFixed(2);
  if (cents === plain) return [plain];
  if (Math.abs(Number(cents) - value) > 1e-9) return [plain];
  return [plain, cents];
}

// null: not a cash fingerprint. 0 and amounts inside one dollar are too
// short to identify a live balance. "0" occurs in almost every source file,
// and a value under 1 collides with ratios and unrelated literals.
// true/false: text does or does not contain the amount as a standalone
// numeric literal, including the cents form.
function standaloneNumericLiteral(text, value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return false;
  if (value === 0 || Math.abs(value) < 1) return null;
  return numericLiteralForms(value).some((literal) => hasBoundedNumericLiteral(text, literal));
}

function assertCashLiteralAbsent(text, value, message) {
  const found = standaloneNumericLiteral(text, value);
  if (found == null) return;
  assert.equal(found, false, message);
}

function assertCashLiteralPresent(text, value, message) {
  const found = standaloneNumericLiteral(text, value);
  if (found == null) return;
  assert.equal(found, true, message);
}

// Decimal quantities stay dotted. String(4.0) is "4", which is not the qty
// fingerprint, and a qty under one dollar is still specific. The cash
// fingerprint skip does not apply here.
function sourceHasLiveDecimal(text, raw) {
  const value = Number(raw);
  const forms = [];
  if (Number.isFinite(value)) {
    for (const form of numericLiteralForms(value)) {
      if (form.includes(".")) forms.push(form);
    }
  }
  const printed = String(raw);
  if (printed.includes(".") && !forms.includes(printed)) forms.push(printed);
  return forms.some((form) => hasBoundedNumericLiteral(text, form));
}

// Published cash is checked too. These fixtures keep the same assertions
// from depending on whatever USD, USDC, and day fields are in rh_cash.json.
// Amounts are test inputs only. The ordinary USDC line is 27.41 because
// 14.07 is already a standalone literal in the export self-test.
const CASH_DROP_FIXTURES = [
  {
    name: "new ET day",
    USD: 20.5,
    USDC: 31.2,
    day_realized_usd: 0,
    day_realized_gross_usd: 0,
    day_sell_fees_usd: 0,
    as_of: "2026-10-09T04:00:00Z",
  },
  {
    name: "fractional USD with flat USDC",
    USD: 1.8,
    USDC: 0,
  },
  {
    name: "flat cash",
    USD: 0,
    USDC: 0,
  },
  {
    name: "ordinary book",
    USD: 433.10,
    USDC: 27.41,
    day_realized_usd: -1.09,
    day_realized_gross_usd: 1.25,
    day_sell_fees_usd: 2.34,
    as_of: "2026-06-01T15:00:00Z",
  },
];

function assertCashDrop(drop, sources) {
  const label = drop.name || "data/rh_cash.json";
  assert.equal(typeof drop.USD, "number", label);
  assert.equal(typeof drop.USDC, "number", label);
  assert.equal(Number.isFinite(drop.USD), true, label);
  assert.equal(Number.isFinite(drop.USDC), true, label);
  if (drop.as_of != null) {
    assert.equal(typeof drop.as_of, "string", label);
    assert.equal(Number.isNaN(Date.parse(drop.as_of)), false, label);
  }
  assertOptionalDayRealized(drop);
  const applied = {
    ...live,
    holdings: (live.holdings || []).map((row) => {
      if (row.ticker === "USD") return { ...row, value_usd: drop.USD };
      if (row.ticker === "USDC") return { ...row, value_usd: drop.USDC, cost_basis_usd: drop.USDC };
      return row;
    }),
  };
  if (Object.hasOwn(drop, "day_realized_usd")) applied.day_pnl_usd = drop.day_realized_usd;
  applied.book_usd = holdingsSum(applied);
  const view = cryptoBookView(applied);
  const usd = holdingRows(applied).find((row) => row.ticker === "USD");
  const usdc = holdingRows(applied).find((row) => row.ticker === "USDC");
  assertCentsEqual(usd.valueUsd, drop.USD, label);
  assertCentsEqual(usdc.valueUsd, drop.USDC, label);
  assertCentsEqual(view.bookUsd, applied.book_usd, label);
  assertHoldingsVersusBook(holdingRows(applied), applied.book_usd, label);
  assertLiveRails(view, applied);
  assert.deepEqual(
    (applied.positions || []).map((row) => row.ticker),
    (live.positions || []).map((row) => row.ticker),
    label
  );
  if (Object.hasOwn(drop, "day_realized_usd")) {
    assertOptionalDayRealized({
      USD: drop.USD,
      USDC: drop.USDC,
      day_realized_usd: drop.day_realized_usd,
    });
    const balance = agenticTotal(applied);
    const spent = Math.min(drop.day_realized_usd, 0);
    assertCentsEqual(view.runningBalance, balance, label);
    assertCentsEqual(view.bookUsd, holdingsSum(applied), label);
    assertCentsEqual(view.dayPnl, drop.day_realized_usd, label);
    assertCentsEqual(view.runningPnl, roundCents(balance - combinedSeed), label);
    assertCentsEqual(view.killHeadroom, killRemainingUsd(balance, drop.day_realized_usd), label);
    assertCentsEqual(
      view.killHeadroom,
      Math.max(0, roundCents(Math.abs(LIVE_RAILS.dayKillFrac) * balance + spent)),
      label
    );
  }
  for (const [file, text] of sources) {
    assertCashLiteralAbsent(text, drop.USD, `${label} ${file} USD`);
    assertCashLiteralAbsent(text, drop.USDC, `${label} ${file} USDC`);
  }
}

function assertLiveRails(view, book) {
  const base = view.runningBalance;
  const signal = book.signal_book_usd;
  const account = view.bookUsd;
  const published = book.book_usd;
  // Same fallback as cryptoBookView. A zero running balance is not the rail.
  const rail =
    base != null && base !== 0
      ? base
      : signal != null
        ? signal
        : account == null
          ? published
          : null;
  if (rail != null && rail !== 0) {
    assertCentsEqual(view.dayKill, railDollars(rail, LIVE_RAILS.dayKillFrac));
    assertCentsEqual(view.dayTarget, railDollars(rail, LIVE_RAILS.dayTargetFrac));
  } else if (rail === 0) {
    assertCentsEqual(view.dayKill, 0);
    assertCentsEqual(view.dayTarget, 0);
  } else {
    assert.equal(view.dayKill, null);
    assert.equal(view.dayTarget, null);
  }
  // Cents can match a different raw book. Each rail is a mismatch only when
  // its rounded dollars differ, not when signal !== rail.
  if (signal != null && rail != null && rail !== 0) {
    const signalKill = railDollars(signal, LIVE_RAILS.dayKillFrac);
    if (!approxCents(signalKill, railDollars(rail, LIVE_RAILS.dayKillFrac))) {
      assertCentsDiffer(view.dayKill, signalKill);
    }
    const signalTarget = railDollars(signal, LIVE_RAILS.dayTargetFrac);
    if (!approxCents(signalTarget, railDollars(rail, LIVE_RAILS.dayTargetFrac))) {
      assertCentsDiffer(view.dayTarget, signalTarget);
    }
  }
  if (base != null) {
    assertCentsEqual(view.killHeadroom, killRemainingUsd(base, book.day_pnl_usd));
  }
  if (rail == null || rail === 0 || view.killHeadroom == null) {
    assert.equal(view.killHeadroomFrac, null);
  } else {
    assert.ok(Math.abs(view.killHeadroomFrac - view.killHeadroom / rail) < 1e-12);
  }
}

test("rails that round to the same cents still follow the running balance", () => {
  const signal = 400;
  const base = 400.04;
  const stored = 40;
  assertCentsDiffer(base, signal);
  assertCentsEqual(railDollars(signal, LIVE_RAILS.dayKillFrac), railDollars(base, LIVE_RAILS.dayKillFrac));
  assertCentsEqual(railDollars(signal, LIVE_RAILS.dayTargetFrac), railDollars(base, LIVE_RAILS.dayTargetFrac));
  assertCentsEqual(railDollars(base, stored / signal), stored);
  const book = {
    signal_book_usd: signal,
    running_balance_usd: base,
    kill_remaining_usd: stored,
  };
  const view = cryptoBookView(book);
  assertCentsEqual(view.runningBalance, base);
  assertCentsEqual(view.dayTarget, railDollars(base, LIVE_RAILS.dayTargetFrac));
  assertCentsEqual(view.dayKill, railDollars(base, LIVE_RAILS.dayKillFrac));
  assertCentsEqual(view.killHeadroom, stored);
  assertLiveRails(view, book);
});

test("the published book is the holdings sum, and the rails use the running balance", () => {
  const books = shownBooks(live);
  const sum = holdingsSum(live);
  assert.equal(books.length, 1);
  assert.equal(books[0].sleeve, "crypto");
  assertCentsEqual(live.book_usd, sum);
  assertCentsEqual(books[0].bookUsd, sum);
  assertCentsEqual(books[0].bookUsd, live.book_usd);
  assertCentsEqual(books[0].runningBalance, agenticTotal(live));
  const openLots = openPositionRows(live);
  const cryptoOpen = openLots.some((row) => row.sleeve === "crypto");
  // A flat book has no open lot. A book that still holds must include crypto.
  if (openLots.length > 0) assert.ok(cryptoOpen);
  assert.deepEqual(tickers(live.positions), tickers(openLots));
  assert.deepEqual(tickers(openLots), tickers(openPositions.positions));
  assert.equal(live.positions.length, openLots.length);
  for (const row of live.positions) {
    const qty = Number(row.qty);
    assert.ok(Number.isFinite(qty) && qty !== 0);
    const name = String(row.ticker || "").trim().toUpperCase();
    assert.ok(name !== "USD" && name !== "USDC");
  }
  // Open lots sit on top of cash. With none, or with lots worth nothing, the
  // agentic total is the cash sum. A stored balance can also equal the signal
  // book, a seed, or a sleeve fraction. Those matches are still the agentic total.
  if (Array.isArray(live.positions)) {
    const cryptoLots = openLots.filter((row) => row.sleeve === "crypto");
    if (cryptoLots.every((row) => typeof row.valueUsd === "number")) {
      const lotValue = roundCents(cryptoLots.reduce((total, row) => total + row.valueUsd, 0));
      assertCentsDifferWhen(books[0].runningBalance, sum, !approxCents(lotValue, 0));
    }
  }
  assertCentsDifferWhen(
    books[0].runningBalance,
    live.signal_book_usd,
    !approxCents(agenticTotal(live), live.signal_book_usd)
  );
  assertCentsEqual(books[0].equitiesUsd, 0);
  assertCentsEqual(books[0].dayPnl, live.day_pnl_usd ?? null);
  assertLiveRails(books[0], live);
  assertCentsDifferWhen(books[0].bookUsd, live.signal_book_usd, !approxCents(sum, live.signal_book_usd));
  for (const seed of [SEEDS_USD.crypto, SEEDS_USD.equities, SEEDS_USD.combined]) {
    assertCentsDifferWhen(books[0].bookUsd, seed, !approxCents(sum, seed));
    assertCentsDifferWhen(books[0].runningBalance, seed, !approxCents(agenticTotal(live), seed));
  }
  assertCentsDifferWhen(
    books[0].runningBalance,
    seededBalance("crypto"),
    !approxCents(agenticTotal(live), seededBalance("crypto"))
  );
  assertCentsDifferWhen(
    books[0].runningBalance,
    seededBalance("equities"),
    !approxCents(agenticTotal(live), seededBalance("equities"))
  );
  if (live.running_balance_usd == null) {
    assertCentsDifferWhen(
      books[0].runningBalance,
      seededBalance("combined"),
      !approxCents(agenticTotal(live), seededBalance("combined"))
    );
  }
  assert.equal(books.some((book) => book.sleeve === "equities"), false);
  assert.equal(books.some((book) => book.sleeve === "combined"), false);
  const pnl = reconciledPnl(live);
  assertCentsEqual(pnl.marked, books[0].runningBalance);
  assertCentsEqual(books[0].runningPnl, pnl.running);
  assertCentsEqual(books[0].unrealizedPnl, pnl.unrealized);
  assertCentsEqual(books[0].realizedPnl, pnl.realized);
  assertCentsEqual(pnl.realized + pnl.unrealized, pnl.running);
  const expectedRunning = roundCents(books[0].runningBalance - combinedSeed);
  assertCentsEqual(pnl.running, expectedRunning);
  assertCentsEqual(books[0].runningPnl, expectedRunning);
  // Export writes running_pnl_usd from the same cash + lots - seed.
  assertCentsEqual(live.running_pnl_usd, expectedRunning);
  assertCentsEqual(live.running_balance_usd, books[0].runningBalance);
  assertCentsEqual(live.kill_remaining_usd, killRemainingUsd(books[0].runningBalance, live.day_pnl_usd));
  // Reconciled realized can print the same cents as the warehouse field.
  assertCentsDifferWhen(
    books[0].realizedPnl,
    live.realized_pnl_usd,
    !approxCents(pnl.realized, live.realized_pnl_usd)
  );
  assertCentsEqual(books[0].realizedPnl + books[0].unrealizedPnl, books[0].runningPnl);
  assert.equal(SEEDS_USD.combined, combinedSeed);
});

test("equities is not rendered as its own book", () => {
  assert.equal(html.includes('data-curve="equities"'), false);
  assert.equal(html.includes('data-curve="combined"'), false);
  assert.equal(html.includes("scrubbed seed"), false);
  assert.equal(app.includes("renderSleeve"), false);
  assert.equal(app.includes("open_positions.json"), false);
  assert.equal(app.includes("Legacy sum"), false);
  assert.equal(app.includes("shownBooks"), true);
});

test("open names are the live book holdings, not tape coins", () => {
  const rows = holdingRows(live);
  assert.deepEqual(
    rows.map((row) => [row.ticker, row.valueUsd]),
    cashRows(live)
  );
  assertHoldingsVersusBook(rows, live.book_usd);
  const tapeNames = new Set((openPositions.positions || []).map((row) => row.ticker));
  for (const row of rows) {
    if (row.ticker === "USD" || row.ticker === "USDC") continue;
    assert.equal(tapeNames.has(row.ticker), false);
  }
  const merged = mergeLiveBook(live, {
    book_usd: 775,
    candidates: [
      { symbol: "AVAX", side: "sell" },
      { symbol: "BTC", side: "sell" },
    ],
  });
  assert.equal(merged.candidates, undefined);
  assertCentsEqual(merged.book_usd, live.book_usd);
  assertCentsEqual(merged.signal_book_usd, 775);
  assertCentsDifferWhen(merged.book_usd, 775, !approxCents(live.book_usd, 775));
  assert.deepEqual(
    holdingRows(merged).map((row) => row.ticker),
    ["USD", "USDC"]
  );
});

test("a single nonzero cash row equals the book and two nonzero rows do not", () => {
  const shapes = [
    { name: "USD only", USD: 12.25, USDC: 0 },
    { name: "USDC only", USD: 0, USDC: 7.8 },
    { name: "both", USD: 12.25, USDC: 7.8 },
    { name: "all zero", USD: 0, USDC: 0 },
  ];
  for (const shape of shapes) {
    const book = {
      holdings: [
        { ticker: "USD", sleeve: "crypto", value_usd: shape.USD },
        { ticker: "USDC", sleeve: "crypto", value_usd: shape.USDC, cost_basis_usd: shape.USDC },
      ],
      positions: [],
    };
    book.book_usd = holdingsSum(book);
    const rows = holdingRows(book);
    const view = cryptoBookView(book);
    assert.equal(view == null, false, shape.name);
    assertCentsEqual(view.bookUsd, book.book_usd, shape.name);
    assertCentsEqual(view.runningBalance, book.book_usd, shape.name);
    assertHoldingsVersusBook(rows, view.bookUsd, shape.name);
    assertLiveRails(view, book);
  }
});

const publicSignal = {
  generated_at: "2026-10-04T20:41:10Z",
  book_usd: 775,
  day_pnl_usd: 2.25,
  kill_remaining_usd: 77.5,
  holdings: [{ ticker: "USDC", sleeve: "crypto", value_usd: 775 }],
  candidates: [
    { symbol: "AVAX", side: "sell" },
    { symbol: "BTC", side: "sell" },
  ],
};

test("account P&L stays on the card and is not a sleeve-seed fraction", () => {
  const old = {
    generated_at: "2026-10-04T16:34:23Z",
    book_usd: 775,
    day_pnl_usd: 1.5,
    kill_remaining_usd: 77.5,
    realized_pnl_usd: 8.5,
    unrealized_pnl_usd: 1.25,
    running_pnl_usd: 9.75,
    holdings: [{ ticker: "USDC", sleeve: "crypto" }],
  };
  const mergedOld = mergeLiveBook(old, publicSignal);
  const oldView = cryptoBookView(mergedOld);
  const oldRows = holdingRows(mergedOld);
  assert.deepEqual(
    oldRows.map((row) => [row.ticker, row.valueUsd]),
    [["USDC", null]]
  );
  assertCentsDiffer(oldRows[0].valueUsd, 775);
  assert.equal(oldView.runningBalance, null);
  assertCentsDiffer(oldView.runningBalance, 775);
  assertCentsEqual(mergedOld.realized_pnl_usd, 8.5);
  assertCentsEqual(mergedOld.running_pnl_usd, 9.75);
  assertCentsEqual(oldView.realizedPnl, 8.5);
  assertCentsEqual(oldView.runningPnl, 9.75);
  assert.notEqual(oldView.realizedPnl, null);
  assert.notEqual(oldView.runningPnl, null);
  assertCentsEqual(oldView.equitiesUsd, 0);
  assertCentsEqual(mergedOld.day_pnl_usd, old.day_pnl_usd);
  assertCentsDiffer(mergedOld.day_pnl_usd, publicSignal.day_pnl_usd);
  const keptDay = mergeLiveBook(old, { book_usd: 775, kill_remaining_usd: 77.5 });
  assertCentsEqual(keptDay.day_pnl_usd, old.day_pnl_usd);
  assertCentsEqual(keptDay.realized_pnl_usd, old.realized_pnl_usd);
  assertCentsEqual(keptDay.running_pnl_usd, old.running_pnl_usd);

  const crypto = summary.find((row) => row.sleeve === "crypto");
  const equities = summary.find((row) => row.sleeve === "equities");
  const snap = deriveSleeve(crypto);
  const withPnl = {
    ...live,
    realized_pnl_usd: 8.5,
    unrealized_pnl_usd: 1.25,
    running_pnl_usd: 9.75,
    running_balance_usd: 812.4,
    day_pnl_usd: 1.5,
  };
  const merged = mergeLiveBook(withPnl, publicSignal);
  const view = cryptoBookView(merged);
  const rows = holdingRows(merged);
  const sum = holdingsSum(live);
  assert.equal(merged.candidates, undefined);
  assert.deepEqual(
    rows.map((row) => [row.ticker, row.valueUsd]),
    cashRows(live)
  );
  for (const [ticker, value] of cashRows(live)) {
    const row = rows.find((item) => item.ticker === ticker);
    assertCentsDifferWhen(row.valueUsd, 775, !approxCents(value, 775), ticker);
  }
  assert.equal(view.sleeve, "crypto");
  assertCentsEqual(view.equitiesUsd, 0);
  assertCentsEqual(merged.book_usd, sum);
  assertCentsEqual(merged.signal_book_usd, 775);
  assertCentsDifferWhen(merged.book_usd, publicSignal.book_usd, !approxCents(sum, publicSignal.book_usd));
  assertCentsEqual(merged.day_pnl_usd, withPnl.day_pnl_usd);
  assertCentsDiffer(merged.day_pnl_usd, publicSignal.day_pnl_usd);
  const keptKill =
    withPnl.kill_remaining_usd == null ? publicSignal.kill_remaining_usd : withPnl.kill_remaining_usd;
  assertCentsEqual(merged.kill_remaining_usd, keptKill);
  assertCentsEqual(merged.realized_pnl_usd, withPnl.realized_pnl_usd);
  assertCentsEqual(merged.unrealized_pnl_usd, withPnl.unrealized_pnl_usd);
  assertCentsEqual(merged.running_pnl_usd, withPnl.running_pnl_usd);
  assertCentsEqual(merged.running_balance_usd, withPnl.running_balance_usd);
  assertCentsDifferWhen(merged.running_balance_usd, sum, !approxCents(withPnl.running_balance_usd, sum));
  assertCentsDiffer(merged.running_balance_usd, 775);
  assert.equal(merged.candidates, undefined);
  assertCentsEqual(view.bookUsd, sum);
  assertCentsEqual(view.runningBalance, agenticTotal(merged));
  assertCentsDifferWhen(
    view.runningBalance,
    withPnl.running_balance_usd,
    !approxCents(agenticTotal(merged), withPnl.running_balance_usd)
  );
  if (Array.isArray(merged.positions)) {
    const cardLots = openPositionRows(merged).filter((row) => row.sleeve === "crypto");
    if (cardLots.every((row) => typeof row.valueUsd === "number")) {
      const lotValue = roundCents(cardLots.reduce((total, row) => total + row.valueUsd, 0));
      assertCentsDifferWhen(view.runningBalance, sum, !approxCents(lotValue, 0));
    }
  }
  assertCentsDifferWhen(view.runningBalance, 775, !approxCents(agenticTotal(merged), 775));
  assertCentsDifferWhen(view.bookUsd, 775, !approxCents(sum, 775));
  assertCentsDifferWhen(view.bookUsd, publicSignal.book_usd, !approxCents(sum, publicSignal.book_usd));
  assertCentsEqual(view.dayPnl, withPnl.day_pnl_usd);
  assertLiveRails(view, merged);
  const card = reconciledPnl(merged);
  assertCentsEqual(view.realizedPnl, card.realized);
  assertCentsEqual(view.unrealizedPnl, card.unrealized);
  assertCentsEqual(view.runningPnl, card.running);
  assertCentsDifferWhen(
    view.realizedPnl,
    withPnl.realized_pnl_usd,
    !approxCents(card.realized, withPnl.realized_pnl_usd)
  );
  assertCentsDifferWhen(
    view.runningPnl,
    withPnl.running_pnl_usd,
    !approxCents(card.running, withPnl.running_pnl_usd)
  );
  assert.notEqual(view.realizedPnl, null);
  assert.notEqual(view.runningPnl, null);
  const seedDollars = Math.round((SEEDS_USD.crypto * crypto.realized_pnl_frac + Number.EPSILON) * 100) / 100;
  const runningSeedDollars =
    Math.round((SEEDS_USD.crypto * crypto.running_pnl_frac + Number.EPSILON) * 100) / 100;
  assertCentsDifferWhen(view.realizedPnl, seedDollars, !approxCents(card.realized, seedDollars));
  assertCentsDifferWhen(view.runningPnl, runningSeedDollars, !approxCents(card.running, runningSeedDollars));
  if (card.marked) {
    assert.ok(Math.abs(view.realizedPnlFrac - card.realized / card.marked) < 1e-12);
  } else {
    assert.equal(view.realizedPnlFrac, null);
  }
  const storedRealizedFrac = withPnl.realized_pnl_usd / SEEDS_USD.crypto;
  const cardRealizedFrac = card.marked ? card.realized / card.marked : null;
  const fracApart =
    cardRealizedFrac == null || storedRealizedFrac == null
      ? cardRealizedFrac !== storedRealizedFrac
      : !approxFrac(cardRealizedFrac, storedRealizedFrac);
  assertFracDifferWhen(view.realizedPnlFrac, storedRealizedFrac, fracApart);
  assertCentsDifferWhen(
    view.runningBalance,
    snap.runningBalance,
    !approxCents(agenticTotal(merged), snap.runningBalance)
  );
  assertCentsDifferWhen(view.realizedPnl, snap.realizedPnl, !approxCents(card.realized, snap.realizedPnl));
  assertCentsDifferWhen(view.runningPnl, snap.runningPnl, !approxCents(card.running, snap.runningPnl));
  const equitySnap = deriveSleeve(equities);
  assertCentsDifferWhen(
    view.realizedPnl,
    equitySnap.realizedPnl,
    !approxCents(card.realized, equitySnap.realizedPnl)
  );
  const published = JSON.stringify(live);
  const liveCard = cryptoBookView(live);
  const livePnl = reconciledPnl(live);
  assertCentsEqual(liveCard.realizedPnl, livePnl.realized);
  assertCentsEqual(liveCard.unrealizedPnl, livePnl.unrealized);
  assertCentsEqual(liveCard.runningPnl, livePnl.running);
  assertCentsEqual(liveCard.runningPnl, roundCents(liveCard.runningBalance - combinedSeed));
  assert.equal(published.includes("\"realized_pnl_usd\": null"), false);
  assert.equal(published.includes("\"running_pnl_usd\": null"), false);
  const tapeNames = (openPositions.positions || []).map((row) => row.ticker);
  for (const name of tapeNames) {
    if (name === "USD" || name === "USDC") continue;
    assert.equal(rows.some((row) => row.ticker === name), false, name);
  }
  assert.equal(app.includes('formatUsd(view.runningBalance)'), true);
  assert.equal(app.includes('formatUsd(view.bookUsd)'), false);
  assert.equal(app.includes("Realized P&L"), true);
  assert.equal(app.includes("Unrealized P&L"), true);
  assert.equal(app.includes("Running P&L"), true);
  assert.equal(app.includes("Signal book_usd"), false);
  assert.equal(html.includes("The line is not the live account."), true);
  assert.equal(html.includes("The live book is the signal value"), false);
  assert.equal(app.includes("The account book is the signal value"), false);
  assert.equal(app.includes("Crypto sleeve snapshot"), false);
  assert.equal(app.includes("Seed \\u00d7 fraction"), false);
  assert.equal(app.includes("renderSleeve"), false);
  assert.equal(app.includes("open_positions.json"), false);
});

test("export writes book_usd as the holdings sum and does not copy the signal book", () => {
  for (const file of [".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    const start = text.indexOf("Commit refreshed JSON");
    const end = text.indexOf("Publish export failure", start);
    const commit = text.slice(start, end);
    const adds = commit.split("\n").filter((line) => line.includes("git add"));
    assert.ok(adds.some((line) => line.includes("data/live_book.json")), file);
    assert.match(commit, /sum of holding values/);
    assert.match(commit, /does not copy the signal book/);
  }
  const exporter = readFileSync(new URL("../scripts/export_kpi.py", import.meta.url), "utf8");
  const derive = readFileSync(new URL("../derive.js", import.meta.url), "utf8");
  assert.equal(exporter.includes("def merge_live_book"), true);
  assert.equal(exporter.includes("def refresh_live_book"), true);
  assert.equal(exporter.includes("def apply_rh_cash"), true);
  assert.equal(exporter.includes("def load_rh_cash"), true);
  assert.equal(exporter.includes("def load_rh_cash_drop"), true);
  assert.equal(exporter.includes("/api/v2/crypto/trading/accounts/"), true);
  assert.equal(exporter.includes("/api/v2/crypto/trading/holdings/"), true);
  assert.equal(exporter.includes("buying_power"), true);
  for (const file of [".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"]) {
    const workflow = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    const exportStep = workflow.split("Export scrubbed Supabase views")[1].split("Commit refreshed JSON")[0];
    assert.match(exportStep, /RH_API_KEY/, file);
    assert.match(exportStep, /RH_BASE64_PRIVATE_KEY/, file);
  }
  assert.equal(exporter.includes("def latest_account_pnl"), true);
  assert.equal(exporter.includes("public.kpi_sleeve_snapshots"), true);
  assert.equal(exporter.includes("realized_pnl_usd"), true);
  assert.equal(exporter.includes("unrealized_pnl_usd"), true);
  assert.equal(exporter.includes("running_pnl_usd"), true);
  assert.equal(exporter.includes("running_balance_usd"), true);
  assert.equal(exporter.includes("refresh_live_book(DATA)"), true);
  const rest = exporter.split("def _fetch_account_snapshot_rest")[1].split("def _fetch_account_snapshot_db")[0];
  assert.match(rest, /eq\.combined/);
  assert.match(rest, /"limit": "1"/);
  assert.equal(rest.includes('"limit": "6"'), false);
  assert.equal(rest.includes("day_pnl_usd"), false);
  assert.equal(exporter.includes("clear_missing_day"), false);
  assert.equal(exporter.includes("out.pop(\"day_pnl_usd\""), false);
  assert.equal(derive.includes("next.day_pnl_usd = day"), true);
  assert.equal(derive.includes("runningBalance: account"), false);
  assert.equal(derive.includes("const realizedPnl = null"), false);
  assert.equal(derive.includes("const runningPnl = null"), false);
  assert.equal(derive.includes("delete base.realized_pnl"), false);
  assert.equal(derive.includes("delete base.running_pnl"), false);
});

test("a cash fingerprint matches only a standalone numeric literal", () => {
  assert.equal(standaloneNumericLiteral("1.85", 1.8), false);
  assert.equal(standaloneNumericLiteral("= 1.8\n", 1.8), true);
  assert.equal(standaloneNumericLiteral("1.80", 1.8), true);
  assert.equal(standaloneNumericLiteral("1.800", 1.8), false);
  assert.equal(standaloneNumericLiteral("a1.8", 1.8), false);
  assert.equal(standaloneNumericLiteral("21.8", 1.8), false);
  assert.equal(standaloneNumericLiteral("0", 0), null);
  assert.equal(standaloneNumericLiteral("0.40", 0.4), null);
});

test("a cash drop replaces USD and USDC and unset REST keys do not fail export", () => {
  const liveDrop = JSON.parse(readFileSync(new URL("../data/rh_cash.json", import.meta.url), "utf8"));
  const sources = [
    ["scripts/export_kpi.py", readFileSync(new URL("../scripts/export_kpi.py", import.meta.url), "utf8")],
    ["scripts/account_book.py", readFileSync(new URL("../scripts/account_book.py", import.meta.url), "utf8")],
    ["scripts/recon_kpi_realized.py", readFileSync(new URL("../scripts/recon_kpi_realized.py", import.meta.url), "utf8")],
    ["scripts/refresh_kpi_snapshots.py", readFileSync(new URL("../scripts/refresh_kpi_snapshots.py", import.meta.url), "utf8")],
    ["derive.js", readFileSync(new URL("../derive.js", import.meta.url), "utf8")],
    ["app.js", readFileSync(new URL("../app.js", import.meta.url), "utf8")],
    ["index.html", readFileSync(new URL("../index.html", import.meta.url), "utf8")],
  ];
  for (const drop of [liveDrop, ...CASH_DROP_FIXTURES]) assertCashDrop(drop, sources);
  const exporter = sources[0][1];
  const main = exporter.split("\ndef main(argv")[1];
  const restAt = main.indexOf("rest_cash = load_rh_cash()");
  const skipAt = main.indexOf("REST cash skipped");
  const dropAt = main.indexOf("load_rh_cash_drop(DATA)");
  assert.ok(restAt >= 0 && restAt < skipAt && skipAt < dropAt);
  assert.equal(main.includes("Export KPI expected live Robinhood cash"), false);
  assert.equal(main.includes("cash=cash"), true);
  assert.equal(exporter.includes("day_realized_usd"), true);
  assert.equal(exporter.includes("day_realized_gross_usd"), true);
  assert.equal(exporter.includes("day_sell_fees_usd"), true);
  assert.equal(exporter.includes("differs from day_realized_usd"), true);
  const killSrc = exporter.split("def kill_remaining_usd")[1].split("\ndef ")[0];
  assert.equal(killSrc.includes("0.10"), false);
  assert.equal(killSrc.includes("0.1"), false);
  for (const file of [".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"]) {
    const workflow = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.match(workflow, /Hourly Actions poll skipped/, file);
    const exportStep = workflow.split("Export scrubbed Supabase views")[1].split("Commit refreshed JSON")[0];
    assert.match(exportStep, /RH_API_KEY: \$\{\{ secrets\.RH_API_KEY \}\}/, file);
    assert.match(exportStep, /RH_BASE64_PRIVATE_KEY: \$\{\{ secrets\.RH_BASE64_PRIVATE_KEY \}\}/, file);
    assert.equal(exportStep.includes("Export KPI expected live Robinhood cash"), false, file);
  }
});

test("a cash drop may include day realized net, gross, and sell fees", () => {
  const ordinary = CASH_DROP_FIXTURES.find((drop) => drop.name === "ordinary book");
  const rolled = CASH_DROP_FIXTURES.find((drop) => drop.name === "new ET day");
  assertOptionalDayRealized(ordinary);
  assertOptionalDayRealized(rolled);
  assertOptionalDayRealized({
    USD: ordinary.USD,
    USDC: ordinary.USDC,
    day_realized_usd: ordinary.day_realized_usd,
  });
  assert.throws(() => assertOptionalDayRealized({ day_realized_usd: Number.NaN }));
});

test("open nets stay beside cash and the rails follow the running balance", () => {
  const withOpen = {
    ...live,
    positions: [
      {
        sleeve: "crypto",
        ticker: "ZZ",
        qty: "3",
        mark: "12.5",
        value_usd: 37.5,
        unrealized_pnl_usd: 7.5,
        running_pnl_usd: 9.5,
      },
      { sleeve: "equities", ticker: "QQ", qty: "0", value_usd: 4 },
      { sleeve: "crypto", ticker: "USDC", qty: "9", value_usd: 9 },
    ],
  };
  const merged = mergeLiveBook(withOpen, {
    ...publicSignal,
    positions: [{ sleeve: "crypto", ticker: "NOPE", qty: "9", value_usd: 1 }],
    holdings: [{ ticker: "USDC", sleeve: "crypto", value_usd: 775 }],
  });
  const view = cryptoBookView(merged);
  const sum = holdingsSum(live);
  const lotUsd = withOpen.positions[0].value_usd;
  const lot = withOpen.positions[0];
  assertCentsEqual(merged.book_usd, sum);
  assertCentsEqual(merged.kill_remaining_usd, live.kill_remaining_usd);
  assertCentsEqual(merged.day_pnl_usd, live.day_pnl_usd);
  assertCentsEqual(merged.signal_book_usd, 775);
  assert.deepEqual(
    merged.positions.map((row) => row.ticker),
    ["ZZ", "QQ", "USDC"]
  );
  assertCentsEqual(view.bookUsd, sum);
  const openCard = reconciledPnl(merged);
  assertCentsEqual(view.runningPnl, openCard.running);
  assertCentsEqual(view.unrealizedPnl, withOpen.positions[0].unrealized_pnl_usd);
  assertCentsEqual(view.realizedPnl, openCard.realized);
  assertCentsEqual(view.realizedPnl + view.unrealizedPnl, view.runningPnl);
  assertCentsDifferWhen(
    view.runningPnl,
    live.running_pnl_usd ?? null,
    live.running_pnl_usd == null || !approxCents(openCard.running, live.running_pnl_usd)
  );
  assertCentsEqual(view.dayPnl, live.day_pnl_usd);
  assertLiveRails(view, merged);
  assertCentsEqual(view.runningBalance, sum + lotUsd);
  const cashKill = railDollars(sum, LIVE_RAILS.dayKillFrac);
  assertCentsDifferWhen(
    view.dayKill,
    cashKill,
    !approxCents(railDollars(view.runningBalance, LIVE_RAILS.dayKillFrac), cashKill)
  );
  assertCentsEqual(view.equitiesUsd, 0);
  assertCentsDifferWhen(view.bookUsd, sum + lotUsd, !approxCents(lotUsd, 0));
  assertCentsEqual(view.runningBalance, sum + lotUsd);
  assert.deepEqual(
    holdingRows(merged).map((row) => row.ticker),
    ["USD", "USDC"]
  );
  assert.deepEqual(
    cardPositionRows(merged).map((row) => [row.ticker, row.qty, row.valueUsd, row.runningPnl]),
    [
      ...cashRows(live).map(([ticker, valueUsd]) => [ticker, null, valueUsd, null]),
      [lot.ticker, Number(lot.qty), lot.value_usd, lot.running_pnl_usd],
    ]
  );
  assert.equal(app.includes('["Ticker", "Qty", "Value", "Running P&L"]'), true);
  assert.equal(openPositionRows(merged).some((row) => row.ticker === "QQ"), false);
  assert.equal(openPositionRows(merged).some((row) => row.ticker === "USDC"), false);
  assert.equal(shownBooks(merged).length, 1);
  assert.equal(app.includes("cardPositionRows"), true);
  assert.equal(app.includes("open_positions.json"), false);
});

test("a float sum of lot marks matches at cents and misses exact equality", () => {
  const marks = [0.1, 0.2];
  const raw = marks.reduce((total, mark) => total + mark, 0);
  const cents = Math.round(raw * 100) / 100;
  assert.notEqual(raw, cents);
  assertCentsEqual(raw, cents);

  const book = {
    holdings: [{ ticker: "USD", sleeve: "crypto", value_usd: marks[0] }],
    positions: [
      { sleeve: "crypto", ticker: "ZZ", qty: "1", value_usd: marks[1], unrealized_pnl_usd: 0 },
    ],
  };
  const view = cryptoBookView(book);
  const naive = holdingsSum(book) + marks[1];
  assert.notEqual(view.runningBalance, naive);
  assertCentsEqual(view.runningBalance, naive);
  assertCentsEqual(view.runningBalance, raw);
  assertCentsDiffer(view.bookUsd, view.runningBalance);
});

test("running balance is cash plus lots, and kill headroom follows the day rail", () => {
  const book = {
    book_usd: 30,
    signal_book_usd: 1000,
    day_pnl_usd: -5,
    kill_remaining_usd: 100,
    running_balance_usd: 9999,
    running_pnl_usd: 1,
    holdings: [
      { ticker: "USD", sleeve: "crypto", value_usd: 20 },
      { ticker: "USDC", sleeve: "crypto", value_usd: 10, cost_basis_usd: 10 },
    ],
    positions: [{ sleeve: "crypto", ticker: "ZZ", qty: "4", value_usd: 170, unrealized_pnl_usd: 8 }],
  };
  const view = cryptoBookView(book);
  const cash = book.holdings.reduce((total, row) => total + row.value_usd, 0);
  const lots = book.positions.reduce((total, row) => total + row.value_usd, 0);
  assertCentsEqual(view.bookUsd, book.book_usd);
  assertCentsEqual(view.runningBalance, cash + lots);
  assertCentsDiffer(view.runningBalance, book.running_balance_usd);
  assertCentsDiffer(view.bookUsd, view.runningBalance);
  const budget = railDollars(cash + lots, Math.abs(LIVE_RAILS.dayKillFrac));
  assertCentsEqual(view.killHeadroom, killRemainingUsd(cash + lots, book.day_pnl_usd));
  assertCentsEqual(view.killHeadroom, railDollars(1, budget + book.day_pnl_usd));
  assertCentsDiffer(view.killHeadroom, railDollars(cash + lots, book.kill_remaining_usd / book.signal_book_usd));
  assertCentsEqual(view.runningPnl, railDollars(1, cash + lots - SEEDS_USD.combined));
  assertCentsEqual(view.dayPnl, book.day_pnl_usd);

  const profit = cryptoBookView({ ...book, day_pnl_usd: -book.day_pnl_usd + 1 });
  assertCentsEqual(profit.killHeadroom, killRemainingUsd(cash + lots, profit.dayPnl));
  assertCentsEqual(profit.killHeadroom, killRemainingUsd(cash + lots, 0));
  assertCentsEqual(profit.dayPnl, -book.day_pnl_usd + 1);

  const stopped = cryptoBookView({ ...book, day_pnl_usd: -(cash + lots) });
  assertCentsEqual(stopped.killHeadroom, 0);

  const kept = mergeLiveBook(book, { ...publicSignal, day_pnl_usd: 9, kill_remaining_usd: 3 });
  assertCentsEqual(kept.day_pnl_usd, book.day_pnl_usd);
  assertCentsEqual(kept.kill_remaining_usd, book.kill_remaining_usd);
  assertCentsEqual(kept.book_usd, book.book_usd);
  assertCentsEqual(kept.signal_book_usd, publicSignal.book_usd);

  const blankAccount = { holdings: [{ ticker: "USD", sleeve: "crypto", value_usd: 12 }], book_usd: 12 };
  const blankSignal = { book_usd: 40, day_pnl_usd: 1.25, kill_remaining_usd: 4 };
  const blank = mergeLiveBook(blankAccount, blankSignal);
  assertCentsEqual(blank.day_pnl_usd, blankSignal.day_pnl_usd);
  assertCentsEqual(blank.kill_remaining_usd, blankSignal.kill_remaining_usd);
  assertCentsEqual(blank.book_usd, blankAccount.book_usd);
  assertCentsEqual(blank.signal_book_usd, blankSignal.book_usd);
});

test("the page does not hardcode live open quantities", () => {
  const opens = JSON.parse(readFileSync(new URL("../data/open_positions.json", import.meta.url), "utf8"));
  const named = (opens.positions || []).filter((row) => row.ticker !== "USD" && row.ticker !== "USDC");
  const qtys = named.map((row) => String(row.qty ?? "")).filter((qty) => qty.includes("."));
  // No live decimal qty to guard when the book is flat.
  if (named.length > 0) assert.ok(qtys.length > 0);
  const files = ["app.js", "derive.js", "index.html", "scripts/test_live_book.mjs"];
  for (const file of files) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    for (const qty of qtys) assert.equal(sourceHasLiveDecimal(text, qty), false, `${file} ${qty}`);
  }
});

test("the page does not hardcode the holdings sum in place of the writer", () => {
  const view = cryptoBookView(live);
  const sum = holdingsSum(live);
  assertCentsEqual(view.bookUsd, live.book_usd);
  assertCentsEqual(view.bookUsd, sum);
  for (const file of ["derive.js", "app.js", "index.html"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.equal(text.includes("774.71"), false, file);
  }
  const published = readFileSync(new URL("../data/live_book.json", import.meta.url), "utf8");
  assertCashLiteralPresent(published, sum);
  assert.equal(deriveSourceHasSeedRebuild(published), false);
  for (const banned of ["789.60", "-1.17", "-9.23", "-10.40"]) {
    for (const file of ["derive.js", "app.js", "index.html"]) {
      const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
      assert.equal(text.includes(banned), false, `${file} ${banned}`);
    }
  }
});

function deriveSourceHasSeedRebuild(published) {
  return published.includes("realized_pnl_frac") || published.includes("running_pnl_frac");
}

test("the book card clock is the warehouse sleeve as_of, not the signal generated_at", () => {
  const signalTime = "2026-10-04T16:34:23Z";
  const warehouseTime = "2026-10-05T08:41:26+00:00";
  // sleeve_as_of and kpi_summary as_of are written by different commits.
  // A stamp already on the book is the card clock even when the summary is newer.
  assert.equal(warehouseSleeveAsOf(live, summary), live.sleeve_as_of);
  assert.equal(cryptoBookView(live).asOf, live.sleeve_as_of);
  if (live.sleeve_as_of !== live.generated_at) {
    assert.notEqual(cryptoBookView(live).asOf, live.generated_at);
  } else {
    assert.equal(cryptoBookView(live).asOf, live.sleeve_as_of);
  }
  assert.equal(cryptoBookView(live).signalGeneratedAt, live.generated_at);

  const merged = mergeLiveBook(
    { ...live, generated_at: signalTime, sleeve_as_of: warehouseTime },
    { ...publicSignal, generated_at: "2026-10-04T20:41:10Z", sleeve_as_of: "1999-01-01T00:00:00Z" }
  );
  assert.equal(merged.generated_at, "2026-10-04T20:41:10Z");
  assert.equal(merged.sleeve_as_of, warehouseTime);
  assertCentsEqual(merged.day_pnl_usd, live.day_pnl_usd);
  const view = cryptoBookView(merged);
  assert.equal(view.asOf, warehouseTime);
  assert.notEqual(view.asOf, merged.generated_at);
  assert.equal(view.signalGeneratedAt, merged.generated_at);
  assertCentsEqual(view.dayPnl, live.day_pnl_usd);

  const unstamped = {
    book_usd: 100,
    signal_book_usd: 100,
    generated_at: signalTime,
    holdings: [{ ticker: "USD", sleeve: "crypto", value_usd: 100 }],
  };
  const fromSummary = cryptoBookView(unstamped, [
    { sleeve: "crypto", as_of: "2026-10-05T01:00:00Z" },
    { sleeve: "combined", as_of: "2026-10-04T00:00:00Z" },
    { sleeve: "combined", as_of: warehouseTime },
  ]);
  assert.equal(fromSummary.asOf, warehouseTime);
  assert.equal(fromSummary.signalGeneratedAt, signalTime);
  assert.notEqual(fromSummary.asOf, signalTime);
  assert.equal(warehouseSleeveAsOf(unstamped), null);

  const derive = readFileSync(new URL("../derive.js", import.meta.url), "utf8");
  const exporter = readFileSync(new URL("../scripts/export_kpi.py", import.meta.url), "utf8");
  assert.equal(derive.includes("asOf: book?.generated_at"), false);
  assert.equal(derive.includes("warehouseSleeveAsOf"), true);
  assert.equal(app.includes("shownBooks(book, summary)"), true);
  assert.equal(app.includes("render(trades, meta, book, summary)"), true);
  assert.equal(app.includes("warehouseSleeveAsOf(book, summary)"), true);
  assert.equal(app.includes("does not refresh kpi_sleeve_snapshots"), false);
  assert.equal(app.includes("Export only re-reads kpi_summary"), false);
  assert.match(app, /The sleeve clock is kpi_sleeve_snapshots as_of/);
  assert.equal(exporter.includes('out["sleeve_as_of"] = sleeve_as_of'), true);
  assert.equal(exporter.includes('out["sleeve_as_of"] = jsonable(as_of)'), true);
});
