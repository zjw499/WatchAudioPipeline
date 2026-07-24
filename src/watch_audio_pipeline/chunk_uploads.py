from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import logging
import os
import re
import uuid

from fastapi import UploadFile

from watch_audio_pipeline.chunks import ChunkReceipt, ChunkStore
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.uploads import ALLOWED_EXTENSIONS, ALLOWED_MIME_PREFIXES, CHUNK_SIZE


SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
chunk_logger = logging.getLogger("upload")


@dataclass(frozen=True)
class QueuedChunk:
    receipt: ChunkReceipt
    stored_path: Path


def queue_chunk_upload(
    *,
    file: UploadFile,
    recording_id: str,
    chunk_index: int,
    is_final: bool,
    source: str,
    paths: AppPaths,
    chunk_store: ChunkStore,
    max_upload_bytes: int,
) -> QueuedChunk:
    if not SESSION_ID_PATTERN.fullmatch(recording_id):
        raise ValueError("invalid recording_id")
    if chunk_index < 0 or chunk_index > 10000:
        raise ValueError("chunk_index must be between 0 and 10000")

    filename = Path(file.filename or f"chunk-{chunk_index}.m4a").name
    suffix = Path(filename).suffix.lower()
    content_type = file.content_type or "application/octet-stream"
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported file type: {suffix}")
    if not any(content_type.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
        raise ValueError(f"unsupported mime type: {content_type}")

    session_directory = paths.chunks / recording_id
    session_directory.mkdir(parents=True, exist_ok=True)
    temp_path = session_directory / f"upload-{uuid.uuid4().hex}{suffix}"
    digest = sha256()
    file_size = 0
    with temp_path.open("wb") as handle:
        while True:
            data = file.file.read(CHUNK_SIZE)
            if not data:
                break
            handle.write(data)
            digest.update(data)
            file_size += len(data)
            if file_size > max_upload_bytes:
                handle.close()
                temp_path.unlink(missing_ok=True)
                raise ValueError(f"chunk too large: {file_size} bytes")
    if file_size == 0:
        temp_path.unlink(missing_ok=True)
        raise ValueError("chunk is empty")

    content_hash = digest.hexdigest()
    stored_filename = f"{chunk_index:06d}-{content_hash[:16]}{suffix}"
    final_path = session_directory / stored_filename
    existing_path = next(session_directory.glob(f"{chunk_index:06d}-*{suffix}"), None)
    if existing_path is None:
        os.replace(temp_path, final_path)
    else:
        temp_path.unlink(missing_ok=True)
        final_path = existing_path

    try:
        receipt = chunk_store.receive_chunk(
            session_id=recording_id,
            chunk_index=chunk_index,
            stored_filename=final_path.name,
            original_filename=filename,
            source=source,
            mime_type=content_type,
            file_size=file_size,
            content_hash=content_hash,
            is_final=is_final,
        )
        session = chunk_store.get_session(recording_id)
        if not receipt.created and session is not None and session.status == "done":
            final_path.unlink(missing_ok=True)
            try:
                session_directory.rmdir()
            except OSError:
                pass
    except Exception:
        if existing_path is None:
            final_path.unlink(missing_ok=True)
        raise

    chunk_logger.info(
        "stream chunk session_id=%s index=%s final=%s bytes=%s created=%s",
        recording_id,
        chunk_index,
        is_final,
        file_size,
        receipt.created,
    )
    return QueuedChunk(receipt=receipt, stored_path=final_path)
