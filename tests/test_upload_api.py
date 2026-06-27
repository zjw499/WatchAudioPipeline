import io

from fastapi.testclient import TestClient

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore


def test_upload_rejects_missing_token(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "audio/mp4")},
    )

    assert response.status_code == 401


def test_upload_rejects_non_audio_mime_type(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        headers={"X-Upload-Token": "test-token"},
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "text/plain")},
    )

    assert response.status_code == 400


def test_upload_rejects_octet_stream_mime_type(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        headers={"X-Upload-Token": "test-token"},
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "application/octet-stream")},
    )

    assert response.status_code == 400


def test_upload_rejects_file_over_limit(tmp_path):
    settings = Settings(project_root=tmp_path, upload_token="test-token", max_upload_bytes=4)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    client = TestClient(create_app(settings, paths, store))

    response = client.post(
        "/upload",
        headers={"X-Upload-Token": "test-token"},
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"12345"), "audio/mp4")},
    )

    assert response.status_code == 400


def test_upload_persists_file_and_queues_job(app_parts):
    _, paths, store, client = app_parts

    response = client.post(
        "/upload",
        headers={"X-Upload-Token": "test-token"},
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"audio-body"), "audio/mp4")},
    )

    payload = response.json()

    assert response.status_code == 201
    assert payload["status"] == "queued"
    assert store.count_jobs() == 1
    assert (paths.incoming / payload["stored_filename"]).is_file()
