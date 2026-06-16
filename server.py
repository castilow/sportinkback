from dotenv import load_dotenv
from pathlib import Path as _Path
ROOT_DIR = _Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

import io
import os
import uuid
from datetime import datetime, timezone, timedelta, date
from typing import List, Optional, Literal

import pandas as pd
import requests  # used only by RFFM scraping (kept local to this module)
from fastapi import FastAPI, APIRouter, HTTPException, Depends, Request, Response, UploadFile, File, Form, Query, Header
from fastapi.responses import StreamingResponse, Response as FastAPIResponse
from starlette.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

# Shared infrastructure lives in deps.py (imported by the feature routers too)
from deps import (
    db, logger, APP_NAME, MAX_UPLOAD_BYTES,
    now_iso, rate_limit_ip,
    init_storage, put_object, get_object,
    set_auth_cookies, clear_auth_cookies, sign_in_with_supabase, sign_out_supabase,
    get_current_user, require_roles,
    ensure_supabase_staff_user, delete_supabase_auth_user,
)

CATEGORIES = ["Prebenjamín", "Benjamín", "Alevín", "Infantil", "Cadete", "Juvenil", "Senior/Filial", "Femenino"]
ENTITIES = ("club", "fundacion")
ROLES = ("admin", "coordinator", "coach", "office", "physio")
KIT_ITEM_CODES = [
    ("camiseta", "Camiseta entrenamiento"),
    ("equipacion_blanca", "Equipacion blanca"),
    ("equipacion_azul", "Equipacion azul"),
    ("calcetines_blancos", "Calcetines blancos"),
    ("calcetines_azules", "Calcetines azules"),
    ("pantalon_largo", "Pantalon largo"),
    ("pantalon_corto", "Pantalon corto"),
    ("sudadera", "Sudadera"),
    ("abrigo", "Abrigo"),
    ("mochila", "Mochila"),
    ("chubasquero", "Chubasquero"),
]

app = FastAPI(title="Rayo Majadahonda Digital API")
api_router = APIRouter(prefix="")


# ------------------ Models ------------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str
    name: str
    role: Literal["admin", "coordinator", "coach", "office", "physio"]
    assigned_teams: List[str] = []


class UserOut(BaseModel):
    id: str
    email: str
    name: str
    role: str
    assigned_teams: List[str] = []
    created_at: Optional[str] = None


class PlayerIn(BaseModel):
    name: str
    dni: str = ""
    birthdate: Optional[str] = None  # ISO date
    category: str
    team: str = ""
    entity: Literal["club", "fundacion"] = "club"
    payment_status: bool = True  # True = al día
    insurance_expiry: Optional[str] = None  # ISO date
    dni_expiry: Optional[str] = None
    phone: str = ""
    email: str = ""
    notes: str = ""


class PlayerOut(PlayerIn):
    id: str
    created_at: Optional[str] = None


class AttendanceIn(BaseModel):
    date: str  # ISO date
    team: str
    category: str
    records: List[dict]  # [{player_id, present: bool}]


class MinutesIn(BaseModel):
    date: str
    team: str
    category: str
    opponent: str = ""
    total_match_minutes: int = 60
    records: List[dict]  # [{player_id, minutes}]


class InjuryIn(BaseModel):
    player_id: str
    status: Literal["Disponible", "Duda", "Baja"]
    notes: str = ""
    reason: str = ""
    estimated_return_days: Optional[int] = None
    diagnosis: str = ""
    body_area: str = ""
    treatment_plan: str = ""


class IncidentIn(BaseModel):
    title: str
    description: str
    location: str = ""
    severity: Literal["baja", "media", "alta"] = "media"


class InventoryItemIn(BaseModel):
    item: str
    quantity: int
    assigned_to_team: str = ""
    assigned_to_user_id: str = ""
    status: str = "ok"


class InventoryConfirmIn(BaseModel):
    status: Literal["ok", "deteriorado", "perdido"]
    notes: str = ""


class OfficePlayerUpdateIn(BaseModel):
    payment_status: Optional[bool] = None


class PlayerKitItemIn(BaseModel):
    code: str
    label: str
    quantity: int = 1
    size: str = ""
    status: Literal["pendiente", "entregado", "incidencia"] = "pendiente"
    notes: str = ""


class PlayerKitIn(BaseModel):
    season: str = "2025/26"
    items: List[PlayerKitItemIn] = Field(default_factory=list)
    notes: str = ""


class InjuryFollowUpIn(BaseModel):
    status: Literal["Disponible", "Duda", "Baja"]
    notes: str = ""
    estimated_return_days: Optional[int] = None
    diagnosis: str = ""
    body_area: str = ""
    treatment_plan: str = ""


class ReminderSendIn(BaseModel):
    player_id: str
    channel: Literal["whatsapp", "email"]
    message: str = ""


# ------------------ Utils ------------------
def semaforo_for_expiry(expiry_iso: Optional[str]) -> str:
    if not expiry_iso:
        return "red"
    try:
        d = datetime.fromisoformat(expiry_iso.replace("Z", "+00:00")).date() if "T" in expiry_iso else date.fromisoformat(expiry_iso)
    except Exception:
        return "red"
    today = date.today()
    days = (d - today).days
    if days < 0:
        return "red"
    if days < 15:
        return "orange"
    if days < 30:
        return "orange"
    return "green"


def enrich_player(p: dict) -> dict:
    p.pop("_id", None)
    p["entity"] = p.get("entity") or "club"
    p["payment_semaforo"] = "green" if p.get("payment_status") else "red"
    if "insurance_semaforo" not in p:
        p["insurance_semaforo"] = semaforo_for_expiry(p.get("insurance_expiry"))
    if "dni_semaforo" not in p:
        p["dni_semaforo"] = semaforo_for_expiry(p.get("dni_expiry"))
    p["has_attention"] = player_has_attention(p)
    return p


def player_has_attention(p: dict) -> bool:
    payment_level = "green" if p.get("payment_status") else "red"
    insurance_level = semaforo_for_expiry(p.get("insurance_expiry"))
    dni_level = semaforo_for_expiry(p.get("dni_expiry"))
    return payment_level != "green" or insurance_level != "green" or dni_level != "green"


def summarize_kit_items(items: List[dict]) -> dict:
    total = len(items or [])
    delivered = sum(1 for item in items or [] if item.get("status") == "entregado")
    incidents = sum(1 for item in items or [] if item.get("status") == "incidencia")
    pending = sum(1 for item in items or [] if item.get("status") == "pendiente")
    if total == 0:
        status = "missing"
    elif incidents > 0:
        status = "issue"
    elif delivered == total:
        status = "complete"
    else:
        status = "partial"
    return {
        "status": status,
        "total_items": total,
        "delivered_items": delivered,
        "pending_items": pending,
        "issue_items": incidents,
    }


# ------------------ Auth endpoints ------------------
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_MINUTES = 15


async def _get_login_attempts(identifier: str) -> dict:
    record = await db.login_attempts.find_one({"identifier": identifier}, {"_id": 0})
    return record or {"identifier": identifier, "count": 0, "locked_until": None}


async def _register_failed_login(identifier: str):
    record = await _get_login_attempts(identifier)
    count = record.get("count", 0) + 1
    update = {"identifier": identifier, "count": count, "last_attempt": now_iso()}
    if count >= LOGIN_MAX_ATTEMPTS:
        update["locked_until"] = (datetime.now(timezone.utc) + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)).isoformat()
    await db.login_attempts.update_one({"identifier": identifier}, {"$set": update}, upsert=True)


async def _clear_login_attempts(identifier: str):
    await db.login_attempts.delete_one({"identifier": identifier})


async def _check_login_lockout(identifier: str):
    record = await db.login_attempts.find_one({"identifier": identifier}, {"_id": 0})
    if not record:
        return
    locked_until = record.get("locked_until")
    if not locked_until:
        return
    try:
        until = datetime.fromisoformat(locked_until)
    except Exception:
        return
    if datetime.now(timezone.utc) < until:
        remaining = int((until - datetime.now(timezone.utc)).total_seconds() / 60) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Vuelve a intentarlo en {remaining} minuto(s).",
        )
    # Lockout expired: reset
    await _clear_login_attempts(identifier)


