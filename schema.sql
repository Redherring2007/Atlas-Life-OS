create extension if not exists pgcrypto;

create table if not exists public.tasks (
    id uuid primary key default gen_random_uuid(),
    telegram_user_id text not null,
    telegram_chat_id text not null,
    source_type text not null check (source_type in ('text', 'voice')),
    raw_input text,
    transcribed_text text,
    title text not null,
    due_at timestamptz null,
    category text not null,
    priority text not null,
    status text default 'pending',
    reminder_sent boolean default false,
    reminder_offset_minutes integer not null default 0,
    created_at timestamptz default now(),
    updated_at timestamptz default now(),
    completed_at timestamptz null
);

create table if not exists public.user_settings (
    telegram_user_id text primary key,
    timezone text not null,
    updated_at timestamptz default now()
);

create or replace function public.set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

alter table public.tasks add column if not exists reminder_offset_minutes integer not null default 0;

drop trigger if exists tasks_set_updated_at on public.tasks;
create trigger tasks_set_updated_at
before update on public.tasks
for each row
execute function public.set_updated_at();

drop trigger if exists user_settings_set_updated_at on public.user_settings;
create trigger user_settings_set_updated_at
before update on public.user_settings
for each row
execute function public.set_updated_at();

create index if not exists tasks_telegram_user_status_idx on public.tasks (telegram_user_id, status);
create index if not exists tasks_due_at_idx on public.tasks (due_at);
create index if not exists tasks_reminder_sent_status_idx on public.tasks (reminder_sent, status);
create index if not exists tasks_created_at_idx on public.tasks (created_at);
