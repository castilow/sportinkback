"""Edición de cuentas existentes, separada de su aprobación y contraseña."""
from __future__ import annotations

import asyncio
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from deps import (
    db, logger, now_iso, require_club_context, require_roles,
    invalidate_user_cache, supabase_admin_find_user_by_email,
    get_supabase_staff_user, update_supabase_staff_user,
)
from players_store import TEMPORADA
from postgres_compat import _json_literal, _sql_literal

router = APIRouter()
MANAGEABLE_ROLES = {
    "admin": {"admin", "coordinator", "coach", "office", "physio", "pending"},
    "coordinator": {"coordinator", "coach", "office", "physio", "pending"},
}


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    email: Optional[EmailStr] = None
    role: Optional[Literal["admin", "coordinator", "coach", "office", "physio", "pending"]] = None
    assigned_teams: Optional[list[str]] = Field(default=None, max_length=500)

    @field_validator("name", "email", "role", "assigned_teams", mode="before")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("Omite los campos que no quieras modificar")
        return value.strip() if isinstance(value, str) else value

    @field_validator("assigned_teams")
    @classmethod
    def clean_teams(cls, value):
        teams = [team.strip() for team in value]
        if any(not team or len(team) > 120 for team in teams):
            raise ValueError("Los equipos deben tener un nombre válido")
        return list(dict.fromkeys(teams))


def _public_user(user):
    return {key: value for key, value in user.items() if key not in {"_id", "password_hash"}}


async def _profile_for_update(auth_id):
    if not hasattr(db, "fetch_json_rows"):
        return None
    rows = await db.fetch_json_rows(f"""
        select row_to_json(p)::text from (
          select id::text as id, club_id::text as club_id, email,
                 nombre as name, rol as role,
                 coalesce(equipos_asignados, '{{}}') as assigned_teams
          from public.profiles where id = {_sql_literal(auth_id)}::uuid
        ) p;
    """)
    return rows[0] if rows else None


async def _validated_teams(club_id, teams):
    if not teams:
        return []
    if hasattr(db, "fetch_json_rows"):
        rows = await db.fetch_json_rows(f"""
            select row_to_json(t)::text from (
              select nombre from public.teams
              where club_id = {_sql_literal(club_id)}::uuid
                and temporada = {_sql_literal(TEMPORADA)} and activo = true
            ) t;
        """)
        names = {row["nombre"].casefold(): row["nombre"] for row in rows}
    else:
        # Compatibilidad con instalaciones antiguas que aún usan MongoDB.
        players = await db.players.find({"club_id": club_id}, {"_id": 0, "team": 1}).to_list(5000)
        names = {row["team"].casefold(): row["team"] for row in players if row.get("team")}
    if any(team.casefold() not in names for team in teams):
        raise HTTPException(status_code=400, detail="Algún equipo no es válido para tu club y temporada")
    return list(dict.fromkeys(names[team.casefold()] for team in teams))


async def _persist_update(target, changes, auth_id):
    if not hasattr(db, "fetch_json_rows"):
        result = await db.users.update_one(
            {"id": target["id"], "club_id": target["club_id"]}, {"$set": changes},
        )
        if result.matched_count != 1:
            raise RuntimeError("La cuenta ya no existe")
        return

    # Una sola sentencia: el perfil efectivo y el documento de la lista cambian
    # juntos, incluso si un constraint de profiles rechaza el nuevo rol/email.
    fields = {"name": "nombre", "email": "email", "role": "rol", "assigned_teams": "equipos_asignados"}
    assignments = []
    for field, column in fields.items():
        if field not in changes:
            continue
        value = changes[field]
        literal = ("array[" + ", ".join(_sql_literal(team) for team in value) + "]::text[]"
                   if field == "assigned_teams" else _sql_literal(value))
        assignments.append(f"{column} = {literal}")
    # Siempre hay campos de negocio; auth_user_id/updated_at no pertenecen al perfil.
    profile_sql = f"""
        profile_update as (
          update public.profiles set {', '.join(assignments)}
          where id = {_sql_literal(auth_id)}::uuid
            and club_id = {_sql_literal(target['club_id'])}::uuid
            and exists (select 1 from document_update)
          returning id
        )
    """ if assignments else ""
    rows = await db.fetch_json_rows(f"""
        with document_update as (
          update public.app_documents
          set doc = doc || {_json_literal(changes)}, updated_at = now()
          where collection = 'users'
            and doc->>'id' = {_sql_literal(target['id'])}
            and doc->>'club_id' = {_sql_literal(target['club_id'])}
          returning doc
        ){',' if profile_sql else ''} {profile_sql} select doc::text from document_update;
    """)
    if not rows:
        raise RuntimeError("La cuenta ya no existe")