@api_router.post("/auth/login")
async def login(data: LoginIn, request: Request, response: Response):
    email = data.email.lower()
    # Prefer client-provided real IP from ingress; fallback to direct client host.
    xff = request.headers.get("x-forwarded-for", "")
    ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
    identifier = f"{ip}:{email}"
    # Also lock purely by email to prevent IP-rotation brute force.
    email_identifier = f"email:{email}"

    await _check_login_lockout(identifier)
    await _check_login_lockout(email_identifier)

    session = None
    try:
        session = sign_in_with_supabase(email, data.password)
    except HTTPException:
        await _register_failed_login(identifier)
        await _register_failed_login(email_identifier)
        raise HTTPException(status_code=401, detail="Credenciales inválidas")

    auth_user = session.get("user") or {}
    user = await db.users.find_one({"auth_user_id": auth_user.get("id")}, {"_id": 0, "password_hash": 0})
    if not user:
        user = await db.users.find_one({"email": email}, {"_id": 0, "password_hash": 0})
        if user and user.get("auth_user_id") != auth_user.get("id"):
            await db.users.update_one({"id": user["id"]}, {"$set": {"auth_user_id": auth_user.get("id")}})
            user["auth_user_id"] = auth_user.get("id")
    if not user:
        await _register_failed_login(identifier)
        await _register_failed_login(email_identifier)
        raise HTTPException(status_code=401, detail="Usuario no autorizado en esta aplicación")

    await _clear_login_attempts(identifier)
    await _clear_login_attempts(email_identifier)
    set_auth_cookies(response, session["access_token"], session["refresh_token"])
    user.pop("_id", None)
    user.pop("password_hash", None)
    return user


@api_router.post("/auth/logout")
async def logout(request: Request, response: Response):
    token = request.cookies.get("access_token")
    if token:
        sign_out_supabase(token)
    clear_auth_cookies(response)
    return {"ok": True}


@api_router.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return user


# ------------------ User management (admin) ------------------
@api_router.get("/users")
async def list_users(user=Depends(require_roles("admin"))):
    users = await db.users.find({}, {"_id": 0, "password_hash": 0}).to_list(500)
    return users


@api_router.post("/users")
async def create_user(data: UserCreate, user=Depends(require_roles("admin"))):
    email = data.email.lower()
    existing = await db.users.find_one({"email": email})
    if existing:
        raise HTTPException(status_code=400, detail="El email ya existe")
    new_id = str(uuid.uuid4())
    auth_user = ensure_supabase_staff_user(
        email,
        data.password,
        name=data.name,
        role=data.role,
        assigned_teams=data.assigned_teams,
        app_user_id=new_id,
    )
    new_user = {
        "id": new_id,
        "auth_user_id": auth_user["id"],
        "email": email,
        "name": data.name,
        "role": data.role,
        "assigned_teams": data.assigned_teams,
        "created_at": now_iso(),
    }
    await db.users.insert_one(new_user)
    new_user.pop("_id", None)
    new_user.pop("password_hash", None)
    return new_user


@api_router.delete("/users/{user_id}")
async def delete_user(user_id: str, user=Depends(require_roles("admin"))):
    if user_id == user["id"]:
        raise HTTPException(status_code=400, detail="No puedes eliminarte a ti mismo")
    target = await db.users.find_one({"id": user_id}, {"_id": 0})
    if not target:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if target.get("auth_user_id"):
        delete_supabase_auth_user(target["auth_user_id"])
    result = await db.users.delete_one({"id": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {"ok": True}


# ------------------ Players CRUD ------------------
@api_router.get("/players")
async def list_players(
    category: Optional[str] = None,
    team: Optional[str] = None,
    search: Optional[str] = None,
    entity: Optional[str] = None,
    attention: Optional[bool] = None,
    user=Depends(get_current_user),
):
    query = {}
    if category:
        query["category"] = category
    if team:
        query["team"] = team
    # Coaches only see their assigned teams (equipo o categoría del SQL)
    coach_allowed: Optional[set] = None
    if user.get("role") == "coach":
        allowed = user.get("assigned_teams") or []
        if not allowed:
            return []
        coach_allowed = set(allowed)
        if team and team not in coach_allowed:
            return []
    players = await db.players.find(query, {"_id": 0}).sort("name", 1).to_list(2000)
    if coach_allowed is not None:
        players = [
            p for p in players
            if p.get("team") in coach_allowed or p.get("category") in coach_allowed
        ]
    players = [enrich_player(p) for p in players]
    if search:
        q = search.strip().lower()
        players = [
            p for p in players
            if q in (p.get("name", "").lower())
            or q in (p.get("dni", "").lower())
            or q in (p.get("team", "").lower())
            or q in (p.get("email", "").lower())
            or q in ("fundacion" if p.get("entity") == "fundacion" else "club")
        ]
    if entity:
        players = [p for p in players if p.get("entity", "club") == entity]
    if attention:
        players = [p for p in players if p.get("has_attention")]
    return players


@api_router.post("/players")
async def create_player(data: PlayerIn, user=Depends(require_roles("admin", "coordinator"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = now_iso()
    await db.players.insert_one(doc)
    doc.pop("_id", None)
    return enrich_player(doc)


@api_router.put("/players/{player_id}")
async def update_player(player_id: str, data: PlayerIn, user=Depends(require_roles("admin", "coordinator"))):
    doc = data.model_dump()
    result = await db.players.update_one({"id": player_id}, {"$set": doc})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Jugador no encontrado")
    updated = await db.players.find_one({"id": player_id}, {"_id": 0})
    return enrich_player(updated)


@api_router.delete("/players/{player_id}")
async def delete_player(player_id: str, user=Depends(require_roles("admin"))):
    await db.players.delete_one({"id": player_id})
    return {"ok": True}


@api_router.post("/players/{player_id}/office")
async def update_player_office(player_id: str, data: OfficePlayerUpdateIn, user=Depends(require_roles("admin", "coordinator", "office"))):
    player = await db.players.find_one({"id": player_id}, {"_id": 0})
    if not player:
        raise HTTPException(status_code=404, detail="Jugador no encontrado")
    update = {}
    if data.payment_status is not None:
        update["payment_status"] = data.payment_status
    if not update:
        raise HTTPException(status_code=400, detail="No hay cambios para aplicar")
    await db.players.update_one({"id": player_id}, {"$set": update})
    updated = await db.players.find_one({"id": player_id}, {"_id": 0})
    return enrich_player(updated)


# ------------------ Import Excel/CSV ------------------
COLUMN_ALIASES = {
    "name": ["nombre", "nombre del jugador", "jugador", "name", "player"],
    "dni": ["dni", "nif", "documento"],
    "birthdate": ["fecha de nacimiento", "nacimiento", "birthdate", "fecha_nacimiento", "fecha nacimiento"],
    "category": ["categoría", "categoria", "category"],
    "team": ["equipo", "team"],
    "entity": ["entidad", "entity", "club", "fundacion", "fundación"],
    "payment_status": ["estado de pago", "estado pago", "pago", "payment"],
    "insurance_expiry": ["fecha de vencimiento de seguro", "vencimiento seguro", "seguro", "insurance", "seguro_vence"],
    "dni_expiry": ["vencimiento dni", "dni vencimiento", "caducidad dni"],
}


def parse_payment(val) -> bool:
    if val is None:
        return True
    s = str(val).strip().lower()
    return s in ("sí", "si", "yes", "y", "true", "1", "ok", "pagado")


def parse_entity(val) -> str:
    s = (str(val or "")).strip().lower()
    if not s:
        return "club"
    if "fund" in s:
        return "fundacion"
    return "club"


def parse_date_val(val) -> Optional[str]:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val.date().isoformat()
    s = str(val).strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            continue
    return None


def map_columns(columns) -> dict:
    mapping = {}
    for col in columns:
        key = str(col).strip().lower()
        for target, aliases in COLUMN_ALIASES.items():
            if key in aliases:
                mapping[target] = col
                break
    return mapping


@api_router.post("/players/import/preview")
async def import_preview(file: UploadFile = File(...), user=Depends(require_roles("admin"))):
    content = await file.read()
    name = (file.filename or "").lower()
    try:
        if name.endswith(".csv"):
            df = pd.read_csv(io.BytesIO(content))
        else:
            df = pd.read_excel(io.BytesIO(content))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"No se pudo leer el archivo: {e}")

    mapping = map_columns(df.columns)
    required = ["name", "category"]
    missing = [r for r in required if r not in mapping]
    if missing:
        raise HTTPException(status_code=400, detail=f"Faltan columnas obligatorias: {', '.join(missing)}")

    rows, errors = [], []
    insurance_expired = 0
    payment_due = 0
    today = date.today()
    for idx, row in df.iterrows():
        try:
            name_val = str(row[mapping["name"]]).strip() if mapping.get("name") else ""
            if not name_val or name_val.lower() == "nan":
                errors.append({"row": int(idx) + 2, "error": "Nombre vacío"})
                continue
            cat = str(row[mapping["category"]]).strip() if mapping.get("category") else ""
            if cat not in CATEGORIES:
                errors.append({"row": int(idx) + 2, "error": f"Categoría inválida: {cat}"})
            p = {
                "name": name_val,
                "dni": str(row[mapping["dni"]]).strip() if mapping.get("dni") else "",
                "birthdate": parse_date_val(row[mapping["birthdate"]]) if mapping.get("birthdate") else None,
                "category": cat,
                "team": str(row[mapping["team"]]).strip() if mapping.get("team") else "",
                "entity": parse_entity(row[mapping["entity"]]) if mapping.get("entity") else "club",
                "payment_status": parse_payment(row[mapping["payment_status"]]) if mapping.get("payment_status") else True,
                "insurance_expiry": parse_date_val(row[mapping["insurance_expiry"]]) if mapping.get("insurance_expiry") else None,
                "dni_expiry": parse_date_val(row[mapping["dni_expiry"]]) if mapping.get("dni_expiry") else None,
                "phone": "", "email": "", "notes": "",
            }
            if not p["payment_status"]:
                payment_due += 1
            if p["insurance_expiry"]:
                try:
                    if date.fromisoformat(p["insurance_expiry"]) < today:
                        insurance_expired += 1
                except Exception:
                    pass
            rows.append(p)
        except Exception as e:
            errors.append({"row": int(idx) + 2, "error": str(e)})

    batch_id = str(uuid.uuid4())
    await db.import_batches.insert_one({
        "id": batch_id, "rows": rows, "errors": errors,
        "created_at": now_iso(), "created_by": user["id"], "committed": False,
    })
    return {
        "batch_id": batch_id,
        "total": len(rows),
        "errors": errors,
        "insurance_expired": insurance_expired,
        "payment_due": payment_due,
        "sample": rows[:8],
        "mapping": {k: str(v) for k, v in mapping.items()},
    }


@api_router.post("/players/import/commit/{batch_id}")
async def import_commit(batch_id: str, user=Depends(require_roles("admin"))):
    batch = await db.import_batches.find_one({"id": batch_id}, {"_id": 0})
    if not batch:
        raise HTTPException(status_code=404, detail="Lote no encontrado")
    if batch.get("committed"):
        raise HTTPException(status_code=400, detail="Lote ya importado")
    to_insert = []
    for r in batch["rows"]:
        r["id"] = str(uuid.uuid4())
        r["created_at"] = now_iso()
        to_insert.append(r)
    if to_insert:
        await db.players.insert_many(to_insert)
    await db.import_batches.update_one({"id": batch_id}, {"$set": {"committed": True}})
    return {"imported": len(to_insert)}


# ------------------ Export Excel ------------------
@api_router.get("/players/export.xlsx")
async def export_players(category: Optional[str] = None, team: Optional[str] = None,
                          user=Depends(require_roles("admin", "coordinator"))):
    query = {}
    if category: query["category"] = category
    if team: query["team"] = team
    players = await db.players.find(query, {"_id": 0}).to_list(5000)
    df = pd.DataFrame([enrich_player(p) for p in players])
    if df.empty:
        df = pd.DataFrame(columns=["name", "dni", "category", "team", "payment_status", "insurance_expiry"])
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Jugadores")
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="jugadores_{date.today().isoformat()}.xlsx"'},
    )


# ------------------ PDF Monthly report ------------------
@api_router.get("/reports/monthly.pdf")
async def monthly_report(user=Depends(require_roles("admin", "coordinator"))):
    players = await db.players.find({}, {"_id": 0}).to_list(5000)
    morosos = [p for p in players if not p.get("payment_status")]
    today = date.today()
    month_start = today.replace(day=1)
    attendance = await db.attendance.find({"date": {"$gte": month_start.isoformat()}}, {"_id": 0}).to_list(2000)

    attendance_counts = {}
    for a in attendance:
        for r in a.get("records", []):
            pid = r.get("player_id")
            if not pid:
                continue
            c = attendance_counts.setdefault(pid, {"total": 0, "present": 0})
            c["total"] += 1
            if r.get("present"):
                c["present"] += 1

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=1.5*cm, rightMargin=1.5*cm, topMargin=1.5*cm, bottomMargin=1.5*cm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("T", parent=styles["Heading1"], textColor=colors.HexColor("#003366"))
    story = [
        Paragraph(f"CF Rayo Majadahonda — Informe mensual ({today.strftime('%B %Y')})", title),
        Spacer(1, 0.4*cm),
        Paragraph(f"Total jugadores: <b>{len(players)}</b> · Morosos: <b style='color:#ED1C24'>{len(morosos)}</b>", styles["Normal"]),
        Spacer(1, 0.6*cm),
        Paragraph("<b>Morosidad</b>", styles["Heading2"]),
    ]
    if morosos:
        tdata = [["Jugador", "Categoría", "Equipo", "DNI"]]
        for p in morosos[:200]:
            tdata.append([p.get("name", ""), p.get("category", ""), p.get("team", ""), p.get("dni", "")])
        t = Table(tdata, colWidths=[6*cm, 4*cm, 4*cm, 3.5*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003366")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F8FAFC")]),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("Sin morosidad registrada.", styles["Normal"]))

    story += [Spacer(1, 0.8*cm), Paragraph("<b>Asistencia (mes)</b>", styles["Heading2"])]
    if attendance_counts:
        tdata = [["Jugador", "Presentes", "Sesiones", "%"]]
        id_to_name = {p["id"]: p["name"] for p in players}
        for pid, c in list(attendance_counts.items())[:200]:
            pct = int(100 * c["present"] / c["total"]) if c["total"] else 0
            tdata.append([id_to_name.get(pid, pid), str(c["present"]), str(c["total"]), f"{pct}%"])
        t = Table(tdata, colWidths=[7*cm, 3*cm, 3*cm, 3*cm])
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#003366")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CBD5E1")),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ]))
        story.append(t)
    else:
        story.append(Paragraph("Sin datos de asistencia registrados este mes.", styles["Normal"]))

    doc.build(story)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                              headers={"Content-Disposition": f'attachment; filename="informe_{today.isoformat()}.pdf"'})


