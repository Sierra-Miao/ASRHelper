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
        settings = QwenSettings(api_key="fake", context="测试词汇")
        result = QwenASR(settings, caller).generate(self.wav(301.25))[0]["sentence_info"]
        self.assertEqual(result[0]["start"], 0)
        self.assertEqual(result[-1]["end"], 301250)
        self.assertTrue(all(10000 <= s["end"] - s["start"] <= 60000 for s in result))
        self.assertTrue(all(a["end"] == b["start"] for a, b in zip(result, result[1:])))
        frames = 0
        for call in captured:
            self.assertEqual(call["asr_options"], {"enable_itn": True})
            encoded = call["messages"][-1]["content"][0]["audio"].split(",", 1)[1]
            self.assertLess(len(encoded), 10 * 1024 * 1024)
            with wave.open(io.BytesIO(base64.b64decode(encoded))) as wav:
                frames += wav.getnframes()
        self.assertEqual(frames, 4820000)

    def test_pause_uploads_are_variable_length_and_preserve_every_pcm_sample(self):
        import numpy as np
        path = self.wav(130)
        rate = 16000
        pcm = (np.sin(np.arange(130 * rate) * (2 * np.pi * 440 / rate)) * 8000).astype("<i2")
        for start, end in [(23, 23.2), (48, 48.8), (72, 72.8), (98, 98.8), (114, 114.8)]:
            interval = slice(round(start * rate), round(end * rate))
            # A -54 dB tone qualifies at -40 dB; the 0.2 s gap is too short.
            pcm[interval] = pcm[interval] // 128 if start == 48 else 0
        # A -30 dB tone must no longer qualify as silence at the new threshold.
        pcm[35 * rate:round(35.8 * rate)] //= 8
        with wave.open(str(path), "wb") as wav:
            wav.setparams((1, 2, rate, 0, "NONE", "not compressed")); wav.writeframes(pcm.tobytes())
        from backend.audio import detect_silences
        pauses = detect_silences(path, 130)
        self.assertEqual(len(pauses), 4)
        self.assertFalse(any(22 < start < 24 for start, end in pauses))
        uploaded, frame_counts, progress = [], [], []
        def caller(**kwargs):
            encoded = kwargs["messages"][-1]["content"][0]["audio"].split(",", 1)[1]
            with wave.open(io.BytesIO(base64.b64decode(encoded))) as chunk:
                frame_counts.append(chunk.getnframes()); uploaded.append(chunk.readframes(chunk.getnframes()))
            return response()
        result = QwenASR(QwenSettings(api_key="fake"), caller).generate(path, progress.append)[0]["sentence_info"]
        self.assertEqual(len(result), 5)
        self.assertTrue(48000 <= result[0]["end"] <= 49200)
        self.assertTrue(72000 <= result[1]["end"] <= 72800)
        self.assertTrue(98000 <= result[2]["end"] <= 98800)
        self.assertEqual(result[-1]["end"], 130000)
        self.assertTrue(all(10 * rate <= count <= 60 * rate for count in frame_counts))
        self.assertEqual(b"".join(uploaded), pcm.tobytes())
        self.assertIn("5/5", progress[-1])

    def test_recording_under_60_seconds_also_detects_and_uploads_separate_turns(self):
        path = self.wav(45)
        calls = []
        def caller(**kwargs):
            calls.append(kwargs)
            return response()
        with patch("backend.qwen.detect_silences", return_value=[(12, 13), (24, 25)]) as detect:
            result = QwenASR(QwenSettings(api_key="fake"), caller).generate(path)[0]["sentence_info"]
        detect.assert_called_once_with(path, 45)
        self.assertEqual([(row["start"], row["end"]) for row in result], [(0, 12500), (12500, 24500), (24500, 45000)])
        self.assertEqual(len(calls), 3)

    def test_silence_detection_failure_does_not_call_cloud_or_expose_stderr(self):
        caller = MagicMock()
        with patch("backend.audio.subprocess.run", return_value=MagicMock(returncode=1, stderr="private failure details")):
            with self.assertRaises(ValueError) as error:
                QwenASR(QwenSettings(api_key="fake"), caller).generate(self.wav(61))
        caller.assert_not_called()
        self.assertIn("主稿已保留", str(error.exception))
        self.assertNotIn("private failure details", str(error.exception))

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

    def test_legacy_chunk_setting_is_ignored_without_losing_saved_api_configuration(self):
        path = self.root / "qwen_settings.json"
        path.write_text(json.dumps({"api_key": "saved-secret", "chunk_seconds": 120, "language": "zh", "timeout": 900}))
        store = QwenStore(path)
        self.assertEqual(store.snapshot().api_key, "saved-secret")
        self.assertEqual(store.snapshot().language, "zh")
        self.assertEqual(store.public()["chunking"], {"mode": "silence", "minimum_seconds": 10, "maximum_seconds": 60})
        self.assertNotIn("chunk_seconds", store.public())

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
