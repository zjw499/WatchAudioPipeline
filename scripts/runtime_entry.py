"""Launch a pipeline command from one explicitly selected release."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys


def load_cli(source_root: Path):
    source_root = source_root.resolve()
    package_root = source_root / "watch_audio_pipeline"
    if not package_root.is_dir():
        raise RuntimeError(f"pipeline package was not found under {source_root}")

    sys.path.insert(0, str(source_root))
    importlib.invalidate_caches()
    package = importlib.import_module("watch_audio_pipeline")
    package_path = Path(package.__file__).resolve()
    if not package_path.is_relative_to(package_root):
        raise RuntimeError(
            f"selected release is {source_root}, but Python imported {package_path}"
        )
    return importlib.import_module("watch_audio_pipeline.cli")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="watch-audio-runtime")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("command")
    args = parser.parse_args(argv)

    os.environ["WATCH_AUDIO_SERVER_VERSION"] = args.runtime_version
    cli = load_cli(args.source_root)
    return cli.main(["--runtime-version", args.runtime_version, args.command])


if __name__ == "__main__":
    raise SystemExit(main())
