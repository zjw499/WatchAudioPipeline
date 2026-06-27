from dataclasses import dataclass
from pathlib import Path

from watch_audio_pipeline.config import Settings


@dataclass(frozen=True)
class AppPaths:
    root: Path
    data: Path
    incoming: Path
    transcripts: Path
    failed: Path
    state: Path
    logs: Path
    database: Path


def build_paths(settings: Settings) -> AppPaths:
    root = settings.project_root.resolve()
    data = root / "data"
    state = data / "state"
    return AppPaths(
        root=root,
        data=data,
        incoming=data / "incoming",
        transcripts=data / "transcripts",
        failed=data / "failed",
        state=state,
        logs=root / "logs",
        database=state / "jobs.sqlite3",
    )


def ensure_directories(paths: AppPaths) -> AppPaths:
    for directory in (
        paths.data,
        paths.incoming,
        paths.transcripts,
        paths.failed,
        paths.state,
        paths.logs,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return paths
