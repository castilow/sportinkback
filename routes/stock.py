"""Catálogo de productos con código de barras (EAN), stock por talla y
registro de entradas / salidas con lector de códigos.

Pensado para la tienda / almacén de ropa del club. Cada producto es una
variante concreta (prenda + talla) con su EAN. El stock vive en el propio
producto y cada cambio queda registrado como movimiento (quién, cuándo, por
qué), así se puede reconstruir cualquier cifra.

Permisos: Dirección y Oficina registran; Coordinación solo consulta.
"""
import asyncio
import io
import re

import psycopg
from psycopg.types.json import Jsonb

from postgres_compat import PostgresCompatDB
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator

from deps import (
    db, now_iso, uuid, MAX_UPLOAD_BYTES,
    require_roles, require_club_context,
)

router = APIRouter()

WRITE_ROLES = ("admin", "office")
READ_ROLES = ("admin", "office", "coordinator")
MAX_IMPORT_ROWS = 5000
MAX_ITEMS_PER_MOVEMENT = 500

_EAN_RE = re.compile(r"^\d{6,14}$")


def _clean_ean(value) -> str:
    s = "" if value is None else str(value).strip()
    if s.endswith(".0"):  # Excel guarda a veces el código como número
        s = s[:-2]
    s = re.sub(r"\s+", "", s)
    return s


def _ean_ok(ean: str) -> bool:
    return bool(_EAN_RE.match(ean or ""))


def _size_from_variant(variant: str) -> str:
    """'8607-2XL' → '2XL'. Si no hay guion, la variante entera."""
    v = (variant or "").strip()
    return v.split("-", 1)[1] if "-" in v else v


def _public_product(p: dict) -> dict:
    p = dict(p)
    p.pop("_id", None)
    stock = int(p.get("stock") or 0)
    min_stock = int(p.get("min_stock") or 0)
    p["stock"] = stock
    p["min_stock"] = min_stock
    p["low"] = stock <= min_stock
    return p


# Todas las escrituras de este módulo usan el mismo bloqueo de club. El bloqueo
# vive en Postgres: funciona entre peticiones, procesos y réplicas del servicio.
def _require_postgres():
    if not isinstance(db, PostgresCompatDB):
        raise HTTPException(status_code=503, detail="Stock requiere PostgreSQL para registrar cambios de forma segura")


def _run_stock_transaction(club_id, operation):
    _require_postgres()
    # Una conexión independiente evita compartir la transacción con otras rutas.
    # El context manager confirma al terminar o revierte TODO ante una excepción.
    with psycopg.connect(db.database_url, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"sportink:stock:{club_id}",))
            return operation(cur)


async def _write_stock(club_id, operation):
    _require_postgres()
    return await asyncio.to_thread(_run_stock_transaction, club_id, operation)


def _select_products(cur, club_id):
    cur.execute("select doc from public.app_documents "
                "where collection = %s and doc->>'club_id' = %s for update",
                ("stock_products", club_id))
    return [row[0] for row in cur.fetchall()]


def _insert_document(cur, collection, doc):
    cur.execute("insert into public.app_documents (collection, doc) values (%s, %s)",
                (collection, Jsonb(doc)))


def _patch_product(cur, club_id, product_id, changes):
    cur.execute("update public.app_documents set doc = doc || %s, updated_at = now() "
                "where collection = %s and doc->>'club_id' = %s and doc->>'id' = %s",
                (Jsonb(changes), "stock_products", club_id, product_id))
    if cur.rowcount != 1:
        raise HTTPException(status_code=409, detail="El producto cambió durante la operación; vuelve a intentarlo")


# ------------------ Modelos ------------------
class ProductIn(BaseModel):
    ean: str
    name: str = Field(min_length=1, max_length=160)
    size: str = Field(default="", max_length=40)
    product_code: str = Field(default="", max_length=40)
    variant_code: str = Field(default="", max_length=60)
    min_stock: int = Field(default=0, ge=0, le=100000)
    price: Optional[float] = Field(default=None, ge=0, le=100000)

    @field_validator("ean")
    @classmethod
    def _v_ean(cls, v):
        v = _clean_ean(v)
        if not _ean_ok(v):
            raise ValueError("Código de barras no válido (solo números, 6-14 cifras)")
        return v


class ProductUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    size: Optional[str] = Field(default=None, max_length=40)
    min_stock: Optional[int] = Field(default=None, ge=0, le=100000)
    price: Optional[float] = Field(default=None, ge=0, le=100000)
    active: Optional[bool] = None


class MovementItemIn(BaseModel):
    product_id: str = ""
    ean: str = ""
    # Entrada/salida: unidades (≥1). Ajuste: recuento real (puede ser 0).
    qty: int = Field(ge=0, le=100000)


class MovementIn(BaseModel):
    type: Literal["entrada", "salida", "ajuste"]
    # entrada: proveedor / devolución…; salida: entrega a jugador, venta, equipo…
    reason: str = Field(default="", max_length=80)
    player_id: str = ""
    player_name: str = Field(default="", max_length=160)
    team: str = Field(default="", max_length=120)
    note: str = Field(default="", max_length=500)
    items: List[MovementItemIn] = Field(min_length=1, max_length=MAX_ITEMS_PER_MOVEMENT)


# ------------------ Catálogo ------------------
@router.get("/stock/products")
async def list_products(include_inactive: bool = False, user=Depends(require_roles(*READ_ROLES))):
    club_id = require_club_context(user)
    rows = await db.stock_products.find({"club_id": club_id}, {"_id": 0}).to_list(20000)
    rows = [r for r in rows if r.get("club_id") == club_id]
    if not include_inactive:
        rows = [r for r in rows if r.get("active", True)]
    rows.sort(key=lambda r: (str(r.get("name", "")).lower(), str(r.get("size", ""))))
    return [_public_product(r) for r in rows]


@router.get("/stock/lookup/{ean}")
async def lookup_ean(ean: str, user=Depends(require_roles(*READ_ROLES))):
    club_id = require_club_context(user)
    code = _clean_ean(ean)
    p = await db.stock_products.find_one({"club_id": club_id, "ean": code}, {"_id": 0})
    if not p:
        raise HTTPException(status_code=404, detail="Código no encontrado en el catálogo")
    return _public_product(p)


@router.post("/stock/products")
async def create_product(data: ProductIn, user=Depends(require_roles(*WRITE_ROLES))):
    club_id = require_club_context(user)

    def apply(cur):
        existing = next((p for p in _select_products(cur, club_id) if p.get("ean") == data.ean), None)
        if existing:
            raise HTTPException(status_code=409, detail=f"Ese código ya es «{existing.get('name')} {existing.get('size', '')}»".strip())
        doc = data.model_dump()
        doc.update({
            "id": str(uuid.uuid4()), "club_id": club_id, "stock": 0, "active": True,
            "created_at": now_iso(), "updated_at": now_iso(), "created_by": user["id"],
        })
        _insert_document(cur, "stock_products", doc)
        return _public_product(doc)

    return await _write_stock(club_id, apply)


@router.put("/stock/products/{product_id}")
async def update_product(product_id: str, data: ProductUpdateIn, user=Depends(require_roles(*WRITE_ROLES))):
    club_id = require_club_context(user)

    def apply(cur):
        p = next((p for p in _select_products(cur, club_id) if p.get("id") == product_id), None)
        if not p:
            raise HTTPException(status_code=404, detail="Producto no encontrado")
        changes = {k: v for k, v in data.model_dump().items() if v is not None}
        if changes:
            changes["updated_at"] = now_iso()
            _patch_product(cur, club_id, product_id, changes)
            p.update(changes)
        return _public_product(p)

    return await _write_stock(club_id, apply)


