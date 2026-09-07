-- Bot Types Migration
-- Tạo bảng bot_types và thêm foreign key vào trading_accounts

-- 1. Tạo bảng bot_types
create table if not exists public.bot_types (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  slug text not null unique,
  description text,
  monthly_profit_min numeric(5,2) not null default 0,
  monthly_profit_max numeric(5,2) not null default 0,
  min_capital numeric(12,2) not null default 0,
  max_capital numeric(12,2),
  risk_level text not null default 'medium' check (risk_level in ('low', 'medium', 'high')),
  trade_frequency text not null default 'medium' check (trade_frequency in ('low', 'medium', 'high')),
  is_active boolean not null default true,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now())
);

-- 2. Thêm foreign key vào trading_accounts
alter table public.trading_accounts
  add column if not exists bot_type_id uuid references public.bot_types(id) on delete set null;

-- 3. Index
create index if not exists idx_bot_types_slug on public.bot_types(slug);
create index if not exists idx_bot_types_active on public.bot_types(is_active);
create index if not exists idx_accounts_bot_type on public.trading_accounts(bot_type_id);

-- 4. Trigger updated_at
drop trigger if exists trg_bot_types_updated_at on public.bot_types;
create trigger trg_bot_types_updated_at before update on public.bot_types
for each row execute function public.set_updated_at();

-- 5. RLS
alter table public.bot_types enable row level security;

-- Public read access for active bot types
drop policy if exists "Anyone can read active bot types" on public.bot_types;
create policy "Anyone can read active bot types" on public.bot_types
for select using (is_active = true);

-- 6. Seed data
insert into public.bot_types (name, slug, description, monthly_profit_min, monthly_profit_max, min_capital, max_capital, risk_level, trade_frequency)
values
  ('Bot an toàn', 'safe', 'Lợi nhuận ổn định 1-3%/tháng, rủi ro thấp, phù hợp cho người mới và người muốn an toàn', 1.00, 3.00, 1000.00, null, 'low', 'low'),
  ('Bot trung bình', 'medium', 'Lợi nhuận 3-5%/tháng, rủi ro vừa phải, cân bằng giữa lợi nhuận và an toàn', 3.00, 5.00, 500.00, null, 'medium', 'medium'),
  ('Bot mạo hiểm', 'risky', 'Có thể lợi nhuận cực lớn hoặc mất toàn bộ, dành cho người chấp nhận rủi ro cao', 0.00, 100.00, 100.00, 150.00, 'high', 'high')
on conflict (slug) do nothing;
