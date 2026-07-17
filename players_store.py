"""Capa de jugadores: lee/escribe public.players (Supabase SQL) o app_documents (JSONB)."""
from __future__ import annotations

import copy
import json
import uuid
from datetime import date
from typing import Any, Optional

from postgres_compat import AsyncCursor, DeleteResult, InsertOneResult, UpdateResult, _sql_literal

TEMPORADA = "25-26"
_use_sql: Optional[bool] = None

ABRV_TO_CATEGORY = {
    "DEBU": "Debutantes",
    "PREB": "Prebenjamín",
    "BENJ": "Benjamín",
    "ALEV": "Alevín",
    "INFA": "Infantil",
    "CADE": "Cadete",
    "JUVE": "Juvenil",
}


def _category_label(row: dict) -> str:
    abrv = (row.get("categoria_abrv") or "").strip()
    if abrv in ABRV_TO_CATEGORY:
        return ABRV_TO_CATEGORY[abrv]
    return (row.get("categoria") or "").strip() or "Sin categoría"


def _team_label(row: dict) -> str:
    return (row.get("equipo") or row.get("categoria") or "").strip()


def _entity_from_row(row: dict) -> str:
    team = _team_label(row).upper()
    if "FUND" in team:
        return "fundacion"
    return "club"


def sql_row_to_player(row: dict) -> dict:
    """Convierte fila SQL agregada al documento que espera el frontend."""
    team = _team_label(row)
    has_debt = bool(row.get("has_debt"))
    return {
        "id": str(row["id"]),
        "name": f"{row.get('nombres', '').strip()} {row.get('apellidos', '').strip()}".strip(),
        "dni": row.get("dni") or "",
        "birthdate": row.get("fecha_nacimiento"),
        "category": _category_label(row),
        "team": team,
        "team_id": str(row["team_id"]) if row.get("team_id") else None,
        "club_id": str(row["club_id"]) if row.get("club_id") else None,
        "entity": _entity_from_row(row),
        "payment_status": not has_debt,
        "insurance_expiry": None,
        "dni_expiry": None,
        "phone": row.get("telefono") or "",
        "email": row.get("email") or "",
        "notes": row.get("notas") or "",
        "created_at": row.get("created_at"),
        # Evitar semáforo rojo por fechas no importadas del Excel
        "insurance_semaforo": "green",
        "dni_semaforo": "green",
    }


async def relational_players_enabled(db: Any) -> bool:
    global _use_sql
    if _use_sql is not None:
        return _use_sql
    if not hasattr(db, "fetch_json_rows"):
        _use_sql = False
        return False
    try:
        count = await db.execute_scalar(
            f"select count(*)::text from public.players where temporada = {_sql_literal(TEMPORADA)} limit 1;"
        )
        _use_sql = int(count or "0") > 0
    except Exception:
        _use_sql = None
        raise
    return _use_sql


PLAYERS_SELECT_SQL = """
select
  p.id::text as id,
  p.nombres,
  p.apellidos,
  p.dni,
  p.fecha_nacimiento::text as fecha_nacimiento,
  p.categoria_abrv,
  p.categoria,
  p.equipo,
  p.club_id::text as club_id,
  p.team_id::text as team_id,
  p.notas,
  p.created_at::text as created_at,
  g.telefono,
  g.email,
  coalesce(pay.has_debt, false) as has_debt
from public.players p
left join lateral (
  select telefono, email
  from public.guardians g
  where g.player_id = p.id and g.orden = 1
  limit 1
) g on true
left join lateral (
  select bool_or(pay.estado = 'por_pagar') as has_debt
  from public.payments pay
  where pay.player_id = p.id and pay.temporada = p.temporada
) pay on true
where p.temporada = {temporada}
"""


async def _fetch_sql_rows(db: Any, sql: str) -> list[dict]:
    payload = await db.execute(
        f"select coalesce(json_agg(row_to_json(sub)), '[]'::json)::text "
        f"from ({sql}) sub;"
    )
    if not payload:
        return []
    data = json.loads(payload)
    return data if isinstance(data, list) else []


