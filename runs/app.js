import {
  emptyBundle,
  countLabel,
  formatProb,
  formatReturn,
  formatScore,
  formatWhen,
  metricEntries,
  normalizeBundle,
  predictionsFor,
  runLabel,
} from "./derive.js";

const statusEl = document.querySelector("#status");
const calibrationEl = document.querySelector("#calibration");
const versionsEl = document.querySelector("#versions");
const runsEl = document.querySelector("#runs");
const predictionsEl = document.querySelector("#predictions");

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function cell(text, className) {
  return el("td", className || "", text == null ? "" : String(text));
}

function table(headers, rows) {
  const wrap = el("div", "table-wrap runs-scroll");
  const tableEl = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  for (const header of headers) headRow.append(el("th", "", header));
  head.append(headRow);
  const body = document.createElement("tbody");
  for (const row of rows) {
    const tr = document.createElement("tr");
    for (const value of row) tr.append(value instanceof Node ? value : cell(value));
    body.append(tr);
  }
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = cell("None in this snapshot.", "empty");
    td.colSpan = headers.length;
    tr.append(td);
    body.append(tr);
  }
  tableEl.append(head, body);
  wrap.append(tableEl);
  return wrap;
}

function yesNo(value) {
  if (value === true) return "yes";
  if (value === false) return "no";
  return "";
}

function labelText(value) {
  if (value === true) return "up";
  if (value === false) return "down";
  return "open";
}

function metric(label, value) {
  const wrap = el("div", "metric");
  wrap.append(el("div", "k", label), el("div", "v", value));
  return wrap;
}

function svgEl(name) {
  return document.createElementNS("http://www.w3.org/2000/svg", name);
}

