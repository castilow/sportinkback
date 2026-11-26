"""Planes y suscripciones (Stripe).

Las pruebas offline no tocan nada externo. Las de BD necesitan un Postgres
TEMPORAL con las migraciones aplicadas (BILLING_TEST_DATABASE_URL, o
CAJA_TEST_DATABASE_URL); Stripe se sustituye siempre por un doble local.
"""
import asyncio
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

BACKEND = os.environ.get("BACKEND_SOURCE", str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, BACKEND)
import postgres_compat  # noqa: E402
with patch.dict(os.environ, {"DATABASE_URL": "postgresql://offline:offline@localhost/offline"}), \
     patch.object(postgres_compat.PostgresCompatDB, "_ensure_schema", lambda self: None):
    import deps  # noqa: E402,F401
    from routes import billing  # noqa: E402

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)
PRICES = {
    "STRIPE_PRICE_BASICO_MONTH": "price_basico_m", "STRIPE_PRICE_BASICO_YEAR": "price_basico_y",
    "STRIPE_PRICE_CLUB_MONTH": "price_club_m", "STRIPE_PRICE_CLUB_YEAR": "price_club_y",
    "STRIPE_PRICE_PREMIUM_MONTH": "price_premium_m",
}


def run(coro):
    return asyncio.run(coro)


def stripe_sub(sub_id="sub_1", status="active", price="price_club_m", club=None, customer="cus_1",
               period_end=None, cancel=False, trial_end=None):
    return {
        "id": sub_id, "object": "subscription", "status": status, "customer": customer,
        "cancel_at_period_end": cancel, "trial_end": trial_end,
        "metadata": {"club_id": club} if club else {},
        # Forma de la API actual: el fin de periodo va en el item.
        "items": {"data": [{"price": {"id": price, "recurring": {"interval": "month"}},
                            "current_period_end": period_end or int((NOW + timedelta(days=30)).timestamp())}]},
    }


# ---------------------------------------------------------------- offline
def test_prueba_propia_da_todo_y_avisa_al_final():
    acc = billing.compute_access({"status": "trialing", "trial_ends_at": NOW + timedelta(days=10)}, NOW)
    assert acc["nivel"] == "full" and acc["plan"] == "premium" and acc["dias_prueba"] == 10 and not acc["aviso"]
    acc = billing.compute_access({"status": "trialing", "trial_ends_at": NOW + timedelta(days=2, hours=3)}, NOW)
    assert acc["dias_prueba"] == 3 and "3 días" in acc["aviso"]
    acc = billing.compute_access({"status": "trialing", "trial_ends_at": NOW - timedelta(minutes=1)}, NOW)
    assert acc["nivel"] == "read_only" and acc["motivo"] == "trial_expired"


def test_estados_de_stripe():
    base = {"stripe_subscription_id": "sub_1", "plan": "basico", "current_period_end": NOW + timedelta(days=3)}
    acc = billing.compute_access({**base, "status": "active"}, NOW)
    assert acc["nivel"] == "full" and acc["plan"] == "basico" and "caja" not in acc["modulos"]
    assert acc["limites"] == {"equipos": 10, "jugadores": 150}
    # Impago: periodo de gracia y luego solo lectura
    acc = billing.compute_access({**base, "status": "past_due", "current_period_end": NOW - timedelta(days=2)}, NOW)
    assert acc["nivel"] == "full" and acc["aviso"]
    acc = billing.compute_access({**base, "status": "past_due", "current_period_end": NOW - timedelta(days=9)}, NOW)
    assert acc["nivel"] == "read_only"
    for st in ("canceled", "unpaid", "incomplete_expired", "paused"):
        assert billing.compute_access({**base, "status": st}, NOW)["nivel"] == "read_only"
    # Club gratis
    acc = billing.compute_access({"status": "canceled", "comped": True, "plan": "premium"}, NOW)
    assert acc["nivel"] == "full" and "marca" in acc["modulos"]


def test_modulos_por_ruta():
    assert billing._module_for("/caja/movimientos", "GET") == "caja"
    assert billing._module_for("/stock", "POST") == "stock"
    assert billing._module_for("/branding", "GET") is None          # el login lee la marca
    assert billing._module_for("/branding", "PUT") == "marca"
    assert billing._module_for("/players", "GET") is None


