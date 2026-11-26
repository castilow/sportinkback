"""Planes y suscripciones de los clubes con Stripe.

Cómo funciona
-------------
- Cada club tiene una fila en public.club_subscriptions. Se crea sola la primera
  vez que se consulta: el club empieza con BILLING_TRIAL_DAYS días de prueba con
  todo incluido. El club por defecto (Rayo) está marcado como «comped» (gratis).
- Dirección elige plan y paga en Stripe Checkout (POST /billing/checkout). Las
  facturas, la tarjeta y la cancelación se gestionan en el portal de Stripe
  (POST /billing/portal). No guardamos datos de tarjeta en ningún momento.
- Stripe nos avisa por webhook (POST /billing/webhook) y actualizamos el estado.
- billing_guard() se aplica a toda la API: si la suscripción no está al día el
  club queda en SOLO LECTURA (puede ver y exportar, no crear ni modificar), y
  los módulos que no entran en su plan responden 402. Nunca se borran datos.
  Solo actúa con BILLING_ENFORCE=1, para poder desplegar antes de configurar Stripe.

Variables de entorno
--------------------
STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
STRIPE_PRICE_{BASICO,CLUB,PREMIUM}_{MONTH,YEAR}   ids de precio (price_…)
FRONTEND_URL                     a dónde vuelve el usuario tras pagar
BILLING_ENFORCE=1                activa los bloqueos
BILLING_TRIAL_DAYS (30), BILLING_GRACE_DAYS (7), STRIPE_AUTOMATIC_TAX=1 (opcional)
PLATFORM_ADMIN_EMAILS            correos que pueden regalar planes (coma)
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

import psycopg
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from deps import db, get_current_user, logger, require_club_context, require_roles
from postgres_compat import PostgresCompatDB

router = APIRouter()

# ------------------------------------------------------------------ planes
# Qué incluye cada plan. Lo que se COBRA es el precio de Stripe (STRIPE_PRICE_*);
# precio_ref (sin IVA) solo se muestra mientras Stripe no está configurado.
CORE_MODULES = ["jugadores", "equipos", "hojas", "convocatorias", "lesiones", "inventario", "incidencias", "estadisticas"]
PLANS: dict[str, dict] = {
    "basico": {
        "nombre": "Básico",
        "descripcion": "Para escuelas y clubes pequeños: jugadores, equipos y el día a día de los entrenadores.",
        "limites": {"equipos": 10, "jugadores": 150},
        "modulos": CORE_MODULES,
        "precio_ref": {"month": 49, "year": 490},
    },
    "club": {
        "nombre": "Club",
        "descripcion": "Todo el club en una app: oficina, caja, stock, calendarios RFFM y familias.",
        "limites": {"equipos": 35, "jugadores": 500},
        "modulos": CORE_MODULES + ["caja", "stock", "rffm", "captacion", "chat"],
        "precio_ref": {"month": 99, "year": 990},
        "destacado": True,
    },
    "premium": {
        "nombre": "Premium",
        "descripcion": "Para clubes grandes: sin límites, con tu marca, soporte prioritario y migración de datos incluida.",
        "limites": {"equipos": None, "jugadores": None},
        "modulos": CORE_MODULES + ["caja", "stock", "rffm", "captacion", "chat", "marca"],
        "precio_ref": {"month": 179, "year": 1790},
    },
}
ALL_MODULES = PLANS["premium"]["modulos"]

# módulo → prefijo de ruta (sin /api). True = solo se cobran las escrituras
# (la marca se LEE sin sesión en la pantalla de login).
MODULE_RULES = [
    ("caja", "/caja", False),
    ("stock", "/stock", False),
    ("rffm", "/rffm", False),
    ("captacion", "/captacion", False),
    ("chat", "/chat", False),
    ("marca", "/branding", True),
]
MODULE_NAMES = {"jugadores": "Jugadores y fichas", "equipos": "Equipos", "hojas": "Hojas de entreno y partido",
                "convocatorias": "Convocatorias por WhatsApp", "lesiones": "Lesiones y fisio", "inventario": "Inventario",
                "incidencias": "Incidencias", "estadisticas": "Estadísticas y minutos",
                "caja": "Caja", "stock": "Stock y escáner", "rffm": "Calendarios RFFM",
                "captacion": "Captación", "chat": "Chat con familias", "marca": "Estudio de marca"}

FULL_STATUSES = {"active", "trialing", "past_due", "incomplete"}
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Rutas que nunca se bloquean: login, la propia facturación…
GUARD_SKIP = ("/auth", "/billing", "/health", "/sports")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _trial_days() -> int:
    try:
        return max(0, int(_env("BILLING_TRIAL_DAYS", "30")))
    except ValueError:
        return 30


def _grace_days() -> int:
    try:
        return max(0, int(_env("BILLING_GRACE_DAYS", "7")))
    except ValueError:
        return 7


def _enforced() -> bool:
    return _env("BILLING_ENFORCE").lower() in ("1", "true", "yes", "on")


def _price_id(plan: str, interval: str) -> str:
    return _env(f"STRIPE_PRICE_{plan.upper()}_{interval.upper()}")


def _plan_for_price(price_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not price_id:
        return None, None
    for plan in PLANS:
        for interval in ("month", "year"):
            if _price_id(plan, interval) == price_id:
                return plan, interval
    return None, None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value) -> Optional[datetime]:
    if value in (None, "", 0):
        return None
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() if d else None


# ------------------------------------------------------------------ Stripe
def _client() -> stripe.StripeClient:
    key = _env("STRIPE_SECRET_KEY")
    if not key:
        raise HTTPException(status_code=503, detail="Los pagos todavía no están configurados (falta STRIPE_SECRET_KEY)")
    return stripe.StripeClient(key)


def _plain(obj) -> dict:
    """Objeto de Stripe → dict normal (StripeObject ya no es un dict)."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return json.loads(str(obj))


