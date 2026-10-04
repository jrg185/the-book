import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  cryptoBookView,
  deriveSleeve,
  holdingRows,
  mergeLiveBook,
  SEEDS_USD,
  shownBooks,
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

test("the published book is the holdings sum, and the rails stay on the signal book", () => {
  const books = shownBooks(live);
  const sum = Math.round((760.64 + 14.07 + Number.EPSILON) * 100) / 100;
  assert.equal(books.length, 1);
  assert.equal(books[0].sleeve, "crypto");
  assert.equal(live.book_usd, sum);
  assert.equal(books[0].bookUsd, sum);
  assert.equal(books[0].bookUsd, live.book_usd);
  assert.equal(books[0].runningBalance, sum);
  assert.equal(books[0].equitiesUsd, 0);
  assert.equal(live.signal_book_usd, 775);
  assert.equal(books[0].dayPnl, live.day_pnl_usd ?? null);
  assert.equal(books[0].dayKill, -77.5);
  assert.equal(books[0].dayTarget, 19.38);
  assert.equal(books[0].killHeadroom, 77.5);
  assert.notEqual(books[0].dayKill, -77.47);
  assert.notEqual(books[0].dayTarget, 19.37);
  assert.notEqual(books[0].bookUsd, live.signal_book_usd);
  for (const seed of [SEEDS_USD.crypto, SEEDS_USD.equities, SEEDS_USD.combined]) {
    assert.notEqual(books[0].bookUsd, seed);
    assert.notEqual(books[0].runningBalance, seed);
  }
  assert.notEqual(books[0].runningBalance, seededBalance("crypto"));
  assert.notEqual(books[0].runningBalance, seededBalance("equities"));
  assert.notEqual(books[0].runningBalance, seededBalance("combined"));
  assert.equal(books.some((book) => book.sleeve === "equities"), false);
  assert.equal(books.some((book) => book.sleeve === "combined"), false);
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
    [
      ["USD", 760.64],
      ["USDC", 14.07],
    ]
  );
  assert.ok(rows.every((row) => row.valueUsd !== live.book_usd));
  const tapeNames = new Set((openPositions.positions || []).map((row) => row.ticker));
  for (const row of rows) assert.equal(tapeNames.has(row.ticker), false);
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

  const crypto = summary.find((row) => row.sleeve === "crypto");
  const equities = summary.find((row) => row.sleeve === "equities");
  const snap = deriveSleeve(crypto);
  const withPnl = {
    ...live,
    realized_pnl_usd: 8.5,
    unrealized_pnl_usd: 1.25,
    running_pnl_usd: 9.75,
    day_pnl_usd: 1.5,
  };
  const merged = mergeLiveBook(withPnl, publicSignal);
  const view = cryptoBookView(merged);
  const rows = holdingRows(merged);
  const sum = Math.round((760.64 + 14.07 + Number.EPSILON) * 100) / 100;
  assert.equal(merged.candidates, undefined);
  assert.deepEqual(
    rows.map((row) => [row.ticker, row.valueUsd]),
    [
      ["USD", 760.64],
      ["USDC", 14.07],
    ]
  );
  assert.ok(rows.every((row) => row.valueUsd !== 775));
  assert.equal(view.sleeve, "crypto");
  assert.equal(view.equitiesUsd, 0);
  assert.equal(merged.book_usd, sum);
  assert.equal(merged.signal_book_usd, 775);
  assert.notEqual(merged.book_usd, publicSignal.book_usd);
  assert.equal(merged.day_pnl_usd, withPnl.day_pnl_usd);
  assert.notEqual(merged.day_pnl_usd, publicSignal.day_pnl_usd);
  assert.equal(merged.kill_remaining_usd, 77.5);
  assert.equal(merged.realized_pnl_usd, withPnl.realized_pnl_usd);
  assert.equal(merged.unrealized_pnl_usd, withPnl.unrealized_pnl_usd);
  assert.equal(merged.running_pnl_usd, withPnl.running_pnl_usd);
  assert.equal(merged.candidates, undefined);
  assert.equal(view.bookUsd, sum);
  assert.equal(view.runningBalance, sum);
  assert.notEqual(view.bookUsd, 775);
  assert.notEqual(view.bookUsd, publicSignal.book_usd);
  assert.equal(view.dayPnl, withPnl.day_pnl_usd);
  assert.equal(view.dayKill, -77.5);
  assert.equal(view.dayTarget, 19.38);
  assert.equal(view.killHeadroom, 77.5);
  assert.notEqual(view.dayKill, -77.47);
  assert.notEqual(view.dayTarget, 19.37);
  assert.equal(view.realizedPnl, withPnl.realized_pnl_usd);
  assert.equal(view.unrealizedPnl, withPnl.unrealized_pnl_usd);
  assert.equal(view.runningPnl, withPnl.running_pnl_usd);
  assert.notEqual(view.realizedPnl, null);
  assert.notEqual(view.runningPnl, null);
  const seedDollars = Math.round((SEEDS_USD.crypto * crypto.realized_pnl_frac + Number.EPSILON) * 100) / 100;
  assert.notEqual(view.realizedPnl, seedDollars);
  assert.notEqual(view.runningPnl, Math.round((SEEDS_USD.crypto * crypto.running_pnl_frac + Number.EPSILON) * 100) / 100);
  assert.ok(Math.abs(view.realizedPnlFrac - withPnl.realized_pnl_usd / sum) < 1e-12);
  assert.notEqual(view.realizedPnlFrac, withPnl.realized_pnl_usd / SEEDS_USD.crypto);
  assert.notEqual(view.runningBalance, snap.runningBalance);
  assert.notEqual(view.realizedPnl, snap.realizedPnl);
  assert.notEqual(view.unrealizedPnl, snap.unrealizedPnl);
  assert.notEqual(view.runningPnl, snap.runningPnl);
  assert.notEqual(view.realizedPnl, deriveSleeve(equities).realizedPnl);
  const published = JSON.stringify(live);
  for (const key of ["realized_pnl_usd", "unrealized_pnl_usd", "running_pnl_usd"]) {
    if (live[key] != null) assert.equal(cryptoBookView(live)[key === "realized_pnl_usd" ? "realizedPnl" : key === "running_pnl_usd" ? "runningPnl" : "unrealizedPnl"], live[key]);
  }
  assert.equal(published.includes("\"realized_pnl_usd\": null"), false);
  assert.equal(published.includes("\"running_pnl_usd\": null"), false);
  const tapeNames = (openPositions.positions || []).map((row) => row.ticker);
  assert.ok(tapeNames.length > 1);
  for (const name of tapeNames) {
    assert.equal(rows.some((row) => row.ticker === name), false, name);
  }
  assert.equal(app.includes('formatUsd(view.bookUsd)'), true);
  assert.equal(app.includes('formatUsd(view.runningBalance)'), false);
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
  assert.equal(exporter.includes("def latest_account_pnl"), true);
  assert.equal(exporter.includes("public.kpi_sleeve_snapshots"), true);
  assert.equal(exporter.includes("realized_pnl_usd"), true);
  assert.equal(exporter.includes("unrealized_pnl_usd"), true);
  assert.equal(exporter.includes("running_pnl_usd"), true);
  assert.equal(exporter.includes("refresh_live_book(DATA)"), true);
  assert.equal(derive.includes("const realizedPnl = null"), false);
  assert.equal(derive.includes("const runningPnl = null"), false);
  assert.equal(derive.includes("delete base.realized_pnl"), false);
  assert.equal(derive.includes("delete base.running_pnl"), false);
});

test("the page does not hardcode the holdings sum in place of the writer", () => {
  const view = cryptoBookView(live);
  const sum = Math.round((760.64 + 14.07 + Number.EPSILON) * 100) / 100;
  assert.equal(view.bookUsd, live.book_usd);
  assert.equal(view.bookUsd, sum);
  for (const file of ["derive.js", "app.js", "index.html"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.equal(text.includes("774.71"), false, file);
  }
  const published = readFileSync(new URL("../data/live_book.json", import.meta.url), "utf8");
  assert.equal(published.includes("774.71"), true);
  assert.equal(deriveSourceHasSeedRebuild(published), false);
});

function deriveSourceHasSeedRebuild(published) {
  return published.includes("realized_pnl_frac") || published.includes("running_pnl_frac");
}
