import {
  RANGES,
  SITE_URL,
  agoText,
  curvePoints,
  dailySharpe,
  drawdownSeries,
  drawdownStats,
  etDayKey,
  latestCall,
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
  windowChange,
  windowPoints,
} from "./board.js";
import { areaChart, sparkline } from "./charts.js";
import { cardOpenKey, defaultCardOpen, modelCardId, rememberOpen, storedOpen } from "./collapse.js";
import {
  closedFillStats,
  cryptoOosModels,
  deriveSleeve,
  feeDragFromTrades,
  formatPct,
  formatUsd,
  formatWinPct,
  formatWinRecord,
  cardPositionRows,
  inferLiveBackend,
  labelFor,
  LIVE_SIGNAL_URL,
  mergeLiveBook,
  shownBooks,
  warehouseSleeveAsOf,
  SEEDS_USD,
  sleeveKey,
  tone,
  winTone,
} from "./derive.js";
import { posOpenKey } from "./positions.js";
import {
  TAPE_MONTH_ALL,
  csvFilename,
  etMonthLabel,
  fillCountText,
  fillStamp,
  filterTapeByMonth,
  linkSegments,
  preferredWhy,
  rememberTapeMonth,
  storedTapeMonth,
  tapeCsv,
  tapeMonthOptions,
  tapeMonthStorageKey,
  tapeOpenKey,
} from "./tape.js";

const statusEl = document.querySelector("#status");
const heroEl = document.querySelector("#hero");
const chipsEl = document.querySelector("#chips");
const tapesEl = document.querySelector("#tapes");
const positionsEl = document.querySelector("#positions");
const curvesEl = document.querySelector("#curves");
const curveChangeEl = document.querySelector("#curve-change");
const rangeToggleEl = document.querySelector("#range-toggle");
const railsEl = document.querySelector("#rails");
const railsSourceEl = document.querySelector("#rails-source");
const underwaterEl = document.querySelector("#underwater");
const dayBannerEl = document.querySelector("#day-banner");
const modelsEl = document.querySelector("#models");
const panelSleeves = document.querySelector("#panel-sleeves");
const panelModels = document.querySelector("#panel-models");
const tabSleeves = document.querySelector("#tab-sleeves");
const tabModels = document.querySelector("#tab-models");
const toastEl = document.querySelector("#toast");

const COLORS = { up: "#2fd17c", down: "#ff5d6c", flat: "#8b97a5", accent: "#f5b84b" };
const RANGE_KEY = "the-book-curve-range";
const FEED_PAGE = 25;

const state = {
  points: [],
  range: "all",
  dayFilter: null,
  tapes: new Map(),
};

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function tip(text) {
  const node = el("span", "tip", "?");
  node.tabIndex = 0;
  node.dataset.tip = text;
  node.setAttribute("aria-label", text);
  return node;
}

function toast(text) {
  toastEl.textContent = text;
  toastEl.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    toastEl.hidden = true;
  }, 1800);
}

function shareButton(text, url, label = "Share on X") {
  const link = el("a", "share-x");
  link.href = shareUrl(text, url);
  link.target = "_blank";
  link.rel = "noopener";
  link.setAttribute("aria-label", label);
  const glyph = el("span", "x-glyph", "\ud835\udd4f");
  glyph.setAttribute("aria-hidden", "true");
  link.append(glyph, document.createTextNode(label === "Share on X" ? "Share" : label));
  return link;
}

function copyLinkButton(url) {
  const button = el("button", "copy-link", "Copy link");
  button.type = "button";
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url);
      toast("Link copied");
    } catch {
      toast(url);
    }
  });
  return button;
}

function watermark(asOf) {
  const mark = el("p", "watermark");
  mark.append(el("span", "wm-brand", "jrg185.github.io/the-book"));
  if (asOf) mark.append(el("span", null, formatEt(asOf)));
  return mark;
}

function mountCollapse({ heading, label, panel, storageKey, defaultOpen, collapsedHost = null }) {
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "tape-toggle";
  toggle.setAttribute("aria-controls", panel.id);
  const open = storedOpen(localStorage, storageKey, defaultOpen);
  toggle.setAttribute("aria-expanded", open ? "true" : "false");
  toggle.append(document.createTextNode(label));
  const chevron = el("span", "chevron");
  chevron.setAttribute("aria-hidden", "true");
  toggle.append(chevron);
  heading.append(toggle);
  panel.hidden = !open;
  if (collapsedHost) collapsedHost.classList.toggle("is-collapsed", !open);
  toggle.addEventListener("click", () => {
    const next = toggle.getAttribute("aria-expanded") !== "true";
    toggle.setAttribute("aria-expanded", next ? "true" : "false");
    panel.hidden = !next;
    if (collapsedHost) collapsedHost.classList.toggle("is-collapsed", !next);
    rememberOpen(localStorage, storageKey, next);
  });
  return toggle;
}

function metric(label, value, valueClass) {
  const wrap = el("div", "metric");
  wrap.append(el("div", "k", label), el("div", `v ${valueClass || ""}`.trim(), value));
  return wrap;
}

function pair(primary, secondary) {
  const wrap = el("div", "pair");
  wrap.append(el("span", "primary", primary));
  if (secondary) wrap.append(el("span", "secondary", secondary));
  return wrap;
}

const STALE_AFTER_MS = 3 * 60 * 60 * 1000;
// Sleeve as_of is the warehouse MTM clock: live_book.sleeve_as_of, else kpi_summary.
// Signal generated_at is not this clock. A fresh export stamps sleeve_as_of from
// the kpi_sleeve_snapshots row that refresh just inserted.
const SNAPSHOT_STALE_MS = 60 * 60 * 1000;

function parseTime(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function formatEt(value) {
  const date = parseTime(value);
  if (!date) return value ? String(value) : "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function formatEtDay(key) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(key || ""));
  if (!match) return String(key || "");
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12));
  return new Intl.DateTimeFormat("en-US", { timeZone: "UTC", weekday: "short", month: "short", day: "numeric", year: "numeric" }).format(date);
}

function shortWhen(ms, long) {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    ...(long ? { hour: "numeric", minute: "2-digit" } : {}),
  }).format(new Date(ms));
}

async function loadJson(path, token) {
  const bust = encodeURIComponent(String(token || Date.now()));
  const response = await fetch(`${path}?t=${bust}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path} ${response.status}`);
  return response.json();
}

function agePhrase(ms) {
  const minutes = Math.max(0, Math.round(ms / 60000));
  if (minutes < 60) return `${minutes} minutes`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (!rest) return hours === 1 ? "1 hour" : `${hours} hours`;
  return `${hours}h ${rest}m`;
}

function statusChip(meta, sleeveAsOf) {
  const exportStatus = String(meta?.export_status || "").toLowerCase();
  const refreshedAt = parseTime(meta?.fetched_at);
  const snapshotAt = parseTime(sleeveAsOf);
  const agedOut = refreshedAt != null && Date.now() - refreshedAt.getTime() > STALE_AFTER_MS;
  const snapshotAge = snapshotAt == null ? null : Date.now() - snapshotAt.getTime();
  const warehouse = String(meta?.warehouse_status || "").toLowerCase();
  const mtmStale = meta?.source === "supabase" && snapshotAge != null && snapshotAge > SNAPSHOT_STALE_MS;
  if (warehouse === "read-only") return { cls: "error", text: "Warehouse read-only" };
  if (warehouse === "disk-full") return { cls: "error", text: "Warehouse disk full" };
  if (exportStatus === "error") return { cls: "error", text: "Export failed" };
  if (exportStatus === "stale" || agedOut) return { cls: "stale", text: "Stale snapshot" };
  if (mtmStale) return { cls: "stale", text: "MTM stale" };
  if (meta?.source === "supabase") return { cls: "live", text: "Live" };
  return { cls: "sample", text: "Sample snapshot" };
}

