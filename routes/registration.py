"""Registro público verificado + onboarding multi-club.

Flujo:
1. POST /auth/register          -> crea Auth + registro pending
2. Usuario verifica email
3. POST /auth/register/complete -> finalize_public_registration (SQL atómico)
4. GET  /auth/register/status   -> estado del alta
5. POST /auth/resend-verification
"""
from __future__ import annotations

import re
import unicodedata
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator

from deps import (
    db,
    logger,
    now_iso,
    rate_limit_ip,
    get_current_user,
    set_auth_cookies,
    sign_up_with_supabase,
    sign_in_with_supabase,
    resend_supabase_signup,
    supabase_rpc,
    supabase_admin_find_user_by_email,
    uuid,
)

router = APIRouter()

MAX_TEAMS = 40
MAX_PLAYERS = 500
SPORT_PRESETS = {
    "futbol": "Fútbol",
    "baloncesto": "Baloncesto",
    "balonmano": "Balonmano",
    "voleibol": "Voleibol",
    "hockey": "Hockey",
    "rugby": "Rugby",
    "otro": "Otro",
}


def _normalize_slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t or "club"


class AdminRegistrationIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def strong_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("La contraseña debe tener al menos 8 caracteres")
        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("La contraseña debe incluir letras y números")
        return v


class ClubRegistrationIn(BaseModel):
    nombre: str = Field(..., min_length=2, max_length=160)
    nombre_corto: str = Field("", max_length=80)
    ciudad: str = Field("", max_length=120)
    temporada: str = Field("25-26", max_length=16)
    slug: Optional[str] = Field(None, max_length=80)


class SportRegistrationIn(BaseModel):
    slug: str = Field("futbol", max_length=40)
    nombre: Optional[str] = Field(None, max_length=80)


class TeamRegistrationIn(BaseModel):
    local_id: str = Field(..., min_length=1, max_length=64)
    nombre: str = Field(..., min_length=1, max_length=120)
    categoria: str = Field("", max_length=120)
    genero: Literal["MASCULINO", "FEMENINO", "MIXTO"] = "MIXTO"
    entidad: Literal["club", "fundacion"] = "club"


class PlayerRegistrationIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    team_local_id: str = Field(..., min_length=1, max_length=64)
    category: str = Field("", max_length=120)
    dni: str = Field("", max_length=32)
    birthdate: Optional[str] = None
    notes: str = Field("", max_length=500)


class PublicRegistrationIn(BaseModel):
    admin: AdminRegistrationIn
    club: ClubRegistrationIn
    sport: SportRegistrationIn = Field(default_factory=SportRegistrationIn)
    teams: List[TeamRegistrationIn] = Field(..., min_length=1, max_length=MAX_TEAMS)
    players: List[PlayerRegistrationIn] = Field(default_factory=list, max_length=MAX_PLAYERS)

    @field_validator("teams")
    @classmethod
    def unique_team_names(cls, teams: List[TeamRegistrationIn]):
        names = [t.nombre.strip().lower() for t in teams]
        if len(names) != len(set(names)):
            raise ValueError("Hay equipos con el mismo nombre")
        local_ids = [t.local_id for t in teams]
        if len(local_ids) != len(set(local_ids)):
            raise ValueError("local_id de equipo duplicado")
        return teams


def _build_payload(data: PublicRegistrationIn) -> dict:
    sport_slug = _normalize_slug(data.sport.slug or "futbol")
    sport_name = data.sport.nombre or SPORT_PRESETS.get(sport_slug, sport_slug.title())
    club_slug = _normalize_slug(data.club.slug or data.club.nombre)
    team_ids = {t.local_id for t in data.teams}
    for p in data.players:
        if p.team_local_id not in team_ids:
            raise HTTPException(status_code=400, detail=f"Jugador '{p.name}' apunta a un equipo inexistente")
    return {
        "admin": {"name": data.admin.name.strip(), "email": data.admin.email.lower()},
        "club": {
            "nombre": data.club.nombre.strip(),
            "nombre_corto": (data.club.nombre_corto or data.club.nombre).strip(),
            "ciudad": (data.club.ciudad or "").strip(),
            "temporada": data.club.temporada.strip() or "25-26",
            "slug": club_slug,
        },
        "sport": {"slug": sport_slug, "nombre": sport_name},
        "teams": [
            {
                "local_id": t.local_id,
                "nombre": t.nombre.strip(),
                "categoria": (t.categoria or t.nombre).strip(),
                "genero": t.genero,
                "entidad": t.entidad,
                "temporada": data.club.temporada.strip() or "25-26",
            }
            for t in data.teams
        ],
        "players": [
            {
                "name": p.name.strip(),
                "team_local_id": p.team_local_id,
                "category": (p.category or "").strip(),
                "dni": (p.dni or "").strip(),
                "birthdate": p.birthdate,
                "notes": (p.notes or "").strip(),
            }
            for p in data.players
        ],
    }


