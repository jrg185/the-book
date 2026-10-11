// Render og.png from data/*.json. Used by .github/workflows/og-card.yml.
//   node scripts/render_og.mjs            writes og.png (needs Chrome)
//   node scripts/render_og.mjs --svg PATH writes only the SVG
import { execFileSync, spawn } from "node:child_process";
import { copyFileSync, existsSync, mkdtempSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { OG_HEIGHT, OG_WIDTH, buildOgSvg, ogSnapshot } from "../og.js";

const root = fileURLToPath(new URL("..", import.meta.url));

function read(name, fallback) {
  const path = join(root, "data", name);
  if (!existsSync(path)) return fallback;
  return JSON.parse(readFileSync(path, "utf8"));
}

function chromeBinary() {
  const candidates = [process.env.CHROME_BIN, "google-chrome-stable", "google-chrome", "chromium", "chromium-browser"];
  for (const bin of candidates.filter(Boolean)) {
    try {
      execFileSync(bin, ["--version"], { stdio: "ignore" });
      return bin;
    } catch {
      /* try the next one */
    }
  }
  throw new Error("Chrome was not found. Set CHROME_BIN.");
}

const snap = ogSnapshot({
  book: read("live_book.json", null),
  summary: read("kpi_summary.json", []),
  trades: read("kpi_trades_scrubbed.json", []),
  curves: read("sleeve_curves.json", { series: [] }),
  scorecard: read("model_scorecard.json", null),
  meta: read("meta.json", {}),
});
const svg = buildOgSvg(snap);

const svgFlag = process.argv.indexOf("--svg");
if (svgFlag >= 0) {
  writeFileSync(process.argv[svgFlag + 1] || join(root, "og.svg"), svg);
  process.exit(0);
}

const dir = mkdtempSync(join(tmpdir(), "og-"));
const svgPath = join(dir, "og.svg");
writeFileSync(svgPath, svg);
const shot = join(dir, "og.png");
const out = join(root, "og.png");

// Headless Chrome can linger after it writes the screenshot. Wait for a
// stable file, then stop the process.
const child = spawn(
  chromeBinary(),
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--hide-scrollbars",
    "--no-first-run",
    "--force-device-scale-factor=1",
    `--user-data-dir=${join(dir, "profile")}`,
    `--window-size=${OG_WIDTH},${OG_HEIGHT}`,
    `--screenshot=${shot}`,
    `file://${svgPath}`,
  ],
  { stdio: "ignore" }
);
let exited = false;
child.on("exit", () => {
  exited = true;
});
const deadline = Date.now() + 90_000;
let lastSize = -1;
while (Date.now() < deadline) {
  await new Promise((resolve) => setTimeout(resolve, 500));
  const size = existsSync(shot) ? statSync(shot).size : 0;
  if (size > 0 && size === lastSize) break;
  lastSize = size;
  if (exited && size === 0) break;
}
if (!exited) child.kill("SIGKILL");
if (!existsSync(shot) || statSync(shot).size === 0) {
  console.error("Chrome did not write og.png");
  process.exit(1);
}
copyFileSync(shot, out);
console.log(`wrote ${out}`);
