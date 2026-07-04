from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.uploads import queue_upload


def create_app(settings: Settings, paths: AppPaths, store: JobStore) -> FastAPI:
    if settings.upload_token in {"", "replace-me"}:
        raise ValueError("upload token must be configured")

    app = FastAPI(title="Watch Audio Pipeline")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/upload")
    def upload_audio(
        file: UploadFile = File(...),
        source: str = Form("iphone-shortcuts"),
        upload_token: str | None = Form(default=None),
        x_upload_token: str | None = Header(default=None),
    ) -> JSONResponse:
        if settings.upload_token not in {x_upload_token, upload_token}:
            raise HTTPException(status_code=401, detail="invalid upload token")

        try:
            result = queue_upload(
                file=file,
                source=source,
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

    return app
