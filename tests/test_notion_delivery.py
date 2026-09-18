from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.notion_delivery import (
    NotionDeliveryStore,
    NotionPage,
    NotionPublisher,
)
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.worker import process_next_notion_job


class RecordingNotionPublisher(NotionPublisher):
    def __init__(self):
        super().__init__(token="test-token", data_source_id="data-source")
        self.requests = []
        self.page = None
        self.children = []
        self.fail_after_append = False

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, deepcopy(payload)))
        if path.endswith("/query"):
            return {"results": [self.page] if self.page else []}
        if method == "POST" and path == "/pages":
            assert self.page is None
            self.page = {"id": "page-123", "url": "https://notion.so/page-123", "properties": deepcopy(payload["properties"])}
            return self.page
        if method == "GET":
            cursor = int(parse_qs(urlsplit(path).query).get("start_cursor", [0])[0])
            children = deepcopy(self.children[cursor:cursor + 100])
            more = len(self.children) > cursor + 100
            return {"results": children, "has_more": more, "next_cursor": str(cursor + 100) if more else None}
        if method == "PATCH" and "/blocks/" in path:
            assert len(payload["children"]) <= 100
            self.children.extend(deepcopy(payload["children"]))
            if self.fail_after_append:
                self.fail_after_append = False
                raise TimeoutError("Response lost after Notion accepted blocks")
            return {"results": []}
        if method == "PATCH" and "/pages/" in path:
            self.page["properties"].update(deepcopy(payload["properties"]))
            return self.page
        raise AssertionError((method, path))


class FakePublisher:
    def __init__(self):
        self.calls = []

    def ensure_meeting(self, **kwargs):
        self.calls.append(kwargs)
        return NotionPage(id="page-123", url="https://notion.so/page-123")


class FailingPublisher:
    def ensure_meeting(self, **kwargs):
        raise RuntimeError("Notion temporarily unavailable")


def test_publisher_creates_structured_page_and_checks_recording_id():
    publisher = RecordingNotionPublisher()

    page = publisher.ensure_meeting(
        recording_id="recording-123",
        client_id="client-123",
        title="Weekly Operations Meeting",
        summary="The team reviewed operations.",
        transcript="First line.\nSecond line.",
        created_at="2026-09-16T12:00:00+00:00",
        duration_seconds=630,
        source="apple-watch-stream",
        speaker_count=3,
        action_items=("Craig will send the report.",),
        decisions=("The launch remains Friday.",),
        topics=("Launch", "Staffing"),
    )

    assert page.id == "page-123"
    _, query_path, query_payload = publisher.requests[0]
    assert query_path == "/data_sources/data-source/query"
    assert query_payload["filter"]["and"][0]["rich_text"]["equals"] == "recording-123"
    assert query_payload["filter"]["and"][1]["rich_text"]["equals"] == "client-123"
    _, create_path, create_payload = publisher.requests[1]
    assert create_path == "/pages"
    assert create_payload["parent"]["data_source_id"] == "data-source"
    assert create_payload["properties"]["Source"]["select"]["name"] == "Apple Watch"
    assert create_payload["properties"]["Duration (min)"]["number"] == 10.5
    assert create_payload["properties"]["Has action items"]["checkbox"] is True
    assert create_payload["properties"]["Delivery"]["select"]["name"] == "Needs review"
    assert publisher.page["properties"]["Delivery"]["select"]["name"] == "Ready"
    block_types = [block["type"] for block in publisher.children]
    assert "to_do" in block_types
    assert block_types.count("heading_2") == 5


def test_publisher_reuses_existing_page_without_creating_duplicate():
    publisher = RecordingNotionPublisher()

    kwargs = dict(
        recording_id="recording-123",
        client_id="client-123",
        title="Existing meeting",
        summary=None,
        transcript="Transcript",
        created_at="2026-09-16T12:00:00+00:00",
        duration_seconds=None,
        source="iphone-app",
        speaker_count=None,
    )
    first = publisher.ensure_meeting(**kwargs)
    expected_children = deepcopy(publisher.children)
    page = publisher.ensure_meeting(**kwargs)

    assert page.id == first.id
    assert publisher.children == expected_children
    assert sum(method == "POST" and path == "/pages" for method, path, _ in publisher.requests) == 1


