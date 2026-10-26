"""Local Chinese transcription with timestamps and anonymous speaker labels."""

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("MODELSCOPE_CACHE", str(ROOT / "models" / "modelscope"))
os.environ.setdefault("HF_HOME", str(ROOT / "models" / "huggingface"))
os.environ.setdefault("TORCH_HOME", str(ROOT / "models" / "torch"))
os.environ.setdefault("NUMBA_CACHE_DIR", str(ROOT / "models" / "numba"))
os.environ.setdefault("PYTHONUTF8", "1")


def load_pipeline():
    print("Loading FunASR and selecting the available device; startup can take 1-2 minutes...", flush=True)
    import torch
    from hardware import hardware_plan, select_runtime_device
    device, reason = select_runtime_device(torch)
    print(reason, flush=True)
    from funasr import AutoModel

    names = {
        "model": "paraformer-zh",
        "vad_model": "fsmn-vad",
        "punc_model": "ct-punc",
        "spk_model": "cam++",
    }
    manifest = ROOT / "models" / "local_paths.json"
    if manifest.is_file():
        try:
            paths = json.loads(manifest.read_text(encoding="utf-8-sig"))
            if not isinstance(paths, dict):
                paths = {}
        except (OSError, ValueError):
            paths = {}
        for key in names:
            local = ROOT / paths.get(key, "__missing__")
            weight = "campplus_cn_common.bin" if key == "spk_model" else "model.pt"
            if (local / "config.yaml").is_file() and (local / weight).is_file():
                names[key] = str(local)

    model = AutoModel(
        **names,
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
    args = parser.parse_args()
    if args.speakers is not None and args.speakers < 1:
        parser.error("--speakers must be at least 1")
    if not args.prepare and (args.audio is None or not args.audio.is_file()):
        parser.error("Provide an existing local audio file")

    model = load_pipeline()
    if args.prepare:
        print(f"All four models loaded successfully on {model.kwargs['device']}.")
        return

    options = {"batch_size_s": 60}
    if args.speakers is not None:
        options["preset_spk_num"] = args.speakers
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
