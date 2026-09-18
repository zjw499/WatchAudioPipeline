"""Native Notion transcription. The PC handles media transport, never speech AI."""

from datetime import UTC, datetime, timedelta
from contextlib import contextmanager
import hashlib
import json
import logging
import math
from pathlib import Path
import uuid

import httpx

from watch_audio_pipeline.audio_batching import InvalidAudioChunks
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.db import connect as open_database
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.notion_delivery import NotionPublisher
from watch_audio_pipeline.notion_report import NotionReport
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.summarization import fallback_title


logger = logging.getLogger("notion")
PART_BYTES = 10 * 1024 * 1024
SINGLE_PART_LIMIT = 20 * 1024 * 1024


@contextmanager
def connect(path):
    connection = open_database(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def now():
    return datetime.now(UTC).isoformat()


def rich_text(parts):
    return "".join(p.get("plain_text", p.get("text", {}).get("content", "")) for p in parts).strip()


class NotionAudioError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Notion API returned HTTP {status}; audio remains saved")


class NativeNotionAPI(NotionPublisher):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.http = httpx.Client(timeout=max(120, self.timeout_seconds), headers={
            "Authorization": f"Bearer {self.token}", "Notion-Version": self.api_version,
            "User-Agent": "Scribe-Pilot/1.0",
        })

    def _request(self, method, path, payload=None, **kwargs):
        try:
            response = self.http.request(method, self.api_base + path, json=payload, **kwargs)
        except httpx.HTTPError:
            raise RuntimeError("Notion network request failed; audio remains saved") from None
        if not response.is_success:
            # Notion validation responses can echo private content. Never log them.
            raise NotionAudioError(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("Notion returned an unreadable response") from None

    def upload_step(self, path, state, checkpoint):
        """Send at most one bounded part per worker cycle; resume after restart."""
        size = path.stat().st_size
        upload_id = state.get("upload_id")
        if upload_id:
            upload = self._request("GET", f"/file_uploads/{upload_id}")
            if upload["status"] == "uploaded":
                return True
            if upload["status"] in {"expired", "failed"}:
                checkpoint(upload_id=None, sent_parts=0)
                upload_id = None
        multipart = size > SINGLE_PART_LIMIT
        parts = math.ceil(size / PART_BYTES) if multipart else 1
        if not upload_id:
            payload = {"filename": "meeting.m4a", "content_type": "audio/mp4"}
            if multipart:
                payload.update(mode="multi_part", number_of_parts=parts)
            upload = self._request("POST", "/file_uploads", payload)
            upload_id = upload["id"]
            checkpoint(upload_id=upload_id, sent_parts=0)
        sent = state.get("sent_parts", 0)
        if sent < parts:
            with path.open("rb") as audio:
                if multipart:
                    audio.seek(sent * PART_BYTES)
                    content = audio.read(PART_BYTES)
                else:
                    content = audio.read(SINGLE_PART_LIMIT + 1)
            result = self._request(
                "POST", f"/file_uploads/{upload_id}/send",
                files={"file": ("meeting.m4a", content, "audio/mp4")},
                data={"part_number": str(sent + 1)} if multipart else {},
            )
            checkpoint(sent_parts=sent + 1)
            if not multipart:
                return result["status"] == "uploaded"
            return False
        self._request("POST", f"/file_uploads/{upload_id}/complete", {})
        return self._request("GET", f"/file_uploads/{upload_id}")["status"] == "uploaded"

    def read_tree(self, block_id):
        blocks = []
        for block in self._list_children(block_id):
            blocks.append(block)
            if block.get("has_children"):
                blocks.extend(self.read_tree(block["id"]))
        return blocks


def session_snapshot(connection, session_id):
    session = connection.execute("SELECT * FROM recording_sessions WHERE id = ?", (session_id,)).fetchone()
    if not session or session["final_chunk_index"] is None:
        return None
    chunks = connection.execute(
        "SELECT * FROM recording_chunks WHERE session_id = ? ORDER BY chunk_index", (session_id,),
    ).fetchall()
    if [c["chunk_index"] for c in chunks] != list(range(session["final_chunk_index"] + 1)):
        return None
    if any(c["status"] in {"failed", "transcribing"} for c in chunks):
        return None
    digest = hashlib.sha256(json.dumps([(c["chunk_index"], c["content_hash"]) for c in chunks]).encode()).hexdigest()
    return digest, session, chunks


class NativeNotionWorker:
    """Run only under the existing exclusive notion-worker lock."""

    def __init__(self, settings, paths, api, audio_batcher):
        self.settings, self.paths, self.api, self.audio_batcher = settings, paths, api, audio_batcher
        self.store = JobStore(paths.database)
        self.memos = MemoStore(paths.database)
        self.chunks = ChunkStore(paths.database)

    def enqueue_ready(self):
        with connect(self.paths.database) as db:
            sessions = db.execute("SELECT id, client_id FROM recording_sessions WHERE status != 'done'").fetchall()
        for item in sessions:
            if not self.settings.uses_native_notion(item["client_id"]):
                continue
            with connect(self.paths.database) as db:
                snapshot = session_snapshot(db, item["id"])
            if not snapshot:
                continue
            revision, session, chunks = snapshot
            job = self.store.get_by_hash(f"recording-session:{session['id']}")
            if not job:
                job = self.store.create_job(
                    source=session["source"], original_filename=session["original_filename"],
                    stored_filename=f"recording-session-{session['id']}.chunks", mime_type="audio/x-codexwatch-chunks",
                    file_size=sum(c["file_size"] for c in chunks), content_hash=f"recording-session:{session['id']}",
                    client_id=session["client_id"], recipient=session["recipient"],
                )
            if job.client_id != session["client_id"]:
                raise RuntimeError("Native Notion recording ownership mismatch")
            self._enqueue(job, revision, session["id"], session["created_at"])
        for status in ("queued", "native_notion", "transcribed", "notion_failed"):
            for job in self.store.list_jobs_by_status(status):
                if self.settings.uses_native_notion(job.client_id) and job.mime_type != "audio/x-codexwatch-chunks":
                    if (self.paths.incoming / job.stored_filename).is_file():
                        self._enqueue(job, job.content_hash, None, job.created_at)

    def _enqueue(self, job, revision, session_id, recorded_at):
        with connect(self.paths.database) as db:
            db.execute("BEGIN IMMEDIATE")
            if session_id:
                current = session_snapshot(db, session_id)
                if not current or current[0] != revision:
                    return
            task_id = uuid.uuid4().hex
            created = db.execute(
                "INSERT OR IGNORE INTO notion_audio_jobs (id, job_id, session_id, revision, state_json, next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, job.id, session_id, revision, json.dumps({"recorded_at": recorded_at}), now(), now(), now()),
            ).rowcount
            if not created:
                return
            db.execute("UPDATE notion_audio_jobs SET status = 'superseded' WHERE job_id = ? AND id != ? AND status = 'active'", (job.id, task_id))
            db.execute("UPDATE jobs SET status = 'native_notion', error_message = NULL, updated_at = ? WHERE id = ?", (now(), job.id))
            # Old transcript-only deliveries must not race the native audio worker.
            db.execute("UPDATE notion_deliveries SET status = 'superseded' WHERE job_id = ?", (job.id,))
            if session_id:
                db.execute("UPDATE recording_sessions SET job_id = ?, status = 'email_queued' WHERE id = ?", (job.id, session_id))
        if self.memos.get(job.id) is None:
            self.memos.upsert_from_job(job, self.paths.transcripts / f"{job.id}.txt", title=fallback_title(job.original_filename), recorded_at=recorded_at)
        self.memos.update_status(job.id, "publishing")

    def step(self):
        self.enqueue_ready()
        with connect(self.paths.database) as db:
            rows = db.execute(
                "SELECT n.*, j.client_id FROM notion_audio_jobs n JOIN jobs j ON j.id = n.job_id WHERE n.status = 'active' AND n.next_attempt_at <= ? ORDER BY n.next_attempt_at, n.created_at", (now(),),
            ).fetchall()
            row = next((r for r in rows if self.settings.uses_native_notion(r["client_id"])), None)
        if not row:
            return None
        task = dict(row)
        job = self.store.get_job(task["job_id"])
        if not self.settings.uses_native_notion(job.client_id):
            return None
        state = json.loads(task["state_json"])
        self._save(task, state, delay=10)
        try:
            self._advance(task, job, state)
        except InvalidAudioChunks as exc:
            invalid = {p.resolve() for p in exc.paths}
            if task["session_id"]:
                for chunk in self.chunks.list_chunks(task["session_id"]):
                    if (self.paths.chunks / chunk.session_id / chunk.stored_filename).resolve() in invalid:
                        self.chunks.mark_chunk_failed(chunk, "Invalid audio; resend this saved chunk")
            self._failed(task, state, "Invalid audio; recording remains saved for retry")
        except Exception as exc:
            # Only our sanitized API errors are safe to include in logs/UI.
            message = str(exc) if isinstance(exc, NotionAudioError) else f"Notion processing waiting for retry ({type(exc).__name__}); audio remains saved"
            self._failed(task, state, message)
        return job.id

    def _save(self, task, state, delay=0, **updates):
        state.update(updates)
        with connect(self.paths.database) as db:
            db.execute(
                "UPDATE notion_audio_jobs SET state_json = ?, next_attempt_at = ?, updated_at = ? WHERE id = ?",
                (json.dumps(state), (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(), now(), task["id"]),
            )

    def _failed(self, task, state, message):
        attempts = task["attempts"] + 1
        delay = min(self.settings.notion_retry_max_seconds, self.settings.notion_retry_base_seconds * 2 ** min(attempts - 1, 16))
        self._save(task, state, delay=delay)
        with connect(self.paths.database) as db:
            db.execute("UPDATE notion_audio_jobs SET attempts = ?, error_message = ? WHERE id = ?", (attempts, message, task["id"]))
        self.memos.update_status(task["job_id"], "notion_failed", message)
        logger.warning("native Notion retry job_id=%s reason=%s", task["job_id"], message)

    def _current(self, task, connection):
        if not task["session_id"]:
            return True
        snapshot = session_snapshot(connection, task["session_id"])
        return snapshot is not None and snapshot[0] == task["revision"]

    def _advance(self, task, job, state):
        with connect(self.paths.database) as db:
            if not self._current(task, db):
                # A new final marker/chunk must be assembled before publishing.
                return
        checkpoint = lambda **kw: self._save(task, state, **kw)
        output = self.paths.state / "notion-audio" / f"{task['id']}.m4a"
        if not state.get("prepared") or not output.exists():
            self.memos.update_status(job.id, "publishing")
            if task["session_id"]:
                sources = [self.paths.chunks / c.session_id / c.stored_filename for c in self.chunks.list_chunks(task["session_id"])]
            else:
                sources = [self.paths.incoming / job.stored_filename]
            prepared = self.audio_batcher.prepare(sources, output)
            checkpoint(prepared=True, duration_seconds=prepared.duration_seconds, phase="uploading")
            return
        if not state.get("page_id"):
            # Adopt the page already visible during recording. An uncertain live
            # creation must reconcile before either worker can create another page.
            with connect(self.paths.database) as db:
                preview = db.execute("SELECT state_json FROM notion_live_sessions WHERE session_id = ? AND client_id = ?",
                                     (task["session_id"], job.client_id)).fetchone()
            live_state = json.loads(preview["state_json"]) if preview else {}
            if live_state.get("page_id"):
                checkpoint(page_id=live_state["page_id"], page_url=live_state["page_url"])
                self.memos.record_notion_receipt(job.id, state["page_id"], state["page_url"])
                return
            page = self.api.find_by_recording_id(task["session_id"] or job.id, job.client_id)
            if page:
                checkpoint(page_id=page.id, page_url=page.url)
            else:
                if state.get("page_attempted") or live_state.get("page_attempted"):
                    raise RuntimeError("Waiting to reconcile an ambiguous Notion page creation")
                checkpoint(page_attempted=True)
                try:
                    page = self.api._request("POST", "/pages", {
                        "parent": {"type": "data_source_id", "data_source_id": self.api.data_source_id},
                        "properties": {
                            "Name": {"title": self.api._rich_text(fallback_title(job.original_filename))},
                            "Recording ID": {"rich_text": self.api._rich_text(task["session_id"] or job.id)},
                            "Client ID": {"rich_text": self.api._rich_text(job.client_id)},
                            "Meeting Date": {"date": {"start": state["recorded_at"]}},
                            "Duration (min)": {"number": round(state["duration_seconds"] / 60, 1)},
                            "Source": {"select": {"name": self.api._source_name(job.source)}},
                            "Delivery": {"select": {"name": "Needs review"}},
                        },
                    })
                except NotionAudioError as exc:
                    if exc.status in {400, 401, 403, 404, 429}:
                        checkpoint(page_attempted=False)
                    raise
                checkpoint(page_id=page["id"], page_url=page["url"])
            self.memos.record_notion_receipt(job.id, state["page_id"], state["page_url"])
            return
        report = (NotionReport(self.api, self.settings.notion_report_instructions_url, task["revision"])
                  if self.settings.uses_notion_report(job.client_id) else None)
        if not state.get("block_id"):
            if state.get("meeting_attempted"):
                blocks = [b for b in self.api._list_children(state["page_id"]) if b["type"] == "meeting_notes" and b["id"] not in state.get("prior_blocks", [])]
                if len(blocks) == 1:
                    checkpoint(block_id=blocks[0]["id"], phase="transcribing")
                    return
                raise RuntimeError("Waiting to reconcile an ambiguous Notion meeting creation")
            if not self.api.upload_step(output, state, checkpoint):
                return
            prior = [b["id"] for b in self.api._list_children(state["page_id"]) if b["type"] == "meeting_notes"]
            # Persist BEFORE this non-idempotent POST. Never blindly retry it.
            checkpoint(meeting_attempted=True, prior_blocks=prior)
            try:
                block = self.api._request("POST", "/blocks/meeting_notes", {
                    "source": {"type": "file_upload", "file_upload_id": state["upload_id"]},
                    "parent": {"type": "page_id", "page_id": state["page_id"]},
                    "title": fallback_title(job.original_filename),
                    "language": "auto", "options": {"kickoff_summary": True},
                })
            except NotionAudioError as exc:
                if exc.status in {400, 401, 403, 404, 429}:
                    checkpoint(meeting_attempted=False)
                raise
            checkpoint(block_id=block["id"], phase="transcribing")
            self.memos.update_status(job.id, "summarizing")
            return
        block = self.api._request("GET", f"/blocks/{state['block_id']}")
        meeting = block["meeting_notes"]
        phase = meeting.get("status", "transcription_in_progress")
        checkpoint(phase=phase, delay=15)
        if phase == "transcription_failed":
            raise RuntimeError("Notion transcription failed; audio retained and existing meeting preserved")
        if phase != "notes_ready":
            self.memos.update_status(job.id, "summarizing")
            return
        transcript_id = meeting.get("children", {}).get("transcript_block_id")
        if not transcript_id:
            raise RuntimeError("Notion transcript is not available yet")
        transcript = "\n\n".join(filter(None, (self.api._block_signature(b)[1] for b in self.api.read_tree(transcript_id))))
        if not transcript.strip():
            raise RuntimeError("Notion returned an empty transcript; audio retained for review")
        summary_id = meeting.get("children", {}).get("summary_block_id")
        summary_blocks = self.api.read_tree(summary_id) if summary_id else []
        summary = "\n".join(filter(None, (self.api._block_signature(b)[1] for b in summary_blocks)))
        actions = tuple(self.api._block_signature(b)[1] for b in summary_blocks if b["type"] == "to_do")
        if report:
            # The native meeting endpoint inserts its block at the page start.
            # Add the report only afterwards so it stays above all transcripts.
            if not state.get("report_slots"):
                if not report.ensure_layout(state["page_id"], state, checkpoint):
                    return
            if not report.attach_audio(state["page_id"], output, task["revision"], state, checkpoint):
                return
            report.publish_draft(summary_blocks, state, checkpoint)
            checkpoint(delay=60)
        title = rich_text(meeting.get("title", [])) or fallback_title(job.original_filename)
        self.api._request("PATCH", f"/pages/{state['page_id']}", {"properties": {
            "Name": {"title": self.api._rich_text(title)}, "Summary": {"rich_text": self.api._rich_text(summary)},
            "Duration (min)": {"number": round(state["duration_seconds"] / 60, 1)},
            "Delivery": {"select": {"name": "Needs review" if report else "Ready"}}, "Has action items": {"checkbox": bool(actions)},
        }})
        # A dedicated transcript per revision avoids changing a memo before the
        # atomic final-source check, including late chunks racing with readback.
        transcript_path = self.paths.transcripts / f"{job.id}.{task['id']}.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        self.memos.upsert_from_job(
            job, transcript_path, title=title, summary=summary, duration_seconds=state["duration_seconds"],
            action_items=actions, recorded_at=state["recorded_at"],
        )
        with connect(self.paths.database) as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._current(task, db):
                return
            timestamp = now()
            db.execute("UPDATE jobs SET status = 'done', transcript_path = ?, error_message = NULL, updated_at = ? WHERE id = ?", (str(transcript_path), timestamp, job.id))
            db.execute("UPDATE memos SET status = 'done', notion_page_id = ?, notion_url = ?, notion_published_at = ?, error_message = NULL, updated_at = ? WHERE job_id = ?", (state["page_id"], state["page_url"], timestamp, timestamp, job.id))
            if task["session_id"]:
                db.execute("UPDATE recording_sessions SET status = 'done', error_message = NULL, updated_at = ? WHERE id = ?", (timestamp, task["session_id"]))
            db.execute("UPDATE notion_audio_jobs SET status = 'done', error_message = NULL, updated_at = ? WHERE id = ?", (timestamp, task["id"]))
        # Keep source audio for recovery; never delete it while late chunks may arrive.
        logger.info("native Notion transcription verified job_id=%s block_id=%s", job.id, state["block_id"])

    def refresh_completed_report(self):
        """Read a subsequently regenerated native summary; never invoke another AI."""
        with connect(self.paths.database) as db:
            rows = db.execute(
                "SELECT n.*, j.client_id FROM notion_audio_jobs n JOIN jobs j ON j.id = n.job_id "
                "WHERE n.status = 'done' AND j.status = 'done' AND n.next_attempt_at <= ? "
                "AND json_extract(n.state_json, '$.report_status') = 'waiting_for_narrative' "
                "AND NOT EXISTS (SELECT 1 FROM notion_audio_jobs newer WHERE newer.job_id = n.job_id AND newer.created_at > n.created_at) "
                "ORDER BY n.next_attempt_at", (now(),),
            ).fetchall()
        task = next((dict(row) for row in rows if self.settings.uses_notion_report(row["client_id"])), None)
        if not task:
            return None
        state = json.loads(task["state_json"])
        self._save(task, state, delay=300)
        checkpoint = lambda **kw: self._save(task, state, delay=300, **kw)
        try:
            with connect(self.paths.database) as db:
                if not self._current(task, db):
                    return None
            meeting = self.api._request("GET", f"/blocks/{state['block_id']}")["meeting_notes"]
            summary_id = meeting.get("children", {}).get("summary_block_id")
            if meeting.get("status") == "notes_ready" and summary_id:
                report = NotionReport(self.api, self.settings.notion_report_instructions_url, task["revision"])
                report.publish_draft(self.api.read_tree(summary_id), state, checkpoint)
        except Exception as exc:
            # This optional presentation step must not turn a saved recording into a failed upload.
            logger.warning("Notion draft refresh pending job_id=%s error_type=%s", task["job_id"], type(exc).__name__)
        return task["job_id"]
