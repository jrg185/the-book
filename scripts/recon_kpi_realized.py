#!/usr/bin/env python3
"""Compare kpi_trades sells with a Robinhood per-trade JSON drop.

Read-only. This script never writes to the warehouse and never calls
Robinhood. It reads local JSON and writes only to stdout or a path you pass.

  python3 scripts/recon_kpi_realized.py --dry-run \\
      --kpi-json kpi_trades.json --rh-pnl-json rh_pnl.json \\
      --rh-orders-json rh_orders.json

  python3 scripts/recon_kpi_realized.py --kpi-json kpi_trades.json --plan-backfill

--dry-run is the default and the only mode. --plan-backfill emits the scrubbed
item-4 diff (fractions of the sleeve ledger seed, no dollars and no order ids).
The header note is: PLANNED ONLY - do not apply until Eng and Wags approve.

Matching: a warehouse sell's order_id selects the RH order, and that order
selects a pnl row with the same symbol and quantity and a timestamp within a
few minutes. Fees come from the order payload via fee_of() when orders are
provided, otherwise from the warehouse fee_usd. RH net is realized gain minus
that fee.

|warehouse pnl - RH net| > 0.01 is flagged. A fifo-dedup residual within 0.03
(and outside 0.01) is labeled rh_price_precision, not a lot error. NULL
order_id rows that twin an order_id fill are listed as legacy duplicates.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import book_seeds
import export_kpi
import sync_rh_kpi_trades as sync

PLAN_NOTE = "PLANNED ONLY - do not apply until Eng and Wags approve."
FLAG_TOL = Decimal("0.01")
# RH pnl-hub price sits slightly off the order average. Residuals inside this
# band are price precision, not a lot-matching error.
PRECISION_TOL = Decimal("0.03")
# Stored and proposed already agree. Diagnosis tolerance, not a price.
NO_CHANGE_TOL = Decimal("0.005")
MATCH_WINDOW = sync.dt.timedelta(minutes=5)

REASON_FEE = "fee_not_netted"
REASON_LATE_FEE = "late_fee_not_netted"
REASON_PHANTOM = "phantom_lots_from_legacy_duplicates"
REASON_AVG = "avg_cost_not_fifo"
REASON_ROUND = "rounding"
REASON_LEGACY = "legacy_duplicate"
ACTION_UPDATE = "UPDATE"
ACTION_KEEP = "NO_CHANGE"
ACTION_EXCLUDE = "EXCLUDE_LEGACY_DUPLICATE_PENDING_APPROVAL"

GROSS_KEYS = ("realized_gain", "realized_pnl", "realized_profit", "pnl", "profit_loss")
QTY_KEYS = ("quantity", "qty", "cumulative_quantity", "filled_asset_quantity")
TIME_KEYS = (
    "timestamp_et",
    "timestamp",
    "closed_at",
    "executed_at",
    "created_at",
    "updated_at",
    "time",
)


class ReconError(RuntimeError):
    """The JSON drop could not be reconciled. Nothing was written."""


def _ticker_of(row: dict) -> str:
    for key in ("ticker", "currency_code", "symbol", "instrument"):
        text = str(row.get(key) or "").strip().upper()
        if not text:
            continue
        if "-" in text:
            text = text.split("-", 1)[0]
        return text
    return ""


def _time_of(row: dict):
    for key in TIME_KEYS:
        if row.get(key):
            try:
                return sync.parse_ts(row[key])
            except (TypeError, ValueError):
                continue
    return None


def _first_dec(row: dict, keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        if key not in row or row.get(key) in (None, ""):
            continue
        amount = sync.dec(row.get(key))
        if amount is not None:
            return amount
    return None


def _seed(sleeve: str) -> Decimal:
    key = str(sleeve or "").strip().lower()
    seed = book_seeds.ledger_seeds().get(key)
    if seed is None or seed == 0:
        raise ReconError(f"no ledger seed for sleeve {sleeve!r}")
    return seed


def _frac(pnl: Decimal, sleeve: str) -> str:
    return format(export_kpi.q6(pnl / _seed(sleeve)), "f")


def _money(value: Decimal) -> str:
    return sync.num_text(value)


def _applied(rows: list[dict], *, net_fee: bool, skip_legacy: bool) -> list[dict]:
    priced = sync.replay_rows(rows, net_fee=net_fee, skip_legacy=skip_legacy)
    applied = [row for row in priced if not row.get("replay_skip")]
    applied.sort(key=lambda row: sync.parse_ts(row["timestamp_et"]))
    return applied


def _index_pnl(rows: list[dict]) -> dict[str, Decimal]:
    found: dict[str, Decimal] = {}
    for row in rows:
        key = str(row.get("id") or "").strip()
        if not key or row.get("replay_skip") or row.get("pnl_trade_usd") in (None, ""):
            continue
        amount = sync.dec(row.get("pnl_trade_usd"))
        if amount is not None:
            found[key] = amount
    return found


def _apply_average(book: dict, row: dict) -> Decimal:
    """Weighted-average gross, used only to tag avg_cost_not_fifo."""
    side = str(row["side"]).lower()
    qty = sync.dec(row["qty"])
    price = sync.dec(row["avg_price"])
    if qty is None or price is None:
        raise ReconError("average replay saw a row without qty or price")
    signed = qty if side == "buy" else -qty
    key = (row["sleeve"], row["ticker"])
    pos = book.get(key)
    open_qty = pos["qty"] if pos else Decimal("0")
    increasing = (signed > 0 and open_qty >= 0) or (signed < 0 and open_qty <= 0)
    if increasing:
        new_qty = open_qty + signed
        if abs(open_qty) <= sync.DUST:
            avg = price
        else:
            avg = (abs(open_qty) * pos["avg"] + abs(signed) * price) / abs(new_qty)
        book[key] = {"qty": new_qty, "avg": avg}
        return Decimal("0")
    if abs(signed) - abs(open_qty) > sync.DUST:
        raise ReconError(f"{row['sleeve']} {row['ticker']}: average replay oversold")
    if open_qty > 0:
        pnl = (price - pos["avg"]) * qty
    else:
        pnl = (pos["avg"] - price) * qty
    remain = abs(open_qty) - abs(signed)
    if remain <= sync.DUST:
        book.pop(key, None)
    else:
        sign = Decimal("1") if open_qty > 0 else Decimal("-1")
        book[key] = {"qty": sign * remain, "avg": pos["avg"]}
    return pnl


def _average_by_id(rows: list[dict]) -> dict[str, Decimal]:
    book: dict = {}
    found: dict[str, Decimal] = {}
    for row in _applied(rows, net_fee=False, skip_legacy=True):
        pnl = _apply_average(book, row)
        key = str(row.get("id") or "").strip()
        if key:
            found[key] = pnl
    return found


def _reasons(
    stored: Decimal,
    proposed: Decimal,
    fee: Decimal,
    dirty: Decimal | None,
    average: Decimal | None,
) -> list[str]:
    reasons: list[str] = []
    gross = proposed + fee
    material = abs(stored - proposed) > NO_CHANGE_TOL
    # Stored pnl already matches FIFO gross. The whole gap is a fee that was
    # not on the row when it was priced. The live sync must not rewrite it;
    # the approved backfill is the only writer.
    if fee > 0 and material and abs(stored - gross) <= NO_CHANGE_TOL:
        reasons.append(REASON_LATE_FEE)
    elif fee > 0 and material and abs(stored - gross) <= abs(stored - proposed) + NO_CHANGE_TOL:
        reasons.append(REASON_FEE)
    if dirty is not None and abs(dirty - proposed) > NO_CHANGE_TOL:
        reasons.append(REASON_PHANTOM)
    if average is not None and abs(average - gross) > NO_CHANGE_TOL:
        reasons.append(REASON_AVG)
    if not reasons:
        reasons.append(REASON_ROUND)
    return reasons


def plan_backfill(rows: list[dict]) -> dict:
    """Scrubbed planned diff. Fractions only. Does not write the warehouse."""
    priced = sync.replay_rows(rows, net_fee=True, skip_legacy=True)
    clean = _index_pnl(priced)
    dirty = _index_pnl(sync.replay_rows(rows, net_fee=True, skip_legacy=False))
    average = _average_by_id(rows)
    planned = []
    for original, replayed in zip(rows, priced):
        if replayed.get("replay_skip") == "duplicate_order_id":
            continue
        side = str(original.get("side") or "").lower()
        legacy = replayed.get("replay_skip") == "legacy_duplicate"
        if not legacy and side != "sell":
            continue
        sleeve = str(original.get("sleeve") or "").strip().lower()
        trade_id = str(original.get("id") or "").strip()
        if not trade_id:
            raise ReconError("plan row is missing kpi_trades id")
        stored = sync.dec(original.get("pnl_trade_usd"))
        if stored is None:
            stored = Decimal("0")
        entry = {
            "kpi_trades_id": trade_id,
            "timestamp_et": original.get("timestamp_et"),
            "ticker": original.get("ticker"),
            "side": side,
            "old_pnl_frac_of_book": _frac(stored, sleeve),
            "new_pnl_frac_of_book": None,
            "delta_frac": None,
            "reasons": [REASON_LEGACY] if legacy else [],
            "action": ACTION_EXCLUDE if legacy else ACTION_UPDATE,
        }
        if not legacy:
            proposed = clean.get(trade_id)
            if proposed is None:
                raise ReconError(f"sell {trade_id} was not priced")
            fee = sync._fee_amount(original)
            entry["reasons"] = _reasons(stored, proposed, fee, dirty.get(trade_id), average.get(trade_id))
            entry["action"] = ACTION_KEEP if abs(stored - proposed) <= NO_CHANGE_TOL else ACTION_UPDATE
            entry["new_pnl_frac_of_book"] = _frac(proposed, sleeve)
            old_q = export_kpi.q6(stored / _seed(sleeve))
            new_q = export_kpi.q6(proposed / _seed(sleeve))
            entry["delta_frac"] = format(export_kpi.q6(new_q - old_q), "f")
        planned.append(entry)
    planned.sort(
        key=lambda row: (
            str(row.get("timestamp_et") or ""),
            str(row.get("ticker") or ""),
            str(row.get("side") or ""),
            str(row.get("kpi_trades_id") or ""),
        )
    )
    counts = {ACTION_UPDATE: 0, ACTION_KEEP: 0, ACTION_EXCLUDE: 0}
    for row in planned:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    return {
        "note": PLAN_NOTE,
        "fractions_of": "config/book_seeds.json for the row sleeve",
        "counts": counts,
        "rows": planned,
    }


def _order_index(orders: list[dict]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for order in orders:
        order_id = str(order.get("id") or order.get("order_id") or "").strip().lower()
        if order_id and order_id not in found:
            found[order_id] = order
    return found


def _order_ticker(order: dict) -> str:
    return _ticker_of(order)


def _order_qty(order: dict) -> Decimal | None:
    return sync.qty_of(order)


def _order_time(order: dict):
    try:
        return sync.parse_ts(sync.fill_time(order))
    except sync.SyncError:
        return _time_of(order)


def _match_pnl(order: dict, pnl_rows: list[dict], ticker: str, qty: Decimal, when) -> dict | None:
    order_id = str(order.get("id") or order.get("order_id") or "").strip().lower()
    candidates = []
    for pnl in pnl_rows:
        pticker = _ticker_of(pnl)
        if pticker and ticker and pticker != ticker:
            continue
        pq = _first_dec(pnl, QTY_KEYS)
        if pq is None or abs(abs(pq) - abs(qty)) > sync.DUST:
            continue
        pt = _time_of(pnl)
        if when is None or pt is None or abs(pt - when) > MATCH_WINDOW:
            continue
        candidates.append(pnl)
    if not candidates:
        return None
    linked = [
        pnl
        for pnl in candidates
        if order_id and str(pnl.get("order_id") or "").strip().lower() == order_id
    ]
    pool = linked or candidates

    def gap(pnl: dict):
        return abs(_time_of(pnl) - when)

    return min(pool, key=gap)


def _residual_class(fifo_net: Decimal, rh_net: Decimal) -> str:
    gap = abs(fifo_net - rh_net)
    if gap <= FLAG_TOL:
        return "method_agrees"
    if gap <= PRECISION_TOL:
        return "rh_price_precision"
    return "unexplained"


def reconcile(rows: list[dict], pnl_rows: list[dict], orders: list[dict] | None) -> dict:
    """Per-sell warehouse vs RH report. orders may be omitted; fees then come from the row."""
    orders = orders or []
    by_order = _order_index(orders)
    clean = _index_pnl(sync.replay_rows(rows, net_fee=True, skip_legacy=True))
    priced = sync.replay_rows(rows, net_fee=True, skip_legacy=True)
    matched = []
    unmatched = []
    legacy = []
    used_pnl: set[int] = set()
    for original, replayed in zip(rows, priced):
        if replayed.get("replay_skip") == "duplicate_order_id":
            continue
        side = str(original.get("side") or "").lower()
        if replayed.get("replay_skip") == "legacy_duplicate":
            legacy.append(
                {
                    "kpi_trades_id": original.get("id"),
                    "timestamp_et": original.get("timestamp_et"),
                    "ticker": original.get("ticker"),
                    "side": side,
                    "class": "legacy_duplicate",
                }
            )
            continue
        if side != "sell":
            continue
        order_id = sync.order_id_of(original)
        trade_id = str(original.get("id") or "").strip()
        warehouse = sync.dec(original.get("pnl_trade_usd"))
        if warehouse is None:
            warehouse = Decimal("0")
        fifo_net = clean.get(trade_id)
        order = by_order.get(order_id) if order_id else None
        if order is None and orders:
            unmatched.append(
                {
                    "kpi_trades_id": trade_id,
                    "ticker": original.get("ticker"),
                    "timestamp_et": original.get("timestamp_et"),
                    "reason": "no_rh_order",
                }
            )
            continue
        if order is not None:
            ticker = _order_ticker(order) or _ticker_of(original)
            qty = _order_qty(order)
            when = _order_time(order)
            fee = sync.fee_of(order)
            fee_source = "rh_order"
        else:
            ticker = _ticker_of(original)
            qty = sync.dec(original.get("qty"))
            when = _time_of(original)
            fee = sync._fee_amount(original)
            fee_source = "warehouse_fee_usd"
            order = {"id": order_id, "order_id": order_id}
        if qty is None or when is None:
            unmatched.append(
                {
                    "kpi_trades_id": trade_id,
                    "ticker": ticker,
                    "timestamp_et": original.get("timestamp_et"),
                    "reason": "sell_missing_qty_or_time",
                }
            )
            continue
        # When the caller passed orders, match through that order. When they
        # did not, match the warehouse sell itself on symbol, qty, and time.
        probe = order if orders else original
        pnl = _match_pnl(probe, pnl_rows, ticker, qty, when)
        if pnl is None or id(pnl) in used_pnl:
            unmatched.append(
                {
                    "kpi_trades_id": trade_id,
                    "ticker": ticker,
                    "timestamp_et": original.get("timestamp_et"),
                    "reason": "no_pnl_row",
                }
            )
            continue
        used_pnl.add(id(pnl))
        gross = _first_dec(pnl, GROSS_KEYS)
        if gross is None:
            unmatched.append(
                {
                    "kpi_trades_id": trade_id,
                    "ticker": ticker,
                    "timestamp_et": original.get("timestamp_et"),
                    "reason": "pnl_row_missing_realized_gain",
                }
            )
            continue
        rh_net = gross - fee
        diff = warehouse - rh_net
        residual = None if fifo_net is None else fifo_net - rh_net
        entry = {
            "kpi_trades_id": trade_id,
            "order_id": order_id or None,
            "ticker": ticker,
            "timestamp_et": original.get("timestamp_et"),
            "warehouse_pnl": _money(warehouse),
            "rh_gross": _money(gross),
            "fee": _money(fee),
            "fee_source": fee_source,
            "rh_net": _money(rh_net),
            "diff": _money(diff),
            "flagged": abs(diff) > FLAG_TOL,
            "fifo_net": None if fifo_net is None else _money(fifo_net),
            "fifo_residual": None if residual is None else _money(residual),
            "residual_class": None if residual is None else _residual_class(fifo_net, rh_net),
        }
        matched.append(entry)
    precision = [row for row in matched if row["residual_class"] == "rh_price_precision"]
    flagged = [row for row in matched if row["flagged"]]
    return {
        "note": "read-only dry-run; nothing was written",
        "dry_run": True,
        "flag_tolerance": _money(FLAG_TOL),
        "price_precision_tolerance": _money(PRECISION_TOL),
        "matched_sells": matched,
        "flagged": flagged,
        "price_precision": precision,
        "legacy_duplicates": legacy,
        "unmatched_sells": unmatched,
        "counts": {
            "matched": len(matched),
            "flagged": len(flagged),
            "price_precision": len(precision),
            "legacy_duplicates": len(legacy),
            "unmatched": len(unmatched),
        },
    }


def _emit(payload: dict, path: str | None) -> None:
    text = json.dumps(payload, indent=2) + "\n"
    if path:
        Path(path).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kpi-json", required=True, help="kpi_trades JSON drop (SELECT * rows).")
    parser.add_argument("--rh-pnl-json", help="get_pnl_trade_history trades JSON.")
    parser.add_argument("--rh-orders-json", help="Optional get_crypto_orders JSON, used for fees and the order match.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Read-only. Default. Writes nothing except stdout or --output / --plan-output.",
    )
    parser.add_argument("--output", help="Write the recon report here instead of stdout.")
    parser.add_argument("--plan-backfill", action="store_true", help="Emit the scrubbed planned diff.")
    parser.add_argument("--plan-output", help="Write the planned diff here instead of stdout.")
    args = parser.parse_args(argv)
    if not args.rh_pnl_json and not args.plan_backfill:
        print("Pass --rh-pnl-json, --plan-backfill, or both. Nothing was written.", file=sys.stderr)
        return 1
    try:
        rows = sync.load_row_document(args.kpi_json)
        report = None
        if args.rh_pnl_json:
            pnl_rows = sync.load_row_document(args.rh_pnl_json)
            order_rows = sync.load_row_document(args.rh_orders_json) if args.rh_orders_json else []
            report = reconcile(rows, pnl_rows, order_rows if args.rh_orders_json else None)
        plan = plan_backfill(rows) if args.plan_backfill else None
    except (sync.SyncError, ReconError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if report is not None and plan is not None and not args.output and not args.plan_output:
        _emit({"report": report, "backfill": plan}, None)
    else:
        if report is not None:
            _emit(report, args.output)
        if plan is not None:
            _emit(plan, args.plan_output)
    print("dry-run: no warehouse writes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
