-- Esquema relacional club — sección masculina temporada 25-26
-- 8 tablas + RLS (admin / coordinator / coach) + vistas de consulta

-- ============ 1. PROFILES ============
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text unique not null,
  nombre text not null,
  rol text not null check (rol in ('admin', 'coordinator', 'coach')),
  equipos_asignados text[] default '{}',
  created_at timestamptz default now()
);

-- ============ 2. PLAYERS ============
create table if not exists public.players (
  id uuid primary key default gen_random_uuid(),
  nombres text not null,
  apellidos text not null,
  genero text check (genero in ('MASCULINO', 'FEMENINO')),
  dni text,
  fecha_nacimiento date,
  año_nacimiento int generated always as (extract(year from fecha_nacimiento)::int) stored,
  categoria_abrv text check (categoria_abrv in ('JUVE', 'CADE', 'INFA', 'ALEV', 'BENJ', 'PREB', 'DEBU')),
  categoria text not null,
  equipo text,
  nr_cupo int,
  situacion text default 'Con Plaza' check (situacion in ('Con Plaza', 'Sin Plaza', 'Lista Espera')),
  estado text check (estado in ('NUEVA ALTA', 'RENOVACION')),
  empadronado boolean default false,
  federado boolean default false,
  direccion_calle text,
  direccion_municipio text,
  hermanos int default 0,
  notas text,
  temporada text not null default '25-26',
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_players_apellidos_nombres on public.players (apellidos, nombres);
create index if not exists idx_players_categoria on public.players (categoria);
create index if not exists idx_players_equipo on public.players (equipo);
create index if not exists idx_players_temporada on public.players (temporada);
create index if not exists idx_players_dni on public.players (dni) where dni is not null;

-- ============ 3. GUARDIANS ============
create table if not exists public.guardians (
  id uuid primary key default gen_random_uuid(),
  player_id uuid not null references public.players(id) on delete cascade,
  orden smallint not null check (orden in (1, 2)),
  nombre text not null,
  dni text,
  telefono text,
  email text,
  es_titular_pago boolean default false,
  unique (player_id, orden)
);

create index if not exists idx_guardians_player on public.guardians (player_id);
create index if not exists idx_guardians_dni on public.guardians (dni);

-- ============ 4. BANK_ACCOUNTS ============
create table if not exists public.bank_accounts (
  id uuid primary key default gen_random_uuid(),
  player_id uuid not null references public.players(id) on delete cascade,
  entidad text,
  titular text,
  iban text,
  paga_en_tienda boolean default false,
  temporada text not null default '25-26',
  unique (player_id, temporada)
);

create index if not exists idx_bank_accounts_player on public.bank_accounts (player_id);
create index if not exists idx_bank_iban on public.bank_accounts (iban);

-- ============ 5. PAYMENTS ============
create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(),
  player_id uuid not null references public.players(id) on delete cascade,
  temporada text not null default '25-26',
  concepto text not null check (concepto in (
    'matricula', 'ropa', 'cuota_1', 'cuota_2', 'cuota_3', 'fianza'
  )),
  importe numeric(10, 2) not null default 0,
  estado text not null check (estado in ('pagado', 'por_pagar', 'rechazado', 'becado')),
  fecha_pago date,
  iban_usado text,
  notas text,
  created_at timestamptz default now(),
  unique (player_id, concepto, temporada)
);

create index if not exists idx_payments_player on public.payments (player_id);
create index if not exists idx_payments_estado on public.payments (estado);
create index if not exists idx_payments_temporada on public.payments (temporada);

-- ============ 6. SCHOLARSHIPS ============
create table if not exists public.scholarships (
  id uuid primary key default gen_random_uuid(),
  player_id uuid not null references public.players(id) on delete cascade,
  temporada text not null,
  porcentaje_beca numeric(5, 2) check (porcentaje_beca between 0 and 100),
  motivo text,
  comentarios text,
  created_at timestamptz default now()
);

create index if not exists idx_scholarships_player on public.scholarships (player_id);

