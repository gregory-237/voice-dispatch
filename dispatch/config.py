from pathlib import Path
from zoneinfo import ZoneInfo
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite+aiosqlite:///./var/dispatch.db"
    postgres_password: str = ""
    admin_password: str
    session_secret: str
    api_token: str
    webhook_secret: str
    webhook_url: str = ""
    redis_url: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen2.5:3b"
    asr_path: str = "models/asr"
    tts_path: str = "models/silero.pt"
    ami_host: str = "127.0.0.1"
    ami_port: int = 5038
    ami_user: str = "voicedispatch"
    ami_password: str = ""
    sip_trunk: str = "carrier"
    timezone: str = "Europe/Samara"
    audio_host: str = "0.0.0.0"
    audio_port: int = 9092
    audio_enabled: bool = True
    worker_enabled: bool = True
    secure_cookie: bool = False
    records: Path = Path("var/audio")


cfg = Settings()
tz = ZoneInfo(cfg.timezone)
cfg.records.mkdir(parents=True, exist_ok=True)
