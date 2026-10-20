import asyncio
import secrets
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select, text
from .config import cfg, tz
from .db import Call, Callback, Delivery, History, Metric, Option, Session, Ticket, Turn, User, now
from .domain import CATEGORIES, PRIORITIES, STATUSES, TRANSITIONS, Change, change_ticket
from .jobs import schedule
from .models import VoiceOptions, models
from .security import password_hash, verify
from .session import options

web = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
RESULTS = dict(
    active="На линии",
    disconnected="Завершён абонентом",
    create="Создана заявка",
    transfer="Перевод",
    emergency="Аварийный перевод",
    error="Сбой",
    close="Подтверждено",
    reopen="Доработка",
    interrupted="Прерван",
)
JOBS = dict(
    queued="Запланирован",
    dialing="Вызов",
    talking="Разговор",
    finished="Завершён",
    failed="Исчерпаны попытки",
    cancelled="Отменён",
)
ROLES = dict(admin="Администратор", dispatcher="Диспетчер", viewer="Наблюдатель")
NAV = [
    ("dashboard", "Обзор смены", "overview"),
    ("tickets", "Заявки", "tickets"),
    ("calls", "Журнал звонков", "phone"),
    ("outbound", "Исходящие", "out"),
    ("studio", "Голосовая проверка", "mic"),
    ("monitor", "Мониторинг", "pulse"),
    ("settings", "Сценарии и модели", "settings"),
    ("users", "Сотрудники", "users"),
]
templates.env.globals.update(
    cats=CATEGORIES,
    priorities=PRIORITIES,
    statuses=STATUSES,
    results=RESULTS,
    jobs=JOBS,
    roles=ROLES,
    nav=NAV,
    timezone_name=cfg.timezone,
)
templates.env.filters["local"] = lambda d, fmt="%d.%m.%Y %H:%M": (
    d.replace(tzinfo=timezone.utc).astimezone(tz).strftime(fmt) if d else "—"
)
attempts = defaultdict(deque)


class LoginRequired(Exception):
    pass


async def identity(req, level="viewer"):
    uid = req.session.get("uid")
    if not uid:
        raise LoginRequired()
    async with Session() as db:
        user = await db.get(User, uid)
    if not user:
        raise LoginRequired()
    if ROLES_RANK[user.role] < ROLES_RANK[level]:
        raise HTTPException(403, "Недостаточно прав")
    return user


ROLES_RANK = {"viewer": 0, "dispatcher": 1, "admin": 2}


def render(req, page, user, **values):
    return templates.TemplateResponse(
        request=req,
        name="panel.html",
        context=dict(
            page=page,
            user=user,
            csrf=req.session.setdefault("csrf", secrets.token_urlsafe(24)),
            date=now(),
            **values,
        ),
    )


async def form(req):
    fields = await req.form()
    if not secrets.compare_digest(fields.get("csrf", ""), req.session.get("csrf", "missing")):
        raise HTTPException(403, "Обновите форму")
    return fields


@web.get("/login")
async def login(req: Request):
    return render(req, "login", None)


@web.post("/login")
async def authenticate(req: Request):
    f = await form(req)
    key = req.client.host
    bucket = attempts[key]
    while bucket and bucket[0] < monotonic() - 300:
        bucket.popleft()
    if len(bucket) >= 12:
        raise HTTPException(429, "Повторите вход через пять минут")
    async with Session() as db:
        user = await db.scalar(select(User).where(User.login == f.get("login")))
    if not user or not await asyncio.to_thread(verify, f.get("password", "")[:512], user.password):
        bucket.append(monotonic())
        return render(req, "login", None, error="Проверьте логин и пароль")
    req.session.clear()
    req.session["uid"] = user.id
    return RedirectResponse("/", 303)


@web.post("/logout")
async def logout(req: Request):
    await form(req)
    req.session.clear()
    return RedirectResponse("/login", 303)


