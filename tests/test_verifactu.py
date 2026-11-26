"""VeriFactu (Dinvbox) en la Caja.

Offline: construcción del registro, QR y validaciones. Con BD (Postgres
TEMPORAL: CAJA_TEST_DATABASE_URL): cobro → envío → webhook → anulación.
Dinvbox se sustituye siempre por un doble local (no sale nada a internet).
"""
import asyncio
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import psycopg
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

BACKEND = os.environ.get("BACKEND_SOURCE", str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, BACKEND)
import postgres_compat  # noqa: E402
with patch.dict(os.environ, {"DATABASE_URL": "postgresql://offline:offline@localhost/offline"}), \
     patch.object(postgres_compat.PostgresCompatDB, "_ensure_schema", lambda self: None):
    import deps  # noqa: E402,F401
    from routes import caja, stock, verifactu  # noqa: E402

SAD = {"nombre": "Club de Fútbol Rayo Majadahonda, S.A.D.", "cif": "A-28562445", "serie": "SAD", "exenta_iva": False, "verifactu": True}
FUN = {"nombre": "Fundación Rayo Majadahonda", "cif": "G88543145", "serie": "FUN", "exenta_iva": True, "verifactu": True}


def run(coro):
    return asyncio.run(coro)


def mov(**kw):
    d = {"id": "m1", "tipo": "ingreso", "numero": "SAD-2026-00001", "fecha": "2026-11-26", "hora": "18:30",
         "importe_c": 33500, "iva": 21, "base_c": 27686, "cuota_iva_c": 5814, "concepto_nombre": "Indumentaria · Común"}
    d.update(kw)
    return d


# ---------------------------------------------------------------- offline
def test_registro_simplificada_con_iva():
    doc = mov()
    doc["verifactu"] = verifactu.prepare(doc, SAD)
    body = verifactu.build_alta(doc, SAD)
    assert body["TipoFactura"] == "F2" and "IDDestinatario" not in body
    assert body["NIF"] == body["IDEmisorFactura"] == "A28562445"
    assert body["NumSerieFactura"] == "SAD-2026-00001" and body["RefExterna"] == "m1"
    assert body["FechaExpedicionFactura"] == "2026-11-26T17:30:00.000Z"          # 18:30 en Madrid = 17:30 UTC
    d = body["Desglose"][0]
    assert d == {"Impuesto": "01", "ClaveRegimen": "01", "CalificacionOperacion": "S1", "TipoImpositivo": 21,
                 "BaseImponibleOimporteNoSujeto": 276.86, "CuotaRepercutida": "58.14"}
    assert body["CuotaTotal"] == "58.14" and body["ImporteTotal"] == 335.0


def test_registro_exento_fundacion_y_factura_completa():
    doc = mov(numero="FUN-2026-00001", importe_c=5000, iva=0, base_c=None, cuota_iva_c=None, concepto_nombre="Cuota")
    doc["verifactu"] = verifactu.prepare(doc, FUN, "b-12959755", "Empresa Ejemplo S.L.")
    body = verifactu.build_alta(doc, FUN)
    assert body["TipoFactura"] == "F1"
    assert body["IDDestinatario"] == {"NombreRazon": "Empresa Ejemplo S.L.", "NIF": "B12959755"}
    assert body["Desglose"] == [{"MotivoExento": "E1", "BaseImponibleOimporteNoSujeto": 50.0}]
    assert body["ImporteTotal"] == 50.0 and "CuotaTotal" not in body


