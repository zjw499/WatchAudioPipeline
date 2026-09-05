from pathlib import Path

from watch_audio_pipeline.gemini_delivery import (
    GeminiAuthenticationRequired,
    GeminiBrowserClient,
    GeminiDeliveryStore,
    GeminiSubmissionNotStarted,
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


class FakeSendButton:
    def __init__(self, page) -> None:
        self.page = page
        self.waited = False
        self.clicked = False

    def wait_for(self, *, state: str, timeout: int) -> None:
        assert state == "visible"
        assert timeout == 1000
        self.waited = True

    def click(self, *, timeout: int) -> None:
        assert timeout == 1000
        self.clicked = True
        self.page.url = "https://gemini.google.com/gem/gem-123/conversation-456"


class FakeGeminiPage:
    def __init__(self) -> None:
        self.url = "https://gemini.google.com/gem/gem-123"
        self.send_button = FakeSendButton(self)

    def get_by_role(self, role: str, *, name: str, exact: bool):
        assert role == "button"
        assert name == "Send message"
        assert exact is True
        return self.send_button

    def wait_for_timeout(self, _timeout: int) -> None:
        pass


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


def test_okta_notification_is_sent_once_per_authentication_episode(tmp_path):
    store, _ = queue_delivery(tmp_path)
    second_path = tmp_path / "second.txt"
    second_path.write_text("Second transcript", encoding="utf-8")
    store.enqueue("job-2", second_path)
    notifier = FakeNotifier()

    for _ in range(2):
        process_next_gemini_delivery(
            store=store,
            client=AuthenticationRequiredClient(),
            max_retries=5,
            retry_base_seconds=30,
            notifier=notifier,
        )

    assert notifier.calls == 1
    assert store.requeue_authentication_required() == 2

    process_next_gemini_delivery(
        store=store,
        client=AuthenticationRequiredClient(),
        max_retries=5,
        retry_base_seconds=30,
        notifier=notifier,
    )

    assert notifier.calls == 2


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
    client._page = page
    client._prompt = FakePromptLocator(100)
    client._prepared_text_length = 100

    conversation_url = client.submit()

    assert conversation_url == "https://gemini.google.com/gem/gem-123/conversation-456"
    assert page.send_button.waited is True
    assert page.send_button.clicked is True
    assert client._prompt is None


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