-- ============ 7. ENROLLMENTS_HISTORY ============
create table if not exists public.enrollments_history (
  id uuid primary key default gen_random_uuid(),
  player_id uuid not null references public.players(id) on delete cascade,
  temporada text not null,
  tipo text not null check (tipo in ('alta', 'renovacion', 'baja')),
  fecha date not null default current_date,
  motivo text,
  usuario_registro uuid references public.profiles(id)
);

create index if not exists idx_enroll_player on public.enrollments_history (player_id);
create index if not exists idx_enroll_tipo on public.enrollments_history (temporada, tipo);

-- ============ 8. CATEGORY_QUOTAS ============
create table if not exists public.category_quotas (
  id uuid primary key default gen_random_uuid(),
  temporada text not null,
  año_nacimiento int not null,
  categoria text not null,
  plazas int not null,
  plazas_reservadas int default 0,
  unique (temporada, año_nacimiento)
);

create index if not exists idx_category_quotas_temporada on public.category_quotas (temporada);

-- ============ TRIGGER updated_at ============
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_players_updated on public.players;
create trigger trg_players_updated
  before update on public.players
  for each row
  execute function public.touch_updated_at();

-- ============ HELPERS RLS ============
create or replace function public.current_rol()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select rol from public.profiles where id = auth.uid()
$$;

create or replace function public.current_teams()
returns text[]
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(equipos_asignados, '{}'::text[])
  from public.profiles
  where id = auth.uid()
$$;

create or replace function public.player_visible_to_coach(p_equipo text)
returns boolean
language sql
stable
as $$
  select p_equipo is not null
    and p_equipo = any(public.current_teams())
$$;

-- ============ ROW LEVEL SECURITY ============
alter table public.profiles enable row level security;
alter table public.players enable row level security;
alter table public.guardians enable row level security;
alter table public.bank_accounts enable row level security;
alter table public.payments enable row level security;
alter table public.scholarships enable row level security;
alter table public.enrollments_history enable row level security;
alter table public.category_quotas enable row level security;

-- --- PROFILES ---
drop policy if exists "profiles lee propio o staff" on public.profiles;
create policy "profiles lee propio o staff"
  on public.profiles for select
  using (
    id = auth.uid()
    or public.current_rol() in ('admin', 'coordinator')
  );

drop policy if exists "admin gestiona profiles" on public.profiles;
create policy "admin gestiona profiles"
  on public.profiles for all
  using (public.current_rol() = 'admin')
  with check (public.current_rol() = 'admin');

-- --- PLAYERS ---
drop policy if exists "staff lee players" on public.players;
create policy "staff lee players"
  on public.players for select
  using (
    public.current_rol() in ('admin', 'coordinator')
    or (
      public.current_rol() = 'coach'
      and public.player_visible_to_coach(equipo)
    )
  );

drop policy if exists "admin coord escribe players" on public.players;
create policy "admin coord escribe players"
  on public.players for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin coord actualiza players" on public.players;
create policy "admin coord actualiza players"
  on public.players for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra players" on public.players;
create policy "admin borra players"
  on public.players for delete
  using (public.current_rol() = 'admin');

