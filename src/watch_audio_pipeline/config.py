from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_prefix="WATCH_AUDIO_",
        extra="ignore",
    )

    project_root: Path = Field(default_factory=lambda: REPO_ROOT)
    upload_token: str = "replace-me"
    max_upload_bytes: int = 25 * 1024 * 1024
