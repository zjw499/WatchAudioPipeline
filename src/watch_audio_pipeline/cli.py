import argparse
import time

import uvicorn

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.config import Settings, load_settings
from watch_audio_pipeline.emailer import SmtpEmailClient
from watch_audio_pipeline.logging_utils import configure_logging
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.transcription import FasterWhisperTranscriber
from watch_audio_pipeline.worker import process_next_email_job, process_next_transcription_job


def build_runtime(settings: Settings):
    paths = ensure_directories(build_paths(settings))
    configure_logging(paths.logs)
    store = JobStore(paths.database)
    return paths, store


def serve(settings: Settings) -> None:
    paths, store = build_runtime(settings)
    app = create_app(settings, paths, store)
    uvicorn.run(app, host=settings.host, port=settings.port)


def build_services(settings: Settings):
    transcriber = FasterWhisperTranscriber(settings.whisper_model, settings.whisper_device)
    email_client = SmtpEmailClient(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        from_address=settings.smtp_from,
        to_address=settings.smtp_to,
    )
    return transcriber, email_client


def process_cycle(settings: Settings, paths, store, transcriber, email_client) -> int:
    processed = 0
    if process_next_transcription_job(store=store, paths=paths, transcriber=transcriber):
        processed += 1
    if process_next_email_job(store=store, email_client=email_client):
        processed += 1
    return processed


def run_worker_once(settings: Settings) -> int:
    paths, store = build_runtime(settings)
    transcriber, email_client = build_services(settings)
    return process_cycle(settings, paths, store, transcriber, email_client)


def run_worker_loop(settings: Settings) -> None:
    paths, store = build_runtime(settings)
    transcriber, email_client = build_services(settings)
    while True:
        processed = process_cycle(settings, paths, store, transcriber, email_client)
        if processed == 0:
            time.sleep(settings.worker_poll_seconds)


def main(
    argv=None,
    *,
    serve_fn=serve,
    worker_once_fn=run_worker_once,
    worker_loop_fn=run_worker_loop,
) -> int:
    parser = argparse.ArgumentParser(prog="watch-audio-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("serve")
    subparsers.add_parser("work-once")
    subparsers.add_parser("worker")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command == "serve":
        serve_fn(settings)
        return 0
    if args.command == "work-once":
        worker_once_fn(settings)
        return 0

    worker_loop_fn(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
