#!/usr/bin/env python3
"""Upsert Robinhood fills into kpi_trades over the Supabase REST API.

Desk or the box runs this. No Postgres URL is needed. Set
SIGNALS_SUPABASE_SERVICE_ROLE_KEY (preferred) or SUPABASE_SERVICE_ROLE_KEY.
SUPABASE_URL defaults to the agentic-signals project. The fills file stays on
that machine. Then dispatch Export KPI with no inputs:

  python3 scripts/upsert_fills_direct.py fills.json
  gh workflow run export-kpi.yml --repo jrg185/the-book

Writes are POST /rest/v1/kpi_trades?on_conflict=order_id with
Prefer: resolution=merge-duplicates. Existing rows are read over REST to seed
the FIFO pricer in scripts/sync_rh_kpi_trades.py. psycopg is used only when
SUPABASE_DB_URL is a real postgres URL. This script prints counts and
ticker/side/qty/pnl only. It never prints an order id or a key.
"""

from __future__ import annotations

import argparse
import sys

import sync_rh_kpi_trades as sync


def _counts(label: str, fetched: int, upserted: int, noted: int, fees: int) -> str:
    return (
        f"{label} fetched={fetched} upserted={upserted} noted={noted} fee_backfill={fees}"
    )


def _row_line(row: dict) -> str:
    pnl = row.get("pnl_trade_usd")
    return (
        f"  {row.get('ticker')} {row.get('side')} qty={row.get('qty')} "
        f"avg={row.get('avg_price')} pnl={'not priced' if pnl is None else pnl}"
    )


def dry_run(fills: str, env: dict[str, str], offline: bool, reprice: bool) -> int:
    orders = sync.load_fills(fills)
    existing = None
    source = "none"
    if not offline and (env.get("SUPABASE_SERVICE_ROLE_KEY") or env.get("SUPABASE_DB_URL")):
        existing, source = sync.load_trades(env)
        if reprice:
            ids = {sync.order_id_of(row) for row in sync.map_orders(orders)}
            existing = [row for row in existing if sync.order_id_of(row) not in ids]
    mapped = sync.dry_run_rows(orders, existing)
    print(_counts(f"dry-run warehouse={source}", len(orders), len(mapped), 0, 0))
    for row in mapped:
        print(_row_line(row))
    return 0


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "fills",
        help="Fills JSON file, or - for stdin. One order, a list, or results/data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read stored rows over REST, map and price, write nothing.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="With --dry-run: do not read the warehouse; sells are not priced.",
    )
    parser.add_argument(
        "--reprice",
        action="store_true",
        help="With --dry-run: price fills already stored as if new (drops them from the seed).",
    )
    args = parser.parse_args(argv)
    env = sync.env_values() if env is None else env
    try:
        if args.dry_run:
            return dry_run(args.fills, env, args.offline, args.reprice)
        return sync.sync(from_json=args.fills, env=env)
    except sync.SyncError as exc:
        print(sync.public_sync_message(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
