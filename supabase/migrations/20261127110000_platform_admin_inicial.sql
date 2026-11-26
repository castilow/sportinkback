-- Primer dueño de la plataforma Sportink (ve el Panel de Sportink).
insert into public.platform_admins (email, nombre)
values ('jesuscastillogomez21@gmail.com', 'Jesus Castillo')
on conflict (email) do nothing;
