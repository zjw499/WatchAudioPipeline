from watch_audio_pipeline.summarization import OllamaSummarizer, SummaryResult


def test_long_transcript_includes_final_segment_and_its_action_item():
    class Summarizer(OllamaSummarizer):
        def __init__(self):
            super().__init__(host="http://localhost", model="test", max_transcript_chars=2000)
            self.inputs = []

        def _summarize_window(self, text, fallback_title):
            self.inputs.append(text)
            return SummaryResult(
                "Meeting", "Team reviewed the plan.",
                action_items=("Send the final report",) if "FINAL_ACTION" in text else (),
            )

    summarizer = Summarizer()
    result = summarizer.summarize("Discussion " * 1200 + " FINAL_ACTION", "Meeting")
    assert any("FINAL_ACTION" in text for text in summarizer.inputs)
    assert result.action_items == ("Send the final report",)
