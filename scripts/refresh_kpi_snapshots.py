#!/usr/bin/env python3
"""Insert a fresh public.kpi_sleeve_snapshots row per sleeve.

public.kpi_summary is a view over the latest snapshot, and scripts/export_kpi.py
only SELECTs that view. Re-export cannot move as_of. This script reads fills from
public.kpi_trades (qty and price), marks what is still open, and INSERTs.

Seeds come from config/book_seeds.json.
Crypto and equities sleeve rows stay seed-anchored so the old sleeve curves
still have a start. They are not the account. The combined row is the
account in scripts/account_book.py: cash plus open crypto lots, one mark
set. Running P&L is that balance minus the combined seed. Equities lots
stay on the equities sleeve row and are not in this balance.
Unrealized keeps the Robinhood basis (buy fee stays out of average cost).
Realized is running P&L minus unrealized, so buy fees land there through
cash. A failed Robinhood cash read falls back to the desk drop. When cash
is still missing, the previous combined dollars and the previous combined
as_of stay. It does not insert a new combined clock, and it does not fall
back to seed + trade P&L + open mark-to-market.
Per-trade pnl_trade_usd is still the close leg only. avg_cost is the open
cost that matches those stored close dollars. A FIFO close leaves the
oldest lots. A close still stored at the older average cost leaves that
blend on the sleeve row.

Do not read public.kpi_trades_scrubbed. That view has no qty or price and joins
the latest snapshot, so it cannot refresh mark-to-market.

Credentials (same as export):
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_DB_URL            optional Postgres URL when REST cannot read or insert

Quote order, first success wins. An open ticker with no mark exits 1 and does
not INSERT.

crypto:
  1. Coinbase Exchange public ticker (no key). MNT uses product MANTLE-USD.
  2. Yahoo chart {SYMBOL}-USD regularMarketPrice (no key).
  3. CoinStats /coins when COINSTATS_API_KEY is set.
equities:
  1. Finnhub /quote when FINNHUB_API_KEY is set.
  2. Yahoo chart {SYMBOL} regularMarketPrice (no key).
  3. Alpha Vantage GLOBAL_QUOTE when ALPHA_VANTAGE_API_KEY is set.

Public Coinbase and Yahoo are enough for the current book. A missing optional
key is not an error when a public mark exists.

  python3 scripts/refresh_kpi_snapshots.py --self-test
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \\
    python3 scripts/refresh_kpi_snapshots.py --dry-run
  python3 scripts/refresh_kpi_snapshots.py

--dry-run prints the rows and does not INSERT. A live run that cannot insert,
including read-only transaction 25006 or a full disk, exits non-zero and leaves
the previous snapshot in place.

After a live run:

  select sleeve, as_of, realized_pnl_usd, unrealized_pnl_usd,
         running_pnl_usd, running_balance_usd, start_balance_usd
  from public.kpi_sleeve_snapshots
  order by as_of desc
  limit 6;
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from book_seeds import current_seeds

DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
SLEEVES = ("crypto", "equities", "combined")
TRADE_SLEEVES = ("crypto", "equities")
START = current_seeds()
DUST = Decimal("0.00000001")
# fifo_gross - stored within this of fee_usd means that fee is already in the
# stored close, so the leftover stays on the FIFO lots.
BASIS_TOL = Decimal("0.001")
SYMBOL = re.compile(r"^[A-Z0-9]{1,15}$")
COINBASE_PRODUCT = {"MNT": "MANTLE-USD"}
WRITE_COLUMNS = (
    "sleeve",
    "as_of",
    "realized_pnl_usd",
    "unrealized_pnl_usd",
    "running_pnl_usd",
    "running_balance_usd",
    "start_balance_usd",
    "day_kill_pct",
    "day_target_pct",
    "notes",
)
USER_AGENT = "the-book-kpi-refresh/1"
FROZEN_PREFIX = "2026-09-27T23:48"
ET = ZoneInfo("America/New_York")


class RefreshError(RuntimeError):
    """A refresh that must not exit 0."""


class QuoteMiss(Exception):
    """One quote source did not return a usable mark."""


def q6(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))


def dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Decimal) -> str:
    quantized = value.quantize(Decimal("0.01"))
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):.2f}"


def num_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def parse_ts(value) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        if " " in text and "T" not in text:
            text = text.replace(" ", "T", 1)
        parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(dt.timezone.utc)


def iso_z(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def redact(text: str, secrets: list[str]) -> str:
    clean = text
    for secret in secrets:
        if secret:
            clean = clean.replace(secret, "[redacted]")
    return clean[:400]


def loads(raw: bytes | str):
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    return json.loads(text, parse_float=Decimal, parse_int=Decimal)


def http_json(url: str, headers: dict, timeout: int = 20, retries: int = 3):
    request = urllib.request.Request(url, headers=headers)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return loads(response.read())
        except json.JSONDecodeError:
            raise QuoteMiss("non-json") from None
        except urllib.error.HTTPError as exc:
            exc.read()
            last = exc
            if exc.code == 404:
                raise QuoteMiss(f"HTTP {exc.code}") from None
            if exc.code in {429, 500, 502, 503, 504} and attempt < retries - 1:
                time.sleep(1.1 * (attempt + 1))
                continue
            raise QuoteMiss(f"HTTP {exc.code}") from None
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(1.1 * (attempt + 1))
                continue
            raise QuoteMiss("network error") from None
    raise QuoteMiss(f"request failed: {last}")


def positive_price(payload_price) -> Decimal:
    price = dec(payload_price)
    if price is None or price <= 0:
        raise QuoteMiss("non-positive price")
    return price


def coinbase_price(ticker: str) -> Decimal:
    product = COINBASE_PRODUCT.get(ticker, f"{ticker}-USD")
    url = "https://api.exchange.coinbase.com/products/" + urllib.parse.quote(product) + "/ticker"
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    if not isinstance(payload, dict):
        raise QuoteMiss("coinbase payload")
    return positive_price(payload.get("price"))


def yahoo_price(symbol: str) -> Decimal:
    query = urllib.parse.urlencode({"interval": "1m", "range": "1d"})
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol) + "?" + query
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict) or chart.get("error"):
        raise QuoteMiss("yahoo chart error")
    results = chart.get("result") or []
    if not results:
        raise QuoteMiss("yahoo empty chart")
    meta = results[0].get("meta") if isinstance(results[0], dict) else None
    if not isinstance(meta, dict):
        raise QuoteMiss("yahoo missing meta")
    return positive_price(meta.get("regularMarketPrice"))


def coinstats_price(ticker: str, key: str) -> Decimal:
    query = urllib.parse.urlencode({"currency": "USD", "symbol": ticker})
    url = "https://openapiv1.coinstats.app/coins?" + query
    payload = http_json(
        url,
        {"User-Agent": USER_AGENT, "Accept": "application/json", "X-API-KEY": key},
    )
    rows = []
    if isinstance(payload, dict):
        rows = payload.get("result") or payload.get("coins") or []
    elif isinstance(payload, list):
        rows = payload
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        if symbol == ticker:
            return positive_price(row.get("price"))
    raise QuoteMiss("coinstats miss")


def finnhub_price(ticker: str, key: str) -> Decimal:
    query = urllib.parse.urlencode({"symbol": ticker, "token": key})
    url = "https://finnhub.io/api/v1/quote?" + query
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    if not isinstance(payload, dict) or payload.get("error"):
        raise QuoteMiss("finnhub error")
    current = payload.get("c")
    if current in (None, 0, Decimal(0)):
        raise QuoteMiss("finnhub empty quote")
    return positive_price(current)


def alphavantage_price(ticker: str, key: str) -> Decimal:
    query = urllib.parse.urlencode(
        {"function": "GLOBAL_QUOTE", "symbol": ticker, "apikey": key}
    )
    url = "https://www.alphavantage.co/query?" + query
    payload = http_json(url, {"User-Agent": USER_AGENT, "Accept": "application/json"})
    if not isinstance(payload, dict):
        raise QuoteMiss("alphavantage payload")
    if payload.get("Note") or payload.get("Information"):
        raise QuoteMiss("alphavantage throttled")
    quote = payload.get("Global Quote") or {}
    if not isinstance(quote, dict):
        raise QuoteMiss("alphavantage quote")
    return positive_price(quote.get("05. price"))


def mark_for(sleeve: str, ticker: str, env: dict[str, str]) -> tuple[Decimal, str]:
    if not SYMBOL.fullmatch(ticker):
        raise RefreshError(f"open ticker {ticker!r} is not a mark symbol")
    attempts: list[tuple[str, callable]] = []
    if sleeve == "crypto":
        attempts.append(("coinbase:ticker", lambda: coinbase_price(ticker)))
        attempts.append(("yahoo:chart", lambda: yahoo_price(f"{ticker}-USD")))
        if env.get("COINSTATS_API_KEY"):
            attempts.append(("coinstats:coins", lambda: coinstats_price(ticker, env["COINSTATS_API_KEY"])))
    elif sleeve == "equities":
        if env.get("FINNHUB_API_KEY"):
            attempts.append(("finnhub:quote", lambda: finnhub_price(ticker, env["FINNHUB_API_KEY"])))
        attempts.append(("yahoo:chart", lambda: yahoo_price(ticker)))
        if env.get("ALPHA_VANTAGE_API_KEY"):
            attempts.append(
                ("alphavantage:global_quote", lambda: alphavantage_price(ticker, env["ALPHA_VANTAGE_API_KEY"]))
            )
    else:
        raise RefreshError(f"no quote path for sleeve {sleeve}")
    tried = []
    for source, fetch in attempts:
        try:
            price = fetch()
        except QuoteMiss as exc:
            tried.append(f"{source} ({exc})")
            continue
        return price, source
    detail = "; ".join(tried) if tried else "no quote source configured"
    raise RefreshError(f"{sleeve} {ticker}: no mark ({detail})")


def _order_id(fill: dict) -> str:
    return str(fill.get("order_id") or "").strip()


def _has_fraction(value) -> bool:
    return bool(re.search(r"\d\.\d", str(value or "")))


def _same_qty(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= DUST


def _same_price(left: Decimal | None, right: Decimal | None) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    diff = abs(left - right)
    if diff <= DUST:
        return True
    scale = max(abs(left), abs(right))
    return scale > 0 and diff / scale <= Decimal("0.000001")


def _prefer_fill(left: dict, right: dict) -> dict:
    """Keep one copy. The sub-second timestamp is the later, fuller ingest."""
    left_frac = _has_fraction(left.get("timestamp_et"))
    right_frac = _has_fraction(right.get("timestamp_et"))
    if left_frac and not right_frac:
        primary, secondary = left, right
    elif right_frac and not left_frac:
        primary, secondary = right, left
    else:
        left_pnl = dec(left.get("pnl_trade_usd")) or Decimal("0")
        right_pnl = dec(right.get("pnl_trade_usd")) or Decimal("0")
        if right_pnl != 0 and left_pnl == 0:
            primary, secondary = right, left
        elif _order_id(right) and not _order_id(left):
            primary, secondary = right, left
        else:
            primary, secondary = left, right
    row = dict(primary)
    if not _order_id(row) and _order_id(secondary):
        row["order_id"] = secondary.get("order_id")
    kept_pnl = dec(row.get("pnl_trade_usd")) or Decimal("0")
    other_pnl = dec(secondary.get("pnl_trade_usd")) or Decimal("0")
    if kept_pnl == 0 and other_pnl != 0:
        row["pnl_trade_usd"] = secondary.get("pnl_trade_usd")
    return row


def _bucket_key(fill: dict):
    sleeve = str(fill.get("sleeve") or "").strip().lower()
    ticker = str(fill.get("ticker") or "").strip().upper()
    side = str(fill.get("side") or "").strip().lower()
    qty = dec(fill.get("qty"))
    price = dec(fill.get("avg_price"))
    if qty is None or not sleeve or not ticker or side not in {"buy", "sell"}:
        return None
    try:
        moment = parse_ts(fill["timestamp_et"]).replace(microsecond=0)
    except (KeyError, TypeError, ValueError):
        return None
    quantum = Decimal("0.00000001")
    price_key = "" if price is None else format(price.quantize(quantum), "f")
    return (sleeve, ticker, side, format(qty.quantize(quantum), "f"), price_key, moment.isoformat())


def _legacy_twin_ids(fills: list[dict]) -> set[int]:
    """Object ids of null order_id rows the sync does not treat as lots.

    Same rule as sync_rh_kpi_trades.legacy_duplicate_ids: a null order_id
    beside an order_id twin, same sleeve, ticker, side, qty, and price,
    timestamp within a couple of seconds. Whole-second stamps a second apart
    are twins. Two fractional fills are not.
    """
    import sync_rh_kpi_trades as sync

    return sync.legacy_duplicate_ids(fills)


def collapse_duplicate_fills(fills: list[dict]) -> tuple[list[dict], int]:
    """Drop a second copy of the same fill before the net is replayed.

    Three warehouse shapes count as one fill:

    - the same non-empty order_id
    - one timestamp at a whole second and another in that same second with a
      fraction, with the same sleeve, ticker, side, qty, and price
    - a null order_id row within a couple of seconds of an order_id twin

    A later exit then closes the name. Two different fills that both carry a
    fractional timestamp stay, including two buys of the same size 30 seconds
    apart. This does not delete kpi_trades.
    """
    rows = [fill for fill in fills if isinstance(fill, dict)]
    skipped = _legacy_twin_ids(rows)
    if skipped:
        rows = [fill for fill in rows if id(fill) not in skipped]
    dropped = len(skipped)
    by_order: dict[str, int] = {}
    stage: list[dict] = []
    for fill in rows:
        oid = _order_id(fill)
        if oid and oid in by_order:
            index = by_order[oid]
            stage[index] = _prefer_fill(stage[index], fill)
            dropped += 1
            continue
        if oid:
            by_order[oid] = len(stage)
        stage.append(fill)

    chosen = [True] * len(stage)
    buckets: dict[tuple, list[int]] = {}
    for index, fill in enumerate(stage):
        key = _bucket_key(fill)
        if key is None:
            continue
        mates = buckets.setdefault(key, [])
        fractional = _has_fraction(fill.get("timestamp_et"))
        partner = None
        for other in mates:
            if not chosen[other]:
                continue
            other_fractional = _has_fraction(stage[other].get("timestamp_et"))
            if fractional == other_fractional:
                continue
            if not _same_qty(dec(fill.get("qty")), dec(stage[other].get("qty"))):
                continue
            if not _same_price(dec(fill.get("avg_price")), dec(stage[other].get("avg_price"))):
                continue
            partner = other
            break
        if partner is None:
            mates.append(index)
            continue
        if fractional:
            stage[index] = _prefer_fill(fill, stage[partner])
            chosen[partner] = False
            mates.append(index)
        else:
            stage[partner] = _prefer_fill(stage[partner], fill)
            chosen[index] = False
        dropped += 1
    return [fill for fill, keep in zip(stage, chosen) if keep], dropped


def open_lots_note(collapsed: int, hidden: list[str], open_names: list[str]) -> str:
    bits = []
    if collapsed:
        bits.append(
            f"Collapsed {collapsed} duplicate kpi_trades rows "
            "(same broker order, a whole-second copy of the same qty and price, "
            "or a null order_id twin)."
        )
    if hidden:
        bits.append("Duplicate opens had hidden these exits: " + ", ".join(hidden) + ".")
    if open_names:
        bits.append(
            "Still open in kpi_trades: "
            + ", ".join(open_names)
            + ". A name Robinhood has already sold is a missing exit in kpi_trades."
        )
    else:
        bits.append("No open lots in kpi_trades.")
    return " ".join(bits)


def _open_lots(lots: list[dict] | None) -> tuple[list[dict], Decimal]:
    kept = [lot for lot in (lots or []) if abs(lot["qty"]) > DUST]
    qty = Decimal("0")
    for lot in kept:
        qty += lot["qty"]
    return kept, qty


def _lots_position(lots: list[dict], cost: Decimal | None = None) -> dict | None:
    """Signed qty and open cost. The default cost is the remaining lot prices."""
    kept, qty = _open_lots(lots)
    if abs(qty) <= DUST:
        return None
    if cost is None:
        cost = Decimal("0")
        for lot in kept:
            cost += abs(lot["qty"]) * lot["px"]
    return {"qty": qty, "avg": cost / abs(qty)}


def _replay_fills(fills: list[dict]) -> tuple[dict[str, Decimal], dict[tuple[str, str], dict]]:
    realized = {sleeve: Decimal("0") for sleeve in TRADE_SLEEVES}
    realized_by_ticker: dict[tuple[str, str], Decimal] = {}
    book: dict[tuple[str, str], list] = {}
    cost_basis: dict[tuple[str, str], Decimal] = {}
    ordered = sorted(enumerate(fills), key=lambda item: (parse_ts(item[1]["timestamp_et"]), item[0]))
    for _, fill in ordered:
        sleeve = str(fill.get("sleeve") or "").strip().lower()
        if sleeve not in TRADE_SLEEVES:
            raise RefreshError(
                f"unexpected sleeve {sleeve!r} on kpi_trades; combined is the sum of the two desks"
            )
        ticker = str(fill.get("ticker") or "").strip().upper()
        if not ticker:
            raise RefreshError("kpi_trades row is missing ticker")
        side = str(fill.get("side") or "").strip().lower()
        if side not in {"buy", "sell"}:
            raise RefreshError(f"{sleeve} {ticker}: side {side!r} is not buy or sell")
        qty = dec(fill.get("qty"))
        if qty is None or qty <= 0:
            raise RefreshError(f"{sleeve} {ticker}: qty must be positive")
        price = dec(fill.get("avg_price"))
        pnl = dec(fill.get("pnl_trade_usd")) or Decimal("0")
        signed = qty if side == "buy" else -qty
        key = (sleeve, ticker)
        lots, open_qty = _open_lots(book.get(key))
        increasing = (signed > 0 and open_qty >= 0) or (signed < 0 and open_qty <= 0)
        if increasing:
            if price is None or price <= 0:
                raise RefreshError(f"{sleeve} {ticker}: opening fill is missing avg_price")
            lots.append({"qty": signed, "px": price})
            book[key] = lots
            cost_basis[key] = cost_basis.get(key, Decimal("0")) + abs(signed) * price
            continue
        excess = abs(signed) - abs(open_qty)
        if excess > DUST:
            raise RefreshError(
                f"{sleeve} {ticker}: close qty {qty} exceeds open {abs(open_qty)}"
            )
        realized[sleeve] += pnl
        realized_by_ticker[key] = realized_by_ticker.get(key, Decimal("0")) + pnl
        remaining = abs(signed)
        fifo_cost = Decimal("0")
        fifo_gross = Decimal("0")
        while remaining > DUST:
            if not lots:
                raise RefreshError(f"{sleeve} {ticker}: close qty {qty} exceeds open {abs(open_qty)}")
            lot = lots[0]
            take = min(abs(lot["qty"]), remaining)
            fifo_cost += lot["px"] * take
            if price is not None:
                if lot["qty"] > 0:
                    fifo_gross += (price - lot["px"]) * take
                else:
                    fifo_gross += (lot["px"] - price) * take
            if lot["qty"] > 0:
                lot["qty"] -= take
            else:
                lot["qty"] += take
            remaining -= take
            if abs(lot["qty"]) <= DUST:
                lots.pop(0)
        # Stored pnl stays on the snapshot. Shift open cost when that pnl is
        # not the FIFO amount, so the leftover mark uses the same basis.
        removed = _cost_removed(open_qty, qty, price, pnl, fifo_gross, fifo_cost, fill)
        cost_basis[key] = cost_basis.get(key, Decimal("0")) - removed
        if _lots_position(lots) is None:
            book.pop(key, None)
            cost_basis.pop(key, None)
        else:
            book[key] = lots
    open_book: dict[tuple[str, str], dict] = {}
    for key, lots in book.items():
        pos = _lots_position(lots, cost_basis.get(key))
        if pos is None:
            continue
        pos["realized"] = realized_by_ticker.get(key, Decimal("0"))
        open_book[key] = pos
    return realized, open_book


def _cost_removed(
    open_qty: Decimal,
    qty: Decimal,
    price: Decimal | None,
    stored: Decimal,
    fifo_gross: Decimal,
    fifo_cost: Decimal,
    fill: dict,
) -> Decimal:
    """Cost the stored close took off the open book.

    FIFO gross, or FIFO gross minus a fee already inside pnl, removes the
    lot cost. Any other stored pnl (the unbackfilled average-cost figure)
    removes the cost that pnl implies, so running P&L still telescopes.
    """
    if price is None or price <= 0:
        return fifo_cost
    fee = dec(fill.get("fee_usd")) or Decimal("0")
    if fee < 0:
        fee = Decimal("0")
    fee_in_pnl = Decimal("0")
    if fee > 0 and abs((fifo_gross - stored) - fee) <= BASIS_TOL:
        fee_in_pnl = fee
    if open_qty > 0:
        return price * qty - fee_in_pnl - stored
    return stored + price * qty + fee_in_pnl


def apply_books(fills: list[dict]) -> tuple[dict[str, Decimal], dict[tuple[str, str], dict]]:
    """Replay buys and sells. Realized is pnl_trade_usd on closes only.

    Sleeve realized is the sum of those close dollars. Each open position
    also keeps that ticker's own close dollars, including closes from before
    a later reopen. Opening fills do not add realized. Leftover avg_cost
    follows the stored close: FIFO lot cost when that pnl is FIFO, and the
    blended cost when the row is still average-cost. Realized and unrealized
    then describe one book.

    Duplicate fills are collapsed first. Replaying both copies leaves the
    later exit covering only one of them, so a closed name stays on the book.
    """
    collapsed, dropped = collapse_duplicate_fills(fills)
    raw_keys: set[tuple[str, str]] = set()
    if dropped:
        try:
            _raw_realized, raw_book = _replay_fills(fills)
            raw_keys = set(raw_book)
        except RefreshError:
            raw_keys = set()
    realized, book = _replay_fills(collapsed)
    hidden = sorted(raw_keys - set(book))
    apply_books.duplicates_collapsed = dropped
    apply_books.exits_hidden_by_duplicates = [f"{sleeve} {ticker}" for sleeve, ticker in hidden]
    return realized, book


apply_books.duplicates_collapsed = 0
apply_books.exits_hidden_by_duplicates = []


def note_for(sleeve: str, realized: Decimal, unrealized: Decimal) -> str:
    return (
        f"sleeve curve only; not the account. "
        f"realized {money(realized)} closed exits; unrealized {money(unrealized)} open MTM; "
        "book=start+realized+unrealized"
    )


def prior_rail(prior: dict | None, key: str) -> Decimal | None:
    if not prior or key not in prior:
        return None
    return dec(prior.get(key))


def _kept_account(prior: dict | None) -> dict[str, str | None]:
    """Previous combined dollars. Missing cash must not invent a new book."""
    import account_book

    keys = (
        "realized_pnl_usd",
        "unrealized_pnl_usd",
        "running_pnl_usd",
        "running_balance_usd",
    )
    kept = {}
    for key in keys:
        raw = None if not prior else prior.get(key)
        amount = dec(raw) if raw not in (None, "") else None
        kept[key] = None if amount is None else num_text(q6(amount))
    kept["notes"] = account_book.absent_note()
    return kept


def build_rows(
    fills: list[dict],
    marks: dict[tuple[str, str], Decimal],
    priors: dict[str, dict],
    as_of: str,
    cash: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    realized_raw, book = apply_books(fills)
    missing = [f"{sleeve} {ticker}" for sleeve, ticker in sorted(book) if (sleeve, ticker) not in marks]
    if missing:
        raise RefreshError("open tickers have no mark: " + ", ".join(missing))
    unrealized_raw = {sleeve: Decimal("0") for sleeve in TRADE_SLEEVES}
    opens = []
    for (sleeve, ticker), pos in sorted(book.items()):
        mark = marks[(sleeve, ticker)]
        unreal = pos["qty"] * (mark - pos["avg"])
        unrealized_raw[sleeve] += unreal
        opens.append(
            {
                "sleeve": sleeve,
                "ticker": ticker,
                "qty": num_text(pos["qty"]),
                "avg_cost": num_text(pos["avg"]),
                "mark": num_text(mark),
                "unrealized_pnl_usd": num_text(q6(unreal)),
            }
        )
    realized = {sleeve: q6(realized_raw[sleeve]) for sleeve in TRADE_SLEEVES}
    unrealized = {sleeve: q6(unrealized_raw[sleeve]) for sleeve in TRADE_SLEEVES}
    realized["combined"] = realized["crypto"] + realized["equities"]
    unrealized["combined"] = unrealized["crypto"] + unrealized["equities"]
    import account_book

    rows = []
    account = None
    held = account_book.cash_total(cash)
    if held is None:
        print(
            "cash drop absent; combined account figures left unchanged",
            file=sys.stderr,
        )
    else:
        account = account_book.statement(fills, marks, cash, START["combined"])
    for sleeve in SLEEVES:
        start = START[sleeve]
        running = realized[sleeve] + unrealized[sleeve]
        prior = priors.get(sleeve) or {}
        if sleeve == "combined" and account is not None:
            row_realized = account["realized_cents"]
            row_unreal = account["unrealized_cents"]
            row_running = account["running_cents"]
            row_balance = account["balance_cents"]
            notes = account_book.format_note(account)
        elif sleeve == "combined":
            kept = _kept_account(prior)
            row_realized = dec(kept["realized_pnl_usd"]) if kept["realized_pnl_usd"] else None
            row_unreal = dec(kept["unrealized_pnl_usd"]) if kept["unrealized_pnl_usd"] else None
            row_running = dec(kept["running_pnl_usd"]) if kept["running_pnl_usd"] else None
            row_balance = dec(kept["running_balance_usd"]) if kept["running_balance_usd"] else None
            notes = kept["notes"]
        else:
            row_realized = realized[sleeve]
            row_unreal = unrealized[sleeve]
            row_running = running
            row_balance = start + running
            notes = note_for(sleeve, realized[sleeve], unrealized[sleeve])
        row_as_of = as_of
        if sleeve == "combined" and account is None:
            raw_as_of = prior.get("as_of") if prior else None
            row_as_of = iso_z(parse_ts(raw_as_of)) if raw_as_of not in (None, "") else None
        rows.append(
            {
                "sleeve": sleeve,
                "as_of": row_as_of,
                "realized_pnl_usd": num_text(q6(row_realized)) if row_realized is not None else None,
                "unrealized_pnl_usd": num_text(q6(row_unreal)) if row_unreal is not None else None,
                "running_pnl_usd": num_text(q6(row_running)) if row_running is not None else None,
                "running_balance_usd": num_text(q6(row_balance)) if row_balance is not None else None,
                "start_balance_usd": num_text(start),
                "day_kill_pct": num_text(prior_rail(prior, "day_kill_pct")),
                "day_target_pct": num_text(prior_rail(prior, "day_target_pct")),
                "notes": notes,
            }
        )
    return rows, opens


def env_values() -> dict[str, str]:
    names = (
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_DB_URL",
        "FINNHUB_API_KEY",
        "COINSTATS_API_KEY",
        "ALPHA_VANTAGE_API_KEY",
        "RH_API_KEY",
        "RH_BASE64_PRIVATE_KEY",
        "RH_AGENTIC_ACCOUNT",
    )
    return {name: (os.environ.get(name) or "").strip() for name in names}


def rest_headers(key: str) -> dict[str, str]:
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def rest_call(base_url: str, key: str, path: str, method: str = "GET", body=None, extra_headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = rest_headers(key)
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read()
            return response.status, loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RefreshError(rest_error(method, path, exc.code, detail, [key])) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RefreshError("REST network error: " + redact(str(exc), [key])) from None


def rest_error(method: str, path: str, status: int, detail: str, secrets: list[str]) -> str:
    code = ""
    message = detail
    try:
        payload = json.loads(detail)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        code = str(payload.get("code") or "")
        message = str(payload.get("message") or detail)
    message = redact(message, secrets)
    table = path.split("?", 1)[0]
    if code == "25006" or "read-only transaction" in message.lower():
        return (
            "INSERT rejected: read-only transaction (25006). "
            "kpi_sleeve_snapshots was not updated."
        )
    if code in {"53100", "53200"} or "no space left" in message.lower():
        return f"INSERT rejected: database disk full ({code or status}). Snapshots were not updated."
    if "PGRST205" in message or status == 404:
        return f"REST {method} {table} is not exposed ({status})."
    return f"REST {method} {table} HTTP {status} {code}: {message}"


def fetch_trades_rest(base_url: str, key: str) -> list[dict]:
    rows: list[dict] = []
    page = 1000
    for offset in range(0, page * 20, page):
        path = (
            "/rest/v1/kpi_trades"
            "?select=sleeve,ticker,side,qty,avg_price,notional_usd,pnl_trade_usd,fee_usd,timestamp_et,order_id"
            "&order=timestamp_et.asc"
            f"&limit={page}&offset={offset}"
        )
        _status, payload = rest_call(base_url, key, path)
        if not isinstance(payload, list):
            raise RefreshError("REST kpi_trades did not return a row list")
        rows.extend(row for row in payload if isinstance(row, dict))
        if len(payload) < page:
            return rows
    raise RefreshError("REST kpi_trades exceeded 20000 rows; refusing a partial book")


def fetch_prior_rest(base_url: str, key: str, sleeve: str) -> dict | None:
    path = (
        "/rest/v1/kpi_sleeve_snapshots"
        f"?sleeve=eq.{sleeve}&select=*&order=as_of.desc&limit=1"
    )
    _status, payload = rest_call(base_url, key, path)
    if not isinstance(payload, list):
        raise RefreshError("REST kpi_sleeve_snapshots did not return a row list")
    return payload[0] if payload else None


def table_columns(base_url: str, key: str) -> set[str] | None:
    """Best-effort column check. A failed probe does not block the INSERT."""
    try:
        status_payload = rest_call(
            base_url,
            key,
            "/rest/v1/",
            extra_headers={"Accept": "application/openapi+json"},
        )
    except (RefreshError, ValueError, TypeError):
        return None
    spec = status_payload[1]
    if not isinstance(spec, dict):
        return None
    schemas = (spec.get("definitions") or {}) | ((spec.get("components") or {}).get("schemas") or {})
    props = (schemas.get("kpi_sleeve_snapshots") or {}).get("properties") or {}
    if not props:
        return None
    return set(props)


def prepare_payload(rows: list[dict], columns: set[str] | None) -> list[dict]:
    note_key = "notes"
    if columns is not None and "notes" not in columns and "note" in columns:
        note_key = "note"
    payload = []
    for row in rows:
        item = {}
        for name in WRITE_COLUMNS:
            key = note_key if name == "notes" else name
            item[key] = row[name]
        payload.append(item)
    return payload


def insert_rest(base_url: str, key: str, payload: list[dict]) -> list:
    _status, body = rest_call(
        base_url,
        key,
        "/rest/v1/kpi_sleeve_snapshots",
        method="POST",
        body=payload,
        extra_headers={"Prefer": "return=representation"},
    )
    if not isinstance(body, list) or len(body) != len(payload):
        raise RefreshError("INSERT did not return the new kpi_sleeve_snapshots rows")
    return body


def connect_db(db_url: str):
    try:
        import psycopg
    except ImportError as exc:
        raise RefreshError("psycopg is required for SUPABASE_DB_URL") from exc
    return psycopg.connect(db_url, connect_timeout=20)


def db_error(exc, secrets: list[str]) -> RefreshError:
    sqlstate = getattr(exc, "sqlstate", "") or ""
    message = redact(str(exc), secrets)
    if sqlstate == "25006" or "read-only transaction" in message.lower():
        return RefreshError(
            "INSERT rejected: read-only transaction (25006). kpi_sleeve_snapshots was not updated."
        )
    if sqlstate in {"53100", "53200"} or "no space left" in message.lower():
        return RefreshError(
            f"INSERT rejected: database disk full ({sqlstate}). Snapshots were not updated."
        )
    return RefreshError(f"database error {sqlstate}: {message}")


def fetch_trades_db(db_url: str, secrets: list[str]) -> list[dict]:
    sql = """
        select sleeve, ticker, side, qty, avg_price, notional_usd, pnl_trade_usd, fee_usd, timestamp_et, order_id
        from public.kpi_trades
        order by timestamp_et asc
    """
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None


def fetch_prior_db(db_url: str, sleeve: str, secrets: list[str]) -> dict | None:
    sql = """
        select *
        from public.kpi_sleeve_snapshots
        where sleeve = %s
        order by as_of desc
        limit 1
    """
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sleeve,))
                row = cur.fetchone()
                if row is None:
                    return None
                columns = [desc.name for desc in cur.description]
                return dict(zip(columns, row))
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None


def insert_db(db_url: str, payload: list[dict], secrets: list[str]) -> list[dict]:
    columns = list(payload[0].keys())
    names = ", ".join(columns)
    places = ", ".join(["%s"] * len(columns))
    sql = (
        f"insert into public.kpi_sleeve_snapshots ({names}) "
        f"values ({places}) returning sleeve, as_of"
    )
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                returned = []
                for row in payload:
                    cur.execute(sql, [row[name] for name in columns])
                    fetched = cur.fetchone()
                    if fetched:
                        returned.append({"sleeve": fetched[0], "as_of": fetched[1]})
            conn.commit()
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None
    if len(returned) != len(payload):
        raise RefreshError("INSERT did not return the new kpi_sleeve_snapshots rows")
    return returned


def summary_as_of_rest(base_url: str, key: str) -> dict[str, dt.datetime]:
    _status, payload = rest_call(base_url, key, "/rest/v1/kpi_summary?select=sleeve,as_of")
    if not isinstance(payload, list):
        raise RefreshError("REST kpi_summary did not return a row list")
    found = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        sleeve = str(row.get("sleeve") or "").strip().lower()
        if sleeve in SLEEVES and row.get("as_of") is not None:
            moment = parse_ts(row["as_of"])
            if sleeve not in found or moment > found[sleeve]:
                found[sleeve] = moment
    return found


def summary_as_of_db(db_url: str, secrets: list[str]) -> dict[str, dt.datetime]:
    sql = "select sleeve, as_of from public.kpi_summary"
    try:
        with connect_db(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                found = {}
                for sleeve, as_of in cur.fetchall():
                    key = str(sleeve or "").strip().lower()
                    if key in SLEEVES and as_of is not None:
                        moment = parse_ts(as_of)
                        if key not in found or moment > found[key]:
                            found[key] = moment
                return found
    except RefreshError:
        raise
    except Exception as exc:
        raise db_error(exc, secrets) from None


def sleeves_left_unchanged(rows: list[dict], as_of: str) -> set[str]:
    """Sleeves that did not get this run's as_of. Combined, when cash is missing."""
    fresh = {row["sleeve"] for row in rows if row.get("as_of") == as_of}
    return {sleeve for sleeve in SLEEVES if sleeve not in fresh}


