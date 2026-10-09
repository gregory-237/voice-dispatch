"""Download models at provisioning time. Run from the repository root."""

import argparse
from pathlib import Path
from urllib.request import urlopen
from shutil import copyfileobj
from uuid import uuid4

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asr", choices=["tiny", "base", "small", "medium"], default="small")
    args = parser.parse_args()
    assets = {Path("models/silero.pt"): "https://models.silero.ai/models/tts/ru/v4_ru.pt"}
    for name in ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]:
        assets[Path("models/asr", name)] = (
            f"https://huggingface.co/Systran/faster-whisper-{args.asr}/resolve/main/{name}?download=true"
        )
    for path, url in assets.items():
        path.parent.mkdir(exist_ok=True, parents=True)
        tmp = path.with_suffix("." + uuid4().hex + ".part")
        try:
            with urlopen(url, timeout=120) as response, tmp.open("wb") as f:
                copyfileobj(response, f)
            tmp.replace(path)
            print(path)
        finally:
            tmp.unlink(missing_ok=True)
