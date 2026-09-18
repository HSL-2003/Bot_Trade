-- ===========================================================================
-- PHASE A MIGRATION - Commerce (cart -> checkout -> PayOS)
-- Run ONCE in Supabase SQL Editor. Safe to re-run (IF NOT EXISTS everywhere).
--
-- Read alongside PROJECT_MASTER_PLAN.md sections 5.5 and 6.1.
-- Depends on: schema.sql (auth.users, trading_accounts), bot_types_migration.sql.
--
-- Invariants encoded here (not just in application code):
--   B4  idempotency_key is UNIQUE per (user_id, key), never globally
--   -   one ACTIVE cart per user, enforced by a partial unique index
--   B3  order_items snapshots price at checkout, so later price edits are inert
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1) bots -- the sellable catalogue. tier_id FK (NOT an inline enum) so
--    bot_types.min_capital / trade_frequency stay the single source of truth.
-- ---------------------------------------------------------------------------
create table if not exists public.bots (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    slug        text unique,
    description text,
    tier_id     uuid references public.bot_types(id) on delete set null,
    price       numeric(14, 2) not null check (price >= 0),
    currency    text not null default 'VND',
    -- Blocks buying a bot that is already owned and not used up (case A.5 #6).
    max_owned_per_user int not null default 1 check (max_owned_per_user >= 1),
    sort_order  int not null default 0,
    is_active   boolean not null default true,
    metadata    jsonb not null default '{}'::jsonb,
    created_at  timestamptz not null default timezone('utc', now()),
    updated_at  timestamptz not null default timezone('utc', now())
);

-- ---------------------------------------------------------------------------
-- 2) bot_builds -- which compiled .ex5 gets deployed. Basis for batched
--    rollout with rollback (case C.2 #5).
-- ---------------------------------------------------------------------------
create table if not exists public.bot_builds (
    id           uuid primary key default gen_random_uuid(),
    bot_id       uuid not null references public.bots(id) on delete cascade,
    file_version text not null,
    sha256       text not null,
    storage_path text not null,
    is_current   boolean not null default false,
    notes        text,
    created_at   timestamptz not null default timezone('utc', now())
);
create index if not exists idx_bot_builds_bot
    on public.bot_builds (bot_id, created_at desc);
-- At most one current build per bot.
create unique index if not exists uq_bot_builds_current
    on public.bot_builds (bot_id) where is_current;

-- ---------------------------------------------------------------------------
-- 3) cart + items. NO price stored on cart_items: price is always read live
--    from bots until checkout snapshots it (plan section A.1).
-- ---------------------------------------------------------------------------
create table if not exists public.carts (
    id         uuid primary key default gen_random_uuid(),
    user_id    uuid not null references auth.users(id) on delete cascade,
    status     text not null default 'ACTIVE' check (status in ('ACTIVE', 'LOCKED')),
    created_at timestamptz not null default timezone('utc', now()),
    updated_at timestamptz not null default timezone('utc', now())
);

-- One ACTIVE cart per user. DB-level guarantee behind get_or_create_active_cart
-- so a race can never create two carts.
create unique index if not exists uq_carts_one_active_per_user
    on public.carts (user_id) where status = 'ACTIVE';
create index if not exists idx_carts_user
    on public.carts (user_id, created_at desc);

create table if not exists public.cart_items (
    id       uuid primary key default gen_random_uuid(),
    cart_id  uuid not null references public.carts(id) on delete cascade,
    bot_id   uuid not null references public.bots(id) on delete cascade,
    qty      int not null default 1 check (qty > 0),
    added_at timestamptz not null default timezone('utc', now()),
    unique (cart_id, bot_id)
);

