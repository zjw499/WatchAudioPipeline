from __future__ import annotations

import logging
from urllib.request import Request, urlopen


notification_logger = logging.getLogger("notification")


class NtfyNotifier:
    def __init__(self, url: str, *, timeout_seconds: int = 10, opener=urlopen):
        self.url = url.strip()
        self.timeout_seconds = timeout_seconds
        self.opener = opener
        if not self.url.startswith("https://"):
            raise ValueError("ntfy URL must use HTTPS")

    def notify_okta_reverification_required(self) -> None:
        request = Request(
            self.url,
            data=(
                b"Gemini delivery is paused. Open the PC and complete the department "
                b"Okta sign-in. Queued transcripts remain saved locally."
            ),
            headers={
                "Title": "Codex Watch needs Okta verification",
                "Priority": "urgent",
                "Tags": "warning,locked_with_key",
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            status = getattr(response, "status", 200)
            if status < 200 or status >= 300:
                raise RuntimeError(f"ntfy returned HTTP {status}")
        notification_logger.info("sent Okta re-verification notification")
