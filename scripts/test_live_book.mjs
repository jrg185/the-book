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

// Combined seed the export subtracts from cash + lots. Read from the writer,
// not a second copy of the dollar figure.
function exportCombinedSeed() {
  const text = readFileSync(new URL("./export_kpi.py", import.meta.url), "utf8");
  const body = text.split("BOOK_SEEDS = {")[1]?.split("}")[0] ?? "";
  const match = body.match(/"combined"\s*:\s*Decimal\("(-?\d+(?:\.\d+)?)"\)/);
  assert.ok(match, "export BOOK_SEEDS combined");
  return Number(match[1]);
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

function assertLiveRails(view, book) {
  const base = view.runningBalance;
  assert.ok(base != null && base !== 0);
  assert.equal(view.dayKill, railDollars(base, LIVE_RAILS.dayKillFrac));
  assert.equal(view.dayTarget, railDollars(base, LIVE_RAILS.dayTargetFrac));
  const signal = book.signal_book_usd;
  // Cents can match a different raw book. Each rail is a mismatch only when
  // its rounded dollars differ, not when signal !== base.
  if (signal != null) {
    const signalKill = railDollars(signal, LIVE_RAILS.dayKillFrac);
    if (signalKill !== railDollars(base, LIVE_RAILS.dayKillFrac)) {
      assert.notEqual(view.dayKill, signalKill);
    }
    const signalTarget = railDollars(signal, LIVE_RAILS.dayTargetFrac);
    if (signalTarget !== railDollars(base, LIVE_RAILS.dayTargetFrac)) {
      assert.notEqual(view.dayTarget, signalTarget);
    }
  }
  assert.equal(view.killHeadroom, killRemainingUsd(base, book.day_pnl_usd));
  assert.ok(Math.abs(view.killHeadroomFrac - view.killHeadroom / base) < 1e-12);
}

test("rails that round to the same cents still follow the running balance", () => {
  const signal = 400;
  const base = 400.04;
  const stored = 40;
  assert.notEqual(base, signal);
  assert.equal(railDollars(signal, LIVE_RAILS.dayKillFrac), railDollars(base, LIVE_RAILS.dayKillFrac));
  assert.equal(railDollars(signal, LIVE_RAILS.dayTargetFrac), railDollars(base, LIVE_RAILS.dayTargetFrac));
  assert.equal(railDollars(base, stored / signal), stored);
  const book = {
    signal_book_usd: signal,
    running_balance_usd: base,
    kill_remaining_usd: stored,
  };
  const view = cryptoBookView(book);
  assert.equal(view.runningBalance, base);
  assert.equal(view.dayTarget, railDollars(base, LIVE_RAILS.dayTargetFrac));
  assert.equal(view.dayKill, railDollars(base, LIVE_RAILS.dayKillFrac));
  assert.equal(view.killHeadroom, stored);
  assertLiveRails(view, book);
});

test("the published book is the holdings sum, and the rails use the running balance", () => {
  const books = shownBooks(live);
  const sum = holdingsSum(live);
  assert.equal(books.length, 1);
  assert.equal(books[0].sleeve, "crypto");
  assert.equal(live.book_usd, sum);
  assert.equal(books[0].bookUsd, sum);
  assert.equal(books[0].bookUsd, live.book_usd);
  assert.equal(books[0].runningBalance, agenticTotal(live));
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
  // Open lots sit on top of cash. With none, the agentic total is the cash sum.
  if (cryptoOpen) assert.notEqual(books[0].runningBalance, sum);
  assert.notEqual(books[0].runningBalance, live.signal_book_usd);
  assert.equal(books[0].equitiesUsd, 0);
  assert.equal(books[0].dayPnl, live.day_pnl_usd ?? null);
  assertLiveRails(books[0], live);
  assert.notEqual(books[0].bookUsd, live.signal_book_usd);
  for (const seed of [SEEDS_USD.crypto, SEEDS_USD.equities, SEEDS_USD.combined]) {
    assert.notEqual(books[0].bookUsd, seed);
    assert.notEqual(books[0].runningBalance, seed);
  }
  assert.notEqual(books[0].runningBalance, seededBalance("crypto"));
  assert.notEqual(books[0].runningBalance, seededBalance("equities"));
  if (live.running_balance_usd == null) {
    assert.notEqual(books[0].runningBalance, seededBalance("combined"));
  }
  assert.equal(books.some((book) => book.sleeve === "equities"), false);
  assert.equal(books.some((book) => book.sleeve === "combined"), false);
  const pnl = reconciledPnl(live);
  assert.equal(pnl.marked, books[0].runningBalance);
  assert.equal(books[0].runningPnl, pnl.running);
  assert.equal(books[0].unrealizedPnl, pnl.unrealized);
  assert.equal(books[0].realizedPnl, pnl.realized);
  assert.equal(Math.round((pnl.realized + pnl.unrealized) * 100) / 100, pnl.running);
  const expectedRunning = roundCents(books[0].runningBalance - combinedSeed);
  assert.equal(pnl.running, expectedRunning);
  assert.equal(books[0].runningPnl, expectedRunning);
  // Export writes running_pnl_usd from the same cash + lots - seed.
  assert.equal(live.running_pnl_usd, expectedRunning);
  assert.equal(live.running_balance_usd, books[0].runningBalance);
  assert.equal(live.kill_remaining_usd, killRemainingUsd(books[0].runningBalance, live.day_pnl_usd));
  assert.notEqual(books[0].realizedPnl, live.realized_pnl_usd);
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
  assert.ok(rows.every((row) => row.valueUsd !== live.book_usd));
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
  assert.equal(merged.book_usd, live.book_usd);
  assert.equal(merged.signal_book_usd, 775);
  assert.notEqual(merged.book_usd, 775);
  assert.deepEqual(
    holdingRows(merged).map((row) => row.ticker),
    ["USD", "USDC"]
  );
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
  assert.notEqual(oldRows[0].valueUsd, 775);
  assert.equal(oldView.runningBalance, null);
  assert.notEqual(oldView.runningBalance, 775);
  assert.equal(mergedOld.realized_pnl_usd, 8.5);
  assert.equal(mergedOld.running_pnl_usd, 9.75);
  assert.equal(oldView.realizedPnl, 8.5);
  assert.equal(oldView.runningPnl, 9.75);
  assert.notEqual(oldView.realizedPnl, null);
  assert.notEqual(oldView.runningPnl, null);
  assert.equal(oldView.equitiesUsd, 0);
  assert.equal(mergedOld.day_pnl_usd, old.day_pnl_usd);
  assert.notEqual(mergedOld.day_pnl_usd, publicSignal.day_pnl_usd);
  const keptDay = mergeLiveBook(old, { book_usd: 775, kill_remaining_usd: 77.5 });
  assert.equal(keptDay.day_pnl_usd, old.day_pnl_usd);
  assert.equal(keptDay.realized_pnl_usd, old.realized_pnl_usd);
  assert.equal(keptDay.running_pnl_usd, old.running_pnl_usd);

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
  assert.ok(rows.every((row) => row.valueUsd !== 775));
  assert.equal(view.sleeve, "crypto");
  assert.equal(view.equitiesUsd, 0);
  assert.equal(merged.book_usd, sum);
  assert.equal(merged.signal_book_usd, 775);
  assert.notEqual(merged.book_usd, publicSignal.book_usd);
  assert.equal(merged.day_pnl_usd, withPnl.day_pnl_usd);
  assert.notEqual(merged.day_pnl_usd, publicSignal.day_pnl_usd);
  const keptKill =
    withPnl.kill_remaining_usd == null ? publicSignal.kill_remaining_usd : withPnl.kill_remaining_usd;
  assert.equal(merged.kill_remaining_usd, keptKill);
  assert.equal(merged.realized_pnl_usd, withPnl.realized_pnl_usd);
  assert.equal(merged.unrealized_pnl_usd, withPnl.unrealized_pnl_usd);
  assert.equal(merged.running_pnl_usd, withPnl.running_pnl_usd);
  assert.equal(merged.running_balance_usd, withPnl.running_balance_usd);
  assert.notEqual(merged.running_balance_usd, sum);
  assert.notEqual(merged.running_balance_usd, 775);
  assert.equal(merged.candidates, undefined);
  assert.equal(view.bookUsd, sum);
  assert.equal(view.runningBalance, agenticTotal(merged));
  assert.notEqual(view.runningBalance, withPnl.running_balance_usd);
  if (openPositionRows(merged).some((row) => row.sleeve === "crypto")) {
    assert.notEqual(view.runningBalance, sum);
  }
  assert.notEqual(view.runningBalance, 775);
  assert.notEqual(view.bookUsd, 775);
  assert.notEqual(view.bookUsd, publicSignal.book_usd);
  assert.equal(view.dayPnl, withPnl.day_pnl_usd);
  assertLiveRails(view, merged);
  const card = reconciledPnl(merged);
  assert.equal(view.realizedPnl, card.realized);
  assert.equal(view.unrealizedPnl, card.unrealized);
  assert.equal(view.runningPnl, card.running);
  assert.notEqual(view.realizedPnl, withPnl.realized_pnl_usd);
  assert.notEqual(view.runningPnl, withPnl.running_pnl_usd);
  assert.notEqual(view.realizedPnl, null);
  assert.notEqual(view.runningPnl, null);
  const seedDollars = Math.round((SEEDS_USD.crypto * crypto.realized_pnl_frac + Number.EPSILON) * 100) / 100;
  assert.notEqual(view.realizedPnl, seedDollars);
  assert.notEqual(view.runningPnl, Math.round((SEEDS_USD.crypto * crypto.running_pnl_frac + Number.EPSILON) * 100) / 100);
  assert.ok(Math.abs(view.realizedPnlFrac - card.realized / card.marked) < 1e-12);
  assert.notEqual(view.realizedPnlFrac, withPnl.realized_pnl_usd / SEEDS_USD.crypto);
  assert.notEqual(view.runningBalance, snap.runningBalance);
  assert.notEqual(view.realizedPnl, snap.realizedPnl);
  assert.notEqual(view.runningPnl, snap.runningPnl);
  assert.notEqual(view.realizedPnl, deriveSleeve(equities).realizedPnl);
  const published = JSON.stringify(live);
  const liveCard = cryptoBookView(live);
  const livePnl = reconciledPnl(live);
  assert.equal(liveCard.realizedPnl, livePnl.realized);
  assert.equal(liveCard.unrealizedPnl, livePnl.unrealized);
  assert.equal(liveCard.runningPnl, livePnl.running);
  assert.equal(liveCard.runningPnl, roundCents(liveCard.runningBalance - combinedSeed));
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

test("a cash drop replaces USD and USDC and unset REST keys do not fail export", () => {
  const drop = JSON.parse(readFileSync(new URL("../data/rh_cash.json", import.meta.url), "utf8"));
  assert.equal(typeof drop.USD, "number");
  assert.equal(typeof drop.USDC, "number");
  assert.equal(Number.isFinite(drop.USD), true);
  assert.equal(Number.isFinite(drop.USDC), true);
  if (drop.as_of != null) {
    assert.equal(typeof drop.as_of, "string");
    assert.equal(Number.isNaN(Date.parse(drop.as_of)), false);
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
  applied.book_usd = holdingsSum(applied);
  const view = cryptoBookView(applied);
  const usd = holdingRows(applied).find((row) => row.ticker === "USD");
  const usdc = holdingRows(applied).find((row) => row.ticker === "USDC");
  assert.equal(usd.valueUsd, drop.USD);
  assert.equal(usdc.valueUsd, drop.USDC);
  assert.equal(view.bookUsd, applied.book_usd);
  assertLiveRails(view, applied);
  assert.deepEqual(
    (applied.positions || []).map((row) => row.ticker),
    (live.positions || []).map((row) => row.ticker)
  );
  const exporter = readFileSync(new URL("../scripts/export_kpi.py", import.meta.url), "utf8");
  const main = exporter.split("\ndef main(argv")[1];
  const restAt = main.indexOf("rest_cash = load_rh_cash()");
  const skipAt = main.indexOf("REST cash skipped");
  const dropAt = main.indexOf("load_rh_cash_drop(DATA)");
  assert.ok(restAt >= 0 && restAt < skipAt && skipAt < dropAt);
  assert.equal(main.includes("Export KPI expected live Robinhood cash"), false);
  assert.equal(main.includes("cash=cash"), true);
  assert.equal(exporter.includes(String(drop.USD)), false);
  assert.equal(exporter.includes("day_realized_usd"), true);
  assert.equal(exporter.includes("day_realized_gross_usd"), true);
  assert.equal(exporter.includes("day_sell_fees_usd"), true);
  assert.equal(exporter.includes("differs from day_realized_usd"), true);
  const killSrc = exporter.split("def kill_remaining_usd")[1].split("\ndef ")[0];
  assert.equal(killSrc.includes("0.10"), false);
  assert.equal(killSrc.includes("0.1"), false);
  for (const file of ["derive.js", "app.js", "index.html"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.equal(text.includes(String(drop.USD)), false, file);
    assert.equal(text.includes(String(drop.USDC)), false, file);
  }
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
  const published = JSON.parse(readFileSync(new URL("../data/rh_cash.json", import.meta.url), "utf8"));
  assertOptionalDayRealized(published);
  const drop = {
    USD: published.USD,
    USDC: published.USDC,
    day_realized_usd: -1.09,
    day_realized_gross_usd: 1.25,
    day_sell_fees_usd: 2.34,
  };
  assertOptionalDayRealized(drop);
  assertOptionalDayRealized({
    USD: published.USD,
    USDC: published.USDC,
    day_realized_usd: drop.day_realized_usd,
  });
  assert.throws(() => assertOptionalDayRealized({ day_realized_usd: Number.NaN }));

  const book = {
    ...live,
    day_pnl_usd: drop.day_realized_usd,
    holdings: (live.holdings || []).map((row) => {
      if (row.ticker === "USD") return { ...row, value_usd: drop.USD };
      if (row.ticker === "USDC") return { ...row, value_usd: drop.USDC, cost_basis_usd: drop.USDC };
      return row;
    }),
  };
  book.book_usd = holdingsSum(book);
  const view = cryptoBookView(book);
  const balance = agenticTotal(book);
  const spent = Math.min(drop.day_realized_usd, 0);
  assert.equal(view.runningBalance, balance);
  assert.equal(view.bookUsd, holdingsSum(book));
  assert.equal(view.dayPnl, drop.day_realized_usd);
  assert.equal(view.runningPnl, roundCents(balance - combinedSeed));
  assert.equal(view.killHeadroom, killRemainingUsd(balance, drop.day_realized_usd));
  assert.equal(view.killHeadroom, Math.max(0, roundCents(Math.abs(LIVE_RAILS.dayKillFrac) * balance + spent)));
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
  assert.equal(merged.book_usd, sum);
  assert.equal(merged.kill_remaining_usd, live.kill_remaining_usd);
  assert.equal(merged.day_pnl_usd, live.day_pnl_usd);
  assert.equal(merged.signal_book_usd, 775);
  assert.deepEqual(
    merged.positions.map((row) => row.ticker),
    ["ZZ", "QQ", "USDC"]
  );
  assert.equal(view.bookUsd, sum);
  const openCard = reconciledPnl(merged);
  assert.equal(view.runningPnl, openCard.running);
  assert.equal(view.unrealizedPnl, 7.5);
  assert.equal(view.realizedPnl, openCard.realized);
  assert.equal(Math.round((view.realizedPnl + view.unrealizedPnl) * 100) / 100, view.runningPnl);
  assert.notEqual(view.runningPnl, live.running_pnl_usd ?? null);
  assert.equal(view.dayPnl, live.day_pnl_usd);
  assertLiveRails(view, merged);
  assert.equal(view.runningBalance, sum + 37.5);
  assert.notEqual(view.dayKill, railDollars(sum, LIVE_RAILS.dayKillFrac));
  assert.equal(view.equitiesUsd, 0);
  assert.notEqual(view.bookUsd, sum + 37.5);
  assert.equal(view.runningBalance, sum + 37.5);
  assert.deepEqual(
    holdingRows(merged).map((row) => row.ticker),
    ["USD", "USDC"]
  );
  assert.deepEqual(
    cardPositionRows(merged).map((row) => [row.ticker, row.qty, row.valueUsd, row.runningPnl]),
    [
      ...cashRows(live).map(([ticker, valueUsd]) => [ticker, null, valueUsd, null]),
      ["ZZ", 3, 37.5, 9.5],
    ]
  );
  assert.equal(app.includes('["Ticker", "Qty", "Value", "Running P&L"]'), true);
  assert.equal(openPositionRows(merged).some((row) => row.ticker === "QQ"), false);
  assert.equal(openPositionRows(merged).some((row) => row.ticker === "USDC"), false);
  assert.equal(shownBooks(merged).length, 1);
  assert.equal(app.includes("cardPositionRows"), true);
  assert.equal(app.includes("open_positions.json"), false);
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
  assert.equal(view.bookUsd, 30);
  assert.equal(view.runningBalance, 200);
  assert.notEqual(view.runningBalance, book.running_balance_usd);
  assert.notEqual(view.bookUsd, view.runningBalance);
  const budget = railDollars(200, Math.abs(LIVE_RAILS.dayKillFrac));
  assert.equal(view.killHeadroom, killRemainingUsd(200, -5));
  assert.equal(view.killHeadroom, railDollars(1, budget - 5));
  assert.notEqual(view.killHeadroom, railDollars(200, book.kill_remaining_usd / book.signal_book_usd));
  assert.equal(view.runningPnl, railDollars(1, 200 - SEEDS_USD.combined));
  assert.equal(view.dayPnl, -5);

  const profit = cryptoBookView({ ...book, day_pnl_usd: 6 });
  assert.equal(profit.killHeadroom, killRemainingUsd(200, 6));
  assert.equal(profit.killHeadroom, killRemainingUsd(200, 0));
  assert.equal(profit.dayPnl, 6);

  const stopped = cryptoBookView({ ...book, day_pnl_usd: -80 });
  assert.equal(stopped.killHeadroom, 0);

  const kept = mergeLiveBook(book, { ...publicSignal, day_pnl_usd: 9, kill_remaining_usd: 3 });
  assert.equal(kept.day_pnl_usd, -5);
  assert.equal(kept.kill_remaining_usd, 100);
  assert.equal(kept.book_usd, 30);
  assert.equal(kept.signal_book_usd, publicSignal.book_usd);

  const blank = mergeLiveBook(
    { holdings: [{ ticker: "USD", sleeve: "crypto", value_usd: 12 }], book_usd: 12 },
    { book_usd: 40, day_pnl_usd: 1.25, kill_remaining_usd: 4 }
  );
  assert.equal(blank.day_pnl_usd, 1.25);
  assert.equal(blank.kill_remaining_usd, 4);
  assert.equal(blank.book_usd, 12);
  assert.equal(blank.signal_book_usd, 40);
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
    for (const qty of qtys) assert.equal(text.includes(qty), false, `${file} ${qty}`);
  }
});

test("the page does not hardcode the holdings sum in place of the writer", () => {
  const view = cryptoBookView(live);
  const sum = holdingsSum(live);
  assert.equal(view.bookUsd, live.book_usd);
  assert.equal(view.bookUsd, sum);
  for (const file of ["derive.js", "app.js", "index.html"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.equal(text.includes("774.71"), false, file);
  }
  const published = readFileSync(new URL("../data/live_book.json", import.meta.url), "utf8");
  assert.equal(published.includes(String(sum)), true);
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
  assert.notEqual(cryptoBookView(live).asOf, live.generated_at);
  assert.equal(cryptoBookView(live).signalGeneratedAt, live.generated_at);

  const merged = mergeLiveBook(
    { ...live, generated_at: signalTime, sleeve_as_of: warehouseTime },
    { ...publicSignal, generated_at: "2026-10-04T20:41:10Z", sleeve_as_of: "1999-01-01T00:00:00Z" }
  );
  assert.equal(merged.generated_at, "2026-10-04T20:41:10Z");
  assert.equal(merged.sleeve_as_of, warehouseTime);
  assert.equal(merged.day_pnl_usd, live.day_pnl_usd);
  const view = cryptoBookView(merged);
  assert.equal(view.asOf, warehouseTime);
  assert.notEqual(view.asOf, merged.generated_at);
  assert.equal(view.signalGeneratedAt, merged.generated_at);
  assert.equal(view.dayPnl, live.day_pnl_usd);

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
