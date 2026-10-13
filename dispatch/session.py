import asyncio
import httpx
import contextlib
import logging
import re
from datetime import timedelta
from time import perf_counter
from sqlalchemy import select
from .audio import Recording
from .config import cfg
from .db import Call, Callback, Metric, Option, Session, Ticket, Turn, now
from .dialogue import Dialogue, HELLO, Reply
from .domain import EMERGENCY, Intake, create_ticket, event
from .jobs import ami, finish_callback
from .models import Segmenter, VoiceOptions, models, vad_model

log = logging.getLogger(__name__)


async def options():
    async with Session() as db:
        values = {o.key: o.value for o in (await db.scalars(select(Option))).all()}
    return VoiceOptions.model_validate(values), values.get("greeting", HELLO)


class Conversation:
    def __init__(self, uid, transport, medium, backend=models):
        self.uid, self.io, self.medium, self.backend = uid, transport, medium, backend
        self.queue = asyncio.Queue(maxsize=4)
        self.done = asyncio.Event()
        self.playback = None
        self.terminal = False
        self.result = "disconnected"
        self.generation = 0
        self.callback_id = None

    async def metric(self, stage, start):
        ms = round((perf_counter() - start) * 1000, 1)
        async with Session.begin() as db:
            db.add(Metric(call_id=self.uid, stage=stage, ms=ms))

    async def turn(self, speaker, text):
        async with Session.begin() as db:
            db.add(Turn(call_id=self.uid, speaker=speaker, text=text))
        await self.io.event({"type": "turn", "speaker": speaker, "text": text})

    async def interrupt(self):
        if self.playback and not self.playback.done():
            self.playback.cancel()
            await asyncio.gather(self.playback, return_exceptions=True)
            await self.io.event({"type": "interrupt"})

    async def say(self, reply, cacheable):
        await self.turn("assistant", reply.text)
        for sentence in re.split(r"(?<=[.!?])\s+", reply.text):
            if not sentence:
                continue
            start = perf_counter()
            pcm, cached = await asyncio.wait_for(
                self.backend.speech(sentence, self.voice.speaker, cacheable), 45
            )
            await self.metric("tts_cache" if cached else "tts", start)
            for i in range(0, len(pcm), 320):
                frame = pcm[i : i + 320]
                await self.io.send(frame)
                self.record.write(1, frame)
                await asyncio.sleep(len(frame) / 16000)

    async def listen(self):
        while not self.done.is_set():
            pcm = await asyncio.wait_for(self.io.receive(), 60)
            if not pcm:
                return
            self.record.write(0, pcm)
            for kind, data in self.segmenter.feed(pcm):
                if self.terminal:
                    continue
                if kind == "start":
                    self.generation += 1
                    await self.interrupt()
                else:
                    self.queue.put_nowait((self.generation, data))

    async def persist_action(self, reply):
        action = reply.action
        async with Session.begin() as db:
            call = await db.get(Call, self.uid)
            if action == "create":
                ticket = await create_ticket(
                    db, Intake(**self.flow.slots), "voice:" + self.uid, "Голосовой ассистент"
                )
                call.ticket_id = ticket.id
                reply.text = f"Заявка номер {ticket.id} зарегистрирована. Спасибо за обращение."
            elif action == "emergency":
                if not call.ticket_id:
                    defaults = dict(
                        address="Уточняет диспетчер", apartment="", name="", phone="", consent=False
                    )
                    ticket = Ticket(
                        **(defaults | self.flow.slots),
                        source_key="voice:" + self.uid,
                        digest="",
                        priority="emergency",
                        status="new",
                    )
                    db.add(ticket)
                    await db.flush()
                    call.ticket_id = ticket.id
                else:
                    ticket = await db.get(Ticket, call.ticket_id)
                    ticket.priority = "emergency"
                    await db.flush()
                await event(
                    db, ticket, "Голосовой ассистент", "Аварийное обращение. Передано на линию диспетчера"
                )
            if self.callback_id and action in ("close", "reopen"):
                job = await db.get(Callback, self.callback_id, with_for_update=True)
                await finish_callback(db, job, action)
            self.result = action

    async def respond(self):
        self.playback = asyncio.create_task(self.say(self.flow.start(), not self.callback_id))
        while True:
            take = asyncio.create_task(self.queue.get())
            try:
                if self.playback:
                    finished, _ = await asyncio.wait(
                        [self.playback, take], return_when=asyncio.FIRST_COMPLETED
                    )
                    if self.playback in finished and not self.playback.cancelled():
                        self.playback.result()
                serial, pcm = await take
            finally:
                if not take.done():
                    take.cancel()
                    await asyncio.gather(take, return_exceptions=True)
            start = perf_counter()
            text = await asyncio.wait_for(asyncio.to_thread(self.backend.transcribe, pcm), 50)
            await self.metric("asr", start)
            if not text:
                reply = Reply("Повторите, пожалуйста.")
            else:
                await self.turn("resident", text)
                extra = {}
                if self.flow.state in ("description", "phone") and not EMERGENCY.search(text):
                    start = perf_counter()
                    try:
                        extra = await asyncio.wait_for(
                            self.backend.extract(text, self.flow.state, self.voice.llm_model), 25
                        )
                        await self.metric("llm", start)
                    except (ValueError, TimeoutError, OSError, httpx.HTTPError):
                        log.warning("llm.fallback", extra={"session": self.uid})
                reply = self.flow.accept(text, extra)
            if reply.action != "continue":
                self.terminal = True
                await self.persist_action(reply)
            if serial != self.generation and not self.terminal:
                continue
            await self.interrupt()
            self.playback = asyncio.create_task(
                self.say(reply, not self.terminal and self.flow.state != "confirm")
            )
            if self.terminal:
                if reply.action in ("emergency", "transfer") and self.medium == "phone":
                    # Ending AudioSocket returns the live channel to the dispatcher dialplan immediately.
                    await self.interrupt()
                else:
                    await self.playback
                await self.io.event({"type": "result", "action": reply.action})
                return

    async def run(self):
        self.voice, greeting = await options()
        async with Session.begin() as db:
            job = await db.scalar(
                select(Callback)
                .where(Callback.session == self.uid, Callback.state == "dialing")
                .with_for_update()
            )
            if job:
                self.callback_id = job.id
                job.state, job.due = "talking", now() + timedelta(minutes=11)
            self.flow = Dialogue(greeting=greeting, callback=job.ticket_id if job else None)
            db.add(
                Call(
                    id=self.uid,
                    transport=self.medium,
                    direction="out" if job else "in",
                    ticket_id=job.ticket_id if job else None,
                )
            )
        self.record = Recording(cfg.records, self.uid)
        tasks = []
        try:
            self.segmenter = Segmenter(await asyncio.to_thread(vad_model), self.voice.silence_ms)
            async with asyncio.timeout(600):
                tasks = [asyncio.create_task(self.listen()), asyncio.create_task(self.respond())]
                finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in finished:
                    task.result()
        except Exception as exc:
            log.exception("voice.failed", extra={"session": self.uid})
            if not self.terminal:
                self.result = "error"
            with contextlib.suppress(Exception):
                await self.io.event({"type": "error", "message": "Сеанс прерван · " + type(exc).__name__})
        finally:
            self.done.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.playback:
                self.playback.cancel()
                await asyncio.gather(self.playback, return_exceptions=True)
            await asyncio.to_thread(self.record.close)
            async with Session.begin() as db:
                call = await db.get(Call, self.uid)
                call.ended, call.result = now(), self.result
                if self.callback_id:
                    job = await db.get(Callback, self.callback_id, with_for_update=True)
                    if job.state == "talking":
                        await finish_callback(db, job, self.result)
            if self.medium == "phone" and self.result in ("create", "close", "reopen"):
                with contextlib.suppress(OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
                    await ami({"Action": "DBPut", "Family": "vd-result", "Key": self.uid, "Val": "done"})
            await self.io.close()