async def _insert_pending_registration(email: str, admin_name: str, auth_user_id: Optional[str], payload: dict) -> str:
    reg_id = str(uuid.uuid4())
    doc = {
        "id": reg_id,
        "email": email.lower(),
        "auth_user_id": auth_user_id,
        "admin_name": admin_name,
        "payload": payload,
        "status": "pending",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    # Preferimos SQL si está disponible
    if hasattr(db, "execute"):
        from postgres_compat import _sql_literal
        import json as _json

        await db.execute(
            f"""
            insert into public.public_registrations
              (id, email, auth_user_id, admin_name, payload, status)
            values (
              {_sql_literal(reg_id)}::uuid,
              {_sql_literal(email.lower())},
              {("null" if not auth_user_id else _sql_literal(auth_user_id) + "::uuid")},
              {_sql_literal(admin_name)},
              {_sql_literal(_json.dumps(payload))}::jsonb,
              'pending'
            );
            """
        )
    else:
        await db.public_registrations.insert_one(doc)
    return reg_id


async def _find_pending_by_email(email: str) -> Optional[dict]:
    if hasattr(db, "fetch_json_rows"):
        from postgres_compat import _sql_literal

        rows = await db.fetch_json_rows(
            f"""
            select id::text as id, email, auth_user_id::text as auth_user_id,
                   admin_name, payload, status, club_id::text as club_id,
                   error_message, created_at::text as created_at
            from public.public_registrations
            where lower(email) = {_sql_literal(email.lower())}
              and status = 'pending'
            order by created_at desc
            limit 1;
            """
        )
        return rows[0] if rows else None
    return await db.public_registrations.find_one({"email": email.lower(), "status": "pending"}, {"_id": 0})


async def _find_registration_by_auth(auth_user_id: str) -> Optional[dict]:
    if hasattr(db, "fetch_json_rows"):
        from postgres_compat import _sql_literal

        rows = await db.fetch_json_rows(
            f"""
            select id::text as id, email, auth_user_id::text as auth_user_id,
                   admin_name, payload, status, club_id::text as club_id,
                   error_message, created_at::text as created_at
            from public.public_registrations
            where auth_user_id = {_sql_literal(auth_user_id)}::uuid
            order by created_at desc
            limit 1;
            """
        )
        return rows[0] if rows else None
    return await db.public_registrations.find_one({"auth_user_id": auth_user_id}, {"_id": 0})


@router.post("/auth/register", status_code=202)
async def register_public(data: PublicRegistrationIn, request: Request):
    await rate_limit_ip(request, "public_register", max_hits=5, window_s=3600)
    email = data.admin.email.lower()
    payload = _build_payload(data)

    existing_pending = await _find_pending_by_email(email)
    if existing_pending:
        return {
            "status": "pending_verification",
            "registration_id": existing_pending["id"],
            "email": email,
            "message": "Ya hay un registro pendiente. Revisa tu correo o reenvía la verificación.",
        }

    # Evitar duplicar clubes/admins ya existentes
    existing_auth = None
    try:
        existing_auth = supabase_admin_find_user_by_email(email)
    except Exception as exc:
        logger.warning("No se pudo comprobar Auth existente: %s", exc)

    if existing_auth:
        raise HTTPException(status_code=409, detail="Ya existe una cuenta con este correo. Inicia sesión.")

    try:
        signup = sign_up_with_supabase(email, data.admin.password, name=data.admin.name.strip())
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Signup falló: %s", exc)
        raise HTTPException(status_code=400, detail="No se pudo crear la cuenta")

    auth_user = signup.get("user") or {}
    auth_user_id = auth_user.get("id")
    try:
        reg_id = await _insert_pending_registration(
            email=email,
            admin_name=data.admin.name.strip(),
            auth_user_id=auth_user_id,
            payload=payload,
        )
    except Exception as exc:
        logger.error("No se pudo guardar registro pendiente: %s", exc)
        # Compensación: borrar Auth huérfano si acaba de crearse
        if auth_user_id:
            try:
                from deps import delete_supabase_auth_user

                delete_supabase_auth_user(auth_user_id)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="No se pudo iniciar el registro")

    # Si Supabase ya devolvió sesión (confirmación desactivada), devolvemos hint
    session = None
    if signup.get("access_token"):
        session = {
            "access_token": signup.get("access_token"),
            "refresh_token": signup.get("refresh_token"),
        }

    return {
        "status": "pending_verification",
        "registration_id": reg_id,
        "email": email,
        "email_verified": bool(auth_user.get("email_confirmed_at") or auth_user.get("confirmed_at")),
        "has_session": bool(session),
        "message": "Cuenta creada. Verifica tu correo para completar el alta del club.",
    }


