from datetime import datetime, timezone
from sqlalchemy import JSON, ForeignKey, String, Text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from .config import cfg


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str]
    role: Mapped[str]
    password: Mapped[str]


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    created: Mapped[datetime] = mapped_column(default=now, index=True)
    address: Mapped[str]
    apartment: Mapped[str]
    name: Mapped[str]
    category: Mapped[str]
    description: Mapped[str] = mapped_column(Text)
    phone: Mapped[str]
    consent: Mapped[bool] = mapped_column(default=False)
    priority: Mapped[str]
    status: Mapped[str] = mapped_column(default="new", index=True)
    assignee: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    source_key: Mapped[str] = mapped_column(unique=True)
    digest: Mapped[str]
    version: Mapped[int] = mapped_column(default=1)
    __mapper_args__ = {"version_id_col": version}


class History(Base):
    __tablename__ = "history"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    created: Mapped[datetime] = mapped_column(default=now)
    actor: Mapped[str]
    detail: Mapped[str]


class Call(Base):
    __tablename__ = "calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created: Mapped[datetime] = mapped_column(default=now, index=True)
    ended: Mapped[datetime | None]
    transport: Mapped[str]
    direction: Mapped[str]
    result: Mapped[str] = mapped_column(default="active")
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), index=True)


class Turn(Base):
    __tablename__ = "turns"
    id: Mapped[int] = mapped_column(primary_key=True)
    call_id: Mapped[str] = mapped_column(ForeignKey("calls.id"), index=True)
    created: Mapped[datetime] = mapped_column(default=now)
    speaker: Mapped[str]
    text: Mapped[str] = mapped_column(Text)


class Metric(Base):
    __tablename__ = "metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    created: Mapped[datetime] = mapped_column(default=now, index=True)
    call_id: Mapped[str]
    stage: Mapped[str]
    ms: Mapped[float]


class Callback(Base):
    __tablename__ = "callbacks"
    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    due: Mapped[datetime] = mapped_column(index=True)
    state: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    session: Mapped[str | None] = mapped_column(unique=True)
    detail: Mapped[str] = mapped_column(default="")


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[str] = mapped_column(primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    due: Mapped[datetime] = mapped_column(default=now, index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    state: Mapped[str] = mapped_column(default="queued")


class Option(Base):
    __tablename__ = "options"
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


engine = create_async_engine(cfg.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False)
