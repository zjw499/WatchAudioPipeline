import io
import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories
from watch_audio_pipeline.store import JobStore


@pytest.mark.parametrize(
    ("username", "password", "error"),
    [
        ("", "test-password", "basic auth username"),
        ("replace-me", "test-password", "basic auth username"),
        ("test-user", "", "basic auth password"),
        ("test-user", "replace-me", "basic auth password"),
    ],
)
def test_create_app_rejects_placeholder_basic_auth(tmp_path, username, password, error):
    settings = Settings(
        project_root=tmp_path,
        basic_auth_username=username,
        basic_auth_password=password,
    )
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)

    with pytest.raises(ValueError, match=error):
        create_app(settings, paths, store)


def test_upload_route_is_synchronous(app_parts):
    _, _, _, client = app_parts

    upload_route = next(route for route in client.app.routes if getattr(route, "path", None) == "/upload")

    assert not inspect.iscoroutinefunction(upload_route.endpoint)


def test_health_reports_server_and_api_versions(app_parts):
    _, _, _, client = app_parts

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "server_version": "development",
        "api_version": "1",
    }


def test_upload_rejects_missing_basic_auth(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "audio/mp4")},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_upload_rejects_invalid_basic_auth(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "wrong-password"),
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "audio/mp4")},
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"


def test_upload_rejects_non_audio_mime_type(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "text/plain")},
    )

    assert response.status_code == 400


def test_upload_rejects_octet_stream_mime_type(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "application/octet-stream")},
    )

    assert response.status_code == 400


def test_upload_rejects_file_over_limit(tmp_path):
    settings = Settings(
        project_root=tmp_path,
        basic_auth_username="test-user",
        basic_auth_password="test-password",
        max_upload_bytes=4,
    )
    paths = ensure_directories(build_paths(settings))
    store = JobStore(paths.database)
    client = TestClient(create_app(settings, paths, store))

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"12345"), "audio/mp4")},
    )

    assert response.status_code == 400


def test_upload_persists_file_and_queues_job(app_parts):
    _, paths, store, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={"source": "iphone-shortcuts"},
        files={"file": ("note.m4a", io.BytesIO(b"audio-body"), "audio/mp4")},
    )

    payload = response.json()

    assert response.status_code == 201
    assert payload["status"] == "queued"
    assert store.count_jobs() == 1
    assert (paths.incoming / payload["stored_filename"]).is_file()


def test_upload_persists_per_recording_recipient(app_parts):
    _, _, store, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={
            "source": "iphone-app",
            "client_id": "tester-client-1234",
            "recipient": "tester@example.com",
        },
        files={"file": ("note.m4a", io.BytesIO(b"tester-audio"), "audio/mp4")},
    )

    assert response.status_code == 201
    job = store.get_job(response.json()["job_id"])
    assert job is not None
    assert job.client_id == "tester-client-1234"
    assert job.recipient == "tester@example.com"


def test_upload_rejects_invalid_per_recording_recipient(app_parts):
    _, _, _, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={"recipient": "not-an-email"},
        files={"file": ("note.m4a", io.BytesIO(b"audio"), "audio/mp4")},
    )

    assert response.status_code == 400


def test_upload_accepts_basic_auth_for_apps_without_custom_headers(app_parts):
    _, paths, store, client = app_parts

    response = client.post(
        "/upload",
        auth=("test-user", "test-password"),
        data={"source": "voice-record-pro"},
        files={"file": ("note.m4a", io.BytesIO(b"voice-record-pro-body"), "audio/mp4")},
    )

    payload = response.json()

    assert response.status_code == 201
    assert payload["status"] == "queued"
    assert store.count_jobs() == 1
    assert (paths.incoming / payload["stored_filename"]).is_file()


def test_transcript_route_queues_email_without_audio(app_parts):
    _, paths, store, client = app_parts

    response = client.post(
        "/transcript",
        auth=("test-user", "test-password"),
        json={
            "filename": "watch-note.m4a",
            "source": "iphone-on-device",
            "transcript": "Transcript generated locally on the iPhone.",
        },
    )

    payload = response.json()

    assert response.status_code == 201
    assert payload["status"] == "email_queued"
    assert store.count_jobs() == 1
    job = store.get_job(payload["job_id"])
    assert job is not None
    assert job.status == "transcribed"
    assert Path(job.transcript_path).read_text(encoding="utf-8") == "Transcript generated locally on the iPhone."
    assert (paths.transcripts / f"{job.id}.txt").is_file()
