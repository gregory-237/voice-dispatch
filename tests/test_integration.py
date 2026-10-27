import asyncio
from uuid import uuid4
import httpx
from dispatch.audio import AudioSocket, packet, read_packet
from dispatch.config import cfg
from dispatch.main import app


async def test_crm_idempotency_and_versions(database):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as c:
        payload = dict(
            address="Мира 12",
            apartment="6",
            name="Иван",
            category="water",
            description="Течёт кран",
            phone="+79000000000",
            consent=False,
        )
        headers = {"Authorization": "Bearer " + cfg.api_token, "Idempotency-Key": "case-one"}
        first = await c.post("/api/v1/tickets", json=payload, headers=headers)
        repeat = await c.post("/api/v1/tickets", json=payload, headers=headers)
        assert first.status_code == 201 and first.json() == repeat.json()
        url = "/api/v1/tickets/" + str(first.json()["id"])
        assert (
            await c.patch(url, json={"version": 1, "status": "working"}, headers=headers)
        ).status_code == 200
        assert (
            await c.patch(url, json={"version": 1, "status": "closed"}, headers=headers)
        ).status_code == 409
        assert (await c.get(url)).status_code == 401


async def test_tcp_audio_roundtrip():
    uid = uuid4()
    seen = asyncio.get_running_loop().create_future()

    async def serve(reader, writer):
        sock = AudioSocket(reader, writer)
        try:
            identity = await sock.identify()
            audio = await sock.receive()
            await sock.send(audio)
            seen.set_result(identity)
        finally:
            await sock.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    async with server:
        r, w = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        w.write(packet(1, uid.bytes) + packet(16, b"\x00\x04" * 160))
        await w.drain()
        assert await read_packet(r) == (16, b"\x00\x04" * 160)
        assert await seen == str(uid)
        w.close()
        await w.wait_closed()
