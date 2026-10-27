"""Detect acoustic speaker turns before ASR; never split text by character count."""
import copy
import math
import re

import numpy as np


def speaker_windows(regions, sample_count, fs=16000, window=1.5, step=.5):
    """Return unpadded, recording-relative windows, including short VAD regions."""
    windows = []
    for start, end in regions:
        start = max(0, round(float(start) * fs / 1000))
        end = min(sample_count, round(float(end) * fs / 1000))
        if end <= start:
            continue
        width, shift = round(window * fs), round(step * fs)
        starts = list(range(start, max(start + 1, end - width + 1), shift))
        if end - starts[-1] > width:
            starts.append(end - width)
        windows.extend((a / fs, min(a + width, end) / fs) for a in starts)
    return windows


def speaker_turns(regions, windows, labels):
    """Partition each VAD region at adjacent window-center midpoints.

    This is a window-resolution acoustic estimate, not word alignment. Short
    turns keep their own labels; no duration-based reassignment is performed.
    """
    if len(windows) != len(labels):
        raise ValueError("声纹数量与音频窗口不匹配")
    turns, cursor = [], 0
    for start_ms, end_ms in regions:
        start, end = float(start_ms) / 1000, float(end_ms) / 1000
        local = []
        while cursor < len(windows) and windows[cursor][0] < end:
            a, b = windows[cursor]
            if a >= start - 1e-6 and b <= end + 1e-6:
                local.append(((a + b) / 2, int(labels[cursor])))
            cursor += 1
        if not local:
            continue
        left, speaker = start, local[0][1]
        for previous, current in zip(local, local[1:]):
            if current[1] == speaker:
                continue
            boundary = (previous[0] + current[0]) / 2
            turns.append({"start": round(left * 1000), "end": round(boundary * 1000), "spk": speaker})
            left, speaker = boundary, current[1]
        turns.append({"start": round(left * 1000), "end": round(end * 1000), "spk": speaker})
    return turns


def asr_chunks(turns, maximum_ms=30000):
    """Bound ASR input length without dropping a short final chunk."""
    chunks = []
    for turn in turns:
        duration = turn["end"] - turn["start"]
        count = max(1, math.ceil(duration / maximum_ms))
        # Equal-sized crops avoid a 1 ms remainder after an otherwise long turn.
        for index in range(count):
            start = turn["start"] + round(duration * index / count)
            end = turn["start"] + round(duration * (index + 1) / count)
            if end > start:
                chunks.append({**turn, "start": start, "end": end})
    return chunks


class TurnFirstPipeline:
    """Own the orchestration, reuse the initialized FunASR sub-models unchanged."""
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def __getattr__(self, name):
        return getattr(self.pipeline, name)

    def generate(self, input, **options):
        from funasr.utils.load_utils import load_audio_text_image_video
        import torch

        pipeline = self.pipeline
        # Web/CLI inputs are a single recording. Keep the native 16 kHz loader.
        if isinstance(input, (list, tuple)):
            raise ValueError("说话人精细识别每次支持一段录音")
        audio = load_audio_text_image_video(str(input) if hasattr(input, "__fspath__") else input,
                                           fs=16000, audio_fs=options.get("fs", 16000))
        audio = torch.as_tensor(audio).flatten().cpu().numpy().astype(np.float32, copy=False)
        vad = pipeline.inference(audio, model=pipeline.vad_model,
            kwargs=copy.copy(pipeline.vad_kwargs), disable_pbar=True)
        regions = sorted(vad[0].get("value", [])) if vad else []
        # VAD boundaries use the exact sample grid, including the final sample.
        regions = [[max(0, round(a)), min(round(len(audio) / 16), round(b))]
                   for a, b in regions if b > a]
        windows = speaker_windows(regions, len(audio))
        if not windows:
            return [{"text": "", "sentence_info": []}]
        features = []
        for offset in range(0, len(windows), 32):
            batch = [audio[round(a * 16000):round(b * 16000)] for a, b in windows[offset:offset + 32]]
            results = pipeline.inference(batch, model=pipeline.spk_model,
                kwargs=copy.copy(pipeline.spk_kwargs), batch_size=32, disable_pbar=True)
            if len(results) != len(batch):
                raise ValueError("声纹提取返回的窗口数量不完整")
            features.extend(item["spk_embedding"].detach().cpu() for item in results)
        labels = pipeline.cb_model(torch.cat(features), oracle_num=options.get("preset_spk_num"))
        turns = speaker_turns(regions, windows, labels)
        chunks = asr_chunks(turns)
        sentences = []
        # Each crop contains one detected speaker. ASR punctuation can no longer
        # merge another speaker into the same row and overwrite their identity.
        for chunk in chunks:
            crop = audio[chunk["start"] * 16:chunk["end"] * 16]
            config = copy.copy(pipeline.kwargs)
            config.pop("cache", None)
            cfg = {k: v for k, v in options.items() if k not in
                   {"preset_spk_num", "sentence_timestamp", "batch_size_s", "batch_size"}}
            result = pipeline.inference(crop, model=pipeline.model, kwargs=config,
                                        **{**cfg, "batch_size": 1, "disable_pbar": True})
            if len(result) != 1:
                raise ValueError("语音转写未返回完整片段，请重试")
            text = re.sub(r"<\|[^>]*\|>", "", result[0].get("text", "")).strip()
            if text and pipeline.punc_model is not None:
                punctuated = pipeline.inference(text, model=pipeline.punc_model,
                    kwargs=copy.copy(pipeline.punc_kwargs), disable_pbar=True)
                if punctuated:
                    text = punctuated[0].get("text", text)
            if text:
                sentences.append({**chunk, "text": text, "sentence": text})
        counts = {str(int(label)): int(np.sum(labels == label)) for label in np.unique(labels)}
        warnings = []
        if len(counts) > 1 and max(counts.values()) / len(windows) > .95:
            warnings.append("超过 95% 的声纹窗口被分给同一人，请核对说话人标签；这可能是主讲人占时很长，也可能是聚类退化，不能仅以已分出指定人数判断成功。")
        return [{"text": " ".join(row["text"] for row in sentences), "sentence_info": sentences,
                 "warnings": warnings,
                 "speaker_diagnostics": {"mode": "turn_first_v1", "window_seconds": 1.5,
                    "step_seconds": .5, "windows": len(windows), "turns": len(turns),
                    "short_turns": sum(row["end"] - row["start"] < 700 for row in turns),
                    "speaker_window_counts": counts}}]
