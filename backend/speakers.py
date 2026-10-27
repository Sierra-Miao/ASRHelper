"""Project-owned ERes2NetV2 adapter and bounded meeting clustering for FunASR."""
import json
import os
import shutil
from pathlib import Path

from speaker_config import ROOT, SPEAKERS, speaker_path


def prepare_speaker(model="eres2netv2"):
    existing = speaker_path(model)
    if existing:
        return existing
    if model == "cam++":
        import transcribe
        from funasr.download.download_model_from_hub import download_model
        config = download_model(model="cam++", hub="ms", trust_remote_code=False)
        return Path(config["model_path"])
    if model != "eres2netv2":
        raise ValueError("不支持此说话人模型")
    from modelscope.hub.file_download import model_file_download
    checkpoint = model_file_download("iic/speech_eres2netv2_sv_zh-cn_16k-common",
        "pretrained_eres2netv2.ckpt", revision="v1.0.2", cache_dir=str(ROOT / "models" / "modelscope"))
    folder = ROOT / "models" / "speakers" / model
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "model.pending.pt"
    temporary.unlink(missing_ok=True)
    try:
        os.link(checkpoint, temporary)
    except OSError:
        shutil.copyfile(checkpoint, temporary)
    temporary.replace(folder / "model.pt")
    (folder / "source.json").write_text(json.dumps({"model": "iic/speech_eres2netv2_sv_zh-cn_16k-common", "revision": "v1.0.2"}), encoding="utf-8")
    # The implementation is audited in this repository, never executed remotely.
    config = folder / "config.pending.yaml"
    config.write_text("model: ProjectERes2NetV2\nmodel_conf:\n  feat_dim: 80\n  embedding_size: 192\n", encoding="utf-8")
    config.replace(folder / "config.yaml")
    return folder


def register_eres2net():
    import torch
    import torchaudio.compliance.kaldi as kaldi
    from funasr.register import tables
    from funasr.utils.load_utils import load_audio_text_image_video
    from .vendor.eres2net.model import ERes2NetV2

    if "ProjectERes2NetV2" in tables.model_classes:
        return

    @tables.register("model_classes", "ProjectERes2NetV2")
    class ERes2NetAdapter(ERes2NetV2):
        def __init__(self, feat_dim=80, embedding_size=192, **kwargs):
            super().__init__(feat_dim=feat_dim, embedding_size=embedding_size, baseWidth=26, scale=2, expansion=2)

        def inference(self, data_in, key=None, **kwargs):
            samples = load_audio_text_image_video(data_in, fs=16000, audio_fs=kwargs.get("fs", 16000), data_type="sound")
            embeddings, duration = [], 0
            # Separate windows avoid padded frames changing shorter embeddings.
            for sample in samples:
                waveform = torch.as_tensor(sample, dtype=torch.float32).flatten().cpu()
                duration += waveform.numel() / 16000
                if waveform.numel() < 640:
                    waveform = torch.nn.functional.pad(waveform, (0, 640 - waveform.numel()))
                feats = kaldi.fbank(waveform.unsqueeze(0), num_mel_bins=80, sample_frequency=16000, dither=0)
                feats = feats - feats.mean(0, keepdim=True)
                with torch.inference_mode():
                    embedding = self(feats.unsqueeze(0).to(kwargs["device"]))
                embeddings.append({"spk_embedding": embedding})
            return embeddings, {"batch_data_time": max(duration, .001)}


class MeetingCluster:
    """Normalized K-Means for known K, without irreversible hierarchical merges.

    Average linkage cut at a small K can spend most clusters on outliers and
    merge the actual speakers into one large group. Multiple K-Means restarts
    optimize the whole recording; no equal-size or equal-duration rule is used.
    Lloyd iteration avoids an N x N pairwise distance matrix on long meetings.
    """

    def __call__(self, embeddings, oracle_num=None, **kwargs):
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.preprocessing import normalize
        if hasattr(embeddings, "detach"):
            embeddings = embeddings.detach().cpu().numpy()
        values = np.asarray(embeddings, dtype=np.float32)
        if values.ndim != 2 or not len(values) or not np.isfinite(values).all():
            raise ValueError("说话人特征为空或包含无效数据")
        if oracle_num is None:
            # Keep FunASR's automatic count estimator; this change targets known K.
            import torch
            from funasr.models.campplus.cluster_backend import ClusterBackend
            return ClusterBackend()(torch.from_numpy(values))
        count = int(oracle_num)
        if count < 1 or count > len(values):
            raise ValueError("有效语音片段不足以区分指定说话人数，请降低人数或上传更完整录音")
        if count == 1:
            return np.zeros(len(values), dtype=int)
        if np.any(np.linalg.norm(values, axis=1) < 1e-10):
            raise ValueError("声纹包含全零向量，无法可靠区分说话人")
        values = normalize(values)
        labels = KMeans(n_clusters=count, random_state=0, n_init=20,
                        algorithm="lloyd").fit_predict(values)
        if len(np.unique(labels)) < count:
            raise ValueError("有效声纹种类不足以分成指定人数，请检查实际出声人数")
        # Stable display numbering: speaker 1 is the first observed identity.
        mapping = {}
        return np.array([mapping.setdefault(int(label), len(mapping)) for label in labels])


def configure_clustering(pipeline, model):
    if model == "eres2netv2":
        pipeline.cb_model = MeetingCluster()
        from .diarization import TurnFirstPipeline
        return TurnFirstPipeline(pipeline)
    return pipeline
