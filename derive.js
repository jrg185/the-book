// Tape fill dollars are still a scrubbed fraction times SEEDS_USD.
// SEEDS_USD is loaded from config/book_seeds.json. This file has no seed table.
// book_usd on data/live_book.json is the cash holdings sum.
// running_balance_usd is the agentic total: that cash plus each marked open lot.
// Day kill and day target are fractions of that running balance.
// signal_book_usd is stored beside the account and is not that rail.
// This file never talks to Supabase.

import { SEEDS_USD } from "./book_seeds.js";

export { SEEDS_USD };

// Public signal file. Its book_usd is the signal book, not the published account. No secret.
export const LIVE_SIGNAL_URL =
  "https://raw.githubusercontent.com/jrg185/agentic-crypto-signals/main/signals/latest.json";

// Same rails the crypto sleeve already uses. Dollars are these fractions of the running balance.
export const LIVE_RAILS = {
  dayKillFrac: -0.1,
  dayTargetFrac: 0.025,
};

const SLEEVE_ORDER = ["combined", "crypto", "equities"];

export function num(value) {
  if (value == null || value === "") return null;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  const cleaned = String(value).replace(/[$,%\s,]/g, "").replace(/^\((.*)\)$/, "-$1");
  if (cleaned === "" || cleaned === "-" || cleaned === "—") return null;
  const n = Number(cleaned);
  return Number.isFinite(n) ? n : null;
}

// Rails may arrive as fractions (-0.10) or percent points (-10).
export function asFraction(value) {
  const n = num(value);
  if (n == null) return null;
  return Math.abs(n) > 1 ? n / 100 : n;
}

export function pick(row, keys) {
  if (!row) return null;
  for (const key of keys) {
    if (row[key] != null && row[key] !== "") return row[key];
  }
  return null;
}

export function sleeveKey(row) {
  return String(pick(row, ["sleeve", "book", "desk"]) || "")
    .trim()
    .toLowerCase();
}

export function seedFor(row, seeds) {
  const table = seeds == null ? SEEDS_USD : seeds;
  const explicit = num(pick(row, ["start", "seed", "start_usd", "seed_usd", "book_usd"]));
  if (explicit != null) return explicit;
  const key = sleeveKey(row);
  if (key === "combined") {
    const crypto = table.crypto ?? 0;
    const equities = table.equities ?? 0;
    if (crypto || equities) return crypto + equities;
  }
  return table[key] ?? null;
}

export function money(seed, frac) {
  if (seed == null || frac == null) return null;
  return Math.round((seed * frac + Number.EPSILON) * 100) / 100;
}

// Headroom inside the day-kill rail. dayKillFrac is the signed LIVE_RAILS
// fraction. The budget is that fraction's magnitude times the running balance.
// A negative day P&L spends the budget. A positive day does not add to it.
// The result is never below zero. Export writes the same dollars from the
// same LIVE_RAILS fraction. There is no second copy of the fraction here.
export function killRemainingUsd(runningBalance, dayPnl, dayKillFrac = LIVE_RAILS.dayKillFrac) {
  const balance = num(runningBalance);
  const frac = num(dayKillFrac);
  if (balance == null || frac == null) return null;
  const day = num(dayPnl);
  const spent = day == null ? 0 : Math.min(day, 0);
  return Math.max(0, money(1, Math.abs(frac) * balance + spent));
}

