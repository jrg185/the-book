const TRUE_TEXT = new Set(["true", "t", "1", "yes", "y"]);
const FALSE_TEXT = new Set(["false", "f", "0", "no", "n"]);

export function forbiddenField(key) {
  const text = String(key);
  const lowered = text.toLowerCase();
  return (
    text.includes("$") ||
    lowered.includes("usd") ||
    lowered.includes("account") ||
    lowered.includes("order_id") ||
    lowered.includes("fill_trade_id")
  );
}

export function asBool(value) {
  if (typeof value === "boolean") return value;
  if (value == null || value === "") return null;
  if (value === 1) return true;
  if (value === 0) return false;
  const text = String(value).trim().toLowerCase();
  if (TRUE_TEXT.has(text)) return true;
  if (FALSE_TEXT.has(text)) return false;
  return null;
}

export function asTraded(value) {
  return asBool(value) === true;
}

function asText(value) {
  if (value == null || typeof value === "object") return null;
  const text = String(value).split("$").join("").trim();
  return text || null;
}

const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

// Shown text never carries a UUID. id, run_id, and model_version_id stay
// opaque join keys and are never rendered.
export function asDisplay(value) {
  const text = asText(value);
  if (!text) return null;
  return text.replace(UUID_RE, "[id]");
}