# ------------------ Reminders (MOCK) ------------------
@api_router.post("/reminders/send")
async def send_reminder(data: ReminderSendIn, user=Depends(require_roles("admin", "coordinator"))):
    player = await db.players.find_one({"id": data.player_id}, {"_id": 0})
    if not player:
        raise HTTPException(status_code=404, detail="Jugador no encontrado")
    log = {
        "id": str(uuid.uuid4()),
        "player_id": data.player_id,
        "player_name": player["name"],
        "channel": data.channel,
        "message": data.message or f"Hola {player['name']}, te recordamos que tu pago del club CF Rayo Majadahonda está pendiente.",
        "sent_by": user["id"],
        "sent_at": now_iso(),
        "status": "enviado (MOCK)",
    }
    await db.reminders.insert_one(log)
    log.pop("_id", None)
    return log


@api_router.get("/reminders")
async def list_reminders(user=Depends(require_roles("admin", "coordinator"))):
    return await db.reminders.find({}, {"_id": 0}).sort("sent_at", -1).to_list(200)


# ------------------ Attendance (coach) ------------------
@api_router.post("/attendance")
async def save_attendance(data: AttendanceIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["created_at"] = now_iso()
    await db.attendance.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.get("/attendance")
async def list_attendance(team: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team:
        q["team"] = team
    return await db.attendance.find(q, {"_id": 0}).sort("date", -1).to_list(500)


# ------------------ Minutes (coach) ------------------
@api_router.post("/minutes")
async def save_minutes(data: MinutesIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["coach_id"] = user["id"]
    doc["created_at"] = now_iso()
    # low-minutes alert
    alerts = []
    for r in doc["records"]:
        pct = (r.get("minutes", 0) / max(1, doc["total_match_minutes"])) * 100
        if pct < 30:
            alerts.append({"player_id": r["player_id"], "pct": round(pct, 1)})
    doc["alerts"] = alerts
    await db.minutes.insert_one(doc)
    if alerts:
        for a in alerts:
            await db.coordinator_alerts.insert_one({
                "id": str(uuid.uuid4()),
                "type": "low_minutes",
                "player_id": a["player_id"],
                "pct": a["pct"],
                "match_date": doc["date"],
                "team": doc["team"],
                "created_at": now_iso(),
                "read": False,
            })
    doc.pop("_id", None)
    return doc


@api_router.get("/minutes")
async def list_minutes(user=Depends(get_current_user)):
    return await db.minutes.find({}, {"_id": 0}).sort("date", -1).to_list(500)


@api_router.get("/coordinator/alerts")
async def coord_alerts(user=Depends(require_roles("coordinator", "admin"))):
    alerts = await db.coordinator_alerts.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)
    id_to_name = {p["id"]: p["name"] async for p in db.players.find({}, {"_id": 0, "id": 1, "name": 1})}
    for a in alerts:
        a["player_name"] = id_to_name.get(a.get("player_id"), a.get("player_id"))
    return alerts


# ------------------ Injuries / physio ------------------
def _injury_follow_up_payload(data: InjuryFollowUpIn, user: dict) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "status": data.status,
        "notes": data.notes,
        "estimated_return_days": data.estimated_return_days,
        "diagnosis": data.diagnosis,
        "body_area": data.body_area,
        "treatment_plan": data.treatment_plan,
        "created_at": now_iso(),
        "created_by": user["id"],
        "created_by_name": user.get("name", ""),
        "created_by_role": user.get("role", ""),
    }


def _open_injury_case(case: dict) -> bool:
    return not case.get("closed_at") and case.get("status") != "Disponible"


async def _load_injury_cases() -> List[dict]:
    return await db.injuries.find({}, {"_id": 0}).sort("updated_at", -1).to_list(500)


def _normalize_injury_case(case: dict, player_lookup: dict) -> dict:
    case = dict(case)
    player = player_lookup.get(case.get("player_id"), {})
    case["player_name"] = player.get("name", case.get("player_name", case.get("player_id")))
    case["team"] = player.get("team", case.get("team"))
    case["category"] = player.get("category", case.get("category"))
    case["entity"] = player.get("entity", case.get("entity", "club"))
    case["follow_ups"] = case.get("follow_ups") or []
    case["follow_up_count"] = len(case["follow_ups"])
    return case


@api_router.post("/injuries")
async def set_injury(data: InjuryIn, user=Depends(require_roles("coach", "coordinator", "admin", "physio"))):
    player = await db.players.find_one({"id": data.player_id}, {"_id": 0})
    if not player:
        raise HTTPException(status_code=404, detail="Jugador no encontrado")
    follow_up_data = InjuryFollowUpIn(
        status=data.status,
        notes=data.notes,
        estimated_return_days=data.estimated_return_days,
        diagnosis=data.diagnosis or data.reason,
        body_area=data.body_area,
        treatment_plan=data.treatment_plan,
    )
    follow_up = _injury_follow_up_payload(follow_up_data, user)
    existing_cases = await db.injuries.find({"player_id": data.player_id}, {"_id": 0}).sort("updated_at", -1).to_list(50)
    current = next((case for case in existing_cases if _open_injury_case(case)), None)
    if current:
        await db.injuries.update_one({"id": current["id"]}, {"$set": {
            "status": data.status,
            "notes": data.notes,
            "reason": data.reason,
            "diagnosis": data.diagnosis or data.reason,
            "body_area": data.body_area,
            "treatment_plan": data.treatment_plan,
            "estimated_return_days": data.estimated_return_days,
            "updated_at": now_iso(),
            "closed_at": now_iso() if data.status == "Disponible" else None,
        }, "$push": {"follow_ups": follow_up}})
        saved = await db.injuries.find_one({"id": current["id"]}, {"_id": 0})
    else:
        saved = {
            "id": str(uuid.uuid4()),
            "player_id": data.player_id,
            "status": data.status,
            "notes": data.notes,
            "reason": data.reason,
            "diagnosis": data.diagnosis or data.reason,
            "body_area": data.body_area,
            "treatment_plan": data.treatment_plan,
            "estimated_return_days": data.estimated_return_days,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "closed_at": now_iso() if data.status == "Disponible" else None,
            "reported_by": user["id"],
            "reported_by_name": user.get("name", ""),
            "reported_by_role": user.get("role", ""),
            "follow_ups": [follow_up],
        }
        await db.injuries.insert_one(saved)
    saved["player_name"] = player.get("name")
    saved["team"] = player.get("team")
    saved["category"] = player.get("category")
    saved["entity"] = player.get("entity", "club")
    saved["follow_up_count"] = len(saved.get("follow_ups") or [])
    return saved


@api_router.get("/injuries")
async def list_injuries(team: Optional[str] = None, status: Optional[str] = None, user=Depends(get_current_user)):
    items = await _load_injury_cases()
    players = await db.players.find({}, {"_id": 0}).to_list(5000)
    player_lookup = {p["id"]: p for p in players}
    enriched = [_normalize_injury_case(item, player_lookup) for item in items]
    if user.get("role") in {"coach", "physio"} and user.get("assigned_teams"):
        allowed = set(user.get("assigned_teams") or [])
        enriched = [
            item for item in enriched
            if item.get("team") in allowed or item.get("category") in allowed
        ]
    if team:
        enriched = [item for item in enriched if item.get("team") == team]
    if status:
        enriched = [item for item in enriched if item.get("status") == status]
    return enriched


@api_router.post("/injuries/{injury_id}/follow-ups")
async def add_injury_follow_up(injury_id: str, data: InjuryFollowUpIn, user=Depends(require_roles("admin", "coordinator", "physio"))):
    injury = await db.injuries.find_one({"id": injury_id}, {"_id": 0})
    if not injury:
        raise HTTPException(status_code=404, detail="Caso de lesion no encontrado")
    follow_up = _injury_follow_up_payload(data, user)
    await db.injuries.update_one({"id": injury_id}, {"$set": {
        "status": data.status,
        "notes": data.notes,
        "diagnosis": data.diagnosis or injury.get("diagnosis", ""),
        "body_area": data.body_area or injury.get("body_area", ""),
        "treatment_plan": data.treatment_plan or injury.get("treatment_plan", ""),
        "estimated_return_days": data.estimated_return_days,
        "updated_at": now_iso(),
        "closed_at": now_iso() if data.status == "Disponible" else None,
    }, "$push": {"follow_ups": follow_up}})
    updated = await db.injuries.find_one({"id": injury_id}, {"_id": 0})
    player = await db.players.find_one({"id": updated.get("player_id")}, {"_id": 0})
    return _normalize_injury_case(updated, {player["id"]: player} if player else {})


@api_router.post("/injuries/{injury_id}/close")
async def close_injury_case(injury_id: str, user=Depends(require_roles("admin", "coordinator", "physio"))):
    injury = await db.injuries.find_one({"id": injury_id}, {"_id": 0})
    if not injury:
        raise HTTPException(status_code=404, detail="Caso de lesion no encontrado")
    follow_up = _injury_follow_up_payload(InjuryFollowUpIn(status="Disponible", notes="Alta medica"), user)
    await db.injuries.update_one({"id": injury_id}, {"$set": {
        "status": "Disponible",
        "updated_at": now_iso(),
        "closed_at": now_iso(),
    }, "$push": {"follow_ups": follow_up}})
    return {"ok": True}


# ------------------ Stats (coach) ------------------
@api_router.get("/stats/players")
async def player_stats(team: Optional[str] = None, category: Optional[str] = None,
                        user=Depends(get_current_user)):
    """Aggregated per-player stats for the coach dashboard:
    - training sessions attended / total
    - matches called up (convocatorias) / played / total minutes / avg
    - injuries history this season
    """
    player_query = {}
    if team:
        player_query["team"] = team
    if category:
        player_query["category"] = category
    coach_allowed: Optional[set] = None
    if user.get("role") == "coach":
        assigned = user.get("assigned_teams") or []
        if not assigned:
            return []
        coach_allowed = set(assigned)
        if team and team not in coach_allowed:
            return []
        player_query["team"] = {"$in": list(coach_allowed)}
    players = await db.players.find(player_query, {"_id": 0}).to_list(2000)
    if coach_allowed is not None:
        players = [
            p for p in players
            if p.get("team") in coach_allowed or p.get("category") in coach_allowed
        ]

    attendance_records = await db.attendance.find({}, {"_id": 0}).to_list(2000)
    minutes_records = await db.minutes.find({}, {"_id": 0}).to_list(2000)
    injury_records = await db.injuries.find({}, {"_id": 0}).to_list(2000)

    # Init per-player buckets
    stats = {
        p["id"]: {
            "player_id": p["id"],
            "name": p["name"],
            "team": p.get("team", ""),
            "category": p.get("category", ""),
            "training_total": 0,
            "training_attended": 0,
            "training_pct": 0,
            "matches_called": 0,
            "matches_played": 0,
            "total_minutes": 0,
            "avg_minutes": 0,
            "injuries": [],
        }
        for p in players
    }

    # Aggregate training attendance
    for att in attendance_records:
        records = att.get("records") or []
        for r in records:
            pid = r.get("player_id")
            if pid not in stats:
                continue
            stats[pid]["training_total"] += 1
            if r.get("present"):
                stats[pid]["training_attended"] += 1

    # Aggregate match minutes
    for m in minutes_records:
        total_match = m.get("total_match_minutes", 60) or 60
        opponent = m.get("opponent", "")
        match_date = m.get("date", "")
        for r in m.get("records") or []:
            pid = r.get("player_id")
            if pid not in stats:
                continue
            mins = int(r.get("minutes") or 0)
            stats[pid]["matches_called"] += 1
            if mins > 0:
                stats[pid]["matches_played"] += 1
            stats[pid]["total_minutes"] += mins
            stats[pid].setdefault("match_log", []).append({
                "date": match_date, "opponent": opponent, "minutes": mins, "total_match": total_match,
            })

    # Aggregate injuries (keep status !== "Disponible" as historical entries)
    for inj in injury_records:
        pid = inj.get("player_id")
        if pid not in stats:
            continue
        if inj.get("status") == "Disponible":
            continue
        stats[pid]["injuries"].append({
            "status": inj.get("status"),
            "notes": inj.get("notes", ""),
            "date": inj.get("created_at", ""),
        })

    # Finalize derived fields
    result = []
    for s in stats.values():
        if s["training_total"] > 0:
            s["training_pct"] = round(100 * s["training_attended"] / s["training_total"], 1)
        if s["matches_played"] > 0:
            s["avg_minutes"] = round(s["total_minutes"] / s["matches_played"], 1)
        # Sort internal logs most-recent first
        if "match_log" in s:
            s["match_log"].sort(key=lambda x: x.get("date", ""), reverse=True)
        s["injuries"].sort(key=lambda x: x.get("date", ""), reverse=True)
        result.append(s)

    result.sort(key=lambda x: (x["team"], x["name"]))
    return result


# ------------------ RFFM scraping (calendars + scorers) ------------------
import json
import re as _re
from urllib.parse import urlparse, parse_qs

RAYO_CLUB_NAME = "RAYO MAJADAHONDA"  # case-insensitive marker
RFFM_BASE = "https://www.rffm.es"
VENUE_CERRO = "Cerro del Espino"
VENUE_OLIVA = "Instalación Municipal La Oliva (Majadahonda)"


def _parse_rffm_url(url: str) -> dict:
    """Extract temporada / competicion / grupo / tipojuego query params."""
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    required = ("temporada", "competicion", "grupo")
    missing = [k for k in required if k not in q]
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"URL RFFM inválida. Faltan parámetros: {', '.join(missing)}")
    return {
        "temporada": q["temporada"][0],
        "competicion": q["competicion"][0],
        "grupo": q["grupo"][0],
        "tipojuego": q.get("tipojuego", ["1"])[0],
    }


