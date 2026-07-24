from __future__ import annotations

from dataclasses import dataclass
import json
import logging
from urllib.error import URLError
from urllib.request import Request, urlopen


summary_logger = logging.getLogger("summary")


@dataclass(frozen=True)
class SummaryResult:
    title: str
    summary: str


class OllamaSummarizer:
    """Small local-only Ollama client for memo titles and factual summaries."""

    def __init__(
        self,
        *,
        host: str,
        model: str,
        timeout_seconds: int = 120,
        max_transcript_chars: int = 80000,
    ) -> None:
        self.url = host.rstrip("/") + "/api/generate"
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_transcript_chars = max_transcript_chars

    def summarize(self, transcript: str, fallback_title: str) -> SummaryResult | None:
        if not transcript.strip():
            return None
        bounded_transcript = transcript[: self.max_transcript_chars]
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "system": (
                "You create concise memo metadata from a transcript. Use only facts in the transcript. "
                "Do not invent names, events, diagnoses, or actions. Return JSON with string fields "
                "title and summary. The title must be 3 to 8 words. The summary must be 1 to 3 short "
                "paragraphs and clearly state uncertainty when the transcript is unclear."
            ),
            "prompt": (
                "Create a useful title and factual summary for this recording. "
                "Do not mention that you are an AI.\n\nTRANSCRIPT:\n" + bounded_transcript
            ),
            "options": {"temperature": 0.1},
        }
        request = Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
            response_text = str(raw.get("response", "")).strip()
            parsed = json.loads(response_text)
            title = str(parsed.get("title", "")).strip() or fallback_title
            summary = str(parsed.get("summary", "")).strip()
            if not summary:
                return None
            return SummaryResult(title=title[:120], summary=summary)
        except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            summary_logger.warning("ollama unavailable or returned invalid summary: %s", exc)
            return None


def fallback_title(filename: str) -> str:
    name = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    for prefix in ("iphone-", "watch-", "recording-", "audio-"):
        if name.lower().startswith(prefix):
            name = name[len(prefix) :]
    words = [word for word in name.replace("_", " ").replace("-", " ").split() if word]
    return " ".join(words[:8]).strip().title() or "New Recording"
