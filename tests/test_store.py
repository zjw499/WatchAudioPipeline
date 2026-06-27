from pathlib import Path

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
