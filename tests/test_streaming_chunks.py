from pathlib import Path

from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.transcription import TranscriptResult
from watch_audio_pipeline.worker import (
    finalize_next_recording_session,
    process_next_chunk_job,
    process_next_email_job,
)


AUTH = ("test-user", "test-password")


class IndexedTranscriber:
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        index = int(audio_path.name.split("-", 1)[0])
        return TranscriptResult(
            text=f"chunk {index}",
            language="en",
            duration_seconds=30,
            speaker_count=2,
        )


class CapturingEmailClient:
    def __init__(self):
        self.messages = []

    def send_text(self, subject, body, recipient=None):
        self.messages.append((subject, body, recipient))

    def send_text_exact(self, subject, body, recipient):
        self.messages.append((subject, body, recipient))


class EmptyChunkTranscriber:
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        raise ValueError(f"audio stream contained no samples in {audio_path.name}")


class FailingTranscriber:
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        raise RuntimeError("temporary transcription failure")


def _upload(client, recording_id, index, *, final=False, content=None, recipient=None):
    data = {
        "recording_id": recording_id,
        "chunk_index": str(index),
        "is_final": "true" if final else "false",
        "source": "apple-watch-stream",
    }
    if recipient is not None:
        data["recipient"] = recipient
    return client.post(
        "/upload/chunk",
        auth=AUTH,
        data=data,
        files={
            "file": (
                f"watch-{recording_id}-{index}.m4a",
                content or f"audio-{index}".encode(),
                "audio/mp4",
            )
        },
    )


def test_streamed_recording_transcribes_in_order_and_emails_once(app_parts):
    _, paths, store, client = app_parts
    recording_id = "recording-12345678"
    chunk_store = ChunkStore(paths.database)
    memo_store = MemoStore(paths.database)

    assert _upload(client, recording_id, 0, recipient="tester@example.com").status_code == 201
    assert _upload(
        client, recording_id, 2, final=True, recipient="tester@example.com"
    ).status_code == 201
    assert _upload(client, recording_id, 1, recipient="tester@example.com").status_code == 201

    transcriber = IndexedTranscriber()
    for _ in range(3):
        assert process_next_chunk_job(
            chunk_store=chunk_store,
            paths=paths,
            transcriber=transcriber,
        ) is not None

    assert finalize_next_recording_session(
        chunk_store=chunk_store,
        store=store,
        paths=paths,
        memo_store=memo_store,
    ) == recording_id

    email = CapturingEmailClient()
    assert process_next_email_job(
        store=store,
        email_client=email,
        paths=paths,
        memo_store=memo_store,
        chunk_store=chunk_store,
    ) is not None
    assert len(email.messages) == 1
    assert email.messages[0][2] == "tester@example.com"
    assert email.messages[0][1].index("chunk 0") < email.messages[0][1].index("chunk 1")
    assert email.messages[0][1].index("chunk 1") < email.messages[0][1].index("chunk 2")
    assert chunk_store.get_session(recording_id).status == "done"
    assert not (paths.chunks / recording_id).exists()

    duplicate = _upload(client, recording_id, 0)
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert not (paths.chunks / recording_id).exists()


def test_late_chunk_reopens_premature_final_marker(app_parts):
    _, paths, _, client = app_parts
    recording_id = "recording-latefinal"
    chunk_store = ChunkStore(paths.database)

    assert _upload(client, recording_id, 0, final=True).status_code == 201
    assert _upload(client, recording_id, 1).status_code == 201
    session = chunk_store.get_session(recording_id)
    assert session is not None
    assert session.status == "receiving"
    assert session.final_chunk_index is None

    assert _upload(client, recording_id, 2, final=True).status_code == 201
    session = chunk_store.get_session(recording_id)
    assert session is not None
    assert session.status == "final_received"
    assert session.final_chunk_index == 2


