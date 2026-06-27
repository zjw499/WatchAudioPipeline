from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: str | None = None
    duration_seconds: float | None = None


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        ...


class FasterWhisperTranscriber:
    def __init__(self, model_name: str, device: str) -> None:
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model_name, device=device, compute_type="int8")

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        segments, info = self.model.transcribe(str(audio_path), vad_filter=True)
        text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
        return TranscriptResult(
            text=text.strip(),
            language=getattr(info, "language", None),
            duration_seconds=getattr(info, "duration", None),
        )
