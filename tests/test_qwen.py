import base64
import io
import json
import tempfile
import time
import unittest
import wave
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

from asr_config import selected_ensemble
from backend.app import create_app
from backend.config import AISettings, SettingsStore
from backend.ensemble import SequentialEnsemble
from backend.qwen import QwenASR, QwenSettings, QwenStore
from backend.service import TranscriptionService
from tests.test_ensemble_and_agent import RESULT, Fake, audio

TEMP = Path(__file__).resolve().parents[1] / "test-results" / "tmp"


def response(text="在线参考"):
    return {"status_code": 200, "output": {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": [{"text": text}]}}]}}


class QwenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=TEMP)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wav(self, seconds):
        path = self.root / "sample.wav"
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(b"\0\0" * round(seconds * 16000))
        return path

    def test_long_audio_covers_all_frames_including_final_short_block(self):
        captured = []
        def caller(**kwargs):
            captured.append(kwargs)
            return response()
        settings = QwenSettings(api_key="fake", chunk_seconds=120, context="测试词汇")
        result = QwenASR(settings, caller).generate(self.wav(301.25))[0]["sentence_info"]
        self.assertEqual([(s["start"], s["end"]) for s in result], [(0, 120000), (120000, 240000), (240000, 301250)])
        frames = 0
        for call in captured:
            self.assertEqual(call["asr_options"], {"enable_itn": True})
            encoded = call["messages"][-1]["content"][0]["audio"].split(",", 1)[1]
            self.assertLess(len(encoded), 10 * 1024 * 1024)
            with wave.open(io.BytesIO(base64.b64decode(encoded))) as wav:
                frames += wav.getnframes()
        self.assertEqual(frames, 4820000)

    def test_official_sdk_wire_endpoint_timeout_and_itn(self):
        def send(session, method, url, **kwargs):
            self.assertEqual(url, "https://dashscope-intl.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
            self.assertEqual(kwargs["timeout"], 900)
            payload = json.loads(kwargs["data"])
            self.assertEqual(payload["model"], "qwen3-asr-flash")
            self.assertTrue(payload["parameters"]["asr_options"]["enable_itn"])
            self.assertNotIn("timeout", payload["parameters"])
            reply = requests.Response(); reply.status_code = 200
            reply._content = json.dumps({"request_id": "mock", "usage": {}, **response()}).encode()
            reply.headers["Content-Type"] = "application/json"
            return reply
        with patch("requests.sessions.Session.request", autospec=True, side_effect=send) as request:
            result = QwenASR(QwenSettings(api_key="fake", base_url="https://dashscope-intl.aliyuncs.com/api/v1")).generate(self.wav(.25))
        self.assertTrue(result[0]["sentence_info"])
        self.assertEqual(request.call_count, 1)

    def test_settings_persist_redact_preserve_and_clear_key(self):
        path = self.root / "qwen_settings.json"
        store = QwenStore(path)
        store.update(QwenSettings(api_key="test-persisted-key"))
        restored = QwenStore(path)
        self.assertEqual(restored.snapshot().api_key, "test-persisted-key")
        self.assertNotIn("test-persisted-key", json.dumps(restored.public()))
        restored.update(QwenSettings(language="zh"))
        self.assertEqual(restored.snapshot().api_key, "test-persisted-key")
        restored.update(QwenSettings(), keep_key=False)
        self.assertFalse(QwenStore(path).public()["ready"])
        with self.assertRaises(ValueError): QwenSettings(base_url="https://fake.invalid/api/v1")

    def test_four_combinations_and_secondary_error_retains_primary(self):
        for primary in ("sensevoice", "paraformer"):
            for reference in ("nano", "qwen"):
                seen = []
                def loader(profile): seen.append(profile); return Fake()
                with patch("backend.qwen.QwenASR.generate", return_value=RESULT):
                    model = SequentialEnsemble(loader=loader, primary=primary, reference=reference, qwen_settings=QwenSettings(api_key="fake"))
                    self.assertEqual(model.generate("sample"), RESULT)
                    self.assertEqual(model.reference_result, RESULT)
                self.assertEqual(seen, [primary] + (["nano"] if reference == "nano" else []))
        model = SequentialEnsemble(loader=lambda _: Fake(), primary="paraformer", reference="qwen")
        with patch("backend.qwen.QwenASR.generate", side_effect=ValueError("调用失败")), self.assertLogs(level="ERROR"):
            self.assertEqual(model.generate("sample"), RESULT)
        self.assertIn("Qwen3-ASR", model.warnings[0]); self.assertIn("Paraformer", model.warnings[0])
        with self.assertRaises(ValueError): SequentialEnsemble(primary="nano", reference="qwen")

    def test_api_activation_persistence_qwen_reference_download_and_handoff(self):
        with patch("backend.service.hardware_plan", return_value={"cpu_threads": 2}):
            service = TranscriptionService(self.root)
        store = SettingsStore(self.root / "settings.json")
        store.update(AISettings(enabled=True, transport="agent"))
        with TestClient(create_app(service, store)) as client:
            self.assertEqual(client.post("/api/models/activate", json={"profile": "ensemble", "primary": "paraformer", "reference": "qwen", "allow_download": True}).status_code, 409)
            saved = client.put("/api/qwen-settings", json={"api_key": "fake-cloud-secret"})
            self.assertEqual(saved.status_code, 200)
            self.assertNotIn("fake-cloud-secret", saved.text)
            model = SequentialEnsemble(loader=lambda _: Fake(), primary="paraformer", reference="qwen")
            with patch("transcribe.load_pipeline", return_value=model) as loader:
                r = client.post("/api/models/activate", json={"profile": "ensemble", "primary": "paraformer", "reference": "qwen", "allow_download": True})
                self.assertEqual(r.status_code, 202, r.text)
                for _ in range(100):
                    if service.model_action["status"] != "loading": break
                    time.sleep(.01)
                self.assertEqual(service.model_action["status"], "completed")
                self.assertEqual(loader.call_args.kwargs["primary"], "paraformer")
            self.assertEqual(selected_ensemble(self.root), {"primary": "paraformer", "reference": "qwen"})
            with patch("backend.qwen.QwenASR.generate", return_value=RESULT), patch("backend.service.models_ready", return_value=True):
                r = client.post("/api/jobs", files={"file": ("mock.wav", audio(), "audio/wav")}, data={"use_ai": "true"})
                self.assertEqual(r.status_code, 202, r.text)
                for _ in range(200):
                    job = client.get(f"/api/jobs/{r.json()['id']}").json()
                    if job["status"] == "completed": break
                    time.sleep(.02)
            self.assertEqual(job["reference_status"], "completed", job)
            self.assertEqual(job["rows"][0]["start_ms"], 100)
            self.assertEqual(client.get(job["downloads"]["reference"]).status_code, 200)
            package = client.get(job["downloads"]["agent"])
            with zipfile.ZipFile(io.BytesIO(package.content)) as archive:
                self.assertIn("Qwen参考.xlsx", archive.namelist())
                self.assertFalse(any(b"fake-cloud-secret" in archive.read(name) for name in archive.namelist()))
                self.assertFalse(any(name.endswith(".json") for name in archive.namelist()))
            client.put("/api/qwen-settings", json={"keep_key": False})
            denied = client.post("/api/jobs", files={"file": ("mock.wav", audio(), "audio/wav")})
            self.assertEqual(denied.status_code, 409)

    def test_provider_failure_and_truncated_response_are_not_success(self):
        for reply in ({"status_code": 401}, {"status_code": 200, "output": {}},
                      {"status_code": 200, "output": {"choices": [{"finish_reason": "length"}]}}):
            with self.assertRaises(ValueError): QwenASR(QwenSettings(api_key="fake"), lambda **_: reply).generate(self.wav(.1))


if __name__ == "__main__": unittest.main()
