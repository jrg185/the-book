-- Book seeds for public.kpi_trades_scrubbed.
--
-- Apply order on Supabase project agentic-signals (bsnqwgbshwszbjncglqx):
--   1. This migration. It creates public.book_seeds, inserts the same rows as
--      config/book_seeds.json, and redefines the view to divide by the join.
--   2. Seed sync. Export KPI runs scripts/sync_book_seeds.py, which upserts
--      config/book_seeds.json again. That file stays the source.
--   3. View use. The export reads public.kpi_trades_scrubbed after the sync.
--
-- Do not apply this file from a laptop. Eng applies it after merge.
-- This does not insert fills, does not place orders, and does not read a sheet.
--
-- public.kpi_trades_scrubbed_prev already exists from 20260928. Renaming it
-- back would restore the pre-ledger view, not the view this migration replaces.
-- Rollback recreates that pre-20261008 definition, then drops public.book_seeds:
--
-- begin;
--     create or replace view public.kpi_trades_scrubbed as
--     with fills as (
--       select
--         t.sleeve,
--         t.timestamp_et,
--         t.ticker,
--         t.side,
--         case lower(btrim(t.sleeve))
--           when 'crypto' then 300::numeric
--           when 'equities' then 500::numeric
--         end as seed,
--         (%s)::numeric as notional_usd,
--         coalesce(t.pnl_trade_usd, 0)::numeric as pnl_usd,
--         (%s)::text as why,
--         t.ctid as row_ctid
--       from public.kpi_trades t
--     ),
--     ledger as (
--       select
--         sleeve,
--         timestamp_et,
--         ticker,
--         side,
--         case
--           when seed is null or seed = 0 or notional_usd is null then null
--           else round(notional_usd / seed, 6)
--         end as notional_frac_of_book,
--         case
--           when seed is null or seed = 0 then null
--           else round(pnl_usd / seed, 6)
--         end as pnl_frac_of_book,
--         why,
--         seed,
--         sum(pnl_usd) over (
--           partition by lower(btrim(sleeve))
--           order by timestamp_et asc, ticker asc, side asc, row_ctid asc
--           rows between unbounded preceding and current row
--         ) as running_pnl_usd
--       from fills
--     )
--     select
--       sleeve,
--       timestamp_et,
--       ticker,
--       side,
--       notional_frac_of_book,
--       pnl_frac_of_book,
--       why,
--       case
--         when seed is null or seed = 0 then null
--         else round(running_pnl_usd / seed, 6)
--       end as running_pnl_frac,
--       case
--         when seed is null or seed = 0 then null
--         else round((seed + running_pnl_usd) / seed, 6)
--       end as running_balance_frac
--     from ledger;
-- drop table public.book_seeds;
-- notify pgrst, 'reload schema';
-- commit;
--
-- The two %s placeholders are the notional and why expressions from
-- scripts/migrations/20260928_kpi_trades_running_ledger.sql. A failure inside
-- this transaction rolls back. The live view stays as it was.

begin;

create table if not exists public.book_seeds (
  sleeve text not null,
  seed_usd numeric not null,
  effective_from date not null,
  primary key (sleeve, effective_from),
  constraint book_seeds_seed_positive check (seed_usd > 0)
);

alter table public.book_seeds enable row level security;

revoke all on public.book_seeds from anon, authenticated;

comment on table public.book_seeds is
  'Per-sleeve book seeds. config/book_seeds.json is the source. Apply this migration, run scripts/sync_book_seeds.py, then read public.kpi_trades_scrubbed.';

-- Same rows as config/book_seeds.json. sync_book_seeds.py upserts that file
-- on Export KPI so a later edit stays the source.
insert into public.book_seeds (sleeve, seed_usd, effective_from) values
  ('crypto', 300, date '2020-01-01'),
  ('equities', 500, date '2020-01-01'),
  ('combined', 800, date '2020-01-01')
on conflict (sleeve, effective_from) do update
set seed_usd = excluded.seed_usd;

do $mig$
declare
  notional_sql text;
  why_sql text;
  has_notes boolean;
  has_notional_usd boolean;
  has_notional boolean;
  prev_def text;
