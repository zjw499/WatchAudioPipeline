from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from watch_audio_pipeline.gemini_delivery import (
    GeminiAuthenticationRequired,
    GeminiBrowserClient,
    GeminiDeliveryStore,
    GeminiSubmissionNotStarted,
    GeminiTrafficChallengeRequired,
    process_next_gemini_delivery,
)


class FakeGeminiClient:
    def __init__(self, store=None, job_id=None) -> None:
        self.store = store
        self.job_id = job_id
        self.prepared = []
        self.closed = 0

    def prepare(self, transcript: str) -> None:
        self.prepared.append(transcript)

    def submit(self) -> str:
        if self.store is not None:
            assert self.store.get(self.job_id).status == "submitting"
        return "https://gemini.google.com/app/conversation-123"

    def close(self) -> None:
        self.closed += 1


class PrepareFailureClient(FakeGeminiClient):
    def prepare(self, transcript: str) -> None:
        raise RuntimeError("browser unavailable")


class AuthenticationRequiredClient(FakeGeminiClient):
    def prepare(self, transcript: str) -> None:
        raise GeminiAuthenticationRequired("Okta session expired")


class TrafficChallengeClient(FakeGeminiClient):
    def prepare(self, transcript: str) -> None:
        raise GeminiTrafficChallengeRequired("Google verification required")


class SubmitFailureClient(FakeGeminiClient):
    def submit(self) -> str:
        raise RuntimeError("confirmation timed out")


class SubmissionNotStartedClient(FakeGeminiClient):
    def submit(self) -> str:
        raise GeminiSubmissionNotStarted("send control did not accept the prompt")


class SubmitAuthenticationRequiredClient(FakeGeminiClient):
    def submit(self) -> str:
        raise GeminiAuthenticationRequired("Google verification required")


class AutoVerificationClient(AuthenticationRequiredClient):
    def __init__(self) -> None:
        super().__init__()
        self.login_windows = 0
        self.authentication_checks = 0

    def open_login(self) -> None:
        self.login_windows += 1

    def check_authentication(self) -> bool:
        self.authentication_checks += 1
        return True


class FakePromptLocator:
    def __init__(self, text_length: int) -> None:
        self.text_length = text_length

    def evaluate(self, _script: str) -> int:
        return self.text_length


class FakeResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.url = (
            "https://gemini.google.com/_/BardChatUi/data/"
            "assistant.lamda.BardFrontendService/StreamGenerate"
        )


class FakeSendButton:
    def __init__(self, page) -> None:
        self.page = page
        self.waited = False
        self.clicked = False

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout in (0, 1000)
        self.waited = True

    def click(self, *, timeout: int) -> None:
        assert timeout in (0, 1000)
        self.clicked = True
        if self.page.prompt_locator is not None:
            self.page.prompt_locator.text_length = 0
        if self.page.generation_status is not None:
            self.page.emit_response(FakeResponse(self.page.generation_status))
        if self.page.navigate_on_click:
            self.page.url = "https://gemini.google.com/gem/gem-123/conversation-456"


class FakeGeminiPage:
    def __init__(
        self,
        *,
        navigate_on_click: bool = True,
        generation_status: int | None = None,
    ) -> None:
        self.url = "https://gemini.google.com/gem/gem-123"
        self.navigate_on_click = navigate_on_click
        self.generation_status = generation_status
        self.prompt_locator = None
        self.response_handlers = []
        self.send_button = FakeSendButton(self)

    def get_by_role(self, role: str, *, name: str, exact: bool):
        assert role == "button"
        assert name == "Send message"
        assert exact is True
        return self.send_button

    def wait_for_timeout(self, _timeout: int) -> None:
        pass

    def on(self, event: str, handler) -> None:
        assert event == "response"
        self.response_handlers.append(handler)

    def remove_listener(self, event: str, handler) -> None:
        assert event == "response"
        self.response_handlers.remove(handler)

    def emit_response(self, response) -> None:
        for handler in self.response_handlers:
            handler(response)


class FakeReusablePage:
    def __init__(self, url: str) -> None:
        self.url = url
        self.goto_calls = []

    def is_closed(self) -> bool:
        return False

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))
        self.url = url