async def fetch_all_sql_players(db: Any) -> list[dict]:
    sql = PLAYERS_SELECT_SQL.format(temporada=_sql_literal(TEMPORADA))
    rows = await _fetch_sql_rows(db, sql)
    return sorted(
        [sql_row_to_player(row) for row in rows if row.get("id")],
        key=lambda p: p.get("name", "").lower(),
    )


async def fetch_sql_player_by_id(db: Any, player_id: str) -> Optional[dict]:
    sql = (
        PLAYERS_SELECT_SQL.format(temporada=_sql_literal(TEMPORADA))
        + f" and p.id = {_sql_literal(player_id)}::uuid"
    )
    rows = await _fetch_sql_rows(db, sql)
    if not rows:
        return None
    return sql_row_to_player(rows[0])


def _app_doc_to_sql_fields(doc: dict) -> dict:
    name = (doc.get("name") or "").strip()
    parts = name.split(None, 1)
    nombres = parts[0] if parts else name
    apellidos = parts[1] if len(parts) > 1 else ""
    return {
        "nombres": nombres,
        "apellidos": apellidos,
        "dni": doc.get("dni") or None,
        "fecha_nacimiento": doc.get("birthdate") or None,
        "categoria": doc.get("category") or doc.get("team") or "Sin categoría",
        "categoria_abrv": None,
        "equipo": doc.get("team") or "",
        "club_id": doc.get("club_id") or None,
        "team_id": doc.get("team_id") or None,
        "notas": doc.get("notes") or "",
        "genero": "MASCULINO",
        "empadronado": True,
        "federado": False,
        "situacion": "Con Plaza",
    }


async def insert_sql_player(db: Any, doc: dict) -> dict:
    pid = doc.get("id") or str(uuid.uuid4())
    fields = _app_doc_to_sql_fields(doc)
    club_sql = "null" if not fields["club_id"] else f"{_sql_literal(fields['club_id'])}::uuid"
    team_sql = "null" if not fields["team_id"] else f"{_sql_literal(fields['team_id'])}::uuid"
    sql = f"""
    insert into public.players (
      id, nombres, apellidos, genero, dni, fecha_nacimiento,
      categoria, equipo, situacion, empadronado, federado, notas, temporada,
      club_id, team_id
    ) values (
      {_sql_literal(pid)}::uuid,
      {_sql_literal(fields['nombres'])},
      {_sql_literal(fields['apellidos'])},
      'MASCULINO',
      {_sql_literal(fields['dni'])},
      {_sql_literal(fields['fecha_nacimiento'])}::date,
      {_sql_literal(fields['categoria'])},
      {_sql_literal(fields['equipo'])},
      'Con Plaza', true, false,
      {_sql_literal(fields['notas'])},
      {_sql_literal(TEMPORADA)},
      {club_sql},
      {team_sql}
    );
    """
    await db.execute(sql)
    if doc.get("payment_status") is False:
        await db.execute(
            f"""
            insert into public.payments (player_id, temporada, concepto, importe, estado)
            values ({_sql_literal(pid)}::uuid, {_sql_literal(TEMPORADA)}, 'matricula', 0, 'por_pagar')
            on conflict (player_id, concepto, temporada) do nothing;
            """
        )
    created = await fetch_sql_player_by_id(db, pid)
    return created or {**doc, "id": pid}


async def update_sql_player(db: Any, player_id: str, doc: dict) -> Optional[dict]:
    fields = _app_doc_to_sql_fields(doc)
    club_sql = "club_id" if not fields["club_id"] else f"{_sql_literal(fields['club_id'])}::uuid"
    team_sql = "null" if not fields["team_id"] else f"{_sql_literal(fields['team_id'])}::uuid"
    if fields["club_id"]:
        club_assign = f"club_id = {_sql_literal(fields['club_id'])}::uuid,"
    else:
        club_assign = ""
    sql = f"""
    update public.players set
      nombres = {_sql_literal(fields['nombres'])},
      apellidos = {_sql_literal(fields['apellidos'])},
      dni = {_sql_literal(fields['dni'])},
      fecha_nacimiento = {_sql_literal(fields['fecha_nacimiento'])}::date,
      categoria = {_sql_literal(fields['categoria'])},
      equipo = {_sql_literal(fields['equipo'])},
      {club_assign}
      team_id = {team_sql},
      notas = {_sql_literal(fields['notas'])},
      updated_at = now()
    where id = {_sql_literal(player_id)}::uuid;
    """
    await db.execute(sql)
    if doc.get("payment_status") is not None:
        estado = "pagado" if doc["payment_status"] else "por_pagar"
        await db.execute(
            f"""
            update public.payments set estado = {_sql_literal(estado)}
            where player_id = {_sql_literal(player_id)}::uuid
              and temporada = {_sql_literal(TEMPORADA)}
              and concepto = 'matricula';
            """
        )
    return await fetch_sql_player_by_id(db, player_id)


