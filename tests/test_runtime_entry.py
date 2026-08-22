import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "runtime_entry.py"


def _load_runtime_entry():
    spec = importlib.util.spec_from_file_location("runtime_entry_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_runtime_entry_rejects_missing_selected_package(tmp_path):
    runtime_entry = _load_runtime_entry()

    with pytest.raises(RuntimeError, match="package was not found"):
        runtime_entry.load_cli(tmp_path)


def test_runtime_entry_imports_package_from_selected_source(tmp_path):
    runtime_entry = _load_runtime_entry()
    source = tmp_path / "release" / "src"
    package = source / "watch_audio_pipeline"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("def main(argv): return 0\n", encoding="utf-8")

    existing = {
        name: module
        for name, module in sys.modules.items()
        if name == "watch_audio_pipeline" or name.startswith("watch_audio_pipeline.")
    }
    for name in existing:
        sys.modules.pop(name, None)
    original_path = list(sys.path)
    try:
        cli = runtime_entry.load_cli(source)
        assert Path(cli.__file__).resolve().is_relative_to(source.resolve())
    finally:
        sys.path[:] = original_path
        for name in list(sys.modules):
            if name == "watch_audio_pipeline" or name.startswith("watch_audio_pipeline."):
                sys.modules.pop(name, None)
        sys.modules.update(existing)
