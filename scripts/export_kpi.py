#!/usr/bin/env python3
"""Export scrubbed Supabase KPI views into data/*.json for GitHub Pages.

The browser never sees this script's credentials. Set:

  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY   PostgREST read of the public views

SUPABASE_URL defaults to the agentic-signals project when unset.
ALPHA_VANTAGE_API_KEY and FINNHUB_API_KEY are not read here. Marks are
applied by scripts/refresh_kpi_snapshots.py before this export.
With no service role key, the script leaves the committed KPI JSON
in place, stamps data/meta.json with export_status "stale", and exits 0,
unless KPI_REFRESH_EXPECTED=1. In that case a missing Supabase credential
or the latest kpi_summary.as_of per sleeve older than 15 minutes exits
non-zero, stamps meta.json, and does not rewrite KPI numbers. Older
snapshots for the same sleeve are ignored. It does not rewrite data/models.json.

Robinhood cash is separate. Unset RH_API_KEY or RH_BASE64_PRIVATE_KEY skips
the signed REST read and does not fail the run, even when KPI_REFRESH_EXPECTED
is set. Export then reads data/rh_cash.json. A missing or invalid drop leaves
the USD and USDC lines already on data/live_book.json. Live balances are not
hardcoded in this script.

  python3 scripts/export_kpi.py --self-test

Views (fraction / percent rails; no PII):
  public.kpi_summary
  public.kpi_trades_scrubbed

Derived, scrubbed before they are written (no raw dollar columns, no account ids):
  public.kpi_trades → data/open_positions.json
    Net open qty by ticker and sleeve, marked with the same public quotes as
    scripts/refresh_kpi_snapshots.py. unrealized_pnl_frac is that P&L ÷ sleeve seed.
  public.kpi_sleeve_snapshots → data/sleeve_curves.json
    History as fractions of the book seed in config/book_seeds.json.

Optional, written when the view exists and skipped when it does not:
  public.models_oos

Derived for the Models tab, from the exported tape plus committed model files:
  data/model_scorecard.json
    Crypto live backend, closed-fill win rate, fee drag, kill headroom, and
    the 30 bp OOS table. Fee dollars stay UNKNOWN when the warehouse has no
    fee column. Order ids are not written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from book_seeds import current_seeds

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
FIXTURES = ROOT / "fixtures"

DEFAULT_URL = "https://bsnqwgbshwszbjncglqx.supabase.co"
DEFAULT_BUCKET = "model-weights"
PROJECT_REF = "bsnqwgbshwszbjncglqx"
MAX_BUCKET_JSON = 200_000
VIEWS = ("kpi_summary", "kpi_trades_scrubbed")
OPTIONAL_VIEWS = ("models_oos",)

# Dropped before JSON is written into the public repo. Financial fraction
# columns are kept. Seed columns (start / seed / book_usd) are kept.
DENY_KEYS = {
    "email",
    "phone",
    "order_id",
    "account_id",
    "user_id",
    "api_key",
    "service_role",
    "secret",
    "password",
    "token",
    "ssn",
    "address",
}

# Book seeds from config/book_seeds.json. Curves and open P&L use these divisors,
# not a raw account balance, so the page can show seed × fraction.
BOOK_SEEDS = current_seeds()
SEEDS = {name: BOOK_SEEDS[name] for name in ("crypto", "equities")}
SNAPSHOT_USD = (
    ("realized_pnl_usd", "realized_pnl_frac"),
    ("unrealized_pnl_usd", "unrealized_pnl_frac"),
    ("running_pnl_usd", "running_pnl_frac"),
    ("running_balance_usd", "running_balance_frac"),
    ("day_pnl_usd", "day_pnl_frac"),
)


def frac(dollars: str | Decimal, seed: Decimal) -> float:
    quant = (Decimal(dollars) / seed).quantize(Decimal("0.0000000001"))
    return float(quant)


def book_frac(running_pnl: str | Decimal, seed: Decimal) -> float:
    """Sleeve book / start. Interim book is start + running P&L, never cash residual."""
    if seed == 0:
        raise ValueError("seed is zero")
    return frac(seed + Decimal(running_pnl), seed)


def sheet_frac(dollars: str | Decimal, seed: Decimal) -> float:
    """Book or P&L ÷ start, rounded to 6 decimals."""
    if seed == 0:
        raise ValueError("seed is zero")
    quant = (Decimal(dollars) / seed).quantize(Decimal("0.000001"))
    return float(quant)


# Live public.kpi_summary uses warehouse names. The page reads the right-hand names.
# running_bal_vs_start is book/start and wins over a cash residual still called
# running_balance_frac.
SUMMARY_REMAP = (
    ("running_bal_vs_start", "running_balance_frac"),
    ("pnl_pct_of_book", "running_pnl_frac"),
    ("notes", "note"),
)


def _as_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def replace_cash_balance(row: dict) -> dict:
    """Turn a cash residual into book/start when the warehouse book column is absent.

    Book ≈ 1 + running P&L. Cash leftovers (crypto −0.023, equities 0.7) sit below 0.95.
    """
    pnl = _as_float(row.get("running_pnl_frac"))
    if pnl is None:
        return row
    book = float((Decimal(str(pnl)) + 1).quantize(Decimal("0.000001")))
    bal = _as_float(row.get("running_balance_frac"))
    if bal is None or bal < 0.95:
        row["running_balance_frac"] = book
    return row


def reshape_summary_row(row: dict) -> dict:
    """Map a live kpi_summary row onto the page contract."""
    if not isinstance(row, dict):
        return row
    had_warehouse_book = row.get("running_bal_vs_start") is not None
    out = dict(row)
    for src, dest in SUMMARY_REMAP:
        if src not in out:
            continue
        if out[src] is not None:
            out[dest] = out[src]
        del out[src]
    for key in ("running_balance_frac", "running_pnl_frac"):
        value = _as_float(out.get(key))
        if value is not None:
            out[key] = float(Decimal(str(value)).quantize(Decimal("0.000001")))
    if not had_warehouse_book:
        out = replace_cash_balance(out)
    return out


def reshape_trade_row(row: dict) -> dict:
    """Keep the row. Running ledger is applied later by attach_running_ledger."""
    if not isinstance(row, dict):
        return row
    return dict(row)


# Tape ledger seeds. Same file as BOOK_SEEDS. The combined book is not a fill sleeve.
LEDGER_SEEDS = {name: seed for name, seed in BOOK_SEEDS.items() if name != "combined"}


def _crypto_seed() -> Decimal:
    seed = BOOK_SEEDS.get("crypto")
    if seed is None or seed == 0:
        raise RuntimeError("crypto book seed is missing from config/book_seeds.json")
    return seed
LEDGER_QUANT = Decimal("0.000001")
MACHINE_WHY = re.compile(
    r"^RH Agentic (?:backfill|sync) order "
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def q6(value: Decimal) -> Decimal:
    """Six-decimal fraction. Half away from zero, matching Postgres round(numeric, 6)."""
    return value.quantize(LEDGER_QUANT, rounding=ROUND_HALF_UP)


def _decimal_or_none(value):
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _text_or_none(value):
    if value is None:
        return None
    return str(value)


def full_why(row: dict):
    """Return the full note. Never slices.

    A machine `RH Agentic backfill|sync order <uuid>` yields to a human notes
    field when that field is present. A why that is only a prefix of notes
    (view left()) yields to the longer notes text.
    """
    if not isinstance(row, dict):
        return None
    why_text = _text_or_none(row.get("why"))
    notes_value = row.get("notes") if "notes" in row else row.get("note")
    notes_text = _text_or_none(notes_value)
    why_stripped = "" if why_text is None else why_text.strip()
    notes_stripped = "" if notes_text is None else notes_text.strip()
    notes_human = bool(notes_stripped) and not MACHINE_WHY.match(notes_stripped)
    if notes_human and (
        MACHINE_WHY.match(why_stripped)
        or (why_stripped and notes_text.startswith(why_text) and len(notes_text) > len(why_text))
    ):
        return notes_text
    if why_text is not None:
        return why_text
    if notes_human:
        return notes_text
    return None


def _trade_pnl_usd(row: dict, seed: Decimal, use_dollars: bool) -> Decimal:
    if use_dollars:
        pnl = _decimal_or_none(row.get("pnl_trade_usd"))
        if pnl is None:
            pnl = _decimal_or_none(row.get("pnl_usd"))
        if pnl is not None:
            return pnl
    frac = _decimal_or_none(row.get("pnl_frac_of_book"))
    if frac is None:
        frac = _decimal_or_none(row.get("pnl_frac"))
    if frac is None:
        return Decimal("0")
    return frac * seed


def attach_running_ledger(rows: list) -> list:
    """Regenerate running realized P&L and book balance per sleeve.

    Chronological (timestamp, ticker, side, original index). Book at the fill
    is seed + cumulative pnl_trade_usd, as a fraction of the sleeve seed.
    Opening fills with a null trade P&L count as zero. Does not read a sheet.
    Overwrites any running_* the view already sent. Does not truncate why.
    """
    out = [dict(row) if isinstance(row, dict) else row for row in rows]
    grouped: dict[str, list[int]] = {}
    for index, row in enumerate(out):
        if not isinstance(row, dict):
            continue
        sleeve = str(row.get("sleeve") or "").strip().lower()
        grouped.setdefault(sleeve, []).append(index)
    for sleeve, indexes in grouped.items():
        seed = LEDGER_SEEDS.get(sleeve)
        if seed is None or seed == 0:
            continue
        use_dollars = any(
            _decimal_or_none(out[i].get("pnl_trade_usd")) is not None
            or _decimal_or_none(out[i].get("pnl_usd")) is not None
            for i in indexes
        )
        ordered = sorted(
            indexes,
            key=lambda i: (
                str(out[i].get("timestamp_et") or out[i].get("ts") or ""),
                str(out[i].get("ticker") or ""),
                str(out[i].get("side") or ""),
                i,
            ),
        )
        cum = Decimal("0")
        for i in ordered:
            row = out[i]
            cum += _trade_pnl_usd(row, seed, use_dollars)
            running = q6(cum / seed)
            balance = q6((seed + cum) / seed)
            row["running_pnl_frac"] = float(running)
            row["running_balance_frac"] = float(balance)
            why = full_why(row)
            if why is not None:
                row["why"] = why
    return out


def normalize_sleeve(value) -> str | None:
    key = str(value or "").strip().lower()
    if key == "equity":
        return "equities"
    if key in BOOK_SEEDS:
        return key
    return None


def _ratio(dollars: Decimal, seed: Decimal) -> float:
    return float(q6(dollars / seed))


def scrub_snapshot_history(rows: list) -> list:
    """Warehouse sleeve snapshots as fractions of the book seed.

    Copies only sleeve, as_of, and fraction fields. Dollar columns, notes, and
    account ids are not written. A row with no book fraction is skipped so the
    chart does not invent a point.
    """
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sleeve = normalize_sleeve(row.get("sleeve"))
        as_of = row.get("as_of")
        seed = BOOK_SEEDS.get(sleeve) if sleeve else None
        if sleeve is None or seed is None or not as_of:
            continue
        item = {"sleeve": sleeve, "as_of": jsonable(as_of)}
        for src, dest in SNAPSHOT_USD:
            dollars = _decimal_or_none(row.get(src))
            if dollars is not None:
                item[dest] = _ratio(dollars, seed)
                continue
            existing = _decimal_or_none(row.get(dest))
            if existing is not None:
                item[dest] = float(q6(existing))
        if "running_balance_frac" not in item and "running_pnl_frac" in item:
            item["running_balance_frac"] = float(q6(Decimal("1") + Decimal(str(item["running_pnl_frac"]))))
        if "running_balance_frac" not in item:
            continue
        out.append(item)
    out.sort(key=lambda item: (str(item["as_of"]), item["sleeve"]))
    return out


def scrub_open_positions(fills: list, marks: dict, as_of: str) -> dict:
    """Net open qty by ticker and sleeve. Unrealized is P&L ÷ sleeve seed.

    `marks` are prices from the same quote path as refresh_kpi_snapshots.
    A missing mark is an error. The JSON has avg and mark prices plus qty,
    and does not carry warehouse dollar columns or account ids.
    """
    import refresh_kpi_snapshots as refresh

    try:
        _realized, book = refresh.apply_books(fills)
    except refresh.RefreshError as exc:
        raise RuntimeError(str(exc)) from None
    missing = [f"{sleeve} {ticker}" for sleeve, ticker in sorted(book) if (sleeve, ticker) not in marks]
    if missing:
        raise RuntimeError("open tickers have no mark: " + ", ".join(missing))
    order = {"crypto": 0, "equities": 1}
    positions = []
    for (sleeve, ticker), pos in sorted(book.items(), key=lambda item: (order.get(item[0][0], 9), item[0][1])):
        qty = pos["qty"]
        if abs(qty) <= refresh.DUST:
            continue
        seed = BOOK_SEEDS.get(sleeve)
        if seed is None:
            raise RuntimeError(f"open sleeve {sleeve} has no book seed")
        avg = pos["avg"]
        mark = Decimal(str(marks[(sleeve, ticker)]))
        unreal = qty * (mark - avg)
        positions.append(
            scrub_row(
                {
                    "sleeve": sleeve,
                    "ticker": ticker,
                    "side": "long" if qty > 0 else "short",
                    "qty": format(abs(qty), "f"),
                    "avg": format(avg, "f"),
                    "mark": format(mark, "f"),
                    "unrealized_pnl_frac": _ratio(unreal, seed),
                }
            )
        )
    return {"as_of": as_of, "positions": positions}


def sample_bundle() -> dict:
    """Stand-in rows in the scrubbed view shape. Not a live fetch.

    running_balance_frac is sleeve book / start. Until true mark-to-market,
    book = start + running P&L. Cash left after a fill is not the balance.
    """
    crypto = SEEDS["crypto"]
    equities = SEEDS["equities"]
    combined = BOOK_SEEDS["combined"]
    # Crypto Desk rebuild: book is start plus running P&L (realized plus uPnL).
    # Equities and combined books are the same sheet figures.
    crypto_pnl = "23.77"
    equities_pnl = "0.87"
    combined_pnl = "24.64"
    as_of = "2026-09-27T22:06:00-04:00"
    summary = [
        {
            "sleeve": "crypto",
            "as_of": as_of,
            "running_balance_frac": sheet_frac("323.77", crypto),
            "running_pnl_frac": sheet_frac(crypto_pnl, crypto),
            "day_pnl_frac": None,
            "day_kill_pct": -0.10,
            "kill_headroom_frac": frac("30", crypto),
            "day_target_pct": 0.025,
            "note": "Realized +$6.24. Book is start + realized + uPnL. Day target is realized-only.",
        },
        {
            "sleeve": "equities",
            "as_of": as_of,
            "running_balance_frac": sheet_frac("500.87", equities),
            "running_pnl_frac": sheet_frac(equities_pnl, equities),
            "day_pnl_frac": None,
            "day_kill_pct": -0.25,
            "kill_headroom_frac": frac("125", equities),
            "day_target_pct": None,
            "note": "Rails are percent of book.",
        },
        {
            "sleeve": "combined",
            "as_of": as_of,
            "running_balance_frac": sheet_frac("824.64", combined),
            "running_pnl_frac": sheet_frac(combined_pnl, combined),
            "day_pnl_frac": 0,
            "day_kill_pct": None,
            "kill_headroom_frac": frac("155", combined),
            "day_target_pct": None,
            "note": "Per-sleeve kill rails. Crypto day target is realized-only.",
        },
    ]
    # pnl and running P&L are dollars. Book at the fill is start + running P&L.
    trade_src = [
        ("crypto", "2026-09-27T09:58:00-04:00", "QNT", "sell", 0.1031, "3.98", "3.98", "+15% scale"),
        ("crypto", "2026-09-27T12:50:00-04:00", "W", "sell", 1058, "2.20", "6.18", "+15% scale"),
        ("crypto", "2026-09-27T14:55:00-04:00", "GRT", "buy", 886.2, "0", "6.18", "artifact buy"),
        ("crypto", "2026-09-27T17:56:00-04:00", "GRT", "sell", 443.1, "1.85", "8.03", "+15% scale"),
        ("crypto", "2026-09-27T17:56:00-04:00", "ORCA", "sell", 15.13, "0.50", "8.53", "rotation"),
        ("crypto", "2026-09-27T17:56:00-04:00", "NEAR", "buy", 4.93, "0", "8.53", "rotation"),
        ("crypto", "2026-09-27T17:56:00-04:00", "IMX", "buy", 149.5, "0", "8.53", "rotation"),
        ("equities", "2026-09-25T15:37:00-04:00", "QCOM", "buy", 0.743509, "0", "0", "swing entry"),
    ]
    trades = []
    for sleeve, ts, ticker, side, qty, pnl, run_pnl, why in trade_src:
        seed = SEEDS[sleeve]
        trades.append(
            {
                "sleeve": sleeve,
                "ts": ts,
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "pnl_frac": frac(pnl, seed),
                "running_pnl_frac": frac(run_pnl, seed),
                "running_balance_frac": sheet_frac(seed + Decimal(run_pnl), seed),
                "why": why,
            }
        )
    meta = {
        "source": "sample",
        "fetched_at": None,
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in VIEWS],
        "note": (
            "Sample snapshot. Balances are sleeve book from the sheet desks (crypto $323.77, realized +$6.24; equities $500.87; combined $824.64), not cash. "
            "Replaced when the export Action can read the Supabase views. "
            "Dollar figures on the page are seed × fraction."
        ),
    }
    models_oos = {
        "rows": [],
        "note": "No out-of-sample model rows in this snapshot.",
    }
    models = {
        "as_of": as_of,
        "status": "placeholder",
        "note": "OOS metrics stay empty until T04 publishes them. Replace data/models.json; the page reads these fields.",
        "models": [
            {
                "sleeve": "crypto",
                "name": "Crypto v0 heuristic",
                "used": (
                    f"Shadow advisory for the ${format(crypto, 'f')} crypto sleeve. "
                    "Scores 24h return, volume z, and an ATR-ish range. It does not place orders."
                ),
                "training": "No fit. Buy when 24h return is above +2% and volume z is above 0.5. Sell when 24h return is below -2%.",
                "data_source": "Coinbase Exchange public hourly candles, 168 bars. 57 Robinhood USD names that also have a Coinbase product.",
                "oos": {
                    "status": "placeholder",
                    "window": None,
                    "hit_rate": None,
                    "avg_return": None,
                    "n": None,
                    "note": "Pending T04.",
                },
            },
            {
                "sleeve": "equities",
                "name": "Equities sleeve",
                "used": "No fitted equities model is published on this board.",
                "training": "Not published.",
                "data_source": "Scrubbed equities fills only. This page has no broker feed.",
                "oos": {
                    "status": "placeholder",
                    "window": None,
                    "hit_rate": None,
                    "avg_return": None,
                    "n": None,
                    "note": "Pending T04.",
                },
            },
        ],
    }
    return {
        "kpi_summary": summary,
        "kpi_trades_scrubbed": trades,
        "open_positions": {
            "as_of": as_of,
            "positions": [],
            "note": "Sample snapshot has no marked open book.",
        },
        "sleeve_curves": {
            "updated_at": as_of,
            "series": [],
            "note": "Sample snapshot has no sleeve history.",
        },
        "models_oos": models_oos,
        "models": models,
        "meta": meta,
    }


class ViewMissing(RuntimeError):
    """Raised when an optional view is not in the database."""


def looks_like_secret(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if text.startswith("eyJ") and text.count(".") >= 2:
        return True
    lowered = text.lower()
    return "service_role" in lowered or lowered.startswith("sb_secret_")


def scrub_row(row: dict) -> dict:
    clean = {}
    for key, value in row.items():
        if str(key).lower() in DENY_KEYS:
            continue
        if looks_like_secret(value):
            continue
        clean[str(key)] = jsonable(value)
    return clean


def jsonable(value):
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return scrub_row(value)
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def install_sample(target: Path) -> None:
    bundle = sample_bundle()
    write_json(target / "kpi_summary.json", bundle["kpi_summary"])
    write_json(target / "kpi_trades_scrubbed.json", bundle["kpi_trades_scrubbed"])
    write_json(target / "open_positions.json", bundle["open_positions"])
    write_json(target / "sleeve_curves.json", bundle["sleeve_curves"])
    write_json(target / "models_oos.json", bundle["models_oos"])
    write_json(target / "models.json", bundle["models"])
    write_json(target / "meta.json", bundle["meta"])


def fetch_rest(base_url: str, key: str, view: str) -> list:
    url = base_url.rstrip("/") + f"/rest/v1/{view}?select=*"
    request = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:180]
        if key and key in detail:
            detail = detail.replace(key, "[redacted]")
        if exc.code == 404 or "PGRST205" in detail:
            raise ViewMissing(view) from None
        raise RuntimeError(f"REST {view} HTTP {exc.code}: {detail}") from None
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise RuntimeError(f"REST {view} did not return a row list")
    return [scrub_row(row) for row in payload]


def fetch_rest_paged(base_url: str, key: str, view: str, order: str) -> list:
    """Read a public table in pages. Refuses a truncated curve or book."""
    rows: list = []
    page = 1000
    max_pages = 40
    for page_index in range(max_pages):
        query = urllib.parse.urlencode(
            {
                "select": "*",
                "order": order,
                "limit": str(page),
                "offset": str(page_index * page),
            }
        )
        url = base_url.rstrip("/") + f"/rest/v1/{view}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
                "User-Agent": "agentic-sleeves-kpi-export",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:180]
            if key and key in detail:
                detail = detail.replace(key, "[redacted]")
            if exc.code == 404 or "PGRST205" in detail:
                raise ViewMissing(view) from None
            raise RuntimeError(f"REST {view} HTTP {exc.code}: {detail}") from None
        payload = json.loads(body)
        if not isinstance(payload, list):
            raise RuntimeError(f"REST {view} did not return a row list")
        rows.extend(scrub_row(row) for row in payload if isinstance(row, dict))
        if len(payload) < page:
            return rows
    raise RuntimeError(f"{view} exceeded {page * max_pages} rows; refusing a partial export")


def load_trades_for_positions(base_url: str, key: str | None, db_url: str | None) -> list:
    import refresh_kpi_snapshots as refresh

    secrets = [key or "", db_url or ""]
    if key:
        try:
            return refresh.fetch_trades_rest(base_url, key)
        except refresh.RefreshError as exc:
            if not db_url:
                raise RuntimeError(refresh.redact(str(exc), secrets)) from None
            print(f"REST read of kpi_trades failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise RuntimeError("No Supabase credential")
    return refresh.fetch_trades_db(db_url, secrets)


def load_open_positions(base_url: str, key: str | None, db_url: str | None, as_of: str) -> dict:
    """Replay kpi_trades and mark opens. Does not invent a price."""
    import refresh_kpi_snapshots as refresh

    fills = load_trades_for_positions(base_url, key, db_url)
    try:
        _realized, book = refresh.apply_books(fills)
    except refresh.RefreshError as exc:
        raise RuntimeError(refresh.redact(str(exc), [key or "", db_url or ""])) from None
    if not book:
        return {"as_of": as_of, "positions": [], "card_positions": []}
    env = refresh.env_values()
    if base_url:
        env["SUPABASE_URL"] = base_url
    if key:
        env["SUPABASE_SERVICE_ROLE_KEY"] = key
    if db_url:
        env["SUPABASE_DB_URL"] = db_url
    secrets = [
        env.get("SUPABASE_SERVICE_ROLE_KEY", ""),
        env.get("SUPABASE_DB_URL", ""),
        env.get("FINNHUB_API_KEY", ""),
        env.get("COINSTATS_API_KEY", ""),
        env.get("ALPHA_VANTAGE_API_KEY", ""),
    ]
    import account_book

    try:
        marks, origin = account_book.resolve_or_reuse(sorted(book), refresh.resolve_marks, env)
    except refresh.RefreshError as exc:
        raise RuntimeError(refresh.redact(str(exc), secrets)) from None
    if origin == "refresh":
        print("open marks reused from the refresh run", file=sys.stderr)
    scrubbed = scrub_open_positions(fills, marks, as_of)
    scrubbed["card_positions"] = card_positions(fills, marks)
    return scrubbed


def _signed_open_qty(qty: Decimal, side: object) -> Decimal:
    magnitude = abs(qty)
    return magnitude if str(side or "long").strip().lower() != "short" else -magnitude


def _card_position_row(
    sleeve: str,
    ticker: str,
    qty: Decimal,
    avg: Decimal | None,
    mark: Decimal,
    realized: Decimal | None = None,
) -> dict:
    """One open net. Value is qty times the mark. It is not a seed fraction.

    Running P&L is that ticker's close pnl_trade_usd plus qty * (mark - avg),
    the same two terms the sleeve snapshot adds for realized_pnl_usd and
    unrealized_pnl_usd.
    """
    row = {
        "sleeve": sleeve,
        "ticker": ticker,
        "qty": format(qty, "f"),
        "mark": format(mark, "f"),
        "value_usd": _cents_number(qty * mark),
    }
    if avg is not None:
        unreal = qty * (mark - avg)
        row["avg_cost"] = format(avg, "f")
        row["unrealized_pnl_usd"] = _cents_number(unreal)
        if realized is not None:
            row["running_pnl_usd"] = _cents_number(realized + unreal)
    return row


def card_positions(fills: list, marks: dict) -> list:
    """Net open qty from public.kpi_trades, marked like the sleeve snapshot.

    Reads sleeve, ticker, side, qty, avg_price, pnl_trade_usd, and timestamp_et
    through apply_books. The mark is the same quote that produces
    unrealized_pnl_usd. Running P&L adds that ticker's close pnl_trade_usd
    to qty * (mark - avg). A ticker with no mark is an error. A flat net is
    omitted. USD and USDC stay cash lines, not a second open row.
    """
    import refresh_kpi_snapshots as refresh

    try:
        _realized, book = refresh.apply_books(fills)
    except refresh.RefreshError as exc:
        raise RuntimeError(str(exc)) from None
    missing = [f"{sleeve} {ticker}" for sleeve, ticker in sorted(book) if (sleeve, ticker) not in marks]
    if missing:
        raise RuntimeError("open tickers have no mark: " + ", ".join(missing))
    order = {"crypto": 0, "equities": 1}
    rows = []
    for (sleeve, ticker), pos in sorted(book.items(), key=lambda item: (order.get(item[0][0], 9), item[0][1])):
        if ticker in {"USD", "USDC"}:
            continue
        qty = pos["qty"]
        if abs(qty) <= refresh.DUST:
            continue
        mark = Decimal(str(marks[(sleeve, ticker)]))
        rows.append(
            _card_position_row(
                sleeve,
                ticker,
                qty,
                pos.get("avg"),
                mark,
                pos.get("realized") if pos.get("realized") is not None else Decimal("0"),
            )
        )
    return rows


def card_positions_from_scrubbed(payload) -> list:
    """Card rows from the open-position read that already fetched the marks.

    Uses qty, side, avg, and mark on that payload. A row with no mark is
    skipped. unrealized_pnl_frac is not turned back into dollars.
    """
    rows_in = payload.get("positions") if isinstance(payload, dict) else payload
    order = {"crypto": 0, "equities": 1}
    rows = []
    for row in rows_in or []:
        if not isinstance(row, dict):
            continue
        sleeve = str(row.get("sleeve") or "").strip().lower()
        ticker = str(row.get("ticker") or "").strip().upper()
        if sleeve not in {"crypto", "equities"} or not ticker or ticker in {"USD", "USDC"}:
            continue
        qty = _decimal_or_none(row.get("qty"))
        mark = _decimal_or_none(row.get("mark"))
        if qty is None or mark is None or abs(qty) <= Decimal("0.00000001"):
            continue
        signed = _signed_open_qty(qty, row.get("side"))
        realized = _decimal_or_none(row.get("running_pnl_usd"))
        # A scrubbed fraction row has no close dollars. Copy a running figure
        # only when the fill replay already stored it. Do not rebuild it here.
        if realized is None:
            rows.append(_card_position_row(sleeve, ticker, signed, _decimal_or_none(row.get("avg")), mark))
        else:
            built = _card_position_row(sleeve, ticker, signed, _decimal_or_none(row.get("avg")), mark)
            built["running_pnl_usd"] = _cents_number(realized)
            rows.append(built)
    rows.sort(key=lambda item: (order.get(item["sleeve"], 9), item["ticker"]))
    return rows


def apply_card_positions(book: dict, positions: list | None) -> dict:
    """Write open nets onto the account file without touching cash or rails.

    None leaves positions already on the file. A list, including an empty
    one, is the read that just happened.
    """
    out = dict(book) if isinstance(book, dict) else {}
    if not isinstance(positions, list):
        return out
    out["positions"] = [dict(row) for row in positions if isinstance(row, dict)]
    return out


def load_sleeve_curves(base_url: str, key: str | None, db_url: str | None, updated_at: str) -> dict:
    if key:
        try:
            raw = fetch_rest_paged(base_url, key, "kpi_sleeve_snapshots", "as_of.asc")
        except ViewMissing:
            if not db_url:
                raise
            raw = fetch_db(db_url, "kpi_sleeve_snapshots")
        except Exception:
            if not db_url:
                raise
            print("REST read of kpi_sleeve_snapshots failed; trying SUPABASE_DB_URL", file=sys.stderr)
            raw = fetch_db(db_url, "kpi_sleeve_snapshots")
    else:
        if not db_url:
            raise RuntimeError("No Supabase credential")
        raw = fetch_db(db_url, "kpi_sleeve_snapshots")
    return {"updated_at": updated_at, "series": scrub_snapshot_history(raw)}


def fetch_db(db_url: str, view: str) -> list:
    try:
        import psycopg
        from psycopg.errors import UndefinedTable
    except ImportError as exc:
        raise RuntimeError("psycopg is required for SUPABASE_DB_URL") from exc
    sql = f"select * from public.{view}"
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description]
                return [scrub_row(dict(zip(columns, row))) for row in cur.fetchall()]
    except UndefinedTable:
        raise ViewMissing(view) from None


def load_view(base_url: str, key: str | None, db_url: str | None, view: str) -> list:
    if key:
        try:
            return fetch_rest(base_url, key, view)
        except ViewMissing:
            if not db_url:
                raise
        except Exception:
            if not db_url:
                raise
            print(f"REST read of {view} failed; trying SUPABASE_DB_URL", file=sys.stderr)
    if not db_url:
        raise RuntimeError("No Supabase credential")
    return fetch_db(db_url, view)


def storage_call(base_url: str, key: str, path: str, body: dict | None = None) -> bytes:
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="POST" if body is not None else "GET",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:120]
        if key and key in detail:
            detail = detail.replace(key, "[redacted]")
        raise RuntimeError(f"storage HTTP {exc.code}") from None


def rows_from_oos_payload(payload):
    if isinstance(payload, list):
        return [scrub_row(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [scrub_row(row) for row in payload["rows"] if isinstance(row, dict)]
    return None


def fetch_bucket_oos(base_url: str, key: str, bucket: str) -> list | None:
    """Pull a small models/OOS JSON from storage when the view is absent."""
    try:
        listed = json.loads(
            storage_call(
                base_url,
                key,
                f"/storage/v1/object/list/{urllib.parse.quote(bucket)}",
                {"prefix": "", "limit": 100},
            ).decode("utf-8")
        )
    except Exception as exc:
        print(f"Storage list skipped: {exc}", file=sys.stderr)
        return None
    if not isinstance(listed, list):
        return None
    names = []
    for item in listed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        lowered = name.lower()
        if lowered.endswith(".json") and ("oos" in lowered or "model" in lowered):
            names.append(name)
    for name in names:
        encoded = urllib.parse.quote(name)
        try:
            raw = storage_call(base_url, key, f"/storage/v1/object/{urllib.parse.quote(bucket)}/{encoded}")
        except Exception as exc:
            print(f"Storage object skipped: {exc}", file=sys.stderr)
            continue
        if len(raw) > MAX_BUCKET_JSON:
            continue
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        rows = rows_from_oos_payload(payload)
        if rows is not None:
            return rows
    return None


def export_live(base_url: str, key: str | None, db_url: str | None) -> dict:
    rows = {}
    for view in VIEWS:
        rows[view] = load_view(base_url, key, db_url, view)
    rows["kpi_summary"] = [reshape_summary_row(row) for row in rows["kpi_summary"]]
    rows["kpi_summary"] = latest_summary_rows(rows["kpi_summary"])
    rows["kpi_trades_scrubbed"] = attach_running_ledger(
        [reshape_trade_row(row) for row in rows["kpi_trades_scrubbed"]]
    )
    fetched_at = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opened = load_open_positions(base_url, key, db_url, fetched_at)
    rows["card_positions"] = opened.pop("card_positions", [])
    rows["open_positions"] = opened
    rows["sleeve_curves"] = load_sleeve_curves(base_url, key, db_url, fetched_at)
    missing = []
    for view in OPTIONAL_VIEWS:
        try:
            rows[view] = load_view(base_url, key, db_url, view)
        except ViewMissing:
            missing.append(view)
            rows[view] = None
    # A missing or empty models_oos view must not wipe a committed seed.
    # The page keeps data/models_oos.json until the view returns rows.
    present = [name for name in (*VIEWS, *OPTIONAL_VIEWS) if name not in missing]
    present.extend(["kpi_trades", "kpi_sleeve_snapshots"])
    rows["meta"] = {
        "source": "supabase",
        "fetched_at": fetched_at,
        "export_status": "ok",
        "export_error": None,
        "export_attempted_at": fetched_at,
        "project_ref": PROJECT_REF,
        "views": [f"public.{name}" for name in present],
        "row_counts": {
            name: len(rows[name]) for name in present if isinstance(rows.get(name), list)
        },
        "note": (
            "Exported from scrubbed views. Balances are sleeve book (cash + MTM; interim start + running P&L), not cash. "
            "kpi_summary remaps running_bal_vs_start, pnl_pct_of_book, and notes "
            "onto running_balance_frac, running_pnl_frac, and note. "
            "Tape running_pnl_frac and running_balance_frac are regenerated per sleeve "
            "from warehouse trade P&L (seed + cumulative realized), not from a sheet. "
            "why is the full note. "
            "Page dollars are seed × fraction. "
            "Sleeve as_of comes from kpi_sleeve_snapshots, inserted by "
            "scripts/refresh_kpi_snapshots.py before this export. "
            "open_positions.json is net open qty from kpi_trades, marked with the same "
            "public quotes as that refresh; unrealized_pnl_frac is the open P&L divided "
            "by the sleeve seed. sleeve_curves.json is snapshot history as fractions of "
            "the book seed. Raw dollar columns and account ids are not written."
        ),
        "warehouse_status": "ok",
    }
    rows["meta"]["row_counts"]["open_positions"] = len(rows["open_positions"]["positions"])
    rows["meta"]["row_counts"]["sleeve_curves"] = len(rows["sleeve_curves"]["series"])
    if missing:
        rows["meta"]["optional_missing"] = missing
    facts = crypto_scorecard_facts(base_url, key, db_url)
    rows["crypto_fee_drag"] = facts["fee_drag"]
    rows["order_id_unique"] = facts["order_id_unique"]
    rows["signal_linkage"] = load_signal_linkage(base_url, key, db_url)
    if isinstance(rows.get("signal_linkage"), dict):
        # Counts only. Check rebuilds the scorecard from this committed block.
        rows["meta"]["signal_linkage"] = rows["signal_linkage"]
    rows["kpi_trades_scrubbed"] = attach_fee_frac(
        rows["kpi_trades_scrubbed"], facts.get("fee_rows") or []
    )
    # The check job has no warehouse secrets. Publish the fee total the scrubbed
    # tape can reproduce (fee_frac_of_book × seed), not a second dollar sum.
    tape_fees = fee_drag_from_rows(rows["kpi_trades_scrubbed"])
    if isinstance(tape_fees, dict) and tape_fees.get("status") == "known":
        rows["crypto_fee_drag"] = tape_fees
    return rows


def oos_has_rows(payload) -> bool:
    if isinstance(payload, list):
        return any(isinstance(row, dict) for row in payload)
    if isinstance(payload, dict):
        rows = payload.get("rows")
        return isinstance(rows, list) and any(isinstance(row, dict) for row in rows)
    return False


MISSING_CREDS = (
    "Export did not refresh: SUPABASE_SERVICE_ROLE_KEY and SUPABASE_DB_URL are unset. "
    "Showing the last committed snapshot."
)


def parse_as_of(value) -> dt.datetime:
    text = str(value).strip().replace("Z", "+00:00")
    if " " in text and "T" not in text:
        text = text.replace(" ", "T", 1)
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def latest_summary_rows(rows: list) -> list:
    """Keep the row with the greatest as_of for each sleeve.

    A history dump of kpi_sleeve_snapshots must not fail freshness on an
    older sibling, and must not be written into kpi_summary.json. A sleeve
    with no as_of keeps one row so assert_summary_fresh still fails loud.
    Empty input is unchanged.
    """
    if not rows:
        return list(rows)

    groups: dict[str, list] = {}
    order: list[str] = []
    for index, row in enumerate(rows):
        sleeve = row.get("sleeve") if isinstance(row, dict) else None
        if sleeve is None or str(sleeve).strip() == "":
            key = f"\0{index}"
        else:
            key = str(sleeve).strip().lower()
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(row)

    latest: list = []
    for key in order:
        cohort = groups[key]
        chosen = None
        chosen_at = None
        for row in cohort:
            as_of = row.get("as_of") if isinstance(row, dict) else None
            if not as_of:
                continue
            moment = parse_as_of(as_of)
            if chosen_at is None or moment >= chosen_at:
                chosen = row
                chosen_at = moment
        latest.append(cohort[0] if chosen is None else chosen)
    return latest


def assert_summary_fresh(rows: list, *, now: dt.datetime | None = None) -> None:
    """Refuse to write JSON when the latest sleeve snapshot is still pre-refresh.

    Pass rows from latest_summary_rows. Empty input, a missing as_of, or an
    as_of outside the window fails.
    """
    if not rows:
        raise RuntimeError("kpi_summary is empty after refresh; not writing JSON")
    current = now or dt.datetime.now(dt.timezone.utc)
    for row in rows:
        as_of = row.get("as_of") if isinstance(row, dict) else None
        if not as_of:
            raise RuntimeError("kpi_summary row is missing as_of after refresh; not writing JSON")
        moment = parse_as_of(as_of)
        if current - moment > dt.timedelta(minutes=15) or moment - current > dt.timedelta(minutes=5):
            raise RuntimeError(
                f"kpi_summary as_of {moment.strftime('%Y-%m-%dT%H:%M:%SZ')} is stale after refresh; "
                "not writing JSON"
            )


def committed_as_of(target: Path) -> str | None:
    path = target / "kpi_summary.json"
    if not path.exists():
        return None
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    best = None
    best_at = None
    for row in rows:
        if not isinstance(row, dict) or not row.get("as_of"):
            continue
        try:
            moment = parse_as_of(row["as_of"])
        except (TypeError, ValueError):
            continue
        if best_at is None or moment >= best_at:
            best_at = moment
            best = str(row["as_of"])
    return best


def failure_message(exc: BaseException) -> tuple[str, str]:
    """Public status for the board. The message must not contain secrets or KPI numbers."""
    text = str(exc).lower()
    errno = getattr(exc, "errno", None)
    if errno == 28 or "no space left" in text or "disk full" in text or "enospc" in text:
        return "error", "Export failed: disk full. KPI numbers were left unchanged."
    if errno == 30 or "read-only" in text or "readonly" in text or "read only" in text or "erofs" in text:
        return "error", "Export failed: read-only database or filesystem. KPI numbers were left unchanged."
    if "no supabase credential" in text:
        return "stale", MISSING_CREDS
    if isinstance(exc, RuntimeError) and str(exc).startswith("REST "):
        detail = str(exc)
        if "://" in detail or looks_like_secret(detail):
            detail = "REST read failed"
        else:
            parts = []
            for token in detail.split():
                if looks_like_secret(token) or token.startswith("eyJ"):
                    parts.append("[redacted]")
                else:
                    parts.append(token)
            detail = " ".join(parts)
        return "error", f"Export failed: {detail[:180]}. KPI numbers were left unchanged."
    return "error", "Export failed before it could refresh the snapshot. KPI numbers were left unchanged."


def classify_failure(exc: BaseException, frozen: str | None) -> tuple[str, str, str | None]:
    """Status, board copy, and warehouse_status. Does not invent mark-to-market."""
    status, message = failure_message(exc)
    raw = str(exc)
    low = f"{raw} {message}".lower()
    when = frozen or "the last committed snapshot"
    if "25006" in raw or "read-only" in low or "readonly" in low or "read only" in low:
        return "error", f"warehouse read-only — snapshot frozen at {when}", "read-only"
    if "53100" in raw or "disk full" in low or "no space" in low:
        return "error", f"warehouse disk full — snapshot frozen at {when}", "disk-full"
    if "stale after refresh" in low:
        return "error", raw[:300], "stale-snapshot"
    return status, message, None


def stamp_export_failure(
    target: Path,
    status: str,
    message: str,
    warehouse_status: str | None = None,
    snapshot_as_of: str | None = None,
) -> None:
    """Record a missed refresh on meta.json. Does not rewrite KPI JSON."""
    meta_path = target / "meta.json"
    meta: dict = {}
    if meta_path.exists():
        try:
            loaded = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            meta = loaded
    meta["export_status"] = status
    meta["export_error"] = message
    meta["export_attempted_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if warehouse_status:
        meta["warehouse_status"] = warehouse_status
    else:
        meta.pop("warehouse_status", None)
    if snapshot_as_of:
        meta["snapshot_as_of"] = snapshot_as_of
    write_json(meta_path, meta)


OOS_FEE_BPS = 30
# Recorded from warehouse tape: median positive fee_ratio ≈ 0.0095.
# T24d sets FEE_BPS from this. This export does not retrain.
LIVE_FEE_HANDOFF = (
    "Measured live fee ~95 bps/leg (median) / ~190 RT from tape. "
    "T24d will set FEE_BPS from that."
)
OOS_MODEL_ORDER = ("rules", "logistic", "lgbm")
FEE_FIELD_KEYS = ("fee_usd", "fee", "fee_charged")
CRYPTO_FACT_SELECTS = (
    "side,order_id,timestamp_et,ticker,notional_usd,pnl_trade_usd,pnl_frac_of_book,fee_usd",
    "side,order_id,timestamp_et,ticker,notional_usd,fee_usd",
    "side,order_id,pnl_trade_usd,pnl_frac_of_book,fee_usd",
    "side,order_id,pnl_trade_usd,fee_usd",
    "side,fee_usd",
    "side,fee",
)
CRYPTO_FACT_SQL = (
    "select side, order_id, timestamp_et, ticker, notional_usd, pnl_trade_usd, pnl_frac_of_book, fee_usd "
    "from public.kpi_trades where sleeve = 'crypto'",
    "select side, order_id, timestamp_et, ticker, notional_usd, fee_usd "
    "from public.kpi_trades where sleeve = 'crypto'",
    "select side, order_id, pnl_trade_usd, pnl_frac_of_book, fee_usd from public.kpi_trades where sleeve = 'crypto'",
    "select side, order_id, pnl_trade_usd, fee_usd from public.kpi_trades where sleeve = 'crypto'",
    "select side, order_id, pnl_trade_usd, fee from public.kpi_trades where sleeve = 'crypto'",
    "select side, fee_usd from public.kpi_trades where sleeve = 'crypto'",
    "select side, fee from public.kpi_trades where sleeve = 'crypto'",
)


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _finite_number(value):
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _sleeve_name(row: dict) -> str:
    return str(row.get("sleeve") or row.get("book") or row.get("desk") or "").strip().lower()


def _exit_pnl_frac(row: dict):
    if "pnl_frac_of_book" in row and row.get("pnl_frac_of_book") not in (None, ""):
        parsed = _finite_number(row.get("pnl_frac_of_book"))
        if parsed is not None:
            return parsed
    return _finite_number(row.get("pnl_frac"))


def _cents(frac, seed: Decimal | None = None):
    book = _crypto_seed() if seed is None else seed
    if frac is None:
        return None
    return float((Decimal(str(frac)) * book).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def closed_fill_stats(rows: list, sleeve: str = "crypto", seed: Decimal | None = None) -> dict:
    """Sell fills with a finite pnl fraction. Flat zero is excluded.

    A repeated order id is counted once when that field is present. The
    scrubbed Pages tape drops order ids, so those rows stay one-per-line.
    An omitted seed is the crypto seed from config/book_seeds.json.
    """
    if seed is None:
        seed = _crypto_seed()
    wanted = {"crypto", "equities"} if sleeve == "combined" else {sleeve}
    seen: set[str] = set()
    wins = losses = flats = deduped = 0
    order_id_available = False
    total = Decimal("0")
    for row in rows or []:
        if not isinstance(row, dict) or _sleeve_name(row) not in wanted:
            continue
        if str(row.get("side") or "").strip().lower() != "sell":
            continue
        frac = _exit_pnl_frac(row)
        if frac is None:
            continue
        order_id = str(row.get("order_id") or "").strip()
        if order_id:
            order_id_available = True
            if order_id in seen:
                deduped += 1
                continue
            seen.add(order_id)
        if frac == 0:
            flats += 1
            continue
        if frac > 0:
            wins += 1
        else:
            losses += 1
        total += Decimal(str(frac))
    decided = wins + losses
    expectancy = None if decided == 0 else total / Decimal(decided)
    return {
        "wins": wins,
        "losses": losses,
        "flats": flats,
        "decided": decided,
        "win_rate": None if decided == 0 else float(q6(Decimal(wins) / Decimal(decided))),
        "expectancy_frac": None if expectancy is None else float(q6(expectancy)),
        "expectancy_usd": None if expectancy is None else _cents(expectancy, seed),
        "seed_usd": int(seed),
        "deduped": deduped,
        "order_id_available": order_id_available,
    }


def unknown_fee(note: str) -> dict:
    return {
        "status": "unknown",
        "fee_usd": None,
        "sell_fee_usd": None,
        "fee_frac": None,
        "n": None,
        "seed_usd": int(_crypto_seed()),
        "note": note,
    }


def _fee_dollars(row: dict, seed: Decimal) -> Decimal | None:
    """Fee dollars from a raw column, or from fee_frac_of_book × sleeve seed.

    The public tape keeps the fraction only. Check rebuilds the scorecard from
    that fraction and does not need warehouse credentials.
    """
    for key in FEE_FIELD_KEYS:
        if key not in row:
            continue
        amount = _decimal_or_none(row.get(key))
        if amount is not None:
            return amount
    if "fee_frac_of_book" not in row or seed in (None, 0):
        return None
    frac = _decimal_or_none(row.get("fee_frac_of_book"))
    if frac is None:
        return None
    return frac * seed


def fee_drag_from_rows(rows: list, sleeve: str = "crypto", seed: Decimal | None = None) -> dict | None:
    if seed is None:
        seed = _crypto_seed()
    seen: set[str] = set()
    total = Decimal("0")
    sell_total = Decimal("0")
    n = 0
    numeric = False
    for row in rows or []:
        if not isinstance(row, dict) or _sleeve_name(row) not in {sleeve, ""}:
            continue
        order_id = str(row.get("order_id") or "").strip()
        if order_id:
            if order_id in seen:
                continue
            seen.add(order_id)
        amount = _fee_dollars(row, seed)
        if amount is None:
            continue
        numeric = True
        total += amount
        n += 1
        if str(row.get("side") or "").strip().lower() == "sell":
            sell_total += amount
    if not numeric:
        return None

    def usd(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    rounded = usd(total)
    rounded_sell = usd(sell_total)
    note = (
        "Sum of crypto fee dollars on the rows that were read. Identifiers are not written. "
        + LIVE_FEE_HANDOFF
    )
    median_bps = _median_positive_fee_bps(rows, sleeve)
    if median_bps is not None:
        note += f" This read median {median_bps} bps/leg (~{median_bps * 2} RT)."
    return {
        "status": "known",
        "fee_usd": float(rounded),
        "sell_fee_usd": float(rounded_sell),
        "fee_frac": float(q6(rounded / seed)) if seed else None,
        "n": n,
        "seed_usd": int(seed),
        "note": note,
    }


def _median_positive_fee_bps(rows: list, sleeve: str = "crypto") -> int | None:
    """Median fee_usd / notional_usd among positive fees, in basis points."""
    ratios: list[Decimal] = []
    for row in rows or []:
        if not isinstance(row, dict) or _sleeve_name(row) not in {sleeve, ""}:
            continue
        fee = _decimal_or_none(row.get("fee_usd"))
        if fee is None:
            fee = _decimal_or_none(row.get("fee"))
        notional = _decimal_or_none(row.get("notional_usd"))
        if fee is not None and notional is not None and fee > 0 and notional > 0:
            ratios.append(fee / notional)
            continue
        fee_frac = _decimal_or_none(row.get("fee_frac_of_book"))
        notional_frac = _decimal_or_none(row.get("notional_frac_of_book"))
        if (
            fee_frac is None
            or notional_frac is None
            or fee_frac <= 0
            or notional_frac <= 0
        ):
            continue
        ratios.append(fee_frac / notional_frac)
    if not ratios:
        return None
    ratios.sort()
    mid = len(ratios) // 2
    if len(ratios) % 2:
        median = ratios[mid]
    else:
        median = (ratios[mid - 1] + ratios[mid]) / Decimal(2)
    return int((median * Decimal(10000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fee_match_key(row: dict):
    ticker = str(row.get("ticker") or "").strip().upper()
    side = str(row.get("side") or "").strip().lower()
    stamp = row.get("timestamp_et") if row.get("timestamp_et") not in (None, "") else row.get("ts")
    if not ticker or side not in {"buy", "sell"} or stamp in (None, ""):
        return None
    text = str(stamp).strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        stamp_key = text
    else:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        stamp_key = parsed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    sleeve = str(row.get("sleeve") or "crypto").strip().lower()
    return sleeve, ticker, side, stamp_key


def _row_fee_usd(row: dict) -> Decimal | None:
    for key in FEE_FIELD_KEYS:
        if key in row:
            amount = _decimal_or_none(row.get(key))
            if amount is not None:
                return amount
    return None


def attach_fee_frac(rows: list, facts: list) -> list:
    """Publish fee_frac_of_book (fee dollars / sleeve seed). Drop raw fee dollars and ids.

    Facts may carry order ids and fee dollars. Those fields are not copied onto
    the public tape. A row with no matched fee is left without a fee fraction.
    """
    queues: dict[tuple, list] = {}
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        key = _fee_match_key(fact)
        if key is None:
            continue
        queues.setdefault(key, []).append(fact)
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            out.append(row)
            continue
        item = dict(row)
        fee = _row_fee_usd(item)
        if fee is None:
            key = _fee_match_key(item)
            bucket = queues.get(key) if key is not None else None
            if bucket:
                fee = _row_fee_usd(bucket.pop(0))
        sleeve = str(item.get("sleeve") or "").strip().lower()
        seed = LEDGER_SEEDS.get(sleeve)
        if fee is not None and seed not in (None, 0):
            item["fee_frac_of_book"] = float(q6(fee / seed))
        for key in FEE_FIELD_KEYS:
            item.pop(key, None)
        item.pop("order_id", None)
        item.pop("notional_usd", None)
        out.append(item)
    return out


def _oos_list(payload) -> list:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [row for row in payload["rows"] if isinstance(row, dict)]
    return []


def _crypto_oos_rows(payload) -> list:
    rows = []
    for row in _oos_list(payload):
        sleeve = str(row.get("sleeve") or row.get("asset_class") or "").strip().lower()
        if sleeve == "crypto":
            rows.append(row)
    rows.sort(key=lambda row: OOS_MODEL_ORDER.index(str(row.get("model") or "").lower()) if str(row.get("model") or "").lower() in OOS_MODEL_ORDER else 99)
    return rows


def live_backend_block(models: dict, oos) -> dict:
    models = models if isinstance(models, dict) else {}
    parts = [str(models.get("note") or "")]
    for model in models.get("models") or []:
        if not isinstance(model, dict):
            continue
        if str(model.get("sleeve") or "").strip().lower() != "crypto":
            continue
        parts.append(str(model.get("used") or ""))
        parts.append(str(model.get("training") or ""))
        nested = model.get("oos")
        if isinstance(nested, dict):
            parts.append(str(nested.get("note") or ""))
    text = "\n".join(parts)
    rules = "--backend rules" in text
    promoted = None
    for row in _crypto_oos_rows(oos):
        if row.get("promoted") is True:
            promoted = str(row.get("model") or "") or None
            break
    if rules and promoted:
        note = (
            f"The CLI is still --backend rules. {promoted} is promoted on after-cost mean "
            "and is not the live backend."
        )
    elif rules:
        note = "The CLI is still --backend rules."
    else:
        note = "Live backend is not stated in data/models.json. Not inferred."
    return {
        "id": "rules" if rules else None,
        "cli": "--backend rules" if rules else None,
        "promoted_model": promoted,
        "promoted_in_use": False if rules else None,
        "source": "data/models.json",
        "note": note,
    }


def _first_stamp(payload, keys):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if value:
            return value
    return None


def as_fraction(value):
    """Match derive.js asFraction. Absolute values above 1 are percent points."""
    number = _finite_number(value)
    if number is None:
        return None
    return number / 100.0 if abs(number) > 1 else number


def kill_block(summary) -> dict:
    rows = summary if isinstance(summary, list) else []
    crypto = next((row for row in rows if isinstance(row, dict) and _sleeve_name(row) == "crypto"), None)
    if not crypto:
        return {
            "as_of": None,
            "kill_headroom_stored": None,
            "kill_headroom_frac": None,
            "kill_headroom_usd": None,
            "day_kill_pct": None,
            "day_kill_usd": None,
            "day_target_pct": None,
            "day_target_usd": None,
            "seed_usd": int(_crypto_seed()),
            "note": "No crypto row in kpi_summary.",
        }
    stored = _finite_number(crypto.get("kill_headroom_frac"))
    head = as_fraction(stored)
    day_kill = as_fraction(crypto.get("day_kill_pct"))
    day_target = as_fraction(crypto.get("day_target_pct"))
    return {
        "as_of": crypto.get("as_of"),
        "kill_headroom_stored": stored,
        "kill_headroom_frac": head,
        "kill_headroom_usd": _cents(head),
        "day_kill_pct": day_kill,
        "day_kill_usd": _cents(day_kill),
        "day_target_pct": day_target,
        "day_target_usd": _cents(day_target),
        "seed_usd": int(_crypto_seed()),
        "note": "Same reading as the crypto sleeve card. A stored absolute value above 1 is percent points.",
    }


def oos_block(oos) -> dict:
    payload = oos if isinstance(oos, dict) else {"rows": _oos_list(oos)}
    models = []
    for row in _crypto_oos_rows(payload):
        models.append(
            {
                "model": row.get("model"),
                "auc": row.get("auc"),
                "brier": row.get("brier"),
                "n_long": row.get("n_long"),
                "after_cost_mean": row.get("after_cost_mean"),
                "ir_vs_btc": row.get("sleeve_ir_vs_spy"),
                "n_cohorts": row.get("n_cohorts"),
                "promoted": row.get("promoted"),
            }
        )
    return {
        "fee_bps": OOS_FEE_BPS,
        "updated_at": payload.get("updated_at") if isinstance(payload, dict) else None,
        "benchmark_note": payload.get("benchmark_note") if isinstance(payload, dict) else None,
        "note": "Out-of-sample after-cost uses 30 bp. " + LIVE_FEE_HANDOFF,
        "models": models,
    }


def unknown_linkage(note: str) -> dict:
    return {
        "status": "unknown",
        "artifacts_table": "signal_artifacts",
        "outcomes_table": "signal_trade_outcomes",
        "artifact_count": None,
        "outcome_count": None,
        "last_generated_at": None,
        "note": note,
    }


def _content_range_count(header: str | None):
    if not header or "/" not in header:
        return None
    total = header.rsplit("/", 1)[-1].strip()
    if total == "*" or not total.isdigit():
        return None
    return int(total)


def _rest_latest_count(base_url: str, key: str, table: str, stamp: str):
    """Return (count, latest stamp) or None. Does not return row payloads."""
    query = urllib.parse.urlencode(
        {"select": stamp, "order": f"{stamp}.desc", "limit": "1"}
    )
    request = urllib.request.Request(
        base_url.rstrip("/") + f"/rest/v1/{table}?{query}",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Prefer": "count=exact",
            "Range": "0-0",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
            count = _content_range_count(response.headers.get("content-range"))
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    latest = None
    if payload and isinstance(payload[0], dict):
        latest = payload[0].get(stamp)
    return count, latest


def _rest_count(base_url: str, key: str, table: str) -> int | None:
    """Exact row count. The response body is discarded."""
    request = urllib.request.Request(
        base_url.rstrip("/") + f"/rest/v1/{table}?select=*",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "Prefer": "count=exact",
            "Range": "0-0",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()
            return _content_range_count(response.headers.get("content-range"))
    except Exception:
        return None


def _sql_count_pair(db_url: str, table: str):
    for sql in (
        f"select count(*)::int as n, max(generated_at) as latest from public.{table}",
        f"select count(*)::int as n, max(created_at) as latest from public.{table}",
        f"select count(*)::int as n, null::timestamptz as latest from public.{table}",
    ):
        rows = _sql_optional(db_url, sql)
        if rows:
            return rows[0].get("n"), rows[0].get("latest")
    return None


def load_signal_linkage(base_url: str, key: str | None, db_url: str | None) -> dict:
    """Counts and the latest generated_at only. No artifact payload, no order ids."""
    if not key and not db_url:
        return unknown_linkage(
            "No warehouse credential on this export. signal_artifacts counts were not read."
        )
    artifact = None
    outcomes = None
    if key:
        for stamp in ("generated_at", "created_at"):
            artifact = _rest_latest_count(base_url, key, "signal_artifacts", stamp)
            if artifact is not None:
                break
        for stamp in ("generated_at", "created_at", "joined_at"):
            outcomes = _rest_latest_count(base_url, key, "signal_trade_outcomes", stamp)
            if outcomes is not None:
                break
        if artifact is None:
            counted = _rest_count(base_url, key, "signal_artifacts")
            if counted is not None:
                artifact = (counted, None)
        if outcomes is None:
            counted = _rest_count(base_url, key, "signal_trade_outcomes")
            if counted is not None:
                outcomes = (counted, None)
    if artifact is None and db_url:
        artifact = _sql_count_pair(db_url, "signal_artifacts")
    if outcomes is None and db_url:
        outcomes = _sql_count_pair(db_url, "signal_trade_outcomes")
    if artifact is None and outcomes is None:
        return unknown_linkage(
            "signal_artifacts and signal_trade_outcomes were not read. Counts are not estimated."
        )
    artifact_count, generated = artifact if artifact else (None, None)
    outcome_count = outcomes[0] if outcomes else None
    if generated is not None and not isinstance(generated, str):
        generated = jsonable(generated)
    return {
        "status": "known",
        "artifacts_table": "signal_artifacts",
        "outcomes_table": "signal_trade_outcomes",
        "artifact_count": artifact_count,
        "outcome_count": outcome_count,
        "last_generated_at": generated,
        "note": "Counts only. The artifact payload stays in the warehouse.",
    }


def build_model_scorecard(
    summary,
    trades,
    models,
    oos,
    fee_drag=None,
    order_id_unique=None,
    signal_linkage=None,
) -> dict:
    """Crypto scorecard from already-exported JSON. Does not invent fees."""
    fills = closed_fill_stats(trades if isinstance(trades, list) else [], "crypto")
    if fills["order_id_available"]:
        fill_note = (
            f"Duplicate order ids dropped: {fills['deduped']}. Flat zero is excluded. "
            "T24b will improve joined-fill metrics."
        )
    else:
        fill_note = (
            "Sell fills with a finite P&L. Flat zero is excluded, same as sleeve Win %. "
            "The scrubbed tape has no order id, so rows are not collapsed. "
            "T24b will improve joined-fill metrics."
        )
    scanned = fee_drag_from_rows(trades if isinstance(trades, list) else [])
    if isinstance(fee_drag, dict) and fee_drag.get("status") == "known":
        fees = dict(fee_drag)
        note = str(fees.get("note") or "")
        if "95 bps" not in note:
            fees["note"] = (note + " " + LIVE_FEE_HANDOFF).strip()
    elif isinstance(scanned, dict) and scanned.get("status") == "known":
        fees = scanned
    elif isinstance(fee_drag, dict) and fee_drag.get("status") == "unknown":
        fees = fee_drag
    else:
        fees = unknown_fee("No fee column on the scrubbed tape. Not estimated.")
    model_payload = models if isinstance(models, dict) else {}
    oos_payload = oos if isinstance(oos, (dict, list)) else {"rows": []}
    trained = _first_stamp(model_payload, ("trained_at", "promoted_at", "fit_at"))
    for model in model_payload.get("models") or []:
        if isinstance(model, dict) and str(model.get("sleeve") or "").strip().lower() == "crypto":
            trained = trained or _first_stamp(model, ("trained_at", "promoted_at", "fit_at"))
    unique = order_id_unique if isinstance(order_id_unique, dict) else None
    if unique:
        unique = {key: value for key, value in unique.items() if key != "order_id"}
    return {
        "sleeve": "crypto",
        "ticket": "T24e",
        "live_backend": live_backend_block(model_payload, oos_payload),
        "closed_fills": {
            **fills,
            "dedupe": "order-id" if fills["order_id_available"] else "scrubbed-rows",
            "note": fill_note,
        },
        "order_id_unique": unique,
        "fee_drag": fees,
        "kill": kill_block(summary),
        "oos": oos_block(oos_payload),
        "last_train": {
            "models_as_of": model_payload.get("as_of"),
            "oos_updated_at": oos_payload.get("updated_at") if isinstance(oos_payload, dict) else None,
            "trained_at": trained,
            "promoted_at": _first_stamp(model_payload, ("promoted_at",)),
            "note": (
                "No separate train or promote timestamp is in the exported model files."
                if not trained
                else "Train timestamp is the field exported on data/models.json."
            ),
        },
        "signal_linkage": signal_linkage
        if isinstance(signal_linkage, dict)
        else unknown_linkage(
            "Scrubbed Pages JSON has no signal_artifacts or signal_trade_outcomes counts. Not estimated."
        ),
    }


def _fee_rows_for_tape(rows: list) -> list:
    """Match keys and fee dollars only. Order ids are dropped."""
    public = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        public.append(
            {
                "sleeve": row.get("sleeve") or "crypto",
                "timestamp_et": row.get("timestamp_et"),
                "ticker": row.get("ticker"),
                "side": row.get("side"),
                "fee_usd": row.get("fee_usd", row.get("fee")),
                "notional_usd": row.get("notional_usd"),
            }
        )
    return public


def _rest_crypto_facts(base_url: str, key: str) -> list | None:
    for select in CRYPTO_FACT_SELECTS:
        rows: list = []
        for offset in range(0, 10000, 1000):
            query = f"sleeve=eq.crypto&select={select}&limit=1000&offset={offset}"
            chunk = _rest_optional(base_url, key, query)
            if chunk is None:
                rows = []
                break
            rows.extend(chunk)
            if len(chunk) < 1000:
                return rows
        else:
            if rows:
                return rows
    return None


def _rest_optional(base_url: str, key: str, query: str):
    url = base_url.rstrip("/") + "/rest/v1/kpi_trades?" + query
    request = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    return [row for row in payload if isinstance(row, dict)]


def _sql_optional(db_url: str, sql: str):
    try:
        import psycopg
    except ImportError:
        return None
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                columns = [desc.name for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception:
        return None


def _normalize_fact_rows(rows: list) -> list:
    normalized = []
    seed = _crypto_seed()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["sleeve"] = item.get("sleeve") or "crypto"
        if item.get("pnl_frac_of_book") in (None, "") and item.get("pnl_trade_usd") not in (None, ""):
            pnl = _decimal_or_none(item.get("pnl_trade_usd"))
            if pnl is not None:
                item["pnl_frac_of_book"] = float(q6(pnl / seed))
        normalized.append(item)
    return normalized


def crypto_scorecard_facts(base_url: str, key: str | None, db_url: str | None) -> dict:
    """Warehouse fees and order-id-unique sells.

    The result has aggregates only. Order ids are dropped before return.
    A missing credential leaves a clear unknown note. A successful read that
    includes fee amounts returns status known and does not keep the scrubbed
    UNKNOWN note.
    """
    rows = None
    if key:
        rows = _rest_crypto_facts(base_url, key)
    if rows is None and db_url:
        for sql in CRYPTO_FACT_SQL:
            rows = _sql_optional(db_url, sql)
            if rows is not None:
                break
    if rows is None:
        if not key and not db_url:
            note = (
                "No warehouse credential on this export. kpi_trades.fee_usd was not read. Not estimated."
            )
        else:
            note = (
                "Warehouse credentials were set but kpi_trades fee columns were not returned. "
                "Fee drag stays unknown for this run. Not estimated."
            )
        return {
            "fee_drag": unknown_fee(note),
            "order_id_unique": None,
            "fee_rows": [],
        }
    fees = fee_drag_from_rows(rows) or unknown_fee(
        "Crypto trades were read and no fee amount was present. Not estimated."
    )
    normalized = _normalize_fact_rows(rows)
    unique = None
    if any(str(row.get("order_id") or "").strip() for row in normalized):
        stats = closed_fill_stats(normalized, "crypto")
        if stats["order_id_available"] and (
            any(row.get("pnl_frac_of_book") not in (None, "") for row in normalized)
            or any(row.get("pnl_trade_usd") not in (None, "") for row in normalized)
        ):
            unique = {
                "wins": stats["wins"],
                "losses": stats["losses"],
                "flats": stats["flats"],
                "deduped": stats["deduped"],
                "win_rate": stats["win_rate"],
                "expectancy_frac": stats["expectancy_frac"],
                "expectancy_usd": stats["expectancy_usd"],
                "note": "Unique order id among crypto sells. The headline Win % stays the scrubbed tape.",
            }
    return {"fee_drag": fees, "order_id_unique": unique, "fee_rows": _fee_rows_for_tape(rows)}


def write_model_scorecard(target: Path, bundle: dict) -> None:
    models = _read_json(target / "models.json", {})
    oos = bundle.get("models_oos")
    if not oos_has_rows(oos):
        oos = _read_json(target / "models_oos.json", {"rows": []})
    payload = build_model_scorecard(
        bundle.get("kpi_summary") or [],
        bundle.get("kpi_trades_scrubbed") or [],
        models if isinstance(models, dict) else {},
        oos,
        fee_drag=bundle.get("crypto_fee_drag"),
        order_id_unique=bundle.get("order_id_unique"),
        signal_linkage=bundle.get("signal_linkage"),
    )
    write_json(target / "model_scorecard.json", payload)


LIVE_SIGNAL_URL = (
    "https://raw.githubusercontent.com/jrg185/agentic-crypto-signals/main/signals/latest.json"
)


def js_round_cents(value) -> float:
    """Cents, matching derive.js money() / Math.round (ties toward +infinity)."""
    shifted = (float(value) + sys.float_info.epsilon) * 100
    return math.floor(shifted + 0.5) / 100.0


def approx_cents(left, right, tol: float = 0.005) -> bool:
    """True when two USD amounts are inside half a cent.

    None and non-numbers do not match. Callers that must tell null from a
    dollar still use identity.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if left is None or right is None:
        return False
    try:
        a = float(left)
        b = float(right)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(a) or not math.isfinite(b):
        return False
    return abs(a - b) < tol


