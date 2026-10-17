// Daily share card. Pure: data in, SVG text out. scripts/render_og.mjs
// rasterizes it to og.png. Every figure is read from data/*.json.

import { curvePoints, drawdownStats, liveRails, railView, seedReturn, windowPoints } from "./board.js";
import { closedFillStats, cryptoBookView, formatPct, formatUsd, formatWinPct, formatWinRecord, sleeveKey } from "./derive.js";

export const OG_WIDTH = 1200;
export const OG_HEIGHT = 630;

const C = {
  bg: "#0b0d10",
  panel: "#12161b",
  line: "#232a33",
  ink: "#f3f5f7",
  dim: "#8b97a5",
  up: "#2fd17c",
  down: "#ff5d6c",
  accent: "#f5b84b",
};

const FONT = "Inter, 'DejaVu Sans', 'Liberation Sans', Arial, sans-serif";

export function xmlEscape(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function toneColor(value) {
  if (value == null || value === 0) return C.dim;
  return value > 0 ? C.up : C.down;
}

function etStamp(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function sparkPath(points, box) {
  if (points.length < 2) return { line: "", area: "" };
  const t0 = points[0].ms;
  const t1 = points[points.length - 1].ms;
  let lo = Math.min(...points.map((p) => p.frac));
  let hi = Math.max(...points.map((p) => p.frac));
  if (lo === hi) {
    lo -= 0.01;
    hi += 0.01;
  }
  const x = (ms) => box.x + ((ms - t0) / (t1 - t0 || 1)) * box.w;
  const y = (frac) => box.y + box.h - ((frac - lo) / (hi - lo)) * box.h;
  const step = Math.max(1, Math.floor(points.length / 400));
  const kept = points.filter((_, i) => i % step === 0 || i === points.length - 1);
  const coords = kept.map((p) => `${x(p.ms).toFixed(1)},${y(p.frac).toFixed(1)}`);
  const line = `M${coords.join(" L")}`;
  const area = `${line} L${(box.x + box.w).toFixed(1)},${box.y + box.h} L${box.x},${box.y + box.h} Z`;
  return { line, area };
}

// Snapshot fields the card needs, all derived from the committed JSON.
export function ogSnapshot({ book, summary, trades, curves, scorecard, meta }) {
  const view = cryptoBookView(book, summary);
  const points = curvePoints(curves, "crypto");
  const all = windowPoints(points, "all");
  const fills = closedFillStats(trades, "crypto");
  const dd = drawdownStats(all);
  const rails = railView(view?.runningBalance, view?.dayPnl, liveRails(summary, scorecard), view?.unrealizedPnl);
  const cryptoFills = (Array.isArray(trades) ? trades : []).filter((row) => sleeveKey(row) === "crypto").length;
  const sleeveReturn = all.length ? all[all.length - 1].frac - 1 : null;
  return {
    balance: view?.runningBalance ?? null,
    runningPnl: view?.runningPnl ?? null,
    runningPnlFrac: seedReturn(view?.runningPnl),
    dayPnl: view?.dayPnl ?? null,
    asOf: view?.asOf || meta?.fetched_at || null,
    points: all,
    sleeveReturn,
    winRate: fills.rate,
    winRecord: formatWinRecord(fills),
    maxDrawdown: dd.max,
    fills: cryptoFills,
    headroomUsd: rails.headroomUsd,
    killFrac: rails.killFrac,
  };
}

export function buildOgSvg(snap) {
  const s = snap || {};
  const W = OG_WIDTH;
  const H = OG_HEIGHT;
  const spark = sparkPath(s.points || [], { x: 640, y: 150, w: 500, h: 250 });
  const pnlColor = toneColor(s.runningPnl);
  const dayColor = toneColor(s.dayPnl);
  const allTime = s.runningPnl == null
    ? "—"
    : `${formatUsd(s.runningPnl, { signed: true })}  (${formatPct(s.runningPnlFrac, { signed: true, digits: 2 })})`;
  const stats = [
    ["WIN RATE", `${formatWinPct(s.winRate)}`, s.winRecord ? `${s.winRecord} closed` : ""],
    ["MAX DRAWDOWN", formatPct(s.maxDrawdown, { digits: 1 }), "crypto sleeve"],
    ["FILLS", s.fills == null ? "—" : String(s.fills), "crypto tape"],
    ["KILL HEADROOM", formatUsd(s.headroomUsd), s.killFrac == null ? "rail not published" : `${formatPct(s.killFrac)} day rail`],
  ];
  const statW = 270;
  const statCells = stats
    .map(([k, v, sub], i) => {
      const x = 60 + i * (statW + 10);
      return `
  <g transform="translate(${x},458)">
    <rect width="${statW}" height="104" rx="18" fill="${C.panel}" stroke="${C.line}"/>
    <text x="22" y="34" font-size="15" letter-spacing="2" fill="${C.dim}" font-weight="600">${xmlEscape(k)}</text>
    <text x="22" y="72" font-size="34" font-weight="700" fill="${C.ink}">${xmlEscape(v)}</text>
    <text x="22" y="94" font-size="14" fill="${C.dim}">${xmlEscape(sub)}</text>
  </g>`;
    })
    .join("");
  const sleeve = s.sleeveReturn == null ? "" : `Crypto sleeve ${formatPct(s.sleeveReturn, { signed: true, digits: 2 })} vs seed`;
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="${xmlEscape(FONT)}">
  <defs>
    <linearGradient id="fade" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${pnlColor}" stop-opacity="0.35"/>
      <stop offset="1" stop-color="${pnlColor}" stop-opacity="0"/>
    </linearGradient>
    <radialGradient id="glow" cx="0.15" cy="0.1" r="0.9">
      <stop offset="0" stop-color="#1b2430"/>
      <stop offset="1" stop-color="${C.bg}"/>
    </radialGradient>
  </defs>
  <rect width="${W}" height="${H}" fill="url(#glow)"/>
  <g transform="translate(60,66)">
    <rect width="34" height="34" rx="8" fill="${C.accent}"/>
    <text x="50" y="26" font-size="26" font-weight="900" fill="${C.ink}" letter-spacing="1">THE BOOK</text>
    <text x="196" y="26" font-size="18" fill="${C.dim}">· one live crypto book, public scoreboard</text>
  </g>
  <text x="60" y="168" font-size="18" letter-spacing="3" fill="${C.dim}" font-weight="600">ACCOUNT VALUE</text>
  <text x="56" y="262" font-size="96" font-weight="900" fill="${C.ink}" letter-spacing="-2">${xmlEscape(formatUsd(s.balance))}</text>
  <text x="60" y="318" font-size="30" font-weight="700" fill="${pnlColor}">${xmlEscape(allTime)}</text>
  <text x="60" y="350" font-size="17" fill="${C.dim}">all-time vs seed</text>
  <text x="60" y="400" font-size="22" fill="${C.dim}">Day P&amp;L <tspan fill="${dayColor}" font-weight="700">${xmlEscape(formatUsd(s.dayPnl, { signed: true }))}</tspan></text>
  <rect x="620" y="130" width="540" height="290" rx="22" fill="${C.panel}" stroke="${C.line}"/>
  ${spark.area ? `<path d="${spark.area}" fill="url(#fade)"/>` : ""}
  ${spark.line ? `<path d="${spark.line}" fill="none" stroke="${pnlColor}" stroke-width="3.5" stroke-linejoin="round" stroke-linecap="round"/>` : `<text x="890" y="285" text-anchor="middle" font-size="20" fill="${C.dim}">No curve in this snapshot</text>`}
  <text x="640" y="410" font-size="16" fill="${C.dim}">${xmlEscape(sleeve)}</text>
  ${statCells}
  <text x="60" y="600" font-size="17" fill="${C.dim}">jrg185.github.io/the-book</text>
  <text x="${W - 60}" y="600" text-anchor="end" font-size="17" fill="${C.dim}">${xmlEscape(s.asOf ? `as of ${etStamp(s.asOf)}` : "")}</text>
</svg>
`;
}