-- ---------------------------------------------------------------------------
-- 4) orders. cart_id points forward (no carts.last_order_id) so unlocking the
--    cart is a direct read instead of a reverse lookup that can drift.
-- ---------------------------------------------------------------------------
create table if not exists public.orders (
    id            uuid primary key default gen_random_uuid(),
    order_code    bigint not null unique,   -- PayOS caps this at ~9 digits
    user_id       uuid not null references auth.users(id) on delete restrict,
    cart_id       uuid references public.carts(id) on delete set null,
    total_amount  numeric(14, 2) not null check (total_amount >= 0),
    received_amount numeric(14, 2),         -- actual amount transferred (reconciliation)
    currency      text not null default 'VND',
    idempotency_key text not null,
    status        text not null default 'PENDING' check (status in
        ('PENDING', 'PAID', 'EXPIRED', 'FAILED',
         'REFUND_PENDING', 'REFUNDED', 'COMPLETED')),
    late_payment  boolean not null default false,
    manual_review_reason text,
    payos_payment_link_id  text,
    payos_payment_link_url text,            -- cached so a retry needs no PayOS call
    refund_provider_txn_id text,
    created_at    timestamptz not null default timezone('utc', now()),
    expires_at    timestamptz,
    paid_at       timestamptz,
    updated_at    timestamptz not null default timezone('utc', now())
);

-- B4: idempotency is scoped to the user. A global unique would let one client
-- probe another client's order by reusing a key.
create unique index if not exists uq_orders_user_idempotency
    on public.orders (user_id, idempotency_key);

create index if not exists idx_orders_user_status on public.orders (user_id, status);
create index if not exists idx_orders_pending_expiry on public.orders (expires_at)
    where status = 'PENDING';
create index if not exists idx_orders_pending_poll on public.orders (created_at)
    where status = 'PENDING';

create table if not exists public.order_items (
    id                  uuid primary key default gen_random_uuid(),
    order_id            uuid not null references public.orders(id) on delete cascade,
    bot_id              uuid not null references public.bots(id) on delete restrict,
    qty                 int not null default 1 check (qty > 0),
    unit_price_snapshot numeric(14, 2) not null,   -- B3: frozen at checkout
    bot_name_snapshot   text not null
);
create index if not exists idx_order_items_order on public.order_items (order_id);

-- ---------------------------------------------------------------------------
-- 5) order_code generator. SEQUENCE instead of MAX() + 1 (race-free, plan P12).
--    Starts at 100000000 and is clamped to 9 digits by wrapping in the app.
-- ---------------------------------------------------------------------------
create sequence if not exists public.order_code_seq start 100000000;

-- ---------------------------------------------------------------------------
-- 5b) user_bot_licenses -- the "Thu vien" (library). Included here, not in
--     Phase B, because the payment webhook must grant a license the moment an
--     order is paid; without this table Phase A cannot complete a purchase.
--     Phase B adds the activation/switch flows on top of these rows.
-- ---------------------------------------------------------------------------
create table if not exists public.user_bot_licenses (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users(id) on delete cascade,
    bot_id       uuid not null references public.bots(id) on delete restrict,
    order_id     uuid references public.orders(id) on delete set null,
    -- OWNED_INACTIVE : bought, never activated (or bought again after a stop)
    -- ACTIVE         : running on a VPS
    -- PERMANENTLY_STOPPED : was running, replaced by another bot -> must re-buy
    -- REFUNDED       : money returned; must NOT count toward max_owned_per_user
    status       text not null default 'OWNED_INACTIVE' check (status in
        ('OWNED_INACTIVE', 'ACTIVE', 'PERMANENTLY_STOPPED', 'REFUNDED')),
    purchased_at timestamptz not null default timezone('utc', now()),
    activated_at timestamptz,
    stopped_at   timestamptz,
    stop_reason  text check (stop_reason in
        ('SWITCHED_TO_NEW_BOT', 'ADMIN_ACTION', 'VPS_TERMINATED_NONPAYMENT'))
);
create index if not exists idx_licenses_user_bot
    on public.user_bot_licenses (user_id, bot_id, status);
create index if not exists idx_licenses_order on public.user_bot_licenses (order_id);
-- At most one ACTIVE license per user: enforces the core business rule
-- "1 customer = 1 ACTIVE bot at a time" at the database level.
create unique index if not exists uq_licenses_one_active_per_user
    on public.user_bot_licenses (user_id) where status = 'ACTIVE';

