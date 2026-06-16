"""Chat endpoints (public parent chat + staff)."""
from datetime import datetime, timezone, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from deps import (
    db, now_iso, _slug, _secrets, uuid,
    get_current_user, require_roles, _parent_or_staff, rate_limit_ip,
    create_parent_chat_session,
)

router = APIRouter()


# ------------------ Models ------------------
class ChatRoomIn(BaseModel):
    team: str


class ChatMessageIn(BaseModel):
    text: str


class ChatJoinIn(BaseModel):
    child_name: str


class PollIn(BaseModel):
    question: str
    options: List[str]
    kind: str = "attendance"  # attendance | tournament | generic


class PollVoteIn(BaseModel):
    option_index: int
    child_name: Optional[str] = None  # deprecated: identity.child is preferred


class ChatSimulateIn(BaseModel):
    reset: bool = False


# ------------------ Rooms ------------------
@router.post("/chat/rooms")
async def create_chat_room(data: ChatRoomIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    existing = await db.team_rooms.find_one({"team": data.team})
    if existing:
        existing.pop("_id", None)
        return existing
    slug = f"{_slug(data.team)}-{_secrets.token_urlsafe(9).lower()}"
    room = {
        "id": str(uuid.uuid4()), "team": data.team,
        "slug": slug, "created_by": user["id"], "created_at": now_iso(),
    }
    await db.team_rooms.insert_one(room)
    room.pop("_id", None)
    return room


@router.get("/chat/rooms")
async def list_chat_rooms(user=Depends(get_current_user)):
    q = {}
    if user.get("role") == "coach":
        allowed = user.get("assigned_teams") or []
        q["team"] = {"$in": allowed}
    return await db.team_rooms.find(q, {"_id": 0}).to_list(200)


@router.get("/chat/rooms/{slug}")
async def get_chat_room(slug: str, request: Request):
    # Public: rate-limit to prevent slug enumeration
    await rate_limit_ip(request, "chat_room_lookup", max_hits=30, window_s=60)
    room = await db.team_rooms.find_one({"slug": slug}, {"_id": 0})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    return room


@router.post("/chat/rooms/{slug}/join")
async def join_chat(slug: str, data: ChatJoinIn, response: Response):
    room = await db.team_rooms.find_one({"slug": slug})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    if not data.child_name.strip():
        raise HTTPException(status_code=400, detail="Nombre del jugador requerido")
    token = await create_parent_chat_session(slug, data.child_name.strip())
    response.set_cookie(f"chat_{slug}", token, httponly=True, secure=False, samesite="lax", max_age=2592000, path="/")
    return {"ok": True, "child_name": data.child_name.strip()}


# ------------------ Messages ------------------
@router.get("/chat/rooms/{slug}/messages")
async def list_chat_messages(slug: str, request: Request):
    room = await db.team_rooms.find_one({"slug": slug})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    await _parent_or_staff(request, slug)
    return await db.chat_messages.find({"room_id": room["id"]}, {"_id": 0}).sort("created_at", 1).to_list(1000)


@router.post("/chat/rooms/{slug}/messages")
async def post_chat_message(slug: str, data: ChatMessageIn, request: Request):
    room = await db.team_rooms.find_one({"slug": slug})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    identity = await _parent_or_staff(request, slug)
    if not data.text.strip():
        raise HTTPException(status_code=400, detail="Mensaje vacío")
    msg = {
        "id": str(uuid.uuid4()), "room_id": room["id"],
        "author_name": identity.get("child") or identity.get("name", "?"),
        "author_role": identity["kind"],
        "text": data.text.strip(), "kind": "text",
        "created_at": now_iso(),
    }
    await db.chat_messages.insert_one(msg)
    msg.pop("_id", None)
    return msg


@router.post("/chat/rooms/{slug}/simulate")
async def simulate_chat_room(slug: str, data: ChatSimulateIn, user=Depends(require_roles("coach", "coordinator", "admin"))):
    room = await db.team_rooms.find_one({"slug": slug}, {"_id": 0})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")

    if data.reset:
        await db.chat_messages.delete_many({"room_id": room["id"]})

    existing = await db.chat_messages.count_documents({"room_id": room["id"]})
    if existing > 0:
        return {"ok": True, "inserted": 0, "reason": "already_has_messages"}

    now = datetime.now(timezone.utc)
    demo_messages = [
        {
            "id": str(uuid.uuid4()),
            "room_id": room["id"],
            "author_name": user.get("name", "Entrenador"),
            "author_role": "coach",
            "text": f"Hola familias de {room['team']} 👋 Bienvenidos al chat del equipo.",
            "kind": "text",
            "created_at": (now - timedelta(minutes=7)).isoformat(),
        },
        {
            "id": str(uuid.uuid4()),
            "room_id": room["id"],
            "author_name": "Lucía (madre de Pablo)",
            "author_role": "parent",
            "text": "Gracias míster. ¿La hora de mañana sigue siendo a las 18:30?",
            "kind": "text",
            "created_at": (now - timedelta(minutes=5)).isoformat(),
        },
        {
            "id": str(uuid.uuid4()),
            "room_id": room["id"],
            "author_name": user.get("name", "Entrenador"),
            "author_role": "coach",
            "text": "Sí, confirmada ✅ Traed agua y camiseta azul.",
            "kind": "text",
            "created_at": (now - timedelta(minutes=3)).isoformat(),
        },
        {
            "id": str(uuid.uuid4()),
            "room_id": room["id"],
            "author_name": "Carlos (padre de Mario)",
            "author_role": "parent",
            "text": "Perfecto, allí estaremos. ¡Gracias!",
            "kind": "text",
            "created_at": (now - timedelta(minutes=1)).isoformat(),
        },
    ]
    await db.chat_messages.insert_many(demo_messages)
    return {"ok": True, "inserted": len(demo_messages)}


# ------------------ Polls ------------------
@router.post("/chat/rooms/{slug}/polls")
async def create_poll(slug: str, data: PollIn, request: Request):
    room = await db.team_rooms.find_one({"slug": slug})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    identity = await _parent_or_staff(request, slug)
    if identity["kind"] != "staff":
        raise HTTPException(status_code=403, detail="Solo los entrenadores pueden crear encuestas")
    if not data.options or len(data.options) < 2:
        raise HTTPException(status_code=400, detail="La encuesta necesita al menos 2 opciones")
    poll = {
        "id": str(uuid.uuid4()), "room_id": room["id"],
        "question": data.question, "options": data.options, "kind": data.kind,
        "created_by": identity.get("name", "Entrenador"),
        "created_at": now_iso(),
    }
    await db.polls.insert_one(poll)
    # Also insert as a chat message so it appears in the feed
    await db.chat_messages.insert_one({
        "id": str(uuid.uuid4()), "room_id": room["id"],
        "author_name": identity.get("name", "Entrenador"),
        "author_role": "coach",
        "text": f"📊 Encuesta: {data.question}",
        "kind": "poll", "poll_id": poll["id"],
        "created_at": now_iso(),
    })
    poll.pop("_id", None)
    return poll


@router.get("/chat/rooms/{slug}/polls")
async def list_polls(slug: str, request: Request):
    room = await db.team_rooms.find_one({"slug": slug})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    await _parent_or_staff(request, slug)
    polls = await db.polls.find({"room_id": room["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)
    for p in polls:
        votes = await db.poll_votes.find({"poll_id": p["id"]}, {"_id": 0}).to_list(1000)
        counts = [0] * len(p["options"])
        for v in votes:
            if 0 <= v.get("option_index", -1) < len(counts):
                counts[v["option_index"]] += 1
        p["vote_counts"] = counts
        p["voters"] = [{"child": v["voter_name"], "option_index": v["option_index"]} for v in votes]
    return polls


@router.post("/chat/rooms/{slug}/polls/{poll_id}/vote")
async def vote_poll(slug: str, poll_id: str, data: PollVoteIn, request: Request):
    room = await db.team_rooms.find_one({"slug": slug})
    if not room:
        raise HTTPException(status_code=404, detail="Sala no encontrada")
    identity = await _parent_or_staff(request, slug)
    poll = await db.polls.find_one({"id": poll_id}, {"_id": 0})
    if not poll:
        raise HTTPException(status_code=404, detail="Encuesta no encontrada")
    if not (0 <= data.option_index < len(poll["options"])):
        raise HTTPException(status_code=400, detail="Opción inválida")
    voter_name = identity.get("child") or identity.get("name")
    if not voter_name:
        raise HTTPException(status_code=400, detail="Identidad no válida para votar")
    await db.poll_votes.delete_many({"poll_id": poll_id, "voter_name": voter_name})
    await db.poll_votes.insert_one({
        "id": str(uuid.uuid4()), "poll_id": poll_id,
        "voter_name": voter_name, "option_index": data.option_index,
        "created_at": now_iso(),
    })
    return {"ok": True}