// Keep the committed account holdings, including each value and cost basis.
// book_usd is the sum of those values. The signal book is stored beside it
// and is not copied onto book_usd. It is not the day-kill rail.
// A day P&L or kill headroom already on the file stays. Export writes those
// from the cash drop and the running-balance rail. A blank day still takes
// the signal figure. Realized, unrealized, running P&L, and
// running_balance_usd stay on the file. The card total is cash plus marked
// open lots. Its P&L is that total minus the combined seed.
// Candidates are scores, not broker holdings, so they are never copied in.
// A signal holding list is not the account. It cannot replace cash and USDC.
// Open positions already on the account stay. A signal position list is not the book.
// generated_at is the signal file time. sleeve_as_of is the warehouse MTM clock
// and is not replaced by the signal.
export function mergeLiveBook(committed, signal) {
  const base = committed && typeof committed === "object" ? { ...committed } : {};
  delete base.candidates;
  const sleeveAsOf = pick(committed, ["sleeve_as_of"]);
  const holdings = Array.isArray(committed?.holdings) ? committed.holdings.map((row) => ({ ...row })) : [];
  const next = { ...base, holdings };
  const live = signal && typeof signal === "object" ? signal : null;
  if (live) {
    const signalBook = num(live.book_usd);
    if (signalBook != null) next.signal_book_usd = signalBook;
    if (live.generated_at) next.generated_at = live.generated_at;
    const day = num(live.day_pnl_usd);
    if (day != null && num(next.day_pnl_usd) == null) next.day_pnl_usd = day;
    const kill = num(live.kill_remaining_usd);
    if (kill != null && num(next.kill_remaining_usd) == null) next.kill_remaining_usd = kill;
  }
  if (sleeveAsOf) next.sleeve_as_of = sleeveAsOf;
  else delete next.sleeve_as_of;
  next.holdings = holdings;
  delete next.candidates;
  const account = accountValueUsd(holdingRows(next));
  if (account != null) next.book_usd = account;
  return next;
}

function bookFrac(dollars, bookUsd) {
  if (dollars == null || bookUsd == null || bookUsd === 0) return null;
  return dollars / bookUsd;
}

