-- VeriFactu (Dinvbox): token propio de cada club, si no usa el de la plataforma.
-- Solo lo lee el backend (service role); nunca se devuelve al navegador.
create table if not exists public.verifactu_cuentas (
  club_id    uuid primary key references public.clubs(id) on delete cascade,
  host       text,
  token      text not null,
  updated_at timestamptz not null default now()
);
alter table public.verifactu_cuentas enable row level security;
revoke all on public.verifactu_cuentas from anon, authenticated;
