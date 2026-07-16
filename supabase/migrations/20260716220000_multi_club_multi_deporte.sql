-- =====================================================================
-- Estructura multi-club / multi-deporte
-- Jerarquía: sports (deportes) -> clubs (clubes) -> teams (equipos) -> players
-- Los jugadores existentes (Rayo Majadahonda, fútbol) se vinculan automáticamente.
-- El campo legacy players.equipo (texto) se mantiene sincronizado por trigger
-- para no romper el backend actual.
-- =====================================================================

-- ============ 1. SPORTS (deportes) ============
create table if not exists public.sports (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  nombre text not null,
  created_at timestamptz default now()
);

-- ============ 2. CLUBS (clubes) ============
create table if not exists public.clubs (
  id uuid primary key default gen_random_uuid(),
  slug text unique not null,
  nombre text not null,
  nombre_corto text,
  escudo_url text,
  color_primario text,
  color_secundario text,
  ciudad text,
  pais text default 'España',
  is_default boolean not null default false,
  activo boolean not null default true,
  created_at timestamptz default now()
);

-- Solo puede haber un club por defecto (el que absorbe datos legacy sin club)
create unique index if not exists uq_clubs_default on public.clubs (is_default) where is_default;

-- ============ 3. CLUB_SPORTS (deportes que practica cada club) ============
create table if not exists public.club_sports (
  id uuid primary key default gen_random_uuid(),
  club_id uuid not null references public.clubs(id) on delete cascade,
  sport_id uuid not null references public.sports(id) on delete cascade,
  created_at timestamptz default now(),
  unique (club_id, sport_id)
);

-- ============ 4. TEAMS (equipos) ============
create table if not exists public.teams (
  id uuid primary key default gen_random_uuid(),
  club_id uuid not null references public.clubs(id) on delete cascade,
  sport_id uuid not null references public.sports(id) on delete restrict,
  nombre text not null,
  categoria text,
  genero text check (genero in ('MASCULINO', 'FEMENINO', 'MIXTO')),
  temporada text not null default '25-26',
  entidad text default 'club' check (entidad in ('club', 'fundacion')),
  activo boolean not null default true,
  created_at timestamptz default now(),
  updated_at timestamptz default now(),
  unique (club_id, sport_id, nombre, temporada)
);

create index if not exists idx_teams_club on public.teams (club_id);
create index if not exists idx_teams_sport on public.teams (sport_id);
create index if not exists idx_teams_temporada on public.teams (temporada);

drop trigger if exists trg_teams_updated on public.teams;
create trigger trg_teams_updated
  before update on public.teams
  for each row
  execute function public.touch_updated_at();

-- ============ 5. SEEDS: fútbol + Rayo Majadahonda ============
insert into public.sports (slug, nombre)
values ('futbol', 'Fútbol')
on conflict (slug) do nothing;

insert into public.clubs (slug, nombre, nombre_corto, ciudad, is_default)
values ('rayo-majadahonda', 'CF Rayo Majadahonda', 'Rayo Majadahonda', 'Majadahonda', true)
on conflict (slug) do nothing;

insert into public.club_sports (club_id, sport_id)
select c.id, s.id
from public.clubs c, public.sports s
where c.slug = 'rayo-majadahonda' and s.slug = 'futbol'
on conflict (club_id, sport_id) do nothing;

-- ============ 6. PLAYERS: columnas nuevas ============
alter table public.players add column if not exists club_id uuid references public.clubs(id) on delete restrict;
alter table public.players add column if not exists team_id uuid references public.teams(id) on delete set null;

create index if not exists idx_players_club on public.players (club_id);
create index if not exists idx_players_team on public.players (team_id);
create index if not exists idx_players_club_temporada on public.players (club_id, temporada);

-- ============ 7. BACKFILL: crear equipos desde players.equipo ============
-- Un equipo por (nombre, temporada); la categoría se toma de la más frecuente.
insert into public.teams (club_id, sport_id, nombre, categoria, genero, temporada, entidad)
select
  c.id,
  s.id,
  src.equipo,
  src.categoria,
  coalesce(nullif(src.genero, ''), 'MASCULINO'),
  src.temporada,
  case when src.equipo like 'FUND.%' then 'fundacion' else 'club' end
from (
  select distinct on (p.equipo, p.temporada)
    p.equipo,
    p.temporada,
    p.categoria,
    p.genero,
    count(*) over (partition by p.equipo, p.temporada, p.categoria) as n
  from public.players p
  where p.equipo is not null and p.equipo <> ''
  order by p.equipo, p.temporada, n desc
) src
cross join public.clubs c
cross join public.sports s
where c.slug = 'rayo-majadahonda' and s.slug = 'futbol'
on conflict (club_id, sport_id, nombre, temporada) do nothing;

-- Vincular jugadores existentes a su club y equipo
update public.players p
set club_id = c.id
from public.clubs c
where c.slug = 'rayo-majadahonda' and p.club_id is null;

update public.players p
set team_id = t.id
from public.teams t
where p.team_id is null
  and p.equipo is not null
  and t.nombre = p.equipo
  and t.temporada = p.temporada
  and t.club_id = p.club_id;

