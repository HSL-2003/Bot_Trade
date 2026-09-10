-- Direct-write (asyncpg) support: unique ticket per account so critical-path
-- upserts are truly idempotent (Phase 1 · item 1.3).
-- Run in Supabase SQL Editor. Safe to rerun (IF NOT EXISTS).
create unique index if not exists uq_orders_account_ticket
  on public.trade_orders (account_id, broker_ticket)
  where broker_ticket is not null;