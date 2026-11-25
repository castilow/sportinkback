"""Training & match sheets + convocations endpoints (incl. PDF generation)."""
import asyncio
import io
from datetime import datetime, timezone
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse, Response as FastAPIResponse
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

from deps import (
    db, now_iso, uuid, APP_NAME, MAX_UPLOAD_BYTES,
    get_current_user, require_roles, require_club_context, put_object, get_object,
)

router = APIRouter()


async def _sync_captacion_destacados(match_doc: dict, user: dict) -> None:
    """Registra en captación a los jugadores marcados como destacados del club en la hoja de partido."""
    sheet_id = match_doc.get("id")
    if not sheet_id:
        return
    team = match_doc.get("team") or ""
    category = match_doc.get("category") or ""
    match_date = match_doc.get("match_date") or ""
    opponent = match_doc.get("opponent") or ""
    result = match_doc.get("result") or ""
    coach_id = match_doc.get("coach_id")
    coach_name = match_doc.get("coach_name") or ""
    for p in match_doc.get("outstanding_club") or []:
        pid = p.get("player_id")
        if not pid:
            continue
        dup = await db.captacion_entries.find_one(
            {"kind": "destacado_partido", "match_sheet_id": sheet_id, "player_id": pid,
             "club_id": match_doc.get("club_id")},
            {"_id": 0},
        )
        if dup:
            continue
        entry = {
            "id": str(uuid.uuid4()),
            "kind": "destacado_partido",
            "player_id": pid,
            "player_name": p.get("name") or "",
            "dorsal": p.get("dorsal") or "",
            "team": team,
            "category": category,
            "match_sheet_id": sheet_id,
            "match_date": match_date,
            "opponent": opponent,
            "result": result,
            "coach_id": coach_id,
            "coach_name": coach_name,
            "notes": "",
            "created_at": now_iso(),
            "created_by": user["id"],
            "created_by_name": user.get("name", ""),
        }
        entry["club_id"] = require_club_context(user)
        await db.captacion_entries.insert_one(entry)


def _captacion_visible_for_coach(entry: dict, user: dict) -> bool:
    teams = set(user.get("assigned_teams") or [])
    uid = user.get("id")
    if entry.get("team") and entry.get("team") in teams:
        return True
    if entry.get("coach_id") == uid:
        return True
    if entry.get("created_by") == uid:
        return True
    return False


# ------------------ Models ------------------
class TrainingSheetIn(BaseModel):
    team: str
    category: str = ""
    date: str
    period: str = ""
    session_number: str = ""
    attendance_label: str = ""
    obj_condicional: str = ""
    obj_tec_tac: str = ""
    obj_roles_ct: str = ""
    organization_space: str = ""
    organization_players: str = ""
    organization_tasks: str = ""
    materials: dict = {}
    tasks: List[dict] = []
    players_petos: List[dict] = []
    next_rival: str = ""
    photo_path: Optional[str] = None


class MatchSheetIn(BaseModel):
    team: str
    category: str = ""
    opponent: str
    jornada: Optional[int] = None
    competition: str = ""
    group: str = ""
    match_date: str
    result: str = ""
    # Duración del partido para calcular el % de minutos de cada jugador.
    # Sin esto la hoja de partido no alimentaba las alertas de coordinación:
    # los minutos vivían en un endpoint aparte (/minutes) que la interfaz del
    # entrenador no llegaba a usar.
    total_match_minutes: int = 0
    formation: str = ""
    formation_positions: Any = None
    starters: List[dict] = []
    substitutes: List[dict] = []
    outstanding_club: List[dict] = []
    outstanding_rival: List[dict] = []
    observations: str = ""


class CaptacionVisitaIn(BaseModel):
    """Jugador externo que viene a ver un partido / entrenamiento (captación)."""
    player_name: str
    team: str = ""
    category: str = ""
    visit_date: str = ""
    notes: str = ""
    contact_phone: str = ""


class ConvocationIn(BaseModel):
    team: str
    category: str = ""
    match_date: str
    match_time: str = ""
    opponent: str
    venue: str = ""
    is_home: bool = True
    meeting_time: str = ""
    meeting_place: str = ""
    notes: str = ""
    records: List[dict] = []


# ------------------ Training sheets ------------------
@router.post("/training-sheets/upload-photo")
async def upload_training_photo(file: UploadFile = File(...), user=Depends(require_roles("coach", "coordinator", "admin"))):
    ext = (file.filename or "file").split(".")[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "heic", "heif"):
        ext = "jpg"
    path = f"{APP_NAME}/training/{user['id']}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Archivo demasiado grande (máx {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
    mime = file.content_type or "image/jpeg"
    result = put_object(path, data, mime)
    return {"storage_path": result["path"], "content_type": mime}


