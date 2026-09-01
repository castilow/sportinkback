-- =====================================================================
-- Eliminar las cuentas demo
--
-- Estaban cableadas en seed_admin() y se recreaban en cada arranque.
-- El código ya no las genera; esto borra las que quedaron en la base.
--
-- Cubre los dos dominios que llegaron a sembrarse en algún momento:
--   @sportink.app  (seed actual)
--   @rayomajadahonda.com  (seed antiguo, el que anunciaba el login)
-- =====================================================================

-- 1. Documentos legacy en app_documents (colección "users")
delete from public.app_documents
where collection = 'users'
  and lower(doc->>'email') in (
    'coordinador@sportink.app',
    'entrenador@sportink.app',
    'oficina@sportink.app',
    'fisio@sportink.app',
    'coordinador@rayomajadahonda.com',
    'entrenador@rayomajadahonda.com',
    'oficina@rayomajadahonda.com',
    'fisio@rayomajadahonda.com',
    'admin@rayomajadahonda.com'
  );

-- 2. Perfiles SQL
delete from public.profiles
where lower(email) in (
    'coordinador@sportink.app',
    'entrenador@sportink.app',
    'oficina@sportink.app',
    'fisio@sportink.app',
    'coordinador@rayomajadahonda.com',
    'entrenador@rayomajadahonda.com',
    'oficina@rayomajadahonda.com',
    'fisio@rayomajadahonda.com',
    'admin@rayomajadahonda.com'
  );

-- 3. Usuarios de Supabase Auth
--    profiles.id referencia auth.users(id) on delete cascade, así que este
--    borrado arrastra el perfil aunque el paso 2 no lo haya cogido.
delete from auth.users
where lower(email) in (
    'coordinador@sportink.app',
    'entrenador@sportink.app',
    'oficina@sportink.app',
    'fisio@sportink.app',
    'coordinador@rayomajadahonda.com',
    'entrenador@rayomajadahonda.com',
    'oficina@rayomajadahonda.com',
    'fisio@rayomajadahonda.com',
    'admin@rayomajadahonda.com'
  );
