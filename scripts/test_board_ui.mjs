import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  curvePoints,
  dailySharpe,
  drawdownSeries,
  drawdownStats,
  liveRails,
  parseHash,
  permalink,
  railView,
  rankModels,
  redactIds,
  runRows,
  seedReturn,
  shareUrl,
  tradeAnchors,
  tradeSlug,
  windowChange,
  windowPoints,
} from "../board.js";
import { cryptoBookView, killRemainingUsd, SEEDS_USD } from "../derive.js";
import { buildOgSvg, ogSnapshot, xmlEscape } from "../og.js";
import { preferredWhy } from "../tape.js";

const src = (name) => readFileSync(new URL(`../${name}`, import.meta.url), "utf8");
const data = (name) => JSON.parse(src(`data/${name}`));

const summary = data("kpi_summary.json");
const scorecard = data("model_scorecard.json");
const book = data("live_book.json");
const trades = data("kpi_trades_scrubbed.json");
const curves = data("sleeve_curves.json");
const meta = data("meta.json");

const FRONT = ["app.js", "board.js", "og.js", "runs.js", "charts.js", "index.html", "runs.html"];

test("day rails come from the warehouse export, then the scorecard, never a constant", () => {
  const crypto = summary.find((row) => row.sleeve === "crypto");
  const rails = liveRails(summary, scorecard);
  if (crypto?.day_kill_pct != null) {
    assert.equal(rails.source, "data/kpi_summary.json");
    assert.equal(rails.killFrac, Number(crypto.day_kill_pct));
  }
  const custom = liveRails([{ sleeve: "crypto", day_kill_pct: -0.07, day_target_pct: 0.031 }], null);
  assert.equal(custom.killFrac, -0.07);
  assert.equal(custom.targetFrac, 0.031);
  const points = liveRails([{ sleeve: "crypto", day_kill_pct: -7 }], null);
  assert.equal(points.killFrac, -0.07);
  const fallback = liveRails([{ sleeve: "crypto" }], { kill: { day_kill_pct: -0.2, day_target_pct: 0.01 } });
  assert.equal(fallback.source, "data/model_scorecard.json");
  assert.equal(fallback.killFrac, -0.2);
  const none = liveRails([], null);
  assert.equal(none.killFrac, null);
  assert.equal(none.targetFrac, null);
  assert.equal(none.source, null);
});

test("rail bars use the export headroom formula on the running balance", () => {
  const view = cryptoBookView(book, summary);
  const rails = liveRails(summary, scorecard);
  const r = railView(view.runningBalance, view.dayPnl, rails);
  if (rails.killFrac != null) {
    assert.equal(r.headroomUsd, killRemainingUsd(view.runningBalance, view.dayPnl, rails.killFrac));
  }
  const losing = railView(1000, -25, { killFrac: -0.1, targetFrac: 0.025 });
  assert.equal(losing.killUsd, -100);
  assert.equal(losing.headroomUsd, 75);
  assert.equal(losing.killUsedFrac, 0.25);
  assert.equal(losing.targetUsd, 25);
  assert.equal(losing.targetProgress, 0);
  const winning = railView(1000, 10, { killFrac: -0.1, targetFrac: 0.025 });
  assert.equal(winning.headroomUsd, 100);
  assert.equal(winning.targetProgress, 0.4);
  const unpublished = railView(1000, 10, { killFrac: null, targetFrac: null });
  assert.equal(unpublished.headroomUsd, null);
  assert.equal(unpublished.targetUsd, null);
});

test("front end has no rail, fee, or seed constants of its own", () => {
  for (const file of FRONT) {
    const text = src(file);
    assert.equal(/\b0\.025\b/.test(text), false, `${file} target rail literal`);
    assert.equal(/-0\.10?\b/.test(text), false, `${file} kill rail literal`);
    assert.equal(text.includes("LIVE_RAILS"), false, `${file} reads LIVE_RAILS`);
    assert.equal(/\b\d+ ?bps\b/.test(text), false, `${file} fee bps literal`);
    assert.equal(text.includes("--10%") || text.includes("+2.5%"), false, `${file} rail copy`);
  }
  const seeds = JSON.parse(src("config/book_seeds.json")).seeds.map((row) => String(row.seed_usd));
  for (const file of ["app.js", "board.js", "og.js", "runs.js", "index.html", "runs.html"]) {
    const text = src(file);
    for (const seed of seeds) {
      assert.equal(new RegExp(`\\$${seed}\\b`).test(text), false, `${file} $${seed}`);
    }
  }
});

