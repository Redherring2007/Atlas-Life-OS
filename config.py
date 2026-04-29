import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    database_url: str
    openai_api_key: str | None
    whisper_model_size: str
    whisper_language: str | None
    reminder_check_seconds: int
    local_timezone: str


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_config() -> Config:
    return Config(
        telegram_bot_token=_required("TELEGRAM_BOT_TOKEN"),
        database_url=_required("DATABASE_URL"),
        openai_api_key=os.getenv("OPENAI_API_KEY") or None,
        whisper_model_size=os.getenv("WHISPER_MODEL_SIZE", "small"),
        whisper_language=os.getenv("WHISPER_LANGUAGE", "en") or None,
        reminder_check_seconds=int(os.getenv("REMINDER_CHECK_SECONDS", "60")),
        local_timezone=os.getenv("LOCAL_TIMEZONE", "Asia/Dubai"),
    )


config = load_config()
