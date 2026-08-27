-- admin_dashboard_migration.sql
-- ---------------------------------------------------------------------------
-- Admin dashboard: soft-delete (block/unblock) + per-user roles + admin
-- aggregation views. Run in Supabase SQL Editor.
--
-- Soft delete semantics (never DELETE rows):
--   active <-> blocked   admin flips a trading account between the two.
--   Blocking revokes the user's active sessions and stops new sign-ins for that
--   account, while all trade/order history remains queryable by admins.
--   Unblocking restores login/trading without any data loss.
-- ---------------------------------------------------------------------------
begin;

-- ---- 1) trading_accounts: soft-delete fields + lock_state + extend status ----
-- lock_state/lock_reason/locked_at/unlocked_at come from lock_control_migration.sql
-- but are re-added idempotently here so this file is self-contained (order of
-- running the migration files no longer matters).
alter table public.trading_accounts
  add column if not exists lock_state text not null default 'unlocked'
    check (lock_state in ('unlocked', 'soft_locked', 'hard_locked'));
alter table public.trading_accounts
  add column if not exists lock_reason text,
  add column if not exists locked_at timestamptz,
  add column if not exists unlocked_at timestamptz,
  add column if not exists blocked_reason text,
  add column if not exists blocked_at timestamptz,
  add column if not exists unblocked_at timestamptz;

create index if not exists idx_accounts_lock_state on public.trading_accounts(lock_state) where lock_state <> 'unlocked';

-- Extend the status CHECK constraint to accept 'blocked' (drop & recreate).
alter table public.trading_accounts
  drop constraint if exists trading_accounts_status_check;
alter table public.trading_accounts
  add constraint trading_accounts_status_check
    check (status in ('active', 'suspended', 'blocked', 'archived'));

-- ---- 2) user-level roles (source of truth for authorization) --------------
alter table public.user_profiles
  add column if not exists roles jsonb not null default '["trader"]'::jsonb;

-- ---- 3) Admin aggregation views -------------------------------------------
create or replace view public.admin_account_stats with (security_invoker = true) as
select a.id as account_id,
       a.owner_user_id,
       a.status,
       a.is_active,
       a.lock_state,
       a.lock_reason,
       count(t.id) filter (where t.status = 'closed')            as total_closed,
       count(t.id) filter (where t.status = 'closed' and t.profit > 0) as winning_trades,
       count(t.id) filter (where t.status = 'closed' and t.profit < 0) as losing_trades,
       coalesce(sum(t.profit) filter (where t.status = 'closed'), 0)   as total_profit,
       coalesce(sum(t.quantity) filter (where t.status = 'filled'), 0) as total_volume
from public.trading_accounts a
left join public.trade_orders t on t.account_id = a.id
group by a.id, a.owner_user_id, a.status, a.is_active, a.lock_state, a.lock_reason;

create or replace view public.admin_daily_profit with (security_invoker = true) as
select date_trunc('day', t.closed_at at time zone 'utc')::date as trading_date,
       account_id,
       count(*) as trades,
       count(*) filter (where t.profit > 0) as winning_trades,
       count(*) filter (where t.profit < 0) as losing_trades,
       coalesce(sum(t.profit), 0) as profit,
       coalesce(sum(t.quantity), 0) as volume
from public.trade_orders t
where t.status = 'closed' and t.closed_at is not null
group by 1, 2;

create or replace view public.admin_symbol_stats with (security_invoker = true) as
select symbol,
       count(*) as total_trades,
       count(*) filter (where t.status = 'closed' and t.profit > 0) as winning_trades,
       coalesce(sum(t.profit) filter (where t.status = 'closed'), 0) as total_profit,
       coalesce(sum(t.quantity), 0) as total_volume
from public.trade_orders t
group by symbol;

commit;