function renderStatus(meta, sleeveAsOf) {
  statusEl.replaceChildren();
  const exportStatus = String(meta?.export_status || "").toLowerCase();
  const refreshedAt = parseTime(meta?.fetched_at);
  const snapshotAt = parseTime(sleeveAsOf);
  const agedOut = refreshedAt != null && Date.now() - refreshedAt.getTime() > STALE_AFTER_MS;
  const snapshotAge = snapshotAt == null ? null : Date.now() - snapshotAt.getTime();
  const warehouse = String(meta?.warehouse_status || "").toLowerCase();
  const mtmStale = meta?.source === "supabase" && snapshotAge != null && snapshotAge > SNAPSHOT_STALE_MS;
  const exportBroken = exportStatus === "error" || exportStatus === "stale" || agedOut;
  const frozenLabel = formatEt(meta?.snapshot_as_of || sleeveAsOf);
  const chip = statusChip(meta, sleeveAsOf);
  statusEl.className = `status is-${chip.cls}`;

  const lines = el("div", "status-lines");
  const refreshed = formatEt(meta?.fetched_at);
  lines.append(
    el("p", "status-copy", refreshed ? `Last refreshed ${refreshed}` : "Last refreshed time is missing from this snapshot.")
  );
  const asOf = formatEt(sleeveAsOf);
  if (asOf) lines.append(el("p", "status-copy", `Sleeve as of ${asOf}`));
  if (warehouse === "read-only") {
    lines.append(
      el("p", "status-copy status-error", frozenLabel ? `warehouse read-only — snapshot frozen at ${frozenLabel}` : "warehouse read-only — snapshot frozen")
    );
  } else if (warehouse === "disk-full") {
    lines.append(
      el("p", "status-copy status-error", frozenLabel ? `warehouse disk full — snapshot frozen at ${frozenLabel}` : "warehouse disk full — snapshot frozen")
    );
  } else if (meta?.export_error) {
    lines.append(el("p", "status-copy status-error", String(meta.export_error)));
  } else if (exportBroken) {
    lines.append(el("p", "status-copy status-error", "This JSON is not a fresh warehouse export."));
  } else if (mtmStale) {
    lines.append(
      el(
        "p",
        "status-copy status-warn",
        `Warehouse MTM is ${agePhrase(snapshotAge)} old. The sleeve clock is kpi_sleeve_snapshots as_of. Export KPI inserts a fresh snapshot before it re-reads kpi_summary.`
      )
    );
  } else if (meta?.source === "supabase" && !asOf) {
    lines.append(el("p", "status-copy status-warn", "Sleeve as_of is missing, so this page cannot tell whether warehouse MTM moved."));
  } else if (!refreshed && meta?.note) {
    lines.append(el("p", "status-copy", String(meta.note)));
  }
  const attempted = formatEt(meta?.export_attempted_at);
  if (attempted && attempted !== refreshed && (exportStatus === "error" || exportStatus === "stale")) {
    lines.append(el("p", "status-copy", `Last export attempt ${attempted}`));
  }
  statusEl.append(el("span", `chip ${chip.cls}`, chip.text), lines);
}

function freshness(asOf) {
  const node = el("span", "fresh");
  node.dataset.asof = asOf || "";
  const dot = el("i", "pulse");
  dot.setAttribute("aria-hidden", "true");
  node.append(dot, el("span", "fresh-text", ""));
  paintFreshness(node);
  return node;
}

function paintFreshness(node) {
  const date = parseTime(node.dataset.asof);
  const text = node.querySelector(".fresh-text");
  if (!text) return;
  text.textContent = date ? `Updated ${agoText(date.getTime())}` : "Update time unknown";
  node.title = date ? formatEt(node.dataset.asof) : "";
  node.classList.toggle("old", !date || Date.now() - date.getTime() > SNAPSHOT_STALE_MS);
}

function pnlChip(label, dollars, frac, extra) {
  const chip = el("div", `pnl-chip ${tone(dollars)}`);
  chip.append(el("span", "pc-label", label));
  const value = el("span", "pc-value", formatUsd(dollars, { signed: true }));
  chip.append(value);
  if (frac != null) chip.append(el("span", "pc-frac", formatPct(frac, { signed: true, digits: 2 })));
  if (extra) chip.append(tip(extra));
  return chip;
}

function renderHero(view, meta, chip) {
  heroEl.replaceChildren();
  if (!view) {
    heroEl.append(el("p", "empty", "Live book has no book_usd."));
    return;
  }
  const seedFrac = seedReturn(view.runningPnl);
  const top = el("div", "hero-top");
  const label = el("p", "eyebrow");
  label.append(document.createTextNode("Account value"), tip("RH cash plus open lots. One crypto book. Equities are not a second book."));
  const status = el("span", `chip ${chip.cls}`, chip.text);
  top.append(label, status, freshness(view.asOf || meta?.fetched_at));

  const body = el("div", "hero-body");
  const left = el("div", "hero-left");
  left.append(el("p", `hero-value ${view.runningBalance == null ? "flat" : ""}`.trim(), formatUsd(view.runningBalance)));
  const chips = el("div", "hero-chips");
  chips.append(
    pnlChip("All-time", view.runningPnl, seedFrac, `Running P&L: RH book versus the $${SEEDS_USD.combined} seed.`),
    pnlChip("Today", view.dayPnl, null, "Day P&L, realized, from the Robinhood cash drop.")
  );
  left.append(chips);

  const split = el("dl", "hero-split");
  for (const [name, dollars, hint] of [
    ["Realized P&L", view.realizedPnl, "Running minus unrealized. Includes buy fees."],
    ["Unrealized P&L", view.unrealizedPnl, "Open lots, mark versus cost."],
    ["Running P&L", view.runningPnl, "Cash plus open lots minus the combined seed."],
  ]) {
    const dt = el("dt", null, name);
    dt.title = hint;
    split.append(dt, el("dd", tone(dollars), formatUsd(dollars, { signed: true })));
  }
  left.append(split);

  const right = el("div", "hero-right");
  const all = state.points;
  const sleeve = all.length ? all[all.length - 1].frac - 1 : null;
  right.append(sparkline(all, (p) => p.frac, COLORS[tone(sleeve)] || COLORS.flat, { width: 320, height: 96 }));
  right.append(el("p", "fine", sleeve == null ? "No sleeve history yet." : `Crypto sleeve ${formatPct(sleeve, { signed: true, digits: 2 })} vs seed`));

  body.append(left, right);
  const actions = el("div", "card-actions");
  const shareText = [
    `The Book: ${formatUsd(view.runningBalance)} account value`,
    view.runningPnl == null ? null : `${formatUsd(view.runningPnl, { signed: true })} (${formatPct(seedFrac, { signed: true, digits: 2 })}) all-time`,
    view.dayPnl == null ? null : `${formatUsd(view.dayPnl, { signed: true })} today`,
  ]
    .filter(Boolean)
    .join(" · ");
  actions.append(shareButton(shareText, SITE_URL), copyLinkButton(SITE_URL));
  heroEl.append(top, body, actions, watermark(view.asOf));
}

function statChip(label, value, sub, valueTone, hint) {
  const chip = el("div", "stat");
  const k = el("div", "stat-k", label);
  if (hint) k.append(tip(hint));
  chip.append(k, el("div", `stat-v ${valueTone || ""}`.trim(), value));
  if (sub) chip.append(el("div", "stat-sub", sub));
  return chip;
}

function feeView(scorecard, trades) {
  const fromTape = feeDragFromTrades(trades, "crypto");
  const published = scorecard?.fee_drag;
  const drag = published?.status === "known" ? published : fromTape;
  if (!drag || drag.status !== "known" || (drag.fee_usd == null && drag.sell_fee_usd == null)) {
    return {
      value: "UNKNOWN",
      toneClass: "flat",
      frac: null,
      note: published?.note || "No fee column on the scrubbed tape. Not estimated.",
    };
  }
  const total = drag.fee_usd != null ? drag.fee_usd : drag.sell_fee_usd;
  let note = drag.note || "Crypto fee dollars from the rows that were read.";
  if (drag.sell_fee_usd != null && drag.fee_usd != null && drag.sell_fee_usd !== drag.fee_usd) {
    note = `${note} Closed-sell fees ${formatUsd(drag.sell_fee_usd)}.`;
  }
  return {
    value: formatUsd(total),
    toneClass: total > 0 ? "down" : "flat",
    frac: drag.fee_frac ?? null,
    note,
  };
}

