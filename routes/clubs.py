"""CRUD de clubes y equipos con aislamiento por club_id."""
from __future__ import annotations

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from deps import (
    db,
    get_current_user,
    require_roles,
    require_club_context,
    now_iso,
    uuid,
)

router = APIRouter()


class TeamIn(BaseModel):
    nombre: str = Field(..., min_length=1, max_length=120)
    categoria: str = Field("", max_length=120)
    genero: Literal["MASCULINO", "FEMENINO", "MIXTO"] = "MIXTO"
    temporada: str = Field("25-26", max_length=16)
    entidad: Literal["club", "fundacion"] = "club"
    sport_slug: str = Field("futbol", max_length=40)
    activo: bool = True


class TeamOut(BaseModel):
    id: str
    club_id: str
    sport_id: Optional[str] = None
    nombre: str
    categoria: Optional[str] = None
    genero: Optional[str] = None
    temporada: str
    entidad: str = "club"
    activo: bool = True
    num_jugadores: int = 0


class ClubOut(BaseModel):
    id: str
    slug: str
    nombre: str
    nombre_corto: Optional[str] = None
    ciudad: Optional[str] = None
    color_primario: Optional[str] = None
    color_secundario: Optional[str] = None
    escudo_url: Optional[str] = None


async def _sql_rows(query: str) -> list:
    if not hasattr(db, "fetch_json_rows"):
        raise HTTPException(status_code=500, detail="Base de datos SQL no disponible")
    return await db.fetch_json_rows(query)


@router.get("/clubs/me", response_model=ClubOut)
async def get_my_club(user=Depends(get_current_user)):
    club_id = require_club_context(user)
    from postgres_compat import _sql_literal

    rows = await _sql_rows(
        f"""
        select id::text as id, slug, nombre, nombre_corto, ciudad,
               color_primario, color_secundario, escudo_url
        from public.clubs
        where id = {_sql_literal(club_id)}::uuid
        limit 1;
        """
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Club no encontrado")
    return rows[0]


@router.get("/teams", response_model=List[TeamOut])
async def list_teams(
    temporada: Optional[str] = None,
    include_inactive: bool = False,
    user=Depends(get_current_user),
):
    club_id = require_club_context(user)
    from postgres_compat import _sql_literal

    where = [f"t.club_id = {_sql_literal(club_id)}::uuid"]
    if temporada:
        where.append(f"t.temporada = {_sql_literal(temporada)}")
    if not include_inactive:
        where.append("t.activo = true")
    # Coaches: solo sus equipos asignados
    if user.get("role") == "coach":
        allowed = user.get("assigned_teams") or []
        if not allowed:
            return []
        names = ", ".join(_sql_literal(n) for n in allowed)
        where.append(f"t.nombre in ({names})")

    rows = await _sql_rows(
        f"""
        select
          t.id::text as id,
          t.club_id::text as club_id,
          t.sport_id::text as sport_id,
          t.nombre,
          t.categoria,
          t.genero,
          t.temporada,
          t.entidad,
          t.activo,
          count(p.id)::int as num_jugadores
        from public.teams t
        left join public.players p on p.team_id = t.id
        where {' and '.join(where)}
        group by t.id
        order by t.nombre;
        """
    )
    return rows


@router.post("/teams", response_model=TeamOut)
async def create_team(data: TeamIn, user=Depends(require_roles("admin", "coordinator"))):
    club_id = require_club_context(user)
    from postgres_compat import _sql_literal

    # Resolver deporte del club
    sports = await _sql_rows(
        f"""
        select s.id::text as id, s.slug
        from public.club_sports cs
        join public.sports s on s.id = cs.sport_id
        where cs.club_id = {_sql_literal(club_id)}::uuid
        order by case when s.slug = {_sql_literal(data.sport_slug)} then 0 else 1 end, s.nombre
        limit 1;
        """
    )
    if not sports:
        # Crear deporte y vincular
        slug = data.sport_slug or "futbol"
        await db.execute(
            f"""
            insert into public.sports (slug, nombre)
            values ({_sql_literal(slug)}, {_sql_literal(slug.title())})
            on conflict (slug) do nothing;
            """
        )
        await db.execute(
            f"""
            insert into public.club_sports (club_id, sport_id)
            select {_sql_literal(club_id)}::uuid, id
            from public.sports where slug = {_sql_literal(slug)}
            on conflict do nothing;
            """
        )
        sports = await _sql_rows(
            f"select id::text as id from public.sports where slug = {_sql_literal(slug)} limit 1;"
        )
    sport_id = sports[0]["id"]
    team_id = str(uuid.uuid4())
    nombre = data.nombre.strip()
    categoria = (data.categoria or nombre).strip()

    try:
        await db.execute(
            f"""
            insert into public.teams
              (id, club_id, sport_id, nombre, categoria, genero, temporada, entidad, activo)
            values (
              {_sql_literal(team_id)}::uuid,
              {_sql_literal(club_id)}::uuid,
              {_sql_literal(sport_id)}::uuid,
              {_sql_literal(nombre)},
              {_sql_literal(categoria)},
              {_sql_literal(data.genero)},
              {_sql_literal(data.temporada)},
              {_sql_literal(data.entidad)},
              {str(data.activo).lower()}
            );
            """
        )
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Ya existe un equipo con ese nombre en la temporada")
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "id": team_id,
        "club_id": club_id,
        "sport_id": sport_id,
        "nombre": nombre,
        "categoria": categoria,
        "genero": data.genero,
        "temporada": data.temporada,
        "entidad": data.entidad,
        "activo": data.activo,
        "num_jugadores": 0,
    }


