import pytest

from watch_audio_pipeline.cli import main
from watch_audio_pipeline.cli import serve
from watch_audio_pipeline.cli import _exclusive_worker_lock
from watch_audio_pipeline.config import Settings


def test_main_dispatches_work_once(monkeypatch, tmp_path):
    called = {"work_once": 0}
    monkeypatch.setattr(
        "watch_audio_pipeline.cli.load_settings",
        lambda: Settings(project_root=tmp_path),
    )

    exit_code = main(
        ["work-once"],
        serve_fn=lambda settings: None,
        worker_once_fn=lambda settings: called.__setitem__("work_once", called["work_once"] + 1),
        worker_loop_fn=lambda settings: None,
    )

    assert exit_code == 0
    assert called["work_once"] == 1


def test_main_accepts_runtime_version_marker(monkeypatch, tmp_path):
    called = {"worker": 0}
    monkeypatch.setattr(
        "watch_audio_pipeline.cli.load_settings",
        lambda: Settings(project_root=tmp_path),
    )

    exit_code = main(
        ["--runtime-version", "abc123", "worker"],
        worker_loop_fn=lambda settings: called.__setitem__("worker", called["worker"] + 1),
    )

    assert exit_code == 0
    assert called["worker"] == 1


def test_worker_lock_rejects_second_worker(tmp_path):
    lock_path = tmp_path / "worker.lock"

    with _exclusive_worker_lock(lock_path):
        with pytest.raises(RuntimeError, match="already running"):
            with _exclusive_worker_lock(lock_path):
                pass


def test_main_dispatches_send_test_email(monkeypatch, tmp_path):
    called = {"send_test_email": 0}
    monkeypatch.setattr(
        "watch_audio_pipeline.cli.load_settings",
        lambda: Settings(project_root=tmp_path),
    )

    exit_code = main(
        ["send-test-email"],
        serve_fn=lambda settings: None,
        worker_once_fn=lambda settings: None,
        worker_loop_fn=lambda settings: None,
        test_email_fn=lambda settings: called.__setitem__(
            "send_test_email",
            called["send_test_email"] + 1,
        ),
        retry_email_fn=lambda settings: None,
    )

    assert exit_code == 0
    assert called["send_test_email"] == 1


def test_main_dispatches_retry_email_failed(monkeypatch, tmp_path):
    called = {"retry_email": 0}
    monkeypatch.setattr(
        "watch_audio_pipeline.cli.load_settings",
        lambda: Settings(project_root=tmp_path),
    )

    exit_code = main(
        ["retry-email-failed"],
        serve_fn=lambda settings: None,
        worker_once_fn=lambda settings: None,
        worker_loop_fn=lambda settings: None,
        test_email_fn=lambda settings: None,
        retry_email_fn=lambda settings: called.__setitem__(
            "retry_email",
            called["retry_email"] + 1,
        ),
    )

    assert exit_code == 0
    assert called["retry_email"] == 1


def test_serve_passes_https_certificate_paths(monkeypatch, tmp_path):
    captured = {}
    cert_path = tmp_path / "watch-audio.crt"
    key_path = tmp_path / "watch-audio.key"
    cert_path.write_text("certificate", encoding="utf-8")
    key_path.write_text("private key", encoding="utf-8")

    def fake_run(app, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("watch_audio_pipeline.cli.uvicorn.run", fake_run)

    serve(
        Settings(
            project_root=tmp_path,
            basic_auth_username="test-user",
            basic_auth_password="test-password",
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
        )
    )

    assert captured["ssl_certfile"] == str(cert_path)
    assert captured["ssl_keyfile"] == str(key_path)
