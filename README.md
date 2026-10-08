# The Book

Read-only dashboard for The Book. The live page is **one crypto book**. The value is the Agentic account (cash plus each holding). Equities are $0 and are not a second book.

The page is static. It does not place orders, and it does not call Supabase from the browser. It reads `data/live_book.json`. `book_usd` is cash: the sum of the holding values. `running_balance_usd` is the agentic total: that cash plus each marked open lot. The card running balance is the same total. Realized, unrealized, and running P&L on the card are that balance minus the $800 combined seed, with realized plus unrealized equal to running. Day P&L is `day_pnl_usd`, written from `day_realized_usd` on the Robinhood MCP cash drop when Crypto Desk includes it. The page also reads the public signal file for `signal_book_usd`. That fetch does not replace account holdings, a day already on the file, or the snapshot P&L fields, and it does not copy the signal book onto `book_usd` or onto the running-balance line. It does not use a secret.

- Repo: https://github.com/jrg185/the-book
- Pages: https://jrg185.github.io/the-book/

## What the board shows

The account card is one crypto book. Running balance is RH cash plus each marked open lot. That total is `running_balance_usd`. `book_usd` is the cash holdings sum alone. The running balance is not the signal book and not the scrubbed $300 / $500 divisors. Holdings are the names and `value_usd` on that file. Tape coins that are not on that list are not open positions. Equities are not drawn as a book.

The card also shows day P&L (`day_pnl_usd`), the −10% day kill and +2.5% day target as dollars of the running balance (RH cash plus USDC plus open marks), and kill headroom. Kill headroom is `max(0, |dayKillFrac| × running balance + min(day_pnl_usd, 0))`, where `dayKillFrac` is `LIVE_RAILS.dayKillFrac` in `derive.js`. Export writes that same dollar onto `kill_remaining_usd`, so the card and the file match. A positive day does not add headroom. The rails are not fractions of `signal_book_usd` and not of the cash sum alone. Running P&L is the agentic total minus the $800 combined seed. Unrealized is the open lots' mark versus cost. Realized is running minus unrealized. When holdings and a position list are present, export writes `running_balance_usd` and `running_pnl_usd` from that total and does not take those two from the warehouse. It still writes the latest `combined` snapshot's `realized_pnl_usd` and `unrealized_pnl_usd`, and stamps that row's `as_of` onto `sleeve_as_of`. The snapshot table has no `day_pnl_usd` column. A missing `day_realized_usd` on the cash drop leaves the signal day in place and logs the absence. The read filters `sleeve=combined` and takes one row. It does not divide them by the sleeve seed, and it does not substitute the crypto sleeve row. The crypto row of `data/kpi_summary.json` stays on the old sleeve seed and is not this card. A missing holding value stays blank. A book with no position list still shows the stored running balance.

Open positions on that file are net size from `public.kpi_trades` (`sleeve`, `ticker`, `side`, `qty`, `avg_price`, `pnl_trade_usd`, `timestamp_et`). A non-zero net is open. The mark is the same quote the snapshot uses for `unrealized_pnl_usd`. Each open row's `running_pnl_usd` is that ticker's close `pnl_trade_usd` plus `qty * (mark - avg)`, the same two terms the combined snapshot adds into `realized_pnl_usd` and `unrealized_pnl_usd`. Export writes each open net onto `positions` with `value_usd` = qty × mark and that running figure. There is no positions table. USD and USDC stay the cash lines, with no P&L on those rows. A flat name stays off the list. Equities are not a second book.

`SEEDS_USD` in `derive.js` ($300 crypto, $500 equities, $800 combined) turns scrubbed fill fractions back into trade dollars. The $800 combined figure is also the seed the card subtracts from the RH balance. It is not the account book. The crypto curve is that old fraction history, not a second dollar book. Tape running P&L is still the sleeve seed fraction and is not this card.

## Data path

Source of truth is the Supabase project **agentic-signals** (`bsnqwgbshwszbjncglqx`).

```
https://bsnqwgbshwszbjncglqx.supabase.co
```

