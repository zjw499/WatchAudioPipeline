import argparse
from contextlib import contextmanager
import os
import time

import uvicorn

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.config import Settings, load_settings
from watch_audio_pipeline.emailer import SmtpEmailClient
from watch_audio_pipeline.gemini_delivery import (
    GeminiBrowserClient,
    GeminiDeliveryStore,
    process_next_gemini_delivery,
)
from watch_audio_pipeline.logging_utils import configure_logging
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.notifications import NtfyNotifier
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.summarization import OllamaSummarizer
from watch_audio_pipeline.transcription import (
    FasterWhisperTranscriber,
    GroqWhisperTranscriber,
    PyannoteSpeakerDiarizer,
)
from watch_audio_pipeline.watch_folder import import_ready_audio_files
from watch_audio_pipeline.worker import (
    finalize_next_recording_session,
    process_next_chunk_job,
    process_next_email_job,
    process_next_transcription_job,
    recover_retryable_chunk_failures,
    log_worker_start,
)


def build_runtime(settings: Settings):
    paths = ensure_directories(build_paths(settings))
    configure_logging(paths.logs)
    store = JobStore(paths.database)
    return paths, store


def serve(settings: Settings) -> None:
    paths, store = build_runtime(settings)
    app = create_app(
        settings,
        paths,
        store,
        MemoStore(paths.database),
        ChunkStore(paths.database),
    )
    server_kwargs = {"host": settings.host, "port": settings.port}
    if settings.ssl_certfile or settings.ssl_keyfile:
        if not settings.ssl_certfile or not settings.ssl_keyfile:
            raise ValueError("ssl_certfile and ssl_keyfile must both be configured")
        server_kwargs["ssl_certfile"] = str(settings.ssl_certfile)
        server_kwargs["ssl_keyfile"] = str(settings.ssl_keyfile)
    uvicorn.run(app, **server_kwargs)


def build_transcriber(settings: Settings):
    provider = settings.transcription_provider.strip().lower()
    if provider == "groq":
        api_key = settings.groq_api_key.strip()
        if not api_key and settings.groq_api_key_file:
            api_key = settings.groq_api_key_file.read_text(encoding="utf-8").strip()
        return GroqWhisperTranscriber(
            api_key=api_key,
            model_name=settings.groq_model,
            language=settings.groq_language,
            timeout_seconds=settings.groq_timeout_seconds,
            max_retries=settings.groq_max_retries,
        )
    if provider != "local":
        raise ValueError(f"unsupported transcription provider: {settings.transcription_provider}")

    speaker_diarizer = None
    if settings.diarization_enabled:
        token = settings.diarization_token.strip()
        if not token and settings.diarization_token_file:
            token = settings.diarization_token_file.read_text(encoding="utf-8").strip()
        speaker_diarizer = PyannoteSpeakerDiarizer(
            model_name=settings.diarization_model,
            token=token,
            device=settings.diarization_device,
            min_speakers=settings.diarization_min_speakers,
            max_speakers=settings.diarization_max_speakers,
        )

    return FasterWhisperTranscriber(
        settings.whisper_model,
        settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cpu_threads=settings.whisper_cpu_threads,
        num_workers=settings.whisper_num_workers,
        speaker_diarizer=speaker_diarizer,
    )


def build_email_client(settings: Settings):
    return SmtpEmailClient(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        from_address=settings.smtp_from,
        to_address=settings.smtp_to,
    )


def build_summarizer(settings: Settings):
    return (
        OllamaSummarizer(
            host=settings.ollama_host,
            model=settings.ollama_model,
            timeout_seconds=settings.ollama_timeout_seconds,
            max_transcript_chars=settings.ollama_max_transcript_chars,
        )
        if settings.ollama_enabled
        else None
    )


def build_gemini_client(settings: Settings) -> GeminiBrowserClient:
    return GeminiBrowserClient(
        gem_url=settings.gemini_gem_url.strip(),
        profile_dir=settings.gemini_profile_dir,
        chrome_channel=settings.gemini_chrome_channel,
        headless=settings.gemini_headless,
        timeout_seconds=settings.gemini_timeout_seconds,
    )


def build_notifier(settings: Settings):
    if not settings.ntfy_enabled:
        return None
    if not settings.ntfy_url.strip():
        raise ValueError("ntfy_url must be configured when ntfy is enabled")
    return NtfyNotifier(
        settings.ntfy_url,
        timeout_seconds=settings.ntfy_timeout_seconds,
    )


def build_services(settings: Settings):
    transcriber = build_transcriber(settings)
    email_client = build_email_client(settings)
    summarizer = build_summarizer(settings)
    return transcriber, email_client, summarizer


def process_cycle(settings: Settings, paths, store, transcriber, email_client, summarizer=None) -> int:
    processed = 0
    memo_store = MemoStore(paths.database)
    chunk_store = ChunkStore(paths.database)
    gemini_delivery_store = (
        GeminiDeliveryStore(paths.database) if settings.gemini_enabled else None
    )
    if settings.watch_folder_enabled:
        summary = import_ready_audio_files(
            watch_folder=settings.watch_folder,
            paths=paths,
            store=store,
            max_upload_bytes=settings.max_upload_bytes,
            min_age_seconds=settings.watch_folder_min_age_seconds,
        )
        processed += summary.queued
    processed += recover_retryable_chunk_failures(chunk_store)
    if process_next_chunk_job(
        chunk_store=chunk_store,
        paths=paths,
        transcriber=transcriber,
    ):
        processed += 1
    if finalize_next_recording_session(
        chunk_store=chunk_store,
        store=store,
        paths=paths,
        memo_store=memo_store,
        summarizer=summarizer,
    ):
        processed += 1
    if process_next_transcription_job(
        store=store,
        paths=paths,
        transcriber=transcriber,
        memo_store=memo_store,
        summarizer=summarizer,
    ):
        processed += 1
    if process_next_email_job(
        store=store,
        email_client=email_client,
        paths=paths,
        memo_store=memo_store,
        chunk_store=chunk_store,
        gemini_delivery_store=gemini_delivery_store,
    ):
        processed += 1
    return processed


