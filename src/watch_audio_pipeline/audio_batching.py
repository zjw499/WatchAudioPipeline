from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess


@dataclass(frozen=True)
class PreparedAudioBatch:
    path: Path
    duration_seconds: float
    overlap_seconds: float
    is_silent: bool


class InvalidAudioChunks(ValueError):
    def __init__(self, paths: list[Path]):
        self.paths = tuple(paths)
        super().__init__(
            "invalid audio chunk(s): " + ", ".join(path.name for path in paths)
        )


def _resolve_executable(configured: str, name: str) -> str:
    if configured.strip():
        path = Path(configured).expanduser()
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"configured {name} executable was not found: {path}")

    discovered = shutil.which(name)
    if discovered:
        return discovered

    executable = f"{name}.exe" if not name.endswith(".exe") else name
    package_root = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet" / "Packages"
    matches = sorted(package_root.glob(f"Gyan.FFmpeg*/ffmpeg-*/bin/{executable}"))
    if matches:
        return str(matches[-1])
    raise FileNotFoundError(f"{name} is required for streamed transcription batching")


class FFmpegAudioBatcher:
    def __init__(
        self,
        *,
        ffmpeg_path: str = "",
        ffprobe_path: str = "",
        overlap_seconds: float = 2.0,
        silence_max_db: float = -50.0,
    ):
        self.ffmpeg = _resolve_executable(ffmpeg_path, "ffmpeg")
        self.ffprobe = _resolve_executable(ffprobe_path, "ffprobe")
        self.overlap_seconds = max(0.0, overlap_seconds)
        self.silence_max_db = silence_max_db

    def prepare(
        self,
        audio_paths: list[Path],
        output_path: Path,
        *,
        overlap_source: Path | None = None,
    ) -> PreparedAudioBatch:
        invalid = [path for path in audio_paths if not self._is_valid_audio(path)]
        if overlap_source is not None and not self._is_valid_audio(overlap_source):
            overlap_source = None
        if invalid:
            raise InvalidAudioChunks(invalid)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        list_path = output_path.with_suffix(".concat.txt")
        overlap_path = output_path.with_suffix(".overlap.m4a")
        inputs = list(audio_paths)
        applied_overlap = 0.0
        try:
            if overlap_source is not None and self.overlap_seconds > 0:
                self._run(
                    [
                        self.ffmpeg,
                        "-y",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-sseof",
                        f"-{self.overlap_seconds:g}",
                        "-i",
                        str(overlap_source),
                        "-map",
                        "0:a:0",
                        "-vn",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "48k",
                        str(overlap_path),
                    ]
                )
                inputs.insert(0, overlap_path)
                applied_overlap = min(
                    self.overlap_seconds,
                    self._duration(overlap_path),
                )

            list_path.write_text(
                "".join(f"file '{self._concat_path(path)}'\n" for path in inputs),
                encoding="utf-8",
            )
            self._run(
                [
                    self.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(list_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "48k",
                    "-movflags",
                    "+faststart",
                    str(output_path),
                ]
            )
            duration = self._duration(output_path)
            return PreparedAudioBatch(
                path=output_path,
                duration_seconds=duration,
                overlap_seconds=applied_overlap,
                is_silent=self._is_confirmed_silence(output_path),
            )
        finally:
            list_path.unlink(missing_ok=True)
            overlap_path.unlink(missing_ok=True)

    def _is_valid_audio(self, path: Path) -> bool:
        try:
            result = self._run(
                [
                    self.ffprobe,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "json",
                    str(path),
                ]
            )
            streams = json.loads(result.stdout or "{}").get("streams", [])
            return bool(streams and streams[0].get("codec_type") == "audio")
        except (OSError, ValueError, subprocess.CalledProcessError):
            return False

    def _duration(self, path: Path) -> float:
        result = self._run(
            [
                self.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
        return max(0.0, float(result.stdout.strip()))

    def _is_confirmed_silence(self, path: Path) -> bool:
        result = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        match = re.search(r"max_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", result.stderr)
        if not match:
            return False
        value = match.group(1).lower()
        return value in {"-inf", "inf"} or float(value) <= self.silence_max_db

    @staticmethod
    def _concat_path(path: Path) -> str:
        return str(path.resolve()).replace("\\", "/").replace("'", "'\\''")

    @staticmethod
    def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=True)
