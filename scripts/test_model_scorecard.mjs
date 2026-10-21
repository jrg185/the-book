import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { approxCents, approxFrac, assertCentsDiffer, assertCentsEqual, assertFracEqual, FRAC_TOL } from "./cents.mjs";
import {
  closedFillStats,
  cryptoBookView,
  cryptoOosModels,
  feeDragFromTrades,
  deriveSleeve,
  formatWinRecord,
  inferLiveBackend,
  resolveLiveBackend,
  killRemainingUsd,
  LIVE_RAILS,
  money,
  railUsd,
} from "../derive.js";

const read = (name) => JSON.parse(readFileSync(new URL(`../data/${name}`, import.meta.url), "utf8"));
const trades = read("kpi_trades_scrubbed.json");
const models = read("models.json");
const oos = read("models_oos.json");
const summary = read("kpi_summary.json");
const app = readFileSync(new URL("../app.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");

test("crypto closed fills match sleeve win rules and the scrubbed tape", () => {
  const stats = closedFillStats(trades, "crypto");
  const fills = read("model_scorecard.json").closed_fills;
  for (const key of ["wins", "losses", "flats", "decided", "deduped"]) {
    assert.equal(Number.isInteger(stats[key]), true);
    assert.equal(stats[key] >= 0, true);
    assert.equal(stats[key], fills[key]);
  }
  assert.equal(stats.wins + stats.losses, stats.decided);
  assert.equal(stats.orderIdAvailable, false);
  assert.equal(Object.hasOwn(fills, "order_id_available"), false);
  assert.equal(Object.hasOwn(read("model_scorecard.json"), "order_id_unique"), false);
  assert.equal(fills.dedupe, "scrubbed-rows");
  assert.equal(typeof stats.expectancyUsd, "number");
  assert.equal(Number.isFinite(stats.expectancyUsd), true);
  assertCentsEqual(stats.expectancyUsd, fills.expectancy_usd);
  assertFracEqual(stats.rate, fills.win_rate);
  assert.equal(formatWinRecord(stats), formatWinRecord(fills));
  // Scorecard expectancy is q6 of the same tape. That can sit on x.xx5%.
  assertFracEqual(stats.expectancyFrac, fills.expectancy_frac);
  const tapeFees = feeDragFromTrades(trades, "crypto");
  const fees = read("model_scorecard.json").fee_drag;
  assert.equal(tapeFees.status, "known");
  assert.equal(fees.status, "known");
  for (const key of ["n", "fee_usd", "sell_fee_usd"]) {
    assert.equal(typeof tapeFees[key], "number");
    assert.equal(Number.isFinite(tapeFees[key]), true);
    assert.equal(tapeFees[key] >= 0, true);
  }
  assert.equal(tapeFees.n, fees.n);
  assertCentsEqual(tapeFees.fee_usd, fees.fee_usd);
  assertCentsEqual(tapeFees.sell_fee_usd, fees.sell_fee_usd);
  assert.equal(typeof tapeFees.fee_frac, "number");
  assert.equal(Number.isFinite(tapeFees.fee_frac), true);
  assert.equal(tapeFees.fee_frac >= 0, true);
  assertFracEqual(tapeFees.fee_frac, fees.fee_frac);
});

test("order id collapses duplicate sells and fee dollars stay explicit", () => {
  const stats = closedFillStats(
    [
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.01, order_id: "a" },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0.01, order_id: "a" },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: 0, order_id: "b" },
      { sleeve: "crypto", side: "buy", pnl_frac_of_book: 0.4, fee_usd: 0.25 },
      { sleeve: "crypto", side: "sell", pnl_frac_of_book: -0.02, fee_usd: 0.1 },
    ],
    "crypto"
  );
  assert.equal(stats.wins, 1);
  assert.equal(stats.losses, 1);
  assert.equal(stats.flats, 1);
  assert.equal(stats.deduped, 1);
  assert.equal(stats.orderIdAvailable, true);
  const fees = feeDragFromTrades(
    [
      { sleeve: "crypto", side: "buy", fee_usd: 0.25, order_id: "a" },
      { sleeve: "crypto", side: "buy", fee_usd: 0.25, order_id: "a" },
      { sleeve: "crypto", side: "sell", fee_usd: 0.1 },
    ],
    "crypto"
  );
  assert.equal(fees.status, "known");
  assertCentsEqual(fees.fee_usd, 0.35);
  assertCentsEqual(fees.sell_fee_usd, 0.1);
});

