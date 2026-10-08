// Load config/book_seeds.json. That file is the only seed source.
import seedConfig from "./config/book_seeds.json" with { type: "json" };

function isIsoDate(value) {
  return /^\d{4}-\d{2}-\d{2}$/.test(value);
}

export function seedRows(config = seedConfig) {
  const rows = Array.isArray(config) ? config : config?.seeds;
  if (!Array.isArray(rows) || rows.length === 0) {
    throw new Error("config/book_seeds.json needs a non-empty seeds list");
  }
  const seen = new Set();
  const parsed = rows.map((row) => {
    const sleeve = String(row?.sleeve || "").trim().toLowerCase();
    const seed = Number(row?.seed_usd);
    const effective = String(row?.effective_from || "");
    if (!sleeve || !Number.isFinite(seed) || seed <= 0 || !isIsoDate(effective)) {
      throw new Error("book seed row is incomplete");
    }
    const key = `${sleeve}|${effective}`;
    if (seen.has(key)) throw new Error(`duplicate book seed ${sleeve} ${effective}`);
    seen.add(key);
    return { sleeve, seed_usd: seed, effective_from: effective };
  });
  parsed.sort((a, b) =>
    a.sleeve === b.sleeve
      ? a.effective_from < b.effective_from
        ? -1
        : a.effective_from > b.effective_from
          ? 1
          : 0
      : a.sleeve < b.sleeve
        ? -1
        : 1
  );
  return parsed;
}

// `on` is YYYY-MM-DD. Omit it to take the latest row per sleeve.
export function currentSeeds(config = seedConfig, on = null) {
  const chosen = new Map();
  for (const row of seedRows(config)) {
    if (on != null && row.effective_from > on) continue;
    const prev = chosen.get(row.sleeve);
    if (!prev || row.effective_from >= prev.effective_from) chosen.set(row.sleeve, row);
  }
  if (chosen.size === 0) throw new Error("no book seed is effective");
  const out = {};
  for (const [sleeve, row] of chosen) out[sleeve] = row.seed_usd;
  return out;
}

export const SEEDS_USD = currentSeeds();