def usd_equal(left, right) -> bool:
    if left is None or right is None:
        return left is right
    return approx_cents(left, right)


def usd_differ(left, right) -> bool:
    return not usd_equal(left, right)


_LIVE_RAILS: dict[str, Decimal] | None = None


def live_rails() -> dict[str, Decimal]:
    """dayKillFrac and dayTargetFrac from derive.js LIVE_RAILS.

    The card and this export share that object. This module does not keep
    a second copy of the fractions.
    """
    global _LIVE_RAILS
    if _LIVE_RAILS is not None:
        return _LIVE_RAILS
    text = (ROOT / "derive.js").read_text(encoding="utf-8")
    match = re.search(r"export const LIVE_RAILS\s*=\s*\{([^}]+)\}", text)
    if not match:
        raise RuntimeError("derive.js LIVE_RAILS is missing")
    body = match.group(1)
    rails: dict[str, Decimal] = {}
    for key in ("dayKillFrac", "dayTargetFrac"):
        found = re.search(rf"{key}\s*:\s*(-?\d+(?:\.\d+)?)", body)
        if not found:
            raise RuntimeError(f"derive.js LIVE_RAILS has no {key}")
        rails[key] = Decimal(found.group(1))
    _LIVE_RAILS = rails
    return rails


def kill_remaining_usd(running_balance, day_pnl) -> float | None:
    """max(0, |dayKillFrac| × running balance + min(day P&L, 0)).

    dayKillFrac is the signed rail from derive.js LIVE_RAILS. The budget is
    its magnitude. A positive day does not increase headroom. The net day
    figure is used as-is. This does not apply a fee rate.
    """
    if running_balance is None:
        return None
    frac = float(live_rails()["dayKillFrac"])
    balance = float(running_balance)
    day = 0.0 if day_pnl is None else float(day_pnl)
    rounded = js_round_cents(abs(frac) * balance + min(day, 0.0))
    return 0.0 if rounded < 0 else rounded


