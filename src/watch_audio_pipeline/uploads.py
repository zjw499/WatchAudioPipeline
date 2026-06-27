from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import os
import uuid

from fastapi import UploadFile

from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobRecord, JobStore


ALLOWED_EXTENSIONS = {".m4a", ".mp3", ".wav", ".caf"}
ALLOWED_MIME_PREFIXES = ("audio/",)
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class UploadResult:
    job: JobRecord
    created: bool


async def queue_upload(
    *,
    file: UploadFile,
    source: str,
    paths: AppPaths,
    store: JobStore,
    max_upload_bytes: int,
) -> UploadResult:
    filename = file.filename or "upload.bin"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"unsupported file type: {suffix}")

    content_type = file.content_type or "application/octet-stream"
    if not any(content_type.startswith(prefix) for prefix in ALLOWED_MIME_PREFIXES):
        raise ValueError(f"unsupported mime type: {content_type}")

    temp_path = paths.incoming / f"upload-{uuid.uuid4().hex}{suffix}"
    digest = sha256()
    file_size = 0

    with temp_path.open("wb") as handle:
        while True:
            chunk = await file.read(CHUNK_SIZE)
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
    return UploadResult(job=job, created=True)
