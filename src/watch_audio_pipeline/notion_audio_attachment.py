"""Preserve complete Notion-hosted audio independently from AI report generation."""


REJECTED = {400, 401, 403, 404, 429}


class NotionAudioAttachment:
    def __init__(self, api):
        self.api = api

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
                    "caption": self.api._rich_text(caption),
                },
            }]})
        except Exception as exc:
            if getattr(exc, "status", None) in REJECTED:
                checkpoint(audio_attach_attempted=False)
            raise
        return False
