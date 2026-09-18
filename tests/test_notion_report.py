from copy import deepcopy
import json

import pytest

from watch_audio_pipeline.notion_audio import connect
from watch_audio_pipeline.notion_report import (
    INTERVENTION_PREFIX, NotionReport, PENDING_INTERVENTIONS, PENDING_NARRATIVE,
    full_rich_text, parse_native_draft,
)
from test_notion_live import API, add, fixture, main_blocks, run, state


NARRATIVE = (
    "(FirstPass: Unable to determine from the transcript alone; structured PCR fields and required timing need review.)\n\n"
    "Arrived on scene to find a 40-year-old male reporting pain after a fall. "
    "The patient described pain in the left leg and denied losing consciousness. "
    "FD crew assessed the patient, applied a splint to the injured leg, and assisted "
    "the patient onto the stretcher. The patient reported less pain after splinting. "
    "Sunstar crew assumed patient care and transported the patient."
)
INTERVENTIONS = "FD crew assessed the patient, applied a leg splint with reported pain improvement, and assisted with stretcher movement. Sunstar crew transported the patient."
DRAFT = NARRATIVE + "\n\n" + INTERVENTION_PREFIX + " " + INTERVENTIONS


def test_native_draft_contract_and_complete_text():
    api = API()
    draft = parse_native_draft(api._text_paragraphs(DRAFT))
    assert draft.narrative == NARRATIVE
    assert draft.interventions == INTERVENTIONS
    long = "Evidence-backed words. " * 350
    assert "".join(p["text"]["content"] for p in full_rich_text(long)) == long


@pytest.mark.parametrize("text", [
    "Generic meeting summary.",
    DRAFT.replace("Arrived on scene", "- Arrived on scene"),
    DRAFT.replace("40-year-old", "BP 120/80. 40-year-old"),
    DRAFT.replace("40-year-old", "SpO2 was 96. 40-year-old"),
    DRAFT.replace("40-year-old", "At 10:30 a 40-year-old"),
    DRAFT.replace("assessed the patient", "seamlessly assessed the patient"),
    DRAFT.replace(INTERVENTION_PREFIX, "Planned interventions:"),
    "(FirstPass: Unknown.)\n\nBrief.\n\n" + INTERVENTION_PREFIX + " None.",
])
def test_nonconforming_native_summary_stays_pending(text):
    assert parse_native_draft(API()._text_paragraphs(text)) is None


def test_rich_text_and_headings_are_not_accepted_as_plain_narrative():
    blocks = API()._text_paragraphs(DRAFT)
    blocks[1]["paragraph"]["rich_text"][0]["annotations"] = {"bold": True}
    assert parse_native_draft(blocks) is None
    assert parse_native_draft([API()._heading("Summary"), *API()._text_paragraphs(DRAFT)]) is None


class ReportAPI(API):
    def __init__(self):
        super().__init__()
        self.summary_text = "Generic summary"

    def _request(self, method, path, payload=None):
        result = super()._request(method, path, payload)
        if path == "/blocks/meeting_notes":
            parent = payload["parent"]["page_id"]
            self.children[parent].remove(result["id"])
            self.children[parent].insert(0, result["id"])
        if path == "/pages":
            for block in payload.get("children", []):
                self._store(result["id"], block)
        return result

    def read_tree(self, block_id):
        if block_id == "summary":
            return self._text_paragraphs(self.summary_text)
        return super().read_tree(block_id)


def test_report_insertion_does_not_reparse_existing_transcript_markdown():
    api, state = ReportAPI(), {}
    original = api._store("page", api._paragraph("- Spoken sentence.\n# Literal speech.\n* Not formatting."))
    before = deepcopy(original)
    report = NotionReport(api, revision="source")
    checkpoint = lambda **updates: state.update(updates)
    assert not report.ensure_layout("page", state, checkpoint)
    assert report.ensure_layout("page", state, checkpoint)
    assert api.blocks[original["id"]] == before
    assert api._list_children("page")[-1] == before
    assert api.requests == [("PATCH", "/blocks/page/children", {
        "children": report.layout_blocks(), "position": {"type": "start"},
    })]


def test_report_insert_lost_response_reconciles_without_duplicate():
    api, state = ReportAPI(), {}
    report = NotionReport(api, revision="source")
    checkpoint = lambda **updates: state.update(updates)
    api.lost = "append"
    with pytest.raises(TimeoutError):
        report.ensure_layout("page", state, checkpoint)
    assert report.ensure_layout("page", state, checkpoint)
    assert len(api._list_children("page")) == 6
    assert len(api.requests) == 1


def enable(fixture):
    settings, paths, _, native, live = fixture
    settings.notion_report_client_ids = ["owner"]
    api = ReportAPI()
    native.api = live.api = api
    return settings, paths, api, native, live


