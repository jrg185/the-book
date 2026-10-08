import assert from "node:assert/strict";
import test from "node:test";

import { currentSeeds, SEEDS_USD, seedRows } from "../book_seeds.js";
import { closedFillStats, feeDragFromTrades, money, SEEDS_USD as derivedSeeds } from "../derive.js";
import { assertCentsEqual } from "./cents.mjs";

test("derive.js uses the config loader", () => {
  assert.deepEqual(derivedSeeds, SEEDS_USD);
  const rows = seedRows();
  assert.deepEqual(
    rows.map((row) => row.sleeve).sort(),
    ["combined", "crypto", "equities"]
  );
  for (const row of rows) {
    assert.equal(typeof row.seed_usd, "number");
    assert.ok(row.seed_usd > 0);
    assert.equal(SEEDS_USD[row.sleeve], row.seed_usd);
  }
});

test("an earlier effective date keeps the earlier seed", () => {
  const config = {
    seeds: [
      { sleeve: "crypto", seed_usd: "10", effective_from: "2020-01-01" },
      { sleeve: "crypto", seed_usd: "11", effective_from: "2024-06-01" },
    ],
  };
  assert.equal(currentSeeds(config, "2021-01-01").crypto, 10);
  assert.equal(currentSeeds(config).crypto, 11);
});

test("scorecard helpers take the crypto seed from config", () => {
  const trades = [{ sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.01, fee_frac_of_book: 0.001 }];
  const stats = closedFillStats(trades, "crypto");
  assert.equal(stats.seed, SEEDS_USD.crypto);
  assertCentsEqual(stats.expectancyUsd, money(SEEDS_USD.crypto, 0.01));
  const fees = feeDragFromTrades(trades, "crypto");
  assert.equal(fees.seed_usd, SEEDS_USD.crypto);
  assertCentsEqual(fees.fee_usd, money(SEEDS_USD.crypto, 0.001));
});
