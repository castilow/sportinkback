"""Training & match sheets + convocations endpoints (incl. PDF generation)."""
import asyncio
import io
from datetime import datetime, timezone
from typing import List, Optional

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
    get_current_user, require_roles, put_object, get_object,
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
            {"kind": "destacado_partido", "match_sheet_id": sheet_id, "player_id": pid},
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
    await db.training_sheets.insert_one(doc)
    doc.pop("_id", None)
    return doc


@router.get("/training-sheets")
async def list_training_sheets(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]
    return await db.training_sheets.find(q, {"_id": 0}).sort("date", -1).to_list(500)


@router.get("/training-sheets/{sheet_id}/photo")
async def training_photo(sheet_id: str, request: Request):
    await get_current_user(request)
    sheet = await db.training_sheets.find_one({"id": sheet_id}, {"_id": 0})
    if not sheet or not sheet.get("photo_path"):
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    data, ct = get_object(sheet["photo_path"])
    return FastAPIResponse(content=data, media_type=ct)


# ------------------ Match sheets ------------------
@router.post("/match-sheets")
async def create_match_sheet(data: MatchSheetIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["coach_name"] = user.get("name", "")
    doc["created_at"] = now_iso()
    doc["status"] = "enviada"
    await db.match_sheets.insert_one(doc)
    await _sync_captacion_destacados(doc, user)
    doc.pop("_id", None)
    return doc


@router.get("/match-sheets")
async def list_match_sheets(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]
    return await db.match_sheets.find(q, {"_id": 0}).sort("match_date", -1).to_list(500)


@router.get("/match-sheets/{sheet_id}/pdf")
async def match_sheet_pdf(sheet_id: str, user=Depends(get_current_user)):
    sheet = await db.match_sheets.find_one({"id": sheet_id}, {"_id": 0})
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
        Paragraph("C.F. Rayo Majadahonda — Hoja de partido", h1),
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
        data = [["Dorsal", "Jugador", "Posición", "Min. sub."]]
        for r in rows:
            data.append([str(r.get("dorsal", "") or "-"), r.get("name", ""), r.get("position", "") or "-", str(r.get("substitution_minute", "") or "-")])
        t = Table(data, colWidths=[2*cm, 8*cm, 4*cm, 3*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003366")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
            ("ALIGN", (0, 0), (0, -1), "CENTER"),
            ("ALIGN", (3, 0), (3, -1), "CENTER"),
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
    story.append(Paragraph(f"Documento generado por Rayo Majadahonda Digital · {datetime.now(timezone.utc).strftime('%d/%m/%Y %H:%M UTC')}", meta))

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
    q: dict = {}
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
    r = await db.captacion_entries.delete_one({"id": entry_id})
    if r.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Entrada no encontrada")
    return {"ok": True}


# ------------------ Convocations ------------------
@router.post("/convocations")
async def create_convocation(data: ConvocationIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["coach_name"] = user.get("name", "")
    doc["created_at"] = now_iso()
    await db.convocations.insert_one(doc)

    # Auto-post to team chat if room exists
    room = await db.team_rooms.find_one({"team": data.team})
    if room:
        called_ids = {r["player_id"] for r in data.records if r.get("called")}
        players = await db.players.find({"id": {"$in": list(called_ids)}}, {"_id": 0, "id": 1, "name": 1}).to_list(200)
        names = sorted([p["name"] for p in players])
        home_away = "LOCAL" if data.is_home else "VISITANTE"
        msg = (f"📣 CONVOCATORIA · {data.match_date} {data.match_time}\n"
               f"{home_away}: CF Rayo Majadahonda vs {data.opponent}\n"
               f"{data.venue}\n"
               + (f"⏰ Citación: {data.meeting_time} en {data.meeting_place}\n" if data.meeting_time else "")
               + (f"📝 {data.notes}\n" if data.notes else "")
               + f"\nConvocados ({len(names)}):\n" + "\n".join(f"• {n}" for n in names))
        await db.chat_messages.insert_one({
            "id": str(uuid.uuid4()),
            "room_id": room["id"],
            "author_name": user.get("name", "Entrenador"),
            "author_role": "coach",
            "text": msg,
            "kind": "convocation",
            "created_at": now_iso(),
        })
    doc.pop("_id", None)
    return doc


@router.get("/convocations")
async def list_convocations(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team:
        q["team"] = team
    if user.get("role") == "coach":
        q["coach_id"] = user["id"]
    return await db.convocations.find(q, {"_id": 0}).sort("match_date", -1).to_list(500)
