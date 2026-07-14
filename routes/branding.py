"""Branding / White Label endpoints.

- GET  /branding        -> público (lo necesita el login antes de autenticar).
- PUT  /branding        -> admin: guarda la marca (colores, logo, nombre).
- POST /branding        -> admin: alias de PUT (compatibilidad con el frontend).
- POST /branding/logo   -> admin: sube un logo a Supabase Storage y devuelve su ruta.

De momento es mono-organización (una marca "default"). Para multi-cliente,
basta con cambiar la clave fija por el id de organización.
"""
from typing import Dict, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from deps import (
    db, now_iso, uuid, APP_NAME, MAX_UPLOAD_BYTES,
    require_roles, put_object,
)

router = APIRouter()

BRANDING_KEY = "default"

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


def _clean(doc: dict) -> dict:
    """Devuelve solo los campos públicos de marca."""
    if not doc:
        return dict(DEFAULT_BRANDING)
    return {
        "appName": doc.get("appName") or DEFAULT_BRANDING["appName"],
        "logoUrl": doc.get("logoUrl") or DEFAULT_BRANDING["logoUrl"],
        "wordmarkUrl": doc.get("wordmarkUrl") or DEFAULT_BRANDING["wordmarkUrl"],
        "colors": {**DEFAULT_BRANDING["colors"], **(doc.get("colors") or {})},
    }


@router.get("/branding")
async def get_branding():
    """Marca actual. Público: el login la necesita antes de autenticar."""
    doc = await db.branding.find_one({"key": BRANDING_KEY}, {"_id": 0})
    return _clean(doc)


async def _save(payload: BrandingIn, user: dict) -> dict:
    data = {
        "key": BRANDING_KEY,
        "appName": payload.appName,
        "logoUrl": payload.logoUrl,
        "wordmarkUrl": payload.wordmarkUrl,
        "colors": payload.colors.dict(),
        "updated_at": now_iso(),
        "updated_by": user.get("id"),
    }
    existing = await db.branding.find_one({"key": BRANDING_KEY}, {"_id": 0})
    if existing:
        await db.branding.update_one({"key": BRANDING_KEY}, {"$set": data})
    else:
        await db.branding.insert_one({"id": str(uuid.uuid4()), **data})
    return _clean(await db.branding.find_one({"key": BRANDING_KEY}, {"_id": 0}))


@router.put("/branding")
async def put_branding(payload: BrandingIn, user=Depends(require_roles("admin"))):
    return await _save(payload, user)


@router.post("/branding")
async def post_branding(payload: BrandingIn, user=Depends(require_roles("admin"))):
    return await _save(payload, user)


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
