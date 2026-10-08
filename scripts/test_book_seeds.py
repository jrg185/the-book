"""Loader, seed sync plan, and the guard against money seed literals."""

import json
import re
import sys
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

import book_seeds
import export_kpi
import refresh_kpi_snapshots
import sync_book_seeds

# Already-applied view. The live definition is 20261008_book_seeds.sql.
HISTORICAL_VIEW = Path("scripts/migrations/20260928_kpi_trades_running_ledger.sql")

MONEY_SEED = re.compile(
    r"""(?x)
    Decimal\(\s*['\"](?:300|500|800)(?:\.0+)?['\"]\s*\)
    | \$(?:300|800)\b
    | \$500(?![\d.])
    | \b(?:300|500|800)\s*::\s*numeric\b
    | (?<![\w.])(?<!\[:)(?:300|800)(?![\w.])
    | ['\"](?:300|800)['\"]
    """
)

SKIP_DIRS = {".git", "data", "fixtures", "node_modules", "__pycache__"}


def money_seed_hits(text: str) -> list[int]:
    return [match.start() for match in MONEY_SEED.finditer(text)]


def production_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix not in {".py", ".js", ".mjs", ".sql"}:
            continue
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py"):
            continue
        rel = path.relative_to(ROOT)
        if rel == HISTORICAL_VIEW:
            continue
        found.append(rel)
    return found


class BookSeedLoaderTests(unittest.TestCase):
    def test_current_file_is_the_export_and_refresh_table(self):
        loaded = book_seeds.current_seeds()
        self.assertEqual(set(loaded), {"crypto", "equities", "combined"})
        for seed in loaded.values():
            self.assertGreater(seed, 0)
        self.assertEqual(export_kpi.BOOK_SEEDS, loaded)
        self.assertEqual(export_kpi.LEDGER_SEEDS, book_seeds.ledger_seeds())
        self.assertNotIn("combined", export_kpi.LEDGER_SEEDS)
        self.assertEqual(refresh_kpi_snapshots.START, loaded)
        rows = book_seeds.load_seed_rows()
        by_sleeve = {row["sleeve"]: format(row["seed_usd"], "f") for row in rows}
        for sleeve, seed in loaded.items():
            self.assertEqual(by_sleeve[sleeve], format(seed, "f"))

    def test_latest_effective_row_wins_and_an_earlier_date_keeps_the_old_seed(self):
        payload = {
            "seeds": [
                {"sleeve": "crypto", "seed_usd": "10", "effective_from": "2020-01-01"},
                {"sleeve": "crypto", "seed_usd": "11", "effective_from": "2024-06-01"},
                {"sleeve": "equities", "seed_usd": "12", "effective_from": "2020-01-01"},
            ]
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "book_seeds.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            latest = book_seeds.current_seeds(path)
            earlier = book_seeds.current_seeds(path, on=date(2021, 1, 1))
        self.assertEqual(latest["crypto"], Decimal("11"))
        self.assertEqual(earlier["crypto"], Decimal("10"))
        self.assertEqual(earlier["equities"], Decimal("12"))

    def test_sync_plan_matches_the_file_and_does_not_connect(self):
        self.assertEqual(sync_book_seeds.self_test(), 0)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            self.assertEqual(sync_book_seeds.main(["--dry-run"]), 0)
            self.assertEqual(sync_book_seeds.main(["--self-test"]), 0)
        self.assertIn("on conflict (sleeve, effective_from)", sync_book_seeds.UPSERT_SQL.lower())

    def test_migration_joins_the_seed_table_and_does_not_insert(self):
        text = (ROOT / "scripts/migrations/20261008_book_seeds.sql").read_text(encoding="utf-8")
        self.assertIn("public.book_seeds", text)
        self.assertIn("left join lateral", text)
        self.assertNotIn("insert into public.book_seeds", text.lower())
        self.assertEqual(money_seed_hits(text), [])

    def test_scorecard_seed_comes_from_config(self):
        crypto = book_seeds.current_seeds()["crypto"]
        stats = export_kpi.closed_fill_stats(
            [{"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": "0.01"}]
        )
        self.assertEqual(stats["seed_usd"], int(crypto))
        unknown = export_kpi.unknown_fee("fixture")
        self.assertEqual(unknown["seed_usd"], int(crypto))

    def test_money_seed_literals_stay_out_of_production_code(self):
        self.assertTrue((ROOT / HISTORICAL_VIEW).is_file())
        hits = []
        for rel in production_files():
            text = (ROOT / rel).read_text(encoding="utf-8")
            for lineno, line in enumerate(text.splitlines(), start=1):
                if MONEY_SEED.search(line):
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
        self.assertEqual(hits, [])

    def test_slice_and_fee_residual_are_not_seed_literals(self):
        self.assertEqual(money_seed_hits('return "error", raw[:300], "stale-snapshot"'), [])
        self.assertEqual(money_seed_hits('fee == "14.800000"'), [])
        self.assertEqual(money_seed_hits("equities $500.87"), [])
        self.assertNotEqual(money_seed_hits('seed = Decimal("300")'), [])
        self.assertNotEqual(money_seed_hits("when 'crypto' then 300::numeric"), [])


if __name__ == "__main__":
    unittest.main()
