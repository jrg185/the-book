import { agoText, permalink, shareUrl, SITE_URL } from "../board.js";
import {
  countLabel,
  emptyBundle,
  formatProb,
  formatReturn,
  formatScore,
  formatWhen,
  normalizeBundle,
  oosChips,
  predictionsByRun,
  runAnchors,
  runLabel,
  runModelLabel,
  runModelName,
  runStats,
  versionNames,
} from "./derive.js";

const RUNS_URL = `${SITE_URL}runs/`;
const RUN_PAGE = 25;
const PREDICTION_PAGE = 60;

const statusEl = document.querySelector("#status");
const summaryEl = document.querySelector("#runs-summary");
const calibrationEl = document.querySelector("#calibration");
const versionsEl = document.querySelector("#versions");
const runsEl = document.querySelector("#runs");
const predictionsEl = document.querySelector("#predictions");
const toastEl = document.querySelector("#toast");

const state = { bundle: null, runs: [], anchors: [], names: new Map(), byRun: new Map(), runLimit: RUN_PAGE, selected: 0 };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
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

function shareButton(text, url) {
  const link = el("a", "share-x");
  link.href = shareUrl(text, url);
  link.target = "_blank";
  link.rel = "noopener";
  link.setAttribute("aria-label", "Share on X");
  const glyph = el("span", "x-glyph", "\ud835\udd4f");
  glyph.setAttribute("aria-hidden", "true");
  link.append(glyph, document.createTextNode("Share"));
  return link;
}

function copyLinkButton(url, label = "Copy link") {
  const button = el("button", "copy-link", label);
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
  mark.append(el("span", "wm-brand", "jrg185.github.io/the-book/runs"));
  if (asOf) mark.append(el("span", null, formatWhen(asOf)));
  return mark;
}

function stat(label, value, sub, valueTone) {
  const chip = el("div", "stat");
  chip.append(el("div", "stat-k", label), el("div", `stat-v ${valueTone || ""}`.trim(), value || "—"));
  if (sub) chip.append(el("div", "stat-sub", sub));
  return chip;
}

function td(label, text, className) {
  const cell = el("td", className || "", text == null ? "" : String(text));
  cell.dataset.label = label;
  return cell;
}

function table(headers, className) {
  const wrap = el("div", "table-wrap");
  const tableEl = el("table", `cards-on-mobile ${className || ""}`.trim());
  const head = document.createElement("thead");
  const row = document.createElement("tr");
  for (const [label, cls] of headers) {
    const th = el("th", cls || "", label);
    th.scope = "col";
    row.append(th);
  }
  head.append(row);
  const body = document.createElement("tbody");
  tableEl.append(head, body);
  wrap.append(tableEl);
  return { wrap, body };
}

function emptyRow(body, span, text) {
  const tr = el("tr", "empty-row");
  const cell = el("td", "empty", text);
  cell.colSpan = span;
  tr.append(cell);
  body.append(tr);
}

function sidePill(side) {
  const text = side ? side.toUpperCase() : "—";
  const cls = side === "buy" || side === "long" ? "buy" : side === "sell" || side === "short" ? "sell" : "flat";
  return el("span", `pill ${cls}`, text);
}

function splitText(stats) {
  return `${stats.buy} buy · ${stats.sell} sell · ${stats.flat} flat`;
}

function hitText(stats) {
  if (!stats.calls) return stats.resolved ? `${stats.resolved} resolved, no directional calls` : "Unresolved";
  return `${formatProb(stats.hitRate)} of ${stats.calls}`;
}

function runShareText(run, stats) {
  return [
    `The Book scoring run: ${runModelLabel(run, state.names)}`,
    formatWhen(run.generated_at),
    countLabel(stats.n, "symbol", "symbols"),
    splitText(stats),
  ]
    .filter(Boolean)
    .join(" · ");
}

function formatDay(value) {
  const date = new Date(value || "");
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", month: "short", day: "numeric" }).format(date);
}

function statusCounts(versions) {
  const counts = new Map();
  for (const version of versions) if (version.status) counts.set(version.status, (counts.get(version.status) || 0) + 1);
  return [...counts].map(([status, n]) => `${n} ${status}`).join(" · ");
}

function renderStatus(bundle) {
  statusEl.replaceChildren();
  statusEl.classList.remove("is-live");
  if (bundle.source !== "supabase" || !bundle.fetchedAt) {
    statusEl.append(el("p", "status-copy", "Scoring runs are not in this snapshot yet."));
    return;
  }
  statusEl.classList.add("is-live");
  statusEl.append(
    el(
      "p",
      "status-copy",
      `Last exported ${formatWhen(bundle.fetchedAt)}. ${countLabel(bundle.runs.length, "run", "runs")}, ${countLabel(bundle.predictions.length, "prediction", "predictions")}, ${countLabel(bundle.modelVersions.length, "model version", "model versions")}.`
    )
  );
}

