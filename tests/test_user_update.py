"""Pruebas offline: sin startup, credenciales ni llamadas a Auth/Postgres reales."""
import asyncio
import copy
import importlib
import json
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import postgres_compat

# deps construye su adaptador al importarse. Sustituimos solo la inicialización
# de esquema, antes de importarlo y sin cargar ningún .env del desarrollador.
with patch.dict(os.environ, {"DATABASE_URL": "postgresql://offline:offline@localhost/offline"}), \
     patch.object(postgres_compat.PostgresCompatDB, "_ensure_schema", lambda self: None):
    import deps
    users = importlib.import_module("routes.users")

CLUB = "11111111-1111-1111-1111-111111111111"
OTHER_CLUB = "22222222-2222-2222-2222-222222222222"
AUTH_ID = "33333333-3333-3333-3333-333333333333"
ACTOR_AUTH_ID = "44444444-4444-4444-4444-444444444444"


class FakeUsers:
    def __init__(self, records):
        self.records = records

    async def find_one(self, query, projection=None):
        for row in self.records:
            # Usa el matcher REAL, para verificar regex/opciones del adaptador.
            if postgres_compat._matches(row, query):
                return postgres_compat._apply_projection(row, projection)
        return None


class FakeDB:
    def __init__(self, target):
        self.users = FakeUsers([target])
        self.profile = None
        self.duplicate_profile = False
        self.teams = [{"nombre": "Juvenil A"}, {"nombre": "Cadete B"}]
        self.queries = []
        self.writes = []
        self.fail_write = False

    async def fetch_json_rows(self, sql):
        self.queries.append(sql)
        if "update public.app_documents" in sql:
            if self.fail_write:
                raise RuntimeError("constraint simulated")
            changes = json.loads(re.search(r"set doc = doc \|\| '(.*?)'::jsonb", sql, re.S)[1].replace("''", "'"))
            self.writes.append(changes)
            self.users.records[0].update(copy.deepcopy(changes))
            if self.profile:
                self.profile.update({key: value for key, value in changes.items()
                                     if key in {"name", "email", "role", "assigned_teams"}})
            return [copy.deepcopy(self.users.records[0])]
        if "lower(email)" in sql:
            return [{"id": "some-other-profile"}] if self.duplicate_profile else []
        if "from public.profiles" in sql:
            return [copy.deepcopy(self.profile)] if self.profile else []
        if "from public.teams" in sql:
            return copy.deepcopy(self.teams)
        raise AssertionError(f"Unexpected SQL: {sql}")


