-- Emails automáticos de la suscripción (bienvenida, fin de prueba, pago fallido)
-- y plan que el club eligió en la web al registrarse.
alter table public.club_subscriptions
  add column if not exists emails_enviados jsonb not null default '{}'::jsonb,
  add column if not exists plan_interes text;

alter table public.club_subscriptions drop constraint if exists club_subscriptions_plan_interes_chk;
alter table public.club_subscriptions
  add constraint club_subscriptions_plan_interes_chk
  check (plan_interes is null or plan_interes in ('basico', 'club', 'premium'));