function renderSummary(bundle) {
  summaryEl.replaceChildren();
  const latest = state.runs[0] || null;
  const top = el("div", "hero-top");
  top.append(el("p", "eyebrow", "Scoring runs"));
  const promoted = bundle.modelVersions.find((version) => version.status === "promoted");
  if (promoted?.name) top.append(el("span", "chip live", `promoted: ${promoted.name}`));
  const fresh = el("span", "fresh");
  const ms = Date.parse(latest?.generated_at || "");
  if (!Number.isNaN(ms) && Date.now() - ms > 6 * 3600 * 1000) fresh.classList.add("old");
  fresh.append(el("span", "pulse"), document.createTextNode(Number.isNaN(ms) ? "No runs yet" : `Last run ${agoText(ms)}`));
  top.append(fresh);

  const body = el("div", "runs-hero-body");
  const left = el("div");
  if (latest) {
    const stats = runStats(state.byRun.get(latest.id));
    const name = runModelName(latest, state.names);
    left.append(
      el("p", "hero-kicker", `Latest run · ${formatWhen(latest.generated_at)}`),
      el("p", "runs-hero-model", `${countLabel(stats.n, "symbol", "symbols")} scored`)
    );
    const meta = el("p", "runs-hero-meta");
    meta.append(
      el("span", name ? "" : "dim", runModelLabel(latest, state.names)),
      document.createTextNode([latest.backend, latest.mode].filter(Boolean).map((part) => ` · ${part}`).join(""))
    );
    if (latest.anomaly) meta.append(el("span", "chip stale", "anomaly"));
    left.append(meta, el("p", "runs-hero-split", splitText(stats)));
  } else {
    left.append(el("p", "empty", "No scoring runs in this snapshot."));
  }
  body.append(left);

  const calibration = bundle.calibration;
  const resolved = bundle.predictions.filter((row) => row.resolved_at);
  const overall = runStats(resolved);
  const chips = el("div", "chips runs-chips");
  chips.append(
    stat("Runs", String(bundle.runs.length), bundle.runs.length ? `since ${formatDay(state.runs[state.runs.length - 1].generated_at)}` : ""),
    stat("Predictions", bundle.predictions.length.toLocaleString("en-US"), `${new Set(bundle.predictions.map((row) => row.symbol)).size} symbols`),
    stat("Models", String(bundle.modelVersions.length), statusCounts(bundle.modelVersions)),
    stat("Resolved", calibration.nResolved.toLocaleString("en-US"), "8h label known"),
    stat("Hit rate", overall.calls ? formatProb(overall.hitRate) : "—", overall.calls ? `${overall.calls} buy or sell calls` : "No resolved buy or sell calls", overall.hitRate == null ? "" : overall.hitRate >= 0.5 ? "up" : "down"),
    stat("Log loss", formatScore(calibration.logloss), calibration.brier == null ? "" : `Brier ${formatScore(calibration.brier)}`),
  );

  const actions = el("div", "card-actions");
  const text = latest
    ? `The Book: ${countLabel(bundle.runs.length, "scoring run", "scoring runs")}, ${bundle.predictions.length.toLocaleString("en-US")} predictions, log loss ${formatScore(calibration.logloss)}`
    : "The Book scoring runs";
  actions.append(shareButton(text, RUNS_URL), copyLinkButton(RUNS_URL));
  summaryEl.append(top, body, chips, actions, watermark(bundle.fetchedAt));
}