-- --- GUARDIANS (datos sensibles) ---
drop policy if exists "staff senior lee guardians" on public.guardians;
create policy "staff senior lee guardians"
  on public.guardians for select
  using (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior inserta guardians" on public.guardians;
create policy "staff senior inserta guardians"
  on public.guardians for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza guardians" on public.guardians;
create policy "staff senior actualiza guardians"
  on public.guardians for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra guardians" on public.guardians;
create policy "admin borra guardians"
  on public.guardians for delete
  using (public.current_rol() = 'admin');

-- --- BANK_ACCOUNTS ---
drop policy if exists "staff senior lee bank" on public.bank_accounts;
create policy "staff senior lee bank"
  on public.bank_accounts for select
  using (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior inserta bank" on public.bank_accounts;
create policy "staff senior inserta bank"
  on public.bank_accounts for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza bank" on public.bank_accounts;
create policy "staff senior actualiza bank"
  on public.bank_accounts for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra bank" on public.bank_accounts;
create policy "admin borra bank"
  on public.bank_accounts for delete
  using (public.current_rol() = 'admin');

-- --- PAYMENTS ---
drop policy if exists "staff senior lee payments" on public.payments;
create policy "staff senior lee payments"
  on public.payments for select
  using (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior inserta payments" on public.payments;
create policy "staff senior inserta payments"
  on public.payments for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza payments" on public.payments;
create policy "staff senior actualiza payments"
  on public.payments for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra payments" on public.payments;
create policy "admin borra payments"
  on public.payments for delete
  using (public.current_rol() = 'admin');

-- --- SCHOLARSHIPS ---
drop policy if exists "staff senior lee scholarships" on public.scholarships;
create policy "staff senior lee scholarships"
  on public.scholarships for select
  using (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior escribe scholarships" on public.scholarships;
create policy "staff senior escribe scholarships"
  on public.scholarships for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza scholarships" on public.scholarships;
create policy "staff senior actualiza scholarships"
  on public.scholarships for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra scholarships" on public.scholarships;
create policy "admin borra scholarships"
  on public.scholarships for delete
  using (public.current_rol() = 'admin');

-- --- ENROLLMENTS_HISTORY ---
drop policy if exists "staff lee enrollments" on public.enrollments_history;
create policy "staff lee enrollments"
  on public.enrollments_history for select
  using (
    public.current_rol() in ('admin', 'coordinator')
    or (
      public.current_rol() = 'coach'
      and exists (
        select 1
        from public.players p
        where p.id = player_id
          and public.player_visible_to_coach(p.equipo)
      )
    )
  );

drop policy if exists "staff senior escribe enrollments" on public.enrollments_history;
create policy "staff senior escribe enrollments"
  on public.enrollments_history for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza enrollments" on public.enrollments_history;
create policy "staff senior actualiza enrollments"
  on public.enrollments_history for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra enrollments" on public.enrollments_history;
create policy "admin borra enrollments"
  on public.enrollments_history for delete
  using (public.current_rol() = 'admin');

-- --- CATEGORY_QUOTAS ---
drop policy if exists "staff lee cupos" on public.category_quotas;
create policy "staff lee cupos"
  on public.category_quotas for select
  using (public.current_rol() in ('admin', 'coordinator', 'coach'));

drop policy if exists "staff senior escribe cupos" on public.category_quotas;
create policy "staff senior escribe cupos"
  on public.category_quotas for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza cupos" on public.category_quotas;
create policy "staff senior actualiza cupos"
  on public.category_quotas for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra cupos" on public.category_quotas;
create policy "admin borra cupos"
  on public.category_quotas for delete
  using (public.current_rol() = 'admin');

-- ============ VISTAS ============
create or replace view public.v_players_full
with (security_invoker = true)
as
select
  p.*,
  coalesce(
    jsonb_agg(distinct to_jsonb(g)) filter (where g.id is not null),
    '[]'::jsonb
  ) as tutores,
  b.iban,
  b.entidad,
  coalesce(
    jsonb_agg(distinct to_jsonb(pay)) filter (where pay.id is not null),
    '[]'::jsonb
  ) as pagos
from public.players p
left join public.guardians g on g.player_id = p.id
left join public.bank_accounts b
  on b.player_id = p.id and b.temporada = p.temporada
left join public.payments pay
  on pay.player_id = p.id and pay.temporada = p.temporada
group by p.id, b.iban, b.entidad;

create or replace view public.v_morosos
with (security_invoker = true)
as
select
  p.id,
  p.nombres,
  p.apellidos,
  p.categoria,
  p.equipo,
  sum(case when pay.estado = 'por_pagar' then pay.importe else 0 end) as deuda_total,
  array_agg(pay.concepto) filter (where pay.estado = 'por_pagar') as conceptos_pendientes
from public.players p
join public.payments pay on pay.player_id = p.id
where pay.estado = 'por_pagar'
group by p.id, p.nombres, p.apellidos, p.categoria, p.equipo;

grant usage on schema public to anon, authenticated, service_role;
grant select, insert, update, delete on all tables in schema public to authenticated, service_role;
grant select on public.v_players_full, public.v_morosos to authenticated, service_role;
