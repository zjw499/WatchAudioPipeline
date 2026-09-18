from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.cli import process_cycle
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.db import connect
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.notion_audio import NativeNotionAPI, NativeNotionWorker, PART_BYTES
from watch_audio_pipeline.notion_delivery import NotionPage, NotionPublisher
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.audio_batching import PreparedAudioBatch


class FakeBatcher:
    def __init__(self):
        self.sources = []

    def prepare(self, sources, output):
        self.sources.append(list(sources))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"".join(p.read_bytes() for p in sources))
        return PreparedAudioBatch(output, 7200, 0, False)


class FakeNative(NotionPublisher):
    def __init__(self):
        super().__init__(token="test", data_source_id="test")
        self.page = None
        self.meetings = []
        self.requests = []
        self.lose_create_response = False
        self.lose_page_response = False
        self.delay_visibility = False
        self.empty_transcript = False
        self.on_readback = None

    def find_by_recording_id(self, recording_id, client_id):
        return NotionPage(**self.page) if self.page else None

    def _list_children(self, page_id):
        return [] if self.delay_visibility else deepcopy(self.meetings)

    def upload_step(self, path, state, checkpoint):
        assert path.read_bytes()
        checkpoint(upload_id="upload-123")
        return True

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, payload))
        if path == "/pages":
            self.page = {"id": "page-123", "url": "https://notion.so/page-123"}
            if self.lose_page_response:
                self.lose_page_response = False
                raise TimeoutError()
            return self.page
        if path == "/blocks/meeting_notes":
            block = {"id": f"meeting-{len(self.meetings)}", "type": "meeting_notes"}
            self.meetings.append(block)
            if self.lose_create_response:
                self.lose_create_response = False
                raise TimeoutError()
            return block
        if path.startswith("/blocks/"):
            return {"meeting_notes": {
                "status": "notes_ready", "title": self._rich_text("Native Notion title"),
                "children": {"transcript_block_id": "transcript", "summary_block_id": "summary"},
            }}
        if path.startswith("/pages/"):
            return self.page
        raise AssertionError(path)

    def read_tree(self, block_id):
        if self.on_readback:
            callback, self.on_readback = self.on_readback, None
            callback()
        text = "Beginning. Middle. End." if block_id == "transcript" else "Native summary"
        if block_id == "transcript" and self.empty_transcript:
            text = ""
        return [self._paragraph(text)]


@pytest.fixture
def setup(tmp_path):
    settings = Settings(_env_file=None, project_root=tmp_path, notion_enabled=True,
                        notion_transcribe_audio=True, notion_client_ids=["owner"], email_enabled=False,
                        basic_auth_username="test", basic_auth_password="test")
    paths = ensure_directories(build_paths(settings))
    worker = NativeNotionWorker(settings, paths, FakeNative(), FakeBatcher())
    return settings, paths, worker


def add_chunk(worker, index, final=False, owner="owner", recording="test-stream"):
    directory = worker.paths.chunks / recording
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"{index}.m4a"
    audio = bytes([index + 1]) * 100
    (directory / filename).write_bytes(audio)
    worker.chunks.receive_chunk(
        session_id=recording, chunk_index=index, stored_filename=filename,
        original_filename="Synthetic meeting.m4a", source="apple-watch-stream", client_id=owner,
        recipient=None, mime_type="audio/mp4", file_size=len(audio),
        content_hash=hashlib.sha256(audio).hexdigest(), is_final=final,
    )


def run(worker, cycles=10):
    for _ in range(cycles):
        db = connect(worker.paths.database)
        with db:
            db.execute("UPDATE notion_audio_jobs SET next_attempt_at = '2000-01-01' WHERE status = 'active'")
        db.close()
        worker.step()


def test_native_transcription_waits_for_final_and_missing_chunks(setup):
    _, _, worker = setup
    add_chunk(worker, 0)
    run(worker)
    assert not worker.api.requests
    add_chunk(worker, 2, final=True)
    run(worker)
    assert not worker.api.requests
    add_chunk(worker, 1)
    run(worker)
    session = worker.chunks.get_session("test-stream")
    assert session.status == "done"
    memo = worker.memos.get(session.job_id)
    assert memo.status == "done"
    assert Path(memo.transcript_path).read_text() == "Beginning. Middle. End."
    assert memo.summary == "Native summary"
    assert memo.title == "Native Notion title"
    assert memo.audio_deleted_at is None
    assert [p.name for p in worker.audio_batcher.sources[0]] == ["0.m4a", "1.m4a", "2.m4a"]
    assert all(p.exists() for p in worker.audio_batcher.sources[0])


def test_native_transcript_only_disables_notion_summary_generation(setup):
    settings, _, worker = setup
    settings.notion_transcript_only_client_ids = ["owner"]
    add_chunk(worker, 0, final=True)
    run(worker)

    create = next(payload for method, path, payload in worker.api.requests
                  if method == "POST" and path == "/blocks/meeting_notes")
    assert create["options"]["kickoff_summary"] is False
    memo = worker.memos.get(worker.chunks.get_session("test-stream").job_id)
    assert memo.summary == ""


