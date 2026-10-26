import copy
import json
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .config import DATA
from .excel import normalize_segments, write_excel
from .llm import CorrectionError, correct_workbook
from hardware import hardware_plan

ACTIVE = {"queued", "loading", "decoding", "transcribing", "exporting", "correcting"}


def ffmpeg_executable():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def safe_name(filename):
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:160] or "音频文件.wav"


class TranscriptionService:
    def __init__(self, data_dir=DATA):
        self.data_dir = data_dir
        self.jobs_dir = data_dir / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="funasr")
        self.model = None
        self.model_state = "not_loaded"
        self.hardware = hardware_plan()
        self.device = f"CPU · {self.hardware['cpu_threads']} 线程"
        self.jobs = {}
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
                if job["status"] in ACTIVE:
                    has_original = (path.parent / "original.xlsx").is_file()
                    job.update(status="completed" if has_original else "failed",
                               stage="上次运行已中断，原始结果已保留" if has_original else "上次运行已中断，请重新上传",
                               ai_error="校对被中断，可重新校对" if has_original and job.get("use_ai") else "")
                self.jobs[job["id"]] = job
            except (OSError, ValueError, KeyError):
                continue

    def close(self):
        self.executor.shutdown(wait=False, cancel_futures=True)

    def directory(self, job_id):
        if not re.fullmatch(r"[a-f0-9]{32}", job_id):
            raise KeyError(job_id)
        return self.jobs_dir / job_id

    def get(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])

    def update(self, job_id, **changes):
        with self.lock:
            job = self.jobs[job_id]
            job.update(changes)
            temporary = self.directory(job_id) / "job.json.tmp"
            temporary.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.directory(job_id) / "job.json")

    def public(self, job_id, include_rows=True):
        job = self.get(job_id)
        directory = self.directory(job_id)
        job["downloads"] = {}
        job["device"] = self.device
        for variant, name in (("original", "original.xlsx"), ("corrected", "corrected.xlsx"), ("bundle", "results.zip")):
            if (directory / name).is_file():
                job["downloads"][variant] = f"/api/jobs/{job_id}/download/{variant}"
        if not include_rows:
            job.pop("rows", None)
            job.pop("corrected_rows", None)
        return job

    def history(self):
        with self.lock:
            ids = sorted(self.jobs, key=lambda key: self.jobs[key]["created_at"], reverse=True)[:30]
        return [self.public(job_id, include_rows=False) for job_id in ids]

    def register(self, filename, speakers, use_ai):
        job_id = uuid.uuid4().hex
        self.directory(job_id).mkdir(parents=True)
        with self.lock:
            self.jobs[job_id] = {"id": job_id, "filename": safe_name(filename), "speakers": speakers,
                "use_ai": use_ai, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "queued", "stage": "等待处理", "rows": [], "corrected_rows": [],
                "duration_ms": 0, "error": "", "ai_error": "", "warnings": []}
        self.update(job_id)
        return job_id

    def abandon_upload(self, job_id):
        self.update(job_id, status="failed", stage="上传失败", error="文件上传未完成，请重试")

    def submit(self, job_id, audio_path, settings):
        self.executor.submit(self.process, job_id, audio_path, settings)

    def process(self, job_id, audio_path, settings):
        directory = self.directory(job_id)
        decoded = directory / "decoded.wav"
        try:
            self.update(job_id, status="decoding", stage="正在读取音频")
            completed = subprocess.run([ffmpeg_executable(), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000", str(decoded)],
                capture_output=True, timeout=3600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if completed.returncode:
                raise ValueError("音频解码失败，请确认文件没有损坏且包含音轨")
            import soundfile
            info = soundfile.info(decoded)
            self.update(job_id, duration_ms=round(info.duration * 1000))
            if self.model is None:
                self.model_state = "loading"
                self.update(job_id, status="loading", stage="首次加载四个模型，通常需要约 1–2 分钟")
                from transcribe import load_pipeline
                self.model = load_pipeline()
                self.model_state = "ready"
                device = str(self.model.kwargs.get("device", "cpu"))
                self.device = (f"CPU · {self.hardware['cpu_threads']} 线程" if device == "cpu" else device.upper())
            self.update(job_id, status="transcribing", stage="正在识别语音与说话人")
            options = {"batch_size_s": 60, "disable_pbar": True}
            speakers = self.get(job_id)["speakers"]
            if speakers is not None:
                options["preset_spk_num"] = speakers
            result = self.model.generate(input=str(decoded), **options)
            rows = normalize_segments(result)
            self.update(job_id, status="exporting", stage="正在生成原始 Excel", rows=rows)
            original_pending = directory / "original.pending.xlsx"
            write_excel(rows, original_pending, "原始识别")
            original_pending.replace(directory / "original.xlsx")
            (directory / "asr.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            if self.get(job_id)["use_ai"]:
                self.correct(job_id, settings)
            else:
                self.update(job_id, status="completed", stage="识别完成，原始 Excel 已就绪")
        except Exception as exc:
            if self.model is None:
                self.model_state = "not_loaded"
            message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "识别未完成，请检查模型文件或音频格式后重试"
            if isinstance(exc, subprocess.TimeoutExpired):
                message = "音频解码超时，请尝试缩短音频"
            if (directory / "original.xlsx").is_file():
                self.update(job_id, status="completed", stage="原始识别已完成", ai_error=message)
            else:
                self.update(job_id, status="failed", stage="识别失败", error=message)
        finally:
            audio_path.unlink(missing_ok=True)
            decoded.unlink(missing_ok=True)
            settings.api_key = ""

    def correct(self, job_id, settings):
        directory = self.directory(job_id)
        try:
            self.update(job_id, status="correcting", stage="AI 正在校对表格", ai_error="", use_ai=True)
            job = self.get(job_id)
            rows, warnings = correct_workbook(settings, directory / "original.xlsx", job["rows"],
                lambda stage: self.update(job_id, stage=stage))
            # Replace only after the entire correction has passed validation.
            temporary = directory / "corrected.pending.xlsx"
            write_excel(rows, temporary, "AI 校对")
            temporary.replace(directory / "corrected.xlsx")
            with zipfile.ZipFile(directory / "results.pending.zip", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.write(directory / "original.xlsx", "原始识别.xlsx")
                archive.write(directory / "corrected.xlsx", "AI校对.xlsx")
            (directory / "results.pending.zip").replace(directory / "results.zip")
            self.update(job_id, corrected_rows=rows, warnings=warnings,
                        status="completed", stage="识别与 AI 校对完成")
        except Exception as exc:
            message = str(exc) if isinstance(exc, CorrectionError) else "AI 校对未完成，请检查接口配置后重试"
            self.update(job_id, status="completed", stage="原始 Excel 已就绪，AI 校对未完成", ai_error=message)
        finally:
            settings.api_key = ""

    def retry_correction(self, job_id, settings):
        with self.lock:
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                raise ValueError("任务仍在处理中")
            if not (self.directory(job_id) / "original.xlsx").is_file():
                raise ValueError("尚无可校对的原始 Excel")
            self.update(job_id, status="queued", stage="等待 AI 校对", ai_error="", use_ai=True)
            self.executor.submit(self.correct, job_id, settings)