begin
  if to_regclass('public.kpi_trades') is null then
    raise exception 'public.kpi_trades is missing on this database';
  end if;
  if not exists (
    select 1
    from information_schema.columns
    where table_schema = 'public'
      and table_name = 'kpi_trades'
      and column_name = 'pnl_trade_usd'
  ) then
    raise exception 'public.kpi_trades.pnl_trade_usd is missing';
  end if;

  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'kpi_trades' and column_name = 'notional_usd'
  ) into has_notional_usd;
  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'kpi_trades' and column_name = 'notional'
  ) into has_notional;
  select exists (
    select 1 from information_schema.columns
    where table_schema = 'public' and table_name = 'kpi_trades' and column_name = 'notes'
  ) into has_notes;

  if has_notional_usd and has_notional then
    notional_sql := 'coalesce(t.notional_usd, t.notional, t.qty * t.avg_price)';
  elsif has_notional_usd then
    notional_sql := 'coalesce(t.notional_usd, t.qty * t.avg_price)';
  elsif has_notional then
    notional_sql := 'coalesce(t.notional, t.qty * t.avg_price)';
  else
    notional_sql := '(t.qty * t.avg_price)';
  end if;

  if has_notes then
    why_sql := $why$
      case
        when t.why ~* '^RH Agentic (backfill|sync) order [0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
          and nullif(btrim(t.notes), '') is not null
          and btrim(t.notes) !~* '^RH Agentic (backfill|sync) order [0-9a-f]{8}-'
        then t.notes
        when nullif(btrim(t.notes), '') is not null
          and t.why is not null
          and length(t.notes) > length(t.why)
          and left(t.notes, length(t.why)) = t.why
          and btrim(t.notes) !~* '^RH Agentic (backfill|sync) order [0-9a-f]{8}-'
        then t.notes
        else t.why
      end
    $why$;
  else
    why_sql := 't.why';
  end if;

  -- Copy the old SELECT (pg_get_viewdef), so the backup does not depend on
  -- the view we are about to replace. Skip when prev already exists.
  if to_regclass('public.kpi_trades_scrubbed') is not null
     and to_regclass('public.kpi_trades_scrubbed_prev') is null then
    prev_def := pg_get_viewdef('public.kpi_trades_scrubbed'::regclass, true);
    prev_def := rtrim(prev_def);
    if right(prev_def, 1) = ';' then
      prev_def := left(prev_def, length(prev_def) - 1);
    end if;
    raise notice 'previous public.kpi_trades_scrubbed definition: %', prev_def;
    execute 'create view public.kpi_trades_scrubbed_prev as ' || prev_def;
  end if;

  execute format($view$
    create or replace view public.kpi_trades_scrubbed as
    with fills as (
      select
        t.sleeve,
        t.timestamp_et,
        t.ticker,
        t.side,
        bs.seed_usd as seed,
        (%s)::numeric as notional_usd,
        coalesce(t.pnl_trade_usd, 0)::numeric as pnl_usd,
        (%s)::text as why,
        t.ctid as row_ctid
      from public.kpi_trades t
      left join lateral (
        select s.seed_usd
        from public.book_seeds s
        where lower(btrim(s.sleeve)) = lower(btrim(t.sleeve))
          and (
            t.timestamp_et is null
            or s.effective_from <= t.timestamp_et::date
          )
        order by s.effective_from desc
        limit 1
      ) bs on true
    ),
    ledger as (
      select
        sleeve,
        timestamp_et,
        ticker,
        side,
        case
          when seed is null or seed = 0 or notional_usd is null then null
          else round(notional_usd / seed, 6)
        end as notional_frac_of_book,
        case
          when seed is null or seed = 0 then null
          else round(pnl_usd / seed, 6)
        end as pnl_frac_of_book,
        why,
        seed,
        sum(pnl_usd) over (
          partition by lower(btrim(sleeve))
          order by timestamp_et asc, ticker asc, side asc, row_ctid asc
          rows between unbounded preceding and current row
        ) as running_pnl_usd
      from fills
    )
    select
      sleeve,
      timestamp_et,
      ticker,
      side,
      notional_frac_of_book,
      pnl_frac_of_book,
      why,
      case
        when seed is null or seed = 0 then null
        else round(running_pnl_usd / seed, 6)
      end as running_pnl_frac,
      case
        when seed is null or seed = 0 then null
        else round((seed + running_pnl_usd) / seed, 6)
      end as running_balance_frac
    from ledger
  $view$, notional_sql, why_sql);
end
$mig$;

commit;

notify pgrst, 'reload schema';
