-- Dueños de la plataforma Sportink: ven el «Panel de Sportink» (todos los clubes,
-- suscripciones e ingresos) y pueden regalar planes o ampliar pruebas.
-- Solo la lee el backend (service role): sin políticas RLS para el navegador.
create table if not exists public.platform_admins (
  email      text primary key check (email = lower(email) and position('@' in email) > 1),
  nombre     text,
  created_at timestamptz not null default now()
);

alter table public.platform_admins enable row level security;
revoke all on public.platform_admins from anon, authenticated;

-- Para añadir a alguien (SQL editor de Supabase):
--   insert into public.platform_admins (email, nombre) values ('correo@ejemplo.com', 'Nombre');