test("live backend comes from the signal and falls back to notes", () => {
  const predictions = read("predictions.json");
  const scorecard = read("model_scorecard.json");
  assert.equal(Boolean(predictions.backend), true);
  assert.equal(Boolean(predictions.model_version), true);
  assert.equal(scorecard.live_backend.id, predictions.backend);
  assert.equal(scorecard.live_backend.model_version, predictions.model_version);
  assert.equal(scorecard.live_backend.cli, `--backend ${predictions.backend}`);
  assert.equal(scorecard.live_backend.source, predictions.source || "signals/latest.json");
  assert.equal(JSON.stringify(models).includes("--backend rules"), false);
  const fromExport = resolveLiveBackend(scorecard, models, null);
  assert.equal(fromExport.id, predictions.backend);
  assert.equal(fromExport.model_version, predictions.model_version);
  const staleModels = {
    note: "The CLI stays --backend rules.",
    models: [{ sleeve: "crypto", used: "stays --backend rules" }],
  };
  const staleCard = {
    live_backend: {
      id: "rules",
      cli: "--backend rules",
      source: "data/models.json",
      note: "The CLI is still --backend rules.",
    },
  };
  const fromSignal = resolveLiveBackend(staleCard, staleModels, predictions);
  assert.equal(fromSignal.id, predictions.backend);
  assert.equal(fromSignal.model_version, predictions.model_version);
  assert.notEqual(fromSignal.id, "rules");
  const backend = inferLiveBackend(staleModels);
  assert.equal(backend.id, "rules");
  assert.equal(backend.cli, "--backend rules");
  assert.equal(backend.model_version, null);
  const unnamed = resolveLiveBackend(null, { note: "no cli flag" }, { generated_at: "2026-10-17T00:00:00Z" });
  assert.equal(unnamed.id, null);
  const rows = cryptoOosModels(oos);
  assert.deepEqual(
    rows.map((row) => row.model),
    ["rules", "logistic", "lgbm"]
  );
  assert.equal(rows.find((row) => row.model === "lgbm").promoted, true);
  assert.equal(rows.find((row) => row.model === "logistic").promoted, false);
  const crypto = summary.find((row) => row.sleeve === "crypto");
  const derived = deriveSleeve(crypto);
  const book = read("live_book.json");
  const view = cryptoBookView(book, summary);
  assertFracEqual(derived.killHeadroomFrac, scorecard.kill.kill_headroom_frac);
  assert.equal(Number.isFinite(scorecard.kill.kill_headroom_usd), true);
  assert.equal(LIVE_RAILS.source, "data/kpi_summary.json");
  assertFracEqual(LIVE_RAILS.dayKillFrac, Number(crypto.day_kill_pct));
  assertFracEqual(LIVE_RAILS.dayTargetFrac, Number(crypto.day_target_pct));
  assertFracEqual(derived.dayKillFrac, scorecard.kill.day_kill_pct);
  assertFracEqual(derived.dayKillFrac, LIVE_RAILS.dayKillFrac);
  // The sleeve curve still recovers dollars from the config seed. The published
  // kill rail is the live running balance, the same base as kill_remaining_usd.
  assertCentsEqual(derived.dayKill, money(derived.seed, LIVE_RAILS.dayKillFrac));
  assertCentsEqual(view.runningBalance, book.running_balance_usd);
  assertCentsEqual(scorecard.kill.running_balance_usd, view.runningBalance);
  assertCentsEqual(scorecard.kill.day_kill_usd, railUsd(view.runningBalance, LIVE_RAILS.dayKillFrac));
  assertCentsEqual(view.dayKill, scorecard.kill.day_kill_usd);
  assertCentsEqual(scorecard.kill.day_target_usd, railUsd(view.runningBalance, LIVE_RAILS.dayTargetFrac));
  assertCentsEqual(scorecard.kill.kill_headroom_usd, book.kill_remaining_usd);
  assertCentsEqual(money(derived.seed, crypto.kill_headroom_frac), book.kill_remaining_usd);
  if (book.day_open_usd != null) {
    const headroom = killRemainingUsd(book.running_balance_usd, book.day_open_usd, LIVE_RAILS.dayKillFrac);
    assertCentsEqual(book.kill_remaining_usd, headroom);
  }
  const fixtureOpen = 400;
  const fixtureBalance = 380;
  const fixtureHeadroom = killRemainingUsd(fixtureBalance, fixtureOpen, LIVE_RAILS.dayKillFrac);
  assertCentsEqual(fixtureHeadroom, killRemainingUsd(fixtureBalance, fixtureOpen));
  assertCentsDiffer(fixtureHeadroom, Math.abs(railUsd(fixtureBalance, LIVE_RAILS.dayKillFrac)));
  assertFracEqual(crypto.kill_headroom_frac, scorecard.kill.kill_headroom_frac);
  assertCentsDiffer(scorecard.kill.day_kill_usd, money(derived.seed, LIVE_RAILS.dayKillFrac));
  if (book.day_open_usd != null && approxCents(book.day_open_usd, book.running_balance_usd)) {
    assertCentsEqual(book.kill_remaining_usd, Math.abs(scorecard.kill.day_kill_usd));
  }
  assertFracEqual(derived.dayTargetFrac, scorecard.kill.day_target_pct);
  assertFracEqual(derived.dayTargetFrac, LIVE_RAILS.dayTargetFrac);
  const deriveSrc = readFileSync(new URL("../derive.js", import.meta.url), "utf8");
  assert.equal(/dayKillFrac\s*[:=]\s*-?\d/.test(deriveSrc), false);
  assert.equal(/dayTargetFrac\s*[:=]\s*-?\d/.test(deriveSrc), false);
  assert.equal(deriveSrc.includes('type: "json"'), false);
  assert.equal(deriveSrc.includes("type: 'json'"), false);
});

