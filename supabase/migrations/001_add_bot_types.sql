-- Migration: Add bot_types table for managing different bot configurations
-- Run this in Supabase Dashboard > SQL Editor after the main schema

-- Bot types table: Define reusable bot configurations
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

-- Add bot_type_id reference to trading_accounts
alter table public.trading_accounts 
add column if not exists bot_type_id uuid references public.bot_types(id) on delete set null;

-- Indexes for bot_types
create index if not exists idx_bot_types_active on public.bot_types(is_active);
create index if not exists idx_bot_types_risk on public.bot_types(risk_level) where is_active = true;
create index if not exists idx_accounts_bot_type on public.trading_accounts(bot_type_id) where bot_type_id is not null;

-- Trigger for updated_at
drop trigger if exists trg_bot_types_updated_at on public.bot_types;
create trigger trg_bot_types_updated_at before update on public.bot_types
for each row execute function public.set_updated_at();

-- Enable RLS
alter table public.bot_types enable row level security;

-- Read policy for authenticated users (traders can see available bot types)
drop policy if exists "Authenticated users can read active bot types" on public.bot_types;
create policy "Authenticated users can read active bot types" on public.bot_types
for select using (is_active = true and auth.role() = 'authenticated');

-- Admin policies will be enforced by service-role key bypass

-- Insert some default bot types
insert into public.bot_types (name, description, risk_level, risk_percent, max_spread, max_daily_loss_percent) values
  ('Conservative Gold', 'Low risk gold trading with tight controls', 'low', 1.0, 150, 3.0),
  ('Standard Gold', 'Balanced gold trading with moderate risk', 'medium', 1.5, 200, 5.0),
  ('Aggressive Gold', 'High risk gold trading for experienced traders', 'high', 3.0, 300, 10.0),
  ('Oil Scalper', 'Fast oil trading with quick exits', 'medium', 2.0, 100, 5.0),
  ('Forex Swing', 'Medium-term forex position trading', 'low', 1.2, 50, 4.0)
on conflict do nothing;

comment on table public.bot_types is 'Reusable bot configuration templates for different trading strategies';
comment on column public.bot_types.risk_level is 'Risk category: low, medium, or high';
comment on column public.bot_types.risk_percent is 'Percentage of account equity to risk per trade';
comment on column public.bot_types.max_spread is 'Maximum allowed spread in points before rejecting trade';
comment on column public.bot_types.max_daily_loss_percent is 'Daily loss limit as percentage of equity';
