"""Shared model selection and offline readiness checks (standard library only)."""
import json
import os
from pathlib import Path
from speaker_config import selected_speaker, speaker_path

ROOT = Path(__file__).resolve().parent
PROFILES = {
    "nano": {"label": "Fun-ASR-Nano 2512 · 精度优先", "model": "FunAudioLLM/Fun-ASR-Nano-2512",
             "vad_model": "fsmn-vad", "spk_model": "cam++"},
    "paraformer": {"label": "SeACo Paraformer-large · 速度优先", "model": "paraformer-zh",
                   "vad_model": "fsmn-vad", "punc_model": "ct-punc", "spk_model": "cam++"},
    "sensevoice": {"label": "SenseVoice Small · 多语言均衡", "model": "iic/SenseVoiceSmall",
                   "vad_model": "fsmn-vad", "punc_model": "ct-punc", "spk_model": "cam++"},
    "ensemble": {"label": "双模型 · 传统模型主稿 + LLM 语音参考"},
}
CATALOG = {
    "ensemble": {"name": "双模型交叉核对", "parameters": "传统侧 SenseVoice / Paraformer；LLM 侧 Fun-ASR-Nano / 在线 Qwen3-ASR",
                 "download": "两种模型及公共组件，已下载文件复用",
                 "memory": "串行运行；Nano 建议留出 6 GB 可用内存；Qwen 在线识别不加载本地 LLM 权重",
                 "scenarios": "口语会议、疑难词或局部乱码；传统侧提供时间与说话人，AI 对照 LLM 转写处理疑难内容",
                 "languages": "随所选组合变化；Qwen 支持多语言自动识别",
                 "numbers": "两个模型分别开启数字规范化；有冲突时由 AI 结合上下文判断并保留存疑项",
                 "timing": "以传统模型为主；参考时间仅匹配音频范围，不替换主稿说话人与时间",
                 "requirements": "传统模型 + 本地 Nano 或 DashScope SDK / Qwen API Key；在线模式上传音频并按服务商规则计费；参考失败保留主稿",
                 "source": "https://github.com/modelscope/FunASR"},
    "nano": {"name": "Fun-ASR-Nano 2512", "parameters": "约 8 亿（800M）", "download": "主模型约 3–4 GB，另需 VAD / 所选说话人模型",
             "memory": "建议至少 12 GB 系统内存、加载前可用内存 ≥ 6 GB；CUDA 建议 6 GB 以上显存；CPU 可运行但较慢",
             "scenarios": "中文会议、口音和方言、嘈杂或较远距离录音、专业术语；优先追求识别质量",
             "languages": "中文、英语、日语；支持多种中文方言和口音",
             "numbers": "原生 ITN 数字规范化 + 本机数量格式整理",
             "timing": "ERes2NetV2：先检测换人后转写，时间为音频轮次边界；CAM++：VAD 片段内分配说话人。均不承诺逐字时间戳",
             "requirements": "Python 3.12 / FunASR 1.4.16 / PyTorch 2.8；复用项目依赖；CPU 使用 FP32",
             "source": "https://github.com/modelscope/FunASR/tree/main/examples/industrial_data_pretraining/fun_asr_nano"},
    "sensevoice": {"name": "SenseVoice Small", "parameters": "约 2.34 亿（234M）", "download": "主模型约 0.9–1 GB，另需 VAD / 所选说话人模型",
                   "memory": "建议至少 8 GB 系统内存、加载前可用 ≥ 1.5 GB；CUDA 建议 4 GB 以上显存；适合 CPU",
                   "scenarios": "中文/粤语及多语言录音、日常会议；兼顾 CPU 速度和识别质量",
                   "languages": "普通话、粤语、英语、日语、韩语",
                   "numbers": "原生 use_itn 数字与标点规范化 + 本机数量格式整理",
                   "timing": "ERes2NetV2：先检测换人后转写，保留短轮次；CAM++：按原句段分配说话人。换人边界不等于逐字对齐",
                   "requirements": "复用项目 Python / FunASR / PyTorch，无额外远程代码或安装步骤",
                   "source": "https://github.com/FunAudioLLM/SenseVoice"},
    "paraformer": {"name": "SeACo Paraformer-large", "parameters": "约 2.2 亿级（ASR 主体，组件另计）", "download": "完整旧版四模型流水线约 2.2 GB",
                   "memory": "建议至少 8 GB 系统内存、加载前可用 ≥ 1.5 GB；CUDA 建议 4 GB 以上显存；CPU 友好",
                   "scenarios": "普通话、清晰录音、重视速度与较细句段时间的会议；原有模型",
                   "languages": "普通话为主，可识别部分中英混合内容",
                   "numbers": "本机数量格式整理；不保证复杂数字全部规范化",
                   "timing": "ERes2NetV2：先检测换人后转写，保留短轮次；CAM++：字符对齐后按标点分句并分配说话人",
                   "requirements": "ASR + FSMN-VAD + CT-Punc + 所选说话人模型；复用现有已下载模型",
                   "source": "https://github.com/modelscope/FunASR/blob/main/model_zoo/readme.md"},
}


