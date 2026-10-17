// Scoreboard helpers. No DOM, no network. Every number comes from data/*.json
// or config/*.json. Rails, seeds, fees, and prices are never written here.

import { killRemainingUsd, num, publishedRails, railUsd, SEEDS_USD, sleeveKey } from "./derive.js";

export const SITE_URL = "https://jrg185.github.io/the-book/";

export const RANGES = [
  { key: "24h", label: "24H", ms: 24 * 60 * 60 * 1000 },
  { key: "7d", label: "7D", ms: 7 * 24 * 60 * 60 * 1000 },
  { key: "30d", label: "30D", ms: 30 * 24 * 60 * 60 * 1000 },
  { key: "all", label: "ALL", ms: null },
];

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

// All-time return of the account against the combined seed in config/book_seeds.json.
export function seedReturn(runningPnl, seeds = SEEDS_USD) {
  const pnl = num(runningPnl);
  const seed = num(seeds?.combined);
  if (pnl == null || !seed) return null;
  return pnl / seed;
}

// Day rails come from the warehouse export: the crypto row of kpi_summary,
// then the scorecard kill block. A rail that neither file publishes stays null.
// publishedRails in derive.js is that read. This file does not keep a fraction.
export function liveRails(summary, scorecard) {
  const rails = publishedRails(summary, scorecard);
  return {
    killFrac: rails.dayKillFrac,
    targetFrac: rails.dayTargetFrac,
    source: rails.source,
    asOf: rails.asOf,
  };
}

// Dollars for the rail bars. Both rails are fractions of the running balance
// (RH cash plus open lots). Headroom uses the same formula export writes,
// including open losses.
export function railView(balance, dayPnl, rails, openPnl = null) {
  const total = num(balance);
  const day = num(dayPnl);
  const killFrac = rails?.killFrac ?? null;
  const targetFrac = rails?.targetFrac ?? null;
  const out = {
    balance: total,
    dayPnl: day,
    killFrac,
    targetFrac,
    killUsd: null,
    killUsedFrac: null,
    headroomUsd: null,
    headroomFrac: null,
    targetUsd: null,
    targetProgress: null,
  };
  if (total == null || total === 0) return out;
  if (killFrac != null) {
    const budget = Math.abs(killFrac) * total;
    out.killUsd = railUsd(total, killFrac);
    out.headroomUsd = killRemainingUsd(total, day, killFrac, openPnl);
    out.headroomFrac = out.headroomUsd == null ? null : out.headroomUsd / total;
    out.killUsedFrac =
      budget === 0 || out.headroomUsd == null ? null : clamp01((budget - out.headroomUsd) / budget);
  }
  if (targetFrac != null) {
    out.targetUsd = railUsd(total, targetFrac);
    out.targetProgress = out.targetUsd === 0 ? null : clamp01(Math.max(0, day ?? 0) / out.targetUsd);
  }
  return out;
}

// Crypto sleeve history as [{ ms, asOf, frac }], oldest first.
export function curvePoints(payload, sleeve = "crypto") {
  const rows = Array.isArray(payload) ? payload : payload?.series;
  if (!Array.isArray(rows)) return [];
  const points = [];
  for (const row of rows) {
    if (!row || sleeveKey(row) !== sleeve) continue;
    const frac = num(row.running_balance_frac);
    const ms = Date.parse(row.as_of);
    if (frac == null || Number.isNaN(ms)) continue;
    points.push({ ms, asOf: row.as_of, frac });
  }
  points.sort((a, b) => a.ms - b.ms);
  return points;
}

export function rangeSpan(key) {
  const found = RANGES.find((range) => range.key === key);
  return found ? found.ms : null;
}

// Points inside the window ending at the last point. The point just before the
// window is kept as the base so the change reads from the window's open.
export function windowPoints(points, key) {
  const list = Array.isArray(points) ? points : [];
  const span = rangeSpan(key);
  if (!list.length || span == null) return list.slice();
  const cutoff = list[list.length - 1].ms - span;
  let start = list.findIndex((point) => point.ms >= cutoff);
  if (start < 0) start = list.length - 1;
  if (start > 0) start -= 1;
  return list.slice(start);
}

export function windowChange(points) {
  if (!Array.isArray(points) || points.length < 2) return null;
  const first = points[0].frac;
  const last = points[points.length - 1].frac;
  if (!first) return null;
  return last / first - 1;
}

// Underwater: each point's distance below the running peak, as a fraction.
export function drawdownSeries(points) {
  const list = Array.isArray(points) ? points : [];
  let peak = -Infinity;
  return list.map((point) => {
    peak = Math.max(peak, point.frac);
    return { ms: point.ms, asOf: point.asOf, dd: peak > 0 ? point.frac / peak - 1 : 0 };
  });
}

