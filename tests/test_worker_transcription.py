from dataclasses import dataclass

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.worker import process_next_transcription_job


@dataclass
class FakeTranscriptResult:
    text: str
    language: str | None = None
    duration_seconds: float | None = None


class FakeTranscriber:
    def transcribe(self, audio_path):
        return FakeTranscriptResult(text=f"Transcript for {audio_path.name}")


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

    assert processed_job_id == job.id
    assert saved.status == "transcribed"
    assert saved.transcript_path is not None
