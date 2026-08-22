import shutil
import logging
import os
from pathlib import Path

from watch_audio_pipeline.emailer import build_memo_email, build_subject
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.summarization import OllamaSummarizer, fallback_title
from watch_audio_pipeline.transcription import Transcriber, is_retryable_transcription_error


transcription_logger = logging.getLogger("transcription")
email_logger = logging.getLogger("email")


def process_next_chunk_job(
    *,
    chunk_store: ChunkStore,
    paths: AppPaths,
    transcriber: Transcriber,
) -> str | None:
    chunk = chunk_store.claim_next_chunk()
    if chunk is None:
        return None

    audio_path = paths.chunks / chunk.session_id / chunk.stored_filename
    try:
        transcript = transcriber.transcribe(audio_path)
        directory = paths.chunk_transcripts / chunk.session_id
        directory.mkdir(parents=True, exist_ok=True)
        transcript_path = directory / f"{chunk.chunk_index:06d}.txt"
        transcript_path.write_text(transcript.text, encoding="utf-8")
        chunk_store.mark_transcribed(
            chunk,
            transcript_path,
            language=getattr(transcript, "language", None),
            duration_seconds=getattr(transcript, "duration_seconds", None),
            speaker_count=getattr(transcript, "speaker_count", None),
        )
        transcription_logger.info(
            "transcribed stream chunk session_id=%s index=%s",
            chunk.session_id,
            chunk.chunk_index,
        )
        return f"{chunk.session_id}:{chunk.chunk_index}"
    except ValueError as exc:
        if "audio stream contained no samples" not in str(exc).lower():
            chunk_store.mark_chunk_failed(chunk, str(exc))
            transcription_logger.exception(
                "stream chunk failed session_id=%s index=%s",
                chunk.session_id,
                chunk.chunk_index,
            )
            return None
        directory = paths.chunk_transcripts / chunk.session_id
        directory.mkdir(parents=True, exist_ok=True)
        transcript_path = directory / f"{chunk.chunk_index:06d}.txt"
        transcript_path.write_text("", encoding="utf-8")
        chunk_store.mark_transcribed(
            chunk,
            transcript_path,
            language=None,
            duration_seconds=0,
            speaker_count=None,
        )
        transcription_logger.warning(
            "empty stream chunk treated as silence session_id=%s index=%s",
            chunk.session_id,
            chunk.chunk_index,
        )
        return f"{chunk.session_id}:{chunk.chunk_index}"
    except Exception as exc:
        if is_retryable_transcription_error(exc):
            chunk_store.requeue_chunk(chunk, str(exc))
            transcription_logger.warning(
                "transient stream chunk failure requeued session_id=%s index=%s error=%s",
                chunk.session_id,
                chunk.chunk_index,
                type(exc).__name__,
            )
            return None
        chunk_store.mark_chunk_failed(chunk, str(exc))
        transcription_logger.exception(
            "stream chunk failed session_id=%s index=%s",
            chunk.session_id,
            chunk.chunk_index,
        )
        return None


def log_worker_start(server_version: str) -> None:
    transcription_logger.info(
        "worker started runtime_version=%s pid=%s",
        server_version,
        os.getpid(),
    )


def finalize_next_recording_session(
    *,
    chunk_store: ChunkStore,
    store: JobStore,
    paths: AppPaths,
    memo_store: MemoStore,
    summarizer: OllamaSummarizer | None = None,
) -> str | None:
    session = chunk_store.claim_ready_session()
    if session is None:
        return None

    try:
        chunks = chunk_store.list_chunks(session.id)
        transcript_parts = []
        for chunk in chunks:
            if not chunk.transcript_path:
                raise FileNotFoundError(
                    f"missing transcript for session {session.id} chunk {chunk.chunk_index}"
                )
            transcript_parts.append(Path(chunk.transcript_path).read_text(encoding="utf-8").strip())
        transcript_text = "\n\n".join(part for part in transcript_parts if part).strip()
        content_hash = f"recording-session:{session.id}"
        job = store.get_by_hash(content_hash)
        if job is None:
            job = store.create_job(
                source=session.source,
                original_filename=session.original_filename,
                stored_filename=f"recording-session-{session.id}.chunks",
                mime_type="audio/x-codexwatch-chunks",
                file_size=sum(chunk.file_size for chunk in chunks),
                content_hash=content_hash,
                client_id=session.client_id,
                recipient=session.recipient,
            )
        else:
            if job.client_id != session.client_id:
                raise ValueError("recording job client_id does not match session")
            if job.recipient != session.recipient:
                job = store.update_recipient(job.id, session.recipient)
        transcript_path = paths.transcripts / f"{job.id}.txt"
        transcript_path.write_text(transcript_text, encoding="utf-8")
        store.mark_transcribed(job.id, transcript_path)

        preferences = memo_store.get_preferences(session.client_id)
        fallback = fallback_title(session.original_filename)
        memo_summary = (
            summarizer.summarize(transcript_text, fallback)
            if summarizer is not None and preferences.get("summary_enabled", True)
            else None
        )
        languages = [chunk.language for chunk in chunks if chunk.language]
        memo_store.upsert_from_job(
            job,
            transcript_path,
            title=(
                memo_summary.title
                if memo_summary and preferences.get("generate_title", True)
                else fallback
            ),
            summary=memo_summary.summary if memo_summary else None,
            duration_seconds=sum(chunk.duration_seconds or 0 for chunk in chunks) or None,
            language=max(set(languages), key=languages.count) if languages else None,
            speaker_count=max((chunk.speaker_count or 0 for chunk in chunks), default=0) or None,
        )
        chunk_store.attach_job(session.id, job.id)
        transcription_logger.info(
            "finalized stream session_id=%s chunks=%s job_id=%s",
            session.id,
            len(chunks),
            job.id,
        )
        return session.id
    except Exception as exc:
        chunk_store.mark_session_failed(session.id, str(exc))
        transcription_logger.exception("stream finalization failed session_id=%s", session.id)
        return None