def agentic_book_usd(book) -> float | None:
    """Cash holdings plus marked crypto open lots, in cents.

    Same rows as derive.js agenticBookUsd. A missing value leaves the total
    unknown. No position list means the book has not published lots.
    USD and USDC positions are cash and are not added again. Equities are
    not added.
    """
    if not isinstance(book, dict) or not isinstance(book.get("positions"), list):
        return None
    total = Decimal("0")
    rows = 0
    for row in book.get("holdings") or []:
        if not isinstance(row, dict):
            continue
        if not str(row.get("ticker") or "").strip():
            continue
        sleeve = str(row.get("sleeve") or "crypto").strip().lower()
        if sleeve != "crypto":
            continue
        value = _decimal_or_none(row.get("value_usd"))
        if value is None:
            return None
        total += value
        rows += 1
    for row in book.get("positions") or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker or ticker in {"USD", "USDC"}:
            continue
        sleeve = str(row.get("sleeve") or "crypto").strip().lower()
        if sleeve != "crypto":
            continue
        qty = _decimal_or_none(row.get("qty"))
        if qty is None or qty == 0:
            continue
        value = _decimal_or_none(row.get("value_usd"))
        if value is None:
            return None
        total += value
        rows += 1
    if rows == 0:
        return None
    return js_round_cents(total)


