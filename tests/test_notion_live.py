from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from watch_audio_pipeline.app import create_app
from watch_audio_pipeline.audio_batching import PreparedAudioBatch
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.notion_audio import NativeNotionWorker, NotionAudioError, connect
from watch_audio_pipeline.notion_delivery import NotionPage, NotionPublisher
from watch_audio_pipeline.notion_live import LiveNotionWorker
from watch_audio_pipeline.paths import build_paths, ensure_directories


class Batcher:
    def __init__(self):
        self.sources = []
        self.silent = False

    def prepare(self, sources, output):
        self.sources.append(list(sources))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"synthetic")
        return PreparedAudioBatch(output, 30 * len(sources), 0, self.silent)


class API(NotionPublisher):
    def __init__(self):
        super().__init__(token="synthetic", data_source_id="test")
        self.pages, self.blocks, self.children, self.requests = {}, {}, {}, []
        self.lost, self.reject, self.hidden = None, None, False
        self.meeting_phase = "notes_ready"
        self.text = "Synthetic segment transcript."
        self.counter = 0

    def _id(self):
        self.counter += 1
        return f"id-{self.counter}"

    def find_by_recording_id(self, recording_id, client_id):
        if self.hidden:
            return None
        for page in self.pages.values():
            properties = page["properties"]
            if (properties.get("Recording ID", {}).get("rich_text") == self._rich_text(recording_id)
                    and properties.get("Client ID", {}).get("rich_text") == self._rich_text(client_id)):
                return NotionPage(id=page["id"], url=page["url"])

    def _list_children(self, block_id):
        return [] if self.hidden else [deepcopy(self.blocks[i]) for i in self.children.get(block_id, [])]

    def _store(self, parent, block):
        block = {**deepcopy(block), "id": self._id()}
        self.blocks[block["id"]] = block
        self.children.setdefault(parent, []).append(block["id"])
        return deepcopy(block)

    def _request(self, method, path, payload=None):
        self.requests.append((method, path, deepcopy(payload)))
        operation = "page" if path == "/pages" else "meeting" if path == "/blocks/meeting_notes" else "append" if path.endswith("/children") else "other"
        if self.reject == operation:
            self.reject = None
            raise NotionAudioError(429)
        if path == "/pages":
            parent = payload["parent"]
            if parent["type"] == "page_id":
                title = payload["properties"]["title"]["title"][0]["text"]["content"]
                block = self._store(parent["page_id"], {"type": "child_page", "child_page": {"title": title}})
                result = {"id": block["id"], "url": f"https://notion.test/{block['id']}", **payload}
            else:
                page_id = self._id()
                result = {"id": page_id, "url": f"https://notion.test/{page_id}", **payload}
            self.pages[result["id"]] = result
        elif path == "/blocks/meeting_notes":
            result = self._store(payload["parent"]["page_id"], {
                "type": "meeting_notes", "meeting_notes": {"status": self.meeting_phase,
                    "title": self._rich_text("Complete synthetic meeting"),
                    "children": {"transcript_block_id": "transcript", "summary_block_id": "summary"}},
            })
        elif path.endswith("/children"):
            result = {"results": [self._store(path.split("/")[2], b) for b in payload["children"]]}
        elif path.startswith("/blocks/"):
            block = self.blocks[path.split("/")[2]]
            if method == "PATCH":
                block.update(deepcopy(payload))
            result = deepcopy(block)
        elif path.startswith("/pages/"):
            page = self.pages[path.split("/")[2]]
            page["properties"].update(payload["properties"])
            result = deepcopy(page)
        else:
            raise AssertionError(path)
        if self.lost == operation:
            self.lost = None
            raise TimeoutError("lost response after write")
        return result

    def upload_step(self, path, state, checkpoint):
        assert path.is_file()
        checkpoint(upload_id="synthetic-upload")
        return True

    def read_tree(self, block_id):
        return self._text_paragraphs(self.text) if block_id == "transcript" else [self._paragraph("Synthetic summary")]


@pytest.fixture
def fixture(tmp_path):
    settings = Settings(_env_file=None, project_root=tmp_path, notion_enabled=True, notion_transcribe_audio=True,
                        notion_client_ids=["owner"], notion_live_preview_enabled=True, notion_live_client_ids=["owner"],
                        notion_live_start_after=datetime.now(UTC) - timedelta(minutes=1), email_enabled=False,
                        basic_auth_username="test", basic_auth_password="synthetic-password")
    paths = ensure_directories(build_paths(settings))
    api, batcher = API(), Batcher()
    native = NativeNotionWorker(settings, paths, api, batcher)
    live = LiveNotionWorker(settings, paths, api, batcher)
    return settings, paths, api, native, live