@pytest.fixture
def state(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No se permiten red ni base de datos reales")

    monkeypatch.setattr(deps.requests, "request", forbidden)
    monkeypatch.setattr(postgres_compat.PostgresCompatDB, "_run_psql", forbidden)
    target = {"id": "app-target", "auth_user_id": AUTH_ID, "club_id": CLUB,
              "name": "Entrenador", "email": "coach@example.com", "role": "coach",
              "assigned_teams": ["Juvenil A"], "created_at": "yesterday", "password_hash": "secret"}
    actor = {"id": "actor", "auth_user_id": ACTOR_AUTH_ID, "club_id": CLUB, "role": "admin"}
    fake = FakeDB(target)
    auth_user = {"id": AUTH_ID, "email": target["email"], "user_metadata": {"app_user_id": target["id"]}}
    calls = []
    cache = []
    monkeypatch.setattr(users, "db", fake)
    monkeypatch.setattr(users, "get_supabase_staff_user", lambda uid: copy.deepcopy(auth_user))
    monkeypatch.setattr(users, "supabase_admin_find_user_by_email", lambda email: None)
    monkeypatch.setattr(users, "update_supabase_staff_user", lambda uid, **data: calls.append((uid, data)))
    monkeypatch.setattr(users, "invalidate_user_cache", lambda: cache.append(True))
    return SimpleNamespace(target=target, actor=actor, db=fake, auth=auth_user, calls=calls, cache=cache)


def run_update(state, payload, user_id="app-target"):
    return asyncio.run(users.update_user(user_id, users.UserUpdate(**payload), state.actor))


def assert_status(state, payload, code, user_id="app-target"):
    with pytest.raises(HTTPException) as exc:
        run_update(state, payload, user_id)
    assert exc.value.status_code == code


@pytest.mark.parametrize("payload", [
    {"name": "   "}, {"name": None}, {"email": "invalid"}, {"email": None},
    {"role": "superadmin"}, {"role": None}, {"assigned_teams": None},
    {"assigned_teams": [""]}, {"assigned_teams": [123]}, {"password": "new password"},
    {"club_id": OTHER_CLUB}, {"name": "x" * 121},
])
def test_rejects_invalid_fields(payload):
    with pytest.raises(ValidationError):
        users.UserUpdate(**payload)


def test_partial_name_preserves_teams_role_email_and_password(state):
    result = run_update(state, {"name": "  Nuevo nombre  "})
    assert result["name"] == "Nuevo nombre"
    assert result["assigned_teams"] == ["Juvenil A"]
    assert result["role"] == "coach"
    assert "password_hash" not in result
    assert state.target["password_hash"] == "secret"
    assert state.calls[0][1] == {"name": "Nuevo nombre", "app_user_id": "app-target"}
    assert set(state.db.writes[0]) == {"name", "auth_user_id", "updated_at"}
    assert state.cache == [True]


def test_empty_patch_has_no_side_effects(state):
    result = run_update(state, {})
    assert "password_hash" not in result
    assert state.calls == state.cache == state.db.writes == []


def test_target_must_exist_and_share_club(state):
    assert_status(state, {"name": "Nuevo"}, 404, user_id="missing")
    state.target["club_id"] = OTHER_CLUB
    assert_status(state, {"name": "Nuevo"}, 403)
    assert state.calls == []


@pytest.mark.parametrize("payload,target_role", [({"name": "Nuevo"}, "admin"), ({"role": "admin"}, "coach")])
def test_coordinator_cannot_edit_or_assign_admin(state, payload, target_role):
    state.actor["role"] = "coordinator"
    state.target["role"] = target_role
    assert_status(state, payload, 403)
    assert state.calls == []


def test_effective_profile_prevents_coordinator_editing_hidden_admin(state):
    state.actor["role"] = "coordinator"
    state.db.profile = {**state.target, "id": AUTH_ID, "role": "admin"}
    assert_status(state, {"name": "Nuevo"}, 403)
    assert state.calls == []


def test_profile_must_also_share_club(state):
    state.db.profile = {**state.target, "id": AUTH_ID, "club_id": OTHER_CLUB}
    assert_status(state, {"name": "Nuevo"}, 403)


def test_self_is_identified_by_auth_id_and_cannot_change_role_or_teams(state):
    state.actor.update(id=AUTH_ID, auth_user_id=AUTH_ID)
    assert_status(state, {"role": "office"}, 400)
    assert_status(state, {"assigned_teams": ["Cadete B"]}, 400)
    assert run_update(state, {"name": "Mi nombre"})["name"] == "Mi nombre"


@pytest.mark.parametrize("old_role,new_role", [("pending", "coach"), ("coach", "pending")])
def test_edit_does_not_bypass_approval(state, old_role, new_role):
    state.target["role"] = old_role
    assert_status(state, {"role": new_role}, 400)
    assert state.calls == []


def test_pending_can_change_name_and_teams_without_approval(state):
    state.target["role"] = "pending"
    result = run_update(state, {"name": "Pendiente", "assigned_teams": ["Cadete B"]})
    assert result["role"] == "pending"
    assert "role" not in state.db.writes[0]
    assert "approved_at" not in result


def test_teams_are_validated_and_canonicalized_in_club_and_season(state):
    result = run_update(state, {"assigned_teams": [" juvenil a ", "Juvenil A", "Cadete B"]})
    assert result["assigned_teams"] == ["Juvenil A", "Cadete B"]
    query = next(q for q in state.db.queries if "from public.teams" in q)
    assert f"club_id = '{CLUB}'::uuid" in query
    assert f"temporada = '{users.TEMPORADA}'" in query
    assert "activo = true" in query


def test_invalid_team_is_rejected_without_writes(state):
    assert_status(state, {"assigned_teams": ["Equipo de otro club"]}, 400)
    assert state.calls == []


@pytest.mark.parametrize("role", ["admin", "coordinator"])
def test_change_to_global_role_clears_teams_even_if_omitted(state, role):
    result = run_update(state, {"role": role})
    assert result["assigned_teams"] == []
    assert state.db.writes[0]["assigned_teams"] == []
    assert state.calls[0][1]["assigned_teams"] == []


def test_office_preserves_existing_assignment_semantics(state):
    result = run_update(state, {"role": "office", "assigned_teams": ["Cadete B"]})
    assert result["assigned_teams"] == ["Cadete B"]


def test_duplicate_document_email_uses_case_insensitive_real_adapter_matcher(state):
    state.db.users.records.append({"id": "other", "email": "EXISTING@EXAMPLE.COM", "club_id": OTHER_CLUB})
    assert_status(state, {"email": "existing@example.com"}, 409)
    assert state.calls == []


def test_duplicate_auth_email_is_rejected_globally(state, monkeypatch):
    monkeypatch.setattr(users, "supabase_admin_find_user_by_email", lambda email: {"id": ACTOR_AUTH_ID})
    assert_status(state, {"email": "existing@example.com"}, 409)
    assert state.calls == []


def test_duplicate_profile_email_is_rejected(state):
    state.db.duplicate_profile = True
    assert_status(state, {"email": "existing@example.com"}, 409)
    assert state.calls == []


def test_email_is_normalized_and_synced_to_auth_and_profile(state):
    state.db.profile = {**state.target, "id": AUTH_ID}
    result = run_update(state, {"email": "New@Example.COM"})
    assert result["email"] == "new@example.com"
    assert state.calls[0][1]["email"] == "new@example.com"
    assert state.db.profile["email"] == "new@example.com"
    assert state.db.users.records[0]["email"] == "new@example.com"
    sql = next(q for q in state.db.queries if "update public.app_documents" in q)
    assert "update public.profiles set email" in sql
    assert "and exists (select 1 from document_update)" in sql


def test_unrequested_email_is_never_reverted_to_stale_profile_value(state):
    state.db.profile = {**state.target, "id": AUTH_ID, "email": "stale@example.com"}
    result = run_update(state, {"name": "Actualizado"})
    assert result["email"] == state.auth["email"]
    assert "email" not in state.calls[0][1]
    assert "email" not in state.db.writes[0]


def test_partial_update_keeps_effective_profile_teams(state):
    state.db.profile = {**state.target, "id": AUTH_ID, "assigned_teams": ["Cadete B"]}
    result = run_update(state, {"name": "Actualizado"})
    assert result["assigned_teams"] == ["Cadete B"]
    assert "assigned_teams" not in state.db.writes[0]


def test_persistence_failure_compensates_only_changed_auth_fields_and_invalidates_cache(state):
    state.db.fail_write = True
    assert_status(state, {"email": "new@example.com"}, 503)
    assert len(state.calls) == 2
    assert state.calls[1][1] == {"email": "coach@example.com", "app_user_id": "app-target"}
    assert state.target["email"] == "coach@example.com"
    assert state.cache == [True]


def test_auth_failure_does_not_persist_and_invalidates_cache(state, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("Auth unavailable")
    monkeypatch.setattr(users, "update_supabase_staff_user", fail)
    assert_status(state, {"name": "Nuevo"}, 503)
    assert state.db.writes == []
    assert state.cache == [True]


def test_auth_duplicate_race_retains_409(state, monkeypatch):
    def duplicate(*args, **kwargs):
        raise HTTPException(status_code=409, detail="Duplicate")
    monkeypatch.setattr(users, "update_supabase_staff_user", duplicate)
    assert_status(state, {"email": "duplicate@example.com"}, 409)
    assert state.db.writes == []


def test_legacy_auth_link_is_resolved_without_creating_an_account(state, monkeypatch):
    state.target.pop("auth_user_id")
    monkeypatch.setattr(users, "supabase_admin_find_user_by_email", lambda email: copy.deepcopy(state.auth))
    result = run_update(state, {"name": "Cuenta antigua"})
    assert result["auth_user_id"] == AUTH_ID
    assert state.db.writes[0]["auth_user_id"] == AUTH_ID


def test_unlinked_account_cannot_reuse_someone_elses_auth(state, monkeypatch):
    state.target.pop("auth_user_id")
    state.auth["user_metadata"]["app_user_id"] = "someone-else"
    monkeypatch.setattr(users, "supabase_admin_find_user_by_email", lambda email: copy.deepcopy(state.auth))
    assert_status(state, {"name": "Cambio"}, 409)
    assert state.calls == []


def test_mismatched_auth_email_is_not_silently_overwritten(state):
    state.auth["email"] = "other@example.com"
    assert_status(state, {"name": "Cambio"}, 409)
    assert state.calls == []


def test_auth_helper_does_not_send_password_or_confirm_email(state, monkeypatch):
    requests = []
    def fake_request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"user": {"id": AUTH_ID}})
    monkeypatch.setattr(deps, "_supabase_admin_request", fake_request)
    deps.update_supabase_staff_user(AUTH_ID, name="Nombre", app_user_id="app-target")
    payload = requests[0][2]["json"]
    assert payload == {"user_metadata": {"name": "Nombre", "app_user_id": "app-target"}}
    deps.update_supabase_staff_user(AUTH_ID, email="New@Example.COM", role="coach", assigned_teams=[])
    payload = requests[1][2]["json"]
    assert payload["email"] == "new@example.com"
    assert payload["app_metadata"] == {"role": "coach"}
    assert "password" not in payload and "email_confirm" not in payload


