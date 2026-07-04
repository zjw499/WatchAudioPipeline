import io
import inspect

import pytest
from fastapi.testclient import TestClient

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore


@pytest.mark.parametrize("upload_token", ["", "replace-me"])
def test_create_app_rejects_placeholder_upload_token(tmp_path, upload_token):
    settings = Settings(project_root=tmp_path, upload_token=upload_token)
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)

    with pytest.raises(ValueError, match="upload token"):
        create_app(settings, paths, store)


def test_upload_route_is_synchronous(app_parts):
    _, _, _, client = app_parts

    upload_route = next(route for route in client.app.routes if getattr(route, "path", None) == "/upload")

    assert not inspect.iscoroutinefunction(upload_route.endpoint)


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


def test_upload_accepts_form_token_for_apps_without_custom_headers(app_parts):
    _, paths, store, client = app_parts

    response = client.post(
        "/upload",
        data={"source": "voice-record-pro", "upload_token": "test-token"},
        files={"file": ("note.m4a", io.BytesIO(b"voice-record-pro-body"), "audio/mp4")},
    )

    payload = response.json()

    assert response.status_code == 201
    assert payload["status"] == "queued"
    assert store.count_jobs() == 1
    assert (paths.incoming / payload["stored_filename"]).is_file()
