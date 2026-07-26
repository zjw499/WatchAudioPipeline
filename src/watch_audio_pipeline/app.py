from secrets import compare_digest
from hashlib import sha256
from pathlib import Path
import logging

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.chunk_uploads import queue_chunk_upload
from watch_audio_pipeline.chunks import ChunkStore
from watch_audio_pipeline.memos import MemoStore
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.recipients import normalize_client_id, normalize_recipient
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.summarization import fallback_title
from watch_audio_pipeline.uploads import queue_upload


basic_auth = HTTPBasic(auto_error=False)
app_logger = logging.getLogger("watch_audio_pipeline.app")


class TranscriptUpload(BaseModel):
    transcript: str
    filename: str = "recording.m4a"
    source: str = "iphone-on-device"
    recipient: str | None = None


def require_basic_auth(settings: Settings, credentials: HTTPBasicCredentials | None) -> None:
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="basic auth required",
            headers={"WWW-Authenticate": "Basic"},
        )

    username_ok = compare_digest(credentials.username, settings.basic_auth_username)
    password_ok = compare_digest(credentials.password, settings.basic_auth_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="invalid basic auth credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


def request_client_id(request: Request) -> str:
    try:
        return normalize_client_id(request.headers.get("X-Codex-Client-ID"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

def create_app(
    settings: Settings,
    paths: AppPaths,
    store: JobStore,
    memo_store: MemoStore | None = None,
    chunk_store: ChunkStore | None = None,
) -> FastAPI:
    if settings.basic_auth_username in {"", "replace-me"}:
        raise ValueError("basic auth username must be configured")
    if settings.basic_auth_password in {"", "replace-me"}:
        raise ValueError("basic auth password must be configured")

    app = FastAPI(title="Watch Audio Pipeline")
    memo_store = memo_store or MemoStore(paths.database)
    chunk_store = chunk_store or ChunkStore(paths.database)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "server_version": settings.server_version,
            "api_version": "1",
        }

    @app.post("/upload")
    def upload_audio(
        file: UploadFile = File(...),
        source: str = Form("iphone-shortcuts"),
        client_id: str | None = Form(None),
        recipient: str | None = Form(None),
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)

        try:
            result = queue_upload(
                file=file,
                source=source,
                client_id=normalize_client_id(client_id),
                recipient=normalize_recipient(recipient),
                paths=paths,
                store=store,
                max_upload_bytes=settings.max_upload_bytes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        return JSONResponse(
            {
                "status": "queued" if result.created else "duplicate",
                "job_id": result.job.id,
                "stored_filename": result.job.stored_filename,
            },
            status_code=201 if result.created else 200,
        )

    @app.post("/upload/chunk")
    def upload_audio_chunk(
        file: UploadFile = File(...),
        recording_id: str = Form(...),
        chunk_index: int = Form(...),
        is_final: bool = Form(False),
        source: str = Form("apple-watch-stream"),
        client_id: str | None = Form(None),
        recipient: str | None = Form(None),
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        try:
            queued = queue_chunk_upload(
                file=file,
                recording_id=recording_id,
                chunk_index=chunk_index,
                is_final=is_final,
                source=source.strip() or "apple-watch-stream",
                client_id=normalize_client_id(client_id),
                recipient=normalize_recipient(recipient),
                paths=paths,
                chunk_store=chunk_store,
                max_upload_bytes=settings.max_upload_bytes,
            )
        except ValueError as exc:
            app_logger.warning(
                "chunk upload rejected recording_id=%s chunk_index=%s final=%s reason=%s",
                recording_id,
                chunk_index,
                is_final,
                exc,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        receipt = queued.receipt
        return JSONResponse(
            {
                "status": "queued" if receipt.created else "duplicate",
                "recording_id": recording_id,
                "chunk_index": chunk_index,
                "received_chunks": receipt.received_count,
                "final_chunk_index": receipt.final_chunk_index,
            },
            status_code=201 if receipt.created else 200,
        )

    @app.get("/recordings/{recording_id}")
    def recording_progress(
        recording_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        progress = chunk_store.progress(recording_id, request_client_id(request))
        if progress is None:
            raise HTTPException(status_code=404, detail="recording not found")
        return JSONResponse(progress)

    @app.post("/recordings/{recording_id}/retry")
    def retry_recording(
        recording_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        if not chunk_store.retry_session(recording_id, request_client_id(request)):
            raise HTTPException(status_code=409, detail="recording is not retryable")
        return JSONResponse({"status": "queued", "recording_id": recording_id})

    @app.post("/transcript")
    def upload_transcript(
        payload: TranscriptUpload,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)

        transcript = payload.transcript.strip()
        if not transcript:
            raise HTTPException(status_code=400, detail="transcript must not be empty")

        filename = Path(payload.filename).name or "recording.m4a"
        source = payload.source.strip() or "iphone-on-device"
        client_id = request_client_id(request)
        digest = sha256(
            f"{client_id}\0{source}\0{filename}\0{transcript}".encode("utf-8")
        ).hexdigest()
        content_hash = f"transcript:{digest}"
        existing = store.get_by_hash(content_hash)
        if existing is not None:
            return JSONResponse(
                {
                    "status": "duplicate",
                    "job_id": existing.id,
                    "stored_filename": existing.stored_filename,
                },
                status_code=200,
            )

        try:
            recipient = normalize_recipient(payload.recipient)
            job = store.create_job(
                source=source,
                original_filename=filename,
                stored_filename=f"{digest}.txt",
                mime_type="text/plain",
                file_size=len(transcript.encode("utf-8")),
                content_hash=content_hash,
                client_id=client_id,
                recipient=recipient,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        transcript_path = paths.transcripts / f"{job.id}.txt"
        transcript_path.write_text(transcript, encoding="utf-8")
        store.mark_transcribed(job.id, transcript_path)
        memo_store.upsert_from_job(
            job,
            transcript_path,
            title=fallback_title(filename),
        )

        return JSONResponse(
            {
                "status": "email_queued",
                "job_id": job.id,
                "stored_filename": job.stored_filename,
            },
            status_code=201,
        )

    @app.get("/memos")
    def list_memos(
        request: Request,
        q: str = "",
        limit: int = 100,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        memos = memo_store.list(q, limit, request_client_id(request))
        return JSONResponse({"memos": [memo.to_dict() for memo in memos]})

    @app.get("/memos/{memo_id}")
    def get_memo(
        memo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        memo = memo_store.get(memo_id, request_client_id(request))
        if memo is None:
            raise HTTPException(status_code=404, detail="memo not found")
        result = memo.to_dict()
        transcript_path = Path(memo.transcript_path)
        result["transcript"] = transcript_path.read_text(encoding="utf-8") if transcript_path.exists() else ""
        return JSONResponse(result)

    @app.post("/memos/{memo_id}/retry")
    def retry_memo(
        memo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        if not memo_store.retry(memo_id, request_client_id(request)):
            raise HTTPException(status_code=409, detail="memo is not retryable")
        return JSONResponse({"status": "queued", "memo_id": memo_id})

    @app.delete("/memos/{memo_id}")
    def delete_memo(
        memo_id: str,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        job = None
        existing_memo = memo_store.get(memo_id, request_client_id(request))
        if existing_memo is not None:
            job = store.get_job(existing_memo.job_id)
        memo = memo_store.delete(memo_id, request_client_id(request))
        if memo is None:
            raise HTTPException(status_code=404, detail="memo not found")
        Path(memo.transcript_path).unlink(missing_ok=True)
        if job is not None:
            (paths.incoming / job.stored_filename).unlink(missing_ok=True)
            (paths.failed / job.stored_filename).unlink(missing_ok=True)
        return JSONResponse({"status": "deleted", "memo_id": memo_id})

    @app.get("/preferences")
    def get_preferences(
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        return JSONResponse(memo_store.get_preferences(request_client_id(request)))

    @app.put("/preferences")
    def update_preferences(
        payload: dict,
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    ) -> JSONResponse:
        require_basic_auth(settings, credentials)
        client_id = request_client_id(request)
        allowed = set(memo_store.get_preferences(client_id))
        updates = {key: value for key, value in payload.items() if key in allowed}
        return JSONResponse(memo_store.update_preferences(updates, client_id))

    return app
