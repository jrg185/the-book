#!/usr/bin/env python3
"""Upsert Robinhood fills into kpi_trades without a public Actions payload.

Desk or the box runs this. SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must
be set. The fills file stays on that machine. Then dispatch Export KPI
with no inputs:

  python3 scripts/upsert_fills_direct.py fills.json
  gh workflow run export-kpi.yml --repo jrg185/the-book

The mapper, FIFO price, and order-id conflict key are
scripts/sync_rh_kpi_trades.py. A repeated order id does not insert again.
This script prints counts only. It does not print an order id.
"""

from __future__ import annotations

import argparse
import sys

import sync_rh_kpi_trades as sync


def _counts(label: str, fetched: int, upserted: int, noted: int, fees: int) -> str:
    return (
        f"{label} fetched={fetched} upserted={upserted} noted={noted} fee_backfill={fees}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "fills",
        help="Fills JSON file, or - for stdin. One order, a list, or results/data.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Map and price nothing in the warehouse. Prints counts only.",
    )
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            orders = sync.load_fills(args.fills)
            mapped = sync.dry_run_rows(orders, None)
            print(_counts("dry-run", len(orders), len(mapped), 0, 0))
            return 0
        return sync.sync(from_json=args.fills)
    except sync.SyncError as exc:
        print(sync.public_sync_message(str(exc)), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
