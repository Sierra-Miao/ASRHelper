"""Local Chinese transcription with timestamps and anonymous speaker labels."""

import argparse
import json
import os
from pathlib import Path
from asr_config import PROFILES, selected_profile, local_models, manifest_path, recognition_options

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MODELSCOPE_CACHE", str(ROOT / "models" / "modelscope"))
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(ROOT / "models" / "torch"))
os.environ.setdefault("NUMBA_CACHE_DIR", str(ROOT / "models" / "numba"))
os.environ.setdefault("PYTHONUTF8", "1")


def load_pipeline(profile=None):
    profile = profile or selected_profile()
    from hardware import memory_status
    memory = memory_status()
    minimum = {"nano": 6, "sensevoice": 1.5, "paraformer": 1.5}[profile]
    if memory and memory["available_gb"] < minimum:
        raise MemoryError(f"当前可用内存约 {memory['available_gb']} GB，加载此模型建议至少留出 {minimum} GB 可用内存；请关闭其他程序后重试。")
    print("Loading FunASR and selecting the available device; startup can take 1-2 minutes...", flush=True)
    import torch
    from hardware import hardware_plan, select_runtime_device
    device, reason = select_runtime_device(torch)
    print(reason, flush=True)
    from funasr import AutoModel

    print(PROFILES[profile]["label"], flush=True)
    names = {key: value for key, value in PROFILES[profile].items() if key != "label"}
    names.update(local_models(profile))
    manifest = manifest_path(profile)
    extra = {"vad_kwargs": {"max_single_segment_time": 30000}}
    if profile == "nano":
        # FunASR 1.4.16 contains the implementation; do not install remote code.
        # CPU FP32 avoids slow/unsupported BF16 kernels on older processors.
        extra.update(spk_mode="vad_segment", llm_conf={"llm_dtype": "fp32"})
    model = AutoModel(
        **names,
        **extra,
        device=device,
        ncpu=hardware_plan()["cpu_threads"],
        disable_update=True,
    )
    paths = {}
    for name, config in (
        ("model", model.kwargs),
        ("vad_model", model.vad_kwargs),
        ("punc_model", model.punc_kwargs),
        ("spk_model", model.spk_kwargs),
    ):
        if name not in names:
            continue
        path = Path(config["model_path"]).resolve()
        paths[name] = os.path.relpath(path, ROOT)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(paths, indent=2), encoding="utf-8")
    return model


def timestamp(milliseconds, separator=","):
    milliseconds = max(0, round(milliseconds))
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path, nargs="?", help="Local audio file")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--speakers", type=int, help="Known number of speakers")
    parser.add_argument("--prepare", action="store_true", help="Download and load models only")
    parser.add_argument("--model", choices=PROFILES, default=None, help="ASR profile")
    args = parser.parse_args()
    if args.speakers is not None and args.speakers < 1:
        parser.error("--speakers must be at least 1")
    if not args.prepare and (args.audio is None or not args.audio.is_file()):
        parser.error("Provide an existing local audio file")

    profile = args.model or selected_profile()
    model = load_pipeline(profile)
    if args.prepare:
        print(f"{PROFILES[profile]['label']} loaded successfully on {model.kwargs['device']}.")
        return

    options = recognition_options(profile, speakers=args.speakers)
    result = model.generate(input=str(args.audio.resolve()), **options)

    # Keep the original structured result in addition to readable exports.
    args.output_dir.mkdir(parents=True, exist_ok=True)
    def output_path(extension):
        return args.output_dir / f"{args.audio.stem}.{extension}"

    output_path("json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines, subtitles = [], []
    for item in result:
        for segment in item.get("sentence_info", []):
            start, end = segment["start"], segment["end"]
            speaker = segment.get("spk", "unknown")
            text = segment.get("text", segment.get("sentence", "")).strip()
            label = f"Speaker {speaker}"
            lines.append(
                f"[{timestamp(start, '.')} - {timestamp(end, '.')}] {label}: {text}"
            )
            subtitles.append(
                f"{len(subtitles) + 1}\n"
                f"{timestamp(start)} --> {timestamp(end)}\n{label}: {text}\n"
            )
    if not lines:
        lines = [item.get("text", "") for item in result]
    output_path("txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    output_path("srt").write_text("\n".join(subtitles), encoding="utf-8")
    print("\n".join(lines))
    print(f"Saved JSON, TXT and SRT to: {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
