"""Publish an instruction reference, not an installed Notion custom-summary template."""

import argparse
import json
from pathlib import Path

from watch_audio_pipeline.cli import build_notion_publisher
from watch_audio_pipeline.config import load_settings
from watch_audio_pipeline.notion_audio import NativeNotionAPI, NotionAudioError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-id", required=True)
    args = parser.parse_args()
    settings = load_settings()
    source = settings.project_root / "docs" / "pinellas-narrative-instructions.md"
    journal = settings.project_root / ".runtime" / "notion-narrative-instructions.json"
    state = json.loads(journal.read_text()) if journal.exists() else {}
    title = "Scribe Pilot - Pinellas Narrative Instructions"
    publisher = build_notion_publisher(settings)
    api = NativeNotionAPI(token=publisher.token, data_source_id=publisher.data_source_id,
                          api_base=publisher.api_base, api_version=publisher.api_version)

    def save(**updates):
        state.update(updates)
        journal.parent.mkdir(parents=True, exist_ok=True)
        temporary = journal.with_suffix(".tmp")
        temporary.write_text(json.dumps(state), encoding="utf-8")
        temporary.replace(journal)

    if state.get("parent_id") and state["parent_id"] != args.parent_id:
        raise RuntimeError("Instruction page parent differs from saved setup")
    save(parent_id=args.parent_id)
    if not state.get("page_id"):
        matches = [b for b in api._list_children(args.parent_id)
                   if b["type"] == "child_page" and b["child_page"].get("title") == title]
        if len(matches) == 1:
            page = api._request("GET", f"/pages/{matches[0]['id']}")
        elif matches or state.get("attempted"):
            raise RuntimeError("Instruction page creation needs reconciliation")
        else:
            save(attempted=True)
            try:
                page = api._request("POST", "/pages", {
                    "parent": {"type": "page_id", "page_id": args.parent_id},
                    "properties": {"title": {"title": api._rich_text(title)}},
                })
            except NotionAudioError as exc:
                if exc.status in {400, 401, 403, 404, 429}:
                    save(attempted=False)
                raise
        save(page_id=page["id"], url=page["url"])
    blocks = [api._paragraph(
        "SETUP PENDING: This is an instruction reference, not an installed custom-summary template. "
        "In a native Notion meeting, open Instructions > Add custom instructions and put the instruction "
        "set below in that template. Select it and set it as default if appropriate for your other meetings. "
        "Test a synthetic Scribe Pilot recording: defaults for API-created meetings are not yet verified. "
        "For an existing completed meeting select the instructions and choose Retry summary. "
        "No PHI approval or BAA is established by this setup."
    )]
    for section in source.read_text(encoding="utf-8").split("\n\n"):
        if section.startswith("#"):
            blocks.append(api._heading(section.lstrip("# ").strip()))
        else:
            blocks.extend(api._text_paragraphs(section))
    accepted = api._list_children(state["page_id"])
    if len(accepted) > len(blocks) or any(api._block_signature(a) != api._block_signature(b)
                                        for a, b in zip(accepted, blocks)):
        raise RuntimeError("Instruction reference changed; preserve it for review")
    if state.get("append_attempted") and len(accepted) < state["append_target"]:
        raise RuntimeError("Waiting for instruction append reconciliation")
    for offset in range(len(accepted), len(blocks), 60):
        batch = blocks[offset:offset + 60]
        save(append_attempted=True, append_target=offset + len(batch))
        try:
            api._request("PATCH", f"/blocks/{state['page_id']}/children", {"children": batch})
        except NotionAudioError as exc:
            if exc.status in {400, 401, 403, 404, 429}:
                save(append_attempted=False)
            raise
        save(append_attempted=False)
    verified = api._list_children(state["page_id"])
    assert [api._block_signature(b) for b in verified] == [api._block_signature(b) for b in blocks]
    save(verified=True, installed_as_custom_instructions=False)
    print(json.dumps({"url": state["url"], "blocks_verified": len(verified), "template_setup": "pending"}))


if __name__ == "__main__":
    main()