function renderVersions(bundle) {
  versionsEl.replaceChildren();
  if (!bundle.modelVersions.length) {
    versionsEl.append(el("p", "empty", "No model versions in this snapshot."));
    return;
  }
  const order = { promoted: 0, candidate: 1 };
  const versions = [...bundle.modelVersions].sort(
    (a, b) => (order[a.status] ?? 2) - (order[b.status] ?? 2) || String(a.name).localeCompare(String(b.name))
  );
  for (const version of versions) {
    const card = el("article", "version-card shot");
    const head = el("div", "version-head");
    head.append(el("strong", "version-name", version.name || "Unnamed"));
    if (version.status) head.append(el("span", `chip ${version.status === "promoted" ? "live" : ""}`.trim(), version.status));
    card.append(head);
    const lineage = [
      version.parent ? `from ${version.parent}` : "root",
      version.trained_at ? `trained ${formatWhen(version.trained_at)}` : null,
      version.promoted_at ? `promoted ${formatWhen(version.promoted_at)}` : null,
    ].filter(Boolean);
    card.append(el("p", "fine", lineage.join(" · ")));
    const chips = oosChips(version.oos_metrics);
    if (chips.length) {
      const list = el("ul", "oos-chips");
      list.setAttribute("aria-label", `${version.name || "Model"} out-of-sample metrics`);
      for (const chip of chips) {
        const item = el("li", `ochip ${chip.kind === "count" ? "count" : ""}`.trim());
        item.append(el("span", "ochip-k", chip.label), el("span", `ochip-v ${chip.tone}`.trim(), chip.value));
        list.append(item);
      }
      card.append(list);
    } else if (typeof version.oos_metrics === "string" && version.oos_metrics) {
      card.append(el("p", "fine", version.oos_metrics));
    } else {
      card.append(el("p", "empty", "No out-of-sample metrics published."));
    }
    versionsEl.append(card);
  }
}

function svgEl(name, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function calibrationChart(bins) {
  const width = 640;
  const height = 240;
  const pad = { l: 40, r: 10, t: 14, b: 34 };
  const innerW = width - pad.l - pad.r;
  const innerH = height - pad.t - pad.b;
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, class: "chart-svg calib-svg", role: "img" });
  const title = svgEl("title");
  title.textContent = "Calibration of p_up against the realized label rate";
  svg.append(title);
  const y = (value) => pad.t + innerH - Math.max(0, Math.min(1, value)) * innerH;
  for (const tick of [0, 0.25, 0.5, 0.75, 1]) {
    svg.append(svgEl("line", { x1: pad.l, x2: width - pad.r, y1: y(tick), y2: y(tick), class: "grid" }));
    const label = svgEl("text", { x: pad.l - 8, y: y(tick) + 4, class: "axis", "text-anchor": "end" });
    label.textContent = `${Math.round(tick * 100)}%`;
    svg.append(label);
  }
  svg.append(svgEl("line", { x1: pad.l, y1: y(0), x2: width - pad.r, y2: y(1), class: "calib-ideal" }));
  const slot = innerW / Math.max(bins.length, 1);
  bins.forEach((bin, index) => {
    const x = pad.l + index * slot;
    if (bin.labelRate != null && bin.n > 0) {
      const top = y(bin.labelRate);
      const bar = svgEl("rect", { x: x + slot * 0.16, y: top, width: slot * 0.68, height: y(0) - top, rx: 4, class: "calib-bar" });
      const tip = svgEl("title");
      tip.textContent = `${formatProb(bin.lo)} to ${formatProb(bin.hi)}: ${bin.n} rows, label rate ${formatProb(bin.labelRate)}, mean p_up ${formatProb(bin.meanPUp)}`;
      bar.append(tip);
      svg.append(bar);
      const count = svgEl("text", { x: x + slot / 2, y: top - 6, class: "axis", "text-anchor": "middle" });
      count.textContent = `n ${bin.n}`;
      svg.append(count);
    }
    if (bin.meanPUp != null && bin.n > 0) {
      svg.append(svgEl("line", { x1: x + slot * 0.1, x2: x + slot * 0.9, y1: y(bin.meanPUp), y2: y(bin.meanPUp), class: "calib-mean" }));
    }
    const caption = svgEl("text", { x: x + slot / 2, y: height - 12, class: "axis", "text-anchor": "middle" });
    caption.textContent = bin.lo == null ? "" : `${Math.round(bin.lo * 100)}`;
    svg.append(caption);
  });
  const frame = el("div", "chart-frame");
  frame.append(svg);
  return frame;
}

