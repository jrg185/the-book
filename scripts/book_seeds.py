"""Load config/book_seeds.json. That file is the only seed source."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "book_seeds.json"


def load_seed_rows(path: Path | None = None) -> list[dict]:
    """Every sleeve row, oldest effective date first."""
    payload = json.loads((path or CONFIG_PATH).read_text(encoding="utf-8"))
    rows = payload.get("seeds") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not rows:
        raise ValueError("config/book_seeds.json needs a non-empty seeds list")
    parsed = []
    seen: set[tuple[str, date]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("book seed row must be an object")
        sleeve = str(row.get("sleeve") or "").strip().lower()
        if not sleeve:
            raise ValueError("book seed row needs a sleeve")
        try:
            seed = Decimal(str(row.get("seed_usd")))
        except Exception as exc:
            raise ValueError(f"book seed for {sleeve} is not a decimal") from exc
        if seed <= 0:
            raise ValueError(f"book seed for {sleeve} must be positive")
        try:
            effective = date.fromisoformat(str(row.get("effective_from") or ""))
        except ValueError as exc:
            raise ValueError(f"book seed for {sleeve} needs effective_from") from exc
        key = (sleeve, effective)
        if key in seen:
            raise ValueError(f"duplicate book seed {sleeve} {effective.isoformat()}")
        seen.add(key)
        parsed.append({"sleeve": sleeve, "seed_usd": seed, "effective_from": effective})
    parsed.sort(key=lambda item: (item["sleeve"], item["effective_from"]))
    return parsed


def current_seeds(path: Path | None = None, on: date | None = None) -> dict[str, Decimal]:
    """Seed in force for each sleeve.

    `on` selects the latest effective_from on or before that date. Omit it to
    take the latest row, which is the seed the page and the export use.
    """
    chosen: dict[str, tuple[date, Decimal]] = {}
    for row in load_seed_rows(path):
        if on is not None and row["effective_from"] > on:
            continue
        prev = chosen.get(row["sleeve"])
        if prev is None or row["effective_from"] >= prev[0]:
            chosen[row["sleeve"]] = (row["effective_from"], row["seed_usd"])
    if not chosen:
        raise ValueError("no book seed is effective")
    return {sleeve: seed for sleeve, (_day, seed) in chosen.items()}


def ledger_seeds(path: Path | None = None, on: date | None = None) -> dict[str, Decimal]:
    """Trade-sleeve seeds. The combined book is not a fill sleeve."""
    return {name: seed for name, seed in current_seeds(path, on).items() if name != "combined"}