def test_auth_duplicate_lookup_searches_beyond_first_page(state, monkeypatch):
    paths = []
    def fake_request(method, path, **kwargs):
        paths.append(path)
        batch = [{"id": str(i), "email": f"{i}@example.com"} for i in range(1000)] if "page=1&" in path else [
            {"id": AUTH_ID, "email": "found@example.com"}]
        return SimpleNamespace(status_code=200, json=lambda: {"users": batch})
    monkeypatch.setattr(deps, "_supabase_admin_request", fake_request)
    assert deps.supabase_admin_find_user_by_email("FOUND@example.com")["id"] == AUTH_ID
    assert len(paths) == 2


@pytest.mark.parametrize("role,status", [("coach", 403), ("office", 403), ("pending", 403), (None, 401)])
def test_http_role_dependency_denies_non_managers(state, role, status):
    app = FastAPI()
    app.include_router(users.router, prefix="/api")
    async def current_user():
        if role is None:
            raise HTTPException(status_code=401, detail="No autenticado")
        return {**state.actor, "role": role}
    app.dependency_overrides[deps.get_current_user] = current_user
    async def request():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://offline") as client:
            return await client.patch("/api/users/app-target", json={"name": "Nuevo"})
    response = asyncio.run(request())
    assert response.status_code == status
    assert state.calls == []
