begin;

alter table public.user_profiles
  add column if not exists full_name text,
  add column if not exists phone text,
  add column if not exists avatar_url text,
  add column if not exists timezone text not null default 'Asia/Ho_Chi_Minh',
  add column if not exists preferred_currency text not null default 'USD',
  add column if not exists language text not null default 'vi',
  add column if not exists risk_level text not null default 'medium',
  add column if not exists trading_goal text,
  add column if not exists last_login_at timestamptz;

create table if not exists public.account_equity_snapshots (
  id bigint generated always as identity primary key,
  account_id text not null references public.trading_accounts(id) on delete restrict,
  user_id uuid not null references auth.users(id) on delete restrict,
  balance numeric(20,8) not null default 0,
  equity numeric(20,8) not null default 0,
  floating_profit numeric(20,8) not null default 0,
  used_margin numeric(20,8) not null default 0,
  free_margin numeric(20,8) not null default 0,
  drawdown_percent numeric(12,6) not null default 0,
  recorded_at timestamptz not null default timezone('utc', now())
);
create index if not exists idx_equity_account_time on public.account_equity_snapshots(account_id, recorded_at desc);
alter table public.account_equity_snapshots enable row level security;
drop policy if exists "Users can read their equity snapshots" on public.account_equity_snapshots;
create policy "Users can read their equity snapshots" on public.account_equity_snapshots for select using (auth.uid() = user_id);

create or replace view public.user_profit_daily with (security_invoker = true) as
select user_id, account_id, timezone('utc', coalesce(closed_at, updated_at))::date as trading_date,
 count(*) as total_trades,
 count(*) filter (where profit > 0) as winning_trades,
 count(*) filter (where profit < 0) as losing_trades,
 coalesce(sum(profit), 0)::numeric as daily_profit,
 coalesce(sum(case when profit > 0 then profit else 0 end), 0)::numeric as gross_profit,
 coalesce(sum(case when profit < 0 then profit else 0 end), 0)::numeric as gross_loss
from public.trade_orders where status = 'closed' and profit is not null
group by user_id, account_id, timezone('utc', coalesce(closed_at, updated_at))::date;

commit;