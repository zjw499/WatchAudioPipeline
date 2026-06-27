from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from watch_audio_pipeline.config import Settings
from watch_audio_pipeline.paths import AppPaths
from watch_audio_pipeline.store import JobStore
from watch_audio_pipeline.uploads import queue_upload


def create_app(settings: Settings, paths: AppPaths, store: JobStore) -> FastAPI:
    app = FastAPI(title="Watch Audio Pipeline")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/upload")
    async def upload_audio(
        file: UploadFile = File(...),
        source: str = Form("iphone-shortcuts"),
        x_upload_token: str | None = Header(default=None),
    ) -> JSONResponse:
        if x_upload_token != settings.upload_token:
            raise HTTPException(status_code=401, detail="invalid upload token")

        try:
            result = await queue_upload(
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
