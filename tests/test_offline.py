"""Tests que NO necesitan base de datos ni red.  Ejecutar:  pytest tests/test_offline.py -q"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import players_store  # noqa: E402
from postgres_compat import PostgresCollection as AppCollection  # noqa: E402


def test_sql_prefilter_only_pushes_club_and_id_strings():
    f = AppCollection._sql_prefilter
    assert f(None) == ""
    assert f({}) == ""
    assert "doc->>'club_id' = 'abc'" in f({"club_id": "abc"})
    assert "doc->>'id' = 'x1'" in f({"id": "x1", "club_id": "abc"})
    # operadores, listas y valores no-texto NO se empujan a SQL (siguen filtrándose en Python)
    assert f({"club_id": {"$in": ["a", "b"]}}) == ""
    assert f({"club_id": None}) == ""
    assert f({"team": "Juvenil A"}) == ""


def test_sql_prefilter_escapes_quotes():
    out = AppCollection._sql_prefilter({"club_id": "a'; drop table x; --"})
    assert "''" in out  # la comilla simple queda escapada


class _FakeDb:
    """Registra el SQL que se ejecutaría."""

    def __init__(self):
        self.sql = []

    async def execute(self, sql):
        self.sql.append(sql)
        return "[]"


def test_players_fetch_is_filtered_by_club_in_sql():
    db = _FakeDb()
    club = "11111111-1111-1111-1111-111111111111"
    asyncio.run(players_store.fetch_all_sql_players(db, club))
    assert f"p.club_id = '{club}'::uuid" in db.sql[0]


def test_players_fetch_rejects_invalid_club_id_without_touching_db():
    db = _FakeDb()
    out = asyncio.run(players_store.fetch_all_sql_players(db, "no-es-un-uuid' or '1'='1"))
    assert out == []
    assert db.sql == []  # ni siquiera se lanza la consulta


def test_players_cache_is_per_club_and_invalidation_is_targeted():
    calls = []

    async def fake_fetch(db, club_id=None):
        calls.append(club_id)
        return [{"id": f"p-{club_id}", "club_id": club_id}]

    original = players_store.fetch_all_sql_players
    players_store.fetch_all_sql_players = fake_fetch
    try:
        store = players_store.PlayersStore(db=object(), fallback_collection=object())
        asyncio.run(store._load("A"))
        asyncio.run(store._load("B"))
        asyncio.run(store._load("A"))
        assert calls == ["A", "B"]          # A se sirve de caché
        store._invalidate("B")              # escribe el club B...
        asyncio.run(store._load("A"))
        assert calls == ["A", "B"]          # ...y la caché de A sigue intacta
        asyncio.run(store._load("B"))
        assert calls == ["A", "B", "B"]     # la de B se recarga
        store._invalidate()                 # sin club conocido: se vacía todo
        asyncio.run(store._load("A"))
        assert calls[-1] == "A"
    finally:
        players_store.fetch_all_sql_players = original
