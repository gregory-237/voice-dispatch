import asyncio
import contextlib
import struct
import wave
from pathlib import Path
from time import monotonic
from uuid import UUID

import numpy as np

HEADER = struct.Struct("!BH")


def packet(kind, payload=b""):
    return HEADER.pack(kind, len(payload)) + payload


async def read_packet(reader):
    kind, size = HEADER.unpack(await reader.readexactly(3))
    if size > 8192:
        raise ValueError("Слишком большой кадр")
    return kind, await reader.readexactly(size)


class AudioSocket:
    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer

    async def identify(self):
        kind, payload = await asyncio.wait_for(read_packet(self.reader), 4)
        if kind != 1 or len(payload) != 16:
            raise ValueError("Ожидался UUID")
        return str(UUID(bytes=payload))

    async def receive(self):
        while True:
            kind, data = await read_packet(self.reader)
            if kind in (0, 255):
                return b""
            if kind == 3:
                continue
            if kind != 0x10 or not data or len(data) % 2:
                raise ValueError("Ожидался PCM16 mono 8000")
            return data

    async def send(self, pcm):
        self.writer.write(packet(0x10, pcm))
        await self.writer.drain()

    async def event(self, message):
        pass  # AudioSocket carries only PCM, digits and session control.

    async def close(self):
        with contextlib.suppress(OSError, ConnectionError):
            self.writer.write(packet(0))
            await self.writer.drain()
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


class Recording:
    """Keep stereo channels aligned to the call clock, including overlapping speech."""

    def __init__(self, directory: Path, session):
        self.start = monotonic()
        self.path = directory / (str(UUID(session)) + ".wav")
        self.temp = [self.path.with_suffix(s) for s in (".in.pcm", ".out.pcm")]
        self.files = [p.open("w+b") for p in self.temp]

    def write(self, channel, pcm):
        f = self.files[channel]
        offset = int((monotonic() - self.start) * 8000) * 2
        if offset > f.tell() + 640:
            f.seek(offset)
        f.write(pcm)

    def close(self):
        for f in self.files:
            f.seek(0)
        with wave.open(str(self.path), "wb") as wav:
            wav.setparams((2, 2, 8000, 0, "NONE", "not compressed"))
            while True:
                a, b = (f.read(16000) for f in self.files)
                n = max(len(a), len(b))
                if not n:
                    break
                frame = np.column_stack([np.frombuffer(v.ljust(n, b"\0"), dtype="<i2") for v in (a, b)])
                wav.writeframesraw(frame.tobytes())
        for f, p in zip(self.files, self.temp):
            f.close()
            p.unlink()
