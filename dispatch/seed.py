"""Populate an empty registry with identifiable synthetic records, without dialing."""

import asyncio
from datetime import timedelta
from uuid import uuid5, NAMESPACE_URL
from sqlalchemy import select
from .db import Call, Callback, History, Session, Ticket, Turn, engine, now
from .security import bootstrap

SAMPLES = [
    ("ул. Садовая, 18", "Течёт кран в подвале", "water"),
    ("ул. Мира, 7", "Не горит свет на лестнице", "power"),
    ("пр-т Строителей, 42", "Холодные батареи в квартире", "heat"),
    ("ул. Полевая, 3", "Не закрывается дверь подъезда", "common"),
    ("ул. Набережная, 12", "Лифт пропускает этаж", "lift"),
    ("ул. Лесная, 26", "Прорвало трубу в подвале", "water"),
    ("ул. Центральная, 9", "Нет горячей воды", "water"),
]
NAMES = ["Ирина С.", "Олег К.", "Марина В.", "Денис А.", "Елена М."]


async def seed():
    await bootstrap()
    async with Session.begin() as db:
        if await db.scalar(select(Ticket.id).limit(1)):
            print("Реестр уже заполнен")
            return
        moment = now()
        for i in range(72):
            address, description, category = SAMPLES[i % len(SAMPLES)]
            at = moment - timedelta(minutes=1 + i * 9 + (i // 10) ** 2 * 155)
            status = ["new", "working", "assigned", "done", "closed"][i % 5]
            t = Ticket(
                created=at,
                address=address,
                description=description,
                category=category,
                apartment=str(2 + i % 50),
                name=NAMES[i % 5],
                phone="+7000000" + f"{i:04}",
                consent=i % 3 != 0,
                status=status,
                assignee=1 if status != "new" else None,
                priority="emergency" if i % 7 == 5 else "high" if i % 5 == 1 else "normal",
                source_key=f"fixture:{i}",
                digest="synthetic",
            )
            db.add(t)
            await db.flush()
            uid = str(uuid5(NAMESPACE_URL, f"urn:voice-dispatch:synthetic:{i}"))
            db.add(
                Call(
                    id=uid,
                    created=at - timedelta(seconds=48),
                    ended=at,
                    transport="phone",
                    direction="in",
                    result="emergency" if t.priority == "emergency" else "create",
                    ticket_id=t.id,
                )
            )
            await db.flush()
            db.add(
                History(
                    ticket_id=t.id,
                    created=at,
                    actor="Демонстрационные данные",
                    detail="Зарегистрировано обращение",
                )
            )
            db.add(
                Turn(call_id=uid, speaker="resident", text=description, created=at - timedelta(seconds=25))
            )
            db.add(Turn(call_id=uid, speaker="assistant", text="Обращение принято.", created=at))
            if status == "closed" and t.consent:
                db.add(
                    Callback(
                        ticket_id=t.id, state="finished", due=at, attempts=1, detail="Выполнение подтверждено"
                    )
                )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
