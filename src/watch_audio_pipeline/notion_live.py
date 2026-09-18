"""Ordered, resumable native Notion previews. Final whole-audio notes stay authoritative."""

from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging

from watch_audio_pipeline.notion_audio import NotionAudioError, connect, now


logger = logging.getLogger("notion")
REJECTED = {400, 401, 403, 404, 429}
LIVE_LABEL = "Live transcript: waiting for the first audio segment."
ARCHIVE_TITLE = "Live audio parts (Notion transcription sources)"


class LiveNotionWorker:
    """Shares the exclusive notion-worker lock with NativeNotionWorker."""

    def __init__(self, settings, paths, api, audio_batcher):
        self.settings, self.paths, self.api, self.audio_batcher = settings, paths, api, audio_batcher

    def enqueue(self):
        with connect(self.paths.database) as db:
            sessions = db.execute("SELECT * FROM recording_sessions WHERE status != 'done'").fetchall()
            for session in sessions:
                if (not self.settings.uses_live_notion(session["client_id"])
                        or datetime.fromisoformat(session["created_at"]) < self.settings.notion_live_start_after):
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO notion_live_sessions "
                    "(session_id, client_id, next_attempt_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (session["id"], session["client_id"], now(), now(), now()),
                )

    def step(self):
        self.enqueue()
        with connect(self.paths.database) as db:
            rows = db.execute(
                "SELECT * FROM notion_live_sessions WHERE status = 'active' AND next_attempt_at <= ? "
                "ORDER BY next_attempt_at, created_at", (now(),),
            ).fetchall()
            task = next((dict(r) for r in rows if self.settings.uses_live_notion(r["client_id"])), None)
            if not task:
                return None
            session = db.execute("SELECT * FROM recording_sessions WHERE id = ?", (task["session_id"],)).fetchone()
            chunks = db.execute("SELECT * FROM recording_chunks WHERE session_id = ? ORDER BY chunk_index", (task["session_id"],)).fetchall()
        if not session or session["client_id"] != task["client_id"]:
            return None
        state = json.loads(task["state_json"])
        self._save(task, state, delay=3)
        try:
            self._advance(task, state, session, chunks)
            with connect(self.paths.database) as db:
                db.execute("UPDATE notion_live_sessions SET attempts = 0, error_message = NULL WHERE session_id = ?", (task["session_id"],))
        except Exception as exc:
            message = (str(exc) if isinstance(exc, NotionAudioError) else
                       f"Live Notion preview waiting for retry ({type(exc).__name__}); final audio remains saved")
            attempts = task["attempts"] + 1
            self._save(task, state, delay=min(300, 10 * 2 ** min(attempts - 1, 5)))
            with connect(self.paths.database) as db:
                db.execute("UPDATE notion_live_sessions SET attempts = ?, error_message = ? WHERE session_id = ?",
                           (attempts, message, task["session_id"]))
            logger.warning("live Notion retry recording_id=%s reason=%s", task["session_id"], message)
        return task["session_id"]

    def _save(self, task, state, delay=3, **updates):
        state.update(updates)
        with connect(self.paths.database) as db:
            db.execute("UPDATE notion_live_sessions SET state_json = ?, next_attempt_at = ?, updated_at = ? WHERE session_id = ?",
                       (json.dumps(state), (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(), now(), task["session_id"]))

    def _post_once(self, path, payload, state, flag, checkpoint):
        # A lost response is ambiguous: reconcile the remote object, never blindly POST again.
        if state.get(flag):
            raise RuntimeError("Waiting for Notion creation reconciliation")
        checkpoint(**{flag: True})
        try:
            return self.api._request("POST", path, payload)
        except NotionAudioError as exc:
            if exc.status in REJECTED:
                checkpoint(**{flag: False})
            raise

    def _page(self, task, state, session, checkpoint):
        if state.get("page_id"):
            return True
        with connect(self.paths.database) as db:
            native = db.execute(
                "SELECT n.state_json FROM notion_audio_jobs n JOIN jobs j ON j.id = n.job_id "
                "WHERE n.session_id = ? AND j.client_id = ? ORDER BY n.created_at DESC LIMIT 1",
                (session["id"], session["client_id"]),
            ).fetchone()
        native_state = json.loads(native["state_json"]) if native else {}
        if native_state.get("page_id"):
            checkpoint(page_id=native_state["page_id"], page_url=native_state["page_url"])
            return True
        found = self.api.find_by_recording_id(session["id"], session["client_id"])
        if found:
            checkpoint(page_id=found.id, page_url=found.url)
            return True
        if native_state.get("page_attempted"):
            raise RuntimeError("Waiting for complete meeting page reconciliation")
        date = datetime.fromisoformat(session["created_at"]).astimezone().strftime("%b %d, %Y %I:%M %p")
        page = self._post_once("/pages", {
            "parent": {"type": "data_source_id", "data_source_id": self.api.data_source_id},
            "properties": {
                "Name": {"title": self.api._rich_text(f"Watch meeting - {date}")},
                "Recording ID": {"rich_text": self.api._rich_text(session["id"])},
                "Client ID": {"rich_text": self.api._rich_text(session["client_id"])},
                "Meeting Date": {"date": {"start": session["created_at"]}},
                "Source": {"select": {"name": self.api._source_name(session["source"])}},
                "Delivery": {"select": {"name": "Needs review"}},
            },
        }, state, "page_attempted", checkpoint)
        checkpoint(page_id=page["id"], page_url=page["url"])
        return False

    def _append_once(self, parent, blocks, receipt, state, checkpoint):
        children = self.api._list_children(parent)
        expected = [self.api._block_signature(b) for b in blocks]
        signatures = [self.api._block_signature(b) for b in children]
        # Each batch starts with a unique heading. Resolve uncertain writes by content.
        positions = [i for i, value in enumerate(signatures) if value == expected[0]]
        if positions:
            if len(positions) == 1 and signatures[positions[0]:positions[0] + len(expected)] == expected:
                return children[positions[0]:positions[0] + len(expected)]
            raise RuntimeError("Notion live content changed; preserve it for review")
        flag = receipt + "_attempted"
        if state.get(flag):
            raise RuntimeError("Waiting for Notion append reconciliation")
        checkpoint(**{flag: True})
        try:
            result = self.api._request("PATCH", f"/blocks/{parent}/children", {"children": blocks})
        except NotionAudioError as exc:
            if exc.status in REJECTED:
                checkpoint(**{flag: False})
            raise
        return result["results"]

    def _status(self, state, text, checkpoint):
        if state.get("status_text") != text and state.get("status_block_id"):
            self.api._request("PATCH", f"/blocks/{state['status_block_id']}",
                              {"paragraph": {"rich_text": self.api._rich_text(text)}})
            checkpoint(status_text=text)

    def _advance(self, task, state, session, chunks):
        checkpoint = lambda **kw: self._save(task, state, **kw)
        if session["status"] == "done":
            text = "Complete. Notion's full transcript and summary are available in this page's meeting block. Live parts are a preview; review the complete meeting for context."
            if self.settings.uses_notion_transcript_only(session["client_id"]):
                text = "Complete. Notion's full transcript is saved on this page."
                if self.settings.uses_notion_audio_attachment(session["client_id"]):
                    text += " The complete playable recording is attached below."
                text += " Live parts are a preview; review the complete transcript for context."
            elif self.settings.uses_notion_report(session["client_id"]):
                text = "Complete. The full transcript and playable recording are saved on this page. Narrative drafting has a separate status above; clinician review is required."
            self._status(state, text, checkpoint)
            with connect(self.paths.database) as db:
                db.execute("UPDATE notion_live_sessions SET status = 'done' WHERE session_id = ?", (session["id"],))
            return
        if not chunks:
            return
        if not self._page(task, state, session, checkpoint):
            return
        if not state.get("status_block_id"):
            blocks = self._append_once(state["page_id"], [self.api._heading("Live transcript"), self.api._paragraph(LIVE_LABEL)], "header", state, checkpoint)
            checkpoint(status_block_id=blocks[1]["id"], status_text=LIVE_LABEL)
            return
        if not state.get("archive_id"):
            matches = [b for b in self.api._list_children(state["page_id"])
                       if b["type"] == "child_page" and b["child_page"].get("title") == ARCHIVE_TITLE]
            if len(matches) == 1:
                checkpoint(archive_id=matches[0]["id"])
            elif matches:
                raise RuntimeError("Ambiguous Notion live archive")
            else:
                page = self._post_once("/pages", {
                    "parent": {"type": "page_id", "page_id": state["page_id"]},
                    "properties": {"title": {"title": self.api._rich_text(ARCHIVE_TITLE)}},
                }, state, "archive_attempted", checkpoint)
                checkpoint(archive_id=page["id"])
            return
        parts = state.setdefault("parts", {})
        for chunk in chunks:
            previous = parts.get(str(chunk["chunk_index"]), {})
            if previous.get("hash") and previous["hash"] != chunk["content_hash"]:
                self._status(state, "An audio segment was replaced. Live preview paused; the complete meeting will use the corrected audio after you stop.", checkpoint)
                return
        index = state.get("next_index", 0)
        chunk = next((c for c in chunks if c["chunk_index"] == index), None)
        if chunk is None:
            final_output = ("transcript" if self.settings.uses_notion_transcript_only(session["client_id"])
                            else "transcript and summary")
            text = (f"Live transcript through part {index}. Waiting for the next audio segment."
                    if session["final_chunk_index"] is None else
                    f"Recording stopped. Waiting for any missing audio and Notion's complete meeting {final_output}. Live parts ready: {index}.")
            self._status(state, text, checkpoint)
            return
        part = parts.setdefault(str(index), {"hash": chunk["content_hash"]})
        def part_save(**updates):
            part.update(updates)
            checkpoint()
        # The local output name is not derived from an untrusted recording filename.
        key = hashlib.sha256(session["id"].encode()).hexdigest()
        output = self.paths.state / "notion-live" / f"{key}-{index}-{chunk['content_hash'][:16]}.m4a"
        if not part.get("prepared") or not output.exists():
            prepared = self.audio_batcher.prepare([self.paths.chunks / session["id"] / chunk["stored_filename"]], output)
            part_save(prepared=True, duration_seconds=prepared.duration_seconds, silent=prepared.is_silent)
            return
        if part.get("silent"):
            part_save(transcript="[Silent audio segment.]")
        if not part.get("transcript"):
            if not part.get("block_id"):
                if part.get("meeting_attempted"):
                    matches = [b for b in self.api._list_children(state["archive_id"])
                               if b["type"] == "meeting_notes" and b["id"] not in part.get("prior_blocks", [])]
                    if len(matches) != 1:
                        raise RuntimeError("Waiting for Notion live meeting reconciliation")
                    part_save(block_id=matches[0]["id"])
                    return
                if not self.api.upload_step(output, part, part_save):
                    return
                part_save(prior_blocks=[b["id"] for b in self.api._list_children(state["archive_id"]) if b["type"] == "meeting_notes"])
                block = self._post_once("/blocks/meeting_notes", {
                    "source": {"type": "file_upload", "file_upload_id": part["upload_id"]},
                    "parent": {"type": "page_id", "page_id": state["archive_id"]},
                    "title": f"Live part {index + 1:03d}", "language": "auto",
                    "options": {"kickoff_summary": False},
                }, part, "meeting_attempted", part_save)
                part_save(block_id=block["id"])
                return
            meeting = self.api._request("GET", f"/blocks/{part['block_id']}")["meeting_notes"]
            phase = meeting.get("status")
            if phase == "transcription_failed":
                raise RuntimeError("Live segment transcription failed; full audio preserved")
            if phase != "notes_ready":
                return
            transcript_id = meeting.get("children", {}).get("transcript_block_id")
            if not transcript_id:
                return
            text = "\n\n".join(filter(None, (self.api._block_signature(b)[1] for b in self.api.read_tree(transcript_id))))
            if not text.strip():
                raise RuntimeError("Empty Notion live transcript; source preserved")
            part_save(transcript=text)
        paragraphs = self.api._text_paragraphs(part["transcript"])
        if len(paragraphs) > 90:
            raise RuntimeError("Live segment too large; full meeting will handle it")
        self._append_once(state["page_id"], [self.api._heading(f"Live part {index + 1:03d}")] + paragraphs,
                          f"part_{index}", state, checkpoint)
        part_save(published=True)
        checkpoint(next_index=index + 1)
        self._status(state, f"Live transcript through part {index + 1}. Updates arrive in audio batches, not word by word. Final notes appear after you stop.", checkpoint)
        logger.info("live Notion part verified recording_id=%s chunk_index=%s", session["id"], index)