function renderChips(trades, scorecard) {
  chipsEl.replaceChildren();
  const fills = closedFillStats(trades, "crypto");
  const dd = drawdownStats(state.points);
  const sharpe = dailySharpe(state.points);
  const fee = feeView(scorecard, trades);
  const count = trades.filter((row) => sleeveKey(row) === "crypto").length;
  chipsEl.append(
    statChip("Win rate", formatWinPct(fills.rate), `${formatWinRecord(fills)} closed sells`, winTone(fills), "Crypto sells with a finite P&L. Flat exits are excluded."),
    statChip(
      "Expectancy",
      fills.expectancyFrac == null ? "\u2014" : formatPct(fills.expectancyFrac, { signed: true, digits: 2 }),
      fills.expectancyUsd == null ? "per closed sell" : `${formatUsd(fills.expectancyUsd, { signed: true })} per closed sell`,
      tone(fills.expectancyFrac),
      "Mean P&L of decided crypto sells, as a fraction of the sleeve seed."
    ),
    statChip("Max drawdown", formatPct(dd.max, { digits: 1 }), dd.current == null ? "" : `now ${formatPct(dd.current, { digits: 1 })}`, dd.max ? "down" : "flat", "Worst fall from a running peak on the crypto sleeve curve."),
    statChip(
      "Sharpe",
      sharpe.value == null ? "\u2014" : sharpe.value.toFixed(2),
      sharpe.value == null ? `needs 5 daily returns (${sharpe.days})` : `${sharpe.days} daily returns`,
      tone(sharpe.value),
      "Daily returns of the crypto sleeve curve, annualized by \u221a365. Short samples are noisy."
    ),
    statChip("Fee drag", fee.value, fee.frac == null ? "" : `${formatPct(fee.frac, { digits: 1 })} of sleeve seed`, fee.toneClass, fee.note),
    statChip("Fills", String(count), "crypto tape", "flat", "Rows on the scrubbed crypto tape, buys and sells.")
  );
}

function renderRange() {
  rangeToggleEl.replaceChildren();
  for (const range of RANGES) {
    const button = el("button", null, range.label);
    button.type = "button";
    button.dataset.range = range.key;
    button.setAttribute("aria-pressed", range.key === state.range ? "true" : "false");
    button.addEventListener("click", () => {
      state.range = range.key;
      try {
        localStorage.setItem(RANGE_KEY, range.key);
      } catch {
        /* private mode */
      }
      renderRange();
      paintCurves();
    });
    rangeToggleEl.append(button);
  }
}

function paintCurves() {
  if (!curvesEl) return;
  if (!state.points.length) {
    curvesEl.replaceChildren(el("p", "empty", "Sleeve history is not in this snapshot yet."));
    curveChangeEl.textContent = "";
    return;
  }
  const points = windowPoints(state.points, state.range);
  const change = windowChange(points);
  curveChangeEl.replaceChildren();
  const label = RANGES.find((range) => range.key === state.range)?.label || "";
  if (change != null) {
    curveChangeEl.append(el("span", `delta ${tone(change)}`, formatPct(change, { signed: true, digits: 2 })), document.createTextNode(` ${label}`));
  }
  areaChart(curvesEl, points, {
    height: 240,
    value: (p) => p.frac - 1,
    baseline: 0,
    color: COLORS[tone(change)] || COLORS.flat,
    format: (v) => formatPct(v, { signed: true, digits: 1 }),
    when: (ms, long) => shortWhen(ms, long || state.range === "24h"),
    label: `Crypto sleeve return vs seed, ${label}`,
  });
}

function railBar({ title, value, detail, fill, toneClass, hint }) {
  const row = el("div", "rail");
  const head = el("div", "rail-head");
  const name = el("span", "rail-name", title);
  if (hint) name.append(tip(hint));
  head.append(name, el("span", `rail-value ${toneClass || ""}`.trim(), value));
  const track = el("div", "rail-track");
  track.setAttribute("role", "meter");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.setAttribute("aria-label", title);
  if (fill != null) {
    track.setAttribute("aria-valuenow", String(Math.round(fill * 100)));
    const bar = el("span", toneClass || "");
    bar.style.width = `${Math.max(0, Math.min(1, fill)) * 100}%`;
    track.append(bar);
  } else {
    track.classList.add("unknown");
  }
  row.append(head, track, el("p", "rail-detail", detail));
  return row;
}

function renderRisk(view, summary, scorecard) {
  railsEl.replaceChildren();
  const rails = liveRails(summary, scorecard);
  const r = railView(view?.runningBalance, view?.dayPnl, rails);
  railsSourceEl.textContent = rails.source
    ? `Day rails from ${rails.source}${rails.asOf ? `, as of ${formatEt(rails.asOf)}` : ""}.`
    : "No day rail is published in this snapshot.";

  if (r.killFrac == null) {
    railsEl.append(railBar({ title: "Day kill", value: "Not published", detail: "The warehouse export has no crypto day_kill_pct.", fill: null }));
  } else {
    const left = r.killUsedFrac == null ? null : 1 - r.killUsedFrac;
    railsEl.append(
      railBar({
        title: "Day kill headroom",
        value: formatUsd(r.headroomUsd),
        detail: `${formatPct(r.killFrac)} rail = ${formatUsd(r.killUsd)} on ${formatUsd(r.balance)}. Today ${formatUsd(r.dayPnl, { signed: true })}.`,
        fill: left,
        toneClass: left == null ? "flat" : left < 0.25 ? "down" : left < 0.6 ? "warn" : "up",
        hint: "Budget left before the day kill. A losing day spends it. A winning day does not add to it.",
      })
    );
  }
  if (r.targetFrac == null) {
    railsEl.append(railBar({ title: "Day target", value: "Not published", detail: "The warehouse export has no crypto day_target_pct.", fill: null }));
  } else {
    railsEl.append(
      railBar({
        title: "Day target",
        value: `${formatUsd(Math.max(0, r.dayPnl ?? 0))} / ${formatUsd(r.targetUsd, { signed: true })}`,
        detail: `${formatPct(r.targetFrac, { signed: true })} of the running balance, realized only.`,
        fill: r.targetProgress,
        toneClass: "accent",
        hint: "Progress of today's realized P&L toward the day target.",
      })
    );
  }
  const dd = drawdownStats(state.points);
  railsEl.append(
    railBar({
      title: "Below peak",
      value: formatPct(dd.current, { digits: 1 }),
      detail: dd.max == null ? "No sleeve history yet." : `Worst so far ${formatPct(dd.max, { digits: 1 })}${dd.maxAt ? ` on ${shortWhen(Date.parse(dd.maxAt))}` : ""}.`,
      fill: dd.current == null || !dd.max ? 0 : Math.min(1, dd.current / dd.max),
      toneClass: "down",
      hint: "Current drawdown of the crypto sleeve as a share of its worst drawdown.",
    })
  );

  paintUnderwater();
}

function paintUnderwater() {
  areaChart(underwaterEl, drawdownSeries(state.points), {
    height: 140,
    value: (p) => p.dd,
    baseline: 0,
    color: COLORS.down,
    invert: true,
    format: (v) => formatPct(v, { digits: 1 }),
    when: (ms, long) => shortWhen(ms, long),
    label: "Crypto sleeve drawdown from running peak",
    emptyText: "No sleeve history yet.",
  });
}

