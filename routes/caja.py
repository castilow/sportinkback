"""Caja de mostrador del club (Oficina).

Sustituye a la hoja «Control de ventas en mostrador»: cobros y salidas de
dinero, recibos con numeración fija, cierre de caja diario e informes.

Reglas que la hacen fiable:
- Cada escritura va en UNA transacción de Postgres con un bloqueo por club, así
  dos personas cobrando a la vez nunca reciben el mismo número de recibo.
- Los números de recibo son correlativos por serie (SAD, Fundación…) y año, y
  no cambian nunca. Un cobro no se borra: se ANULA y queda en el historial.
- Un día cerrado no admite cobros ni cambios hasta que se reabre.
- Si el cobro incluye prendas, se descuentan del stock en la misma
  transacción; al anular el cobro, vuelven al stock.

Importes en céntimos (enteros) para evitar errores de redondeo.
Permisos: Dirección y Oficina.
"""
import asyncio
import hashlib
import io
import re
import unicodedata
from datetime import date, datetime, timezone
from typing import List, Literal, Optional

import psycopg
from psycopg.types.json import Jsonb
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from postgres_compat import PostgresCompatDB
from deps import db, now_iso, uuid, MAX_UPLOAD_BYTES, require_roles, require_club_context

router = APIRouter()

CAJA_ROLES = ("admin", "office")
MAX_CENTS = 10_000_000  # 100.000 €
PAGOS = ("efectivo", "tarjeta", "bizum", "mixto")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Europe/Madrid")
except Exception:  # sin tzdata: hora UTC
    _TZ = timezone.utc


# ------------------------------------------------------------------ config
# Valores iniciales sacados de la herramienta que usa Oficina. Se pueden
# cambiar desde Ajustes sin tocar código.
DEFAULT_CONFIG = {
    "conceptos": [
        {"key": "indumentaria", "nombre": "Indumentaria", "entidad": "SAD", "iva": 21, "pide_nombre": True,
         "precio": None, "color": "blue", "stock": True,
         "opciones": [{"nombre": "Común", "precio": 335}, {"nombre": "Élite", "precio": 275},
                      {"nombre": "Señ. femenino", "precio": 245}, {"nombre": "Debutantes", "precio": 98},
                      {"nombre": "Otros", "precio": None}]},
        {"key": "abono", "nombre": "Abono", "entidad": "SAD", "iva": 10, "pide_nombre": True, "precio": None, "color": "navy"},
        {"key": "taquilla", "nombre": "Taquilla", "entidad": "SAD", "iva": 21, "pide_nombre": False, "precio": None, "color": "green"},
        {"key": "matricula", "nombre": "Matrícula", "entidad": "Fundación", "iva": None, "pide_nombre": True, "precio": None, "color": "violet"},
        {"key": "cuota", "nombre": "Cuota", "entidad": "Fundación", "iva": None, "pide_nombre": True, "precio": None, "color": "violet"},
        {"key": "alto_rendimiento", "nombre": "Alto Rendimiento", "entidad": "Fundación", "iva": None, "pide_nombre": True, "precio": None, "color": "violet"},
        {"key": "academia_basilio", "nombre": "Academia Basilio", "entidad": "Fundación", "iva": None, "pide_nombre": True, "precio": None, "color": "violet"},
        {"key": "futbol_andando", "nombre": "Fútbol Andando", "entidad": "Fundación", "iva": None, "pide_nombre": True, "precio": None, "color": "violet"},
        {"key": "jugadores_internacionales", "nombre": "Jugadores Internacionales", "entidad": None, "iva": None, "pide_nombre": True, "precio": None, "color": "violet"},
        {"key": "merchandising", "nombre": "Merchandising", "entidad": "SAD", "iva": 21, "pide_nombre": False, "precio": None, "color": "amber", "stock": True},
        {"key": "scientific", "nombre": "Scientific", "entidad": "SAD", "iva": None, "pide_nombre": False, "precio": None, "color": "amber"},
        {"key": "otro", "nombre": "Otro", "entidad": None, "iva": None, "pide_nombre": True, "precio": None, "color": "slate", "libre": True},
    ],
    "entidades": {
        "SAD": {"nombre": "Club de Fútbol Rayo Majadahonda, S.A.D.", "cif": "A-28562445",
                "direccion": "Calle de las Moreras, S/N · 28222 Majadahonda (Madrid)", "serie": "SAD", "exenta_iva": False},
        "Fundación": {"nombre": "Fundación Rayo Majadahonda", "cif": "G88543145",
                      "direccion": "Calle Moreras, S/N · 28222 Majadahonda (Madrid)", "serie": "FUN", "exenta_iva": True},
    },
    "recibo": {"papel": "80", "copias": 2, "pie": "Gracias por confiar en el club."},
    "motivos_salida": ["Cambio para la caja", "Compra de material", "Pago a proveedor", "Devolución a familia", "Ingreso en banco", "Otro"],
}


def _slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")[:40] or "concepto"


def _cents(value) -> int:
    if value is None or value == "":
        return 0
    try:
        c = int(round(float(str(value).replace(",", ".")) * 100))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"Importe no válido: {value}")
    return c


def _eur(c: int) -> float:
    return round((c or 0) / 100, 2)


# ------------------------------------------------------------------ BD
def _require_postgres():
    if not isinstance(db, PostgresCompatDB):
        raise HTTPException(status_code=503, detail="La caja requiere PostgreSQL")


def _run_tx(club_id: str, operation, with_stock_lock: bool = False):
    _require_postgres()
    with psycopg.connect(db.database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"sportink:caja:{club_id}",))
            if with_stock_lock:
                # Mismo bloqueo que usa el módulo de stock: orden fijo caja → stock.
                cur.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"sportink:stock:{club_id}",))
            return operation(cur)


