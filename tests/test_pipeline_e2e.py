import io
from dataclasses import dataclass

from fastapi.testclient import TestClient

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.worker import process_next_email_job, process_next_transcription_job


@dataclass(frozen=True)
class FakeTranscriptResult:
    text: str
    language: str | None = None
    duration_seconds: float | None = None


class FakeTranscriber:
    def transcribe(self, audio_path):
        return FakeTranscriptResult(text="Final transcript body")


class FakeEmailClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_text(self, subject: str, body: str) -> None:
        self.messages.append((subject, body))


def test_upload_then_transcribe_then_email(tmp_path):
    settings = Settings(project_root=tmp_path, upload_token="test-token")
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    client = TestClient(create_app(settings, paths, store))

    upload_response = client.post(
        "/upload",
        headers={"X-Upload-Token": "test-token"},
        data={"source": "iphone-shortcuts"},
        files={"file": ("visit.m4a", io.BytesIO(b"audio-data"), "audio/mp4")},
    )

    assert upload_response.status_code == 201

    email_client = FakeEmailClient()
    assert process_next_transcription_job(
        store=store,
        paths=paths,
        transcriber=FakeTranscriber(),
    ) is not None
    assert process_next_email_job(store=store, email_client=email_client) is not None

    finished_jobs = store.list_jobs_by_status("done")
    assert len(finished_jobs) == 1
    assert email_client.messages[0][1] == "Final transcript body"
    assert "visit.m4a" not in email_client.messages[0][0]
