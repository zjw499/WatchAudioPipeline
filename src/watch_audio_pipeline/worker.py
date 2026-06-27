import shutil

from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.transcription import Transcriber


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
        return job.id
    except Exception as exc:
        error_message = str(exc)
        if audio_path.exists():
            try:
                shutil.copy2(audio_path, paths.failed / job.stored_filename)
            except Exception as copy_exc:
                error_message = f"{error_message}; failed to copy audio to failed dir: {copy_exc}"
        store.mark_failed(job.id, error_message)
        return None