def process_next_transcription_job(
    *,
    store: JobStore,
    paths: AppPaths,
    transcriber: Transcriber,
    memo_store: MemoStore | None = None,
    summarizer: OllamaSummarizer | None = None,
) -> str | None:
    job = store.claim_next_job("queued", "transcribing")
    if job is None:
        return None

    audio_path = paths.incoming / job.stored_filename
    try:
        transcript = transcriber.transcribe(audio_path)
        transcript_path = paths.transcripts / f"{job.id}.txt"
        transcript_path.write_text(transcript.text, encoding="utf-8")
        store.mark_transcribed(job.id, transcript_path)
        if memo_store is not None:
            preferences = memo_store.get_preferences(job.client_id)
            memo_summary = (
                summarizer.summarize(
                    transcript.text,
                    fallback_title(job.original_filename),
                )
                if summarizer is not None and preferences.get("summary_enabled", True)
                else None
            )
            memo_store.upsert_from_job(
                job,
                transcript_path,
                title=(
                    memo_summary.title
                    if memo_summary and preferences.get("generate_title", True)
                    else fallback_title(job.original_filename)
                ),
                summary=memo_summary.summary if memo_summary else None,
                duration_seconds=getattr(transcript, "duration_seconds", None),
                language=getattr(transcript, "language", None),
                speaker_count=getattr(transcript, "speaker_count", None),
            )
        transcription_logger.info("transcribed job_id=%s transcript=%s", job.id, transcript_path.name)
        return job.id
    except Exception as exc:
        error_message = str(exc)
        if audio_path.exists():
            try:
                shutil.copy2(audio_path, paths.failed / job.stored_filename)
            except Exception as copy_exc:
                error_message = f"{error_message}; failed to copy audio to failed dir: {copy_exc}"
        store.mark_failed(job.id, error_message)
        transcription_logger.exception("transcription failed job_id=%s", job.id)
        return None


def process_next_email_job(
    *,
    store: JobStore,
    email_client,
    include_failed: bool = False,
    paths: AppPaths | None = None,
    memo_store: MemoStore | None = None,
    chunk_store: ChunkStore | None = None,
    gemini_delivery_store=None,
) -> str | None:
    job = store.claim_next_job("transcribed", "emailing")
    if job is None and include_failed:
        job = store.claim_next_job("email_failed", "emailing")
    if job is None:
        return None

    try:
        if job.transcript_path is None:
            raise FileNotFoundError(f"missing transcript path for job {job.id}")

        transcript_text = Path(job.transcript_path).read_text(encoding="utf-8")
        if gemini_delivery_store is not None:
            gemini_delivery_store.enqueue(job.id, Path(job.transcript_path))
        memo = memo_store.get(job.id) if memo_store is not None else None
        preferences = (
            memo_store.get_preferences(job.client_id)
            if memo_store is not None
            else {}
        )
        should_send = preferences.get("send_email", True)
        if should_send:
            if memo is None:
                subject = build_subject(job.id)
                body = transcript_text
            else:
                subject = build_subject(
                    job.id,
                    memo.title,
                    str(preferences.get("email_prefix", "")),
                )
                body = build_memo_email(
                    title=memo.title,
                    summary=memo.summary if preferences.get("auto_email_summary", True) else None,
                    transcript=transcript_text,
                    remove_footer=bool(preferences.get("remove_footer", False)),
                )
            recipient = (job.recipient or str(preferences.get("recipient", ""))).strip()
            if recipient:
                if job.recipient and hasattr(email_client, "send_text_exact"):
                    email_client.send_text_exact(subject, body, recipient)
                else:
                    email_client.send_text(subject, body, recipient)
            else:
                email_client.send_text(subject, body)

        audio_deleted = False
        if paths is not None:
            for audio_path in (
                paths.incoming / job.stored_filename,
                paths.failed / job.stored_filename,
            ):
                if audio_path.exists():
                    audio_path.unlink()
                    audio_deleted = True
        if chunk_store is not None and paths is not None:
            session = chunk_store.session_for_job(job.id)
            if session is not None:
                audio_deleted = chunk_store.cleanup_completed_session(
                    session.id,
                    chunk_root=paths.chunks,
                    transcript_root=paths.chunk_transcripts,
                ) or audio_deleted
        store.mark_done(job.id)
        if memo_store is not None and memo is not None:
            memo_store.mark_email_sent(job.id, audio_deleted)
        email_logger.info("emailed job_id=%s", job.id)
        return job.id
    except Exception as exc:
        store.mark_email_failed(job.id, str(exc))
        if memo_store is not None:
            memo_store.update_status(job.id, "email_failed", str(exc))
        email_logger.exception("email failed job_id=%s", job.id)
        return None
