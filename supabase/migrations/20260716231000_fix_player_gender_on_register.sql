-- Los jugadores solo admiten MASCULINO/FEMENINO; mapear MIXTO -> MASCULINO en el alta.
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
  v_genero text;
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

  insert into public.sports (slug, nombre)
  values (v_sport_slug, v_sport_name)
  on conflict (slug) do update set nombre = excluded.nombre
  returning * into v_sport;

  if v_sport.id is null then
    select * into v_sport from public.sports where slug = v_sport_slug;
  end if;

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

    v_genero := upper(coalesce(v_player->>'genero', v_player->>'gender', ''));
    if v_genero not in ('MASCULINO', 'FEMENINO') then
      -- Heredar del equipo o MASCULINO por defecto
      select genero into v_genero from public.teams where id = v_team_id;
      if v_genero is null or v_genero = 'MIXTO' then
        v_genero := 'MASCULINO';
      end if;
    end if;

    insert into public.players (
      nombres, apellidos, genero, dni, fecha_nacimiento, categoria, equipo,
      situacion, estado, temporada, club_id, team_id, notas
    ) values (
      v_nombres,
      v_apellidos,
      v_genero,
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