function renderCalibration(bundle) {
  calibrationEl.replaceChildren();
  const calibration = bundle.calibration;
  if (!calibration.nResolved) {
    calibrationEl.append(el("p", "empty", "No resolved predictions in this snapshot."));
    return;
  }
  const chips = el("div", "chips calib-chips");
  chips.append(
    stat("Resolved", calibration.nResolved.toLocaleString("en-US")),
    stat("Log loss", formatScore(calibration.logloss), "Lower is better"),
    stat("Brier", formatScore(calibration.brier), "0.25 is a coin flip"),
  );
  const legend = el("div", "legend");
  for (const [cls, text] of [
    ["calib-bar", "Realized label rate"],
    ["calib-mean", "Mean p_up"],
    ["calib-ideal", "Perfect calibration"],
  ]) {
    const item = el("span");
    item.append(el("i", `swatch ${cls}`), document.createTextNode(text));
    legend.append(item);
  }
  const occupied = calibration.bins.filter((bin) => bin.n > 0);
  const { wrap, body } = table([["p_up bin"], ["Rows", "num"], ["Mean p_up", "num"], ["Label rate", "num"]], "calib-table");
  for (const bin of occupied) {
    const tr = document.createElement("tr");
    tr.append(
      td("p_up bin", `${formatProb(bin.lo)} to ${formatProb(bin.hi)}`, "ticker"),
      td("Rows", String(bin.n), "num"),
      td("Mean p_up", formatProb(bin.meanPUp), "num"),
      td("Label rate", formatProb(bin.labelRate), "num"),
    );
    body.append(tr);
  }
  if (!occupied.length) emptyRow(body, 4, "No occupied bins.");
  const note = el("p", "fine", "Bars above the diagonal mean the label came up more often than the model said. Empty bins are hidden from the table.");
  const grid = el("div", "calib-grid");
  const chart = el("div");
  chart.append(calibrationChart(calibration.bins), legend);
  const side = el("div");
  side.append(chips, wrap, note);
  grid.append(chart, side);
  calibrationEl.append(grid, watermark(bundle.fetchedAt));
}

function renderRuns() {
  runsEl.replaceChildren();
  const { wrap, body } = table(
    [
      ["Generated", "time"],
      ["Model"],
      ["Backend"],
      ["Mode"],
      ["Last bar"],
      ["Symbols", "num"],
      ["Buy / sell / flat", "num"],
      ["Resolved hit", "num"],
      ["Flags"],
      ["", "actions"],
    ],
    "runs-table"
  );
  const shown = state.runs.slice(0, state.runLimit);
  shown.forEach((run, index) => {
    const anchor = state.anchors[index];
    const stats = runStats(state.byRun.get(run.id));
    const tr = document.createElement("tr");
    tr.id = anchor;
    if (index === state.selected) tr.classList.add("is-selected");
    const when = td("Generated", "", "time");
    const pick = el("button", "run-pick", formatWhen(run.generated_at) || "Unknown time");
    pick.type = "button";
    pick.setAttribute("aria-label", `Show predictions for ${runLabel(run, state.names)}`);
    pick.addEventListener("click", () => selectRun(index, true));
    when.append(pick);
    const name = runModelName(run, state.names);
    const model = td("Model", runModelLabel(run, state.names), name ? "ticker" : "ticker dim");
    const flags = td("Flags", "", "flags");
    if (run.anomaly) flags.append(el("span", "chip stale", "anomaly"));
    else flags.append(el("span", "dim", run.anomaly === false ? "clean" : "—"));
    const actions = td("", "", "row-actions");
    const url = permalink(anchor, RUNS_URL);
    const share = shareButton(runShareText(run, stats), url);
    actions.append(share, copyLinkButton(url, "Link"));
    tr.append(
      when,
      model,
      td("Backend", run.backend || "—"),
      td("Mode", run.mode || "—"),
      td("Last bar", formatWhen(run.last_bar_time) || "—"),
      td("Symbols", String(stats.n), "num"),
      td("Buy / sell / flat", `${stats.buy} / ${stats.sell} / ${stats.flat}`, "num"),
      td("Resolved hit", stats.calls ? `${formatProb(stats.hitRate)} (${stats.calls})` : stats.resolved ? "no calls" : "open", "num"),
      flags,
      actions,
    );
    body.append(tr);
  });
  if (!shown.length) emptyRow(body, 10, "None in this snapshot.");
  runsEl.append(wrap);
  if (state.runs.length > state.runLimit) {
    const more = el("button", "show-more", `Show ${Math.min(RUN_PAGE, state.runs.length - state.runLimit)} more of ${state.runs.length - state.runLimit}`);
    more.type = "button";
    more.addEventListener("click", () => {
      state.runLimit += RUN_PAGE;
      renderRuns();
    });
    runsEl.append(more);
  }
  const unnamed = state.runs.filter((run) => !runModelName(run, state.names)).length;
  if (unnamed) {
    runsEl.append(
      el(
        "p",
        "fine",
        `${countLabel(unnamed, "run has", "runs have")} no model_version_name in this export, so the model shows as unnamed. The next Export KPI run reads model_version_name from runs_public.`
      )
    );
  }
}

