-- Confluence Algo Bot database schema for Supabase/PostgreSQL
-- Run this file in Supabase Dashboard > SQL Editor.
-- The application must use the service-role key only on the backend.

create extension if not exists pgcrypto;

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

-- One profile per Supabase Auth user.
create table if not exists public.user_profiles (
  user_id uuid primary key references auth.users(id) on delete restrict,
  display_name text,
  status text not null default 'active' check (status in ('active', 'suspended', 'deleted')),
  is_active boolean not null default true,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  deleted_at timestamptz
);

-- Bot types: Reusable bot configuration templates
create table if not exists public.bot_types (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  description text,
  risk_level text not null default 'medium' check (risk_level in ('low', 'medium', 'high')),
  
  -- Trading parameters
  default_symbol text not null default 'XAUUSD',
  risk_percent numeric(5, 2) not null default 1.5 check (risk_percent > 0 and risk_percent <= 100),
  max_spread integer not null default 200 check (max_spread > 0),
  max_daily_loss_percent numeric(5, 2) not null default 5.0 check (max_daily_loss_percent > 0 and max_daily_loss_percent <= 100),
  auto_trading boolean not null default true,
  
  -- Safety controls
  trailing_stop_enabled boolean not null default true,
  trailing_stop_distance integer not null default 100 check (trailing_stop_distance >= 0),
  breakeven_enabled boolean not null default true,
  breakeven_trigger integer not null default 200 check (breakeven_trigger >= 0),
  cooldown_minutes integer not null default 15 check (cooldown_minutes >= 0),
  max_open_trades integer not null default 5 check (max_open_trades > 0),
  
  -- Metadata
  is_active boolean not null default true,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- A user may own multiple trading accounts.
create table if not exists public.trading_accounts (
  id text primary key,
  owner_user_id uuid references auth.users(id) on delete restrict,
  bot_type_id uuid references public.bot_types(id) on delete set null,
  name text,
  broker text,
  account_number text,
  settings jsonb not null default '{}'::jsonb,
  status text not null default 'active' check (status in ('active', 'suspended', 'archived')),
  is_active boolean not null default true,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  deleted_at timestamptz
);

-- Persistent application sessions. Store only a hash of the bearer token.
create table if not exists public.user_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete restrict,
  account_id text not null references public.trading_accounts(id) on delete restrict,
  token_hash text not null unique,
  roles jsonb not null default '["trader"]'::jsonb,
  expires_at timestamptz not null,
  revoked_at timestamptz,
  status text not null default 'active' check (status in ('active', 'revoked', 'expired')),
  is_active boolean not null default true,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- Immutable audit/history table for submitted, filled, cancelled and rejected orders.
-- Do not hard-delete rows: change status and/or is_active instead.
create table if not exists public.trade_orders (
  id uuid primary key default gen_random_uuid(),
  account_id text not null references public.trading_accounts(id) on delete restrict,
  user_id uuid references auth.users(id) on delete restrict,
  broker_ticket bigint,
  client_order_id text,
  symbol text not null,
  side text not null check (side in ('BUY', 'SELL')),
  order_type text not null check (order_type in ('MARKET', 'BUY_LIMIT', 'SELL_LIMIT', 'BUY_STOP', 'SELL_STOP')),
  quantity numeric(20, 8) not null check (quantity > 0),
  entry_price numeric(20, 8),
  stop_loss numeric(20, 8),
  take_profit numeric(20, 8),
  close_price numeric(20, 8),
  profit numeric(20, 8),
  status text not null default 'submitted' check (status in ('submitted', 'pending', 'filled', 'partially_filled', 'cancelled', 'rejected', 'closed', 'failed')),
  is_active boolean not null default true,
  metadata jsonb not null default '{}'::jsonb,
  submitted_at timestamptz not null default timezone('utc', now()),
  filled_at timestamptz,
  closed_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- Optional event log for broker callbacks and state transitions.
create table if not exists public.trade_order_events (
  id bigint generated always as identity primary key,
  order_id uuid not null references public.trade_orders(id) on delete restrict,
  event_type text not null,
  status text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_profiles_status on public.user_profiles(status, is_active);
create index if not exists idx_bot_types_active on public.bot_types(is_active);
create index if not exists idx_bot_types_risk on public.bot_types(risk_level) where is_active = true;
create index if not exists idx_accounts_owner on public.trading_accounts(owner_user_id, status, is_active);
create index if not exists idx_accounts_bot_type on public.trading_accounts(bot_type_id) where bot_type_id is not null;
create index if not exists idx_sessions_user on public.user_sessions(user_id, is_active);
create index if not exists idx_sessions_account on public.user_sessions(account_id, is_active);
create index if not exists idx_sessions_expiry on public.user_sessions(expires_at) where is_active = true;
create index if not exists idx_orders_account_time on public.trade_orders(account_id, submitted_at desc);
create index if not exists idx_orders_user_time on public.trade_orders(user_id, submitted_at desc);
create index if not exists idx_orders_status on public.trade_orders(status, is_active);
create index if not exists idx_orders_ticket on public.trade_orders(broker_ticket) where broker_ticket is not null;
create index if not exists idx_order_events_order_time on public.trade_order_events(order_id, created_at desc);

drop trigger if exists trg_profiles_updated_at on public.user_profiles;
create trigger trg_profiles_updated_at before update on public.user_profiles
for each row execute function public.set_updated_at();

drop trigger if exists trg_bot_types_updated_at on public.bot_types;
create trigger trg_bot_types_updated_at before update on public.bot_types
for each row execute function public.set_updated_at();

drop trigger if exists trg_accounts_updated_at on public.trading_accounts;
create trigger trg_accounts_updated_at before update on public.trading_accounts
for each row execute function public.set_updated_at();

drop trigger if exists trg_sessions_updated_at on public.user_sessions;
create trigger trg_sessions_updated_at before update on public.user_sessions
for each row execute function public.set_updated_at();

drop trigger if exists trg_orders_updated_at on public.trade_orders;
create trigger trg_orders_updated_at before update on public.trade_orders
for each row execute function public.set_updated_at();

-- Enable RLS. Backend requests using the service-role key bypass these policies.
alter table public.user_profiles enable row level security;
alter table public.bot_types enable row level security;
alter table public.trading_accounts enable row level security;
alter table public.user_sessions enable row level security;
alter table public.trade_orders enable row level security;
alter table public.trade_order_events enable row level security;

-- Basic user-facing read policies. Writes should go through the backend.
drop policy if exists "Users can read their profile" on public.user_profiles;
create policy "Users can read their profile" on public.user_profiles
for select using (auth.uid() = user_id);

drop policy if exists "Authenticated users can read active bot types" on public.bot_types;
create policy "Authenticated users can read active bot types" on public.bot_types
for select using (is_active = true and auth.role() = 'authenticated');

drop policy if exists "Users can read their accounts" on public.trading_accounts;
create policy "Users can read their accounts" on public.trading_accounts
for select using (auth.uid() = owner_user_id);

drop policy if exists "Users can read their order history" on public.trade_orders;
create policy "Users can read their order history" on public.trade_orders
for select using (auth.uid() = user_id or auth.uid() = (select owner_user_id from public.trading_accounts a where a.id = account_id));

-- Never expose session tokens to the browser through a table policy.

-- Insert default bot types
insert into public.bot_types (name, description, risk_level, risk_percent, max_spread, max_daily_loss_percent, trailing_stop_distance, breakeven_trigger, cooldown_minutes, max_open_trades) values
  ('Conservative Gold', 'Low risk gold trading with tight controls', 'low', 1.0, 150, 3.0, 80, 150, 20, 3),
  ('Standard Gold', 'Balanced gold trading with moderate risk', 'medium', 1.5, 200, 5.0, 100, 200, 15, 5),
  ('Aggressive Gold', 'High risk gold trading for experienced traders', 'high', 3.0, 300, 10.0, 150, 300, 10, 10),
  ('Oil Scalper', 'Fast oil trading with quick exits', 'medium', 2.0, 100, 5.0, 50, 100, 5, 5),
  ('Forex Swing', 'Medium-term forex position trading', 'low', 1.2, 50, 4.0, 60, 120, 30, 3)
on conflict do nothing;