def _frontend_base(request: Optional[Request] = None) -> str:
    explicit = _env("FRONTEND_URL").rstrip("/")
    if explicit:
        return explicit
    allowed = [o.strip().rstrip("/") for o in _env("CORS_ORIGINS").split(",") if o.strip() and o.strip() != "*"]
    if request is not None:
        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin in allowed:
            return origin
    if allowed:
        return allowed[0]
    raise HTTPException(status_code=503, detail="Falta FRONTEND_URL para volver a la app después de pagar")


# ------------------------------------------------------------------ BD
COLUMNS = ("club_id", "plan", "status", "billing_interval", "trial_ends_at", "current_period_end",
           "cancel_at_period_end", "comped", "stripe_customer_id", "stripe_subscription_id",
           "stripe_price_id", "notes", "created_at", "updated_at")
_SELECT = "select " + ", ".join(f"{c}::text" if c == "club_id" else c for c in COLUMNS) + " from public.club_subscriptions"


def _require_postgres():
    if not isinstance(db, PostgresCompatDB):
        raise HTTPException(status_code=503, detail="La facturación requiere PostgreSQL")


def _connect():
    _require_postgres()
    return psycopg.connect(db.database_url, connect_timeout=10)


def _row(cur) -> Optional[dict]:
    r = cur.fetchone()
    return dict(zip(COLUMNS, r)) if r else None


def _get_or_create_sync(club_id: str) -> dict:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(_SELECT + " where club_id = %s::uuid", (club_id,))
        row = _row(cur)
        if row:
            return row
        cur.execute("select is_default from public.clubs where id = %s::uuid", (club_id,))
        club = cur.fetchone()
        if not club:
            raise HTTPException(status_code=404, detail="Club no encontrado")
        if club[0]:
            cur.execute(
                "insert into public.club_subscriptions (club_id, plan, status, comped, notes) "
                "values (%s::uuid, 'premium', 'active', true, 'Club fundador: sin cuota') on conflict (club_id) do nothing",
                (club_id,))
        else:
            cur.execute(
                "insert into public.club_subscriptions (club_id, plan, status, trial_ends_at) "
                "values (%s::uuid, 'premium', 'trialing', now() + make_interval(days => %s)) on conflict (club_id) do nothing",
                (club_id, _trial_days()))
            created = cur.rowcount == 1
        cur.execute(_SELECT + " where club_id = %s::uuid", (club_id,))
        row = _row(cur)
    if not club[0] and created:
        _after_trial_created(club_id)
    return row


def _after_trial_created(club_id: str):
    """Club nuevo en prueba: guarda el plan que eligió en la web y le da la bienvenida."""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select payload->>'plan_interes' from public.public_registrations "
                        "where club_id = %s::uuid order by created_at desc limit 1", (club_id,))
            r = cur.fetchone()
            if r and r[0] in PLANS:
                cur.execute("update public.club_subscriptions set plan_interes = %s where club_id = %s::uuid", (r[0], club_id))
    except Exception as exc:
        logger.info("Plan elegido en el registro no disponible para %s: %s", club_id, exc)
    import threading
    threading.Thread(target=send_billing_email_safe, args=(club_id, "bienvenida"), daemon=True).start()


def _update_sync(club_id: str, changes: dict) -> dict:
    allowed = {k: v for k, v in changes.items() if k in COLUMNS and k not in ("club_id", "created_at", "updated_at")}
    if not allowed:
        return _get_or_create_sync(club_id)
    sets = ", ".join(f"{k} = %s" for k in allowed)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"update public.club_subscriptions set {sets}, updated_at = now() where club_id = %s::uuid",
                    (*allowed.values(), club_id))
        cur.execute(_SELECT + " where club_id = %s::uuid", (club_id,))
        return _row(cur)


def _club_by_customer_sync(customer_id: str) -> Optional[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select club_id::text from public.club_subscriptions where stripe_customer_id = %s", (customer_id,))
        r = cur.fetchone()
        return r[0] if r else None


def _club_info_sync(club_id: str) -> dict:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select nombre, slug from public.clubs where id = %s::uuid", (club_id,))
        r = cur.fetchone()
        return {"nombre": r[0], "slug": r[1]} if r else {"nombre": "", "slug": ""}


def _usage_sync(club_id: str) -> dict:
    from players_store import TEMPORADA
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select count(*) from public.teams where club_id = %s::uuid and activo = true and temporada = %s",
                    (club_id, TEMPORADA))
        equipos = cur.fetchone()[0]
        cur.execute("select count(*) from public.players where club_id = %s::uuid and temporada = %s", (club_id, TEMPORADA))
        jugadores = cur.fetchone()[0]
    return {"equipos": int(equipos), "jugadores": int(jugadores)}


