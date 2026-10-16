import hashlib
import hmac
import secrets
from sqlalchemy import select
from .config import cfg
from .db import Session, User


def password_hash(value, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(value.encode(), salt=salt.encode(), n=2**14, r=8, p=1).hex()
    return salt + "$" + digest


def verify(value, stored):
    return hmac.compare_digest(password_hash(value, stored.split("$")[0]), stored)


async def bootstrap():
    if min(map(len, [cfg.session_secret, cfg.admin_password, cfg.api_token, cfg.webhook_secret])) < 16:
        raise RuntimeError("Заполните секреты в .env с помощью scripts/configure.py")
    async with Session.begin() as db:
        if not await db.scalar(select(User.id).limit(1)):
            db.add(
                User(
                    login="admin",
                    name="Администратор",
                    role="admin",
                    password=password_hash(cfg.admin_password),
                )
            )
