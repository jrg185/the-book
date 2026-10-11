import { agoText, permalink, runRows, runsUpdatedAt, shareUrl } from "./board.js";
import { formatPct, tone } from "./derive.js";

const listEl = document.querySelector("#runs");
const subEl = document.querySelector("#runs-sub");
const filterEl = document.querySelector("#run-filter");
const selectEl = document.querySelector("#run-model");
const toastEl = document.querySelector("#toast");
const RUNS_URL = "https://jrg185.github.io/the-book/runs.html";

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function formatEt(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(date);
}

function runAnchor(run, used) {
  const base = run.ms == null ? "run" : `run-${new Date(run.ms).toISOString().replace(/\.\d+Z$/, "").replace(/[-:]/g, "").replace("T", "-")}`;
  const n = (used.get(base) || 0) + 1;
  used.set(base, n);
  return n === 1 ? base : `${base}-${n}`;
}

function statusTone(status) {
  const s = String(status || "").toLowerCase();
  if (/(ok|success|done|complete|filled|placed)/.test(s)) return "live";
  if (/(fail|error|kill|halt|abort)/.test(s)) return "error";
  if (/(skip|hold|stale|pause|no.?trade)/.test(s)) return "stale";
  return "";
}

function renderRun(run, id) {
  const card = el("article", "card shot run-card");
  card.id = id;
  const top = el("div", "run-top");
  top.append(el("strong", null, run.model || "Run"));
  if (run.status) top.append(el("span", `chip ${statusTone(run.status)}`.trim(), run.status));
  if (run.startedAt) {
    const when = el("time", "run-when", `${formatEt(run.startedAt)} · ${agoText(run.ms)}`);
    when.dateTime = run.startedAt;
    top.append(when);
  }
  card.append(top);
  if (run.decision) card.append(el("p", "call-decision", run.decision));
  if (run.summary) card.append(el("blockquote", "call-why", run.summary));
  const stats = el("div", "run-stats");
  if (run.trades != null) stats.append(el("span", null, `${run.trades} trade${run.trades === 1 ? "" : "s"}`));
  if (run.pnlFrac != null) stats.append(el("span", tone(run.pnlFrac), `${formatPct(run.pnlFrac, { signed: true, digits: 2 })} of book`));
  if (run.tickers.length) stats.append(el("span", "dim", run.tickers.join(" · ")));
  if (stats.childNodes.length) card.append(stats);
  const actions = el("div", "card-actions");
  const url = permalink(id, RUNS_URL);
  const text = [`${run.model || "A run"} on The Book`, run.decision, run.startedAt ? formatEt(run.startedAt) : null].filter(Boolean).join(" · ");
  const share = el("a", "share-x");
  share.href = shareUrl(text, url);
  share.target = "_blank";
  share.rel = "noopener";
  share.append(el("span", "x-glyph", "\ud835\udd4f"), document.createTextNode("Share"));
  const copy = el("button", "copy-link", "Copy link");
  copy.type = "button";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url);
      toastEl.textContent = "Link copied";
    } catch {
      toastEl.textContent = url;
    }
    toastEl.hidden = false;
    setTimeout(() => {
      toastEl.hidden = true;
    }, 1800);
  });
  actions.append(share, copy);
  card.append(actions);
  const mark = el("p", "watermark");
  mark.append(el("span", "wm-brand", "jrg185.github.io/the-book/runs.html"), el("span", null, run.startedAt ? formatEt(run.startedAt) : ""));
  card.append(mark);
  return card;
}

function paint(runs, model) {
  listEl.replaceChildren();
  const used = new Map();
  const shown = model ? runs.filter((run) => run.model === model) : runs;
  for (const run of shown) listEl.append(renderRun(run, runAnchor(run, used)));
  if (!shown.length) listEl.append(el("p", "empty", "No runs for this model."));
}

function focusHash() {
  const id = location.hash.replace(/^#/, "");
  if (!/^run-[0-9a-z-]+$/.test(id)) return;
  const node = document.getElementById(id);
  if (node) {
    node.scrollIntoView({ block: "center" });
    node.classList.add("flash");
  }
}

async function main() {
  let payload = null;
  try {
    const response = await fetch(`data/runs.json?t=${Date.now()}`, { cache: "no-store" });
    if (response.ok) payload = await response.json();
  } catch {
    payload = null;
  }
  const runs = runRows(payload);
  if (!payload || !runs.length) {
    subEl.textContent = payload ? "data/runs.json has no runs yet." : "No runs are published yet.";
    listEl.replaceChildren(
      el("p", "empty", "This page reads data/runs.json. When the runs export lands, each desk run shows up here with its model, decision, and reasoning.")
    );
    return;
  }
  const updated = runsUpdatedAt(payload);
  subEl.textContent = `${runs.length} run${runs.length === 1 ? "" : "s"}${updated ? ` · updated ${formatEt(updated)}` : ""}`;
  const models = [...new Set(runs.map((run) => run.model).filter(Boolean))].sort();
  if (models.length > 1) {
    filterEl.hidden = false;
    selectEl.append(new Option("All", ""));
    for (const model of models) selectEl.append(new Option(model, model));
    selectEl.addEventListener("change", () => paint(runs, selectEl.value));
  }
  paint(runs, "");
  focusHash();
  window.addEventListener("hashchange", focusHash);
}

main();