`public.kpi_summary` is a view over the latest `public.kpi_sleeve_snapshots` row. Export only SELECTs that view, so it cannot move `as_of`. The Action runs `scripts/refresh_kpi_snapshots.py` first. That script reads `public.kpi_trades` (qty and price), marks open positions, and INSERTs a new snapshot per sleeve. It does not read `public.kpi_trades_scrubbed`.

GitHub Actions then writes the scrubbed views into the repo:

- `public.kpi_summary` → `data/kpi_summary.json`
- `public.kpi_trades_scrubbed` → `data/kpi_trades_scrubbed.json`
- `public.models_oos` → `data/models_oos.json` when that view exists
- provenance → `data/meta.json`

Pages serves that committed JSON. The browser only fetches `data/*.json`.

Workflow: [`.github/workflows/export-kpi.yml`](.github/workflows/export-kpi.yml) (same bytes as [`scripts/export-kpi.yml`](scripts/export-kpi.yml)).

- `workflow_dispatch`, `repository_dispatch` type `rh-fill`, pull requests (position math and the RH fill mapper), and pushes to `main` other than `data/**`
- schedule: `0 * * * *` is the Robinhood poll and skips when `RH_API_KEY` or `RH_BASE64_PRIVATE_KEY` is unset. When the poll runs, it reads `RH_AGENTIC_ACCOUNT` and fails if that secret is unset. Mark refresh stays every 15 minutes on weekdays from 13:00–21:45 UTC, and hourly outside that window including weekends. Those mark crons do not call Robinhood
- A fill payload, or a secrets-backed hourly poll, upserts `public.kpi_trades`, then the same run refreshes `kpi_sleeve_snapshots` and exports. A bad requested payload, or a failed refresh, does not commit KPI JSON
- Reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. `SUPABASE_DB_URL` is optional
- Crypto marks: public Coinbase ticker, then Yahoo `{SYMBOL}-USD`. Equities marks: Finnhub when `FINNHUB_API_KEY` is set, then Yahoo chart. CoinStats and Alpha Vantage are later fallbacks when those keys are set
- Writes `data/kpi_summary.json`, `data/kpi_trades_scrubbed.json`, `data/models_oos.json`, `data/model_scorecard.json`, and `data/meta.json` when they changed. The same run rewrites `data/live_book.json` so `book_usd` is the cash holdings sum. When holdings and a position list are present, `running_balance_usd` is that cash plus open lots and `running_pnl_usd` is that total minus the combined seed. Realized and unrealized still come from the latest `sleeve=combined` row of `public.kpi_sleeve_snapshots`, and that row's `as_of` is stamped onto `sleeve_as_of`. The run writes each open net from `public.kpi_trades` onto `positions`, and replaces the USD and USDC holding lines from Robinhood cash when a read is available (signed REST if `RH_API_KEY` and `RH_BASE64_PRIVATE_KEY` are set, otherwise `data/rh_cash.json`). Unset keys skip REST and do not fail the export. A missing or invalid drop leaves those cash lines in place. The book card uses `sleeve_as_of` as the MTM clock. `generated_at` stays the signal file time. It keeps `signal_book_usd` from the signal file when that file is readable. Day P&L is `day_realized_usd` from the cash drop when that number is present; otherwise the signal day stays and the absence is logged. Kill headroom is recomputed from the running balance and that day. It does not copy the signal book onto `book_usd` or onto the running-balance line, and it does not clear day P&L when the snapshot has no day column. The commit step `git add`s `data/live_book.json` and `data/model_scorecard.json` with the other KPI files. `SUPABASE_DB_URL` is passed into the export step so fee and signal-linkage reads can fall back to SQL.
- Does not rewrite `data/models.json`
- Does not deploy Pages and does not change the Pages source

The board reads `meta.fetched_at` as **Last refreshed** in America/New_York, and each sleeve `as_of` the same way. A healthy export sets `meta.export_status` to `ok`. If the service role key is missing, the script leaves the KPI files alone, sets `export_status` to `stale`, and exits 0. If the refresh throws (read-only filesystem, disk full, or a failed REST read), it stamps `export_status` `error` on `meta.json` only and exits 1. The workflow then commits that meta file and stays red. The page shows an **Export failed** or **Stale snapshot** chip plus the error copy, and it does not invent new KPI numbers. If a crash cannot write `meta.json`, the chip turns stale once `fetched_at` is older than 3 hours.