-- ============ 8. TRIGGER de consistencia players <-> teams ============
-- Mantiene club_id / team_id / equipo coherentes sin romper el backend legacy,
-- que sigue insertando jugadores solo con el nombre del equipo en texto.
create or replace function public.players_resolve_team()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  v_club uuid;
  v_sport uuid;
  v_team public.teams%rowtype;
begin
  -- Club por defecto para datos legacy sin club explícito
  if new.club_id is null then
    select id into v_club from public.clubs where is_default limit 1;
    new.club_id := v_club;
  end if;

  -- Si viene team_id explícito, el texto equipo se deriva del equipo real
  if new.team_id is not null then
    select * into v_team from public.teams where id = new.team_id;
    if v_team.id is not null then
      new.equipo := v_team.nombre;
      new.club_id := v_team.club_id;
    end if;
    return new;
  end if;

  -- Si solo viene el texto equipo, buscar (o crear) el equipo en su club
  if new.equipo is not null and new.equipo <> '' then
    select * into v_team
    from public.teams
    where club_id = new.club_id
      and nombre = new.equipo
      and temporada = new.temporada
    limit 1;

    if v_team.id is null then
      select cs.sport_id into v_sport
      from public.club_sports cs
      where cs.club_id = new.club_id
      order by cs.created_at
      limit 1;

      if v_sport is not null then
        insert into public.teams (club_id, sport_id, nombre, categoria, genero, temporada, entidad)
        values (
          new.club_id,
          v_sport,
          new.equipo,
          new.categoria,
          coalesce(nullif(new.genero, ''), 'MIXTO'),
          new.temporada,
          case when new.equipo like 'FUND.%' then 'fundacion' else 'club' end
        )
        returning * into v_team;
      end if;
    end if;

    new.team_id := v_team.id;
  end if;

  return new;
end;
$$;

drop trigger if exists trg_players_resolve_team on public.players;
create trigger trg_players_resolve_team
  before insert or update of equipo, team_id, club_id on public.players
  for each row
  execute function public.players_resolve_team();

-- ============ 9. PROFILES y CATEGORY_QUOTAS por club ============
alter table public.profiles add column if not exists club_id uuid references public.clubs(id) on delete set null;

update public.profiles pr
set club_id = c.id
from public.clubs c
where c.slug = 'rayo-majadahonda' and pr.club_id is null;

alter table public.category_quotas add column if not exists club_id uuid references public.clubs(id) on delete cascade;

update public.category_quotas q
set club_id = c.id
from public.clubs c
where c.slug = 'rayo-majadahonda' and q.club_id is null;

create index if not exists idx_category_quotas_club on public.category_quotas (club_id);

-- ============ 10. RLS ============
alter table public.sports enable row level security;
alter table public.clubs enable row level security;
alter table public.club_sports enable row level security;
alter table public.teams enable row level security;

-- Lectura para todo el staff autenticado
drop policy if exists "staff lee sports" on public.sports;
create policy "staff lee sports"
  on public.sports for select
  using (auth.role() = 'authenticated');

drop policy if exists "staff lee clubs" on public.clubs;
create policy "staff lee clubs"
  on public.clubs for select
  using (auth.role() = 'authenticated');

drop policy if exists "staff lee club_sports" on public.club_sports;
create policy "staff lee club_sports"
  on public.club_sports for select
  using (auth.role() = 'authenticated');

drop policy if exists "staff lee teams" on public.teams;
create policy "staff lee teams"
  on public.teams for select
  using (auth.role() = 'authenticated');

-- Escritura solo admin (los coordinadores gestionan jugadores, no la estructura)
drop policy if exists "admin escribe sports" on public.sports;
create policy "admin escribe sports"
  on public.sports for all
  using (public.current_rol() = 'admin')
  with check (public.current_rol() = 'admin');

drop policy if exists "admin escribe clubs" on public.clubs;
create policy "admin escribe clubs"
  on public.clubs for all
  using (public.current_rol() = 'admin')
  with check (public.current_rol() = 'admin');

drop policy if exists "admin escribe club_sports" on public.club_sports;
create policy "admin escribe club_sports"
  on public.club_sports for all
  using (public.current_rol() = 'admin')
  with check (public.current_rol() = 'admin');

drop policy if exists "admin coord escribe teams" on public.teams;
create policy "admin coord escribe teams"
  on public.teams for all
  using (public.current_rol() in ('admin', 'coordinator'))
  with check (public.current_rol() in ('admin', 'coordinator'));

-- ============ 11. VISTA de plantillas por club/deporte/equipo ============
create or replace view public.v_team_rosters
with (security_invoker = true)
as
select
  cl.id   as club_id,
  cl.nombre as club,
  sp.nombre as deporte,
  t.id    as team_id,
  t.nombre as equipo,
  t.categoria,
  t.temporada,
  t.entidad,
  count(p.id) as num_jugadores
from public.teams t
join public.clubs cl on cl.id = t.club_id
join public.sports sp on sp.id = t.sport_id
left join public.players p on p.team_id = t.id
group by cl.id, cl.nombre, sp.nombre, t.id, t.nombre, t.categoria, t.temporada, t.entidad;

grant select, insert, update, delete on public.sports, public.clubs, public.club_sports, public.teams to authenticated, service_role;
grant select on public.v_team_rosters to authenticated, service_role;