def add(fixture, index, final=False, client="owner", recording="stream", audio=None):
    _, paths, _, native, _ = fixture
    directory = paths.chunks / recording
    directory.mkdir(parents=True, exist_ok=True)
    filename = f"part-{index}.m4a"
    audio = audio or bytes([index + 1]) * 100
    (directory / filename).write_bytes(audio)
    native.chunks.receive_chunk(session_id=recording, chunk_index=index, stored_filename=filename,
        original_filename="Synthetic meeting.m4a", source="apple-watch-stream", client_id=client, recipient=None,
        mime_type="audio/mp4", file_size=len(audio), content_hash=hashlib.sha256(audio).hexdigest(), is_final=final)


def run(fixture, cycles=15, final=False):
    _, paths, _, native, live = fixture
    for _ in range(cycles):
        with connect(paths.database) as db:
            db.execute("UPDATE notion_live_sessions SET next_attempt_at = '2000-01-01'")
            db.execute("UPDATE notion_audio_jobs SET next_attempt_at = '2000-01-01'")
        live.step()
        if final:
            native.step()


def state(fixture):
    with connect(fixture[1].database) as db:
        row = db.execute("SELECT * FROM notion_live_sessions WHERE session_id = 'stream'").fetchone()
    return json.loads(row["state_json"])


def main_blocks(fixture):
    return fixture[2]._list_children(state(fixture)["page_id"])


def test_live_text_before_final_and_same_page_for_complete_notes(fixture):
    add(fixture, 0)
    run(fixture)
    live = state(fixture)
    assert live["next_index"] == 1
    assert fixture[3].chunks.get_session("stream").job_id is None
    assert any(b.get("paragraph", {}).get("rich_text") == fixture[2]._rich_text("Synthetic segment transcript.") for b in main_blocks(fixture))
    assert all(p["options"]["kickoff_summary"] is False for _, path, p in fixture[2].requests if path == "/blocks/meeting_notes")
    add(fixture, 1, final=True)
    run(fixture, 25, final=True)
    session = fixture[3].chunks.get_session("stream")
    assert session.status == "done"
    memo = fixture[3].memos.get(session.job_id)
    assert memo.notion_page_id == live["page_id"]
    assert sum("Recording ID" in p["properties"] for p in fixture[2].pages.values()) == 1
    assert "Complete." in state(fixture)["status_text"]
    assert all(p.exists() for sources in fixture[3].audio_batcher.sources for p in sources)


def test_missing_and_duplicate_chunks_stay_ordered_across_restart(fixture):
    add(fixture, 0)
    run(fixture)
    add(fixture, 0)
    add(fixture, 2, final=True)
    run(fixture)
    assert state(fixture)["next_index"] == 1
    settings, paths, api, native, _ = fixture
    restarted = (*fixture[:4], LiveNotionWorker(settings, paths, api, native.audio_batcher))
    add(restarted, 1)
    run(restarted, 25)
    headings = [api._block_signature(b)[1] for b in main_blocks(restarted) if b["type"] == "heading_2"]
    assert headings == ["Live transcript", "Live part 001", "Live part 002", "Live part 003"]


@pytest.mark.parametrize("operation", ["page", "meeting", "append"])
def test_lost_responses_do_not_repeat_writes(fixture, operation):
    fixture[2].lost = operation
    add(fixture, 0)
    run(fixture, 25)
    assert state(fixture)["next_index"] == 1
    assert sum(path == "/pages" for _, path, _ in fixture[2].requests) == 2
    assert sum(path == "/blocks/meeting_notes" for _, path, _ in fixture[2].requests) == 1
    assert len(main_blocks(fixture)) == 5


def test_final_worker_waits_for_ambiguous_live_page(fixture):
    fixture[2].lost = "page"
    add(fixture, 0)
    run(fixture, 1)
    fixture[2].hidden = True
    add(fixture, 1, final=True)
    run(fixture, 8, final=True)
    assert len(fixture[2].pages) == 1
    assert fixture[3].chunks.get_session("stream").status != "done"
    fixture[2].hidden = False
    run(fixture, 25, final=True)
    assert fixture[3].chunks.get_session("stream").status == "done"
    assert sum("Recording ID" in p["properties"] for p in fixture[2].pages.values()) == 1