def _mark_event_sync(event_id: str, event_type: str) -> bool:
    """True si el evento es nuevo (hay que procesarlo)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("insert into public.stripe_events (id, type) values (%s, %s) on conflict (id) do nothing",
                    (event_id, event_type))
        return cur.rowcount == 1


def _unmark_event_sync(event_id: str):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("delete from public.stripe_events where id = %s", (event_id,))


async def _io(fn, *args):
    return await asyncio.to_thread(fn, *args)


# ------------------------------------------------------------------ acceso
def compute_access(sub: dict, now: Optional[datetime] = None) -> dict:
    """Qué puede hacer el club con su suscripción actual."""
    now = now or _now()
    plan_key = sub.get("plan") if sub.get("plan") in PLANS else "premium"
    status = sub.get("status") or "trialing"
    trial_end = sub.get("trial_ends_at")
    period_end = sub.get("current_period_end")
    out = {"nivel": "full", "plan": plan_key, "motivo": None, "aviso": None, "dias_prueba": None}

    if sub.get("comped"):
        out["motivo"] = "comped"
    elif status == "trialing" and not sub.get("stripe_subscription_id"):
        # Prueba propia (sin tarjeta): todo incluido hasta la fecha de fin.
        if trial_end and trial_end > now:
            import math
            days = max(1, math.ceil((trial_end - now).total_seconds() / 86400))
            out.update(plan="premium", motivo="trial", dias_prueba=days)
            if days <= 5:
                out["aviso"] = f"Tu prueba gratuita termina en {days} día{'s' if days != 1 else ''}. Elige un plan para no perder el acceso."
        else:
            out.update(nivel="read_only", motivo="trial_expired",
                       aviso="La prueba gratuita ha terminado. El club está en modo consulta hasta que elijas un plan.")
    elif status in ("active", "trialing"):
        out["motivo"] = status
        if sub.get("cancel_at_period_end") and period_end:
            out["aviso"] = f"La suscripción se cancelará el {period_end.date().strftime('%d/%m/%Y')}."
    elif status in ("past_due", "incomplete"):
        limit = (period_end or now) + timedelta(days=_grace_days())
        if now <= limit:
            out.update(motivo=status, aviso="No hemos podido cobrar la suscripción. Actualiza el método de pago para no perder el acceso.")
        else:
            out.update(nivel="read_only", motivo=status,
                       aviso="La suscripción tiene pagos pendientes. El club está en modo consulta hasta que se actualice el pago.")
    else:  # canceled, unpaid, incomplete_expired, paused
        out.update(nivel="read_only", motivo=status,
                   aviso="La suscripción no está activa. El club está en modo consulta: puedes ver y exportar, pero no modificar.")

    plan = PLANS[out["plan"]]
    out["modulos"] = list(plan["modulos"])
    out["limites"] = dict(plan["limites"])
    return out


_CACHE: dict[str, tuple[float, dict, dict]] = {}
_CACHE_TTL = 30.0


def _invalidate(club_id: Optional[str] = None):
    if club_id:
        _CACHE.pop(club_id, None)
    else:
        _CACHE.clear()


async def get_subscription(club_id: str) -> tuple[dict, dict]:
    hit = _CACHE.get(club_id)
    if hit and time.monotonic() - hit[0] < _CACHE_TTL:
        return hit[1], hit[2]
    sub = await _io(_get_or_create_sync, club_id)
    acc = compute_access(sub)
    _CACHE[club_id] = (time.monotonic(), sub, acc)
    return sub, acc


def _module_for(path: str, method: str) -> Optional[str]:
    for module, prefix, writes_only in MODULE_RULES:
        if path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?"):
            if writes_only and method not in WRITE_METHODS:
                return None
            return module
    return None


def _payment_required(code: str, message: str, **extra):
    raise HTTPException(status_code=402, detail={"code": code, "message": message, **extra})


async def billing_guard(request: Request):
    """Dependencia global de la API: modo solo lectura, módulos por plan y límites."""
    if not _enforced() or request.method == "OPTIONS":
        return
    path = request.url.path
    if path.startswith("/api/"):
        path = path[4:]
    if any(path == p or path.startswith(p + "/") for p in GUARD_SKIP):
        return
    try:
        user = await get_current_user(request)
    except HTTPException:
        return  # sin sesión: que decida el propio endpoint (familias, login…)
    club_id = user.get("club_id")
    if not club_id or user.get("role") == "pending":
        return
    try:
        sub, acc = await get_subscription(club_id)
    except Exception as exc:  # la facturación nunca debe tumbar la app
        logger.error("billing_guard: no se pudo leer la suscripción de %s: %s", club_id, exc)
        return

    method = request.method.upper()
    module = _module_for(path, method)
    if module and module not in acc["modulos"]:
        _payment_required("plan_upgrade_required",
                          f"«{MODULE_NAMES.get(module, module)}» no está incluido en tu plan. Mejora el plan para usarlo.",
                          modulo=module)
    if method in WRITE_METHODS and acc["nivel"] != "full":
        _payment_required("subscription_inactive", acc.get("aviso") or "La suscripción no está activa.", motivo=acc.get("motivo"))
    if method == "POST" and path in ("/teams", "/teams/import", "/players", "/players/import/preview") or \
            (method == "POST" and path.startswith("/players/import/commit")):
        limites = acc["limites"]
        if limites.get("equipos") is None and limites.get("jugadores") is None:
            return
        usage = await _io(_usage_sync, club_id)
        kind = "equipos" if path.startswith("/teams") else "jugadores"
        cap = limites.get(kind)
        if cap is not None and usage[kind] >= cap:
            _payment_required("plan_limit_reached",
                              f"Has llegado al máximo de {cap} {kind} de tu plan. Mejora el plan para añadir más.",
                              limite=kind, maximo=cap, uso=usage[kind])


# ------------------------------------------------------------------ aplicar Stripe → BD
def _subscription_fields(sub: dict) -> dict:
    items = ((sub.get("items") or {}).get("data")) or []
    first = items[0] if items else {}
    price = first.get("price") or {}
    price_id = price.get("id") if isinstance(price, dict) else price
    plan, interval = _plan_for_price(price_id)
    if not plan:
        plan = (sub.get("metadata") or {}).get("plan") if (sub.get("metadata") or {}).get("plan") in PLANS else None
    if not interval and isinstance(price, dict):
        interval = ((price.get("recurring") or {}).get("interval"))
    # Desde la API 2025-03-31 el fin de periodo está en cada item.
    period_end = sub.get("current_period_end") or first.get("current_period_end")
    status = sub.get("status") or "incomplete"
    if status not in ("trialing", "active", "past_due", "unpaid", "canceled", "incomplete", "incomplete_expired", "paused"):
        status = "incomplete"
    fields = {
        "status": status,
        "stripe_subscription_id": sub.get("id"),
        "stripe_price_id": price_id,
        "billing_interval": interval if interval in ("month", "year") else None,
        "current_period_end": _ts(period_end),
        "trial_ends_at": _ts(sub.get("trial_end")),
        "cancel_at_period_end": bool(sub.get("cancel_at_period_end")) or bool(sub.get("cancel_at")),
    }
    customer = sub.get("customer")
    if customer:
        fields["stripe_customer_id"] = customer if isinstance(customer, str) else customer.get("id")
    if plan:
        fields["plan"] = plan
    return fields


def apply_subscription_sync(club_id: str, sub: dict) -> dict:
    current = _get_or_create_sync(club_id)
    fields = _subscription_fields(sub)
    stored = current.get("stripe_subscription_id")
    # Un evento tardío de una suscripción antigua no pisa la nueva.
    if stored and fields["stripe_subscription_id"] != stored and current.get("status") in FULL_STATUSES \
            and fields["status"] in ("canceled", "incomplete_expired", "unpaid"):
        logger.info("Stripe: ignorado %s de la suscripción antigua %s (club %s)", fields["status"], fields["stripe_subscription_id"], club_id)
        return current
    if not fields.get("trial_ends_at"):
        fields.pop("trial_ends_at", None)  # no borra la fecha de la prueba propia
    row = _update_sync(club_id, fields)
    _invalidate(club_id)
    return row


def _club_from_subscription(sub: dict) -> Optional[str]:
    club = (sub.get("metadata") or {}).get("club_id")
    if club:
        return club
    customer = sub.get("customer")
    customer_id = customer if isinstance(customer, str) else (customer or {}).get("id")
    return _club_by_customer_sync(customer_id) if customer_id else None


def _invoice_subscription_id(invoice: dict) -> Optional[str]:
    sub = invoice.get("subscription")
    if not sub:
        sub = (((invoice.get("parent") or {}).get("subscription_details")) or {}).get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    return sub


def handle_event_sync(event: dict) -> str:
    etype = event.get("type") or ""
    obj = ((event.get("data") or {}).get("object")) or {}
    if etype == "checkout.session.completed":
        if obj.get("mode") != "subscription" or not obj.get("subscription"):
            return "ignorado"
        club_id = (obj.get("metadata") or {}).get("club_id") or obj.get("client_reference_id")
        if not club_id:
            return "sin club"
        sub = _plain(_client().v1.subscriptions.retrieve(obj["subscription"]))
        sub.setdefault("metadata", {}).setdefault("club_id", club_id)
        apply_subscription_sync(club_id, sub)
        return "aplicado"
    if etype.startswith("customer.subscription."):
        club_id = _club_from_subscription(obj)
        if not club_id:
            return "sin club"
        apply_subscription_sync(club_id, obj)
        return "aplicado"
    if etype in ("invoice.paid", "invoice.payment_succeeded", "invoice.payment_failed"):
        sub_id = _invoice_subscription_id(obj)
        if not sub_id:
            return "ignorado"
        sub = _plain(_client().v1.subscriptions.retrieve(sub_id))
        club_id = _club_from_subscription(sub)
        if not club_id:
            return "sin club"
        apply_subscription_sync(club_id, sub)
        if etype == "invoice.payment_failed":
            send_billing_email_safe(club_id, f"pago_fallido:{obj.get('id') or event.get('id')}", invoice=obj)
        return "aplicado"
    return "ignorado"


# ------------------------------------------------------------------ rutas
def _sub_out(sub: dict, acc: dict, usage: Optional[dict] = None) -> dict:
    return {
        "plan": acc["plan"],
        "plan_nombre": PLANS[acc["plan"]]["nombre"],
        "plan_contratado": sub.get("plan") if sub.get("stripe_subscription_id") else None,
        "estado": sub.get("status"),
        "intervalo": sub.get("billing_interval"),
        "prueba_hasta": _iso(sub.get("trial_ends_at")),
        "renueva_el": _iso(sub.get("current_period_end")),
        "cancela_al_final": bool(sub.get("cancel_at_period_end")),
        "gratis": bool(sub.get("comped")),
        "tiene_cliente_stripe": bool(sub.get("stripe_customer_id")),
        "acceso": acc,
        "uso": usage,
        "cobros_activos": _enforced(),
        "pagos_configurados": bool(_env("STRIPE_SECRET_KEY")),
        "plan_interes": sub.get("plan_interes"),
    }


_PRICE_CACHE: dict = {"t": 0.0, "data": {}}


def _prices_sync() -> dict:
    if time.monotonic() - _PRICE_CACHE["t"] < 600 and _PRICE_CACHE["data"]:
        return _PRICE_CACHE["data"]
    data: dict = {}
    if not _env("STRIPE_SECRET_KEY"):
        return data
    client = _client()
    for plan in PLANS:
        for interval in ("month", "year"):
            pid = _price_id(plan, interval)
            if not pid:
                continue
            try:
                p = _plain(client.v1.prices.retrieve(pid))
                data[(plan, interval)] = {"importe": (p.get("unit_amount") or 0) / 100, "moneda": (p.get("currency") or "eur").upper()}
            except Exception as exc:
                logger.warning("Stripe: no se pudo leer el precio %s: %s", pid, exc)
    _PRICE_CACHE.update(t=time.monotonic(), data=data)
    return data


@router.get("/billing/plans")
async def list_plans():
    """Catálogo público de planes (la landing y la pantalla «Plan» lo usan)."""
    prices = await _io(_prices_sync)
    out = []
    for key, p in PLANS.items():
        out.append({
            "key": key, "nombre": p["nombre"], "descripcion": p["descripcion"], "destacado": bool(p.get("destacado")),
            "limites": p["limites"],
            "modulos": [{"key": m, "nombre": MODULE_NAMES.get(m, m.capitalize())} for m in p["modulos"]],
            "precios": {
                interval: {**(prices.get((key, interval)) or {"importe": p["precio_ref"][interval], "moneda": "EUR", "referencia": True}),
                           "disponible": bool(_price_id(key, interval) and _env("STRIPE_SECRET_KEY"))}
                for interval in ("month", "year")
            },
        })
    return {"planes": out, "prueba_dias": _trial_days()}


@router.get("/billing/me")
async def my_subscription(user=Depends(get_current_user)):
    club_id = require_club_context(user)
    _invalidate(club_id)
    sub, acc = await get_subscription(club_id)
    usage = await _io(_usage_sync, club_id)
    out = _sub_out({**sub, "plan_interes": await _io(_plan_interes_sync, club_id)}, acc, usage)
    out["puede_gestionar"] = user.get("role") == "admin"
    return out


class CheckoutIn(BaseModel):
    plan: Literal["basico", "club", "premium"]
    intervalo: Literal["month", "year"] = "month"


def _ensure_customer_sync(club_id: str, user: dict) -> str:
    sub = _get_or_create_sync(club_id)
    if sub.get("stripe_customer_id"):
        return sub["stripe_customer_id"]
    club = _club_info_sync(club_id)
    customer = _plain(_client().v1.customers.create(params={
        "name": club.get("nombre") or None,
        "email": user.get("email") or None,
        "metadata": {"club_id": club_id, "club_slug": club.get("slug") or ""},
        "preferred_locales": ["es"],
    }, options={"idempotency_key": f"sportink-customer-{club_id}"}))
    _update_sync(club_id, {"stripe_customer_id": customer["id"]})
    return customer["id"]


@router.post("/billing/checkout")
async def create_checkout(data: CheckoutIn, request: Request, user=Depends(require_roles("admin"))):
    club_id = require_club_context(user)
    price = _price_id(data.plan, data.intervalo)
    if not price:
        raise HTTPException(status_code=400, detail="Ese plan todavía no tiene precio configurado en Stripe")
    sub = await _io(_get_or_create_sync, club_id)
    if sub.get("comped"):
        raise HTTPException(status_code=409, detail="Este club no paga cuota de plataforma")
    if sub.get("stripe_subscription_id") and sub.get("status") in FULL_STATUSES:
        raise HTTPException(status_code=409, detail="Ya tienes una suscripción. Para cambiar de plan usa «Gestionar suscripción».")
    over = _over_limits(data.plan, await _io(_usage_sync, club_id))
    if over:
        raise HTTPException(status_code=409, detail={
            "code": "downgrade_over_limit",
            "message": "Este plan se te queda corto: " + "; ".join(over) + ". Elige un plan mayor.",
        })
    base = _frontend_base(request)
    customer_id = await _io(_ensure_customer_sync, club_id, user)

    sub_data: dict = {"metadata": {"club_id": club_id, "plan": data.plan}}
    trial_end = sub.get("trial_ends_at")
    # Si aún le quedan días de prueba, no los pierde por pagar antes (Stripe pide ≥ 48 h).
    if trial_end and trial_end - _now() > timedelta(hours=49):
        sub_data["trial_end"] = int(trial_end.timestamp())
    params = {
        "mode": "subscription",
        "customer": customer_id,
        "client_reference_id": club_id,
        "line_items": [{"price": price, "quantity": 1}],
        "success_url": f"{base}/plan?pago=ok&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base}/plan?pago=cancelado",
        "allow_promotion_codes": True,
        "billing_address_collection": "required",
        "tax_id_collection": {"enabled": True},
        "customer_update": {"address": "auto", "name": "auto"},
        "locale": "es",
        "metadata": {"club_id": club_id, "plan": data.plan},
        "subscription_data": sub_data,
    }
    if _env("STRIPE_AUTOMATIC_TAX").lower() in ("1", "true", "yes"):
        params["automatic_tax"] = {"enabled": True}
    try:
        session = _plain(await _io(lambda: _client().v1.checkout.sessions.create(params=params)))
    except stripe.StripeError as exc:
        logger.error("Stripe checkout falló (club %s): %s", club_id, exc)
        raise HTTPException(status_code=502, detail="No se pudo abrir el pago. Inténtalo de nuevo en unos minutos.")
    return {"url": session.get("url"), "id": session.get("id")}


@router.post("/billing/portal")
async def create_portal(request: Request, user=Depends(require_roles("admin"))):
    club_id = require_club_context(user)
    sub = await _io(_get_or_create_sync, club_id)
    if not sub.get("stripe_customer_id"):
        raise HTTPException(status_code=404, detail="Todavía no hay ninguna suscripción que gestionar")
    base = _frontend_base(request)
    try:
        session = _plain(await _io(lambda: _client().v1.billing_portal.sessions.create(params={
            "customer": sub["stripe_customer_id"], "return_url": f"{base}/plan", "locale": "es",
        })))
    except stripe.StripeError as exc:
        logger.error("Stripe portal falló (club %s): %s", club_id, exc)
        raise HTTPException(status_code=502, detail="No se pudo abrir el portal de facturación")
    return {"url": session.get("url")}


@router.post("/billing/sync")
async def sync_subscription(session_id: Optional[str] = None, user=Depends(require_roles("admin"))):
    """Tras volver de Stripe: no esperamos al webhook para mostrar el plan activo."""
    club_id = require_club_context(user)
    sub = await _io(_get_or_create_sync, club_id)

    def run():
        client = _client()
        sub_id = sub.get("stripe_subscription_id")
        if session_id:
            s = _plain(client.v1.checkout.sessions.retrieve(session_id))
            owner = (s.get("metadata") or {}).get("club_id") or s.get("client_reference_id")
            if owner != club_id:
                raise HTTPException(status_code=403, detail="Ese pago no es de tu club")
            sub_id = s.get("subscription") if isinstance(s.get("subscription"), str) else (s.get("subscription") or {}).get("id")
        if not sub_id:
            return _get_or_create_sync(club_id)
        stripe_sub = _plain(client.v1.subscriptions.retrieve(sub_id))
        if _club_from_subscription(stripe_sub) not in (None, club_id):
            raise HTTPException(status_code=403, detail="Esa suscripción no es de tu club")
        stripe_sub.setdefault("metadata", {}).setdefault("club_id", club_id)
        return apply_subscription_sync(club_id, stripe_sub)

    try:
        row = await _io(run)
    except stripe.StripeError as exc:
        logger.error("Stripe sync falló (club %s): %s", club_id, exc)
        raise HTTPException(status_code=502, detail="No se pudo consultar Stripe")
    _invalidate(club_id)
    acc = compute_access(row)
    return _sub_out(row, acc, await _io(_usage_sync, club_id))


@router.post("/billing/webhook")
async def stripe_webhook(request: Request):
    secret = _env("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="Webhook no configurado")
    payload = await request.body()
    try:
        stripe.WebhookSignature.verify_header(payload.decode("utf-8"), request.headers.get("stripe-signature") or "", secret, 300)
        event = json.loads(payload)
    except (stripe.SignatureVerificationError, ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Firma no válida")
    event_id, etype = event.get("id"), event.get("type") or ""
    if not event_id:
        raise HTTPException(status_code=400, detail="Evento sin id")
    if not await _io(_mark_event_sync, event_id, etype):
        return {"received": True, "duplicado": True}
    try:
        result = await _io(handle_event_sync, event)
    except Exception as exc:
        # Se desmarca para que Stripe lo reintente.
        await _io(_unmark_event_sync, event_id)
        logger.error("Stripe webhook %s (%s) falló: %s", event_id, etype, exc)
        raise HTTPException(status_code=500, detail="Error procesando el evento")
    logger.info("Stripe webhook %s %s → %s", event_id, etype, result)
    return {"received": True, "resultado": result}


def _plan_interes_sync(club_id: str) -> Optional[str]:
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select plan_interes from public.club_subscriptions where club_id = %s::uuid", (club_id,))
            r = cur.fetchone()
            return r[0] if r else None
    except Exception:
        return None  # migración de emails aún no aplicada


# ------------------------------------------------------------------ capacidad (importaciones)
KIND_LABEL = {"jugadores": ("jugador", "jugadores"), "equipos": ("equipo", "equipos")}


async def capacity_info(club_id: Optional[str], kind: str, adding: int) -> Optional[dict]:
    """¿Caben `adding` jugadores/equipos más en el plan? None si no hay límite."""
    if not _enforced() or not club_id or adding <= 0:
        return None
    try:
        sub, acc = await get_subscription(club_id)
    except Exception:
        return None
    cap = acc["limites"].get(kind)
    if cap is None:
        return None
    usage = (await _io(_usage_sync, club_id))[kind]
    libre = max(0, cap - usage)
    ok = usage + adding <= cap
    one, many = KIND_LABEL.get(kind, (kind, kind))
    msg = None if ok else (
        f"Este archivo añade {adding} {one if adding == 1 else many} y tu plan {PLANS[acc['plan']]['nombre']} "
        f"permite {cap} (ya tienes {usage}). Mejora el plan o importa como máximo {libre}."
    )
    return {"permitido": ok, "tipo": kind, "uso": usage, "maximo": cap, "nuevos": adding, "libre": libre,
            "plan": acc["plan"], "mensaje": msg}


async def ensure_capacity(user: dict, kind: str, adding: int):
    info = await capacity_info(user.get("club_id"), kind, adding)
    if info and not info["permitido"]:
        _payment_required("plan_limit_reached", info["mensaje"], limite=kind, maximo=info["maximo"],
                          uso=info["uso"], nuevos=adding)


# ------------------------------------------------------------------ cambio de plan (con control de bajada)
class ChangePlanIn(BaseModel):
    plan: Literal["basico", "club", "premium"]
    intervalo: Literal["month", "year"] = "month"


def _over_limits(plan: str, usage: dict) -> list[str]:
    out = []
    for kind, cap in PLANS[plan]["limites"].items():
        if cap is not None and usage.get(kind, 0) > cap:
            out.append(f"tienes {usage[kind]} {kind} y el plan {PLANS[plan]['nombre']} permite {cap}")
    return out


@router.post("/billing/change-plan")
async def change_plan(data: ChangePlanIn, user=Depends(require_roles("admin"))):
    """Subir o bajar de plan (o pasar a anual) sin salir de Sportink.
    Antes de bajar, comprueba que el club cabe en el plan nuevo."""
    club_id = require_club_context(user)
    sub = await _io(_get_or_create_sync, club_id)
    if not sub.get("stripe_subscription_id") or sub.get("status") not in FULL_STATUSES:
        raise HTTPException(status_code=409, detail="No tienes una suscripción activa: elige un plan para empezar.")
    price = _price_id(data.plan, data.intervalo)
    if not price:
        raise HTTPException(status_code=400, detail="Ese plan todavía no tiene precio configurado en Stripe")
    if price == sub.get("stripe_price_id"):
        raise HTTPException(status_code=409, detail="Ya tienes este plan.")
    usage = await _io(_usage_sync, club_id)
    over = _over_limits(data.plan, usage)
    if over:
        raise HTTPException(status_code=409, detail={
            "code": "downgrade_over_limit",
            "message": "No puedes pasar a este plan: " + "; ".join(over) + ". Da de baja lo que sobra o elige un plan mayor.",
            "uso": usage, "limites": PLANS[data.plan]["limites"],
        })

    def run():
        client = _client()
        current = _plain(client.v1.subscriptions.retrieve(sub["stripe_subscription_id"]))
        items = ((current.get("items") or {}).get("data")) or []
        if not items:
            raise HTTPException(status_code=409, detail="La suscripción no tiene ningún concepto que cambiar")
        updated = _plain(client.v1.subscriptions.update(sub["stripe_subscription_id"], params={
            "items": [{"id": items[0]["id"], "price": price}],
            "proration_behavior": "create_prorations",
            "metadata": {"club_id": club_id, "plan": data.plan},
        }))
        updated.setdefault("metadata", {}).setdefault("club_id", club_id)
        return apply_subscription_sync(club_id, updated)

    try:
        row = await _io(run)
    except stripe.StripeError as exc:
        logger.error("Stripe cambio de plan falló (club %s): %s", club_id, exc)
        raise HTTPException(status_code=502, detail="No se pudo cambiar el plan en Stripe. Inténtalo de nuevo.")
    _invalidate(club_id)
    logger.info("Club %s cambia a %s/%s", club_id, data.plan, data.intervalo)
    return _sub_out(row, compute_access(row), usage)


# ------------------------------------------------------------------ emails automáticos
def _admin_emails_sync(club_id: str) -> list[str]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("select distinct lower(email) from public.profiles where club_id = %s::uuid and rol = 'admin' "
                    "and email is not null and email <> ''", (club_id,))
        return [r[0] for r in cur.fetchall()]


def _claim_email_sync(club_id: str, key: str) -> bool:
    """Marca el email como enviado; False si ya lo estaba (evita duplicados entre procesos)."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("update public.club_subscriptions set emails_enviados = emails_enviados || jsonb_build_object(%s::text, now()) "
                    "where club_id = %s::uuid and not (emails_enviados ? %s::text)", (key, club_id, key))
        return cur.rowcount == 1