async def _write(club_id: str, operation, with_stock_lock: bool = False):
    _require_postgres()
    return await asyncio.to_thread(_run_tx, club_id, operation, with_stock_lock)


def _run_read(sql: str, params: tuple):
    _require_postgres()
    with psycopg.connect(db.database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]


async def _read(sql: str, params: tuple):
    return await asyncio.to_thread(_run_read, sql, params)


def _insert(cur, collection: str, doc: dict):
    cur.execute("insert into public.app_documents (collection, doc) values (%s, %s)", (collection, Jsonb(doc)))


def _patch(cur, collection: str, club_id: str, doc_id: str, changes: dict):
    cur.execute("update public.app_documents set doc = doc || %s, updated_at = now() "
                "where collection = %s and doc->>'club_id' = %s and doc->>'id' = %s",
                (Jsonb(changes), collection, club_id, doc_id))
    if cur.rowcount != 1:
        raise HTTPException(status_code=409, detail="El registro cambió durante la operación; vuelve a intentarlo")


def _select(cur, collection: str, club_id: str, where: str = "", params: tuple = (), lock: bool = False):
    cur.execute(f"select doc from public.app_documents where collection = %s and doc->>'club_id' = %s {where}"
                + (" for update" if lock else ""), (collection, club_id, *params))
    return [r[0] for r in cur.fetchall()]


def _config_sync(cur, club_id: str) -> dict:
    rows = _select(cur, "caja_config", club_id)
    cfg = rows[0] if rows else None
    return _merge_config(cfg)


def _merge_config(cfg: Optional[dict]) -> dict:
    import copy
    out = copy.deepcopy(DEFAULT_CONFIG)
    if cfg:
        for k in ("conceptos", "entidades", "recibo", "motivos_salida"):
            if cfg.get(k):
                out[k] = cfg[k]
    return out


async def _config(club_id: str) -> dict:
    rows = await _read("select doc from public.app_documents where collection = %s and doc->>'club_id' = %s",
                       ("caja_config", club_id))
    return _merge_config(rows[0] if rows else None)


# ------------------------------------------------------------------ cálculos
def _iva_for(cfg: dict, concepto_key: str, entidad: Optional[str]) -> Optional[int]:
    ent = (cfg.get("entidades") or {}).get(entidad or "") or {}
    if ent.get("exenta_iva"):
        return 0
    c = next((c for c in cfg["conceptos"] if c.get("key") == concepto_key), None)
    return c.get("iva") if c else None


def _serie_for(cfg: dict, tipo: str, entidad: Optional[str]) -> str:
    if tipo == "gasto":
        return "SAL"
    ent = (cfg.get("entidades") or {}).get(entidad or "")
    return (ent or {}).get("serie") or "GEN"


def _next_number(cur, club_id: str, serie: str, year: int) -> tuple:
    cur.execute(
        "select coalesce(max((doc->>'correlativo')::int), 0) from public.app_documents "
        "where collection = 'caja_movimientos' and doc->>'club_id' = %s and doc->>'serie' = %s and doc->>'ejercicio' = %s",
        (club_id, serie, str(year)))
    n = int(cur.fetchone()[0] or 0) + 1
    return n, f"{serie}-{year}-{n:05d}"


def _closed(cur, club_id: str, fecha: str) -> Optional[dict]:
    rows = _select(cur, "caja_cierres", club_id, "and doc->>'fecha' = %s and coalesce(doc->>'estado','cerrado') = 'cerrado'", (fecha,))
    return rows[0] if rows else None


def _check_open(cur, club_id: str, fecha: str):
    c = _closed(cur, club_id, fecha)
    if c:
        raise HTTPException(status_code=409, detail=f"La caja del {fecha} está cerrada. Reábrela para hacer cambios.")


def _split_pago(pago: str, importe_c: int, efectivo_c: int, tarjeta_c: int, bizum_c: int) -> tuple:
    if pago == "efectivo":
        return importe_c, 0, 0
    if pago == "tarjeta":
        return 0, importe_c, 0
    if pago == "bizum":
        return 0, 0, importe_c
    if min(efectivo_c, tarjeta_c, bizum_c) < 0 or efectivo_c + tarjeta_c + bizum_c != importe_c:
        raise HTTPException(status_code=400, detail=f"En pago mixto, efectivo + tarjeta + Bizum debe sumar {_eur(importe_c):.2f} €")
    if sum(1 for x in (efectivo_c, tarjeta_c, bizum_c) if x > 0) < 2:
        raise HTTPException(status_code=400, detail="Un pago mixto necesita al menos dos formas de pago")
    return efectivo_c, tarjeta_c, bizum_c


def _public(m: dict) -> dict:
    m = dict(m)
    for k in ("importe", "efectivo", "tarjeta", "bizum", "entregado", "cambio", "base", "cuota_iva"):
        if f"{k}_c" in m:
            m[k] = _eur(m.get(f"{k}_c"))
    return m