def account_book_usd(holdings) -> float | None:
    """Sum of crypto holding values, in cents. A missing value leaves the book unknown.

    Same names the page keeps. Tape tickers are not added here.
    """
    rows = []
    for row in holdings or []:
        if not isinstance(row, dict):
            continue
        if not str(row.get("ticker") or "").strip():
            continue
        sleeve = str(row.get("sleeve") or "crypto").strip().lower()
        if sleeve != "crypto":
            continue
        rows.append(row)
    if not rows:
        return None
    total = Decimal("0")
    for row in rows:
        value = _decimal_or_none(row.get("value_usd"))
        if value is None:
            return None
        total += value
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


# Whole-account card. The crypto sleeve row is not this book, and these
# dollars are not divided by the book seeds in config/book_seeds.json.
ACCOUNT_PNL_KEYS = (
    "running_balance_usd",
    "realized_pnl_usd",
    "unrealized_pnl_usd",
    "running_pnl_usd",
)


def _cents_number(value):
    number = _decimal_or_none(value)
    if number is None:
        return None
    return float(number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def latest_account_pnl(rows: list) -> dict | None:
    """Latest combined snapshot, in dollars.

    Reads running_balance_usd, realized_pnl_usd, unrealized_pnl_usd, and
    running_pnl_usd. The snapshot table has no day_pnl_usd column, so this
    read does not supply day P&L. A fraction column is not multiplied by a
    sleeve seed. A crypto-only row is not the account. as_of from that
    combined row is the warehouse sleeve clock. The writer stores it as
    sleeve_as_of. It is not a dollar column.
    """
    chosen = None
    chosen_at = None
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("sleeve") or "").strip().lower() != "combined":
            continue
        as_of = row.get("as_of")
        if not as_of:
            continue
        try:
            moment = parse_as_of(as_of)
        except (TypeError, ValueError):
            continue
        if chosen_at is None or moment >= chosen_at:
            chosen = row
            chosen_at = moment
    if chosen is None:
        return None
    out = {}
    for key in ACCOUNT_PNL_KEYS:
        if key not in chosen:
            continue
        number = _cents_number(chosen.get(key))
        if number is None:
            continue
        out[key] = number
    if not out:
        return None
    as_of = chosen.get("as_of")
    if as_of:
        out["as_of"] = jsonable(as_of)
    return out


def apply_account_pnl(book: dict, pnl: dict | None) -> dict:
    """Copy snapshot dollar columns onto the account file.

    Does not invent a figure when the column was not on the row. Does not
    touch day_pnl_usd. The snapshot table has no day column, and a missing
    key must not clear the signal day already on the file. Copies as_of
    onto sleeve_as_of when the snapshot has one. A missing as_of does not
    clear a sleeve clock already on the file.
    """
    out = dict(book) if isinstance(book, dict) else {}
    if not isinstance(pnl, dict):
        return out
    for key in ACCOUNT_PNL_KEYS:
        if key not in pnl:
            continue
        number = _cents_number(pnl.get(key))
        if number is None:
            continue
        out[key] = number
    as_of = pnl.get("as_of")
    if as_of:
        out["sleeve_as_of"] = jsonable(as_of)
    return out


def _account_snapshot_sql() -> str:
    return (
        "select sleeve, as_of, running_balance_usd, realized_pnl_usd, "
        "unrealized_pnl_usd, running_pnl_usd "
        "from public.kpi_sleeve_snapshots "
        "where lower(btrim(sleeve)) = 'combined' "
        "order by as_of desc limit 1"
    )


def _fetch_account_snapshot_rest(base_url: str, key: str) -> list:
    query = urllib.parse.urlencode(
        {
            "select": "sleeve,as_of,running_balance_usd,realized_pnl_usd,unrealized_pnl_usd,running_pnl_usd",
            "sleeve": "eq.combined",
            "order": "as_of.desc",
            "limit": "1",
        }
    )
    url = base_url.rstrip("/") + f"/rest/v1/kpi_sleeve_snapshots?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
            "User-Agent": "agentic-sleeves-kpi-export",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:180]
        if key and key in detail:
            detail = detail.replace(key, "[redacted]")
        raise RuntimeError(f"REST kpi_sleeve_snapshots HTTP {exc.code}: {detail}") from None
    payload = json.loads(body)
    if not isinstance(payload, list):
        raise RuntimeError("REST kpi_sleeve_snapshots did not return a row list")
    return [scrub_row(row) for row in payload if isinstance(row, dict)]


def _fetch_account_snapshot_db(db_url: str) -> list:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError("psycopg is required for SUPABASE_DB_URL") from exc
    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(_account_snapshot_sql())
            columns = [desc.name for desc in cur.description]
            return [scrub_row(dict(zip(columns, row))) for row in cur.fetchall()]


def load_account_pnl(base_url: str, key: str | None, db_url: str | None) -> dict | None:
    """Read the latest combined row of public.kpi_sleeve_snapshots.

    Columns: running_balance_usd, realized_pnl_usd, unrealized_pnl_usd, and
    running_pnl_usd. sleeve=combined, one row. Returns None when there is no credential.
    """
    if not key and not db_url:
        return None
    rows: list | None = None
    if key:
        try:
            rows = _fetch_account_snapshot_rest(base_url, key)
        except Exception:
            if not db_url:
                raise
            print(
                "REST read of kpi_sleeve_snapshots account P&L failed; trying SUPABASE_DB_URL",
                file=sys.stderr,
            )
            rows = None
    if rows is None:
        if not db_url:
            raise RuntimeError("No Supabase credential")
        rows = _fetch_account_snapshot_db(db_url)
    pnl = latest_account_pnl(rows)
    if pnl:
        print(
            "live book P&L from public.kpi_sleeve_snapshots "
            "sleeve=combined "
            + " ".join(f"{key}={pnl[key]}" for key in ACCOUNT_PNL_KEYS if key in pnl)
        )
    else:
        print("live book P&L: public.kpi_sleeve_snapshots has no combined dollar row")
    return pnl


