from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys
from typing import Any, Protocol
import warnings


_NVIDIA_DLL_HANDLES = []


def _configure_nvidia_dll_paths() -> None:
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    package_root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    dll_paths = []
    for component in ("cublas", "cudnn", "cuda_nvrtc"):
        dll_directory = package_root / component / "bin"
        if dll_directory.is_dir():
            dll_paths.append(str(dll_directory))
            _NVIDIA_DLL_HANDLES.append(os.add_dll_directory(str(dll_directory)))
    if dll_paths:
        os.environ["PATH"] = os.pathsep.join(dll_paths + [os.environ.get("PATH", "")])


@dataclass(frozen=True)
class TranscriptResult:
    text: str
    language: str | None = None
    duration_seconds: float | None = None
    speaker_count: int | None = None


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SpeakerTurn:
    start: float
    end: float
    label: str


class SpeakerDiarizer(Protocol):
    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        ...


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> TranscriptResult:
        ...


class GroqWhisperTranscriber:
    def __init__(
        self,
        api_key: str,
        model_name: str = "whisper-large-v3-turbo",
        language: str | None = "en",
        timeout_seconds: int = 120,
        max_retries: int = 4,
        *,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Groq transcription requires an API key")
        if client is None:
            from groq import Groq

            client = Groq(
                api_key=api_key.strip(),
                timeout=timeout_seconds,
                max_retries=max_retries,
            )
        self.client = client
        self.model_name = model_name
        self.language = language.strip() if language and language.strip() else None

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        # watchOS may close a stream with a header-only final M4A chunk.
        if audio_path.stat().st_size < 1024:
            return TranscriptResult(
                text="",
                language=self.language,
                duration_seconds=0,
                speaker_count=None,
            )

        request: dict[str, Any] = {
            "model": self.model_name,
            "response_format": "verbose_json",
            "temperature": 0.0,
        }
        if self.language:
            request["language"] = self.language

        with audio_path.open("rb") as audio_file:
            response = self.client.audio.transcriptions.create(
                file=(audio_path.name, audio_file.read()),
                **request,
            )

        return TranscriptResult(
            text=str(getattr(response, "text", "")).strip(),
            language=getattr(response, "language", self.language),
            duration_seconds=getattr(response, "duration", None),
            speaker_count=None,
        )


class FasterWhisperTranscriber:
    def __init__(
        self,
        model_name: str,
        device: str,
        compute_type: str = "int8",
        cpu_threads: int = 0,
        num_workers: int = 1,
        speaker_diarizer: SpeakerDiarizer | None = None,
    ) -> None:
        if device.lower().startswith("cuda"):
            _configure_nvidia_dll_paths()
        from faster_whisper import WhisperModel

        self.model = WhisperModel(
            model_name,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
            num_workers=num_workers,
        )
        self.speaker_diarizer = speaker_diarizer

    def transcribe(self, audio_path: Path) -> TranscriptResult:
        diarization_pool = None
        diarization_future = None
        if self.speaker_diarizer is not None:
            # Diarization runs on CPU while Whisper uses the GPU.
            diarization_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diarization")
            diarization_future = diarization_pool.submit(self.speaker_diarizer.diarize, audio_path)
        try:
            segments, info = self.model.transcribe(str(audio_path), vad_filter=True)
            transcript_segments = [
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=segment.text.strip(),
                )
                for segment in segments
                if segment.text.strip()
            ]
            speaker_turns = diarization_future.result() if diarization_future is not None else None
        finally:
            if diarization_pool is not None:
                diarization_pool.shutdown(wait=True)
        if self.speaker_diarizer is None:
            text = " ".join(segment.text for segment in transcript_segments)
            speaker_count = None
        else:
            assert speaker_turns is not None
            text = render_speaker_transcript(transcript_segments, speaker_turns)
            speaker_count = len({turn.label for turn in speaker_turns}) or None
        return TranscriptResult(
            text=text.strip(),
            language=getattr(info, "language", None),
            duration_seconds=getattr(info, "duration", None),
            speaker_count=speaker_count,
        )


