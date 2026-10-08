#!/usr/bin/env python3
"""Upsert config/book_seeds.json into public.book_seeds.

The repo file is the source. This script does not invent seeds. Running it
again writes the same rows. Rows in the table that are not in the file are
removed, so the table matches the file.

Apply order: the book_seeds migration, then this sync, then any read of
public.kpi_trades_scrubbed. The view divides by the joined seed.

  python3 scripts/sync_book_seeds.py --self-test
  python3 scripts/sync_book_seeds.py --dry-run
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python3 scripts/sync_book_seeds.py

SUPABASE_DB_URL wins when it is set. Otherwise the service role uses REST.
--dry-run prints the rows and does not connect.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import book_seeds

UPSERT_SQL = """
insert into public.book_seeds (sleeve, seed_usd, effective_from)
values (%s, %s, %s)
on conflict (sleeve, effective_from) do update
set seed_usd = excluded.seed_usd
"""

DELETE_SQL = """
delete from public.book_seeds as live
where not exists (
  select 1
  from unnest(%s::text[], %s::date[]) as kept(sleeve, effective_from)
  where kept.sleeve = live.sleeve
    and kept.effective_from = live.effective_from
)
"""


class SeedSyncError(RuntimeError):
    """The seed table was not changed."""


def sync_rows(path: Path | None = None) -> list[dict]:
    rows = []
    for row in book_seeds.load_seed_rows(path):
        rows.append(
            {
                "sleeve": row["sleeve"],
                "seed_usd": format(row["seed_usd"], "f"),
                "effective_from": row["effective_from"].isoformat(),
            }
        )
    return rows


def apply_rows(rows: list[dict]) -> None:
    if not rows:
        raise SeedSyncError("refusing to sync an empty seed list")
    db_url = (os.environ.get("SUPABASE_DB_URL") or "").strip()
    key = (os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    base_url = (os.environ.get("SUPABASE_URL") or "").strip()
    if db_url:
        _apply_db(db_url, rows)
        return
    if key and base_url:
        _apply_rest(base_url, key, rows)
        return
    raise SeedSyncError(
        "SUPABASE_DB_URL or SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are unset. "
        "public.book_seeds was not written."
    )


def _apply_db(db_url: str, rows: list[dict]) -> None:
    try:
        import psycopg
    except ImportError as exc:
        raise SeedSyncError("psycopg is required for SUPABASE_DB_URL") from exc
    sleeves = [row["sleeve"] for row in rows]
    dates = [row["effective_from"] for row in rows]
    try:
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                for row in rows:
                    cur.execute(UPSERT_SQL, (row["sleeve"], row["seed_usd"], row["effective_from"]))
                cur.execute(DELETE_SQL, (sleeves, dates))
            conn.commit()
    except SeedSyncError:
        raise
    except Exception as exc:
        raise SeedSyncError("book_seeds upsert failed: " + _redact(str(exc), [db_url])) from None


def _apply_rest(base_url: str, key: str, rows: list[dict]) -> None:
    query = urllib.parse.urlencode({"on_conflict": "sleeve,effective_from"})
    _rest(
        base_url,
        key,
        "/rest/v1/book_seeds?" + query,
        method="POST",
        body=rows,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
    )
    _status, existing = _rest(
        base_url,
        key,
        "/rest/v1/book_seeds?select=sleeve,effective_from",
    )
    if not isinstance(existing, list):
        raise SeedSyncError("book_seeds read did not return a row list")
    kept = {(row["sleeve"], row["effective_from"]) for row in rows}
    for row in existing:
        if not isinstance(row, dict):
            continue
        sleeve = str(row.get("sleeve") or "")
        effective = str(row.get("effective_from") or "")[:10]
        if (sleeve, effective) in kept:
            continue
        filt = urllib.parse.urlencode(
            {"sleeve": f"eq.{sleeve}", "effective_from": f"eq.{effective}"}
        )
        _rest(base_url, key, "/rest/v1/book_seeds?" + filt, method="DELETE")


def _rest(base_url: str, key: str, path: str, method: str = "GET", body=None, extra_headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "the-book-seed-sync/1",
    }
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
            return response.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SeedSyncError(
            f"book_seeds {method} failed ({exc.code}): " + _redact(detail[:240], [key])
        ) from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise SeedSyncError("book_seeds network error: " + _redact(str(exc), [key])) from None


def _redact(text: str, secrets: list[str]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def self_test() -> int:
    rows = sync_rows()
    sleeves = {row["sleeve"] for row in rows}
    if sleeves != {"crypto", "equities", "combined"}:
        raise SeedSyncError(f"config sleeves drifted: {sorted(sleeves)}")
    if any(not row["seed_usd"] or not row["effective_from"] for row in rows):
        raise SeedSyncError("sync row is incomplete")
    if sync_rows() != rows:
        raise SeedSyncError("sync plan is not stable")
    if "on conflict (sleeve, effective_from)" not in UPSERT_SQL.lower():
        raise SeedSyncError("upsert is not idempotent")
    if "delete from public.book_seeds" not in DELETE_SQL.lower():
        raise SeedSyncError("sync would leave rows that are not in the config")
    loaded = book_seeds.current_seeds()
    by_sleeve = {row["sleeve"]: row["seed_usd"] for row in rows}
    for sleeve, seed in loaded.items():
        if by_sleeve.get(sleeve) != format(seed, "f"):
            raise SeedSyncError(f"sync row does not match the loader for {sleeve}")
    print("self-test ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rows from config/book_seeds.json and do not write",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Check the loader and the upsert plan, then exit",
    )
    args = parser.parse_args(argv)
    try:
        if args.self_test:
            return self_test()
        rows = sync_rows()
        if args.dry_run:
            print(json.dumps(rows, indent=2))
            print("dry-run: public.book_seeds was not written")
            return 0
        apply_rows(rows)
    except SeedSyncError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"upserted {len(rows)} book seed rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
