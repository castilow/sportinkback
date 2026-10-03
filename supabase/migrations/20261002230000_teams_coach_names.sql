-- Guardar el entrenador de una plantilla antes de disponer de su cuenta.
alter table public.teams add column if not exists coach_name text;