async def delete_sql_player(db: Any, player_id: str) -> bool:
    await db.execute(
        f"delete from public.players where id = {_sql_literal(player_id)}::uuid;"
    )
    return True


def _match_mongo_query(player: dict, query: Optional[dict]) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(_match_mongo_query(player, sub) for sub in (expected or [])):
                return False
            continue
        if key == "id" and isinstance(expected, dict) and "$in" in expected:
            if player.get("id") not in (expected["$in"] or []):
                return False
            continue
        if key == "team" and isinstance(expected, dict) and "$in" in expected:
            allowed = expected["$in"] or []
            if player.get("team") not in allowed and player.get("category") not in allowed:
                return False
            continue
        val = player.get(key)
        if isinstance(expected, dict):
            if "$in" in expected and val not in (expected["$in"] or []):
                return False
        elif val != expected:
            return False
    return True


def _apply_projection(doc: dict, projection: Optional[dict]) -> dict:
    if not projection:
        return copy.deepcopy(doc)
    fields = {k: v for k, v in (projection or {}).items() if k != "_id"}
    if not fields:
        return copy.deepcopy(doc)
    if any(fields.values()):
        return {k: copy.deepcopy(doc[k]) for k, v in fields.items() if v and k in doc}
    out = copy.deepcopy(doc)
    for key, value in fields.items():
        if not value:
            out.pop(key, None)
    return out


class PlayersStoreCursor(AsyncCursor):
    def __init__(self, players: list[dict], query: Optional[dict], projection: Optional[dict]):
        self._all = players
        self._query = query
        self._projection = projection
        self._sort_key: Optional[str] = None
        self._sort_dir = 1

    def sort(self, key: str, direction: int = 1):
        self._sort_key = key.lstrip("-")
        self._sort_dir = direction
        return self

    async def to_list(self, length: Optional[int] = None) -> list[dict]:
        items = [p for p in self._all if _match_mongo_query(p, self._query)]
        if self._sort_key:
            items.sort(
                key=lambda d: (d.get(self._sort_key) or "").lower()
                if self._sort_key == "name"
                else d.get(self._sort_key),
                reverse=self._sort_dir < 0,
            )
        if length is not None:
            items = items[:length]
        return [_apply_projection(p, self._projection) for p in items]


