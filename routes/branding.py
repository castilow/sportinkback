"""Branding / White Label endpoints (una marca por CLUB).

Cada club tiene sus propios "presets" de marca (nombre, colores, logo) y
exactamente uno activo. Los presets antiguos sin `club_id` pertenecen al club
por defecto (Rayo Majadahonda).

- GET  /branding?club=<slug>              -> público. Marca activa de ese club (o del club por defecto).
                                              La usa el login antes de autenticar.
- GET  /branding/me                       -> autenticado. Marca activa del club del usuario.
- PUT/POST /branding                      -> admin. Guarda cambios sobre el preset ACTIVO de SU club.
- POST /branding/logo                     -> admin. Sube un logo a Storage.
- GET/POST /branding/presets              -> admin. Lista / crea presets de SU club.
- PUT/DELETE /branding/presets/{id}       -> admin. Solo presets de SU club.
- POST /branding/presets/{id}/activate    -> admin. Publica ese preset en su club.
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator

from deps import (
    db, now_iso, uuid, APP_NAME, MAX_UPLOAD_BYTES,
    require_roles, put_object, _fetch_default_club_id, get_current_user, require_club_context,
)

router = APIRouter()


async def require_brand_admin(user=Depends(require_roles("admin"))):
    """Solo el admin de un club puede editar la marca de SU club."""
    require_club_context(user)
    return user


async def _club_id_from_slug(slug: str) -> Optional[str]:
    if not hasattr(db, "fetch_json_rows") or not slug:
        return None
    from postgres_compat import _sql_literal

    rows = await db.fetch_json_rows(
        f"select json_build_object('id', id::text)::text from public.clubs where slug = {_sql_literal(slug.lower())} limit 1;"
    )
    return rows[0]["id"] if rows else None


async def _club_display_name(club_id: str) -> Optional[str]:
    if not hasattr(db, "fetch_json_rows"):
        return None
    from postgres_compat import _sql_literal

    rows = await db.fetch_json_rows(
        f"select json_build_object('n', coalesce(nombre_corto, nombre))::text from public.clubs where id = {_sql_literal(club_id)}::uuid limit 1;"
    )
    return rows[0]["n"] if rows else None


# Clave legacy: antes de existir presets, había un único documento "default".
# Se conserva solo para poder migrarlo automáticamente al nuevo modelo.
_LEGACY_KEY = "default"

DEFAULT_BRANDING = {
    "appName": "Sportink",
    "logoUrl": "/sportink-logo.png",
    "wordmarkUrl": "/sportink-wordmark.png",
    "colors": {"primary": "#1d4ed8", "navy": "#0a1f4d", "accent": "#3b82f6"},
}


import re as _re

_HEX = _re.compile(r"^#[0-9a-fA-F]{6}$")


class BrandingColors(BaseModel):
    primary: str = "#1d4ed8"
    navy: str = "#0a1f4d"
    accent: str = "#3b82f6"

    @field_validator("primary", "navy", "accent")
    @classmethod
    def _valid_hex(cls, v: str) -> str:
        if not _HEX.match(v or ""):
            raise ValueError("Color inválido: usa formato #RRGGBB")
        return v


_MAX_LOGO_CHARS = 400_000  # ~300 KB en base64


def _check_logo(v: Optional[str]) -> Optional[str]:
    if v and len(v) > _MAX_LOGO_CHARS:
        raise HTTPException(status_code=413, detail="El logo es demasiado grande. Usa una imagen de menos de 300 KB.")
    if v and v.startswith("data:") and not v.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="El logo debe ser una imagen.")
    return v


class BrandingIn(BaseModel):
    appName: str = Field(default="Sportink", max_length=80)
    logoUrl: Optional[str] = None
    wordmarkUrl: Optional[str] = None
    colors: BrandingColors = BrandingColors()


class PresetIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    appName: str = Field(default="Sportink", max_length=80)
    logoUrl: Optional[str] = None
    wordmarkUrl: Optional[str] = None
    colors: BrandingColors = BrandingColors()


class PresetUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    appName: Optional[str] = Field(default=None, max_length=80)
    logoUrl: Optional[str] = None
    wordmarkUrl: Optional[str] = None
    colors: Optional[BrandingColors] = None


def _clean(doc: dict) -> dict:
    """Solo los campos públicos de marca (para el login / resto del software)."""
    if not doc:
        return dict(DEFAULT_BRANDING)
    return {
        "appName": doc.get("appName") or DEFAULT_BRANDING["appName"],
        "logoUrl": doc.get("logoUrl") or DEFAULT_BRANDING["logoUrl"],
        "wordmarkUrl": doc.get("wordmarkUrl") or DEFAULT_BRANDING["wordmarkUrl"],
        "colors": {**DEFAULT_BRANDING["colors"], **(doc.get("colors") or {})},
    }


def _clean_preset(doc: dict) -> dict:
    """Forma completa de un preset (incluye id/nombre/estado), para el panel de admin."""
    base = _clean(doc)
    return {
        "id": doc.get("id"),
        "name": doc.get("name") or "Sin nombre",
        "active": bool(doc.get("active")),
        "updated_at": doc.get("updated_at"),
        **base,
    }


async def _migrate_legacy_if_needed() -> None:
    """Convierte el antiguo documento único {key: 'default'} en el primer preset."""
    legacy = await db.branding.find_one({"key": _LEGACY_KEY})
    if not legacy:
        return
    data = {
        "id": str(uuid.uuid4()),
        "name": "Predeterminado",
        "active": True,
        "appName": legacy.get("appName"),
        "logoUrl": legacy.get("logoUrl"),
        "wordmarkUrl": legacy.get("wordmarkUrl"),
        "colors": legacy.get("colors") or {},
        "updated_at": legacy.get("updated_at") or now_iso(),
        "updated_by": legacy.get("updated_by"),
    }
    await db.branding.insert_one(data)
    await db.branding.delete_one({"key": _LEGACY_KEY})


async def _list_presets_raw(club_id: str) -> List[dict]:
    await _migrate_legacy_if_needed()
    default_club = await _fetch_default_club_id()
    docs = await db.branding.find(None, {"_id": 0}).to_list(2000)
    # Filtro en Python por compatibilidad Mongo/Postgres. Sin club_id = club por defecto.
    return [d for d in docs if d.get("id") and (d.get("club_id") or default_club) == club_id]


async def _get_active_raw(club_id: str) -> dict:
    presets = await _list_presets_raw(club_id)
    if not presets:
        # Primer uso de este club: creamos su preset inicial con el nombre del club.
        default_club = await _fetch_default_club_id()
        name = None if club_id == default_club else await _club_display_name(club_id)
        seed = {
            "id": str(uuid.uuid4()),
            "club_id": club_id,
            "name": "Predeterminado",
            "active": True,
            **DEFAULT_BRANDING,
            "appName": name or DEFAULT_BRANDING["appName"],
            "updated_at": now_iso(),
            "updated_by": None,
        }
        await db.branding.insert_one(seed)
        return seed
    active = next((p for p in presets if p.get("active")), None)
    if active:
        return active
    presets.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    fallback = presets[0]
    await db.branding.update_one({"id": fallback["id"]}, {"$set": {"active": True}})
    fallback["active"] = True
    return fallback


@router.get("/branding")
async def get_branding(club: Optional[str] = Query(default=None, max_length=80)):
    """Marca activa. Público: el login la necesita antes de autenticar.
    Con ?club=<slug> devuelve la de ese club; sin él, la del club por defecto."""
    club_id = await _club_id_from_slug(club) if club else None
    if not club_id:
        club_id = await _fetch_default_club_id()
    if not club_id:
        return dict(DEFAULT_BRANDING)
    return _clean(await _get_active_raw(club_id))


@router.get("/branding/me")
async def get_my_branding(user=Depends(get_current_user)):
    """Marca activa del club del usuario autenticado."""
    club_id = user.get("club_id")
    if not club_id:
        return dict(DEFAULT_BRANDING)
    return _clean(await _get_active_raw(club_id))


async def _save_active(payload: BrandingIn, user: dict) -> dict:
    club_id = require_club_context(user)
    active = await _get_active_raw(club_id)
    data = {
        "appName": payload.appName.strip() or DEFAULT_BRANDING["appName"],
        "logoUrl": _check_logo(payload.logoUrl),
        "wordmarkUrl": payload.wordmarkUrl,
        "colors": payload.colors.model_dump(),
        "club_id": club_id,
        "updated_at": now_iso(),
        "updated_by": user.get("id"),
    }
    await db.branding.update_one({"id": active["id"]}, {"$set": data})
    updated = await db.branding.find_one({"id": active["id"]}, {"_id": 0})
    return _clean(updated)


@router.put("/branding")
async def put_branding(payload: BrandingIn, user=Depends(require_brand_admin)):
    return await _save_active(payload, user)


@router.post("/branding")
async def post_branding(payload: BrandingIn, user=Depends(require_brand_admin)):
    return await _save_active(payload, user)


@router.post("/branding/logo")
async def upload_logo(file: UploadFile = File(...), user=Depends(require_brand_admin)):
    """Sube un logo a Storage y devuelve su ruta. Alternativa a incrustarlo en base64."""
    ext = (file.filename or "logo.png").split(".")[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "svg"):
        raise HTTPException(status_code=400, detail="Formato no admitido (usa PNG, JPG, WEBP o SVG)")
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Archivo demasiado grande (máx {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
    path = f"{APP_NAME}/branding/logo-{uuid.uuid4()}.{ext}"
    mime = file.content_type or "image/png"
    result = put_object(path, data, mime)
    return {"storage_path": result["path"], "size": result.get("size", len(data))}


# ------------------------------------------------------------------
# Presets: guardar varias propuestas de marca (una por cliente) y elegir
# cuál está publicada en el software real.
# ------------------------------------------------------------------

async def _own_preset(preset_id: str, club_id: str) -> dict:
    presets = await _list_presets_raw(club_id)
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Preset no encontrado")
    return target


@router.get("/branding/presets")
async def list_presets(user=Depends(require_brand_admin)):
    club_id = require_club_context(user)
    await _get_active_raw(club_id)  # garantiza que existe al menos uno
    presets = await _list_presets_raw(club_id)
    presets.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return [_clean_preset(p) for p in presets]


@router.post("/branding/presets")
async def create_preset(payload: PresetIn, user=Depends(require_brand_admin)):
    club_id = require_club_context(user)
    data = {
        "id": str(uuid.uuid4()),
        "club_id": club_id,
        "name": payload.name.strip() or "Sin nombre",
        "active": False,
        "appName": payload.appName,
        "logoUrl": _check_logo(payload.logoUrl),
        "wordmarkUrl": payload.wordmarkUrl,
        "colors": payload.colors.model_dump(),
        "updated_at": now_iso(),
        "updated_by": user.get("id"),
    }
    await db.branding.insert_one(data)
    return _clean_preset(data)


@router.put("/branding/presets/{preset_id}")
async def update_preset(preset_id: str, payload: PresetUpdate, user=Depends(require_brand_admin)):
    club_id = require_club_context(user)
    existing = await _own_preset(preset_id, club_id)
    patch: Dict = {"club_id": club_id, "updated_at": now_iso(), "updated_by": user.get("id")}
    if payload.name is not None:
        patch["name"] = payload.name.strip() or existing.get("name") or "Sin nombre"
    if payload.appName is not None:
        patch["appName"] = payload.appName
    if payload.logoUrl is not None:
        patch["logoUrl"] = _check_logo(payload.logoUrl)
    if payload.wordmarkUrl is not None:
        patch["wordmarkUrl"] = payload.wordmarkUrl
    if payload.colors is not None:
        patch["colors"] = payload.colors.model_dump()
    await db.branding.update_one({"id": preset_id}, {"$set": patch})
    updated = await db.branding.find_one({"id": preset_id}, {"_id": 0})
    return _clean_preset(updated)


@router.delete("/branding/presets/{preset_id}")
async def delete_preset(preset_id: str, user=Depends(require_brand_admin)):
    club_id = require_club_context(user)
    presets = await _list_presets_raw(club_id)
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Preset no encontrado")
    if len(presets) <= 1:
        raise HTTPException(status_code=400, detail="No puedes borrar el único preset que queda")
    if target.get("active"):
        raise HTTPException(status_code=400, detail="No puedes borrar el preset activo. Activa otro primero.")
    await db.branding.delete_one({"id": preset_id})
    return {"ok": True}


@router.post("/branding/presets/{preset_id}/activate")
async def activate_preset(preset_id: str, user=Depends(require_brand_admin)):
    club_id = require_club_context(user)
    presets = await _list_presets_raw(club_id)
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Preset no encontrado")
    for p in presets:
        if p.get("active") and p.get("id") != preset_id:
            await db.branding.update_one({"id": p["id"]}, {"$set": {"active": False}})
    await db.branding.update_one(
        {"id": preset_id},
        {"$set": {"active": True, "club_id": club_id, "updated_at": now_iso(), "updated_by": user.get("id")}},
    )
    updated = await db.branding.find_one({"id": preset_id}, {"_id": 0})
    return _clean_preset(updated)