def _totales(movs: list, fondo_c: int = 0) -> dict:
    t = {"ingresos_c": 0, "gastos_c": 0, "efectivo_c": 0, "tarjeta_c": 0, "bizum_c": 0,
         "gastos_efectivo_c": 0, "num_cobros": 0, "num_salidas": 0, "num_anulados": 0}
    por_entidad: dict = {}
    por_concepto: dict = {}
    for m in movs:
        if m.get("estado") == "anulado":
            t["num_anulados"] += 1
            continue
        imp = int(m.get("importe_c") or 0)
        if m.get("tipo") == "gasto":
            t["gastos_c"] += imp
            t["num_salidas"] += 1
            t["gastos_efectivo_c"] += int(m.get("efectivo_c") or 0)
            continue
        t["num_cobros"] += 1
        t["ingresos_c"] += imp
        t["efectivo_c"] += int(m.get("efectivo_c") or 0)
        t["tarjeta_c"] += int(m.get("tarjeta_c") or 0)
        t["bizum_c"] += int(m.get("bizum_c") or 0)
        ent = m.get("entidad") or "Sin entidad"
        por_entidad[ent] = por_entidad.get(ent, 0) + imp
        nombre = m.get("concepto_nombre") or m.get("concepto") or "Otro"
        por_concepto[nombre] = por_concepto.get(nombre, 0) + imp
    cajon = fondo_c + t["efectivo_c"] - t["gastos_efectivo_c"]
    out = {k[:-2] if k.endswith("_c") else k: (_eur(v) if k.endswith("_c") else v) for k, v in t.items()}
    out["fondo_inicial"] = _eur(fondo_c)
    out["esperado_en_cajon"] = _eur(cajon)
    out["neto"] = _eur(t["ingresos_c"] - t["gastos_c"])
    out["por_entidad"] = [{"nombre": k, "importe": _eur(v)} for k, v in sorted(por_entidad.items(), key=lambda x: -x[1])]
    out["por_concepto"] = [{"nombre": k, "importe": _eur(v)} for k, v in sorted(por_concepto.items(), key=lambda x: -x[1])]
    return out


# ------------------------------------------------------------------ modelos
class PrendaIn(BaseModel):
    ean: str = Field(default="", max_length=20)
    product_id: str = Field(default="", max_length=60)
    qty: int = Field(default=1, ge=1, le=500)


class MovimientoIn(BaseModel):
    tipo: Literal["ingreso", "gasto"] = "ingreso"
    fecha: str
    concepto: str = Field(default="", max_length=60)        # key del concepto (ingreso)
    opcion: str = Field(default="", max_length=60)          # p. ej. «Común» en Indumentaria
    descripcion: str = Field(default="", max_length=160)    # texto libre (Otro / motivo de salida)
    nombre: str = Field(default="", max_length=160)
    player_id: str = Field(default="", max_length=60)
    player_team: str = Field(default="", max_length=120)
    entidad: Optional[str] = Field(default=None, max_length=40)
    importe: float = Field(gt=0, le=100000)
    pago: Literal["efectivo", "tarjeta", "bizum", "mixto"] = "efectivo"
    efectivo: float = Field(default=0, ge=0, le=100000)
    tarjeta: float = Field(default=0, ge=0, le=100000)
    bizum: float = Field(default=0, ge=0, le=100000)
    entregado: Optional[float] = Field(default=None, ge=0, le=100000)  # lo que da la familia en efectivo
    nota: str = Field(default="", max_length=500)
    prendas: List[PrendaIn] = Field(default_factory=list, max_length=100)

    @field_validator("fecha")
    @classmethod
    def _v_fecha(cls, v):
        if not _DATE_RE.match(v or ""):
            raise ValueError("Fecha no válida (AAAA-MM-DD)")
        date.fromisoformat(v)
        return v


class MovimientoUpdateIn(BaseModel):
    nombre: Optional[str] = Field(default=None, max_length=160)
    player_id: Optional[str] = Field(default=None, max_length=60)
    player_team: Optional[str] = Field(default=None, max_length=120)
    nota: Optional[str] = Field(default=None, max_length=500)
    descripcion: Optional[str] = Field(default=None, max_length=160)
    importe: Optional[float] = Field(default=None, gt=0, le=100000)
    pago: Optional[Literal["efectivo", "tarjeta", "bizum", "mixto"]] = None
    efectivo: Optional[float] = Field(default=None, ge=0, le=100000)
    tarjeta: Optional[float] = Field(default=None, ge=0, le=100000)
    bizum: Optional[float] = Field(default=None, ge=0, le=100000)


class AnularIn(BaseModel):
    motivo: str = Field(min_length=3, max_length=300)


class CierreIn(BaseModel):
    fecha: str
    fondo_inicial: float = Field(default=0, ge=0, le=100000)
    efectivo_contado: float = Field(ge=0, le=100000)
    nota: str = Field(default="", max_length=500)

    @field_validator("fecha")
    @classmethod
    def _v_fecha(cls, v):
        if not _DATE_RE.match(v or ""):
            raise ValueError("Fecha no válida (AAAA-MM-DD)")
        date.fromisoformat(v)
        return v


class ConceptoCfg(BaseModel):
    key: str = Field(default="", max_length=40)
    nombre: str = Field(min_length=1, max_length=60)
    entidad: Optional[str] = Field(default=None, max_length=40)
    iva: Optional[int] = Field(default=None, ge=0, le=30)
    pide_nombre: bool = True
    precio: Optional[float] = Field(default=None, ge=0, le=100000)
    color: str = Field(default="slate", max_length=20)
    stock: bool = False
    libre: bool = False
    activo: bool = True
    opciones: List[dict] = Field(default_factory=list, max_length=20)


class EntidadCfg(BaseModel):
    nombre: str = Field(min_length=1, max_length=160)
    cif: str = Field(default="", max_length=30)
    direccion: str = Field(default="", max_length=200)
    serie: str = Field(min_length=1, max_length=6)
    exenta_iva: bool = False

    @field_validator("serie")
    @classmethod
    def _v_serie(cls, v):
        v = v.strip().upper()
        if not re.match(r"^[A-Z0-9]{1,6}$", v) or v in ("SAL", "HIST"):
            raise ValueError("Serie: letras o números, máx. 6, distinta de SAL e HIST")
        return v


class ConfigIn(BaseModel):
    conceptos: List[ConceptoCfg] = Field(min_length=1, max_length=40)
    entidades: dict
    recibo: dict = Field(default_factory=dict)
    motivos_salida: List[str] = Field(default_factory=list, max_length=30)


