from watch_audio_pipeline.notifications import NtfyNotifier


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_ntfy_notification_contains_only_operational_status():
    requests = []

    def opener(request, *, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    notifier = NtfyNotifier(
        "https://ntfy.sh/test-topic",
        timeout_seconds=7,
        opener=opener,
    )
    notifier.notify_okta_reverification_required()

    request, timeout = requests[0]
    body = request.data.decode("utf-8")
    assert timeout == 7
    assert request.full_url == "https://ntfy.sh/test-topic"
    assert request.headers["Title"] == "Scribe Pilot needs Okta verification"
    assert "Okta sign-in" in body
    assert "job" not in body.lower()
    assert "patient" not in body.lower()
    assert "recording" not in body.lower()


def test_ntfy_requires_https():
    try:
        NtfyNotifier("http://ntfy.sh/test-topic")
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("HTTP ntfy URL was accepted")
