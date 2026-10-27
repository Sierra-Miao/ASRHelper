"""Sequential dual-ASR execution with a durable primary-result callback."""
import gc
import json
import logging
import os
import sys
from pathlib import Path

from asr_config import ROOT, PROFILES, local_models, manifest_path, models_ready, recognition_options


def release_model():
    gc.collect()
    torch = sys.modules.get("torch")
    if torch is not None and torch.cuda.is_available():
        torch.cuda.empty_cache()


def prepare_profile(profile, speaker=None):
    """Fetch weights/configs only; do not hold two initialized models in memory."""
    if models_ready(profile, speaker=speaker):
        return
    # Import transcribe first to establish the project's cache directories.
    import transcribe
    from funasr.download.download_model_from_hub import download_model
    names = {k: v for k, v in PROFILES[profile].items() if k != "label"}
    names.update(local_models(profile, speaker=speaker))
    paths = {}
    for key, name in names.items():
        if key == "spk_model":
            from .speakers import prepare_speaker
            from speaker_config import selected_speaker
            path = prepare_speaker(speaker or selected_speaker()).resolve()
            paths[key] = os.path.relpath(path, ROOT)
            continue
        config = download_model(model=name, hub="ms", trust_remote_code=False)
        path = Path(config["model_path"]).resolve()
        weight = "campplus_cn_common.bin" if key == "spk_model" else "model.pt"
        if not (path / weight).is_file():
            raise ValueError(f"{profile} 模型下载不完整，请检查网络后重试")
        paths[key] = os.path.relpath(path, ROOT)
    manifest = manifest_path(profile)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(paths, indent=2), encoding="utf-8")
    temporary.replace(manifest)
    if not models_ready(profile, speaker=speaker):
        raise ValueError(f"{profile} 模型配置或分词文件不完整，请重试下载")


class SequentialEnsemble:
    def __init__(self, prepare=False, loader=None, primary="sensevoice", reference="nano", qwen_settings=None, speaker=None):
        if primary not in {"sensevoice", "paraformer"} or reference not in {"nano", "qwen"}:
            raise ValueError("双模型须选择一个传统模型和一个 LLM 语音模型")
        self.primary, self.reference = primary, reference
        self.primary_label = "SenseVoice" if primary == "sensevoice" else "Paraformer"
        self.reference_label = "Fun-ASR-Nano" if reference == "nano" else "Qwen3-ASR"
        self.qwen_settings = qwen_settings
        self.kwargs = {"device": "cpu"}
        self.reference_result = []
        self.warnings = []
        self.loader = loader
        self.speaker = speaker
        if prepare:
            prepare_profile(primary, speaker)
            if reference == "nano":
                prepare_profile("nano", speaker)

    def generate(self, input, primary_callback=None, progress=None, **kwargs):
        from transcribe import load_pipeline
        loader = self.loader or (lambda profile: load_pipeline(profile, speaker=self.speaker))
        progress = progress or (lambda _: None)
        speakers = kwargs.get("preset_spk_num")
        self.reference_result, self.warnings = [], []
        model = None
        try:
            progress(f"双模型 1/2：{self.primary_label} 主稿、时间与说话人识别")
            model = loader(self.primary)
            self.kwargs = {"device": str(model.kwargs.get("device", "cpu"))}
            primary = model.generate(input=input, **recognition_options(self.primary, speakers))
        finally:
            model = None
            release_model()
        if primary_callback:
            primary_callback(primary)
        try:
            progress(f"双模型 2/2：{self.reference_label} 参考转写（主稿已保存）")
            if self.reference == "qwen":
                from .qwen import QwenASR, QwenStore
                model = QwenASR(self.qwen_settings or QwenStore().snapshot())
                self.reference_result = model.generate(input=input, progress=progress)
            else:
                model = loader("nano")
                self.reference_result = model.generate(input=input, **recognition_options("nano", speakers))
        except Exception as exc:
            logging.exception("Secondary ASR pass failed; primary transcript retained")
            reason = "内存不足" if isinstance(exc, MemoryError) or "out of memory" in str(exc).lower() else str(exc) if self.reference == "qwen" and isinstance(exc, ValueError) else "下载或识别失败"
            self.warnings.append(f"{self.reference_label} 参考识别未完成（{reason}），本次仅保留 {self.primary_label} 主稿；不能视为已完成双模型核对。")
        finally:
            model = None
            release_model()
        return primary
