-- Índice para filtrar por club en SQL (colecciones de app_documents).
-- Acompaña al pre-filtro `doc->>'club_id' = ...` de postgres_compat._find_rows.
create index if not exists idx_app_documents_collection_club
    on public.app_documents (collection, (doc->>'club_id'));