def test_late_retry_after_done_reopens_session_instead_of_returning_400(app_parts):
    _, paths, store, client = app_parts
    recording_id = "recording-lateretry"
    chunk_store = ChunkStore(paths.database)
    memo_store = MemoStore(paths.database)

    assert _upload(client, recording_id, 0, final=True).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=IndexedTranscriber(),
    ) is not None
    assert finalize_next_recording_session(
        chunk_store=chunk_store,
        store=store,
        paths=paths,
        memo_store=memo_store,
    ) == recording_id
    email = CapturingEmailClient()
    assert process_next_email_job(
        store=store,
        email_client=email,
        paths=paths,
        memo_store=memo_store,
        chunk_store=chunk_store,
    ) is not None
    assert chunk_store.get_session(recording_id).status == "done"

    late = _upload(client, recording_id, 1)
    assert late.status_code == 201
    session = chunk_store.get_session(recording_id)
    assert session is not None
    assert session.status == "receiving"
    assert session.final_chunk_index is None


def test_chunk_retry_is_idempotent_and_conflicting_content_is_rejected(app_parts):
    _, _, _, client = app_parts
    recording_id = "recording-abcdefgh"
    first = _upload(client, recording_id, 0)
    duplicate = _upload(client, recording_id, 0)
    conflict = _upload(client, recording_id, 0, content=b"different")

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert conflict.status_code == 400


def test_streamed_recording_keeps_one_recipient_for_all_chunks(app_parts):
    _, paths, _, client = app_parts
    recording_id = "recording-recipient"
    chunk_store = ChunkStore(paths.database)

    assert _upload(client, recording_id, 0, recipient="tester@example.com").status_code == 201
    mismatch = _upload(client, recording_id, 1, final=True, recipient="other@example.com")

    assert mismatch.status_code == 400
    session = chunk_store.get_session(recording_id)
    assert session is not None
    assert session.recipient == "tester@example.com"


def test_session_waits_for_missing_chunk_before_finalizing(app_parts):
    _, paths, store, client = app_parts
    recording_id = "recording-waiting1"
    chunk_store = ChunkStore(paths.database)
    memo_store = MemoStore(paths.database)

    _upload(client, recording_id, 0)
    _upload(client, recording_id, 2, final=True)
    for _ in range(2):
        process_next_chunk_job(
            chunk_store=chunk_store,
            paths=paths,
            transcriber=IndexedTranscriber(),
        )

    assert finalize_next_recording_session(
        chunk_store=chunk_store,
        store=store,
        paths=paths,
        memo_store=memo_store,
    ) is None
    assert store.count_jobs() == 0


def test_retry_resumes_failed_empty_chunk_and_allows_finalization(app_parts):
    _, paths, store, client = app_parts
    recording_id = "recording-retry123"
    chunk_store = ChunkStore(paths.database)
    memo_store = MemoStore(paths.database)

    assert _upload(client, recording_id, 0, final=True).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=EmptyChunkTranscriber(),
    ) is not None
    assert chunk_store.get_session(recording_id).status == "final_received"

    retry = client.post(f"/recordings/{recording_id}/retry", auth=AUTH)
    assert retry.status_code == 200
    assert finalize_next_recording_session(
        chunk_store=chunk_store,
        store=store,
        paths=paths,
        memo_store=memo_store,
    ) == recording_id


def test_retry_requeues_failed_chunk(app_parts):
    _, paths, _, client = app_parts
    recording_id = "recording-retryfail"
    chunk_store = ChunkStore(paths.database)

    assert _upload(client, recording_id, 0, final=True).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=FailingTranscriber(),
    ) is None
    assert chunk_store.get_session(recording_id).status == "failed"

    retry = client.post(f"/recordings/{recording_id}/retry", auth=AUTH)
    assert retry.status_code == 200
    assert chunk_store.get_session(recording_id).status == "final_received"
    assert chunk_store.list_chunks(recording_id)[0].status == "queued"
