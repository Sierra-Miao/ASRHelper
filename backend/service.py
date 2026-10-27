import copy
import json
import re
import shutil
import subprocess
import threading
import uuid
import zipfile
import gc
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .config import DATA
from .excel import normalize_segments, write_excel
from .llm import CorrectionError, correct_workbook
from hardware import hardware_plan, memory_status
from asr_config import CATALOG, PROFILES, selected_profile, selected_ensemble, models_ready, recognition_options
from .qwen import QwenStore
from .diagnostics import coverage
from .numbers import normalize_numbers
from .paragraphs import local_paragraphs
from .audio import ffmpeg_executable
from speaker_config import SPEAKERS, selected_speaker, speaker_path

ACTIVE = {"queued", "loading", "decoding", "transcribing", "exporting", "correcting"}


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
        self.profile = selected_profile(data_dir)
        self.ensemble = selected_ensemble(data_dir)
        self.speaker = selected_speaker(data_dir)
        self.qwen_store = QwenStore(data_dir / "qwen_settings.json")
        self.model_action = {"status": "idle", "profile": self.profile, "message": "", "error": ""}
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

    def model_catalog(self):
        with self.lock:
            busy = self.model_action["status"] == "loading" or any(j["status"] in ACTIVE for j in self.jobs.values())
            return {"selected": self.profile, "loaded": self.model is not None,
                    "busy": busy, "action": copy.deepcopy(self.model_action),
                    "memory": memory_status(), "ensemble": dict(self.ensemble), "qwen": self.qwen_store.public(),
                    "speaker": self.speaker, "speakers": [{"id": key, **spec, "ready": bool(speaker_path(key))} for key, spec in SPEAKERS.items()],
                    "models": [{"id": key, **item, "ready": self.profile_ready(key),
                                "selected": key == self.profile} for key, item in CATALOG.items()],
                    "note": "内存和下载量为本项目估算；说话人模型独立选择。ERes2NetV2 先检测换人再转写，短轮次不再强制归并；时间是音频窗口边界，无法保证逐字准确或消除重叠讲话。指定人数必须是实际出声人数。Qwen 是在线参考，使用时上传音频并可能产生费用。"}

    def profile_ready(self, profile, choice=None):
        if profile != "ensemble":
            return models_ready(profile, speaker=self.speaker)
        choice = choice or self.ensemble
        return models_ready("ensemble", **choice, speaker=self.speaker) and (choice["reference"] != "qwen" or self.qwen_store.public()["ready"])

    def load_selected(self, profile, choice=None):
        from transcribe import load_pipeline
        if profile == "ensemble":
            return load_pipeline(profile, **(choice or self.ensemble), qwen_settings=self.qwen_store.snapshot(), speaker=self.speaker)
        return load_pipeline(profile, speaker=self.speaker)

    def activate_speaker(self, model, allow_download=False):
        if model not in SPEAKERS:
            raise ValueError("不支持此说话人模型")
        with self.lock:
            if self.model_catalog()["busy"]:
                raise ValueError("正在处理任务，请完成后再切换说话人模型")
            if not speaker_path(model) and not allow_download:
                raise ValueError("说话人模型尚未下载，请点击“下载并启用”")
            self.model_action = {"status": "loading", "profile": self.profile, "message": "正在准备说话人模型", "error": ""}
            self.executor.submit(self._activate_speaker, model)
        return self.model_catalog()

    def _activate_speaker(self, model):
        try:
            self.model = None
            self.model_state = "not_loaded"
            from .ensemble import release_model
            from .speakers import prepare_speaker
            release_model()
            prepare_speaker(model)
            temporary = self.data_dir / "speaker_settings.json.tmp"
            temporary.write_text(json.dumps({"model": model}), encoding="utf-8")
            temporary.replace(self.data_dir / "speaker_settings.json")
            with self.lock:
                self.speaker = model
                self.model_action.update(status="completed", message="说话人模型已切换，下次转写使用新模型；历史结果不变", error="")
        except Exception as exc:
            logging.exception("Speaker backend preparation failed")
            self.model_action.update(status="failed", message="说话人模型未切换，原选择保留", error=recognition_error(exc, downloading=True))

    def activate_model(self, profile, allow_download=False, primary=None, reference=None):
        if profile not in PROFILES:
            raise ValueError("不支持此模型")
        with self.lock:
            choice = {"primary": primary or self.ensemble["primary"], "reference": reference or self.ensemble["reference"]}
            if choice["primary"] not in {"sensevoice", "paraformer"} or choice["reference"] not in {"nano", "qwen"}:
                raise ValueError("双模型须选择一个传统模型和一个 LLM 语音模型")
            if profile == "ensemble" and choice["reference"] == "qwen" and not self.qwen_store.public()["ready"]:
                raise ValueError("请先保存 Qwen3-ASR 的 DashScope API Key")
            if self.model_action["status"] == "loading" or any(j["status"] in ACTIVE for j in self.jobs.values()):
                raise ValueError("正在处理任务，请完成后再切换模型")
            if not self.profile_ready(profile, choice) and not allow_download:
                raise ValueError("模型尚未下载，请点击“下载并切换”确认下载")
            self.model_action = {"status": "loading", "profile": profile,
                                 "message": "正在下载缺失文件并加载模型；首次下载可能需要几分钟，请保持启动窗口打开", "error": ""}
            self.executor.submit(self._activate_model, profile, choice)
        return self.model_catalog()

    def _activate_model(self, profile, choice=None):
        try:
            # Unload first so model switching does not hold two large models in RAM.
            self.model = None
            self.model_state = "loading"
            gc.collect()
            import sys
            torch = sys.modules.get("torch")
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
            choice = choice or self.ensemble
            model = self.load_selected(profile, choice)
            temporary = self.data_dir / "asr_settings.json.tmp"
            temporary.write_text(json.dumps({"profile": profile, **choice}), encoding="utf-8")
            temporary.replace(self.data_dir / "asr_settings.json")
            with self.lock:
                self.model, self.profile = model, profile
                self.ensemble = dict(choice)
                self.model_state = "ready"
                device = str(model.kwargs.get("device", "cpu"))
                self.device = f"CPU · {self.hardware['cpu_threads']} 线程" if device == "cpu" else device.upper()
                self.model_action.update(status="completed", message="模型已切换，下次启动自动恢复", error="")
        except Exception as exc:
            logging.exception("Model download/load failed: %s", profile)
            self.model_state = "not_loaded"
            self.model_action.update(status="failed", message="模型未切换，原选择保留", error=recognition_error(exc, downloading=True))

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
        if job.get("rows") and "coverage" not in job:
            report, warnings = coverage(job["rows"], job.get("duration_ms", 0))
            job["coverage"] = report
            job["warnings"] = list(dict.fromkeys(job.get("warnings", []) + warnings))
        job["downloads"] = {}
        job["device"] = self.device
        for variant, name in (("original", "original.xlsx"), ("corrected", "corrected.xlsx"), ("bundle", "results.zip"),
                              ("paragraphs", "paragraphs.xlsx"), ("reference", "nano-reference.xlsx"), ("agent", "agent-review.zip")):
            if (directory / name).is_file():
                job["downloads"][variant] = f"/api/jobs/{job_id}/download/{variant}"
        if (directory / "llm-reference.xlsx").is_file():
            job["downloads"]["reference"] = f"/api/jobs/{job_id}/download/reference"
        if not include_rows:
            job.pop("rows", None)
            job.pop("corrected_rows", None)
            job.pop("paragraph_rows", None)
            job.pop("reference_rows", None)
        return job

    def history(self):
        with self.lock:
            ids = sorted(self.jobs, key=lambda key: self.jobs[key]["created_at"], reverse=True)[:30]
        return [self.public(job_id, include_rows=False) for job_id in ids]

    def register(self, filename, speakers, use_ai):
        job_id = uuid.uuid4().hex
        with self.lock:
            if self.model_action["status"] == "loading":
                raise ValueError("模型正在下载或切换，请稍后上传")
            if (self.model is None or self.profile == "ensemble") and not self.profile_ready(self.profile):
                raise ValueError("请先在“语音模型”中下载并启用一个模型")
            self.directory(job_id).mkdir(parents=True)
            self.jobs[job_id] = {"id": job_id, "filename": safe_name(filename), "speakers": speakers,
                "speaker_model": self.speaker, "speaker_model_name": SPEAKERS[self.speaker]["name"],
                "asr_profile": self.profile, "asr_model": PROFILES[self.profile]["label"],
                "use_ai": use_ai, "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "queued", "stage": "等待处理", "rows": [], "corrected_rows": [],
                "duration_ms": 0, "error": "", "ai_error": "", "warnings": []}
            if self.profile == "ensemble":
                self.jobs[job_id].update(ensemble=dict(self.ensemble),
                    reference_model="Fun-ASR-Nano" if self.ensemble["reference"] == "nano" else "Qwen3-ASR",
                    asr_model=f"双模型 · {PROFILES[self.ensemble['primary']]['label']} + {'Fun-ASR-Nano' if self.ensemble['reference'] == 'nano' else 'Qwen3-ASR 在线参考'}")
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
                self.update(job_id, status="loading", stage="正在加载所选语音模型，首次加载需要一些时间")
                self.model = self.load_selected(self.profile)
                self.model_state = "ready"
                device = str(self.model.kwargs.get("device", "cpu"))
                self.device = (f"CPU · {self.hardware['cpu_threads']} 线程" if device == "cpu" else device.upper())
            self.update(job_id, status="transcribing", stage="正在识别语音与说话人")
            speakers = self.get(job_id)["speakers"]
            options = recognition_options(self.profile, speakers)
            if self.profile == "ensemble":
                if self.ensemble["reference"] == "qwen":
                    self.model.qwen_settings = self.qwen_store.snapshot()
                result = self.model.generate(input=str(decoded), **options,
                    primary_callback=lambda primary: self.save_primary(job_id, primary),
                    progress=lambda stage: self.update(job_id, status="transcribing", stage=stage))
                references = []
                if self.model.reference_result:
                    try:
                        references = normalize_segments(self.model.reference_result)
                    except ValueError:
                        self.model.warnings.append("LLM 语音模型未返回有效参考句段，本次仅使用传统模型主稿。")
                if references:
                    name = "nano-reference.xlsx" if self.ensemble["reference"] == "nano" else "llm-reference.xlsx"
                    write_excel(references, directory / name, "LLM语音参考")
                (directory / "reference-asr.json").write_text(json.dumps(self.model.reference_result, ensure_ascii=False, indent=2), encoding="utf-8")
                job = self.get(job_id)
                self.update(job_id, reference_rows=references, reference_status="completed" if references else "failed",
                            warnings=list(dict.fromkeys(job.get("warnings", []) + self.model.warnings)))
            else:
                result = self.model.generate(input=str(decoded), **options)
                self.save_primary(job_id, result)
            if self.get(job_id)["use_ai"]:
                self.correct(job_id, settings)
            else:
                self.update(job_id, status="completed", stage="识别完成，原始 Excel 已就绪")
        except Exception as exc:
            logging.exception("ASR failed for job %s", job_id)
            if self.model is None:
                self.model_state = "not_loaded"
            message = recognition_error(exc)
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
            if self.profile == "ensemble" and getattr(self.model, "qwen_settings", None):
                self.model.qwen_settings.api_key = ""

    def save_primary(self, job_id, result):
        directory = self.directory(job_id)
        rows = normalize_segments(result)
        for row in rows:
            row["content"] = normalize_numbers(row["content"])
        paragraphs = local_paragraphs(rows)
        report, warnings = coverage(rows, self.get(job_id)["duration_ms"])
        for item in result:
            warnings.extend(item.get("warnings", []))
        original_pending = directory / "original.pending.xlsx"
        write_excel(rows, original_pending, "原始识别")
        original_pending.replace(directory / "original.xlsx")
        write_excel(paragraphs, directory / "paragraphs.xlsx", "本机发言合并")
        (directory / "asr.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.update(job_id, rows=rows, paragraph_rows=paragraphs, coverage=report, warnings=warnings)

    def save_correction(self, job_id, rows, warnings=()):
        directory = self.directory(job_id)
        temporary = directory / "corrected.pending.xlsx"
        write_excel(rows, temporary, "AI 段落整理")
        temporary.replace(directory / "corrected.xlsx")
        with zipfile.ZipFile(directory / "results.pending.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(directory / "original.xlsx", "原始识别.xlsx")
            archive.write(directory / "corrected.xlsx", "AI校对.xlsx")
            if (directory / "nano-reference.xlsx").is_file():
                archive.write(directory / "nano-reference.xlsx", "Nano参考.xlsx")
            if (directory / "llm-reference.xlsx").is_file():
                archive.write(directory / "llm-reference.xlsx", "Qwen参考.xlsx")
        (directory / "results.pending.zip").replace(directory / "results.zip")
        job = self.get(job_id)
        self.update(job_id, corrected_rows=rows, warnings=list(dict.fromkeys(job.get("warnings", []) + list(warnings))),
                    status="completed", agent_pending=False, stage="识别与 AI 段落整理完成")

    def prepare_agent(self, job_id, settings):
        from .handoff import build_handoff
        with self.lock:
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                raise ValueError("任务仍在处理中，请完成后导出 Agent 校对包")
            if not (self.directory(job_id) / "original.xlsx").is_file():
                raise ValueError("原始表格尚未就绪")
            return build_handoff(settings, self.directory(job_id), job)

    def correct(self, job_id, settings):
        directory = self.directory(job_id)
        try:
            self.update(job_id, status="correcting", stage="AI 正在校对表格", ai_error="", use_ai=True)
            job = self.get(job_id)
            if settings.transport == "agent":
                from .handoff import build_handoff
                build_handoff(settings, directory, job)
                self.update(job_id, status="completed", stage="Agent 交接包已就绪，请在客户端完成整理并直接下载 Excel", agent_pending=True)
                return
            extra = {"reference_rows": job["reference_rows"]} if job.get("reference_rows") else {}
            rows, warnings = correct_workbook(settings, directory / "original.xlsx", job["rows"],
                lambda stage: self.update(job_id, stage=stage), **extra)
            self.save_correction(job_id, rows, warnings)
        except Exception as exc:
            message = str(exc) if isinstance(exc, CorrectionError) else "Agent 交接包生成失败，请检查本机文件读写后重试" if settings.transport == "agent" else "AI 校对未完成，请检查接口配置后重试"
            self.update(job_id, status="completed", stage="原始 Excel 已就绪，AI 校对未完成", ai_error=message)
        finally:
            settings.api_key = ""

    def retry_correction(self, job_id, settings):
        with self.lock:
            if self.model_action["status"] == "loading":
                raise ValueError("模型正在切换，请稍后校对")
            job = self.get(job_id)
            if job["status"] in ACTIVE:
                raise ValueError("任务仍在处理中")
            if not (self.directory(job_id) / "original.xlsx").is_file():
                raise ValueError("尚无可校对的原始 Excel")
            self.update(job_id, status="queued", stage="等待 AI 校对", ai_error="", use_ai=True)
            self.executor.submit(self.correct, job_id, settings)


def recognition_error(exc, downloading=False):
    message = str(exc).lower()
    if isinstance(exc, MemoryError) or any(text in message for text in (
            "out of memory", "can't allocate memory", "cannot allocate memory", "not enough memory", "paging file", "页面文件")):
        detail = str(exc) if isinstance(exc, MemoryError) and "当前可用内存" in str(exc) else "内存或显存不足。"
        return detail + " 请关闭占内存程序，或在“语音模型”中切换至 SenseVoice / Paraformer 后重试。"
    if downloading:
        return "模型下载或加载失败，请检查网络、磁盘空间和启动窗口中的错误后重试。失败时不会保存新的模型选择。"
    if isinstance(exc, (ValueError, FileNotFoundError)):
        return str(exc)
    return "识别未完成，请查看启动窗口中的错误，检查所选模型和音频后重试"