def _unclaim_email_sync(club_id: str, key: str):
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("update public.club_subscriptions set emails_enviados = emails_enviados - %s::text where club_id = %s::uuid",
                    (key, club_id))


def _email_html(title: str, paragraphs: list[str], cta: Optional[tuple[str, str]], club: str) -> str:
    import html as _h
    body = "".join(f'<p style="margin:0 0 14px;font-size:15px;line-height:1.6;color:#334155">{p}</p>' for p in paragraphs)
    button = (f'<a href="{_h.escape(cta[1])}" style="display:inline-block;background:#1d4ed8;color:#ffffff;text-decoration:none;'
              f'font-weight:600;font-size:15px;padding:12px 22px;border-radius:10px">{_h.escape(cta[0])}</a>') if cta else ""
    return f"""<!doctype html><html><body style="margin:0;background:#f1f5f9;font-family:Inter,Segoe UI,Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9;padding:28px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e2e8f0">
<tr><td style="background:#0a1f4d;padding:22px 28px;color:#ffffff;font-size:20px;font-weight:800;letter-spacing:-.3px">Sportink<span style="color:#60a5fa">.</span>
<div style="font-size:12px;font-weight:500;color:#93c5fd;letter-spacing:.12em;text-transform:uppercase;margin-top:2px">{_h.escape(club)}</div></td></tr>
<tr><td style="padding:28px">
<h1 style="margin:0 0 16px;font-size:22px;line-height:1.3;color:#0a1f4d">{title}</h1>{body}
<div style="margin-top:22px">{button}</div></td></tr>
<tr><td style="padding:16px 28px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8">Recibes este correo porque eres de Dirección en Sportink. ¿Dudas? Responde a este email.</td></tr>
</table></td></tr></table></body></html>"""


