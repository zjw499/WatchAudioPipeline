from dataclasses import dataclass

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
import watch_audio_pipeline.worker as worker_module
from watch_audio_pipeline.worker import process_next_transcription_job


@dataclass
class FakeTranscriptResult:
    text: str
    language: str | None = None
    duration_seconds: float | None = None


class FakeTranscriber:
    def transcribe(self, audio_path):
        return FakeTranscriptResult(text=f"Transcript for {audio_path.name}")


class ExplodingTranscriber:
    def transcribe(self, audio_path):
        raise RuntimeError(f"transcription failed for {audio_path.name}")


def test_process_next_transcription_job_writes_transcript(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="note.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001",
    )
    (paths.incoming / job.stored_filename).write_bytes(b"audio")

    processed_job_id = process_next_transcription_job(
        store=store,
        paths=paths,
        transcriber=FakeTranscriber(),
    )

    saved = store.get_job(job.id)
    transcript_path = paths.transcripts / f"{job.id}.txt"

    assert processed_job_id == job.id
    assert saved.status == "transcribed"
    assert saved.transcript_path == str(transcript_path)
    assert transcript_path.read_text(encoding="utf-8") == f"Transcript for {job.stored_filename}"


def test_process_next_transcription_job_marks_failed_and_copies_audio(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="note.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001",
    )
    original_audio = paths.incoming / job.stored_filename
    original_audio.write_bytes(b"audio")

    processed_job_id = process_next_transcription_job(
        store=store,
        paths=paths,
        transcriber=ExplodingTranscriber(),
    )

    saved = store.get_job(job.id)
    failed_copy = paths.failed / job.stored_filename

    assert processed_job_id is None
    assert saved.status == "failed"
    assert saved.error_message == f"transcription failed for {job.stored_filename}"
    assert failed_copy.exists()
    assert failed_copy.read_bytes() == original_audio.read_bytes()


def test_process_next_transcription_job_marks_failed_even_if_copy_fails(tmp_path, monkeypatch):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="note.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001",
    )
    (paths.incoming / job.stored_filename).write_bytes(b"audio")

    def failing_copy2(source, destination):
        raise OSError(f"copy failed for {destination}")

    monkeypatch.setattr(worker_module.shutil, "copy2", failing_copy2)

    processed_job_id = process_next_transcription_job(
        store=store,
        paths=paths,
        transcriber=ExplodingTranscriber(),
    )

    saved = store.get_job(job.id)

    assert processed_job_id is None
    assert saved.status == "failed"
    assert "transcription failed" in saved.error_message
    assert "copy failed" in saved.error_message