### Warehouse MTM

`public.kpi_summary` is a view over the latest `public.kpi_sleeve_snapshots` row. Re-exporting JSON cannot move `as_of` by itself.

The writer is in this repo: `scripts/refresh_kpi_snapshots.py`. Export KPI runs it after the optional Robinhood fill sync. It reads `public.kpi_trades` (qty and price), marks open names from public Coinbase and Yahoo quotes, and INSERTs a new snapshot with `as_of` set to now. It does not place orders and it does not change the table schema. Sibling `upsert-warehouse` Actions in `agentic-crypto-signals` and `agentic-equity-signals` write `bars`, `features`, `labels`, and `model_runs`. They are not the sleeve MTM writer.

Order:

1. The hourly poll, or a desk-sent fill, inserts Robinhood fills into `kpi_trades`. The mark-refresh crons skip this step.
2. Refresh inserts `kpi_sleeve_snapshots`.
3. Export reads `kpi_summary` and `kpi_trades_scrubbed` and commits JSON only if `as_of` is within 15 minutes.
4. Pages shows that `as_of` on the book card and the status line. It does not use signal `generated_at` as the sleeve age. When `data/live_book.json` has no `sleeve_as_of` yet, the page uses the combined `kpi_summary` `as_of`.

If the INSERT fails because the database is read-only (25006) or the disk is full, the script leaves the KPI numbers alone and stamps `meta.warehouse_status`. The chip reads **Warehouse read-only** or **Warehouse disk full**, with copy `snapshot frozen at` the last committed sleeve time. The Action publishes that meta file and stays red.

When `meta.source` is `supabase` and the latest sleeve `as_of` is older than 60 minutes, and the warehouse did not report read-only or disk full, the chip is **MTM stale**. Cache-busting the JSON does not make that snapshot current.

