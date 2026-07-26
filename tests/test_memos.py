from dataclasses import dataclass

from fastapi.testclient import TestClient

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.worker import process_next_email_job, process_next_transcription_job


@dataclass
class FakeTranscript:
    text: str = "A local transcript"
    language: str = "en"
    duration_seconds: float = 12.0
    speaker_count: int = 2


class FakeTranscriber:
    def transcribe(self, audio_path):
        return FakeTranscript()


class FakeEmailClient:
    def __init__(self):
        self.messages = []

    def send_text(self, subject, body, to_address=None):
        self.messages.append((subject, body, to_address))


def test_pc_pipeline_persists_memo_and_deletes_audio_after_email(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    memo_store = MemoStore(paths.database)
    job = store.create_job(
        source="watch",
        original_filename="watch-note.m4a",
        stored_filename="audio.m4a",
        mime_type="audio/mp4",
        file_size=5,
        content_hash="memo-test",
    )
    audio_path = paths.incoming / job.stored_filename
    audio_path.write_bytes(b"audio")

    assert process_next_transcription_job(
        store=store,
        paths=paths,
        transcriber=FakeTranscriber(),
        memo_store=memo_store,
    ) == job.id
    memo = memo_store.get(job.id)
    assert memo is not None
    assert memo.speaker_count == 2

    email_client = FakeEmailClient()
    assert process_next_email_job(
        store=store,
        email_client=email_client,
        paths=paths,
        memo_store=memo_store,
    ) == job.id
    assert not audio_path.exists()
    assert memo_store.get(job.id).audio_deleted_at is not None
    assert "A local transcript" in email_client.messages[0][1]


def test_memo_api_lists_detail_and_preferences(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        basic_auth_username="user",
        basic_auth_password="pass",
    )
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    client = TestClient(create_app(settings, paths, store))

    response = client.put("/preferences", auth=("user", "pass"), json={"recipient": "dest@example.com"})
    assert response.status_code == 200
    assert response.json()["recipient"] == "dest@example.com"

    response = client.get("/memos", auth=("user", "pass"))
    assert response.status_code == 200
    assert response.json() == {"memos": []}


def test_preferences_are_isolated_by_client_id(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        basic_auth_username="user",
        basic_auth_password="pass",
    )
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    client = TestClient(create_app(settings, paths, store))
    headers_a = {"X-Codex-Client-ID": "client-a-1234"}
    headers_b = {"X-Codex-Client-ID": "client-b-1234"}

    response = client.put(
        "/preferences",
        auth=("user", "pass"),
        headers=headers_a,
        json={"email_prefix": "Tester A"},
    )
    assert response.status_code == 200

    response = client.get("/preferences", auth=("user", "pass"), headers=headers_b)
    assert response.status_code == 200
    assert response.json()["email_prefix"] == ""

    response = client.get("/preferences", auth=("user", "pass"), headers=headers_a)
    assert response.status_code == 200
    assert response.json()["email_prefix"] == "Tester A"


def test_legacy_preferences_keep_default_record(tmp_path):
    store = MemoStore(tmp_path / "state.sqlite")
    store.update_preferences({"recipient": "legacy@example.com"})

    assert store.get_preferences("legacy")["recipient"] == "legacy@example.com"
    assert store.update_preferences({"email_prefix": "Legacy"}, "legacy")["recipient"] == "legacy@example.com"


def test_memo_list_isolated_by_client_id(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        basic_auth_username="user",
        basic_auth_password="pass",
    )
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    memo_store = MemoStore(paths.database)
    for client_id, suffix in (("client-a-1234", "a"), ("client-b-1234", "b")):
        job = store.create_job(
            source="iphone-app",
            original_filename=f"memo-{suffix}.m4a",
            stored_filename=f"memo-{suffix}.m4a",
            mime_type="audio/mp4",
            file_size=1,
            content_hash=f"memo-{suffix}",
            client_id=client_id,
        )
        transcript_path = paths.transcripts / f"{job.id}.txt"
        transcript_path.write_text(f"transcript-{suffix}", encoding="utf-8")
        memo_store.upsert_from_job(job, transcript_path, title=f"Memo {suffix}")

    client = TestClient(create_app(settings, paths, store, memo_store=memo_store))
    response = client.get(
        "/memos",
        auth=("user", "pass"),
        headers={"X-Codex-Client-ID": "client-a-1234"},
    )

    assert response.status_code == 200
    assert [memo["title"] for memo in response.json()["memos"]] == ["Memo a"]
