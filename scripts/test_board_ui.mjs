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
  seedReturn,
  shareUrl,
  tradeAnchors,
  tradeSlug,
  windowChange,
  windowPoints,
} from "../board.js";
import { cryptoBookView, killRemainingUsd, railUsd, SEEDS_USD } from "../derive.js";
import { buildOgSvg, ogSnapshot, xmlEscape } from "../og.js";
import { preferredWhy } from "../tape.js";
import {
  normalizeBundle,
  oosChips,
  predictionsByRun,
  runAnchors,
  runModelLabel,
  runModelName,
  runStats,
  versionNames,
} from "../runs/derive.js";

const src = (name) => readFileSync(new URL(`../${name}`, import.meta.url), "utf8");
const data = (name) => JSON.parse(src(`data/${name}`));

const summary = data("kpi_summary.json");
const scorecard = data("model_scorecard.json");
const book = data("live_book.json");
const trades = data("kpi_trades_scrubbed.json");
const curves = data("sleeve_curves.json");
const meta = data("meta.json");

const FRONT = ["app.js", "board.js", "og.js", "charts.js", "index.html", "runs.html", "runs/index.html", "runs/app.js", "runs/derive.js"];

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
  const r = railView(view.runningBalance, view.dayPnl, rails, view.dayOpen);
  if (rails.killFrac != null) {
    assert.equal(r.headroomUsd, killRemainingUsd(view.runningBalance, view.dayOpen, rails.killFrac));
    assert.equal(r.killUsd, railUsd(view.runningBalance, rails.killFrac));
    assert.equal(r.killUsd, scorecard.kill.day_kill_usd);
  }
  const flat = railView(1000, 0, { killFrac: -0.1, targetFrac: 0.025 }, 1000);
  assert.equal(flat.killUsd, -100);
  assert.equal(flat.headroomUsd, 100);
  const losing = railView(975, -25, { killFrac: -0.1, targetFrac: 0.025 }, 1000);
  assert.equal(losing.headroomUsd, 75);
  assert.equal(losing.killUsedFrac, 0.25);
  assert.equal(losing.targetUsd, railUsd(975, 0.025));
  assert.equal(losing.targetProgress, 0);
  const winning = railView(1032, 32, { killFrac: -0.1, targetFrac: 0.025 }, 1000);
  assert.equal(winning.headroomUsd, 132);
  assert.equal(winning.targetProgress, 1);
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
  for (const file of FRONT) {
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

test("runs page names the model from model_version_name and keeps run ids out of links", () => {
  const bundle = normalizeBundle({
    source: "supabase",
    fetched_at: "2026-10-11T17:00:00Z",
    runs: [
      { id: "4f7c2a8e-1b2d-4c3e-9f10-112233445566", model_version_name: "lgbm_v1", backend: "ml", generated_at: "2026-10-11T16:31:53+00:00" },
      { id: "r-2", model_version_id: "v-2", backend: "ml", generated_at: "2026-10-11T16:31:53+00:00" },
      { id: "r-3", backend: "rules", generated_at: "2026-10-10T10:00:00+00:00" },
      { id: "r-4", backend: "ml", generated_at: "2026-10-10T09:00:00+00:00" },
    ],
    predictions: [
      { run_id: "r-2", symbol: "BTC", side: "buy", p_up: 0.6, label_up_net30: true, resolved_at: "2026-10-12T00:00:00Z" },
      { run_id: "r-2", symbol: "ETH", side: "sell", p_up: 0.4, label_up_net30: true, resolved_at: "2026-10-12T00:00:00Z" },
      { run_id: "r-2", symbol: "SOL", side: "flat", p_up: 0.5 },
    ],
    model_versions: [{ id: "v-2", name: "lgbm_no_dow", status: "candidate" }],
  });
  const names = versionNames(bundle);
  assert.equal(runModelName(bundle.runs[0], names), "lgbm_v1");
  assert.equal(runModelName(bundle.runs[1], names), "lgbm_no_dow");
  assert.equal(runModelLabel(bundle.runs[2], names), "Rules engine");
  assert.equal(runModelLabel(bundle.runs[3], names), "Unnamed model");
  const anchors = runAnchors(bundle.runs);
  assert.deepEqual(anchors, ["run-20261011-163153", "run-20261011-163153-2", "run-20261010-100000", "run-20261010-090000"]);
  for (const id of anchors) assert.equal(/4f7c|r-\d/.test(id), false, id);
  const stats = runStats(predictionsByRun(bundle).get("r-2"));
  assert.deepEqual([stats.n, stats.buy, stats.sell, stats.flat, stats.calls, stats.hits], [3, 1, 1, 1, 2, 1]);
  assert.equal(stats.hitRate, 0.5);

  const real = normalizeBundle(data("runs.json"));
  assert.ok(real.runs.length > 0);
  for (const run of real.runs) assert.equal(typeof runModelLabel(run, versionNames(real)), "string");
  const realAnchors = runAnchors(real.runs);
  assert.equal(new Set(realAnchors).size, realAnchors.length);
  for (const id of realAnchors) assert.match(id, /^run-\d{8}-\d{6}(-\d+)?$/);
  const page = src("runs/app.js");
  assert.equal(page.includes("option.value = run.id"), false);
  assert.equal(page.includes("tr.id = run.id"), false);
});

test("run ids are opaque join keys, hashed or raw, and never reach shown text", () => {
  const uuid = "4f7c2a8e-1b2d-4c3e-9f10-112233445566";
  for (const key of ["k9f3a2c1", uuid]) {
    const bundle = normalizeBundle({
      source: "supabase",
      runs: [{ id: key, model_version: uuid, backend: "ml", mode: `live ${uuid}`, generated_at: "2026-10-11T16:31:53+00:00" }],
      predictions: [{ run_id: key, symbol: `BTC ${uuid}`, side: "sell", p_up: 0.3 }],
      model_versions: [{ id: uuid, name: `lgbm ${uuid}`, parent: uuid, status: "promoted", oos_metrics: { note: `see ${uuid}`, auc: 0.5 } }],
    });
    const run = bundle.runs[0];
    assert.equal(run.id, key);
    assert.equal(predictionsByRun(bundle).get(run.id).length, 1);
    assert.deepEqual(runAnchors(bundle.runs), ["run-20261011-163153"]);
    const shown = JSON.stringify([
      runModelLabel(run, versionNames(bundle)),
      run.mode,
      bundle.predictions[0].symbol,
      bundle.modelVersions[0].name,
      bundle.modelVersions[0].parent,
      oosChips(bundle.modelVersions[0].oos_metrics),
    ]);
    assert.equal(shown.includes("4f7c2a8e"), false, shown);
    assert.ok(shown.includes("[id]"));
  }
  for (const file of ["runs/app.js", "app.js"]) {
    const lines = src(file).split("\n").filter((line) => /\b(run|latest|row|item)\.(id|run_id)\b/.test(line));
    for (const line of lines) assert.match(line, /byRun\.get\(|predictionsByRun\(bundle\)\.get\(/, `${file}: ${line.trim()}`);
  }
});

test("out-of-sample metrics become ordered chips without private keys", () => {
  const chips = oosChips({ auc: 0.5303, hit_rate: 0.524, buy_mean_net: 0.0053, sell_mean_net: -0.0029, n_flat: 414038, fee_usd: 1, account_x: 2, extra: 3 });
  assert.deepEqual(chips.map((chip) => chip.key), ["auc", "hit_rate", "buy_mean_net", "sell_mean_net", "n_flat", "extra"]);
  assert.equal(chips[0].value, "0.5303");
  assert.equal(chips[1].value, "52.4%");
  assert.equal(chips[2].value, "+0.53%");
  assert.equal(chips[2].tone, "up");
  assert.equal(chips[3].tone, "down");
  assert.equal(chips[4].value, "414k");
  assert.deepEqual(oosChips(null), []);
  for (const version of normalizeBundle(data("runs.json")).modelVersions) {
    const metrics = version.oos_metrics;
    if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) continue;
    if (Object.keys(metrics).length === 0) continue;
    assert.ok(oosChips(metrics).length > 0, version.name);
  }
});

test("runs.html only redirects to the one runs page", () => {
  const html = src("runs.html");
  assert.match(html, /http-equiv="refresh" content="0; url=runs\/"/);
  assert.equal(html.includes("<script type=\"module\""), false);
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
  for (const file of ["index.html", "runs/index.html"]) {
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