def _build_email(kind: str, club: str, sub: dict, base: str, invoice: Optional[dict] = None) -> tuple[str, str, str]:
    plan_url = f"{base}/plan" if base else "/plan"
    import html as _h
    c = _h.escape(club or "tu club")
    trial_end = sub.get("trial_ends_at")
    fecha = trial_end.astimezone(timezone.utc).strftime("%d/%m/%Y") if trial_end else ""
    interes = sub.get("plan_interes")
    plan_line = f"Cuando quieras, activa el plan <b>{PLANS[interes]['nombre']}</b> que elegiste en la web." if interes in PLANS else \
        "Cuando quieras, elige el plan que mejor encaje con tu club."
    if kind == "bienvenida":
        title = f"¡Bienvenido a Sportink, {c}!"
        paras = [f"Ya tienes <b>{_trial_days()} días de prueba con todo incluido</b> (hasta el {fecha}), sin tarjeta.",
                 "Para empezar en 10 minutos: importa tus jugadores desde Excel, invita a tus entrenadores en «Usuarios» "
                 "y prueba la hoja de partido desde el móvil.", plan_line]
        cta = ("Entrar en Sportink", base or plan_url)
        subject = "Bienvenido a Sportink: tu prueba de 30 días ha empezado"
    elif kind == "prueba_7":
        title = "Te queda una semana de prueba"
        paras = [f"La prueba de {c} termina el <b>{fecha}</b>.", plan_line,
                 "Si no eliges plan, no se borra nada: el club quedará en modo consulta hasta que lo actives."]
        cta = ("Ver planes", plan_url)
        subject = "Tu prueba de Sportink termina en 7 días"
    elif kind == "prueba_3":
        title = "Quedan 3 días de prueba"
        paras = [f"El {fecha} termina la prueba de {c}. Elige un plan para seguir trabajando sin cortes.", plan_line]
        cta = ("Elegir plan", plan_url)
        subject = "Quedan 3 días de tu prueba de Sportink"
    elif kind == "prueba_fin":
        title = "Tu prueba ha terminado"
        paras = [f"{c} está ahora en <b>modo consulta</b>: podéis ver y exportar todo, pero no crear ni modificar.",
                 "Todos vuestros datos siguen ahí. Activa un plan y volvéis a trabajar al momento."]
        cta = ("Activar un plan", plan_url)
        subject = "Tu prueba de Sportink ha terminado"
    else:  # pago_fallido
        amount = ""
        if invoice and invoice.get("amount_due"):
            amount = f" de {invoice['amount_due'] / 100:.2f} €".replace(".", ",")
        title = "No hemos podido cobrar tu suscripción"
        paras = [f"El cobro{amount} de la suscripción de {c} ha fallado. Stripe lo reintentará en los próximos días.",
                 f"Para evitar cortes, revisa o cambia el método de pago. Si en {_grace_days()} días sigue pendiente, "
                 "el club pasará a modo consulta (sin borrar nada)."]
        cta = ("Actualizar método de pago", plan_url)
        subject = "Problema con el cobro de tu suscripción a Sportink"
    import re as _re
    text = "\n\n".join(_re.sub(r"<[^>]+>", "", p) for p in [title, *paras]) + (f"\n\n{cta[0]}: {cta[1]}" if cta else "")
    return subject, _email_html(title, paras, cta, club or "Sportink"), text