function renderPredictions() {
  const run = state.runs[state.selected] || null;
  const rows = run ? state.byRun.get(run.id) || [] : state.bundle.predictions;
  const sorted = [...rows].sort((a, b) => (b.conviction ?? -1) - (a.conviction ?? -1) || String(a.symbol).localeCompare(String(b.symbol)));
  let limit = PREDICTION_PAGE;

  const tools = el("div", "tape-head");
  const label = el("label", "tape-month");
  label.append(document.createTextNode("Run"));
  const select = document.createElement("select");
  select.setAttribute("aria-label", "Scoring run");
  state.runs.forEach((item, index) => {
    const option = document.createElement("option");
    option.value = String(index);
    option.textContent = runLabel(item, state.names);
    if (index === state.selected) option.selected = true;
    select.append(option);
  });
  select.addEventListener("change", () => selectRun(Number(select.value), false));
  label.append(select);
  const stats = runStats(rows);
  tools.append(label, el("p", "tape-count", `${countLabel(stats.n, "prediction", "predictions")} · ${splitText(stats)} · ${stats.traded} traded`));

  const host = el("div");
  const draw = () => {
    const { wrap, body } = table(
      [["Symbol", "ticker"], ["p_up", "num"], ["Side"], ["Conviction", "num"], ["Bar"], ["Expires"], ["8h return", "num"], ["Label"], ["Traded"]],
      "pred-table"
    );
    for (const row of sorted.slice(0, limit)) {
      const tr = document.createElement("tr");
      const side = td("Side", "", "side");
      side.append(sidePill(row.side));
      const label = row.label_up_net30 === true ? "up" : row.label_up_net30 === false ? "down" : "open";
      tr.append(
        td("Symbol", row.symbol || "", "ticker"),
        td("p_up", formatProb(row.p_up), "num"),
        side,
        td("Conviction", row.conviction == null ? "" : row.conviction.toFixed(2), "num"),
        td("Bar", formatWhen(row.bar_time)),
        td("Expires", formatWhen(row.expires_at)),
        td("8h return", formatReturn(row.fwd_ret_8h) || "—", `num ${row.fwd_ret_8h > 0 ? "up" : row.fwd_ret_8h < 0 ? "down" : "dim"}`),
        td("Label", label, label === "up" ? "up" : label === "down" ? "down" : "dim"),
        td("Traded", row.traded ? "yes" : "no", row.traded ? "" : "dim"),
      );
      body.append(tr);
    }
    if (!sorted.length) emptyRow(body, 9, "No predictions for this run.");
    host.replaceChildren(wrap);
    if (sorted.length > limit) {
      const more = el("button", "show-more", `Show all ${sorted.length}`);
      more.type = "button";
      more.addEventListener("click", () => {
        limit = sorted.length;
        draw();
      });
      host.append(more);
    }
  };
  draw();
  const note = el("p", "fine", "Sorted by conviction. A buy or sell call hits when the resolved label_up_net30 went its way.");
  predictionsEl.replaceChildren(tools, host, note);
}

function selectRun(index, scroll) {
  if (!state.runs[index]) return;
  state.selected = index;
  if (index >= state.runLimit) state.runLimit = Math.ceil((index + 1) / RUN_PAGE) * RUN_PAGE;
  renderRuns();
  renderPredictions();
  if (scroll) predictionsEl.closest("section").scrollIntoView({ behavior: "smooth", block: "start" });
}

function applyHash() {
  const id = location.hash.replace(/^#/, "");
  if (!/^run-[0-9a-z-]+$/.test(id)) return;
  const index = state.anchors.indexOf(id);
  if (index < 0) return;
  selectRun(index, false);
  const row = document.getElementById(id);
  if (row) {
    row.scrollIntoView({ block: "center" });
    row.classList.add("flash");
  }
}

function render(bundle) {
  state.bundle = bundle;
  state.names = versionNames(bundle);
  state.runs = [...bundle.runs].sort((a, b) => String(b.generated_at || "").localeCompare(String(a.generated_at || "")));
  state.anchors = runAnchors(state.runs);
  state.byRun = predictionsByRun(bundle);
  state.selected = 0;
  state.runLimit = RUN_PAGE;
  renderStatus(bundle);
  renderSummary(bundle);
  renderVersions(bundle);
  renderCalibration(bundle);
  renderRuns();
  renderPredictions();
}

async function load() {
  try {
    const response = await fetch("../data/runs.json", { cache: "no-store" });
    if (!response.ok) throw new Error(String(response.status));
    render(normalizeBundle(await response.json()));
  } catch (error) {
    render(emptyBundle());
    statusEl.replaceChildren(el("p", "status-copy status-error", "Scoring runs snapshot could not be read."));
  }
  applyHash();
  window.addEventListener("hashchange", applyHash);
}

load();
