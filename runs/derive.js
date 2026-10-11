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
  if (Number.isNaN(date.getTime())) return asText(value) || "";
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
  return String(value).split("$").join("");
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

export function runLabel(run) {
  return [formatWhen(run.generated_at), run.model_version || "unknown model", run.backend || ""]
    .filter(Boolean)
    .join(" · ");
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
    "backend",
    "mode",
    "generated_at",
    "last_bar_time",
    "anomaly",
  ]);
  run.id = asText(run.id);
  run.model_version_id = asText(run.model_version_id);
  run.model_version = asText(run.model_version);
  run.backend = asText(run.backend);
  run.mode = asText(run.mode);
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
  prediction.symbol = asText(prediction.symbol);
  prediction.p_up = asNumber(prediction.p_up);
  prediction.side = asText(prediction.side);
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
  version.name = asText(version.name);
  version.parent = asText(version.parent);
  version.status = asText(version.status);
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
      clean[key] = typeof value === "string" ? value.split("$").join("") : value;
    }
    version.oos_metrics = clean;
  } else if (typeof metrics === "string") {
    version.oos_metrics = metrics.split("$").join("");
  } else if (metrics != null && typeof metrics !== "number" && typeof metrics !== "boolean") {
    version.oos_metrics = null;
  }
  if (!version.id && !version.name) return null;
  return version;
}

function normalizeCalibration(raw) {
  const body = raw && typeof raw === "object" ? raw : {};
  const bins = Array.isArray(body.bins) ? body.bins : [];
  return {
    nResolved: Number.isInteger(body.n_resolved) ? body.n_resolved : 0,
    logloss: asNumber(body.logloss),
    brier: asNumber(body.brier),
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