def selected_ensemble(data_dir=None):
    data_dir = Path(data_dir or os.environ.get("FUNASR_DATA_DIR", ROOT / "data"))
    try:
        saved = json.loads((data_dir / "asr_settings.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    return {"primary": saved.get("primary") if saved.get("primary") in {"sensevoice", "paraformer"} else "sensevoice",
            "reference": saved.get("reference") if saved.get("reference") in {"nano", "qwen"} else "nano"}


def selected_profile(data_dir=None):
    saved = "nano"
    data_dir = Path(data_dir or os.environ.get("FUNASR_DATA_DIR", ROOT / "data"))
    try:
        saved = json.loads((data_dir / "asr_settings.json").read_text(encoding="utf-8"))["profile"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    value = os.environ.get("FUNASR_ASR_MODEL", str(saved)).strip().lower()
    if value not in PROFILES:
        raise ValueError("模型选项必须为 nano、sensevoice、paraformer 或 ensemble")
    return value


def manifest_path(profile=None, root=ROOT):
    profile = profile or selected_profile()
    return root / "models" / ("local_paths.json" if profile == "paraformer" else f"local_paths.{profile}.json")


def local_models(profile=None, root=ROOT, speaker=None):
    profile = profile or selected_profile()
    try:
        paths = json.loads(manifest_path(profile, root).read_text(encoding="utf-8-sig"))
        if not isinstance(paths, dict):
            paths = {}
    except (OSError, ValueError):
        paths = {}
    # Reuse companion models from the original installation without redownloading.
    if profile != "paraformer":
        try:
            old = json.loads(manifest_path("paraformer", root).read_text(encoding="utf-8-sig"))
            if isinstance(old, dict):
                for key in ("vad_model", "punc_model", "spk_model"):
                    paths.setdefault(key, old.get(key))
        except (OSError, ValueError):
            pass
    found = {}
    for key in PROFILES[profile]:
        if key == "spk_model":
            path = speaker_path(speaker or selected_speaker(), root)
            if path:
                found[key] = str(path.resolve())
            continue
        if key == "label" or not isinstance(paths.get(key), str):
            continue
        path = root / paths[key]
        weight = "campplus_cn_common.bin" if key == "spk_model" else "model.pt"
        if not (path / "config.yaml").is_file() or not (path / weight).is_file():
            continue
        if profile == "nano" and key == "model":
            decoder = path / "Qwen3-0.6B"
            if not (decoder / "config.json").is_file() or not (decoder / "tokenizer.json").is_file():
                continue
        found[key] = str(path.resolve())
    return found


def models_ready(profile=None, root=ROOT, primary=None, reference=None, speaker=None):
    profile = profile or selected_profile()
    if profile == "ensemble":
        choice = selected_ensemble()
        return models_ready(primary or choice["primary"], root, speaker=speaker) and ((reference or choice["reference"]) == "qwen" or models_ready("nano", root, speaker=speaker))
    required = set(PROFILES[profile]) - {"label"}
    return required <= local_models(profile, root, speaker=speaker).keys()


def recognition_options(profile=None, speakers=None):
    profile = profile or selected_profile()
    options = {"batch_size_s": 30, "disable_pbar": True, "sentence_timestamp": True}
    if profile == "nano":
        options.update(batch_size=1, itn=True, language="中文", max_length=1024,
                       llm_dtype="fp32", llm_kwargs={"do_sample": False, "num_beams": 1})
    elif profile == "sensevoice":
        options.update(use_itn=True, language="auto", ban_emo_unk=True)
    if speakers is not None:
        options["preset_spk_num"] = speakers
    return options
