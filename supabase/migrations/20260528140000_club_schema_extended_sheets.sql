-- Hojas Excel adicionales: histórico bajas/renov., documentación, log remesas, tablas temporada

-- ============ 9. PLAYERS_HISTORICAL ============
create table if not exists public.players_historical (
  id uuid primary key default gen_random_uuid(),
  player_id uuid references public.players(id) on delete set null,
  nombres text not null,
  apellidos text not null,
  genero text,
  dni text,
  fecha_nacimiento date,
  año_nacimiento int,
  categoria_abrv text,
  categoria text,
  estado_proceso text check (estado_proceso in ('NUEVA ALTA', 'RENOVACION', 'BAJA', 'PENDIENTE')),
  nombre_tutor_1 text,
  dni_tutor_1 text,
  nombre_tutor_2 text,
  dni_tutor_2 text,
  telefono_1 text,
  telefono_2 text,
  email text,
  direccion_calle text,
  direccion_municipio text,
  empadronado boolean,
  federado boolean,
  entidad_banco text,
  titular text,
  iban text,
  hermanos int default 0,
  notas_proceso text,
  temporada text default '25-26',
  created_at timestamptz default now()
);

create index if not exists idx_histor_player on public.players_historical (player_id);
create index if not exists idx_histor_estado on public.players_historical (estado_proceso);
create index if not exists idx_histor_apellidos on public.players_historical (apellidos, nombres);
create index if not exists idx_histor_temporada on public.players_historical (temporada);

-- ============ 10. DOCUMENTATION_STATUS ============
create table if not exists public.documentation_status (
  id uuid primary key default gen_random_uuid(),
  player_id uuid references public.players(id) on delete cascade,
  nombre_completo text not null,
  equipo text,
  año_nacimiento int,
  empadronado text,
  federado text,
  pct_hijos numeric(5, 2),
  temporada text default '25-26',
  resuelto boolean default false,
  notas text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists idx_doc_player on public.documentation_status (player_id);
create index if not exists idx_doc_pendiente on public.documentation_status (resuelto) where resuelto = false;
create index if not exists idx_doc_temporada on public.documentation_status (temporada);

drop trigger if exists trg_documentation_updated on public.documentation_status;
create trigger trg_documentation_updated
  before update on public.documentation_status
  for each row
  execute function public.touch_updated_at();

-- ============ 11. PAYMENT_PROCESSING_LOG ============
create table if not exists public.payment_processing_log (
  id uuid primary key default gen_random_uuid(),
  player_id uuid references public.players(id) on delete set null,
  nombre_completo text not null,
  tipo text not null check (tipo in ('MATRICULA', 'FIANZA')),
  alias text,
  importe numeric(10, 2),
  estado_pago text,
  iban text,
  concepto text,
  temporada_concepto text,
  comentarios text,
  procesado boolean default true,
  created_at timestamptz default now()
);

create index if not exists idx_log_player on public.payment_processing_log (player_id);
create index if not exists idx_log_tipo on public.payment_processing_log (tipo);

-- ============ 12. SEASON_LOOKUP ============
create table if not exists public.season_lookup (
  año_nacimiento int primary key,
  categoria text not null,
  abrv text not null
);

-- Coach: visibilidad vía jugador vinculado
create or replace function public.coach_can_see_player(p_player_id uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select p_player_id is not null
    and exists (
      select 1
      from public.players p
      where p.id = p_player_id
        and public.player_visible_to_coach(p.equipo)
    )
$$;

-- ============ RLS ============
alter table public.players_historical enable row level security;
alter table public.documentation_status enable row level security;
alter table public.payment_processing_log enable row level security;
alter table public.season_lookup enable row level security;

-- --- PLAYERS_HISTORICAL ---
drop policy if exists "staff lee players_historical" on public.players_historical;
create policy "staff lee players_historical"
  on public.players_historical for select
  using (
    public.current_rol() in ('admin', 'coordinator')
    or (
      public.current_rol() = 'coach'
      and public.coach_can_see_player(player_id)
    )
  );

drop policy if exists "staff senior escribe players_historical" on public.players_historical;
create policy "staff senior escribe players_historical"
  on public.players_historical for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza players_historical" on public.players_historical;
create policy "staff senior actualiza players_historical"
  on public.players_historical for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra players_historical" on public.players_historical;
create policy "admin borra players_historical"
  on public.players_historical for delete
  using (public.current_rol() = 'admin');

-- --- DOCUMENTATION_STATUS ---
drop policy if exists "staff lee documentation_status" on public.documentation_status;
create policy "staff lee documentation_status"
  on public.documentation_status for select
  using (
    public.current_rol() in ('admin', 'coordinator')
    or (
      public.current_rol() = 'coach'
      and public.coach_can_see_player(player_id)
    )
  );

drop policy if exists "staff senior escribe documentation_status" on public.documentation_status;
create policy "staff senior escribe documentation_status"
  on public.documentation_status for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza documentation_status" on public.documentation_status;
create policy "staff senior actualiza documentation_status"
  on public.documentation_status for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra documentation_status" on public.documentation_status;
create policy "admin borra documentation_status"
  on public.documentation_status for delete
  using (public.current_rol() = 'admin');

-- --- PAYMENT_PROCESSING_LOG ---
drop policy if exists "staff lee payment_processing_log" on public.payment_processing_log;
create policy "staff lee payment_processing_log"
  on public.payment_processing_log for select
  using (
    public.current_rol() in ('admin', 'coordinator')
    or (
      public.current_rol() = 'coach'
      and public.coach_can_see_player(player_id)
    )
  );

drop policy if exists "staff senior escribe payment_processing_log" on public.payment_processing_log;
create policy "staff senior escribe payment_processing_log"
  on public.payment_processing_log for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza payment_processing_log" on public.payment_processing_log;
create policy "staff senior actualiza payment_processing_log"
  on public.payment_processing_log for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra payment_processing_log" on public.payment_processing_log;
create policy "admin borra payment_processing_log"
  on public.payment_processing_log for delete
  using (public.current_rol() = 'admin');

-- --- SEASON_LOOKUP (lectura todo el staff) ---
drop policy if exists "staff lee season_lookup" on public.season_lookup;
create policy "staff lee season_lookup"
  on public.season_lookup for select
  using (public.current_rol() in ('admin', 'coordinator', 'coach'));

drop policy if exists "staff senior escribe season_lookup" on public.season_lookup;
create policy "staff senior escribe season_lookup"
  on public.season_lookup for insert
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "staff senior actualiza season_lookup" on public.season_lookup;
create policy "staff senior actualiza season_lookup"
  on public.season_lookup for update
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

drop policy if exists "admin borra season_lookup" on public.season_lookup;
create policy "admin borra season_lookup"
  on public.season_lookup for delete
  using (public.current_rol() = 'admin');

grant select, insert, update, delete on public.players_historical to authenticated, service_role;
grant select, insert, update, delete on public.documentation_status to authenticated, service_role;
grant select, insert, update, delete on public.payment_processing_log to authenticated, service_role;
grant select, insert, update, delete on public.season_lookup to authenticated, service_role;
