-- =====================================================================
-- app_documents: cerrar el acceso directo vía PostgREST
--
-- Esta tabla guarda usuarios, jugadores legacy, inventario, incidencias,
-- asistencia, minutos, lesiones, tickets, chat y sesiones de padres.
-- Se creó sin RLS y sin políticas, así que con la SUPABASE_ANON_KEY
-- (que es pública por diseño) se podía leer entera:
--     GET /rest/v1/app_documents?select=*
--
-- El backend no se ve afectado: conecta por DATABASE_URL con un rol
-- privilegiado que no pasa por PostgREST ni por RLS.
-- =====================================================================

alter table public.app_documents enable row level security;

-- Sin políticas, RLS deniega todo por defecto para anon/authenticated.
-- Se revocan además los privilegios de tabla por si se añade una política
-- permisiva en el futuro por descuido.
revoke all on public.app_documents from anon;
revoke all on public.app_documents from authenticated;

revoke all on sequence public.app_documents_row_id_seq from anon;
revoke all on sequence public.app_documents_row_id_seq from authenticated;
