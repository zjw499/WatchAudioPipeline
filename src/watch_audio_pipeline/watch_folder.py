from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import logging
import os
import time

from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.uploads import ALLOWED_EXTENSIONS, queue_local_file


watch_logger = logging.getLogger("upload")


@dataclass(frozen=True)
class WatchImportSummary:
    queued: int = 0
    duplicate: int = 0
    skipped: int = 0
    errors: int = 0


def import_ready_audio_files(
    *,
    watch_folder: Path,
    paths: AppPaths,
    store: JobStore,
    max_upload_bytes: int,
    min_age_seconds: int,
    source: str = "tailscale-taildrop",
) -> WatchImportSummary:
    if not watch_folder.exists():
        watch_logger.warning("watch folder does not exist: %s", watch_folder)
        return WatchImportSummary()

    state_path = paths.state / "watch-folder-imports.json"
    state = _load_state(state_path)
    now = time.time()
    queued = duplicate = skipped = errors = 0
    changed = False

    for file_path in sorted(watch_folder.iterdir(), key=lambda candidate: candidate.name.lower()):
        try:
            if not file_path.is_file() or file_path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue

            stat = file_path.stat()
            if now - stat.st_mtime < min_age_seconds:
                skipped += 1
                continue

            fingerprint = _fingerprint(file_path, stat)
            if fingerprint in state:
                skipped += 1
                continue

            result = queue_local_file(
                file_path=file_path,
                source=source,
                paths=paths,
                store=store,
                max_upload_bytes=max_upload_bytes,
            )
            if result.created:
                queued += 1
            else:
                duplicate += 1

            state[fingerprint] = {
                "job_id": result.job.id,
                "stored_filename": result.job.stored_filename,
                "created": result.created,
                "source_path": str(file_path),
            }
            changed = True
            watch_logger.info(
                "watch folder imported source=%s stored_filename=%s created=%s",
                file_path,
                result.job.stored_filename,
                result.created,
            )
        except Exception:
            errors += 1
            watch_logger.exception("watch folder import failed source=%s", file_path)

    if changed:
        _save_state(state_path, state)

    return WatchImportSummary(
        queued=queued,
        duplicate=duplicate,
        skipped=skipped,
        errors=errors,
    )


def _fingerprint(file_path: Path, stat: os.stat_result) -> str:
    return f"{file_path.resolve(strict=False)}|{stat.st_size}|{stat.st_mtime_ns}"


def _load_state(state_path: Path) -> dict[str, dict[str, object]]:
    if not state_path.exists():
        return {}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        watch_logger.exception("failed to load watch folder state: %s", state_path)
        return {}


def _save_state(state_path: Path, state: dict[str, dict[str, object]]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = state_path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp_path, state_path)
