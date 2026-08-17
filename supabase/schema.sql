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

-- A user may own multiple trading accounts.
create table if not exists public.trading_accounts (
  id text primary key,
  owner_user_id uuid references auth.users(id) on delete restrict,
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
create index if not exists idx_accounts_owner on public.trading_accounts(owner_user_id, status, is_active);
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
alter table public.trading_accounts enable row level security;
alter table public.user_sessions enable row level security;
alter table public.trade_orders enable row level security;
alter table public.trade_order_events enable row level security;

-- Basic user-facing read policies. Writes should go through the backend.
drop policy if exists "Users can read their profile" on public.user_profiles;
create policy "Users can read their profile" on public.user_profiles
for select using (auth.uid() = user_id);

drop policy if exists "Users can read their accounts" on public.trading_accounts;
create policy "Users can read their accounts" on public.trading_accounts
for select using (auth.uid() = owner_user_id);

drop policy if exists "Users can read their order history" on public.trade_orders;
create policy "Users can read their order history" on public.trade_orders
for select using (auth.uid() = user_id or auth.uid() = (select owner_user_id from public.trading_accounts a where a.id = account_id));

-- Never expose session tokens to the browser through a table policy.