def task_state(fixture):
    with connect(fixture[1].database) as db:
        row = db.execute("SELECT * FROM notion_audio_jobs ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row), json.loads(row["state_json"])


def test_full_page_order_audio_and_pending_generation_do_not_block_transcript(fixture):
    fixture = enable(fixture)
    add(fixture, 0)
    run(fixture)
    add(fixture, 1, final=True)
    run(fixture, 30, final=True)
    assert fixture[3].chunks.get_session("stream").status == "done"
    blocks = main_blocks(fixture)
    texts = [fixture[2]._block_signature(b)[1] for b in blocks]
    assert texts.index("Narrative draft") < texts.index("Interventions documented") < texts.index("Live transcript")
    assert texts.index("Interventions documented") < next(i for i, b in enumerate(blocks) if b["type"] == "meeting_notes")
    assert PENDING_NARRATIVE in texts and PENDING_INTERVENTIONS in texts
    assert len([b for b in blocks if b["type"] == "audio"]) == 1
    task, report = task_state(fixture)
    assert report["report_status"] == "waiting_for_narrative"
    assert fixture[2].pages[report["page_id"]]["properties"]["Delivery"]["select"]["name"] == "Needs review"
    assert fixture[3].memos.get(task["job_id"]).transcript_path
    assert all(p.exists() for sources in fixture[3].audio_batcher.sources for p in sources)


def test_notion_summary_retry_is_projected_without_ai_call_or_transcript_edit(fixture):
    fixture = enable(fixture)
    add(fixture, 0, final=True)
    run(fixture, 30, final=True)
    task, report = task_state(fixture)
    requests_before = len(fixture[2].requests)
    fixture[2].summary_text = DRAFT
    with connect(fixture[1].database) as db:
        db.execute("UPDATE notion_audio_jobs SET next_attempt_at = '2000-01-01'")
    fixture[3].refresh_completed_report()
    _, updated = task_state(fixture)
    assert updated["report_status"] == "draft_ready"
    assert fixture[2]._block_signature(fixture[2].blocks[updated["report_slots"]["narrative"]])[1] == NARRATIVE
    assert fixture[2]._block_signature(fixture[2].blocks[updated["report_slots"]["interventions"]])[1] == INTERVENTIONS
    assert not any(method == "POST" for method, _, _ in fixture[2].requests[requests_before:])
    assert not any(method == "PATCH" and "transcript" in path for method, path, _ in fixture[2].requests)


def test_user_edits_are_preserved(fixture):
    fixture = enable(fixture)
    add(fixture, 0, final=True)
    run(fixture, 30, final=True)
    _, report = task_state(fixture)
    slot = report["report_slots"]["narrative"]
    fixture[2].blocks[slot]["paragraph"]["rich_text"] = full_rich_text("Clinician's own draft.")
    fixture[2].summary_text = DRAFT
    with connect(fixture[1].database) as db:
        db.execute("UPDATE notion_audio_jobs SET next_attempt_at = '2000-01-01'")
    fixture[3].refresh_completed_report()
    assert task_state(fixture)[1]["report_status"] == "human_edited"
    assert fixture[2]._block_signature(fixture[2].blocks[slot])[1] == "Clinician's own draft."


def test_attachment_lost_response_does_not_duplicate_or_reupload(tmp_path):
    api, state = ReportAPI(), {"upload_id": "full-upload"}
    path = tmp_path / "complete.m4a"
    path.write_bytes(b"synthetic audio")
    report = NotionReport(api)
    api.lost = "append"
    with pytest.raises(TimeoutError):
        report.attach_audio("page", path, "revision", state, lambda **kw: state.update(kw))
    assert report.attach_audio("page", path, "revision", state, lambda **kw: state.update(kw))
    assert len(api._list_children("page")) == 1
    api.hidden = True
    with pytest.raises(RuntimeError, match="reconciliation"):
        report.attach_audio("page", path, "revision", state, lambda **kw: state.update(kw))
    assert len(api.children["page"]) == 1


def test_reports_never_enroll_other_clients_or_wildcard(fixture):
    settings = fixture[0]
    settings.notion_client_ids = ["*"]
    settings.notion_report_client_ids = ["owner"]
    assert settings.uses_notion_report("owner")
    assert not settings.uses_notion_report("other")
    settings.notion_report_client_ids = ["*"]
    assert not settings.uses_notion_report("owner")


def test_draft_refresh_error_cannot_mark_completed_recording_failed(fixture):
    fixture = enable(fixture)
    add(fixture, 0, final=True)
    run(fixture, 30, final=True)
    task, _ = task_state(fixture)
    fixture[2].lost = "other"
    with connect(fixture[1].database) as db:
        db.execute("UPDATE notion_audio_jobs SET next_attempt_at = '2000-01-01'")
    fixture[3].refresh_completed_report()
    assert fixture[3].store.get_job(task["job_id"]).status == "done"


def test_late_revision_does_not_reuse_an_older_narrative(fixture):
    fixture = enable(fixture)
    fixture[2].summary_text = DRAFT
    add(fixture, 0, final=True)
    run(fixture, 30, final=True)
    _, original = task_state(fixture)
    assert original["report_status"] == "draft_ready"
    fixture[2].summary_text = "Generic revised summary"
    add(fixture, 1, final=True)
    run(fixture, 30, final=True)
    _, revised = task_state(fixture)
    assert revised["report_slots"] != original["report_slots"]
    assert revised["report_status"] == "waiting_for_narrative"
    blocks = main_blocks(fixture)
    assert fixture[2]._block_signature(blocks[3])[1] == PENDING_NARRATIVE
