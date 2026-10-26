"""Fast preflight; never imports torch/FunASR or installs anything."""
import importlib.metadata as metadata
import importlib.util
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hardware import backend_matches, hardware_plan


def inspect_environment():
    missing = []
    try:
        from packaging.requirements import Requirement
        requirements = ROOT / "requirements.lock.txt"
        if not requirements.is_file():
            requirements = ROOT / "requirements.in"
        for line in requirements.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "-")):
                continue
            requirement = Requirement(line)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            try:
                actual = metadata.version(requirement.name)
                if actual not in requirement.specifier:
                    missing.append(f"{line} (installed: {actual})")
            except metadata.PackageNotFoundError:
                missing.append(line)
    except ImportError:
        missing.append("packaging (dependency checker)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg and importlib.util.find_spec("imageio_ffmpeg"):
        try:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except (RuntimeError, OSError):
            pass
    models_ready = False
    manifest = ROOT / "models" / "local_paths.json"
    try:
        paths = json.loads(manifest.read_text(encoding="utf-8-sig"))
        models_ready = all(
            (ROOT / paths[name] / "config.yaml").is_file()
            and (ROOT / paths[name] / weight).is_file()
            for name, weight in (("model", "model.pt"), ("vad_model", "model.pt"),
                                 ("punc_model", "model.pt"), ("spk_model", "campplus_cn_common.bin"))
        )
    except (OSError, ValueError, KeyError, TypeError):
        pass
    hardware = hardware_plan()
    try:
        torch_version = metadata.version("torch")
        audio_version = metadata.version("torchaudio")
    except metadata.PackageNotFoundError:
        torch_version = audio_version = ""
    acceleration_ok = bool(torch_version and audio_version
                           and backend_matches(torch_version, hardware["backend"])
                           and backend_matches(audio_version, hardware["backend"]))
    result = {"python_ok": sys.version_info[:2] == (3, 12), "python": sys.version.split()[0],
              "packages_ok": not missing, "missing": missing,
              "models_ready": models_ready, "ffmpeg_ok": bool(ffmpeg),
              "hardware": hardware, "torch_version": torch_version, "acceleration_ok": acceleration_ok}
    result["ready"] = all(result[key] for key in ("python_ok", "packages_ok", "models_ready", "ffmpeg_ok"))
    return result


if __name__ == "__main__":
    result = inspect_environment()
    print(json.dumps(result, ensure_ascii=True))
    sys.exit(0 if result["ready"] else 1)
