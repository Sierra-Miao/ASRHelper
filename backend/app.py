import hashlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import AISettings, PROMPT, ROOT, SettingsStore
from .llm import CorrectionError, test_connection
from .service import TranscriptionService

MAX_UPLOAD = 512 * 1024 * 1024
ALLOWED = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".wma", ".aiff", ".aif", ".mp4", ".webm", ".mkv", ".mov"}


def create_app(service=None, settings_store=None):
    service = service or TranscriptionService()
    store = settings_store or SettingsStore()

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.close()

    app = FastAPI(title="听笺 · 本地语音工作台", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.service = service
    app.state.settings_store = store
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
            if origin and origin.rstrip("/") != expected:
                return JSONResponse({"detail": "仅允许本地页面发起操作"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > MAX_UPLOAD + 1024 * 1024:
                    return JSONResponse({"detail": "文件超过 512 MB 上限"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效请求长度"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.get("/api/health")
    def health():
        return {"app_id": "funasr-local-studio", "instance": hashlib.sha256(str(ROOT).encode()).hexdigest()[:12],
                "status": "ok", "model_state": service.model_state, "device": service.device,
                "hardware": service.hardware, "max_upload_mb": 512}

    @app.get("/api/settings")
    def settings():
        return {**store.public(), "default_prompt": PROMPT}

    class SettingsUpdate(AISettings):
        keep_key: bool = True

    @app.put("/api/settings")
    def save_settings(body: SettingsUpdate):
        try:
            return store.update(AISettings(**body.model_dump(exclude={"keep_key"})), body.keep_key)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/settings/test")
    async def test_settings():
        settings = store.snapshot()
        if not settings.ready:
            raise HTTPException(400, "请先保存 API 地址和模型名称")
        try:
            await run_in_threadpool(test_connection, settings)
            return {"ok": True, "message": "连接成功。附件模式还需要服务商支持 Files 和 Excel 输入。" if settings.transport == "file" else "连接成功"}
        except CorrectionError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.get("/api/jobs")
    def history():
        return service.history()

    @app.post("/api/jobs", status_code=202)
    async def upload(file: UploadFile = File(...), speakers: int | None = Form(None), use_ai: bool = Form(False)):
        if speakers is not None and not 1 <= speakers <= 30:
            raise HTTPException(400, "说话人数须在 1–30 之间")
        filename = file.filename or "audio.wav"
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED:
            raise HTTPException(400, "不支持此文件格式，请上传 WAV、MP3、M4A 等音频或常见视频文件")
        settings = store.snapshot()
        if use_ai and (not settings.enabled or not settings.ready):
            raise HTTPException(400, "请先在 AI 设置中启用并保存接口配置")
        job_id = service.register(filename, speakers, use_ai)
        path = service.directory(job_id) / ("upload" + extension)
        size = 0
        try:
            # Bound memory use; UploadFile is spooled by Starlette.
            with path.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise HTTPException(413, "文件超过 512 MB 上限")
                    await run_in_threadpool(handle.write, chunk)
            if size == 0:
                raise HTTPException(400, "上传的文件为空")
        except BaseException:
            path.unlink(missing_ok=True)
            service.abandon_upload(job_id)
            raise
        finally:
            await file.close()
        service.submit(job_id, path, settings)
        return service.public(job_id)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        try:
            return service.public(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.post("/api/jobs/{job_id}/correct", status_code=202)
    def correct(job_id: str):
        settings = store.snapshot()
        if not settings.ready or not settings.enabled:
            raise HTTPException(400, "请先启用并保存 AI 配置")
        try:
            service.retry_correction(job_id, settings)
            return service.public(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/jobs/{job_id}/download/{variant}")
    def download(job_id: str, variant: str):
        files = {"original": ("original.xlsx", "原始识别.xlsx"), "corrected": ("corrected.xlsx", "AI校对.xlsx"), "bundle": ("results.zip", "识别与校对.zip")}
        try:
            job = service.get(job_id)
            if variant not in files:
                raise KeyError(variant)
            name, label = files[variant]
            path = service.directory(job_id) / name
            if not path.is_file():
                raise KeyError(variant)
            stem = Path(job["filename"]).stem
            media = "application/zip" if variant == "bundle" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            return FileResponse(path, filename=f"{stem}_{label}", media_type=media)
        except KeyError as exc:
            raise HTTPException(404, "结果文件尚未生成") from exc

    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app


app = create_app()
