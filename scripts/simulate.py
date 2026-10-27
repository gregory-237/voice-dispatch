"""Replay PCM WAV over AudioSocket and drain returned audio concurrently."""

import argparse
import asyncio
import struct
import time
import wave
from pathlib import Path
from uuid import uuid4


async def simulate(host, port, audio, pause):
    uid = uuid4()
    reader, writer = await asyncio.open_connection(host, port)
    writer.write(struct.pack("!BH", 1, 16) + uid.bytes)

    async def drain():
        size = 0
        try:
            while True:
                kind, length = struct.unpack("!BH", await reader.readexactly(3))
                await reader.readexactly(length)
                if kind == 0:
                    break
                size += length
        except asyncio.IncompleteReadError:
            pass
        return size

    reply = asyncio.create_task(drain())
    started = time.perf_counter()
    try:
        for data in [b"\0" * int(16000 * pause), audio, b"\0" * 16000 * 4]:
            for i in range(0, len(data), 320):
                if reply.done():
                    break
                frame = data[i : i + 320]
                writer.write(struct.pack("!BH", 16, len(frame)) + frame)
                await writer.drain()
                await asyncio.sleep(len(frame) / 16000)
        writer.write(b"\0\0\0")
        await writer.drain()
        received = await asyncio.wait_for(reply, 15)
    finally:
        writer.close()
        await writer.wait_closed()
        reply.cancel()
    return {"call_id": str(uid), "seconds": round(time.perf_counter() - started, 2), "rx_pcm_bytes": received}


def read_wav(path):
    with wave.open(str(path), "rb") as w:
        if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (1, 2, 8000):
            raise ValueError("Требуется WAV PCM16 mono 8000")
        return w.readframes(w.getnframes())


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("wav", type=Path)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9092)
    p.add_argument("--load", action="store_true")
    p.add_argument("--pause", type=float, default=2.0)
    args = p.parse_args()
    audio = read_wav(args.wav)
    for count in [1, 4, 8, 12] if args.load else [1]:
        print("sessions", count, flush=True)
        for result in await asyncio.gather(
            *(simulate(args.host, args.port, audio, args.pause) for _ in range(count)), return_exceptions=True
        ):
            print(result, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
