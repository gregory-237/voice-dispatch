import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text, update
from sqlalchemy.orm.exc import StaleDataError
from starlette.middleware.sessions import SessionMiddleware
from .api import api
from .audio import AudioSocket
from .config import cfg
from .db import Call, Session, engine, now
from .jobs import worker
from .models import models
from .security import bootstrap
from .session import Conversation
from .web import LoginRequired, identity, render, web


class LogJSON(logging.Formatter):
    def format(self, record):
        data = dict(
            at=datetime.now(timezone.utc).isoformat(),
            level=record.levelname,
            event=record.getMessage(),
            logger=record.name,
        )
        if hasattr(record, "session"):
            data["call_id"] = record.session
        if record.exc_info:
            data["error"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


log = logging.getLogger("dispatch")
handler = logging.StreamHandler()
handler.setFormatter(LogJSON())
log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False


@asynccontextmanager
async def lifespan(app):
    await bootstrap()
    async with Session.begin() as db:
        await db.execute(update(Call).where(Call.ended.is_(None)).values(ended=now(), result="interrupted"))
    active = set()

    async def telephone(reader, writer):
        sock = AudioSocket(reader, writer)
        task = asyncio.current_task()
        active.add(task)
        try:
            uid = await sock.identify()
            await Conversation(uid, sock, "phone").run()
        except Exception:
            log.exception("audio.connection_failed")
        finally:
            writer.close()
            active.discard(task)

    server = (
        await asyncio.start_server(telephone, cfg.audio_host, cfg.audio_port) if cfg.audio_enabled else None
    )
    app.state.server = server
    task = asyncio.create_task(worker()) if cfg.worker_enabled else None
    try:
        yield
    finally:
        if server:
            server.close()
            await server.wait_closed()
        pending = list(active) + ([task] if task else [])
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if models.redis:
            await models.redis.aclose()
        await engine.dispose()


app = FastAPI(title="VoiceDispatch", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=cfg.session_secret,
    same_site="strict",
    https_only=cfg.secure_cookie,
    max_age=8 * 3600,
    session_cookie="vd_session",
)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(api)
app.include_router(web)


@app.exception_handler(LoginRequired)
async def need_login(req, exc):
    return RedirectResponse("/login", 303)


@app.exception_handler(ValueError)
@app.exception_handler(StaleDataError)
async def conflict(req: Request, exc):
    msg = "Данные изменились. Обновите карточку." if isinstance(exc, StaleDataError) else str(exc)
    if req.url.path.startswith("/api/"):
        return JSONResponse({"detail": msg}, status_code=409)
    response = render(req, "error", await identity(req), error=msg)
    response.status_code = 409
    return response


@app.get("/health")
async def health():
    async with Session() as db:
        await db.execute(text("SELECT 1"))
    return {"status": "ok"}


class BrowserAudio:
    def __init__(self, ws):
        self.ws = ws

    async def receive(self):
        try:
            pcm = await self.ws.receive_bytes()
        except WebSocketDisconnect:
            return b""
        if len(pcm) > 8192 or len(pcm) % 2:
            raise ValueError("Некорректный PCM")
        return pcm

    async def send(self, pcm):
        await self.ws.send_bytes(pcm)

    async def event(self, message):
        await self.ws.send_json(message)

    async def close(self):
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await self.ws.close()


@app.websocket("/ws/voice")
async def browser(ws: WebSocket):
    from urllib.parse import urlsplit

    origin = urlsplit(ws.headers.get("origin", ""))
    if origin.netloc != ws.headers.get("host") or origin.scheme not in ("http", "https"):
        await ws.close(code=1008)
        return
    try:
        await identity(ws, "dispatcher")
    except Exception:
        await ws.close(code=1008)
        return
    await ws.accept()
    await Conversation(str(uuid4()), BrowserAudio(ws), "browser").run()
