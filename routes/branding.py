"""Branding / White Label endpoints.

Modelo: varios "presets" de marca (uno por cliente/propuesta), cada uno con
nombre, colores, logo y nombre de app. Exactamente uno está "activo" en
cada momento: ese es el que ve el login real y el resto del software.

- GET  /branding                          -> público. Marca activa (la usa el login antes de autenticar).
- PUT  /branding, POST /branding          -> admin. Alias entre sí: guardan cambios sobre el preset ACTIVO
                                              (compatibilidad con el botón "Guardar y aplicar" ya existente).
- POST /branding/logo                     -> admin. Sube un logo a Storage y devuelve su ruta.

- GET    /branding/presets                -> admin. Lista todos los presets guardados.
- POST   /branding/presets                -> admin. Crea un preset nuevo con nombre (no lo activa).
- PUT    /branding/presets/{id}           -> admin. Actualiza los datos de un preset (nombre/colores/logo).
- DELETE /branding/presets/{id}           -> admin. Borra un preset (no se puede borrar el activo ni el último).
- POST   /branding/presets/{id}/activate  -> admin. Marca ese preset como el activo (lo publica en vivo).
"""
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from deps import (
    db, now_iso, uuid, APP_NAME, MAX_UPLOAD_BYTES,
    require_roles, put_object,
)

router = APIRouter()

# Clave legacy: antes de existir presets, había un único documento "default".
# Se conserva solo para poder migrarlo automáticamente al nuevo modelo.
_LEGACY_KEY = "default"

DEFAULT_BRANDING = {
    "appName": "Sportink",
    "logoUrl": "/sportink-logo.png",
    "wordmarkUrl": "/sportink-wordmark.png",
    "colors": {"primary": "#1d4ed8", "navy": "#0a1f4d", "accent": "#3b82f6"},
}


class BrandingColors(BaseModel):
    primary: str = "#1d4ed8"
    navy: str = "#0a1f4d"
    accent: str = "#3b82f6"


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


async def _list_presets_raw() -> List[dict]:
    await _migrate_legacy_if_needed()
    docs = await db.branding.find(None, {"_id": 0}).to_list(500)
    # Filtramos en Python (en vez de en la query) por compatibilidad con ambos backends (Mongo/Postgres).
    return [d for d in docs if d.get("id")]


async def _get_active_raw() -> dict:
    presets = await _list_presets_raw()
    if not presets:
        # Primer arranque: no existe ningún preset todavía -> creamos "Predeterminado".
        seed = {
            "id": str(uuid.uuid4()),
            "name": "Predeterminado",
            "active": True,
            **DEFAULT_BRANDING,
            "updated_at": now_iso(),
            "updated_by": None,
        }
        await db.branding.insert_one(seed)
        return seed
    active = next((p for p in presets if p.get("active")), None)
    if active:
        return active
    # Ninguno marcado como activo (no debería pasar) -> activamos el más reciente.
    presets.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    fallback = presets[0]
    await db.branding.update_one({"id": fallback["id"]}, {"$set": {"active": True}})
    fallback["active"] = True
    return fallback


@router.get("/branding")
async def get_branding():
    """Marca activa. Público: el login la necesita antes de autenticar."""
    active = await _get_active_raw()
    return _clean(active)


async def _save_active(payload: BrandingIn, user: dict) -> dict:
    active = await _get_active_raw()
    data = {
        "appName": payload.appName,
        "logoUrl": payload.logoUrl,
        "wordmarkUrl": payload.wordmarkUrl,
        "colors": payload.colors.dict(),
        "updated_at": now_iso(),
        "updated_by": user.get("id"),
    }
    await db.branding.update_one({"id": active["id"]}, {"$set": data})
    updated = await db.branding.find_one({"id": active["id"]}, {"_id": 0})
    return _clean(updated)


@router.put("/branding")
async def put_branding(payload: BrandingIn, user=Depends(require_roles("admin"))):
    return await _save_active(payload, user)


@router.post("/branding")
async def post_branding(payload: BrandingIn, user=Depends(require_roles("admin"))):
    return await _save_active(payload, user)


@router.post("/branding/logo")
async def upload_logo(file: UploadFile = File(...), user=Depends(require_roles("admin"))):
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

@router.get("/branding/presets")
async def list_presets(user=Depends(require_roles("admin"))):
    presets = await _list_presets_raw()
    presets.sort(key=lambda p: p.get("updated_at") or "", reverse=True)
    return [_clean_preset(p) for p in presets]


@router.post("/branding/presets")
async def create_preset(payload: PresetIn, user=Depends(require_roles("admin"))):
    data = {
        "id": str(uuid.uuid4()),
        "name": payload.name.strip() or "Sin nombre",
        "active": False,
        "appName": payload.appName,
        "logoUrl": payload.logoUrl,
        "wordmarkUrl": payload.wordmarkUrl,
        "colors": payload.colors.dict(),
        "updated_at": now_iso(),
        "updated_by": user.get("id"),
    }
    await db.branding.insert_one(data)
    return _clean_preset(data)


@router.put("/branding/presets/{preset_id}")
async def update_preset(preset_id: str, payload: PresetUpdate, user=Depends(require_roles("admin"))):
    existing = await db.branding.find_one({"id": preset_id}, {"_id": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Preset no encontrado")
    patch: Dict = {"updated_at": now_iso(), "updated_by": user.get("id")}
    if payload.name is not None:
        patch["name"] = payload.name.strip() or existing.get("name") or "Sin nombre"
    if payload.appName is not None:
        patch["appName"] = payload.appName
    if payload.logoUrl is not None:
        patch["logoUrl"] = payload.logoUrl
    if payload.wordmarkUrl is not None:
        patch["wordmarkUrl"] = payload.wordmarkUrl
    if payload.colors is not None:
        patch["colors"] = payload.colors.dict()
    await db.branding.update_one({"id": preset_id}, {"$set": patch})
    updated = await db.branding.find_one({"id": preset_id}, {"_id": 0})
    return _clean_preset(updated)


@router.delete("/branding/presets/{preset_id}")
async def delete_preset(preset_id: str, user=Depends(require_roles("admin"))):
    presets = await _list_presets_raw()
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Preset no encontrado")
    if len(presets) <= 1:
        raise HTTPException(status_code=400, detail="No podés borrar el único preset que queda")
    if target.get("active"):
        raise HTTPException(status_code=400, detail="No podés borrar el preset activo. Activá otro primero.")
    await db.branding.delete_one({"id": preset_id})
    return {"ok": True}


@router.post("/branding/presets/{preset_id}/activate")
async def activate_preset(preset_id: str, user=Depends(require_roles("admin"))):
    presets = await _list_presets_raw()
    target = next((p for p in presets if p.get("id") == preset_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Preset no encontrado")
    for p in presets:
        if p.get("active") and p.get("id") != preset_id:
            await db.branding.update_one({"id": p["id"]}, {"$set": {"active": False}})
    await db.branding.update_one(
        {"id": preset_id},
        {"$set": {"active": True, "updated_at": now_iso(), "updated_by": user.get("id")}},
    )
    updated = await db.branding.find_one({"id": preset_id}, {"_id": 0})
    return _clean_preset(updated)
