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
    upload_token: str = "replace-me"