test("day kill uses the kill-remaining cent round on a half-cent balance", () => {
  // 712.05 × 10% is a half cent. 712.1 × 10% is not, so the nearby tenth
  // hides the tie. The signed product round and the shared helper disagree
  // only on the edge balance.
  const edge = 712.05;
  const nearby = 712.1;
  const frac = -0.1;
  assertCentsDiffer(edge, nearby);
  const shared = railUsd(edge, frac);
  const signed = money(edge, frac);
  assertCentsDiffer(shared, signed);
  assertCentsEqual(Math.abs(shared), killRemainingUsd(edge, edge, frac));
  assertCentsEqual(railUsd(nearby, frac), money(nearby, frac));
  const book = {
    running_balance_usd: edge,
    day_open_usd: edge,
    day_pnl_usd: 0,
    unrealized_pnl_usd: 0,
    holdings: [{ ticker: "USD", sleeve: "crypto", value_usd: nearby }],
  };
  const view = cryptoBookView(book, [{ sleeve: "crypto", day_kill_pct: frac, day_target_pct: 0.025 }]);
  // Cash on the file is the nearby tenth. With no open-lot list the rail
  // stays the running balance, not that cash line.
  assertCentsEqual(view.runningBalance, edge);
  assertCentsDiffer(view.runningBalance, nearby);
  assertCentsEqual(view.dayKill, shared);
  assertCentsEqual(view.dayKill, railUsd(view.runningBalance, frac));
  assertCentsDiffer(view.dayKill, signed);
  assertCentsEqual(Math.abs(view.dayKill), view.killHeadroom);
});

test("book-fraction tolerance is half of 0.01 percent", () => {
  assert.equal(approxFrac(0, FRAC_TOL), false);
  assert.equal(approxFrac(0, -FRAC_TOL), false);
  assert.equal(approxFrac(0, FRAC_TOL / 2), true);
  assertFracEqual(null, null);
  assert.throws(() => assertFracEqual(0, null));
});

test("models tab markup loads the scorecard instead of a second page", () => {
  assert.equal(html.includes('id="tab-models"'), true);
  assert.equal(html.includes("Crypto scorecard"), true);
  assert.equal(html.includes('id="panel-models"'), true);
  assert.equal(app.includes('loadJson("data/model_scorecard.json"'), true);
  assert.equal(app.includes("renderCryptoScorecard"), true);
  assert.equal(app.includes('metric("Fee drag"'), true);
  assert.equal(app.includes("UNKNOWN"), true);
  // The fee assumption and the measured fee are published in the scorecard
  // note. The page shows that note and does not carry its own copy of the fee.
  assert.equal(app.includes("scorecard?.oos?.note"), true);
  assert.equal(app.includes("scorecard?.oos?.fee_bps"), true);
  assert.equal(app.includes("95 bps/leg"), false);
  assert.equal(app.includes("190 RT"), false);
  assert.equal(/fee_bps\s*\?\?\s*\d/.test(app), false);
  assert.equal(app.includes("fee_frac_of_book"), true);
  assert.equal(app.includes('"Fee"'), true);
  const scorecard = read("model_scorecard.json");
  assert.match(scorecard.oos.note, /95 bps\/leg/);
  assert.match(scorecard.oos.note, /190 RT/);
  assert.match(scorecard.oos.note, /T24d will set FEE_BPS/);
  assert.equal(scorecard.oos.fee_bps, 30);
  assert.equal(scorecard.fee_drag.status, "known");
  for (const key of ["fee_usd", "sell_fee_usd", "fee_frac"]) {
    assert.equal(typeof scorecard.fee_drag[key], "number");
    assert.equal(Number.isFinite(scorecard.fee_drag[key]), true);
    assert.equal(scorecard.fee_drag[key] >= 0, true);
  }
  assert.equal(Number.isInteger(scorecard.fee_drag.n), true);
  assert.equal(scorecard.fee_drag.n >= 0, true);
  assert.match(scorecard.fee_drag.note, /Measured live fee/);
  assert.match(scorecard.fee_drag.note, /T24d/);
  assert.equal(scorecard.signal_linkage.status, "known");
  for (const key of ["artifact_count", "outcome_count"]) {
    assert.equal(Number.isInteger(scorecard.signal_linkage[key]), true);
    assert.equal(scorecard.signal_linkage[key] >= 0, true);
  }
  assert.match(String(scorecard.signal_linkage.last_generated_at || ""), /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/);
  assert.equal(app.includes("T24b will improve joined-fill metrics."), true);
  assert.equal(app.includes("signal_artifacts"), true);
  assert.equal(app.includes("signal_trade_outcomes"), true);
  assert.equal(models.models.find((row) => row.sleeve === "equities").used.includes("Equity modeling and place are not live."), true);
  assert.equal(app.includes("Queued RTH"), false);
  assert.equal(html.includes("One crypto book."), true);
  assert.equal(html.includes("Equity research"), true);
  assert.equal(models.models.find((row) => row.sleeve === "equities").used.includes("Paused."), true);
  assert.equal(app.includes("--backend rules"), true);
});
