"""Local FFmpeg silence detection and lossless, sample-accurate chunk planning."""
import re
import shutil
import subprocess

MIN_CHUNK_SECONDS = 10
MAX_CHUNK_SECONDS = 60
SILENCE_DB = -40
SILENCE_SECONDS = .5


def ffmpeg_executable():
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def detect_silences(path, duration):
    """Keep leading/trailing silence; detection never trims the recording."""
    try:
        result = subprocess.run([ffmpeg_executable(), "-nostdin", "-hide_banner", "-nostats",
            "-loglevel", "info", "-i", str(path), "-vn", "-af",
            f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_SECONDS}", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        raise ValueError("本机 FFmpeg 停顿检测失败或超时，请检查音频与 FFmpeg 环境；主稿已保留") from None
    if result.returncode:
        raise ValueError("本机 FFmpeg 无法检测音频停顿，请检查音频文件；主稿已保留")
    pauses, start = [], None
    for kind, value in re.findall(r"silence_(start|end):\s*(-?[\d.]+(?:e[+-]?\d+)?)", result.stderr):
        second = min(duration, max(0., float(value)))
        if kind == "start":
            start = second
        elif start is not None:
            if second > start:
                pauses.append((start, second))
            start = None
    if start is not None and duration > start:
        pauses.append((start, duration))
    return pauses


def pause_chunks(total, rate, pauses):
    """Cover every sample exactly once, with chunks of 10–60 seconds.

    Select the first feasible pause after 10 seconds, including in the tail.
    Cut at its midpoint, clipped to the legal range. Reserve at least 10 seconds
    for the tail, so a long recording never ends with a tiny upload.
    With no feasible pause, enforce the duration bound. A whole recording under
    10 seconds is kept as-is; silence is never added to meet a nominal minimum.
    """
    minimum, maximum = MIN_CHUNK_SECONDS * rate, MAX_CHUNK_SECONDS * rate
    quiet = sorted((max(0, round(a * rate)), min(total, round(b * rate))) for a, b in pauses if b > a)
    chunks, offset = [], 0
    while offset < total:
        remaining = total - offset
        # Two legal chunks need at least 20 seconds in total.
        if remaining < 2 * minimum:
            chunks.append((offset, total))
            break
        lower, upper = offset + minimum, min(offset + maximum, total - minimum)
        cut = None
        for start, end in quiet:
            if start > upper:
                break
            if end < lower:
                continue
            cut = min(upper, max(lower, (start + end) // 2))
            break
        if cut is None:
            # Continuous speech that already fits needs no forced cut.
            if remaining <= maximum:
                chunks.append((offset, total))
                break
            cut = upper
        chunks.append((offset, cut))
        offset = cut
    return chunks
