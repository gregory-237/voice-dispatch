import argparse
import asyncio
import getpass
from sqlalchemy import select
from .db import Session, User, engine
from .security import password_hash


async def change(login):
    password = getpass.getpass("Новый пароль: ")
    if len(password) < 12 or password != getpass.getpass("Повторите пароль: "):
        raise SystemExit("Введите дважды одинаковый пароль длиной от 12 символов")
    async with Session.begin() as db:
        user = await db.scalar(select(User).where(User.login == login))
        if not user:
            raise SystemExit("Сотрудник не найден")
        user.password = password_hash(password)
    await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("login")
    asyncio.run(change(parser.parse_args().login))