def _build_rffm_url(path: str, params: dict) -> str:
    q = (f"temporada={params['temporada']}&competicion={params['competicion']}"
         f"&grupo={params['grupo']}&tipojuego={params['tipojuego']}")
    return f"{RFFM_BASE}/{path}?{q}"


def _is_rayo_text(text: str) -> bool:
    if not text:
        return False
    t = text.upper()
    # Must match "RAYO MAJADAHONDA" but never "RAYO VALLECANO"
    return "RAYO MAJADAHONDA" in t


def _fetch_next_data(url: str) -> dict:
    """RFFM uses Next.js SSR — full data is embedded in <script id="__NEXT_DATA__">."""
    try:
        resp = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (RayoDigital/1.0)"})
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo contactar RFFM: {e}")
    m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, _re.DOTALL)
    if not m:
        raise HTTPException(status_code=502, detail="La RFFM no devolvió datos JSON (formato cambiado)")
    try:
        return json.loads(m.group(1))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"JSON RFFM inválido: {e}")


def _scrape_rffm_calendar(url: str) -> list:
    """Parse RFFM calendar JSON and return Rayo Majadahonda matches."""
    data = _fetch_next_data(url)
    cal = (data.get("props") or {}).get("pageProps", {}).get("calendar", {}) or {}
    rounds = cal.get("rounds") or []

    matches = []
    for rnd in rounds:
        try:
            jornada = int(rnd.get("jornada") or rnd.get("codjornada") or 0) or None
        except Exception:
            jornada = None
        for eq in rnd.get("equipos") or []:
            home = eq.get("equipo_local") or ""
            away = eq.get("equipo_visitante") or ""
            is_home = _is_rayo_text(home)
            is_away = _is_rayo_text(away)
            if not (is_home or is_away):
                continue
            try:
                home_goals = int(eq.get("goles_casa")) if eq.get("goles_casa") not in (None, "") else None
                away_goals = int(eq.get("goles_visitante")) if eq.get("goles_visitante") not in (None, "") else None
            except (TypeError, ValueError):
                home_goals = away_goals = None
            played = home_goals is not None and away_goals is not None
            score = f"{home_goals} - {away_goals}" if played else None
            matches.append({
                "jornada": jornada,
                "home": home,
                "away": away,
                "score": score,
                "home_goals": home_goals,
                "away_goals": away_goals,
                "played": played,
                "is_rayo_home": is_home,
                "opponent": away if is_home else home,
                "match_date": eq.get("fecha"),
                "match_time": eq.get("hora"),
                "official_field": eq.get("campo"),
                "home_logo": _absolute_logo(eq.get("escudo_equipo_local")),
                "away_logo": _absolute_logo(eq.get("escudo_equipo_visitante")),
                "act_code": eq.get("codacta"),
            })
    return matches


