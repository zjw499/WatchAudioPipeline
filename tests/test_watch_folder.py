import os
import time

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.watch_folder import import_ready_audio_files


def test_import_ready_audio_files_queues_copy_and_leaves_original(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    watch_folder = tmp_path / "Downloads"
    watch_folder.mkdir()
    audio_path = watch_folder / "taildrop.m4a"
    audio_path.write_bytes(b"audio-data")
    old_time = time.time() - 60
    os.utime(audio_path, (old_time, old_time))

    summary = import_ready_audio_files(
        watch_folder=watch_folder,
        paths=paths,
        store=store,
        max_upload_bytes=settings.max_upload_bytes,
        min_age_seconds=10,
    )

    queued_jobs = store.list_jobs_by_status("queued")

    assert summary.queued == 1
    assert summary.duplicate == 0
    assert len(queued_jobs) == 1
    assert queued_jobs[0].source == "tailscale-taildrop"
    assert queued_jobs[0].original_filename == "taildrop.m4a"
    assert (paths.incoming / queued_jobs[0].stored_filename).read_bytes() == b"audio-data"
    assert audio_path.exists()


def test_import_ready_audio_files_skips_previously_imported_file(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    watch_folder = tmp_path / "Downloads"
    watch_folder.mkdir()
    audio_path = watch_folder / "taildrop.m4a"
    audio_path.write_bytes(b"audio-data")
    old_time = time.time() - 60
    os.utime(audio_path, (old_time, old_time))

    first = import_ready_audio_files(
        watch_folder=watch_folder,
        paths=paths,
        store=store,
        max_upload_bytes=settings.max_upload_bytes,
        min_age_seconds=10,
    )
    second = import_ready_audio_files(
        watch_folder=watch_folder,
        paths=paths,
        store=store,
        max_upload_bytes=settings.max_upload_bytes,
        min_age_seconds=10,
    )

    assert first.queued == 1
    assert second.queued == 0
    assert second.skipped == 1
    assert store.count_jobs() == 1


def test_import_ready_audio_files_skips_fresh_file(tmp_path):
    settings = Settings(project_root=tmp_path)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    watch_folder = tmp_path / "Downloads"
    watch_folder.mkdir()
    audio_path = watch_folder / "taildrop.m4a"
    audio_path.write_bytes(b"audio-data")

    summary = import_ready_audio_files(
        watch_folder=watch_folder,
        paths=paths,
        store=store,
        max_upload_bytes=settings.max_upload_bytes,
        min_age_seconds=10,
    )

    assert summary.queued == 0
    assert summary.skipped == 1
    assert store.count_jobs() == 0
