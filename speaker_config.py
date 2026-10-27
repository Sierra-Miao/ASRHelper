"""Speaker backend selection and readiness; no ML imports during startup checks."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEAKERS = {
    "eres2netv2": {"name": "ERes2NetV2 · 先分人后转写", "weight": "model.pt",
        "description": "中文约 20 万说话人训练；1.5 秒声纹窗口、0.5 秒步长，先检测换人再分别转写，不再强制归并短轮次。指定人数采用归一化 K-Means 全程分组，替换易受离群点影响的层次合并；不能保证声纹标签正确。CPU 可用，比 CAM++ 慢；重叠讲话、回声和短应答仍可能识别错误。",
        "source": "https://modelscope.cn/models/iic/speech_eres2netv2_sv_zh-cn_16k-common"},
    "cam++": {"name": "CAM++ · 原版速度优先", "weight": "campplus_cn_common.bin",
        "description": "保留原有轻量声纹模型与聚类，供速度优先及效果对照使用。",
        "source": "https://modelscope.cn/models/iic/speech_campplus_sv_zh-cn_16k-common"},
}


def selected_speaker(data_dir=None):
    value = "eres2netv2"
    try:
        saved = json.loads((Path(data_dir or os.environ.get("FUNASR_DATA_DIR", ROOT / "data")) / "speaker_settings.json").read_text(encoding="utf-8-sig"))
        value = saved["model"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    if not isinstance(value, str) or value not in SPEAKERS:
        raise ValueError("说话人模型必须为 eres2netv2 或 cam++")
    return value


def speaker_path(model, root=ROOT):
    if model == "eres2netv2":
        path = root / "models" / "speakers" / "eres2netv2"
        return path if (path / "config.yaml").is_file() and (path / "model.pt").is_file() else None
    if model != "cam++":
        raise ValueError("不支持此说话人模型")
    candidates = []
    for manifest in (root / "models").glob("local_paths*.json"):
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if isinstance(saved.get("spk_model"), str):
                candidates.append(root / saved["spk_model"])
        except (OSError, ValueError, AttributeError):
            continue
    # Reuse a cached CAM++ after profile manifests have switched to ERes2NetV2.
    candidates.extend((root / "models" / "modelscope").glob("models/iic--speech_campplus_sv_zh-cn_16k-common/snapshots/*"))
    return next((path for path in candidates if (path / "config.yaml").is_file() and (path / SPEAKERS[model]["weight"]).is_file()), None)