def test_validaciones_y_qr():
    with pytest.raises(HTTPException) as e:
        verifactu.prepare(mov(importe_c=45000), SAD)                       # > 400 € sin NIF
    assert "400" in e.value.detail
    with pytest.raises(HTTPException):
        verifactu.prepare(mov(), SAD, "123", "X")                          # NIF raro
    with pytest.raises(HTTPException):
        verifactu.prepare(mov(iva=None), SAD)                              # sin IVA configurado
    with pytest.raises(HTTPException):
        verifactu.prepare(mov(), {**SAD, "cif": ""})                       # entidad sin CIF
    assert verifactu.prepare(mov(), {**SAD, "verifactu": False}) is None  # entidad sin VeriFactu
    assert verifactu.prepare(mov(tipo="gasto"), SAD) is None              # las salidas no son facturas
    url = verifactu.qr_url("A-28562445", "SAD-2026-00001", "2026-11-26", 33500)
    assert url.startswith(verifactu.QR_PROD) and "nif=A28562445" in url and "fecha=26-11-2026" in url and "importe=335.00" in url
    assert verifactu.qr_svg(url).startswith("<svg")


def test_respuestas_de_dinvbox(monkeypatch):
    saved = {}
    monkeypatch.setattr(verifactu, "_patch_vf_sync", lambda mid, p: saved.update(p))
    assert verifactu._apply_result("m", {"response": {"status": "en_proceso", "message": "Factura encolada correctamente", "job_id": 47}})["estado"] == "enviada"
    ok = verifactu._apply_result("m", {"status": 200, "error": False, "job_id": 50, "data": {
        "EstadoEnvio": "Correcto", "estado_registro": "Correcto", "CSV": "ABC", "Huella": "H", "ValidationUrl": "https://aeat/qr"}})
    assert ok["estado"] == "correcto" and ok["csv"] == "ABC" and ok["qr_url"] == "https://aeat/qr"
    bad = verifactu._apply_result("m", {"status": 200, "error": False, "job_id": 45, "data": {
        "EstadoEnvio": "Incorrecto", "ErrorCode": "1105", "ErrorDescription": "El valor del campo FechaExpedicionFactura es incorrecto."}})
    assert bad["estado"] == "rechazada" and bad["error_codigo"] == "1105"
    err = verifactu._apply_result("m", {"status": "200", "error": 10, "job_id": 31, "data": {"ErrorCode": 0, "ErrorDescription": "Undefined array key"}})
    assert err["estado"] == "error" and "Undefined" in err["error"]


# ---------------------------------------------------------------- con BD
class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p, self.text = status, payload, str(payload)

    def json(self):
        return self._p


@pytest.fixture
def database(monkeypatch):
    dsn = os.environ.get("CAJA_TEST_DATABASE_URL") or os.environ.get("STOCK_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Requiere Postgres temporal: CAJA_TEST_DATABASE_URL")
    from psycopg.conninfo import conninfo_to_dict
    if conninfo_to_dict(dsn).get("host", "") not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail("Solo Postgres local temporal")
    with patch.object(postgres_compat.PostgresCompatDB, "_ensure_schema", lambda self: None):
        db = postgres_compat.PostgresCompatDB(dsn)
    for mod in (caja, stock, verifactu):
        monkeypatch.setattr(mod, "db", db)
    monkeypatch.setenv("VERIFACTU_URL", "https://dinvbox.test")
    monkeypatch.setenv("VERIFACTU_TOKEN", "tok-test")
    monkeypatch.setenv("VERIFACTU_WEBHOOK_SECRET", "s3cr3t")
    club = str(uuid.uuid4())
    user = {"id": "u-office", "role": "office", "club_id": club, "name": "Paqui"}
    sent = []

    def fake_post(url, json=None, timeout=None, headers=None):
        sent.append((url, json, headers))
        return FakeResp(200, {"response": {"status": "en_proceso", "message": "Factura encolada correctamente", "job_id": 47 + len(sent)}})
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    # Configuración: SAD y Fundación con VeriFactu
    cfg = caja._merge_config(None)
    cfg["entidades"]["SAD"]["verifactu"] = True
    cfg["entidades"]["Fundación"]["verifactu"] = True
    run(caja.put_config(caja.ConfigIn(conceptos=cfg["conceptos"], entidades=cfg["entidades"], recibo=cfg["recibo"],
                                      motivos_salida=cfg["motivos_salida"]), user={**user, "role": "admin"}))
    yield SimpleNamespace(dsn=dsn, club=club, user=user, sent=sent)
    with psycopg.connect(dsn) as conn:
        conn.execute("delete from public.app_documents where doc->>'club_id' = %s", (club,))


