// SVG line and area charts with a hover crosshair. DOM only, no data access.

const SVG_NS = "http://www.w3.org/2000/svg";

export function svgEl(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [key, value] of Object.entries(attrs || {})) node.setAttribute(key, String(value));
  return node;
}

function thin(points, max) {
  if (points.length <= max) return points;
  const step = points.length / max;
  const out = [];
  for (let i = 0; i < max; i += 1) out.push(points[Math.floor(i * step)]);
  out.push(points[points.length - 1]);
  return out;
}

// points: [{ ms, ... }]. value(point) is what is plotted.
// opts: { height, color, baseline, format(v), when(ms), label, zeroLine, invert }
export function areaChart(host, points, opts = {}) {
  const width = Math.max(280, Math.round(host.clientWidth || 720));
  host.replaceChildren();
  const height = opts.height || 260;
  const pad = { left: 8, right: 56, top: 14, bottom: 26 };
  if (!points || points.length < 2) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = opts.emptyText || "Not enough history in this window.";
    host.append(empty);
    return null;
  }
  const value = opts.value || ((point) => point.v);
  const drawn = thin(points, 600);
  const values = drawn.map(value);
  let lo = Math.min(...values, opts.baseline ?? Infinity);
  let hi = Math.max(...values, opts.baseline ?? -Infinity);
  if (lo === hi) {
    lo -= 0.005;
    hi += 0.005;
  }
  const span = hi - lo;
  lo -= span * 0.08;
  hi += span * 0.08;
  const t0 = drawn[0].ms;
  const t1 = drawn[drawn.length - 1].ms;
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const x = (ms) => pad.left + ((ms - t0) / (t1 - t0 || 1)) * plotW;
  const y = (v) => pad.top + (1 - (v - lo) / (hi - lo)) * plotH;
  const base = opts.baseline ?? lo;

  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    width,
    height,
    role: "img",
    class: "chart-svg",
  });
  const title = svgEl("title");
  title.textContent = opts.label || "Chart";
  svg.append(title);

  const gradId = `g-${Math.random().toString(36).slice(2, 8)}`;
  const defs = svgEl("defs");
  const grad = svgEl("linearGradient", { id: gradId, x1: 0, y1: opts.invert ? 1 : 0, x2: 0, y2: opts.invert ? 0 : 1 });
  grad.append(
    svgEl("stop", { offset: 0, "stop-color": opts.color, "stop-opacity": 0.32 }),
    svgEl("stop", { offset: 1, "stop-color": opts.color, "stop-opacity": 0 })
  );
  defs.append(grad);
  svg.append(defs);

  for (let i = 0; i < 4; i += 1) {
    const v = hi - ((hi - lo) * (i + 0.5)) / 4;
    svg.append(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: y(v), y2: y(v), class: "grid" }));
    const label = svgEl("text", { x: width - pad.right + 8, y: y(v) + 4, class: "axis" });
    label.textContent = opts.format ? opts.format(v) : v.toFixed(3);
    svg.append(label);
  }
  if (opts.baseline != null && opts.baseline >= lo && opts.baseline <= hi) {
    svg.append(svgEl("line", { x1: pad.left, x2: width - pad.right, y1: y(opts.baseline), y2: y(opts.baseline), class: "baseline" }));
  }

  const coords = drawn.map((point) => `${x(point.ms).toFixed(1)},${y(value(point)).toFixed(1)}`);
  const line = `M${coords.join(" L")}`;
  const by = y(Math.min(Math.max(base, lo), hi)).toFixed(1);
  svg.append(
    svgEl("path", { d: `${line} L${x(t1).toFixed(1)},${by} L${x(t0).toFixed(1)},${by} Z`, fill: `url(#${gradId})` }),
    svgEl("path", { d: line, fill: "none", stroke: opts.color, "stroke-width": 2.25, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke" })
  );
  const last = drawn[drawn.length - 1];
  svg.append(svgEl("circle", { cx: x(last.ms), cy: y(value(last)), r: 3.5, fill: opts.color, class: "last-dot" }));

  const startLabel = svgEl("text", { x: pad.left, y: height - 6, class: "axis" });
  startLabel.textContent = opts.when ? opts.when(t0) : "";
  const endLabel = svgEl("text", { x: width - pad.right, y: height - 6, class: "axis", "text-anchor": "end" });
  endLabel.textContent = opts.when ? opts.when(t1) : "";
  svg.append(startLabel, endLabel);

  const cross = svgEl("line", { y1: pad.top, y2: height - pad.bottom, class: "crosshair", visibility: "hidden" });
  const dot = svgEl("circle", { r: 4.5, fill: opts.color, class: "cross-dot", visibility: "hidden" });
  svg.append(cross, dot);

  const frame = document.createElement("div");
  frame.className = "chart-frame";
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  tip.hidden = true;
  frame.append(svg, tip);
  host.append(frame);

  function nearest(ms) {
    let lo2 = 0;
    let hi2 = points.length - 1;
    while (hi2 - lo2 > 1) {
      const mid = (lo2 + hi2) >> 1;
      if (points[mid].ms < ms) lo2 = mid;
      else hi2 = mid;
    }
    return Math.abs(points[lo2].ms - ms) <= Math.abs(points[hi2].ms - ms) ? points[lo2] : points[hi2];
  }

  function show(clientX) {
    const rect = svg.getBoundingClientRect();
    if (!rect.width) return;
    const vx = ((clientX - rect.left) / rect.width) * width;
    const ratio = Math.min(1, Math.max(0, (vx - pad.left) / plotW));
    const point = nearest(t0 + ratio * (t1 - t0));
    const px = x(point.ms);
    const py = y(value(point));
    cross.setAttribute("x1", px);
    cross.setAttribute("x2", px);
    dot.setAttribute("cx", px);
    dot.setAttribute("cy", py);
    cross.setAttribute("visibility", "visible");
    dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.replaceChildren();
    const v = document.createElement("strong");
    v.textContent = opts.format ? opts.format(value(point)) : String(value(point));
    const t = document.createElement("span");
    t.textContent = opts.when ? opts.when(point.ms, true) : "";
    tip.append(v, t);
    const left = (px / width) * rect.width;
    tip.style.left = `${Math.min(Math.max(left, 60), rect.width - 60)}px`;
  }
  function hide() {
    cross.setAttribute("visibility", "hidden");
    dot.setAttribute("visibility", "hidden");
    tip.hidden = true;
  }
  svg.addEventListener("pointermove", (event) => show(event.clientX));
  svg.addEventListener("pointerdown", (event) => show(event.clientX));
  svg.addEventListener("pointerleave", hide);
  return { svg, frame };
}

export function sparkline(points, value, color, size = { width: 220, height: 56 }) {
  const svg = svgEl("svg", { viewBox: `0 0 ${size.width} ${size.height}`, class: "spark", "aria-hidden": "true", preserveAspectRatio: "none" });
  if (!points || points.length < 2) return svg;
  const drawn = thin(points, 160);
  const vals = drawn.map(value);
  let lo = Math.min(...vals);
  let hi = Math.max(...vals);
  if (lo === hi) {
    lo -= 0.01;
    hi += 0.01;
  }
  const t0 = drawn[0].ms;
  const t1 = drawn[drawn.length - 1].ms;
  const coords = drawn.map((p) => {
    const px = ((p.ms - t0) / (t1 - t0 || 1)) * size.width;
    const py = 3 + (1 - (value(p) - lo) / (hi - lo)) * (size.height - 6);
    return `${px.toFixed(1)},${py.toFixed(1)}`;
  });
  svg.append(svgEl("path", { d: `M${coords.join(" L")}`, fill: "none", stroke: color, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }));
  return svg;
}
