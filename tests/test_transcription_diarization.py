from watch_audio_pipeline.transcription import (
    SpeakerTurn,
    TranscriptSegment,
    render_speaker_transcript,
)


def test_render_speaker_transcript_assigns_and_groups_anonymous_speakers():
    segments = [
        TranscriptSegment(start=0.0, end=2.0, text="Hello there."),
        TranscriptSegment(start=2.1, end=3.0, text="How are you?"),
        TranscriptSegment(start=3.1, end=5.0, text="I am doing well."),
    ]
    turns = [
        SpeakerTurn(start=0.0, end=3.0, label="SPEAKER_00"),
        SpeakerTurn(start=3.0, end=5.0, label="SPEAKER_01"),
    ]

    assert render_speaker_transcript(segments, turns) == (
        "Speaker 1: Hello there. How are you?\n"
        "Speaker 2: I am doing well."
    )


def test_render_speaker_transcript_falls_back_to_plain_text_without_turns():
    segments = [
        TranscriptSegment(start=0.0, end=1.0, text="One."),
        TranscriptSegment(start=1.0, end=2.0, text="Two."),
    ]

    assert render_speaker_transcript(segments, []) == "One. Two."
