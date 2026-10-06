import hashlib
import re
from enum import StrEnum
from uuid import uuid4
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from .config import cfg
from .db import Delivery, History, Ticket, User


class Category(StrEnum):
    water = "water"
    heat = "heat"
    power = "power"
    lift = "lift"
    common = "common"
    other = "other"


CATEGORIES = dict(
    water="Водоснабжение",
    heat="Отопление",
    power="Электроснабжение",
    lift="Лифты",
    common="Общее имущество",
    other="Прочее",
)
STATUSES = dict(new="Новая", assigned="Назначена", working="В работе", done="Выполнена", closed="Закрыта")
PRIORITIES = dict(normal="Плановый", high="Срочный", emergency="Аварийный")
TRANSITIONS = {
    "new": {"assigned", "working"},
    "assigned": {"working"},
    "working": {"done"},
    "done": {"closed", "working"},
    "closed": {"working"},
}
EMERGENCY = re.compile(
    r"запах.{0,10}газ|пахнет газ|утечк\w* газа|пожар|горит провод|искрит|прорвало|прорыв труб|затопл|застрял\w* в лифт|дым из",
    re.I,
)
KEYWORDS = {
    "water": r"вод|кран|теч[её]т|канализ|труб",
    "heat": r"батаре|отоплен|радиатор",
    "power": r"свет|электр|провод|розетк",
    "lift": r"лифт",
    "common": r"подъезд|двер|мусор|двор",
}


def category_for(text):
    return next((key for key, pattern in KEYWORDS.items() if re.search(pattern, text, re.I)), "other")


def normalize_phone(value):
    if re.search(r"[^+0-9()\s.\-]", value):
        raise ValueError("Назовите номер телефона цифрами")
    digits = re.sub(r"\D", "", value)
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if not re.fullmatch(r"[1-9]\d{10,14}", digits):
        raise ValueError("Проверьте номер телефона")
    return "+" + digits


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    address: str = Field(min_length=3, max_length=240)
    apartment: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=2, max_length=100)
    category: Category
    description: str = Field(min_length=3, max_length=1600)
    phone: str
    consent: bool = False

    @field_validator("phone")
    @classmethod
    def phone_format(cls, value):
        return normalize_phone(value)


class Change(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    status: Literal["new", "assigned", "working", "done", "closed"] | None = None
    assignee: int | None = None
    priority: Literal["normal", "high", "emergency"] | None = None


def snapshot(ticket):
    keys = (
        "id",
        "address",
        "apartment",
        "name",
        "category",
        "description",
        "phone",
        "consent",
        "priority",
        "status",
        "assignee",
        "version",
    )
    return {k: getattr(ticket, k) for k in keys} | {"created": ticket.created.isoformat() + "Z"}


async def event(db, ticket, actor, detail):
    db.add(History(ticket_id=ticket.id, actor=actor, detail=detail))
    if cfg.webhook_url:
        event_id = str(uuid4())
        db.add(
            Delivery(
                id=event_id, payload={"id": event_id, "event": "ticket.changed", "ticket": snapshot(ticket)}
            )
        )


async def create_ticket(db, data, key, actor, emergency=False):
    digest = hashlib.sha256(data.model_dump_json().encode()).hexdigest()
    existing = await db.scalar(select(Ticket).where(Ticket.source_key == key))
    if existing:
        if existing.digest != digest:
            raise ValueError("Ключ уже использован с другими данными")
        return existing
    priority = "emergency" if emergency or EMERGENCY.search(data.description) else "normal"
    ticket = Ticket(**data.model_dump(), source_key=key, digest=digest, priority=priority)
    db.add(ticket)
    await db.flush()
    await event(db, ticket, actor, "Зарегистрировано обращение")
    return ticket


async def change_ticket(db, ticket, patch, actor):
    if ticket.version != patch.version:
        raise ValueError("Карточку уже изменили. Обновите страницу")
    changes = []
    if patch.assignee is not None:
        user = await db.get(User, patch.assignee)
        if not user or user.role == "viewer":
            raise ValueError("Выберите ответственного сотрудника")
        ticket.assignee = user.id
        changes.append("Ответственный — " + user.name)
    if patch.status and patch.status != ticket.status:
        if patch.status not in TRANSITIONS[ticket.status]:
            raise ValueError("Недопустимый переход статуса")
        if patch.status == "assigned" and not ticket.assignee:
            raise ValueError("Не указан ответственный")
        ticket.status = patch.status
        changes.append("Статус — " + STATUSES[patch.status])
    if patch.priority and patch.priority != ticket.priority:
        ticket.priority = patch.priority
        changes.append("Приоритет — " + PRIORITIES[patch.priority])
    if changes:
        await db.flush()
        await event(db, ticket, actor, ". ".join(changes))
