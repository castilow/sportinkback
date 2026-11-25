"""Control de hojas: emparejar partidos RFFM con hojas enviadas."""
from routes.sheets import _iso_day, _norm_team, _pair_sheets, _rffm_index, _sheet_status, _week_bounds


def test_fechas_rffm_y_app():
    assert _iso_day("22-11-2026") == "2026-11-22"
    assert _iso_day("22/11/2026") == "2026-11-22"
    assert _iso_day("2026-11-22T10:00:00") == "2026-11-22"
    assert _iso_day("") is None and _iso_day(None) is None


def test_semana_explicita_y_ordenada():
    assert _week_bounds("2026-11-22", "2026-11-16") == ("2026-11-16", "2026-11-22")
    d, h = _week_bounds("2026-11-19", None)
    assert (d, h) == ("2026-11-16", "2026-11-22")


def test_nombres_de_equipo_sin_tildes_ni_mayusculas():
    assert _norm_team("Alevín  A") == _norm_team("ALEVIN A")
    idx = _rffm_index(["Alevín A", "Infantil B"], [
        {"team_name": "ALEVIN A", "match_date": "22-11-2026"},
        {"team_name": "CF Rayo Majadahonda Infantil B", "match_date": "22-11-2026"},
    ])
    assert len(idx["Alevín A"]) == 1
    assert len(idx["Infantil B"]) == 1  # coincide por palabras contenidas


def test_emparejado_con_un_dia_de_margen_y_amistosos_aparte():
    fx = [{"date": "2026-11-22"}, {"date": "2026-11-15"}]
    sheets = [
        {"id": "a", "_day": "2026-11-23"},  # la rellenó al día siguiente con la fecha mal
        {"id": "b", "_day": "2026-11-19"},  # amistoso entre semana
    ]
    pairs, extra = _pair_sheets(fx, sheets)
    by = {f["date"]: sh for f, sh in pairs}
    assert by["2026-11-22"]["id"] == "a"
    assert by["2026-11-15"] is None
    assert [s["id"] for s in extra] == ["b"]


def test_estados():
    today = "2026-11-25"
    assert _sheet_status("2026-11-22", None, today) == "falta"
    assert _sheet_status("2026-11-25", None, today) == "hoy"
    assert _sheet_status("2026-11-29", None, today) == "proxima"
    assert _sheet_status("2026-11-22", {"created_at": "2026-11-22T20:00:00+00:00"}, today) == "enviada"
    assert _sheet_status("2026-11-22", {"created_at": "2026-11-25T20:00:00+00:00"}, today) == "tarde"