@router.put("/teams/{team_id}", response_model=TeamOut)
async def update_team(team_id: str, data: TeamIn, user=Depends(require_roles("admin", "coordinator"))):
    club_id = require_club_context(user)
    from postgres_compat import _sql_literal

    existing = await _sql_rows(
        f"""
        select id::text as id from public.teams
        where id = {_sql_literal(team_id)}::uuid
          and club_id = {_sql_literal(club_id)}::uuid
        limit 1;
        """
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")

    nombre = data.nombre.strip()
    categoria = (data.categoria or nombre).strip()
    await db.execute(
        f"""
        update public.teams set
          nombre = {_sql_literal(nombre)},
          categoria = {_sql_literal(categoria)},
          genero = {_sql_literal(data.genero)},
          temporada = {_sql_literal(data.temporada)},
          entidad = {_sql_literal(data.entidad)},
          activo = {str(data.activo).lower()},
          updated_at = now()
        where id = {_sql_literal(team_id)}::uuid
          and club_id = {_sql_literal(club_id)}::uuid;
        """
    )
    # Mantener texto legacy en jugadores del equipo
    await db.execute(
        f"""
        update public.players
        set equipo = {_sql_literal(nombre)}
        where team_id = {_sql_literal(team_id)}::uuid
          and club_id = {_sql_literal(club_id)}::uuid;
        """
    )
    rows = await _sql_rows(
        f"""
        select
          t.id::text as id, t.club_id::text as club_id, t.sport_id::text as sport_id,
          t.nombre, t.categoria, t.genero, t.temporada, t.entidad, t.activo,
          count(p.id)::int as num_jugadores
        from public.teams t
        left join public.players p on p.team_id = t.id
        where t.id = {_sql_literal(team_id)}::uuid
        group by t.id;
        """
    )
    return rows[0]


@router.delete("/teams/{team_id}")
async def archive_team(team_id: str, user=Depends(require_roles("admin", "coordinator"))):
    """Archiva el equipo (soft-delete). No borra jugadores."""
    club_id = require_club_context(user)
    from postgres_compat import _sql_literal

    result_rows = await _sql_rows(
        f"""
        update public.teams
        set activo = false, updated_at = now()
        where id = {_sql_literal(team_id)}::uuid
          and club_id = {_sql_literal(club_id)}::uuid
        returning id::text as id;
        """
    )
    if not result_rows:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    return {"ok": True, "id": team_id, "activo": False}