function calibrationChart(bins) {
  const width = 640;
  const height = 220;
  const padL = 36;
  const padB = 32;
  const padT = 12;
  const padR = 8;
  const innerW = width - padL - padR;
  const innerH = height - padT - padB;
  const svg = svgEl("svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("role", "img");
  const title = svgEl("title");
  title.textContent = "Calibration of p_up against the realized label rate";
  svg.append(title);
  const slot = innerW / Math.max(bins.length, 1);
  for (const tick of [0, 0.5, 1]) {
    const y = padT + innerH - tick * innerH;
    const line = svgEl("line");
    line.setAttribute("x1", String(padL));
    line.setAttribute("x2", String(width - padR));
    line.setAttribute("y1", String(y));
    line.setAttribute("y2", String(y));
    line.setAttribute("stroke", "rgba(244, 239, 230, 0.14)");
    svg.append(line);
    const label = svgEl("text");
    label.setAttribute("x", "0");
    label.setAttribute("y", String(y + 4));
    label.setAttribute("fill", "#b3a794");
    label.setAttribute("font-size", "11");
    label.textContent = String(tick);
    svg.append(label);
  }
  bins.forEach((bin, index) => {
    const x = padL + index * slot;
    if (bin.labelRate != null && bin.n > 0) {
      const bar = svgEl("rect");
      const barH = Math.max(0, Math.min(1, bin.labelRate)) * innerH;
      bar.setAttribute("x", String(x + slot * 0.18));
      bar.setAttribute("y", String(padT + innerH - barH));
      bar.setAttribute("width", String(slot * 0.64));
      bar.setAttribute("height", String(barH));
      bar.setAttribute("fill", "#e3a15a");
      svg.append(bar);
    }
    if (bin.meanPUp != null && bin.n > 0) {
      const y = padT + innerH - Math.max(0, Math.min(1, bin.meanPUp)) * innerH;
      const mark = svgEl("line");
      mark.setAttribute("x1", String(x + slot * 0.12));
      mark.setAttribute("x2", String(x + slot * 0.88));
      mark.setAttribute("y1", String(y));
      mark.setAttribute("y2", String(y));
      mark.setAttribute("stroke", "#7eb8c4");
      mark.setAttribute("stroke-width", "2");
      svg.append(mark);
    }
    const caption = svgEl("text");
    caption.setAttribute("x", String(x + slot / 2));
    caption.setAttribute("y", String(height - 10));
    caption.setAttribute("fill", "#b3a794");
    caption.setAttribute("font-size", "10");
    caption.setAttribute("text-anchor", "middle");
    const lo = bin.lo == null ? "" : Math.round(bin.lo * 100);
    caption.textContent = String(lo);
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
  const metrics = el("div", "metrics");
  metrics.append(
    metric("Resolved", String(calibration.nResolved)),
    metric("Log loss", formatScore(calibration.logloss)),
    metric("Brier", formatScore(calibration.brier)),
  );
  calibrationEl.append(metrics);
  calibrationEl.append(calibrationChart(calibration.bins));
  const legend = el("div", "legend");
  const rate = el("span");
  const rateSwatch = document.createElement("i");
  rateSwatch.style.background = "var(--crypto)";
  rate.append(rateSwatch, document.createTextNode("Realized label rate"));
  const mean = el("span");
  const meanSwatch = document.createElement("i");
  meanSwatch.style.background = "var(--equities)";
  mean.append(meanSwatch, document.createTextNode("Mean p_up"));
  legend.append(rate, mean);
  calibrationEl.append(legend);
  calibrationEl.append(
    table(
      ["Bin", "n", "Mean p_up", "Label rate"],
      calibration.bins.map((bin) => [
        `${formatProb(bin.lo)} to ${formatProb(bin.hi)}`,
        String(bin.n),
        formatProb(bin.meanPUp),
        formatProb(bin.labelRate),
      ]),
    ),
  );
}

function renderVersions(bundle) {
  versionsEl.replaceChildren();
  versionsEl.append(
    table(
      ["Name", "Status", "Parent", "Trained", "Promoted", "OOS"],
      bundle.modelVersions.map((version) => {
        const metrics = metricEntries(version.oos_metrics)
          .map((entry) => `${entry.key} ${entry.value}`)
          .join(", ");
        return [
          version.name || "",
          version.status || "",
          version.parent || "",
          formatWhen(version.trained_at),
          formatWhen(version.promoted_at),
          metrics,
        ];
      }),
    ),
  );
}

function renderRuns(bundle) {
  runsEl.replaceChildren();
  runsEl.append(
    table(
      ["Generated", "Model", "Backend", "Mode", "Last bar", "Anomaly"],
      bundle.runs.map((run) => [
        formatWhen(run.generated_at),
        run.model_version || "",
        run.backend || "",
        run.mode || "",
        formatWhen(run.last_bar_time),
        yesNo(run.anomaly),
      ]),
    ),
  );
}

function renderPredictions(bundle) {
  const selected = bundle.runs[0] ? bundle.runs[0].id : "";
  const draw = (runId) => {
    const rows = predictionsFor(bundle, runId);
    const body = table(
      ["Symbol", "p_up", "Side", "Conviction", "Bar", "Expires", "8h return", "Label", "Resolved", "Traded"],
      rows.map((row) => [
        row.symbol || "",
        formatProb(row.p_up),
        row.side || "",
        row.conviction == null ? "" : String(row.conviction),
        formatWhen(row.bar_time),
        formatWhen(row.expires_at),
        formatReturn(row.fwd_ret_8h),
        labelText(row.label_up_net30),
        formatWhen(row.resolved_at),
        yesNo(row.traded),
      ]),
    );
    const tools = el("div", "tape-head");
    const label = el("label", "tape-month");
    label.append(document.createTextNode("Run"));
    const select = document.createElement("select");
    select.setAttribute("aria-label", "Scoring run");
    const all = document.createElement("option");
    all.value = "";
    all.textContent = "All exported";
    select.append(all);
    for (const run of bundle.runs) {
      const option = document.createElement("option");
      option.value = run.id || "";
      option.textContent = runLabel(run);
      if ((run.id || "") === runId) option.selected = true;
      select.append(option);
    }
    if (!runId) all.selected = true;
    label.append(select);
    tools.append(label, el("p", "tape-count", `${rows.length} predictions`));
    predictionsEl.replaceChildren(tools, body);
    select.addEventListener("change", () => draw(select.value));
  };
  draw(selected);
}

function renderStatus(bundle) {
  statusEl.replaceChildren();
  if (bundle.source !== "supabase" || !bundle.fetchedAt) {
    statusEl.append(el("p", "status-copy", "Scoring runs are not in this snapshot yet."));
    return;
  }
  statusEl.append(
    el(
      "p",
      "status-copy",
      `Last exported ${formatWhen(bundle.fetchedAt)}. ${countLabel(bundle.runs.length, "run", "runs")}, ${countLabel(bundle.predictions.length, "prediction", "predictions")}, ${countLabel(bundle.modelVersions.length, "model version", "model versions")}.`,
    ),
  );
}

function render(bundle) {
  renderStatus(bundle);
  renderCalibration(bundle);
  renderVersions(bundle);
  renderRuns(bundle);
  renderPredictions(bundle);
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
}

load();
