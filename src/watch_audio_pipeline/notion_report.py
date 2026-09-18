"""Present native Notion drafts without treating generic summaries as EMS reports."""

from dataclasses import dataclass
import hashlib
import re

from watch_audio_pipeline.notion_delivery import NotionPublisher


NARRATIVE_HEADING = "Narrative draft"
INTERVENTIONS_HEADING = "Interventions documented"
PENDING_NARRATIVE = (
    "Narrative pending: configure the Pinellas custom instructions in Notion. "
    "A generic meeting summary is not an EMS narrative."
)
PENDING_INTERVENTIONS = "Pending a source-grounded review of the complete transcript."
INTERVENTION_PREFIX = "Interventions documented in transcript:"
REJECTED = {400, 401, 403, 404, 429}


@dataclass(frozen=True)
class NarrativeDraft:
    narrative: str
    interventions: str


def parse_native_draft(blocks):
    """Check the presentation contract, NOT clinical accuracy or FirstPass compliance."""
    content = []
    for block in blocks:
        kind, text = NotionPublisher._block_signature(block)
        if not text.strip():
            continue
        if kind != "paragraph":
            return None
        rich_text = block.get("paragraph", {}).get("rich_text", [])
        if any(any(p.get("annotations", {}).get(key) for key in ("bold", "italic", "code", "strikethrough", "underline")) for p in rich_text):
            return None
        content.append(text.strip())
    text = "\n\n".join(content)
    if not re.match(r"^\(FirstPass:[^\n]+\)\s*\n", text):
        return None
    if text.count(INTERVENTION_PREFIX) != 1:
        return None
    narrative, interventions = text.split(INTERVENTION_PREFIX)
    body = narrative.split("\n", 1)[1].strip()
    if len(body) <= 325 or not interventions.strip() or len(text) > 24000:
        return None
    if re.search(r"(?im)^\s*(?:[-*#>]\s|\d+[.)]\s|[SOAP]:\s)|\*\*|```", text):
        return None
    if re.search(r"(?i)\bseamless(?:ly)?\b|no interventions required|0\.9% sodium chloride", text):
        return None
    if re.search(r"(?i)\b(?:BP|SpO2|GCS|EtCO2|MAP|HR|RR|blood pressure|heart rate|respiratory rate|oxygen saturation)\b\s*(?:(?:was|of|is|at)\s+)?[:=]?\s*\d|\b\d{1,3}\s*/\s*\d{1,3}\b|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b", text):
        return None
    return NarrativeDraft(narrative.strip(), interventions.strip())


def full_rich_text(text):
    return [{"type": "text", "text": {"content": text[i:i + 1900]}} for i in range(0, len(text), 1900)]


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


