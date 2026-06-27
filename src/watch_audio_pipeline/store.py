from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import uuid

from watch_audio_pipeline.db import connect, init_db


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class JobRecord:
    id: str
    source: str
    original_filename: str
    stored_filename: str
    mime_type: str
    file_size: int
    content_hash: str
    status: str
    transcript_path: str | None
    error_message: str | None
    created_at: str
    updated_at: str


class JobStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(self.database_path)

    def _row_to_job(self, row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            source=row["source"],
            original_filename=row["original_filename"],
            stored_filename=row["stored_filename"],
            mime_type=row["mime_type"],
            file_size=row["file_size"],
            content_hash=row["content_hash"],
            status=row["status"],
            transcript_path=row["transcript_path"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def count_jobs(self) -> int:
        connection = connect(self.database_path)
        row = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        connection.close()
        return int(row["count"])

    def get_by_hash(self, content_hash: str) -> JobRecord | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM jobs WHERE content_hash = ?",
            (content_hash,),
        ).fetchone()
        connection.close()
        return self._row_to_job(row) if row else None

    def get_job(self, job_id: str) -> JobRecord | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        connection.close()
        return self._row_to_job(row) if row else None

    def create_job(
        self,
        *,
        source: str,
        original_filename: str,
        stored_filename: str,
        mime_type: str,
        file_size: int,
        content_hash: str,
    ) -> JobRecord:
        existing = self.get_by_hash(content_hash)
        if existing:
            return existing

        job_id = uuid.uuid4().hex
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                INSERT INTO jobs (
                    id, source, original_filename, stored_filename, mime_type,
                    file_size, content_hash, status, transcript_path,
                    error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    source,
                    original_filename,
                    stored_filename,
                    mime_type,
                    file_size,
                    content_hash,
                    "queued",
                    None,
                    None,
                    now,
                    now,
                ),
            )
        connection.close()
        return self.get_job(job_id)

    def claim_next_job(self, from_status: str, to_status: str) -> JobRecord | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
            (from_status,),
        ).fetchone()
        if row is None:
            connection.close()
            return None

        now = _utc_now()
        with connection:
            connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (to_status, now, row["id"]),
            )
        connection.close()
        return self.get_job(row["id"])

    def mark_transcribed(self, job_id: str, transcript_path: Path) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, transcript_path = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("transcribed", str(transcript_path), None, _utc_now(), job_id),
            )
        connection.close()
