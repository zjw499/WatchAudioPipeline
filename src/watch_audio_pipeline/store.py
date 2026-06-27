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

    def _get_job_by_query(
        self,
        connection,
        query: str,
        parameters: tuple[str, ...],
    ) -> JobRecord | None:
        row = connection.execute(query, parameters).fetchone()
        return self._row_to_job(row) if row else None

    def count_jobs(self) -> int:
        connection = connect(self.database_path)
        row = connection.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        connection.close()
        return int(row["count"])

    def get_by_hash(self, content_hash: str) -> JobRecord | None:
        connection = connect(self.database_path)
        job = self._get_job_by_query(
            connection,
            "SELECT * FROM jobs WHERE content_hash = ?",
            (content_hash,),
        )
        connection.close()
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        connection = connect(self.database_path)
        job = self._get_job_by_query(
            connection,
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        )
        connection.close()
        return job

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
                ON CONFLICT(content_hash) DO NOTHING
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
            job = self._get_job_by_query(
                connection,
                "SELECT * FROM jobs WHERE content_hash = ?",
                (content_hash,),
            )
        connection.close()
        if job is None:
            raise RuntimeError(f"failed to load job for content hash {content_hash}")
        return job

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
        job = None
        with connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (to_status, now, row["id"], from_status),
            )
            if cursor.rowcount != 0:
                job = self._get_job_by_query(
                    connection,
                    "SELECT * FROM jobs WHERE id = ?",
                    (row["id"],),
                )
        connection.close()
        return job

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

    def mark_failed(self, job_id: str, error_message: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("failed", error_message, _utc_now(), job_id),
            )
        connection.close()

    def mark_done(self, job_id: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("done", None, _utc_now(), job_id),
            )
        connection.close()

    def mark_email_failed(self, job_id: str, error_message: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                ("email_failed", error_message, _utc_now(), job_id),
            )
        connection.close()
