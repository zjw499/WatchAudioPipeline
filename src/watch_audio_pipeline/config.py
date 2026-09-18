import os
from datetime import datetime
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = Path(os.environ.get("WATCH_AUDIO_ENV_FILE", REPO_ROOT / ".env"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_prefix="WATCH_AUDIO_",
        env_ignore_empty=True,
        extra="ignore",
    )

    project_root: Path = Field(default_factory=lambda: REPO_ROOT)
    host: str = "0.0.0.0"
    port: int = 8787
    ssl_certfile: Path | None = None
    ssl_keyfile: Path | None = None
    basic_auth_username: str = "watch-audio"
    basic_auth_password: str = "replace-me"
    max_upload_bytes: int = 25 * 1024 * 1024
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "watch-audio@example.com"
    smtp_to: str = "you@example.com"
    transcription_provider: str = "groq"
    groq_api_key: str = ""
    groq_api_key_file: Path | None = None
    groq_model: str = "whisper-large-v3-turbo"
    groq_language: str = "en"
    groq_timeout_seconds: int = 120
    groq_max_retries: int = 4
    whisper_model: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"
    whisper_cpu_threads: int = 8
    whisper_num_workers: int = 1
    diarization_enabled: bool = False
    diarization_model: str = "pyannote/speaker-diarization-community-1"
    diarization_token: str = ""
    diarization_token_file: Path | None = None
    diarization_device: str = "cpu"
    diarization_min_speakers: int | None = None
    diarization_max_speakers: int | None = None
    ollama_enabled: bool = False
    ollama_host: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_timeout_seconds: int = 120
    ollama_max_transcript_chars: int = 80000
    notion_enabled: bool = False
    notion_transcribe_audio: bool = False
    notion_live_preview_enabled: bool = False
    notion_live_client_ids: list[str] = Field(default_factory=list)
    notion_live_start_after: datetime | None = None
    notion_report_client_ids: list[str] = Field(default_factory=list)
    notion_report_instructions_url: str = ""
    notion_client_ids: list[str] = Field(default_factory=list)
    notion_api_base: str = "https://api.notion.com/v1"
    notion_api_version: str = "2026-03-11"
    notion_token: str = ""
    notion_token_file: Path | None = None
    notion_data_source_id: str = ""
    notion_database_url: str = ""
    notion_timeout_seconds: int = 45
    notion_retry_base_seconds: int = 30
    notion_retry_max_seconds: int = 30 * 60
    email_enabled: bool = True
    watch_folder_enabled: bool = False
    watch_folder: Path = Field(default_factory=lambda: Path.home() / "Downloads")
    watch_folder_min_age_seconds: int = 10
    worker_poll_seconds: int = 10
    stream_batch_chunks: int = 8
    stream_overlap_seconds: float = 2.0
    stream_silence_max_db: float = -50.0
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    worker_lock_name: str = "worker.lock"
    gemini_enabled: bool = False
    gemini_gem_url: str = ""
    gemini_profile_dir: Path = Field(
        default_factory=lambda: Path.home() / ".config" / "watch-audio" / "gemini-chrome-profile"
    )
    gemini_chrome_channel: str = "chrome"
    gemini_headless: bool = True
    gemini_timeout_seconds: int = 90
    gemini_poll_seconds: int = 15
    gemini_min_submission_interval_seconds: int = 120
    gemini_max_retries: int = 5
    gemini_retry_base_seconds: int = 30
    gemini_challenge_initial_cooldown_seconds: int = 30 * 60
    gemini_challenge_second_cooldown_seconds: int = 2 * 60 * 60
    gemini_challenge_max_cooldown_seconds: int = 8 * 60 * 60
    gemini_challenge_reset_seconds: int = 24 * 60 * 60
    gemini_auto_open_verification: bool = False
    ntfy_enabled: bool = False
    ntfy_url: str = ""
    ntfy_timeout_seconds: int = 10
    server_version: str = "development"

    def uses_notion(self, client_id: str) -> bool:
        return self.notion_enabled and (
            "*" in self.notion_client_ids or client_id in self.notion_client_ids
        )

    def uses_native_notion(self, client_id: str) -> bool:
        return self.notion_transcribe_audio and self.uses_notion(client_id)

    def uses_live_notion(self, client_id: str) -> bool:
        # Live delivery is explicitly enrolled, never inherited by other testers.
        return bool(self.notion_live_preview_enabled and self.notion_live_start_after
                    and self.notion_live_start_after.tzinfo
                    and client_id in self.notion_live_client_ids
                    and self.uses_native_notion(client_id))

    def uses_notion_report(self, client_id: str) -> bool:
        # Narrative instructions and retained cloud audio are explicit opt-ins.
        return client_id in self.notion_report_client_ids and self.uses_native_notion(client_id)

    @property
    def native_notion_clients(self) -> tuple[str, ...]:
        return tuple(self.notion_client_ids) if self.notion_enabled and self.notion_transcribe_audio else ()


def load_settings() -> Settings:
    return Settings()