def cobro(s, **kw):
    base = dict(tipo="ingreso", fecha="2026-11-26", concepto="indumentaria", opcion="Común",
                nombre="Familia García", importe=335, pago="tarjeta")
    base.update(kw)
    return run(caja.create_movimiento(caja.MovimientoIn(**base), user=s.user))


def test_cobro_se_envia_y_el_webhook_lo_confirma(database):
    m = cobro(database)
    assert m["verifactu"]["estado"] == "enviada" and m["verifactu"]["job_id"] == "48"
    url, body, headers = database.sent[0]
    assert url == "https://dinvbox.test/api/verifactu/alta" and headers["Authorization"] == "Bearer tok-test"
    assert body["NumSerieFactura"] == m["numero"] and body["TipoFactura"] == "F2"

    app = FastAPI()
    app.include_router(verifactu.router, prefix="/api")

    async def hook(payload, secret="s3cr3t"):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post(f"/api/verifactu/webhook?secret={secret}", json=payload)
    assert run(hook({"job_id": 48}, secret="mal")).status_code == 404
    r = run(hook({"status": 200, "error": False, "job_id": 48, "data": {
        "EstadoEnvio": "Correcto", "CSV": "CSV123", "Huella": "H", "ValidationUrl": "https://aeat/validar?x=1"}}))
    assert r.json() == {"ok": True, "estado": "correcto"}
    doc = verifactu._get_doc_sync(m["id"], database.club)
    assert doc["verifactu"]["estado"] == "correcto" and doc["verifactu"]["csv"] == "CSV123"
    assert doc["verifactu"]["qr_url"] == "https://aeat/validar?x=1"

    # No se puede cambiar el importe de una factura enviada; al anular queda pendiente en la AEAT
    with pytest.raises(HTTPException) as e:
        run(caja.update_movimiento(m["id"], caja.MovimientoUpdateIn(importe=300), user=database.user))
    assert e.value.status_code == 409
    a = run(caja.anular_movimiento(m["id"], caja.AnularIn(motivo="Error de talla"), user=database.user))
    assert a["verifactu"]["anulacion_pendiente"] is True


def test_fundacion_exenta_y_factura_completa(database):
    m = cobro(database, concepto="cuota", opcion="", importe=60, factura_nif="B12959755", factura_nombre="Empresa Ejemplo S.L.")
    body = database.sent[-1][1]
    assert m["verifactu"]["tipo"] == "F1" and body["Desglose"] == [{"MotivoExento": "E1", "BaseImponibleOimporteNoSujeto": 60.0}]
    with pytest.raises(HTTPException) as e:
        cobro(database, importe=450)                                       # > 400 € sin NIF: no se cobra
    assert "400" in e.value.detail


def test_si_dinvbox_falla_el_cobro_se_guarda_y_se_reintenta(database, monkeypatch):
    import requests

    def down(*a, **k):
        raise requests.ConnectionError("sin red")
    monkeypatch.setattr(requests, "post", down)
    m = cobro(database)
    assert m["numero"] and m["verifactu"]["estado"] == "error" and "conectar" in m["verifactu"]["error"]
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {"response": {"status": "en_proceso", "job_id": 99}}))
    res = run(verifactu.retry_one(m["id"], user=database.user))
    assert res["estado"] == "enviada" and res["job_id"] == "99"


def test_salidas_y_entidades_sin_verifactu_no_se_envian(database):
    n = len(database.sent)
    run(caja.create_movimiento(caja.MovimientoIn(tipo="gasto", fecha="2026-11-26", descripcion="Material", importe=20,
                                                 pago="efectivo"), user=database.user))
    assert len(database.sent) == n