class ResendVerificationIn(BaseModel):
    email: EmailStr


@router.post("/auth/resend-verification")
async def resend_verification(data: ResendVerificationIn, request: Request):
    await rate_limit_ip(request, "resend_verification", max_hits=8, window_s=3600)
    try:
        resend_supabase_signup(str(data.email))
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Resend falló: %s", exc)
    return {"ok": True, "message": "Si el correo existe, se ha reenviado la verificación."}


@router.get("/auth/register/status")
async def registration_status(user: dict = Depends(get_current_user)):
    auth_user_id = user.get("auth_user_id") or user.get("id")
    reg = await _find_registration_by_auth(auth_user_id) if auth_user_id else None
    if not reg and user.get("email"):
        reg = await _find_pending_by_email(user["email"])
    return {
        "email": user.get("email"),
        "email_verified": bool(user.get("email_verified")),
        "role": user.get("role"),
        "club_id": user.get("club_id"),
        "onboarding_completed": bool(user.get("onboarding_completed")),
        "registration": {
            "id": reg.get("id") if reg else None,
            "status": reg.get("status") if reg else ("completed" if user.get("onboarding_completed") else None),
            "club_id": reg.get("club_id") if reg else user.get("club_id"),
            "error_message": reg.get("error_message") if reg else None,
            "payload": reg.get("payload") if reg and reg.get("status") == "pending" else None,
        },
    }


@router.post("/auth/register/complete")
async def complete_public_registration(
    request: Request,
    response: Response,
    user: dict = Depends(get_current_user),
):
    await rate_limit_ip(request, "register_complete", max_hits=20, window_s=3600)

    if not user.get("email_verified"):
        raise HTTPException(status_code=403, detail="Debes verificar tu correo antes de completar el alta")

    auth_user_id = user.get("auth_user_id") or user.get("id")
    if not auth_user_id:
        raise HTTPException(status_code=401, detail="Sesión inválida")

    # Ya onboarded
    if user.get("onboarding_completed") and user.get("club_id") and user.get("role") != "pending":
        return {
            "status": "completed",
            "club_id": user.get("club_id"),
            "idempotent": True,
            "user": user,
        }

    reg = await _find_registration_by_auth(auth_user_id)
    if not reg:
        reg = await _find_pending_by_email(user.get("email") or "")
    if not reg:
        raise HTTPException(status_code=404, detail="No hay un registro pendiente para esta cuenta")

    try:
        result = supabase_rpc(
            "finalize_public_registration",
            {"p_registration_id": reg["id"], "p_auth_user_id": auth_user_id},
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("finalize_public_registration falló: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))

    # Refrescar identidad
    refreshed = await get_current_user(request)
    return {
        **(result if isinstance(result, dict) else {"result": result}),
        "user": refreshed,
    }


@router.get("/sports")
async def list_sports():
    """Catálogo de deportes (público autenticado o anónimo para el asistente)."""
    presets = [{"slug": k, "nombre": v} for k, v in SPORT_PRESETS.items()]
    if hasattr(db, "fetch_json_rows"):
        try:
            rows = await db.fetch_json_rows(
                "select slug, nombre from public.sports order by nombre;"
            )
            known = {r["slug"]: r for r in rows}
            for p in presets:
                known.setdefault(p["slug"], p)
            return list(known.values())
        except Exception:
            pass
    return presets