test("front end never hardcodes the published balance or prices", () => {
  const literals = [book.running_balance_usd, book.book_usd, book.running_pnl_usd, book.realized_pnl_usd]
    .filter((value) => typeof value === "number" && value !== 0)
    .map((value) => value.toFixed(2));
  for (const row of book.positions || []) {
    if (row.mark) literals.push(String(row.mark));
    if (row.avg_cost) literals.push(String(row.avg_cost));
  }
  for (const file of FRONT) {
    const text = src(file);
    for (const literal of literals) {
      assert.equal(new RegExp(`(?<![\\d.])${literal.replace(".", "\\.")}(?![\\d])`).test(text), false, `${file} ${literal}`);
    }
  }
});

test("permalinks use only time, ticker, and side and never an order id", () => {
  const trade = {
    timestamp_et: "2026-09-26T18:22:05.677327+00:00",
    ticker: "AVAX",
    side: "sell",
    order_id: "4f7c2a8e-1b2d-4c3e-9f10-112233445566",
    account_number: "ACCT-1",
  };
  const slug = tradeSlug(trade);
  assert.equal(slug, "trade-20260926-182205-avax-sell");
  assert.match(slug, /^trade-[a-z0-9-]+$/);
  assert.equal(slug.includes("4f7c"), false);
  const twin = { ...trade, order_id: "other" };
  const ids = tradeAnchors([trade, twin]);
  assert.equal(ids.get(trade), slug);
  assert.equal(ids.get(twin), `${slug}-2`);
  const all = tradeAnchors(trades);
  assert.equal(new Set(all.values()).size, all.size);
  for (const id of all.values()) assert.match(id, /^trade-\d{8}-\d{6}-[a-z0-9]+-(buy|sell)(-\d+)?$/);
  assert.deepEqual(parseHash(`#${slug}`), { kind: "trade", id: slug });
  assert.deepEqual(parseHash("#day-2026-10-08"), { kind: "day", day: "2026-10-08" });
  assert.equal(parseHash("#trade-<script>").kind, "anchor");
  assert.equal(parseHash("#model-crypto").kind, "models");
});

test("share links go to the X intent with an encoded permalink", () => {
  const url = shareUrl("Sold AVAX · +$1.00", permalink("trade-20260926-182205-avax-sell"));
  assert.match(url, /^https:\/\/x\.com\/intent\/tweet\?/);
  const params = new URL(url).searchParams;
  assert.equal(params.get("text"), "Sold AVAX · +$1.00");
  assert.equal(params.get("url"), "https://jrg185.github.io/the-book/#trade-20260926-182205-avax-sell");
});

test("free text masks pasted broker ids and keeps agent links", () => {
  const text = "closed per 4f7c2a8e-1b2d-4c3e-9f10-112233445566, see bc-0a1b2c3d4e5f6a7b8c9d0e1f2a3b and PR #12";
  const out = redactIds(text);
  assert.equal(out.includes("4f7c2a8e"), false);
  assert.ok(out.includes("[id]"));
  assert.ok(out.includes("bc-0a1b2c3d4e5f6a7b8c9d0e1f2a3b"));
  assert.ok(out.includes("PR #12"));
  const machine = preferredWhy({ why: "RH Agentic sync order 4f7c2a8e-1b2d-4c3e-9f10-112233445566" });
  assert.equal(machine.kind, "machine");
  const app = src("app.js");
  assert.equal(app.includes("whyUuid"), false);
  assert.equal(app.includes("Order id"), false);
  assert.equal(app.includes(".uuid"), false);
});

test("runs reader degrades on a missing file and never reads id fields", () => {
  assert.deepEqual(runRows(null), []);
  assert.deepEqual(runRows({}), []);
  const rows = runRows({
    updated_at: "2026-10-11T16:00:00Z",
    runs: [
      { run_id: "r-1", started_at: "2026-10-11T15:00:00Z", model: "rules", status: "ok", summary: "Bought ARB. ref 4f7c2a8e-1b2d-4c3e-9f10-112233445566", order_ids: ["x"], account_number: "A1", trades: 2, pnl_frac: 0.004 },
      { id: "r-0", started_at: "2026-10-10T15:00:00Z", model: "lgbm", decision: "hold" },
    ],
  });
  assert.equal(rows.length, 2);
  assert.equal(rows[0].model, "rules");
  assert.equal(rows[0].trades, 2);
  assert.equal(rows[0].summary.includes("4f7c"), false);
  const blob = JSON.stringify(rows);
  for (const banned of ["r-1", "r-0", "A1", "order_ids", "account"]) assert.equal(blob.includes(banned), false, banned);
  assert.equal(rows[1].decision, "hold");
});

