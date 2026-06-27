from watch_audio_pipeline.cli import main
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
