import hashlib
import hmac
import time
from datetime import timezone
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .config import cfg
from .db import History, Session, Ticket
from .domain import Change, Intake, change_ticket, create_ticket, snapshot
from .jobs import schedule, signature

api = APIRouter(prefix="/api/v1")


def token(authorization: Annotated[str, Header()] = ""):
    if not hmac.compare_digest(authorization, "Bearer " + cfg.api_token):
        raise HTTPException(401, "Требуется API-токен")


def key_valid(key):
    if not key or len(key) > 120:
        raise HTTPException(422, "Нужен Idempotency-Key длиной до 120 символов")
    return key


async def insert(data, key, actor):
    try:
        async with Session.begin() as db:
            return snapshot(await create_ticket(db, data, key, actor))
    except IntegrityError:
        # Concurrent insert has committed by the time its unique constraint fires.
        async with Session() as db:
            row = await db.scalar(select(Ticket).where(Ticket.source_key == key))
            if row and row.digest == hashlib.sha256(data.model_dump_json().encode()).hexdigest():
                return snapshot(row)
        raise HTTPException(409, "Конфликт ключа идемпотентности") from None


@api.post("/tickets", dependencies=[Depends(token)], status_code=201)
async def add(data: Intake, idempotency_key: Annotated[str, Header()]):
    return await insert(data, "api:" + key_valid(idempotency_key), "CRM")


@api.get("/tickets", dependencies=[Depends(token)])
async def listing(status: str = "", after: int = 0, limit: int = 50):
    async with Session() as db:
        q = select(Ticket).where(Ticket.id > after).order_by(Ticket.id).limit(min(max(limit, 1), 200))
        if status:
            q = q.where(Ticket.status == status)
        return [snapshot(t) for t in await db.scalars(q)]


@api.get("/tickets/{ticket_id}", dependencies=[Depends(token)])
async def detail(ticket_id: int):
    async with Session() as db:
        t = await db.get(Ticket, ticket_id)
        if not t:
            raise HTTPException(404)
        history = await db.scalars(select(History).where(History.ticket_id == ticket_id).order_by(History.id))
        return snapshot(t) | {
            "history": [
                {"at": h.created.isoformat() + "Z", "actor": h.actor, "detail": h.detail} for h in history
            ]
        }


@api.patch("/tickets/{ticket_id}", dependencies=[Depends(token)])
async def patch(ticket_id: int, data: Change):
    async with Session.begin() as db:
        t = await db.get(Ticket, ticket_id, with_for_update=True)
        if not t:
            raise HTTPException(404)
        await change_ticket(db, t, data, "CRM")
        return snapshot(t)


class Schedule(BaseModel):
    due: AwareDatetime


@api.post("/tickets/{ticket_id}/callback", dependencies=[Depends(token)])
async def callback(ticket_id: int, data: Schedule):
    async with Session.begin() as db:
        job = await schedule(db, ticket_id, data.due.astimezone(timezone.utc).replace(tzinfo=None))
        return {"id": job.id, "status": job.state}


@api.post("/webhooks/intake", status_code=201)
async def intake_hook(
    request: Request,
    x_vd_time: Annotated[str, Header()],
    x_vd_signature: Annotated[str, Header()],
    idempotency_key: Annotated[str, Header()],
):
    body = await request.body()
    if not x_vd_time.isdecimal() or len(x_vd_time) > 12 or abs(time.time() - int(x_vd_time)) > 300:
        raise HTTPException(401, "Истёк срок подписи")
    if not hmac.compare_digest(signature(x_vd_time, body), x_vd_signature):
        raise HTTPException(401, "Неверная подпись")
    # Event id is signed inside the payload as well as sent in the header.
    envelope = Webhook.model_validate_json(body)
    if envelope.id != key_valid(idempotency_key):
        raise HTTPException(422, "ID события отличается от заголовка")
    return await insert(envelope.ticket, "hook:" + envelope.id, "Webhook CRM")


class Webhook(BaseModel):
    id: str
    ticket: Intake
