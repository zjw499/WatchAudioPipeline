from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.emailer import build_subject
from watch_audio_pipeline.gemini_delivery import GeminiDeliveryStore
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.worker import process_next_email_job


class FakeEmailClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_text(self, subject: str, body: str) -> None:
        self.messages.append((subject, body))


class ExplodingEmailClient:
    def send_text(self, subject: str, body: str) -> None:
        raise RuntimeError("smtp send failed")


def test_process_next_email_job_sends_transcript_and_marks_done(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001",
    )
    transcript_path = paths.transcripts / f"{job.id}.txt"
    transcript_text = "Visit transcript body"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)
    email_client = FakeEmailClient()

    processed_job_id = process_next_email_job(
        store=store,
        email_client=email_client,
    )

    saved = store.get_job(job.id)

    assert processed_job_id == job.id
    assert saved.status == "done"
    assert saved.error_message is None
    assert email_client.messages == [
        (build_subject(job.id), transcript_text),
    ]
    assert job.original_filename not in email_client.messages[0][0]


def test_process_next_email_job_queues_gemini_delivery_once(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="apple-watch-stream",
        original_filename="stream.m4a",
        stored_filename="stream.chunks",
        mime_type="audio/x-codexwatch-chunks",
        file_size=8,
        content_hash="gemini-queue-test",
    )
    transcript_path = paths.transcripts / f"{job.id}.txt"
    transcript_path.write_text("Transcript for the Gem", encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)
    gemini_store = GeminiDeliveryStore(paths.database)

    processed_job_id = process_next_email_job(
        store=store,
        email_client=FakeEmailClient(),
        gemini_delivery_store=gemini_store,
    )

    assert processed_job_id == job.id
    delivery = gemini_store.get(job.id)
    assert delivery.status == "queued"
    assert delivery.transcript_path == str(transcript_path)


def test_process_next_email_job_marks_email_failed_when_send_raises(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001",
    )
    transcript_path = paths.transcripts / f"{job.id}.txt"
    transcript_path.write_text("Visit transcript body", encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)

    processed_job_id = process_next_email_job(
        store=store,
        email_client=ExplodingEmailClient(),
    )

    saved = store.get_job(job.id)

    assert processed_job_id is None
    assert saved.status == "email_failed"
    assert saved.error_message == "smtp send failed"


def test_process_next_email_job_can_retry_email_failed_job(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001-retry",
    )
    transcript_path = paths.transcripts / f"{job.id}.txt"
    transcript_text = "Visit transcript body"
    transcript_path.write_text(transcript_text, encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)
    process_next_email_job(store=store, email_client=ExplodingEmailClient())

    email_client = FakeEmailClient()
    processed_job_id = process_next_email_job(
        store=store,
        email_client=email_client,
        include_failed=True,
    )

    saved = store.get_job(job.id)

    assert processed_job_id == job.id
    assert saved.status == "done"
    assert saved.error_message is None
    assert email_client.messages == [
        (build_subject(job.id), transcript_text),
    ]


def test_process_next_email_job_does_not_retry_email_failed_by_default(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001-no-retry",
    )
    transcript_path = paths.transcripts / f"{job.id}.txt"
    transcript_path.write_text("Visit transcript body", encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)
    process_next_email_job(store=store, email_client=ExplodingEmailClient())

    processed_job_id = process_next_email_job(store=store, email_client=FakeEmailClient())

    saved = store.get_job(job.id)

    assert processed_job_id is None
    assert saved.status == "email_failed"


def test_process_next_email_job_marks_email_failed_when_transcript_file_missing(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    job = store.create_job(
        source="iphone-shortcuts",
        original_filename="visit.m4a",
        stored_filename="hash001.m4a",
        mime_type="audio/mp4",
        file_size=8,
        content_hash="hash001-missing",
    )
    missing_transcript_path = paths.transcripts / f"{job.id}.txt"
    store.mark_transcribed(job.id, missing_transcript_path)

    processed_job_id = process_next_email_job(
        store=store,
        email_client=FakeEmailClient(),
    )

    saved = store.get_job(job.id)

    assert processed_job_id is None
    assert saved.status == "email_failed"
    assert saved.error_message is not None
    assert "No such file or directory" in saved.error_message