def merge_live_book(committed, signal) -> dict:
    """Copy the signal file without putting its book_usd on the account.

    Holdings, including each value and cost basis, stay unless a live cash
    read replaces USD and USDC before this merge. book_usd becomes the sum
    of those values. The signal book is kept as signal_book_usd. It is not
    copied onto book_usd and it is not the card's day-kill rail.
    Kill headroom is copied when the signal has it. Day P&L is copied from
    the signal when that file has a finite day_pnl_usd. A missing signal day
    leaves the day already on the account. Realized, unrealized, running P&L,
    and running_balance_usd already on the account are kept. Open positions
    already on the account are kept. Candidates, a signal holding list, and a
    signal position list are not the account. generated_at stays the signal
    file time. sleeve_as_of is the warehouse snapshot clock already on the
    account. The signal cannot replace it.
    """
    base = dict(committed) if isinstance(committed, dict) else {}
    holdings = [dict(row) for row in base.get("holdings") or [] if isinstance(row, dict)]
    live = signal if isinstance(signal, dict) else None
    generated = base.get("generated_at")
    sleeve_as_of = base.get("sleeve_as_of")
    day = base.get("day_pnl_usd")
    kill = base.get("kill_remaining_usd")
    signal_book = _finite_number(base.get("signal_book_usd"))
    if live:
        if live.get("generated_at"):
            generated = live.get("generated_at")
        signal_value = _finite_number(live.get("book_usd"))
        if signal_value is not None:
            signal_book = signal_value
        day_value = _finite_number(live.get("day_pnl_usd"))
        if day_value is not None:
            day = day_value
        kill_value = _finite_number(live.get("kill_remaining_usd"))
        if kill_value is not None:
            kill = kill_value
    account = account_book_usd(holdings)
    book = account if account is not None else _finite_number(base.get("book_usd"))
    out = {}
    if base.get("source"):
        out["source"] = base.get("source")
    if generated:
        out["generated_at"] = generated
    if sleeve_as_of:
        out["sleeve_as_of"] = sleeve_as_of
    if book is not None:
        out["book_usd"] = book
    if signal_book is not None:
        out["signal_book_usd"] = signal_book
    if day is not None:
        out["day_pnl_usd"] = day
    for key in ("running_balance_usd", "realized_pnl_usd", "unrealized_pnl_usd", "running_pnl_usd"):
        if base.get(key) is not None:
            out[key] = base.get(key)
    for key in ("day_realized_gross_usd", "day_sell_fees_usd"):
        if base.get(key) is not None:
            out[key] = base.get(key)
    if isinstance(base.get("positions"), list):
        out["positions"] = [dict(row) for row in base["positions"] if isinstance(row, dict)]
    if kill is not None:
        out["kill_remaining_usd"] = kill
    out["holdings"] = holdings
    return out


