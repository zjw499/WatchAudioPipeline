from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories


def test_settings_load_env_and_create_runtime_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_AUDIO_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WATCH_AUDIO_UPLOAD_TOKEN", "test-token")

    settings = Settings()
    paths = ensure_directories(build_paths(settings))

    assert settings.upload_token == "test-token"
    assert paths.incoming.is_dir()
    assert paths.transcripts.is_dir()
    assert paths.failed.is_dir()
    assert paths.state.is_dir()
    assert paths.logs.is_dir()
    assert paths.database.parent == paths.state