def test_campos_de_suscripcion_api_nueva_y_antigua(monkeypatch):
    for k, v in PRICES.items():
        monkeypatch.setenv(k, v)
    f = billing._subscription_fields(stripe_sub(price="price_basico_y"))
    assert f["plan"] == "basico" and f["billing_interval"] == "year" and f["status"] == "active"
    assert f["current_period_end"] and f["stripe_customer_id"] == "cus_1"
    old = stripe_sub(price="price_desconocido")
    old["current_period_end"] = 1790000000
    old["metadata"] = {"plan": "club"}
    f = billing._subscription_fields(old)
    assert f["plan"] == "club" and f["current_period_end"].year == 2026
    assert billing._invoice_subscription_id({"parent": {"subscription_details": {"subscription": "sub_9"}}}) == "sub_9"
    assert billing._invoice_subscription_id({"subscription": "sub_8"}) == "sub_8"


def test_webhook_sin_firma_valida_se_rechaza(monkeypatch):
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    app = FastAPI()
    app.include_router(billing.router, prefix="/api")

    async def req():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/api/billing/webhook", content=b'{"id":"evt_1"}', headers={"stripe-signature": "t=1,v1=malo"})
    assert run(req()).status_code == 400


# ---------------------------------------------------------------- con BD
class FakeStripe:
    """Doble mínimo de StripeClient (solo lo que usa billing.py)."""

    def __init__(self):
        self.subs, self.calls = {}, []
        outer = self

        class Subs:
            def retrieve(self, sub_id, params=None, options=None):
                outer.calls.append(("sub.retrieve", sub_id))
                return outer.subs[sub_id]

            def update(self, sub_id, params=None, options=None):
                outer.calls.append(("sub.update", sub_id, params))
                sub = json.loads(json.dumps(outer.subs[sub_id]))
                sub["items"]["data"][0]["price"] = {"id": params["items"][0]["price"], "recurring": {"interval": "month"}}
                sub["metadata"].update(params.get("metadata") or {})
                outer.subs[sub_id] = sub
                return sub

        class Customers:
            def create(self, params=None, options=None):
                outer.calls.append(("customer.create", params, options))
                return {"id": "cus_nuevo"}

        class Sessions:
            def create(self, params=None, options=None):
                outer.calls.append(("checkout.create", params))
                return {"id": "cs_1", "url": "https://checkout.stripe.test/cs_1"}

            def retrieve(self, sid, params=None, options=None):
                return {"id": sid, "subscription": "sub_1", "metadata": {"club_id": outer.club}}

        class Portal:
            def create(self, params=None, options=None):
                outer.calls.append(("portal.create", params))
                return {"url": "https://billing.stripe.test/p"}

        class Prices:
            def retrieve(self, pid, params=None, options=None):
                return {"id": pid, "unit_amount": 4900, "currency": "eur"}

        self.v1 = SimpleNamespace(subscriptions=Subs(), customers=Customers(),
                                  checkout=SimpleNamespace(sessions=Sessions()),
                                  billing_portal=SimpleNamespace(sessions=Portal()), prices=Prices())


@pytest.fixture
def env(monkeypatch):
    dsn = os.environ.get("BILLING_TEST_DATABASE_URL") or os.environ.get("CAJA_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Requiere Postgres temporal con migraciones: BILLING_TEST_DATABASE_URL")
    from psycopg.conninfo import conninfo_to_dict
    if conninfo_to_dict(dsn).get("host", "") not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail("Las pruebas de facturación solo permiten Postgres local temporal")
    with patch.object(postgres_compat.PostgresCompatDB, "_ensure_schema", lambda self: None):
        db = postgres_compat.PostgresCompatDB(dsn)
    monkeypatch.setattr(billing, "db", db)
    for k, v in PRICES.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_local")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setenv("FRONTEND_URL", "https://app.sportink.test")
    monkeypatch.setenv("BILLING_ENFORCE", "1")
    club = str(uuid.uuid4())
    with psycopg.connect(dsn) as conn:
        conn.execute("insert into public.clubs (id, slug, nombre) values (%s, %s, %s)", (club, "t-" + club[:8], "CD Prueba Pagos"))
    fake = FakeStripe()
    fake.club = club
    monkeypatch.setattr(billing, "_client", lambda: fake)
    billing._invalidate()
    user = {"id": "u-admin", "role": "admin", "club_id": club, "email": "dir@prueba.test", "name": "Dir"}
    app = FastAPI()

    async def fake_user():
        return dict(user)
    app.dependency_overrides[deps.get_current_user] = fake_user
    monkeypatch.setattr(billing, "get_current_user", lambda request: fake_user())

    from fastapi import APIRouter
    api = APIRouter()

    @api.get("/players")
    async def players():
        return []

    @api.post("/players")
    async def new_player():
        return {"ok": True}

    @api.post("/teams")
    async def new_team():
        return {"ok": True}

    @api.get("/caja/dia")
    async def caja():
        return {"ok": True}

    api.include_router(billing.router)
    # Igual que en server.py: toda la API pasa por billing_guard.
    app.include_router(api, prefix="/api", dependencies=[Depends(billing.billing_guard)])
    yield SimpleNamespace(dsn=dsn, club=club, fake=fake, app=app, user=user)
    with psycopg.connect(dsn) as conn:
        conn.execute("delete from public.clubs where id = %s", (club,))
        conn.execute("delete from public.stripe_events where id like 'evt_test_%%'")
    billing._invalidate()