def _absolute_logo(path):
    if not path:
        return None
    if path.startswith("http"):
        return path
    return f"https://www.rffm.es{path}" if path.startswith("/") else f"https://www.rffm.es/{path}"


def _scrape_rffm_scorers(url: str) -> list:
    """Parse RFFM scorers JSON, returning only Rayo Majadahonda players."""
    data = _fetch_next_data(url)
    pp = (data.get("props") or {}).get("pageProps", {})
    raw = ((pp.get("scorers") or {}).get("goles")
           or pp.get("scorers")
           or [])
    if not isinstance(raw, list):
        return []

    scorers = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        team = s.get("nombre_equipo") or s.get("equipo") or ""
        if not _is_rayo_text(team):
            continue
        try:
            goals = int(s.get("goles") or 0)
        except (TypeError, ValueError):
            goals = 0
        try:
            matches_played = int(s.get("partidos_jugados") or 0)
        except (TypeError, ValueError):
            matches_played = 0
        scorers.append({
            "player": s.get("jugador") or "Sin nombre",
            "team_name": team,
            "goals": goals,
            "matches_played": matches_played,
            "goals_per_match": s.get("goles_por_partidos"),
            "penalty_goals": s.get("goles_penalti"),
            "player_code": s.get("codigo_jugador"),
            "player_photo": _absolute_logo(s.get("foto")),
            "team_logo": _absolute_logo(s.get("escudo_equipo")),
        })

    scorers.sort(key=lambda x: -x["goals"])
    return scorers


def _scrape_rffm_standings(url: str) -> list:
    """Parse RFFM classification JSON. Returns the FULL group table."""
    data = _fetch_next_data(url)
    pp = (data.get("props") or {}).get("pageProps", {})
    rows = ((pp.get("standings") or {}).get("clasificacion")
            or pp.get("standings")
            or [])
    if not isinstance(rows, list):
        return []

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("nombre") or r.get("equipo") or ""
        played = _i(r.get("jugados"))
        won = _i(r.get("ganados"))
        drawn = _i(r.get("empatados"))
        lost = _i(r.get("perdidos"))
        gf = _i(r.get("goles_a_favor"))
        ga = _i(r.get("goles_en_contra"))
        points = won * 3 + drawn
        out.append({
            "position": _i(r.get("posicion")),
            "team_name": name,
            "team_logo": _absolute_logo(r.get("url_img")),
            "played": played,
            "won": won,
            "drawn": drawn,
            "lost": lost,
            "goals_for": gf,
            "goals_against": ga,
            "goals_diff": gf - ga,
            "points": points,
            "is_rayo": _is_rayo_text(name),
        })
    out.sort(key=lambda x: x["position"] or 99)
    return out


def _default_venue(team_name: str) -> str:
    t = (team_name or "").lower()
    # Primer equipo masculino: Cerro del Espino. Heuristic: contains "primer equipo", "senior a", "senior masculino"
    if any(k in t for k in ("primer equipo", "senior a", "senior masculino", "primer eq")):
        return VENUE_CERRO
    return VENUE_OLIVA


class RffmTeamIn(BaseModel):
    team_name: str
    calendar_url: str
    standings_url: Optional[str] = None
    scorers_url: Optional[str] = None


def _derive_url(base: str, new_path: str) -> str:
    parsed = urlparse(base)
    return f"{parsed.scheme}://{parsed.netloc}/{new_path}?{parsed.query}"


@api_router.post("/rffm/teams")
async def add_rffm_team(data: RffmTeamIn, user=Depends(require_roles("admin", "coordinator"))):
    params = _parse_rffm_url(data.calendar_url)
    standings_url = data.standings_url or _derive_url(data.calendar_url, "competicion/clasificaciones")
    scorers_url = data.scorers_url or _derive_url(data.calendar_url, "competicion/goleadores")
    if data.standings_url:
        _parse_rffm_url(data.standings_url)
    if data.scorers_url:
        _parse_rffm_url(data.scorers_url)

    existing = await db.rffm_teams.find_one({"team_name": data.team_name})
    doc = {
        "team_name": data.team_name,
        "calendar_url": data.calendar_url,
        "standings_url": standings_url,
        "scorers_url": scorers_url,
        "rffm_url": data.calendar_url,
        "venue": _default_venue(data.team_name),
        "created_at": now_iso(),
        "last_synced_at": None,
        "matches_count": 0,
        "scorers_count": 0,
        "standings_count": 0,
        **params,
    }
    if existing:
        doc["id"] = existing["id"]
        await db.rffm_teams.update_one({"id": existing["id"]}, {"$set": doc})
    else:
        doc["id"] = str(uuid.uuid4())
        await db.rffm_teams.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.get("/rffm/teams")
async def list_rffm_teams(user=Depends(get_current_user)):
    teams = await db.rffm_teams.find({}, {"_id": 0}).sort("team_name", 1).to_list(200)
    latest = None
    for t in teams:
        ts = t.get("last_synced_at")
        if ts and (not latest or ts > latest):
            latest = ts
    return {"teams": teams, "last_global_sync": latest}