def run_worker_once(settings: Settings) -> int:
    paths, store = build_runtime(settings)
    transcriber, email_client, summarizer = build_services(settings)
    return process_cycle(settings, paths, store, transcriber, email_client, summarizer)


def run_worker_loop(settings: Settings) -> None:
    paths, store = build_runtime(settings)
    with _exclusive_worker_lock(paths.state / "worker.lock"):
        log_worker_start(settings.server_version)
        transcriber, email_client, summarizer = build_services(settings)
        while True:
            processed = process_cycle(settings, paths, store, transcriber, email_client, summarizer)
            if processed == 0:
                time.sleep(settings.worker_poll_seconds)


@contextmanager
def _exclusive_worker_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    acquired = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            raise RuntimeError("another transcription worker is already running") from exc
        yield
    finally:
        try:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def send_test_email(settings: Settings) -> int:
    paths, _ = build_runtime(settings)
    email_client = build_email_client(settings)
    email_client.send_text(
        "Watch Audio Pipeline SMTP test",
        "SMTP is configured for the Watch Audio Pipeline.",
    )
    return 1


def retry_failed_email_once(settings: Settings) -> int:
    paths, store = build_runtime(settings)
    email_client = build_email_client(settings)
    return 1 if process_next_email_job(
        store=store,
        email_client=email_client,
        include_failed=True,
        paths=paths,
        memo_store=MemoStore(paths.database),
        chunk_store=ChunkStore(paths.database),
        gemini_delivery_store=(
            GeminiDeliveryStore(paths.database) if settings.gemini_enabled else None
        ),
    ) else 0


def run_gemini_worker_once(settings: Settings) -> int:
    if not settings.gemini_enabled:
        return 0
    paths, _ = build_runtime(settings)
    delivery_store = GeminiDeliveryStore(paths.database)
    client = build_gemini_client(settings)
    notifier = build_notifier(settings)
    try:
        return 1 if process_next_gemini_delivery(
            store=delivery_store,
            client=client,
            max_retries=settings.gemini_max_retries,
            retry_base_seconds=settings.gemini_retry_base_seconds,
            notifier=notifier,
        ) else 0
    finally:
        client.close()


def run_gemini_worker_loop(settings: Settings) -> None:
    if not settings.gemini_enabled:
        raise ValueError("Gemini delivery is disabled")
    paths, _ = build_runtime(settings)
    delivery_store = GeminiDeliveryStore(paths.database)
    delivery_store.recover_pre_submission_claims()
    client = build_gemini_client(settings)
    notifier = build_notifier(settings)
    try:
        while True:
            processed = process_next_gemini_delivery(
                store=delivery_store,
                client=client,
                max_retries=settings.gemini_max_retries,
                retry_base_seconds=settings.gemini_retry_base_seconds,
                notifier=notifier,
            )
            if processed is None:
                time.sleep(settings.gemini_poll_seconds)
    finally:
        client.close()


def open_gemini_login(settings: Settings) -> int:
    client = build_gemini_client(settings)
    client.open_login()
    return 0


def check_gemini_login(settings: Settings) -> int:
    paths, _ = build_runtime(settings)
    client = build_gemini_client(settings)
    if not client.check_authentication():
        return 2
    GeminiDeliveryStore(paths.database).requeue_authentication_required()
    return 0


def retry_failed_gemini(settings: Settings) -> int:
    paths, _ = build_runtime(settings)
    return GeminiDeliveryStore(paths.database).requeue_failed()


def main(
    argv=None,
    *,
    serve_fn=serve,
    worker_once_fn=run_worker_once,
    worker_loop_fn=run_worker_loop,
    test_email_fn=send_test_email,
    retry_email_fn=retry_failed_email_once,
    gemini_once_fn=run_gemini_worker_once,
    gemini_loop_fn=run_gemini_worker_loop,
    gemini_login_fn=open_gemini_login,
    gemini_check_fn=check_gemini_login,
    gemini_retry_fn=retry_failed_gemini,
) -> int:
    parser = argparse.ArgumentParser(prog="watch-audio-pipeline")
    parser.add_argument("--runtime-version", default="development")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    subparsers.add_parser("work-once")
    subparsers.add_parser("worker")
    subparsers.add_parser("send-test-email")
    subparsers.add_parser("retry-email-failed")
    subparsers.add_parser("gemini-once")
    subparsers.add_parser("gemini-worker")
    subparsers.add_parser("gemini-login")
    subparsers.add_parser("gemini-check")
    subparsers.add_parser("gemini-retry-failed")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "serve":
        serve_fn(settings)
        return 0
    if args.command == "work-once":
        worker_once_fn(settings)
        return 0
    if args.command == "send-test-email":
        test_email_fn(settings)
        return 0
    if args.command == "retry-email-failed":
        retry_email_fn(settings)
        return 0
    if args.command == "gemini-once":
        gemini_once_fn(settings)
        return 0
    if args.command == "gemini-login":
        return gemini_login_fn(settings)
    if args.command == "gemini-check":
        return gemini_check_fn(settings)
    if args.command == "gemini-retry-failed":
        gemini_retry_fn(settings)
        return 0
    if args.command == "gemini-worker":
        gemini_loop_fn(settings)
        return 0

    worker_loop_fn(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
