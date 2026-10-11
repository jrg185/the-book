import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  countLabel,
  emptyBundle,
  forbiddenField,
  formatProb,
  formatReturn,
  normalizeBundle,
  predictionsFor,
} from "../runs/derive.js";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

test("the board links to the scoring runs page", () => {
  const html = read("index.html");
  const links = html.match(/href="runs\/"/g) || [];
  assert.equal(links.length, 2);
  const page = read("runs/index.html");
  assert.equal(page.includes('src="app.js"'), true);
  assert.equal(page.includes('href="../styles.css"'), true);
  assert.equal(page.includes('id="calibration"'), true);
  assert.equal(page.includes('id="predictions"'), true);
  assert.equal(page.includes('id="versions"'), true);
  assert.equal(page.includes('id="runs"'), true);
  assert.equal(read("runs/app.js").includes("../data/runs.json"), true);
});

test("the runs page does not format dollars or keep private fields", () => {
  for (const path of ["runs/index.html", "runs/app.js", "runs/derive.js"]) {
    const text = read(path);
    assert.equal(text.includes("formatUsd"), false, path);
  }
  for (const path of ["data/runs.json", "data/runs.csv"]) {
    const text = read(path);
    assert.equal(text.includes("$"), false, path);
    assert.equal(text.toLowerCase().includes("fill_trade_id"), false, path);
    assert.equal(text.toLowerCase().includes("order_id"), false, path);
    assert.equal(/(^|[,{])"?[a-z0-9_]*usd[a-z0-9_]*"?\s*:/i.test(text), false, path);
    assert.equal(/(^|[,{])"?[a-z0-9_]*account[a-z0-9_]*"?\s*:/i.test(text), false, path);
  }
  assert.equal(forbiddenField("balance_usd"), true);
  assert.equal(forbiddenField("account_id"), true);
  assert.equal(forbiddenField("p_up"), false);

  const bundle = normalizeBundle({
    source: "supabase",
    fetched_at: "2026-10-11T16:00:00Z",
    runs: [
      {
        id: "r1",
        model_version: "one",
        backend: "ml",
        mode: "paper",
        generated_at: "2026-10-11T16:00:00Z",
        last_bar_time: "2026-10-11T15:00:00Z",
        anomaly: false,
        order_id: "nope",
        fill_trade_id: "nope",
        account_id: "nope",
        pnl_usd: 4,
      },
    ],
    predictions: [
      {
        run_id: "r1",
        symbol: "BTC",
        p_up: 0.8,
        side: "long",
        conviction: 0.2,
        bar_time: "2026-10-11T15:00:00Z",
        expires_at: "2026-10-11T23:00:00Z",
        fwd_ret_8h: 0.01,
        label_up_net30: true,
        resolved_at: "2026-10-11T23:00:00Z",
        traded: true,
        realized_pnl_usd: 3,
      },
      { run_id: "r2", symbol: "ETH", p_up: 0.2, traded: false },
    ],
    model_versions: [
      { id: "m", name: "one", status: "promoted", oos_metrics: { auc: 0.5, fee_usd: 1 } },
    ],
    calibration: {
      n_resolved: 1,
      logloss: 0.2,
      brier: 0.1,
      bins: [{ lo: 0.8, hi: 0.9, n: 1, mean_p_up: 0.8, label_rate: 1 }],
    },
  });
  const blob = JSON.stringify(bundle);
  assert.equal(blob.includes("order_id"), false);
  assert.equal(blob.includes("fill_trade_id"), false);
  assert.equal(blob.includes("usd"), false);
  assert.equal(blob.includes("$"), false);
  assert.equal(blob.includes("account"), false);
  assert.equal(bundle.predictions[0].traded, true);
  assert.equal(typeof bundle.predictions[0].traded, "boolean");
  assert.equal(predictionsFor(bundle, "r1").length, 1);
  assert.equal(bundle.modelVersions[0].oos_metrics.auc, 0.5);
  assert.equal(bundle.modelVersions[0].oos_metrics.fee_usd, undefined);
  assert.equal(countLabel(1, "model version", "model versions"), "1 model version");
  assert.equal(countLabel(2, "run", "runs"), "2 runs");
  assert.equal(formatProb(0.8), "80.0%");
  assert.equal(formatReturn(0.01), "+1.00%");
  assert.equal(formatReturn(-0.02), "-2.00%");
  assert.equal(emptyBundle().source, "empty");
  assert.equal(emptyBundle().calibration.nResolved, 0);
});
