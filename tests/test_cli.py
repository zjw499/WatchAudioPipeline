from watch_audio_pipeline.cli import main
from watch_audio_pipeline.cli import serve
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
            upload_token="test-token",
            ssl_certfile=cert_path,
            ssl_keyfile=key_path,
        )
    )

    assert captured["ssl_certfile"] == str(cert_path)
    assert captured["ssl_keyfile"] == str(key_path)