def call(s, method, path, **kw):
    async def go():
        async with AsyncClient(transport=ASGITransport(app=s.app), base_url="http://t") as c:
            return await c.request(method, path, **kw)
    return run(go())


def signed(payload: dict, secret="whsec_test"):
    body = json.dumps(payload).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"stripe-signature": f"t={t},v1={sig}", "content-type": "application/json"}


def sql(s, q, *args):
    with psycopg.connect(s.dsn) as conn:
        cur = conn.execute(q, args)
        return cur.fetchall() if cur.description else None


def test_club_nuevo_empieza_en_prueba_y_el_rayo_es_gratis(env):
    r = call(env, "GET", "/api/billing/me")
    assert r.status_code == 200
    d = r.json()
    assert d["estado"] == "trialing" and d["acceso"]["nivel"] == "full" and d["acceso"]["dias_prueba"] == 30
    assert d["puede_gestionar"] and d["uso"] == {"equipos": 0, "jugadores": 0}
    rayo = sql(env, "select s.comped, s.status from public.club_subscriptions s join public.clubs c on c.id = s.club_id where c.is_default")
    assert rayo == [(True, "active")]


def test_pagar_con_checkout_y_webhook(env):
    call(env, "GET", "/api/billing/me")
    r = call(env, "POST", "/api/billing/checkout", json={"plan": "club", "intervalo": "month"})
    assert r.status_code == 200 and r.json()["url"].startswith("https://checkout.stripe.test")
    kind, params = env.fake.calls[-1]
    assert kind == "checkout.create" and params["line_items"][0]["price"] == "price_club_m"
    assert params["customer"] == "cus_nuevo" and params["metadata"]["club_id"] == env.club
    assert "trial_end" in params["subscription_data"]          # conserva los días de prueba
    assert params["success_url"].startswith("https://app.sportink.test/plan?pago=ok")
    # cliente creado una sola vez, con clave de idempotencia
    assert [c for c in env.fake.calls if c[0] == "customer.create"][0][2]["idempotency_key"].endswith(env.club)

    env.fake.subs["sub_1"] = stripe_sub(club=env.club, customer="cus_nuevo")
    body, headers = signed({"id": "evt_test_1", "type": "checkout.session.completed",
                            "data": {"object": {"mode": "subscription", "subscription": "sub_1", "client_reference_id": env.club,
                                                "metadata": {"club_id": env.club}}}})
    r = call(env, "POST", "/api/billing/webhook", content=body, headers=headers)
    assert r.status_code == 200 and r.json()["resultado"] == "aplicado"
    r = call(env, "POST", "/api/billing/webhook", content=body, headers=headers)
    assert r.json().get("duplicado") is True                     # Stripe reintenta: no se procesa dos veces
    d = call(env, "GET", "/api/billing/me").json()
    assert d["estado"] == "active" and d["plan"] == "club" and d["intervalo"] == "month" and d["renueva_el"]
    # Con suscripción activa no se abre otro checkout: se gestiona en el portal
    assert call(env, "POST", "/api/billing/checkout", json={"plan": "premium"}).status_code == 409
    assert call(env, "POST", "/api/billing/portal").json()["url"].startswith("https://billing.stripe.test")


def test_impago_y_cancelacion_dejan_solo_lectura(env):
    call(env, "GET", "/api/billing/me")
    sql(env, "update public.club_subscriptions set stripe_customer_id = 'cus_x' where club_id = %s", env.club)
    body, headers = signed({"id": "evt_test_2", "type": "customer.subscription.deleted",
                            "data": {"object": stripe_sub(sub_id="sub_2", status="canceled", customer="cus_x")}})
    assert call(env, "POST", "/api/billing/webhook", content=body, headers=headers).status_code == 200
    billing._invalidate()
    assert call(env, "GET", "/api/players").status_code == 200        # puede consultar
    r = call(env, "POST", "/api/players")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "subscription_inactive"
    assert call(env, "GET", "/api/billing/me").status_code == 200      # y ver cómo reactivar


