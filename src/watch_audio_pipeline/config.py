from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="WATCH_AUDIO_",
        extra="ignore",
    )

    project_root: Path = Field(default_factory=Path.cwd)
    host: str = "0.0.0.0"
    port: int = 8787
    data_dir_name: str = "data"
    upload_token: str = "replace-me"
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "watch-audio@example.com"
    smtp_to: str = "you@example.com"
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    worker_poll_seconds: int = 10
    max_upload_bytes: int = 300_000_000


def load_settings() -> Settings:
    return Settings()
