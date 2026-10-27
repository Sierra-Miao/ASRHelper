"""Qwen3-ASR reference transcription using the official DashScope SDK."""
import base64
import io
import json
import re
import wave
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, Field, field_validator
from .config import DATA
from .audio import detect_silences, pause_chunks, MIN_CHUNK_SECONDS, MAX_CHUNK_SECONDS


class QwenSettings(BaseModel):
    api_key: str = Field(default="", repr=False)
    base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    model: str = "qwen3-asr-flash"
    timeout: int = Field(default=900, ge=15, le=900)
    language: str = ""
    context: str = Field(default="", max_length=5000)

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value):
        from urllib.parse import urlsplit
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        # Avoid sending the cloud key/audio to an accidental arbitrary endpoint.
        if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.port not in {None, 443} or parsed.path != "/api/v1"
                or not (host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com", "dashscope-us.aliyuncs.com"}
                        or re.fullmatch(r"[a-zA-Z0-9-]+\.[a-zA-Z0-9-]+\.maas\.aliyuncs\.com", host))):
            raise ValueError("请填写阿里云 DashScope 的 HTTPS /api/v1 地址，支持地域及业务空间专用域名")
        return value

    @field_validator("api_key", "language", "context", "model")
    @classmethod
    def trim(cls, value):
        return value.strip()

    @field_validator("model")
    @classmethod
    def validate_model(cls, value):
        if not re.fullmatch(r"qwen3-asr-flash(?:-\d{4}-\d{2}-\d{2})?", value):
            raise ValueError("此接口支持 qwen3-asr-flash 或其日期快照版本")
        return value

    @field_validator("language")
    @classmethod
    def validate_language(cls, value):
        if value and not re.fullmatch(r"[a-z]{2,3}", value):
            raise ValueError("语言请使用 zh、en、yue 等代码；留空自动识别")
        return value


class QwenStore:
    def __init__(self, path=DATA / "qwen_settings.json"):
        self.path, self.lock = Path(path), Lock()
        self.settings = QwenSettings()
        if self.path.is_file():
            try:
                self.settings = QwenSettings(**json.loads(self.path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError, TypeError):
                pass

    def snapshot(self):
        with self.lock:
            return self.settings.model_copy(deep=True)

    def public(self):
        settings = self.snapshot()
        return {**settings.model_dump(exclude={"api_key"}), "has_api_key": bool(settings.api_key), "ready": bool(settings.api_key),
                "chunking": {"mode": "silence", "minimum_seconds": MIN_CHUNK_SECONDS, "maximum_seconds": MAX_CHUNK_SECONDS}}

    def update(self, settings, keep_key=True):
        with self.lock:
            if keep_key and not settings.api_key:
                settings = settings.model_copy(update={"api_key": self.settings.api_key})
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            self.settings = settings
        return self.public()


class QwenASR:
    def __init__(self, settings, caller=None):
        self.settings, self.caller = settings, caller
        self.kwargs = {"device": "cloud"}

    def generate(self, input, progress=None, **kwargs):
        if not self.settings.api_key:
            raise ValueError("请先保存 Qwen3-ASR 的 DashScope API Key")
        caller = self.caller
        if caller is None:
            try:
                from dashscope import MultiModalConversation
            except ImportError:
                raise ValueError("缺少 DashScope SDK，请重新运行一键启动并同意安装依赖") from None
            caller = MultiModalConversation.call
        progress = progress or (lambda _: None)
        segments = []
        # FFmpeg detects pauses locally; PCM slicing uses exact sample positions
        # without another encoding pass, dropping silence, or overlapping uploads.
        with wave.open(str(input), "rb") as audio:
            rate, width, channels = audio.getframerate(), audio.getsampwidth(), audio.getnchannels()
            if (rate, width, channels) != (16000, 2, 1):
                raise ValueError("在线识别需要单声道 16 kHz PCM 音频，请通过网页上传并解码")
            total = audio.getnframes()
            progress("Qwen3-ASR：正在本机通过 FFmpeg 检测停顿，规划 10–60 秒音频片段（主稿已保存）")
            pauses = detect_silences(input, total / rate) if total >= 2 * MIN_CHUNK_SECONDS * rate else []
            blocks = pause_chunks(total, rate, pauses)
            for index, (offset, end) in enumerate(blocks, 1):
                count = end - offset
                audio.setpos(offset)
                pcm = audio.readframes(count)
                if len(pcm) != count * width * channels:
                    raise ValueError("在线识别读取音频不完整，主稿已保留")
                buffer = io.BytesIO()
                with wave.open(buffer, "wb") as chunk:
                    chunk.setnchannels(channels); chunk.setsampwidth(width); chunk.setframerate(rate)
                    chunk.writeframes(pcm)
                data = "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                messages = []
                if self.settings.context:
                    messages.append({"role": "system", "content": [{"text": self.settings.context}]})
                messages.append({"role": "user", "content": [{"audio": data}]})
                options = {"enable_itn": True}
                if self.settings.language:
                    options["language"] = self.settings.language
                progress(f"Qwen3-ASR 在线参考：{index}/{len(blocks)} 段 · {count / rate:.1f} 秒（主稿已保存）")
                try:
                    response = caller(model=self.settings.model, api_key=self.settings.api_key,
                        base_address=self.settings.base_url, messages=messages, result_format="message",
                        asr_options=options, request_timeout=self.settings.timeout)
                except Exception:
                    # Provider exceptions can contain credentials or submitted audio.
                    raise ValueError("Qwen3-ASR 请求失败或超时，请检查网络、地域、密钥与服务额度；主稿已保留") from None
                status = response.get("status_code")
                if status != 200:
                    hints = {401: "密钥或地域不匹配", 403: "没有模型权限", 429: "限流或额度不足", 400: "模型、输入或参数不受支持"}
                    raise ValueError("Qwen3-ASR 调用失败：" + hints.get(status, f"服务状态 {status}") + "；主稿已保留")
                try:
                    choice = response["output"]["choices"][0]
                    if choice.get("finish_reason") == "length":
                        raise ValueError()
                    content = choice["message"]["content"]
                    text = content if isinstance(content, str) else "".join(p.get("text", "") for p in content)
                    if not isinstance(text, str):
                        raise ValueError()
                except (KeyError, IndexError, TypeError, ValueError):
                    raise ValueError("Qwen3-ASR 响应缺失或被截断，参考识别未完成；主稿已保留") from None
                if text.strip():
                    segments.append({"start": round(offset * 1000 / rate), "end": round((offset + count) * 1000 / rate), "text": text.strip()})
        if not segments:
            raise ValueError("Qwen3-ASR 没有返回有效参考文字；主稿已保留")
        return [{"sentence_info": segments}]
