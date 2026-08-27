-- admin_perf_migration.sql
-- -----------------------------------------------------------------------------
-- Admin dashboard performance: kill the N+1 / full-table-scan slowness.
-- Run this file once in Supabase Dashboard > SQL Editor (idempotent).
--
-- Why indexes alone were not enough:
--   * A B-tree index gives O(log n) for a SINGLE lookup, but the app was doing
--     up to (1 + 2N) sequential HTTP requests to PostgREST (1x accounts +
--     Nx profiles + Nx lock_state). That is network-latency-bound, not
--     CPU-bound — indexes cannot reduce the COUNT of round trips.
--   * The real fix = (a) batch those per-row lookups into 2 bulk queries,
--     (b) push aggregation into SQL via the existing admin_* views, and
--     (c) include covering indexes so those aggregate queries are index-only.
-- -----------------------------------------------------------------------------
begin;

-- ---- 1) Covering index for the admin_account_stats view --------------------
-- The view joins trading_accounts X trade_orders on account_id and counts rows
-- filtered by status/profit. A covering (INCLUDE) index lets Postgres answer the
-- count/sum entirely from the index, no heap fetch => index-only scan, ~O(#orders).
drop index if exists idx_orders_admin_stats;
create index idx_orders_admin_stats
  on public.trade_orders (account_id, status)
  include (profit, quantity, closed_at);

-- ---- 2) Covering index for admin_daily_profit (group by day, status='closed') ----
-- Supports the WHERE status='closed' AND closed_at IS NOT NULL + GROUP BY date_trunc(day, closed_at).
create index if not exists idx_orders_closed_day
  on public.trade_orders (status, closed_at desc)
  where status = 'closed' and closed_at is not null;

-- ---- 3) Covering index for admin_symbol_stats (group by symbol) ----------------
create index if not exists idx_orders_symbol
  on public.trade_orders (symbol, status)
  include (profit, quantity);

-- ---- 4) Help a build a dashboard without pulling every row ----------------------
-- The view below already pre-aggregates per account and is READY to be used by
-- /api/admin/overview instead of list_accounts()+get_all_trades() in Python.
-- It supports the per-account stats endpoint as well.
create or replace view public.admin_account_summary with (security_invoker = true) as
select a.id                                              as account_id,
       a.owner_user_id,
       a.status,
       a.is_active,
       a.lock_state,
       a.lock_reason,
       p.display_name,
       p.roles,
       coalesce(s.total_closed, 0)                       as total_trades,
       coalesce(s.winning_trades, 0)                     as winning_trades,
       coalesce(s.losing_trades, 0)                      as losing_trades,
       coalesce(round(s.total_profit, 2), 0)              as total_profit,
       coalesce(round(s.total_volume, 2), 0)              as total_volume
from public.trading_accounts a
left join public.user_profiles p on p.user_id = a.owner_user_id
left join public.admin_account_stats s on s.account_id = a.id;

commit;