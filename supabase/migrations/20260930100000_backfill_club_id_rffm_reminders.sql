-- Aislamiento multi-club: RFFM (equipos, partidos, goleadores, clasificación) y
-- recordatorios no llevaban club_id. Todo lo existente es del club por defecto.
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
   where collection in ('rffm_teams','rffm_matches','rffm_scorers','rffm_standings','reminders')
     and (doc->>'club_id') is null;

  get diagnostics v_filas = row_count;
  raise notice 'club_id asignado a % documentos (club %)', v_filas, v_club;
end $$;