class NotionReport:
    def __init__(self, api, instructions_url="", revision="current"):
        self.api = api
        self.instructions_url = instructions_url
        self.marker = f"Scribe Pilot source revision: {revision}"

    def layout_blocks(self):
        notice = self.api._paragraph(
            "EMS / Fire draft: clinician review required. FirstPass is provisional; "
            "missing transcript details do not prove care was omitted."
        )
        if self.instructions_url:
            notice["paragraph"]["rich_text"].append({"type": "text", "text": {
                "content": " Instruction set", "link": {"url": self.instructions_url},
            }})
        return [self.api._paragraph(self.marker), notice, self.api._heading(NARRATIVE_HEADING), self.api._paragraph(PENDING_NARRATIVE),
                self.api._heading(INTERVENTIONS_HEADING), self.api._paragraph(PENDING_INTERVENTIONS)]

    def ensure_layout(self, page_id, state, checkpoint):
        blocks = self.api._list_children(page_id)
        markers = [i for i, b in enumerate(blocks) if self.api._block_signature(b) == ("paragraph", self.marker)]
        if len(markers) > 1:
            raise RuntimeError("Ambiguous narrative source revision; preserve for review")
        # A late audio revision gets a new report, never yesterday's narrative.
        blocks = blocks[markers[0] + 1:markers[0] + 6] if markers else []
        slots = {}
        for heading, key in ((NARRATIVE_HEADING, "narrative"), (INTERVENTIONS_HEADING, "interventions")):
            matches = [i for i, b in enumerate(blocks) if self.api._block_signature(b) == ("heading_2", heading)]
            if len(matches) > 1:
                raise RuntimeError("Ambiguous narrative layout; preserve for review")
            if matches:
                index = matches[0] + 1
                if index >= len(blocks) or blocks[index]["type"] != "paragraph":
                    raise RuntimeError("Narrative layout edited; preserve for review")
                slots[key] = blocks[index]["id"]
        if len(slots) == 2:
            checkpoint(report_slots=slots)
            return True
        if slots or state.get("report_layout_attempted"):
            raise RuntimeError("Waiting for narrative layout reconciliation")
        checkpoint(report_layout_attempted=True)
        try:
            # Markdown insertion can reinterpret existing transcript punctuation.
            self.api._request("PATCH", f"/blocks/{page_id}/children", {
                "children": self.layout_blocks(), "position": {"type": "start"},
            })
        except Exception as exc:
            if getattr(exc, "status", None) in REJECTED:
                checkpoint(report_layout_attempted=False)
            raise
        return False

    def attach_audio(self, page_id, output, revision, state, checkpoint):
        caption = f"Complete recording - source revision {revision}"
        children = self.api._list_children(page_id)
        matches = [b for b in children if b["type"] == "audio" and
                   "".join(p.get("plain_text", p.get("text", {}).get("content", ""))
                           for p in b["audio"].get("caption", [])) == caption]
        if len(matches) == 1:
            if matches[0]["audio"].get("type") not in {"file", "file_upload"}:
                raise RuntimeError("Recording attachment is not Notion-hosted")
            checkpoint(audio_block_id=matches[0]["id"])
            return True
        if matches or state.get("audio_attach_attempted") or state.get("audio_block_id"):
            raise RuntimeError("Waiting for recording attachment reconciliation")
        # Reuse the full meeting's private upload. Refresh it if it expired.
        if not self.api.upload_step(output, state, checkpoint):
            return False
        checkpoint(audio_attach_attempted=True)
        try:
            self.api._request("PATCH", f"/blocks/{page_id}/children", {"children": [{
                "object": "block", "type": "audio", "audio": {
                    "type": "file_upload", "file_upload": {"id": state["upload_id"]},
                    "caption": full_rich_text(caption),
                },
            }]})
        except Exception as exc:
            if getattr(exc, "status", None) in REJECTED:
                checkpoint(audio_attach_attempted=False)
            raise
        return False

    def publish_draft(self, summary_blocks, state, checkpoint):
        draft = parse_native_draft(summary_blocks)
        if draft is None:
            checkpoint(report_status="waiting_for_narrative")
            return False
        values = {"narrative": draft.narrative, "interventions": draft.interventions}
        placeholders = {"narrative": PENDING_NARRATIVE, "interventions": PENDING_INTERVENTIONS}
        for key, value in values.items():
            block_id = state["report_slots"][key]
            current = self.api._request("GET", f"/blocks/{block_id}")
            kind, text = self.api._block_signature(current)
            allowed = {placeholders[key], value}
            if kind != "paragraph" or (text not in allowed and _hash(text) != state.get(f"report_{key}_hash")):
                checkpoint(report_status="human_edited")
                return False
        for key, value in values.items():
            block_id = state["report_slots"][key]
            self.api._request("PATCH", f"/blocks/{block_id}", {"paragraph": {"rich_text": full_rich_text(value)}})
            actual = self.api._request("GET", f"/blocks/{block_id}")
            if self.api._block_signature(actual) != ("paragraph", value):
                raise RuntimeError("Narrative readback incomplete")
            checkpoint(**{f"report_{key}_hash": _hash(value)})
        checkpoint(report_status="draft_ready")
        return True