function roundCents(value) {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

// Account value is the sum of holdings that each carry their own value.
// A missing value stays blank. Signal book_usd is not painted onto that row.
// The card total adds marked open lots on top of this cash. It is not the
// stored running_balance_usd and not the signal book.
function accountValueUsd(rows) {
  if (!rows.length || rows.some((row) => row.valueUsd == null)) return null;
  return roundCents(rows.reduce((sum, row) => sum + row.valueUsd, 0));
}

// Unrealized P&L comes from each holding's value minus its cost basis.
// USD cash has no mark. A holding without a cost basis leaves the total unknown.
function accountUnrealizedUsd(book) {
  const rows = Array.isArray(book?.holdings) ? book.holdings : [];
  const crypto = rows.filter((row) => {
    if (!row || !String(row.ticker || "").trim()) return false;
    return String(row.sleeve || "crypto").trim().toLowerCase() === "crypto";
  });
  if (!crypto.length) return null;
  let sum = 0;
  for (const row of crypto) {
    const ticker = String(row.ticker).trim().toUpperCase();
    if (ticker === "USD") continue;
    const value = num(row.value_usd);
    const cost = num(pick(row, ["cost_basis_usd", "cost_basis"]));
    if (value == null || cost == null) return null;
    sum += value - cost;
  }
  return roundCents(sum);
}

// One crypto book. Equities are $0 and are not a second book.
// book_usd stays the holdings sum (cash). The card running balance is RH
// cash plus each marked open lot, the same total export writes as
// running_balance_usd when a position list is present. A book with no
// position list still shows the stored running_balance_usd.
// Day P&L is the dollar on the file. Export fills it from the cash drop
// when that drop has day_realized_usd. The day kill and the day target are
// fractions of the same total as RUNNING BALANCE. Kill headroom is
// max(0, |dayKillFrac| × that total + min(day P&L, 0)), using LIVE_RAILS.
// When the RH total is known, running P&L is that total minus the combined
// seed. Unrealized is the open lots' mark versus cost. Realized is running
// minus unrealized, so the three match the cash-and-lots book.
// A book with no position list still shows the snapshot dollars. A missing
// unrealized dollar falls back to holding value minus cost basis.
// asOf is the warehouse sleeve clock: sleeve_as_of on the book, or the
// combined kpi_summary as_of when the export has not stamped the book yet.
// generated_at stays signal context and is not this clock.
export function warehouseSleeveAsOf(book, summary) {
  const stamped = pick(book, ["sleeve_as_of"]);
  if (stamped) return stamped;
  const rows = Array.isArray(summary) ? summary : [];
  for (const sleeve of ["combined", "crypto", "equities"]) {
    let best = null;
    let bestMs = -Infinity;
    for (const row of rows) {
      if (sleeveKey(row) !== sleeve || !row?.as_of) continue;
      const ms = Date.parse(row.as_of);
      if (Number.isNaN(ms) || ms < bestMs) continue;
      best = row.as_of;
      bestMs = ms;
    }
    if (best) return best;
  }
  return null;
}

// RH Agentic total: USD and USDC holdings plus each marked crypto open lot.
// USDC stays a cash line, so it is not added again from positions.
// A missing value leaves the total unknown. No position list means this
// book has not published lots, and the stored running balance still shows.
function agenticBookUsd(book) {
  if (!book || !Array.isArray(book.positions)) return null;
  const rows = holdingRows(book).concat(openPositionRows(book).filter((row) => row.sleeve === "crypto"));
  if (!rows.length || rows.some((row) => row.valueUsd == null)) return null;
  return roundCents(rows.reduce((sum, row) => sum + row.valueUsd, 0));
}

function lotUnrealized(row) {
  const direct = num(row?.unrealized_pnl_usd);
  if (direct != null) return direct;
  const value = num(row?.value_usd);
  const qty = num(row?.qty);
  const avg = num(pick(row, ["avg_cost", "avg"]));
  if (value == null || qty == null || avg == null) return null;
  return value - qty * avg;
}

// Open crypto marks versus cost. USD and USDC are cash. A flat name is
// skipped. An open lot with no unrealized figure and no cost leaves the
// total unknown so the card does not invent a split.
function openCryptoUnrealized(book) {
  if (!book || !Array.isArray(book.positions)) return null;
  let sum = 0;
  for (const row of book.positions) {
    if (!row || !String(row.ticker || "").trim()) continue;
    const ticker = String(row.ticker).trim().toUpperCase();
    if (ticker === "USD" || ticker === "USDC") continue;
    const sleeve = String(row.sleeve || "crypto").trim().toLowerCase();
    if (sleeve !== "crypto") continue;
    const qty = num(row.qty);
    if (qty == null || qty === 0) continue;
    const unreal = lotUnrealized(row);
    if (unreal == null) return null;
    sum += unreal;
  }
  return roundCents(sum);
}

// RH book versus the combined seed from config/book_seeds.json.
// running = cash + lots − that seed.
// unrealized = open-lot mark versus cost. realized = running − unrealized.
// Null when this file has not published a position list.
export function reconciledPnl(book) {
  const marked = agenticBookUsd(book);
  const unrealized = openCryptoUnrealized(book);
  const seed = SEEDS_USD.combined;
  if (marked == null || unrealized == null || seed == null) return null;
  const running = roundCents(marked - seed);
  return {
    seed,
    marked,
    unrealized,
    running,
    realized: roundCents(running - unrealized),
  };
}

export function cryptoBookView(book, summary) {
  const account = accountValueUsd(holdingRows(book));
  const published = num(book?.book_usd);
  const bookUsd = account != null ? account : published;
  const signalBook = num(book?.signal_book_usd);
  const dayPnl = num(book?.day_pnl_usd);
  const killStored = num(book?.kill_remaining_usd);
  const marked = agenticBookUsd(book);
  const runningBalance = marked != null ? marked : num(pick(book, ["running_balance_usd", "running_balance"]));
  // Same book as RUNNING BALANCE. A holdings sum with no live total and no
  // stored signal book must not become the rail.
  const rail =
    runningBalance != null && runningBalance !== 0
      ? runningBalance
      : signalBook != null
        ? signalBook
        : account == null
          ? published
          : null;
  if (bookUsd == null && rail == null) return null;
  // Same headroom export writes on kill_remaining_usd: the day-kill budget
  // on the running balance, reduced by a negative day, floored at zero.
  const killHeadroom = runningBalance != null ? killRemainingUsd(runningBalance, dayPnl) : killStored;
  const reconciled = reconciledPnl(book);
  let realizedPnl = num(pick(book, ["realized_pnl_usd", "realized_pnl"]));
  let unrealizedPnl = num(pick(book, ["unrealized_pnl_usd", "unrealized_pnl"]));
  if (unrealizedPnl == null) unrealizedPnl = accountUnrealizedUsd(book);
  let runningPnl = num(pick(book, ["running_pnl_usd", "running_pnl"]));
  if (runningPnl == null && realizedPnl != null && unrealizedPnl != null) {
    runningPnl = roundCents(realizedPnl + unrealizedPnl);
  }
  if (reconciled) {
    realizedPnl = reconciled.realized;
    unrealizedPnl = reconciled.unrealized;
    runningPnl = reconciled.running;
  }
  const pctBook = reconciled ? reconciled.marked : bookUsd;
  return {
    sleeve: "crypto",
    label: "Crypto",
    bookUsd,
    runningBalance,
    equitiesUsd: 0,
    asOf: warehouseSleeveAsOf(book, summary),
    signalGeneratedAt: book?.generated_at || null,
    dayPnl,
    dayKillFrac: LIVE_RAILS.dayKillFrac,
    dayKill: money(rail, LIVE_RAILS.dayKillFrac),
    dayTargetFrac: LIVE_RAILS.dayTargetFrac,
    dayTarget: money(rail, LIVE_RAILS.dayTargetFrac),
    killHeadroom,
    killHeadroomFrac: rail == null || rail === 0 || killHeadroom == null ? null : killHeadroom / rail,
    realizedPnl,
    realizedPnlFrac: bookFrac(realizedPnl, pctBook),
    unrealizedPnl,
    unrealizedPnlFrac: bookFrac(unrealizedPnl, pctBook),
    runningPnl,
    runningPnlFrac: bookFrac(runningPnl, pctBook),
  };
}

export function shownBooks(book, summary) {
  const crypto = cryptoBookView(book, summary);
  return crypto ? [crypto] : [];
}

// Names and values come from the live book holdings. A missing value stays
// blank. The signal book is not painted onto a cash row. Tape tickers are
// not added here. Open nets live on book.positions and are not part of this sum.
export function holdingRows(book) {
  const rows = Array.isArray(book?.holdings) ? book.holdings : [];
  const named = rows.filter((row) => {
    if (!row || !String(row.ticker || "").trim()) return false;
    const sleeve = String(row.sleeve || "crypto").trim().toLowerCase();
    return sleeve === "crypto";
  });
  return named.map((row) => ({
    sleeve: "crypto",
    ticker: String(row.ticker).trim(),
    valueUsd: num(row.value_usd),
  }));
}

// Open size is the net the export wrote from kpi_trades. A zero net is flat
// and stays off the list. USD and USDC stay on the cash lines, not here.
// value_usd is qty times the snapshot mark. A missing mark stays blank.
export function openPositionRows(book) {
  const rows = Array.isArray(book?.positions) ? book.positions : [];
  const opens = [];
  for (const row of rows) {
    if (!row || !String(row.ticker || "").trim()) continue;
    const ticker = String(row.ticker).trim().toUpperCase();
    if (ticker === "USD" || ticker === "USDC") continue;
    const sleeve = String(row.sleeve || "crypto").trim().toLowerCase();
    if (sleeve !== "crypto" && sleeve !== "equities") continue;
    const qty = num(row.qty);
    if (qty == null || qty === 0) continue;
    opens.push({
      sleeve,
      ticker,
      qty,
      valueUsd: num(row.value_usd),
      runningPnl: num(row.running_pnl_usd),
    });
  }
  return opens;
}

// Cash lines first, then each open net. Cash has no running P&L.
// Position value is not the cash sum, and it is not the sleeve running P&L.
export function cardPositionRows(book) {
  const cash = holdingRows(book).map((row) => ({ ...row, qty: null, runningPnl: null }));
  return cash.concat(openPositionRows(book));
}

function fractionFrom(row, fracKeys, dollarKeys, seed, { percentPoints = false } = {}) {
  const rawFrac = pick(row, fracKeys);
  if (rawFrac != null && rawFrac !== "") return percentPoints ? asFraction(rawFrac) : num(rawFrac);
  const dollars = num(pick(row, dollarKeys));
  if (dollars == null || seed == null || seed === 0) return null;
  return dollars / seed;
}

export function deriveSleeve(row, seeds) {
  const table = seeds == null ? SEEDS_USD : seeds;
  const seed = seedFor(row, table);
  // Book / start can be above 1. Do not treat that as percent points.
  const runningBalanceFrac = fractionFrom(
    row,
    ["running_balance_frac", "running_bal_vs_start", "balance_frac", "bal_frac", "running_bal_frac"],
    ["running_balance", "running_balance_usd", "balance_usd"],
    seed
  );
  const runningPnlFrac = fractionFrom(
    row,
    ["running_pnl_frac", "pnl_pct_of_book", "pnl_frac", "running_pnl_pct"],
    ["running_pnl_usd", "pnl_usd", "running_pnl"],
    seed,
    { percentPoints: true }
  );
  // Named _frac fields stay fractions even above 1. Cards label these apart from running P&L.
  const realizedPnlFrac = fractionFrom(
    row,
    ["realized_pnl_frac"],
    ["realized_pnl_usd", "realized_pnl"],
    seed
  );
  const unrealizedPnlFrac = fractionFrom(
    row,
    ["unrealized_pnl_frac"],
    ["unrealized_pnl_usd", "unrealized_pnl"],
    seed
  );
  const dayPnlFrac = fractionFrom(
    row,
    ["day_pnl_frac", "day_pnl_pct"],
    ["day_pnl_usd", "day_pnl"],
    seed,
    { percentPoints: true }
  );
  const dayKillFrac = fractionFrom(
    row,
    ["day_kill_pct", "kill_pct", "day_kill_frac"],
    ["day_kill_usd", "day_kill_dollars", "day_kill"],
    seed,
    { percentPoints: true }
  );
  const killHeadroomFrac = fractionFrom(
    row,
    ["kill_headroom_frac", "headroom_frac", "kill_headroom_pct"],
    ["kill_headroom_usd", "kill_headroom_dollars", "kill_headroom"],
    seed,
    { percentPoints: true }
  );
  const dayTargetFrac = fractionFrom(
    row,
    ["day_target_pct", "target_pct", "day_target_frac"],
    ["day_target_usd", "day_target_dollars", "day_target"],
    seed,
    { percentPoints: true }
  );

  return {
    sleeve: sleeveKey(row),
    label: labelFor(sleeveKey(row)),
    asOf: pick(row, ["as_of", "asof", "snapshot_at", "updated_at"]),
    note: pick(row, ["note", "notes", "day_target_note"]),
    seed,
    runningBalanceFrac,
    runningPnlFrac,
    realizedPnlFrac,
    unrealizedPnlFrac,
    dayPnlFrac,
    dayKillFrac,
    killHeadroomFrac,
    dayTargetFrac,
    runningBalance: money(seed, runningBalanceFrac),
    runningPnl: money(seed, runningPnlFrac),
    realizedPnl: money(seed, realizedPnlFrac),
    unrealizedPnl: money(seed, unrealizedPnlFrac),
    dayPnl: money(seed, dayPnlFrac),
    dayKill: money(seed, dayKillFrac),
    killHeadroom: money(seed, killHeadroomFrac),
    dayTarget: money(seed, dayTargetFrac),
  };
}

export function labelFor(sleeve) {
  if (sleeve === "crypto") return "Crypto";
  if (sleeve === "equities") return "Equities";
  if (sleeve === "combined") return "Combined";
  return sleeve ? sleeve.charAt(0).toUpperCase() + sleeve.slice(1) : "Book";
}

export function sortSleeves(rows, seeds) {
  const derived = rows.map((row) => deriveSleeve(row, seeds));
  return derived.sort((a, b) => {
    const ai = SLEEVE_ORDER.indexOf(a.sleeve);
    const bi = SLEEVE_ORDER.indexOf(b.sleeve);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}

export function formatUsd(value, { signed = false } = {}) {
  if (value == null || Number.isNaN(value)) return "—";
  const abs = Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  if (value < 0) return `-$${abs}`;
  if (signed && value > 0) return `+$${abs}`;
  return `$${abs}`;
}

export function formatPct(frac, { signed = false, digits = 1 } = {}) {
  if (frac == null || Number.isNaN(frac)) return "—";
  const pct = frac * 100;
  const body = `${Math.abs(pct).toFixed(digits)}%`;
  if (pct < 0) return `-${body}`;
  if (signed && pct > 0) return `+${body}`;
  return body;
}

// Width of the kill-headroom meter, as a percent of the track.
// Matches the stated headroom fraction of book (the percent on the card).
// Not rescaled by the day-kill rail, and not inverted into kill already used.
export function headroomFill(derived) {
  if (derived.killHeadroomFrac == null || Number.isNaN(derived.killHeadroomFrac)) return null;
  return Math.max(0, Math.min(100, derived.killHeadroomFrac * 100));
}

export function tone(value) {
  if (value == null || value === 0) return "flat";
  return value > 0 ? "up" : "down";
}

function exitPnlFrac(trade) {
  if (!trade) return null;
  const primary = num(trade.pnl_frac_of_book);
  if (primary != null) return primary;
  return num(trade.pnl_frac);
}

// Closed exits only: sell fills with a finite pnl fraction.
// Flat (0) exits are excluded from wins, losses, and the denominator.
// Combined is crypto sells plus equities sells, not a third tape.
export function winStats(trades, sleeve) {
  const rows = Array.isArray(trades) ? trades : [];
  const wanted = sleeve === "combined" ? ["crypto", "equities"] : [sleeve];
  let wins = 0;
  let losses = 0;
  for (const trade of rows) {
    if (!wanted.includes(sleeveKey(trade))) continue;
    if (String(trade?.side || "").trim().toLowerCase() !== "sell") continue;
    const frac = exitPnlFrac(trade);
    if (frac == null || frac === 0) continue;
    if (frac > 0) wins += 1;
    else losses += 1;
  }
  const decided = wins + losses;
  return {
    wins,
    losses,
    rate: decided === 0 ? null : wins / decided,
  };
}

export function formatWinPct(rate) {
  return formatPct(rate, { digits: 0 });
}

export function formatWinRecord(stats) {
  const wins = stats?.wins || 0;
  const losses = stats?.losses || 0;
  return `${wins}\u2013${losses}`;
}

export function winTone(stats) {
  if (!stats || stats.rate == null) return tone(null);
  if (stats.wins === stats.losses) return tone(0);
  return tone(stats.wins - stats.losses);
}

const OOS_MODEL_ORDER = ["rules", "logistic", "lgbm"];
const FEE_KEYS = ["fee_usd", "fee", "fee_charged"];

function finiteFrac(value) {
  return num(value);
}

// Same exit rule as winStats. order id collapses duplicate sells when the
// field is actually on the row. The scrubbed Pages tape does not carry it.
export function closedFillStats(trades, sleeve, seed) {
  const bookSeed = seed == null ? SEEDS_USD.crypto : seed;
  const rows = Array.isArray(trades) ? trades : [];
  const wanted = sleeve === "combined" ? ["crypto", "equities"] : [sleeve];
  const seen = new Set();
  let wins = 0;
  let losses = 0;
  let flats = 0;
  let deduped = 0;
  let orderIdAvailable = false;
  let sum = 0;
  for (const trade of rows) {
    if (!wanted.includes(sleeveKey(trade))) continue;
    if (String(trade?.side || "").trim().toLowerCase() !== "sell") continue;
    const frac = exitPnlFrac(trade);
    if (frac == null) continue;
    const orderId = String(trade?.order_id || "").trim();
    if (orderId) {
      orderIdAvailable = true;
      if (seen.has(orderId)) {
        deduped += 1;
        continue;
      }
      seen.add(orderId);
    }
    if (frac === 0) {
      flats += 1;
      continue;
    }
    if (frac > 0) wins += 1;
    else losses += 1;
    sum += frac;
  }
  const decided = wins + losses;
  const expectancyFrac = decided === 0 ? null : sum / decided;
  return {
    wins,
    losses,
    flats,
    decided,
    rate: decided === 0 ? null : wins / decided,
    expectancyFrac,
    expectancyUsd: money(bookSeed, expectancyFrac),
    seed: bookSeed,
    deduped,
    orderIdAvailable,
  };
}

function feeAmount(row, seed) {
  for (const key of FEE_KEYS) {
    if (row && Object.prototype.hasOwnProperty.call(row, key)) {
      const amount = finiteFrac(row[key]);
      if (amount != null) return amount;
    }
  }
  if (row && Object.prototype.hasOwnProperty.call(row, "fee_frac_of_book") && seed) {
    const frac = finiteFrac(row.fee_frac_of_book);
    if (frac != null) return frac * seed;
  }
  return null;
}

export function feeDragFromTrades(trades, sleeve = "crypto", seed) {
  const bookSeed = seed == null ? SEEDS_USD.crypto : seed;
  const rows = Array.isArray(trades) ? trades : [];
  const seen = new Set();
  let sawField = false;
  let numeric = false;
  let total = 0;
  let sellTotal = 0;
  let n = 0;
  for (const trade of rows) {
    if (sleeveKey(trade) !== sleeve) continue;
    const hasField =
      FEE_KEYS.some((key) => trade && Object.prototype.hasOwnProperty.call(trade, key)) ||
      Object.prototype.hasOwnProperty.call(trade || {}, "fee_frac_of_book");
    if (!hasField) continue;
    sawField = true;
    const orderId = String(trade?.order_id || "").trim();
    if (orderId) {
      if (seen.has(orderId)) continue;
      seen.add(orderId);
    }
    const amount = feeAmount(trade, bookSeed);
    if (amount == null) continue;
    numeric = true;
    total += amount;
    n += 1;
    if (String(trade?.side || "").trim().toLowerCase() === "sell") sellTotal += amount;
  }
  if (!sawField || !numeric) return null;
  const roundUsd = (value) => Math.round((value + Number.EPSILON) * 100) / 100;
  const feeUsd = roundUsd(total);
  const sellFeeUsd = roundUsd(sellTotal);
  return {
    status: "known",
    fee_usd: feeUsd,
    sell_fee_usd: sellFeeUsd,
    fee_frac: bookSeed ? feeUsd / bookSeed : null,
    n,
    seed_usd: bookSeed,
    note: "Sum of fee dollars on the crypto rows in this snapshot.",
  };
}

export function inferLiveBackend(models) {
  const list = Array.isArray(models?.models) ? models.models : [];
  const crypto = list.find((model) => String(model?.sleeve || "").trim().toLowerCase() === "crypto") || {};
  const oosNote = crypto.oos && typeof crypto.oos === "object" ? crypto.oos.note : "";
  const text = [models?.note, crypto.used, crypto.training, oosNote].filter(Boolean).join("\n");
  const rules = text.includes("--backend rules");
  return {
    id: rules ? "rules" : null,
    cli: rules ? "--backend rules" : null,
    note: rules
      ? "The CLI is still --backend rules."
      : "Live backend is not stated in data/models.json. Not inferred.",
  };
}

export function cryptoOosModels(payload) {
  const rows = Array.isArray(payload) ? payload : Array.isArray(payload?.rows) ? payload.rows : [];
  const crypto = rows.filter((row) => {
    const sleeve = String(row?.sleeve || row?.asset_class || "").trim().toLowerCase();
    return sleeve === "crypto";
  });
  return crypto.slice().sort((a, b) => {
    const ai = OOS_MODEL_ORDER.indexOf(String(a?.model || "").toLowerCase());
    const bi = OOS_MODEL_ORDER.indexOf(String(b?.model || "").toLowerCase());
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });
}
