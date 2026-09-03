from pathlib import Path

from watch_audio_pipeline.audio_batching import PreparedAudioBatch
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.transcription import TranscriptResult
from watch_audio_pipeline.worker import (
    deduplicate_transcript_overlap,
    finalize_next_recording_session,
    process_next_chunk_job,
    suppress_repeated_boundary_hallucinations,
)


AUTH = ("test-user", "test-password")


class FakeBatcher:
    def __init__(self, *, silent: bool = False):
        self.silent = silent
        self.calls = []

    def prepare(self, audio_paths, output_path, *, overlap_source=None):
        self.calls.append((list(audio_paths), overlap_source))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"combined-audio")
        overlap = 2.0 if overlap_source is not None else 0.0
        return PreparedAudioBatch(
            path=output_path,
            duration_seconds=(30.0 * len(audio_paths)) + overlap,
            overlap_seconds=overlap,
            is_silent=self.silent,
        )


class SequenceTranscriber:
    def __init__(self, *texts):
        self.texts = list(texts)
        self.calls = []

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        self.calls.append(audio_path)
        return TranscriptResult(
            text=self.texts[len(self.calls) - 1],
            language="en",
            duration_seconds=242.0,
            speaker_count=None,
        )


class UnexpectedTranscriber:
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        raise AssertionError(f"silent audio should not be transcribed: {audio_path}")


def _upload(client, recording_id, index, *, final=False):
    return client.post(
        "/upload/chunk",
        auth=AUTH,
        data={
            "recording_id": recording_id,
            "chunk_index": str(index),
            "is_final": "true" if final else "false",
            "source": "apple-watch-stream",
        },
        files={
            "file": (
                f"watch-{recording_id}-{index}.m4a",
                f"audio-{index}".encode(),
                "audio/mp4",
            )
        },
    )


def test_batch_waits_for_eight_contiguous_chunks(app_parts):
    _, paths, _, client = app_parts
    recording_id = "recording-batch-wait"
    chunk_store = ChunkStore(paths.database)
    batcher = FakeBatcher()
    transcriber = SequenceTranscriber("first batch")

    for index in range(7):
        assert _upload(client, recording_id, index).status_code == 201

    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=transcriber,
        audio_batcher=batcher,
        batch_size=8,
    ) is None
    assert not batcher.calls
    assert {chunk.status for chunk in chunk_store.list_chunks(recording_id)} == {
        "batch_queued"
    }

    assert _upload(client, recording_id, 7).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=transcriber,
        audio_batcher=batcher,
        batch_size=8,
    ) == f"{recording_id}:0-7"
    assert len(batcher.calls) == 1
    assert len(batcher.calls[0][0]) == 8
    assert batcher.calls[0][1] is None
    assert len(transcriber.calls) == 1
    assert {chunk.status for chunk in chunk_store.list_chunks(recording_id)} == {
        "transcribed"
    }


def test_final_tail_uses_overlap_and_removes_duplicate_words(app_parts):
    _, paths, store, client = app_parts
    recording_id = "recording-batch-tail"
    chunk_store = ChunkStore(paths.database)
    batcher = FakeBatcher()
    transcriber = SequenceTranscriber(
        "Alpha beta gamma delta.",
        "Beta gamma delta. Continued words.",
    )

    for index in range(8):
        assert _upload(client, recording_id, index).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=transcriber,
        audio_batcher=batcher,
        batch_size=8,
    ) == f"{recording_id}:0-7"

    assert _upload(client, recording_id, 8).status_code == 201
    assert _upload(client, recording_id, 9, final=True).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=transcriber,
        audio_batcher=batcher,
        batch_size=8,
    ) == f"{recording_id}:8-9"

    assert len(batcher.calls) == 2
    assert len(batcher.calls[1][0]) == 2
    assert batcher.calls[1][1] == batcher.calls[0][0][-1]
    assert finalize_next_recording_session(
        chunk_store=chunk_store,
        store=store,
        paths=paths,
        memo_store=MemoStore(paths.database),
    ) == recording_id
    job = store.get_by_hash(f"recording-session:{recording_id}")
    assert job is not None
    assert Path(job.transcript_path).read_text(encoding="utf-8") == (
        "Alpha beta gamma delta.\n\nContinued words."
    )


def test_confirmed_silence_skips_transcription(app_parts):
    _, paths, _, client = app_parts
    recording_id = "recording-batch-silent"
    chunk_store = ChunkStore(paths.database)

    assert _upload(client, recording_id, 0, final=True).status_code == 201
    assert process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=UnexpectedTranscriber(),
        audio_batcher=FakeBatcher(silent=True),
        batch_size=8,
    ) == f"{recording_id}:0-0"
    chunk = chunk_store.list_chunks(recording_id)[0]
    assert chunk.status == "transcribed"
    assert Path(chunk.transcript_path).read_text(encoding="utf-8") == ""


def test_overlap_deduplication_requires_three_exact_words():
    assert deduplicate_transcript_overlap(
        "Earlier words one two three.",
        "One two three. New sentence.",
    ) == "New sentence."
    assert deduplicate_transcript_overlap("Earlier one two.", "One two. New.") == (
        "One two. New."
    )


def test_repeated_trailing_thank_you_is_suppressed_only_when_systemic():
    parts = [
        "First content. Thank you.",
        "Second content. Thank you!",
        "Third content. Thank you.",
        "Fourth content.",
        "Patient said thank you.",
    ]

    cleaned, count = suppress_repeated_boundary_hallucinations(parts)

    assert count == 3
    assert cleaned == [
        "First content.",
        "Second content.",
        "Third content.",
        "Fourth content.",
        "Patient said thank you.",
    ]
    assert suppress_repeated_boundary_hallucinations(parts[:2]) == (parts[:2], 0)