def test_evento_tardio_de_suscripcion_antigua_no_pisa_la_nueva(env):
    call(env, "GET", "/api/billing/me")
    billing.apply_subscription_sync(env.club, stripe_sub(sub_id="sub_nueva", club=env.club))
    billing.apply_subscription_sync(env.club, stripe_sub(sub_id="sub_vieja", status="canceled", club=env.club))
    row = sql(env, "select status, stripe_subscription_id from public.club_subscriptions where club_id = %s", env.club)
    assert row == [("active", "sub_nueva")]


def test_modulos_y_limites_del_plan_basico(env):
    call(env, "GET", "/api/billing/me")
    billing.apply_subscription_sync(env.club, stripe_sub(price="price_basico_m", club=env.club))
    r = call(env, "GET", "/api/caja/dia")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "plan_upgrade_required"
    from players_store import TEMPORADA
    with psycopg.connect(env.dsn) as conn:
        sport = conn.execute("select id from public.sports limit 1").fetchone()[0]
        for i in range(10):
            conn.execute("insert into public.teams (club_id, sport_id, nombre, categoria, temporada, activo) values (%s, %s, %s, 'Alevín', %s, true)",
                         (env.club, sport, f"Equipo {i}", TEMPORADA))
    billing._invalidate()
    r = call(env, "POST", "/api/teams")
    assert r.status_code == 402 and r.json()["detail"]["code"] == "plan_limit_reached" and r.json()["detail"]["maximo"] == 10
    assert call(env, "POST", "/api/players").status_code == 200      # jugadores aún por debajo del límite
    sql(env, "delete from public.teams where club_id = %s", env.club)


def test_sin_billing_enforce_no_se_bloquea_nada(env, monkeypatch):
    call(env, "GET", "/api/billing/me")
    sql(env, "update public.club_subscriptions set trial_ends_at = now() - interval '1 day' where club_id = %s", env.club)
    billing._invalidate()
    assert call(env, "POST", "/api/players").status_code == 402
    monkeypatch.setenv("BILLING_ENFORCE", "0")
    assert call(env, "POST", "/api/players").status_code == 200


def test_sync_tras_volver_de_stripe(env):
    call(env, "GET", "/api/billing/me")
    env.fake.subs["sub_1"] = stripe_sub(club=env.club, price="price_premium_m")
    r = call(env, "POST", "/api/billing/sync", params={"session_id": "cs_1"})
    assert r.status_code == 200 and r.json()["plan"] == "premium" and r.json()["estado"] == "active"



# ---------------------------------------------------------------- mejoras: importaciones, cambio de plan, emails, plan elegido
def add_teams(s, n):
    from players_store import TEMPORADA
    with psycopg.connect(s.dsn) as conn:
        sport = conn.execute("select id from public.sports limit 1").fetchone()[0]
        for i in range(n):
            conn.execute("insert into public.teams (club_id, sport_id, nombre, categoria, temporada, activo) values (%s, %s, %s, 'Alevín', %s, true)",
                         (s.club, sport, f"Eq {uuid.uuid4().hex[:6]}", TEMPORADA))


def test_importar_cuenta_lo_que_trae_el_archivo(env):
    call(env, "GET", "/api/billing/me")
    billing.apply_subscription_sync(env.club, stripe_sub(price="price_basico_m", club=env.club))
    add_teams(env, 8)
    info = run(billing.capacity_info(env.club, "equipos", 3))
    assert info["permitido"] is False and info["libre"] == 2 and "como máximo 2" in info["mensaje"]
    assert run(billing.capacity_info(env.club, "equipos", 2))["permitido"] is True
    with pytest.raises(HTTPException) as e:
        run(billing.ensure_capacity(env.user, "equipos", 3))
    assert e.value.status_code == 402 and e.value.detail["code"] == "plan_limit_reached"
    run(billing.ensure_capacity(env.user, "jugadores", 150))          # justo en el límite: vale
    with pytest.raises(HTTPException):
        run(billing.ensure_capacity(env.user, "jugadores", 151))
    sql(env, "delete from public.teams where club_id = %s", env.club)