def test_native_clients_never_claimed_by_groq_or_legacy_finalizer(setup):
    settings, paths, worker = setup
    add_chunk(worker, 0, final=True)
    job = worker.store.create_job(source="iphone", original_filename="test.m4a", stored_filename="test.m4a",
                                  mime_type="audio/mp4", file_size=1, content_hash="whole", client_id="owner")
    (paths.incoming / job.stored_filename).write_bytes(b"x")
    class Forbidden:
        def transcribe(self, *args):
            pytest.fail("Native Notion audio reached Groq")
    assert process_cycle(settings, paths, worker.store, Forbidden(), None, audio_batcher=FakeBatcher()) == 0
    assert worker.chunks.claim_next_chunk(("owner",)) is None
    assert worker.chunks.claim_ready_session(("owner",)) is None
    assert worker.store.claim_next_job("queued", "transcribing", excluded_clients=("owner",)) is None
    run(worker)
    assert worker.store.get_job(job.id).status == "done"


def test_other_users_unchanged_and_not_uploaded_to_owner_workspace(setup):
    _, _, worker = setup
    add_chunk(worker, 0, final=True, owner="craig")
    run(worker)
    assert not worker.api.requests
    chunks = worker.chunks.claim_next_chunk_batch(8, ("owner",))
    assert len(chunks) == 1


@pytest.mark.parametrize("lost", ["lose_create_response", "lose_page_response"])
def test_restart_and_ambiguous_response_does_not_duplicate(setup, lost):
    settings, paths, worker = setup
    setattr(worker.api, lost, True)
    add_chunk(worker, 0, final=True)
    run(worker, 3)
    restarted = NativeNotionWorker(settings, paths, worker.api, worker.audio_batcher)
    run(restarted)
    assert restarted.chunks.get_session("test-stream").status == "done"
    assert sum(path == "/blocks/meeting_notes" for _, path, _ in worker.api.requests) == 1
    assert sum(path == "/pages" for _, path, _ in worker.api.requests) == 1


def test_ambiguous_create_stays_held_until_original_block_is_visible(setup):
    _, _, worker = setup
    worker.api.lose_create_response = True
    add_chunk(worker, 0, final=True)
    run(worker, 3)
    worker.api.delay_visibility = True
    run(worker, 5)
    assert len(worker.api.meetings) == 1
    assert worker.chunks.get_session("test-stream").status != "done"
    worker.api.delay_visibility = False
    run(worker)
    assert worker.chunks.get_session("test-stream").status == "done"


def test_late_audio_during_readback_never_marks_old_revision_complete(setup):
    _, _, worker = setup
    add_chunk(worker, 0, final=True)
    worker.api.on_readback = lambda: add_chunk(worker, 1)
    run(worker)
    assert worker.chunks.get_session("test-stream").status != "done"
    add_chunk(worker, 1, final=True)
    run(worker)
    assert worker.chunks.get_session("test-stream").status == "done"
    assert len(worker.api.meetings) == 2
    assert sum(path == "/pages" for _, path, _ in worker.api.requests) == 1


def test_duplicate_chunk_after_completion_creates_no_new_meeting(setup):
    _, _, worker = setup
    add_chunk(worker, 0, final=True)
    run(worker)
    add_chunk(worker, 0, final=True)
    run(worker)
    assert len(worker.api.meetings) == 1


def test_empty_transcript_not_treated_as_success(setup):
    _, _, worker = setup
    worker.api.empty_transcript = True
    add_chunk(worker, 0, final=True)
    run(worker)
    assert worker.chunks.get_session("test-stream").status != "done"
    assert all(p.exists() for p in worker.audio_batcher.sources[0])


def test_multipart_upload_resumes_without_loading_whole_file(tmp_path):
    class UploadAPI(NativeNotionAPI):
        def __init__(self):
            self.sent = []
            self.uploaded = False
            self.created = 0

        def _request(self, method, path, payload=None, **kwargs):
            if path == "/file_uploads":
                assert payload["mode"] == "multi_part"
                assert payload["number_of_parts"] == 3
                assert payload["content_type"] == "audio/mp4"
                self.created += 1
                return {"id": "upload", "status": "pending"}
            if path.endswith("/send"):
                self.sent.append((kwargs["data"]["part_number"], len(kwargs["files"]["file"][1])))
            if path.endswith("/complete"):
                self.uploaded = True
            return {"status": "uploaded" if self.uploaded else "pending"}
    path = tmp_path / "long.m4a"
    with path.open("wb") as audio:
        audio.truncate(2 * PART_BYTES + 123)
    api, state = UploadAPI(), {}
    for _ in range(5):
        if api.upload_step(path, state, lambda **updates: state.update(updates)):
            break
        state = json.loads(json.dumps(state))
    assert api.sent == [("1", PART_BYTES), ("2", PART_BYTES), ("3", 123)]
    assert api.created == 1
    assert api.uploaded


def test_progress_and_destination_report_native_provider_only_for_owner(setup):
    from fastapi.testclient import TestClient
    from watch_audio_pipeline.app import create_app
    settings, paths, worker = setup
    settings.notion_client_ids = ["owner-phone"]
    client = TestClient(create_app(settings, paths, worker.store))
    auth = (settings.basic_auth_username, settings.basic_auth_password)
    for owner, provider in (("owner-phone", "notion"), ("other-phone", "groq")):
        response = client.get("/destination", auth=auth, headers={"X-Codex-Client-ID": owner})
        assert response.status_code == 200, response.json()
        assert response.json()["transcription_provider"] == provider
