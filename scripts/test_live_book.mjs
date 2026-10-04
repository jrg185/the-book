import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  cryptoBookView,
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

test("the live book is signal book_usd, not the 300/500/800 seeds", () => {
  const books = shownBooks(live);
  assert.equal(books.length, 1);
  assert.equal(books[0].sleeve, "crypto");
  assert.equal(books[0].bookUsd, live.book_usd);
  assert.equal(books[0].equitiesUsd, 0);
  for (const seed of [SEEDS_USD.crypto, SEEDS_USD.equities, SEEDS_USD.combined]) {
    assert.notEqual(books[0].bookUsd, seed);
  }
  assert.notEqual(books[0].bookUsd, seededBalance("crypto"));
  assert.notEqual(books[0].bookUsd, seededBalance("equities"));
  assert.notEqual(books[0].bookUsd, seededBalance("combined"));
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
    rows.map((row) => row.ticker),
    ["USDC"]
  );
  assert.equal(rows[0].valueUsd, live.book_usd);
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
    ["USDC"]
  );
});

test("the page does not hardcode a broker total in place of book_usd", () => {
  const view = cryptoBookView(live);
  assert.equal(view.bookUsd, live.book_usd);
  for (const file of ["derive.js", "app.js", "index.html", "data/live_book.json"]) {
    const text = readFileSync(new URL(`../${file}`, import.meta.url), "utf8");
    assert.equal(text.includes("774.71"), false, file);
  }
});
