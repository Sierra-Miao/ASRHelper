"""Hardware-aware installation planning without importing PyTorch or FunASR."""
import argparse
import csv
import json
import os
import platform
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


def memory_status():
    """Current Windows RAM, without WMI permissions or importing torch."""
    if os.name != "nt":
        return {}
    try:
        import ctypes
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page", "free_page", "virtual", "free_virtual", "extended")]
        status = Status()
        status.length = ctypes.sizeof(Status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return {"total_gb": round(status.total / 2**30, 1), "available_gb": round(status.available / 2**30, 1)}
    except (OSError, AttributeError):
        pass
    return {}


def _run(command, timeout=15):
    try:
        process = subprocess.run(command, capture_output=True, timeout=timeout,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode == 0:
            return process.stdout.decode("utf-8-sig", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def choose_backend(info, force_cpu=False):
    """Choose only CUDA builds published for the pinned PyTorch 2.8 release."""
    result = dict(info)
    result["gpu_names"] = [name for name in (info.get("gpu_names") or []) if isinstance(name, str) and name.strip()]
    logical = int(info.get("logical_cores") or os.cpu_count() or 4)
    physical = int(info.get("physical_cores") or logical)
    result.update(cpu_threads=max(1, min(8, physical)), backend="cpu", device="cpu", gpu_index=None)
    if force_cpu:
        result["reason"] = "已指定 CPU 模式"
        return result
    match = re.search(r"(\d+)\.(\d+)", str(info.get("cuda_version", "")))
    driver_cuda = tuple(map(int, match.groups())) if match else (0, 0)
    candidates = []
    for gpu in info.get("nvidia_gpus", []):
        try:
            capability = float(gpu["compute_cap"])
        except (KeyError, ValueError, TypeError):
            continue
        # CUDA 12.8 wheels omit pre-Turing architectures; use cu126 for them.
        if driver_cuda >= (12, 8) and capability >= 7.5:
            backend = "cu128"
        elif driver_cuda >= (12, 6) and 5.0 <= capability < 10.0:
            backend = "cu126"
        else:
            continue
        candidates.append((int(gpu.get("memory_mb") or 0), gpu, backend))
    if candidates:
        _, gpu, backend = max(candidates, key=lambda item: item[0])
        result.update(backend=backend, device=f"cuda:{gpu['index']}", gpu_index=gpu["index"],
                      reason=f"检测到 {gpu['name']}，选择 PyTorch 2.8 {backend}；运行时还会验证 GPU 运算")
    elif info.get("nvidia_gpus"):
        result["reason"] = "NVIDIA 驱动或 GPU 架构未满足本项目 CUDA 构建条件，使用 CPU"
    elif any("amd" in name.lower() or "radeon" in name.lower() for name in result["gpu_names"]):
        result["reason"] = "检测到 AMD 显卡；本项目的 Windows/PyTorch 2.8 配置使用 CPU，不安装 CUDA 或 DirectML"
    else:
        result["reason"] = "未检测到可用的 NVIDIA CUDA 设备，使用 CPU"
    return result


@lru_cache(maxsize=1)
def detect_hardware():
    info = {"cpu_name": platform.processor() or "CPU", "logical_cores": os.cpu_count() or 4,
            "physical_cores": None, "gpu_names": [], "nvidia_gpus": [], "cuda_version": ""}
    if os.name == "nt":
        script = """[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false);
$cpu = Get-CimInstance Win32_Processor -ErrorAction Stop;
$gpu = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue;
@{cpu_name=($cpu.Name -join ', ').Trim(); physical_cores=($cpu | Measure-Object NumberOfCores -Sum).Sum;
logical_cores=($cpu | Measure-Object NumberOfLogicalProcessors -Sum).Sum;
gpu_names=@($gpu | ForEach-Object { $_.Name })} | ConvertTo-Json -Compress"""
        raw = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
        try:
            if raw:
                info.update(json.loads(raw))
        except (ValueError, TypeError):
            pass
    nvidia = shutil.which("nvidia-smi")
    if not nvidia and os.name == "nt":
        candidates = [Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/nvidia-smi.exe",
                      Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "NVIDIA Corporation/NVSMI/nvidia-smi.exe"]
        nvidia = next((str(path) for path in candidates if path.is_file()), None)
    if nvidia:
        text = _run([nvidia])
        match = re.search(r"CUDA Version:\s*([\d.]+)", text)
        if match:
            info["cuda_version"] = match.group(1)
        query = _run([nvidia, "--query-gpu=index,name,memory.total,compute_cap", "--format=csv,noheader,nounits"])
        for row in csv.reader(query.splitlines()):
            try:
                index, name, memory, capability = [value.strip() for value in row]
                info["nvidia_gpus"].append({"index": int(index), "name": name,
                    "memory_mb": int(float(memory)), "compute_cap": float(capability)})
            except (ValueError, TypeError):
                continue
        if text and not info["nvidia_gpus"]:
            info["nvidia_gpus"] = [{"index": 0, "name": "NVIDIA GPU", "compute_cap": None}]
    return info


def hardware_plan():
    return choose_backend(detect_hardware(), os.environ.get("FUNASR_DEVICE", "auto").lower() == "cpu")


def backend_matches(version, backend):
    # GPU builds can still run on CPU when no supported accelerator is available.
    return backend == "cpu" or version.endswith("+" + backend)


def select_runtime_device(torch):
    """Validate a real CUDA operation before promising GPU acceleration."""
    plan = hardware_plan()
    if plan["backend"] == "cpu":
        return "cpu", plan["reason"]
    try:
        if torch.cuda.is_available():
            preferred = plan["gpu_index"]
            if os.environ.get("CUDA_VISIBLE_DEVICES") is not None:
                preferred = 0
            count = torch.cuda.device_count()
            indexes = [preferred] + [index for index in range(count) if index != preferred]
            for index in indexes:
                if index is None or index >= count:
                    continue
                try:
                    device = f"cuda:{index}"
                    torch.tensor([1.0], device=device).add_(1).item()
                    return device, f"使用 {torch.cuda.get_device_name(index)} 进行 CUDA 加速"
                except Exception:
                    continue
    except Exception:
        pass
    return "cpu", "CUDA 运算验证未通过，已回退到 CPU；可检查 NVIDIA 驱动或使用 -Device cpu 启动"


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true", help="Also run a small PyTorch operation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = hardware_plan()
    if args.verify:
        import torch
        report["runtime_device"], report["runtime_reason"] = select_runtime_device(torch)
    print(json.dumps(report, ensure_ascii=True))
