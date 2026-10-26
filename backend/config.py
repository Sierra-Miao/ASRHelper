import json
import hashlib
import os
from pathlib import Path
from threading import Lock
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("FUNASR_DATA_DIR", ROOT / "data"))
LEGACY_PROMPT = "这是使用ASR对语音转文字后的结果，请你检查中间是否有明显的识别错误并将它纠正，包括但不限于一个人说的一句话被拆开成了多段等"
PROMPT = (ROOT / "prompts" / "asr_correction.txt").read_text(encoding="utf-8").strip()
PREVIOUS_DEFAULTS = {"de60f33ce9e241bdd148f388f69bff59f63a97d546766fec309c154707fb6253",
                     "ba7d79266c391384982be19479273f6c9d817d89a1ebe2a1d6436f09283e3f64",
                     "b9b9bdd4cbb17f0c59cd65a58d8da0e784c88ca777631644a6f9e539364ba07d",
                     "1e49a18469447d785a2b910c281ca16aa8d096d0bd101ccda491e35e935dca65"}


class AISettings(BaseModel):
    enabled: bool = False
    base_url: str = ""
    model: str = ""
    api_key: str = Field(default="", repr=False)
    transport: str = "table"
    prompt: str = PROMPT
    timeout: int = Field(default=900, ge=15, le=900)
    max_tokens: int = Field(default=40960, ge=1024, le=65536)
    batch_rows: int = Field(default=100, ge=10, le=500)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value):
        value = value.strip().rstrip("/")
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("API 地址必须是完整的 http:// 或 https:// 地址")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("API 地址不能包含密码、查询参数或片段")
        return value

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, value):
        if value not in {"table", "file", "agent"}:
            raise ValueError("请选择 API 表格、Excel 附件或 Agent Skill 交接模式")
        return value

    @field_validator("model", "api_key", "prompt")
    @classmethod
    def trim(cls, value):
        return value.strip()

    @property
    def ready(self):
        # An empty key is useful for local compatible servers.
        return self.transport == "agent" or bool(self.base_url and self.model)


class SettingsStore:
    """Persist user-approved API configuration, including the key, locally."""

    def __init__(self, path=DATA / "settings.json"):
        self.path = path
        self.lock = Lock()
        self.settings = AISettings()
        if path.is_file():
            try:
                saved = json.loads(path.read_text(encoding="utf-8-sig"))
                if not isinstance(saved, dict):
                    raise ValueError("Settings must be a JSON object")
                old_prompt = saved.get("prompt")
                migrate_prompt = old_prompt is None or (isinstance(old_prompt, str) and (
                    old_prompt.strip() in {"", LEGACY_PROMPT} or hashlib.sha256(old_prompt.strip().encode()).hexdigest() in PREVIOUS_DEFAULTS))
                if migrate_prompt:
                    saved["prompt"] = PROMPT
                self.settings = AISettings(**saved)
                if migrate_prompt:
                    self._persist(self.settings)
            except (ValueError, OSError):
                pass

    def _persist(self, settings):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(settings.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self):
        with self.lock:
            return self.settings.model_copy(deep=True)

    def public(self):
        settings = self.snapshot()
        result = settings.model_dump(exclude={"api_key"})
        result.update(has_api_key=bool(settings.api_key), ready=settings.ready)
        return result

    def update(self, settings, keep_key=True):
        with self.lock:
            if keep_key and not settings.api_key:
                settings = settings.model_copy(update={"api_key": self.settings.api_key})
            if settings.enabled and not settings.ready:
                raise ValueError("启用 AI 校对前，请填写 API 地址和模型名称")
            self._persist(settings)
            self.settings = settings
        return self.public()