function renderPositions(book, asOf) {
  if (!positionsEl) return;
  positionsEl.replaceChildren();
  const rows = cardPositionRows(book);
  const section = el("section", "pos");
  const heading = el("h3", "sr-only-heading");
  const panel = el("div", "pos-panel");
  panel.id = "pos-panel-crypto";
  mountCollapse({
    heading,
    label: "Crypto",
    panel,
    storageKey: posOpenKey("crypto"),
    defaultOpen: true,
  });
  section.append(heading);
  if (!rows.length) {
    panel.append(el("p", "empty", "The live book has no holdings."));
    section.append(panel);
    positionsEl.append(section);
    return;
  }
  const total = rows.reduce((sum, row) => sum + (row.valueUsd || 0), 0);
  const wrap = el("div", "table-wrap");
  const table = el("table", "cards-on-mobile");
  const thead = el("thead");
  const headRow = el("tr");
  const labels = ["Ticker", "Qty", "Value", "Running P&L"];
  for (const label of ["Ticker", "Qty", "Value", "Running P&L"]) {
    const th = el("th", label === "Ticker" ? "" : "num", label);
    th.scope = "col";
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = el("tbody");
  for (const row of rows) {
    const tr = el("tr", row.qty == null ? "cash-row" : "");
    const ticker = el("td", "ticker");
    ticker.dataset.label = labels[0];
    ticker.append(el("span", "tk", row.ticker));
    if (row.valueUsd != null && total > 0) {
      const weight = el("span", "weight");
      const bar = el("i");
      bar.style.width = `${(row.valueUsd / total) * 100}%`;
      weight.append(bar);
      weight.title = `${formatPct(row.valueUsd / total)} of book`;
      ticker.append(weight);
    }
    const cells = [
      ticker,
      el("td", "num", row.qty == null ? "cash" : String(row.qty)),
      el("td", "num", formatUsd(row.valueUsd)),
      el("td", `num ${row.runningPnl == null ? "" : tone(row.runningPnl)}`.trim(), row.runningPnl == null ? "\u2014" : formatUsd(row.runningPnl, { signed: true })),
    ];
    cells.forEach((cell, i) => {
      cell.dataset.label = labels[i];
    });
    tr.append(...cells);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  panel.append(wrap, watermark(asOf));
  section.append(panel);
  positionsEl.append(section);
}

function readFrac(row, key) {
  if (!row || row[key] == null || row[key] === "") return null;
  const n = Number(row[key]);
  return Number.isFinite(n) ? n : null;
}

function seedTimes(seed, frac) {
  if (seed == null || frac == null) return null;
  return Math.round((seed * frac + Number.EPSILON) * 100) / 100;
}

function notionalText(seed, frac) {
  if (frac == null) return "\u2014";
  const dollars = seedTimes(seed, frac);
  const pct = formatPct(frac, { digits: 2 });
  return dollars == null ? pct : `${formatUsd(dollars)} (${pct})`;
}

function feeText(seed, frac) {
  if (frac == null) return "\u2014";
  const dollars = seedTimes(seed, frac);
  return dollars == null ? "\u2014" : formatUsd(dollars);
}

function tradeStamp(trade) {
  return fillStamp(trade);
}

function fillLinked(parent, text, sleeve) {
  for (const part of linkSegments(text, sleeve)) {
    if (part.type === "link") {
      const anchor = document.createElement("a");
      anchor.href = part.href;
      anchor.target = "_blank";
      anchor.rel = "noopener";
      anchor.textContent = part.text;
      parent.append(anchor);
    } else if (part.text) {
      parent.append(document.createTextNode(part.text));
    }
  }
}

// Fee is fee_frac_of_book times the sleeve seed. Machine why strings show
// only their label. The broker id inside them is never rendered.
function tapeFields(sleeve, trade) {
  const shaped = deriveSleeve({ ...trade, sleeve });
  const seed = shaped.seed || deriveSleeve({ sleeve }).seed;
  const pnlFrac = readFrac(trade, "pnl_frac_of_book") ?? readFrac(trade, "pnl_frac");
  const pnl = seedTimes(seed, pnlFrac);
  const why = preferredWhy(trade);
  return {
    time: formatEt(tradeStamp(trade)) || "\u2014",
    ticker: trade.ticker || "\u2014",
    side: trade.side || "\u2014",
    notional: notionalText(seed, readFrac(trade, "notional_frac_of_book")),
    fee: feeText(seed, readFrac(trade, "fee_frac_of_book")),
    tradePnl: formatUsd(pnl, { signed: true }),
    runningPnl: formatUsd(shaped.runningPnl, { signed: true }),
    runningBalance: formatUsd(shaped.runningBalance),
    pnl,
    pnlFrac,
    runningPnlValue: shaped.runningPnl,
    runningBalanceValue: shaped.runningBalance,
    why: why.kind === "human" ? redactIds(why.text) : why.text,
    whyKind: why.kind,
  };
}

function tradeShareText(fields) {
  const side = String(fields.side).toLowerCase();
  const head = `${side === "sell" ? "Sold" : side === "buy" ? "Bought" : fields.side} ${fields.ticker} on The Book`;
  const bits = [head];
  if (side === "sell" && fields.pnl != null) {
    bits.push(`${fields.tradePnl} (${formatPct(fields.pnlFrac, { signed: true, digits: 2 })} of book)`);
  } else if (fields.notional !== "\u2014") {
    bits.push(fields.notional);
  }
  bits.push(fields.time);
  return bits.join(" · ");
}

function renderWhyCell(fields, sleeve, id) {
  const cell = el("td", "why");
  cell.dataset.label = "Why";
  const body = el("span", "why-text");
  if (fields.whyKind === "machine") {
    body.append(el("span", "why-label", fields.why));
  } else if (fields.why) {
    fillLinked(body, fields.why, sleeve);
    cell.title = fields.why;
  } else {
    body.append(el("span", "why-label", "\u2014"));
  }
  cell.append(body);
  if (id) {
    const actions = el("span", "row-actions");
    const link = el("a", "permalink", "#");
    link.href = `#${id}`;
    link.setAttribute("aria-label", `Permalink to ${fields.ticker} ${fields.side} at ${fields.time}`);
    actions.append(link, shareButton(tradeShareText(fields), permalink(id), "Share on X"));
    cell.append(actions);
  }
  return cell;
}

function downloadCsv(filename, text) {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.rel = "noopener";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function appendTapeRow(tbody, sleeve, trade, id) {
  const fields = tapeFields(sleeve, trade);
  const tr = el("tr");
  if (id) tr.id = id;
  const time = el("td", "time");
  const dayKey = etDayKey(tradeStamp(trade));
  if (dayKey) {
    const dayLink = el("a", "day-link", fields.time);
    dayLink.href = `#day-${dayKey}`;
    dayLink.title = `All fills on ${formatEtDay(dayKey)}`;
    time.append(dayLink);
  } else {
    time.textContent = fields.time;
  }
  const side = el("td", "side");
  side.append(el("span", `pill ${trade.side || ""}`, fields.side));
  const cells = [
    time,
    el("td", "ticker", fields.ticker),
    side,
    el("td", "num", fields.notional),
    el("td", "num", fields.fee),
    el("td", `num ${tone(fields.pnl)}`, fields.tradePnl),
    el("td", `num ${tone(fields.runningPnlValue)}`, fields.runningPnl),
    el("td", `num ${tone(fields.runningBalanceValue)}`, fields.runningBalance),
    renderWhyCell(fields, sleeve, id),
  ];
  const labels = ["Time", "Ticker", "Side", "Notional", "Fee", "Trade P&L", "Running P&L", "Running balance", "Why"];
  cells.forEach((cell, i) => {
    cell.dataset.label = labels[i];
  });
  tr.append(...cells);
  tbody.append(tr);
}

function renderTape(sleeve, trades, anchors) {
  const section = el("section", `tape ${sleeve}`);
  const head = el("div", "tape-head");
  const heading = el("h3");
  const panel = el("div", "tape-panel");
  panel.id = `tape-panel-${sleeve}`;
  mountCollapse({
    heading,
    label: labelFor(sleeve),
    panel,
    storageKey: tapeOpenKey(sleeve),
    defaultOpen: sleeve !== "equities",
  });

  const months = tapeMonthOptions(trades, tradeStamp);
  const monthKey = tapeMonthStorageKey(sleeve);
  let month = storedTapeMonth(localStorage, monthKey, months);
  let showAll = false;
  const tools = el("div", "tape-tools");
  let select = null;

  if (months.length) {
    const monthLabel = el("label", "tape-month");
    monthLabel.htmlFor = `tape-month-${sleeve}`;
    monthLabel.append(document.createTextNode("Month"));
    select = document.createElement("select");
    select.id = `tape-month-${sleeve}`;
    const allOption = document.createElement("option");
    allOption.value = TAPE_MONTH_ALL;
    allOption.textContent = "All";
    select.append(allOption);
    for (const key of months) {
      const option = document.createElement("option");
      option.value = key;
      option.textContent = etMonthLabel(key);
      select.append(option);
    }
    select.value = month;
    select.addEventListener("change", () => {
      month = select.value;
      rememberTapeMonth(localStorage, monthKey, month);
      paintRows();
    });
    monthLabel.append(select);
    tools.append(monthLabel);
  }

  const count = el("p", "fine tape-count");
  count.id = `tape-count-${sleeve}`;
  if (trades.length) {
    count.setAttribute("aria-live", "polite");
    tools.append(count);
  }

  const exportBtn = document.createElement("button");
  exportBtn.type = "button";
  exportBtn.className = "tape-export";
  exportBtn.textContent = "CSV";
  exportBtn.setAttribute("aria-label", `Download ${labelFor(sleeve)} CSV`);
  exportBtn.addEventListener("click", () => {
    const rows = filterTapeByMonth(trades, month, tradeStamp).map((trade) => tapeFields(sleeve, trade));
    downloadCsv(csvFilename(sleeve), tapeCsv(rows));
  });
  tools.append(exportBtn);
  head.append(heading, tools);
  section.append(head);
  if (sleeve === "equities") {
    section.append(el("p", "note", "Historical fills. Equities are not an open book."));
  }

  if (!trades.length) {
    panel.append(el("p", "empty", "No scrubbed fills."));
    section.append(panel);
    return { section, reveal: () => false, paint: () => {} };
  }

  const wrap = el("div", "table-wrap");
  const table = el("table", "cards-on-mobile");
  const captionText = `${labelFor(sleeve)} fills. Fee, trade P&L, running P&L, and running balance recover scrubbed fractions. They are not the live account book.`;
  const caption = el("caption", "sr-only", `${labelFor(sleeve)} fills`);
  const note = el("p", "tape-note", captionText);
  note.id = `tape-note-${sleeve}`;
  table.setAttribute("aria-describedby", note.id);
  const thead = el("thead");
  const headRow = el("tr");
  for (const label of ["Time", "Ticker", "Side", "Notional", "Fee", "Trade P&L", "Running P&L", "Running balance", "Why"]) {
    const th = el("th", null, label);
    th.scope = "col";
    headRow.append(th);
  }
  thead.append(headRow);
  const tbody = el("tbody");
  const more = el("button", "show-more");
  more.type = "button";
  more.addEventListener("click", () => {
    showAll = true;
    paintRows();
  });

  function visibleRows() {
    if (state.dayFilter) return trades.filter((trade) => etDayKey(tradeStamp(trade)) === state.dayFilter);
    return filterTapeByMonth(trades, month, tradeStamp);
  }

  function paintRows() {
    const visible = visibleRows();
    count.textContent = fillCountText(visible.length, trades.length);
    tbody.replaceChildren();
    if (!visible.length) {
      const tr = el("tr", "empty-row");
      const td = el("td", null, state.dayFilter ? "No fills on this day." : "No fills in this month.");
      td.colSpan = 9;
      tr.append(td);
      tbody.append(tr);
      more.hidden = true;
      return;
    }
    const shown = showAll || state.dayFilter ? visible : visible.slice(0, FEED_PAGE);
    for (const trade of shown) appendTapeRow(tbody, sleeve, trade, anchors.get(trade));
    more.hidden = shown.length === visible.length;
    more.textContent = `Show all ${visible.length} fills`;
  }

  function reveal(id) {
    const trade = trades.find((row) => anchors.get(row) === id);
    if (!trade) return false;
    month = TAPE_MONTH_ALL;
    if (select) select.value = month;
    showAll = true;
    if (panel.hidden) heading.querySelector("button")?.click();
    paintRows();
    return true;
  }

  paintRows();
  table.append(caption, thead, tbody);
  wrap.append(table);
  panel.append(wrap, more, note);
  section.append(panel);
  return { section, reveal, paint: paintRows };
}

function render(tradeRows, meta, book, summary) {
  const trades = Array.isArray(tradeRows) ? tradeRows : [];
  const books = shownBooks(book, summary);
  const view = books[0] || null;
  const sleeveAsOf = warehouseSleeveAsOf(book, summary);
  renderStatus(meta || {}, sleeveAsOf);
  renderHero(view, meta, statusChip(meta || {}, sleeveAsOf));

  const grouped = new Map();
  for (const trade of trades) {
    const key = sleeveKey(trade);
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(trade);
  }
  tapesEl.replaceChildren();
  state.tapes.clear();
  const anchors = tradeAnchors(
    trades.slice().sort((a, b) => String(tradeStamp(a)).localeCompare(String(tradeStamp(b)))),
    tradeStamp
  );
  for (const key of ["crypto", "equities"]) {
    const rows = (grouped.get(key) || []).slice().sort((a, b) => {
      const da = parseTime(tradeStamp(a));
      const db = parseTime(tradeStamp(b));
      const am = da ? da.getTime() : Number.NEGATIVE_INFINITY;
      const bm = db ? db.getTime() : Number.NEGATIVE_INFINITY;
      if (am !== bm) return bm - am;
      return String(b.timestamp_et || "").localeCompare(String(a.timestamp_et || ""));
    });
    const tape = renderTape(key, rows, anchors);
    state.tapes.set(key, tape);
    tapesEl.append(tape.section);
  }
  return view;
}

function paintDayBanner(trades) {
  if (!state.dayFilter) {
    dayBannerEl.hidden = true;
    dayBannerEl.replaceChildren();
    return;
  }
  const day = state.dayFilter;
  const rows = trades.filter((trade) => sleeveKey(trade) === "crypto" && etDayKey(tradeStamp(trade)) === day);
  let pnl = 0;
  let closed = 0;
  for (const trade of rows) {
    if (String(trade.side).toLowerCase() !== "sell") continue;
    const frac = readFrac(trade, "pnl_frac_of_book") ?? readFrac(trade, "pnl_frac");
    if (frac == null) continue;
    pnl += seedTimes(SEEDS_USD.crypto, frac) ?? 0;
    closed += 1;
  }
  pnl = Math.round(pnl * 100) / 100;
  dayBannerEl.hidden = false;
  dayBannerEl.replaceChildren();
  const text = el("div", "day-text");
  text.append(el("strong", null, formatEtDay(day)), document.createTextNode(` · ${rows.length} crypto fills · ${closed} closed · `));
  text.append(el("span", tone(pnl), `${formatUsd(pnl, { signed: true })} trade P&L`));
  const actions = el("div", "card-actions");
  const url = permalink(`day-${day}`);
  actions.append(
    shareButton(`The Book on ${formatEtDay(day)}: ${rows.length} crypto fills, ${formatUsd(pnl, { signed: true })} closed trade P&L`, url),
    copyLinkButton(url)
  );
  const clear = el("a", "clear-day", "Clear");
  clear.href = "#";
  clear.addEventListener("click", (event) => {
    event.preventDefault();
    history.pushState(null, "", `${location.pathname}${location.search}`);
    applyHash();
  });
  actions.append(clear);
  dayBannerEl.append(text, actions);
}

function showTab(name) {
  const models = name === "models";
  panelSleeves.hidden = models;
  panelModels.hidden = !models;
  tabSleeves.setAttribute("aria-selected", models ? "false" : "true");
  tabModels.setAttribute("aria-selected", models ? "true" : "false");
}

function oosText(oos) {
  if (!oos || typeof oos !== "object") return "Pending T04.";
  const pending = oos.status === "placeholder" || (oos.hit_rate == null && oos.avg_return == null && oos.n == null);
  if (pending) return oos.note || "Pending T04.";
  const bits = [];
  if (oos.window) bits.push(String(oos.window));
  if (oos.hit_rate != null) bits.push(`hit ${oos.hit_rate}`);
  if (oos.avg_return != null) bits.push(`avg ${oos.avg_return}`);
  if (oos.n != null) bits.push(`n=${oos.n}`);
  return bits.join(" \u00b7 ") || "Pending T04.";
}

function oosRows(payload) {
  if (Array.isArray(payload)) return payload.filter((row) => row && typeof row === "object");
  if (payload && Array.isArray(payload.rows)) return payload.rows.filter((row) => row && typeof row === "object");
  return [];
}

function sameSleeve(row, sleeve) {
  const value = String(row.sleeve || row.book || row.asset_class || "").trim().toLowerCase();
  if (sleeve === "equities") return value === "equities" || value === "equity";
  return value === sleeve;
}

function formatMetricRow(row) {
  const hasFit = row.auc != null || row.brier != null || row.n_long != null || row.after_cost_mean != null;
  if (!hasFit) return summarizeOosRow(row);
  const bits = [String(row.model || row.name || "model")];
  if (row.auc != null) bits.push(`AUC ${row.auc}`);
  if (row.brier != null) bits.push(`Brier ${row.brier}`);
  if (row.n_long != null) bits.push(`n_long ${row.n_long}`);
  if (row.after_cost_mean != null) bits.push(`after cost ${row.after_cost_mean}`);
  if (row.sleeve_ir_vs_spy != null) bits.push(`IR vs SPY ${row.sleeve_ir_vs_spy}`);
  if (row.sleeve_sharpe != null) bits.push(`Sharpe ${row.sleeve_sharpe}`);
  if (row.n_cohorts != null) bits.push(`${row.n_cohorts} cohorts`);
  if (row.promoted === false) bits.push("not promoted");
  if (row.promoted === true) bits.push("promoted");
  if (row.note) bits.push(String(row.note));
  return bits.join(" \u00b7 ");
}

function summarizeOosRow(row) {
  if (row.hit_rate != null || row.avg_return != null || row.n != null || row.window) return oosText(row);
  const skip = new Set(["sleeve", "book", "name"]);
  const bits = [];
  for (const [key, value] of Object.entries(row)) {
    if (skip.has(key) || value == null || value === "") continue;
    bits.push(`${key} ${value}`);
    if (bits.length >= 4) break;
  }
  return bits.join(" \u00b7 ") || "Pending T04.";
}

function fixedDigits(value, digits) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "\u2014";
  return n.toFixed(digits);
}

function backendView(scorecard, models, oosPayload) {
  const published = scorecard?.live_backend;
  if (published && (published.id || published.note)) return published;
  const inferred = inferLiveBackend(models);
  const promoted = cryptoOosModels(oosPayload).find((row) => row.promoted === true);
  if (inferred.id === "rules" && promoted?.model) {
    return {
      ...inferred,
      promoted_model: promoted.model,
      promoted_in_use: false,
      note: `The CLI is still --backend rules. ${promoted.model} is promoted on after-cost mean and is not the live backend.`,
    };
  }
  return inferred;
}

function renderLatestCall(trades, backend, runs) {
  const card = el("article", "card shot latest-call");
  const head = el("header", "card-head");
  const titleWrap = el("div");
  titleWrap.append(el("h2", null, "Latest call"), el("p", "sub", "What the desk did last, and why."));
  head.append(titleWrap, el("span", "chip live", backend.id ? `live: ${backend.id}` : "live backend UNKNOWN"));
  card.append(head);

  const latestRun = runs[0] || null;
  if (latestRun) {
    const run = el("div", "call");
    const meta = el("p", "call-meta");
    meta.append(
      el("strong", null, latestRun.model || "Run"),
      document.createTextNode(latestRun.startedAt ? ` · ${formatEt(latestRun.startedAt)}` : ""),
      document.createTextNode(latestRun.status ? ` · ${latestRun.status}` : "")
    );
    run.append(meta);
    if (latestRun.decision) run.append(el("p", "call-decision", latestRun.decision));
    if (latestRun.summary) run.append(el("blockquote", "call-why", latestRun.summary));
    const link = el("a", "more-link", "All runs \u2192");
    link.href = "runs.html";
    run.append(link);
    card.append(run);
  }

  const call = latestCall(trades, preferredWhy, tradeStamp);
  if (call) {
    const fields = tapeFields("crypto", call.trade);
    const fill = el("div", "call");
    const meta = el("p", "call-meta");
    meta.append(el("span", `pill ${call.trade.side || ""}`, fields.side), el("strong", null, ` ${fields.ticker}`), document.createTextNode(` · ${fields.time}`));
    fill.append(meta);
    const quote = el("blockquote", "call-why");
    fillLinked(quote, call.why, "crypto");
    fill.append(quote);
    card.append(fill);
  } else if (!latestRun) {
    card.append(el("p", "empty", "No fill with a written reason in this snapshot."));
  }
  card.append(el("p", "fine", backend.note || "Live backend is not stated in this snapshot."), watermark(null));
  return card;
}

function renderModelRace(oosPayload, backend) {
  const rows = rankModels(cryptoOosModels(oosPayload));
  const card = el("article", "card shot model-race");
  const head = el("header", "card-head");
  const titleWrap = el("div");
  titleWrap.append(el("h2", null, "Model leaderboard"), el("p", "sub", "Crypto out-of-sample, ranked by after-cost mean per trade."));
  head.append(titleWrap);
  card.append(head);
  if (!rows.length) {
    card.append(el("p", "empty", "No crypto out-of-sample rows in this snapshot."));
    return card;
  }
  const max = Math.max(...rows.map((row) => Math.abs(row.afterCost ?? 0)), 1e-9);
  const list = el("ol", "race");
  rows.forEach((row, i) => {
    const item = el("li", "race-row");
    const name = el("div", "race-name");
    name.append(el("span", "rank", String(i + 1)), el("strong", null, row.model));
    if (row.promoted) name.append(el("span", "badge", "promoted"));
    if (backend.id && row.model === backend.id) name.append(el("span", "badge live", "live"));
    const bar = el("div", "race-bar");
    const fill = el("span", tone(row.afterCost));
    fill.style.width = `${(Math.abs(row.afterCost ?? 0) / max) * 50}%`;
    if ((row.afterCost ?? 0) < 0) fill.style.right = "50%";
    else fill.style.left = "50%";
    bar.append(fill);
    const nums = el("div", "race-nums");
    nums.append(
      el("span", tone(row.afterCost), row.afterCost == null ? "\u2014" : formatPct(row.afterCost, { signed: true, digits: 2 })),
      el("span", "dim", `IR ${row.ir == null ? "\u2014" : row.ir.toFixed(2)}`),
      el("span", "dim", `Sharpe ${row.sharpe == null ? "\u2014" : row.sharpe.toFixed(2)}`)
    );
    item.append(name, bar, nums);
    list.append(item);
  });
  card.append(list);
  const benchmark = oosPayload?.benchmark_note;
  card.append(el("p", "fine", benchmark ? String(benchmark) : "IR is against the sleeve benchmark named in data/models_oos.json."), watermark(oosPayload?.updated_at));
  return card;
}

function renderCryptoScorecard(models, oosPayload, trades, scorecard, book, summary) {
  const card = el("article", "sleeve crypto model-card model-scorecard card");
  card.id = "crypto-scorecard";
  const head = el("header", "sleeve-head");
  const title = el("h2");
  head.append(title);
  head.append(el("p", "fine", "Closed fills, rails, and the published out-of-sample read"));
  card.append(head);
  const panel = el("div", "card-panel");
  panel.id = "card-panel-crypto-scorecard";

  const grid = el("div", "metrics");
  const backend = backendView(scorecard, models, oosPayload);
  const backendMetric = metric("Live backend", backend.id || "UNKNOWN", "flat");
  backendMetric.append(el("p", "fine", backend.note || "Live backend is not stated in this snapshot."));
  grid.append(backendMetric);

  const fills = closedFillStats(trades, "crypto");
  const win = metric("Win %", formatWinPct(fills.rate), winTone(fills));
  win.append(el("p", "fine", `${formatWinRecord(fills)} closed sells`));
  grid.append(win);

  const expectancy = metric("Expectancy");
  const expectancyValue = expectancy.querySelector(".v");
  expectancyValue.classList.add(tone(fills.expectancyFrac));
  expectancyValue.replaceChildren(
    pair(
      fills.expectancyFrac == null ? "\u2014" : formatPct(fills.expectancyFrac, { signed: true, digits: 2 }),
      fills.expectancyUsd == null ? "" : formatUsd(fills.expectancyUsd, { signed: true })
    )
  );
  expectancy.append(el("p", "fine", "Mean P&L of decided crypto sells."));
  grid.append(expectancy);

  const fee = feeView(scorecard, trades);
  const feeMetric = metric("Fee drag", fee.value, fee.toneClass);
  feeMetric.append(el("p", "fine", fee.note));
  grid.append(feeMetric);

  const view = shownBooks(book, summary)[0] || null;
  const rails = railView(view?.runningBalance, view?.dayPnl, liveRails(summary, scorecard));
  const headroom = metric("Kill headroom");
  const headroomValue = headroom.querySelector(".v");
  if (rails.headroomUsd == null) {
    headroomValue.textContent = "\u2014";
  } else {
    headroomValue.replaceChildren(pair(`${formatPct(rails.headroomFrac)} of book`, formatUsd(rails.headroomUsd)));
  }
  const railText = [];
  if (rails.killFrac != null) railText.push(`Day kill ${formatPct(rails.killFrac)} (${formatUsd(rails.killUsd)})`);
  if (rails.targetFrac != null) {
    railText.push(`Day target ${formatPct(rails.targetFrac, { signed: true })} (${formatUsd(rails.targetUsd, { signed: true })}), realized only`);
  }
  headroom.append(el("p", "fine", railText.join(". ") || "Day rails are not in this snapshot."));
  grid.append(headroom);
  panel.append(grid);

  const unique = scorecard?.order_id_unique;
  let fillNote = fills.orderIdAvailable
    ? `Duplicate order ids dropped: ${fills.deduped}. Flat exits excluded. T24b will improve joined-fill metrics.`
    : "Same rules as sleeve Win %: sell, finite P&L, flat zero excluded. The scrubbed tape has no order id, so rows are not collapsed. T24b will improve joined-fill metrics.";
  if (unique && Number.isFinite(unique.wins) && Number.isFinite(unique.losses) && (unique.wins !== fills.wins || unique.losses !== fills.losses)) {
    fillNote = `Warehouse order-id unique closed sells are ${unique.wins}\u2013${unique.losses}. The headline matches sleeve Win % on the scrubbed tape. T24b will improve joined-fill metrics.`;
  }
  panel.append(el("p", "note", fillNote));

  const oosModels = cryptoOosModels(oosPayload);
  if (!oosModels.length) {
    panel.append(el("p", "note", "No crypto out-of-sample rows in this snapshot."));
  } else {
    const wrap = el("div", "score-scroll");
    const table = el("table", "score-oos");
    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    for (const label of ["Model", "AUC", "After cost", "IR vs BTC", "n long", "Promoted"]) {
      headerRow.append(el("th", null, label));
    }
    thead.append(headerRow);
    const tbody = document.createElement("tbody");
    for (const row of oosModels) {
      const line = document.createElement("tr");
      const promoted = row.promoted === true ? "yes" : row.promoted === false ? "no" : "\u2014";
      for (const text of [
        String(row.model || "model"),
        fixedDigits(row.auc, 4),
        fixedDigits(row.after_cost_mean, 4),
        fixedDigits(row.sleeve_ir_vs_spy, 4),
        row.n_long == null ? "\u2014" : String(row.n_long),
        promoted,
      ]) {
        line.append(el("td", null, text));
      }
      tbody.append(line);
    }
    table.append(thead, tbody);
    wrap.append(table);
    panel.append(wrap);
  }
  const benchmark = oosPayload?.benchmark_note || scorecard?.oos?.benchmark_note;
  if (benchmark) panel.append(el("p", "fine", String(benchmark)));
  const oosNote = oosPayload?.note || scorecard?.oos?.note;
  const feeBps = scorecard?.oos?.fee_bps;
  panel.append(
    el(
      "p",
      "note",
      oosNote
        ? String(oosNote)
        : feeBps != null
          ? `Out-of-sample after-cost uses ${feeBps} bp.`
          : "The out-of-sample fee assumption is not in this snapshot."
    )
  );

  const train = scorecard?.last_train || {};
  const trainBits = [];
  const modelsAsOf = train.models_as_of || models?.as_of;
  const oosAsOf = train.oos_updated_at || oosPayload?.updated_at;
  if (modelsAsOf) trainBits.push(`Models as of ${formatEt(modelsAsOf)}`);
  if (oosAsOf) trainBits.push(`OOS updated ${formatEt(oosAsOf)}`);
  if (train.trained_at) trainBits.push(`Trained ${formatEt(train.trained_at)}`);
  if (train.promoted_at) trainBits.push(`Promoted ${formatEt(train.promoted_at)}`);
  trainBits.push(train.note || "No separate train or promote timestamp is in the exported model files.");
  panel.append(el("p", "fine", trainBits.join(". ").replace(/\.\./g, ".")));
  panel.append(renderSignalLinkage(scorecard));
  card.append(panel);
  mountCollapse({
    heading: title,
    label: "Crypto scorecard",
    panel,
    storageKey: cardOpenKey("crypto-scorecard"),
    defaultOpen: defaultCardOpen("crypto"),
    collapsedHost: card,
  });
  return card;
}

function renderSignalLinkage(scorecard) {
  const link = scorecard?.signal_linkage;
  const known = link?.status === "known";
  const artifacts = known && link.artifact_count != null ? String(link.artifact_count) : "UNKNOWN";
  const outcomes = known && link.outcome_count != null ? String(link.outcome_count) : "UNKNOWN";
  const generated = link?.last_generated_at ? formatEt(link.last_generated_at) : "UNKNOWN";
  return el(
    "p",
    "note",
    `Live scores land in signal_artifacts (${artifacts}). Fills join on signal_trade_outcomes (${outcomes}). Last generated ${generated}.`
  );
}

function renderModels(payload, oosPayload, meta, summary, trades, scorecard, book, runs) {
  modelsEl.replaceChildren();
  const models = payload && Array.isArray(payload.models) ? payload.models.slice() : [];
  const exported = oosRows(oosPayload);
  const freshBits = [];
  if (meta?.fetched_at) freshBits.push(`Last refreshed ${formatEt(meta.fetched_at)}`);
  if (payload?.as_of) freshBits.push(`Models as of ${formatEt(payload.as_of)}`);
  if (oosPayload?.updated_at) freshBits.push(`OOS updated ${formatEt(oosPayload.updated_at)}`);
  if (freshBits.length) modelsEl.append(el("p", "freshness", freshBits.join(" · ")));

  const backend = backendView(scorecard, payload, oosPayload);
  const top = el("div", "duo");
  top.append(renderModelRace(oosPayload, backend), renderLatestCall(trades, backend, runs));
  modelsEl.append(top);
  if (payload && payload.note) modelsEl.append(el("p", "note", String(payload.note)));
  modelsEl.append(renderCryptoScorecard(payload, oosPayload, trades, scorecard, book, summary));
  if (!models.length && exported.length) {
    for (const row of exported) {
      models.push({
        sleeve: row.sleeve || row.book || (String(row.asset_class || "").toLowerCase() === "equity" ? "equities" : row.asset_class) || "",
        name: row.name || row.model || row.sleeve || "Model",
        used: row.used || "\u2014",
        training: row.training || "\u2014",
        data_source: row.data_source || row.data || "\u2014",
        oos: row,
      });
    }
  }
  if (!models.length) {
    modelsEl.append(el("p", "empty", "No model cards in this snapshot."));
    return;
  }
  const grid = el("div", "model-grid");
  const used = new Set();
  const anchored = new Set();
  const cardIds = new Set();
  for (const model of models) {
    const mine = exported.filter((row) => sameSleeve(row, String(model.sleeve || "").toLowerCase()));
    for (const row of mine) used.add(row);
    const card = el("article", "sleeve model-card card");
    const sleeveName = String(model.sleeve || "").toLowerCase();
    if ((sleeveName === "crypto" || sleeveName === "equities") && !anchored.has(sleeveName)) {
      card.id = `model-${sleeveName}`;
      anchored.add(sleeveName);
    }
    const head = el("header", "sleeve-head");
    const title = el("h2");
    head.append(title);
    if (model.sleeve) head.append(el("p", "fine", String(model.sleeve)));
    if (sleeveName === "equities") head.append(el("p", "chip paused", "Paused"));
    card.append(head);
    const panel = el("div", "card-panel");
    let storageId = modelCardId(model);
    if (cardIds.has(storageId)) storageId = `${storageId}-${cardIds.size + 1}`;
    cardIds.add(storageId);
    panel.id = `card-panel-${storageId}`;
    if (sleeveName === "equities") {
      panel.append(
        el("p", "note", "Paused. Equity modeling and place are not live. Crypto owns the Agentic book. This card is the last published research read.")
      );
    }
    const list = el("dl");
    for (const [label, value] of [
      ["Used", model.used],
      ["Training", model.training],
      ["Data", model.data_source],
    ]) {
      list.append(el("dt", null, label), el("dd", null, value == null || value === "" ? "\u2014" : String(value)));
    }
    list.append(el("dt", null, "OOS"));
    const oosDd = el("dd");
    if (mine.length) {
      const items = el("ul", "oos-list");
      for (const row of mine) items.append(el("li", null, formatMetricRow(row)));
      oosDd.append(items);
    } else {
      oosDd.textContent = oosText(model.oos);
    }
    list.append(oosDd);
    panel.append(list);
    card.append(panel);
    mountCollapse({
      heading: title,
      label: model.name || "Model",
      panel,
      storageKey: cardOpenKey(storageId),
      defaultOpen: defaultCardOpen(sleeveName),
      collapsedHost: card,
    });
    grid.append(card);
  }
  modelsEl.append(grid);
  const rest = exported.filter((row) => !used.has(row));
  if (rest.length) modelsEl.append(el("p", "note", rest.map(summarizeOosRow).join("; ")));
}

function focusId(id) {
  if (!id) return;
  const node = document.getElementById(id);
  if (!node) return;
  node.scrollIntoView({ block: "center" });
  node.classList.remove("flash");
  void node.offsetWidth;
  node.classList.add("flash");
}

let lastTrades = [];

function applyHash() {
  const target = parseHash(location.hash);
  if (target.kind === "models") {
    state.dayFilter = null;
    showTab("models");
    requestAnimationFrame(() => focusId(target.id === "models" ? "" : target.id));
  } else {
    showTab("sleeves");
    state.dayFilter = target.kind === "day" ? target.day : null;
    for (const tape of state.tapes.values()) tape.paint();
    paintDayBanner(lastTrades);
    if (target.kind === "day") {
      requestAnimationFrame(() => document.querySelector("#feed-section")?.scrollIntoView({ block: "start" }));
    } else if (target.kind === "trade") {
      for (const tape of state.tapes.values()) if (tape.reveal(target.id)) break;
      requestAnimationFrame(() => focusId(target.id));
    }
  }
}

async function loadLiveBook(token) {
  const committed = await loadJson("data/live_book.json", token).catch(() => null);
  try {
    const response = await fetch(LIVE_SIGNAL_URL, { cache: "no-store" });
    if (!response.ok) return committed;
    return mergeLiveBook(committed, await response.json());
  } catch {
    return committed;
  }
}

async function loadAll(meta) {
  const token = meta.fetched_at || meta.export_attempted_at || Date.now();
  const [summary, trades, models, oos, book, curves, scorecard, runsPayload] = await Promise.all([
    loadJson("data/kpi_summary.json", token),
    loadJson("data/kpi_trades_scrubbed.json", token),
    loadJson("data/models.json", token).catch(() => ({ models: [], note: "data/models.json is not in this snapshot." })),
    loadJson("data/models_oos.json", token).catch(() => ({ rows: [] })),
    loadLiveBook(token),
    loadJson("data/sleeve_curves.json", token).catch(() => ({ missing: true, series: [] })),
    loadJson("data/model_scorecard.json", token).catch(() => null),
    loadJson("data/runs.json", token).catch(() => null),
  ]);
  lastTrades = Array.isArray(trades) ? trades : [];
  state.points = curvePoints(curves, "crypto");
  const view = render(trades, meta, book, summary);
  renderChips(lastTrades, scorecard);
  renderRange();
  paintCurves();
  renderRisk(view, summary, scorecard);
  renderPositions(book, view?.asOf);
  renderModels(models, oos, meta, summary, lastTrades, scorecard, book, runRows(runsPayload));
  applyHash();
}

async function readMeta() {
  try {
    return await loadJson("data/meta.json", Date.now());
  } catch {
    return {
      export_status: "error",
      export_error: "data/meta.json did not load. The board cannot confirm this snapshot is fresh.",
    };
  }
}

async function main() {
  try {
    const saved = localStorage.getItem(RANGE_KEY);
    if (RANGES.some((range) => range.key === saved)) state.range = saved;
  } catch {
    /* private mode */
  }
  tabSleeves.addEventListener("click", () => {
    if (location.hash.startsWith("#model")) history.pushState(null, "", `${location.pathname}${location.search}`);
    showTab("sleeves");
  });
  tabModels.addEventListener("click", () => {
    history.pushState(null, "", "#models");
    showTab("models");
  });
  document.querySelectorAll('a[data-tab="models"]').forEach((anchor) => {
    anchor.addEventListener("click", () => {
      showTab("models");
      requestAnimationFrame(() => focusId(String(anchor.getAttribute("href") || "").replace(/^#/, "")));
    });
  });
  window.addEventListener("hashchange", applyHash);

  let meta = await readMeta();
  try {
    await loadAll(meta);
  } catch (error) {
    renderStatus(meta);
    const lines = statusEl.querySelector(".status-lines") || statusEl;
    lines.append(el("p", "status-copy status-error", "KPI JSON did not load."));
    heroEl.replaceChildren(el("p", "empty", String(error.message || error)));
    if (positionsEl) positionsEl.replaceChildren(el("p", "empty", "Open positions did not load."));
    if (curvesEl) curvesEl.replaceChildren(el("p", "empty", "Sleeve history did not load."));
  }

  let width = window.innerWidth;
  window.addEventListener("resize", () => {
    if (Math.abs(window.innerWidth - width) < 24) return;
    width = window.innerWidth;
    paintCurves();
    paintUnderwater();
  });

  setInterval(() => document.querySelectorAll(".fresh").forEach(paintFreshness), 30_000);
  setInterval(async () => {
    const next = await readMeta();
    if (next.fetched_at && next.fetched_at !== meta.fetched_at) {
      meta = next;
      loadAll(meta).catch(() => {});
    }
  }, 5 * 60_000);
}

main();