def assert_fresh(
    found: dict[str, dt.datetime],
    floor: dt.datetime,
    unchanged: set[str] | None = None,
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    kept = unchanged or set()
    missing = [sleeve for sleeve in SLEEVES if sleeve not in found]
    if missing:
        raise RefreshError(
            "public.kpi_summary is missing " + ", ".join(missing) + " after INSERT"
        )
    for sleeve, moment in found.items():
        if sleeve in kept:
            continue
        text = iso_z(moment)
        if moment < floor or moment > now + dt.timedelta(minutes=2):
            frozen = " Still the frozen 2026-09-27 23:48Z snapshot." if text.startswith(FROZEN_PREFIX) else ""
            raise RefreshError(
                f"public.kpi_summary {sleeve} as_of {text} is not the row just inserted.{frozen}"
            )


def load_inputs(env: dict[str, str]) -> tuple[list[dict], dict[str, dict], str]:
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    secrets = [key, db_url]
    if key:
        try:
            trades = fetch_trades_rest(base_url, key)
            priors = {sleeve: fetch_prior_rest(base_url, key, sleeve) or {} for sleeve in SLEEVES}
            return trades, priors, "rest"
        except RefreshError as exc:
            if not db_url:
                if "kpi_trades" in str(exc):
                    raise RefreshError(
                        str(exc)
                        + " Refusing kpi_trades_scrubbed: that view has no qty or price."
                    ) from None
                raise
            print(f"REST read failed ({exc}); trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise RefreshError(
            "SUPABASE_SERVICE_ROLE_KEY and SUPABASE_DB_URL are unset. "
            "Refusing to exit 0 with the frozen snapshot."
        )
    trades = fetch_trades_db(db_url, secrets)
    priors = {sleeve: fetch_prior_db(db_url, sleeve, secrets) or {} for sleeve in SLEEVES}
    return trades, priors, "db"


def resolve_marks(book_keys: list[tuple[str, str]], env: dict[str, str]) -> dict[tuple[str, str], tuple[Decimal, str]]:
    marks = {}
    errors = []
    for sleeve, ticker in book_keys:
        try:
            price, source = mark_for(sleeve, ticker, env)
        except RefreshError as exc:
            errors.append(str(exc))
            continue
        marks[(sleeve, ticker)] = (price, source)
        print(f"mark {sleeve} {ticker} {price} via {source}", file=sys.stderr)
    if errors:
        raise RefreshError("open tickers have no mark:\n" + "\n".join(errors))
    return marks


def refresh(dry_run: bool) -> int:
    env = env_values()
    secrets = [env.get("SUPABASE_SERVICE_ROLE_KEY", ""), env.get("SUPABASE_DB_URL", "")]
    trades, priors, source = load_inputs(env)
    if not trades:
        raise RefreshError("public.kpi_trades returned no rows; refusing to write a flat snapshot")
    _realized, book = apply_books(trades)
    quotes = resolve_marks(sorted(book), env)
    marks = {key: price for key, (price, _source) in quotes.items()}
    as_of = iso_z(dt.datetime.now(dt.timezone.utc))
    import account_book

    account_book.save_marks(marks, as_of)
    cash, origin = account_book.read_cash(env, Path(__file__).resolve().parents[1] / "data")
    if origin:
        print(f"account cash from {origin}", file=sys.stderr)
    rows, opens = build_rows(trades, marks, priors, as_of, cash=cash)
    combined = next(row for row in rows if row["sleeve"] == "combined")
    if combined.get("running_balance_usd") is None:
        raise RefreshError(
            "cash drop absent and no previous combined figures; snapshot was not updated"
        )
    insert_rows = [row for row in rows if row.get("as_of") == as_of]
    unchanged = sleeves_left_unchanged(rows, as_of)
    account_book.mark_unchanged(unchanged)
    if not any(row["sleeve"] == "combined" for row in insert_rows):
        print(
            "cash drop absent; combined snapshot as_of left unchanged",
            file=sys.stderr,
        )
    collapsed = int(getattr(apply_books, "duplicates_collapsed", 0) or 0)
    hidden = list(getattr(apply_books, "exits_hidden_by_duplicates", []) or [])
    open_names = [f"{item['sleeve']} {item['ticker']} {item['qty']}" for item in opens]
    print(open_lots_note(collapsed, hidden, [f"{item['sleeve']} {item['ticker']}" for item in opens]))
    if open_names:
        print("open lots: " + ", ".join(open_names))
    for item in opens:
        item["source"] = quotes[(item["sleeve"], item["ticker"])][1]
    plan = {
        "dry_run": dry_run,
        "source": source,
        "as_of": as_of,
        "prior_as_of": {
            sleeve: (iso_z(parse_ts(priors[sleeve]["as_of"])) if priors.get(sleeve) and priors[sleeve].get("as_of") else None)
            for sleeve in SLEEVES
        },
        "opens": opens,
        "rows": rows,
    }
    if dry_run:
        print(json.dumps(plan, indent=2))
        print("dry-run: no INSERT", file=sys.stderr)
        return 0
    key = env.get("SUPABASE_SERVICE_ROLE_KEY") or ""
    db_url = env.get("SUPABASE_DB_URL") or ""
    base_url = env.get("SUPABASE_URL") or DEFAULT_URL
    columns = table_columns(base_url, key) if key else None
    payload = prepare_payload(insert_rows, columns)
    inserted = False
    rest_error_text = ""
    if key and source == "rest":
        try:
            insert_rest(base_url, key, payload)
            inserted = True
        except RefreshError as exc:
            rest_error_text = str(exc)
            if not db_url:
                raise
            print(f"REST insert failed ({exc}); trying SUPABASE_DB_URL", file=sys.stderr)
    if not inserted:
        if not db_url:
            raise RefreshError(rest_error_text or "no database credential for INSERT")
        insert_db(db_url, payload, secrets)
    floor = parse_ts(as_of) - dt.timedelta(seconds=2)
    if key:
        try:
            found = summary_as_of_rest(base_url, key)
        except RefreshError:
            if not db_url:
                raise
            found = summary_as_of_db(db_url, secrets)
    else:
        found = summary_as_of_db(db_url, secrets)
    assert_fresh(found, floor, unchanged)
    for row in insert_rows:
        print(
            "inserted {sleeve} as_of={as_of} realized={realized_pnl_usd} "
            "unrealized={unrealized_pnl_usd} running={running_pnl_usd} "
            "balance={running_balance_usd}".format(**row)
        )
    return 0


def self_test() -> int:
    fills = [
        {
            "sleeve": "crypto",
            "ticker": "aaa",
            "side": "buy",
            "qty": "10",
            "avg_price": "2",
            "pnl_trade_usd": "99",
            "timestamp_et": "2026-09-01T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "AAA",
            "side": "buy",
            "qty": "10",
            "avg_price": "4",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-02T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "AAA",
            "side": "sell",
            "qty": "5",
            "avg_price": "5",
            "pnl_trade_usd": "15",
            "timestamp_et": "2026-09-03T00:00:00Z",
        },
        {
            "sleeve": "equities",
            "ticker": "QCOM",
            "side": "buy",
            "qty": "2",
            "avg_price": "100",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-04T00:00:00Z",
        },
    ]
    marks = {("crypto", "AAA"): Decimal("4"), ("equities", "QCOM"): Decimal("110")}
    priors = {"crypto": {"day_kill_pct": Decimal("-0.10"), "day_target_pct": Decimal("0.025")}}
    desk_cash = {"USD": Decimal("40"), "USDC": Decimal("10")}
    rows, opens = build_rows(fills, marks, priors, "2026-09-28T01:00:00Z", cash=desk_cash)
    by = {row["sleeve"]: row for row in rows}
    # Opening buys ignore pnl_trade_usd. FIFO sells 5 of the $2 lot at $5 (realized 15).
    # Leftover is 5 @ 2 and 10 @ 4, cost 50. Mark 4 unrealized is 10, not 15 at the old blend of 3.
    if by["crypto"]["realized_pnl_usd"] != "15.000000":
        raise RefreshError(f"crypto realized {by['crypto']['realized_pnl_usd']}")
    if by["crypto"]["unrealized_pnl_usd"] != "10.000000":
        raise RefreshError(f"crypto unrealized {by['crypto']['unrealized_pnl_usd']}")
    if by["crypto"]["running_pnl_usd"] != "25.000000":
        raise RefreshError("crypto running")
    if by["crypto"]["running_balance_usd"] != "325.000000":
        raise RefreshError("crypto balance")
    if by["crypto"]["day_kill_pct"] != "-0.10" or by["crypto"]["day_target_pct"] != "0.025":
        raise RefreshError("crypto rails were not copied")
    if by["equities"]["realized_pnl_usd"] != "0.000000":
        raise RefreshError("equities realized")
    if by["equities"]["unrealized_pnl_usd"] != "20.000000":
        raise RefreshError("equities unrealized")
    if by["equities"]["running_balance_usd"] != "520.000000":
        raise RefreshError("equities balance")
    if by["equities"]["day_kill_pct"] is not None:
        raise RefreshError("missing prior rail should stay null")
    # Cash 50 plus the open crypto lot (15 * 4). Equities stay off this row.
    # Realized is running minus unrealized, not the trade-list 15.
    combined_balance = Decimal("50") + Decimal("15") * Decimal("4")
    combined_running = combined_balance - START["combined"]
    combined_unreal = Decimal("10")
    combined_realized = combined_running - combined_unreal
    if by["combined"]["realized_pnl_usd"] != num_text(q6(combined_realized)):
        raise RefreshError(f"combined realized {by['combined']['realized_pnl_usd']}")
    if by["combined"]["unrealized_pnl_usd"] != num_text(q6(combined_unreal)):
        raise RefreshError("combined unrealized")
    if by["combined"]["running_pnl_usd"] != num_text(q6(combined_running)):
        raise RefreshError("combined running")
    if Decimal(by["combined"]["realized_pnl_usd"]) + Decimal(by["combined"]["unrealized_pnl_usd"]) != Decimal(
        by["combined"]["running_pnl_usd"]
    ):
        raise RefreshError("combined realized plus unrealized left running")
    if by["combined"]["running_balance_usd"] != num_text(q6(combined_balance)):
        raise RefreshError("combined balance")
    if "not a sleeve seed curve" not in by["combined"]["notes"] or "buy fees" not in by["combined"]["notes"]:
        raise RefreshError(f"combined note {by['combined']['notes']}")
    if by["combined"]["start_balance_usd"] != num_text(START["combined"]):
        raise RefreshError("combined seed")
    if "not the account" not in by["crypto"]["notes"] or "not the account" not in by["equities"]["notes"]:
        raise RefreshError("sleeve note still reads as the account")
    kept_prior = {
        "combined": {
            "as_of": "2026-09-01T00:00:00Z",
            "realized_pnl_usd": "1.25",
            "unrealized_pnl_usd": "0.25",
            "running_pnl_usd": "1.50",
            "running_balance_usd": "12.50",
        }
    }
    import io

    old_err = sys.stderr
    sys.stderr = io.StringIO()
    try:
        kept_rows, _kept_opens = build_rows(fills, marks, kept_prior, "2026-09-28T01:00:00Z")
        absent_log = sys.stderr.getvalue()
    finally:
        sys.stderr = old_err
    kept_combined = {row["sleeve"]: row for row in kept_rows}["combined"]
    if kept_combined["running_balance_usd"] != "12.500000":
        raise RefreshError(f"missing cash rewrote the book {kept_combined}")
    if kept_combined["as_of"] != "2026-09-01T00:00:00Z":
        raise RefreshError(f"missing cash stamped a fresh combined clock {kept_combined['as_of']}")
    if kept_combined["realized_pnl_usd"] != "1.250000" or kept_combined["running_pnl_usd"] != "1.500000":
        raise RefreshError("missing cash fell back to seed+realized+unrealized")
    if "cash drop absent" not in absent_log or "not a seed+realized+unrealized fallback" not in kept_combined["notes"]:
        raise RefreshError("missing cash was not logged")
    if len(opens) != 2:
        raise RefreshError("open count")
    aaa = next(row for row in opens if row["ticker"] == "AAA")
    if Decimal(aaa["qty"]) != Decimal("15") or Decimal(aaa["avg_cost"]) != Decimal("50") / Decimal("15"):
        raise RefreshError(f"fifo leftover was blended {aaa}")
    # Warehouse rows still store the average-cost close (10), not the FIFO 15.
    # Leftover cost stays the blend of 3 so running is 25, not 10 + 10.
    average_fills = [dict(fill) for fill in fills]
    average_fills[2] = dict(average_fills[2], pnl_trade_usd="10")
    avg_rows, avg_opens = build_rows(average_fills, marks, priors, "2026-09-28T01:00:00Z")
    avg_by = {row["sleeve"]: row for row in avg_rows}
    if avg_by["crypto"]["realized_pnl_usd"] != "10.000000":
        raise RefreshError(f"average realized {avg_by['crypto']['realized_pnl_usd']}")
    if avg_by["crypto"]["unrealized_pnl_usd"] != "15.000000":
        raise RefreshError(f"average unrealized was marked FIFO {avg_by['crypto']['unrealized_pnl_usd']}")
    if avg_by["crypto"]["running_pnl_usd"] != "25.000000":
        raise RefreshError(f"average running mixed bases {avg_by['crypto']['running_pnl_usd']}")
    avg_aaa = next(row for row in avg_opens if row["ticker"] == "AAA")
    if Decimal(avg_aaa["qty"]) != Decimal("15") or Decimal(avg_aaa["avg_cost"]) != Decimal("3"):
        raise RefreshError(f"average-cost close was marked FIFO {avg_aaa}")
    fee_fills = [dict(fill) for fill in fills]
    fee_fills[2] = dict(fee_fills[2], pnl_trade_usd="14.8", fee_usd="0.2")
    fee_rows, fee_opens = build_rows(fee_fills, marks, priors, "2026-09-28T01:00:00Z")
    fee_by = {row["sleeve"]: row for row in fee_rows}
    fee_aaa = next(row for row in fee_opens if row["ticker"] == "AAA")
    if Decimal(fee_aaa["avg_cost"]) != Decimal("50") / Decimal("15"):
        raise RefreshError(f"fee-net FIFO leftover absorbed the fee {fee_aaa}")
    if fee_by["crypto"]["realized_pnl_usd"] != "14.800000" or fee_by["crypto"]["running_pnl_usd"] != "24.800000":
        raise RefreshError(f"fee-net book {fee_by['crypto']}")
    try:
        build_rows(fills, {("crypto", "AAA"): Decimal("4")}, priors, "2026-09-28T01:00:00Z")
    except RefreshError as exc:
        if "QCOM" not in str(exc):
            raise
    else:
        raise RefreshError("missing mark did not fail")
    oversell = list(fills)
    oversell.append(
        {
            "sleeve": "equities",
            "ticker": "QCOM",
            "side": "sell",
            "qty": "9",
            "avg_price": "110",
            "pnl_trade_usd": "1",
            "timestamp_et": "2026-09-05T00:00:00Z",
        }
    )
    try:
        build_rows(oversell, marks, priors, "2026-09-28T01:00:00Z")
    except RefreshError as exc:
        if "exceeds open" not in str(exc):
            raise
    else:
        raise RefreshError("oversell did not fail")
    closed = [
        {
            "sleeve": "crypto",
            "ticker": "W",
            "side": "buy",
            "qty": "3",
            "avg_price": "1",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-01T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "W",
            "side": "sell",
            "qty": "3",
            "avg_price": "2",
            "pnl_trade_usd": "3",
            "timestamp_et": "2026-09-02T00:00:00Z",
        },
    ]
    flat_rows, flat_opens = build_rows(closed, {}, {}, "2026-09-28T01:00:00Z")
    if flat_opens or flat_rows[0]["realized_pnl_usd"] != "3.000000":
        raise RefreshError("flat close should book realized and no mark")
    if flat_rows[0]["running_balance_usd"] != "303.000000":
        raise RefreshError("flat balance")
    short_fills = [
        {
            "sleeve": "equities",
            "ticker": "QCOM",
            "side": "sell",
            "qty": "2",
            "avg_price": "10",
            "pnl_trade_usd": "50",
            "timestamp_et": "2026-09-01T00:00:00Z",
        }
    ]
    short_rows, _short_opens = build_rows(
        short_fills,
        {("equities", "QCOM"): Decimal("9")},
        {},
        "2026-09-28T01:00:00Z",
    )
    by_short = {row["sleeve"]: row for row in short_rows}
    if by_short["equities"]["unrealized_pnl_usd"] != "2.000000":
        raise RefreshError(f"short unrealized {by_short['equities']['unrealized_pnl_usd']}")
    if by_short["equities"]["realized_pnl_usd"] != "0.000000":
        raise RefreshError("opening sell must not count pnl")
    duplicated = [
        {
            "sleeve": "crypto",
            "ticker": "AVAX",
            "side": "buy",
            "qty": "2.005",
            "avg_price": "11.1328",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-27T07:50:18Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "AVAX",
            "side": "buy",
            "qty": "2.005",
            "avg_price": "11.1328",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-27T07:50:18.526927Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "AVAX",
            "side": "sell",
            "qty": "2.005",
            "avg_price": "11.2",
            "pnl_trade_usd": "1",
            "timestamp_et": "2026-10-01T01:50:15.242024Z",
        },
    ]
    dup_rows, dup_opens = build_rows(duplicated, {}, {}, "2026-10-06T00:00:00Z")
    if dup_opens:
        raise RefreshError(f"duplicate open survived the exit: {dup_opens}")
    if dup_rows[0]["realized_pnl_usd"] != "1.000000":
        raise RefreshError(f"duplicate close double-counted pnl: {dup_rows[0]['realized_pnl_usd']}")
    if apply_books.duplicates_collapsed != 1:
        raise RefreshError(f"duplicate collapse count {apply_books.duplicates_collapsed}")
    if apply_books.exits_hidden_by_duplicates != ["crypto AVAX"]:
        raise RefreshError(f"hidden exits {apply_books.exits_hidden_by_duplicates}")
    paired = [
        {
            "sleeve": "crypto",
            "ticker": "W",
            "side": "buy",
            "qty": "3",
            "avg_price": "1",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-26T16:36:50Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "W",
            "side": "buy",
            "qty": "3",
            "avg_price": "1",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-26T16:36:50.714048Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "W",
            "side": "sell",
            "qty": "3",
            "avg_price": "2",
            "pnl_trade_usd": "1",
            "timestamp_et": "2026-09-26T18:22:05Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "W",
            "side": "sell",
            "qty": "3",
            "avg_price": "2",
            "pnl_trade_usd": "3",
            "timestamp_et": "2026-09-26T18:22:05.677327Z",
        },
    ]
    pair_rows, pair_opens = build_rows(paired, {}, {}, "2026-10-06T00:00:00Z")
    if pair_opens or pair_rows[0]["realized_pnl_usd"] != "3.000000":
        raise RefreshError(f"paired duplicate sell {pair_rows[0]['realized_pnl_usd']} {pair_opens}")
    if apply_books.duplicates_collapsed != 2:
        raise RefreshError("paired fills were not both collapsed")
    distinct = [
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "1",
            "avg_price": "2",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-26T20:48:07.153603Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "1",
            "avg_price": "2",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-09-26T20:48:37.025431Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "sell",
            "qty": "1",
            "avg_price": "3",
            "pnl_trade_usd": "1",
            "timestamp_et": "2026-09-26T20:49:44.595697Z",
        },
    ]
    _distinct_rows, distinct_opens = build_rows(
        distinct,
        {("crypto", "QNT"): Decimal("3")},
        {},
        "2026-10-06T00:00:00Z",
    )
    if len(distinct_opens) != 1 or distinct_opens[0]["qty"] != "1":
        raise RefreshError(f"two real buys were collapsed: {distinct_opens}")
    if apply_books.duplicates_collapsed != 0:
        raise RefreshError("distinct fills were marked duplicate")
    same_order = [
        {
            "sleeve": "crypto",
            "ticker": "ZZZ",
            "side": "buy",
            "qty": "4",
            "avg_price": "0.245",
            "pnl_trade_usd": "0",
            "order_id": "6ab90000-0000-4000-8000-0000000000aa",
            "timestamp_et": "2026-10-06T19:41:50Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "ZZZ",
            "side": "buy",
            "qty": "4",
            "avg_price": "0.245",
            "pnl_trade_usd": "0",
            "order_id": "6ab90000-0000-4000-8000-0000000000aa",
            "timestamp_et": "2026-10-06T19:46:50.169016Z",
        },
    ]
    _order_rows, order_opens = build_rows(
        same_order,
        {("crypto", "ZZZ"): Decimal("0.25")},
        {},
        "2026-10-06T00:00:00Z",
    )
    if len(order_opens) != 1 or order_opens[0]["qty"] != "4":
        raise RefreshError(f"same order_id was replayed twice: {order_opens}")
    if apply_books.duplicates_collapsed != 1:
        raise RefreshError("same order_id was not one fill")
    legacy_twin = [
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "4",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "order_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            "timestamp_et": "2026-01-01T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "4",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-01-01T00:00:01Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "sell",
            "qty": "4",
            "avg_price": "14",
            "pnl_trade_usd": "16",
            "order_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2",
            "timestamp_et": "2026-01-02T00:00:00Z",
        },
    ]
    twin_rows, twin_opens = build_rows(
        legacy_twin,
        {("crypto", "QNT"): Decimal("14")},
        {},
        "2026-10-06T00:00:00Z",
    )
    if twin_opens:
        raise RefreshError(f"null order_id twin a second later stayed open: {twin_opens}")
    if twin_rows[0]["realized_pnl_usd"] != "16.000000":
        raise RefreshError(f"legacy twin close {twin_rows[0]['realized_pnl_usd']}")
    if apply_books.duplicates_collapsed != 1:
        raise RefreshError(f"legacy twin was not collapsed {apply_books.duplicates_collapsed}")
    whole_second = [
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "4",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "order_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1",
            "timestamp_et": "2026-01-01T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "4",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-01-01T00:00:00Z",
        },
    ]
    _whole_rows, whole_opens = build_rows(
        whole_second,
        {("crypto", "QNT"): Decimal("10")},
        {},
        "2026-10-06T00:00:00Z",
    )
    if len(whole_opens) != 1 or whole_opens[0]["qty"] != "4":
        raise RefreshError(f"same-second null twin was replayed: {whole_opens}")
    far_twin = [
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "1",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-01-01T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "QNT",
            "side": "buy",
            "qty": "1",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "order_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
            "timestamp_et": "2026-01-01T00:00:03Z",
        },
    ]
    _far_rows, far_opens = build_rows(
        far_twin,
        {("crypto", "QNT"): Decimal("10")},
        {},
        "2026-10-06T00:00:00Z",
    )
    if len(far_opens) != 1 or far_opens[0]["qty"] != "2":
        raise RefreshError(f"a null row three seconds away was treated as a twin: {far_opens}")
    if apply_books.duplicates_collapsed != 0:
        raise RefreshError("a far null row was collapsed")
    message = rest_error(
        "POST",
        "/rest/v1/kpi_sleeve_snapshots",
        400,
        '{"code":"25006","message":"cannot execute INSERT in a read-only transaction"}',
        [],
    )
    if "25006" not in message or "not updated" not in message:
        raise RefreshError(message)
    print("self-test ok")
    return 0


def stamp_warehouse_failure(message: str) -> None:
    """Write the board chip onto data/meta.json. Does not rewrite KPI numbers."""
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    import export_kpi

    frozen = export_kpi.committed_as_of(root / "data")
    _status, public, warehouse_status = export_kpi.classify_failure(RefreshError(message), frozen)
    export_kpi.stamp_export_failure(
        root / "data",
        "error",
        public,
        warehouse_status=warehouse_status,
        snapshot_as_of=frozen,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read fills, mark the book, print rows, and do not INSERT",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check position math and the read-only INSERT error, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        return refresh(dry_run=args.dry_run)
    except RefreshError as exc:
        print(str(exc), file=sys.stderr)
        try:
            stamp_warehouse_failure(str(exc))
        except OSError as stamp_exc:
            print(f"Could not record warehouse status in data/meta.json: {stamp_exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
