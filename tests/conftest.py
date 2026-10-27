import os
import secrets
import tempfile
from pathlib import Path
import pytest_asyncio

root = Path(tempfile.mkdtemp(prefix="vd-suite-"))
os.environ.update(
    DATABASE_URL="sqlite+aiosqlite:///" + (root / "suite.db").as_posix(),
    REDIS_URL="",
    WEBHOOK_URL="",
    RECORDS=str(root / "audio"),
    WORKER_ENABLED="false",
    AUDIO_ENABLED="false",
)
for key in ("ADMIN_PASSWORD", "API_TOKEN", "WEBHOOK_SECRET", "SESSION_SECRET"):
    os.environ[key] = secrets.token_urlsafe(32)


@pytest_asyncio.fixture
async def database():
    from dispatch.db import Base, engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()
