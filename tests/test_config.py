from pathlib import Path

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import build_paths, ensure_directories


def test_settings_default_project_root_is_repo_root_from_any_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    settings = Settings()

    assert settings.project_root == Path(__file__).resolve().parents[1]
    assert Settings.model_config["env_file"] == Path(__file__).resolve().parents[1] / ".env"


def test_settings_load_env_and_create_runtime_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_AUDIO_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("WATCH_AUDIO_BASIC_AUTH_USERNAME", "test-user")
    monkeypatch.setenv("WATCH_AUDIO_BASIC_AUTH_PASSWORD", "test-password")
    monkeypatch.setenv("WATCH_AUDIO_WATCH_FOLDER_ENABLED", "true")
    monkeypatch.setenv("WATCH_AUDIO_WATCH_FOLDER", str(tmp_path / "Downloads"))

    settings = Settings()
    paths = ensure_directories(build_paths(settings))

    assert settings.basic_auth_username == "test-user"
    assert settings.basic_auth_password == "test-password"
    assert settings.watch_folder_enabled is True
    assert settings.watch_folder == tmp_path / "Downloads"
    assert paths.incoming.is_dir()
    assert paths.transcripts.is_dir()
    assert paths.failed.is_dir()
    assert paths.state.is_dir()
    assert paths.logs.is_dir()
    assert paths.database.parent == paths.state
