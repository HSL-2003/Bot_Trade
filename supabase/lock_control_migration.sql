-- lock_control_migration.sql
-- ---------------------------------------------------------------------------
-- Persisted trading lock state (soft / hard) on trading_accounts.
--
-- Semantics / state transitions:
--   unlocked      -> soft_locked  : block NEW entries (soft lock). Open
--                                    positions & risk controls keep working.
--   unlocked      -> hard_locked  : full emergency lockdown; all positions are
--                                    closed immediately after this is set.
--   soft_locked   -> unlocked     : resume opening trades; no position impact.
--   soft_locked   -> hard_locked  : escalate to full emergency lockdown.
--   hard_locked   -> unlocked     : admin/user release; positions must be
--                                    re-opened manually (hard lock closes them).
--   hard_locked   -> soft_locked  : NOT allowed directly (a hard lock is only
--                                    released by going back to 'unlocked').
--
-- The application keeps the in-memory value in sync by reading this column
-- when a bot session is created and writing here on every transition.
-- ---------------------------------------------------------------------------
begin;

alter table public.trading_accounts
  add column if not exists lock_state text not null default 'unlocked'
    check (lock_state in ('unlocked', 'soft_locked', 'hard_locked'));

alter table public.trading_accounts
  add column if not exists lock_reason text;

alter table public.trading_accounts
  add column if not exists locked_at timestamptz;

alter table public.trading_accounts
  add column if not exists unlocked_at timestamptz;

create index if not exists idx_accounts_lock_state on public.trading_accounts(lock_state) where lock_state <> 'unlocked';

commit;