@web.get("/")
async def dashboard(req: Request):
    user = await identity(req)
    today = datetime.now(tz).date()
    start = (
        datetime.combine(today, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    )
    async with Session() as db:

        async def count(model, *filters):
            return await db.scalar(select(func.count()).select_from(model).where(*filters))

        stats = [
            await count(Ticket, Ticket.created >= start),
            await count(Ticket, Ticket.status.in_(["assigned", "working"])),
            await count(Ticket, Ticket.priority == "emergency", Ticket.status.not_in(["done", "closed"])),
            await count(Call, Call.created >= start),
        ]
        tickets = list(await db.scalars(select(Ticket).order_by(Ticket.created.desc()).limit(6)))
        counts = dict(
            (
                await db.execute(
                    select(Ticket.category, func.count())
                    .where(Ticket.created >= start - timedelta(days=6))
                    .group_by(Ticket.category)
                )
            ).all()
        )
        trend = []
        for i in range(7):
            day = start - timedelta(days=6 - i)
            trend.append(
                (
                    (today - timedelta(days=6 - i)).strftime("%d.%m"),
                    await count(Call, Call.created >= day, Call.created < day + timedelta(days=1)),
                    await count(Ticket, Ticket.created >= day, Ticket.created < day + timedelta(days=1)),
                )
            )
    return render(
        req,
        "dashboard",
        user,
        stats=stats,
        tickets=tickets,
        counts=counts,
        trend=trend,
        peak=max(1, *[max(x[1:]) for x in trend]),
    )


@web.get("/tickets")
async def tickets(req: Request, q: str = "", status: str = "", priority: str = "", p: int = 1):
    user = await identity(req)
    stmt = select(Ticket)
    if status:
        stmt = stmt.where(Ticket.status == status)
    if priority:
        stmt = stmt.where(Ticket.priority == priority)
    if q:
        value = "%" + q[:200] + "%"
        stmt = stmt.where(
            or_(
                Ticket.address.ilike(value),
                Ticket.name.ilike(value),
                Ticket.description.ilike(value),
                Ticket.id == (int(q) if q.isascii() and q.isdigit() and len(q) < 10 else -1),
            )
        )
    p = max(1, p)
    async with Session() as db:
        rows = list(await db.scalars(stmt.order_by(Ticket.created.desc()).offset((p - 1) * 40).limit(41)))
    return render(
        req,
        "tickets",
        user,
        tickets=rows[:40],
        more=len(rows) > 40,
        p=p,
        query=urlencode(dict(q=q, status=status, priority=priority)),
        q=q,
        status=status,
        priority=priority,
    )


@web.get("/tickets/{tid}")
async def ticket(req: Request, tid: int):
    user = await identity(req)
    async with Session() as db:
        t = await db.get(Ticket, tid)
        if not t:
            raise HTTPException(404)
        history = list(
            await db.scalars(select(History).where(History.ticket_id == tid).order_by(History.id.desc()))
        )
        calls = list(await db.scalars(select(Call).where(Call.ticket_id == tid).order_by(Call.created)))
        staff = list(await db.scalars(select(User).where(User.role != "viewer").order_by(User.name)))
    return render(
        req, "ticket", user, t=t, history=history, calls=calls, staff=staff, allowed=TRANSITIONS[t.status]
    )


@web.post("/tickets/{tid}")
async def edit(req: Request, tid: int):
    user = await identity(req, "dispatcher")
    f = await form(req)
    data = Change(
        version=f["version"], status=f["status"], assignee=f.get("assignee") or None, priority=f["priority"]
    )
    async with Session.begin() as db:
        t = await db.get(Ticket, tid, with_for_update=True)
        if not t:
            raise HTTPException(404)
        await change_ticket(db, t, data, user.name)
    return RedirectResponse(f"/tickets/{tid}", 303)


@web.post("/tickets/{tid}/callback")
async def book(req: Request, tid: int):
    await identity(req, "dispatcher")
    f = await form(req)
    when = datetime.fromisoformat(f["due"]).replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    async with Session.begin() as db:
        await schedule(db, tid, when)
    return RedirectResponse("/outbound", 303)


@web.get("/calls")
async def calls(req: Request, direction: str = "", p: int = 1):
    user = await identity(req)
    stmt = select(Call)
    if direction:
        stmt = stmt.where(Call.direction == direction)
    async with Session() as db:
        rows = list(
            await db.scalars(stmt.order_by(Call.created.desc()).offset((max(1, p) - 1) * 40).limit(41))
        )
    return render(
        req,
        "calls",
        user,
        calls=rows[:40],
        direction=direction,
        p=max(1, p),
        more=len(rows) > 40,
        query=urlencode(dict(direction=direction)),
    )


@web.get("/calls/{uid}")
async def call(req: Request, uid: str):
    user = await identity(req)
    async with Session() as db:
        c = await db.get(Call, uid)
        if not c:
            raise HTTPException(404)
        turns = list(await db.scalars(select(Turn).where(Turn.call_id == uid).order_by(Turn.id)))
    return render(req, "call", user, c=c, turns=turns, recorded=(cfg.records / (c.id + ".wav")).is_file())


@web.get("/audio/{uid}")
async def recording(req: Request, uid: str):
    from uuid import UUID

    await identity(req)
    path = cfg.records / (str(UUID(uid)) + ".wav")
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav", headers={"Cache-Control": "private, no-store"})


@web.get("/outbound")
async def queue(req: Request):
    user = await identity(req)
    async with Session() as db:
        rows = (
            await db.execute(select(Callback, Ticket).join(Ticket).order_by(Callback.due.desc()).limit(200))
        ).all()
    return render(req, "outbound", user, queue=rows)


@web.get("/studio")
async def studio(req: Request):
    return render(req, "studio", await identity(req, "dispatcher"))


@web.get("/settings")
async def settings(req: Request):
    user = await identity(req, "admin")
    voice, greeting = await options()
    return render(req, "settings", user, voice=voice, greeting=greeting)


@web.post("/settings")
async def save(req: Request):
    await identity(req, "admin")
    f = await form(req)
    voice = VoiceOptions.model_validate(dict(f))
    greeting = f["greeting"].strip()
    if not 10 <= len(greeting) <= 400 or "автоматизи" not in greeting.lower():
        raise ValueError("Представьтесь автоматизированной диспетчерской (10–400 символов)")
    async with Session.begin() as db:
        for k, v in (voice.model_dump() | {"greeting": greeting}).items():
            await db.merge(Option(key=k, value=str(v)))
    return RedirectResponse("/settings?saved=1", 303)


@web.get("/users")
async def staff(req: Request):
    user = await identity(req, "admin")
    async with Session() as db:
        rows = list(await db.scalars(select(User).order_by(User.id)))
    return render(req, "users", user, staff=rows)


@web.post("/users")
async def add_user(req: Request):
    await identity(req, "admin")
    f = await form(req)
    if (
        f["role"] not in ROLES
        or not 12 <= len(f["password"]) <= 200
        or not 1 <= len(f["login"].strip()) <= 60
        or not f["name"].strip()
    ):
        raise ValueError("Проверьте логин, имя, роль и пароль (от 12 символов)")
    async with Session.begin() as db:
        if await db.scalar(select(User.id).where(User.login == f["login"].strip())):
            raise ValueError("Логин занят")
        db.add(
            User(
                login=f["login"].strip(),
                name=f["name"].strip()[:100],
                role=f["role"],
                password=password_hash(f["password"]),
            )
        )
    return RedirectResponse("/users", 303)


@web.get("/monitor")
async def monitor(req: Request):
    user = await identity(req)
    async with Session() as db:
        await db.execute(text("SELECT 1"))
        metrics = (
            await db.execute(
                select(Metric.stage, func.count(), func.avg(Metric.ms), func.max(Metric.ms))
                .where(Metric.created >= now() - timedelta(hours=24))
                .group_by(Metric.stage)
            )
        ).all()
        failures = await db.scalar(
            select(func.count()).select_from(Delivery).where(Delivery.state == "failed")
        )
    services = {
        "База данных": True,
        "AudioSocket": getattr(req.app.state, "server", None) is not None,
        "Модель ASR": Path(cfg.asr_path, "model.bin").is_file(),
        "Модель TTS": Path(cfg.tts_path).is_file(),
    }
    try:
        services["Redis"] = bool(models.redis and await models.redis.ping())
    except Exception:
        services["Redis"] = False
    voice, _ = await options()
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            response = await client.get(cfg.ollama_url + "/api/tags")
            services["Ollama · " + voice.llm_model] = voice.llm_model in [
                m["name"] for m in response.json()["models"]
            ]
    except (httpx.HTTPError, ValueError, KeyError):
        services["Ollama · " + voice.llm_model] = False
    return render(req, "monitor", user, services=services, metrics=metrics, failures=failures)
