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

test("the signal book is not the 300/500/800 seeds and is not the account value", () => {
  const books = shownBooks(live);
  assert.equal(books.length, 1);
  assert.equal(books[0].sleeve, "crypto");
  assert.equal(books[0].bookUsd, live.book_usd);
  assert.equal(books[0].equitiesUsd, 0);
  assert.equal(books[0].runningBalance, 774.71);
  assert.notEqual(books[0].runningBalance, live.book_usd);
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
    book_usd: live.book_usd,
    candidates: [
      { symbol: "AVAX", side: "sell" },
      { symbol: "BTC", side: "sell" },
    ],
  });
  assert.equal(merged.candidates, undefined);
  assert.deepEqual(
    holdingRows(merged).map((row) => row.ticker),
    ["USD", "USDC"]
  );
});

const publicSignal = {
  generated_at: "2026-10-04T20:41:10Z",
  book_usd: 775,
  day_pnl_usd: 0,
  kill_remaining_usd: 77.5,
  holdings: [{ ticker: "USDC", sleeve: "crypto", value_usd: 775 }],
  candidates: [
    { symbol: "AVAX", side: "sell" },
    { symbol: "BTC", side: "sell" },
  ],
};

test("the old one-USDC $775 card and +$3.73 are not the account", () => {
  const old = {
    generated_at: "2026-10-04T16:34:23Z",
    book_usd: 775,
    day_pnl_usd: 0,
    kill_remaining_usd: 77.5,
    realized_pnl_usd: 3.73,
    unrealized_pnl_usd: 0,
    running_pnl_usd: 3.73,
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
  assert.notEqual(oldView.realizedPnl, 3.73);
  assert.equal(oldView.realizedPnl, null);
  assert.notEqual(oldView.runningPnl, 3.73);
  assert.equal(oldView.runningPnl, null);
  assert.equal(oldView.equitiesUsd, 0);

  const crypto = summary.find((row) => row.sleeve === "crypto");
  const equities = summary.find((row) => row.sleeve === "equities");
  const snap = deriveSleeve(crypto);
  const merged = mergeLiveBook(live, publicSignal);
  const view = cryptoBookView(merged);
  const rows = holdingRows(merged);
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
  assert.equal(view.bookUsd, 775);
  assert.equal(view.runningBalance, 774.71);
  assert.notEqual(view.runningBalance, 775);
  assert.notEqual(view.runningBalance, publicSignal.book_usd);
  assert.equal(view.dayPnl, 0);
  assert.notEqual(view.realizedPnl, 3.73);
  assert.equal(view.realizedPnl, null);
  assert.notEqual(view.runningPnl, 3.73);
  assert.equal(view.runningPnl, null);
  assert.equal(view.realizedPnlFrac, null);
  assert.equal(view.runningPnlFrac, null);
  assert.equal(view.unrealizedPnl, 0);
  assert.equal(view.unrealizedPnlFrac, 0);
  assert.notEqual(view.runningBalance, snap.runningBalance);
  assert.notEqual(view.realizedPnl, snap.realizedPnl);
  assert.notEqual(view.unrealizedPnl, snap.unrealizedPnl);
  assert.notEqual(view.runningPnl, snap.runningPnl);
  assert.notEqual(view.realizedPnl, deriveSleeve(equities).realizedPnl);
  assert.equal(JSON.stringify(live).includes("3.73"), false);
  const tapeNames = (openPositions.positions || []).map((row) => row.ticker);
  assert.ok(tapeNames.length > 1);
  for (const name of tapeNames) {
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

test("export does not rewrite the account file Pages serves", () => {
  for (const file of [".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    const start = text.indexOf("Commit refreshed JSON");
    const end = text.indexOf("Publish export failure", start);
    const commit = text.slice(start, end);
    const adds = commit.split("\n").filter((line) => line.includes("git add"));
    assert.ok(adds.length > 0, file);
    for (const line of adds) assert.equal(line.includes("live_book.json"), false, `${file}: ${line}`);
    assert.match(commit, /does not rewrite/);
  }
});

test("the page does not hardcode a broker total in place of book_usd", () => {
  const view = cryptoBookView(live);
  assert.equal(view.bookUsd, live.book_usd);
  for (const file of ["derive.js", "app.js", "index.html", "data/live_book.json"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.equal(text.includes("774.71"), false, file);
  }
});
