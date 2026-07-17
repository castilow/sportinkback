-- =====================================================================
-- Registro público + aislamiento por club
-- - public_registrations: altas pendientes hasta verificar correo
-- - profiles.onboarding_completed
-- - current_club_id() + RLS por club
-- - finalize_public_registration(...): alta atómica
-- =====================================================================

-- ============ 1. COLUMNAS DE ESTADO ============
alter table public.profiles
  add column if not exists onboarding_completed boolean not null default false;

-- Los perfiles del club por defecto ya están onboarded
update public.profiles
set onboarding_completed = true
where club_id is not null and onboarding_completed = false;

-- ============ 2. REGISTROS PÚBLICOS PENDIENTES ============
create table if not exists public.public_registrations (
  id uuid primary key default gen_random_uuid(),
  email text not null,
  auth_user_id uuid,
  admin_name text not null,
  payload jsonb not null default '{}'::jsonb,
  status text not null default 'pending'
    check (status in ('pending', 'completed', 'expired', 'failed')),
  club_id uuid references public.clubs(id) on delete set null,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz
);

create unique index if not exists uq_public_registrations_email_pending
  on public.public_registrations (lower(email))
  where status = 'pending';

create index if not exists idx_public_registrations_auth
  on public.public_registrations (auth_user_id);

drop trigger if exists trg_public_registrations_updated on public.public_registrations;
create trigger trg_public_registrations_updated
  before update on public.public_registrations
  for each row
  execute function public.touch_updated_at();

-- ============ 3. HELPERS DE TENENCIA ============
create or replace function public.current_club_id()
returns uuid
language sql
stable
security definer
set search_path = public
as $$
  select club_id from public.profiles where id = auth.uid()
$$;

create or replace function public.current_onboarding_completed()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(onboarding_completed, false)
  from public.profiles
  where id = auth.uid()
$$;

-- ============ 4. RLS POR CLUB ============
-- Clubs: el staff solo ve su club
drop policy if exists "staff lee clubs" on public.clubs;
create policy "staff lee clubs"
  on public.clubs for select
  using (
    id = public.current_club_id()
    or public.current_rol() = 'admin' and id = public.current_club_id()
  );

drop policy if exists "admin escribe clubs" on public.clubs;
create policy "admin escribe clubs"
  on public.clubs for all
  using (
    public.current_rol() = 'admin'
    and (id = public.current_club_id() or public.current_club_id() is null)
  )
  with check (public.current_rol() = 'admin');

-- Teams: solo del club del usuario
drop policy if exists "staff lee teams" on public.teams;
create policy "staff lee teams"
  on public.teams for select
  using (club_id = public.current_club_id());

drop policy if exists "admin coord escribe teams" on public.teams;
create policy "admin coord escribe teams"
  on public.teams for all
  using (
    public.current_rol() in ('admin', 'coordinator')
    and club_id = public.current_club_id()
  )
  with check (
    public.current_rol() in ('admin', 'coordinator')
    and club_id = public.current_club_id()
  );

-- Players: filtrar por club_id (mantener coach por equipo)
drop policy if exists "staff lee players" on public.players;
create policy "staff lee players"
  on public.players for select
  using (
    club_id = public.current_club_id()
    and (
      public.current_rol() in ('admin', 'coordinator', 'office', 'physio')
      or (
        public.current_rol() = 'coach'
        and public.player_visible_to_coach(equipo)
      )
    )
  );

drop policy if exists "admin coord escribe players" on public.players;
create policy "admin coord escribe players"
  on public.players for insert
  with check (
    public.current_rol() in ('admin', 'coordinator')
    and club_id = public.current_club_id()
  );

drop policy if exists "admin coord actualiza players" on public.players;
create policy "admin coord actualiza players"
  on public.players for update
  using (
    public.current_rol() in ('admin', 'coordinator')
    and club_id = public.current_club_id()
  )
  with check (
    public.current_rol() in ('admin', 'coordinator')
    and club_id = public.current_club_id()
  );

drop policy if exists "admin borra players" on public.players;
create policy "admin borra players"
  on public.players for delete
  using (
    public.current_rol() = 'admin'
    and club_id = public.current_club_id()
  );

