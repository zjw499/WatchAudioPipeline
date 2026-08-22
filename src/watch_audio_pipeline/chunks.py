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
    client_id: str
    original_filename: str
    recipient: str | None
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

    @staticmethod
    def _restore_completed_transcript(connection, session_row) -> None:
        """Rehydrate a cleaned session before accepting late chunks.

        Completed sessions retain the full transcript on the job while their
        per-chunk transcript files are deleted. Reuse that full transcript as
        the completed prefix so a late Watch retry can append new chunks.
        """
        job_id = session_row["job_id"]
        final_chunk_index = session_row["final_chunk_index"]
        if job_id is None or final_chunk_index is None:
            return

        chunk_rows = connection.execute(
            """
            SELECT chunk_index, transcript_path FROM recording_chunks
            WHERE session_id = ? AND chunk_index <= ? ORDER BY chunk_index ASC
            """,
            (session_row["id"], final_chunk_index),
        ).fetchall()
        if not chunk_rows or all(
            chunk["transcript_path"] and Path(chunk["transcript_path"]).exists()
            for chunk in chunk_rows
        ):
            return

        job_row = connection.execute(
            "SELECT transcript_path FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job_row is None or not job_row["transcript_path"]:
            return
        aggregate_path = Path(job_row["transcript_path"])
        if not aggregate_path.exists():
            return

        empty_path = aggregate_path.with_name(f"{job_id}.recovered-empty.txt")
        empty_path.write_text("", encoding="utf-8")
        for offset, chunk in enumerate(chunk_rows):
            connection.execute(
                """
                UPDATE recording_chunks
                SET transcript_path = ?, status = 'transcribed',
                    error_message = NULL, updated_at = ?
                WHERE session_id = ? AND chunk_index = ?
                """,
                (
                    str(aggregate_path if offset == 0 else empty_path),
                    _utc_now(),
                    session_row["id"],
                    chunk["chunk_index"],
                ),
            )

    def receive_chunk(
        self,
        *,
        session_id: str,
        chunk_index: int,
        stored_filename: str,
        original_filename: str,
        source: str,
        client_id: str,
        recipient: str | None,
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
                        id, source, client_id, original_filename, recipient, status, final_chunk_index,
                        job_id, error_message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'receiving', NULL, NULL, NULL, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (session_id, source, client_id, original_filename, recipient, now, now),
                )
                session_row = connection.execute(
                    "SELECT * FROM recording_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session_row is None:
                    raise RuntimeError(f"failed to load recording session {session_id}")
                if session_row["source"] != source:
                    raise ValueError("recording session source does not match")
                if session_row["client_id"] != client_id:
                    raise ValueError("recording session client_id does not match")
                if recipient is not None and session_row["recipient"] != recipient:
                    connection.execute(
                        """
                        UPDATE recording_sessions
                        SET recipient = ?, updated_at = ? WHERE id = ?
                        """,
                        (recipient, now, session_id),
                    )
                existing_final = session_row["final_chunk_index"]
                existing = connection.execute(
                    "SELECT * FROM recording_chunks WHERE session_id = ? AND chunk_index = ?",
                    (session_id, chunk_index),
                ).fetchone()
                created = existing is None
                if existing is not None and existing["content_hash"] != content_hash:
                    raise ValueError("chunk index was already uploaded with different content")

                session_status = session_row["status"]
                if session_status == "done" and existing is not None:
                    count = connection.execute(
                        "SELECT COUNT(*) AS count FROM recording_chunks WHERE session_id = ?",
                        (session_id,),
                    ).fetchone()["count"]
                    if existing_final is None or chunk_index <= existing_final:
                        return ChunkReceipt(self._chunk(existing), False, int(count), existing_final)

                if session_status == "done":
                    # A late retry can arrive after an old final marker closed
                    # the session. Reopen it so the client can repair the
                    # recording instead of receiving a permanent 400.
                    self._restore_completed_transcript(connection, session_row)
                    connection.execute(
                        """
                        UPDATE recording_sessions
                        SET status = 'receiving', final_chunk_index = NULL,
                            error_message = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (now, session_id),
                    )
                    existing_final = None
                    session_status = "receiving"

                accepts_final = is_final and (
                    existing_final is None or chunk_index >= existing_final
                )
                clears_final = (
                    existing_final is not None
                    and chunk_index > existing_final
                    and not is_final
                )
                next_final_index = (
                    chunk_index
                    if accepts_final
                    else None
                    if clears_final
                    else existing_final
                )
                next_session_status = (
                    "final_received"
                    if accepts_final
                    else "receiving"
                    if clears_final
                    else session_status
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
                elif existing["transcript_path"] is None or not Path(
                    existing["transcript_path"]
                ).exists():
                    # Completed sessions remove their audio/transcript files.
                    # If a phone retry supplies that chunk again, point the
                    # database row at the new file and transcribe it again.
                    connection.execute(
                        """
                        UPDATE recording_chunks
                        SET stored_filename = ?, status = 'queued',
                            transcript_path = NULL, language = NULL,
                            duration_seconds = NULL, speaker_count = NULL,
                            error_message = NULL, updated_at = ?
                        WHERE session_id = ? AND chunk_index = ?
                        """,
                        (stored_filename, now, session_id, chunk_index),
                    )
                connection.execute(
                    """
                    UPDATE recording_sessions
                    SET final_chunk_index = ?, status = ?,
                        error_message = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_final_index, next_session_status, now, session_id),
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

    def requeue_chunk(self, chunk: RecordingChunk, error_message: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE recording_chunks
                SET status = 'queued', error_message = ?, updated_at = ?
                WHERE session_id = ? AND chunk_index = ?
                """,
                (error_message, _utc_now(), chunk.session_id, chunk.chunk_index),
            )
            connection.execute(
                """
                UPDATE recording_sessions
                SET status = CASE
                        WHEN final_chunk_index IS NULL THEN 'receiving'
                        ELSE 'final_received'
                    END,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (_utc_now(), chunk.session_id),
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

    def progress(
        self,
        session_id: str,
        client_id: str | None = None,
    ) -> dict[str, int | str | None] | None:
        session = self.get_session(session_id)
        if session is None or (client_id is not None and session.client_id != client_id):
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

    def retry_session(self, session_id: str, client_id: str | None = None) -> bool:
        session = self.get_session(session_id)
        if (
            session is None
            or session.status == "done"
            or (client_id is not None and session.client_id != client_id)
        ):
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
