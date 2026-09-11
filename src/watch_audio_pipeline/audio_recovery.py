"""Archive explicitly requested original chunks without reprocessing a memo."""

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from watch_audio_pipeline.chunk_uploads import SESSION_ID_PATTERN
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.uploads import CHUNK_SIZE


class AudioRecovery:
    def __init__(self, paths: AppPaths, chunks: ChunkStore):
        self.root = paths.data / "recovered-audio"
        self.chunks = chunks

    def _source(self, recording_id: str, client_id: str):
        if not SESSION_ID_PATTERN.fullmatch(recording_id):
            raise ValueError("invalid recording_id")
        session = self.chunks.get_session(recording_id)
        if session is None or session.client_id != client_id:
            raise ValueError("recording not found for this device")
        chunks = self.chunks.list_chunks(recording_id)
        if (
            session.status != "done"
            or session.final_chunk_index is None
            or [c.chunk_index for c in chunks] != list(range(session.final_chunk_index + 1))
        ):
            raise ValueError("only completed recordings can be recovered")
        return session, chunks, self.root / recording_id

    @staticmethod
    def _filename(chunk) -> str:
        return f"{chunk.chunk_index:06d}-{chunk.content_hash[:16]}{Path(chunk.stored_filename).suffix}"

    def start(self, recording_id: str, client_id: str) -> dict:
        session, chunks, directory = self._source(recording_id, client_id)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "recording_id": recording_id,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "original_filename": session.original_filename,
            "purpose": "original audio recovery; no transcription or email",
            "chunks": [
                {key: asdict(chunk)[key] for key in (
                    "chunk_index", "content_hash", "file_size", "duration_seconds"
                )}
                for chunk in chunks
            ],
        }
        # Repeated requests preserve the first request and all recovered audio.
        try:
            with (directory / "manifest.json").open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2)
        except FileExistsError:
            pass
        return self.progress(recording_id, client_id)

    def progress(self, recording_id: str, client_id: str) -> dict:
        _, chunks, directory = self._source(recording_id, client_id)
        requested = (directory / "manifest.json").is_file()
        received = []
        for chunk in chunks:
            path = directory / self._filename(chunk)
            if path.is_file() and path.stat().st_size == chunk.file_size:
                received.append(chunk.chunk_index)
        received_set = set(received)
        missing = [c.chunk_index for c in chunks if c.chunk_index not in received_set]
        return {
            "recording_id": recording_id,
            "status": "complete" if requested and not missing else (
                "waiting_for_watch" if requested else "not_requested"
            ),
            "expected_chunks": len(chunks),
            "received_chunks": len(received),
            "missing_chunk_indexes": missing,
            "duration_seconds": sum(c.duration_seconds or 0 for c in chunks),
        }

    def receive(self, recording_id: str, client_id: str, chunk_index: int,
                file: UploadFile, max_upload_bytes: int) -> dict:
        _, chunks, directory = self._source(recording_id, client_id)
        if not (directory / "manifest.json").is_file():
            raise ValueError("audio recovery must be requested first")
        chunk = next((c for c in chunks if c.chunk_index == chunk_index), None)
        if chunk is None:
            raise ValueError("chunk index is not part of the original recording")
        target = directory / self._filename(chunk)
        temporary = directory / f"upload-{uuid4().hex}.tmp"
        digest = sha256()
        size = 0
        try:
            with temporary.open("xb") as handle:
                while data := file.file.read(CHUNK_SIZE):
                    size += len(data)
                    if size > min(max_upload_bytes, chunk.file_size):
                        raise ValueError("recovered chunk exceeds original size")
                    digest.update(data)
                    handle.write(data)
            if size != chunk.file_size or digest.hexdigest() != chunk.content_hash:
                raise ValueError("recovered audio does not match the original chunk")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return self.progress(recording_id, client_id)
