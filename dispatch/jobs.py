import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import timedelta
from uuid import uuid4
import httpx
from sqlalchemy import select
from .config import cfg
from .db import Callback, Delivery, Session, Ticket, now
from .domain import Change, change_ticket

log = logging.getLogger(__name__)


def signature(stamp, body):
    return hmac.new(cfg.webhook_secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()


async def ami(action):
    """One bounded AMI transaction, events disabled to avoid interleaved packets."""
    if not cfg.ami_password:
        raise ValueError("AMI не настроен")
    async with asyncio.timeout(6):
        reader, writer = await asyncio.open_connection(cfg.ami_host, cfg.ami_port)
        try:
            await reader.readline()
            for fields in (
                {"Action": "Login", "Username": cfg.ami_user, "Secret": cfg.ami_password, "Events": "off"},
                action,
            ):
                if any("\n" in str(v) or "\r" in str(v) for v in fields.values()):
                    raise ValueError("Недопустимое значение AMI")
                writer.write(("".join(f"{k}: {v}\r\n" for k, v in fields.items()) + "\r\n").encode())
                await writer.drain()
                response = await reader.readuntil(b"\r\n\r\n")
                if b"response: success" not in response.lower():
                    raise ConnectionError("AMI отклонил команду")
        finally:
            writer.close()
            await writer.wait_closed()


async def schedule(db, ticket_id, when):
    ticket = await db.get(Ticket, ticket_id, with_for_update=True)
    if not ticket or not ticket.consent or not ticket.phone or ticket.status != "done":
        raise ValueError("Нужна выполненная заявка с согласием на контрольный звонок")
    existing = await db.scalar(
        select(Callback).where(
            Callback.ticket_id == ticket_id, Callback.state.in_(["queued", "dialing", "talking"])
        )
    )
    if existing:
        return existing
    job = Callback(ticket_id=ticket.id, due=max(now(), when))
    db.add(job)
    await db.flush()
    return job


def retry(job, detail):
    job.state = "failed" if job.attempts >= 3 else "queued"
    job.due = now() + timedelta(minutes=5 * job.attempts)
    job.detail = detail
    job.session = None


async def finish_callback(db, job, outcome):
    ticket = await db.get(Ticket, job.ticket_id, with_for_update=True)
    if outcome not in ("close", "reopen") or ticket.status != "done":
        if ticket.status == "done":
            retry(job, "Разговор без подтверждения")
        else:
            job.state, job.detail = "cancelled", "Статус заявки изменён"
        return
    status = "closed" if outcome == "close" else "working"
    await change_ticket(db, ticket, Change(version=ticket.version, status=status), "Контрольный звонок")
    job.state, job.detail = (
        "finished",
        ("Выполнение подтверждено" if outcome == "close" else "Возвращена в работу"),
    )


async def outbound_once():
    async with Session.begin() as db:
        job = await db.scalar(
            select(Callback)
            .where(Callback.state.in_(["queued", "dialing", "talking"]), Callback.due <= now())
            .order_by(Callback.due)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not job:
            return False
        if job.state != "queued":
            retry(job, "Нет ответа до истечения срока ожидания")
            return True
        ticket = await db.get(Ticket, job.ticket_id)
        if ticket.status != "done" or not ticket.consent:
            job.state, job.detail = "cancelled", "Звонок больше не требуется"
            return True
        uid = str(uuid4())
        phone = ticket.phone
        job.state, job.session, job.attempts = "dialing", uid, job.attempts + 1
        job.due = now() + timedelta(seconds=75)
        job_id = job.id
    try:
        if not re.fullmatch(r"\+[1-9]\d{10,14}", phone) or not re.fullmatch(r"[A-Za-z0-9_-]+", cfg.sip_trunk):
            raise ValueError("Некорректный маршрут")
        await ami(
            {
                "Action": "Originate",
                "Channel": f"PJSIP/{phone}@{cfg.sip_trunk}",
                "Context": "vd-callback",
                "Exten": "s",
                "Priority": "1",
                "Timeout": "45000",
                "Async": "true",
                "Variable": "VD_UUID=" + uid,
            }
        )
    except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError) as exc:
        async with Session.begin() as db:
            job = await db.get(Callback, job_id, with_for_update=True)
            if job.session == uid and job.state == "dialing":
                retry(job, "Ошибка AMI · " + type(exc).__name__)
    return True


async def delivery_once():
    if not cfg.webhook_url:
        return False
    async with Session.begin() as db:
        item = await db.scalar(
            select(Delivery)
            .where(Delivery.state == "queued", Delivery.due <= now())
            .order_by(Delivery.due)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if not item:
            return False
        # Commit an expiring lease before networking. Redelivery uses the same event id.
        item.attempts += 1
        item.due = now() + timedelta(seconds=60)
        key, attempt, payload = item.id, item.attempts, item.payload
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    stamp = str(int(time.time()))
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                cfg.webhook_url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": key,
                    "X-VD-Time": stamp,
                    "X-VD-Signature": signature(stamp, body),
                },
            )
            r.raise_for_status()
        state = "sent"
    except httpx.HTTPError:
        state = "failed" if attempt >= 8 else "queued"
    async with Session.begin() as db:
        item = await db.get(Delivery, key, with_for_update=True)
        if item.attempts == attempt:
            item.state = state
            item.due = now() + timedelta(seconds=min(3600, 2**attempt * 5))
    return True


async def worker():
    while True:
        try:
            results = await asyncio.gather(outbound_once(), delivery_once())
            await asyncio.sleep(0.1 if any(results) else 2)
        except Exception:
            log.exception("worker.failed")
            await asyncio.sleep(3)
