from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

from watch_audio_pipeline.db import connect, init_db


gemini_logger = logging.getLogger("gemini")

_CONVERSATION_URL_RE = re.compile(
    r"https://gemini\.google\.com/"
    r"(?:app/[^/?#]+|gem/[A-Za-z0-9_-]+/[^/?#]+)"
    r"(?:[?#].*)?$"
)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class GeminiDeliveryRecord:
    job_id: str
    transcript_path: str
    status: str
    attempts: int
    next_attempt_at: str | None
    conversation_url: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    delivered_at: str | None


@dataclass(frozen=True)
class GeminiWorkerState:
    last_submission_at: str | None
    challenge_count: int
    last_challenge_at: str | None
    blocked_until: str | None


def _row_to_delivery(row) -> GeminiDeliveryRecord:
    return GeminiDeliveryRecord(
        job_id=row["job_id"],
        transcript_path=row["transcript_path"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        conversation_url=row["conversation_url"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        delivered_at=row["delivered_at"],
    )


class GeminiDeliveryStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        init_db(self.database_path)

    def recover_pre_submission_claims(self) -> int:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'retry_wait', next_attempt_at = ?, updated_at = ?,
                    error_message = 'delivery worker stopped before submission'
                WHERE status = 'sending'
                """,
                (now, now),
            )
        connection.close()
        return cursor.rowcount

    def enqueue(self, job_id: str, transcript_path: Path) -> GeminiDeliveryRecord:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                INSERT INTO gemini_deliveries (
                    job_id, transcript_path, status, attempts, next_attempt_at,
                    conversation_url, error_message, created_at, updated_at, delivered_at
                ) VALUES (?, ?, 'queued', 0, NULL, NULL, NULL, ?, ?, NULL)
                ON CONFLICT(job_id) DO NOTHING
                """,
                (job_id, str(transcript_path), now, now),
            )
            row = connection.execute(
                "SELECT * FROM gemini_deliveries WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        connection.close()
        if row is None:
            raise RuntimeError(f"failed to queue Gemini delivery for job {job_id}")
        return _row_to_delivery(row)

    def get(self, job_id: str) -> GeminiDeliveryRecord | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM gemini_deliveries WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        connection.close()
        return _row_to_delivery(row) if row else None

    def get_worker_state(self) -> GeminiWorkerState:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM gemini_worker_state WHERE id = 1"
        ).fetchone()
        connection.close()
        if row is None:
            raise RuntimeError("Gemini worker state is not initialized")
        return GeminiWorkerState(
            last_submission_at=row["last_submission_at"],
            challenge_count=row["challenge_count"],
            last_challenge_at=row["last_challenge_at"],
            blocked_until=row["blocked_until"],
        )

    def has_authentication_required(self) -> bool:
        connection = connect(self.database_path)
        row = connection.execute(
            """
            SELECT 1 FROM gemini_deliveries
            WHERE status = 'authentication_required'
            LIMIT 1
            """
        ).fetchone()
        connection.close()
        return row is not None

    def seconds_until_submission_allowed(
        self,
        min_interval_seconds: int,
        *,
        now: datetime | None = None,
    ) -> float:
        current = now or datetime.now(UTC)
        state = self.get_worker_state()
        allowed_at = current
        if state.last_submission_at:
            allowed_at = max(
                allowed_at,
                datetime.fromisoformat(state.last_submission_at)
                + timedelta(seconds=max(0, min_interval_seconds)),
            )
        if state.blocked_until:
            allowed_at = max(allowed_at, datetime.fromisoformat(state.blocked_until))
        return max(0.0, (allowed_at - current).total_seconds())

    def mark_submission_started(self, *, now: datetime | None = None) -> None:
        submitted_at = (now or datetime.now(UTC)).isoformat()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE gemini_worker_state
                SET last_submission_at = ?
                WHERE id = 1
                """,
                (submitted_at,),
            )
        connection.close()

    def record_traffic_challenge(
        self,
        cooldown_seconds: tuple[int, ...],
        *,
        reset_seconds: int,
        now: datetime | None = None,
    ) -> GeminiWorkerState:
        if not cooldown_seconds:
            raise ValueError("at least one Gemini challenge cooldown is required")
        current = now or datetime.now(UTC)
        state = self.get_worker_state()
        previous_challenge = (
            datetime.fromisoformat(state.last_challenge_at)
            if state.last_challenge_at
            else None
        )
        if (
            previous_challenge is None
            or current - previous_challenge > timedelta(seconds=max(0, reset_seconds))
        ):
            challenge_count = 1
        else:
            challenge_count = state.challenge_count + 1
        cooldown_index = min(challenge_count - 1, len(cooldown_seconds) - 1)
        blocked_until = current + timedelta(
            seconds=max(0, cooldown_seconds[cooldown_index])
        )
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE gemini_worker_state
                SET challenge_count = ?, last_challenge_at = ?, blocked_until = ?
                WHERE id = 1
                """,
                (challenge_count, current.isoformat(), blocked_until.isoformat()),
            )
        connection.close()
        return self.get_worker_state()

    def claim_next(self, max_retries: int) -> GeminiDeliveryRecord | None:
        now = _utc_now()
        connection = connect(self.database_path)
        row = connection.execute(
            """
            SELECT * FROM gemini_deliveries
            WHERE status IN ('queued', 'retry_wait')
              AND attempts < ?
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (max_retries, now),
        ).fetchone()
        if row is None:
            connection.close()
            return None

        claimed = None
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'sending', attempts = attempts + 1,
                    next_attempt_at = NULL, error_message = NULL, updated_at = ?
                WHERE job_id = ? AND status IN ('queued', 'retry_wait')
                """,
                (now, row["job_id"]),
            )
            if cursor.rowcount:
                claimed_row = connection.execute(
                    "SELECT * FROM gemini_deliveries WHERE job_id = ?",
                    (row["job_id"],),
                ).fetchone()
                claimed = _row_to_delivery(claimed_row)
        connection.close()
        return claimed

    def mark_submitting(self, job_id: str) -> None:
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'submitting', updated_at = ?
                WHERE job_id = ? AND status = 'sending'
                """,
                (_utc_now(), job_id),
            )
        connection.close()
        if cursor.rowcount != 1:
            raise RuntimeError(f"Gemini delivery {job_id} was not ready to submit")

    def mark_delivered(self, job_id: str, conversation_url: str) -> None:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'delivered', conversation_url = ?, error_message = NULL,
                    updated_at = ?, delivered_at = ?
                WHERE job_id = ? AND status = 'submitting'
                """,
                (conversation_url, now, now, job_id),
            )
        connection.close()

    def confirm_delivery(self, job_id: str, conversation_url: str) -> None:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'delivered', conversation_url = ?, error_message = NULL,
                    updated_at = ?, delivered_at = ?
                WHERE job_id = ? AND status = 'confirmation_needed'
                """,
                (conversation_url, now, now, job_id),
            )
        connection.close()
        if cursor.rowcount != 1:
            raise RuntimeError(f"Gemini delivery {job_id} was not awaiting confirmation")

    def mark_retry(
        self,
        job_id: str,
        error_message: str,
        *,
        max_retries: int,
        retry_base_seconds: int,
    ) -> None:
        delivery = self.get(job_id)
        if delivery is None:
            return
        now = datetime.now(UTC)
        exhausted = delivery.attempts >= max_retries
        delay = retry_base_seconds * (2 ** max(0, delivery.attempts - 1))
        next_attempt_at = None if exhausted else (now + timedelta(seconds=delay)).isoformat()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = ?, next_attempt_at = ?, error_message = ?, updated_at = ?
                WHERE job_id = ? AND status = 'sending'
                """,
                (
                    "failed" if exhausted else "retry_wait",
                    next_attempt_at,
                    error_message,
                    now.isoformat(),
                    job_id,
                ),
            )
        connection.close()

    def mark_unsubmitted_retry(
        self,
        job_id: str,
        error_message: str,
        *,
        max_retries: int,
        retry_base_seconds: int,
    ) -> None:
        delivery = self.get(job_id)
        if delivery is None:
            return
        now = datetime.now(UTC)
        exhausted = delivery.attempts >= max_retries
        delay = retry_base_seconds * (2 ** max(0, delivery.attempts - 1))
        next_attempt_at = None if exhausted else (now + timedelta(seconds=delay)).isoformat()
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = ?, next_attempt_at = ?, error_message = ?, updated_at = ?
                WHERE job_id = ? AND status = 'submitting'
                """,
                (
                    "failed" if exhausted else "retry_wait",
                    next_attempt_at,
                    error_message,
                    now.isoformat(),
                    job_id,
                ),
            )
        connection.close()
        if cursor.rowcount != 1:
            raise RuntimeError(f"Gemini delivery {job_id} was not being submitted")

    def mark_confirmation_needed(self, job_id: str, error_message: str) -> None:
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'confirmation_needed', error_message = ?, updated_at = ?
                WHERE job_id = ? AND status = 'submitting'
                """,
                (error_message, _utc_now(), job_id),
            )
        connection.close()

    def mark_authentication_required(self, job_id: str, error_message: str) -> bool:
        connection = connect(self.database_path)
        with connection:
            existing_episode = connection.execute(
                """
                SELECT 1 FROM gemini_deliveries
                WHERE status = 'authentication_required' AND job_id != ?
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'authentication_required', error_message = ?, updated_at = ?
                WHERE job_id = ? AND status IN ('sending', 'submitting')
                """,
                (error_message, _utc_now(), job_id),
            )
        connection.close()
        return cursor.rowcount == 1 and existing_episode is None

    def requeue_authentication_required(self) -> int:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'queued', attempts = 0, next_attempt_at = NULL,
                    error_message = NULL, updated_at = ?
                WHERE status = 'authentication_required'
                """,
                (now,),
            )
        connection.close()
        return cursor.rowcount

    def requeue_failed(self) -> int:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE gemini_deliveries
                SET status = 'queued', attempts = 0, next_attempt_at = NULL,
                    error_message = NULL, updated_at = ?
                WHERE status = 'failed'
                """,
                (now,),
            )
        connection.close()
        return cursor.rowcount