-- Club sports: lectura del propio club
drop policy if exists "staff lee club_sports" on public.club_sports;
create policy "staff lee club_sports"
  on public.club_sports for select
  using (club_id = public.current_club_id());

-- Sports: catálogo legible por autenticados
drop policy if exists "staff lee sports" on public.sports;
create policy "staff lee sports"
  on public.sports for select
  using (auth.role() = 'authenticated');

-- Public registrations: solo service_role (sin políticas de authenticated)
alter table public.public_registrations enable row level security;

-- ============ 5. FINALIZACIÓN TRANSACCIONAL ============
create or replace function public.finalize_public_registration(
  p_registration_id uuid,
  p_auth_user_id uuid
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  reg public.public_registrations%rowtype;
  v_payload jsonb;
  v_club public.clubs%rowtype;
  v_sport public.sports%rowtype;
  v_sport_slug text;
  v_sport_name text;
  v_club_slug text;
  v_club_name text;
  v_season text;
  v_admin_name text;
  v_team jsonb;
  v_player jsonb;
  v_team_id uuid;
  v_team_map jsonb := '{}'::jsonb;
  v_local_key text;
  v_created_teams int := 0;
  v_created_players int := 0;
  v_nombres text;
  v_apellidos text;
  v_parts text[];
begin
  select * into reg
  from public.public_registrations
  where id = p_registration_id
  for update;

  if not found then
    raise exception 'Registro no encontrado';
  end if;

  if reg.status = 'completed' then
    return jsonb_build_object(
      'status', 'completed',
      'club_id', reg.club_id,
      'idempotent', true
    );
  end if;

  if reg.status <> 'pending' then
    raise exception 'Registro en estado %', reg.status;
  end if;

  if reg.auth_user_id is distinct from p_auth_user_id then
    raise exception 'El usuario autenticado no coincide con el registro';
  end if;

  v_payload := reg.payload;
  v_admin_name := coalesce(reg.admin_name, v_payload #>> '{admin,name}', split_part(reg.email, '@', 1));
  v_club_name := coalesce(v_payload #>> '{club,nombre}', v_payload #>> '{club,name}');
  v_club_slug := coalesce(v_payload #>> '{club,slug}', lower(regexp_replace(v_club_name, '[^a-zA-Z0-9]+', '-', 'g')));
  v_club_slug := trim(both '-' from v_club_slug);
  v_season := coalesce(v_payload #>> '{club,temporada}', v_payload #>> '{club,season}', '25-26');
  v_sport_slug := coalesce(v_payload #>> '{sport,slug}', 'futbol');
  v_sport_name := coalesce(v_payload #>> '{sport,nombre}', v_payload #>> '{sport,name}', 'Fútbol');

  if v_club_name is null or length(trim(v_club_name)) < 2 then
    raise exception 'Nombre de club obligatorio';
  end if;

  -- Deporte (crear si no existe)
  insert into public.sports (slug, nombre)
  values (v_sport_slug, v_sport_name)
  on conflict (slug) do update set nombre = excluded.nombre
  returning * into v_sport;

  if v_sport.id is null then
    select * into v_sport from public.sports where slug = v_sport_slug;
  end if;

  -- Club
  insert into public.clubs (slug, nombre, nombre_corto, ciudad, is_default, activo)
  values (
    v_club_slug,
    v_club_name,
    coalesce(v_payload #>> '{club,nombre_corto}', v_club_name),
    v_payload #>> '{club,ciudad}',
    false,
    true
  )
  returning * into v_club;

  insert into public.club_sports (club_id, sport_id)
  values (v_club.id, v_sport.id)
  on conflict (club_id, sport_id) do nothing;

  -- Perfil admin
  insert into public.profiles (id, email, nombre, rol, equipos_asignados, club_id, onboarding_completed, created_at)
  values (
    p_auth_user_id,
    lower(reg.email),
    v_admin_name,
    'admin',
    '{}',
    v_club.id,
    true,
    now()
  )
  on conflict (id) do update set
    email = excluded.email,
    nombre = excluded.nombre,
    rol = 'admin',
    club_id = excluded.club_id,
    onboarding_completed = true;

  -- Equipos
  for v_team in select * from jsonb_array_elements(coalesce(v_payload->'teams', '[]'::jsonb))
  loop
    v_local_key := coalesce(v_team->>'local_id', v_team->>'nombre', v_team->>'name');
    insert into public.teams (club_id, sport_id, nombre, categoria, genero, temporada, entidad, activo)
    values (
      v_club.id,
      v_sport.id,
      coalesce(v_team->>'nombre', v_team->>'name'),
      coalesce(v_team->>'categoria', v_team->>'category', coalesce(v_team->>'nombre', v_team->>'name')),
      coalesce(v_team->>'genero', v_team->>'gender', 'MIXTO'),
      coalesce(v_team->>'temporada', v_season),
      coalesce(v_team->>'entidad', 'club'),
      true
    )
    returning id into v_team_id;

    v_team_map := v_team_map || jsonb_build_object(v_local_key, v_team_id::text);
    v_created_teams := v_created_teams + 1;
  end loop;

  -- Jugadores
  for v_player in select * from jsonb_array_elements(coalesce(v_payload->'players', '[]'::jsonb))
  loop
    v_local_key := coalesce(v_player->>'team_local_id', v_player->>'equipo', v_player->>'team');
    v_team_id := nullif(v_team_map->>v_local_key, '')::uuid;

    if v_team_id is null and (v_player->>'equipo' is not null or v_player->>'team' is not null) then
      select id into v_team_id
      from public.teams
      where club_id = v_club.id
        and nombre = coalesce(v_player->>'equipo', v_player->>'team')
        and temporada = v_season
      limit 1;
    end if;

    v_parts := regexp_split_to_array(trim(coalesce(v_player->>'name', '')), '\s+');
    if coalesce(v_player->>'nombres', '') <> '' then
      v_nombres := v_player->>'nombres';
      v_apellidos := coalesce(v_player->>'apellidos', '');
    elsif array_length(v_parts, 1) >= 2 then
      v_nombres := v_parts[1];
      v_apellidos := array_to_string(v_parts[2:array_length(v_parts, 1)], ' ');
    else
      v_nombres := coalesce(v_parts[1], 'Jugador');
      v_apellidos := 'Sin apellido';
    end if;

    insert into public.players (
      nombres, apellidos, genero, dni, fecha_nacimiento, categoria, equipo,
      situacion, estado, temporada, club_id, team_id, notas
    ) values (
      v_nombres,
      v_apellidos,
      coalesce(v_player->>'genero', v_player->>'gender', 'MIXTO'),
      nullif(v_player->>'dni', ''),
      nullif(v_player->>'birthdate', '')::date,
      coalesce(v_player->>'categoria', v_player->>'category', 'Sin categoría'),
      coalesce(
        (select nombre from public.teams where id = v_team_id),
        v_player->>'equipo',
        v_player->>'team',
        ''
      ),
      'Con Plaza',
      'NUEVA ALTA',
      v_season,
      v_club.id,
      v_team_id,
      nullif(v_player->>'notes', '')
    );
    v_created_players := v_created_players + 1;
  end loop;

  update public.public_registrations
  set status = 'completed',
      club_id = v_club.id,
      completed_at = now(),
      error_message = null
  where id = reg.id;

  return jsonb_build_object(
    'status', 'completed',
    'club_id', v_club.id,
    'club_slug', v_club.slug,
    'club_name', v_club.nombre,
    'sport_slug', v_sport.slug,
    'teams_created', v_created_teams,
    'players_created', v_created_players,
    'idempotent', false
  );
exception
  when others then
    update public.public_registrations
    set status = 'failed', error_message = SQLERRM
    where id = p_registration_id and status = 'pending';
    raise;
end;
$$;

revoke all on function public.finalize_public_registration(uuid, uuid) from public, anon, authenticated;
grant execute on function public.finalize_public_registration(uuid, uuid) to service_role;

grant select, insert, update, delete on public.public_registrations to service_role;
