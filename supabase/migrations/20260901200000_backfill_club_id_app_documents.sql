-- =====================================================================
-- Backfill de club_id en app_documents
--
-- Inventario, incidencias, asistencia, minutos, tickets, salas de chat y
-- hojas se guardaban sin club. Al empezar a filtrar por club_id, esos
-- documentos dejarían de aparecer. Esto los asigna al club existente.
--
-- Cambia el UUID si tu club por defecto es otro:
--   select id, nombre, slug, is_default from public.clubs;
-- =====================================================================

do $$
declare
  v_club uuid;
  v_filas int;
begin
  select id into v_club
  from public.clubs
  where is_default = true or slug = 'rayo-majadahonda'
  order by is_default desc
  limit 1;

  if v_club is null then
    raise exception 'No hay club por defecto. Marca uno: update public.clubs set is_default = true where id = ...';
  end if;

  update public.app_documents
     set doc = doc || jsonb_build_object('club_id', v_club::text),
         updated_at = now()
   where collection in (
           'inventory', 'incidents', 'attendance', 'minutes',
           'coordinator_alerts', 'tickets', 'team_rooms',
           'training_sheets', 'match_sheets', 'convocations',
           'captacion_entries', 'player_kits', 'injuries', 'users'
         )
     and (doc->>'club_id') is null;

  get diagnostics v_filas = row_count;
  raise notice 'club_id asignado a % documentos (club %)', v_filas, v_club;
end $$;

-- Comprobación: debe quedar todo a 0
select collection, count(*) as sin_club
from public.app_documents
where collection in (
        'inventory', 'incidents', 'attendance', 'minutes',
        'coordinator_alerts', 'tickets', 'team_rooms',
        'training_sheets', 'match_sheets', 'convocations',
        'captacion_entries', 'player_kits', 'injuries', 'users'
      )
  and (doc->>'club_id') is null
group by collection
order by collection;