class PlayersStore:
    """Sustituto de db.players cuando hay datos en public.players."""

    def __init__(self, db: Any, fallback_collection: Any):
        self._db = db
        self._fallback = fallback_collection
        self._cache: Optional[list[dict]] = None

    async def _load(self) -> list[dict]:
        if self._cache is None:
            self._cache = await fetch_all_sql_players(self._db)
        return self._cache

    def _invalidate(self):
        self._cache = None

    async def _enabled(self) -> bool:
        return await relational_players_enabled(self._db)

    def find(self, query: Optional[dict] = None, projection: Optional[dict] = None):
        return _PlayersFindCursor(self, query, projection)

    async def find_one(self, query: dict, projection: Optional[dict] = None) -> Optional[dict]:
        if not await self._enabled():
            return await self._fallback.find_one(query, projection)
        if query.get("id") and not isinstance(query["id"], dict):
            doc = await fetch_sql_player_by_id(self._db, query["id"])
            if doc and _match_mongo_query(doc, query):
                return _apply_projection(doc, projection)
            return None
        cursor = self.find(query, projection)
        rows = await cursor.to_list(1)
        return rows[0] if rows else None

    async def insert_one(self, doc: dict):
        if not await self._enabled():
            return await self._fallback.insert_one(doc)
        self._invalidate()
        created = await insert_sql_player(self._db, doc)
        return InsertOneResult(inserted_id=created.get("id"))

    async def insert_many(self, docs: list[dict]):
        if not await self._enabled():
            return await self._fallback.insert_many(docs)
        for doc in docs:
            await insert_sql_player(self._db, doc)
        self._invalidate()
        return None

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        if not await self._enabled():
            return await self._fallback.update_one(query, update, upsert=upsert)
        player_id = query.get("id")
        if not player_id:
            return UpdateResult(0, 0)
        patch = update.get("$set") or {}
        existing = await fetch_sql_player_by_id(self._db, player_id)
        if not existing:
            return UpdateResult(0, 0)
        merged = {**existing, **patch}
        await update_sql_player(self._db, player_id, merged)
        self._invalidate()
        return UpdateResult(1, 1)

    async def delete_one(self, query: dict):
        if not await self._enabled():
            return await self._fallback.delete_one(query)
        player_id = query.get("id")
        if not player_id:
            return DeleteResult(0)
        await delete_sql_player(self._db, player_id)
        self._invalidate()
        return DeleteResult(1)

    async def count_documents(self, query: Optional[dict] = None) -> int:
        if not await self._enabled():
            return await self._fallback.count_documents(query)
        cursor = self.find(query)
        return len(await cursor.to_list(5000))

    async def create_index(self, *args, **kwargs):
        return await self._fallback.create_index(*args, **kwargs)


class _PlayersFindCursor:
    def __init__(self, store: PlayersStore, query: Optional[dict], projection: Optional[dict]):
        self._store = store
        self._query = query
        self._projection = projection
        self._sort_key = None
        self._sort_dir = 1
        self._fallback_cursor = None

    def sort(self, key: str, direction: int = 1):
        self._sort_key = key.lstrip("-")
        self._sort_dir = direction
        return self

    async def to_list(self, length: Optional[int] = None) -> list[dict]:
        if not await self._store._enabled():
            if self._fallback_cursor is None:
                self._fallback_cursor = self._store._fallback.find(
                    self._query, self._projection
                )
                if self._sort_key:
                    self._fallback_cursor.sort(self._sort_key, self._sort_dir)
            return await self._fallback_cursor.to_list(length)
        players = await self._store._load()
        cursor = PlayersStoreCursor(players, self._query, self._projection)
        if self._sort_key:
            cursor.sort(self._sort_key, self._sort_dir)
        return await cursor.to_list(length)

    def __aiter__(self):
        self._aiter = None
        return self

    async def __anext__(self):
        if self._aiter is None:
            self._aiter = iter(await self.to_list(None))
        try:
            return next(self._aiter)
        except StopIteration:
            raise StopAsyncIteration


async def top_teams_for_coach_seed(db: Any, limit: int = 3) -> list[str]:
    """Equipos más poblados en SQL para asignar al entrenador demo."""
    if not await relational_players_enabled(db):
        return ["Alevín A", "Infantil B"]
    sql = f"""
    select coalesce(nullif(trim(equipo), ''), categoria) as team, count(*)::int as c
    from public.players
    where temporada = {_sql_literal(TEMPORADA)}
    group by 1
    order by c desc
    limit {int(limit)};
    """
    try:
        lines = (await db.execute(sql)).splitlines()
        teams = [ln.split("|")[0].strip() for ln in lines if ln.strip()]
        return teams or ["DEBUTANTES 1", "ALEVINES 2"]
    except Exception:
        return ["DEBUTANTES 1", "ALEVINES 2"]


def install_players_store(db: Any) -> Any:
    """Envuelve la colección players para usar SQL cuando exista temporada 25-26."""
    if not hasattr(db, "_collections"):
        return db
    fallback = db._collections.get("players") or db.players
    store = PlayersStore(db, fallback)
    db._collections["players"] = store  # type: ignore[assignment]
    return store