test("curve windows, drawdown, and Sharpe read the crypto sleeve fractions", () => {
  const points = curvePoints(curves, "crypto");
  assert.ok(points.length > 1);
  for (let i = 1; i < points.length; i += 1) assert.ok(points[i].ms >= points[i - 1].ms);
  const day = windowPoints(points, "24h");
  assert.ok(day.length >= 1 && day.length <= points.length);
  assert.ok(day[day.length - 1].ms - day[1 < day.length ? 1 : 0].ms <= 24 * 3600 * 1000);
  assert.equal(windowPoints(points, "all").length, points.length);
  const synthetic = [
    { ms: 0, frac: 1 },
    { ms: 1, frac: 1.2 },
    { ms: 2, frac: 0.9 },
    { ms: 3, frac: 1.1 },
  ];
  const dd = drawdownSeries(synthetic);
  assert.equal(dd[2].dd.toFixed(4), (0.9 / 1.2 - 1).toFixed(4));
  assert.equal(drawdownStats(synthetic).max.toFixed(4), (0.9 / 1.2 - 1).toFixed(4));
  assert.equal(windowChange(synthetic).toFixed(4), "0.1000");
  assert.equal(dailySharpe(synthetic).value, null);
  const s = dailySharpe(points);
  assert.ok(s.value == null || Number.isFinite(s.value));
});

test("all-time return is against the combined seed from config", () => {
  assert.equal(seedReturn(80, { combined: 800 }), 0.1);
  assert.equal(seedReturn(null), null);
  const view = cryptoBookView(book, summary);
  if (view.runningPnl != null) assert.equal(seedReturn(view.runningPnl), view.runningPnl / SEEDS_USD.combined);
});

test("model leaderboard ranks by after-cost mean", () => {
  const ranked = rankModels([
    { model: "a", after_cost_mean: -0.001 },
    { model: "b", after_cost_mean: 0.002, promoted: true },
    { model: "c" },
  ]);
  assert.deepEqual(ranked.map((row) => row.model), ["b", "a", "c"]);
  assert.equal(ranked[0].promoted, true);
});

test("share card is built from data JSON and carries no ids", () => {
  const snap = ogSnapshot({ book, summary, trades, curves, scorecard, meta });
  const view = cryptoBookView(book, summary);
  assert.equal(snap.balance, view.runningBalance);
  const svg = buildOgSvg(snap);
  assert.match(svg, /^<svg[^>]+width="1200"[^>]+height="630"/);
  assert.ok(svg.includes("THE BOOK"));
  if (view.runningBalance != null) {
    assert.ok(svg.includes(view.runningBalance.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })));
  }
  for (const banned of ["order_id", "account_id", "project_ref", meta.project_ref].filter(Boolean)) {
    assert.equal(svg.includes(banned), false, banned);
  }
  assert.equal(xmlEscape(`<a & "b">`), "&lt;a &amp; &quot;b&quot;&gt;");
  const empty = buildOgSvg(ogSnapshot({ book: null, summary: [], trades: [], curves: { series: [] }, scorecard: null, meta: {} }));
  assert.ok(empty.includes("No curve in this snapshot"));
});

test("pages carry Open Graph and large-image Twitter cards", () => {
  for (const file of ["index.html", "runs.html"]) {
    const html = src(file);
    assert.match(html, /<meta property="og:title" content="[^"]+">/, file);
    assert.match(html, /<meta property="og:description" content="[^"]+">/, file);
    assert.match(html, /<meta property="og:image" content="https:\/\/jrg185\.github\.io\/the-book\/og\.png">/, file);
    assert.match(html, /<meta name="twitter:card" content="summary_large_image">/, file);
    assert.match(html, /<meta name="twitter:image" content="https:\/\/jrg185\.github\.io\/the-book\/og\.png">/, file);
  }
  const png = readFileSync(new URL("../og.png", import.meta.url));
  assert.equal(png.subarray(1, 4).toString("ascii"), "PNG");
  assert.equal(png.readUInt32BE(16), 1200);
  assert.equal(png.readUInt32BE(20), 630);
});

test("og workflow commits only og.png and leaves Export KPI alone", () => {
  const workflow = src(".github/workflows/og-card.yml");
  const adds = workflow.split("\n").filter((line) => line.includes("git add"));
  assert.deepEqual(adds.map((line) => line.trim()), ["git add -- og.png"]);
  assert.match(workflow, /cron:/);
  assert.match(workflow, /node scripts\/render_og\.mjs/);
  assert.equal(workflow.includes("export_kpi"), false);
  assert.equal(workflow.includes("secrets."), false);
});