@router.post("/training-sheets")
async def create_training_sheet(data: TrainingSheetIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["coach_name"] = user.get("name", "")
    doc["created_at"] = now_iso()
    doc["status"] = "enviada"
    doc["kind"] = "entrenamiento"
    doc["club_id"] = require_club_context(user)
    await db.training_sheets.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/training-sheets")
async def list_training_sheets(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {"club_id": require_club_context(user)}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]
    return await db.training_sheets.find(q, {"_id": 0}).sort("date", -1).to_list(500)


@router.get("/training-sheets/{sheet_id}/photo")
async def training_photo(sheet_id: str, request: Request):
    user = await get_current_user(request)
    sheet = await db.training_sheets.find_one(
        {"id": sheet_id, "club_id": require_club_context(user)}, {"_id": 0})
    if not sheet or not sheet.get("photo_path"):
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    data, ct = get_object(sheet["photo_path"])
    return FastAPIResponse(content=data, media_type=ct)


# ------------------ Match sheets ------------------
LOW_MINUTES_PCT = 30


# Duración estándar (minutos) por categoría, igual que en el frontend.
_CATEGORY_MATCH_MINUTES = (
    ("prebenjamin", 40), ("benjamin", 40), ("alevin", 50), ("infantil", 60),
    ("cadete", 70), ("juvenil", 90), ("senior", 90), ("filial", 90), ("femenino", 90),
)


def default_match_minutes(category: str) -> int:
    import unicodedata
    norm = unicodedata.normalize("NFKD", category or "").encode("ascii", "ignore").decode().lower()
    for key, mins in _CATEGORY_MATCH_MINUTES:
        if key in norm:
            return mins
    return 60


def _to_int(val) -> Optional[int]:
    try:
        n = int(float(str(val).strip()))
    except (TypeError, ValueError):
        return None
    return n


def _row_minutes(row: dict, is_starter: bool, total: int) -> int:
    """Minutos jugados de una fila de la hoja.

    Usa ``minutes`` si el frontend lo manda. Si no, lo calcula con el minuto
    de cambio: un titular juega hasta que sale (o el partido entero); un
    suplente juega desde que entra hasta el final (0 si no entra).
    """
    explicit = _to_int(row.get("minutes"))
    if explicit is not None and explicit >= 0:
        return min(explicit, total) if total > 0 else explicit
    sub = _to_int(row.get("substitution_minute"))
    if total <= 0:
        return 0
    if is_starter:
        return min(sub, total) if sub and sub > 0 else total
    return max(0, total - sub) if sub and sub > 0 else 0


def _match_sheet_minutes(doc: dict, total: int = 0) -> list[dict]:
    """Minutos por jugador a partir de titulares y suplentes de la hoja."""
    out = []
    for is_starter, key in ((True, "starters"), (False, "substitutes")):
        for row in doc.get(key) or []:
            pid = row.get("player_id") or row.get("id")
            if not pid:
                continue
            minutes = _row_minutes(row, is_starter, total)
            row["minutes"] = minutes  # queda guardado en la hoja para verlo sin PDF
            out.append({"player_id": pid, "name": row.get("name", ""), "minutes": minutes,
                        "starter": is_starter})
    return out


@router.post("/match-sheets")
async def create_match_sheet(data: MatchSheetIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["coach_name"] = user.get("name", "")
    doc["created_at"] = now_iso()
    doc["status"] = "enviada"
    # Campo común con las hojas de entrenamiento para poder listarlas juntas
    # y ordenarlas por fecha (entrenamiento usa "date", partido "match_date").
    doc["date"] = data.match_date
    doc["kind"] = "partido"

    total = int(doc.get("total_match_minutes") or 0) or default_match_minutes(data.category)
    doc["total_match_minutes"] = total
    minutes_rows = _match_sheet_minutes(doc, total)
    doc["minutes_records"] = minutes_rows
    doc["minutes_total_played"] = sum(r["minutes"] for r in minutes_rows)

    club_id = require_club_context(user)
    doc["club_id"] = club_id
    await db.match_sheets.insert_one(doc)

    # Registro de minutos para /stats y el panel de coordinación. Antes lo
    # enviaba el frontend en una segunda petición a /minutes dentro de un
    # try/catch: si fallaba, la hoja quedaba guardada y las alertas no salían,
    # sin que nadie se enterara. Ahora va en la misma petición.
    if total > 0 and minutes_rows:
        await db.minutes.insert_one({
            "id": str(uuid.uuid4()),
            "date": data.match_date,
            "team": data.team,
            "category": data.category,
            "opponent": data.opponent,
            "total_match_minutes": total,
            "records": minutes_rows,
            "coach_id": user["id"],
            "match_sheet_id": doc["id"],
            "club_id": club_id,
            "created_at": now_iso(),
        })

    # Alertas de pocos minutos para coordinación: por debajo del 30 %.
    if total > 0 and minutes_rows:
        alerts = [
            {"player_id": r["player_id"], "pct": round(r["minutes"] / total * 100, 1)}
            for r in minutes_rows
            if (r["minutes"] / total * 100) < LOW_MINUTES_PCT
        ]
        for a in alerts:
            await db.coordinator_alerts.insert_one({
                "id": str(uuid.uuid4()),
                "type": "low_minutes",
                "player_id": a["player_id"],
                "pct": a["pct"],
                "match_date": data.match_date,
                "team": data.team,
                "source": "match_sheet",
                "match_sheet_id": doc["id"],
                "club_id": club_id,
                "player_name": next((r["name"] for r in minutes_rows if r["player_id"] == a["player_id"]), ""),
                "opponent": data.opponent,
                "minutes": next((r["minutes"] for r in minutes_rows if r["player_id"] == a["player_id"]), 0),
                "total_match_minutes": total,
                "created_at": now_iso(),
                "read": False,
            })
        doc["alerts"] = alerts

    await _sync_captacion_destacados(doc, user)
    doc.pop("_id", None)
    return doc


@router.get("/match-sheets")
async def list_match_sheets(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {"club_id": require_club_context(user)}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]
    return await db.match_sheets.find(q, {"_id": 0}).sort("match_date", -1).to_list(500)


@router.get("/match-sheets/{sheet_id}/pdf")
async def match_sheet_pdf(sheet_id: str, user=Depends(get_current_user)):
    sheet = await db.match_sheets.find_one(
        {"id": sheet_id, "club_id": require_club_context(user)}, {"_id": 0})
    if not sheet:
        raise HTTPException(status_code=404, detail="Hoja no encontrada")
    if user.get("role") == "coach" and sheet.get("coach_id") != user["id"]:
        raise HTTPException(status_code=403, detail="Permiso denegado")
    pdf_bytes = await asyncio.to_thread(_build_match_sheet_pdf, sheet)
    fname = f"hoja_partido_{sheet.get('team','').replace(' ','_')}_vs_{sheet.get('opponent','').replace(' ','_')}_{sheet.get('match_date','')}.pdf"
    return StreamingResponse(io.BytesIO(pdf_bytes), media_type="application/pdf",
                              headers={"Content-Disposition": f'attachment; filename="{fname}"'})


def _build_match_sheet_pdf(sheet: dict) -> bytes:
    """Sync reportlab builder — invoked via asyncio.to_thread to avoid blocking the loop."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.2*cm, rightMargin=1.2*cm, topMargin=1.2*cm, bottomMargin=1.2*cm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle("H1", parent=styles["Heading1"], textColor=colors.HexColor("#003366"), spaceAfter=4)
    h2 = ParagraphStyle("H2", parent=styles["Heading2"], textColor=colors.HexColor("#003366"), spaceBefore=8, spaceAfter=4, fontSize=12)
    meta = ParagraphStyle("M", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#475569"))
    big = ParagraphStyle("B", parent=styles["Normal"], fontSize=14, alignment=1, textColor=colors.HexColor("#ED1C24"), spaceBefore=4, spaceAfter=6)

    story = [
        Paragraph("Hoja de partido", h1),
        Paragraph(f"{sheet.get('competition') or 'Liga'} · Jornada {sheet.get('jornada') or '-'} · Grupo {sheet.get('group') or '-'}", meta),
        Spacer(1, 0.2*cm),
        Paragraph(f"<b>{sheet.get('team', '')}</b> &nbsp;·&nbsp; vs &nbsp;·&nbsp; <b>{sheet.get('opponent', '')}</b>", h2),
        Paragraph(f"Resultado: {sheet.get('result') or '—'}", big),
        Paragraph(f"Fecha del partido: <b>{sheet.get('match_date', '')}</b> · Categoría: <b>{sheet.get('category', '-')}</b> · Entrenador: <b>{sheet.get('coach_name', '-')}</b>", meta),
        Spacer(1, 0.3*cm),
    ]

    def _player_table(rows, title):
        story.append(Paragraph(title, h2))
        if not rows:
            story.append(Paragraph("—", styles["Normal"])); return
        data = [["Dorsal", "Jugador", "Posición", "Min. cambio", "Min. jugados"]]
        for r in rows:
            played = r.get("minutes")
            data.append([str(r.get("dorsal", "") or "-"), r.get("name", ""), r.get("position", "") or "-",
                         str(r.get("substitution_minute", "") or "-"),
                         "-" if played is None or played == "" else f"{played}'"])
        t = Table(data, colWidths=[1.7*cm, 7*cm, 3.4*cm, 2.6*cm, 2.8*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003366")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 0), (4, -1), "CENTER"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
        ]))
        story.append(t)

    _player_table(sheet.get("starters", []), f"Titulares ({len(sheet.get('starters') or [])})")
    _player_table(sheet.get("substitutes", []), f"Suplentes ({len(sheet.get('substitutes') or [])})")

    club = sheet.get("outstanding_club") or []
    if club:
        story.append(Paragraph("Destacados del club", h2))
        story.append(Paragraph(" · ".join(f"⭐ {p.get('name','')}" for p in club), styles["Normal"]))

    rival = sheet.get("outstanding_rival") or []
    if rival:
        story.append(Paragraph("Destacados del rival", h2))
        data = [["Dorsal", "Nombre", "Posición", "Descripción"]]
        for p in rival:
            data.append([str(p.get("dorsal", "") or "-"), p.get("name", ""), p.get("position", "") or "-", p.get("description", "") or ""])
        t = Table(data, colWidths=[2*cm, 4*cm, 3*cm, 8*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ED1C24")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(t)

    if sheet.get("observations"):
        story.append(Paragraph("Observaciones", h2))
        story.append(Paragraph(sheet["observations"].replace("\n", "<br/>"), styles["Normal"]))

    story.append(Spacer(1, 1.2*cm))
    sign = Table([
        ["Firma del entrenador", "", "Firma del delegado/árbitro"],
        ["_____________________________", "", "_____________________________"],
        [sheet.get("coach_name", "") or "", "", ""],
    ], colWidths=[7*cm, 3*cm, 7*cm])
    sign.setStyle(TableStyle([
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#475569")),
        ("TEXTCOLOR", (0, 2), (-1, 2), colors.HexColor("#0f172a")),
    ]))
    story.append(sign)

    story.append(Spacer(1, 0.8*cm))
    story.append(Paragraph(f"Documento generado con Sportink · {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", meta))

    doc.build(story)
    return buf.getvalue()


# ------------------ Captación (destacados partido + visitas) ------------------
@router.get("/captacion")
async def list_captacion(
    team: Optional[str] = None,
    kind: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Lista candidatos de captación: destacados en hojas de partido y visitas manuales."""
    q: dict = {"club_id": require_club_context(user)}
    if team:
        q["team"] = team
    if kind in ("destacado_partido", "visita"):
        q["kind"] = kind
    items = await db.captacion_entries.find(q, {"_id": 0}).sort("created_at", -1).to_list(2000)
    if user.get("role") == "coach":
        items = [e for e in items if _captacion_visible_for_coach(e, user)]
    return items


@router.post("/captacion/visitas")
async def create_captacion_visita(data: CaptacionVisitaIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    if not data.player_name.strip():
        raise HTTPException(status_code=400, detail="Indica el nombre del jugador")
    if user.get("role") == "coach":
        assigned = user.get("assigned_teams") or []
        if data.team and data.team not in assigned:
            raise HTTPException(status_code=403, detail="No puedes registrar visitas para un equipo que no tienes asignado")
    entry = {
        "id": str(uuid.uuid4()),
        "club_id": require_club_context(user),
        "kind": "visita",
        "player_id": None,
        "player_name": data.player_name.strip(),
        "dorsal": "",
        "team": (data.team or "").strip(),
        "category": (data.category or "").strip(),
        "match_sheet_id": None,
        "match_date": "",
        "visit_date": (data.visit_date or "").strip(),
        "opponent": "",
        "result": "",
        "coach_id": user["id"] if user.get("role") == "coach" else None,
        "coach_name": user.get("name", ""),
        "notes": (data.notes or "").strip(),
        "contact_phone": (data.contact_phone or "").strip(),
        "created_at": now_iso(),
        "created_by": user["id"],
        "created_by_name": user.get("name", ""),
    }
    await db.captacion_entries.insert_one(entry)
    return entry


@router.delete("/captacion/{entry_id}")
async def delete_captacion_entry(entry_id: str, user=Depends(require_roles("admin", "coordinator"))):
    own = await db.captacion_entries.find_one(
        {"id": entry_id, "club_id": require_club_context(user)}, {"_id": 0, "id": 1})
    if not own:
        raise HTTPException(status_code=404, detail="Entrada no encontrada")
    r = await db.captacion_entries.delete_one({"id": entry_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Entrada no encontrada")
    return {"ok": True}


@router.get("/sheets")
async def list_all_sheets(
    date: Optional[str] = None,
    team: Optional[str] = None,
    user=Depends(get_current_user),
):
    """Hojas de entrenamiento y de partido juntas, ordenadas por fecha.

    Las dos colecciones guardaban la fecha con nombres distintos ("date" y
    "match_date"), así que no había forma de verlas en un mismo listado.
    """
    q = {"club_id": require_club_context(user)}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]

    trainings, matches = await asyncio.gather(
        db.training_sheets.find(q, {"_id": 0}).to_list(500),
        db.match_sheets.find(q, {"_id": 0}).to_list(500),
    )

    items = []
    for t in trainings:
        items.append({**t, "kind": t.get("kind") or "entrenamiento", "date": t.get("date") or ""})
    for m in matches:
        items.append({
            **m,
            "kind": m.get("kind") or "partido",
            # Las hojas antiguas no tienen "date": se deriva de match_date.
            "date": m.get("date") or m.get("match_date") or "",
        })

    if date:
        items = [i for i in items if i.get("date") == date]

    items.sort(key=lambda i: (i.get("date") or "", i.get("created_at") or ""), reverse=True)
    return items


# ------------------ Control de hojas ------------------
# Quién ha rellenado la hoja de partido (y los entrenos / convocatoria) y quién
# no. Cruza los equipos del club con el calendario RFFM para saber qué equipos
# jugaron y con las hojas enviadas.

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _CTRL_TZ = _ZoneInfo("Europe/Madrid")
except Exception:  # sin tzdata
    _CTRL_TZ = timezone.utc

_LATE_DAYS = 2  # una hoja enviada más de 2 días después del partido cuenta como «tarde»


def _today_madrid():
    return datetime.now(_CTRL_TZ).date()


def _iso_day(val) -> Optional[str]:
    """Fecha en ISO (YYYY-MM-DD) desde los formatos que usan las hojas y la RFFM."""
    if val is None:
        return None
    s = str(val).strip()[:10]
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm_team(name) -> str:
    import unicodedata
    import re as _re
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().lower()
    return _re.sub(r"[^a-z0-9]+", " ", s).strip()


def _week_bounds(desde: Optional[str], hasta: Optional[str]):
    """Semana a revisar. Sin fechas: de lunes a miércoles se muestra la semana
    anterior (el fin de semana que se acaba de jugar), el resto la actual."""
    from datetime import timedelta
    d = _iso_day(desde)
    h = _iso_day(hasta)
    if d and h:
        if d > h:
            d, h = h, d
        return d, h
    today = _today_madrid()
    base = datetime.fromisoformat(d).date() if d else (today - timedelta(days=7) if today.weekday() <= 2 else today)
    monday = base - timedelta(days=base.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def _days_between(a: str, b: str) -> int:
    return (datetime.fromisoformat(b).date() - datetime.fromisoformat(a).date()).days


def _season_start() -> str:
    from players_store import TEMPORADA
    try:
        year = 2000 + int(str(TEMPORADA).split("-")[0])
    except Exception:
        year = _today_madrid().year if _today_madrid().month >= 8 else _today_madrid().year - 1
    return f"{year}-08-01"


async def _club_teams(user: dict) -> list[dict]:
    """Equipos activos de la temporada con su entrenador."""
    from routes.clubs import list_teams
    from players_store import TEMPORADA
    try:
        teams = await list_teams(temporada=TEMPORADA, include_inactive=False, user={**user, "role": "admin"})
    except Exception:
        teams = []
    coaches: dict[str, list[str]] = {}
    if hasattr(db, "fetch_json_rows"):
        from postgres_compat import _sql_literal
        try:
            rows = await db.fetch_json_rows(
                f"""
                select json_build_object('name', coalesce(nombre, email),
                                         'teams', coalesce(equipos_asignados, '{{}}'))::text
                from public.profiles
                where club_id = {_sql_literal(require_club_context(user))}::uuid and rol = 'coach';
                """
            )
        except Exception:
            rows = []
        for r in rows:
            for t in r.get("teams") or []:
                coaches.setdefault(_norm_team(t), []).append(r.get("name") or "")
    out = []
    for t in teams:
        name = t.get("nombre") or ""
        names = [n for n in coaches.get(_norm_team(name), []) if n]
        if t.get("coach_name") and t["coach_name"] not in names:
            names.insert(0, t["coach_name"])
        out.append({"id": t.get("id"), "team": name, "category": t.get("categoria") or "", "coaches": names})
    return out


def _rffm_index(team_names: list[str], rffm_matches: list[dict]) -> dict[str, list[dict]]:
    """Asocia los partidos RFFM a los equipos del club por nombre (sin tildes ni
    mayúsculas; si no coincide exacto, por palabras contenidas)."""
    by_rffm: dict[str, list[dict]] = {}
    for m in rffm_matches:
        by_rffm.setdefault(_norm_team(m.get("team_name")), []).append(m)
    result: dict[str, list[dict]] = {}
    keys = list(by_rffm)
    for name in team_names:
        n = _norm_team(name)
        if n in by_rffm:
            result[name] = by_rffm[n]
            continue
        toks = set(n.split())
        cands = [k for k in keys if toks and len(toks) >= 2 and (toks <= set(k.split()) or set(k.split()) <= toks)]
        if len(cands) == 1:
            result[name] = by_rffm[cands[0]]
    return result


def _pair_sheets(fixtures: list[dict], sheets: list[dict]):
    """Empareja cada partido con su hoja (misma fecha ±1 día). Devuelve el
    emparejamiento y las hojas que no corresponden a ningún partido RFFM
    (amistosos, copa…)."""
    used: set[str] = set()
    pairs = []
    for f in sorted(fixtures, key=lambda x: x["date"]):
        best = None
        for sh in sheets:
            if sh["id"] in used or not sh.get("_day"):
                continue
            diff = abs(_days_between(f["date"], sh["_day"]))
            if diff <= 1 and (best is None or diff < best[0]):
                best = (diff, sh)
        if best:
            used.add(best[1]["id"])
        pairs.append((f, best[1] if best else None))
    extra = [sh for sh in sheets if sh["id"] not in used]
    return pairs, extra


def _fixture(m: dict) -> Optional[dict]:
    day = _iso_day(m.get("match_date"))
    if not day:
        return None
    return {
        "date": day,
        "time": m.get("match_time") or "",
        "opponent": m.get("opponent") or "",
        "home": bool(m.get("is_rayo_home")),
        "jornada": m.get("jornada"),
        "venue": m.get("official_field") or m.get("venue") or "",
        "score": m.get("score"),
    }


def _sheet_status(fixture_day: str, sheet: Optional[dict], today: str) -> str:
    if sheet:
        created = _iso_day(sheet.get("created_at")) or sheet.get("_day")
        if created and _days_between(fixture_day, created) > _LATE_DAYS:
            return "tarde"
        return "enviada"
    if fixture_day < today:
        return "falta"
    if fixture_day == today:
        return "hoy"
    return "proxima"


async def _load_control_data(user: dict, since: str):
    club = require_club_context(user)
    teams, rffm, msheets, tsheets, convs = await asyncio.gather(
        _club_teams(user),
        db.rffm_matches.find({"club_id": club}, {"_id": 0}).to_list(5000),
        db.match_sheets.find({"club_id": club}, {"_id": 0}).to_list(5000),
        db.training_sheets.find({"club_id": club}, {"_id": 0, "id": 1, "team": 1, "date": 1, "created_at": 1}).to_list(5000),
        db.convocations.find({"club_id": club}, {"_id": 0, "id": 1, "team": 1, "match_date": 1, "opponent": 1, "created_at": 1}).to_list(5000),
    )
    for sh in msheets:
        sh["_day"] = _iso_day(sh.get("match_date") or sh.get("date"))
    for sh in tsheets:
        sh["_day"] = _iso_day(sh.get("date"))
    for c in convs:
        c["_day"] = _iso_day(c.get("match_date"))

    # Equipos con hojas pero que no están en el catálogo (nombres antiguos): se
    # añaden para que no se pierdan de vista.
    known = {_norm_team(t["team"]) for t in teams}
    for sh in msheets + tsheets:
        n = _norm_team(sh.get("team"))
        if n and n not in known and (sh.get("_day") or "") >= since:
            known.add(n)
            teams.append({"id": None, "team": sh.get("team"), "category": sh.get("category") or "", "coaches": [sh.get("coach_name")] if sh.get("coach_name") else []})

    def group(items):
        out: dict[str, list[dict]] = {}
        for it in items:
            out.setdefault(_norm_team(it.get("team")), []).append(it)
        return out

    rffm_by_team = _rffm_index([t["team"] for t in teams], rffm)
    return teams, rffm_by_team, group(msheets), group(tsheets), group(convs)


@router.get("/sheets/control")
async def sheets_control(
    desde: Optional[str] = None,
    hasta: Optional[str] = None,
    user=Depends(require_roles("admin", "coordinator")),
):
    """Control semanal: por equipo, si ha enviado la hoja de partido, cuántos
    entrenos ha registrado y si hizo convocatoria; y su cumplimiento en la temporada."""
    d, h = _week_bounds(desde, hasta)
    today = _today_madrid().isoformat()
    season_from = _season_start()
    teams, rffm_by_team, ms_by, ts_by, cv_by = await _load_control_data(user, min(d, season_from))

    rows = []
    for t in teams:
        key = _norm_team(t["team"])
        fixtures_all = [f for f in (_fixture(m) for m in rffm_by_team.get(t["team"], [])) if f]
        sheets_all = [s for s in ms_by.get(key, []) if s.get("_day")]

        # --- semana
        week_fx = [f for f in fixtures_all if d <= f["date"] <= h]
        week_sheets = [s for s in sheets_all if _days_between(d, s["_day"]) >= -1 and _days_between(s["_day"], h) >= -1]
        pairs, extra = _pair_sheets(week_fx, week_sheets)
        extra = [s for s in extra if d <= s["_day"] <= h]
        convs = [c for c in cv_by.get(key, []) if c.get("_day")]
        matches = []
        for f, sh in pairs:
            conv = next((c for c in convs if abs(_days_between(f["date"], c["_day"])) <= 1), None)
            matches.append({
                **f,
                "status": _sheet_status(f["date"], sh, today),
                "sheet_id": sh.get("id") if sh else None,
                "sheet_sent_at": sh.get("created_at") if sh else None,
                "result": (sh or {}).get("result") or None,
                "convocation": bool(conv),
            })
        for sh in extra:
            matches.append({
                "date": sh["_day"], "time": "", "opponent": sh.get("opponent") or "",
                "home": None, "jornada": sh.get("jornada"), "venue": "", "score": None,
                "status": "enviada", "sheet_id": sh.get("id"), "sheet_sent_at": sh.get("created_at"),
                "result": sh.get("result") or None, "extra": True,
                "convocation": any(abs(_days_between(sh["_day"], c["_day"])) <= 1 for c in convs),
            })
        matches.sort(key=lambda m: m["date"])

        trainings = sorted({s["_day"] for s in ts_by.get(key, []) if s.get("_day") and d <= s["_day"] <= h})
        week_convs = [c for c in convs if d <= c["_day"] <= h]

        statuses = [m["status"] for m in matches]
        if "falta" in statuses:
            status = "falta"
        elif "hoy" in statuses or "proxima" in statuses:
            status = "pendiente"
        elif matches:
            status = "ok"
        elif fixtures_all:
            status = "sin_partido"   # tiene calendario pero no jugó esta semana
        else:
            status = "sin_calendario"

        # --- temporada (hasta hoy)
        past_fx = [f for f in fixtures_all if season_from <= f["date"] < today]
        s_pairs, _ = _pair_sheets(past_fx, [s for s in sheets_all if s["_day"] >= season_from])
        sent = sum(1 for _, sh in s_pairs if sh)
        late = sum(1 for f, sh in s_pairs if sh and _sheet_status(f["date"], sh, today) == "tarde")
        missing_list = [f for f, sh in s_pairs if not sh]
        last_match = max((s["_day"] for s in sheets_all), default=None)
        last_training = max((s["_day"] for s in ts_by.get(key, []) if s.get("_day")), default=None)

        rows.append({
            "team": t["team"],
            "team_id": t.get("id"),
            "category": t.get("category") or "",
            "coaches": t.get("coaches") or [],
            "rffm_linked": bool(fixtures_all),
            "status": status,
            "matches": matches,
            "trainings": len(trainings),
            "training_dates": trainings,
            "convocations": len(week_convs),
            "last_match_sheet": last_match,
            "last_training_sheet": last_training,
            "season": {
                "expected": len(past_fx),
                "sent": sent,
                "late": late,
                "missing": len(missing_list),
                "pct": round(100 * sent / len(past_fx)) if past_fx else None,
            },
        })

    order = {"falta": 0, "pendiente": 1, "sin_calendario": 2, "sin_partido": 3, "ok": 4}
    rows.sort(key=lambda r: (order.get(r["status"], 9), r["team"].lower()))
    summary = {k: sum(1 for r in rows if r["status"] == k) for k in order}
    summary["teams"] = len(rows)
    summary["missing_sheets"] = sum(1 for r in rows for m in r["matches"] if m["status"] == "falta")
    summary["without_training"] = sum(1 for r in rows if r["trainings"] == 0)
    return {"desde": d, "hasta": h, "today": today, "season_from": season_from, "summary": summary, "teams": rows}


@router.get("/sheets/mis-pendientes")
async def my_pending_sheets(user=Depends(get_current_user)):
    """Para el entrenador: partidos ya jugados (últimas 3 semanas) de sus equipos
    sin hoja de partido. Se usa para el aviso del panel."""
    from datetime import timedelta
    allowed = [str(t).strip("{} ") for t in (user.get("assigned_teams") or []) if str(t).strip("{} ")]
    if user.get("role") != "coach" or not allowed:
        return []
    club = require_club_context(user)
    today = _today_madrid()
    since = (today - timedelta(days=21)).isoformat()
    rffm, msheets = await asyncio.gather(
        db.rffm_matches.find({"club_id": club}, {"_id": 0}).to_list(5000),
        db.match_sheets.find({"club_id": club}, {"_id": 0, "id": 1, "team": 1, "match_date": 1, "date": 1}).to_list(5000),
    )
    idx = _rffm_index(allowed, rffm)
    out = []
    for team in allowed:
        fx = [f for f in (_fixture(m) for m in idx.get(team, [])) if f and since <= f["date"] < today.isoformat()]
        sheets = []
        for s in msheets:
            if _norm_team(s.get("team")) == _norm_team(team):
                s["_day"] = _iso_day(s.get("match_date") or s.get("date"))
                sheets.append(s)
        pairs, _ = _pair_sheets(fx, sheets)
        for f, sh in pairs:
            if not sh:
                out.append({**f, "team": team, "days_late": _days_between(f["date"], today.isoformat())})
    out.sort(key=lambda m: m["date"])
    return out


# ------------------ Convocations ------------------
def build_convocation_text(data, names: list[str]) -> str:
    """Texto de la convocatoria listo para pegar en WhatsApp."""
    home_away = "LOCAL" if data.is_home else "VISITANTE"
    lines = [
        f"📣 CONVOCATORIA · {data.match_date} {data.match_time}".strip(),
        f"{home_away}: {data.team} vs {data.opponent}",
    ]
    if data.venue:
        lines.append(data.venue)
    if data.meeting_time:
        lines.append(f"⏰ Citación: {data.meeting_time}" + (f" en {data.meeting_place}" if data.meeting_place else ""))
    if data.notes:
        lines.append(f"📝 {data.notes}")
    lines += ["", f"Convocados ({len(names)}):"] + [f"• {n}" for n in names]
    return "\n".join(lines)


@router.post("/convocations")
async def create_convocation(data: ConvocationIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["coach_name"] = user.get("name", "")
    doc["created_at"] = now_iso()
    doc["club_id"] = require_club_context(user)
    await db.convocations.insert_one(doc)

    # Texto para WhatsApp: el club ya no usa el chat interno, así que en vez de
    # publicarlo en la sala se devuelve para que el entrenador lo copie.
    called_ids = {r["player_id"] for r in data.records if r.get("called")}
    called_players = await db.players.find(
        {"id": {"$in": list(called_ids)}, "club_id": require_club_context(user)}, {"_id": 0, "id": 1, "name": 1}
    ).to_list(200) if called_ids else []
    names = sorted([p["name"] for p in called_players])
    whatsapp_text = build_convocation_text(data, names)

    await db.convocations.update_one(
        {"id": doc["id"]}, {"$set": {"whatsapp_text": whatsapp_text}}
    )
    doc["whatsapp_text"] = whatsapp_text
    doc.pop("_id", None)
    return doc


@router.get("/convocations")
async def list_convocations(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {"club_id": require_club_context(user)}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]
    return await db.convocations.find(q, {"_id": 0}).sort("match_date", -1).to_list(500)