class GeminiAuthenticationRequired(RuntimeError):
    pass


class GeminiTrafficChallengeRequired(GeminiAuthenticationRequired):
    pass


class GeminiSubmissionNotStarted(RuntimeError):
    pass


class GeminiBrowserClient:
    def __init__(
        self,
        *,
        gem_url: str,
        profile_dir: Path,
        chrome_channel: str,
        headless: bool,
        timeout_seconds: int,
    ) -> None:
        if not re.fullmatch(r"https://gemini\.google\.com/gem/[A-Za-z0-9_-]+", gem_url):
            raise ValueError("gemini_gem_url must be an exact https://gemini.google.com/gem/... URL")
        self.gem_url = gem_url
        self.profile_dir = profile_dir
        self.chrome_channel = chrome_channel
        self.headless = headless
        self.timeout_ms = timeout_seconds * 1000
        self._playwright = None
        self._context = None
        self._page = None
        self._prompt = None
        self._prepared_text_length = 0

    @staticmethod
    def _sync_playwright():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                'Gemini browser delivery requires: pip install -e ".[gemini]"'
            ) from exc
        return sync_playwright

    def _open(self, *, headless: bool | None = None):
        if self._page is not None and not self._page.is_closed():
            current_url = self._page.url
            current_base_url = current_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
            if (
                current_base_url != self.gem_url.rstrip("/")
                and "google.com/sorry" not in current_url
                and "accounts.google.com" not in current_url
            ):
                self._page.goto(
                    self.gem_url,
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
            return self._page
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = self._sync_playwright()().start()
        effective_headless = self.headless if headless is None else headless
        launch_args = (
            ["--start-minimized"]
            if not effective_headless and headless is None
            else []
        )
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=self.chrome_channel,
            headless=effective_headless,
            args=launch_args,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        for extra_page in self._context.pages[1:]:
            extra_page.close()
        self._page.goto(self.gem_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        return self._page

    def prepare(self, transcript: str) -> None:
        page = self._open()
        page.wait_for_timeout(1000)
        sign_in = page.get_by_role("button", name="Sign in")
        prompt = page.get_by_role("textbox", name="Enter a prompt for Gemini")
        if "google.com/sorry" in page.url:
            raise GeminiTrafficChallengeRequired(
                "Google paused the Gemini browser session for verification"
            )
        if (sign_in.count() and sign_in.first.is_visible()) or "gemini.google.com" not in page.url:
            raise GeminiAuthenticationRequired(
                "department Gemini authentication is required in the dedicated browser profile"
            )
        try:
            prompt.wait_for(state="visible", timeout=min(self.timeout_ms, 30_000))
        except Exception as exc:
            raise GeminiAuthenticationRequired(
                "department Gemini authentication is required in the dedicated browser profile"
            ) from exc
        prompt_text = (
            "Apply this Gem's instructions to the following recording transcript. "
            "Do not repeat this request text in the response.\n\n"
            f"{transcript.strip()}"
        )
        prompt.fill(prompt_text)
        self._prompt = prompt
        self._prepared_text_length = len(prompt_text)

    def _prompt_text_length(self) -> int:
        if self._prompt is None:
            return 0
        try:
            return int(
                self._prompt.evaluate(
                    "element => (element.innerText || element.textContent || '').trim().length"
                )
            )
        except Exception:
            return 0

    def submit(self) -> str:
        if self._page is None or self._prompt is None:
            raise RuntimeError("Gemini delivery was not prepared")
        generation_statuses: list[int] = []

        def capture_generation_response(response) -> None:
            try:
                parts = urlsplit(response.url)
                if (
                    parts.hostname == "gemini.google.com"
                    and parts.path.endswith("/StreamGenerate")
                ):
                    generation_statuses.append(response.status)
            except Exception:
                return

        self._page.on("response", capture_generation_response)
        send_button = self._page.get_by_role("button", name="Send message", exact=True)
        try:
            try:
                send_button.wait_for(
                    state="visible",
                    timeout=min(self.timeout_ms, 30_000),
                )
                send_button.click(timeout=min(self.timeout_ms, 30_000))
            except Exception as exc:
                if self._prompt_text_length() >= max(
                    1,
                    self._prepared_text_length // 2,
                ):
                    raise GeminiSubmissionNotStarted(
                        "Gemini did not accept the prompt; it remains queued for retry"
                    ) from exc
                raise

            deadline = time.monotonic() + min(self.timeout_ms, 60_000) / 1000
            while time.monotonic() < deadline:
                current_url = self._page.url
                if _CONVERSATION_URL_RE.fullmatch(current_url):
                    self._prompt = None
                    self._prepared_text_length = 0
                    return current_url
                if "google.com/sorry" in current_url:
                    raise GeminiTrafficChallengeRequired(
                        "Google paused the Gemini browser session for verification"
                    )
                if "accounts.google.com" in current_url:
                    raise GeminiAuthenticationRequired(
                        "department Gemini authentication is required in the dedicated browser profile"
                    )
                self._page.wait_for_timeout(250)

            if self._prompt_text_length() >= max(
                1,
                self._prepared_text_length // 2,
            ):
                raise GeminiSubmissionNotStarted(
                    "Gemini did not accept the prompt; it remains queued for retry"
                )
            if not any(200 <= status < 300 for status in generation_statuses):
                raise GeminiSubmissionNotStarted(
                    "Gemini cleared the prompt without starting generation; "
                    "it remains queued for retry"
                )
            raise RuntimeError(
                "Gemini started generation but did not expose a conversation URL; "
                f"current URL: {self._page.url}"
            )
        finally:
            self._page.remove_listener("response", capture_generation_response)

    def check_authentication(self) -> bool:
        try:
            page = self._open(headless=False)
            page.wait_for_timeout(1000)
            sign_in = page.get_by_role("button", name="Sign in")
            prompt = page.get_by_role("textbox", name="Enter a prompt for Gemini")
            if "gemini.google.com" not in page.url or (
                sign_in.count() and sign_in.first.is_visible()
            ):
                return False
            try:
                prompt.wait_for(state="visible", timeout=min(self.timeout_ms, 15_000))
            except Exception:
                return False
            return prompt.count() == 1 and prompt.is_visible()
        finally:
            self.close()

    def open_login(self) -> None:
        page = self._open(headless=False)
        page.bring_to_front()
        try:
            page.wait_for_event("close", timeout=0)
        finally:
            self.close()

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._context = None
        self._page = None
        self._prompt = None
        self._prepared_text_length = 0


def process_next_gemini_delivery(
    *,
    store: GeminiDeliveryStore,
    client,
    max_retries: int,
    retry_base_seconds: int,
    notifier=None,
    auto_open_verification: bool = False,
    min_submission_interval_seconds: int = 0,
    challenge_cooldown_seconds: tuple[int, ...] = (),
    challenge_reset_seconds: int = 24 * 60 * 60,
) -> str | None:
    if store.has_authentication_required():
        return None
    if store.seconds_until_submission_allowed(min_submission_interval_seconds) > 0:
        return None
    delivery = store.claim_next(max_retries)
    if delivery is None:
        return None

    try:
        transcript = Path(delivery.transcript_path).read_text(encoding="utf-8")
        if not transcript.strip():
            raise ValueError("transcript is empty")
        client.prepare(transcript)
    except GeminiAuthenticationRequired as exc:
        client.close()
        challenge_state = None
        if isinstance(exc, GeminiTrafficChallengeRequired) and challenge_cooldown_seconds:
            challenge_state = store.record_traffic_challenge(
                challenge_cooldown_seconds,
                reset_seconds=challenge_reset_seconds,
            )
        should_notify = store.mark_authentication_required(delivery.job_id, str(exc))
        gemini_logger.error(
            "Gemini authentication required job_id=%s blocked_until=%s",
            delivery.job_id,
            challenge_state.blocked_until if challenge_state else None,
        )
        if should_notify and notifier is not None:
            try:
                notifier.notify_okta_reverification_required()
            except Exception:
                gemini_logger.exception("Gemini verification notification failed")
        if should_notify and auto_open_verification:
            try:
                client.open_login()
                if client.check_authentication():
                    requeued = store.requeue_authentication_required()
                    gemini_logger.info(
                        "Gemini verification completed; requeued deliveries count=%s",
                        requeued,
                    )
            except Exception:
                gemini_logger.exception("Automatic Gemini verification window failed")
        return None
    except Exception as exc:
        client.close()
        store.mark_retry(
            delivery.job_id,
            str(exc),
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
        gemini_logger.exception(
            "Gemini delivery preparation failed job_id=%s",
            delivery.job_id,
        )
        return None

    store.mark_submitting(delivery.job_id)
    store.mark_submission_started()
    try:
        conversation_url = client.submit()
        store.mark_delivered(delivery.job_id, conversation_url)
        gemini_logger.info(
            "Gemini transcript delivered job_id=%s conversation_url=%s",
            delivery.job_id,
            conversation_url,
        )
        return delivery.job_id
    except GeminiSubmissionNotStarted as exc:
        client.close()
        store.mark_unsubmitted_retry(
            delivery.job_id,
            str(exc),
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
        )
        gemini_logger.warning(
            "Gemini submission was not started; retry scheduled job_id=%s",
            delivery.job_id,
        )
        return None
    except GeminiAuthenticationRequired as exc:
        client.close()
        challenge_state = None
        if isinstance(exc, GeminiTrafficChallengeRequired) and challenge_cooldown_seconds:
            challenge_state = store.record_traffic_challenge(
                challenge_cooldown_seconds,
                reset_seconds=challenge_reset_seconds,
            )
        should_notify = store.mark_authentication_required(delivery.job_id, str(exc))
        gemini_logger.error(
            "Gemini browser verification required during submission job_id=%s blocked_until=%s",
            delivery.job_id,
            challenge_state.blocked_until if challenge_state else None,
        )
        if should_notify and notifier is not None:
            try:
                notifier.notify_okta_reverification_required()
            except Exception:
                gemini_logger.exception("Gemini verification notification failed")
        if should_notify and auto_open_verification:
            try:
                client.open_login()
                if client.check_authentication():
                    requeued = store.requeue_authentication_required()
                    gemini_logger.info(
                        "Gemini verification completed; requeued deliveries count=%s",
                        requeued,
                    )
            except Exception:
                gemini_logger.exception("Automatic Gemini verification window failed")
        return None
    except Exception as exc:
        client.close()
        store.mark_confirmation_needed(delivery.job_id, str(exc))
        gemini_logger.exception(
            "Gemini submission requires confirmation job_id=%s",
            delivery.job_id,
        )
        return None
