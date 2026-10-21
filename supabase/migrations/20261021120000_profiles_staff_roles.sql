-- Los roles de la API deben poder persistirse también en el perfil efectivo.
-- No concede permisos a pending: sigue sin acceso hasta la aprobación.
begin;

alter table public.profiles drop constraint if exists profiles_rol_check;
alter table public.profiles add constraint profiles_rol_check
    check (rol in ('admin', 'coordinator', 'coach', 'office', 'physio', 'pending'));

commit;
