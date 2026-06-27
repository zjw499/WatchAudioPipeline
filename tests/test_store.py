import sqlite3
from pathlib import Path

import pytest

import watch_audio_pipeline.store as store_module
from watch_audio_pipeline.store import JobStore


def test_create_job_prevents_duplicate_hash(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")

    first = store.create_job(
        source="iphone-shortcuts",
        original_filename="note.m4a",
        stored_filename="abc123.m4a",
        mime_type="audio/mp4",
        file_size=128,
        content_hash="abc123",
    )
    duplicate = store.create_job(
        source="iphone-shortcuts",
        original_filename="note-copy.m4a",
        stored_filename="abc123.m4a",
        mime_type="audio/mp4",
        file_size=128,
        content_hash="abc123",
    )

    assert duplicate.id == first.id
    assert store.count_jobs() == 1


def test_claim_and_mark_transcribed(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=256,
        content_hash="hash001",
    )

    claimed = store.claim_next_job("queued", "transcribing")

    assert claimed is not None
    assert claimed.id == job.id

    transcript_path = tmp_path / "transcripts" / f"{job.id}.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("Transcript body", encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)

    saved = store.get_job(job.id)
    assert saved is not None
    assert saved.status == "transcribed"
    assert saved.transcript_path == str(transcript_path)


def test_create_job_returns_existing_job_when_insert_races(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    original_connect = store_module.connect
    race_inserted = False

    class RaceConnection:
        def __init__(self, inner: sqlite3.Connection):
            self._inner = inner

        def execute(self, sql: str, parameters=()):
            nonlocal race_inserted
            normalized_sql = " ".join(sql.split()).upper()
            if (
                not race_inserted
                and normalized_sql.startswith("INSERT INTO JOBS")
                and parameters[6] == "race-hash"
            ):
                race_inserted = True
                competing = original_connect(store.database_path)
                with competing:
                    competing.execute(
                        """
                        INSERT INTO jobs (
                            id, source, original_filename, stored_filename, mime_type,
                            file_size, content_hash, status, transcript_path,
                            error_message, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "existing-job",
                            "iphone-shortcuts",
                            "already-there.m4a",
                            "existing.m4a",
                            "audio/mp4",
                            64,
                            "race-hash",
                            "queued",
                            None,
                            None,
                            "2026-01-01T00:00:00+00:00",
                            "2026-01-01T00:00:00+00:00",
                        ),
                    )
                competing.close()
            return self._inner.execute(sql, parameters)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def race_connect(database_path: Path) -> sqlite3.Connection:
        return RaceConnection(original_connect(database_path))

    monkeypatch.setattr(store_module, "connect", race_connect)

    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="note.m4a",
        stored_filename="race.m4a",
        mime_type="audio/mp4",
        file_size=128,
        content_hash="race-hash",
    )

    assert job.id == "existing-job"
    assert store.count_jobs() == 1


def test_claim_next_job_returns_none_when_queue_empty(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")

    claimed = store.claim_next_job("queued", "transcribing")

    assert claimed is None


def test_claim_next_job_does_not_steal_already_claimed_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    original_connect = store_module.connect
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=256,
        content_hash="hash001",
    )
    race_triggered = False

    class RaceConnection:
        def __init__(self, inner: sqlite3.Connection):
            self._inner = inner

        def execute(self, sql: str, parameters=()):
            nonlocal race_triggered
            normalized_sql = " ".join(sql.split()).upper()
            if (
                not race_triggered
                and normalized_sql.startswith("UPDATE JOBS SET STATUS = ?")
                and parameters[2] == job.id
            ):
                race_triggered = True
                competing = original_connect(store.database_path)
                with competing:
                    competing.execute(
                        "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                        ("transcribing", "2026-01-01T00:00:00+00:00", job.id),
                    )
                competing.close()
            return self._inner.execute(sql, parameters)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def close(self) -> None:
            self._inner.close()

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def race_connect(database_path: Path) -> sqlite3.Connection:
        return RaceConnection(original_connect(database_path))

    monkeypatch.setattr(store_module, "connect", race_connect)

    claimed = store.claim_next_job("queued", "transcribing")

    assert claimed is None


def test_getters_return_saved_jobs_by_hash_and_id(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.sqlite3")
    created = store.create_job(
        source="iphone-shortcuts",
        original_filename="note.m4a",
        stored_filename="abc123.m4a",
        mime_type="audio/mp4",
        file_size=128,
        content_hash="abc123",
    )

    by_hash = store.get_by_hash("abc123")
    by_id = store.get_job(created.id)

    assert by_hash == created
    assert by_id == created
    assert store.get_by_hash("missing") is None
    assert store.get_job("missing") is None
