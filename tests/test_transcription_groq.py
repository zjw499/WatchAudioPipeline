from types import SimpleNamespace

from watch_audio_pipeline.transcription import GroqWhisperTranscriber


class FakeTranscriptions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text="  Test transcript.  ", language="en", duration=12.5)


def test_groq_transcriber_uses_turbo_model_and_maps_response(tmp_path):
    transcriptions = FakeTranscriptions()
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    audio_path = tmp_path / "recording.m4a"
    audio_bytes = b"test audio" * 128
    audio_path.write_bytes(audio_bytes)
    transcriber = GroqWhisperTranscriber(
        api_key="test-key",
        model_name="whisper-large-v3-turbo",
        language="en",
        client=client,
    )

    result = transcriber.transcribe(audio_path)

    assert result.text == "Test transcript."
    assert result.language == "en"
    assert result.duration_seconds == 12.5
    assert result.speaker_count is None
    assert transcriptions.calls == [
        {
            "file": ("recording.m4a", audio_bytes),
            "model": "whisper-large-v3-turbo",
            "response_format": "verbose_json",
            "temperature": 0.0,
            "language": "en",
        }
    ]


def test_groq_transcriber_requires_an_api_key():
    try:
        GroqWhisperTranscriber(api_key="")
    except ValueError as exc:
        assert str(exc) == "Groq transcription requires an API key"
    else:
        raise AssertionError("expected an empty Groq key to be rejected")


def test_groq_transcriber_treats_header_only_chunk_as_silence(tmp_path):
    transcriptions = FakeTranscriptions()
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    audio_path = tmp_path / "final.m4a"
    audio_path.write_bytes(b"m4a header only")
    transcriber = GroqWhisperTranscriber(api_key="test-key", client=client)

    result = transcriber.transcribe(audio_path)

    assert result.text == ""
    assert result.duration_seconds == 0
    assert transcriptions.calls == []
