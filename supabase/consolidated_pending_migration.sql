-- ===========================================================================
-- CONSOLIDATED PENDING MIGRATIONS - run ONCE in Supabase SQL Editor
-- Combines: direct_write_migration.sql + phase2_migration.sql (sections 1-2)
-- + LOCK_HARD trigger FIXED for PostgREST (no current_setting dependency).
-- Safe to rerun (IF NOT EXISTS / OR REPLACE).
-- ===========================================================================

-- ---- A) Direct-write unique ticket index (Phase 1 item 1.3) ----
create unique index if not exists uq_orders_account_ticket  on public.trade_orders (account_id, broker_ticket)  where broker_ticket is not null;

-- ---- B) Composite / partial indexes (Phase 2 item 2.1) ----
-- NOTE: idx_orders_account_time (account_id, submitted_at DESC) already exists
-- in schema.sql and matches recent_trades ordering (closed_at/submitted_at), so
-- we keep it as-is. The covering index below targets closed-trade aggregation.
create index if not exists idx_orders_open_account
  on public.trade_orders (account_id, submitted_at DESC)
  WHERE status IN ('submitted', 'pending', 'filled', 'partially_filled')
    AND is_active = true;
create index if not exists idx_orders_closed_account
  on public.trade_orders (account_id, closed_at DESC)
  INCLUDE (profit)
  WHERE status = 'closed' AND profit IS NOT NULL;
create index if not exists idx_sessions_expiry_active
  on public.user_sessions (expires_at)
  WHERE is_active = true AND status = 'active';

-- ---- C) Admin Account Summary View (Phase 2 item 2.2) ----
-- REPLACES the older admin_account_summary (from admin_perf_migration.sql).
-- Postgres cannot rename/reorder columns via CREATE OR REPLACE VIEW (42P16),
-- so we DROP first. Column set is a SUPERSET of the old view (status,
-- lock_reason, roles) AND the Phase 2 view (account_status, bot_type_*,
-- today_profit, win_rate) plus both id/account_id aliases -- the app filters
-- on id and the frontend reads account_id, so both must exist.
drop view if exists public.admin_account_summary;
create view public.admin_account_summary
with (security_invoker = true) as
SELECT
    ta.id AS id,
    ta.id AS account_id,
    ta.owner_user_id,
    COALESCE(up.display_name, ta.name) AS display_name,
    ta.status AS status,
    ta.status AS account_status,
    ta.is_active,
    ta.lock_state,
    ta.lock_reason,
    up.roles,
    ta.bot_type_id,
    bt.name AS bot_type_name,
    bt.risk_level AS bot_type_risk,
    ta.created_at,
    COALESCE(o.total_trades, 0) AS total_trades,
    COALESCE(o.winning_trades, 0) AS winning_trades,
    COALESCE(o.losing_trades, 0) AS losing_trades,
    COALESCE(o.win_rate, 0) AS win_rate,
    COALESCE(o.total_profit, 0) AS total_profit,
    COALESCE(o.total_volume, 0) AS total_volume,
    COALESCE(o.today_profit, 0) AS today_profit,
    COALESCE(o.open_count, 0) AS open_count
FROM public.trading_accounts ta
LEFT JOIN public.user_profiles up ON up.user_id = ta.owner_user_id
LEFT JOIN public.bot_types bt ON bt.id = ta.bot_type_id
LEFT JOIN LATERAL (
    SELECT
        COUNT(*) AS total_trades,
        COUNT(*) FILTER (WHERE t.profit > 0) AS winning_trades,
        COUNT(*) FILTER (WHERE t.profit < 0) AS losing_trades,
        CASE WHEN COUNT(*) > 0
             THEN ROUND(COUNT(*) FILTER (WHERE t.profit > 0)::numeric / COUNT(*) * 100, 2)
             ELSE 0
        END AS win_rate,
        COALESCE(SUM(t.profit), 0) AS total_profit,
        COALESCE(SUM(t.quantity), 0) AS total_volume,
        COALESCE(SUM(t.profit) FILTER (WHERE t.closed_at >= date_trunc('day', now(), 'UTC')), 0) AS today_profit,
        COUNT(*) FILTER (WHERE t.status IN ('submitted', 'pending', 'filled', 'partially_filled')) AS open_count
    FROM public.trade_orders t
    WHERE t.account_id = ta.id
      AND t.is_active = true
      AND t.status = 'closed'
      AND t.profit IS NOT NULL
) o ON true;

-- D) LOCK_HARD Protection Trigger (item 2.5) -- FIXED for PostgREST
--    ORIGINAL BUG: relied on current_setting(user-defined session variable)
--    that PostgREST CANNOT set; that would 500 every admin unlock via REST.
--    FIX: gate on explicit unlocked_by column written by admin unlock endpoint.
alter table public.trading_accounts
  add column if not exists unlocked_by text;

create or replace function public.enforce_hard_lock()
returns trigger as $$
begin
    if old.lock_state = 'hard_locked' and new.lock_state is distinct from 'hard_locked' then
        if new.unlocked_by is null or new.unlocked_by = '' then
            raise exception 'LOCK_HARD cannot be released without explicit authorization (unlocked_by). Use the admin unlock endpoint.'
                using errcode = 'P0001';
        end if;
    end if;
    return new;
end;
$$ language plpgsql;

drop trigger if exists trg_protect_hard_lock on public.trading_accounts;
create trigger trg_protect_hard_lock
    before update on public.trading_accounts
    for each row
    execute function public.enforce_hard_lock();

-- ===== END =====
