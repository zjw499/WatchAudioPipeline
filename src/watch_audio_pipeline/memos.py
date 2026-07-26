from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from pathlib import Path

from watch_audio_pipeline.db import connect, init_db
from watch_audio_pipeline.store import JobRecord


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


DEFAULT_PREFERENCES = {
    "language": "English",
    "speaker_labels_enabled": True,
    "auto_paragraphs": True,
    "prefer_numbers": False,
    "generate_title": True,
    "send_email": True,
    "recipient": "",
    "add_emoji": False,
    "email_prefix": "",
    "remove_footer": False,
    "empty_subject": False,
    "private_mode": False,
    "summary_enabled": True,
    "auto_email_summary": False,
    "summary_template": "default",
}


@dataclass(frozen=True)
class MemoRecord:
    id: str
    job_id: str
    title: str
    summary: str | None
    transcript_path: str
    original_filename: str
    source: str
    duration_seconds: float | None
    language: str | None
    speaker_count: int | None
    status: str
    created_at: str
    updated_at: str
    audio_deleted_at: str | None
    email_sent_at: str | None
    error_message: str | None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["transcript"] = None
        return result


def _row_to_memo(row) -> MemoRecord:
    return MemoRecord(
        id=row["id"],
        job_id=row["job_id"],
        title=row["title"],
        summary=row["summary"],
        transcript_path=row["transcript_path"],
        original_filename=row["original_filename"],
        source=row["source"],
        duration_seconds=row["duration_seconds"],
        language=row["language"],
        speaker_count=row["speaker_count"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        audio_deleted_at=row["audio_deleted_at"],
        email_sent_at=row["email_sent_at"],
        error_message=row["error_message"],
    )


class MemoStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(self.database_path)

    def upsert_from_job(
        self,
        job: JobRecord,
        transcript_path: Path,
        *,
        title: str,
        summary: str | None = None,
        duration_seconds: float | None = None,
        language: str | None = None,
        speaker_count: int | None = None,
    ) -> MemoRecord:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                INSERT INTO memos (
                    id, job_id, title, summary, transcript_path, original_filename,
                    source, duration_seconds, language, speaker_count, status,
                    created_at, updated_at, audio_deleted_at, email_sent_at, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    transcript_path = excluded.transcript_path,
                    duration_seconds = excluded.duration_seconds,
                    language = excluded.language,
                    speaker_count = excluded.speaker_count,
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    error_message = NULL
                """,
                (
                    job.id,
                    job.id,
                    title,
                    summary,
                    str(transcript_path),
                    job.original_filename,
                    job.source,
                    duration_seconds,
                    language,
                    speaker_count,
                    "transcribed",
                    job.created_at,
                    now,
                    None,
                    None,
                    None,
                ),
            )
            row = connection.execute("SELECT * FROM memos WHERE job_id = ?", (job.id,)).fetchone()
        connection.close()
        if row is None:
            raise RuntimeError(f"failed to persist memo for job {job.id}")
        return _row_to_memo(row)

    def get(self, memo_id: str, client_id: str | None = None) -> MemoRecord | None:
        connection = connect(self.database_path)
        if client_id is None:
            row = connection.execute("SELECT * FROM memos WHERE id = ?", (memo_id,)).fetchone()
        else:
            row = connection.execute(
                """
                SELECT memos.* FROM memos
                JOIN jobs ON jobs.id = memos.job_id
                WHERE memos.id = ? AND jobs.client_id = ?
                """,
                (memo_id, client_id),
            ).fetchone()
        connection.close()
        return _row_to_memo(row) if row else None

    def list(
        self,
        query: str = "",
        limit: int = 100,
        client_id: str | None = None,
    ) -> list[MemoRecord]:
        connection = connect(self.database_path)
        query = query.strip()
        owner_clause = ""
        owner_parameters: tuple[str, ...] = ()
        if client_id is not None:
            owner_clause = "JOIN jobs ON jobs.id = memos.job_id WHERE jobs.client_id = ?"
            owner_parameters = (client_id,)
        if query:
            where_clause = (
                "WHERE memos.title LIKE ? OR memos.original_filename LIKE ? OR memos.summary LIKE ?"
                if client_id is None
                else "AND (memos.title LIKE ? OR memos.original_filename LIKE ? OR memos.summary LIKE ?)"
            )
            rows = connection.execute(
                f"SELECT memos.* FROM memos {owner_clause} {where_clause} ORDER BY memos.created_at DESC LIMIT ?",
                owner_parameters + (f"%{query}%", f"%{query}%", f"%{query}%", max(1, min(limit, 200))),
            ).fetchall()
        else:
            rows = connection.execute(
                f"SELECT memos.* FROM memos {owner_clause} ORDER BY memos.created_at DESC LIMIT ?",
                owner_parameters + (max(1, min(limit, 200)),),
            ).fetchall()
        connection.close()
        return [_row_to_memo(row) for row in rows]

    def update_status(self, memo_id: str, status: str, error_message: str | None = None) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                "UPDATE memos SET status = ?, error_message = ?, updated_at = ? WHERE id = ?",
                (status, error_message, _utc_now(), memo_id),
            )
        connection.close()

    def mark_email_sent(self, memo_id: str, audio_deleted: bool) -> None:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE memos
                SET status = 'done', email_sent_at = ?,
                    audio_deleted_at = CASE WHEN ? THEN ? ELSE audio_deleted_at END,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, audio_deleted, now, now, memo_id),
            )
        connection.close()

    def retry(self, memo_id: str, client_id: str | None = None) -> bool:
        connection = connect(self.database_path)
        with connection:
            if client_id is None:
                row = connection.execute(
                    "SELECT job_id, status FROM memos WHERE id = ?", (memo_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT memos.job_id, memos.status FROM memos
                    JOIN jobs ON jobs.id = memos.job_id
                    WHERE memos.id = ? AND jobs.client_id = ?
                    """,
                    (memo_id, client_id),
                ).fetchone()
            if row is None or row["status"] not in {"failed", "email_failed"}:
                return False
            now = _utc_now()
            connection.execute(
                "UPDATE jobs SET status = 'queued', error_message = NULL, updated_at = ? WHERE id = ?",
                (now, row["job_id"]),
            )
            connection.execute(
                "UPDATE memos SET status = 'queued', error_message = NULL, updated_at = ? WHERE id = ?",
                (now, memo_id),
            )
        connection.close()
        return True

    def delete(self, memo_id: str, client_id: str | None = None) -> MemoRecord | None:
        memo = self.get(memo_id, client_id)
        if memo is None:
            return None
        connection = connect(self.database_path)
        with connection:
            connection.execute("DELETE FROM memos WHERE id = ?", (memo_id,))
            connection.execute("DELETE FROM jobs WHERE id = ?", (memo.job_id,))
        connection.close()
        return memo

    @staticmethod
    def _preference_id(client_id: str | None) -> str:
        # Keep older shortcuts and jobs on the pre-client-isolation preference record.
        return "default" if client_id in {None, "", "legacy"} else client_id

    def get_preferences(self, client_id: str | None = None) -> dict:
        preference_id = self._preference_id(client_id)
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT value_json FROM app_preferences WHERE id = ?", (preference_id,)
        ).fetchone()
        connection.close()
        if row is None:
            return dict(DEFAULT_PREFERENCES)
        try:
            stored = json.loads(row["value_json"])
        except json.JSONDecodeError:
            stored = {}
        return {**DEFAULT_PREFERENCES, **stored}

    def update_preferences(self, updates: dict, client_id: str | None = None) -> dict:
        preference_id = self._preference_id(client_id)
        preferences = {**self.get_preferences(client_id), **updates}
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                INSERT INTO app_preferences (id, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (preference_id, json.dumps(preferences, sort_keys=True), _utc_now()),
            )
        connection.close()
        return preferences
