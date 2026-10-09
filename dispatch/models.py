"""Local inference. Native libraries are loaded only on voice traffic."""

import asyncio
import hashlib
import json
import threading
from collections import OrderedDict, deque
from functools import lru_cache
from importlib.metadata import distribution
from pathlib import Path
from typing import Literal

import httpx
import numpy as np
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from .config import cfg
from .domain import Category


class Extraction(BaseModel):
    category: Category | None = None
    phone: str | None = Field(default=None, max_length=30)


class VoiceOptions(BaseModel):
    llm_model: str = Field(default=cfg.llm_model, pattern=r"^[a-zA-Z0-9_./:-]{1,100}$")
    speaker: Literal["xenia", "kseniya", "baya", "aidar", "eugene"] = "xenia"
    silence_ms: int = Field(default=640, ge=256, le=1600)


@lru_cache(maxsize=1)
def vad_model():
    import onnxruntime as ort

    path = distribution("silero-vad").locate_file("silero_vad/data/silero_vad.onnx")
    options = ort.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class Segmenter:
    def __init__(self, model, silence_ms):
        self.model = model
        self.state = np.zeros((2, 1, 128), dtype="float32")
        self.context = np.zeros((1, 32), dtype="float32")
        self.pre = deque(maxlen=6)
        self.frames = []
        self.quiet = 0
        self.tail = max(1, silence_ms // 32)
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= 512:
            frame = bytes(self.buffer[:512])
            del self.buffer[:512]
            samples = np.frombuffer(frame, dtype="<i2").astype("float32")[None, :] / 32768
            x = np.concatenate([self.context, samples], axis=1)
            prob, self.state = self.model.run(
                None, {"input": x, "state": self.state, "sr": np.array(8000, dtype="int64")}
            )
            self.context = samples[:, -32:]
            speech = prob.item() >= 0.50
            if not self.frames:
                self.pre.append(frame)
                if speech:
                    self.frames = list(self.pre)
                    self.pre.clear()
                    self.quiet = 0
                    yield "start", b""
            else:
                self.frames.append(frame)
                self.quiet = 0 if speech else self.quiet + 1
                if self.quiet >= self.tail or len(self.frames) >= 625:
                    utterance = b"".join(self.frames)
                    self.frames = []
                    yield "end", utterance


class Models:
    def __init__(self):
        self.asr = self.tts = None
        self.asr_lock = threading.Lock()
        self.tts_lock = threading.Lock()
        self.cache = OrderedDict()
        self.redis = (
            Redis.from_url(cfg.redis_url, socket_connect_timeout=0.5, socket_timeout=0.6)
            if cfg.redis_url
            else None
        )

    def transcribe(self, pcm):
        from faster_whisper import WhisperModel

        with self.asr_lock:
            if self.asr is None:
                self.asr = WhisperModel(
                    cfg.asr_path, device="cpu", compute_type="int8", cpu_threads=4, local_files_only=True
                )
            x = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
            audio = np.interp(np.arange(2 * len(x)) / 2, np.arange(len(x)), x).astype("float32")
            segments, _ = self.asr.transcribe(
                audio, language="ru", beam_size=3, condition_on_previous_text=False
            )
            return " ".join(s.text.strip() for s in segments).strip()

    def synthesize(self, text, speaker):
        import torch

        with self.tts_lock, torch.inference_mode():
            if self.tts is None:
                torch.set_num_threads(2)
                self.tts = torch.package.PackageImporter(str(Path(cfg.tts_path))).load_pickle(
                    "tts_models", "model"
                )
                self.tts.to(torch.device("cpu"))
            samples = self.tts.apply_tts(text=text, speaker=speaker, sample_rate=8000)
            return (samples.detach().cpu().numpy().clip(-1, 1) * 32767).astype("<i2").tobytes()

    async def speech(self, text, speaker, cacheable):
        # Cache only fixed prompts. Resident data never enters the shared cache.
        key = "vd:tts:v4:" + hashlib.sha256((speaker + "|" + text).encode()).hexdigest()
        if cacheable and key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key], True
        if cacheable and self.redis:
            try:
                value = await self.redis.get(key)
                if value:
                    return value, True
            except RedisError:
                pass
        value = await asyncio.to_thread(self.synthesize, text, speaker)
        if cacheable:
            self.cache[key] = value
            if len(self.cache) > 96:
                self.cache.popitem(last=False)
            if self.redis:
                try:
                    await self.redis.setex(key, 86400, value)
                except RedisError:
                    pass
        return value, False

    async def extract(self, text, state, model):
        prompt = (
            "Извлеки категорию ЖКХ или номер телефона цифрами из русской речи. "
            "Если значение не названо, верни null. Категории water, heat, power, lift, common, other."
        )
        body = {
            "model": model,
            "stream": True,
            "format": Extraction.model_json_schema(),
            "messages": [
                {"role": "system", "content": prompt},
                {"role": "user", "content": json.dumps({"поле": state, "реплика": text}, ensure_ascii=False)},
            ],
            "options": {"temperature": 0, "num_predict": 100, "num_ctx": 2048},
        }
        async with (
            httpx.AsyncClient(timeout=30) as client,
            client.stream("POST", cfg.ollama_url + "/api/chat", json=body) as res,
        ):
            res.raise_for_status()
            chunks = []
            async for line in res.aiter_lines():
                item = json.loads(line)
                if "error" in item:
                    raise ValueError("Ошибка Ollama")
                chunks.append(item.get("message", {}).get("content", ""))
                if item.get("done"):
                    return Extraction.model_validate_json("".join(chunks)).model_dump(exclude_none=True)
        raise ValueError("Прерван поток Ollama")


models = Models()