_IMPORT_COLS = {
    "product_code": ("nº producto", "n° producto", "no producto", "producto", "codigo producto", "código producto", "ref", "referencia"),
    "variant_code": ("cód. variante", "cod. variante", "código variante", "codigo variante", "variante"),
    "name": ("columna1", "descripción", "descripcion", "nombre", "artículo", "articulo", "prenda"),
    "ean": ("nº referencia cruzada", "n° referencia cruzada", "referencia cruzada", "ean", "código de barras", "codigo de barras", "cod. barras"),
    "size": ("talla", "size"),
    "price": ("precio", "pvp", "price"),
    "min_stock": ("stock mínimo", "stock minimo", "mínimo", "minimo"),
}


def _map_import_columns(headers) -> dict:
    out = {}
    for idx, h in enumerate(headers):
        key = str(h or "").strip().lower()
        for field, aliases in _IMPORT_COLS.items():
            if field not in out and key in aliases:
                out[field] = idx
    return out


@router.post("/stock/products/import")
async def import_products(file: UploadFile = File(...), user=Depends(require_roles(*WRITE_ROLES))):
    """Importa el catálogo de EAN (Excel o CSV). Crea los códigos nuevos y
    actualiza nombre/talla de los existentes. Nunca toca el stock."""
    club_id = require_club_context(user)
    _require_postgres()
    max_bytes = min(MAX_UPLOAD_BYTES, 5 * 1024 * 1024)
    content = await file.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise HTTPException(status_code=413, detail="El archivo es demasiado grande (máx. 5 MB)")
    name = (file.filename or "").lower()
    try:
        import pandas as pd
        if name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content), dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(io.BytesIO(content), dtype=str, keep_default_na=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")
    cols = _map_import_columns(df.columns)
    if "ean" not in cols or "name" not in cols:
        raise HTTPException(
            status_code=400,
            detail="Faltan columnas: hace falta el código de barras (EAN / «Nº referencia cruzada») y la descripción (Nombre / «Columna1»).",
        )
    if len(df) > MAX_IMPORT_ROWS:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_IMPORT_ROWS} filas por archivo")

    def apply(cur):
        existing = _select_products(cur, club_id)
        by_ean = {p.get("ean"): p for p in existing if p.get("club_id") == club_id}

        created = updated = unchanged = 0
        errors: list = []
        seen: set = set()
        for i, row in enumerate(df.itertuples(index=False), start=2):
            values = list(row)

            def cell(field):
                idx = cols.get(field)
                return "" if idx is None else str(values[idx] if values[idx] is not None else "").strip()

            ean = _clean_ean(cell("ean"))
            nm = cell("name")
            if not ean and not nm:
                continue
            if not _ean_ok(ean):
                errors.append({"row": i, "error": f"Código de barras no válido: «{ean}»"})
                continue
            if not nm:
                errors.append({"row": i, "error": "Falta la descripción"})
                continue
            if ean in seen:
                errors.append({"row": i, "error": f"Código repetido en el archivo: {ean}"})
                continue
            seen.add(ean)
            variant = cell("variant_code")
            size = cell("size") or _size_from_variant(variant)
            fields = {
                "name": nm[:160], "size": size[:40], "product_code": cell("product_code")[:40],
                "variant_code": variant[:60],
                # Prefijo de la variante (p. ej. color): distingue medias con la misma talla.
                "color": (variant.split("-", 1)[0] if "-" in variant else "")[:20],
            }
            price_raw = cell("price").replace(",", ".")
            if price_raw:
                try:
                    fields["price"] = round(float(price_raw), 2)
                except ValueError:
                    pass
            min_raw = cell("min_stock")
            if min_raw.isdigit():
                fields["min_stock"] = int(min_raw)

            current = by_ean.get(ean)
            if current:
                diff = {k: v for k, v in fields.items() if current.get(k) != v}
                if diff:
                    diff["updated_at"] = now_iso()
                    _patch_product(cur, club_id, current["id"], diff)
                    updated += 1
                else:
                    unchanged += 1
                continue
            doc = {
                "id": str(uuid.uuid4()), "club_id": club_id, "ean": ean, **fields,
                "price": fields.get("price"), "min_stock": fields.get("min_stock", 0),
                "stock": 0, "active": True, "created_at": now_iso(), "updated_at": now_iso(),
                "created_by": user["id"],
            }
            _insert_document(cur, "stock_products", doc)
            by_ean[ean] = doc
            created += 1

        return {"created": created, "updated": updated, "unchanged": unchanged, "errors": errors[:50],
                "error_count": len(errors)}

    return await _write_stock(club_id, apply)