# ------------------------------------------------------------------ rutas: config
@router.get("/caja/config")
async def get_config(user=Depends(require_roles(*CAJA_ROLES))):
    return await _config(require_club_context(user))


@router.put("/caja/config")
async def put_config(data: ConfigIn, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    entidades = {}
    for name, raw in (data.entidades or {}).items():
        if not isinstance(raw, dict):
            continue
        e = EntidadCfg(**raw)
        entidades[str(name)[:40]] = e.model_dump()
    if not entidades:
        raise HTTPException(status_code=400, detail="Debe haber al menos una entidad")
    series = [e["serie"] for e in entidades.values()]
    if len(series) != len(set(series)):
        raise HTTPException(status_code=400, detail="Cada entidad necesita una serie de recibos distinta")
    conceptos, seen = [], set()
    for c in data.conceptos:
        d = c.model_dump()
        d["key"] = _slug(d["key"] or d["nombre"])
        if d["key"] in seen:
            raise HTTPException(status_code=400, detail=f"Concepto repetido: {d['nombre']}")
        if d["entidad"] and d["entidad"] not in entidades:
            raise HTTPException(status_code=400, detail=f"El concepto {d['nombre']} usa una entidad que no existe")
        seen.add(d["key"])
        d["opciones"] = [{"nombre": str(o.get("nombre", ""))[:60],
                          "precio": (round(float(o["precio"]), 2) if o.get("precio") not in (None, "") else None)}
                         for o in d["opciones"] if str(o.get("nombre", "")).strip()]
        conceptos.append(d)
    recibo = {
        "papel": data.recibo.get("papel") if data.recibo.get("papel") in ("80", "58", "a4") else "80",
        "copias": 2 if int(data.recibo.get("copias") or 1) >= 2 else 1,
        "pie": str(data.recibo.get("pie") or "")[:200],
    }
    motivos = [str(m)[:60] for m in data.motivos_salida if str(m).strip()]

    def apply(cur):
        rows = _select(cur, "caja_config", club_id, lock=True)
        doc = {"conceptos": conceptos, "entidades": entidades, "recibo": recibo,
               "motivos_salida": motivos or DEFAULT_CONFIG["motivos_salida"],
               "updated_at": now_iso(), "updated_by": user.get("name", "")}
        if rows:
            _patch(cur, "caja_config", club_id, rows[0]["id"], doc)
            return _merge_config({**rows[0], **doc})
        doc.update({"id": str(uuid.uuid4()), "club_id": club_id})
        _insert(cur, "caja_config", doc)
        return _merge_config(doc)

    return await _write(club_id, apply)


# ------------------------------------------------------------------ rutas: movimientos
def _apply_stock_out(cur, club_id: str, prendas: list, numero: str, user: dict, nombre: str, player_id: str, reverse=False):
    """Descuenta (o devuelve, si reverse) prendas del stock. Requiere el bloqueo de stock."""
    from routes.stock import _select_products, _patch_product, _clean_ean
    products = _select_products(cur, club_id)
    by_id = {p["id"]: p for p in products}
    by_ean = {p.get("ean"): p for p in products}
    lines: dict = {}
    missing = []
    for it in prendas:
        p = by_id.get(it.get("product_id")) if it.get("product_id") else by_ean.get(_clean_ean(it.get("ean")))
        if not p:
            missing.append(it.get("ean") or it.get("product_id"))
            continue
        lines[p["id"]] = lines.get(p["id"], 0) + int(it.get("qty") or 1)
    if missing:
        raise HTTPException(status_code=404, detail=f"Prendas que no están en el catálogo: {', '.join(missing[:10])}")
    if not reverse:
        short = [f"{by_id[pid].get('name')} {by_id[pid].get('size', '')} (hay {int(by_id[pid].get('stock') or 0)})"
                 for pid, q in lines.items() if q > int(by_id[pid].get("stock") or 0)]
        if short:
            raise HTTPException(status_code=409, detail="No hay stock suficiente: " + "; ".join(short[:10]))
    items = []
    for pid, q in lines.items():
        p = by_id[pid]
        before = int(p.get("stock") or 0)
        after = before + q if reverse else before - q
        _patch_product(cur, club_id, pid, {"stock": after, "updated_at": now_iso()})
        items.append({"product_id": pid, "ean": p.get("ean"), "name": p.get("name"), "size": p.get("size", ""),
                      "qty": q, "stock_before": before, "stock_after": after})
    mov = {
        "id": str(uuid.uuid4()), "club_id": club_id, "type": "entrada" if reverse else "salida",
        "reason": "Anulación de cobro" if reverse else "Venta en tienda",
        "player_id": player_id or "", "player_name": nombre or "", "team": "",
        "note": f"{'Anulado el ' if reverse else ''}recibo {numero}", "items": items,
        "units": sum(i["qty"] for i in items), "user_id": user["id"], "user_name": user.get("name", ""),
        "user_role": user.get("role", ""), "created_at": now_iso(), "caja_numero": numero,
    }
    _insert(cur, "stock_movements", mov)
    return mov, items


@router.post("/caja/movimientos")
async def create_movimiento(data: MovimientoIn, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    importe_c = _cents(data.importe)
    if importe_c <= 0 or importe_c > MAX_CENTS:
        raise HTTPException(status_code=400, detail="Importe no válido")
    ef, ta, bi = _split_pago(data.pago, importe_c, _cents(data.efectivo), _cents(data.tarjeta), _cents(data.bizum))
    entregado_c = _cents(data.entregado) if data.entregado is not None else None
    if entregado_c is not None and ef > 0 and entregado_c < ef:
        raise HTTPException(status_code=400, detail=f"Lo entregado en efectivo es menos de {_eur(ef):.2f} €")
    if data.tipo == "gasto" and data.prendas:
        raise HTTPException(status_code=400, detail="Una salida de dinero no lleva prendas")
    now_local = datetime.now(_TZ)

    def apply(cur):
        _check_open(cur, club_id, data.fecha)
        cfg = _config_sync(cur, club_id)
        concepto_nombre, iva, entidad = "", None, data.entidad
        if data.tipo == "ingreso":
            c = next((c for c in cfg["conceptos"] if c.get("key") == data.concepto and c.get("activo", True)), None)
            if not c:
                raise HTTPException(status_code=400, detail="Elige qué ha pagado (concepto)")
            if c.get("libre") and not data.descripcion.strip():
                raise HTTPException(status_code=400, detail="Escribe el concepto")
            if c.get("pide_nombre", True) and not data.nombre.strip():
                raise HTTPException(status_code=400, detail="Escribe el nombre del jugador o la familia")
            entidad = entidad or c.get("entidad")
            if entidad and entidad not in cfg["entidades"]:
                raise HTTPException(status_code=400, detail="Entidad no válida")
            if not entidad:
                raise HTTPException(status_code=400, detail="Elige si es SAD o Fundación")
            concepto_nombre = c["nombre"] + (f" · {data.opcion}" if data.opcion else "") \
                + (f" · {data.descripcion.strip()}" if c.get("libre") and data.descripcion.strip() else "")
            iva = _iva_for(cfg, c["key"], entidad)
        else:
            if not data.descripcion.strip():
                raise HTTPException(status_code=400, detail="Escribe en qué se ha gastado")
            if entidad and entidad not in cfg["entidades"]:
                raise HTTPException(status_code=400, detail="Entidad no válida")
            concepto_nombre = data.descripcion.strip()
        serie = _serie_for(cfg, data.tipo, entidad)
        year = int(data.fecha[:4])
        n, numero = _next_number(cur, club_id, serie, year)
        base_c = cuota_c = None
        if iva is not None:
            base_c = int(round(importe_c / (1 + iva / 100)))
            cuota_c = importe_c - base_c
        doc = {
            "id": str(uuid.uuid4()), "club_id": club_id, "numero": numero, "serie": serie,
            "ejercicio": str(year), "correlativo": n, "fecha": data.fecha,
            "hora": now_local.strftime("%H:%M"), "tipo": data.tipo,
            "concepto": data.concepto if data.tipo == "ingreso" else "salida",
            "concepto_nombre": concepto_nombre, "opcion": data.opcion, "descripcion": data.descripcion.strip(),
            "nombre": data.nombre.strip(), "player_id": data.player_id, "player_team": data.player_team.strip(),
            "entidad": entidad, "pago": data.pago, "importe_c": importe_c, "efectivo_c": ef,
            "tarjeta_c": ta, "bizum_c": bi, "iva": iva, "base_c": base_c, "cuota_iva_c": cuota_c,
            "nota": data.nota.strip(), "estado": "valido", "cambios": [],
            "user_id": user["id"], "user_name": user.get("name", ""), "origen": "caja",
            "created_at": now_iso(), "updated_at": now_iso(),
        }
        if entregado_c is not None and ef > 0:
            doc["entregado_c"] = entregado_c
            doc["cambio_c"] = entregado_c - ef
        if data.prendas:
            mov, items = _apply_stock_out(cur, club_id, [p.model_dump() for p in data.prendas], numero, user,
                                          doc["nombre"], data.player_id)
            doc["stock_movement_id"] = mov["id"]
            doc["prendas"] = [{"product_id": i["product_id"], "ean": i["ean"], "name": i["name"],
                               "size": i["size"], "qty": i["qty"]} for i in items]
        _insert(cur, "caja_movimientos", doc)
        return _public(doc)

    return await _write(club_id, apply, with_stock_lock=bool(data.prendas))


@router.get("/caja/movimientos")
async def list_movimientos(desde: Optional[str] = None, hasta: Optional[str] = None, q: Optional[str] = None,
                           player_id: Optional[str] = None, tipo: Optional[str] = None,
                           entidad: Optional[str] = None, estado: Optional[str] = None,
                           user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    hoy = datetime.now(_TZ).date().isoformat()
    desde = desde if desde and _DATE_RE.match(desde) else hoy
    hasta = hasta if hasta and _DATE_RE.match(hasta) else desde
    if desde > hasta:
        desde, hasta = hasta, desde
    rows = await _read(
        "select doc from public.app_documents where collection = 'caja_movimientos' and doc->>'club_id' = %s "
        "and doc->>'fecha' between %s and %s", (club_id, desde, hasta))
    if player_id:
        rows = [r for r in rows if r.get("player_id") == player_id]
    if tipo in ("ingreso", "gasto"):
        rows = [r for r in rows if r.get("tipo") == tipo]
    if entidad:
        rows = [r for r in rows if (r.get("entidad") or "") == entidad]
    if estado in ("valido", "anulado"):
        rows = [r for r in rows if r.get("estado") == estado]
    if q:
        needle = _slug(q).replace("_", " ")
        rows = [r for r in rows if needle in _slug(" ".join(str(r.get(k, "")) for k in
                                                            ("nombre", "concepto_nombre", "numero", "nota"))).replace("_", " ")]
    rows.sort(key=lambda r: (r.get("fecha", ""), r.get("created_at", "")), reverse=True)
    return {"desde": desde, "hasta": hasta, "movimientos": [_public(r) for r in rows[:5000]],
            "totales": _totales(rows)}


@router.get("/caja/dia")
async def caja_dia(fecha: Optional[str] = None, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    fecha = fecha if fecha and _DATE_RE.match(fecha) else datetime.now(_TZ).date().isoformat()
    rows = await _read("select doc from public.app_documents where collection = 'caja_movimientos' "
                       "and doc->>'club_id' = %s and doc->>'fecha' = %s", (club_id, fecha))
    cierres = await _read("select doc from public.app_documents where collection = 'caja_cierres' "
                          "and doc->>'club_id' = %s and doc->>'fecha' = %s", (club_id, fecha))
    cierre = next((c for c in cierres if c.get("estado", "cerrado") == "cerrado"), None)
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    fondo_c = int((cierre or {}).get("fondo_inicial_c") or 0)
    return {"fecha": fecha, "movimientos": [_public(r) for r in rows], "totales": _totales(rows, fondo_c),
            "cierre": _public_cierre(cierre) if cierre else None}


@router.put("/caja/movimientos/{mov_id}")
async def update_movimiento(mov_id: str, data: MovimientoUpdateIn, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)

    def apply(cur):
        rows = _select(cur, "caja_movimientos", club_id, "and doc->>'id' = %s", (mov_id,), lock=True)
        if not rows:
            raise HTTPException(status_code=404, detail="Movimiento no encontrado")
        m = rows[0]
        if m.get("estado") == "anulado":
            raise HTTPException(status_code=409, detail="Un movimiento anulado no se puede editar")
        _check_open(cur, club_id, m["fecha"])
        changes, log = {}, {}
        for k in ("nombre", "player_id", "player_team", "nota", "descripcion"):
            v = getattr(data, k)
            if v is not None and v.strip() != (m.get(k) or ""):
                log[k] = [m.get(k) or "", v.strip()]
                changes[k] = v.strip()
        importe_c = _cents(data.importe) if data.importe is not None else int(m["importe_c"])
        pago = data.pago or m.get("pago", "efectivo")
        if data.importe is not None or data.pago is not None or any(
                getattr(data, k) is not None for k in ("efectivo", "tarjeta", "bizum")):
            ef, ta, bi = _split_pago(
                pago, importe_c,
                _cents(data.efectivo) if data.efectivo is not None else int(m.get("efectivo_c") or 0),
                _cents(data.tarjeta) if data.tarjeta is not None else int(m.get("tarjeta_c") or 0),
                _cents(data.bizum) if data.bizum is not None else int(m.get("bizum_c") or 0))
            new = {"importe_c": importe_c, "pago": pago, "efectivo_c": ef, "tarjeta_c": ta, "bizum_c": bi}
            for k, v in new.items():
                if m.get(k) != v:
                    log[k] = [m.get(k), v]
                    changes[k] = v
            if "importe_c" in changes and m.get("iva") is not None:
                base_c = int(round(importe_c / (1 + m["iva"] / 100)))
                changes["base_c"], changes["cuota_iva_c"] = base_c, importe_c - base_c
            if "efectivo_c" in changes:
                changes["entregado_c"] = None
                changes["cambio_c"] = None
        if not changes:
            return _public(m)
        changes["cambios"] = (m.get("cambios") or []) + [{"at": now_iso(), "por": user.get("name", ""), "campos": log}]
        changes["updated_at"] = now_iso()
        _patch(cur, "caja_movimientos", club_id, mov_id, changes)
        m.update(changes)
        return _public(m)

    return await _write(club_id, apply)


@router.post("/caja/movimientos/{mov_id}/anular")
async def anular_movimiento(mov_id: str, data: AnularIn, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)

    def apply(cur):
        rows = _select(cur, "caja_movimientos", club_id, "and doc->>'id' = %s", (mov_id,), lock=True)
        if not rows:
            raise HTTPException(status_code=404, detail="Movimiento no encontrado")
        m = rows[0]
        if m.get("estado") == "anulado":
            raise HTTPException(status_code=409, detail="Ya estaba anulado")
        _check_open(cur, club_id, m["fecha"])
        changes = {"estado": "anulado", "anulacion": {"motivo": data.motivo.strip(), "por": user.get("name", ""),
                                                       "at": now_iso()}, "updated_at": now_iso()}
        if m.get("prendas"):
            mov, _ = _apply_stock_out(cur, club_id, m["prendas"], m["numero"], user, m.get("nombre", ""),
                                      m.get("player_id", ""), reverse=True)
            changes["stock_return_id"] = mov["id"]
        _patch(cur, "caja_movimientos", club_id, mov_id, changes)
        m.update(changes)
        return _public(m)

    # Toma el bloqueo de stock siempre: no sabemos si lleva prendas hasta leerlo.
    return await _write(club_id, apply, with_stock_lock=True)


# ------------------------------------------------------------------ rutas: cierre
def _public_cierre(c: dict) -> dict:
    c = dict(c)
    for k in ("fondo_inicial", "efectivo_contado", "esperado", "descuadre"):
        if f"{k}_c" in c:
            c[k] = _eur(c[f"{k}_c"])
    return c


@router.post("/caja/cierres")
async def cerrar_caja(data: CierreIn, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    fondo_c, contado_c = _cents(data.fondo_inicial), _cents(data.efectivo_contado)

    def apply(cur):
        if _closed(cur, club_id, data.fecha):
            raise HTTPException(status_code=409, detail="Esa caja ya está cerrada")
        movs = _select(cur, "caja_movimientos", club_id, "and doc->>'fecha' = %s", (data.fecha,))
        tot = _totales(movs, fondo_c)
        esperado_c = _cents(tot["esperado_en_cajon"])
        doc = {
            "id": str(uuid.uuid4()), "club_id": club_id, "fecha": data.fecha, "estado": "cerrado",
            "fondo_inicial_c": fondo_c, "efectivo_contado_c": contado_c, "esperado_c": esperado_c,
            "descuadre_c": contado_c - esperado_c, "totales": tot, "nota": data.nota.strip(),
            "user_id": user["id"], "user_name": user.get("name", ""), "created_at": now_iso(),
        }
        _insert(cur, "caja_cierres", doc)
        return _public_cierre(doc)

    return await _write(club_id, apply)


@router.post("/caja/cierres/{fecha}/reabrir")
async def reabrir_caja(fecha: str, data: AnularIn, user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)

    def apply(cur):
        c = _closed(cur, club_id, fecha)
        if not c:
            raise HTTPException(status_code=404, detail="Esa caja no está cerrada")
        _patch(cur, "caja_cierres", club_id, c["id"], {"estado": "reabierto", "reapertura": {
            "motivo": data.motivo.strip(), "por": user.get("name", ""), "at": now_iso()}})
        return {"ok": True}

    return await _write(club_id, apply)


@router.get("/caja/cierres")
async def list_cierres(desde: Optional[str] = None, hasta: Optional[str] = None,
                       user=Depends(require_roles(*CAJA_ROLES))):
    club_id = require_club_context(user)
    hasta = hasta if hasta and _DATE_RE.match(hasta) else datetime.now(_TZ).date().isoformat()
    desde = desde if desde and _DATE_RE.match(desde) else f"{hasta[:4]}-01-01"
    rows = await _read("select doc from public.app_documents where collection = 'caja_cierres' "
                       "and doc->>'club_id' = %s and doc->>'fecha' between %s and %s", (club_id, desde, hasta))
    rows.sort(key=lambda r: (r.get("fecha", ""), r.get("created_at", "")), reverse=True)
    return [_public_cierre(r) for r in rows]


# ------------------------------------------------------------------ export / import
@router.get("/caja/export.xlsx")
async def export_xlsx(desde: Optional[str] = None, hasta: Optional[str] = None,
                      user=Depends(require_roles(*CAJA_ROLES))):
    data = await list_movimientos(desde=desde, hasta=hasta, user=user)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Movimientos"
    head = ["Fecha", "Hora", "Nº recibo", "Tipo", "Entidad", "Concepto", "Nombre", "Equipo", "Importe (€)",
            "Pago", "Efectivo (€)", "Tarjeta (€)", "Bizum (€)", "% IVA", "Base imponible (€)", "Cuota IVA (€)",
            "Estado", "Motivo anulación", "Nota", "Apuntado por"]
    ws.append(head)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="0B2A55")
        c.alignment = Alignment(vertical="center")
    for m in sorted(data["movimientos"], key=lambda r: (r.get("fecha", ""), r.get("created_at", ""))):
        signo = -1 if m.get("tipo") == "gasto" else 1
        ws.append([
            m.get("fecha"), m.get("hora"), m.get("numero"), "Salida" if m.get("tipo") == "gasto" else "Cobro",
            m.get("entidad") or "", m.get("concepto_nombre"), m.get("nombre"), m.get("player_team", ""),
            signo * m.get("importe", 0), (m.get("pago") or "").capitalize(), m.get("efectivo", 0),
            m.get("tarjeta", 0), m.get("bizum", 0), m.get("iva"), m.get("base"), m.get("cuota_iva"),
            "Anulado" if m.get("estado") == "anulado" else "Válido",
            (m.get("anulacion") or {}).get("motivo", ""), m.get("nota", ""), m.get("user_name", ""),
        ])
    for col, w in zip("ABCDEFGHIJKLMNOPQRST", [11, 7, 16, 8, 11, 30, 26, 14, 11, 9, 11, 11, 10, 7, 14, 12, 9, 22, 26, 16]):
        ws.column_dimensions[col].width = w
    for row in ws.iter_rows(min_row=2):
        for idx in (8, 10, 11, 12, 14, 15):
            row[idx].number_format = '#,##0.00 €'
    ws.freeze_panes = "A2"
    rs = wb.create_sheet("Resumen")
    t = data["totales"]
    rs.append(["Periodo", f"{data['desde']} a {data['hasta']}"])
    for k, label in (("ingresos", "Cobros"), ("gastos", "Salidas de caja"), ("neto", "Neto"),
                     ("efectivo", "Cobrado en efectivo"), ("tarjeta", "Cobrado con tarjeta"), ("bizum", "Cobrado por Bizum")):
        rs.append([label, t[k]])
    rs.append([])
    rs.append(["Por entidad", ""])
    for e in t["por_entidad"]:
        rs.append([e["nombre"], e["importe"]])
    rs.append([])
    rs.append(["Por concepto", ""])
    for e in t["por_concepto"]:
        rs.append([e["nombre"], e["importe"]])
    rs.column_dimensions["A"].width = 30
    rs.column_dimensions["B"].width = 22
    for row in rs.iter_rows():
        if isinstance(row[1].value, (int, float)):
            row[1].number_format = '#,##0.00 €'
        if row[0].value in ("Por entidad", "Por concepto", "Periodo"):
            row[0].font = Font(bold=True)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"caja_{data['desde']}_{data['hasta']}.xlsx"
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


_IMPORT_ALIASES = {
    "fecha": ("fecha",), "origen": ("origen",), "entidad": ("entidad",), "concepto": ("concepto",),
    "nombre": ("nombre",), "importe": ("importe", "total"), "pago": ("pago",), "tipo": ("tipo",),
    "nota": ("nota", "observaciones"), "efectivo": ("cobrado en efectivo", "efectivo"),
    "tarjeta": ("cobrado en tarjeta", "tarjeta"), "id": ("id",), "iva": ("% iva", "iva"),
}


def _parse_fecha(v) -> Optional[str]:
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, (datetime, date)):
        return (v.date() if isinstance(v, datetime) else v).isoformat()
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


@router.post("/caja/import")
async def import_historial(file: UploadFile = File(...), user=Depends(require_roles(*CAJA_ROLES))):
    """Importa el historial de la hoja de Google de la caja anterior (pestaña
    «Movimientos» descargada como Excel o CSV). No duplica: cada fila se
    reconoce por su ID (o por sus datos si no tiene)."""
    club_id = require_club_context(user)
    _require_postgres()
    max_bytes = min(MAX_UPLOAD_BYTES, 8 * 1024 * 1024)
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="El archivo es demasiado grande (máx. 8 MB)")
    try:
        import pandas as pd
        if (file.filename or "").lower().endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=object, keep_default_na=False)
        else:
            xl = pd.ExcelFile(io.BytesIO(content))
            sheet = next((s for s in xl.sheet_names if s.strip().lower() == "movimientos"), xl.sheet_names[0])
            df = pd.read_excel(xl, sheet_name=sheet, dtype=object, keep_default_na=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")
    cols = {}
    for i, h in enumerate(df.columns):
        key = str(h or "").strip().lower()
        for f, al in _IMPORT_ALIASES.items():
            if f not in cols and key in al:
                cols[f] = i
    if not {"fecha", "importe"} <= cols.keys():
        raise HTTPException(status_code=400, detail="Faltan columnas: hacen falta al menos «Fecha» e «Importe»")
    if len(df) > 20000:
        raise HTTPException(status_code=400, detail="Máximo 20.000 filas por archivo")

    def cell(values, f):
        i = cols.get(f)
        v = None if i is None else values[i]
        return "" if v is None else v

    parsed, errors = [], []
    for n, row in enumerate(df.itertuples(index=False), start=2):
        values = list(row)
        fecha = _parse_fecha(cell(values, "fecha"))
        if not fecha:
            if any(str(v).strip() for v in values):
                errors.append({"row": n, "error": "Fecha no válida"})
            continue
        try:
            total_c = _cents(cell(values, "importe"))
        except HTTPException:
            errors.append({"row": n, "error": "Importe no válido"})
            continue
        if total_c == 0:
            continue
        tipo_raw = str(cell(values, "tipo")).strip().lower()
        tipo = "gasto" if tipo_raw.startswith("gasto") or total_c < 0 else "ingreso"
        importe_c = abs(total_c)
        pago_raw = _slug(str(cell(values, "pago")))
        pago = "mixto" if pago_raw.startswith("mixto") else "tarjeta" if pago_raw.startswith("tarjeta") \
            else "bizum" if pago_raw.startswith("bizum") else "efectivo"
        ef_c = abs(_cents(cell(values, "efectivo"))) if str(cell(values, "efectivo")).strip() else None
        ta_c = abs(_cents(cell(values, "tarjeta"))) if str(cell(values, "tarjeta")).strip() else None
        if pago == "mixto" and ef_c is not None and ta_c is not None:
            ef, ta = ef_c, ta_c
        else:
            ef, ta = (importe_c, 0) if pago == "efectivo" else (0, importe_c) if pago == "tarjeta" else (0, 0)
        bi = importe_c if pago == "bizum" else 0
        entidad_raw = str(cell(values, "entidad")).strip()
        entidad = "Fundación" if _slug(entidad_raw).startswith("fundacion") else ("SAD" if entidad_raw else None)
        concepto = str(cell(values, "concepto")).strip() or ("Salida de caja" if tipo == "gasto" else "Otro")
        nombre = str(cell(values, "nombre")).strip()
        legacy = str(cell(values, "id")).strip()
        if not legacy:
            legacy = "h:" + hashlib.sha1(f"{fecha}|{concepto}|{nombre}|{total_c}|{tipo}|{cell(values, 'nota')}|{n}".encode()).hexdigest()[:16]
        iva_raw = str(cell(values, "iva")).strip().replace("%", "")
        iva = int(float(iva_raw)) if iva_raw.replace(".", "", 1).isdigit() else None
        hora = ""
        origen = str(cell(values, "origen"))
        mh = re.search(r"(\d{1,2}:\d{2})", origen)
        if mh:
            hora = mh.group(1)
        parsed.append({"fecha": fecha, "hora": hora, "tipo": tipo, "importe_c": importe_c, "pago": pago,
                       "efectivo_c": ef, "tarjeta_c": ta, "bizum_c": bi, "entidad": entidad,
                       "concepto_nombre": concepto, "nombre": nombre, "nota": str(cell(values, "nota")).strip(),
                       "legacy_id": legacy, "iva": iva})

    def apply(cur):
        cur.execute("select doc->>'legacy_id' from public.app_documents where collection = 'caja_movimientos' "
                    "and doc->>'club_id' = %s and doc ? 'legacy_id'", (club_id,))
        seen = {r[0] for r in cur.fetchall()}
        creados = repetidos = 0
        for p in parsed:
            if p["legacy_id"] in seen:
                repetidos += 1
                continue
            seen.add(p["legacy_id"])
            year = int(p["fecha"][:4])
            n, numero = _next_number(cur, club_id, "HIST", year)
            base_c = cuota_c = None
            if p["iva"] is not None and p["tipo"] == "ingreso":
                base_c = int(round(p["importe_c"] / (1 + p["iva"] / 100)))
                cuota_c = p["importe_c"] - base_c
            doc = {
                "id": str(uuid.uuid4()), "club_id": club_id, "numero": numero, "serie": "HIST",
                "ejercicio": str(year), "correlativo": n, **p,
                "concepto": "importado", "descripcion": "", "opcion": "", "player_id": "", "player_team": "",
                "base_c": base_c, "cuota_iva_c": cuota_c, "estado": "valido", "cambios": [],
                "user_id": user["id"], "user_name": user.get("name", ""), "origen": "importado",
                "created_at": now_iso(), "updated_at": now_iso(),
            }
            _insert(cur, "caja_movimientos", doc)
            creados += 1
        return {"importados": creados, "repetidos": repetidos, "errores": errors[:50], "num_errores": len(errors)}

    return await _write(club_id, apply)
