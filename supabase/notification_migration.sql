-- ===========================================================================
-- Notification migration - in-app notifications + real-time fan-out
-- Run ONCE in Supabase SQL Editor. Safe to re-run.
--
-- Delivery model (see PROJECT_MASTER_PLAN.md > "Phan 5 - Notifications"):
--   1. Every notification is persisted here, so history survives reconnects.
--   2. Real time reaches clients two ways, both driven by this same row:
--      - Web dashboard: backend pushes over the existing /ws WebSocket.
--      - Mobile (Flutter): Supabase Realtime postgres_changes subscription.
--
-- MANUAL STEP: after running this file, confirm the table appears under
-- Database > Replication > supabase_realtime. The DO block below adds it, but
-- verify once because the publication only exists on hosted Supabase.
-- ===========================================================================

create extension if not exists pgcrypto;

create table if not exists public.notifications (
    id          uuid primary key default gen_random_uuid(),
    user_id     uuid not null references auth.users(id) on delete cascade,
    type        text not null,
    title       text not null,
    message     text not null,
    data        jsonb not null default '{}'::jsonb,
    severity    text not null default 'info'
                check (severity in ('info', 'success', 'warning', 'critical')),
    read_at     timestamptz,
    created_at  timestamptz not null default timezone('utc', now())
);

-- Hot path: "my notifications, newest first".
create index if not exists idx_notifications_user_created
    on public.notifications (user_id, created_at desc);

-- Partial index for the unread badge - tiny and always current.
create index if not exists idx_notifications_user_unread
    on public.notifications (user_id)
    where read_at is null;

alter table public.notifications enable row level security;

-- Users only ever see their own notifications. The service-role key (backend)
-- bypasses RLS and is the only writer.
drop policy if exists "Users read own notifications" on public.notifications;
create policy "Users read own notifications"
    on public.notifications for select
    using (auth.uid() = user_id);

drop policy if exists "Users update own notifications" on public.notifications;
create policy "Users update own notifications"
    on public.notifications for update
    using (auth.uid() = user_id)
    with check (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Real-time: publish row changes on this table to Supabase Realtime so the
-- Flutter app can subscribe directly (no extra server work per client).
-- Guarded because the publication only exists on hosted Supabase.
-- ---------------------------------------------------------------------------
do $$
begin
    if exists (select 1 from pg_publication where pubname = 'supabase_realtime') then
        if not exists (
            select 1 from pg_publication_tables
            where pubname = 'supabase_realtime'
              and schemaname = 'public'
              and tablename = 'notifications'
        ) then
            alter publication supabase_realtime add table public.notifications;
        end if;
    else
        raise notice 'Publication supabase_realtime not found - enable Realtime for public.notifications in the dashboard.';
    end if;
end $$;