export function drawdownStats(points) {
  const series = drawdownSeries(points);
  if (!series.length) return { max: null, current: null, maxAt: null };
  let worst = series[0];
  for (const point of series) if (point.dd < worst.dd) worst = point;
  return { max: worst.dd, current: series[series.length - 1].dd, maxAt: worst.asOf };
}

export function etDayKey(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(date);
  const get = (type) => parts.find((part) => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

// Daily Sharpe from the last curve point of each ET day, annualized by √365
// because crypto trades every day. Fewer than five daily returns is null.
export function dailySharpe(points) {
  const byDay = new Map();
  for (const point of Array.isArray(points) ? points : []) byDay.set(etDayKey(point.ms), point.frac);
  const closes = [...byDay.values()];
  const returns = [];
  for (let i = 1; i < closes.length; i += 1) {
    if (closes[i - 1]) returns.push(closes[i] / closes[i - 1] - 1);
  }
  if (returns.length < 5) return { value: null, days: returns.length };
  const mean = returns.reduce((sum, r) => sum + r, 0) / returns.length;
  const variance = returns.reduce((sum, r) => sum + (r - mean) ** 2, 0) / (returns.length - 1);
  const sd = Math.sqrt(variance);
  return { value: sd === 0 ? null : (mean / sd) * Math.sqrt(365), days: returns.length };
}

function compactUtc(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().replace(/\.\d+Z$/, "Z").replace(/[-:]/g, "").replace("T", "-").replace("Z", "");
}

function slugPart(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

// Permalink ids are built only from fields already on the page: the fill time,
// the ticker, and the side. Order, fill, and account ids are never read.
export function tradeSlug(trade, stampOf = (row) => row?.timestamp_et || row?.ts) {
  const stamp = compactUtc(stampOf(trade));
  const bits = [stamp, slugPart(trade?.ticker), slugPart(trade?.side)].filter(Boolean);
  return bits.length ? `trade-${bits.join("-")}` : "";
}

export function tradeAnchors(trades, stampOf) {
  const ids = new Map();
  const used = new Map();
  for (const trade of Array.isArray(trades) ? trades : []) {
    const base = tradeSlug(trade, stampOf);
    if (!base) continue;
    const n = (used.get(base) || 0) + 1;
    used.set(base, n);
    ids.set(trade, n === 1 ? base : `${base}-${n}`);
  }
  return ids;
}

export function parseHash(hash) {
  const raw = String(hash || "").replace(/^#/, "");
  const day = /^day-(\d{4}-\d{2}-\d{2})$/.exec(raw);
  if (day) return { kind: "day", day: day[1] };
  if (/^trade-[a-z0-9-]+$/.test(raw)) return { kind: "trade", id: raw };
  if (raw === "models" || raw.startsWith("model-")) return { kind: "models", id: raw };
  return { kind: raw ? "anchor" : "none", id: raw };
}

export function shareUrl(text, url) {
  const params = new URLSearchParams();
  if (text) params.set("text", text);
  if (url) params.set("url", url);
  return `https://x.com/intent/tweet?${params.toString()}`;
}

export function permalink(id, base = SITE_URL) {
  return id ? `${base}#${id}` : base;
}

const UUID_RE = /(?<!bc-)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi;
const LONG_HEX_RE = /(?<!bc-)\b[0-9a-f]{24,}\b/gi;

// Free text can carry a pasted broker id. Mask it before it reaches the page.
export function redactIds(text) {
  if (text == null) return "";
  return String(text).replace(UUID_RE, "[id]").replace(LONG_HEX_RE, "[id]");
}

export function agoText(ms, now = Date.now()) {
  if (ms == null || Number.isNaN(ms)) return "";
  const minutes = Math.max(0, Math.round((now - ms) / 60000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

// Ranked by after-cost mean, the metric the OOS table promotes on.
export function rankModels(rows) {
  const list = (Array.isArray(rows) ? rows : []).filter((row) => row && typeof row === "object");
  return list
    .map((row) => ({
      model: String(row.model || row.name || "model"),
      afterCost: num(row.after_cost_mean),
      ir: num(row.sleeve_ir_vs_spy),
      sharpe: num(row.sleeve_sharpe),
      auc: num(row.auc),
      nLong: num(row.n_long),
      promoted: row.promoted === true,
    }))
    .sort((a, b) => (b.afterCost ?? -Infinity) - (a.afterCost ?? -Infinity));
}

// Most recent fill whose why is human text, for the "latest call" pane.
export function latestCall(trades, preferredWhy, stampOf) {
  let best = null;
  let bestMs = -Infinity;
  for (const trade of Array.isArray(trades) ? trades : []) {
    if (sleeveKey(trade) !== "crypto") continue;
    const why = preferredWhy(trade);
    if (why.kind !== "human") continue;
    const ms = Date.parse(stampOf(trade));
    if (Number.isNaN(ms) || ms < bestMs) continue;
    best = { trade, why: redactIds(why.text) };
    bestMs = ms;
  }
  return best;
}
