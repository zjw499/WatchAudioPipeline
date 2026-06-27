import shutil
import logging
from pathlib import Path

from watch_audio_pipeline.emailer import build_subject
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.transcription import Transcriber


transcription_logger = logging.getLogger("transcription")
email_logger = logging.getLogger("email")


def process_next_transcription_job(
    *,
    store: JobStore,
    paths: AppPaths,
    transcriber: Transcriber,
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


def process_next_email_job(*, store: JobStore, email_client) -> str | None:
    job = store.claim_next_job("transcribed", "emailing")
    if job is None:
        return None

    try:
        if job.transcript_path is None:
            raise FileNotFoundError(f"missing transcript path for job {job.id}")

        transcript_text = Path(job.transcript_path).read_text(encoding="utf-8")
        email_client.send_text(
            build_subject(job.id),
            transcript_text,
        )
        store.mark_done(job.id)
        email_logger.info("emailed job_id=%s", job.id)
        return job.id
    except Exception as exc:
        store.mark_email_failed(job.id, str(exc))
        email_logger.exception("email failed job_id=%s", job.id)
        return None