def test_transcript_only_page_contains_complete_groq_transcript_without_generated_notes():
    publisher = RecordingNotionPublisher()

    publisher.ensure_meeting(
        recording_id="groq-recording",
        client_id="owner-phone",
        title="Completed Recording",
        summary="This must not be published.",
        transcript="First paragraph.\nSecond paragraph.",
        created_at="2026-09-17T12:00:00+00:00",
        duration_seconds=300,
        source="apple-watch-stream",
        speaker_count=None,
        action_items=("This must not be published.",),
        decisions=("This must not be published.",),
        topics=("This must not be published.",),
        transcript_only=True,
    )

    text = "\n".join(publisher._block_signature(block)[1] for block in publisher.children)
    assert "Groq Whisper" in text
    assert "First paragraph." in text
    assert "Second paragraph." in text
    assert "Summary" not in text
    assert "Action items" not in text
    assert "This must not be published." not in text
    assert publisher.page["properties"]["Summary"]["rich_text"] == []
    assert publisher.page["properties"]["Has action items"]["checkbox"] is False
    assert publisher.page["properties"]["Delivery"]["select"]["name"] == "Ready"


def test_long_meeting_resumes_after_accepted_append_times_out():
    publisher = RecordingNotionPublisher()
    publisher.fail_after_append = True
    transcript = "\n".join(f"Line {index}: " + "word " * 400 for index in range(125))
    kwargs = dict(
        recording_id="long-meeting", client_id="owner", title="Long meeting",
        summary="Test summary", transcript=transcript,
        created_at="2026-09-16T12:00:00+00:00", duration_seconds=7200,
        source="apple-watch-stream", speaker_count=None,
    )
    with pytest.raises(TimeoutError):
        publisher.ensure_meeting(**kwargs)
    assert publisher.page["properties"]["Delivery"]["select"]["name"] == "Needs review"
    publisher.ensure_meeting(**kwargs)
    blocks = publisher._meeting_blocks(
        summary=kwargs["summary"], transcript=transcript,
        action_items=(), decisions=(), topics=(),
    )
    assert publisher.children == blocks
    assert publisher.page["properties"]["Delivery"]["select"]["name"] == "Ready"


def test_notion_route_is_scoped_to_configured_phone(tmp_path):
    from fastapi.testclient import TestClient
    from watch_audio_pipeline.app import create_app

    settings = Settings(
        _env_file=None, project_root=tmp_path, basic_auth_username="test", basic_auth_password="test",
        notion_enabled=True, notion_client_ids=["owner-phone"], notion_database_url="https://notion.so/private",
        notion_transcript_only_client_ids=["owner-phone"],
        gemini_gem_url="https://gemini.google.com/gem/test-owner",
        gemini_handoff_client_ids=["owner-phone"],
    )
    paths = ensure_directories(build_paths(settings))
    client = TestClient(create_app(settings, paths, JobStore(paths.database)))
    owner = client.get("/destination", auth=("test", "test"), headers={"X-Codex-Client-ID": "owner-phone"})
    other = client.get("/destination", auth=("test", "test"), headers={"X-Codex-Client-ID": "other-phone"})
    assert owner.json()["mode"] == "notion"
    assert owner.json()["transcription_provider"] == "groq"
    assert owner.json()["gemini_url"] == "https://gemini.google.com/gem/test-owner"
    assert settings.uses_notion_transcript_only("owner-phone") is True
    assert other.json()["mode"] == "email"
    assert other.json()["url"] is None
    assert other.json()["gemini_url"] is None