def send_billing_email_sync(club_id: str, key: str, invoice: Optional[dict] = None) -> bool:
    import mailer
    kind = key.split(":")[0]
    if not mailer.configured():
        logger.info("Email %s para %s no enviado: falta configurar SMTP", kind, club_id)
        return False
    to = _admin_emails_sync(club_id)
    if not to:
        return False
    if not _claim_email_sync(club_id, key):
        return False
    sub = _get_or_create_sync(club_id)
    sub["plan_interes"] = _plan_interes_sync(club_id)
    club = _club_info_sync(club_id).get("nombre") or ""
    try:
        base = _frontend_base(None)
    except HTTPException:
        base = ""
    subject, html, text = _build_email(kind, club, sub, base, invoice)
    ok = mailer.send_email(to, subject, html, text)
    if not ok:
        _unclaim_email_sync(club_id, key)  # se reintentará en la siguiente pasada
    return ok


def send_billing_email_safe(club_id: str, key: str, invoice: Optional[dict] = None) -> bool:
    try:
        return send_billing_email_sync(club_id, key, invoice)
    except Exception as exc:
        logger.warning("Email %s de %s falló: %s", key, club_id, exc)
        return False


def run_billing_emails_sync() -> dict:
    """Pasada periódica: recordatorios de fin de prueba (y bienvenidas pendientes)."""
    import mailer
    if not mailer.configured():
        return {"enviados": 0, "motivo": "smtp_no_configurado"}
    sent = {}
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""
            select club_id::text,
              case
                when trial_ends_at > now() + interval '3 days' and trial_ends_at <= now() + interval '7 days' then 'prueba_7'
                when trial_ends_at > now() and trial_ends_at <= now() + interval '3 days' then 'prueba_3'
                when trial_ends_at <= now() and trial_ends_at > now() - interval '7 days' then 'prueba_fin'
                when created_at > now() - interval '3 days' then 'bienvenida'
              end as kind
            from public.club_subscriptions
            where not comped and status = 'trialing' and stripe_subscription_id is null and trial_ends_at is not null
        """)
        rows = [(c, k) for c, k in cur.fetchall() if k]
    for club_id, kind in rows:
        # La bienvenida también vale si se perdió (p. ej. SMTP caído al crear el club).
        if send_billing_email_safe(club_id, kind):
            sent[kind] = sent.get(kind, 0) + 1
    return {"enviados": sum(sent.values()), "detalle": sent}


async def billing_email_loop():
    """Cada hora. Arranca con el servidor si hay SMTP (BILLING_EMAILS=0 lo desactiva)."""
    await asyncio.sleep(60)
    while True:
        try:
            r = await _io(run_billing_emails_sync)
            if r.get("enviados"):
                logger.info("Emails de suscripción: %s", r)
        except Exception as exc:
            logger.warning("Pasada de emails de suscripción falló: %s", exc)
        await asyncio.sleep(3600)


def start_background_tasks():
    import mailer
    if _env("BILLING_EMAILS", "1").lower() in ("0", "false", "no", "off") or not mailer.configured():
        return
    asyncio.get_event_loop().create_task(billing_email_loop())


@router.post("/billing/cron")
async def billing_cron(request: Request):
    """Para un cron externo (Render Cron, GitHub Actions): cabecera X-Cron-Secret = CRON_SECRET."""
    secret = _env("CRON_SECRET")
    if not secret or request.headers.get("x-cron-secret") != secret:
        raise HTTPException(status_code=404, detail="Not Found")
    return await _io(run_billing_emails_sync)


# ------------------------------------------------------------------ plataforma (dueños de Sportink)
def _is_platform_admin(user: dict) -> bool:
    emails = {e.strip().lower() for e in _env("PLATFORM_ADMIN_EMAILS").split(",") if e.strip()}
    return bool(emails) and (user.get("email") or "").lower() in emails


async def require_platform_admin(user=Depends(get_current_user)):
    if not _is_platform_admin(user):
        raise HTTPException(status_code=404, detail="Not Found")
    return user


class AdminClubIn(BaseModel):
    comped: Optional[bool] = None
    plan: Optional[Literal["basico", "club", "premium"]] = None
    extender_prueba_dias: Optional[int] = Field(default=None, ge=1, le=365)
    notes: Optional[str] = Field(default=None, max_length=300)


@router.get("/billing/admin/clubs")
async def admin_list_clubs(user=Depends(require_platform_admin)):
    def run():
        with _connect() as conn, conn.cursor() as cur:
            cur.execute("select c.id::text, c.nombre, c.slug, s.plan, s.status, s.comped, s.trial_ends_at, "
                        "s.current_period_end, s.billing_interval, s.stripe_customer_id "
                        "from public.clubs c left join public.club_subscriptions s on s.club_id = c.id order by c.nombre")
            cols = ["club_id", "nombre", "slug", "plan", "estado", "gratis", "prueba_hasta", "renueva_el", "intervalo", "stripe_customer_id"]
            return [{k: (_iso(v) if isinstance(v, datetime) else v) for k, v in zip(cols, r)} for r in cur.fetchall()]
    return await _io(run)


@router.put("/billing/admin/clubs/{club_id}")
async def admin_update_club(club_id: str, data: AdminClubIn, user=Depends(require_platform_admin)):
    sub = await _io(_get_or_create_sync, club_id)
    changes: dict = {}
    if data.comped is not None:
        changes["comped"] = data.comped
    if data.plan:
        changes["plan"] = data.plan
    if data.notes is not None:
        changes["notes"] = data.notes
    if data.extender_prueba_dias:
        base = max(sub.get("trial_ends_at") or _now(), _now())
        changes["trial_ends_at"] = base + timedelta(days=data.extender_prueba_dias)
        if not sub.get("stripe_subscription_id"):
            changes["status"] = "trialing"
    row = await _io(_update_sync, club_id, changes)
    _invalidate(club_id)
    logger.info("Plataforma: %s cambia la suscripción del club %s: %s", user.get("email"), club_id, list(changes))
    return _sub_out(row, compute_access(row))