class FakeNotifier:
    def __init__(self, *, fail=False):
        self.calls = 0
        self.fail = fail

    def notify_okta_reverification_required(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("ntfy unavailable")


def queue_delivery(tmp_path: Path):
    store = GeminiDeliveryStore(tmp_path / "state.sqlite3")
    transcript_path = tmp_path / "transcript.txt"
    transcript_path.write_text("Patient encounter transcript", encoding="utf-8")
    delivery = store.enqueue("job-1", transcript_path)
    return store, delivery


def test_enqueue_is_idempotent(tmp_path):
    store, first = queue_delivery(tmp_path)
    second = store.enqueue("job-1", tmp_path / "different.txt")

    assert first == second
    assert second.status == "queued"
    assert second.attempts == 0


def test_successful_delivery_crosses_submission_boundary_once(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = FakeGeminiClient(store, "job-1")

    processed = process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
    )

    saved = store.get("job-1")
    assert processed == "job-1"
    assert saved.status == "delivered"
    assert saved.attempts == 1
    assert saved.conversation_url == "https://gemini.google.com/app/conversation-123"
    assert client.prepared == ["Patient encounter transcript"]
    assert client.closed == 0


def test_pre_submission_failure_is_scheduled_for_retry(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = PrepareFailureClient()

    processed = process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
    )

    saved = store.get("job-1")
    assert processed is None
    assert saved.status == "retry_wait"
    assert saved.attempts == 1
    assert saved.next_attempt_at is not None
    assert client.closed == 1


def test_expired_login_pauses_without_automatic_retry(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = AuthenticationRequiredClient()
    notifier = FakeNotifier()

    process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
    )

    saved = store.get("job-1")
    assert saved.status == "authentication_required"
    assert saved.attempts == 1
    assert saved.next_attempt_at is None
    assert store.claim_next(5) is None
    assert notifier.calls == 1


def test_authentication_episode_pauses_the_entire_queue(tmp_path):
    store, _ = queue_delivery(tmp_path)
    second_path = tmp_path / "second.txt"
    second_path.write_text("Second transcript", encoding="utf-8")
    store.enqueue("job-2", second_path)
    notifier = FakeNotifier()

    process_next_gemini_delivery(
        store=store,
        client=AuthenticationRequiredClient(),
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
    )
    second_client = AuthenticationRequiredClient()
    process_next_gemini_delivery(
        store=store,
        client=second_client,
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
    )

    assert notifier.calls == 1
    assert store.get("job-2").status == "queued"
    assert second_client.prepared == []
    assert store.requeue_authentication_required() == 1

    process_next_gemini_delivery(
        store=store,
        client=AuthenticationRequiredClient(),
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
    )

    assert notifier.calls == 2


def test_submission_interval_prevents_backlog_bursts(tmp_path):
    store, _ = queue_delivery(tmp_path)
    first_client = FakeGeminiClient(store, "job-1")
    assert process_next_gemini_delivery(
        store=store,
        client=first_client,
        max_retries=5,
        retry_base_seconds=30,
        min_submission_interval_seconds=120,
    ) == "job-1"

    second_path = tmp_path / "second.txt"
    second_path.write_text("Second transcript", encoding="utf-8")
    store.enqueue("job-2", second_path)
    second_client = FakeGeminiClient(store, "job-2")

    assert process_next_gemini_delivery(
        store=store,
        client=second_client,
        max_retries=5,
        retry_base_seconds=30,
        min_submission_interval_seconds=120,
    ) is None
    assert store.get("job-2").status == "queued"
    assert second_client.prepared == []
    persisted_store = GeminiDeliveryStore(store.database_path)
    assert persisted_store.seconds_until_submission_allowed(120) > 0


def test_traffic_challenge_records_persisted_cooldown(tmp_path):
    store, _ = queue_delivery(tmp_path)

    process_next_gemini_delivery(
        store=store,
        client=TrafficChallengeClient(),
        max_retries=5,
        retry_base_seconds=30,
        challenge_cooldown_seconds=(1800, 7200, 28800),
    )

    state = store.get_worker_state()
    assert state.challenge_count == 1
    assert state.last_challenge_at is not None
    assert state.blocked_until is not None
    assert datetime.fromisoformat(state.blocked_until) - datetime.fromisoformat(
        state.last_challenge_at
    ) == timedelta(seconds=1800)


def test_traffic_challenge_cooldown_escalates_and_resets(tmp_path):
    store, _ = queue_delivery(tmp_path)
    first = datetime(2026, 1, 1, tzinfo=UTC)
    cooldowns = (1800, 7200, 28800)

    first_state = store.record_traffic_challenge(
        cooldowns,
        reset_seconds=86400,
        now=first,
    )
    second_state = store.record_traffic_challenge(
        cooldowns,
        reset_seconds=86400,
        now=first + timedelta(hours=3),
    )
    third_state = store.record_traffic_challenge(
        cooldowns,
        reset_seconds=86400,
        now=first + timedelta(hours=6),
    )
    reset_state = store.record_traffic_challenge(
        cooldowns,
        reset_seconds=86400,
        now=first + timedelta(days=2),
    )

    assert first_state.challenge_count == 1
    assert datetime.fromisoformat(first_state.blocked_until) == first + timedelta(
        minutes=30
    )
    assert second_state.challenge_count == 2
    assert datetime.fromisoformat(second_state.blocked_until) == first + timedelta(
        hours=5
    )
    assert third_state.challenge_count == 3
    assert datetime.fromisoformat(third_state.blocked_until) == first + timedelta(
        hours=14
    )
    assert reset_state.challenge_count == 1


def test_ntfy_failure_does_not_requeue_or_crash_delivery(tmp_path):
    store, _ = queue_delivery(tmp_path)

    processed = process_next_gemini_delivery(
        store=store,
        client=AuthenticationRequiredClient(),
        max_retries=5,
        retry_base_seconds=30,
        notifier=FakeNotifier(fail=True),
    )

    assert processed is None
    assert store.get("job-1").status == "authentication_required"


def test_post_submission_failure_is_quarantined_not_retried(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = SubmitFailureClient()

    process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
    )

    saved = store.get("job-1")
    assert saved.status == "confirmation_needed"
    assert saved.attempts == 1
    assert saved.next_attempt_at is None
    assert store.claim_next(5) is None


def test_definitely_unsubmitted_prompt_is_scheduled_for_retry(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = SubmissionNotStartedClient()

    process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
    )

    saved = store.get("job-1")
    assert saved.status == "retry_wait"
    assert saved.attempts == 1
    assert saved.next_attempt_at is not None
    assert client.closed == 1


def test_verification_during_submit_pauses_and_notifies(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = SubmitAuthenticationRequiredClient()
    notifier = FakeNotifier()

    process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
    )

    saved = store.get("job-1")
    assert saved.status == "authentication_required"
    assert saved.attempts == 1
    assert notifier.calls == 1
    assert client.closed == 1


def test_interactive_verification_requeues_paused_delivery(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = AutoVerificationClient()
    notifier = FakeNotifier()

    process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
        auto_open_verification=True,
    )

    saved = store.get("job-1")
    assert saved.status == "queued"
    assert saved.attempts == 0
    assert notifier.calls == 1
    assert client.login_windows == 1
    assert client.authentication_checks == 1


def test_browser_client_clicks_send_and_returns_conversation_url(tmp_path):
    client = GeminiBrowserClient(
        gem_url="https://gemini.google.com/gem/gem-123",
        profile_dir=tmp_path / "profile",
        chrome_channel="chrome",
        headless=True,
        timeout_seconds=1,
    )
    page = FakeGeminiPage()
    prompt = FakePromptLocator(100)
    page.prompt_locator = prompt
    client._page = page
    client._prompt = prompt
    client._prepared_text_length = 100

    conversation_url = client.submit()

    assert conversation_url == "https://gemini.google.com/gem/gem-123/conversation-456"
    assert page.send_button.waited is True
    assert page.send_button.clicked is True
    assert client._prompt is None


def test_browser_client_retries_when_generation_never_started(tmp_path):
    client = GeminiBrowserClient(
        gem_url="https://gemini.google.com/gem/gem-123",
        profile_dir=tmp_path / "profile",
        chrome_channel="chrome",
        headless=False,
        timeout_seconds=0,
    )
    page = FakeGeminiPage(navigate_on_click=False)
    prompt = FakePromptLocator(100)
    page.prompt_locator = prompt
    client._page = page
    client._prompt = prompt
    client._prepared_text_length = 100

    with pytest.raises(GeminiSubmissionNotStarted, match="without starting generation"):
        client.submit()

    assert page.response_handlers == []


def test_browser_client_quarantines_started_generation_without_url(tmp_path):
    client = GeminiBrowserClient(
        gem_url="https://gemini.google.com/gem/gem-123",
        profile_dir=tmp_path / "profile",
        chrome_channel="chrome",
        headless=False,
        timeout_seconds=0,
    )
    page = FakeGeminiPage(navigate_on_click=False, generation_status=200)
    prompt = FakePromptLocator(100)
    page.prompt_locator = prompt
    client._page = page
    client._prompt = prompt
    client._prepared_text_length = 100

    with pytest.raises(RuntimeError, match="started generation"):
        client.submit()

    assert page.response_handlers == []


def test_browser_client_reuses_root_page_without_reloading(tmp_path):
    client = GeminiBrowserClient(
        gem_url="https://gemini.google.com/gem/gem-123",
        profile_dir=tmp_path / "profile",
        chrome_channel="chrome",
        headless=False,
        timeout_seconds=1,
    )
    page = FakeReusablePage(
        "https://gemini.google.com/gem/gem-123?utm_source=recording"
    )
    client._page = page

    assert client._open() is page
    assert page.goto_calls == []

    page.url = "https://gemini.google.com/gem/gem-123/conversation-456"
    assert client._open() is page
    assert page.goto_calls == [
        ("https://gemini.google.com/gem/gem-123", "domcontentloaded", 1000)
    ]


def test_uncertain_delivery_can_be_confirmed_without_resubmission(tmp_path):
    store, _ = queue_delivery(tmp_path)
    client = SubmitFailureClient()
    process_next_gemini_delivery(
        store=store,
        client=client,
        max_retries=5,
        retry_base_seconds=30,
    )

    conversation_url = "https://gemini.google.com/gem/gem-123/conversation-456"
    store.confirm_delivery("job-1", conversation_url)

    saved = store.get("job-1")
    assert saved.status == "delivered"
    assert saved.conversation_url == conversation_url
    assert saved.attempts == 1


def test_worker_restart_recovers_only_pre_submission_claims(tmp_path):
    store, _ = queue_delivery(tmp_path)
    claimed = store.claim_next(5)
    assert claimed.status == "sending"

    assert store.recover_pre_submission_claims() == 1
    recovered = store.get("job-1")
    assert recovered.status == "retry_wait"
    assert recovered.next_attempt_at is not None
