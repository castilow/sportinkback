"""VeriFactu (AEAT) para la Caja, a través de Dinvbox (vf-dinvbox.es).

Qué hace
--------
- Cada cobro de una entidad con «VeriFactu» activado se registra como factura
  (F2 simplificada, o F1 si se dan NIF y nombre del cliente) y se envía a
  Dinvbox, que lo remite a la AEAT.
- El cobro NUNCA se pierde por un fallo del envío: se guarda en la caja y el
  envío queda «pendiente» o «error» y se reintenta (botón y tarea cada 10 min).
- El QR del recibo se genera al momento con la URL de cotejo de la AEAT
  (nif, numserie, fecha, importe); si Dinvbox devuelve su ValidationUrl, se usa esa.
- Dinvbox responde por webhook (POST /api/verifactu/webhook?secret=…).

Variables de entorno
--------------------
VERIFACTU_URL          host de Dinvbox (p. ej. https://api.vf-dinvbox.es)
VERIFACTU_TOKEN        token Bearer de la plataforma (o uno por club en Ajustes)
VERIFACTU_WEBHOOK_SECRET  secreto que Dinvbox añade a la URL del webhook
VERIFACTU_ENTORNO      «pruebas» o «produccion» (decide la URL del QR por defecto)
VERIFACTU_QR_BASE      URL de cotejo del QR (si hay que cambiarla)

Pendiente de Dinvbox: anulaciones / rectificativas y «Consultar envíos» no están
en su documentación pública. Al anular un cobro ya enviado se marca
«anulacion_pendiente» para hacerlo cuando tengamos ese endpoint.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from deps import db, logger, now_iso, require_club_context, require_roles

router = APIRouter()

QR_PROD = "https://www2.agenciatributaria.gob.es/wlpl/TIKE-CONT/ValidarQR"
QR_TEST = "https://prewww2.aeat.es/wlpl/TIKE-CONT/ValidarQR"
SIMPLIFICADA_MAX_C = 40000  # 400 € IVA incluido (factura simplificada)
MAX_INTENTOS = 6


def _env(name: str, default: str = "") -> str:
    import os
    return (os.environ.get(name) or default).strip()


try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Madrid")
except Exception:  # sin tzdata
    _TZ = timezone.utc


# ------------------------------------------------------------------ utilidades
def norm_nif(v: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def valid_nif(v: str) -> bool:
    """Formato de NIF/CIF/NIE español (9 caracteres). No comprueba la letra de control."""
    return bool(re.fullmatch(r"[0-9XYZKLM][0-9]{7}[A-Z0-9]|[ABCDEFGHJNPQRSUVW][0-9]{7}[0-9A-J]", norm_nif(v)))


def qr_url(nif: str, numserie: str, fecha_iso: str, importe_c: int) -> str:
    base = _env("VERIFACTU_QR_BASE") or (QR_TEST if _env("VERIFACTU_ENTORNO").lower().startswith("prueba") else QR_PROD)
    y, m, d = fecha_iso[:10].split("-")
    return base + "?" + urlencode({"nif": norm_nif(nif), "numserie": numserie,
                                   "fecha": f"{d}-{m}-{y}", "importe": f"{importe_c / 100:.2f}"})


def qr_svg(url: str) -> str:
    import segno
    return segno.make(url, error="m").svg_inline(scale=3, border=1)


def _money(c: int) -> float:
    return round((c or 0) / 100, 2)


def _fecha_expedicion(doc: dict) -> str:
    """Fecha y hora de emisión en UTC con el formato de los ejemplos de Dinvbox."""
    hora = doc.get("hora") or "12:00"
    local = datetime.fromisoformat(f"{doc['fecha']}T{hora}:00").replace(tzinfo=_TZ)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_alta(doc: dict, entidad: dict) -> dict:
    """Cuerpo de POST /api/verifactu/alta a partir de un cobro de la Caja."""
    vf = doc.get("verifactu") or {}
    nif = norm_nif(entidad.get("cif", ""))
    importe_c = int(doc["importe_c"])
    body = {
        "IDEmisorFactura": nif,
        "NIF": nif,
        "NombreRazon": entidad.get("nombre", "")[:120],
        "NumSerieFactura": doc["numero"],
        "FechaExpedicionFactura": _fecha_expedicion(doc),
        "RefExterna": doc["id"],
        "TipoFactura": vf.get("tipo") or "F2",
        "DescripcionOperacion": (doc.get("concepto_nombre") or "Cobro")[:500],
    }
    dest = vf.get("destinatario")
    if body["TipoFactura"] == "F1" and dest:
        body["IDDestinatario"] = {"NombreRazon": dest["nombre"][:120], "NIF": norm_nif(dest["nif"])}
    if vf.get("exenta") or entidad.get("exenta_iva"):
        body["Desglose"] = [{"MotivoExento": vf.get("motivo_exento") or "E1",
                             "BaseImponibleOimporteNoSujeto": _money(importe_c)}]
        body["ImporteTotal"] = _money(importe_c)
    else:
        base_c = int(doc.get("base_c") if doc.get("base_c") is not None else importe_c)
        cuota_c = int(doc.get("cuota_iva_c") or 0)
        body["Desglose"] = [{
            "Impuesto": "01", "ClaveRegimen": "01", "CalificacionOperacion": "S1",
            "TipoImpositivo": doc.get("iva"),
            "BaseImponibleOimporteNoSujeto": _money(base_c),
            "CuotaRepercutida": f"{cuota_c / 100:.2f}",
        }]
        body["CuotaTotal"] = f"{cuota_c / 100:.2f}"
        body["ImporteTotal"] = _money(importe_c)
    return body


def prepare(doc: dict, entidad: dict, factura_nif: str = "", factura_nombre: str = "") -> Optional[dict]:
    """Datos VeriFactu que se guardan con el cobro (o None si la entidad no lo usa).
    Lanza HTTPException si falta algo obligatorio: mejor no cobrar que emitir mal la factura."""
    if doc.get("tipo") != "ingreso" or not entidad.get("verifactu"):
        return None
    if not valid_nif(entidad.get("cif", "")):
        raise HTTPException(status_code=400, detail=f"VeriFactu: falta un CIF válido para «{entidad.get('nombre', '')}» (Ajustes de la caja).")
    exenta = bool(entidad.get("exenta_iva"))
    if not exenta and doc.get("iva") is None:
        raise HTTPException(status_code=400, detail=f"VeriFactu: configura el IVA de «{doc.get('concepto_nombre', '')}» en Ajustes antes de cobrar.")
    nif = norm_nif(factura_nif)
    if nif and not valid_nif(nif):
        raise HTTPException(status_code=400, detail="El NIF del cliente no parece válido.")
    if nif and not factura_nombre.strip():
        raise HTTPException(status_code=400, detail="Escribe el nombre o razón social del cliente para la factura.")
    if not nif and int(doc["importe_c"]) > SIMPLIFICADA_MAX_C:
        raise HTTPException(status_code=400, detail="Más de 400 €: pide el NIF y el nombre del cliente para hacer factura completa.")
    vf = {
        "estado": "pendiente",
        "tipo": "F1" if nif else "F2",
        "exenta": exenta,
        "motivo_exento": "E1" if exenta else None,
        "qr_url": qr_url(entidad["cif"], doc["numero"], doc["fecha"], int(doc["importe_c"])),
        "intentos": 0,
        "creado": now_iso(),
    }
    if nif:
        vf["destinatario"] = {"nif": nif, "nombre": factura_nombre.strip()[:120]}
    return vf


# ------------------------------------------------------------------ BD
def _connect():
    return psycopg.connect(db.database_url, connect_timeout=10)


def _get_doc_sync(mov_id: str, club_id: Optional[str] = None) -> Optional[dict]:
    with _connect() as conn, conn.cursor() as cur:
        q = "select doc from public.app_documents where collection = 'caja_movimientos' and doc->>'id' = %s"
        args: tuple = (mov_id,)
        if club_id:
            q += " and doc->>'club_id' = %s"
            args = (mov_id, club_id)
        cur.execute(q, args)
        r = cur.fetchone()
        return r[0] if r else None


def _patch_vf_sync(mov_id: str, patch: dict) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "update public.app_documents set doc = jsonb_set(doc, '{verifactu}', coalesce(doc->'verifactu', '{}'::jsonb) || %s), "
            "updated_at = now() where collection = 'caja_movimientos' and doc->>'id' = %s",
            (Jsonb(patch), mov_id))


def _token_for_sync(club_id: str) -> tuple[str, str]:
    """(host, token): el del club si lo tiene, si no el de la plataforma."""
    host, token = _env("VERIFACTU_URL"), _env("VERIFACTU_TOKEN")
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select host, token from public.verifactu_cuentas where club_id = %s::uuid", (club_id,))
            r = cur.fetchone()
            if r and r[1]:
                host, token = (r[0] or host), r[1]
    except Exception:
        pass  # tabla aún sin crear
    return host.rstrip("/"), token


def _config_entidad_sync(club_id: str, entidad: str) -> dict:
    from routes import caja
    with _connect() as conn, conn.cursor() as cur:
        cfg = caja._config_sync(cur, club_id)
    return (cfg.get("entidades") or {}).get(entidad or "") or {}


# ------------------------------------------------------------------ envío
def _apply_result(mov_id: str, payload: dict) -> dict:
    """Interpreta la respuesta de Dinvbox (síncrona o del webhook) y la guarda."""
    patch: dict = {"actualizado": now_iso()}
    resp = payload.get("response") if isinstance(payload.get("response"), dict) else None
    data = payload.get("data") if isinstance(payload.get("data"), dict) else None
    if resp and resp.get("status") == "en_proceso":
        patch.update(estado="enviada", job_id=str(resp.get("job_id") or ""), mensaje=resp.get("message") or "")
    elif data and data.get("EstadoEnvio"):
        ok = str(data.get("EstadoEnvio")).lower() == "correcto"
        patch.update(
            estado="correcto" if ok else "rechazada",
            estado_registro=data.get("estado_registro"),
            csv=data.get("CSV"), huella=data.get("Huella"),
            fecha_huella=data.get("FechaHoraHusoGenRegistroHuella"),
            error_codigo=None if ok else str(data.get("ErrorCode") or ""),
            error=None if ok else (data.get("ErrorDescription") or "Rechazada por la AEAT"),
        )
        if payload.get("job_id") is not None:
            patch["job_id"] = str(payload.get("job_id"))
        if data.get("ValidationUrl"):
            patch["qr_url"] = data["ValidationUrl"]
    elif payload.get("error") not in (None, False, 0, "0") or (data and data.get("ErrorDescription")):
        desc = (data or {}).get("ErrorDescription") or payload.get("message") or str(payload.get("error"))
        patch.update(estado="error", error=f"Dinvbox: {desc}"[:300])
        if payload.get("job_id") is not None:
            patch["job_id"] = str(payload.get("job_id"))
    else:
        patch.update(estado="error", error="Respuesta no reconocida de Dinvbox")
    _patch_vf_sync(mov_id, patch)
    return patch


def send_alta_sync(club_id: str, mov_id: str) -> dict:
    import requests
    doc = _get_doc_sync(mov_id, club_id)
    if not doc or not doc.get("verifactu"):
        return {}
    vf = doc["verifactu"]
    if vf.get("estado") in ("enviada", "correcto"):
        return vf
    host, token = _token_for_sync(club_id)
    intentos = int(vf.get("intentos") or 0) + 1
    if not host or not token:
        # No cuenta como intento: en cuanto se configure, el reintento automático lo envía.
        patch = {"estado": "error", "error": "VeriFactu sin configurar (falta la URL o el token de Dinvbox)",
                 "actualizado": now_iso()}
        _patch_vf_sync(mov_id, patch)
        return {**vf, **patch}
    entidad = _config_entidad_sync(club_id, doc.get("entidad"))
    body = build_alta(doc, entidad)
    try:
        r = requests.post(f"{host}/api/verifactu/alta", json=body, timeout=20,
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        try:
            payload = r.json()
        except ValueError:
            payload = {"error": f"HTTP {r.status_code}", "message": r.text[:200]}
        if r.status_code >= 400 and not payload.get("data"):
            payload = {"error": f"HTTP {r.status_code}", "message": payload.get("message") or r.text[:200]}
    except requests.RequestException as exc:
        payload = {"error": "conexion", "message": f"No se pudo conectar con Dinvbox ({type(exc).__name__})"}
    _patch_vf_sync(mov_id, {"intentos": intentos, "enviado": now_iso()})
    patch = _apply_result(mov_id, payload)
    logger.info("VeriFactu %s (%s): %s", doc.get("numero"), mov_id, patch.get("estado"))
    return {**vf, "intentos": intentos, **patch}


async def send_alta(club_id: str, mov_id: str, timeout: float = 12.0) -> Optional[dict]:
    """Envía sin bloquear la caja más de `timeout` segundos (si tarda, sigue en segundo plano)."""
    task = asyncio.ensure_future(asyncio.to_thread(send_alta_sync, club_id, mov_id))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout)
    except asyncio.TimeoutError:
        return None
    except Exception as exc:
        logger.warning("VeriFactu: envío de %s falló: %s", mov_id, exc)
        return None


def retry_pending_sync() -> int:
    """Reintenta los cobros con error de conexión/configuración (no los rechazados por la AEAT)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            select doc->>'club_id', doc->>'id' from public.app_documents
            where collection = 'caja_movimientos' and doc ? 'verifactu'
              and doc->'verifactu'->>'estado' in ('pendiente', 'error')
              and coalesce((doc->'verifactu'->>'intentos')::int, 0) < %s
              and doc->>'estado' = 'valido'
              and (doc->>'created_at')::timestamptz > now() - interval '5 days'
            limit 200""", (MAX_INTENTOS,))
        rows = cur.fetchall()
    for club_id, mov_id in rows:
        try:
            send_alta_sync(club_id, mov_id)
        except Exception as exc:
            logger.warning("VeriFactu: reintento de %s falló: %s", mov_id, exc)
    return len(rows)


async def _retry_loop():
    await asyncio.sleep(90)
    while True:
        try:
            n = await asyncio.to_thread(retry_pending_sync)
            if n:
                logger.info("VeriFactu: %s envíos reintentados", n)
        except Exception as exc:
            logger.warning("VeriFactu: pasada de reintentos falló: %s", exc)
        await asyncio.sleep(600)


def start_background_tasks():
    if not (_env("VERIFACTU_URL") and _env("VERIFACTU_TOKEN")):
        return
    asyncio.get_event_loop().create_task(_retry_loop())


# ------------------------------------------------------------------ rutas
CAJA_ROLES = ("admin", "office")


@router.post("/caja/movimientos/{mov_id}/verifactu/reintentar")
async def retry_one(mov_id: str, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    doc = await asyncio.to_thread(_get_doc_sync, mov_id, club_id)
    if not doc or not doc.get("verifactu"):
        raise HTTPException(status_code=404, detail="Ese cobro no se envía a VeriFactu")
    if doc.get("estado") == "anulado":
        raise HTTPException(status_code=409, detail="El cobro está anulado")
    if doc["verifactu"].get("estado") in ("enviada", "correcto"):
        return doc["verifactu"]
    if doc["verifactu"].get("estado") == "rechazada":
        await asyncio.to_thread(_patch_vf_sync, mov_id, {"estado": "pendiente"})
    res = await asyncio.to_thread(send_alta_sync, club_id, mov_id)
    return res


@router.get("/caja/movimientos/{mov_id}/verifactu/qr")
async def qr_for(mov_id: str, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    doc = await asyncio.to_thread(_get_doc_sync, mov_id, club_id)
    if not doc or not (doc.get("verifactu") or {}).get("qr_url"):
        raise HTTPException(status_code=404, detail="Sin QR")
    url = doc["verifactu"]["qr_url"]
    return {"url": url, "svg": qr_svg(url)}


@router.post("/verifactu/webhook")
async def webhook(request: Request):
    secret = _env("VERIFACTU_WEBHOOK_SECRET")
    if not secret or request.query_params.get("secret") != secret:
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        payload = json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="JSON no válido")
    job_id = payload.get("job_id") or (payload.get("response") or {}).get("job_id")
    ref = (payload.get("data") or {}).get("RefExterna") or payload.get("RefExterna")

    def find():
        with _connect() as conn, conn.cursor() as cur:
            if ref:
                cur.execute("select doc->>'id' from public.app_documents where collection = 'caja_movimientos' "
                            "and doc->>'id' = %s", (str(ref),))
            else:
                cur.execute("select doc->>'id' from public.app_documents where collection = 'caja_movimientos' "
                            "and doc->'verifactu'->>'job_id' = %s", (str(job_id),))
            r = cur.fetchone()
            return r[0] if r else None

    mov_id = await asyncio.to_thread(find) if (job_id or ref) else None
    if not mov_id:
        logger.warning("VeriFactu webhook sin cobro asociado (job %s, ref %s)", job_id, ref)
        return {"ok": True, "asociado": False}
    patch = await asyncio.to_thread(_apply_result, mov_id, payload)
    return {"ok": True, "estado": patch.get("estado")}


class CredencialesIn(BaseModel):
    token: str = Field(default="", max_length=4000)
    host: str = Field(default="", max_length=300)


@router.get("/caja/verifactu/estado")
async def estado(user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)

    def run():
        try:
            with _connect() as conn, conn.cursor() as cur:
                cur.execute("select token is not null and token <> '' from public.verifactu_cuentas where club_id = %s::uuid", (club_id,))
                r = cur.fetchone()
                propio = bool(r and r[0])
        except Exception:
            propio = False
        return propio
    propio = await asyncio.to_thread(run)
    plataforma = bool(_env("VERIFACTU_URL") and _env("VERIFACTU_TOKEN"))
    return {"configurado": propio or plataforma, "origen": "club" if propio else ("plataforma" if plataforma else None),
            "entorno": "pruebas" if _env("VERIFACTU_ENTORNO").lower().startswith("prueba") else "produccion",
            "webhook": bool(_env("VERIFACTU_WEBHOOK_SECRET"))}


@router.put("/caja/verifactu/credenciales")
async def set_credentials(data: CredencialesIn, user=Depends(require_roles("admin"))):
    """Token propio de Dinvbox del club (solo Dirección). Nunca se devuelve."""
    club_id = require_club_context(user)
    host = data.host.strip()
    if host and not host.startswith("https://"):
        raise HTTPException(status_code=400, detail="La URL de Dinvbox debe empezar por https://")

    def run():
        with _connect() as conn, conn.cursor() as cur:
            if not data.token.strip():
                cur.execute("delete from public.verifactu_cuentas where club_id = %s::uuid", (club_id,))
            else:
                cur.execute("insert into public.verifactu_cuentas (club_id, host, token, updated_at) values (%s::uuid, %s, %s, now()) "
                            "on conflict (club_id) do update set host = excluded.host, token = excluded.token, updated_at = now()",
                            (club_id, host or None, data.token.strip()))
    await asyncio.to_thread(run)
    logger.info("VeriFactu: %s actualiza las credenciales del club %s", user.get("email"), club_id)
    return await estado(user)