def _transcribed_job(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    memo_store = MemoStore(paths.database)
    job = store.create_job(
        source="apple-watch-stream",
        original_filename="meeting.m4a",
        stored_filename="meeting.m4a",
        mime_type="audio/mp4",
        file_size=5,
        content_hash="recording-session:watch-recording-123",
        client_id="client-123",
    )
    audio_path = paths.incoming / job.stored_filename
    audio_path.write_bytes(b"audio")
    transcript_path = paths.transcripts / f"{job.id}.txt"
    transcript_path.write_text("Meeting transcript", encoding="utf-8")
    store.mark_transcribed(job.id, transcript_path)
    memo_store.upsert_from_job(
        job,
        transcript_path,
        title="Test Meeting",
        summary="Meeting summary",
        action_items=("Send report",),
    )
    delivery_store = NotionDeliveryStore(paths.database)
    delivery_store.enqueue(job.id)
    return paths, store, memo_store, delivery_store, job, audio_path


def test_worker_publishes_notion_page_before_marking_job_done(tmp_path):
    paths, store, memo_store, delivery_store, job, audio_path = _transcribed_job(tmp_path)
    publisher = FakePublisher()

    result = process_next_notion_job(
        store=store,
        delivery_store=delivery_store,
        publisher=publisher,
        paths=paths,
        memo_store=memo_store,
    )

    assert result == job.id
    assert store.get_job(job.id).status == "done"
    assert delivery_store.get(job.id).status == "delivered"
    memo = memo_store.get(job.id)
    assert memo.notion_url == "https://notion.so/page-123"
    assert memo.status == "done"
    assert not audio_path.exists()
    assert publisher.calls[0]["recording_id"] == "watch-recording-123"


def test_transcript_only_worker_skips_summarizer_and_publishes_groq_text(tmp_path):
    paths, store, memo_store, delivery_store, job, _ = _transcribed_job(tmp_path)
    transcript_path = Path(store.get_job(job.id).transcript_path)
    memo_store.upsert_from_job(
        job,
        transcript_path,
        title="Groq Recording",
        summary=None,
    )
    publisher = FakePublisher()

    class ForbiddenSummarizer:
        def summarize(self, *_args, **_kwargs):
            pytest.fail("Transcript-only delivery must not invoke narrative generation")

    result = process_next_notion_job(
        store=store,
        delivery_store=delivery_store,
        publisher=publisher,
        paths=paths,
        memo_store=memo_store,
        summarizer=ForbiddenSummarizer(),
        transcript_only_clients=("client-123",),
    )

    assert result == job.id
    assert publisher.calls[0]["transcript"] == "Meeting transcript"
    assert publisher.calls[0]["transcript_only"] is True


def test_worker_retries_notion_failure_without_deleting_audio(tmp_path):
    paths, store, memo_store, delivery_store, job, audio_path = _transcribed_job(tmp_path)

    result = process_next_notion_job(
        store=store,
        delivery_store=delivery_store,
        publisher=FailingPublisher(),
        paths=paths,
        memo_store=memo_store,
        retry_base_seconds=1,
    )

    assert result is None
    assert store.get_job(job.id).status == "notion_failed"
    assert memo_store.get(job.id).status == "notion_failed"
    delivery = delivery_store.get(job.id)
    assert delivery.status == "retry"
    assert delivery.next_attempt_at is not None
    assert audio_path.exists()


def test_late_audio_appends_one_revision_without_overwriting_original():
    publisher = RecordingNotionPublisher()
    kwargs = dict(
        recording_id="late-audio", client_id="owner", title="Meeting",
        summary="Original notes", transcript="Original transcript",
        created_at="2026-09-16T12:00:00+00:00", duration_seconds=60,
        source="apple-watch-stream", speaker_count=None,
    )
    original = publisher.ensure_meeting(**kwargs)
    original_blocks = deepcopy(publisher.children)
    kwargs.update(summary="Complete notes", transcript="Original transcript. Final part.", previously_published=True)
    publisher.fail_after_append = True
    with pytest.raises(TimeoutError):
        publisher.ensure_meeting(**kwargs)
    updated = publisher.ensure_meeting(**kwargs)
    expected = deepcopy(publisher.children)
    publisher.ensure_meeting(**kwargs)
    assert updated.id == original.id
    assert publisher.children[:len(original_blocks)] == original_blocks
    assert publisher.children == expected
    assert "Final part." in str(publisher.children[-1])


def test_delivered_job_only_reopens_for_changed_transcript(tmp_path):
    _, _, _, delivery_store, job, _ = _transcribed_job(tmp_path)
    delivery_store.mark_delivered(job.id, NotionPage("page", "https://notion.so/page"))
    delivery_store.enqueue(job.id, "first-hash")
    assert delivery_store.get(job.id).status == "queued"
    delivery_store.mark_delivered(job.id, NotionPage("page", "https://notion.so/page"))
    delivery_store.enqueue(job.id, "first-hash")
    assert delivery_store.get(job.id).status == "delivered"
    delivery_store.enqueue(job.id, "second-hash")
    assert delivery_store.get(job.id).status == "queued"


def test_worker_hashes_real_transcript_and_tolerates_missing_file(tmp_path):
    from hashlib import sha256
    from watch_audio_pipeline.cli import _transcript_hash

    transcript = tmp_path / "meeting.txt"
    transcript.write_text("Complete meeting.", encoding="utf-8")
    assert _transcript_hash(str(transcript)) == sha256(b"Complete meeting.").hexdigest()
    assert _transcript_hash(str(tmp_path / "missing.txt")) is None
    assert _transcript_hash(None) is None


def test_audio_arriving_during_publication_is_not_deleted_or_marked_done(tmp_path):
    paths, store, memos, deliveries, job, audio = _transcribed_job(tmp_path)

    class LateAudioPublisher(FakePublisher):
        def ensure_meeting(self, **kwargs):
            page = super().ensure_meeting(**kwargs)
            transcript = paths.transcripts / f"{job.id}.txt"
            transcript.write_text("Complete transcript with a delayed final segment.", encoding="utf-8")
            store.mark_transcribed(job.id, transcript)
            return page

    assert process_next_notion_job(
        store=store, delivery_store=deliveries, publisher=LateAudioPublisher(),
        paths=paths, memo_store=memos,
    ) is None
    assert audio.exists()
    assert store.get_job(job.id).status == "transcribed"
    assert deliveries.get(job.id).status == "retry"
    assert memos.get(job.id).notion_url == "https://notion.so/page-123"