def test_cambiar_de_plan_y_no_bajar_si_no_cabe(env):
    call(env, "GET", "/api/billing/me")
    env.fake.subs["sub_1"] = stripe_sub(club=env.club, price="price_club_m")
    env.fake.subs["sub_1"]["items"]["data"][0]["id"] = "si_1"
    billing.apply_subscription_sync(env.club, env.fake.subs["sub_1"])
    add_teams(env, 12)                                               # Básico permite 10
    r = call(env, "POST", "/api/billing/change-plan", json={"plan": "basico", "intervalo": "month"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "downgrade_over_limit"
    assert "12 equipos" in r.json()["detail"]["message"]
    r = call(env, "POST", "/api/billing/change-plan", json={"plan": "premium", "intervalo": "month"})
    assert r.status_code == 200 and r.json()["plan"] == "premium"
    upd = [c for c in env.fake.calls if c[0] == "sub.update"][-1]
    assert upd[2]["items"] == [{"id": "si_1", "price": "price_premium_m"}] and upd[2]["proration_behavior"] == "create_prorations"
    assert call(env, "POST", "/api/billing/change-plan", json={"plan": "premium"}).status_code == 409   # ya lo tiene
    sql(env, "delete from public.teams where club_id = %s", env.club)


@pytest.fixture
def mail(env, monkeypatch):
    import mailer
    uid = str(uuid.uuid4())
    mine = f"dir-{uid[:6]}@club.test"
    sent = []
    # Solo los correos de ESTE club (la pasada periódica recorre todos los clubes de la BD).
    monkeypatch.setattr(mailer, "configured", lambda: True)
    monkeypatch.setattr(mailer, "send_email",
                        lambda to, subject, html, text: (sent.append((to, subject, html, text)) if mine in to else None) or True)
    with psycopg.connect(env.dsn) as conn:
        conn.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"dir-{uid[:6]}@club.test"))
        conn.execute("insert into public.profiles (id, email, nombre, rol, club_id) values (%s, %s, 'Dir', 'admin', %s)",
                     (uid, f"dir-{uid[:6]}@club.test", env.club))
    yield sent
    with psycopg.connect(env.dsn) as conn:
        conn.execute("delete from public.profiles where id = %s", (uid,))
        conn.execute("delete from auth.users where id = %s", (uid,))


def test_emails_de_prueba_sin_duplicados(env, mail):
    billing._get_or_create_sync(env.club)
    time.sleep(0.5)                                                  # la bienvenida sale en un hilo
    assert [m[1] for m in mail] == ["Bienvenido a Sportink: tu prueba de 30 días ha empezado"]
    assert "CD Prueba Pagos" in mail[0][2] and mail[0][0][0].endswith("@club.test")
    sql(env, "update public.club_subscriptions set trial_ends_at = now() + interval '5 days' where club_id = %s", env.club)
    billing.run_billing_emails_sync()
    billing.run_billing_emails_sync()                                # segunda pasada: nada nuevo
    sql(env, "update public.club_subscriptions set trial_ends_at = now() + interval '2 days' where club_id = %s", env.club)
    billing.run_billing_emails_sync()
    sql(env, "update public.club_subscriptions set trial_ends_at = now() - interval '1 hour' where club_id = %s", env.club)
    billing.run_billing_emails_sync()
    subjects = [m[1] for m in mail]
    assert subjects[1:] == ["Tu prueba de Sportink termina en 7 días", "Quedan 3 días de tu prueba de Sportink",
                            "Tu prueba de Sportink ha terminado"]
    assert "/plan" in mail[-1][2] and "modo consulta" in mail[-1][3]


def test_email_de_pago_fallido(env, mail):
    billing._get_or_create_sync(env.club)
    time.sleep(0.3)
    mail.clear()
    env.fake.subs["sub_9"] = stripe_sub(sub_id="sub_9", status="past_due", club=env.club)
    body, headers = signed({"id": "evt_test_fail", "type": "invoice.payment_failed",
                            "data": {"object": {"id": "in_1", "amount_due": 11979,
                                                "parent": {"subscription_details": {"subscription": "sub_9"}}}}})
    assert call(env, "POST", "/api/billing/webhook", content=body, headers=headers).status_code == 200
    assert [m[1] for m in mail] == ["Problema con el cobro de tu suscripción a Sportink"] and "119,79 €" in mail[0][3]


def test_plan_elegido_en_la_web(env):
    sql(env, "insert into public.public_registrations (id, email, admin_name, payload, status, club_id) "
             "values (gen_random_uuid(), %s, 'Ana', %s::jsonb, 'completed', %s)",
        f"a-{env.club[:6]}@x.test", json.dumps({"plan_interes": "club"}), env.club)
    d = call(env, "GET", "/api/billing/me").json()
    assert d["plan_interes"] == "club"
    sql(env, "delete from public.public_registrations where club_id = %s", env.club)
