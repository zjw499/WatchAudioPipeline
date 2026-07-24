from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO
import logging
import mimetypes
import os
import uuid

from fastapi import UploadFile

from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobRecord, JobStore


ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".caf"}
ALLOWED_MIME_PREFIXES = ("audio/",)
CHUNK_SIZE = 1024 * 1024
upload_logger = logging.getLogger("upload")


@dataclass(frozen=True)
class UploadResult:
    job: JobRecord
    created: bool


def queue_upload(
    *,
    file: UploadFile,
    source: str,
    paths: AppPaths,
    store: JobStore,
    max_upload_bytes: int,
) -> UploadResult:
    filename = file.filename or "upload.bin"
    content_type = file.content_type or "application/octet-stream"
    return _queue_stream(
        stream=file.file,
        filename=filename,
        content_type=content_type,
        source=source,
        paths=paths,
        store=store,
        max_upload_bytes=max_upload_bytes,
    )


def queue_local_file(
    *,
    file_path: Path,
    source: str,
    paths: AppPaths,
    store: JobStore,
    max_upload_bytes: int,
) -> UploadResult:
    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or f"audio/{file_path.suffix.lower().lstrip('.')}"
    with file_path.open("rb") as stream:
        return _queue_stream(
            stream=stream,
            filename=filename,
            content_type=content_type,
            source=source,
            paths=paths,
            store=store,
            max_upload_bytes=max_upload_bytes,
        )


def _queue_stream(
    *,
    stream: BinaryIO,
    filename: str,
    content_type: str,
    source: str,
    paths: AppPaths,
    store: JobStore,
    max_upload_bytes: int,
) -> UploadResult:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported file type: {suffix}")

    if not any(content_type.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
        raise ValueError(f"unsupported mime type: {content_type}")

    temp_path = paths.incoming / f"upload-{uuid.uuid4().hex}{suffix}"
    digest = sha256()
    file_size = 0

    with temp_path.open("wb") as handle:
        while True:
            chunk = stream.read(CHUNK_SIZE)
            if not chunk:
                break
            handle.write(chunk)
            digest.update(chunk)
            file_size += len(chunk)
            if file_size > max_upload_bytes:
                handle.close()
                temp_path.unlink(missing_ok=True)
                raise ValueError(f"file too large: {file_size} bytes")

    content_hash = digest.hexdigest()
    stored_filename = f"{content_hash}{suffix}"
    final_path = paths.incoming / stored_filename

    existing = store.get_by_hash(content_hash)
    if existing is not None:
        temp_path.unlink(missing_ok=True)
        upload_logger.info("duplicate content hash=%s source=%s", content_hash, source)
        return UploadResult(job=existing, created=False)

    os.replace(temp_path, final_path)
    job = store.create_job(
        source=source,
        original_filename=filename,
        stored_filename=stored_filename,
        mime_type=content_type,
        file_size=file_size,
        content_hash=content_hash,
    )
    upload_logger.info(
        "queued job_id=%s stored_filename=%s bytes=%s",
        job.id,
        job.stored_filename,
        file_size,
    )
    return UploadResult(job=job, created=True)
