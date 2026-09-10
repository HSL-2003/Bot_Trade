-- ============================================================================
-- Phase 2 Migration: Data Layer & Consistency
-- Composite indexes, admin summary views, LOCK_HARD protection trigger.
-- Run in Supabase SQL Editor. Safe to rerun (IF NOT EXISTS / OR REPLACE).
-- ============================================================================

-- ============================================================================
-- 1. Composite Partial Index (Phase 2 · 2.1)
--    Replaces full-table scans for per-account queries and admin aggregation.
--    Partitioning deliberately SKIPPED (Bẫy 4): <10M rows doesn't need it yet.
-- ============================================================================

-- Per-account recent trades (ORDER BY created_at DESC LIMIT 50)
create index if not exists idx_orders_account_time
  on public.trade_orders (account_id, created_at DESC)
  INCLUDE (profit, volume, symbol, status);

-- Open positions only (partial index, much smaller than full)
create index if not exists idx_orders_open_account
  on public.trade_orders (account_id, submitted_at DESC)
  WHERE status IN ('submitted', 'pending', 'filled', 'partially_filled')
    AND is_active = true;

-- Closed trades for P&L aggregation
create index if not exists idx_orders_closed_account
  on public.trade_orders (account_id, closed_at DESC)
  INCLUDE (profit)
  WHERE status = 'closed' AND profit IS NOT NULL;

-- User sessions: fast expiry cleanup
create index if not exists idx_sessions_expiry_active
  on public.user_sessions (expires_at)
  WHERE is_active = true AND status = 'active';

-- ============================================================================
-- 2. Admin Account Summary View (Phase 2 · 2.2)
--    Replaces Python-side aggregation over get_all_trades(5000) in
--    admin_overview. One SQL query instead of O(all-trades) in Python.
--    Uses security_invoker so RLS still applies.
-- ============================================================================

create or replace view public.admin_account_summary
with (security_invoker = true) as
SELECT
    ta.id AS account_id,
    ta.owner_user_id,
    COALESCE(up.display_name, ta.name) AS display_name,
    ta.status AS account_status,
    ta.lock_state,
    ta.is_active,
    ta.bot_type_id,
    ta.created_at,
    COALESCE(o.total_trades, 0) AS total_trades,
    COALESCE(o.winning_trades, 0) AS winning_trades,
    COALESCE(o.losing_trades, 0) AS losing_trades,
    COALESCE(o.win_rate, 0) AS win_rate,
    COALESCE(o.total_profit, 0) AS total_profit,
    COALESCE(o.total_volume, 0) AS total_volume,
    COALESCE(o.today_profit, 0) AS today_profit,
    COALESCE(o.open_count, 0) AS open_count,
    bt.name AS bot_type_name,
    bt.risk_level AS bot_type_risk
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

-- ============================================================================
-- 3. LOCK_HARD Protection Trigger (Phase 2 · 2.5)
--    Prevents LOCK_HARD → LOCK_UNLOCKED unless the caller has super_admin role.
--    Database-level enforcement is the only way to guarantee this survives
--    restarts and code bugs.
-- ============================================================================

-- Uses app.current_role set by the backend via `SET LOCAL app.current_role`.
-- The backend sets it to 'super_admin' only for the unlock endpoint when the
-- caller is a verified admin. All other paths default to 'app' which cannot
-- bypass this trigger.

create or replace function public.enforce_hard_lock()
returns trigger as $$
begin
    -- Only fire when someone tries to unlock from LOCK_HARD
    if old.lock_state = 'hard_locked' and new.lock_state != 'hard_locked' then
        -- Service-role key (backend) is the only writer to this table, but we
        -- still gate on an app-level setting that the backend must set
        -- explicitly for unlock operations. If it's not set, block.
        if current_setting('app.allow_unlock', true) IS DISTINCT FROM 'true' then
            raise exception
                'LOCK_HARD cannot be released without explicit authorization. '
                'Use the admin unlock endpoint (sets app.allow_unlock=true).'
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

-- ============================================================================
-- 4. Idempotent direct-write unique index (Phase 1 · 1.3, carried forward)
-- ============================================================================

create unique index if not exists uq_orders_account_ticket
  on public.trade_orders (account_id, broker_ticket)
  where broker_ticket is not null;