class PyannoteSpeakerDiarizer:
    """Run the open-source pyannote pipeline locally and return anonymous turns."""

    def __init__(
        self,
        model_name: str,
        token: str,
        device: str = "cpu",
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> None:
        try:
            os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
            import torch
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="torchcodec is not installed correctly.*",
                    category=UserWarning,
                )
                from pyannote.audio import Pipeline
        except ImportError as exc:
            raise RuntimeError(
                "Speaker diarization is enabled but pyannote.audio is not installed. "
                "Install the diarization extra before starting the worker."
            ) from exc

        if not token.strip():
            raise ValueError(
                "Speaker diarization requires a Hugging Face access token after accepting "
                "the pyannote model terms."
            )
        if (
            min_speakers is not None
            and max_speakers is not None
            and min_speakers > max_speakers
        ):
            raise ValueError("diarization_min_speakers cannot exceed diarization_max_speakers")

        self.torch = torch
        self.pipeline = Pipeline.from_pretrained(model_name, token=token.strip())
        if device.lower() != "cpu":
            self.pipeline.to(torch.device(device))
        self.min_speakers = min_speakers
        self.max_speakers = max_speakers

    def diarize(self, audio_path: Path) -> list[SpeakerTurn]:
        options: dict[str, int] = {}
        if self.min_speakers is not None:
            options["min_speakers"] = self.min_speakers
        if self.max_speakers is not None:
            options["max_speakers"] = self.max_speakers

        output = self.pipeline(_decode_audio_for_diarization(audio_path, self.torch), **options)
        annotation = getattr(output, "exclusive_speaker_diarization", None)
        if annotation is None:
            annotation = output.speaker_diarization
        return _speaker_turns_from_annotation(annotation)


def render_speaker_transcript(
    segments: list[TranscriptSegment] | tuple[TranscriptSegment, ...],
    speaker_turns: list[SpeakerTurn] | tuple[SpeakerTurn, ...],
) -> str:
    """Attach stable anonymous speaker labels to Whisper's timestamped segments."""
    if not speaker_turns:
        return " ".join(segment.text for segment in segments).strip()

    labels: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    for segment in segments:
        speaker = _speaker_for_segment(segment, speaker_turns)
        display_label = labels.setdefault(speaker, f"Speaker {len(labels) + 1}")
        if lines and lines[-1][0] == display_label:
            lines[-1] = (display_label, f"{lines[-1][1]} {segment.text}")
        else:
            lines.append((display_label, segment.text))

    return "\n".join(f"{speaker}: {text}" for speaker, text in lines).strip()


def _speaker_for_segment(
    segment: TranscriptSegment,
    speaker_turns: list[SpeakerTurn] | tuple[SpeakerTurn, ...],
) -> str:
    midpoint = (segment.start + segment.end) / 2
    overlaps = [
        (min(segment.end, turn.end) - max(segment.start, turn.start), turn.label)
        for turn in speaker_turns
        if min(segment.end, turn.end) > max(segment.start, turn.start)
    ]
    if overlaps:
        return max(overlaps, key=lambda item: item[0])[1]

    nearest = min(
        speaker_turns,
        key=lambda turn: min(abs(midpoint - turn.start), abs(midpoint - turn.end)),
    )
    return nearest.label


def _speaker_turns_from_annotation(annotation: Any) -> list[SpeakerTurn]:
    if not hasattr(annotation, "itertracks"):
        raise TypeError("The diarization pipeline returned an unsupported annotation type")

    return [
        SpeakerTurn(start=float(turn.start), end=float(turn.end), label=str(label))
        for turn, _, label in annotation.itertracks(yield_label=True)
        if turn.end > turn.start
    ]


def _decode_audio_for_diarization(audio_path: Path, torch: Any) -> dict[str, Any]:
    """Decode through PyAV so Windows does not depend on TorchCodec DLL loading."""
    import av
    import numpy as np

    with av.open(str(audio_path)) as container:
        audio_stream = next((stream for stream in container.streams if stream.type == "audio"), None)
        if audio_stream is None:
            raise ValueError(f"audio stream not found in {audio_path.name}")

        resampler = av.AudioResampler(format="fltp", layout="mono", rate=16_000)
        chunks = []
        for frame in container.decode(audio=audio_stream.index):
            chunks.extend(resampler.resample(frame))
        chunks.extend(resampler.resample(None))

    arrays = []
    for frame in chunks:
        array = frame.to_ndarray()
        if array.ndim == 1:
            array = array[None, :]
        arrays.append(array.astype(np.float32, copy=False))

    if not arrays:
        raise ValueError(f"audio stream contained no samples in {audio_path.name}")

    waveform = np.concatenate(arrays, axis=1)
    return {"waveform": torch.from_numpy(waveform), "sample_rate": 16_000}
