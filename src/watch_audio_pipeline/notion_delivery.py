from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from watch_audio_pipeline.db import connect, init_db


notion_logger = logging.getLogger("notion")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class NotionDelivery:
    job_id: str
    status: str
    attempts: int
    next_attempt_at: str | None
    page_id: str | None
    page_url: str | None
    error_message: str | None
    created_at: str
    updated_at: str
    delivered_at: str | None
    transcript_hash: str | None


@dataclass(frozen=True)
class NotionPage:
    id: str
    url: str


class NotionDeliveryStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        init_db(database_path)

    @staticmethod
    def _from_row(row) -> NotionDelivery:
        return NotionDelivery(**dict(row))

    def get(self, job_id: str) -> NotionDelivery | None:
        connection = connect(self.database_path)
        row = connection.execute(
            "SELECT * FROM notion_deliveries WHERE job_id = ?", (job_id,)
        ).fetchone()
        connection.close()
        return self._from_row(row) if row else None

    def enqueue(self, job_id: str, transcript_hash: str | None = None) -> None:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                INSERT INTO notion_deliveries (
                    job_id, status, attempts, next_attempt_at, page_id, page_url,
                    error_message, created_at, updated_at, delivered_at, transcript_hash
                ) VALUES (?, 'queued', 0, NULL, NULL, NULL, NULL, ?, ?, NULL, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    status = 'queued', attempts = 0, next_attempt_at = NULL,
                    error_message = NULL, updated_at = excluded.updated_at,
                    transcript_hash = excluded.transcript_hash
                WHERE notion_deliveries.status = 'delivered'
                  AND excluded.transcript_hash IS NOT NULL
                  AND COALESCE(notion_deliveries.transcript_hash, '') != excluded.transcript_hash
                """,
                (job_id, now, now, transcript_hash),
            )
        connection.close()

    def recover_stale(self, stale_seconds: int = 15 * 60) -> int:
        threshold = (datetime.now(UTC) - timedelta(seconds=stale_seconds)).isoformat()
        connection = connect(self.database_path)
        with connection:
            cursor = connection.execute(
                """
                UPDATE notion_deliveries
                SET status = 'retry', next_attempt_at = ?, updated_at = ?
                WHERE status = 'publishing' AND updated_at < ?
                """,
                (_utc_now(), _utc_now(), threshold),
            )
        connection.close()
        return cursor.rowcount

    def claim_next(self) -> NotionDelivery | None:
        now = _utc_now()
        connection = connect(self.database_path)
        row = connection.execute(
            """
            SELECT * FROM notion_deliveries
            WHERE status IN ('queued', 'retry')
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY created_at ASC LIMIT 1
            """,
            (now,),
        ).fetchone()
        if row is None:
            connection.close()
            return None
        claimed = None
        with connection:
            cursor = connection.execute(
                """
                UPDATE notion_deliveries
                SET status = 'publishing', attempts = attempts + 1, updated_at = ?
                WHERE job_id = ? AND status IN ('queued', 'retry')
                """,
                (now, row["job_id"]),
            )
            if cursor.rowcount:
                claimed_row = connection.execute(
                    "SELECT * FROM notion_deliveries WHERE job_id = ?", (row["job_id"],)
                ).fetchone()
                claimed = self._from_row(claimed_row)
        connection.close()
        return claimed

    def mark_delivered(self, job_id: str, page: NotionPage) -> None:
        now = _utc_now()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE notion_deliveries
                SET status = 'delivered', page_id = ?, page_url = ?, error_message = NULL,
                    next_attempt_at = NULL, updated_at = ?, delivered_at = ?
                WHERE job_id = ?
                """,
                (page.id, page.url, now, now, job_id),
            )
        connection.close()

    def mark_retry(
        self,
        job_id: str,
        error_message: str,
        *,
        base_seconds: int,
        max_seconds: int,
    ) -> None:
        delivery = self.get(job_id)
        attempts = delivery.attempts if delivery else 1
        delay = min(max_seconds, base_seconds * (2 ** min(16, max(0, attempts - 1))))
        next_attempt = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat()
        connection = connect(self.database_path)
        with connection:
            connection.execute(
                """
                UPDATE notion_deliveries
                SET status = 'retry', next_attempt_at = ?, error_message = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (next_attempt, error_message[:2000], _utc_now(), job_id),
            )
        connection.close()


class NotionPublisher:
    def __init__(
        self,
        *,
        token: str,
        data_source_id: str,
        api_base: str = "https://api.notion.com/v1",
        api_version: str = "2026-03-11",
        timeout_seconds: int = 45,
    ) -> None:
        if not token.strip():
            raise ValueError("Notion token is required")
        if not data_source_id.strip():
            raise ValueError("Notion data source ID is required")
        self.token = token.strip()
        self.data_source_id = data_source_id.strip()
        self.api_base = api_base.rstrip("/")
        self.api_version = api_version
        self.timeout_seconds = timeout_seconds

    def ensure_meeting(
        self,
        *,
        recording_id: str,
        client_id: str,
        title: str,
        summary: str | None,
        transcript: str,
        created_at: str,
        duration_seconds: float | None,
        source: str,
        speaker_count: int | None,
        action_items: tuple[str, ...] = (),
        decisions: tuple[str, ...] = (),
        topics: tuple[str, ...] = (),
        previously_published: bool = False,
    ) -> NotionPage:
        existing = self.find_by_recording_id(recording_id, client_id)

        properties = {
            "Name": {"type": "title", "title": self._rich_text(title[:120])},
            "Meeting Date": {"type": "date", "date": {"start": created_at}},
            "Source": {"type": "select", "select": {"name": self._source_name(source)}},
            "Delivery": {"type": "select", "select": {"name": "Needs review"}},
            "Recording ID": {"type": "rich_text", "rich_text": self._rich_text(recording_id)},
            "Client ID": {"type": "rich_text", "rich_text": self._rich_text(client_id)},
            "Summary": {"type": "rich_text", "rich_text": self._rich_text((summary or "")[:1900])},
            "Has action items": {"type": "checkbox", "checkbox": bool(action_items)},
        }
        if duration_seconds is not None:
            properties["Duration (min)"] = {
                "type": "number",
                "number": round(duration_seconds / 60, 1),
            }
        if speaker_count is not None:
            properties["Speakers"] = {"type": "number", "number": speaker_count}

        blocks = self._meeting_blocks(
            summary=summary,
            action_items=action_items,
            decisions=decisions,
            topics=topics,
            transcript=transcript,
        )
        if existing is None:
            raw = self._request("POST", "/pages", {
                "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
                "properties": properties,
            })
            page = NotionPage(id=str(raw["id"]), url=str(raw["url"]))
        else:
            page = existing
        # Read back accepted blocks before appending, including after ambiguous timeouts.
        accepted = self._list_children(page.id)
        offset = 0
        if previously_published and any(
            self._block_signature(actual) != self._block_signature(expected)
            for actual, expected in zip(accepted, blocks)
        ):
            # Late audio creates a verified revision without overwriting human edits.
            digest = hashlib.sha256(json.dumps(blocks, sort_keys=True).encode()).hexdigest()[:16]
            marker = self._heading(f"Updated meeting notes ({digest})")
            signature = self._block_signature(marker)
            offset = next(
                (index for index, block in enumerate(accepted) if self._block_signature(block) == signature),
                len(accepted),
            )
            blocks = [marker, *blocks]
        accepted = accepted[offset:]
        matching = 0
        for actual, expected in zip(accepted, blocks):
            if self._block_signature(actual) != self._block_signature(expected):
                raise RuntimeError("The Notion meeting has changed. Delivery paused to preserve its content.")
            matching += 1
        for index in range(matching, len(blocks), 60):
            self._request(
                "PATCH",
                f"/blocks/{page.id}/children",
                {"children": blocks[index:index + 60]},
            )
        verified = self._list_children(page.id)[offset:]
        if len(verified) < len(blocks) or any(
            self._block_signature(actual) != self._block_signature(expected)
            for actual, expected in zip(verified, blocks)
        ):
            raise RuntimeError("Notion content verification incomplete; delivery will retry")
        properties["Delivery"]["select"]["name"] = "Ready" if summary else "Needs review"
        self._request("PATCH", f"/pages/{page.id}", {"properties": properties})
        return page

    def find_by_recording_id(self, recording_id: str, client_id: str) -> NotionPage | None:
        raw = self._request(
            "POST",
            f"/data_sources/{self.data_source_id}/query",
            {
                "filter": {"and": [
                    {"property": "Recording ID", "rich_text": {"equals": recording_id}},
                    {"property": "Client ID", "rich_text": {"equals": client_id}},
                ]},
                "page_size": 1,
            },
        )
        results = raw.get("results", [])
        if not results:
            return None
        return NotionPage(id=str(results[0]["id"]), url=str(results[0]["url"]))

    def _list_children(self, page_id: str) -> list[dict]:
        blocks = []
        cursor = None
        while True:
            query = {"page_size": 100}
            if cursor:
                query["start_cursor"] = cursor
            result = self._request("GET", f"/blocks/{page_id}/children?{urlencode(query)}")
            blocks.extend(result.get("results", []))
            cursor = result.get("next_cursor")
            if not result.get("has_more") or not cursor:
                return blocks

    @staticmethod
    def _block_signature(block: dict) -> tuple[str, str]:
        block_type = block.get("type", "")
        text = "".join(
            part.get("text", {}).get("content", part.get("plain_text", ""))
            for part in block.get(block_type, {}).get("rich_text", [])
        )
        return block_type, text

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        request = Request(
            self.api_base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Notion-Version": self.api_version,
                "Content-Type": "application/json",
                "User-Agent": "Scribe-Pilot/1.0",
            },
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            # Do not log response bodies; validation errors can echo meeting text.
            raise RuntimeError(f"Notion API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Notion API request failed: {exc}") from exc

    @staticmethod
    def _source_name(source: str) -> str:
        normalized = source.casefold()
        if "watch" in normalized:
            return "Apple Watch"
        if "iphone" in normalized:
            return "iPhone"
        return "Imported"

    @staticmethod
    def _rich_text(text: str) -> list[dict]:
        if not text:
            return []
        return [{"type": "text", "text": {"content": text[:2000]}}]

    @classmethod
    def _paragraph(cls, text: str) -> dict:
        return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": cls._rich_text(text)}}

    @classmethod
    def _heading(cls, text: str) -> dict:
        return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": cls._rich_text(text)}}

    @classmethod
    def _meeting_blocks(
        cls,
        *,
        summary: str | None,
        action_items: tuple[str, ...],
        decisions: tuple[str, ...],
        topics: tuple[str, ...],
        transcript: str,
    ) -> list[dict]:
        blocks = [
            {
                "object": "block",
                "type": "callout",
                "callout": {
                    "rich_text": cls._rich_text("Recorded with Scribe Pilot. Review generated notes against the full transcript below."),
                    "color": "blue_background",
                },
            },
            cls._heading("Summary"),
        ]
        blocks.extend(cls._text_paragraphs(summary or "No summary was generated."))
        blocks.append(cls._heading("Action items"))
        if action_items:
            blocks.extend(
                {
                    "object": "block",
                    "type": "to_do",
                    "to_do": {"rich_text": cls._rich_text(item), "checked": False},
                }
                for item in action_items
            )
        else:
            blocks.append(cls._paragraph("No explicit action items identified."))
        blocks.append(cls._heading("Decisions"))
        blocks.extend(cls._bullets(decisions, "No explicit decisions identified."))
        blocks.append(cls._heading("Topics"))
        blocks.extend(cls._bullets(topics, "No topics were generated."))
        blocks.append(cls._heading("Transcript"))
        blocks.extend(cls._text_paragraphs(transcript or "No speech was transcribed."))
        return blocks

    @classmethod
    def _bullets(cls, values: tuple[str, ...], empty_text: str) -> list[dict]:
        if not values:
            return [cls._paragraph(empty_text)]
        return [
            {
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": cls._rich_text(value)},
            }
            for value in values
        ]

    @classmethod
    def _text_paragraphs(cls, text: str) -> list[dict]:
        paragraphs: list[dict] = []
        for source_paragraph in text.splitlines() or [text]:
            source_paragraph = source_paragraph.strip()
            if not source_paragraph:
                continue
            for index in range(0, len(source_paragraph), 1900):
                paragraphs.append(cls._paragraph(source_paragraph[index:index + 1900]))
        return paragraphs or [cls._paragraph("")]
