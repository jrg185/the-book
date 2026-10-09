#!/usr/bin/env python3
"""Account book shared by the sleeve snapshot and the live-book export.

Balance is cash plus open crypto lots, marked once. Running P&L is that
balance minus the combined seed in config/book_seeds.json. Unrealized is
open quantity times (mark - average cost) on the Robinhood basis, which
leaves the buy fee out of cost. Realized is running P&L minus unrealized,
so every buy fee that cash already paid shows up there.

Per-trade pnl_trade_usd is unchanged: a sell nets its own fee, and a buy
does not move that column or the lot's average cost.

Cash is the same read the export uses. Signed Robinhood REST when the API
key and private key are set, otherwise data/rh_cash.json (USD plus USDC).
A missing drop does not invent seed + trade P&L + open mark-to-market.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import sys
import uuid
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "account_book.json"
MARKS_NAME = "the-book-kpi-marks.json"
CASH_KEYS = ("USD", "USDC")
CENT = Decimal("0.01")
MICRO = Decimal("0.000001")


class AccountBookError(RuntimeError):
    """The account book could not be built from the inputs."""


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _q6(value: Decimal) -> Decimal:
    return value.quantize(MICRO)


def cents_half_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def js_cents(value: Decimal) -> Decimal:
    """Cents, matching derive.js Math.round (ties toward +infinity)."""
    shifted = (float(value) + sys.float_info.epsilon) * 100
    return Decimal(str(math.floor(shifted + 0.5) / 100.0))


def marks_path() -> Path:
    override = (os.environ.get("KPI_MARKS_PATH") or "").strip()
    if override:
        return Path(override)
    return Path("/tmp") / MARKS_NAME


def marks_max_age_seconds(path: Path | None = None) -> int:
    """How long a saved quote set may be reused. Read from config."""
    config = path or CONFIG_PATH
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountBookError(f"account tolerance config is unreadable: {config}") from exc
    if not isinstance(payload, dict) or "marks_max_age_seconds" not in payload:
        raise AccountBookError("account tolerance config needs marks_max_age_seconds")
    raw = payload.get("marks_max_age_seconds")
    try:
        amount = int(str(raw))
    except (TypeError, ValueError) as exc:
        raise AccountBookError("marks_max_age_seconds must be a non-negative integer") from exc
    if amount < 0:
        raise AccountBookError("marks_max_age_seconds must be a non-negative integer")
    return amount


def _parse_utc(value) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _marks_are_fresh(payload: dict) -> bool:
    """A quote file from this run. Missing, old, or future stamps are not reused."""
    written = _parse_utc(payload.get("written_at"))
    if written is None:
        return False
    age = (dt.datetime.now(dt.timezone.utc) - written).total_seconds()
    if age < -120:
        return False
    return age <= marks_max_age_seconds()


def save_marks(marks: dict, as_of: str) -> Path:
    """Persist one quote set so export does not fetch a second set."""
    path = marks_path()
    run_id = (os.environ.get("KPI_MARKS_RUN_ID") or "").strip() or uuid.uuid4().hex
    payload = {
        "as_of": as_of,
        "written_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_id": run_id,
        "marks": {
            f"{sleeve}:{ticker}": format(price, "f")
            for (sleeve, ticker), price in sorted(marks.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _read_marks_payload() -> dict | None:
    path = marks_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not _marks_are_fresh(payload):
        return None
    return payload


def mark_unchanged(sleeves) -> None:
    """Sleeves whose as_of this refresh deliberately left alone.

    Export reads the same file so a kept combined clock is not a stale export.
    """
    path = marks_path()
    payload = _read_marks_payload()
    if payload is None:
        return
    names = sorted({str(name).strip().lower() for name in sleeves if str(name).strip()})
    payload["unchanged_sleeves"] = names
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def unchanged_sleeves() -> set[str]:
    """Sleeves recorded by mark_unchanged for this fresh quote file."""
    payload = _read_marks_payload()
    if payload is None:
        return set()
    raw = payload.get("unchanged_sleeves")
    if not isinstance(raw, list):
        return set()
    return {str(name).strip().lower() for name in raw if str(name).strip()}


def load_marks() -> dict | None:
    """Marks saved by the refresh step in this run.

    None when the file is absent, stale-shaped, older than the configured
    age, or stamped for a different KPI_MARKS_RUN_ID.
    """
    path = marks_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not _marks_are_fresh(payload):
        print("stale quote set ignored", file=sys.stderr)
        return None
    expected_run = (os.environ.get("KPI_MARKS_RUN_ID") or "").strip()
    if expected_run and str(payload.get("run_id") or "") != expected_run:
        print("quote set run id does not match this run", file=sys.stderr)
        return None
    raw = payload.get("marks")
    if not isinstance(raw, dict) or not raw:
        return None
    marks = {}
    for key, price in raw.items():
        if not isinstance(key, str) or ":" not in key:
            return None
        sleeve, ticker = key.split(":", 1)
        amount = _dec(price)
        if amount is None or amount <= 0 or not sleeve or not ticker:
            return None
        marks[(sleeve, ticker)] = amount
    return marks


def resolve_or_reuse(keys: list[tuple[str, str]], resolve, env: dict) -> tuple[dict, str]:
    """One quote set. The refresh file wins. Otherwise `resolve` is called once."""
    cached = cached_marks(keys)
    if cached is not None:
        return cached, "refresh"
    quotes = resolve(keys, env)
    marks = {}
    for key, value in quotes.items():
        price = value[0] if isinstance(value, tuple) else value
        marks[key] = price
    return marks, "quotes"


def cached_marks(keys: list[tuple[str, str]]) -> dict | None:
    """The saved quote set when it covers every open name. Otherwise None."""
    stored = load_marks()
    if stored is None:
        return None
    if any(key not in stored for key in keys):
        return None
    return {key: stored[key] for key in keys}


def cash_total(cash: dict | None) -> Decimal | None:
    """USD plus USDC. One line is enough. Neither line means the drop is missing."""
    if not isinstance(cash, dict):
        return None
    total = Decimal("0")
    present = False
    for key in CASH_KEYS:
        if key not in cash or cash.get(key) in (None, ""):
            continue
        amount = _dec(cash.get(key))
        if amount is None:
            continue
        total += amount
        present = True
    if not present:
        return None
    return total


def residual_tolerance(path: Path | None = None) -> Decimal:
    """Unexplained-residual warning line. Read from config, not a literal here."""
    config = path or CONFIG_PATH
    try:
        payload = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccountBookError(f"account tolerance config is unreadable: {config}") from exc
    if not isinstance(payload, dict) or "residual_tolerance_usd" not in payload:
        raise AccountBookError("account tolerance config needs residual_tolerance_usd")
    amount = _dec(payload.get("residual_tolerance_usd"))
    if amount is None or amount < 0:
        raise AccountBookError("residual_tolerance_usd must be a non-negative decimal")
    return amount


def read_cash(env: dict | None = None, data_dir: Path | None = None) -> tuple[dict | None, str | None]:
    """Robinhood REST when keys are set, otherwise the desk drop.

    A failed or incomplete REST read is logged and the desk drop is used.
    Cash is missing only when that drop fails too. Returns (None, None)
    in that case. Does not invent a balance.
    """
    import export_kpi

    try:
        rest = export_kpi.load_rh_cash(env)
    except RuntimeError as exc:
        print(
            f"Robinhood cash read failed ({exc}); falling back to data/rh_cash.json",
            file=sys.stderr,
        )
        rest = None
    if rest is not None:
        return rest, "robinhood"
    folder = data_dir if data_dir is not None else export_kpi.DATA
    drop = export_kpi.load_rh_cash_drop(folder)
    if drop is not None:
        return drop, "rh_cash.json"
    return None, None


def _refresh():
    import refresh_kpi_snapshots as refresh

    return refresh


def _marked_lots(book: dict, marks: dict) -> tuple[Decimal, Decimal, list[tuple[Decimal, Decimal]]]:
    """Open crypto lots, one quote set. Equities stay off the Agentic balance."""
    lots = Decimal("0")
    unreal = Decimal("0")
    pairs: list[tuple[Decimal, Decimal]] = []
    for (sleeve, ticker), pos in book.items():
        if sleeve != "crypto" or ticker in {"USD", "USDC"}:
            continue
        if (sleeve, ticker) not in marks:
            raise AccountBookError(f"open ticker has no mark: {sleeve} {ticker}")
        mark = marks[(sleeve, ticker)]
        qty = pos["qty"]
        value = qty * mark
        gap = qty * (mark - pos["avg"])
        lots += value
        unreal += gap
        pairs.append((value, gap))
    return lots, unreal, pairs


def _sleeve_unrealized(book: dict, marks: dict, sleeve: str) -> Decimal:
    total = Decimal("0")
    for (name, ticker), pos in book.items():
        if name != sleeve or ticker in {"USD", "USDC"}:
            continue
        if (name, ticker) not in marks:
            raise AccountBookError(f"open ticker has no mark: {name} {ticker}")
        total += pos["qty"] * (marks[(name, ticker)] - pos["avg"])
    return total


def publish_identity(
    cash: Decimal,
    lot_values: list[Decimal],
    lot_unreals: list[Decimal] | None,
    seed: Decimal,
) -> dict:
    """Cent fields the card and the snapshot both publish.

    Balance is cash plus each lot value, each line at cents, then the same
    final cent round the page uses. Realized is running minus unrealized so
    the published sum matches running P&L to the cent. Lot unrealized that
    is still unknown leaves realized and unrealized unset.
    """
    cash_cents = cents_half_up(cash)
    value_cents = [cents_half_up(value) for value in lot_values]
    balance = js_cents(cash_cents + sum(value_cents, Decimal("0")))
    running = js_cents(balance - seed)
    out = {
        "balance_exact": cash + sum(lot_values, Decimal("0")),
        "balance_cents": balance,
        "running_cents": running,
        "unrealized_cents": None,
        "realized_cents": None,
    }
    if lot_unreals is None:
        return out
    unreal = sum((cents_half_up(value) for value in lot_unreals), Decimal("0"))
    unreal = js_cents(unreal)
    out["unrealized_cents"] = unreal
    out["realized_cents"] = running - unreal
    return out


def statement(
    fills: list[dict],
    marks: dict,
    cash: dict | None,
    seed: Decimal | None = None,
    snapshot_marks: dict | None = None,
) -> dict:
    """One account reading from fills, one mark dict, and a cash drop.

    snapshot_marks is only for the mark-drift line. The published balance
    uses `marks`.
    """
    import book_seeds

    refresh = _refresh()
    held = cash_total(cash)
    if held is None:
        raise AccountBookError("cash drop is missing")
    if seed is None:
        seed = book_seeds.current_seeds()["combined"]
    realized_raw, book = refresh.apply_books(fills)
    trade_pnl = sum(realized_raw.values(), Decimal("0"))
    crypto_trade_pnl = realized_raw.get("crypto", Decimal("0"))
    equities_realized = realized_raw.get("equities", Decimal("0"))
    lots, unreal_exact, pairs = _marked_lots(book, marks)
    published = publish_identity(
        held,
        [value for value, _gap in pairs],
        [gap for _value, gap in pairs],
        seed,
    )
    audit = _audit(
        fills,
        held,
        seed,
        trade_pnl,
        crypto_trade_pnl,
        equities_realized,
        marks,
        snapshot_marks if snapshot_marks is not None else marks,
        published["balance_cents"],
    )
    return {
        "cash": held,
        "seed": seed,
        "lots_exact": lots,
        "unrealized_exact": unreal_exact,
        "balance_exact": published["balance_exact"],
        "balance_cents": published["balance_cents"],
        "running_cents": published["running_cents"],
        "unrealized_cents": published["unrealized_cents"],
        "realized_cents": published["realized_cents"],
        "running_exact": published["balance_exact"] - seed,
        "realized_exact": (published["balance_exact"] - seed) - unreal_exact,
        "trade_pnl": trade_pnl,
        "equities_realized": equities_realized,
        **audit,
    }


def _positive_fee(fill: dict) -> Decimal:
    fee = _dec(fill.get("fee_usd")) or Decimal("0")
    if fee < 0:
        return Decimal("0")
    return fee


def _notional(fill: dict, qty: Decimal, price: Decimal) -> Decimal:
    amount = _dec(fill.get("notional_usd"))
    if amount is None:
        return qty * price
    return amount


def _audit(
    fills: list[dict],
    cash: Decimal,
    seed: Decimal,
    trade_pnl: Decimal,
    crypto_trade_pnl: Decimal,
    equities_realized: Decimal,
    marks: dict,
    snapshot_marks: dict | None,
    published_balance: Decimal,
) -> dict:
    """A–G gap between the seed-anchored book and cash plus lots.

    The walk uses the same collapsed fills as the snapshot. Buy fees are the
    whole crypto buy fee, split into the piece still on open lots and the
    piece that left on sells. Rounding is the cent notional versus quantity
    times price. The basis line is the flat-close sliver outside the fee
    tolerance. Funding is the combined seed plus equities realized minus the
    cash that crypto trades do not explain. Residual is account realized
    minus crypto trade P&L, buy fees, and rounding. Equities closes stay
    out of that residual.
    """
    refresh = _refresh()
    collapsed, _dropped = refresh.collapse_duplicate_fills(fills)
    ordered = sorted(
        enumerate(collapsed),
        key=lambda item: (refresh.parse_ts(item[1]["timestamp_et"]), item[0]),
    )
    buy_fees = Decimal("0")
    buy_rounding = Decimal("0")
    sell_rounding = Decimal("0")
    basis_gap = Decimal("0")
    crypto_flows = Decimal("0")
    lots: dict[tuple[str, str], list[dict]] = {}
    for _, fill in ordered:
        sleeve = str(fill.get("sleeve") or "").strip().lower()
        if sleeve not in refresh.TRADE_SLEEVES:
            continue
        ticker = str(fill.get("ticker") or "").strip().upper()
        side = str(fill.get("side") or "").strip().lower()
        qty = _dec(fill.get("qty"))
        price = _dec(fill.get("avg_price"))
        if qty is None or price is None:
            continue
        fee = _positive_fee(fill)
        notional = _notional(fill, qty, price)
        gross = qty * price
        key = (sleeve, ticker)
        book_lots, open_qty = refresh._open_lots(lots.get(key))
        signed = qty if side == "buy" else -qty
        increasing = (signed > 0 and open_qty >= 0) or (signed < 0 and open_qty <= 0)
        if sleeve == "crypto":
            if side == "buy":
                buy_fees += fee
                buy_rounding += notional - gross
                crypto_flows -= notional + fee
            else:
                sell_rounding += gross - notional
                crypto_flows += notional - fee
        if increasing:
            if side == "buy":
                book_lots.append({"qty": signed, "px": price, "fee": fee})
            else:
                book_lots.append({"qty": signed, "px": price, "fee": Decimal("0")})
            lots[key] = book_lots
            continue
        stored = _dec(fill.get("pnl_trade_usd")) or Decimal("0")
        remaining = abs(signed)
        fifo_gross = Decimal("0")
        while remaining > refresh.DUST and book_lots:
            lot = book_lots[0]
            take = min(abs(lot["qty"]), remaining)
            share = (lot["fee"] * take / abs(lot["qty"])) if abs(lot["qty"]) > refresh.DUST else Decimal("0")
            lot["fee"] -= share
            if lot["qty"] > 0:
                fifo_gross += (price - lot["px"]) * take
                lot["qty"] -= take
            else:
                fifo_gross += (lot["px"] - price) * take
                lot["qty"] += take
            remaining -= take
            if abs(lot["qty"]) <= refresh.DUST:
                book_lots.pop(0)
        flat = refresh._lots_position(book_lots) is None
        if flat and sleeve == "crypto":
            mismatch = (fifo_gross - stored) - fee
            if abs((fifo_gross - stored) - fee) > refresh.BASIS_TOL:
                basis_gap += -mismatch
            lots.pop(key, None)
        else:
            lots[key] = book_lots
    open_buy_fees = Decimal("0")
    for (sleeve, _ticker), book_lots in lots.items():
        if sleeve != "crypto":
            continue
        for lot in book_lots:
            open_buy_fees += lot["fee"]
    closed_buy_fees = buy_fees - open_buy_fees
    net_funding = cash - crypto_flows
    funding = seed + equities_realized - net_funding
    _realized, live_book = refresh.apply_books(fills)
    live_crypto_u = _sleeve_unrealized(live_book, marks, "crypto") if live_book else Decimal("0")
    snap = snapshot_marks if snapshot_marks is not None else marks
    snap_crypto_u = _sleeve_unrealized(live_book, snap, "crypto") if live_book else Decimal("0")
    try:
        equity_u = _sleeve_unrealized(live_book, snap, "equities") if live_book else Decimal("0")
    except AccountBookError:
        equity_u = Decimal("0")
    mark_drift = snap_crypto_u - live_crypto_u
    balance_exact = cash + sum(
        (pos["qty"] * marks[(sleeve, ticker)])
        for (sleeve, ticker), pos in live_book.items()
        if sleeve == "crypto" and ticker not in {"USD", "USDC"}
    )
    cents_gap = balance_exact - published_balance
    warehouse = seed + trade_pnl + snap_crypto_u + equity_u
    gap = warehouse - published_balance
    rounding = buy_rounding + sell_rounding
    realized_exact = (balance_exact - seed) - live_crypto_u
    # Agentic realized does not include equities closes. Those stay in funding.
    residual = realized_exact - (crypto_trade_pnl - buy_fees - rounding)
    return {
        "buy_fees": buy_fees,
        "open_buy_fees": open_buy_fees,
        "closed_buy_fees": closed_buy_fees,
        "buy_rounding": buy_rounding,
        "sell_rounding": sell_rounding,
        "rounding": rounding,
        "basis_gap": basis_gap,
        "funding": funding,
        "mark_drift": mark_drift,
        "cents_gap": cents_gap,
        "equity_unrealized": equity_u,
        "gap": gap,
        "residual": residual,
        "components_sum": buy_fees + buy_rounding + sell_rounding + basis_gap + funding + mark_drift + cents_gap + equity_u,
    }


def decompose(
    fills: list[dict],
    marks: dict,
    cash: dict | None,
    seed: Decimal | None = None,
    snapshot_marks: dict | None = None,
) -> dict:
    """Gap breakdown. snapshot_marks is the other quote set; omit it when the marks are shared."""
    return statement(fills, marks, cash, seed, snapshot_marks=snapshot_marks)


def position_identity(positions: list | None, cash: dict | None, seed: Decimal) -> dict | None:
    """Published split from card rows and a cash drop. None when cash or a lot value is missing."""
    held = cash_total(cash)
    if held is None or not isinstance(positions, list):
        return None
    values: list[Decimal] = []
    unreals: list[Decimal] = []
    known = True
    for row in positions:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        sleeve = str(row.get("sleeve") or "crypto").strip().lower()
        if not ticker or ticker in {"USD", "USDC"} or sleeve != "crypto":
            continue
        qty = _dec(row.get("qty"))
        if qty is None or qty == 0:
            continue
        mark = _dec(row.get("mark"))
        avg = _dec(row.get("avg_cost"))
        if avg is None:
            avg = _dec(row.get("avg"))
        if qty is not None and mark is not None and avg is not None:
            values.append(qty * mark)
            unreals.append(qty * (mark - avg))
            continue
        value = _dec(row.get("value_usd"))
        if value is None:
            return None
        values.append(value)
        unreal = _dec(row.get("unrealized_pnl_usd"))
        if unreal is None:
            known = False
        else:
            unreals.append(unreal)
    published = publish_identity(held, values, unreals if known else None, seed)
    return published


def format_note(reading: dict) -> str:
    """Audit split for the combined snapshot. Sleeve rows are not this note."""
    return (
        "account cash+open lots; not a sleeve seed curve. "
        f"trade-list realized {_money(reading['trade_pnl'])}; "
        f"buy fees {_money(reading['buy_fees'])}; "
        f"rounding {_money(reading['rounding'])}; "
        f"residual {_money(reading['residual'])}"
    )


def _money(value: Decimal) -> str:
    quantized = cents_half_up(value)
    sign = "-" if quantized < 0 else ""
    return f"{sign}${abs(quantized):.2f}"


def absent_note() -> str:
    return (
        "cash drop absent; previous combined figures kept. "
        "This is not a seed+realized+unrealized fallback."
    )


def text_amount(value: Decimal) -> str:
    return format(_q6(value), "f")


def breakdown_payload(reading: dict, tolerance: Decimal) -> dict:
    residual = reading["residual"]
    over = abs(residual) > tolerance
    return {
        "mode": "account",
        "A_buy_fees_usd": text_amount(reading["buy_fees"]),
        "A_open_lot_buy_fees_usd": text_amount(reading["open_buy_fees"]),
        "A_closed_lot_buy_fees_usd": text_amount(reading["closed_buy_fees"]),
        "B_buy_rounding_usd": text_amount(reading["buy_rounding"]),
        "C_sell_rounding_usd": text_amount(reading["sell_rounding"]),
        "D_basis_gap_usd": text_amount(reading["basis_gap"]),
        "E_funding_usd": text_amount(reading["funding"]),
        "F_mark_drift_usd": text_amount(reading["mark_drift"]),
        "G_cents_rounding_usd": text_amount(reading["cents_gap"]),
        "equity_unrealized_usd": text_amount(reading["equity_unrealized"]),
        "gap_usd": text_amount(reading["gap"]),
        "components_sum_usd": text_amount(reading["components_sum"]),
        "trade_pnl_usd": text_amount(reading["trade_pnl"]),
        "balance_usd": text_amount(reading["balance_exact"]),
        "running_pnl_usd": text_amount(reading["running_exact"]),
        "unrealized_usd": text_amount(reading["unrealized_exact"]),
        "realized_usd": text_amount(reading["realized_exact"]),
        "unexplained_residual_usd": text_amount(residual),
        "tolerance_usd": text_amount(tolerance),
        "ok": not over,
        "lines": [
            f"A buy fees {text_amount(reading['buy_fees'])} "
            f"(open {text_amount(reading['open_buy_fees'])}, closed {text_amount(reading['closed_buy_fees'])})",
            f"B buy rounding {text_amount(reading['buy_rounding'])}",
            f"C sell rounding {text_amount(reading['sell_rounding'])}",
            f"D flat-close basis {text_amount(reading['basis_gap'])}",
            f"E funding {text_amount(reading['funding'])}",
            f"F mark drift {text_amount(reading['mark_drift'])}",
            f"G cents rounding {text_amount(reading['cents_gap'])}",
            f"gap {text_amount(reading['gap'])} components {text_amount(reading['components_sum'])}",
            f"residual {text_amount(residual)} tolerance {text_amount(tolerance)}",
        ],
    }