Export KPI does not read a Google Sheet. A one-shot notes backfill can copy human Why/Notes into `kpi_trades` from a CSV export of the ledger sheet. That script is not on the export path. See [Notes backfill](#notes-backfill).

The live `kpi_summary` view uses warehouse names. Export remaps them onto the page shape before writing JSON: `running_bal_vs_start` → `running_balance_frac`, `pnl_pct_of_book` → `running_pnl_frac`, `notes` → `note`. `sleeve`, `as_of`, `day_kill_pct`, `day_target_pct`, and `kill_headroom_frac` stay as they are. When both a warehouse book ratio and a cash residual are present, the warehouse ratio wins.

### View contract

`kpi_summary` rows:

| Column | Meaning |
| --- | --- |
| `sleeve` | `crypto`, `equities`, or `combined` |
| `as_of` | Snapshot timestamp |
| `running_balance_frac` | Sleeve book ÷ seed. Legacy sheet example, not a live two-desk book: crypto 1.079233 ($323.77 / $300), equities 1.00174 ($500.87 / $500), combined 1.0308 ($824.64 / $800). |
| `running_pnl_frac` | Running P&L ÷ seed |
| `day_pnl_frac` | Day P&L ÷ seed, or null |
| `day_kill_pct` | Kill rail as a fraction of book (`-0.10` = −10%). Percent points such as `-10` are also accepted. |
| `kill_headroom_frac` | Room left inside the kill rail, as a fraction of book |
| `day_target_pct` | Target as a fraction of book (`0.025` = +2.5%), or null |
| `note` | Short scrubbed note. No names, emails, or account ids. |

Optional divisor override on a scrubbed row: `start`, `seed`, `start_usd`, `seed_usd`, or `book_usd`. Fill dollars use that divisor, or `SEEDS_USD` when it is absent. The account card does not. Its book is the sum of the live-book holdings.

`kpi_trades_scrubbed` rows:

| Column | Meaning |
| --- | --- |
| `sleeve` | `crypto` or `equities` |
| `ts` | Fill time |
| `ticker` | Symbol |
| `side` | `buy` or `sell` |
| `qty` | Quantity |
| `pnl_frac` | Trade P&L ÷ sleeve seed |
| `running_pnl_frac` | Cumulative realized P&L through that fill ÷ sleeve seed. Export regenerates this per sleeve in timestamp order. Crypto seed $300, equities seed $500. |
| `running_balance_frac` | Book at that fill ÷ sleeve seed, where book = start + cumulative realized P&L. Not cash leftover and not open-position mark-to-market. |
| `why` | Full note. No `left()` truncation. No PII. A machine `RH Agentic backfill order <uuid>` or `RH Agentic sync order <uuid>` string is not the human note. New fills leave `why` empty unless the payload has a human note. |
| `fee_frac_of_book` | Fill fee ÷ sleeve seed, when the warehouse row had `fee_usd`. The page shows seed × this fraction. Raw `fee_usd` dollars and order ids are not written. |

Export always recomputes `running_pnl_frac` and `running_balance_frac` from warehouse trade P&L before writing JSON. It does not copy a sheet running balance. When the scrubbed row includes `pnl_trade_usd`, the sum is dollars then ÷ seed. Otherwise it sums `pnl_frac_of_book` (each value is already trade P&L ÷ seed). `why` is written in full. If `why` is a machine order string and `notes` is human, the JSON `why` is the notes text.

The exporter drops `email`, `phone`, `order_id`, `account_id`, `user_id`, `api_key`, `service_role`, `secret`, `password`, `token`, `ssn`, and `address` if a view ever returns them. It also drops JWT-shaped strings.

`models_oos` is optional. The exporter does not fail the job when that view is absent.

The Models tab reads `data/models.json` (same shape in `fixtures/models.json`). Each card has `name`, `sleeve`, `used`, `training`, `data_source`, and `oos` (`window`, `hit_rate`, `avg_return`, `n`, `note`). Until T04 publishes metrics, `oos.status` is `placeholder` and the three numbers stay null. Replacing the file is enough; the page does not need a code change.

The crypto scorecard on that tab is T24e. It reads the scrubbed tape, `data/kpi_summary.json`, `data/models.json`, and `data/models_oos.json`. Export also writes `data/model_scorecard.json` so a warehouse fee sum, and `signal_artifacts` / `signal_trade_outcomes` counts, can land without putting order ids or a service role in the browser. Closed-fill win rate uses the same rule as sleeve Win %: crypto sells, finite P&L, flat zero excluded. The scrubbed tape has no order id, so those rows are not collapsed. T24b will improve joined-fill metrics. Fee drag is UNKNOWN when no fee column is present. Artifact and outcome counts are UNKNOWN until that export can read them. Out-of-sample after-cost is labeled 30 bp until T24d fee-corrects it. The live backend stays `--backend rules` while LightGBM is promoted and unused. The equities card stays the last published research read. Modeling and place on that desk are paused.

### Secrets

Until `SUPABASE_SERVICE_ROLE_KEY` is set, the site ships the sample in `data/` (same bytes as `fixtures/`). `meta.json` says `"source": "sample"`.

Add these repository secrets (Settings → Secrets and variables → Actions). Do not commit them. Do not put them in client JavaScript.

| Secret | Use |
| --- | --- |
| `SUPABASE_URL` | `https://bsnqwgbshwszbjncglqx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | PostgREST read of `kpi_trades` and INSERT into `kpi_sleeve_snapshots`, then `GET /rest/v1/<view>?select=*`. Until it is set, a local export leaves the committed JSON alone. The Action refresh step exits non-zero instead, so it does not commit a stale snapshot. |
| `SUPABASE_DB_URL` | Optional. Used when REST cannot read `kpi_trades` or the INSERT is rejected. |
| `FINNHUB_API_KEY` | Optional equities mark. Yahoo chart is the public fallback. |
| `COINSTATS_API_KEY` | Optional crypto mark after Coinbase and Yahoo. |
| `ALPHA_VANTAGE_API_KEY` | Optional equities mark after Finnhub and Yahoo. |
| `RH_AGENTIC_ACCOUNT` | Agentic account for the hourly poll and for signed cash REST. Set this repository secret before merge. The poll and the cash read fail if it is unset. |

Public Coinbase and Yahoo marks do not need those quote keys. `ROBINHOOD_TOKEN` is not used. Optional later, not required to merge: `RH_API_KEY` and `RH_BASE64_PRIVATE_KEY` turn on the Actions hourly poll. That poll reads the Agentic account from `RH_AGENTIC_ACCOUNT` and fails if the variable is unset.

### Agentic cash

Export KPI writes the USD and USDC lines on `data/live_book.json` from a cash read, then recomputes `book_usd` as the cash holdings sum. `running_balance_usd` is the agentic total: that cash plus each marked open lot. The day kill and the day target stay fractions of that running balance. Kill headroom (`kill_remaining_usd`) is `max(0, |dayKillFrac| × running balance + min(day_pnl_usd, 0))`, using `LIVE_RAILS.dayKillFrac` from `derive.js`.

The read order is signed Robinhood REST when `RH_API_KEY` and `RH_BASE64_PRIVATE_KEY` are both set, otherwise `data/rh_cash.json`, otherwise the cash lines already on the file. Unset keys skip REST. That skip does not fail the export, including when `KPI_REFRESH_EXPECTED=1`. When those keys are set, the cash read uses `RH_AGENTIC_ACCOUNT` and fails if it is unset. The hourly `0 * * * *` poll stays skip-when-unset and does not call Robinhood. The API key secrets stay optional.

`data/rh_cash.json` is the desk drop. `USD` and `USDC` are numbers. `as_of` is optional ISO8601. Unknown keys are ignored. A missing or invalid file is not a balance.

`day_realized_usd` is optional. It is the net crypto day P&L from Robinhood MCP `get_realized_pnl` with `span=day`: `total_returns` (gross) minus that day's sell-side fees on filled crypto orders. Export copies it onto `day_pnl_usd` as-is and uses that same net in `kill_remaining_usd`. Export does not apply a fee rate and does not recompute the net. When the field is absent, the signal day stays and export logs the absence. The scheduled export still finishes.

`day_realized_gross_usd` and `day_sell_fees_usd` are optional audit fields. When they are present, export copies them onto `data/live_book.json`. If all three numbers are present and gross minus sell fees differs from `day_realized_usd` by more than $0.01, export prints a warning and does not fail. The published day stays the net figure.

Desk path: Robinhood MCP on the Agentic account named by `RH_AGENTIC_ACCOUNT` → write `data/rh_cash.json` → run **Export KPI**. A push that only touches `data/**` does not start the workflow. Do not hardcode live balances in `derive.js`, the rail math, or `scripts/export_kpi.py`. The drop file is the source of truth until Desk replaces it.

### RH fill ingest

Robinhood has no fill webhook. Standing sync is an hourly poll. Tonight that poll is Crypto Desk, not Actions. The `0 * * * *` cron skips until the two API secrets exist. The `*/15` cron only refreshes marks.

Once an hour, from a checkout of this repo:

```bash
git pull
python3 scripts/sync_rh_kpi_trades.py --print-cursor
```

Call Robinhood Trading MCP `get_crypto_orders` with `rhs_account_number` set to `RH_AGENTIC_ACCOUNT`, `state` `filled`, and `updated_at_gte` set to the printed timestamp. If the response has `next`, call again with `cursor` set to that value until `next` is absent. Save the orders as one JSON document (`{"results":[...]}` or `{"data":{"results":[...]}}` or a list). USDC is skipped by the script. Sleeve is `crypto` or `equities` from asset class.

```bash
gh workflow run export-kpi.yml --repo jrg185/the-book -f sync_rh_json="$(cat fills.json)"
```

That run upserts, refreshes, and exports. When `data/rh_kpi_sync_cursor.json` changes, the export commit stores the next poll timestamp. Pull before the next hour. A local upsert, if you already have the Supabase secrets, is `python3 scripts/sync_rh_kpi_trades.py --from-json fills.json`.

Bonus, not the standing path: send one fill as soon as you see it. Crypto or Equities Desk can use the same schema.

```bash
gh api repos/jrg185/the-book/dispatches --method POST --input - <<'JSON'
{
  "event_type": "rh-fill",
  "client_payload": {
    "id": "11111111-1111-4111-8111-111111111111",
    "currency_code": "GRT",
    "side": "buy",
    "state": "filled",
    "cumulative_quantity": "100",
    "average_price": "0.05",
    "rounded_executed_notional": "5",
    "fee": "0.01",
    "created_at": "2026-09-28T18:00:00Z"
  }
}
JSON
```

`client_payload` must be a JSON object. One order, `{"results":[...]}`, or `{"data":{"results":[...]}}` all work. A list is not valid as `client_payload`. Use the workflow input for a list:

```bash
gh workflow run export-kpi.yml --repo jrg185/the-book -f sync_rh_json="$(cat fills.json)"
```

The Action writes that payload to `/tmp/rh-fill.json` and runs `scripts/sync_rh_kpi_trades.py --from-json`. The same run then refreshes sleeve snapshots and exports. USDC and funding pairs are skipped. `sleeve` is `crypto` or `equities` from `asset_class` (a bare equity symbol also maps to `equities`). An object with `"asset_class": "equity"` and `"symbol": "QCOM"` is sleeve `equities`. Extra MCP fields and account numbers are not written. The Robinhood id is stored in `order_id` only. `why` and `notes` stay null unless the fill carries a human `why`, `notes`, `note`, or `exit`. A desk `exit` string is folded into `notes` and is not its own column. A machine `RH Agentic backfill|sync order <uuid>` string and the bare placeholder `backfill from RH` are not stored. On conflict, why/notes are updated only when the new why is human and the stored why is empty or a machine stub. A stored human why is left unchanged, including when a later payload is empty or a stub. A later hourly poll can attach ledger why, notes, or exit to an existing stub by `order_id`.

A local upsert, without Actions, is the same mapper:

```bash
python3 scripts/sync_rh_kpi_trades.py --from-json fills.json
```

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` perform that upsert. `SUPABASE_DB_URL` is optional and is not on the repo today. REST is enough after the SQL below has been applied. A new `order_id` is `POST /rest/v1/kpi_trades?on_conflict=order_id` with `Prefer: resolution=ignore-duplicates`, so qty and price are not rewritten. When that order is already stored, a human `why` / `notes` / `exit` on a later payload is a PATCH of those columns only, and only while the stored why is still empty or a machine stub.

The pull-request check only runs `--self-test`. If a fill was sent and the JSON is bad, the sync exits 1 and stamps `data/meta.json` (**Export failed**). A scheduled run with no payload skips the sync and still refreshes.

Apply [`scripts/migrate_kpi_trades_order_id.sql`](scripts/migrate_kpi_trades_order_id.sql) before the first upsert. It adds nullable `order_id text`, copies uuids out of `RH Agentic backfill order <uuid>` / `RH Agentic sync order <uuid>`, and creates a unique index. When `SUPABASE_DB_URL` is set, the sync runs that file itself. A repeat `order_id` updates `why` and `notes` only when the new why is human and the stored why is empty or a machine stub. A stored human why is not replaced.

```bash
psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -f scripts/migrate_kpi_trades_order_id.sql
```

After the migration is applied, the hourly `gh workflow run` upserts, refreshes, and exports, so a new fill shows on https://jrg185.github.io/the-book/ when that run finishes. The 15-minute cron does not call Robinhood.

After the Supabase secrets are saved, run **Actions → Export KPI → Run workflow**. A successful export sets `meta.source` to `supabase` and replaces the KPI JSON. `fixtures/` and `data/models.json` stay as they are.

If the service role secret is unset, a local `scripts/export_kpi.py` exits 0, leaves the KPI JSON alone, and marks `meta.export_status` as `stale`. `KPI_REFRESH_EXPECTED=1` (set on the export step after refresh) exits non-zero instead.

## GitHub Pages

Pages is a legacy site: branch `main`, path `/` (repository root). Pushing `index.html`, `styles.css`, `app.js`, and `data/*.json` to `main` publishes them. Do not switch the source to GitHub Actions.

The site is https://jrg185.github.io/the-book/

## Local preview

```bash
python3 scripts/refresh_kpi_snapshots.py --self-test
python3 scripts/sync_rh_kpi_trades.py --self-test
python3 scripts/sync_rh_kpi_trades.py --dry-run
python3 scripts/export_kpi.py --install-sample
python3 -m py_compile scripts/export_kpi.py
python3 -m unittest scripts/test_export_status.py
python3 -m http.server 8765
```

Dry-run reads `kpi_trades` and prints the rows it would insert. It does not write:

```bash
SUPABASE_URL=https://bsnqwgbshwszbjncglqx.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=... \
python3 scripts/refresh_kpi_snapshots.py --dry-run
```

After a live run, `as_of` should be the run time:

```sql
select sleeve, as_of, realized_pnl_usd, unrealized_pnl_usd,
       running_pnl_usd, running_balance_usd, start_balance_usd
from public.kpi_sleeve_snapshots
order by as_of desc
limit 6;
```

Open http://127.0.0.1:8765/

## Tape running ledger

Local checks for this change:

```bash
python3 scripts/export_kpi.py --self-test
python3 scripts/backfill_notes_from_sheet.py --self-test
python3 -m py_compile scripts/export_kpi.py scripts/backfill_notes_from_sheet.py
python3 -m unittest scripts/test_export_status.py
```

`scripts/export_kpi.py` fills Running P&L and Running balance on every export, including before the SQL view is updated. The view should match that math so a direct `select` from `kpi_trades_scrubbed` is the same ledger.

Apply [`scripts/migrations/20260928_kpi_trades_running_ledger.sql`](scripts/migrations/20260928_kpi_trades_running_ledger.sql) on **agentic-signals** (`bsnqwgbshwszbjncglqx`):

1. Supabase → SQL editor → paste the file → Run.
2. The script keeps the previous view as `public.kpi_trades_scrubbed_prev` the first time, then `create or replace`s `public.kpi_trades_scrubbed` with window sums of `pnl_trade_usd`. It reloads the PostgREST schema cache.
3. Actions → Export KPI → Run workflow.
4. Hard-refresh https://jrg185.github.io/the-book/

After that, the last crypto fill has non-null `running_pnl_frac` and `running_balance_frac`. On a fill row, Running P&L and Running balance recover dollars from that fraction. They are not the account card. The account card running balance is RH cash plus marked lots. Why shows the full note (wraps, and the cell `title` is the same text).

Check:

```sql
select sleeve, timestamp_et, ticker, side,
       running_pnl_frac, running_balance_frac, length(why) as why_len
from public.kpi_trades_scrubbed
where lower(sleeve) = 'crypto'
order by timestamp_et desc, ticker desc, side desc
limit 1;
```

The migration does not write `kpi_trades` and does not place orders. It does not change `scripts/sync_rh_kpi_trades.py`.

## Notes backfill

One shot, not a live feed. Export the private Agentic Trading Ledger tabs **Crypto** and **Equities**. The sheet id is `YOUR_SHEET_ID` (do not commit the live link).

Export each tab to CSV. Then, with the service role or `SUPABASE_DB_URL`:

```bash
python3 scripts/backfill_notes_from_sheet.py \
  --csv crypto.csv --sleeve crypto \
  --csv equities.csv --sleeve equities \
  --dry-run

SUPABASE_URL=https://bsnqwgbshwszbjncglqx.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=... \
python3 scripts/backfill_notes_from_sheet.py \
  --csv crypto.csv --sleeve crypto \
  --csv equities.csv --sleeve equities \
  --apply
```

The script matches sheet `order_id` to `kpi_trades.order_id` or to a UUID still stored in `why`, else sleeve + ticker + side + qty + timestamp (sheet clocks are America/New_York). Sheet human why replaces a machine warehouse why. When the sheet does not mention a row, and warehouse `notes` is human while `why` is `RH Agentic backfill|sync order <uuid>`, notes is copied onto why. It does not insert rows and does not change qty.

Dry-run is the default: the commands above with `--dry-run`, or with neither flag, print the plan and write nothing. `--apply` writes `why` and `notes`. Passing both `--dry-run` and `--apply` still does not write. Export KPI does not run this script and does not read the sheet. Re-run Export KPI afterward so Pages picks up the notes. The cloud agent that added this script could read the sheet and could not write `kpi_trades` (no service role in that environment).
