"""Infraestructura compartida del backend usando Supabase."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets as _secrets
import time
import unicodedata
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import bcrypt
import requests
from fastapi import Depends, HTTPException, Request, Response

from postgres_compat import PostgresCompatDB

try:
    from motor.motor_asyncio import AsyncIOMotorClient
except Exception:  # pragma: no cover
    AsyncIOMotorClient = None

# ------------------ Config ------------------
DATABASE_URL = os.environ.get("DATABASE_URL")
MONGO_URL = os.environ.get("MONGO_URL")
DB_NAME = os.environ.get("DB_NAME")
JWT_ALGORITHM = "HS256"
JWT_SECRET = os.environ.get("JWT_SECRET", "unused_when_supabase_auth_enabled")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_STORAGE_BUCKET = os.environ.get("SUPABASE_STORAGE_BUCKET", "club-assets")
APP_NAME = os.environ.get("APP_NAME", "rayomajadahonda")
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 10 * 1024 * 1024))

if DATABASE_URL:
    db = PostgresCompatDB(DATABASE_URL)
    from players_store import install_players_store

    install_players_store(db)
else:
    if not MONGO_URL or not DB_NAME:
        raise RuntimeError("Falta DATABASE_URL o, en su defecto, MONGO_URL y DB_NAME.")
    if AsyncIOMotorClient is None:
        raise RuntimeError("Motor no está disponible y no se ha configurado DATABASE_URL.")
    _client = AsyncIOMotorClient(MONGO_URL)
    db = _client[DB_NAME]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("rayo")


# ------------------ Misc utilities ------------------
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return "".join(c if c.isalnum() else "-" for c in t).strip("-") or "equipo"


# ------------------ Rate limiter ------------------
_ip_hits: dict[str, deque] = defaultdict(deque)
_ip_hits_lock = asyncio.Lock()


async def rate_limit_ip(request: Request, key: str, max_hits: int, window_s: int):
    xff = request.headers.get("x-forwarded-for", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    bucket = f"{client_ip}|{key}"
    now = time.monotonic()
    async with _ip_hits_lock:
        q = _ip_hits[bucket]
        while q and (now - q[0]) > window_s:
            q.popleft()
        if len(q) >= max_hits:
            retry = int(window_s - (now - q[0])) + 1
            raise HTTPException(status_code=429, detail=f"Demasiadas solicitudes. Reintenta en {retry}s.")
        q.append(now)


def _supabase_headers(api_key: str, bearer: Optional[str] = None, extra: Optional[dict] = None) -> dict:
    headers = {"apikey": api_key}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if extra:
        headers.update(extra)
    return headers


def _supabase_error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict):
            return (
                data.get("msg")
                or data.get("message")
                or data.get("error_description")
                or data.get("error")
                or str(data)
            )
        return str(data)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"


def _supabase_request(method: str, path: str, *, api_key: str, bearer: Optional[str] = None, **kwargs) -> requests.Response:
    if not SUPABASE_URL:
        raise RuntimeError("SUPABASE_URL no configurada.")
    headers = kwargs.pop("headers", {})
    headers = _supabase_headers(api_key, bearer, headers)
    return requests.request(method, f"{SUPABASE_URL}{path}", headers=headers, timeout=kwargs.pop("timeout", 30), **kwargs)


def _supabase_admin_request(method: str, path: str, **kwargs) -> requests.Response:
    if not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY no configurada.")
    return _supabase_request(method, path, api_key=SUPABASE_SERVICE_ROLE_KEY, bearer=SUPABASE_SERVICE_ROLE_KEY, **kwargs)


def _supabase_public_request(method: str, path: str, bearer: Optional[str] = None, **kwargs) -> requests.Response:
    if not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_ANON_KEY no configurada.")
    return _supabase_request(method, path, api_key=SUPABASE_ANON_KEY, bearer=bearer, **kwargs)


# ------------------ Storage (Supabase) ------------------
_storage_ready = False


def init_storage() -> Optional[str]:
    global _storage_ready
    if _storage_ready:
        return SUPABASE_STORAGE_BUCKET
    try:
        resp = _supabase_admin_request(
            "POST",
            "/storage/v1/bucket",
            json={"name": SUPABASE_STORAGE_BUCKET, "public": False, "file_size_limit": MAX_UPLOAD_BYTES},
        )
        if resp.status_code not in (200, 201, 400, 409):
            raise RuntimeError(_supabase_error_message(resp))
        _storage_ready = True
        return SUPABASE_STORAGE_BUCKET
    except Exception as exc:
        logger.error(f"Storage init failed: {exc}")
        return None


def put_object(path: str, data: bytes, content_type: str) -> dict:
    bucket = init_storage()
    if not bucket:
        raise HTTPException(status_code=500, detail="Almacenamiento no disponible")
    encoded_path = quote(path, safe="/.-_")
    resp = _supabase_admin_request(
        "POST",
        f"/storage/v1/object/{bucket}/{encoded_path}",
        headers={"Content-Type": content_type, "x-upsert": "true"},
        data=data,
        timeout=120,
    )
    if resp.status_code >= 300:
        raise HTTPException(status_code=500, detail=f"Error subiendo archivo a Supabase Storage: {_supabase_error_message(resp)}")
    return {"path": path, "bucket": bucket, "size": len(data)}


def get_object(path: str):
    bucket = init_storage()
    if not bucket:
        raise HTTPException(status_code=500, detail="Almacenamiento no disponible")
    encoded_path = quote(path, safe="/.-_")
    resp = _supabase_admin_request("GET", f"/storage/v1/object/{bucket}/{encoded_path}", timeout=60)
    if resp.status_code >= 300:
        raise HTTPException(status_code=404 if resp.status_code == 404 else 500, detail=_supabase_error_message(resp))
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


# ------------------ Auth (Supabase) ------------------
def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), h.encode("utf-8"))
    except Exception:
        return False


def _cookie_secure() -> bool:
    raw = (os.environ.get("COOKIE_SECURE") or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return os.environ.get("ENV", os.environ.get("ENVIRONMENT", "")).lower() in ("production", "prod")


def set_auth_cookies(response: Response, access: str, refresh: str):
    secure = _cookie_secure()
    response.set_cookie("access_token", access, httponly=True, secure=secure, samesite="lax", max_age=43200, path="/")
    response.set_cookie("refresh_token", refresh, httponly=True, secure=secure, samesite="lax", max_age=604800, path="/")


def clear_auth_cookies(response: Response):
    response.delete_cookie("access_token", path="/")
    response.delete_cookie("refresh_token", path="/")


def sign_in_with_supabase(email: str, password: str) -> dict:
    resp = _supabase_public_request(
        "POST",
        "/auth/v1/token?grant_type=password",
        json={"email": email, "password": password},
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 300:
        raise HTTPException(status_code=401, detail="Credenciales inválidas")
    return resp.json()


def sign_up_with_supabase(email: str, password: str, *, name: str = "") -> dict:
    """Alta en Supabase Auth. La confirmación de correo depende de la config del proyecto."""
    payload = {
        "email": email.lower(),
        "password": password,
        "data": {"name": name},
    }
    resp = _supabase_public_request(
        "POST",
        "/auth/v1/signup",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 300:
        raise HTTPException(status_code=400, detail=_supabase_error_message(resp))
    return resp.json()


def resend_supabase_signup(email: str) -> None:
    resp = _supabase_public_request(
        "POST",
        "/auth/v1/resend",
        json={"type": "signup", "email": email.lower()},
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 300 and resp.status_code != 422:
        raise HTTPException(status_code=400, detail=_supabase_error_message(resp))


def supabase_rpc(fn_name: str, payload: dict) -> dict:
    """Ejecuta una RPC de Postgres vía PostgREST con service_role."""
    resp = _supabase_admin_request(
        "POST",
        f"/rest/v1/rpc/{fn_name}",
        json=payload,
        headers={"Content-Type": "application/json", "Prefer": "return=representation"},
    )
    if resp.status_code >= 300:
        raise HTTPException(status_code=400, detail=_supabase_error_message(resp))
    data = resp.json()
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else {"result": data[0]}
    if isinstance(data, dict):
        return data
    return {"result": data}


def sign_out_supabase(access_token: str):
    # scope=local: invalida SOLO la sesión de este token/pestaña.
    # Sin esto, GoTrue usa scope=global por defecto y un logout en un
    # dispositivo/pestaña cierra la sesión de ESE MISMO USUARIO en todos
    # los demás dispositivos y pestañas donde tenga sesión abierta.
    resp = _supabase_public_request("POST", "/auth/v1/logout?scope=local", bearer=access_token)
    if resp.status_code >= 300 and resp.status_code != 401:
        logger.warning(f"Supabase logout devolvió {resp.status_code}: {_supabase_error_message(resp)}")


def _extract_access_token(request: Optional[Request] = None, authorization: Optional[str] = None, query_token: Optional[str] = None) -> Optional[str]:
    token = None
    if request:
        token = request.cookies.get("access_token")
        if not token:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
    if not token and authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token and query_token:
        token = query_token
    return token


def get_supabase_user(access_token: str) -> dict:
    resp = _supabase_public_request("GET", "/auth/v1/user", bearer=access_token)
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Token inválido")
    if resp.status_code >= 300:
        raise HTTPException(status_code=401, detail=_supabase_error_message(resp))
    return resp.json()


def _auth_email_verified(auth_user: dict) -> bool:
    if auth_user.get("email_confirmed_at"):
        return True
    # Usuarios creados por admin (seed) suelen venir ya confirmados
    if auth_user.get("confirmed_at"):
        return True
    return False


async def _fetch_profile_by_auth_id(auth_user_id: str) -> Optional[dict]:
    """Lee public.profiles cuando existe la capa Postgres."""
    if not hasattr(db, "fetch_json_rows"):
        return None
    try:
        from postgres_compat import _sql_literal

        rows = await db.fetch_json_rows(
            f"""
            select id::text as id, email, nombre as name, rol as role,
                   coalesce(equipos_asignados, '{{}}') as assigned_teams,
                   club_id::text as club_id,
                   coalesce(onboarding_completed, false) as onboarding_completed,
                   created_at::text as created_at
            from public.profiles
            where id = {_sql_literal(auth_user_id)}::uuid
            limit 1;
            """
        )
        if not rows:
            return None
        row = rows[0]
        teams = row.get("assigned_teams") or []
        if isinstance(teams, str):
            teams = [t for t in teams.strip("{}").split(",") if t]
        return {
            "id": row["id"],
            "auth_user_id": auth_user_id,
            "email": (row.get("email") or "").lower(),
            "name": row.get("name") or row.get("email") or "",
            "role": row.get("role"),
            "assigned_teams": teams,
            "club_id": row.get("club_id"),
            "onboarding_completed": bool(row.get("onboarding_completed")),
            "created_at": row.get("created_at"),
        }
    except Exception as exc:
        logger.warning("No se pudo leer profiles: %s", exc)
        return None


async def resolve_app_user_from_token(access_token: str) -> dict:
    """Resuelve el usuario de la app.

    IMPORTANTE: no se confía en user_metadata.role del cliente.
    El rol solo sale de profiles / app_documents.users provisionados por el servidor.
    """
    auth_user = get_supabase_user(access_token)
    auth_user_id = auth_user.get("id")
    email = (auth_user.get("email") or "").lower()
    email_verified = _auth_email_verified(auth_user)

    profile = await _fetch_profile_by_auth_id(auth_user_id) if auth_user_id else None
    user = None
    if profile and profile.get("role"):
        user = profile
    else:
        user = await db.users.find_one({"auth_user_id": auth_user_id}, {"_id": 0, "password_hash": 0})
        if not user and email:
            user = await db.users.find_one({"email": email}, {"_id": 0, "password_hash": 0})
            if user and user.get("auth_user_id") != auth_user_id:
                await db.users.update_one({"id": user["id"]}, {"$set": {"auth_user_id": auth_user_id}})
                user["auth_user_id"] = auth_user_id

    if not user:
        # Alta pública pendiente de onboarding: identidad mínima sin privilegios
        meta = auth_user.get("user_metadata") or {}
        return {
            "id": auth_user_id,
            "auth_user_id": auth_user_id,
            "email": email,
            "name": meta.get("name") or email,
            "role": "pending",
            "assigned_teams": [],
            "club_id": None,
            "email_verified": email_verified,
            "onboarding_completed": False,
            "created_at": now_iso(),
        }

    user.pop("password_hash", None)
    user["auth_user_id"] = auth_user_id or user.get("auth_user_id")
    user["email_verified"] = email_verified
    user.setdefault("club_id", None)
    user.setdefault("assigned_teams", user.get("assigned_teams") or [])

    # Staff legacy (app_documents) sin club: asignar el club por defecto
    if not user.get("club_id") and user.get("role") in ("admin", "coordinator", "coach", "office", "physio"):
        default_club = await _fetch_default_club_id()
        if default_club:
            user["club_id"] = default_club
            user["onboarding_completed"] = True

    user.setdefault("onboarding_completed", bool(user.get("club_id")))
    return user


async def _fetch_default_club_id() -> Optional[str]:
    if not hasattr(db, "fetch_json_rows"):
        return None
    try:
        rows = await db.fetch_json_rows(
            """
            select id::text as id from public.clubs
            where is_default = true or slug = 'rayo-majadahonda'
            order by is_default desc
            limit 1;
            """
        )
        return rows[0]["id"] if rows else None
    except Exception:
        return None


async def get_current_user(request: Request) -> dict:
    token = _extract_access_token(request=request)
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")
    return await resolve_app_user_from_token(token)


async def get_user_from_request_values(request: Optional[Request] = None, authorization: Optional[str] = None, query_token: Optional[str] = None) -> dict:
    token = _extract_access_token(request=request, authorization=authorization, query_token=query_token)
    if not token:
        raise HTTPException(status_code=401, detail="No autenticado")
    return await resolve_app_user_from_token(token)


def require_roles(*allowed):
    async def _dep(user: dict = Depends(get_current_user)):
        if user.get("role") == "pending":
            raise HTTPException(status_code=403, detail="Completa el registro del club para continuar")
        if user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail="Permiso denegado")
        return user

    return _dep


def require_club_context(user: dict) -> str:
    """Exige club_id en el usuario autenticado."""
    club_id = user.get("club_id")
    if not club_id:
        raise HTTPException(status_code=403, detail="Tu cuenta no tiene club asignado. Completa el onboarding.")
    return club_id


def supabase_admin_list_users() -> list[dict]:
    resp = _supabase_admin_request("GET", "/auth/v1/admin/users?page=1&per_page=1000")
    if resp.status_code >= 300:
        raise RuntimeError(_supabase_error_message(resp))
    return resp.json().get("users", [])


def supabase_admin_find_user_by_email(email: str) -> Optional[dict]:
    email = email.lower()
    for user in supabase_admin_list_users():
        if (user.get("email") or "").lower() == email:
            return user
    return None


def ensure_supabase_staff_user(email: str, password: str, *, name: str, role: str, assigned_teams: Optional[list] = None, app_user_id: Optional[str] = None) -> dict:
    email = email.lower()
    payload = {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {
            "name": name,
            "role": role,
            "assigned_teams": assigned_teams or [],
            "app_user_id": app_user_id,
        },
        "app_metadata": {"role": role},
    }
    existing = supabase_admin_find_user_by_email(email)
    if existing:
        resp = _supabase_admin_request(
            "PUT",
            f"/auth/v1/admin/users/{existing['id']}",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code >= 300:
            raise RuntimeError(_supabase_error_message(resp))
        return resp.json().get("user") or resp.json()
    resp = _supabase_admin_request(
        "POST",
        "/auth/v1/admin/users",
        json=payload,
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 300:
        raise RuntimeError(_supabase_error_message(resp))
    return resp.json().get("user") or resp.json()


def delete_supabase_auth_user(auth_user_id: str):
    resp = _supabase_admin_request(
        "DELETE",
        f"/auth/v1/admin/users/{auth_user_id}",
        json={"shouldSoftDelete": False},
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code >= 300 and resp.status_code != 404:
        raise RuntimeError(_supabase_error_message(resp))


async def create_parent_chat_session(slug: str, child_name: str) -> str:
    token = _secrets.token_urlsafe(32)
    await db.parent_chat_sessions.insert_one({
        "id": str(uuid.uuid4()),
        "token": token,
        "slug": slug,
        "child": child_name.strip(),
        "created_at": now_iso(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
    })
    return token


async def _get_parent_chat_session(token: str, slug: str) -> Optional[dict]:
    session = await db.parent_chat_sessions.find_one({"token": token, "slug": slug}, {"_id": 0})
    if not session:
        return None
    expires_at = session.get("expires_at")
    if expires_at:
        try:
            if datetime.now(timezone.utc) >= datetime.fromisoformat(expires_at):
                await db.parent_chat_sessions.delete_one({"id": session["id"]})
                return None
        except Exception:
            return None
    return session


# ------------------ Parent-or-staff (chat) ------------------
async def _parent_or_staff(request: Request, slug: str) -> dict:
    """Resuelve identidad para el chat público/privado del equipo."""
    pcookie = request.cookies.get(f"chat_{slug}")
    if pcookie:
        session = await _get_parent_chat_session(pcookie, slug)
        if session:
            return {"kind": "parent", "child": session.get("child")}
    try:
        user = await get_current_user(request)
        return {"kind": "staff", "role": user["role"], "name": user["name"], "id": user["id"]}
    except HTTPException:
        raise HTTPException(status_code=401, detail="Debes identificarte para acceder al chat")


__all__ = [
    "db",
    "logger",
    "APP_NAME",
    "MAX_UPLOAD_BYTES",
    "SUPABASE_STORAGE_BUCKET",
    "SUPABASE_URL",
    "SUPABASE_ANON_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "_secrets",
    "now_iso",
    "_slug",
    "rate_limit_ip",
    "init_storage",
    "put_object",
    "get_object",
    "hash_password",
    "verify_password",
    "set_auth_cookies",
    "clear_auth_cookies",
    "sign_in_with_supabase",
    "sign_up_with_supabase",
    "sign_out_supabase",
    "resend_supabase_signup",
    "supabase_rpc",
    "get_current_user",
    "get_user_from_request_values",
    "require_roles",
    "require_club_context",
    "ensure_supabase_staff_user",
    "delete_supabase_auth_user",
    "create_parent_chat_session",
    "_parent_or_staff",
    "uuid",
]
