"""CRUD de clubes y equipos con aislamiento por club_id."""
from __future__ import annotations

from typing import List, Literal, Optional

import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
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


# ------------------ Importar equipos desde Excel/CSV ------------------
_TEAM_COLS = {
    "nombre": ["equipo", "nombre", "nombre del equipo", "team", "team name", "name", "equip", "nom", "nom de l'equip"],
    "categoria": ["categoría", "categoria", "category", "cat"],
    "genero": ["género", "genero", "gender", "sexo", "gènere"],
    "entidad": ["entidad", "entitat", "entity"],
    "temporada": ["temporada", "season"],
}
MAX_TEAM_IMPORT = 500


def _cell(v) -> str:
    s = "" if v is None else str(v).strip()
    return "" if s.lower() in ("nan", "none", "nat") else s


def _norm_genero(v: str) -> str:
    s = v.strip().lower()
    if s.startswith("mix"):
        return "MIXTO"
    if s.startswith(("f", "w")):
        return "FEMENINO"
    if s.startswith(("masc", "male", "m", "h")):
        return "MASCULINO"
    return "MIXTO"


async def _club_sport_id_for_import(club_id: str) -> str:
    from postgres_compat import _sql_literal

    rows = await _sql_rows(
        f"""
        select json_build_object('id', s.id::text)::text from public.club_sports cs
        join public.sports s on s.id = cs.sport_id
        where cs.club_id = {_sql_literal(club_id)}::uuid
        order by s.nombre limit 1;
        """
    )
    if rows:
        return rows[0]["id"]
    await db.execute("insert into public.sports (slug, nombre) values ('futbol', 'Futbol') on conflict (slug) do nothing;")
    await db.execute(
        f"""
        insert into public.club_sports (club_id, sport_id)
        select {_sql_literal(club_id)}::uuid, id from public.sports where slug = 'futbol'
        on conflict do nothing;
        """
    )
    rows = await _sql_rows("select json_build_object('id', id::text)::text from public.sports where slug = 'futbol' limit 1;")
    return rows[0]["id"]


@router.get("/teams/import/template.xlsx")
async def teams_import_template(user=Depends(require_roles("admin", "coordinator"))):
    import pandas as pd

    df = pd.DataFrame([
        {"Equipo": "Alevín A", "Categoría": "Alevín", "Género": "Masculino", "Entidad": "Club", "Temporada": "25-26"},
        {"Equipo": "Cadete Femenino", "Categoría": "Cadete", "Género": "Femenino", "Entidad": "Club", "Temporada": "25-26"},
        {"Equipo": "Sub-12 Mixto", "Categoría": "Sub-12", "Género": "Mixto", "Entidad": "Club", "Temporada": "25-26"},
    ])
    notes = pd.DataFrame({"Instrucciones": [
        "Obligatoria solo la columna Equipo. Categoría, Género (Masculino/Femenino/Mixto), Entidad (Club/Fundación) y Temporada son opcionales.",
        "Si dejas la Categoría vacía se usa el nombre del equipo. Los equipos que ya existan se omiten.",
        "Borra las filas de ejemplo antes de subir tu listado.",
    ]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Equipos")
        notes.to_excel(w, index=False, sheet_name="Instrucciones")
        for ws in w.book.worksheets:
            for col in ws.columns:
                ws.column_dimensions[col[0].column_letter].width = max(14, min(70, max(len(str(c.value or "")) for c in col) + 2))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="plantilla_equipos.xlsx"'},
    )


@router.post("/teams/import")
async def import_teams(file: UploadFile = File(...), user=Depends(require_roles("admin", "coordinator"))):
    import pandas as pd
    from postgres_compat import _sql_literal

    club_id = require_club_context(user)
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="El archivo es demasiado grande (máx. 5 MB)")
    name = (file.filename or "").lower()
    try:
        df = pd.read_csv(io.BytesIO(content)) if name.endswith(".csv") else pd.read_excel(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {exc}")

    mapping = {}
    for col in df.columns:
        key = str(col).strip().lower()
        for target, aliases in _TEAM_COLS.items():
            if key in aliases and target not in mapping:
                mapping[target] = col
                break
    if "nombre" not in mapping:
        raise HTTPException(status_code=400, detail="Falta la columna obligatoria: Equipo")
    if len(df) > MAX_TEAM_IMPORT:
        raise HTTPException(status_code=400, detail=f"Máximo {MAX_TEAM_IMPORT} equipos por archivo")

    sport_id = await _club_sport_id_for_import(club_id)
    created, skipped, errors = [], [], []
    seen = set()
    for idx, row in df.iterrows():
        line = int(idx) + 2
        nombre = _cell(row[mapping["nombre"]])[:120]
        if not nombre:
            continue  # fila vacía
        key = nombre.lower()
        if key in seen:
            skipped.append(nombre)
            continue
        seen.add(key)
        categoria = (_cell(row[mapping["categoria"]]) if "categoria" in mapping else "")[:120] or nombre
        genero = _norm_genero(_cell(row[mapping["genero"]])) if "genero" in mapping else "MIXTO"
        ent_raw = _cell(row[mapping["entidad"]]).lower() if "entidad" in mapping else ""
        entidad = "fundacion" if ent_raw.startswith("fund") else "club"
        temporada = (_cell(row[mapping["temporada"]]) if "temporada" in mapping else "")[:16] or "25-26"
        try:
            res = await _sql_rows(
                f"""
                insert into public.teams (club_id, sport_id, nombre, categoria, genero, temporada, entidad, activo)
                values ({_sql_literal(club_id)}::uuid, {_sql_literal(sport_id)}::uuid, {_sql_literal(nombre)},
                        {_sql_literal(categoria)}, {_sql_literal(genero)}, {_sql_literal(temporada)},
                        {_sql_literal(entidad)}, true)
                on conflict (club_id, sport_id, nombre, temporada) do nothing
                returning json_build_object('id', id::text)::text;
                """
            )
            (created if res else skipped).append(nombre)
        except Exception as exc:
            errors.append({"row": line, "error": str(exc)[:160]})
    return {"created": len(created), "skipped": len(skipped), "errors": errors, "names": created[:50]}