@router.patch("/users/{user_id}")
async def update_user(user_id: str, data: UserUpdate, user=Depends(require_roles("admin", "coordinator"))):
    club_id = require_club_context(user)
    target = await db.users.find_one({"id": user_id}, {"_id": 0, "password_hash": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if target.get("club_id") != club_id:
        raise HTTPException(status_code=403, detail="Ese usuario es de otro club")
    allowed = MANAGEABLE_ROLES.get(user.get("role"), set())
    if target.get("role") not in allowed:
        raise HTTPException(status_code=403, detail="No puedes editar usuarios con ese rol")
    changes = data.model_dump(exclude_unset=True)
    if not changes:
        return _public_user(target)

    try:
        auth_id = target.get("auth_user_id")
        auth_user = (await asyncio.to_thread(get_supabase_staff_user, auth_id) if auth_id else
                     await asyncio.to_thread(supabase_admin_find_user_by_email, target.get("email", "")))
        if not auth_user:
            raise HTTPException(status_code=409, detail="La cuenta no tiene un acceso asociado que se pueda editar")
        auth_id = auth_user["id"]
        if (auth_user.get("email") or "").lower() != (target.get("email") or "").lower():
            raise HTTPException(status_code=409, detail="El correo de acceso y el de la cuenta no coinciden")
        linked_app_id = (auth_user.get("user_metadata") or {}).get("app_user_id")
        if not target.get("auth_user_id") and linked_app_id and linked_app_id != target["id"]:
            raise HTTPException(status_code=409, detail="El acceso está asociado a otra cuenta")
        profile = await _profile_for_update(auth_id)
    except HTTPException:
        raise
    except Exception:
        logger.exception("No se pudo verificar la cuenta que se va a editar")
        raise HTTPException(status_code=503, detail="No se pudo verificar el acceso. Inténtalo de nuevo")

    # El perfil decide los permisos; Auth decide el correo real de acceso.
    effective = {**target, **(profile or {}), "email": auth_user["email"].lower()}
    effective.setdefault("assigned_teams", [])
    if profile and profile.get("club_id") != club_id:
        raise HTTPException(status_code=403, detail="El perfil de acceso pertenece a otro club")
    old_role = effective.get("role")
    new_role = changes.get("role", old_role)
    if old_role not in allowed or new_role not in allowed:
        raise HTTPException(status_code=403, detail="No puedes asignar ni editar ese rol")
    if old_role == "pending" and new_role != "pending":
        raise HTTPException(status_code=400, detail="Usa la aprobación de la cuenta para darle acceso")
    if old_role != "pending" and new_role == "pending":
        raise HTTPException(status_code=400, detail="No puedes convertir una cuenta activa en pendiente")

    is_self = user_id == user.get("id") or auth_id in {user.get("id"), user.get("auth_user_id")}
    if is_self and (new_role != old_role or (
        "assigned_teams" in changes and set(changes["assigned_teams"]) != set(effective.get("assigned_teams") or [])
    )):
        raise HTTPException(status_code=400, detail="No puedes cambiar tu propio rol ni tus equipos")
    if "assigned_teams" in changes:
        if new_role in {"admin", "coordinator"} and changes["assigned_teams"]:
            raise HTTPException(status_code=400, detail="Dirección y coordinación no necesitan equipos asignados")
        changes["assigned_teams"] = await _validated_teams(club_id, changes["assigned_teams"])
    if new_role != old_role and new_role in {"admin", "coordinator"}:
        changes["assigned_teams"] = []
    if "email" in changes:
        changes["email"] = str(changes["email"]).lower()
        if changes["email"] != (effective.get("email") or "").lower():
            duplicate = await db.users.find_one({"email": {"$regex": r"^" + re.escape(changes["email"]) + r"$", "$options": "i"}})
            if duplicate and duplicate.get("id") != user_id:
                raise HTTPException(status_code=409, detail="El email ya existe")
            if hasattr(db, "fetch_json_rows"):
                duplicate_profiles = await db.fetch_json_rows(f"""
                    select row_to_json(p)::text from (
                      select id::text as id from public.profiles
                      where lower(email) = {_sql_literal(changes['email'])}
                        and id <> {_sql_literal(auth_id)}::uuid limit 1
                    ) p;
                """)
                if duplicate_profiles:
                    raise HTTPException(status_code=409, detail="El email ya existe")
            try:
                duplicate_auth = await asyncio.to_thread(supabase_admin_find_user_by_email, changes["email"])
            except Exception:
                logger.exception("No se pudo comprobar el correo nuevo")
                raise HTTPException(status_code=503, detail="No se pudo comprobar el correo. Inténtalo de nuevo")
            if duplicate_auth and duplicate_auth.get("id") != auth_id:
                raise HTTPException(status_code=409, detail="Ya existe una cuenta con ese correo")

    updated = {**effective, **changes, "id": target["id"], "auth_user_id": auth_id, "updated_at": now_iso()}
    # PATCH no reescribe campos omitidos: cambiar el nombre no debe revertir
    # un cambio de permisos que se haya guardado en otra petición.
    persisted = dict(changes)
    persisted.update(auth_user_id=auth_id, updated_at=updated["updated_at"])
    auth_changes = dict(changes)
    auth_changed = False
    try:
        await asyncio.to_thread(update_supabase_staff_user, auth_id,
                                **auth_changes, app_user_id=target["id"])
        auth_changed = True
        await _persist_update(target, persisted, auth_id)
    except Exception as exc:
        if auth_changed:
            try:
                previous = {key: effective[key] for key in auth_changes}
                await asyncio.to_thread(update_supabase_staff_user, auth_id,
                                        **previous, app_user_id=target["id"])
            except Exception:
                logger.exception("No se pudo restaurar Auth tras fallar la edición de la cuenta %s", user_id)
        logger.exception("No se pudo guardar la edición de la cuenta %s", user_id)
        if isinstance(exc, HTTPException) and exc.status_code == 409 and not auth_changed:
            raise
        raise HTTPException(status_code=503, detail="No se pudo guardar la cuenta. Inténtalo de nuevo")
    finally:
        invalidate_user_cache()
    return _public_user(updated)
