"""Tickets endpoints — upload, list, file stream, approval (admin only)."""
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel

from deps import (
    db, now_iso, uuid, APP_NAME, MAX_UPLOAD_BYTES,
    get_current_user, get_user_from_request_values, require_roles, put_object, get_object,
)

router = APIRouter()


class TicketApproveIn(BaseModel):
    approved: bool
    notes: str = ""


@router.post("/tickets/upload")
async def upload_ticket(file: UploadFile = File(...), concept: str = Form(""), amount: float = Form(0),
                         user=Depends(require_roles("coordinator", "admin"))):
    ext = (file.filename or "file").split(".")[-1].lower()
    if ext not in ("png", "jpg", "jpeg", "webp", "heic", "heif", "pdf"):
        ext = "jpg"
    path = f"{APP_NAME}/tickets/{user['id']}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"Archivo demasiado grande (máx {MAX_UPLOAD_BYTES // (1024*1024)} MB)")
    mime = file.content_type or "image/jpeg"
    result = put_object(path, data, mime)
    ticket = {
        "id": str(uuid.uuid4()),
        "storage_path": result["path"],
        "original_filename": file.filename or f"ticket.{ext}",
        "content_type": mime,
        "size": result.get("size", len(data)),
        "concept": concept,
        "amount": float(amount or 0),
        "uploaded_by": user["id"],
        "uploader_name": user.get("name", ""),
        "status": "pendiente",
        "approval_notes": "",
        "created_at": now_iso(),
    }
    await db.tickets.insert_one(ticket)
    ticket.pop("_id", None)
    return ticket


@router.get("/tickets")
async def list_tickets(user=Depends(get_current_user)):
    q = {}
    if user["role"] == "coach":
        q["uploaded_by"] = user["id"]
    return await db.tickets.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)


@router.get("/tickets/{ticket_id}/file")
async def ticket_file(ticket_id: str, auth: Optional[str] = Query(None), authorization: Optional[str] = Header(None), request: Request = None):
    user = await get_user_from_request_values(request=request, authorization=authorization, query_token=auth)
    ticket = await db.tickets.find_one({"id": ticket_id}, {"_id": 0})
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    # Coaches can only access their own tickets. Admins/coordinators can see all.
    if user.get("role") == "coach" and ticket.get("uploaded_by") != user.get("id"):
        raise HTTPException(status_code=403, detail="Permiso denegado")
    data, ct = get_object(ticket["storage_path"])
    return FastAPIResponse(content=data, media_type=ticket.get("content_type", ct))


@router.post("/tickets/{ticket_id}/approve")
async def approve_ticket(ticket_id: str, data: TicketApproveIn, user=Depends(require_roles("admin"))):
    new_status = "aprobado" if data.approved else "rechazado"
    result = await db.tickets.update_one({"id": ticket_id}, {"$set": {
        "status": new_status, "approval_notes": data.notes,
        "approved_by": user["id"], "approved_at": now_iso(),
    }})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    return await db.tickets.find_one({"id": ticket_id}, {"_id": 0})
