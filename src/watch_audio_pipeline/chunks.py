from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import shutil

from watch_audio_pipeline.db import connect, init_db


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class RecordingSession:
    id: str
    source: str
    original_filename: str
    status: str
    final_chunk_index: int | None
    job_id: str | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class RecordingChunk:
    session_id: str
    chunk_index: int
    stored_filename: str
    mime_type: str
    file_size: int
    content_hash: str
    status: str
    transcript_path: str | None
    language: str | None
    duration_seconds: float | None
    speaker_count: int | None
    error_message: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ChunkReceipt:
    chunk: RecordingChunk
    created: bool
    received_count: int
    final_chunk_index: int | None


class ChunkStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(self.database_path)

    @staticmethod
    def _session(row) -> RecordingSession:
        return RecordingSession(**dict(row))

    @staticmethod
    def _chunk(row) -> RecordingChunk:
        return RecordingChunk(**dict(row))

    def receive_chunk(
        self,
        *,
        session_id: str,
        chunk_index: int,
        stored_filename: str,
        original_filename: str,
        source: str,
        mime_type: str,
        file_size: int,
        content_hash: str,
        is_final: bool,
    ) -> ChunkReceipt:
        now = _utc_now()
        connection = connect(self.database_path)
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO recording_sessions (
                        id, source, original_filename, status, final_chunk_index,
                        job_id, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, 'receiving', NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (session_id, source, original_filename, now, now),
                )
                session_row = connection.execute(
                    "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session_row is None:
                    raise RuntimeError(f"failed to load recording session {session_id}")
                if session_row["source"] != source:
                    raise ValueError("recording session source does not match")
                existing_final = session_row["final_chunk_index"]
                if is_final and existing_final is not None and existing_final != chunk_index:
                    raise ValueError("recording session already has a different final chunk")
                if existing_final is not None and chunk_index > existing_final:
                    raise ValueError("chunk index is after the final chunk")

                existing = connection.execute(
                    "SELECT * FROM recording_chunks WHERE session_id = ? AND chunk_index = ?",
                    (session_id, chunk_index),
                ).fetchone()
                created = existing is None
                if existing is not None and existing["content_hash"] != content_hash:
                    raise ValueError("chunk index was already uploaded with different content")
                if session_row["status"] == "done":
                    if existing is None:
                        raise ValueError("recording session is already complete")
                    count = connection.execute(
                        "SELECT COUNT(*) AS count FROM recording_chunks WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()["count"]
                    return ChunkReceipt(
                        self._chunk(existing),
                        False,
                        int(count),
                        existing_final,
                    )
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO recording_chunks (
                            session_id, chunk_index, stored_filename, mime_type, file_size,
                            content_hash, status, transcript_path, language, duration_seconds,
                            speaker_count, error_message, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'queued', NULL, NULL, NULL, NULL, NULL, ?, ?)
                        """,
                        (
                            session_id, chunk_index, stored_filename, mime_type, file_size,
                            content_hash, now, now,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE recording_sessions
                    SET final_chunk_index = CASE WHEN ? THEN ? ELSE final_chunk_index END,
                        status = CASE WHEN ? THEN 'final_received' ELSE status END,
                        error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (is_final, chunk_index, is_final, now, session_id),
                )
                row = connection.execute(
                    "SELECT * FROM recording_chunks WHERE session_id = ? AND chunk_index = ?",
                    (session_id, chunk_index),
                ).fetchone()
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM recording_chunks WHERE session_id = ?",
                    (session_id,),
                ).fetchone()["count"]
                final_index = connection.execute(
                    "SELECT final_chunk_index FROM recording_sessions WHERE id = ?",
                    (session_id,),
                ).fetchone()["final_chunk_index"]
            if row is None:
                raise RuntimeError("failed to persist recording chunk")
            return ChunkReceipt(self._chunk(row), created, int(count), final_index)
        finally:
            connection.close()

    def claim_next_chunk(self) -> RecordingChunk | None:
        connection = connect(self.database_path)
        try:
            row = connection.execute(
                """
                SELECT * FROM recording_chunks
                WHERE status = 'queued'
                ORDER BY created_at ASC, chunk_index ASC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            with connection:
                cursor = connection.execute(
                    """
                    UPDATE recording_chunks SET status = 'transcribing', updated_at = ?
                    WHERE session_id = ? AND chunk_index = ? AND status = 'queued'
                    """,
                    (_utc_now(), row["session_id"], row["chunk_index"]),
                )
                if cursor.rowcount == 0:
                    return None
                claimed = connection.execute(
                    "SELECT * FROM recording_chunks WHERE session_id = ? AND chunk_index = ?",
                    (row["session_id"], row["chunk_index"]),
                ).fetchone()
            return self._chunk(claimed)
        finally:
            connection.close()

    def mark_transcribed(
        self,
        chunk: RecordingChunk,
        transcript_path: Path,
        *,
        language: str | None,
        duration_seconds: float | None,
        speaker_count: int | None,
    ) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_chunks
                SET status = 'transcribed', transcript_path = ?, language = ?,
                    duration_seconds = ?, speaker_count = ?, error_message = NULL,
                    updated_at = ?
                WHERE session_id = ? AND chunk_index = ?
                """,
                (
                    str(transcript_path), language, duration_seconds, speaker_count,
                    _utc_now(), chunk.session_id, chunk.chunk_index,
                ),
            )
        connection.close()

    def mark_chunk_failed(self, chunk: RecordingChunk, error_message: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_chunks SET status = 'failed', error_message = ?, updated_at = ?
                WHERE session_id = ? AND chunk_index = ?
                """,
                (error_message, _utc_now(), chunk.session_id, chunk.chunk_index),
            )
            connection.execute(
                """
                UPDATE recording_sessions SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_message, _utc_now(), chunk.session_id),
            )
        connection.close()

    def claim_ready_session(self) -> RecordingSession | None:
        connection = connect(self.database_path)
        try:
            sessions = connection.execute(
                """
                SELECT * FROM recording_sessions
                WHERE final_chunk_index IS NOT NULL AND status IN ('receiving', 'final_received')
                ORDER BY created_at ASC
                """
            ).fetchall()
            for row in sessions:
                chunk_rows = connection.execute(
                    """
                    SELECT chunk_index, status FROM recording_chunks
                    WHERE session_id = ? ORDER BY chunk_index ASC
                    """,
                    (row["id"],),
                ).fetchall()
                expected = list(range(int(row["final_chunk_index"]) + 1))
                indexes = [int(chunk["chunk_index"]) for chunk in chunk_rows]
                if indexes != expected or any(chunk["status"] != "transcribed" for chunk in chunk_rows):
                    continue
                with connection:
                    cursor = connection.execute(
                        """
                        UPDATE recording_sessions SET status = 'finalizing', updated_at = ?
                        WHERE id = ? AND status IN ('receiving', 'final_received')
                        """,
                        (_utc_now(), row["id"]),
                    )
                if cursor.rowcount:
                    updated = connection.execute(
                        "SELECT * FROM recording_sessions WHERE id = ?", (row["id"],)
                    ).fetchone()
                    return self._session(updated)
            return None
        finally:
            connection.close()

    def list_chunks(self, session_id: str) -> list[RecordingChunk]:
        connection = connect(self.database_path)
        rows = connection.execute(
            "SELECT * FROM recording_chunks WHERE session_id = ? ORDER BY chunk_index ASC",
            (session_id,),
        ).fetchall()
        connection.close()
        return [self._chunk(row) for row in rows]

    def get_session(self, session_id: str) -> RecordingSession | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        connection.close()
        return self._session(row) if row else None

    def progress(self, session_id: str) -> dict[str, int | str | None] | None:
        session = self.get_session(session_id)
        if session is None:
            return None
        chunks = self.list_chunks(session_id)
        return {
            "recording_id": session.id,
            "status": session.status,
            "received_chunks": len(chunks),
            "transcribed_chunks": sum(chunk.status == "transcribed" for chunk in chunks),
            "final_chunk_index": session.final_chunk_index,
            "job_id": session.job_id,
        }

    def retry_session(self, session_id: str) -> bool:
        session = self.get_session(session_id)
        if session is None or session.status == "done":
            return False
        now = _utc_now()
        resumed_status = "final_received" if session.final_chunk_index is not None else "receiving"
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_chunks
                SET status = 'queued', error_message = NULL, updated_at = ?
                WHERE session_id = ? AND status = 'failed'
                """,
                (now, session_id),
            )
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = ?, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (resumed_status, now, session_id),
            )
            if session.job_id is not None:
                connection.execute(
                    """
                    UPDATE jobs SET status = 'transcribed', error_message = NULL, updated_at = ?
                    WHERE id = ? AND status = 'email_failed'
                    """,
                    (now, session.job_id),
                )
        connection.close()
        return True

    def attach_job(self, session_id: str, job_id: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = 'email_queued', job_id = ?, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (job_id, _utc_now(), session_id),
            )
        connection.close()

    def mark_session_failed(self, session_id: str, error_message: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_sessions SET status = 'failed', error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_message, _utc_now(), session_id),
            )
        connection.close()

    def session_for_job(self, job_id: str) -> RecordingSession | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM recording_sessions WHERE job_id = ?", (job_id,)
        ).fetchone()
        connection.close()
        return self._session(row) if row else None

    def cleanup_completed_session(
        self,
        session_id: str,
        *,
        chunk_root: Path,
        transcript_root: Path,
    ) -> bool:
        audio_directory = chunk_root / session_id
        transcript_directory = transcript_root / session_id
        removed = audio_directory.exists()
        shutil.rmtree(audio_directory, ignore_errors=True)
        shutil.rmtree(transcript_directory, ignore_errors=True)
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_sessions SET status = 'done', error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_utc_now(), session_id),
            )
        connection.close()
        return removed