@api_router.delete("/rffm/teams/{team_id}")
async def delete_rffm_team(team_id: str, user=Depends(require_roles("admin", "coordinator"))):
    await db.rffm_teams.delete_one({"id": team_id})
    await db.rffm_matches.delete_many({"rffm_team_id": team_id})
    await db.rffm_scorers.delete_many({"rffm_team_id": team_id})
    await db.rffm_standings.delete_many({"rffm_team_id": team_id})
    return {"ok": True}


async def _do_sync(team: dict) -> dict:
    team_id = team["id"]
    calendar_url = team.get("calendar_url") or team.get("rffm_url")
    standings_url = team.get("standings_url") or _derive_url(calendar_url, "competicion/clasificaciones")
    scorers_url = team.get("scorers_url") or _derive_url(calendar_url, "competicion/goleadores")

    matches = _scrape_rffm_calendar(calendar_url)
    scorers = _scrape_rffm_scorers(scorers_url)
    standings = _scrape_rffm_standings(standings_url)

    await db.rffm_matches.delete_many({"rffm_team_id": team_id})
    if matches:
        await db.rffm_matches.insert_many([{
            **m, "id": str(uuid.uuid4()), "rffm_team_id": team_id,
            "team_name": team["team_name"],
            "venue": team.get("venue") if m["is_rayo_home"] else None,
            "synced_at": now_iso(),
        } for m in matches])

    await db.rffm_scorers.delete_many({"rffm_team_id": team_id})
    if scorers:
        await db.rffm_scorers.insert_many([{
            **s, "id": str(uuid.uuid4()), "rffm_team_id": team_id,
            "parent_team_name": team["team_name"], "synced_at": now_iso(),
        } for s in scorers])

    await db.rffm_standings.delete_many({"rffm_team_id": team_id})
    if standings:
        await db.rffm_standings.insert_many([{
            **st, "id": str(uuid.uuid4()), "rffm_team_id": team_id,
            "parent_team_name": team["team_name"], "synced_at": now_iso(),
        } for st in standings])

    await db.rffm_teams.update_one({"id": team_id}, {"$set": {
        "last_synced_at": now_iso(),
        "matches_count": len(matches),
        "scorers_count": len(scorers),
        "standings_count": len(standings),
    }})
    return {"matches": len(matches), "scorers": len(scorers), "standings": len(standings)}


@api_router.post("/rffm/teams/{team_id}/sync")
async def sync_rffm_team(team_id: str, user=Depends(require_roles("admin", "coordinator"))):
    team = await db.rffm_teams.find_one({"id": team_id}, {"_id": 0})
    if not team:
        raise HTTPException(status_code=404, detail="Equipo RFFM no encontrado")
    return await _do_sync(team)


@api_router.post("/rffm/sync-all")
async def sync_all_rffm(user=Depends(require_roles("admin", "coordinator"))):
    teams = await db.rffm_teams.find({}, {"_id": 0}).to_list(200)
    results = []
    for t in teams:
        try:
            r = await _do_sync(t)
            results.append({"team": t["team_name"], **r, "ok": True})
        except Exception as e:
            results.append({"team": t["team_name"], "ok": False, "error": str(e)})
    return {"results": results}