def fetch_live_signal() -> dict | None:
    """Read the public signal file. A 404 or a private repo leaves the account file."""
    request = urllib.request.Request(
        LIVE_SIGNAL_URL,
        headers={
            "Accept": "application/json",
            "User-Agent": "the-book-live-book",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def usd_cash_from_accounts(payload, account_number: str):
    """USD cash for one Robinhood crypto account.

    v2 returns a results list. v1 returns the account object. An explicit
    cash field wins over buying_power. A non-USD buying-power currency is
    not cash. Missing data stays unknown instead of inventing a balance.
    """
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        rows = [row for row in payload["results"] if isinstance(row, dict)]
    elif isinstance(payload, dict) and (
        payload.get("buying_power") not in (None, "") or payload.get("cash") not in (None, "")
    ):
        rows = [payload]
    else:
        return None
    chosen = None
    wanted = str(account_number or "").strip()
    for row in rows:
        number = str(row.get("account_number") or "").strip()
        if wanted and number == wanted:
            chosen = row
            break
    if chosen is None and len(rows) == 1 and not str(rows[0].get("account_number") or "").strip():
        chosen = rows[0]
    if chosen is None:
        return None
    currency = str(chosen.get("buying_power_currency") or "USD").strip().upper()
    if currency != "USD":
        return None
    for key in ("cash", "cash_available", "buying_power"):
        if key not in chosen or chosen.get(key) in (None, ""):
            continue
        number = _cents_number(chosen.get(key))
        if number is not None:
            return number
    return None


def usdc_usd_from_holdings(payload):
    """USDC dollar line from holdings total_quantity.

    A results list with no USDC row is a flat stablecoin line. A payload
    that is not a holdings page stays unknown.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return None
    total = Decimal("0")
    seen = False
    for row in payload["results"]:
        if not isinstance(row, dict):
            continue
        code = str(row.get("asset_code") or "").strip().upper()
        if code != "USDC":
            continue
        qty = _decimal_or_none(row.get("total_quantity"))
        if qty is None:
            return None
        total += qty
        seen = True
    if not seen:
        return 0.0
    return float(total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _rh_collect(api_key: str, private_key: str, path: str, rh_get, path_from_next):
    """Follow a Robinhood GET until next is absent. GET only."""
    rows = []
    seen: set[str] = set()
    for _ in range(10):
        if path in seen:
            break
        seen.add(path)
        payload = rh_get(api_key, private_key, path)
        if not isinstance(payload, dict):
            raise RuntimeError("Robinhood cash read returned an unexpected payload")
        batch = payload.get("results")
        if isinstance(batch, list):
            rows.extend(item for item in batch if isinstance(item, dict))
        elif payload.get("buying_power") not in (None, "") or payload.get("cash") not in (None, ""):
            return payload
        else:
            raise RuntimeError("Robinhood cash read had no results")
        nxt = payload.get("next")
        if not nxt:
            return {"results": rows}
        path = path_from_next(str(nxt))
    raise RuntimeError("Robinhood cash pagination did not finish")


def load_rh_cash(env: dict | None = None) -> dict | None:
    """Live USD buying power and USDC quantity for the agentic account.

    None when RH_API_KEY or RH_BASE64_PRIVATE_KEY is unset. That None does
    not fail export; the caller may read data/rh_cash.json. A signed GET
    that fails, or a payload without both lines, raises so export does not
    publish fresh lots on a broken cash read. This does not place an order.
    """
    from sync_rh_kpi_trades import SyncError, path_from_next, rh_credentials, rh_get

    source = env if env is not None else os.environ
    creds = rh_credentials(source)
    if creds is None:
        return None
    api_key, private_key, account = creds
    holdings_path = (
        "/api/v2/crypto/trading/holdings/?account_number="
        + urllib.parse.quote(account)
        + "&asset_code=USDC"
    )
    try:
        accounts = _rh_collect(
            api_key, private_key, "/api/v2/crypto/trading/accounts/", rh_get, path_from_next
        )
        holdings = _rh_collect(api_key, private_key, holdings_path, rh_get, path_from_next)
    except SyncError as exc:
        raise RuntimeError(
            "Robinhood cash read failed. Holdings were not refreshed. " + str(exc)
        ) from None
    usd = usd_cash_from_accounts(accounts, account)
    usdc = usdc_usd_from_holdings(holdings)
    if usd is None or usdc is None:
        raise RuntimeError(
            "Robinhood cash read did not include USD buying power and USDC quantity. "
            "Holdings were not refreshed."
        )
    return {"USD": usd, "USDC": usdc}


_ISO8601_STAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
)


def _iso8601_stamp(value) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if _ISO8601_STAMP.fullmatch(text) is None:
        return False
    try:
        parse_as_of(text)
    except (TypeError, ValueError):
        return False
    return True


def _json_cash_number(value):
    """A JSON number, in cents. Strings and booleans are not cash."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    return _cents_number(value)


# Optional desk-drop day fields. day_realized_usd is already net of sell fees.
# The gross and sell-fee keys are audit copies. Export does not recompute the net.
_DAY_DROP_KEYS = ("day_realized_usd", "day_realized_gross_usd", "day_sell_fees_usd")


def day_drop_consistent(drop: dict) -> None:
    """Optional day_realized_usd must be a finite number.

    When day_realized_gross_usd and day_sell_fees_usd are present too, gross
    minus fees matches the net within one cent. A missing net is not an error.
    """
    if not isinstance(drop, dict) or "day_realized_usd" not in drop:
        return
    net = drop.get("day_realized_usd")
    if isinstance(net, bool) or not isinstance(net, (int, float)) or not math.isfinite(net):
        raise RuntimeError("day_realized_usd must be a finite number")
    if "day_realized_gross_usd" not in drop or "day_sell_fees_usd" not in drop:
        return
    gross = drop.get("day_realized_gross_usd")
    fees = drop.get("day_sell_fees_usd")
    for key, value in (("day_realized_gross_usd", gross), ("day_sell_fees_usd", fees)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise RuntimeError(f"{key} must be a finite number")
    gap = abs(Decimal(str(gross)) - Decimal(str(fees)) - Decimal(str(net)))
    if gap > Decimal("0.01"):
        raise RuntimeError(
            "day_realized_gross_usd minus day_sell_fees_usd "
            f"differs from day_realized_usd by {gap}"
        )


def load_rh_cash_drop(data_dir: Path) -> dict | None:
    """USD and USDC from data/rh_cash.json.

    Shape is {"USD": number, "USDC": number, "as_of": "ISO8601 optional"}.
    Optional day_realized_usd is the net day P&L: Robinhood get_realized_pnl
    span=day total_returns minus that day's sell-side fees. Optional
    day_realized_gross_usd and day_sell_fees_usd are audit numbers. A present
    numeric field is passed through. A non-number is ignored and does not
    reject the cash lines. Unknown keys are ignored. A missing file,
    non-object, non-number cash line, or a present as_of that is not
    ISO8601 returns None. This does not call Robinhood and does not invent
    a balance.
    """
    payload = _read_json(Path(data_dir) / "rh_cash.json", None)
    if not isinstance(payload, dict):
        return None
    usd = _json_cash_number(payload.get("USD"))
    usdc = _json_cash_number(payload.get("USDC"))
    if usd is None or usdc is None:
        return None
    out = {"USD": usd, "USDC": usdc}
    if "as_of" in payload and payload.get("as_of") not in (None, ""):
        as_of = payload.get("as_of")
        if not _iso8601_stamp(as_of):
            return None
        out["as_of"] = str(as_of).strip()
    for key in _DAY_DROP_KEYS:
        if key not in payload or payload.get(key) in (None, ""):
            continue
        number = _json_cash_number(payload.get(key))
        if number is None:
            continue
        out[key] = number
    return out


def apply_rh_cash(book: dict, cash: dict | None) -> dict:
    """Replace USD and USDC holdings with a Robinhood cash read.

    book_usd is recomputed from the holdings. Open lots, the signal book,
    and kill_remaining_usd stay. None leaves the cash lines already on the file.
    USDC cost basis matches the quantity so the stablecoin line stays flat.
    """
    out = dict(book) if isinstance(book, dict) else {}
    if not isinstance(cash, dict):
        return out
    holdings = [dict(row) for row in out.get("holdings") or [] if isinstance(row, dict)]
    index = {}
    for i, row in enumerate(holdings):
        ticker = str(row.get("ticker") or "").strip().upper()
        if ticker in {"USD", "USDC"} and ticker not in index:
            index[ticker] = i
    for ticker in ("USD", "USDC"):
        if ticker not in cash:
            continue
        cents = _cents_number(cash.get(ticker))
        if cents is None:
            continue
        if ticker in index:
            row = holdings[index[ticker]]
            row["ticker"] = ticker
            row["sleeve"] = str(row.get("sleeve") or "crypto").strip().lower() or "crypto"
            row["value_usd"] = cents
        else:
            row = {"ticker": ticker, "sleeve": "crypto", "value_usd": cents}
            holdings.append(row)
        if ticker == "USDC":
            row["cost_basis_usd"] = cents
    out["holdings"] = holdings
    account = account_book_usd(holdings)
    if account is not None:
        out["book_usd"] = account
    return out


def _drop_number(cash, key: str):
    if not isinstance(cash, dict) or key not in cash:
        return None
    return _json_cash_number(cash.get(key))


def apply_published_book(book: dict, cash: dict | None) -> dict:
    """Write the agentic total, the drop's day P&L, and kill headroom.

    When a cash drop and a position list are present, running_balance_usd is
    cash plus open crypto lots and running_pnl_usd is that total minus the
    combined seed in BOOK_SEEDS. Realized and unrealized come from that same
    total and the same marks as positions[].value_usd: unrealized is the open
    lots, realized is running minus unrealized. A missing cash drop leaves
    the previous balance and P&L and logs the absence. It does not fall back
    to seed + trade P&L + open mark-to-market.
    day_realized_usd, when present, is copied onto day_pnl_usd as-is. Gross
    and sell-fee audit fields are copied when present. If all three are
    present and gross minus fees differs from the net by more than one cent,
    a warning is printed and the net is left unchanged. A missing
    day_realized_usd leaves the day already on the file and logs that.
    kill_remaining_usd uses the running balance and that day.
    """
    out = dict(book) if isinstance(book, dict) else {}
    if not isinstance(cash, dict):
        print("cash drop absent; account balance, realized, and unrealized left unchanged")
        return out
    net = _drop_number(cash, "day_realized_usd")
    gross = _drop_number(cash, "day_realized_gross_usd")
    fees = _drop_number(cash, "day_sell_fees_usd")
    if net is None:
        print("day_realized_usd absent from the cash drop; day_pnl_usd left unchanged")
    else:
        out["day_pnl_usd"] = net
        print(f"live book day_pnl_usd from day_realized_usd={net}")
    if gross is not None:
        out["day_realized_gross_usd"] = gross
    if fees is not None:
        out["day_sell_fees_usd"] = fees
    if net is not None and gross is not None and fees is not None:
        gap = abs(Decimal(str(gross)) - Decimal(str(fees)) - Decimal(str(net)))
        if gap > Decimal("0.01"):
            print(
                "warning: day_realized_gross_usd minus day_sell_fees_usd "
                f"differs from day_realized_usd by {gap}; "
                "day_pnl_usd stays the net figure",
                file=sys.stderr,
            )
    marked = agentic_book_usd(out)
    if marked is None:
        return out
    out["running_balance_usd"] = marked
    out["running_pnl_usd"] = js_round_cents(Decimal(str(marked)) - BOOK_SEEDS["combined"])
    import account_book

    identity = account_book.position_identity(out.get("positions"), cash, BOOK_SEEDS["combined"])
    if identity is not None and identity.get("realized_cents") is not None:
        # Same cent round as the card. Realized is the remainder, so the
        # published sum matches running P&L to the cent. Prefer this split
        # over the warehouse row. sleeve_as_of stays the snapshot clock.
        if usd_equal(identity["balance_cents"], marked):
            out["unrealized_pnl_usd"] = float(identity["unrealized_cents"])
            out["realized_pnl_usd"] = float(identity["realized_cents"])
            out["running_pnl_usd"] = float(identity["running_cents"])
    day = _finite_number(out.get("day_pnl_usd"))
    kill = kill_remaining_usd(marked, day)
    if kill is not None:
        out["kill_remaining_usd"] = kill
    return out


def refresh_live_book(
    target: Path,
    signal: dict | None = None,
    *,
    fetch: bool = True,
    account_pnl: dict | None = None,
    positions: list | None = None,
    cash: dict | None = None,
) -> dict | None:
    """Rewrite data/live_book.json so book_usd is the holdings sum.

    Does nothing when the account file is absent. Does not invent holdings.
    When account_pnl is the latest combined snapshot, sleeve_as_of is that
    row's as_of. When a cash drop and open lots are present, running balance,
    running P&L, realized, and unrealized come from cash plus those lots on
    one mark set. Realized is running minus unrealized. A missing cash drop
    leaves the previous figures. Day P&L is day_realized_usd from the cash drop when
    that number is present; otherwise the signal copy stays and the absence
    is logged. kill_remaining_usd is the day-kill headroom on the running
    balance. generated_at stays the signal time. A missing snapshot does not
    delete P&L or day P&L already on the file. When positions is the open-net
    read, those rows replace the list. None leaves open rows already on the
    file. When cash is a Robinhood USD and USDC read (REST, or
    data/rh_cash.json when REST keys are unset), those holdings are that cash
    and book_usd is recomputed. None leaves the cash lines already on the file.
    """
    path = target / "live_book.json"
    if not path.exists():
        return None
    committed = _read_json(path, None)
    if not isinstance(committed, dict):
        return None
    if signal is None and fetch:
        signal = fetch_live_signal()
    merged = merge_live_book(committed, signal)
    if account_pnl:
        merged = apply_account_pnl(merged, account_pnl)
        merged = merge_live_book(merged, None)
    if cash is not None:
        merged = apply_rh_cash(merged, cash)
    if positions is not None:
        merged = apply_card_positions(merged, positions)
    merged = apply_published_book(merged, cash)
    write_json(path, merged)
    return merged


def write_bundle(target: Path, bundle: dict) -> None:
    for name in (*VIEWS, "meta"):
        write_json(target / f"{name}.json", bundle[name])
    if isinstance(bundle.get("open_positions"), dict):
        write_json(target / "open_positions.json", bundle["open_positions"])
    curves = bundle.get("sleeve_curves")
    if isinstance(curves, dict):
        write_json(target / "sleeve_curves.json", curves)
    elif isinstance(curves, list):
        write_json(target / "sleeve_curves.json", {"series": curves})
    oos = bundle.get("models_oos")
    if oos_has_rows(oos):
        if isinstance(oos, list):
            oos = {"rows": oos, "updated_at": bundle["meta"]["fetched_at"]}
        write_json(target / "models_oos.json", oos)
    write_model_scorecard(target, bundle)


def self_test() -> int:
    """Mixed history passes; a cohort whose latest rows are all stale fails."""
    marks = (0.1, 0.2)
    raw_lot = marks[0] + marks[1]
    rounded_lot = js_round_cents(raw_lot)
    if raw_lot == rounded_lot:
        raise RuntimeError("lot-mark float sum no longer misses exact equality")
    if not approx_cents(raw_lot, rounded_lot):
        raise RuntimeError(f"cent compare rejected a lot-mark float sum: {raw_lot} vs {rounded_lot}")

    now = dt.datetime(2026, 9, 28, 1, 30, tzinfo=dt.timezone.utc)
    fresh = (now - dt.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = "2026-09-27T23:48:00Z"
    mixed = [
        {"sleeve": "crypto", "as_of": stale, "note": "old"},
        {"sleeve": "equities", "as_of": stale, "note": "old"},
        {"sleeve": "crypto", "as_of": fresh, "note": "new"},
        {"sleeve": "combined", "as_of": stale, "note": "old"},
        {"sleeve": "equities", "as_of": fresh, "note": "new"},
        {"sleeve": "combined", "as_of": fresh, "note": "new"},
        {"sleeve": "crypto"},
    ]
    latest = latest_summary_rows(mixed)
    if len(latest) != 3 or any(row.get("note") != "new" for row in latest):
        raise RuntimeError(f"latest-as_of filter kept the wrong rows: {latest}")
    assert_summary_fresh(latest, now=now)

    older = (now - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_stale = [
        {"sleeve": "crypto", "as_of": older},
        {"sleeve": "crypto", "as_of": stale},
        {"sleeve": "equities", "as_of": stale},
        {"sleeve": "combined", "as_of": stale},
    ]
    try:
        assert_summary_fresh(latest_summary_rows(all_stale), now=now)
    except RuntimeError as exc:
        if "stale" not in str(exc):
            raise
    else:
        raise RuntimeError("all-stale kpi_summary did not fail freshness")

    try:
        assert_summary_fresh([])
    except RuntimeError as exc:
        if "empty" not in str(exc):
            raise
    else:
        raise RuntimeError("empty kpi_summary did not fail")

    try:
        assert_summary_fresh(latest_summary_rows([{"sleeve": "crypto"}]), now=now)
    except RuntimeError as exc:
        if "as_of" not in str(exc):
            raise
    else:
        raise RuntimeError("missing as_of did not fail")

    long_why = "artifact buy " + ("x" * 400)
    fills = [
        {
            "sleeve": "crypto",
            "timestamp_et": "2026-09-27T18:00:00+00:00",
            "ticker": "W",
            "side": "sell",
            "pnl_trade_usd": "2.20",
            "why": "later",
        },
        {
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T16:00:00+00:00",
            "ticker": "QNT",
            "side": "sell",
            "pnl_trade_usd": "3.98",
            "why": long_why,
        },
        {
            "sleeve": "crypto",
            "timestamp_et": "2026-09-26T12:00:00+00:00",
            "ticker": "AVAX",
            "side": "buy",
            "pnl_trade_usd": "0",
            "why": "RH Agentic backfill order 6ab70000-0000-4000-8000-000000000001",
            "notes": "backfill from RH",
        },
        {
            "sleeve": "equities",
            "timestamp_et": "2026-09-25T19:37:00+00:00",
            "ticker": "QCOM",
            "side": "buy",
            "pnl_frac_of_book": 0,
            "why": "SWING unlock Joe/Wags; soft tgt flexible",
        },
    ]
    ledger = attach_running_ledger(fills)
    crypto = [row for row in ledger if row["sleeve"] == "crypto"]
    by_ticker = {row["ticker"]: row for row in crypto}
    if by_ticker["AVAX"]["running_pnl_frac"] != 0:
        raise RuntimeError(f"AVAX running pnl {by_ticker['AVAX']['running_pnl_frac']}")
    crypto_seed = BOOK_SEEDS["crypto"]
    equities_seed = BOOK_SEEDS["equities"]
    if by_ticker["QNT"]["running_pnl_frac"] != float(q6(Decimal("3.98") / crypto_seed)):
        raise RuntimeError(f"QNT running pnl {by_ticker['QNT']['running_pnl_frac']}")
    expected_last = q6(Decimal("6.18") / crypto_seed)
    if by_ticker["W"]["running_pnl_frac"] != float(expected_last):
        raise RuntimeError(f"W running pnl {by_ticker['W']['running_pnl_frac']}")
    if by_ticker["W"]["running_balance_frac"] != float(q6((crypto_seed + Decimal("6.18")) / crypto_seed)):
        raise RuntimeError(f"W running balance {by_ticker['W']['running_balance_frac']}")
    if by_ticker["W"]["running_pnl_frac"] is None or by_ticker["W"]["running_balance_frac"] is None:
        raise RuntimeError("last crypto fill ledger is null")
    if by_ticker["QNT"]["why"] != long_why:
        raise RuntimeError("why was truncated")
    if by_ticker["AVAX"]["why"] != "backfill from RH":
        raise RuntimeError(f"machine why was not replaced: {by_ticker['AVAX']['why']}")
    cleared = attach_running_ledger(
        [
            {
                "sleeve": "crypto",
                "timestamp_et": "2026-09-28T12:00:00+00:00",
                "ticker": "OP",
                "side": "buy",
                "pnl_trade_usd": "0",
                "why": None,
                "order_id": "6ab90000-0000-4000-8000-000000000004",
            }
        ]
    )
    if cleared[0].get("why") not in (None, ""):
        raise RuntimeError(f"cleared why was rewritten {cleared[0].get('why')!r}")
    if "sync order" in json.dumps(cleared):
        raise RuntimeError("export invented a sync-order why")
    scrubbed = scrub_row(cleared[0])
    if "order_id" in scrubbed or "order_id" not in DENY_KEYS:
        raise RuntimeError("order_id would be written to Pages JSON")
    equities = [row for row in ledger if row["sleeve"] == "equities"]
    if len(equities) != 1 or equities[0]["running_pnl_frac"] != 0 or equities[0]["running_balance_frac"] != 1:
        raise RuntimeError(f"equities ledger {equities}")
    # Scrubbed rows have no dollar P&L. Summing pnl_frac_of_book still fills the last row.
    bare = attach_running_ledger(
        [
            {"sleeve": "crypto", "timestamp_et": "2026-09-26T16:36:50+00:00", "ticker": "AVAX", "side": "buy", "pnl_frac_of_book": 0, "why": "a"},
            {"sleeve": "crypto", "timestamp_et": "2026-09-26T18:22:05+00:00", "ticker": "AVAX", "side": "sell", "pnl_frac_of_book": -0.001833, "why": "b"},
        ]
    )
    if bare[-1]["running_pnl_frac"] is None or bare[-1]["running_balance_frac"] is None:
        raise RuntimeError("frac-only ledger left the last row null")
    if bare[-1]["running_pnl_frac"] != float(q6(Decimal("-0.001833"))):
        raise RuntimeError(f"frac-only running pnl {bare[-1]['running_pnl_frac']}")

    opens = scrub_open_positions(
        [
            {
                "sleeve": "crypto",
                "ticker": "aaa",
                "side": "buy",
                "qty": "10",
                "avg_price": "2",
                "pnl_trade_usd": "99",
                "timestamp_et": "2026-09-01T00:00:00Z",
                "account_id": "TEST-ACCOUNT",
                "order_id": "6ab90000-0000-4000-8000-000000000099",
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
                "pnl_trade_usd": "10",
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
        ],
        {("crypto", "AAA"): Decimal("4"), ("equities", "QCOM"): Decimal("110")},
        "2026-09-28T01:00:00Z",
    )
    by_ticker = {row["ticker"]: row for row in opens["positions"]}
    # 15 shares left. Stored close pnl is the average-cost 10, so leftover
    # cost stays the pre-close blend of 3. Mark 4. Unrealized dollars ÷ the crypto seed.
    # A stored FIFO close of 15 would leave cost 50 and unrealized 10.
    if by_ticker["AAA"]["side"] != "long" or by_ticker["AAA"]["qty"] != "15":
        raise RuntimeError(f"AAA open qty {by_ticker.get('AAA')}")
    if by_ticker["AAA"]["avg"] != "3":
        raise RuntimeError(f"AAA leftover avg mixed FIFO cost onto average pnl {by_ticker['AAA']['avg']}")
    if by_ticker["AAA"]["unrealized_pnl_frac"] != float(q6(Decimal("15") / crypto_seed)):
        raise RuntimeError(f"AAA unrealized frac {by_ticker['AAA']['unrealized_pnl_frac']}")
    if by_ticker["QCOM"]["unrealized_pnl_frac"] != float(q6(Decimal("20") / equities_seed)):
        raise RuntimeError(f"QCOM unrealized frac {by_ticker['QCOM']['unrealized_pnl_frac']}")
    public_blob = json.dumps(opens)
    if "TEST-ACCOUNT" in public_blob or "unrealized_pnl_usd" in public_blob or "order_id" in public_blob:
        raise RuntimeError("open positions JSON leaked an account field")
    if "20.000000" in public_blob or "15.000000" in public_blob:
        raise RuntimeError("open positions JSON wrote raw unrealized dollars")
    try:
        scrub_open_positions(
            [
                {
                    "sleeve": "equities",
                    "ticker": "QCOM",
                    "side": "buy",
                    "qty": "1",
                    "avg_price": "10",
                    "pnl_trade_usd": "0",
                    "timestamp_et": "2026-09-01T00:00:00Z",
                }
            ],
            {},
            "2026-09-28T01:00:00Z",
        )
    except RuntimeError as exc:
        if "no mark" not in str(exc):
            raise
    else:
        raise RuntimeError("missing open mark did not fail")
    flat = scrub_open_positions(
        [
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
        ],
        {},
        "2026-09-28T01:00:00Z",
    )
    if flat["positions"]:
        raise RuntimeError("a flat book should not invent an open position")

    history = scrub_snapshot_history(
        [
            {
                "sleeve": "crypto",
                "as_of": "2026-09-28T00:00:00Z",
                "running_balance_usd": "323.77",
                "running_pnl_usd": "23.77",
                "realized_pnl_usd": "6.24",
                "unrealized_pnl_usd": "17.53",
                "start_balance_usd": format(crypto_seed, "f"),
                "account_id": "TEST-ACCOUNT",
                "notes": "realized $6.24; book $323.77",
            },
            {
                "sleeve": "equities",
                "as_of": "2026-09-27T00:00:00Z",
                "running_pnl_frac": "0.001740",
            },
            {
                "sleeve": "combined",
                "as_of": "2026-09-28T00:00:00Z",
                "running_balance_usd": "824.64",
                "email": "joe@example.com",
            },
        ]
    )
    history_blob = json.dumps(history)
    if "323.77" in history_blob or "TEST-ACCOUNT" in history_blob or "joe@example.com" in history_blob:
        raise RuntimeError(f"curve JSON leaked warehouse dollars or an account: {history_blob}")
    if "$" in history_blob or "running_balance_usd" in history_blob or "notes" in history_blob:
        raise RuntimeError("curve JSON kept a dollar column or a note")
    crypto_point = next(row for row in history if row["sleeve"] == "crypto")
    if crypto_point["running_balance_frac"] != float(q6(Decimal("323.77") / crypto_seed)):
        raise RuntimeError(f"crypto curve frac {crypto_point['running_balance_frac']}")
    equities_point = next(row for row in history if row["sleeve"] == "equities")
    if equities_point["running_balance_frac"] != float(q6(Decimal("1") + Decimal("0.001740"))):
        raise RuntimeError("equities curve did not derive book from running P&L")
    if [row["sleeve"] for row in history] != ["equities", "combined", "crypto"]:
        raise RuntimeError(f"curve order {history}")

    card = build_model_scorecard(
        summary=[
            {
                "sleeve": "crypto",
                "as_of": "2026-09-28T00:00:00Z",
                "kill_headroom_frac": 1.25,
                "day_kill_pct": -0.1,
                "day_target_pct": 0.025,
            }
        ],
        trades=[
            {"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": 0.01, "order_id": "a"},
            {"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": 0.01, "order_id": "a"},
            {"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": -0.02},
            {"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": 0},
            {"sleeve": "crypto", "side": "buy", "pnl_frac_of_book": 0.5, "fee_usd": "1.00"},
        ],
        models={
            "as_of": "2026-09-28T00:16:00Z",
            "note": "The CLI stays --backend rules.",
            "models": [{"sleeve": "crypto", "used": "python -m model.run stays --backend rules."}],
        },
        oos={
            "updated_at": "2026-09-28T00:16:00Z",
            "rows": [
                {"sleeve": "crypto", "model": "lgbm", "auc": 0.53, "after_cost_mean": 0.003, "sleeve_ir_vs_spy": 0.1, "n_long": 6, "promoted": True},
                {"sleeve": "crypto", "model": "rules", "auc": 0.5, "after_cost_mean": -0.001, "sleeve_ir_vs_spy": -1, "n_long": 10, "promoted": False},
                {"asset_class": "equity", "model": "lgbm", "promoted": True, "after_cost_mean": 0.008},
                {"sleeve": "crypto", "model": "logistic", "auc": 0.52, "after_cost_mean": 0.002, "sleeve_ir_vs_spy": 0.2, "n_long": 4, "promoted": False},
            ],
        },
    )
    if card["live_backend"]["id"] != "rules" or card["live_backend"]["promoted_in_use"] is not False:
        raise RuntimeError(f"backend {card['live_backend']}")
    if card["live_backend"]["promoted_model"] != "lgbm":
        raise RuntimeError(f"promoted {card['live_backend']}")
    if card["closed_fills"]["wins"] != 1 or card["closed_fills"]["losses"] != 1 or card["closed_fills"]["deduped"] != 1:
        raise RuntimeError(f"fills {card['closed_fills']}")
    if card["fee_drag"]["status"] != "known" or not usd_equal(card["fee_drag"]["fee_usd"], 1.0):
        raise RuntimeError(f"fees {card['fee_drag']}")
    if "95 bps" not in card["fee_drag"]["note"] or "190 RT" not in card["fee_drag"]["note"] or "T24d" not in card["fee_drag"]["note"]:
        raise RuntimeError(f"fee handoff {card['fee_drag']['note']}")
    if card["oos"]["fee_bps"] != 30 or [row["model"] for row in card["oos"]["models"]] != ["rules", "logistic", "lgbm"]:
        raise RuntimeError(f"oos {card['oos']}")
    if "30 bp" not in card["oos"]["note"] or "95 bps" not in card["oos"]["note"] or "190 RT" not in card["oos"]["note"]:
        raise RuntimeError(f"oos handoff {card['oos']['note']}")
    kept = build_model_scorecard(
        summary=[],
        trades=[{"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": 0.01}],
        models={},
        oos={"rows": []},
        fee_drag={
            "status": "known",
            "fee_usd": 4.5,
            "sell_fee_usd": 2.0,
            "fee_frac": 0.015,
            "n": 10,
            "seed_usd": int(crypto_seed),
            "note": "from warehouse",
        },
    )
    if kept["fee_drag"]["status"] != "known" or not usd_equal(kept["fee_drag"]["fee_usd"], 4.5):
        raise RuntimeError(f"warehouse fees were replaced {kept['fee_drag']}")
    if "95 bps" not in kept["fee_drag"]["note"] or '"order_id"' in json.dumps(kept):
        raise RuntimeError("known fee drag dropped the handoff or wrote an id")
    ratio_rows = [
        {"sleeve": "crypto", "side": "buy", "fee_usd": "0.95", "notional_usd": "100", "order_id": "a"},
        {"sleeve": "crypto", "side": "sell", "fee_usd": "0.95", "notional_usd": "100", "order_id": "b"},
        {"sleeve": "crypto", "side": "buy", "fee_usd": "0.95", "notional_usd": "100", "order_id": "c"},
        {"sleeve": "crypto", "side": "sell", "fee_usd": "0", "notional_usd": "100", "order_id": "d"},
    ]
    measured = fee_drag_from_rows(ratio_rows)
    if measured["status"] != "known" or measured["n"] != 4 or not usd_equal(measured["fee_usd"], 2.85):
        raise RuntimeError(f"fee sum {measured}")
    if "This read median 95 bps/leg" not in measured["note"] or "~190 RT" not in measured["note"]:
        raise RuntimeError(f"median note {measured['note']}")
    taped = attach_fee_frac(
        [
            {
                "sleeve": "crypto",
                "ticker": "BTC",
                "side": "buy",
                "timestamp_et": "2026-09-28T00:00:00+00:00",
                "pnl_frac_of_book": 0,
                "order_id": "should-drop",
                "fee_usd": "2.85",
            }
        ],
        [],
    )
    if taped[0].get("fee_frac_of_book") != float(q6(Decimal("2.85") / crypto_seed)):
        raise RuntimeError(f"fee frac {taped}")
    if "fee_usd" in taped[0] or "order_id" in taped[0]:
        raise RuntimeError(f"raw fee leaked {taped}")
    matched = attach_fee_frac(
        [
            {
                "sleeve": "crypto",
                "ticker": "btc",
                "side": "buy",
                "timestamp_et": "2026-09-28T00:00:00Z",
                "pnl_frac_of_book": 0,
            }
        ],
        [
            {
                "sleeve": "crypto",
                "ticker": "BTC",
                "side": "buy",
                "timestamp_et": "2026-09-28T00:00:00+00:00",
                "fee_usd": "2.85",
                "order_id": "do-not-copy",
            }
        ],
    )
    if matched[0].get("fee_frac_of_book") != taped[0]["fee_frac_of_book"] or "order_id" in matched[0] or "fee_usd" in matched[0]:
        raise RuntimeError(f"matched fee {matched}")
    missed = crypto_scorecard_facts("https://example.invalid", None, None)
    if missed["fee_drag"]["status"] != "unknown" or "credential" not in missed["fee_drag"]["note"].lower():
        raise RuntimeError(f"missing credential note {missed['fee_drag']}")
    if missed["fee_drag"]["fee_usd"] is not None:
        raise RuntimeError("missing credential invented a fee")
    for relative in (".github/workflows/export-kpi.yml", "scripts/export-kpi.yml"):
        workflow = (ROOT / relative).read_text(encoding="utf-8")
        commit = workflow.split("Commit refreshed JSON", 1)[1].split("Publish export failure", 1)[0]
        if "data/model_scorecard.json" not in commit:
            raise RuntimeError(f"{relative} does not git add data/model_scorecard.json")
        if "data/live_book.json" not in commit:
            raise RuntimeError(f"{relative} does not git add data/live_book.json")
    copied = {
        "generated_at": "2026-10-04T20:41:10Z",
        "book_usd": 775,
        "day_pnl_usd": 2.25,
        "kill_remaining_usd": 77.5,
        "holdings": [{"ticker": "USDC", "sleeve": "crypto", "value_usd": 775}],
        "candidates": [{"symbol": "BTC", "side": "sell"}],
    }
    account = {
        "source": "https://github.com/jrg185/agentic-crypto-signals/blob/main/signals/latest.json",
        "generated_at": "2026-10-04T16:34:23Z",
        "book_usd": 775,
        "signal_book_usd": 775,
        "day_pnl_usd": 1.5,
        "kill_remaining_usd": 77.5,
        "running_balance_usd": 812.4,
        "realized_pnl_usd": 8.5,
        "unrealized_pnl_usd": 1.25,
        "running_pnl_usd": 9.75,
        "holdings": [
            {"ticker": "USD", "sleeve": "crypto", "value_usd": 760.64},
            {"ticker": "USDC", "sleeve": "crypto", "value_usd": 14.07, "cost_basis_usd": 14.07},
        ],
    }
    merged_book = merge_live_book(account, copied)
    expected_book = account_book_usd(account["holdings"])
    if expected_book is None or not usd_equal(merged_book["book_usd"], expected_book):
        raise RuntimeError(f"live book was not the holdings sum: {merged_book}")
    if not usd_differ(merged_book["book_usd"], copied["book_usd"]) or not usd_differ(merged_book["book_usd"], 775):
        raise RuntimeError("signal book_usd was copied onto the account")
    if merged_book.get("signal_book_usd") != 775:
        raise RuntimeError(f"signal rail book was dropped: {merged_book}")
    if merged_book.get("realized_pnl_usd") != 8.5 or merged_book.get("running_pnl_usd") != 9.75:
        raise RuntimeError(f"account P&L was dropped: {merged_book}")
    if merged_book.get("unrealized_pnl_usd") != 1.25:
        raise RuntimeError(f"account unrealized was dropped: {merged_book}")
    if "candidates" in merged_book or [row["ticker"] for row in merged_book["holdings"]] != ["USD", "USDC"]:
        raise RuntimeError(f"signal holdings replaced the account: {merged_book}")
    if merged_book["holdings"][1].get("cost_basis_usd") != 14.07:
        raise RuntimeError("USDC cost basis changed")
    if merged_book["day_pnl_usd"] != copied["day_pnl_usd"] or merged_book["kill_remaining_usd"] != 77.5:
        raise RuntimeError("day pnl was not copied from the signal, or kill headroom changed")
    if merged_book["day_pnl_usd"] == 1.5:
        raise RuntimeError("account day replaced the signal day")
    if merged_book.get("running_balance_usd") != 812.4:
        raise RuntimeError(f"merge dropped running balance: {merged_book}")
    if not usd_differ(merged_book["running_balance_usd"], expected_book) or not usd_differ(
        merged_book["running_balance_usd"], 775
    ):
        raise RuntimeError("running balance was replaced by the holdings sum or the signal book")
    snapshot_rows = [
        {
            "sleeve": "crypto",
            "as_of": "2026-10-04T21:40:15Z",
            "realized_pnl_usd": "4.25",
            "unrealized_pnl_usd": "1.10",
            "running_pnl_usd": "5.35",
            "running_balance_usd": "305.35",
        },
        {
            "sleeve": "combined",
            "as_of": "2026-10-04T20:00:00Z",
            "realized_pnl_usd": "6.00",
            "unrealized_pnl_usd": "1.00",
            "running_pnl_usd": "7.00",
            "running_balance_usd": "807.00",
        },
        {
            "sleeve": "combined",
            "as_of": "2026-10-04T21:40:15Z",
            "realized_pnl_usd": "3.40",
            "unrealized_pnl_usd": "0.60",
            "running_pnl_usd": "4.00",
            "running_balance_usd": "804.00",
        },
    ]
    account_pnl = latest_account_pnl(snapshot_rows)
    if account_pnl != {
        "running_balance_usd": 804.0,
        "realized_pnl_usd": 3.4,
        "unrealized_pnl_usd": 0.6,
        "running_pnl_usd": 4.0,
        "as_of": "2026-10-04T21:40:15Z",
    }:
        raise RuntimeError(f"combined snapshot was not the account P&L: {account_pnl}")
    if "day_pnl_usd" in account_pnl:
        raise RuntimeError("snapshot read supplied a day figure")
    if account_pnl["realized_pnl_usd"] == 4.25 or account_pnl["running_balance_usd"] == 305.35:
        raise RuntimeError("crypto-only snapshot replaced the account")
    if account_pnl["running_balance_usd"] == 807.0:
        raise RuntimeError("an older combined row replaced the latest")
    seeded = float((Decimal("0.049797") * crypto_seed).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    if usd_equal(account_pnl["realized_pnl_usd"], seeded) or usd_equal(account_pnl["running_pnl_usd"], seeded):
        raise RuntimeError("account P&L was recomputed as a fraction of the sleeve seed")
    if latest_account_pnl(
        [
            {
                "sleeve": "combined",
                "as_of": "2026-10-04T21:40:15Z",
                "realized_pnl_frac": "0.049797",
                "running_pnl_frac": "0.019026",
            }
        ]
    ):
        raise RuntimeError("a seed fraction was turned back into account dollars")
    exporter = Path(__file__).read_text(encoding="utf-8")
    rest = exporter.split("def _fetch_account_snapshot_rest", 1)[1].split("def _fetch_account_snapshot_db", 1)[0]
    sql = exporter.split("def _account_snapshot_sql", 1)[1].split("def _fetch_account_snapshot_rest", 1)[0]
    if "eq.combined" not in rest or '"limit": "1"' not in rest:
        raise RuntimeError("REST account read must filter sleeve=combined and limit 1")
    if '"limit": "6"' in rest or "day_pnl_usd" in rest:
        raise RuntimeError("REST account read must not scan every sleeve or select day_pnl_usd")
    if "combined" not in sql or "limit 1" not in sql or "day_pnl_usd" in sql:
        raise RuntimeError("SQL account read must filter combined, limit 1, and skip day_pnl_usd")
    published = apply_account_pnl(merged_book, account_pnl)
    published = merge_live_book(published, copied)
    if published.get("realized_pnl_usd") != account_pnl["realized_pnl_usd"]:
        raise RuntimeError(f"writer dropped realized: {published}")
    if published.get("running_pnl_usd") != account_pnl["running_pnl_usd"]:
        raise RuntimeError(f"writer dropped running: {published}")
    if published.get("running_balance_usd") != account_pnl["running_balance_usd"]:
        raise RuntimeError(f"writer dropped running balance: {published}")
    if not usd_differ(published["running_balance_usd"], published["book_usd"]) or not usd_differ(
        published["running_balance_usd"], 775
    ):
        raise RuntimeError("writer put the holdings sum or the signal book on running balance")
    if published.get("day_pnl_usd") != copied["day_pnl_usd"]:
        raise RuntimeError(f"writer dropped the signal day: {published}")
    if not usd_equal(published["book_usd"], expected_book) or published.get("signal_book_usd") != 775:
        raise RuntimeError(f"writer moved the book off the holdings sum: {published}")
    if published.get("sleeve_as_of") != "2026-10-04T21:40:15Z":
        raise RuntimeError(f"writer dropped the warehouse sleeve clock: {published}")
    if published.get("generated_at") != copied["generated_at"]:
        raise RuntimeError(f"writer dropped the signal generated_at: {published}")
    if published.get("sleeve_as_of") == published.get("generated_at"):
        raise RuntimeError("signal generated_at became the sleeve MTM clock")
    spoofed = dict(copied)
    spoofed["sleeve_as_of"] = "1999-01-01T00:00:00Z"
    spoofed["generated_at"] = "1999-01-01T00:00:00Z"
    refused = merge_live_book(published, spoofed)
    if refused.get("sleeve_as_of") != "2026-10-04T21:40:15Z":
        raise RuntimeError(f"signal replaced the sleeve clock: {refused}")
    if refused.get("generated_at") != "1999-01-01T00:00:00Z":
        raise RuntimeError(f"signal generated_at was not kept as signal context: {refused}")
    no_clock = apply_account_pnl(
        published,
        {
            "realized_pnl_usd": 3.4,
            "unrealized_pnl_usd": 0.6,
            "running_pnl_usd": 4.0,
            "running_balance_usd": 804.0,
        },
    )
    if no_clock.get("sleeve_as_of") != published.get("sleeve_as_of"):
        raise RuntimeError(f"a snapshot without as_of cleared the sleeve clock: {no_clock}")
    no_day = latest_account_pnl(
        [
            {
                "sleeve": "combined",
                "as_of": "2026-10-04T21:40:15Z",
                "realized_pnl_usd": "8.5",
                "unrealized_pnl_usd": "1.25",
                "running_pnl_usd": "9.75",
                "running_balance_usd": "812.40",
            }
        ]
    )
    kept = apply_account_pnl(published, no_day)
    if kept.get("day_pnl_usd") != copied["day_pnl_usd"]:
        raise RuntimeError(f"a snapshot without day_pnl_usd cleared the signal day: {kept}")
    stale_book = merge_live_book(account, None)
    if not usd_equal(stale_book["book_usd"], expected_book) or stale_book.get("signal_book_usd") != 775:
        raise RuntimeError(f"a missing signal put 775 back: {stale_book}")
    if stale_book.get("realized_pnl_usd") != 8.5 or stale_book.get("running_pnl_usd") != 9.75:
        raise RuntimeError(f"a missing signal deleted account P&L: {stale_book}")
    if stale_book.get("day_pnl_usd") != 1.5 or stale_book.get("running_balance_usd") != 812.4:
        raise RuntimeError(f"a missing signal cleared day P&L or running balance: {stale_book}")
    if stale_book.get("sleeve_as_of"):
        raise RuntimeError(f"a missing snapshot invented a sleeve clock: {stale_book}")
    if stale_book.get("generated_at") != account["generated_at"]:
        raise RuntimeError(f"a missing signal cleared signal generated_at: {stale_book}")
    open_fills = [
        {
            "sleeve": "crypto",
            "ticker": "zz",
            "side": "buy",
            "qty": "4",
            "avg_price": "10",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-10-01T00:00:00Z",
        },
        {
            "sleeve": "crypto",
            "ticker": "zz",
            "side": "sell",
            "qty": "1",
            "avg_price": "12",
            "pnl_trade_usd": "2",
            "timestamp_et": "2026-10-02T00:00:00Z",
        },
        {
            "sleeve": "equities",
            "ticker": "qq",
            "side": "buy",
            "qty": "2",
            "avg_price": "8",
            "pnl_trade_usd": "0",
            "timestamp_et": "2026-10-01T00:00:00Z",
        },
        {
            "sleeve": "equities",
            "ticker": "qq",
            "side": "sell",
            "qty": "2",
            "avg_price": "9",
            "pnl_trade_usd": "2",
            "timestamp_et": "2026-10-02T00:00:00Z",
        },
    ]
    open_marks = {("crypto", "ZZ"): Decimal("12.5")}
    opened = card_positions(open_fills, open_marks)
    if opened != [
        {
            "sleeve": "crypto",
            "ticker": "ZZ",
            "qty": "3",
            "mark": "12.5",
            "value_usd": 37.5,
            "avg_cost": "10",
            "unrealized_pnl_usd": 7.5,
            "running_pnl_usd": 9.5,
        }
    ]:
        raise RuntimeError(f"open net was not qty times the mark: {opened}")
    if any(row["ticker"] == "QQ" for row in opened):
        raise RuntimeError("a flat equity net was published")
    if usd_equal(opened[0]["value_usd"], opened[0]["unrealized_pnl_usd"]):
        raise RuntimeError("open value collapsed to unrealized")
    if usd_equal(opened[0]["running_pnl_usd"], opened[0]["unrealized_pnl_usd"]):
        raise RuntimeError("running P&L dropped the ticker's close")
    position_src = Path(__file__).read_text(encoding="utf-8").split("def card_positions(", 1)[1].split(
        "def card_positions_from_scrubbed", 1
    )[0]
    for column in ("sleeve", "ticker", "side", "qty", "avg_price", "pnl_trade_usd", "timestamp_et"):
        if column not in position_src:
            raise RuntimeError(f"open read does not name kpi_trades.{column}")
    seed_token = format(crypto_seed, "f")
    if (
        "unrealized_pnl_frac" in position_src
        or f"* {seed_token}" in position_src
        or f'* Decimal("{seed_token}")' in position_src
    ):
        raise RuntimeError("open value is rebuilt from a seed fraction")
    scrubbed = scrub_open_positions(open_fills, open_marks, "2026-10-04T00:00:00Z")
    from_scrub = card_positions_from_scrubbed(scrubbed)
    if from_scrub[0]["qty"] != opened[0]["qty"] or not usd_equal(from_scrub[0]["value_usd"], opened[0]["value_usd"]):
        raise RuntimeError(f"scrubbed mark did not match the book: {from_scrub}")
    if "running_pnl_usd" in from_scrub[0]:
        raise RuntimeError("a scrubbed fraction row invented running P&L")
    carried = card_positions_from_scrubbed(
        [
            {
                "sleeve": "crypto",
                "ticker": "ZZ",
                "qty": "3",
                "side": "long",
                "avg": "10",
                "mark": "12.5",
                "running_pnl_usd": "9.5",
            }
        ]
    )
    if carried[0].get("running_pnl_usd") != 9.5:
        raise RuntimeError(f"stored running P&L was dropped: {carried}")
    reopened = card_positions(
        [
            {
                "sleeve": "crypto",
                "ticker": "yy",
                "side": "buy",
                "qty": "1",
                "avg_price": "8",
                "pnl_trade_usd": "0",
                "timestamp_et": "2026-10-01T00:00:00Z",
            },
            {
                "sleeve": "crypto",
                "ticker": "yy",
                "side": "sell",
                "qty": "1",
                "avg_price": "12",
                "pnl_trade_usd": "4",
                "timestamp_et": "2026-10-02T00:00:00Z",
            },
            {
                "sleeve": "crypto",
                "ticker": "yy",
                "side": "buy",
                "qty": "2",
                "avg_price": "8",
                "pnl_trade_usd": "0",
                "timestamp_et": "2026-10-03T00:00:00Z",
            },
        ],
        {("crypto", "YY"): Decimal("9")},
    )
    if reopened != [
        {
            "sleeve": "crypto",
            "ticker": "YY",
            "qty": "2",
            "mark": "9",
            "value_usd": 18.0,
            "avg_cost": "8",
            "unrealized_pnl_usd": 2.0,
            "running_pnl_usd": 6.0,
        }
    ]:
        raise RuntimeError(f"reopen dropped earlier close dollars: {reopened}")
    if card_positions_from_scrubbed(
        [{"sleeve": "crypto", "ticker": "ZZ", "qty": "3", "side": "long", "unrealized_pnl_frac": "0.025"}]
    ):
        raise RuntimeError("a fraction without a mark was turned into a position")
    held = dict(account)
    held["positions"] = [{"sleeve": "crypto", "ticker": "ZZ", "qty": "3", "value_usd": 37.5}]
    kept_open = merge_live_book(held, {**copied, "positions": [{"ticker": "NOPE", "qty": "9", "value_usd": 1}]})
    if [row.get("ticker") for row in kept_open.get("positions") or []] != ["ZZ"]:
        raise RuntimeError(f"signal positions replaced the book: {kept_open}")
    if not usd_equal(kept_open["book_usd"], expected_book) or kept_open.get("kill_remaining_usd") != 77.5:
        raise RuntimeError(f"open positions moved the book or the kill: {kept_open}")
    if kept_open.get("day_pnl_usd") != copied["day_pnl_usd"]:
        raise RuntimeError("open positions replaced the signal day")
    published_open = apply_card_positions(kept_open, opened)
    if published_open["positions"] != opened:
        raise RuntimeError(f"writer dropped the open net: {published_open}")
    if not usd_equal(published_open["book_usd"], expected_book) or not usd_equal(
        published_open.get("running_balance_usd"), 812.4
    ):
        raise RuntimeError("writer put the open mark on the running balance or the cash book")
    if [row["ticker"] for row in published_open["holdings"]] != ["USD", "USDC"]:
        raise RuntimeError("writer replaced the cash lines")
    cashed = apply_rh_cash(published_open, {"USD": 120.5, "USDC": 3.25})
    usd_row = next(row for row in cashed["holdings"] if row["ticker"] == "USD")
    usdc_row = next(row for row in cashed["holdings"] if row["ticker"] == "USDC")
    if usd_row["value_usd"] != 120.5 or usdc_row["value_usd"] != 3.25:
        raise RuntimeError(f"cash lines were not the Robinhood read: {cashed['holdings']}")
    if usdc_row.get("cost_basis_usd") != 3.25:
        raise RuntimeError("USDC cost basis was left on the previous quantity")
    if not usd_equal(cashed["book_usd"], account_book_usd(cashed["holdings"])):
        raise RuntimeError("book_usd was not the refreshed holdings sum")
    if not usd_differ(cashed["book_usd"], published_open["book_usd"]):
        raise RuntimeError("stale cash stayed on the book")
    if cashed.get("positions") != published_open.get("positions"):
        raise RuntimeError("cash refresh rewrote open lots")
    if cashed.get("kill_remaining_usd") != published_open.get("kill_remaining_usd"):
        raise RuntimeError("cash refresh moved kill headroom")
    if cashed.get("signal_book_usd") != published_open.get("signal_book_usd"):
        raise RuntimeError("cash refresh copied onto the signal book")
    main_src = Path(__file__).read_text(encoding="utf-8").split("\ndef main(argv", 1)[1]
    if "Export KPI expected live Robinhood cash" in main_src:
        raise RuntimeError("KPI_REFRESH_EXPECTED still fail-closes when Robinhood keys are unset")
    rest_at = main_src.find("rest_cash = load_rh_cash()")
    skip_at = main_src.find("REST cash skipped")
    drop_at = main_src.find("load_rh_cash_drop(DATA)")
    if rest_at < 0 or not (rest_at < skip_at < drop_at):
        raise RuntimeError("main does not soft-skip REST cash onto the drop file")
    if "cash=cash" not in main_src[drop_at:]:
        raise RuntimeError("main does not pass resolved cash into refresh_live_book")
    seeded = load_rh_cash_drop(DATA)
    if not isinstance(seeded, dict) or "USD" not in seeded or "USDC" not in seeded:
        raise RuntimeError("data/rh_cash.json is not a cash drop")
    if any(key not in {"USD", "USDC", "as_of", *_DAY_DROP_KEYS} for key in seeded):
        raise RuntimeError(f"cash drop kept an unknown key: {seeded}")
    raw_drop = json.loads((DATA / "rh_cash.json").read_text(encoding="utf-8"))
    day_drop_consistent(raw_drop)
    desk_day = {
        "USD": raw_drop["USD"],
        "USDC": raw_drop["USDC"],
        "day_realized_usd": -1.09,
        "day_realized_gross_usd": 1.25,
        "day_sell_fees_usd": 2.34,
    }
    day_drop_consistent(desk_day)
    try:
        day_drop_consistent(
            {
                "day_realized_usd": desk_day["day_realized_usd"] + 0.02,
                "day_realized_gross_usd": desk_day["day_realized_gross_usd"],
                "day_sell_fees_usd": desk_day["day_sell_fees_usd"],
            }
        )
    except RuntimeError:
        pass
    else:
        raise RuntimeError("a day audit gap beyond one cent was accepted")
    try:
        day_drop_consistent({"day_realized_usd": float("nan")})
    except RuntimeError:
        pass
    else:
        raise RuntimeError("a non-finite day_realized_usd was accepted")
    bare_env = {"KPI_REFRESH_EXPECTED": "1"}
    if load_rh_cash(bare_env) is not None:
        raise RuntimeError("unset RH keys did not skip REST")
    if _json_cash_number(float("nan")) is not None or _json_cash_number(True) is not None:
        raise RuntimeError("non-finite or boolean cash was accepted")
    if _json_cash_number("11.5") is not None:
        raise RuntimeError("a string cash line was accepted")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        if load_rh_cash_drop(folder) is not None:
            raise RuntimeError("a missing cash drop invented a balance")
        (folder / "rh_cash.json").write_text("{", encoding="utf-8")
        if load_rh_cash_drop(folder) is not None:
            raise RuntimeError("invalid cash drop JSON became a balance")
        (folder / "rh_cash.json").write_text("[]", encoding="utf-8")
        if load_rh_cash_drop(folder) is not None:
            raise RuntimeError("a cash drop list became a balance")
        for bad in (
            {"USD": "11.5", "USDC": 2.25},
            {"USD": True, "USDC": 2.25},
            {"USDC": 2.25},
            {"USD": 11.5, "USDC": 2.25, "as_of": "yesterday"},
            {"USD": 11.5, "USDC": 2.25, "as_of": "2026-13-40T00:00:00Z"},
        ):
            (folder / "rh_cash.json").write_text(json.dumps(bad), encoding="utf-8")
            if load_rh_cash_drop(folder) is not None:
                raise RuntimeError(f"invalid cash drop was accepted: {bad}")
        (folder / "rh_cash.json").write_text(
            json.dumps(
                {
                    "USD": 11.5,
                    "USDC": 2.25,
                    "as_of": "2026-01-02T03:04:05Z",
                    "account_number": "ignored",
                }
            ),
            encoding="utf-8",
        )
        parsed_drop = load_rh_cash_drop(folder)
        if parsed_drop != {"USD": 11.5, "USDC": 2.25, "as_of": "2026-01-02T03:04:05Z"}:
            raise RuntimeError(f"cash drop was not the file: {parsed_drop}")
        (folder / "rh_cash.json").write_text(
            json.dumps({"USD": 11, "USDC": 2, "note": "ignored"}),
            encoding="utf-8",
        )
        plain_drop = load_rh_cash_drop(folder)
        if plain_drop != {"USD": 11.0, "USDC": 2.0}:
            raise RuntimeError(f"optional as_of was required: {plain_drop}")
        resolved = load_rh_cash(bare_env)
        if resolved is None:
            resolved = load_rh_cash_drop(folder)
        if resolved != plain_drop:
            raise RuntimeError(
                "KPI_REFRESH_EXPECTED with unset RH keys did not use the drop: "
                + repr(resolved)
            )
        prior = {
            "book_usd": 100,
            "signal_book_usd": 775,
            "kill_remaining_usd": 77.5,
            "holdings": [
                {"ticker": "USD", "sleeve": "crypto", "value_usd": 70},
                {"ticker": "USDC", "sleeve": "crypto", "value_usd": 30, "cost_basis_usd": 30},
            ],
            "positions": [{"sleeve": "crypto", "ticker": "ZZ", "qty": "1", "value_usd": 5}],
        }
        write_json(folder / "live_book.json", prior)
        refreshed = refresh_live_book(folder, fetch=False, cash=resolved)
        usd_line = next(row for row in refreshed["holdings"] if row["ticker"] == "USD")
        usdc_line = next(row for row in refreshed["holdings"] if row["ticker"] == "USDC")
        if usd_line["value_usd"] != 11.0 or usdc_line["value_usd"] != 2.0:
            raise RuntimeError(f"drop was not applied: {refreshed['holdings']}")
        if usdc_line.get("cost_basis_usd") != 2.0:
            raise RuntimeError("drop left USDC cost basis on the previous quantity")
        if not usd_equal(refreshed["book_usd"], account_book_usd(refreshed["holdings"])):
            raise RuntimeError("drop apply did not recompute book_usd")
        if refreshed.get("positions") != prior["positions"]:
            raise RuntimeError("drop apply rewrote open lots")
        if refreshed.get("signal_book_usd") != 775:
            raise RuntimeError("drop apply moved the signal book")
        marked_cash = agentic_book_usd(refreshed)
        if marked_cash is None or not usd_equal(refreshed.get("running_balance_usd"), marked_cash):
            raise RuntimeError(f"running balance was not cash plus lots: {refreshed}")
        if not usd_differ(refreshed["book_usd"], refreshed["running_balance_usd"]):
            raise RuntimeError("book_usd included open lots")
        if not usd_equal(
            refreshed.get("running_pnl_usd"),
            js_round_cents(Decimal(str(marked_cash)) - BOOK_SEEDS["combined"]),
        ):
            raise RuntimeError(f"running P&L was not the agentic total minus the combined seed: {refreshed}")
        if not usd_equal(refreshed.get("kill_remaining_usd"), kill_remaining_usd(marked_cash, refreshed.get("day_pnl_usd"))):
            raise RuntimeError(f"kill headroom was not the day rail: {refreshed}")
        if refreshed.get("day_pnl_usd") is not None:
            raise RuntimeError("a drop without day_realized invented a day")
        empty = folder / "empty"
        empty.mkdir()
        write_json(empty / "live_book.json", prior)
        skipped = load_rh_cash(bare_env)
        if skipped is not None or load_rh_cash_drop(empty) is not None:
            raise RuntimeError("missing RH keys and no drop did not soft-skip")
        left = refresh_live_book(empty, fetch=False, cash=skipped)
        if [row.get("value_usd") for row in left["holdings"]] != [70, 30]:
            raise RuntimeError(f"soft-skip rewrote holdings: {left['holdings']}")
        if left["book_usd"] != 100:
            raise RuntimeError(f"soft-skip recomputed the book: {left['book_usd']}")
        (folder / "rh_cash.json").write_text(
            json.dumps(
                {
                    "USD": 4,
                    "USDC": 1,
                    "day_realized_usd": "nope",
                    "day_sell_fees_usd": True,
                    "fee_bps": 30,
                }
            ),
            encoding="utf-8",
        )
        rejected_day = load_rh_cash_drop(folder)
        if rejected_day != {"USD": 4.0, "USDC": 1.0}:
            raise RuntimeError(f"a non-number day figure changed the cash drop: {rejected_day}")
        (folder / "rh_cash.json").write_text(
            json.dumps(
                {
                    "USD": 4,
                    "USDC": 1,
                    "day_realized_usd": -1.25,
                    "day_realized_gross_usd": -1,
                    "day_sell_fees_usd": 0.25,
                    "fee_bps": 30,
                }
            ),
            encoding="utf-8",
        )
        passed_day = load_rh_cash_drop(folder)
        if passed_day != {
            "USD": 4.0,
            "USDC": 1.0,
            "day_realized_usd": -1.25,
            "day_realized_gross_usd": -1.0,
            "day_sell_fees_usd": 0.25,
        }:
            raise RuntimeError(f"day drop fields were not passed through: {passed_day}")
        synthetic = {
            "book_usd": 80,
            "signal_book_usd": 400,
            "day_pnl_usd": 1.5,
            "kill_remaining_usd": 8,
            "running_balance_usd": 804,
            "running_pnl_usd": 4,
            "realized_pnl_usd": 3.4,
            "unrealized_pnl_usd": 0.6,
            "holdings": [
                {"ticker": "USD", "sleeve": "crypto", "value_usd": 70},
                {"ticker": "USDC", "sleeve": "crypto", "value_usd": 10, "cost_basis_usd": 10},
            ],
            "positions": [
                {"sleeve": "crypto", "ticker": "ZZ", "qty": "2", "value_usd": 50, "unrealized_pnl_usd": 4},
                {"sleeve": "equities", "ticker": "QQ", "qty": "3", "value_usd": 90},
                {"sleeve": "crypto", "ticker": "USDC", "qty": "1", "value_usd": 9},
            ],
        }
        write_json(folder / "live_book.json", synthetic)
        day_cash = {
            "USD": 40,
            "USDC": 10,
            "day_realized_usd": -2.5,
            "day_realized_gross_usd": 1.0,
            "day_sell_fees_usd": 0.25,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            published_day = refresh_live_book(
                folder,
                fetch=False,
                cash=day_cash,
                account_pnl={
                    "running_balance_usd": 804.0,
                    "running_pnl_usd": 4.0,
                    "realized_pnl_usd": 3.4,
                    "unrealized_pnl_usd": 0.6,
                    "as_of": "2026-02-02T00:00:00Z",
                },
            )
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        if "differs from day_realized_usd" not in stderr.getvalue():
            raise RuntimeError("gross minus fees did not warn when it missed the net")
        if published_day["day_pnl_usd"] != -2.5:
            raise RuntimeError(f"day P&L was not the drop net: {published_day}")
        if published_day.get("day_realized_gross_usd") != 1.0 or published_day.get("day_sell_fees_usd") != 0.25:
            raise RuntimeError(f"audit fields were not copied: {published_day}")
        if not usd_equal(published_day["book_usd"], 50):
            raise RuntimeError(f"book_usd was not the cash sum: {published_day}")
        if not usd_equal(published_day["running_balance_usd"], 100):
            raise RuntimeError(f"running balance was not cash plus the crypto lot: {published_day}")
        if not usd_differ(published_day["running_balance_usd"], 804) or not usd_differ(published_day["running_pnl_usd"], 4):
            raise RuntimeError("warehouse running figures replaced the agentic total")
        if not usd_equal(published_day["running_pnl_usd"], js_round_cents(Decimal("100") - BOOK_SEEDS["combined"])):
            raise RuntimeError(f"running P&L was not the combined seed gap: {published_day}")
        if not usd_differ(published_day["running_pnl_usd"], js_round_cents(Decimal("100") - BOOK_SEEDS["crypto"])):
            raise RuntimeError("running P&L used the crypto seed")
        if not usd_equal(published_day["kill_remaining_usd"], kill_remaining_usd(100, -2.5)):
            raise RuntimeError(f"kill headroom ignored the day loss: {published_day}")
        if published_day.get("sleeve_as_of") != "2026-02-02T00:00:00Z":
            raise RuntimeError(f"snapshot sleeve clock was dropped: {published_day}")
        if not usd_equal(published_day.get("unrealized_pnl_usd"), 4):
            raise RuntimeError(f"unrealized was not the open lot: {published_day}")
        identity_running = Decimal(str(published_day["running_pnl_usd"]))
        identity_unreal = Decimal(str(published_day["unrealized_pnl_usd"]))
        if not usd_equal(published_day.get("realized_pnl_usd"), identity_running - identity_unreal):
            raise RuntimeError(f"realized was not running minus unrealized: {published_day}")
        if not usd_equal(identity_running, identity_unreal + Decimal(str(published_day["realized_pnl_usd"]))):
            raise RuntimeError("published realized plus unrealized left running")
        if published_day.get("signal_book_usd") != 400:
            raise RuntimeError("signal book moved")
        matched_cash = {
            "USD": 40,
            "USDC": 10,
            "day_realized_usd": -1.5,
            "day_realized_gross_usd": -1.0,
            "day_sell_fees_usd": 0.5,
        }
        stderr = io.StringIO()
        old_err = sys.stderr
        sys.stderr = stderr
        try:
            matched = refresh_live_book(folder, fetch=False, cash=matched_cash)
        finally:
            sys.stderr = old_err
        if "differs from day_realized_usd" in stderr.getvalue():
            raise RuntimeError("a matching gross minus fees warned")
        if matched["day_pnl_usd"] != -1.5 or not usd_equal(matched["kill_remaining_usd"], kill_remaining_usd(100, -1.5)):
            raise RuntimeError(f"matched net was recomputed: {matched}")
        cent_cash = {
            "USD": 40,
            "USDC": 10,
            "day_realized_usd": 1.0,
            "day_realized_gross_usd": 1.01,
            "day_sell_fees_usd": 0,
        }
        stderr = io.StringIO()
        old_err = sys.stderr
        sys.stderr = stderr
        try:
            within = refresh_live_book(folder, fetch=False, cash=cent_cash)
        finally:
            sys.stderr = old_err
        if "differs from day_realized_usd" in stderr.getvalue():
            raise RuntimeError("a one-cent audit gap failed the export")
        if within["day_pnl_usd"] != 1.0:
            raise RuntimeError(f"a one-cent gap changed day P&L: {within}")
        profit_cash = {"USD": 40, "USDC": 10, "day_realized_usd": 6}
        profit = refresh_live_book(folder, fetch=False, cash=profit_cash)
        if profit["day_pnl_usd"] != 6 or not usd_equal(profit["kill_remaining_usd"], kill_remaining_usd(100, 0)):
            raise RuntimeError(f"a positive day increased kill headroom: {profit}")
        if not usd_equal(profit["kill_remaining_usd"], kill_remaining_usd(100, 6)):
            raise RuntimeError("positive day headroom did not match a flat day")
        stopped_cash = {"USD": 40, "USDC": 10, "day_realized_usd": -40}
        stopped = refresh_live_book(folder, fetch=False, cash=stopped_cash)
        if stopped["day_pnl_usd"] != -40 or not usd_equal(stopped["kill_remaining_usd"], 0):
            raise RuntimeError(f"a day loss past the rail left headroom: {stopped}")
        kept_signal = {
            "generated_at": "2026-01-01T00:00:00Z",
            "book_usd": 50,
            "day_pnl_usd": 1.5,
            "kill_remaining_usd": 8,
            "holdings": [
                {"ticker": "USD", "sleeve": "crypto", "value_usd": 40},
                {"ticker": "USDC", "sleeve": "crypto", "value_usd": 10, "cost_basis_usd": 10},
            ],
            "positions": [{"sleeve": "crypto", "ticker": "ZZ", "qty": "2", "value_usd": 50}],
        }
        write_json(folder / "live_book.json", kept_signal)
        stdout = io.StringIO()
        old_out = sys.stdout
        sys.stdout = stdout
        try:
            from_signal = refresh_live_book(
                folder,
                {
                    "generated_at": "2026-01-02T00:00:00Z",
                    "book_usd": 400,
                    "day_pnl_usd": 3.25,
                    "kill_remaining_usd": 40,
                },
                fetch=False,
                cash={"USD": 40, "USDC": 10},
            )
        finally:
            sys.stdout = old_out
        if "day_realized_usd absent" not in stdout.getvalue():
            raise RuntimeError("a missing day_realized_usd was not logged")
        if from_signal["day_pnl_usd"] != 3.25:
            raise RuntimeError(f"a missing day_realized_usd did not keep the signal day: {from_signal}")
        if not usd_equal(from_signal["running_balance_usd"], 100):
            raise RuntimeError(f"signal day path dropped the agentic total: {from_signal}")
        if not usd_equal(from_signal["kill_remaining_usd"], kill_remaining_usd(100, 3.25)):
            raise RuntimeError(f"signal day path left the signal kill: {from_signal}")
        unmarked = dict(synthetic)
        unmarked["positions"] = [{"sleeve": "crypto", "ticker": "ZZ", "qty": "2"}]
        unmarked["running_balance_usd"] = 804
        unmarked["running_pnl_usd"] = 4
        write_json(folder / "live_book.json", unmarked)
        kept_warehouse = refresh_live_book(
            folder,
            fetch=False,
            cash={"USD": 40, "USDC": 10},
            account_pnl={
                "running_balance_usd": 811,
                "running_pnl_usd": 11,
                "realized_pnl_usd": 3.4,
                "unrealized_pnl_usd": 0.6,
                "as_of": "2026-02-03T00:00:00Z",
            },
        )
        if kept_warehouse.get("running_balance_usd") != 811 or kept_warehouse.get("running_pnl_usd") != 11:
            raise RuntimeError(f"a lot without a mark invented an agentic total: {kept_warehouse}")
        kill_src = Path(__file__).read_text(encoding="utf-8").split("def kill_remaining_usd", 1)[1].split(
            "\ndef ", 1
        )[0]
        if "0.10" in kill_src or "0.1" in kill_src:
            raise RuntimeError("kill headroom hardcoded a fraction")
    parsed_usd = usd_cash_from_accounts(
        {
            "results": [
                {"account_number": "other", "buying_power": "1", "buying_power_currency": "USD"},
                {"account_number": "agent", "buying_power": "42.5", "buying_power_currency": "USD"},
            ]
        },
        "agent",
    )
    if parsed_usd != 42.5:
        raise RuntimeError(f"account cash was not the agentic buying power: {parsed_usd}")
    preferred = usd_cash_from_accounts(
        {
            "results": [
                {
                    "account_number": "agent",
                    "cash": "10",
                    "buying_power": "4",
                    "buying_power_currency": "USD",
                }
            ]
        },
        "agent",
    )
    if preferred != 10:
        raise RuntimeError(f"explicit cash lost to buying power: {preferred}")
    if (
        usd_cash_from_accounts(
            {"results": [{"account_number": "agent", "buying_power": "4", "buying_power_currency": "EUR"}]},
            "agent",
        )
        is not None
    ):
        raise RuntimeError("non-USD buying power was treated as cash")
    if usd_cash_from_accounts({"results": []}, "agent") is not None:
        raise RuntimeError("a missing account invented cash")
    parsed_usdc = usdc_usd_from_holdings(
        {
            "results": [
                {"asset_code": "USDC", "total_quantity": "3.2"},
                {"asset_code": "BTC", "total_quantity": "1"},
            ]
        }
    )
    if parsed_usdc != 3.2:
        raise RuntimeError(f"USDC quantity was not read: {parsed_usdc}")
    if usdc_usd_from_holdings({"results": []}) != 0:
        raise RuntimeError("a flat USDC read invented a balance")
    if usdc_usd_from_holdings({"error": "no"}) is not None:
        raise RuntimeError("a bad holdings payload became a balance")
    if card["kill"]["kill_headroom_stored"] != 1.25 or card["kill"]["kill_headroom_frac"] != 0.0125:
        raise RuntimeError(f"kill {card['kill']}")
    if not usd_equal(card["kill"]["kill_headroom_usd"], 3.75) or not usd_equal(card["kill"]["day_kill_usd"], -30.0):
        raise RuntimeError(f"kill dollars {card['kill']}")
    if '"order_id"' in json.dumps(card):
        raise RuntimeError("scorecard wrote an order id")
    plain = build_model_scorecard(
        summary=[],
        trades=[{"sleeve": "crypto", "side": "sell", "pnl_frac_of_book": 0.02}],
        models={"note": "no cli flag"},
        oos={"rows": []},
    )
    if plain["fee_drag"]["status"] != "unknown" or plain["live_backend"]["id"] is not None:
        raise RuntimeError(f"plain scorecard {plain['fee_drag']} {plain['live_backend']}")
    if plain["closed_fills"]["wins"] != 1 or not usd_equal(plain["closed_fills"]["expectancy_usd"], 6.0):
        raise RuntimeError(f"plain fills {plain['closed_fills']}")

    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install-sample",
        action="store_true",
        help="Write the committed sample into data/ and fixtures/ and exit",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check latest-as_of filtering against the freshness window, then exit",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if args.install_sample:
        install_sample(DATA)
        install_sample(FIXTURES)
        print("Installed sample JSON into data/ and fixtures/")
        return 0

    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip() or None
    db_url = (os.environ.get("SUPABASE_DB_URL") or "").strip() or None
    base_url = (os.environ.get("SUPABASE_URL") or DEFAULT_URL).strip()

    refresh_expected = (os.environ.get("KPI_REFRESH_EXPECTED") or "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if not key and not db_url:
        if refresh_expected:
            print(
                "KPI_REFRESH_EXPECTED is set but Supabase credentials are missing; "
                "refusing to leave KPI JSON unchanged.",
                file=sys.stderr,
            )
            try:
                stamp_export_failure(DATA, "error", MISSING_CREDS)
            except OSError as exc:
                print(f"Could not record export status in data/meta.json: {exc}", file=sys.stderr)
            return 1
        print(MISSING_CREDS)
        needed = [DATA / f"{name}.json" for name in (*VIEWS, *OPTIONAL_VIEWS, "models", "meta")]
        if any(not path.exists() for path in needed):
            print("Sample JSON missing; installing fixtures.", file=sys.stderr)
            install_sample(DATA)
        try:
            refresh_live_book(DATA)
        except OSError as exc:
            print(f"Could not refresh data/live_book.json: {exc}", file=sys.stderr)
            return 1
        try:
            stamp_export_failure(DATA, "stale", MISSING_CREDS)
        except OSError as exc:
            print(f"Could not record export status in data/meta.json: {exc}", file=sys.stderr)
            return 1
        return 0

    try:
        bundle = export_live(base_url, key, db_url)
        if refresh_expected:
            assert_summary_fresh(bundle["kpi_summary"])
        write_bundle(DATA, bundle)
        account_pnl = load_account_pnl(base_url, key, db_url)
        # REST when the keys are set. Unset keys skip REST even if
        # KPI_REFRESH_EXPECTED is set, then the desk drop, then the cash
        # lines already on the file. Live balances are not hardcoded here.
        rest_cash = load_rh_cash()
        if rest_cash is None:
            print(
                "Robinhood REST cash skipped. "
                "RH_API_KEY or RH_BASE64_PRIVATE_KEY is unset."
            )
            cash = load_rh_cash_drop(DATA)
        else:
            cash = rest_cash
        if cash:
            origin = "Robinhood" if rest_cash is not None else "data/rh_cash.json"
            print(
                "live book cash from "
                + origin
                + " "
                + " ".join(f"{ticker}={cash[ticker]}" for ticker in ("USD", "USDC") if ticker in cash)
            )
        refresh_live_book(
            DATA,
            account_pnl=account_pnl,
            positions=bundle.get("card_positions"),
            cash=cash,
        )
    except Exception as exc:
        frozen = committed_as_of(DATA)
        status, message, warehouse_status = classify_failure(exc, frozen)
        print(message, file=sys.stderr)
        try:
            stamp_export_failure(
                DATA,
                status,
                message,
                warehouse_status=warehouse_status,
                snapshot_as_of=frozen,
            )
        except OSError as stamp_exc:
            print(f"Could not record export status in data/meta.json: {stamp_exc}", file=sys.stderr)
        return 1
    if not (DATA / "models.json").exists():
        write_json(DATA / "models.json", sample_bundle()["models"])
    print(
        "Exported "
        + ", ".join(f"{name}={bundle['meta']['row_counts'][name]}" for name in VIEWS)
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
