-- Alertas de minutos (y minutos) guardadas sin club_id.
-- Desde que el panel de coordinación filtra por club, estas alertas no
-- aparecían. Se asigna el club del jugador; si no se encuentra, el club
-- por defecto (Rayo).

-- 1) Alertas: club del jugador
update public.app_documents a
   set doc = a.doc || jsonb_build_object('club_id', p.club_id::text),
       updated_at = now()
  from public.players p
 where a.collection = 'coordinator_alerts'
   and (a.doc->>'club_id') is null
   and p.id::text = a.doc->>'player_id'
   and p.club_id is not null;

-- 2) Minutos: club del primer jugador del registro
update public.app_documents a
   set doc = a.doc || jsonb_build_object('club_id', p.club_id::text),
       updated_at = now()
  from public.players p
 where a.collection = 'minutes'
   and (a.doc->>'club_id') is null
   and p.id::text = a.doc->'records'->0->>'player_id'
   and p.club_id is not null;

-- 3) Lo que quede: club por defecto
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
    raise notice 'No hay club por defecto; se omiten las alertas sin jugador conocido';
    return;
  end if;

  update public.app_documents
     set doc = doc || jsonb_build_object('club_id', v_club::text),
         updated_at = now()
   where collection in ('coordinator_alerts', 'minutes')
     and (doc->>'club_id') is null;

  get diagnostics v_filas = row_count;
  raise notice 'club_id asignado a % documentos restantes (club %)', v_filas, v_club;
end $$;