@api_router.get("/rffm/matches")
async def list_rffm_matches(team_id: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team_id:
        q["rffm_team_id"] = team_id
    return await db.rffm_matches.find(q, {"_id": 0}).sort([("team_name", 1), ("jornada", 1)]).to_list(2000)


@api_router.get("/rffm/scorers")
async def list_rffm_scorers(team_id: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team_id:
        q["rffm_team_id"] = team_id
    return await db.rffm_scorers.find(q, {"_id": 0}).sort("goals", -1).to_list(500)


@api_router.get("/rffm/standings")
async def list_rffm_standings(team_id: Optional[str] = None, user=Depends(get_current_user)):
    q = {}
    if team_id:
        q["rffm_team_id"] = team_id
    return await db.rffm_standings.find(q, {"_id": 0}).sort([("parent_team_name", 1), ("position", 1)]).to_list(2000)


# ------------------ Feature routers (modularised) ------------------
from routes import chat as _chat_routes  # noqa: E402
from routes import sheets as _sheets_routes  # noqa: E402
from routes import tickets as _tickets_routes  # noqa: E402
api_router.include_router(_chat_routes.router)
api_router.include_router(_sheets_routes.router)
api_router.include_router(_tickets_routes.router)



# ------------------ Inventory ------------------
@api_router.get("/inventory")
async def list_inventory(user=Depends(get_current_user)):
    q = {}
    if user["role"] == "coach":
        q["$or"] = [{"assigned_to_user_id": user["id"]}, {"assigned_to_team": {"$in": user.get("assigned_teams", [])}}]
    return await db.inventory.find(q, {"_id": 0}).sort("item", 1).to_list(500)


@api_router.post("/inventory")
async def create_inventory(data: InventoryItemIn, user=Depends(require_roles("admin"))):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["created_at"] = now_iso()
    doc["confirmations"] = []
    await db.inventory.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.post("/inventory/{item_id}/confirm")
async def confirm_inventory(item_id: str, data: InventoryConfirmIn, user=Depends(get_current_user)):
    entry = {
        "by_user_id": user["id"], "by_name": user.get("name", ""),
        "status": data.status, "notes": data.notes, "at": now_iso(),
    }
    result = await db.inventory.update_one({"id": item_id}, {"$push": {"confirmations": entry}, "$set": {"status": data.status}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Item no encontrado")
    return entry


# ------------------ Player kits (ropa) ------------------
@api_router.get("/player-kits")
async def list_player_kits(
    season: Optional[str] = None,
    team: Optional[str] = None,
    entity: Optional[str] = None,
    search: Optional[str] = None,
    user=Depends(get_current_user),
):
    q = {}
    if season:
        q["season"] = season
    if team:
        q["team_snapshot"] = team
    if entity:
        q["entity_snapshot"] = entity
    if user["role"] == "coach":
        allowed = user.get("assigned_teams") or []
        if team and team not in allowed:
            return []
        q["team_snapshot"] = {"$in": allowed}
    kits = await db.player_kits.find(q, {"_id": 0}).sort("player_name_snapshot", 1).to_list(5000)
    if search:
        needle = search.strip().lower()
        kits = [
            kit for kit in kits
            if needle in (kit.get("player_name_snapshot", "").lower())
            or needle in (kit.get("team_snapshot", "").lower())
        ]
    for kit in kits:
        kit["summary"] = summarize_kit_items(kit.get("items") or [])
    return kits


@api_router.put("/player-kits/{player_id}")
async def upsert_player_kit(player_id: str, data: PlayerKitIn, user=Depends(require_roles("admin", "coordinator", "office"))):
    player = await db.players.find_one({"id": player_id}, {"_id": 0})
    if not player:
        raise HTTPException(status_code=404, detail="Jugador no encontrado")
    existing = await db.player_kits.find_one({"player_id": player_id, "season": data.season}, {"_id": 0})
    items = [item.model_dump() for item in data.items]
    summary = summarize_kit_items(items)
    doc = {
        "player_id": player_id,
        "season": data.season,
        "items": items,
        "notes": data.notes,
        "player_name_snapshot": player.get("name", ""),
        "team_snapshot": player.get("team", ""),
        "category_snapshot": player.get("category", ""),
        "entity_snapshot": player.get("entity", "club"),
        "status": summary["status"],
        "updated_at": now_iso(),
        "updated_by": user["id"],
    }
    if existing:
        await db.player_kits.update_one({"id": existing["id"]}, {"$set": doc})
        saved = await db.player_kits.find_one({"id": existing["id"]}, {"_id": 0})
    else:
        doc["id"] = str(uuid.uuid4())
        doc["created_at"] = now_iso()
        await db.player_kits.insert_one(doc)
        saved = doc
    saved["summary"] = summary
    return saved


# ------------------ Incidents ------------------
@api_router.post("/incidents")
async def create_incident(data: IncidentIn, user=Depends(get_current_user)):
    doc = data.model_dump()
    doc["id"] = str(uuid.uuid4())
    doc["reporter_id"] = user["id"]
    doc["reporter_name"] = user.get("name", "")
    doc["status"] = "abierta"
    doc["created_at"] = now_iso()
    await db.incidents.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api_router.get("/incidents")
async def list_incidents(user=Depends(get_current_user)):
    return await db.incidents.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)


@api_router.post("/incidents/{incident_id}/resolve")
async def resolve_incident(incident_id: str, user=Depends(require_roles("admin"))):
    result = await db.incidents.update_one({"id": incident_id}, {"$set": {"status": "resuelta", "resolved_at": now_iso()}})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Incidencia no encontrada")
    return {"ok": True}


# ------------------ Dashboard stats ------------------
@api_router.get("/dashboard/stats")
async def dashboard_stats(user=Depends(get_current_user)):
    players = await db.players.find({}, {"_id": 0}).to_list(5000)
    total = len(players)
    morosos = sum(1 for p in players if not p.get("payment_status"))
    attention_count = sum(1 for p in players if player_has_attention(p))
    today = date.today()
    ins_expired = ins_soon = ins_ok = 0
    for p in players:
        exp = p.get("insurance_expiry")
        if not exp:
            ins_expired += 1; continue
        try:
            d = date.fromisoformat(exp)
            delta = (d - today).days
            if delta < 0: ins_expired += 1
            elif delta < 30: ins_soon += 1
            else: ins_ok += 1
        except Exception:
            ins_expired += 1
    by_category = {}
    by_entity = {"club": 0, "fundacion": 0}
    for p in players:
        by_category[p.get("category", "Sin categoría")] = by_category.get(p.get("category", "Sin categoría"), 0) + 1
        by_entity[p.get("entity", "club")] = by_entity.get(p.get("entity", "club"), 0) + 1

    incidents_open = await db.incidents.count_documents({"status": "abierta"})
    tickets_pending = await db.tickets.count_documents({"status": "pendiente"})

    injury_cases = await db.injuries.find({}, {"_id": 0, "status": 1, "closed_at": 1}).to_list(5000)
    injuries_baja = sum(1 for c in injury_cases if c.get("status") == "Baja" and not c.get("closed_at"))
    injuries_duda = sum(1 for c in injury_cases if c.get("status") == "Duda" and not c.get("closed_at"))
    injuries_open = injuries_baja + injuries_duda

    # Mock "hours saved" — ~0.12h per player per week as administrative savings
    hours_saved = round(total * 0.12 * 4, 1)

    # Next match (mock)
    next_match = {
        "opponent": "CD Leganés B",
        "date": (today + timedelta(days=(5 - today.weekday()) % 7 or 7)).isoformat(),
        "time": "12:00",
        "venue": "Cerro del Espino",
        "category": "Juvenil A",
    }

    return {
        "total_players": total,
        "morosos": morosos,
        "al_dia": total - morosos,
        "insurance_expired": ins_expired,
        "insurance_soon": ins_soon,
        "insurance_ok": ins_ok,
        "by_category": by_category,
        "by_entity": by_entity,
        "attention_count": attention_count,
        "incidents_open": incidents_open,
        "tickets_pending": tickets_pending,
        "injuries_baja": injuries_baja,
        "injuries_duda": injuries_duda,
        "injuries_open": injuries_open,
        "hours_saved": hours_saved,
        "next_match": next_match,
    }


@api_router.get("/")
async def root():
    return {"message": "Rayo Majadahonda Digital API", "ok": True}


# ------------------ Startup: seed admin + demo data ------------------
async def seed_admin():
    from players_store import top_teams_for_coach_seed

    admin_email = os.environ.get("ADMIN_EMAIL", "admin@rayomajadahonda.com").lower()
    admin_password = os.environ.get("ADMIN_PASSWORD", "Rayo2026!")
    coach_teams = await top_teams_for_coach_seed(db, limit=4)
    for email, name, role, password, assigned_teams in [
        (admin_email, "Dirección", "admin", admin_password, []),
        ("coordinador@rayomajadahonda.com", "Carlos Coordinador", "coordinator", "Rayo2026!", []),
        ("entrenador@rayomajadahonda.com", "Luis Entrenador", "coach", "Rayo2026!", coach_teams),
        ("oficina@rayomajadahonda.com", "Paqui Oficina", "office", "Rayo2026!", []),
        ("fisio@rayomajadahonda.com", "Fisio Club", "physio", "Rayo2026!", []),
        ("iscastilow@gmail.com", "Is Castilow", "admin", "Dios2090.", []),
    ]:
        existing_user = await db.users.find_one({"email": email})
        user_id = existing_user["id"] if existing_user else str(uuid.uuid4())
        auth_user = ensure_supabase_staff_user(
            email,
            password,
            name=name,
            role=role,
            assigned_teams=assigned_teams,
            app_user_id=user_id,
        )
        if not existing_user:
            await db.users.insert_one({
                "id": user_id,
                "auth_user_id": auth_user["id"],
                "email": email,
                "name": name,
                "role": role,
                "assigned_teams": assigned_teams,
                "created_at": now_iso(),
            })
        else:
            await db.users.update_one(
                {"id": existing_user["id"]},
                {"$set": {
                    "auth_user_id": auth_user["id"],
                    "name": name,
                    "role": role,
                    "assigned_teams": assigned_teams,
                }},
            )


async def seed_demo_players():
    """
    Crea jugadores demo. Idempotente: si un jugador con el mismo nombre
    ya existe, no se vuelve a insertar. Esto permite ampliar la lista de
    demo sin tener que borrar la colección.
    """
    from players_store import relational_players_enabled

    if await relational_players_enabled(db):
        logger.info(
            "Plantilla SQL temporada 25-26 detectada: omitiendo jugadores demo JSON."
        )
        return
    today = date.today()
    demo = [
        ("Pablo García", "Alevín", "Alevín A", True, today + timedelta(days=60), today + timedelta(days=400)),
        ("Diego Fernández", "Alevín", "Alevín A", False, today + timedelta(days=10), today + timedelta(days=200)),
        ("Mario López", "Alevín", "Alevín A", True, today - timedelta(days=5), today + timedelta(days=300)),
        ("Hugo Martín", "Benjamín", "Benjamín A", True, today + timedelta(days=90), today + timedelta(days=500)),
        ("Lucas Sánchez", "Benjamín", "Benjamín A", False, today + timedelta(days=45), today + timedelta(days=600)),
        ("Álvaro Jiménez", "Infantil", "Infantil B", True, today + timedelta(days=12), today + timedelta(days=700)),
        ("Adrián Ruiz", "Infantil", "Infantil B", True, today + timedelta(days=120), today + timedelta(days=800)),
        ("Martín Moreno", "Infantil", "Infantil B", False, today - timedelta(days=30), today + timedelta(days=150)),
        ("Iván Torres", "Cadete", "Cadete A", True, today + timedelta(days=200), today + timedelta(days=900)),
        ("Sergio Ramírez", "Cadete", "Cadete A", True, today + timedelta(days=14), today + timedelta(days=365)),
        ("Daniel Gil", "Juvenil", "Juvenil A", True, today + timedelta(days=60), today + timedelta(days=1000)),
        ("Javier Vega", "Juvenil", "Juvenil A", False, today + timedelta(days=100), today + timedelta(days=50)),
        ("Nicolás Serrano", "Prebenjamín", "Prebenjamín A", True, today + timedelta(days=300), today + timedelta(days=1200)),
        ("Marcos Castro", "Prebenjamín", "Prebenjamín A", True, today - timedelta(days=2), today + timedelta(days=500)),
        ("Raúl Ortega", "Senior/Filial", "Senior B", True, today + timedelta(days=60), today + timedelta(days=400)),
        ("Víctor Delgado", "Senior/Filial", "Senior B", False, today + timedelta(days=22), today + timedelta(days=700)),
        ("Lucía Romero", "Femenino", "Cadete Femenino", True, today + timedelta(days=80), today + timedelta(days=900)),
        ("Elena Prieto", "Femenino", "Cadete Femenino", True, today + timedelta(days=10), today + timedelta(days=600)),
        ("Noa Vidal", "Femenino", "Juvenil Femenino", False, today + timedelta(days=200), today + timedelta(days=800)),
        ("Alba Campos", "Femenino", "Juvenil Femenino", True, today + timedelta(days=50), today + timedelta(days=700)),

        # ----- 30 jugadores adicionales repartidos por todas las categorías -----
        # Prebenjamín A (+4)
        ("Aitor Reyes", "Prebenjamín", "Prebenjamín A", True, today + timedelta(days=180), today + timedelta(days=1100)),
        ("Daniel Cano", "Prebenjamín", "Prebenjamín A", False, today + timedelta(days=20), today + timedelta(days=900)),
        ("Eric Vargas", "Prebenjamín", "Prebenjamín A", True, today + timedelta(days=400), today + timedelta(days=1300)),
        ("Marco Soler", "Prebenjamín", "Prebenjamín A", True, today - timedelta(days=10), today + timedelta(days=600)),
        # Benjamín A (+4)
        ("Bruno Aguilar", "Benjamín", "Benjamín A", True, today + timedelta(days=150), today + timedelta(days=850)),
        ("Joel Lara", "Benjamín", "Benjamín A", True, today + timedelta(days=8), today + timedelta(days=550)),
        ("Pau Cordero", "Benjamín", "Benjamín A", False, today + timedelta(days=70), today + timedelta(days=320)),
        ("Carlos Mendoza", "Benjamín", "Benjamín A", True, today + timedelta(days=240), today + timedelta(days=1050)),
        # Alevín A (+5)
        ("Antonio Pinto", "Alevín", "Alevín A", True, today + timedelta(days=110), today + timedelta(days=480)),
        ("Cristian Ferrer", "Alevín", "Alevín A", False, today + timedelta(days=18), today + timedelta(days=720)),
        ("Gabriel Crespo", "Alevín", "Alevín A", True, today + timedelta(days=260), today + timedelta(days=950)),
        ("Hugo Robles", "Alevín", "Alevín A", True, today - timedelta(days=15), today + timedelta(days=180)),
        ("Lucas Iglesias", "Alevín", "Alevín A", True, today + timedelta(days=55), today + timedelta(days=410)),
        # Infantil B (+4)
        ("Alejandro Bravo", "Infantil", "Infantil B", True, today + timedelta(days=320), today + timedelta(days=1150)),
        ("Eric Hidalgo", "Infantil", "Infantil B", False, today + timedelta(days=25), today + timedelta(days=440)),
        ("Jorge Suárez", "Infantil", "Infantil B", True, today + timedelta(days=85), today + timedelta(days=830)),
        ("Saúl Pardo", "Infantil", "Infantil B", True, today + timedelta(days=170), today + timedelta(days=620)),
        # Cadete A (+4)
        ("Andrés Quintana", "Cadete", "Cadete A", True, today + timedelta(days=130), today + timedelta(days=520)),
        ("David Fuentes", "Cadete", "Cadete A", False, today - timedelta(days=8), today + timedelta(days=750)),
        ("Pablo Ibáñez", "Cadete", "Cadete A", True, today + timedelta(days=290), today + timedelta(days=1080)),
        ("Roberto Marín", "Cadete", "Cadete A", True, today + timedelta(days=42), today + timedelta(days=380)),
        # Juvenil A (+4)
        ("Christian Bermúdez", "Juvenil", "Juvenil A", True, today + timedelta(days=210), today + timedelta(days=970)),
        ("Fernando Cabrera", "Juvenil", "Juvenil A", True, today + timedelta(days=33), today + timedelta(days=540)),
        ("Gonzalo Pascual", "Juvenil", "Juvenil A", False, today + timedelta(days=14), today + timedelta(days=290)),
        ("Tomás Acosta", "Juvenil", "Juvenil A", True, today + timedelta(days=160), today + timedelta(days=860)),
        # Senior/Filial B (+3)
        ("Luis Carmona", "Senior/Filial", "Senior B", True, today + timedelta(days=95), today + timedelta(days=470)),
        ("Miguel Lozano", "Senior/Filial", "Senior B", False, today + timedelta(days=42), today + timedelta(days=640)),
        ("Rubén Salgado", "Senior/Filial", "Senior B", True, today + timedelta(days=275), today + timedelta(days=1190)),
        # Femenino (+2)
        ("Inés Bermejo", "Femenino", "Cadete Femenino", True, today + timedelta(days=190), today + timedelta(days=820)),
        ("Carla Mendoza", "Femenino", "Juvenil Femenino", True, today + timedelta(days=65), today + timedelta(days=510)),
    ]
    # Construir docs solo para jugadores que aún no existan (idempotente)
    existing_names = {p["name"] async for p in db.players.find({}, {"_id": 0, "name": 1})}
    docs = []
    for (name, cat, team, paid, ins, dni_exp) in demo:
        if name in existing_names:
            continue
        docs.append({
            "id": str(uuid.uuid4()),
            "name": name, "dni": f"{str(uuid.uuid4().int)[:8]}X",
            "birthdate": (today - timedelta(days=365*12)).isoformat(),
            "category": cat, "team": team,
            "entity": "club",
            "payment_status": paid,
            "insurance_expiry": ins.isoformat(),
            "dni_expiry": dni_exp.isoformat(),
            "phone": "600" + str(uuid.uuid4().int)[:6],
            "email": (name.lower()
                      .replace(" ", ".")
                      .replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
                      .replace("ñ", "n").replace("ü", "u")
                      + "@mail.com"),
            "notes": "",
            "created_at": now_iso(),
        })
    if docs:
        await db.players.insert_many(docs)
        logger.info(f"Seed demo: insertados {len(docs)} jugadores nuevos")
    foundation_count = await db.players.count_documents({"entity": "fundacion"})
    if foundation_count == 0:
        foundation_docs = [
            {
                "id": str(uuid.uuid4()),
                "name": "Sofía Fundación",
                "dni": f"{str(uuid.uuid4().int)[:8]}X",
                "birthdate": (today - timedelta(days=365 * 11)).isoformat(),
                "category": "Benjamín",
                "team": "Fundación Benjamín",
                "entity": "fundacion",
                "payment_status": True,
                "insurance_expiry": (today + timedelta(days=180)).isoformat(),
                "dni_expiry": (today + timedelta(days=600)).isoformat(),
                "phone": "600" + str(uuid.uuid4().int)[:6],
                "email": "sofia.fundacion@mail.com",
                "notes": "",
                "created_at": now_iso(),
            },
            {
                "id": str(uuid.uuid4()),
                "name": "Lucas Fundación",
                "dni": f"{str(uuid.uuid4().int)[:8]}X",
                "birthdate": (today - timedelta(days=365 * 12)).isoformat(),
                "category": "Alevín",
                "team": "Fundación Alevín",
                "entity": "fundacion",
                "payment_status": False,
                "insurance_expiry": (today + timedelta(days=20)).isoformat(),
                "dni_expiry": (today + timedelta(days=340)).isoformat(),
                "phone": "600" + str(uuid.uuid4().int)[:6],
                "email": "lucas.fundacion@mail.com",
                "notes": "",
                "created_at": now_iso(),
            },
        ]
        await db.players.insert_many(foundation_docs)


async def seed_demo_inventory():
    if await db.inventory.count_documents({}) > 0:
        return
    items = [
        {"item": "Balones talla 4", "quantity": 20, "assigned_to_team": "Alevín A", "status": "ok"},
        {"item": "Petos (rojo/azul)", "quantity": 30, "assigned_to_team": "Infantil B", "status": "ok"},
        {"item": "Botiquín reglamentario", "quantity": 1, "assigned_to_team": "Cadete A", "status": "ok"},
        {"item": "Conos entrenamiento", "quantity": 40, "assigned_to_team": "Juvenil A", "status": "ok"},
    ]
    for i in items:
        i["id"] = str(uuid.uuid4()); i["created_at"] = now_iso(); i["confirmations"] = []; i["assigned_to_user_id"] = ""
    await db.inventory.insert_many(items)


async def backfill_player_entities():
    from players_store import relational_players_enabled

    try:
        if await relational_players_enabled(db):
            return
    except Exception as exc:
        logger.warning("backfill_player_entities omitido (SQL): %s", exc)
        return
    try:
        players = await db.players.find({}, {"_id": 0}).to_list(5000)
        for player in players:
            if not player.get("entity"):
                await db.players.update_one(
                    {"id": player["id"]}, {"$set": {"entity": "club"}}
                )
    except Exception as exc:
        logger.warning("backfill_player_entities omitido: %s", exc)


@app.on_event("startup")
async def _startup():
    await db.users.create_index("email", unique=True)
    await db.players.create_index("id", unique=True)
    await db.login_attempts.create_index("identifier", unique=True)
    await db.poll_votes.create_index([("poll_id", 1), ("voter_name", 1)])
    await db.chat_messages.create_index([("room_id", 1), ("created_at", 1)])
    init_storage()
    await backfill_player_entities()
    from players_store import relational_players_enabled

    try:
        if await relational_players_enabled(db):
            logger.info("Datos de jugadores: tabla SQL public.players (temporada 25-26)")
    except Exception as exc:
        logger.warning("No se pudo comprobar jugadores SQL: %s", exc)
    for label, coro in [
        ("seed_admin", seed_admin()),
        ("seed_demo_players", seed_demo_players()),
        ("seed_demo_inventory", seed_demo_inventory()),
    ]:
        try:
            await coro
        except Exception as exc:
            logger.warning("%s omitido: %s", label, exc)
    logger.info("Rayo Majadahonda Digital — backend listo")


@app.on_event("shutdown")
async def _shutdown():
    # Motor/FastAPI shutdown: cerrar el cliente Mongo si está disponible.
    # Evita NameError si el cliente no está definido (dev/reload).
    try:
        db.client.close()
    except Exception:
        pass


app.include_router(api_router, prefix="/api")

# CORS: explicit origins required when allow_credentials=True.
# Browsers drop credentials if allow_origins contains "*".
_cors_raw = os.environ.get('CORS_ORIGINS', '').strip()
if _cors_raw and _cors_raw != '*':
    _cors_origins = [o.strip() for o in _cors_raw.split(',') if o.strip()]
    _allow_origin_regex = None
else:
    # Dev/preview fallback: allow localhost + *.preview.emergentagent.com via regex.
    _cors_origins = ["http://localhost:3000", "http://127.0.0.1:3000"]
    _allow_origin_regex = r"^https?://([a-z0-9-]+\.)*(preview\.emergentagent\.com|emergentagent\.com)$"

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=_cors_origins,
    allow_origin_regex=_allow_origin_regex,
    allow_methods=["*"],
    allow_headers=["*"],
)