alter table public.user_bot_licenses enable row level security;
drop policy if exists "Owners read own licenses" on public.user_bot_licenses;
create policy "Owners read own licenses"
    on public.user_bot_licenses for select using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- 6) updated_at triggers (reuse the helper from schema.sql)
-- ---------------------------------------------------------------------------
drop trigger if exists trg_bots_updated_at on public.bots;
create trigger trg_bots_updated_at before update on public.bots
    for each row execute function public.set_updated_at();

drop trigger if exists trg_carts_updated_at on public.carts;
create trigger trg_carts_updated_at before update on public.carts
    for each row execute function public.set_updated_at();

drop trigger if exists trg_orders_updated_at on public.orders;
create trigger trg_orders_updated_at before update on public.orders
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- 7) RLS. Users read their own commerce rows; the backend (service role)
--    writes. Bots are a public catalogue for any signed-in user.
-- ---------------------------------------------------------------------------
alter table public.bots        enable row level security;
alter table public.bot_builds  enable row level security;
alter table public.carts       enable row level security;
alter table public.cart_items  enable row level security;
alter table public.orders      enable row level security;
alter table public.order_items enable row level security;

drop policy if exists "Anyone signed in reads active bots" on public.bots;
create policy "Anyone signed in reads active bots"
    on public.bots for select using (is_active);

drop policy if exists "Owners read own cart" on public.carts;
create policy "Owners read own cart"
    on public.carts for select using (auth.uid() = user_id);

drop policy if exists "Owners read own orders" on public.orders;
create policy "Owners read own orders"
    on public.orders for select using (auth.uid() = user_id);

drop policy if exists "Owners read own order items" on public.order_items;
create policy "Owners read own order items"
    on public.order_items for select using (
        exists (select 1 from public.orders o
                 where o.id = order_items.order_id and o.user_id = auth.uid())
    );

drop policy if exists "Owners read own cart items" on public.cart_items;
create policy "Owners read own cart items"
    on public.cart_items for select using (
        exists (select 1 from public.carts c
                 where c.id = cart_items.cart_id and c.user_id = auth.uid())
    );

-- ---------------------------------------------------------------------------
-- 8) Seed the three sellable bots, linked to the existing bot_types rows.
--    Prices are placeholders - edit in the admin UI or update here.
--    Safe to re-run: ON CONFLICT (slug) DO UPDATE keeps the row in sync.
-- ---------------------------------------------------------------------------
insert into public.bots (name, slug, description, tier_id, price, currency,
                         max_owned_per_user, sort_order, is_active)
values
    (
        'Bot An Toan',
        'bot-an-toan',
        'Loi nhuan 1-3%/thang. Yeu cau von tu $1000. Vao lenh it, uu tien bao toan von.',
        (select id from public.bot_types where slug = 'safe'),
        2000000, 'VND', 1, 10, true
    ),
    (
        'Bot Trung Binh',
        'bot-trung-binh',
        'Loi nhuan 3-5%/thang. Yeu cau von tu $500. Tan suat giao dich trung binh.',
        (select id from public.bot_types where slug = 'medium'),
        3500000, 'VND', 1, 20, true
    ),
    (
        'Bot Mao Hiem',
        'bot-mao-hiem',
        'Co the mat toan bo hoac sinh loi nhuan rat lon. Yeu cau von $100-$150.',
        (select id from public.bot_types where slug = 'risky'),
        1500000, 'VND', 1, 30, true
    )
on conflict (slug) do update set
    name               = excluded.name,
    description        = excluded.description,
    tier_id            = excluded.tier_id,
    max_owned_per_user = excluded.max_owned_per_user,
    sort_order         = excluded.sort_order;

-- ===========================================================================
-- VERIFY (optional, run manually to confirm):
--   select b.slug, b.price, bt.name as tier, bt.min_capital
--     from public.bots b left join public.bot_types bt on bt.id = b.tier_id
--    order by b.sort_order;
-- ===========================================================================