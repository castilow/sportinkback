"""Compatibilidad tipo MongoDB sobre Postgres usando una tabla JSONB.

Permite reutilizar el backend actual mientras se migra desde MongoDB a Supabase.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import re
import shutil
import subprocess

import requests
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_logger = logging.getLogger("rayo.postgres_compat")


def _project_ref_from_env() -> str:
    explicit = os.environ.get("SUPABASE_PROJECT_REF", "").strip()
    if explicit:
        return explicit
    base = os.environ.get("SUPABASE_URL", "").strip()
    if base:
        host = urlsplit(base).hostname or ""
        if host.endswith(".supabase.co"):
            return host.split(".")[0]
    return "qupelkaavccclpqrzinc"


SCHEMA_SQL = """
create table if not exists public.app_documents (
    row_id bigserial primary key,
    collection text not null,
    doc jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_app_documents_collection
    on public.app_documents (collection);

create index if not exists idx_app_documents_collection_doc_id
    on public.app_documents (collection, (doc->>'id'));

create unique index if not exists uq_app_documents_collection_doc_id
    on public.app_documents (collection, (doc->>'id'))
    where doc ? 'id';

create unique index if not exists uq_app_documents_users_email
    on public.app_documents ((lower(doc->>'email')))
    where collection = 'users' and doc ? 'email';

create unique index if not exists uq_app_documents_login_identifier
    on public.app_documents ((doc->>'identifier'))
    where collection = 'login_attempts' and doc ? 'identifier';

create unique index if not exists uq_app_documents_poll_votes_pair
    on public.app_documents ((doc->>'poll_id'), (doc->>'voter_name'))
    where collection = 'poll_votes' and doc ? 'poll_id' and doc ? 'voter_name';
"""


@dataclass
class InsertOneResult:
    inserted_id: Optional[int]


@dataclass
class UpdateResult:
    matched_count: int
    modified_count: int
    upserted_id: Optional[int] = None


@dataclass
class DeleteResult:
    deleted_count: int


def _sql_literal(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("'", "''")
    return f"'{text}'"


def _json_literal(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False).replace("'", "''")
    return f"'{text}'::jsonb"


def _sort_value(value: Any) -> Any:
    if value is None:
        return (1, "")
    if isinstance(value, (dict, list)):
        return (0, json.dumps(value, ensure_ascii=False, sort_keys=True))
    return (0, value)


def _apply_projection(doc: dict, projection: Optional[dict]) -> dict:
    if not projection:
        return copy.deepcopy(doc)
    fields = {k: v for k, v in projection.items() if k != "_id"}
    if not fields:
        return copy.deepcopy(doc)
    include_mode = any(bool(v) for v in fields.values())
    if include_mode:
        return {k: copy.deepcopy(doc[k]) for k, v in fields.items() if v and k in doc}
    out = copy.deepcopy(doc)
    for key, value in fields.items():
        if not value:
            out.pop(key, None)
    return out


def _match_operator(field_value: Any, operator: str, expected: Any) -> bool:
    if operator == "$in":
        return field_value in (expected or [])
    if operator == "$gte":
        return field_value is not None and field_value >= expected
    if operator == "$regex":
        flags = 0
        if isinstance(expected, re.Pattern):
            pattern = expected
        else:
            pattern = re.compile(str(expected), flags)
        return bool(pattern.search("" if field_value is None else str(field_value)))
    if operator == "$options":
        return True
    return False


def _match_condition(field_value: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        if "$regex" in expected:
            flags = re.IGNORECASE if "i" in str(expected.get("$options", "")) else 0
            pattern = re.compile(str(expected.get("$regex", "")), flags)
            if not pattern.search("" if field_value is None else str(field_value)):
                return False
        for operator, value in expected.items():
            if operator in {"$regex", "$options"}:
                continue
            if not _match_operator(field_value, operator, value):
                return False
        return True
    return field_value == expected


def _matches(doc: dict, query: Optional[dict]) -> bool:
    if not query:
        return True
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, subquery) for subquery in expected or []):
                return False
            continue
        if not _match_condition(doc.get(key), expected):
            return False
    return True


def _apply_update(doc: dict, update: dict) -> dict:
    out = copy.deepcopy(doc)
    for key, value in (update.get("$set") or {}).items():
        out[key] = value
    for key, value in (update.get("$push") or {}).items():
        current = list(out.get(key) or [])
        current.append(value)
        out[key] = current
    return out


def _upsert_base_doc(query: dict) -> dict:
    base: dict[str, Any] = {}
    for key, value in (query or {}).items():
        if key.startswith("$"):
            continue
        if isinstance(value, dict):
            continue
        base[key] = value
    return base


class AsyncCursor:
    def __init__(self, collection: "PostgresCollection", query: Optional[dict], projection: Optional[dict]):
        self._collection = collection
        self._query = query or {}
        self._projection = projection
        self._sort_fields: list[tuple[str, int]] = []

    def sort(self, field: Any, direction: Optional[int] = None) -> "AsyncCursor":
        if isinstance(field, list):
            self._sort_fields = [(name, order) for name, order in field]
        else:
            self._sort_fields = [(field, 1 if direction is None else direction)]
        return self

    async def to_list(self, limit: int) -> list[dict]:
        docs = await self._collection._find_docs(self._query, self._projection)
        for field, direction in reversed(self._sort_fields):
            docs.sort(key=lambda item: _sort_value(item.get(field)), reverse=(direction or 1) < 0)
        return docs[:limit]

    def __aiter__(self):
        async def _generator():
            for item in await self.to_list(100000):
                yield item

        return _generator()


class PostgresCollection:
    def __init__(self, db: "PostgresCompatDB", name: str):
        self._db = db
        self._name = name

    async def _all_rows(self) -> list[dict]:
        sql = (
            "select json_build_object('row_id', row_id, 'doc', doc)::text "
            "from public.app_documents "
            f"where collection = {_sql_literal(self._name)} "
            "order by row_id"
        )
        return await self._db.fetch_json_rows(sql)

    async def _find_rows(self, query: Optional[dict]) -> list[dict]:
        rows = await self._all_rows()
        return [row for row in rows if _matches(row["doc"], query)]

    async def _find_docs(self, query: Optional[dict], projection: Optional[dict]) -> list[dict]:
        rows = await self._find_rows(query)
        return [_apply_projection(row["doc"], projection) for row in rows]

    def find(self, query: Optional[dict] = None, projection: Optional[dict] = None) -> AsyncCursor:
        return AsyncCursor(self, query, projection)

    async def find_one(self, query: Optional[dict] = None, projection: Optional[dict] = None) -> Optional[dict]:
        rows = await self._find_rows(query)
        if not rows:
            return None
        return _apply_projection(rows[0]["doc"], projection)

    async def insert_one(self, doc: dict) -> InsertOneResult:
        clean = copy.deepcopy(doc)
        clean.pop("_id", None)
        sql = (
            "insert into public.app_documents (collection, doc) values "
            f"({_sql_literal(self._name)}, {_json_literal(clean)}) "
            "returning row_id"
        )
        output = await self._db.execute_scalar(sql)
        return InsertOneResult(inserted_id=int(output) if output else None)

    async def insert_many(self, docs: Iterable[dict]) -> list[InsertOneResult]:
        results = []
        for doc in docs:
            results.append(await self.insert_one(doc))
        return results

    async def update_one(self, query: dict, update: dict, upsert: bool = False) -> UpdateResult:
        rows = await self._find_rows(query)
        if not rows:
            if not upsert:
                return UpdateResult(matched_count=0, modified_count=0)
            new_doc = _apply_update(_upsert_base_doc(query), update)
            result = await self.insert_one(new_doc)
            return UpdateResult(matched_count=0, modified_count=0, upserted_id=result.inserted_id)

        row = rows[0]
        new_doc = _apply_update(row["doc"], update)
        sql = (
            "update public.app_documents "
            f"set doc = {_json_literal(new_doc)}, updated_at = now() "
            f"where row_id = {int(row['row_id'])}"
        )
        await self._db.execute(sql)
        modified = 0 if new_doc == row["doc"] else 1
        return UpdateResult(matched_count=1, modified_count=modified)

    async def delete_one(self, query: dict) -> DeleteResult:
        rows = await self._find_rows(query)
        if not rows:
            return DeleteResult(deleted_count=0)
        sql = f"delete from public.app_documents where row_id = {int(rows[0]['row_id'])}"
        await self._db.execute(sql)
        return DeleteResult(deleted_count=1)

    async def delete_many(self, query: dict) -> DeleteResult:
        rows = await self._find_rows(query)
        if not rows:
            return DeleteResult(deleted_count=0)
        ids = ", ".join(str(int(row["row_id"])) for row in rows)
        sql = f"delete from public.app_documents where row_id in ({ids})"
        await self._db.execute(sql)
        return DeleteResult(deleted_count=len(rows))

    async def count_documents(self, query: Optional[dict] = None) -> int:
        rows = await self._find_rows(query)
        return len(rows)

    async def create_index(self, *_args, **_kwargs):
        return None


class PostgresCompatDB:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.client = self
        self._collections: dict[str, PostgresCollection] = {}
        self._psql_bin = os.environ.get("PSQL_BIN") or shutil.which("psql") or "/opt/homebrew/opt/libpq/bin/psql"
        self._sanitized_url, self._password = self._split_password(database_url)
        self._ensure_schema()

    def __getattr__(self, item: str) -> PostgresCollection:
        if item.startswith("_"):
            raise AttributeError(item)
        if item not in self._collections:
            self._collections[item] = PostgresCollection(self, item)
        return self._collections[item]

    def close(self):
        return None

    def _split_password(self, database_url: str) -> tuple[str, Optional[str]]:
        parsed = urlsplit(database_url)
        if parsed.password is None:
            return database_url, None
        username = parsed.username or ""
        if parsed.port:
            netloc = f"{username}@{parsed.hostname}:{parsed.port}"
        else:
            netloc = f"{username}@{parsed.hostname}"
        query = urlencode(parse_qsl(parsed.query, keep_blank_values=True))
        sanitized = urlunsplit((parsed.scheme, netloc, parsed.path, query, parsed.fragment))
        return sanitized, parsed.password

    def _management_token(self) -> str:
        return os.environ.get("SUPABASE_ACCESS_TOKEN", "").strip()

    def _management_query(self, sql: str):
        token = self._management_token()
        if not token:
            return None
        ref = _project_ref_from_env()
        last_err = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"https://api.supabase.com/v1/projects/{ref}/database/query",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json={"query": sql},
                    timeout=45,
                )
                if resp.status_code >= 300:
                    raise RuntimeError(_supabase_mgmt_error(resp))
                data = resp.json()
                if data is None:
                    return []
                return data
            except Exception as exc:
                last_err = exc
                if attempt < 2:
                    import time
                    time.sleep(2 * (attempt + 1))
        raise last_err  # type: ignore[misc]

    def _run_psql(self, sql: str) -> str:
        mgmt = self._management_query(sql)
        if mgmt is not None:
            return self._format_management_output(mgmt)
        env = os.environ.copy()
        if self._password:
            env["PGPASSWORD"] = self._password
        proc = subprocess.run(
            [self._psql_bin, self._sanitized_url, "-v", "ON_ERROR_STOP=1", "-X", "-qAt", "-c", sql],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.stdout.strip()

    @staticmethod
    def _format_management_output(data) -> str:
        if not isinstance(data, list):
            return ""
        if not data:
            return ""
        if len(data) == 1 and isinstance(data[0], dict) and len(data[0]) == 1:
            val = next(iter(data[0].values()))
            if val is None:
                return ""
            return str(val)
        lines: list[str] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            if len(row) == 1:
                val = next(iter(row.values()))
                if isinstance(val, str):
                    lines.append(val)
                else:
                    lines.append(json.dumps(val, ensure_ascii=False))
            else:
                lines.append(json.dumps(row, ensure_ascii=False))
        return "\n".join(lines)

    def _ensure_schema(self):
        try:
            self._run_psql(SCHEMA_SQL)
        except Exception as exc:
            if self._management_token():
                _logger.warning("No se pudo asegurar app_documents via SQL: %s", exc)
                return
            raise

    async def execute(self, sql: str) -> str:
        return await asyncio.to_thread(self._run_psql, sql)

    async def execute_scalar(self, sql: str) -> str:
        return await self.execute(sql)

    async def fetch_json_rows(self, sql: str) -> list[dict]:
        if self._management_token():
            data = await asyncio.to_thread(self._management_query, sql)
            if data is not None:
                rows: list[dict] = []
                for row in data:
                    if not isinstance(row, dict):
                        continue
                    # Algunas consultas envuelven el resultado en una sola columna JSON.
                    # Si solo hay una columna string, intentar parsearla; si no es JSON,
                    # conservar la fila tal cual (p. ej. select id::text).
                    if len(row) == 1:
                        val = next(iter(row.values()))
                        if isinstance(val, dict):
                            rows.append(val)
                            continue
                        if isinstance(val, str):
                            try:
                                parsed = json.loads(val)
                                if isinstance(parsed, dict):
                                    rows.append(parsed)
                                    continue
                                if isinstance(parsed, list):
                                    rows.extend([r for r in parsed if isinstance(r, dict)])
                                    continue
                            except Exception:
                                pass
                    rows.append(row)
                return rows
        output = await self.execute(sql)
        if not output:
            return []
        rows = []
        for line in output.splitlines():
            if not line.strip():
                continue
            rows.append(json.loads(line))
        return rows


def _supabase_mgmt_error(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        if isinstance(payload, dict):
            return str(payload.get("message") or payload)
        return str(payload)
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"