# ------------------ Movimientos ------------------
@router.post("/stock/movements")
async def create_movement(data: MovementIn, user=Depends(require_roles(*WRITE_ROLES))):
    """Entrada (suma), salida (resta) o ajuste (fija el recuento real).

    Una salida que dejaría alguna talla en negativo se rechaza entera y dice
    cuáles: así el stock nunca miente. Si el recuento físico no cuadra, se
    corrige con un ajuste.
    """
    club_id = require_club_context(user)
    def apply(cur):
        products = _select_products(cur, club_id)
        products = [p for p in products if p.get("club_id") == club_id]
        by_id = {p["id"]: p for p in products}
        by_ean = {p.get("ean"): p for p in products}

        # Agrupar líneas por producto (el mismo código escaneado varias veces suma)
        lines: dict = {}
        missing = []
        for it in data.items:
            p = by_id.get(it.product_id) if it.product_id else by_ean.get(_clean_ean(it.ean))
            if not p:
                missing.append(it.ean or it.product_id)
                continue
            if data.type == "ajuste":
                lines[p["id"]] = it.qty
            elif it.qty < 1:
                continue
            else:
                lines[p["id"]] = lines.get(p["id"], 0) + it.qty
        if missing:
            raise HTTPException(status_code=404, detail=f"Códigos que no están en el catálogo: {', '.join(missing[:10])}")

        if not lines:
            raise HTTPException(status_code=400, detail="No hay unidades que registrar")

        if data.type == "salida":
            short = []
            for pid, qty in lines.items():
                have = int(by_id[pid].get("stock") or 0)
                if qty > have:
                    p = by_id[pid]
                    short.append(f"{p.get('name')} {p.get('size', '')} (hay {have}, salen {qty})".replace("  ", " "))
            if short:
                raise HTTPException(status_code=409, detail="No hay stock suficiente: " + "; ".join(short[:10]))

        movement_id = str(uuid.uuid4())
        out_items = []
        for pid, qty in lines.items():
            p = by_id[pid]
            before = int(p.get("stock") or 0)
            if data.type == "entrada":
                after = before + qty
            elif data.type == "salida":
                after = before - qty
            else:
                after = qty
            _patch_product(cur, club_id, pid, {"stock": after, "updated_at": now_iso()})
            out_items.append({
                "product_id": pid, "ean": p.get("ean"), "name": p.get("name"), "size": p.get("size", ""),
                "qty": qty if data.type != "ajuste" else after - before,
                "stock_before": before, "stock_after": after,
            })

        doc = {
            "id": movement_id, "club_id": club_id, "type": data.type,
            "reason": data.reason.strip(), "player_id": data.player_id, "player_name": data.player_name.strip(),
            "team": data.team.strip(), "note": data.note.strip(), "items": out_items,
            "units": sum(abs(i["qty"]) for i in out_items),
            "user_id": user["id"], "user_name": user.get("name", ""), "user_role": user.get("role", ""),
            "created_at": now_iso(),
        }
        _insert_document(cur, "stock_movements", doc)
        doc.pop("_id", None)
        return doc

    return await _write_stock(club_id, apply)

@router.get("/stock/movements")
async def list_movements(limit: int = 200, type: Optional[str] = None, product_id: Optional[str] = None,
                         user=Depends(require_roles(*READ_ROLES))):
    club_id = require_club_context(user)
    q = {"club_id": club_id}
    if type in ("entrada", "salida", "ajuste"):
        q["type"] = type
    rows = await db.stock_movements.find(q, {"_id": 0}).sort("created_at", -1).to_list(max(1, min(limit, 2000)))
    rows = [r for r in rows if r.get("club_id") == club_id]
    if product_id:
        rows = [r for r in rows if any(i.get("product_id") == product_id for i in r.get("items") or [])]
    return rows
