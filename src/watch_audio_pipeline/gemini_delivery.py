from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path
import re

from watch_audio_pipeline.db import connect, init_db


gemini_logger = logging.getLogger("gemini")


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
                WHERE job_id = ? AND status = 'sending'
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
            self._page.goto(self.gem_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            return self._page
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = self._sync_playwright()().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=self.chrome_channel,
            headless=self.headless if headless is None else headless,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.goto(self.gem_url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        return self._page

    def prepare(self, transcript: str) -> None:
        page = self._open()
        page.wait_for_timeout(1000)
        sign_in = page.get_by_role("button", name="Sign in")
        prompt = page.get_by_role("textbox", name="Enter a prompt for Gemini")
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
        prompt.fill(
            "Apply this Gem's instructions to the following recording transcript. "
            "Do not repeat this request text in the response.\n\n"
            f"{transcript.strip()}"
        )
        self._prompt = prompt

    def submit(self) -> str:
        if self._page is None or self._prompt is None:
            raise RuntimeError("Gemini delivery was not prepared")
        self._prompt.press("Enter")
        self._page.wait_for_url(
            re.compile(
                r"https://gemini\.google\.com/"
                r"(?:app/[^/?#]+|gem/[A-Za-z0-9_-]+/[^/?#]+)"
            ),
            timeout=self.timeout_ms,
        )
        self._page.wait_for_timeout(1500)
        stop_response = self._page.get_by_role("button", name="Stop response")
        if stop_response.count() and stop_response.first.is_visible():
            stop_response.first.wait_for(state="hidden", timeout=self.timeout_ms)
        self._prompt = None
        return self._page.url

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


def process_next_gemini_delivery(
    *,
    store: GeminiDeliveryStore,
    client,
    max_retries: int,
    retry_base_seconds: int,
    notifier=None,
) -> str | None:
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
        should_notify = store.mark_authentication_required(delivery.job_id, str(exc))
        gemini_logger.error(
            "Gemini authentication required job_id=%s",
            delivery.job_id,
        )
        if should_notify and notifier is not None:
            try:
                notifier.notify_okta_reverification_required()
            except Exception:
                gemini_logger.exception("Okta re-verification notification failed")
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
    try:
        conversation_url = client.submit()
        store.mark_delivered(delivery.job_id, conversation_url)
        gemini_logger.info(
            "Gemini transcript delivered job_id=%s conversation_url=%s",
            delivery.job_id,
            conversation_url,
        )
        return delivery.job_id
    except Exception as exc:
        client.close()
        store.mark_confirmation_needed(delivery.job_id, str(exc))
        gemini_logger.exception(
            "Gemini submission requires confirmation job_id=%s",
            delivery.job_id,
        )
        return None