function asNumber(value) {
  if (typeof value === "boolean" || value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function copyAllow(row, keys) {
  const out = {};
  for (const key of keys) {
    if (!forbiddenField(key)) out[key] = row[key];
  }
  return out;
}

export function formatWhen(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return asDisplay(value) || "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

export function formatProb(value) {
  const number = asNumber(value);
  if (number == null) return "";
  return `${(number * 100).toFixed(1)}%`;
}

export function formatReturn(value) {
  const number = asNumber(value);
  if (number == null) return "";
  const points = number * 100;
  const sign = points > 0 ? "+" : "";
  return `${sign}${points.toFixed(2)}%`;
}

export function fullPrecisionLine(fullPrecision) {
  if (!fullPrecision || !fullPrecision.nScored) return "";
  return `Full-precision only: n=${fullPrecision.nScored.toLocaleString("en-US")}, logloss ${formatScore(fullPrecision.logloss)}, Brier ${formatScore(fullPrecision.brier)}`;
}

export function formatScore(value) {
  const number = asNumber(value);
  if (number == null) return "";
  return number.toFixed(4);
}

export function formatMetricValue(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return String(Math.round(value * 10000) / 10000);
  }
  if (typeof value === "boolean") return value ? "true" : "false";
  if (value == null || typeof value === "object") return "";
  return asDisplay(value) || "";
}

export function metricEntries(metrics) {
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return [];
  return Object.keys(metrics)
    .filter((key) => !forbiddenField(key))
    .sort()
    .map((key) => ({ key, value: formatMetricValue(metrics[key]) }));
}

export function countLabel(count, singular, plural) {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function versionNames(bundle) {
  const names = new Map();
  for (const version of bundle?.modelVersions || []) {
    if (version.id && version.name) names.set(version.id, version.name);
  }
  return names;
}

export function runModelName(run, names) {
  if (!run) return null;
  return (
    run.model_version_name ||
    run.model_version ||
    (run.model_version_id && names ? names.get(run.model_version_id) : null) ||
    null
  );
}

export function runModelLabel(run, names) {
  const name = runModelName(run, names);
  if (name) return name;
  if (run && run.backend === "rules") return "Rules engine";
  return "Unnamed model";
}

export function runLabel(run, names) {
  return [formatWhen(run.generated_at), runModelLabel(run, names), run.backend || ""]
    .filter(Boolean)
    .join(" · ");
}

const OOS_ORDER = [
  ["auc", "AUC", "score"],
  ["hit_rate", "Hit rate", "prob"],
  ["after_cost_mean", "After cost", "return"],
  ["buy_hit_rate", "Buy hit", "prob"],
  ["buy_mean_net", "Buy net", "return"],
  ["sell_hit_rate", "Sell hit", "prob"],
  ["sell_mean_net", "Sell net", "return"],
  ["logloss", "Log loss", "score"],
  ["brier", "Brier", "score"],
  ["n_buy", "Buys", "count"],
  ["n_sell", "Sells", "count"],
  ["n_long", "Longs", "count"],
  ["n_flat", "Flats", "count"],
];

function compactCount(value) {
  if (Math.abs(value) >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  if (Math.abs(value) >= 1e4) return `${Math.round(value / 1e3)}k`;
  return String(Math.round(value));
}

// Known OOS keys first, in a fixed order, then anything else the registry adds.
export function oosChips(metrics) {
  if (!metrics || typeof metrics !== "object" || Array.isArray(metrics)) return [];
  const chips = [];
  const seen = new Set();
  for (const [key, label, kind] of OOS_ORDER) {
    if (forbiddenField(key)) continue;
    const number = asNumber(metrics[key]);
    if (number == null) continue;
    seen.add(key);
    let value;
    let tone = "";
    if (kind === "prob") value = formatProb(number);
    else if (kind === "return") {
      value = formatReturn(number);
      tone = number > 0 ? "up" : number < 0 ? "down" : "";
    } else if (kind === "count") value = compactCount(number);
    else value = number.toFixed(4);
    chips.push({ key, label, value, tone, kind });
  }
  for (const entry of metricEntries(metrics)) {
    if (seen.has(entry.key) || entry.value === "") continue;
    chips.push({ key: entry.key, label: entry.key, value: entry.value, tone: "", kind: "other" });
  }
  return chips;
}

function predictionHit(row) {
  if (typeof row.label_up_net30 !== "boolean") return null;
  if (row.side === "buy" || row.side === "long") return row.label_up_net30;
  if (row.side === "sell" || row.side === "short") return !row.label_up_net30;
  return null;
}

export function runStats(rows) {
  const stats = { n: 0, buy: 0, sell: 0, flat: 0, traded: 0, resolved: 0, calls: 0, hits: 0, hitRate: null };
  for (const row of rows || []) {
    stats.n += 1;
    if (row.side === "buy" || row.side === "long") stats.buy += 1;
    else if (row.side === "sell" || row.side === "short") stats.sell += 1;
    else stats.flat += 1;
    if (row.traded) stats.traded += 1;
    if (row.resolved_at) stats.resolved += 1;
    const hit = predictionHit(row);
    if (hit != null) {
      stats.calls += 1;
      if (hit) stats.hits += 1;
    }
  }
  stats.hitRate = stats.calls ? stats.hits / stats.calls : null;
  return stats;
}

export function predictionsByRun(bundle) {
  const map = new Map();
  for (const row of bundle?.predictions || []) {
    if (!row.run_id) continue;
    if (!map.has(row.run_id)) map.set(row.run_id, []);
    map.get(row.run_id).push(row);
  }
  return map;
}

// Anchors use the run clock only so a shared link never carries a run id.
export function runAnchors(runs) {
  const used = new Map();
  return (runs || []).map((run) => {
    const ms = Date.parse(run.generated_at || "");
    const base = Number.isNaN(ms)
      ? "run"
      : `run-${new Date(ms).toISOString().replace(/\.\d+Z$/, "").replace(/[-:]/g, "").replace("T", "-")}`;
    const n = (used.get(base) || 0) + 1;
    used.set(base, n);
    return n === 1 ? base : `${base}-${n}`;
  });
}

export function predictionsFor(bundle, runId) {
  if (!runId) return bundle.predictions;
  return bundle.predictions.filter((row) => row.run_id === runId);
}

function normalizeRun(row) {
  if (!row || typeof row !== "object") return null;
  const run = copyAllow(row, [
    "id",
    "model_version_id",
    "model_version",
    "model_version_name",
    "backend",
    "mode",
    "generated_at",
    "last_bar_time",
    "anomaly",
  ]);
  run.id = asText(run.id);
  run.model_version_id = asText(run.model_version_id);
  run.model_version = asDisplay(run.model_version);
  run.model_version_name = asDisplay(run.model_version_name);
  run.backend = asDisplay(run.backend);
  run.mode = asDisplay(run.mode);
  run.generated_at = asText(run.generated_at);
  run.last_bar_time = asText(run.last_bar_time);
  run.anomaly = asBool(run.anomaly);
  if (!run.id && !run.generated_at) return null;
  return run;
}

function normalizePrediction(row) {
  if (!row || typeof row !== "object") return null;
  const prediction = copyAllow(row, [
    "run_id",
    "symbol",
    "p_up",
    "side",
    "conviction",
    "bar_time",
    "expires_at",
    "fwd_ret_8h",
    "label_up_net30",
    "resolved_at",
    "traded",
  ]);
  prediction.run_id = asText(prediction.run_id);
  prediction.symbol = asDisplay(prediction.symbol);
  prediction.p_up = asNumber(prediction.p_up);
  prediction.side = asDisplay(prediction.side);
  prediction.conviction = asNumber(prediction.conviction);
  prediction.bar_time = asText(prediction.bar_time);
  prediction.expires_at = asText(prediction.expires_at);
  prediction.fwd_ret_8h = asNumber(prediction.fwd_ret_8h);
  prediction.label_up_net30 = asBool(prediction.label_up_net30);
  prediction.resolved_at = asText(prediction.resolved_at);
  prediction.traded = asTraded(prediction.traded);
  if (!prediction.run_id && !prediction.symbol) return null;
  return prediction;
}

function normalizeVersion(row) {
  if (!row || typeof row !== "object") return null;
  const version = copyAllow(row, ["id", "name", "parent", "status", "oos_metrics", "trained_at", "promoted_at", "created_at"]);
  version.id = asText(version.id);
  version.name = asDisplay(version.name);
  version.parent = asDisplay(version.parent);
  version.status = asDisplay(version.status);
  version.trained_at = asText(version.trained_at);
  version.promoted_at = asText(version.promoted_at);
  version.created_at = asText(version.created_at);
  const metrics = version.oos_metrics;
  if (metrics && typeof metrics === "object" && !Array.isArray(metrics)) {
    const clean = {};
    for (const key of Object.keys(metrics)) {
      if (forbiddenField(key)) continue;
      const value = metrics[key];
      if (value != null && typeof value === "object") continue;
      clean[key] = typeof value === "string" ? asDisplay(value) : value;
    }
    version.oos_metrics = clean;
  } else if (typeof metrics === "string") {
    version.oos_metrics = asDisplay(metrics);
  } else if (metrics != null && typeof metrics !== "number" && typeof metrics !== "boolean") {
    version.oos_metrics = null;
  }
  if (!version.id && !version.name) return null;
  return version;
}

function normalizeFullPrecision(raw) {
  if (!raw || typeof raw !== "object") return null;
  return {
    nScored: Number.isInteger(raw.n_scored) ? raw.n_scored : 0,
    logloss: asNumber(raw.logloss),
    brier: asNumber(raw.brier),
  };
}

function normalizeCalibration(raw) {
  const body = raw && typeof raw === "object" ? raw : {};
  const bins = Array.isArray(body.bins) ? body.bins : [];
  return {
    nResolved: Number.isInteger(body.n_resolved) ? body.n_resolved : 0,
    nScored: Number.isInteger(body.n_scored) ? body.n_scored : null,
    nUnscored: Number.isInteger(body.n_unscored) ? body.n_unscored : null,
    logloss: asNumber(body.logloss),
    brier: asNumber(body.brier),
    fullPrecision: normalizeFullPrecision(body.full_precision),
    bins: bins.map((slot) => ({
      lo: asNumber(slot && slot.lo),
      hi: asNumber(slot && slot.hi),
      n: slot && Number.isFinite(Number(slot.n)) ? Number(slot.n) : 0,
      meanPUp: asNumber(slot && slot.mean_p_up),
      labelRate: asNumber(slot && slot.label_rate),
    })),
  };
}

export function normalizeBundle(raw) {
  const body = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    source: body.source === "supabase" ? "supabase" : "empty",
    fetchedAt: asText(body.fetched_at),
    runs: (Array.isArray(body.runs) ? body.runs : []).map(normalizeRun).filter(Boolean),
    predictions: (Array.isArray(body.predictions) ? body.predictions : []).map(normalizePrediction).filter(Boolean),
    modelVersions: (Array.isArray(body.model_versions) ? body.model_versions : []).map(normalizeVersion).filter(Boolean),
    calibration: normalizeCalibration(body.calibration),
  };
}

export function emptyBundle() {
  return normalizeBundle({
    source: "empty",
    fetched_at: null,
    runs: [],
    predictions: [],
    model_versions: [],
    calibration: { n_resolved: 0, logloss: null, brier: null, bins: [] },
  });
}
