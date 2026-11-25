-- Suscripciones de los clubes (Stripe).
-- Una fila por club. La escribe SOLO el backend (service role): sin políticas RLS
-- para anon/authenticated, nadie puede leerla ni tocarla desde el navegador.

create table if not exists public.club_subscriptions (
  club_id                uuid primary key references public.clubs(id) on delete cascade,
  plan                   text not null default 'premium',
  status                 text not null default 'trialing',
  billing_interval       text,
  trial_ends_at          timestamptz,
  current_period_end     timestamptz,
  cancel_at_period_end   boolean not null default false,
  comped                 boolean not null default false,
  stripe_customer_id     text unique,
  stripe_subscription_id text unique,
  stripe_price_id        text,
  notes                  text,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  constraint club_subscriptions_status_chk check (status in (
    'trialing', 'active', 'past_due', 'unpaid', 'canceled',
    'incomplete', 'incomplete_expired', 'paused'
  )),
  constraint club_subscriptions_interval_chk check (billing_interval is null or billing_interval in ('month', 'year'))
);

create index if not exists idx_club_subscriptions_customer on public.club_subscriptions (stripe_customer_id);

-- Eventos de Stripe ya procesados (los webhooks pueden llegar repetidos).
create table if not exists public.stripe_events (
  id          text primary key,
  type        text not null,
  received_at timestamptz not null default now()
);

alter table public.club_subscriptions enable row level security;
alter table public.stripe_events enable row level security;
revoke all on public.club_subscriptions from anon, authenticated;
revoke all on public.stripe_events from anon, authenticated;

-- El club por defecto (CF Rayo Majadahonda) no paga por la plataforma.
insert into public.club_subscriptions (club_id, plan, status, comped, notes)
select id, 'premium', 'active', true, 'Club fundador: sin cuota'
from public.clubs
where is_default = true
on conflict (club_id) do nothing;