@pytest.mark.parametrize("operation", ["page", "meeting", "append"])
def test_explicit_rate_limit_retries(fixture, operation):
    fixture[2].reject = operation
    add(fixture, 0)
    run(fixture, 25)
    assert state(fixture)["next_index"] == 1


def test_other_clients_and_existing_recordings_are_not_enrolled(fixture):
    add(fixture, 0, client="craig", recording="other")
    add(fixture, 0)
    with connect(fixture[1].database) as db:
        db.execute("UPDATE recording_sessions SET created_at = '2000-01-01T00:00:00+00:00' WHERE id = 'stream'")
    run(fixture)
    assert not fixture[2].requests
    fixture[0].notion_live_client_ids = ["*"]
    assert not fixture[0].uses_live_notion("owner")


def test_replaced_published_chunk_pauses_preview_but_not_final(fixture):
    add(fixture, 0)
    run(fixture)
    with connect(fixture[1].database) as db:
        db.execute("UPDATE recording_chunks SET status = 'failed' WHERE session_id = 'stream' AND chunk_index = 0")
    add(fixture, 0, audio=b"replacement")
    add(fixture, 1, final=True)
    run(fixture, 1)
    assert "replaced" in state(fixture)["status_text"]
    assert state(fixture)["next_index"] == 1
    run(fixture, 25, final=True)
    assert fixture[3].chunks.get_session("stream").status == "done"


def test_silent_preview_skips_ai_but_keeps_final_audio(fixture):
    fixture[3].audio_batcher.silent = True
    add(fixture, 0)
    run(fixture)
    assert state(fixture)["next_index"] == 1
    assert not any(path == "/blocks/meeting_notes" for _, path, _ in fixture[2].requests)
    assert fixture[3].audio_batcher.sources[0][0].exists()


def test_long_transcript_is_not_truncated(fixture):
    fixture[2].text = "Synthetic words. " * 350
    add(fixture, 0)
    run(fixture)
    blocks = main_blocks(fixture)
    after_heading = next(i for i, b in enumerate(blocks) if fixture[2]._block_signature(b)[1] == "Live part 001") + 1
    actual = " ".join(fixture[2]._block_signature(b)[1] for b in blocks[after_heading:])
    expected = " ".join(fixture[2]._block_signature(b)[1] for b in fixture[2].read_tree("transcript"))
    assert actual.split() == expected.split()


def test_live_failure_does_not_block_complete_audio(fixture):
    fixture[2].meeting_phase = "transcription_failed"
    add(fixture, 0)
    run(fixture)
    assert state(fixture).get("next_index", 0) == 0
    fixture[2].meeting_phase = "notes_ready"
    add(fixture, 1, final=True)
    run(fixture, 25, final=True)
    assert fixture[3].chunks.get_session("stream").status == "done"


def test_live_waits_for_ambiguous_final_page(fixture):
    add(fixture, 0, final=True)
    fixture[2].lost = "page"
    for _ in range(2):
        with connect(fixture[1].database) as db:
            db.execute("UPDATE notion_audio_jobs SET next_attempt_at = '2000-01-01'")
        fixture[3].step()
    fixture[2].hidden = True
    run(fixture, 8)
    assert len(fixture[2].pages) == 1
    fixture[2].hidden = False
    run(fixture, 25, final=True)
    assert fixture[3].chunks.get_session("stream").status == "done"
    assert sum("Recording ID" in p["properties"] for p in fixture[2].pages.values()) == 1


def test_missing_cutoff_or_naive_cutoff_disables_live(fixture):
    settings = fixture[0]
    settings.notion_live_start_after = None
    assert not settings.uses_live_notion("owner")
    settings.notion_live_start_after = datetime(2026, 1, 1)
    assert not settings.uses_live_notion("owner")


def test_progress_exposes_live_page_only_to_owning_client(fixture):
    settings, paths, _, native, _ = fixture
    settings.notion_client_ids = settings.notion_live_client_ids = ["owner0001"]
    add(fixture, 0, client="owner0001")
    run(fixture)
    client = TestClient(create_app(settings, paths, native.store))
    auth = (settings.basic_auth_username, settings.basic_auth_password)
    response = client.get("/recordings/stream", auth=auth, headers={"X-Codex-Client-ID": "owner0001"})
    assert response.status_code == 200
    assert response.json()["notion_url"] == state(fixture)["page_url"]
    assert response.json()["live_notion_parts"] == 1
    assert client.get("/recordings/stream", auth=auth, headers={"X-Codex-Client-ID": "craig0001"}).status_code == 404
    assert client.get("/recordings/stream", headers={"X-Codex-Client-ID": "owner0001"}).status_code == 401
